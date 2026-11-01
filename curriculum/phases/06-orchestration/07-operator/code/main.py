"""minioperator: a StaticSite CustomResourceDefinition and the controller behind it. The controller lists then watches StaticSites and the Deployments it owns (through kubectl proxy with urllib, or an in-memory FakeApi), reconciles a ConfigMap, an nginx Deployment and a Service with ownerReferences, writes status through the status subresource, removes an external record behind a finalizer, and requeues 409 conflicts with backoff."""
from __future__ import annotations

import argparse
import collections
import copy
import hashlib
import http.client
import json
import operator as op
import os
import re
import signal
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Deque, Dict, Iterable, Iterator, List, Optional, Sequence, Set, Tuple

GROUP = "learn.cloudcaptain.io"
VERSION = "v1alpha1"
FINALIZER = f"{GROUP}/external-record"
CONTENT_ANNOTATION = f"{GROUP}/content-sha256"
MANAGED_BY = "minioperator"
IMAGE = "nginx:1.29-alpine"
DEFAULT_ACCOUNT = "minioperator"
DEMO_START = datetime(2026, 11, 1, 9, 0, tzinfo=timezone.utc).timestamp()

Obj = Dict[str, Any]
Note = Callable[[str, str, str], None]


@dataclass(frozen=True)
class Kind:
    group: str
    version: str
    kind: str
    plural: str

    @property
    def api_version(self) -> str:
        return f"{self.group}/{self.version}" if self.group else self.version

    @property
    def resource(self) -> str:
        return f"{self.plural}.{self.group}" if self.group else self.plural

    def path(self, namespace: str, name: Optional[str] = None, subresource: Optional[str] = None) -> str:
        root = f"/apis/{self.group}/{self.version}" if self.group else f"/api/{self.version}"
        path = f"{root}/namespaces/{namespace}/{self.plural}"
        if name:
            path += f"/{name}"
            if subresource:
                path += f"/{subresource}"
        return path


SITE = Kind(GROUP, VERSION, "StaticSite", "staticsites")
CONFIGMAP = Kind("", "v1", "ConfigMap", "configmaps")
DEPLOYMENT = Kind("apps", "v1", "Deployment", "deployments")
SERVICE = Kind("", "v1", "Service", "services")
KINDS = {kind.plural: kind for kind in (SITE, CONFIGMAP, DEPLOYMENT, SERVICE)}
GENERATIONAL = {SITE.plural, DEPLOYMENT.plural}
STATUS_SUBRESOURCE = {SITE.plural, DEPLOYMENT.plural}

CONDITION_SCHEMA: Obj = {
    "type": "object",
    "required": ["type", "status"],
    "properties": {
        "type": {"type": "string"},
        "status": {"type": "string", "enum": ["True", "False", "Unknown"]},
        "reason": {"type": "string"},
        "message": {"type": "string"},
        "observedGeneration": {"type": "integer", "format": "int64"},
        "lastTransitionTime": {"type": "string", "format": "date-time"},
    },
}

SITE_SCHEMA: Obj = {
    "type": "object",
    "description": "A static website: nginx serving spec.content as index.html.",
    "required": ["spec"],
    "properties": {
        "apiVersion": {"type": "string"},
        "kind": {"type": "string"},
        "metadata": {"type": "object"},
        "spec": {
            "type": "object",
            "description": "What the site should be. The operator never writes here.",
            "required": ["content"],
            "x-kubernetes-validations": [
                {"rule": "self.tier != 'prod' || self.replicas >= 2", "message": "prod sites need at least 2 replicas"},
            ],
            "properties": {
                "content": {"type": "string", "minLength": 1, "maxLength": 65536, "description": "The index.html nginx serves at /."},
                "replicas": {"type": "integer", "minimum": 1, "maximum": 10, "default": 1, "description": "Number of nginx pods."},
                "tier": {"type": "string", "enum": ["dev", "prod"], "default": "dev", "description": "prod sites need at least 2 replicas."},
            },
        },
        "status": {
            "type": "object",
            "description": "What the operator observed. Written only through the status subresource.",
            "properties": {
                "observedGeneration": {"type": "integer", "format": "int64"},
                "readyReplicas": {"type": "integer"},
                "url": {"type": "string"},
                "conditions": {"type": "array", "x-kubernetes-list-type": "map", "x-kubernetes-list-map-keys": ["type"], "items": CONDITION_SCHEMA},
            },
        },
    },
}

READY_PATH = '.status.conditions[?(@.type=="Ready")]'

CRD: Obj = {
    "apiVersion": "apiextensions.k8s.io/v1",
    "kind": "CustomResourceDefinition",
    "metadata": {"name": SITE.resource},
    "spec": {
        "group": GROUP,
        "scope": "Namespaced",
        "names": {"kind": SITE.kind, "listKind": "StaticSiteList", "plural": SITE.plural, "singular": "staticsite", "shortNames": ["site"]},
        "versions": [
            {
                "name": VERSION,
                "served": True,
                "storage": True,
                "subresources": {"status": {}},
                "additionalPrinterColumns": [
                    {"name": "Replicas", "type": "integer", "jsonPath": ".spec.replicas"},
                    {"name": "Ready", "type": "string", "jsonPath": f"{READY_PATH}.status"},
                    {"name": "Reason", "type": "string", "jsonPath": f"{READY_PATH}.reason"},
                    {"name": "Age", "type": "date", "jsonPath": ".metadata.creationTimestamp"},
                ],
                "schema": {"openAPIV3Schema": SITE_SCHEMA},
            }
        ],
    },
}

RBAC_RULES: List[Obj] = [
    {"apiGroups": [GROUP], "resources": ["staticsites"], "verbs": ["get", "list", "watch", "update"]},
    {"apiGroups": [GROUP], "resources": ["staticsites/status"], "verbs": ["update"]},
    {"apiGroups": [GROUP], "resources": ["staticsites/finalizers"], "verbs": ["update"]},
    {"apiGroups": [""], "resources": ["configmaps", "services"], "verbs": ["get", "create", "update"]},
    {"apiGroups": ["apps"], "resources": ["deployments"], "verbs": ["get", "list", "watch", "create", "update"]},
]


