"""minitrace: W3C Trace Context and baggage, a tracer with OTLP/HTTP JSON export, three small HTTP services that propagate context, and a collector that assembles traces, head- and tail-samples them and finds each trace's critical path and self time per service."""
from __future__ import annotations

import argparse
import gzip
import json
import math
import random
import re
import signal
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field, replace
from fractions import Fraction
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple

VERSION = "1.0.0"
TRACEPARENT_FORMAT = re.compile(r"^([0-9a-f]{2})-([0-9a-f]{32})-([0-9a-f]{16})-([0-9a-f]{2})(-.*)?$")
INVALID_TRACE_ID = "0" * 32
INVALID_SPAN_ID = "0" * 16
FLAG_SAMPLED = 0x01
TRACESTATE_MAX_MEMBERS = 32
TRACESTATE_LONG_MEMBER = 128
TRACESTATE_KEY = re.compile(r"^(?:[a-z][a-z0-9_\-*/]{0,255}|[a-z0-9][a-z0-9_\-*/]{0,240}@[a-z][a-z0-9_\-*/]{0,13})$")
TRACESTATE_VALUE = re.compile(r"^[\x20-\x2b\x2d-\x3c\x3e-\x7e]{0,255}[\x21-\x2b\x2d-\x3c\x3e-\x7e]$")
BAGGAGE_KEY = re.compile(r"^[!#$%&'*+\-.^_`|~0-9A-Za-z]+$")
BAGGAGE_VALUE = re.compile(r"^[\x21\x23-\x2b\x2d-\x3a\x3c-\x5b\x5d-\x7e]*$")
BAGGAGE_SAFE = "!#$&'()*+-./:<=>?@[]^_`{|}~"
BAGGAGE_MAX_MEMBERS = 64
BAGGAGE_MAX_BYTES = 8192

KIND_UNSPECIFIED, KIND_INTERNAL, KIND_SERVER, KIND_CLIENT, KIND_PRODUCER, KIND_CONSUMER = range(6)
KIND_NAMES = {0: "UNSPECIFIED", 1: "INTERNAL", 2: "SERVER", 3: "CLIENT", 4: "PRODUCER", 5: "CONSUMER"}
STATUS_UNSET, STATUS_OK, STATUS_ERROR = 0, 1, 2
STATUS_NAMES = {0: "UNSET", 1: "OK", 2: "ERROR"}

MAX_UINT64 = (1 << 64) - 1
FNV_OFFSET_64 = 0xCBF29CE484222325
FNV_PRIME_64 = 0x100000001B3
DEFAULT_HASH_SALT = "default-hash-seed"
RETRYABLE = (0, 429, 502, 503, 504)
SAMPLER_NAMES = ("always_on", "always_off", "traceidratio", "parentbased_always_on", "parentbased_always_off", "parentbased_traceidratio")

FAULTS = ("db-slow", "db-error", "api-slow")
DEFAULT_DELAYS_MS = {"db": 5.0, "score": 15.0, "db-slow": 400.0, "api-slow": 350.0}
DEFAULT_PATTERN = "ok,db-slow,ok,ok,db-error,ok,api-slow,ok,ok,unsampled+db-error"
BAR = "█"


class ContextError(ValueError):
    pass


@dataclass(frozen=True)
class SpanContext:
    trace_id: str
    span_id: str
    flags: int = FLAG_SAMPLED
    tracestate: Tuple[Tuple[str, str], ...] = ()
    remote: bool = False

    @property
    def sampled(self) -> bool:
        return bool(self.flags & FLAG_SAMPLED)


@dataclass(frozen=True)
class BaggageEntry:
    key: str
    value: str
    properties: Tuple[str, ...] = ()


def parse_traceparent(header: str) -> SpanContext:
    value = header.strip(" \t")
    match = TRACEPARENT_FORMAT.match(value)
    if not match:
        if TRACEPARENT_FORMAT.match(value.lower()):
            raise ContextError(f"traceparent {header!r} uses uppercase hex; only lowercase is valid")
        raise ContextError(f"traceparent {header!r} is not VERSION-TRACEID-PARENTID-FLAGS (2, 32, 16 and 2 lowercase hex digits)")
    version, trace_id, span_id, flags, rest = match.groups()
    if version == "ff":
        raise ContextError("traceparent version ff is forbidden")
    if version == "00" and rest:
        raise ContextError("traceparent version 00 has exactly four fields; found more after the flags")
    if trace_id == INVALID_TRACE_ID:
        raise ContextError("trace-id is all zeros, which is invalid")
    if span_id == INVALID_SPAN_ID:
        raise ContextError("parent-id is all zeros, which is invalid")
    return SpanContext(trace_id, span_id, int(flags, 16), (), True)


def format_traceparent(context: SpanContext) -> str:
    return f"00-{context.trace_id}-{context.span_id}-{context.flags & FLAG_SAMPLED:02x}"


def parse_tracestate(headers: Sequence[str]) -> List[Tuple[str, str]]:
    entries: List[Tuple[str, str]] = []
    seen = set()
    for member in ",".join(headers).split(","):
        member = member.strip(" \t")
        if not member:
            continue
        key, sep, value = member.partition("=")
        if not sep or not TRACESTATE_KEY.match(key) or not TRACESTATE_VALUE.match(value):
            raise ContextError(f"tracestate member {member!r} is not key=value with a valid key and value")
        if key in seen:
            raise ContextError(f"tracestate key {key!r} appears twice")
        seen.add(key)
        entries.append((key, value))
    if len(entries) > TRACESTATE_MAX_MEMBERS:
        raise ContextError(f"tracestate has {len(entries)} members; the limit is {TRACESTATE_MAX_MEMBERS}")
    return entries


def update_tracestate(entries: Sequence[Tuple[str, str]], key: str, value: str) -> List[Tuple[str, str]]:
    if not TRACESTATE_KEY.match(key) or not TRACESTATE_VALUE.match(value):
        raise ContextError(f"cannot set tracestate {key}={value}: invalid key or value")
    updated = [(key, value)] + [(k, v) for k, v in entries if k != key]
    while len(updated) > TRACESTATE_MAX_MEMBERS:
        long_members = [i for i, (k, v) in enumerate(updated) if i > 0 and len(k) + 1 + len(v) > TRACESTATE_LONG_MEMBER]
        updated.pop(long_members[-1] if long_members else -1)
    return updated


def format_tracestate(entries: Iterable[Tuple[str, str]]) -> str:
    return ",".join(f"{key}={value}" for key, value in entries)


def parse_baggage(headers: Sequence[str], problems: Optional[List[str]] = None) -> Dict[str, BaggageEntry]:
    entries: Dict[str, BaggageEntry] = {}
    for member in ",".join(headers).split(","):
        if not member.strip(" \t"):
            continue
        parts = [part.strip(" \t") for part in member.split(";")]
        key, sep, value = parts[0].partition("=")
        key, value = key.strip(" \t"), value.strip(" \t")
        if not sep or not BAGGAGE_KEY.match(key) or not BAGGAGE_VALUE.match(value):
            if problems is not None:
                problems.append(f"baggage member {member.strip()!r} dropped: needs token=value with value characters percent-encoded")
            continue
        entries[key] = BaggageEntry(key, urllib.parse.unquote(value, errors="replace"), tuple(part for part in parts[1:] if part))
    return entries


