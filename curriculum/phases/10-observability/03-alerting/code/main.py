"""minialert: Prometheus alerting rules on a virtual clock (for, keep_firing_for, templates, promtool-style unit tests) and an Alertmanager-style router (routing tree, grouping, inhibition, silences, deduplication) that renders webhook notifications, with a live API server, a webhook recorder, a driver for the real Alertmanager and a comparer."""
from __future__ import annotations

import argparse
import copy
import functools
import json
import math
import os
import re
import shlex
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, Dict, FrozenSet, List, Optional, Sequence, Set, Tuple

DURATION = re.compile(r"^(?:(\d+)y)?(?:(\d+)w)?(?:(\d+)d)?(?:(\d+)h)?(?:(\d+)m)?(?:(\d+)s)?(?:(\d+)ms)?$")
DURATION_UNITS = (365 * 86400, 7 * 86400, 86400, 3600, 60, 1, 0.001)
DEFAULT_GROUP_WAIT = 30.0
DEFAULT_GROUP_INTERVAL = 300.0
DEFAULT_REPEAT_INTERVAL = 4 * 3600.0
DEFAULT_RESOLVE_TIMEOUT = 300.0
LOOKBACK_DELTA = 300.0
RESOLVED_RETENTION = 900.0
RESEND_DELAY = 60.0
FNV_OFFSET = 14695981039346656037
FNV_PRIME = 1099511628211
MASK64 = (1 << 64) - 1
SEPARATOR = 0xFF
ZERO_TIME = "0001-01-01T00:00:00Z"
DEFAULT_START = "2026-11-02T09:00:00Z"
REASON_NONE = "none"
REASON_FIRST = "first notification"
REASON_NEW_ALERTS = "new alerts added"
REASON_NEW_RESOLVED = "some alerts resolved"
REASON_ALL_RESOLVED = "all alerts resolved"
REASON_REPEAT = "repeat interval elapsed"
SEND_RESOLVED_BY_DEFAULT = ("webhook", "pagerduty", "opsgenie", "msteams", "telegram", "discord")
OP_RANK = {"=": 0, "!=": 1, "=~": 2, "!~": 3}
PENDING, FIRING, INACTIVE = "pending", "firing", "inactive"
META_CHARACTERS = set("\\.+*?()|[]{}^$")
MATCHER_TEXT = re.compile(r"^\s*([a-zA-Z_][a-zA-Z0-9_]*)\s*(=~|!~|!=|=)\s*(.*?)\s*$", re.S)
RFC3339 = re.compile(r"^(\d{4})-(\d{2})-(\d{2})[Tt ](\d{2}):(\d{2}):(\d{2})(?:\.(\d+))?(Z|z|[+-]\d{2}:\d{2})$")
STEP_KINDS = ("post", "resolve", "silence", "expire", "snapshot")
BRIEF_LABELS = ("service", "instance", "queue", "cluster", "job")


class InputError(ValueError):
    pass


class ConfigError(InputError):
    pass


class TemplateError(ValueError):
    pass


def parse_duration(text: Any) -> float:
    raw = str(text).strip()
    if raw == "0":
        return 0.0
    match = DURATION.match(raw)
    if not raw or not match or not any(match.groups()):
        raise InputError(f"not a duration: {raw!r} (examples: 500ms, 30s, 5m, 1h30m, 1d)")
    return float(sum(int(value) * unit for value, unit in zip(match.groups(), DURATION_UNITS) if value))


def format_duration(seconds: float) -> str:
    millis = int(round(seconds * 1000))
    if millis == 0:
        return "0s"
    out = "-" if millis < 0 else ""
    millis = abs(millis)
    for name, size in (("y", 31536000000), ("w", 604800000), ("d", 86400000), ("h", 3600000), ("m", 60000), ("s", 1000), ("ms", 1)):
        if millis >= size:
            out += f"{millis // size}{name}"
            millis %= size
    return out


def rfc3339(unix: Optional[float]) -> str:
    if unix is None:
        return ZERO_TIME
    moment = datetime.fromtimestamp(unix, tz=timezone.utc)
    text = moment.strftime("%Y-%m-%dT%H:%M:%S")
    if moment.microsecond:
        text += f".{moment.microsecond:06d}".rstrip("0")
    return text + "Z"


def parse_time(text: Any) -> Optional[float]:
    if text is None or text == "":
        return None
    match = RFC3339.match(str(text).strip())
    if not match:
        raise InputError(f"not an RFC 3339 time: {text!r} (example: 2026-11-02T09:00:00Z)")
    year, month, day, hour, minute, second, fraction, zone = match.groups()
    moment = datetime(int(year), int(month), int(day), int(hour), int(minute), int(second), tzinfo=timezone.utc)
    unix = moment.timestamp() + (float("0." + fraction) if fraction else 0.0)
    if zone not in ("Z", "z"):
        sign = 1 if zone[0] == "+" else -1
        unix -= sign * (int(zone[1:3]) * 3600 + int(zone[4:6]) * 60)
    return unix


def go_float(value: float) -> str:
    if math.isnan(value):
        return "NaN"
    if math.isinf(value):
        return "+Inf" if value > 0 else "-Inf"
    if value == 0:
        return "-0" if math.copysign(1.0, value) < 0 else "0"
    sign = "-" if value < 0 else ""
    _, digit_tuple, exponent = Decimal(repr(abs(value))).normalize().as_tuple()
    digits = "".join(str(d) for d in digit_tuple)
    point = len(digits) + int(exponent)
    scientific = point - 1
    if scientific < -4 or scientific >= 6:
        mantissa = digits[0] + ("." + digits[1:] if len(digits) > 1 else "")
        return f"{sign}{mantissa}e{'-' if scientific < 0 else '+'}{abs(scientific):02d}"
    if point <= 0:
        return f"{sign}0.{'0' * -point}{digits}"
    if point >= len(digits):
        return f"{sign}{digits}{'0' * (point - len(digits))}"
    return f"{sign}{digits[:point]}.{digits[point:]}"


def go_quote(text: str) -> str:
    out = []
    for char in text:
        if char == '"':
            out.append('\\"')
        elif char == "\\":
            out.append("\\\\")
        elif char == "\n":
            out.append("\\n")
        elif char == "\t":
            out.append("\\t")
        elif char == "\r":
            out.append("\\r")
        elif ord(char) < 0x20 or ord(char) == 0x7F:
            out.append(f"\\x{ord(char):02x}")
        else:
            out.append(char)
    return '"' + "".join(out) + '"'


def quote_meta(text: str) -> str:
    return "".join("\\" + char if char in META_CHARACTERS else char for char in text)


def fingerprint(labels: Dict[str, str]) -> str:
    value = FNV_OFFSET
    for name in sorted(labels):
        for chunk in (name.encode(), bytes([SEPARATOR]), labels[name].encode(), bytes([SEPARATOR])):
            for byte in chunk:
                value = ((value ^ byte) * FNV_PRIME) & MASK64
    return f"{value:016x}"


def labelset_string(labels: Dict[str, str]) -> str:
    return "{" + ", ".join(f"{name}={go_quote(labels[name])}" for name in sorted(labels)) + "}"


def brief(labels: Dict[str, str]) -> str:
    shown = [name for name in BRIEF_LABELS if name in labels][:2]
    return labels.get("alertname", "?") + "{" + ", ".join(f"{name}={go_quote(labels[name])}" for name in shown) + "}"


@functools.lru_cache(maxsize=512)
def anchored(pattern: str, dotall: bool = False) -> "re.Pattern[str]":
    try:
        return re.compile(f"(?:{pattern})", re.S if dotall else 0)
    except re.error as exc:
        raise InputError(f"bad regular expression {pattern!r}: {exc}") from exc


@dataclass(frozen=True)
class Matcher:
    name: str
    op: str
    value: str

    def matches(self, labels: Dict[str, str]) -> bool:
        actual = labels.get(self.name, "")
        if self.op == "=":
            return actual == self.value
        if self.op == "!=":
            return actual != self.value
        found = anchored(self.value).fullmatch(actual) is not None
        return found if self.op == "=~" else not found

    def __str__(self) -> str:
        escaped = self.value.replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')
        return f'{self.name}{self.op}"{escaped}"'


def parse_matcher(text: Any) -> Matcher:
    match = MATCHER_TEXT.match(str(text))
    if not match:
        raise InputError(f'not a matcher: {text!r} (examples: severity="page", team=~"db|storage", env!=dev)')
    name, op, raw = match.groups()
    if raw.startswith('"'):
        try:
            value = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise InputError(f"bad quoted value in matcher {text!r}: {exc}") from exc
        if not isinstance(value, str):
            raise InputError(f"bad quoted value in matcher {text!r}")
    elif '"' in raw:
        raise InputError(f"unbalanced quote in matcher {text!r}")
    else:
        value = raw
    if op in ("=~", "!~"):
        anchored(value)
    return Matcher(name, op, value)


def sort_matchers(matchers: List[Matcher]) -> List[Matcher]:
    return sorted(matchers, key=lambda m: (m.name, m.value, OP_RANK[m.op]))


def matchers_text(matchers: Sequence[Matcher]) -> str:
    return "{" + ",".join(str(m) for m in matchers) + "}"


def all_match(matchers: Sequence[Matcher], labels: Dict[str, str]) -> bool:
    return all(m.matches(labels) for m in matchers)


@dataclass
class Integration:
    kind: str
    index: int
    url: str
    send_resolved: bool
    max_alerts: int


@dataclass
class Receiver:
    name: str
    integrations: List[Integration]


@dataclass
class InhibitRule:
    name: str
    source: List[Matcher]
    target: List[Matcher]
    equal: List[str]


@dataclass(eq=False)
class Route:
    matchers: List[Matcher]
    receiver: str
    group_by: List[str]
    group_by_all: bool
    group_wait: float
    group_interval: float
    repeat_interval: float
    proceed: bool
    parent: Optional["Route"] = field(default=None, repr=False)
    position: int = 0
    idx: int = 0
    routes: List["Route"] = field(default_factory=list, repr=False)

    def key(self) -> str:
        return (self.parent.key() + "/" if self.parent else "") + matchers_text(self.matchers)

    def path(self) -> str:
        return "route" if self.parent is None else f"{self.parent.path()}.routes[{self.position}]"

    def match(self, labels: Dict[str, str]) -> List["Route"]:
        if not all_match(self.matchers, labels):
            return []
        found: List[Route] = []
        for child in self.routes:
            matched = child.match(labels)
            found.extend(matched)
            if matched and not child.proceed:
                break
        return found or [self]

    def walk(self) -> List["Route"]:
        out = [self]
        for child in self.routes:
            out.extend(child.walk())
        return out

    def describe(self) -> str:
        grouping = "..." if self.group_by_all else ", ".join(self.group_by)
        return f"group_by [{grouping}], group_wait {format_duration(self.group_wait)}, group_interval {format_duration(self.group_interval)}, repeat_interval {format_duration(self.repeat_interval)}"


@dataclass
class Config:
    route: Route
    receivers: Dict[str, Receiver]
    inhibit_rules: List[InhibitRule]
    resolve_timeout: float


def legacy_matchers(exact: Any, regex: Any, modern: Any) -> List[Matcher]:
    out = [Matcher(str(k), "=", str(v)) for k, v in (exact or {}).items()]
    for name, pattern in (regex or {}).items():
        anchored(str(pattern))
        out.append(Matcher(str(name), "=~", str(pattern)))
    out.extend(parse_matcher(text) for text in modern or [])
    return out


