"""minigateway: a Gateway API router and reverse proxy. It attaches HTTPRoutes to Gateway listeners, writes route status, orders matches by the spec's precedence and forwards requests through filters and weighted backends."""
from __future__ import annotations

import argparse
import json
import os
import random
import re
import sys
import threading
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, List, Optional, Sequence, Tuple

GROUP = "gateway.networking.k8s.io"
CONTROLLER = "cloudcaptain.io/minigateway"
PATH_TYPES = ("Exact", "PathPrefix", "RegularExpression")
VALUE_TYPES = ("Exact", "RegularExpression")
ROUTE_KINDS_BY_PROTOCOL = {"HTTP": ["HTTPRoute"], "HTTPS": ["HTTPRoute"], "TLS": ["TLSRoute"], "TCP": ["TCPRoute"], "UDP": ["UDPRoute"]}
FILTER_FIELDS = {"RequestHeaderModifier": "requestHeaderModifier", "RequestRedirect": "requestRedirect", "URLRewrite": "urlRewrite"}
WELL_KNOWN_PORTS = {"http": 80, "https": 443}
HOP_BY_HOP = {"connection", "keep-alive", "proxy-connection", "transfer-encoding", "te", "trailer", "upgrade", "content-length", "host"}
CRITERIA = (
    "exact hostname",
    "longer hostname",
    "exact path",
    "longer path",
    "method match",
    "more header matches",
    "more query param matches",
    "older route",
    "route namespace/name",
    "rule order",
    "match order",
)
DEFAULT_PORT_OFFSET = 8000


class InputError(Exception):
    pass


@dataclass
class Listener:
    name: str
    port: int
    protocol: str
    hostname: Optional[str]
    allowed_from: str
    allowed_labels: Dict[str, str]
    allowed_kinds: List[str]


@dataclass
class Gateway:
    namespace: str
    name: str
    class_name: str
    listeners: List[Listener]

    @property
    def key(self) -> str:
        return f"{self.namespace}/{self.name}"


@dataclass
class ValueMatch:
    kind: str
    name: str
    value: str


@dataclass
class Match:
    path_type: str = "PathPrefix"
    path: str = "/"
    headers: List[ValueMatch] = field(default_factory=list)
    query: List[ValueMatch] = field(default_factory=list)
    method: Optional[str] = None


@dataclass
class BackendRef:
    name: str
    namespace: str
    port: Optional[int]
    weight: int = 1
    group: str = ""
    kind: str = "Service"

    @property
    def key(self) -> str:
        return f"{self.namespace}/{self.name}"


@dataclass
class Rule:
    index: int
    matches: List[Match]
    filters: List[dict]
    backends: List[BackendRef]


@dataclass
class ParentRef:
    name: str
    namespace: str
    section: Optional[str] = None
    port: Optional[int] = None
    group: str = GROUP
    kind: str = "Gateway"

    def to_json(self) -> dict:
        ref = {"group": self.group, "kind": self.kind, "namespace": self.namespace, "name": self.name}
        if self.section:
            ref["sectionName"] = self.section
        if self.port:
            ref["port"] = self.port
        return ref


@dataclass
class Route:
    namespace: str
    name: str
    created: str
    hostnames: List[str]
    parents: List[ParentRef]
    rules: List[Rule]
    generation: int = 1

    @property
    def key(self) -> str:
        return f"{self.namespace}/{self.name}"


@dataclass
class Grant:
    namespace: str
    sources: List[Tuple[str, str, str]]
    targets: List[Tuple[str, str, Optional[str]]]


@dataclass
class World:
    gateways: List[Gateway] = field(default_factory=list)
    routes: List[Route] = field(default_factory=list)
    grants: List[Grant] = field(default_factory=list)
    services: Dict[str, List[int]] = field(default_factory=dict)
    namespaces: Dict[str, Dict[str, str]] = field(default_factory=dict)
    warnings: List[str] = field(default_factory=list)


@dataclass
class Condition:
    type: str
    status: str
    reason: str
    message: str
    generation: int = 1

    def to_json(self) -> dict:
        return {"type": self.type, "status": self.status, "reason": self.reason, "message": self.message, "observedGeneration": self.generation}


@dataclass
class ParentStatus:
    parent: ParentRef
    conditions: List[Condition]


@dataclass
class Attachment:
    route: Route
    gateway: Gateway
    listener: Listener
    hostnames: List[str]


@dataclass
class Candidate:
    attachment: Attachment
    rule: Rule
    match_index: int
    match: Match


@dataclass
class Request:
    method: str
    host: str
    authority: str
    path: str
    query: str
    headers: Dict[str, str]
    port: int


@dataclass
class Split:
    backend: BackendRef
    percent: float
    problem: Optional[str]


@dataclass
class Decision:
    status: Optional[int]
    action: str
    reason: str
    winner: Optional[Candidate] = None
    ranked: List[Candidate] = field(default_factory=list)
    why: str = ""
    location: Optional[str] = None
    headers: Dict[str, str] = field(default_factory=dict)
    path: str = "/"
    query: str = ""
    host: str = ""
    splits: List[Split] = field(default_factory=list)
    backend: Optional[BackendRef] = None


