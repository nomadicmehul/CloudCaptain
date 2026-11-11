from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Set, Tuple
from urllib.parse import urlparse

DEFAULT_NAMESPACE = "default"
API_VERSIONS = ("backstage.io/v1alpha1", "backstage.io/v1beta1")
KINDS = ("API", "Component", "Domain", "Group", "Location", "Resource", "System", "User")
ROOT_FIELDS = ("apiVersion", "kind", "metadata", "spec")
ONCALL_ANNOTATIONS = (
    "pagerduty.com/service-id",
    "pagerduty.com/integration-key",
    "opsgenie.com/team",
    "opsgenie.com/component-selector",
)
KUBERNETES_ID = "backstage.io/kubernetes-id"
SPECIAL_SCHEMES = ("http", "https", "ftp", "ws", "wss")


class InputError(Exception):
    pass


class YamlError(InputError):
    def __init__(self, source: str, line: int, message: str):
        super().__init__(f"{source}:{line}: {message}")
        self.source = source
        self.line = line


class RefError(ValueError):
    pass


KEY_LINE = re.compile(
    r"""^("(?:[^"\\]|\\.)*"|'(?:[^']|'')*'|[^\s"'#\[\]{},&*!|>%@`-][^#]*?|-[^\s#][^#]*?)\s*:(?:[ \t]+(.*))?$"""
)
BLOCK_HEADER = re.compile(r"^([|>])([-+]?)$")
INT_DECIMAL = re.compile(r"[-+]?[0-9]+")
FLOAT = re.compile(r"[-+]?(\.[0-9]+|[0-9]+(\.[0-9]*)?)([eE][-+]?[0-9]+)?")
ESCAPES = {"0": "\0", "a": "\a", "b": "\b", "t": "\t", "n": "\n", "v": "\v", "f": "\f", "r": "\r", "e": "\x1b",
           " ": " ", '"': '"', "/": "/", "\\": "\\", "N": "\x85", "_": "\xa0"}


def strip_comment(text: str) -> str:
    quote = ""
    for index, char in enumerate(text):
        if quote:
            if char == quote:
                quote = ""
        elif char in "\"'" and (index == 0 or text[index - 1] in " \t[{,:"):
            quote = char
        elif char == "#" and (index == 0 or text[index - 1] in " \t"):
            return text[:index].rstrip()
    return text.rstrip()


def resolve_plain(text: str) -> Any:
    if text in ("", "~", "null", "Null", "NULL"):
        return None
    if text in ("true", "True", "TRUE"):
        return True
    if text in ("false", "False", "FALSE"):
        return False
    if INT_DECIMAL.fullmatch(text):
        return int(text)
    if re.fullmatch(r"0o[0-7]+", text):
        return int(text[2:], 8)
    if re.fullmatch(r"0x[0-9a-fA-F]+", text):
        return int(text[2:], 16)
    if FLOAT.fullmatch(text):
        return float(text)
    return text


def split_flow(text: str) -> List[str]:
    parts, depth, quote, start = [], 0, "", 0
    for index, char in enumerate(text):
        if quote:
            if char == quote:
                quote = ""
        elif char in "\"'":
            quote = char
        elif char in "[{":
            depth += 1
        elif char in "]}":
            depth -= 1
        elif char == "," and depth == 0:
            parts.append(text[start:index].strip())
            start = index + 1
    tail = text[start:].strip()
    if tail:
        parts.append(tail)
    return parts


def unquote_double(body: str) -> str:
    out, index = [], 0
    while index < len(body):
        char = body[index]
        if char != "\\":
            out.append(char)
            index += 1
            continue
        code = body[index + 1] if index + 1 < len(body) else ""
        if code in ESCAPES:
            out.append(ESCAPES[code])
            index += 2
        elif code in ("x", "u", "U"):
            width = {"x": 2, "u": 4, "U": 8}[code]
            out.append(chr(int(body[index + 2:index + 2 + width], 16)))
            index += 2 + width
        else:
            raise ValueError(f"unknown escape \\{code}")
    return "".join(out)


