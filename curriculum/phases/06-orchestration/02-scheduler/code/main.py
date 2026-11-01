"""minisched: run one pod through kube-scheduler's filter and score plugins against `kubectl get nodes -o json` and the pods already bound, then print the node it would bind to with per-plugin scores, or the FailedScheduling message."""
from __future__ import annotations

import argparse
import json
import math
import re
import sys
from dataclasses import dataclass, field
from fractions import Fraction
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple

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
BYTE_RESOURCES = ("memory", "ephemeral-storage")
DEFAULT_MILLI_CPU = 100
DEFAULT_MEMORY = 200 * 1024 * 1024
MAX_NODE_SCORE = 100
UNSCHEDULABLE = "Unschedulable"
UNRESOLVABLE = "UnschedulableAndUnresolvable"
TERMINAL_PHASES = ("Succeeded", "Failed")
FILTER_EFFECTS = ("NoSchedule", "NoExecute")
UNSCHEDULABLE_TAINT = {"key": "node.kubernetes.io/unschedulable", "effect": "NoSchedule"}
WORKLOAD_KINDS = ("Deployment", "ReplicaSet", "StatefulSet", "DaemonSet", "Job")
SCORED_RESOURCES = ("cpu", "memory")
STRATEGIES = ("LeastAllocated", "MostAllocated")
WEIGHTS = {
    "NodeResourcesFit": 1,
    "NodeResourcesBalancedAllocation": 1,
    "NodeAffinity": 2,
    "TaintToleration": 3,
    "PodTopologySpread": 2,
}
COLUMNS = {
    "NodeResourcesFit": "Fit",
    "NodeResourcesBalancedAllocation": "Balance",
    "NodeAffinity": "Affinity",
    "TaintToleration": "Taint",
    "PodTopologySpread": "Spread",
}
REASON_UNSCHEDULABLE = "node(s) were unschedulable"
REASON_NODE_NAME = "node(s) didn't match the requested node name"
REASON_AFFINITY = "node(s) didn't match Pod's node affinity/selector"
REASON_SPREAD = "node(s) didn't match pod topology spread constraints"
REASON_SPREAD_LABEL = REASON_SPREAD + " (missing required label)"
REASON_NOT_HELPFUL = "Preemption is not helpful for scheduling"
REASON_NO_VICTIMS = "No preemption victims found for incoming pod"
NO_NODES = "no nodes available to schedule pods"


class InputError(ValueError):
    pass


@dataclass
class Node:
    name: str
    labels: Dict[str, str]
    taints: List[Dict[str, Any]]
    unschedulable: bool
    allocatable: Dict[str, int]
    capacity: Dict[str, int]


@dataclass
class Pod:
    namespace: str
    name: str
    labels: Dict[str, str]
    spec: Dict[str, Any]
    phase: str = ""
    terminating: bool = False
    requests: Dict[str, int] = field(default_factory=dict)
    non_zero: Dict[str, int] = field(default_factory=dict)

    @property
    def key(self) -> str:
        return f"{self.namespace}/{self.name}"

    @property
    def node_name(self) -> str:
        return self.spec.get("nodeName") or ""

    @property
    def priority(self) -> int:
        return int(self.spec.get("priority") or 0)

    @property
    def tolerations(self) -> List[Dict[str, Any]]:
        return list(self.spec.get("tolerations") or [])


@dataclass
class NodeInfo:
    node: Node
    pods: List[Pod]

    @property
    def requested(self) -> Dict[str, int]:
        return total_of(p.requests for p in self.pods)

    @property
    def non_zero_requested(self) -> Dict[str, int]:
        return total_of(p.non_zero for p in self.pods)


@dataclass
class Cluster:
    nodes: List[Node]
    pods: List[Pod]

    def info(self, name: str) -> NodeInfo:
        node = next(n for n in self.nodes if n.name == name)
        return NodeInfo(node, [p for p in self.pods if p.node_name == name])

    def infos(self) -> List[NodeInfo]:
        return [self.info(n.name) for n in self.nodes]

    def without(self, removed: Iterable[Pod]) -> "Cluster":
        keys = {p.key for p in removed}
        return Cluster(self.nodes, [p for p in self.pods if p.key not in keys])