def iso(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class ApiError(Exception):
    def __init__(self, code: int, reason: str, message: str):
        super().__init__(f"{code} {reason}: {message}")
        self.code = code
        self.reason = reason
        self.message = message


class Gone(ApiError):
    def __init__(self, message: str):
        super().__init__(410, "Expired", message)


def status_error(status: Obj) -> ApiError:
    code = int(status.get("code") or 500)
    message = str(status.get("message") or "")
    if code == 410:
        return Gone(message)
    return ApiError(code, str(status.get("reason") or "Unknown"), message)


def conflict(kind: Kind, name: str) -> ApiError:
    return ApiError(409, "Conflict", f'Operation cannot be fulfilled on {kind.resource} "{name}": the object has been modified; please apply your changes to the latest version and try again')


def invalid_message(kind: Kind, name: str, errors: List[str]) -> str:
    detail = errors[0] if len(errors) == 1 else "[" + ", ".join(errors) + "]"
    return f'{kind.kind}.{kind.group} "{name}" is invalid: {detail}'


class CelError(Exception):
    pass


CEL_TOKEN = re.compile(r"\s*(?:(\d+)|('(?:[^'\\]|\\.)*'|\"(?:[^\"\\]|\\.)*\")|([A-Za-z_][A-Za-z0-9_]*)|(\|\||&&|==|!=|<=|>=|[-+*/%<>!?:.,()\[\]]))")
CEL_ESCAPES = {"n": "\n", "t": "\t"}
COMPARE = {"==": op.eq, "!=": op.ne, "<": op.lt, "<=": op.le, ">": op.gt, ">=": op.ge}


def cel_tokens(text: str) -> List[Tuple[str, Any]]:
    tokens: List[Tuple[str, Any]] = []
    position = 0
    while text[position:].strip():
        match = CEL_TOKEN.match(text, position)
        if not match:
            raise CelError(f"unexpected character {text[position:].lstrip()[:1]!r} at offset {position}")
        number, string, ident, symbol = match.groups()
        if number is not None:
            tokens.append(("int", int(number)))
        elif string is not None:
            tokens.append(("str", re.sub(r"\\(.)", lambda m: CEL_ESCAPES.get(m.group(1), m.group(1)), string[1:-1])))
        elif ident is not None:
            tokens.append(("ident", ident))
        else:
            tokens.append(("op", symbol))
        position = match.end()
    tokens.append(("end", None))
    return tokens


class CelParser:
    def __init__(self, text: str):
        self.tokens = cel_tokens(text)
        self.position = 0

    def peek(self) -> Any:
        return self.tokens[self.position][1]

    def take(self, expected: Optional[str] = None) -> Tuple[str, Any]:
        token = self.tokens[self.position]
        if expected is not None and token[1] != expected:
            raise CelError(f"expected {expected!r}, found {token[1] if token[1] is not None else 'end of expression'!r}")
        self.position += 1
        return token

    def parse(self) -> tuple:
        node = self.conditional()
        if self.tokens[self.position][0] != "end":
            raise CelError(f"unexpected {self.peek()!r} after a complete expression")
        return node

    def conditional(self) -> tuple:
        node = self.or_expr()
        if self.peek() == "?":
            self.take("?")
            when_true = self.conditional()
            self.take(":")
            return ("cond", node, when_true, self.conditional())
        return node

    def or_expr(self) -> tuple:
        node = self.and_expr()
        while self.peek() == "||":
            self.take()
            node = ("or", node, self.and_expr())
        return node

    def and_expr(self) -> tuple:
        node = self.relation()
        while self.peek() == "&&":
            self.take()
            node = ("and", node, self.relation())
        return node

    def relation(self) -> tuple:
        node = self.addition()
        while self.peek() in ("==", "!=", "<", "<=", ">", ">=", "in"):
            node = ("bin", self.take()[1], node, self.addition())
        return node

    def addition(self) -> tuple:
        node = self.multiplication()
        while self.peek() in ("+", "-"):
            node = ("bin", self.take()[1], node, self.multiplication())
        return node

    def multiplication(self) -> tuple:
        node = self.unary()
        while self.peek() in ("*", "/", "%"):
            node = ("bin", self.take()[1], node, self.unary())
        return node

    def unary(self) -> tuple:
        if self.peek() == "!":
            self.take()
            return ("not", self.unary())
        if self.peek() == "-":
            self.take()
            return ("neg", self.unary())
        return self.member()

    def member(self) -> tuple:
        node = self.primary()
        while self.peek() in (".", "["):
            if self.take()[1] == "[":
                index = self.conditional()
                self.take("]")
                node = ("index", node, index)
                continue
            kind, name = self.take()
            if kind != "ident":
                raise CelError(f"expected a field name after '.', found {name!r}")
            node = ("call", name, node, self.arguments()) if self.peek() == "(" else ("field", node, name)
        return node

    def primary(self) -> tuple:
        kind, value = self.take()
        if kind in ("int", "str"):
            return ("lit", value)
        if kind == "ident":
            if value in ("true", "false"):
                return ("lit", value == "true")
            if value == "null":
                return ("lit", None)
            if self.peek() != "(":
                return ("ident", value)
            args = self.arguments()
            if value == "has":
                if len(args) != 1 or args[0][0] != "field":
                    raise CelError("has() takes one field selection, such as has(self.tier)")
                return ("has", args[0])
            return ("call", value, None, args)
        if value == "(":
            node = self.conditional()
            self.take(")")
            return node
        if value == "[":
            items = []
            while self.peek() != "]":
                items.append(self.conditional())
                if self.peek() != "]":
                    self.take(",")
            self.take("]")
            return ("list", items)
        raise CelError(f"unexpected {value if value is not None else 'end of expression'!r}")

    def arguments(self) -> List[tuple]:
        self.take("(")
        args: List[tuple] = []
        while self.peek() != ")":
            args.append(self.conditional())
            if self.peek() != ")":
                self.take(",")
        self.take(")")
        return args


def cel_compile(text: str) -> tuple:
    return CelParser(text).parse()


def cel_type(value: Any) -> str:
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, int):
        return "int"
    if isinstance(value, float):
        return "double"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "list"
    if isinstance(value, dict):
        return "map"
    return "null_type"


def cel_bool(value: Any, operator_name: str) -> bool:
    if not isinstance(value, bool):
        raise CelError(f"no matching overload for '{operator_name}' applied to ({cel_type(value)})")
    return value


def cel_binary(name: str, left: Any, right: Any) -> Any:
    left_type, right_type = cel_type(left), cel_type(right)
    if name == "in":
        if right_type in ("list", "map"):
            return any(cel_type(item) == left_type and item == left for item in right)
        raise CelError(f"no matching overload for 'in' applied to ({left_type}, {right_type})")
    if left_type != right_type:
        raise CelError(f"no matching overload for '{name}' applied to ({left_type}, {right_type})")
    if name in ("==", "!="):
        return COMPARE[name](left, right)
    if name in COMPARE and left_type in ("int", "double", "string"):
        return COMPARE[name](left, right)
    if name == "+" and left_type in ("int", "double", "string", "list"):
        return left + right
    if name in ("-", "*") and left_type in ("int", "double"):
        return left - right if name == "-" else left * right
    if name in ("/", "%") and left_type == "int":
        if right == 0:
            raise CelError("division by zero")
        quotient = abs(left) // abs(right) * (1 if (left >= 0) == (right >= 0) else -1)
        return quotient if name == "/" else left - quotient * right
    raise CelError(f"no matching overload for '{name}' applied to ({left_type}, {right_type})")


def cel_call(name: str, target: Any, args: List[Any], has_target: bool) -> Any:
    if name == "size":
        values = ([target] if has_target else []) + args
        if len(values) == 1 and cel_type(values[0]) in ("string", "list", "map"):
            return len(values[0])
        raise CelError("no matching overload for 'size'")
    if not has_target:
        raise CelError(f"undeclared reference to '{name}'")
    if name in ("startsWith", "endsWith", "contains", "matches"):
        if not isinstance(target, str) or len(args) != 1 or not isinstance(args[0], str):
            raise CelError(f"no matching overload for '{name}'")
        if name == "startsWith":
            return target.startswith(args[0])
        if name == "endsWith":
            return target.endswith(args[0])
        if name == "contains":
            return args[0] in target
        return re.search(args[0], target) is not None
    raise CelError(f"undeclared reference to '{name}'")


def cel_eval(node: tuple, env: Dict[str, Any]) -> Any:
    tag = node[0]
    if tag == "lit":
        return node[1]
    if tag == "ident":
        if node[1] not in env:
            raise CelError(f"undeclared reference to '{node[1]}'")
        return env[node[1]]
    if tag == "list":
        return [cel_eval(item, env) for item in node[1]]
    if tag == "field":
        target = cel_eval(node[1], env)
        if not isinstance(target, dict) or node[2] not in target:
            raise CelError(f"no such key: {node[2]}")
        return target[node[2]]
    if tag == "has":
        target = cel_eval(node[1][1], env)
        return isinstance(target, dict) and node[1][2] in target
    if tag == "index":
        target, index = cel_eval(node[1], env), cel_eval(node[2], env)
        try:
            return target[index]
        except (KeyError, IndexError, TypeError):
            raise CelError(f"no such key or index: {index!r}") from None
    if tag in ("and", "or"):
        left = cel_bool(cel_eval(node[1], env), "&&" if tag == "and" else "||")
        if left == (tag == "or"):
            return left
        return cel_bool(cel_eval(node[2], env), "&&" if tag == "and" else "||")
    if tag == "not":
        return not cel_bool(cel_eval(node[1], env), "!")
    if tag == "neg":
        value = cel_eval(node[1], env)
        if cel_type(value) not in ("int", "double"):
            raise CelError(f"no matching overload for '-' applied to ({cel_type(value)})")
        return -value
    if tag == "cond":
        return cel_eval(node[2] if cel_bool(cel_eval(node[1], env), "?:") else node[3], env)
    if tag == "bin":
        return cel_binary(node[1], cel_eval(node[2], env), cel_eval(node[3], env))
    target = None if node[2] is None else cel_eval(node[2], env)
    return cel_call(node[1], target, [cel_eval(arg, env) for arg in node[3]], node[2] is not None)


