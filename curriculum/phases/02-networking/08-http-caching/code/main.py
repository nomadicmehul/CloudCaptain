"""minicache: an RFC 9111 caching reverse proxy (Vary keys, freshness and Age, conditional revalidation, stale-while-revalidate, stale-if-error, request collapsing, purge by URL and tag) and a checker that explains how a browser, a shared cache, a CDN and nginx treat a response."""
from __future__ import annotations

import argparse
import email.utils
import http.client
import json
import os
import re
import socket
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Set, Tuple

NAME = "minicache"
Headers = List[Tuple[str, str]]
HOP_BY_HOP = {"connection", "keep-alive", "proxy-connection", "te", "trailer", "transfer-encoding", "upgrade"}
TAG_HEADERS = {"surrogate-key", "cache-tag"}
CONDITIONALS = {"if-none-match", "if-modified-since", "if-match", "if-unmodified-since", "if-range", "range"}
NOT_UPDATED_BY_304 = HOP_BY_HOP | {"content-length", "content-encoding", "content-range"}
NOT_MODIFIED_FIELDS = {"cache-control", "content-location", "date", "etag", "expires", "vary", "last-modified", "age", "via", "cache-status", "x-cache-status"}
HEURISTIC_STATUSES = {200, 203, 204, 206, 300, 301, 308, 404, 405, 410, 414, 501}
ERROR_STATUSES = {500, 502, 503, 504}
SAFE_METHODS = {"GET", "HEAD", "OPTIONS", "TRACE"}
CACHEABLE_METHODS = {"GET", "HEAD"}
MAX_DELTA = 2 ** 31
DAY = 86400
LOOPBACK = {"127.0.0.1", "::1"}
SHOWN_HEADERS = ("Cache-Control", "CDN-Cache-Control", "Expires", "Date", "Last-Modified", "ETag", "Vary", "Age", "Pragma", "Set-Cookie", "Surrogate-Key", "Cache-Tag", "X-Accel-Expires")
FRAGMENTING_VARY = {"user-agent", "cookie", "authorization", "referer"}
ROUTE_FIELDS = {"cache_control": str, "vary": str, "delay": (int, float), "headers": dict, "etag": bool, "last_modified_age": int}
DEFAULT_ROUTES: Dict[str, Dict[str, Any]] = {
    "fresh": {"cache_control": "max-age=2"},
    "plain": {"cache_control": "max-age=1"},
    "vary": {"cache_control": "max-age=60", "vary": "Accept-Language"},
    "nostore": {"cache_control": "no-store"},
    "nocache": {"cache_control": "no-cache"},
    "private": {"cache_control": "private, max-age=60"},
    "cookie": {"cache_control": "max-age=60", "headers": {"Set-Cookie": "session=s3cr3t; Path=/; HttpOnly"}},
    "sie": {"cache_control": "max-age=1, stale-if-error=60"},
    "swr": {"cache_control": "max-age=1, stale-while-revalidate=30"},
    "slow": {"cache_control": "max-age=60", "delay": 1.0},
    "tagged": {"cache_control": "max-age=60", "headers": {"Surrogate-Key": "product-42 catalogue"}},
    "heuristic": {"last_modified_age": 36000},
    "app.3f9a1c2b.js": {"cache_control": "public, max-age=31536000, immutable"},
    "ready": {"cache_control": "no-store"},
}


class InputError(Exception):
    pass


class UpstreamError(Exception):
    def __init__(self, kind: str, message: str) -> None:
        super().__init__(message)
        self.kind = kind


def header(headers: Headers, name: str) -> Optional[str]:
    wanted = name.lower()
    values = [value for key, value in headers if key.lower() == wanted]
    return ", ".join(values) if values else None


def without(headers: Headers, names: Iterable[str]) -> Headers:
    drop = {name.lower() for name in names}
    return [(key, value) for key, value in headers if key.lower() not in drop]


def replace(headers: Headers, name: str, value: str) -> Headers:
    return without(headers, [name]) + [(name, value)]


def end_to_end(headers: Headers) -> Headers:
    named = {item.strip().lower() for item in (header(headers, "Connection") or "").split(",") if item.strip()}
    return without(headers, HOP_BY_HOP | named)


def split_list(value: str) -> List[str]:
    items: List[str] = []
    current: List[str] = []
    quoted = escaped = False
    for char in value:
        if escaped:
            escaped = False
        elif char == "\\" and quoted:
            escaped = True
        elif char == '"':
            quoted = not quoted
        elif char == "," and not quoted:
            items.append("".join(current).strip())
            current = []
            continue
        current.append(char)
    items.append("".join(current).strip())
    return [item for item in items if item]


def unquote(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] == '"':
        return re.sub(r"\\(.)", r"\1", value[1:-1])
    return value


def parse_cache_control(value: Optional[str]) -> Dict[str, Optional[str]]:
    directives: Dict[str, Optional[str]] = {}
    for item in split_list(value or ""):
        name, sep, raw = item.partition("=")
        name = name.strip().lower()
        if name and name not in directives:
            directives[name] = unquote(raw) if sep else None
    return directives


def delta_seconds(value: Optional[str]) -> Optional[int]:
    if value is None or not re.fullmatch(r"[0-9]+", value.strip()):
        return None
    return min(int(value.strip()), MAX_DELTA)


def parse_http_date(value: Optional[str]) -> Optional[float]:
    if not value or not value.strip():
        return None
    try:
        parsed = email.utils.parsedate_to_datetime(value.strip())
    except (TypeError, ValueError, IndexError, OverflowError):
        return None
    if parsed is None:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


def http_date(when: float) -> str:
    return email.utils.formatdate(when, usegmt=True)


@dataclass(frozen=True)
class Policy:
    shared: bool = True
    cdn: bool = False
    heuristic_fraction: float = 0.1
    refuse_set_cookie: bool = True

    @property
    def label(self) -> str:
        if self.cdn:
            return "CDN: shared, reads CDN-Cache-Control first"
        return "shared cache" if self.shared else "private cache"


@dataclass
class Response:
    status: int
    headers: Headers
    body: bytes = b""


@dataclass
class Entry:
    key: str
    url: str
    response: Response
    vary: Dict[str, Optional[str]]
    request_time: float
    response_time: float
    tags: List[str] = field(default_factory=list)
    hits: int = 0

    def matches(self, request_headers: Headers) -> bool:
        return all(normalize_field(header(request_headers, name)) == value for name, value in self.vary.items())


@dataclass
class Assessment:
    action: str
    fwd: str
    age: float
    lifetime: float
    source: str
    reason: str

    @property
    def ttl(self) -> float:
        return self.lifetime - self.age


@dataclass
class Result:
    status: int
    headers: Headers
    body: bytes
    cache_status: str


Fetch = Callable[[str, str, Headers, Optional[bytes]], Response]


def response_directives(headers: Headers, policy: Policy) -> Dict[str, Optional[str]]:
    if policy.cdn:
        targeted = header(headers, "CDN-Cache-Control")
        if targeted is not None:
            return parse_cache_control(targeted)
    return parse_cache_control(header(headers, "Cache-Control"))


def request_directives(headers: Headers) -> Dict[str, Optional[str]]:
    value = header(headers, "Cache-Control")
    if value is None:
        return {"no-cache": None} if "no-cache" in parse_cache_control(header(headers, "Pragma")) else {}
    return parse_cache_control(value)


