"""minichaos: run a chaos experiment as data. Measure a steady-state hypothesis from a Prometheus-format endpoint, inject faults inside a blast radius, abort when the SLI breaks, always roll back, and write a journal with a verdict and its evidence."""
from __future__ import annotations

import argparse
import bisect
import http.client
import json
import math
import os
import random
import re
import shlex
import signal
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, List, Optional, Sequence, Tuple

Labels = Tuple[Tuple[str, str], ...]

METRIC_NAME = re.compile(r"[a-zA-Z_:][a-zA-Z0-9_:]*")
LABEL_NAME = re.compile(r"[a-zA-Z_][a-zA-Z0-9_]*")
SELECTOR = re.compile(r"^\s*([a-zA-Z_:][a-zA-Z0-9_:]*)\s*(?:\{(.*)\})?\s*$", re.S)
MATCHER = re.compile(r'\s*([a-zA-Z_][a-zA-Z0-9_]*)\s*(=~|!~|!=|=)\s*"((?:[^"\\]|\\.)*)"\s*(?:,|$)')
ESCAPES = {"n": "\n", "\\": "\\", '"': '"'}
SLI_TYPES = ("error_ratio", "latency_quantile")
MODES = ("one", "all", "fixed", "fixed-percent", "random-max-percent")
PERCENT_MODES = ("fixed-percent", "random-max-percent")
SIGNALS = {"TERM": signal.SIGTERM, "KILL": signal.SIGKILL, "INT": signal.SIGINT, "HUP": signal.SIGHUP}
PROTECTED_NAMESPACES = ("kube-system", "kube-public", "kube-node-lease")
LOAD_BUCKETS = (0.005, 0.01, 0.025, 0.05, 0.1, 0.15, 0.25, 0.5, 1.0, 2.5, 5.0)
LOAD_REQUESTS = "minichaos_load_requests_total"
LOAD_LATENCY = "minichaos_load_request_duration_seconds"
OUTCOMES = ("error", "ok")
ATTEMPTS = 3
BURN_GRACE_S = 30.0
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


class InputError(ValueError):
    pass


class Refused(Exception):
    pass


class FaultError(Exception):
    pass


class ProbeError(Exception):
    pass


@dataclass(frozen=True)
class Sample:
    name: str
    labels: Labels
    value: float


def parse_value(text: str, where: str) -> float:
    try:
        return float(text)
    except ValueError:
        raise InputError(f"{where}: {text!r} is not a number")


def parse_labels(line: str, position: int, where: str) -> Tuple[Labels, int]:
    labels: List[Tuple[str, str]] = []
    while True:
        while position < len(line) and line[position] in " ,":
            position += 1
        if position >= len(line):
            raise InputError(f"{where}: missing }} after the labels")
        if line[position] == "}":
            return tuple(sorted(labels)), position + 1
        name = LABEL_NAME.match(line, position)
        if not name or line[name.end():name.end() + 2] != '="':
            raise InputError(f'{where}: expected label="value" at column {position + 1}')
        position = name.end() + 2
        value: List[str] = []
        while True:
            if position >= len(line):
                raise InputError(f"{where}: the value of label {name.group(0)} has no closing quote")
            char = line[position]
            if char == "\\" and position + 1 < len(line):
                value.append(ESCAPES.get(line[position + 1], "\\" + line[position + 1]))
                position += 2
            elif char == '"':
                position += 1
                break
            else:
                value.append(char)
                position += 1
        labels.append((name.group(0), "".join(value)))


def parse_exposition(text: str, source: str = "metrics") -> List[Sample]:
    samples: List[Sample] = []
    for number, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        where = f"{source}:{number}"
        name = METRIC_NAME.match(line)
        if not name:
            raise InputError(f"{where}: {line[:60]!r} does not start with a metric name")
        labels: Labels = ()
        position = name.end()
        if position < len(line) and line[position] == "{":
            labels, position = parse_labels(line, position + 1, where)
        fields = line[position:].split(" # ")[0].split()
        if not fields or len(fields) > 2:
            raise InputError(f"{where}: expected a value and an optional timestamp after {name.group(0)}")
        samples.append(Sample(name.group(0), labels, parse_value(fields[0], where)))
    return samples


@dataclass(frozen=True)
class Selector:
    name: str
    matchers: Tuple[Tuple[str, str, str], ...] = ()

    def matches(self, sample: Sample, name: str = "", ignore: Tuple[str, ...] = ()) -> bool:
        if sample.name != (name or self.name):
            return False
        labels = dict(sample.labels)
        for key, op, value in self.matchers:
            if key in ignore:
                continue
            actual = labels.get(key, "")
            if op == "=" and actual != value:
                return False
            if op == "!=" and actual == value:
                return False
            if op == "=~" and not re.fullmatch(value, actual):
                return False
            if op == "!~" and re.fullmatch(value, actual):
                return False
        return True

    def text(self) -> str:
        if not self.matchers:
            return self.name
        return self.name + "{" + ",".join(f'{k}{op}"{v}"' for k, op, v in self.matchers) + "}"


def parse_selector(text: str) -> Selector:
    match = SELECTOR.match(text or "")
    if not match:
        raise InputError(f'not a series selector: {text!r} (example: http_requests_total{{code=~"5.."}})')
    body = (match.group(2) or "").strip()
    matchers: List[Tuple[str, str, str]] = []
    position = 0
    while position < len(body):
        part = MATCHER.match(body, position)
        if not part or part.end() == position:
            raise InputError(f"selector {text!r}: cannot read a matcher at {body[position:]!r}")
        key, op, raw = part.groups()
        value = re.sub(r"\\(.)", lambda m: ESCAPES.get(m.group(1), m.group(1)), raw)
        if op in ("=~", "!~"):
            try:
                re.compile(value)
            except re.error as exc:
                raise InputError(f"selector {text!r}: {value!r} is not a regular expression ({exc})")
        matchers.append((key, op, value))
        position = part.end()
    return Selector(match.group(1), tuple(matchers))


def series(samples: Sequence[Sample], selector: Selector, name: str = "", ignore: Tuple[str, ...] = ()) -> Dict[Labels, float]:
    return {s.labels: s.value for s in samples if selector.matches(s, name, ignore) and not math.isnan(s.value)}


def counter_increase(before: Dict[Labels, float], after: Dict[Labels, float]) -> float:
    total = 0.0
    for labels, value in after.items():
        previous = before.get(labels, 0.0)
        total += value if value < previous else value - previous
    return total


def bucket_increases(before: Sequence[Sample], after: Sequence[Sample], selector: Selector) -> List[Tuple[float, float]]:
    name = selector.name + "_bucket"
    old = series(before, selector, name, ("le",))
    by_bound: Dict[float, float] = {}
    for labels, value in series(after, selector, name, ("le",)).items():
        le = dict(labels).get("le")
        if le is None:
            continue
        bound = parse_value(le, f"{name} le")
        previous = old.get(labels, 0.0)
        by_bound[bound] = by_bound.get(bound, 0.0) + (value if value < previous else value - previous)
    return sorted(by_bound.items())


def bucket_quantile(q: float, buckets: Sequence[Tuple[float, float]]) -> Optional[float]:
    if not buckets or buckets[-1][0] != math.inf or buckets[-1][1] <= 0:
        return None
    counts: List[float] = []
    for _, count in buckets:
        counts.append(max(count, counts[-1]) if counts else count)
    rank = q * counts[-1]
    lower, below = 0.0, 0.0
    for index, (bound, _) in enumerate(buckets):
        if counts[index] >= rank:
            if bound == math.inf:
                return buckets[-2][0] if len(buckets) > 1 else None
            if index == 0 and bound <= 0:
                return bound
            if counts[index] <= below:
                return bound
            return lower + (bound - lower) * (rank - below) / (counts[index] - below)
        lower, below = bound, counts[index]
    return None


@dataclass
class SLI:
    name: str
    kind: str
    maximum: float
    errors: Optional[Selector] = None
    total: Optional[Selector] = None
    histogram: Optional[Selector] = None
    quantile: float = 0.95

    def evaluate(self, before: Sequence[Sample], after: Sequence[Sample]) -> Tuple[Optional[float], float]:
        if self.kind == "error_ratio":
            total = counter_increase(series(before, self.total), series(after, self.total))
            errors = counter_increase(series(before, self.errors), series(after, self.errors))
            return (errors / total if total > 0 else None), total
        buckets = bucket_increases(before, after, self.histogram)
        return bucket_quantile(self.quantile, buckets), (buckets[-1][1] if buckets else 0.0)


@dataclass
class Reading:
    name: str
    value: Optional[float]
    maximum: float
    requests: float
    status: str

    def text(self) -> str:
        if self.status == "no-data":
            return f"{self.name} no data"
        return f"{self.name} {self.value:.3f} {'<=' if self.status == 'ok' else '>'} {self.maximum:g}"


@dataclass
class CheckResult:
    name: str
    ok: bool
    detail: str


