"""tokencost: turn dated, sourced prices, measured or estimated throughput and a day of traffic into replicas, utilisation, cost per million input and output tokens, and the break-even volume against API prices."""
from __future__ import annotations

import argparse
import csv
import json
import math
import statistics
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field, replace
from datetime import date, datetime, timedelta, timezone
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

BITS_PER_WEIGHT = {"F32": 32.0, "F16": 16.0, "BF16": 16.0, "Q8_0": 8.5, "Q6_K": 6.5625, "Q5_K": 5.5, "Q4_K": 4.5, "Q4_0": 4.5}
SECONDS_PER_HOUR = 3600
HOURS_PER_DAY = 24
DEFAULT_MAX_AGE_DAYS = 90
SWEEP = (0.25, 0.5, 1.0, 2.0, 4.0, 8.0)
SCAN_LOW, SCAN_HIGH = 1e-3, 1e4
IDLE = "__idle__"
UNALLOCATED = "__unallocated__"
TERMINAL_PHASES = ("Succeeded", "Failed")
FOCUS_COLUMNS = [
    "ChargePeriodStart", "ChargePeriodEnd", "BilledCost", "EffectiveCost", "ListCost", "BillingCurrency",
    "ChargeCategory", "ChargeDescription", "ConsumedQuantity", "ConsumedUnit", "ResourceId", "ResourceName",
    "ServiceCategory", "ServiceName", "AllocatedMethodId", "AllocatedResourceId", "AllocatedTags",
]
METRIC_PREDICTED = "llamacpp:tokens_predicted_total"
METRIC_PROMPT = "llamacpp:prompt_tokens_total"

Post = Callable[[str, Dict[str, Any], float], Dict[str, Any]]


class InputError(ValueError):
    pass


@dataclass
class Instance:
    name: str
    hourly_price: float
    as_of: str
    source: str
    accelerators: int = 0
    accelerator: str = ""
    memory_gb: float = 0.0
    bandwidth_gbps: float = 0.0
    tflops: float = 0.0


@dataclass
class ApiPrice:
    name: str
    input_per_mtok: float
    output_per_mtok: float
    as_of: str
    source: str
    cached_input_per_mtok: Optional[float] = None


@dataclass
class PriceBook:
    currency: str
    instances: Dict[str, Instance]
    apis: Dict[str, ApiPrice]
    warnings: List[str] = field(default_factory=list)


@dataclass
class Point:
    concurrency: int
    requests_per_second: float
    input_tokens: float
    output_tokens: float
    stream_tokens_per_second: float
    prefill_tokens_per_second: Optional[float] = None


@dataclass
class Option:
    name: str
    instance: str
    model: str
    quantization: str
    method: str
    source: str
    points: List[Point]


@dataclass
class Scenario:
    name: str
    hourly_rps: List[float]
    input_tokens: float
    output_tokens: float
    min_stream_tps: float
    target_utilization: float
    min_replicas: int
    max_replicas: Optional[int]
    days_per_month: float = 30.0
    overhead_monthly: float = 0.0
    cached_input_fraction: float = 0.0
    compare_with: List[str] = field(default_factory=list)


def number(entry: Dict[str, Any], key: str, where: str, minimum: float = 0.0, strict: bool = True, default: Optional[float] = None) -> float:
    value = entry.get(key, default)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise InputError(f"{where}: {key} must be a number, got {value!r}")
    if value < minimum or (strict and value == minimum):
        raise InputError(f"{where}: {key} must be {'above' if strict else 'at least'} {minimum:g}, got {value}")
    return float(value)


def dated(entry: Dict[str, Any], where: str, today: date, max_age_days: int, warnings: List[str]) -> Tuple[str, str]:
    source = str(entry.get("source") or "").strip()
    if not entry.get("as_of"):
        raise InputError(f"{where}: no as_of date; every price needs the day it was read")
    if not source:
        raise InputError(f"{where}: no source; every price needs the page or quote it came from")
    try:
        day = datetime.strptime(str(entry["as_of"]), "%Y-%m-%d").date()
    except ValueError:
        raise InputError(f"{where}: as_of {entry['as_of']!r} is not a date (YYYY-MM-DD)") from None
    if day > today:
        raise InputError(f"{where}: as_of {day} is after today ({today})")
    age = (today - day).days
    if age > max_age_days:
        warnings.append(f"{where}: price is {age} days old (as_of {day}, stale after {max_age_days}); re-read {source}")
    return str(day), source


def load_prices(data: Dict[str, Any], today: date, max_age_days: int = DEFAULT_MAX_AGE_DAYS) -> PriceBook:
    warnings: List[str] = []
    instances: Dict[str, Instance] = {}
    apis: Dict[str, ApiPrice] = {}
    for raw in data.get("instances") or []:
        name = str(raw.get("name") or "")
        where = f"instance {name or '(no name)'}"
        if not name or name in instances:
            raise InputError(f"{where}: every instance needs a unique name")
        as_of, source = dated(raw, where, today, max_age_days, warnings)
        instances[name] = Instance(
            name, number(raw, "hourly_price", where), as_of, source, int(raw.get("accelerators") or 0), str(raw.get("accelerator") or ""),
            number(raw, "memory_gb", where, strict=False, default=0), number(raw, "bandwidth_gbps", where, strict=False, default=0), number(raw, "tflops", where, strict=False, default=0),
        )
    for raw in data.get("apis") or []:
        name = str(raw.get("name") or "")
        where = f"api {name or '(no name)'}"
        if not name or name in apis:
            raise InputError(f"{where}: every api needs a unique name")
        as_of, source = dated(raw, where, today, max_age_days, warnings)
        cached = raw.get("cached_input_per_mtok")
        apis[name] = ApiPrice(name, number(raw, "input_per_mtok", where, strict=False), number(raw, "output_per_mtok", where, strict=False), as_of, source, None if cached is None else number(raw, "cached_input_per_mtok", where, strict=False))
    if not instances and not apis:
        raise InputError("the price file lists no instances and no apis")
    return PriceBook(str(data.get("currency") or "USD"), instances, apis, warnings)


def kv_bytes_per_token(layers: int, kv_heads: int, head_dim: int, dtype_bytes: float = 2.0) -> float:
    return 2 * layers * kv_heads * head_dim * dtype_bytes