def vary_names(headers: Headers) -> List[str]:
    return sorted({item.strip().lower() for item in (header(headers, "Vary") or "").split(",") if item.strip()})


def normalize_field(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    return ", ".join(re.sub(r"\s+", " ", part.strip()) for part in value.split(","))


def tags_of(headers: Headers) -> List[str]:
    raw = " ".join(value for value in (header(headers, "Surrogate-Key"), header(headers, "Cache-Tag")) if value)
    return sorted({tag for tag in re.split(r"[\s,]+", raw) if tag})


def primary_key(method: str, url: str) -> str:
    parts = urllib.parse.urlsplit(url)
    target = (parts.path or "/") + (f"?{parts.query}" if parts.query else "")
    verb = "GET" if method.upper() == "HEAD" else method.upper()
    return f"{verb} {parts.scheme.lower()}://{parts.netloc.lower()}{target}"


def storable(method: str, status: int, request_headers: Headers, response_headers: Headers, policy: Policy) -> Tuple[bool, str]:
    if method.upper() not in CACHEABLE_METHODS:
        return False, f"{method.upper()} responses are not stored"
    if status < 200:
        return False, f"{status} is an interim response"
    if status in (206, 304):
        return False, f"{status} is a partial or validation response, not a complete one"
    if "no-store" in request_directives(request_headers):
        return False, "the request said Cache-Control: no-store"
    cc = response_directives(response_headers, policy)
    if "no-store" in cc:
        return False, "no-store"
    if policy.shared and "private" in cc:
        return False, "private: only a browser's own cache may store it"
    if policy.shared and header(request_headers, "Authorization") is not None and not {"public", "s-maxage", "must-revalidate"} & set(cc):
        return False, "the request carried Authorization and the response does not say public, s-maxage or must-revalidate"
    if "*" in vary_names(response_headers):
        return False, "Vary: * never matches a later request"
    if policy.shared and policy.refuse_set_cookie and header(response_headers, "Set-Cookie") is not None:
        return False, "Set-Cookie: a shared copy would hand this cookie to the next user"
    explicit = [name for name in ("public", "max-age") if name in cc]
    if policy.shared and "s-maxage" in cc:
        explicit.append("s-maxage")
    if not policy.shared and "private" in cc:
        explicit.append("private")
    if header(response_headers, "Expires") is not None:
        explicit.append("Expires")
    if explicit:
        return True, "explicit: " + ", ".join(explicit)
    if status in HEURISTIC_STATUSES:
        return True, f"status {status} is heuristically cacheable"
    return False, f"status {status} needs an explicit lifetime to be stored"


def freshness_lifetime(status: int, headers: Headers, policy: Policy, received: float) -> Tuple[float, str]:
    cc = response_directives(headers, policy)
    if policy.shared and "s-maxage" in cc:
        return float(delta_seconds(cc["s-maxage"]) or 0), "s-maxage"
    if "max-age" in cc:
        return float(delta_seconds(cc["max-age"]) or 0), "max-age"
    date = parse_http_date(header(headers, "Date"))
    base = date if date is not None else received
    expires_text = header(headers, "Expires")
    if expires_text is not None:
        expires = parse_http_date(expires_text)
        if expires is None:
            return 0.0, "Expires (invalid, so already expired)"
        return max(0.0, expires - base), "Expires - Date"
    last_modified = parse_http_date(header(headers, "Last-Modified"))
    if last_modified is not None and (status in HEURISTIC_STATUSES or "public" in cc):
        return max(0.0, (base - last_modified) * policy.heuristic_fraction), f"{policy.heuristic_fraction:.0%} since Last-Modified"
    return 0.0, "none"


def current_age(entry: Entry, now: float) -> float:
    headers = entry.response.headers
    date = parse_http_date(header(headers, "Date"))
    date_value = date if date is not None else entry.response_time
    age_value = float(delta_seconds(header(headers, "Age")) or 0)
    apparent_age = max(0.0, entry.response_time - date_value)
    response_delay = entry.response_time - entry.request_time
    corrected_initial_age = max(apparent_age, age_value + response_delay)
    resident_time = now - entry.response_time
    return corrected_initial_age + resident_time


def entry_lifetime(entry: Entry, policy: Policy) -> Tuple[float, str]:
    return freshness_lifetime(entry.response.status, entry.response.headers, policy, entry.response_time)


def staleness_forbidden(cc: Dict[str, Optional[str]], policy: Policy) -> Optional[str]:
    if "no-cache" in cc:
        return "no-cache"
    if "must-revalidate" in cc:
        return "must-revalidate"
    if policy.shared and "proxy-revalidate" in cc:
        return "proxy-revalidate"
    if policy.shared and "s-maxage" in cc:
        return "s-maxage, which implies proxy-revalidate"
    return None


def assess(entry: Entry, request_headers: Headers, now: float, policy: Policy) -> Assessment:
    lifetime, source = entry_lifetime(entry, policy)
    age = current_age(entry, now)
    wants = request_directives(request_headers)
    cc = response_directives(entry.response.headers, policy)

    def verdict(action: str, reason: str, fwd: str = "stale") -> Assessment:
        return Assessment(action, fwd, age, lifetime, source, reason)

    if "no-cache" in wants:
        return verdict("revalidate", "the request said no-cache", "request")
    if "no-cache" in cc:
        return verdict("revalidate", "the response said no-cache: revalidate before every use")
    max_age = delta_seconds(wants.get("max-age"))
    if max_age is not None and age > max_age:
        return verdict("revalidate", f"the request accepts max-age={max_age} and the copy is {age:.0f}s old", "request")
    min_fresh = delta_seconds(wants.get("min-fresh"))
    if min_fresh is not None and lifetime - age < min_fresh:
        return verdict("revalidate", f"the request wants min-fresh={min_fresh} and {lifetime - age:.0f}s are left", "request")
    if lifetime > age:
        return verdict("hit", f"fresh: {source} gives {lifetime:.0f}s, age {age:.0f}s")
    staleness = age - lifetime
    forbidden = staleness_forbidden(cc, policy)
    if forbidden:
        return verdict("revalidate", f"stale by {staleness:.0f}s and {forbidden} forbids serving it stale")
    if "max-stale" in wants:
        allowed = delta_seconds(wants["max-stale"])
        if wants["max-stale"] is None or (allowed is not None and staleness <= allowed):
            return verdict("hit", f"stale by {staleness:.0f}s, accepted by the request's max-stale")
    window = delta_seconds(cc.get("stale-while-revalidate"))
    if window is not None and staleness <= window:
        return verdict("stale", f"stale by {staleness:.0f}s, within stale-while-revalidate={window}")
    return verdict("revalidate", f"stale by {staleness:.0f}s")


def stale_on_error(entry: Entry, request_headers: Headers, now: float, policy: Policy) -> Tuple[bool, str]:
    lifetime, _ = entry_lifetime(entry, policy)
    staleness = current_age(entry, now) - lifetime
    cc = response_directives(entry.response.headers, policy)
    forbidden = staleness_forbidden(cc, policy)
    if forbidden:
        return False, f"{forbidden} forbids serving it stale"
    windows = [w for w in (delta_seconds(cc.get("stale-if-error")), delta_seconds(request_directives(request_headers).get("stale-if-error"))) if w is not None]
    if not windows:
        return False, "no stale-if-error"
    if staleness <= max(windows):
        return True, f"stale by {staleness:.0f}s, within stale-if-error={max(windows)}"
    return False, f"stale by {staleness:.0f}s, beyond stale-if-error={max(windows)}"


def validators(response: Response) -> Headers:
    found: Headers = []
    etag = header(response.headers, "ETag")
    if etag:
        found.append(("If-None-Match", etag))
    last_modified = header(response.headers, "Last-Modified")
    if last_modified:
        found.append(("If-Modified-Since", last_modified))
    return found


def opaque_tag(tag: str) -> str:
    tag = tag.strip()
    return tag[2:] if tag.startswith("W/") else tag


def etag_matches(condition: str, etag: Optional[str]) -> bool:
    if etag is None:
        return False
    if condition.strip() == "*":
        return True
    return opaque_tag(etag) in {opaque_tag(item) for item in split_list(condition)}


def not_modified_for(request_headers: Headers, response: Response) -> bool:
    if response.status != 200:
        return False
    condition = header(request_headers, "If-None-Match")
    if condition is not None:
        return etag_matches(condition, header(response.headers, "ETag"))
    since = parse_http_date(header(request_headers, "If-Modified-Since"))
    modified = parse_http_date(header(response.headers, "Last-Modified"))
    return since is not None and modified is not None and modified <= since


def upstream_headers(request_headers: Headers, extra: Sequence[Tuple[str, str]] = (), keep_conditionals: bool = False) -> Headers:
    dropped = {"host", "content-length"} | (set() if keep_conditionals else CONDITIONALS)
    kept = without(end_to_end(request_headers), dropped)
    via = header(request_headers, "Via")
    return replace(kept, "Via", f"{via}, 1.1 {NAME}" if via else f"1.1 {NAME}") + list(extra)


def cache_status_value(name: str, params: Sequence[Tuple[str, Any]]) -> str:
    parts = [name]
    for key, value in params:
        if value is None or value is False:
            continue
        if value is True:
            parts.append(key)
        elif isinstance(value, int) or key == "fwd":
            parts.append(f"{key}={value}")
        else:
            text = str(value).replace("\\", "\\\\").replace('"', '\\"')
            parts.append(f'{key}="{text}"')
    return "; ".join(parts)


class Cache:
    def __init__(self, fetch: Fetch, policy: Optional[Policy] = None, clock: Callable[[], float] = time.time, lock_timeout: float = 5.0, name: str = NAME) -> None:
        self.fetch = fetch
        self.policy = policy or Policy()
        self.clock = clock
        self.lock_timeout = lock_timeout
        self.name = name
        self.entries: Dict[str, List[Entry]] = {}
        self.inflight: Dict[str, threading.Event] = {}
        self.refreshing: Set[int] = set()
        self.threads: List[threading.Thread] = []
        self.lock = threading.RLock()
        self.origin_requests = 0

    def handle(self, method: str, url: str, headers: Headers, body: Optional[bytes] = None) -> Result:
        method = method.upper()
        if method not in CACHEABLE_METHODS:
            return self.pass_through(method, url, headers, body)
        key = primary_key(method, url)
        wants = request_directives(headers)
        if "no-store" in wants:
            return self.forward_only(method, url, headers)
        entry = self.lookup(key, headers)
        if entry is None:
            if "only-if-cached" in wants:
                return self.refuse_uncached(method, headers)
            return self.miss(method, key, url, headers)
        verdict = assess(entry, headers, self.clock(), self.policy)
        if verdict.action == "hit":
            return self.from_store(entry, method, headers, "HIT", [("hit", True), ("ttl", round(verdict.ttl))])
        if "only-if-cached" in wants:
            return self.refuse_uncached(method, headers)
        if verdict.action == "stale":
            return self.stale_while_revalidate(entry, method, url, headers, verdict)
        return self.revalidate(entry, method, url, headers, verdict.fwd)

    def lookup(self, key: str, request_headers: Headers) -> Optional[Entry]:
        with self.lock:
            for entry in self.entries.get(key, []):
                if entry.matches(request_headers):
                    return entry
        return None

    def known(self, key: str) -> bool:
        with self.lock:
            return bool(self.entries.get(key))

    def store(self, key: str, url: str, request_headers: Headers, response: Response, request_time: float, response_time: float) -> Entry:
        kept = end_to_end(response.headers)
        selected = {name: normalize_field(header(request_headers, name)) for name in vary_names(kept)}
        entry = Entry(key, url, Response(response.status, kept, response.body), selected, request_time, response_time, tags_of(kept))
        with self.lock:
            others = [old for old in self.entries.get(key, []) if not old.matches(request_headers)]
            self.entries[key] = others + [entry]
        return entry

    def remove(self, entry: Entry) -> None:
        with self.lock:
            left = [old for old in self.entries.get(entry.key, []) if old is not entry]
            if left:
                self.entries[entry.key] = left
            else:
                self.entries.pop(entry.key, None)

    def purge_url(self, url: str) -> int:
        with self.lock:
            return len(self.entries.pop(primary_key("GET", url), []))

    def purge_tag(self, tag: str) -> int:
        removed = 0
        with self.lock:
            for key in list(self.entries):
                keep = [entry for entry in self.entries[key] if tag not in entry.tags]
                removed += len(self.entries[key]) - len(keep)
                if keep:
                    self.entries[key] = keep
                else:
                    del self.entries[key]
        return removed

    def invalidate(self, url: str, response_headers: Headers) -> int:
        removed = self.purge_url(url)
        origin = urllib.parse.urlsplit(url).netloc.lower()
        for name in ("Location", "Content-Location"):
            value = header(response_headers, name)
            if value:
                target = urllib.parse.urljoin(url, value)
                if urllib.parse.urlsplit(target).netloc.lower() == origin:
                    removed += self.purge_url(target)
        return removed

    def snapshot(self) -> List[Dict[str, Any]]:
        now = self.clock()
        with self.lock:
            items = [(key, list(entries)) for key, entries in sorted(self.entries.items())]
        rows = []
        for key, entries in items:
            for entry in entries:
                lifetime, source = entry_lifetime(entry, self.policy)
                age = current_age(entry, now)
                rows.append({"key": key, "vary": entry.vary, "status": entry.response.status, "age": round(age), "ttl": round(lifetime - age), "source": source, "etag": header(entry.response.headers, "ETag"), "tags": entry.tags, "hits": entry.hits})
        return rows

    def call_origin(self, method: str, url: str, headers: Headers, body: Optional[bytes] = None) -> Response:
        with self.lock:
            self.origin_requests += 1
        return self.fetch(method, url, headers, body)

    def deliver(self, response: Response, method: str, request_headers: Headers, word: str, params: Sequence[Tuple[str, Any]], age: Optional[float] = None) -> Result:
        headers = without(end_to_end(response.headers), TAG_HEADERS | {"content-length", "x-cache-status"})
        if age is not None:
            headers = replace(headers, "Age", str(max(0, int(age))))
        via = header(headers, "Via")
        headers = replace(headers, "Via", f"{via}, 1.1 {self.name}" if via else f"1.1 {self.name}")
        previous = header(headers, "Cache-Status")
        mine = cache_status_value(self.name, params)
        headers = replace(headers, "Cache-Status", f"{previous}, {mine}" if previous else mine)
        headers.append(("X-Cache-Status", word))
        if not_modified_for(request_headers, response):
            return Result(304, [(k, v) for k, v in headers if k.lower() in NOT_MODIFIED_FIELDS], b"", word)
        headers.append(("Content-Length", str(len(response.body))))
        return Result(response.status, headers, b"" if method == "HEAD" else response.body, word)

    def from_store(self, entry: Entry, method: str, request_headers: Headers, word: str, params: Sequence[Tuple[str, Any]]) -> Result:
        with self.lock:
            entry.hits += 1
        return self.deliver(entry.response, method, request_headers, word, params, age=current_age(entry, self.clock()))

    def gateway_error(self, status: int, message: str, method: str, request_headers: Headers, word: str, params: Sequence[Tuple[str, Any]]) -> Result:
        body = f"{self.name}: {message}\n".encode()
        response = Response(status, [("Content-Type", "text/plain; charset=utf-8"), ("Cache-Control", "no-store")], body)
        return self.deliver(response, method, request_headers, word, list(params) + [("detail", message)])

    def refuse_uncached(self, method: str, request_headers: Headers) -> Result:
        response = Response(504, [("Content-Type", "text/plain; charset=utf-8"), ("Cache-Control", "no-store")], b"only-if-cached: no usable stored response\n")
        return self.deliver(response, method, request_headers, "MISS", [("detail", "only-if-cached")])

    def pass_through(self, method: str, url: str, headers: Headers, body: Optional[bytes]) -> Result:
        try:
            response = self.call_origin(method, url, upstream_headers(headers, keep_conditionals=True), body)
        except UpstreamError as exc:
            return self.gateway_error(504 if exc.kind == "timeout" else 502, f"origin {exc.kind}: {exc}", method, headers, "BYPASS", [("fwd", "method")])
        params: List[Tuple[str, Any]] = [("fwd", "method"), ("fwd-status", response.status)]
        if method not in SAFE_METHODS and 200 <= response.status < 400:
            params.append(("detail", f"invalidated {self.invalidate(url, response.headers)} stored response(s)"))
        return self.deliver(response, method, headers, "BYPASS", params)

    def forward_only(self, method: str, url: str, headers: Headers) -> Result:
        try:
            response = self.call_origin("GET", url, upstream_headers(headers))
        except UpstreamError as exc:
            return self.gateway_error(504 if exc.kind == "timeout" else 502, f"origin {exc.kind}: {exc}", method, headers, "BYPASS", [("fwd", "bypass")])
        return self.deliver(response, method, headers, "BYPASS", [("fwd", "bypass"), ("fwd-status", response.status)])

    def miss(self, method: str, key: str, url: str, headers: Headers) -> Result:
        fwd = "vary-miss" if self.known(key) else "uri-miss"
        with self.lock:
            event = self.inflight.get(key)
            leader = event is None
            if event is None:
                event = threading.Event()
                self.inflight[key] = event
        if leader:
            try:
                return self.fetch_and_store(method, key, url, headers, "MISS", fwd)
            finally:
                with self.lock:
                    self.inflight.pop(key, None)
                event.set()
        event.wait(self.lock_timeout)
        entry = self.lookup(key, headers)
        if entry is not None:
            verdict = assess(entry, headers, self.clock(), self.policy)
            if verdict.action == "hit":
                return self.from_store(entry, method, headers, "HIT", [("fwd", fwd), ("collapsed", True), ("ttl", round(verdict.ttl))])
        return self.fetch_and_store(method, key, url, headers, "MISS", fwd)

    def fetch_and_store(self, method: str, key: str, url: str, headers: Headers, word: str, fwd: str) -> Result:
        request_time = self.clock()
        try:
            response = self.call_origin("GET", url, upstream_headers(headers))
        except UpstreamError as exc:
            return self.gateway_error(504 if exc.kind == "timeout" else 502, f"origin {exc.kind}: {exc}", method, headers, word, [("fwd", fwd)])
        return self.accept(response, method, key, url, headers, word, fwd, request_time)

    def accept(self, response: Response, method: str, key: str, url: str, headers: Headers, word: str, fwd: str, request_time: float, old: Optional[Entry] = None) -> Result:
        response_time = self.clock()
        ok, why = storable("GET", response.status, headers, response.headers, self.policy)
        if not ok:
            if old is not None:
                self.remove(old)
            return self.deliver(response, method, headers, word, [("fwd", fwd), ("fwd-status", response.status), ("detail", why)])
        entry = self.store(key, url, headers, response, request_time, response_time)
        lifetime, _ = entry_lifetime(entry, self.policy)
        ttl = round(lifetime - current_age(entry, response_time))
        return self.deliver(entry.response, method, headers, word, [("fwd", fwd), ("fwd-status", response.status), ("stored", True), ("ttl", ttl)])

    def freshen(self, entry: Entry, not_modified: Response, request_time: float, response_time: float) -> None:
        updates = [(k, v) for k, v in end_to_end(not_modified.headers) if k.lower() not in NOT_UPDATED_BY_304]
        names = {k.lower() for k, _ in updates}
        merged = [(k, v) for k, v in entry.response.headers if k.lower() not in names] + updates
        with self.lock:
            entry.response = Response(entry.response.status, merged, entry.response.body)
            entry.request_time = request_time
            entry.response_time = response_time
            entry.tags = tags_of(merged)

    def revalidate(self, entry: Entry, method: str, url: str, headers: Headers, fwd: str) -> Result:
        request_time = self.clock()
        try:
            response = self.call_origin("GET", url, upstream_headers(headers, validators(entry.response)))
        except UpstreamError as exc:
            return self.on_error(entry, method, headers, fwd, None, exc)
        if response.status == 304:
            self.freshen(entry, response, request_time, self.clock())
            lifetime, _ = entry_lifetime(entry, self.policy)
            ttl = round(lifetime - current_age(entry, self.clock()))
            return self.deliver(entry.response, method, headers, "REVALIDATED", [("fwd", fwd), ("fwd-status", 304), ("ttl", ttl)])
        if response.status in ERROR_STATUSES:
            return self.on_error(entry, method, headers, fwd, response, None)
        return self.accept(response, method, entry.key, url, headers, "EXPIRED", fwd, request_time, old=entry)

    def on_error(self, entry: Entry, method: str, headers: Headers, fwd: str, response: Optional[Response], exc: Optional[UpstreamError]) -> Result:
        now = self.clock()
        allowed, why = stale_on_error(entry, headers, now, self.policy)
        status = response.status if response is not None else None
        if allowed:
            lifetime, _ = entry_lifetime(entry, self.policy)
            ttl = round(lifetime - current_age(entry, now))
            return self.from_store(entry, method, headers, "STALE", [("fwd", fwd), ("fwd-status", status), ("ttl", ttl), ("detail", "stale-if-error")])
        if response is not None:
            return self.deliver(response, method, headers, "EXPIRED", [("fwd", fwd), ("fwd-status", status), ("detail", why)])
        kind = exc.kind if exc is not None else "error"
        cc = response_directives(entry.response.headers, self.policy)
        code = 504 if kind == "timeout" or {"must-revalidate", "proxy-revalidate"} & set(cc) else 502
        return self.gateway_error(code, f"origin {kind}: {exc}; {why}", method, headers, "EXPIRED", [("fwd", fwd)])

    def stale_while_revalidate(self, entry: Entry, method: str, url: str, headers: Headers, verdict: Assessment) -> Result:
        with self.lock:
            busy = id(entry) in self.refreshing
            if not busy:
                self.refreshing.add(id(entry))
        if not busy:
            self.spawn(self.refresh, entry, url, list(headers))
        word = "UPDATING" if busy else "STALE"
        return self.from_store(entry, method, headers, word, [("hit", True), ("ttl", round(verdict.ttl)), ("detail", "stale-while-revalidate")])

    def refresh(self, entry: Entry, url: str, headers: Headers) -> None:
        try:
            self.revalidate(entry, "GET", url, headers, "stale")
        finally:
            with self.lock:
                self.refreshing.discard(id(entry))

    def spawn(self, target: Callable[..., None], *args: Any) -> None:
        thread = threading.Thread(target=target, args=args, daemon=True)
        thread.start()
        with self.lock:
            self.threads = [t for t in self.threads if t.is_alive()] + [thread]

    def join(self, timeout: float = 5.0) -> None:
        with self.lock:
            threads = list(self.threads)
        for thread in threads:
            thread.join(timeout)


def origin_fetcher(origin: str, timeout: float = 10.0) -> Fetch:
    parts = urllib.parse.urlsplit(origin)
    if parts.scheme != "http" or not parts.hostname:
        raise InputError(f"--origin {origin!r}: expected http://HOST:PORT")
    host, port, prefix = parts.hostname, parts.port or 80, parts.path.rstrip("/")

    def fetch(method: str, url: str, headers: Headers, body: Optional[bytes]) -> Response:
        target = urllib.parse.urlsplit(url)
        path = prefix + (target.path or "/") + (f"?{target.query}" if target.query else "")
        outgoing: Dict[str, str] = {"Host": parts.netloc}
        for name, value in headers:
            outgoing[name] = f"{outgoing[name]}, {value}" if name in outgoing else value
        connection = http.client.HTTPConnection(host, port, timeout=timeout)
        try:
            connection.request(method, path, body=body, headers=outgoing)
            reply = connection.getresponse()
            return Response(reply.status, list(reply.getheaders()), reply.read())
        except socket.timeout as exc:
            raise UpstreamError("timeout", f"no answer from {parts.netloc} within {timeout:g}s") from exc
        except (OSError, http.client.HTTPException) as exc:
            raise UpstreamError("connect", f"{parts.netloc}: {exc}") from exc
        finally:
            connection.close()

    return fetch


def has_content_hash(url: str) -> bool:
    parts = urllib.parse.urlsplit(url)
    if {"v", "ver", "version", "hash"} & set(urllib.parse.parse_qs(parts.query)):
        return True
    segment = parts.path.rsplit("/", 1)[-1]
    found = re.search(r"[.\-_]([A-Za-z0-9_]{8,})\.[A-Za-z0-9]{1,8}$", segment)
    return bool(found and re.search(r"[0-9]", found.group(1)) and re.search(r"[A-Za-z]", found.group(1)))


def audience_verdict(status: int, headers: Headers, policy: Policy, received: float) -> Dict[str, Any]:
    ok, why = storable("GET", status, [], headers, policy)
    cc = response_directives(headers, policy)
    lifetime, source = freshness_lifetime(status, headers, policy, received)
    age = float(delta_seconds(header(headers, "Age")) or 0)
    forbidden = staleness_forbidden(cc, policy) if ok else None
    validator = "ETag" if header(headers, "ETag") else "Last-Modified" if header(headers, "Last-Modified") else None
    allowed = ok and not forbidden
    reads = "CDN-Cache-Control" if policy.cdn and header(headers, "CDN-Cache-Control") is not None else "Cache-Control"
    if "no-cache" in cc:
        source = "no-cache"
    elif reads == "CDN-Cache-Control" and source in ("max-age", "s-maxage"):
        source = f"{source} in CDN-Cache-Control"
    return {
        "store": ok,
        "why": why,
        "reads": reads,
        "lifetime": int(lifetime) if ok else 0,
        "source": source if ok else "-",
        "remaining": max(0, int(lifetime - age)) if ok else 0,
        "revalidate_every_use": ok and "no-cache" in cc,
        "after_expiry": (f"revalidate with {validator}" if validator else "download again, no validator") if ok else "-",
        "stale_while_revalidate": (delta_seconds(cc.get("stale-while-revalidate")) or 0) if allowed else 0,
        "stale_if_error": (delta_seconds(cc.get("stale-if-error")) or 0) if allowed else 0,
        "stale_forbidden_by": forbidden,
        "key": ["method", "URL"] + vary_names(headers),
    }


def nginx_verdict(status: int, headers: Headers, now: float) -> Dict[str, Any]:
    cc = (header(headers, "Cache-Control") or "").lower()
    validator = "ETag" if header(headers, "ETag") else "Last-Modified" if header(headers, "Last-Modified") else None
    base: Dict[str, Any] = {"store": False, "why": "", "reads": "X-Accel-Expires, Cache-Control, Expires", "lifetime": 0, "source": "-", "remaining": 0, "revalidate_every_use": False, "after_expiry": "-", "stale_while_revalidate": 0, "stale_if_error": 0, "stale_forbidden_by": None, "key": ["$scheme$proxy_host$request_uri"] + vary_names(headers)}

    def refuse(why: str) -> Dict[str, Any]:
        return dict(base, why=why)

    if header(headers, "Set-Cookie") is not None:
        return refuse("Set-Cookie, unless proxy_ignore_headers Set-Cookie")
    vary = header(headers, "Vary")
    if vary is not None and vary.strip() == "*":
        return refuse("Vary: *")
    if vary is not None and len(vary) > 128:
        return refuse("Vary longer than 128 bytes")
    lifetime: Optional[float] = None
    source = ""
    accel = (header(headers, "X-Accel-Expires") or "").strip()
    if accel.startswith("@") and accel[1:].isdigit():
        lifetime, source = float(int(accel[1:]) - now), "X-Accel-Expires @time"
    elif accel.isdigit():
        if int(accel) == 0:
            return refuse("X-Accel-Expires: 0")
        lifetime, source = float(accel), "X-Accel-Expires"
    if lifetime is None:
        if any(word in cc for word in ("no-cache", "no-store", "private")):
            return refuse("Cache-Control contains no-cache, no-store or private")
        for name in ("s-maxage", "max-age"):
            found = re.search(re.escape(name) + r"=([^,;\s]*)", cc)
            if found:
                if not found.group(1).isdigit():
                    return refuse(f"{name} is not a number")
                if int(found.group(1)) == 0:
                    return refuse(f"{name}=0")
                lifetime, source = float(found.group(1)), name
                break
    if lifetime is None:
        expires_text = header(headers, "Expires")
        if expires_text is not None:
            expires = parse_http_date(expires_text)
            if expires is None or expires < now:
                return refuse("Expires is invalid or in the past")
            lifetime, source = expires - now, "Expires vs nginx's clock"
    if lifetime is None:
        return refuse("no X-Accel-Expires, max-age, s-maxage or Expires; only proxy_cache_valid would store it")
    swr = re.search(r"stale-while-revalidate=(\d+)", cc)
    sie = re.search(r"stale-if-error=(\d+)", cc)
    updating = int(swr.group(1)) if swr else 0
    return dict(
        base,
        store=True,
        why="explicit lifetime",
        lifetime=int(lifetime),
        source=source,
        remaining=int(lifetime),
        after_expiry=f"revalidate with {validator} if proxy_cache_revalidate on" if validator else "download again, no validator",
        stale_while_revalidate=updating,
        stale_if_error=int(sie.group(1)) if sie else updating,
    )


def notes_for(status: int, headers: Headers, url: str, now: float) -> List[Dict[str, str]]:
    found: List[Dict[str, str]] = []

    def note(code: str, text: str) -> None:
        found.append({"code": code, "text": text})

    cc = parse_cache_control(header(headers, "Cache-Control"))
    shared_ok, _ = storable("GET", status, [], headers, Policy(shared=True, refuse_set_cookie=False))
    explicit = {"max-age", "s-maxage"} & set(cc) or header(headers, "Expires") is not None
    if header(headers, "Set-Cookie") is not None and shared_ok:
        note("set-cookie", "Set-Cookie on a response a shared cache may store: a cache that stores it replays this cookie to the next user, and nginx and several CDNs refuse to store it, so it is a permanent miss there. Mark it private or set the cookie on a different response.")
    if "no-store" not in cc and not header(headers, "ETag") and not header(headers, "Last-Modified"):
        note("no-validator", "No ETag or Last-Modified: once a copy expires, every cache downloads the whole body again instead of getting a 304.")
    if not explicit and "no-store" not in cc and "no-cache" not in cc and status in HEURISTIC_STATUSES:
        lifetime, _ = freshness_lifetime(status, headers, Policy(), now)
        if lifetime > 0:
            note("heuristic", f"No explicit lifetime: RFC 9111 caches may guess {int(lifetime)}s (10% of the time since Last-Modified), nginx without proxy_cache_valid does not store it at all. Say what you mean with max-age.")
        else:
            note("no-lifetime", "No explicit lifetime and no Last-Modified: caches may store it but must revalidate before any reuse.")
    if "max-age" in cc and header(headers, "Expires") is not None:
        note("expires-ignored", "Expires is ignored because max-age is present; only HTTP/1.0 caches read Expires.")
    if header(headers, "Pragma") is not None:
        note("pragma", "Pragma in a response means nothing to an HTTP/1.1 cache; RFC 9111 deprecates it. Use Cache-Control.")
    names = vary_names(headers)
    if "*" in names:
        note("vary-star", "Vary: * makes every stored copy unusable for any later request.")
    fragmenting = sorted(FRAGMENTING_VARY & set(names))
    if fragmenting:
        note("vary-fragments", f"Vary: {', '.join(fragmenting)} stores one copy per distinct value; the hit ratio falls towards zero. Vary on a normalised header your edge computes instead.")
    if "no-cache" in cc and "no-store" not in cc:
        note("no-cache", "no-cache stores the response and revalidates it before every use; it does not keep it out of caches (that is no-store). nginx treats no-cache as do-not-store.")
    if "private" in cc and "s-maxage" in cc:
        note("private-s-maxage", "private wins: no shared cache stores it, so s-maxage has no effect.")
    lifetime = delta_seconds(cc.get("s-maxage")) or delta_seconds(cc.get("max-age")) or 0
    hashed = bool(url) and has_content_hash(url)
    if url and not hashed and lifetime >= DAY and "private" not in cc:
        note("long-unhashed", f"{lifetime}s of freshness on a URL without a content hash: after a deploy every cache keeps the old file until it expires. Put a hash in the file name, or use a short max-age with a validator.")
    if hashed and (lifetime < 30 * DAY or "immutable" not in cc):
        note("hashed-short", "The file name carries a content hash, so this URL's content never changes: Cache-Control: public, max-age=31536000, immutable is safe and saves every revalidation.")
    age = delta_seconds(header(headers, "Age"))
    if age:
        note("age", f"Age: {age}: this copy came through a cache upstream; {max(0, lifetime - age)}s of freshness are left, not {lifetime}s.")
    if header(headers, "CDN-Cache-Control") is not None:
        note("cdn-cache-control", "CDN-Cache-Control is used instead of Cache-Control by CDNs that implement RFC 9213; browsers and other proxies ignore it.")
    tags = tags_of(headers)
    if tags:
        note("tags", f"Tags {', '.join(tags)}: a CDN that reads Surrogate-Key or Cache-Tag can purge every URL carrying a tag in one call.")
    return found


AUDIENCES: Tuple[Tuple[str, Policy], ...] = (
    ("browser", Policy(shared=False, refuse_set_cookie=False)),
    ("shared", Policy(shared=True, refuse_set_cookie=False)),
    ("cdn", Policy(shared=True, cdn=True)),
)


def explain(status: int, headers: Headers, url: str = "", now: Optional[float] = None) -> Dict[str, Any]:
    now = time.time() if now is None else now
    audiences = {name: audience_verdict(status, headers, policy, now) for name, policy in AUDIENCES}
    audiences["nginx"] = nginx_verdict(status, headers, now)
    shown = {name: header(headers, name) for name in SHOWN_HEADERS if header(headers, name) is not None}
    return {"url": url, "status": status, "headers": shown, "audiences": audiences, "notes": notes_for(status, headers, url, now)}


def render_check(report: Dict[str, Any]) -> str:
    lines = [f"{report['url'] or '(response headers)'}  status {report['status']}"]
    lines += [f"  {name}: {value}" for name, value in report["headers"].items()]
    lines.append("")
    lines.append(f"  {'audience':<8} {'store':<5} {'fresh':>9}  {'from':<30} {'after expiry':<50} stale allowed")
    for name, verdict in report["audiences"].items():
        if not verdict["store"]:
            lines.append(f"  {name:<8} {'no':<5} {'-':>9}  {verdict['why']}")
            continue
        fresh = "0s" if verdict["revalidate_every_use"] else f"{verdict['remaining']}s"
        after = "revalidate every use" if verdict["revalidate_every_use"] else verdict["after_expiry"]
        stale = []
        if verdict["stale_while_revalidate"]:
            stale.append(f"{verdict['stale_while_revalidate']}s while revalidating")
        if verdict["stale_if_error"]:
            stale.append(f"{verdict['stale_if_error']}s on error")
        if verdict["stale_forbidden_by"]:
            stale = [f"never ({verdict['stale_forbidden_by']})"]
        lines.append(f"  {name:<8} {'yes':<5} {fresh:>9}  {verdict['source']:<30} {after:<50} {', '.join(stale) or 'no'}")
    if report["notes"]:
        lines.append("")
        lines.append("notes:")
        lines += [f"  - [{n['code']}] {n['text']}" for n in report["notes"]]
    return "\n".join(lines)


def parse_header_blocks(text: str) -> List[Tuple[int, Headers]]:
    blocks: List[Tuple[int, Headers]] = []
    status: Optional[int] = None
    headers: Headers = []
    for number, raw in enumerate(text.splitlines(), 1):
        line = raw.rstrip("\r")
        if line.startswith(("* ", "> ", "{ ", "} ")) or line.strip() in ("*", ">", "<"):
            continue
        if line.startswith("< "):
            line = line[2:]
        if not line.strip():
            continue
        status_line = re.match(r"^HTTP/\d(?:\.\d)?\s+(\d{3})\b", line)
        if status_line:
            if status is not None or headers:
                blocks.append((status or 200, headers))
            status, headers = int(status_line.group(1)), []
            continue
        name, sep, value = line.partition(":")
        if not sep or not name.strip() or " " in name.strip():
            raise InputError(f"line {number}: not a header line: {line!r}")
        headers.append((name.strip(), value.strip()))
    if status is not None or headers:
        blocks.append((status or 200, headers))
    if not blocks:
        raise InputError("no response headers found (expected the output of curl -sI or curl -sv)")
    return blocks


def parse_header_args(values: Sequence[str]) -> Headers:
    parsed: Headers = []
    for value in values:
        name, sep, rest = value.partition(":")
        if not sep or not name.strip():
            raise InputError(f"-H {value!r}: expected 'Name: value'")
        parsed.append((name.strip(), rest.strip()))
    return parsed


def fetch_headers(url: str, request_headers: Headers) -> Tuple[int, Headers, str]:
    request = urllib.request.Request(url, headers=dict(request_headers), method="GET")
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(request, timeout=10) as reply:
            reply.read()
            return reply.status, list(reply.getheaders()), reply.geturl()
    except urllib.error.HTTPError as err:
        return err.code, list(err.headers.items()), url
    except (urllib.error.URLError, OSError) as err:
        raise OSError(f"{url}: {getattr(err, 'reason', err)}") from err


def read_text(path: str) -> str:
    try:
        with open(path, encoding="utf-8") as handle:
            return handle.read()
    except OSError as exc:
        raise InputError(f"{path}: {exc.strerror or exc}") from exc


def load_routes(path: str) -> Dict[str, Dict[str, Any]]:
    try:
        data = json.loads(read_text(path))
    except json.JSONDecodeError as exc:
        raise InputError(f"{path}: not JSON: line {exc.lineno}: {exc.msg}") from exc
    if not isinstance(data, dict):
        raise InputError(f"{path}: expected an object of scenario name -> route")
    for name, route in data.items():
        if not isinstance(route, dict):
            raise InputError(f"{path}: route {name!r} must be an object")
        for key, value in route.items():
            if key not in ROUTE_FIELDS:
                raise InputError(f"{path}: route {name!r}: unknown field {key!r} (known: {', '.join(sorted(ROUTE_FIELDS))})")
            if not isinstance(value, ROUTE_FIELDS[key]) or isinstance(value, bool) != (ROUTE_FIELDS[key] is bool):
                raise InputError(f"{path}: route {name!r}: {key} has the wrong type")
    return data


class OriginServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address: Tuple[str, int], routes: Optional[Dict[str, Dict[str, Any]]] = None) -> None:
        super().__init__(address, OriginHandler)
        self.routes = dict(DEFAULT_ROUTES if routes is None else routes)
        self.started = int(time.time())
        self.versions: Dict[str, int] = {}
        self.down = False
        self.lock = threading.Lock()
        self.requests: Dict[str, List[Dict[str, Any]]] = {}

    def record(self, path: str, row: Dict[str, Any]) -> None:
        with self.lock:
            self.requests.setdefault(path, []).append(row)

    def stats(self) -> Dict[str, Any]:
        with self.lock:
            paths = {path: {"count": len(rows), "requests": list(rows)} for path, rows in sorted(self.requests.items())}
            return {"down": self.down, "versions": dict(self.versions), "paths": paths}


class OriginHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server: OriginServer

    def reply(self, status: int, headers: Headers, body: bytes = b"") -> None:
        self.send_response_only(status)
        for name, value in headers:
            self.send_header(name, value)
        if status != 304:
            self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if self.command != "HEAD" and status != 304:
            self.wfile.write(body)

    def answer(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        if length:
            self.rfile.read(length)
        server = self.server
        path = urllib.parse.urlsplit(self.path).path
        if path.startswith("/__origin/"):
            self.admin(path[len("/__origin/"):])
            return
        scenario = path.rstrip("/").rsplit("/", 1)[-1]
        route = server.routes.get(scenario)
        inm, ims = self.headers.get("If-None-Match"), self.headers.get("If-Modified-Since")
        row: Dict[str, Any] = {"method": self.command, "time": round(time.time(), 3), "if_none_match": inm, "if_modified_since": ims, "accept_language": self.headers.get("Accept-Language")}
        plain = [("Date", http_date(time.time())), ("Content-Type", "text/plain; charset=utf-8"), ("Cache-Control", "no-store")]
        if server.down:
            status, headers, body = 503, plain, b"origin is down\n"
        elif route is None:
            status, headers, body = 404, plain, f"no scenario {scenario!r}\n".encode()
        elif self.command not in ("GET", "HEAD"):
            with server.lock:
                server.versions[scenario] = server.versions.get(scenario, 1) + 1
            status, headers, body = 200, plain, f"{scenario} updated to v{server.versions[scenario]}\n".encode()
        else:
            time.sleep(float(route.get("delay", 0)))
            status, headers, body = self.scenario(scenario, route, inm, ims)
        row["status"] = status
        server.record(path, row)
        self.reply(status, headers, body)

    def scenario(self, name: str, route: Dict[str, Any], inm: Optional[str], ims: Optional[str]) -> Tuple[int, Headers, bytes]:
        server = self.server
        version = server.versions.get(name, 1)
        etag = f'"{name}-v{version}"'
        last_modified = http_date(server.started - int(route.get("last_modified_age", 3600)))
        headers: Headers = [("Date", http_date(time.time())), ("Last-Modified", last_modified), ("Content-Type", "text/plain; charset=utf-8")]
        if route.get("etag", True):
            headers.append(("ETag", etag))
        if route.get("cache_control"):
            headers.append(("Cache-Control", route["cache_control"]))
        if route.get("vary"):
            headers.append(("Vary", route["vary"]))
        headers += [(str(k), str(v)) for k, v in (route.get("headers") or {}).items()]
        if inm is not None:
            unchanged = route.get("etag", True) and etag_matches(inm, etag)
        else:
            since = parse_http_date(ims)
            unchanged = since is not None and parse_http_date(last_modified) <= since
        if unchanged:
            return 304, [(k, v) for k, v in headers if k.lower() != "content-type"], b""
        language = self.headers.get("Accept-Language") or "-"
        return 200, headers, f"{name} v{version} lang={language}\n".encode()

    def admin(self, action: str) -> None:
        server = self.server
        verb, _, argument = action.partition("/")
        with server.lock:
            if verb == "down":
                server.down = True
            elif verb == "up":
                server.down = False
            elif verb == "reset":
                server.requests.clear()
            elif verb == "bump" and argument in server.routes:
                server.versions[argument] = server.versions.get(argument, 1) + 1
            elif verb != "stats":
                self.reply(404, [("Content-Type", "application/json")], b'{"error": "unknown origin admin action"}\n')
                return
        payload = (json.dumps(server.stats(), indent=2) + "\n").encode()
        self.reply(200, [("Content-Type", "application/json"), ("Cache-Control", "no-store")], payload)

    do_GET = do_HEAD = do_POST = do_PUT = do_DELETE = do_PATCH = answer

    def log_message(self, format: str, *args: Any) -> None:
        return


def json_result(status: int, payload: Dict[str, Any]) -> Result:
    body = (json.dumps(payload, indent=2) + "\n").encode()
    return Result(status, [("Content-Type", "application/json"), ("Cache-Control", "no-store"), ("Content-Length", str(len(body)))], body, "BYPASS")


class CacheServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address: Tuple[str, int], cache: Cache, verbose: bool = False) -> None:
        super().__init__(address, CacheHandler)
        self.cache = cache
        self.verbose = verbose


class CacheHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server: CacheServer

    def handle_any(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else None
        host = self.headers.get("Host") or "%s:%d" % self.server.server_address[:2]
        url = f"http://{host}{self.path}"
        if self.command == "PURGE" or self.path.startswith("/.minicache/"):
            result = self.admin(url)
        else:
            result = self.server.cache.handle(self.command, url, list(self.headers.items()), body)
        self.send_response_only(result.status)
        for name, value in result.headers:
            self.send_header(name, value)
        self.end_headers()
        if result.body and self.command != "HEAD":
            self.wfile.write(result.body)
        if self.server.verbose:
            print(f"[{NAME}] {self.command} {self.path} {result.status} {result.cache_status}", file=sys.stderr, flush=True)

    def admin(self, url: str) -> Result:
        if self.client_address[0] not in LOOPBACK:
            return json_result(403, {"error": "purge and admin endpoints answer loopback clients only"})
        cache = self.server.cache
        path = urllib.parse.urlsplit(url).path
        if self.command == "PURGE" and path.startswith("/.minicache/tag/"):
            tag = urllib.parse.unquote(path[len("/.minicache/tag/"):])
            count = cache.purge_tag(tag)
            return json_result(200 if count else 404, {"tag": tag, "purged": count})
        if self.command == "PURGE":
            count = cache.purge_url(url)
            return json_result(200 if count else 404, {"url": url, "purged": count})
        if path == "/.minicache/entries":
            return json_result(200, {"policy": cache.policy.label, "origin_requests": cache.origin_requests, "entries": cache.snapshot()})
        return json_result(404, {"error": f"unknown admin path {path}"})

    do_GET = do_HEAD = do_POST = do_PUT = do_DELETE = do_PATCH = do_OPTIONS = do_PURGE = handle_any

    def log_message(self, format: str, *args: Any) -> None:
        return


def announce(server: ThreadingHTTPServer, port_file: Optional[str], what: str) -> None:
    host, port = server.server_address[:2]
    if port_file:
        with open(port_file + ".tmp", "w", encoding="utf-8") as handle:
            handle.write(f"{port}\n")
        os.replace(port_file + ".tmp", port_file)
    print(f"{what} listening on http://{host}:{port}", flush=True)


def run_forever(server: ThreadingHTTPServer) -> int:
    try:
        server.serve_forever(poll_interval=0.2)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


def render_routes(routes: Dict[str, Dict[str, Any]]) -> str:
    lines = [f"{'scenario':<18} {'Cache-Control':<40} extra"]
    for name, route in routes.items():
        extra = []
        if route.get("vary"):
            extra.append(f"Vary: {route['vary']}")
        if route.get("delay"):
            extra.append(f"delay {route['delay']}s")
        extra += [f"{k}: {v}" for k, v in (route.get("headers") or {}).items()]
        if "last_modified_age" in route:
            extra.append(f"Last-Modified {route['last_modified_age']}s before start")
        lines.append(f"{name:<18} {route.get('cache_control') or '(none)':<40} {'; '.join(extra)}")
    return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog=NAME, description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    serve = sub.add_parser("serve", help="run the caching reverse proxy in front of an origin")
    serve.add_argument("--origin", required=True, help="http://HOST:PORT of the origin")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8080, help="0 picks a free port")
    serve.add_argument("--port-file", help="write the bound port to this file once listening")
    serve.add_argument("--private", action="store_true", help="behave as a private (browser) cache: store private responses, ignore s-maxage")
    serve.add_argument("--cdn", action="store_true", help="read CDN-Cache-Control instead of Cache-Control when present (RFC 9213)")
    serve.add_argument("--lock-timeout", type=float, default=5.0, help="how long concurrent misses wait for the first one, like proxy_cache_lock_timeout")
    serve.add_argument("--timeout", type=float, default=10.0, help="origin connect and read timeout in seconds")
    serve.add_argument("--heuristic", type=float, default=0.1, help="fraction of Date - Last-Modified used as heuristic freshness")
    serve.add_argument("--verbose", action="store_true", help="one stderr line per request")
    origin = sub.add_parser("origin", help="a test origin that counts requests and answers each scenario with its cache headers")
    origin.add_argument("--host", default="127.0.0.1")
    origin.add_argument("--port", type=int, default=9000, help="0 picks a free port")
    origin.add_argument("--port-file", help="write the bound port to this file once listening")
    origin.add_argument("--routes", help="JSON file of scenario -> {cache_control, vary, delay, headers, etag, last_modified_age}, merged over the built-in ones")
    origin.add_argument("--list", action="store_true", help="print the scenarios and exit")
    origin.add_argument("--json", action="store_true", help="with --list, print JSON")
    check = sub.add_parser("check", help="explain how a browser, a shared cache, a CDN and nginx treat a response")
    check.add_argument("target", help="a URL to fetch, a file of response headers (curl -sI or curl -sv output), or - for stdin")
    check.add_argument("-H", "--header", action="append", default=[], help="'Name: value' request header for a URL target, repeatable")
    check.add_argument("--url", default="", help="the URL a header file belongs to, for the content-hash checks")
    check.add_argument("--json", action="store_true", help="machine-readable report")
    return parser