def build_route(raw: Dict[str, Any], parent: Optional[Route], position: int, counter: List[int]) -> Route:
    if not isinstance(raw, dict):
        raise ConfigError(f"a route must be an object, got {raw!r}")
    if parent is None:
        receiver, group_by, group_by_all = "", [], False
        timers = [DEFAULT_GROUP_WAIT, DEFAULT_GROUP_INTERVAL, DEFAULT_REPEAT_INTERVAL]
    else:
        receiver, group_by, group_by_all = parent.receiver, list(parent.group_by), parent.group_by_all
        timers = [parent.group_wait, parent.group_interval, parent.repeat_interval]
    receiver = str(raw.get("receiver") or receiver)
    if raw.get("group_by") is not None:
        names = [str(n) for n in raw["group_by"]]
        if "..." in names:
            if len(names) > 1:
                raise ConfigError("cannot have wildcard group_by (`...`) and other labels at the same time")
            group_by, group_by_all = [], True
        else:
            if len(set(names)) != len(names):
                raise ConfigError(f"duplicated label in group_by: {names}")
            group_by, group_by_all = names, False
    for i, key in enumerate(("group_wait", "group_interval", "repeat_interval")):
        if raw.get(key) is not None:
            timers[i] = parse_duration(raw[key])
    if timers[1] == 0:
        raise ConfigError("group_interval cannot be zero")
    if timers[2] == 0:
        raise ConfigError("repeat_interval cannot be zero")
    route = Route(
        matchers=sort_matchers(legacy_matchers(raw.get("match"), raw.get("match_re"), raw.get("matchers"))),
        receiver=receiver,
        group_by=group_by,
        group_by_all=group_by_all,
        group_wait=timers[0],
        group_interval=timers[1],
        repeat_interval=timers[2],
        proceed=bool(raw.get("continue", False)),
        parent=parent,
        position=position,
        idx=counter[0],
    )
    counter[0] += 1
    route.routes = [build_route(child, route, i, counter) for i, child in enumerate(raw.get("routes") or [])]
    return route


def load_config(raw: Any) -> Config:
    if not isinstance(raw, dict):
        raise ConfigError("the configuration must be an object with route and receivers")
    resolve_timeout = parse_duration((raw.get("global") or {}).get("resolve_timeout", "5m"))
    receivers: Dict[str, Receiver] = {}
    for entry in raw.get("receivers") or []:
        name = str(entry.get("name") or "")
        if not name:
            raise ConfigError("missing name on receiver")
        if name in receivers:
            raise ConfigError(f'notification config name "{name}" is not unique')
        integrations = []
        for key in [k for k in entry if k.endswith("_configs")]:
            kind = key[: -len("_configs")]
            for index, item in enumerate(entry[key] or []):
                if kind == "webhook" and not item.get("url"):
                    raise ConfigError(f'receiver "{name}": one of url or url_file must be configured for webhook_configs[{index}]')
                send_resolved = bool(item.get("send_resolved", kind in SEND_RESOLVED_BY_DEFAULT))
                integrations.append(Integration(kind, index, str(item.get("url") or ""), send_resolved, int(item.get("max_alerts") or 0)))
        receivers[name] = Receiver(name, integrations)
    root = raw.get("route")
    if not isinstance(root, dict):
        raise ConfigError("no routes provided")
    if not root.get("receiver"):
        raise ConfigError("root route must specify a default receiver")
    if root.get("matchers") or root.get("match") or root.get("match_re"):
        raise ConfigError("root route must not have any matchers")
    route = build_route(root, None, 0, [0])
    for node in route.walk():
        if node.receiver not in receivers:
            raise ConfigError(f'undefined receiver "{node.receiver}" used in route')
    rules = []
    for index, entry in enumerate(raw.get("inhibit_rules") or []):
        source = legacy_matchers(entry.get("source_match"), entry.get("source_match_re"), entry.get("source_matchers"))
        target = legacy_matchers(entry.get("target_match"), entry.get("target_match_re"), entry.get("target_matchers"))
        rules.append(InhibitRule(str(entry.get("name") or f"inhibit_rules[{index}]"), source, target, [str(n) for n in entry.get("equal") or []]))
    return Config(route, receivers, rules, resolve_timeout)


def group_labels(labels: Dict[str, str], route: Route) -> Dict[str, str]:
    return {k: v for k, v in labels.items() if route.group_by_all or k in route.group_by}


def group_key(route: Route, labels: Dict[str, str]) -> str:
    return f"{route.key()}:{labelset_string(labels)}"


def explain_route(route: Route, labels: Dict[str, str], lines: List[str], depth: int = 0) -> List[Route]:
    indent = "  " * depth
    failed = [m for m in route.matchers if not m.matches(labels)]
    if failed:
        m = failed[0]
        lines.append(f"{indent}{route.path()} {matchers_text(route.matchers)}: no match ({m.name}={go_quote(labels.get(m.name, ''))} fails {m})")
        return []
    lines.append(f"{indent}{route.path()} {matchers_text(route.matchers)}: match, receiver {route.receiver}")
    found: List[Route] = []
    for child in route.routes:
        matched = explain_route(child, labels, lines, depth + 1)
        found.extend(matched)
        if matched and not child.proceed:
            if child.position < len(route.routes) - 1:
                lines.append(f"{indent}  stop: {child.path()} matched and continue is false, later siblings are not tried")
            break
        if matched:
            lines.append(f"{indent}  continue: true on {child.path()}, trying the next sibling")
    if not found:
        if route.routes:
            lines.append(f"{indent}  no child matched, so {route.path()} itself receives the alert")
        found = [route]
    return found


@dataclass(eq=False)
class StoredAlert:
    labels: Dict[str, str]
    annotations: Dict[str, str]
    starts_at: float
    ends_at: float
    updated_at: float
    timeout: bool
    generator_url: str = ""

    @property
    def fp(self) -> str:
        return fingerprint(self.labels)

    def resolved_at(self, now: float) -> bool:
        return self.ends_at <= now


@dataclass
class Silence:
    ident: str
    matchers: List[Matcher]
    starts_at: float
    ends_at: float
    comment: str
    created_by: str

    def active(self, now: float) -> bool:
        return self.starts_at <= now < self.ends_at


@dataclass
class LogEntry:
    firing: Set[str]
    resolved: Set[str]
    timestamp: float


@dataclass(eq=False)
class Group:
    route: Route
    labels: Dict[str, str]
    key: str
    alerts: Dict[str, StoredAlert]
    next_flush: float
    created: float
    seq: int


def merge_alerts(old: StoredAlert, new: StoredAlert, now: float) -> StoredAlert:
    if new.updated_at < old.updated_at:
        old, new = new, old
    merged = copy.copy(new)
    if old.starts_at < new.starts_at:
        merged.starts_at = old.starts_at
    if new.resolved_at(now):
        if old.resolved_at(now) and old.ends_at > new.ends_at:
            merged.ends_at = old.ends_at
    elif old.ends_at > new.ends_at and not old.timeout:
        merged.ends_at = old.ends_at
    return merged


def needs_update(entry: Optional[LogEntry], firing: Set[str], resolved: Set[str], repeat: float, now: float, send_resolved: bool) -> str:
    if entry is None:
        return REASON_FIRST if firing else REASON_NONE
    if not firing <= entry.firing:
        return REASON_FIRST if not entry.firing else REASON_NEW_ALERTS
    if not firing:
        return REASON_ALL_RESOLVED if entry.firing else REASON_NONE
    if send_resolved and not resolved <= entry.resolved:
        return REASON_NEW_RESOLVED
    if entry.timestamp < now - repeat:
        return REASON_REPEAT
    return REASON_NONE


def labelset_before(a: Dict[str, str], b: Dict[str, str]) -> bool:
    if len(a) != len(b):
        return len(a) < len(b)
    for name in sorted(set(a) | set(b)):
        if name not in a:
            return True
        if name not in b:
            return False
        if a[name] != b[name]:
            return a[name] < b[name]
    return False


def alert_before(a: Dict[str, str], b: Dict[str, str]) -> bool:
    for name in ("job", "instance"):
        if name not in a and name not in b:
            continue
        if name not in a:
            return False
        if name not in b:
            return True
        if a[name] != b[name]:
            return a[name] < b[name]
    return labelset_before(a, b)


def compare_alerts(left: Tuple[StoredAlert, bool], right: Tuple[StoredAlert, bool]) -> int:
    if alert_before(left[0].labels, right[0].labels):
        return -1
    if alert_before(right[0].labels, left[0].labels):
        return 1
    return 0


def common_map(maps: List[Dict[str, str]]) -> Dict[str, str]:
    if not maps:
        return {}
    common = dict(maps[0])
    for other in maps[1:]:
        common = {k: v for k, v in common.items() if other.get(k) == v}
    return dict(sorted(common.items()))