@dataclass
class Status:
    plugin: str
    code: str
    reasons: List[str]
    detail: str = ""


@dataclass
class SpreadConstraint:
    max_skew: int
    topology_key: str
    selector: Optional[Dict[str, Any]]
    min_domains: int
    honor_affinity: bool
    honor_taints: bool


@dataclass
class CycleState:
    hard_spread: List[SpreadConstraint]
    hard_counts: List[Dict[str, int]]


@dataclass
class NodeScore:
    node: str
    plugins: Dict[str, int]
    total: int


@dataclass
class Preemption:
    message: str
    node: Optional[str] = None
    victims: List[str] = field(default_factory=list)


@dataclass
class Result:
    pod: Pod
    strategy: str
    statuses: Dict[str, Optional[Status]]
    scores: List[NodeScore]
    node: Optional[str]
    message: str = ""
    filter_message: str = ""
    preemption: Optional[Preemption] = None


def parse_quantity(text: Any) -> Fraction:
    raw = str(text).strip()
    match = QUANTITY.match(raw)
    if not match:
        raise InputError(f"not a Kubernetes quantity: {raw!r} (examples: 500m, 2, 1.5, 128Mi, 1Gi, 1e3)")
    number, exponent, suffix = match.groups()
    value = Fraction(number)
    if exponent:
        return value * Fraction(10) ** int(exponent[1:])
    if suffix in BINARY_SUFFIXES:
        return value * BINARY_SUFFIXES[suffix]
    return value * DECIMAL_SUFFIXES[suffix or ""]


def milli_cpu(text: Any) -> int:
    return math.ceil(parse_quantity(text) * 1000)


def whole_units(text: Any) -> int:
    return math.ceil(parse_quantity(text))


def resource_list(raw: Optional[Dict[str, Any]]) -> Dict[str, int]:
    return {name: milli_cpu(value) if name == "cpu" else whole_units(value) for name, value in (raw or {}).items()}


def add(left: Dict[str, int], right: Dict[str, int]) -> Dict[str, int]:
    out = dict(left)
    for name, value in right.items():
        out[name] = out.get(name, 0) + value
    return out


def maximum(left: Dict[str, int], right: Dict[str, int]) -> Dict[str, int]:
    out = dict(left)
    for name, value in right.items():
        out[name] = max(out.get(name, 0), value)
    return out


def total_of(lists: Iterable[Dict[str, int]]) -> Dict[str, int]:
    out: Dict[str, int] = {}
    for item in lists:
        out = add(out, item)
    return out


def container_requests(container: Dict[str, Any], non_zero: bool) -> Dict[str, int]:
    resources = container.get("resources") or {}
    requests = resource_list(resources.get("limits"))
    requests.update(resource_list(resources.get("requests")))
    if non_zero:
        requests.setdefault("cpu", DEFAULT_MILLI_CPU)
        requests.setdefault("memory", DEFAULT_MEMORY)
    return requests


def pod_requests(spec: Dict[str, Any], non_zero: bool = False) -> Dict[str, int]:
    total = total_of(container_requests(c, non_zero) for c in spec.get("containers") or [])
    sidecars: Dict[str, int] = {}
    init_peak: Dict[str, int] = {}
    for container in spec.get("initContainers") or []:
        requests = container_requests(container, non_zero)
        if container.get("restartPolicy") == "Always":
            sidecars = add(sidecars, requests)
            init_peak = maximum(init_peak, sidecars)
        else:
            init_peak = maximum(init_peak, add(sidecars, requests))
    total = maximum(add(total, sidecars), init_peak)
    return add(total, resource_list(spec.get("overhead")))


def show(name: str, value: int) -> str:
    if name == "cpu":
        return f"{value}m"
    if name in BYTE_RESOURCES or name.startswith("hugepages-"):
        for unit, size in (("Gi", 1 << 30), ("Mi", 1 << 20), ("Ki", 1 << 10)):
            if value and value % size == 0:
                return f"{value // size}{unit}"
        return f"{value / (1 << 20):.1f}Mi" if abs(value) >= 1 << 20 else str(value)
    return str(value)