def cmd_check(args: argparse.Namespace) -> int:
    request_headers = parse_header_args(args.header)
    if re.match(r"^https?://", args.target):
        status, headers, url = fetch_headers(args.target, request_headers)
    else:
        text = sys.stdin.read() if args.target == "-" else read_text(args.target)
        status, headers = parse_header_blocks(text)[-1]
        url = args.url
    report = explain(status, headers, url)
    print(json.dumps(report, indent=2) if args.json else render_check(report))
    return 0


def cmd_origin(args: argparse.Namespace) -> int:
    routes = dict(DEFAULT_ROUTES)
    if args.routes:
        routes.update(load_routes(args.routes))
    if args.list:
        print(json.dumps(routes, indent=2) if args.json else render_routes(routes))
        return 0
    server = OriginServer((args.host, args.port), routes)
    announce(server, args.port_file, "origin")
    return run_forever(server)


def cmd_serve(args: argparse.Namespace) -> int:
    if args.private and args.cdn:
        raise InputError("--private and --cdn exclude each other: a CDN is a shared cache")
    if not 0 <= args.heuristic <= 1:
        raise InputError("--heuristic must be a fraction between 0 and 1")
    policy = Policy(shared=not args.private, cdn=args.cdn, heuristic_fraction=args.heuristic, refuse_set_cookie=not args.private)
    cache = Cache(origin_fetcher(args.origin, args.timeout), policy, lock_timeout=args.lock_timeout)
    server = CacheServer((args.host, args.port), cache, args.verbose)
    announce(server, args.port_file, f"{NAME} ({policy.label}) for {args.origin}")
    return run_forever(server)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(list(sys.argv[1:] if argv is None else argv))
    try:
        if args.command == "check":
            return cmd_check(args)
        if args.command == "origin":
            return cmd_origin(args)
        return cmd_serve(args)
    except InputError as exc:
        print(f"{NAME}: {exc}", file=sys.stderr)
        return 2
    except OSError as exc:
        print(f"{NAME}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