@dataclass
class Measurement:
    phase: str
    at: float
    window_s: float
    readings: List[Reading] = field(default_factory=list)
    checks: List[CheckResult] = field(default_factory=list)
    error: str = ""

    @property
    def status(self) -> str:
        if self.error:
            return "error"
        states = [r.status for r in self.readings] + ["ok" if c.ok else "breach" for c in self.checks]
        if "breach" in states:
            return "breach"
        return "no-data" if "no-data" in states else "ok"

    @property
    def requests(self) -> float:
        return max((r.requests for r in self.readings), default=0.0)

    def reading(self, name: str) -> Optional[Reading]:
        return next((r for r in self.readings if r.name == name), None)

    def text(self) -> str:
        if self.error:
            return f"error: {self.error}"
        parts = [r.text() for r in self.readings] + [c.detail for c in self.checks]
        return f"{self.status}: " + ", ".join(parts) + f" ({self.requests:g} requests in {self.window_s:g} s)"

    def to_json(self) -> Dict[str, Any]:
        return {
            "phase": self.phase,
            "at": self.at,
            "window_s": self.window_s,
            "status": self.status,
            "error": self.error,
            "readings": [{"sli": r.name, "value": r.value, "max": r.maximum, "requests": r.requests, "status": r.status} for r in self.readings],
            "checks": [{"name": c.name, "ok": c.ok, "detail": c.detail} for c in self.checks],
        }


@dataclass
class Check:
    kind: str
    namespace: str
    name: str

    def run(self, world: Any) -> CheckResult:
        argv = ["kubectl", "get", "deployment", self.name, "-n", self.namespace, "-o", "json"]
        code, out, err = world.run(argv)
        label = f"deployment {self.namespace}/{self.name}"
        if code != 0:
            return CheckResult(label, False, f"{label}: {(err or out).strip()}")
        try:
            doc = json.loads(out)
        except json.JSONDecodeError:
            return CheckResult(label, False, f"{label}: kubectl did not print JSON")
        want = int((doc.get("spec") or {}).get("replicas", 1))
        status = doc.get("status") or {}
        ready, available, updated = (int(status.get(k) or 0) for k in ("readyReplicas", "availableReplicas", "updatedReplicas"))
        ok = ready == want and available == want and updated == want
        return CheckResult(label, ok, f"{label} {ready}/{want} ready")


@dataclass
class Hypothesis:
    source: Dict[str, Any]
    interval_s: float
    min_requests: float
    slis: List[SLI]
    checks: List[Check]
    recovery_s: float


@dataclass
class AbortRule:
    sli: str
    above: float
    count: int


@dataclass
class BlastRadius:
    namespaces: List[str]
    netns: List[str]
    max_percent: float = 50.0
    max_pods: Optional[int] = None
    max_processes: int = 1
    max_workers: int = 1
    max_duration_s: float = 300.0
    allow_host_network: bool = False
    allow_system_namespaces: bool = False


def selection_size(mode: str, value: float, count: int, worst_case: bool = False, rng: Optional[random.Random] = None) -> int:
    if mode == "one":
        return min(1, count)
    if mode == "all":
        return count
    if mode == "fixed":
        if value < 1:
            raise InputError("mode fixed needs a value of at least 1")
        return min(int(value), count)
    if not 0 < value <= 100:
        raise InputError(f"mode {mode} needs a percentage in (0, 100], got {value:g}")
    percent = value if mode == "fixed-percent" or worst_case else (rng or random.Random()).randint(0, int(value))
    return math.ceil(count * percent / 100 - 1e-9)


def allowed_count(count: int, blast: BlastRadius) -> int:
    allowed = math.floor(count * blast.max_percent / 100 + 1e-9)
    return allowed if blast.max_pods is None else min(allowed, blast.max_pods)


def namespace_refusals(namespace: str, blast: BlastRadius) -> List[str]:
    if namespace in PROTECTED_NAMESPACES and not blast.allow_system_namespaces:
        return [f"namespace {namespace} runs cluster add-ons; set blast_radius.allow_system_namespaces to target it"]
    if namespace not in blast.namespaces:
        return [f"namespace {namespace} is not in blast_radius.namespaces ({', '.join(blast.namespaces) or 'empty'})"]
    return []


def percent_refusals(mode: str, value: float, blast: BlastRadius) -> List[str]:
    if mode == "all" and blast.max_percent < 100:
        return [f"mode all affects 100% of the targets; blast_radius.max_percent is {blast.max_percent:g}"]
    if mode in PERCENT_MODES and value > blast.max_percent:
        return [f"mode {mode} {value:g}% is above blast_radius.max_percent {blast.max_percent:g}"]
    return []


def choose(names: Sequence[str], mode: str, value: float, blast: BlastRadius, rng: random.Random, what: str) -> List[str]:
    if not names:
        raise Refused(f"no running {what}: a fault that hits nothing proves nothing")
    wanted = selection_size(mode, value, len(names), worst_case=True)
    allowed = allowed_count(len(names), blast)
    if wanted > allowed:
        cap = f", max_pods {blast.max_pods}" if blast.max_pods is not None else ""
        raise Refused(f"mode {mode} would affect {wanted} of {len(names)} {what}; the blast radius allows {allowed} ({blast.max_percent:g}% rounded down{cap})")
    size = selection_size(mode, value, len(names), rng=rng) if mode == "random-max-percent" else wanted
    return sorted(rng.sample(list(names), size))


def running_pods(world: Any, namespace: str, selector: str) -> List[str]:
    argv = ["kubectl", "get", "pods", "-n", namespace, "-l", selector, "-o", "json"]
    code, out, err = world.run(argv)
    if code != 0:
        raise FaultError(f"{shlex.join(argv)}: {(err or out).strip()}")
    try:
        items = json.loads(out).get("items") or []
    except json.JSONDecodeError:
        raise FaultError(f"{shlex.join(argv)} did not print JSON")
    return sorted(
        p["metadata"]["name"]
        for p in items
        if (p.get("status") or {}).get("phase") == "Running" and not p["metadata"].get("deletionTimestamp")
    )


def require(spec: Dict[str, Any], key: str, what: str) -> str:
    value = spec.get(key)
    if not isinstance(value, str) or not value:
        raise InputError(f"{what}: {key} is required")
    return value


def number(spec: Dict[str, Any], key: str, default: float, what: str) -> float:
    try:
        return float(spec.get(key, default))
    except (TypeError, ValueError):
        raise InputError(f"{what}: {key} must be a number, got {spec.get(key)!r}")


class Fault:
    kind = "fault"

    def describe(self) -> str:
        return self.kind

    def refusals(self, blast: BlastRadius) -> List[str]:
        return []

    def plan(self, world: Any, blast: BlastRadius) -> List[str]:
        return []

    def commands(self, duration_s: float) -> List[List[str]]:
        return []

    def start(self, world: Any, blast: BlastRadius, duration_s: float) -> str:
        raise NotImplementedError

    def revert(self, world: Any) -> str:
        return "nothing to undo"


class PodDelete(Fault):
    kind = "pod-delete"

    def __init__(self, spec: Dict[str, Any]):
        self.namespace = require(spec, "namespace", self.kind)
        self.selector = require(spec, "selector", self.kind)
        self.mode = str(spec.get("mode", "one"))
        self.value = number(spec, "value", 1, self.kind)
        self.grace_period_s = spec.get("grace_period_s")
        self.deleted: List[str] = []
        if self.mode not in MODES:
            raise InputError(f"pod-delete: mode {self.mode!r} is not one of {', '.join(MODES)}")
        if self.grace_period_s is not None and (not isinstance(self.grace_period_s, int) or self.grace_period_s < 0):
            raise InputError("pod-delete: grace_period_s must be a whole number of seconds, 0 or more")
        selection_size(self.mode, self.value, 1, worst_case=True)

    def describe(self) -> str:
        value = f" {self.value:g}" if self.mode in ("fixed",) + PERCENT_MODES else ""
        grace = "the pod's own grace period" if self.grace_period_s is None else f"grace period {self.grace_period_s} s"
        return f"delete pods matching {self.selector} in {self.namespace} (mode {self.mode}{value}, {grace})"

    def refusals(self, blast: BlastRadius) -> List[str]:
        return namespace_refusals(self.namespace, blast) + percent_refusals(self.mode, self.value, blast)

    def select(self, world: Any, blast: BlastRadius) -> Tuple[List[str], List[str]]:
        pods = running_pods(world, self.namespace, self.selector)
        return choose(pods, self.mode, self.value, blast, world.rng, f"pods matching {self.selector} in {self.namespace}"), pods

    def plan(self, world: Any, blast: BlastRadius) -> List[str]:
        chosen, pods = self.select(world, blast)
        return [f"{len(chosen)} of {len(pods)} running pods, chosen at injection time from {', '.join(pods)}"]

    def delete_command(self, pods: Sequence[str]) -> List[str]:
        argv = ["kubectl", "delete", "pod", "-n", self.namespace] + list(pods) + ["--wait=false"]
        if self.grace_period_s is not None:
            argv.append(f"--grace-period={self.grace_period_s}")
            if self.grace_period_s == 0:
                argv.append("--force")
        return argv

    def commands(self, duration_s: float) -> List[List[str]]:
        return [["kubectl", "get", "pods", "-n", self.namespace, "-l", self.selector, "-o", "json"], self.delete_command(["POD..."])]

    def start(self, world: Any, blast: BlastRadius, duration_s: float) -> str:
        targets, _ = self.select(world, blast)
        argv = self.delete_command(targets)
        code, out, err = world.run(argv)
        if code != 0:
            raise FaultError(f"{shlex.join(argv)}: {(err or out).strip()}")
        self.deleted = targets
        return f"deleted {', '.join(targets)}"

    def revert(self, world: Any) -> str:
        return "nothing to undo: the ReplicaSet controller creates a replacement for every deleted pod"


