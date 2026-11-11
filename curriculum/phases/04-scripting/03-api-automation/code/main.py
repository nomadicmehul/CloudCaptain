from __future__ import annotations

import argparse
import base64
import hashlib
import http
import http.client
import json
import math
import random
import re
import socket
import ssl
import struct
import sys
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import timezone
from email.utils import parsedate_to_datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, Dict, Iterator, List, Optional, Sequence, Tuple
from urllib.parse import parse_qs, parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

IDEMPOTENT_METHODS = frozenset({"GET", "HEAD", "OPTIONS", "PUT", "DELETE", "TRACE"})
RETRY_STATUSES = frozenset({408, 429, 500, 502, 503, 504})
IDEMPOTENCY_HEADER = "Idempotency-Key"
EPOCH_THRESHOLD = 1_000_000_000
NEVER_RETRIED_ERRORS = frozenset({"tls", "url"})
STEP_KEYS = frozenset({"status", "headers", "delay", "reset", "times", "reset_in", "body"})

LogFn = Callable[[Dict[str, Any]], None]


class APIError(Exception):
    pass


class TransportError(APIError):
    def __init__(self, kind: str, detail: str):
        super().__init__(f"{kind} error: {detail}")
        self.kind = kind
        self.detail = detail
        self.attempts = 1


class CircuitOpenError(APIError):
    def __init__(self, retry_in: float):
        super().__init__(f"circuit open, next trial allowed in {max(0.0, retry_in):.1f}s")
        self.retry_in = retry_in


class RateLimitedError(APIError):
    def __init__(self, wait: float):
        super().__init__(f"rate limit exhausted for {wait:.0f}s, more than max_wait")
        self.wait = wait


class HTTPStatusError(APIError):
    def __init__(self, response: "Response"):
        super().__init__(f"HTTP {response.status} from {response.url} after {response.attempts} attempt(s)")
        self.response = response


class PaginationError(APIError):
    pass


@dataclass
class Response:
    status: int
    headers: Dict[str, str]
    body: bytes
    url: str
    attempts: int = 1
    from_cache: bool = False

    def header(self, name: str, default: Optional[str] = None) -> Optional[str]:
        return self.headers.get(name.lower(), default)

    def json(self) -> Any:
        return json.loads(self.body.decode("utf-8")) if self.body else None

    @property
    def text(self) -> str:
        return self.body.decode("utf-8", errors="replace")


@dataclass
class Timeouts:
    connect: float = 3.05
    read: float = 30.0


@dataclass
class RetryPolicy:
    retries: int = 3
    backoff_base: float = 0.5
    backoff_cap: float = 30.0
    max_wait: float = 60.0
    deadline: float = 120.0
    statuses: frozenset = RETRY_STATUSES

    def ceiling(self, retry_number: int) -> float:
        return min(self.backoff_cap, self.backoff_base * (2 ** retry_number))


@dataclass
class Outcome:
    status: Optional[int] = None
    headers: Dict[str, str] = field(default_factory=dict)
    error: Optional[str] = None


@dataclass
class Decision:
    retry: bool
    delay: float
    reason: str


def merge_headers(pairs: Sequence[Tuple[str, str]]) -> Dict[str, str]:
    merged: Dict[str, str] = {}
    for name, value in pairs:
        key = name.lower()
        merged[key] = f"{merged[key]}, {value}" if key in merged else value
    return merged


def describe(err: BaseException) -> str:
    text = str(err) or repr(err)
    return f"{type(err).__name__}: {text}"


def http_transport(method: str, url: str, headers: Dict[str, str], body: Optional[bytes], timeouts: Timeouts) -> Response:
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise TransportError("url", f"unsupported URL {url!r}")
    connection_class = http.client.HTTPSConnection if parts.scheme == "https" else http.client.HTTPConnection
    connection = connection_class(parts.hostname, parts.port, timeout=timeouts.connect)
    target = (parts.path or "/") + (f"?{parts.query}" if parts.query else "")
    try:
        try:
            connection.connect()
        except ssl.SSLError as err:
            raise TransportError("tls", describe(err)) from None
        except OSError as err:
            raise TransportError("connect", describe(err)) from None
        connection.sock.settimeout(timeouts.read)
        try:
            connection.request(method, target, body=body, headers=headers)
            reply = connection.getresponse()
            data = reply.read()
        except socket.timeout:
            raise TransportError("timeout", f"no response within the {timeouts.read:g}s read timeout") from None
        except (OSError, http.client.HTTPException) as err:
            raise TransportError("read", describe(err)) from None
        return Response(reply.status, merge_headers(reply.getheaders()), data, url)
    finally:
        connection.close()


def full_jitter(policy: RetryPolicy, retry_number: int, rng: random.Random) -> float:
    return rng.random() * policy.ceiling(retry_number)