TYPE_CHECKS: Dict[str, Callable[[Any], bool]] = {
    "object": lambda v: isinstance(v, dict),
    "array": lambda v: isinstance(v, list),
    "string": lambda v: isinstance(v, str),
    "integer": lambda v: isinstance(v, int) and not isinstance(v, bool),
    "number": lambda v: isinstance(v, (int, float)) and not isinstance(v, bool),
    "boolean": lambda v: isinstance(v, bool),
}
ROOT_KEYS = ("apiVersion", "kind", "metadata")


def json_type(value: Any) -> str:
    return {"bool": "boolean", "int": "integer", "double": "number", "string": "string", "list": "array", "map": "object"}.get(cel_type(value), "null")


def show(value: Any) -> str:
    return json.dumps(value) if isinstance(value, str) else str(value)


def join(path: str, key: str) -> str:
    return f"{path}.{key}" if path else key


def check(schema: Obj, value: Any, path: str, errors: List[str], warnings: List[str]) -> Any:
    expected = schema.get("type")
    if expected and not TYPE_CHECKS[expected](value):
        errors.append(f'{path}: Invalid value: "{json_type(value)}": {path} in body must be of type {expected}: "{json_type(value)}"')
        return value
    before = len(errors)
    if expected == "object":
        value = check_object(schema, value, path, errors, warnings)
    elif expected == "array":
        value = [check(schema.get("items") or {}, item, f"{path}[{index}]", errors, warnings) for index, item in enumerate(value)]
    else:
        check_scalar(schema, value, path, errors)
    if len(errors) == before:
        check_rules(schema, value, path, errors)
    return value


def check_object(schema: Obj, value: Obj, path: str, errors: List[str], warnings: List[str]) -> Obj:
    properties = schema.get("properties") or {}
    result: Obj = {}
    for key, item in value.items():
        if not path and key in ROOT_KEYS:
            result[key] = copy.deepcopy(item)
        elif key in properties:
            result[key] = check(properties[key], item, join(path, key), errors, warnings)
        else:
            warnings.append(f'unknown field "{join(path, key)}"')
    for key, child in properties.items():
        if key not in result and "default" in child:
            result[key] = copy.deepcopy(child["default"])
    for key in schema.get("required") or []:
        if key not in result:
            errors.append(f"{join(path, key)}: Required value")
    return result


def check_scalar(schema: Obj, value: Any, path: str, errors: List[str]) -> None:
    if "enum" in schema and value not in schema["enum"]:
        supported = ", ".join(show(item) for item in schema["enum"])
        errors.append(f"{path}: Unsupported value: {show(value)}: supported values: {supported}")
    if isinstance(value, str):
        if len(value) < schema.get("minLength", 0):
            errors.append(f"{path}: Invalid value: {show(value)}: {path} in body should be at least {schema['minLength']} chars long")
        if "maxLength" in schema and len(value) > schema["maxLength"]:
            errors.append(f"{path}: Too long: may not be more than {schema['maxLength']} bytes")
    elif TYPE_CHECKS["number"](value):
        if "minimum" in schema and value < schema["minimum"]:
            errors.append(f"{path}: Invalid value: {show(value)}: {path} in body should be greater than or equal to {schema['minimum']}")
        if "maximum" in schema and value > schema["maximum"]:
            errors.append(f"{path}: Invalid value: {show(value)}: {path} in body should be less than or equal to {schema['maximum']}")


def check_rules(schema: Obj, value: Any, path: str, errors: List[str]) -> None:
    for rule in schema.get("x-kubernetes-validations") or []:
        label = schema.get("type", "object")
        try:
            passed = cel_eval(cel_compile(rule["rule"]), {"self": value})
        except CelError as exc:
            errors.append(f'{path}: Invalid value: "{label}": rule {rule["rule"]!r} could not be evaluated: {exc}')
            continue
        if passed is not True:
            errors.append(f'{path}: Invalid value: "{label}": {rule.get("message") or "failed rule: " + rule["rule"]}')


def admit(obj: Obj, schema: Obj = SITE_SCHEMA) -> Tuple[Obj, List[str], List[str]]:
    errors: List[str] = []
    warnings: List[str] = []
    result = check(schema, obj, "", errors, warnings)
    return result, errors, warnings


def parse_watch_lines(lines: Iterable[Any]) -> Iterator[Obj]:
    for raw in lines:
        text = (raw.decode("utf-8") if isinstance(raw, bytes) else raw).strip()
        if not text:
            continue
        event = json.loads(text)
        if event.get("type") == "ERROR":
            raise status_error(event.get("object") or {})
        yield event


def selects(selector: Optional[str], obj: Obj) -> bool:
    if not selector:
        return True
    labels = (obj.get("metadata") or {}).get("labels") or {}
    for term in selector.split(","):
        key, _, value = term.partition("=")
        if labels.get(key.strip()) != value.strip():
            return False
    return True


def desired_part(obj: Obj) -> Obj:
    return {key: value for key, value in obj.items() if key not in ("apiVersion", "kind", "metadata", "status")}


