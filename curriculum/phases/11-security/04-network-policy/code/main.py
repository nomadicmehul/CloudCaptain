"""mininetpol: evaluate Kubernetes NetworkPolicies over the JSON kubectl prints. `matrix` predicts which pod may open a connection to which pod, address and port, `explain` names the policy and rule that decide one connection, `lint` finds the common traps, and `serve`, `probe` and `compare` check the prediction against the real network from inside the pods."""
from __future__ import annotations

import argparse
import ipaddress
import json
import os
import re
import signal
import socket
import struct
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, Iterator, List, Optional, Sequence, Tuple

INGRESS = "Ingress"
EGRESS = "Egress"
POLICY_TYPES = (INGRESS, EGRESS)
PEER_FIELD = {INGRESS: "from", EGRESS: "to"}
RULES_FIELD = {INGRESS: "ingress", EGRESS: "egress"}
OTHER_END = {INGRESS: "source", EGRESS: "destination"}
PROTOCOLS = ("TCP", "UDP", "SCTP")
OPERATORS = ("In", "NotIn", "Exists", "DoesNotExist")
PORT_NAME = re.compile(r"^[a-z0-9]([a-z0-9-]{0,13}[a-z0-9])?$")
NAME_LABEL = "kubernetes.io/metadata.name"
TERMINAL_PHASES = ("Succeeded", "Failed")
SYSTEM_NAMESPACE = "kube-system"
DNS_KEY = "dns"
DNS_LABELS = {"k8s-app": "kube-dns"}
DNS_QUERY_NAME = "kubernetes.default.svc.cluster.local"
RCODES = {0: "NOERROR", 1: "FORMERR", 2: "SERVFAIL", 3: "NXDOMAIN", 4: "NOTIMP", 5: "REFUSED"}
WARNING = "warning"
INFO = "info"
ALLOWED = "allowed"
BLOCKED = "blocked"
PARTIAL = "partial"

PortSpec = Tuple[int, str]


class InputError(ValueError):
    pass


@dataclass(frozen=True)
class ContainerPort:
    name: str
    port: int
    protocol: str


@dataclass
class Pod:
    namespace: str
    name: str
    labels: Dict[str, str]
    ip: str
    ports: List[ContainerPort]

    @property
    def key(self) -> str:
        return f"{self.namespace}/{self.name}"


@dataclass
class Selector:
    match_labels: Dict[str, str]
    expressions: List[Tuple[str, str, Tuple[str, ...]]]

    @property
    def empty(self) -> bool:
        return not self.match_labels and not self.expressions


@dataclass
class Peer:
    pod_selector: Optional[Selector] = None
    namespace_selector: Optional[Selector] = None
    cidr: Optional[str] = None
    excepts: Tuple[str, ...] = ()


@dataclass
class PolicyPort:
    protocol: str
    port: Any = None
    end_port: Optional[int] = None


@dataclass
class Rule:
    peers: List[Peer]
    ports: List[PolicyPort]


@dataclass
class Policy:
    namespace: str
    name: str
    pod_selector: Selector
    types: List[str]
    ingress: List[Rule]
    egress: List[Rule]
    spec: Dict[str, Any]

    @property
    def key(self) -> str:
        return f"{self.namespace}/{self.name}"

    def rules(self, direction: str) -> List[Rule]:
        return self.ingress if direction == INGRESS else self.egress


@dataclass
class Endpoint:
    key: str
    ip: str
    pod: Optional[Pod] = None
    ports: List[PortSpec] = field(default_factory=list)


@dataclass
class Cluster:
    pods: List[Pod]
    namespaces: Dict[str, Dict[str, str]]
    policies: List[Policy]
    notes: List[str] = field(default_factory=list)

    def namespace_labels(self, name: str) -> Dict[str, str]:
        return self.namespaces.get(name) or {NAME_LABEL: name}

    def pod_by_key(self, key: str) -> Optional[Pod]:
        return next((p for p in self.pods if p.key == key), None)

    def dns_backends(self) -> List[Pod]:
        return [p for p in self.pods if p.namespace == SYSTEM_NAMESPACE and all(p.labels.get(k) == v for k, v in DNS_LABELS.items())]


@dataclass
class RuleCheck:
    policy: str
    direction: str
    index: int
    peer_ok: bool
    port_ok: bool
    peer_text: str
    port_text: str

    @property
    def allows(self) -> bool:
        return self.peer_ok and self.port_ok

    @property
    def where(self) -> str:
        return f"{self.policy} {RULES_FIELD[self.direction]}[{self.index}]"


@dataclass
class Side:
    direction: str
    subject: Optional[str]
    namespace: str
    isolating: List[str]
    allowed: bool
    checks: List[RuleCheck]

    @property
    def deciding(self) -> Optional[RuleCheck]:
        return next((c for c in self.checks if c.allows), None)


@dataclass
class Verdict:
    source: Endpoint
    destination: Endpoint
    port: int
    protocol: str
    egress: Side
    ingress: Side

    @property
    def allowed(self) -> bool:
        return self.egress.allowed and self.ingress.allowed


@dataclass
class Cell:
    source: str
    target: str
    kind: str
    address: str
    port: int
    protocol: str
    state: str
    reasons: List[str]

    @property
    def allowed(self) -> bool:
        return self.state == ALLOWED

    @property
    def key(self) -> str:
        return f"{self.source} -> {self.target} {self.port}/{self.protocol}"


@dataclass
class Matrix:
    namespaces: List[str]
    sources: List[Pod]
    targets: List[str]
    cells: List[Cell]
    probe_ports: List[PortSpec]
    externals: List[Endpoint]


@dataclass
class Finding:
    severity: str
    subject: str
    code: str
    message: str


def parse_selector(raw: Any, where: str) -> Selector:
    if raw is None:
        return Selector({}, [])
    if not isinstance(raw, dict):
        raise InputError(f"{where}: a label selector is an object with matchLabels and matchExpressions")
    labels = raw.get("matchLabels") or {}
    if not isinstance(labels, dict):
        raise InputError(f"{where}.matchLabels: expected an object of label: value pairs")
    expressions: List[Tuple[str, str, Tuple[str, ...]]] = []
    for index, expression in enumerate(raw.get("matchExpressions") or []):
        here = f"{where}.matchExpressions[{index}]"
        key, operator = expression.get("key"), expression.get("operator")
        values = tuple(str(v) for v in expression.get("values") or [])
        if not key:
            raise InputError(f"{here}.key: Required value")
        if operator not in OPERATORS:
            raise InputError(f"{here}.operator: Unsupported value {operator!r}: supported values {', '.join(OPERATORS)}")
        if operator in ("In", "NotIn") and not values:
            raise InputError(f"{here}.values: must be specified when `operator` is 'In' or 'NotIn'")
        if operator in ("Exists", "DoesNotExist") and values:
            raise InputError(f"{here}.values: may not be specified when `operator` is 'Exists' or 'DoesNotExist'")
        expressions.append((str(key), operator, values))
    return Selector({str(k): str(v) for k, v in labels.items()}, expressions)