class Dispatcher:
    def __init__(self, config: Config, origin: float = 0.0, external_url: str = "http://minialert.invalid") -> None:
        self.config = config
        self.origin = origin
        self.external_url = external_url
        self.alerts: Dict[str, StoredAlert] = {}
        self.groups: Dict[Tuple[int, str], Group] = {}
        self.nflog: Dict[Tuple[str, str, str, int], LogEntry] = {}
        self.silences: Dict[str, Silence] = {}
        self.notifications: List[Dict[str, Any]] = []
        self.events: List[Dict[str, Any]] = []
        self.flush_times: List[float] = []
        self.sequence = 0

    def event(self, now: float, kind: str, text: str) -> None:
        self.events.append({"at": round(now - self.origin, 6), "kind": kind, "text": text})

    def post(self, raw_alerts: List[Dict[str, Any]], now: float) -> List[str]:
        stored = []
        for raw in raw_alerts:
            labels = raw.get("labels")
            if not isinstance(labels, dict) or not labels:
                raise InputError(f"alert has no labels (at least one label pair is required): {raw!r}")
            labels = {str(k): str(v) for k, v in labels.items()}
            starts, ends = raw.get("startsAt"), raw.get("endsAt")
            if starts is None:
                starts = ends if ends is not None else now
            timeout = ends is None
            if ends is None:
                ends = now + self.config.resolve_timeout
            if ends < starts:
                raise InputError(f"{brief(labels)}: end time must not be before start time")
            alert = StoredAlert(labels, {str(k): str(v) for k, v in (raw.get("annotations") or {}).items()}, float(starts), float(ends), now, timeout, str(raw.get("generatorURL") or ""))
            old = self.alerts.get(alert.fp)
            if old and ((old.starts_at < alert.ends_at < old.ends_at) or (old.starts_at < alert.starts_at < old.ends_at)):
                alert = merge_alerts(old, alert, now)
            self.alerts[alert.fp] = alert
            routes = self.config.route.match(labels)
            state = "resolved" if alert.resolved_at(now) else "firing"
            self.event(now, "post", f"{state} {brief(labels)} -> {', '.join(r.receiver for r in routes)}")
            for route in routes:
                self.group_alert(alert, route, now)
            stored.append(alert.fp)
        return stored

    def group_alert(self, alert: StoredAlert, route: Route, now: float) -> None:
        labels = group_labels(alert.labels, route)
        slot = (route.idx, fingerprint(labels))
        group = self.groups.get(slot)
        if group is not None:
            group.alerts[alert.fp] = alert
            return
        self.sequence += 1
        group = Group(route, labels, group_key(route, labels), {alert.fp: alert}, now + route.group_wait, now, self.sequence)
        if alert.starts_at + route.group_wait < now:
            group.next_flush = now
        self.groups[slot] = group
        self.event(now, "group", f"new group {route.receiver} {labelset_string(labels)}: first flush at {group.next_flush - self.origin:.1f}s")

    def add_silence(self, ident: str, matchers: List[Matcher], starts: float, ends: float, comment: str = "", created_by: str = "") -> Silence:
        if not matchers:
            raise InputError(f"silence {ident}: at least one matcher is required")
        if ends <= starts:
            raise InputError(f"silence {ident}: start time must be before end time")
        silence = Silence(ident, matchers, starts, ends, comment, created_by)
        self.silences[ident] = silence
        self.event(starts, "silence", f"silence {ident} {matchers_text(matchers)} until {ends - self.origin:.1f}s")
        return silence

    def expire_silence(self, ident: str, now: float) -> None:
        silence = self.silences.get(ident)
        if silence is None:
            raise InputError(f"no silence with id {ident!r}")
        if silence.ends_at < now:
            raise InputError(f"silence {ident} is already expired")
        silence.starts_at = min(silence.starts_at, now)
        silence.ends_at = now
        self.event(now, "silence", f"silence {ident} expired")

    def inhibited_by(self, labels: Dict[str, str], now: float) -> Optional[str]:
        for rule in self.config.inhibit_rules:
            if not all_match(rule.target, labels):
                continue
            both_sides = all_match(rule.source, labels)
            sources = []
            for fp, candidate in self.alerts.items():
                if candidate.resolved_at(now) or not all_match(rule.source, candidate.labels):
                    continue
                if any(candidate.labels.get(n, "") != labels.get(n, "") for n in rule.equal):
                    continue
                if both_sides and all_match(rule.target, candidate.labels):
                    continue
                sources.append(fp)
            if sources:
                return min(sources)
        return None

    def silenced_by(self, labels: Dict[str, str], now: float) -> List[str]:
        return sorted(s.ident for s in self.silences.values() if s.active(now) and all_match(s.matchers, labels))

    def next_flush(self) -> Optional[float]:
        return min((g.next_flush for g in self.groups.values()), default=None)

    def flush_due(self, now: float) -> int:
        due = sorted((g for g in self.groups.values() if g.next_flush <= now + 1e-9), key=lambda g: (g.next_flush, g.seq))
        for group in due:
            self.flush(group, group.next_flush if group.next_flush > now - 1e-9 else now)
        return len(due)

    def flush(self, group: Group, now: float) -> None:
        self.flush_times.append(now)
        group.next_flush = now + group.route.group_interval
        view = sorted(((a, not a.resolved_at(now)) for a in group.alerts.values()), key=functools.cmp_to_key(compare_alerts))
        self.notify(group, view, now)
        for alert, firing in view:
            if not firing and group.alerts.get(alert.fp) is alert:
                del group.alerts[alert.fp]
        if not group.alerts:
            del self.groups[(group.route.idx, fingerprint(group.labels))]
            self.event(now, "group", f"group {group.route.receiver} {labelset_string(group.labels)} is empty after the flush and is destroyed")

    def notify(self, group: Group, view: List[Tuple[StoredAlert, bool]], now: float) -> None:
        receiver = self.config.receivers[group.route.receiver]
        where = f"{receiver.name} {labelset_string(group.labels)}"
        kept = []
        for alert, firing in view:
            source = self.inhibited_by(alert.labels, now)
            if source:
                self.event(now, "inhibited", f"{where}: {brief(alert.labels)} inhibited by {brief(self.alerts[source].labels)}")
                continue
            silences = self.silenced_by(alert.labels, now)
            if silences:
                self.event(now, "silenced", f"{where}: {brief(alert.labels)} silenced by {', '.join(silences)}")
                continue
            kept.append((alert, firing))
        if not kept:
            return
        if not receiver.integrations:
            self.event(now, "blackhole", f"{where}: receiver has no integrations, {len(kept)} alert(s) go nowhere")
            return
        firing_set = {a.fp for a, f in kept if f}
        resolved_set = {a.fp for a, f in kept if not f}
        for integration in receiver.integrations:
            log_key = (group.key, receiver.name, integration.kind, integration.index)
            entry = self.nflog.get(log_key)
            reason = needs_update(entry, firing_set, resolved_set, group.route.repeat_interval, now, integration.send_resolved)
            if reason == REASON_NONE:
                since = f"last notified at {entry.timestamp - self.origin:.1f}s" if entry else "only resolved alerts, never notified"
                self.event(now, "dedup", f"{where}: nothing new ({since})")
                continue
            sendable = kept if integration.send_resolved else [(a, f) for a, f in kept if f]
            if sendable:
                payload = self.render(group, receiver, integration, sendable, reason)
                self.notifications.append({"at": round(now - self.origin, 6), "receiver": receiver.name, "integration": f"{integration.kind}[{integration.index}]", "url": integration.url, "payload": payload})
                names = ", ".join(("" if f else "resolved ") + brief(a.labels) for a, f in sendable)
                self.event(now, "notify", f"NOTIFY {where} {payload['status']} ({reason}): {names}")
            else:
                self.event(now, "notify", f"{where}: only resolved alerts and send_resolved is false, logged but not sent")
            self.nflog[log_key] = LogEntry(set(firing_set), set(resolved_set), now)

    def render(self, group: Group, receiver: Receiver, integration: Integration, items: List[Tuple[StoredAlert, bool]], reason: str) -> Dict[str, Any]:
        visible = items[: integration.max_alerts] if integration.max_alerts else items
        alerts = [
            {
                "status": "firing" if firing else "resolved",
                "labels": dict(sorted(alert.labels.items())),
                "annotations": dict(sorted(alert.annotations.items())),
                "startsAt": rfc3339(alert.starts_at),
                "endsAt": ZERO_TIME if firing else rfc3339(alert.ends_at),
                "generatorURL": alert.generator_url,
                "fingerprint": alert.fp,
            }
            for alert, firing in visible
        ]
        return {
            "receiver": quote_meta(receiver.name),
            "status": "firing" if any(f for _, f in visible) else "resolved",
            "alerts": alerts,
            "notification_reason": reason,
            "groupLabels": dict(sorted(group.labels.items())),
            "commonLabels": common_map([a["labels"] for a in alerts]),
            "commonAnnotations": common_map([a["annotations"] for a in alerts]),
            "routeLabels": {},
            "externalURL": self.external_url,
            "version": "4",
            "groupKey": group.key,
            "truncatedAlerts": len(items) - len(visible),
        }

    def snapshot(self, now: float) -> List[Dict[str, Any]]:
        out = []
        for fp in sorted(self.alerts):
            alert = self.alerts[fp]
            if alert.ends_at < now:
                continue
            source = self.inhibited_by(alert.labels, now)
            silences = self.silenced_by(alert.labels, now)
            out.append(
                {
                    "fingerprint": fp,
                    "labels": dict(sorted(alert.labels.items())),
                    "annotations": dict(sorted(alert.annotations.items())),
                    "startsAt": rfc3339(alert.starts_at),
                    "endsAt": rfc3339(alert.ends_at),
                    "updatedAt": rfc3339(alert.updated_at),
                    "receivers": [{"name": r.receiver} for r in self.config.route.match(alert.labels)],
                    "status": {"state": "suppressed" if source or silences else "active", "inhibitedBy": [source] if source else [], "silencedBy": silences, "mutedBy": []},
                }
            )
        return out


@dataclass
class Step:
    at: float
    kind: str
    alerts: List[Dict[str, Any]] = field(default_factory=list)
    silence: Dict[str, Any] = field(default_factory=dict)
    name: str = ""


def scenario_alert(ref: Any, defined: Dict[str, Dict[str, Any]], where: str) -> Dict[str, Any]:
    if isinstance(ref, str):
        if ref not in defined:
            raise InputError(f"{where}: unknown alert {ref!r} (defined: {', '.join(sorted(defined))})")
        return copy.deepcopy(defined[ref])
    if isinstance(ref, dict) and isinstance(ref.get("labels"), dict):
        return copy.deepcopy(ref)
    raise InputError(f"{where}: an alert is an id from \"alerts\" or an object with labels, got {ref!r}")


def load_scenario(raw: Any) -> Tuple[List[Step], float]:
    if not isinstance(raw, dict) or not isinstance(raw.get("steps"), list):
        raise InputError('a scenario is an object with "alerts" and a list of "steps"')
    defined = {str(k): v for k, v in (raw.get("alerts") or {}).items()}
    steps = []
    for index, entry in enumerate(raw["steps"]):
        where = f"step {index + 1}"
        kinds = [k for k in STEP_KINDS if k in entry]
        if len(kinds) != 1 or "at" not in entry:
            raise InputError(f"{where}: needs \"at\" and exactly one of {', '.join(STEP_KINDS)}, got {entry!r}")
        kind = kinds[0]
        step = Step(parse_duration(entry["at"]), kind)
        if kind in ("post", "resolve"):
            step.alerts = [scenario_alert(ref, defined, where) for ref in entry[kind]]
        elif kind == "silence":
            spec = dict(entry["silence"])
            if not spec.get("id") or not spec.get("matchers"):
                raise InputError(f"{where}: a silence needs an id and matchers")
            spec["parsed"] = [parse_matcher(m) for m in spec["matchers"]]
            spec["seconds"] = parse_duration(spec.get("duration", "1h"))
            step.silence = spec
        else:
            step.name = str(entry[kind])
        steps.append(step)
    last = max((s.at for s in steps), default=0.0)
    until = parse_duration(raw["until"]) if raw.get("until") else last
    return sorted(steps, key=lambda s: s.at), until


def apply_step(dispatcher: Dispatcher, step: Step, now: float, snapshots: List[Dict[str, Any]]) -> None:
    if step.kind == "post":
        dispatcher.post([{k: a.get(k) for k in ("labels", "annotations", "generatorURL")} for a in step.alerts], now)
    elif step.kind == "resolve":
        dispatcher.post([dict({k: a.get(k) for k in ("labels", "annotations", "generatorURL")}, endsAt=now) for a in step.alerts], now)
    elif step.kind == "silence":
        spec = step.silence
        dispatcher.add_silence(str(spec["id"]), spec["parsed"], now, now + spec["seconds"], str(spec.get("comment", "")), str(spec.get("createdBy", "")))
    elif step.kind == "expire":
        dispatcher.expire_silence(step.name, now)
    else:
        snapshots.append({"name": step.name, "at": round(now - dispatcher.origin, 6), "alerts": dispatcher.snapshot(now)})
        dispatcher.event(now, "snapshot", f"snapshot {step.name}")


def simulate(config: Config, raw_scenario: Any, start: str = DEFAULT_START) -> Dict[str, Any]:
    steps, until = load_scenario(raw_scenario)
    origin = parse_time(start) or 0.0
    dispatcher = Dispatcher(config, origin)
    snapshots: List[Dict[str, Any]] = []
    position = 0
    while True:
        step_at = steps[position].at if position < len(steps) else math.inf
        pending = dispatcher.next_flush()
        flush_at = pending - origin if pending is not None else math.inf
        if min(step_at, flush_at) > until + 1e-9:
            break
        if step_at <= flush_at + 1e-9:
            apply_step(dispatcher, steps[position], origin + step_at, snapshots)
            position += 1
        else:
            dispatcher.flush_due(origin + flush_at)
    flushes = [t - origin for t in dispatcher.flush_times]
    changes = [s.at for s in steps if s.kind != "snapshot"]
    margin = min((abs(s - f) for s in changes for f in flushes), default=math.inf)
    return {
        "origin": rfc3339(origin),
        "until": until,
        "min_margin": None if margin == math.inf else round(margin, 6),
        "notifications": dispatcher.notifications,
        "snapshots": snapshots,
        "events": dispatcher.events,
    }


def render_simulation(result: Dict[str, Any]) -> str:
    lines = ["     at  event"]
    for event in result["events"]:
        lines.append(f"{event['at']:6.1f}s  {event['text']}")
    receivers = sorted({n["receiver"] for n in result["notifications"]})
    margin = result["min_margin"]
    lines.append(f"summary: {len(result['notifications'])} notification(s) to {', '.join(receivers) or 'nobody'}; smallest gap between a step and a flush: {'n/a' if margin is None else f'{margin:.1f}s'}")
    return "\n".join(lines)


def http_request(method: str, url: str, body: Any = None, timeout: float = 5.0) -> Tuple[int, str]:
    data = None if body is None else json.dumps(body).encode()
    request = urllib.request.Request(url, data=data, method=method, headers={"Content-Type": "application/json"} if data is not None else {})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(request, timeout=timeout) as response:
            return response.status, response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")


