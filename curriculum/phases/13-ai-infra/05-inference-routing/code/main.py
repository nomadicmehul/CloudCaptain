"""prefixroute: a prefix-cache-aware router for OpenAI-compatible model servers. It hashes prompts into chained token blocks, scores replicas on prefix matches, queue depth and KV pressure the way the llm-d endpoint picker does, simulates routing policies on a trace, prices prefill/decode disaggregation and resolves InferencePool endpoints."""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import random
import re
import socket
import struct
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import OrderedDict
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

TOKEN_RE = re.compile(r"""([{}:,\-.?!;@#$%^&*()+_~/\\><\[\]="']|\w+)(\s*)""")
FNV_OFFSET = 0x811C9DC5
FNV_PRIME = 0x01000193
POLICIES = ("prefix", "prefix-only", "least-queue", "round-robin", "random")
DEFAULT_WEIGHTS: Dict[str, float] = {"prefix": 3.0, "queue": 2.0, "kv": 2.0, "no-hit-lru": 1.0}
ROUTED_PATHS = ("/v1/chat/completions", "/v1/completions")
METRIC_WAITING = "vllm:num_requests_waiting"
METRIC_RUNNING = "vllm:num_requests_running"
METRIC_KV = "vllm:kv_cache_usage_perc"
METRIC_HITS = "vllm:prefix_cache_hits_total"
METRIC_QUERIES = "vllm:prefix_cache_queries_total"
METRIC_CACHE_CONFIG = "vllm:cache_config_info"
SAMPLE_RE = re.compile(r"^([a-zA-Z_:][a-zA-Z0-9_:]*)(?:\{(.*)\})?\s+(\S+)")
LABEL_RE = re.compile(r'([a-zA-Z_][a-zA-Z0-9_]*)="((?:[^"\\]|\\.)*)"')
HOP_BY_HOP = {"connection", "keep-alive", "proxy-connection", "transfer-encoding", "te", "trailer", "upgrade", "content-length", "host"}
LABEL_VALUE_RE = re.compile(r"^(([A-Za-z0-9][-A-Za-z0-9_.]*)?[A-Za-z0-9])?$")
APP_PROTOCOLS = ("http", "kubernetes.io/h2c")
FAILURE_MODES = ("FailOpen", "FailClose")
UNIQUE_PORT_MESSAGE = "port number must be unique"
SERVICE_PORT_MESSAGE = "port is required when kind is 'Service' or unspecified (defaults to 'Service')"
WORDS = (
    "pod", "node", "deploy", "rollout", "replica", "service", "ingress", "gateway", "route", "token",
    "cache", "prefix", "block", "queue", "latency", "budget", "tenant", "policy", "quota", "secret",
    "volume", "image", "registry", "digest", "probe", "restart", "scale", "metric", "alert", "trace",
    "span", "label", "selector", "namespace", "cluster", "region", "zone", "shard", "batch", "stream",
    "prompt", "model", "weights", "adapter", "gpu", "memory", "bandwidth", "throughput", "request", "answer",
    "refund", "invoice", "order", "account", "ticket", "escalate", "summary", "context", "window", "limit",
)


class InputError(Exception):
    pass


def fnv1a_32(text: str) -> int:
    value = FNV_OFFSET
    for byte in text.encode("utf-8"):
        value = ((value ^ byte) * FNV_PRIME) & 0xFFFFFFFF
    return value


def tokenize(text: str) -> List[int]:
    return [fnv1a_32(match.group(0)) for match in TOKEN_RE.finditer(text)]


def prompt_text(body: Dict[str, Any]) -> str:
    if not isinstance(body, dict):
        raise InputError("request body must be a JSON object")
    if "messages" in body:
        messages = body.get("messages")
        if not isinstance(messages, list) or not messages:
            raise InputError("messages must be a non-empty list")
        parts = []
        for message in messages:
            if not isinstance(message, dict):
                raise InputError("every message must be an object with role and content")
            content = message.get("content")
            if isinstance(content, list):
                content = "".join(str(part.get("text", "")) for part in content if isinstance(part, dict))
            parts.append(f"<|{message.get('role', 'user')}|>{content or ''}<|end|>")
        return "".join(parts)
    if "prompt" in body:
        prompt = body["prompt"]
        return "".join(str(p) for p in prompt) if isinstance(prompt, list) else str(prompt)
    raise InputError("request needs messages (chat) or prompt (completions)")


def h64(data: bytes) -> int:
    return int.from_bytes(hashlib.blake2b(data, digest_size=8).digest(), "little")


def length_prefixed(text: str) -> bytes:
    raw = text.encode("utf-8")
    return struct.pack("<Q", len(raw)) + raw


def block_hashes(tokens: Sequence[int], block_size: int, model: str = "", salt: str = "", include_partial: bool = True) -> List[int]:
    if block_size <= 0:
        raise InputError(f"block size must be positive, got {block_size}")
    full, rest = divmod(len(tokens), block_size)
    count = full + (1 if include_partial and rest else 0)
    previous = h64(length_prefixed(model) + length_prefixed(salt))
    hashes = []
    for index in range(count):
        block = tokens[index * block_size:(index + 1) * block_size]
        content = h64(struct.pack(f"<{len(block)}I", *block))
        previous = h64(struct.pack("<QQ", content, previous))
        hashes.append(previous)
    return hashes


class PrefixIndex:
    def __init__(self, capacity: int = 31250):
        self.capacity = capacity
        self.blocks: Dict[str, "OrderedDict[int, None]"] = {}

    def longest_match(self, hashes: Sequence[int], endpoint: str) -> int:
        lru = self.blocks.get(endpoint)
        matched = 0
        for value in hashes:
            if not lru or value not in lru:
                break
            matched += 1
        return matched

    def record(self, hashes: Sequence[int], endpoint: str) -> int:
        lru = self.blocks.setdefault(endpoint, OrderedDict())
        for value in hashes:
            if value in lru:
                lru.move_to_end(value)
            else:
                lru[value] = None
        evicted = 0
        while len(lru) > self.capacity:
            lru.popitem(last=False)
            evicted += 1
        return evicted

    def size(self, endpoint: str) -> int:
        return len(self.blocks.get(endpoint) or ())


