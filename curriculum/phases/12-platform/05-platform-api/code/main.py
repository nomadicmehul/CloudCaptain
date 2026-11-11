from __future__ import annotations

import argparse
import base64
import copy
import difflib
import hashlib
import hmac
import json
import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

GROUP = "platform.cloudcaptain.io"
KIND = "Workload"
VERSIONS = ("v1alpha1", "v1beta1")
HUB = "v1beta1"
PRESERVE_ANNOTATION = f"{GROUP}/v1beta1-spec"
MANAGED_BY = "miniplatform"
DEV_SEED = "miniplatform-dev-seed"

DNS_LABEL = r"^[a-z]([-a-z0-9]*[a-z0-9])?$"
HOSTNAME = r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?(\.[a-z0-9]([-a-z0-9]*[a-z0-9])?)*$"
ENV_NAME = r"^[A-Za-z_][A-Za-z0-9_]*$"
IMAGE_DIGEST = r"^[^@ ]+@sha256:[a-f0-9]{64}$"
IMAGE_TAG = r"^([^/ ]+/)*[^/: ]+:[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}$"

DEFAULT_PLATFORM: Dict[str, Any] = {
    "sizes": {
        "small": {"requests": {"cpu": "100m", "memory": "128Mi"}, "limits": {"memory": "256Mi"}},
        "medium": {"requests": {"cpu": "250m", "memory": "256Mi"}, "limits": {"memory": "512Mi"}},
        "large": {"requests": {"cpu": "500m", "memory": "512Mi"}, "limits": {"memory": "1Gi"}},
    },
    "servicePort": 80,
    "runAsUser": 65532,
    "gateway": {"name": "platform", "namespace": "platform-gateway", "dataplaneNamespace": "envoy-gateway-system"},
    "postgres": {"image": "postgres:17-alpine", "port": 5432, "uid": 70},
}

GUARDED_SEGMENTS = {
    "securityContext",
    "hostNetwork",
    "hostPID",
    "hostIPC",
    "serviceAccountName",
    "automountServiceAccountToken",
    "selector",
}

KIND_ORDER = [
    "ServiceAccount",
    "Secret",
    "Service",
    "StatefulSet",
    "Deployment",
    "HorizontalPodAutoscaler",
    "PodDisruptionBudget",
    "NetworkPolicy",
    "HTTPRoute",
]

API_VERSIONS = {
    "ServiceAccount": "v1",
    "Secret": "v1",
    "Service": "v1",
    "StatefulSet": "apps/v1",
    "Deployment": "apps/v1",
    "HorizontalPodAutoscaler": "autoscaling/v2",
    "PodDisruptionBudget": "policy/v1",
    "NetworkPolicy": "networking.k8s.io/v1",
    "HTTPRoute": "gateway.networking.k8s.io/v1",
}

IMMUTABLE = {
    "Deployment": ("spec.selector",),
    "StatefulSet": ("spec.selector", "spec.serviceName", "spec.volumeClaimTemplates"),
    "PodDisruptionBudget": (),
    "Service": ("spec.clusterIP",),
}


class InputError(Exception):
    pass


@dataclass
class Rule:
    cel: str
    message: str
    check: Callable[[Any], bool]


@dataclass
class Field:
    type: str
    required: Tuple[str, ...] = ()
    props: Dict[str, "Field"] = field(default_factory=dict)
    items: Optional["Field"] = None
    values: Optional["Field"] = None
    enum: Tuple[str, ...] = ()
    minimum: Optional[int] = None
    maximum: Optional[int] = None
    max_length: Optional[int] = None
    max_items: Optional[int] = None
    pattern: Optional[str] = None
    rules: Tuple[Rule, ...] = ()
    list_map_key: Optional[str] = None
    preserve: bool = False


def string(max_length: int = 253, **kwargs: Any) -> Field:
    return Field("string", max_length=max_length, **kwargs)


def strings(max_items: int = 32) -> Field:
    return Field("array", items=string(1024), max_items=max_items)


def has(value: Dict[str, Any], *keys: str) -> bool:
    return all(k in value for k in keys)


IMAGE_RULES = (
    Rule(
        f"self.matches('{IMAGE_DIGEST}') || self.matches('{IMAGE_TAG}')",
        "image needs a tag or a digest",
        lambda v: bool(re.search(IMAGE_DIGEST, v) or re.search(IMAGE_TAG, v)),
    ),
    Rule("!self.endsWith(':latest')", "the latest tag is not allowed; pin a version", lambda v: not v.endswith(":latest")),
)

ENV_RULES = (
    Rule(
        f"self.all(k, k.matches('{ENV_NAME}'))",
        "env names are letters, digits and _ and do not start with a digit",
        lambda v: all(re.search(ENV_NAME, k) for k in v),
    ),
)

PATH_RULES = (
    Rule("self.startsWith('/')", "path must start with /", lambda v: v.startswith("/")),
    Rule("self == '/' || !self.endsWith('/')", "path must not end with / (except /)", lambda v: v == "/" or not v.endswith("/")),
)

SCALING_RULES = (
    Rule(
        "has(self.replicas) != (has(self.min) || has(self.max))",
        "set either replicas or min and max, not both",
        lambda v: ("replicas" in v) != ("min" in v or "max" in v),
    ),
    Rule(
        "has(self.min) == has(self.max)",
        "min and max go together",
        lambda v: ("min" in v) == ("max" in v),
    ),
    Rule(
        "!has(self.min) || !has(self.max) || self.min <= self.max",
        "min must not be greater than max",
        lambda v: not has(v, "min", "max") or v["min"] <= v["max"],
    ),
    Rule(
        "!has(self.targetCPUUtilization) || has(self.max)",
        "targetCPUUtilization needs min and max",
        lambda v: "targetCPUUtilization" not in v or "max" in v,
    ),
)

NAME_RULE = Rule(
    f"self.metadata.name.size() <= 40 && self.metadata.name.matches('{DNS_LABEL}')",
    "name must be a DNS label of at most 40 characters",
    lambda v: len(v) <= 40 and bool(re.search(DNS_LABEL, v)),
)