def split_address(text: str) -> Tuple[str, int]:
    host, _, port = text.rpartition(":")
    if not host or not port.isdigit():
        raise InputError(f"not a HOST:PORT address: {text!r} (example: 127.0.0.1:9093)")
    return host, int(port)


def api_matchers(matchers: List[Matcher]) -> List[Dict[str, Any]]:
    return [{"name": m.name, "value": m.value, "isRegex": m.op in ("=~", "!~"), "isEqual": m.op in ("=", "=~")} for m in matchers]


def from_api_matchers(raw: List[Dict[str, Any]]) -> List[Matcher]:
    out = []
    for item in raw or []:
        regex, equal = bool(item.get("isRegex")), item.get("isEqual", True) is not False
        op = {(False, True): "=", (False, False): "!=", (True, True): "=~", (True, False): "!~"}[(regex, equal)]
        if regex:
            anchored(str(item.get("value", "")))
        out.append(Matcher(str(item["name"]), op, str(item.get("value", ""))))
    return out


class LiveAlertmanager:
    def __init__(self, config: Config, clock: Callable[[], float] = time.time, send: Callable[[str, Any], Tuple[int, str]] = lambda url, body: http_request("POST", url, body), tick: float = 0.02) -> None:
        self.clock = clock
        self.dispatcher = Dispatcher(config, origin=clock(), external_url="http://minialert.invalid")
        self.lock = threading.Lock()
        self.send = send
        self.tick = tick
        self.stopped = threading.Event()

    def flush(self) -> None:
        with self.lock:
            before = len(self.dispatcher.notifications)
            self.dispatcher.flush_due(self.clock())
            fresh = self.dispatcher.notifications[before:]
        for note in fresh:
            if not note["url"]:
                continue
            try:
                self.send(note["url"], note["payload"])
            except OSError as exc:
                print(f"minialert: webhook {note['url']} failed: {exc}", file=sys.stderr)

    def loop(self) -> None:
        while not self.stopped.wait(self.tick):
            self.flush()

    def post_alerts(self, body: Any) -> None:
        if not isinstance(body, list):
            raise InputError("POST /api/v2/alerts expects a JSON list of alerts")
        parsed = [{"labels": a.get("labels"), "annotations": a.get("annotations"), "generatorURL": a.get("generatorURL"), "startsAt": parse_time(a.get("startsAt")), "endsAt": parse_time(a.get("endsAt"))} for a in body]
        with self.lock:
            self.dispatcher.post(parsed, self.clock())

    def get_alerts(self) -> List[Dict[str, Any]]:
        with self.lock:
            return self.dispatcher.snapshot(self.clock())

    def post_silence(self, body: Dict[str, Any]) -> str:
        now = self.clock()
        starts = parse_time(body.get("startsAt")) or now
        ends = parse_time(body.get("endsAt"))
        if ends is None or ends < now:
            raise InputError("Failed to create silence: end time can't be in the past")
        ident = str(body.get("id") or uuid.uuid4())
        with self.lock:
            self.dispatcher.add_silence(ident, from_api_matchers(body.get("matchers")), max(starts, now), ends, str(body.get("comment", "")), str(body.get("createdBy", "")))
        return ident

    def delete_silence(self, ident: str) -> None:
        with self.lock:
            self.dispatcher.expire_silence(ident, self.clock())


def reply(handler: BaseHTTPRequestHandler, status: int, body: Any = None) -> None:
    data = b"" if body is None else (body.encode() if isinstance(body, str) else json.dumps(body).encode())
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json" if not isinstance(body, str) else "text/plain")
    handler.send_header("Content-Length", str(len(data)))
    handler.end_headers()
    handler.wfile.write(data)


def read_body(handler: BaseHTTPRequestHandler) -> Any:
    length = int(handler.headers.get("Content-Length") or 0)
    raw = handler.rfile.read(length) if length else b""
    return json.loads(raw.decode("utf-8") or "null")


def api_handler(live: LiveAlertmanager) -> type:
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args: Any) -> None:
            pass

        def guarded(self, action: Callable[[], None]) -> None:
            try:
                action()
            except (InputError, json.JSONDecodeError, KeyError, TypeError, AttributeError) as exc:
                reply(self, 400, {"code": 400, "message": str(exc)})

        def do_GET(self) -> None:
            path = self.path.split("?")[0]
            if path in ("/-/ready", "/-/healthy"):
                reply(self, 200, "OK")
            elif path == "/api/v2/alerts":
                reply(self, 200, live.get_alerts())
            elif path == "/api/v2/silences":
                with live.lock:
                    silences = list(live.dispatcher.silences.values())
                now = live.clock()
                reply(self, 200, [{"id": s.ident, "matchers": api_matchers(s.matchers), "startsAt": rfc3339(s.starts_at), "endsAt": rfc3339(s.ends_at), "comment": s.comment, "createdBy": s.created_by, "status": {"state": "active" if s.active(now) else ("pending" if s.starts_at > now else "expired")}} for s in silences])
            else:
                reply(self, 404, {"code": 404, "message": f"no route {path}"})

        def do_POST(self) -> None:
            path = self.path.split("?")[0]
            if path == "/api/v2/alerts":
                self.guarded(lambda: (live.post_alerts(read_body(self)), reply(self, 200)))
            elif path == "/api/v2/silences":
                self.guarded(lambda: reply(self, 200, {"silenceID": live.post_silence(read_body(self))}))
            else:
                reply(self, 404, {"code": 404, "message": f"no route {path}"})

        def do_DELETE(self) -> None:
            path = self.path.split("?")[0]
            if path.startswith("/api/v2/silence/"):
                self.guarded(lambda: (live.delete_silence(path.rsplit("/", 1)[1]), reply(self, 200)))
            else:
                reply(self, 404, {"code": 404, "message": f"no route {path}"})

    return Handler


def start_live(config: Config, address: str) -> Tuple[ThreadingHTTPServer, LiveAlertmanager]:
    host, port = split_address(address)
    live = LiveAlertmanager(config)
    server = ThreadingHTTPServer((host, port), api_handler(live))
    server.daemon_threads = True
    threading.Thread(target=live.loop, daemon=True).start()
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, live


def receiver_handler(out_path: str, lock: threading.Lock, clock: Callable[[], float] = time.time) -> type:
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args: Any) -> None:
            pass

        def do_POST(self) -> None:
            arrived = clock()
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b""
            try:
                body: Any = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                body = raw.decode("utf-8", "replace")
            with lock:
                with open(out_path, "a", encoding="utf-8") as handle:
                    handle.write(json.dumps({"at": arrived, "path": self.path, "body": body}) + "\n")
            reply(self, 200, "ok")

        def do_GET(self) -> None:
            count = 0
            with lock:
                if os.path.exists(out_path):
                    with open(out_path, encoding="utf-8") as handle:
                        count = sum(1 for _ in handle)
            reply(self, 200, {"received": count})

    return Handler


def start_receiver(address: str, out_path: str) -> ThreadingHTTPServer:
    host, port = split_address(address)
    open(out_path, "a", encoding="utf-8").close()
    server = ThreadingHTTPServer((host, port), receiver_handler(out_path, threading.Lock()))
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def api_alert(alert: Dict[str, Any], ends: Optional[float]) -> Dict[str, Any]:
    body: Dict[str, Any] = {"labels": alert["labels"], "annotations": alert.get("annotations") or {}}
    if alert.get("generatorURL"):
        body["generatorURL"] = alert["generatorURL"]
    if ends is not None:
        body["endsAt"] = rfc3339(ends)
    return body


def drive(
    raw_scenario: Any,
    base_url: str,
    amtool: Optional[str] = None,
    clock: Callable[[], float] = time.time,
    sleep: Callable[[float], None] = time.sleep,
    request: Callable[..., Tuple[int, str]] = http_request,
    run: Callable[..., Any] = subprocess.run,
) -> Dict[str, Any]:
    steps, until = load_scenario(raw_scenario)
    base = base_url.rstrip("/")
    t0 = clock()
    record: Dict[str, Any] = {"t0": t0, "alertmanager": base, "steps": [], "snapshots": [], "silences": {}}

    def checked(method: str, path: str, body: Any = None) -> str:
        status, text = request(method, base + path, body)
        if status != 200:
            raise InputError(f"{method} {path} answered {status}: {text.strip()}")
        return text

    def tool(args: List[str]) -> str:
        argv = shlex.split(amtool or "") + args
        result = run(argv, capture_output=True, text=True)
        if result.returncode != 0:
            raise InputError(f"{' '.join(argv)} exited {result.returncode}: {(result.stderr or result.stdout).strip()}")
        return result.stdout.strip()

    for step in steps:
        delay = t0 + step.at - clock()
        if delay > 0:
            sleep(delay)
        now = clock()
        detail: Dict[str, Any] = {"at": step.at, "actual": round(now - t0, 3), "kind": step.kind}
        if step.kind in ("post", "resolve"):
            checked("POST", "/api/v2/alerts", [api_alert(a, now if step.kind == "resolve" else None) for a in step.alerts])
            detail["alerts"] = [fingerprint({str(k): str(v) for k, v in a["labels"].items()}) for a in step.alerts]
        elif step.kind == "silence":
            spec = step.silence
            if amtool:
                output = tool(["silence", "add", *[str(m) for m in spec["matchers"]], f"--duration={spec.get('duration', '1h')}", f"--comment={spec.get('comment') or 'minialert drive'}", f"--author={spec.get('createdBy') or 'minialert'}"])
                ident = output.splitlines()[-1].strip() if output else ""
            else:
                body = {"matchers": api_matchers(spec["parsed"]), "startsAt": rfc3339(now), "endsAt": rfc3339(now + spec["seconds"]), "createdBy": spec.get("createdBy") or "minialert", "comment": spec.get("comment") or "minialert drive"}
                ident = str(json.loads(checked("POST", "/api/v2/silences", body))["silenceID"])
            if not ident:
                raise InputError(f"silence {spec['id']}: no silence id returned")
            record["silences"][str(spec["id"])] = ident
            detail["silence"] = ident
        elif step.kind == "expire":
            ident = record["silences"].get(step.name)
            if ident is None:
                raise InputError(f"expire {step.name}: that silence was never created")
            if amtool:
                tool(["silence", "expire", ident])
            else:
                checked("DELETE", f"/api/v2/silence/{ident}")
        else:
            record["snapshots"].append({"name": step.name, "at": round(clock() - t0, 3), "alerts": json.loads(checked("GET", "/api/v2/alerts"))})
        record["steps"].append(detail)
    remaining = t0 + until - clock()
    if remaining > 0:
        sleep(remaining)
    return record


def notification_content(payload: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "status": payload.get("status"),
        "groupLabels": payload.get("groupLabels") or {},
        "commonLabels": payload.get("commonLabels") or {},
        "alerts": sorted((json.dumps(a.get("labels") or {}, sort_keys=True), a.get("status")) for a in payload.get("alerts") or []),
        "fingerprints": sorted(a.get("fingerprint") for a in payload.get("alerts") or []),
    }


