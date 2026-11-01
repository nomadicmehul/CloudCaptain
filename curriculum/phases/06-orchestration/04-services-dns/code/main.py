"""miniproxy: compute the EndpointSlices, kube-proxy rules and cluster DNS answers that Services and Pods produce, and trace a Service through a real node's ruleset."""
from __future__ import annotations

import argparse
import hashlib
import ipaddress
import json
import random
import re
import shlex
import sys
from collections import Counter
from dataclasses import asdict, dataclass, field
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple, Union

ZONE = "cluster.local"
MASQ_MARK = "0x4000"
MAX_ENDPOINTS_PER_SLICE = 100
MANAGED_BY = "endpointslice-controller.k8s.io"
SERVICE_NAME_LABEL = "kubernetes.io/service-name"
MANAGED_BY_LABEL = "endpointslice.kubernetes.io/managed-by"
DEFAULT_AFFINITY_SECONDS = 10800
STATISTIC_SCALE = 0x80000000
TERMINAL_PHASES = ("Succeeded", "Failed")
BUILTIN_CHAINS = {"nat": ("PREROUTING", "INPUT", "OUTPUT", "POSTROUTING"), "filter": ("INPUT", "FORWARD", "OUTPUT")}
NODEPORTS_RULE = '-A KUBE-SERVICES -m comment --comment "kubernetes service nodeports; NOTE: this must be the last rule in this chain" -m addrtype --dst-type LOCAL -j KUBE-NODEPORTS'


class InputError(Exception):
    pass


@dataclass
class ServicePort:
    name: str
    port: int
    target_port: Union[int, str]
    protocol: str = "TCP"
    node_port: Optional[int] = None


@dataclass
class Service:
    namespace: str
    name: str
    cluster_ip: Optional[str]
    ports: List[ServicePort]
    selector: Dict[str, str] = field(default_factory=dict)
    type: str = "ClusterIP"
    publish_not_ready: bool = False
    session_affinity: str = "None"
    affinity_seconds: int = DEFAULT_AFFINITY_SECONDS
    external_traffic_policy: str = "Cluster"
    external_name: Optional[str] = None

    @property
    def key(self) -> str:
        return f"{self.namespace}/{self.name}"

    @property
    def headless(self) -> bool:
        return self.cluster_ip == "None"

    @property
    def virtual_ip(self) -> Optional[str]:
        if self.type == "ExternalName" or self.cluster_ip in (None, "", "None"):
            return None
        return self.cluster_ip


@dataclass
class ContainerPort:
    name: str
    port: int
    protocol: str = "TCP"


@dataclass
class Pod:
    namespace: str
    name: str
    labels: Dict[str, str]
    ip: Optional[str]
    node_name: Optional[str] = None
    phase: str = "Running"
    ready: bool = False
    terminating: bool = False
    ports: List[ContainerPort] = field(default_factory=list)
    hostname: Optional[str] = None
    subdomain: Optional[str] = None


@dataclass
class EndpointPort:
    name: str
    port: int
    protocol: str = "TCP"


@dataclass
class Endpoint:
    address: str
    ready: bool
    serving: bool
    terminating: bool
    node_name: Optional[str] = None
    pod: Optional[str] = None
    hostname: Optional[str] = None


@dataclass
class EndpointSlice:
    namespace: str
    name: str
    service: str
    ports: List[EndpointPort]
    endpoints: List[Endpoint]
    managed_by: str = MANAGED_BY
    address_type: str = "IPv4"


@dataclass
class Cluster:
    services: List[Service]
    pods: List[Pod]
    slices: List[EndpointSlice]

    def service(self, ref: str) -> Optional[Service]:
        namespace, _, name = ref.rpartition("/")
        for service in self.services:
            if service.name == name and (not namespace or service.namespace == namespace):
                return service
        return None


def flatten(data: Any, source: str) -> List[Dict[str, Any]]:
    if isinstance(data, list):
        return [obj for item in data for obj in flatten(item, source)]
    if isinstance(data, dict) and isinstance(data.get("items"), list):
        return flatten(data["items"], source)
    if isinstance(data, dict) and "kind" in data:
        return [data]
    raise InputError(f"{source}: expected a Kubernetes object or a List with items (kubectl get ... -o json)")


def load_objects(paths: Sequence[str]) -> List[Dict[str, Any]]:
    objects: List[Dict[str, Any]] = []
    for path in paths:
        try:
            with open(path, encoding="utf-8") as handle:
                data = json.load(handle)
        except OSError as exc:
            raise InputError(f"{path}: {exc.strerror or exc}") from None
        except json.JSONDecodeError as exc:
            raise InputError(f"{path}: not JSON ({exc.msg}, line {exc.lineno}); dump objects with kubectl get svc,pods,endpointslices -o json") from None
        objects.extend(flatten(data, path))
    return objects


def metadata(obj: Dict[str, Any]) -> Dict[str, Any]:
    meta = obj.get("metadata") or {}
    if not meta.get("name"):
        raise InputError(f"a {obj.get('kind', 'object')} without metadata.name")
    return meta


def port_number(value: Any, where: str) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        raise InputError(f"{where}: port {value!r} is not a number") from None


def protocol_of(item: Dict[str, Any]) -> str:
    return str(item.get("protocol") or "TCP").upper()


def parse_service(obj: Dict[str, Any]) -> Service:
    meta = metadata(obj)
    spec = obj.get("spec") or {}
    where = f"Service {meta.get('namespace') or 'default'}/{meta['name']}"
    ports = []
    for item in spec.get("ports") or []:
        number = port_number(item.get("port"), where)
        target = item.get("targetPort", number)
        if isinstance(target, str) and target.isdigit():
            target = int(target)
        ports.append(ServicePort(item.get("name") or "", number, target, protocol_of(item), item.get("nodePort")))
    client_ip = (spec.get("sessionAffinityConfig") or {}).get("clientIP") or {}
    return Service(
        namespace=meta.get("namespace") or "default",
        name=meta["name"],
        cluster_ip=spec.get("clusterIP"),
        ports=ports,
        selector=dict(spec.get("selector") or {}),
        type=spec.get("type") or "ClusterIP",
        publish_not_ready=bool(spec.get("publishNotReadyAddresses")),
        session_affinity=spec.get("sessionAffinity") or "None",
        affinity_seconds=int(client_ip.get("timeoutSeconds") or DEFAULT_AFFINITY_SECONDS),
        external_traffic_policy=spec.get("externalTrafficPolicy") or "Cluster",
        external_name=spec.get("externalName"),
    )