SIZE = string(16, enum=("small", "medium", "large"))
PORT = Field("integer", minimum=1, maximum=65535)
ENV_MAP = Field("map", values=string(4096), max_items=64, rules=ENV_RULES)
ENV_LIST = Field(
    "array",
    items=Field("object", required=("name",), props={"name": string(128, pattern=ENV_NAME), "value": string(4096)}),
    max_items=64,
    list_map_key="name",
)

SPEC_V1ALPHA1 = Field(
    "object",
    required=("team", "image", "port"),
    props={
        "team": string(63, pattern=DNS_LABEL),
        "image": string(512, rules=IMAGE_RULES),
        "command": strings(),
        "args": strings(),
        "port": PORT,
        "replicas": Field("integer", minimum=1, maximum=50),
        "size": SIZE,
        "healthPath": string(256, rules=PATH_RULES[:1]),
        "env": ENV_MAP,
        "public": Field("object", required=("host",), props={"host": string(253, pattern=HOSTNAME), "path": string(256, rules=PATH_RULES)}),
        "database": string(16, enum=("postgres",)),
        "calls": Field("array", items=string(40, pattern=DNS_LABEL), max_items=16),
    },
)

SPEC_V1BETA1 = Field(
    "object",
    required=("owner", "container"),
    props={
        "owner": string(63, pattern=DNS_LABEL),
        "container": Field(
            "object",
            required=("image", "port"),
            props={
                "image": string(512, rules=IMAGE_RULES),
                "command": strings(),
                "args": strings(),
                "port": PORT,
                "healthPath": string(256, rules=PATH_RULES[:1]),
                "env": ENV_LIST,
            },
        ),
        "size": SIZE,
        "scaling": Field(
            "object",
            props={
                "replicas": Field("integer", minimum=1, maximum=50),
                "min": Field("integer", minimum=1, maximum=50),
                "max": Field("integer", minimum=1, maximum=50),
                "targetCPUUtilization": Field("integer", minimum=10, maximum=95),
            },
            rules=SCALING_RULES,
        ),
        "route": Field("object", required=("host",), props={"host": string(253, pattern=HOSTNAME), "path": string(256, rules=PATH_RULES)}),
        "dependencies": Field(
            "array",
            items=Field(
                "object",
                required=("name", "type"),
                props={"name": string(20, pattern=DNS_LABEL), "type": string(16, enum=("postgres", "workload"))},
            ),
            max_items=16,
            list_map_key="name",
        ),
        "overrides": Field(
            "array",
            items=Field(
                "object",
                required=("kind", "patch"),
                props={"kind": string(32, enum=tuple(k for k in KIND_ORDER if k not in ("Secret", "StatefulSet"))), "patch": Field("object", preserve=True)},
            ),
            max_items=16,
        ),
    },
)

SCHEMAS = {"v1alpha1": SPEC_V1ALPHA1, "v1beta1": SPEC_V1BETA1}

TYPE_CHECKS: Dict[str, Callable[[Any], bool]] = {
    "string": lambda v: isinstance(v, str),
    "integer": lambda v: isinstance(v, int) and not isinstance(v, bool),
    "object": lambda v: isinstance(v, dict),
    "map": lambda v: isinstance(v, dict),
    "array": lambda v: isinstance(v, list),
}


def show(value: Any) -> str:
    return json.dumps(value)[:60]


def check(schema: Field, value: Any, path: str, errors: List[str]) -> None:
    start = len(errors)
    if not TYPE_CHECKS[schema.type](value):
        wanted = "object" if schema.type == "map" else schema.type
        errors.append(f"{path}: Invalid value: {show(value)}: must be of type {wanted}")
        return
    if schema.type == "string":
        if schema.enum and value not in schema.enum:
            supported = ", ".join(json.dumps(e) for e in schema.enum)
            errors.append(f"{path}: Unsupported value: {json.dumps(value)}: supported values: {supported}")
        if schema.max_length is not None and len(value) > schema.max_length:
            errors.append(f"{path}: Too long: may not be more than {schema.max_length} bytes")
        if schema.pattern and not re.search(schema.pattern, value):
            errors.append(f"{path}: Invalid value: {json.dumps(value)}: should match '{schema.pattern}'")
    if schema.type == "integer":
        if schema.minimum is not None and value < schema.minimum:
            errors.append(f"{path}: Invalid value: {value}: should be greater than or equal to {schema.minimum}")
        if schema.maximum is not None and value > schema.maximum:
            errors.append(f"{path}: Invalid value: {value}: should be less than or equal to {schema.maximum}")
    if schema.type == "object" and not schema.preserve:
        for key in schema.required:
            if key not in value:
                errors.append(f"{path}.{key}: Required value")
        for key, child in value.items():
            if key not in schema.props:
                hint = difflib.get_close_matches(key, list(schema.props), n=1)
                suffix = f" (did you mean {hint[0]!r}?)" if hint else ""
                errors.append(f"{path}.{key}: unknown field{suffix}")
            else:
                check(schema.props[key], child, f"{path}.{key}", errors)
    if schema.type == "map":
        if schema.max_items is not None and len(value) > schema.max_items:
            errors.append(f"{path}: Too many: {len(value)}: must have at most {schema.max_items} items")
        for key, child in value.items():
            check(schema.values, child, f"{path}.{key}", errors)
    if schema.type == "array":
        if schema.max_items is not None and len(value) > schema.max_items:
            errors.append(f"{path}: Too many: {len(value)}: must have at most {schema.max_items} items")
        seen: Dict[Any, int] = {}
        for index, child in enumerate(value):
            check(schema.items, child, f"{path}[{index}]", errors)
            if schema.list_map_key and isinstance(child, dict) and schema.list_map_key in child:
                key = child[schema.list_map_key]
                if key in seen:
                    errors.append(f"{path}[{index}]: Duplicate value: {schema.list_map_key}={json.dumps(key)}")
                seen[key] = index
    if len(errors) > start:
        return
    for rule in schema.rules:
        if not rule.check(value):
            errors.append(f"{path}: Invalid value: {rule.message}")


