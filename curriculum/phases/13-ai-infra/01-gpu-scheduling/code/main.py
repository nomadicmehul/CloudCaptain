"""gpusched: place GPU pods on a cluster model (nodes, GPUs, GPU memory, NVLink islands, MIG, time-slicing) with bin-pack, spread or topology-aware policies; compare what kube-scheduler sees through extended resources with Dynamic Resource Allocation; simulate gang scheduling; measure fragmentation; and predict Pending reasons from `kubectl get -o json` output."""
from __future__ import annotations

import argparse
import itertools
import json
import math
import re
import sys
from dataclasses import dataclass, field
from fractions import Fraction
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple

POLICIES = ("binpack", "spread", "topology")
VIEWS = ("gpu", "extended")
GANG_MODES = ("pod", "gang")
GPU_RESOURCE = "nvidia.com/gpu"
DRIVER = "gpu.example.com"
DEFAULT_SIZES = (1, 2, 4, 8)
MIG_MEMORY_SLICES = 8
MIG_COMPUTE_SLICES = 7
TERMINAL_PHASES = ("Succeeded", "Failed")
FILTER_EFFECTS = ("NoSchedule", "NoExecute")
UNSCHEDULABLE_TAINT = {"key": "node.kubernetes.io/unschedulable", "effect": "NoSchedule"}
REASON_UNSCHEDULABLE = "node(s) were unschedulable"
REASON_TAINT = "node(s) had untolerated taint(s)"
REASON_CLAIMS = "cannot allocate all claims"
REASON_CLAIM_ELSEWHERE = "resourceclaim not available on the node"
QUANTITY = re.compile(r"^([+-]?(?:\d+(?:\.\d*)?|\.\d+))(?:([eE][+-]?\d+)|(Ki|Mi|Gi|Ti|Pi|Ei|n|u|m|k|M|G|T|P|E))?$")
BINARY_SUFFIXES = {"Ki": 1 << 10, "Mi": 1 << 20, "Gi": 1 << 30, "Ti": 1 << 40, "Pi": 1 << 50, "Ei": 1 << 60}
DECIMAL_SUFFIXES = {
    "n": Fraction(1, 10**9),
    "u": Fraction(1, 10**6),
    "m": Fraction(1, 10**3),
    "": Fraction(1),
    "k": Fraction(10**3),
    "M": Fraction(10**6),
    "G": Fraction(10**9),
    "T": Fraction(10**12),
    "P": Fraction(10**15),
    "E": Fraction(10**18),
}
CEL_TOKEN = re.compile(
    r"""\s*(?:(?P<number>\d+)|(?P<string>"(?:[^"\\]|\\.)*"|'(?:[^'\\]|\\.)*')|(?P<op>==|!=|>=|<=|&&|\|\||[<>!().\[\],])|(?P<name>[A-Za-z_][A-Za-z0-9_]*))"""
)
CEL_COMPARISONS = ("==", "!=", "<", "<=", ">", ">=")


class InputError(ValueError):
    pass


class CelError(Exception):
    pass


@dataclass(frozen=True)
class MigProfile:
    name: str
    compute: int
    memory_slices: int
    starts: Tuple[int, ...]
    memory_gib: int


A100_80GB_MIG = {
    p.name: p
    for p in (
        MigProfile("1g.10gb", 1, 1, (0, 1, 2, 3, 4, 5, 6), 10),
        MigProfile("2g.20gb", 2, 2, (0, 2, 4), 20),
        MigProfile("3g.40gb", 3, 4, (0, 4), 40),
        MigProfile("4g.40gb", 4, 4, (0,), 40),
        MigProfile("7g.80gb", 7, 8, (0,), 80),
    )
}


@dataclass
class MigInstance:
    profile: str
    start: int
    holder: str = ""

    @property
    def slices(self) -> Set[int]:
        return set(range(self.start, self.start + A100_80GB_MIG[self.profile].memory_slices))

    @property
    def label(self) -> str:
        return f"{self.profile}@{self.start}"


@dataclass
class Gpu:
    index: int
    island: int
    replicas: int = 1
    holders: List[Tuple[str, float]] = field(default_factory=list)
    mig: List[MigInstance] = field(default_factory=list)

    @property
    def free_replicas(self) -> int:
        return 0 if self.mig else self.replicas - len(self.holders)

    @property
    def declared_gib(self) -> float:
        return sum(memory for _, memory in self.holders)


@dataclass
class Node:
    name: str
    model: str
    memory_gib: float
    gpus: List[Gpu]
    nvlink_gbps: float = 0.0
    pcie_gbps: float = 0.0
    time_slicing: int = 1
    labels: Dict[str, str] = field(default_factory=dict)

    @property
    def shared(self) -> bool:
        return self.time_slicing > 1

    @property
    def mig_enabled(self) -> bool:
        return any(g.mig for g in self.gpus)


@dataclass
class Cluster:
    nodes: List[Node]
    network_gbps: float = 0.0

    def node(self, name: str) -> Node:
        for node in self.nodes:
            if node.name == name:
                return node
        raise InputError(f"no node named {name}")


@dataclass
class PodSpec:
    name: str
    gpus: int = 1
    memory_gib: float = 0.0
    model: str = ""
    nvlink: bool = False
    shared: bool = False
    mig: str = ""
    gradient_gb: float = 0.0


@dataclass
class Option:
    node: Node
    gpus: List[int]
    mig_start: Optional[int]
    key: Tuple[Any, ...]


@dataclass
class Placement:
    pod: str
    node: str
    resource: str
    gpus: List[int]
    mig_start: Optional[int] = None
    islands: List[int] = field(default_factory=list)
    link_gbps: float = 0.0
    allreduce_s: float = 0.0
    problems: List[str] = field(default_factory=list)


@dataclass
class Decision:
    pod: PodSpec
    policy: str
    view: str
    placement: Optional[Placement]
    reasons: Dict[str, List[str]]
    message: str = ""


@dataclass
class FragRow:
    size: int
    by_count: int
    by_island: int
    stranded: int
    stranded_pct: float


@dataclass
class Job:
    name: str
    pods: int
    gpus_per_pod: int
    duration: float
    arrival: float = 0.0
    memory_gib: float = 0.0
    model: str = ""
    nvlink: bool = False


@dataclass
class JobState:
    job: Job
    queued_at: float
    placed: List[Placement] = field(default_factory=list)
    reserved_at: Optional[float] = None
    start: Optional[float] = None
    finish: Optional[float] = None
    timeouts: int = 0
    logged_hold: int = 0

    @property
    def held_gpus(self) -> int:
        return sum(len(p.gpus) for p in self.placed)


@dataclass
class GangResult:
    mode: str
    timeout: float
    jobs: List[JobState]
    outcome: str
    detail: str
    makespan: float
    idle_gpu_seconds: float
    busy_gpu_seconds: float
    total_gpus: int
    events: List[str]


@dataclass
class Device:
    driver: str
    pool: str
    node: Optional[str]
    name: str
    attributes: Dict[str, Any]
    capacity: Dict[str, Fraction]

    @property
    def key(self) -> Tuple[str, str, str]:
        return self.driver, self.pool, self.name


@dataclass
class DeviceClass:
    name: str
    selectors: List[str]


@dataclass
class DeviceRequest:
    name: str
    device_class: str
    selectors: List[str]
    mode: str = "ExactCount"
    count: int = 1


@dataclass
class Claim:
    namespace: str
    name: str
    requests: List[DeviceRequest]
    constraints: List[Tuple[List[str], str]]
    results: List[Dict[str, str]] = field(default_factory=list)
    node: Optional[str] = None

    @property
    def key(self) -> str:
        return f"{self.namespace}/{self.name}"

    @property
    def allocated(self) -> bool:
        return bool(self.results)


@dataclass
class Quantity:
    value: Fraction


@dataclass
class DraWorld:
    classes: Dict[str, DeviceClass]
    devices: List[Device]
    claims: List[Claim]


def parse_quantity(text: Any) -> Fraction:
    raw = str(text).strip()
    match = QUANTITY.match(raw)
    if not match:
        raise InputError(f"not a Kubernetes quantity: {raw!r} (examples: 2, 40Gi, 500m)")
    number, exponent, suffix = match.groups()
    value = Fraction(number)
    if exponent:
        return value * Fraction(10) ** int(exponent[1:])
    if suffix in BINARY_SUFFIXES:
        return value * BINARY_SUFFIXES[suffix]
    return value * DECIMAL_SUFFIXES[suffix or ""]