def flatten(data: Any) -> List[dict]:
    if isinstance(data, list):
        return [item for entry in data for item in flatten(entry)]
    if isinstance(data, dict) and isinstance(data.get("items"), list):
        return flatten(data["items"])
    if isinstance(data, dict) and data.get("kind"):
        return [data]
    return []


def read_documents(paths: Sequence[str]) -> List[dict]:
    items: List[dict] = []
    for path in paths:
        names = sorted(os.path.join(path, n) for n in os.listdir(path) if n.endswith(".json")) if os.path.isdir(path) else [path]
        for name in names:
            try:
                with open(name, encoding="utf-8") as handle:
                    data = json.load(handle)
            except OSError as exc:
                raise InputError(f"{name}: {exc.strerror}") from None
            except json.JSONDecodeError as exc:
                raise InputError(f"{name}:{exc.lineno}: invalid JSON: {exc.msg}") from None
            items.extend(flatten(data))
    return items


def identity(obj: dict) -> Tuple[str, str]:
    meta = obj.get("metadata") or {}
    return meta.get("namespace") or "default", meta.get("name") or ""


def parse_listener(raw: dict, where: str) -> Listener:
    allowed = raw.get("allowedRoutes") or {}
    namespaces = allowed.get("namespaces") or {}
    source = namespaces.get("from") or "Same"
    if source not in ("Same", "All", "Selector"):
        raise InputError(f"{where}.allowedRoutes.namespaces.from: {source!r} is not Same, All or Selector")
    selector = namespaces.get("selector") or {}
    if selector.get("matchExpressions"):
        raise InputError(f"{where}.allowedRoutes.namespaces.selector: matchExpressions are not supported by minigateway; use matchLabels")
    port = raw.get("port")
    if not isinstance(port, int) or not 1 <= port <= 65535:
        raise InputError(f"{where}.port: must be an integer from 1 to 65535")
    protocol = raw.get("protocol") or "HTTP"
    kinds = [k.get("kind") for k in allowed.get("kinds") or [] if k.get("kind")]
    hostname = (raw.get("hostname") or "").lower() or None
    return Listener(raw.get("name") or "", port, protocol, hostname, source, dict(selector.get("matchLabels") or {}), kinds or list(ROUTE_KINDS_BY_PROTOCOL.get(protocol, [])))


def parse_value_matches(raw_list: Optional[list], where: str, fold: bool) -> List[ValueMatch]:
    seen = set()
    result: List[ValueMatch] = []
    for index, raw in enumerate(raw_list or []):
        kind = raw.get("type") or "Exact"
        if kind not in VALUE_TYPES:
            raise InputError(f"{where}[{index}].type: {kind!r} is not Exact or RegularExpression")
        name = (raw.get("name") or "").lower() if fold else raw.get("name") or ""
        if name in seen:
            continue
        seen.add(name)
        result.append(ValueMatch(kind, name, str(raw.get("value", ""))))
    return result


def parse_match(raw: dict, where: str) -> Match:
    path = raw.get("path") or {}
    path_type = path.get("type") or "PathPrefix"
    value = path.get("value") if path.get("value") is not None else "/"
    if path_type not in PATH_TYPES:
        raise InputError(f"{where}.path.type: {path_type!r} is not Exact, PathPrefix or RegularExpression")
    if path_type != "RegularExpression" and not value.startswith("/"):
        raise InputError(f'{where}.path.value: must be an absolute path starting with "/", got {value!r}')
    headers = parse_value_matches(raw.get("headers"), f"{where}.headers", True)
    query = parse_value_matches(raw.get("queryParams"), f"{where}.queryParams", False)
    return Match(path_type, value, headers, query, (raw.get("method") or "").upper() or None)


def parse_backend(raw: dict, namespace: str, where: str) -> BackendRef:
    kind = raw.get("kind") or "Service"
    group = raw.get("group") or ""
    port = raw.get("port")
    if kind == "Service" and group == "" and port is None:
        raise InputError(f"{where}: port is required when the backendRef is a Service")
    weight = raw.get("weight", 1)
    if not isinstance(weight, int) or not 0 <= weight <= 1000000:
        raise InputError(f"{where}.weight: must be an integer from 0 to 1000000")
    return BackendRef(raw.get("name") or "", raw.get("namespace") or namespace, port, weight, group, kind)


def filter_spec(item: dict) -> dict:
    return item.get(FILTER_FIELDS.get(item.get("type") or "", ""), {}) or {}


def parse_rule(raw: dict, index: int, namespace: str, where: str, warnings: List[str]) -> Rule:
    matches = [parse_match(m, f"{where}.matches[{i}]") for i, m in enumerate(raw.get("matches") or [{}])]
    filters = list(raw.get("filters") or [])
    kinds = [item.get("type") for item in filters]
    backends = [parse_backend(b, namespace, f"{where}.backendRefs[{i}]") for i, b in enumerate(raw.get("backendRefs") or [])]
    if "RequestRedirect" in kinds and "URLRewrite" in kinds:
        raise InputError(f"{where}: may specify either RequestRedirect or URLRewrite, but not both")
    if "RequestRedirect" in kinds and backends:
        raise InputError(f"{where}: RequestRedirect filter must not be used together with backendRefs")
    for item in filters:
        if item.get("type") not in FILTER_FIELDS:
            warnings.append(f"{where}: filter {item.get('type')} is not implemented by minigateway and is ignored")
            continue
        if (filter_spec(item).get("path") or {}).get("type") == "ReplacePrefixMatch":
            if not (len(matches) == 1 and matches[0].path_type == "PathPrefix"):
                raise InputError(f"{where}: {item['type']} with path.replacePrefixMatch needs exactly one PathPrefix match")
    return Rule(index, matches, filters, backends)