def openapi(schema: Field) -> Dict[str, Any]:
    out: Dict[str, Any] = {"type": "object" if schema.type == "map" else schema.type}
    if schema.enum:
        out["enum"] = list(schema.enum)
    if schema.max_length is not None:
        out["maxLength"] = schema.max_length
    if schema.pattern:
        out["pattern"] = schema.pattern
    if schema.minimum is not None:
        out["minimum"] = schema.minimum
    if schema.maximum is not None:
        out["maximum"] = schema.maximum
    if schema.type == "object":
        if schema.preserve:
            out["x-kubernetes-preserve-unknown-fields"] = True
        else:
            out["properties"] = {k: openapi(v) for k, v in schema.props.items()}
        if schema.required:
            out["required"] = list(schema.required)
    if schema.type == "map":
        out["additionalProperties"] = openapi(schema.values)
        out["maxProperties"] = schema.max_items
    if schema.type == "array":
        out["items"] = openapi(schema.items)
        out["maxItems"] = schema.max_items
        if schema.list_map_key:
            out["x-kubernetes-list-type"] = "map"
            out["x-kubernetes-list-map-keys"] = [schema.list_map_key]
    if schema.rules:
        out["x-kubernetes-validations"] = [{"rule": r.cel, "message": r.message} for r in schema.rules]
    return out


def crd() -> Dict[str, Any]:
    root = {
        "type": "object",
        "required": ["spec"],
        "properties": {
            "apiVersion": {"type": "string"},
            "kind": {"type": "string"},
            "metadata": {"type": "object"},
            "spec": openapi(SPEC_V1BETA1),
        },
        "x-kubernetes-validations": [{"rule": NAME_RULE.cel, "message": NAME_RULE.message}],
    }
    return {
        "apiVersion": "apiextensions.k8s.io/v1",
        "kind": "CustomResourceDefinition",
        "metadata": {"name": f"workloads.{GROUP}", "labels": {"app.kubernetes.io/managed-by": MANAGED_BY}},
        "spec": {
            "group": GROUP,
            "scope": "Namespaced",
            "names": {"kind": KIND, "plural": "workloads", "singular": "workload", "shortNames": ["wl"]},
            "versions": [{"name": HUB, "served": True, "storage": True, "schema": {"openAPIV3Schema": root}}],
        },
    }


@dataclass
class Dependency:
    name: str
    type: str


@dataclass
class Workload:
    name: str
    owner: str
    image: str
    port: int
    command: List[str] = field(default_factory=list)
    args: List[str] = field(default_factory=list)
    env: Dict[str, str] = field(default_factory=dict)
    health_path: str = "/healthz"
    size: str = "small"
    replicas: Optional[int] = None
    autoscale: Optional[Tuple[int, int, int]] = None
    route: Optional[Tuple[str, str]] = None
    dependencies: List[Dependency] = field(default_factory=list)
    overrides: List[Dict[str, Any]] = field(default_factory=list)

    @property
    def min_replicas(self) -> int:
        return self.autoscale[0] if self.autoscale else (self.replicas or 1)


def version_of(obj: Dict[str, Any]) -> str:
    api_version = obj.get("apiVersion", "")
    group, _, version = api_version.rpartition("/")
    if obj.get("kind") != KIND or group != GROUP:
        raise InputError(f"{obj.get('kind')} {api_version!r}: expected kind {KIND} in group {GROUP}")
    if version not in VERSIONS:
        raise InputError(f"apiVersion: unsupported version {version!r}; served versions: {', '.join(VERSIONS)}")
    return version


def alpha_to_beta(obj: Dict[str, Any]) -> Dict[str, Any]:
    spec = obj.get("spec", {})
    meta = copy.deepcopy(obj.get("metadata", {}))
    annotations = meta.get("annotations") or {}
    preserved = annotations.pop(PRESERVE_ANNOTATION, None)
    if not annotations:
        meta.pop("annotations", None)
    container = {k: spec[k] for k in ("image", "command", "args", "port", "healthPath") if k in spec}
    if "env" in spec:
        container["env"] = [{"name": k, "value": v} for k, v in sorted(spec["env"].items())]
    out: Dict[str, Any] = {"owner": spec.get("team"), "container": container}
    if "size" in spec:
        out["size"] = spec["size"]
    out["scaling"] = {"replicas": spec.get("replicas", 1)}
    if "public" in spec:
        out["route"] = dict(spec["public"])
    dependencies = []
    if "database" in spec:
        dependencies.append({"name": "db", "type": spec["database"]})
    dependencies += [{"name": name, "type": "workload"} for name in spec.get("calls", [])]
    if dependencies:
        out["dependencies"] = dependencies
    if preserved:
        for key, value in json.loads(preserved).items():
            if value is None:
                out.pop(key, None)
            else:
                out[key] = value
    return {"apiVersion": f"{GROUP}/v1beta1", "kind": KIND, "metadata": meta, "spec": out}


def beta_to_alpha(obj: Dict[str, Any]) -> Dict[str, Any]:
    spec = obj.get("spec", {})
    meta = copy.deepcopy(obj.get("metadata", {}))
    container = spec.get("container", {})
    out: Dict[str, Any] = {"team": spec.get("owner")}
    out.update({k: container[k] for k in ("image", "command", "args", "port", "healthPath") if k in container})
    if "env" in container:
        out["env"] = {e["name"]: e.get("value", "") for e in container["env"]}
    if "size" in spec:
        out["size"] = spec["size"]
    scaling = spec.get("scaling", {})
    out["replicas"] = scaling.get("replicas", scaling.get("min", 1))
    if "route" in spec:
        out["public"] = dict(spec["route"])
    dependencies = spec.get("dependencies", [])
    databases = [d for d in dependencies if d["type"] != "workload"]
    if databases:
        out["database"] = databases[0]["type"]
    calls = [d["name"] for d in dependencies if d["type"] == "workload"]
    if calls:
        out["calls"] = calls
    rebuilt = alpha_to_beta({"apiVersion": f"{GROUP}/v1alpha1", "kind": KIND, "metadata": {}, "spec": out})["spec"]
    defaults: Dict[str, Any] = {"scaling": {"replicas": 1}}
    lost = {key: spec.get(key) for key in ("scaling", "dependencies", "overrides") if spec.get(key, defaults.get(key)) != rebuilt.get(key)}
    if lost:
        meta.setdefault("annotations", {})[PRESERVE_ANNOTATION] = json.dumps(lost, sort_keys=True, separators=(",", ":"))
    return {"apiVersion": f"{GROUP}/v1alpha1", "kind": KIND, "metadata": meta, "spec": out}