class FakeApi:
    def __init__(self, clock: Callable[[], float] = time.time):
        self.clock = clock
        self.objects: Dict[Tuple[str, str, str], Obj] = {}
        self.revision = 0
        self.history: List[Tuple[int, str, str, Obj]] = []
        self.compacted = 0
        self.uids = 0
        self.conflicts: Dict[Tuple[str, str], int] = collections.Counter()
        self.collected: List[str] = []
        self.warnings: List[str] = []

    def inject_conflict(self, verb: str, kind: Kind, count: int = 1) -> None:
        self.conflicts[(verb, kind.plural)] += count

    def compact(self) -> None:
        self.compacted = self.revision
        self.history = []

    def stored(self, kind: Kind, namespace: str, name: str) -> Obj:
        found = self.objects.get((kind.plural, namespace, name))
        if found is None:
            raise ApiError(404, "NotFound", f'{kind.resource} "{name}" not found')
        return found

    def commit(self, kind: Kind, obj: Obj, event: str) -> Obj:
        self.revision += 1
        meta = obj["metadata"]
        meta["resourceVersion"] = str(self.revision)
        key = (kind.plural, meta["namespace"], meta["name"])
        if event == "DELETED":
            self.objects.pop(key, None)
        else:
            self.objects[key] = obj
        self.history.append((self.revision, kind.plural, event, copy.deepcopy(obj)))
        return copy.deepcopy(obj)

    def take_conflict(self, verb: str, kind: Kind, name: str) -> None:
        if self.conflicts[(verb, kind.plural)] > 0:
            self.conflicts[(verb, kind.plural)] -= 1
            raise conflict(kind, name)

    def validated(self, kind: Kind, name: str, body: Obj) -> Obj:
        if kind.plural != SITE.plural:
            return body
        result, errors, warnings = admit(body)
        self.warnings.extend(warnings)
        if errors:
            raise ApiError(422, "Invalid", invalid_message(kind, name, errors))
        return result

    def list(self, kind: Kind, namespace: str, label_selector: Optional[str] = None) -> Tuple[List[Obj], str]:
        items = [copy.deepcopy(obj) for key, obj in sorted(self.objects.items(), key=lambda pair: pair[0]) if key[0] == kind.plural and key[1] == namespace and selects(label_selector, obj)]
        return items, str(self.revision)

    def watch(self, kind: Kind, namespace: str, resource_version: str, label_selector: Optional[str] = None, timeout: float = 60.0) -> Iterator[Obj]:
        since = int(resource_version or 0)
        if since < self.compacted:
            status = {"kind": "Status", "apiVersion": "v1", "status": "Failure", "code": 410, "reason": "Expired", "message": f"too old resource version: {since} ({self.compacted})"}
            lines = [json.dumps({"type": "ERROR", "object": status})]
        else:
            lines = [json.dumps({"type": event, "object": obj}) for revision, plural, event, obj in self.history if revision > since and plural == kind.plural and obj["metadata"]["namespace"] == namespace and selects(label_selector, obj)]
        return parse_watch_lines(lines)

    def get(self, kind: Kind, namespace: str, name: str) -> Obj:
        return copy.deepcopy(self.stored(kind, namespace, name))

    def create(self, kind: Kind, namespace: str, obj: Obj) -> Obj:
        body = copy.deepcopy(obj)
        name = (body.get("metadata") or {}).get("name") or ""
        if (kind.plural, namespace, name) in self.objects:
            raise ApiError(409, "AlreadyExists", f'{kind.resource} "{name}" already exists')
        if kind.plural in STATUS_SUBRESOURCE:
            body.pop("status", None)
        body = self.validated(kind, name, body)
        self.uids += 1
        meta = body.setdefault("metadata", {})
        meta.update({"namespace": namespace, "uid": f"uid-{self.uids:04d}", "creationTimestamp": iso(self.clock())})
        if kind.plural in GENERATIONAL:
            meta["generation"] = 1
        body.setdefault("apiVersion", kind.api_version)
        body.setdefault("kind", kind.kind)
        return self.commit(kind, body, "ADDED")

    def update(self, kind: Kind, namespace: str, obj: Obj) -> Obj:
        name = (obj.get("metadata") or {}).get("name") or ""
        current = self.stored(kind, namespace, name)
        if (obj.get("metadata") or {}).get("resourceVersion") != current["metadata"]["resourceVersion"]:
            raise conflict(kind, name)
        self.take_conflict("update", kind, name)
        body = self.validated(kind, name, copy.deepcopy(obj))
        if kind.plural in STATUS_SUBRESOURCE:
            body.pop("status", None)
            if "status" in current:
                body["status"] = copy.deepcopy(current["status"])
        body.setdefault("apiVersion", kind.api_version)
        body.setdefault("kind", kind.kind)
        meta = body.setdefault("metadata", {})
        for key in ("namespace", "uid", "creationTimestamp", "generation", "deletionTimestamp", "deletionGracePeriodSeconds", "resourceVersion"):
            if key in current["metadata"]:
                meta[key] = current["metadata"][key]
            else:
                meta.pop(key, None)
        added = sorted(set(meta.get("finalizers") or []) - set(current["metadata"].get("finalizers") or []))
        if current["metadata"].get("deletionTimestamp") and added:
            raise ApiError(422, "Invalid", invalid_message(kind, name, [f"metadata.finalizers: Forbidden: no new finalizers can be added if the object is being deleted, found new finalizers {added}"]))
        if body == current:
            return copy.deepcopy(current)
        if kind.plural in GENERATIONAL and desired_part(body) != desired_part(current):
            meta["generation"] = int(current["metadata"].get("generation") or 1) + 1
        if meta.get("deletionTimestamp") and not meta.get("finalizers"):
            return self.remove(kind, body)
        return self.commit(kind, body, "MODIFIED")

    def update_status(self, kind: Kind, namespace: str, obj: Obj) -> Obj:
        name = (obj.get("metadata") or {}).get("name") or ""
        current = self.stored(kind, namespace, name)
        if (obj.get("metadata") or {}).get("resourceVersion") != current["metadata"]["resourceVersion"]:
            raise conflict(kind, name)
        self.take_conflict("status", kind, name)
        body = copy.deepcopy(current)
        body.pop("status", None)
        if "status" in obj:
            body["status"] = copy.deepcopy(obj["status"])
        if body == current:
            return copy.deepcopy(current)
        return self.commit(kind, body, "MODIFIED")

    def delete(self, kind: Kind, namespace: str, name: str) -> Obj:
        current = copy.deepcopy(self.stored(kind, namespace, name))
        meta = current["metadata"]
        if not meta.get("finalizers"):
            return self.remove(kind, current)
        if meta.get("deletionTimestamp"):
            return current
        meta["deletionTimestamp"] = iso(self.clock())
        meta["deletionGracePeriodSeconds"] = 0
        if "generation" in meta:
            meta["generation"] += 1
        return self.commit(kind, current, "MODIFIED")

    def remove(self, kind: Kind, body: Obj) -> Obj:
        gone = self.commit(kind, body, "DELETED")
        uid = gone["metadata"]["uid"]
        for (plural, namespace, name), other in list(self.objects.items()):
            owned = any(ref.get("uid") == uid for ref in other["metadata"].get("ownerReferences") or [])
            if owned and (plural, namespace, name) in self.objects:
                self.collected.append(f"{KINDS[plural].kind} {namespace}/{name}")
                self.delete(KINDS[plural], namespace, name)
        return gone

    def settle(self) -> int:
        changed = 0
        for (plural, _, _), obj in list(self.objects.items()):
            if plural != DEPLOYMENT.plural:
                continue
            replicas = int((obj.get("spec") or {}).get("replicas") or 0)
            status = {"observedGeneration": obj["metadata"].get("generation", 1), "replicas": replicas, "updatedReplicas": replicas, "readyReplicas": replicas, "availableReplicas": replicas}
            if obj.get("status") != status:
                body = copy.deepcopy(obj)
                body["status"] = status
                self.commit(DEPLOYMENT, body, "MODIFIED")
                changed += 1
        return changed


def http_error(exc: urllib.error.HTTPError) -> ApiError:
    text = exc.read().decode("utf-8", "replace") if exc.fp else ""
    try:
        status = json.loads(text)
    except ValueError:
        status = None
    if not isinstance(status, dict) or status.get("kind") != "Status":
        status = {"code": exc.code, "reason": str(exc.reason), "message": text.strip() or str(exc.reason)}
    status.setdefault("code", exc.code)
    return status_error(status)


def retry_after(headers: Any, attempt: int) -> float:
    value = headers.get("Retry-After") if headers is not None else None
    try:
        return min(float(value), 10.0)
    except (TypeError, ValueError):
        return min(0.5 * 2 ** attempt, 10.0)