def server_wait(headers: Dict[str, str], now: float) -> Optional[Tuple[float, str]]:
    retry_after = (headers.get("retry-after") or "").strip()
    if retry_after.isdigit():
        return float(retry_after), "retry-after"
    if retry_after:
        try:
            when = parsedate_to_datetime(retry_after)
        except (TypeError, ValueError, IndexError):
            when = None
        if when is not None:
            if when.tzinfo is None:
                when = when.replace(tzinfo=timezone.utc)
            return max(0.0, when.timestamp() - now), "retry-after"
    remaining = (headers.get("x-ratelimit-remaining") or "").strip()
    reset = (headers.get("x-ratelimit-reset") or "").strip()
    if remaining == "0" and reset.isdigit():
        value = int(reset)
        wait = value - now if value >= EPOCH_THRESHOLD else value
        return max(0.0, float(wait)), "x-ratelimit-reset"
    return None


def retry_safe(method: str, has_key: bool) -> bool:
    return method.upper() in IDEMPOTENT_METHODS or has_key


def decide(policy: RetryPolicy, method: str, has_key: bool, attempt: int, outcome: Outcome, now: float, rng: random.Random) -> Decision:
    if outcome.error is None and outcome.status not in policy.statuses:
        return Decision(False, 0.0, "done")
    if outcome.error in NEVER_RETRIED_ERRORS:
        return Decision(False, 0.0, f"{outcome.error} errors are not retried")
    if outcome.error != "connect" and not retry_safe(method, has_key):
        return Decision(False, 0.0, f"{method.upper()} without {IDEMPOTENCY_HEADER} is not retried")
    if attempt > policy.retries:
        return Decision(False, 0.0, f"retries exhausted ({policy.retries})")
    hint = server_wait(outcome.headers, now) if outcome.error is None else None
    if hint is not None:
        wait, source = hint
        if wait > policy.max_wait:
            return Decision(False, wait, f"{source} asks for {wait:.1f}s, more than max_wait {policy.max_wait:g}s")
        return Decision(True, wait, source)
    return Decision(True, full_jitter(policy, attempt - 1, rng), "backoff")


class TokenBucket:
    def __init__(self, rate: float, burst: int, clock: Callable[[], float] = time.monotonic):
        if rate <= 0 or burst < 1:
            raise ValueError("token bucket needs rate > 0 and burst >= 1")
        self.rate = rate
        self.capacity = float(burst)
        self.tokens = float(burst)
        self.clock = clock
        self.updated = clock()

    def reserve(self, amount: float = 1.0) -> float:
        now = self.clock()
        self.tokens = min(self.capacity, self.tokens + (now - self.updated) * self.rate)
        self.updated = now
        self.tokens -= amount
        return 0.0 if self.tokens >= 0 else -self.tokens / self.rate


class CircuitBreaker:
    def __init__(self, failures: int = 5, reset_after: float = 30.0, clock: Callable[[], float] = time.monotonic):
        if failures < 1 or reset_after <= 0:
            raise ValueError("circuit breaker needs failures >= 1 and reset_after > 0")
        self.threshold = failures
        self.reset_after = reset_after
        self.clock = clock
        self.state = "closed"
        self.failures = 0
        self.opened_at = 0.0
        self.trial_in_flight = False
        self.lock = threading.Lock()

    def current(self) -> str:
        with self.lock:
            return self._current()

    def _current(self) -> str:
        if self.state == "open" and self.clock() - self.opened_at >= self.reset_after:
            self.state = "half-open"
            self.trial_in_flight = False
        return self.state

    def allow(self) -> None:
        with self.lock:
            state = self._current()
            if state == "open":
                raise CircuitOpenError(self.reset_after - (self.clock() - self.opened_at))
            if state == "half-open":
                if self.trial_in_flight:
                    raise CircuitOpenError(0.0)
                self.trial_in_flight = True

    def record(self, failed: bool) -> None:
        with self.lock:
            self.trial_in_flight = False
            if not failed:
                self.state = "closed"
                self.failures = 0
                return
            self.failures += 1
            if self.state == "half-open" or self.failures >= self.threshold:
                self.state = "open"
                self.opened_at = self.clock()


def breaker_failure(outcome: Outcome) -> bool:
    return outcome.error is not None or (outcome.status or 0) >= 500


def has_header(headers: Dict[str, str], name: str) -> bool:
    return any(key.lower() == name.lower() for key in headers)