class YamlReader:
    def __init__(self, lines: List[str], source: str, first_line: int):
        self.lines = lines
        self.source = source
        self.first_line = first_line
        self.pos = 0

    def fail(self, index: int, message: str) -> YamlError:
        return YamlError(self.source, self.first_line + index, message)

    def next_content(self) -> Optional[int]:
        index = self.pos
        while index < len(self.lines):
            stripped = self.lines[index].strip()
            if stripped and not stripped.startswith("#"):
                return index
            index += 1
        return None

    def indent_at(self, index: int) -> int:
        line = self.lines[index]
        indent = len(line) - len(line.lstrip(" "))
        if line[indent:indent + 1] == "\t":
            raise self.fail(index, "tabs are not allowed for indentation")
        return indent

    def parse_document(self) -> Any:
        index = self.next_content()
        if index is None:
            return None
        value = self.parse_block(self.indent_at(index))
        rest = self.next_content()
        if rest is not None:
            raise self.fail(rest, "unexpected content; check the indentation")
        return value

    def parse_block(self, indent: int) -> Any:
        index = self.next_content()
        text = self.lines[index][indent:]
        if text == "-" or text.startswith("- "):
            return self.parse_sequence(indent)
        if KEY_LINE.match(text):
            return self.parse_mapping(indent)
        self.pos = index + 1
        return self.parse_value(strip_comment(text), indent - 1, index, False)

    def parse_mapping(self, indent: int) -> Dict[str, Any]:
        result: Dict[str, Any] = {}
        while True:
            index = self.next_content()
            if index is None or self.indent_at(index) < indent:
                return result
            if self.indent_at(index) > indent:
                raise self.fail(index, "unexpected indentation")
            text = self.lines[index][indent:]
            match = KEY_LINE.match(text)
            if not match:
                if text == "-" or text.startswith("- "):
                    raise self.fail(index, "a list item where a key was expected")
                raise self.fail(index, f"expected 'key: value', found {text.strip()!r}")
            key = self.parse_inline(match.group(1).strip(), index)
            if not isinstance(key, str):
                key = json.dumps(key)
            if key in result:
                raise self.fail(index, f"duplicate key {key!r}")
            self.pos = index + 1
            result[key] = self.parse_value(strip_comment(match.group(2) or ""), indent, index, True)

    def parse_sequence(self, indent: int) -> List[Any]:
        items: List[Any] = []
        while True:
            index = self.next_content()
            if index is None or self.indent_at(index) < indent:
                return items
            if self.indent_at(index) > indent:
                raise self.fail(index, "unexpected indentation")
            text = self.lines[index][indent:]
            if not (text == "-" or text.startswith("- ")):
                return items
            rest = text[1:].lstrip(" ")
            column = indent + len(text) - len(rest)
            if strip_comment(rest) == "":
                self.pos = index + 1
                items.append(self.parse_value("", indent, index, False))
            elif KEY_LINE.match(rest) or rest == "-" or rest.startswith("- "):
                self.lines[index] = " " * column + rest
                items.append(self.parse_block(column))
            else:
                self.pos = index + 1
                items.append(self.parse_value(strip_comment(rest), indent, index, False))

    def parse_value(self, text: str, parent: int, index: int, in_mapping: bool) -> Any:
        if text == "":
            child = self.next_content()
            if child is None:
                return None
            child_indent = self.indent_at(child)
            child_text = self.lines[child][child_indent:]
            if child_indent > parent:
                return self.parse_block(child_indent)
            if in_mapping and child_indent == parent and (child_text == "-" or child_text.startswith("- ")):
                return self.parse_sequence(child_indent)
            return None
        header = BLOCK_HEADER.match(text)
        if header:
            return self.parse_block_scalar(header.group(1), header.group(2), parent, index)
        if text[0] in "&*!":
            raise self.fail(index, "anchors, aliases and tags are not supported by this YAML subset")
        if text[0] in "|>":
            raise self.fail(index, f"unsupported block scalar header {text!r}")
        value = self.parse_inline(text, index)
        if text[0] in "\"'[{":
            return value
        folded = [text]
        while True:
            more = self.next_content()
            if more is None or self.indent_at(more) <= parent:
                break
            folded.append(self.lines[more].strip())
            self.pos = more + 1
        return value if len(folded) == 1 else " ".join(folded)

    def parse_block_scalar(self, style: str, chomp: str, parent: int, index: int) -> str:
        body: List[str] = []
        block_indent = None
        cursor = index + 1
        while cursor < len(self.lines):
            line = self.lines[cursor]
            if line.strip() == "":
                body.append("")
                cursor += 1
                continue
            indent = len(line) - len(line.lstrip(" "))
            if block_indent is None:
                if indent <= parent:
                    break
                block_indent = indent
            if indent < block_indent:
                break
            body.append(line[block_indent:])
            cursor += 1
        trailing = 0
        while body and body[-1] == "":
            body.pop()
            trailing += 1
        self.pos = cursor - trailing
        if style == "|":
            text = "\n".join(body)
        else:
            text = ""
            for position, line in enumerate(body):
                if position == 0:
                    text = line
                elif line == "":
                    text += "\n"
                elif body[position - 1] == "":
                    text += line
                else:
                    text += " " + line
        if not body:
            return "\n" * trailing if chomp == "+" else ""
        if chomp == "-":
            return text
        if chomp == "+":
            return text + "\n" * (1 + trailing)
        return text + "\n"

    def parse_inline(self, text: str, index: int) -> Any:
        if text.startswith('"'):
            if len(text) < 2 or not text.endswith('"'):
                raise self.fail(index, "unterminated double-quoted string (multi-line quoted strings are not supported)")
            try:
                return unquote_double(text[1:-1])
            except ValueError as error:
                raise self.fail(index, str(error))
        if text.startswith("'"):
            if len(text) < 2 or not text.endswith("'"):
                raise self.fail(index, "unterminated single-quoted string (multi-line quoted strings are not supported)")
            return text[1:-1].replace("''", "'")
        if text.startswith("["):
            if not text.endswith("]"):
                raise self.fail(index, "flow sequences must close on the same line")
            return [self.parse_inline(part, index) for part in split_flow(text[1:-1])]
        if text.startswith("{"):
            if not text.endswith("}"):
                raise self.fail(index, "flow mappings must close on the same line")
            result: Dict[str, Any] = {}
            for part in split_flow(text[1:-1]):
                match = KEY_LINE.match(part)
                if not match:
                    raise self.fail(index, f"expected 'key: value' in flow mapping, found {part!r}")
                result[str(self.parse_inline(match.group(1).strip(), index))] = self.parse_inline((match.group(2) or "").strip(), index)
            return result
        return resolve_plain(text)


def parse_yaml_documents(text: str, source: str = "<string>") -> List[Tuple[int, Any, int]]:
    lines = text.replace("\r\n", "\n").split("\n")
    segments: List[Tuple[int, List[str], bool]] = []
    current: List[str] = []
    start, explicit = 1, False
    for number, line in enumerate(lines, start=1):
        if line == "---" or line.startswith("--- ") or line.startswith("---\t"):
            segments.append((start, current, explicit))
            rest = strip_comment(line[3:]).strip()
            if rest:
                raise YamlError(source, number, "content on the '---' line is not supported")
            current, start, explicit = [], number + 1, True
        elif line == "..." and current is not None:
            segments.append((start, current, explicit))
            current, start, explicit = [], number + 1, False
        elif line.startswith("%") and not current:
            start = number + 1
        else:
            current.append(line)
    segments.append((start, current, explicit))
    documents: List[Tuple[int, Any, int]] = []
    for first_line, body, _ in segments:
        value = YamlReader(body, source, first_line).parse_document()
        if value is not None:
            documents.append((len(documents), value, first_line))
    return documents


@dataclass
class Finding:
    field: str
    message: str
    severity: str = "error"

    def as_dict(self) -> Dict[str, str]:
        return {"field": self.field, "message": self.message, "severity": self.severity}


@dataclass
class Entity:
    data: Any
    source: str
    doc: int
    line: int
    findings: List[Finding] = field(default_factory=list)

    @property
    def kind(self) -> str:
        return self.data.get("kind") if isinstance(self.data, dict) and isinstance(self.data.get("kind"), str) else ""

    @property
    def metadata(self) -> Dict[str, Any]:
        meta = self.data.get("metadata") if isinstance(self.data, dict) else None
        return meta if isinstance(meta, dict) else {}

    @property
    def spec(self) -> Dict[str, Any]:
        spec = self.data.get("spec") if isinstance(self.data, dict) else None
        return spec if isinstance(spec, dict) else {}

    @property
    def namespace(self) -> str:
        value = self.metadata.get("namespace")
        return value if isinstance(value, str) and value else DEFAULT_NAMESPACE

    @property
    def name(self) -> str:
        value = self.metadata.get("name")
        return value if isinstance(value, str) else ""

    @property
    def ref(self) -> str:
        return ref_string(self.kind or "?", self.namespace, self.name or "?")

    @property
    def annotations(self) -> Dict[str, Any]:
        value = self.metadata.get("annotations")
        return value if isinstance(value, dict) else {}

    @property
    def accepted(self) -> bool:
        return not any(f.severity == "error" for f in self.findings)


def ref_string(kind: str, namespace: str, name: str) -> str:
    return f"{kind}:{namespace}/{name}".lower()


def kind_of(ref: str) -> str:
    return ref.split(":", 1)[0]


def parse_ref(text: str, default_kind: Optional[str] = None, default_namespace: str = DEFAULT_NAMESPACE) -> Tuple[str, str, str]:
    colon = text.find(":")
    slash = text.find("/")
    if slash != -1 and slash < colon:
        colon = -1
    kind = None if colon == -1 else text[:colon]
    namespace = None if slash == -1 else text[colon + 1:slash]
    name = text[max(colon + 1, slash + 1):]
    if kind == "" or namespace == "" or name == "":
        raise RefError(f"{text!r} is not of the form [<kind>:][<namespace>/]<name>")
    kind = kind or default_kind
    if not kind:
        raise RefError(f"{text!r} has no kind and this field has no default kind; write it as <kind>:{text}, for example resource:{text}")
    return kind, namespace or default_namespace, name