def format_baggage(entries: Iterable[BaggageEntry]) -> str:
    members: List[str] = []
    size = 0
    for entry in entries:
        text = f"{entry.key}={urllib.parse.quote(entry.value, safe=BAGGAGE_SAFE)}" + "".join(";" + prop for prop in entry.properties)
        added = len(text.encode("utf-8")) + (1 if members else 0)
        if len(members) == BAGGAGE_MAX_MEMBERS or size + added > BAGGAGE_MAX_BYTES:
            continue
        members.append(text)
        size += added
    return ",".join(members)


@dataclass
class Extracted:
    parent: Optional[SpanContext]
    baggage: Dict[str, BaggageEntry]
    problems: List[str]


def extract(headers: Dict[str, List[str]]) -> Extracted:
    problems: List[str] = []
    parent: Optional[SpanContext] = None
    values = headers.get("traceparent") or []
    if values:
        try:
            parent = parse_traceparent(values[0])
        except ContextError as exc:
            problems.append(f"{exc}; starting a new trace")
    if parent is not None and headers.get("tracestate"):
        try:
            parent = replace(parent, tracestate=tuple(parse_tracestate(headers["tracestate"])))
        except ContextError as exc:
            problems.append(f"{exc}; tracestate dropped")
    baggage = parse_baggage(headers.get("baggage") or [], problems)
    return Extracted(parent, baggage, problems)


def inject(context: SpanContext, baggage: Dict[str, BaggageEntry], headers: Optional[Dict[str, str]] = None) -> Dict[str, str]:
    headers = dict(headers or {})
    headers["traceparent"] = format_traceparent(context)
    if context.tracestate:
        headers["tracestate"] = format_tracestate(context.tracestate)
    text = format_baggage(baggage.values())
    if text:
        headers["baggage"] = text
    return headers


class IdGenerator:
    def __init__(self, seed: Optional[int] = None) -> None:
        self.rng: Any = random.Random(seed) if seed is not None else random.SystemRandom()
        self.lock = threading.Lock()

    def trace_id(self) -> str:
        return self._hex(16)

    def span_id(self) -> str:
        return self._hex(8)

    def _hex(self, nbytes: int) -> str:
        with self.lock:
            while True:
                value = self.rng.getrandbits(nbytes * 8)
                if value:
                    return format(value, f"0{nbytes * 2}x")


Sampler = Callable[[Optional[SpanContext], str], bool]


def always_on(parent: Optional[SpanContext], trace_id: str) -> bool:
    return True


def always_off(parent: Optional[SpanContext], trace_id: str) -> bool:
    return False


def trace_id_ratio(ratio: float) -> Sampler:
    if not 0.0 <= ratio <= 1.0:
        raise ValueError(f"sampling ratio must be between 0 and 1, got {ratio}")
    bound = round(ratio * (MAX_UINT64 + 1))

    def sample(parent: Optional[SpanContext], trace_id: str) -> bool:
        return int(trace_id[16:], 16) < bound

    return sample


def parent_based(root: Sampler) -> Sampler:
    def sample(parent: Optional[SpanContext], trace_id: str) -> bool:
        if parent is None:
            return root(parent, trace_id)
        return parent.sampled

    return sample


def make_sampler(name: str, arg: float = 1.0) -> Sampler:
    roots: Dict[str, Callable[[], Sampler]] = {"always_on": lambda: always_on, "always_off": lambda: always_off, "traceidratio": lambda: trace_id_ratio(arg)}
    base = name[len("parentbased_"):] if name.startswith("parentbased_") else name
    if base not in roots:
        raise ValueError(f"unknown sampler {name!r}; use one of {', '.join(SAMPLER_NAMES)} (the OTEL_TRACES_SAMPLER names)")
    root = roots[base]()
    return parent_based(root) if name.startswith("parentbased_") else root


@dataclass
class Event:
    name: str
    time_ns: int
    attributes: Dict[str, Any] = field(default_factory=dict)


@dataclass
class Link:
    trace_id: str
    span_id: str
    tracestate: str = ""
    attributes: Dict[str, Any] = field(default_factory=dict)


@dataclass
class Span:
    name: str
    context: SpanContext
    parent_span_id: Optional[str]
    kind: int
    start_ns: int
    end_ns: int = 0
    service: str = ""
    attributes: Dict[str, Any] = field(default_factory=dict)
    events: List[Event] = field(default_factory=list)
    links: List[Link] = field(default_factory=list)
    status_code: int = STATUS_UNSET
    status_message: str = ""
    recording: bool = True
    on_end: Optional[Callable[["Span"], None]] = field(default=None, repr=False, compare=False)
    clock: Callable[[], int] = field(default=time.time_ns, repr=False, compare=False)

    @property
    def trace_id(self) -> str:
        return self.context.trace_id

    @property
    def span_id(self) -> str:
        return self.context.span_id

    @property
    def duration_ns(self) -> int:
        return max(0, self.end_ns - self.start_ns)

    def set_attribute(self, key: str, value: Any) -> None:
        if self.recording and not self.end_ns:
            self.attributes[key] = value

    def add_event(self, name: str, attributes: Optional[Dict[str, Any]] = None, time_ns: Optional[int] = None) -> None:
        if self.recording and not self.end_ns:
            self.events.append(Event(name, time_ns or self.clock(), dict(attributes or {})))

    def set_status(self, code: int, message: str = "") -> None:
        if self.status_code == STATUS_OK or code == STATUS_UNSET or self.end_ns:
            return
        self.status_code = code
        self.status_message = message if code == STATUS_ERROR else ""

    def record_exception(self, exc: BaseException) -> None:
        self.add_event("exception", {"exception.type": type(exc).__name__, "exception.message": str(exc)})

    def end(self, end_ns: Optional[int] = None) -> None:
        if self.end_ns:
            return
        self.end_ns = max(end_ns or self.clock(), self.start_ns)
        if self.recording and self.on_end is not None:
            self.on_end(self)


class Tracer:
    def __init__(self, service: str, sampler: Sampler = always_on, on_end: Optional[Callable[[Span], None]] = None, ids: Optional[IdGenerator] = None, clock: Callable[[], int] = time.time_ns) -> None:
        self.service = service
        self.sampler = sampler
        self.on_end = on_end
        self.ids = ids or IdGenerator()
        self.clock = clock

    def start_span(self, name: str, kind: int = KIND_INTERNAL, parent: Optional[SpanContext] = None, attributes: Optional[Dict[str, Any]] = None, links: Optional[List[Link]] = None, sampled: Optional[bool] = None, start_ns: Optional[int] = None) -> Span:
        trace_id = parent.trace_id if parent else self.ids.trace_id()
        decision = self.sampler(parent, trace_id) if sampled is None else sampled
        context = SpanContext(trace_id, self.ids.span_id(), FLAG_SAMPLED if decision else 0, parent.tracestate if parent else ())
        return Span(name, context, parent.span_id if parent else None, kind, start_ns or self.clock(), service=self.service, attributes=dict(attributes or {}) if decision else {}, links=list(links or []) if decision else [], recording=decision, on_end=self.on_end, clock=self.clock)


def http_span_status(kind: int, status: int) -> int:
    if status >= 500 or status < 100:
        return STATUS_ERROR
    if status >= 400 and kind == KIND_CLIENT:
        return STATUS_ERROR
    return STATUS_UNSET


def encode_value(value: Any) -> Dict[str, Any]:
    if isinstance(value, bool):
        return {"boolValue": value}
    if isinstance(value, int):
        return {"intValue": str(value)}
    if isinstance(value, float):
        return {"doubleValue": value}
    if isinstance(value, (list, tuple)):
        return {"arrayValue": {"values": [encode_value(item) for item in value]}}
    if isinstance(value, dict):
        return {"kvlistValue": {"values": encode_attributes(value)}}
    return {"stringValue": str(value)}