class ProcessKill(Fault):
    kind = "process-kill"

    def __init__(self, spec: Dict[str, Any]):
        self.match = require(spec, "match", self.kind)
        self.signal_name = str(spec.get("signal", "TERM")).upper()
        if self.signal_name.startswith("SIG"):
            self.signal_name = self.signal_name[3:]
        if self.signal_name not in SIGNALS:
            raise InputError(f"process-kill: signal {self.signal_name!r} is not one of {', '.join(SIGNALS)}")
        self.killed: List[int] = []

    def describe(self) -> str:
        return f"send SIG{self.signal_name} to the process whose command line contains {self.match!r}"

    def refusals(self, blast: BlastRadius) -> List[str]:
        if len(self.match) < 4:
            return [f"process-kill match {self.match!r} is shorter than 4 characters and would match too much"]
        return []

    def targets(self, world: Any, blast: BlastRadius) -> List[Tuple[int, str]]:
        found = [(pid, command) for pid, command in world.processes() if self.match in command and pid not in world.protected]
        if not found:
            raise Refused(f"no process matches {self.match!r}: a fault that hits nothing proves nothing")
        if len(found) > blast.max_processes:
            raise Refused(f"{len(found)} processes match {self.match!r} (pids {', '.join(str(p) for p, _ in found)}); blast_radius.max_processes is {blast.max_processes}")
        return found

    def plan(self, world: Any, blast: BlastRadius) -> List[str]:
        return [f"{pid} {command}" for pid, command in self.targets(world, blast)]

    def start(self, world: Any, blast: BlastRadius, duration_s: float) -> str:
        for pid, _ in self.targets(world, blast):
            try:
                world.kill(pid, SIGNALS[self.signal_name])
            except ProcessLookupError:
                continue
            self.killed.append(pid)
        return f"sent SIG{self.signal_name} to pid {', '.join(str(p) for p in self.killed) or 'none (already gone)'}"

    def revert(self, world: Any) -> str:
        return "nothing to undo: whatever supervises the process restarts it, or nothing does, and the after-measurement shows which"


class Netem(Fault):
    kind = "netem"

    def __init__(self, spec: Dict[str, Any]):
        self.device = require(spec, "device", self.kind)
        self.netns = str(spec.get("netns") or "")
        self.delay_ms = number(spec, "delay_ms", 0, self.kind)
        self.jitter_ms = number(spec, "jitter_ms", 0, self.kind)
        self.loss_percent = number(spec, "loss_percent", 0, self.kind)
        self.applied = False
        if self.delay_ms <= 0 and self.loss_percent <= 0:
            raise InputError("netem: set delay_ms or loss_percent (or both)")
        if self.delay_ms < 0 or self.jitter_ms < 0 or not 0 <= self.loss_percent <= 100:
            raise InputError("netem: delay_ms and jitter_ms must be 0 or more, loss_percent between 0 and 100")
        if self.jitter_ms and not self.delay_ms:
            raise InputError("netem: jitter_ms needs delay_ms")

    def prefix(self) -> List[str]:
        return ["ip", "netns", "exec", self.netns] if self.netns else []

    def add_command(self) -> List[str]:
        argv = self.prefix() + ["tc", "qdisc", "add", "dev", self.device, "root", "netem"]
        if self.delay_ms:
            argv += ["delay", f"{self.delay_ms:g}ms"] + ([f"{self.jitter_ms:g}ms"] if self.jitter_ms else [])
        if self.loss_percent:
            argv += ["loss", f"{self.loss_percent:g}%"]
        return argv

    def delete_command(self) -> List[str]:
        return self.prefix() + ["tc", "qdisc", "del", "dev", self.device, "root"]

    def show_command(self) -> List[str]:
        return self.prefix() + ["tc", "qdisc", "show", "dev", self.device]

    def commands(self, duration_s: float) -> List[List[str]]:
        return [self.show_command(), self.add_command(), self.delete_command()]

    def describe(self) -> str:
        argv = self.add_command()
        return f"{' '.join(argv[argv.index('netem') + 1:])} on {self.device} in netns {self.netns or '(host)'}"

    def refusals(self, blast: BlastRadius) -> List[str]:
        if not self.netns and not blast.allow_host_network:
            return [f"netem without a netns shapes the host's own {self.device}, which can cut your own session; set blast_radius.allow_host_network only with console access"]
        if self.netns and self.netns not in blast.netns:
            return [f"netns {self.netns} is not in blast_radius.netns ({', '.join(blast.netns) or 'empty'})"]
        return []

    def root_qdisc(self, world: Any) -> Tuple[str, str]:
        argv = self.show_command()
        code, out, err = world.run(argv)
        if code != 0:
            raise FaultError(f"{shlex.join(argv)}: {(err or out).strip()}")
        for line in out.splitlines():
            parts = line.split()
            if len(parts) >= 4 and parts[0] == "qdisc" and parts[3] == "root":
                return parts[1], parts[2]
        return "", ""

    def plan(self, world: Any, blast: BlastRadius) -> List[str]:
        kind, handle = self.root_qdisc(world)
        if kind == "netem" or (handle and handle != "0:"):
            raise Refused(f"{self.device} already has a configured root qdisc ({kind} {handle}); the rollback, tc qdisc del ... root, would remove it too")
        return [f"root qdisc of {self.device} is {kind or 'none'} {handle}, the kernel default".strip()]

    def start(self, world: Any, blast: BlastRadius, duration_s: float) -> str:
        self.plan(world, blast)
        argv = self.add_command()
        code, out, err = world.run(argv)
        if code != 0:
            message = (err or out).strip()
            hint = " (the sch_netem kernel module is missing: modprobe sch_netem)" if "unknown" in message.lower() else ""
            raise FaultError(f"{shlex.join(argv)}: {message}{hint}")
        self.applied = True
        kind, _ = self.root_qdisc(world)
        if kind != "netem":
            raise FaultError(f"after {shlex.join(argv)} the root qdisc of {self.device} is {kind or 'missing'}, not netem")
        return shlex.join(argv)

    def revert(self, world: Any) -> str:
        if not self.applied:
            return "nothing to undo: the qdisc was never added"
        argv = self.delete_command()
        last = ""
        for attempt in range(1, ATTEMPTS + 1):
            code, out, err = world.run(argv)
            last = (err or out).strip()
            try:
                kind, _ = self.root_qdisc(world)
            except FaultError as exc:
                last = str(exc)
                continue
            if kind != "netem":
                self.applied = False
                return f"{shlex.join(argv)} (attempt {attempt}); root qdisc is now {kind or 'the default'}"
        raise FaultError(f"netem is still on {self.device} after {ATTEMPTS} attempts ({last}); remove it by hand: {shlex.join(argv)}")


class CpuBurn(Fault):
    kind = "cpu-burn"

    def __init__(self, spec: Dict[str, Any]):
        self.workers = int(number(spec, "workers", 1, self.kind))
        self.processes: List[Any] = []
        if self.workers < 1:
            raise InputError("cpu-burn: workers must be at least 1")

    def describe(self) -> str:
        return f"burn CPU with {self.workers} busy worker process(es)"

    def refusals(self, blast: BlastRadius) -> List[str]:
        if self.workers > blast.max_workers:
            return [f"cpu-burn asks for {self.workers} workers; blast_radius.max_workers is {blast.max_workers}"]
        return []

    def command(self, seconds: float) -> List[str]:
        return [sys.executable, os.path.abspath(__file__), "burn", "--seconds", f"{seconds:g}"]

    def commands(self, duration_s: float) -> List[List[str]]:
        return [self.command(duration_s + BURN_GRACE_S)]

    def start(self, world: Any, blast: BlastRadius, duration_s: float) -> str:
        argv = self.command(duration_s + BURN_GRACE_S)
        for _ in range(self.workers):
            self.processes.append(world.spawn(argv))
        pids = ", ".join(str(p.pid) for p in self.processes)
        return f"started {self.workers} burner(s), pid {pids}; each exits by itself after {duration_s + BURN_GRACE_S:g} s"

    def revert(self, world: Any) -> str:
        for process in self.processes:
            if process.poll() is None:
                process.terminate()
        for process in self.processes:
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        alive = [str(p.pid) for p in self.processes if p.poll() is None]
        if alive:
            raise FaultError(f"burners still running: {', '.join(alive)}; stop them by hand: kill -9 {' '.join(alive)}")
        return f"stopped {len(self.processes)} burner(s)"