def parse_parent(raw: dict, namespace: str) -> ParentRef:
    return ParentRef(raw.get("name") or "", raw.get("namespace") or namespace, raw.get("sectionName") or None, raw.get("port"), raw.get("group") or GROUP, raw.get("kind") or "Gateway")


def build_world(items: Sequence[dict]) -> World:
    world = World()
    for obj in items:
        kind = obj.get("kind")
        api = obj.get("apiVersion") or ""
        namespace, name = identity(obj)
        spec = obj.get("spec") or {}
        where = f"{kind} {namespace}/{name}"
        if kind == "Gateway" and api.startswith(GROUP):
            listeners = [parse_listener(raw, f"{where}: spec.listeners[{i}]") for i, raw in enumerate(spec.get("listeners") or [])]
            world.gateways.append(Gateway(namespace, name, spec.get("gatewayClassName") or "", listeners))
        elif kind == "HTTPRoute" and api.startswith(GROUP):
            meta = obj.get("metadata") or {}
            rules = [parse_rule(raw, i, namespace, f"{where}: spec.rules[{i}]", world.warnings) for i, raw in enumerate(spec.get("rules") or [{}])]
            parents = [parse_parent(raw, namespace) for raw in spec.get("parentRefs") or []]
            hostnames = [h.lower() for h in spec.get("hostnames") or []]
            world.routes.append(Route(namespace, name, meta.get("creationTimestamp") or "", hostnames, parents, rules, int(meta.get("generation") or 1)))
        elif kind == "ReferenceGrant" and api.startswith(GROUP):
            sources = [(f.get("group") or "", f.get("kind") or "", f.get("namespace") or "") for f in spec.get("from") or []]
            targets = [(t.get("group") or "", t.get("kind") or "", t.get("name") or None) for t in spec.get("to") or []]
            world.grants.append(Grant(namespace, sources, targets))
        elif kind == "Service" and api == "v1":
            world.services[f"{namespace}/{name}"] = [p.get("port") for p in spec.get("ports") or []]
        elif kind == "Namespace" and api == "v1":
            labels = dict((obj.get("metadata") or {}).get("labels") or {})
            labels["kubernetes.io/metadata.name"] = name
            world.namespaces[name] = labels
    return world


def hostname_matches(pattern: str, host: str) -> bool:
    if pattern.startswith("*."):
        return host.endswith(pattern[1:]) and len(host) > len(pattern) - 1
    return pattern == host


def intersect(listener: Optional[str], route: str) -> Optional[str]:
    if listener is None or listener == route:
        return route
    route_wild, listener_wild = route.startswith("*."), listener.startswith("*.")
    if listener_wild and not route_wild:
        return route if hostname_matches(listener, route) else None
    if route_wild and not listener_wild:
        return listener if hostname_matches(route, listener) else None
    if route_wild and listener_wild:
        if route.endswith(listener[1:]):
            return route
        if listener.endswith(route[1:]):
            return listener
    return None


def effective_hostnames(listener: Listener, route: Route) -> Optional[List[str]]:
    if not route.hostnames:
        return [listener.hostname] if listener.hostname else []
    found: List[str] = []
    for hostname in route.hostnames:
        common = intersect(listener.hostname, hostname)
        if common and common not in found:
            found.append(common)
    return found or None


def reference_granted(grants: Sequence[Grant], route: Route, ref: BackendRef) -> bool:
    for grant in grants:
        if grant.namespace != ref.namespace:
            continue
        if not any(group == GROUP and kind == "HTTPRoute" and namespace == route.namespace for group, kind, namespace in grant.sources):
            continue
        if any(group == ref.group and kind == ref.kind and name in (None, ref.name) for group, kind, name in grant.targets):
            return True
    return False


def ref_problem(route: Route, ref: BackendRef, world: World) -> Optional[Tuple[str, str]]:
    if ref.kind != "Service" or ref.group != "":
        return "InvalidKind", f"backendRef {ref.group or 'core'}/{ref.kind} {ref.name}: only core Services are supported"
    if ref.namespace != route.namespace and not reference_granted(world.grants, route, ref):
        return "RefNotPermitted", f"backendRef {ref.key} is in another namespace and no ReferenceGrant in {ref.namespace} allows HTTPRoutes from {route.namespace}"
    if ref.key not in world.services:
        return "BackendNotFound", f"service {ref.key} not found"
    return None


def resolve_refs(route: Route, world: World) -> Condition:
    for rule in route.rules:
        for ref in rule.backends:
            problem = ref_problem(route, ref, world)
            if problem:
                return Condition("ResolvedRefs", "False", problem[0], problem[1], route.generation)
    return Condition("ResolvedRefs", "True", "ResolvedRefs", "all backendRefs resolved", route.generation)