def is_object_name(value: Any) -> bool:
    return isinstance(value, str) and 1 <= len(value) <= 63 and re.fullmatch(r"([A-Za-z0-9][-A-Za-z0-9_.]*)?[A-Za-z0-9]", value) is not None


def is_dns_label(value: Any) -> bool:
    return isinstance(value, str) and 1 <= len(value) <= 63 and re.fullmatch(r"[a-z0-9]+(?:-+[a-z0-9]+)*", value) is not None


def is_dns_subdomain(value: Any) -> bool:
    return isinstance(value, str) and 1 <= len(value) <= 253 and all(is_dns_label(part) for part in value.split("."))


def prefix_and_suffix(value: Any, prefix_ok: Callable[[Any], bool], suffix_ok: Callable[[Any], bool]) -> bool:
    if not isinstance(value, str):
        return False
    parts = value.split("/")
    if len(parts) == 1:
        return suffix_ok(parts[0])
    return len(parts) == 2 and prefix_ok(parts[0]) and suffix_ok(parts[1])


def is_api_version(value: Any) -> bool:
    return prefix_and_suffix(value, is_dns_subdomain, lambda n: 1 <= len(n) <= 63 and re.fullmatch(r"[a-zA-Z0-9]+", n) is not None)


def is_kind_name(value: Any) -> bool:
    return isinstance(value, str) and 1 <= len(value) <= 63 and re.fullmatch(r"[a-zA-Z][a-zA-Z0-9]*", value) is not None


def is_key(value: Any) -> bool:
    return prefix_and_suffix(value, is_dns_subdomain, is_object_name)


def is_label_value(value: Any) -> bool:
    return value == "" or is_object_name(value)


def is_tag(value: Any) -> bool:
    return isinstance(value, str) and 1 <= len(value) <= 63 and re.fullmatch(r"[a-z0-9:+#]+(-[a-z0-9:+#]+)*", value) is not None


def is_url(value: Any) -> bool:
    if not isinstance(value, str) or not re.match(r"[A-Za-z][A-Za-z0-9+.-]*:", value):
        return False
    parsed = urlparse(value)
    return parsed.scheme.lower() not in SPECIAL_SCHEMES or bool(parsed.netloc)


def non_empty_string(value: Any) -> bool:
    return isinstance(value, str) and value != ""


STRING, STRINGS, PROFILE = "string", "strings", "profile"
SPEC_SCHEMAS: Dict[str, Tuple[Tuple[str, ...], Dict[str, Any]]] = {
    "Component": (("type", "lifecycle", "owner"), {"type": STRING, "lifecycle": STRING, "owner": STRING, "system": STRING,
                  "subcomponentOf": STRING, "providesApis": STRINGS, "consumesApis": STRINGS, "dependsOn": STRINGS}),
    "API": (("type", "lifecycle", "owner", "definition"), {"type": STRING, "lifecycle": STRING, "owner": STRING,
            "system": STRING, "definition": STRING}),
    "Resource": (("type", "owner"), {"type": STRING, "owner": STRING, "dependsOn": STRINGS, "system": STRING}),
    "System": (("owner",), {"owner": STRING, "domain": STRING, "type": STRING}),
    "Domain": (("owner",), {"owner": STRING, "subdomainOf": STRING, "type": STRING}),
    "Group": (("type", "children"), {"type": STRING, "profile": PROFILE, "parent": STRING, "children": STRINGS, "members": STRINGS}),
    "User": (("memberOf",), {"profile": PROFILE, "memberOf": STRINGS}),
    "Location": ((), {"type": STRING, "target": STRING, "targets": STRINGS, "presence": ("required", "optional")}),
}

ALL_KINDS = KINDS
RELATION_FIELDS: Dict[str, List[Tuple[str, str, Optional[str], Optional[Tuple[str, ...]]]]] = {
    "Component": [("spec.owner", "ownedBy", "Group", ("Group", "User")), ("spec.subcomponentOf", "partOf", "Component", None),
                  ("spec.providesApis", "providesApi", "API", None), ("spec.consumesApis", "consumesApi", "API", None),
                  ("spec.dependsOn", "dependsOn", None, None), ("spec.dependencyOf", "dependencyOf", None, None),
                  ("spec.system", "partOf", "System", None)],
    "API": [("spec.owner", "ownedBy", "Group", ("Group", "User")), ("spec.system", "partOf", "System", None)],
    "Resource": [("spec.owner", "ownedBy", "Group", ("Group", "User")), ("spec.dependsOn", "dependsOn", None, None),
                 ("spec.dependencyOf", "dependencyOf", None, None), ("spec.system", "partOf", "System", None)],
    "System": [("spec.owner", "ownedBy", "Group", ("Group", "User")), ("spec.domain", "partOf", "Domain", None)],
    "Domain": [("spec.owner", "ownedBy", "Group", ("Group", "User")), ("spec.subdomainOf", "partOf", "Domain", None)],
    "Group": [("spec.parent", "childOf", "Group", ("Group",)), ("spec.children", "parentOf", "Group", ("Group",)),
              ("spec.members", "hasMember", "User", ("User",))],
    "User": [("spec.memberOf", "memberOf", "Group", ("Group",))],
    "Location": [],
}
RELATION_PAIRS: List[Tuple[str, str, Tuple[str, ...], Tuple[str, ...]]] = [
    ("ownedBy", "ownerOf", ALL_KINDS, ("Group", "User")),
    ("providesApi", "apiProvidedBy", ("Component",), ("API",)),
    ("consumesApi", "apiConsumedBy", ("Component",), ("API",)),
    ("dependsOn", "dependencyOf", ("Component", "Resource"), ("Component", "Resource")),
    ("parentOf", "childOf", ("Group",), ("Group",)),
    ("memberOf", "hasMember", ("User",), ("Group",)),
    ("partOf", "hasPart", ("Component", "API", "Resource"), ("Component", "System")),
    ("partOf", "hasPart", ("System",), ("Domain",)),
    ("partOf", "hasPart", ("Domain",), ("Domain",)),
]


def reverse_of(relation: str, from_kind: str, to_kind: str) -> Optional[str]:
    for forward, reverse, sources, targets in RELATION_PAIRS:
        for kind_a, kind_b, name, opposite in ((sources, targets, forward, reverse), (targets, sources, reverse, forward)):
            if name == relation and from_kind.lower() in [k.lower() for k in kind_a] and to_kind.lower() in [k.lower() for k in kind_b]:
                return opposite
    return None


