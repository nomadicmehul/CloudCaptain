from __future__ import annotations

import argparse
import copy
import hashlib
import json
import math
import os
import re
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from fractions import Fraction
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

Obj = Dict[str, Any]
Runner = Callable[[List[str], Optional[str]], Tuple[int, str, str]]
Quantity = Tuple[Fraction, bool]

MANAGER = "minienv"
GROUP = "preview.cloudcaptain.io"
LABEL_MANAGED = "app.kubernetes.io/managed-by"
LABEL_REPO = f"{GROUP}/repo"
LABEL_PR = f"{GROUP}/pr"
LABEL_OWNER = f"{GROUP}/owner"
LABEL_EXPIRES = f"{GROUP}/expires"
LABEL_ENV = f"{GROUP}/environment"
ANNOTATION_EXPIRES_AT = f"{GROUP}/expires-at"
ANNOTATION_URL = f"{GROUP}/url"
ANNOTATION_HEAD = f"{GROUP}/head-sha"
ANNOTATION_BRANCH = f"{GROUP}/branch"
JANITOR_EXPIRES = "janitor/expires"
APP = "app"
TTL_LABEL = re.compile(r"^preview-ttl-([0-9]{1,4})h$")
SKIP_LABEL = "no-preview"
FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")

RESOURCES = {
    "Namespace": ("namespaces", False),
    "ResourceQuota": ("resourcequotas", True),
    "LimitRange": ("limitranges", True),
    "NetworkPolicy": ("networkpolicies.networking.k8s.io", True),
    "RoleBinding": ("rolebindings.rbac.authorization.k8s.io", True),
    "Deployment": ("deployments.apps", True),
    "Service": ("services", True),
    "HTTPRoute": ("httproutes.gateway.networking.k8s.io", True),
    "Pod": ("pods", True),
    "ClusterRoleBinding": ("clusterrolebindings.rbac.authorization.k8s.io", False),
    "ClusterRole": ("clusterroles.rbac.authorization.k8s.io", False),
    "PersistentVolume": ("persistentvolumes", False),
}
CLUSTER_LEFTOVER_KINDS = ("ClusterRoleBinding", "ClusterRole", "PersistentVolume")

DEFAULT_QUOTA = {
    "pods": "10",
    "requests.cpu": "1",
    "requests.memory": "1Gi",
    "limits.cpu": "2",
    "limits.memory": "2Gi",
    "services": "5",
    "services.loadbalancers": "0",
    "services.nodeports": "0",
    "persistentvolumeclaims": "2",
    "requests.storage": "5Gi",
}
DEFAULT_LIMITS = {
    "default": {"cpu": "250m", "memory": "256Mi"},
    "defaultRequest": {"cpu": "100m", "memory": "128Mi"},
    "max": {"cpu": "1", "memory": "1Gi"},
}
DEFAULT_PRICES = {"currency": "USD", "cpu_core_hour": 0.0316, "memory_gib_hour": 0.0042}


class MinienvError(Exception):
    exit_code = 1


class InputError(MinienvError):
    exit_code = 2


class ClusterError(MinienvError):
    exit_code = 1


@dataclass
class Config:
    repo: str = "shop"
    prefix: str = "preview"
    image_template: str = "ghcr.io/acme/{repo}:pr-{number}-{short_sha}"
    domain: str = "preview.example.test"
    port: int = 8080
    replicas: int = 1
    ttl_hours: int = 72
    require_label: Optional[str] = None
    user_prefix: str = ""
    role: str = "edit"
    gateway: Optional[str] = None
    quota: Dict[str, str] = field(default_factory=lambda: dict(DEFAULT_QUOTA))
    limits: Dict[str, Dict[str, str]] = field(default_factory=lambda: copy.deepcopy(DEFAULT_LIMITS))
    app_requests: Dict[str, str] = field(default_factory=lambda: {"cpu": "100m", "memory": "128Mi"})
    app_limits: Dict[str, str] = field(default_factory=lambda: {"cpu": "250m", "memory": "256Mi"})


@dataclass
class PullRequest:
    number: int
    state: str
    login: str
    head_sha: str
    branch: str
    updated_at: datetime
    labels: List[str]