def parse_pod(obj: Dict[str, Any]) -> Pod:
    meta = metadata(obj)
    spec = obj.get("spec") or {}
    status = obj.get("status") or {}
    where = f"Pod {meta.get('namespace') or 'default'}/{meta['name']}"
    conditions = {c.get("type"): c.get("status") for c in status.get("conditions") or []}
    ports = [
        ContainerPort(p.get("name") or "", port_number(p.get("containerPort"), where), protocol_of(p))
        for container in spec.get("containers") or []
        for p in container.get("ports") or []
    ]
    return Pod(
        namespace=meta.get("namespace") or "default",
        name=meta["name"],
        labels=dict(meta.get("labels") or {}),
        ip=status.get("podIP") or None,
        node_name=spec.get("nodeName"),
        phase=status.get("phase") or "Pending",
        ready=conditions.get("Ready") == "True",
        terminating=bool(meta.get("deletionTimestamp")),
        ports=ports,
        hostname=spec.get("hostname"),
        subdomain=spec.get("subdomain"),
    )


def parse_slice(obj: Dict[str, Any]) -> EndpointSlice:
    meta = metadata(obj)
    labels = meta.get("labels") or {}
    where = f"EndpointSlice {meta.get('namespace') or 'default'}/{meta['name']}"
    ports = [EndpointPort(p.get("name") or "", port_number(p.get("port"), where), protocol_of(p)) for p in obj.get("ports") or [] if p.get("port") is not None]
    endpoints = []
    for item in obj.get("endpoints") or []:
        conditions = item.get("conditions") or {}
        ready = conditions.get("ready") is not False
        serving = conditions.get("serving")
        target = item.get("targetRef") or {}
        for address in item.get("addresses") or []:
            endpoints.append(Endpoint(address, ready, ready if serving is None else bool(serving), bool(conditions.get("terminating")), item.get("nodeName"), target.get("name"), item.get("hostname")))
    return EndpointSlice(meta.get("namespace") or "default", meta["name"], labels.get(SERVICE_NAME_LABEL, ""), ports, endpoints, labels.get(MANAGED_BY_LABEL, ""), obj.get("addressType") or "IPv4")


def cluster_from(objects: Iterable[Dict[str, Any]]) -> Cluster:
    services: List[Service] = []
    pods: List[Pod] = []
    slices: List[EndpointSlice] = []
    for obj in objects:
        kind = obj.get("kind")
        if kind == "Service":
            services.append(parse_service(obj))
        elif kind == "Pod":
            pods.append(parse_pod(obj))
        elif kind == "EndpointSlice":
            slices.append(parse_slice(obj))
    return Cluster(sorted(services, key=lambda s: s.key), pods, slices)


def ip_key(address: Optional[str]) -> Tuple[int, int, str]:
    try:
        parsed = ipaddress.ip_address(address or "")
    except ValueError:
        return (9, 0, address or "")
    return (parsed.version, int(parsed), "")


def selects(service: Service, pod: Pod) -> bool:
    return bool(service.selector) and pod.namespace == service.namespace and all(pod.labels.get(k) == v for k, v in service.selector.items())


def in_endpoints(pod: Pod) -> bool:
    return pod.phase not in TERMINAL_PHASES and bool(pod.ip)


def resolve_target_port(port: ServicePort, pod: Pod) -> Optional[int]:
    if isinstance(port.target_port, int):
        return port.target_port
    for container_port in pod.ports:
        if container_port.name == port.target_port and container_port.protocol == port.protocol:
            return container_port.port
    return None


def endpoint_for(service: Service, pod: Pod) -> Endpoint:
    ready = service.publish_not_ready or (pod.ready and not pod.terminating)
    serving = service.publish_not_ready or pod.ready
    hostname = pod.hostname if pod.hostname and pod.subdomain == service.name else None
    return Endpoint(pod.ip or "", ready, serving, pod.terminating, pod.node_name, pod.name, hostname)


def short_hash(*parts: str, length: int = 16) -> str:
    return hashlib.sha256("|".join(parts).encode()).hexdigest().upper()[:length]


def reconcile_slices(service: Service, pods: Iterable[Pod], max_per_slice: int = MAX_ENDPOINTS_PER_SLICE) -> List[EndpointSlice]:
    if not service.selector or service.type == "ExternalName":
        return []
    groups: Dict[Tuple[Tuple[str, int, str], ...], List[Endpoint]] = {}
    for pod in sorted(pods, key=lambda p: (ip_key(p.ip), p.name)):
        if not selects(service, pod) or not in_endpoints(pod):
            continue
        ports = []
        for port in service.ports:
            number = resolve_target_port(port, pod)
            if number is not None:
                ports.append((port.name, number, port.protocol))
        groups.setdefault(tuple(ports), []).append(endpoint_for(service, pod))
    if not groups:
        return [EndpointSlice(service.namespace, f"{service.name}-{short_hash(service.key, length=5).lower()}", service.name, [], [])]
    slices = []
    for ports, endpoints in sorted(groups.items()):
        for start in range(0, len(endpoints), max_per_slice):
            name = f"{service.name}-{short_hash(service.key, repr(ports), str(start), length=5).lower()}"
            slices.append(EndpointSlice(service.namespace, name, service.name, [EndpointPort(*p) for p in ports], endpoints[start:start + max_per_slice]))
    return slices


def slices_for(cluster: Cluster, source: str = "auto") -> Dict[str, List[EndpointSlice]]:
    result: Dict[str, List[EndpointSlice]] = {}
    for service in cluster.services:
        given = [s for s in cluster.slices if s.namespace == service.namespace and s.service == service.name]
        compute = bool(service.selector) and (source == "computed" or (source == "auto" and bool(cluster.pods)))
        result[service.key] = reconcile_slices(service, cluster.pods) if compute else given
    return result


def proxy_endpoints(port: ServicePort, slices: Iterable[EndpointSlice], node: Optional[str] = None) -> List[Tuple[str, int]]:
    candidates: List[Tuple[Endpoint, int]] = []
    for endpoint_slice in slices:
        if endpoint_slice.address_type != "IPv4":
            continue
        match = next((p for p in endpoint_slice.ports if p.name == port.name and p.protocol == port.protocol), None)
        if match is None:
            continue
        for endpoint in endpoint_slice.endpoints:
            if node is None or endpoint.node_name == node:
                candidates.append((endpoint, match.port))
    chosen = [c for c in candidates if c[0].ready] or [c for c in candidates if c[0].serving and c[0].terminating]
    return sorted({(e.address, number) for e, number in chosen}, key=lambda item: (ip_key(item[0]), item[1]))