def selector_matches(selector: Selector, labels: Dict[str, str]) -> bool:
    for key, value in selector.match_labels.items():
        if labels.get(key) != value:
            return False
    for key, operator, values in selector.expressions:
        present = key in labels
        if operator == "In" and not (present and labels[key] in values):
            return False
        if operator == "NotIn" and present and labels[key] in values:
            return False
        if operator == "Exists" and not present:
            return False
        if operator == "DoesNotExist" and present:
            return False
    return True


def describe_selector(selector: Selector) -> str:
    parts = [f"{k}={v}" for k, v in sorted(selector.match_labels.items())]
    for key, operator, values in selector.expressions:
        if operator == "In":
            parts.append(f"{key} in ({','.join(values)})")
        elif operator == "NotIn":
            parts.append(f"{key} notin ({','.join(values)})")
        elif operator == "Exists":
            parts.append(key)
        else:
            parts.append(f"!{key}")
    return ",".join(parts) or "{}"


def parse_ip_block(raw: Any, where: str) -> Tuple[str, Tuple[str, ...]]:
    if not isinstance(raw, dict) or not raw.get("cidr"):
        raise InputError(f"{where}.cidr: Required value")
    try:
        network = ipaddress.ip_network(str(raw["cidr"]), strict=False)
    except ValueError:
        raise InputError(f"{where}.cidr: {raw['cidr']!r} must be a valid CIDR, for example 10.0.0.0/8")
    excepts = []
    for index, text in enumerate(raw.get("except") or []):
        try:
            subnet = ipaddress.ip_network(str(text), strict=False)
        except ValueError:
            raise InputError(f"{where}.except[{index}]: {text!r} must be a valid CIDR")
        if subnet.version != network.version or not subnet.subnet_of(network) or subnet.prefixlen <= network.prefixlen:
            raise InputError(f"{where}.except[{index}]: {subnet} must be a strict subset of `cidr` {network}")
        excepts.append(str(subnet))
    return str(network), tuple(excepts)


def parse_peer(raw: Any, where: str) -> Peer:
    if not isinstance(raw, dict):
        raise InputError(f"{where}: a peer is an object with podSelector, namespaceSelector or ipBlock")
    has_pods = raw.get("podSelector") is not None
    has_namespaces = raw.get("namespaceSelector") is not None
    block = raw.get("ipBlock")
    if not (has_pods or has_namespaces or block is not None):
        raise InputError(f"{where}: Required value: must specify a peer")
    if block is not None and (has_pods or has_namespaces):
        raise InputError(f"{where}: Forbidden: may not specify both ipBlock and another peer")
    if block is not None:
        cidr, excepts = parse_ip_block(block, f"{where}.ipBlock")
        return Peer(cidr=cidr, excepts=excepts)
    return Peer(
        parse_selector(raw["podSelector"], f"{where}.podSelector") if has_pods else None,
        parse_selector(raw["namespaceSelector"], f"{where}.namespaceSelector") if has_namespaces else None,
    )


def parse_port(raw: Any, where: str) -> PolicyPort:
    if not isinstance(raw, dict):
        raise InputError(f"{where}: a port is an object with protocol, port and endPort")
    protocol = raw.get("protocol") or "TCP"
    if protocol not in PROTOCOLS:
        raise InputError(f"{where}.protocol: Unsupported value {protocol!r}: supported values {', '.join(PROTOCOLS)}")
    port, end = raw.get("port"), raw.get("endPort")
    if port is None:
        if end is not None:
            raise InputError(f"{where}.endPort: may not be specified when `port` is not specified")
        return PolicyPort(protocol)
    if isinstance(port, bool) or not isinstance(port, (int, str)):
        raise InputError(f"{where}.port: expected a number or a port name, got {port!r}")
    if isinstance(port, int):
        if not 1 <= port <= 65535:
            raise InputError(f"{where}.port: {port} must be between 1 and 65535, inclusive")
        if end is not None:
            if isinstance(end, bool) or not isinstance(end, int) or not 1 <= end <= 65535:
                raise InputError(f"{where}.endPort: {end!r} must be between 1 and 65535, inclusive")
            if end < port:
                raise InputError(f"{where}.endPort: must be greater than or equal to `port`")
        return PolicyPort(protocol, port, end)
    if end is not None:
        raise InputError(f"{where}.endPort: may not be specified when `port` is non-numeric")
    if not PORT_NAME.match(port) or "--" in port or not re.search("[a-z]", port):
        raise InputError(f"{where}.port: {port!r} is not a port name (at most 15 lowercase letters, digits and '-', at least one letter)")
    return PolicyPort(protocol, port)


def parse_rule(raw: Any, direction: str, where: str) -> Rule:
    if not isinstance(raw, dict):
        raise InputError(f"{where}: a rule is an object with {PEER_FIELD[direction]} and ports")
    peers = [parse_peer(p, f"{where}.{PEER_FIELD[direction]}[{j}]") for j, p in enumerate(raw.get(PEER_FIELD[direction]) or [])]
    ports = [parse_port(p, f"{where}.ports[{k}]") for k, p in enumerate(raw.get("ports") or [])]
    return Rule(peers, ports)


def default_policy_types(spec: Dict[str, Any]) -> List[str]:
    return [INGRESS, EGRESS] if spec.get("egress") else [INGRESS]


def make_policy(obj: Dict[str, Any], default_namespace: str = "default") -> Policy:
    meta = obj.get("metadata") or {}
    name = meta.get("name")
    if not name:
        raise InputError("a NetworkPolicy without metadata.name")
    namespace = meta.get("namespace") or default_namespace
    where = f"networkpolicy {namespace}/{name}: spec"
    spec = obj.get("spec") or {}
    if not isinstance(spec, dict):
        raise InputError(f"{where}: expected an object")
    raw_types = spec.get("policyTypes")
    if raw_types:
        for value in raw_types:
            if value not in POLICY_TYPES:
                raise InputError(f"{where}.policyTypes: Unsupported value {value!r}: supported values Ingress, Egress")
        types = [t for t in POLICY_TYPES if t in raw_types]
    else:
        types = default_policy_types(spec)
    ingress = [parse_rule(r, INGRESS, f"{where}.ingress[{i}]") for i, r in enumerate(spec.get("ingress") or [])]
    egress = [parse_rule(r, EGRESS, f"{where}.egress[{i}]") for i, r in enumerate(spec.get("egress") or [])]
    return Policy(namespace, str(name), parse_selector(spec.get("podSelector"), f"{where}.podSelector"), types, ingress, egress, spec)


def pod_ip(status: Dict[str, Any]) -> str:
    ips = [entry.get("ip") for entry in status.get("podIPs") or [] if entry.get("ip")]
    return str(ips[0] if ips else status.get("podIP") or "")


