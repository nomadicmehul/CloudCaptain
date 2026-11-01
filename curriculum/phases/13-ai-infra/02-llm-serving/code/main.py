from __future__ import annotations

import argparse
import http.client
import json
import math
import random
import re
import sys
import threading
import time
import urllib.error
import urllib.request
from collections import OrderedDict, deque
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any, Callable, Deque, Dict, Iterable, List, Optional, Sequence, Tuple

GIB = 1024 ** 3
DEFAULT_PROMPT = "Explain in one short paragraph what a KV cache is and why an LLM server needs one."
LLAMA_CPP_IMAGE = "ghcr.io/ggml-org/llama.cpp:server-b11706"
METRIC_LINE = re.compile(r"^([a-zA-Z_:][a-zA-Z0-9_:]*)(\{[^}]*\})?\s+(\S+)")


class InputError(Exception):
    pass


class CapacityError(Exception):
    pass


class ServerUnreachable(Exception):
    pass


@dataclass(frozen=True)
class Hardware:
    name: str
    memory_gib: float
    bandwidth_gbs: float
    tflops: float
    mbu: float
    mfu: float
    step_overhead_ms: float
    reserve_gib: float


@dataclass(frozen=True)
class ModelSpec:
    name: str
    params_b: float
    bytes_per_param: float
    layers: int
    kv_heads: int
    head_dim: int
    kv_dtype_bytes: int = 2

    @property
    def params(self) -> float:
        return self.params_b * 1e9

    @property
    def weight_bytes(self) -> float:
        return self.params * self.bytes_per_param

    @property
    def kv_bytes_per_token(self) -> int:
        return 2 * self.layers * self.kv_heads * self.head_dim * self.kv_dtype_bytes


HARDWARE: Dict[str, Hardware] = {
    "h100-sxm-80gb": Hardware("h100-sxm-80gb", 79.6, 3350, 989, 0.75, 0.5, 1.0, 2.0),
    "a100-sxm-80gb": Hardware("a100-sxm-80gb", 80.0, 2039, 312, 0.75, 0.5, 1.0, 2.0),
    "l4-24gb": Hardware("l4-24gb", 22.5, 300, 121, 0.75, 0.5, 1.0, 1.5),
    "cpu-4vcpu": Hardware("cpu-4vcpu", 16.0, 20, 0.1, 1.0, 1.0, 2.0, 0.0),
}

MODELS: Dict[str, ModelSpec] = {
    "llama-3.1-8b": ModelSpec("llama-3.1-8b", 8.03, 2.0, 32, 8, 128),
    "llama-3.1-70b": ModelSpec("llama-3.1-70b", 70.6, 2.0, 80, 8, 128),
    "smollm2-135m-q8_0": ModelSpec("smollm2-135m-q8_0", 0.1345, 34 / 32, 30, 3, 64),
}


def step_seconds(hardware: Hardware, model: ModelSpec, tokens: int, context_tokens: int) -> float:
    compute = 2 * model.params * tokens / (hardware.tflops * 1e12 * hardware.mfu)
    memory = (model.weight_bytes + context_tokens * model.kv_bytes_per_token) / (hardware.bandwidth_gbs * 1e9 * hardware.mbu)
    return hardware.step_overhead_ms / 1000 + max(compute, memory)


def critical_batch(hardware: Hardware, model: ModelSpec) -> float:
    weight_read = model.weight_bytes / (hardware.bandwidth_gbs * 1e9 * hardware.mbu)
    return weight_read * hardware.tflops * 1e12 * hardware.mfu / (2 * model.params)


@dataclass
class CapacityPlan:
    hardware: Hardware
    model: ModelSpec
    gpu_memory_utilization: float
    block_size: int
    budget_bytes: float
    kv_bytes: float
    num_blocks: int
    max_model_len: int
    fixed: bool = False

    @property
    def kv_tokens(self) -> int:
        return self.num_blocks * self.block_size

    @property
    def max_concurrency(self) -> float:
        return self.kv_tokens / self.max_model_len