def check_envelope(data: Dict[str, Any], add: Callable[[str, str], None]) -> None:
    for key in data:
        if key not in ROOT_FIELDS:
            add(key, f"unknown root field {key!r}; only apiVersion, kind, metadata and spec are allowed (move it under spec or into an annotation)")
    for key, check, expectation in (("apiVersion", is_api_version, "a group/version such as backstage.io/v1alpha1"),
                                    ("kind", is_kind_name, "a letter followed by letters and digits")):
        value = data.get(key)
        if value is None:
            add(key, "is required")
        elif not non_empty_string(value):
            add(key, "must be a non-empty string")
        elif not check(value):
            add(key, f"{value!r} is not valid; expected {expectation}")
    meta = data.get("metadata")
    if not isinstance(meta, dict):
        add("metadata", "is required and must be a mapping")
        return
    name = meta.get("name")
    if name is None:
        add("metadata.name", "is required")
    elif not non_empty_string(name):
        add("metadata.name", f"must be a non-empty string, found {json.dumps(name)}")
    elif not is_object_name(name):
        add("metadata.name", f"{name!r} is not valid; expected [a-zA-Z0-9] sequences separated by -, _ or ., at most 63 characters")
    if "namespace" in meta:
        namespace = meta["namespace"]
        if not non_empty_string(namespace):
            add("metadata.namespace", "must be a non-empty string")
        elif not is_dns_label(namespace):
            add("metadata.namespace", f"{namespace!r} is not valid; expected [a-z0-9] sequences separated by -, at most 63 characters")
    for key in ("title", "description"):
        if key in meta and not isinstance(meta[key], str):
            add(f"metadata.{key}", "must be a string")
    if "title" in meta and meta["title"] == "":
        add("metadata.title", "must not be empty")
    for group, value_check in (("labels", is_label_value), ("annotations", lambda v: True)):
        values = meta.get(group)
        if values is None:
            continue
        if not isinstance(values, dict):
            add(f"metadata.{group}", "must be a mapping")
            continue
        for key, value in values.items():
            if not isinstance(value, str):
                add(f"metadata.{group}.{key}", f"value must be a string, found {json.dumps(value)} (quote it)")
            elif not is_key(key):
                add(f"metadata.{group}.{key}", f"key {key!r} is not valid; expected an optional DNS prefix and a name, like example.com/my-key")
            elif not value_check(value):
                add(f"metadata.{group}.{key}", f"value {value!r} is not a valid label value")
    tags = meta.get("tags")
    if tags is not None:
        if not isinstance(tags, list):
            add("metadata.tags", "must be a list")
        else:
            for index, tag in enumerate(tags):
                if not non_empty_string(tag):
                    add(f"metadata.tags.{index}", "must be a non-empty string")
                elif not is_tag(tag):
                    add(f"metadata.tags.{index}", f"{tag!r} is not valid; expected lowercase [a-z0-9:+#] sequences separated by -, at most 63 characters")
    links = meta.get("links")
    if links is not None:
        if not isinstance(links, list):
            add("metadata.links", "must be a list")
        else:
            for index, link in enumerate(links):
                if not isinstance(link, dict):
                    add(f"metadata.links.{index}", "must be a mapping with a url")
                    continue
                if "url" not in link:
                    add(f"metadata.links.{index}.url", "is required")
                elif not non_empty_string(link["url"]) or not is_url(link["url"]):
                    add(f"metadata.links.{index}.url", f"{link['url']!r} is not a valid URL")
                for key in ("title", "icon", "type"):
                    if key in link and not non_empty_string(link[key]):
                        add(f"metadata.links.{index}.{key}", "must be a non-empty string")
    if "spec" in data and not isinstance(data["spec"], dict):
        add("spec", "must be a mapping")


def check_spec(kind: str, data: Dict[str, Any], add: Callable[[str, str], None]) -> None:
    spec = data.get("spec")
    if not isinstance(spec, dict):
        add("spec", f"is required for kind {kind}")
        return
    required, properties = SPEC_SCHEMAS[kind]
    for key in required:
        if key not in spec:
            add(f"spec.{key}", f"is required for kind {kind}")
    for key, shape in properties.items():
        if key not in spec:
            continue
        value = spec[key]
        path = f"spec.{key}"
        if shape == STRING and not non_empty_string(value):
            add(path, f"must be a non-empty string, found {json.dumps(value)}")
        elif shape == STRINGS:
            if not isinstance(value, list):
                add(path, f"must be a list of strings, found {json.dumps(value)}")
                continue
            for index, item in enumerate(value):
                if not non_empty_string(item):
                    add(f"{path}.{index}", f"must be a non-empty string, found {json.dumps(item)}")
        elif shape == PROFILE:
            if not isinstance(value, dict):
                add(path, "must be a mapping")
                continue
            for part in ("displayName", "email", "picture"):
                if part in value and not non_empty_string(value[part]):
                    add(f"{path}.{part}", "must be a non-empty string")
        elif isinstance(shape, tuple) and value not in shape:
            add(path, f"must be one of {', '.join(shape)}, found {json.dumps(value)}")


def field_values(entity_data: Dict[str, Any], path: str) -> List[Any]:
    value: Any = entity_data
    for part in path.split("."):
        value = value.get(part) if isinstance(value, dict) else None
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def check_references(kind: str, data: Dict[str, Any], namespace: str, add: Callable[[str, str], None]) -> None:
    for path, _, default_kind, _ in RELATION_FIELDS.get(kind, []):
        for item in field_values(data, path):
            if not non_empty_string(item):
                continue
            try:
                parse_ref(item, default_kind, namespace)
            except RefError as error:
                add(path, str(error))


def validate_entity(data: Any) -> List[Finding]:
    if not isinstance(data, dict):
        return [Finding("<root>", f"expected a mapping at the document root, found {type(data).__name__}")]
    found: List[Finding] = []

    def add(path: str, message: str) -> None:
        if path not in {f.field for f in found}:
            found.append(Finding(path, message))

    check_envelope(data, add)
    kind, api_version = data.get("kind"), data.get("apiVersion")
    if isinstance(kind, str) and isinstance(api_version, str) and kind and api_version:
        if kind not in KINDS:
            close = [k for k in KINDS if k.lower() == kind.lower()]
            hint = f"; kinds are case-sensitive, did you mean {close[0]}?" if close else f"; the built-in kinds are {', '.join(KINDS)}"
            add("kind", f"unknown kind {kind!r}{hint}")
        elif api_version not in API_VERSIONS:
            add("apiVersion", f"{api_version!r} is not a version of kind {kind}; use {' or '.join(API_VERSIONS)}")
        else:
            check_spec(kind, data, add)
            meta = data.get("metadata") if isinstance(data.get("metadata"), dict) else {}
            namespace = meta.get("namespace") if non_empty_string(meta.get("namespace")) else DEFAULT_NAMESPACE
            check_references(kind, data, namespace, add)
    return found


@dataclass
class Edge:
    source: str
    relation: str
    target: str
    path: str
    forward: bool


def edges_of(entity: Entity) -> Tuple[List[Edge], List[Finding]]:
    edges: List[Edge] = []
    notes: List[Finding] = []
    source = entity.ref
    for path, relation, default_kind, allowed in RELATION_FIELDS.get(entity.kind, []):
        for item in field_values(entity.data, path):
            if not non_empty_string(item):
                continue
            kind, namespace, name = parse_ref(item, default_kind, entity.namespace)
            if allowed and kind.lower() not in [k.lower() for k in allowed]:
                notes.append(Finding(path, f"{item!r} is a {kind}; {relation} only accepts {' or '.join(allowed)}, so Backstage drops this relation", "warning"))
                continue
            target = ref_string(kind, namespace, name)
            edges.append(Edge(source, relation, target, path, True))
            reverse = reverse_of(relation, entity.kind, kind)
            if reverse:
                edges.append(Edge(target, reverse, source, path, False))
    return edges, notes