def mig_start(instances: Sequence[MigInstance], profile: MigProfile) -> Optional[int]:
    used: Set[int] = set()
    for instance in instances:
        used |= instance.slices
    compute = sum(A100_80GB_MIG[i.profile].compute for i in instances)
    if compute + profile.compute > MIG_COMPUTE_SLICES:
        return None
    for start in profile.starts:
        if not used & set(range(start, start + profile.memory_slices)):
            return start
    return None


def add_mig_instance(gpu: Gpu, entry: Any, where: str) -> MigInstance:
    if isinstance(entry, dict):
        name, start, holder = str(entry.get("profile", "")), entry.get("start"), str(entry.get("holder") or "")
    else:
        name, _, at = str(entry).partition("@")
        start, holder = (int(at) if at else None), ""
    profile = A100_80GB_MIG.get(name)
    if profile is None:
        raise InputError(f"{where}: unknown MIG profile {name!r} (A100 80GB: {', '.join(A100_80GB_MIG)})")
    if start is None:
        start = mig_start(gpu.mig, profile)
        if start is None:
            raise InputError(f"{where}: no room for {name} next to {', '.join(i.label for i in gpu.mig)}")
    elif start not in profile.starts:
        raise InputError(f"{where}: {name} can start at memory slice {', '.join(map(str, profile.starts))}, not {start}")
    elif mig_start(gpu.mig, MigProfile(name, profile.compute, profile.memory_slices, (start,), profile.memory_gib)) is None:
        raise InputError(f"{where}: {name}@{start} overlaps {', '.join(i.label for i in gpu.mig)} or exceeds {MIG_COMPUTE_SLICES} compute slices")
    instance = MigInstance(name, int(start), holder)
    gpu.mig.append(instance)
    return instance


def make_node(raw: Dict[str, Any]) -> Node:
    name = raw.get("name")
    if not name:
        raise InputError("a node without a name")
    spec = raw.get("gpu") or {}
    count = int(spec.get("count", 0))
    replicas = int(spec.get("time_slicing") or 1)
    layouts = spec.get("mig") or []
    if layouts and replicas > 1:
        raise InputError(f"node {name}: MIG and time-slicing on one node; the device plugin config picks one per node")
    island_of: Dict[int, int] = {}
    groups = spec.get("islands") or []
    for number, members in enumerate(groups):
        for index in members:
            if index in island_of or not 0 <= int(index) < count:
                raise InputError(f"node {name}: GPU {index} is out of range or in two NVLink islands")
            island_of[int(index)] = number
    gpus = [Gpu(i, island_of.get(i, len(groups) + i), replicas) for i in range(count)]
    if len(layouts) > count:
        raise InputError(f"node {name}: {len(layouts)} MIG layouts for {count} GPUs")
    for index, layout in enumerate(layouts):
        for entry in layout:
            add_mig_instance(gpus[index], entry, f"node {name} GPU {index}")
    for index in raw.get("busy") or []:
        if not 0 <= int(index) < count or gpus[int(index)].mig:
            raise InputError(f"node {name}: busy GPU {index} does not exist or is MIG-partitioned")
        gpus[int(index)].holders = [("existing", 0.0)] * replicas
    return Node(
        str(name),
        str(spec.get("model") or ""),
        float(spec.get("memory_gib") or 0),
        gpus,
        float(spec.get("nvlink_gbps") or 0),
        float(spec.get("pcie_gbps") or 0),
        replicas,
        dict(raw.get("labels") or {}),
    )


def load_cluster(doc: Any) -> Cluster:
    if not isinstance(doc, dict) or not isinstance(doc.get("nodes"), list):
        raise InputError('a cluster file is {"nodes": [...]}, one entry per node; see code/fixtures/cluster.json')
    nodes = [make_node(raw) for raw in doc["nodes"]]
    names = [n.name for n in nodes]
    if len(set(names)) != len(names):
        raise InputError("node names must be unique")
    return Cluster(nodes, float(doc.get("network_gbps") or 0))


def load_pods(doc: Any) -> List[PodSpec]:
    items = doc.get("pods") if isinstance(doc, dict) and "pods" in doc else doc
    if isinstance(items, dict):
        items = [items]
    if not isinstance(items, list):
        raise InputError('a pods file is a list of {"name": ..., "gpus": N}')
    pods: List[PodSpec] = []
    for raw in items:
        if not raw.get("name"):
            raise InputError(f"a pod without a name: {raw}")
        mig = str(raw.get("mig") or "")
        if mig and mig not in A100_80GB_MIG:
            raise InputError(f"pod {raw['name']}: unknown MIG profile {mig!r}")
        if mig and raw.get("shared"):
            raise InputError(f"pod {raw['name']}: a MIG instance is not time-sliced")
        gpus = int(raw.get("gpus", 1))
        if gpus < 1:
            raise InputError(f"pod {raw['name']}: gpus must be at least 1 (extended resources are whole numbers)")
        if mig and gpus != 1:
            raise InputError(f"pod {raw['name']}: a MIG pod asks for one instance of its profile")
        copies = int(raw.get("count", 1))
        for copy in range(copies):
            pods.append(
                PodSpec(
                    raw["name"] if copies == 1 else f"{raw['name']}-{copy}",
                    gpus,
                    float(raw.get("memory_gib") or 0),
                    str(raw.get("model") or ""),
                    bool(raw.get("nvlink")),
                    bool(raw.get("shared")),
                    mig,
                    float(raw.get("gradient_gb") or 0),
                )
            )
    return pods


def advertised(node: Node) -> Dict[str, int]:
    counts: Dict[str, int] = {}
    for gpu in node.gpus:
        if gpu.mig:
            for instance in gpu.mig:
                key = f"nvidia.com/mig-{instance.profile}"
                counts[key] = counts.get(key, 0) + 1
        else:
            counts[GPU_RESOURCE] = counts.get(GPU_RESOURCE, 0) + gpu.replicas
    return counts


def resource_name(pod: PodSpec) -> str:
    return f"nvidia.com/mig-{pod.mig}" if pod.mig else GPU_RESOURCE


def by_island(gpus: Iterable[Gpu]) -> Dict[int, List[Gpu]]:
    groups: Dict[int, List[Gpu]] = {}
    for gpu in gpus:
        groups.setdefault(gpu.island, []).append(gpu)
    return groups


def gpu_view_mismatch(node: Node, pod: PodSpec) -> str:
    if pod.shared and not node.shared:
        return "node is not time-sliced"
    if node.shared and not pod.shared:
        return "node is time-sliced"
    if pod.model and pod.model != node.model:
        return f"GPU model is not {pod.model}"
    if pod.memory_gib > node.memory_gib:
        return f"GPU memory below {pod.memory_gib:g} GiB"
    return ""


def choose_gpus(candidates: List[Gpu], count: int, policy: str) -> List[Gpu]:
    if policy != "topology":
        return candidates[:count]
    groups = sorted(by_island(candidates).items())
    fitting = [(len(members), island, members) for island, members in groups if len(members) >= count]
    if fitting:
        return min(fitting, key=lambda f: (f[0], f[1]))[2][:count]
    chosen: List[Gpu] = []
    for _, members in sorted(groups, key=lambda g: (-len(g[1]), g[0])):
        chosen.extend(members[: count - len(chosen)])
        if len(chosen) == count:
            break
    return chosen


def pack_key(policy: str, free_after: int, name: str, spans: int = 0, island_left: int = 0) -> Tuple[Any, ...]:
    if policy == "spread":
        return (-free_after, name)
    if policy == "topology":
        return (spans, island_left, free_after, name)
    return (free_after, name)


def option_for(node: Node, pod: PodSpec, policy: str, view: str) -> Tuple[Optional[Option], str]:
    if pod.mig:
        free = [(g.index, i.start) for g in node.gpus for i in g.mig if i.profile == pod.mig and not i.holder]
        if not free:
            return None, f"Insufficient {resource_name(pod)}"
        gpu, start = free[0]
        return Option(node, [gpu], start, pack_key("binpack" if policy == "topology" else policy, len(free) - 1, node.name)), ""
    if view == "extended":
        units = [g.index for g in node.gpus if not g.mig for _ in range(g.free_replicas)]
        if len(units) < pod.gpus:
            return None, f"Insufficient {GPU_RESOURCE}"
        return Option(node, units[: pod.gpus], None, pack_key(policy, len(units) - pod.gpus, node.name)), ""
    mismatch = gpu_view_mismatch(node, pod)
    if mismatch:
        return None, mismatch
    candidates = [g for g in node.gpus if not g.mig and g.free_replicas > 0 and (not node.shared or g.declared_gib + pod.memory_gib <= node.memory_gib)]
    if len(candidates) < pod.gpus:
        return None, f"Insufficient {GPU_RESOURCE}"
    if pod.nvlink and pod.gpus > 1 and not any(len(m) >= pod.gpus for m in by_island(candidates).values()):
        return None, f"no NVLink island with {pod.gpus} free GPUs"
    chosen = choose_gpus(candidates, pod.gpus, policy)
    islands = {g.island for g in chosen}
    free_after = sum(g.free_replicas for g in candidates) - pod.gpus
    island_left = len(by_island(candidates)[next(iter(islands))]) - pod.gpus if len(islands) == 1 else 0
    key = pack_key(policy, free_after, node.name, 1 if len(islands) > 1 else 0, island_left)
    return Option(node, [g.index for g in chosen], None, key), ""