class HttpApi:
    def __init__(self, server: str, opener: Callable[..., Any] = urllib.request.urlopen, timeout: float = 30.0, sleep: Callable[[float], Any] = time.sleep, retries: int = 5):
        self.server = server.rstrip("/")
        self.opener = opener
        self.timeout = timeout
        self.sleep = sleep
        self.retries = retries

    def url(self, path: str, query: Optional[Dict[str, Optional[str]]] = None) -> str:
        pairs = {key: value for key, value in (query or {}).items() if value is not None}
        return self.server + path + ("?" + urllib.parse.urlencode(pairs) if pairs else "")

    def send(self, method: str, path: str, body: Optional[Obj] = None, query: Optional[Dict[str, Optional[str]]] = None, timeout: Optional[float] = None) -> Any:
        data = None if body is None else json.dumps(body).encode("utf-8")
        attempt = 0
        while True:
            request = urllib.request.Request(self.url(path, query), data=data, method=method, headers={"Accept": "application/json", "Content-Type": "application/json"})
            try:
                return self.opener(request, timeout=timeout or self.timeout)
            except urllib.error.HTTPError as exc:
                if exc.code == 429 and attempt < self.retries:
                    self.sleep(retry_after(exc.headers, attempt))
                    attempt += 1
                    continue
                raise http_error(exc) from None
            except urllib.error.URLError as exc:
                raise ApiError(0, "Unreachable", f"{self.server}: {exc.reason}; is kubectl proxy running?") from None

    def call(self, method: str, path: str, body: Optional[Obj] = None, query: Optional[Dict[str, Optional[str]]] = None) -> Obj:
        response = self.send(method, path, body, query)
        try:
            return json.loads(response.read().decode("utf-8") or "{}")
        finally:
            response.close()

    def list(self, kind: Kind, namespace: str, label_selector: Optional[str] = None) -> Tuple[List[Obj], str]:
        doc = self.call("GET", kind.path(namespace), query={"labelSelector": label_selector})
        return list(doc.get("items") or []), str((doc.get("metadata") or {}).get("resourceVersion") or "0")

    def watch(self, kind: Kind, namespace: str, resource_version: str, label_selector: Optional[str] = None, timeout: float = 60.0) -> Iterator[Obj]:
        query = {"watch": "1", "resourceVersion": resource_version, "allowWatchBookmarks": "true", "timeoutSeconds": str(int(timeout)), "labelSelector": label_selector}
        response = self.send("GET", kind.path(namespace), query=query, timeout=timeout + 15)
        try:
            yield from parse_watch_lines(response)
        finally:
            response.close()

    def get(self, kind: Kind, namespace: str, name: str) -> Obj:
        return self.call("GET", kind.path(namespace, name))

    def create(self, kind: Kind, namespace: str, obj: Obj) -> Obj:
        return self.call("POST", kind.path(namespace), obj)

    def update(self, kind: Kind, namespace: str, obj: Obj) -> Obj:
        return self.call("PUT", kind.path(namespace, obj["metadata"]["name"]), obj)

    def update_status(self, kind: Kind, namespace: str, obj: Obj) -> Obj:
        return self.call("PUT", kind.path(namespace, obj["metadata"]["name"], "status"), obj)

    def delete(self, kind: Kind, namespace: str, name: str) -> Obj:
        return self.call("DELETE", kind.path(namespace, name))


class RecordingApi:
    def __init__(self, inner: Any):
        self.inner = inner
        self.calls: Set[Tuple[str, str, str]] = set()

    def seen(self, verb: str, kind: Kind, subresource: str = "") -> None:
        self.calls.add((verb, kind.group, kind.plural + (f"/{subresource}" if subresource else "")))

    def list(self, kind: Kind, namespace: str, label_selector: Optional[str] = None) -> Tuple[List[Obj], str]:
        self.seen("list", kind)
        return self.inner.list(kind, namespace, label_selector)

    def watch(self, kind: Kind, namespace: str, resource_version: str, label_selector: Optional[str] = None, timeout: float = 60.0) -> Iterator[Obj]:
        self.seen("watch", kind)
        return self.inner.watch(kind, namespace, resource_version, label_selector, timeout)

    def get(self, kind: Kind, namespace: str, name: str) -> Obj:
        self.seen("get", kind)
        return self.inner.get(kind, namespace, name)

    def create(self, kind: Kind, namespace: str, obj: Obj) -> Obj:
        self.seen("create", kind)
        for ref in (obj.get("metadata") or {}).get("ownerReferences") or []:
            if ref.get("blockOwnerDeletion") and ref.get("kind") == SITE.kind:
                self.seen("update", SITE, "finalizers")
        return self.inner.create(kind, namespace, obj)

    def update(self, kind: Kind, namespace: str, obj: Obj) -> Obj:
        self.seen("update", kind)
        return self.inner.update(kind, namespace, obj)

    def update_status(self, kind: Kind, namespace: str, obj: Obj) -> Obj:
        self.seen("update", kind, "status")
        return self.inner.update_status(kind, namespace, obj)

    def delete(self, kind: Kind, namespace: str, name: str) -> Obj:
        self.seen("delete", kind)
        return self.inner.delete(kind, namespace, name)


def rbac_allows(rules: List[Obj], verb: str, group: str, resource: str) -> bool:
    def hit(values: List[str], wanted: str) -> bool:
        return wanted in values or "*" in values

    return any(hit(rule["apiGroups"], group) and hit(rule["resources"], resource) and hit(rule["verbs"], verb) for rule in rules)


def rbac_manifest(namespace: str, account: str = DEFAULT_ACCOUNT) -> Obj:
    meta = {"name": account, "namespace": namespace}
    return {
        "apiVersion": "v1",
        "kind": "List",
        "items": [
            {"apiVersion": "v1", "kind": "ServiceAccount", "metadata": dict(meta)},
            {"apiVersion": "rbac.authorization.k8s.io/v1", "kind": "Role", "metadata": dict(meta), "rules": copy.deepcopy(RBAC_RULES)},
            {
                "apiVersion": "rbac.authorization.k8s.io/v1",
                "kind": "RoleBinding",
                "metadata": dict(meta),
                "roleRef": {"apiGroup": "rbac.authorization.k8s.io", "kind": "Role", "name": account},
                "subjects": [{"kind": "ServiceAccount", "name": account, "namespace": namespace}],
            },
        ],
    }


def object_key(obj: Obj) -> str:
    meta = obj.get("metadata") or {}
    return f"{meta.get('namespace', '')}/{meta.get('name', '')}"


def site_keys(obj: Obj) -> List[str]:
    return [object_key(obj)]


def owner_keys(obj: Obj) -> List[str]:
    meta = obj.get("metadata") or {}
    return [f"{meta.get('namespace', '')}/{ref['name']}" for ref in meta.get("ownerReferences") or [] if ref.get("controller") and ref.get("kind") == SITE.kind and ref.get("apiVersion") == SITE.api_version]


class WorkQueue:
    def __init__(self, clock: Callable[[], float] = time.time, base: float = 0.5, cap: float = 30.0):
        self.clock = clock
        self.base = base
        self.cap = cap
        self.due: Dict[str, float] = {}
        self.failures: Dict[str, int] = {}
        self.cond = threading.Condition()

    def add(self, key: str, delay: float = 0.0) -> None:
        with self.cond:
            when = self.clock() + delay
            if key not in self.due or when < self.due[key]:
                self.due[key] = when
            self.cond.notify()

    def retry(self, key: str) -> float:
        with self.cond:
            self.failures[key] = self.failures.get(key, 0) + 1
            delay = min(self.cap, self.base * 2 ** (self.failures[key] - 1))
            self.add(key, delay)
        return delay

    def forget(self, key: str) -> None:
        with self.cond:
            self.failures.pop(key, None)

    def pop(self) -> Optional[str]:
        with self.cond:
            now = self.clock()
            ready = [key for key, when in self.due.items() if when <= now]
            if not ready:
                return None
            key = min(ready, key=lambda item: (self.due[item], item))
            del self.due[key]
            return key

    def wait(self, timeout: float) -> Optional[str]:
        deadline = time.monotonic() + timeout
        with self.cond:
            while True:
                key = self.pop()
                remaining = deadline - time.monotonic()
                if key is not None or remaining <= 0:
                    return key
                soonest = min(self.due.values(), default=None)
                self.cond.wait(remaining if soonest is None else max(0.01, min(remaining, soonest - self.clock())))