def resource_order(name: str) -> Tuple[int, str]:
    rank = {"pods": 0, "cpu": 1, "memory": 2, "ephemeral-storage": 3}
    return rank.get(name, 4), name


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
    if operator == "Equal":
        return (toleration.get("value") or "") == (taint.get("value") or "")
    return False


def untolerated(taints: List[Dict[str, Any]], tolerations: List[Dict[str, Any]], effects: Sequence[str]) -> Optional[Dict[str, Any]]:
    for taint in taints:
        if taint.get("effect") in effects and not any(tolerates(t, taint) for t in tolerations):
            return taint
    return None


def requirement_matches(requirement: Dict[str, Any], labels: Dict[str, str]) -> bool:
    key = requirement.get("key", "")
    operator = requirement.get("operator", "")
    values = [str(v) for v in requirement.get("values") or []]
    present = key in labels
    if operator == "In":
        return present and labels[key] in values
    if operator == "NotIn":
        return not present or labels[key] not in values
    if operator == "Exists":
        return present
    if operator == "DoesNotExist":
        return not present
    if operator in ("Gt", "Lt"):
        try:
            left, right = int(labels[key]), int(values[0])
        except (KeyError, IndexError, ValueError):
            return False
        return left > right if operator == "Gt" else left < right
    raise InputError(f"unknown operator {operator!r} for key {key!r} (In, NotIn, Exists, DoesNotExist, Gt, Lt)")


def term_matches(term: Dict[str, Any], node: Node) -> bool:
    expressions = term.get("matchExpressions") or []
    fields = term.get("matchFields") or []
    if not expressions and not fields:
        return False
    return all(requirement_matches(r, node.labels) for r in expressions) and all(
        requirement_matches(r, {"metadata.name": node.name}) for r in fields
    )


def node_affinity_of(pod: Pod) -> Dict[str, Any]:
    return ((pod.spec.get("affinity") or {}).get("nodeAffinity")) or {}


def required_terms(pod: Pod) -> Optional[List[Dict[str, Any]]]:
    required = node_affinity_of(pod).get("requiredDuringSchedulingIgnoredDuringExecution")
    if required is None:
        return None
    return list(required.get("nodeSelectorTerms") or [])


def preferred_terms(pod: Pod) -> List[Dict[str, Any]]:
    return list(node_affinity_of(pod).get("preferredDuringSchedulingIgnoredDuringExecution") or [])


def affinity_mismatch(pod: Pod, node: Node) -> str:
    for key, value in (pod.spec.get("nodeSelector") or {}).items():
        if node.labels.get(key) != str(value):
            found = f"{key}={node.labels[key]}" if key in node.labels else f"no {key} label"
            return f"nodeSelector wants {key}={value}, node has {found}"
    terms = required_terms(pod)
    if terms is not None and not any(term_matches(t, node) for t in terms):
        return f"none of the {len(terms)} required nodeSelectorTerms match"
    return ""


def matches_node_selection(pod: Pod, node: Node) -> bool:
    return not affinity_mismatch(pod, node)


def selector_matches(selector: Optional[Dict[str, Any]], labels: Dict[str, str]) -> bool:
    if selector is None:
        return False
    for key, value in (selector.get("matchLabels") or {}).items():
        if labels.get(key) != value:
            return False
    return all(requirement_matches(r, labels) for r in selector.get("matchExpressions") or [])


def with_match_label_keys(selector: Optional[Dict[str, Any]], keys: List[str], labels: Dict[str, str]) -> Optional[Dict[str, Any]]:
    if not keys:
        return selector
    merged = {
        "matchLabels": dict((selector or {}).get("matchLabels") or {}),
        "matchExpressions": list((selector or {}).get("matchExpressions") or []),
    }
    for key in keys:
        if key in labels:
            merged["matchExpressions"].append({"key": key, "operator": "In", "values": [labels[key]]})
    return merged


def spread_constraints(pod: Pod, hard: bool) -> List[SpreadConstraint]:
    out: List[SpreadConstraint] = []
    for raw in pod.spec.get("topologySpreadConstraints") or []:
        if (raw.get("whenUnsatisfiable") == "DoNotSchedule") != hard:
            continue
        out.append(
            SpreadConstraint(
                int(raw.get("maxSkew", 1)),
                raw.get("topologyKey", ""),
                with_match_label_keys(raw.get("labelSelector"), list(raw.get("matchLabelKeys") or []), pod.labels),
                int(raw.get("minDomains") or 0),
                (raw.get("nodeAffinityPolicy") or "Honor") == "Honor",
                (raw.get("nodeTaintsPolicy") or "Ignore") == "Honor",
            )
        )
    return out