def compare_runs(simulated: Dict[str, Any], driven: Dict[str, Any], hooks: List[Dict[str, Any]], tolerance: float = 1.0) -> Tuple[List[str], List[str]]:
    expected: Dict[Tuple[str, str], List[Tuple[float, Dict[str, Any]]]] = {}
    for note in simulated["notifications"]:
        payload = note["payload"]
        expected.setdefault((payload["receiver"], payload["groupKey"]), []).append((float(note["at"]), payload))
    actual: Dict[Tuple[str, str], List[Tuple[float, Dict[str, Any]]]] = {}
    for hook in hooks:
        body = hook["body"]
        if isinstance(body, dict) and "groupKey" in body:
            actual.setdefault((body.get("receiver", ""), body["groupKey"]), []).append((float(hook["at"]) - float(driven["t0"]), body))
    lines = [f"{'sim':>6} {'real':>6}  {'receiver':<13} {'status':<9} {'reason':<24} alerts  group"]
    problems = []
    for key in sorted(set(expected) | set(actual)):
        want = sorted(expected.get(key, []), key=lambda x: x[0])
        got = sorted(actual.get(key, []), key=lambda x: x[0])
        for i in range(max(len(want), len(got))):
            if i >= len(got):
                problems.append(f"missing: {key[0]} {key[1]} notification {i + 1} ({want[i][1]['status']}, {want[i][1].get('notification_reason')}) expected at {want[i][0]:.1f}s")
                continue
            if i >= len(want):
                problems.append(f"unexpected: {key[0]} {key[1]} notification {i + 1} ({got[i][1].get('status')}, {got[i][1].get('notification_reason')}) at {got[i][0]:.1f}s")
                continue
            (sim_at, sim_payload), (real_at, real_payload) = want[i], got[i]
            mine, theirs = notification_content(sim_payload), notification_content(real_payload)
            diffs = [name for name in mine if mine[name] != theirs[name]]
            if sim_payload.get("notification_reason") and real_payload.get("notification_reason") and sim_payload["notification_reason"] != real_payload["notification_reason"]:
                diffs.append("notification_reason")
            if abs(sim_at - real_at) > tolerance:
                diffs.append(f"time ({sim_at:.1f}s vs {real_at:.1f}s)")
            lines.append(f"{sim_at:5.1f}s {real_at:5.1f}s  {key[0]:<13} {mine['status']:<9} {str(sim_payload.get('notification_reason')):<24} {len(mine['alerts']):>6}  {key[1]}" + (f"  DIFF: {', '.join(diffs)}" if diffs else ""))
            if diffs:
                problems.append(f"{key[0]} {key[1]} notification {i + 1}: {', '.join(diffs)}; minialert {json.dumps(mine)}; real {json.dumps(theirs)}")
    real_ids = {str(v): str(k) for k, v in (driven.get("silences") or {}).items()}
    real_snaps = {s["name"]: s for s in driven.get("snapshots") or []}
    for snap in simulated["snapshots"]:
        theirs = real_snaps.get(snap["name"])
        if theirs is None:
            problems.append(f"snapshot {snap['name']}: missing from the driven run")
            continue
        def summary(alerts: List[Dict[str, Any]], mapping: Dict[str, str]) -> Dict[str, Tuple[Any, ...]]:
            return {
                a["fingerprint"]: (
                    tuple(r["name"] for r in a.get("receivers") or []),
                    a["status"]["state"],
                    tuple(sorted(a["status"].get("inhibitedBy") or [])),
                    tuple(sorted(mapping.get(s, s) for s in a["status"].get("silencedBy") or [])),
                )
                for a in alerts
            }
        mine, real = summary(snap["alerts"], {}), summary(theirs["alerts"], real_ids)
        suppressed = sum(1 for v in mine.values() if v[1] == "suppressed")
        lines.append(f"snapshot {snap['name']}: {len(mine)} active alert(s), {suppressed} suppressed in minialert; {len(real)} and {sum(1 for v in real.values() if v[1] == 'suppressed')} in the driven run")
        for fp in sorted(set(mine) | set(real)):
            if mine.get(fp) != real.get(fp):
                problems.append(f"snapshot {snap['name']} alert {fp}: minialert {mine.get(fp)} vs real {real.get(fp)}")
    return lines, problems


SERIES_SPEC = re.compile(r"^\s*([a-zA-Z_:][a-zA-Z0-9_:]*)?\s*(?:\{(.*)\})?\s*$", re.S)
SERIES_LABEL = re.compile(r'\s*([a-zA-Z_][a-zA-Z0-9_]*)\s*=\s*"((?:[^"\\]|\\.)*)"\s*(?:,|$)')
NUMBER_TEXT = r"(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?"
EXPANSION = re.compile(rf"^([+-]?{NUMBER_TEXT}|[+-]?Inf|NaN)(?:([+-])({NUMBER_TEXT}))?x(\d+)$")
SINGLE = re.compile(rf"^([+-]?{NUMBER_TEXT}|[+-]?Inf|NaN)$")
MISSING = "_"
STALE = "stale"


def parse_series_spec(text: str) -> Dict[str, str]:
    match = SERIES_SPEC.match(text)
    if not match or (not match.group(1) and match.group(2) is None):
        raise InputError(f"not a series: {text!r} (example: up{{job=\"api\", instance=\"a\"}})")
    labels: Dict[str, str] = {}
    if match.group(1):
        labels["__name__"] = match.group(1)
    body = match.group(2) or ""
    position = 0
    while body[position:].strip():
        item = SERIES_LABEL.match(body, position)
        if not item:
            raise InputError(f"cannot parse labels in series {text!r} near {body[position:]!r}")
        labels[item.group(1)] = json.loads('"' + item.group(2) + '"')
        position = item.end()
    return labels


def expand_values(text: str) -> List[Any]:
    out: List[Any] = []
    for token in text.split():
        if token in (MISSING, STALE):
            out.append(token)
            continue
        blanks = re.match(r"^_x(\d+)$", token)
        if blanks:
            out.extend([MISSING] * int(blanks.group(1)))
            continue
        single = SINGLE.match(token)
        if single:
            out.append(float(single.group(1)))
            continue
        expansion = EXPANSION.match(token)
        if not expansion:
            raise InputError(f"cannot expand series value {token!r} (examples: 1, 1x4, 0+10x5, 5-1x3, _, _x3, stale)")
        start, sign, step, times = expansion.groups()
        value, delta = float(start), (float(step) * (-1 if sign == "-" else 1)) if step else 0.0
        for _ in range(int(times) + 1):
            out.append(value)
            value += delta
    return out


@dataclass
class SeriesStore:
    series: Dict[FrozenSet[Tuple[str, str]], List[Tuple[float, Optional[float]]]] = field(default_factory=dict)

    def add(self, labels: Dict[str, str], at: float, value: Optional[float]) -> None:
        self.series.setdefault(frozenset(labels.items()), []).append((at, value))

    def latest(self, matchers: Sequence[Matcher], at: float) -> List[Tuple[Dict[str, str], float]]:
        out = []
        for key, points in self.series.items():
            labels = dict(key)
            if not all(promql_match(m, labels) for m in matchers):
                continue
            window = [p for p in points if at - LOOKBACK_DELTA < p[0] <= at]
            if window and window[-1][1] is not None:
                out.append((labels, window[-1][1]))
        return out

    def ranged(self, matchers: Sequence[Matcher], at: float, width: float) -> List[Tuple[Dict[str, str], List[float]]]:
        out = []
        for key, points in self.series.items():
            labels = dict(key)
            if not all(promql_match(m, labels) for m in matchers):
                continue
            values = [v for t, v in points if at - width < t <= at and v is not None]
            if values:
                out.append((labels, values))
        return out


def promql_match(matcher: Matcher, labels: Dict[str, str]) -> bool:
    actual = labels.get(matcher.name, "")
    if matcher.op == "=":
        return actual == matcher.value
    if matcher.op == "!=":
        return actual != matcher.value
    found = anchored(matcher.value, True).fullmatch(actual) is not None
    return found if matcher.op == "=~" else not found


TOKEN = re.compile(
    r"""\s*(?:
    (?P<number>(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?|(?:Inf|NaN)(?![a-zA-Z0-9_:]))
   |(?P<range>\[[^\]]*\])
   |(?P<string>"(?:[^"\\]|\\.)*"|'(?:[^'\\]|\\.)*')
   |(?P<ident>[a-zA-Z_:][a-zA-Z0-9_:]*)
   |(?P<op>==|!=|>=|<=|=~|!~|[-+*/%^(){},=<>])
    )""",
    re.X,
)
AGGREGATIONS = ("sum", "avg", "min", "max", "count")
OVER_TIME = ("avg_over_time", "max_over_time", "min_over_time", "sum_over_time", "count_over_time", "last_over_time")
COMPARISONS = ("==", "!=", ">", "<", ">=", "<=")
UNSUPPORTED = ("and", "or", "unless", "offset", "on", "ignoring", "group_left", "group_right", "rate", "irate", "increase", "histogram_quantile", "topk", "bottomk")


@dataclass
class Node:
    kind: str
    value: Any = None
    args: List["Node"] = field(default_factory=list)
    matchers: List[Matcher] = field(default_factory=list)
    width: Optional[float] = None
    grouping: List[str] = field(default_factory=list)
    without: bool = False
    bool_mod: bool = False


class ExprParser:
    def __init__(self, text: str) -> None:
        self.text = text
        self.tokens: List[Tuple[str, str]] = []
        position = 0
        while position < len(text):
            if not text[position:].strip():
                break
            match = TOKEN.match(text, position)
            if not match or match.end() == position:
                raise InputError(f"cannot parse expression {text!r} at {text[position:]!r}")
            kind = match.lastgroup or ""
            self.tokens.append((kind, match.group(kind)))
            position = match.end()
        self.position = 0

    def peek(self) -> Tuple[str, str]:
        return self.tokens[self.position] if self.position < len(self.tokens) else ("end", "")

    def take(self, value: Optional[str] = None) -> Tuple[str, str]:
        token = self.peek()
        if value is not None and token[1] != value:
            raise InputError(f"expected {value!r} in {self.text!r}, found {token[1] or 'the end'!r}")
        self.position += 1
        return token

    def parse(self) -> Node:
        node = self.comparison()
        if self.peek()[0] != "end":
            raise InputError(f"unexpected {self.peek()[1]!r} in {self.text!r}")
        return node

    def comparison(self) -> Node:
        node = self.additive()
        while self.peek()[1] in COMPARISONS:
            op = self.take()[1]
            bool_mod = False
            if self.peek() == ("ident", "bool"):
                self.take()
                bool_mod = True
            node = Node("binary", op, [node, self.additive()], bool_mod=bool_mod)
        return node

    def additive(self) -> Node:
        node = self.multiplicative()
        while self.peek()[1] in ("+", "-"):
            op = self.take()[1]
            node = Node("binary", op, [node, self.multiplicative()])
        return node

    def multiplicative(self) -> Node:
        node = self.unary()
        while self.peek()[1] in ("*", "/", "%"):
            op = self.take()[1]
            node = Node("binary", op, [node, self.unary()])
        return node

    def unary(self) -> Node:
        if self.peek()[1] in ("-", "+"):
            op = self.take()[1]
            inner = self.unary()
            return inner if op == "+" else Node("binary", "*", [Node("number", -1.0), inner])
        node = self.primary()
        while self.peek()[1] == "^":
            self.take()
            node = Node("binary", "^", [node, self.unary()])
        return node

    def grouping(self) -> Tuple[List[str], bool]:
        without = self.take()[1] == "without"
        self.take("(")
        names = []
        while self.peek()[1] != ")":
            names.append(self.take()[1])
            if self.peek()[1] == ",":
                self.take()
        self.take(")")
        return names, without

    def primary(self) -> Node:
        kind, value = self.peek()
        if kind == "number":
            self.take()
            return Node("number", float(value))
        if value == "(":
            self.take()
            node = self.comparison()
            self.take(")")
            return node
        if value == "{":
            return self.selector("")
        if kind != "ident":
            raise InputError(f"unexpected {value or 'end of expression'!r} in {self.text!r}")
        if value in UNSUPPORTED:
            raise InputError(f"{value} is not in minialert's PromQL subset (selectors, comparisons, arithmetic, sum/avg/min/max/count by|without, absent, *_over_time); the PromQL lesson's miniql has it")
        if value in AGGREGATIONS:
            self.take()
            names, without = self.grouping() if self.peek()[1] in ("by", "without") else ([], False)
            self.take("(")
            inner = self.comparison()
            self.take(")")
            if self.peek()[1] in ("by", "without"):
                names, without = self.grouping()
            return Node("aggregate", value, [inner], grouping=names, without=without)
        if value == "absent" or value in OVER_TIME:
            self.take()
            self.take("(")
            inner = self.comparison()
            self.take(")")
            if value in OVER_TIME and (inner.kind != "selector" or inner.width is None):
                raise InputError(f"{value} needs a range selector such as metric[5m] in {self.text!r}")
            return Node("call", value, [inner])
        self.take()
        return self.selector(value)

    def selector(self, name: str) -> Node:
        matchers = [Matcher("__name__", "=", name)] if name else []
        if self.peek()[1] == "{":
            self.take()
            while self.peek()[1] != "}":
                label = self.take()[1]
                op = self.take()[1]
                if op not in OP_RANK:
                    raise InputError(f"bad matcher operator {op!r} in {self.text!r}")
                text = self.take()[1]
                value = json.loads(text) if text.startswith('"') else text[1:-1]
                if op in ("=~", "!~"):
                    anchored(value, True)
                matchers.append(Matcher(label, op, value))
                if self.peek()[1] == ",":
                    self.take()
            self.take("}")
        if not matchers:
            raise InputError(f"a selector needs a metric name or at least one matcher in {self.text!r}")
        width = None
        if self.peek()[0] == "range":
            width = parse_duration(self.take()[1][1:-1])
        return Node("selector", name, matchers=matchers, width=width)