class Informer:
    def __init__(self, api: Any, kind: Kind, namespace: str, keys_of: Callable[[Obj], List[str]], enqueue: Callable[[str], None], note: Note, label_selector: Optional[str] = None, watch_timeout: float = 60.0):
        self.api = api
        self.kind = kind
        self.namespace = namespace
        self.keys_of = keys_of
        self.enqueue = enqueue
        self.note = note
        self.label_selector = label_selector
        self.watch_timeout = watch_timeout
        self.cache: Dict[str, Obj] = {}
        self.resource_version = "0"

    def relist(self) -> None:
        items, revision = self.api.list(self.kind, self.namespace, self.label_selector)
        fresh = {object_key(item): item for item in items}
        missed = [self.cache[key] for key in self.cache if key not in fresh]
        self.cache = fresh
        self.resource_version = revision
        for obj in missed + items:
            for key in self.keys_of(obj):
                self.enqueue(key)
        self.note(self.kind.plural, "list", f"{len(items)} item(s) at resourceVersion {revision}")

    def handle(self, event: Obj) -> None:
        obj = event.get("object") or {}
        revision = (obj.get("metadata") or {}).get("resourceVersion")
        if revision:
            self.resource_version = revision
        if event.get("type") == "BOOKMARK":
            return
        if event.get("type") == "DELETED":
            self.cache.pop(object_key(obj), None)
        else:
            self.cache[object_key(obj)] = obj
        for key in self.keys_of(obj):
            self.enqueue(key)

    def poll(self) -> None:
        try:
            for event in self.api.watch(self.kind, self.namespace, self.resource_version, self.label_selector, self.watch_timeout):
                self.handle(event)
        except Gone as exc:
            self.note(self.kind.plural, "watch expired", f"410 Gone ({exc.message}); relisting")
            self.relist()

    def run(self, stop: threading.Event) -> None:
        while not stop.is_set():
            try:
                self.poll()
            except (ApiError, OSError, ValueError, http.client.HTTPException) as exc:
                self.note(self.kind.plural, "watch error", f"{exc}; relisting in 1s")
                if stop.wait(1.0):
                    return
                try:
                    self.relist()
                except (ApiError, OSError, ValueError, http.client.HTTPException) as again:
                    self.note(self.kind.plural, "list error", str(again))


class DirectoryRecords:
    def __init__(self, root: str):
        self.root = Path(root)

    def path(self, namespace: str, name: str) -> Path:
        return self.root / f"{namespace}_{name}.json"

    def publish(self, namespace: str, name: str, record: Obj) -> bool:
        self.root.mkdir(parents=True, exist_ok=True)
        text = json.dumps(record, indent=2, sort_keys=True) + "\n"
        target = self.path(namespace, name)
        if target.exists() and target.read_text(encoding="utf-8") == text:
            return False
        temporary = target.with_suffix(".tmp")
        temporary.write_text(text, encoding="utf-8")
        os.replace(temporary, target)
        return True

    def remove(self, namespace: str, name: str) -> bool:
        try:
            self.path(namespace, name).unlink()
        except FileNotFoundError:
            return False
        return True

    def exists(self, namespace: str, name: str) -> bool:
        return self.path(namespace, name).exists()


class MemoryRecords:
    def __init__(self) -> None:
        self.records: Dict[str, Obj] = {}
        self.failures = 0

    def publish(self, namespace: str, name: str, record: Obj) -> bool:
        key = f"{namespace}/{name}"
        if self.records.get(key) == record:
            return False
        self.records[key] = copy.deepcopy(record)
        return True

    def remove(self, namespace: str, name: str) -> bool:
        if self.failures > 0:
            self.failures -= 1
            raise OSError("external records API unavailable")
        return self.records.pop(f"{namespace}/{name}", None) is not None

    def exists(self, namespace: str, name: str) -> bool:
        return f"{namespace}/{name}" in self.records


def selector_labels(site: Obj) -> Dict[str, str]:
    return {"app.kubernetes.io/name": "staticsite", "app.kubernetes.io/instance": site["metadata"]["name"]}


def site_labels(site: Obj) -> Dict[str, str]:
    return {**selector_labels(site), "app.kubernetes.io/managed-by": MANAGED_BY}


def content_hash(site: Obj) -> str:
    return hashlib.sha256(site["spec"]["content"].encode("utf-8")).hexdigest()[:16]


def site_url(site: Obj) -> str:
    meta = site["metadata"]
    return f"http://{meta['name']}.{meta.get('namespace', 'default')}.svc.cluster.local"


def owner_reference(site: Obj) -> Obj:
    meta = site["metadata"]
    return {"apiVersion": SITE.api_version, "kind": SITE.kind, "name": meta["name"], "uid": meta["uid"], "controller": True, "blockOwnerDeletion": True}


def controlled_by(obj: Obj, site: Obj) -> bool:
    uid = site["metadata"]["uid"]
    return any(ref.get("controller") and ref.get("uid") == uid for ref in obj["metadata"].get("ownerReferences") or [])


def skeleton(kind: Kind, namespace: str, name: str, site: Obj) -> Obj:
    return {"apiVersion": kind.api_version, "kind": kind.kind, "metadata": {"name": name, "namespace": namespace, "ownerReferences": [owner_reference(site)]}}


def mutate_configmap(obj: Obj, site: Obj) -> Obj:
    obj["metadata"].setdefault("labels", {}).update(site_labels(site))
    obj["data"] = {"index.html": site["spec"]["content"]}
    return obj


def mutate_deployment(obj: Obj, site: Obj) -> Obj:
    obj["metadata"].setdefault("labels", {}).update(site_labels(site))
    spec = obj.setdefault("spec", {})
    spec["replicas"] = int(site["spec"].get("replicas", 1))
    spec.setdefault("selector", {"matchLabels": selector_labels(site)})
    template = spec.setdefault("template", {})
    template.setdefault("metadata", {}).setdefault("labels", {}).update(site_labels(site))
    template["metadata"].setdefault("annotations", {})[CONTENT_ANNOTATION] = content_hash(site)
    pod = template.setdefault("spec", {})
    containers = pod.setdefault("containers", [])
    nginx = next((container for container in containers if container.get("name") == "nginx"), None)
    if nginx is None:
        nginx = {"name": "nginx"}
        containers.append(nginx)
    nginx["image"] = IMAGE
    nginx["ports"] = [{"name": "http", "containerPort": 80, "protocol": "TCP"}]
    nginx["readinessProbe"] = {"httpGet": {"path": "/", "port": 80, "scheme": "HTTP"}, "periodSeconds": 5, "timeoutSeconds": 1, "successThreshold": 1, "failureThreshold": 3}
    nginx["volumeMounts"] = [{"name": "content", "mountPath": "/usr/share/nginx/html", "readOnly": True}]
    pod["volumes"] = [{"name": "content", "configMap": {"name": f"{site['metadata']['name']}-content", "defaultMode": 420}}]
    return obj


def mutate_service(obj: Obj, site: Obj) -> Obj:
    obj["metadata"].setdefault("labels", {}).update(site_labels(site))
    spec = obj.setdefault("spec", {})
    spec["selector"] = selector_labels(site)
    spec["ports"] = [{"name": "http", "port": 80, "targetPort": 80, "protocol": "TCP"}]
    return obj


def children(site: Obj) -> List[Tuple[Kind, str, Callable[[Obj, Obj], Obj]]]:
    name = site["metadata"]["name"]
    return [(CONFIGMAP, f"{name}-content", mutate_configmap), (DEPLOYMENT, name, mutate_deployment), (SERVICE, name, mutate_service)]


def external_record(site: Obj) -> Obj:
    meta = site["metadata"]
    return {"site": object_key(site), "uid": meta["uid"], "url": site_url(site), "contentSha256": content_hash(site)}