def make_pod(obj: Dict[str, Any], default_namespace: str = "default") -> Tuple[Optional[Pod], str]:
    meta, spec, status = obj.get("metadata") or {}, obj.get("spec") or {}, obj.get("status") or {}
    name = meta.get("name")
    if not name:
        raise InputError("a Pod without metadata.name")
    key = f"{meta.get('namespace') or default_namespace}/{name}"
    if spec.get("hostNetwork"):
        return None, f"{key} skipped: a hostNetwork pod uses the node's addresses and no NetworkPolicy selects it"
    if status.get("phase") in TERMINAL_PHASES:
        return None, f"{key} skipped: phase {status['phase']}"
    ip = pod_ip(status)
    if not ip:
        return None, f"{key} skipped: no pod IP yet"
    ports = []
    for container in spec.get("containers") or []:
        for entry in container.get("ports") or []:
            try:
                number = int(entry.get("containerPort"))
            except (TypeError, ValueError):
                raise InputError(f"pod {key}: containerPort {entry.get('containerPort')!r} is not a number")
            ports.append(ContainerPort(str(entry.get("name") or ""), number, entry.get("protocol") or "TCP"))
    namespace, _, pod_name = key.partition("/")
    labels = {str(k): str(v) for k, v in (meta.get("labels") or {}).items()}
    return Pod(namespace, pod_name, labels, ip, ports), ""


def iter_objects(doc: Any, kind_hint: str = "") -> Iterator[Dict[str, Any]]:
    if isinstance(doc, list):
        for item in doc:
            yield from iter_objects(item, kind_hint)
        return
    if not isinstance(doc, dict):
        raise InputError(f"expected JSON objects, found a {type(doc).__name__}")
    kind = doc.get("kind") or kind_hint
    if isinstance(doc.get("items"), list):
        yield from iter_objects(doc["items"], kind[:-4] if kind.endswith("List") and kind != "List" else "")
        return
    yield dict(doc, kind=kind)


def load_cluster(docs: Iterable[Any], default_namespace: str = "default") -> Cluster:
    pods: Dict[str, Pod] = {}
    namespaces: Dict[str, Dict[str, str]] = {}
    policies: Dict[str, Policy] = {}
    notes: List[str] = []
    for doc in docs:
        for obj in iter_objects(doc):
            kind = obj.get("kind")
            if kind == "Pod":
                pod, note = make_pod(obj, default_namespace)
                if pod is None:
                    notes.append(note)
                else:
                    pods[pod.key] = pod
            elif kind == "Namespace":
                meta = obj.get("metadata") or {}
                if not meta.get("name"):
                    raise InputError("a Namespace without metadata.name")
                labels = {str(k): str(v) for k, v in (meta.get("labels") or {}).items()}
                labels.setdefault(NAME_LABEL, meta["name"])
                namespaces[meta["name"]] = labels
            elif kind == "NetworkPolicy":
                policy = make_policy(obj, default_namespace)
                policies[policy.key] = policy
    for name in sorted({p.namespace for p in pods.values()} - set(namespaces)):
        namespaces[name] = {NAME_LABEL: name}
        notes.append(f"namespace {name}: labels unknown, only {NAME_LABEL}={name} is assumed (add kubectl get namespaces -o json)")
    return Cluster(sorted(pods.values(), key=lambda p: (p.namespace, p.name)), namespaces, sorted(policies.values(), key=lambda p: (p.namespace, p.name)), notes)


def endpoint_of(pod: Pod) -> Endpoint:
    return Endpoint(pod.key, pod.ip, pod)


def in_block(ip: str, cidr: str, excepts: Sequence[str]) -> bool:
    try:
        address = ipaddress.ip_address(ip)
    except ValueError:
        return False
    network = ipaddress.ip_network(cidr)
    if address.version != network.version or address not in network:
        return False
    return not any(address in ipaddress.ip_network(e) for e in excepts)


def peer_matches(cluster: Cluster, policy_namespace: str, peer: Peer, other: Endpoint) -> bool:
    if peer.cidr is not None:
        return in_block(other.ip, peer.cidr, peer.excepts)
    pod = other.pod
    if pod is None:
        return False
    if peer.namespace_selector is None:
        in_namespace = pod.namespace == policy_namespace
    else:
        in_namespace = selector_matches(peer.namespace_selector, cluster.namespace_labels(pod.namespace))
    return in_namespace and (peer.pod_selector is None or selector_matches(peer.pod_selector, pod.labels))


def port_matches(rule_port: PolicyPort, port: int, protocol: str, destination: Optional[Pod]) -> bool:
    if rule_port.protocol != protocol:
        return False
    if rule_port.port is None:
        return True
    if isinstance(rule_port.port, int):
        last = rule_port.end_port if rule_port.end_port is not None else rule_port.port
        return rule_port.port <= port <= last
    if destination is None:
        return False
    return any(cp.name == rule_port.port and cp.port == port and cp.protocol == protocol for cp in destination.ports)


def describe_peer(peer: Peer, namespace: str) -> str:
    if peer.cidr is not None:
        return f"ipBlock {peer.cidr}" + (f" except {', '.join(peer.excepts)}" if peer.excepts else "")
    pods = "every pod" if peer.pod_selector is None or peer.pod_selector.empty else f"pods {describe_selector(peer.pod_selector)}"
    if peer.namespace_selector is None:
        return f"{pods} in namespace {namespace}"
    if peer.namespace_selector.empty:
        return f"{pods} in every namespace"
    return f"{pods} in namespaces {describe_selector(peer.namespace_selector)}"


def describe_port(rule_port: PolicyPort) -> str:
    if rule_port.port is None:
        return f"every {rule_port.protocol} port"
    if isinstance(rule_port.port, str):
        return f"{rule_port.port}/{rule_port.protocol} (named)"
    if rule_port.end_port is not None:
        return f"{rule_port.port}-{rule_port.end_port}/{rule_port.protocol}"
    return f"{rule_port.port}/{rule_port.protocol}"


def selecting_policies(cluster: Cluster, pod: Pod, direction: str) -> List[Policy]:
    return [p for p in cluster.policies if p.namespace == pod.namespace and direction in p.types and selector_matches(p.pod_selector, pod.labels)]


def check_rule(cluster: Cluster, policy: Policy, direction: str, index: int, rule: Rule, other: Endpoint, port: int, protocol: str, destination: Optional[Pod]) -> RuleCheck:
    peer_field = PEER_FIELD[direction]
    if not rule.peers:
        peer_ok, peer_text = True, f"no {peer_field}: any {OTHER_END[direction]}"
    else:
        hit = next((j for j, peer in enumerate(rule.peers) if peer_matches(cluster, policy.namespace, peer, other)), None)
        peer_ok = hit is not None
        if hit is not None:
            peer_text = f"{peer_field}[{hit}] {describe_peer(rule.peers[hit], policy.namespace)}"
        else:
            peer_text = f"{other.key} is not in {peer_field} (" + "; ".join(describe_peer(p, policy.namespace) for p in rule.peers) + ")"
    if not rule.ports:
        port_ok, port_text = True, "no ports: any port"
    else:
        hit = next((k for k, rp in enumerate(rule.ports) if port_matches(rp, port, protocol, destination)), None)
        port_ok = hit is not None
        if hit is not None:
            chosen = rule.ports[hit]
            port_text = f"ports[{hit}] {describe_port(chosen)}"
            if isinstance(chosen.port, str) and destination is not None:
                port_text += f" is {port} on {destination.key}"
        else:
            port_text = f"{port}/{protocol} is not in ports (" + ", ".join(describe_port(rp) for rp in rule.ports) + ")"
    return RuleCheck(policy.name, direction, index, peer_ok, port_ok, peer_text, port_text)