def parse_time(text: str) -> datetime:
    try:
        moment = datetime.fromisoformat(text.strip().replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        raise InputError(f"not an ISO 8601 time: {text!r}")
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_pulls(data: Any) -> List[PullRequest]:
    if not isinstance(data, list):
        raise InputError("pull request list: expected a JSON array like GET /repos/OWNER/REPO/pulls returns")
    pulls = []
    for index, item in enumerate(data):
        where = f"pull request list[{index}]"
        if not isinstance(item, dict):
            raise InputError(f"{where}: expected an object")
        try:
            number = item["number"]
            login = item["user"]["login"]
            sha = item["head"]["sha"]
            updated = item["updated_at"]
        except (KeyError, TypeError) as exc:
            raise InputError(f"{where}: missing field {exc}")
        if not isinstance(number, int) or number <= 0:
            raise InputError(f"{where}: number must be a positive integer, got {number!r}")
        if not re.fullmatch(r"[0-9a-f]{7,64}", str(sha)):
            raise InputError(f"{where}: head.sha is not a commit id: {sha!r}")
        labels = [label["name"] if isinstance(label, dict) else str(label) for label in item.get("labels") or []]
        pulls.append(PullRequest(number, str(item.get("state", "open")), str(login), str(sha), str(item["head"].get("ref", "")), parse_time(str(updated)), labels))
    return pulls


def load_pulls(path: str) -> List[PullRequest]:
    try:
        with open(path, encoding="utf-8") as handle:
            return parse_pulls(json.load(handle))
    except OSError as exc:
        raise InputError(f"{path}: {exc.strerror}")
    except json.JSONDecodeError as exc:
        raise InputError(f"{path}: not JSON ({exc})")


def slug(text: str, limit: int) -> str:
    cleaned = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-") or "x"
    if len(cleaned) <= limit:
        return cleaned
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[:6]
    return cleaned[: limit - 7].rstrip("-") + "-" + digest


def namespace_name(config: Config, number: int) -> str:
    tail = f"-pr-{number}"
    head = f"{config.prefix}-{config.repo}"
    return slug(head, 63 - len(tail)) + tail


def namespace_stem(config: Config) -> str:
    return namespace_name(config, 1)[: -len("1")]


def label_value(text: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_.-]+", "-", text)[:63]
    return re.sub(r"^[^A-Za-z0-9]+|[^A-Za-z0-9]+$", "", cleaned)


def ttl_hours(pr: PullRequest, config: Config) -> int:
    for label in pr.labels:
        found = TTL_LABEL.match(label)
        if found and int(found.group(1)) > 0:
            return int(found.group(1))
    return config.ttl_hours


def expiry(pr: PullRequest, config: Config) -> datetime:
    return pr.updated_at + timedelta(hours=ttl_hours(pr, config))


def wanted(pr: PullRequest, config: Config, now: datetime) -> Tuple[bool, str]:
    if pr.state != "open":
        return False, f"state is {pr.state}"
    if SKIP_LABEL in pr.labels:
        return False, f"labelled {SKIP_LABEL}"
    if config.require_label and config.require_label not in pr.labels:
        return False, f"missing label {config.require_label}"
    if expiry(pr, config) <= now:
        return False, f"expired at {iso(expiry(pr, config))} (push a commit to renew)"
    return True, "open"


def image_for(pr: PullRequest, config: Config) -> str:
    try:
        return config.image_template.format(repo=config.repo, number=pr.number, sha=pr.head_sha, short_sha=pr.head_sha[:7], branch=slug(pr.branch or "head", 40))
    except (KeyError, IndexError) as exc:
        raise InputError(f"--image-template: unknown placeholder {exc}; use repo, number, sha, short_sha, branch")


def url_for(pr: PullRequest, config: Config) -> str:
    return f"https://pr-{pr.number}.{slug(config.repo, 40)}.{config.domain}"


def meta(name: str, namespace: Optional[str], labels: Dict[str, str], annotations: Optional[Dict[str, str]] = None) -> Obj:
    body: Obj = {"name": name, "labels": dict(labels)}
    if namespace:
        body["namespace"] = namespace
    if annotations:
        body["annotations"] = dict(annotations)
    return body


def desired_objects(pr: PullRequest, config: Config) -> List[Obj]:
    ns = namespace_name(config, pr.number)
    expires = expiry(pr, config)
    common = {LABEL_MANAGED: MANAGER, LABEL_REPO: label_value(config.repo), LABEL_PR: str(pr.number), LABEL_ENV: ns}
    ns_labels = dict(common, **{LABEL_OWNER: label_value(pr.login), LABEL_EXPIRES: str(int(expires.timestamp()))})
    ns_annotations = {ANNOTATION_EXPIRES_AT: iso(expires), JANITOR_EXPIRES: iso(expires), ANNOTATION_URL: url_for(pr, config), ANNOTATION_HEAD: pr.head_sha, ANNOTATION_BRANCH: pr.branch}
    selector = {"app.kubernetes.io/name": APP}
    objects: List[Obj] = [
        {"apiVersion": "v1", "kind": "Namespace", "metadata": meta(ns, None, ns_labels, ns_annotations)},
        {"apiVersion": "v1", "kind": "ResourceQuota", "metadata": meta("preview-quota", ns, common), "spec": {"hard": dict(config.quota)}},
        {"apiVersion": "v1", "kind": "LimitRange", "metadata": meta("preview-limits", ns, common),
         "spec": {"limits": [dict({"type": "Container"}, **copy.deepcopy(config.limits))]}},
        network_policy("default-deny", ns, common, {"podSelector": {}, "policyTypes": ["Ingress", "Egress"]}),
        network_policy("allow-dns", ns, common, {"podSelector": {}, "policyTypes": ["Egress"], "egress": [{
            "to": [{"namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": "kube-system"}}, "podSelector": {"matchLabels": {"k8s-app": "kube-dns"}}}],
            "ports": [{"protocol": "UDP", "port": 53}, {"protocol": "TCP", "port": 53}]}]}),
        network_policy("allow-same-namespace", ns, common, {"podSelector": {}, "policyTypes": ["Ingress", "Egress"],
                                                           "ingress": [{"from": [{"podSelector": {}}]}], "egress": [{"to": [{"podSelector": {}}]}]}),
        {"apiVersion": "rbac.authorization.k8s.io/v1", "kind": "RoleBinding", "metadata": meta("preview-author", ns, common),
         "roleRef": {"apiGroup": "rbac.authorization.k8s.io", "kind": "ClusterRole", "name": config.role},
         "subjects": [{"apiGroup": "rbac.authorization.k8s.io", "kind": "User", "name": config.user_prefix + pr.login}]},
        deployment(pr, config, ns, common, selector),
        {"apiVersion": "v1", "kind": "Service", "metadata": meta(APP, ns, common),
         "spec": {"type": "ClusterIP", "selector": dict(selector), "ports": [{"name": "http", "port": 80, "targetPort": config.port, "protocol": "TCP"}]}},
    ]
    if config.gateway:
        gateway_ns, gateway_name = split_gateway(config.gateway)
        objects.append(network_policy("allow-gateway", ns, common, {"podSelector": {"matchLabels": dict(selector)}, "policyTypes": ["Ingress"], "ingress": [{
            "from": [{"namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": gateway_ns}}}],
            "ports": [{"protocol": "TCP", "port": config.port}]}]}))
        objects.append({"apiVersion": "gateway.networking.k8s.io/v1", "kind": "HTTPRoute", "metadata": meta(APP, ns, common),
                        "spec": {"parentRefs": [{"name": gateway_name, "namespace": gateway_ns}], "hostnames": [url_for(pr, config).split("://", 1)[1]],
                                 "rules": [{"backendRefs": [{"name": APP, "port": 80}]}]}})
    return objects


def split_gateway(text: str) -> Tuple[str, str]:
    parts = text.split("/")
    if len(parts) != 2 or not all(parts):
        raise InputError(f"--gateway must be NAMESPACE/NAME, got {text!r}")
    return parts[0], parts[1]


def network_policy(name: str, ns: str, labels: Dict[str, str], spec: Obj) -> Obj:
    return {"apiVersion": "networking.k8s.io/v1", "kind": "NetworkPolicy", "metadata": meta(name, ns, labels), "spec": spec}


def deployment(pr: PullRequest, config: Config, ns: str, labels: Dict[str, str], selector: Dict[str, str]) -> Obj:
    container = {
        "name": APP,
        "image": image_for(pr, config),
        "ports": [{"name": "http", "containerPort": config.port, "protocol": "TCP"}],
        "env": [{"name": "PREVIEW_URL", "value": url_for(pr, config)}, {"name": "PREVIEW_SHA", "value": pr.head_sha}],
        "resources": {"requests": dict(config.app_requests), "limits": dict(config.app_limits)},
        "readinessProbe": {"httpGet": {"path": "/", "port": config.port}, "periodSeconds": 2},
    }
    return {"apiVersion": "apps/v1", "kind": "Deployment", "metadata": meta(APP, ns, labels),
            "spec": {"replicas": config.replicas, "selector": {"matchLabels": dict(selector)},
                     "template": {"metadata": {"labels": dict(selector, **{LABEL_PR: str(pr.number)})},
                                  "spec": {"terminationGracePeriodSeconds": 5, "containers": [container]}}}}