def convert(obj: Dict[str, Any], to: str) -> Dict[str, Any]:
    if to not in VERSIONS:
        raise InputError(f"--to: unsupported version {to!r}; served versions: {', '.join(VERSIONS)}")
    current = version_of(obj)
    if current == to:
        return copy.deepcopy(obj)
    return alpha_to_beta(obj) if to == "v1beta1" else beta_to_alpha(obj)


def env_prefix(name: str) -> str:
    return name.upper().replace("-", "_")


def dependency_env_names(dep: Dependency) -> List[str]:
    prefix = env_prefix(dep.name)
    if dep.type == "workload":
        return [f"{prefix}_URL"]
    return [f"{prefix}_{suffix}" for suffix in ("HOST", "PORT", "NAME", "USER", "PASSWORD")]


def rendered_kinds(workload: Workload) -> List[str]:
    kinds = ["ServiceAccount", "Service", "Deployment", "NetworkPolicy"]
    if workload.autoscale:
        kinds.append("HorizontalPodAutoscaler")
    if workload.min_replicas >= 2:
        kinds.append("PodDisruptionBudget")
    if workload.route:
        kinds.append("HTTPRoute")
    return kinds


def guarded_paths(patch: Any, prefix: str = "") -> List[str]:
    found = []
    if isinstance(patch, dict):
        for key, value in patch.items():
            path = f"{prefix}.{key}" if prefix else key
            if key in GUARDED_SEGMENTS:
                found.append(path)
            else:
                found += guarded_paths(value, path)
    return found


def semantic_errors(workload: Workload) -> List[str]:
    errors = []
    for dep in workload.dependencies:
        for name in dependency_env_names(dep):
            if name in workload.env:
                errors.append(f"spec.container.env: {name} is set by dependency {dep.name}; remove it from env")
    kinds = rendered_kinds(workload)
    for index, override in enumerate(workload.overrides):
        where = f"spec.overrides[{index}]"
        if override["kind"] not in kinds:
            errors.append(f"{where}.kind: this workload renders no {override['kind']}; it renders {', '.join(sorted(kinds))}")
        for path in guarded_paths(override["patch"]):
            errors.append(f"{where}.patch.{path}: guarded by the platform; ask the platform team instead of overriding it")
    return errors


def to_workload(beta: Dict[str, Any]) -> Workload:
    spec = beta["spec"]
    container = spec["container"]
    scaling = spec.get("scaling") or {"replicas": 1}
    route = spec.get("route")
    return Workload(
        name=beta["metadata"]["name"],
        owner=spec["owner"],
        image=container["image"],
        port=container["port"],
        command=list(container.get("command", [])),
        args=list(container.get("args", [])),
        env={e["name"]: e.get("value", "") for e in container.get("env", [])},
        health_path=container.get("healthPath", "/healthz"),
        size=spec.get("size", "small"),
        replicas=scaling.get("replicas"),
        autoscale=(scaling["min"], scaling["max"], scaling.get("targetCPUUtilization", 70)) if "min" in scaling else None,
        route=(route["host"], route.get("path", "/")) if route else None,
        dependencies=[Dependency(d["name"], d["type"]) for d in spec.get("dependencies", [])],
        overrides=list(spec.get("overrides", [])),
    )


@dataclass
class Checked:
    source: str
    name: str
    version: str
    errors: List[str]
    workload: Optional[Workload] = None


def validate(obj: Dict[str, Any], source: str = "-") -> Checked:
    name = (obj.get("metadata") or {}).get("name", "")
    try:
        version = version_of(obj)
    except InputError as err:
        return Checked(source, name, "?", [str(err)])
    errors: List[str] = []
    if not isinstance(obj.get("metadata"), dict) or "name" not in obj["metadata"]:
        errors.append("metadata.name: Required value")
    elif not NAME_RULE.check(name):
        errors.append(f"metadata.name: Invalid value: {NAME_RULE.message}")
    if "spec" not in obj:
        errors.append("spec: Required value")
    else:
        check(SCHEMAS[version], obj["spec"], "spec", errors)
    if errors:
        return Checked(source, name, version, errors)
    workload = to_workload(convert(obj, HUB))
    return Checked(source, name, version, semantic_errors(workload), workload)


def flatten_input(data: Any, source: str) -> List[Dict[str, Any]]:
    if isinstance(data, list):
        return [item for entry in data for item in flatten_input(entry, source)]
    if isinstance(data, dict) and data.get("kind") == "List":
        return flatten_input(data.get("items", []), source)
    if isinstance(data, dict):
        return [data]
    raise InputError(f"{source}: expected a JSON object, a JSON array or a List")


def load(paths: Sequence[str]) -> List[Tuple[str, Dict[str, Any]]]:
    out = []
    for path in paths:
        try:
            text = sys.stdin.read() if path == "-" else Path(path).read_text(encoding="utf-8")
        except OSError as err:
            raise InputError(f"{path}: {err.strerror}") from None
        try:
            data = json.loads(text)
        except json.JSONDecodeError as err:
            raise InputError(f"{path}: not valid JSON: {err.msg} at line {err.lineno} column {err.colno}") from None
        out += [(path, obj) for obj in flatten_input(data, path)]
    return out


def deep_merge(base: Dict[str, Any], extra: Dict[str, Any]) -> Dict[str, Any]:
    out = copy.deepcopy(base)
    for key, value in extra.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = deep_merge(out[key], value)
        else:
            out[key] = copy.deepcopy(value)
    return out


def merge_patch(target: Any, patch: Any) -> Any:
    if not isinstance(patch, dict):
        return copy.deepcopy(patch)
    out = copy.deepcopy(target) if isinstance(target, dict) else {}
    for key, value in patch.items():
        if value is None:
            out.pop(key, None)
        else:
            out[key] = merge_patch(out.get(key), value)
    return out