def evaluate_side(cluster: Cluster, direction: str, subject: Optional[Pod], other: Endpoint, port: int, protocol: str, destination: Optional[Pod]) -> Side:
    if subject is None:
        return Side(direction, None, "", [], True, [])
    policies = selecting_policies(cluster, subject, direction)
    checks = [
        check_rule(cluster, policy, direction, index, rule, other, port, protocol, destination)
        for policy in policies
        for index, rule in enumerate(policy.rules(direction))
    ]
    allowed = not policies or any(c.allows for c in checks)
    return Side(direction, subject.key, subject.namespace, [p.name for p in policies], allowed, checks)


def evaluate(cluster: Cluster, source: Endpoint, destination: Endpoint, port: int, protocol: str) -> Verdict:
    egress = evaluate_side(cluster, EGRESS, source.pod, destination, port, protocol, destination.pod)
    ingress = evaluate_side(cluster, INGRESS, destination.pod, source, port, protocol, destination.pod)
    return Verdict(source, destination, port, protocol, egress, ingress)


def summarize(side: Side) -> str:
    direction = side.direction.lower()
    if side.subject is None:
        return f"{direction}: not a pod"
    if not side.isolating:
        return f"{direction}: not isolated"
    deciding = side.deciding
    if deciding is not None:
        return f"{direction}: allowed by {deciding.where}"
    return f"{direction}: isolated by {', '.join(side.isolating)}, nothing allows it"


def target_ports(pod: Pod, extra: Sequence[PortSpec]) -> List[PortSpec]:
    return sorted({(cp.port, cp.protocol) for cp in pod.ports} | set(extra), key=lambda p: (p[1], p[0]))


def state_of(verdict: Verdict) -> str:
    return ALLOWED if verdict.allowed else BLOCKED


def build_matrix(cluster: Cluster, namespaces: Sequence[str] = (), probe_ports: Sequence[PortSpec] = (), externals: Sequence[Endpoint] = (), dns: bool = False) -> Matrix:
    present = {p.namespace for p in cluster.pods}
    scope = list(dict.fromkeys(namespaces)) or sorted(present - {SYSTEM_NAMESPACE})
    for name in scope:
        if name not in present:
            raise InputError(f"no pods with an IP in namespace {name}; pass kubectl get pods -A -o json")
    pods = sorted((p for p in cluster.pods if p.namespace in scope), key=lambda p: (scope.index(p.namespace), p.name))
    backends = cluster.dns_backends() if dns else []
    if dns and not backends:
        raise InputError(f"--dns: no {SYSTEM_NAMESPACE} pods labelled k8s-app=kube-dns in the input; pass kubectl get pods -A -o json")
    cells: List[Cell] = []
    for source in pods:
        origin = endpoint_of(source)
        for target in pods:
            if target.key == source.key:
                continue
            for port, protocol in target_ports(target, probe_ports):
                verdict = evaluate(cluster, origin, endpoint_of(target), port, protocol)
                cells.append(Cell(source.key, target.key, "pod", target.ip, port, protocol, state_of(verdict), [summarize(verdict.egress), summarize(verdict.ingress)]))
        for external in externals:
            for port, protocol in external.ports:
                verdict = evaluate(cluster, origin, external, port, protocol)
                cells.append(Cell(source.key, external.key, "external", external.ip, port, protocol, state_of(verdict), [summarize(verdict.egress), summarize(verdict.ingress)]))
        if dns:
            verdicts = [evaluate(cluster, origin, endpoint_of(b), 53, "UDP") for b in backends]
            allowed = sum(1 for v in verdicts if v.allowed)
            state = ALLOWED if allowed == len(verdicts) else BLOCKED if allowed == 0 else PARTIAL
            reasons = [summarize(verdicts[0].egress), summarize(verdicts[0].ingress)]
            if state == PARTIAL:
                reasons = [f"{allowed} of {len(verdicts)} kube-dns pods allowed: lookups fail whenever the Service picks another one"] + [f"{v.destination.key}: {summarize(v.egress)}" for v in verdicts]
            cells.append(Cell(source.key, DNS_KEY, "dns", "", 53, "UDP", state, reasons))
    targets = [p.key for p in pods] + [e.key for e in externals] + ([DNS_KEY] if dns else [])
    return Matrix(scope, pods, targets, cells, sorted(set(probe_ports), key=lambda p: (p[1], p[0])), list(externals))


def short_labels(keys: Sequence[str]) -> Dict[str, str]:
    names = [k.split("/", 1)[1] for k in keys if "/" in k]
    return {k: (k.split("/", 1)[1] if "/" in k and names.count(k.split("/", 1)[1]) == 1 else k) for k in keys}


def cell_text(cells: Sequence[Cell]) -> str:
    parts = []
    for cell in cells:
        label = str(cell.port) if cell.protocol == "TCP" else f"{cell.port}/{cell.protocol}"
        if cell.state == ALLOWED:
            parts.append(label)
        elif cell.state == PARTIAL:
            parts.append("~" + label)
    return ",".join(parts) or "-"


def render_matrix(matrix: Matrix, cluster: Cluster) -> str:
    labels = short_labels(matrix.targets)
    probes = ", ".join(f"{p}/{proto}" for p, proto in matrix.probe_ports) or "declared container ports only"
    lines = [f"{len(matrix.sources)} pods in {', '.join(matrix.namespaces)}; probe ports: {probes}"]
    lines.extend(f"note: {n}" for n in cluster.notes)
    lines.append("isolation (the policies that select each pod; - means not isolated, that end allows everything):")
    width = max([len(labels[p.key]) for p in matrix.sources] + [4])
    isolation = [
        (labels[p.key], ", ".join(x.name for x in selecting_policies(cluster, p, INGRESS)) or "-", ", ".join(x.name for x in selecting_policies(cluster, p, EGRESS)) or "-")
        for p in matrix.sources
    ]
    inner = max([len(i) for _, i, _ in isolation] + [1])
    for name, ingress, egress in isolation:
        lines.append(f"  {name:<{width}}  ingress: {ingress:<{inner}}  egress: {egress}")
    lines.append("allowed ports, row to column (- none, . self, ~ only some kube-dns pods):")
    grid: Dict[Tuple[str, str], List[Cell]] = {}
    for cell in matrix.cells:
        grid.setdefault((cell.source, cell.target), []).append(cell)
    rows = [[""] + [labels[t] for t in matrix.targets]]
    for pod in matrix.sources:
        rows.append([labels[pod.key]] + ["." if t == pod.key else cell_text(grid.get((pod.key, t), [])) for t in matrix.targets])
    widths = [max(len(r[i]) for r in rows) for i in range(len(rows[0]))]
    for row in rows:
        lines.append(("  " + "  ".join(v.ljust(widths[i]) for i, v in enumerate(row))).rstrip())
    lines.append(f"{sum(1 for c in matrix.cells if c.allowed)} of {len(matrix.cells)} connections allowed")
    return "\n".join(lines)