SUFFIXES: Dict[str, Tuple[Fraction, bool]] = {
    "Ki": (Fraction(1024), True), "Mi": (Fraction(1024 ** 2), True), "Gi": (Fraction(1024 ** 3), True),
    "Ti": (Fraction(1024 ** 4), True), "Pi": (Fraction(1024 ** 5), True), "Ei": (Fraction(1024 ** 6), True),
    "n": (Fraction(1, 10 ** 9), False), "u": (Fraction(1, 10 ** 6), False), "m": (Fraction(1, 1000), False), "": (Fraction(1), False),
    "k": (Fraction(10 ** 3), False), "M": (Fraction(10 ** 6), False), "G": (Fraction(10 ** 9), False),
    "T": (Fraction(10 ** 12), False), "P": (Fraction(10 ** 15), False), "E": (Fraction(10 ** 18), False),
}
QUANTITY = re.compile(r"^([+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+))(?:e([+-]?[0-9]+))?([A-Za-z]{0,2})$")
DECIMAL_SUFFIX = {-9: "n", -6: "u", -3: "m", 0: "", 3: "k", 6: "M", 9: "G", 12: "T", 15: "P", 18: "E"}


def parse_quantity(text: Any) -> Quantity:
    found = QUANTITY.match(str(text).strip())
    if not found or found.group(3) not in SUFFIXES:
        raise InputError(f"not a Kubernetes quantity: {text!r}")
    number = Fraction(found.group(1))
    if found.group(2):
        number *= Fraction(10) ** int(found.group(2))
    scale, binary = SUFFIXES[found.group(3)]
    return number * scale, binary


def is_quantity(text: Any) -> bool:
    try:
        parse_quantity(text)
    except InputError:
        return False
    return True


def format_quantity(value: Fraction, binary: bool = False) -> str:
    if value == 0:
        return "0"
    if binary and value.denominator == 1 and value != 0:
        power = 0
        while power < 6 and value.numerator % (1024 ** (power + 1)) == 0:
            power += 1
        return f"{value.numerator // 1024 ** power}{['', 'Ki', 'Mi', 'Gi', 'Ti', 'Pi', 'Ei'][power]}"
    for exponent in range(18, -12, -3):
        mantissa = value / Fraction(10) ** exponent
        if mantissa.denominator == 1:
            return f"{mantissa.numerator}{DECIMAL_SUFFIX.get(exponent, f'e{exponent}')}"
    return f"{math.ceil(value * 10 ** 9)}n"


def add(left: Optional[Quantity], right: Quantity) -> Quantity:
    if left is None or left[0] == 0:
        return right[0] + (left[0] if left else 0), right[1]
    return left[0] + right[0], left[1]


def show(quantity: Quantity) -> str:
    return format_quantity(quantity[0], quantity[1])


def containers_of(pod: Obj) -> List[Obj]:
    return list((pod.get("spec") or {}).get("containers") or [])


def init_containers_of(pod: Obj) -> List[Obj]:
    return list((pod.get("spec") or {}).get("initContainers") or [])


def default_requests_to_limits(container: Obj) -> None:
    resources = container.setdefault("resources", {})
    limits = resources.get("limits") or {}
    for name, value in limits.items():
        resources.setdefault("requests", {}).setdefault(name, value)


def apply_limit_range(container: Obj, item: Obj, errors: List[str]) -> None:
    resources = container.setdefault("resources", {})
    for name, value in (item.get("default") or {}).items():
        resources.setdefault("limits", {}).setdefault(name, value)
    for name, value in (item.get("defaultRequest") or {}).items():
        resources.setdefault("requests", {}).setdefault(name, value)
    kind = item.get("type", "Container")
    for name, bound in sorted((item.get("min") or {}).items()):
        request = (resources.get("requests") or {}).get(name)
        if request is None:
            errors.append(f"minimum {name} usage per {kind} is {bound}.  No request is specified")
        elif parse_quantity(request)[0] < parse_quantity(bound)[0]:
            errors.append(f"minimum {name} usage per {kind} is {bound}, but request is {request}")
    for name, bound in sorted((item.get("max") or {}).items()):
        limit = (resources.get("limits") or {}).get(name)
        if limit is None:
            errors.append(f"maximum {name} usage per {kind} is {bound}.  No limit is specified")
        elif parse_quantity(limit)[0] > parse_quantity(bound)[0]:
            errors.append(f"maximum {name} usage per {kind} is {bound}, but limit is {limit}")


def pod_usage(pod: Obj) -> Dict[str, Quantity]:
    totals: Dict[str, Quantity] = {}
    for container in containers_of(pod):
        for section in ("requests", "limits"):
            for name, value in ((container.get("resources") or {}).get(section) or {}).items():
                key = f"{section}.{name}"
                totals[key] = add(totals.get(key), parse_quantity(value))
    for container in init_containers_of(pod):
        for section in ("requests", "limits"):
            for name, value in ((container.get("resources") or {}).get(section) or {}).items():
                key = f"{section}.{name}"
                quantity = parse_quantity(value)
                if key not in totals or quantity[0] > totals[key][0]:
                    totals[key] = quantity
    for name in ("cpu", "memory"):
        if f"requests.{name}" in totals:
            totals[name] = totals[f"requests.{name}"]
    totals["pods"] = (Fraction(1), False)
    totals["count/pods"] = (Fraction(1), False)
    return totals


def pretty(values: Dict[str, Quantity]) -> str:
    return ",".join(f"{key}={show(values[key])}" for key in sorted(values))


def aggregate(errors: List[str]) -> str:
    return errors[0] if len(errors) == 1 else "[" + ", ".join(errors) + "]"


def check_quota(pod: Obj, quota: Obj) -> Optional[str]:
    name = quota["metadata"]["name"]
    spec_hard = (quota.get("spec") or {}).get("hard") or {}
    status = quota.get("status") or {}
    hard = {key: parse_quantity(value) for key, value in (status.get("hard") or {}).items()}
    unknown = sorted(key for key in spec_hard if key not in hard)
    if unknown:
        return f"status unknown for quota: {name}, resources: {','.join(unknown)}"
    used = {key: parse_quantity(value) for key, value in (status.get("used") or {}).items()}
    explicit = [key for key in sorted(hard) if key in ("cpu", "memory", "requests.cpu", "requests.memory", "limits.cpu", "limits.memory")]
    missing = []
    for key in explicit:
        section, resource = key.split(".", 1) if "." in key else ("requests", key)
        lacking = [c.get("name", "") for c in containers_of(pod) if resource not in ((c.get("resources") or {}).get(section) or {})]
        if lacking:
            missing.append(f"{key} for: {','.join(lacking)}")
    if missing:
        return f"failed quota: {name}: must specify {'; '.join(missing)}"
    requested = {key: value for key, value in pod_usage(pod).items() if key in hard}
    exceeded = sorted(key for key, value in requested.items() if (used.get(key, (Fraction(0), False))[0] + value[0]) > hard[key][0])
    if not exceeded:
        return None
    return "exceeded quota: {}, requested: {}, used: {}, limited: {}".format(
        name, pretty({k: requested[k] for k in exceeded}), pretty({k: used.get(k, (Fraction(0), requested[k][1])) for k in exceeded}), pretty({k: hard[k] for k in exceeded}))