class Client:
    def __init__(
        self,
        policy: Optional[RetryPolicy] = None,
        timeouts: Optional[Timeouts] = None,
        bucket: Optional[TokenBucket] = None,
        breaker: Optional[CircuitBreaker] = None,
        transport: Callable[..., Response] = http_transport,
        clock: Callable[[], float] = time.monotonic,
        wall: Callable[[], float] = time.time,
        sleep: Callable[[float], None] = time.sleep,
        rng: Optional[random.Random] = None,
        log: Optional[LogFn] = None,
        user_agent: str = "apiclient/1.0",
    ):
        self.policy = policy or RetryPolicy()
        self.timeouts = timeouts or Timeouts()
        self.bucket = bucket
        self.breaker = breaker
        self.transport = transport
        self.clock = clock
        self.wall = wall
        self.sleep = sleep
        self.rng = rng or random.Random()
        self.log = log or (lambda record: None)
        self.user_agent = user_agent
        self.etags: Dict[str, Response] = {}
        self.paused_until = 0.0

    def request(self, method: str, url: str, body: Optional[bytes] = None, headers: Optional[Dict[str, str]] = None, idempotency_key: Optional[str] = None) -> Response:
        method = method.upper()
        sent = {"User-Agent": self.user_agent, "Accept": "application/json"}
        sent.update(headers or {})
        if idempotency_key:
            sent[IDEMPOTENCY_HEADER] = idempotency_key
        if body is not None and not has_header(sent, "Content-Type"):
            sent["Content-Type"] = "application/json"
        has_key = has_header(sent, IDEMPOTENCY_HEADER)
        cached = self.etags.get(url) if method == "GET" else None
        if cached is not None and cached.header("etag"):
            sent["If-None-Match"] = cached.header("etag") or ""
        started = self.clock()
        attempt = 0
        while True:
            attempt += 1
            self.wait_for_budget(method, url, attempt)
            if self.breaker is not None:
                try:
                    self.breaker.allow()
                except CircuitOpenError as err:
                    self.log({"event": "refused", "method": method, "url": url, "attempt": attempt, "reason": str(err), "breaker": "open"})
                    raise
            began = self.clock()
            failure: Optional[TransportError] = None
            response: Optional[Response] = None
            try:
                response = self.transport(method, url, sent, body, self.timeouts)
                outcome = Outcome(response.status, response.headers)
            except TransportError as err:
                failure = err
                outcome = Outcome(error=err.kind)
            elapsed = self.clock() - began
            if self.breaker is not None:
                self.breaker.record(breaker_failure(outcome))
            if response is not None:
                self.observe_rate_limit(response.headers)
            decision = decide(self.policy, method, has_key, attempt, outcome, self.wall(), self.rng)
            if decision.retry and (self.clock() - started) + decision.delay > self.policy.deadline:
                decision = Decision(False, decision.delay, f"deadline {self.policy.deadline:g}s would be exceeded")
            self.log({
                "event": "attempt",
                "method": method,
                "url": url,
                "attempt": attempt,
                "status": outcome.status,
                "error": failure.detail if failure else None,
                "error_kind": outcome.error,
                "ms": round(elapsed * 1000, 1),
                "decision": "retry" if decision.retry else "stop",
                "reason": decision.reason,
                "delay_s": round(decision.delay, 3),
                "idempotency_key": has_key,
                "ratelimit_remaining": response.header("x-ratelimit-remaining") if response else None,
                "breaker": self.breaker.current() if self.breaker else None,
            })
            if not decision.retry:
                if failure is not None:
                    failure.attempts = attempt
                    raise failure
                assert response is not None
                response.attempts = attempt
                return self.finish(method, url, response, cached)
            self.sleep(decision.delay)

    def wait_for_budget(self, method: str, url: str, attempt: int) -> None:
        pause = self.paused_until - self.wall()
        if pause > 0:
            if pause > self.policy.max_wait:
                raise RateLimitedError(pause)
            self.log({"event": "pause", "method": method, "url": url, "attempt": attempt, "delay_s": round(pause, 3), "reason": "x-ratelimit-remaining was 0"})
            self.sleep(pause)
        self.paused_until = 0.0
        if self.bucket is not None:
            wait = self.bucket.reserve()
            if wait > 0:
                self.log({"event": "throttle", "method": method, "url": url, "attempt": attempt, "delay_s": round(wait, 3), "reason": "client token bucket"})
                self.sleep(wait)

    def observe_rate_limit(self, headers: Dict[str, str]) -> None:
        if (headers.get("x-ratelimit-remaining") or "").strip() != "0":
            return
        hint = server_wait({"x-ratelimit-remaining": "0", "x-ratelimit-reset": headers.get("x-ratelimit-reset", "")}, self.wall())
        if hint is not None:
            self.paused_until = self.wall() + hint[0]

    def finish(self, method: str, url: str, response: Response, cached: Optional[Response]) -> Response:
        if response.status == 304 and cached is not None:
            merged = dict(cached.headers)
            merged.update(response.headers)
            return Response(cached.status, merged, cached.body, url, response.attempts, True)
        if method == "GET" and response.status == 200 and response.header("etag"):
            self.etags[url] = response
        return response

    def get(self, url: str, headers: Optional[Dict[str, str]] = None) -> Response:
        response = self.request("GET", url, headers=headers)
        if response.status >= 400:
            raise HTTPStatusError(response)
        return response