class Manifest(Fault):
    kind = "manifest"

    def __init__(self, spec: Dict[str, Any], base_dir: str):
        path = require(spec, "path", self.kind)
        self.path = path if os.path.isabs(path) else os.path.join(base_dir, path)
        try:
            with open(self.path, encoding="utf-8") as handle:
                self.doc = json.load(handle)
        except OSError as exc:
            raise InputError(f"manifest: cannot read {self.path}: {exc.strerror}")
        except json.JSONDecodeError as exc:
            raise InputError(f"manifest: {self.path} is not JSON ({exc.msg}); kubectl accepts JSON manifests")
        self.wait = spec.get("wait") or {}
        self.applied = False
        meta = self.doc.get("metadata") or {}
        self.name = f"{self.doc.get('kind', '?')} {meta.get('namespace', 'default')}/{meta.get('name', '?')}"

    @property
    def spec(self) -> Dict[str, Any]:
        return self.doc.get("spec") or {}

    @property
    def namespaces(self) -> List[str]:
        own = (self.doc.get("metadata") or {}).get("namespace") or "default"
        return sorted({own} | set((self.spec.get("selector") or {}).get("namespaces") or [own]))

    def describe(self) -> str:
        return f"apply {self.name} from {os.path.basename(self.path)}"

    def refusals(self, blast: BlastRadius) -> List[str]:
        reasons: List[str] = []
        for namespace in self.namespaces:
            reasons.extend(namespace_refusals(namespace, blast))
        if self.spec.get("mode"):
            reasons.extend(percent_refusals(str(self.spec["mode"]), float(self.spec.get("value") or 1), blast))
        return reasons

    def plan(self, world: Any, blast: BlastRadius) -> List[str]:
        labels = (self.spec.get("selector") or {}).get("labelSelectors")
        mode = self.spec.get("mode")
        if not labels or not mode:
            return [f"{self.name}: no labelSelectors and mode to check against the blast radius"]
        selector = ",".join(f"{k}={v}" for k, v in sorted(labels.items()))
        pods = [f"{ns}/{name}" for ns in self.namespaces for name in running_pods(world, ns, selector)]
        if not pods:
            raise Refused(f"no running pods match {selector} in {', '.join(self.namespaces)}: a fault that hits nothing proves nothing")
        wanted = selection_size(str(mode), float(self.spec.get("value") or 1), len(pods), worst_case=True)
        allowed = allowed_count(len(pods), blast)
        if wanted > allowed:
            raise Refused(f"{self.name} mode {mode} would affect {wanted} of {len(pods)} pods; the blast radius allows {allowed}")
        return [f"{self.name} mode {mode} affects at most {wanted} of {len(pods)} pods matching {selector}"]

    def commands(self, duration_s: float) -> List[List[str]]:
        return [["kubectl", "apply", "-f", self.path], ["kubectl", "delete", "-f", self.path, "--ignore-not-found", "--timeout=60s"]]

    def start(self, world: Any, blast: BlastRadius, duration_s: float) -> str:
        self.plan(world, blast)
        self.applied = True
        argv = ["kubectl", "apply", "-f", self.path]
        code, out, err = world.run(argv)
        if code != 0:
            raise FaultError(f"{shlex.join(argv)}: {(err or out).strip()}")
        if not self.wait:
            return out.strip()
        expression, wanted = str(self.wait.get("jsonpath", "")), str(self.wait.get("equals", ""))
        deadline = world.now() + float(self.wait.get("timeout_s", 30))
        seen = ""
        while True:
            code, out, err = world.run(["kubectl", "get", "-f", self.path, "-o", f"jsonpath={expression}"])
            seen = out.strip() if code == 0 else (err or out).strip()
            if code == 0 and seen == wanted:
                return f"applied {self.name}; {expression} is {wanted}"
            if world.now() >= deadline:
                raise FaultError(f"{self.name}: {expression} is {seen!r}, not {wanted!r}, after {self.wait.get('timeout_s', 30)} s")
            world.sleep(1, interruptible=False)

    def revert(self, world: Any) -> str:
        if not self.applied:
            return "nothing to undo: the manifest was never applied"
        argv = ["kubectl", "delete", "-f", self.path, "--ignore-not-found", "--timeout=60s"]
        last = ""
        for attempt in range(1, ATTEMPTS + 1):
            code, out, err = world.run(argv)
            if code == 0:
                self.applied = False
                return f"deleted {self.name} (attempt {attempt})"
            last = (err or out).strip()
        raise FaultError(f"{self.name} is still there after {ATTEMPTS} attempts ({last}); remove it by hand: {shlex.join(argv)}")


FAULTS = {"pod-delete": PodDelete, "process-kill": ProcessKill, "netem": Netem, "cpu-burn": CpuBurn}


def make_fault(spec: Any, base_dir: str) -> Fault:
    if not isinstance(spec, dict):
        raise InputError("method.faults: every fault is an object with a type")
    kind = spec.get("type")
    if kind == "manifest":
        return Manifest(spec, base_dir)
    if kind not in FAULTS:
        raise InputError(f"unknown fault type {kind!r} (known: {', '.join(sorted(list(FAULTS) + ['manifest']))})")
    return FAULTS[kind](spec)


@dataclass
class Experiment:
    title: str
    hypothesis: Hypothesis
    faults: List[Fault]
    duration_s: float
    abort_rules: List[AbortRule]
    probe_errors: int
    blast: BlastRadius


def make_sli(spec: Any) -> SLI:
    if not isinstance(spec, dict) or spec.get("type") not in SLI_TYPES:
        raise InputError(f"hypothesis.slis: every SLI needs a type, one of {', '.join(SLI_TYPES)}")
    name = str(spec.get("name") or spec["type"])
    if "max" not in spec:
        raise InputError(f"SLI {name}: max is required, the tolerance of the hypothesis")
    maximum = number(spec, "max", 0, f"SLI {name}")

    def selector(key: str) -> Selector:
        if not spec.get(key):
            raise InputError(f"SLI {name}: {key} is required (a series selector)")
        return parse_selector(str(spec[key]))

    if spec["type"] == "error_ratio":
        return SLI(name, "error_ratio", maximum, errors=selector("errors"), total=selector("total"))
    quantile = number(spec, "quantile", 0.95, f"SLI {name}")
    if not 0 < quantile < 1:
        raise InputError(f"SLI {name}: quantile must be between 0 and 1, got {quantile:g}")
    return SLI(name, "latency_quantile", maximum, histogram=selector("histogram"), quantile=quantile)


def make_check(spec: Any) -> Check:
    if not isinstance(spec, dict) or spec.get("type") != "deployment-ready":
        raise InputError("hypothesis.checks: the only check type is deployment-ready")
    return Check("deployment-ready", require(spec, "namespace", "check"), require(spec, "name", "check"))


def make_blast(spec: Any) -> BlastRadius:
    if not isinstance(spec, dict):
        raise InputError("blast_radius must be an object")
    return BlastRadius(
        namespaces=[str(n) for n in spec.get("namespaces") or []],
        netns=[str(n) for n in spec.get("netns") or []],
        max_percent=number(spec, "max_percent", 50, "blast_radius"),
        max_pods=int(spec["max_pods"]) if spec.get("max_pods") is not None else None,
        max_processes=int(number(spec, "max_processes", 1, "blast_radius")),
        max_workers=int(number(spec, "max_workers", 1, "blast_radius")),
        max_duration_s=number(spec, "max_duration_s", 300, "blast_radius"),
        allow_host_network=bool(spec.get("allow_host_network", False)),
        allow_system_namespaces=bool(spec.get("allow_system_namespaces", False)),
    )