def policies_json(cluster: Cluster) -> List[Dict[str, Any]]:
    return [
        {
            "key": p.key,
            "policyTypes": p.types,
            "selects": [pod.key for pod in cluster.pods if pod.namespace == p.namespace and selector_matches(p.pod_selector, pod.labels)],
        }
        for p in cluster.policies
    ]


def cell_json(cell: Cell) -> Dict[str, Any]:
    return {
        "from": cell.source,
        "to": cell.target,
        "kind": cell.kind,
        "address": cell.address,
        "port": cell.port,
        "protocol": cell.protocol,
        "state": cell.state,
        "allowed": cell.allowed,
        "reasons": cell.reasons,
    }


def matrix_json(matrix: Matrix, cluster: Cluster) -> Dict[str, Any]:
    return {
        "namespaces": matrix.namespaces,
        "probe_ports": [f"{p}/{proto}" for p, proto in matrix.probe_ports],
        "pods": [
            {
                "key": p.key,
                "ip": p.ip,
                "labels": p.labels,
                "ports": [{"name": c.name, "port": c.port, "protocol": c.protocol} for c in p.ports],
                "ingress": [x.name for x in selecting_policies(cluster, p, INGRESS)],
                "egress": [x.name for x in selecting_policies(cluster, p, EGRESS)],
            }
            for p in matrix.sources
        ],
        "externals": [{"name": e.key, "ip": e.ip, "ports": [f"{p}/{proto}" for p, proto in e.ports]} for e in matrix.externals],
        "policies": policies_json(cluster),
        "cells": [cell_json(c) for c in matrix.cells],
        "allowed": sum(1 for c in matrix.cells if c.allowed),
        "total": len(matrix.cells),
        "notes": cluster.notes,
    }


def describe_side(side: Side, end: Endpoint) -> List[str]:
    title = f"egress from {end.key}" if side.direction == EGRESS else f"ingress to {end.key}"
    if side.subject is None:
        return [f"  {title}: not a pod, so no NetworkPolicy applies at this end"]
    if not side.isolating:
        return [f"  {title}: not isolated, no NetworkPolicy in {side.namespace} selects it for {side.direction}; this end allows everything"]
    lines = [f"  {title}: isolated by {', '.join(side.isolating)}"]
    deciding = side.deciding
    if deciding is not None:
        lines.append(f"    allowed by {deciding.where}: {deciding.peer_text}; {deciding.port_text}")
        return lines
    lines.append("    nothing allows it; every rule that was checked:")
    for name in side.isolating:
        checks = [c for c in side.checks if c.policy == name]
        if not checks:
            lines.append(f"    {name}: no {RULES_FIELD[side.direction]} rules, it only isolates")
        for check in checks:
            lines.append(f"    {check.where}: peer {'yes' if check.peer_ok else 'no'}, {check.peer_text}; port {'yes' if check.port_ok else 'no'}, {check.port_text}")
    return lines


def render_explain(verdict: Verdict) -> str:
    v = verdict
    lines = [f"{v.source.key} ({v.source.ip}) -> {v.destination.key} ({v.destination.ip}) {v.port}/{v.protocol}: {'allowed' if v.allowed else 'DENIED'}"]
    lines.extend(describe_side(v.egress, v.source))
    lines.extend(describe_side(v.ingress, v.destination))
    if not v.allowed:
        blocking = [s for s in (v.egress, v.ingress) if not s.allowed]
        ends = " and ".join(f"the {s.direction.lower()} side of {s.subject}" for s in blocking)
        lines.append(f"  a connection needs both ends to allow it: change {ends}")
    return "\n".join(lines)


def side_json(side: Side) -> Dict[str, Any]:
    deciding = side.deciding
    return {
        "direction": side.direction,
        "pod": side.subject,
        "isolated_by": side.isolating,
        "allowed": side.allowed,
        "allowed_by": deciding.where if deciding else None,
        "rules": [{"rule": c.where, "peer": c.peer_ok, "port": c.port_ok, "peer_detail": c.peer_text, "port_detail": c.port_text} for c in side.checks],
    }


def verdict_json(verdict: Verdict) -> Dict[str, Any]:
    return {
        "from": verdict.source.key,
        "to": verdict.destination.key,
        "port": verdict.port,
        "protocol": verdict.protocol,
        "allowed": verdict.allowed,
        "egress": side_json(verdict.egress),
        "ingress": side_json(verdict.ingress),
    }


def namespace_only(peer: Peer) -> bool:
    return peer.namespace_selector is not None and peer.pod_selector is None and peer.cidr is None


def pod_only(peer: Peer) -> bool:
    return peer.pod_selector is not None and peer.namespace_selector is None and peer.cidr is None


def lint_spec(policy: Policy) -> List[Finding]:
    found: List[Finding] = []
    spec = policy.spec

    def add(severity: str, code: str, message: str) -> None:
        found.append(Finding(severity, policy.key, code, message))

    if "podSelector" not in spec:
        add(WARNING, "podselector-missing", f"spec.podSelector is missing: the API server stores it as {{}}, which selects every pod in {policy.namespace}; write podSelector: {{}} if you mean that")
    elif policy.pod_selector.empty:
        add(INFO, "selects-namespace", f"podSelector {{}} selects every pod in {policy.namespace}")
    if "egress" in spec and not spec.get("egress") and not spec.get("policyTypes"):
        add(WARNING, "egress-not-isolated", "spec.egress is empty and policyTypes is not set: the API server defaults policyTypes to [Ingress], so this policy denies ingress and leaves egress open; set policyTypes: [Ingress, Egress] to deny both")
    for direction in POLICY_TYPES:
        rules = policy.rules(direction)
        name = RULES_FIELD[direction]
        if rules and direction not in policy.types:
            add(WARNING, "ignored-rules", f"spec.{name} has {len(rules)} rule(s) but policyTypes is [{', '.join(policy.types)}]: they are ignored")
            continue
        peer_field = PEER_FIELD[direction]
        for index, rule in enumerate(rules):
            here = f"{name}[{index}]"
            if not rule.peers and not rule.ports:
                add(INFO, "allow-all", f"{here} has no {peer_field} and no ports: it allows every {OTHER_END[direction]} on every port, inside and outside the cluster")
            for j in range(len(rule.peers) - 1):
                first, second = rule.peers[j], rule.peers[j + 1]
                if namespace_only(first) and pod_only(second):
                    namespaces, pods = first, second
                elif pod_only(first) and namespace_only(second):
                    namespaces, pods = second, first
                else:
                    continue
                meant = describe_peer(Peer(pods.pod_selector, namespaces.namespace_selector), policy.namespace)
                add(
                    WARNING,
                    "or-peers",
                    f"{here}.{peer_field}[{j}] and [{j + 1}] are two peers, combined with OR: {describe_peer(first, policy.namespace)}, or {describe_peer(second, policy.namespace)}. If you meant {meant}, put both selectors in one element",
                )
    return found