def count_matching(pods: List[Pod], selector: Optional[Dict[str, Any]], namespace: str) -> int:
    return sum(1 for p in pods if p.namespace == namespace and not p.terminating and selector_matches(selector, p.labels))


def spread_counts(pod: Pod, cluster: Cluster, constraints: List[SpreadConstraint]) -> List[Dict[str, int]]:
    counts: List[Dict[str, int]] = [{} for _ in constraints]
    keys = [c.topology_key for c in constraints]
    for info in cluster.infos():
        labels = info.node.labels
        if not all(k in labels for k in keys):
            continue
        for index, constraint in enumerate(constraints):
            if constraint.honor_affinity and not matches_node_selection(pod, info.node):
                continue
            if constraint.honor_taints and untolerated(info.node.taints, pod.tolerations, FILTER_EFFECTS):
                continue
            domain = labels[constraint.topology_key]
            counts[index][domain] = counts[index].get(domain, 0) + count_matching(info.pods, constraint.selector, pod.namespace)
    return counts


def global_minimum(counts: Dict[str, int], constraint: SpreadConstraint) -> int:
    if not counts or len(counts) < constraint.min_domains:
        return 0
    return min(counts.values())


def prefilter(pod: Pod, cluster: Cluster) -> CycleState:
    hard = spread_constraints(pod, hard=True)
    return CycleState(hard, spread_counts(pod, cluster, hard))


def node_unschedulable(pod: Pod, info: NodeInfo, state: CycleState) -> Optional[Status]:
    if info.node.unschedulable and not any(tolerates(t, UNSCHEDULABLE_TAINT) for t in pod.tolerations):
        return Status("NodeUnschedulable", UNRESOLVABLE, [REASON_UNSCHEDULABLE], "spec.unschedulable is true (kubectl cordon)")
    return None


def node_name(pod: Pod, info: NodeInfo, state: CycleState) -> Optional[Status]:
    wanted = pod.node_name
    if wanted and wanted != info.node.name:
        return Status("NodeName", UNRESOLVABLE, [REASON_NODE_NAME], f"spec.nodeName is {wanted}")
    return None


def taint_toleration(pod: Pod, info: NodeInfo, state: CycleState) -> Optional[Status]:
    taint = untolerated(info.node.taints, pod.tolerations, FILTER_EFFECTS)
    if taint is None:
        return None
    key, value = taint.get("key", ""), taint.get("value", "")
    return Status("TaintToleration", UNRESOLVABLE, [f"node(s) had untolerated taint {{{key}: {value}}}"], f"{key}={value}:{taint.get('effect')}")


def node_affinity(pod: Pod, info: NodeInfo, state: CycleState) -> Optional[Status]:
    mismatch = affinity_mismatch(pod, info.node)
    if not mismatch:
        return None
    return Status("NodeAffinity", UNRESOLVABLE, [REASON_AFFINITY], mismatch)


def resources_fit(pod: Pod, info: NodeInfo, state: CycleState) -> Optional[Status]:
    allocatable, used = info.node.allocatable, info.requested
    reasons: List[str] = []
    details: List[str] = []
    if len(info.pods) + 1 > allocatable.get("pods", 0):
        reasons.append("Too many pods")
        details.append(f"pods: {len(info.pods)} bound, {allocatable.get('pods', 0)} allowed")
    for name in sorted(pod.requests, key=resource_order):
        wanted = pod.requests[name]
        if name == "pods" or wanted <= 0:
            continue
        free = allocatable.get(name, 0) - used.get(name, 0)
        if wanted > free:
            reasons.append(f"Insufficient {name}")
            details.append(f"{name}: requested {show(name, wanted)}, free {show(name, free)} of {show(name, allocatable.get(name, 0))}")
    if reasons:
        return Status("NodeResourcesFit", UNSCHEDULABLE, reasons, "; ".join(details))
    return None