@dataclass
class PortPlan:
    service: str
    port_name: str
    protocol: str
    cluster_ip: str
    port: int
    node_port: Optional[int]
    endpoints: List[Tuple[str, int]]
    local_endpoints: List[Tuple[str, int]]
    external_policy: str = "Cluster"
    affinity_seconds: Optional[int] = None

    @property
    def id(self) -> str:
        return self.service + (f":{self.port_name}" if self.port_name else "")

    @property
    def proto(self) -> str:
        return self.protocol.lower()


def plan_ports(cluster: Cluster, slices: Dict[str, List[EndpointSlice]], node: Optional[str] = None) -> List[PortPlan]:
    plans = []
    for service in cluster.services:
        if service.virtual_ip is None:
            continue
        given = slices.get(service.key, [])
        for port in service.ports:
            plans.append(PortPlan(
                service.key,
                port.name,
                port.protocol,
                service.virtual_ip,
                port.port,
                port.node_port if service.type in ("NodePort", "LoadBalancer") else None,
                proxy_endpoints(port, given),
                proxy_endpoints(port, given, node) if node else [],
                service.external_traffic_policy,
                service.affinity_seconds if service.session_affinity == "ClientIP" else None,
            ))
    return plans


def iptables_probability(remaining: int) -> str:
    written = float(f"{1.0 / remaining:0.10f}")
    return f"{round(written * STATISTIC_SCALE) / STATISTIC_SCALE:.11f}"


def cascade(count: int) -> List[Optional[str]]:
    if count <= 0:
        return []
    return [iptables_probability(count - index) for index in range(count - 1)] + [None]


def shares(probabilities: Sequence[Optional[float]]) -> List[float]:
    left = 1.0
    result = []
    for probability in probabilities:
        take = left if probability is None else left * probability
        result.append(take)
        left -= take
    return result


def svc_suffix(plan: PortPlan) -> str:
    return short_hash(plan.id, plan.proto)


def sep_chain(plan: PortPlan, address: str, number: int) -> str:
    return "KUBE-SEP-" + short_hash(plan.id, plan.proto, f"{address}:{number}")


def comment(text: str) -> str:
    return f'-m comment --comment "{text}"'


def endpoint_jumps(plan: PortPlan, chain: str, endpoints: Sequence[Tuple[str, int]]) -> List[str]:
    rules = []
    seps = [(sep_chain(plan, address, number), address, number) for address, number in endpoints]
    if plan.affinity_seconds:
        for sep, address, number in seps:
            rules.append(f"-A {chain} {comment(f'{plan.id} -> {address}:{number}')} -m recent --name {sep} --rcheck --seconds {plan.affinity_seconds} --reap -j {sep}")
    for (sep, address, number), probability in zip(seps, cascade(len(seps))):
        statistic = f" -m statistic --mode random --probability {probability}" if probability else ""
        rules.append(f"-A {chain} {comment(f'{plan.id} -> {address}:{number}')}{statistic} -j {sep}")
    return rules


def sep_rules(plan: PortPlan, address: str, number: int) -> List[str]:
    sep = sep_chain(plan, address, number)
    recent = f" -m recent --set --name {sep} --mask 255.255.255.255 --rsource" if plan.affinity_seconds else ""
    return [
        f"-A {sep} -s {address}/32 {comment(plan.id)} -j KUBE-MARK-MASQ",
        f"-A {sep} -p {plan.proto} {comment(plan.id)}{recent} -m {plan.proto} -j DNAT --to-destination {address}:{number}",
    ]


def external_rules(plan: PortPlan, ext: str, svc: str, svl: str, cluster_cidr: Optional[str]) -> List[str]:
    if plan.external_policy != "Local":
        return [f"-A {ext} {comment(f'masquerade traffic for {plan.id} external destinations')} -j KUBE-MARK-MASQ", f"-A {ext} -j {svc}"]
    rules = []
    if cluster_cidr:
        rules.append(f"-A {ext} -s {cluster_cidr} {comment(f'pod traffic for {plan.id} external destinations')} -j {svc}")
    rules.append(f"-A {ext} {comment(f'masquerade LOCAL traffic for {plan.id} external destinations')} -m addrtype --src-type LOCAL -j KUBE-MARK-MASQ")
    rules.append(f"-A {ext} {comment(f'route LOCAL traffic for {plan.id} external destinations')} -m addrtype --src-type LOCAL -j {svc}")
    rules.append(f"-A {ext} -j {svl}")
    return rules


def emit_table(table: str, chains: Dict[str, List[str]]) -> str:
    builtin = BUILTIN_CHAINS[table]
    custom = sorted(name for name in chains if name not in builtin)
    lines = [f"*{table}"]
    lines += [f":{name} ACCEPT [0:0]" for name in builtin]
    lines += [f":{name} - [0:0]" for name in custom]
    for name in [*builtin, *custom]:
        lines += chains.get(name, [])
    lines.append("COMMIT")
    return "\n".join(lines) + "\n"