def namespace_allowed(listener: Listener, gateway: Gateway, route: Route, world: World) -> bool:
    if listener.allowed_from == "All":
        return True
    if listener.allowed_from == "Same":
        return route.namespace == gateway.namespace
    labels = world.namespaces.get(route.namespace, {"kubernetes.io/metadata.name": route.namespace})
    return all(labels.get(key) == value for key, value in listener.allowed_labels.items())


def accept(route: Route, parent: ParentRef, gateway: Gateway, world: World) -> Tuple[Condition, List[Attachment]]:
    def accepted(ok: bool, reason: str, message: str) -> Condition:
        return Condition("Accepted", "True" if ok else "False", reason, message, route.generation)

    named = [l for l in gateway.listeners if parent.section in (None, l.name) and parent.port in (None, l.port)]
    if not named:
        return accepted(False, "NoMatchingParent", f"no listener on {gateway.key} has sectionName {parent.section!r} and port {parent.port}"), []
    allowed = [l for l in named if namespace_allowed(l, gateway, route, world) and "HTTPRoute" in l.allowed_kinds]
    if not allowed:
        return accepted(False, "NotAllowedByListeners", f"listener(s) {', '.join(l.name for l in named)} on {gateway.key} do not allow HTTPRoutes from namespace {route.namespace}"), []
    landed: List[Attachment] = []
    for listener in allowed:
        hostnames = effective_hostnames(listener, route)
        if hostnames is not None:
            landed.append(Attachment(route, gateway, listener, hostnames))
    if not landed:
        listeners = ", ".join(f"{l.name}={l.hostname or '*'}" for l in allowed)
        return accepted(False, "NoMatchingListenerHostname", f"no listener hostname intersects {', '.join(route.hostnames)} (listeners: {listeners})"), []
    return accepted(True, "Accepted", f"attached to listener(s) {', '.join(a.listener.name for a in landed)}"), landed


def attach(world: World) -> Tuple[List[Attachment], Dict[str, List[ParentStatus]], List[str]]:
    gateways = {g.key: g for g in world.gateways}
    attachments: List[Attachment] = []
    statuses: Dict[str, List[ParentStatus]] = {}
    problems: List[str] = list(world.warnings)
    for route in world.routes:
        refs = resolve_refs(route, world)
        statuses[route.key] = []
        for parent in route.parents:
            gateway = gateways.get(f"{parent.namespace}/{parent.name}") if parent.kind == "Gateway" and parent.group == GROUP else None
            if gateway is None:
                problems.append(f"HTTPRoute {route.key}: parentRef {parent.kind} {parent.namespace}/{parent.name} not found; no controller writes status for it")
                continue
            condition, landed = accept(route, parent, gateway, world)
            attachments.extend(landed)
            statuses[route.key].append(ParentStatus(parent, [condition, refs]))
        if not route.parents:
            problems.append(f"HTTPRoute {route.key}: no parentRefs; it is not attached to anything")
    return attachments, statuses, problems


def path_matches(kind: str, value: str, path: str) -> bool:
    if kind == "Exact":
        return path == value
    if kind == "PathPrefix":
        prefix = value.rstrip("/")
        return not prefix or path == prefix or path.startswith(prefix + "/")
    return re.fullmatch(value, path) is not None


def value_ok(match: ValueMatch, value: str) -> bool:
    if match.kind == "Exact":
        return value == match.value
    return re.fullmatch(match.value, value) is not None


def request_matches(match: Match, request: Request) -> bool:
    if not path_matches(match.path_type, match.path, request.path):
        return False
    if match.method and match.method != request.method:
        return False
    for header in match.headers:
        value = request.headers.get(header.name)
        if value is None or not value_ok(header, value):
            return False
    if match.query:
        params: Dict[str, str] = {}
        for name, value in urllib.parse.parse_qsl(request.query, keep_blank_values=True):
            params.setdefault(name, value)
        for param in match.query:
            value = params.get(param.name)
            if value is None or not value_ok(param, value):
                return False
    return True


def host_allowed(hostnames: Sequence[str], host: str) -> bool:
    return not hostnames or any(hostname_matches(name, host) for name in hostnames)


def hostname_rank(hostnames: Sequence[str], host: Optional[str]) -> Tuple[int, int]:
    best = (0, 0)
    for name in hostnames:
        if host is not None and not hostname_matches(name, host):
            continue
        best = max(best, (0, len(name)) if name.startswith("*.") else (len(name), len(name)))
    return best


def precedence_key(candidate: Candidate, host: Optional[str]) -> tuple:
    match = candidate.match
    route = candidate.attachment.route
    exact_chars, any_chars = hostname_rank(candidate.attachment.hostnames, host)
    path_rank = {"Exact": 2, "PathPrefix": 1}.get(match.path_type, 0)
    path_chars = len(match.path.rstrip("/")) if match.path_type == "PathPrefix" else len(match.path)
    return (
        -exact_chars,
        -any_chars,
        -path_rank,
        -path_chars,
        0 if match.method else 1,
        -len(match.headers),
        -len(match.query),
        route.created,
        route.key,
        candidate.rule.index,
        candidate.match_index,
    )


def why_first(first: Candidate, second: Candidate, host: Optional[str]) -> str:
    for index, (a, b) in enumerate(zip(precedence_key(first, host), precedence_key(second, host))):
        if a != b:
            return CRITERIA[index]
    return "identical"