def load_experiment(doc: Any, base_dir: str = ".") -> Experiment:
    if not isinstance(doc, dict):
        raise InputError("an experiment is a JSON object with title, hypothesis, method, abort and blast_radius")
    for key in ("title", "hypothesis", "method", "abort", "blast_radius"):
        if key not in doc:
            hint = {"abort": ": every experiment needs a condition that stops it early", "blast_radius": ": list the namespaces and netns it may touch"}.get(key, "")
            raise InputError(f"experiment: {key} is missing{hint}")
    spec = doc["hypothesis"] or {}
    slis = [make_sli(s) for s in spec.get("slis") or []]
    if not slis:
        raise InputError("hypothesis.slis is empty: a steady state is measured, not assumed")
    names = [s.name for s in slis]
    if len(set(names)) != len(names):
        raise InputError(f"hypothesis.slis: duplicate names in {names}")
    source = spec.get("source") or {}
    if not isinstance(source, dict) or not any(k in source for k in ("url", "kubectl_raw", "file")):
        raise InputError("hypothesis.source needs url, kubectl_raw or file: where the Prometheus-format metrics are")
    interval = number(spec, "interval_s", 5, "hypothesis")
    if interval <= 0:
        raise InputError("hypothesis.interval_s must be above 0")
    hypothesis = Hypothesis(source, interval, number(spec, "min_requests", 1, "hypothesis"), slis, [make_check(c) for c in spec.get("checks") or []], number(spec, "recovery_s", 60, "hypothesis"))
    method = doc["method"] or {}
    faults = [make_fault(f, base_dir) for f in method.get("faults") or []]
    if not faults:
        raise InputError("method.faults is empty; use minichaos measure to check the steady state on its own")
    duration = number(method, "duration_s", 30, "method")
    if duration <= 0:
        raise InputError("method.duration_s must be above 0")
    abort = doc["abort"] or {}
    rules: List[AbortRule] = []
    for rule in abort.get("rules") or []:
        if not isinstance(rule, dict) or rule.get("sli") not in names or "above" not in rule:
            raise InputError(f"abort.rules: every rule needs sli (one of {', '.join(names)}) and above")
        rules.append(AbortRule(rule["sli"], number(rule, "above", 0, "abort rule"), max(1, int(number(rule, "for", 1, "abort rule")))))
    return Experiment(str(doc["title"]), hypothesis, faults, duration, rules, max(1, int(number(abort, "probe_errors", 3, "abort"))), make_blast(doc["blast_radius"]))


def static_refusals(experiment: Experiment) -> List[str]:
    reasons: List[str] = []
    if experiment.duration_s > experiment.blast.max_duration_s:
        reasons.append(f"method.duration_s {experiment.duration_s:g} is above blast_radius.max_duration_s {experiment.blast.max_duration_s:g}")
    if not experiment.abort_rules:
        reasons.append("abort.rules is empty: every experiment needs a condition that stops it early")
    for fault in experiment.faults:
        reasons.extend(fault.refusals(experiment.blast))
    return reasons


@dataclass
class Journal:
    title: str
    verdict: str = ""
    reason: str = ""
    exit_code: int = 1
    events: List[Dict[str, Any]] = field(default_factory=list)
    measurements: List[Measurement] = field(default_factory=list)
    faults: List[Dict[str, Any]] = field(default_factory=list)
    rollback_ok: bool = True
    injected_at_s: Optional[float] = None
    aborted_at_s: Optional[float] = None
    reverted_at_s: Optional[float] = None
    recovered_after_s: Optional[float] = None

    def log(self, world: Any, phase: str, message: str, **data: Any) -> None:
        self.events.append(dict({"at": round(world.now(), 3), "phase": phase, "message": message}, **data))

    def to_json(self) -> Dict[str, Any]:
        return {
            "title": self.title,
            "verdict": self.verdict,
            "reason": self.reason,
            "exit_code": self.exit_code,
            "rollback_ok": self.rollback_ok,
            "injected_at_s": self.injected_at_s,
            "aborted_at_s": self.aborted_at_s,
            "reverted_at_s": self.reverted_at_s,
            "recovered_after_s": self.recovered_after_s,
            "faults": self.faults,
            "measurements": [m.to_json() for m in self.measurements],
            "events": self.events,
        }


class Prober:
    def __init__(self, hypothesis: Hypothesis, world: Any, journal: Journal):
        self.hypothesis, self.world, self.journal = hypothesis, world, journal
        self.last: Optional[Tuple[float, List[Sample]]] = None

    def scrape(self) -> Tuple[float, List[Sample]]:
        text = self.world.fetch(self.hypothesis.source)
        try:
            return self.world.now(), parse_exposition(text, "source")
        except InputError as exc:
            raise ProbeError(str(exc))

    def restart(self) -> None:
        try:
            self.last = self.scrape()
        except ProbeError:
            self.last = None

    def window(self, phase: str, interruptible: bool = True) -> Measurement:
        self.world.sleep(self.hypothesis.interval_s, interruptible=interruptible)
        at = round(self.world.now(), 3)
        try:
            current = self.scrape()
        except ProbeError as exc:
            return self.record(Measurement(phase, at, 0.0, error=str(exc)))
        if self.last is None:
            self.last = current
            return self.record(Measurement(phase, at, 0.0, error="no earlier scrape to compare with"))
        start, before = self.last
        measurement = Measurement(phase, at, round(current[0] - start, 3))
        for sli in self.hypothesis.slis:
            value, requests = sli.evaluate(before, current[1])
            if value is None or requests < self.hypothesis.min_requests:
                status = "no-data"
            else:
                status = "breach" if value > sli.maximum else "ok"
            measurement.readings.append(Reading(sli.name, value, sli.maximum, requests, status))
        if phase != "during":
            measurement.checks = [check.run(self.world) for check in self.hypothesis.checks]
        self.last = current
        return self.record(measurement)

    def record(self, measurement: Measurement) -> Measurement:
        self.journal.measurements.append(measurement)
        self.journal.log(self.world, measurement.phase, measurement.text())
        return measurement


def observe(experiment: Experiment, world: Any, prober: Prober, journal: Journal) -> Tuple[str, str]:
    streaks = [0 for _ in experiment.abort_rules]
    errors_in_row = 0
    windows = max(1, int(experiment.duration_s / experiment.hypothesis.interval_s + 1e-9))
    prober.restart()
    for _ in range(windows):
        measurement = prober.window("during")
        stop = world.stopping()
        if stop:
            return "aborted", f"abort button: {stop}"
        if measurement.error:
            errors_in_row += 1
            if errors_in_row >= experiment.probe_errors:
                return "aborted", f"{errors_in_row} probe errors in a row, flying blind: {measurement.error}"
            continue
        errors_in_row = 0
        for index, rule in enumerate(experiment.abort_rules):
            reading = measurement.reading(rule.sli)
            breached = reading is not None and reading.status != "no-data" and reading.value is not None and reading.value > rule.above
            streaks[index] = streaks[index] + 1 if breached else 0
            if streaks[index] >= rule.count:
                return "aborted", f"{rule.sli} {reading.value:.3f} > {rule.above:g} in {rule.count} measurement(s) in a row"
    return "completed", ""


def rollback(started: List[Fault], world: Any, journal: Journal) -> None:
    for fault in reversed(started):
        try:
            message = fault.revert(world)
            journal.log(world, "rollback", f"{fault.kind}: {message}", ok=True)
            result = message
        except Exception as exc:
            journal.rollback_ok = False
            result = f"FAILED: {exc}"
            journal.log(world, "rollback", f"{fault.kind}: {result}", ok=False)
        for entry in journal.faults:
            if entry["fault"] is fault:
                entry["revert"] = result
    journal.reverted_at_s = round(world.now(), 3)


def recover(experiment: Experiment, world: Any, prober: Prober, journal: Journal) -> None:
    prober.restart()
    reverted = world.now()
    deadline = reverted + experiment.hypothesis.recovery_s
    while True:
        measurement = prober.window("after", interruptible=False)
        if measurement.status == "ok":
            journal.recovered_after_s = round(world.now() - reverted, 3)
            return
        if world.now() >= deadline:
            return


def decide(journal: Journal, outcome: str, reason: str) -> Tuple[str, str]:
    during = [m for m in journal.measurements if m.phase == "during"]
    after = [m for m in journal.measurements if m.phase == "after"]
    recovered = f"recovered {journal.recovered_after_s:g} s after the rollback" if journal.recovered_after_s is not None else "did not recover within recovery_s"
    if outcome in ("aborted", "failed"):
        return outcome, f"{reason}; {recovered}"
    breaches = [m for m in during if m.status == "breach"]
    if breaches:
        return "deviated", f"hypothesis broken during the fault, first at +{breaches[0].at:g} s: {breaches[0].text()}; {recovered}"
    if journal.recovered_after_s is None:
        if any(m.status == "breach" for m in after):
            return "deviated", f"the system did not return to steady state within recovery_s: {after[-1].text()}"
        return "inconclusive", "no measurement after the rollback had enough data"
    blind = [m for m in during if m.status in ("no-data", "error")]
    if blind:
        return "inconclusive", f"{len(blind)} of {len(during)} measurements during the fault had no data; nothing broke, but nothing was proven either"
    return "held", f"{len(during)} measurements during the fault within tolerance; {recovered}"


def finish(journal: Journal, world: Any, verdict: str, reason: str) -> Journal:
    journal.verdict, journal.reason = verdict, reason
    journal.exit_code = 0 if verdict == "held" and journal.rollback_ok else 2 if verdict == "refused" else 1
    if not journal.rollback_ok:
        journal.reason += "; ROLLBACK FAILED, clean up by hand with the command in the rollback line"
    journal.log(world, "verdict", f"{verdict}: {journal.reason}")
    for entry in journal.faults:
        entry.pop("fault", None)
    return journal