def parse_expr(text: str) -> Node:
    return ExprParser(text).parse()


Vector = List[Tuple[Dict[str, str], float]]


def without_name(labels: Dict[str, str]) -> Dict[str, str]:
    return {k: v for k, v in labels.items() if k != "__name__"}


def arithmetic(op: str, a: float, b: float) -> float:
    if op == "+":
        return a + b
    if op == "-":
        return a - b
    if op == "*":
        return a * b
    if op == "/":
        if b == 0:
            return math.nan if a == 0 or math.isnan(a) else math.copysign(math.inf, a)
        return a / b
    if op == "%":
        return math.fmod(a, b) if b != 0 else math.nan
    try:
        return math.pow(a, b)
    except (OverflowError, ValueError):
        return math.nan


def compare(op: str, a: float, b: float) -> bool:
    return {"==": a == b, "!=": a != b, ">": a > b, "<": a < b, ">=": a >= b, "<=": a <= b}[op]


def evaluate(node: Node, store: SeriesStore, at: float) -> Any:
    if node.kind == "number":
        return node.value
    if node.kind == "selector":
        if node.width is not None:
            raise InputError("a range selector can only be the argument of an *_over_time function")
        return store.latest(node.matchers, at)
    if node.kind == "call":
        inner = node.args[0]
        if node.value == "absent":
            found = evaluate(inner, store, at)
            if found:
                return []
            labels = {m.name: m.value for m in inner.matchers if m.op == "=" and m.name != "__name__"} if inner.kind == "selector" else {}
            return [(labels, 1.0)]
        out: Vector = []
        for labels, values in store.ranged(inner.matchers, at, inner.width or 0.0):
            reduce = {
                "avg_over_time": lambda v: sum(v) / len(v),
                "max_over_time": max,
                "min_over_time": min,
                "sum_over_time": sum,
                "count_over_time": lambda v: float(len(v)),
                "last_over_time": lambda v: v[-1],
            }[node.value]
            out.append((without_name(labels), float(reduce(values))))
        return out
    if node.kind == "aggregate":
        vector = evaluate(node.args[0], store, at)
        if not isinstance(vector, list):
            raise InputError(f"{node.value} expects an instant vector")
        buckets: Dict[FrozenSet[Tuple[str, str]], List[float]] = {}
        for labels, value in vector:
            if node.without:
                key = {k: v for k, v in labels.items() if k not in node.grouping and k != "__name__"}
            else:
                key = {k: labels[k] for k in node.grouping if k in labels}
            buckets.setdefault(frozenset(key.items()), []).append(value)
        reducers = {"sum": sum, "avg": lambda v: sum(v) / len(v), "min": min, "max": max, "count": lambda v: float(len(v))}
        return [(dict(key), float(reducers[node.value](values))) for key, values in buckets.items()]
    lhs, rhs = evaluate(node.args[0], store, at), evaluate(node.args[1], store, at)
    return binary(node.value, lhs, rhs, node.bool_mod)


def binary(op: str, lhs: Any, rhs: Any, bool_mod: bool) -> Any:
    is_comparison = op in COMPARISONS
    if not isinstance(lhs, list) and not isinstance(rhs, list):
        if is_comparison:
            if not bool_mod:
                raise InputError("comparisons between scalars must use the bool modifier")
            return 1.0 if compare(op, lhs, rhs) else 0.0
        return arithmetic(op, lhs, rhs)
    pairs: List[Tuple[Dict[str, str], float, float]] = []
    if isinstance(lhs, list) and isinstance(rhs, list):
        index: Dict[FrozenSet[Tuple[str, str]], float] = {}
        for labels, value in rhs:
            signature = frozenset(without_name(labels).items())
            if signature in index:
                raise InputError("found duplicate series for the match group on the right-hand side; many-to-many matching is not in the subset")
            index[signature] = value
        for labels, value in lhs:
            signature = frozenset(without_name(labels).items())
            if signature in index:
                pairs.append((labels, value, index[signature]))
        vector_side = 0
    elif isinstance(lhs, list):
        pairs = [(labels, value, rhs) for labels, value in lhs]
        vector_side = 0
    else:
        pairs = [(labels, lhs, value) for labels, value in rhs]
        vector_side = 1
    out: Vector = []
    for labels, a, b in pairs:
        if is_comparison:
            hit = compare(op, a, b)
            if bool_mod:
                out.append((without_name(labels), 1.0 if hit else 0.0))
            elif hit:
                out.append((labels, a if vector_side == 0 else b))
        else:
            out.append((without_name(labels), arithmetic(op, a, b)))
    return out


ACTION = re.compile(r"\{\{(-?)(.*?)(-?)\}\}", re.S)
GO_VERB = re.compile(r"%([-+# 0]*)(\d+)?(?:\.(\d+))?([vsdfgeqxX%])")


def go_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float):
        return go_float(value)
    if isinstance(value, dict):
        return "map[" + " ".join(f"{k}:{v}" for k, v in sorted(value.items())) + "]"
    return str(value)


def go_g4(value: float) -> str:
    if math.isnan(value):
        return "NaN"
    if math.isinf(value):
        return "+Inf" if value > 0 else "-Inf"
    return "%.4g" % value


def to_float(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError) as exc:
        raise TemplateError(f"cannot convert {value!r} to a number") from exc


def humanize(value: Any) -> str:
    v = to_float(value)
    if v == 0 or math.isnan(v) or math.isinf(v):
        return go_g4(v)
    prefix = ""
    if abs(v) >= 1:
        for p in ("k", "M", "G", "T", "P", "E", "Z", "Y"):
            if abs(v) < 1000:
                break
            prefix, v = p, v / 1000
        return go_g4(v) + prefix
    for p in ("m", "u", "n", "p", "f", "a", "z", "y"):
        if abs(v) >= 1:
            break
        prefix, v = p, v * 1000
    return go_g4(v) + prefix


def go_printf(fmt: str, *args: Any) -> str:
    queue = list(args)
    out = []
    position = 0
    for match in GO_VERB.finditer(fmt):
        out.append(fmt[position:match.start()])
        position = match.end()
        flags, width, precision, verb = match.groups()
        if verb == "%":
            out.append("%")
            continue
        if not queue:
            out.append(f"%!{verb}(MISSING)")
            continue
        arg = queue.pop(0)
        spec = "%" + (flags or "") + (width or "") + (f".{precision}" if precision is not None else "")
        if verb in ("v", "s") or (verb == "g" and precision is None):
            text = go_value(arg)
            out.append((spec + "s") % text)
        elif verb == "q":
            out.append(go_quote(str(arg)))
        elif verb == "d":
            number = to_float(arg)
            out.append((spec + "d") % int(number) if number == int(number) else f"%!d(float64={go_float(number)})")
        elif verb in ("x", "X"):
            out.append((spec + verb) % int(to_float(arg)))
        else:
            out.append((spec + verb) % to_float(arg))
    out.append(fmt[position:])
    if queue:
        out.append("%!(EXTRA " + ", ".join(go_value(a) for a in queue) + ")")
    return "".join(out)


def split_outside_quotes(text: str, separator: str) -> List[str]:
    parts, current, quote = [], [], ""
    escaped = False
    for char in text:
        if quote:
            current.append(char)
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = ""
        elif char in ('"', "`"):
            quote = char
            current.append(char)
        elif (separator == " " and char.isspace()) or char == separator:
            parts.append("".join(current))
            current = []
        else:
            current.append(char)
    if quote:
        raise TemplateError(f"unterminated quoted string in {text!r}")
    parts.append("".join(current))
    return [p.strip() for p in parts if p.strip()] if separator == " " else [p.strip() for p in parts]


TEMPLATE_FUNCTIONS: Dict[str, Callable[..., Any]] = {
    "humanize": humanize,
    "humanizePercentage": lambda v: go_g4(to_float(v) * 100) + "%",
    "printf": lambda fmt, *args: go_printf(str(fmt), *args),
    "toUpper": lambda s: str(s).upper(),
    "toLower": lambda s: str(s).lower(),
    "index": lambda mapping, key: mapping.get(str(key), "") if isinstance(mapping, dict) else "",
}


def template_operand(token: str, env: Dict[str, Any]) -> Any:
    if token.startswith('"'):
        return json.loads(token)
    if token.startswith("`") and token.endswith("`"):
        return token[1:-1]
    if re.match(r"^[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?$", token):
        return float(token)
    aliases = {".Labels": "$labels", ".Value": "$value", ".ExternalLabels": "$externalLabels", ".ExternalURL": "$externalURL"}
    for long_name, short in aliases.items():
        if token == long_name or token.startswith(long_name + "."):
            token = short + token[len(long_name):]
    root, _, attribute = token.partition(".")
    if root not in env:
        raise TemplateError(f'undefined variable "{root}"')
    value = env[root]
    if attribute:
        if not isinstance(value, dict):
            raise TemplateError(f"can't evaluate field {attribute} in {root}")
        return value.get(attribute, "")
    return value


def run_pipeline(action: str, env: Dict[str, Any]) -> Any:
    result: Any = None
    for index, command in enumerate(split_outside_quotes(action, "|")):
        words = split_outside_quotes(command, " ")
        if not words:
            raise TemplateError(f"missing value for command in {action!r}")
        head, rest = words[0], words[1:]
        if head in TEMPLATE_FUNCTIONS:
            args = [template_operand(w, env) for w in rest] + ([result] if index > 0 else [])
            try:
                result = TEMPLATE_FUNCTIONS[head](*args)
            except TypeError as exc:
                raise TemplateError(f"wrong number of args for {head}: {exc}") from exc
        elif head.startswith(("$", ".", '"', "`")) or re.match(r"^[+-]?[\d.]", head):
            if rest or index > 0:
                raise TemplateError(f"{head} is not a function in {action!r}")
            result = template_operand(head, env)
        else:
            raise TemplateError(f'function "{head}" not defined')
    return result