def admit_pod(pod: Obj, objects: Sequence[Obj]) -> Tuple[Obj, Optional[str]]:
    admitted = copy.deepcopy(pod)
    name = (admitted.get("metadata") or {}).get("name", "")
    for container in init_containers_of(admitted) + containers_of(admitted):
        default_requests_to_limits(container)
    errors: List[str] = []
    for limit_range in sorted((o for o in objects if o.get("kind") == "LimitRange"), key=lambda o: o["metadata"]["name"]):
        for item in (limit_range.get("spec") or {}).get("limits") or []:
            if item.get("type", "Container") == "Container":
                for container in init_containers_of(admitted) + containers_of(admitted):
                    apply_limit_range(container, item, errors)
    if errors:
        return admitted, f'pods "{name}" is forbidden: {aggregate(errors)}'
    for quota in sorted((o for o in objects if o.get("kind") == "ResourceQuota"), key=lambda o: o["metadata"]["name"]):
        problem = check_quota(admitted, quota)
        if problem:
            return admitted, f'pods "{name}" is forbidden: {problem}'
    return admitted, None


def parse_rejection(message: str) -> Dict[str, Dict[str, Fraction]]:
    found = re.search(r"exceeded quota: ([^,]+), requested: (\S*), used: (\S*), limited: (\S*)", message)
    if not found:
        return {}
    parts: Dict[str, Dict[str, Fraction]] = {"quota": {found.group(1): Fraction(0)}}
    for label, text in (("requested", found.group(2)), ("used", found.group(3)), ("limited", found.group(4))):
        parts[label] = {key: parse_quantity(value)[0] for key, value in (item.split("=", 1) for item in text.split(",") if "=" in item)}
    return parts


def same_scalar(have: Any, want: Any) -> bool:
    if have == want:
        return True
    if isinstance(have, str) and isinstance(want, str) and is_quantity(have) and is_quantity(want):
        return parse_quantity(have)[0] == parse_quantity(want)[0]
    return False


def covers(live: Any, want: Any) -> bool:
    if isinstance(want, dict):
        return isinstance(live, dict) and all(key in live and covers(live[key], value) for key, value in want.items())
    if isinstance(want, list):
        return isinstance(live, list) and len(live) == len(want) and all(covers(a, b) for a, b in zip(live, want))
    return same_scalar(live, want)


def run_command(argv: List[str], stdin: Optional[str] = None) -> Tuple[int, str, str]:
    try:
        proc = subprocess.run(argv, input=stdin, capture_output=True, text=True, timeout=120, env=dict(os.environ, LC_ALL="C"))
    except FileNotFoundError:
        return 127, "", f"{argv[0]}: command not found"
    except subprocess.TimeoutExpired:
        return 124, "", f"{' '.join(argv[:3])}: timed out after 120s"
    return proc.returncode, proc.stdout, proc.stderr


def resource_of(kind: str) -> Tuple[str, bool]:
    if kind not in RESOURCES:
        raise InputError(f"minienv does not manage kind {kind}")
    return RESOURCES[kind]


class Kubectl:
    def __init__(self, context: Optional[str] = None, runner: Runner = run_command) -> None:
        self.context = context
        self.runner = runner
        self.writes = 0

    def run(self, args: List[str], stdin: Optional[str] = None) -> str:
        argv = ["kubectl"] + (["--context", self.context] if self.context else []) + args
        code, out, err = self.runner(argv, stdin)
        if code != 0:
            raise ClusterError(f"kubectl {' '.join(args[:2])}: {err.strip() or out.strip() or f'exit {code}'}")
        return out

    def get(self, kind: str, name: str, namespace: Optional[str] = None) -> Optional[Obj]:
        resource, namespaced = resource_of(kind)
        scope = ["-n", namespace] if namespaced and namespace else []
        out = self.run(["get", resource, name] + scope + ["-o", "json", "--ignore-not-found"])
        return json.loads(out) if out.strip() else None

    def list(self, kind: str, selector: Optional[str] = None, namespace: Optional[str] = None) -> List[Obj]:
        resource, namespaced = resource_of(kind)
        scope = (["-n", namespace] if namespace else ["-A"]) if namespaced else []
        out = self.run(["get", resource] + scope + (["-l", selector] if selector else []) + ["-o", "json"])
        return list(json.loads(out).get("items") or []) if out.strip() else []

    def apply(self, obj: Obj) -> Obj:
        self.writes += 1
        return json.loads(self.run(["apply", "--server-side", f"--field-manager={MANAGER}", "--force-conflicts", "-o", "json", "-f", "-"], json.dumps(obj)))

    def delete(self, kind: str, name: str, namespace: Optional[str] = None) -> None:
        resource, namespaced = resource_of(kind)
        self.writes += 1
        scope = ["-n", namespace] if namespaced and namespace else []
        self.run(["delete", resource, name] + scope + ["--ignore-not-found", "--wait=false"])


def set_default(obj: Obj, path: Sequence[str], value: Any) -> None:
    for key in path[:-1]:
        obj = obj.setdefault(key, {})
    obj.setdefault(path[-1], value)


def merge(base: Any, patch: Any) -> Any:
    if isinstance(base, dict) and isinstance(patch, dict):
        result = copy.deepcopy(base)
        for key, value in patch.items():
            result[key] = merge(result.get(key), value)
        return result
    return copy.deepcopy(patch)


def canonical_quantities(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: canonical_quantities(v) for k, v in value.items()}
    if isinstance(value, list):
        return [canonical_quantities(v) for v in value]
    if isinstance(value, str) and value != "Container" and is_quantity(value):
        return show(parse_quantity(value))
    return value