def link_bandwidth(node: Node, gpus: Sequence[int]) -> float:
    islands = {node.gpus[i].island for i in gpus}
    if len(islands) == 1 and node.nvlink_gbps > 0:
        return node.nvlink_gbps
    return node.pcie_gbps


def allreduce_seconds(gpus: int, gradient_gb: float, bandwidth_gbps: float) -> float:
    if gpus < 2 or gradient_gb <= 0 or bandwidth_gbps <= 0:
        return 0.0
    return 2 * (gpus - 1) / gpus * gradient_gb / bandwidth_gbps


def consequences(node: Node, pod: PodSpec, gpus: Sequence[int]) -> List[str]:
    problems: List[str] = []
    if pod.mig:
        return problems
    if pod.model and node.model != pod.model:
        problems.append(f"runs on {node.model}, wanted {pod.model}")
    if pod.memory_gib > node.memory_gib:
        problems.append(f"needs {pod.memory_gib:g} GiB per GPU, {node.model} has {node.memory_gib:g} GiB: out of memory when the weights load")
    if node.shared:
        if not pod.shared:
            problems.append("lands on a time-sliced replica: shares a physical GPU, no memory or fault isolation")
        if len(set(gpus)) < len(gpus):
            problems.append(f"{len(gpus)} replicas on {len(set(gpus))} physical GPU(s): no extra compute")
        for index in sorted(set(gpus)):
            total = node.gpus[index].declared_gib + pod.memory_gib * list(gpus).count(index)
            if total > node.memory_gib:
                problems.append(f"GPU {index} memory oversubscribed: {total:g} of {node.memory_gib:g} GiB declared")
    if pod.nvlink and len({node.gpus[i].island for i in gpus}) > 1:
        problems.append(f"spans NVLink islands: all-reduce over PCIe at {node.pcie_gbps:g} GB/s instead of NVLink at {node.nvlink_gbps:g} GB/s")
    return problems


def fit_error(total: int, reasons: Iterable[Sequence[str]]) -> str:
    histogram: Dict[str, int] = {}
    for node_reasons in reasons:
        for reason in node_reasons:
            histogram[reason] = histogram.get(reason, 0) + 1
    parts = sorted(f"{count} {reason}" for reason, count in histogram.items())
    message = f"0/{total} nodes are available:"
    if parts:
        message += " " + ", ".join(parts) + "."
    return message


def commit(node: Node, pod: PodSpec, option: Option) -> None:
    if option.mig_start is not None:
        for instance in node.gpus[option.gpus[0]].mig:
            if instance.profile == pod.mig and instance.start == option.mig_start:
                instance.holder = pod.name
                return
    for index in option.gpus:
        node.gpus[index].holders.append((pod.name, pod.memory_gib))


def release(node: Node, placement: Placement) -> None:
    for index in set(placement.gpus):
        gpu = node.gpus[index]
        gpu.holders = [h for h in gpu.holders if h[0] != placement.pod]
        for instance in gpu.mig:
            if instance.holder == placement.pod:
                instance.holder = ""


def schedule(cluster: Cluster, pod: PodSpec, policy: str = "binpack", view: str = "gpu", apply: bool = True) -> Decision:
    if policy not in POLICIES:
        raise InputError(f"unknown policy {policy!r} ({', '.join(POLICIES)})")
    if view == "extended" and policy == "topology":
        raise InputError("--view extended has no topology policy: through extended resources the scheduler sees a count per node, not islands")
    options: List[Option] = []
    reasons: Dict[str, List[str]] = {}
    for node in cluster.nodes:
        option, reason = option_for(node, pod, policy, view)
        if option is None:
            reasons[node.name] = [reason]
        else:
            options.append(option)
    if not options:
        return Decision(pod, policy, view, None, reasons, fit_error(len(cluster.nodes), reasons.values()))
    best = min(options, key=lambda o: o.key)
    node = best.node
    link = link_bandwidth(node, best.gpus) if len(best.gpus) > 1 and not pod.mig else 0.0
    placement = Placement(
        pod.name,
        node.name,
        resource_name(pod),
        list(best.gpus),
        best.mig_start,
        sorted({node.gpus[i].island for i in best.gpus}),
        link,
        allreduce_seconds(len(best.gpus), pod.gradient_gb, link),
        consequences(node, pod, best.gpus),
    )
    if apply:
        commit(node, pod, best)
    return Decision(pod, policy, view, placement, reasons)


def place_all(cluster: Cluster, pods: Sequence[PodSpec], policy: str = "binpack", view: str = "gpu") -> List[Decision]:
    return [schedule(cluster, pod, policy, view) for pod in pods]


def free_exclusive(node: Node) -> List[Gpu]:
    if node.shared:
        return []
    return [g for g in node.gpus if not g.mig and not g.holders]


def mig_report(node: Node, gpu: Gpu) -> Dict[str, Any]:
    used = [i for i in gpu.mig if i.holder]
    free: Dict[str, int] = {}
    for instance in gpu.mig:
        if not instance.holder:
            free[instance.profile] = free.get(instance.profile, 0) + 1
    addable = [p.name for p in A100_80GB_MIG.values() if mig_start(used, p) is not None]
    return {"node": node.name, "gpu": gpu.index, "used": [f"{i.label}={i.holder}" for i in used], "free": free, "repartition_could_add": addable}


def shared_report(node: Node, gpu: Gpu) -> Dict[str, Any]:
    return {
        "node": node.name,
        "gpu": gpu.index,
        "replicas_used": len(gpu.holders),
        "replicas": gpu.replicas,
        "declared_gib": gpu.declared_gib,
        "memory_gib": node.memory_gib,
        "oversubscribed": gpu.declared_gib > node.memory_gib,
    }