def parse_link_header(value: str, base: str = "") -> Dict[str, str]:
    links: Dict[str, str] = {}
    for target, params in re.findall(r"<([^>]*)>([^<]*)", value or ""):
        match = re.search(r'rel\s*=\s*"([^"]*)"|rel\s*=\s*([^\s;,"]+)', params, re.IGNORECASE)
        if not match:
            continue
        for rel in (match.group(1) or match.group(2)).split():
            links.setdefault(rel.lower(), urljoin(base, target) if base else target)
    return links


def with_query(url: str, updates: Dict[str, str]) -> str:
    parts = urlsplit(url)
    query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if k not in updates]
    query.extend(updates.items())
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))


def page_json(response: Response) -> Any:
    try:
        return response.json()
    except ValueError:
        raise PaginationError(f"{response.url}: page is not JSON: {response.text[:80]!r}") from None


def page_items(data: Any, items_key: Optional[str], url: str) -> List[Any]:
    items = data.get(items_key) if items_key and isinstance(data, dict) else data
    if not isinstance(items, list):
        where = f"'{items_key}'" if items_key else "the body"
        raise PaginationError(f"{url}: expected a JSON array in {where}")
    return items


def paginate_links(client: Client, url: str, items_key: Optional[str] = None, max_pages: int = 1000) -> Iterator[Any]:
    seen = set()
    next_url: Optional[str] = url
    while next_url:
        if next_url in seen:
            raise PaginationError(f"Link rel=next loops back to {next_url}")
        if len(seen) >= max_pages:
            raise PaginationError(f"stopped after max_pages={max_pages}")
        seen.add(next_url)
        response = client.get(next_url)
        items = page_items(page_json(response), items_key, next_url)
        client.log({"event": "page", "url": next_url, "items": len(items)})
        yield from items
        next_url = parse_link_header(response.header("link") or "", next_url).get("next")


def paginate_cursor(client: Client, url: str, cursor_param: str = "cursor", items_key: str = "items", next_key: str = "next_cursor", max_pages: int = 1000) -> Iterator[Any]:
    seen = set()
    cursor: Optional[str] = None
    for _ in range(max_pages):
        page_url = with_query(url, {cursor_param: cursor}) if cursor else url
        response = client.get(page_url)
        data = page_json(response)
        items = page_items(data, items_key, page_url)
        client.log({"event": "page", "url": page_url, "items": len(items)})
        yield from items
        cursor = data.get(next_key) if isinstance(data, dict) else None
        if not cursor:
            return
        if cursor in seen:
            raise PaginationError(f"cursor {cursor!r} repeats")
        seen.add(cursor)
    raise PaginationError(f"stopped after max_pages={max_pages}")


def encode_cursor(offset: int) -> str:
    return base64.urlsafe_b64encode(f"offset:{offset}".encode()).decode().rstrip("=")


def decode_cursor(token: str) -> int:
    try:
        text = base64.urlsafe_b64decode(token + "=" * (-len(token) % 4)).decode()
    except (ValueError, UnicodeDecodeError):
        raise ValueError(f"invalid cursor {token!r}") from None
    prefix, _, number = text.partition(":")
    if prefix != "offset" or not number.isdigit():
        raise ValueError(f"invalid cursor {token!r}")
    return int(number)


def etag_of(document: Dict[str, Any]) -> str:
    return '"' + hashlib.sha256(json.dumps(document, sort_keys=True).encode()).hexdigest()[:16] + '"'


def etag_matches(header: Optional[str], etag: str) -> bool:
    if not header:
        return False
    if header.strip() == "*":
        return True
    strong = etag[2:] if etag.startswith("W/") else etag
    for candidate in header.split(","):
        candidate = candidate.strip()
        if (candidate[2:] if candidate.startswith("W/") else candidate) == strong:
            return True
    return False


def validate_plan(steps: Any) -> List[Dict[str, Any]]:
    if not isinstance(steps, list):
        raise ValueError("plan must be a list of steps")
    for index, step in enumerate(steps):
        if not isinstance(step, dict):
            raise ValueError(f"plan[{index}] must be an object")
        unknown = set(step) - STEP_KEYS
        if unknown:
            raise ValueError(f"plan[{index}]: unknown keys {sorted(unknown)}")
        if "status" in step and not (isinstance(step["status"], int) and 100 <= step["status"] <= 599):
            raise ValueError(f"plan[{index}].status must be an HTTP status code")
        if int(step.get("times", 1)) < 1:
            raise ValueError(f"plan[{index}].times must be >= 1")
    return [dict(step) for step in steps]