def decode_value(value: Dict[str, Any]) -> Any:
    if "stringValue" in value:
        return str(value["stringValue"])
    if "intValue" in value:
        return int(value["intValue"])
    if "doubleValue" in value:
        return float(value["doubleValue"])
    if "boolValue" in value:
        return bool(value["boolValue"])
    if "arrayValue" in value:
        return [decode_value(item) for item in (value["arrayValue"] or {}).get("values") or []]
    if "kvlistValue" in value:
        return decode_attributes((value["kvlistValue"] or {}).get("values") or [])
    if "bytesValue" in value:
        return str(value["bytesValue"])
    return None


def encode_attributes(attributes: Dict[str, Any]) -> List[Dict[str, Any]]:
    return [{"key": key, "value": encode_value(value)} for key, value in attributes.items()]


def decode_attributes(items: Optional[Sequence[Dict[str, Any]]]) -> Dict[str, Any]:
    return {str(item["key"]): decode_value(item.get("value") or {}) for item in items or [] if isinstance(item, dict) and "key" in item}


def encode_span(span: Span) -> Dict[str, Any]:
    body: Dict[str, Any] = {
        "traceId": span.trace_id,
        "spanId": span.span_id,
        "name": span.name,
        "kind": span.kind,
        "startTimeUnixNano": str(span.start_ns),
        "endTimeUnixNano": str(span.end_ns),
        "attributes": encode_attributes(span.attributes),
        "status": {"code": span.status_code},
    }
    if span.parent_span_id:
        body["parentSpanId"] = span.parent_span_id
    if span.context.tracestate:
        body["traceState"] = format_tracestate(span.context.tracestate)
    if span.status_message:
        body["status"]["message"] = span.status_message
    if span.events:
        body["events"] = [{"timeUnixNano": str(event.time_ns), "name": event.name, "attributes": encode_attributes(event.attributes)} for event in span.events]
    if span.links:
        body["links"] = [dict({"traceId": link.trace_id, "spanId": link.span_id, "attributes": encode_attributes(link.attributes)}, **({"traceState": link.tracestate} if link.tracestate else {})) for link in span.links]
    return body


def to_otlp(spans: Sequence[Span], scope: str = "minitrace") -> Dict[str, Any]:
    by_service: Dict[str, List[Span]] = {}
    for span in spans:
        by_service.setdefault(span.service or "unknown_service", []).append(span)
    return {"resourceSpans": [{
        "resource": {"attributes": encode_attributes({"service.name": service, "telemetry.sdk.name": "minitrace", "telemetry.sdk.language": "python", "telemetry.sdk.version": VERSION})},
        "scopeSpans": [{"scope": {"name": scope, "version": VERSION}, "spans": [encode_span(span) for span in items]}],
    } for service, items in by_service.items()]}


def normalize_id(value: Any, length: int, name: str) -> str:
    text = str(value or "").lower()
    if len(text) != length or not re.fullmatch(r"[0-9a-f]+", text) or set(text) == {"0"}:
        raise ValueError(f"{name} must be {length} hex characters and not all zeros, got {value!r}")
    return text


def enum_value(value: Any, names: Dict[int, str], prefix: str) -> int:
    if value is None or value == "":
        return 0
    if isinstance(value, int):
        return value
    text = str(value)
    if text.isdigit():
        return int(text)
    for code, name in names.items():
        if text in (name, prefix + name):
            return code
    raise ValueError(f"unknown enum value {value!r}")


def decode_span(raw: Dict[str, Any], service: str) -> Span:
    trace_id = normalize_id(raw.get("traceId"), 32, "traceId")
    span_id = normalize_id(raw.get("spanId"), 16, "spanId")
    parent = normalize_id(raw.get("parentSpanId"), 16, "parentSpanId") if raw.get("parentSpanId") else None
    status = raw.get("status") or {}
    tracestate = tuple(parse_tracestate([raw["traceState"]])) if raw.get("traceState") else ()
    span = Span(
        str(raw.get("name") or ""),
        SpanContext(trace_id, span_id, FLAG_SAMPLED, tracestate),
        parent,
        enum_value(raw.get("kind"), KIND_NAMES, "SPAN_KIND_"),
        int(raw.get("startTimeUnixNano") or 0),
        int(raw.get("endTimeUnixNano") or 0),
        service,
        decode_attributes(raw.get("attributes")),
        [Event(str(event.get("name") or ""), int(event.get("timeUnixNano") or 0), decode_attributes(event.get("attributes"))) for event in raw.get("events") or []],
        [Link(normalize_id(link.get("traceId"), 32, "link traceId"), normalize_id(link.get("spanId"), 16, "link spanId"), str(link.get("traceState") or ""), decode_attributes(link.get("attributes"))) for link in raw.get("links") or []],
        enum_value(status.get("code"), STATUS_NAMES, "STATUS_CODE_"),
        str(status.get("message") or ""),
    )
    if span.end_ns < span.start_ns:
        raise ValueError(f"span {span.name!r} ends before it starts")
    return span


def from_otlp(payload: Dict[str, Any], problems: Optional[List[str]] = None) -> List[Span]:
    spans: List[Span] = []
    for resource_spans in payload.get("resourceSpans") or []:
        resource = decode_attributes((resource_spans.get("resource") or {}).get("attributes"))
        service = str(resource.get("service.name") or "unknown_service")
        for scope_spans in resource_spans.get("scopeSpans") or []:
            for raw in scope_spans.get("spans") or []:
                try:
                    spans.append(decode_span(raw, service))
                except (ValueError, TypeError, AttributeError, ContextError) as exc:
                    if problems is None:
                        raise ValueError(f"{service}: {exc}") from None
                    problems.append(f"{service} span {raw.get('name', '?') if isinstance(raw, dict) else '?'}: {exc}")
    return spans


def load_otlp_files(paths: Sequence[str]) -> List[Span]:
    spans: List[Span] = []
    for path in paths:
        with open(path, encoding="utf-8") as handle:
            text = handle.read()
        try:
            payloads = [json.loads(text)] if text.strip() else []
        except json.JSONDecodeError:
            payloads = []
            for number, line in enumerate(text.splitlines(), 1):
                if line.strip():
                    try:
                        payloads.append(json.loads(line))
                    except json.JSONDecodeError as exc:
                        raise ValueError(f"{path}:{number}: not JSON ({exc.msg})") from None
        for payload in payloads:
            spans.extend(from_otlp(payload))
    return spans


Transport = Callable[[str, Dict[str, str], bytes, float], Tuple[int, Dict[str, str], bytes]]


def urllib_transport(url: str, headers: Dict[str, str], body: bytes, timeout: float) -> Tuple[int, Dict[str, str], bytes]:
    request = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, dict(response.headers.items()), response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, dict(exc.headers.items()) if exc.headers else {}, exc.read()


def retry_after(value: Optional[str], default: float) -> float:
    if value and value.strip().isdigit():
        return float(value.strip())
    return default