def topology_spread(pod: Pod, info: NodeInfo, state: CycleState) -> Optional[Status]:
    for constraint, counts in zip(state.hard_spread, state.hard_counts):
        domain = info.node.labels.get(constraint.topology_key)
        if domain is None:
            return Status("PodTopologySpread", UNRESOLVABLE, [REASON_SPREAD_LABEL], f"node has no label {constraint.topology_key}")
        self_match = 1 if selector_matches(constraint.selector, pod.labels) else 0
        skew = counts.get(domain, 0) + self_match - global_minimum(counts, constraint)
        if skew > constraint.max_skew:
            return Status("PodTopologySpread", UNSCHEDULABLE, [REASON_SPREAD], f"{constraint.topology_key}={domain}: skew {skew} > maxSkew {constraint.max_skew}")
    return None


FilterPlugin = Callable[[Pod, NodeInfo, CycleState], Optional[Status]]
FILTERS: List[Tuple[str, FilterPlugin]] = [
    ("NodeUnschedulable", node_unschedulable),
    ("NodeName", node_name),
    ("TaintToleration", taint_toleration),
    ("NodeAffinity", node_affinity),
    ("NodeResourcesFit", resources_fit),
    ("PodTopologySpread", topology_spread),
]


def run_filters(pod: Pod, info: NodeInfo, state: CycleState) -> Optional[Status]:
    for _, plugin in FILTERS:
        status = plugin(pod, info, state)
        if status is not None:
            return status
    return None


def filter_nodes(pod: Pod, cluster: Cluster) -> Dict[str, Optional[Status]]:
    state = prefilter(pod, cluster)
    return {info.node.name: run_filters(pod, info, state) for info in cluster.infos()}


def fit_error(total_nodes: int, statuses: Iterable[Status]) -> str:
    histogram: Dict[str, int] = {}
    for status in statuses:
        for reason in status.reasons:
            histogram[reason] = histogram.get(reason, 0) + 1
    parts = sorted(f"{count} {reason}" for reason, count in histogram.items())
    message = f"0/{total_nodes} nodes are available:"
    if parts:
        message += " " + ", ".join(parts) + "."
    return message


def select_victims(pod: Pod, cluster: Cluster, node: str, lower: List[Pod]) -> Tuple[List[Pod], Optional[Status]]:
    kept = cluster.without(lower)
    status = run_filters(pod, kept.info(node), prefilter(pod, kept))
    if status is not None:
        return [], status
    victims: List[Pod] = []
    for candidate in lower:
        attempt = Cluster(kept.nodes, kept.pods + [candidate])
        if run_filters(pod, attempt.info(node), prefilter(pod, attempt)) is None:
            kept = attempt
        else:
            victims.append(candidate)
    return victims, None


def preempt(pod: Pod, cluster: Cluster, statuses: Dict[str, Optional[Status]]) -> Preemption:
    if pod.spec.get("preemptionPolicy") == "Never":
        return Preemption("preemption: not eligible due to preemptionPolicy=Never.")
    outcomes: List[Status] = []
    candidates: List[Tuple[Tuple[int, int, int, str], List[Pod]]] = []
    for info in cluster.infos():
        status = statuses.get(info.node.name)
        if status is None or status.code != UNSCHEDULABLE:
            outcomes.append(Status("DefaultPreemption", UNRESOLVABLE, [REASON_NOT_HELPFUL]))
            continue
        lower = sorted((p for p in info.pods if p.priority < pod.priority), key=lambda p: (-p.priority, p.key))
        if not lower:
            outcomes.append(Status("DefaultPreemption", UNRESOLVABLE, [REASON_NO_VICTIMS]))
            continue
        victims, failure = select_victims(pod, cluster, info.node.name, lower)
        if failure is not None:
            outcomes.append(failure)
            continue
        rank = (max(v.priority for v in victims), sum(v.priority for v in victims), len(victims), info.node.name)
        candidates.append((rank, victims))
    if candidates:
        rank, victims = min(candidates, key=lambda c: c[0])
        evicted = ", ".join(f"{v.key} (priority {v.priority})" for v in victims)
        return Preemption(f"preemption: nominate {rank[3]} and evict {evicted}", rank[3], [v.key for v in victims])
    return Preemption("preemption: " + fit_error(len(cluster.nodes), outcomes))


