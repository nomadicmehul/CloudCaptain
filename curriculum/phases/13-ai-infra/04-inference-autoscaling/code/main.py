"""infscale: an autoscaler for model servers that scales on queue depth, and a simulator that shows why CPU is the wrong signal."""
from __future__ import annotations

import argparse
import json
import math
import os
import random
import re
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from collections import deque
from dataclasses import asdict, dataclass, field, replace
from typing import Any, Callable, Deque, Dict, Iterable, List, Optional, Sequence, Tuple

PROG = "infscale"


class InputError(ValueError):
    pass


METRIC_NAME = re.compile(r"[a-zA-Z_:][a-zA-Z0-9_:]*")
LABEL_NAME = re.compile(r"[a-zA-Z_][a-zA-Z0-9_]*")
ESCAPES = {"\\": "\\", '"': '"', "n": "\n"}
SPECIAL_VALUES = {"NaN": math.nan, "+Inf": math.inf, "Inf": math.inf, "-Inf": -math.inf}


@dataclass(frozen=True)
class Sample:
    name: str
    labels: Tuple[Tuple[str, str], ...]
    value: float

    def label(self, key: str, default: str = "") -> str:
        return dict(self.labels).get(key, default)


@dataclass
class Exposition:
    samples: List[Sample] = field(default_factory=list)
    types: Dict[str, str] = field(default_factory=dict)
    helps: Dict[str, str] = field(default_factory=dict)

    def names(self) -> set:
        return {s.name for s in self.samples} | set(self.types)

    def values(self, name: str) -> List[float]:
        return [s.value for s in self.samples if s.name == name and not math.isnan(s.value)]


def skip_spaces(line: str, index: int) -> int:
    while index < len(line) and line[index] in " \t":
        index += 1
    return index


def read_quoted(line: str, index: int, where: str) -> Tuple[str, int]:
    out: List[str] = []
    while index < len(line):
        char = line[index]
        if char == "\\":
            following = line[index + 1] if index + 1 < len(line) else ""
            out.append(ESCAPES.get(following, "\\" + following))
            index += 2
            continue
        if char == '"':
            return "".join(out), index + 1
        out.append(char)
        index += 1
    raise InputError(f"{where}: unterminated label value")


def parse_labels(line: str, start: int, where: str) -> Tuple[Tuple[Tuple[str, str], ...], int]:
    labels: List[Tuple[str, str]] = []
    index = start + 1
    while True:
        index = skip_spaces(line, index)
        if index < len(line) and line[index] == "}":
            return tuple(labels), index + 1
        match = LABEL_NAME.match(line, index)
        if not match:
            raise InputError(f"{where}: expected a label name at column {index + 1}")
        index = skip_spaces(line, match.end())
        if index >= len(line) or line[index] != "=":
            raise InputError(f"{where}: expected '=' after label {match.group(0)}")
        index = skip_spaces(line, index + 1)
        if index >= len(line) or line[index] != '"':
            raise InputError(f"{where}: label {match.group(0)} needs a quoted value")
        value, index = read_quoted(line, index + 1, where)
        labels.append((match.group(0), value))
        index = skip_spaces(line, index)
        if index < len(line) and line[index] == ",":
            index += 1
            continue
        if index < len(line) and line[index] == "}":
            return tuple(labels), index + 1
        raise InputError(f"{where}: expected ',' or '}}' at column {index + 1}")


def parse_value(text: str, where: str) -> float:
    if text in SPECIAL_VALUES:
        return SPECIAL_VALUES[text]
    try:
        return float(text)
    except ValueError:
        raise InputError(f"{where}: {text!r} is not a sample value") from None


def parse_exposition(text: str, source: str = "metrics") -> Exposition:
    result = Exposition()
    for number, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        where = f"{source}:{number}"
        if not line:
            continue
        if line.startswith("#"):
            parts = line[1:].strip().split(None, 2)
            if len(parts) >= 3 and parts[0] == "TYPE":
                result.types[parts[1]] = parts[2].strip()
            elif len(parts) >= 2 and parts[0] == "HELP":
                result.helps[parts[1]] = parts[2] if len(parts) > 2 else ""
            continue
        match = METRIC_NAME.match(line)
        if not match:
            raise InputError(f"{where}: {line[:60]!r} does not start with a metric name")
        rest = match.end()
        labels: Tuple[Tuple[str, str], ...] = ()
        if rest < len(line) and line[rest] == "{":
            labels, rest = parse_labels(line, rest, where)
        fields = line[rest:].split()
        if not fields or len(fields) > 2:
            raise InputError(f"{where}: expected a value and an optional timestamp after {match.group(0)}")
        result.samples.append(Sample(match.group(0), labels, parse_value(fields[0], where)))
    return result


@dataclass(frozen=True)
class ServerProfile:
    name: str
    running: Tuple[str, ...]
    waiting: Tuple[str, ...]
    kv_usage: Tuple[str, ...]
    prompt_tokens: Tuple[str, ...]
    generation_tokens: Tuple[str, ...]


PROFILES: Tuple[ServerProfile, ...] = (
    ServerProfile("vllm", ("vllm:num_requests_running",), ("vllm:num_requests_waiting",), ("vllm:kv_cache_usage_perc", "vllm:gpu_cache_usage_perc"), ("vllm:prompt_tokens_total",), ("vllm:generation_tokens_total",)),
    ServerProfile("llama.cpp", ("llamacpp:requests_processing",), ("llamacpp:requests_deferred",), (), ("llamacpp:prompt_tokens_total",), ("llamacpp:tokens_predicted_total",)),
    ServerProfile("sglang", ("sglang:num_running_reqs",), ("sglang:num_queue_reqs",), ("sglang:token_usage",), ("sglang:prompt_tokens_total",), ("sglang:generation_tokens_total",)),
)
CPU_SECONDS = "process_cpu_seconds_total"


@dataclass
class ReplicaMetrics:
    name: str
    server: str = "sim"
    running: float = 0.0
    waiting: float = 0.0
    kv_usage: Optional[float] = None
    prompt_tokens: Optional[float] = None
    generation_tokens: Optional[float] = None
    cpu_seconds: Optional[float] = None
    generation_per_s: Optional[float] = None
    cpu_percent: Optional[float] = None

    @property
    def queue(self) -> float:
        return self.running + self.waiting


def first_present(exposition: Exposition, names: Sequence[str], combine: Callable[[List[float]], float]) -> Optional[float]:
    for name in names:
        values = exposition.values(name)
        if values:
            return combine(values)
    return None


def detect_profile(exposition: Exposition) -> ServerProfile:
    present = exposition.names()
    for profile in PROFILES:
        if present.intersection(profile.running + profile.waiting):
            return profile
    known = ", ".join(name for profile in PROFILES for name in profile.running + profile.waiting)
    raise InputError(f"no model-server queue metric found (looked for {known}); is this the /metrics page of vLLM, llama.cpp started with --metrics, or SGLang with --enable-metrics?")