def replace_prefix(path: str, prefix: str, replacement: str) -> str:
    rest = path[len(prefix.rstrip("/")):]
    return (replacement.rstrip("/") + rest) or "/"


def rewrite_path(spec: Optional[dict], match: Match, path: str) -> str:
    if not spec:
        return path
    if spec.get("type") == "ReplaceFullPath":
        return spec.get("replaceFullPath") or "/"
    if spec.get("type") == "ReplacePrefixMatch":
        return replace_prefix(path, match.path, spec.get("replacePrefixMatch") or "")
    return path


def redirect_location(spec: dict, request: Request, candidate: Candidate) -> str:
    listener = candidate.attachment.listener
    scheme = spec.get("scheme") or ("https" if listener.protocol == "HTTPS" else "http")
    hostname = spec.get("hostname") or request.host
    path = rewrite_path(spec.get("path"), candidate.match, request.path)
    port = spec.get("port")
    if port is None:
        port = WELL_KNOWN_PORTS.get(scheme, listener.port) if spec.get("scheme") else listener.port
    authority = hostname if WELL_KNOWN_PORTS.get(scheme) == port else f"{hostname}:{port}"
    return f"{scheme}://{authority}{path}" + (f"?{request.query}" if request.query else "")


def modify_headers(headers: Dict[str, str], spec: dict) -> Dict[str, str]:
    result = dict(headers)
    for item in spec.get("set") or []:
        result[item["name"].lower()] = item["value"]
    for item in spec.get("add") or []:
        name = item["name"].lower()
        result[name] = f"{result[name]},{item['value']}" if name in result else item["value"]
    for name in spec.get("remove") or []:
        result.pop(name.lower(), None)
    return result


def weight_splits(candidate: Candidate, world: World) -> List[Split]:
    backends = candidate.rule.backends
    total = sum(b.weight for b in backends)
    splits: List[Split] = []
    for backend in backends:
        problem = ref_problem(candidate.attachment.route, backend, world)
        splits.append(Split(backend, 100.0 * backend.weight / total if total else 0.0, problem[0] if problem else None))
    return splits


def pick_backend(backends: Sequence[BackendRef], rng: random.Random) -> Optional[BackendRef]:
    total = sum(b.weight for b in backends)
    if total <= 0:
        return None
    point = rng.randrange(total)
    for backend in backends:
        if point < backend.weight:
            return backend
        point -= backend.weight
    return None


def make_request(method: str, authority: str, target: str, headers: Dict[str, str], port: int) -> Request:
    parts = urllib.parse.urlsplit(target)
    host = authority.split("]")[0].lstrip("[") if authority.startswith("[") else authority.split(":")[0]
    return Request(method.upper(), host.lower(), authority, parts.path or "/", parts.query, {k.lower(): v for k, v in headers.items()}, port)


def decide(candidates: Sequence[Candidate], request: Request, world: World, rng: Optional[random.Random] = None) -> Decision:
    matching = [c for c in candidates if host_allowed(c.attachment.hostnames, request.host) and request_matches(c.match, request)]
    matching.sort(key=lambda c: precedence_key(c, request.host))
    if not matching:
        return Decision(404, "respond", f"no rule matches {request.method} {request.host}{request.path}")
    winner = matching[0]
    why = why_first(winner, matching[1], request.host) if len(matching) > 1 else "only match"
    headers, path, host = dict(request.headers), request.path, request.authority
    for item in winner.rule.filters:
        kind = item.get("type")
        if kind == "RequestRedirect":
            spec = filter_spec(item)
            location = redirect_location(spec, request, winner)
            return Decision(int(spec.get("statusCode") or 302), "redirect", f"RequestRedirect to {location}", winner, matching, why, location=location)
        if kind == "RequestHeaderModifier":
            headers = modify_headers(headers, filter_spec(item))
        elif kind == "URLRewrite":
            spec = filter_spec(item)
            host = spec.get("hostname") or host
            path = rewrite_path(spec.get("path"), winner.match, path)
    splits = weight_splits(winner, world)
    decision = Decision(None, "forward", "forward", winner, matching, why, headers=headers, path=path, query=request.query, host=host, splits=splits)
    if not any(s.percent > 0 for s in splits):
        decision.status, decision.action, decision.reason = 500, "respond", "the rule has no backendRefs with a weight above zero"
        return decision
    if rng is not None:
        decision.backend = pick_backend(winner.rule.backends, rng)
        problem = ref_problem(winner.attachment.route, decision.backend, world) if decision.backend else None
        if problem:
            decision.status, decision.action, decision.reason = 500, "respond", f"{problem[0]}: {problem[1]}"
    return decision


class Router:
    def __init__(self, world: World, gateway: Optional[str] = None):
        self.world = world
        self.attachments, self.statuses, self.problems = attach(world)
        self.gateway = pick_gateway(world, gateway)

    def ports(self) -> List[int]:
        return sorted({l.port for l in self.gateway.listeners})

    def default_port(self) -> int:
        ports = self.ports()
        if len(ports) != 1:
            raise InputError(f"gateway {self.gateway.key} listens on ports {ports}; pass --port")
        return ports[0]

    def candidates(self, port: int) -> List[Candidate]:
        found: List[Candidate] = []
        for attachment in self.attachments:
            if attachment.gateway.key != self.gateway.key or attachment.listener.port != port:
                continue
            for rule in attachment.route.rules:
                for index, match in enumerate(rule.matches):
                    found.append(Candidate(attachment, rule, index, match))
        return found

    def decide(self, request: Request, rng: Optional[random.Random] = None) -> Decision:
        return decide(self.candidates(request.port), request, self.world, rng)

    def table(self, port: int) -> List[Candidate]:
        return sorted(self.candidates(port), key=lambda c: precedence_key(c, None))