def plan_capacity(hardware: Hardware, model: ModelSpec, gpu_memory_utilization: float = 0.9, block_size: int = 16, kv_tokens: Optional[int] = None, max_model_len: int = 4096) -> CapacityPlan:
    if not 0 < gpu_memory_utilization <= 1:
        raise InputError(f"--gpu-memory-utilization must be in (0, 1], got {gpu_memory_utilization}")
    if block_size < 1 or max_model_len < 1:
        raise InputError("--block-size and --max-model-len must be positive")
    budget = hardware.memory_gib * GIB * gpu_memory_utilization
    kv_bytes = budget - model.weight_bytes - hardware.reserve_gib * GIB
    if kv_tokens is not None:
        if kv_tokens < block_size:
            raise InputError(f"--kv-tokens {kv_tokens} is smaller than one block of {block_size} tokens")
        kv_bytes = float(kv_tokens * model.kv_bytes_per_token)
        num_blocks = kv_tokens // block_size
    else:
        if kv_bytes <= 0:
            raise CapacityError(
                f"{model.name} does not fit on {hardware.name}: weights {model.weight_bytes / 1e9:.2f} GB plus {hardware.reserve_gib} GiB reserve "
                f"exceed {budget / GIB:.2f} GiB ({hardware.memory_gib} GiB x {gpu_memory_utilization}); quantize, shard with tensor parallelism or pick a bigger device"
            )
        num_blocks = int(kv_bytes // (block_size * model.kv_bytes_per_token))
        if num_blocks < 1:
            raise CapacityError(f"{model.name} on {hardware.name} leaves room for no KV block of {block_size} tokens")
    return CapacityPlan(hardware, model, gpu_memory_utilization, block_size, budget, kv_bytes, num_blocks, max_model_len, kv_tokens is not None)


class BlockPool:
    def __init__(self, num_blocks: int):
        self.num_blocks = num_blocks
        self.free: "OrderedDict[int, None]" = OrderedDict((block, None) for block in range(num_blocks))
        self.refs: Dict[int, int] = {}
        self.cached: Dict[Tuple[str, int], int] = {}
        self.key_of: Dict[int, Tuple[str, int]] = {}
        self.evictions = 0

    def free_count(self) -> int:
        return len(self.free)

    def used(self) -> int:
        return self.num_blocks - len(self.free)

    def free_among(self, blocks: Sequence[int]) -> int:
        return sum(1 for block in blocks if block in self.free)

    def allocate(self, count: int) -> List[int]:
        if count > len(self.free):
            raise CapacityError(f"asked for {count} KV blocks, {len(self.free)} free")
        blocks = []
        for _ in range(count):
            block, _ = self.free.popitem(last=False)
            key = self.key_of.pop(block, None)
            if key is not None:
                del self.cached[key]
                self.evictions += 1
            self.refs[block] = 1
            blocks.append(block)
        return blocks

    def release(self, blocks: Sequence[int]) -> None:
        for block in reversed(blocks):
            self.refs[block] -= 1
            if self.refs[block] == 0:
                del self.refs[block]
                self.free[block] = None

    def lookup(self, keys: Sequence[Tuple[str, int]]) -> List[int]:
        hits = []
        for key in keys:
            block = self.cached.get(key)
            if block is None:
                break
            hits.append(block)
        return hits

    def touch(self, blocks: Sequence[int]) -> None:
        for block in blocks:
            self.free.pop(block, None)
            self.refs[block] = self.refs.get(block, 0) + 1

    def register(self, block: int, key: Tuple[str, int]) -> None:
        if key in self.cached or block in self.key_of:
            return
        self.cached[key] = block
        self.key_of[block] = key


@dataclass
class EngineConfig:
    max_num_seqs: int = 256
    max_num_batched_tokens: int = 8192
    block_size: int = 16
    chunked_prefill: bool = True
    prefix_caching: bool = False
    max_model_len: Optional[int] = None


@dataclass(eq=False)
class Request:
    rid: str
    arrival: float
    prompt_tokens: int
    output_tokens: int
    prefix: str = ""
    prefix_tokens: int = 0
    computed: int = 0
    generated: int = 0
    blocks: List[int] = field(default_factory=list)
    scheduled_at: Optional[float] = None
    token_times: List[float] = field(default_factory=list)
    finished_at: Optional[float] = None
    preemptions: int = 0
    cached_tokens: int = 0
    rejected: str = ""

    @property
    def length(self) -> int:
        return self.prompt_tokens + self.generated

    @property
    def remaining(self) -> int:
        return self.length - self.computed


@dataclass
class StepRecord:
    start: float
    seconds: float
    seqs: int
    tokens: int
    prefill_tokens: int
    decode_seqs: int
    kv_blocks_used: int


class Engine:
    def __init__(self, hardware: Hardware, model: ModelSpec, config: EngineConfig, num_blocks: int):
        if config.max_num_seqs < 1 or config.max_num_batched_tokens < 1:
            raise InputError("--max-num-seqs and --max-num-batched-tokens must be positive")
        self.hardware = hardware
        self.model = model
        self.config = config
        self.pool = BlockPool(num_blocks)
        self.waiting: Deque[Request] = deque()
        self.running: List[Request] = []
        self.rejected: List[Request] = []
        self.steps: List[StepRecord] = []
        self.finished_last: List[Request] = []
        self.now = 0.0
        self.preemptions = 0

    def blocks_for(self, tokens: int) -> int:
        return -(-tokens // self.config.block_size)

    def add(self, request: Request) -> bool:
        total = request.prompt_tokens + request.output_tokens
        limit = self.config.max_model_len
        if limit is not None and total > limit:
            request.rejected = f"prompt {request.prompt_tokens} + output {request.output_tokens} tokens exceeds max-model-len {limit}"
        elif self.blocks_for(total) > self.pool.num_blocks:
            request.rejected = f"needs {self.blocks_for(total)} KV blocks, the cache has {self.pool.num_blocks}"
        elif not self.config.chunked_prefill and total > self.config.max_num_batched_tokens:
            request.rejected = f"{total} tokens exceed max-num-batched-tokens {self.config.max_num_batched_tokens} with chunked prefill off"
        if request.rejected:
            self.rejected.append(request)
            return False
        self.waiting.append(request)
        return True

    def prefix_keys(self, request: Request) -> List[Tuple[str, int]]:
        if not (self.config.prefix_caching and request.prefix):
            return []
        full = min(request.prefix_tokens, request.length - 1) // self.config.block_size
        return [(request.prefix, index) for index in range(full)]

    def preempt(self, request: Request) -> None:
        self.pool.release(request.blocks)
        request.blocks = []
        request.computed = 0
        request.preemptions += 1
        self.preemptions += 1
        self.waiting.appendleft(request)

    def schedule(self) -> List[Tuple[Request, int]]:
        budget = self.config.max_num_batched_tokens
        batch: List[Tuple[Request, int]] = []
        preempted = False
        index = 0
        while index < len(self.running) and budget > 0:
            request = self.running[index]
            tokens = min(request.remaining, budget)
            need = self.blocks_for(request.computed + tokens) - len(request.blocks)
            while need > self.pool.free_count() and request in self.running:
                self.preempt(self.running.pop())
                preempted = True
            if request not in self.running:
                break
            request.blocks.extend(self.pool.allocate(need))
            batch.append((request, tokens))
            budget -= tokens
            index += 1
        if preempted:
            return batch
        while self.waiting and len(self.running) < self.config.max_num_seqs and budget > 0:
            request = self.waiting[0]
            hits = self.pool.lookup(self.prefix_keys(request))
            cached = len(hits) * self.config.block_size
            tokens = min(request.remaining - cached, budget)
            if not self.config.chunked_prefill and tokens < request.remaining - cached:
                break
            need = self.blocks_for(cached + tokens) - len(hits)
            if need > self.pool.free_count() - self.pool.free_among(hits):
                break
            self.waiting.popleft()
            self.pool.touch(hits)
            request.blocks = hits + self.pool.allocate(need)
            request.computed = cached
            request.cached_tokens += cached
            if request.scheduled_at is None:
                request.scheduled_at = self.now
            self.running.append(request)
            batch.append((request, tokens))
            budget -= tokens
        return batch

    def register_prefix(self, request: Request) -> None:
        for index, key in enumerate(self.prefix_keys(request)):
            if (index + 1) * self.config.block_size <= request.computed:
                self.pool.register(request.blocks[index], key)

    def step(self) -> Optional[StepRecord]:
        batch = self.schedule()
        self.finished_last = []
        if not batch:
            return None
        tokens = sum(n for _, n in batch)
        context = sum(request.computed + n for request, n in batch)
        decode = sum(1 for request, _ in batch if request.generated > 0 and request.remaining == 1)
        seconds = step_seconds(self.hardware, self.model, tokens, context)
        record = StepRecord(self.now, seconds, len(batch), tokens, tokens - decode, decode, self.pool.used())
        self.now += seconds
        for request, n in batch:
            request.computed += n
            self.register_prefix(request)
            if request.computed == request.length:
                request.generated += 1
                request.token_times.append(self.now)
                if request.generated >= request.output_tokens:
                    request.finished_at = self.now
                    self.finished_last.append(request)
        for request in self.finished_last:
            self.running.remove(request)
            self.pool.release(request.blocks)
            request.blocks = []
        self.steps.append(record)
        return record


def simulate(engine: Engine, requests: Sequence[Request], concurrency: Optional[int] = None, max_steps: int = 2_000_000) -> List[Request]:
    pending: Deque[Request] = deque(requests if concurrency else sorted(requests, key=lambda r: r.arrival))

    def release(count: int) -> None:
        accepted = 0
        while pending and accepted < count:
            request = pending.popleft()
            request.arrival = engine.now
            if engine.add(request):
                accepted += 1

    if concurrency:
        release(concurrency)
    steps = 0
    while engine.waiting or engine.running or pending:
        if not concurrency:
            while pending and pending[0].arrival <= engine.now:
                engine.add(pending.popleft())
        if not engine.waiting and not engine.running:
            if concurrency or not pending:
                break
            engine.now = pending[0].arrival
            continue
        if engine.step() is None:
            raise RuntimeError(f"scheduler stalled at t={engine.now:.3f}s with {len(engine.waiting)} waiting and {len(engine.running)} running")
        if concurrency:
            release(len(engine.finished_last))
        steps += 1
        if steps > max_steps:
            raise RuntimeError(f"no result after {max_steps} steps")
    return list(requests)


def parse_range(text: str, flag: str) -> Tuple[int, int]:
    match = re.fullmatch(r"\s*(\d+)\s*(?:-\s*(\d+)\s*)?", str(text))
    if not match:
        raise InputError(f"{flag} must be N or LO-HI (for example 512 or 256-1024), got {text!r}")
    low = int(match.group(1))
    high = int(match.group(2) or low)
    if low < 1 or high < low:
        raise InputError(f"{flag}: need 1 <= LO <= HI, got {text!r}")
    return low, high


def synthetic_workload(count: int, prompt: Tuple[int, int], output: Tuple[int, int], rate: Optional[float] = None, seed: int = 7, prefixes: int = 0, prefix_tokens: int = 0) -> List[Request]:
    if count < 1:
        raise InputError("--requests must be at least 1")
    if rate is not None and rate <= 0:
        raise InputError("--rate must be positive")
    rng = random.Random(seed)
    clock = 0.0
    requests = []
    for index in range(count):
        if rate:
            clock += rng.expovariate(rate)
        prompt_tokens = rng.randint(*prompt)
        prefix = f"system-{index % prefixes}" if prefixes else ""
        requests.append(Request(f"r{index + 1:04d}", clock, prompt_tokens, rng.randint(*output), prefix, min(prefix_tokens, prompt_tokens) if prefix else 0))
    return requests


def load_trace(text: str) -> List[Request]:
    stripped = text.strip()
    if stripped.startswith("["):
        try:
            items = json.loads(stripped)
        except json.JSONDecodeError as err:
            raise InputError(f"trace is not valid JSON: {err}") from None
        numbered = list(enumerate(items, 1))
    else:
        numbered = []
        for number, line in enumerate(text.splitlines(), 1):
            if not line.strip():
                continue
            try:
                numbered.append((number, json.loads(line)))
            except json.JSONDecodeError as err:
                raise InputError(f"trace line {number}: {err.msg}") from None
    requests = []
    for number, item in numbered:
        try:
            prompt_tokens = int(item["prompt_tokens"])
            output_tokens = int(item["output_tokens"])
            arrival = float(item.get("arrival", 0.0))
        except (KeyError, TypeError, ValueError):
            raise InputError(f"trace line {number}: needs prompt_tokens, output_tokens and a numeric arrival (seconds)") from None
        if prompt_tokens < 1 or output_tokens < 1 or arrival < 0:
            raise InputError(f"trace line {number}: tokens must be >= 1 and arrival >= 0")
        prefix = str(item.get("prefix") or "")
        requests.append(Request(str(item.get("id") or f"r{number:04d}"), arrival, prompt_tokens, output_tokens, prefix, min(int(item.get("prefix_tokens") or 0), prompt_tokens) if prefix else 0))
    if not requests:
        raise InputError("trace has no requests")
    return requests


@dataclass
class RequestRecord:
    rid: str
    sent: float
    token_times: List[float] = field(default_factory=list)
    finished: Optional[float] = None
    prompt_tokens: int = 0
    output_tokens: int = 0
    scheduled: Optional[float] = None
    usage_reported: bool = False
    error: str = ""


def records_from_requests(requests: Iterable[Request]) -> List[RequestRecord]:
    records = []
    for request in requests:
        error = request.rejected or ("" if request.finished_at is not None else "not finished")
        records.append(RequestRecord(request.rid, request.arrival, list(request.token_times), request.finished_at, request.prompt_tokens, request.generated, request.scheduled_at, True, error))
    return records


def percentile(values: Sequence[float], pct: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    rank = max(1, math.ceil(pct / 100 * len(ordered)))
    return float(ordered[rank - 1])


def distribution(values: Sequence[float]) -> Optional[Dict[str, float]]:
    if not values:
        return None
    return {"p50": percentile(values, 50), "p95": percentile(values, 95), "mean": sum(values) / len(values), "max": max(values)}


def summarize(records: Sequence[RequestRecord]) -> Dict[str, Any]:
    done = [r for r in records if not r.error and r.finished is not None and r.token_times]
    ttft = [(r.token_times[0] - r.sent) * 1000 for r in done]
    tpot = [(r.token_times[-1] - r.token_times[0]) * 1000 / (r.output_tokens - 1) for r in done if r.output_tokens > 1 and len(r.token_times) > 1]
    itl = [(later - earlier) * 1000 for r in done for earlier, later in zip(r.token_times, r.token_times[1:])]
    queue = [(r.scheduled - r.sent) * 1000 for r in done if r.scheduled is not None]
    e2e = [(r.finished - r.sent) * 1000 for r in done]
    output_tokens = sum(r.output_tokens for r in done)
    prompt_tokens = sum(r.prompt_tokens for r in done)
    span = (max(r.finished for r in done) - min(r.sent for r in records)) if done else 0.0
    rate = (lambda amount: amount / span if span > 0 else 0.0)
    return {
        "requests": len(records),
        "completed": len(done),
        "errors": [{"id": r.rid, "error": r.error} for r in records if r not in done],
        "duration_s": span,
        "prompt_tokens": prompt_tokens,
        "output_tokens": output_tokens,
        "ttft_ms": distribution(ttft),
        "tpot_ms": distribution(tpot),
        "itl_ms": distribution(itl),
        "queue_ms": distribution(queue),
        "e2e_ms": distribution(e2e),
        "throughput": {"output_tokens_per_s": rate(output_tokens), "total_tokens_per_s": rate(output_tokens + prompt_tokens), "requests_per_s": rate(len(done))},
    }


def engine_report(engine: Engine, plan: CapacityPlan, requests: Sequence[Request]) -> Dict[str, Any]:
    steps = engine.steps
    busy = sum(s.seconds for s in steps)
    prompt = sum(r.prompt_tokens for r in requests if not r.rejected)
    cached = sum(r.cached_tokens for r in requests)
    return {
        "hardware": engine.hardware.name,
        "model": engine.model.name,
        "kv_blocks": engine.pool.num_blocks,
        "block_size": engine.config.block_size,
        "kv_tokens": plan.kv_tokens,
        "max_num_seqs": engine.config.max_num_seqs,
        "max_num_batched_tokens": engine.config.max_num_batched_tokens,
        "chunked_prefill": engine.config.chunked_prefill,
        "prefix_caching": engine.config.prefix_caching,
        "steps": len(steps),
        "mean_batch_seqs": sum(s.seqs for s in steps) / len(steps) if steps else 0.0,
        "kv_peak_pct": 100 * max((s.kv_blocks_used for s in steps), default=0) / engine.pool.num_blocks,
        "kv_mean_pct": 100 * sum(s.kv_blocks_used * s.seconds for s in steps) / (busy * engine.pool.num_blocks) if busy else 0.0,
        "preemptions": engine.preemptions,
        "evictions": engine.pool.evictions,
        "prefix_hit_pct": 100 * cached / prompt if prompt else 0.0,
        "rejected": [{"id": r.rid, "reason": r.rejected} for r in engine.rejected],
    }


def run_scenario(args: argparse.Namespace, concurrency: Optional[int]) -> Dict[str, Any]:
    hardware, model = pick(HARDWARE, args.hardware, "--hardware"), pick(MODELS, args.model, "--model")
    plan = plan_capacity(hardware, model, args.gpu_memory_utilization, args.block_size, args.kv_tokens, args.max_model_len or 4096)
    config = EngineConfig(args.max_num_seqs, args.max_num_batched_tokens, args.block_size, not args.no_chunked_prefill, args.prefix_caching, args.max_model_len)
    engine = Engine(hardware, model, config, plan.num_blocks)
    if args.trace:
        requests = load_trace(read_text(args.trace))
    else:
        requests = synthetic_workload(args.requests, parse_range(args.prompt_tokens, "--prompt-tokens"), parse_range(args.output_tokens, "--output-tokens"), None if concurrency else args.rate, args.seed, args.prefixes, args.prefix_tokens)
    if not concurrency and not args.rate and not args.trace:
        concurrency = len(requests)
    simulate(engine, requests, concurrency)
    report = summarize(records_from_requests(requests))
    report["concurrency"] = concurrency
    report["rate"] = None if concurrency else args.rate
    report["engine"] = engine_report(engine, plan, requests)
    return report


def capacity_report(plan: CapacityPlan) -> Dict[str, Any]:
    hardware, model = plan.hardware, plan.model
    return {
        "hardware": hardware.name,
        "model": model.name,
        "weights_gb": model.weight_bytes / 1e9,
        "kv_bytes_per_token": model.kv_bytes_per_token,
        "budget_gib": plan.budget_bytes / GIB,
        "kv_cache_gib": plan.kv_bytes / GIB,
        "kv_blocks": plan.num_blocks,
        "block_size": plan.block_size,
        "kv_tokens": plan.kv_tokens,
        "max_model_len": plan.max_model_len,
        "max_concurrency": plan.max_concurrency,
        "decode_ms_batch1": step_seconds(hardware, model, 1, 1) * 1000,
        "critical_batch_tokens": critical_batch(hardware, model),
    }


def format_capacity(report: Dict[str, Any], plan: CapacityPlan) -> str:
    model, hardware = plan.model, plan.hardware
    return "\n".join([
        f"capacity  {model.name} on {hardware.name}  gpu-memory-utilization {plan.gpu_memory_utilization:.2f}",
        f"  weights          {report['weights_gb']:9.2f} GB   {model.params_b} B params x {model.bytes_per_param:.4g} bytes",
        f"  kv per token     {report['kv_bytes_per_token'] / 1024:9.1f} KiB  2 x {model.layers} layers x {model.kv_heads} kv heads x {model.head_dim} head dim x {model.kv_dtype_bytes} bytes",
        f"  memory budget    {report['budget_gib']:9.2f} GiB  {hardware.memory_gib} GiB x {plan.gpu_memory_utilization:.2f}",
        f"  kv cache         {report['kv_cache_gib']:9.2f} GiB  " + ("fixed by --kv-tokens" if plan.fixed else f"budget - weights - {hardware.reserve_gib} GiB reserve"),
        f"  blocks           {report['kv_blocks']:,} x {plan.block_size} tokens = {report['kv_tokens']:,} tokens",
        f"  max concurrency  {report['max_concurrency']:.2f}x at {plan.max_model_len:,} tokens per request",
        f"  decode step      {report['decode_ms_batch1']:.2f} ms at batch 1; compute catches up with the weight read at ~{report['critical_batch_tokens']:.0f} tokens per step",
    ])


def fmt_ms(dist: Optional[Dict[str, float]], key: str) -> str:
    return f"{dist[key]:10.1f}" if dist else f"{'-':>10}"


def format_latency(report: Dict[str, Any]) -> List[str]:
    lines = [f"  {'':14}{'p50':>10}{'p95':>10}"]
    for label, key in (("queue ms", "queue_ms"), ("ttft ms", "ttft_ms"), ("tpot ms", "tpot_ms"), ("itl ms", "itl_ms"), ("e2e ms", "e2e_ms")):
        if key == "queue_ms" and report.get(key) is None:
            continue
        lines.append(f"  {label:14}{fmt_ms(report.get(key), 'p50')}{fmt_ms(report.get(key), 'p95')}")
    t = report["throughput"]
    lines.append(f"  throughput    {t['output_tokens_per_s']:.1f} output tok/s   {t['total_tokens_per_s']:.1f} total tok/s   {t['requests_per_s']:.2f} req/s")
    return lines


def format_simulation(report: Dict[str, Any]) -> str:
    e = report["engine"]
    shape = f"concurrency {report['concurrency']}" if report["concurrency"] else f"Poisson {report['rate']} req/s" if report["rate"] else "trace arrivals"
    lines = [
        f"servesim  {e['model']} on {e['hardware']}  {report['requests']} requests  {shape}",
        f"  kv cache      {e['kv_blocks']:,} blocks x {e['block_size']} tokens   peak {e['kv_peak_pct']:.1f}%   mean {e['kv_mean_pct']:.1f}%",
        f"  scheduler     max-num-seqs {e['max_num_seqs']}   max-num-batched-tokens {e['max_num_batched_tokens']}   chunked prefill {'on' if e['chunked_prefill'] else 'off'}   prefix caching {'on' if e['prefix_caching'] else 'off'}",
        f"  steps         {e['steps']:,}   mean batch {e['mean_batch_seqs']:.1f} seqs   preemptions {e['preemptions']}   evictions {e['evictions']}   prefix hits {e['prefix_hit_pct']:.1f}%",
    ]
    lines += format_latency(report)
    for item in e["rejected"]:
        lines.append(f"  rejected      {item['id']}: {item['reason']}")
    return "\n".join(lines)


def format_sweep(rows: Sequence[Dict[str, Any]]) -> str:
    lines = [f"  {'concurrency':>11} {'out tok/s':>10} {'ttft p50':>9} {'tpot p50':>9} {'tpot p95':>9} {'queue p95':>10} {'batch':>6} {'kv peak':>8} {'preempt':>8}"]
    for row in rows:
        e = row["engine"]
        queue = row["queue_ms"]["p95"] if row["queue_ms"] else 0.0
        lines.append(
            f"  {row['concurrency']:>11} {row['throughput']['output_tokens_per_s']:>10.1f} {fmt_ms(row['ttft_ms'], 'p50')[1:]:>9} {fmt_ms(row['tpot_ms'], 'p50')[1:]:>9} "
            f"{fmt_ms(row['tpot_ms'], 'p95')[1:]:>9} {queue:>10.1f} {e['mean_batch_seqs']:>6.1f} {e['kv_peak_pct']:>7.1f}% {e['preemptions']:>8}"
        )
    return "\n".join(lines)


def sse_data(line: str) -> Optional[str]:
    text = line.strip()
    if not text.startswith("data:"):
        return None
    return text[5:].strip()


def record_from_stream(rid: str, sent: float, lines: Iterable[Tuple[float, str]]) -> RequestRecord:
    record = RequestRecord(rid, sent)
    last = sent
    complete = False
    for at, line in lines:
        last = at
        data = sse_data(line)
        if data is None or not data:
            continue
        if data == "[DONE]":
            complete = True
            break
        try:
            event = json.loads(data)
        except json.JSONDecodeError:
            record.error = f"stream sent data that is not JSON: {data[:120]}"
            break
        if "error" in event:
            detail = event["error"]
            record.error = str(detail.get("message") if isinstance(detail, dict) else detail)
            break
        for choice in event.get("choices") or []:
            delta = choice.get("delta") or {}
            if delta.get("content") or delta.get("reasoning_content"):
                record.token_times.append(at)
            if choice.get("finish_reason"):
                complete = True
        usage = event.get("usage")
        if usage:
            record.prompt_tokens = int(usage.get("prompt_tokens") or 0)
            record.output_tokens = int(usage.get("completion_tokens") or 0)
            record.usage_reported = True
    record.finished = last
    if not record.usage_reported:
        record.output_tokens = len(record.token_times)
    if not record.error and not record.token_times:
        record.error = "stream ended without any content"
    if not record.error and not complete:
        record.error = "stream ended before a finish_reason or [DONE]"
    return record


def chat_body(model: str, prompt: str, max_tokens: int, ignore_eos: bool) -> Dict[str, Any]:
    body: Dict[str, Any] = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": max_tokens,
        "temperature": 0,
        "stream": True,
        "stream_options": {"include_usage": True},
    }
    if ignore_eos:
        body["ignore_eos"] = True
    return body


def default_opener() -> urllib.request.OpenerDirector:
    return urllib.request.build_opener(urllib.request.ProxyHandler({}))


def stream_chat(base_url: str, body: Dict[str, Any], rid: str, timeout: float = 120.0, clock: Callable[[], float] = time.perf_counter, opener: Optional[urllib.request.OpenerDirector] = None) -> RequestRecord:
    opener = opener or default_opener()
    request = urllib.request.Request(base_url.rstrip("/") + "/v1/chat/completions", data=json.dumps(body).encode("utf-8"), headers={"content-type": "application/json", "accept": "text/event-stream"}, method="POST")
    sent = clock()
    try:
        response = opener.open(request, timeout=timeout)
    except urllib.error.HTTPError as err:
        return RequestRecord(rid, sent, finished=clock(), error=f"HTTP {err.code}: {err.read()[:200].decode('utf-8', 'replace')}")
    except urllib.error.URLError as err:
        if isinstance(err.reason, ConnectionRefusedError):
            raise ServerUnreachable(f"{base_url}: {err.reason}") from None
        return RequestRecord(rid, sent, finished=clock(), error=f"connection failed: {err.reason}")
    except OSError as err:
        return RequestRecord(rid, sent, finished=clock(), error=f"connection failed: {err}")
    try:
        with response:
            return record_from_stream(rid, sent, ((clock(), raw.decode("utf-8", "replace")) for raw in response))
    except (OSError, http.client.HTTPException) as err:
        return RequestRecord(rid, sent, finished=clock(), error=f"stream interrupted: {err!r}")


def parse_prometheus(text: str) -> Dict[str, float]:
    values: Dict[str, float] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        match = METRIC_LINE.match(line)
        if not match:
            continue
        try:
            number = float(match.group(3))
        except ValueError:
            continue
        values[match.group(1)] = values.get(match.group(1), 0.0) + number
    return values


def scrape(base_url: str, opener: Optional[urllib.request.OpenerDirector] = None, timeout: float = 5.0) -> Optional[Dict[str, float]]:
    opener = opener or default_opener()
    try:
        with opener.open(base_url.rstrip("/") + "/metrics", timeout=timeout) as response:
            return parse_prometheus(response.read().decode("utf-8", "replace"))
    except (urllib.error.URLError, OSError, ValueError):
        return None


SERVER_COUNTERS = {
    "prompt_tokens": ("llamacpp:prompt_tokens_total", "vllm:prompt_tokens_total"),
    "generated_tokens": ("llamacpp:tokens_predicted_total", "vllm:generation_tokens_total"),
    "decode_calls": ("llamacpp:n_decode_total",),
    "preemptions": ("vllm:num_preemptions_total",),
}
SERVER_GAUGES = {
    "running": ("llamacpp:requests_processing", "vllm:num_requests_running"),
    "waiting": ("llamacpp:requests_deferred", "vllm:num_requests_waiting"),
    "kv_usage": ("vllm:kv_cache_usage_perc",),
}


def server_window(before: Optional[Dict[str, float]], after: Optional[Dict[str, float]], samples: Sequence[Dict[str, float]] = ()) -> Optional[Dict[str, Any]]:
    if before is None or after is None:
        return None
    names = " ".join(after)
    window: Dict[str, Any] = {"implementation": "llama.cpp" if "llamacpp:" in names else "vllm" if "vllm:" in names else "unknown"}
    for key, candidates in SERVER_COUNTERS.items():
        for name in candidates:
            if name in before and name in after:
                window[key] = after[name] - before[name]
                break
    for key, candidates in SERVER_GAUGES.items():
        for name in candidates:
            seen = [s[name] for s in list(samples) + [after] if name in s]
            if seen:
                window[f"max_{key}"] = max(seen)
                break
    busy, decodes = "llamacpp:n_busy_slots_per_decode", "llamacpp:n_decode_total"
    if all(name in snapshot for name in (busy, decodes) for snapshot in (before, after)) and after[decodes] > before[decodes]:
        window["mean_batch_per_decode"] = (after[busy] * after[decodes] - before[busy] * before[decodes]) / (after[decodes] - before[decodes])
    return window


def first_model(base_url: str, opener: Optional[urllib.request.OpenerDirector] = None) -> str:
    opener = opener or default_opener()
    try:
        with opener.open(base_url.rstrip("/") + "/v1/models", timeout=10) as response:
            data = json.loads(response.read().decode("utf-8")).get("data") or []
    except urllib.error.HTTPError:
        return "default"
    except (urllib.error.URLError, OSError) as err:
        raise ServerUnreachable(f"{base_url}: {getattr(err, 'reason', err)}") from None
    except ValueError:
        return "default"
    return str(data[0].get("id")) if data else "default"


def run_load(base_url: str, prompts: Sequence[str], concurrency: int, max_tokens: int, model: Optional[str] = None, ignore_eos: bool = False, timeout: float = 120.0, sample_interval: float = 0.2, clock: Callable[[], float] = time.perf_counter, opener: Optional[urllib.request.OpenerDirector] = None) -> Dict[str, Any]:
    if concurrency < 1 or max_tokens < 1 or not prompts:
        raise InputError("--concurrency, --max-tokens and --requests must be at least 1")
    opener = opener or default_opener()
    model = model or first_model(base_url, opener)
    before = scrape(base_url, opener)
    samples: List[Dict[str, float]] = []
    stop = threading.Event()

    def sample() -> None:
        while not stop.wait(sample_interval):
            snapshot = scrape(base_url, opener)
            if snapshot is not None:
                samples.append(snapshot)

    sampler = threading.Thread(target=sample, daemon=True)
    if before is not None:
        sampler.start()
    lock = threading.Lock()
    cursor = [0]

    def worker() -> List[RequestRecord]:
        done = []
        while True:
            with lock:
                if cursor[0] >= len(prompts):
                    return done
                index = cursor[0]
                cursor[0] += 1
            done.append(stream_chat(base_url, chat_body(model, prompts[index], max_tokens, ignore_eos), f"r{index + 1:04d}", timeout, clock, opener))

    try:
        with ThreadPoolExecutor(max_workers=concurrency) as pool:
            futures = [pool.submit(worker) for _ in range(concurrency)]
            records = [record for future in futures for record in future.result()]
    finally:
        stop.set()
        if sampler.is_alive():
            sampler.join()
    records.sort(key=lambda r: r.rid)
    report = summarize(records)
    report.update({"url": base_url, "model": model, "concurrency": concurrency, "max_tokens": max_tokens, "usage_reported": all(r.usage_reported for r in records)})
    report["server"] = server_window(before, scrape(base_url, opener), samples)
    return report


def format_loadgen(report: Dict[str, Any]) -> str:
    done = max(report["completed"], 1)
    lines = [
        f"loadgen  {report['url']}  model {report['model']}  {report['requests']} requests  concurrency {report['concurrency']}  max_tokens {report['max_tokens']}",
        f"  completed {report['completed']}/{report['requests']}   errors {len(report['errors'])}   prompt {report['prompt_tokens'] / done:.0f} tok/request   output {report['output_tokens'] / done:.0f} tok/request ({'usage' if report['usage_reported'] else 'counted chunks'})",
    ]
    lines += format_latency(report)
    server = report.get("server")
    if server is None:
        lines.append("  server        no /metrics (llama-server needs --metrics; vLLM serves it by default)")
    else:
        parts = [f"{key.replace('_', ' ')} {value:g}" if isinstance(value, float) else f"{key.replace('_', ' ')} {value}" for key, value in server.items() if key != "implementation"]
        lines.append(f"  server        {server['implementation']} /metrics: " + "   ".join(parts))
    for item in report["errors"][:5]:
        lines.append(f"  error         {item['id']}: {item['error']}")
    return "\n".join(lines)


def pick(table: Dict[str, Any], name: str, flag: str) -> Any:
    if name not in table:
        raise InputError(f"{flag} {name!r} is not a preset; choose one of {', '.join(sorted(table))}")
    return table[name]


def read_text(path: str) -> str:
    try:
        if path == "-":
            return sys.stdin.read()
        with open(path, encoding="utf-8") as handle:
            return handle.read()
    except OSError as err:
        raise InputError(f"cannot read {path}: {err.strerror}") from None


def parse_concurrency_list(text: str) -> List[int]:
    try:
        values = [int(part) for part in str(text).split(",") if part.strip()]
    except ValueError:
        raise InputError(f"--concurrency must be N or a comma-separated list such as 1,4,16, got {text!r}") from None
    if not values or any(value < 1 for value in values):
        raise InputError(f"--concurrency values must be >= 1, got {text!r}")
    return values


def rounded(value: Any) -> Any:
    if isinstance(value, float):
        return round(value, 3)
    if isinstance(value, dict):
        return {key: rounded(item) for key, item in value.items()}
    if isinstance(value, list):
        return [rounded(item) for item in value]
    return value


def add_engine_flags(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--hardware", default="h100-sxm-80gb", help=f"one of {', '.join(HARDWARE)}")
    parser.add_argument("--model", default="llama-3.1-8b", help=f"one of {', '.join(MODELS)}")
    parser.add_argument("--gpu-memory-utilization", type=float, default=0.9)
    parser.add_argument("--block-size", type=int, default=16)
    parser.add_argument("--kv-tokens", type=int, help="fix the KV cache size in tokens (llama.cpp --ctx-size) instead of deriving it from memory")
    parser.add_argument("--max-model-len", type=int)
    parser.add_argument("--json", action="store_true")


def add_scheduler_flags(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--max-num-seqs", type=int, default=256, help="sequences in one batch (llama.cpp --parallel)")
    parser.add_argument("--max-num-batched-tokens", type=int, default=8192, help="token budget per step")
    parser.add_argument("--no-chunked-prefill", action="store_true")
    parser.add_argument("--prefix-caching", action="store_true")
    parser.add_argument("--requests", type=int, default=64)
    parser.add_argument("--prompt-tokens", default="256-1024", help="N or LO-HI")
    parser.add_argument("--output-tokens", default="64-256", help="N or LO-HI")
    parser.add_argument("--rate", type=float, help="open loop: Poisson arrivals per second")
    parser.add_argument("--prefixes", type=int, default=0, help="number of distinct shared system prompts")
    parser.add_argument("--prefix-tokens", type=int, default=0, help="length of each shared system prompt")
    parser.add_argument("--trace", help="JSONL or JSON list of {arrival, prompt_tokens, output_tokens, prefix, prefix_tokens}")
    parser.add_argument("--seed", type=int, default=7)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="servesim", description="Token-level LLM serving simulator and a load generator for OpenAI-compatible servers.")
    sub = parser.add_subparsers(dest="command", required=True)
    capacity = sub.add_parser("capacity", help="weights, KV cache blocks and maximum concurrency for a model on a device")
    add_engine_flags(capacity)
    run = sub.add_parser("simulate", help="run the continuous-batching simulator on a workload")
    add_engine_flags(run)
    add_scheduler_flags(run)
    run.add_argument("--concurrency", help="closed loop: N clients that send the next request when the previous one ends")
    sweep = sub.add_parser("sweep", help="simulate the same workload at several concurrencies")
    add_engine_flags(sweep)
    add_scheduler_flags(sweep)
    sweep.add_argument("--concurrency", default="1,2,4,8,16,32,64")
    load = sub.add_parser("loadgen", help="stream requests to a real OpenAI-compatible server and measure the same metrics")
    load.add_argument("--url", default="http://127.0.0.1:8080")
    load.add_argument("--model", help="model name to send (default: the first id from /v1/models)")
    load.add_argument("--concurrency", type=int, default=1)
    load.add_argument("--requests", type=int, default=8)
    load.add_argument("--max-tokens", type=int, default=64)
    load.add_argument("--prompt", default=DEFAULT_PROMPT)
    load.add_argument("--ignore-eos", action="store_true", help="ask the server to generate exactly max_tokens (llama.cpp and vLLM accept it)")
    load.add_argument("--timeout", type=float, default=120.0)
    load.add_argument("--json", action="store_true")
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    try:
        args = parser.parse_args(list(sys.argv[1:] if argv is None else argv))
    except SystemExit as exit_:
        return int(exit_.code or 0)
    try:
        if args.command == "capacity":
            plan = plan_capacity(pick(HARDWARE, args.hardware, "--hardware"), pick(MODELS, args.model, "--model"), args.gpu_memory_utilization, args.block_size, args.kv_tokens, args.max_model_len or 4096)
            report = capacity_report(plan)
            print(json.dumps(rounded(report), indent=2) if args.json else format_capacity(report, plan))
            return 0
        if args.command == "simulate":
            concurrency = parse_concurrency_list(args.concurrency)[0] if args.concurrency else None
            report = run_scenario(args, concurrency)
            print(json.dumps(rounded(report), indent=2) if args.json else format_simulation(report))
            return 0 if report["completed"] else 1
        if args.command == "sweep":
            rows = [run_scenario(args, value) for value in parse_concurrency_list(args.concurrency)]
            if args.json:
                print(json.dumps(rounded(rows), indent=2))
            else:
                first = rows[0]["engine"]
                print(f"sweep  {first['model']} on {first['hardware']}  {args.requests} requests  prompt {args.prompt_tokens}  output {args.output_tokens}")
                print(format_sweep(rows))
            return 0
        prompts = [f"{args.prompt} (request {index + 1})" for index in range(max(args.requests, 0))]
        report = run_load(args.url, prompts, args.concurrency, args.max_tokens, args.model, args.ignore_eos, args.timeout)
        print(json.dumps(rounded(report), indent=2) if args.json else format_loadgen(report))
        return 0 if not report["errors"] else 1
    except InputError as err:
        print(f"servesim: {err}", file=sys.stderr)
        return 2
    except CapacityError as err:
        print(f"servesim: {err}", file=sys.stderr)
        return 1
    except ServerUnreachable as err:
        print(f"servesim: cannot reach {err}. Start a server first, for example:\n  docker run --rm -d -p 8080:8080 -v $PWD/models:/models {LLAMA_CPP_IMAGE} -m /models/MODEL.gguf --parallel 4 --ctx-size 4096 --metrics", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