def ready_condition(site: Obj, deployment: Optional[Obj], problems: List[str]) -> Tuple[str, str, str]:
    desired = int(site["spec"].get("replicas", 1))
    if problems:
        return "False", "OwnershipConflict", "; ".join(problems)
    if deployment is None:
        return "Unknown", "NoDeployment", "the Deployment does not exist yet"
    status = deployment.get("status") or {}
    updated = int(status.get("updatedReplicas") or 0)
    ready = int(status.get("readyReplicas") or 0)
    if int(status.get("observedGeneration") or 0) < int(deployment["metadata"].get("generation") or 1) or updated < desired:
        return "False", "RollingOut", f"{updated}/{desired} pods run the current template"
    if ready < desired:
        return "False", "PodsNotReady", f"{ready}/{desired} pods ready"
    return "True", "Available", f"{ready}/{desired} pods ready at {site_url(site)}"


def desired_status(site: Obj, deployment: Optional[Obj], problems: List[str], now: str) -> Obj:
    generation = int(site["metadata"].get("generation") or 1)
    state, reason, message = ready_condition(site, deployment, problems)
    previous = next((c for c in (site.get("status") or {}).get("conditions") or [] if c.get("type") == "Ready"), None)
    since = previous["lastTransitionTime"] if previous and previous.get("status") == state and previous.get("lastTransitionTime") else now
    return {
        "observedGeneration": generation,
        "readyReplicas": int(((deployment or {}).get("status") or {}).get("readyReplicas") or 0),
        "url": site_url(site),
        "conditions": [{"type": "Ready", "status": state, "reason": reason, "message": message, "observedGeneration": generation, "lastTransitionTime": since}],
    }


class Operator:
    def __init__(self, api: Any, records: Any, namespace: str, clock: Callable[[], float] = time.time, log: Optional[Callable[[str], None]] = None, backoff_base: float = 0.5, backoff_cap: float = 30.0, watch_timeout: float = 60.0):
        self.api = api
        self.records = records
        self.namespace = namespace
        self.clock = clock
        self.log = log or (lambda line: None)
        self.events: Deque[Obj] = collections.deque(maxlen=2000)
        self.queue = WorkQueue(clock, backoff_base, backoff_cap)
        self.sites = Informer(api, SITE, namespace, site_keys, self.queue.add, self.note, None, watch_timeout)
        self.deployments = Informer(api, DEPLOYMENT, namespace, owner_keys, self.queue.add, self.note, f"app.kubernetes.io/managed-by={MANAGED_BY}", watch_timeout)

    def note(self, key: str, action: str, detail: str = "") -> None:
        self.events.append({"time": self.clock(), "key": key, "action": action, "detail": detail})
        self.log(f"{key}: {action}" + (f": {detail}" if detail else ""))

    def reconcile(self, key: str) -> str:
        namespace, name = key.split("/", 1)
        try:
            site = self.api.get(SITE, namespace, name)
        except ApiError as exc:
            if exc.code == 404:
                return "absent"
            raise
        meta = site["metadata"]
        finalizers = list(meta.get("finalizers") or [])
        if meta.get("deletionTimestamp"):
            return self.finalize(key, site, finalizers)
        if FINALIZER not in finalizers:
            meta["finalizers"] = finalizers + [FINALIZER]
            site = self.api.update(SITE, namespace, site)
            self.note(key, "finalizer added", FINALIZER)
        problems: List[str] = []
        deployment: Optional[Obj] = None
        for kind, child, mutate in children(site):
            current, outcome = self.ensure(kind, namespace, child, site, mutate)
            if outcome == "not adopted":
                problems.append(f"{kind.kind} {child} exists and is not controlled by this StaticSite")
            if outcome != "unchanged":
                self.note(key, outcome, f"{kind.kind} {child}")
            if kind == DEPLOYMENT:
                deployment = current
        if not problems and self.records.publish(namespace, name, external_record(site)):
            self.note(key, "external record published", site_url(site))
        status = desired_status(site, deployment, problems, iso(self.clock()))
        if status != site.get("status"):
            site["status"] = status
            self.api.update_status(SITE, namespace, site)
            ready = status["conditions"][0]
            self.note(key, "status", f"Ready={ready['status']} {ready['reason']}, observedGeneration {status['observedGeneration']}")
        return "synced"

    def finalize(self, key: str, site: Obj, finalizers: List[str]) -> str:
        if FINALIZER not in finalizers:
            return "deleting"
        namespace, name = key.split("/", 1)
        removed = self.records.remove(namespace, name)
        self.note(key, "external record deleted" if removed else "external record already gone", site_url(site))
        site["metadata"]["finalizers"] = [item for item in finalizers if item != FINALIZER]
        self.api.update(SITE, namespace, site)
        self.note(key, "finalizer removed", "the API server can delete the object now")
        return "finalized"

    def ensure(self, kind: Kind, namespace: str, name: str, site: Obj, mutate: Callable[[Obj, Obj], Obj]) -> Tuple[Optional[Obj], str]:
        try:
            current = self.api.get(kind, namespace, name)
        except ApiError as exc:
            if exc.code != 404:
                raise
            return self.api.create(kind, namespace, mutate(skeleton(kind, namespace, name, site), site)), "created"
        if not controlled_by(current, site):
            return None, "not adopted"
        wanted = mutate(copy.deepcopy(current), site)
        if wanted == current:
            return current, "unchanged"
        return self.api.update(kind, namespace, wanted), "updated"

    def process(self, key: str) -> Optional[str]:
        try:
            outcome = self.reconcile(key)
        except ApiError as exc:
            delay = self.queue.retry(key)
            self.note(key, "conflict" if exc.code == 409 else "error", f"{exc.message or exc}; requeue in {delay:g}s")
            return None
        except (OSError, ValueError, http.client.HTTPException) as exc:
            delay = self.queue.retry(key)
            self.note(key, "error", f"{exc}; requeue in {delay:g}s")
            return None
        self.queue.forget(key)
        return outcome

    def start(self) -> None:
        self.sites.relist()
        self.deployments.relist()

    def pump(self, limit: int = 200) -> int:
        processed = 0
        while processed < limit:
            self.sites.poll()
            self.deployments.poll()
            key = self.queue.pop()
            if key is None:
                break
            self.process(key)
            processed += 1
        return processed

    def resync(self) -> None:
        for key in list(self.sites.cache):
            self.queue.add(key)

    def serve(self, stop: threading.Event, resync_seconds: float = 30.0) -> None:
        for informer in (self.sites, self.deployments):
            threading.Thread(target=informer.run, args=(stop,), daemon=True).start()
        self.log(f"watching {SITE.plural} and {DEPLOYMENT.plural} in namespace {self.namespace}")
        next_resync = time.monotonic() + resync_seconds
        while not stop.is_set():
            key = self.queue.wait(0.5)
            if key is not None:
                self.process(key)
            if time.monotonic() >= next_resync:
                self.resync()
                next_resync = time.monotonic() + resync_seconds


class FakeClock:
    def __init__(self, start: float):
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def site_manifest(name: str, content: str, replicas: int, tier: str = "dev") -> Obj:
    return {"apiVersion": SITE.api_version, "kind": SITE.kind, "metadata": {"name": name}, "spec": {"content": content, "replicas": replicas, "tier": tier}}


def edit_spec(api: Any, namespace: str, name: str, **changes: Any) -> Obj:
    site = api.get(SITE, namespace, name)
    site["spec"].update(changes)
    return api.update(SITE, namespace, site)