class Catalog:
    def __init__(self, entities: Iterable[Entity]):
        self.entities: Dict[str, Entity] = {}
        self.conflicts: List[Dict[str, str]] = []
        self.dropped: List[Dict[str, str]] = []
        self.edges: List[Edge] = []
        for entity in entities:
            if not entity.accepted:
                continue
            if entity.ref in self.entities:
                first = self.entities[entity.ref]
                self.conflicts.append({"ref": entity.ref, "source": entity.source, "first": first.source,
                                       "message": f"{entity.ref} is already defined in {first.source}; the second definition is ignored"})
                continue
            self.entities[entity.ref] = entity
        self.out: Dict[str, Dict[str, Set[str]]] = {}
        for ref in sorted(self.entities):
            edges, notes = edges_of(self.entities[ref])
            for note in notes:
                self.dropped.append({"ref": ref, "field": note.field, "message": note.message})
            for edge in edges:
                self.edges.append(edge)
                self.out.setdefault(edge.source, {}).setdefault(edge.relation, set()).add(edge.target)

    def relations(self) -> List[Tuple[str, str, str]]:
        return sorted({(e.source, e.relation, e.target) for e in self.edges})

    def targets(self, ref: str, relation: str) -> List[str]:
        return sorted(self.out.get(ref, {}).get(relation, set()))

    def broken_references(self) -> List[Dict[str, str]]:
        broken = []
        for edge in self.edges:
            if edge.forward and edge.target not in self.entities:
                broken.append({"ref": edge.source, "field": edge.path, "target": edge.target,
                               "message": f"{edge.path} points at {edge.target}, which is not in the catalog"})
        return sorted(broken, key=lambda b: (b["ref"], b["field"], b["target"]))

    def resolve(self, text: str) -> str:
        try:
            kind, namespace, name = parse_ref(text, "*", DEFAULT_NAMESPACE)
        except RefError as error:
            raise InputError(str(error))
        if kind != "*":
            ref = ref_string(kind, namespace, name)
            if ref in self.entities:
                return ref
            candidates = sorted(r for r in self.entities if r.split(":", 1)[1] == ref.split(":", 1)[1])
            hint = f"; did you mean {' or '.join(candidates)}?" if candidates else ""
            raise InputError(f"no entity {ref} in the catalog{hint}")
        suffix = f"{namespace}/{name}".lower()
        candidates = sorted(r for r in self.entities if r.split(":", 1)[1] == suffix)
        if len(candidates) == 1:
            return candidates[0]
        if candidates:
            raise InputError(f"{text!r} is ambiguous: {', '.join(candidates)}; add the kind")
        raise InputError(f"no entity named {suffix} in the catalog")

    def owners(self, ref: str) -> List[str]:
        return self.targets(ref, "ownedBy")

    def resolved_owners(self, ref: str) -> List[str]:
        return [owner for owner in self.owners(ref) if owner in self.entities]

    def oncall(self, ref: str) -> Tuple[Dict[str, str], Optional[str]]:
        for candidate in [ref] + self.resolved_owners(ref):
            found = {k: v for k, v in self.entities[candidate].annotations.items() if k in ONCALL_ANNOTATIONS and v}
            if found:
                return found, candidate
        return {}, None


@dataclass
class Loaded:
    base: str
    files: List[str]
    entities: List[Entity]
    errors: List[Dict[str, str]]
    skipped: List[Dict[str, str]]
    catalog: Catalog


def yaml_files(directory: str) -> List[str]:
    found = []
    for root, dirs, files in os.walk(directory):
        dirs.sort()
        for name in sorted(files):
            if name.endswith((".yaml", ".yml")):
                found.append(os.path.join(root, name))
    return found


def location_targets(entity: Entity) -> List[str]:
    spec = entity.spec
    targets = [spec["target"]] if non_empty_string(spec.get("target")) else []
    targets += [t for t in spec.get("targets", []) if non_empty_string(t)] if isinstance(spec.get("targets"), list) else []
    return targets


def load(paths: Sequence[str]) -> Loaded:
    queue: deque = deque()
    for path in paths:
        if os.path.isdir(path):
            for found in yaml_files(path):
                queue.append((found, "required", "<command line>"))
        else:
            queue.append((path, "required", "<command line>"))
    seen: Set[str] = set()
    raw: List[Tuple[str, List[Tuple[int, Any, int]]]] = []
    errors: List[Dict[str, str]] = []
    skipped: List[Dict[str, str]] = []
    while queue:
        path, presence, via = queue.popleft()
        real = os.path.realpath(path)
        if real in seen:
            continue
        seen.add(real)
        if not os.path.isfile(real):
            entry = {"path": real, "via": via, "message": "location target does not exist"}
            (skipped if presence == "optional" else errors).append(entry)
            continue
        try:
            with open(real, encoding="utf-8") as handle:
                documents = parse_yaml_documents(handle.read(), real)
        except (OSError, UnicodeDecodeError) as error:
            errors.append({"path": real, "via": via, "message": f"cannot read: {error}"})
            continue
        except YamlError as error:
            errors.append({"path": real, "via": via, "message": f"YAML error on line {error.line}: {str(error).split(': ', 1)[1]}"})
            continue
        raw.append((real, documents))
        for _, value, _ in documents:
            if isinstance(value, dict) and value.get("kind") == "Location" and isinstance(value.get("spec"), dict):
                spec = value["spec"]
                if spec.get("type") not in (None, "file", "url"):
                    continue
                presence_of = spec.get("presence") if spec.get("presence") in ("required", "optional") else "required"
                holder = Entity(value, real, 0, 0)
                for target in location_targets(holder):
                    if re.match(r"[a-zA-Z][a-zA-Z0-9+.-]*://", target):
                        skipped.append({"path": target, "via": real, "message": "remote location targets are not fetched"})
                        continue
                    queue.append((os.path.join(os.path.dirname(real), target), presence_of, real))
    every = [p for p, _ in raw] + [e["path"] for e in errors]
    base = os.path.commonpath([os.path.dirname(p) for p in every]) if every else os.getcwd()

    def show(path: str) -> str:
        return os.path.relpath(path, base) if os.path.isabs(path) else path

    entities: List[Entity] = []
    for real, documents in raw:
        for index, value, line in documents:
            entity = Entity(value, show(real), index, line)
            entity.findings = validate_entity(value)
            entities.append(entity)
    for entry in errors + skipped:
        entry["path"] = show(entry["path"])
        entry["via"] = show(entry["via"]) if entry["via"] != "<command line>" else entry["via"]
    return Loaded(base, sorted(show(p) for p, _ in raw), entities, errors, skipped, Catalog(entities))