def labels_for(workload: Workload, component: Optional[str] = None) -> Dict[str, str]:
    name = f"{workload.name}-{component}" if component else workload.name
    labels = {
        "app.kubernetes.io/name": name,
        "app.kubernetes.io/managed-by": MANAGED_BY,
        f"{GROUP}/owner": workload.owner,
        f"{GROUP}/workload": workload.name,
    }
    if component:
        labels["app.kubernetes.io/component"] = "database"
    return labels


def manifest(kind: str, name: str, labels: Dict[str, str], namespace: str, body: Dict[str, Any]) -> Dict[str, Any]:
    meta: Dict[str, Any] = {"name": name, "labels": dict(labels)}
    if namespace:
        meta["namespace"] = namespace
    obj = {"apiVersion": API_VERSIONS[kind], "kind": kind, "metadata": meta}
    obj.update(body)
    return obj


def password_for(seed: str, namespace: str, workload: str, dependency: str) -> str:
    digest = hmac.new(seed.encode(), f"{namespace}/{workload}/{dependency}".encode(), hashlib.sha256).digest()
    return base64.urlsafe_b64encode(digest).decode()[:24]


def pod_security(uid: int) -> Dict[str, Any]:
    return {"runAsNonRoot": True, "runAsUser": uid, "runAsGroup": uid, "fsGroup": uid, "seccompProfile": {"type": "RuntimeDefault"}}


def container_security(read_only: bool) -> Dict[str, Any]:
    return {"allowPrivilegeEscalation": False, "readOnlyRootFilesystem": read_only, "capabilities": {"drop": ["ALL"]}}


def dependency_env(workload: Workload, dep: Dependency, platform: Dict[str, Any]) -> List[Dict[str, Any]]:
    prefix = env_prefix(dep.name)
    if dep.type == "workload":
        return [{"name": f"{prefix}_URL", "value": f"http://{dep.name}:{platform['servicePort']}"}]
    service = f"{workload.name}-{dep.name}"
    return [
        {"name": f"{prefix}_HOST", "value": service},
        {"name": f"{prefix}_PORT", "value": str(platform["postgres"]["port"])},
        {"name": f"{prefix}_NAME", "value": workload.name},
        {"name": f"{prefix}_USER", "value": workload.name},
        {"name": f"{prefix}_PASSWORD", "valueFrom": {"secretKeyRef": {"name": service, "key": "password"}}},
    ]


def render_workload(
    workload: Workload, callers: Sequence[str], platform: Dict[str, Any], namespace: str, seed: str
) -> List[Dict[str, Any]]:
    labels = labels_for(workload)
    selector = {"app.kubernetes.io/name": workload.name}
    port = platform["servicePort"]
    objects = [manifest("ServiceAccount", workload.name, labels, namespace, {"automountServiceAccountToken": False})]
    env = [{"name": k, "value": v} for k, v in sorted(workload.env.items())]
    for dep in workload.dependencies:
        env += dependency_env(workload, dep, platform)
    container: Dict[str, Any] = {"name": "main", "image": workload.image}
    if workload.command:
        container["command"] = list(workload.command)
    if workload.args:
        container["args"] = list(workload.args)
    probe = {"httpGet": {"path": workload.health_path, "port": "http"}}
    container.update(
        {
            "ports": [{"name": "http", "containerPort": workload.port, "protocol": "TCP"}],
            "env": env,
            "resources": copy.deepcopy(platform["sizes"][workload.size]),
            "readinessProbe": dict(probe, periodSeconds=5),
            "livenessProbe": dict(probe, periodSeconds=10, failureThreshold=3),
            "securityContext": container_security(True),
            "volumeMounts": [{"name": "tmp", "mountPath": "/tmp"}],
        }
    )
    if not env:
        del container["env"]
    deployment_spec: Dict[str, Any] = {}
    if workload.replicas is not None:
        deployment_spec["replicas"] = workload.replicas
    deployment_spec.update(
        {
            "selector": {"matchLabels": dict(selector)},
            "template": {
                "metadata": {"labels": dict(labels)},
                "spec": {
                    "serviceAccountName": workload.name,
                    "automountServiceAccountToken": False,
                    "securityContext": pod_security(platform["runAsUser"]),
                    "containers": [container],
                    "volumes": [{"name": "tmp", "emptyDir": {}}],
                },
            },
        }
    )
    objects.append(manifest("Deployment", workload.name, labels, namespace, {"spec": deployment_spec}))
    objects.append(
        manifest(
            "Service",
            workload.name,
            labels,
            namespace,
            {"spec": {"selector": dict(selector), "ports": [{"name": "http", "port": port, "targetPort": workload.port, "protocol": "TCP"}]}},
        )
    )
    if workload.autoscale:
        low, high, cpu = workload.autoscale
        objects.append(
            manifest(
                "HorizontalPodAutoscaler",
                workload.name,
                labels,
                namespace,
                {
                    "spec": {
                        "scaleTargetRef": {"apiVersion": "apps/v1", "kind": "Deployment", "name": workload.name},
                        "minReplicas": low,
                        "maxReplicas": high,
                        "metrics": [{"type": "Resource", "resource": {"name": "cpu", "target": {"type": "Utilization", "averageUtilization": cpu}}}],
                    }
                },
            )
        )
    if workload.min_replicas >= 2:
        objects.append(
            manifest("PodDisruptionBudget", workload.name, labels, namespace, {"spec": {"maxUnavailable": 1, "selector": {"matchLabels": dict(selector)}}})
        )
    ingress = []
    if workload.route:
        ingress.append(
            {
                "from": [{"namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": platform["gateway"]["dataplaneNamespace"]}}}],
                "ports": [{"port": workload.port, "protocol": "TCP"}],
            }
        )
    if callers:
        ingress.append(
            {
                "from": [{"podSelector": {"matchLabels": {"app.kubernetes.io/name": caller}}} for caller in sorted(callers)],
                "ports": [{"port": workload.port, "protocol": "TCP"}],
            }
        )
    policy: Dict[str, Any] = {"podSelector": {"matchLabels": dict(selector)}, "policyTypes": ["Ingress"]}
    if ingress:
        policy["ingress"] = ingress
    objects.append(manifest("NetworkPolicy", workload.name, labels, namespace, {"spec": policy}))
    if workload.route:
        host, path = workload.route
        gateway = platform["gateway"]
        parent = {"name": gateway["name"]}
        if gateway.get("namespace"):
            parent["namespace"] = gateway["namespace"]
        objects.append(
            manifest(
                "HTTPRoute",
                workload.name,
                labels,
                namespace,
                {
                    "spec": {
                        "parentRefs": [parent],
                        "hostnames": [host],
                        "rules": [{"matches": [{"path": {"type": "PathPrefix", "value": path}}], "backendRefs": [{"name": workload.name, "port": port}]}],
                    }
                },
            )
        )
    for dep in workload.dependencies:
        if dep.type == "postgres":
            objects += render_postgres(workload, dep, platform, namespace, seed)
    for override in workload.overrides:
        for index, obj in enumerate(objects):
            if obj["kind"] == override["kind"] and obj["metadata"]["name"] == workload.name:
                objects[index] = merge_patch(obj, override["patch"])
    return objects