class BlockCache:
    def __init__(self, capacity_blocks: int, block_size: int = 16):
        self.capacity = capacity_blocks
        self.block_size = block_size
        self.blocks: "OrderedDict[int, None]" = OrderedDict()
        self.hit_tokens = 0
        self.query_tokens = 0
        self.lock = threading.Lock()

    def admit(self, tokens: Sequence[int], model: str = "", salt: str = "") -> int:
        hashes = block_hashes(tokens, self.block_size, model, salt, include_partial=False)
        usable = max(0, (len(tokens) - 1) // self.block_size)
        with self.lock:
            matched = 0
            for value in hashes[:usable]:
                if value not in self.blocks:
                    break
                matched += 1
            for value in hashes:
                if value in self.blocks:
                    self.blocks.move_to_end(value)
                else:
                    self.blocks[value] = None
            while len(self.blocks) > self.capacity:
                self.blocks.popitem(last=False)
            cached = matched * self.block_size
            self.hit_tokens += cached
            self.query_tokens += len(tokens)
        return cached


@dataclass
class Endpoint:
    name: str
    url: str = ""
    waiting: int = 0
    running: int = 0
    kv_usage: float = 0.0
    inflight: int = 0
    has_metrics: bool = False
    healthy: bool = True
    prefix_hits: float = 0.0
    prefix_queries: float = 0.0
    block_size: Optional[int] = None


def score_prefix(matches: Dict[str, int], total_blocks: int) -> Dict[str, float]:
    return {name: (matched / total_blocks if total_blocks else 0.0) for name, matched in matches.items()}


def score_queue(endpoints: Sequence[Endpoint]) -> Dict[str, float]:
    measured = [e for e in endpoints if e.has_metrics]
    if not measured:
        return {}
    low = min(e.waiting for e in measured)
    high = max(e.waiting for e in measured)
    if high == low:
        return {e.name: 1.0 for e in measured}
    return {e.name: (high - e.waiting) / (high - low) for e in measured}


def score_kv(endpoints: Sequence[Endpoint]) -> Dict[str, float]:
    return {e.name: 1.0 - min(1.0, max(0.0, e.kv_usage)) for e in endpoints if e.has_metrics}


def score_no_hit_lru(names: Sequence[str], cold: bool, cold_order: Sequence[str]) -> Dict[str, float]:
    if not cold:
        return {name: 0.5 for name in names}
    never = [name for name in names if name not in cold_order]
    used = [name for name in cold_order if name in names]
    total = len(never) + len(used)
    scores = {name: 1.0 for name in never}
    for index, name in enumerate(used):
        scores[name] = 1.0 - (len(never) + index) / total
    return scores


@dataclass
class Decision:
    endpoint: str
    policy: str
    total_blocks: int
    matches: Dict[str, int]
    scores: Dict[str, Dict[str, float]]
    totals: Dict[str, float]
    cold: bool
    hashes: List[int] = field(default_factory=list, repr=False)

    def describe(self) -> str:
        head = f"{self.policy} -> {self.endpoint}"
        if self.policy in ("round-robin", "random"):
            return head
        if self.policy == "least-queue":
            return f"{head} (load {-self.totals[self.endpoint]:.0f})"
        others = sorted(((total, name) for name, total in self.totals.items() if name != self.endpoint), reverse=True)
        runner = f", next {others[0][1]} {others[0][0]:.2f}" if others else ""
        state = "cold, no endpoint holds a block" if self.cold else f"match {self.matches.get(self.endpoint, 0)}/{self.total_blocks} blocks"
        return f"{head}: {state}, score {self.totals[self.endpoint]:.2f}{runner}"


class Router:
    def __init__(self, endpoints: Sequence[Endpoint], policy: str = "prefix", block_size: int = 16, weights: Optional[Dict[str, float]] = None, index_capacity: int = 31250, seed: int = 0):
        if policy not in POLICIES:
            raise InputError(f"unknown policy {policy!r}; choose one of {', '.join(POLICIES)}")
        if not endpoints:
            raise InputError("at least one endpoint is required")
        names = [e.name for e in endpoints]
        if len(set(names)) != len(names):
            raise InputError(f"endpoint names must be unique: {', '.join(names)}")
        for key in weights or {}:
            if key not in DEFAULT_WEIGHTS:
                raise InputError(f"unknown scorer {key!r}; scorers are {', '.join(DEFAULT_WEIGHTS)}")
        self.endpoints = {e.name: e for e in endpoints}
        self.order = names
        self.policy = policy
        self.block_size = block_size
        self.weights = dict(DEFAULT_WEIGHTS, **(weights or {}))
        self.index = PrefixIndex(index_capacity)
        self.rng = random.Random(seed)
        self.next_rr = 0
        self.cold_order: List[str] = []
        self.lock = threading.Lock()

    def candidates(self) -> List[Endpoint]:
        healthy = [self.endpoints[n] for n in self.order if self.endpoints[n].healthy]
        return healthy or [self.endpoints[n] for n in self.order]

    def best(self, totals: Dict[str, float]) -> str:
        top = max(totals.values())
        tied = [name for name in self.order if name in totals and abs(totals[name] - top) < 1e-9]
        return tied[0] if len(tied) == 1 else self.rng.choice(tied)

    def pick(self, tokens: Sequence[int], model: str = "", salt: str = "") -> Decision:
        hashes = block_hashes(tokens, self.block_size, model, salt)
        pool = self.candidates()
        names = [e.name for e in pool]
        matches = {name: self.index.longest_match(hashes, name) for name in names}
        cold = not any(matches.values())
        scores: Dict[str, Dict[str, float]] = {}
        if self.policy == "round-robin":
            chosen = names[self.next_rr % len(names)]
            self.next_rr += 1
            totals = {name: float(name == chosen) for name in names}
        elif self.policy == "random":
            chosen = self.rng.choice(names)
            totals = {name: float(name == chosen) for name in names}
        elif self.policy == "least-queue":
            totals = {e.name: -float(max(e.inflight, e.waiting + e.running)) for e in pool}
            scores["least-queue"] = dict(totals)
            chosen = self.best(totals)
        else:
            scores["prefix"] = score_prefix(matches, len(hashes))
            if self.policy == "prefix":
                scores["queue"] = score_queue(pool)
                scores["kv"] = score_kv(pool)
                scores["no-hit-lru"] = score_no_hit_lru(names, cold, self.cold_order)
            totals = {name: sum(self.weights[scorer] * values.get(name, 0.0) for scorer, values in scores.items()) for name in names}
            chosen = self.best(totals)
        return Decision(chosen, self.policy, len(hashes), matches, scores, totals, cold, hashes)

    def commit(self, decision: Decision) -> None:
        self.index.record(decision.hashes, decision.endpoint)
        if decision.cold:
            if decision.endpoint in self.cold_order:
                self.cold_order.remove(decision.endpoint)
            self.cold_order.append(decision.endpoint)


def parse_prometheus(text: str) -> List[Tuple[str, Dict[str, str], float]]:
    samples = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        match = SAMPLE_RE.match(line)
        if not match:
            continue
        try:
            value = float(match.group(3))
        except ValueError:
            continue
        samples.append((match.group(1), dict(LABEL_RE.findall(match.group(2) or "")), value))
    return samples


def server_metrics(text: str) -> Dict[str, Any]:
    samples = parse_prometheus(text)

    def total(name: str) -> float:
        return sum(value for metric, _, value in samples if metric == name)

    kv = [value for metric, _, value in samples if metric == METRIC_KV]
    block_sizes = [labels.get("block_size") for metric, labels, _ in samples if metric == METRIC_CACHE_CONFIG and labels.get("block_size")]
    found = sorted({metric for metric, _, _ in samples if metric.startswith("vllm:")})
    return {
        "waiting": int(total(METRIC_WAITING)),
        "running": int(total(METRIC_RUNNING)),
        "kv_usage": max(kv) if kv else 0.0,
        "prefix_hits": total(METRIC_HITS),
        "prefix_queries": total(METRIC_QUERIES),
        "block_size": int(block_sizes[0]) if block_sizes else None,
        "found": found,
    }


def apply_metrics(endpoint: Endpoint, metrics: Dict[str, Any]) -> None:
    endpoint.waiting = metrics["waiting"]
    endpoint.running = metrics["running"]
    endpoint.kv_usage = metrics["kv_usage"]
    endpoint.prefix_hits = metrics["prefix_hits"]
    endpoint.prefix_queries = metrics["prefix_queries"]
    endpoint.block_size = metrics["block_size"]
    endpoint.has_metrics = True
    endpoint.healthy = True


def percentile(values: Sequence[float], pct: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    rank = max(1, math.ceil(pct / 100 * len(ordered)))
    return float(ordered[rank - 1])


def latency_stats(values: Sequence[float]) -> Dict[str, float]:
    return {"p50": round(percentile(values, 50), 1), "p95": round(percentile(values, 95), 1), "mean": round(sum(values) / len(values), 1) if values else 0.0}


def group_spread(rows: Sequence[Dict[str, Any]]) -> Tuple[Dict[str, List[str]], int]:
    groups: Dict[str, set] = {}
    for row in rows:
        if row.get("group") and row.get("endpoint"):
            groups.setdefault(row["group"], set()).add(row["endpoint"])
    spread = {group: sorted(endpoints) for group, endpoints in sorted(groups.items())}
    return spread, sum(1 for endpoints in spread.values() if len(endpoints) == 1)


@dataclass
class TraceRequest:
    index: int
    arrival: float
    group: str
    body: Dict[str, Any]
    tokens: List[int]
    output_tokens: int
    path: str

    @property
    def model(self) -> str:
        return str(self.body.get("model", ""))

    @property
    def salt(self) -> str:
        return str(self.body.get("cache_salt") or "")


def parse_trace_line(raw: Dict[str, Any], index: int) -> TraceRequest:
    if not isinstance(raw, dict) or not isinstance(raw.get("body"), dict):
        raise InputError(f"trace line {index + 1}: needs a body object with messages or prompt")
    body = raw["body"]
    try:
        tokens = tokenize(prompt_text(body))
    except InputError as exc:
        raise InputError(f"trace line {index + 1}: {exc}")
    output = int(raw.get("output_tokens") or body.get("max_tokens") or 16)
    path = ROUTED_PATHS[0] if "messages" in body else ROUTED_PATHS[1]
    return TraceRequest(index, float(raw.get("t", 0.0)), str(raw.get("group", "")), body, tokens, output, path)


def load_trace(path: str) -> List[TraceRequest]:
    requests = []
    with open(path, encoding="utf-8") as handle:
        for number, line in enumerate(handle):
            if not line.strip():
                continue
            try:
                raw = json.loads(line)
            except ValueError as exc:
                raise InputError(f"{path} line {number + 1}: not JSON: {exc}")
            requests.append(parse_trace_line(raw, len(requests)))
    if not requests:
        raise InputError(f"{path}: the trace is empty")
    return requests


def make_trace(groups: int = 8, requests: int = 64, prefix_words: int = 180, suffix_words: int = 12, seed: int = 7, model: str = "cc-sim", max_tokens: int = 1, rate: float = 20.0, hot: float = 0.0) -> List[Dict[str, Any]]:
    if groups < 1 or requests < 1:
        raise InputError("groups and requests must be at least 1")
    rng = random.Random(seed)
    systems = [f"You are the support assistant for tenant {g}. Follow the runbook. " + " ".join(rng.choice(WORDS) for _ in range(prefix_words)) for g in range(groups)]
    lines = []
    arrival = 0.0
    for index in range(requests):
        group = 0 if hot and rng.random() < hot else rng.randrange(groups)
        if rate > 0 and index:
            arrival += rng.expovariate(rate)
        question = f"Question {index}: " + " ".join(rng.choice(WORDS) for _ in range(suffix_words)) + "?"
        body = {"model": model, "messages": [{"role": "system", "content": systems[group]}, {"role": "user", "content": question}], "max_tokens": max_tokens}
        lines.append({"t": round(arrival, 4), "group": f"tenant-{group}", "body": body})
    return lines


@dataclass
class SimConfig:
    replicas: int = 3
    cache_blocks: int = 64
    block_size: int = 16
    slots: int = 4
    prefill_tps: float = 1000.0
    prefill_overhead_ms: float = 5.0
    itl_ms: float = 1.0


class ReplicaSim:
    def __init__(self, name: str, config: SimConfig):
        self.name = name
        self.config = config
        self.cache = BlockCache(config.cache_blocks, config.block_size)
        self.lanes = [0.0] * max(1, config.slots)
        self.jobs: List[Tuple[float, float, int]] = []

    def snapshot(self, now: float) -> Tuple[int, int, float]:
        self.jobs = [job for job in self.jobs if job[1] > now]
        waiting = sum(1 for start, _, _ in self.jobs if start > now)
        running_blocks = [blocks for start, end, blocks in self.jobs if start <= now < end]
        usage = min(1.0, sum(running_blocks) / self.config.cache_blocks) if self.config.cache_blocks else 1.0
        return waiting, len(running_blocks), usage

    def admit(self, request: TraceRequest) -> Dict[str, float]:
        lane = min(range(len(self.lanes)), key=lambda i: self.lanes[i])
        start = max(request.arrival, self.lanes[lane])
        cached = self.cache.admit(request.tokens, request.model, request.salt)
        prefill = self.config.prefill_overhead_ms / 1000 + (len(request.tokens) - cached) / self.config.prefill_tps
        end = start + prefill + max(0, request.output_tokens - 1) * self.config.itl_ms / 1000
        self.lanes[lane] = end
        self.jobs.append((start, end, math.ceil(len(request.tokens) / self.config.block_size)))
        return {"cached": cached, "queue_ms": (start - request.arrival) * 1000, "ttft_ms": (start - request.arrival + prefill) * 1000}


def simulate(requests: Sequence[TraceRequest], policy: str, config: SimConfig, seed: int = 0, weights: Optional[Dict[str, float]] = None, keep_decisions: int = 0) -> Dict[str, Any]:
    if config.replicas < 1:
        raise InputError("replicas must be at least 1")
    replicas = [ReplicaSim(f"replica-{i + 1}", config) for i in range(config.replicas)]
    endpoints = [Endpoint(r.name, has_metrics=True) for r in replicas]
    router = Router(endpoints, policy, config.block_size, weights, seed=seed)
    rows = []
    decisions = []
    for request in sorted(requests, key=lambda r: (r.arrival, r.index)):
        for replica, endpoint in zip(replicas, endpoints):
            endpoint.waiting, endpoint.running, endpoint.kv_usage = replica.snapshot(request.arrival)
            endpoint.inflight = endpoint.waiting + endpoint.running
        decision = router.pick(request.tokens, request.model, request.salt)
        router.commit(decision)
        result = replicas[router.order.index(decision.endpoint)].admit(request)
        rows.append(dict(result, group=request.group, endpoint=decision.endpoint, prompt=len(request.tokens)))
        if len(decisions) < keep_decisions:
            decisions.append(f"#{request.index + 1} {request.group}: {decision.describe()}")
    spread, one = group_spread(rows)
    prompt = sum(row["prompt"] for row in rows)
    cached = sum(row["cached"] for row in rows)
    return {
        "policy": policy,
        "requests": len(rows),
        "prompt_tokens": prompt,
        "cached_tokens": int(cached),
        "hit_rate": round(cached / prompt, 4) if prompt else 0.0,
        "ttft_ms": latency_stats([row["ttft_ms"] for row in rows]),
        "queue_ms_p95": round(percentile([row["queue_ms"] for row in rows], 95), 1),
        "per_endpoint": {r.name: sum(1 for row in rows if row["endpoint"] == r.name) for r in replicas},
        "groups": spread,
        "groups_on_one_endpoint": one,
        "groups_total": len(spread),
        "decisions": decisions,
    }


@dataclass
class ModelShape:
    name: str
    layers: int
    kv_heads: int
    head_dim: int
    params_b: float
    dtype_bytes: int = 2

    def kv_bytes_per_token(self) -> int:
        return 2 * self.layers * self.kv_heads * self.head_dim * self.dtype_bytes

    def weights_bytes(self) -> float:
        return self.params_b * 1e9 * self.dtype_bytes


MODEL_SHAPES = {
    "llama-3.1-8b": ModelShape("llama-3.1-8b", 32, 8, 128, 8.03),
    "llama-3.1-70b": ModelShape("llama-3.1-70b", 80, 8, 128, 70.6),
    "qwen2.5-7b": ModelShape("qwen2.5-7b", 28, 4, 128, 7.62),
}


def kv_capacity_tokens(shape: ModelShape, gpu_memory_gb: float, utilization: float = 0.9, gpus: int = 1) -> int:
    budget = gpu_memory_gb * 1e9 * gpus * utilization - shape.weights_bytes()
    return max(0, int(budget // shape.kv_bytes_per_token()))


def disagg_plan(shape: ModelShape, prompt_tokens: int, cached_tokens: int = 0, prefill_tps: float = 12000.0, link_gbps: float = 100.0, link_latency_ms: float = 0.5, threshold_tokens: int = 256, chunk_tokens: int = 0) -> Dict[str, Any]:
    if prompt_tokens < 1 or not 0 <= cached_tokens <= prompt_tokens:
        raise InputError(f"need 0 <= cached tokens ({cached_tokens}) <= prompt tokens ({prompt_tokens}) and a positive prompt")
    if prefill_tps <= 0 or link_gbps <= 0:
        raise InputError("prefill tokens/s and link Gb/s must be positive")
    uncached = prompt_tokens - cached_tokens
    prefill_s = uncached / prefill_tps
    kv_bytes = uncached * shape.kv_bytes_per_token()
    transfer_s = link_latency_ms / 1000 + kv_bytes * 8 / (link_gbps * 1e9)
    stall_s = prefill_s if chunk_tokens <= 0 else min(prefill_s, chunk_tokens / prefill_tps)
    production_gbps = shape.kv_bytes_per_token() * prefill_tps * 8 / 1e9
    remote = threshold_tokens > 0 and uncached >= threshold_tokens
    return {
        "model": shape.name,
        "kv_bytes_per_token": shape.kv_bytes_per_token(),
        "uncached_tokens": uncached,
        "kv_transfer_mb": round(kv_bytes / 1e6, 1),
        "prefill_ms": round(prefill_s * 1000, 1),
        "transfer_ms": round(transfer_s * 1000, 1),
        "ttft_aggregated_ms": round(prefill_s * 1000, 1),
        "ttft_disaggregated_ms": round((prefill_s + transfer_s) * 1000, 1),
        "decode_stall_avoided_ms": round(stall_s * 1000, 1),
        "link_gbps": link_gbps,
        "kv_production_gbps": round(production_gbps, 1),
        "link_keeps_up": link_gbps >= production_gbps,
        "decider": "remote prefill" if remote else "local prefill",
    }


def validate_pool(pool: Dict[str, Any]) -> List[str]:
    spec = pool.get("spec") if isinstance(pool, dict) else None
    if not isinstance(spec, dict):
        return ["spec: Required value"]
    problems = []
    selector = spec.get("selector")
    labels = selector.get("matchLabels") if isinstance(selector, dict) else None
    if not isinstance(selector, dict):
        problems.append("spec.selector: Required value")
    elif not isinstance(labels, dict):
        problems.append("spec.selector.matchLabels: Required value")
    elif not labels:
        problems.append("spec.selector.matchLabels: Invalid value: 0: should have at least 1 properties")
    else:
        for key, value in sorted(labels.items()):
            if not isinstance(value, str) or len(value) > 63 or not LABEL_VALUE_RE.match(value):
                problems.append(f"spec.selector.matchLabels.{key}: Invalid value: {value!r}: not a valid label value")
    ports = spec.get("targetPorts")
    if not isinstance(ports, list):
        problems.append("spec.targetPorts: Required value")
    elif not 1 <= len(ports) <= 8:
        problems.append(f"spec.targetPorts: Invalid value: {len(ports)}: must have between 1 and 8 items")
    else:
        numbers = []
        for index, port in enumerate(ports):
            number = port.get("number") if isinstance(port, dict) else None
            if not isinstance(number, int) or isinstance(number, bool) or not 1 <= number <= 65535:
                problems.append(f"spec.targetPorts[{index}].number: Invalid value: {number!r}: must be between 1 and 65535")
            numbers.append(number)
        if len(set(map(repr, numbers))) != len(numbers):
            problems.append(f'spec.targetPorts: Invalid value: "array": {UNIQUE_PORT_MESSAGE}')
    ref = spec.get("endpointPickerRef")
    if ref is not None:
        if not isinstance(ref, dict) or not ref.get("name"):
            problems.append("spec.endpointPickerRef.name: Required value")
        ref = ref if isinstance(ref, dict) else {}
        if ref.get("kind", "Service") == "Service" and "port" not in ref:
            problems.append(f'spec.endpointPickerRef: Invalid value: "object": {SERVICE_PORT_MESSAGE}')
        if ref.get("failureMode", "FailClose") not in FAILURE_MODES:
            problems.append(f'spec.endpointPickerRef.failureMode: Unsupported value: "{ref.get("failureMode")}": supported values: "FailOpen", "FailClose"')
    app = spec.get("appProtocol", "http")
    if app not in APP_PROTOCOLS:
        problems.append(f'spec.appProtocol: Unsupported value: "{app}": supported values: "http", "kubernetes.io/h2c"')
    return problems


def pod_ready(pod: Dict[str, Any]) -> bool:
    status = pod.get("status") or {}
    if (pod.get("metadata") or {}).get("deletionTimestamp") or status.get("phase") != "Running" or not status.get("podIP"):
        return False
    return any(c.get("type") == "Ready" and c.get("status") == "True" for c in status.get("conditions") or [])


def pool_endpoints(pool: Dict[str, Any], pods: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    namespace = (pool.get("metadata") or {}).get("namespace") or "default"
    labels = pool["spec"]["selector"]["matchLabels"]
    ports = [p["number"] for p in pool["spec"]["targetPorts"]]
    result = []
    for pod in sorted(pods, key=lambda p: (p.get("metadata") or {}).get("name", "")):
        meta = pod.get("metadata") or {}
        if (meta.get("namespace") or "default") != namespace:
            continue
        if any((meta.get("labels") or {}).get(key) != value for key, value in labels.items()):
            continue
        ip = (pod.get("status") or {}).get("podIP") or ""
        for port in ports:
            name = meta.get("name", "") if len(ports) == 1 else f"{meta.get('name', '')}-{port}"
            result.append({"name": name, "pod": meta.get("name", ""), "ip": ip, "port": port, "ready": pod_ready(pod), "url": f"http://{ip}:{port}" if ip else ""})
    return result


def flatten(data: Any) -> List[Dict[str, Any]]:
    items = data if isinstance(data, list) else data.get("items") if isinstance(data, dict) and isinstance(data.get("items"), list) else [data]
    out: List[Dict[str, Any]] = []
    for item in items:
        if isinstance(item, dict) and isinstance(item.get("items"), list):
            out.extend(flatten(item))
        elif isinstance(item, dict):
            out.append(item)
    return out


def read_documents(paths: Sequence[str]) -> List[Dict[str, Any]]:
    docs: List[Dict[str, Any]] = []
    for path in paths:
        try:
            with open(path, encoding="utf-8") as handle:
                docs.extend(flatten(json.load(handle)))
        except ValueError as exc:
            raise InputError(f"{path}: not JSON (use kubectl get ... -o json): {exc}")
    return docs


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        return None


OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect)


def fetch(url: str, data: Optional[bytes] = None, headers: Optional[Dict[str, str]] = None, timeout: float = 30.0) -> Tuple[int, Dict[str, str], bytes]:
    request = urllib.request.Request(url, data=data, headers=headers or {}, method="POST" if data is not None else "GET")
    try:
        with OPENER.open(request, timeout=timeout) as response:
            return response.status, {k.lower(): v for k, v in response.getheaders()}, response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, {k.lower(): v for k, v in exc.headers.items()}, exc.read()


def metrics_url(url: str) -> str:
    return url if url.rstrip("/").endswith("/metrics") else url.rstrip("/") + "/metrics"


def scrape_server(url: str) -> Dict[str, Any]:
    status, _, raw = fetch(metrics_url(url), timeout=5.0)
    if status != 200:
        raise OSError(f"{metrics_url(url)} answered {status}")
    return server_metrics(raw.decode("utf-8", "replace"))


def extract_usage(raw: bytes) -> Optional[Dict[str, int]]:
    text = raw.decode("utf-8", "replace")
    usage = None
    for candidate in [text] + [line[5:].strip() for line in text.splitlines() if line.startswith("data:")]:
        try:
            doc = json.loads(candidate)
        except ValueError:
            continue
        if isinstance(doc, dict) and isinstance(doc.get("usage"), dict):
            usage = doc["usage"]
    if usage is None:
        return None
    details = usage.get("prompt_tokens_details") or {}
    return {"prompt_tokens": int(usage.get("prompt_tokens") or 0), "cached_tokens": int(details.get("cached_tokens") or 0)}


def parse_named(values: Sequence[str], flag: str) -> List[Tuple[str, str]]:
    pairs = []
    for value in values:
        name, sep, url = value.partition("=")
        if not sep:
            url = value
            name = urllib.parse.urlsplit(value).netloc or value
        if not url.startswith(("http://", "https://")):
            raise InputError(f"{flag} {value!r}: expected NAME=http://HOST:PORT")
        pairs.append((name, url.rstrip("/")))
    return pairs


class Scraper(threading.Thread):
    def __init__(self, router: Router, interval: float, scrape: Callable[[str], Dict[str, Any]] = scrape_server):
        super().__init__(daemon=True)
        self.router = router
        self.interval = interval
        self.scrape = scrape
        self.stopped = threading.Event()

    def scrape_once(self) -> None:
        for endpoint in list(self.router.endpoints.values()):
            try:
                metrics = self.scrape(endpoint.url)
            except (OSError, urllib.error.URLError, ValueError):
                with self.router.lock:
                    endpoint.healthy = False
                    endpoint.has_metrics = False
                continue
            with self.router.lock:
                apply_metrics(endpoint, metrics)

    def run(self) -> None:
        while not self.stopped.is_set():
            self.scrape_once()
            self.stopped.wait(self.interval)


class RouterServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address: Tuple[str, int], router: Router, verbose: bool = False, timeout: float = 120.0):
        super().__init__(address, RouterHandler)
        self.router = router
        self.verbose = verbose
        self.upstream_timeout = timeout
        self.stats = {name: {"requests": 0, "errors": 0, "matched_blocks": 0, "total_blocks": 0, "prompt_tokens": 0, "cached_tokens": 0} for name in router.order}
        self.decisions: List[Dict[str, Any]] = []
        self.lock = threading.Lock()

    def note(self, decision: Decision, status: int, usage: Optional[Dict[str, int]]) -> None:
        with self.lock:
            stats = self.stats[decision.endpoint]
            stats["requests"] += 1
            stats["errors"] += int(status != 200)
            stats["matched_blocks"] += decision.matches.get(decision.endpoint, 0)
            stats["total_blocks"] += decision.total_blocks
            if usage:
                stats["prompt_tokens"] += usage["prompt_tokens"]
                stats["cached_tokens"] += usage["cached_tokens"]
            self.decisions = (self.decisions + [{"endpoint": decision.endpoint, "status": status, "describe": decision.describe()}])[-50:]
        if self.verbose:
            print(f"{status} {decision.describe()}", file=sys.stderr, flush=True)

    def state(self) -> Dict[str, Any]:
        with self.router.lock, self.lock:
            endpoints = []
            for name in self.router.order:
                e = self.router.endpoints[name]
                endpoints.append(dict(self.stats[name], name=name, url=e.url, healthy=e.healthy, waiting=e.waiting, running=e.running, kv_usage=e.kv_usage, inflight=e.inflight, indexed_blocks=self.router.index.size(name), server_prefix_hits=e.prefix_hits, server_prefix_queries=e.prefix_queries))
            return {"policy": self.router.policy, "block_size": self.router.block_size, "weights": self.router.weights, "endpoints": endpoints, "decisions": list(self.decisions)}


class RouterHandler(BaseHTTPRequestHandler):
    server: RouterServer

    def log_message(self, fmt: str, *args: Any) -> None:
        return None

    def reply(self, status: int, payload: Any) -> None:
        body = json.dumps(payload, indent=2).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        path = self.path.split("?")[0]
        if path == "/healthz":
            return self.reply(200, {"status": "ok"})
        if path == "/prefixroute/state":
            return self.reply(200, self.server.state())
        if path == "/v1/models":
            endpoint = self.server.router.candidates()[0]
            try:
                status, _, raw = fetch(endpoint.url + path, timeout=10.0)
                return self.reply(status, json.loads(raw or b"{}"))
            except (OSError, urllib.error.URLError, ValueError) as exc:
                return self.reply(502, {"error": {"message": f"{endpoint.name}: {exc}", "type": "bad_gateway"}})
        return self.reply(404, {"error": {"message": f"no route for GET {path}"}})

    def do_POST(self) -> None:
        if self.path.split("?")[0] not in ROUTED_PATHS:
            return self.reply(404, {"error": {"message": f"prefixroute routes {', '.join(ROUTED_PATHS)}"}})
        raw = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        try:
            body = json.loads(raw or b"{}")
            tokens = tokenize(prompt_text(body))
        except (ValueError, InputError) as exc:
            return self.reply(400, {"error": {"message": str(exc), "type": "invalid_request_error"}})
        router = self.server.router
        with router.lock:
            decision = router.pick(tokens, str(body.get("model", "")), str(body.get("cache_salt") or ""))
            router.commit(decision)
            endpoint = router.endpoints[decision.endpoint]
            endpoint.inflight += 1
        try:
            self.forward(decision, endpoint, raw)
        finally:
            with router.lock:
                endpoint.inflight -= 1

    def forward(self, decision: Decision, endpoint: Endpoint, raw: bytes) -> None:
        headers = {k: v for k, v in self.headers.items() if k.lower() not in HOP_BY_HOP}
        request = urllib.request.Request(endpoint.url + self.path, data=raw, headers=headers, method="POST")
        try:
            response = OPENER.open(request, timeout=self.server.upstream_timeout)
        except urllib.error.HTTPError as exc:
            response = exc
        except (urllib.error.URLError, OSError) as exc:
            with self.server.router.lock:
                endpoint.healthy = False
            self.server.note(decision, 502, None)
            return self.reply(502, {"error": {"message": f"{endpoint.name} ({endpoint.url}): {exc}", "type": "bad_gateway"}})
        status = getattr(response, "status", None) or response.code
        self.send_response(status)
        for key, value in response.headers.items():
            if key.lower() not in HOP_BY_HOP:
                self.send_header(key, value)
        self.send_header("x-prefixroute-endpoint", decision.endpoint)
        self.send_header("x-prefixroute-policy", decision.policy)
        self.send_header("x-prefixroute-match", f"{decision.matches.get(decision.endpoint, 0)}/{decision.total_blocks}")
        self.end_headers()
        captured = bytearray()
        read = getattr(response, "read1", response.read)
        while True:
            chunk = read(8192)
            if not chunk:
                break
            self.wfile.write(chunk)
            self.wfile.flush()
            if len(captured) < 1_000_000:
                captured.extend(chunk)
        response.close()
        self.server.note(decision, status, extract_usage(bytes(captured)))


class FakeModelServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address: Tuple[str, int], name: str, model: str = "cc-sim", cache_blocks: int = 64, block_size: int = 16, prefill_overhead_ms: float = 5.0, prefill_ms_per_token: float = 0.0):
        super().__init__(address, FakeModelHandler)
        self.name = name
        self.model = model
        self.cache = BlockCache(cache_blocks, block_size)
        self.prefill_overhead_ms = prefill_overhead_ms
        self.prefill_ms_per_token = prefill_ms_per_token
        self.running = 0
        self.active_blocks = 0
        self.served = 0
        self.lock = threading.Lock()

    def metrics_text(self) -> str:
        with self.lock:
            usage = min(1.0, self.active_blocks / self.cache.capacity) if self.cache.capacity else 0.0
            running = self.running
        label = f'{{model_name="{self.model}"}}'
        return "\n".join([
            f"# TYPE {METRIC_RUNNING} gauge", f"{METRIC_RUNNING}{label} {running}",
            f"# TYPE {METRIC_WAITING} gauge", f"{METRIC_WAITING}{label} 0",
            f"# TYPE {METRIC_KV} gauge", f"{METRIC_KV}{label} {usage}",
            f"# TYPE {METRIC_HITS} counter", f"{METRIC_HITS}{label} {self.cache.hit_tokens}",
            f"# TYPE {METRIC_QUERIES} counter", f"{METRIC_QUERIES}{label} {self.cache.query_tokens}",
            f"# TYPE {METRIC_CACHE_CONFIG} gauge", f'{METRIC_CACHE_CONFIG}{{block_size="{self.cache.block_size}",num_gpu_blocks="{self.cache.capacity}"}} 1',
        ]) + "\n"


class FakeModelHandler(BaseHTTPRequestHandler):
    server: FakeModelServer

    def log_message(self, fmt: str, *args: Any) -> None:
        return None

    def reply(self, status: int, payload: Any, content_type: str = "application/json") -> None:
        body = payload.encode() if isinstance(payload, str) else json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("x-inference-pod", self.server.name)
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        if self.path == "/metrics":
            return self.reply(200, self.server.metrics_text(), "text/plain; version=0.0.4")
        if self.path in ("/health", "/v1/models"):
            return self.reply(200, {"object": "list", "data": [{"id": self.server.model, "object": "model"}]})
        return self.reply(404, {"error": {"message": f"no route for {self.path}"}})

    def do_POST(self) -> None:
        if self.path not in ROUTED_PATHS:
            return self.reply(404, {"error": {"message": f"no route for {self.path}"}})
        server = self.server
        try:
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
            tokens = tokenize(prompt_text(body))
        except (ValueError, InputError) as exc:
            return self.reply(400, {"error": {"message": str(exc), "type": "invalid_request_error"}})
        if body.get("model") != server.model:
            return self.reply(404, {"error": {"message": f"The model `{body.get('model')}` does not exist.", "type": "NotFoundError"}})
        cached = server.cache.admit(tokens, server.model, str(body.get("cache_salt") or ""))
        blocks = math.ceil(len(tokens) / server.cache.block_size)
        with server.lock:
            server.running += 1
            server.active_blocks += blocks
            server.served += 1
            number = server.served
        try:
            time.sleep((server.prefill_overhead_ms + (len(tokens) - cached) * server.prefill_ms_per_token) / 1000)
        finally:
            with server.lock:
                server.running -= 1
                server.active_blocks -= blocks
        usage = {"prompt_tokens": len(tokens), "completion_tokens": 1, "total_tokens": len(tokens) + 1, "prompt_tokens_details": {"cached_tokens": cached}}
        self.reply(200, {"id": f"cmpl-{server.name}-{number}", "object": "chat.completion", "model": server.model, "choices": [{"index": 0, "message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}], "usage": usage})


def send_request(base: str, path: str, body: Dict[str, Any], timeout: float) -> Tuple[int, Dict[str, str], bytes, float]:
    started = time.monotonic()
    try:
        status, headers, raw = fetch(base.rstrip("/") + path, json.dumps(body).encode(), {"Content-Type": "application/json"}, timeout)
    except (urllib.error.URLError, OSError) as exc:
        return 0, {}, str(exc).encode(), (time.monotonic() - started) * 1000
    return status, headers, raw, (time.monotonic() - started) * 1000


def wait_for_port(url: str, seconds: float) -> None:
    parts = urllib.parse.urlsplit(url)
    deadline = time.monotonic() + seconds
    while True:
        try:
            socket.create_connection((parts.hostname, parts.port or 80), timeout=1.0).close()
            return
        except OSError as exc:
            if time.monotonic() > deadline:
                raise OSError(f"{url} did not accept connections within {seconds:.0f}s: {exc}")
            time.sleep(0.2)


def summarize_replay(rows: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    ok = [row for row in rows if row["status"] == 200]
    spread, one = group_spread(ok)
    per_endpoint: Dict[str, int] = {}
    for row in ok:
        per_endpoint[row["endpoint"]] = per_endpoint.get(row["endpoint"], 0) + 1
    prompt = sum(row["prompt_tokens"] for row in ok)
    cached = sum(row["cached_tokens"] for row in ok)
    failed = [row for row in rows if row["status"] != 200]
    return {
        "requests": len(rows),
        "errors": len(failed),
        "first_error": failed[0]["error"] if failed else "",
        "per_endpoint": dict(sorted(per_endpoint.items())),
        "groups": spread,
        "groups_on_one_endpoint": one,
        "groups_total": len(spread),
        "prompt_tokens": prompt,
        "cached_tokens": cached,
        "client_hit_rate": round(cached / prompt, 4) if prompt else 0.0,
        "latency_ms": latency_stats([row["latency_ms"] for row in ok]),
    }


def replay(requests: Sequence[TraceRequest], url: str, concurrency: int = 1, timeout: float = 60.0, metrics: Sequence[Tuple[str, str]] = (), wait: float = 15.0, sender: Callable[..., Tuple[int, Dict[str, str], bytes, float]] = send_request, scraper: Callable[[str], Dict[str, Any]] = scrape_server) -> Dict[str, Any]:
    baseline = {name: scraper(target) for name, target in metrics}
    rows: List[Dict[str, Any]] = [{} for _ in requests]
    pending = list(range(len(requests)))
    lock = threading.Lock()

    def worker() -> None:
        while True:
            with lock:
                if not pending:
                    return
                index = pending.pop(0)
            request = requests[index]
            status, headers, raw, elapsed = sender(url, request.path, request.body, timeout)
            usage = extract_usage(raw) or {"prompt_tokens": 0, "cached_tokens": 0}
            endpoint = headers.get("x-prefixroute-endpoint") or headers.get("x-inference-pod") or "unknown"
            rows[index] = dict(usage, group=request.group, status=status, endpoint=endpoint, latency_ms=elapsed, error="" if status == 200 else f"{status}: {raw[:200].decode('utf-8', 'replace')}")

    threads = [threading.Thread(target=worker, daemon=True) for _ in range(max(1, concurrency))]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    summary = summarize_replay(rows)
    if metrics:
        deadline = time.monotonic() + wait
        while True:
            current = {name: scraper(target) for name, target in metrics}
            deltas = {name: {"hits": current[name]["prefix_hits"] - baseline[name]["prefix_hits"], "queries": current[name]["prefix_queries"] - baseline[name]["prefix_queries"]} for name, _ in metrics}
            queries = sum(d["queries"] for d in deltas.values())
            if queries >= summary["prompt_tokens"] or time.monotonic() > deadline:
                break
            time.sleep(0.2)
        hits = sum(d["hits"] for d in deltas.values())
        for d in deltas.values():
            d["hit_rate"] = round(d["hits"] / d["queries"], 4) if d["queries"] else 0.0
        summary["server"] = {"endpoints": deltas, "hits": hits, "queries": queries, "hit_rate": round(hits / queries, 4) if queries else 0.0, "synced": queries >= summary["prompt_tokens"]}
    return summary


def parse_weights(values: Sequence[str]) -> Dict[str, float]:
    weights = {}
    for value in values:
        name, sep, number = value.partition("=")
        try:
            weights[name] = float(number)
        except ValueError:
            sep = ""
        if not sep:
            raise InputError(f"--weight {value!r}: expected SCORER=NUMBER, scorers are {', '.join(DEFAULT_WEIGHTS)}")
    return weights


def render_simulation(reports: Sequence[Dict[str, Any]]) -> str:
    lines = [f"{'policy':<12} {'hit rate':>8} {'TTFT p50':>9} {'TTFT p95':>9} {'queue p95':>10} {'groups on 1 replica':>20}  requests per replica"]
    for r in reports:
        per = " ".join(str(count) for count in r["per_endpoint"].values())
        lines.append(f"{r['policy']:<12} {r['hit_rate'] * 100:>7.1f}% {r['ttft_ms']['p50']:>6.0f} ms {r['ttft_ms']['p95']:>6.0f} ms {r['queue_ms_p95']:>7.0f} ms {r['groups_on_one_endpoint']:>16}/{r['groups_total']:<3}  {per}")
    for r in reports:
        for line in r["decisions"]:
            lines.append(f"  {line}")
    return "\n".join(lines)


def render_disagg(plans: Sequence[Dict[str, Any]], capacity: int) -> str:
    first = plans[0]
    lines = [
        f"{first['model']}: {first['kv_bytes_per_token']} bytes of KV per token ({first['kv_bytes_per_token'] / 1024:.0f} KiB); one replica holds about {capacity} tokens of KV cache",
        f"prompt: {first['uncached_tokens']} uncached tokens = {first['kv_transfer_mb']} MB of KV; prefill {first['prefill_ms']} ms; the prefill GPU produces KV at {first['kv_production_gbps']} Gb/s",
        f"decider (nonCachedTokens threshold): {first['decider']}; a co-located prefill would stall each decode step by {first['decode_stall_avoided_ms']} ms",
        f"{'link':>9} {'transfer':>10} {'TTFT local':>11} {'TTFT remote':>12}  link keeps up",
    ]
    for p in plans:
        lines.append(f"{p['link_gbps']:>5.0f} Gb/s {p['transfer_ms']:>7.1f} ms {p['ttft_aggregated_ms']:>8.1f} ms {p['ttft_disaggregated_ms']:>9.1f} ms  {'yes' if p['link_keeps_up'] else 'no'}")
    return "\n".join(lines)


def render_pool(pool: Dict[str, Any], endpoints: Sequence[Dict[str, Any]]) -> str:
    meta = pool.get("metadata") or {}
    spec = pool["spec"]
    selector = ",".join(f"{k}={v}" for k, v in sorted(spec["selector"]["matchLabels"].items()))
    ports = ",".join(str(p["number"]) for p in spec["targetPorts"])
    ref = spec.get("endpointPickerRef")
    picker = f"{ref.get('kind', 'Service')}/{ref['name']}:{(ref.get('port') or {}).get('number', '-')} {ref.get('failureMode', 'FailClose')}" if ref else "none"
    lines = [f"InferencePool {meta.get('namespace', 'default')}/{meta.get('name')}: selector {selector}, targetPorts {ports}, endpointPickerRef {picker}"]
    for e in endpoints:
        lines.append(f"  {e['name']:<40} {e['ip'] or '(no IP)'}:{e['port']}  {'ready' if e['ready'] else 'not ready'}")
    lines.append(f"endpoints: {sum(1 for e in endpoints if e['ready'])} ready of {len(endpoints)}")
    return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="prefixroute", description="Prefix-cache-aware routing for OpenAI-compatible model servers.")
    sub = parser.add_subparsers(dest="command", required=True)
    trace = sub.add_parser("trace", help="generate a JSONL trace with shared system prompts")
    trace.add_argument("-o", "--output")
    trace.add_argument("--groups", type=int, default=8)
    trace.add_argument("--requests", type=int, default=64)
    trace.add_argument("--prefix-words", type=int, default=180)
    trace.add_argument("--suffix-words", type=int, default=12)
    trace.add_argument("--seed", type=int, default=7)
    trace.add_argument("--model", default="cc-sim")
    trace.add_argument("--max-tokens", type=int, default=1)
    trace.add_argument("--rate", type=float, default=20.0, help="mean arrivals per second (Poisson); 0 puts every request at t=0")
    trace.add_argument("--hot", type=float, default=0.0, help="fraction of requests sent to tenant-0")
    sim = sub.add_parser("simulate", help="replay a trace against simulated replicas under each routing policy")
    sim.add_argument("trace")
    sim.add_argument("--policy", default="all", choices=POLICIES + ("all",))
    sim.add_argument("--replicas", type=int, default=3)
    sim.add_argument("--cache-blocks", type=int, default=64)
    sim.add_argument("--block-size", type=int, default=16)
    sim.add_argument("--slots", type=int, default=4)
    sim.add_argument("--prefill-tps", type=float, default=1000.0)
    sim.add_argument("--prefill-overhead-ms", type=float, default=5.0)
    sim.add_argument("--itl-ms", type=float, default=1.0)
    sim.add_argument("--seed", type=int, default=0)
    sim.add_argument("--weight", action="append", default=[])
    sim.add_argument("--show-decisions", type=int, default=0)
    sim.add_argument("--json", action="store_true")
    dis = sub.add_parser("disagg", help="price prefill/decode disaggregation: KV bytes, transfer time, TTFT")
    dis.add_argument("--model", default="llama-3.1-8b", choices=sorted(MODEL_SHAPES))
    dis.add_argument("--prompt-tokens", type=int, default=4000)
    dis.add_argument("--cached-tokens", type=int, default=0)
    dis.add_argument("--prefill-tps", type=float, default=12000.0)
    dis.add_argument("--link-gbps", type=float, action="append")
    dis.add_argument("--link-latency-ms", type=float, default=0.5)
    dis.add_argument("--threshold", type=int, default=256, help="nonCachedTokens: remote prefill when at least this many tokens are not cached on the decode pod")
    dis.add_argument("--chunk-tokens", type=int, default=0, help="chunked prefill budget per step; 0 means one unchunked prefill")
    dis.add_argument("--gpu-memory-gb", type=float, default=80.0)
    dis.add_argument("--gpu-memory-utilization", type=float, default=0.9)
    dis.add_argument("--gpus", type=int, default=1, help="GPUs per replica (tensor parallel)")
    dis.add_argument("--json", action="store_true")
    serve = sub.add_parser("serve", help="run the router as an OpenAI-compatible proxy")
    serve.add_argument("--endpoint", action="append", default=[], required=True, help="NAME=http://HOST:PORT, once per replica")
    serve.add_argument("--policy", default="prefix", choices=POLICIES)
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8080)
    serve.add_argument("--block-size", type=int, default=16)
    serve.add_argument("--scrape-interval", type=float, default=0.5)
    serve.add_argument("--weight", action="append", default=[])
    serve.add_argument("--seed", type=int, default=0)
    serve.add_argument("--verbose", action="store_true")
    rep = sub.add_parser("replay", help="send a trace to a router or a server and summarise where it went")
    rep.add_argument("trace")
    rep.add_argument("--url", required=True)
    rep.add_argument("--concurrency", type=int, default=1)
    rep.add_argument("--metrics", action="append", default=[], help="NAME=http://HOST:PORT of a server whose /metrics to read before and after")
    rep.add_argument("--wait", type=float, default=15.0)
    rep.add_argument("--wait-ready", type=float, default=0.0)
    rep.add_argument("--timeout", type=float, default=60.0)
    rep.add_argument("--json", action="store_true")
    scrape = sub.add_parser("scrape", help="read vLLM-style /metrics from servers")
    scrape.add_argument("endpoints", nargs="+")
    scrape.add_argument("--json", action="store_true")
    pool = sub.add_parser("pool", help="validate an InferencePool and resolve its endpoints from Pods")
    pool.add_argument("files", nargs="+")
    pool.add_argument("--pool")
    pool.add_argument("--json", action="store_true")
    pool.add_argument("--args", action="store_true", help="print --endpoint flags for serve, ready endpoints only")
    fake = sub.add_parser("fake-server", help="a tiny vLLM-like server with a prefix cache and /metrics")
    fake.add_argument("--name", required=True)
    fake.add_argument("--host", default="127.0.0.1")
    fake.add_argument("--port", type=int, default=0)
    fake.add_argument("--model", default="cc-sim")
    fake.add_argument("--cache-blocks", type=int, default=64)
    fake.add_argument("--block-size", type=int, default=16)
    fake.add_argument("--prefill-overhead-ms", type=float, default=5.0)
    fake.add_argument("--prefill-ms-per-token", type=float, default=1.0)
    return parser


def run_forever(server: ThreadingHTTPServer) -> int:
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


def cmd_trace(args: argparse.Namespace) -> int:
    lines = make_trace(args.groups, args.requests, args.prefix_words, args.suffix_words, args.seed, args.model, args.max_tokens, args.rate, args.hot)
    text = "".join(json.dumps(line) + "\n" for line in lines)
    if args.output:
        with open(args.output, "w", encoding="utf-8") as handle:
            handle.write(text)
        print(f"wrote {len(lines)} requests in {args.groups} groups to {args.output}")
    else:
        sys.stdout.write(text)
    return 0


def cmd_simulate(args: argparse.Namespace) -> int:
    requests = load_trace(args.trace)
    config = SimConfig(args.replicas, args.cache_blocks, args.block_size, args.slots, args.prefill_tps, args.prefill_overhead_ms, args.itl_ms)
    policies = POLICIES if args.policy == "all" else (args.policy,)
    weights = parse_weights(args.weight)
    reports = [simulate(requests, policy, config, args.seed, weights, args.show_decisions) for policy in policies]
    print(json.dumps(reports, indent=2) if args.json else render_simulation(reports))
    return 0


def cmd_disagg(args: argparse.Namespace) -> int:
    shape = MODEL_SHAPES[args.model]
    plans = [disagg_plan(shape, args.prompt_tokens, args.cached_tokens, args.prefill_tps, gbps, args.link_latency_ms, args.threshold, args.chunk_tokens) for gbps in (args.link_gbps or [10.0, 25.0, 100.0, 400.0])]
    capacity = kv_capacity_tokens(shape, args.gpu_memory_gb, args.gpu_memory_utilization, args.gpus)
    print(json.dumps({"kv_capacity_tokens": capacity, "plans": plans}, indent=2) if args.json else render_disagg(plans, capacity))
    return 0


def cmd_serve(args: argparse.Namespace) -> int:
    endpoints = [Endpoint(name, url) for name, url in parse_named(args.endpoint, "--endpoint")]
    router = Router(endpoints, args.policy, args.block_size, parse_weights(args.weight), seed=args.seed)
    scraper = Scraper(router, args.scrape_interval)
    scraper.scrape_once()
    scraper.start()
    server = RouterServer((args.host, args.port), router, args.verbose)
    print(f"listening url=http://{args.host}:{server.server_address[1]} policy={args.policy} endpoints={','.join(router.order)}", flush=True)
    return run_forever(server)


def cmd_replay(args: argparse.Namespace) -> int:
    requests = load_trace(args.trace)
    if args.wait_ready:
        wait_for_port(args.url, args.wait_ready)
    summary = replay(requests, args.url, args.concurrency, args.timeout, parse_named(args.metrics, "--metrics"), args.wait)
    if args.json:
        print(json.dumps(summary, indent=2))
    else:
        print(f"{summary['requests']} requests, {summary['errors']} errors; latency p50 {summary['latency_ms']['p50']} ms p95 {summary['latency_ms']['p95']} ms")
        print(f"per endpoint: {summary['per_endpoint']}")
        print(f"groups on one endpoint: {summary['groups_on_one_endpoint']}/{summary['groups_total']}; cached prompt tokens (usage): {summary['client_hit_rate'] * 100:.1f}%")
        if "server" in summary:
            parts = ", ".join(f"{name} {d['hit_rate'] * 100:.1f}%" for name, d in summary["server"]["endpoints"].items())
            print(f"server prefix cache hit rate: {summary['server']['hit_rate'] * 100:.1f}% ({parts})")
        if summary["first_error"]:
            print(f"first error: {summary['first_error']}", file=sys.stderr)
    return 1 if summary["errors"] else 0


def cmd_scrape(args: argparse.Namespace) -> int:
    results = {name: scrape_server(url) for name, url in parse_named(args.endpoints, "endpoint")}
    if args.json:
        print(json.dumps(results, indent=2))
        return 0
    for name, m in results.items():
        rate = m["prefix_hits"] / m["prefix_queries"] * 100 if m["prefix_queries"] else 0.0
        print(f"{name}: waiting {m['waiting']} running {m['running']} kv {m['kv_usage']:.2f} prefix hits {m['prefix_hits']:.0f}/{m['prefix_queries']:.0f} tokens ({rate:.1f}%) block_size {m['block_size']}")
    return 0


def cmd_pool(args: argparse.Namespace) -> int:
    docs = read_documents(args.files)
    pools = [d for d in docs if d.get("kind") == "InferencePool" and (args.pool is None or (d.get("metadata") or {}).get("name") == args.pool)]
    if len(pools) != 1:
        raise InputError(f"expected exactly one InferencePool{' named ' + args.pool if args.pool else ''}, found {len(pools)}")
    problems = validate_pool(pools[0])
    if problems:
        for problem in problems:
            print(f"invalid: {problem}", file=sys.stderr)
        return 2
    endpoints = pool_endpoints(pools[0], [d for d in docs if d.get("kind") == "Pod"])
    if args.args:
        print(" ".join(f"--endpoint {e['name']}={e['url']}" for e in endpoints if e["ready"]))
    elif args.json:
        print(json.dumps({"pool": (pools[0].get("metadata") or {}).get("name"), "endpoints": endpoints}, indent=2))
    else:
        print(render_pool(pools[0], endpoints))
    return 0


def cmd_fake_server(args: argparse.Namespace) -> int:
    server = FakeModelServer((args.host, args.port), args.name, args.model, args.cache_blocks, args.block_size, args.prefill_overhead_ms, args.prefill_ms_per_token)
    print(f"fake-server {args.name} listening on http://{args.host}:{server.server_address[1]}", flush=True)
    return run_forever(server)


COMMANDS = {"trace": cmd_trace, "simulate": cmd_simulate, "disagg": cmd_disagg, "serve": cmd_serve, "replay": cmd_replay, "scrape": cmd_scrape, "pool": cmd_pool, "fake-server": cmd_fake_server}


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(list(sys.argv[1:] if argv is None else argv))
    try:
        return COMMANDS[args.command](args)
    except InputError as exc:
        print(f"prefixroute: {exc}", file=sys.stderr)
        return 2
    except OSError as exc:
        print(f"prefixroute: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