def least_allocated(pairs: List[Tuple[int, int]]) -> int:
    total = weights = 0
    for requested, allocatable in pairs:
        if allocatable == 0:
            continue
        total += 0 if requested > allocatable else (allocatable - requested) * MAX_NODE_SCORE // allocatable
        weights += 1
    return total // weights if weights else 0


def most_allocated(pairs: List[Tuple[int, int]]) -> int:
    total = weights = 0
    for requested, allocatable in pairs:
        if allocatable == 0:
            continue
        total += min(requested, allocatable) * MAX_NODE_SCORE // allocatable
        weights += 1
    return total // weights if weights else 0


def balanced_allocation(pairs: List[Tuple[int, int]]) -> int:
    fractions = [min(requested / allocatable, 1.0) for requested, allocatable in pairs if allocatable]
    std = 0.0
    if len(fractions) == 2:
        std = abs(fractions[0] - fractions[1]) / 2
    elif len(fractions) > 2:
        mean = sum(fractions) / len(fractions)
        std = math.sqrt(sum((f - mean) ** 2 for f in fractions) / len(fractions))
    return int((1 - std) * MAX_NODE_SCORE)


def allocation(pod: Pod, info: NodeInfo, non_zero: bool) -> List[Tuple[int, int]]:
    used = info.non_zero_requested if non_zero else info.requested
    wanted = pod.non_zero if non_zero else pod.requests
    return [(used.get(r, 0) + wanted.get(r, 0), info.node.allocatable.get(r, 0)) for r in SCORED_RESOURCES]


def normalize(raw: Dict[str, int], reverse: bool = False) -> Dict[str, int]:
    top = max(raw.values(), default=0)
    if top == 0:
        return {name: MAX_NODE_SCORE if reverse else 0 for name in raw}
    scaled = {name: MAX_NODE_SCORE * value // top for name, value in raw.items()}
    return {name: MAX_NODE_SCORE - value for name, value in scaled.items()} if reverse else scaled


def preferred_weight(pod: Pod, node: Node) -> int:
    return sum(int(t.get("weight", 0)) for t in preferred_terms(pod) if term_matches(t.get("preference") or {}, node))


def intolerable_preferences(pod: Pod, node: Node) -> int:
    return sum(1 for t in node.taints if t.get("effect") == "PreferNoSchedule" and not any(tolerates(x, t) for x in pod.tolerations))


def spread_scores(pod: Pod, cluster: Cluster, feasible: List[NodeInfo]) -> Optional[Dict[str, int]]:
    soft = spread_constraints(pod, hard=False)
    if not soft:
        return None
    keys = [c.topology_key for c in soft]
    scored = [i for i in feasible if all(k in i.node.labels for k in keys)]
    sizes = [len({i.node.labels[c.topology_key] for i in scored}) for c in soft]
    counts = spread_counts(pod, cluster, soft)
    raw: Dict[str, int] = {}
    for info in scored:
        total = 0.0
        for index, constraint in enumerate(soft):
            matching = counts[index].get(info.node.labels[constraint.topology_key], 0)
            total += matching * math.log(sizes[index] + 2) + (constraint.max_skew - 1)
        raw[info.node.name] = math.floor(total + 0.5)
    result = {i.node.name: 0 for i in feasible}
    if raw:
        low, high = min(raw.values()), max(raw.values())
        for name, value in raw.items():
            result[name] = MAX_NODE_SCORE if high == 0 else MAX_NODE_SCORE * (high + low - value) // high
    return result


def score(pod: Pod, cluster: Cluster, feasible: List[NodeInfo], strategy: str) -> List[NodeScore]:
    fit = least_allocated if strategy == "LeastAllocated" else most_allocated
    plugins: Dict[str, Dict[str, int]] = {
        "NodeResourcesFit": {i.node.name: fit(allocation(pod, i, non_zero=True)) for i in feasible},
        "NodeResourcesBalancedAllocation": {i.node.name: balanced_allocation(allocation(pod, i, non_zero=False)) for i in feasible},
    }
    if preferred_terms(pod):
        plugins["NodeAffinity"] = normalize({i.node.name: preferred_weight(pod, i.node) for i in feasible})
    plugins["TaintToleration"] = normalize({i.node.name: intolerable_preferences(pod, i.node) for i in feasible}, reverse=True)
    spread = spread_scores(pod, cluster, feasible)
    if spread is not None:
        plugins["PodTopologySpread"] = spread
    results = []
    for info in feasible:
        name = info.node.name
        per_plugin = {plugin: values[name] for plugin, values in plugins.items()}
        results.append(NodeScore(name, per_plugin, sum(WEIGHTS[p] * s for p, s in per_plugin.items())))
    return sorted(results, key=lambda s: (-s.total, s.node))


def schedule(pod: Pod, cluster: Cluster, strategy: str = "LeastAllocated") -> Result:
    if strategy not in STRATEGIES:
        raise InputError(f"unknown scoring strategy {strategy!r} ({', '.join(STRATEGIES)})")
    if not cluster.nodes:
        return Result(pod, strategy, {}, [], None, NO_NODES, NO_NODES)
    statuses = filter_nodes(pod, cluster)
    feasible = [cluster.info(name) for name, status in statuses.items() if status is None]
    if not feasible:
        message = fit_error(len(cluster.nodes), [s for s in statuses.values() if s is not None])
        preemption = preempt(pod, cluster, statuses)
        full = message if preemption.node else f"{message} {preemption.message}"
        return Result(pod, strategy, statuses, [], None, full, message, preemption)
    if len(feasible) == 1:
        return Result(pod, strategy, statuses, [], feasible[0].node.name)
    scores = score(pod, cluster, feasible, strategy)
    return Result(pod, strategy, statuses, scores, scores[0].node)


def read_json(path: str) -> Any:
    try:
        if path == "-":
            return json.load(sys.stdin)
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)
    except OSError as exc:
        raise InputError(f"cannot read {path}: {exc.strerror}")
    except json.JSONDecodeError as exc:
        raise InputError(f"{path} is not JSON ({exc.msg}, line {exc.lineno}); export it with kubectl ... -o json")