class FaultState:
    def __init__(self, items: int = 23, max_per_page: int = 10, clock: Callable[[], float] = time.monotonic, wall: Callable[[], float] = time.time):
        self.lock = threading.Lock()
        self.items = items
        self.max_per_page = max_per_page
        self.clock = clock
        self.wall = wall
        self.reset()

    def reset(self) -> None:
        with self.lock:
            self.plans: Dict[str, List[Dict[str, Any]]] = {}
            self.hits: Dict[str, List[Dict[str, Any]]] = {}
            self.document: Dict[str, Any] = {"name": "cloudcaptain", "version": 1}
            self.limit = 60
            self.remaining = 60
            self.reset_at = int(self.wall()) + 3600

    def plan(self, name: str, steps: Any) -> None:
        checked = validate_plan(steps)
        with self.lock:
            self.plans[name] = checked
            self.hits[name] = []

    def record(self, name: str, method: str, headers: Any, body: bytes) -> int:
        with self.lock:
            hits = self.hits.setdefault(name, [])
            hits.append({
                "t": self.clock(),
                "method": method,
                "idempotency_key": headers.get(IDEMPOTENCY_HEADER),
                "if_none_match": headers.get("If-None-Match"),
                "bytes": len(body),
            })
            return len(hits)

    def next_step(self, name: str) -> Dict[str, Any]:
        with self.lock:
            steps = self.plans.get(name) or []
            if not steps:
                return {}
            step = dict(steps[0])
            times = int(steps[0].get("times", 1))
            if times <= 1:
                steps.pop(0)
            else:
                steps[0]["times"] = times - 1
            return step

    def hits_of(self, name: str) -> List[Dict[str, Any]]:
        with self.lock:
            return [dict(hit) for hit in self.hits.get(name, [])]


class FaultHandler(BaseHTTPRequestHandler):
    server_version = "apiclient-fault/1.0"

    def log_message(self, format: str, *args: Any) -> None:
        return

    def do_GET(self) -> None:
        self.dispatch()

    def do_POST(self) -> None:
        self.dispatch()

    def do_PUT(self) -> None:
        self.dispatch()

    def do_PATCH(self) -> None:
        self.dispatch()

    def do_DELETE(self) -> None:
        self.dispatch()

    @property
    def state(self) -> FaultState:
        return self.server.state

    def dispatch(self) -> None:
        parts = urlsplit(self.path)
        query = parse_qs(parts.query)
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""
        try:
            self.route(parts.path, query, body)
        except (ValueError, KeyError, TypeError) as err:
            self.send_json(400, {"message": str(err)})
        except (BrokenPipeError, ConnectionResetError):
            self.close_connection = True

    def route(self, path: str, query: Dict[str, List[str]], body: bytes) -> None:
        if path == "/__reset" and self.command == "POST":
            self.state.reset()
            return self.send_json(200, {"reset": True})
        if path.startswith("/__plan/") and self.command == "POST":
            name = path[len("/__plan/"):]
            self.state.plan(name, json.loads(body or b"{}")["plan"])
            return self.send_json(200, {"name": name, "planned": True})
        if path.startswith("/__hits/"):
            name = path[len("/__hits/"):]
            return self.send_json(200, {"name": name, "hits": self.state.hits_of(name)})
        if path.startswith("/s/"):
            return self.scripted(path[len("/s/"):], body)
        if path == "/items":
            self.state.record("items", self.command, self.headers, body)
            return self.items_page(query)
        if path == "/cursor":
            self.state.record("cursor", self.command, self.headers, body)
            return self.cursor_page(query)
        if path == "/conditional":
            self.state.record("conditional", self.command, self.headers, body)
            return self.conditional(body)
        self.send_json(404, {"message": f"no route {path}"})

    def scripted(self, name: str, body: bytes) -> None:
        count = self.state.record(name, self.command, self.headers, body)
        step = self.state.next_step(name)
        if step.get("delay"):
            time.sleep(float(step["delay"]))
        if step.get("reset"):
            return self.reset_connection()
        status = int(step.get("status", 200))
        headers = {str(k): str(v) for k, v in (step.get("headers") or {}).items()}
        if "reset_in" in step:
            headers.update({
                "X-RateLimit-Limit": "60",
                "X-RateLimit-Remaining": "0",
                "X-RateLimit-Reset": str(int(self.state.wall()) + int(step["reset_in"])),
            })
        default = {"name": name, "attempt": count, "method": self.command} if status < 400 else {"message": http.HTTPStatus(status).phrase}
        self.send_json(status, step.get("body", default), headers)

    def reset_connection(self) -> None:
        self.connection.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
        self.close_connection = True
        self.rfile.close()
        self.connection.close()

    def items_page(self, query: Dict[str, List[str]]) -> None:
        per_page = min(self.state.max_per_page, max(1, int(query.get("per_page", ["5"])[0])))
        page = max(1, int(query.get("page", ["1"])[0]))
        total = self.state.items
        last = max(1, math.ceil(total / per_page))
        start = (page - 1) * per_page
        rows = [{"id": i, "name": f"item-{i:03d}"} for i in range(start + 1, min(total, start + per_page) + 1)]
        base = f"http://{self.headers.get('Host', 'localhost')}/items"

        def link(number: int, rel: str) -> str:
            return f'<{base}?{urlencode({"page": number, "per_page": per_page})}>; rel="{rel}"'

        links = []
        if page < last:
            links += [link(page + 1, "next"), link(last, "last")]
        if page > 1:
            links += [link(page - 1, "prev"), link(1, "first")]
        self.send_json(200, rows, {"Link": ", ".join(links)} if links else {})

    def cursor_page(self, query: Dict[str, List[str]]) -> None:
        limit = min(self.state.max_per_page, max(1, int(query.get("limit", ["5"])[0])))
        token = query.get("cursor", [""])[0]
        offset = decode_cursor(token) if token else 0
        total = self.state.items
        rows = [{"id": i, "name": f"item-{i:03d}"} for i in range(offset + 1, min(total, offset + limit) + 1)]
        following = encode_cursor(offset + limit) if offset + limit < total else None
        self.send_json(200, {"items": rows, "next_cursor": following})

    def conditional(self, body: bytes) -> None:
        state = self.state
        if self.command == "PUT":
            document = json.loads(body or b"{}")
            if not isinstance(document, dict):
                raise ValueError("document must be a JSON object")
            with state.lock:
                state.document = document
        with state.lock:
            etag = etag_of(state.document)
            not_modified = self.command == "GET" and etag_matches(self.headers.get("If-None-Match"), etag)
            exhausted = state.remaining <= 0 and not not_modified
            if not not_modified and not exhausted:
                state.remaining -= 1
            headers = {
                "ETag": etag,
                "Cache-Control": "private, max-age=60",
                "X-RateLimit-Limit": str(state.limit),
                "X-RateLimit-Remaining": str(state.remaining),
                "X-RateLimit-Used": str(state.limit - state.remaining),
                "X-RateLimit-Reset": str(state.reset_at),
            }
            document = dict(state.document)
        if not_modified:
            return self.send_json(304, None, headers)
        if exhausted:
            return self.send_json(429, {"message": "API rate limit exceeded"}, headers)
        self.send_json(200, document, headers)

    def send_json(self, status: int, payload: Any, headers: Optional[Dict[str, str]] = None) -> None:
        data = b"" if payload is None or status == 304 else json.dumps(payload).encode()
        self.send_response(status)
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        if status != 304:
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        if data:
            self.wfile.write(data)