def render_postgres(workload: Workload, dep: Dependency, platform: Dict[str, Any], namespace: str, seed: str) -> List[Dict[str, Any]]:
    pg = platform["postgres"]
    name = f"{workload.name}-{dep.name}"
    labels = labels_for(workload, dep.name)
    selector = {"app.kubernetes.io/name": name}
    password = password_for(seed, namespace, workload.name, dep.name)
    container = {
        "name": "postgres",
        "image": pg["image"],
        "env": [
            {"name": "PGDATA", "value": "/var/lib/postgresql/data/pgdata"},
            {"name": "POSTGRES_USER", "value": workload.name},
            {"name": "POSTGRES_DB", "value": workload.name},
            {"name": "POSTGRES_PASSWORD", "valueFrom": {"secretKeyRef": {"name": name, "key": "password"}}},
        ],
        "ports": [{"name": "postgres", "containerPort": pg["port"], "protocol": "TCP"}],
        "readinessProbe": {"exec": {"command": ["pg_isready", "-U", workload.name, "-d", workload.name]}, "periodSeconds": 3},
        "resources": copy.deepcopy(platform["sizes"]["small"]),
        "securityContext": container_security(False),
        "volumeMounts": [{"name": "data", "mountPath": "/var/lib/postgresql/data"}],
    }
    return [
        manifest("Secret", name, labels, namespace, {"type": "Opaque", "data": {"password": base64.b64encode(password.encode()).decode()}}),
        manifest(
            "Service",
            name,
            labels,
            namespace,
            {"spec": {"selector": dict(selector), "ports": [{"name": "postgres", "port": pg["port"], "targetPort": pg["port"], "protocol": "TCP"}]}},
        ),
        manifest(
            "StatefulSet",
            name,
            labels,
            namespace,
            {
                "spec": {
                    "replicas": 1,
                    "serviceName": name,
                    "selector": {"matchLabels": dict(selector)},
                    "template": {
                        "metadata": {"labels": dict(labels)},
                        "spec": {
                            "automountServiceAccountToken": False,
                            "securityContext": pod_security(pg["uid"]),
                            "containers": [container],
                            "volumes": [{"name": "data", "emptyDir": {}}],
                        },
                    },
                }
            },
        ),
        manifest(
            "NetworkPolicy",
            name,
            labels,
            namespace,
            {
                "spec": {
                    "podSelector": {"matchLabels": dict(selector)},
                    "policyTypes": ["Ingress"],
                    "ingress": [
                        {
                            "from": [{"podSelector": {"matchLabels": {"app.kubernetes.io/name": workload.name}}}],
                            "ports": [{"port": pg["port"], "protocol": "TCP"}],
                        }
                    ],
                }
            },
        ),
    ]


@dataclass
class Rendered:
    objects: List[Dict[str, Any]]
    warnings: List[str]


def callers_of(workloads: Sequence[Workload]) -> Dict[str, List[str]]:
    callers: Dict[str, List[str]] = {}
    for workload in workloads:
        for dep in workload.dependencies:
            if dep.type == "workload":
                callers.setdefault(dep.name, []).append(workload.name)
    return callers


def sort_key(obj: Dict[str, Any]) -> Tuple[int, str]:
    return KIND_ORDER.index(obj["kind"]), obj["metadata"]["name"]


def render(workloads: Sequence[Workload], platform: Dict[str, Any], namespace: str = "", seed: str = DEV_SEED) -> Rendered:
    names = {w.name for w in workloads}
    if len(names) != len(workloads):
        raise InputError("two inputs define the same Workload name")
    warnings = []
    callers = callers_of(workloads)
    objects: List[Dict[str, Any]] = []
    for workload in workloads:
        for dep in workload.dependencies:
            if dep.type == "workload" and dep.name not in names:
                warnings.append(f"{workload.name}: dependency {dep.name} is not among the inputs; its NetworkPolicy will not admit {workload.name} until both are rendered together")
            if dep.type == "postgres" and seed == DEV_SEED:
                warnings.append(f"{workload.name}: dependency {dep.name} uses the development password seed; set MINIPLATFORM_SEED")
        objects += render_workload(workload, callers.get(workload.name, []), platform, namespace, seed)
    return Rendered(sorted(objects, key=sort_key), warnings)


def flatten_paths(value: Any, prefix: str = "") -> Dict[str, Any]:
    if isinstance(value, dict) and value:
        out: Dict[str, Any] = {}
        for key, child in value.items():
            out.update(flatten_paths(child, f"{prefix}.{key}" if prefix else key))
        return out
    if isinstance(value, list) and value:
        out = {}
        for index, child in enumerate(value):
            out.update(flatten_paths(child, f"{prefix}[{index}]"))
        return out
    return {prefix: value}


@dataclass
class Change:
    action: str
    kind: str
    name: str
    fields: List[Tuple[str, Any, Any]] = field(default_factory=list)
    restarts: bool = False
    recreate: List[str] = field(default_factory=list)

    @property
    def key(self) -> str:
        return f"{self.kind}/{self.name}"


MISSING = "(absent)"