def lint_against_pods(cluster: Cluster, policy: Policy) -> List[Finding]:
    found: List[Finding] = []

    def add(severity: str, code: str, message: str) -> None:
        found.append(Finding(severity, policy.key, code, message))

    selected = [p for p in cluster.pods if p.namespace == policy.namespace and selector_matches(policy.pod_selector, p.labels)]
    if not selected:
        add(WARNING, "selects-nothing", f"podSelector {describe_selector(policy.pod_selector)} selects no pod in {policy.namespace}: the policy has no effect yet")
    for direction in policy.types:
        peer_field = PEER_FIELD[direction]
        for index, rule in enumerate(policy.rules(direction)):
            here = f"{RULES_FIELD[direction]}[{index}]"
            reached: List[Pod] = []
            for j, peer in enumerate(rule.peers):
                if peer.cidr is not None:
                    inside = [p.key for p in cluster.pods if in_block(p.ip, peer.cidr, peer.excepts)]
                    if inside:
                        shown = ", ".join(inside[:3]) + (", ..." if len(inside) > 3 else "")
                        add(
                            WARNING,
                            "ipblock-pods",
                            f"{here}.{peer_field}[{j}] ipBlock {peer.cidr} contains the IPs of {len(inside)} pod(s) ({shown}): ipBlock is meant for addresses outside the cluster, and whether it matches pod traffic depends on the network plugin; select pods with podSelector or namespaceSelector",
                        )
                    continue
                matched = [p for p in cluster.pods if peer_matches(cluster, policy.namespace, peer, endpoint_of(p))]
                reached.extend(matched)
                if not matched:
                    hint = ""
                    if peer.namespace_selector is not None and "name" in peer.namespace_selector.match_labels and not any("name" in labels for labels in cluster.namespaces.values()):
                        hint = f"; no namespace carries a label called name, the API server sets {NAME_LABEL} on every namespace"
                    add(WARNING, "peer-selects-nothing", f"{here}.{peer_field}[{j}] ({describe_peer(peer, policy.namespace)}) matches no pod in the input{hint}")
            destinations = selected if direction == INGRESS else (reached if rule.peers else cluster.pods)
            for k, rule_port in enumerate(rule.ports):
                if isinstance(rule_port.port, str) and not any(cp.name == rule_port.port and cp.protocol == rule_port.protocol for p in destinations for cp in p.ports):
                    whose = "selected" if direction == INGRESS else "destination"
                    add(WARNING, "named-port-missing", f"{here}.ports[{k}] names port {rule_port.port}/{rule_port.protocol}, which no {whose} pod declares: the entry matches nothing")
    return found


def rule_may_allow_dns(rule: Rule) -> bool:
    if not rule.ports:
        return True
    for rule_port in rule.ports:
        if rule_port.protocol != "UDP":
            continue
        if rule_port.port is None or isinstance(rule_port.port, str):
            return True
        if rule_port.port <= 53 <= (rule_port.end_port or rule_port.port):
            return True
    return False


def lint_dns(cluster: Cluster) -> List[Finding]:
    hint = f"allow egress to pods k8s-app=kube-dns in namespace {SYSTEM_NAMESPACE} on 53/UDP and 53/TCP"
    isolated = [p for p in cluster.pods if p.namespace != SYSTEM_NAMESPACE and selecting_policies(cluster, p, EGRESS)]
    backends = cluster.dns_backends()
    if isolated and backends:
        no_udp, no_tcp = [], []
        for pod in isolated:
            origin = endpoint_of(pod)
            udp = any(evaluate(cluster, origin, endpoint_of(b), 53, "UDP").allowed for b in backends)
            tcp = any(evaluate(cluster, origin, endpoint_of(b), 53, "TCP").allowed for b in backends)
            if not udp:
                no_udp.append(pod.key)
            elif not tcp:
                no_tcp.append(pod.key)
        found = []
        if no_udp:
            found.append(Finding(WARNING, ", ".join(sorted({k.split('/')[0] for k in no_udp})), "dns-egress", f"{len(no_udp)} pod(s) are isolated for egress and cannot reach kube-dns on 53/UDP ({', '.join(no_udp)}): every name lookup will time out; {hint}"))
        if no_tcp:
            found.append(Finding(INFO, ", ".join(sorted({k.split('/')[0] for k in no_tcp})), "dns-tcp", f"{len(no_tcp)} pod(s) reach kube-dns on 53/UDP but not 53/TCP ({', '.join(no_tcp)}): an answer too large for UDP is truncated and the retry over TCP fails"))
        return found
    namespaces = sorted({p.namespace for p in isolated}) if cluster.pods else sorted({p.namespace for p in cluster.policies if EGRESS in p.types})
    found = []
    for name in namespaces:
        rules = [r for p in cluster.policies if p.namespace == name and EGRESS in p.types for r in p.egress]
        if not any(rule_may_allow_dns(r) for r in rules):
            found.append(Finding(WARNING, name, "dns-egress", f"pods in {name} are isolated for egress and no egress rule there allows 53/UDP: every name lookup will time out; {hint}"))
    return found


def lint(cluster: Cluster) -> List[Finding]:
    found: List[Finding] = []
    for policy in cluster.policies:
        found.extend(lint_spec(policy))
        if cluster.pods:
            found.extend(lint_against_pods(cluster, policy))
    found.extend(lint_dns(cluster))
    return found


def render_lint(findings: Sequence[Finding]) -> str:
    lines = [f"{f.severity}: {f.subject}: {f.code}: {f.message}" for f in findings]
    warnings = sum(1 for f in findings if f.severity == WARNING)
    lines.append(f"{warnings} warning(s), {len(findings) - warnings} info")
    return "\n".join(lines)


def tcp_probe(address: str, port: int, timeout: float) -> Tuple[bool, str]:
    try:
        with socket.create_connection((address, port), timeout=timeout):
            return True, "connected"
    except socket.timeout:
        return False, "timeout"
    except ConnectionRefusedError:
        return False, "refused"
    except OSError as exc:
        return False, exc.strerror or exc.__class__.__name__


def dns_query(name: str, query_id: int) -> bytes:
    question = b"".join(bytes([len(label)]) + label.encode("ascii") for label in name.strip(".").split(".")) + b"\x00"
    return struct.pack("!HHHHHH", query_id, 0x0100, 1, 0, 0, 0) + question + struct.pack("!HH", 1, 1)


def dns_probe(server: str, name: str, timeout: float, port: int = 53) -> Tuple[bool, str]:
    query_id = int.from_bytes(os.urandom(2), "big")
    family = socket.AF_INET6 if ":" in server else socket.AF_INET
    with socket.socket(family, socket.SOCK_DGRAM) as sock:
        sock.settimeout(timeout)
        try:
            sock.sendto(dns_query(name, query_id), (server, port))
            while True:
                data, _ = sock.recvfrom(4096)
                if len(data) >= 4 and struct.unpack("!H", data[:2])[0] == query_id and data[2] & 0x80:
                    code = data[3] & 0x0F
                    return True, f"answer {RCODES.get(code, str(code))}"
        except socket.timeout:
            return False, "timeout"
        except OSError as exc:
            return False, exc.strerror or exc.__class__.__name__