class FaultServer:
    def __init__(self, host: str = "127.0.0.1", port: int = 0, items: int = 23, max_per_page: int = 10):
        self.httpd = ThreadingHTTPServer((host, port), FaultHandler)
        self.httpd.daemon_threads = True
        self.httpd.state = FaultState(items, max_per_page)
        self.url = f"http://{host}:{self.httpd.server_address[1]}"
        self.thread: Optional[threading.Thread] = None

    @property
    def state(self) -> FaultState:
        return self.httpd.state

    def start(self) -> "FaultServer":
        self.thread = threading.Thread(target=self.httpd.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
        self.thread.start()
        return self

    def close(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
        if self.thread is not None:
            self.thread.join()

    def __enter__(self) -> "FaultServer":
        return self.start()

    def __exit__(self, *exc: Any) -> None:
        self.close()


def json_lines(stream: Any) -> LogFn:
    def write(record: Dict[str, Any]) -> None:
        stream.write(json.dumps(record, sort_keys=True) + "\n")
        stream.flush()
    return write


def positive(value: str) -> float:
    number = float(value)
    if not number > 0:
        raise argparse.ArgumentTypeError(f"must be > 0, got {value}")
    return number


def non_negative_int(value: str) -> int:
    number = int(value)
    if number < 0:
        raise argparse.ArgumentTypeError(f"must be >= 0, got {value}")
    return number


def client_options() -> argparse.ArgumentParser:
    parent = argparse.ArgumentParser(add_help=False)
    parent.add_argument("--retries", type=non_negative_int, default=3, help="retries after the first attempt (default 3)")
    parent.add_argument("--backoff-base", type=positive, default=0.5, help="first backoff ceiling in seconds (default 0.5)")
    parent.add_argument("--backoff-cap", type=positive, default=30.0, help="largest backoff ceiling in seconds (default 30)")
    parent.add_argument("--max-wait", type=positive, default=60.0, help="longest Retry-After or rate-limit reset to wait for (default 60)")
    parent.add_argument("--deadline", type=positive, default=120.0, help="total seconds for all attempts and waits (default 120)")
    parent.add_argument("--connect-timeout", type=positive, default=3.05, help="seconds to establish the connection (default 3.05)")
    parent.add_argument("--read-timeout", type=positive, default=30.0, help="seconds to wait for the response (default 30)")
    parent.add_argument("--rate", type=positive, help="client-side limit in requests per second")
    parent.add_argument("--burst", type=int, default=1, help="token bucket size for --rate (default 1)")
    parent.add_argument("--breaker-failures", type=int, default=5, help="consecutive failures that open the circuit (default 5)")
    parent.add_argument("--breaker-reset", type=positive, default=30.0, help="seconds the circuit stays open before a trial (default 30)")
    parent.add_argument("-H", "--header", action="append", default=[], help="extra request header 'Name: value' (repeatable)")
    parent.add_argument("--seed", type=int, help="seed the jitter for reproducible runs")
    parent.add_argument("--log", help="append attempt logs (JSON lines) to this file instead of stderr")
    parent.add_argument("--quiet", action="store_true", help="no attempt logs")
    parent.add_argument("--json", action="store_true", help="machine-readable output on stdout")
    return parent


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="apiclient", description="A resilient HTTP API client and a fault-injecting test server.")
    sub = parser.add_subparsers(dest="command", required=True)
    common = client_options()
    request = sub.add_parser("request", parents=[common], help="send one request with retries, limits and a circuit breaker")
    request.add_argument("method")
    request.add_argument("url")
    request.add_argument("-d", "--data", help="request body (sent as application/json unless -H sets Content-Type)")
    keys = request.add_mutually_exclusive_group()
    keys.add_argument("--idempotency-key", help="send Idempotency-Key, which makes a POST or PATCH retryable")
    keys.add_argument("--auto-idempotency-key", action="store_true", help="generate one Idempotency-Key and reuse it on every attempt")
    request.add_argument("--dry-run", action="store_true", help="print the retry plan without sending anything")
    paginate = sub.add_parser("paginate", parents=[common], help="GET every page and print the items")
    paginate.add_argument("url")
    paginate.add_argument("--cursor", action="store_true", help="cursor pagination in the body instead of Link headers")
    paginate.add_argument("--cursor-param", default="cursor")
    paginate.add_argument("--items-key", help="key holding the items (default: the body is the array; 'items' with --cursor)")
    paginate.add_argument("--next-key", default="next_cursor")
    paginate.add_argument("--max-pages", type=int, default=1000)
    serve = sub.add_parser("serve", help="run the fault-injecting test server")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8080)
    serve.add_argument("--items", type=int, default=23, help="items behind /items and /cursor (default 23)")
    serve.add_argument("--max-per-page", type=int, default=10, help="page size cap (default 10)")
    return parser