def pick_gateway(world: World, wanted: Optional[str]) -> Gateway:
    if wanted:
        for gateway in world.gateways:
            if gateway.key == wanted or (gateway.name == wanted and "/" not in wanted):
                return gateway
        raise InputError(f"no Gateway {wanted} in the input")
    if not world.gateways:
        raise InputError("no Gateway in the input")
    if len(world.gateways) > 1:
        raise InputError(f"several Gateways ({', '.join(g.key for g in world.gateways)}); pass --gateway NAMESPACE/NAME")
    return world.gateways[0]


def describe(candidate: Candidate) -> str:
    match = candidate.match
    parts = [f"{match.path_type} {match.path}"]
    if match.method:
        parts.append(match.method)
    if match.headers:
        parts.append("headers " + " ".join(f"{h.name}={h.value}" for h in match.headers))
    if match.query:
        parts.append("query " + " ".join(f"{q.name}={q.value}" for q in match.query))
    return f"{candidate.attachment.route.key} rule {candidate.rule.index} match {candidate.match_index}: {', '.join(parts)}"


def status_json(world: World) -> dict:
    attachments, statuses, problems = attach(world)
    routes = []
    for route in world.routes:
        parents = [{"parentRef": ps.parent.to_json(), "controllerName": CONTROLLER, "conditions": [c.to_json() for c in ps.conditions]} for ps in statuses.get(route.key, [])]
        routes.append({"namespace": route.namespace, "name": route.name, "status": {"parents": parents}})
    gateways = []
    for gateway in world.gateways:
        listeners = [{"name": l.name, "port": l.port, "protocol": l.protocol, "hostname": l.hostname, "acceptedRoutes": len({a.route.key for a in attachments if a.gateway.key == gateway.key and a.listener.name == l.name})} for l in gateway.listeners]
        gateways.append({"namespace": gateway.namespace, "name": gateway.name, "listeners": listeners})
    return {"routes": routes, "gateways": gateways, "problems": problems}


def render_status(report: dict) -> str:
    lines: List[str] = []
    for route in report["routes"]:
        lines.append(f"HTTPRoute {route['namespace']}/{route['name']}")
        if not route["status"]["parents"]:
            lines.append("  (no status: no parentRef names a Gateway in the input)")
        for parent in route["status"]["parents"]:
            ref = parent["parentRef"]
            section = f" section {ref['sectionName']}" if ref.get("sectionName") else ""
            lines.append(f"  parent {ref['namespace']}/{ref['name']}{section}")
            for c in parent["conditions"]:
                detail = "" if c["status"] == "True" else f": {c['message']}"
                lines.append(f"    {c['type']}={c['status']} ({c['reason']}){detail}")
    for gateway in report["gateways"]:
        lines.append(f"Gateway {gateway['namespace']}/{gateway['name']}")
        for l in gateway["listeners"]:
            lines.append(f"  listener {l['name']} :{l['port']} {l['protocol']} {l['hostname'] or '*'}: {l['acceptedRoutes']} route(s) accepted")
    for problem in report["problems"]:
        lines.append(f"warning: {problem}")
    return "\n".join(lines)


def decision_json(decision: Decision, request: Request) -> dict:
    winner = decision.winner
    return {
        "request": {"method": request.method, "host": request.host, "path": request.path, "query": request.query, "port": request.port},
        "action": decision.action,
        "status": decision.status,
        "reason": decision.reason,
        "winner": {"route": winner.attachment.route.key, "rule": winner.rule.index, "match": winner.match_index, "listener": winner.attachment.listener.name} if winner else None,
        "why": decision.why,
        "candidates": [describe(c) for c in decision.ranked],
        "location": decision.location,
        "upstream": {"host": decision.host, "path": decision.path, "headers": decision.headers} if decision.action == "forward" else None,
        "backends": [{"namespace": s.backend.namespace, "name": s.backend.name, "port": s.backend.port, "weight": s.backend.weight, "percent": round(s.percent, 2), "problem": s.problem} for s in decision.splits],
        "backend": decision.backend.key if decision.backend else None,
    }