def expand_template(text: str, labels: Dict[str, str], value: float, external_labels: Optional[Dict[str, str]] = None, external_url: str = "") -> str:
    env = {"$labels": labels, "$value": value, "$externalLabels": external_labels or {}, "$externalURL": external_url}
    out: List[str] = []
    position, trim_next = 0, False
    try:
        for match in ACTION.finditer(text):
            literal = text[position:match.start()]
            if trim_next:
                literal = literal.lstrip()
            if match.group(1) == "-":
                literal = literal.rstrip()
            out.append(literal)
            out.append(go_value(run_pipeline(match.group(2).strip(), env)))
            trim_next = match.group(3) == "-"
            position = match.end()
        tail = text[position:]
        out.append(tail.lstrip() if trim_next else tail)
    except (TemplateError, json.JSONDecodeError, ValueError) as exc:
        return f"<error expanding template: {exc}>"
    return "".join(out)


@dataclass
class ActiveAlert:
    labels: Dict[str, str]
    annotations: Dict[str, str]
    value: float
    active_at: float
    state: str = PENDING
    fired_at: Optional[float] = None
    resolved_at: Optional[float] = None
    keep_firing_since: Optional[float] = None
    last_sent_at: Optional[float] = None

    def display(self) -> str:
        if self.state == FIRING and self.keep_firing_since is not None:
            return "firing (kept)"
        if self.state == INACTIVE:
            return "resolved" if self.fired_at is not None else "inactive"
        return self.state


@dataclass
class Rule:
    kind: str
    name: str
    expr_text: str
    expr: Node
    hold: float = 0.0
    keep_firing_for: float = 0.0
    labels: Dict[str, str] = field(default_factory=dict)
    annotations: Dict[str, str] = field(default_factory=dict)
    active: Dict[FrozenSet[Tuple[str, str]], ActiveAlert] = field(default_factory=dict)
    last_output: Set[FrozenSet[Tuple[str, str]]] = field(default_factory=set)

    def evaluate(self, store: SeriesStore, at: float, external_labels: Dict[str, str]) -> None:
        result = evaluate(self.expr, store, at)
        if not isinstance(result, list):
            result = [({}, float(result))]
        if self.kind == "record":
            produced = set()
            for labels, value in result:
                out = dict(without_name(labels), **self.labels, __name__=self.name)
                key = frozenset(out.items())
                if key in produced:
                    raise InputError(f"record {self.name}: vector contains metrics with the same labelset after applying rule labels")
                produced.add(key)
                store.add(out, at, value)
            for key in self.last_output - produced:
                store.add(dict(key), at, None)
            self.last_output = produced
            return
        fresh: Dict[FrozenSet[Tuple[str, str]], ActiveAlert] = {}
        for labels, value in result:
            built = without_name(labels)
            for name, template in self.labels.items():
                built[name] = expand_template(template, labels, value, external_labels)
            built["alertname"] = self.name
            notes = {name: expand_template(template, labels, value, external_labels) for name, template in self.annotations.items()}
            key = frozenset(built.items())
            if key in fresh:
                raise InputError(f"alert {self.name}: vector contains metrics with the same labelset after applying alert labels")
            fresh[key] = ActiveAlert(built, notes, value, at)
        for key, alert in fresh.items():
            current = self.active.get(key)
            if current is not None and current.state != INACTIVE:
                current.value, current.annotations = alert.value, alert.annotations
                continue
            self.active[key] = alert
        for key, alert in list(self.active.items()):
            if key not in fresh:
                keep = False
                if alert.state == FIRING and self.keep_firing_for > 0:
                    if alert.keep_firing_since is None:
                        alert.keep_firing_since = at
                    keep = at - alert.keep_firing_since < self.keep_firing_for
                if alert.state == PENDING or (alert.resolved_at is not None and at - alert.resolved_at > RESOLVED_RETENTION):
                    del self.active[key]
                if alert.state != INACTIVE and not keep:
                    alert.state, alert.resolved_at = INACTIVE, at
                if not keep:
                    continue
            else:
                alert.keep_firing_since = None
            if alert.state == PENDING and at - alert.active_at >= self.hold:
                alert.state, alert.fired_at = FIRING, at

    def firing(self) -> List[ActiveAlert]:
        return [a for a in self.active.values() if a.state == FIRING and a.resolved_at is None]

    def sends(self, at: float, interval: float, resend: float = RESEND_DELAY) -> List[Dict[str, Any]]:
        out = []
        for alert in self.active.values():
            if alert.state == PENDING:
                continue
            last = alert.last_sent_at if alert.last_sent_at is not None else -math.inf
            due = (alert.resolved_at is not None and alert.resolved_at > last) or last + resend < at
            if not due:
                continue
            alert.last_sent_at = at
            ends = alert.resolved_at if alert.resolved_at is not None else at + 4 * max(interval, resend)
            out.append({"at": at, "labels": dict(sorted(alert.labels.items())), "annotations": dict(sorted(alert.annotations.items())), "startsAt": alert.fired_at, "endsAt": ends, "status": "resolved" if alert.resolved_at is not None else "firing"})
        return out


def load_rules(path: str) -> List[Tuple[str, List[Rule]]]:
    raw = read_json(path)
    if not isinstance(raw, dict) or not isinstance(raw.get("groups"), list):
        raise InputError(f'{path}: a rule file is an object with a list of "groups"')
    groups = []
    for group in raw["groups"]:
        rules = []
        for index, entry in enumerate(group.get("rules") or []):
            where = f"{path}: group {group.get('name')!r}, rule {index + 1}"
            if ("alert" in entry) == ("record" in entry):
                raise InputError(f"{where}: one of 'record' or 'alert' must be set")
            if not entry.get("expr"):
                raise InputError(f"{where}: field 'expr' must be set in rule")
            kind = "alert" if "alert" in entry else "record"
            if kind == "record" and ("for" in entry or "annotations" in entry or "keep_firing_for" in entry):
                raise InputError(f"{where}: invalid field 'for', 'keep_firing_for' or 'annotations' in recording rule")
            try:
                expr = parse_expr(str(entry["expr"]))
            except InputError as exc:
                raise InputError(f"{where}: {exc}") from exc
            rule = Rule(kind, str(entry[kind]), str(entry["expr"]), expr, parse_duration(entry.get("for", "0")), parse_duration(entry.get("keep_firing_for", "0")), {str(k): str(v) for k, v in (entry.get("labels") or {}).items()}, {str(k): str(v) for k, v in (entry.get("annotations") or {}).items()})
            for name, template in list(rule.labels.items()) + list(rule.annotations.items()):
                rendered = expand_template(template, {}, 0.0)
                if rendered.startswith("<error expanding template"):
                    raise InputError(f"{where} ({rule.name}): template {name}: {rendered}")
            rules.append(rule)
        groups.append((str(group.get("name") or ""), rules))
    return groups


@dataclass
class TestGroup:
    name: str
    interval: float
    store: SeriesStore
    groups: List[Tuple[str, List[Rule]]]
    cases: List[Dict[str, Any]]
    external_labels: Dict[str, str]
    last_sample: float
    raw: Dict[str, Any]
    skipped: int


def load_unit_tests(path: str) -> Tuple[Dict[str, Any], List[str], float, List[TestGroup]]:
    raw = read_json(path)
    if not isinstance(raw, dict) or not isinstance(raw.get("tests"), list):
        raise InputError(f'{path}: a unit test file has "rule_files" and a list of "tests"')
    base = os.path.dirname(os.path.abspath(path))
    rule_files = [os.path.join(base, str(f)) for f in raw.get("rule_files") or []]
    evaluation = parse_duration(raw.get("evaluation_interval", "1m"))
    tests = []
    for index, entry in enumerate(raw["tests"]):
        interval = parse_duration(entry.get("interval", raw.get("evaluation_interval", "1m")))
        store = SeriesStore()
        last = 0.0
        for series in entry.get("input_series") or []:
            labels = parse_series_spec(str(series["series"]))
            for position, item in enumerate(expand_values(str(series.get("values", "")))):
                at = position * interval
                if item == MISSING:
                    continue
                store.add(labels, at, None if item == STALE else item)
                last = max(last, at)
        cases = []
        for case in entry.get("alert_rule_test") or []:
            cases.append({"at": parse_duration(case["eval_time"]), "alertname": str(case["alertname"]), "exp": [{"labels": dict({str(k): str(v) for k, v in (a.get("exp_labels") or {}).items()}, alertname=str(case["alertname"])), "annotations": {str(k): str(v) for k, v in (a.get("exp_annotations") or {}).items()}} for a in case.get("exp_alerts") or []]})
        groups = [g for f in rule_files for g in load_rules(f)]
        tests.append(TestGroup(str(entry.get("name") or f"tests[{index}]"), interval, store, groups, cases, {str(k): str(v) for k, v in (entry.get("external_labels") or {}).items()}, last, entry, len(entry.get("promql_expr_test") or [])))
    return raw, rule_files, evaluation, tests


def alert_key(alert: Dict[str, Dict[str, str]]) -> Tuple[Any, ...]:
    return (tuple(sorted(alert["labels"].items())), tuple(sorted(alert["annotations"].items())))


def run_timeline(test: TestGroup, evaluation: float, until: float, on_tick: Callable[[float], None]) -> None:
    ticks = int(round(until / evaluation))
    for tick in range(ticks + 1):
        at = tick * evaluation
        for _, rules in test.groups:
            for rule in rules:
                rule.evaluate(test.store, at, test.external_labels)
        on_tick(at)


def firing_now(test: TestGroup, name: str) -> List[Dict[str, Dict[str, str]]]:
    found = [{"labels": dict(a.labels), "annotations": dict(a.annotations)} for _, rules in test.groups for r in rules if r.kind == "alert" and r.name == name for a in r.firing()]
    return sorted(found, key=alert_key)


def run_unit_tests(path: str, emit: Optional[str] = None) -> Tuple[List[str], Optional[Dict[str, Any]]]:
    raw, rule_files, evaluation, tests = load_unit_tests(path)
    failures: List[str] = []
    emitted_tests = []
    for test in tests:
        until = max([c["at"] for c in test.cases] + ([test.last_sample] if emit else [0.0]))
        names = []
        for _, rules in test.groups:
            for rule in rules:
                if rule.kind == "alert" and rule.name not in names:
                    names.append(rule.name)
        predictions: List[Dict[str, Any]] = []

        def check(at: float) -> None:
            for case in test.cases:
                if at <= case["at"] < at + evaluation:
                    got = firing_now(test, case["alertname"])
                    want = sorted(case["exp"], key=alert_key)
                    if [alert_key(a) for a in got] != [alert_key(a) for a in want]:
                        failures.append(f"    name: {test.name},\n    alertname: {case['alertname']}, time: {format_duration(case['at'])},\n        exp: {json.dumps(want, sort_keys=True)}\n        got: {json.dumps(got, sort_keys=True)}")
            for name in names:
                predictions.append({"eval_time": format_duration(at), "alertname": name, "exp_alerts": [{"exp_labels": {k: v for k, v in sorted(a["labels"].items()) if k != "alertname"}, "exp_annotations": dict(sorted(a["annotations"].items()))} for a in firing_now(test, name)]})

        run_timeline(test, evaluation, until, check)
        copy_of = {k: v for k, v in test.raw.items() if k not in ("alert_rule_test", "promql_expr_test")}
        copy_of["alert_rule_test"] = predictions
        emitted_tests.append(copy_of)
    emitted = None
    if emit:
        out_dir = os.path.dirname(os.path.abspath(emit))
        emitted = {"rule_files": [os.path.relpath(f, out_dir) for f in rule_files], "evaluation_interval": raw.get("evaluation_interval", "1m"), "tests": emitted_tests}
    return failures, emitted