def parse_headers(values: Sequence[str]) -> Dict[str, str]:
    headers: Dict[str, str] = {}
    for value in values:
        name, sep, content = value.partition(":")
        if not sep or not name.strip() or re.search(r"\s", name.strip()):
            raise ValueError(f"header must look like 'Name: value', got {value!r}")
        headers[name.strip()] = content.strip()
    return headers


def check_url(url: str) -> None:
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise ValueError(f"URL must be http:// or https:// with a host, got {url!r}")


def client_from(args: argparse.Namespace, log: Optional[LogFn]) -> Client:
    if args.burst < 1:
        raise ValueError("--burst must be >= 1")
    if args.breaker_failures < 1:
        raise ValueError("--breaker-failures must be >= 1")
    policy = RetryPolicy(args.retries, args.backoff_base, args.backoff_cap, args.max_wait, args.deadline)
    bucket = TokenBucket(args.rate, args.burst) if args.rate else None
    breaker = CircuitBreaker(args.breaker_failures, args.breaker_reset)
    rng = random.Random(args.seed) if args.seed is not None else random.Random()
    return Client(policy, Timeouts(args.connect_timeout, args.read_timeout), bucket, breaker, rng=rng, log=log)


def plan_of(args: argparse.Namespace, key: Optional[str], headers: Dict[str, str]) -> Dict[str, Any]:
    method = args.method.upper()
    keyed = bool(key) or has_header(headers, IDEMPOTENCY_HEADER)
    policy = RetryPolicy(args.retries, args.backoff_base, args.backoff_cap, args.max_wait, args.deadline)
    safe = retry_safe(method, keyed)
    if method in IDEMPOTENT_METHODS:
        why = f"{method} is idempotent"
    elif keyed:
        why = f"{method} carries {IDEMPOTENCY_HEADER}, so the server can deduplicate"
    else:
        why = f"{method} without {IDEMPOTENCY_HEADER}: retried only when the connection was never made"
    return {
        "method": method,
        "url": args.url,
        "retry_safe": safe,
        "why": why,
        "idempotency_key": key,
        "max_attempts": args.retries + 1 if safe else 1,
        "retry_statuses": sorted(policy.statuses),
        "backoff_ceilings_s": [policy.ceiling(n) for n in range(args.retries)],
        "max_wait_s": args.max_wait,
        "deadline_s": args.deadline,
        "timeouts_s": {"connect": args.connect_timeout, "read": args.read_timeout},
        "rate_limit": {"per_second": args.rate, "burst": args.burst} if args.rate else None,
        "circuit_breaker": {"failures": args.breaker_failures, "reset_s": args.breaker_reset},
    }


