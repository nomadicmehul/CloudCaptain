"""minigitops: a GitOps reconciler. It polls a branch of a Git repository, renders an application path (plain manifests or a Kustomize-style overlay), compares the result with the live objects, syncs with server-side apply in hook phases and sync waves, prunes by tracking label, assesses health per kind and self-heals drift."""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import math
import os
import posixpath
import re
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from fractions import Fraction
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Set, Tuple

Obj = Dict[str, Any]
Path = Tuple[str, ...]
Runner = Callable[[List[str], Optional[str]], Tuple[int, str, str]]

FIELD_MANAGER = "minigitops"
TRACKING_LABEL = "app.kubernetes.io/instance"
WAVE = "argocd.argoproj.io/sync-wave"
HOOK = "argocd.argoproj.io/hook"
HOOK_DELETE = "argocd.argoproj.io/hook-delete-policy"
SYNC_OPTIONS = "argocd.argoproj.io/sync-options"
HOOK_TYPES = ("PreSync", "Sync", "PostSync", "SyncFail", "Skip")
DELETE_POLICIES = ("BeforeHookCreation", "HookSucceeded", "HookFailed")
HEALTH_ORDER = ("Healthy", "Suspended", "Progressing", "Missing", "Degraded", "Unknown")
KIND_ORDER = (
    "Namespace", "NetworkPolicy", "ResourceQuota", "LimitRange", "PodDisruptionBudget", "ServiceAccount", "Secret",
    "ConfigMap", "StorageClass", "PersistentVolume", "PersistentVolumeClaim", "CustomResourceDefinition", "ClusterRole",
    "ClusterRoleBinding", "Role", "RoleBinding", "Service", "DaemonSet", "Pod", "ReplicationController", "ReplicaSet",
    "Deployment", "HorizontalPodAutoscaler", "StatefulSet", "Job", "CronJob", "IngressClass", "Ingress", "APIService",
)
CLUSTER_SCOPED = {
    "Namespace", "ClusterRole", "ClusterRoleBinding", "CustomResourceDefinition", "PersistentVolume", "StorageClass",
    "PriorityClass", "IngressClass", "APIService", "ValidatingWebhookConfiguration", "MutatingWebhookConfiguration",
}
PRUNE_KINDS = (
    "configmap", "secret", "service", "serviceaccount", "persistentvolumeclaim", "deployment.apps", "statefulset.apps",
    "daemonset.apps", "job.batch", "cronjob.batch", "ingress.networking.k8s.io", "role.rbac.authorization.k8s.io",
    "rolebinding.rbac.authorization.k8s.io",
)
KUSTOMIZATION_FILES = ("kustomization.yaml", "kustomization.yml", "Kustomization")
KUSTOMIZATION_FIELDS = ("apiVersion", "kind", "resources", "namespace", "namePrefix", "nameSuffix", "labels", "commonLabels", "images", "patches")
SERVER_FIELDS = ("uid", "resourceVersion", "generation", "creationTimestamp", "managedFields", "selfLink", "deletionTimestamp", "deletionGracePeriodSeconds")
SERVER_ANNOTATIONS = ("kubectl.kubernetes.io/last-applied-configuration", "deployment.kubernetes.io/revision")
WORKLOADS = ("Deployment", "StatefulSet", "DaemonSet", "ReplicaSet", "Job")
CONTAINER_LISTS = ("containers", "initContainers", "ephemeralContainers")
LIST_KEYS = {
    "containers": ("name",), "initContainers": ("name",), "ephemeralContainers": ("name",), "env": ("name",),
    "volumes": ("name",), "volumeMounts": ("mountPath",), "imagePullSecrets": ("name",), "hostAliases": ("ip",),
}
REFERENCES = (("configMapRef", "ConfigMap"), ("secretRef", "Secret"), ("configMapKeyRef", "ConfigMap"), ("secretKeyRef", "Secret"))
QUANTITY_RE = re.compile(r"^([+-]?(?:[0-9]+\.?[0-9]*|\.[0-9]+))([eE][+-]?[0-9]+|Ki|Mi|Gi|Ti|Pi|Ei|n|u|m|k|M|G|T|P|E)?$")
QUANTITY_SUFFIX = {
    "": 1, "n": Fraction(1, 10 ** 9), "u": Fraction(1, 10 ** 6), "m": Fraction(1, 1000), "k": 10 ** 3, "M": 10 ** 6,
    "G": 10 ** 9, "T": 10 ** 12, "P": 10 ** 15, "E": 10 ** 18, "Ki": 2 ** 10, "Mi": 2 ** 20, "Gi": 2 ** 30,
    "Ti": 2 ** 40, "Pi": 2 ** 50, "Ei": 2 ** 60,
}
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
FIXTURE_APP = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "app")
MISSING = object()


class GitOpsError(Exception):
    exit_code = 1


class InputError(GitOpsError):
    exit_code = 2


class YamlError(InputError):
    def __init__(self, line: int, message: str) -> None:
        super().__init__(f"line {line}: {message}")


class GitError(GitOpsError):
    pass


class ClusterError(GitOpsError):
    pass


class Conflict(ClusterError):
    pass


class SyncFailure(GitOpsError):
    pass


NULL_WORDS = {"", "~", "null", "Null", "NULL"}
TRUE_WORDS = {"true", "True", "TRUE"}
FALSE_WORDS = {"false", "False", "FALSE"}
INT_FORMS = ((re.compile(r"^[-+]?[0-9]+$"), 10, ""), (re.compile(r"^0x[0-9a-fA-F]+$"), 16, "0x"), (re.compile(r"^0o[0-7]+$"), 8, "0o"))
FLOAT_RE = re.compile(r"^[-+]?(\.[0-9]+|[0-9]+(\.[0-9]*)?)([eE][-+]?[0-9]+)?$")
SPECIAL_FLOATS = {".inf": math.inf, "+.inf": math.inf, "-.inf": -math.inf, ".Inf": math.inf, "-.Inf": -math.inf, ".nan": math.nan, ".NaN": math.nan}
YAML11_WORDS = {"y", "Y", "yes", "Yes", "YES", "n", "N", "no", "No", "NO", "on", "On", "ON", "off", "Off", "OFF"}
BLOCK_HEADER_RE = re.compile(r"^([|>])([-+]?)([1-9]?)([-+]?)$")
DOC_SEPARATOR = re.compile(r"(?m)^---[ \t]*(?:#.*)?$")
ESCAPES = {"n": "\n", "t": "\t", "r": "\r", '"': '"', "\\": "\\", "/": "/", "0": "\0", " ": " "}


@dataclass
class Line:
    number: int
    indent: int
    text: str


def plain_scalar(text: str) -> Any:
    if text in NULL_WORDS:
        return None
    if text in TRUE_WORDS:
        return True
    if text in FALSE_WORDS:
        return False
    for pattern, base, prefix in INT_FORMS:
        if pattern.match(text):
            sign = -1 if text.startswith("-") else 1
            return sign * int(text.lstrip("+-")[len(prefix):], base)
    if FLOAT_RE.match(text):
        return float(text)
    if text in SPECIAL_FLOATS:
        return SPECIAL_FLOATS[text]
    return text