def replica_metrics(exposition: Exposition, name: str) -> ReplicaMetrics:
    profile = detect_profile(exposition)
    return ReplicaMetrics(
        name=name,
        server=profile.name,
        running=first_present(exposition, profile.running, sum) or 0.0,
        waiting=first_present(exposition, profile.waiting, sum) or 0.0,
        kv_usage=first_present(exposition, profile.kv_usage, max),
        prompt_tokens=first_present(exposition, profile.prompt_tokens, sum),
        generation_tokens=first_present(exposition, profile.generation_tokens, sum),
        cpu_seconds=first_present(exposition, (CPU_SECONDS,), sum),
    )


def counter_delta(previous: Optional[float], current: Optional[float]) -> Optional[float]:
    if previous is None or current is None:
        return None
    return current if current < previous else current - previous


def add_rates(metrics: ReplicaMetrics, previous: Optional[Dict[str, float]], now: float, cpu_request_cores: Optional[float] = None) -> ReplicaMetrics:
    if not previous or now - previous.get("t", now) <= 0:
        return metrics
    elapsed = now - previous["t"]
    generated = counter_delta(previous.get("generation_tokens"), metrics.generation_tokens)
    if generated is not None:
        metrics.generation_per_s = generated / elapsed
    cpu = counter_delta(previous.get("cpu_seconds"), metrics.cpu_seconds)
    if cpu is not None and cpu_request_cores:
        metrics.cpu_percent = 100.0 * cpu / elapsed / cpu_request_cores
    return metrics


def counters_of(metrics: ReplicaMetrics, now: float) -> Dict[str, float]:
    counters = {"t": now}
    for key in ("generation_tokens", "cpu_seconds"):
        value = getattr(metrics, key)
        if value is not None:
            counters[key] = value
    return counters


METRICS = ("queue", "waiting", "running", "kv", "cpu")
ADDITIVE = ("queue", "waiting", "running")


@dataclass
class Policy:
    metric: str = "queue"
    target: float = 8.0
    min_replicas: int = 1
    max_replicas: int = 8
    tolerance: float = 0.1
    up_window_s: float = 0.0
    down_window_s: float = 300.0
    zero_cooldown_s: float = 300.0
    up_percent: float = 100.0
    up_pods: int = 4
    period_s: float = 15.0
    lookahead_s: float = 0.0
    name: str = ""

    @property
    def label(self) -> str:
        return self.name or f"{self.metric}:{self.target:g}"

    def validate(self) -> "Policy":
        if self.metric not in METRICS:
            raise InputError(f"unknown metric {self.metric!r}; choose one of {', '.join(METRICS)}")
        if self.target <= 0:
            raise InputError(f"{self.label}: the target must be above zero")
        if self.metric == "kv" and self.target > 1:
            raise InputError(f"{self.label}: a kv target is a fraction of the cache, for example 0.8")
        if self.min_replicas < 0 or self.max_replicas < max(1, self.min_replicas):
            raise InputError(f"{self.label}: need 0 <= min <= max and max >= 1 (got min {self.min_replicas}, max {self.max_replicas})")
        if self.metric in ("cpu", "kv") and self.min_replicas == 0:
            raise InputError(f"{self.label}: {self.metric} cannot scale from zero, because with no pod there is no sample; set min 1 or scale on a queue metric")
        if self.lookahead_s and self.metric not in ADDITIVE:
            raise InputError(f"{self.label}: lookahead projects demand forward, so it needs queue, waiting or running")
        if self.period_s <= 0 or self.tolerance < 0:
            raise InputError(f"{self.label}: period must be above zero and tolerance not negative")
        return self


POLICY_OPTIONS = {
    "min": ("min_replicas", int),
    "max": ("max_replicas", int),
    "up": ("up_window_s", float),
    "down": ("down_window_s", float),
    "cooldown": ("zero_cooldown_s", float),
    "tolerance": ("tolerance", float),
    "lookahead": ("lookahead_s", float),
    "period": ("period_s", float),
}


def parse_policy(spec: str, defaults: Optional[Policy] = None) -> Policy:
    head, _, options = spec.partition(",")
    metric, separator, target = head.partition(":")
    if not separator:
        raise InputError(f"policy {spec!r}: expected METRIC:TARGET[,option=value...], for example queue:48 or cpu:60,max=6")
    try:
        value = float(target)
    except ValueError:
        raise InputError(f"policy {spec!r}: target {target!r} is not a number") from None
    policy = replace(defaults or Policy(), metric=metric.strip(), target=value, name=spec)
    for item in filter(None, options.split(",")):
        key, equals, raw = item.partition("=")
        if key not in POLICY_OPTIONS or not equals:
            raise InputError(f"policy {spec!r}: unknown option {item!r}; known options: {', '.join(POLICY_OPTIONS)}")
        attribute, kind = POLICY_OPTIONS[key]
        try:
            setattr(policy, attribute, kind(raw))
        except ValueError:
            raise InputError(f"policy {spec!r}: {key}={raw!r} is not a number") from None
    return policy.validate()


@dataclass
class Observation:
    now: float
    current: int
    replicas: List[ReplicaMetrics]
    held: int = 0


@dataclass
class ScalerState:
    history: List[Tuple[float, int]] = field(default_factory=list)
    last_active: Optional[float] = None
    last_demand: Optional[Tuple[float, float]] = None
    counters: Dict[str, Dict[str, float]] = field(default_factory=dict)

    def to_json(self) -> Dict[str, Any]:
        return {"history": [list(item) for item in self.history], "last_active": self.last_active, "last_demand": list(self.last_demand) if self.last_demand else None, "counters": self.counters}

    @classmethod
    def from_json(cls, data: Dict[str, Any]) -> "ScalerState":
        demand = data.get("last_demand")
        return cls([(float(t), int(n)) for t, n in data.get("history") or []], data.get("last_active"), (float(demand[0]), float(demand[1])) if demand else None, dict(data.get("counters") or {}))


@dataclass
class Decision:
    policy: str
    current: int
    desired: int
    raw: Optional[int]
    demand: float
    reason: str
    steps: List[str]

    @property
    def direction(self) -> str:
        if self.desired > self.current:
            return "up"
        if self.desired < self.current:
            return "down"
        return "hold"

    def to_json(self) -> Dict[str, Any]:
        return {"policy": self.policy, "current": self.current, "desired": self.desired, "raw": self.raw, "demand": self.demand, "direction": self.direction, "reason": self.reason, "steps": self.steps}


def ceil_div(value: float, target: float) -> int:
    return max(0, math.ceil(value / target - 1e-9))