class FakeCluster:
    def __init__(self) -> None:
        self.objects: Dict[Tuple[str, str, str], Obj] = {}
        self.revision = 100
        self.writes = 0
        self.calls: List[str] = []

    def key(self, kind: str, name: str, namespace: Optional[str]) -> Tuple[str, str, str]:
        return kind, (namespace or "") if resource_of(kind)[1] else "", name

    def get(self, kind: str, name: str, namespace: Optional[str] = None) -> Optional[Obj]:
        self.calls.append(f"get {kind} {namespace or ''}/{name}")
        found = self.objects.get(self.key(kind, name, namespace))
        return copy.deepcopy(found) if found else None

    def list(self, kind: str, selector: Optional[str] = None, namespace: Optional[str] = None) -> List[Obj]:
        self.calls.append(f"list {kind} {selector or ''}")
        items = [o for (k, ns, _), o in sorted(self.objects.items()) if k == kind and (namespace is None or ns == namespace)]
        return [copy.deepcopy(o) for o in items if matches(o, selector)]

    def defaults(self, obj: Obj) -> None:
        kind, metadata = obj["kind"], obj["metadata"]
        if kind == "Namespace":
            metadata.setdefault("labels", {})["kubernetes.io/metadata.name"] = metadata["name"]
            obj.setdefault("status", {})["phase"] = "Active"
        if kind == "Service":
            set_default(obj, ("spec", "clusterIP"), "10.96.0.%d" % (self.revision % 250 + 2))
            set_default(obj, ("spec", "sessionAffinity"), "None")
        if kind == "Deployment":
            set_default(obj, ("spec", "strategy"), {"type": "RollingUpdate", "rollingUpdate": {"maxSurge": "25%", "maxUnavailable": "25%"}})
            for container in obj["spec"]["template"]["spec"]["containers"]:
                container.setdefault("imagePullPolicy", "IfNotPresent")
                container.setdefault("terminationMessagePath", "/dev/termination-log")
                probe = container.get("readinessProbe") or {}
                if "httpGet" in probe:
                    probe["httpGet"].setdefault("scheme", "HTTP")
                    probe.setdefault("timeoutSeconds", 1)
        if kind == "NetworkPolicy":
            for rule in (obj["spec"].get("ingress") or []) + (obj["spec"].get("egress") or []):
                for port in rule.get("ports") or []:
                    port.setdefault("protocol", "TCP")
        if kind in ("ResourceQuota", "LimitRange"):
            obj["spec"] = canonical_quantities(obj["spec"])
        if kind == "ResourceQuota":
            obj["status"] = {"hard": dict(obj["spec"]["hard"]), "used": self.quota_used(metadata["namespace"], obj["spec"]["hard"])}

    def quota_used(self, namespace: str, hard: Dict[str, str]) -> Dict[str, str]:
        totals: Dict[str, Quantity] = {}
        for (kind, ns, _), pod in self.objects.items():
            if kind == "Pod" and ns == namespace:
                for key, value in pod_usage(pod).items():
                    totals[key] = add(totals.get(key), value)
        return {key: show(totals.get(key, (Fraction(0), False))) for key in hard}

    def store(self, obj: Obj, event: str) -> Obj:
        self.revision += 1
        self.writes += 1
        obj["metadata"]["resourceVersion"] = str(self.revision)
        self.objects[self.key(obj["kind"], obj["metadata"]["name"], obj["metadata"].get("namespace"))] = obj
        self.calls.append(f"{event} {obj['kind']} {obj['metadata'].get('namespace', '')}/{obj['metadata']['name']}")
        return copy.deepcopy(obj)

    def apply(self, obj: Obj) -> Obj:
        key = self.key(obj["kind"], obj["metadata"]["name"], obj["metadata"].get("namespace"))
        namespace = obj["metadata"].get("namespace")
        if resource_of(obj["kind"])[1] and self.key("Namespace", namespace or "", None) not in self.objects:
            raise ClusterError(f'namespaces "{namespace}" not found')
        live = self.objects.get(key)
        if live and live["kind"] == "Namespace" and live["status"].get("phase") == "Terminating":
            raise ClusterError(f'namespace {live["metadata"]["name"]} is being terminated')
        candidate = merge({k: v for k, v in (live or {}).items() if k != "status"}, obj)
        candidate["metadata"].pop("resourceVersion", None)
        self.defaults(candidate)
        if live is not None:
            before = dict(live, metadata={k: v for k, v in live["metadata"].items() if k != "resourceVersion"})
            if before == candidate:
                self.calls.append(f"apply (no change) {obj['kind']} {namespace or ''}/{obj['metadata']['name']}")
                return copy.deepcopy(live)
        if obj["kind"] == "Namespace" and live is None:
            candidate["metadata"]["uid"] = hashlib.sha256(obj["metadata"]["name"].encode()).hexdigest()[:12]
        return self.store(candidate, "update" if live else "create")

    def create_pod(self, pod: Obj, namespace: str) -> Obj:
        objects = [o for (kind, ns, _), o in self.objects.items() if ns == namespace and kind in ("LimitRange", "ResourceQuota")]
        admitted, problem = admit_pod(pod, objects)
        if problem:
            self.calls.append(f"rejected Pod {namespace}/{pod['metadata']['name']}")
            raise ClusterError(f"Error from server (Forbidden): {problem}")
        admitted["metadata"]["namespace"] = namespace
        stored = self.store(admitted, "create")
        for (kind, ns, _), quota in list(self.objects.items()):
            if kind == "ResourceQuota" and ns == namespace:
                quota["status"]["used"] = self.quota_used(namespace, quota["spec"]["hard"])
        return stored

    def delete(self, kind: str, name: str, namespace: Optional[str] = None) -> None:
        key = self.key(kind, name, namespace)
        if key not in self.objects:
            return
        self.writes += 1
        self.calls.append(f"delete {kind} {namespace or ''}/{name}")
        del self.objects[key]
        if kind == "Namespace":
            for other in [k for k in self.objects if k[1] == name and resource_of(k[0])[1]]:
                del self.objects[other]


def matches(obj: Obj, selector: Optional[str]) -> bool:
    labels = (obj.get("metadata") or {}).get("labels") or {}
    for term in [t.strip() for t in (selector or "").split(",") if t.strip()]:
        if "!=" in term:
            key, value = term.split("!=", 1)
            if labels.get(key) == value:
                return False
        elif "=" in term:
            key, value = term.split("=", 1)
            if labels.get(key.rstrip("=")) != value:
                return False
        elif term.startswith("!"):
            if term[1:] in labels:
                return False
        elif term not in labels:
            return False
    return True


def kind_name(obj: Obj) -> str:
    return f"{obj['kind']}/{obj['metadata']['name']}"