def blast_radius(catalog: Catalog, ref: str, max_depth: Optional[int] = None) -> Dict[str, Any]:
    seen: Dict[str, Tuple[int, List[str]]] = {ref: (0, [ref])}
    queue = deque([ref])
    impacted: List[Dict[str, Any]] = []
    while queue:
        current = queue.popleft()
        depth, path = seen[current]
        if max_depth is not None and depth >= max_depth:
            continue
        steps = [(t, "dependsOn") for t in catalog.targets(current, "dependencyOf")]
        steps += [(t, "providesApi") for t in catalog.targets(current, "providesApi")]
        steps += [(t, "consumesApi") for t in catalog.targets(current, "apiConsumedBy")]
        for target, how in steps:
            if target in seen or target not in catalog.entities:
                continue
            seen[target] = (depth + 1, path + [target])
            impacted.append({"ref": target, "depth": depth + 1, "via": how, "path": path + [target],
                             "owners": catalog.resolved_owners(target)})
            queue.append(target)
    teams = sorted({o for item in impacted for o in item["owners"]} | ({"(no owner)"} if any(not i["owners"] for i in impacted) else set()))
    systems = sorted({s for r in [ref] + [i["ref"] for i in impacted] for s in catalog.targets(r, "partOf") if kind_of(s) == "system"})
    return {"entity": ref, "impacted": impacted, "teams": teams, "systems": systems}


def owner_report(catalog: Catalog, ref: str) -> Dict[str, Any]:
    owners, via = catalog.owners(ref), "spec.owner"
    if not catalog.resolved_owners(ref):
        systems = [s for s in catalog.targets(ref, "partOf") if kind_of(s) == "system" and s in catalog.entities]
        fallback = [o for s in systems for o in catalog.resolved_owners(s)]
        if fallback:
            owners, via = fallback, f"system {systems[0]} (the entity's own owner does not resolve)"
    details = []
    for owner in owners:
        entity = catalog.entities.get(owner)
        chain, cursor = [], owner
        while cursor in catalog.entities and catalog.targets(cursor, "childOf") and len(chain) < 10:
            cursor = catalog.targets(cursor, "childOf")[0]
            chain.append(cursor)
        profile = entity.spec.get("profile", {}) if entity else {}
        details.append({"ref": owner, "exists": entity is not None,
                        "display_name": profile.get("displayName") if isinstance(profile, dict) else None,
                        "email": profile.get("email") if isinstance(profile, dict) else None,
                        "members": catalog.targets(owner, "hasMember"), "parents": chain})
    oncall, source = catalog.oncall(ref)
    for owner in [o for o in owners if o in catalog.entities and not oncall]:
        found = {k: v for k, v in catalog.entities[owner].annotations.items() if k in ONCALL_ANNOTATIONS and v}
        if found:
            oncall, source = found, owner
    return {"entity": ref, "via": via, "owners": details, "oncall": oncall, "oncall_from": source}


def gaps(catalog: Catalog, team: Optional[str] = None) -> Dict[str, List[Dict[str, Any]]]:
    report: Dict[str, List[Dict[str, Any]]] = {}
    dropped = {(d["ref"], d["field"]) for d in catalog.dropped}
    for ref in sorted(catalog.entities):
        if catalog.entities[ref].kind != "Component":
            continue
        owners = catalog.owners(ref)
        problems = []
        if not owners:
            reason = "spec.owner names something that cannot own" if (ref, "spec.owner") in dropped else "no spec.owner"
            problems.append(f"no owner ({reason})")
        elif not catalog.resolved_owners(ref):
            problems.append(f"owner {owners[0]} is not in the catalog")
        if not catalog.oncall(ref)[0]:
            problems.append("no on-call annotation on the component or its owner")
        key = owners[0] if owners else "(no owner)"
        if problems and (team is None or key == team):
            report.setdefault(key, []).append({"ref": ref, "problems": problems})
    return report


def version_tuple(text: Any) -> Optional[Tuple[int, ...]]:
    if not isinstance(text, str) or not re.fullmatch(r"v?\d+(\.\d+)*", text.strip()):
        return None
    return tuple(int(part) for part in text.strip().lstrip("v").split("."))


def run_check(catalog: Catalog, ref: str, rule: Dict[str, Any]) -> Optional[str]:
    entity = catalog.entities[ref]
    check = rule.get("check")
    if check == "owner_resolves":
        return None if catalog.resolved_owners(ref) else "owner missing or not in the catalog"
    if check == "annotation":
        keys = rule.get("any", [])
        holders = [ref] + (catalog.resolved_owners(ref) if rule.get("inherit_from_owner") else [])
        for holder in holders:
            if any(catalog.entities[holder].annotations.get(k) for k in keys):
                return None
        return f"none of {', '.join(keys)}"
    if check == "link":
        needle = str(rule.get("match", "")).lower()
        links = entity.metadata.get("links") or []
        for link in links:
            if any(needle in str(link.get(k, "")).lower() for k in ("type", "title", "url")):
                return None
        return f"no link whose type, title or url contains {needle!r}"
    if check == "relation":
        wanted = [t for t in catalog.targets(ref, rule.get("relation", "")) if not rule.get("kind") or kind_of(t) == rule["kind"].lower()]
        return None if wanted else f"no {rule.get('relation')} relation" + (f" to a {rule['kind']}" if rule.get("kind") else "")
    if check == "version_at_least":
        key, minimum = rule.get("annotation", ""), version_tuple(rule.get("minimum"))
        if minimum is None:
            raise InputError(f"rule {rule.get('id')}: minimum {rule.get('minimum')!r} is not a version")
        value = entity.annotations.get(key)
        if value is None:
            return f"no {key} annotation"
        found = version_tuple(value)
        if found is None:
            return f"{key} is {value!r}, not a version"
        return None if found >= minimum else f"{key} is {value}, needs {rule.get('minimum')} or later"
    raise InputError(f"rule {rule.get('id')}: unknown check {check!r}; use owner_resolves, annotation, link, relation or version_at_least")


def matches_filter(entity: Entity, conditions: Dict[str, Any]) -> bool:
    for path, wanted in conditions.items():
        values = field_values(entity.data, path)
        accepted = wanted if isinstance(wanted, list) else [wanted]
        if not values or values[0] not in accepted:
            return False
    return True


def scorecard(catalog: Catalog, card: Dict[str, Any]) -> Dict[str, Any]:
    levels = card.get("levels", [])
    rules = card.get("rules", [])
    if not rules:
        raise InputError("the scorecard has no rules")
    for rule in rules:
        if rule.get("level") not in levels:
            raise InputError(f"rule {rule.get('id')}: level {rule.get('level')!r} is not one of {levels}")
    results = []
    for ref in sorted(catalog.entities):
        entity = catalog.entities[ref]
        if not matches_filter(entity, card.get("filter", {})):
            continue
        failed = {}
        for rule in rules:
            reason = run_check(catalog, ref, rule)
            if reason is not None:
                failed[rule["id"]] = reason
        level = None
        for name in levels:
            if any(r["id"] in failed for r in rules if levels.index(r["level"]) <= levels.index(name)):
                break
            level = name
        owners = catalog.resolved_owners(ref)
        results.append({"ref": ref, "team": owners[0] if owners else "(no owner)", "level": level,
                        "score": round(100.0 * (len(rules) - len(failed)) / len(rules), 1),
                        "passed": [r["id"] for r in rules if r["id"] not in failed],
                        "failed": [{"id": k, "reason": v} for k, v in failed.items()]})
    teams: Dict[str, Dict[str, Any]] = {}
    for item in results:
        entry = teams.setdefault(item["team"], {"entities": 0, "score_total": 0.0, "levels": {name: 0 for name in levels + ["none"]}})
        entry["entities"] += 1
        entry["score_total"] += item["score"]
        entry["levels"][item["level"] or "none"] += 1
    summary = {team: {"entities": v["entities"], "average_score": round(v["score_total"] / v["entities"], 1), "levels": v["levels"]}
               for team, v in sorted(teams.items())}
    return {"scorecard": card.get("name", "scorecard"), "levels": levels, "entities": results, "teams": summary}