def post_otlp(endpoint: str, payload: Dict[str, Any], transport: Transport = urllib_transport, retries: int = 3, sleep: Callable[[float], None] = time.sleep, compress: bool = False) -> Dict[str, Any]:
    body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    if compress:
        body = gzip.compress(body)
        headers["Content-Encoding"] = "gzip"
    total = sum(len(scope.get("spans") or []) for resource in payload.get("resourceSpans") or [] for scope in resource.get("scopeSpans") or [])
    delay = 0.1
    status, raw = 0, b""
    for attempt in range(1, retries + 2):
        try:
            status, response_headers, raw = transport(endpoint, headers, body, 5.0)
        except OSError as exc:
            status, response_headers, raw = 0, {}, str(getattr(exc, "reason", exc)).encode()
        if status == 200:
            try:
                answer = json.loads(raw.decode("utf-8") or "{}")
            except (UnicodeDecodeError, json.JSONDecodeError):
                answer = {}
            partial = answer.get("partialSuccess") or {} if isinstance(answer, dict) else {}
            return {"endpoint": endpoint, "status": 200, "attempts": attempt, "spans": total, "rejected": int(partial.get("rejectedSpans") or 0), "message": str(partial.get("errorMessage") or "")}
        if status not in RETRYABLE or attempt > retries:
            break
        sleep(retry_after({k.lower(): v for k, v in response_headers.items()}.get("retry-after"), delay))
        delay *= 2
    return {"endpoint": endpoint, "status": status, "attempts": attempt, "spans": total, "rejected": total, "message": raw.decode("utf-8", errors="replace")[:300]}


def otlp_exporter(endpoints: Sequence[str], transport: Transport = urllib_transport, log: Callable[[str], None] = lambda text: print(text, file=sys.stderr, flush=True)) -> Callable[[List[Span]], None]:
    def export(spans: List[Span]) -> None:
        payload = to_otlp(spans)
        for endpoint in endpoints:
            result = post_otlp(endpoint, payload, transport)
            if result["status"] != 200 or result["rejected"]:
                log(f"minitrace: export to {endpoint}: status {result['status']}, {result['rejected']}/{result['spans']} spans rejected: {result['message']}")

    return export


class BatchProcessor:
    def __init__(self, export: Callable[[List[Span]], None], max_queue: int = 2048, max_batch: int = 512, delay_s: float = 0.2) -> None:
        self.export = export
        self.max_queue = max_queue
        self.max_batch = max_batch
        self.delay_s = delay_s
        self.queue: List[Span] = []
        self.lock = threading.Lock()
        self.export_lock = threading.Lock()
        self.dropped = 0
        self.exported = 0
        self.stopping = threading.Event()
        self.thread: Optional[threading.Thread] = None

    def on_end(self, span: Span) -> None:
        with self.lock:
            if len(self.queue) >= self.max_queue:
                self.dropped += 1
                return
            self.queue.append(span)

    def flush(self) -> None:
        with self.export_lock:
            while True:
                with self.lock:
                    batch, self.queue = self.queue[:self.max_batch], self.queue[self.max_batch:]
                if not batch:
                    return
                self.export(batch)
                self.exported += len(batch)

    def start(self) -> "BatchProcessor":
        def loop() -> None:
            while not self.stopping.wait(self.delay_s):
                self.flush()

        self.thread = threading.Thread(target=loop, daemon=True)
        self.thread.start()
        return self

    def shutdown(self) -> None:
        self.stopping.set()
        if self.thread is not None:
            self.thread.join(timeout=5)
        self.flush()


@dataclass
class TailPolicy:
    keep_errors: bool = True
    latency_ms: Optional[int] = None
    sample_percent: float = 0.0
    hash_salt: str = DEFAULT_HASH_SALT


def fnv1a_64(data: bytes) -> int:
    value = FNV_OFFSET_64
    for byte in data:
        value = ((value ^ byte) * FNV_PRIME_64) & MAX_UINT64
    return value


def probabilistic_threshold(percent: float) -> int:
    exact = Fraction(MAX_UINT64) * Fraction(percent / 100)
    if exact <= 0:
        return 0
    if exact >= MAX_UINT64:
        return MAX_UINT64
    exponent = exact.numerator.bit_length() - exact.denominator.bit_length()
    if Fraction(2) ** exponent > exact:
        exponent -= 1
    unit = Fraction(2) ** (exponent - 63)
    return math.floor(round(exact / unit) * unit)


def trace_hash(trace_id: str, salt: str) -> int:
    return fnv1a_64(salt.encode("utf-8") + bytes.fromhex(trace_id))


def tail_reasons(trace_id: str, spans: Sequence[Span], policy: TailPolicy) -> List[str]:
    reasons: List[str] = []
    if policy.keep_errors and any(span.status_code == STATUS_ERROR for span in spans):
        reasons.append("errors")
    if policy.latency_ms is not None and spans:
        window_ns = max(span.end_ns for span in spans) - min(span.start_ns for span in spans)
        if window_ns // 1_000_000 >= policy.latency_ms:
            reasons.append("slow")
    if policy.sample_percent > 0 and trace_hash(trace_id, policy.hash_salt) <= probabilistic_threshold(policy.sample_percent):
        reasons.append("sample")
    return reasons


@dataclass
class Decision:
    trace_id: str
    keep: bool
    reasons: List[str]
    spans: int
    late_spans: int = 0


class TailSampler:
    def __init__(self, policy: TailPolicy, decision_wait_s: float = 30.0, clock: Callable[[], float] = time.monotonic) -> None:
        self.policy = policy
        self.decision_wait_s = decision_wait_s
        self.clock = clock
        self.pending: Dict[str, Tuple[float, List[Span]]] = {}
        self.decisions: Dict[str, Decision] = {}
        self.lock = threading.Lock()

    def add(self, spans: Sequence[Span]) -> List[Span]:
        now = self.clock()
        late_kept: List[Span] = []
        with self.lock:
            for span in spans:
                decision = self.decisions.get(span.trace_id)
                if decision is not None:
                    decision.late_spans += 1
                    if decision.keep:
                        late_kept.append(span)
                    continue
                self.pending.setdefault(span.trace_id, (now, []))[1].append(span)
        return late_kept

    def tick(self, now: Optional[float] = None) -> List[Tuple[Decision, List[Span]]]:
        now = self.clock() if now is None else now
        decided: List[Tuple[Decision, List[Span]]] = []
        with self.lock:
            for trace_id in [tid for tid, (first, _) in self.pending.items() if now - first >= self.decision_wait_s]:
                spans = self.pending.pop(trace_id)[1]
                reasons = tail_reasons(trace_id, spans, self.policy)
                decision = Decision(trace_id, bool(reasons), reasons, len(spans))
                self.decisions[trace_id] = decision
                decided.append((decision, spans))
        return decided

    def pending_spans(self, trace_id: str) -> List[Span]:
        with self.lock:
            return list(self.pending.get(trace_id, (0.0, []))[1])


@dataclass
class Segment:
    span_id: str
    service: str
    name: str
    start_ns: int
    end_ns: int

    @property
    def ns(self) -> int:
        return self.end_ns - self.start_ns


def group_traces(spans: Iterable[Span]) -> Dict[str, List[Span]]:
    traces: Dict[str, List[Span]] = {}
    for span in spans:
        traces.setdefault(span.trace_id, []).append(span)
    return traces


def children_of(spans: Sequence[Span]) -> Dict[str, List[Span]]:
    children: Dict[str, List[Span]] = {}
    for span in spans:
        if span.parent_span_id:
            children.setdefault(span.parent_span_id, []).append(span)
    return children


def trace_roots(spans: Sequence[Span]) -> Tuple[List[Span], List[str]]:
    ids = {span.span_id for span in spans}
    roots = [span for span in spans if not span.parent_span_id or span.parent_span_id not in ids]
    missing = sorted({span.parent_span_id for span in spans if span.parent_span_id and span.parent_span_id not in ids})
    roots.sort(key=lambda span: (1 if span.parent_span_id else 0, span.start_ns, -span.end_ns))
    return roots, missing