def raw_recommendation(policy: Policy, observation: Observation, state: ScalerState, steps: List[str]) -> Tuple[Optional[int], Optional[float]]:
    if policy.metric in ADDITIVE:
        total = sum(getattr(replica, policy.metric) for replica in observation.replicas)
        held = observation.held if policy.metric != "running" else 0
        total += held
        projected = total
        if policy.lookahead_s and state.last_demand is not None and observation.now > state.last_demand[0]:
            slope = (total - state.last_demand[1]) / (observation.now - state.last_demand[0])
            if slope > 0:
                projected = total + slope * policy.lookahead_s
                steps.append(f"lookahead: {policy.metric} grows {slope:.2f}/s, so after a {policy.lookahead_s:g} s cold start it will be {projected:.1f}")
        state.last_demand = (observation.now, total)
        raw = ceil_div(projected, policy.target)
        held_note = f" (including {held} held by the activator)" if held else ""
        steps.append(f"{policy.metric} {projected:g}{held_note} / target {policy.target:g} per replica = {raw} replica(s)")
        ratio = projected / (observation.current * policy.target) if observation.current else None
        return raw, ratio
    samples = [replica.kv_usage if policy.metric == "kv" else replica.cpu_percent for replica in observation.replicas]
    present = [value for value in samples if value is not None]
    if not present:
        steps.append(f"no {policy.metric} samples from ready replicas, so no recommendation")
        return None, None
    average = sum(present) / len(present)
    ratio = average / policy.target
    raw = max(0, math.ceil(ratio * len(present) - 1e-9))
    steps.append(f"average {policy.metric} {average:.3g} over {len(present)} replica(s) / target {policy.target:g} = ratio {ratio:.2f}, so {raw} replica(s)")
    return raw, ratio


def stabilise(policy: Policy, state: ScalerState, now: float, current: int, raw: int, steps: List[str]) -> int:
    horizon = max(policy.up_window_s, policy.down_window_s)
    state.history = [(t, n) for t, n in state.history if t > now - horizon]
    up_candidates = [n for t, n in state.history if t > now - policy.up_window_s] + [raw]
    down_candidates = [n for t, n in state.history if t > now - policy.down_window_s] + [raw]
    state.history.append((now, raw))
    up_recommendation = min(up_candidates)
    down_recommendation = max(down_candidates)
    stabilised = current
    if stabilised < up_recommendation:
        stabilised = up_recommendation
    if stabilised > down_recommendation:
        stabilised = down_recommendation
    if stabilised > raw:
        steps.append(f"scale-down stabilisation: the highest recommendation in the last {policy.down_window_s:g} s is {down_recommendation}, so not below {stabilised}")
    elif stabilised < raw:
        steps.append(f"scale-up stabilisation: the lowest recommendation in the last {policy.up_window_s:g} s is {up_recommendation}, so not above {stabilised}")
    return stabilised


def decide(policy: Policy, observation: Observation, state: ScalerState) -> Decision:
    steps: List[str] = []
    current = observation.current
    demand = sum(replica.queue for replica in observation.replicas) + observation.held
    active = demand > 0
    if active:
        state.last_active = observation.now
    raw, ratio = raw_recommendation(policy, observation, state, steps)
    if raw is None:
        return Decision(policy.label, current, current, None, demand, "no-samples", steps)
    reason = "metric"
    if current == 0:
        if not active:
            steps.append("no replicas and nothing queued: stay at zero")
            return Decision(policy.label, 0, 0, raw, demand, "idle", steps)
        raw = max(1, raw)
        reason = "activate-from-zero"
        steps.append(f"activation: {demand:g} request(s) waiting with no replica, start at least one")
    elif ratio is not None and abs(ratio - 1.0) <= policy.tolerance:
        steps.append(f"ratio {ratio:.2f} is within the {policy.tolerance:g} tolerance of 1.0: keep {current}")
        raw = current
        reason = "tolerance"
    stabilised = stabilise(policy, state, observation.now, current, raw, steps)
    if stabilised != raw:
        reason = "stabilised"
    desired = stabilised
    if desired > current:
        limit = max(math.ceil(current * (1 + policy.up_percent / 100.0)), current + policy.up_pods)
        if desired > limit:
            steps.append(f"scale-up rate limit: at most {limit} this period ({policy.up_percent:g}% or {policy.up_pods} pods, whichever is more)")
            desired = limit
            reason = "rate-limited"
    floor = max(1, policy.min_replicas)
    if desired > policy.max_replicas:
        steps.append(f"clamped to max {policy.max_replicas}")
        desired = policy.max_replicas
        reason = "max"
    if desired < floor:
        steps.append(f"clamped to min {floor}" + (" while requests are active or the cooldown runs" if policy.min_replicas == 0 else ""))
        desired = floor
        reason = "min"
    if policy.min_replicas == 0 and not active and current > 0:
        idle = math.inf if state.last_active is None else observation.now - state.last_active
        if idle >= policy.zero_cooldown_s:
            steps.append(f"idle for {'ever' if idle == math.inf else f'{idle:g} s'}, at least the {policy.zero_cooldown_s:g} s cooldown: scale to zero")
            desired = 0
            reason = "idle-to-zero"
        else:
            steps.append(f"idle for {idle:g} s of the {policy.zero_cooldown_s:g} s cooldown before scaling to zero")
    return Decision(policy.label, current, desired, raw, demand, reason, steps)