def render_decision(decision: Decision, request: Request, gateway: str) -> str:
    query = f"?{request.query}" if request.query else ""
    lines = [f"{request.method} {request.host}{request.path}{query} on {gateway} port {request.port}"]
    if decision.ranked:
        lines.append("candidates in precedence order:")
        for index, candidate in enumerate(decision.ranked, 1):
            lines.append(f"  {index}. {describe(candidate)}")
    if decision.winner:
        winner = decision.winner
        beat = f"beats the next candidate on {decision.why}" if decision.why != "only match" else "the only match"
        lines.append(f"winner: {winner.attachment.route.key} rule {winner.rule.index} ({beat})")
        kinds = [f.get("type") for f in winner.rule.filters]
        if kinds:
            lines.append("filters: " + ", ".join(kinds))
    if decision.action == "redirect":
        lines.append(f"redirect: {decision.status} Location {decision.location}")
    elif decision.action == "forward":
        if decision.path != request.path or decision.host != request.authority:
            lines.append(f"upstream request: Host {decision.host}, path {decision.path}")
        for split in decision.splits:
            problem = f" -> 500 ({split.problem})" if split.problem else ""
            lines.append(f"forward: {split.backend.key}:{split.backend.port} weight {split.backend.weight} ({split.percent:.1f}%){problem}")
    else:
        lines.append(f"respond: {decision.status} {decision.reason}")
    return "\n".join(lines)


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect)


def forward(url: str, method: str, headers: Dict[str, str], body: Optional[bytes], timeout: float = 10.0) -> Tuple[int, List[Tuple[str, str]], bytes]:
    request = urllib.request.Request(url, data=body, method=method, headers=headers)
    try:
        with OPENER.open(request, timeout=timeout) as response:
            return response.status, list(response.getheaders()), response.read()
    except urllib.error.HTTPError as err:
        return err.code, list(err.headers.items()), err.read()
    except (urllib.error.URLError, OSError) as err:
        return 503, [], f"upstream connect error: {err}\n".encode()


def backend_url(backends: Dict[str, str], ref: BackendRef) -> Optional[str]:
    return backends.get(f"{ref.key}:{ref.port}") or backends.get(ref.key)


class GatewayServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address: Tuple[str, int], router: Router, gateway_port: int, backends: Dict[str, str], rng: random.Random, verbose: bool = False):
        super().__init__(address, GatewayHandler)
        self.router = router
        self.gateway_port = gateway_port
        self.backends = backends
        self.rng = rng
        self.lock = threading.Lock()
        self.verbose = verbose


class GatewayHandler(BaseHTTPRequestHandler):
    server: GatewayServer

    def handle_any(self) -> None:
        server = self.server
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else None
        request = make_request(self.command, self.headers.get("Host") or "", self.path, dict(self.headers.items()), server.gateway_port)
        with server.lock:
            decision = server.router.decide(request, server.rng)
        extra: List[Tuple[str, str]] = []
        if decision.action == "forward" and decision.backend is not None:
            url = backend_url(server.backends, decision.backend)
            if url is None:
                status, headers, payload = 503, [], f"no ready endpoints for service {decision.backend.key}\n".encode()
            else:
                upstream = {k: v for k, v in decision.headers.items() if k not in HOP_BY_HOP}
                upstream["Host"] = decision.host
                target = url.rstrip("/") + decision.path + (f"?{decision.query}" if decision.query else "")
                status, headers, payload = forward(target, request.method, upstream, body)
        else:
            status, headers, payload = decision.status or 500, [], (decision.reason + "\n").encode()
            if decision.location:
                extra.append(("Location", decision.location))
        if decision.winner:
            extra.append(("x-minigateway-route", f"{decision.winner.attachment.route.key} rule {decision.winner.rule.index}"))
        self.send_response(status)
        for name, value in headers:
            if name.lower() not in HOP_BY_HOP and name.lower() not in ("server", "date"):
                self.send_header(name, value)
        for name, value in extra:
            self.send_header(name, value)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)
        if server.verbose:
            target = decision.backend.key if decision.backend else decision.reason
            print(f"[minigateway] {request.method} {request.host}{request.path} -> {status} {target}", file=sys.stderr, flush=True)

    do_GET = do_POST = do_PUT = do_PATCH = do_DELETE = do_OPTIONS = handle_any

    def log_message(self, format: str, *args: Any) -> None:
        return


class EchoServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address: Tuple[str, int], name: str):
        super().__init__(address, EchoHandler)
        self.backend_name = name