def orphans(loaded: Loaded, previous: Dict[str, Any]) -> List[Dict[str, str]]:
    found = []
    failed = {e["path"] for e in loaded.errors}
    for item in sorted(previous.get("entities", []), key=lambda e: e.get("ref", "")):
        ref, source = item.get("ref", ""), item.get("source", "")
        if ref in loaded.catalog.entities:
            continue
        if source in failed:
            found.append({"ref": ref, "source": source, "status": "stale",
                          "message": f"{source} could not be read; the entity keeps its last known state"})
        elif source in loaded.files:
            found.append({"ref": ref, "source": source, "status": "orphan",
                          "message": f"{source} no longer contains {ref}"})
        else:
            found.append({"ref": ref, "source": source, "status": "orphan",
                          "message": f"no Location points at {source} any more"})
    return found


def k8s_owners(catalog: Catalog, objects: Dict[str, Any]) -> Dict[str, Any]:
    by_id: Dict[str, List[str]] = {}
    for ref, entity in sorted(catalog.entities.items()):
        value = entity.annotations.get(KUBERNETES_ID)
        if entity.kind == "Component" and non_empty_string(value):
            by_id.setdefault(value, []).append(ref)
    items = objects.get("items", [objects]) if isinstance(objects, dict) else []
    workloads, seen_ids = [], set()
    for item in items:
        meta = item.get("metadata", {}) if isinstance(item, dict) else {}
        labels = meta.get("labels") or {}
        workload_id = labels.get(KUBERNETES_ID)
        components = by_id.get(workload_id, []) if workload_id else []
        if workload_id:
            seen_ids.add(workload_id)
        owners = sorted({o for c in components for o in catalog.resolved_owners(c)})
        status = "owned" if owners else ("unlabelled" if not workload_id else ("no-component" if not components else "no-owner"))
        workloads.append({"object": f"{item.get('kind', '?')}/{meta.get('namespace', '?')}/{meta.get('name', '?')}",
                          "kubernetes_id": workload_id, "components": components, "owners": owners, "status": status})
    not_running = sorted(ref for value, refs in by_id.items() if value not in seen_ids for ref in refs)
    return {"workloads": sorted(workloads, key=lambda w: w["object"]), "not_running": not_running}


def entity_report(entity: Entity) -> Dict[str, Any]:
    return {"source": entity.source, "doc": entity.doc, "line": entity.line, "ref": entity.ref,
            "accepted": entity.accepted, "findings": [f.as_dict() for f in entity.findings], "data": entity.data}


def export(loaded: Loaded) -> Dict[str, Any]:
    catalog = loaded.catalog
    entities = []
    for ref in sorted(catalog.entities):
        entity = catalog.entities[ref]
        relations = [{"type": r, "targetRef": t} for (s, r, t) in catalog.relations() if s == ref]
        entities.append({"ref": ref, "source": entity.source, "entity": entity.data, "relations": relations})
    return {"base": loaded.base, "entities": entities}


def emit(payload: Any, as_json: bool, text: Callable[[], List[str]]) -> None:
    if as_json:
        print(json.dumps(payload, indent=2, sort_keys=False))
    else:
        for line in text():
            print(line)


def cmd_validate(loaded: Loaded, args: argparse.Namespace) -> int:
    reports = [entity_report(e) for e in loaded.entities]
    rejected = [r for r in reports if not r["accepted"]]
    payload = {"base": loaded.base, "files": loaded.files, "entities": reports, "load_errors": loaded.errors,
               "skipped": loaded.skipped, "summary": {"files": len(loaded.files), "entities": len(reports),
                                                       "accepted": len(reports) - len(rejected), "rejected": len(rejected)}}

    def text() -> List[str]:
        lines = []
        for r in reports:
            mark = "ok      " if r["accepted"] else "REJECTED"
            lines.append(f"{mark} {r['ref']:<45} {r['source']}#{r['doc']}")
            for f in r["findings"]:
                lines.append(f"           {f['field']}: {f['message']}")
        for e in loaded.errors:
            lines.append(f"ERROR    {e['path']} (from {e['via']}): {e['message']}")
        for s in loaded.skipped:
            lines.append(f"skipped  {s['path']} (from {s['via']}): {s['message']}")
        lines.append(f"{len(loaded.files)} files, {len(reports)} entities: {len(reports) - len(rejected)} accepted, {len(rejected)} rejected, {len(loaded.errors)} location errors")
        return lines

    emit(payload, args.json, text)
    return 1 if rejected or loaded.errors else 0


def cmd_relations(loaded: Loaded, args: argparse.Namespace) -> int:
    catalog = loaded.catalog
    rows, dropped = catalog.relations(), catalog.dropped
    if args.entity:
        ref = catalog.resolve(args.entity)
        rows = [r for r in rows if r[0] == ref]
        dropped = [d for d in dropped if d["ref"] == ref]
    payload = {"relations": [{"source": s, "type": t, "target": d} for s, t, d in rows], "dropped": dropped}
    emit(payload, args.json, lambda: [f"{s:<45} {t:<14} {d}" for s, t, d in rows] + [f"dropped  {d['ref']} {d['field']}: {d['message']}" for d in dropped])
    return 0


def cmd_lint(loaded: Loaded, args: argparse.Namespace) -> int:
    catalog = loaded.catalog
    previous = read_json(args.previous) if args.previous else None
    payload = {"rejected": [entity_report(e)["ref"] for e in loaded.entities if not e.accepted],
               "load_errors": loaded.errors, "conflicts": catalog.conflicts, "dropped_relations": catalog.dropped,
               "broken_references": catalog.broken_references(), "orphans": orphans(loaded, previous) if previous else []}

    def text() -> List[str]:
        lines = [f"rejected entity {r} (run validate for details)" for r in payload["rejected"]]
        lines += [f"location error  {e['path']}: {e['message']}" for e in loaded.errors]
        lines += [f"conflict        {c['message']}" for c in catalog.conflicts]
        lines += [f"dropped         {d['ref']} {d['field']}: {d['message']}" for d in catalog.dropped]
        lines += [f"broken          {b['ref']} {b['message']}" for b in payload["broken_references"]]
        lines += [f"{o['status']:<15} {o['ref']}: {o['message']}" for o in payload["orphans"]]
        return lines or ["clean: no rejected entities, conflicts, dropped relations, broken references or orphans"]

    emit(payload, args.json, text)
    return 1 if any(payload[k] for k in payload) else 0