def render_iptables(plans: Sequence[PortPlan], cluster_cidr: Optional[str] = None, masquerade_all: bool = False) -> str:
    nat: Dict[str, List[str]] = {
        "PREROUTING": [f"-A PREROUTING {comment('kubernetes service portals')} -j KUBE-SERVICES"],
        "OUTPUT": [f"-A OUTPUT {comment('kubernetes service portals')} -j KUBE-SERVICES"],
        "POSTROUTING": [f"-A POSTROUTING {comment('kubernetes postrouting rules')} -j KUBE-POSTROUTING"],
        "KUBE-MARK-MASQ": [f"-A KUBE-MARK-MASQ -j MARK --set-xmark {MASQ_MARK}/{MASQ_MARK}"],
        "KUBE-POSTROUTING": [
            f"-A KUBE-POSTROUTING -m mark ! --mark {MASQ_MARK}/{MASQ_MARK} -j RETURN",
            f"-A KUBE-POSTROUTING -j MARK --set-xmark {MASQ_MARK}/0x0",
            f"-A KUBE-POSTROUTING {comment('kubernetes service traffic requiring SNAT')} -j MASQUERADE --random-fully",
        ],
        "KUBE-SERVICES": [],
        "KUBE-NODEPORTS": [],
    }
    filter_chains: Dict[str, List[str]] = {
        "INPUT": [f"-A INPUT -m conntrack --ctstate NEW {comment('kubernetes externally-visible service portals')} -j KUBE-EXTERNAL-SERVICES"],
        "FORWARD": [f"-A FORWARD -m conntrack --ctstate NEW {comment('kubernetes service portals')} -j KUBE-SERVICES"],
        "OUTPUT": [f"-A OUTPUT -m conntrack --ctstate NEW {comment('kubernetes service portals')} -j KUBE-SERVICES"],
        "KUBE-SERVICES": [],
        "KUBE-EXTERNAL-SERVICES": [],
    }
    for plan in plans:
        suffix = svc_suffix(plan)
        svc, ext, svl = f"KUBE-SVC-{suffix}", f"KUBE-EXT-{suffix}", f"KUBE-SVL-{suffix}"
        if not plan.endpoints:
            filter_chains["KUBE-SERVICES"].append(f"-A KUBE-SERVICES -d {plan.cluster_ip}/32 -p {plan.proto} {comment(f'{plan.id} has no endpoints')} -m {plan.proto} --dport {plan.port} -j REJECT --reject-with icmp-port-unreachable")
            if plan.node_port:
                filter_chains["KUBE-EXTERNAL-SERVICES"].append(f"-A KUBE-EXTERNAL-SERVICES -p {plan.proto} {comment(f'{plan.id} has no endpoints')} -m addrtype --dst-type LOCAL -m {plan.proto} --dport {plan.node_port} -j REJECT --reject-with icmp-port-unreachable")
            continue
        nat["KUBE-SERVICES"].append(f"-A KUBE-SERVICES -d {plan.cluster_ip}/32 -p {plan.proto} {comment(f'{plan.id} cluster IP')} -m {plan.proto} --dport {plan.port} -j {svc}")
        rules = []
        if masquerade_all or cluster_cidr:
            source = "" if masquerade_all else f"! -s {cluster_cidr} "
            rules.append(f"-A {svc} {source}-d {plan.cluster_ip}/32 -p {plan.proto} {comment(f'{plan.id} cluster IP')} -m {plan.proto} --dport {plan.port} -j KUBE-MARK-MASQ")
        nat[svc] = rules + endpoint_jumps(plan, svc, plan.endpoints)
        used = set(plan.endpoints)
        if plan.node_port:
            nat["KUBE-NODEPORTS"].append(f"-A KUBE-NODEPORTS -p {plan.proto} {comment(plan.id)} -m {plan.proto} --dport {plan.node_port} -j {ext}")
            nat[ext] = external_rules(plan, ext, svc, svl, cluster_cidr)
            if plan.external_policy == "Local":
                nat[svl] = endpoint_jumps(plan, svl, plan.local_endpoints)
                used |= set(plan.local_endpoints)
                if not plan.local_endpoints:
                    filter_chains["KUBE-EXTERNAL-SERVICES"].append(f"-A KUBE-EXTERNAL-SERVICES -p {plan.proto} {comment(f'{plan.id} has no local endpoints')} -m addrtype --dst-type LOCAL -m {plan.proto} --dport {plan.node_port} -j DROP")
        for address, number in sorted(used, key=lambda item: (ip_key(item[0]), item[1])):
            nat[sep_chain(plan, address, number)] = sep_rules(plan, address, number)
    nat["KUBE-SERVICES"].append(NODEPORTS_RULE)
    return emit_table("nat", nat) + emit_table("filter", filter_chains)


def nft_names(plan: PortPlan) -> Tuple[str, str]:
    namespace, _, name = plan.service.partition("/")
    return namespace, name


def nft_service_chain(plan: PortPlan) -> str:
    namespace, name = nft_names(plan)
    return f"service-{short_hash(plan.id, plan.proto, length=8)}-{namespace}/{name}/{plan.proto}/{plan.port_name}"


def nft_endpoint_chain(plan: PortPlan, address: str, number: int) -> str:
    namespace, name = nft_names(plan)
    return f"endpoint-{short_hash(plan.id, plan.proto, f'{address}:{number}', length=8)}-{namespace}/{name}/{plan.proto}/{plan.port_name}__{address}/{number}"


def nft_map(name: str, kind: str, elements: Sequence[str]) -> List[str]:
    lines = [f"\tmap {name} {{", f"\t\ttype {kind}"]
    if elements:
        lines.append("\t\telements = { " + ",\n\t\t\t     ".join(elements) + " }")
    return lines + ["\t}", ""]


def nft_chain(name: str, body: Sequence[str]) -> List[str]:
    return [f"\tchain {name} {{"] + [f"\t\t{line}" for line in body] + ["\t}", ""]


def render_nftables(plans: Sequence[PortPlan], cluster_cidr: Optional[str] = None) -> str:
    services: List[str] = []
    no_endpoints: List[str] = []
    chains: List[Tuple[str, List[str]]] = []
    for plan in plans:
        key = f"{plan.cluster_ip} . {plan.proto} . {plan.port}"
        if not plan.endpoints:
            no_endpoints.append(f"{key} : goto reject-chain")
            continue
        chain = nft_service_chain(plan)
        services.append(f"{key} : goto {chain}")
        body = []
        if cluster_cidr:
            body.append(f"ip daddr {plan.cluster_ip} {plan.proto} dport {plan.port} ip saddr != {cluster_cidr} jump mark-for-masquerade")
        targets = ", ".join(f"{slot} : goto {nft_endpoint_chain(plan, a, n)}" for slot, (a, n) in enumerate(plan.endpoints))
        body.append(f"numgen random mod {len(plan.endpoints)} vmap {{ {targets} }}")
        chains.append((chain, body))
        for address, number in plan.endpoints:
            chains.append((nft_endpoint_chain(plan, address, number), [f"ip saddr {address} jump mark-for-masquerade", f"meta l4proto {plan.proto} dnat to {address}:{number}"]))
    lines = ["table ip kube-proxy {"]
    lines += nft_map("service-ips", "ipv4_addr . inet_proto . inet_service : verdict", services)
    lines += nft_map("no-endpoint-services", "ipv4_addr . inet_proto . inet_service : verdict", no_endpoints)
    lines += nft_chain("filter-prerouting", ["type filter hook prerouting priority dstnat - 10; policy accept;", "ip daddr . meta l4proto . th dport vmap @no-endpoint-services"])
    lines += nft_chain("nat-prerouting", ["type nat hook prerouting priority dstnat; policy accept;", "jump services"])
    lines += nft_chain("nat-output", ["type nat hook output priority dstnat; policy accept;", "jump services"])
    lines += nft_chain("nat-postrouting", ["type nat hook postrouting priority srcnat; policy accept;", "jump masquerading"])
    lines += nft_chain("services", ["ip daddr . meta l4proto . th dport vmap @service-ips"])
    lines += nft_chain("mark-for-masquerade", [f"meta mark set meta mark | {MASQ_MARK}"])
    lines += nft_chain("masquerading", [f"meta mark & {MASQ_MARK} == 0 return", f"meta mark set meta mark ^ {MASQ_MARK}", "masquerade fully-random"])
    lines += nft_chain("reject-chain", ["reject"])
    for name, body in chains:
        lines += nft_chain(name, body)
    if lines[-1] == "":
        lines.pop()
    lines.append("}")
    return "\n".join(lines) + "\n"


