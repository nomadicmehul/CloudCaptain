"""minihelm: load a chart, merge values, render a subset of Go templates into a manifest, and keep Helm-compatible release records (sh.helm.release.v1.NAME.vN Secrets holding base64 of gzipped JSON) with install, upgrade, rollback, history and uninstall over a pluggable store and applier."""
from __future__ import annotations

import argparse
import base64
import copy
import gzip
import hashlib
import inspect
import json
import math
import os
import re
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple

STORAGE_PREFIX = "sh.helm.release.v1"
SECRET_TYPE = "helm.sh/release.v1"
GZIP_MAGIC = b"\x1f\x8b\x08"
HOOK_ANNOTATION = "helm.sh/hook"
RELEASE_NAME_RE = re.compile(r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?(\.[a-z0-9]([-a-z0-9]*[a-z0-9])?)*$")
MAX_RELEASE_NAME = 53
MAX_SET_INDEX = 65536
MAX_INCLUDE_DEPTH = 100
PENDING = ("pending-install", "pending-upgrade", "pending-rollback")
ERR_PENDING = "another operation (install/upgrade/rollback) is in progress"
INSTALL_ORDER = (
    "PriorityClass", "Namespace", "NetworkPolicy", "ResourceQuota", "LimitRange", "PodDisruptionBudget",
    "ServiceAccount", "Secret", "SecretList", "ConfigMap", "StorageClass", "PersistentVolume", "PersistentVolumeClaim",
    "CustomResourceDefinition", "ClusterRole", "ClusterRoleList", "ClusterRoleBinding", "ClusterRoleBindingList",
    "Role", "RoleList", "RoleBinding", "RoleBindingList", "Service", "DaemonSet", "Pod", "ReplicationController",
    "ReplicaSet", "Deployment", "HorizontalPodAutoscaler", "StatefulSet", "Job", "CronJob", "IngressClass", "Ingress",
    "APIService", "MutatingWebhookConfiguration", "ValidatingWebhookConfiguration",
)
METADATA_FIELDS = (
    "name", "home", "sources", "version", "description", "keywords", "maintainers", "icon", "apiVersion",
    "condition", "tags", "appVersion", "deprecated", "annotations", "kubeVersion", "dependencies", "type",
)
METADATA_STRINGS = ("name", "home", "version", "description", "icon", "apiVersion", "condition", "tags", "appVersion", "kubeVersion", "type")
SEMVER_RE = re.compile(r"^v?(0|[1-9]\d*)(\.(0|[1-9]\d*))?(\.(0|[1-9]\d*))?(-[0-9A-Za-z.-]+)?(\+[0-9A-Za-z.-]+)?$")


class MinihelmError(Exception):
    exit_code = 1


class InputError(MinihelmError):
    exit_code = 2


class YamlError(InputError):
    def __init__(self, line: int, message: str) -> None:
        super().__init__(f"line {line}: {message}")
        self.line = line


class ValuesError(InputError):
    pass


class ChartError(InputError):
    pass


class TemplateError(InputError):
    def __init__(self, message: str, located: bool = False) -> None:
        super().__init__(message)
        self.located = located


class ReleaseError(MinihelmError):
    pass


class ApplyError(MinihelmError):
    pass


class RequiredValue(Exception):
    pass


NULL_WORDS = {"", "~", "null", "Null", "NULL"}
TRUE_WORDS = {"y", "Y", "yes", "Yes", "YES", "true", "True", "TRUE", "on", "On", "ON"}
FALSE_WORDS = {"n", "N", "no", "No", "NO", "false", "False", "FALSE", "off", "Off", "OFF"}
INT_FORMS = (
    (re.compile(r"^[-+]?(0|[1-9][0-9_]*)$"), 10, ""),
    (re.compile(r"^[-+]?0x[0-9a-fA-F_]+$"), 16, "0x"),
    (re.compile(r"^[-+]?0o[0-7_]+$"), 8, "0o"),
    (re.compile(r"^[-+]?0b[01_]+$"), 2, "0b"),
    (re.compile(r"^[-+]?0[0-7_]+$"), 8, "0"),
)
FLOAT_RE = re.compile(r"^[-+]?(\.[0-9]+|[0-9]+(\.[0-9]*)?)([eE][-+]?[0-9]+)?$")
SPECIAL_FLOATS = {
    ".inf": math.inf, ".Inf": math.inf, ".INF": math.inf, "+.inf": math.inf, "+.Inf": math.inf, "+.INF": math.inf,
    "-.inf": -math.inf, "-.Inf": -math.inf, "-.INF": -math.inf, ".nan": math.nan, ".NaN": math.nan, ".NAN": math.nan,
}
BASE60_RE = re.compile(r"^[-+]?[0-9][0-9_]*(?::[0-5]?[0-9])+(?:\.[0-9_]*)?$")
BLOCK_HEADER_RE = re.compile(r"^([|>])([-+]?)([1-9]?)([-+]?)$")
DOC_SEPARATOR = re.compile(r"(?m)^---[ \t]*")
ESCAPES = {"n": "\n", "t": "\t", "r": "\r", '"': '"', "\\": "\\", "/": "/", "0": "\0", "a": "\a", "b": "\b", "f": "\f", "v": "\v", "e": "\x1b", " ": " "}


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
            digits = text.lstrip("+-")[len(prefix):].replace("_", "")
            if digits:
                return sign * int(digits, base)
    if FLOAT_RE.match(text):
        return float(text)
    if text in SPECIAL_FLOATS:
        return SPECIAL_FLOATS[text]
    return text


def key_string(value: Any) -> str:
    if isinstance(value, str):
        return value
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    return json_number(value)


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
        if "\t" in raw[: len(raw) - len(raw.lstrip())] and raw.strip():
            raise YamlError(number, "tabs are not allowed for indentation; use spaces")
        text = strip_comment(raw)
        if not text.strip() or text.strip() in ("---", "..."):
            continue
        indent = len(text) - len(text.lstrip(" "))
        lines.append(Line(number, indent, text.strip()))
    return lines


def parse_double_quoted(text: str, number: int) -> Tuple[str, str]:
    out: List[str] = []
    index = 1
    while index < len(text):
        char = text[index]
        if char == "\\":
            if index + 1 >= len(text):
                raise YamlError(number, "unfinished escape sequence")
            code = text[index + 1]
            if code in "xuU":
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


def parse_flow(text: str, number: int) -> Tuple[Any, str]:
    text = text.lstrip()
    closer = "]" if text.startswith("[") else "}"
    is_list = closer == "]"
    items: List[Any] = []
    mapping: Dict[str, Any] = {}
    rest = text[1:].lstrip()
    while True:
        if rest.startswith(closer):
            return (items if is_list else mapping), rest[1:].lstrip()
        if not rest:
            raise YamlError(number, f"unterminated flow collection; missing {closer}")
        if is_list:
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


def parse_scalar(text: str, number: int) -> Any:
    text = text.strip()
    if text.startswith(("&", "*")) or text.startswith("<<"):
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
            if not key:
                raise YamlError(number, "empty key")
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
            mapping.pop(key, None)
            self.position += 1
            if rest and BLOCK_HEADER_RE.match(rest):
                mapping[key] = self.block_scalar(rest, line.number, indent)
                continue
            if rest:
                mapping[key] = parse_scalar(rest, line.number)
                continue
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
        if chomp == "+":
            return text + "\n" * (1 + trailing)
        return text + "\n"


def fold_lines(lines: List[str]) -> str:
    out = [lines[0]]
    for previous, line in zip(lines, lines[1:]):
        if line == "":
            out.append("\n")
        elif previous == "":
            out.append(line)
        elif line.startswith(" ") or previous.startswith(" "):
            out.append("\n" + line)
        else:
            out.append(" " + line)
    return "".join(out)


def parse_yaml(source: str) -> Any:
    return YamlParser(source).parse_document()


def split_manifests(text: str) -> List[str]:
    return [doc.lstrip() for doc in DOC_SEPARATOR.split(text.lstrip()) if doc.strip()]


def parse_documents(text: str) -> List[Any]:
    return [doc for doc in (parse_yaml(piece) for piece in split_manifests(text)) if doc is not None]


def json_number(value: float) -> str:
    if math.isnan(value):
        return ".nan"
    if math.isinf(value):
        return ".inf" if value > 0 else "-.inf"
    if value == int(value) and abs(value) < 1e21:
        return str(int(value))
    return repr(value)


def needs_double_quotes(text: str) -> bool:
    if not isinstance(plain_scalar(text), str) or BASE60_RE.match(text):
        return True
    return any((ord(char) < 32 and char != "\n") or ord(char) == 127 for char in text)


def plain_allowed(text: str) -> bool:
    if text[0] in " \t" or text[-1] in " \t":
        return False
    if text[0] in "!&*%@`|>'\"{}[],#":
        return False
    if text[0] in "-?:" and (len(text) == 1 or text[1] == " "):
        return False
    return ": " not in text and " #" not in text and not text.endswith(":") and "\n" not in text


def double_quote(text: str) -> str:
    out = ['"']
    for char in text:
        if char in '"\\':
            out.append("\\" + char)
        elif char == "\n":
            out.append("\\n")
        elif char == "\t":
            out.append("\\t")
        elif char == "\r":
            out.append("\\r")
        elif ord(char) < 32 or ord(char) == 127:
            out.append(f"\\x{ord(char):02x}")
        else:
            out.append(char)
    return "".join(out) + '"'


def yaml_string(text: str) -> str:
    if text == "":
        return '""'
    if needs_double_quotes(text):
        return double_quote(text)
    if plain_allowed(text):
        return text
    if "\n" in text:
        return double_quote(text)
    return "'" + text.replace("'", "''") + "'"


def yaml_scalar(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return json_number(value)
    return yaml_string(str(value))


def literal_allowed(text: str) -> bool:
    if "\n" not in text or text[0] in " \t\n":
        return False
    if any((ord(char) < 32 and char != "\n") or ord(char) == 127 for char in text):
        return False
    return not any(line.endswith((" ", "\t")) for line in text.split("\n"))


def literal_block(text: str) -> Tuple[str, List[str]]:
    if text.endswith("\n\n"):
        return "|+", text[:-1].split("\n")
    if text.endswith("\n"):
        return "|", text[:-1].split("\n")
    return "|-", text.split("\n")


def emit_yaml(value: Any, indent: int) -> List[str]:
    pad = " " * indent
    if isinstance(value, dict):
        if not value:
            return [pad + "{}"]
        lines: List[str] = []
        for key in sorted(value, key=str):
            lines.extend(emit_entry(pad + yaml_string(str(key)) + ":", value[key], indent, True))
        return lines
    if isinstance(value, list):
        if not value:
            return [pad + "[]"]
        lines = []
        for item in value:
            lines.extend(emit_entry(pad + "-", item, indent, False))
        return lines
    if isinstance(value, str) and literal_allowed(value):
        header, body = literal_block(value)
        return [pad + header] + [(pad + "  " + line) if line else "" for line in body]
    return [pad + yaml_scalar(value)]


def emit_entry(prefix: str, child: Any, indent: int, in_mapping: bool) -> List[str]:
    if isinstance(child, (dict, list)) and child:
        if in_mapping and isinstance(child, dict):
            return [prefix] + emit_yaml(child, indent + 2)
        if in_mapping:
            return [prefix] + emit_yaml(child, indent)
        lines = emit_yaml(child, indent + 2)
        return [prefix + " " + lines[0][indent + 2 :]] + lines[1:]
    if isinstance(child, str) and literal_allowed(child):
        header, body = literal_block(child)
        return [prefix + " " + header] + [(" " * (indent + 2) + line) if line else "" for line in body]
    if isinstance(child, dict):
        return [prefix + " {}"]
    if isinstance(child, list):
        return [prefix + " []"]
    return [prefix + " " + yaml_scalar(child)]


def to_yaml(value: Any) -> str:
    return "\n".join(emit_yaml(value, 0)) + "\n"


class Obj(dict):
    def __init__(self, type_name: str, **fields: Any) -> None:
        super().__init__(**fields)
        self.type_name = type_name


def go_float(value: float) -> str:
    if math.isnan(value):
        return "NaN"
    if math.isinf(value):
        return "+Inf" if value > 0 else "-Inf"
    if value == 0:
        return "0"
    sign = "-" if value < 0 else ""
    digits_tuple, exponent = Decimal(repr(abs(value))).normalize().as_tuple()[1:]
    digits = "".join(str(d) for d in digits_tuple)
    point = len(digits) + int(exponent)
    power = point - 1
    if power < -4 or power >= 6:
        mantissa = digits[0] + ("." + digits[1:] if len(digits) > 1 else "")
        return f"{sign}{mantissa}e{'+' if power >= 0 else '-'}{abs(power):02d}"
    if point <= 0:
        return f"{sign}0.{'0' * -point}{digits}"
    if point >= len(digits):
        return sign + digits + "0" * (point - len(digits))
    return f"{sign}{digits[:point]}.{digits[point:]}"


def go_print(value: Any, top: bool = True) -> str:
    if value is None:
        return "" if top else "<nil>"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return go_float(value)
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        return "map[" + " ".join(f"{key}:{go_print(value[key], False)}" for key in sorted(value, key=str)) + "]"
    if isinstance(value, (list, tuple)):
        return "[" + " ".join(go_print(item, False) for item in value) + "]"
    return str(value)


def go_type(value: Any) -> str:
    if value is None:
        return "<nil>"
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, int):
        return "int"
    if isinstance(value, float):
        return "float64"
    if isinstance(value, str):
        return "string"
    if isinstance(value, Obj):
        return value.type_name
    if isinstance(value, dict):
        return "map[string]interface {}"
    if isinstance(value, list):
        return "[]interface {}"
    return type(value).__name__


def truthy(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, Obj):
        return True
    if isinstance(value, (bool, int, float)):
        return bool(value)
    if isinstance(value, (str, list, dict, tuple)):
        return len(value) > 0
    return True


def is_empty(value: Any) -> bool:
    return not truthy(value) and not isinstance(value, Obj)


def go_quote(text: str) -> str:
    named = {'"': '\\"', "\\": "\\\\", "\n": "\\n", "\t": "\\t", "\r": "\\r", "\a": "\\a", "\b": "\\b", "\f": "\\f", "\v": "\\v"}
    out = ['"']
    for char in text:
        if char in named:
            out.append(named[char])
        elif ord(char) < 32 or ord(char) == 127:
            out.append(f"\\x{ord(char):02x}")
        elif not char.isprintable():
            out.append(f"\\u{ord(char):04x}" if ord(char) < 0x10000 else f"\\U{ord(char):08x}")
        else:
            out.append(char)
    return "".join(out) + '"'


def floatify(value: Any) -> Any:
    if isinstance(value, bool) or value is None:
        return value
    if isinstance(value, int):
        return float(value)
    if isinstance(value, dict):
        return {key: floatify(item) for key, item in value.items()}
    if isinstance(value, list):
        return [floatify(item) for item in value]
    return value


def jsonable(value: Any) -> Any:
    if isinstance(value, float) and not math.isinf(value) and not math.isnan(value) and value == int(value) and abs(value) < 1e21:
        return int(value)
    if isinstance(value, dict):
        return {str(key): jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [jsonable(item) for item in value]
    return value


def merge_maps(base: Dict[str, Any], overlay: Dict[str, Any]) -> Dict[str, Any]:
    out = dict(base)
    for key, value in overlay.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = merge_maps(out[key], value)
        else:
            out[key] = value
    return out


def clean_nils(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: clean_nils(item) for key, item in value.items() if item is not None}
    return value


def coalesce(defaults: Dict[str, Any], config: Dict[str, Any]) -> Dict[str, Any]:
    result = copy.deepcopy(config)
    for key, default in defaults.items():
        if key in result:
            if result[key] is None:
                del result[key]
            elif isinstance(result[key], dict) and isinstance(default, dict):
                result[key] = coalesce(default, result[key])
        elif default is not None:
            result[key] = clean_nils(copy.deepcopy(default))
    return result


def split_unescaped(text: str, separator: str, respect_braces: bool = False) -> List[str]:
    parts: List[str] = []
    buffer: List[str] = []
    depth = 0
    index = 0
    while index < len(text):
        char = text[index]
        if char == "\\" and index + 1 < len(text):
            buffer.append(text[index : index + 2])
            index += 2
            continue
        if respect_braces and char == "{":
            depth += 1
        elif respect_braces and char == "}":
            depth = max(0, depth - 1)
        elif char == separator and depth == 0:
            parts.append("".join(buffer))
            buffer = []
            index += 1
            continue
        buffer.append(char)
        index += 1
    parts.append("".join(buffer))
    return parts


def unescape(text: str) -> str:
    return re.sub(r"\\(.)", r"\1", text)


def typed_set_value(text: str) -> Any:
    if text.lower() == "true":
        return True
    if text.lower() == "false":
        return False
    if text.lower() == "null":
        return None
    if text == "0":
        return 0
    if text and text[0] != "0" and re.fullmatch(r"[-+]?\d+", text):
        number = int(text)
        if -(2 ** 63) <= number < 2 ** 63:
            return number
    return text


def parse_set_key(key: str) -> List[Any]:
    path: List[Any] = []
    for part in split_unescaped(key, "."):
        match = re.fullmatch(r"((?:\\.|[^\[\]])*)((?:\[-?\d+\])*)", part)
        if match is None or (not match.group(1) and not match.group(2)):
            raise ValuesError(f"key {key!r} is not a valid --set path (use a.b.c or list[0].name)")
        if match.group(1):
            path.append(unescape(match.group(1)))
        for raw_index in re.findall(r"\[(-?\d+)\]", match.group(2)):
            index = int(raw_index)
            if index < 0:
                raise ValuesError(f"negative {index} index not allowed")
            if index > MAX_SET_INDEX:
                raise ValuesError(f"index of {index} is greater than maximum supported index of {MAX_SET_INDEX}")
            path.append(index)
    if not path or not isinstance(path[0], str):
        raise ValuesError(f"key {key!r} must start with a name, not a list index")
    return path


def set_path(root: Dict[str, Any], path: List[Any], value: Any) -> None:
    node: Any = root
    for position, part in enumerate(path):
        last = position == len(path) - 1
        want_list = not last and isinstance(path[position + 1], int)
        if isinstance(part, int):
            while len(node) <= part:
                node.append(None)
        if last:
            node[part] = value
            return
        child = node[part] if isinstance(part, int) else node.get(part)
        if want_list and not isinstance(child, list):
            child = []
            node[part] = child
        elif not want_list and not isinstance(child, dict):
            child = {}
            node[part] = child
        node = child


def parse_set(expression: str, into: Dict[str, Any], as_string: bool = False) -> Dict[str, Any]:
    for assignment in split_unescaped(expression, ",", respect_braces=True):
        if not assignment:
            continue
        pieces = split_unescaped(assignment, "=")
        if len(pieces) < 2:
            raise ValuesError(f"key {unescape(assignment)!r} has no value (use --set {assignment}=VALUE)")
        key, raw_value = pieces[0], "=".join(pieces[1:])
        if not as_string and raw_value.startswith("{") and raw_value.endswith("}"):
            value: Any = [typed_set_value(unescape(item)) for item in split_unescaped(raw_value[1:-1], ",") if item != ""]
        elif as_string:
            value = unescape(raw_value)
        else:
            value = typed_set_value(unescape(raw_value))
        set_path(into, parse_set_key(key), value)
    return into


def load_values_file(path: str) -> Dict[str, Any]:
    try:
        with open(path, encoding="utf-8") as handle:
            data = parse_yaml(handle.read())
    except OSError as exc:
        raise ValuesError(f"cannot read values file {path}: {exc.strerror}") from exc
    except YamlError as exc:
        raise ValuesError(f"{path}: {exc}") from exc
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ValuesError(f"{path}: a values file must be a mapping, got {go_type(data)}")
    return floatify(data)


def user_values(files: Sequence[str] = (), sets: Sequence[str] = (), set_strings: Sequence[str] = ()) -> Dict[str, Any]:
    config: Dict[str, Any] = {}
    for path in files:
        config = merge_maps(config, load_values_file(path))
    for expression in sets:
        parse_set(expression, config)
    for expression in set_strings:
        parse_set(expression, config, as_string=True)
    return config


@dataclass
class Chart:
    path: str
    metadata: Dict[str, Any]
    values: Dict[str, Any]
    templates: Dict[str, str]
    warnings: List[str] = field(default_factory=list)

    @property
    def name(self) -> str:
        return self.metadata["name"]

    @property
    def version(self) -> str:
        return self.metadata["version"]

    @property
    def app_version(self) -> str:
        return self.metadata.get("appVersion", "")


def normalise_metadata(raw: Dict[str, Any]) -> Dict[str, Any]:
    metadata: Dict[str, Any] = {}
    for key in METADATA_FIELDS:
        value = raw.get(key)
        if value in (None, "", [], {}, False):
            continue
        if key in METADATA_STRINGS:
            value = value if isinstance(value, str) else go_print(value)
        elif key in ("sources", "keywords"):
            value = [go_print(item) for item in value] if isinstance(value, list) else [go_print(value)]
        elif key == "annotations" and isinstance(value, dict):
            value = {str(k): go_print(v) for k, v in value.items()}
        elif key == "deprecated":
            value = bool(value)
        metadata[key] = value
    return metadata


def load_chart(path: str) -> Chart:
    if not os.path.isdir(path):
        raise ChartError(f"{path}: not a chart directory (expected Chart.yaml, values.yaml and templates/)")
    chart_file = os.path.join(path, "Chart.yaml")
    if not os.path.exists(chart_file):
        raise ChartError(f"{path}: Chart.yaml file is missing")
    try:
        with open(chart_file, encoding="utf-8") as handle:
            raw = parse_yaml(handle.read())
    except YamlError as exc:
        raise ChartError(f"{chart_file}: {exc}") from exc
    if not isinstance(raw, dict):
        raise ChartError(f"{chart_file}: validation: chart.metadata is required")
    metadata = normalise_metadata(raw)
    if not metadata.get("apiVersion"):
        raise ChartError(f"{chart_file}: validation: chart.metadata.apiVersion is required")
    if not metadata.get("name"):
        raise ChartError(f"{chart_file}: validation: chart.metadata.name is required")
    if not metadata.get("version"):
        raise ChartError(f"{chart_file}: validation: chart.metadata.version is required")
    if not SEMVER_RE.match(metadata["version"]):
        raise ChartError(f"{chart_file}: validation: chart.metadata.version {metadata['version']!r} is invalid (it must be SemVer 2, e.g. 0.1.0)")
    if metadata.get("type") not in (None, "application", "library"):
        raise ChartError(f"{chart_file}: validation: chart.metadata.type must be application or library")
    values_file = os.path.join(path, "values.yaml")
    values = load_values_file(values_file) if os.path.exists(values_file) else {}
    templates: Dict[str, str] = {}
    template_dir = os.path.join(path, "templates")
    for root, dirs, files in os.walk(template_dir):
        dirs.sort()
        for name in sorted(files):
            full = os.path.join(root, name)
            relative = "templates/" + os.path.relpath(full, template_dir).replace(os.sep, "/")
            with open(full, encoding="utf-8") as handle:
                templates[relative] = handle.read()
    warnings: List[str] = []
    if metadata.get("dependencies") or (os.path.isdir(os.path.join(path, "charts")) and os.listdir(os.path.join(path, "charts"))):
        warnings.append("subcharts and dependencies are ignored by minihelm")
    if os.path.isdir(os.path.join(path, "crds")):
        warnings.append("crds/ is ignored by minihelm (Helm installs it before the templates and never upgrades it)")
    if os.path.exists(os.path.join(path, "values.schema.json")):
        warnings.append("values.schema.json is not validated by minihelm")
    return Chart(path, metadata, values, dict(sorted(templates.items())), warnings)


SPACE_CHARS = set(" \t\r\n")
KEYWORD_RE = re.compile(r"^(if|else|end|range|with|define|template|block|break|continue)\b\s*(.*)$", re.S)
EXPR_TOKEN_RE = re.compile(
    r"""
    (?P<space>\s+)
  | (?P<string>"(?:\\.|[^"\\])*")
  | (?P<raw>`[^`]*`)
  | (?P<char>'(?:\\.|[^'\\])')
  | (?P<number>[-+]?(?:\d+\.\d*|\.\d+|\d+)(?:[eE][-+]?\d+)?)
  | (?P<declare>:=)
  | (?P<assign>=)
  | (?P<pipe>\|)
  | (?P<lparen>\()
  | (?P<rparen>\))
  | (?P<comma>,)
  | (?P<variable>\$[A-Za-z0-9_]*)
  | (?P<field>(?:\.[A-Za-z_][A-Za-z0-9_]*)+|\.)
  | (?P<ident>[A-Za-z_][A-Za-z0-9_]*)
    """,
    re.X,
)


@dataclass
class Token:
    kind: str
    value: str
    line: int
    trim_left: bool = False
    trim_right: bool = False


def action_end(source: str, index: int, name: str, line: int) -> int:
    start = index
    while start < len(source) and source[start] in SPACE_CHARS:
        start += 1
    if source.startswith("/*", start):
        close = source.find("*/", start + 2)
        end = source.find("}}", close + 2) if close >= 0 else -1
        if end < 0:
            raise TemplateError(f"template: {name}:{line}: unclosed comment")
        return end
    quote = None
    position = index
    while position < len(source):
        char = source[position]
        if quote:
            if char == "\\" and quote != "`":
                position += 2
                continue
            if char == quote:
                quote = None
        elif char in "\"`'":
            quote = char
        elif source.startswith("}}", position):
            return position
        position += 1
    raise TemplateError(f"template: {name}:{line}: unclosed action")


def lex_template(source: str, name: str) -> List[Token]:
    tokens: List[Token] = []
    position = 0
    line = 1
    while position < len(source):
        start = source.find("{{", position)
        if start < 0:
            tokens.append(Token("text", source[position:], line))
            break
        if start > position:
            tokens.append(Token("text", source[position:start], line))
        line += source.count("\n", position, start)
        inner = start + 2
        trim_left = source[inner : inner + 1] == "-" and source[inner + 1 : inner + 2] in SPACE_CHARS and inner + 1 < len(source)
        if trim_left:
            inner += 1
        end = action_end(source, inner, name, line)
        body = source[inner:end]
        trim_right = len(body) >= 2 and body[-1] == "-" and body[-2] in SPACE_CHARS
        if trim_right:
            body = body[:-1]
        stripped = body.strip()
        kind = "comment" if stripped.startswith("/*") else "action"
        tokens.append(Token(kind, stripped, line, trim_left, trim_right))
        line += source.count("\n", start, end + 2)
        position = end + 2
    for index, token in enumerate(tokens):
        if token.kind == "text":
            continue
        if token.trim_left and index > 0 and tokens[index - 1].kind == "text":
            tokens[index - 1].value = tokens[index - 1].value.rstrip(" \t\r\n")
        if token.trim_right and index + 1 < len(tokens) and tokens[index + 1].kind == "text":
            tokens[index + 1].value = tokens[index + 1].value.lstrip(" \t\r\n")
    return tokens


@dataclass
class Operand:
    kind: str
    value: Any = None
    names: List[str] = field(default_factory=list)
    text: str = ""


@dataclass
class Pipeline:
    text: str
    decl: List[str]
    assign: bool
    commands: List[List[Operand]]


@dataclass
class Text:
    value: str


@dataclass
class Action:
    pipe: Pipeline
    where: str


@dataclass
class IfNode:
    branches: List[Tuple[Pipeline, List[Any]]]
    otherwise: List[Any]
    where: str


@dataclass
class RangeNode:
    pipe: Pipeline
    body: List[Any]
    otherwise: List[Any]
    where: str


@dataclass
class WithNode:
    pipe: Pipeline
    body: List[Any]
    otherwise: List[Any]
    where: str


@dataclass
class CallNode:
    name: str
    pipe: Optional[Pipeline]
    where: str


@dataclass
class ExprToken:
    kind: str
    text: str
    glued: bool


def go_unquote(literal: str) -> str:
    value, _ = parse_double_quoted(literal, 0)
    return value


class ExpressionParser:
    def __init__(self, text: str, name: str, line: int, functions: Iterable[str]) -> None:
        self.text = text
        self.name = name
        self.line = line
        self.functions = set(functions)
        self.tokens = self.tokenize(text)
        self.position = 0

    def error(self, message: str) -> TemplateError:
        return TemplateError(f"template: {self.name}:{self.line}: {message}")

    def tokenize(self, text: str) -> List[ExprToken]:
        tokens: List[ExprToken] = []
        position = 0
        glued = True
        while position < len(text):
            match = EXPR_TOKEN_RE.match(text, position)
            if match is None:
                raise self.error(f"unexpected {text[position]!r} in {{{{ {text} }}}}")
            if match.lastgroup == "space":
                glued = False
            else:
                tokens.append(ExprToken(match.lastgroup or "", match.group(), glued))
                glued = True
            position = match.end()
        return tokens

    def peek(self, offset: int = 0) -> Optional[ExprToken]:
        index = self.position + offset
        return self.tokens[index] if index < len(self.tokens) else None

    def parse(self, allow_two_vars: bool = False) -> Pipeline:
        if not self.tokens:
            raise self.error("missing value for command")
        decl: List[str] = []
        assign = False
        first, second = self.peek(), self.peek(1)
        if first and first.kind == "variable" and second and second.kind in ("declare", "assign"):
            decl, assign = [first.text], second.kind == "assign"
            self.position = 2
        elif allow_two_vars and first and first.kind == "variable" and second and second.kind == "comma":
            third, fourth = self.peek(2), self.peek(3)
            if not (third and third.kind == "variable" and fourth and fourth.kind == "declare"):
                raise self.error("range can only declare two variables: range $index, $value := ...")
            decl = [first.text, third.text]
            self.position = 4
        commands = self.commands()
        if self.position != len(self.tokens):
            raise self.error(f"unexpected {self.tokens[self.position].text!r} in {{{{ {self.text} }}}}")
        return Pipeline(self.text, decl, assign, commands)

    def commands(self) -> List[List[Operand]]:
        commands = [self.command()]
        while self.peek() is not None and self.peek().kind == "pipe":
            self.position += 1
            commands.append(self.command())
        return commands

    def command(self) -> List[Operand]:
        operands: List[Operand] = []
        while self.peek() is not None and self.peek().kind not in ("pipe", "rparen"):
            operands.append(self.operand())
        if not operands:
            raise self.error(f"missing value for command in {{{{ {self.text} }}}}")
        return operands

    def operand(self) -> Operand:
        token = self.tokens[self.position]
        self.position += 1
        if token.kind == "string":
            operand = Operand("const", go_unquote(token.text), text=token.text)
        elif token.kind == "raw":
            operand = Operand("const", token.text[1:-1], text=token.text)
        elif token.kind == "char":
            operand = Operand("const", ord(go_unquote('"' + token.text[1:-1] + '"')), text=token.text)
        elif token.kind == "number":
            number: Any = int(token.text) if re.fullmatch(r"[-+]?\d+", token.text) else float(token.text)
            operand = Operand("const", number, text=token.text)
        elif token.kind == "field":
            operand = Operand("dot", names=[] if token.text == "." else token.text.split(".")[1:], text=token.text)
        elif token.kind == "variable":
            operand = Operand("var", token.text, text=token.text)
        elif token.kind == "ident":
            if token.text in ("true", "false"):
                operand = Operand("const", token.text == "true", text=token.text)
            elif token.text == "nil":
                operand = Operand("const", None, text=token.text)
            elif token.text in self.functions:
                operand = Operand("func", token.text, text=token.text)
            else:
                raise self.error(f'function "{token.text}" not defined (it is not in minihelm\'s subset)')
        elif token.kind == "lparen":
            inner = self.commands()
            closing = self.peek()
            if closing is None or closing.kind != "rparen":
                raise self.error(f"unclosed left paren in {{{{ {self.text} }}}}")
            self.position += 1
            operand = Operand("sub", Pipeline(self.text, [], False, inner), text="(...)")
        else:
            raise self.error(f"unexpected {token.text!r} in {{{{ {self.text} }}}}")
        while operand.kind in ("var", "sub", "dot") and self.peek() is not None and self.peek().kind == "field" and self.peek().glued and self.peek().text != ".":
            operand.names.extend(self.peek().text.split(".")[1:])
            self.position += 1
        return operand


class TemplateParser:
    def __init__(self, tokens: List[Token], name: str, functions: Iterable[str]) -> None:
        self.tokens = tokens
        self.name = name
        self.functions = list(functions)
        self.index = 0
        self.defines: Dict[str, List[Any]] = {}

    def error(self, token: Optional[Token], message: str) -> TemplateError:
        line = token.line if token else "EOF"
        return TemplateError(f"template: {self.name}:{line}: {message}")

    def pipeline(self, text: str, token: Token, allow_two_vars: bool = False) -> Pipeline:
        return ExpressionParser(text, self.name, token.line, self.functions).parse(allow_two_vars)

    def parse(self) -> Tuple[List[Any], Dict[str, List[Any]]]:
        nodes, keyword, token = self.parse_list(top=True)
        if keyword is not None:
            raise self.error(token, f"unexpected {{{{{keyword}}}}}")
        return nodes, self.defines

    def parse_list(self, top: bool = False) -> Tuple[List[Any], Optional[str], Optional[Token]]:
        nodes: List[Any] = []
        while self.index < len(self.tokens):
            token = self.tokens[self.index]
            self.index += 1
            if token.kind == "text":
                if token.value:
                    nodes.append(Text(token.value))
                continue
            if token.kind == "comment":
                continue
            match = KEYWORD_RE.match(token.value)
            keyword, rest = (match.group(1), match.group(2).strip()) if match else (None, token.value)
            where = f"{self.name}:{token.line}"
            if keyword in ("end", "else"):
                return nodes, keyword, token
            if keyword == "if":
                nodes.append(self.parse_if(rest, token))
            elif keyword == "range":
                pipe = self.pipeline(rest, token, allow_two_vars=True)
                body, otherwise = self.parse_body_with_else(token, "range")
                nodes.append(RangeNode(pipe, body, otherwise, where))
            elif keyword == "with":
                pipe = self.pipeline(rest, token)
                body, otherwise = self.parse_body_with_else(token, "with")
                nodes.append(WithNode(pipe, body, otherwise, where))
            elif keyword == "define":
                if not top:
                    raise self.error(token, "define is only allowed at the top level of a file")
                name = self.template_name(rest, token)
                body, end_keyword, end_token = self.parse_list()
                if end_keyword != "end":
                    raise self.error(end_token, f"expected {{{{end}}}} for define {name!r}")
                self.defines[name] = body
            elif keyword == "template":
                literal = re.match(r'^("(?:\\.|[^"\\])*"|`[^`]*`)\s*(.*)$', rest, re.S)
                if literal is None:
                    raise self.error(token, "template needs a quoted name: {{ template \"name\" . }}")
                pipe = self.pipeline(literal.group(2), token) if literal.group(2).strip() else None
                nodes.append(CallNode(self.template_name(literal.group(1), token), pipe, where))
            elif keyword in ("block", "break", "continue"):
                raise self.error(token, f"{{{{{keyword}}}}} is not supported by minihelm")
            else:
                nodes.append(Action(self.pipeline(token.value, token), where))
        if not top:
            raise self.error(None, "unexpected EOF (a {{if}}, {{range}}, {{with}} or {{define}} is missing its {{end}})")
        return nodes, None, None

    def template_name(self, text: str, token: Token) -> str:
        text = text.strip()
        if text.startswith('"') and text.endswith('"') and len(text) >= 2:
            return go_unquote(text)
        if text.startswith("`") and text.endswith("`") and len(text) >= 2:
            return text[1:-1]
        raise self.error(token, f"template names must be quoted strings, got {text!r}")

    def parse_if(self, rest: str, token: Token) -> IfNode:
        branches: List[Tuple[Pipeline, List[Any]]] = []
        pipe = self.pipeline(rest, token)
        while True:
            body, keyword, end_token = self.parse_list()
            branches.append((pipe, body))
            if keyword == "end":
                return IfNode(branches, [], f"{self.name}:{token.line}")
            else_rest = KEYWORD_RE.match(end_token.value).group(2).strip()
            if else_rest.startswith("if ") or else_rest == "if":
                pipe = self.pipeline(else_rest[2:].strip(), end_token)
                continue
            if else_rest:
                raise self.error(end_token, f"unexpected {else_rest!r} after else")
            otherwise, keyword, closing = self.parse_list()
            if keyword != "end":
                raise self.error(closing, "expected {{end}} after {{else}}")
            return IfNode(branches, otherwise, f"{self.name}:{token.line}")

    def parse_body_with_else(self, token: Token, kind: str) -> Tuple[List[Any], List[Any]]:
        body, keyword, end_token = self.parse_list()
        if keyword == "end":
            return body, []
        if KEYWORD_RE.match(end_token.value).group(2).strip():
            raise self.error(end_token, f"{{{{else ...}}}} chains are not supported inside {kind} by minihelm")
        otherwise, keyword, closing = self.parse_list()
        if keyword != "end":
            raise self.error(closing, f"expected {{{{end}}}} to close {kind}")
        return body, otherwise


MISSING = object()


class Scope:
    def __init__(self, dot: Any, root: Any) -> None:
        self.dot = dot
        self.frames: List[Dict[str, Any]] = [{"$": root}]

    def push(self) -> None:
        self.frames.append({})

    def pop(self) -> None:
        self.frames.pop()

    def declare(self, name: str, value: Any) -> None:
        self.frames[-1][name] = value

    def assign(self, name: str, value: Any) -> None:
        for frame in reversed(self.frames):
            if name in frame:
                frame[name] = value
                return
        raise TemplateError(f"undefined variable: {name}")

    def lookup(self, name: str) -> Any:
        for frame in reversed(self.frames):
            if name in frame:
                return frame[name]
        raise TemplateError(f"undefined variable: {name}")


def want_str(value: Any) -> str:
    if isinstance(value, str):
        return value
    if value is None:
        raise TemplateError("invalid value; expected string")
    raise TemplateError(f"wrong type for value; expected string; got {go_type(value)}")


def want_int(value: Any) -> int:
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    if value is None:
        raise TemplateError("invalid value; expected int")
    raise TemplateError(f"wrong type for value; expected int; got {go_type(value)}")


def comparable_kind(value: Any) -> Optional[str]:
    if value is None:
        return "nil"
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, int):
        return "int"
    if isinstance(value, float):
        return "float"
    if isinstance(value, str):
        return "string"
    return None


def f_eq(first: Any, *others: Any) -> bool:
    if not others:
        raise TemplateError("missing argument for comparison")
    for other in others:
        left, right = comparable_kind(first), comparable_kind(other)
        if left is None or right is None:
            raise TemplateError(f"non-comparable type {go_type(first if left is None else other)}")
        if "nil" in (left, right):
            if left == right:
                return True
            continue
        if left != right:
            raise TemplateError(f"incompatible types for comparison: {go_type(first)} and {go_type(other)}")
        if first == other:
            return True
    return False


def bad_verb(verb: str, value: Any) -> str:
    if value is None:
        return f"%!{verb}(<nil>)"
    return f"%!{verb}({go_type(value)}={go_print(value, False)})"


def format_verb(verb: str, value: Any, precision: Optional[str]) -> str:
    if verb == "v":
        return go_print(value, False)
    if verb == "s":
        return value if isinstance(value, str) else (go_print(value, False) if isinstance(value, (list, dict)) else bad_verb(verb, value))
    if verb == "q":
        return go_quote(value) if isinstance(value, str) else bad_verb(verb, value)
    if verb == "d":
        return str(value) if isinstance(value, int) and not isinstance(value, bool) else bad_verb(verb, value)
    if verb == "t":
        return ("true" if value else "false") if isinstance(value, bool) else bad_verb(verb, value)
    if verb in "fF":
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return f"{float(value):.{int(precision) if precision else 6}f}"
        return bad_verb(verb, value)
    return bad_verb(verb, value)


def go_sprintf(template: Any, *args: Any) -> str:
    template = want_str(template)
    out: List[str] = []
    position = 0
    used = 0
    for match in re.finditer(r"%([-+# 0]*)(\d*)(?:\.(\d+))?([a-zA-Z%])", template):
        out.append(template[position : match.start()])
        position = match.end()
        flags, width, precision, verb = match.groups()
        if verb == "%":
            out.append("%")
            continue
        if used >= len(args):
            out.append(f"%!{verb}(MISSING)")
            continue
        text = format_verb(verb, args[used], precision)
        used += 1
        if width:
            text = text.ljust(int(width)) if "-" in flags else text.rjust(int(width), "0" if "0" in flags else " ")
        out.append(text)
    out.append(template[position:])
    if used < len(args):
        out.append("%!(EXTRA " + ", ".join(f"{go_type(a)}={go_print(a, False)}" for a in args[used:]) + ")")
    return "".join(out)


def go_sprint(*args: Any) -> str:
    out: List[str] = []
    for index, value in enumerate(args):
        if index > 0 and not isinstance(value, str) and not isinstance(args[index - 1], str):
            out.append(" ")
        out.append(go_print(value, False))
    return "".join(out)


def f_trunc(count: Any, text: Any) -> str:
    count, text = want_int(count), want_str(text)
    if count < 0 and len(text) + count > 0:
        return text[len(text) + count :]
    if count >= 0 and len(text) > count:
        return text[:count]
    return text


def f_indent(spaces: Any, text: Any) -> str:
    pad = " " * want_int(spaces)
    return pad + want_str(text).replace("\n", "\n" + pad)


def f_to_yaml(value: Any) -> str:
    text = to_yaml(jsonable(value))
    return text[:-1] if text.endswith("\n") else text


def f_len(value: Any) -> int:
    if isinstance(value, (str, list, dict)):
        return len(value)
    raise TemplateError(f"len of {'nil pointer' if value is None else 'type ' + go_type(value)}")


def f_index(collection: Any, *keys: Any) -> Any:
    for key in keys:
        if isinstance(collection, dict):
            collection = collection.get(key)
        elif isinstance(collection, list):
            index = want_int(key)
            if not 0 <= index < len(collection):
                raise TemplateError(f"error calling index: index out of range: {index}")
            collection = collection[index]
        elif collection is None:
            raise TemplateError("error calling index: index of untyped nil")
        else:
            raise TemplateError(f"error calling index: can't index item of type {go_type(collection)}")
    return collection


def f_first(items: Any) -> Any:
    if items is None:
        return None
    if not isinstance(items, list):
        raise TemplateError(f"error calling first: cannot find first on type {go_type(items)}")
    return items[0] if items else None


def f_dict(*pairs: Any) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    for index in range(0, len(pairs), 2):
        out[go_print(pairs[index])] = pairs[index + 1] if index + 1 < len(pairs) else ""
    return out


BASE_FUNCTIONS: Dict[str, Callable[..., Any]] = {
    "default": lambda default, *given: default if not given or is_empty(given[0]) else given[0],
    "empty": is_empty,
    "quote": lambda *values: " ".join(go_quote(go_print(value)) for value in values if value is not None),
    "squote": lambda *values: " ".join("'" + go_print(value) + "'" for value in values if value is not None),
    "upper": lambda text: want_str(text).upper(),
    "lower": lambda text: want_str(text).lower(),
    "trunc": f_trunc,
    "trimSuffix": lambda suffix, text: want_str(text)[: -len(suffix)] if suffix and want_str(text).endswith(want_str(suffix)) else want_str(text),
    "trimPrefix": lambda prefix, text: want_str(text)[len(prefix) :] if prefix and want_str(text).startswith(want_str(prefix)) else want_str(text),
    "replace": lambda old, new, text: want_str(text).replace(want_str(old), want_str(new)),
    "contains": lambda substring, text: want_str(substring) in want_str(text),
    "indent": f_indent,
    "nindent": lambda spaces, text: "\n" + f_indent(spaces, text),
    "toYaml": f_to_yaml,
    "toJson": lambda value: json.dumps(jsonable(value), separators=(",", ":"), sort_keys=True, ensure_ascii=False),
    "printf": go_sprintf,
    "print": go_sprint,
    "eq": f_eq,
    "ne": lambda left, right: not f_eq(left, right),
    "not": lambda value: not truthy(value),
    "and": lambda *values: values[-1],
    "or": lambda *values: values[-1],
    "len": f_len,
    "index": f_index,
    "sha256sum": lambda text: hashlib.sha256(want_str(text).encode("utf-8")).hexdigest(),
    "b64enc": lambda text: base64.b64encode(want_str(text).encode("utf-8")).decode("ascii"),
    "list": lambda *items: list(items),
    "dict": f_dict,
    "first": f_first,
}


class Engine:
    def __init__(self) -> None:
        self.templates: Dict[str, List[Any]] = {}
        self.depth = 0
        self.functions: Dict[str, Callable[..., Any]] = dict(BASE_FUNCTIONS)
        self.functions["include"] = self.include
        self.functions["required"] = self.required
        self.signatures = {name: inspect.signature(function) for name, function in self.functions.items()}

    def add(self, name: str, source: str) -> None:
        nodes, defines = TemplateParser(lex_template(source, name), name, self.functions).parse()
        self.templates[name] = nodes
        self.templates.update(defines)

    def execute(self, name: str, dot: Any) -> str:
        if name not in self.templates:
            raise TemplateError(f'template: no template "{name}" associated with template "gotpl"')
        if self.depth >= MAX_INCLUDE_DEPTH:
            raise TemplateError(f"rendering template has a nested reference name: {name}: unable to execute template")
        self.depth += 1
        try:
            out: List[str] = []
            self.walk(self.templates[name], Scope(dot, dot), out)
            return "".join(out)
        finally:
            self.depth -= 1

    def include(self, name: Any, data: Any) -> str:
        try:
            return self.execute(want_str(name), data)
        except TemplateError as exc:
            if exc.located:
                raise
            raise TemplateError(f"error calling include: {exc}") from exc

    def required(self, message: Any, value: Any) -> Any:
        if value is None or value == "":
            raise RequiredValue(go_print(message))
        return value

    def walk(self, nodes: List[Any], scope: Scope, out: List[str]) -> None:
        for node in nodes:
            if isinstance(node, Text):
                out.append(node.value)
            elif isinstance(node, Action):
                value = self.run(node.pipe, scope, node.where)
                if not node.pipe.decl:
                    out.append(go_print(value))
            elif isinstance(node, IfNode):
                for pipe, body in node.branches:
                    if truthy(self.run(pipe, scope, node.where)):
                        self.block(body, scope, out)
                        break
                else:
                    self.block(node.otherwise, scope, out)
            elif isinstance(node, WithNode):
                value = self.run(node.pipe, scope, node.where)
                if truthy(value):
                    self.block(node.body, scope, out, value)
                else:
                    self.block(node.otherwise, scope, out)
            elif isinstance(node, RangeNode):
                self.range(node, scope, out)
            elif isinstance(node, CallNode):
                dot = self.run(node.pipe, scope, node.where) if node.pipe else None
                out.append(self.execute(node.name, dot))

    def block(self, body: List[Any], scope: Scope, out: List[str], dot: Any = MISSING) -> None:
        saved = scope.dot
        if dot is not MISSING:
            scope.dot = dot
        scope.push()
        try:
            self.walk(body, scope, out)
        finally:
            scope.pop()
            scope.dot = saved

    def range(self, node: RangeNode, scope: Scope, out: List[str]) -> None:
        value = self.run(node.pipe, scope, node.where, declare=False)
        if value is None:
            items: List[Tuple[Any, Any]] = []
        elif isinstance(value, Obj):
            raise self.located(node.where, node.pipe, f"range can't iterate over {go_type(value)}")
        elif isinstance(value, dict):
            items = [(key, value[key]) for key in sorted(value, key=str)]
        elif isinstance(value, list):
            items = list(enumerate(value))
        elif isinstance(value, int) and not isinstance(value, bool):
            items = [(index, index) for index in range(value)]
        else:
            raise self.located(node.where, node.pipe, f"range can't iterate over {go_print(value, False)}")
        if not items:
            self.block(node.otherwise, scope, out)
            return
        for key, item in items:
            saved = scope.dot
            scope.dot = item
            scope.push()
            if len(node.pipe.decl) == 1:
                scope.declare(node.pipe.decl[0], item)
            elif len(node.pipe.decl) == 2:
                scope.declare(node.pipe.decl[0], key)
                scope.declare(node.pipe.decl[1], item)
            try:
                self.walk(node.body, scope, out)
            finally:
                scope.pop()
                scope.dot = saved

    def located(self, where: str, pipe: Pipeline, message: str) -> TemplateError:
        name = where.rsplit(":", 1)[0]
        return TemplateError(f'template: {where}: executing "{name}" at <{pipe.text}>: {message}', located=True)

    def run(self, pipe: Pipeline, scope: Scope, where: str, declare: bool = True) -> Any:
        try:
            value = self.evaluate(pipe, scope)
        except RequiredValue as exc:
            if self.depth > 1:
                raise
            raise TemplateError(f"execution error at ({where}): {exc}", located=True) from exc
        except TemplateError as exc:
            if exc.located:
                raise
            raise self.located(where, pipe, str(exc)) from exc
        if declare and pipe.decl:
            if pipe.assign:
                scope.assign(pipe.decl[0], value)
            else:
                scope.declare(pipe.decl[0], value)
        return value

    def evaluate(self, pipe: Pipeline, scope: Scope) -> Any:
        value: Any = MISSING
        for command in pipe.commands:
            value = self.command(command, scope, value)
        return value

    def command(self, command: List[Operand], scope: Scope, piped: Any) -> Any:
        head = command[0]
        if head.kind == "func":
            if head.value in ("and", "or"):
                return self.logical(head.value, command[1:], scope, piped)
            args = [self.operand(operand, scope) for operand in command[1:]]
            if piped is not MISSING:
                args.append(piped)
            return self.call(head.value, args)
        if len(command) > 1 or piped is not MISSING:
            raise TemplateError(f"can't give argument to non-function {head.text}")
        return self.operand(head, scope)

    def logical(self, name: str, operands: List[Operand], scope: Scope, piped: Any) -> Any:
        getters: List[Callable[[], Any]] = [lambda operand=operand: self.operand(operand, scope) for operand in operands]
        if piped is not MISSING:
            getters.append(lambda: piped)
        if not getters:
            raise TemplateError(f"wrong number of args for {name}: want at least 1 got 0")
        result: Any = None
        for get in getters:
            result = get()
            if (name == "and") != truthy(result):
                return result
        return result

    def call(self, name: str, args: List[Any]) -> Any:
        try:
            self.signatures[name].bind(*args)
        except TypeError as exc:
            raise TemplateError(f"wrong number of args for {name}: got {len(args)}") from exc
        return self.functions[name](*args)

    def operand(self, operand: Operand, scope: Scope) -> Any:
        if operand.kind == "const":
            value = operand.value
        elif operand.kind == "dot":
            value = scope.dot
        elif operand.kind == "var":
            value = scope.lookup(operand.value)
        elif operand.kind == "sub":
            value = self.evaluate(operand.value, scope)
        else:
            value = self.call(operand.value, [])
        for name in operand.names:
            value = field_of(value, name)
        return value


def field_of(value: Any, name: str) -> Any:
    if isinstance(value, Obj):
        if name in value:
            return value[name]
        raise TemplateError(f"can't evaluate field {name} in type {value.type_name}")
    if isinstance(value, dict):
        return value.get(name)
    if value is None:
        raise TemplateError(f"nil pointer evaluating interface {{}}.{name}")
    raise TemplateError(f"can't evaluate field {name} in type {go_type(value)}")


def validate_release_name(name: str) -> None:
    if len(name) > MAX_RELEASE_NAME or not RELEASE_NAME_RE.match(name):
        raise InputError(f"release name {name!r}: invalid release name, must match regex {RELEASE_NAME_RE.pattern} and the length must not be longer than {MAX_RELEASE_NAME}")


def chart_object(chart: Chart) -> Obj:
    meta = chart.metadata
    return Obj(
        "*chart.Metadata",
        Name=meta.get("name", ""), Home=meta.get("home", ""), Sources=meta.get("sources", []), Version=meta.get("version", ""),
        Description=meta.get("description", ""), Keywords=meta.get("keywords", []), Maintainers=meta.get("maintainers", []),
        Icon=meta.get("icon", ""), APIVersion=meta.get("apiVersion", ""), Condition=meta.get("condition", ""), Tags=meta.get("tags", ""),
        AppVersion=meta.get("appVersion", ""), Deprecated=meta.get("deprecated", False), Annotations=meta.get("annotations", {}),
        KubeVersion=meta.get("kubeVersion", ""), Dependencies=meta.get("dependencies", []), Type=meta.get("type", ""),
    )


@dataclass
class Rendered:
    manifest: str
    entries: List[Dict[str, Any]]
    hooks: List[Dict[str, Any]]
    values: Dict[str, Any]


def kind_rank(kind: str) -> Tuple[int, str]:
    return (INSTALL_ORDER.index(kind), "") if kind in INSTALL_ORDER else (len(INSTALL_ORDER), kind)


def render_chart(
    chart: Chart,
    config: Dict[str, Any],
    release_name: str,
    namespace: str = "default",
    revision: int = 1,
    is_upgrade: bool = False,
    defaults: Optional[Dict[str, Any]] = None,
) -> Rendered:
    validate_release_name(release_name)
    if chart.metadata.get("type") == "library":
        raise ChartError(f"{chart.name}: library charts are not installable")
    values = coalesce(chart.values if defaults is None else defaults, config)
    engine = Engine()
    for path, source in chart.templates.items():
        engine.add(f"{chart.name}/{path}", source)
    release = Obj("common.ReleaseOptions", Name=release_name, Namespace=namespace, Revision=revision, IsInstall=not is_upgrade, IsUpgrade=is_upgrade, Service="Helm")
    outputs: Dict[str, str] = {}
    for path in chart.templates:
        base = path.rsplit("/", 1)[-1]
        if base.startswith("_") or base == "NOTES.txt":
            continue
        full = f"{chart.name}/{path}"
        top = Obj("chartutil.Values", Values=values, Release=release, Chart=chart_object(chart), Template=Obj("chartutil.Template", Name=full, BasePath=f"{chart.name}/templates"))
        outputs[full] = engine.execute(full, top)
    entries: List[Dict[str, Any]] = []
    hooks: List[Dict[str, Any]] = []
    for full in sorted(outputs):
        for content in split_manifests(outputs[full]):
            try:
                doc = parse_yaml(content)
            except YamlError as exc:
                raise ChartError(f"YAML parse error on {full}: {exc}") from exc
            if doc is None:
                continue
            if not isinstance(doc, dict):
                raise ChartError(f"YAML parse error on {full}: a manifest must be a mapping, got {go_type(doc)}")
            metadata = doc.get("metadata") if isinstance(doc.get("metadata"), dict) else {}
            annotations = metadata.get("annotations") if isinstance(metadata.get("annotations"), dict) else {}
            entry = {"path": full, "content": content, "kind": str(doc.get("kind", "")), "name": str(metadata.get("name", "")), "doc": doc}
            if HOOK_ANNOTATION in annotations:
                entry["events"] = [event.strip() for event in str(annotations[HOOK_ANNOTATION]).split(",")]
                hooks.append(entry)
            else:
                entries.append(entry)
    entries.sort(key=lambda entry: kind_rank(entry["kind"]))
    hooks.sort(key=lambda entry: kind_rank(entry["kind"]))
    manifest = "".join(f"---\n# Source: {entry['path']}\n{entry['content']}\n" for entry in entries)
    return Rendered(manifest, entries, hooks, values)


def template_output(rendered: Rendered) -> str:
    text = rendered.manifest.strip() + "\n"
    for hook in rendered.hooks:
        text += f"---\n# Source: {hook['path']}\n{hook['content']}\n"
    return text


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def rfc3339(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def release_key(name: str, version: int) -> str:
    return f"{STORAGE_PREFIX}.{name}.v{version}"


def encode_release(record: Dict[str, Any]) -> str:
    payload = json.dumps(record, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return base64.b64encode(gzip.compress(payload, compresslevel=9, mtime=0)).decode("ascii")


def decode_release(text: str) -> Dict[str, Any]:
    try:
        raw = base64.b64decode(text.strip())
        if raw[:3] == GZIP_MAGIC:
            raw = gzip.decompress(raw)
        record = json.loads(raw.decode("utf-8"))
    except (ValueError, OSError, UnicodeDecodeError) as exc:
        raise ReleaseError(f"cannot decode release: {exc}") from exc
    if not isinstance(record, dict):
        raise ReleaseError("cannot decode release: the payload is not a JSON object")
    return record


def secret_object(record: Dict[str, Any], moment: datetime, created: bool) -> Dict[str, Any]:
    labels = {"name": record["name"], "owner": "helm", "status": record["info"]["status"], "version": str(record["version"])}
    labels["createdAt" if created else "modifiedAt"] = str(int(moment.timestamp()))
    return {
        "apiVersion": "v1",
        "kind": "Secret",
        "metadata": {"name": release_key(record["name"], record["version"]), "namespace": record["namespace"], "labels": labels},
        "type": SECRET_TYPE,
        "data": {"release": base64.b64encode(encode_release(record).encode("ascii")).decode("ascii")},
    }


def release_from_secret(secret: Dict[str, Any]) -> Dict[str, Any]:
    data = (secret.get("data") or {}).get("release")
    if not data:
        raise ReleaseError(f"secret {secret.get('metadata', {}).get('name', '?')} has no data.release")
    try:
        inner = base64.b64decode(data).decode("ascii")
    except (ValueError, UnicodeDecodeError) as exc:
        raise ReleaseError(f"cannot decode data.release: {exc}") from exc
    return decode_release(inner)


def decode_any(text: str) -> Any:
    text = text.strip()
    if text.startswith("{"):
        obj = json.loads(text)
        if obj.get("kind") == "List" or "items" in obj:
            return [release_from_secret(item) for item in obj.get("items", [])]
        if obj.get("kind") == "Secret" or "data" in obj:
            return release_from_secret(obj)
        return obj
    try:
        return decode_release(text)
    except ReleaseError:
        return decode_release(base64.b64decode(text).decode("ascii"))


def chart_record(chart: Chart) -> Dict[str, Any]:
    return {
        "metadata": chart.metadata,
        "lock": None,
        "templates": [{"name": path, "data": base64.b64encode(source.encode("utf-8")).decode("ascii")} for path, source in chart.templates.items()],
        "values": jsonable(chart.values),
        "schema": None,
        "files": [],
    }


def new_record(name: str, namespace: str, version: int, chart_data: Dict[str, Any], config: Dict[str, Any], manifest: str, moment: datetime, status: str, description: str, first_deployed: Optional[str] = None) -> Dict[str, Any]:
    record: Dict[str, Any] = {
        "name": name,
        "info": {"first_deployed": first_deployed or rfc3339(moment), "last_deployed": rfc3339(moment), "description": description, "status": status},
        "chart": chart_data,
    }
    if config:
        record["config"] = jsonable(config)
    record.update({"manifest": manifest, "version": version, "namespace": namespace, "apply_method": "csa"})
    return record


def set_status(record: Dict[str, Any], status: str, description: Optional[str] = None) -> None:
    record["info"]["status"] = status
    if description is not None:
        record["info"]["description"] = description


def chart_label(record: Dict[str, Any]) -> str:
    metadata = (record.get("chart") or {}).get("metadata") or {}
    return f"{metadata.get('name', 'MISSING')}-{metadata.get('version', '')}"


def history_row(record: Dict[str, Any]) -> Dict[str, Any]:
    info = record.get("info") or {}
    row = {
        "revision": record.get("version"),
        "updated": info.get("last_deployed", ""),
        "status": info.get("status", ""),
        "chart": chart_label(record),
        "app_version": ((record.get("chart") or {}).get("metadata") or {}).get("appVersion", ""),
        "description": info.get("description", ""),
    }
    if info.get("rollback_revision"):
        row["rollback_revision"] = info["rollback_revision"]
    return row


class MemorySecrets:
    def __init__(self) -> None:
        self.secrets: Dict[str, Dict[str, Any]] = {}

    def get(self, key: str) -> Optional[Dict[str, Any]]:
        return copy.deepcopy(self.secrets.get(key))

    def create(self, secret: Dict[str, Any]) -> None:
        key = secret["metadata"]["name"]
        if key in self.secrets:
            raise ReleaseError("release: already exists")
        self.secrets[key] = copy.deepcopy(secret)

    def update(self, secret: Dict[str, Any]) -> None:
        key = secret["metadata"]["name"]
        if key not in self.secrets:
            raise ReleaseError("release: not found")
        self.secrets[key] = copy.deepcopy(secret)

    def list(self, labels: Dict[str, str]) -> List[Dict[str, Any]]:
        return [copy.deepcopy(s) for s in self.secrets.values() if all(s["metadata"].get("labels", {}).get(k) == v for k, v in labels.items())]

    def delete(self, key: str) -> None:
        self.secrets.pop(key, None)


class DirSecrets(MemorySecrets):
    def __init__(self, root: str, namespace: str) -> None:
        super().__init__()
        self.directory = os.path.join(root, namespace)
        if os.path.isdir(self.directory):
            for name in sorted(os.listdir(self.directory)):
                if name.endswith(".json"):
                    with open(os.path.join(self.directory, name), encoding="utf-8") as handle:
                        secret = json.load(handle)
                    self.secrets[secret["metadata"]["name"]] = secret

    def write(self, secret: Dict[str, Any]) -> None:
        os.makedirs(self.directory, exist_ok=True)
        with open(os.path.join(self.directory, secret["metadata"]["name"] + ".json"), "w", encoding="utf-8") as handle:
            json.dump(secret, handle, indent=2)
            handle.write("\n")

    def create(self, secret: Dict[str, Any]) -> None:
        super().create(secret)
        self.write(secret)

    def update(self, secret: Dict[str, Any]) -> None:
        super().update(secret)
        self.write(secret)

    def delete(self, key: str) -> None:
        super().delete(key)
        path = os.path.join(self.directory, key + ".json")
        if os.path.exists(path):
            os.remove(path)


Runner = Callable[[Sequence[str], Optional[str]], Tuple[int, str, str]]


def subprocess_runner(args: Sequence[str], stdin: Optional[str] = None) -> Tuple[int, str, str]:
    try:
        proc = subprocess.run(list(args), input=stdin, capture_output=True, text=True)
    except FileNotFoundError:
        return 127, "", f"{args[0]}: command not found"
    return proc.returncode, proc.stdout, proc.stderr


def kubectl_args(context: Optional[str], args: Sequence[str]) -> List[str]:
    return ["kubectl", *(["--context", context] if context else []), *args]


class KubectlSecrets:
    def __init__(self, runner: Runner, namespace: str, context: Optional[str] = None) -> None:
        self.runner = runner
        self.namespace = namespace
        self.context = context

    def kubectl(self, args: Sequence[str], stdin: Optional[str] = None) -> Tuple[int, str, str]:
        return self.runner(kubectl_args(self.context, args), stdin)

    def get(self, key: str) -> Optional[Dict[str, Any]]:
        code, out, err = self.kubectl(["get", "secret", key, "-n", self.namespace, "-o", "json"])
        if code != 0:
            if "NotFound" in err:
                return None
            raise ReleaseError(f"get secret {key}: {err.strip()}")
        return json.loads(out)

    def create(self, secret: Dict[str, Any]) -> None:
        code, _, err = self.kubectl(["create", "-n", self.namespace, "-f", "-"], json.dumps(secret))
        if code != 0:
            if "AlreadyExists" in err:
                raise ReleaseError("release: already exists")
            raise ReleaseError(f"create: failed to create: {err.strip()}")

    def update(self, secret: Dict[str, Any]) -> None:
        code, _, err = self.kubectl(["replace", "-n", self.namespace, "-f", "-"], json.dumps(secret))
        if code != 0:
            raise ReleaseError(f"update: failed to update: {err.strip()}")

    def list(self, labels: Dict[str, str]) -> List[Dict[str, Any]]:
        selector = ",".join(f"{key}={value}" for key, value in sorted(labels.items()))
        code, out, err = self.kubectl(["get", "secrets", "-n", self.namespace, "-l", selector, "-o", "json"])
        if code != 0:
            raise ReleaseError(f"query: failed to query with labels: {err.strip()}")
        return json.loads(out).get("items", [])

    def delete(self, key: str) -> None:
        code, _, err = self.kubectl(["delete", "secret", key, "-n", self.namespace, "--ignore-not-found"])
        if code != 0:
            raise ReleaseError(f"delete secret {key}: {err.strip()}")


class ReleaseStorage:
    def __init__(self, backend: Any, namespace: str, clock: Callable[[], datetime] = utc_now) -> None:
        self.backend = backend
        self.namespace = namespace
        self.clock = clock

    def create(self, record: Dict[str, Any]) -> None:
        self.backend.create(secret_object(record, self.clock(), created=True))

    def update(self, record: Dict[str, Any]) -> None:
        secret = secret_object(record, self.clock(), created=False)
        previous = self.backend.get(secret["metadata"]["name"])
        created = ((previous or {}).get("metadata", {}).get("labels") or {}).get("createdAt")
        if created:
            secret["metadata"]["labels"]["createdAt"] = created
        self.backend.update(secret)

    def history(self, name: str) -> List[Dict[str, Any]]:
        records: List[Dict[str, Any]] = []
        for secret in self.backend.list({"owner": "helm", "name": name}):
            try:
                records.append(release_from_secret(secret))
            except ReleaseError:
                continue
        return sorted(records, key=lambda record: record.get("version", 0))

    def delete(self, name: str, version: int) -> None:
        self.backend.delete(release_key(name, version))


def object_key(doc: Dict[str, Any]) -> Tuple[str, str, str]:
    metadata = doc.get("metadata") if isinstance(doc.get("metadata"), dict) else {}
    return str(doc.get("apiVersion", "")), str(doc.get("kind", "")), str(metadata.get("name", ""))


def manifest_objects(manifest: Optional[str]) -> List[Tuple[Tuple[str, str, str], str]]:
    objects = []
    for content in split_manifests(manifest or ""):
        doc = parse_yaml(content)
        if isinstance(doc, dict):
            objects.append((object_key(doc), content))
    return objects


def stale_objects(previous: Optional[str], current: List[Tuple[Tuple[str, str, str], str]]) -> List[Tuple[Tuple[str, str, str], str]]:
    keep = {key for key, _ in current}
    return [(key, content) for key, content in manifest_objects(previous) if key not in keep]


class RecordingApplier:
    def __init__(self, fail_with: Optional[str] = None) -> None:
        self.calls: List[Tuple[str, str, List[Tuple[str, str, str]]]] = []
        self.live: Dict[Tuple[str, str, str, str], str] = {}
        self.fail_with = fail_with

    def apply(self, namespace: str, manifest: str, previous: Optional[str]) -> None:
        if self.fail_with:
            raise ApplyError(self.fail_with)
        objects = manifest_objects(manifest)
        stale = stale_objects(previous, objects)
        self.calls.append(("apply", namespace, [key for key, _ in objects]))
        if stale:
            self.calls.append(("delete", namespace, [key for key, _ in stale]))
        for key, _ in stale:
            self.live.pop((namespace, *key), None)
        for key, content in objects:
            self.live[(namespace, *key)] = content

    def delete(self, namespace: str, manifest: str) -> None:
        objects = manifest_objects(manifest)
        self.calls.append(("delete", namespace, [key for key, _ in objects]))
        for key, _ in objects:
            self.live.pop((namespace, *key), None)


class KubectlApplier:
    def __init__(self, runner: Runner, context: Optional[str] = None) -> None:
        self.runner = runner
        self.context = context

    def run(self, args: Sequence[str], stdin: str) -> None:
        code, out, err = self.runner(kubectl_args(self.context, args), stdin)
        if code != 0:
            raise ApplyError((err or out).strip() or f"kubectl {' '.join(args)} exited {code}")

    def apply(self, namespace: str, manifest: str, previous: Optional[str]) -> None:
        objects = manifest_objects(manifest)
        if objects:
            self.run(["apply", "-n", namespace, "-f", "-"], manifest)
        stale = stale_objects(previous, objects)
        if stale:
            self.run(["delete", "-n", namespace, "--ignore-not-found", "-f", "-"], "---\n".join(content for _, content in stale))

    def delete(self, namespace: str, manifest: str) -> None:
        if manifest_objects(manifest):
            self.run(["delete", "-n", namespace, "--ignore-not-found", "-f", "-"], manifest)


class Manager:
    def __init__(self, storage: ReleaseStorage, applier: Any, clock: Callable[[], datetime] = utc_now, max_history: int = 10) -> None:
        self.storage = storage
        self.applier = applier
        self.clock = clock
        self.max_history = max_history

    @property
    def namespace(self) -> str:
        return self.storage.namespace

    def install(self, name: str, chart: Chart, config: Dict[str, Any]) -> Dict[str, Any]:
        validate_release_name(name)
        if self.storage.history(name):
            raise ReleaseError("cannot reuse a name that is still in use")
        rendered = render_chart(chart, config, name, self.namespace, 1, False)
        record = new_record(name, self.namespace, 1, chart_record(chart), config, rendered.manifest, self.clock(), "pending-install", "Initial install underway")
        self.storage.create(record)
        try:
            self.applier.apply(self.namespace, rendered.manifest, None)
        except ApplyError as exc:
            set_status(record, "failed", f'Release "{name}" failed: {exc}')
            self.storage.update(record)
            raise ReleaseError(f"INSTALLATION FAILED: {exc}") from exc
        set_status(record, "deployed", "Install complete")
        self.storage.update(record)
        return record

    def upgrade(
        self,
        name: str,
        chart: Chart,
        config: Dict[str, Any],
        install: bool = False,
        reuse_values: bool = False,
        reset_values: bool = False,
        reset_then_reuse_values: bool = False,
    ) -> Dict[str, Any]:
        history = self.storage.history(name)
        if not history:
            if install:
                return self.install(name, chart, config)
            raise ReleaseError(f'UPGRADE FAILED: "{name}" has no deployed releases')
        last = history[-1]
        if last["info"]["status"] in PENDING:
            raise ReleaseError(f"UPGRADE FAILED: {ERR_PENDING}")
        deployed = [record for record in history if record["info"]["status"] == "deployed"]
        if deployed:
            current = deployed[-1]
        elif last["info"]["status"] in ("failed", "superseded"):
            current = last
        else:
            raise ReleaseError(f'UPGRADE FAILED: "{name}" has no deployed releases')
        old_config = floatify(current.get("config") or {})
        defaults = None
        if reuse_values:
            defaults = coalesce(floatify((current.get("chart") or {}).get("values") or {}), old_config)
            config = merge_maps(old_config, config)
        elif reset_then_reuse_values:
            config = merge_maps(old_config, config)
        elif not config and not reset_values:
            config = old_config
        revision = last["version"] + 1
        rendered = render_chart(chart, config, name, current["namespace"], revision, True, defaults)
        self.prune(name, history)
        record = new_record(name, current["namespace"], revision, chart_record(chart), config, rendered.manifest, self.clock(), "pending-upgrade", "Preparing upgrade", current["info"].get("first_deployed"))
        self.storage.create(record)
        try:
            self.applier.apply(self.namespace, rendered.manifest, current.get("manifest"))
        except ApplyError as exc:
            set_status(record, "failed", f'Upgrade "{name}" failed: {exc}')
            self.storage.update(record)
            raise ReleaseError(f"UPGRADE FAILED: {exc}") from exc
        for older in deployed:
            set_status(older, "superseded")
            self.storage.update(older)
        set_status(record, "deployed", "Upgrade complete")
        self.storage.update(record)
        return record

    def rollback(self, name: str, revision: int = 0) -> Dict[str, Any]:
        history = self.storage.history(name)
        if not history:
            raise ReleaseError("release: not found")
        if revision < 0:
            raise ReleaseError("invalid release revision")
        last = history[-1]
        target_version = revision or last["version"] - 1
        target = next((record for record in history if record["version"] == target_version), None)
        if target is None:
            raise ReleaseError(f"release has no {target_version} version")
        deployed = [record for record in history if record["info"]["status"] == "deployed"]
        current = last
        self.prune(name, history)
        record = new_record(name, target["namespace"], last["version"] + 1, target["chart"], target.get("config") or {}, target.get("manifest", ""), self.clock(), "pending-rollback", f"Rollback to {target_version}", current["info"].get("first_deployed"))
        record["info"]["rollback_revision"] = target_version
        self.storage.create(record)
        try:
            self.applier.apply(self.namespace, record["manifest"], current.get("manifest"))
        except ApplyError as exc:
            set_status(current, "superseded")
            self.storage.update(current)
            set_status(record, "failed", f'Rollback "{name}" failed: {exc}')
            self.storage.update(record)
            raise ReleaseError(f'Rollback "{name}" failed: {exc}') from exc
        for older in deployed:
            set_status(older, "superseded")
            self.storage.update(older)
        set_status(record, "deployed")
        self.storage.update(record)
        return record

    def prune(self, name: str, history: List[Dict[str, Any]]) -> None:
        if self.max_history <= 0:
            return
        deployed = [record["version"] for record in history if record["info"]["status"] == "deployed"]
        keep = deployed[-1] if deployed else None
        records = list(history)
        for record in list(records):
            if len(records) <= self.max_history - 1:
                break
            if record["version"] == keep:
                continue
            self.storage.delete(name, record["version"])
            records.remove(record)

    def history(self, name: str) -> List[Dict[str, Any]]:
        history = self.storage.history(name)
        if not history:
            raise ReleaseError("release: not found")
        return history

    def get(self, name: str, revision: int = 0) -> Dict[str, Any]:
        history = self.history(name)
        if not revision:
            deployed = [record for record in history if record["info"]["status"] == "deployed"]
            return deployed[-1] if deployed else history[-1]
        for record in history:
            if record["version"] == revision:
                return record
        raise ReleaseError(f"release: not found (no revision {revision})")

    def uninstall(self, name: str, keep_history: bool = False) -> Dict[str, Any]:
        history = self.history(name)
        target = self.get(name)
        if target["info"]["status"] == "uninstalled":
            raise ReleaseError(f'release "{name}" is already uninstalled; nothing to do')
        set_status(target, "uninstalling", "Deletion in progress (or silently failed)")
        self.storage.update(target)
        self.applier.delete(self.namespace, target.get("manifest", ""))
        if keep_history:
            target["info"]["deleted"] = rfc3339(self.clock())
            set_status(target, "uninstalled", "Uninstallation complete")
            self.storage.update(target)
        else:
            for record in history:
                self.storage.delete(name, record["version"])
        return target


def make_manager(args: argparse.Namespace, runner: Runner, clock: Callable[[], datetime]) -> Manager:
    store = args.store
    if store == "memory":
        backend: Any = MemorySecrets()
    elif store == "secrets":
        backend = KubectlSecrets(runner, args.namespace, args.kube_context)
    elif store.startswith("dir:") and len(store) > 4:
        backend = DirSecrets(store[4:], args.namespace)
    else:
        raise InputError(f"--store must be dir:PATH, secrets or memory, got {store!r}")
    applier = KubectlApplier(runner, args.kube_context) if args.applier == "kubectl" else RecordingApplier()
    return Manager(ReleaseStorage(backend, args.namespace, clock), applier, clock, getattr(args, "history_max", 10))


def print_release(record: Dict[str, Any], as_json: bool) -> None:
    info = record["info"]
    summary = {
        "name": record["name"],
        "namespace": record["namespace"],
        "revision": record["version"],
        "status": info["status"],
        "chart": chart_label(record),
        "description": info.get("description", ""),
        "last_deployed": info.get("last_deployed", ""),
    }
    if as_json:
        print(json.dumps(summary, indent=2))
        return
    print(f"NAME: {summary['name']}\nLAST DEPLOYED: {summary['last_deployed']}\nNAMESPACE: {summary['namespace']}\nSTATUS: {summary['status']}\nREVISION: {summary['revision']}\nCHART: {summary['chart']}\nDESCRIPTION: {summary['description']}")


def human_time(text: str) -> str:
    match = re.match(r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})(?:\.\d+)?(Z|[+-]\d{2}:\d{2})$", text or "")
    if match is None:
        return text
    moment = datetime.fromisoformat(match.group(1) + ("+00:00" if match.group(2) == "Z" else match.group(2)))
    return f"{moment:%a %b} {moment.day:2d} {moment:%H:%M:%S %Y}"


def render_table(rows: List[Dict[str, Any]]) -> str:
    headers = ["REVISION", "UPDATED", "STATUS", "CHART", "APP VERSION", "DESCRIPTION"]
    keys = ["revision", "updated", "status", "chart", "app_version", "description"]
    table = [headers] + [[human_time(str(row.get(key, ""))) if key == "updated" else str(row.get(key, "")) for key in keys] for row in rows]
    widths = [max(len(line[index]) for line in table) for index in range(len(headers) - 1)]
    return "\n".join("  ".join(cell.ljust(width) for cell, width in zip(line, widths)) + "  " + line[-1] for line in table)


def add_values_flags(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("-f", "--values", action="append", default=[], metavar="FILE", help="values file; repeat to layer several, later files win")
    parser.add_argument("--set", action="append", default=[], metavar="K=V", help="set a value on the command line (a.b=c, list[0].x=y, k={a,b}); typed like Helm: true, false, null and integers")
    parser.add_argument("--set-string", action="append", default=[], metavar="K=V", help="like --set but every value stays a string")


def add_common(parser: argparse.ArgumentParser, store: bool = True) -> None:
    parser.add_argument("-n", "--namespace", default="default", help="release namespace (default: default)")
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    if store:
        parser.add_argument("--store", default="dir:.minihelm", help="release records: dir:PATH (Secret JSON files, default dir:.minihelm), secrets (real Secrets through kubectl) or memory")
        parser.add_argument("--applier", choices=("none", "kubectl"), default="none", help="none records what would be applied; kubectl runs kubectl apply/delete")
        parser.add_argument("--kube-context", help="kubectl context for --store secrets and --applier kubectl")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="minihelm", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    template = sub.add_parser("template", help="render a chart to a manifest without storing anything")
    template.add_argument("name")
    template.add_argument("chart")
    template.add_argument("--is-upgrade", action="store_true", help="render with .Release.IsUpgrade true")
    add_values_flags(template)
    add_common(template, store=False)
    values = sub.add_parser("values", help="print the computed values: chart defaults, then -f files, then --set")
    values.add_argument("chart")
    add_values_flags(values)
    add_common(values, store=False)
    install = sub.add_parser("install", help="render and apply revision 1 and store its release record")
    install.add_argument("name")
    install.add_argument("chart")
    add_values_flags(install)
    add_common(install)
    upgrade = sub.add_parser("upgrade", help="render and apply a new revision")
    upgrade.add_argument("name")
    upgrade.add_argument("chart")
    upgrade.add_argument("-i", "--install", action="store_true", help="install if the release does not exist")
    upgrade.add_argument("--reuse-values", action="store_true", help="merge new values over the last release's values and keep its chart defaults")
    upgrade.add_argument("--reset-values", action="store_true", help="ignore the last release's values")
    upgrade.add_argument("--reset-then-reuse-values", action="store_true", help="new chart defaults, then the last release's values, then new values")
    upgrade.add_argument("--history-max", type=int, default=10, help="keep at most this many revisions (0 for no limit)")
    add_values_flags(upgrade)
    add_common(upgrade)
    rollback = sub.add_parser("rollback", help="write a new revision that replays an old revision's manifest")
    rollback.add_argument("name")
    rollback.add_argument("revision", nargs="?", type=int, default=0, help="revision to roll back to (default: the previous one)")
    rollback.add_argument("--history-max", type=int, default=10)
    add_common(rollback)
    history = sub.add_parser("history", help="list a release's revisions")
    history.add_argument("name")
    add_common(history)
    uninstall = sub.add_parser("uninstall", help="delete the release's objects and its records")
    uninstall.add_argument("name")
    uninstall.add_argument("--keep-history", action="store_true", help="keep the records and mark the last one uninstalled")
    add_common(uninstall)
    get = sub.add_parser("get", help="print the manifest or values stored in a release record")
    get.add_argument("what", choices=("manifest", "values"))
    get.add_argument("name")
    get.add_argument("--revision", type=int, default=0)
    get.add_argument("--all", action="store_true", help="values: print computed values, not only user-supplied ones")
    add_common(get)
    decode = sub.add_parser("decode", help="decode a release Secret (kubectl get secret -o json) or its base64 payload into the record JSON")
    decode.add_argument("source", nargs="?", default="-", help="file, or - for stdin")
    return parser


def warn(chart: Chart) -> None:
    for message in chart.warnings:
        print(f"minihelm: warning: {message}", file=sys.stderr)


def dispatch(args: argparse.Namespace, runner: Runner, clock: Callable[[], datetime]) -> int:
    if args.command == "decode":
        try:
            text = sys.stdin.read() if args.source == "-" else open(args.source, encoding="utf-8").read()
        except OSError as exc:
            raise InputError(f"cannot read {args.source}: {exc.strerror}") from exc
        try:
            print(json.dumps(decode_any(text), indent=2, ensure_ascii=False))
        except (ValueError, KeyError) as exc:
            raise ReleaseError(f"cannot decode {args.source}: {exc}") from exc
        return 0
    if args.command in ("template", "values", "install", "upgrade"):
        chart = load_chart(args.chart)
        warn(chart)
        config = user_values(args.values, args.set, args.set_string)
        if args.command == "values":
            computed = jsonable(coalesce(chart.values, config))
            print(json.dumps(computed, indent=2, sort_keys=True) if args.json else to_yaml(computed), end="\n" if args.json else "")
            return 0
        if args.command == "template":
            rendered = render_chart(chart, config, args.name, args.namespace, 1, args.is_upgrade)
            if args.json:
                print(json.dumps(jsonable([entry["doc"] for entry in rendered.entries + rendered.hooks]), indent=2))
            else:
                sys.stdout.write(template_output(rendered))
            return 0
        manager = make_manager(args, runner, clock)
        if chart_hooks(chart, config, args):
            print("minihelm: warning: this chart has hooks; minihelm renders them but never runs them", file=sys.stderr)
        if args.command == "install":
            record = manager.install(args.name, chart, config)
        else:
            record = manager.upgrade(args.name, chart, config, args.install, args.reuse_values, args.reset_values, args.reset_then_reuse_values)
        print_release(record, args.json)
        return 0
    manager = make_manager(args, runner, clock)
    if args.command == "rollback":
        record = manager.rollback(args.name, args.revision)
        if not args.json:
            print("Rollback was a success! Happy Helming!")
        print_release(record, args.json)
    elif args.command == "history":
        rows = [history_row(record) for record in manager.history(args.name)]
        print(json.dumps(rows, indent=2) if args.json else render_table(rows))
    elif args.command == "uninstall":
        manager.uninstall(args.name, args.keep_history)
        print(f'release "{args.name}" uninstalled')
    elif args.command == "get":
        record = manager.get(args.name, args.revision)
        if args.what == "manifest":
            sys.stdout.write(record.get("manifest", ""))
        else:
            values = record.get("config") or {}
            if args.all:
                values = jsonable(coalesce(floatify((record.get("chart") or {}).get("values") or {}), floatify(values)))
            if args.json:
                print(json.dumps(values, indent=2, sort_keys=True))
            else:
                print("COMPUTED VALUES:" if args.all else "USER-SUPPLIED VALUES:")
                sys.stdout.write(to_yaml(values) if values else "null\n")
    return 0


def chart_hooks(chart: Chart, config: Dict[str, Any], args: argparse.Namespace) -> bool:
    try:
        return bool(render_chart(chart, config, args.name, args.namespace).hooks)
    except MinihelmError:
        return False


def main(argv: Optional[Sequence[str]] = None, runner: Optional[Runner] = None, clock: Optional[Callable[[], datetime]] = None) -> int:
    args = build_parser().parse_args(list(sys.argv[1:] if argv is None else argv))
    try:
        return dispatch(args, runner or subprocess_runner, clock or utc_now)
    except MinihelmError as exc:
        print(f"minihelm: {exc}", file=sys.stderr)
        return exc.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