def key_string(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    return "null" if value is None else str(value)


def strip_comment(text: str) -> str:
    quote = None
    index = 0
    while index < len(text):
        char = text[index]
        if quote:
            if char == "\\" and quote == '"':
                index += 2
                continue
            if char == quote:
                quote = None
        elif char in "\"'" and (index == 0 or text[index - 1] in " \t:[{,-"):
            quote = char
        elif char == "#" and (index == 0 or text[index - 1] in " \t"):
            return text[:index].rstrip()
        index += 1
    return text.rstrip()


def split_lines(source: str) -> List[Line]:
    lines: List[Line] = []
    for number, raw in enumerate(source.splitlines(), start=1):
        if raw.strip() and "\t" in raw[: len(raw) - len(raw.lstrip())]:
            raise YamlError(number, "tabs are not allowed for indentation; use spaces")
        text = strip_comment(raw)
        if not text.strip() or text.strip() in ("---", "..."):
            continue
        lines.append(Line(number, len(text) - len(text.lstrip(" ")), text.strip()))
    return lines


def parse_double_quoted(text: str, number: int) -> Tuple[str, str]:
    out: List[str] = []
    index = 1
    while index < len(text):
        char = text[index]
        if char == "\\":
            code = text[index + 1 : index + 2]
            if code in ("x", "u", "U"):
                width = {"x": 2, "u": 4, "U": 8}[code]
                out.append(chr(int(text[index + 2 : index + 2 + width], 16)))
                index += 2 + width
                continue
            out.append(ESCAPES.get(code, code))
            index += 2
            continue
        if char == '"':
            return "".join(out), text[index + 1 :]
        out.append(char)
        index += 1
    raise YamlError(number, "unterminated double-quoted string")


def parse_single_quoted(text: str, number: int) -> Tuple[str, str]:
    out: List[str] = []
    index = 1
    while index < len(text):
        if text[index] == "'":
            if text[index + 1 : index + 2] == "'":
                out.append("'")
                index += 2
                continue
            return "".join(out), text[index + 1 :]
        out.append(text[index])
        index += 1
    raise YamlError(number, "unterminated single-quoted string")


def parse_flow_value(text: str, number: int, terminators: str) -> Tuple[Any, str]:
    if text.startswith('"'):
        return parse_double_quoted(text, number)
    if text.startswith("'"):
        return parse_single_quoted(text, number)
    if text[:1] in "[{":
        return parse_flow(text, number)
    end = len(text)
    for index, char in enumerate(text):
        if char in terminators:
            end = index
            break
    return plain_scalar(text[:end].strip()), text[end:]


def parse_flow(text: str, number: int) -> Tuple[Any, str]:
    closer = "]" if text.startswith("[") else "}"
    items: List[Any] = []
    mapping: Dict[str, Any] = {}
    rest = text[1:].lstrip()
    while True:
        if rest.startswith(closer):
            return (items if closer == "]" else mapping), rest[1:].lstrip()
        if not rest:
            raise YamlError(number, f"unterminated flow collection; missing {closer}")
        if closer == "]":
            value, rest = parse_flow_value(rest, number, ",]")
            items.append(value)
        else:
            key, rest = parse_flow_value(rest, number, ":,}")
            rest = rest.lstrip()
            value = None
            if rest.startswith(":"):
                value, rest = parse_flow_value(rest[1:].lstrip(), number, ",}")
            mapping[key_string(key)] = value
        rest = rest.lstrip()
        if rest.startswith(","):
            rest = rest[1:].lstrip()
        elif not rest.startswith(closer):
            raise YamlError(number, f"expected , or {closer} in flow collection")


def parse_scalar(text: str, number: int) -> Any:
    if text.startswith(("&", "*", "<<")):
        raise YamlError(number, "anchors, aliases and merge keys are not supported; repeat the value")
    if text.startswith("!"):
        raise YamlError(number, "tags such as !!str are not supported; quote the value instead")
    if text.startswith('"'):
        value, rest = parse_double_quoted(text, number)
    elif text.startswith("'"):
        value, rest = parse_single_quoted(text, number)
    elif text[:1] in "[{":
        value, rest = parse_flow(text, number)
    else:
        return plain_scalar(text)
    if rest.strip():
        raise YamlError(number, f"unexpected text after value: {rest.strip()!r}")
    return value


def split_key(text: str, number: int) -> Optional[Tuple[str, str]]:
    if text.startswith(("'", '"')):
        key, rest = (parse_single_quoted if text[0] == "'" else parse_double_quoted)(text, number)
        rest = rest.lstrip()
        if rest.startswith(":") and (len(rest) == 1 or rest[1] == " "):
            return key, rest[1:].strip()
        return None
    if text[:1] in "[{":
        return None
    for index, char in enumerate(text):
        if char == ":" and (index + 1 == len(text) or text[index + 1] == " "):
            key = text[:index].strip()
            if key == "<<":
                raise YamlError(number, "merge keys (<<) are not supported; repeat the values")
            return key_string(plain_scalar(key)), text[index + 1 :].strip()
    return None


def is_item(text: str) -> bool:
    return text == "-" or text.startswith("- ")


class YamlParser:
    def __init__(self, source: str) -> None:
        self.raw = source.splitlines()
        self.lines = split_lines(source)
        self.position = 0

    def peek(self) -> Optional[Line]:
        return self.lines[self.position] if self.position < len(self.lines) else None

    def parse_document(self) -> Any:
        if not self.lines:
            return None
        value = self.parse_block(self.lines[0].indent)
        extra = self.peek()
        if extra is not None:
            raise YamlError(extra.number, f"unexpected indentation ({extra.indent} spaces)")
        return value

    def parse_block(self, indent: int) -> Any:
        line = self.peek()
        if line is None:
            return None
        if is_item(line.text):
            return self.parse_sequence(indent)
        if split_key(line.text, line.number) is None:
            self.position += 1
            if BLOCK_HEADER_RE.match(line.text):
                return self.block_scalar(line.text, line.number, indent - 1)
            return parse_scalar(line.text, line.number)
        return self.parse_mapping(indent)

    def parse_sequence(self, indent: int, compact: bool = False) -> List[Any]:
        items: List[Any] = []
        while True:
            line = self.peek()
            if line is None or line.indent < indent:
                return items
            if line.indent > indent:
                raise YamlError(line.number, f"bad indentation in sequence (expected {indent} spaces, got {line.indent})")
            if not is_item(line.text):
                if compact:
                    return items
                raise YamlError(line.number, "expected a sequence item starting with '- '")
            rest = line.text[1:].strip()
            if not rest:
                self.position += 1
                nested = self.peek()
                items.append(self.parse_block(nested.indent) if nested is not None and nested.indent > indent else None)
                continue
            if BLOCK_HEADER_RE.match(rest):
                self.position += 1
                items.append(self.block_scalar(rest, line.number, indent))
                continue
            column = indent + (len(line.text) - len(line.text[1:].lstrip()))
            self.lines[self.position] = Line(line.number, column, rest)
            items.append(self.parse_block(column))

    def parse_mapping(self, indent: int) -> Dict[str, Any]:
        mapping: Dict[str, Any] = {}
        while True:
            line = self.peek()
            if line is None or line.indent < indent:
                return mapping
            if line.indent > indent:
                raise YamlError(line.number, f"bad indentation in mapping (expected {indent} spaces, got {line.indent})")
            parts = split_key(line.text, line.number)
            if parts is None:
                raise YamlError(line.number, f"expected 'key: value', got {line.text!r}")
            key, rest = parts
            if key in mapping:
                raise YamlError(line.number, f"duplicate key {key!r}")
            self.position += 1
            if rest and BLOCK_HEADER_RE.match(rest):
                mapping[key] = self.block_scalar(rest, line.number, indent)
            elif rest:
                mapping[key] = parse_scalar(rest, line.number)
            else:
                nested = self.peek()
                if nested is None or nested.indent < indent:
                    mapping[key] = None
                elif nested.indent == indent:
                    mapping[key] = self.parse_sequence(indent, compact=True) if is_item(nested.text) else None
                else:
                    mapping[key] = self.parse_block(nested.indent)

    def block_scalar(self, header: str, number: int, parent_indent: int) -> str:
        match = BLOCK_HEADER_RE.match(header)
        if match is None:
            raise YamlError(number, f"bad block scalar header {header!r}")
        style, chomp, explicit = match.group(1), match.group(2) or match.group(4), match.group(3)
        content_indent: Optional[int] = parent_indent + int(explicit) if explicit else None
        body: List[str] = []
        index = number
        while index < len(self.raw):
            raw = self.raw[index]
            if not raw.strip():
                body.append("")
                index += 1
                continue
            indent = len(raw) - len(raw.lstrip(" "))
            if content_indent is None:
                if indent <= parent_indent:
                    break
                content_indent = indent
            if indent < content_indent:
                break
            body.append(raw[content_indent:])
            index += 1
        while self.position < len(self.lines) and self.lines[self.position].number <= index:
            self.position += 1
        trailing = 0
        while body and body[-1] == "":
            body.pop()
            trailing += 1
        if not body:
            return ""
        text = "\n".join(body) if style == "|" else fold_lines(body)
        if chomp == "-":
            return text
        return text + ("\n" * (1 + trailing) if chomp == "+" else "\n")


def fold_lines(lines: List[str]) -> str:
    out = [lines[0]]
    for previous, line in zip(lines, lines[1:]):
        if line == "":
            out.append("\n")
        elif previous == "" or line.startswith(" ") or previous.startswith(" "):
            out.append(("" if previous == "" else "\n") + line)
        else:
            out.append(" " + line)
    return "".join(out)


def parse_yaml(source: str) -> Any:
    return YamlParser(source).parse_document()


def parse_documents(text: str) -> List[Any]:
    return [doc for doc in (parse_yaml(piece) for piece in DOC_SEPARATOR.split(text) if piece.strip()) if doc is not None]


def plain_allowed(text: str) -> bool:
    if text[0] in " \t!&*%@`|>'\"{}[],#?:-" or text[-1] in " \t:":
        return False
    return ": " not in text and " #" not in text and "\n" not in text


def yaml_string(text: str) -> str:
    if text and plain_allowed(text) and plain_scalar(text) == text and text not in YAML11_WORDS:
        return text
    return json.dumps(text)


def yaml_scalar(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return json.dumps(value)
    return yaml_string(str(value))


def emit_yaml(value: Any, indent: int) -> List[str]:
    pad = " " * indent
    if isinstance(value, dict) and value:
        lines: List[str] = []
        for key, child in value.items():
            head = pad + yaml_string(str(key)) + ":"
            if isinstance(child, dict) and child:
                lines += [head] + emit_yaml(child, indent + 2)
            elif isinstance(child, list) and child:
                lines += [head] + emit_yaml(child, indent)
            else:
                lines.append(head + " " + emit_yaml(child, 0)[0])
        return lines
    if isinstance(value, list) and value:
        lines = []
        for item in value:
            nested = emit_yaml(item, indent + 2)
            lines += [pad + "- " + nested[0][indent + 2 :]] + nested[1:]
        return lines
    if isinstance(value, dict):
        return [pad + "{}"]
    if isinstance(value, list):
        return [pad + "[]"]
    return [pad + yaml_scalar(value)]


def to_yaml_documents(objects: Sequence[Obj]) -> str:
    return "---\n".join("\n".join(emit_yaml(obj, 0)) + "\n" for obj in objects)


def normal_path(path: str) -> str:
    return join_path("", path or ".")


def join_path(base: str, relative: str) -> str:
    if relative.startswith("/"):
        raise InputError(f"absolute path {relative!r}: paths are relative to the repository or the kustomization")
    joined = posixpath.normpath(posixpath.join(base, relative))
    if joined == ".":
        return ""
    if joined == ".." or joined.startswith("../"):
        raise InputError(f"{relative!r} from {base or '.'} leaves the repository")
    return joined


class DirTree:
    def __init__(self, root: str) -> None:
        self.root = os.path.abspath(root)
        self.label = f"directory {self.root}"

    def full(self, path: str) -> str:
        return os.path.join(self.root, path) if path else self.root

    def is_dir(self, path: str) -> bool:
        return os.path.isdir(self.full(path))

    def is_file(self, path: str) -> bool:
        return os.path.isfile(self.full(path))

    def files(self, path: str) -> List[str]:
        return sorted(name for name in os.listdir(self.full(path)) if os.path.isfile(os.path.join(self.full(path), name)))

    def read(self, path: str) -> str:
        try:
            with open(self.full(path), encoding="utf-8") as handle:
                return handle.read()
        except OSError as err:
            raise InputError(f"cannot read {path}: {err.strerror}") from err


class GitTree:
    def __init__(self, git: "Git", revision: str) -> None:
        self.git = git
        self.revision = revision
        self.label = f"{git.url} at {revision[:7]}"
        self.paths = {p for p in git.run("ls-tree", "-r", "-z", "--name-only", revision).split("\0") if p}
        self.dirs = {""}
        for path in self.paths:
            parent = posixpath.dirname(path)
            while parent and parent not in self.dirs:
                self.dirs.add(parent)
                parent = posixpath.dirname(parent)

    def is_dir(self, path: str) -> bool:
        return path in self.dirs

    def is_file(self, path: str) -> bool:
        return path in self.paths

    def files(self, path: str) -> List[str]:
        return sorted(posixpath.basename(p) for p in self.paths if posixpath.dirname(p) == path)

    def read(self, path: str) -> str:
        if path not in self.paths:
            raise InputError(f"{path} does not exist in {self.label}")
        return self.git.run("cat-file", "blob", f"{self.revision}:{path}")


def kustomization_file(tree: Any, path: str) -> Optional[str]:
    for name in KUSTOMIZATION_FILES:
        if tree.is_file(join_path(path, name)):
            return name
    return None


def yaml_file(tree: Any, path: str) -> List[Any]:
    text = tree.read(path)
    try:
        if path.endswith(".json"):
            return [json.loads(text)]
        return parse_documents(text)
    except YamlError as err:
        raise InputError(f"{path}: {err}") from err
    except ValueError as err:
        raise InputError(f"{path}: not valid JSON: {err}") from err


def load_manifests(tree: Any, path: str) -> List[Obj]:
    objects: List[Obj] = []
    for number, doc in enumerate(yaml_file(tree, path), start=1):
        items = doc.get("items") if isinstance(doc, dict) and str(doc.get("kind", "")).endswith("List") else [doc]
        for item in items or []:
            if not (isinstance(item, dict) and item.get("apiVersion") and item.get("kind") and isinstance(item.get("metadata"), dict) and item["metadata"].get("name")):
                raise InputError(f"{path}: document {number} is not a Kubernetes object (it needs apiVersion, kind and metadata.name)")
            objects.append(item)
    return objects


def render(tree: Any, path: str) -> List[Obj]:
    path = normal_path(path)
    if tree.is_file(path):
        raise InputError(f"path {path} is a file; the application path is a directory")
    if not tree.is_dir(path):
        raise InputError(f"path {path or '.'} does not exist in {tree.label}")
    if kustomization_file(tree, path):
        return kustomize(tree, path, ())
    objects: List[Obj] = []
    for name in tree.files(path):
        if name.endswith((".yaml", ".yml", ".json")):
            objects += load_manifests(tree, join_path(path, name))
    check_unique(objects, path or ".")
    return objects


def as_list(value: Any, where: str, name: str) -> List[Any]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise InputError(f"{where}: {name} must be a list")
    return value


def kustomize(tree: Any, path: str, stack: Tuple[str, ...]) -> List[Obj]:
    if path in stack:
        raise InputError("resources cycle: " + " -> ".join(stack + (path,)))
    name = kustomization_file(tree, path)
    if name is None:
        raise InputError(f"{path or '.'} is a directory without a kustomization.yaml; a directory in resources must have one")
    where = join_path(path, name)
    docs = yaml_file(tree, where)
    spec = docs[0] if docs else {}
    if len(docs) > 1 or not isinstance(spec, dict):
        raise InputError(f"{where}: a kustomization is one YAML mapping")
    unknown = sorted(set(spec) - set(KUSTOMIZATION_FIELDS))
    if unknown:
        raise InputError(f"{where}: {', '.join(unknown)} not supported by minigitops (supported: {', '.join(KUSTOMIZATION_FIELDS[2:])})")
    objects: List[Obj] = []
    for entry in as_list(spec.get("resources"), where, "resources"):
        if not isinstance(entry, str):
            raise InputError(f"{where}: resources entries are paths")
        if "://" in entry or entry.startswith(("github.com/", "git@")):
            raise InputError(f"{where}: remote resource {entry!r} is not supported; vendor it into the repository")
        target = join_path(path, entry)
        if tree.is_dir(target):
            objects += kustomize(tree, target, stack + (path,))
        elif tree.is_file(target):
            objects += load_manifests(tree, target)
        else:
            raise InputError(f"{where}: resource {entry!r} not found")
    check_unique(objects, where)
    for entry in as_list(spec.get("patches"), where, "patches"):
        apply_patch(objects, tree, path, where, entry)
    if spec.get("namespace"):
        for obj in objects:
            if obj["kind"] not in CLUSTER_SCOPED:
                obj["metadata"]["namespace"] = spec["namespace"]
    if spec.get("namePrefix") or spec.get("nameSuffix"):
        rename(objects, str(spec.get("namePrefix") or ""), str(spec.get("nameSuffix") or ""))
    label_sets = [{"pairs": spec["commonLabels"], "includeSelectors": True}] if spec.get("commonLabels") else []
    for entry in label_sets + as_list(spec.get("labels"), where, "labels"):
        pairs = entry.get("pairs") if isinstance(entry, dict) else None
        if not isinstance(pairs, dict) or not all(isinstance(v, str) for v in pairs.values()):
            raise InputError(f"{where}: labels need pairs of strings; quote values such as \"true\" or \"1\"")
        add_labels(objects, pairs, bool(entry.get("includeSelectors")), bool(entry.get("includeTemplates")))
    for entry in as_list(spec.get("images"), where, "images"):
        set_image(objects, entry, where)
    return objects


def check_unique(objects: Sequence[Obj], where: str) -> None:
    seen: Set[Tuple[str, str, str, str]] = set()
    for obj in objects:
        key = ident(obj)
        if key in seen:
            raise InputError(f"{where}: may not add resource with an already registered id: {describe(obj)}")
        seen.add(key)


def apply_patch(objects: List[Obj], tree: Any, path: str, where: str, entry: Any) -> None:
    if not isinstance(entry, dict) or ("path" in entry) == ("patch" in entry):
        raise InputError(f"{where}: each patch needs exactly one of path or patch")
    target = entry.get("target") or {}
    extra = sorted(set(target) - {"kind", "name", "namespace"})
    if extra:
        raise InputError(f"{where}: patch target fields {', '.join(extra)} are not supported (use kind and name)")
    if "path" in entry:
        docs = yaml_file(tree, join_path(path, str(entry["path"])))
    else:
        try:
            docs = parse_documents(str(entry["patch"]))
        except YamlError as err:
            raise InputError(f"{where}: inline patch: {err}") from err
    for doc in docs:
        if isinstance(doc, list):
            raise InputError(f"{where}: JSON 6902 patches (a list of operations) are not supported; write a strategic merge patch")
        if not isinstance(doc, dict):
            raise InputError(f"{where}: a patch is a mapping")
        kind = target.get("kind") or doc.get("kind")
        name = target.get("name") or (doc.get("metadata") or {}).get("name")
        if not kind or not name:
            raise InputError(f"{where}: a patch must name its target with kind and metadata.name, or with target")
        matches = [i for i, obj in enumerate(objects) if obj["kind"] == kind and obj["metadata"]["name"] == name and (not target.get("namespace") or obj["metadata"].get("namespace") == target["namespace"])]
        if not matches:
            raise InputError(f"{where}: patch target {kind}/{name} not found among {len(objects)} resource(s)")
        for index in matches:
            objects[index] = strategic_merge(objects[index], doc, ())


def merge_keys(names: Sequence[str]) -> Tuple[str, ...]:
    if not names:
        return ()
    if names[-1] == "ports":
        return ("containerPort",) if len(names) > 1 and names[-2] in CONTAINER_LISTS else ("port",)
    return LIST_KEYS.get(names[-1], ())


def item_key(item: Obj, keys: Sequence[str]) -> Tuple[Any, ...]:
    return tuple(item.get(k) for k in keys)


def strip_directives(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: strip_directives(v) for k, v in value.items() if k != "$patch"}
    if isinstance(value, list):
        return [strip_directives(v) for v in value if not (isinstance(v, dict) and v.get("$patch") == "delete")]
    return copy.deepcopy(value)


def strategic_merge(base: Any, patch: Any, names: Tuple[str, ...]) -> Any:
    if isinstance(patch, dict):
        if patch.get("$patch") == "replace" or not isinstance(base, dict):
            return strip_directives(patch)
        merged = copy.deepcopy(base)
        for key, value in patch.items():
            if key == "$patch":
                continue
            if value is None:
                merged.pop(key, None)
            elif key in merged:
                merged[key] = strategic_merge(merged[key], value, names + (key,))
            else:
                merged[key] = strip_directives(value)
        return merged
    if isinstance(patch, list) and isinstance(base, list):
        keys = merge_keys(names)
        if keys and all(isinstance(i, dict) for i in base + patch):
            merged_list = copy.deepcopy(base)
            for item in patch:
                index = next((i for i, have in enumerate(merged_list) if item_key(have, keys) == item_key(item, keys)), None)
                if item.get("$patch") == "delete":
                    if index is not None:
                        del merged_list[index]
                elif index is None:
                    merged_list.append(strip_directives(item))
                else:
                    merged_list[index] = strategic_merge(merged_list[index], item, names)
            return merged_list
    return strip_directives(patch)


def pod_template(obj: Obj) -> Optional[Obj]:
    spec = obj.get("spec")
    if not isinstance(spec, dict):
        return None
    if obj.get("kind") in WORKLOADS:
        template = spec.get("template")
    elif obj.get("kind") == "CronJob":
        template = ((spec.get("jobTemplate") or {}).get("spec") or {}).get("template")
    else:
        return None
    return template if isinstance(template, dict) else None


def pod_specs(obj: Obj) -> List[Obj]:
    if obj.get("kind") == "Pod":
        return [obj["spec"]] if isinstance(obj.get("spec"), dict) else []
    template = pod_template(obj)
    return [template["spec"]] if template and isinstance(template.get("spec"), dict) else []


def containers(spec: Obj) -> List[Obj]:
    return [c for name in CONTAINER_LISTS for c in spec.get(name) or [] if isinstance(c, dict)]


def rename(objects: List[Obj], prefix: str, suffix: str) -> None:
    renames: Dict[Tuple[str, str], str] = {}
    for obj in objects:
        old = obj["metadata"]["name"]
        renames[(obj["kind"], old)] = prefix + old + suffix
        obj["metadata"]["name"] = prefix + old + suffix

    def update(ref: Any, key: str, kind: str) -> None:
        if isinstance(ref, dict) and isinstance(ref.get(key), str):
            ref[key] = renames.get((kind, ref[key]), ref[key])

    for obj in objects:
        for spec in pod_specs(obj):
            for container in containers(spec):
                for source in container.get("envFrom") or []:
                    for ref_field, kind in REFERENCES[:2]:
                        update((source or {}).get(ref_field), "name", kind)
                for env in container.get("env") or []:
                    for ref_field, kind in REFERENCES[2:]:
                        update(((env or {}).get("valueFrom") or {}).get(ref_field), "name", kind)
            for volume in spec.get("volumes") or []:
                update(volume.get("configMap"), "name", "ConfigMap")
                update(volume.get("secret"), "secretName", "Secret")
                update(volume.get("persistentVolumeClaim"), "claimName", "PersistentVolumeClaim")
            update(spec, "serviceAccountName", "ServiceAccount")
            for pull in spec.get("imagePullSecrets") or []:
                update(pull, "name", "Secret")


def add_labels(objects: List[Obj], pairs: Dict[str, str], include_selectors: bool, include_templates: bool) -> None:
    for obj in objects:
        meta = obj["metadata"]
        meta["labels"] = dict(meta.get("labels") or {}, **pairs)
        template = pod_template(obj)
        if template is not None and (include_selectors or include_templates):
            template_meta = template.setdefault("metadata", {})
            template_meta["labels"] = dict(template_meta.get("labels") or {}, **pairs)
        if not include_selectors:
            continue
        spec = obj.setdefault("spec", {})
        if obj["kind"] in ("Deployment", "StatefulSet", "DaemonSet", "ReplicaSet"):
            selector = spec.setdefault("selector", {})
            selector["matchLabels"] = dict(selector.get("matchLabels") or {}, **pairs)
        elif obj["kind"] == "Service":
            spec["selector"] = dict(spec.get("selector") or {}, **pairs)


def split_image(reference: str) -> Tuple[str, str, str]:
    name, _, digest = reference.partition("@")
    slash, colon = name.rfind("/"), name.rfind(":")
    if colon > slash:
        return name[:colon], name[colon + 1 :], digest
    return name, "", digest


def set_image(objects: List[Obj], entry: Any, where: str) -> None:
    if not isinstance(entry, dict) or not entry.get("name"):
        raise InputError(f"{where}: each images entry needs a name")
    for key in ("newName", "newTag", "digest"):
        if entry.get(key) is not None and not isinstance(entry[key], str):
            raise InputError(f"{where}: images {entry['name']} {key} must be a string; quote it (\"3.10\" is a tag, 3.10 is the number 3.1)")
    for obj in objects:
        for spec in pod_specs(obj):
            for container in containers(spec):
                if not isinstance(container.get("image"), str):
                    continue
                name, tag, digest = split_image(container["image"])
                if name != entry["name"]:
                    continue
                name = entry.get("newName") or name
                if entry.get("digest"):
                    container["image"] = f"{name}@{entry['digest']}"
                elif entry.get("newTag") is not None:
                    container["image"] = f"{name}:{entry['newTag']}"
                else:
                    container["image"] = name + (f":{tag}" if tag else "") + (f"@{digest}" if digest else "")


def group_of(obj: Obj) -> str:
    api = str(obj.get("apiVersion") or "")
    return api.split("/")[0] if "/" in api else ""


def ident(obj: Obj) -> Tuple[str, str, str, str]:
    meta = obj.get("metadata") or {}
    return group_of(obj), str(obj.get("kind")), str(meta.get("namespace") or ""), str(meta.get("name"))


def describe(obj: Obj) -> str:
    return f"{obj.get('kind')}/{(obj.get('metadata') or {}).get('name')}"


def resource_name(obj: Obj) -> str:
    group = group_of(obj)
    return str(obj["kind"]).lower() + (f".{group}" if group else "")


def annotations_of(obj: Optional[Obj]) -> Dict[str, str]:
    return ((obj or {}).get("metadata") or {}).get("annotations") or {}


def hook_types(obj: Obj) -> List[str]:
    raw = annotations_of(obj).get(HOOK)
    if not raw:
        return []
    types = [t.strip() for t in str(raw).split(",") if t.strip()]
    for hook_type in types:
        if hook_type not in HOOK_TYPES:
            raise InputError(f"{describe(obj)}: unknown hook {hook_type!r} (known: {', '.join(HOOK_TYPES)})")
    return types


def delete_policies(obj: Obj) -> Set[str]:
    raw = annotations_of(obj).get(HOOK_DELETE)
    policies = {p.strip() for p in str(raw).split(",") if p.strip()} if raw else {"BeforeHookCreation"}
    unknown = policies - set(DELETE_POLICIES)
    if unknown:
        raise InputError(f"{describe(obj)}: unknown hook-delete-policy {', '.join(sorted(unknown))}")
    return policies


def wave_of(obj: Optional[Obj]) -> int:
    raw = annotations_of(obj).get(WAVE)
    if raw is None:
        return 0
    try:
        return int(str(raw).strip())
    except ValueError as err:
        raise InputError(f"{describe(obj or {})}: {WAVE} must be an integer, got {raw!r}") from err


def sync_options(obj: Optional[Obj]) -> Set[str]:
    return {o.strip() for o in str(annotations_of(obj).get(SYNC_OPTIONS) or "").split(",") if o.strip()}


def kind_rank(kind: str) -> int:
    return KIND_ORDER.index(kind) if kind in KIND_ORDER else len(KIND_ORDER)


def sync_key(obj: Obj) -> Tuple[int, int, str]:
    return wave_of(obj), kind_rank(str(obj.get("kind"))), str(obj["metadata"]["name"])


def leaf_paths(value: Any, path: Path = (), names: Tuple[str, ...] = ()) -> Dict[Path, Any]:
    if isinstance(value, dict) and value:
        out: Dict[Path, Any] = {}
        for key, child in value.items():
            out.update(leaf_paths(child, path + ("f:" + str(key),), names + (str(key),)))
        return out
    if isinstance(value, list) and value:
        keys = merge_keys(names)
        if keys and all(isinstance(i, dict) and all(k in i for k in keys) for i in value):
            out = {}
            for item in value:
                segment = "k:" + json.dumps({k: item[k] for k in keys}, sort_keys=True, separators=(",", ":"))
                out.update(leaf_paths(item, path + (segment,), names))
            return out
    return {path: value}


def fields_v1(paths: Iterable[Path]) -> Obj:
    tree: Obj = {}
    for path in paths:
        node = tree
        for segment in path:
            node = node.setdefault(segment, {})
            if segment.startswith("k:"):
                node.setdefault(".", {})
    return tree


def paths_of_fields(tree: Obj, prefix: Path = ()) -> List[Path]:
    out: List[Path] = []
    for segment, child in tree.items():
        if segment == ".":
            continue
        nested = {k: v for k, v in (child or {}).items() if k != "."}
        out += paths_of_fields(nested, prefix + (segment,)) if nested else [prefix + (segment,)]
    return out


def segment_matches(desired: str, owned: str) -> bool:
    if desired == owned:
        return True
    if desired.startswith("k:") and owned.startswith("k:"):
        want, have = json.loads(desired[2:]), json.loads(owned[2:])
        return all(have.get(k) == v for k, v in want.items())
    return False


def covered(owned: Path, desired: Iterable[Path]) -> bool:
    for path in desired:
        width = min(len(owned), len(path))
        if all(segment_matches(path[i], owned[i]) for i in range(width)):
            return True
    return False


def is_key_field(path: Path) -> bool:
    return len(path) >= 2 and path[-1].startswith("f:") and path[-2].startswith("k:") and path[-1][2:] in json.loads(path[-2][2:])


def resolve(value: Any, path: Path) -> Any:
    for segment in path:
        if segment.startswith("f:") and isinstance(value, dict) and segment[2:] in value:
            value = value[segment[2:]]
        elif segment.startswith("k:") and isinstance(value, list):
            want = json.loads(segment[2:])
            value = next((i for i in value if isinstance(i, dict) and all(i.get(k) == v for k, v in want.items())), MISSING)
            if value is MISSING:
                return MISSING
        else:
            return MISSING
    return value


def delete_path(obj: Obj, path: Path) -> None:
    parent = resolve(obj, path[:-1])
    last = path[-1]
    if last.startswith("f:") and isinstance(parent, dict):
        parent.pop(last[2:], None)
    elif last.startswith("k:") and isinstance(parent, list):
        want = json.loads(last[2:])
        parent[:] = [i for i in parent if not (isinstance(i, dict) and all(i.get(k) == v for k, v in want.items()))]
    if len(path) > 1 and parent in ({}, []):
        delete_path(obj, path[:-1])


def pretty_path(path: Path) -> str:
    out = ""
    for segment in path:
        if segment.startswith("f:"):
            out += ("." if out else "") + segment[2:]
        elif segment.startswith("k:"):
            out += "[" + ",".join(f"{k}={v}" for k, v in json.loads(segment[2:]).items()) + "]"
        else:
            out += f"[{segment}]"
    return out


def quantity(value: Any) -> Optional[Fraction]:
    if isinstance(value, bool):
        return None
    match = QUANTITY_RE.match(str(value).strip())
    if not match:
        return None
    number, suffix = match.groups()
    if suffix and suffix[0] in "eE":
        return Fraction(number) * Fraction(10) ** int(suffix[1:])
    return Fraction(number) * QUANTITY_SUFFIX[suffix or ""]


def same_value(path: Path, want: Any, have: Any) -> bool:
    if isinstance(want, dict) and isinstance(have, dict):
        return all(k in have and same_value(path + ("f:" + k,), v, have[k]) for k, v in want.items())
    if isinstance(want, list) and isinstance(have, list):
        return len(want) == len(have) and all(same_value(path, a, b) for a, b in zip(want, have))
    if isinstance(want, bool) or isinstance(have, bool):
        return want is have
    if any(s in ("f:limits", "f:requests") for s in path) and "f:resources" in path:
        a, b = quantity(want), quantity(have)
        if a is not None and b is not None:
            return a == b
    return want == have


def owned_paths(live: Obj, manager: str) -> List[Path]:
    out: List[Path] = []
    for entry in (live.get("metadata") or {}).get("managedFields") or []:
        if entry.get("manager") == manager and entry.get("operation") == "Apply" and not entry.get("subresource"):
            out += paths_of_fields(entry.get("fieldsV1") or {})
    return out


@dataclass
class Difference:
    path: str
    change: str
    desired: Any = None
    live: Any = None

    def text(self) -> str:
        if self.change == "removed":
            return f"{self.path}: live {short(self.live)}, removed from Git"
        if self.change == "missing":
            return f"{self.path}: Git {short(self.desired)}, missing live"
        return f"{self.path}: Git {short(self.desired)}, live {short(self.live)}"

    def to_dict(self) -> Obj:
        return {"path": self.path, "change": self.change, "desired": self.desired, "live": self.live}


def short(value: Any) -> str:
    text = json.dumps(value, sort_keys=True)
    return text if len(text) <= 60 else text[:57] + "..."


def normalise(live: Obj) -> Obj:
    clean = copy.deepcopy(live)
    clean.pop("status", None)
    meta = clean.get("metadata") or {}
    for key in SERVER_FIELDS:
        meta.pop(key, None)
    notes = meta.get("annotations") or {}
    for key in SERVER_ANNOTATIONS:
        notes.pop(key, None)
    if "annotations" in meta and not notes:
        meta.pop("annotations")
    return clean


def diff(desired: Obj, live: Obj, manager: str = FIELD_MANAGER) -> List[Difference]:
    clean = normalise(live)
    want = leaf_paths(desired)
    out: List[Difference] = []
    for path, value in want.items():
        have = resolve(clean, path)
        if have is MISSING:
            if value not in ({}, [], None):
                out.append(Difference(pretty_path(path), "missing", value, None))
        elif not same_value(path, value, have):
            out.append(Difference(pretty_path(path), "changed", value, have))
    reported: Set[str] = set()
    for owned in owned_paths(live, manager):
        if is_key_field(owned) or covered(owned, want):
            continue
        top = owned
        while len(top) > 1 and not covered(top[:-1], want) and resolve(clean, top[:-1]) is not MISSING:
            top = top[:-1]
        have = resolve(clean, top)
        if have is not MISSING and pretty_path(top) not in reported:
            reported.add(pretty_path(top))
            out.append(Difference(pretty_path(top), "removed", None, have))
    return out


def condition(obj: Obj, kind: str) -> Optional[Obj]:
    for item in (obj.get("status") or {}).get("conditions") or []:
        if item.get("type") == kind:
            return item
    return None


def deployment_health(obj: Obj) -> Tuple[str, str]:
    spec, status = obj.get("spec") or {}, obj.get("status") or {}
    if spec.get("paused"):
        return "Suspended", "Deployment is paused"
    if (obj.get("metadata") or {}).get("generation", 0) > status.get("observedGeneration", 0):
        return "Progressing", "Waiting for rollout to finish: observed deployment generation less than desired generation"
    progressing = condition(obj, "Progressing")
    replicas, updated = spec.get("replicas", 1), status.get("updatedReplicas", 0)
    if progressing and progressing.get("reason") == "ProgressDeadlineExceeded":
        return "Degraded", f"Deployment {obj['metadata']['name']!r} exceeded its progress deadline"
    if updated < replicas:
        return "Progressing", f"Waiting for rollout to finish: {updated} out of {replicas} new replicas have been updated..."
    if status.get("replicas", 0) > updated:
        return "Progressing", f"Waiting for rollout to finish: {status.get('replicas', 0) - updated} old replicas are pending termination..."
    if status.get("availableReplicas", 0) < updated:
        return "Progressing", f"Waiting for rollout to finish: {status.get('availableReplicas', 0)} of {updated} updated replicas are available..."
    return "Healthy", ""


def statefulset_health(obj: Obj) -> Tuple[str, str]:
    spec, status = obj.get("spec") or {}, obj.get("status") or {}
    if (obj.get("metadata") or {}).get("generation", 0) > status.get("observedGeneration", 0):
        return "Progressing", "Waiting for statefulset spec update to be observed..."
    replicas = spec.get("replicas", 1)
    if status.get("readyReplicas", 0) < replicas:
        return "Progressing", f"Waiting for {replicas - status.get('readyReplicas', 0)} pods to be ready..."
    if status.get("updateRevision") and status.get("updateRevision") != status.get("currentRevision"):
        return "Progressing", "waiting for statefulset rolling update to complete"
    return "Healthy", ""


def daemonset_health(obj: Obj) -> Tuple[str, str]:
    status = obj.get("status") or {}
    if (obj.get("metadata") or {}).get("generation", 0) > status.get("observedGeneration", 0):
        return "Progressing", "Waiting for rollout to finish: observed daemon set generation less than desired generation"
    wanted = status.get("desiredNumberScheduled", 0)
    if status.get("updatedNumberScheduled", 0) < wanted or status.get("numberAvailable", 0) < wanted:
        return "Progressing", f"Waiting for daemon set rollout to finish: {status.get('numberAvailable', 0)} of {wanted} updated pods are available..."
    return "Healthy", ""


def job_health(obj: Obj) -> Tuple[str, str]:
    failed = condition(obj, "Failed")
    if failed and failed.get("status") == "True":
        return "Degraded", failed.get("message") or "Job failed"
    complete = condition(obj, "Complete")
    if complete and complete.get("status") == "True":
        return "Healthy", complete.get("message") or "Job completed"
    if (obj.get("spec") or {}).get("suspend"):
        return "Suspended", "Job is suspended"
    return "Progressing", "Job is running"


def pod_health(obj: Obj) -> Tuple[str, str]:
    status = obj.get("status") or {}
    phase = status.get("phase")
    if phase == "Succeeded":
        return "Healthy", "Pod completed"
    if phase == "Failed":
        return "Degraded", status.get("message") or "Pod failed"
    for container in status.get("containerStatuses") or []:
        reason = ((container.get("state") or {}).get("waiting") or {}).get("reason")
        if reason in ("CrashLoopBackOff", "ImagePullBackOff", "ErrImagePull", "CreateContainerConfigError", "InvalidImageName"):
            return "Degraded", f"container {container.get('name')}: {reason}"
    ready = condition(obj, "Ready")
    if phase == "Running" and ready and ready.get("status") == "True":
        return "Healthy", ""
    return "Progressing", f"Pod is {phase or 'Pending'}"


def assess(obj: Optional[Obj]) -> Tuple[str, str]:
    if obj is None:
        return "Missing", "resource is missing"
    if (obj.get("metadata") or {}).get("deletionTimestamp"):
        return "Progressing", "pending deletion"
    kind = obj.get("kind")
    if kind in ("Deployment", "ReplicaSet"):
        return deployment_health(obj)
    if kind == "StatefulSet":
        return statefulset_health(obj)
    if kind == "DaemonSet":
        return daemonset_health(obj)
    if kind == "Job":
        return job_health(obj)
    if kind == "Pod":
        return pod_health(obj)
    if kind == "PersistentVolumeClaim":
        phase = (obj.get("status") or {}).get("phase")
        return ("Healthy", "") if phase == "Bound" else ("Degraded", "claim lost") if phase == "Lost" else ("Progressing", f"claim is {phase or 'Pending'}")
    if kind == "Service" and (obj.get("spec") or {}).get("type") == "LoadBalancer":
        ingress = ((obj.get("status") or {}).get("loadBalancer") or {}).get("ingress") or []
        return ("Healthy", "") if any(i.get("ip") or i.get("hostname") for i in ingress) else ("Progressing", "waiting for a load balancer address")
    return "Healthy", ""


def worst(statuses: Iterable[str]) -> str:
    found = list(statuses)
    return max(found, key=HEALTH_ORDER.index) if found else "Healthy"


def first_line(text: str) -> str:
    return next((line.strip() for line in text.splitlines() if line.strip()), "")


def run_command(argv: List[str], stdin: Optional[str] = None) -> Tuple[int, str, str]:
    env = dict(os.environ, GIT_TERMINAL_PROMPT="0", LC_ALL="C")
    try:
        proc = subprocess.run(argv, input=stdin, capture_output=True, text=True, env=env, timeout=300)
    except FileNotFoundError:
        return 127, "", f"{argv[0]}: command not found"
    except subprocess.TimeoutExpired:
        return 124, "", f"{' '.join(argv[:3])}: timed out after 300s"
    return proc.returncode, proc.stdout, proc.stderr


class Git:
    def __init__(self, url: str, cache_dir: Optional[str] = None, runner: Runner = run_command) -> None:
        self.url = url if "://" in url or url.startswith("git@") else os.path.abspath(url)
        digest = hashlib.sha256(self.url.encode("utf-8")).hexdigest()[:16]
        self.cache_dir = cache_dir or os.path.join(tempfile.gettempdir(), "minigitops", digest + ".git")
        self.runner = runner
        self.calls: List[str] = []

    def call(self, args: List[str]) -> Tuple[int, str, str]:
        self.calls.append(args[0])
        return self.runner(["git"] + args, None)

    def run(self, *args: str) -> str:
        code, out, err = self.call(["--git-dir", self.cache_dir] + list(args))
        if code != 0:
            raise GitError(f"git {args[0]}: {first_line(err) or f'exit {code}'}")
        return out

    def resolve(self, ref: str) -> str:
        if SHA_RE.match(ref):
            return ref
        full = ref if ref.startswith("refs/") else f"refs/heads/{ref}"
        code, out, err = self.call(["ls-remote", self.url, full])
        if code != 0:
            raise GitError(f"cannot read {self.url}: {first_line(err) or f'git ls-remote exit {code}'}")
        for line in out.splitlines():
            sha, _, name = line.partition("\t")
            if name == full:
                return sha
        raise GitError(f"branch {ref!r} not found in {self.url}")

    def has(self, sha: str) -> bool:
        return os.path.isdir(self.cache_dir) and self.call(["--git-dir", self.cache_dir, "cat-file", "-e", f"{sha}^{{commit}}"])[0] == 0

    def fetch(self, sha: str) -> None:
        if self.has(sha):
            return
        if not os.path.isdir(self.cache_dir):
            os.makedirs(os.path.dirname(self.cache_dir), exist_ok=True)
            code, _, err = self.call(["init", "--quiet", "--bare", self.cache_dir])
            if code != 0:
                raise GitError(f"git init {self.cache_dir}: {err.strip()}")
        self.run("fetch", "--quiet", "--no-tags", self.url, "+refs/heads/*:refs/remotes/origin/*")
        if not self.has(sha):
            raise GitError(f"revision {sha[:12]} is not on any branch of {self.url}")

    def commit(self, sha: str) -> Obj:
        out = self.run("log", "-1", "--format=%H%x00%an%x00%cI%x00%s", sha).rstrip("\n")
        parts = out.split("\0") + ["", "", "", ""]
        return {"sha": parts[0], "author": parts[1], "date": parts[2], "message": parts[3]}


class Source:
    def __init__(self, git: Git, branch: str, path: str) -> None:
        self.git = git
        self.branch = branch
        self.path = normal_path(path)
        self.cache: Dict[str, List[Obj]] = {}
        self.commits: Dict[str, Obj] = {}

    def resolve(self) -> str:
        return self.git.resolve(self.branch)

    def manifests(self, revision: str) -> List[Obj]:
        if revision not in self.cache:
            self.git.fetch(revision)
            self.cache[revision] = render(GitTree(self.git, revision), self.path)
        return copy.deepcopy(self.cache[revision])

    def commit(self, revision: str) -> Obj:
        if revision not in self.commits:
            self.git.fetch(revision)
            self.commits[revision] = self.git.commit(revision)
        return self.commits[revision]


class Kubectl:
    def __init__(self, context: Optional[str] = None, runner: Runner = run_command) -> None:
        self.context = context
        self.runner = runner

    def run(self, args: List[str], stdin: Optional[str] = None) -> str:
        argv = ["kubectl"] + (["--context", self.context] if self.context else []) + args
        code, out, err = self.runner(argv, stdin)
        if code != 0:
            message = err.strip() or out.strip() or f"exit {code}"
            raise (Conflict if "conflict" in message.lower() and args[0] == "apply" else ClusterError)(f"kubectl {args[0]}: {message}")
        return out

    def scope(self, obj: Obj) -> List[str]:
        namespace = (obj.get("metadata") or {}).get("namespace")
        return ["-n", namespace] if namespace and obj.get("kind") not in CLUSTER_SCOPED else []

    def get(self, obj: Obj) -> Optional[Obj]:
        out = self.run(["get", resource_name(obj), obj["metadata"]["name"]] + self.scope(obj) + ["-o", "json", "--show-managed-fields", "--ignore-not-found"])
        return json.loads(out) if out.strip() else None

    def list(self, namespace: str, selector: str, kinds: Sequence[str]) -> List[Obj]:
        out = self.run(["get", ",".join(kinds), "-n", namespace, "-l", selector, "-o", "json", "--show-managed-fields"])
        return json.loads(out).get("items") or [] if out.strip() else []

    def apply(self, obj: Obj, manager: str = FIELD_MANAGER, force: bool = True) -> Obj:
        args = ["apply", "--server-side", f"--field-manager={manager}"] + (["--force-conflicts"] if force else []) + ["-o", "json", "-f", "-"]
        return json.loads(self.run(args, json.dumps(obj)))

    def create(self, obj: Obj) -> Obj:
        return json.loads(self.run(["create", "-o", "json", "-f", "-"], json.dumps(obj)))

    def delete(self, obj: Obj, wait: bool = False) -> None:
        args = ["delete", resource_name(obj), obj["metadata"]["name"]] + self.scope(obj) + ["--ignore-not-found", "--cascade=background", f"--wait={'true' if wait else 'false'}"]
        self.run(args + (["--timeout=120s"] if wait else []))


def strip_server(obj: Obj) -> Obj:
    clean = copy.deepcopy(obj)
    clean.pop("status", None)
    for key in SERVER_FIELDS:
        clean.setdefault("metadata", {}).pop(key, None)
    return clean


class FakeCluster:
    def __init__(self, rollout_ticks: int = 2, job_ticks: int = 1, deadline_ticks: int = 5) -> None:
        self.rollout_ticks = rollout_ticks
        self.job_ticks = job_ticks
        self.deadline_ticks = deadline_ticks
        self.objects: Dict[Tuple[str, str, str, str], Obj] = {}
        self.owners: Dict[Tuple[str, str, str, str], Dict[str, Tuple[str, Set[Path]]]] = {}
        self.changed: Dict[Tuple[str, str, str, str], int] = {}
        self.clock = 0
        self.revision = 0
        self.applies: List[str] = []

    def rendered(self, key: Tuple[str, str, str, str]) -> Obj:
        obj = copy.deepcopy(self.objects[key])
        obj["metadata"]["managedFields"] = [
            {"manager": manager, "operation": operation, "apiVersion": obj.get("apiVersion"), "fieldsType": "FieldsV1", "fieldsV1": fields_v1(sorted(paths))}
            for manager, (operation, paths) in sorted(self.owners.get(key, {}).items()) if paths
        ]
        return obj

    def get(self, obj: Obj) -> Optional[Obj]:
        key = ident(obj)
        return self.rendered(key) if key in self.objects else None

    def list(self, namespace: str, selector: str, kinds: Sequence[str]) -> List[Obj]:
        label, _, value = selector.partition("=")
        return [self.rendered(k) for k, o in sorted(self.objects.items()) if k[2] == namespace and (o["metadata"].get("labels") or {}).get(label) == value]

    def store(self, key: Tuple[str, str, str, str], obj: Obj, previous: Optional[Obj]) -> Obj:
        self.revision += 1
        apply_defaults(obj, self.revision)
        meta = obj["metadata"]
        if previous is None:
            meta.update({"uid": f"uid-{self.revision:04d}", "creationTimestamp": "2026-11-01T00:00:00Z", "generation": 1})
            self.changed[key] = self.clock
        else:
            for name in ("uid", "creationTimestamp"):
                meta[name] = previous["metadata"][name]
            spec_changed = obj.get("spec") != previous.get("spec")
            meta["generation"] = previous["metadata"]["generation"] + (1 if spec_changed else 0)
            if spec_changed:
                self.changed[key] = self.clock
            if previous.get("status") is not None:
                obj["status"] = previous["status"]
        meta["resourceVersion"] = str(self.revision)
        self.objects[key] = obj
        return self.rendered(key)

    def apply(self, obj: Obj, manager: str = FIELD_MANAGER, force: bool = True) -> Obj:
        desired = strip_server(obj)
        key = ident(desired)
        paths = leaf_paths(desired)
        owners = self.owners.setdefault(key, {})
        current = self.objects.get(key)
        self.applies.append(describe(desired))
        if current is None:
            merged = desired
        else:
            conflicts = [(other, path) for other, (_, owned) in owners.items() if other != manager for path, value in paths.items()
                         if path in owned and resolve(current, path) is not MISSING and not same_value(path, value, resolve(current, path))]
            if conflicts and not force:
                raise Conflict("Apply failed with %d conflict(s): %s" % (len(conflicts), "; ".join(f'conflict with "{m}": .{pretty_path(p)}' for m, p in conflicts)))
            for other, path in conflicts:
                owners[other][1].discard(path)
            merged = strip_server(current)
            previous = owners.get(manager, ("Apply", set()))[1]
            others = set().union(*(owned for m, (_, owned) in owners.items() if m != manager))
            for path in sorted(previous, key=len, reverse=True):
                if path not in paths and path not in others:
                    delete_path(merged, path)
            merged = strategic_merge(merged, desired, ())
        owners[manager] = ("Apply", set(paths))
        return self.store(key, merged, current)

    def update(self, ref: Obj, mutate: Callable[[Obj], None], manager: str = "kubectl") -> Obj:
        key = ident(ref)
        if key not in self.objects:
            raise ClusterError(f"NotFound: {describe(ref)}")
        current = self.objects[key]
        changed = strip_server(current)
        mutate(changed)
        before, after = leaf_paths(strip_server(current)), leaf_paths(changed)
        touched = {p for p, v in after.items() if p not in before or before[p] != v}
        owners = self.owners.setdefault(key, {})
        for other, (_, owned) in owners.items():
            if other != manager:
                owned.difference_update(touched)
        operation, owned = owners.get(manager, ("Update", set()))
        owners[manager] = (operation, owned | touched)
        return self.store(key, changed, current)

    def scale(self, ref: Obj, replicas: int, manager: str = "kubectl") -> Obj:
        return self.update(ref, lambda o: o["spec"].__setitem__("replicas", replicas), manager)

    def create(self, obj: Obj) -> Obj:
        desired = strip_server(obj)
        key = ident(desired)
        if key in self.objects:
            raise ClusterError(f'kubectl create: AlreadyExists: {desired["kind"].lower()} "{desired["metadata"]["name"]}" already exists')
        self.owners[key] = {"kubectl-create": ("Update", set(leaf_paths(desired)))}
        return self.store(key, desired, None)

    def delete(self, obj: Obj, wait: bool = False) -> None:
        key = ident(obj)
        self.objects.pop(key, None)
        self.owners.pop(key, None)

    def tick(self) -> None:
        self.clock += 1
        for key, obj in self.objects.items():
            elapsed = self.clock - self.changed.get(key, self.clock)
            images = [str(c.get("image", "")) for spec in pod_specs(obj) for c in containers(spec)]
            broken = any(image.endswith(":broken") for image in images)
            if obj["kind"] == "Deployment":
                obj["status"] = deployment_status(obj, elapsed, broken, self.rollout_ticks, self.deadline_ticks)
            elif obj["kind"] == "Job" and elapsed >= self.job_ticks:
                kind, message = ("Failed", "Job has reached the specified backoff limit") if broken else ("Complete", "")
                obj["status"] = {"conditions": [{"type": kind, "status": "True", "message": message}], ("failed" if broken else "succeeded"): 1}


def deployment_status(obj: Obj, elapsed: int, broken: bool, rollout_ticks: int, deadline_ticks: int) -> Obj:
    replicas = obj["spec"].get("replicas", 1)
    generation = obj["metadata"]["generation"]
    previous = obj.get("status") or {}
    old = previous.get("updatedReplicas", 0) if previous.get("observedGeneration") == generation else previous.get("availableReplicas", 0)
    if broken:
        reason = "ProgressDeadlineExceeded" if elapsed >= deadline_ticks else "ReplicaSetUpdated"
        return {"observedGeneration": generation, "replicas": replicas + 1, "updatedReplicas": 1, "readyReplicas": replicas, "availableReplicas": replicas,
                "conditions": [{"type": "Progressing", "status": "False" if elapsed >= deadline_ticks else "True", "reason": reason}]}
    if elapsed >= rollout_ticks:
        return {"observedGeneration": generation, "replicas": replicas, "updatedReplicas": replicas, "readyReplicas": replicas, "availableReplicas": replicas,
                "conditions": [{"type": "Available", "status": "True", "reason": "MinimumReplicasAvailable"}, {"type": "Progressing", "status": "True", "reason": "NewReplicaSetAvailable"}]}
    updated = min(replicas, max(1, replicas * elapsed // max(1, rollout_ticks)))
    return {"observedGeneration": generation, "replicas": max(replicas, old + updated), "updatedReplicas": updated, "readyReplicas": updated, "availableReplicas": updated,
            "conditions": [{"type": "Progressing", "status": "True", "reason": "ReplicaSetUpdated"}]}


def apply_defaults(obj: Obj, serial: int) -> None:
    kind = obj.get("kind")
    spec = obj.get("spec") if isinstance(obj.get("spec"), dict) else None
    if kind == "Deployment" and spec is not None:
        spec.setdefault("replicas", 1)
        spec.setdefault("revisionHistoryLimit", 10)
        spec.setdefault("progressDeadlineSeconds", 600)
        spec.setdefault("strategy", {"type": "RollingUpdate", "rollingUpdate": {"maxSurge": "25%", "maxUnavailable": "25%"}})
    if kind == "Service" and spec is not None:
        spec.setdefault("type", "ClusterIP")
        spec.setdefault("sessionAffinity", "None")
        spec.setdefault("clusterIP", f"10.96.{serial // 250}.{serial % 250 + 1}")
        for port in spec.get("ports") or []:
            port.setdefault("protocol", "TCP")
            port.setdefault("targetPort", port.get("port"))
    if kind == "Job" and spec is not None:
        spec.setdefault("backoffLimit", 6)
        spec.setdefault("completions", 1)
        spec.setdefault("parallelism", 1)
    for pod in pod_specs(obj):
        pod.setdefault("restartPolicy", "Always")
        pod.setdefault("dnsPolicy", "ClusterFirst")
        pod.setdefault("schedulerName", "default-scheduler")
        for container in containers(pod):
            container.setdefault("imagePullPolicy", "IfNotPresent")
            container.setdefault("terminationMessagePath", "/dev/termination-log")
            for port in container.get("ports") or []:
                port.setdefault("protocol", "TCP")


@dataclass
class App:
    name: str
    namespace: str
    repo: str
    branch: str = "main"
    path: str = "."
    prune: bool = False
    self_heal: bool = False
    automated: bool = True
    tracking_label: str = TRACKING_LABEL
    field_manager: str = FIELD_MANAGER


@dataclass
class ResourceState:
    desired: Obj
    live: Optional[Obj]
    status: str
    health: str
    message: str
    diffs: List[Difference] = field(default_factory=list)

    def summary(self) -> Obj:
        return {
            "group": group_of(self.desired), "kind": self.desired["kind"], "namespace": self.desired["metadata"].get("namespace", ""),
            "name": self.desired["metadata"]["name"], "wave": wave_of(self.desired), "status": self.status, "health": self.health,
            "message": self.message, "diff": [d.to_dict() for d in self.diffs], "requiresPruning": False,
        }


@dataclass
class Comparison:
    revision: str
    resources: List[ResourceState]
    hooks: List[Obj]
    prune: List[Obj]

    @property
    def out_of_sync(self) -> List[ResourceState]:
        return [r for r in self.resources if r.status == "OutOfSync"]

    @property
    def sync_status(self) -> str:
        return "OutOfSync" if self.out_of_sync or self.prune else "Synced"

    @property
    def health(self) -> str:
        return worst(r.health for r in self.resources)

    @property
    def health_message(self) -> str:
        for resource in self.resources:
            if resource.health == self.health and resource.message:
                return f"{describe(resource.desired)}: {resource.message}"
        return ""


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class Reconciler:
    def __init__(self, app: App, source: Source, cluster: Any, sleep: Callable[[float], None] = time.sleep,
                 log: Callable[[str], None] = print, health_timeout: float = 120.0, poll_interval: float = 1.0,
                 now: Callable[[], str] = utc_now) -> None:
        self.app = app
        self.source = source
        self.cluster = cluster
        self.sleep = sleep
        self.log = log
        self.health_timeout = health_timeout
        self.poll_interval = poll_interval
        self.now = now
        self.operation: Optional[Obj] = None
        self.history: List[Obj] = []
        self.last_attempt: Optional[str] = None
        self.note = ""
        self.status: Obj = {}

    def prepare(self, obj: Obj) -> Obj:
        prepared = copy.deepcopy(obj)
        meta = prepared["metadata"]
        meta["labels"] = dict(meta.get("labels") or {}, **{self.app.tracking_label: self.app.name})
        if prepared["kind"] not in CLUSTER_SCOPED and not meta.get("namespace"):
            meta["namespace"] = self.app.namespace
        return prepared

    def desired(self, revision: str) -> Tuple[List[Obj], List[Obj]]:
        resources: List[Obj] = []
        hooks: List[Obj] = []
        for obj in self.source.manifests(revision):
            types = hook_types(obj)
            if "Skip" in types:
                continue
            (hooks if types else resources).append(self.prepare(obj))
        return resources, hooks

    def compare(self, revision: str) -> Comparison:
        resources, hooks = self.desired(revision)
        states: List[ResourceState] = []
        for obj in resources:
            live = self.cluster.get(obj)
            diffs = diff(obj, live, self.app.field_manager) if live is not None else []
            health, message = assess(live)
            states.append(ResourceState(obj, live, "OutOfSync" if live is None or diffs else "Synced", health, message, diffs))
        wanted = {ident(o) for o in resources + hooks}
        kinds = sorted(set(PRUNE_KINDS) | {resource_name(o) for o in resources if o["kind"] not in CLUSTER_SCOPED})
        tracked = self.cluster.list(self.app.namespace, f"{self.app.tracking_label}={self.app.name}", kinds)
        prune = [o for o in tracked if ident(o) not in wanted and not annotations_of(o).get(HOOK) and not o["metadata"].get("deletionTimestamp")]
        return Comparison(revision, states, hooks, prune)

    def decide(self, comparison: Comparison) -> Optional[str]:
        self.note = ""
        if comparison.sync_status == "Synced":
            self.last_attempt = comparison.revision
            return None
        if not self.app.automated:
            self.note = "automated sync is off; run sync"
            return None
        if not comparison.out_of_sync and not self.app.prune:
            self.note = f"{len(comparison.prune)} resource(s) need pruning; automated prune is off"
            return None
        if comparison.revision != self.last_attempt:
            return "initial sync" if self.last_attempt is None else "new revision"
        if self.app.self_heal:
            return "self-heal"
        self.note = f"live state differs from Git at {comparison.revision[:7]}, which was already synced; self-heal is off"
        return None

    def record(self, op: Obj, obj: Obj, step: str, action: str) -> None:
        op["resources"].append({"step": step, "kind": obj["kind"], "name": obj["metadata"]["name"], "action": action})
        self.log(f"sync      {step:<9} {describe(obj):<34} {action}")

    def wait_healthy(self, objects: Sequence[Obj], label: str) -> None:
        attempts = max(1, int(self.health_timeout / max(self.poll_interval, 0.001)))
        pending: List[str] = []
        for attempt in range(attempts + 1):
            pending = []
            for obj in objects:
                status, message = assess(self.cluster.get(obj))
                if status == "Degraded":
                    raise SyncFailure(f"{label}: {describe(obj)} is Degraded: {message}")
                if status not in ("Healthy", "Suspended"):
                    pending.append(f"{describe(obj)} {status}: {message}")
            if not pending or attempt == attempts:
                break
            self.sleep(self.poll_interval)
        if pending:
            raise SyncFailure(f"{label}: timed out after {self.health_timeout:g}s waiting for " + "; ".join(pending))

    def run_hooks(self, phase: str, hooks: Sequence[Obj], op: Obj, created: List[Obj]) -> None:
        selected = sorted((h for h in hooks if phase in hook_types(h)), key=sync_key)
        for wave in sorted({wave_of(h) for h in selected}):
            group = [h for h in selected if wave_of(h) == wave]
            for hook in group:
                if "BeforeHookCreation" in delete_policies(hook):
                    self.cluster.delete(hook, wait=True)
                self.cluster.create(hook)
                created.append(hook)
                self.record(op, hook, phase, "hook created")
            self.wait_healthy(group, f"{phase} hooks")
            for hook in group:
                self.record(op, hook, phase, "hook Healthy")

    def sync(self, comparison: Comparison, reason: str = "manual", prune: Optional[bool] = None) -> Obj:
        prune = self.app.prune if prune is None else prune
        op: Obj = {"revision": comparison.revision, "reason": reason, "phase": "Running", "startedAt": self.now(), "resources": [], "message": ""}
        self.last_attempt = comparison.revision
        created: List[Obj] = []
        try:
            self.run_hooks("PreSync", comparison.hooks, op, created)
            sync_hooks = [h for h in comparison.hooks if "Sync" in hook_types(h)]
            for wave in sorted({wave_of(r.desired) for r in comparison.resources} | {wave_of(h) for h in sync_hooks}):
                batch = sorted((r for r in comparison.resources if wave_of(r.desired) == wave), key=lambda r: sync_key(r.desired))
                for resource in batch:
                    if resource.status == "OutOfSync":
                        self.cluster.apply(resource.desired, self.app.field_manager, True)
                        self.record(op, resource.desired, f"wave {wave}", "created" if resource.live is None else "configured")
                for hook in sorted((h for h in sync_hooks if wave_of(h) == wave), key=sync_key):
                    if "BeforeHookCreation" in delete_policies(hook):
                        self.cluster.delete(hook, wait=True)
                    self.cluster.create(hook)
                    created.append(hook)
                    self.record(op, hook, f"wave {wave}", "hook created")
                self.wait_healthy([r.desired for r in batch] + [h for h in sync_hooks if wave_of(h) == wave], f"wave {wave}")
            for obj in sorted(comparison.prune, key=lambda o: (-wave_of(o), -kind_rank(str(o["kind"])), o["metadata"]["name"])):
                if not prune:
                    self.record(op, obj, "prune", "skipped (prune is off)")
                elif "Prune=false" in sync_options(obj):
                    self.record(op, obj, "prune", "skipped (Prune=false)")
                else:
                    self.cluster.delete(obj, wait=False)
                    self.record(op, obj, "prune", "pruned")
            self.run_hooks("PostSync", comparison.hooks, op, created)
            op["phase"], op["message"] = "Succeeded", "successfully synced (all tasks run)"
        except (SyncFailure, ClusterError) as err:
            op["phase"], op["message"] = "Failed", str(err)
            self.log(f"sync      failed    {err}")
            try:
                self.run_hooks("SyncFail", comparison.hooks, op, created)
            except GitOpsError as hook_err:
                self.log(f"sync      SyncFail  {hook_err}")
        policy = "HookSucceeded" if op["phase"] == "Succeeded" else "HookFailed"
        for hook in created:
            if policy in delete_policies(hook):
                self.cluster.delete(hook, wait=False)
                self.record(op, hook, "cleanup", f"hook deleted ({policy})")
        op["finishedAt"] = self.now()
        self.operation = op
        self.history.append({"revision": op["revision"], "phase": op["phase"], "reason": reason, "finishedAt": op["finishedAt"]})
        return op

    def reconcile(self) -> Obj:
        try:
            revision = self.source.resolve()
            comparison = self.compare(revision)
        except GitOpsError as err:
            self.log(f"compare   error     {err}")
            self.status = dict(self.status or self.base_status(), conditions=[{"type": "ComparisonError", "message": str(err)}])
            return self.status
        reason = self.decide(comparison)
        self.log(f"compare   {revision[:7]}   {comparison.sync_status}/{comparison.health}" + (f"; syncing ({reason})" if reason else (f"; {self.note}" if self.note else "")))
        for resource in comparison.out_of_sync:
            for difference in resource.diffs:
                self.log(f"drift     {describe(resource.desired):<34} {difference.text()}")
        for obj in comparison.prune:
            self.log(f"drift     {describe(obj):<34} live with {self.app.tracking_label}={self.app.name}, not in Git")
        if reason:
            self.sync(comparison, reason)
            comparison = self.compare(revision)
        self.status = self.render_status(comparison)
        return self.status

    def base_status(self) -> Obj:
        return {"app": self.app.name, "namespace": self.app.namespace, "repo": self.source.git.url, "branch": self.source.branch, "path": self.source.path or "."}

    def render_status(self, comparison: Comparison) -> Obj:
        resources = [r.summary() for r in comparison.resources]
        resources += [{"group": group_of(o), "kind": o["kind"], "namespace": o["metadata"].get("namespace", ""), "name": o["metadata"]["name"],
                       "wave": wave_of(o), "status": "OutOfSync", "health": "", "message": "requires pruning", "diff": [], "requiresPruning": True}
                      for o in comparison.prune]
        return dict(self.base_status(), **{
            "revision": comparison.revision, "commit": self.source.commit(comparison.revision),
            "sync": {"status": comparison.sync_status, "revision": comparison.revision},
            "health": {"status": comparison.health, "message": comparison.health_message},
            "resources": resources, "hooks": [f"{','.join(hook_types(h))} {describe(h)}" for h in comparison.hooks],
            "operation": self.operation, "history": self.history[-10:], "note": self.note, "conditions": [],
        })

    def restore(self, status_file: str) -> None:
        try:
            with open(status_file, encoding="utf-8") as handle:
                saved = json.load(handle)
        except (OSError, ValueError):
            return
        if saved.get("app") == self.app.name and saved.get("namespace") == self.app.namespace:
            self.operation = saved.get("operation")
            self.history = saved.get("history") or []
            self.last_attempt = (self.operation or {}).get("revision")

    def watch(self, iterations: int = 0, interval: float = 3.0, status_file: Optional[str] = None) -> Obj:
        if status_file:
            self.restore(status_file)
        count = 0
        while True:
            status = self.reconcile()
            if status_file:
                write_json(status_file, status)
            count += 1
            if iterations and count >= iterations:
                return status
            self.sleep(interval)


def write_json(path: str, data: Obj) -> None:
    temporary = path + ".tmp"
    with open(temporary, "w", encoding="utf-8") as handle:
        json.dump(data, handle, indent=2)
    os.replace(temporary, path)


def format_status(status: Obj) -> str:
    lines = [
        f"Name:      {status['app']}",
        f"Repo:      {status['repo']}",
        f"Path:      {status['path']} (branch {status['branch']})",
    ]
    commit = status.get("commit") or {}
    if status.get("revision"):
        lines.append(f"Revision:  {status['revision'][:7]} {commit.get('message', '')}".rstrip())
        lines.append(f"Sync:      {status['sync']['status']}")
        lines.append(f"Health:    {status['health']['status']}" + (f" ({status['health']['message']})" if status['health']['message'] else ""))
    for item in status.get("conditions") or []:
        lines.append(f"Condition: {item['type']}: {item['message']}")
    if status.get("note"):
        lines.append(f"Note:      {status['note']}")
    if status.get("resources"):
        lines += ["", f"{'KIND':<12} {'NAME':<28} {'WAVE':>4}  {'STATUS':<10} {'HEALTH':<12} MESSAGE"]
        for item in status["resources"]:
            lines.append(f"{item['kind']:<12} {item['name']:<28} {item['wave']:>4}  {item['status']:<10} {item['health'] or '-':<12} {item['message']}".rstrip())
            for difference in item["diff"]:
                lines.append("    " + Difference(difference["path"], difference["change"], difference["desired"], difference["live"]).text())
    if status.get("hooks"):
        lines.append("Hooks:     " + ", ".join(status["hooks"]))
    operation = status.get("operation")
    if operation:
        lines += ["", f"Operation: {operation['phase']} ({operation['reason']}) at {operation['revision'][:7]}: {operation['message']}"]
        lines += [f"  {r['step']:<9} {r['kind'] + '/' + r['name']:<34} {r['action']}" for r in operation["resources"]]
    return "\n".join(lines)


def git_run(args: List[str], cwd: str) -> str:
    code, out, err = run_command(["git", "-C", cwd, "-c", "user.name=minigitops", "-c", "user.email=demo@example.invalid", "-c", "init.defaultBranch=main"] + args)
    if code != 0:
        raise GitError(f"git {args[0]}: {err.strip()}")
    return out


def replace_in_file(path: str, old: str, new: str) -> None:
    with open(path, encoding="utf-8") as handle:
        text = handle.read()
    if old not in text:
        raise InputError(f"{path}: {old!r} not found")
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(text.replace(old, new))


def demo_repo(root: str) -> Tuple[str, str]:
    origin, work = os.path.join(root, "app.git"), os.path.join(root, "work")
    os.makedirs(work)
    git_run(["init", "--quiet", "--bare", origin], root)
    git_run(["init", "--quiet", "-b", "main"], work)
    shutil.copytree(FIXTURE_APP, work, dirs_exist_ok=True)
    commit_all(work, "initial app")
    return origin, work


def commit_all(work: str, message: str) -> str:
    git_run(["add", "-A"], work)
    git_run(["commit", "--quiet", "-m", message], work)
    git_run(["push", "--quiet", os.path.join(os.path.dirname(work), "app.git"), "main"], work)
    return git_run(["rev-parse", "HEAD"], work).strip()


def bump_image(work: str) -> str:
    replace_in_file(os.path.join(work, "overlays", "prod", "kustomization.yaml"), 'newTag: "3.9"', 'newTag: "3.10"')
    return commit_all(work, "bump web to pause 3.10")


def drop_legacy_flags(work: str) -> str:
    replace_in_file(os.path.join(work, "base", "kustomization.yaml"), "  - legacy-flags.yaml\n", "")
    os.remove(os.path.join(work, "base", "legacy-flags.yaml"))
    return commit_all(work, "remove legacy feature flags")


def demo(self_heal: bool = True) -> Obj:
    if shutil.which("git") is None:
        raise InputError("demo needs git on PATH")
    with tempfile.TemporaryDirectory() as root:
        origin, work = demo_repo(root)
        cluster = FakeCluster()
        events: List[str] = []
        app = App(name="shop", namespace="prod", repo=origin, path="overlays/prod", prune=True, self_heal=self_heal)
        reconciler = Reconciler(app, Source(Git(origin, os.path.join(root, "cache.git")), app.branch, app.path), cluster,
                                sleep=lambda _: cluster.tick(), log=lambda m: events.append(f"t={cluster.clock:02d}  {m}"),
                                health_timeout=20, poll_interval=1, now=lambda: f"t={cluster.clock:02d}")
        deployment = {"apiVersion": "apps/v1", "kind": "Deployment", "metadata": {"name": "prod-web", "namespace": "prod"}}
        legacy = {"apiVersion": "v1", "kind": "ConfigMap", "metadata": {"name": "prod-legacy-flags", "namespace": "prod"}}
        steps: List[Obj] = []

        def step(name: str) -> None:
            events.append(f"t={cluster.clock:02d}  ---- {name}")
            status = reconciler.reconcile()
            live = cluster.get(deployment) or {}
            container = ((((live.get("spec") or {}).get("template") or {}).get("spec") or {}).get("containers") or [{}])[0]
            steps.append({"step": name, "revision": status["revision"], "sync": status["sync"]["status"], "health": status["health"]["status"],
                          "operation": (status.get("operation") or {}).get("reason"), "replicas": (live.get("spec") or {}).get("replicas"),
                          "image": container.get("image"), "legacy_flags": cluster.get(legacy) is not None})

        step("initial commit")
        bump_image(work)
        step("new commit: image tag 3.9 -> 3.10")
        drop_legacy_flags(work)
        step("new commit: legacy-flags.yaml removed")
        def managers() -> List[Tuple[str, str, List[str]]]:
            entries = (cluster.get(deployment) or {}).get("metadata", {}).get("managedFields", [])
            return [(m["manager"], m["operation"], [pretty_path(p) for p in paths_of_fields(m["fieldsV1"]) if p[:2] == ("f:spec", "f:replicas")]) for m in entries]

        cluster.scale(deployment, 5)
        events.append(f"t={cluster.clock:02d}  kubectl   scale     Deployment/prod-web                --replicas=5 (by hand)")
        after_scale = managers()
        step("drift: kubectl scale --replicas=5")
        return {"events": events, "steps": steps, "managers_after_scale": after_scale, "managers": managers(), "history": reconciler.history, "status": reconciler.status}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="minigitops", description="A GitOps reconciler: Git branch -> rendered manifests -> diff -> server-side apply, prune, health.")
    commands = parser.add_subparsers(dest="command", required=True)
    render_cmd = commands.add_parser("render", help="print the manifests an application path renders to")
    where = render_cmd.add_mutually_exclusive_group(required=True)
    where.add_argument("--repo", help="Git repository URL or path to a (bare) repository")
    where.add_argument("--dir", help="a working directory instead of a Git repository")
    render_cmd.add_argument("--revision", default="main", help="branch or commit SHA (default main)")
    render_cmd.add_argument("--path", default=".", help="application path inside the repository")
    render_cmd.add_argument("--cache", help="local cache repository for fetched objects")
    render_cmd.add_argument("--json", action="store_true", help="print a JSON List instead of YAML")
    for name, text in (("status", "compare Git with the cluster; exit 1 unless Synced and Healthy"), ("sync", "sync once: hooks, waves, apply, prune"), ("watch", "poll the branch and reconcile in a loop")):
        cmd = commands.add_parser(name, help=text)
        cmd.add_argument("--repo", required=True)
        cmd.add_argument("--branch", default="main")
        cmd.add_argument("--path", default=".")
        cmd.add_argument("--app", required=True, help="application name, the tracking label value")
        cmd.add_argument("--namespace", required=True, help="destination namespace")
        cmd.add_argument("--context", help="kubectl context")
        cmd.add_argument("--cache")
        cmd.add_argument("--tracking-label", default=TRACKING_LABEL)
        cmd.add_argument("--field-manager", default=FIELD_MANAGER)
        cmd.add_argument("--health-timeout", type=float, default=180.0)
        cmd.add_argument("--json", action="store_true")
        if name != "status":
            cmd.add_argument("--prune", action="store_true", help="delete tracked objects that left Git")
        if name == "watch":
            cmd.add_argument("--self-heal", action="store_true", help="sync again when the live state drifts at the same revision")
            cmd.add_argument("--interval", type=float, default=3.0)
            cmd.add_argument("--iterations", type=int, default=0, help="stop after N reconciles (0 = forever)")
            cmd.add_argument("--status-file", help="write the app status here after every reconcile")
    demo_cmd = commands.add_parser("demo", help="the whole loop on an in-memory cluster and a temporary Git repository")
    demo_cmd.add_argument("--no-self-heal", action="store_true")
    demo_cmd.add_argument("--json", action="store_true")
    return parser


def stamp(message: str) -> None:
    print(f"{utc_now()}  {message}", flush=True)


def main(argv: Optional[List[str]] = None, runner: Runner = run_command, sleep: Callable[[float], None] = time.sleep) -> int:
    parser = build_parser()
    try:
        args = parser.parse_args(sys.argv[1:] if argv is None else argv)
    except SystemExit as exit_:
        return int(exit_.code or 0)
    try:
        if args.command == "render":
            if args.dir:
                if not os.path.isdir(args.dir):
                    raise InputError(f"--dir {args.dir}: not a directory")
                objects = render(DirTree(args.dir), args.path)
            else:
                git = Git(args.repo, args.cache, runner)
                revision = git.resolve(args.revision)
                git.fetch(revision)
                objects = render(GitTree(git, revision), args.path)
            print(json.dumps({"apiVersion": "v1", "kind": "List", "items": objects}, indent=2) if args.json else to_yaml_documents(objects), end="" if not args.json else "\n")
            return 0
        if args.command == "demo":
            result = demo(self_heal=not args.no_self_heal)
            if args.json:
                print(json.dumps(result, indent=2))
            else:
                print("\n".join(result["events"]))
                print("\n" + "\n".join(f"{s['step']:<40} {s['revision'][:7]} {s['sync']}/{s['health']} replicas={s['replicas']} image={s['image']} legacy-flags={'present' if s['legacy_flags'] else 'gone'}" for s in result["steps"]))
                for label, entries in (("after kubectl scale", result["managers_after_scale"]), ("after self-heal", result["managers"])):
                    print(f"managedFields on prod-web {label}: " + ", ".join(f"{m} ({op}{', owns spec.replicas' if owned else ''})" for m, op, owned in entries))
            return 0
        app = App(name=args.app, namespace=args.namespace, repo=args.repo, branch=args.branch, path=args.path,
                  prune=getattr(args, "prune", False), self_heal=getattr(args, "self_heal", False),
                  automated=args.command == "watch", tracking_label=args.tracking_label, field_manager=args.field_manager)
        quiet = args.json or args.command == "status"
        reconciler = Reconciler(app, Source(Git(args.repo, args.cache, runner), args.branch, args.path), Kubectl(args.context, runner),
                                sleep=sleep, log=(lambda _: None) if quiet else stamp, health_timeout=args.health_timeout, poll_interval=2.0)
        if args.command == "watch":
            reconciler.watch(args.iterations, args.interval, args.status_file)
            return 0
        revision = reconciler.source.resolve()
        comparison = reconciler.compare(revision)
        if args.command == "sync":
            reconciler.sync(comparison, "manual sync", prune=args.prune)
            comparison = reconciler.compare(revision)
        status = reconciler.render_status(comparison)
        print(json.dumps(status, indent=2) if args.json else format_status(status))
        if args.command == "sync" and status["operation"]["phase"] != "Succeeded":
            return 1
        return 0 if status["sync"]["status"] == "Synced" and status["health"]["status"] == "Healthy" else 1
    except GitOpsError as err:
        print(f"minigitops: {err}", file=sys.stderr)
        return err.exit_code
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