def run_experiment(experiment: Experiment, world: Any) -> Journal:
    journal = Journal(experiment.title)
    refusals = static_refusals(experiment)
    if refusals:
        return finish(journal, world, "refused", "; ".join(refusals))
    for fault in experiment.faults:
        entry: Dict[str, Any] = {"fault": fault, "type": fault.kind, "description": fault.describe(), "targets": [], "result": "", "revert": ""}
        journal.faults.append(entry)
        try:
            entry["targets"] = fault.plan(world, experiment.blast)
        except Refused as exc:
            return finish(journal, world, "refused", str(exc))
        except FaultError as exc:
            return finish(journal, world, "failed", f"planning {fault.kind}: {exc}")
        journal.log(world, "plan", f"{fault.describe()}: {'; '.join(entry['targets']) or 'ok'}")
    prober = Prober(experiment.hypothesis, world, journal)
    prober.restart()
    baseline = prober.window("before", interruptible=False)
    if baseline.status != "ok":
        return finish(journal, world, "not-run", f"steady state not met before any fault, nothing injected: {baseline.text()}")
    started: List[Fault] = []
    outcome, reason = "completed", ""
    journal.injected_at_s = round(world.now(), 3)
    try:
        for fault, entry in zip(experiment.faults, journal.faults):
            started.append(fault)
            entry["result"] = fault.start(world, experiment.blast, experiment.duration_s)
            journal.log(world, "inject", f"{fault.kind}: {entry['result']}")
        outcome, reason = observe(experiment, world, prober, journal)
    except Refused as exc:
        outcome, reason = "failed", f"refused at injection time: {exc}"
    except FaultError as exc:
        outcome, reason = "failed", f"fault failed: {exc}"
    finally:
        if outcome == "aborted":
            journal.aborted_at_s = round(world.now(), 3)
            journal.log(world, "abort", reason)
        rollback(started, world, journal)
    recover(experiment, world, prober, journal)
    verdict, why = decide(journal, outcome, reason)
    return finish(journal, world, verdict, why)


def read_processes(root: str = "/proc") -> List[Tuple[int, str]]:
    if not os.path.isdir(root):
        raise FaultError(f"{root} does not exist: process-kill reads /proc, which only Linux has")
    found: List[Tuple[int, str]] = []
    for entry in os.listdir(root):
        if not entry.isdigit():
            continue
        try:
            with open(os.path.join(root, entry, "cmdline"), "rb") as handle:
                raw = handle.read()
        except OSError:
            continue
        command = raw.rstrip(b"\0").replace(b"\0", b" ").decode("utf-8", "replace")
        if command:
            found.append((int(entry), command))
    return sorted(found)