def fragmentation(cluster: Cluster, sizes: Sequence[int] = DEFAULT_SIZES) -> Dict[str, Any]:
    per_node = {n.name: free_exclusive(n) for n in cluster.nodes}
    free = sum(len(gpus) for gpus in per_node.values())
    rows: List[FragRow] = []
    for size in sizes:
        by_count = sum(len(gpus) // size for gpus in per_node.values())
        within = sum(len(members) // size for gpus in per_node.values() for members in by_island(gpus).values())
        stranded = free - by_count * size
        rows.append(FragRow(size, by_count, within, stranded, round(100.0 * stranded / free, 1) if free else 0.0))
    return {
        "free": free,
        "largest_on_node": max((len(g) for g in per_node.values()), default=0),
        "largest_in_island": max((len(m) for g in per_node.values() for m in by_island(g).values()), default=0),
        "free_per_node": {name: [g.index for g in gpus] for name, gpus in per_node.items() if not cluster.node(name).shared and not cluster.node(name).mig_enabled},
        "rows": [r.__dict__ for r in rows],
        "mig": [mig_report(n, g) for n in cluster.nodes for g in n.gpus if g.mig],
        "shared": [shared_report(n, g) for n in cluster.nodes if n.shared for g in n.gpus],
    }


def load_jobs(doc: Any) -> List[Job]:
    items = doc.get("jobs") if isinstance(doc, dict) else doc
    if not isinstance(items, list):
        raise InputError('a jobs file is a list of {"name", "pods", "gpus_per_pod", "duration", "arrival"}')
    jobs = []
    for raw in items:
        try:
            jobs.append(
                Job(
                    str(raw["name"]),
                    int(raw["pods"]),
                    int(raw["gpus_per_pod"]),
                    float(raw["duration"]),
                    float(raw.get("arrival") or 0),
                    float(raw.get("memory_gib") or 0),
                    str(raw.get("model") or ""),
                    bool(raw.get("nvlink")),
                )
            )
        except KeyError as exc:
            raise InputError(f"job {raw.get('name', '?')}: missing {exc}")
    return jobs


def place_pod(cluster: Cluster, state: JobState, policy: str) -> bool:
    job = state.job
    pod = PodSpec(f"{job.name}-{len(state.placed)}", job.gpus_per_pod, job.memory_gib, job.model, job.nvlink)
    decision = schedule(cluster, pod, policy, "gpu")
    if decision.placement is None:
        return False
    state.placed.append(decision.placement)
    return True


def release_job(cluster: Cluster, state: JobState) -> None:
    for placement in state.placed:
        release(cluster.node(placement.node), placement)
    state.placed = []
    state.reserved_at = None


def schedule_pass(cluster: Cluster, queue: List[JobState], mode: str, timeout: float, policy: str, now: float) -> None:
    if mode == "pod":
        progress = True
        while progress:
            progress = False
            for state in queue:
                if len(state.placed) < state.job.pods and place_pod(cluster, state, policy):
                    progress = True
    else:
        for state in queue:
            while len(state.placed) < state.job.pods and place_pod(cluster, state, policy):
                pass
            if len(state.placed) < state.job.pods and timeout <= 0:
                release_job(cluster, state)
    for state in queue:
        if state.placed and state.reserved_at is None and len(state.placed) < state.job.pods:
            state.reserved_at = now


def simulate_gang(cluster: Cluster, jobs: Sequence[Job], mode: str = "gang", timeout: float = 0.0, policy: str = "binpack", max_steps: int = 10000) -> GangResult:
    if mode not in GANG_MODES:
        raise InputError(f"unknown mode {mode!r} ({', '.join(GANG_MODES)})")
    future = sorted(jobs, key=lambda j: (j.arrival, j.name))
    queue: List[JobState] = []
    running: List[JobState] = []
    finished: List[JobState] = []
    events: List[str] = []
    total = sum(len(n.gpus) for n in cluster.nodes if not n.shared and not n.mig_enabled)
    now = idle = busy = 0.0
    for _ in range(max_steps):
        for state in [s for s in running if s.finish is not None and s.finish <= now]:
            release_job(cluster, state)
            running.remove(state)
            finished.append(state)
            events.append(f"t={now:g} {state.job.name} finished, {state.job.pods * state.job.gpus_per_pod} GPUs free again")
        if mode == "gang" and timeout > 0:
            for state in [s for s in queue if s.reserved_at is not None and now >= s.reserved_at + timeout]:
                events.append(f"t={now:g} {state.job.name} timed out with {len(state.placed)}/{state.job.pods} pods, releases {state.held_gpus} GPUs")
                release_job(cluster, state)
                state.timeouts += 1
                queue.remove(state)
                queue.append(state)
        while future and future[0].arrival <= now:
            job = future.pop(0)
            queue.append(JobState(job, now))
            events.append(f"t={now:g} {job.name} arrives: {job.pods} pod{'' if job.pods == 1 else 's'} x {job.gpus_per_pod} GPUs")
        schedule_pass(cluster, queue, mode, timeout, policy, now)
        for state in [s for s in queue if len(s.placed) == s.job.pods]:
            queue.remove(state)
            state.start, state.finish, state.reserved_at = now, now + state.job.duration, None
            running.append(state)
            nodes = sorted({p.node for p in state.placed})
            events.append(f"t={now:g} {state.job.name} starts on {', '.join(nodes)}, runs until t={state.finish:g}")
        for state in queue:
            if state.placed and state.logged_hold != state.held_gpus:
                events.append(f"t={now:g} {state.job.name} holds {state.held_gpus} GPUs with {len(state.placed)}/{state.job.pods} pods placed, waiting")
            state.logged_hold = state.held_gpus
        upcoming = [s.finish for s in running if s.finish is not None] + [j.arrival for j in future[:1]]
        if mode == "gang" and timeout > 0 and (running or future):
            upcoming += [s.reserved_at + timeout for s in queue if s.reserved_at is not None]
        if not upcoming:
            break
        step = min(upcoming)
        idle += sum(s.held_gpus for s in queue) * (step - now)
        busy += sum(s.held_gpus for s in running) * (step - now)
        now = step
    if not queue and not future:
        outcome, detail = "finished", f"all {len(finished)} jobs finished"
    elif any(s.placed for s in queue):
        outcome = "deadlock"
        holders = ", ".join(f"{s.job.name} holds {s.held_gpus} GPUs with {len(s.placed)}/{s.job.pods} pods" for s in queue if s.placed)
        detail = f"nothing is running and nothing can start: {holders}; each waits for GPUs another one holds"
    else:
        outcome = "stuck"
        detail = "; ".join(f"{s.job.name} needs {s.job.pods} x {s.job.gpus_per_pod} GPUs at once and never fits" for s in queue)
    makespan = max([s.finish for s in finished if s.finish is not None] + [now])
    return GangResult(mode, timeout, finished + running + queue, outcome, detail, makespan, idle, busy, total, events)


def cel_tokens(text: str) -> List[Tuple[str, str]]:
    tokens: List[Tuple[str, str]] = []
    source = text.strip()
    position = 0
    while position < len(source):
        match = CEL_TOKEN.match(source, position)
        if not match or match.end() == position:
            raise InputError(f"CEL: cannot read {source[position:position + 20]!r} in {text!r}")
        kind = match.lastgroup or ""
        tokens.append((kind, match.group(kind)))
        position = match.end()
    return tokens


class CelParser:
    def __init__(self, text: str) -> None:
        self.text = text
        self.tokens = cel_tokens(text)
        self.position = 0

    def peek(self) -> Tuple[str, str]:
        return self.tokens[self.position] if self.position < len(self.tokens) else ("end", "")

    def at(self, op: str) -> bool:
        return self.peek() == ("op", op)

    def take(self, op: Optional[str] = None) -> Tuple[str, str]:
        token = self.peek()
        if op is not None and token != ("op", op):
            raise InputError(f"CEL: expected {op!r} but found {token[1] or 'the end'!r} in {self.text!r}")
        self.position += 1
        return token

    def parse(self) -> Tuple[Any, ...]:
        node = self.disjunction()
        if self.peek()[0] != "end":
            raise InputError(f"CEL: unexpected {self.peek()[1]!r} in {self.text!r}")
        return node

    def disjunction(self) -> Tuple[Any, ...]:
        node = self.conjunction()
        while self.at("||"):
            self.take()
            node = ("or", node, self.conjunction())
        return node

    def conjunction(self) -> Tuple[Any, ...]:
        node = self.unary()
        while self.at("&&"):
            self.take()
            node = ("and", node, self.unary())
        return node

    def unary(self) -> Tuple[Any, ...]:
        if self.at("!"):
            self.take()
            return ("not", self.unary())
        left = self.postfix()
        if self.peek()[0] == "op" and self.peek()[1] in CEL_COMPARISONS:
            op = self.take()[1]
            return ("cmp", op, left, self.postfix())
        return left

    def postfix(self) -> Tuple[Any, ...]:
        node = self.primary()
        while True:
            if self.at("."):
                self.take()
                kind, name = self.take()
                if kind != "name":
                    raise InputError(f"CEL: expected a field name after '.' in {self.text!r}")
                node = ("call", node, name, self.arguments()) if self.at("(") else ("member", node, name)
            elif self.at("["):
                self.take()
                key = self.disjunction()
                self.take("]")
                node = ("index", node, key)
            else:
                return node

    def primary(self) -> Tuple[Any, ...]:
        kind, value = self.take()
        if kind == "number":
            return ("literal", int(value))
        if kind == "string":
            return ("literal", re.sub(r"\\(.)", r"\1", value[1:-1]))
        if kind == "name":
            if value in ("true", "false"):
                return ("literal", value == "true")
            if self.at("("):
                return ("call", None, value, self.arguments())
            return ("name", value)
        if (kind, value) == ("op", "("):
            node = self.disjunction()
            self.take(")")
            return node
        raise InputError(f"CEL: unexpected {value or 'end of expression'!r} in {self.text!r}")

    def arguments(self) -> List[Tuple[Any, ...]]:
        self.take("(")
        args: List[Tuple[Any, ...]] = []
        if not self.at(")"):
            args.append(self.disjunction())
            while self.at(","):
                self.take()
                args.append(self.disjunction())
        self.take(")")
        return args


CEL_CACHE: Dict[str, Tuple[Any, ...]] = {}


def compile_cel(expression: str) -> Tuple[Any, ...]:
    if expression not in CEL_CACHE:
        CEL_CACHE[expression] = CelParser(expression).parse()
    return CEL_CACHE[expression]


def cel_lookup(target: Any, key: Any) -> Any:
    if isinstance(target, dict):
        if key in target:
            return target[key]
        raise CelError(f"no such key: {key}")
    raise CelError(f"cannot select {key!r} from {type(target).__name__}")


def cel_compare(op: str, left: Any, right: Any) -> bool:
    if type(left) is not type(right) or isinstance(left, (Quantity, dict)):
        raise CelError(f"no such overload: {type(left).__name__} {op} {type(right).__name__}")
    if op == "==":
        return left == right
    if op == "!=":
        return left != right
    if isinstance(left, bool):
        raise CelError(f"no such overload: bool {op} bool")
    return {"<": left < right, "<=": left <= right, ">": left > right, ">=": left >= right}[op]


def cel_call(node: Tuple[Any, ...], env: Dict[str, Any]) -> Any:
    _, target_node, name, arg_nodes = node
    args = [cel_eval(a, env) for a in arg_nodes]
    if target_node is None:
        if name == "quantity" and len(args) == 1 and isinstance(args[0], str):
            try:
                return Quantity(parse_quantity(args[0]))
            except InputError as exc:
                raise CelError(str(exc))
        raise CelError(f"unknown function {name}")
    target = cel_eval(target_node, env)
    if isinstance(target, Quantity) and len(args) == 1 and isinstance(args[0], Quantity):
        other = args[0].value
        if name == "compareTo":
            return (target.value > other) - (target.value < other)
        if name == "isGreaterThan":
            return target.value > other
        if name == "isLessThan":
            return target.value < other
    if isinstance(target, str) and len(args) == 1 and isinstance(args[0], str):
        if name == "startsWith":
            return target.startswith(args[0])
        if name == "endsWith":
            return target.endswith(args[0])
        if name == "contains":
            return args[0] in target
    raise CelError(f"no such overload: {type(target).__name__}.{name}")


def cel_logical(kind: str, left: Tuple[Any, ...], right: Tuple[Any, ...], env: Dict[str, Any]) -> bool:
    results: List[Any] = []
    for side in (left, right):
        try:
            value = cel_eval(side, env)
            if not isinstance(value, bool):
                raise CelError(f"{kind} needs bool operands")
            results.append(value)
        except CelError as exc:
            results.append(exc)
    if kind == "and" and any(r is False for r in results):
        return False
    if kind == "or" and any(r is True for r in results):
        return True
    for result in results:
        if isinstance(result, CelError):
            raise result
    return results[0] and results[1] if kind == "and" else results[0] or results[1]


def cel_eval(node: Tuple[Any, ...], env: Dict[str, Any]) -> Any:
    kind = node[0]
    if kind == "literal":
        return node[1]
    if kind == "name":
        return cel_lookup(env, node[1])
    if kind == "member":
        return cel_lookup(cel_eval(node[1], env), node[2])
    if kind == "index":
        return cel_lookup(cel_eval(node[1], env), cel_eval(node[2], env))
    if kind == "not":
        value = cel_eval(node[1], env)
        if not isinstance(value, bool):
            raise CelError("! needs a bool")
        return not value
    if kind in ("and", "or"):
        return cel_logical(kind, node[1], node[2], env)
    if kind == "cmp":
        return cel_compare(node[1], cel_eval(node[2], env), cel_eval(node[3], env))
    return cel_call(node, env)


def device_env(device: Device) -> Dict[str, Any]:
    attributes: Dict[str, Dict[str, Any]] = {}
    for qualified, value in device.attributes.items():
        domain, _, name = qualified.rpartition("/")
        attributes.setdefault(domain, {})[name] = value
    capacity: Dict[str, Dict[str, Any]] = {}
    for qualified, value in device.capacity.items():
        domain, _, name = qualified.rpartition("/")
        capacity.setdefault(domain, {})[name] = Quantity(value)
    return {"device": {"driver": device.driver, "attributes": attributes, "capacity": capacity}}


def selector_matches(expression: str, device: Device) -> bool:
    tree = compile_cel(expression)
    try:
        return cel_eval(tree, device_env(device)) is True
    except CelError:
        return False


def qualify(name: str, driver: str) -> str:
    return name if "/" in name else f"{driver}/{name}"


def attribute_value(raw: Any, where: str) -> Any:
    if not isinstance(raw, dict) or len(raw) != 1:
        raise InputError(f"{where}: an attribute has exactly one of int, bool, string, version")
    kind, value = next(iter(raw.items()))
    if kind == "int":
        return int(value)
    if kind == "bool":
        return bool(value)
    if kind in ("string", "version"):
        return str(value)
    raise InputError(f"{where}: unknown attribute type {kind!r}")


def devices_from_slice(obj: Dict[str, Any]) -> List[Device]:
    spec = obj.get("spec") or {}
    driver = str(spec.get("driver") or "")
    pool = str((spec.get("pool") or {}).get("name") or "")
    node = spec.get("nodeName") or None
    out: List[Device] = []
    for raw in spec.get("devices") or []:
        body = raw.get("basic") or raw
        where = f"ResourceSlice {(obj.get('metadata') or {}).get('name', '?')} device {raw.get('name', '?')}"
        attributes = {qualify(k, driver): attribute_value(v, where) for k, v in (body.get("attributes") or {}).items()}
        capacity = {qualify(k, driver): parse_quantity(v.get("value") if isinstance(v, dict) else v) for k, v in (body.get("capacity") or {}).items()}
        out.append(Device(driver, pool, node, str(raw.get("name")), attributes, capacity))
    return out


def node_from_selector(selector: Optional[Dict[str, Any]]) -> Optional[str]:
    for term in (selector or {}).get("nodeSelectorTerms") or []:
        for requirement in term.get("matchFields") or []:
            if requirement.get("key") == "metadata.name" and requirement.get("operator") == "In" and requirement.get("values"):
                return str(requirement["values"][0])
    return None


def make_claim(obj: Dict[str, Any], default_namespace: str = "default") -> Claim:
    meta = obj.get("metadata") or {}
    spec = obj.get("spec") or {}
    if obj.get("kind") == "ResourceClaimTemplate":
        spec = spec.get("spec") or {}
    key = f"{meta.get('namespace') or default_namespace}/{meta.get('name') or 'unnamed'}"
    devices = spec.get("devices") or {}
    requests: List[DeviceRequest] = []
    for raw in devices.get("requests") or []:
        if "firstAvailable" in raw:
            raise InputError(f"claim {key}: request {raw.get('name')} uses firstAvailable, which gpusched does not implement")
        body = raw.get("exactly") or raw
        selectors = [str((s.get("cel") or {}).get("expression") or "") for s in body.get("selectors") or []]
        requests.append(DeviceRequest(str(raw.get("name")), str(body.get("deviceClassName") or ""), selectors, str(body.get("allocationMode") or "ExactCount"), int(body.get("count") or 1)))
    constraints = [(list(c.get("requests") or []), str(c["matchAttribute"])) for c in devices.get("constraints") or [] if c.get("matchAttribute")]
    allocation = (obj.get("status") or {}).get("allocation") or {}
    results = [dict(r) for r in (allocation.get("devices") or {}).get("results") or []]
    namespace, _, name = key.partition("/")
    return Claim(namespace, name, requests, constraints, results, node_from_selector(allocation.get("nodeSelector")))


def flatten(docs: Iterable[Any]) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for doc in docs:
        if isinstance(doc, list):
            out.extend(flatten(doc))
        elif isinstance(doc, dict) and isinstance(doc.get("items"), list):
            out.extend(flatten(doc["items"]))
        elif isinstance(doc, dict):
            out.append(doc)
    return out


def load_dra(docs: Iterable[Any]) -> DraWorld:
    classes: Dict[str, DeviceClass] = {}
    devices: List[Device] = []
    claims: List[Claim] = []
    for obj in flatten(docs):
        kind = obj.get("kind")
        if kind == "DeviceClass":
            selectors = [str((s.get("cel") or {}).get("expression") or "") for s in (obj.get("spec") or {}).get("selectors") or []]
            classes[(obj.get("metadata") or {}).get("name", "")] = DeviceClass((obj.get("metadata") or {}).get("name", ""), selectors)
        elif kind == "ResourceSlice":
            devices.extend(devices_from_slice(obj))
        elif kind in ("ResourceClaim", "ResourceClaimTemplate"):
            claims.append(make_claim(obj))
    return DraWorld(classes, devices, claims)


def in_use(world: DraWorld, skip: Iterable[str] = ()) -> Set[Tuple[str, str, str]]:
    skipped = set(skip)
    used: Set[Tuple[str, str, str]] = set()
    for claim in world.claims:
        if claim.key in skipped:
            continue
        for result in claim.results:
            used.add((result.get("driver", ""), result.get("pool", ""), result.get("device", "")))
    return used


def constraints_hold(claim: Claim, picks: Sequence[Tuple[str, Device]]) -> bool:
    for requests, attribute in claim.constraints:
        values = []
        for request, device in picks:
            if requests and request not in requests:
                continue
            if attribute not in device.attributes:
                return False
            values.append(device.attributes[attribute])
        if len(set(values)) > 1:
            return False
    return True


def matching_devices(world: DraWorld, request: DeviceRequest, node: Optional[str]) -> List[Device]:
    device_class = world.classes.get(request.device_class)
    if device_class is None:
        raise InputError(f"request {request.name}: DeviceClass {request.device_class!r} not found")
    selectors = device_class.selectors + request.selectors
    return [d for d in world.devices if d.node in (None, node) and all(selector_matches(s, d) for s in selectors)]


def allocate(world: DraWorld, claim: Claim, node: Optional[str], used: Set[Tuple[str, str, str]]) -> Optional[List[Tuple[str, Device]]]:
    candidates = [matching_devices(world, request, node) for request in claim.requests]

    def search(index: int, picks: List[Tuple[str, Device]], taken: Set[Tuple[str, str, str]]) -> Optional[List[Tuple[str, Device]]]:
        if index == len(claim.requests):
            return picks
        request = claim.requests[index]
        if request.mode == "All":
            everything = candidates[index]
            if not everything or any(d.key in used or d.key in taken for d in everything):
                return None
            combos: Iterable[Sequence[Device]] = [everything]
        else:
            free = [d for d in candidates[index] if d.key not in used and d.key not in taken]
            combos = itertools.combinations(free, request.count)
        for combo in combos:
            attempt = picks + [(request.name, d) for d in combo]
            if not constraints_hold(claim, attempt):
                continue
            found = search(index + 1, attempt, taken | {d.key for d in combo})
            if found is not None:
                return found
        return None

    return search(0, [], set())


def explain_allocation_failure(world: DraWorld, claim: Claim, node: Optional[str], used: Set[Tuple[str, str, str]]) -> str:
    parts = []
    for request in claim.requests:
        matching = matching_devices(world, request, node)
        free = [d for d in matching if d.key not in used]
        need = "all" if request.mode == "All" else str(request.count)
        parts.append(f"request {request.name}: {len(matching)} device(s) match class {request.device_class} and the selectors, {len(free)} free, need {need}")
    if claim.constraints and all(len([d for d in matching_devices(world, r, node) if d.key not in used]) >= r.count for r in claim.requests if r.mode != "All"):
        parts.append("enough devices, but no combination satisfies " + ", ".join(f"matchAttribute {a}" for _, a in claim.constraints))
    return "; ".join(parts)


def allocation_result(picks: Sequence[Tuple[str, Device]], node: Optional[str]) -> Dict[str, Any]:
    result: Dict[str, Any] = {"devices": {"results": [{"request": r, "driver": d.driver, "pool": d.pool, "device": d.name} for r, d in picks]}}
    if node and any(d.node for _, d in picks):
        result["nodeSelector"] = {"nodeSelectorTerms": [{"matchFields": [{"key": "metadata.name", "operator": "In", "values": [node]}]}]}
    return result


def device_nodes(world: DraWorld) -> List[Optional[str]]:
    names = sorted({d.node for d in world.devices if d.node})
    return list(names) if names else [None]


def allocate_claims(world: DraWorld, only_node: Optional[str] = None) -> List[Dict[str, Any]]:
    used = in_use(world)
    reports: List[Dict[str, Any]] = []
    for claim in world.claims:
        if claim.allocated:
            reports.append({"claim": claim.key, "allocated": True, "node": claim.node, "already": True, "allocation": {"devices": {"results": claim.results}}, "why": "already allocated"})
            continue
        nodes = [only_node] if only_node else device_nodes(world)
        report: Dict[str, Any] = {"claim": claim.key, "allocated": False, "node": None, "already": False, "allocation": None, "why": ""}
        reasons = []
        for node in nodes:
            picks = allocate(world, claim, node, used)
            if picks is not None:
                used |= {d.key for _, d in picks}
                report.update(allocated=True, node=node, allocation=allocation_result(picks, node), why="")
                break
            if any(matching_devices(world, r, node) for r in claim.requests):
                reasons.append(f"{node or 'any node'}: {explain_allocation_failure(world, claim, node, used)}")
        if not report["allocated"]:
            report["why"] = REASON_CLAIMS + ": " + (" | ".join(reasons) or "no device on any node matches the DeviceClass and the selectors")
        reports.append(report)
    return reports


def cluster_slices(cluster: Cluster, driver: str = DRIVER, api_version: str = "resource.k8s.io/v1") -> List[Dict[str, Any]]:
    slices = []
    for node in cluster.nodes:
        devices = []
        for gpu in node.gpus:
            if gpu.mig:
                for instance in gpu.mig:
                    profile = A100_80GB_MIG[instance.profile]
                    attributes = {"type": {"string": "mig"}, "model": {"string": node.model}, "profile": {"string": instance.profile}, "parentIndex": {"int": gpu.index}}
                    devices.append(slice_device(f"gpu-{gpu.index}-mig-{instance.profile.replace('.', '-')}-{instance.start}", attributes, f"{profile.memory_gib}Gi", api_version))
            else:
                attributes = {"type": {"string": "gpu"}, "model": {"string": node.model}, "index": {"int": gpu.index}, "nvlinkIsland": {"int": gpu.island}, "timeSliced": {"bool": node.shared}}
                devices.append(slice_device(f"gpu-{gpu.index}", attributes, f"{node.memory_gib:g}Gi", api_version))
        slices.append(
            {
                "apiVersion": api_version,
                "kind": "ResourceSlice",
                "metadata": {"name": f"{node.name}-{driver}", "labels": {"cloudcaptain.io/lesson": "gpu-scheduling"}},
                "spec": {"driver": driver, "nodeName": node.name, "pool": {"name": node.name, "generation": 1, "resourceSliceCount": 1}, "devices": devices},
            }
        )
    return slices


def slice_device(name: str, attributes: Dict[str, Any], memory: str, api_version: str) -> Dict[str, Any]:
    body = {"attributes": attributes, "capacity": {"memory": {"value": memory}}}
    if api_version.endswith("v1beta1"):
        return {"name": name, "basic": body}
    return dict({"name": name}, **body)


def cluster_in_use(cluster: Cluster, driver: str = DRIVER) -> Set[Tuple[str, str, str]]:
    used = set()
    for node in cluster.nodes:
        for gpu in node.gpus:
            if gpu.holders:
                used.add((driver, node.name, f"gpu-{gpu.index}"))
            for instance in gpu.mig:
                if instance.holder:
                    used.add((driver, node.name, f"gpu-{gpu.index}-mig-{instance.profile.replace('.', '-')}-{instance.start}"))
    return used


def default_classes(driver: str = DRIVER) -> List[DeviceClass]:
    domain = driver.split(".", 1)[1] if "." in driver else driver
    return [
        DeviceClass(f"{kind}.{domain}", [f'device.driver == "{driver}" && device.attributes["{driver}"].type == "{kind}"'])
        for kind in ("gpu", "mig")
    ]


def tolerates(toleration: Dict[str, Any], taint: Dict[str, Any]) -> bool:
    effect = toleration.get("effect") or ""
    if effect and effect != taint.get("effect"):
        return False
    key = toleration.get("key") or ""
    if key and key != taint.get("key"):
        return False
    operator = toleration.get("operator") or "Equal"
    if operator == "Exists":
        return True
    return operator == "Equal" and (toleration.get("value") or "") == (taint.get("value") or "")


def is_extended(name: str) -> bool:
    domain = name.split("/", 1)[0] if "/" in name else ""
    return bool(domain) and not domain.endswith("kubernetes.io") and not name.startswith("requests.")


def container_extended(container: Dict[str, Any]) -> Dict[str, int]:
    resources = container.get("resources") or {}
    merged = dict(resources.get("limits") or {})
    merged.update(resources.get("requests") or {})
    return {name: math.ceil(parse_quantity(value)) for name, value in merged.items() if is_extended(name)}


def pod_extended(spec: Dict[str, Any]) -> Dict[str, int]:
    total: Dict[str, int] = {}
    for container in spec.get("containers") or []:
        for name, value in container_extended(container).items():
            total[name] = total.get(name, 0) + value
    for container in spec.get("initContainers") or []:
        for name, value in container_extended(container).items():
            total[name] = max(total.get(name, 0), value)
    return total


def node_extended(obj: Dict[str, Any]) -> Dict[str, int]:
    status = obj.get("status") or {}
    allocatable = status.get("allocatable") or status.get("capacity") or {}
    return {name: math.floor(parse_quantity(value)) for name, value in allocatable.items() if is_extended(name)}


def pod_claims(pod: Dict[str, Any], world: DraWorld) -> Tuple[List[Claim], List[str]]:
    namespace = (pod.get("metadata") or {}).get("namespace") or "default"
    generated = {s.get("name"): s.get("resourceClaimName") for s in (pod.get("status") or {}).get("resourceClaimStatuses") or []}
    claims: List[Claim] = []
    missing: List[str] = []
    for ref in (pod.get("spec") or {}).get("resourceClaims") or []:
        name = ref.get("resourceClaimName") or generated.get(ref.get("name"))
        template = ref.get("resourceClaimTemplateName")
        wanted = name or template
        found = next((c for c in world.claims if c.namespace == namespace and c.name == wanted), None)
        if found is None:
            missing.append(f"{namespace}/{wanted}")
        elif template and not name:
            claims.append(Claim(namespace, f"{(pod.get('metadata') or {}).get('name')}-{ref.get('name')}", found.requests, found.constraints))
        else:
            claims.append(found)
    return claims, missing


def k8s_predict(nodes: List[Dict[str, Any]], pods: List[Dict[str, Any]], pod: Dict[str, Any], world: DraWorld) -> Dict[str, Any]:
    key = f"{(pod.get('metadata') or {}).get('namespace') or 'default'}/{(pod.get('metadata') or {}).get('name')}"
    spec = pod.get("spec") or {}
    wanted = pod_extended(spec)
    tolerations = spec.get("tolerations") or []
    claims, missing = pod_claims(pod, world)
    if missing:
        raise InputError(f"pod {key} references ResourceClaim(s) {', '.join(missing)} that are not in --objects; export them with kubectl get resourceclaims -n NAMESPACE -o json")
    used: Dict[str, Dict[str, int]] = {}
    for other in pods:
        meta = other.get("metadata") or {}
        other_key = f"{meta.get('namespace') or 'default'}/{meta.get('name')}"
        node_name = (other.get("spec") or {}).get("nodeName")
        if not node_name or other_key == key or (other.get("status") or {}).get("phase") in TERMINAL_PHASES:
            continue
        bucket = used.setdefault(node_name, {})
        for name, value in pod_extended(other.get("spec") or {}).items():
            bucket[name] = bucket.get(name, 0) + value
    taken = in_use(world, skip=[c.key for c in claims])
    rows: List[Dict[str, Any]] = []
    for obj in nodes:
        name = (obj.get("metadata") or {}).get("name", "")
        node_spec = obj.get("spec") or {}
        row: Dict[str, Any] = {"node": name, "feasible": False, "reasons": [], "detail": "", "allocations": []}
        rows.append(row)
        if node_spec.get("unschedulable") and not any(tolerates(t, UNSCHEDULABLE_TAINT) for t in tolerations):
            row.update(reasons=[REASON_UNSCHEDULABLE], detail="spec.unschedulable is true")
            continue
        taint = next((t for t in node_spec.get("taints") or [] if t.get("effect") in FILTER_EFFECTS and not any(tolerates(x, t) for x in tolerations)), None)
        if taint is not None:
            row.update(reasons=[REASON_TAINT], detail=f"{taint.get('key')}={taint.get('value', '')}:{taint.get('effect')}")
            continue
        allocatable = node_extended(obj)
        short = []
        for resource in sorted(wanted):
            free = allocatable.get(resource, 0) - used.get(name, {}).get(resource, 0)
            if wanted[resource] > free:
                short.append((resource, free))
        if short:
            row["reasons"] = [f"Insufficient {r}" for r, _ in short]
            row["detail"] = "; ".join(f"{r}: requested {wanted[r]}, free {f} of {allocatable.get(r, 0)}" for r, f in short)
            continue
        claimed = set(taken)
        failure = ""
        for claim in claims:
            if claim.allocated:
                if claim.node and claim.node != name:
                    failure = REASON_CLAIM_ELSEWHERE
                    row["detail"] = f"claim {claim.key} is allocated on {claim.node}"
                    break
                row["allocations"].append({"claim": claim.key, "allocation": {"devices": {"results": claim.results}}, "already": True})
                continue
            picks = allocate(world, claim, name, claimed)
            if picks is None:
                failure = REASON_CLAIMS
                row["detail"] = f"claim {claim.key}: " + explain_allocation_failure(world, claim, name, claimed)
                break
            claimed |= {d.key for _, d in picks}
            row["allocations"].append({"claim": claim.key, "allocation": allocation_result(picks, name), "already": False})
        if failure:
            row["reasons"] = [failure]
            row["allocations"] = []
            continue
        row["feasible"] = True
    feasible = [r for r in rows if r["feasible"]]
    message = "" if feasible else fit_error(len(rows), [r["reasons"] for r in rows])
    return {
        "pod": key,
        "requests": wanted,
        "claims": [c.key for c in claims],
        "fits": bool(feasible),
        "node": feasible[0]["node"] if feasible else None,
        "message": message,
        "nodes": rows,
    }


def read_json(path: str) -> Any:
    try:
        if path == "-":
            return json.load(sys.stdin)
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)
    except OSError as exc:
        raise InputError(f"cannot read {path}: {exc.strerror}")
    except json.JSONDecodeError as exc:
        raise InputError(f"{path} is not JSON ({exc.msg}, line {exc.lineno})")


def describe_cluster(cluster: Cluster) -> str:
    parts = []
    for node in cluster.nodes:
        kind = "MIG" if node.mig_enabled else f"time-sliced x{node.time_slicing}" if node.shared else f"{len({g.island for g in node.gpus})} NVLink island(s)" if node.nvlink_gbps else "no NVLink"
        parts.append(f"{node.name} ({len(node.gpus)} x {node.model}, {node.memory_gib:g} GiB, {kind})")
    return ", ".join(parts)


def render_decision(decision: Decision) -> List[str]:
    pod = decision.pod
    placement = decision.placement
    if placement is None:
        return [f"  {pod.name:<14} Pending  {decision.message}"]
    where = f"{placement.resource} GPU {placement.gpus[0]} instance {pod.mig}@{placement.mig_start}" if placement.mig_start is not None else f"{placement.resource} GPUs {placement.gpus} islands {placement.islands}"
    lines = [f"  {pod.name:<14} -> {placement.node:<8} {where}"]
    if placement.allreduce_s:
        lines.append(f"  {'':<14}    all-reduce of {pod.gradient_gb:g} GB over {placement.link_gbps:g} GB/s: {placement.allreduce_s:.3f} s per step")
    for problem in placement.problems:
        lines.append(f"  {'':<14}    ! {problem}")
    return lines


def render_fragmentation(report: Dict[str, Any]) -> List[str]:
    lines = [f"fragmentation of exclusive GPUs: {report['free']} free; largest block on one node {report['largest_on_node']}, inside one NVLink island {report['largest_in_island']}"]
    lines.append("  size  pods(count)  pods(island)  stranded")
    for row in report["rows"]:
        lines.append(f"  {row['size']:>4}  {row['by_count']:>11}  {row['by_island']:>12}  {row['stranded']:>4} ({row['stranded_pct']:g}%)")
    for mig in report["mig"]:
        free = ", ".join(f"{p} x{n}" for p, n in mig["free"].items()) or "none"
        lines.append(f"  MIG {mig['node']} GPU {mig['gpu']}: used {', '.join(mig['used']) or 'none'}; free instances {free}; a repartition could add {', '.join(mig['repartition_could_add']) or 'nothing'}")
    for shared in report["shared"]:
        flag = "  OVERSUBSCRIBED" if shared["oversubscribed"] else ""
        lines.append(f"  time-sliced {shared['node']} GPU {shared['gpu']}: {shared['replicas_used']}/{shared['replicas']} replicas, {shared['declared_gib']:g} of {shared['memory_gib']:g} GiB declared{flag}")
    return lines


def decision_json(decision: Decision) -> Dict[str, Any]:
    placement = decision.placement
    return {
        "pod": decision.pod.name,
        "scheduled": placement is not None,
        "placement": None if placement is None else placement.__dict__,
        "message": decision.message,
        "reasons": decision.reasons,
    }


def parse_sizes(text: str) -> List[int]:
    try:
        sizes = [int(s) for s in text.split(",") if s.strip()]
    except ValueError:
        raise InputError(f"--sizes takes integers like 1,2,4,8, not {text!r}")
    if not sizes or min(sizes) < 1:
        raise InputError("--sizes needs positive integers")
    return sizes


def cmd_place(args: argparse.Namespace) -> int:
    cluster = load_cluster(read_json(args.cluster))
    pods = load_pods(read_json(args.pods))
    decisions = place_all(cluster, pods, args.policy, args.view)
    report = fragmentation(cluster, parse_sizes(args.sizes))
    if args.json:
        print(json.dumps({"policy": args.policy, "view": args.view, "decisions": [decision_json(d) for d in decisions], "fragmentation": report}, indent=2))
    else:
        print(f"policy {args.policy}, view {args.view}: {describe_cluster(cluster)}")
        for decision in decisions:
            print("\n".join(render_decision(decision)))
        print("\n".join(render_fragmentation(report)))
    return 0 if all(d.placement for d in decisions) else 1


def cmd_frag(args: argparse.Namespace) -> int:
    cluster = load_cluster(read_json(args.cluster))
    report = fragmentation(cluster, parse_sizes(args.sizes))
    print(json.dumps(report, indent=2) if args.json else "\n".join(render_fragmentation(report)))
    return 0


def cmd_slices(args: argparse.Namespace) -> int:
    cluster = load_cluster(read_json(args.cluster))
    print(json.dumps({"apiVersion": "v1", "kind": "List", "items": cluster_slices(cluster, args.driver, args.api_version)}, indent=2))
    return 0


def cmd_dra(args: argparse.Namespace) -> int:
    docs = [read_json(path) for path in args.objects]
    world = load_dra(docs)
    if args.cluster:
        cluster = load_cluster(read_json(args.cluster))
        world.devices.extend(d for s in cluster_slices(cluster, args.driver) for d in devices_from_slice(s))
        for device_class in default_classes(args.driver):
            world.classes.setdefault(device_class.name, device_class)
        busy = cluster_in_use(cluster, args.driver)
        if busy:
            world.claims.insert(0, Claim("gpusched", "busy-gpus", [], [], [{"driver": d, "pool": p, "device": n} for d, p, n in sorted(busy)]))
    reports = [r for r in allocate_claims(world, args.node) if r["claim"] != "gpusched/busy-gpus"]
    if args.json:
        print(json.dumps({"claims": reports}, indent=2))
    else:
        for report in reports:
            if report["allocated"] and not report["already"]:
                devices = ", ".join(f"{r['pool']}/{r['device']}" for r in report["allocation"]["devices"]["results"])
                print(f"claim {report['claim']}: allocated on {report['node']}: {devices}")
            elif report["allocated"]:
                print(f"claim {report['claim']}: already allocated")
            else:
                print(f"claim {report['claim']}: {report['why']}")
    return 0 if all(r["allocated"] for r in reports) else 1


def cmd_gang(args: argparse.Namespace) -> int:
    cluster = load_cluster(read_json(args.cluster))
    jobs = load_jobs(read_json(args.jobs))
    result = simulate_gang(cluster, jobs, args.mode, args.timeout, args.policy)
    if args.json:
        print(json.dumps({
            "mode": result.mode,
            "timeout": result.timeout,
            "outcome": result.outcome,
            "detail": result.detail,
            "makespan": result.makespan,
            "idle_gpu_hours": round(result.idle_gpu_seconds / 3600, 3),
            "busy_gpu_hours": round(result.busy_gpu_seconds / 3600, 3),
            "jobs": [{"name": s.job.name, "start": s.start, "finish": s.finish, "wait": None if s.start is None else s.start - s.job.arrival, "timeouts": s.timeouts, "held_gpus": s.held_gpus} for s in result.jobs],
            "events": result.events,
        }, indent=2))
    else:
        print(f"mode {result.mode}" + (f", timeout {result.timeout:g} s" if result.mode == "gang" else "") + f", {result.total_gpus} GPUs")
        for event in result.events:
            print(f"  {event}")
        for state in result.jobs:
            when = f"start t={state.start:g}, finish t={state.finish:g}, waited {state.start - state.job.arrival:g} s" if state.start is not None else "never started"
            print(f"  job {state.job.name:<8} {when}" + (f", {state.timeouts} timeout(s)" if state.timeouts else ""))
        print(f"{result.outcome}: {result.detail}")
        print(f"makespan {result.makespan:g} s, GPU-hours held idle by partial gangs {result.idle_gpu_seconds / 3600:.2f}, GPU-hours running {result.busy_gpu_seconds / 3600:.2f}")
    return 0 if result.outcome == "finished" else 1


def cmd_k8s(args: argparse.Namespace) -> int:
    nodes = flatten([read_json(args.nodes)])
    pods = flatten([read_json(args.pods)]) if args.pods else []
    pod = read_json(args.pod)
    world = load_dra([read_json(p) for p in args.objects])
    result = k8s_predict(nodes, pods, pod, world)
    if args.json:
        print(json.dumps(result, indent=2))
    else:
        asked = ", ".join(f"{k} {v}" for k, v in result["requests"].items()) or "no extended resources"
        print(f"pod {result['pod']} requests {asked}" + (f", claims {', '.join(result['claims'])}" if result["claims"] else ""))
        for row in result["nodes"]:
            state = "ok" if row["feasible"] else ", ".join(row["reasons"])
            detail = f" ({row['detail']})" if row["detail"] else ""
            print(f"  {row['node']:<24} {state}{detail}")
            for allocation in row["allocations"] if row["feasible"] else []:
                devices = ", ".join(f"{r['pool']}/{r['device']}" for r in allocation["allocation"]["devices"]["results"])
                print(f"  {'':<24} claim {allocation['claim']} -> {devices}")
        print(f"fits on {result['node']}" if result["fits"] else f"FailedScheduling: {result['message']}")
    return 0 if result["fits"] else 1


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="gpusched", description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)

    place = commands.add_parser("place", help="place pods one after another and report fragmentation")
    place.add_argument("cluster")
    place.add_argument("pods")
    place.add_argument("--policy", choices=POLICIES, default="binpack")
    place.add_argument("--view", choices=VIEWS, default="gpu", help="gpu: everything gpusched knows; extended: only the nvidia.com/gpu count kube-scheduler sees")
    place.add_argument("--sizes", default="1,2,4,8", help="job sizes for the fragmentation table")
    place.add_argument("--json", action="store_true")
    place.set_defaults(func=cmd_place)

    frag = commands.add_parser("frag", help="fragmentation of a cluster file whose nodes list busy GPUs")
    frag.add_argument("cluster")
    frag.add_argument("--sizes", default="1,2,4,8")
    frag.add_argument("--json", action="store_true")
    frag.set_defaults(func=cmd_frag)

    slices = commands.add_parser("slices", help="the ResourceSlices a DRA driver would publish for the cluster model")
    slices.add_argument("cluster")
    slices.add_argument("--driver", default=DRIVER)
    slices.add_argument("--api-version", default="resource.k8s.io/v1", choices=("resource.k8s.io/v1", "resource.k8s.io/v1beta2", "resource.k8s.io/v1beta1"))
    slices.set_defaults(func=cmd_slices)

    dra = commands.add_parser("dra", help="allocate ResourceClaims against ResourceSlices the way the DynamicResources plugin does")
    dra.add_argument("--objects", action="append", default=[], metavar="FILE", help="DeviceClasses, ResourceSlices, ResourceClaims as JSON (repeatable)")
    dra.add_argument("--cluster", help="also publish the cluster model's GPUs as ResourceSlices")
    dra.add_argument("--node", help="allocate on this node only")
    dra.add_argument("--driver", default=DRIVER)
    dra.add_argument("--json", action="store_true")
    dra.set_defaults(func=cmd_dra)

    gang = commands.add_parser("gang", help="simulate distributed training jobs: pod-by-pod versus all-or-nothing")
    gang.add_argument("cluster")
    gang.add_argument("jobs")
    gang.add_argument("--mode", choices=GANG_MODES, default="gang")
    gang.add_argument("--timeout", type=float, default=0.0, help="gang mode: seconds a partial gang may hold GPUs before releasing them (0: never hold)")
    gang.add_argument("--policy", choices=POLICIES, default="binpack")
    gang.add_argument("--json", action="store_true")
    gang.set_defaults(func=cmd_gang)

    k8s = commands.add_parser("k8s", help="predict the Pending reason for a pod from kubectl JSON: extended resources and ResourceClaims")
    k8s.add_argument("--nodes", required=True, help="kubectl get nodes -o json")
    k8s.add_argument("--pods", help="kubectl get pods -A -o json")
    k8s.add_argument("--pod", required=True, help="the pod to place, as JSON; - reads stdin")
    k8s.add_argument("--objects", action="append", default=[], metavar="FILE", help="kubectl get deviceclasses,resourceslices,resourceclaims -A -o json (repeatable)")
    k8s.add_argument("--json", action="store_true")
    k8s.set_defaults(func=cmd_k8s)

    args = parser.parse_args(list(sys.argv[1:] if argv is None else argv))
    try:
        return int(args.func(args))
    except InputError as exc:
        print(f"gpusched: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
