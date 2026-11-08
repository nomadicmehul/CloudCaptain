"""miniincident: an incident service. It receives Alertmanager webhooks and PagerDuty Events API v2 events, opens one incident per group key, pages whoever is on call in layered schedules, escalates on a clock, keeps a timeline of alerts, pages, acknowledgements, roles, deploys, commands and status updates, computes MTTA and MTTR, and drafts a blameless postmortem."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shlex
import statistics
import sys
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, List, Optional, Sequence, Tuple
from urllib.parse import parse_qs, urlparse
from zoneinfo import ZoneInfo

TIME = re.compile(r"^(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2}):(\d{2})(\.\d+)?(Z|[+-]\d{2}:?\d{2})$")
DURATION = re.compile(r"(\d+(?:\.\d+)?)(w|d|h|m|s)")
UNITS = {"w": 604800, "d": 86400, "h": 3600, "m": 60, "s": 1}
DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
ROLES = ("commander", "comms", "ops")
UPDATE_STATES = ("investigating", "identified", "monitoring", "resolved")
COMPONENT_STATES = ("operational", "degraded_performance", "partial_outage", "major_outage")
PD_ACTIONS = ("trigger", "acknowledge", "resolve")
PD_SEVERITIES = ("critical", "error", "warning", "info")
INCIDENT_ID = re.compile(r"^INC-\d+$")
BLAME = re.compile(r"\b(human error|should have|careless\w*|negligen\w*|at fault|fault of|blame\w*|forgot to|didn't bother)\b", re.IGNORECASE)
POSTMORTEM_SECTIONS = ("Summary", "Impact", "Timeline", "Response", "Contributing factors", "What went well", "What was hard", "Action items", "Language check")
COMMAND_HELP = (
    "/inc open TITLE [service=S] [sev=SEVn] | ack [ID] | resolve [ID] [note] | sev [ID] SEVn | sev [ID] impact=I scope=S"
    " | role [ID] commander|comms|ops @user | status [ID] investigating|identified|monitoring|resolved MESSAGE"
    " | note [ID] TEXT | page [ID] @user|schedule:NAME"
)


class InputError(Exception):
    pass


def parse_time(text: Any) -> Optional[float]:
    if text is None or text == "":
        return None
    if isinstance(text, (int, float)) and not isinstance(text, bool):
        return float(text)
    match = TIME.match(str(text).strip())
    if not match:
        raise InputError(f"not an RFC 3339 time: {text!r} (example: 2026-11-02T10:00:00Z)")
    year, month, day, hour, minute, second, fraction, offset = match.groups()
    if int(year) <= 1:
        return None
    tz = timezone.utc
    if offset != "Z":
        digits = offset[1:].replace(":", "")
        delta = timedelta(hours=int(digits[:2]), minutes=int(digits[2:]))
        tz = timezone(delta if offset[0] == "+" else -delta)
    moment = datetime(int(year), int(month), int(day), int(hour), int(minute), int(second), tzinfo=tz)
    return moment.timestamp() + (float(fraction) if fraction else 0.0)


def parse_duration(text: Any) -> float:
    if isinstance(text, (int, float)) and not isinstance(text, bool):
        return float(text)
    raw = str(text).strip()
    parts = DURATION.findall(raw)
    if not raw or "".join(number + unit for number, unit in parts) != raw:
        raise InputError(f"not a duration: {text!r} (examples: 90s, 5m, 1h30m, 7d)")
    return sum(float(number) * UNITS[unit] for number, unit in parts)


def parse_when(text: Any, base: float) -> float:
    raw = str(text).strip()
    if raw.startswith("+"):
        return base + parse_duration(raw[1:])
    value = parse_time(text)
    if value is None:
        raise InputError(f"not a time: {text!r} (use RFC 3339 or +5m relative to the start)")
    return value


def iso(ts: Optional[float]) -> str:
    if ts is None:
        return ""
    return datetime.fromtimestamp(round(ts), timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def hhmmss(ts: float) -> str:
    return datetime.fromtimestamp(round(ts), timezone.utc).strftime("%H:%M:%S")


def human(seconds: Optional[float]) -> str:
    if seconds is None:
        return "n/a"
    total = int(round(seconds))
    hours, rest = divmod(total, 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return f"{hours}h {minutes:02d}m"
    if minutes:
        return f"{minutes}m {secs:02d}s"
    return f"{secs}s"


def labelset(labels: Dict[str, Any]) -> str:
    return "{" + ", ".join(f"{name}={json.dumps(str(labels[name]), ensure_ascii=False)}" for name in sorted(labels)) + "}"


def group_key(route_key: str, group_labels: Dict[str, Any]) -> str:
    return f"{route_key}:{labelset(group_labels)}"


def dedup_key(key: str) -> str:
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def fingerprint(alert: Dict[str, Any]) -> str:
    if alert.get("fingerprint"):
        return str(alert["fingerprint"])
    return hashlib.sha256(labelset(alert.get("labels") or {}).encode("utf-8")).hexdigest()[:16]


def zone(name: str) -> Any:
    if name in ("", "UTC", "Etc/UTC"):
        return timezone.utc
    try:
        return ZoneInfo(name)
    except Exception as exc:
        raise InputError(f"unknown time zone {name!r}: {exc}")


def clock_minutes(text: Any) -> int:
    match = re.fullmatch(r"(\d{1,2}):(\d{2})", str(text).strip())
    if not match or int(match.group(2)) > 59 or int(match.group(1)) * 60 + int(match.group(2)) > 1440:
        raise InputError(f"not a time of day: {text!r} (examples: 08:00, 16:30, 24:00)")
    return int(match.group(1)) * 60 + int(match.group(2))


@dataclass
class Restriction:
    start_minute: int
    end_minute: int
    days: Tuple[int, ...]


@dataclass
class Layer:
    name: str
    users: List[str]
    start: float
    rotation: float
    zone: Any
    restrictions: List[Restriction]


@dataclass
class Schedule:
    name: str
    layers: List[Layer]
    overrides: List[Tuple[float, float, str]]


@dataclass
class OnCall:
    schedule: str
    user: Optional[str]
    source: str
    since: Optional[float]


@dataclass
class Level:
    targets: List[str]
    escalate_after: float


@dataclass
class Policy:
    name: str
    levels: List[Level]
    repeat: int


@dataclass
class Severity:
    name: str
    description: str
    page: bool
    roles: List[str]
    status_update: float
    component_status: str
    postmortem: bool


@dataclass
class Config:
    severities: Dict[str, Severity]
    impacts: List[str]
    scopes: List[str]
    matrix: Dict[str, Dict[str, str]]
    alert_severity: Dict[str, str]
    default_severity: str
    services: Dict[str, Dict[str, str]]
    schedules: Dict[str, Schedule]
    policies: Dict[str, Policy]
    deploy_lookback: float
    reopen_window: float
    doc: Dict[str, Any]


@dataclass
class Entry:
    seq: int
    at: float
    kind: str
    text: str
    actor: str = ""
    data: Dict[str, Any] = field(default_factory=dict)


@dataclass
class Escalation:
    policy: str = ""
    level: int = 0
    cycle: int = 0
    next_at: Optional[float] = None
    state: str = "pending"


@dataclass
class Incident:
    id: str
    key: str
    title: str
    service: str
    severity: str
    status: str
    opened_at: float
    group_key: str = ""
    impact_start: Optional[float] = None
    acknowledged_at: Optional[float] = None
    resolved_at: Optional[float] = None
    alerts: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    roles: Dict[str, str] = field(default_factory=dict)
    handoffs: List[Dict[str, Any]] = field(default_factory=list)
    timeline: List[Entry] = field(default_factory=list)
    escalation: Escalation = field(default_factory=Escalation)
    notifications: Dict[str, int] = field(default_factory=dict)
    repeats: int = 0
    status_updates: List[Dict[str, Any]] = field(default_factory=list)
    deploys: List[str] = field(default_factory=list)
    related: List[str] = field(default_factory=list)
    severity_source: str = "alert"


def load_schedule(name: str, spec: Dict[str, Any]) -> Schedule:
    default_zone = spec.get("timezone", "UTC")
    layers = []
    for number, layer in enumerate(spec.get("layers") or [], 1):
        where = f"schedule {name} layer {number}"
        users = [str(u) for u in layer.get("users") or []]
        if not users:
            raise InputError(f"{where}: users is empty")
        start = parse_time(layer.get("start"))
        if start is None:
            raise InputError(f"{where}: start is required (the first handoff, RFC 3339)")
        rotation = parse_duration(layer.get("rotation", "168h"))
        if rotation <= 0:
            raise InputError(f"{where}: rotation must be longer than zero")
        restrictions = []
        for item in layer.get("restrictions") or []:
            days = item.get("days") or list(DAYS)
            unknown = [d for d in days if d not in DAYS]
            if unknown:
                raise InputError(f"{where}: unknown day(s) {', '.join(unknown)}; use {', '.join(DAYS)}")
            restrictions.append(Restriction(clock_minutes(item.get("from", "00:00")), clock_minutes(item.get("to", "24:00")), tuple(DAYS.index(d) for d in days)))
        layers.append(Layer(str(layer.get("name") or f"layer-{number}"), users, start, rotation, zone(layer.get("timezone", default_zone)), restrictions))
    if not layers:
        raise InputError(f"schedule {name} has no layers")
    overrides = []
    for item in spec.get("overrides") or []:
        start, end = parse_time(item.get("start")), parse_time(item.get("end"))
        if start is None or end is None or end <= start or not item.get("user"):
            raise InputError(f"schedule {name}: an override needs user, start and an end after the start")
        overrides.append((start, end, str(item["user"])))
    return Schedule(name, layers, overrides)


def load_config(doc: Any) -> Config:
    if not isinstance(doc, dict):
        raise InputError("the config must be a JSON object")
    severities: Dict[str, Severity] = {}
    for name, spec in (doc.get("severities") or {}).items():
        roles = list(spec.get("roles") or [])
        unknown = [r for r in roles if r not in ROLES]
        if unknown:
            raise InputError(f"severity {name}: unknown role(s) {', '.join(unknown)}; roles are {', '.join(ROLES)}")
        state = spec.get("component_status", "operational")
        if state not in COMPONENT_STATES:
            raise InputError(f"severity {name}: component_status {state!r} is not one of {', '.join(COMPONENT_STATES)}")
        severities[name] = Severity(name, str(spec.get("description", "")), bool(spec.get("page")), roles, parse_duration(spec.get("status_update", 0)), state, bool(spec.get("postmortem")))
    if not severities:
        raise InputError("the config has no severities")
    matrix = doc.get("severity_matrix") or {}
    impacts, scopes, cells = list(matrix.get("impact") or []), list(matrix.get("scope") or []), matrix.get("cells") or {}
    for impact in impacts:
        for scope in scopes:
            value = (cells.get(impact) or {}).get(scope)
            if value not in severities:
                raise InputError(f"severity_matrix.cells.{impact}.{scope} is {value!r}; expected one of {', '.join(severities)}")
    alert_severity = {str(k): str(v) for k, v in (doc.get("alert_severity") or {}).items()}
    default_severity = str(doc.get("default_severity") or list(severities)[-1])
    for value in list(alert_severity.values()) + [default_severity]:
        if value not in severities:
            raise InputError(f"alert_severity and default_severity must name a severity, got {value!r}")
    schedules = {name: load_schedule(name, spec) for name, spec in (doc.get("schedules") or {}).items()}
    policies: Dict[str, Policy] = {}
    for name, spec in (doc.get("escalation_policies") or {}).items():
        levels = []
        for number, level in enumerate(spec.get("levels") or [], 1):
            targets = [str(t) for t in level.get("targets") or []]
            if not targets:
                raise InputError(f"escalation policy {name} level {number}: targets is empty")
            for target in targets:
                kind, _, ref = target.partition(":")
                if kind not in ("schedule", "user") or not ref:
                    raise InputError(f"escalation policy {name} level {number}: target {target!r}; use schedule:NAME or user:NAME")
                if kind == "schedule" and ref not in schedules:
                    raise InputError(f"escalation policy {name} level {number}: no schedule {ref!r}")
            levels.append(Level(targets, parse_duration(level.get("escalate_after", "5m"))))
        if not levels:
            raise InputError(f"escalation policy {name} has no levels")
        policies[name] = Policy(name, levels, int(spec.get("repeat", 0)))
    services: Dict[str, Dict[str, str]] = {}
    for name, spec in (doc.get("services") or {}).items():
        policy = str(spec.get("escalation_policy") or "")
        if policy and policy not in policies:
            raise InputError(f"service {name}: no escalation policy {policy!r}")
        services[name] = {"escalation_policy": policy, "component": str(spec.get("component") or "")}
    return Config(severities, impacts, scopes, cells, alert_severity, default_severity, services, schedules, policies,
                  parse_duration(doc.get("deploy_lookback", "60m")), parse_duration(doc.get("reopen_window", "30m")), doc)


def classify(config: Config, impact: str, scope: str) -> str:
    if impact not in config.impacts or scope not in config.scopes:
        raise InputError(f"impact must be one of {', '.join(config.impacts)} and scope one of {', '.join(config.scopes)}; got {impact!r}, {scope!r}")
    return config.matrix[impact][scope]


def severity_from_labels(config: Config, labels: Dict[str, Any]) -> Tuple[str, str]:
    impact, scope = labels.get("impact"), labels.get("scope")
    if impact in config.impacts and scope in config.scopes:
        return classify(config, impact, scope), f"matrix impact={impact} scope={scope}"
    label = str(labels.get("severity", ""))
    if label in config.alert_severity:
        return config.alert_severity[label], f"alert severity {label}"
    return config.default_severity, "default"


def severity_rank(config: Config, name: str) -> int:
    names = list(config.severities)
    return names.index(name) if name in names else len(names)


def restriction_covers(restriction: Restriction, local: datetime) -> bool:
    minute, weekday = local.hour * 60 + local.minute, local.weekday()
    start, end = restriction.start_minute, restriction.end_minute
    if start == end:
        return weekday in restriction.days
    if start < end:
        return weekday in restriction.days and start <= minute < end
    if minute >= start:
        return weekday in restriction.days
    return minute < end and (weekday - 1) % 7 in restriction.days


def layer_user(layer: Layer, at: float) -> Optional[Tuple[str, float]]:
    if at < layer.start:
        return None
    if layer.restrictions:
        local = datetime.fromtimestamp(at, layer.zone)
        if not any(restriction_covers(r, local) for r in layer.restrictions):
            return None
    shift = int((at - layer.start) // layer.rotation)
    return layer.users[shift % len(layer.users)], layer.start + shift * layer.rotation


def on_call(schedule: Schedule, at: float) -> OnCall:
    for start, end, user in reversed(schedule.overrides):
        if start <= at < end:
            return OnCall(schedule.name, user, "override", start)
    for layer in reversed(schedule.layers):
        found = layer_user(layer, at)
        if found:
            return OnCall(schedule.name, found[0], f"layer {layer.name}", found[1])
    return OnCall(schedule.name, None, "gap", None)


def render_schedule(schedule: Schedule, start: float, end: float, step: float = 60.0) -> List[Dict[str, Any]]:
    segments: List[Dict[str, Any]] = []
    at = start
    while at < end:
        current = on_call(schedule, at)
        stop = min(at + step, end)
        if segments and (segments[-1]["user"], segments[-1]["source"]) == (current.user, current.source):
            segments[-1]["end"] = stop
        else:
            segments.append({"start": at, "end": stop, "user": current.user, "source": current.source})
        at = stop
    return segments


def incident_times(incident: Incident) -> Dict[str, Optional[float]]:
    opened = incident.opened_at
    return {
        "time_to_detect": None if incident.impact_start is None else max(0.0, opened - incident.impact_start),
        "time_to_acknowledge": None if incident.acknowledged_at is None else incident.acknowledged_at - opened,
        "time_to_resolve": None if incident.resolved_at is None else incident.resolved_at - opened,
    }


def summarise(values: List[float]) -> Dict[str, Any]:
    if not values:
        return {"count": 0, "mean": None, "median": None}
    return {"count": len(values), "mean": sum(values) / len(values), "median": float(statistics.median(values))}


def metrics(incidents: Sequence[Incident]) -> Dict[str, Any]:
    rows = [{"id": i.id, "severity": i.severity, "status": i.status, **incident_times(i)} for i in incidents]
    pick = lambda name, subset: [r[name] for r in subset if r[name] is not None]
    by_severity = {}
    for name in sorted({r["severity"] for r in rows}):
        subset = [r for r in rows if r["severity"] == name]
        by_severity[name] = {"incidents": len(subset), "mtta": summarise(pick("time_to_acknowledge", subset)), "mttr": summarise(pick("time_to_resolve", subset))}
    return {
        "incidents": len(rows),
        "open": sum(1 for i in incidents if i.status != "resolved"),
        "resolved": sum(1 for i in incidents if i.status == "resolved"),
        "resolved_without_ack": sum(1 for i in incidents if i.status == "resolved" and i.acknowledged_at is None),
        "escalations": sum(1 for i in incidents for e in i.timeline if e.kind == "escalation"),
        "mttd": summarise(pick("time_to_detect", rows)),
        "mtta": summarise(pick("time_to_acknowledge", rows)),
        "mttr": summarise(pick("time_to_resolve", rows)),
        "by_severity": by_severity,
        "per_incident": rows,
    }


class IncidentService:
    def __init__(self, config: Config):
        self.config = config
        self.incidents: List[Incident] = []
        self.pages: List[Dict[str, Any]] = []
        self.deploys: List[Dict[str, Any]] = []
        self.unmatched: List[Dict[str, Any]] = []
        self.sequence = 0
        self.entries = 0

    def add(self, incident: Incident, at: float, kind: str, text: str, actor: str = "miniincident", **data: Any) -> Entry:
        self.entries += 1
        entry = Entry(self.entries, at, kind, text, actor, data)
        incident.timeline.append(entry)
        return entry

    def get(self, incident_id: str) -> Incident:
        for incident in self.incidents:
            if incident.id == incident_id:
                return incident
        raise InputError(f"no incident {incident_id}; known: {', '.join(i.id for i in self.incidents) or 'none'}")

    def active(self, key: str) -> Optional[Incident]:
        for incident in reversed(self.incidents):
            if incident.key == key and incident.status != "resolved":
                return incident
        return None

    def last_resolved(self, key: str) -> Optional[Incident]:
        for incident in reversed(self.incidents):
            if incident.key == key and incident.status == "resolved":
                return incident
        return None

    def open(self, key: str, now: float, title: str, service: str, severity: str, why: str, source: str, actor: str = "miniincident") -> Incident:
        previous = self.last_resolved(key)
        self.sequence += 1
        incident = Incident(f"INC-{self.sequence}", key, title, service, severity, "triggered", now)
        self.incidents.append(incident)
        self.add(incident, now, "opened", f"opened from {source}: {title} ({severity}, {why})", actor, severity=severity, source=source)
        if previous and previous.resolved_at is not None and now - previous.resolved_at <= self.config.reopen_window:
            incident.related.append(previous.id)
            self.add(incident, now, "related", f"same key as {previous.id}, resolved {human(now - previous.resolved_at)} earlier: flapping or not fixed?", related=previous.id)
        self.start_paging(incident, now)
        return incident

    def start_paging(self, incident: Incident, now: float) -> None:
        severity = self.config.severities[incident.severity]
        service = self.config.services.get(incident.service) or self.config.services.get("*") or {}
        policy = service.get("escalation_policy", "")
        incident.escalation.policy = policy
        if not severity.page:
            incident.escalation.state = "not paged"
            self.add(incident, now, "not-paged", f"{incident.severity} does not page; it waits for working hours")
            return
        if not policy:
            incident.escalation.state = "no policy"
            self.add(incident, now, "not-paged", f"service {incident.service} has no escalation policy; nobody was paged")
            return
        incident.escalation.state = "paging"
        self.page_level(incident, now)

    def resolve_targets(self, incident: Incident, targets: Sequence[str], at: float) -> List[Tuple[str, str]]:
        users = []
        for target in targets:
            kind, _, name = target.partition(":")
            if kind == "user":
                users.append((name, target))
                continue
            current = on_call(self.config.schedules[name], at)
            if current.user:
                users.append((current.user, f"{target}, {current.source}"))
            else:
                self.add(incident, at, "page.gap", f"nobody is on call in schedule {name}", schedule=name)
        return users

    def page(self, incident: Incident, at: float, users: Sequence[Tuple[str, str]], level: int) -> None:
        for user, why in users:
            self.pages.append({"incident": incident.id, "at": at, "user": user, "level": level, "via": why})
            label = f"level {level}" if level else "manual"
            self.add(incident, at, "page", f"paged {user} ({label}, {why})", user=user, level=level)

    def page_level(self, incident: Incident, at: float) -> None:
        escalation = incident.escalation
        level = self.config.policies[escalation.policy].levels[escalation.level]
        users = self.resolve_targets(incident, level.targets, at)
        self.page(incident, at, users, escalation.level + 1)
        escalation.next_at = at + level.escalate_after if users else at

    def tick(self, now: float) -> List[Dict[str, Any]]:
        happened = []
        for incident in self.incidents:
            escalation = incident.escalation
            while incident.status == "triggered" and escalation.next_at is not None and escalation.next_at <= now:
                due = escalation.next_at
                policy = self.config.policies[escalation.policy]
                waited = policy.levels[escalation.level].escalate_after
                if escalation.level + 1 < len(policy.levels):
                    escalation.level += 1
                elif escalation.cycle < policy.repeat:
                    escalation.cycle, escalation.level = escalation.cycle + 1, 0
                else:
                    escalation.next_at, escalation.state = None, "exhausted"
                    self.add(incident, due, "escalation.exhausted", f"escalation policy {policy.name} exhausted: nobody acknowledged after {sum(1 for p in self.pages if p['incident'] == incident.id)} page(s)")
                    happened.append({"incident": incident.id, "at": due, "exhausted": True})
                    break
                self.add(incident, due, "escalation", f"escalated to level {escalation.level + 1} after {human(waited)} without acknowledgement", level=escalation.level + 1)
                self.page_level(incident, due)
                happened.append({"incident": incident.id, "at": due, "level": escalation.level + 1})
        return happened

    def receive_alertmanager(self, payload: Any, now: float) -> Dict[str, Any]:
        if not isinstance(payload, dict) or not payload.get("groupKey") or not isinstance(payload.get("alerts"), list):
            raise InputError("not an Alertmanager webhook: groupKey and alerts are required (webhook version 4)")
        key = dedup_key(payload["groupKey"])
        incident = self.active(key)
        labels = {**(payload.get("groupLabels") or {}), **(payload.get("commonLabels") or {})}
        if incident is None:
            if payload.get("status") == "resolved":
                self.unmatched.append({"at": now, "groupKey": payload["groupKey"], "status": "resolved"})
                return {"action": "ignored", "reason": "resolved notification for a group with no open incident", "key": key}
            annotations = payload.get("commonAnnotations") or {}
            title = str(annotations.get("summary") or f"{labels.get('alertname', 'alert')} on {labels.get('service', 'unknown')}")
            severity, why = severity_from_labels(self.config, labels)
            incident = self.open(key, now, title, str(labels.get("service") or "unknown"), severity, why, "alertmanager")
            action = "opened"
        else:
            action = "updated"
        incident.group_key = incident.group_key or str(payload["groupKey"])
        incident.notifications["alertmanager"] = incident.notifications.get("alertmanager", 0) + 1
        changes = self.merge_alerts(incident, payload["alerts"], now)
        if action == "updated" and not changes:
            incident.repeats += 1
            action = "repeat"
        self.attach_deploys(incident)
        if payload.get("status") == "resolved" and incident.status != "resolved":
            ends = [a.get("endsAt") for a in incident.alerts.values() if a.get("endsAt") is not None]
            at = min(max(ends), now) if ends else now
            self.resolve(incident, max(at, incident.opened_at), "alertmanager", "every alert in the group resolved")
            action = "resolved"
        return {"action": action, "incident": incident.id, "key": key, "changes": changes}

    def merge_alerts(self, incident: Incident, alerts: Sequence[Dict[str, Any]], now: float) -> int:
        changes = 0
        for alert in alerts:
            labels = alert.get("labels") or {}
            fp = fingerprint(alert)
            status = alert.get("status") or "firing"
            starts, ends = parse_time(alert.get("startsAt")), parse_time(alert.get("endsAt"))
            if starts is not None and (incident.impact_start is None or starts < incident.impact_start):
                incident.impact_start = starts
            known = incident.alerts.get(fp)
            if known is None:
                incident.alerts[fp] = {"labels": labels, "status": status, "startsAt": starts, "endsAt": ends if status == "resolved" else None}
                if status == "firing":
                    kind = "alert" if len(incident.alerts) == 1 else "alert.added"
                    self.add(incident, starts if starts is not None and kind == "alert" else now, kind, f"firing {labels.get('alertname', 'alert')} {labelset(labels)}", "alertmanager", fingerprint=fp)
                changes += 1
            elif known["status"] != status:
                known["status"], known["endsAt"] = status, ends if status == "resolved" else None
                verb = "resolved" if status == "resolved" else "firing again"
                self.add(incident, ends if status == "resolved" and ends is not None else now, "alert.resolved" if status == "resolved" else "alert.added", f"{verb} {labels.get('alertname', 'alert')} {labelset(labels)}", "alertmanager", fingerprint=fp)
                changes += 1
        return changes

    def receive_pagerduty(self, event: Any, now: float) -> Dict[str, Any]:
        if not isinstance(event, dict):
            raise InputError("a PagerDuty event is a JSON object")
        action = event.get("event_action")
        if action not in PD_ACTIONS:
            raise InputError(f"event_action must be one of {', '.join(PD_ACTIONS)}, got {action!r}")
        if not event.get("routing_key"):
            raise InputError("routing_key is required (the integration key of the service)")
        key = str(event.get("dedup_key") or "")
        if action == "trigger":
            payload = event.get("payload") or {}
            missing = [name for name in ("summary", "source", "severity") if not payload.get(name)]
            if missing:
                raise InputError("a trigger needs payload." + ", payload.".join(missing))
            if payload["severity"] not in PD_SEVERITIES:
                raise InputError(f"payload.severity must be one of {', '.join(PD_SEVERITIES)}, got {payload['severity']!r}")
            key = key or uuid.uuid4().hex
            incident = self.active(key)
            if incident is None:
                severity, why = severity_from_labels(self.config, {"severity": payload["severity"]})
                service = str(payload.get("component") or payload.get("source"))
                incident = self.open(key, now, str(payload["summary"]), service, severity, why, "pagerduty")
                self.attach_deploys(incident)
            incident.notifications["pagerduty"] = incident.notifications.get("pagerduty", 0) + 1
            return {"status": "success", "message": "Event processed", "dedup_key": key}
        if not key:
            raise InputError("dedup_key is required to acknowledge or resolve")
        incident = self.active(key)
        if incident is not None:
            incident.notifications["pagerduty"] = incident.notifications.get("pagerduty", 0) + 1
            if action == "acknowledge":
                self.acknowledge(incident, now, f"pagerduty:{event.get('client') or 'events-api'}")
            else:
                self.resolve(incident, now, "pagerduty", "resolve event from the Events API v2")
        return {"status": "success", "message": "Event processed", "dedup_key": key}

    def record_deploy(self, event: Any, now: float) -> Dict[str, Any]:
        if not isinstance(event, dict) or not event.get("service") or not event.get("version"):
            raise InputError("a deploy event needs service and version (and optionally at, actor, kind)")
        at = parse_time(event.get("at")) if event.get("at") else now
        deploy = {"id": f"D-{len(self.deploys) + 1}", "service": str(event["service"]), "version": str(event["version"]),
                  "actor": str(event.get("actor") or "unknown"), "kind": str(event.get("kind") or "deploy"), "at": at}
        self.deploys.append(deploy)
        for incident in self.incidents:
            if incident.status != "resolved":
                self.attach_deploys(incident)
        return deploy

    def attach_deploys(self, incident: Incident) -> None:
        start = min(incident.opened_at, incident.impact_start if incident.impact_start is not None else incident.opened_at)
        for deploy in self.deploys:
            if deploy["service"] != incident.service or deploy["id"] in incident.deploys or deploy["at"] < start - self.config.deploy_lookback:
                continue
            if incident.resolved_at is not None and deploy["at"] > incident.resolved_at:
                continue
            incident.deploys.append(deploy["id"])
            when = f"{human(start - deploy['at'])} before the first alert" if deploy["at"] < start else "during the incident"
            self.add(incident, deploy["at"], "deploy", f"{deploy['kind']} {deploy['service']} {deploy['version']} by {deploy['actor']} ({when})", deploy["actor"], deploy=deploy["id"])

    def acknowledge(self, incident: Incident, now: float, actor: str) -> str:
        if incident.status != "triggered":
            return f"{incident.id} is already {incident.status}"
        incident.status, incident.acknowledged_at = "acknowledged", now
        incident.escalation.next_at = None
        if incident.escalation.state == "paging":
            incident.escalation.state = "acknowledged"
        self.add(incident, now, "ack", f"acknowledged by {actor} after {human(now - incident.opened_at)}", actor)
        if "commander" not in incident.roles and not actor.startswith("pagerduty:"):
            self.assign(incident, "commander", actor, now, actor, "first responder")
        return f"{incident.id} acknowledged by {actor}; escalation stopped"

    def resolve(self, incident: Incident, at: float, actor: str, reason: str) -> str:
        if incident.status == "resolved":
            return f"{incident.id} is already resolved"
        incident.status, incident.resolved_at = "resolved", at
        incident.escalation.next_at = None
        if incident.escalation.state == "paging":
            incident.escalation.state = "resolved"
        self.add(incident, at, "resolved", f"resolved: {reason}, {human(at - incident.opened_at)} after it opened", actor)
        if incident.status_updates and incident.status_updates[-1]["state"] != "resolved":
            self.status_update(incident, at, "resolved", "This incident has been resolved.", actor)
        return f"{incident.id} resolved"

    def assign(self, incident: Incident, role: str, user: str, now: float, actor: str, why: str = "") -> str:
        if role not in ROLES:
            raise InputError(f"unknown role {role!r}; roles are {', '.join(ROLES)}")
        user = user.lstrip("@")
        previous = incident.roles.get(role)
        if previous == user:
            return f"{user} already holds {role}"
        incident.roles[role] = user
        if previous:
            incident.handoffs.append({"role": role, "from": previous, "to": user, "at": now})
            self.add(incident, now, "handoff", f"{role} handed off from {previous} to {user}", actor, role=role)
            return f"{role} on {incident.id}: {previous} to {user}"
        self.add(incident, now, "role", f"{user} is {role}" + (f" ({why})" if why else ""), actor, role=role)
        return f"{user} is {role} on {incident.id}"

    def set_severity(self, incident: Incident, severity: str, now: float, actor: str, why: str) -> str:
        if severity not in self.config.severities:
            raise InputError(f"unknown severity {severity!r}; use {', '.join(self.config.severities)}")
        old = incident.severity
        if old == severity:
            return f"{incident.id} is already {severity}"
        incident.severity, incident.severity_source = severity, "human"
        self.add(incident, now, "severity", f"severity {old} to {severity} ({why})", actor, old=old, new=severity)
        if self.config.severities[severity].page and incident.status == "triggered" and incident.escalation.state in ("not paged", "pending"):
            self.start_paging(incident, now)
        return f"{incident.id} is {severity}"

    def status_update(self, incident: Incident, now: float, state: str, message: str, actor: str) -> str:
        if state not in UPDATE_STATES:
            raise InputError(f"status must be one of {', '.join(UPDATE_STATES)}, got {state!r}")
        if not message:
            raise InputError("a status page update needs a message")
        incident.status_updates.append({"at": now, "state": state, "message": message, "actor": actor})
        self.add(incident, now, "status", f"status page {state}: {message}", actor, state=state)
        return f"status page updated for {incident.id}: {state}"

    def pick(self, args: List[str]) -> Tuple[Incident, List[str]]:
        if args and INCIDENT_ID.match(args[0]):
            return self.get(args[0]), args[1:]
        open_ids = [i for i in self.incidents if i.status != "resolved"]
        if len(open_ids) == 1:
            return open_ids[0], args
        raise InputError("which incident? name it first, for example /inc ack INC-1; open: " + (", ".join(i.id for i in open_ids) or "none"))

    def command(self, user: str, text: str, now: float) -> str:
        try:
            words = shlex.split(text)
        except ValueError as exc:
            raise InputError(f"cannot parse command {text!r}: {exc}")
        if words and words[0] in ("/inc", "/incident"):
            words = words[1:]
        if not words or words[0] == "help":
            return COMMAND_HELP
        user = user.lstrip("@") or "unknown"
        verb, args = words[0], words[1:]
        if verb == "open":
            options = dict(a.split("=", 1) for a in args if "=" in a)
            title = " ".join(a for a in args if "=" not in a) or "declared incident"
            severity = options.get("sev", self.config.default_severity)
            if severity not in self.config.severities:
                raise InputError(f"unknown severity {severity!r}; use {', '.join(self.config.severities)}")
            incident = self.open(f"manual:{self.sequence + 1}", now, title, options.get("service", "unknown"), severity, f"declared by {user}", "chatops", user)
            self.attach_deploys(incident)
            return f"{incident.id} opened ({severity})"
        incident, args = self.pick(args)
        if verb == "ack":
            return self.acknowledge(incident, now, user)
        if verb == "resolve":
            return self.resolve(incident, now, user, " ".join(args) or f"resolved by {user}")
        if verb == "sev":
            options = dict(a.split("=", 1) for a in args if "=" in a)
            if "impact" in options or "scope" in options:
                severity = classify(self.config, options.get("impact", ""), options.get("scope", ""))
                return self.set_severity(incident, severity, now, user, f"matrix impact={options.get('impact')} scope={options.get('scope')}")
            if not args:
                raise InputError("usage: /inc sev [ID] SEVn, or /inc sev [ID] impact=I scope=S")
            return self.set_severity(incident, args[0], now, user, f"set by {user}")
        if verb == "role":
            if len(args) != 2:
                raise InputError("usage: /inc role [ID] commander|comms|ops @user")
            return self.assign(incident, args[0], args[1], now, user)
        if verb == "status":
            if len(args) < 2:
                raise InputError("usage: /inc status [ID] investigating|identified|monitoring|resolved MESSAGE")
            return self.status_update(incident, now, args[0], " ".join(args[1:]), user)
        if verb == "note":
            if not args:
                raise InputError("usage: /inc note [ID] TEXT")
            self.add(incident, now, "note", " ".join(args), user)
            return f"note added to {incident.id}"
        if verb == "page":
            if not args:
                raise InputError("usage: /inc page [ID] @user|schedule:NAME")
            target = args[0] if args[0].startswith("schedule:") else f"user:{args[0].lstrip('@')}"
            if target.startswith("schedule:") and target[9:] not in self.config.schedules:
                raise InputError(f"no schedule {target[9:]!r}; known: {', '.join(self.config.schedules)}")
            self.page(incident, now, self.resolve_targets(incident, [target], now), 0)
            return f"paged {target} for {incident.id}"
        raise InputError(f"unknown command {verb!r}; {COMMAND_HELP}")

    def warnings(self, incident: Incident, now: float) -> List[str]:
        found = []
        severity = self.config.severities.get(incident.severity)
        if severity is None:
            return [f"unknown severity {incident.severity}"]
        if incident.status != "resolved":
            missing = [r for r in severity.roles if r not in incident.roles]
            if missing:
                found.append(f"{incident.severity} needs {', '.join(missing)}")
            commander = incident.roles.get("commander")
            if commander and "comms" in severity.roles and any(incident.roles.get(r) == commander for r in ROLES if r != "commander"):
                found.append(f"{commander} is commander and holds another role; split them at {incident.severity}")
            if severity.status_update:
                last = incident.status_updates[-1]["at"] if incident.status_updates else incident.opened_at
                if now - last > severity.status_update:
                    found.append(f"status page update overdue by {human(now - last - severity.status_update)} ({incident.severity} updates every {human(severity.status_update)})")
        if incident.escalation.state == "exhausted":
            found.append("escalation exhausted with nobody acknowledging")
        return found

    def incident_json(self, incident: Incident, now: float) -> Dict[str, Any]:
        doc = asdict(incident)
        doc["timeline"] = sorted(doc["timeline"], key=lambda e: (e["at"], e["seq"]))
        doc["times"] = incident_times(incident)
        doc["warnings"] = self.warnings(incident, now)
        doc["pages"] = [p for p in self.pages if p["incident"] == incident.id]
        return doc

    def status_page(self) -> Dict[str, Any]:
        worst: Dict[str, str] = {}
        for incident in self.incidents:
            component = (self.config.services.get(incident.service) or {}).get("component")
            if incident.status == "resolved" or not component:
                continue
            state = self.config.severities[incident.severity].component_status
            if COMPONENT_STATES.index(state) > COMPONENT_STATES.index(worst.get(component, "operational")):
                worst[component] = state
        components = {spec["component"]: worst.get(spec["component"], "operational") for spec in self.config.services.values() if spec.get("component")}
        published = [{"id": i.id, "title": i.title, "state": i.status_updates[-1]["state"], "updates": i.status_updates} for i in self.incidents if i.status_updates]
        return {"components": components, "incidents": published}

    def to_json(self) -> Dict[str, Any]:
        return {"config": self.config.doc, "sequence": self.sequence, "entries": self.entries, "incidents": [asdict(i) for i in self.incidents],
                "pages": self.pages, "deploys": self.deploys, "unmatched": self.unmatched}

    @classmethod
    def from_json(cls, doc: Dict[str, Any]) -> "IncidentService":
        service = cls(load_config(doc.get("config")))
        service.sequence, service.entries = int(doc.get("sequence", 0)), int(doc.get("entries", 0))
        service.pages, service.deploys, service.unmatched = list(doc.get("pages") or []), list(doc.get("deploys") or []), list(doc.get("unmatched") or [])
        for item in doc.get("incidents") or []:
            item = dict(item)
            item["timeline"] = [Entry(**e) for e in item.get("timeline") or []]
            item["escalation"] = Escalation(**(item.get("escalation") or {}))
            service.incidents.append(Incident(**item))
        return service


def cell(text: Any) -> str:
    return str(text).replace("|", "\\|").replace("\n", " ")


def contributing_questions(incident: Incident, service: IncidentService) -> List[str]:
    questions = []
    times = incident_times(incident)
    deploys = {d["id"]: d for d in service.deploys}
    start = incident.impact_start if incident.impact_start is not None else incident.opened_at
    for deploy_id in incident.deploys:
        deploy = deploys.get(deploy_id)
        if deploy is None:
            continue
        if deploy["at"] < start:
            questions.append(f"{deploy['service']} {deploy['version']} was deployed {human(start - deploy['at'])} before the first alert. Did it contribute, and what would have caught the problem before production?")
        else:
            questions.append(f"A {deploy['kind']} to {deploy['version']} happened at {hhmmss(deploy['at'])}. Did it change the impact, and what made that the right moment to act?")
    if times["time_to_detect"]:
        questions.append(f"The first alert started at {hhmmss(start)} and the incident opened {human(times['time_to_detect'])} later. Do the alert's `for` and the route's `group_wait` fit this severity?")
    escalations = [e for e in incident.timeline if e.kind == "escalation"]
    if escalations:
        questions.append(f"The page escalated {len(escalations)} time(s) before anyone acknowledged. What made the first page hard to answer: the alert, the schedule, the notification path or the load on that person?")
    changes = [e for e in incident.timeline if e.kind == "severity"]
    if changes:
        questions.append(f"Severity changed from {changes[0].data.get('old')} to {changes[-1].data.get('new')}. What information would have set it right at the start?")
    if incident.status == "resolved" and incident.acknowledged_at is None:
        questions.append("It resolved before anyone acknowledged it. Is this alert actionable, or should it be a ticket or a dashboard?")
    if incident.repeats:
        questions.append(f"Alertmanager re-sent the unchanged group {incident.repeats} time(s). Is the route's `repeat_interval` right for this alert?")
    return questions


def postmortem(service: IncidentService, incident: Incident, now: float) -> str:
    config = service.config
    times = incident_times(incident)
    initial = next((e.data.get("severity") for e in incident.timeline if e.kind == "opened"), incident.severity)
    seen = [initial] + [e.data.get("new") for e in incident.timeline if e.kind == "severity"]
    highest = min(seen, key=lambda name: severity_rank(config, name))
    entries = sorted(incident.timeline, key=lambda e: (e.at, e.seq))
    alerts = sorted({str(a["labels"].get("alertname", "alert")) for a in incident.alerts.values()})
    lines = [f"# Postmortem: {incident.id} {incident.title}", ""]
    lines.append(f"Status: draft. Severity: {highest} at its highest. Service: {incident.service}. Drafted {iso(now)} from the incident timeline.")
    lines.append("This review is blameless: it describes what the system and the people in it saw and did, and asks why each decision made sense at the time. Every FILL IN is for the review meeting, not for this tool.")
    lines += ["", "## Summary", "", "FILL IN: what users experienced, in two sentences, and what ended it.", ""]
    lines.append(f"{incident.id} opened at {iso(incident.opened_at)} from {' and '.join(sorted(incident.notifications)) or 'a command'} and was "
                 + (f"resolved at {iso(incident.resolved_at)}, {human(times['time_to_resolve'])} later." if incident.resolved_at is not None else f"still {incident.status} when this draft was made."))
    lines += ["", "## Impact", "", "| Measure | Value |", "|---|---|"]
    lines.append(f"| Impact start (first alert) | {iso(incident.impact_start) or 'unknown'} |")
    lines.append(f"| Opened | {iso(incident.opened_at)} |")
    lines.append(f"| Acknowledged | {iso(incident.acknowledged_at) or 'never'} |")
    lines.append(f"| Resolved | {iso(incident.resolved_at) or 'not yet'} |")
    lines.append(f"| Time to detect | {human(times['time_to_detect'])} |")
    lines.append(f"| Time to acknowledge | {human(times['time_to_acknowledge'])} |")
    lines.append(f"| Time to resolve | {human(times['time_to_resolve'])} |")
    lines.append(f"| Alerts | {len(incident.alerts)} ({cell(', '.join(alerts)) or 'none'}) |")
    lines.append(f"| Status page updates | {len(incident.status_updates)} |")
    lines.append("| Users affected | FILL IN |")
    lines += ["", "## Timeline", "", "| Time (UTC) | Event | Source |", "|---|---|---|"]
    for entry in entries:
        lines.append(f"| {iso(entry.at)} | {cell(entry.text)} | {cell(entry.actor)} |")
    lines += ["", "## Response", ""]
    lines.append("- Roles at the end: " + (", ".join(f"{role} {incident.roles[role]}" for role in ROLES if role in incident.roles) or "none assigned"))
    for handoff in incident.handoffs:
        lines.append(f"- Handoff at {hhmmss(handoff['at'])}: {handoff['role']} from {handoff['from']} to {handoff['to']}")
    for page in [p for p in service.pages if p["incident"] == incident.id]:
        lines.append(f"- Page at {hhmmss(page['at'])}: {page['user']} ({'level ' + str(page['level']) if page['level'] else 'manual'}, {page['via']})")
    if incident.status_updates:
        first = incident.status_updates[0]["at"] - incident.opened_at
        lines.append(f"- First public status update {human(first)} after opening; {len(incident.status_updates)} in total")
    lines += ["", "## Contributing factors", "", "Questions the timeline raises. Answer them in the review; do not assume the answers.", ""]
    lines += [f"- {q}" for q in contributing_questions(incident, service)] or ["- The timeline raised no questions on its own."]
    lines += ["", "FILL IN: the contributing factors the review agrees on. There is rarely a single root cause.", ""]
    lines += ["## What went well", "", "FILL IN", "", "## What was hard", "", "FILL IN", ""]
    lines += ["## Action items", "", "| Action | Kind (detect, mitigate, prevent) | Owner | Due |", "|---|---|---|---|", "| FILL IN | | | |", ""]
    lines.append("Each action has one owner and a due date. Prefer actions that change the system (a test, an alert, a limit, a runbook step) over asking people to be more careful.")
    lines += ["", "## Language check", ""]
    flagged = [e for e in entries if BLAME.search(e.text)]
    if flagged:
        lines += [f"- {hhmmss(e.at)} {e.actor}: \"{e.text}\" uses \"{BLAME.search(e.text).group(0)}\"; rewrite it as a question about the system" for e in flagged]
    else:
        lines.append("- No blame words found in the timeline.")
    return "\n".join(lines) + "\n"


def read_json(path: str) -> Any:
    try:
        with (sys.stdin if path == "-" else open(path, encoding="utf-8")) as handle:
            return json.load(handle)
    except OSError as exc:
        raise InputError(f"cannot read {path}: {exc.strerror}")
    except json.JSONDecodeError as exc:
        raise InputError(f"{path} is not JSON: {exc}")


def write_state(service: IncidentService, path: Optional[str]) -> None:
    if not path:
        return
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(service.to_json(), handle, indent=2)
    os.replace(tmp, path)


class Clock:
    def __init__(self, start: Optional[float] = None):
        self.virtual = start is not None
        self.value = start if start is not None else 0.0

    def now(self) -> float:
        return self.value if self.virtual else time.time()

    def move(self, when: Any) -> float:
        if not self.virtual:
            raise InputError("the clock is real; start serve with --virtual-clock TIME to move it")
        target = parse_when(when, self.value)
        if target < self.value:
            raise InputError(f"the clock only moves forward: {iso(target)} is before {iso(self.value)}")
        self.value = target
        return target


def make_handler(service: IncidentService, clock: Clock, lock: threading.Lock, state_path: Optional[str]) -> type:
    class Handler(BaseHTTPRequestHandler):
        server_version = "miniincident/1.0"

        def log_message(self, fmt: str, *args: Any) -> None:
            return

        def reply(self, code: int, body: Any, content_type: str = "application/json") -> None:
            data = body.encode("utf-8") if isinstance(body, str) else json.dumps(body, indent=2).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def body(self) -> Any:
            raw = self.rfile.read(int(self.headers.get("Content-Length") or 0))
            try:
                return json.loads(raw.decode("utf-8") or "null")
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise InputError(f"request body is not JSON: {exc}")

        def do_GET(self) -> None:
            url = urlparse(self.path)
            query = {k: v[-1] for k, v in parse_qs(url.query).items()}
            parts = [p for p in url.path.split("/") if p]
            try:
                with lock:
                    now = clock.now()
                    if url.path == "/-/ready":
                        return self.reply(200, "ok\n", "text/plain")
                    if parts == ["incidents"]:
                        return self.reply(200, [service.incident_json(i, now) for i in service.incidents])
                    if len(parts) == 2 and parts[0] == "incidents":
                        return self.reply(200, service.incident_json(service.get(parts[1]), now))
                    if len(parts) == 3 and parts[0] == "incidents" and parts[2] == "postmortem":
                        return self.reply(200, postmortem(service, service.get(parts[1]), now), "text/markdown; charset=utf-8")
                    if parts == ["stats"]:
                        return self.reply(200, metrics(service.incidents))
                    if parts == ["status"]:
                        return self.reply(200, service.status_page())
                    if parts == ["pages"]:
                        return self.reply(200, service.pages)
                    if parts == ["clock"]:
                        return self.reply(200, {"now": iso(now), "epoch": now, "virtual": clock.virtual})
                    if parts == ["oncall"]:
                        at = parse_when(query.get("at", "+0s"), now)
                        names = [query["schedule"]] if query.get("schedule") else list(service.config.schedules)
                        unknown = [n for n in names if n not in service.config.schedules]
                        if unknown:
                            raise InputError(f"no schedule {unknown[0]!r}")
                        return self.reply(200, [asdict(on_call(service.config.schedules[n], at)) for n in names])
                self.reply(404, {"error": f"no route GET {url.path}"})
            except InputError as exc:
                self.reply(400, {"error": str(exc)})

        def do_POST(self) -> None:
            path = urlparse(self.path).path
            try:
                body = self.body()
                with lock:
                    if path == "/clock":
                        if not isinstance(body, dict) or "now" not in body:
                            raise InputError('POST /clock wants {"now": "2026-11-02T10:05:00Z"} or {"now": "+5m"}')
                        clock.move(body["now"])
                        result: Any = {"now": iso(clock.now()), "escalations": service.tick(clock.now())}
                        write_state(service, state_path)
                        return self.reply(200, result)
                    now = clock.now()
                    service.tick(now)
                    if path == "/alertmanager":
                        result, code = service.receive_alertmanager(body, now), 200
                    elif path == "/v2/enqueue":
                        result, code = service.receive_pagerduty(body, now), 202
                    elif path == "/deploys":
                        result, code = service.record_deploy(body, now), 201
                    elif path == "/commands":
                        if not isinstance(body, dict) or not body.get("text"):
                            raise InputError('POST /commands wants {"user": "alice", "text": "/inc ack INC-1"}')
                        result, code = {"reply": service.command(str(body.get("user") or ""), str(body["text"]), now)}, 200
                    else:
                        return self.reply(404, {"error": f"no route POST {path}"})
                    write_state(service, state_path)
                self.reply(code, result)
            except InputError as exc:
                self.reply(400, {"status": "invalid event", "error": str(exc)})

    return Handler


def serve(service: IncidentService, listen: str, clock: Clock, state_path: Optional[str]) -> int:
    host, _, port = listen.rpartition(":")
    lock = threading.Lock()
    server = ThreadingHTTPServer((host or "127.0.0.1", int(port)), make_handler(service, clock, lock, state_path))
    print(f"miniincident listening on http://{server.server_address[0]}:{server.server_address[1]} ({'virtual clock at ' + iso(clock.now()) if clock.virtual else 'real clock'})", flush=True)
    stop = threading.Event()

    def ticker() -> None:
        while not stop.wait(1.0):
            with lock:
                if service.tick(clock.now()):
                    write_state(service, state_path)

    if not clock.virtual:
        threading.Thread(target=ticker, daemon=True).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        server.server_close()
    return 0


def replay(service: IncidentService, scenario: Dict[str, Any]) -> List[Dict[str, Any]]:
    start = parse_time(scenario.get("start"))
    if start is None:
        raise InputError("a scenario needs start (RFC 3339) and steps")
    log = []
    for number, step in enumerate(scenario.get("steps") or [], 1):
        at = parse_when(step.get("at", "+0s"), start)
        for event in service.tick(at):
            log.append({"at": event["at"], "step": number, "kind": "escalation", "result": event})
        if "webhook" in step:
            result, kind = service.receive_alertmanager(step["webhook"], at), "webhook"
        elif "pagerduty" in step:
            result, kind = service.receive_pagerduty(step["pagerduty"], at), "pagerduty"
        elif "deploy" in step:
            result, kind = service.record_deploy(step["deploy"], at), "deploy"
        elif "command" in step:
            result, kind = {"reply": service.command(str(step["command"].get("user", "")), str(step["command"].get("text", "")), at)}, "command"
        elif "tick" in step:
            result, kind = {}, "tick"
        else:
            raise InputError(f"step {number}: expected one of webhook, pagerduty, deploy, command, tick")
        log.append({"at": at, "step": number, "kind": kind, "result": result, "input": step})
    return log


def describe_step(row: Dict[str, Any]) -> str:
    result, step = row["result"], row.get("input") or {}
    if row["kind"] == "escalation":
        return f"escalation {result['incident']} " + ("exhausted" if result.get("exhausted") else f"to level {result['level']}")
    if row["kind"] == "webhook":
        hook = step["webhook"]
        return f"webhook   {hook.get('status')} {hook.get('groupKey')}: {result['action']} {result.get('incident', '')}".rstrip()
    if row["kind"] == "pagerduty":
        return f"pagerduty {step['pagerduty'].get('event_action')} {result['dedup_key'][:12]}..."
    if row["kind"] == "deploy":
        return f"deploy    {result['kind']} {result['service']} {result['version']} by {result['actor']}"
    if row["kind"] == "command":
        return f"command   {step['command'].get('user')}: {step['command'].get('text')} -> {result['reply']}"
    return "tick"


def render_replay(service: IncidentService, log: List[Dict[str, Any]], now: float) -> str:
    lines = ["steps"]
    lines += [f"  {hhmmss(row['at'])}  {describe_step(row)}" for row in log]
    lines.append("incidents")
    for incident in service.incidents:
        times = incident_times(incident)
        lines.append(f"  {incident.id}  {incident.severity}  {incident.status:<12}  {incident.title}")
        lines.append(f"         TTD {human(times['time_to_detect'])}  TTA {human(times['time_to_acknowledge'])}  TTR {human(times['time_to_resolve'])}"
                     f"  alerts {len(incident.alerts)}  notifications {sum(incident.notifications.values())} ({incident.repeats} repeat)  roles {', '.join(f'{r}={u}' for r, u in incident.roles.items()) or 'none'}")
        for warning in service.warnings(incident, now):
            lines.append(f"         warning: {warning}")
    lines.append("pages")
    lines += [f"  {hhmmss(p['at'])}  {p['incident']}  {'level ' + str(p['level']) if p['level'] else 'manual '}  {p['user']:<8} {p['via']}" for p in service.pages]
    stats = metrics(service.incidents)
    lines.append(f"MTTA {human(stats['mtta']['mean'])} over {stats['mtta']['count']} acknowledged, MTTR {human(stats['mttr']['mean'])} over {stats['mttr']['count']} resolved,"
                 f" {stats['resolved_without_ack']} resolved without acknowledgement, {stats['escalations']} escalation(s)")
    return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="miniincident", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("serve", help="receive webhooks, PagerDuty-style events, deploys and /inc commands over HTTP")
    run.add_argument("--config", required=True)
    run.add_argument("--listen", default="127.0.0.1:8085", help="HOST:PORT; port 0 picks a free one")
    run.add_argument("--state", help="write the whole state as JSON after every change")
    run.add_argument("--virtual-clock", metavar="TIME", help="start a virtual clock at TIME; POST /clock moves it")
    rep = sub.add_parser("replay", help="run a scripted scenario on a virtual clock")
    rep.add_argument("scenario")
    rep.add_argument("--config", required=True)
    rep.add_argument("--state", help="also write the final state as JSON")
    rep.add_argument("--postmortem", metavar="ID", help="print the postmortem draft for this incident instead")
    rep.add_argument("--json", action="store_true")
    oc = sub.add_parser("oncall", help="who is on call at a time, or the final schedule over a range with its gaps")
    oc.add_argument("--config", required=True)
    oc.add_argument("--schedule", help="one schedule (default: all)")
    oc.add_argument("--at", help="RFC 3339 time (default: now)")
    oc.add_argument("--from", dest="start", help="render the final schedule from this time")
    oc.add_argument("--to", dest="end", help="until this time (or +7d)")
    oc.add_argument("--json", action="store_true")
    sev = sub.add_parser("severity", help="look up the severity matrix and the response it requires")
    sev.add_argument("--config", required=True)
    sev.add_argument("--impact")
    sev.add_argument("--scope")
    sev.add_argument("--label", action="append", default=[], metavar="NAME=VALUE", help="alert labels, as Alertmanager would send them")
    sev.add_argument("--json", action="store_true")
    pm = sub.add_parser("postmortem", help="draft the postmortem of one incident from a saved state")
    pm.add_argument("--state", required=True)
    pm.add_argument("--incident", required=True)
    pm.add_argument("--now", help="the time printed as the draft time (default: the resolve time)")
    met = sub.add_parser("metrics", help="MTTD, MTTA and MTTR from a saved state")
    met.add_argument("--state", required=True)
    met.add_argument("--json", action="store_true")
    return parser


def cmd_oncall(args: argparse.Namespace, config: Config) -> int:
    names = [args.schedule] if args.schedule else list(config.schedules)
    unknown = [n for n in names if n not in config.schedules]
    if unknown:
        raise InputError(f"no schedule {unknown[0]!r}; known: {', '.join(config.schedules)}")
    if args.start:
        start = parse_when(args.start, time.time())
        end = parse_when(args.end or "+7d", start)
        report = {n: render_schedule(config.schedules[n], start, end) for n in names}
        gaps = sum(1 for segments in report.values() for s in segments if s["user"] is None)
        if args.json:
            print(json.dumps({n: [{**s, "start": iso(s["start"]), "end": iso(s["end"])} for s in segs] for n, segs in report.items()}, indent=2))
        else:
            for name, segments in report.items():
                print(name)
                for s in segments:
                    print(f"  {iso(s['start'])}  {iso(s['end'])}  {s['user'] or 'NOBODY':<8} {s['source']}")
        return 1 if gaps else 0
    at = parse_when(args.at, time.time()) if args.at else time.time()
    rows = [on_call(config.schedules[n], at) for n in names]
    if args.json:
        print(json.dumps([{**asdict(r), "at": iso(at), "since": iso(r.since)} for r in rows], indent=2))
    else:
        for r in rows:
            print(f"{r.schedule:<20} {r.user or 'NOBODY':<8} {r.source}" + (f" since {iso(r.since)}" if r.since is not None else ""))
    return 1 if any(r.user is None for r in rows) else 0


def cmd_severity(args: argparse.Namespace, config: Config) -> int:
    if args.impact or args.scope:
        name, why = classify(config, args.impact or "", args.scope or ""), f"matrix impact={args.impact} scope={args.scope}"
    else:
        labels = dict(item.split("=", 1) for item in args.label if "=" in item)
        name, why = severity_from_labels(config, labels)
    sev = config.severities[name]
    if args.json:
        print(json.dumps({"severity": name, "why": why, **asdict(sev)}, indent=2))
        return 0
    update = f"public update every {human(sev.status_update)}" if sev.status_update else "no status page cadence"
    print(f"{name} ({why}): {sev.description}")
    print(f"  page: {'yes' if sev.page else 'no'}; roles: {', '.join(sev.roles) or 'none'}; {update}; component {sev.component_status}; postmortem {'required' if sev.postmortem else 'optional'}")
    return 0


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(list(sys.argv[1:] if argv is None else argv))
    try:
        if args.command in ("postmortem", "metrics"):
            service = IncidentService.from_json(read_json(args.state))
            if args.command == "metrics":
                stats = metrics(service.incidents)
                if args.json:
                    print(json.dumps(stats, indent=2))
                else:
                    for name in ("mttd", "mtta", "mttr"):
                        print(f"{name.upper():<5} mean {human(stats[name]['mean']):>8}  median {human(stats[name]['median']):>8}  over {stats[name]['count']}")
                    print(f"incidents {stats['incidents']}, open {stats['open']}, resolved without acknowledgement {stats['resolved_without_ack']}, escalations {stats['escalations']}")
                return 0
            incident = service.get(args.incident)
            now = parse_time(args.now) if args.now else (incident.resolved_at or incident.opened_at)
            sys.stdout.write(postmortem(service, incident, now or incident.opened_at))
            return 0
        config = load_config(read_json(args.config))
        if args.command == "oncall":
            return cmd_oncall(args, config)
        if args.command == "severity":
            return cmd_severity(args, config)
        service = IncidentService(config)
        if args.command == "serve":
            clock = Clock(parse_time(args.virtual_clock) if args.virtual_clock else None)
            return serve(service, args.listen, clock, args.state)
        scenario = read_json(args.scenario)
        log = replay(service, scenario)
        end = max([row["at"] for row in log] or [parse_time(scenario.get("start")) or 0.0])
        write_state(service, args.state)
        if args.postmortem:
            sys.stdout.write(postmortem(service, service.get(args.postmortem), end))
        elif args.json:
            print(json.dumps({"steps": [{**row, "at": iso(row["at"])} for row in log], "incidents": [service.incident_json(i, end) for i in service.incidents],
                              "pages": service.pages, "metrics": metrics(service.incidents), "status_page": service.status_page()}, indent=2))
        else:
            print(render_replay(service, log, end))
        return 0
    except InputError as exc:
        print(f"miniincident: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
