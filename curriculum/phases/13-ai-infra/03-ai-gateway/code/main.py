"""tokengate: an OpenAI-compatible AI gateway. It routes each request by its model name to one or more backends, enforces per-key request and token budgets with token buckets (an estimate charged up front, the response's usage settled afterwards), retries and falls back on 429 and 5xx, passes SSE streams through while counting their tokens, answers exact repeats from a cache, logs every request with the prompt redacted, and prices it from a price table."""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid
from collections import OrderedDict
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

CHARS_PER_TOKEN = 4
MESSAGE_OVERHEAD_TOKENS = 4
REPLY_PRIMING_TOKENS = 3
MAX_BODY_BYTES = 4 * 1024 * 1024
UNREACHABLE = 0
RETRYABLE = (UNREACHABLE, 429, 500, 502, 503, 504)
ENDPOINTS = ("/v1/chat/completions", "/v1/completions")
CACHE_MODES = ("off", "deterministic", "all")
CACHE_SCOPES = ("key", "global")
LOG_MODES = ("hash", "redacted", "full")
IGNORED_IN_CACHE_KEY = ("stream", "stream_options", "user")
HOP_BY_HOP = {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization", "te", "trailers", "transfer-encoding", "upgrade", "content-length"}
DURATION = re.compile(r"(\d+(?:\.\d+)?)(ms|h|m|s)")
DURATION_SCALE = {"h": 3600.0, "m": 60.0, "s": 1.0, "ms": 0.001}
REDACTIONS = [
    ("EMAIL", re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")),
    ("API_KEY", re.compile(r"\b(?:sk|pk|rk)-[A-Za-z0-9_-]{8,}")),
    ("AWS_KEY", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("CARD", re.compile(r"\b(?:\d[ -]?){12,18}\d\b")),
    ("IP", re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")),
]


class InputError(ValueError):
    pass


@dataclass
class Backend:
    name: str
    url: str
    api_key_env: str = ""
    timeout: float = 60.0


@dataclass
class Deployment:
    route: str
    backend: str
    model: str

    @property
    def label(self) -> str:
        return f"{self.backend}/{self.model}"


@dataclass
class KeyPolicy:
    name: str
    digest: str
    rpm: Optional[int] = None
    tpm: Optional[int] = None
    models: List[str] = field(default_factory=list)

    def allows(self, route: str) -> bool:
        return not self.models or route in self.models


@dataclass
class Price:
    input_per_mtok: float
    output_per_mtok: float

    def cost(self, prompt_tokens: int, completion_tokens: int) -> float:
        return round((prompt_tokens * self.input_per_mtok + completion_tokens * self.output_per_mtok) / 1_000_000, 10)


@dataclass
class PriceTable:
    prices: Dict[str, Price]
    date: str = ""
    source: str = ""
    currency: str = "USD"

    def lookup(self, deployment: Deployment) -> Optional[Price]:
        return self.prices.get(deployment.label) or self.prices.get(deployment.model)


@dataclass
class Config:
    backends: Dict[str, Backend]
    routes: Dict[str, List[Deployment]]
    keys: Dict[str, KeyPolicy]
    prices: PriceTable
    retries: int = 1
    backoff_seconds: float = 0.25
    max_retry_wait: float = 2.0
    cooldown_seconds: float = 30.0
    default_max_tokens: int = 256
    cache_mode: str = "deterministic"
    cache_ttl: float = 300.0
    cache_max_entries: int = 1000
    cache_scope: str = "key"
    log_prompts: str = "redacted"
    warnings: List[str] = field(default_factory=list)


def fingerprint(secret: str) -> str:
    return hashlib.sha256(secret.encode()).hexdigest()


def read_json(path: str) -> Any:
    try:
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)
    except OSError as exc:
        raise InputError(f"{path}: {exc.strerror}")
    except json.JSONDecodeError as exc:
        raise InputError(f"{path}: line {exc.lineno}: {exc.msg}")


def positive(value: Any, where: str, kind: type = int, allow_zero: bool = False) -> Any:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or (kind is int and not isinstance(value, int)):
        raise InputError(f"{where}: expected a number, got {value!r}")
    if value < 0 or (value == 0 and not allow_zero):
        raise InputError(f"{where}: must be {'zero or more' if allow_zero else 'positive'}, got {value!r}")
    return kind(value)


def choice(value: Any, allowed: Sequence[str], where: str) -> str:
    if value not in allowed:
        raise InputError(f"{where}: {value!r} is not one of {', '.join(allowed)}")
    return value


def load_prices(raw: Any, base_dir: str) -> PriceTable:
    if raw is None:
        return PriceTable({})
    if isinstance(raw, str):
        raw = read_json(os.path.join(base_dir, raw))
    if not isinstance(raw, dict):
        raise InputError("prices: expected an object or the name of a JSON file")
    for name in ("date", "source"):
        if not raw.get(name):
            raise InputError(f"prices: a price table needs a {name!r}; a price without a date and a source is a guess")
    entries: Dict[str, Price] = {}
    for name, item in (raw.get("per_million_tokens") or {}).items():
        try:
            entries[name] = Price(float(item["input"]), float(item["output"]))
        except (KeyError, TypeError, ValueError):
            raise InputError(f"prices.per_million_tokens.{name}: needs numeric 'input' and 'output'")
    return PriceTable(entries, str(raw["date"]), str(raw["source"]), str(raw.get("currency", "USD")))


def load_config(raw: Any, base_dir: str = ".", overrides: Optional[Dict[str, str]] = None) -> Config:
    if not isinstance(raw, dict):
        raise InputError("config: expected a JSON object with backends, models and keys")
    backends: Dict[str, Backend] = {}
    for name, item in (raw.get("backends") or {}).items():
        url = (overrides or {}).get(name) or (item or {}).get("url")
        if not url:
            raise InputError(f"backends.{name}: needs a url (or --backend {name}=URL)")
        timeout = positive(item.get("timeout_seconds", 60.0), f"backends.{name}.timeout_seconds", float)
        backends[name] = Backend(name, url.rstrip("/"), item.get("api_key_env", ""), timeout)
    for name in overrides or {}:
        if name not in backends:
            raise InputError(f"--backend {name}=...: no backend named {name!r} in the config (have: {', '.join(sorted(backends)) or 'none'})")
    routes: Dict[str, List[Deployment]] = {}
    for route, items in (raw.get("models") or {}).items():
        if not isinstance(items, list) or not items:
            raise InputError(f"models.{route}: expected a non-empty list of deployments, primary first")
        routes[route] = []
        for index, item in enumerate(items):
            where = f"models.{route}[{index}]"
            if item.get("backend") not in backends:
                raise InputError(f"{where}: backend {item.get('backend')!r} is not defined under backends")
            if not item.get("model"):
                raise InputError(f"{where}: needs the upstream model name")
            routes[route].append(Deployment(route, item["backend"], item["model"]))
    if not routes:
        raise InputError("config: models is empty; nothing to route")
    keys: Dict[str, KeyPolicy] = {}
    names = set()
    for index, item in enumerate(raw.get("keys") or []):
        where = f"keys[{index}]"
        name = item.get("name")
        digest = item.get("key_sha256") or (fingerprint(item["key"]) if item.get("key") else "")
        if not name or not digest:
            raise InputError(f"{where}: needs a name and either key or key_sha256")
        if name in names or digest in keys:
            raise InputError(f"{where}: duplicate key name or secret ({name})")
        allowed = list(item.get("models") or [])
        for route in allowed:
            if route not in routes:
                raise InputError(f"{where}.models: {route!r} is not a route under models")
        names.add(name)
        keys[digest] = KeyPolicy(name, digest, positive(item.get("rpm"), f"{where}.rpm"), positive(item.get("tpm"), f"{where}.tpm"), allowed)
    retry = raw.get("retry") or {}
    cache = raw.get("cache") or {}
    config = Config(
        backends=backends,
        routes=routes,
        keys=keys,
        prices=load_prices(raw.get("prices"), base_dir),
        retries=positive(retry.get("retries", 1), "retry.retries", int, allow_zero=True),
        backoff_seconds=positive(retry.get("backoff_seconds", 0.25), "retry.backoff_seconds", float, allow_zero=True),
        max_retry_wait=positive(retry.get("max_wait_seconds", 2.0), "retry.max_wait_seconds", float, allow_zero=True),
        cooldown_seconds=positive(retry.get("cooldown_seconds", 30.0), "retry.cooldown_seconds", float, allow_zero=True),
        default_max_tokens=positive((raw.get("limits") or {}).get("default_max_tokens", 256), "limits.default_max_tokens"),
        cache_mode=choice(cache.get("mode", "deterministic"), CACHE_MODES, "cache.mode"),
        cache_ttl=positive(cache.get("ttl_seconds", 300.0), "cache.ttl_seconds", float),
        cache_max_entries=positive(cache.get("max_entries", 1000), "cache.max_entries"),
        cache_scope=choice(cache.get("scope", "key"), CACHE_SCOPES, "cache.scope"),
        log_prompts=choice((raw.get("log") or {}).get("prompts", "redacted"), LOG_MODES, "log.prompts"),
    )
    for route, deployments in routes.items():
        for deployment in deployments:
            if config.prices.lookup(deployment) is None:
                config.warnings.append(f"no price for {deployment.label}: requests through it are logged with cost unknown")
    if not keys:
        config.warnings.append("no keys: every request will get 401")
    return config


def load_config_file(path: str, overrides: Optional[Dict[str, str]] = None) -> Config:
    return load_config(read_json(path), os.path.dirname(os.path.abspath(path)), overrides)


@dataclass
class Bucket:
    capacity: float
    per_second: float
    level: float
    updated: float

    @classmethod
    def per_minute(cls, limit: int, now: float) -> "Bucket":
        return cls(float(limit), limit / 60.0, float(limit), now)

    def refill(self, now: float) -> None:
        if now > self.updated:
            self.level = min(self.capacity, self.level + (now - self.updated) * self.per_second)
            self.updated = now

    def wait_for(self, amount: float, now: float) -> float:
        self.refill(now)
        return 0.0 if self.level >= amount else (amount - self.level) / self.per_second

    def adjust(self, amount: float, now: float) -> None:
        self.refill(now)
        self.level = min(self.capacity, self.level + amount)

    def remaining(self, now: float) -> int:
        self.refill(now)
        return max(0, int(self.level))

    def reset_in(self, now: float) -> float:
        self.refill(now)
        return max(0.0, (self.capacity - self.level) / self.per_second)


@dataclass
class Admission:
    allowed: bool
    estimate: int
    limited_by: str = ""
    retry_after: float = 0.0
    message: str = ""


class Limiter:
    def __init__(self, clock: Callable[[], float]):
        self.clock = clock
        self.buckets: Dict[str, Dict[str, Bucket]] = {}

    def buckets_for(self, policy: KeyPolicy, now: float) -> Dict[str, Bucket]:
        if policy.name not in self.buckets:
            found: Dict[str, Bucket] = {}
            if policy.rpm:
                found["requests"] = Bucket.per_minute(policy.rpm, now)
            if policy.tpm:
                found["tokens"] = Bucket.per_minute(policy.tpm, now)
            self.buckets[policy.name] = found
        return self.buckets[policy.name]

    def admit(self, policy: KeyPolicy, estimate: int) -> Admission:
        now = self.clock()
        buckets = self.buckets_for(policy, now)
        tokens = buckets.get("tokens")
        if tokens is not None and estimate > tokens.capacity:
            return Admission(False, estimate, "too_large", 0.0, f"key {policy.name}: this request may use {estimate} tokens (prompt estimate plus max_tokens) but the key allows {int(tokens.capacity)} tokens per minute; lower max_tokens or shorten the prompt")
        needs = {"requests": 1, "tokens": estimate}
        blocked = {kind: bucket.wait_for(needs[kind], now) for kind, bucket in buckets.items()}
        blocked = {kind: wait for kind, wait in blocked.items() if wait > 0}
        if blocked:
            kind = max(blocked, key=lambda k: blocked[k])
            bucket = buckets[kind]
            unit = "RPM" if kind == "requests" else "TPM"
            message = f"Rate limit reached for key {policy.name} on {kind} per min ({unit}): Limit {int(bucket.capacity)}, Remaining {bucket.remaining(now)}, Requested {needs[kind]}. Please try again in {format_duration(blocked[kind])}."
            return Admission(False, estimate, kind, blocked[kind], message)
        for kind, bucket in buckets.items():
            bucket.adjust(-needs[kind], now)
        return Admission(True, estimate)

    def settle(self, policy: KeyPolicy, estimate: int, actual: int) -> None:
        now = self.clock()
        bucket = self.buckets_for(policy, now).get("tokens")
        if bucket is not None:
            bucket.adjust(estimate - actual, now)

    def headers(self, policy: KeyPolicy) -> List[Tuple[str, str]]:
        now = self.clock()
        out: List[Tuple[str, str]] = []
        for kind, bucket in sorted(self.buckets_for(policy, now).items()):
            out.append((f"x-ratelimit-limit-{kind}", str(int(bucket.capacity))))
            out.append((f"x-ratelimit-remaining-{kind}", str(bucket.remaining(now))))
            out.append((f"x-ratelimit-reset-{kind}", format_duration(bucket.reset_in(now))))
        return out


def format_duration(seconds: float) -> str:
    if seconds <= 0:
        return "0s"
    if seconds < 1:
        return f"{max(1, math.ceil(seconds * 1000))}ms"
    whole = math.ceil(seconds)
    hours, rest = divmod(whole, 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return f"{hours}h{minutes}m{secs}s"
    if minutes:
        return f"{minutes}m{secs}s"
    return f"{secs}s"


def parse_duration(text: str) -> Optional[float]:
    text = (text or "").strip()
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        pass
    parts = DURATION.findall(text)
    if not parts or "".join(number + unit for number, unit in parts) != text:
        return None
    return sum(float(number) * DURATION_SCALE[unit] for number, unit in parts)


def provider_wait(headers: Dict[str, str], now: float) -> Optional[float]:
    if headers.get("retry-after-ms"):
        try:
            return max(0.0, float(headers["retry-after-ms"]) / 1000)
        except ValueError:
            pass
    value = headers.get("retry-after")
    if value:
        try:
            return max(0.0, float(value))
        except ValueError:
            try:
                return max(0.0, parsedate_to_datetime(value).timestamp() - now)
            except (TypeError, ValueError):
                pass
    waits = []
    for kind in ("requests", "tokens"):
        if headers.get(f"x-ratelimit-remaining-{kind}") == "0":
            found = parse_duration(headers.get(f"x-ratelimit-reset-{kind}", ""))
            if found is not None:
                waits.append(found)
    return max(waits) if waits else None


def text_of(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(part.get("text", "") if isinstance(part, dict) else str(part) for part in content)
    return str(content)


def prompt_text(body: Dict[str, Any]) -> str:
    if "messages" in body:
        return "\n".join(text_of(message.get("content")) for message in body.get("messages") or [] if isinstance(message, dict))
    prompt = body.get("prompt", "")
    return "\n".join(str(p) for p in prompt) if isinstance(prompt, list) else str(prompt)


def estimate_text_tokens(chars: int) -> int:
    return math.ceil(chars / CHARS_PER_TOKEN)


def estimate_prompt_tokens(body: Dict[str, Any]) -> int:
    if "messages" in body:
        messages = [m for m in body.get("messages") or [] if isinstance(m, dict)]
        return REPLY_PRIMING_TOKENS + sum(MESSAGE_OVERHEAD_TOKENS + estimate_text_tokens(len(text_of(m.get("content")))) for m in messages)
    return estimate_text_tokens(len(prompt_text(body)))


def requested_output_tokens(body: Dict[str, Any], default: int) -> int:
    for name in ("max_completion_tokens", "max_tokens"):
        value = body.get(name)
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
            return value
    return default


def estimate_tokens(body: Dict[str, Any], default_max_tokens: int) -> int:
    choices = body.get("n") if isinstance(body.get("n"), int) and body.get("n") > 0 else 1
    return estimate_prompt_tokens(body) + choices * requested_output_tokens(body, default_max_tokens)


def usage_tokens(usage: Any) -> Optional[Tuple[int, int]]:
    if not isinstance(usage, dict):
        return None
    prompt, completion = usage.get("prompt_tokens"), usage.get("completion_tokens")
    if isinstance(prompt, int) and isinstance(completion, int):
        return prompt, completion
    return None


def response_text(data: Any) -> str:
    if not isinstance(data, dict):
        return ""
    parts = []
    for item in data.get("choices") or []:
        if isinstance(item, dict):
            parts.append(text_of((item.get("message") or {}).get("content")) + str(item.get("text") or ""))
    return "".join(parts)


def parse_body(raw: bytes, path: str) -> Dict[str, Any]:
    try:
        body = json.loads(raw.decode("utf-8") or "null")
    except (UnicodeDecodeError, ValueError) as exc:
        raise InputError(f"request body is not JSON: {exc}")
    if not isinstance(body, dict):
        raise InputError("request body must be a JSON object")
    if not isinstance(body.get("model"), str) or not body["model"]:
        raise InputError("you must provide a model parameter")
    if path.endswith("/chat/completions") and not isinstance(body.get("messages"), list):
        raise InputError("'messages' is required and must be a list")
    if path.endswith("/v1/completions") and "prompt" not in body:
        raise InputError("'prompt' is required")
    return body


def cache_decision(body: Dict[str, Any], mode: str, cache_control: str = "") -> str:
    if mode == "off":
        return "off"
    if body.get("stream"):
        return "skip:stream"
    if "no-cache" in cache_control or "no-store" in cache_control:
        return "skip:no-cache"
    if body.get("n", 1) != 1:
        return "skip:n"
    if mode == "deterministic" and body.get("temperature") != 0:
        return "skip:temperature"
    return "use"


def cache_key(route: str, body: Dict[str, Any], scope: str) -> str:
    material = {k: v for k, v in body.items() if k not in IGNORED_IN_CACHE_KEY}
    material["model"] = route
    canonical = json.dumps(material, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(f"{scope}\n{canonical}".encode()).hexdigest()


class ResponseCache:
    def __init__(self, ttl: float, max_entries: int, clock: Callable[[], float]):
        self.ttl = ttl
        self.max_entries = max_entries
        self.clock = clock
        self.entries: "OrderedDict[str, Tuple[float, Dict[str, Any]]]" = OrderedDict()
        self.hits = 0
        self.misses = 0

    def get(self, key: str) -> Optional[Dict[str, Any]]:
        found = self.entries.get(key)
        if found is None or found[0] <= self.clock():
            self.entries.pop(key, None)
            self.misses += 1
            return None
        self.entries.move_to_end(key)
        self.hits += 1
        return found[1]

    def put(self, key: str, value: Dict[str, Any]) -> None:
        self.entries[key] = (self.clock() + self.ttl, value)
        self.entries.move_to_end(key)
        while len(self.entries) > self.max_entries:
            self.entries.popitem(last=False)


def redact(text: str) -> Tuple[str, Dict[str, int]]:
    counts: Dict[str, int] = {}
    for label, pattern in REDACTIONS:
        text, found = pattern.subn(f"[{label}]", text)
        if found:
            counts[label] = found
    return text, counts


def prompt_record(body: Dict[str, Any], mode: str, limit: int = 2000) -> Dict[str, Any]:
    text = prompt_text(body)
    record: Dict[str, Any] = {"sha256": hashlib.sha256(text.encode()).hexdigest()[:16], "chars": len(text)}
    if mode == "redacted":
        clean, counts = redact(text)
        record["text"] = clean[:limit]
        record["redactions"] = counts
    elif mode == "full":
        record["text"] = text[:limit]
    return record


@dataclass
class Reply:
    status: int
    headers: Dict[str, str]
    stream: Any = None
    error: str = ""
    body: Optional[bytes] = None

    def read(self) -> bytes:
        if self.body is None:
            try:
                self.body = self.stream.read() if self.stream is not None else self.error.encode()
            except OSError as exc:
                self.body = f"upstream read error: {exc}".encode()
            self.close()
        return self.body

    def readline(self) -> bytes:
        return self.stream.readline() if self.stream is not None else b""

    def close(self) -> None:
        if self.stream is not None:
            try:
                self.stream.close()
            except OSError:
                pass
            self.stream = None


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect)
Transport = Callable[[str, bytes, Dict[str, str], float], Reply]


def http_transport(url: str, payload: bytes, headers: Dict[str, str], timeout: float) -> Reply:
    request = urllib.request.Request(url, data=payload, method="POST", headers=headers)
    try:
        response = OPENER.open(request, timeout=timeout)
        return Reply(response.status, {k.lower(): v for k, v in response.getheaders()}, response)
    except urllib.error.HTTPError as err:
        return Reply(err.code, {k.lower(): v for k, v in err.headers.items()}, err)
    except (urllib.error.URLError, OSError) as err:
        return Reply(UNREACHABLE, {}, None, f"upstream connect error: {getattr(err, 'reason', err)}")


@dataclass
class Attempt:
    deployment: str
    status: int
    elapsed_ms: float
    retry_after: Optional[float] = None
    error: str = ""


@dataclass
class CallResult:
    deployment: Optional[Deployment]
    reply: Optional[Reply]
    attempts: List[Attempt]
    injected_usage: bool = False

    @property
    def ok(self) -> bool:
        return self.reply is not None and 200 <= self.reply.status < 300


@dataclass
class StreamTally:
    usage: Optional[Tuple[int, int]] = None
    content_chars: int = 0
    events: int = 0
    done: bool = False
    first_token_at: Optional[float] = None
    finish_reason: str = ""
    client_gone: bool = False
    error: str = ""


def inspect_event(lines: List[bytes], tally: StreamTally, clock: Callable[[], float], drop_usage_only: bool) -> bool:
    data = b"\n".join(line[5:].strip() for line in lines if line.startswith(b"data:"))
    if not data:
        return True
    if data == b"[DONE]":
        tally.done = True
        return True
    try:
        chunk = json.loads(data)
    except ValueError:
        return True
    if not isinstance(chunk, dict):
        return True
    tally.events += 1
    found = usage_tokens(chunk.get("usage"))
    if found:
        tally.usage = found
    choices = chunk.get("choices") or []
    for item in choices:
        if not isinstance(item, dict):
            continue
        delta = item.get("delta") or {}
        text = text_of(delta.get("content")) + text_of(delta.get("reasoning_content")) + str(item.get("text") or "")
        if text:
            tally.content_chars += len(text)
            if tally.first_token_at is None:
                tally.first_token_at = clock()
        if item.get("finish_reason"):
            tally.finish_reason = str(item["finish_reason"])
    if isinstance(chunk.get("error"), dict):
        tally.error = str(chunk["error"].get("message", "error event"))
    return not (drop_usage_only and found and not choices)


def relay_stream(readline: Callable[[], bytes], write: Callable[[bytes], bool], tally: StreamTally, clock: Callable[[], float], drop_usage_only: bool) -> StreamTally:
    event: List[bytes] = []
    while True:
        try:
            line = readline()
        except OSError as exc:
            tally.error = f"upstream read error: {exc}"
            line = b""
        if not line:
            break
        event.append(line)
        if line.strip() == b"":
            if inspect_event(event, tally, clock, drop_usage_only) and not write(b"".join(event)):
                tally.client_gone = True
                return tally
            event = []
    if event and inspect_event(event, tally, clock, drop_usage_only) and not write(b"".join(event)):
        tally.client_gone = True
    return tally


def error_body(code: str, kind: str, message: str) -> bytes:
    return json.dumps({"error": {"message": message, "type": kind, "param": None, "code": code}}).encode()


def iso(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp, timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def format_cost(cost: Optional[float]) -> str:
    return "unknown" if cost is None else f"${cost:.8f}"


def access_line(record: Dict[str, Any]) -> str:
    attempts = ",".join(f"{a['deployment']}:{a['status'] or 'unreachable'}" for a in record.get("attempts") or []) or "-"
    return (
        f"tokengate {record.get('status')} key={record.get('key', '-')} model={record.get('route', '-')} -> {record.get('deployment', '-')}"
        f" in={record.get('prompt_tokens', 0)} out={record.get('completion_tokens', 0)} cost={format_cost(record['cost_usd']) if 'cost_usd' in record else '-'}"
        f" cache={record.get('cache', '-')} attempts={attempts} {record.get('latency_ms', 0):.0f}ms"
    )


class Gateway:
    def __init__(self, config: Config, transport: Transport = http_transport, clock: Callable[[], float] = time.monotonic, sleep: Callable[[float], None] = time.sleep, wall: Callable[[], float] = time.time, log_path: Optional[str] = None, access: Optional[Callable[[str], None]] = None):
        self.config = config
        self.transport = transport
        self.clock = clock
        self.sleep = sleep
        self.wall = wall
        self.log_path = log_path
        self.access = access
        self.limiter = Limiter(clock)
        self.cache = ResponseCache(config.cache_ttl, config.cache_max_entries, clock)
        self.cooldowns: Dict[str, float] = {}
        self.usage: Dict[str, Dict[str, Any]] = {}
        self.lock = threading.Lock()
        self.log_lock = threading.Lock()

    def authenticate(self, authorization: str) -> Optional[KeyPolicy]:
        scheme, _, token = (authorization or "").partition(" ")
        if scheme.lower() != "bearer" or not token.strip():
            return None
        return self.config.keys.get(fingerprint(token.strip()))

    def plan(self, route: str) -> Tuple[List[Deployment], float]:
        now = self.clock()
        ready: List[Deployment] = []
        waits: List[float] = []
        with self.lock:
            for deployment in self.config.routes[route]:
                until = self.cooldowns.get(deployment.label, 0.0)
                if until > now:
                    waits.append(until - now)
                else:
                    ready.append(deployment)
        return ready, min(waits) if waits else 0.0

    def cool_down(self, deployment: Deployment, seconds: float) -> None:
        with self.lock:
            self.cooldowns[deployment.label] = max(self.cooldowns.get(deployment.label, 0.0), self.clock() + seconds)

    def upstream_request(self, deployment: Deployment, path: str, body: Dict[str, Any], stream: bool) -> Tuple[str, bytes, Dict[str, str], bool]:
        backend = self.config.backends[deployment.backend]
        payload = dict(body, model=deployment.model)
        injected = False
        if stream:
            options = dict(payload.get("stream_options") or {})
            if not options.get("include_usage"):
                options["include_usage"] = True
                injected = True
            payload["stream_options"] = options
        headers = {"Content-Type": "application/json", "Accept": "text/event-stream" if stream else "application/json", "Accept-Encoding": "identity"}
        secret = os.environ.get(backend.api_key_env, "") if backend.api_key_env else ""
        if secret:
            headers["Authorization"] = f"Bearer {secret}"
        return backend.url + path, json.dumps(payload).encode(), headers, injected

    def call(self, plan: List[Deployment], path: str, body: Dict[str, Any], stream: bool) -> CallResult:
        attempts: List[Attempt] = []
        last: Optional[Reply] = None
        for deployment in plan:
            url, payload, headers, injected = self.upstream_request(deployment, path, body, stream)
            timeout = self.config.backends[deployment.backend].timeout
            for attempt in range(self.config.retries + 1):
                started = self.clock()
                reply = self.transport(url, payload, headers, timeout)
                wait = provider_wait(reply.headers, self.wall())
                attempts.append(Attempt(deployment.label, reply.status, round((self.clock() - started) * 1000, 1), wait, reply.error))
                if reply.status not in RETRYABLE:
                    return CallResult(deployment, reply, attempts, injected)
                reply.read()
                last = reply
                delay = wait if wait is not None else self.config.backoff_seconds * (2 ** attempt)
                if attempt < self.config.retries and delay <= self.config.max_retry_wait:
                    self.sleep(delay)
                    continue
                if reply.status == 429:
                    self.cool_down(deployment, wait if wait is not None else self.config.cooldown_seconds)
                break
        return CallResult(None, last, attempts)

    def completion(self, path: str, headers: Dict[str, str], raw: bytes, writer: Any) -> Dict[str, Any]:
        record: Dict[str, Any] = {"id": uuid.uuid4().hex[:16], "ts": iso(self.wall()), "path": path, "_started": self.clock()}
        policy = self.authenticate(headers.get("authorization", ""))
        if policy is None:
            return self.reject(writer, record, 401, "invalid_api_key", "invalid_request_error", "Incorrect or missing API key: send Authorization: Bearer <key>")
        record["key"] = policy.name
        try:
            body = parse_body(raw, path)
        except InputError as exc:
            return self.reject(writer, record, 400, "invalid_request", "invalid_request_error", str(exc))
        route = body["model"]
        stream = bool(body.get("stream"))
        record.update(route=route, stream=stream, prompt=prompt_record(body, self.config.log_prompts))
        if route not in self.config.routes:
            return self.reject(writer, record, 404, "model_not_found", "invalid_request_error", f"The model '{route}' does not exist on this gateway; GET /v1/models lists the routes")
        if not policy.allows(route):
            return self.reject(writer, record, 403, "model_not_allowed", "invalid_request_error", f"key {policy.name} may not use model '{route}'")
        estimate = estimate_tokens(body, self.config.default_max_tokens)
        record["estimate"] = estimate
        with self.lock:
            admission = self.limiter.admit(policy, estimate)
            limits = self.limiter.headers(policy)
        if not admission.allowed:
            record["limited_by"] = admission.limited_by
            if admission.limited_by == "too_large":
                return self.reject(writer, record, 400, "request_too_large", "invalid_request_error", admission.message, limits + [("x-tokengate-limited-by", "too_large")])
            extra = limits + [("retry-after", str(max(1, math.ceil(admission.retry_after)))), ("x-tokengate-limited-by", admission.limited_by)]
            return self.reject(writer, record, 429, "rate_limit_exceeded", admission.limited_by, admission.message, extra)
        decision = cache_decision(body, self.config.cache_mode, headers.get("cache-control", ""))
        record["cache"] = decision
        cache_id = cache_key(route, body, policy.name if self.config.cache_scope == "key" else "*") if decision == "use" else ""
        if cache_id:
            with self.lock:
                hit = self.cache.get(cache_id)
            if hit is not None:
                return self.serve_hit(writer, record, policy, estimate, hit)
            record["cache"] = "miss"
        plan, cooling = self.plan(route)
        if not plan:
            self.refund(policy, estimate)
            record["limited_by"] = "upstream"
            message = f"every deployment of '{route}' is cooling down after a 429 from upstream; try again in {format_duration(cooling)}"
            return self.reject(writer, record, 429, "upstream_cooldown", "requests", message, [("retry-after", str(max(1, math.ceil(cooling)))), ("x-tokengate-limited-by", "upstream")])
        result = self.call(plan, path, body, stream)
        record["attempts"] = [asdict(a) for a in result.attempts]
        if not result.ok:
            return self.upstream_failure(writer, record, policy, estimate, result)
        if stream:
            return self.relay(writer, record, policy, body, estimate, result)
        return self.forward(writer, record, policy, body, estimate, result, cache_id)

    def refund(self, policy: KeyPolicy, estimate: int) -> None:
        with self.lock:
            self.limiter.settle(policy, estimate, 0)

    def trace_headers(self, record: Dict[str, Any], deployment: str) -> List[Tuple[str, str]]:
        attempts = ",".join(f"{a['deployment']}:{a['status'] or 'unreachable'}" for a in record.get("attempts") or [])
        found = [("x-tokengate-request-id", record["id"]), ("x-tokengate-route", record["route"]), ("x-tokengate-deployment", deployment), ("x-tokengate-cache", record["cache"])]
        return found + ([("x-tokengate-attempts", attempts)] if attempts else [])

    def settle(self, record: Dict[str, Any], policy: KeyPolicy, estimate: int, deployment: Deployment, counted: Tuple[int, int], source: str) -> Tuple[Optional[float], List[Tuple[str, str]]]:
        price = self.config.prices.lookup(deployment)
        cost = price.cost(*counted) if price else None
        with self.lock:
            self.limiter.settle(policy, estimate, sum(counted))
            limits = self.limiter.headers(policy)
        record.update(deployment=deployment.label, upstream_model=deployment.model, prompt_tokens=counted[0], completion_tokens=counted[1], usage_source=source, cost_usd=cost)
        return cost, limits

    def forward(self, writer: Any, record: Dict[str, Any], policy: KeyPolicy, body: Dict[str, Any], estimate: int, result: CallResult, cache_id: str) -> Dict[str, Any]:
        reply, deployment = result.reply, result.deployment
        data = reply.read()
        try:
            parsed = json.loads(data)
        except ValueError:
            parsed = None
        counted = usage_tokens(parsed.get("usage")) if isinstance(parsed, dict) else None
        source = "reported"
        if counted is None:
            counted, source = (estimate_prompt_tokens(body), estimate_text_tokens(len(response_text(parsed)))), "estimated"
        cost, limits = self.settle(record, policy, estimate, deployment, counted, source)
        content_type = reply.headers.get("content-type", "application/json")
        if cache_id and isinstance(parsed, dict) and reply.status == 200:
            with self.lock:
                self.cache.put(cache_id, {"body": data, "content_type": content_type, "usage": counted, "cost": cost, "deployment": deployment.label})
        out = [("Content-Type", content_type)] + self.trace_headers(record, deployment.label) + limits
        if cost is not None:
            out.append(("x-tokengate-cost-usd", f"{cost:.10f}"))
        return self.send(writer, record, reply.status, out, data)

    def relay(self, writer: Any, record: Dict[str, Any], policy: KeyPolicy, body: Dict[str, Any], estimate: int, result: CallResult) -> Dict[str, Any]:
        reply, deployment = result.reply, result.deployment
        with self.lock:
            limits = self.limiter.headers(policy)
        content_type = reply.headers.get("content-type", "text/event-stream")
        writer.start_stream(reply.status, [("Content-Type", content_type), ("Cache-Control", "no-cache")] + self.trace_headers(record, deployment.label) + limits)
        tally = relay_stream(reply.readline, writer.write_chunk, StreamTally(), self.clock, result.injected_usage)
        reply.close()
        if tally.usage:
            counted, source = tally.usage, "reported"
        else:
            counted, source = (estimate_prompt_tokens(body), estimate_text_tokens(tally.content_chars)), "estimated"
        self.settle(record, policy, estimate, deployment, counted, source)
        record.update(status=reply.status, stream_done=tally.done, stream_events=tally.events, finish_reason=tally.finish_reason)
        if tally.first_token_at is not None:
            record["ttft_ms"] = round((tally.first_token_at - record["_started"]) * 1000, 1)
        if tally.client_gone:
            record["client_gone"] = True
        if tally.error:
            record["stream_error"] = tally.error
        return self.finish(record)

    def serve_hit(self, writer: Any, record: Dict[str, Any], policy: KeyPolicy, estimate: int, hit: Dict[str, Any]) -> Dict[str, Any]:
        with self.lock:
            self.limiter.settle(policy, estimate, 0)
            limits = self.limiter.headers(policy)
        record.update(cache="hit", deployment=hit["deployment"], prompt_tokens=0, completion_tokens=0, cached_tokens=sum(hit["usage"]), usage_source="cache", cost_usd=0.0, saved_usd=hit["cost"])
        return self.send(writer, record, 200, [("Content-Type", hit["content_type"])] + self.trace_headers(record, hit["deployment"]) + limits + [("x-tokengate-cost-usd", "0")], hit["body"])

    def upstream_failure(self, writer: Any, record: Dict[str, Any], policy: KeyPolicy, estimate: int, result: CallResult) -> Dict[str, Any]:
        self.refund(policy, estimate)
        reply = result.reply
        status = reply.status if reply is not None else UNREACHABLE
        if result.deployment is not None and reply is not None:
            record["deployment"] = result.deployment.label
            out = [("Content-Type", reply.headers.get("content-type", "application/json"))] + self.trace_headers(record, result.deployment.label)
            record["error"] = f"upstream_{status}"
            return self.send(writer, record, status, out, reply.read())
        tried = ", ".join(f"{a.deployment} {a.status or 'unreachable'}" for a in result.attempts)
        extra = self.trace_headers(record, "-")
        if status == 429:
            record["limited_by"] = "upstream"
            waits = [a.retry_after for a in result.attempts if a.retry_after is not None]
            extra += [("retry-after", str(max(1, math.ceil(min(waits) if waits else self.config.cooldown_seconds)))), ("x-tokengate-limited-by", "upstream")]
            return self.reject(writer, record, 429, "upstream_rate_limited", "requests", f"every deployment of '{record['route']}' is rate limited upstream ({tried})", extra)
        return self.reject(writer, record, 502, "upstream_error", "api_error", f"no deployment of '{record['route']}' answered ({tried})", extra)

    def reject(self, writer: Any, record: Dict[str, Any], status: int, code: str, kind: str, message: str, extra: Sequence[Tuple[str, str]] = ()) -> Dict[str, Any]:
        headers = [("Content-Type", "application/json")]
        if not any(name == "x-tokengate-request-id" for name, _ in extra):
            headers.append(("x-tokengate-request-id", record["id"]))
        record.update(error=code, message=message)
        return self.send(writer, record, status, headers + list(extra), error_body(code, kind, message))

    def send(self, writer: Any, record: Dict[str, Any], status: int, headers: Sequence[Tuple[str, str]], body: bytes) -> Dict[str, Any]:
        record["status"] = status
        self.finish(record)
        writer.respond(status, headers, body)
        return record

    def finish(self, record: Dict[str, Any]) -> Dict[str, Any]:
        record["latency_ms"] = round((self.clock() - record.pop("_started")) * 1000, 1)
        with self.lock:
            self.account(record)
        if self.log_path:
            with self.log_lock, open(self.log_path, "a", encoding="utf-8") as handle:
                handle.write(json.dumps(record, sort_keys=True) + "\n")
        if self.access:
            self.access(access_line(record))
        return record

    def account(self, record: Dict[str, Any]) -> None:
        if "key" not in record:
            return
        usage = self.usage.setdefault(record["key"], {"requests": 0, "ok": 0, "limited": 0, "errors": 0, "cache_hits": 0, "streams": 0, "prompt_tokens": 0, "completion_tokens": 0, "cost_usd": 0.0, "by_route": {}})
        usage["requests"] += 1
        status = record.get("status", 0)
        if 200 <= status < 300:
            usage["ok"] += 1
        elif status == 429:
            usage["limited"] += 1
        else:
            usage["errors"] += 1
        usage["cache_hits"] += 1 if record.get("cache") == "hit" else 0
        usage["streams"] += 1 if record.get("stream") and 200 <= status < 300 else 0
        usage["prompt_tokens"] += record.get("prompt_tokens", 0)
        usage["completion_tokens"] += record.get("completion_tokens", 0)
        usage["cost_usd"] = round(usage["cost_usd"] + (record.get("cost_usd") or 0.0), 10)
        if record.get("route"):
            route = usage["by_route"].setdefault(record["route"], {"requests": 0, "prompt_tokens": 0, "completion_tokens": 0})
            route["requests"] += 1
            route["prompt_tokens"] += record.get("prompt_tokens", 0)
            route["completion_tokens"] += record.get("completion_tokens", 0)

    def models(self, policy: KeyPolicy) -> Dict[str, Any]:
        return {"object": "list", "data": [{"id": route, "object": "model", "owned_by": "tokengate"} for route in sorted(self.config.routes) if policy.allows(route)]}

    def usage_report(self, policy: KeyPolicy) -> Dict[str, Any]:
        with self.lock:
            usage = json.loads(json.dumps(self.usage.get(policy.name) or {}))
            limits = dict(self.limiter.headers(policy))
            cooling = {label: round(until - self.clock(), 1) for label, until in self.cooldowns.items() if until > self.clock()}
        return {"key": policy.name, "usage": usage, "limits": limits, "cooling_down": cooling, "cache": {"entries": len(self.cache.entries), "hits": self.cache.hits, "misses": self.cache.misses}}


class GatewayServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address: Tuple[str, int], gateway: Gateway):
        super().__init__(address, GatewayHandler)
        self.gateway = gateway


class GatewayHandler(BaseHTTPRequestHandler):
    server: GatewayServer

    def respond(self, status: int, headers: Sequence[Tuple[str, str]], body: bytes) -> None:
        self.send_response(status)
        for name, value in headers:
            if name.lower() not in HOP_BY_HOP:
                self.send_header(name, value)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def start_stream(self, status: int, headers: Sequence[Tuple[str, str]]) -> None:
        self.send_response(status)
        for name, value in headers:
            if name.lower() not in HOP_BY_HOP:
                self.send_header(name, value)
        self.end_headers()
        self.wfile.flush()

    def write_chunk(self, chunk: bytes) -> bool:
        try:
            self.wfile.write(chunk)
            self.wfile.flush()
            return True
        except (BrokenPipeError, ConnectionResetError):
            return False

    def do_GET(self) -> None:
        path = self.path.split("?")[0]
        gateway = self.server.gateway
        if path in ("/health", "/healthz"):
            return self.respond(200, [("Content-Type", "application/json")], b'{"status": "ok"}')
        policy = gateway.authenticate(self.headers.get("Authorization", ""))
        if policy is None:
            return self.respond(401, [("Content-Type", "application/json")], error_body("invalid_api_key", "invalid_request_error", "Incorrect or missing API key"))
        if path == "/v1/models":
            return self.respond(200, [("Content-Type", "application/json")], json.dumps(gateway.models(policy)).encode())
        if path == "/tokengate/usage":
            return self.respond(200, [("Content-Type", "application/json")], json.dumps(gateway.usage_report(policy)).encode())
        self.respond(404, [("Content-Type", "application/json")], error_body("not_found", "invalid_request_error", f"no route for GET {path}"))

    def do_POST(self) -> None:
        path = self.path.split("?")[0]
        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_BODY_BYTES:
            return self.respond(413, [("Content-Type", "application/json")], error_body("request_too_large", "invalid_request_error", f"body of {length} bytes is over {MAX_BODY_BYTES}"))
        raw = self.rfile.read(length) if length else b""
        if path not in ENDPOINTS:
            return self.respond(404, [("Content-Type", "application/json")], error_body("not_found", "invalid_request_error", f"no route for POST {path}; tokengate serves {', '.join(ENDPOINTS)}"))
        self.server.gateway.completion(path, {k.lower(): v for k, v in self.headers.items()}, raw, self)

    def log_message(self, format: str, *args: Any) -> None:
        return


def fake_prompt_tokens(body: Dict[str, Any]) -> int:
    if "messages" in body:
        return sum(len(text_of(m.get("content")).split()) + 1 for m in body.get("messages") or [] if isinstance(m, dict))
    return len(prompt_text(body).split())


def fake_words(name: str, body: Dict[str, Any]) -> List[str]:
    count = min(requested_output_tokens(body, 8), 16)
    return ([f"[{name}]"] + [f"w{i}" for i in range(1, count)])[:count]


class FakeServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address: Tuple[str, int], name: str, models: Sequence[str], delay: float = 0.0, report_usage: bool = True):
        super().__init__(address, FakeHandler)
        self.name = name
        self.served_models = list(models)
        self.delay = delay
        self.report_usage = report_usage
        self.lock = threading.Lock()
        self.failures: List[Dict[str, Any]] = []
        self.stats: Dict[str, Any] = {}
        self.reset()

    def reset(self) -> None:
        with self.lock:
            self.failures = []
            self.stats = {"requests": 0, "completions": 0, "failed": 0, "streams": 0, "models": {}, "last_request": None, "last_authorization": ""}

    def next_failure(self, body: Dict[str, Any], authorization: str = "") -> Optional[Dict[str, Any]]:
        with self.lock:
            self.stats["requests"] += 1
            self.stats["last_request"] = body
            self.stats["last_authorization"] = authorization
            model = str(body.get("model"))
            self.stats["models"][model] = self.stats["models"].get(model, 0) + 1
            if not self.failures:
                self.stats["completions"] += 1
                return None
            failure = self.failures[0]
            failure["times"] -= 1
            if failure["times"] <= 0:
                self.failures.pop(0)
            self.stats["failed"] += 1
            return dict(failure)


class FakeHandler(BaseHTTPRequestHandler):
    server: FakeServer

    def reply_json(self, status: int, data: Any, headers: Sequence[Tuple[str, str]] = ()) -> None:
        body = json.dumps(data).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        for name, value in headers:
            self.send_header(name, value)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        path = self.path.split("?")[0]
        if path == "/v1/models":
            return self.reply_json(200, {"object": "list", "data": [{"id": m, "object": "model", "owned_by": self.server.name} for m in self.server.served_models]})
        if path == "/fake/stats":
            with self.server.lock:
                return self.reply_json(200, json.loads(json.dumps(self.server.stats)))
        if path == "/health":
            return self.reply_json(200, {"status": "ok"})
        self.reply_json(404, {"error": {"message": f"no route for GET {path}", "type": "fake", "code": "404"}})

    def do_POST(self) -> None:
        path = self.path.split("?")[0]
        raw = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        try:
            body = json.loads(raw.decode() or "{}")
        except ValueError:
            return self.reply_json(400, {"error": {"message": "body is not JSON", "type": "invalid_request_error", "code": "400"}})
        if path == "/fake/fail":
            failure = {"status": int(body.get("status", 429)), "times": int(body.get("times", 1)), "retry_after": body.get("retry_after")}
            with self.server.lock:
                self.server.failures.append(failure)
            return self.reply_json(200, {"queued": failure})
        if path == "/fake/reset":
            self.server.reset()
            return self.reply_json(200, {"reset": True})
        if path not in ENDPOINTS:
            return self.reply_json(404, {"error": {"message": f"no route for POST {path}", "type": "fake", "code": "404"}})
        failure = self.server.next_failure(body, self.headers.get("Authorization", ""))
        if failure is not None:
            headers = [("retry-after", str(failure["retry_after"]))] if failure["retry_after"] is not None else []
            return self.reply_json(failure["status"], {"error": {"message": f"{self.server.name}: injected {failure['status']}", "type": "fake", "code": str(failure["status"])}}, headers)
        self.complete(path, body)

    def complete(self, path: str, body: Dict[str, Any]) -> None:
        name = self.server.name
        words = fake_words(name, body)
        usage = {"prompt_tokens": fake_prompt_tokens(body), "completion_tokens": len(words), "total_tokens": fake_prompt_tokens(body) + len(words)}
        finish = "length" if len(words) == requested_output_tokens(body, 8) else "stop"
        chat = path.endswith("/chat/completions")
        ident = f"cmpl-{name}-{uuid.uuid4().hex[:8]}"
        if not body.get("stream"):
            text = " ".join(words)
            item = {"index": 0, "message": {"role": "assistant", "content": text}, "finish_reason": finish} if chat else {"index": 0, "text": text, "finish_reason": finish}
            data = {"id": ident, "object": "chat.completion" if chat else "text_completion", "created": int(time.time()), "model": body.get("model"), "choices": [item]}
            if self.server.report_usage:
                data["usage"] = usage
            return self.reply_json(200, data)
        with self.server.lock:
            self.server.stats["streams"] += 1
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        kind = "chat.completion.chunk" if chat else "text_completion"

        def event(choices: List[Dict[str, Any]], extra: Optional[Dict[str, Any]] = None) -> None:
            chunk = dict({"id": ident, "object": kind, "created": int(time.time()), "model": body.get("model"), "choices": choices}, **(extra or {}))
            self.wfile.write(f"data: {json.dumps(chunk)}\n\n".encode())
            self.wfile.flush()

        try:
            for index, word in enumerate(words):
                if self.server.delay:
                    time.sleep(self.server.delay)
                piece = word if index == len(words) - 1 else word + " "
                event([{"index": 0, "delta": {"content": piece}, "finish_reason": None} if chat else {"index": 0, "text": piece, "finish_reason": None}])
            event([{"index": 0, "delta": {}, "finish_reason": finish} if chat else {"index": 0, "text": "", "finish_reason": finish}])
            if self.server.report_usage and (body.get("stream_options") or {}).get("include_usage"):
                event([], {"usage": usage})
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            return

    def log_message(self, format: str, *args: Any) -> None:
        return


def parse_pairs(values: Sequence[str], flag: str) -> Dict[str, str]:
    pairs: Dict[str, str] = {}
    for value in values:
        key, sep, rest = value.partition("=")
        if not sep or not key or not rest:
            raise InputError(f"{flag} {value!r}: expected NAME=URL")
        pairs[key] = rest
    return pairs


def find_policy(config: Config, ref: str) -> KeyPolicy:
    for policy in config.keys.values():
        if ref in (policy.name, policy.digest) or fingerprint(ref) == policy.digest:
            return policy
    raise InputError(f"--key {ref!r}: no key with that name or secret (names: {', '.join(sorted(p.name for p in config.keys.values()))})")


def plan_request(config: Config, policy: KeyPolicy, body: Dict[str, Any], path: str = "/v1/chat/completions") -> Dict[str, Any]:
    body = parse_body(json.dumps(body).encode(), path)
    route = body["model"]
    if route not in config.routes:
        raise InputError(f"model {route!r} is not a route (routes: {', '.join(sorted(config.routes))})")
    deployments = config.routes[route]
    prompt, output = estimate_prompt_tokens(body), requested_output_tokens(body, config.default_max_tokens)
    total = estimate_tokens(body, config.default_max_tokens)
    costs = [(d.label, config.prices.lookup(d)) for d in deployments]
    priced = [(label, price.cost(prompt, total - prompt)) for label, price in costs if price is not None]
    decision = cache_decision(body, config.cache_mode)
    return {
        "key": policy.name,
        "route": route,
        "allowed": policy.allows(route),
        "deployments": [d.label for d in deployments],
        "estimate": {"prompt": prompt, "output": output, "total": total},
        "tpm": policy.tpm,
        "rpm": policy.rpm,
        "fits": policy.tpm is None or total <= policy.tpm,
        "cache": decision,
        "cache_key": cache_key(route, body, policy.name if config.cache_scope == "key" else "*")[:16] if decision == "use" else "",
        "max_cost_usd": max((cost for _, cost in priced), default=None),
        "unpriced": [label for label, price in costs if price is None],
        "logged_prompt": prompt_record(body, config.log_prompts, 200),
    }


def render_plan(report: Dict[str, Any]) -> str:
    estimate = report["estimate"]
    lines = [
        f"key {report['key']}, model {report['route']}{'' if report['allowed'] else ' (NOT allowed for this key: 403)'}",
        f"deployments in order: {', '.join(report['deployments'])}",
        f"estimate: prompt ~{estimate['prompt']} + output {estimate['output']} = {estimate['total']} tokens charged up front, settled from usage",
        f"tokens per minute: {report['tpm'] or 'unlimited'}; {'fits' if report['fits'] else 'never fits: 400 request_too_large'}",
        f"cache: {report['cache']}{' key ' + report['cache_key'] if report['cache_key'] else ''}",
        f"worst-case cost at the estimate: {format_cost(report['max_cost_usd'])}" + (f" (no price for {', '.join(report['unpriced'])})" if report["unpriced"] else ""),
        f"logged prompt: {json.dumps(report['logged_prompt'].get('text', '<hash only>'))} sha256 {report['logged_prompt']['sha256']}",
    ]
    return "\n".join(lines)


def check_report(config: Config) -> Dict[str, Any]:
    return {
        "routes": {route: [{"deployment": d.label, "url": config.backends[d.backend].url, "price": asdict(config.prices.lookup(d)) if config.prices.lookup(d) else None} for d in deployments] for route, deployments in sorted(config.routes.items())},
        "keys": [{"name": p.name, "rpm": p.rpm, "tpm": p.tpm, "models": p.models or ["*"]} for p in sorted(config.keys.values(), key=lambda p: p.name)],
        "prices": {"date": config.prices.date, "source": config.prices.source, "currency": config.prices.currency, "entries": len(config.prices.prices)},
        "retry": {"retries": config.retries, "backoff_seconds": config.backoff_seconds, "max_wait_seconds": config.max_retry_wait, "cooldown_seconds": config.cooldown_seconds},
        "cache": {"mode": config.cache_mode, "ttl_seconds": config.cache_ttl, "max_entries": config.cache_max_entries, "scope": config.cache_scope},
        "log_prompts": config.log_prompts,
        "warnings": config.warnings,
    }


def render_check(report: Dict[str, Any]) -> str:
    lines = [f"routes ({len(report['routes'])}):"]
    for route, deployments in report["routes"].items():
        chain = " then ".join(d["deployment"] for d in deployments)
        lines.append(f"  {route:<10} {chain}")
    lines.append(f"keys ({len(report['keys'])}):")
    for key in report["keys"]:
        lines.append(f"  {key['name']:<10} rpm={key['rpm'] or '-'} tpm={key['tpm'] or '-'} models={','.join(key['models'])}")
    prices, retry, cache = report["prices"], report["retry"], report["cache"]
    lines.append(f"prices: {prices['entries']} entries in {prices['currency']}, dated {prices['date'] or '-'}, source: {prices['source'] or '-'}")
    lines.append(f"retry: {retry['retries']} per deployment, backoff {retry['backoff_seconds']}s, wait at most {retry['max_wait_seconds']}s, cooldown after 429 {retry['cooldown_seconds']}s")
    lines.append(f"cache: {cache['mode']}, ttl {cache['ttl_seconds']}s, {cache['max_entries']} entries, scope {cache['scope']}; prompts logged as {report['log_prompts']}")
    lines.extend(f"warning: {w}" for w in report["warnings"])
    return "\n".join(lines)


def percentile(values: Sequence[float], share: float) -> float:
    ordered = sorted(values)
    if not ordered:
        return 0.0
    return ordered[max(0, math.ceil(share * len(ordered)) - 1)]


def summarise_logs(records: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    groups: Dict[Tuple[str, str], List[Dict[str, Any]]] = {}
    for record in records:
        groups.setdefault((record.get("key", "-"), record.get("route", "-")), []).append(record)
    rows = []
    for (key, route), items in sorted(groups.items()):
        latencies = [r.get("latency_ms", 0.0) for r in items if 200 <= r.get("status", 0) < 300 and r.get("cache") != "hit"]
        rows.append({
            "key": key,
            "route": route,
            "requests": len(items),
            "ok": sum(1 for r in items if 200 <= r.get("status", 0) < 300),
            "limited": sum(1 for r in items if r.get("status") == 429),
            "errors": sum(1 for r in items if r.get("status", 0) >= 400 and r.get("status") != 429),
            "cache_hits": sum(1 for r in items if r.get("cache") == "hit"),
            "fallbacks": sum(1 for r in items if len({a["deployment"] for a in r.get("attempts") or []}) > 1),
            "prompt_tokens": sum(r.get("prompt_tokens", 0) for r in items),
            "completion_tokens": sum(r.get("completion_tokens", 0) for r in items),
            "cost_usd": round(sum(r.get("cost_usd") or 0.0 for r in items), 10),
            "saved_usd": round(sum(r.get("saved_usd") or 0.0 for r in items), 10),
            "p50_ms": percentile(latencies, 0.5),
            "p95_ms": percentile(latencies, 0.95),
        })
    return rows


def read_logs(paths: Sequence[str]) -> List[Dict[str, Any]]:
    records = []
    for path in paths:
        try:
            with open(path, encoding="utf-8") as handle:
                for number, line in enumerate(handle, 1):
                    if line.strip():
                        try:
                            records.append(json.loads(line))
                        except ValueError as exc:
                            raise InputError(f"{path}:{number}: not a JSON line ({exc})")
        except OSError as exc:
            raise InputError(f"{path}: {exc.strerror}")
    return records


def render_report(rows: Sequence[Dict[str, Any]]) -> str:
    header = f"{'key':<10} {'model':<8} {'req':>4} {'ok':>4} {'429':>4} {'err':>4} {'hit':>4} {'fb':>3} {'in':>7} {'out':>7} {'cost':>13} {'p50':>7} {'p95':>7}"
    lines = [header]
    for r in rows:
        lines.append(f"{r['key']:<10} {r['route']:<8} {r['requests']:>4} {r['ok']:>4} {r['limited']:>4} {r['errors']:>4} {r['cache_hits']:>4} {r['fallbacks']:>3} {r['prompt_tokens']:>7} {r['completion_tokens']:>7} {format_cost(r['cost_usd']):>13} {r['p50_ms']:>6.0f}ms {r['p95_ms']:>5.0f}ms")
    return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="tokengate", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    serve = sub.add_parser("serve", help="run the gateway")
    serve.add_argument("--config", required=True, help="gateway JSON: backends, models, keys, prices, retry, cache, log")
    serve.add_argument("--backend", action="append", default=[], help="NAME=URL, replaces the url of a backend in the config; repeatable")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=4000, help="0 picks a free port")
    serve.add_argument("--log", help="append one JSON line per request to this file")
    serve.add_argument("--quiet", action="store_true", help="no access line per request on stderr")
    check = sub.add_parser("check", help="validate a config and print routes, keys, prices and policies")
    check.add_argument("--config", required=True)
    check.add_argument("--backend", action="append", default=[])
    check.add_argument("--json", action="store_true")
    plan = sub.add_parser("plan", help="what the gateway would do with one request, without sending it")
    plan.add_argument("--config", required=True)
    plan.add_argument("--key", required=True, help="key name or secret")
    plan.add_argument("request", help="request body JSON file, or - for stdin")
    plan.add_argument("--path", default="/v1/chat/completions", choices=ENDPOINTS)
    plan.add_argument("--json", action="store_true")
    report = sub.add_parser("report", help="summarise --log files per key and model")
    report.add_argument("logs", nargs="+")
    report.add_argument("--json", action="store_true")
    fake = sub.add_parser("fake", help="a tiny OpenAI-compatible backend that can be told to fail")
    fake.add_argument("--name", default="fake")
    fake.add_argument("--host", default="127.0.0.1")
    fake.add_argument("--port", type=int, default=0)
    fake.add_argument("--model", action="append", default=[], help="model id for /v1/models; repeatable")
    fake.add_argument("--delay-ms", type=float, default=0.0, help="pause before each streamed token")
    fake.add_argument("--no-usage", action="store_true", help="omit usage from responses, like a server that does not report it")
    return parser


def serve_forever(server: ThreadingHTTPServer) -> int:
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(list(sys.argv[1:] if argv is None else argv))
    try:
        if args.command == "fake":
            fake = FakeServer((args.host, args.port), args.name, args.model or [args.name], args.delay_ms / 1000, not args.no_usage)
            print(f"fake {args.name} listening on http://{args.host}:{fake.server_address[1]}", flush=True)
            return serve_forever(fake)
        if args.command == "report":
            rows = summarise_logs(read_logs(args.logs))
            print(json.dumps(rows, indent=2) if args.json else render_report(rows))
            return 0
        if args.command == "plan":
            config = load_config_file(args.config)
            raw = sys.stdin.read() if args.request == "-" else open(args.request, encoding="utf-8").read()
            try:
                body = json.loads(raw)
            except ValueError as exc:
                raise InputError(f"{args.request}: not JSON ({exc})")
            report = plan_request(config, find_policy(config, args.key), body, args.path)
            print(json.dumps(report, indent=2) if args.json else render_plan(report))
            return 0
        config = load_config_file(args.config, parse_pairs(args.backend, "--backend"))
        if args.command == "check":
            report = check_report(config)
            print(json.dumps(report, indent=2) if args.json else render_check(report))
            return 0
        access = None if args.quiet else (lambda line: print(line, file=sys.stderr, flush=True))
        gateway = Gateway(config, log_path=args.log, access=access)
        server = GatewayServer((args.host, args.port), gateway)
        for warning in config.warnings:
            print(f"warning: {warning}", file=sys.stderr, flush=True)
        print(f"tokengate listening url=http://{args.host}:{server.server_address[1]} models={','.join(sorted(config.routes))}", flush=True)
        return serve_forever(server)
    except InputError as exc:
        print(f"tokengate: {exc}", file=sys.stderr)
        return 2
    except OSError as exc:
        print(f"tokengate: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