def print_plan(plan: Dict[str, Any], as_json: bool) -> None:
    if as_json:
        print(json.dumps(plan, indent=2))
        return
    print(f"{plan['method']} {plan['url']}")
    print(f"retry-safe: {'yes' if plan['retry_safe'] else 'no'} ({plan['why']})")
    print(f"attempts: at most {plan['max_attempts']}; retried on {', '.join(map(str, plan['retry_statuses']))} and transport errors")
    ceilings = ", ".join(f"{c:g}s" for c in plan["backoff_ceilings_s"]) or "none"
    print(f"backoff: full jitter under {ceilings}; Retry-After or X-RateLimit-Reset wins when present, up to {plan['max_wait_s']:g}s")
    print(f"timeouts: connect {plan['timeouts_s']['connect']:g}s, read {plan['timeouts_s']['read']:g}s, all attempts within {plan['deadline_s']:g}s")


def emit_response(response: Response, as_json: bool) -> None:
    if as_json:
        try:
            body: Any = response.json()
        except ValueError:
            body = response.text
        print(json.dumps({"status": response.status, "attempts": response.attempts, "from_cache": response.from_cache, "headers": response.headers, "body": body}, indent=2))
        return
    text = response.text
    sys.stdout.write(text if text.endswith("\n") or not text else text + "\n")


def run_request(args: argparse.Namespace, log: Optional[LogFn]) -> int:
    check_url(args.url)
    if not re.fullmatch(r"[A-Za-z]+", args.method):
        raise ValueError(f"method must be letters only, got {args.method!r}")
    headers = parse_headers(args.header)
    key = str(uuid.uuid4()) if args.auto_idempotency_key else args.idempotency_key
    if args.dry_run:
        print_plan(plan_of(args, key, headers), args.json)
        return 0
    client = client_from(args, log)
    body = args.data.encode() if args.data is not None else None
    response = client.request(args.method, args.url, body=body, headers=headers, idempotency_key=key)
    emit_response(response, args.json)
    if response.status >= 400:
        print(f"apiclient: {args.method.upper()} {args.url} -> HTTP {response.status} after {response.attempts} attempt(s)", file=sys.stderr)
        return 1
    return 0


def run_paginate(args: argparse.Namespace, log: Optional[LogFn]) -> int:
    check_url(args.url)
    client = client_from(args, log)
    headers = parse_headers(args.header)
    if headers:
        original = client.transport

        def transport(method: str, url: str, sent: Dict[str, str], body: Optional[bytes], timeouts: Timeouts) -> Response:
            return original(method, url, dict(sent, **headers), body, timeouts)
        client.transport = transport
    if args.cursor:
        items = list(paginate_cursor(client, args.url, args.cursor_param, args.items_key or "items", args.next_key, args.max_pages))
    else:
        items = list(paginate_links(client, args.url, args.items_key, args.max_pages))
    if args.json:
        print(json.dumps(items, indent=2))
    else:
        for item in items:
            print(json.dumps(item, sort_keys=True))
    return 0


def run_serve(args: argparse.Namespace) -> int:
    server = FaultServer(args.host, args.port, args.items, args.max_per_page)
    print(f"apiclient fault server listening on {server.url}", flush=True)
    try:
        server.httpd.serve_forever(poll_interval=0.1)
    except KeyboardInterrupt:
        pass
    finally:
        server.httpd.server_close()
    return 0


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    try:
        args = parser.parse_args(list(sys.argv[1:] if argv is None else argv))
    except SystemExit as exit:
        return int(exit.code or 0)
    if args.command == "serve":
        try:
            return run_serve(args)
        except OSError as err:
            print(f"apiclient: cannot listen on {args.host}:{args.port}: {err}", file=sys.stderr)
            return 1
    log_file = None
    try:
        if args.quiet:
            log = None
        elif args.log:
            log_file = open(args.log, "a", encoding="utf-8")
            log = json_lines(log_file)
        else:
            log = json_lines(sys.stderr)
        if args.command == "request":
            return run_request(args, log)
        return run_paginate(args, log)
    except ValueError as err:
        print(f"apiclient: {err}", file=sys.stderr)
        return 2
    except TransportError as err:
        print(f"apiclient: {err} after {err.attempts} attempt(s)", file=sys.stderr)
        return 1
    except APIError as err:
        print(f"apiclient: {err}", file=sys.stderr)
        return 1
    except OSError as err:
        print(f"apiclient: {err}", file=sys.stderr)
        return 1
    finally:
        if log_file is not None:
            log_file.close()


if __name__ == "__main__":
    raise SystemExit(main())