@dataclass
class Rule:
    chain: str
    options: Dict[str, Tuple[str, bool]]
    text: str

    @property
    def jump(self) -> Optional[str]:
        return self.value("-j")

    def value(self, option: str) -> Optional[str]:
        item = self.options.get(option)
        return item[0] if item else None


def parse_rule(line: str, number: int) -> Rule:
    try:
        tokens = shlex.split(line)
    except ValueError as exc:
        raise InputError(f"line {number}: {exc}") from None
    if len(tokens) < 2:
        raise InputError(f"line {number}: -A without a chain name")
    options: Dict[str, Tuple[str, bool]] = {}
    negate = False
    index = 2
    while index < len(tokens):
        token = tokens[index]
        if token == "!":
            negate = True
            index += 1
            continue
        has_value = index + 1 < len(tokens) and tokens[index + 1] != "!" and not tokens[index + 1].startswith("-")
        options.setdefault(token, (tokens[index + 1] if has_value else "", negate))
        negate = False
        index += 2 if has_value else 1
    return Rule(tokens[1], options, line)


def parse_iptables_save(text: str) -> Dict[str, Dict[str, List[Rule]]]:
    tables: Dict[str, Dict[str, List[Rule]]] = {}
    table: Optional[Dict[str, List[Rule]]] = None
    for number, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("*"):
            table = tables.setdefault(line[1:], {})
        elif line == "COMMIT":
            table = None
        elif table is None:
            raise InputError(f"line {number}: {line[:40]!r} is outside a *table ... COMMIT block; pass iptables-save output")
        elif line.startswith(":"):
            table.setdefault(line[1:].split()[0], [])
        elif line.startswith("-A "):
            rule = parse_rule(line, number)
            table.setdefault(rule.chain, []).append(rule)
    return tables


@dataclass
class Packet:
    address: str
    port: int
    protocol: str = "tcp"
    node_local: bool = False

    def describe(self) -> str:
        return f"{self.address}:{self.port}/{self.protocol}" + (" (to a node address)" if self.node_local else "")


def in_network(address: str, network: str) -> bool:
    try:
        return ipaddress.ip_address(address) in ipaddress.ip_network(network, strict=False)
    except ValueError:
        return False


def matches(rule: Rule, packet: Packet) -> bool:
    for option in ("-d", "--destination"):
        if option in rule.options:
            value, negated = rule.options[option]
            if in_network(packet.address, value) == negated:
                return False
    if "-p" in rule.options:
        value, negated = rule.options["-p"]
        if (value.lower() == packet.protocol) == negated:
            return False
    if "--dport" in rule.options:
        value, negated = rule.options["--dport"]
        if (value == str(packet.port)) == negated:
            return False
    if "--dst-type" in rule.options:
        value, negated = rule.options["--dst-type"]
        hit = packet.node_local if value == "LOCAL" else True
        if hit == negated:
            return False
    return True


def source_conditioned(rule: Rule) -> bool:
    return "-s" in rule.options or "--source" in rule.options or "--src-type" in rule.options or "--rcheck" in rule.options


def masquerade_condition(rule: Rule) -> str:
    if "-s" in rule.options:
        value, negated = rule.options["-s"]
        return f"{rule.chain}: source {'not in' if negated else 'is'} {value}"
    if "--src-type" in rule.options:
        return f"{rule.chain}: source is an address of this node"
    return f"{rule.chain}: always"


@dataclass
class Hop:
    chain: str
    probability: Optional[str]
    share: float
    destination: Optional[str]


@dataclass
class Trace:
    packet: str
    mode: str
    path: List[str] = field(default_factory=list)
    service: Optional[str] = None
    endpoints: List[Hop] = field(default_factory=list)
    masquerade: List[str] = field(default_factory=list)
    affinity: bool = False
    rejected: Optional[str] = None

    @property
    def found(self) -> bool:
        return bool(self.endpoints)


def collect_endpoints(result: Trace, nat: Dict[str, List[Rule]], chain: str, packet: Packet) -> None:
    jumps: List[Tuple[str, Optional[str]]] = []
    for rule in nat.get(chain, []):
        if rule.jump == "KUBE-MARK-MASQ":
            if matches(rule, packet):
                result.masquerade.append(masquerade_condition(rule))
        elif (rule.jump or "").startswith("KUBE-SEP-"):
            if "--rcheck" in rule.options:
                result.affinity = True
                continue
            jumps.append((rule.jump or "", rule.value("--probability")))
    portions = shares([float(p) if p else None for _, p in jumps])
    for (sep, probability), share in zip(jumps, portions):
        destination = None
        for rule in nat.get(sep, []):
            if rule.jump == "KUBE-MARK-MASQ" and "-s" in rule.options:
                result.masquerade.append(masquerade_condition(rule) + " (hairpin: the endpoint calling its own Service)")
            elif rule.jump == "DNAT":
                destination = rule.value("--to-destination")
        result.endpoints.append(Hop(sep, probability, share, destination))


def trace_iptables(text: str, packet: Packet) -> Trace:
    tables = parse_iptables_save(text)
    nat = tables.get("nat") or {}
    result = Trace(packet.describe(), "iptables")
    filters = tables.get("filter") or {}
    for rule in filters.get("KUBE-SERVICES", []) + filters.get("KUBE-EXTERNAL-SERVICES", []):
        if rule.jump in ("REJECT", "DROP") and matches(rule, packet):
            result.rejected = f"{rule.jump} in filter/{rule.chain}: {rule.value('--comment') or rule.text}"
            return result
    if "KUBE-SERVICES" not in nat:
        raise InputError("no KUBE-SERVICES chain in the nat table: kube-proxy is not in iptables mode on this node, or this is not iptables-save -t nat output")
    chain = "KUBE-SERVICES"
    result.path.append(chain)
    for _ in range(12):
        rules = nat.get(chain, [])
        if any((r.jump or "").startswith("KUBE-SEP-") for r in rules):
            collect_endpoints(result, nat, chain, packet)
            return result
        following = None
        for rule in rules:
            if not matches(rule, packet):
                continue
            if rule.jump == "KUBE-MARK-MASQ":
                result.masquerade.append(masquerade_condition(rule))
                continue
            if source_conditioned(rule) or rule.jump not in nat:
                continue
            following = rule
            break
        if following is None:
            return result
        label = following.value("--comment")
        if label and (result.service is None or chain != "KUBE-SERVICES"):
            result.service = label
        chain = following.jump or ""
        result.path.append(chain)
    return result