class Reconciler:
    def __init__(self, cluster: Any, config: Config, now: datetime, dry_run: bool = False, log: Callable[[str], None] = lambda _: None) -> None:
        self.cluster = cluster
        self.config = config
        self.now = now
        self.dry_run = dry_run
        self.log = log

    def ensure(self, obj: Obj) -> str:
        live = self.cluster.get(obj["kind"], obj["metadata"]["name"], obj["metadata"].get("namespace"))
        if live is not None and covers(live, obj):
            return "unchanged"
        action = "updated" if live is not None else "created"
        if live is not None and obj["kind"] == "RoleBinding" and live.get("roleRef") != obj["roleRef"]:
            action = "replaced"
            if not self.dry_run:
                self.cluster.delete("RoleBinding", obj["metadata"]["name"], obj["metadata"]["namespace"])
        if not self.dry_run:
            self.cluster.apply(obj)
        return action if not self.dry_run else f"would be {action}"

    def reconcile(self, pulls: Sequence[PullRequest]) -> Obj:
        report: Obj = {"now": iso(self.now), "dry_run": self.dry_run, "environments": [], "skipped": [], "writes": 0}
        for pr in sorted(pulls, key=lambda p: p.number):
            keep, reason = wanted(pr, self.config, self.now)
            if not keep:
                report["skipped"].append({"pr": pr.number, "reason": reason})
                self.log(f"#{pr.number}: skipped, {reason}")
                continue
            namespace = namespace_name(self.config, pr.number)
            live_ns = self.cluster.get("Namespace", namespace)
            if live_ns and (live_ns.get("status") or {}).get("phase") == "Terminating":
                report["skipped"].append({"pr": pr.number, "reason": f"namespace {namespace} is terminating; retry later"})
                continue
            if live_ns and (live_ns["metadata"].get("labels") or {}).get(LABEL_MANAGED) != MANAGER:
                raise ClusterError(f"namespace {namespace} exists and is not managed by {MANAGER}; refusing to adopt it")
            actions = {}
            for obj in desired_objects(pr, self.config):
                actions[kind_name(obj)] = self.ensure(obj)
            writes = sum(1 for action in actions.values() if action in ("created", "updated", "replaced"))
            report["writes"] += writes
            report["environments"].append({"pr": pr.number, "namespace": namespace, "owner": pr.login, "image": image_for(pr, self.config),
                                           "url": url_for(pr, self.config), "expires": iso(expiry(pr, self.config)), "writes": writes, "actions": actions})
            self.log(f"#{pr.number}: {namespace} {writes} write(s), expires {iso(expiry(pr, self.config))}, {url_for(pr, self.config)}")
        return report

    def environments(self) -> List[Obj]:
        selector = f"{LABEL_MANAGED}={MANAGER},{LABEL_REPO}={label_value(self.config.repo)}"
        start = namespace_stem(self.config)
        return [ns for ns in self.cluster.list("Namespace", selector) if ns["metadata"]["name"].startswith(start)]

    def reap_reason(self, ns: Obj, open_numbers: Optional[set]) -> Optional[str]:
        labels = ns["metadata"].get("labels") or {}
        try:
            expires = datetime.fromtimestamp(int(labels.get(LABEL_EXPIRES, "")), timezone.utc)
        except ValueError:
            return f"label {LABEL_EXPIRES} is missing or not a number"
        if expires <= self.now:
            return f"expired at {iso(expires)}"
        if open_numbers is not None and labels.get(LABEL_PR) not in open_numbers:
            return f"pull request #{labels.get(LABEL_PR)} is closed, gone or no longer wants a preview"
        return None

    def leftovers(self, namespace: str) -> List[Tuple[str, str]]:
        found = []
        for kind in CLUSTER_LEFTOVER_KINDS:
            for obj in self.cluster.list(kind, LABEL_ENV):
                if obj["metadata"]["labels"].get(LABEL_ENV) == namespace:
                    found.append((kind, obj["metadata"]["name"]))
        for pv in self.cluster.list("PersistentVolume"):
            claim = (pv.get("spec") or {}).get("claimRef") or {}
            if claim.get("namespace") == namespace and ("PersistentVolume", pv["metadata"]["name"]) not in found:
                found.append(("PersistentVolume", pv["metadata"]["name"]))
        return sorted(found)

    def orphans(self, live_names: set) -> Dict[str, List[Tuple[str, str]]]:
        start = namespace_stem(self.config)
        result: Dict[str, List[Tuple[str, str]]] = {}
        for kind in CLUSTER_LEFTOVER_KINDS:
            for obj in self.cluster.list(kind, LABEL_ENV):
                owner = obj["metadata"]["labels"].get(LABEL_ENV, "")
                if owner.startswith(start) and owner not in live_names:
                    result.setdefault(owner, []).append((kind, obj["metadata"]["name"]))
        for pv in self.cluster.list("PersistentVolume"):
            owner = ((pv.get("spec") or {}).get("claimRef") or {}).get("namespace", "")
            if owner.startswith(start) and owner not in live_names and ("PersistentVolume", pv["metadata"]["name"]) not in result.get(owner, []):
                result.setdefault(owner, []).append(("PersistentVolume", pv["metadata"]["name"]))
        return result

    def reap(self, pulls: Sequence[PullRequest], allow_empty: bool = False) -> Obj:
        report: Obj = {"now": iso(self.now), "dry_run": self.dry_run, "deleted": [], "kept": [], "orphans": [], "warnings": []}
        open_numbers: Optional[set] = {str(p.number) for p in pulls if wanted(p, self.config, self.now)[0]}
        if not pulls and not allow_empty:
            open_numbers = None
            report["warnings"].append("the pull request list is empty; closed-PR reaping is off (pass --allow-empty if that is really the state)")
        live = self.environments()
        for ns in live:
            name = ns["metadata"]["name"]
            reason = self.reap_reason(ns, open_numbers)
            if reason is None:
                report["kept"].append({"namespace": name, "expires": (ns["metadata"].get("annotations") or {}).get(ANNOTATION_EXPIRES_AT)})
                continue
            leftovers = self.leftovers(name)
            if not self.dry_run:
                for kind, item in leftovers:
                    self.cluster.delete(kind, item)
                if (ns.get("status") or {}).get("phase") != "Terminating":
                    self.cluster.delete("Namespace", name)
            report["deleted"].append({"namespace": name, "reason": reason, "leftovers": [f"{k}/{n}" for k, n in leftovers]})
            self.log(f"{name}: {'would delete' if self.dry_run else 'deleted'} ({reason}); leftovers: {', '.join(f'{k}/{n}' for k, n in leftovers) or 'none'}")
        for owner, items in sorted(self.orphans({ns["metadata"]["name"] for ns in live}).items()):
            if not self.dry_run:
                for kind, item in items:
                    self.cluster.delete(kind, item)
            report["orphans"].append({"namespace": owner, "leftovers": [f"{k}/{n}" for k, n in items]})
        return report


def pod_requests(deployment_obj: Obj) -> Tuple[Fraction, Fraction]:
    spec = deployment_obj.get("spec") or {}
    replicas = int(spec.get("replicas", 1))
    pod = {"spec": (spec.get("template") or {}).get("spec") or {}}
    usage = pod_usage(pod)
    cpu = usage.get("requests.cpu", (Fraction(0), False))[0]
    memory = usage.get("requests.memory", (Fraction(0), True))[0]
    return cpu * replicas, memory * replicas


def money(cpu: Fraction, memory: Fraction, prices: Obj) -> float:
    return float(cpu) * float(prices["cpu_core_hour"]) + float(memory) / 1024 ** 3 * float(prices["memory_gib_hour"])


def cost_report(cluster: Any, reconciler: Reconciler, prices: Obj) -> Obj:
    rows = []
    for ns in reconciler.environments():
        name = ns["metadata"]["name"]
        cpu = memory = Fraction(0)
        for item in cluster.list("Deployment", None, name):
            c, m = pod_requests(item)
            cpu, memory = cpu + c, memory + m
        ceiling = 0.0
        for quota in cluster.list("ResourceQuota", None, name):
            hard = (quota.get("spec") or {}).get("hard") or {}
            ceiling += money(parse_quantity(hard.get("requests.cpu", "0"))[0], parse_quantity(hard.get("requests.memory", "0"))[0], prices)
        labels = ns["metadata"].get("labels") or {}
        hours_left = max(0.0, (int(labels.get(LABEL_EXPIRES, "0")) - reconciler.now.timestamp()) / 3600.0)
        hourly = money(cpu, memory, prices)
        rows.append({"namespace": name, "pr": labels.get(LABEL_PR), "owner": labels.get(LABEL_OWNER), "requests_cpu": format_quantity(cpu),
                     "requests_memory": format_quantity(memory, True), "hourly": round(hourly, 6), "hourly_ceiling": round(ceiling, 6),
                     "hours_left": round(hours_left, 2), "projected": round(hourly * hours_left, 4)})
    return {"currency": prices.get("currency", "USD"), "prices": prices, "environments": rows,
            "total_hourly": round(sum(r["hourly"] for r in rows), 6), "total_projected": round(sum(r["projected"] for r in rows), 4)}