def critical_path(spans: Sequence[Span]) -> List[Segment]:
    if not spans:
        return []
    children = children_of(spans)
    roots, _ = trace_roots(spans)
    segments: List[Segment] = []
    walk_critical(roots[0], roots[0].end_ns, children, segments)
    return list(reversed(segments))


def walk_critical(span: Span, until_ns: int, children: Dict[str, List[Span]], segments: List[Segment]) -> None:
    cursor = min(span.end_ns, until_ns)
    for child in sorted(children.get(span.span_id, []), key=lambda c: (c.end_ns, c.start_ns), reverse=True):
        if child.start_ns >= cursor or child.end_ns <= span.start_ns:
            continue
        child_end = min(child.end_ns, cursor)
        if child_end < cursor:
            segments.append(Segment(span.span_id, span.service, span.name, child_end, cursor))
        walk_critical(child, child_end, children, segments)
        cursor = max(child.start_ns, span.start_ns)
        if cursor <= span.start_ns:
            break
    if cursor > span.start_ns:
        segments.append(Segment(span.span_id, span.service, span.name, span.start_ns, cursor))


def covered_ns(intervals: Iterable[Tuple[int, int]]) -> int:
    total = 0
    current_start: Optional[int] = None
    current_end = 0
    for start, end in sorted(intervals):
        if current_start is None or start > current_end:
            if current_start is not None:
                total += current_end - current_start
            current_start, current_end = start, end
        else:
            current_end = max(current_end, end)
    if current_start is not None:
        total += current_end - current_start
    return total


def self_time_ns(span: Span, children: Sequence[Span]) -> int:
    clipped = [(max(c.start_ns, span.start_ns), min(c.end_ns, span.end_ns)) for c in children]
    return span.duration_ns - covered_ns((start, end) for start, end in clipped if end > start)


def ms(ns: float) -> float:
    return round(ns / 1_000_000, 3)


def merge_path(segments: Sequence[Segment]) -> List[Dict[str, Any]]:
    merged: List[Dict[str, Any]] = []
    for segment in segments:
        if merged and merged[-1]["span_id"] == segment.span_id:
            merged[-1]["ns"] += segment.ns
        else:
            merged.append({"service": segment.service, "name": segment.name, "span_id": segment.span_id, "ns": segment.ns})
    return [{"service": item["service"], "name": item["name"], "span_id": item["span_id"], "ms": ms(item["ns"])} for item in merged]


def summarize(spans: Sequence[Span]) -> Dict[str, Any]:
    roots, missing = trace_roots(spans)
    children = children_of(spans)
    segments = critical_path(spans)
    by_span: Dict[str, int] = {}
    critical_by_service: Dict[str, int] = {}
    for segment in segments:
        by_span[segment.span_id] = by_span.get(segment.span_id, 0) + segment.ns
        critical_by_service[segment.service] = critical_by_service.get(segment.service, 0) + segment.ns
    self_by_service: Dict[str, int] = {}
    for span in spans:
        self_by_service[span.service] = self_by_service.get(span.service, 0) + self_time_ns(span, children.get(span.span_id, []))
    lookup = {span.span_id: span for span in spans}
    slowest = max(by_span.items(), key=lambda item: item[1]) if by_span else None
    start = min(span.start_ns for span in spans)
    end = max(span.end_ns for span in spans)
    return {
        "trace_id": spans[0].trace_id,
        "spans": len(spans),
        "services": sorted({span.service for span in spans}),
        "root": {"service": roots[0].service, "name": roots[0].name, "span_id": roots[0].span_id},
        "roots": len(roots),
        "missing_parents": missing,
        "single_tree": len(roots) == 1 and not missing and roots[0].parent_span_id is None,
        "start_ns": start,
        "duration_ms": ms(end - start),
        "errors": [f"{span.service} {span.name}" for span in spans if span.status_code == STATUS_ERROR],
        "critical_path": merge_path(segments),
        "critical_ms_by_service": {service: ms(ns) for service, ns in sorted(critical_by_service.items(), key=lambda item: -item[1])},
        "self_ms_by_service": {service: ms(ns) for service, ns in sorted(self_by_service.items(), key=lambda item: -item[1])},
        "slowest_hop": {"service": lookup[slowest[0]].service, "name": lookup[slowest[0]].name, "span_id": slowest[0], "ms": ms(slowest[1])} if slowest else None,
        "links": [{"trace_id": link.trace_id, "span_id": link.span_id} for span in spans for link in span.links],
    }


def waterfall(spans: Sequence[Span], width: int = 40) -> str:
    summary = summarize(spans)
    roots, missing = trace_roots(spans)
    children = children_of(spans)
    on_path = {segment.span_id for segment in critical_path(spans)}
    origin = min(span.start_ns for span in spans)
    total = max(1, max(span.end_ns for span in spans) - origin)
    lines = [f"trace {summary['trace_id']}  {summary['spans']} spans  {len(summary['services'])} services  {summary['duration_ms']:.1f} ms"]
    if missing:
        lines.append(f"  missing parent span(s) {', '.join(missing)}: the context arrived but the parent's spans did not")

    def visit(span: Span, depth: int) -> None:
        offset = min(width - 1, int((span.start_ns - origin) * width / total))
        length = max(1, min(width - offset, round(span.duration_ns * width / total)))
        bar = (" " * offset + BAR * length).ljust(width)
        label = ("  " * depth + span.name)[:34]
        flags = ("*" if span.span_id in on_path else " ") + (" ERROR" if span.status_code == STATUS_ERROR else "")
        lines.append(f"  {span.service[:10]:<10} {label:<34} {KIND_NAMES.get(span.kind, '?'):<8} {bar} {span.duration_ns / 1_000_000:>8.1f} ms {flags}".rstrip())
        for child in sorted(children.get(span.span_id, []), key=lambda c: (c.start_ns, c.span_id)):
            visit(child, depth + 1)

    for root in roots:
        visit(root, 0)
    lines.append("critical path (*): " + " -> ".join(f"{item['service']} {item['name']} {item['ms']:.1f} ms" for item in summary["critical_path"]))
    lines.append("self time: " + ", ".join(f"{service} {value:.1f} ms" for service, value in summary["self_ms_by_service"].items()))
    return "\n".join(lines)


def header_lists(message: Any) -> Dict[str, List[str]]:
    headers: Dict[str, List[str]] = {}
    for key in message.keys():
        headers.setdefault(key.lower(), [])
    for key in headers:
        headers[key] = [str(value) for value in message.get_all(key) or []]
    return headers


App = Callable[[str, str, Dict[str, List[str]], bytes], Tuple[int, Any]]


def make_server(app: App, host: str = "127.0.0.1", port: int = 0) -> ThreadingHTTPServer:
    class Handler(BaseHTTPRequestHandler):
        def dispatch(self) -> None:
            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(length) if length else b""
            status, payload = app(self.command, self.path, header_lists(self.headers), body)
            data = json.dumps(payload).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        do_GET = dispatch
        do_POST = dispatch

        def log_message(self, format: str, *args: Any) -> None:
            return

    server = ThreadingHTTPServer((host, port), Handler)
    server.daemon_threads = True
    return server


HttpGet = Callable[[str, Dict[str, str]], Tuple[int, Any]]