def diff_objects(old: Sequence[Dict[str, Any]], new: Sequence[Dict[str, Any]]) -> Tuple[List[Change], List[str]]:
    def index(objects: Sequence[Dict[str, Any]]) -> Dict[Tuple[str, str], Dict[str, Any]]:
        return {(o["kind"], o["metadata"]["name"]): o for o in objects}

    before, after = index(old), index(new)
    changes: List[Change] = []
    for key in sorted(set(before) | set(after), key=lambda k: (KIND_ORDER.index(k[0]), k[1])):
        kind, name = key
        if key not in before:
            changes.append(Change("add", kind, name))
            continue
        if key not in after:
            changes.append(Change("remove", kind, name))
            continue
        a, b = flatten_paths(before[key]), flatten_paths(after[key])
        fields = [(p, a.get(p, MISSING), b.get(p, MISSING)) for p in sorted(set(a) | set(b)) if a.get(p, MISSING) != b.get(p, MISSING)]
        if not fields:
            continue
        change = Change("change", kind, name, fields)
        change.restarts = kind in ("Deployment", "StatefulSet") and any(p.startswith("spec.template.") for p, _, _ in fields)
        change.recreate = sorted({imm for p, _, _ in fields for imm in IMMUTABLE.get(kind, ()) if p == imm or p.startswith(imm + ".") or p.startswith(imm + "[")})
        changes.append(change)
    warnings = []
    added = {(c.kind, c.name) for c in changes if c.action == "add"}
    for change in changes:
        if change.kind == "Deployment" and change.action == "change":
            for path, was, now in change.fields:
                if path == "spec.replicas" and now == MISSING and ("HorizontalPodAutoscaler", change.name) in added:
                    warnings.append(
                        f"{change.key}: spec.replicas leaves the manifest while an HPA arrives; applying it resets the Deployment to 1 replica "
                        f"(from {was}) until the HPA reacts. Hand the field over first (kubectl apply edit-last-applied, or the server-side apply ownership transfer)"
                    )
        for path in change.recreate:
            warnings.append(f"{change.key}: {path} is immutable; the apply fails until the object is deleted and recreated")
    return changes, warnings


def to_yaml(value: Any, indent: int = 0) -> str:
    pad = " " * indent
    if isinstance(value, dict) and value:
        lines = []
        for key, child in value.items():
            if isinstance(child, (dict, list)) and child:
                lines.append(f"{pad}{scalar(key)}:")
                lines.append(to_yaml(child, indent + 2 if isinstance(child, dict) else indent))
            else:
                lines.append(f"{pad}{scalar(key)}: {scalar(child)}")
        return "\n".join(lines)
    if isinstance(value, list) and value:
        lines = []
        for child in value:
            if isinstance(child, (dict, list)) and child:
                nested = to_yaml(child, indent + 2)
                lines.append(f"{pad}- {nested[indent + 2:]}")
            else:
                lines.append(f"{pad}- {scalar(child)}")
        return "\n".join(lines)
    return pad + scalar(value)


PLAIN = re.compile(r"^[A-Za-z_/][A-Za-z0-9_./-]*$")
RESERVED = {"true", "false", "yes", "no", "on", "off", "null", "y", "n", "~"}


def scalar(value: Any) -> str:
    if isinstance(value, dict):
        return "{}"
    if isinstance(value, list):
        return "[]"
    if isinstance(value, str) and PLAIN.match(value) and value.lower() not in RESERVED:
        return value
    return json.dumps(value)


def to_score(workload: Workload, platform: Dict[str, Any]) -> Dict[str, Any]:
    variables = dict(sorted(workload.env.items()))
    resources: Dict[str, Any] = {}
    for dep in workload.dependencies:
        prefix = env_prefix(dep.name)
        if dep.type == "workload":
            resources[dep.name] = {"type": "service-port", "params": {"workload": dep.name, "port": "http"}}
            variables[f"{prefix}_URL"] = f"http://${{resources.{dep.name}.hostname}}:${{resources.{dep.name}.port}}"
        else:
            resources[dep.name] = {"type": dep.type}
            for suffix, output in (("HOST", "host"), ("PORT", "port"), ("NAME", "name"), ("USER", "username"), ("PASSWORD", "password")):
                variables[f"{prefix}_{suffix}"] = f"${{resources.{dep.name}.{output}}}"
    if workload.route:
        host, path = workload.route
        resources["route"] = {"type": "route", "params": {"host": host, "path": path, "port": platform["servicePort"]}}
    probe = {"httpGet": {"path": workload.health_path, "port": workload.port}}
    container: Dict[str, Any] = {"image": workload.image}
    if workload.command:
        container["command"] = list(workload.command)
    if workload.args:
        container["args"] = list(workload.args)
    if variables:
        container["variables"] = variables
    container.update({"resources": copy.deepcopy(platform["sizes"][workload.size]), "readinessProbe": probe, "livenessProbe": copy.deepcopy(probe)})
    score: Dict[str, Any] = {
        "apiVersion": "score.dev/v1b1",
        "metadata": {"name": workload.name},
        "containers": {"main": container},
        "service": {"ports": {"http": {"port": platform["servicePort"], "targetPort": workload.port}}},
    }
    if resources:
        score["resources"] = resources
    return score


def emit(objects: Sequence[Dict[str, Any]], output: str) -> str:
    if output == "json":
        return json.dumps({"apiVersion": "v1", "kind": "List", "items": list(objects)}, indent=2)
    return "\n".join(f"---\n{to_yaml(obj)}" for obj in objects)


def checked_workloads(paths: Sequence[str]) -> Tuple[List[Workload], List[Checked]]:
    results = [validate(obj, source) for source, obj in load(paths)]
    return [r.workload for r in results if r.workload and not r.errors], [r for r in results if r.errors]


def report_invalid(bad: Sequence[Checked]) -> None:
    for result in bad:
        for error in result.errors:
            print(f"error: {result.source}: {result.name or '?'}: {error}", file=sys.stderr)


