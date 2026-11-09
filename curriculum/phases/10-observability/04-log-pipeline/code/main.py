"""minilog: a log shipper and a log store. `ship` tails files with positions keyed by device and inode (a content fingerprint guards identity), joins multiline entries, runs pipeline stages and pushes batches with retries to any Loki-compatible push API; `serve` keeps label-indexed streams of compressed chunks and answers a LogQL subset over the same HTTP API; `query` and `cardinality` talk to either; `generate` writes test logs with rename and copytruncate rotation."""
from __future__ import annotations

import argparse
import base64
import bisect
import fnmatch
import glob
import hashlib
import json
import os
import re
import shutil
import signal
import stat
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import zlib
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, Dict, List, Optional, Pattern, Sequence, Set, Tuple

NS = 1_000_000_000
FINGERPRINT_BYTES = 1024
MIN_FINGERPRINT = 64
MAX_READ = 1 << 20
SKIP_SUFFIXES = (".gz", ".bz2", ".xz", ".zip", ".zst", ".tmp")
LABEL_NAME = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]*$")
UNSAFE_KEY_CHARS = re.compile(r"[^A-Za-z0-9_]")
UNBOUNDED_HINT = re.compile(r"(^|_)(id|ids|uuid|guid|trace|span|request|session|user|email|ip|token|url|query|path)($|_)", re.IGNORECASE)
DEFAULT_PUSH_PATH = "/loki/api/v1/push"
DURATION_PART = re.compile(r"(\d+(?:\.\d+)?)(ms|s|m|h|d|w|y)")
DURATION_UNITS = {"ms": 10**6, "s": NS, "m": 60 * NS, "h": 3600 * NS, "d": 86400 * NS, "w": 7 * 86400 * NS, "y": 365 * 86400 * NS}
RFC3339 = re.compile(r"^(?P<date>\d{4}-\d{2}-\d{2})[Tt ](?P<time>\d{2}:\d{2}:\d{2})(?P<frac>\.\d+)?(?P<zone>[Zz]|[+-]\d{2}:\d{2})$")
TIMESTAMP_FORMATS = ("RFC3339", "RFC3339Nano", "Unix", "UnixMs", "UnixUs", "UnixNs")
STAGE_TYPES = ("json", "logfmt", "regex", "timestamp", "labels", "structured_metadata", "drop", "limit")
RANGE_FUNCTIONS = ("count_over_time", "rate", "bytes_over_time", "bytes_rate")
VECTOR_OPS = ("sum", "avg", "min", "max", "count")
UNSUPPORTED_STAGES = ("pattern", "unpack", "label_format", "drop", "keep", "unwrap", "decolorize", "line_format_json")
EMPTY_COMPATIBLE = "queries require at least one regexp or equality matcher that does not have an empty-compatible value"
NAMED_GROUP = re.compile(r"\(\?<([A-Za-z_][A-Za-z0-9_]*)>")
TEMPLATE_FIELD = re.compile(r"\{\{\s*(\.[A-Za-z_][A-Za-z0-9_]*|__line__)\s*\}\}")
TOKEN = re.compile(
    r"""(?P<space>\s+)
    |(?P<string>"(?:[^"\\]|\\.)*")
    |(?P<raw>`[^`]*`)
    |(?P<duration>(?:\d+(?:\.\d+)?(?:ms|s|m|h|d|w|y))+)(?![A-Za-z0-9_])
    |(?P<number>[-+]?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?)
    |(?P<op>\|=|\|~|!=|!~|=~|==|>=|<=|[{}()\[\],|=><])
    |(?P<ident>[A-Za-z_][A-Za-z0-9_]*)""",
    re.VERBOSE,
)


class InputError(ValueError):
    pass


class QueryError(ValueError):
    pass


def warn_stderr(message: str) -> None:
    print(f"minilog: warning: {message}", file=sys.stderr)