class RealWorld:
    def __init__(self, context: str = "", seed: Optional[int] = None, abort_file: str = "", proc_root: str = "/proc"):
        self.context, self.abort_file, self.proc_root = context, abort_file, proc_root
        self.rng = random.Random(seed)
        self.stop_reason = ""
        self.started = time.monotonic()
        self.protected = {1, os.getpid(), os.getppid()}

    def now(self) -> float:
        return time.monotonic() - self.started

    def stopping(self) -> str:
        if not self.stop_reason and self.abort_file and os.path.exists(self.abort_file):
            self.stop_reason = f"{self.abort_file} exists"
        return self.stop_reason

    def sleep(self, seconds: float, interruptible: bool = True) -> None:
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if interruptible and self.stopping():
                return
            time.sleep(max(0.0, min(0.2, deadline - time.monotonic())))

    def run(self, argv: List[str]) -> Tuple[int, str, str]:
        if argv and argv[0] == "kubectl" and self.context:
            argv = ["kubectl", "--context", self.context] + argv[1:]
        try:
            done = subprocess.run(argv, capture_output=True, text=True, timeout=90)
        except FileNotFoundError:
            return 127, "", f"{argv[0]}: command not found"
        except subprocess.TimeoutExpired:
            return 124, "", f"{shlex.join(argv)} timed out after 90 s"
        return done.returncode, done.stdout, done.stderr

    def fetch(self, source: Dict[str, Any]) -> str:
        if "url" in source:
            try:
                with OPENER.open(str(source["url"]), timeout=5) as response:
                    return response.read().decode("utf-8", "replace")
            except (urllib.error.URLError, OSError, http.client.HTTPException) as exc:
                raise ProbeError(f"GET {source['url']}: {getattr(exc, 'reason', exc)}")
        if "kubectl_raw" in source:
            code, out, err = self.run(["kubectl", "get", "--raw", str(source["kubectl_raw"])])
            if code != 0:
                raise ProbeError(f"kubectl get --raw {source['kubectl_raw']}: {(err or out).strip()}")
            return out
        try:
            with open(str(source["file"]), encoding="utf-8") as handle:
                return handle.read()
        except OSError as exc:
            raise ProbeError(f"cannot read {source['file']}: {exc.strerror}")

    def processes(self) -> List[Tuple[int, str]]:
        return read_processes(self.proc_root)

    def kill(self, pid: int, sig: int) -> None:
        os.kill(pid, sig)

    def spawn(self, argv: List[str]) -> Any:
        return subprocess.Popen(argv, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


class LoadStats:
    def __init__(self, buckets: Sequence[float] = LOAD_BUCKETS):
        self.lock = threading.Lock()
        self.buckets = tuple(buckets)
        self.requests = {outcome: 0.0 for outcome in OUTCOMES}
        self.histograms = {outcome: [0.0] * (len(self.buckets) + 1) for outcome in OUTCOMES}
        self.sums = {outcome: 0.0 for outcome in OUTCOMES}
        self.reasons: Dict[str, int] = {}

    def observe(self, outcome: str, seconds: float, times: float = 1.0, reason: str = "") -> None:
        if times <= 0:
            return
        with self.lock:
            self.requests[outcome] += times
            self.histograms[outcome][bisect.bisect_left(self.buckets, seconds)] += times
            self.sums[outcome] += seconds * times
            if reason:
                self.reasons[reason] = self.reasons.get(reason, 0) + 1

    def exposition(self) -> str:
        with self.lock:
            lines = [f"# HELP {LOAD_REQUESTS} Requests sent by minichaos load, by outcome.", f"# TYPE {LOAD_REQUESTS} counter"]
            lines += [f'{LOAD_REQUESTS}{{outcome="{o}"}} {show(self.requests[o])}' for o in OUTCOMES]
            lines += [f"# HELP {LOAD_LATENCY} Request latency seen by minichaos load.", f"# TYPE {LOAD_LATENCY} histogram"]
            for outcome in OUTCOMES:
                cumulative = 0.0
                for bound, count in zip(self.buckets + (math.inf,), self.histograms[outcome]):
                    cumulative += count
                    le = "+Inf" if bound == math.inf else f"{bound:g}"
                    lines.append(f'{LOAD_LATENCY}_bucket{{outcome="{outcome}",le="{le}"}} {show(cumulative)}')
                lines.append(f'{LOAD_LATENCY}_sum{{outcome="{outcome}"}} {show(self.sums[outcome])}')
                lines.append(f'{LOAD_LATENCY}_count{{outcome="{outcome}"}} {show(cumulative)}')
        return "\n".join(lines) + "\n"

    def summary(self) -> Dict[str, Any]:
        with self.lock:
            ok, errors = self.requests["ok"], self.requests["error"]
            cumulative, buckets = 0.0, []
            for bound, count in zip(self.buckets + (math.inf,), self.histograms["ok"]):
                cumulative += count
                buckets.append((bound, cumulative))
            reasons = dict(self.reasons)
        total = ok + errors
        return {
            "ok": int(ok),
            "errors": int(errors),
            "error_rate": round(errors / total, 4) if total else None,
            "p50_s": bucket_quantile(0.5, buckets),
            "p95_s": bucket_quantile(0.95, buckets),
            "reasons": reasons,
        }


def show(value: float) -> str:
    return str(int(value)) if float(value).is_integer() else repr(float(value))


class Pacer:
    def __init__(self, rate: float):
        self.interval = 1.0 / rate if rate > 0 else 0.0
        self.next = time.monotonic()
        self.lock = threading.Lock()

    def slot(self) -> float:
        with self.lock:
            now = time.monotonic()
            if not self.interval:
                return now
            slot = max(self.next, now)
            self.next = slot + self.interval
            return slot


def load_worker(url: str, timeout: float, pacer: Pacer, stats: LoadStats, stop: threading.Event) -> None:
    while not stop.is_set():
        wait = pacer.slot() - time.monotonic()
        if wait > 0 and stop.wait(wait):
            return
        started = time.monotonic()
        outcome, reason = "ok", ""
        try:
            with OPENER.open(url, timeout=timeout) as response:
                response.read()
        except urllib.error.HTTPError as exc:
            outcome, reason = "error", f"http {exc.code}"
        except (urllib.error.URLError, OSError, http.client.HTTPException, ValueError) as exc:
            outcome, reason = "error", type(getattr(exc, "reason", exc)).__name__
        stats.observe(outcome, time.monotonic() - started, reason=reason)


def metrics_handler(stats: LoadStats) -> Any:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            path = self.path.split("?", 1)[0]
            if path == "/metrics":
                code, body = 200, stats.exposition().encode()
            elif path == "/healthz":
                code, body = 200, b"ok\n"
            else:
                code, body = 404, b"try /metrics\n"
            self.send_response(code)
            self.send_header("Content-Type", "text/plain; version=0.0.4; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args: Any) -> None:
            return

    return Handler


def run_load(args: argparse.Namespace) -> int:
    stats = LoadStats()
    stop = threading.Event()
    try:
        server = ThreadingHTTPServer((args.metrics_host, args.metrics_port), metrics_handler(stats))
    except OSError as exc:
        print(f"minichaos load: cannot listen on {args.metrics_host}:{args.metrics_port}: {exc.strerror}", file=sys.stderr)
        return 2
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    print(f"minichaos load: {args.url} with {args.concurrency} worker(s), metrics on http://{args.metrics_host}:{server.server_address[1]}/metrics", file=sys.stderr, flush=True)
    previous = {signum: signal.signal(signum, lambda *_: stop.set()) for signum in (signal.SIGTERM, signal.SIGINT)}
    pacer = Pacer(args.rate)
    workers = [threading.Thread(target=load_worker, args=(args.url, args.timeout, pacer, stats, stop), daemon=True) for _ in range(args.concurrency)]
    for worker in workers:
        worker.start()
    deadline = time.monotonic() + args.duration if args.duration > 0 else math.inf
    while not stop.wait(min(1.0, max(0.0, deadline - time.monotonic()))):
        if time.monotonic() >= deadline:
            stop.set()
    for worker in workers:
        worker.join(timeout=args.timeout + 1)
    server.shutdown()
    server.server_close()
    for signum, handler in previous.items():
        signal.signal(signum, handler)
    summary = stats.summary()
    if args.json:
        print(json.dumps(summary, indent=2))
    else:
        p95 = "-" if summary["p95_s"] is None else f"{summary['p95_s']:.3f} s"
        print(f"{summary['ok']} ok, {summary['errors']} errors, p95 {p95}, reasons {summary['reasons']}")
    return 0 if summary["ok"] > 0 else 1


def burn(seconds: float) -> int:
    deadline = time.monotonic() + seconds
    spins = 0
    while time.monotonic() < deadline:
        for _ in range(20000):
            spins += 1
    return 0


class SimulatedProcess:
    def __init__(self, argv: List[str], pid: int):
        self.argv, self.pid = argv, pid
        self.returncode: Optional[int] = None

    def poll(self) -> Optional[int]:
        return self.returncode

    def terminate(self) -> None:
        self.returncode = -signal.SIGTERM

    def kill(self) -> None:
        self.returncode = -signal.SIGKILL

    def wait(self, timeout: Optional[float] = None) -> Optional[int]:
        return self.returncode


class SimulatedWorld:
    def __init__(self, replicas: int = 3, rps: float = 50.0, latency_s: float = 0.02, ready_after_s: float = 4.0, endpoint_lag_s: float = 1.0, restart_after_s: float = 3.0, cores: int = 4, seed: int = 1):
        self.rng = random.Random(seed)
        self.clock = 0.0
        self.stop_reason = ""
        self.interrupt_at: Optional[Tuple[float, str]] = None
        self.protected = {1}
        self.rps, self.latency_s, self.cores = rps, latency_s, cores
        self.ready_after_s, self.endpoint_lag_s, self.restart_after_s = ready_after_s, endpoint_lag_s, restart_after_s
        self.desired = replicas
        self.serial = 0
        self.pods: Dict[str, float] = {}
        for _ in range(replicas):
            self.add_pod(0.0)
        self.dropping: List[Tuple[float, float]] = []
        self.qdisc: Dict[Tuple[str, str], Tuple[float, float]] = {}
        self.table: Dict[int, str] = {4242: "python3 -m http.server 8080", 4343: "cc-chaos-decoy 300"}
        self.supervised = {"python3 -m http.server 8080"}
        self.down_until = 0.0
        self.restarting: List[Tuple[float, str]] = []
        self.burners: List[SimulatedProcess] = []
        self.applied: Dict[str, Dict[str, Any]] = {}
        self.stats = LoadStats()
        self.commands: List[List[str]] = []
        self.failing: Dict[str, int] = {}
        self.unreachable: List[Tuple[float, float]] = []
        self.next_pid = 5000

    def add_pod(self, ready_at: float) -> None:
        self.serial += 1
        self.pods[f"web-{self.serial}"] = ready_at

    def now(self) -> float:
        return self.clock

    def stopping(self) -> str:
        if not self.stop_reason and self.interrupt_at and self.clock >= self.interrupt_at[0]:
            self.stop_reason = self.interrupt_at[1]
        return self.stop_reason

    def sleep(self, seconds: float, interruptible: bool = True) -> None:
        end = self.clock + seconds
        while self.clock < end - 1e-9:
            if interruptible and self.stopping():
                return
            step = min(0.25, end - self.clock)
            self.advance(step)
            self.clock = round(self.clock + step, 6)

    def advance(self, dt: float) -> None:
        for at, command in list(self.restarting):
            if at <= self.clock:
                self.restarting.remove((at, command))
                self.table[self.next_pid] = command
                self.next_pid += 1
        ready = [name for name, at in self.pods.items() if at <= self.clock]
        requests = self.rps * dt
        if self.clock < self.down_until or not ready:
            self.stats.observe("error", 0.001, requests)
            return
        dropped = min(1.0, sum(share for until, share in self.dropping if self.clock < until))
        delay = sum(d for d, _ in self.qdisc.values())
        loss = min(1.0, sum(l for _, l in self.qdisc.values()))
        busy = sum(1 for p in self.burners if p.returncode is None)
        latency = self.latency_s * self.desired / len(ready) * (1 + busy / self.cores) + 2 * delay
        slow = min(1.0, 3 * loss)
        ok = requests * (1 - dropped)
        self.stats.observe("error", 0.001, requests * dropped)
        self.stats.observe("ok", latency, ok * (1 - slow))
        self.stats.observe("ok", latency + 1.0, ok * slow)

    def run(self, argv: List[str]) -> Tuple[int, str, str]:
        self.commands.append(list(argv))
        line = " ".join(argv)
        for fragment, remaining in list(self.failing.items()):
            if fragment in line and remaining > 0:
                self.failing[fragment] = remaining - 1
                return 1, "", f"simulated failure of {fragment}"
        if argv[:3] == ["ip", "netns", "exec"]:
            return self.tc(argv[3], argv[4:])
        if argv[:1] == ["tc"]:
            return self.tc("", argv)
        if argv[:1] == ["kubectl"]:
            return self.kubectl(argv[1:])
        return 127, "", f"{argv[0]}: not simulated"

    def tc(self, netns: str, argv: List[str]) -> Tuple[int, str, str]:
        if argv[:2] != ["tc", "qdisc"] or len(argv) < 5:
            return 1, "", "simulated tc: only tc qdisc add|del|show dev DEV"
        action, key = argv[2], (netns, argv[4])
        if action == "show":
            if key in self.qdisc:
                delay, loss = self.qdisc[key]
                return 0, f"qdisc netem 8001: root refcnt 2 limit 1000 delay {delay * 1000:g}ms loss {loss * 100:g}%\n", ""
            return 0, "qdisc noqueue 0: root refcnt 2\n", ""
        if action == "add":
            if key in self.qdisc:
                return 2, "", "Error: Exclusivity flag on, cannot modify."
            words = argv[6:]
            delay = float(words[words.index("delay") + 1].rstrip("ms")) / 1000 if "delay" in words else 0.0
            loss = float(words[words.index("loss") + 1].rstrip("%")) / 100 if "loss" in words else 0.0
            self.qdisc[key] = (delay, loss)
            return 0, "", ""
        if action == "del":
            if key not in self.qdisc:
                return 2, "", "Error: Cannot delete qdisc with handle of zero."
            del self.qdisc[key]
            return 0, "", ""
        return 1, "", f"simulated tc: unknown action {action}"

    def delete_pods(self, names: Sequence[str], grace: Optional[int]) -> None:
        ready = sum(1 for at in self.pods.values() if at <= self.clock) or 1
        for name in names:
            if self.pods.pop(name, None) is None:
                continue
            if grace == 0:
                self.dropping.append((self.clock + self.endpoint_lag_s, 1.0 / ready))
            self.add_pod(self.clock + self.ready_after_s)

    def kubectl(self, args: List[str]) -> Tuple[int, str, str]:
        if args[:2] == ["get", "pods"]:
            items = [{"metadata": {"name": name}, "status": {"phase": "Running"}} for name in sorted(self.pods)]
            return 0, json.dumps({"items": items}), ""
        if args[:2] == ["delete", "pod"]:
            names = [a for a in args[4:] if not a.startswith("--")]
            grace = next((int(a.split("=", 1)[1]) for a in args if a.startswith("--grace-period=")), None)
            self.delete_pods(names, grace)
            return 0, "".join(f'pod "{n}" deleted\n' for n in names), ""
        if args[:2] == ["get", "deployment"]:
            ready = sum(1 for at in self.pods.values() if at <= self.clock)
            status = {"replicas": len(self.pods), "readyReplicas": ready, "availableReplicas": ready, "updatedReplicas": len(self.pods)}
            return 0, json.dumps({"spec": {"replicas": self.desired}, "status": status}), ""
        if args[:2] == ["get", "--raw"]:
            return 0, self.stats.exposition(), ""
        if args[:2] == ["apply", "-f"]:
            return self.apply(args[2])
        if args[:2] == ["get", "-f"]:
            return (0, "True", "") if args[2] in self.applied else (1, "", "Error from server (NotFound)")
        if args[:2] == ["delete", "-f"]:
            self.applied.pop(args[2], None)
            return 0, "deleted\n", ""
        return 1, "", f"kubectl {' '.join(args)}: not simulated"

    def apply(self, path: str) -> Tuple[int, str, str]:
        with open(path, encoding="utf-8") as handle:
            doc = json.load(handle)
        spec = doc.get("spec") or {}
        self.applied[path] = doc
        if doc.get("kind") == "PodChaos" and spec.get("action") == "pod-kill":
            pods = sorted(self.pods)
            size = selection_size(str(spec.get("mode", "one")), float(spec.get("value") or 1), len(pods), rng=self.rng)
            self.delete_pods(self.rng.sample(pods, size), int(spec.get("gracePeriod", 0)))
        return 0, f"{doc.get('kind', 'object').lower()}/{(doc.get('metadata') or {}).get('name', '?')} created", ""

    def fetch(self, source: Dict[str, Any]) -> str:
        if any(start <= self.clock < end for start, end in self.unreachable):
            raise ProbeError("simulated: connection refused")
        return self.stats.exposition()

    def processes(self) -> List[Tuple[int, str]]:
        return sorted(self.table.items())

    def kill(self, pid: int, sig: int) -> None:
        command = self.table.pop(pid, None)
        if command is None:
            raise ProcessLookupError(pid)
        if command in self.supervised:
            self.down_until = self.clock + self.restart_after_s
            self.restarting.append((self.down_until, command))

    def spawn(self, argv: List[str]) -> SimulatedProcess:
        process = SimulatedProcess(argv, self.next_pid)
        self.next_pid += 1
        self.burners.append(process)
        return process


def render_journal(journal: Journal) -> str:
    lines = [f"experiment: {journal.title}"]
    for event in journal.events:
        stamp = f"+{event['at']:.1f}s"
        lines.append(f"  {stamp:>8}  {event['phase']:<8}  {event['message']}")
    return "\n".join(lines)


def read_experiment(path: str, source: str = "") -> Experiment:
    try:
        with open(path, encoding="utf-8") as handle:
            doc = json.load(handle)
    except OSError as exc:
        raise InputError(f"cannot read {path}: {exc.strerror}")
    except json.JSONDecodeError as exc:
        raise InputError(f"{path} is not JSON ({exc.msg}, line {exc.lineno})")
    experiment = load_experiment(doc, os.path.dirname(os.path.abspath(path)))
    if source:
        experiment.hypothesis.source = {"url": source}
    return experiment


def install_abort_signals(world: RealWorld) -> None:
    def handler(signum: int, frame: Any) -> None:
        world.stop_reason = f"signal {signal.Signals(signum).name}"

    for signum in (signal.SIGINT, signal.SIGTERM):
        signal.signal(signum, handler)


def emit(journal: Journal, as_json: bool, journal_path: str = "") -> int:
    if journal_path:
        with open(journal_path, "w", encoding="utf-8") as handle:
            json.dump(journal.to_json(), handle, indent=2)
    print(json.dumps(journal.to_json(), indent=2) if as_json else render_journal(journal))
    return journal.exit_code


def validate(args: argparse.Namespace) -> int:
    experiment = read_experiment(args.experiment, args.source)
    refusals = static_refusals(experiment)
    report: Dict[str, Any] = {"title": experiment.title, "faults": [], "refusals": refusals}
    world = RealWorld(args.context) if args.resolve else None
    for fault in experiment.faults:
        entry: Dict[str, Any] = {"type": fault.kind, "description": fault.describe()}
        if args.commands:
            entry["commands"] = [shlex.join(c) for c in fault.commands(experiment.duration_s)]
        if world is not None and not refusals:
            try:
                entry["targets"] = fault.plan(world, experiment.blast)
            except (Refused, FaultError) as exc:
                report["refusals"].append(str(exc))
        report["faults"].append(entry)
    report["ok"] = not report["refusals"]
    if args.json:
        print(json.dumps(report, indent=2))
    else:
        print(f"experiment: {experiment.title}")
        for entry in report["faults"]:
            print(f"  {entry['type']}: {entry['description']}")
            for target in entry.get("targets", []):
                print(f"    target: {target}")
            for command in entry.get("commands", []):
                print(f"    $ {command}")
        for reason in report["refusals"]:
            print(f"  refused: {reason}")
        print("ok" if report["ok"] else "refused")
    return 0 if report["ok"] else 2


def measure(args: argparse.Namespace) -> int:
    experiment = read_experiment(args.experiment, args.source)
    world = RealWorld(args.context)
    journal = Journal(experiment.title)
    prober = Prober(experiment.hypothesis, world, journal)
    prober.restart()
    measurement = prober.window("measure", interruptible=False)
    if args.json:
        print(json.dumps(measurement.to_json(), indent=2))
    else:
        print(f"steady state of: {experiment.title}")
        print(f"  {measurement.text()}")
    return 0 if measurement.status == "ok" else 1


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="minichaos", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    commands = {
        "run": "run the experiment against the real system: kubectl, ip netns, tc, /proc",
        "simulate": "run the experiment against a simulated three-replica service, instantly and deterministically",
        "validate": "check the experiment and its blast radius without injecting anything",
        "measure": "measure the steady-state hypothesis once, without faults",
    }
    parsers: Dict[str, argparse.ArgumentParser] = {}
    for name, text in commands.items():
        command = sub.add_parser(name, help=text, description=text)
        command.add_argument("experiment", help="experiment JSON file")
        command.add_argument("--source", default="", help="override hypothesis.source with this metrics URL")
        command.add_argument("--json", action="store_true", help="machine-readable output")
        if name != "simulate":
            command.add_argument("--context", default="", help="kubectl context for pod faults, checks and kubectl_raw sources")
        parsers[name] = command
    parsers["run"].add_argument("--journal", default="", help="also write the journal as JSON to this file")
    parsers["run"].add_argument("--abort-file", default="", help="abort as soon as this file exists (an abort button for a second terminal)")
    parsers["run"].add_argument("--seed", type=int, default=None, help="seed for target selection")
    parsers["simulate"].add_argument("--replicas", type=int, default=3)
    parsers["simulate"].add_argument("--rps", type=float, default=50.0)
    parsers["simulate"].add_argument("--seed", type=int, default=1)
    parsers["validate"].add_argument("--resolve", action="store_true", help="resolve targets against the cluster or host and check the blast radius")
    parsers["validate"].add_argument("--commands", action="store_true", help="print the commands each fault runs")
    load = sub.add_parser("load", help="load generator that exports its own SLIs on /metrics")
    load.add_argument("--url", required=True)
    load.add_argument("--concurrency", type=int, default=4)
    load.add_argument("--rate", type=float, default=0.0, help="requests per second across all workers (0: as fast as they can)")
    load.add_argument("--duration", type=float, default=0.0, help="seconds (0: until SIGTERM)")
    load.add_argument("--timeout", type=float, default=2.0)
    load.add_argument("--metrics-host", default="127.0.0.1")
    load.add_argument("--metrics-port", type=int, default=9100)
    load.add_argument("--json", action="store_true")
    burner = sub.add_parser("burn", help="spin one CPU for N seconds (what cpu-burn starts)")
    burner.add_argument("--seconds", type=float, required=True)
    args = parser.parse_args(list(sys.argv[1:] if argv is None else argv))
    try:
        if args.command == "load":
            return run_load(args)
        if args.command == "burn":
            return burn(args.seconds)
        if args.command == "validate":
            return validate(args)
        if args.command == "measure":
            return measure(args)
        experiment = read_experiment(args.experiment, args.source)
        if args.command == "simulate":
            return emit(run_experiment(experiment, SimulatedWorld(args.replicas, args.rps, seed=args.seed)), args.json)
        world = RealWorld(args.context, args.seed, args.abort_file)
        install_abort_signals(world)
        return emit(run_experiment(experiment, world), args.json, args.journal)
    except InputError as exc:
        print(f"minichaos: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