def platform_from(args: argparse.Namespace) -> Dict[str, Any]:
    platform = copy.deepcopy(DEFAULT_PLATFORM)
    if args.platform:
        try:
            platform = deep_merge(platform, json.loads(Path(args.platform).read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError) as err:
            raise InputError(f"--platform {args.platform}: {err}") from None
    if args.gateway:
        namespace, _, name = args.gateway.rpartition("/")
        platform["gateway"] = dict(platform["gateway"], name=name, namespace=namespace)
    return platform


def summary(objects: Sequence[Dict[str, Any]]) -> str:
    lines = [f"{'KIND':<24} {'NAME':<20} WORKLOAD"]
    for obj in objects:
        labels = obj["metadata"].get("labels", {})
        lines.append(f"{obj['kind']:<24} {obj['metadata']['name']:<20} {labels.get(GROUP + '/workload', '')}")
    lines.append(f"{len(objects)} objects")
    return "\n".join(lines)


def cmd_validate(args: argparse.Namespace) -> int:
    results = [validate(obj, source) for source, obj in load(args.files)]
    if args.json:
        print(json.dumps({"results": [{"source": r.source, "name": r.name, "version": r.version, "valid": not r.errors, "errors": r.errors} for r in results]}, indent=2))
    else:
        for r in results:
            print(f"{r.source}: {r.name or '?'} ({r.version}): {'valid' if not r.errors else 'invalid'}")
            for error in r.errors:
                print(f"  {error}")
    return 1 if any(r.errors for r in results) else 0


def cmd_convert(args: argparse.Namespace) -> int:
    converted = []
    for source, obj in load(args.files):
        result = validate(obj, source)
        if result.errors:
            report_invalid([result])
            return 1
        converted.append(convert(obj, args.to))
    print(json.dumps(converted[0] if len(converted) == 1 else {"apiVersion": "v1", "kind": "List", "items": converted}, indent=2))
    return 0


def render_files(paths: Sequence[str], args: argparse.Namespace) -> Optional[Rendered]:
    workloads, bad = checked_workloads(paths)
    if bad:
        report_invalid(bad)
        return None
    seed = os.environ.get("MINIPLATFORM_SEED", DEV_SEED)
    return render(workloads, platform_from(args), args.namespace, seed)


def cmd_render(args: argparse.Namespace) -> int:
    rendered = render_files(args.files, args)
    if rendered is None:
        return 1
    for warning in rendered.warnings:
        print(f"warning: {warning}", file=sys.stderr)
    print(summary(rendered.objects) if args.summary else emit(rendered.objects, args.output))
    return 0


def cmd_diff(args: argparse.Namespace) -> int:
    old, new = render_files([args.old], args), render_files([args.new], args)
    if old is None or new is None:
        return 2
    changes, warnings = diff_objects(old.objects, new.objects)
    if args.json:
        print(json.dumps({"changes": [{"action": c.action, "kind": c.kind, "name": c.name, "restarts": c.restarts, "recreate": c.recreate, "fields": [{"path": p, "old": a, "new": b} for p, a, b in c.fields]} for c in changes], "warnings": warnings}, indent=2))
    else:
        symbols = {"add": "+", "remove": "-", "change": "~"}
        for c in changes:
            note = "   [restarts pods]" if c.restarts else ""
            print(f"{symbols[c.action]} {c.key}{note}")
            for path, was, now in c.fields:
                print(f"    {path}: {json.dumps(was) if was is not MISSING else MISSING} -> {json.dumps(now) if now is not MISSING else MISSING}")
        for warning in warnings:
            print(f"warning: {warning}")
        counts = {a: sum(1 for c in changes if c.action == a) for a in ("add", "remove", "change")}
        print(f"{counts['add']} added, {counts['remove']} removed, {counts['change']} changed")
    return 1 if changes else 0


def cmd_to_score(args: argparse.Namespace) -> int:
    workloads, bad = checked_workloads(args.files)
    if bad:
        report_invalid(bad)
        return 1
    platform = platform_from(args)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    for workload in workloads:
        target = out / f"{workload.name}.score.yaml"
        target.write_text(json.dumps(to_score(workload, platform), indent=2) + "\n", encoding="utf-8")
        print(target)
    return 0


def cmd_crd(args: argparse.Namespace) -> int:
    print(json.dumps(crd(), indent=2))
    return 0


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="miniplatform", description="Expand a developer-facing Workload into the Kubernetes objects a platform runs.")
    sub = p.add_subparsers(dest="command", required=True)

    def platform_flags(s: argparse.ArgumentParser) -> None:
        s.add_argument("--platform", help="JSON file merged over the built-in platform defaults")
        s.add_argument("--gateway", help="NAMESPACE/NAME of the Gateway routes attach to")
        s.add_argument("--namespace", default="", help="metadata.namespace for every generated object")

    s = sub.add_parser("validate", help="check Workloads against their version's schema and rules")
    s.add_argument("files", nargs="+")
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_validate)
    s = sub.add_parser("convert", help="convert a Workload between v1alpha1 and v1beta1")
    s.add_argument("files", nargs="+")
    s.add_argument("--to", required=True, choices=VERSIONS)
    s.set_defaults(func=cmd_convert)
    s = sub.add_parser("render", help="print the Kubernetes objects for one or more Workloads")
    s.add_argument("files", nargs="+")
    s.add_argument("-o", "--output", choices=("yaml", "json"), default="yaml")
    s.add_argument("--summary", action="store_true")
    platform_flags(s)
    s.set_defaults(func=cmd_render)
    s = sub.add_parser("diff", help="show what a Workload change does to the generated objects (exit 1 when they differ)")
    s.add_argument("old")
    s.add_argument("new")
    s.add_argument("--json", action="store_true")
    platform_flags(s)
    s.set_defaults(func=cmd_diff)
    s = sub.add_parser("to-score", help="write the equivalent Score file for each Workload")
    s.add_argument("files", nargs="+")
    s.add_argument("--out", required=True)
    platform_flags(s)
    s.set_defaults(func=cmd_to_score)
    s = sub.add_parser("crd", help="print the Workload CustomResourceDefinition with its CEL rules")
    s.set_defaults(func=cmd_crd)
    return p


def main(argv: Optional[List[str]] = None) -> int:
    args = parser().parse_args(sys.argv[1:] if argv is None else argv)
    try:
        return args.func(args)
    except InputError as err:
        print(f"error: {err}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