def parse_rfc3339(text: str) -> int:
    match = RFC3339.match(text.strip())
    if not match:
        raise ValueError(f"not an RFC 3339 timestamp: {text!r}")
    base = datetime.strptime(f"{match.group('date')}T{match.group('time')}", "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)
    zone = match.group("zone")
    offset = 0
    if zone not in ("Z", "z"):
        sign = 1 if zone[0] == "+" else -1
        offset = sign * (int(zone[1:3]) * 3600 + int(zone[4:6]) * 60)
    frac = (match.group("frac") or ".")[1:]
    nanos = int((frac + "000000000")[:9]) if frac else 0
    return (int(base.timestamp()) - offset) * NS + nanos


def format_rfc3339(ns: int) -> str:
    seconds, nanos = divmod(ns, NS)
    text = datetime.fromtimestamp(seconds, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
    return text + ("." + f"{nanos:09d}".rstrip("0") if nanos else "") + "Z"


def stamp_millis(ns: int) -> str:
    seconds, nanos = divmod(ns, NS)
    return datetime.fromtimestamp(seconds, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S") + f".{nanos // 10**6:03d}Z"


def parse_duration(text: str) -> int:
    parts = DURATION_PART.findall(text)
    if not parts or "".join(n + u for n, u in parts) != text:
        raise QueryError(f"not a duration: {text!r} (examples: 30s, 5m, 1h30m)")
    return sum(int(Decimal(n) * DURATION_UNITS[u]) for n, u in parts)


def parse_api_time(value: Optional[str], default: int) -> int:
    if value is None or value == "":
        return default
    if "." in value:
        try:
            return int((Decimal(value) * 1000).to_integral_value()) * 10**6
        except InvalidOperation:
            pass
    try:
        number = int(value)
    except ValueError:
        try:
            return parse_rfc3339(value)
        except ValueError:
            raise QueryError(f"cannot parse {value!r} as a timestamp (nanoseconds, seconds or RFC 3339)")
    return number * NS if len(value.lstrip("-")) <= 10 else number


def parse_step(value: Optional[str], start: int, end: int) -> int:
    if not value:
        return max(NS, (end - start) // 250 // NS * NS)
    try:
        step = int(Decimal(value) * NS)
    except InvalidOperation:
        step = parse_duration(value)
    if step <= 0:
        raise QueryError("step must be positive")
    return step


def parse_timestamp_value(value: str, fmt: str) -> Optional[int]:
    try:
        if fmt in ("RFC3339", "RFC3339Nano"):
            return parse_rfc3339(value)
        if fmt == "Unix":
            return int(Decimal(value) * NS)
        factor = {"UnixMs": 10**6, "UnixUs": 10**3, "UnixNs": 1}[fmt]
        return int(value) * factor
    except (ValueError, InvalidOperation, KeyError):
        return None


def label_string(labels: Dict[str, str]) -> str:
    return "{" + ", ".join(f"{k}={json.dumps(v, ensure_ascii=False)}" for k, v in sorted(labels.items())) + "}"


def format_value(value: float) -> str:
    if value != value:
        return "NaN"
    if value in (float("inf"), float("-inf")):
        return "+Inf" if value > 0 else "-Inf"
    if float(value).is_integer():
        return str(int(value))
    return format(Decimal(repr(float(value))), "f")


def api_seconds(ns: int) -> Any:
    return ns // NS if ns % NS == 0 else round(ns / NS, 3)


def sanitize_key(key: str) -> str:
    key = key.strip()
    if not key:
        return ""
    if "0" <= key[0] <= "9":
        key = "_" + key
    return UNSAFE_KEY_CHARS.sub("_", key)


def join_key(parts: Sequence[str]) -> str:
    if len(parts) == 1:
        return sanitize_key(parts[0])
    out = ""
    for index, part in enumerate(parts):
        part = part.strip()
        if not part:
            continue
        if index > 0 and out:
            out += "_"
        if not out and "0" <= part[0] <= "9":
            out += "_"
        out += UNSAFE_KEY_CHARS.sub("_", part)
    return out


class RawNumber(str):
    pass


def load_json_line(line: str) -> Dict[str, Any]:
    obj = json.loads(line, parse_int=RawNumber, parse_float=RawNumber, parse_constant=RawNumber)
    if not isinstance(obj, dict):
        raise ValueError("expecting json object, but it is not")
    return obj


def json_fields(obj: Dict[str, Any], prefix: Tuple[str, ...] = ()) -> List[Tuple[Tuple[str, ...], str]]:
    out: List[Tuple[Tuple[str, ...], str]] = []
    for key, value in obj.items():
        path = prefix + (key,)
        if isinstance(value, dict):
            out.extend(json_fields(value, path))
        elif isinstance(value, bool):
            out.append((path, "true" if value else "false"))
        elif isinstance(value, str):
            out.append((path, str(value)))
    return out


def flat_json(line: str) -> Dict[str, str]:
    return {join_key(path): value for path, value in json_fields(load_json_line(line)) if join_key(path)}


def skip_value(line: str, pos: int) -> int:
    while pos < len(line) and line[pos] > " ":
        pos += 1
    return pos


def quoted_value(line: str, pos: int) -> Tuple[Optional[str], int]:
    escaped = has_escape = False
    for index in range(pos + 1, len(line)):
        char = line[index]
        if escaped:
            escaped = False
        elif char == "\\":
            escaped = has_escape = True
        elif char == '"':
            raw = line[pos:index + 1]
            if not has_escape:
                return raw[1:-1], index + 1
            try:
                return json.loads(raw), index + 1
            except ValueError:
                return None, index + 1
    return None, len(line)


def logfmt_pairs(line: str) -> List[Tuple[str, str]]:
    pairs: List[Tuple[str, str]] = []
    pos, size = 0, len(line)
    while pos < size:
        while pos < size and line[pos] <= " ":
            pos += 1
        if pos >= size:
            break
        start = pos
        while pos < size and line[pos] > " " and line[pos] not in '="':
            pos += 1
        if pos < size and line[pos] == '"':
            pos = skip_value(line, pos)
            continue
        key = line[start:pos]
        if pos >= size or line[pos] <= " ":
            pairs.append((key, ""))
            continue
        if not key:
            pos = skip_value(line, pos)
            continue
        pos += 1
        if pos >= size or line[pos] <= " ":
            pairs.append((key, ""))
            continue
        if line[pos] == '"':
            value, pos = quoted_value(line, pos)
            if value is not None:
                pairs.append((key, value))
            continue
        start = pos
        while pos < size and line[pos] > " " and line[pos] not in '="':
            pos += 1
        if pos < size and line[pos] in '="':
            pos = skip_value(line, pos)
            continue
        pairs.append((key, line[start:pos]))
    return pairs


def flat_logfmt(line: str) -> Dict[str, str]:
    out: Dict[str, str] = {}
    for key, value in logfmt_pairs(line):
        name = sanitize_key(key)
        if name and value:
            out[name] = value
    return out


def compile_named(pattern: str, where: str) -> Pattern[str]:
    try:
        regex = re.compile(NAMED_GROUP.sub(r"(?P<\1>", pattern))
    except re.error as exc:
        raise InputError(f"{where}: invalid regular expression {pattern!r}: {exc}")
    if not regex.groupindex:
        raise InputError(f"{where}: at least one named capture must be supplied, for example (?P<status>\\d+)")
    return regex


def file_key(st: os.stat_result) -> str:
    return f"{st.st_dev}:{st.st_ino}"


def compare_prefix(new: bytes, known: bytes) -> str:
    if not new or not known:
        return "different"
    size = min(len(new), len(known))
    if new[:size] != known[:size]:
        return "different"
    if size >= MIN_FINGERPRINT or len(known) <= len(new):
        return "same"
    return "maybe"


@dataclass(eq=False)
class Source:
    pattern: str
    labels: Dict[str, str]
    stages: List[Dict[str, Any]]
    firstline: Optional[Pattern[str]] = None
    max_lines: int = 128
    max_wait: float = 3.0


@dataclass
class Line:
    text: str
    end: int


@dataclass(eq=False)
class FileState:
    key: str
    path: str
    origin: str
    source: Source
    offset: int = 0
    committed: int = 0
    prefix: bytes = b""
    size: int = 0
    fd: Optional[int] = None
    block: List[Line] = field(default_factory=list)
    block_since: float = 0.0


@dataclass
class Entry:
    labels: Dict[str, str]
    ts: int
    line: str
    metadata: Dict[str, str]


@dataclass
class Item:
    state: Optional[FileState]
    end: int
    entry: Optional[Entry]


@dataclass
class Config:
    labels: Dict[str, str]
    sources: List[Source]
    filename_label: bool = True
    max_label_values: int = 25
    batch_wait: float = 1.0
    batch_bytes: int = 1 << 20
    warnings: List[str] = field(default_factory=list)


def check_labels(labels: Any, where: str) -> Dict[str, str]:
    if not isinstance(labels, dict):
        raise InputError(f"{where}: labels must be an object of name: value")
    for name, value in labels.items():
        if not LABEL_NAME.match(name) or name.startswith("__"):
            raise InputError(f"{where}: {name!r} is not a valid label name ([a-zA-Z_][a-zA-Z0-9_]*, no leading __)")
        if not isinstance(value, str) or not value:
            raise InputError(f"{where}: label {name} needs a non-empty string value")
    return dict(labels)


def check_stage(raw: Any, where: str) -> Dict[str, Any]:
    if not isinstance(raw, dict) or raw.get("type") not in STAGE_TYPES:
        raise InputError(f"{where}: each stage needs a type, one of {', '.join(STAGE_TYPES)}")
    stage = dict(raw)
    kind = stage["type"]
    if kind == "regex":
        stage["_re"] = compile_named(str(stage.get("expression") or ""), where)
    elif kind == "timestamp":
        if not stage.get("source") or stage.get("format") not in TIMESTAMP_FORMATS:
            raise InputError(f"{where}: timestamp needs a source field and a format, one of {', '.join(TIMESTAMP_FORMATS)}")
    elif kind in ("labels", "structured_metadata"):
        names = stage.get("names")
        if not isinstance(names, list) or not names:
            raise InputError(f"{where}: {kind} needs names: [field, ...]")
        for name in names:
            if not isinstance(name, str) or not LABEL_NAME.match(name):
                raise InputError(f"{where}: {name!r} is not a valid label name")
    elif kind == "drop":
        if not any(k in stage for k in ("value", "expression", "longer_than")):
            raise InputError(f"{where}: drop needs value, expression or longer_than")
        if "expression" in stage:
            try:
                stage["_re"] = re.compile(stage["expression"])
            except re.error as exc:
                raise InputError(f"{where}: invalid expression: {exc}")
    elif kind == "limit":
        rate, burst = stage.get("rate"), stage.get("burst", stage.get("rate"))
        if not isinstance(rate, (int, float)) or rate <= 0 or not isinstance(burst, (int, float)) or burst < 1:
            raise InputError(f"{where}: limit needs rate > 0 (entries per second) and burst >= 1")
    return stage


def load_config(path: str) -> Config:
    try:
        with open(path, encoding="utf-8") as handle:
            doc = json.load(handle)
    except OSError as exc:
        raise InputError(f"cannot read config {path}: {exc.strerror}")
    except ValueError as exc:
        raise InputError(f"config {path} is not JSON: {exc}")
    if not isinstance(doc, dict):
        raise InputError(f"config {path} must be a JSON object")
    base = os.path.dirname(os.path.abspath(path))
    config = Config(check_labels(doc.get("labels") or {}, "labels"), [])
    config.filename_label = bool(doc.get("filename_label", True))
    config.max_label_values = int(doc.get("max_label_values", 25))
    config.batch_wait = float(doc.get("batch_wait", 1.0))
    config.batch_bytes = int(doc.get("batch_bytes", 1 << 20))
    for index, raw in enumerate(doc.get("sources") or []):
        where = f"sources[{index}]"
        if not isinstance(raw, dict) or not raw.get("path"):
            raise InputError(f"{where}: needs a path glob such as logs/app.log*")
        pattern = raw["path"] if os.path.isabs(raw["path"]) else os.path.join(base, raw["path"])
        source = Source(pattern, check_labels(raw.get("labels") or {}, where), [check_stage(s, f"{where}.stages[{i}]") for i, s in enumerate(raw.get("stages") or [])])
        multiline = raw.get("multiline")
        if multiline:
            try:
                source.firstline = re.compile(multiline["firstline"])
            except (KeyError, TypeError, re.error) as exc:
                raise InputError(f"{where}: multiline needs a valid firstline regex: {exc}")
            source.max_lines = int(multiline.get("max_lines", 128))
            source.max_wait = float(multiline.get("max_wait", 3.0))
        names = list(source.labels) + [n for s in source.stages if s["type"] == "labels" for n in s["names"]]
        for name in names:
            if UNBOUNDED_HINT.search(name):
                config.warnings.append(f"label {name!r} in {where} looks unbounded; every value is a new stream. Keep it in the line or send it as structured metadata")
        config.sources.append(source)
    if not config.sources:
        raise InputError("config needs at least one entry in sources")
    return config


class Tailer:
    def __init__(self, sources: List[Source], positions: Optional[str] = None, warn: Callable[[str], None] = warn_stderr):
        self.sources = sources
        self.positions = positions
        self.warn = warn
        self.states: Dict[str, FileState] = {}
        self.retired: List[FileState] = []
        self.stats = {"files": 0, "renamed": 0, "truncated": 0, "copies": 0, "pending_copies": 0, "closed": 0, "lost_bytes": 0}
        self.load()

    def source_for(self, *paths: str) -> Optional[Source]:
        for source in self.sources:
            if any(p and fnmatch.fnmatch(p, source.pattern) for p in paths):
                return source
        return None

    def load(self) -> None:
        if not self.positions or not os.path.exists(self.positions):
            return
        try:
            with open(self.positions, encoding="utf-8") as handle:
                doc = json.load(handle)
            entries = doc["files"].items()
        except (OSError, ValueError, KeyError, AttributeError) as exc:
            raise InputError(f"positions file {self.positions} is unreadable ({exc}); move it away to start from the beginning")
        for key, entry in entries:
            source = self.source_for(entry.get("path", ""), entry.get("origin", ""))
            if source is None:
                continue
            offset = int(entry.get("offset", 0))
            prefix = base64.b64decode(entry.get("prefix", ""))
            self.states[key] = FileState(key, entry["path"], entry.get("origin") or entry["path"], source, offset, offset, prefix)

    def discover(self) -> Dict[str, Tuple[str, Source]]:
        found: Dict[str, Tuple[str, Source]] = {}
        for source in self.sources:
            for path in sorted(glob.glob(source.pattern)):
                if path.endswith(SKIP_SUFFIXES):
                    continue
                try:
                    st = os.stat(path)
                except OSError:
                    continue
                if stat.S_ISREG(st.st_mode):
                    found.setdefault(file_key(st), (path, source))
        return found

    def open_checked(self, key: str, path: str) -> Optional[int]:
        try:
            fd = os.open(path, os.O_RDONLY)
        except OSError:
            return None
        if file_key(os.fstat(fd)) != key:
            os.close(fd)
            return None
        return fd

    def rewritten(self, state: FileState) -> bool:
        size = os.fstat(state.fd).st_size
        if size < state.offset or size < len(state.prefix):
            return True
        return bool(state.prefix) and os.pread(state.fd, len(state.prefix), 0) != state.prefix

    def close(self, state: FileState) -> None:
        if state.fd is not None:
            os.close(state.fd)
            state.fd = None

    def poll(self) -> List[Tuple[FileState, List[Line]]]:
        found = self.discover()
        reads: List[Tuple[FileState, List[Line]]] = []
        candidates: List[FileState] = []
        for key in list(self.states):
            state = self.states[key]
            if key not in found:
                del self.states[key]
                if state.fd is None:
                    candidates.append(state)
                    continue
                lines = self.read(state, final=True)
                if lines:
                    reads.append((state, lines))
                self.close(state)
                self.retired.append(state)
                self.stats["closed"] += 1
                continue
            path = found[key][0]
            if state.fd is None:
                state.fd = self.open_checked(key, path)
                if state.fd is None:
                    del self.states[key]
                    continue
            if path != state.path:
                self.stats["renamed"] += 1
                state.path = path
            if self.rewritten(state):
                del self.states[key]
                self.stats["truncated"] += 1
                candidates.append(state)
        for key, (path, source) in found.items():
            if key not in self.states:
                self.adopt(key, path, source, candidates)
        for state in candidates:
            unread = state.size - state.offset
            if state.fd is not None and unread > 0:
                self.stats["lost_bytes"] += unread
                self.warn(f"{state.path} was truncated with {unread} bytes unread and no rotated copy matched its fingerprint; those lines are lost (include the rotated name in the glob)")
            self.close(state)
            self.retired.append(state)
        for state in list(self.states.values()):
            lines = self.read(state)
            if lines:
                reads.append((state, lines))
        return reads

    def adopt(self, key: str, path: str, source: Source, candidates: List[FileState]) -> None:
        fd = self.open_checked(key, path)
        if fd is None:
            return
        size = os.fstat(fd).st_size
        if size == 0:
            os.close(fd)
            return
        prefix = os.pread(fd, min(FINGERPRINT_BYTES, size), 0)
        for candidate in candidates:
            if candidate.key == key or candidate.source is not source:
                continue
            verdict = compare_prefix(prefix, candidate.prefix)
            if verdict == "same":
                candidates.remove(candidate)
                self.close(candidate)
                candidate.key, candidate.path, candidate.fd = key, path, fd
                self.states[key] = candidate
                self.stats["copies"] += 1
                return
            if verdict == "maybe":
                os.close(fd)
                return
        for other in self.states.values():
            if other.source is source and compare_prefix(prefix, other.prefix) != "different":
                self.stats["pending_copies"] += 1
                os.close(fd)
                return
        self.states[key] = FileState(key, path, path, source, prefix=prefix, fd=fd)
        self.stats["files"] += 1

    def read(self, state: FileState, final: bool = False) -> List[Line]:
        if state.fd is None:
            return []
        lines: List[Line] = []
        size = os.fstat(state.fd).st_size
        state.size = size
        while state.offset < size:
            chunk = os.pread(state.fd, min(MAX_READ, size - state.offset), state.offset)
            if not chunk:
                break
            cut = chunk.rfind(b"\n")
            if cut < 0:
                if final or len(chunk) == MAX_READ:
                    state.offset += len(chunk)
                    lines.append(Line(chunk.decode("utf-8", "replace"), state.offset))
                    continue
                break
            pos = state.offset
            for raw in chunk[:cut].split(b"\n"):
                pos += len(raw) + 1
                lines.append(Line(raw.decode("utf-8", "replace"), pos))
            state.offset += cut + 1
        if len(state.prefix) < FINGERPRINT_BYTES and size > len(state.prefix):
            state.prefix = os.pread(state.fd, min(FINGERPRINT_BYTES, size), 0)
        return lines

    def save(self) -> None:
        if not self.positions:
            return
        doc = {"version": 1, "files": {key: {"path": s.path, "origin": s.origin, "offset": s.committed, "prefix": base64.b64encode(s.prefix).decode()} for key, s in sorted(self.states.items())}}
        tmp = self.positions + ".tmp"
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(doc, handle, indent=2)
        os.replace(tmp, self.positions)

    def close_all(self) -> None:
        for state in self.states.values():
            self.close(state)


class TokenBucket:
    def __init__(self, rate: float, burst: float, now: float):
        self.rate, self.burst, self.tokens, self.last = rate, burst, burst, now

    def allow(self, now: float) -> bool:
        self.tokens = min(self.burst, self.tokens + (now - self.last) * self.rate)
        self.last = now
        if self.tokens >= 1:
            self.tokens -= 1
            return True
        return False


class Cardinality:
    def __init__(self, limit: int, warn: Callable[[str], None]):
        self.limit, self.warn = limit, warn
        self.values: Dict[str, Set[str]] = {}
        self.warned: Set[str] = set()

    def observe(self, labels: Dict[str, str]) -> None:
        for name, value in labels.items():
            seen = self.values.setdefault(name, set())
            if len(seen) <= self.limit:
                seen.add(value)
            if len(seen) > self.limit and name not in self.warned:
                self.warned.add(name)
                self.warn(f"label {name!r} has more than {self.limit} distinct values; every value is a new stream with its own chunks. Keep it in the line or send it as structured metadata")


def push_body(entries: Sequence[Entry]) -> Dict[str, Any]:
    streams: Dict[str, Dict[str, Any]] = {}
    for entry in entries:
        stream = streams.setdefault(label_string(entry.labels), {"stream": dict(sorted(entry.labels.items())), "values": []})
        value: List[Any] = [str(entry.ts), entry.line]
        if entry.metadata:
            value.append(dict(entry.metadata))
        stream["values"].append(value)
    return {"streams": list(streams.values())}


def push_url(url: str) -> str:
    return url.rstrip("/") + DEFAULT_PUSH_PATH if urllib.parse.urlparse(url).path in ("", "/") else url


Transport = Callable[[str, bytes, Dict[str, str], float], Tuple[int, str]]


def urllib_transport(url: str, data: bytes, headers: Dict[str, str], timeout: float) -> Tuple[int, str]:
    request = urllib.request.Request(url, data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")


class Pusher:
    def __init__(self, urls: Sequence[str], transport: Transport = urllib_transport, sleep: Callable[[float], None] = time.sleep, min_backoff: float = 0.5, max_backoff: float = 300.0, max_retries: int = 10, timeout: float = 10.0, warn: Callable[[str], None] = warn_stderr, should_stop: Callable[[], bool] = lambda: False):
        self.urls = [push_url(u) for u in urls]
        self.transport, self.sleep, self.warn, self.should_stop = transport, sleep, warn, should_stop
        self.min_backoff, self.max_backoff, self.max_retries, self.timeout = min_backoff, max_backoff, max_retries, timeout
        self.stats = {u: {"batches": 0, "entries": 0, "retries": 0, "failed_batches": 0, "failed_entries": 0, "last_error": ""} for u in self.urls}

    def send(self, url: str, data: bytes) -> Tuple[str, str]:
        delay, attempt = self.min_backoff, 0
        while True:
            try:
                status, text = self.transport(url, data, {"Content-Type": "application/json"}, self.timeout)
            except OSError as exc:
                status, text = 0, str(exc)
            if 200 <= status < 300:
                return "ok", ""
            detail = f"HTTP {status}: {text.strip()[:300]}" if status else f"connection failed: {text}"
            if status and status != 429 and status < 500:
                return "rejected", detail
            if attempt >= self.max_retries:
                return "gave up", detail
            if self.should_stop():
                return "interrupted", detail
            attempt += 1
            self.stats[url]["retries"] += 1
            self.sleep(delay)
            delay = min(delay * 2, self.max_backoff)

    def push(self, body: Dict[str, Any], count: int) -> str:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        result = "ok"
        for url in self.urls:
            outcome, detail = self.send(url, data)
            stats = self.stats[url]
            if outcome == "ok":
                stats["batches"] += 1
                stats["entries"] += count
                continue
            stats["failed_batches"] += 1
            stats["failed_entries"] += count
            stats["last_error"] = detail
            self.warn(f"{url}: batch of {count} entries {outcome}: {detail}")
            result = "interrupted" if outcome == "interrupted" or result == "interrupted" else "failed"
        return result


class Shipper:
    def __init__(self, config: Config, tailer: Tailer, pusher: Optional[Pusher], clock: Callable[[], float] = time.monotonic, wall: Callable[[], int] = time.time_ns, dry_run: Optional[Callable[[str], None]] = None, warn: Callable[[str], None] = warn_stderr):
        self.config, self.tailer, self.pusher = config, tailer, pusher
        self.clock, self.wall, self.dry_run = clock, wall, dry_run
        self.batch: List[Item] = []
        self.batch_bytes = 0
        self.batch_opened = 0.0
        self.cardinality = Cardinality(config.max_label_values, warn)
        self.limiters: Dict[Tuple[int, int], TokenBucket] = {}
        self.stats = {"lines": 0, "entries": 0, "shipped": 0, "dropped_by_stage": 0, "dropped_by_limit": 0, "timestamp_failures": 0, "parse_errors": 0, "batches": 0, "undelivered_batches": 0, "interrupted_batches": 0}
        for message in config.warnings:
            warn(message)

    def cycle(self, final: bool = False) -> None:
        now = self.clock()
        for state, lines in self.tailer.poll():
            self.accept(state, lines, now)
        for state in self.tailer.retired:
            self.flush_block(state)
        self.tailer.retired.clear()
        for state in list(self.tailer.states.values()):
            if state.block and (final or now - state.block_since >= state.source.max_wait):
                self.flush_block(state)
        if self.batch and (final or self.batch_bytes >= self.config.batch_bytes or now - self.batch_opened >= self.config.batch_wait):
            self.flush()

    def accept(self, state: FileState, lines: List[Line], now: float) -> None:
        for line in lines:
            self.stats["lines"] += 1
            if state.source.firstline is None:
                self.process(state, line.text, line.end)
                continue
            if state.block and (state.source.firstline.search(line.text) or len(state.block) >= state.source.max_lines):
                self.flush_block(state)
            state.block.append(line)
            state.block_since = now

    def flush_block(self, state: FileState) -> None:
        if state.block:
            text, end = "\n".join(line.text for line in state.block).rstrip("\n"), state.block[-1].end
            state.block = []
            self.process(state, text, end)

    def process(self, state: FileState, text: str, end: int) -> None:
        self.stats["entries"] += 1
        labels = dict(self.config.labels)
        labels.update(state.source.labels)
        if self.config.filename_label:
            labels["filename"] = state.origin
        extracted: Dict[str, str] = {}
        metadata: Dict[str, str] = {}
        ts = self.wall()
        for index, stage in enumerate(state.source.stages):
            kind = stage["type"]
            if kind == "json":
                try:
                    extracted.update(flat_json(text))
                except ValueError:
                    self.stats["parse_errors"] += 1
            elif kind == "logfmt":
                extracted.update(flat_logfmt(text))
            elif kind == "regex":
                match = stage["_re"].search(extracted.get(stage["source"], "") if stage.get("source") else text)
                if match:
                    extracted.update({k: v for k, v in match.groupdict().items() if v is not None})
            elif kind == "timestamp":
                parsed = parse_timestamp_value(extracted.get(stage["source"], ""), stage["format"])
                if parsed is None:
                    self.stats["timestamp_failures"] += 1
                else:
                    ts = parsed
            elif kind in ("labels", "structured_metadata"):
                target = labels if kind == "labels" else metadata
                for name in stage["names"]:
                    if extracted.get(name):
                        target[name] = extracted[name]
            elif kind == "drop" and self.drop_matches(stage, extracted, text):
                self.stats["dropped_by_stage"] += 1
                self.add(Item(state, end, None), 0)
                return
            elif kind == "limit":
                bucket = self.limiters.setdefault((id(state.source), index), TokenBucket(float(stage["rate"]), float(stage.get("burst", stage["rate"])), self.clock()))
                if not bucket.allow(self.clock()):
                    self.stats["dropped_by_limit"] += 1
                    self.add(Item(state, end, None), 0)
                    return
        self.cardinality.observe(labels)
        self.add(Item(state, end, Entry(labels, ts, text, metadata)), len(text.encode("utf-8")))

    @staticmethod
    def drop_matches(stage: Dict[str, Any], extracted: Dict[str, str], text: str) -> bool:
        value = extracted.get(stage["source"]) if stage.get("source") else text
        if value is None:
            return False
        if "value" in stage and value != str(stage["value"]):
            return False
        if "_re" in stage and not stage["_re"].search(value):
            return False
        if "longer_than" in stage and len(text.encode("utf-8")) <= int(stage["longer_than"]):
            return False
        return True

    def add(self, item: Item, size: int) -> None:
        if not self.batch:
            self.batch_opened = self.clock()
        self.batch.append(item)
        self.batch_bytes += size

    def flush(self) -> None:
        items, self.batch, self.batch_bytes = self.batch, [], 0
        entries = [i.entry for i in items if i.entry is not None]
        if entries:
            body = push_body(entries)
            self.stats["batches"] += 1
            outcome = "ok"
            if self.dry_run is not None:
                self.dry_run(json.dumps(body, ensure_ascii=False))
            elif self.pusher is not None:
                outcome = self.pusher.push(body, len(entries))
            if outcome == "interrupted":
                self.stats["interrupted_batches"] += 1
                return
            if outcome == "failed":
                self.stats["undelivered_batches"] += 1
            self.stats["shipped"] += len(entries)
        for item in items:
            if item.state is not None:
                item.state.committed = max(item.state.committed, item.end)
        if self.dry_run is None:
            self.tailer.save()

    def stop(self) -> None:
        if self.batch:
            self.flush()
        elif self.dry_run is None:
            self.tailer.save()
        self.tailer.close_all()

    def report(self) -> Dict[str, Any]:
        files = [{"key": s.key, "path": s.path, "origin": s.origin, "offset": s.offset, "committed": s.committed, "pending_lines": len(s.block)} for s in self.tailer.states.values()]
        return {**self.stats, "rotation": dict(self.tailer.stats), "endpoints": self.pusher.stats if self.pusher else {}, "label_values": {k: len(v) for k, v in sorted(self.cardinality.values.items())}, "files": files}


@dataclass
class Chunk:
    min_ts: int
    max_ts: int
    count: int
    raw_bytes: int
    data: bytes


@dataclass(eq=False)
class Stream:
    key: str
    labels: Dict[str, str]
    chunks: List[Chunk] = field(default_factory=list)
    head: List[Tuple[int, str, Dict[str, str]]] = field(default_factory=list)
    head_bytes: int = 0
    max_ts: int = 0
    seen: Set[Tuple[int, str]] = field(default_factory=set)

    def cut(self) -> None:
        if not self.head:
            return
        rows = sorted(self.head, key=lambda e: e[0])
        raw = "\n".join(json.dumps([ts, line, md], ensure_ascii=False, separators=(",", ":")) for ts, line, md in rows).encode("utf-8")
        self.chunks.append(Chunk(rows[0][0], rows[-1][0], len(rows), len(raw), zlib.compress(raw, 6)))
        self.head, self.head_bytes = [], 0


class Store:
    def __init__(self, chunk_bytes: int = 64 * 1024, max_behind: int = 3600 * NS):
        self.chunk_bytes, self.max_behind = chunk_bytes, max_behind
        self.streams: Dict[str, Stream] = {}
        self.postings: Dict[str, Dict[str, Set[str]]] = {}
        self.stats = {"pushes": 0, "entries": 0, "duplicates_dropped": 0, "rejected": 0}
        self.lock = threading.RLock()

    def stream_for(self, labels: Dict[str, str]) -> Stream:
        key = label_string(labels)
        stream = self.streams.get(key)
        if stream is None:
            stream = self.streams[key] = Stream(key, dict(labels))
            for name, value in labels.items():
                self.postings.setdefault(name, {}).setdefault(value, set()).add(key)
        return stream

    def push(self, body: Any) -> Tuple[int, str]:
        if not isinstance(body, dict) or not isinstance(body.get("streams"), list):
            return 400, 'push body must be {"streams": [{"stream": {...}, "values": [["<ns>", "<line>"]]}]}'
        rejected: List[str] = []
        with self.lock:
            self.stats["pushes"] += 1
            for raw in body["streams"]:
                try:
                    labels = check_labels(raw.get("stream") if isinstance(raw, dict) else None, "stream")
                    if not labels:
                        raise InputError("stream: at least one label is required")
                except InputError as exc:
                    rejected.append(str(exc))
                    continue
                stream = self.stream_for(labels)
                for value in raw.get("values") or []:
                    problem = self.append(stream, value)
                    if problem:
                        rejected.append(problem)
        if rejected:
            self.stats["rejected"] += len(rejected)
            return 400, f"{len(rejected)} entries rejected; first: {rejected[0]}"
        return 204, ""

    def append(self, stream: Stream, value: Any) -> str:
        if not isinstance(value, list) or len(value) not in (2, 3) or not isinstance(value[1], str) or not str(value[0]).isdigit():
            return f"entry for stream {stream.key} must be [\"<unix ns>\", \"<line>\"] or [\"<unix ns>\", \"<line>\", {{metadata}}]"
        ts, line = int(value[0]), value[1]
        metadata = value[2] if len(value) == 3 else {}
        if not isinstance(metadata, dict) or not all(isinstance(k, str) and LABEL_NAME.match(k) and isinstance(v, str) for k, v in metadata.items()):
            return f"entry for stream {stream.key}: structured metadata must map label names to strings"
        if stream.max_ts and ts < stream.max_ts - self.max_behind:
            return f"entry too far behind, entry timestamp is: {format_rfc3339(ts)}, oldest acceptable timestamp is: {format_rfc3339(stream.max_ts - self.max_behind)} (stream {stream.key})"
        if (ts, line) in stream.seen:
            self.stats["duplicates_dropped"] += 1
            return ""
        stream.seen.add((ts, line))
        stream.head.append((ts, line, {k: metadata[k] for k in sorted(metadata)}))
        stream.head_bytes += len(line.encode("utf-8"))
        stream.max_ts = max(stream.max_ts, ts)
        self.stats["entries"] += 1
        if stream.head_bytes >= self.chunk_bytes:
            stream.cut()
        return ""

    def select(self, matchers: Sequence["Matcher"]) -> List[Stream]:
        if not matchers or all(m.matches("") for m in matchers):
            raise QueryError(EMPTY_COMPATIBLE)
        keys: Optional[Set[str]] = None
        for matcher in matchers:
            if matcher.op == "=" and matcher.value:
                hits = self.postings.get(matcher.name, {}).get(matcher.value, set())
                keys = set(hits) if keys is None else keys & hits
        candidates = self.streams.keys() if keys is None else keys
        return [self.streams[k] for k in sorted(candidates) if all(m.matches(self.streams[k].labels.get(m.name, "")) for m in matchers)]

    def read(self, stream: Stream, start: int, end: int, qstats: Dict[str, int]) -> List[Tuple[int, str, Dict[str, str]]]:
        rows: List[Tuple[int, str, Dict[str, str]]] = []
        for chunk in stream.chunks:
            if chunk.max_ts < start or chunk.min_ts >= end:
                qstats["chunks_pruned"] += 1
                continue
            qstats["chunks_scanned"] += 1
            qstats["bytes_decompressed"] += chunk.raw_bytes
            for row in zlib.decompress(chunk.data).decode("utf-8").split("\n"):
                ts, line, metadata = json.loads(row)
                qstats["entries_scanned"] += 1
                if start <= ts < end:
                    rows.append((ts, line, metadata))
        for ts, line, metadata in stream.head:
            qstats["entries_scanned"] += 1
            if start <= ts < end:
                rows.append((ts, line, metadata))
        rows.sort(key=lambda r: r[0])
        return rows

    def describe(self) -> Dict[str, Any]:
        with self.lock:
            streams = []
            for stream in self.streams.values():
                entries = sum(c.count for c in stream.chunks) + len(stream.head)
                streams.append({"labels": stream.labels, "entries": entries, "chunks": len(stream.chunks), "head_entries": len(stream.head), "raw_bytes": sum(c.raw_bytes for c in stream.chunks), "compressed_bytes": sum(len(c.data) for c in stream.chunks)})
            raw = sum(s["raw_bytes"] for s in streams)
            packed = sum(s["compressed_bytes"] for s in streams)
            return {**self.stats, "streams": len(streams), "chunks": sum(s["chunks"] for s in streams), "compression_ratio": round(raw / packed, 2) if packed else None, "label_values": {n: len(v) for n, v in sorted(self.postings.items())}, "per_stream": sorted(streams, key=lambda s: label_string(s["labels"]))}


@dataclass
class Matcher:
    name: str
    op: str
    value: str
    regex: Optional[Pattern[str]] = None

    def matches(self, value: str) -> bool:
        if self.op == "=":
            return value == self.value
        if self.op == "!=":
            return value != self.value
        hit = self.regex.fullmatch(value) is not None
        return hit if self.op == "=~" else not hit


@dataclass
class LineFilter:
    op: str
    value: str
    regex: Optional[Pattern[str]] = None

    def test(self, line: str) -> bool:
        if self.op == "|=":
            return self.value in line
        if self.op == "!=":
            return self.value not in line
        hit = self.regex.search(line) is not None
        return hit if self.op == "|~" else not hit


@dataclass
class ParserStage:
    kind: str
    regex: Optional[Pattern[str]] = None


@dataclass
class Condition:
    name: str
    op: str
    text: Optional[Matcher] = None
    number: Optional[float] = None


@dataclass
class LabelFilter:
    conditions: List[Condition]


@dataclass
class LineFormat:
    template: str


@dataclass
class LogQuery:
    matchers: List[Matcher]
    stages: List[Any]


@dataclass
class RangeAggregation:
    function: str
    query: LogQuery
    range_ns: int


@dataclass
class VectorAggregation:
    op: str
    grouping: Optional[str]
    labels: List[str]
    inner: Any


def compile_regex(pattern: str, where: str) -> Pattern[str]:
    try:
        return re.compile(pattern)
    except re.error as exc:
        raise QueryError(f"{where}: invalid regular expression {pattern!r}: {exc}")


def tokenize(text: str) -> List[Tuple[str, str]]:
    tokens: List[Tuple[str, str]] = []
    pos = 0
    while pos < len(text):
        match = TOKEN.match(text, pos)
        if not match:
            raise QueryError(f"parse error at col {pos + 1}: unexpected {text[pos]!r}")
        kind = match.lastgroup
        value = match.group()
        pos = match.end()
        if kind == "space":
            continue
        if kind == "string":
            try:
                value = json.loads(value)
            except ValueError:
                raise QueryError(f"parse error: invalid escape in string {value} (write a regex backslash as \\\\ or use backticks)")
            kind = "str"
        elif kind == "raw":
            value, kind = value[1:-1], "str"
        tokens.append((kind, value))
    return tokens


class QueryParser:
    def __init__(self, text: str):
        self.text = text
        self.tokens = tokenize(text)
        self.pos = 0

    def peek(self) -> Tuple[str, str]:
        return self.tokens[self.pos] if self.pos < len(self.tokens) else ("end", "")

    def take(self, kind: Optional[str] = None, value: Optional[str] = None) -> str:
        got = self.peek()
        if (kind and got[0] != kind) or (value is not None and got[1] != value):
            want = value or kind
            raise QueryError(f"parse error: expected {want!r}, found {got[1] or 'end of query'!r} in {self.text!r}")
        self.pos += 1
        return got[1]

    def parse(self) -> Any:
        node = self.expression()
        if self.peek()[0] != "end":
            raise QueryError(f"parse error: unexpected {self.peek()[1]!r} after the end of the expression")
        return node

    def expression(self) -> Any:
        kind, value = self.peek()
        if kind == "ident" and value in VECTOR_OPS:
            return self.vector_aggregation()
        if kind == "ident" and value in RANGE_FUNCTIONS:
            return self.range_aggregation()
        if (kind, value) == ("op", "{"):
            return self.log_query()
        raise QueryError(f"parse error: a query starts with {{selector}}, {', '.join(RANGE_FUNCTIONS)} or {', '.join(VECTOR_OPS)}; found {value!r}")

    def grouping(self) -> Tuple[Optional[str], List[str]]:
        kind, value = self.peek()
        if kind != "ident" or value not in ("by", "without"):
            return None, []
        self.take()
        self.take("op", "(")
        names: List[str] = []
        while self.peek() != ("op", ")"):
            names.append(self.take("ident"))
            if self.peek() == ("op", ","):
                self.take()
        self.take("op", ")")
        return value, names

    def vector_aggregation(self) -> VectorAggregation:
        op = self.take("ident")
        grouping, names = self.grouping()
        self.take("op", "(")
        inner = self.expression()
        self.take("op", ")")
        if grouping is None:
            grouping, names = self.grouping()
        if isinstance(inner, LogQuery):
            raise QueryError(f"parse error: {op} aggregates series; wrap the log query in a range function such as count_over_time({{...}}[5m])")
        return VectorAggregation(op, grouping, names, inner)

    def range_aggregation(self) -> RangeAggregation:
        function = self.take("ident")
        self.take("op", "(")
        query = self.log_query()
        self.take("op", "[")
        duration = self.take("duration")
        self.take("op", "]")
        self.take("op", ")")
        return RangeAggregation(function, query, parse_duration(duration))

    def selector(self) -> List[Matcher]:
        self.take("op", "{")
        matchers: List[Matcher] = []
        while self.peek() != ("op", "}"):
            name = self.take("ident")
            op = self.take("op")
            if op not in ("=", "!=", "=~", "!~"):
                raise QueryError(f"parse error: label matcher {name}{op} must use =, !=, =~ or !~")
            value = self.take("str")
            regex = compile_regex(value, f"matcher {name}{op}") if op in ("=~", "!~") else None
            matchers.append(Matcher(name, op, value, regex))
            if self.peek() == ("op", ","):
                self.take()
        self.take("op", "}")
        return matchers

    def log_query(self) -> LogQuery:
        matchers = self.selector()
        stages: List[Any] = []
        while True:
            kind, value = self.peek()
            if kind == "op" and value in ("|=", "!=", "|~", "!~"):
                self.take()
                text = self.take("str")
                stages.append(LineFilter(value, text, compile_regex(text, f"line filter {value}") if value in ("|~", "!~") else None))
            elif (kind, value) == ("op", "|"):
                self.take()
                stages.append(self.pipe_stage())
            else:
                return LogQuery(matchers, stages)

    def pipe_stage(self) -> Any:
        kind, value = self.peek()
        if kind == "ident" and value in ("json", "logfmt"):
            self.take()
            if self.peek()[0] in ("ident", "str"):
                raise QueryError(f"parse error: minilog's {value} parser takes no parameters")
            return ParserStage(value)
        if kind == "ident" and value == "regexp":
            self.take()
            pattern = self.take("str")
            try:
                return ParserStage("regexp", compile_named(pattern, "regexp"))
            except InputError as exc:
                raise QueryError(str(exc))
        if kind == "ident" and value == "line_format":
            self.take()
            template = self.take("str")
            for raw in re.findall(r"\{\{.*?\}\}", template):
                if not TEMPLATE_FIELD.fullmatch(raw):
                    raise QueryError(f"line_format: minilog supports {{{{.label}}}} and {{{{__line__}}}} only, not {raw}")
            return LineFormat(template)
        if kind == "ident" and value in UNSUPPORTED_STAGES and self.tokens[self.pos + 1:self.pos + 2] != [("op", "=")]:
            raise QueryError(f"parse error: | {value} is valid LogQL but not part of minilog's subset")
        return self.label_filter()

    def label_filter(self) -> LabelFilter:
        conditions: List[Condition] = []
        while True:
            name = self.take("ident")
            op = self.take("op")
            kind, value = self.peek()
            if kind == "str":
                self.take()
                if op not in ("=", "!=", "=~", "!~"):
                    raise QueryError(f"parse error: {name}{op}{value!r}: strings compare with =, !=, =~ or !~")
                conditions.append(Condition(name, op, text=Matcher(name, op, value, compile_regex(value, f"label filter {name}{op}") if op in ("=~", "!~") else None)))
            elif kind == "number":
                self.take()
                if op not in ("=", "==", "!=", ">", ">=", "<", "<="):
                    raise QueryError(f"parse error: {name}{op}{value}: numbers compare with ==, !=, >, >=, < or <=")
                conditions.append(Condition(name, "==" if op == "=" else op, number=float(value)))
            elif kind == "duration":
                raise QueryError(f"parse error: {name}{op}{value}: duration and bytes label filters are not part of minilog's subset")
            else:
                raise QueryError(f"parse error: label filter {name}{op} needs a string or a number, found {value!r}")
            nxt = self.peek()
            if nxt == ("op", ",") or nxt == ("ident", "and"):
                self.take()
                continue
            if nxt == ("ident", "or"):
                raise QueryError("parse error: minilog supports `and` and `,` between label filters, not `or`")
            return LabelFilter(conditions)


def parse_query(text: str) -> Any:
    return QueryParser(text).parse()


def render_template(template: str, labels: Dict[str, str], line: str) -> str:
    def value(match: "re.Match[str]") -> str:
        ref = match.group(1)
        return line if ref == "__line__" else labels.get(ref[1:], "")

    return TEMPLATE_FIELD.sub(value, template)


def evaluate_pipeline(stages: Sequence[Any], base: Dict[str, str], metadata: Dict[str, str], line: str) -> Optional[Tuple[str, Dict[str, str]]]:
    parsed: Dict[str, str] = {}
    errors: Dict[str, str] = {}

    def lookup(name: str) -> Optional[str]:
        for scope in (errors, parsed, metadata, base):
            if name in scope:
                return scope[name]
        return None

    for stage in stages:
        if isinstance(stage, LineFilter):
            if not stage.test(line):
                return None
        elif isinstance(stage, ParserStage):
            try:
                if stage.kind == "json":
                    fields = flat_json(line)
                elif stage.kind == "logfmt":
                    fields = flat_logfmt(line)
                else:
                    match = stage.regex.search(line)
                    fields = {sanitize_key(k): (v or "") for k, v in match.groupdict().items()} if match else {}
            except ValueError as exc:
                errors.setdefault("__error__", "JSONParserErr")
                errors.setdefault("__error_details__", str(exc))
                continue
            for key, value in fields.items():
                parsed[key + "_extracted" if key in base or key in metadata else key] = value
        elif isinstance(stage, LabelFilter):
            for condition in stage.conditions:
                current = lookup(condition.name)
                if condition.text is not None:
                    if not condition.text.matches(current or ""):
                        return None
                    continue
                if current is None:
                    return None
                try:
                    number = float(current)
                except ValueError:
                    errors.setdefault("__error__", "LabelFilterErr")
                    errors.setdefault("__error_details__", f"strconv.ParseFloat: parsing {current!r}: invalid syntax")
                    continue
                if not compare_numbers(number, condition.op, condition.number):
                    return None
        elif isinstance(stage, LineFormat):
            line = render_template(stage.template, {**base, **metadata, **parsed, **errors}, line)
    merged = {**base, **metadata, **parsed, **errors}
    return line, {k: v for k, v in merged.items() if v != ""}


def compare_numbers(left: float, op: str, right: float) -> bool:
    return {"==": left == right, "!=": left != right, ">": left > right, ">=": left >= right, "<": left < right, "<=": left <= right}[op]


def new_qstats() -> Dict[str, int]:
    return {"streams_matched": 0, "chunks_scanned": 0, "chunks_pruned": 0, "bytes_decompressed": 0, "entries_scanned": 0, "entries_returned": 0}


def log_entries(store: Store, query: LogQuery, start: int, end: int, qstats: Dict[str, int]) -> List[Tuple[int, int, Dict[str, str], str]]:
    out: List[Tuple[int, int, Dict[str, str], str]] = []
    with store.lock:
        streams = store.select(query.matchers)
        qstats["streams_matched"] += len(streams)
        for stream in streams:
            for ts, line, metadata in store.read(stream, start, end, qstats):
                result = evaluate_pipeline(query.stages, stream.labels, metadata, line)
                if result is not None:
                    out.append((ts, len(out), result[1], result[0]))
    return out


def run_log_query(store: Store, query: LogQuery, start: int, end: int, limit: int, direction: str, qstats: Dict[str, int]) -> List[Dict[str, Any]]:
    rows = log_entries(store, query, start, end, qstats)
    rows.sort(key=lambda r: (r[0], r[1]), reverse=direction == "backward")
    rows = rows[:limit]
    qstats["entries_returned"] = len(rows)
    streams: Dict[str, Dict[str, Any]] = {}
    for ts, _, labels, line in rows:
        streams.setdefault(label_string(labels), {"stream": dict(sorted(labels.items())), "values": []})["values"].append([str(ts), line])
    return [streams[k] for k in sorted(streams)]


Series = Dict[str, Tuple[Dict[str, str], Dict[int, float]]]


def evaluate_metric(store: Store, node: Any, times: List[int], qstats: Dict[str, int]) -> Series:
    if isinstance(node, RangeAggregation):
        rows = log_entries(store, node.query, min(times) - node.range_ns + 1, max(times) + 1, qstats)
        grouped: Dict[str, Tuple[Dict[str, str], List[Tuple[int, int]]]] = {}
        for ts, _, labels, line in rows:
            if "__error__" in labels:
                raise QueryError(f"pipeline error: '{labels['__error__']}' for series: '{label_string(labels)}'. Use a label filter to intercept this error, for example | __error__=\"\"")
            grouped.setdefault(label_string(labels), (labels, []))[1].append((ts, len(line.encode("utf-8"))))
        seconds = node.range_ns / NS
        series: Series = {}
        for key, (labels, points) in grouped.items():
            points.sort()
            stamps = [p[0] for p in points]
            sizes = [0]
            for _, size in points:
                sizes.append(sizes[-1] + size)
            values: Dict[int, float] = {}
            for t in times:
                lo, hi = bisect.bisect_right(stamps, t - node.range_ns), bisect.bisect_right(stamps, t)
                if hi > lo:
                    count, total = hi - lo, sizes[hi] - sizes[lo]
                    values[t] = {"count_over_time": count, "rate": count / seconds, "bytes_over_time": total, "bytes_rate": total / seconds}[node.function]
            if values:
                series[key] = (labels, values)
        return series
    inner = evaluate_metric(store, node.inner, times, qstats)
    buckets: Dict[str, Tuple[Dict[str, str], Dict[int, List[float]]]] = {}
    for labels, values in inner.values():
        if node.grouping == "by":
            kept = {k: v for k, v in labels.items() if k in node.labels}
        elif node.grouping == "without":
            kept = {k: v for k, v in labels.items() if k not in node.labels}
        else:
            kept = {}
        bucket = buckets.setdefault(label_string(kept), (kept, {}))[1]
        for t, value in values.items():
            bucket.setdefault(t, []).append(value)
    reduce = {"sum": sum, "avg": lambda v: sum(v) / len(v), "min": min, "max": max, "count": len}[node.op]
    return {key: (labels, {t: float(reduce(v)) for t, v in sorted(values.items())}) for key, (labels, values) in buckets.items()}


def execute(store: Store, params: Dict[str, str], instant: bool, now: Optional[int] = None) -> Dict[str, Any]:
    now = time.time_ns() if now is None else now
    text = params.get("query", "")
    if not text:
        raise QueryError("missing query parameter")
    node = parse_query(text)
    qstats = new_qstats()
    if isinstance(node, LogQuery):
        if instant:
            raise QueryError("log queries are not supported as an instant query type, use /loki/api/v1/query_range")
        end = parse_api_time(params.get("end"), now)
        start = parse_api_time(params.get("start"), end - 3600 * NS)
        direction = params.get("direction", "backward")
        if direction not in ("forward", "backward"):
            raise QueryError("direction must be forward or backward")
        result = run_log_query(store, node, start, end, int(params.get("limit", 100)), direction, qstats)
        return {"status": "success", "data": {"resultType": "streams", "result": result, "stats": {"minilog": qstats}}}
    if instant:
        at = parse_api_time(params.get("time"), now)
        series = evaluate_metric(store, node, [at], qstats)
        result = [{"metric": labels, "value": [api_seconds(at), format_value(values[at])]} for _, (labels, values) in sorted(series.items())]
        return {"status": "success", "data": {"resultType": "vector", "result": result, "stats": {"minilog": qstats}}}
    end = parse_api_time(params.get("end"), now)
    start = parse_api_time(params.get("start"), end - 3600 * NS)
    step = parse_step(params.get("step"), start, end)
    times = list(range(start, end + 1, step))
    if not times:
        raise QueryError("end must not be before start")
    series = evaluate_metric(store, node, times, qstats)
    result = [{"metric": labels, "values": [[api_seconds(t), format_value(v)] for t, v in sorted(values.items())]} for _, (labels, values) in sorted(series.items())]
    return {"status": "success", "data": {"resultType": "matrix", "result": result, "stats": {"minilog": qstats}}}


def series_of(store: Store, params: Dict[str, List[str]]) -> List[Dict[str, str]]:
    selectors = params.get("match[]") or params.get("match") or []
    if not selectors:
        raise QueryError("series needs at least one match[] selector, for example {job=~\".+\"}")
    found: Dict[str, Dict[str, str]] = {}
    for text in selectors:
        node = parse_query(text)
        if not isinstance(node, LogQuery) or node.stages:
            raise QueryError(f"series match[] must be a plain stream selector, got {text}")
        with store.lock:
            for stream in store.select(node.matchers):
                found[stream.key] = stream.labels
    return [found[k] for k in sorted(found)]


def make_handler(store: Store) -> type:
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args: Any) -> None:
            return

        def reply(self, status: int, body: Any = None, content_type: str = "application/json") -> None:
            data = b"" if status == 204 or body is None else (json.dumps(body, ensure_ascii=False).encode("utf-8") if content_type == "application/json" else str(body).encode("utf-8"))
            self.send_response(status)
            if status != 204:
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            if data:
                self.wfile.write(data)

        def do_GET(self) -> None:
            url = urllib.parse.urlparse(self.path)
            params = urllib.parse.parse_qs(url.query)
            single = {k: v[-1] for k, v in params.items()}
            try:
                if url.path == "/ready":
                    self.reply(200, "ready\n", "text/plain")
                elif url.path in ("/loki/api/v1/query", "/loki/api/v1/query_range"):
                    self.reply(200, execute(store, single, url.path.endswith("/query")))
                elif url.path == "/loki/api/v1/labels":
                    with store.lock:
                        self.reply(200, {"status": "success", "data": sorted(store.postings)})
                elif url.path.startswith("/loki/api/v1/label/") and url.path.endswith("/values"):
                    name = urllib.parse.unquote(url.path[len("/loki/api/v1/label/"):-len("/values")])
                    with store.lock:
                        self.reply(200, {"status": "success", "data": sorted(store.postings.get(name, {}))})
                elif url.path == "/loki/api/v1/series":
                    self.reply(200, {"status": "success", "data": series_of(store, params)})
                elif url.path == "/minilog/stats":
                    self.reply(200, store.describe())
                else:
                    self.reply(404, f"404 page not found: {url.path}\n", "text/plain")
            except QueryError as exc:
                self.reply(400, f"{exc}\n", "text/plain")

        def do_POST(self) -> None:
            if urllib.parse.urlparse(self.path).path != DEFAULT_PUSH_PATH:
                self.reply(404, "404 page not found\n", "text/plain")
                return
            data = self.rfile.read(int(self.headers.get("Content-Length") or 0))
            if "json" not in (self.headers.get("Content-Type") or ""):
                self.reply(415, "minilog accepts JSON pushes only (Content-Type: application/json); real shippers default to snappy-compressed protobuf, which Loki also accepts\n", "text/plain")
                return
            if self.headers.get("Content-Encoding") == "gzip":
                data = zlib.decompress(data, 16 + zlib.MAX_WBITS)
            try:
                body = json.loads(data.decode("utf-8"))
            except ValueError as exc:
                self.reply(400, f"push body is not JSON: {exc}\n", "text/plain")
                return
            status, message = store.push(body)
            self.reply(status, f"{message}\n" if message else None, "text/plain")

    return Handler


def start_server(store: Store, host: str = "127.0.0.1", port: int = 0) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer((host, port), make_handler(store))
    server.daemon_threads = True
    return server


def http_get_json(base: str, path: str, params: Sequence[Tuple[str, str]], timeout: float = 30.0) -> Dict[str, Any]:
    url = base.rstrip("/") + path + "?" + urllib.parse.urlencode(list(params))
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise QueryError(f"HTTP {exc.code} from {base}: {exc.read().decode('utf-8', 'replace').strip()}")


def render_result(doc: Dict[str, Any]) -> str:
    data = doc.get("data") or {}
    kind = data.get("resultType")
    lines: List[str] = []
    if kind == "streams":
        rows = sorted((int(v[0]), label_string(s["stream"]), v[1]) for s in data.get("result", []) for v in s["values"])
        lines.extend(f"{format_rfc3339(ts)} {labels} {line}" for ts, labels, line in rows)
        lines.append(f"{len(rows)} entries in {len(data.get('result', []))} streams")
    elif kind == "vector":
        lines.extend(f"{label_string(s['metric'])} {s['value'][1]}" for s in data.get("result", []))
    elif kind == "matrix":
        for series in data.get("result", []):
            lines.append(label_string(series["metric"]))
            lines.extend(f"  {format_rfc3339(int(Decimal(str(t)) * NS))} {v}" for t, v in series["values"])
    stats = (data.get("stats") or {}).get("minilog")
    if stats:
        lines.append("stats: " + ", ".join(f"{k}={v}" for k, v in stats.items()))
    return "\n".join(lines)


def cardinality_report(series: List[Dict[str, str]], limit: int) -> Dict[str, Any]:
    values: Dict[str, Set[str]] = {}
    for labels in series:
        for name, value in labels.items():
            values.setdefault(name, set()).add(value)
    rows = sorted(({"name": n, "values": len(v), "examples": sorted(v)[:3]} for n, v in values.items()), key=lambda r: (-r["values"], r["name"]))
    warnings = []
    for row in rows:
        if row["values"] > limit:
            warnings.append(f"label {row['name']!r} has {row['values']} values across {len(series)} streams; above {limit}, a label is usually an identifier that belongs in the line or in structured metadata")
        elif UNBOUNDED_HINT.search(row["name"]):
            warnings.append(f"label {row['name']!r} looks unbounded by its name; check that its values come from a fixed set")
    return {"streams": len(series), "labels": rows, "warnings": warnings}


JSON_LEVELS = (("debug", lambda s: s % 10 == 7), ("error", lambda s: s % 25 == 0), ("warn", lambda s: s % 20 == 0))
TRACEBACK = (
    "Traceback (most recent call last):",
    '  File "/app/payments.py", line 42, in charge',
    "    response = gateway.authorize(card, amount)",
    '  File "/app/gateway.py", line 17, in authorize',
    '    raise UpstreamTimeout("card network did not answer in 900 ms")',
    "gateway.UpstreamTimeout: card network did not answer in 900 ms",
)


def json_level(seq: int) -> str:
    return next((name for name, rule in JSON_LEVELS if rule(seq)), "info")


def render_entry(fmt: str, seq: int, ts: int, traceback_every: int = 0) -> str:
    if fmt == "json":
        level = json_level(seq)
        status = {"error": (500, 502, 503)[seq // 25 % 3], "warn": 429}.get(level, (200, 200, 201, 204)[seq % 4])
        record = {
            "ts": stamp_millis(ts),
            "level": level,
            "seq": seq,
            "msg": {"error": "upstream timeout", "warn": "rate limited"}.get(level, "request served"),
            "method": "POST" if seq % 3 == 0 else "GET",
            "path": f"/api/orders/{1000 + seq}" if seq % 3 == 0 else "/api/cart",
            "status": status,
            "duration_ms": 5 + seq * 37 % 900,
            "user_id": f"u{seq * 7919 % 5000:04d}",
            "trace_id": hashlib.sha256(f"checkout-{seq}".encode()).hexdigest()[:32],
        }
        return json.dumps(record, separators=(",", ":"))
    traceback = bool(traceback_every) and seq % traceback_every == 0
    level = "error" if traceback or seq % 25 == 0 else "warn" if seq % 20 == 0 else "info"
    status, msg = {"error": (502, "charge failed"), "warn": (402, "card declined")}.get(level, (200, "charge ok"))
    line = f'ts={stamp_millis(ts)} level={level} seq={seq} msg="{msg}" status={status} duration_ms={40 + seq * 13 % 700} card={("visa", "mastercard", "amex")[seq % 3]}'
    return "\n".join((line,) + TRACEBACK) if traceback else line


def entry_time(start: int, index: int, interval: float) -> int:
    return start + int((Decimal(index) + Decimal("0.5")) * Decimal(str(interval)) * NS)


def shift_rotated(path: str, keep: int) -> None:
    for index in range(keep - 1, 0, -1):
        if os.path.exists(f"{path}.{index}"):
            os.replace(f"{path}.{index}", f"{path}.{index + 1}")


def open_append(path: str) -> int:
    return os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)


def generate(path: str, fmt: str = "json", count: int = 100, first: int = 1, start: Optional[int] = None, interval: float = 0.5, rotate_every: int = 0, rotate: str = "rename", keep: int = 10, late_writes: int = 1, traceback_every: int = 0, pace: float = 0.0, sleep: Callable[[float], None] = time.sleep) -> Dict[str, Any]:
    if fmt not in ("json", "logfmt") or rotate not in ("rename", "copytruncate") or count < 1 or keep < 1:
        raise InputError("generate needs --format json|logfmt, --rotate rename|copytruncate, --count >= 1 and --keep >= 1")
    start = (time.time_ns() // (60 * NS) - 20) * 60 * NS if start is None else start
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    fd = open_append(path)
    reopen_after = 0
    summary: Dict[str, Any] = {"path": path, "format": fmt, "written": 0, "first": first, "last": first + count - 1, "start": format_rfc3339(start), "rotations": 0, "debug": [], "tracebacks": []}
    for index in range(count):
        seq = first + index
        os.write(fd, (render_entry(fmt, seq, entry_time(start, index, interval), traceback_every) + "\n").encode("utf-8"))
        summary["written"] += 1
        if fmt == "json" and json_level(seq) == "debug":
            summary["debug"].append(seq)
        if fmt == "logfmt" and traceback_every and seq % traceback_every == 0:
            summary["tracebacks"].append(seq)
        if reopen_after:
            reopen_after -= 1
            if not reopen_after:
                os.close(fd)
                fd = open_append(path)
        if rotate_every and (index + 1) % rotate_every == 0 and index + 1 < count:
            shift_rotated(path, keep)
            if rotate == "rename":
                os.replace(path, path + ".1")
                os.close(open_append(path))
                if late_writes > 0:
                    reopen_after = late_writes
                else:
                    os.close(fd)
                    fd = open_append(path)
            else:
                shutil.copyfile(path, path + ".1")
                os.ftruncate(fd, 0)
            summary["rotations"] += 1
        if pace:
            sleep(pace)
    os.close(fd)
    summary["files"] = sorted(os.path.basename(p) for p in glob.glob(path + "*"))
    return summary


def command_generate(args: argparse.Namespace) -> int:
    start = parse_rfc3339(args.start) if args.start else None
    summary = generate(args.path, args.format, args.count, args.first, start, args.interval, args.rotate_every, args.rotate, args.keep, args.late_writes, args.traceback_every, args.pace)
    if args.json:
        print(json.dumps(summary, indent=2))
    else:
        print(f"wrote {summary['written']} entries (seq {summary['first']}-{summary['last']}) from {summary['start']} with {summary['rotations']} {args.rotate} rotations: {', '.join(summary['files'])}")
    return 0


def command_ship(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    if args.batch_wait is not None:
        config.batch_wait = args.batch_wait
    if args.multiline_wait is not None:
        for source in config.sources:
            source.max_wait = args.multiline_wait
    if not args.push and not args.dry_run:
        raise InputError("ship needs --push URL (repeatable) or --dry-run")
    stop = {"flag": False}
    tailer = Tailer(config.sources, None if args.dry_run else args.positions)
    pusher = None if args.dry_run else Pusher(args.push, max_retries=args.max_retries, min_backoff=args.min_backoff, should_stop=lambda: stop["flag"])
    shipper = Shipper(config, tailer, pusher, dry_run=print if args.dry_run else None)

    def request_stop(signum: int, frame: Any) -> None:
        stop["flag"] = True

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)

    def write_status() -> None:
        if args.status:
            tmp = args.status + ".tmp"
            with open(tmp, "w", encoding="utf-8") as handle:
                json.dump(shipper.report(), handle, indent=2)
            os.replace(tmp, args.status)

    while True:
        shipper.cycle(final=args.once)
        write_status()
        if args.once or stop["flag"]:
            break
        time.sleep(args.poll)
    shipper.stop()
    write_status()
    report = shipper.report()
    if args.json:
        print(json.dumps(report, indent=2))
    elif not args.dry_run:
        print(f"shipped {report['shipped']} entries from {report['lines']} lines in {report['batches']} batches; dropped {report['dropped_by_stage']} by stage, {report['dropped_by_limit']} by limit; rotation {report['rotation']}", file=sys.stderr)
    return 1 if shipper.stats["undelivered_batches"] or shipper.stats["interrupted_batches"] else 0


def command_serve(args: argparse.Namespace) -> int:
    store = Store(chunk_bytes=args.chunk_bytes)
    server = start_server(store, args.host, args.port)
    port = server.server_address[1]
    if args.port_file:
        with open(args.port_file + ".tmp", "w", encoding="utf-8") as handle:
            handle.write(f"{port}\n")
        os.replace(args.port_file + ".tmp", args.port_file)
    print(f"minilog store listening on http://{args.host}:{port} (push {DEFAULT_PUSH_PATH}, query /loki/api/v1/query_range, stats /minilog/stats)", file=sys.stderr)
    signal.signal(signal.SIGTERM, lambda signum, frame: threading.Thread(target=server.shutdown, daemon=True).start())
    try:
        server.serve_forever(poll_interval=0.2)
    except KeyboardInterrupt:
        pass
    server.server_close()
    return 0


def command_query(args: argparse.Namespace) -> int:
    parse_query(args.query)
    now = time.time_ns()
    params: List[Tuple[str, str]] = [("query", args.query), ("limit", str(args.limit)), ("direction", args.direction)]
    if args.instant:
        params.append(("time", str(parse_api_time(args.time, now))))
        path = "/loki/api/v1/query"
    else:
        end = parse_api_time(args.end, now)
        start = parse_api_time(args.start, end - parse_duration(args.since))
        params += [("start", str(start)), ("end", str(end))]
        if args.step:
            params.append(("step", args.step))
        path = "/loki/api/v1/query_range"
    try:
        doc = http_get_json(args.url, path, params)
    except (urllib.error.URLError, OSError) as exc:
        print(f"minilog: cannot reach {args.url}: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(doc, indent=2, ensure_ascii=False) if args.json else render_result(doc))
    return 0


def command_cardinality(args: argparse.Namespace) -> int:
    now = time.time_ns()
    params = [("match[]", args.match), ("start", str(now - parse_duration(args.since))), ("end", str(now))]
    try:
        doc = http_get_json(args.url, "/loki/api/v1/series", params)
    except (urllib.error.URLError, OSError) as exc:
        print(f"minilog: cannot reach {args.url}: {exc}", file=sys.stderr)
        return 1
    report = cardinality_report(doc.get("data") or [], args.limit)
    if args.json:
        print(json.dumps(report, indent=2))
        return 0
    print(f"streams: {report['streams']}")
    width = max([len(r["name"]) for r in report["labels"]] + [5])
    print(f"  {'label':<{width}}  values  examples")
    for row in report["labels"]:
        print(f"  {row['name']:<{width}}  {row['values']:>6}  {', '.join(row['examples'])}")
    for message in report["warnings"]:
        print(f"warning: {message}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="minilog", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    gen = sub.add_parser("generate", help="write numbered log entries, rotating by rename or copytruncate")
    gen.add_argument("path")
    gen.add_argument("--format", choices=("json", "logfmt"), default="json", help="json: checkout-style JSON lines; logfmt: payments-style logfmt with optional tracebacks")
    gen.add_argument("--count", type=int, default=100)
    gen.add_argument("--first", type=int, default=1, help="first sequence number")
    gen.add_argument("--start", help="RFC 3339 time of the first entry (default: 20 minutes ago, on the minute)")
    gen.add_argument("--interval", type=float, default=0.5, help="seconds between entry timestamps; each entry sits mid-interval")
    gen.add_argument("--rotate-every", type=int, default=0, help="rotate after every N entries")
    gen.add_argument("--rotate", choices=("rename", "copytruncate"), default="rename")
    gen.add_argument("--keep", type=int, default=10, help="rotated files to keep (path.1 is the newest)")
    gen.add_argument("--late-writes", type=int, default=1, help="rename mode: entries written to the old descriptor before the writer reopens")
    gen.add_argument("--traceback-every", type=int, default=0, help="logfmt: every Nth entry carries a multiline traceback")
    gen.add_argument("--pace", type=float, default=0.0, help="real seconds to sleep between entries")
    gen.add_argument("--json", action="store_true")
    gen.set_defaults(func=command_generate)

    ship = sub.add_parser("ship", help="tail files and push entries to Loki-compatible endpoints")
    ship.add_argument("--config", required=True, help="pipeline JSON: labels, sources (path glob, labels, multiline, stages)")
    ship.add_argument("--push", action="append", default=[], help="base URL or full push URL; repeat to fan out")
    ship.add_argument("--positions", default="minilog-positions.json")
    ship.add_argument("--poll", type=float, default=0.25, help="seconds between polls (default 0.25, like Alloy's file watcher)")
    ship.add_argument("--once", action="store_true", help="read to the end, flush multiline blocks, push and exit")
    ship.add_argument("--dry-run", action="store_true", help="print push bodies instead of sending them; positions are not saved")
    ship.add_argument("--status", help="write a JSON status report here after every batch")
    ship.add_argument("--batch-wait", type=float)
    ship.add_argument("--multiline-wait", type=float)
    ship.add_argument("--max-retries", type=int, default=10)
    ship.add_argument("--min-backoff", type=float, default=0.5)
    ship.add_argument("--json", action="store_true")
    ship.set_defaults(func=command_ship)

    serve = sub.add_parser("serve", help="run the store: Loki's push and query API over label-indexed compressed chunks")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=3101)
    serve.add_argument("--port-file", help="write the bound port here (useful with --port 0)")
    serve.add_argument("--chunk-bytes", type=int, default=64 * 1024, help="cut a chunk when the head holds this many bytes of lines")
    serve.set_defaults(func=command_serve)

    query = sub.add_parser("query", help="run LogQL against Loki or a minilog store")
    query.add_argument("query")
    query.add_argument("--url", default="http://127.0.0.1:3100")
    query.add_argument("--since", default="1h")
    query.add_argument("--start")
    query.add_argument("--end")
    query.add_argument("--step")
    query.add_argument("--instant", action="store_true")
    query.add_argument("--time")
    query.add_argument("--limit", type=int, default=100)
    query.add_argument("--direction", choices=("forward", "backward"), default="backward")
    query.add_argument("--json", action="store_true", help="print the raw API response")
    query.set_defaults(func=command_query)

    card = sub.add_parser("cardinality", help="label values per label name from /loki/api/v1/series")
    card.add_argument("--url", default="http://127.0.0.1:3100")
    card.add_argument("--match", default='{job=~".+"}')
    card.add_argument("--since", default="1h")
    card.add_argument("--limit", type=int, default=25)
    card.add_argument("--json", action="store_true")
    card.set_defaults(func=command_cardinality)
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(list(sys.argv[1:] if argv is None else argv))
    try:
        return args.func(args)
    except (InputError, QueryError) as exc:
        print(f"minilog: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