def items_of(doc: Any, kind: str) -> List[Dict[str, Any]]:
    if isinstance(doc, list):
        return doc
    if isinstance(doc, dict) and isinstance(doc.get("items"), list):
        return doc["items"]
    if isinstance(doc, dict) and doc.get("kind") == kind:
        return [doc]
    raise InputError(f"expected a {kind} or a List of {kind}s, as printed by kubectl get ... -o json")


def make_node(obj: Dict[str, Any]) -> Node:
    meta, spec, status = obj.get("metadata") or {}, obj.get("spec") or {}, obj.get("status") or {}
    name = meta.get("name")
    if not name:
        raise InputError("a node without metadata.name")
    try:
        capacity = resource_list(status.get("capacity"))
        allocatable = resource_list(status.get("allocatable")) if status.get("allocatable") else dict(capacity)
    except InputError as exc:
        raise InputError(f"node {name}: {exc}")
    return Node(name, dict(meta.get("labels") or {}), list(spec.get("taints") or []), bool(spec.get("unschedulable")), allocatable, capacity)


def make_pod(obj: Dict[str, Any], default_namespace: str = "default") -> Pod:
    kind = obj.get("kind") or "Pod"
    meta = obj.get("metadata") or {}
    if kind in WORKLOAD_KINDS:
        template = (obj.get("spec") or {}).get("template") or {}
        labels = (template.get("metadata") or {}).get("labels") or {}
        spec = template.get("spec") or {}
    elif kind == "Pod":
        labels = meta.get("labels") or {}
        spec = obj.get("spec") or {}
    else:
        raise InputError(f"kind {kind} has no pod template; pass a Pod, {', '.join(WORKLOAD_KINDS)}")
    key = f"{meta.get('namespace') or default_namespace}/{meta.get('name') or 'unnamed'}"
    if not spec.get("containers"):
        raise InputError(f"pod {key}: spec.containers is empty")
    try:
        requests, non_zero = pod_requests(spec), pod_requests(spec, non_zero=True)
    except InputError as exc:
        raise InputError(f"pod {key}: {exc}")
    namespace, _, name = key.partition("/")
    phase = (obj.get("status") or {}).get("phase") or ""
    return Pod(namespace, name, dict(labels), spec, phase, bool(meta.get("deletionTimestamp")), requests, non_zero)