def demo_pod(name: str, containers: List[Obj]) -> Obj:
    return {"apiVersion": "v1", "kind": "Pod", "metadata": {"name": name}, "spec": {"containers": containers}}


def demo() -> Obj:
    events: List[Obj] = []
    note = lambda step, detail: events.append({"step": step, "detail": detail})
    config = Config(repo="shop", prefix="preview")
    pulls = load_pulls(os.path.join(FIXTURES, "pulls.json"))
    now = parse_time("2026-11-11T09:00:00Z")
    cluster = FakeCluster()
    first = Reconciler(cluster, config, now).reconcile(pulls)
    note("reconcile", f"{len(first['environments'])} environment(s), {first['writes']} write(s), skipped {[s['pr'] for s in first['skipped']]}")
    versions = {k: o["metadata"]["resourceVersion"] for k, o in cluster.objects.items()}
    second = Reconciler(cluster, config, now).reconcile(pulls)
    note("reconcile again", f"{second['writes']} write(s); resourceVersions unchanged: {versions == {k: o['metadata']['resourceVersion'] for k, o in cluster.objects.items()}}")
    pushed = copy.deepcopy(pulls)
    pushed[0].head_sha = "f00dfeed" + pushed[0].head_sha[8:]
    pushed[0].updated_at = now
    third = Reconciler(cluster, config, now).reconcile(pushed)
    changed = {k: v for k, v in third["environments"][0]["actions"].items() if v != "unchanged"}
    note("push to #%d" % pushed[0].number, f"changed: {changed}")
    ns = namespace_name(config, pushed[0].number)
    cluster.create_pod(demo_pod("app-1", [{"name": APP, "image": "nginx:1.30-alpine", "resources": {"requests": dict(config.app_requests), "limits": dict(config.app_limits)}}]), ns)
    defaulted = cluster.create_pod(demo_pod("plain", [{"name": "plain", "image": "registry.k8s.io/pause:3.10"}]), ns)
    note("admitted", f"plain got {defaulted['spec']['containers'][0]['resources']} from the LimitRange")
    for pod in (demo_pod("greedy", [{"name": n, "image": "registry.k8s.io/pause:3.10", "resources": {"limits": {"cpu": "600m", "memory": "64Mi"}}} for n in ("a", "b")]),
                demo_pod("huge", [{"name": "huge", "image": "registry.k8s.io/pause:3.10", "resources": {"requests": {"cpu": "100m"}, "limits": {"cpu": "2"}}}])):
        try:
            cluster.create_pod(pod, ns)
        except ClusterError as exc:
            note("rejected", str(exc))
    doomed = namespace_name(config, 103)
    cluster.store({"apiVersion": "rbac.authorization.k8s.io/v1", "kind": "ClusterRoleBinding", "metadata": {"name": f"{doomed}-reader", "labels": {LABEL_ENV: doomed}},
                   "roleRef": {"apiGroup": "rbac.authorization.k8s.io", "kind": "ClusterRole", "name": "view"}, "subjects": []}, "create")
    cluster.store({"apiVersion": "v1", "kind": "PersistentVolume", "metadata": {"name": f"{doomed}-data", "labels": {}},
                   "spec": {"capacity": {"storage": "1Gi"}, "persistentVolumeReclaimPolicy": "Retain", "claimRef": {"namespace": doomed, "name": "data"}}}, "create")
    costs = cost_report(cluster, Reconciler(cluster, config, now), DEFAULT_PRICES)
    note("cost", ", ".join(f"{r['namespace']} {r['hourly']:.4f}/h, {r['projected']:.2f} until expiry" for r in costs["environments"]))
    later = now + timedelta(hours=2)
    reaped = Reconciler(cluster, config, later).reap(pushed)
    note("reap at " + iso(later), f"deleted {[(d['namespace'], d['reason'], d['leftovers']) for d in reaped['deleted']]}")
    closed = [p for p in pushed if p.number != 102]
    reaped_closed = Reconciler(cluster, config, later).reap(closed)
    note("#102 closed", f"deleted {[(d['namespace'], d['reason']) for d in reaped_closed['deleted']]}")
    empty = Reconciler(cluster, config, later).reap([])
    note("empty pull request list", f"deleted {len(empty['deleted'])}; {empty['warnings'][0]}")
    remaining = sorted(ns["metadata"]["name"] for ns in Reconciler(cluster, config, later).environments())
    note("remaining", ", ".join(remaining))
    return {"events": events, "remaining": remaining, "writes": [first["writes"], second["writes"], third["writes"]], "cost": costs,
            "rejections": [e["detail"] for e in events if e["step"] == "rejected"]}


def config_from(args: argparse.Namespace) -> Config:
    config = Config(repo=args.repo, prefix=args.prefix, image_template=args.image_template, domain=args.domain, port=args.port,
                    replicas=args.replicas, ttl_hours=args.ttl_hours, require_label=args.require_label, user_prefix=args.user_prefix,
                    role=args.role, gateway=args.gateway)
    if config.gateway:
        split_gateway(config.gateway)
    if not re.fullmatch(r"[a-z0-9]([-a-z0-9]*[a-z0-9])?", config.prefix) or len(config.prefix) > 40:
        raise InputError(f"--prefix must be a lowercase DNS label of at most 40 characters, got {config.prefix!r}")
    if not 0 < config.port < 65536 or config.replicas < 0 or config.ttl_hours <= 0:
        raise InputError("--port must be 1-65535, --replicas at least 0 and --ttl-hours positive")
    return config


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="minienv", description="Preview environments per pull request: namespace, quota, limits, network policy, author RoleBinding, app, URL, TTL reaper, cost.")
    sub = parser.add_subparsers(dest="command", required=True)

    def common(p: argparse.ArgumentParser) -> None:
        p.add_argument("--repo", default="shop", help="repository name; part of every namespace name")
        p.add_argument("--prefix", default="preview", help="namespace prefix (default: preview)")
        p.add_argument("--image-template", default=Config.image_template, help="placeholders: {repo} {number} {sha} {short_sha} {branch}")
        p.add_argument("--domain", default=Config.domain)
        p.add_argument("--port", type=int, default=8080, help="container port of the app")
        p.add_argument("--replicas", type=int, default=1)
        p.add_argument("--ttl-hours", type=int, default=72, help="lifetime after the last push; a preview-ttl-<N>h label overrides it")
        p.add_argument("--require-label", help="only pull requests with this label get an environment")
        p.add_argument("--user-prefix", default="", help="prefix for the author's Kubernetes user name, e.g. oidc:")
        p.add_argument("--role", default="edit", help="ClusterRole the author is bound to inside the namespace")
        p.add_argument("--gateway", help="NAMESPACE/NAME of a Gateway: adds an HTTPRoute and an ingress exception")
        p.add_argument("--now", help="ISO time to use instead of the clock (tests, drills)")
        p.add_argument("--context", help="kubectl context")
        p.add_argument("--json", action="store_true", help="machine-readable output")

    render = sub.add_parser("render", help="print the objects for one pull request as a JSON List")
    render.add_argument("--prs", required=True, help="JSON array shaped like GitHub's GET /repos/OWNER/REPO/pulls")
    render.add_argument("--number", type=int, required=True)
    common(render)
    reconcile = sub.add_parser("reconcile", help="create or update one environment per open pull request")
    reconcile.add_argument("--prs", required=True)
    reconcile.add_argument("--dry-run", action="store_true")
    common(reconcile)
    reap = sub.add_parser("reap", help="delete expired and closed-PR environments and their cluster-scoped leftovers")
    reap.add_argument("--prs", required=True)
    reap.add_argument("--dry-run", action="store_true")
    reap.add_argument("--allow-empty", action="store_true", help="treat an empty pull request list as 'every PR is closed'")
    common(reap)
    listing = sub.add_parser("list", help="the environments minienv manages for --repo")
    common(listing)
    cost = sub.add_parser("cost", help="hourly and projected cost per environment from requests and a price table")
    cost.add_argument("--prices", help='JSON like {"currency": "USD", "cpu_core_hour": 0.0316, "memory_gib_hour": 0.0042}')
    common(cost)
    admit = sub.add_parser("admit", help="run LimitRange and ResourceQuota admission for a pod, offline")
    admit.add_argument("pod", help="Pod JSON")
    admit.add_argument("--objects", required=True, help="kubectl get limitrange,resourcequota -n NS -o json")
    admit.add_argument("--json", action="store_true")
    show_demo = sub.add_parser("demo", help="the whole lifecycle against an in-memory cluster")
    show_demo.add_argument("--json", action="store_true")
    return parser