def nft_blocks(text: str) -> Dict[str, str]:
    blocks: Dict[str, str] = {}
    for match in re.finditer(r"\b(chain|map|set)\s+(\"?[^\s{\"]+\"?)\s*\{", text):
        depth, index = 1, match.end()
        while index < len(text) and depth:
            if text[index] == "{":
                depth += 1
            elif text[index] == "}":
                depth -= 1
            index += 1
        blocks[f"{match.group(1)} {match.group(2).strip(chr(34))}"] = text[match.end():index - 1]
    return blocks


def nft_elements(body: str) -> List[str]:
    found = re.search(r"elements\s*=\s*\{(.*)\}", body, re.S)
    if not found:
        return []
    return [" ".join(item.split()) for item in found.group(1).split(",") if item.strip()]


def trace_nft(text: str, packet: Packet) -> Trace:
    blocks = nft_blocks(text)
    result = Trace(packet.describe(), "nftables")
    key = f"{packet.address} . {packet.protocol} . {packet.port}"
    for element in nft_elements(blocks.get("map no-endpoint-services", "")):
        if element.split(" : ")[0].strip() == key:
            result.rejected = f"no-endpoint-services: {element}"
            return result
    if "map service-ips" not in blocks:
        raise InputError("no service-ips map: this is not kube-proxy's table (nft list table ip kube-proxy)")
    chain = None
    for element in nft_elements(blocks["map service-ips"]):
        target, _, verdict = element.partition(" : ")
        if target.strip() == key:
            chain = verdict.split()[-1]
    if chain is None:
        return result
    result.path = ["services", chain]
    result.service = chain
    for line in blocks.get(f"chain {chain}", "").splitlines():
        line = line.strip()
        if line.endswith("jump mark-for-masquerade"):
            result.masquerade.append(f"{chain}: {line[: -len(' jump mark-for-masquerade')]}")
        found = re.search(r"numgen random mod (\d+) vmap \{(.*)\}", line)
        if not found:
            continue
        count = int(found.group(1))
        for slot, endpoint_chain in re.findall(r"(\d+)\s*:\s*goto\s+([^\s,}]+)", found.group(2)):
            dnat = re.search(r"dnat to (\S+)", blocks.get(f"chain {endpoint_chain}", ""))
            result.endpoints.append(Hop(endpoint_chain, f"numgen {slot} of mod {count}", 1.0 / count, dnat.group(1) if dnat else None))
    return result


def trace_ruleset(text: str, packet: Packet) -> Trace:
    if re.search(r"^\s*table\s+(ip|inet)\s", text, re.M):
        return trace_nft(text, packet)
    return trace_iptables(text, packet)


def pick(trace: Trace, rng: random.Random) -> Optional[str]:
    if not trace.endpoints:
        return None
    if trace.mode == "nftables":
        return trace.endpoints[rng.randrange(len(trace.endpoints))].destination
    for hop in trace.endpoints:
        if hop.probability is None or rng.random() < float(hop.probability):
            return hop.destination
    return None


def simulate(trace: Trace, count: int, rng: random.Random) -> Dict[str, int]:
    counter: Counter = Counter(pick(trace, rng) or "dropped" for _ in range(count))
    return dict(sorted(counter.items()))


@dataclass
class Record:
    name: str
    type: str
    value: str
    port: Optional[int] = None
    weight: Optional[int] = None

    def text(self) -> str:
        if self.type == "SRV":
            return f"{self.name}. SRV 0 {self.weight} {self.port} {self.value}."
        if self.type == "CNAME":
            return f"{self.name}. CNAME {self.value}."
        return f"{self.name}. {self.type} {self.value}"


def dashed(address: str) -> str:
    return address.replace(".", "-").replace(":", "-")