def urllib_get(url: str, headers: Dict[str, str], timeout: float = 10.0) -> Tuple[int, Any]:
    request = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, json.loads(response.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as exc:
        text = exc.read().decode("utf-8", errors="replace")
        try:
            return exc.code, json.loads(text)
        except json.JSONDecodeError:
            return exc.code, {"error": text[:200]}


@dataclass
class ServiceConfig:
    name: str
    api_url: str = ""
    db_url: str = ""
    trust_incoming: bool = True
    delays_ms: Dict[str, float] = field(default_factory=lambda: dict(DEFAULT_DELAYS_MS))


ROUTES = {"frontend": ("/checkout",), "api": ("/api/cart", "/api/recommendations"), "db": ("/query",)}


class TracedService:
    def __init__(self, config: ServiceConfig, tracer: Tracer, http_get: HttpGet = urllib_get, sleep: Callable[[float], None] = time.sleep, log: Callable[[str], None] = lambda text: print(text, flush=True)) -> None:
        if config.name not in ROUTES:
            raise ValueError(f"unknown service {config.name!r}; use frontend, api or db")
        self.config = config
        self.tracer = tracer
        self.http_get = http_get
        self.sleep = sleep
        self.log = log

    def app(self, method: str, path: str, headers: Dict[str, List[str]], body: bytes) -> Tuple[int, Any]:
        url = urllib.parse.urlsplit(path)
        incoming = extract(headers)
        parent, links = incoming.parent, []
        if parent is not None and not self.config.trust_incoming:
            links = [Link(parent.trace_id, parent.span_id, format_tracestate(parent.tracestate), {"link.reason": "untrusted incoming context"})]
            parent = None
        known = url.path in ROUTES[self.config.name]
        attributes: Dict[str, Any] = {"http.request.method": method, "url.path": url.path, "url.scheme": "http"}
        if known:
            attributes["http.route"] = url.path
        if url.query:
            attributes["url.query"] = url.query
        for agent in headers.get("user-agent") or []:
            attributes["user_agent.original"] = agent
        span = self.tracer.start_span(f"{method} {url.path}" if known else method, KIND_SERVER, parent, attributes, links)
        for entry in incoming.baggage.values():
            span.set_attribute(entry.key, entry.value)
        for problem in incoming.problems:
            span.add_event("context.invalid", {"reason": problem})
        fault = incoming.baggage.get("cc.fault")
        active = fault.value if fault and fault.value.startswith(self.config.name + "-") else ""
        try:
            status, payload = self.route(url.path, dict(urllib.parse.parse_qsl(url.query)), span, incoming.baggage, active) if known else (404, {"error": f"{self.config.name} has no route {url.path}"})
        except Exception as exc:
            span.record_exception(exc)
            status, payload = 500, {"error": f"{type(exc).__name__}: {exc}"}
        span.set_attribute("http.response.status_code", status)
        if http_span_status(KIND_SERVER, status) == STATUS_ERROR:
            span.set_attribute("error.type", str(status))
            span.set_status(STATUS_ERROR, str(payload.get("error", "")) if isinstance(payload, dict) else "")
        span.end()
        received = (headers.get("traceparent") or ["-"])[0]
        self.log(f"{self.config.name} {method} {url.path} traceparent={received} trace={span.trace_id} sampled={str(span.context.sampled).lower()} status={status}")
        return status, payload

    def call(self, parent: Span, base: str, template: str, baggage: Dict[str, BaggageEntry], query: str = "") -> Tuple[int, Any]:
        url = base.rstrip("/") + template + (f"?{query}" if query else "")
        target = urllib.parse.urlsplit(url)
        span = self.tracer.start_span(f"GET {template}", KIND_CLIENT, parent.context, {"http.request.method": "GET", "url.full": url, "url.template": template, "server.address": target.hostname or "", "server.port": target.port or 80})
        try:
            status, payload = self.http_get(url, inject(span.context, baggage))
        except OSError as exc:
            span.record_exception(exc)
            span.set_attribute("error.type", type(exc).__name__)
            span.set_status(STATUS_ERROR, str(exc))
            span.end()
            return 503, {"error": f"{template} unreachable: {exc}"}
        span.set_attribute("http.response.status_code", status)
        if http_span_status(KIND_CLIENT, status) == STATUS_ERROR:
            span.set_attribute("error.type", str(status))
            span.set_status(STATUS_ERROR, f"HTTP {status}")
        span.end()
        return status, payload

    def pause(self, key: str) -> None:
        self.sleep(self.config.delays_ms.get(key, 0.0) / 1000)

    def route(self, path: str, query: Dict[str, str], span: Span, baggage: Dict[str, BaggageEntry], fault: str) -> Tuple[int, Any]:
        if path == "/checkout":
            with ThreadPoolExecutor(max_workers=2) as pool:
                cart = pool.submit(self.call, span, self.config.api_url, "/api/cart", baggage)
                recommendations = pool.submit(self.call, span, self.config.api_url, "/api/recommendations", baggage)
                (cart_status, cart_body), (rec_status, rec_body) = cart.result(), recommendations.result()
            if cart_status >= 500 or rec_status >= 500:
                return 502, {"error": f"upstream failed: cart {cart_status}, recommendations {rec_status}"}
            if cart_status >= 400:
                return cart_status, cart_body
            return 200, {"cart": cart_body, "recommendations": rec_body}
        if path == "/api/cart":
            status, body = self.call(span, self.config.db_url, "/query", baggage, "table=cart")
            if status >= 500:
                return 502, {"error": f"db answered {status}"}
            return 200, {"items": body.get("rows", []) if isinstance(body, dict) else []}
        if path == "/api/recommendations":
            score = self.tracer.start_span("score", KIND_INTERNAL, span.context, {"recommendations.model": "popular-items"})
            self.pause("api-slow" if fault == "api-slow" else "score")
            score.end()
            return 200, {"items": ["socks", "mug"]}
        table = query.get("table", "orders")
        query_span = self.tracer.start_span(f"SELECT {table}", KIND_INTERNAL, span.context, {"db.system.name": "other_sql", "db.operation.name": "SELECT", "db.collection.name": table})
        self.pause("db-slow" if fault == "db-slow" else "db")
        if fault == "db-error":
            error = TimeoutError("connection pool exhausted")
            query_span.record_exception(error)
            query_span.set_attribute("error.type", "TimeoutError")
            query_span.set_status(STATUS_ERROR, str(error))
            query_span.end()
            return 503, {"error": str(error)}
        query_span.end()
        return 200, {"rows": [{"sku": "socks", "qty": 2}]}


def parse_pattern(text: str) -> List[Tuple[str, bool]]:
    steps: List[Tuple[str, bool]] = []
    for item in text.split(","):
        parts = [part.strip() for part in item.split("+") if part.strip()]
        unknown = [part for part in parts if part not in FAULTS + ("ok", "unsampled")]
        if not parts or unknown:
            raise ValueError(f"pattern step {item!r}: use ok, unsampled, {', '.join(FAULTS)} joined with + (for example unsampled+db-error)")
        faults = [part for part in parts if part in FAULTS]
        steps.append((faults[0] if faults else "", "unsampled" not in parts))
    return steps


def run_load(url: str, count: int, pattern: Sequence[Tuple[str, bool]], tracer: Tracer, http_get: HttpGet = urllib_get, clock: Callable[[], float] = time.monotonic) -> List[Dict[str, Any]]:
    results: List[Dict[str, Any]] = []
    target = urllib.parse.urlsplit(url)
    for index in range(count):
        fault, sampled = pattern[index % len(pattern)]
        span = tracer.start_span(f"GET {target.path}", KIND_CLIENT, None, {"http.request.method": "GET", "url.full": url, "url.template": target.path, "server.address": target.hostname or "", "server.port": target.port or 80}, sampled=sampled)
        baggage = {"cc.fault": BaggageEntry("cc.fault", fault)} if fault else {}
        started = clock()
        try:
            status, _ = http_get(url, inject(span.context, baggage, {"User-Agent": f"minitrace-load/{VERSION}"}))
        except OSError as exc:
            status = 0
            span.record_exception(exc)
        span.set_attribute("http.response.status_code", status)
        if http_span_status(KIND_CLIENT, status) == STATUS_ERROR:
            span.set_attribute("error.type", str(status))
            span.set_status(STATUS_ERROR, f"HTTP {status}")
        span.end()
        results.append({"index": index, "trace_id": span.trace_id, "fault": fault or "ok", "sampled": sampled, "status": status, "ms": round((clock() - started) * 1000, 3)})
    return results


class Collector:
    def __init__(self, sampler: TailSampler, out_path: Optional[str] = None) -> None:
        self.sampler = sampler
        self.out_path = out_path
        self.kept: Dict[str, List[Span]] = {}
        self.received = 0
        self.rejected = 0
        self.lock = threading.Lock()
        self.write_lock = threading.Lock()

    def receive(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        problems: List[str] = []
        spans = from_otlp(payload, problems)
        late = self.sampler.add(spans)
        with self.lock:
            self.received += len(spans)
            self.rejected += len(problems)
            for span in late:
                self.kept.setdefault(span.trace_id, []).append(span)
        if late:
            self.write(late)
        if problems:
            return {"partialSuccess": {"rejectedSpans": str(len(problems)), "errorMessage": "; ".join(problems[:3])}}
        return {}

    def tick(self, now: Optional[float] = None) -> List[Decision]:
        decided = self.sampler.tick(now)
        for decision, spans in decided:
            if decision.keep:
                with self.lock:
                    self.kept[decision.trace_id] = list(spans)
                self.write(spans)
        return [decision for decision, _ in decided]

    def write(self, spans: Sequence[Span]) -> None:
        if not self.out_path:
            return
        with self.write_lock, open(self.out_path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(to_otlp(spans), separators=(",", ":")) + "\n")

    def spans_of(self, trace_id: str) -> List[Span]:
        with self.lock:
            kept = list(self.kept.get(trace_id, []))
        return kept or self.sampler.pending_spans(trace_id)

    def decisions_json(self) -> Dict[str, Any]:
        with self.sampler.lock:
            decisions = {tid: {"keep": d.keep, "reasons": list(d.reasons), "spans": d.spans, "late_spans": d.late_spans} for tid, d in self.sampler.decisions.items()}
            pending = len(self.sampler.pending)
        return {"pending": pending, "received_spans": self.received, "rejected_spans": self.rejected, "kept": sorted(tid for tid, d in decisions.items() if d["keep"]), "decisions": decisions}

    def app(self, method: str, path: str, headers: Dict[str, List[str]], body: bytes) -> Tuple[int, Any]:
        route = urllib.parse.urlsplit(path).path
        if method == "POST" and route == "/v1/traces":
            content_type = ((headers.get("content-type") or [""])[0]).split(";")[0].strip().lower()
            if content_type != "application/json":
                return 415, {"error": f"minitrace accepts OTLP/HTTP JSON only (Content-Type: application/json), got {content_type or 'none'}; binary protobuf is application/x-protobuf"}
            if ((headers.get("content-encoding") or [""])[0]).lower() == "gzip":
                try:
                    body = gzip.decompress(body)
                except (OSError, EOFError) as exc:
                    return 400, {"error": f"Content-Encoding gzip but the body is not gzip: {exc}"}
            try:
                payload = json.loads(body.decode("utf-8") or "{}")
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                return 400, {"error": f"body is not JSON: {exc}"}
            if not isinstance(payload, dict):
                return 400, {"error": "body must be an ExportTraceServiceRequest object with resourceSpans"}
            return 200, self.receive(payload)
        if method == "GET" and route == "/api/decisions":
            return 200, self.decisions_json()
        if method == "GET" and route.startswith("/api/traces/"):
            trace_id = route.rsplit("/", 1)[1].lower()
            spans = self.spans_of(trace_id)
            if not spans:
                return 404, {"error": f"no spans for trace {trace_id}"}
            summary = summarize(spans)
            decision = self.sampler.decisions.get(trace_id)
            summary["decision"] = {"keep": decision.keep, "reasons": decision.reasons} if decision else None
            return 200, summary
        return 404, {"error": f"no route {method} {route}"}


def describe_context(parent: SpanContext) -> Dict[str, Any]:
    return {"trace_id": parent.trace_id, "parent_id": parent.span_id, "flags": f"{parent.flags:02x}", "sampled": parent.sampled, "tracestate": [list(item) for item in parent.tracestate]}


def cmd_context(args: argparse.Namespace) -> int:
    headers: Dict[str, List[str]] = {}
    if args.traceparent is not None:
        headers["traceparent"] = [args.traceparent]
    if args.tracestate:
        headers["tracestate"] = list(args.tracestate)
    if args.baggage:
        headers["baggage"] = list(args.baggage)
    incoming = extract(headers)
    ids = IdGenerator(args.seed)
    parent = incoming.parent
    tracestate = list(parent.tracestate) if parent else []
    for item in args.set_tracestate or []:
        key, _, value = item.partition("=")
        tracestate = update_tracestate(tracestate, key, value)
    outgoing = SpanContext(parent.trace_id if parent else ids.trace_id(), ids.span_id(), parent.flags if parent else FLAG_SAMPLED, tuple(tracestate))
    result = {
        "incoming": describe_context(parent) if parent else None,
        "baggage": {key: {"value": entry.value, "properties": list(entry.properties)} for key, entry in incoming.baggage.items()},
        "problems": incoming.problems,
        "outgoing": inject(outgoing, incoming.baggage),
        "restarted": parent is None,
    }
    if args.json:
        print(json.dumps(result, indent=2))
    else:
        if parent:
            print(f"trace-id   {parent.trace_id}\nparent-id  {parent.span_id}\nflags      {parent.flags:02x} (sampled={str(parent.sampled).lower()})")
            for key, value in parent.tracestate:
                print(f"tracestate {key}={value}")
        for key, entry in incoming.baggage.items():
            print(f"baggage    {key}={entry.value}" + "".join(f";{prop}" for prop in entry.properties))
        for problem in incoming.problems:
            print(f"invalid    {problem}")
        print("next hop   " + ("new trace: " if parent is None else "") + "  ".join(f"{k}: {v}" for k, v in result["outgoing"].items()))
    return 1 if args.traceparent is not None and parent is None else 0


def cmd_serve(args: argparse.Namespace) -> int:
    processor = BatchProcessor(otlp_exporter(args.export), delay_s=args.flush_ms / 1000).start()
    tracer = Tracer(args.service, make_sampler(args.sampler, args.sampler_arg), processor.on_end)
    service = TracedService(ServiceConfig(args.service, args.api, args.db, not args.untrusted), tracer)
    server = make_server(service.app, args.host, args.port)
    print(f"{args.service} listening on http://{args.host}:{server.server_address[1]} exporting to {', '.join(args.export) or 'nowhere'}", flush=True)
    signal.signal(signal.SIGTERM, lambda signum, frame: sys.exit(0))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        processor.shutdown()
    return 0


def cmd_collector(args: argparse.Namespace) -> int:
    policy = TailPolicy(not args.no_errors, args.latency_ms, args.sample_percent, args.hash_salt)
    collector = Collector(TailSampler(policy, args.decision_wait), args.out)
    server = make_server(collector.app, args.host, args.port)
    stopping = threading.Event()

    def ticker() -> None:
        while not stopping.wait(0.2):
            for decision in collector.tick():
                print(f"decision {decision.trace_id} {'keep' if decision.keep else 'drop'} {','.join(decision.reasons) or '-'} spans={decision.spans}", flush=True)

    threading.Thread(target=ticker, daemon=True).start()
    print(f"collector listening on http://{args.host}:{server.server_address[1]} (POST /v1/traces, GET /api/decisions, GET /api/traces/ID)", flush=True)
    signal.signal(signal.SIGTERM, lambda signum, frame: sys.exit(0))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        stopping.set()
        server.server_close()
    return 0


def cmd_load(args: argparse.Namespace) -> int:
    processor = BatchProcessor(otlp_exporter(args.export)).start()
    tracer = Tracer("loadgen", make_sampler(args.sampler, args.sampler_arg), processor.on_end, IdGenerator(args.seed))
    results = run_load(args.url, args.requests, parse_pattern(args.pattern), tracer)
    processor.shutdown()
    if args.json:
        print(json.dumps(results, indent=2))
        return 0
    groups: Dict[Tuple[str, bool, int], List[float]] = {}
    for row in results:
        groups.setdefault((row["fault"], row["sampled"], row["status"]), []).append(row["ms"])
    print(f"{len(results)} requests to {args.url}")
    for (fault, sampled, status), values in sorted(groups.items()):
        print(f"  {fault:<9} {'sampled' if sampled else 'unsampled':<9} status {status:<3}  x{len(values):<3} mean {sum(values) / len(values):7.1f} ms")
    return 0


def cmd_analyze(args: argparse.Namespace) -> int:
    traces = group_traces(load_otlp_files(args.files))
    if args.trace:
        traces = {tid: spans for tid, spans in traces.items() if tid.startswith(args.trace.lower())}
        if not traces:
            print(f"minitrace: no trace starting with {args.trace} in {', '.join(args.files)}", file=sys.stderr)
            return 1
    summaries = [summarize(spans) for spans in traces.values()]
    summaries.sort(key=lambda s: s["start_ns"])
    broken = [s["trace_id"] for s in summaries if not s["single_tree"]]
    if args.json:
        print(json.dumps({"traces": summaries, "spans": sum(s["spans"] for s in summaries), "broken": broken}, indent=2))
    elif args.trace or args.waterfall:
        for summary in summaries:
            print(waterfall(traces[summary["trace_id"]], args.width))
            print()
    else:
        for s in summaries:
            hop = s["slowest_hop"]
            tree = "tree ok" if s["single_tree"] else f"BROKEN: {s['roots']} roots, missing {len(s['missing_parents'])} parent(s)"
            print(f"{s['trace_id']}  {s['spans']:>3} spans  {len(s['services'])} services  {s['duration_ms']:>8.1f} ms  {tree:<9}  slowest hop {hop['service']} {hop['name']} {hop['ms']:.1f} ms" + ("  ERROR" if s["errors"] else ""))
        print(f"{len(summaries)} traces, {sum(s['spans'] for s in summaries)} spans, {len(broken)} broken")
    return 1 if args.strict and broken else 0


def cmd_export(args: argparse.Namespace) -> int:
    spans = load_otlp_files(args.files)
    traces = group_traces(spans)
    failed = 0
    for trace_spans in traces.values():
        result = post_otlp(args.endpoint, to_otlp(trace_spans))
        if result["status"] != 200 or result["rejected"]:
            failed += 1
            print(f"minitrace: {args.endpoint} answered {result['status']}: {result['message']}", file=sys.stderr)
    print(f"{len(spans)} spans in {len(traces)} traces sent to {args.endpoint}, {failed} failed")
    return 1 if failed else 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="minitrace", description="W3C Trace Context, a tracer, three traced services, a tail-sampling collector and critical-path analysis")
    commands = parser.add_subparsers(dest="command", required=True)

    context = commands.add_parser("context", help="parse traceparent, tracestate and baggage, and print what the next hop sends")
    context.add_argument("--traceparent")
    context.add_argument("--tracestate", action="append")
    context.add_argument("--baggage", action="append")
    context.add_argument("--set-tracestate", action="append", metavar="KEY=VALUE")
    context.add_argument("--seed", type=int)
    context.add_argument("--json", action="store_true")

    serve = commands.add_parser("serve", help="run the frontend, api or db service")
    serve.add_argument("service", choices=sorted(ROUTES))
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=0)
    serve.add_argument("--api", default="", help="api base URL (frontend)")
    serve.add_argument("--db", default="", help="db base URL (api)")
    serve.add_argument("--export", action="append", default=[], help="OTLP/HTTP traces endpoint; repeat to fan out")
    serve.add_argument("--sampler", default="parentbased_always_on", choices=SAMPLER_NAMES)
    serve.add_argument("--sampler-arg", type=float, default=1.0)
    serve.add_argument("--untrusted", action="store_true", help="start a new trace and link to the incoming context instead of continuing it")
    serve.add_argument("--flush-ms", type=float, default=200.0)

    collector = commands.add_parser("collector", help="receive OTLP/HTTP JSON, assemble traces and tail-sample them")
    collector.add_argument("--host", default="127.0.0.1")
    collector.add_argument("--port", type=int, default=0)
    collector.add_argument("--decision-wait", type=float, default=30.0)
    collector.add_argument("--latency-ms", type=int)
    collector.add_argument("--no-errors", action="store_true", help="do not keep traces just because a span has status ERROR")
    collector.add_argument("--sample-percent", type=float, default=0.0)
    collector.add_argument("--hash-salt", default=DEFAULT_HASH_SALT)
    collector.add_argument("--out", help="append kept traces here as OTLP JSON, one payload per line")

    load = commands.add_parser("load", help="send requests to the frontend with faults carried in baggage")
    load.add_argument("--url", required=True)
    load.add_argument("--requests", type=int, default=20)
    load.add_argument("--pattern", default=DEFAULT_PATTERN, help=f"steps cycled over the requests (default {DEFAULT_PATTERN})")
    load.add_argument("--seed", type=int)
    load.add_argument("--export", action="append", default=[])
    load.add_argument("--sampler", default="always_on", choices=SAMPLER_NAMES)
    load.add_argument("--sampler-arg", type=float, default=1.0)
    load.add_argument("--json", action="store_true")

    analyze = commands.add_parser("analyze", help="assemble traces from OTLP JSON files: tree check, critical path, self time, waterfall")
    analyze.add_argument("files", nargs="+")
    analyze.add_argument("--trace", help="trace id or prefix; prints its waterfall")
    analyze.add_argument("--waterfall", action="store_true")
    analyze.add_argument("--width", type=int, default=40)
    analyze.add_argument("--strict", action="store_true", help="exit 1 if any trace is not a single tree")
    analyze.add_argument("--json", action="store_true")

    export = commands.add_parser("export", help="send OTLP JSON files to an OTLP/HTTP endpoint, one request per trace")
    export.add_argument("files", nargs="+")
    export.add_argument("--endpoint", default="http://127.0.0.1:4318/v1/traces")
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(sys.argv[1:] if argv is None else list(argv))
    handlers = {"context": cmd_context, "serve": cmd_serve, "collector": cmd_collector, "load": cmd_load, "analyze": cmd_analyze, "export": cmd_export}
    try:
        return handlers[args.command](args)
    except FileNotFoundError as exc:
        print(f"minitrace: {exc.filename}: no such file", file=sys.stderr)
    except (ValueError, ContextError) as exc:
        print(f"minitrace: {exc}", file=sys.stderr)
    except OSError as exc:
        print(f"minitrace: {exc}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