def nameserver(path: str) -> str:
    try:
        with open(path, encoding="utf-8") as handle:
            for line in handle:
                parts = line.split()
                if len(parts) >= 2 and parts[0] == "nameserver":
                    return parts[1]
    except OSError as exc:
        raise InputError(f"cannot read {path}: {exc.strerror}")
    raise InputError(f"{path} has no nameserver line")


def run_probes(cells: Sequence[Dict[str, Any]], timeout: float, dns_server: Optional[str], dns_name: str = DNS_QUERY_NAME) -> List[Dict[str, Any]]:
    def one(cell: Dict[str, Any]) -> Dict[str, Any]:
        outcome: Dict[str, Any] = {"to": cell["to"], "port": cell["port"], "protocol": cell["protocol"]}
        if cell.get("kind") == "dns":
            if not dns_server:
                return dict(outcome, allowed=None, result="no nameserver to ask")
            ok, text = dns_probe(dns_server, dns_name, timeout)
        elif cell.get("protocol") == "TCP":
            ok, text = tcp_probe(cell["address"], int(cell["port"]), timeout)
        else:
            return dict(outcome, allowed=None, result=f"not probed: only TCP and DNS over UDP are probed, not {cell.get('protocol')}")
        return dict(outcome, allowed=ok, result=text)

    if not cells:
        return []
    with ThreadPoolExecutor(max_workers=min(32, len(cells))) as pool:
        return list(pool.map(one, cells))


def compare(predicted: Dict[str, Any], measured: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    results: Dict[Tuple[str, str, int, str], Dict[str, Any]] = {}
    sources = set()
    for doc in measured:
        source = doc.get("from")
        sources.add(source)
        for cell in doc.get("cells") or []:
            results[(source, cell["to"], int(cell["port"]), cell["protocol"])] = cell
    differ: List[str] = []
    skipped: List[str] = []
    compared = 0
    for cell in predicted.get("cells") or []:
        if cell["from"] not in sources:
            continue
        label = f"{cell['from']} -> {cell['to']} {cell['port']}/{cell['protocol']}"
        if cell["state"] == PARTIAL:
            skipped.append(f"{label}: only some kube-dns pods are allowed, so the answer depends on which one the Service picks")
            continue
        got = results.get((cell["from"], cell["to"], int(cell["port"]), cell["protocol"]))
        if got is None:
            differ.append(f"{label}: predicted {cell['state']}, not measured")
            continue
        if got.get("allowed") is None:
            skipped.append(f"{label}: {got.get('result')}")
            continue
        compared += 1
        if bool(got["allowed"]) != bool(cell["allowed"]):
            text = f"{label}: predicted {cell['state']}, measured {got.get('result')}"
            if cell["allowed"] and got.get("result") == "refused":
                text += " (connection refused: nothing listens there, which is not a policy decision)"
            differ.append(text)
    return {"equal": not differ, "compared": compared, "differ": differ, "skipped": skipped, "sources": sorted(s for s in sources if s)}


def render_compare(report: Dict[str, Any]) -> str:
    verdict = "identical" if report["equal"] else f"{len(report['differ'])} differ"
    lines = [f"compared {report['compared']} connections from {len(report['sources'])} pod(s): {verdict}"]
    lines.extend(f"  differ: {d}" for d in report["differ"])
    lines.extend(f"  skipped: {s}" for s in report["skipped"])
    return "\n".join(lines)


class Server:
    def __init__(self, ports: Sequence[int], host: str = "0.0.0.0", banner: str = "mininetpol"):
        self.ports = list(ports)
        self.host = host
        self.banner = banner
        self.sockets: List[socket.socket] = []
        self.stopped = threading.Event()

    def start(self) -> List[int]:
        bound = []
        for port in self.ports:
            listener = socket.socket(socket.AF_INET6 if ":" in self.host else socket.AF_INET, socket.SOCK_STREAM)
            self.sockets.append(listener)
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            listener.bind((self.host, port))
            listener.listen(128)
            listener.settimeout(0.2)
            bound.append(listener.getsockname()[1])
            threading.Thread(target=self.accept_loop, args=(listener, bound[-1]), daemon=True).start()
        return bound

    def accept_loop(self, listener: socket.socket, port: int) -> None:
        while not self.stopped.is_set():
            try:
                connection, _ = listener.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            with connection:
                try:
                    connection.sendall(f"{self.banner} {port}\n".encode())
                except OSError:
                    pass

    def stop(self) -> None:
        self.stopped.set()
        for listener in self.sockets:
            listener.close()


def read_json(path: str) -> Any:
    try:
        if path == "-":
            return json.load(sys.stdin)
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)
    except OSError as exc:
        raise InputError(f"cannot read {path}: {exc.strerror}")
    except json.JSONDecodeError as exc:
        raise InputError(f"{path}: not JSON ({exc.msg} at line {exc.lineno}); mininetpol reads what kubectl get ... -o json prints")


def parse_port_option(text: str) -> PortSpec:
    number, _, protocol = text.partition("/")
    protocol = (protocol or "TCP").upper()
    if protocol not in PROTOCOLS or not number.isdigit() or not 1 <= int(number) <= 65535:
        raise InputError(f"--port {text!r}: expected PORT or PORT/PROTOCOL, for example 8080 or 53/UDP")
    return int(number), protocol


def parse_external(text: str) -> Tuple[str, str, PortSpec]:
    name, equals, rest = text.partition("=")
    address, colon, port = rest.rpartition(":")
    address = address.strip("[]")
    if not equals or not name or not colon or not address:
        raise InputError(f"--external {text!r}: expected NAME=IP:PORT[/PROTOCOL], for example ext-a=172.18.0.5:8080")
    try:
        ipaddress.ip_address(address)
    except ValueError:
        raise InputError(f"--external {text!r}: {address!r} is not an IP address")
    return name, address, parse_port_option(port)


def externals_from(options: Optional[Sequence[str]]) -> List[Endpoint]:
    found: Dict[str, Endpoint] = {}
    for text in options or []:
        name, address, port = parse_external(text)
        endpoint = found.setdefault(name, Endpoint(name, address))
        if endpoint.ip != address:
            raise InputError(f"--external {name} has two addresses: {endpoint.ip} and {address}")
        if port not in endpoint.ports:
            endpoint.ports.append(port)
    return list(found.values())


def resolve_endpoint(cluster: Cluster, text: str, externals: Sequence[Endpoint]) -> Endpoint:
    for external in externals:
        if external.key == text:
            return external
    if "/" in text:
        pod = cluster.pod_by_key(text)
        if pod is None:
            raise InputError(f"no pod {text} with an IP in the input" + (f" ({'; '.join(cluster.notes)})" if cluster.notes else ""))
        return endpoint_of(pod)
    try:
        ipaddress.ip_address(text)
    except ValueError:
        matches = [p for p in cluster.pods if p.name == text]
        if len(matches) == 1:
            return endpoint_of(matches[0])
        if matches:
            raise InputError(f"{text} is ambiguous: {', '.join(p.key for p in matches)}; write NAMESPACE/NAME")
        raise InputError(f"no pod, IP or --external named {text}")
    pod = next((p for p in cluster.pods if p.ip == text), None)
    return endpoint_of(pod) if pod else Endpoint(text, text)