def weight_bytes(params_b: float, quantization: str) -> float:
    bits = BITS_PER_WEIGHT.get(quantization.upper())
    if bits is None:
        raise InputError(f"unknown quantization {quantization!r} (known: {', '.join(BITS_PER_WEIGHT)})")
    return params_b * 1e9 * bits / 8


def max_batch(instance: Instance, params_b: float, quantization: str, kv_per_token: float, context: int, memory_fraction: float = 0.9) -> int:
    usable = instance.memory_gb * 1e9 * max(1, instance.accelerators) * memory_fraction
    free = usable - weight_bytes(params_b, quantization)
    return int(free // (kv_per_token * context)) if free > 0 else 0


def estimate_point(instance: Instance, params_b: float, quantization: str, kv_per_token: float, input_tokens: float, output_tokens: float, batch: int, efficiency: float = 0.6) -> Point:
    devices = max(1, instance.accelerators)
    bandwidth = instance.bandwidth_gbps * 1e9 * devices * efficiency
    flops = instance.tflops * 1e12 * devices * efficiency
    if bandwidth <= 0 or flops <= 0:
        raise InputError(f"instance {instance.name}: bandwidth_gbps and tflops are needed to estimate throughput")
    flops_per_token = 2 * params_b * 1e9
    kv_read = batch * kv_per_token * (input_tokens + output_tokens / 2)
    step = max((weight_bytes(params_b, quantization) + kv_read) / bandwidth, batch * flops_per_token / flops)
    prefill = flops / flops_per_token
    latency = input_tokens / prefill + output_tokens * step
    busy = input_tokens / prefill + output_tokens * step / batch
    return Point(batch, round(1 / busy, 4), input_tokens, output_tokens, round(output_tokens / latency, 2), round(prefill, 1))


def capacity(point: Point, input_tokens: float, output_tokens: float) -> Tuple[float, float]:
    busy = 1 / point.requests_per_second
    prefill = point.prefill_tokens_per_second
    decode_busy = busy - (point.input_tokens / prefill if prefill else 0.0)
    if decode_busy <= 0 or point.output_tokens <= 0:
        raise InputError(f"point c={point.concurrency}: prefill alone takes longer than a whole request; check prefill_tokens_per_second")
    input_busy = input_tokens / prefill if prefill else 0.0
    total = input_busy + output_tokens * decode_busy / point.output_tokens
    return 1 / total, input_busy / total


def replicas_for(demand: Sequence[float], per_replica: float, target: float, min_replicas: int, max_replicas: Optional[int]) -> Tuple[List[int], List[int]]:
    plan: List[int] = []
    short: List[int] = []
    for hour, rps in enumerate(demand):
        need = max(min_replicas, 1, math.ceil(rps / (per_replica * target) - 1e-9)) if rps > 0 else min_replicas
        if max_replicas is not None and need > max_replicas:
            short.append(hour)
            need = max_replicas
        plan.append(need)
    return plan, short


def self_hosted(instance: Instance, point: Point, scenario: Scenario, scale: float = 1.0, replicas: Optional[Sequence[int]] = None, bounded: bool = True) -> Dict[str, Any]:
    uncached = scenario.input_tokens * (1 - scenario.cached_input_fraction)
    per_replica, input_share = capacity(point, uncached, scenario.output_tokens)
    demand = [rps * scale for rps in scenario.hourly_rps]
    if replicas is None:
        plan, short = replicas_for(demand, per_replica, scenario.target_utilization, scenario.min_replicas, scenario.max_replicas if bounded else None)
    else:
        plan = [int(n) for n in replicas]
        short = [hour for hour, (rps, n) in enumerate(zip(demand, plan)) if rps > n * per_replica + 1e-12]
    served = [min(rps, n * per_replica) for rps, n in zip(demand, plan)]
    replica_hours = sum(plan)
    compute = replica_hours * instance.hourly_price * scenario.days_per_month
    monthly = compute + scenario.overhead_monthly
    requests = sum(served) * SECONDS_PER_HOUR * scenario.days_per_month
    input_tokens = requests * scenario.input_tokens
    output_tokens = requests * scenario.output_tokens
    return {
        "requests_per_second_per_replica": per_replica,
        "input_time_share": input_share,
        "replicas": plan,
        "shortfall_hours": short,
        "replica_hours_per_day": replica_hours,
        "utilization": sum(served) / (replica_hours * per_replica) if replica_hours else 0.0,
        "monthly": monthly,
        "compute_monthly": compute,
        "requests_per_month": requests,
        "per_1k_requests": monthly / requests * 1000 if requests else None,
        "per_mtok_input": monthly * input_share / input_tokens * 1e6 if input_tokens else None,
        "per_mtok_output": monthly * (1 - input_share) / output_tokens * 1e6 if output_tokens else None,
        "per_mtok_blended": monthly / (input_tokens + output_tokens) * 1e6 if requests else None,
    }


def api_request_price(api: ApiPrice, scenario: Scenario) -> float:
    cached_price = api.input_per_mtok if api.cached_input_per_mtok is None else api.cached_input_per_mtok
    cached = scenario.input_tokens * scenario.cached_input_fraction
    return ((scenario.input_tokens - cached) * api.input_per_mtok + cached * cached_price + scenario.output_tokens * api.output_per_mtok) / 1e6


def api_cost(api: ApiPrice, scenario: Scenario, scale: float = 1.0) -> Dict[str, Any]:
    per_request = api_request_price(api, scenario)
    requests = sum(scenario.hourly_rps) * scale * SECONDS_PER_HOUR * scenario.days_per_month
    tokens = scenario.input_tokens + scenario.output_tokens
    return {
        "monthly": per_request * requests,
        "requests_per_month": requests,
        "per_1k_requests": per_request * 1000,
        "per_mtok_input": api.input_per_mtok,
        "per_mtok_output": api.output_per_mtok,
        "per_mtok_blended": per_request / tokens * 1e6,
    }


def replica_steps(scenario: Scenario, unit: float, high: float) -> List[Tuple[float, int]]:
    floor = max(scenario.min_replicas, 1)
    steps: List[Tuple[float, int]] = []
    for rps in scenario.hourly_rps:
        if rps <= 0:
            continue
        k = floor
        while True:
            scale = (k + 1e-9) * unit / rps
            if scale >= high:
                break
            steps.append((scale, 1))
            k += 1
    steps.sort()
    return steps


def break_even(instance: Instance, point: Point, scenario: Scenario, api: ApiPrice) -> Dict[str, Any]:
    per_replica, _ = capacity(point, scenario.input_tokens * (1 - scenario.cached_input_fraction), scenario.output_tokens)
    marginal = instance.hourly_price / SECONDS_PER_HOUR / (per_replica * scenario.target_utilization)
    api_price = api_request_price(api, scenario)
    daily = sum(scenario.hourly_rps) * SECONDS_PER_HOUR
    if marginal >= api_price:
        return {"api": api.name, "scale": None, "reason": f"at {scenario.target_utilization:.0%} utilisation a self-hosted request costs {marginal * 1000:.4f} per 1k, the API {api_price * 1000:.4f}; more traffic never closes the gap"}

    unit = per_replica * scenario.target_utilization
    rate = instance.hourly_price * scenario.days_per_month
    api_per_scale = api_cost(api, scenario, 1.0)["monthly"]
    busy_hours = sum(1 for rps in scenario.hourly_rps if rps > 0)
    floor = max(scenario.min_replicas, 1)
    replicas = sum(floor if rps > 0 else scenario.min_replicas for rps in scenario.hourly_rps)
    slope = sum(scenario.hourly_rps) / unit * rate
    ceiling = ((replicas + busy_hours) * rate + scenario.overhead_monthly) / (api_per_scale - slope)
    high = min(SCAN_HIGH, ceiling)

    def dearer_below(count: int) -> float:
        return (count * rate + scenario.overhead_monthly) / api_per_scale

    last_losing = None
    left = 0.0
    for right, added in replica_steps(scenario, unit, high) + [(high, 0)]:
        crossing = dearer_below(replicas)
        if crossing > left:
            last_losing = min(right, crossing)
        replicas += added
        left = right
    if last_losing is None or last_losing < SCAN_LOW:
        return {"api": api.name, "scale": SCAN_LOW, "requests_per_day": round(SCAN_LOW * daily), "tokens_per_day": round(SCAN_LOW * daily * (scenario.input_tokens + scenario.output_tokens)), "reason": "cheaper at every volume scanned"}
    if last_losing >= SCAN_HIGH:
        return {"api": api.name, "scale": None, "reason": f"still dearer at {SCAN_HIGH:g}x the current traffic"}
    scale = math.floor(last_losing * 1e4) / 1e4 + 1e-4
    return {"api": api.name, "scale": round(scale, 4), "requests_per_day": round(scale * daily), "tokens_per_day": round(scale * daily * (scenario.input_tokens + scenario.output_tokens)), "reason": ""}


def money(value: Optional[float], digits: int = 2) -> Optional[float]:
    return None if value is None else round(value, digits)


def point_row(instance: Instance, point: Point, scenario: Scenario, apis: Sequence[ApiPrice], replicas: Optional[Sequence[int]]) -> Dict[str, Any]:
    cost = self_hosted(instance, point, scenario, replicas=replicas)
    return {
        "concurrency": point.concurrency,
        "stream_tokens_per_second": point.stream_tokens_per_second,
        "meets_slo": point.stream_tokens_per_second >= scenario.min_stream_tps,
        "requests_per_second_per_replica": round(cost["requests_per_second_per_replica"], 4),
        "input_time_share": round(cost["input_time_share"], 4),
        "replicas": cost["replicas"],
        "shortfall_hours": cost["shortfall_hours"],
        "replica_hours_per_day": cost["replica_hours_per_day"],
        "utilization": round(cost["utilization"], 4),
        "monthly": money(cost["monthly"]),
        "per_1k_requests": money(cost["per_1k_requests"], 4),
        "per_mtok_input": money(cost["per_mtok_input"], 4),
        "per_mtok_output": money(cost["per_mtok_output"], 4),
        "per_mtok_blended": money(cost["per_mtok_blended"], 4),
        "break_even": [break_even(instance, point, scenario, api) for api in apis],
        "sweep": sweep(instance, point, scenario),
    }


def sweep(instance: Instance, point: Point, scenario: Scenario) -> List[Dict[str, Any]]:
    rows = []
    for scale in SWEEP:
        cost = self_hosted(instance, point, scenario, scale, bounded=False)
        rows.append({"scale": scale, "replica_hours_per_day": cost["replica_hours_per_day"], "utilization": round(cost["utilization"], 4), "monthly": money(cost["monthly"]), "per_mtok_blended": money(cost["per_mtok_blended"], 4)})
    return rows


def evaluate(book: PriceBook, options: Sequence[Option], scenario: Scenario, replicas: Optional[Sequence[int]] = None) -> Dict[str, Any]:
    missing = [name for name in scenario.compare_with if name not in book.apis]
    if missing:
        raise InputError(f"scenario compares with {', '.join(missing)}, which the price file does not list")
    apis = [book.apis[name] for name in scenario.compare_with] if scenario.compare_with else list(book.apis.values())
    hosted = []
    for option in options:
        instance = book.instances.get(option.instance)
        if instance is None:
            raise InputError(f"option {option.name}: no price for instance {option.instance!r} in the price file")
        points = [point_row(instance, point, scenario, apis, replicas) for point in option.points]
        eligible = [row for row in points if row["meets_slo"] and not row["shortfall_hours"]]
        chosen = min(eligible, key=lambda row: (row["monthly"], -row["requests_per_second_per_replica"])) if eligible else None
        best_stream = max(option.points, key=lambda p: p.stream_tokens_per_second)
        hosted.append({
            "option": option.name,
            "instance": instance.name,
            "hourly_price": instance.hourly_price,
            "model": option.model,
            "quantization": option.quantization,
            "method": option.method,
            "source": option.source,
            "points": points,
            "chosen": chosen["concurrency"] if chosen else None,
            "why_not": "" if chosen else (f"no point meets {scenario.min_stream_tps:g} tokens/s per request (best {best_stream.stream_tokens_per_second:g} at c={best_stream.concurrency})" if not any(row["meets_slo"] for row in points) else "every point that meets the SLO runs out of replicas at peak"),
        })
    api_rows = []
    for api in apis:
        cost = api_cost(api, scenario)
        api_rows.append({"api": api.name, "as_of": api.as_of, "source": api.source, "monthly": money(cost["monthly"]), "per_1k_requests": money(cost["per_1k_requests"], 4), "per_mtok_input": api.input_per_mtok, "per_mtok_output": api.output_per_mtok, "per_mtok_blended": money(cost["per_mtok_blended"], 4)})
    candidates = [(row["option"], "self-hosted", next(p for p in row["points"] if p["concurrency"] == row["chosen"])["monthly"]) for row in hosted if row["chosen"] is not None]
    candidates += [(row["api"], "api", row["monthly"]) for row in api_rows]
    best = min(candidates, key=lambda item: item[2]) if candidates else None
    requests = api_cost(apis[0], scenario)["requests_per_month"] if apis else sum(scenario.hourly_rps) * SECONDS_PER_HOUR * scenario.days_per_month
    peak = max(range(HOURS_PER_DAY), key=lambda hour: scenario.hourly_rps[hour])
    dated_entries = [(i.as_of, i.name, i.source) for i in book.instances.values()] + [(a.as_of, a.name, a.source) for a in book.apis.values()]
    return {
        "scenario": scenario.name,
        "currency": book.currency,
        "requests_per_month": round(requests),
        "input_tokens_per_month": round(requests * scenario.input_tokens),
        "output_tokens_per_month": round(requests * scenario.output_tokens),
        "peak": {"hour": peak, "requests_per_second": scenario.hourly_rps[peak]},
        "slo_min_stream_tokens_per_second": scenario.min_stream_tps,
        "target_utilization": scenario.target_utilization,
        "prices": {"entries": len(dated_entries), "oldest": min(dated_entries)[0] if dated_entries else None, "warnings": list(book.warnings)},
        "self_hosted": hosted,
        "apis": api_rows,
        "cheapest": {"name": best[0], "kind": best[1], "monthly": best[2]} if best else None,
        "not_included": not_included(scenario),
    }


def not_included(scenario: Scenario) -> List[str]:
    notes = []
    if scenario.overhead_monthly == 0:
        notes.append("people and operations (set overhead_monthly to include on-call, upgrades and evals)")
    notes.append("egress, storage and the load balancer in front of the replicas")
    notes.append("reserved, committed-use and spot discounts, and API batch or volume discounts")
    notes.append("quality: compare only models that pass the same evaluation")
    return notes


def kubernetes_quantity(value: Any) -> int:
    text = str(value).strip()
    if not text.isdigit():
        raise InputError(f"extended resources are whole numbers, got {text!r}")
    return int(text)


def pod_request(pod: Dict[str, Any], resource: str) -> int:
    spec = pod.get("spec") or {}

    def one(container: Dict[str, Any]) -> int:
        resources = container.get("resources") or {}
        requests = resources.get("requests") or {}
        limits = resources.get("limits") or {}
        return kubernetes_quantity(requests.get(resource, limits.get(resource, 0)))

    total = sum(one(c) for c in spec.get("containers") or [])
    init = max([one(c) for c in spec.get("initContainers") or []] or [0])
    return max(total, init)


def group_of(pod: Dict[str, Any], by: str) -> str:
    meta = pod.get("metadata") or {}
    if by == "namespace":
        return str(meta.get("namespace") or "default")
    if by.startswith("label:"):
        return str((meta.get("labels") or {}).get(by[len("label:"):]) or UNALLOCATED)
    raise InputError(f"--by must be namespace or label:KEY, got {by!r}")


def allocate(nodes: Dict[str, Any], pods: Dict[str, Any], book: PriceBook, resource: str, by: str = "namespace", hours: float = 24.0, instance_for: Optional[Dict[str, str]] = None, share_idle: bool = False) -> Dict[str, Any]:
    instance_for = instance_for or {}
    rows: Dict[str, Dict[str, Any]] = {}
    node_lines = []

    def charge(key: str, node: str, gpus: int, per_gpu_hour: float) -> None:
        row = rows.setdefault(key, {"name": key, "gpus": 0, "gpu_hours": 0.0, "cost": 0.0, "nodes": []})
        row["gpus"] += gpus
        row["gpu_hours"] += gpus * hours
        row["cost"] += gpus * hours * per_gpu_hour
        if node not in row["nodes"]:
            row["nodes"].append(node)

    for node in nodes.get("items") or []:
        name = node["metadata"]["name"]
        gpus = kubernetes_quantity(((node.get("status") or {}).get("capacity") or {}).get(resource, 0))
        if gpus <= 0:
            continue
        instance_name = instance_for.get(name) or ((node["metadata"].get("labels") or {}).get("node.kubernetes.io/instance-type"))
        instance = book.instances.get(str(instance_name))
        if instance is None:
            raise InputError(f"node {name}: no price for instance type {instance_name!r}; add it to the price file or pass --instance {name}=NAME")
        per_gpu_hour = instance.hourly_price / gpus
        requested = 0
        for pod in pods.get("items") or []:
            spec = pod.get("spec") or {}
            if spec.get("nodeName") != name or (pod.get("status") or {}).get("phase") in TERMINAL_PHASES:
                continue
            count = pod_request(pod, resource)
            if count:
                requested += count
                charge(group_of(pod, by), name, count, per_gpu_hour)
        if requested < gpus:
            charge(IDLE, name, gpus - requested, per_gpu_hour)
        node_lines.append({"node": name, "instance": instance.name, "hourly_price": instance.hourly_price, "gpus": gpus, "requested": requested})
    if share_idle and IDLE in rows:
        idle = rows.pop(IDLE)
        busy = sum(row["cost"] for row in rows.values())
        for row in rows.values():
            row["idle_cost"] = idle["cost"] * row["cost"] / busy if busy else 0.0
            row["cost"] += row["idle_cost"]
        if not rows:
            rows[IDLE] = idle
    total = sum(row["cost"] for row in rows.values())
    named = sorted((row for row in rows.values() if row["name"] != IDLE), key=lambda row: (-row["cost"], row["name"]))
    ordered = named + ([rows[IDLE]] if IDLE in rows else [])
    for row in ordered:
        row["cost"] = round(row["cost"], 4)
        row["share"] = round(row["cost"] / total, 4) if total else 0.0
    return {"resource": resource, "by": by, "hours": hours, "currency": book.currency, "nodes": node_lines, "allocations": ordered, "total": round(total, 4)}


def focus_rows(allocation: Dict[str, Any], start: datetime) -> List[Dict[str, Any]]:
    end = start + timedelta(hours=allocation["hours"])

    def stamp(moment: datetime) -> str:
        return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    dimension = "k8s/namespace" if allocation["by"] == "namespace" else "k8s/label/" + allocation["by"][len("label:"):]
    rows = []
    for row in allocation["allocations"]:
        idle = row["name"] == IDLE
        rows.append({
            "ChargePeriodStart": stamp(start),
            "ChargePeriodEnd": stamp(end),
            "BilledCost": row["cost"],
            "EffectiveCost": row["cost"],
            "ListCost": row["cost"],
            "BillingCurrency": allocation["currency"],
            "ChargeCategory": "Usage",
            "ChargeDescription": f"{allocation['resource']} hours on {', '.join(row['nodes'])}" + (" requested by nobody" if idle else f" requested by {row['name']}"),
            "ConsumedQuantity": round(row["gpu_hours"], 4),
            "ConsumedUnit": "GPU-Hours",
            "ResourceId": ",".join(row["nodes"]),
            "ResourceName": ",".join(row["nodes"]),
            "ServiceCategory": "AI and Machine Learning",
            "ServiceName": "Self-hosted inference",
            "AllocatedMethodId": "tokencost-gpu-requests",
            "AllocatedResourceId": None if idle else row["name"],
            "AllocatedTags": None if idle else json.dumps({dimension: row["name"]}, sort_keys=True),
        })
    return rows


def urllib_post(url: str, body: Dict[str, Any], timeout: float) -> Dict[str, Any]:
    request = urllib.request.Request(url, data=json.dumps(body).encode("utf-8"), headers={"content-type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"{url} answered HTTP {exc.code}: {exc.read()[:200].decode('utf-8', 'replace')}") from None
    except urllib.error.URLError as exc:
        raise RuntimeError(f"cannot reach {url}: {exc.reason}; is the model server running?") from None


def fetch_text(url: str, timeout: float = 10.0) -> str:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            return response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"{url} answered HTTP {exc.code}; llama.cpp serves /metrics only with --metrics") from None
    except urllib.error.URLError as exc:
        raise RuntimeError(f"cannot reach {url}: {exc.reason}") from None


def prometheus_value(text: str, name: str) -> Optional[float]:
    total = None
    for line in text.splitlines():
        if line.startswith("#"):
            continue
        metric, _, rest = line.partition(" ")
        if metric == name or metric.startswith(name + "{"):
            total = (total or 0.0) + float(rest.split()[0])
    return total


def run_load(url: str, model: str, concurrency: int, requests: int, max_tokens: int, prompt: str, ignore_eos: bool = True, cache_prompt: Optional[bool] = None, post: Post = urllib_post, clock: Callable[[], float] = time.perf_counter, timeout: float = 300.0, offset: int = 0) -> Tuple[Point, List[Dict[str, Any]]]:
    endpoint = url.rstrip("/") + "/v1/chat/completions"

    def one(index: int) -> Dict[str, Any]:
        body: Dict[str, Any] = {"model": model, "messages": [{"role": "user", "content": f"Ticket {offset + index}: {prompt}"}], "max_tokens": max_tokens, "temperature": 0}
        if ignore_eos:
            body["ignore_eos"] = True
        if cache_prompt is not None:
            body["cache_prompt"] = cache_prompt
        started = clock()
        reply = post(endpoint, body, timeout)
        ended = clock()
        usage = reply.get("usage") or {}
        if "completion_tokens" not in usage:
            raise RuntimeError(f"{endpoint}: the response has no usage.completion_tokens; tokencost needs an OpenAI-compatible server that reports usage")
        timings = reply.get("timings") or {}
        cached = (usage.get("prompt_tokens_details") or {}).get("cached_tokens") or 0
        return {"start": started, "end": ended, "prompt_tokens": int(usage.get("prompt_tokens") or 0), "completion_tokens": int(usage["completion_tokens"]), "cached_tokens": int(cached), "prefill_tokens": timings.get("prompt_n"), "prefill_ms": timings.get("prompt_ms")}

    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        results = list(pool.map(one, range(requests)))
    wall = max(r["end"] for r in results) - min(r["start"] for r in results)
    if wall <= 0:
        raise RuntimeError("the load run took no measurable time")
    speeds = [r["completion_tokens"] / (r["end"] - r["start"]) for r in results if r["end"] > r["start"]]
    timed = [r for r in results if r["prefill_tokens"] and r["prefill_ms"]]
    prefill = sum(r["prefill_tokens"] for r in timed) / sum(r["prefill_ms"] for r in timed) * 1000 if timed else None
    point = Point(
        concurrency,
        round(requests / wall, 4),
        round(statistics.mean(r["prompt_tokens"] for r in results), 1),
        round(statistics.mean(r["completion_tokens"] for r in results), 1),
        round(statistics.median(speeds), 2) if speeds else 0.0,
        round(prefill, 1) if prefill else None,
    )
    return point, results


def isolated_prefill(points: Sequence[Point]) -> List[Point]:
    alone = min(points, key=lambda p: p.concurrency).prefill_tokens_per_second
    return [replace(point, prefill_tokens_per_second=alone) for point in points]


def option_from(data: Dict[str, Any], where: str) -> Option:
    try:
        points = [Point(int(p["concurrency"]), float(p["requests_per_second"]), float(p["input_tokens"]), float(p["output_tokens"]), float(p["stream_tokens_per_second"]), None if p.get("prefill_tokens_per_second") is None else float(p["prefill_tokens_per_second"])) for p in data["points"]]
        option = Option(str(data.get("name") or where), str(data["instance"]), str(data.get("model") or ""), str(data.get("quantization") or ""), str(data.get("method") or "measured"), str(data.get("source") or ""), points)
    except (KeyError, TypeError, ValueError) as exc:
        raise InputError(f"{where}: not a tokencost measurement file ({exc})") from None
    if not points or any(p.requests_per_second <= 0 for p in points):
        raise InputError(f"{where}: needs at least one point with requests_per_second above 0")
    return option


def scenario_from(data: Dict[str, Any], where: str = "scenario") -> Scenario:
    hourly = data.get("hourly_requests_per_second")
    if not isinstance(hourly, list) or len(hourly) != HOURS_PER_DAY or any(isinstance(v, bool) or not isinstance(v, (int, float)) or v < 0 for v in hourly):
        raise InputError(f"{where}: hourly_requests_per_second must list 24 numbers, one per hour, none negative")
    request = data.get("request") or {}
    slo = data.get("slo") or {}
    scaling = data.get("autoscaling") or {}
    target = number(scaling, "target_utilization", f"{where} autoscaling")
    if target > 1:
        raise InputError(f"{where}: target_utilization is a fraction of capacity (0.7 is 70%), got {target}")
    cached = number(data, "cached_input_fraction", where, strict=False, default=0)
    if cached >= 1:
        raise InputError(f"{where}: cached_input_fraction must be below 1, got {cached}")
    max_replicas = scaling.get("max_replicas")
    return Scenario(
        str(data.get("name") or where), [float(v) for v in hourly],
        number(request, "input_tokens", f"{where} request", strict=False), number(request, "output_tokens", f"{where} request"),
        number(slo, "min_stream_tokens_per_second", f"{where} slo", strict=False, default=0), target,
        int(scaling.get("min_replicas", 1)), None if max_replicas is None else int(max_replicas),
        number(data, "days_per_month", where, default=30), number(data, "overhead_monthly", where, strict=False, default=0),
        cached, [str(name) for name in data.get("compare_with") or []],
    )


def replicas_from(data: Any) -> List[int]:
    values = data.get("hourly_replicas") if isinstance(data, dict) else data
    if not isinstance(values, list) or len(values) != HOURS_PER_DAY or any(isinstance(v, bool) or not isinstance(v, int) or v < 0 for v in values):
        raise InputError("--replicas must be a JSON list of 24 whole numbers, or an object with hourly_replicas")
    return values


def number_text(value: Optional[float], digits: int = 4) -> str:
    return "-" if value is None else f"{value:,.{digits}f}"


def render_plan(report: Dict[str, Any]) -> str:
    cur = report["currency"]
    lines = [
        f"Budget review: {report['scenario']}",
        f"  traffic   {report['requests_per_month']:,} requests/month, {report['input_tokens_per_month'] / 1e6:,.0f} M input + {report['output_tokens_per_month'] / 1e6:,.0f} M output tokens, peak {report['peak']['requests_per_second']:g} req/s at {report['peak']['hour']:02d}:00",
        f"  SLO       at least {report['slo_min_stream_tokens_per_second']:g} output tokens/s per request; replicas sized for {report['target_utilization']:.0%} utilisation",
        f"  prices    {cur}, {report['prices']['entries']} entries, oldest {report['prices']['oldest']}, {len(report['prices']['warnings'])} stale",
    ]
    lines += [f"  WARNING   {warning}" for warning in report["prices"]["warnings"]]
    width = max([32] + [len(row["option"]) + 2 for row in report["self_hosted"]] + [len(row["api"]) + 2 for row in report["apis"]])
    lines += ["", "  * chosen batch   x misses the SLO   ! runs out of replicas at peak (max_replicas)", f"  {'self-hosted':<{width}}{'batch':>6}{'tok/s':>7}{'req/s/rep':>10}{'rep-h/d':>8}{'util':>7}{'/month':>11}{'/Mtok in':>10}{'/Mtok out':>10}{'/1k req':>9}"]
    for row in report["self_hosted"]:
        label = f"{row['option']}"
        for point in row["points"]:
            mark = "*" if point["concurrency"] == row["chosen"] else ("x" if not point["meets_slo"] else ("!" if point["shortfall_hours"] else " "))
            lines.append(f"{mark} {label:<{width}}{point['concurrency']:>6}{point['stream_tokens_per_second']:>7.1f}{point['requests_per_second_per_replica']:>10.3f}{point['replica_hours_per_day']:>8}{point['utilization']:>7.0%}{point['monthly']:>11,.2f}{number_text(point['per_mtok_input']):>10}{number_text(point['per_mtok_output']):>10}{number_text(point['per_1k_requests']):>9}")
            label = ""
        if row["chosen"] is None:
            lines.append(f"  {'':<{width}}not eligible: {row['why_not']}")
    lines += ["", f"  {'api':<{width}}{'':>38}{'/month':>11}{'/Mtok in':>10}{'/Mtok out':>10}{'/1k req':>9}"]
    for row in report["apis"]:
        lines.append(f"  {row['api']:<{width}}{'':>38}{row['monthly']:>11,.2f}{row['per_mtok_input']:>10.4f}{row['per_mtok_output']:>10.4f}{row['per_1k_requests']:>9.4f}")
    lines += ["", "  break-even at the chosen batch (self-hosting is cheaper from this volume up)"]
    for row in report["self_hosted"]:
        point = next((p for p in row["points"] if p["concurrency"] == row["chosen"]), None)
        if point is None:
            continue
        for result in point["break_even"]:
            if result["scale"] is None:
                lines.append(f"    {row['option']} vs {result['api']}: never; {result['reason']}")
            else:
                lines.append(f"    {row['option']} vs {result['api']}: {result['scale']:.3g}x current traffic, {result['requests_per_day']:,} requests/day")
    for row in report["self_hosted"]:
        point = next((p for p in row["points"] if p["concurrency"] == row["chosen"]), None)
        if point is not None:
            cells = ", ".join(f"x{s['scale']:g} {s['utilization']:.0%} {s['per_mtok_blended']:.4f}" for s in point["sweep"])
            lines.append(f"  utilisation sweep {row['option']} (traffic, util, {cur}/Mtok blended): {cells}")
    if report["cheapest"]:
        lines += ["", f"  cheapest at current traffic: {report['cheapest']['name']} ({report['cheapest']['kind']}) at {report['cheapest']['monthly']:,.2f} {cur}/month"]
    lines += ["  not included: " + "; ".join(report["not_included"])]
    return "\n".join(lines)


def render_allocation(report: Dict[str, Any]) -> str:
    lines = [f"{report['resource']} over {report['hours']:g} h, by {report['by']}, {report['currency']}"]
    for node in report["nodes"]:
        lines.append(f"  node {node['node']}: {node['instance']} at {node['hourly_price']:g}/h, {node['requested']}/{node['gpus']} requested")
    for row in report["allocations"]:
        lines.append(f"  {row['name']:<24}{row['gpus']:>4} GPUs {row['gpu_hours']:>8.1f} GPU-h {row['cost']:>10.2f}  {row['share']:>6.1%}")
    lines.append(f"  {'total':<24}{'':>28}{report['total']:>10.2f}")
    return "\n".join(lines)


def load_json(path: str) -> Any:
    handle = sys.stdin if path == "-" else open(path, encoding="utf-8")
    try:
        return json.load(handle)
    except json.JSONDecodeError as exc:
        raise InputError(f"{path}: not JSON ({exc.msg} at line {exc.lineno})") from None
    finally:
        if handle is not sys.stdin:
            handle.close()


def write_json(data: Any, path: Optional[str]) -> None:
    text = json.dumps(data, indent=2)
    if path in (None, "-"):
        print(text)
    else:
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(text + "\n")


def today_from(text: Optional[str]) -> date:
    if not text:
        return date.today()
    try:
        return datetime.strptime(text, "%Y-%m-%d").date()
    except ValueError:
        raise InputError(f"--today {text!r} is not YYYY-MM-DD") from None


def integers(text: str) -> List[int]:
    try:
        values = [int(part) for part in text.split(",") if part.strip()]
    except ValueError:
        raise InputError(f"expected a comma-separated list of whole numbers, got {text!r}") from None
    if not values or any(v < 1 for v in values):
        raise InputError(f"expected whole numbers of at least 1, got {text!r}")
    return values


def cmd_prices(args: argparse.Namespace) -> int:
    book = load_prices(load_json(args.prices), today_from(args.today), args.max_age_days)
    if args.json:
        write_json({"currency": book.currency, "instances": [asdict(i) for i in book.instances.values()], "apis": [asdict(a) for a in book.apis.values()], "warnings": book.warnings}, None)
    else:
        for item in book.instances.values():
            print(f"instance {item.name:<16} {item.hourly_price:>8.4f} {book.currency}/h   {item.accelerators}x {item.accelerator or 'cpu':<8} as_of {item.as_of}  {item.source}")
        for api in book.apis.values():
            print(f"api      {api.name:<16} {api.input_per_mtok:>8.4f} in / {api.output_per_mtok:.4f} out {book.currency}/Mtok   as_of {api.as_of}  {api.source}")
        for warning in book.warnings:
            print(f"WARNING {warning}", file=sys.stderr)
    return 0


def cmd_estimate(args: argparse.Namespace) -> int:
    book = load_prices(load_json(args.prices), today_from(args.today), args.max_age_days)
    instance = book.instances.get(args.instance)
    if instance is None:
        raise InputError(f"no instance {args.instance!r} in {args.prices} (have: {', '.join(book.instances) or 'none'})")
    if args.kv_bytes_per_token:
        kv = args.kv_bytes_per_token
    elif args.layers and args.kv_heads and args.head_dim:
        kv = kv_bytes_per_token(args.layers, args.kv_heads, args.head_dim, args.kv_dtype_bytes)
    else:
        raise InputError("give --kv-bytes-per-token, or --layers, --kv-heads and --head-dim")
    limit = max_batch(instance, args.params_b, args.quantization, kv, args.context, args.memory_fraction)
    if limit < 1:
        raise InputError(f"{args.params_b:g}B at {args.quantization} ({weight_bytes(args.params_b, args.quantization) / 1e9:.1f} GB) leaves no room for one {args.context}-token KV cache on {instance.name}")
    batches = [b for b in integers(args.batches) if b <= limit]
    data = {
        "name": args.name or f"{instance.name} {args.model} {args.quantization}",
        "instance": instance.name,
        "model": args.model,
        "quantization": args.quantization,
        "method": "estimated",
        "source": f"tokencost estimate: {args.params_b:g}B params, {BITS_PER_WEIGHT[args.quantization.upper()]:g} bits/weight, {kv:g} B KV/token, {args.context} context, {args.efficiency:g} of peak bandwidth and FLOPs on {instance.accelerator or instance.name}",
        "max_batch": limit,
        "memory_gb": {"weights": round(weight_bytes(args.params_b, args.quantization) / 1e9, 2), "kv_per_sequence": round(kv * args.context / 1e9, 4)},
        "points": [asdict(estimate_point(instance, args.params_b, args.quantization, kv, args.input_tokens, args.output_tokens, b, args.efficiency)) for b in batches],
    }
    write_json(data, args.out)
    return 0


def cmd_measure(args: argparse.Namespace) -> int:
    before = prometheus_value(fetch_text(args.metrics_url), METRIC_PREDICTED) if args.metrics_url else None
    points: List[Point] = []
    completion = 0
    cache = False if args.no_prompt_cache else None
    if args.warmup:
        _, warm = run_load(args.url, args.model, 1, args.warmup, args.max_tokens, args.prompt, args.ignore_eos, cache, offset=10_000)
        completion += sum(r["completion_tokens"] for r in warm)
    for concurrency in integers(args.concurrency):
        point, results = run_load(args.url, args.model, concurrency, args.requests, args.max_tokens, args.prompt, args.ignore_eos, cache, offset=len(points) * args.requests)
        completion += sum(r["completion_tokens"] for r in results)
        points.append(point)
        cached = sum(r["cached_tokens"] for r in results) / max(1, sum(r["prompt_tokens"] for r in results))
        print(f"c={concurrency}: {point.requests_per_second:.3f} req/s, {point.requests_per_second * point.output_tokens:.1f} output tok/s aggregate, {point.stream_tokens_per_second:.1f} tok/s per request, {cached:.0%} of prompt tokens from cache", file=sys.stderr)
    data: Dict[str, Any] = {
        "name": args.name or f"{args.instance} {args.model} {args.quantization}".strip(),
        "instance": args.instance,
        "model": args.model,
        "quantization": args.quantization,
        "method": "measured",
        "source": f"tokencost measure {args.url} on {date.today()}, {args.requests} requests per point, max_tokens {args.max_tokens}",
        "points": [asdict(point) for point in isolated_prefill(points)],
    }
    if args.metrics_url:
        after = prometheus_value(fetch_text(args.metrics_url), METRIC_PREDICTED)
        data["server_metrics"] = {"metric": METRIC_PREDICTED, "delta": None if before is None or after is None else after - before, "client_completion_tokens": completion}
    write_json(data, args.out)
    return 0


def cmd_plan(args: argparse.Namespace) -> int:
    book = load_prices(load_json(args.prices), today_from(args.today), args.max_age_days)
    scenario = scenario_from(load_json(args.scenario), args.scenario)
    options = [option_from(load_json(path), path) for path in args.measurements]
    replicas = replicas_from(load_json(args.replicas)) if args.replicas else None
    report = evaluate(book, options, scenario, replicas)
    if args.json:
        write_json(report, args.out)
    else:
        print(render_plan(report))
    return 0


def cmd_allocate(args: argparse.Namespace) -> int:
    book = load_prices(load_json(args.prices), today_from(args.today), args.max_age_days)
    mapping = {}
    for item in args.instance:
        node, separator, name = item.partition("=")
        if not separator or not node or not name:
            raise InputError(f"--instance {item!r} must look like NODE=INSTANCE")
        mapping[node] = name
    report = allocate(load_json(args.nodes), load_json(args.pods), book, args.resource, args.by, args.hours, mapping, args.share_idle)
    if args.focus:
        start = datetime.strptime(args.period_start, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc) if args.period_start else datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0) - timedelta(hours=args.hours)
        with open(args.focus, "w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=FOCUS_COLUMNS)
            writer.writeheader()
            writer.writerows(focus_rows(report, start))
    if args.json:
        write_json(report, None)
    else:
        print(render_allocation(report))
    return 0


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="tokencost", description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)

    def priced(sub: argparse.ArgumentParser) -> None:
        sub.add_argument("--prices", required=True, metavar="FILE", help="price file: instances and apis, each with as_of and source")
        sub.add_argument("--today", metavar="YYYY-MM-DD", help="the date prices are judged against (default: today)")
        sub.add_argument("--max-age-days", type=int, default=DEFAULT_MAX_AGE_DAYS, help="warn about prices older than this")

    prices = commands.add_parser("prices", help="validate a price file: every entry dated and sourced, stale ones flagged")
    prices.add_argument("prices", metavar="FILE")
    prices.add_argument("--today", metavar="YYYY-MM-DD")
    prices.add_argument("--max-age-days", type=int, default=DEFAULT_MAX_AGE_DAYS)
    prices.add_argument("--json", action="store_true")
    prices.set_defaults(func=cmd_prices)

    estimate = commands.add_parser("estimate", help="first-principles throughput points for an accelerator: weights and KV cache against bandwidth and FLOPs")
    priced(estimate)
    estimate.add_argument("--instance", required=True)
    estimate.add_argument("--model", required=True)
    estimate.add_argument("--name")
    estimate.add_argument("--params-b", type=float, required=True, help="parameters in billions")
    estimate.add_argument("--quantization", default="F16", help=f"one of {', '.join(BITS_PER_WEIGHT)}")
    estimate.add_argument("--kv-bytes-per-token", type=float)
    estimate.add_argument("--layers", type=int)
    estimate.add_argument("--kv-heads", type=int)
    estimate.add_argument("--head-dim", type=int)
    estimate.add_argument("--kv-dtype-bytes", type=float, default=2.0)
    estimate.add_argument("--context", type=int, default=4096, help="tokens of KV cache reserved per sequence")
    estimate.add_argument("--input-tokens", type=float, required=True)
    estimate.add_argument("--output-tokens", type=float, required=True)
    estimate.add_argument("--batches", default="1,4,8,16,32")
    estimate.add_argument("--efficiency", type=float, default=0.6, help="fraction of peak bandwidth and FLOPs the kernels reach")
    estimate.add_argument("--memory-fraction", type=float, default=0.9, help="share of accelerator memory the server may use")
    estimate.add_argument("-o", "--out")
    estimate.set_defaults(func=cmd_estimate)

    measure = commands.add_parser("measure", help="closed-loop load against an OpenAI-compatible server; one point per concurrency")
    measure.add_argument("--url", required=True, help="base URL, e.g. http://127.0.0.1:8080")
    measure.add_argument("--instance", required=True, help="the price-file instance this server runs on")
    measure.add_argument("--model", default="default")
    measure.add_argument("--quantization", default="")
    measure.add_argument("--name")
    measure.add_argument("--concurrency", default="1,4")
    measure.add_argument("--requests", type=int, default=8, help="requests per concurrency level")
    measure.add_argument("--max-tokens", type=int, default=128)
    measure.add_argument("--prompt", default="A customer was billed twice for one order. Summarise the case and list the next steps for support.")
    measure.add_argument("--ignore-eos", action="store_true", help="ask the server for exactly max_tokens (llama.cpp and vLLM accept ignore_eos)")
    measure.add_argument("--no-prompt-cache", action="store_true", help="send cache_prompt=false (llama.cpp) so every prompt token is prefilled and timed")
    measure.add_argument("--warmup", type=int, default=1)
    measure.add_argument("--metrics-url", help="Prometheus endpoint to cross-check generated tokens, e.g. http://127.0.0.1:8080/metrics")
    measure.add_argument("-o", "--out")
    measure.set_defaults(func=cmd_measure)

    plan = commands.add_parser("plan", help="replicas, utilisation, cost per token and break-even for a day of traffic")
    priced(plan)
    plan.add_argument("--scenario", required=True)
    plan.add_argument("--measurements", nargs="+", required=True, metavar="FILE")
    plan.add_argument("--replicas", metavar="FILE", help="24 hourly replica counts from your autoscaler instead of sizing them here")
    plan.add_argument("--json", action="store_true")
    plan.add_argument("-o", "--out")
    plan.set_defaults(func=cmd_plan)

    alloc = commands.add_parser("allocate", help="showback: charge GPU node cost to namespaces or labels by requested GPUs, idle as its own line")
    priced(alloc)
    alloc.add_argument("--nodes", required=True, help="kubectl get nodes -o json")
    alloc.add_argument("--pods", required=True, help="kubectl get pods -A -o json")
    alloc.add_argument("--resource", default="nvidia.com/gpu")
    alloc.add_argument("--by", default="namespace", help="namespace or label:KEY")
    alloc.add_argument("--hours", type=float, default=24.0)
    alloc.add_argument("--instance", action="append", default=[], metavar="NODE=INSTANCE", help="price a node that has no node.kubernetes.io/instance-type label")
    alloc.add_argument("--share-idle", action="store_true")
    alloc.add_argument("--focus", metavar="FILE.csv", help="also write the rows in FOCUS columns")
    alloc.add_argument("--period-start", metavar="YYYY-MM-DDTHH:MM:SSZ")
    alloc.add_argument("--json", action="store_true")
    alloc.set_defaults(func=cmd_allocate)

    args = parser.parse_args(list(sys.argv[1:] if argv is None else argv))
    try:
        return int(args.func(args))
    except FileNotFoundError as exc:
        print(f"tokencost: {exc.filename}: no such file", file=sys.stderr)
        return 2
    except InputError as exc:
        print(f"tokencost: {exc}", file=sys.stderr)
        return 2
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"tokencost: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