def read_json(path: str) -> Any:
    try:
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)
    except OSError as exc:
        raise InputError(f"{path}: {exc.strerror}")
    except json.JSONDecodeError as exc:
        raise InputError(f"{path}: not JSON ({exc})")


def stderr(message: str) -> None:
    print(message, file=sys.stderr)


def format_reconcile(report: Obj) -> str:
    lines = [f"{'(dry run) ' if report['dry_run'] else ''}{len(report['environments'])} environment(s), {report['writes']} write(s) at {report['now']}"]
    for env in report["environments"]:
        changed = [f"{k} {v}" for k, v in env["actions"].items() if v != "unchanged"]
        lines.append(f"  #{env['pr']} {env['namespace']} owner={env['owner']} expires={env['expires']} {env['url']}")
        lines.extend(f"      {line}" for line in changed)
    lines.extend(f"  #{s['pr']} skipped: {s['reason']}" for s in report["skipped"])
    return "\n".join(lines)


def main(argv: Optional[List[str]] = None, runner: Runner = run_command, cluster: Any = None) -> int:
    parser = build_parser()
    try:
        args = parser.parse_args(sys.argv[1:] if argv is None else argv)
    except SystemExit as exit_:
        return int(exit_.code or 0)
    try:
        if args.command == "demo":
            result = demo()
            if args.json:
                print(json.dumps(result, indent=2))
            else:
                print("\n".join(f"{e['step']:<24} {e['detail']}" for e in result["events"]))
            return 0
        if args.command == "admit":
            pod = read_json(args.pod)
            listed = read_json(args.objects)
            objects = listed.get("items", [listed]) if isinstance(listed, dict) else listed
            admitted, problem = admit_pod(pod, objects)
            if args.json:
                print(json.dumps({"allowed": problem is None, "message": problem, "pod": admitted}, indent=2))
            elif problem is None:
                print(json.dumps(admitted, indent=2))
            if problem:
                stderr(f"Error from server (Forbidden): {problem}")
                return 1
            return 0
        config = config_from(args)
        now = parse_time(args.now) if args.now else datetime.now(timezone.utc)
        target = cluster if cluster is not None else Kubectl(args.context, runner)
        log = (lambda _: None) if args.json else stderr
        if args.command == "render":
            pr = next((p for p in load_pulls(args.prs) if p.number == args.number), None)
            if pr is None:
                raise InputError(f"pull request #{args.number} is not in {args.prs}")
            print(json.dumps({"apiVersion": "v1", "kind": "List", "items": desired_objects(pr, config)}, indent=2))
            return 0
        reconciler = Reconciler(target, config, now, dry_run=getattr(args, "dry_run", False), log=log)
        if args.command == "reconcile":
            report = reconciler.reconcile(load_pulls(args.prs))
            print(json.dumps(report, indent=2) if args.json else format_reconcile(report))
            return 0
        if args.command == "reap":
            report = reconciler.reap(load_pulls(args.prs), allow_empty=args.allow_empty)
            for warning in report["warnings"]:
                stderr(f"minienv: warning: {warning}")
            if args.json:
                print(json.dumps(report, indent=2))
            else:
                print(f"{'(dry run) ' if args.dry_run else ''}deleted {len(report['deleted'])}, kept {len(report['kept'])}, orphan sweeps {len(report['orphans'])}")
                for item in report["deleted"]:
                    print(f"  {item['namespace']}: {item['reason']}; leftovers: {', '.join(item['leftovers']) or 'none'}")
            return 0
        if args.command == "list":
            rows = [{"namespace": ns["metadata"]["name"], "pr": ns["metadata"]["labels"].get(LABEL_PR), "owner": ns["metadata"]["labels"].get(LABEL_OWNER),
                     "expires": (ns["metadata"].get("annotations") or {}).get(ANNOTATION_EXPIRES_AT), "url": (ns["metadata"].get("annotations") or {}).get(ANNOTATION_URL),
                     "phase": (ns.get("status") or {}).get("phase")} for ns in reconciler.environments()]
            print(json.dumps(rows, indent=2) if args.json else "\n".join(f"{r['namespace']:<40} #{r['pr']:<5} {r['owner']:<12} {r['expires']} {r['url']}" for r in rows))
            return 0
        if args.command == "cost":
            prices = dict(DEFAULT_PRICES, **(read_json(args.prices) if args.prices else {}))
            report = cost_report(target, reconciler, prices)
            if args.json:
                print(json.dumps(report, indent=2))
            else:
                for row in report["environments"]:
                    print(f"{row['namespace']:<40} {row['requests_cpu']:>6} cpu {row['requests_memory']:>7} {row['hourly']:.4f}/h (ceiling {row['hourly_ceiling']:.4f}/h) {row['projected']:.2f} {report['currency']} until expiry")
                print(f"total {report['total_hourly']:.4f}/h, {report['total_projected']:.2f} {report['currency']} until every environment expires")
            return 0
        raise InputError(f"unknown command {args.command}")
    except MinienvError as err:
        stderr(f"minienv: {err}")
        return err.exit_code
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