def resolve_port(text: str, destination: Endpoint) -> PortSpec:
    name, _, protocol = text.partition("/")
    if name.isdigit():
        return parse_port_option(text)
    ports = destination.pod.ports if destination.pod else []
    for container_port in ports:
        if container_port.name == name and (not protocol or container_port.protocol == protocol.upper()):
            return container_port.port, container_port.protocol
    declared = ", ".join(f"{c.name or '(unnamed)'}={c.port}/{c.protocol}" for c in ports) or "none"
    raise InputError(f"--port {text}: {destination.key} declares no container port named {name} (it declares: {declared})")


def load_files(paths: Sequence[str], default_namespace: str) -> Cluster:
    return load_cluster((read_json(p) for p in paths), default_namespace)


def cmd_matrix(args: argparse.Namespace) -> int:
    cluster = load_files(args.file, args.default_namespace)
    matrix = build_matrix(cluster, args.namespace or [], [parse_port_option(p) for p in args.port or []], externals_from(args.external), args.dns)
    print(json.dumps(matrix_json(matrix, cluster), indent=2) if args.json else render_matrix(matrix, cluster))
    return 0


def cmd_explain(args: argparse.Namespace) -> int:
    cluster = load_files(args.file, args.default_namespace)
    externals = externals_from(args.external)
    source = resolve_endpoint(cluster, args.source, externals)
    destination = resolve_endpoint(cluster, args.destination, externals)
    port, protocol = resolve_port(args.port, destination)
    verdict = evaluate(cluster, source, destination, port, protocol)
    print(json.dumps(verdict_json(verdict), indent=2) if args.json else render_explain(verdict))
    return 0 if verdict.allowed else 1


def cmd_lint(args: argparse.Namespace) -> int:
    cluster = load_files(args.file, args.default_namespace)
    findings = lint(cluster)
    if args.json:
        print(json.dumps({"findings": [f.__dict__ for f in findings], "policies": policies_json(cluster)}, indent=2))
    else:
        print(render_lint(findings))
    return 1 if any(f.severity == WARNING for f in findings) else 0


def cmd_probe(args: argparse.Namespace) -> int:
    predicted = read_json(args.predicted)
    cells = [c for c in predicted.get("cells") or [] if c.get("from") == args.source]
    if not cells:
        sources = sorted({c.get("from") for c in predicted.get("cells") or []})
        raise InputError(f"no cells from {args.source} in {args.predicted}; sources there: {', '.join(sources) or 'none'}")
    server = nameserver(args.resolv_conf) if any(c.get("kind") == "dns" for c in cells) else None
    results = run_probes(cells, args.timeout, server, args.dns_name)
    print(json.dumps({"from": args.source, "timeout": args.timeout, "nameserver": server, "cells": results}, indent=2))
    return 0


def cmd_compare(args: argparse.Namespace) -> int:
    report = compare(read_json(args.predicted), [read_json(p) for p in args.measured])
    print(json.dumps(report, indent=2) if args.json else render_compare(report))
    return 0 if report["equal"] else 1


def cmd_serve(args: argparse.Namespace) -> int:
    server = Server(args.port, args.host, os.environ.get("HOSTNAME") or socket.gethostname())
    try:
        bound = server.start()
    except OSError as exc:
        server.stop()
        print(f"mininetpol: cannot listen on {args.host}: {exc.strerror}", file=sys.stderr)
        return 1
    stop = threading.Event()
    for signum in (signal.SIGTERM, signal.SIGINT):
        signal.signal(signum, lambda *_: stop.set())
    print("mininetpol serve: listening on " + ", ".join(f"{args.host}:{p}/TCP" for p in bound), flush=True)
    while not stop.wait(1):
        pass
    server.stop()
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="mininetpol", description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)

    def cluster_command(name: str, text: str) -> argparse.ArgumentParser:
        command = commands.add_parser(name, help=text, description=text)
        command.add_argument("-f", "--file", action="append", required=True, help="JSON from kubectl get pods,namespaces,networkpolicies -A -o json, or manifests; repeat it; a later file replaces objects of the same name; - reads stdin")
        command.add_argument("--default-namespace", default="default", help="namespace for manifests without metadata.namespace")
        command.add_argument("--json", action="store_true", help="machine-readable output")
        return command

    matrix = cluster_command("matrix", "predict every pod-to-pod, pod-to-address and pod-to-DNS connection")
    matrix.add_argument("--namespace", action="append", help="pods of this namespace are rows and columns; repeat it (default: every namespace but kube-system)")
    matrix.add_argument("--port", action="append", help="also probe PORT[/PROTOCOL] on every pod, declared or not (default: declared container ports)")
    matrix.add_argument("--external", action="append", help="NAME=IP:PORT[/PROTOCOL], an address outside the cluster to add as a column")
    matrix.add_argument("--dns", action="store_true", help="add a dns column: 53/UDP to the kube-dns pods")
    matrix.set_defaults(handler=cmd_matrix)

    explain = cluster_command("explain", "name the policy and rule that allow one connection, or every rule that did not")
    explain.add_argument("--from", dest="source", required=True, help="NAMESPACE/POD, a unique pod name, an IP or an --external name")
    explain.add_argument("--to", dest="destination", required=True, help="NAMESPACE/POD, a unique pod name, an IP or an --external name")
    explain.add_argument("--port", required=True, help="PORT[/PROTOCOL], or a container port name of the destination such as http")
    explain.add_argument("--external", action="append", help="NAME=IP:PORT[/PROTOCOL]")
    explain.set_defaults(handler=cmd_explain)

    lint_command = cluster_command("lint", "find the common NetworkPolicy traps; exit 1 on any warning")
    lint_command.set_defaults(handler=cmd_lint)

    probe = commands.add_parser("probe", help="run inside a pod: try every connection a matrix --json predicts from this pod")
    probe.add_argument("predicted", help="the output of matrix --json; - reads stdin")
    probe.add_argument("--from", dest="source", required=True, help="NAMESPACE/POD of the pod this runs in")
    probe.add_argument("--timeout", type=float, default=1.5, help="seconds per connection attempt (default 1.5)")
    probe.add_argument("--resolv-conf", default="/etc/resolv.conf", help="where to find the DNS server for the dns column")
    probe.add_argument("--dns-name", default=DNS_QUERY_NAME, help="the name to look up")
    probe.set_defaults(handler=cmd_probe)

    compare_command = commands.add_parser("compare", help="compare a matrix --json prediction with probe results; exit 1 if any connection differs")
    compare_command.add_argument("predicted")
    compare_command.add_argument("measured", nargs="+")
    compare_command.add_argument("--json", action="store_true")
    compare_command.set_defaults(handler=cmd_compare)

    serve = commands.add_parser("serve", help="listen on TCP ports and answer every connection with the host name, for test pods")
    serve.add_argument("--port", type=int, action="append", required=True)
    serve.add_argument("--host", default="0.0.0.0")
    serve.set_defaults(handler=cmd_serve)
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(list(sys.argv[1:] if argv is None else argv))
    try:
        return args.handler(args)
    except InputError as exc:
        print(f"mininetpol: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