def cmd_owner(loaded: Loaded, args: argparse.Namespace) -> int:
    report = owner_report(loaded.catalog, loaded.catalog.resolve(args.entity))

    def text() -> List[str]:
        lines = [f"{report['entity']} is owned via {report['via']}:"]
        for o in report["owners"]:
            if not o["exists"]:
                lines.append(f"  {o['ref']} (NOT IN THE CATALOG)")
                continue
            lines.append(f"  {o['ref']} {o['display_name'] or ''} {o['email'] or ''}".rstrip())
            lines.append(f"    members: {', '.join(o['members']) or '(none)'}")
            lines.append(f"    escalation: {' -> '.join(o['parents']) or '(top level)'}")
        if not report["owners"]:
            lines.append("  nobody")
        lines.append(f"on-call: {json.dumps(report['oncall'])} from {report['oncall_from']}" if report["oncall"] else "on-call: none")
        return lines

    emit(report, args.json, text)
    return 0 if any(o["exists"] for o in report["owners"]) else 1


def cmd_blast_radius(loaded: Loaded, args: argparse.Namespace) -> int:
    report = blast_radius(loaded.catalog, loaded.catalog.resolve(args.entity), args.depth)

    def text() -> List[str]:
        lines = [f"if {report['entity']} goes down:"]
        for item in report["impacted"]:
            lines.append(f"  {'  ' * (item['depth'] - 1)}{item['ref']} ({item['via']}, owners {', '.join(item['owners']) or '(no owner)'})")
        lines.append(f"teams to notify: {', '.join(report['teams']) or '(none)'}")
        lines.append(f"systems affected: {', '.join(report['systems']) or '(none)'}")
        return lines

    emit(report, args.json, text)
    return 0


def cmd_gaps(loaded: Loaded, args: argparse.Namespace) -> int:
    team = ref_string(*parse_ref(args.team, "Group")) if args.team else None
    report = gaps(loaded.catalog, team)

    def text() -> List[str]:
        lines = []
        for key, items in report.items():
            lines.append(key)
            lines += [f"  {i['ref']}: {'; '.join(i['problems'])}" for i in items]
        return lines or ["every component has an owner in the catalog and an on-call annotation"]

    emit({"teams": report}, args.json, text)
    return 1 if report else 0


def cmd_scorecard(loaded: Loaded, args: argparse.Namespace) -> int:
    card = read_json(args.rules)
    report = scorecard(loaded.catalog, card)
    levels = report["levels"]
    if args.min_level and args.min_level not in levels:
        raise InputError(f"--min-level {args.min_level!r} is not one of {levels}")

    def text() -> List[str]:
        lines = [f"{report['scorecard']}: levels {' < '.join(levels)}"]
        for item in report["entities"]:
            lines.append(f"  {item['ref']:<45} {item['level'] or 'none':<8} {item['score']:>5}%  {item['team']}")
            lines += [f"      x {f['id']}: {f['reason']}" for f in item["failed"]]
        for team, summary in report["teams"].items():
            counts = ", ".join(f"{k} {v}" for k, v in summary["levels"].items())
            lines.append(f"  team {team}: {summary['entities']} entities, average {summary['average_score']}%, {counts}")
        return lines

    emit(report, args.json, text)
    if args.min_level:
        rank = levels.index(args.min_level)
        below = [i for i in report["entities"] if i["level"] is None or levels.index(i["level"]) < rank]
        return 1 if below else 0
    return 0


def cmd_k8s_owners(loaded: Loaded, args: argparse.Namespace) -> int:
    report = k8s_owners(loaded.catalog, read_json(args.objects))

    def text() -> List[str]:
        lines = [f"{w['status']:<12} {w['object']:<45} {', '.join(w['owners']) or '-'}" for w in report["workloads"]]
        lines += [f"not running  {ref} (has {KUBERNETES_ID} but no workload carries the label)" for ref in report["not_running"]]
        return lines

    emit(report, args.json, text)
    return 1 if any(w["status"] != "owned" for w in report["workloads"]) else 0


def cmd_export(loaded: Loaded, args: argparse.Namespace) -> int:
    payload = export(loaded)
    if args.output:
        with open(args.output, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2)
            handle.write("\n")
        print(f"wrote {len(payload['entities'])} entities to {args.output}", file=sys.stderr)
    else:
        print(json.dumps(payload, indent=2))
    return 0


def read_json(path: str) -> Any:
    try:
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)
    except OSError as error:
        raise InputError(f"cannot read {path}: {error.strerror}")
    except json.JSONDecodeError as error:
        raise InputError(f"{path} is not JSON: {error}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="minicatalog", description="A service catalog from catalog-info.yaml files: validation, relations, ownership, blast radius and scorecards.")
    sub = parser.add_subparsers(dest="command", required=True)

    def add(name: str, handler: Callable[[Loaded, argparse.Namespace], int], help_text: str, entity: bool = False) -> argparse.ArgumentParser:
        command = sub.add_parser(name, help=help_text)
        if entity:
            command.add_argument("entity", help="entity reference, [kind:][namespace/]name")
        command.add_argument("paths", nargs="+", help="catalog files or directories; Location targets are followed")
        command.add_argument("--json", action="store_true", help="machine-readable output")
        command.set_defaults(handler=handler)
        return command

    add("validate", cmd_validate, "validate every entity the way the Backstage catalog does")
    add("relations", cmd_relations, "list relations in both directions").add_argument("--entity", help="only relations from this entity")
    add("lint", cmd_lint, "conflicts, dropped relations, broken references and orphans").add_argument("--previous", help="an earlier export, to find orphans")
    add("owner", cmd_owner, "who owns this entity, its members, escalation and on-call", entity=True)
    add("blast-radius", cmd_blast_radius, "what stops working if this entity goes down", entity=True).add_argument("--depth", type=int, help="stop after this many hops")
    add("gaps", cmd_gaps, "components without a resolvable owner or an on-call annotation").add_argument("--team", help="only this team (group reference)")
    card = add("scorecard", cmd_scorecard, "run scorecard rules from a JSON file")
    card.add_argument("--rules", required=True, help="scorecard definition (JSON)")
    card.add_argument("--min-level", help="exit 1 if any entity is below this level")
    add("k8s-owners", cmd_k8s_owners, "map Kubernetes workloads to catalog owners by backstage.io/kubernetes-id").add_argument("--objects", required=True, help="kubectl get -o json output")
    add("export", cmd_export, "write the catalog with relations as JSON").add_argument("-o", "--output", help="file to write instead of stdout")
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exit_request:
        return int(exit_request.code or 0)
    try:
        for path in args.paths:
            if not os.path.exists(path):
                raise InputError(f"{path} does not exist")
        loaded = load(args.paths)
        if args.command not in ("validate", "lint"):
            for error in loaded.errors:
                print(f"minicatalog: warning: {error['path']}: {error['message']}", file=sys.stderr)
        return args.handler(loaded, args)
    except InputError as error:
        print(f"minicatalog: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