class EchoHandler(BaseHTTPRequestHandler):
    server: EchoServer

    def handle_any(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""
        parts = urllib.parse.urlsplit(self.path)
        payload = {
            "backend": self.server.backend_name,
            "method": self.command,
            "host": self.headers.get("Host"),
            "path": parts.path,
            "query": parts.query,
            "headers": {k.lower(): v for k, v in self.headers.items()},
            "body_bytes": len(body),
        }
        data = (json.dumps(payload, sort_keys=True) + "\n").encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    do_GET = do_POST = do_PUT = do_PATCH = do_DELETE = do_OPTIONS = handle_any

    def log_message(self, format: str, *args: Any) -> None:
        return


def parse_pairs(values: Sequence[str], flag: str) -> Dict[str, str]:
    pairs: Dict[str, str] = {}
    for value in values:
        key, sep, rest = value.partition("=")
        if not sep or not key or not rest:
            raise InputError(f"{flag} {value!r}: expected KEY=VALUE")
        pairs[key] = rest
    return pairs


def parse_binds(values: Sequence[str], router: Router) -> List[Tuple[int, int]]:
    if not values:
        return [(port, port + DEFAULT_PORT_OFFSET) for port in router.ports()]
    binds = []
    for gateway_port, local_port in parse_pairs(values, "--bind").items():
        if not gateway_port.isdigit() or not local_port.isdigit():
            raise InputError(f"--bind {gateway_port}={local_port}: expected GATEWAYPORT=LOCALPORT")
        if int(gateway_port) not in router.ports():
            raise InputError(f"--bind: gateway {router.gateway.key} has no listener on port {gateway_port} (ports: {router.ports()})")
        binds.append((int(gateway_port), int(local_port)))
    return binds


def parse_headers(values: Sequence[str]) -> Dict[str, str]:
    headers: Dict[str, str] = {}
    for value in values:
        name, sep, rest = value.partition(":")
        if not sep or not name.strip():
            raise InputError(f"-H {value!r}: expected 'Name: value'")
        headers[name.strip().lower()] = rest.strip()
    return headers


def start_servers(servers: Sequence[ThreadingHTTPServer]) -> List[threading.Thread]:
    threads = []
    for server in servers:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        threads.append(thread)
    return threads


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="minigateway", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    def with_inputs(p: argparse.ArgumentParser) -> None:
        p.add_argument("files", nargs="+", help="JSON files or directories: Gateway, HTTPRoute, ReferenceGrant, Service, Namespace objects (a List, an array or single objects)")
        p.add_argument("--gateway", help="NAMESPACE/NAME when the input holds several Gateways")
        p.add_argument("--json", action="store_true", help="machine-readable output")

    status = sub.add_parser("status", help="attach routes and print the conditions a controller would write")
    with_inputs(status)
    route = sub.add_parser("route", help="decide one request without sending it")
    with_inputs(route)
    route.add_argument("--host", required=True, help="the Host header")
    route.add_argument("--path", default="/", help="path with an optional ?query")
    route.add_argument("--method", default="GET")
    route.add_argument("-H", "--header", action="append", default=[], help="'Name: value', repeatable")
    route.add_argument("--port", type=int, help="the listener port the request arrives on")
    table = sub.add_parser("table", help="every attached rule for a port, in precedence order")
    with_inputs(table)
    table.add_argument("--port", type=int)
    serve = sub.add_parser("serve", help="run the data plane: a reverse proxy per listener port")
    with_inputs(serve)
    serve.add_argument("--backend", action="append", default=[], help="NAMESPACE/NAME[:PORT]=URL, repeatable; the address of a Service's endpoints")
    serve.add_argument("--bind", action="append", default=[], help=f"GATEWAYPORT=LOCALPORT (0 picks a free port); default listener port + {DEFAULT_PORT_OFFSET}")
    serve.add_argument("--address", default="127.0.0.1")
    serve.add_argument("--seed", type=int, help="seed for the weighted backend choice")
    serve.add_argument("--verbose", action="store_true", help="one stderr line per request")
    backend = sub.add_parser("backend", help="a tiny echo backend that answers with its name and the request it saw")
    backend.add_argument("--name", required=True)
    backend.add_argument("--port", type=int, default=8080)
    backend.add_argument("--host", default="127.0.0.1")
    return parser


def run_forever(servers: Sequence[ThreadingHTTPServer]) -> int:
    threads = start_servers(servers)
    try:
        for thread in threads:
            thread.join()
    except KeyboardInterrupt:
        for server in servers:
            server.shutdown()
    return 0


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(list(sys.argv[1:] if argv is None else argv))
    try:
        if args.command == "backend":
            echo = EchoServer((args.host, args.port), args.name)
            print(f"backend {args.name} listening on http://{args.host}:{echo.server_address[1]}", flush=True)
            return run_forever([echo])
        world = build_world(read_documents(args.files))
        if args.command == "status":
            report = status_json(world)
            print(json.dumps(report, indent=2) if args.json else render_status(report))
            return 0
        backends = parse_pairs(args.backend, "--backend") if args.command == "serve" else {}
        for key in backends:
            world.services.setdefault(key.split(":")[0], [])
        router = Router(world, args.gateway)
        if args.command == "table":
            port = args.port or router.default_port()
            rows = router.table(port)
            if args.json:
                print(json.dumps([{"hostnames": c.attachment.hostnames, "candidate": describe(c)} for c in rows], indent=2))
            else:
                print(f"{router.gateway.key} port {port}: {len(rows)} match(es) in precedence order")
                for index, candidate in enumerate(rows, 1):
                    print(f"  {index:>2}. [{','.join(candidate.attachment.hostnames) or '*'}] {describe(candidate)}")
            return 0
        if args.command == "route":
            request = make_request(args.method, args.host, args.path, parse_headers(args.header), args.port or router.default_port())
            decision = router.decide(request)
            print(json.dumps(decision_json(decision, request), indent=2) if args.json else render_decision(decision, request, router.gateway.key))
            return 0
        servers = []
        for gateway_port, local_port in parse_binds(args.bind, router):
            server = GatewayServer((args.address, local_port), router, gateway_port, backends, random.Random(args.seed), args.verbose)
            servers.append(server)
            print(f"listening gateway={router.gateway.key} port={gateway_port} url=http://{args.address}:{server.server_address[1]}", flush=True)
        for problem in router.problems:
            print(f"warning: {problem}", file=sys.stderr, flush=True)
        return run_forever(servers)
    except InputError as exc:
        print(f"minigateway: {exc}", file=sys.stderr)
        return 2
    except OSError as exc:
        print(f"minigateway: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