def bound_pods(pods: Iterable[Pod], exclude: str = "") -> List[Pod]:
    return [p for p in pods if p.node_name and p.phase not in TERMINAL_PHASES and p.key != exclude]


def describe_requests(requests: Dict[str, int]) -> str:
    return ", ".join(f"{name} {show(name, requests[name])}" for name in sorted(requests, key=resource_order)) or "nothing (scored as 100m CPU and 200Mi)"


def render(result: Result) -> str:
    lines = [f"pod {result.pod.key} requests {describe_requests(result.pod.requests)}"]
    feasible = sum(1 for s in result.statuses.values() if s is None)
    lines.append(f"filter: {feasible} of {len(result.statuses)} nodes feasible")
    width = max([len(n) for n in result.statuses] + [4])
    for name, status in result.statuses.items():
        if status is None:
            lines.append(f"  {name:<{width}}  ok")
        else:
            detail = f" ({status.detail})" if status.detail else ""
            lines.append(f"  {name:<{width}}  {status.plugin:<18} {', '.join(status.reasons)}{detail}")
    if result.node is None:
        lines.append(f"FailedScheduling: {result.message}")
        if result.preemption and result.preemption.node:
            lines.append(result.preemption.message)
        return "\n".join(lines)
    if result.scores:
        plugins = list(result.scores[0].plugins)
        lines.append("score: 0-100 per plugin, then weighted (" + ", ".join(f"{COLUMNS[p]} x{WEIGHTS[p]}" for p in plugins) + f"), Fit is {result.strategy}")
        lines.append(f"  {'node':<{width}}  " + "".join(f"{COLUMNS[p]:>9}" for p in plugins) + f"{'total':>8}")
        for entry in result.scores:
            lines.append(f"  {entry.node:<{width}}  " + "".join(f"{entry.plugins[p]:>9}" for p in plugins) + f"{entry.total:>8}")
    else:
        lines.append("score: skipped, only one feasible node")
    lines.append(f"bind {result.pod.key} to {result.node}")
    return "\n".join(lines)


def to_json(result: Result) -> Dict[str, Any]:
    preemption = result.preemption
    return {
        "pod": result.pod.key,
        "requests": {name: show(name, value) for name, value in result.pod.requests.items()},
        "strategy": result.strategy,
        "scheduled": result.node is not None,
        "node": result.node,
        "message": result.message,
        "filter_message": result.filter_message,
        "preemption": None if preemption is None else {"message": preemption.message, "node": preemption.node, "victims": preemption.victims},
        "filter": [
            {
                "node": name,
                "feasible": status is None,
                "plugin": status.plugin if status else None,
                "code": status.code if status else None,
                "reasons": status.reasons if status else [],
                "detail": status.detail if status else "",
            }
            for name, status in result.statuses.items()
        ],
        "scores": [{"node": s.node, "plugins": s.plugins, "total": s.total} for s in result.scores],
    }


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="minisched", description=__doc__)
    parser.add_argument("--nodes", required=True, help="kubectl get nodes -o json")
    parser.add_argument("--pod", required=True, help="the pod to place as JSON (a Pod, or a Deployment, ReplicaSet, StatefulSet, DaemonSet or Job template); - reads stdin")
    parser.add_argument("--pods", help="kubectl get pods -A -o json: pods already bound, whose requests are already used")
    parser.add_argument("--strategy", choices=STRATEGIES, default="LeastAllocated", help="NodeResourcesFit scoring strategy (default LeastAllocated)")
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    args = parser.parse_args(list(sys.argv[1:] if argv is None else argv))
    try:
        nodes = [make_node(o) for o in items_of(read_json(args.nodes), "Node")]
        pod = make_pod(read_json(args.pod))
        others = [make_pod(o) for o in items_of(read_json(args.pods), "Pod")] if args.pods else []
        result = schedule(pod, Cluster(nodes, bound_pods(others, exclude=pod.key)), args.strategy)
    except InputError as exc:
        print(f"minisched: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(to_json(result), indent=2) if args.json else render(result))
    return 0 if result.node else 1


if __name__ == "__main__":
    raise SystemExit(main())