def dns_records(cluster: Cluster, slices: Dict[str, List[EndpointSlice]], zone: str = ZONE) -> Dict[str, List[Record]]:
    records: Dict[str, List[Record]] = {}

    def add(record: Record) -> None:
        bucket = records.setdefault(record.name.lower(), [])
        if record not in bucket:
            bucket.append(record)

    for service in cluster.services:
        fqdn = f"{service.name}.{service.namespace}.svc.{zone}"
        if service.type == "ExternalName":
            if service.external_name:
                add(Record(fqdn, "CNAME", service.external_name.rstrip(".")))
            continue
        if not service.headless:
            if service.virtual_ip:
                add(Record(fqdn, "A", service.virtual_ip))
                for port in service.ports:
                    if port.name:
                        add(Record(f"_{port.name}._{port.protocol.lower()}.{fqdn}", "SRV", fqdn, port.port, 100))
            continue
        ready = sorted(((e, s) for s in slices.get(service.key, []) for e in s.endpoints if e.ready), key=lambda pair: ip_key(pair[0].address))
        for endpoint, _ in ready:
            add(Record(fqdn, "A", endpoint.address))
        for endpoint, _ in ready:
            add(Record(f"{endpoint.hostname or dashed(endpoint.address)}.{fqdn}", "A", endpoint.address))
        for port in service.ports:
            if not port.name:
                continue
            targets = sorted({(f"{e.hostname or dashed(e.address)}.{fqdn}", p.port) for e, s in ready for p in s.ports if p.name == port.name and p.protocol == port.protocol})
            for target, number in targets:
                add(Record(f"_{port.name}._{port.protocol.lower()}.{fqdn}", "SRV", target, number, 100 // len(targets)))
    for pod in cluster.pods:
        if pod.ip:
            add(Record(f"{dashed(pod.ip)}.{pod.namespace}.pod.{zone}", "A", pod.ip))
    return records


def lookup(records: Dict[str, List[Record]], name: str, qtype: str = "A") -> List[Record]:
    found = records.get(name.rstrip(".").lower(), [])
    if qtype == "ANY":
        return list(found)
    exact = [r for r in found if r.type == qtype]
    return exact or [r for r in found if r.type == "CNAME"]


def search_domains(namespace: str, zone: str = ZONE, extra: Sequence[str] = ()) -> List[str]:
    return [f"{namespace}.svc.{zone}", f"svc.{zone}", zone, *extra]


def candidate_names(name: str, search: Sequence[str], ndots: int = 5, libc: str = "glibc") -> List[str]:
    if name.endswith("."):
        return [name[:-1]]
    expanded = [f"{name}.{domain}" for domain in search]
    if name.count(".") >= ndots:
        return [name] + ([] if libc == "musl" else expanded)
    return expanded + [name]


@dataclass
class Attempt:
    name: str
    rcode: str
    answer: List[str]


@dataclass
class Resolution:
    query: str
    qtype: str
    ndots: int
    search: List[str]
    attempts: List[Attempt]
    answer: List[str]

    @property
    def nxdomains(self) -> int:
        return sum(1 for attempt in self.attempts if attempt.rcode == "NXDOMAIN")


def resolve(name: str, records: Dict[str, List[Record]], search: Sequence[str], qtype: str = "A", ndots: int = 5, zone: str = ZONE, libc: str = "glibc", upstream: Optional[Callable[[str], bool]] = None) -> Resolution:
    typed = name.rstrip(".").lower()
    exists_upstream = upstream or (lambda candidate: candidate == typed and "." in candidate)
    attempts: List[Attempt] = []
    for candidate in candidate_names(name.lower(), search, ndots, libc):
        if candidate == zone or candidate.endswith("." + zone):
            hits = [r.text() for r in lookup(records, candidate, qtype)]
            attempts.append(Attempt(candidate, "NOERROR" if hits else "NXDOMAIN", hits))
        else:
            attempts.append(Attempt(candidate, "FORWARDED" if exists_upstream(candidate) else "NXDOMAIN", []))
        if attempts[-1].rcode != "NXDOMAIN":
            break
    final = attempts[-1] if attempts and attempts[-1].rcode != "NXDOMAIN" else None
    return Resolution(name, qtype, ndots, list(search), attempts, final.answer if final else [])


def render_resolv_conf(namespace: str, nameserver: str, zone: str = ZONE, ndots: int = 5, extra: Sequence[str] = ()) -> str:
    return f"search {' '.join(search_domains(namespace, zone, extra))}\nnameserver {nameserver}\noptions ndots:{ndots}\n"


def parse_resolv_conf(text: str) -> Dict[str, Any]:
    search: List[str] = []
    servers: List[str] = []
    ndots = 1
    for line in text.splitlines():
        parts = line.split("#", 1)[0].split()
        if not parts:
            continue
        if parts[0] in ("search", "domain"):
            search = parts[1:]
        elif parts[0] == "nameserver" and len(parts) > 1:
            servers.append(parts[1])
        elif parts[0] == "options":
            for option in parts[1:]:
                if option.startswith("ndots:") and option[6:].isdigit():
                    ndots = int(option[6:])
    return {"search": search, "nameservers": servers, "ndots": ndots}


def render_slices(slices: Dict[str, List[EndpointSlice]]) -> str:
    lines = []
    for key, items in slices.items():
        if not items:
            lines.append(f"{key}: no EndpointSlices (no selector and none supplied, or ExternalName)")
        for item in items:
            ports = ", ".join(f"{p.name or '<unnamed>'} {p.port}/{p.protocol}" for p in item.ports) or "<unset>"
            lines.append(f"{key}  slice {item.name}  ports {ports}  ({len(item.endpoints)} endpoint(s))")
            for e in item.endpoints:
                where = f" on {e.node_name}" if e.node_name else ""
                host = f" hostname {e.hostname}" if e.hostname else ""
                lines.append(f"  {e.address:<15} ready={str(e.ready).lower():<5} serving={str(e.serving).lower():<5} terminating={str(e.terminating).lower():<5} {e.pod or '-'}{where}{host}")
    return "\n".join(lines)


def render_trace(trace: Trace, simulation: Optional[Dict[str, int]] = None) -> str:
    head = f"{trace.packet} via {trace.mode}: {' -> '.join(trace.path) or '-'}"
    lines = [head + (f'  "{trace.service}"' if trace.service else "")]
    if trace.rejected:
        lines.append(f"  {trace.rejected}")
        lines.append("  clients get an immediate error (connection refused for REJECT); nothing is forwarded")
        return "\n".join(lines)
    if not trace.endpoints:
        lines.append("  no Service rule matches: not a ClusterIP or NodePort on this node, the port or protocol is wrong, or kube-proxy has not synced yet")
        return "\n".join(lines)
    for condition in trace.masquerade:
        lines.append(f"  SNAT mark when {condition}")
    for index, hop in enumerate(trace.endpoints, 1):
        lines.append(f"  {index}. {hop.chain}  {hop.probability or 'rest':<22} share {hop.share * 100:5.1f}%  -> DNAT {hop.destination or '?'}")
    if trace.affinity:
        lines.append("  session affinity ClientIP: -m recent rules ahead of the random ones send a known client back to its last endpoint")
    if simulation:
        total = sum(simulation.values())
        lines.append(f"  simulated {total} new connections: " + ", ".join(f"{d} {n} ({n / total * 100:.1f}%)" for d, n in simulation.items()))
    return "\n".join(lines)


def render_resolution(result: Resolution) -> str:
    lines = [f"{result.query} ({result.qtype}), ndots:{result.ndots}, search {' '.join(result.search) or '-'}"]
    for index, attempt in enumerate(result.attempts, 1):
        detail = "  forwarded upstream" if attempt.rcode == "FORWARDED" else ""
        lines.append(f"  {index}. {attempt.name:<52} {attempt.rcode}{detail}")
        lines += [f"       {record}" for record in attempt.answer]
    tried = len(result.attempts)
    lines.append(f"{tried} name(s) tried, {result.nxdomains} NXDOMAIN; {tried * 2} queries when the client asks for A and AAAA")
    return "\n".join(lines)


def parse_destination(text: str) -> Tuple[str, int]:
    address, _, port = text.rpartition(":")
    try:
        ipaddress.ip_address(address.strip("[]"))
        return address.strip("[]"), int(port)
    except ValueError:
        raise InputError(f"--to {text!r}: expected IP:PORT, for example 10.96.0.10:53") from None


def check_cidr(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    try:
        return str(ipaddress.ip_network(value, strict=False))
    except ValueError:
        raise InputError(f"--cluster-cidr {value!r} is not a CIDR such as 10.244.0.0/16") from None


def plan_json(plan: PortPlan) -> Dict[str, Any]:
    data = asdict(plan)
    data["id"] = plan.id
    data["chain"] = f"KUBE-SVC-{svc_suffix(plan)}"
    data["probabilities"] = cascade(len(plan.endpoints))
    return data


def read_text(path: str) -> str:
    try:
        with open(path, encoding="utf-8") as handle:
            return handle.read()
    except OSError as exc:
        raise InputError(f"{path}: {exc.strerror or exc}") from None


def load_cluster(args: argparse.Namespace) -> Tuple[Cluster, Dict[str, List[EndpointSlice]]]:
    cluster = cluster_from(load_objects(args.sources))
    return cluster, slices_for(cluster, args.slices)


def cmd_endpoints(args: argparse.Namespace) -> int:
    cluster, slices = load_cluster(args)
    if args.service:
        service = cluster.service(args.service)
        if service is None:
            print(f"miniproxy: no Service {args.service} in the input", file=sys.stderr)
            return 1
        slices = {service.key: slices[service.key]}
    if args.json:
        print(json.dumps({key: [asdict(s) for s in items] for key, items in slices.items()}, indent=2))
    else:
        print(render_slices(slices))
    return 0


def cmd_iptables(args: argparse.Namespace) -> int:
    cluster, slices = load_cluster(args)
    plans = plan_ports(cluster, slices, args.node)
    text = render_iptables(plans, check_cidr(args.cluster_cidr), args.masquerade_all)
    print(json.dumps({"services": [plan_json(p) for p in plans], "rules": text}, indent=2) if args.json else text, end="\n" if args.json else "")
    return 0


def cmd_nftables(args: argparse.Namespace) -> int:
    cluster, slices = load_cluster(args)
    plans = plan_ports(cluster, slices)
    text = render_nftables(plans, check_cidr(args.cluster_cidr))
    print(json.dumps({"services": [plan_json(p) for p in plans], "rules": text}, indent=2) if args.json else text, end="\n" if args.json else "")
    return 0


def cmd_dns(args: argparse.Namespace) -> int:
    cluster, slices = load_cluster(args)
    search = search_domains(args.namespace, args.zone, args.search)
    ndots = args.ndots
    if args.resolv_conf:
        conf = parse_resolv_conf(read_text(args.resolv_conf))
        search, ndots = conf["search"], conf["ndots"]
    result = resolve(args.name, dns_records(cluster, slices, args.zone), search, args.type, ndots, args.zone, args.libc)
    if args.json:
        data = asdict(result)
        data["nxdomains"] = result.nxdomains
        print(json.dumps(data, indent=2))
    else:
        print(render_resolution(result))
    return 0 if result.attempts and result.attempts[-1].rcode != "NXDOMAIN" else 1


def cmd_resolv_conf(args: argparse.Namespace) -> int:
    text = render_resolv_conf(args.namespace, args.nameserver, args.zone, args.ndots, args.search)
    print(json.dumps(parse_resolv_conf(text), indent=2) if args.json else text, end="\n" if args.json else "")
    return 0


def cmd_trace(args: argparse.Namespace) -> int:
    address, port = parse_destination(args.to)
    trace = trace_ruleset(read_text(args.ruleset), Packet(address, port, args.proto, args.node_local))
    simulation = simulate(trace, args.simulate, random.Random(args.seed)) if args.simulate and trace.found else None
    if args.json:
        data = asdict(trace)
        data["found"] = trace.found
        data["simulation"] = simulation
        print(json.dumps(data, indent=2))
    else:
        print(render_trace(trace, simulation))
    return 0 if trace.found else 1


def build_parser() -> argparse.ArgumentParser:
    objects = argparse.ArgumentParser(add_help=False)
    objects.add_argument("--from", dest="sources", action="append", required=True, metavar="FILE", help="kubectl get svc,pods,endpointslices -o json output (repeatable)")
    objects.add_argument("--slices", choices=("auto", "computed", "input"), default="auto", help="computed: run the EndpointSlice controller over the pods; input: use the slices in the file; auto: computed when pods are present")
    output = argparse.ArgumentParser(add_help=False)
    output.add_argument("--json", action="store_true", help="machine-readable output")
    parser = argparse.ArgumentParser(prog="miniproxy", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    endpoints = sub.add_parser("endpoints", parents=[objects, output], help="the EndpointSlices the controller writes for each Service")
    endpoints.add_argument("service", nargs="?", help="NAMESPACE/NAME or NAME")
    endpoints.set_defaults(run=cmd_endpoints)
    iptables = sub.add_parser("iptables", parents=[objects, output], help="kube-proxy iptables-mode rules in iptables-save format")
    iptables.add_argument("--cluster-cidr", help="pod CIDR; traffic from outside it is marked for SNAT")
    iptables.add_argument("--masquerade-all", action="store_true", help="mark every packet to a ClusterIP for SNAT")
    iptables.add_argument("--node", help="this node's name, for externalTrafficPolicy: Local")
    iptables.set_defaults(run=cmd_iptables)
    nftables = sub.add_parser("nftables", parents=[objects, output], help="the same Services in kube-proxy's nftables shape")
    nftables.add_argument("--cluster-cidr", help="pod CIDR; traffic from outside it is marked for SNAT")
    nftables.set_defaults(run=cmd_nftables)
    dns = sub.add_parser("dns", parents=[objects, output], help="resolve a name the way a pod's stub resolver and CoreDNS would")
    dns.add_argument("name")
    dns.add_argument("--type", default="A", choices=("A", "SRV", "CNAME", "ANY"))
    dns.add_argument("--namespace", default="default", help="namespace of the asking pod")
    dns.add_argument("--ndots", type=int, default=5)
    dns.add_argument("--search", action="append", default=[], metavar="DOMAIN", help="extra search domain inherited from the node")
    dns.add_argument("--resolv-conf", metavar="FILE", help="a pod's /etc/resolv.conf; its search list and ndots replace --namespace, --search and --ndots")
    dns.add_argument("--libc", choices=("glibc", "musl"), default="glibc")
    dns.add_argument("--zone", default=ZONE)
    dns.set_defaults(run=cmd_dns)
    conf = sub.add_parser("resolv-conf", parents=[output], help="the /etc/resolv.conf the kubelet writes for dnsPolicy ClusterFirst")
    conf.add_argument("--namespace", default="default")
    conf.add_argument("--nameserver", default="10.96.0.10", help="the kube-dns Service ClusterIP")
    conf.add_argument("--ndots", type=int, default=5)
    conf.add_argument("--search", action="append", default=[], metavar="DOMAIN")
    conf.add_argument("--zone", default=ZONE)
    conf.set_defaults(run=cmd_resolv_conf)
    trace = sub.add_parser("trace", parents=[output], help="walk a real node's iptables-save or nft ruleset for one Service address")
    trace.add_argument("--ruleset", required=True, metavar="FILE", help="iptables-save -t nat output, or nft list table ip kube-proxy")
    trace.add_argument("--to", required=True, metavar="IP:PORT")
    trace.add_argument("--proto", default="tcp", choices=("tcp", "udp", "sctp"))
    trace.add_argument("--node-local", action="store_true", help="the address is one of the node's own (a NodePort)")
    trace.add_argument("--simulate", type=int, default=0, metavar="N", help="pick an endpoint for N new connections")
    trace.add_argument("--seed", type=int, default=0)
    trace.set_defaults(run=cmd_trace)
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(list(sys.argv[1:] if argv is None else argv))
    try:
        return args.run(args)
    except InputError as exc:
        print(f"miniproxy: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