def run_demo() -> Obj:
    clock = FakeClock(DEMO_START)
    api = FakeApi(clock)
    records = MemoryRecords()
    recorder = RecordingApi(api)
    operator = Operator(recorder, records, "demo", clock)
    say = operator.note

    def settle() -> None:
        api.settle()
        operator.pump()

    operator.start()
    say("demo", "step", "kubectl apply: StaticSite demo/hello with 2 replicas")
    api.create(SITE, "demo", site_manifest("hello", "<h1>hello</h1>", 2))
    operator.pump()
    say("demo", "step", "the Deployment controller reports 2/2 ready while another writer races the status update: 409")
    api.inject_conflict("status", SITE)
    settle()
    say("demo", "step", "one second later the backoff has passed and the retry succeeds")
    clock.advance(1.0)
    operator.pump()
    say("demo", "step", "kubectl patch: spec.replicas 2 -> 3, so metadata.generation becomes 2")
    edit_spec(api, "demo", "hello", replicas=3)
    operator.pump()
    settle()
    say("demo", "step", "kubectl scale deployment/hello --replicas=5: drift on an owned object")
    drifted = api.get(DEPLOYMENT, "demo", "hello")
    drifted["spec"]["replicas"] = 5
    api.update(DEPLOYMENT, "demo", drifted)
    operator.pump()
    settle()
    say("demo", "step", "new content: the ConfigMap changes and the pod template hash rolls the pods")
    edit_spec(api, "demo", "hello", content="<h1>hello again</h1>")
    operator.pump()
    settle()
    say("demo", "step", "edits land while the operator is not watching and the history is compacted: 410")
    edit_spec(api, "demo", "hello", replicas=2)
    api.compact()
    operator.pump()
    settle()
    say("demo", "step", "kubectl apply: a prod site with 1 replica")
    try:
        api.create(SITE, "demo", site_manifest("shop", "<h1>shop</h1>", 1, "prod"))
    except ApiError as exc:
        say("demo/shop", "rejected", f"{exc.code} {exc.reason}: {exc.message}")
    say("demo", "step", "kubectl delete staticsite hello while the external records API is down")
    records.failures = 1
    api.delete(SITE, "demo", "hello")
    operator.pump()
    say("demo", "step", "the records API is back and the backoff has passed")
    clock.advance(1.0)
    operator.pump()
    for item in api.collected:
        say("garbage-collector", "garbage collected", item)
    events = []
    for event in operator.events:
        entry = dict(event)
        entry["t"] = round(entry.pop("time") - DEMO_START, 3)
        events.append(entry)
    calls = [{"verb": verb, "group": group, "resource": resource, "allowed": rbac_allows(RBAC_RULES, verb, group, resource)} for verb, group, resource in sorted(recorder.calls)]
    return {
        "events": events,
        "remaining": sorted(f"{KINDS[plural].kind} {namespace}/{name}" for plural, namespace, name in api.objects),
        "records": sorted(records.records),
        "calls": calls,
    }


def render_demo(result: Obj) -> str:
    lines: List[str] = []
    for event in result["events"]:
        if event["action"] == "step":
            lines.append(f"\n== {event['detail']}")
        else:
            lines.append(f"  {event['t']:5.1f}s  {event['key']:<18} {event['action']:<27} {event['detail']}".rstrip())
    lines.append("\n== API calls the operator made, checked against the Role from `rbac`")
    for call in result["calls"]:
        group = call["group"] or "core"
        lines.append(f"  {call['verb']:<7} {group + '/' + call['resource']:<46} {'allowed' if call['allowed'] else 'NOT IN THE ROLE'}")
    lines.append(f"\nleft in the fake API: {', '.join(result['remaining']) or 'nothing'}; external records: {', '.join(result['records']) or 'none'}")
    return "\n".join(lines).lstrip("\n")


def load_json(path: str) -> Any:
    text = sys.stdin.read() if path == "-" else Path(path).read_text(encoding="utf-8")
    return json.loads(text)


def stderr_log(line: str) -> None:
    print(f"[minioperator] {time.strftime('%H:%M:%S')} {line}", file=sys.stderr, flush=True)


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="minioperator", description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("crd", help="print the StaticSite CustomResourceDefinition as JSON for kubectl apply -f")
    rbac = commands.add_parser("rbac", help="print the ServiceAccount, Role and RoleBinding the operator needs")
    rbac.add_argument("--namespace", default="default")
    rbac.add_argument("--service-account", default=DEFAULT_ACCOUNT)
    validate = commands.add_parser("validate", help="default, prune and validate a StaticSite JSON file (or -) the way the API server does")
    validate.add_argument("file")
    validate.add_argument("--json", action="store_true")
    cel = commands.add_parser("cel", help="evaluate an expression in the CEL subset with self bound to --self")
    cel.add_argument("expression")
    cel.add_argument("--self", dest="self_json", default="{}", help="JSON value bound to self")
    demo = commands.add_parser("demo", help="run the operator against the in-memory FakeApi and print the event log")
    demo.add_argument("--json", action="store_true")
    run = commands.add_parser("run", help="run the operator against a real API server through kubectl proxy")
    run.add_argument("--server", required=True, help="kubectl proxy address, for example http://127.0.0.1:8001")
    run.add_argument("--namespace", required=True, help="the namespace whose StaticSites this operator owns")
    run.add_argument("--records-dir", required=True, help="directory standing in for an external API: one JSON record per site")
    run.add_argument("--resync", type=float, default=30.0, help="seconds between full resyncs of every known site")
    run.add_argument("--watch-timeout", type=float, default=60.0, help="timeoutSeconds sent with each watch")
    run.add_argument("--backoff-base", type=float, default=0.5, help="first retry delay after an error; doubles per failure")
    args = parser.parse_args(list(sys.argv[1:] if argv is None else argv))

    if args.command == "crd":
        print(json.dumps(CRD, indent=2))
        return 0
    if args.command == "rbac":
        print(json.dumps(rbac_manifest(args.namespace, args.service_account), indent=2))
        return 0
    if args.command == "validate":
        try:
            obj = load_json(args.file)
        except (OSError, ValueError) as exc:
            print(f"minioperator: cannot read {args.file}: {exc}", file=sys.stderr)
            return 2
        if not isinstance(obj, dict):
            print("minioperator: a StaticSite must be a JSON object", file=sys.stderr)
            return 2
        result, errors, warnings = admit(obj)
        if args.json:
            print(json.dumps({"valid": not errors, "errors": errors, "warnings": warnings, "object": result}, indent=2))
        else:
            for warning in warnings:
                print(f"Warning: {warning}", file=sys.stderr)
            print(invalid_message(SITE, (obj.get("metadata") or {}).get("name", ""), errors) if errors else json.dumps(result, indent=2))
        return 1 if errors else 0
    if args.command == "cel":
        try:
            value = json.loads(args.self_json)
        except ValueError as exc:
            print(f"minioperator: --self is not JSON: {exc}", file=sys.stderr)
            return 2
        try:
            print(json.dumps(cel_eval(cel_compile(args.expression), {"self": value})))
        except CelError as exc:
            print(f"minioperator: {exc}", file=sys.stderr)
            return 1
        return 0
    if args.command == "demo":
        result = run_demo()
        print(json.dumps(result, indent=2) if args.json else render_demo(result))
        return 0 if all(call["allowed"] for call in result["calls"]) else 1
    if args.resync <= 0 or args.watch_timeout <= 0 or args.backoff_base <= 0:
        print("minioperator: --resync, --watch-timeout and --backoff-base must be positive", file=sys.stderr)
        return 2
    operator = Operator(HttpApi(args.server), DirectoryRecords(args.records_dir), args.namespace, log=stderr_log, backoff_base=args.backoff_base, watch_timeout=args.watch_timeout)
    try:
        operator.start()
    except ApiError as exc:
        hint = "; is the CRD installed? python3 code/main.py crd | kubectl apply -f -" if exc.code == 404 else ""
        print(f"minioperator: cannot list {SITE.plural} in {args.namespace}: {exc}{hint}", file=sys.stderr)
        return 1
    stop = threading.Event()
    previous = {sig: signal.signal(sig, lambda *_: stop.set()) for sig in (signal.SIGTERM, signal.SIGINT)}
    try:
        operator.serve(stop, args.resync)
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