@dataclass
class GpuProfile:
    name: str = "8B model, FP16, one 80 GB GPU"
    params_billion: float = 8.0
    bytes_per_param: float = 2.0
    hbm_gb_per_s: float = 3350.0
    bandwidth_efficiency: float = 0.7
    peak_tflops: float = 990.0
    mfu: float = 0.4
    gpu_memory_gb: float = 80.0
    memory_utilization: float = 0.9
    reserve_gb: float = 4.0
    kv_bytes_per_token: int = 131072
    max_num_seqs: int = 64
    image_pull_s: float = 45.0
    load_gb_per_s: float = 1.0
    warmup_s: float = 30.0
    cpu_request_cores: float = 4.0
    cpu_idle_cores: float = 0.2
    cpu_engine_cores: float = 1.0
    cpu_per_seq_cores: float = 0.01

    @property
    def weights_bytes(self) -> float:
        return self.params_billion * 1e9 * self.bytes_per_param

    @property
    def kv_capacity_tokens(self) -> int:
        free = self.gpu_memory_gb * 1e9 * self.memory_utilization - self.weights_bytes - self.reserve_gb * 1e9
        if free <= 0:
            raise InputError(f"{self.name}: the weights ({self.weights_bytes / 1e9:g} GB) do not fit in {self.gpu_memory_gb:g} GB x {self.memory_utilization:g}")
        return int(free // self.kv_bytes_per_token)

    @property
    def prefill_tokens_per_s(self) -> float:
        return self.peak_tflops * 1e12 * self.mfu / (2 * self.params_billion * 1e9)

    @property
    def cold_start_s(self) -> float:
        return self.image_pull_s + self.weights_bytes / (self.load_gb_per_s * 1e9) + self.warmup_s

    def decode_step_s(self, context_tokens: float) -> float:
        return (self.weights_bytes + context_tokens * self.kv_bytes_per_token) / (self.hbm_gb_per_s * 1e9 * self.bandwidth_efficiency)

    def cpu_percent(self, running: int) -> float:
        cores = self.cpu_idle_cores + (self.cpu_engine_cores if running else 0.0) + self.cpu_per_seq_cores * running
        return 100.0 * cores / self.cpu_request_cores


@dataclass
class SimRequest:
    arrival: float
    prompt: int
    output: int
    first_token: Optional[float] = None
    finished: Optional[float] = None
    generated: float = 0.0
    rejected: bool = False


@dataclass
class SimReplica:
    index: int
    started: float
    ready_at: float
    running: List[SimRequest] = field(default_factory=list)
    waiting: Deque[SimRequest] = field(default_factory=deque)
    draining: bool = False
    stopped: Optional[float] = None
    debt: float = 0.0

    @property
    def outstanding(self) -> int:
        return len(self.running) + len(self.waiting)

    def reserved_tokens(self) -> int:
        return sum(r.prompt + r.output for r in self.running)

    def metrics(self, gpu: GpuProfile) -> ReplicaMetrics:
        used = sum(r.prompt + r.generated for r in self.running)
        return ReplicaMetrics(f"replica-{self.index}", "sim", float(len(self.running)), float(len(self.waiting)), used / gpu.kv_capacity_tokens, cpu_percent=gpu.cpu_percent(len(self.running)))


def advance(replica: SimReplica, now: float, dt: float, gpu: GpuProfile, done: List[SimRequest]) -> None:
    budget = dt - replica.debt
    replica.debt = 0.0
    reserved = replica.reserved_tokens()
    while replica.waiting and len(replica.running) < gpu.max_num_seqs and budget > 0:
        request = replica.waiting[0]
        if replica.running and reserved + request.prompt + request.output > gpu.kv_capacity_tokens:
            break
        replica.waiting.popleft()
        prefill = request.prompt / gpu.prefill_tokens_per_s
        request.first_token = max(request.arrival, now + dt - budget) + prefill
        request.generated = 1.0
        budget -= prefill
        reserved += request.prompt + request.output
        if request.output <= 1:
            request.finished = request.first_token
            done.append(request)
        else:
            replica.running.append(request)
    if budget < 0:
        replica.debt = -budget
        return
    if not replica.running:
        return
    step = gpu.decode_step_s(sum(r.prompt + r.generated for r in replica.running))
    tokens = budget / step
    still: List[SimRequest] = []
    for request in replica.running:
        need = request.output - request.generated
        if need <= tokens:
            request.finished = now + dt - budget + need * step
            request.generated = float(request.output)
            done.append(request)
        else:
            request.generated += tokens
            still.append(request)
    replica.running = still


def dispatch(request: SimRequest, ready: List[SimReplica]) -> None:
    min(ready, key=lambda r: (r.outstanding, r.index)).waiting.append(request)


def resize(replicas: List[SimReplica], desired: int, now: float, cold_start_s: float) -> int:
    live = [r for r in replicas if r.stopped is None and not r.draining]
    if desired > len(live):
        for _ in range(desired - len(live)):
            replicas.append(SimReplica(len(replicas), now, now + cold_start_s))
    elif desired < len(live):
        order = sorted(live, key=lambda r: (now >= r.ready_at, r.outstanding, -r.index))
        for replica in order[: len(live) - desired]:
            if now < replica.ready_at:
                replica.stopped = now
            else:
                replica.draining = True
    return desired - len(live)


def percentile(values: Sequence[float], pct: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    return float(ordered[max(1, math.ceil(pct / 100 * len(ordered))) - 1])


@dataclass
class SimResult:
    policy: str
    requests: int
    completed: int
    misses: int
    rejected: int
    in_flight: int
    ttft_p50_s: float
    ttft_p95_s: float
    ttft_max_s: float
    replica_minutes: float
    peak_replicas: int
    scale_events: int
    timeline: List[Dict[str, float]] = field(default_factory=list)

    @property
    def miss_rate(self) -> float:
        return self.misses / self.requests if self.requests else 0.0

    def to_json(self, timeline: bool = False) -> Dict[str, Any]:
        data = asdict(self)
        data["miss_rate"] = round(self.miss_rate, 4)
        data["replica_minutes"] = round(self.replica_minutes, 2)
        if not timeline:
            data.pop("timeline")
        return data


def simulate(trace: Sequence[Tuple[float, int, int]], policy: Policy, gpu: GpuProfile, duration: float, dt: float = 0.25, initial: int = 1, slo_ttft_s: float = 2.0, hold: bool = True, cold_start_s: Optional[float] = None) -> SimResult:
    if dt <= 0 or duration <= 0:
        raise InputError("duration and dt must be above zero")
    cold = gpu.cold_start_s if cold_start_s is None else cold_start_s
    requests = [SimRequest(t, p, o) for t, p, o in sorted(trace) if t < duration]
    replicas = [SimReplica(i, 0.0, 0.0) for i in range(initial)]
    held: Deque[SimRequest] = deque()
    state = ScalerState()
    done: List[SimRequest] = []
    timeline: List[Dict[str, float]] = []
    replica_seconds = 0.0
    peak = initial
    events = 0
    position = 0
    next_eval = 0.0
    for number in range(int(math.ceil(duration / dt))):
        now = number * dt
        live = [r for r in replicas if r.stopped is None]
        ready = [r for r in live if not r.draining and now >= r.ready_at]
        while held and ready:
            dispatch(held.popleft(), ready)
        while position < len(requests) and requests[position].arrival < now + dt:
            request = requests[position]
            position += 1
            if ready:
                dispatch(request, ready)
            elif hold:
                held.append(request)
            else:
                request.rejected = True
        for replica in live:
            if now >= replica.ready_at:
                advance(replica, now, dt, gpu, done)
            if replica.draining and not replica.outstanding:
                replica.stopped = now + dt
        replica_seconds += dt * len(live)
        if now + 1e-9 >= next_eval:
            serving = [r for r in replicas if r.stopped is None and not r.draining]
            observation = Observation(now, len(serving), [r.metrics(gpu) for r in serving if now >= r.ready_at], len(held))
            decision = decide(policy, observation, state)
            change = resize(replicas, decision.desired, now, cold)
            if change:
                events += 1
            peak = max(peak, decision.desired)
            timeline.append({"t": now, "replicas": float(len(serving)), "ready": float(len(observation.replicas)), "demand": decision.demand, "desired": float(decision.desired)})
            next_eval += policy.period_s
    ttfts = [r.first_token - r.arrival for r in requests if r.first_token is not None]
    misses = 0
    in_flight = 0
    for request in requests:
        if request.rejected:
            misses += 1
        elif request.first_token is None:
            in_flight += 1
            if duration - request.arrival > slo_ttft_s:
                misses += 1
        elif request.first_token - request.arrival > slo_ttft_s:
            misses += 1
    return SimResult(policy.label, len(requests), sum(1 for r in requests if r.finished is not None), misses, sum(1 for r in requests if r.rejected), in_flight, percentile(ttfts, 50), percentile(ttfts, 95), max(ttfts) if ttfts else 0.0, replica_seconds / 60.0, peak, events, timeline)


def parse_rates(text: str) -> List[Tuple[float, float]]:
    segments: List[Tuple[float, float]] = []
    for item in filter(None, (part.strip() for part in text.split(","))):
        start, equals, rate = item.partition("=")
        try:
            segment = (float(start), float(rate))
        except ValueError:
            raise InputError(f"rates {text!r}: {item!r} is not START=REQUESTS_PER_SECOND") from None
        if not equals or segment[1] < 0 or (segments and segment[0] <= segments[-1][0]):
            raise InputError(f"rates {text!r}: use ascending START=RATE pairs such as 0=2,300=20,900=0")
        segments.append(segment)
    if not segments or segments[0][0] != 0:
        raise InputError(f"rates {text!r}: the first segment must start at 0")
    return segments


def generate_trace(rates: Sequence[Tuple[float, float]], duration: float, prompt: int = 1000, output: int = 200, seed: int = 7, spread: float = 0.25) -> List[Tuple[float, int, int]]:
    rng = random.Random(seed)
    trace: List[Tuple[float, int, int]] = []
    bounds = [start for start, _ in rates[1:]] + [duration]
    for (start, rate), end in zip(rates, bounds):
        if rate <= 0:
            continue
        t = start + rng.expovariate(rate)
        while t < min(end, duration):
            trace.append((t, max(1, int(prompt * rng.uniform(1 - spread, 1 + spread))), max(1, int(output * rng.uniform(1 - spread, 1 + spread)))))
            t += rng.expovariate(rate)
    return trace


def load_trace(path: str) -> List[Tuple[float, int, int]]:
    with open(path, encoding="utf-8") as handle:
        text = handle.read()
    stripped = text.lstrip()
    rows = json.loads(text) if stripped.startswith("[") else [json.loads(line) for line in text.splitlines() if line.strip()]
    trace = []
    for number, row in enumerate(rows, 1):
        try:
            trace.append((float(row["t"]), int(row["prompt_tokens"]), int(row["output_tokens"])))
        except (KeyError, TypeError, ValueError):
            raise InputError(f"{path}: row {number} needs t, prompt_tokens and output_tokens: {row!r}") from None
    return trace


Opener = urllib.request.OpenerDirector


def plain_opener() -> Opener:
    return urllib.request.build_opener(urllib.request.ProxyHandler({}))


def http_get(url: str, opener: Optional[Opener] = None, timeout: float = 10.0) -> str:
    try:
        with (opener or plain_opener()).open(url, timeout=timeout) as response:
            return response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"GET {url}: HTTP {exc.code}") from None
    except (urllib.error.URLError, OSError) as exc:
        raise RuntimeError(f"GET {url}: {getattr(exc, 'reason', exc)}") from None


def read_source(source: str, opener: Optional[Opener] = None) -> Tuple[str, str]:
    if source == "-":
        return "stdin", sys.stdin.read()
    if source.startswith(("http://", "https://")):
        return source, http_get(source, opener)
    with open(source, encoding="utf-8") as handle:
        return os.path.basename(source), handle.read()


def base_url(url: str) -> str:
    url = url.rstrip("/")
    return url[:-3] if url.endswith("/v1") else url


@dataclass
class LoadReport:
    url: str
    model: str
    concurrency: int
    sent: int
    ok: int
    statuses: Dict[str, int]
    elapsed_s: float
    latency_p50_s: float
    latency_p95_s: float
    output_tokens: int

    @property
    def requests_per_s(self) -> float:
        return self.ok / self.elapsed_s if self.elapsed_s else 0.0

    def to_json(self) -> Dict[str, Any]:
        data = asdict(self)
        data["requests_per_s"] = round(self.requests_per_s, 3)
        return data


def discover_model(base: str, opener: Opener, timeout: float = 10.0) -> str:
    try:
        models = json.loads(http_get(base + "/v1/models", opener, timeout)).get("data") or []
    except json.JSONDecodeError:
        raise RuntimeError(f"{base}/v1/models did not return JSON") from None
    if not models:
        raise RuntimeError(f"{base}/v1/models lists no model; pass --model")
    return str(models[0]["id"])


def one_completion(base: str, model: str, prompt: str, max_tokens: int, opener: Opener, timeout: float) -> Tuple[str, float, int]:
    body = json.dumps({"model": model, "prompt": prompt, "max_tokens": max_tokens, "temperature": 0}).encode("utf-8")
    request = urllib.request.Request(base + "/v1/completions", data=body, headers={"Content-Type": "application/json"}, method="POST")
    started = time.monotonic()
    try:
        with opener.open(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8") or "{}")
            return str(response.status), time.monotonic() - started, int((payload.get("usage") or {}).get("completion_tokens") or 0)
    except urllib.error.HTTPError as exc:
        return str(exc.code), time.monotonic() - started, 0
    except (urllib.error.URLError, OSError, ValueError) as exc:
        return ("timeout" if "timed out" in str(exc) else "connection-error"), time.monotonic() - started, 0


def run_load(url: str, concurrency: int, total: Optional[int] = None, duration: Optional[float] = None, max_tokens: int = 16, prompt: str = "Explain autoscaling in one sentence.", model: str = "", opener: Optional[Opener] = None, timeout: float = 60.0) -> LoadReport:
    if concurrency < 1:
        raise InputError("concurrency must be at least 1")
    if total is None and duration is None:
        raise InputError("give --requests N, --duration SECONDS or both")
    base = base_url(url)
    opener = opener or plain_opener()
    model = model or discover_model(base, opener)
    lock = threading.Lock()
    issued = [0]
    results: List[Tuple[str, float, int]] = []
    started = time.monotonic()
    deadline = started + duration if duration is not None else None

    def take() -> bool:
        with lock:
            if total is not None and issued[0] >= total:
                return False
            if deadline is not None and time.monotonic() >= deadline:
                return False
            issued[0] += 1
            return True

    def worker() -> None:
        while take():
            outcome = one_completion(base, model, prompt, max_tokens, opener, timeout)
            with lock:
                results.append(outcome)

    threads = [threading.Thread(target=worker, daemon=True) for _ in range(concurrency)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    elapsed = time.monotonic() - started
    statuses: Dict[str, int] = {}
    for status, _, _ in results:
        statuses[status] = statuses.get(status, 0) + 1
    good = [latency for status, latency, _ in results if status == "200"]
    return LoadReport(base, model, concurrency, len(results), len(good), statuses, elapsed, percentile(good, 50), percentile(good, 95), sum(tokens for _, _, tokens in results))


Runner = Callable[[List[str]], str]


def kubectl_runner(context: Optional[str] = None) -> Runner:
    def run(args: List[str]) -> str:
        command = ["kubectl"] + (["--context", context] if context else []) + args
        try:
            done = subprocess.run(command, capture_output=True, text=True)
        except FileNotFoundError:
            raise RuntimeError("kubectl is not installed or not on PATH") from None
        if done.returncode != 0:
            raise RuntimeError(f"{' '.join(command)}: {(done.stderr or done.stdout).strip()}")
        return done.stdout
    return run


def pod_ready(pod: Dict[str, Any]) -> bool:
    if (pod.get("metadata") or {}).get("deletionTimestamp"):
        return False
    for condition in (pod.get("status") or {}).get("conditions") or []:
        if condition.get("type") == "Ready":
            return condition.get("status") == "True"
    return False


def parse_cpu(text: Any) -> float:
    raw = str(text).strip()
    try:
        return float(raw[:-1]) / 1000 if raw.endswith("m") else float(raw)
    except ValueError:
        raise InputError(f"not a CPU quantity: {raw!r} (examples: 500m, 2)") from None


def cpu_request_cores(pod: Dict[str, Any]) -> Optional[float]:
    values = [((c.get("resources") or {}).get("requests") or {}).get("cpu") for c in (pod.get("spec") or {}).get("containers") or []]
    present = [parse_cpu(v) for v in values if v is not None]
    return sum(present) if present else None


def observe_deployment(run: Runner, namespace: str, deployment: str, port: int, path: str, now: float, state: ScalerState) -> Tuple[Observation, int]:
    spec = json.loads(run(["-n", namespace, "get", "deployment", deployment, "-o", "json"]))
    current = int((spec.get("spec") or {}).get("replicas", 1))
    labels = ((spec.get("spec") or {}).get("selector") or {}).get("matchLabels") or {}
    if not labels:
        raise InputError(f"deployment {namespace}/{deployment} has no spec.selector.matchLabels")
    selector = ",".join(f"{key}={value}" for key, value in sorted(labels.items()))
    pods = json.loads(run(["-n", namespace, "get", "pods", "-l", selector, "-o", "json"])).get("items") or []
    replicas: List[ReplicaMetrics] = []
    seen = set()
    for pod in sorted(pods, key=lambda p: p["metadata"]["name"]):
        name = pod["metadata"]["name"]
        if not pod_ready(pod):
            continue
        text = run(["get", "--raw", f"/api/v1/namespaces/{namespace}/pods/{name}:{port}/proxy{path}"])
        metrics = add_rates(replica_metrics(parse_exposition(text, name), name), state.counters.get(name), now, cpu_request_cores(pod))
        state.counters[name] = counters_of(metrics, now)
        seen.add(name)
        replicas.append(metrics)
    state.counters = {name: value for name, value in state.counters.items() if name in seen}
    return Observation(now, current, replicas), len(pods)


def load_state(path: Optional[str]) -> ScalerState:
    if not path or not os.path.exists(path):
        return ScalerState()
    with open(path, encoding="utf-8") as handle:
        try:
            return ScalerState.from_json(json.load(handle))
        except (json.JSONDecodeError, TypeError, ValueError, IndexError):
            raise InputError(f"{path}: not an infscale state file; delete it to start fresh") from None


def save_state(path: Optional[str], state: ScalerState) -> None:
    if path:
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(state.to_json(), handle, indent=2)


def show(value: Optional[float], digits: int = 1, suffix: str = "") -> str:
    if value is None:
        return "-"
    return f"{value:.{digits}f}{suffix}"


def replica_table(replicas: Sequence[ReplicaMetrics]) -> List[str]:
    lines = [f"  {'replica':<34} {'server':<9} {'running':>7} {'waiting':>7} {'queue':>5} {'kv':>5} {'out tok/s':>9} {'cpu':>6}"]
    for r in replicas:
        lines.append(f"  {r.name[:34]:<34} {r.server:<9} {r.running:>7g} {r.waiting:>7g} {r.queue:>5g} {show(r.kv_usage, 2):>5} {show(r.generation_per_s):>9} {show(r.cpu_percent, 1, '%'):>6}")
    return lines


def decision_lines(decision: Decision, policy: Policy) -> List[str]:
    lines = [f"{decision.policy} (min {policy.min_replicas}, max {policy.max_replicas}): current {decision.current} -> desired {decision.desired} ({decision.direction}; {decision.reason})"]
    lines += [f"  {step}" for step in decision.steps]
    return lines


def add_policy_flags(parser: argparse.ArgumentParser) -> None:
    group = parser.add_argument_group("policy")
    group.add_argument("--metric", default="queue", choices=METRICS, help="queue = running + waiting per replica (default)")
    group.add_argument("--target", type=float, default=8.0, help="target value per replica (queue: requests; kv: 0-1; cpu: percent)")
    group.add_argument("--min", dest="min_replicas", type=int, default=1, help="0 enables scale to zero")
    group.add_argument("--max", dest="max_replicas", type=int, default=8)
    group.add_argument("--tolerance", type=float, default=0.1)
    group.add_argument("--up-window", type=float, default=0.0, help="scale-up stabilisation window in seconds")
    group.add_argument("--down-window", type=float, default=300.0, help="scale-down stabilisation window in seconds")
    group.add_argument("--zero-cooldown", type=float, default=300.0, help="idle seconds before scaling to zero (KEDA cooldownPeriod)")
    group.add_argument("--lookahead", type=float, default=0.0, help="project demand this many seconds ahead, usually the cold start")


def policy_from_args(args: argparse.Namespace) -> Policy:
    return Policy(metric=args.metric, target=args.target, min_replicas=args.min_replicas, max_replicas=args.max_replicas, tolerance=args.tolerance, up_window_s=args.up_window, down_window_s=args.down_window, zero_cooldown_s=args.zero_cooldown, lookahead_s=args.lookahead).validate()


def emit(args: argparse.Namespace, data: Any, lines: Iterable[str]) -> None:
    if getattr(args, "json", False):
        print(json.dumps(data, indent=2))
    else:
        for line in lines:
            print(line)


def cmd_parse(args: argparse.Namespace) -> int:
    replicas = []
    for source in args.sources:
        name, text = read_source(source)
        replicas.append(replica_metrics(parse_exposition(text, name), name))
    emit(args, [asdict(r) for r in replicas], replica_table(replicas))
    return 0


def cmd_decide(args: argparse.Namespace) -> int:
    policy = policy_from_args(args)
    state = load_state(args.state)
    now = time.time() if args.now is None else args.now
    replicas = []
    for source in args.sources:
        name, text = read_source(source)
        metrics = add_rates(replica_metrics(parse_exposition(text, name), name), state.counters.get(name), now, args.cpu_request)
        state.counters[name] = counters_of(metrics, now)
        replicas.append(metrics)
    current = len(replicas) if args.current is None else args.current
    decision = decide(policy, Observation(now, current, replicas, args.held), state)
    save_state(args.state, state)
    emit(args, dict(decision.to_json(), replicas=[asdict(r) for r in replicas]), replica_table(replicas) + decision_lines(decision, policy))
    return 0


def kube_step(run: Runner, namespace: str, deployment: str, policy: Policy, state: ScalerState, now: float, port: int = 8000, path: str = "/metrics", apply: bool = False) -> Tuple[Decision, Observation, int, bool]:
    observation, pods = observe_deployment(run, namespace, deployment, port, path, now, state)
    decision = decide(policy, observation, state)
    applied = False
    if apply and decision.desired != observation.current:
        run(["-n", namespace, "scale", f"deployment/{deployment}", f"--replicas={decision.desired}"])
        applied = True
    return decision, observation, pods, applied


def cmd_kube(args: argparse.Namespace) -> int:
    policy = policy_from_args(args)
    state = load_state(args.state)
    now = time.time() if args.now is None else args.now
    decision, observation, pods, applied = kube_step(kubectl_runner(args.context), args.namespace, args.deployment, policy, state, now, args.port, args.path, args.apply)
    save_state(args.state, state)
    header = f"deployment {args.namespace}/{args.deployment}: spec.replicas {observation.current}, pods {pods}, ready with metrics {len(observation.replicas)}"
    lines = [header] + replica_table(observation.replicas) + decision_lines(decision, policy)
    if applied:
        lines.append(f"applied: kubectl -n {args.namespace} scale deployment/{args.deployment} --replicas={decision.desired}")
    emit(args, dict(decision.to_json(), namespace=args.namespace, deployment=args.deployment, pods=pods, ready=len(observation.replicas), applied=applied, replicas=[asdict(r) for r in observation.replicas]), lines)
    return 0


DEFAULT_RATES = "0=3,180=10,270=20,360=30,720=10,840=3,960=0,1320=0.2"
DEFAULT_POLICIES = ("cpu:60", "cpu:25", "queue:24", "queue:24,lookahead=90", "queue:24,min=0,cooldown=120")


def gpu_from_args(args: argparse.Namespace) -> GpuProfile:
    gpu = GpuProfile()
    if args.max_num_seqs is not None:
        if args.max_num_seqs < 1:
            raise InputError("--max-num-seqs must be at least 1")
        gpu.max_num_seqs = args.max_num_seqs
    gpu.kv_capacity_tokens
    return gpu


def gpu_lines(gpu: GpuProfile, prompt: int = 1000) -> List[str]:
    small = gpu.decode_step_s(prompt)
    full = gpu.decode_step_s(gpu.max_num_seqs * prompt)
    free_gb = gpu.kv_capacity_tokens * gpu.kv_bytes_per_token / 1e9
    return [
        f"replica model: {gpu.name}",
        f"  weights      {gpu.weights_bytes / 1e9:.1f} GB ({gpu.params_billion:g}B parameters x {gpu.bytes_per_param:g} bytes)",
        f"  KV cache     {free_gb:.1f} GB free = {gpu.kv_capacity_tokens} tokens at {gpu.kv_bytes_per_token} bytes per token",
        f"  prefill      {gpu.prefill_tokens_per_s:.0f} tokens/s ({gpu.peak_tflops:g} TFLOPS x {gpu.mfu:g} MFU / {2 * gpu.params_billion:g} GFLOP per token)",
        f"  decode step  {small * 1000:.1f} ms at batch 1, {full * 1000:.1f} ms at batch {gpu.max_num_seqs} ({prompt}-token contexts): {gpu.max_num_seqs / full:.0f} output tokens/s at full batch",
        f"  cold start   {gpu.cold_start_s:.0f} s = image {gpu.image_pull_s:g} s + weights {gpu.weights_bytes / (gpu.load_gb_per_s * 1e9):.0f} s at {gpu.load_gb_per_s:g} GB/s + warm-up {gpu.warmup_s:g} s",
        f"  pod CPU      idle {gpu.cpu_percent(0):.0f}%, batch 1 {gpu.cpu_percent(1):.0f}%, batch {gpu.max_num_seqs} {gpu.cpu_percent(gpu.max_num_seqs):.0f}% of {gpu.cpu_request_cores:g} requested cores, whatever the queue",
    ]


def cmd_gpu(args: argparse.Namespace) -> int:
    gpu = gpu_from_args(args)
    data = dict(asdict(gpu), weights_gb=gpu.weights_bytes / 1e9, kv_capacity_tokens=gpu.kv_capacity_tokens, prefill_tokens_per_s=gpu.prefill_tokens_per_s, cold_start_s=gpu.cold_start_s, decode_step_ms_batch_1=gpu.decode_step_s(1000) * 1000)
    emit(args, data, gpu_lines(gpu))
    return 0


def cmd_simulate(args: argparse.Namespace) -> int:
    gpu = gpu_from_args(args)
    if args.trace:
        trace = load_trace(args.trace)
        duration = args.duration or (max(t for t, _, _ in trace) + 120 if trace else 60)
        described = f"{args.trace}"
    else:
        rates = parse_rates(args.rates)
        duration = args.duration or 1500.0
        trace = generate_trace(rates, duration, args.prompt_tokens, args.output_tokens, args.seed)
        described = f"rates {args.rates} (requests/s from second N), prompt ~{args.prompt_tokens}, output ~{args.output_tokens} tokens, seed {args.seed}"
    defaults = Policy(max_replicas=args.max_replicas, down_window_s=args.down_window)
    policies = [parse_policy(spec, defaults) for spec in (args.policy or DEFAULT_POLICIES)]
    cold = args.cold_start
    results = [simulate(trace, policy, gpu, duration, args.dt, args.initial, args.slo_ttft, not args.no_hold, cold) for policy in policies]
    lines = gpu_lines(gpu) + [f"trace: {len([t for t in trace if t[0] < duration])} requests over {duration:g} s, {described}", f"SLO: time to first token <= {args.slo_ttft:g} s; cold start {gpu.cold_start_s if cold is None else cold:g} s; {'activator holds requests while no replica is ready' if not args.no_hold else 'no activator: requests fail while no replica is ready'}", ""]
    lines.append(f"{'policy':<30} {'SLO miss':>9} {'miss %':>7} {'TTFT p50':>9} {'TTFT p95':>9} {'replica-min':>11} {'peak':>5} {'scale events':>12}")
    for result in results:
        lines.append(f"{result.policy:<30} {result.misses:>9} {100 * result.miss_rate:>6.1f}% {result.ttft_p50_s:>8.2f}s {result.ttft_p95_s:>8.2f}s {result.replica_minutes:>11.1f} {result.peak_replicas:>5} {result.scale_events:>12}")
    if args.timeline:
        for result in results:
            lines.append("")
            lines.append(f"timeline {result.policy}: t, replicas (ready), demand, desired")
            for point in result.timeline:
                lines.append(f"  {point['t']:>7.0f}s {point['replicas']:>3.0f} ({point['ready']:.0f}) {point['demand']:>7.0f} -> {point['desired']:.0f}")
    emit(args, {"duration_s": duration, "requests": len(trace), "slo_ttft_s": args.slo_ttft, "results": [r.to_json(args.timeline) for r in results]}, lines)
    return 0


def cmd_load(args: argparse.Namespace) -> int:
    report = run_load(args.url, args.concurrency, args.requests, args.duration, args.max_tokens, args.prompt, args.model, timeout=args.timeout)
    lines = [
        f"{report.url} model {report.model}: {report.sent} sent at concurrency {report.concurrency} in {report.elapsed_s:.1f} s",
        f"  ok {report.ok}  statuses {json.dumps(report.statuses, sort_keys=True)}  {report.requests_per_s:.2f} req/s  latency p50 {report.latency_p50_s:.2f} s p95 {report.latency_p95_s:.2f} s  output tokens {report.output_tokens}",
    ]
    emit(args, report.to_json(), lines)
    return 0 if report.ok else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog=PROG, description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)

    parse = commands.add_parser("parse", help="read model-server /metrics pages (files, URLs or -) into per-replica signals")
    parse.add_argument("sources", nargs="+")
    parse.add_argument("--json", action="store_true")
    parse.set_defaults(func=cmd_parse)

    choose = commands.add_parser("decide", help="one autoscaling decision from one /metrics page per ready replica")
    choose.add_argument("sources", nargs="*", help="one /metrics file or URL per ready replica")
    choose.add_argument("--current", type=int, help="replicas the Deployment has now, ready or not (default: one per source)")
    choose.add_argument("--held", type=int, default=0, help="requests an activator holds while no replica is ready")
    choose.add_argument("--state", help="JSON file that keeps recommendations, counters and activity between runs")
    choose.add_argument("--now", type=float, help="the clock in seconds (default: time.time())")
    choose.add_argument("--cpu-request", type=float, help="CPU cores requested per pod, to turn process_cpu_seconds_total into percent")
    choose.add_argument("--json", action="store_true")
    add_policy_flags(choose)
    choose.set_defaults(func=cmd_decide)

    kube = commands.add_parser("kube", help="scrape a Deployment's ready pods through the API server proxy, decide, optionally kubectl scale")
    kube.add_argument("--namespace", "-n", required=True)
    kube.add_argument("--deployment", required=True)
    kube.add_argument("--port", type=int, default=8000)
    kube.add_argument("--path", default="/metrics")
    kube.add_argument("--context")
    kube.add_argument("--state")
    kube.add_argument("--now", type=float)
    kube.add_argument("--apply", action="store_true", help="run kubectl scale when the decision changes the replica count")
    kube.add_argument("--json", action="store_true")
    add_policy_flags(kube)
    kube.set_defaults(func=cmd_kube)

    gpu = commands.add_parser("gpu", help="print the replica model the simulator derives from memory, bandwidth and FLOPs")
    gpu.add_argument("--max-num-seqs", type=int)
    gpu.add_argument("--json", action="store_true")
    gpu.set_defaults(func=cmd_gpu)

    sim = commands.add_parser("simulate", help="replay a traffic trace against autoscaling policies and report SLO misses and replica-minutes")
    source = sim.add_mutually_exclusive_group()
    source.add_argument("--trace", help="JSON or JSONL rows with t, prompt_tokens, output_tokens")
    source.add_argument("--rates", default=DEFAULT_RATES, help=f"START=REQUESTS_PER_SECOND segments (default {DEFAULT_RATES})")
    sim.add_argument("--duration", type=float)
    sim.add_argument("--policy", action="append", help=f"METRIC:TARGET[,min=,max=,up=,down=,cooldown=,lookahead=,tolerance=] (repeatable; default {' '.join(DEFAULT_POLICIES)})")
    sim.add_argument("--max", dest="max_replicas", type=int, default=8)
    sim.add_argument("--down-window", type=float, default=300.0)
    sim.add_argument("--max-num-seqs", type=int)
    sim.add_argument("--cold-start", type=float, help="seconds from scale-up to Ready (default: derived from image, weights and warm-up)")
    sim.add_argument("--slo-ttft", type=float, default=2.0)
    sim.add_argument("--initial", type=int, default=1)
    sim.add_argument("--no-hold", action="store_true", help="no activator: requests that arrive with no ready replica fail")
    sim.add_argument("--prompt-tokens", type=int, default=1000)
    sim.add_argument("--output-tokens", type=int, default=200)
    sim.add_argument("--seed", type=int, default=7)
    sim.add_argument("--dt", type=float, default=0.25)
    sim.add_argument("--timeline", action="store_true")
    sim.add_argument("--json", action="store_true")
    sim.set_defaults(func=cmd_simulate)

    load = commands.add_parser("load", help="send concurrent /v1/completions requests to an OpenAI-compatible server")
    load.add_argument("--url", required=True, help="server base URL, for example http://127.0.0.1:8000")
    load.add_argument("--concurrency", type=int, default=4)
    load.add_argument("--requests", type=int)
    load.add_argument("--duration", type=float)
    load.add_argument("--max-tokens", type=int, default=16)
    load.add_argument("--prompt", default="Explain autoscaling in one sentence.")
    load.add_argument("--model", default="", help="model id (default: the first one /v1/models lists)")
    load.add_argument("--timeout", type=float, default=120.0)
    load.add_argument("--json", action="store_true")
    load.set_defaults(func=cmd_load)
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(list(sys.argv[1:] if argv is None else argv))
    try:
        return int(args.func(args))
    except InputError as exc:
        print(f"{PROG}: {exc}", file=sys.stderr)
        return 2
    except FileNotFoundError as exc:
        print(f"{PROG}: {exc.filename}: no such file", file=sys.stderr)
        return 2
    except (RuntimeError, OSError) as exc:
        print(f"{PROG}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