def rules_eval(path: str, which: Optional[str], until: Optional[float]) -> Dict[str, Any]:
    _, _, evaluation, tests = load_unit_tests(path)
    chosen = [t for t in tests if which is None or t.name == which]
    if not chosen:
        raise InputError(f"no test named {which!r} in {path} (have: {', '.join(t.name for t in tests)})")
    test = chosen[0]
    end = until if until is not None else max([c["at"] for c in test.cases] + [test.last_sample])
    previous: Dict[Tuple[str, FrozenSet[Tuple[str, str]]], str] = {}
    transitions: List[Dict[str, Any]] = []
    sent: List[Dict[str, Any]] = []

    def record(at: float) -> None:
        for _, rules in test.groups:
            for rule in rules:
                if rule.kind != "alert":
                    continue
                seen = set()
                for key, alert in rule.active.items():
                    seen.add((rule.name, key))
                    state = alert.display()
                    if previous.get((rule.name, key)) != state:
                        transitions.append({"at": at, "alert": rule.name, "labels": dict(sorted(alert.labels.items())), "state": state, "value": alert.value, "annotations": dict(sorted(alert.annotations.items()))})
                        previous[(rule.name, key)] = state
                for key in [k for k in previous if k[0] == rule.name and k not in seen]:
                    if previous[key] not in ("inactive", "gone"):
                        transitions.append({"at": at, "alert": rule.name, "labels": dict(sorted(dict(key[1]).items())), "state": "gone" if previous[key] == "resolved" else "inactive", "value": None, "annotations": {}})
                    del previous[key]
                sent.extend(dict(s, alert=rule.name) for s in rule.sends(at, test.interval))

    run_timeline(test, evaluation, end, record)
    return {"test": test.name, "interval": evaluation, "until": end, "transitions": transitions, "sent": sent}


def render_eval(result: Dict[str, Any], show_sends: bool) -> str:
    lines = [f"test {result['test']!r}: rules evaluated every {format_duration(result['interval'])} from 0s to {format_duration(result['until'])}", f"{'time':>6}  {'alert':<16} {'state':<14} {'value':>8}  labels"]
    for t in result["transitions"]:
        value = "" if t["value"] is None else go_float(t["value"])
        labels = labelset_string({k: v for k, v in t["labels"].items() if k != "alertname"})
        lines.append(f"{format_duration(t['at']):>6}  {t['alert']:<16} {t['state']:<14} {value:>8}  {labels}")
    if show_sends:
        lines.append("sent to Alertmanager (resend delay 1m, endsAt = resolved time or now + 4 x max(interval, resend delay)):")
        for s in result["sent"]:
            lines.append(f"{format_duration(s['at']):>6}  {s['alert']:<16} {s['status']:<9} startsAt {format_duration(s['startsAt'])} endsAt {format_duration(s['endsAt'])}")
    else:
        lines.append(f"{len(result['sent'])} send(s) to Alertmanager; --show-sends lists them")
    return "\n".join(lines)


def check_rules(paths: Sequence[str]) -> List[str]:
    out = []
    for path in paths:
        groups = load_rules(path)
        count = sum(len(rules) for _, rules in groups)
        out.append(f"Checking {path}\n  SUCCESS: {count} rules found")
    return out


def read_json(path: str) -> Any:
    try:
        if path == "-":
            return json.load(sys.stdin)
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)
    except OSError as exc:
        raise InputError(f"cannot read {path}: {exc.strerror}") from exc
    except json.JSONDecodeError as exc:
        raise InputError(f"{path} is not JSON (YAML is a superset of JSON, so Prometheus and Alertmanager read these files too): {exc}") from exc


def read_jsonl(path: str) -> List[Dict[str, Any]]:
    out = []
    try:
        with open(path, encoding="utf-8") as handle:
            for number, line in enumerate(handle, 1):
                if line.strip():
                    try:
                        out.append(json.loads(line))
                    except json.JSONDecodeError as exc:
                        raise InputError(f"{path} line {number} is not JSON: {exc}") from exc
    except OSError as exc:
        raise InputError(f"cannot read {path}: {exc.strerror}") from exc
    return out


def parse_label_args(items: Sequence[str]) -> Dict[str, str]:
    labels = {}
    for item in items:
        matcher = parse_matcher(item)
        if matcher.op != "=":
            raise InputError("Labels must be specified as key=value pairs")
        labels[matcher.name] = matcher.value
    return labels


def routes_test(config: Config, labels: Dict[str, str]) -> Dict[str, Any]:
    trace: List[str] = []
    routes = explain_route(config.route, labels, trace)
    destinations = []
    for route in routes:
        grouped = group_labels(labels, route)
        destinations.append({"receiver": route.receiver, "route": route.path(), "group_key": group_key(route, grouped), "group_labels": grouped, "options": route.describe()})
    return {"labels": labels, "receivers": [r.receiver for r in routes], "routes": destinations, "trace": trace}


def cmd_check_config(args: argparse.Namespace) -> int:
    print(f"Checking '{args.file}'", end="")
    try:
        raw = read_json(args.file)
        config = load_config(raw)
    except InputError as exc:
        print(f"  FAILED: {exc}")
        return 1
    print("  SUCCESS")
    print("Found:")
    if "global" in raw:
        print(" - global config")
    print(" - route")
    print(f" - {len(config.inhibit_rules)} inhibit rules")
    print(f" - {len(config.receivers)} receivers")
    print(f" - {len(raw.get('templates') or [])} templates")
    return 0


def cmd_routes_test(args: argparse.Namespace) -> int:
    config = load_config(read_json(args.config))
    result = routes_test(config, parse_label_args(args.labels))
    slug = ",".join(result["receivers"])
    if args.json:
        print(json.dumps(result, indent=2))
    else:
        if args.explain:
            print("\n".join(result["trace"]))
            for d in result["routes"]:
                print(f"  -> {d['receiver']}: group {labelset_string(d['group_labels'])}, key {d['group_key']}, {d['options']}")
        print(slug)
    if args.verify_receivers is not None and args.verify_receivers != slug:
        print("WARNING: Expected receivers did not match resolved receivers.")
        return 1
    return 0


def cmd_simulate(args: argparse.Namespace) -> int:
    result = simulate(load_config(read_json(args.config)), read_json(args.scenario), args.start)
    print(json.dumps(result, indent=2) if args.json else render_simulation(result))
    return 0


def cmd_serve(args: argparse.Namespace) -> int:
    server, _ = start_live(load_config(read_json(args.config)), args.listen)
    print(f"minialert: Alertmanager API v2 subset on http://{args.listen} (POST/GET /api/v2/alerts, POST /api/v2/silences, DELETE /api/v2/silence/ID)", file=sys.stderr)
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        server.shutdown()
    return 0


def cmd_receiver(args: argparse.Namespace) -> int:
    server = start_receiver(args.listen, args.out)
    print(f"minialert: recording webhook POSTs on http://{args.listen} to {args.out}", file=sys.stderr)
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        server.shutdown()
    return 0


def cmd_drive(args: argparse.Namespace) -> int:
    record = drive(read_json(args.scenario), args.alertmanager, args.amtool)
    text = json.dumps(record, indent=2)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as handle:
            handle.write(text + "\n")
        print(f"minialert: drove {len(record['steps'])} steps against {args.alertmanager}, {len(record['snapshots'])} snapshot(s), wrote {args.out}", file=sys.stderr)
    else:
        print(text)
    return 0


def cmd_compare(args: argparse.Namespace) -> int:
    lines, problems = compare_runs(read_json(args.simulated), read_json(args.driven), read_jsonl(args.webhook), args.tolerance)
    if args.json:
        print(json.dumps({"rows": lines, "problems": problems}, indent=2))
    else:
        print("\n".join(lines))
        for problem in problems:
            print(f"MISMATCH {problem}")
        print("same notifications and snapshots" if not problems else f"{len(problems)} mismatch(es)")
    return 1 if problems else 0


def cmd_rules_check(args: argparse.Namespace) -> int:
    print("\n".join(check_rules(args.files)))
    return 0


def cmd_rules_eval(args: argparse.Namespace) -> int:
    result = rules_eval(args.file, args.test, parse_duration(args.until) if args.until else None)
    print(json.dumps(result, indent=2) if args.json else render_eval(result, args.show_sends))
    return 0


def cmd_rules_test(args: argparse.Namespace) -> int:
    failures, emitted = run_unit_tests(args.file, args.emit_expected)
    print(f"Unit Testing:  {args.file}")
    if failures:
        print("  FAILED:")
        print("\n\n".join(failures))
    else:
        print("  SUCCESS")
    if emitted is not None and args.emit_expected:
        with open(args.emit_expected, "w", encoding="utf-8") as handle:
            json.dump(emitted, handle, indent=2)
            handle.write("\n")
        cases = sum(len(t["alert_rule_test"]) for t in emitted["tests"])
        print(f"minialert: wrote {args.emit_expected}: {cases} alert_rule_test case(s), one per alert rule per evaluation", file=sys.stderr)
    return 1 if failures else 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="minialert", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("check-config", help="validate an Alertmanager configuration like amtool check-config")
    p.add_argument("file")
    p.set_defaults(func=cmd_check_config)
    routes = sub.add_parser("routes", help="routing tree commands").add_subparsers(dest="routes_command", required=True)
    p = routes.add_parser("test", help="print the receivers a label set reaches, like amtool config routes test")
    p.add_argument("--config", required=True)
    p.add_argument("--verify.receivers", dest="verify_receivers")
    p.add_argument("--explain", "--tree", action="store_true", help="show the walk through the tree and the group each receiver puts the alert in")
    p.add_argument("--json", action="store_true")
    p.add_argument("labels", nargs="*")
    p.set_defaults(func=cmd_routes_test)
    p = sub.add_parser("simulate", help="replay a scenario through the router on a virtual clock")
    p.add_argument("scenario")
    p.add_argument("--config", required=True)
    p.add_argument("--start", default=DEFAULT_START, help="the wall-clock time of 0s in rendered payloads")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_simulate)
    p = sub.add_parser("serve", help="serve the Alertmanager API v2 subset on a real clock and send webhooks")
    p.add_argument("--config", required=True)
    p.add_argument("--listen", default="127.0.0.1:9093")
    p.set_defaults(func=cmd_serve)
    p = sub.add_parser("receiver", help="record webhook POSTs as JSON lines")
    p.add_argument("--listen", default="127.0.0.1:9099")
    p.add_argument("--out", required=True)
    p.set_defaults(func=cmd_receiver)
    p = sub.add_parser("drive", help="play a scenario against a real Alertmanager API in real time")
    p.add_argument("scenario")
    p.add_argument("--alertmanager", required=True, help="base URL, for example http://127.0.0.1:9093")
    p.add_argument("--amtool", help="command prefix for amtool, used for silences, for example 'amtool --alertmanager.url=http://127.0.0.1:9093'")
    p.add_argument("--out")
    p.set_defaults(func=cmd_drive)
    p = sub.add_parser("compare", help="compare a simulation with a driven run and the webhooks it produced")
    p.add_argument("--simulated", required=True)
    p.add_argument("--driven", required=True)
    p.add_argument("--webhook", required=True)
    p.add_argument("--tolerance", type=float, default=1.0)
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_compare)
    rules = sub.add_parser("rules", help="alerting rule commands").add_subparsers(dest="rules_command", required=True)
    p = rules.add_parser("check", help="parse rule files like promtool check rules")
    p.add_argument("files", nargs="+")
    p.set_defaults(func=cmd_rules_check)
    p = rules.add_parser("eval", help="evaluate rules over a unit test file's input series and print the alert lifecycle")
    p.add_argument("file")
    p.add_argument("--test", help="the test group name (default: the first)")
    p.add_argument("--until", help="last evaluation time (default: the last eval_time or sample)")
    p.add_argument("--show-sends", action="store_true")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_rules_eval)
    p = rules.add_parser("test", help="run promtool-format alert unit tests")
    p.add_argument("file")
    p.add_argument("--emit-expected", help="write a promtool test file whose expectations are minialert's predictions at every evaluation")
    p.set_defaults(func=cmd_rules_test)
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(list(sys.argv[1:] if argv is None else argv))
    try:
        return int(args.func(args))
    except InputError as exc:
        print(f"minialert: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
