"""minici: run a GitHub Actions-style workflow on this machine. A YAML subset parser, the ${{ }} expression language, on: filters, needs as a graph run in parallel, strategy.matrix with include and exclude, env precedence, step and job outputs, continue-on-error, timeouts, a fresh working directory per job, a content-addressed cache keyed by hashFiles(), artifacts passed between jobs and secret masking."""
from __future__ import annotations

import argparse
import concurrent.futures
import difflib
import glob
import hashlib
import itertools
import json
import math
import os
import platform
import re
import shlex
import shutil
import signal
import subprocess
import sys
import tarfile
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, IO, List, Optional, Sequence, Set, Tuple

MAX_MATRIX_JOBS = 256
DEFAULT_TIMEOUT_MINUTES = 360
CLEANUP_GRACE_SECONDS = 300
OUTPUT_DRAIN_SECONDS = 1.0
NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_-]*$")
INT_RE = re.compile(r"^[-+]?[0-9]+$")
OCT_RE = re.compile(r"^0o[0-7]+$")
HEX_RE = re.compile(r"^0x[0-9a-fA-F]+$")
FLOAT_RE = re.compile(r"^[-+]?(?:\.[0-9]+|[0-9]+(?:\.[0-9]*)?)(?:[eE][-+]?[0-9]+)?$")
BLOCK_RE = re.compile(r"^([|>])([-+]?)([0-9]?)$")
TOKEN_RE = re.compile(
    r"(?P<number>0x[0-9a-fA-F]+|(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][-+]?[0-9]+)?)"
    r"|(?P<string>'(?:[^']|'')*')"
    r"|(?P<op>==|!=|<=|>=|&&|\|\||[<>!()\[\].,*])"
    r"|(?P<name>[A-Za-z_][A-Za-z0-9_-]*)"
)
COMMAND_RE = re.compile(r"^::([A-Za-z-]+)(?: ([^:]*))?::(.*)$")
ESCAPES = {"n": "\n", "t": "\t", "r": "\r", '"': '"', "\\": "\\", "/": "/", "0": "\0", " ": " "}
LITERALS = {"true": True, "false": False, "null": None}
WORKFLOW_KEYS = ("name", "run-name", "on", "env", "defaults", "jobs", "permissions", "concurrency")
JOB_KEYS = (
    "name", "needs", "if", "runs-on", "env", "outputs", "steps", "strategy", "timeout-minutes", "continue-on-error",
    "defaults", "permissions", "concurrency", "environment", "services", "container", "uses", "with", "secrets",
)
NOT_ENFORCED_JOB_KEYS = ("permissions", "concurrency", "environment", "services", "container")
STEP_KEYS = ("id", "name", "if", "run", "uses", "with", "env", "shell", "working-directory", "continue-on-error", "timeout-minutes")
STRATEGY_KEYS = ("matrix", "fail-fast", "max-parallel")
FILTER_PAIRS = (("branches", "branches-ignore"), ("tags", "tags-ignore"), ("paths", "paths-ignore"))
STATUS_FUNCTIONS = ("success", "failure", "always", "cancelled")
SUCCESS_CALL = ("call", "success", [])
SHELLS = {
    "": ["bash", "-e", "{0}"],
    "bash": ["bash", "--noprofile", "--norc", "-eo", "pipefail", "{0}"],
    "sh": ["sh", "-e", "{0}"],
    "python": ["python", "{0}"],
}
BUILTIN_ACTIONS = ("actions/checkout", "actions/cache", "actions/upload-artifact", "actions/download-artifact")
ARTIFACT_NAME_FORBIDDEN = set('":<>|*?\r\n\\/')
FINAL_RESULTS = ("success", "failure", "cancelled", "skipped")


class YamlError(ValueError):
    def __init__(self, line: int, message: str) -> None:
        super().__init__(f"line {line}: {message}")
        self.line = line


class ExpressionError(ValueError):
    pass


class WorkflowError(ValueError):
    def __init__(self, problems: Sequence[str]) -> None:
        super().__init__("; ".join(problems))
        self.problems = list(problems)


class UsageError(ValueError):
    pass


class StepFailure(Exception):
    def __init__(self, message: str, outcome: str = "failure", outputs: Optional[Dict[str, str]] = None) -> None:
        super().__init__(message)
        self.outcome = outcome
        self.outputs = outputs or {}


@dataclass
class Line:
    number: int
    indent: int
    text: str


def strip_comment(text: str) -> str:
    quote = ""
    escaped = False
    for index, char in enumerate(text):
        if quote:
            if escaped:
                escaped = False
            elif quote == '"' and char == "\\":
                escaped = True
            elif char == quote:
                quote = ""
        elif char in "\"'" and (index == 0 or text[index - 1] in " \t[{,:-"):
            quote = char
        elif char == "#" and (index == 0 or text[index - 1] in " \t"):
            return text[:index].rstrip()
    return text.rstrip()


def parse_double_quoted(text: str, number: int) -> Tuple[str, str]:
    out: List[str] = []
    index = 1
    while index < len(text):
        char = text[index]
        if char == "\\":
            if index + 1 >= len(text):
                raise YamlError(number, "unfinished escape sequence in a double-quoted string")
            out.append(ESCAPES.get(text[index + 1], text[index + 1]))
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


def plain_scalar(text: str) -> Any:
    if text in ("", "~", "null", "Null", "NULL"):
        return None
    if text in ("true", "True", "TRUE"):
        return True
    if text in ("false", "False", "FALSE"):
        return False
    if INT_RE.match(text):
        return int(text)
    if OCT_RE.match(text):
        return int(text[2:], 8)
    if HEX_RE.match(text):
        return int(text[2:], 16)
    if FLOAT_RE.match(text):
        return float(text)
    return text


def parse_flow(text: str, number: int) -> Tuple[Any, str]:
    text = text.lstrip()
    closing = "]" if text.startswith("[") else "}"
    collection: Any = [] if closing == "]" else {}
    rest = text[1:].lstrip()
    while True:
        if rest.startswith(closing):
            return collection, rest[1:].lstrip()
        if not rest:
            raise YamlError(number, f"unterminated flow collection; missing {closing} (flow collections must close on the same line)")
        if closing == "]":
            value, rest = parse_flow_value(rest, number, ",]")
            collection.append(value)
        else:
            key, rest = parse_flow_value(rest, number, ":,}")
            rest = rest.lstrip()
            value = None
            if rest.startswith(":"):
                value, rest = parse_flow_value(rest[1:].lstrip(), number, ",}")
            collection[str(key)] = value
        rest = rest.lstrip()
        if rest.startswith(","):
            rest = rest[1:].lstrip()
        elif not rest:
            raise YamlError(number, f"unterminated flow collection; missing {closing} (flow collections must close on the same line)")
        elif not rest.startswith(closing):
            raise YamlError(number, f"expected , or {closing} in a flow collection")


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


def block_indicator(text: str) -> Optional[Tuple[str, str, str]]:
    match = BLOCK_RE.match(text.strip())
    return match.groups() if match else None


def parse_scalar(text: str, number: int) -> Any:
    text = text.strip()
    if text[:1] in ("&", "*"):
        raise YamlError(number, "anchors and aliases (&name, *name) are not in this subset; repeat the value, or quote it if it is a glob")
    if text[:1] == "!":
        raise YamlError(number, "a value starting with ! is a YAML tag; write the condition as ${{ !... }} or quote it")
    if text[:1] in ("@", "`"):
        raise YamlError(number, f"{text[0]} is reserved in YAML; quote the value")
    if block_indicator(text) is not None:
        raise YamlError(number, "a block scalar (| or >) must follow 'key:' or '- '")
    if text.startswith('"'):
        value, rest = parse_double_quoted(text, number)
    elif text.startswith("'"):
        value, rest = parse_single_quoted(text, number)
    elif text[:1] in "[{":
        value, rest = parse_flow(text, number)
    else:
        return plain_scalar(text)
    if rest.strip():
        raise YamlError(number, f"unexpected text after a quoted value: {rest.strip()!r}")
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
            return key, text[index + 1 :].strip()
    return None


def is_item(text: str) -> bool:
    return text == "-" or text.startswith("- ")


def fold_lines(lines: List[str]) -> str:
    text = ""
    for index, line in enumerate(lines):
        if index == 0:
            text = line
        elif line == "":
            text += "\n"
        elif lines[index - 1] == "":
            text += line
        elif line.startswith(" ") or lines[index - 1].startswith(" "):
            text += "\n" + line
        else:
            text += " " + line
    return text


class YamlParser:
    def __init__(self, source: str) -> None:
        self.raw = source.splitlines()
        self.position = 0
        self.overrides: Dict[int, Line] = {}
        self.started = False

    def peek(self) -> Optional[Line]:
        index = self.position
        while index < len(self.raw):
            if index in self.overrides:
                self.position = index
                return self.overrides[index]
            raw = self.raw[index]
            text = strip_comment(raw)
            body = text.strip()
            if body:
                if "\t" in raw[: len(raw) - len(raw.lstrip())]:
                    raise YamlError(index + 1, "tabs are not allowed for indentation; use spaces")
                if body in ("---", "...") and not text.startswith(" "):
                    if self.started and body == "---":
                        raise YamlError(index + 1, "only one YAML document per file")
                    index += 1
                    continue
                self.position = index
                self.started = True
                return Line(index + 1, len(text) - len(text.lstrip(" ")), body)
            index += 1
        self.position = index
        return None

    def advance(self) -> None:
        self.overrides.pop(self.position, None)
        self.position += 1

    def parse_document(self) -> Any:
        line = self.peek()
        if line is None:
            return None
        value = self.parse_block(line.indent)
        extra = self.peek()
        if extra is not None:
            raise YamlError(extra.number, f"unexpected indentation ({extra.indent} spaces)")
        return value

    def parse_block(self, indent: int) -> Any:
        line = self.peek()
        if line is None:
            return None
        if is_item(line.text):
            return self.parse_sequence(line.indent)
        if split_key(line.text, line.number) is None:
            self.advance()
            return parse_scalar(line.text, line.number)
        return self.parse_mapping(indent)

    def parse_sequence(self, indent: int) -> List[Any]:
        items: List[Any] = []
        while True:
            line = self.peek()
            if line is None or line.indent < indent or (line.indent == indent and not is_item(line.text)):
                return items
            if line.indent > indent:
                raise YamlError(line.number, f"bad indentation in a sequence (expected {indent} spaces, got {line.indent})")
            rest = line.text[1:].strip()
            if not rest:
                self.advance()
                nested = self.peek()
                items.append(self.parse_block(nested.indent) if nested is not None and nested.indent > indent else None)
                continue
            indicator = block_indicator(rest)
            if indicator is not None:
                self.advance()
                items.append(self.read_block_scalar(indent, indicator, line.number))
                continue
            column = indent + (len(line.text) - len(line.text[1:].lstrip()))
            self.overrides[self.position] = Line(line.number, column, rest)
            items.append(self.parse_block(column))

    def parse_mapping(self, indent: int) -> Dict[str, Any]:
        mapping: Dict[str, Any] = {}
        while True:
            line = self.peek()
            if line is None or line.indent < indent:
                return mapping
            if line.indent > indent:
                raise YamlError(line.number, f"bad indentation in a mapping (expected {indent} spaces, got {line.indent}); a value continued on the next line needs | or >")
            parts = split_key(line.text, line.number)
            if parts is None:
                raise YamlError(line.number, f"expected 'key: value', got {line.text!r}")
            key, rest = parts
            if key in mapping:
                raise YamlError(line.number, f"duplicate key {key!r}")
            self.advance()
            indicator = block_indicator(rest)
            if indicator is not None:
                mapping[key] = self.read_block_scalar(indent, indicator, line.number)
                continue
            if rest:
                mapping[key] = parse_scalar(rest, line.number)
                continue
            nested = self.peek()
            if nested is None or nested.indent < indent:
                mapping[key] = None
            elif nested.indent == indent:
                mapping[key] = self.parse_sequence(indent) if is_item(nested.text) else None
            else:
                mapping[key] = self.parse_block(nested.indent)

    def read_block_scalar(self, parent: int, indicator: Tuple[str, str, str], number: int) -> str:
        style, chomp, explicit = indicator
        if explicit:
            raise YamlError(number, "explicit indentation indicators (|2) are not in this subset")
        lines: List[str] = []
        content_indent: Optional[int] = None
        index = self.position
        while index < len(self.raw):
            raw = self.raw[index]
            if not raw.strip():
                lines.append("")
                index += 1
                continue
            indent = len(raw) - len(raw.lstrip(" "))
            if indent <= parent:
                break
            if content_indent is None:
                content_indent = indent
            elif indent < content_indent:
                raise YamlError(index + 1, f"bad indentation in a block scalar (expected at least {content_indent} spaces, got {indent})")
            lines.append(raw[content_indent:])
            index += 1
        self.position = index
        trailing = 0
        while trailing < len(lines) and lines[len(lines) - 1 - trailing] == "":
            trailing += 1
        core = lines[: len(lines) - trailing]
        if not core:
            return "\n" * trailing if chomp == "+" else ""
        text = "\n".join(core) if style == "|" else fold_lines(core)
        if chomp == "-":
            return text
        if chomp == "+":
            return text + "\n" * (trailing + 1)
        return text + "\n"


def parse_yaml(source: str) -> Any:
    return YamlParser(source).parse_document()


class Filtered(list):
    pass


@dataclass
class Scope:
    contexts: Dict[str, Any]
    status: Dict[str, Callable[[], bool]] = field(default_factory=dict)
    hash_files: Optional[Callable[[List[str]], str]] = None


def tokenize(source: str) -> List[Tuple[str, Any]]:
    tokens: List[Tuple[str, Any]] = []
    position = 0
    while True:
        while position < len(source) and source[position].isspace():
            position += 1
        if position >= len(source):
            break
        match = TOKEN_RE.match(source, position)
        if not match:
            hint = "; strings use single quotes" if source[position] == '"' else ""
            raise ExpressionError(f"unexpected character {source[position]!r} at position {position + 1} in {source!r}{hint}")
        kind = match.lastgroup or ""
        text = match.group(kind)
        if kind == "number":
            value: Any = float(int(text, 16)) if text.lower().startswith("0x") else float(text)
        elif kind == "string":
            value = text[1:-1].replace("''", "'")
        else:
            value = text
        tokens.append((kind, value))
        position = match.end()
    tokens.append(("end", None))
    return tokens


class ExpressionParser:
    def __init__(self, source: str) -> None:
        self.source = source
        self.tokens = tokenize(source)
        self.position = 0

    def peek(self) -> Tuple[str, Any]:
        return self.tokens[self.position]

    def take(self) -> Tuple[str, Any]:
        token = self.tokens[self.position]
        self.position += 1
        return token

    def expect(self, op: str) -> None:
        kind, value = self.take()
        if kind != "op" or value != op:
            raise ExpressionError(f"expected {op!r} in {self.source!r}")

    def parse(self) -> Any:
        if self.peek()[0] == "end":
            raise ExpressionError("empty expression")
        node = self.parse_or()
        if self.peek()[0] != "end":
            raise ExpressionError(f"unexpected {self.peek()[1]!r} in {self.source!r}")
        return node

    def parse_or(self) -> Any:
        node = self.parse_and()
        while self.peek() == ("op", "||"):
            self.take()
            node = ("or", node, self.parse_and())
        return node

    def parse_and(self) -> Any:
        node = self.parse_equality()
        while self.peek() == ("op", "&&"):
            self.take()
            node = ("and", node, self.parse_equality())
        return node

    def parse_equality(self) -> Any:
        node = self.parse_comparison()
        while self.peek()[0] == "op" and self.peek()[1] in ("==", "!="):
            op = self.take()[1]
            node = ("compare", op, node, self.parse_comparison())
        return node

    def parse_comparison(self) -> Any:
        node = self.parse_unary()
        while self.peek()[0] == "op" and self.peek()[1] in ("<", "<=", ">", ">="):
            op = self.take()[1]
            node = ("compare", op, node, self.parse_unary())
        return node

    def parse_unary(self) -> Any:
        if self.peek() == ("op", "!"):
            self.take()
            return ("not", self.parse_unary())
        return self.parse_postfix()

    def parse_postfix(self) -> Any:
        node = self.parse_primary()
        while True:
            token = self.peek()
            if token == ("op", "."):
                self.take()
                kind, value = self.take()
                if (kind, value) == ("op", "*"):
                    node = ("star", node)
                elif kind == "name":
                    node = ("property", node, value)
                else:
                    raise ExpressionError(f"expected a property name after '.' in {self.source!r}")
            elif token == ("op", "["):
                self.take()
                if self.peek() == ("op", "*"):
                    self.take()
                    self.expect("]")
                    node = ("star", node)
                else:
                    index = self.parse_or()
                    self.expect("]")
                    node = ("index", node, index)
            else:
                return node

    def parse_primary(self) -> Any:
        kind, value = self.take()
        if kind in ("number", "string"):
            return ("literal", value)
        if kind == "name":
            if value in LITERALS:
                return ("literal", LITERALS[value])
            if self.peek() == ("op", "("):
                self.take()
                args: List[Any] = []
                if self.peek() != ("op", ")"):
                    args.append(self.parse_or())
                    while self.peek() == ("op", ","):
                        self.take()
                        args.append(self.parse_or())
                self.expect(")")
                return ("call", value.lower(), args)
            return ("context", value.lower())
        if (kind, value) == ("op", "("):
            node = self.parse_or()
            self.expect(")")
            return node
        what = "the end of the expression" if kind == "end" else repr(value)
        raise ExpressionError(f"unexpected {what} in {self.source!r}")


def kind_of(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, (int, float)):
        return "number"
    if isinstance(value, str):
        return "string"
    return "object"


def to_number(value: Any) -> float:
    kind = kind_of(value)
    if kind == "null":
        return 0.0
    if kind == "boolean":
        return 1.0 if value else 0.0
    if kind == "number":
        return float(value)
    if kind == "string":
        text = value.strip()
        if text == "":
            return 0.0
        if HEX_RE.match(text):
            return float(int(text[2:], 16))
        return float(text) if FLOAT_RE.match(text) else math.nan
    return math.nan


def format_number(value: float) -> str:
    if math.isnan(value):
        return "NaN"
    if math.isinf(value):
        return "Infinity" if value > 0 else "-Infinity"
    if float(value).is_integer() and abs(value) < 1e15:
        return str(int(value))
    return repr(float(value))


def to_text(value: Any) -> str:
    kind = kind_of(value)
    if kind == "null":
        return ""
    if kind == "boolean":
        return "true" if value else "false"
    if kind == "number":
        return format_number(value)
    if kind == "string":
        return value
    return json.dumps(value, indent=2)


def truthy(value: Any) -> bool:
    kind = kind_of(value)
    if kind == "null":
        return False
    if kind == "boolean":
        return bool(value)
    if kind == "number":
        return not (value == 0 or math.isnan(value))
    if kind == "string":
        return value != ""
    return True


def loose_equal(left: Any, right: Any) -> bool:
    left_kind, right_kind = kind_of(left), kind_of(right)
    if left_kind != right_kind:
        return to_number(left) == to_number(right)
    if left_kind == "string":
        return left.lower() == right.lower()
    if left_kind == "object":
        return left is right
    if left_kind == "number":
        return float(left) == float(right)
    return left == right


def compare(op: str, left: Any, right: Any) -> bool:
    if op == "==":
        return loose_equal(left, right)
    if op == "!=":
        return not loose_equal(left, right)
    if kind_of(left) == "string" and kind_of(right) == "string":
        a, b = left.lower(), right.lower()
    else:
        a, b = to_number(left), to_number(right)
        if math.isnan(a) or math.isnan(b):
            return False
    return {"<": a < b, "<=": a <= b, ">": a > b, ">=": a >= b}[op]


def get_member(target: Any, name: str) -> Any:
    if isinstance(target, Filtered):
        out = Filtered()
        for item in target:
            value = get_member(item, name)
            if value is not None:
                out.append(value)
        return out
    if isinstance(target, dict):
        if name in target:
            return target[name]
        lowered = name.lower()
        for key, value in target.items():
            if str(key).lower() == lowered:
                return value
    return None


def get_index(target: Any, key: Any) -> Any:
    if isinstance(target, list) and kind_of(key) == "number" and not isinstance(target, Filtered):
        position = to_number(key)
        if position.is_integer() and 0 <= position < len(target):
            return target[int(position)]
        return None
    if isinstance(target, Filtered) and kind_of(key) == "number":
        position = int(to_number(key))
        return target[position] if 0 <= position < len(target) else None
    return get_member(target, to_text(key))


def star(target: Any) -> Filtered:
    if isinstance(target, dict):
        return Filtered(target.values())
    if isinstance(target, list):
        return Filtered(target)
    return Filtered()


def fn_contains(search: Any, item: Any) -> bool:
    if isinstance(search, list):
        return any(loose_equal(element, item) for element in search)
    return to_text(item).lower() in to_text(search).lower()


def fn_starts_with(text: Any, prefix: Any) -> bool:
    return to_text(text).lower().startswith(to_text(prefix).lower())


def fn_ends_with(text: Any, suffix: Any) -> bool:
    return to_text(text).lower().endswith(to_text(suffix).lower())


def fn_format(template: Any, *args: Any) -> str:
    text = to_text(template)
    out: List[str] = []
    index = 0
    while index < len(text):
        char = text[index]
        if text.startswith("{{", index) or text.startswith("}}", index):
            out.append(char)
            index += 2
            continue
        if char == "{":
            end = text.find("}", index)
            number = text[index + 1 : end] if end > 0 else ""
            if not number.isdigit() or int(number) >= len(args):
                raise ExpressionError(f"format(): bad placeholder in {text!r}; use {{0}}, {{1}}... for each argument and {{{{ }}}} for braces")
            out.append(to_text(args[int(number)]))
            index = end + 1
            continue
        if char == "}":
            raise ExpressionError(f"format(): unbalanced }} in {text!r}")
        out.append(char)
        index += 1
    return "".join(out)


def fn_join(array: Any, separator: Any = ",") -> str:
    if isinstance(array, list):
        return to_text(separator).join(to_text(item) for item in array)
    return to_text(array)


def fn_to_json(value: Any) -> str:
    return json.dumps(value, indent=2)


def fn_from_json(text: Any) -> Any:
    try:
        return json.loads(to_text(text))
    except json.JSONDecodeError as exc:
        raise ExpressionError(f"fromJSON(): {exc.msg} in {to_text(text)[:60]!r}")


FUNCTIONS: Dict[str, Tuple[int, int, Callable[..., Any]]] = {
    "contains": (2, 2, fn_contains),
    "startswith": (2, 2, fn_starts_with),
    "endswith": (2, 2, fn_ends_with),
    "format": (1, 99, fn_format),
    "join": (1, 2, fn_join),
    "tojson": (1, 1, fn_to_json),
    "fromjson": (1, 1, fn_from_json),
}


def call_function(name: str, args: List[Any], scope: Scope) -> Any:
    if name in STATUS_FUNCTIONS:
        if args:
            raise ExpressionError(f"{name}() takes no arguments")
        if name not in scope.status:
            raise ExpressionError(f"{name}() is only available in if: conditions")
        return scope.status[name]()
    if name == "hashfiles":
        if scope.hash_files is None:
            raise ExpressionError("hashFiles() is only available in steps, where a workspace exists")
        return scope.hash_files([to_text(arg) for arg in args])
    if name not in FUNCTIONS:
        raise ExpressionError(f"Unrecognized function: {name!r}")
    low, high, function = FUNCTIONS[name]
    if not low <= len(args) <= high:
        raise ExpressionError(f"{name}() takes {low} to {high} arguments, got {len(args)}")
    return function(*args)


def evaluate_node(node: Any, scope: Scope) -> Any:
    kind = node[0]
    if kind == "literal":
        return node[1]
    if kind == "context":
        if node[1] not in scope.contexts:
            raise ExpressionError(f"Unrecognized named-value: {node[1]!r}; available here: {', '.join(sorted(scope.contexts))}")
        return scope.contexts[node[1]]
    if kind == "property":
        return get_member(evaluate_node(node[1], scope), node[2])
    if kind == "index":
        return get_index(evaluate_node(node[1], scope), evaluate_node(node[2], scope))
    if kind == "star":
        return star(evaluate_node(node[1], scope))
    if kind == "not":
        return not truthy(evaluate_node(node[1], scope))
    if kind == "and":
        left = evaluate_node(node[1], scope)
        return evaluate_node(node[2], scope) if truthy(left) else left
    if kind == "or":
        left = evaluate_node(node[1], scope)
        return left if truthy(left) else evaluate_node(node[2], scope)
    if kind == "compare":
        return compare(node[1], evaluate_node(node[2], scope), evaluate_node(node[3], scope))
    if kind == "call":
        return call_function(node[1], [evaluate_node(arg, scope) for arg in node[2]], scope)
    if kind == "interpolated":
        return interpolate(node[1], scope)
    raise ExpressionError(f"unknown node {kind}")


def evaluate(source: str, scope: Scope) -> Any:
    return evaluate_node(ExpressionParser(source).parse(), scope)


def find_expressions(text: str) -> List[Tuple[int, int, str]]:
    spans: List[Tuple[int, int, str]] = []
    index = 0
    while True:
        start = text.find("${{", index)
        if start < 0:
            return spans
        position = start + 3
        in_string = False
        while position < len(text):
            char = text[position]
            if in_string:
                if char == "'":
                    if text[position + 1 : position + 2] == "'":
                        position += 2
                        continue
                    in_string = False
            elif char == "'":
                in_string = True
            elif text.startswith("}}", position):
                break
            position += 1
        else:
            raise ExpressionError(f"unclosed ${{{{ in {text.strip()[:80]!r}")
        spans.append((start, position + 2, text[start + 3 : position].strip()))
        index = position + 2


def interpolate(value: Any, scope: Scope) -> Any:
    if isinstance(value, str):
        spans = find_expressions(value)
        if not spans:
            return value
        if len(spans) == 1 and not value[: spans[0][0]].strip() and not value[spans[0][1] :].strip():
            return evaluate(spans[0][2], scope)
        parts: List[str] = []
        last = 0
        for start, end, source in spans:
            parts.append(value[last:start])
            parts.append(to_text(evaluate(source, scope)))
            last = end
        parts.append(value[last:])
        return "".join(parts)
    if isinstance(value, list):
        return [interpolate(item, scope) for item in value]
    if isinstance(value, dict):
        return {key: interpolate(item, scope) for key, item in value.items()}
    return value


def mentions_status(node: Any) -> bool:
    if not isinstance(node, tuple):
        return False
    if node[0] == "call" and node[1] in STATUS_FUNCTIONS:
        return True
    return any(mentions_status(part) for part in node[1:] if isinstance(part, tuple)) or any(
        mentions_status(arg) for part in node[1:] if isinstance(part, list) for arg in part
    )


def condition_ast(raw: Any) -> Tuple[Any, Optional[str]]:
    if raw is None or raw == "":
        return SUCCESS_CALL, None
    if isinstance(raw, (bool, int, float)):
        return ("and", SUCCESS_CALL, ("literal", raw)), None
    text = str(raw).strip()
    spans = find_expressions(text)
    if len(spans) == 1 and spans[0][0] == 0 and spans[0][1] == len(text):
        source = spans[0][2]
    elif spans:
        warning = f"if: {text!r} has text outside ${{{{ }}}}, so it becomes a non-empty string and is always true; put the whole condition inside one ${{{{ }}}} or drop the braces"
        return ("and", SUCCESS_CALL, ("interpolated", text)), warning
    else:
        source = text
    node = ExpressionParser(source).parse()
    if not mentions_status(node):
        node = ("and", SUCCESS_CALL, node)
    return node, None


def contains_expression(value: Any) -> bool:
    if isinstance(value, str):
        return "${{" in value
    if isinstance(value, list):
        return any(contains_expression(item) for item in value)
    if isinstance(value, dict):
        return any(contains_expression(item) for item in value.values())
    return False


def expression_problems(value: Any, where: str) -> List[str]:
    problems: List[str] = []
    if isinstance(value, str):
        try:
            for _, _, source in find_expressions(value):
                ExpressionParser(source).parse()
        except ExpressionError as exc:
            problems.append(f"{where}: {exc}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            problems.extend(expression_problems(item, f"{where}[{index}]"))
    elif isinstance(value, dict):
        for key, item in value.items():
            problems.extend(expression_problems(item, f"{where}.{key}"))
    return problems


@dataclass
class Step:
    index: int
    id: Optional[str]
    name: Optional[str]
    run: Optional[str]
    uses: Optional[str]
    inputs: Dict[str, Any]
    env: Dict[str, Any]
    condition: Any
    shell: Optional[str]
    working_directory: Optional[str]
    continue_on_error: Any
    timeout_minutes: Any

    def display(self) -> str:
        if self.name:
            return str(self.name)
        if self.run is not None:
            first = next((line for line in self.run.splitlines() if line.strip()), "")
            return f"Run {first.strip()}"
        return f"Run {self.uses}"


@dataclass
class Job:
    id: str
    name: Optional[str]
    needs: List[str]
    condition: Any
    runs_on: Any
    env: Dict[str, Any]
    outputs: Dict[str, Any]
    steps: List[Step]
    strategy: Dict[str, Any]
    timeout_minutes: Any
    continue_on_error: Any
    defaults: Dict[str, Any]


@dataclass
class Workflow:
    name: str
    path: str
    on: Dict[str, Dict[str, Any]]
    env: Dict[str, Any]
    defaults: Dict[str, Any]
    jobs: Dict[str, Job]
    notes: List[str] = field(default_factory=list)


def unknown_key(where: str, key: Any, allowed: Sequence[str]) -> str:
    hint = difflib.get_close_matches(str(key), list(allowed), n=1)
    suggestion = f"; did you mean {hint[0]!r}?" if hint else f"; allowed: {', '.join(allowed)}"
    return f"{where}: unexpected key {key!r}{suggestion}"


def as_mapping(value: Any, where: str, problems: List[str]) -> Dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        problems.append(f"{where} must be a mapping")
        return {}
    return {str(key): item for key, item in value.items()}


def normalise_on(raw: Any, problems: List[str]) -> Dict[str, Dict[str, Any]]:
    if raw is None:
        return {}
    if isinstance(raw, str):
        return {raw: {}}
    if isinstance(raw, list):
        return {str(event): {} for event in raw}
    if not isinstance(raw, dict):
        problems.append("on: must be an event name, a list of events or a mapping of events to filters")
        return {}
    events: Dict[str, Dict[str, Any]] = {}
    for event, config in raw.items():
        config = as_mapping(config, f"on.{event}", problems)
        for positive, negative in FILTER_PAIRS:
            if positive in config and negative in config:
                problems.append(f"on.{event}: use {positive} or {negative}, not both; put !patterns in {positive} to exclude")
        for positive, negative in FILTER_PAIRS:
            for key in (positive, negative):
                if isinstance(config.get(key), str):
                    config[key] = [config[key]]
        events[str(event)] = config
    return events


def build_step(where: str, index: int, raw: Any, problems: List[str]) -> Optional[Step]:
    if not isinstance(raw, dict):
        problems.append(f"{where} must be a mapping with run: or uses:")
        return None
    for key in raw:
        if key not in STEP_KEYS:
            problems.append(unknown_key(where, key, STEP_KEYS))
    if ("run" in raw) == ("uses" in raw):
        problems.append(f"{where}: a step needs exactly one of run: or uses:")
        return None
    step_id = raw.get("id")
    if step_id is not None and not (isinstance(step_id, str) and NAME_RE.match(step_id)):
        problems.append(f"{where}.id: {step_id!r} must start with a letter or _ and contain only letters, digits, - and _")
    if "run" in raw and "with" in raw:
        problems.append(f"{where}: with: belongs to uses: steps; pass values to run: through env:")
    run = raw.get("run")
    if "run" in raw and isinstance(run, (dict, list)):
        problems.append(f"{where}.run must be a string")
        run = ""
    return Step(
        index=index,
        id=step_id if isinstance(step_id, str) else None,
        name=to_text(raw.get("name")) if raw.get("name") is not None else None,
        run=to_text(run) if "run" in raw else None,
        uses=to_text(raw.get("uses")) if "uses" in raw else None,
        inputs=as_mapping(raw.get("with"), f"{where}.with", problems),
        env=as_mapping(raw.get("env"), f"{where}.env", problems),
        condition=raw.get("if"),
        shell=to_text(raw.get("shell")) if raw.get("shell") is not None else None,
        working_directory=to_text(raw.get("working-directory")) if raw.get("working-directory") is not None else None,
        continue_on_error=raw.get("continue-on-error"),
        timeout_minutes=raw.get("timeout-minutes"),
    )


def build_job(job_id: str, raw: Any, problems: List[str], notes: List[str]) -> Optional[Job]:
    where = f"jobs.{job_id}"
    if not NAME_RE.match(job_id):
        problems.append(f"{where}: job ids start with a letter or _ and contain only letters, digits, - and _")
    if not isinstance(raw, dict):
        problems.append(f"{where} must be a mapping")
        return None
    for key in raw:
        if key not in JOB_KEYS:
            problems.append(unknown_key(where, key, JOB_KEYS))
    if "uses" in raw:
        problems.append(f"{where}: reusable workflows (jobs.<id>.uses) are not in minici")
        return None
    for key in NOT_ENFORCED_JOB_KEYS:
        if key in raw:
            notes.append(f"{where}.{key} is accepted but not enforced by minici")
    if "runs-on" not in raw:
        problems.append(f"{where}: runs-on is required")
    needs = raw.get("needs") or []
    if isinstance(needs, str):
        needs = [needs]
    if not isinstance(needs, list) or not all(isinstance(need, str) for need in needs):
        problems.append(f"{where}.needs must be a job id or a list of job ids")
        needs = []
    steps_raw = raw.get("steps")
    if not isinstance(steps_raw, list) or not steps_raw:
        problems.append(f"{where}: steps must be a non-empty list")
        steps_raw = []
    steps: List[Step] = []
    seen: Set[str] = set()
    for index, step_raw in enumerate(steps_raw):
        step = build_step(f"{where}.steps[{index}]", index, step_raw, problems)
        if step is None:
            continue
        if step.id:
            if step.id in seen:
                problems.append(f"{where}.steps[{index}].id: {step.id!r} is already used in this job")
            seen.add(step.id)
        steps.append(step)
    strategy = as_mapping(raw.get("strategy"), f"{where}.strategy", problems)
    for key in strategy:
        if key not in STRATEGY_KEYS:
            problems.append(unknown_key(f"{where}.strategy", key, STRATEGY_KEYS))
    return Job(
        id=job_id,
        name=to_text(raw.get("name")) if raw.get("name") is not None else None,
        needs=list(needs),
        condition=raw.get("if"),
        runs_on=raw.get("runs-on"),
        env=as_mapping(raw.get("env"), f"{where}.env", problems),
        outputs=as_mapping(raw.get("outputs"), f"{where}.outputs", problems),
        steps=steps,
        strategy=strategy,
        timeout_minutes=raw.get("timeout-minutes"),
        continue_on_error=raw.get("continue-on-error"),
        defaults=as_mapping(raw.get("defaults"), f"{where}.defaults", problems),
    )


def find_cycle(jobs: Dict[str, Job]) -> Optional[List[str]]:
    state: Dict[str, int] = {}
    stack: List[str] = []

    def visit(name: str) -> Optional[List[str]]:
        state[name] = 1
        stack.append(name)
        for need in jobs[name].needs:
            if need not in jobs or need == name:
                continue
            if state.get(need) == 1:
                return stack[stack.index(need) :] + [need]
            if need not in state:
                found = visit(need)
                if found:
                    return found
        stack.pop()
        state[name] = 2
        return None

    for name in jobs:
        if name not in state:
            found = visit(name)
            if found:
                return found
    return None


def check_needs(jobs: Dict[str, Job], problems: List[str]) -> None:
    for job in jobs.values():
        for need in job.needs:
            if need == job.id:
                problems.append(f"jobs.{job.id}.needs: a job cannot need itself")
            elif need not in jobs:
                hint = difflib.get_close_matches(need, list(jobs), n=1)
                problems.append(f"jobs.{job.id}.needs: job {need!r} does not exist" + (f"; did you mean {hint[0]!r}?" if hint else ""))
    cycle = find_cycle(jobs)
    if cycle:
        problems.append("needs cycle: " + " -> ".join(cycle) + "; no order exists, so no job in it can start")


def build_workflow(data: Any, path: str = "workflow.yml") -> Workflow:
    if not isinstance(data, dict):
        raise WorkflowError([f"{path}: a workflow is a mapping with on: and jobs:"])
    problems: List[str] = []
    notes: List[str] = []
    for key in data:
        if key not in WORKFLOW_KEYS:
            problems.append(unknown_key("workflow", key, WORKFLOW_KEYS))
    if "on" not in data:
        problems.append("on: is required; it names the events that start this workflow")
    on = normalise_on(data.get("on"), problems)
    jobs_raw = data.get("jobs")
    if not isinstance(jobs_raw, dict) or not jobs_raw:
        problems.append("jobs: is required and maps job ids to jobs")
        jobs_raw = {}
    jobs: Dict[str, Job] = {}
    for job_id, raw in jobs_raw.items():
        job = build_job(str(job_id), raw, problems, notes)
        if job is not None:
            jobs[job.id] = job
    check_needs(jobs, problems)
    for job in jobs.values():
        for where, condition in [(f"jobs.{job.id}.if", job.condition)] + [(f"jobs.{job.id}.steps[{step.index}].if", step.condition) for step in job.steps]:
            try:
                condition_ast(condition)
            except ExpressionError as exc:
                problems.append(f"{where}: {exc}")
    for job_id, raw in jobs_raw.items():
        problems.extend(expression_problems(raw, f"jobs.{job_id}"))
    problems.extend(expression_problems(data.get("env"), "env"))
    if "permissions" in data or "concurrency" in data:
        notes.append("workflow-level permissions and concurrency are accepted but not enforced by minici")
    if problems:
        raise WorkflowError(problems)
    return Workflow(
        name=to_text(data.get("name")) if data.get("name") is not None else os.path.basename(path),
        path=path,
        on=on,
        env=as_mapping(data.get("env"), "env", problems),
        defaults=as_mapping(data.get("defaults"), "defaults", problems),
        jobs=jobs,
        notes=notes,
    )


def load_workflow(path: str) -> Workflow:
    try:
        with open(path, encoding="utf-8") as handle:
            text = handle.read()
    except OSError as exc:
        raise WorkflowError([f"{path}: {exc.strerror}"])
    try:
        data = parse_yaml(text)
    except YamlError as exc:
        raise WorkflowError([f"{path}: {exc}"])
    return build_workflow(data, path)


def job_waves(jobs: Dict[str, Job]) -> List[List[str]]:
    depth: Dict[str, int] = {}

    def level(name: str) -> int:
        if name not in depth:
            needs = [need for need in jobs[name].needs if need in jobs]
            depth[name] = 1 + max((level(need) for need in needs), default=-1)
        return depth[name]

    for name in jobs:
        level(name)
    count = max(depth.values(), default=-1) + 1
    return [[name for name in jobs if depth[name] == number] for number in range(count)]


def critical_path(jobs: Dict[str, Job]) -> List[str]:
    best: Dict[str, List[str]] = {}

    def chain(name: str) -> List[str]:
        if name not in best:
            options = [chain(need) for need in jobs[name].needs if need in jobs]
            best[name] = (max(options, key=len) if options else []) + [name]
        return best[name]

    return max((chain(name) for name in jobs), key=len, default=[])


def ancestors(jobs: Dict[str, Job], name: str) -> Set[str]:
    seen: Set[str] = set()
    pending = list(jobs[name].needs)
    while pending:
        need = pending.pop()
        if need in seen or need not in jobs:
            continue
        seen.add(need)
        pending.extend(jobs[need].needs)
    return seen


def filter_regex(pattern: str) -> "re.Pattern[str]":
    out: List[str] = []
    index = 0
    while index < len(pattern):
        char = pattern[index]
        if pattern.startswith("**", index):
            out.append(".*")
            index += 2
            continue
        if char == "*":
            out.append("[^/]*")
        elif char in "?+" and out and not out[-1].endswith(("*", "?", "+")):
            out.append(char)
        elif char == "[" and "]" in pattern[index + 1 :]:
            end = pattern.index("]", index + 1)
            out.append("[" + pattern[index + 1 : end].replace("\\", "\\\\") + "]")
            index = end + 1
            continue
        else:
            out.append(re.escape(char))
        index += 1
    return re.compile("^" + "".join(out) + "$")


def filter_matches(value: str, patterns: Sequence[Any]) -> bool:
    matched = False
    for raw in patterns:
        pattern = to_text(raw)
        negate = pattern.startswith("!")
        if filter_regex(pattern[1:] if negate else pattern).match(value):
            matched = not negate
    return matched


@dataclass
class Trigger:
    triggered: bool
    reason: str


def ref_filters(config: Dict[str, Any], kind: str, name: str) -> Tuple[Optional[bool], str]:
    other = "tags" if kind == "branches" else "branches"
    if kind in config:
        ok = filter_matches(name, config[kind])
        return ok, f"{kind} {'matched' if ok else 'did not match'} {name!r}"
    if f"{kind}-ignore" in config:
        ok = not filter_matches(name, config[f"{kind}-ignore"])
        return ok, f"{kind}-ignore {'did not match' if ok else 'matched'} {name!r}"
    if other in config or f"{other}-ignore" in config:
        return False, f"only {other} filters are set, so a {'tag' if kind == 'tags' else 'branch'} push never matches"
    return None, ""


def evaluate_trigger(workflow: Workflow, event: str, ref: str, changed: Optional[Sequence[str]], base_ref: str = "") -> Trigger:
    if event not in workflow.on:
        return Trigger(False, f"on: lists {', '.join(workflow.on) or 'no events'}, not {event}")
    config = workflow.on[event]
    reasons = [f"event {event}"]
    is_tag = ref.startswith("refs/tags/")
    if event == "push":
        name = ref[len("refs/tags/") :] if is_tag else ref[len("refs/heads/") :] if ref.startswith("refs/heads/") else ref
        ok, why = ref_filters(config, "tags" if is_tag else "branches", name)
        if ok is False:
            return Trigger(False, why)
        if why:
            reasons.append(why)
    elif event in ("pull_request", "pull_request_target"):
        base = base_ref[len("refs/heads/") :] if base_ref.startswith("refs/heads/") else base_ref
        ok, why = ref_filters({k: v for k, v in config.items() if k.startswith("branches")}, "branches", base)
        if ok is False:
            return Trigger(False, why + " (pull_request branch filters match the base branch)")
        if why:
            reasons.append(why)
    has_paths = "paths" in config or "paths-ignore" in config
    if has_paths and event == "push" and is_tag:
        reasons.append("path filters are not evaluated for tag pushes")
    elif has_paths and changed is None:
        reasons.append("path filters not evaluated: no list of changed files (pass --changed)")
    elif "paths" in config:
        hits = [path for path in changed or [] if filter_matches(path, config["paths"])]
        if not hits:
            return Trigger(False, f"paths matched none of the {len(changed or [])} changed file(s)")
        reasons.append(f"paths matched {', '.join(hits[:3])}")
    elif "paths-ignore" in config:
        kept = [path for path in changed or [] if not filter_matches(path, config["paths-ignore"])]
        if not kept:
            return Trigger(False, "paths-ignore matched every changed file")
        reasons.append(f"{', '.join(kept[:3])} not ignored")
    return Trigger(True, "; ".join(reasons))


def same_value(left: Any, right: Any) -> bool:
    if isinstance(left, bool) or isinstance(right, bool):
        return isinstance(left, bool) and isinstance(right, bool) and left == right
    if isinstance(left, (int, float)) and isinstance(right, (int, float)):
        return left == right
    return type(left) == type(right) and left == right


def expand_matrix(matrix: Any) -> List[Dict[str, Any]]:
    if matrix is None:
        return [{}]
    if not isinstance(matrix, dict):
        raise WorkflowError(["strategy.matrix must be a mapping of keys to lists (or fromJSON() of one)"])
    include = matrix.get("include") or []
    exclude = matrix.get("exclude") or []
    if not isinstance(include, list) or not isinstance(exclude, list):
        raise WorkflowError(["strategy.matrix include and exclude must be lists of mappings"])
    dimensions = [(key, values) for key, values in matrix.items() if key not in ("include", "exclude")]
    for key, values in dimensions:
        if not isinstance(values, list) or not values:
            raise WorkflowError([f"strategy.matrix.{key} must be a non-empty list"])
    combos: List[Dict[str, Any]] = []
    if dimensions:
        keys = [key for key, _ in dimensions]
        combos = [dict(zip(keys, values)) for values in itertools.product(*[values for _, values in dimensions])]
    combos = [c for c in combos if not any(all(k in c and same_value(c[k], v) for k, v in e.items()) for e in exclude)]
    originals = [dict(combo) for combo in combos]
    extra: List[Dict[str, Any]] = []
    for entry in include:
        if not isinstance(entry, dict):
            raise WorkflowError(["each strategy.matrix.include entry must be a mapping"])
        matched = False
        for combo, original in zip(combos, originals):
            if all(key not in original or same_value(original[key], value) for key, value in entry.items()):
                combo.update(entry)
                matched = True
        if not matched:
            extra.append(dict(entry))
    result = combos + extra
    if not result:
        raise WorkflowError(["strategy.matrix produced no jobs (every combination was excluded)"])
    if len(result) > MAX_MATRIX_JOBS:
        raise WorkflowError([f"strategy.matrix produced {len(result)} jobs; the limit is {MAX_MATRIX_JOBS}"])
    return result


def parse_file_command(text: str, kind: str) -> List[Tuple[str, str]]:
    pairs: List[Tuple[str, str]] = []
    lines = text.replace("\r\n", "\n").split("\n")
    index = 0
    while index < len(lines):
        line = lines[index]
        index += 1
        if not line.strip():
            continue
        equals, heredoc = line.find("="), line.find("<<")
        if equals > 0 and (heredoc < 0 or equals < heredoc):
            name, value = line[:equals], line[equals + 1 :]
        elif heredoc > 0:
            name, delimiter = line[:heredoc], line[heredoc + 2 :]
            if not delimiter:
                raise StepFailure(f"{kind}: {line!r} has an empty heredoc delimiter")
            body: List[str] = []
            while index < len(lines) and lines[index] != delimiter:
                body.append(lines[index])
                index += 1
            if index >= len(lines):
                raise StepFailure(f"{kind}: matching delimiter not found {delimiter!r}")
            index += 1
            value = "\n".join(body)
        else:
            raise StepFailure(f"{kind}: invalid line {line!r}; write NAME=VALUE, or NAME<<DELIMITER, the lines, then DELIMITER")
        pairs.append((name.strip(), value))
    return pairs


class Masker:
    def __init__(self, values: Sequence[str] = ()) -> None:
        self.values: List[str] = []
        self.lock = threading.Lock()
        for value in values:
            self.add(value)

    def add(self, value: str) -> None:
        candidates = [value] + ([line for line in value.splitlines()] if "\n" in value else [])
        with self.lock:
            for candidate in candidates:
                if candidate.strip() and candidate not in self.values:
                    self.values.append(candidate)
            self.values.sort(key=len, reverse=True)

    def mask(self, text: str) -> str:
        for value in self.values:
            text = text.replace(value, "***")
        return text

    def reveals(self, text: str) -> bool:
        return any(value in text for value in self.values)

    def scrub(self, value: Any) -> Any:
        if isinstance(value, str):
            return self.mask(value)
        if isinstance(value, list):
            return [self.scrub(item) for item in value]
        if isinstance(value, dict):
            return {key: self.scrub(item) for key, item in value.items()}
        return value


class RunLog:
    def __init__(self, masker: Masker, stream: Optional[IO[str]], path: str) -> None:
        self.masker = masker
        self.stream = stream
        self.path = path
        self.lock = threading.Lock()

    def write(self, prefix: str, text: str) -> None:
        line = self.masker.mask(f"[{prefix}] {text}")
        with self.lock:
            with open(self.path, "a", encoding="utf-8") as handle:
                handle.write(line + "\n")
            if self.stream is not None:
                self.stream.write(line + "\n")
                self.stream.flush()


def glob_regex(pattern: str) -> "re.Pattern[str]":
    out: List[str] = []
    index = 0
    while index < len(pattern):
        if pattern.startswith("**/", index):
            out.append("(?:.*/)?")
            index += 3
            continue
        if pattern.startswith("**", index):
            out.append(".*")
            index += 2
            continue
        char = pattern[index]
        out.append("[^/]*" if char == "*" else "[^/]" if char == "?" else re.escape(char))
        index += 1
    return re.compile("^" + "".join(out) + "$")


def match_files(root: str, patterns: Sequence[str]) -> List[str]:
    rules: List[Tuple[bool, "re.Pattern[str]"]] = []
    for raw in patterns:
        for pattern in str(raw).splitlines():
            pattern = pattern.strip()
            if not pattern:
                continue
            negate = pattern.startswith("!")
            body = pattern[1:] if negate else pattern
            if os.path.isabs(body):
                body = os.path.relpath(body, root).replace(os.sep, "/")
            if body.startswith("./"):
                body = body[2:]
            rules.append((negate, glob_regex(body)))
    found: List[str] = []
    for directory, dirnames, filenames in os.walk(root):
        dirnames.sort()
        for name in sorted(filenames):
            relative = os.path.relpath(os.path.join(directory, name), root).replace(os.sep, "/")
            keep = False
            for negate, regex in rules:
                if regex.match(relative):
                    keep = not negate
            if keep:
                found.append(relative)
    return sorted(found)


def hash_files(root: str, patterns: Sequence[str]) -> str:
    files = match_files(root, patterns)
    if not files:
        return ""
    outer = hashlib.sha256()
    for relative in files:
        inner = hashlib.sha256()
        with open(os.path.join(root, relative), "rb") as handle:
            for chunk in iter(lambda: handle.read(65536), b""):
                inner.update(chunk)
        outer.update(inner.digest())
    return outer.hexdigest()


def sha256_file(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def cache_version(paths: Sequence[str]) -> str:
    return hashlib.sha256(("|".join(paths) + "|gzip").encode()).hexdigest()


@dataclass
class CacheEntry:
    key: str
    version: str
    scope: str
    digest: str
    created: float
    size: int


class CacheStore:
    def __init__(self, root: str) -> None:
        self.root = root
        self.lock = threading.Lock()
        os.makedirs(os.path.join(root, "blobs"), exist_ok=True)

    def index_path(self) -> str:
        return os.path.join(self.root, "index.json")

    def entries(self) -> List[CacheEntry]:
        try:
            with open(self.index_path(), encoding="utf-8") as handle:
                data = json.load(handle)
        except FileNotFoundError:
            return []
        return [CacheEntry(**item) for item in data.get("entries", [])]

    def write(self, entries: List[CacheEntry]) -> None:
        temporary = self.index_path() + ".tmp"
        with open(temporary, "w", encoding="utf-8") as handle:
            json.dump({"entries": [entry.__dict__ for entry in entries]}, handle, indent=2)
        os.replace(temporary, self.index_path())

    def find(self, key: str, restore_keys: Sequence[str], version: str, scopes: Sequence[str]) -> Optional[Tuple[CacheEntry, bool]]:
        with self.lock:
            entries = [entry for entry in self.entries() if entry.version == version]
        for scope in scopes:
            here = [entry for _, entry in sorted(enumerate(entries), key=lambda pair: (-pair[1].created, -pair[0])) if entry.scope == scope]
            for entry in here:
                if entry.key == key:
                    return entry, True
            for prefix in [key] + list(restore_keys):
                for entry in here:
                    if entry.key.startswith(prefix):
                        return entry, False
        return None

    def blob(self, entry: CacheEntry) -> str:
        path = os.path.join(self.root, "blobs", entry.digest + ".tgz")
        if not os.path.exists(path) or sha256_file(path) != entry.digest:
            raise StepFailure(f"cache blob for key {entry.key!r} is missing or does not match its digest {entry.digest[:12]}")
        return path

    def save(self, key: str, version: str, scope: str, archive: str) -> bool:
        digest = sha256_file(archive)
        with self.lock:
            entries = self.entries()
            if any(entry.key == key and entry.version == version and entry.scope == scope for entry in entries):
                return False
            blob = os.path.join(self.root, "blobs", digest + ".tgz")
            if not os.path.exists(blob):
                shutil.copyfile(archive, blob)
            entries.append(CacheEntry(key, version, scope, digest, time.time(), os.path.getsize(blob)))
            self.write(entries)
        return True


def inside(base: str, path: str) -> bool:
    base_real, path_real = os.path.realpath(base), os.path.realpath(path)
    return path_real == base_real or path_real.startswith(base_real + os.sep)


def cache_targets(paths: Sequence[str], workspace: str, home: str) -> List[Tuple[str, str, str]]:
    targets: List[Tuple[str, str, str]] = []
    for raw in paths:
        if raw == "~" or raw.startswith("~/"):
            label, base, relative = "home", home, raw[2:] or "."
        elif os.path.isabs(raw):
            raise StepFailure(f"cache path {raw!r} is outside the workspace and ~; minici caches only paths inside them")
        else:
            label, base, relative = "workspace", workspace, raw
        absolute = os.path.normpath(os.path.join(base, relative))
        if not inside(base, absolute):
            raise StepFailure(f"cache path {raw!r} leaves the {label}")
        matches = sorted(glob.glob(absolute, recursive=True)) if glob.has_magic(relative) else [absolute]
        targets.extend((label, base, match) for match in matches)
    return targets


def write_cache_archive(targets: Sequence[Tuple[str, str, str]], destination: str) -> int:
    with tarfile.open(destination, "w:gz") as tar:
        for label, base, absolute in targets:
            if os.path.exists(absolute):
                tar.add(absolute, arcname=label + "/" + os.path.relpath(absolute, base).replace(os.sep, "/"), recursive=True)
    with tarfile.open(destination, "r:gz") as tar:
        return sum(1 for member in tar.getmembers() if member.isfile())


def extract_cache_archive(archive: str, roots: Dict[str, str]) -> int:
    count = 0
    with tarfile.open(archive, "r:gz") as tar:
        for member in tar.getmembers():
            label, _, rest = member.name.partition("/")
            base = roots.get(label)
            if base is None or not (member.isfile() or member.isdir()):
                raise StepFailure(f"refusing cache member {member.name!r}: only files and directories under workspace/ or home/ are restored")
            target = os.path.normpath(os.path.join(base, rest))
            if not inside(base, target):
                raise StepFailure(f"refusing cache member {member.name!r}: it points outside the {label}")
            if member.isdir():
                os.makedirs(target, exist_ok=True)
                continue
            os.makedirs(os.path.dirname(target), exist_ok=True)
            source = tar.extractfile(member)
            if source is None:
                continue
            with open(target, "wb") as handle:
                shutil.copyfileobj(source, handle)
            os.chmod(target, member.mode & 0o777)
            count += 1
    return count


class ArtifactStore:
    def __init__(self, root: str) -> None:
        self.root = root
        self.lock = threading.Lock()
        os.makedirs(root, exist_ok=True)

    def names(self) -> List[str]:
        return sorted(name for name in os.listdir(self.root) if os.path.isdir(os.path.join(self.root, name)))

    def upload(self, name: str, files: Sequence[Tuple[str, str]], overwrite: bool) -> int:
        bad = sorted(set(name) & ARTIFACT_NAME_FORBIDDEN)
        if not name or bad:
            raise StepFailure(f"artifact name {name!r} is empty or contains {''.join(bad)!r}")
        with self.lock:
            target = os.path.join(self.root, name)
            if os.path.exists(target):
                if not overwrite:
                    raise StepFailure(f"an artifact named {name!r} already exists in this run; names are unique per run, so use another name or overwrite: true")
                shutil.rmtree(target)
            for source, relative in files:
                destination = os.path.join(target, relative)
                os.makedirs(os.path.dirname(destination), exist_ok=True)
                shutil.copy2(source, destination)
        return len(files)

    def download(self, name: str, destination: str) -> int:
        source = os.path.join(self.root, name)
        if not os.path.isdir(source):
            raise StepFailure(f"artifact {name!r} not found in this run; uploaded so far: {', '.join(self.names()) or 'none'} (does this job need the uploading job?)")
        count = 0
        for directory, _, filenames in os.walk(source):
            for filename in filenames:
                relative = os.path.relpath(os.path.join(directory, filename), source)
                target = os.path.join(destination, relative)
                os.makedirs(os.path.dirname(target), exist_ok=True)
                shutil.copy2(os.path.join(directory, filename), target)
                count += 1
        return count


def artifact_files(workspace: str, paths: Sequence[str]) -> List[Tuple[str, str]]:
    roots: List[str] = []
    files: Set[str] = set()
    for raw in paths:
        absolute = os.path.normpath(os.path.join(workspace, raw))
        if not inside(workspace, absolute):
            raise StepFailure(f"artifact path {raw!r} leaves the workspace")
        if glob.has_magic(raw):
            prefix = raw.split("*")[0].split("?")[0].split("[")[0]
            roots.append(os.path.normpath(os.path.join(workspace, os.path.dirname(prefix.rstrip("/")) if not prefix.endswith("/") else prefix)))
            files.update(match for match in glob.glob(absolute, recursive=True) if os.path.isfile(match))
        elif os.path.isdir(absolute):
            roots.append(absolute)
            for directory, _, filenames in os.walk(absolute):
                files.update(os.path.join(directory, filename) for filename in filenames)
        elif os.path.isfile(absolute):
            roots.append(os.path.dirname(absolute))
            files.add(absolute)
    if not files:
        return []
    base = os.path.commonpath(roots)
    return [(path, os.path.relpath(path, base)) for path in sorted(files)]


def copy_repository(source: str, destination: str, skip: Set[str]) -> int:
    count = 0
    source = os.path.realpath(source)
    for directory, dirnames, filenames in os.walk(source):
        dirnames[:] = sorted(
            name for name in dirnames if name not in (".git", ".minici") and os.path.realpath(os.path.join(directory, name)) not in skip
        )
        target = os.path.normpath(os.path.join(destination, os.path.relpath(directory, source)))
        os.makedirs(target, exist_ok=True)
        for name in sorted(filenames):
            shutil.copy2(os.path.join(directory, name), os.path.join(target, name), follow_symlinks=False)
            count += 1
    return count


def builtin_action(uses: str) -> Optional[str]:
    spec = uses.split("@", 1)[0].strip().lower()
    return spec if spec in BUILTIN_ACTIONS and "@" in uses else None


def shell_command(shell: str, search_path: str) -> Tuple[List[str], str]:
    name = (shell or "").strip()
    if name in SHELLS:
        argv = list(SHELLS[name])
        if name == "" and shutil.which("bash", path=search_path) is None:
            argv = list(SHELLS["sh"])
        if name == "python" and shutil.which("python", path=search_path) is None:
            argv[0] = "python3"
        return argv, ".py" if name == "python" else ".sh"
    if "{0}" in name:
        return shlex.split(name), ".sh"
    raise StepFailure(f"shell {name!r} is not bash, sh or python; a custom shell is a command with {{0}} where the script path goes")


def minutes(value: Any, default: Optional[float]) -> Optional[float]:
    if value is None or value == "":
        return default
    number = to_number(value)
    if math.isnan(number) or number <= 0:
        raise StepFailure(f"timeout-minutes must be a positive number, got {to_text(value)!r}")
    return number


def runner_os() -> str:
    return {"Linux": "Linux", "Darwin": "macOS", "Windows": "Windows"}.get(platform.system(), platform.system())


def runner_arch() -> str:
    machine = platform.machine().lower()
    return {"x86_64": "X64", "amd64": "X64", "arm64": "ARM64", "aarch64": "ARM64"}.get(machine, machine.upper())


def kill_group(process: "subprocess.Popen[bytes]") -> None:
    try:
        if os.name == "posix":
            os.killpg(process.pid, signal.SIGKILL)
        else:
            process.kill()
    except (ProcessLookupError, PermissionError, OSError):
        pass


def watch(process: "subprocess.Popen[bytes]", step_deadline: Optional[float], job_deadline: Optional[float], cancel: Optional[threading.Event], verdict: Dict[str, str]) -> None:
    while process.poll() is None:
        now = time.monotonic()
        why = ""
        if cancel is not None and cancel.is_set():
            why = "cancelled"
        elif step_deadline is not None and now > step_deadline:
            why = "timeout"
        elif job_deadline is not None and now > job_deadline:
            why = "job-timeout"
        if why:
            verdict["why"] = why
            kill_group(process)
            return
        time.sleep(0.02)


@dataclass
class RunOptions:
    repo: str
    event: str = "push"
    ref: str = "refs/heads/main"
    sha: str = "0" * 40
    base_ref: str = ""
    event_payload: Dict[str, Any] = field(default_factory=dict)
    secrets: Dict[str, str] = field(default_factory=dict)
    variables: Dict[str, str] = field(default_factory=dict)
    inputs: Dict[str, str] = field(default_factory=dict)
    runs_dir: str = ".minici"
    cache_dir: Optional[str] = None
    parallel: int = 4
    keep: bool = False
    default_branch: str = "main"
    repository: str = "local/repo"
    actor: str = "minici"
    stream: Optional[IO[str]] = None


@dataclass
class StepResult:
    index: int
    name: str
    id: Optional[str]
    outcome: str
    conclusion: str
    duration: float = 0.0
    error: str = ""


@dataclass
class JobRun:
    job: Job
    key: str
    display: str
    matrix: Dict[str, Any]
    strategy: Dict[str, Any]
    result: str = "pending"
    outcome: str = ""
    continue_on_error: bool = False
    started: Optional[int] = None
    finished: Optional[int] = None
    duration: float = 0.0
    reason: str = ""
    steps: List[StepResult] = field(default_factory=list)
    outputs: Dict[str, str] = field(default_factory=dict)
    annotations: List[Dict[str, str]] = field(default_factory=list)
    summary: str = ""
    cancel: threading.Event = field(default_factory=threading.Event)

    @property
    def done(self) -> bool:
        return self.result in FINAL_RESULTS


@dataclass
class JobState:
    status: str = "success"
    env_files: Dict[str, str] = field(default_factory=dict)
    path_prefix: List[str] = field(default_factory=list)
    steps: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    posts: List[Tuple[str, Callable[[], None]]] = field(default_factory=list)
    pending_outputs: Dict[str, str] = field(default_factory=dict)
    groups: List["subprocess.Popen[bytes]"] = field(default_factory=list)
    deadline: Optional[float] = None
    timeout: float = DEFAULT_TIMEOUT_MINUTES
    stop_token: str = ""


def layered_env(*layers: Dict[str, str]) -> Dict[str, str]:
    merged: Dict[str, str] = {}
    for layer in layers:
        merged.update(layer)
    return merged


def aggregate(legs: Sequence[JobRun]) -> str:
    results = [leg.result for leg in legs]
    if "failure" in results:
        return "failure"
    if "cancelled" in results:
        return "cancelled"
    if all(result == "skipped" for result in results):
        return "skipped"
    return "success"


def run_conclusion(results: Dict[str, str]) -> str:
    values = list(results.values())
    if "failure" in values:
        return "failure"
    if "cancelled" in values:
        return "cancelled"
    return "success"


def leg_display(job: Job, combo: Dict[str, Any], scope: Scope) -> str:
    if job.name and "${{" in job.name:
        return to_text(interpolate(job.name, scope))
    base = job.name or job.id
    return f"{base} ({', '.join(to_text(value) for value in combo.values())})" if combo else base


def next_run_number(root: str) -> int:
    numbers = [int(name) for name in os.listdir(root) if name.isdigit()]
    return max(numbers, default=0) + 1


class Runner:
    def __init__(self, workflow: Workflow, options: RunOptions) -> None:
        self.workflow = workflow
        self.options = options
        self.lock = threading.Lock()
        self.sequence = itertools.count(1)
        self.masker = Masker(list(options.secrets.values()))
        root = os.path.abspath(options.runs_dir)
        runs = os.path.join(root, "runs")
        os.makedirs(runs, exist_ok=True)
        with self.lock:
            self.run_number = next_run_number(runs)
            self.run_dir = os.path.join(runs, str(self.run_number))
            os.makedirs(self.run_dir)
        self.run_id = str(int(time.time() * 1000))
        self.cache = CacheStore(os.path.abspath(options.cache_dir or os.path.join(root, "cache")))
        self.artifacts = ArtifactStore(os.path.join(self.run_dir, "artifacts"))
        self.log = RunLog(self.masker, options.stream, os.path.join(self.run_dir, "log.txt"))
        self.event_path = os.path.join(self.run_dir, "event.json")
        with open(self.event_path, "w", encoding="utf-8") as handle:
            json.dump(options.event_payload, handle)
        self.skip_paths = {os.path.realpath(root), os.path.realpath(self.cache.root)}
        self.results: Dict[str, str] = {}
        self.job_outputs: Dict[str, Dict[str, str]] = {}
        self.legs: Dict[str, List[JobRun]] = {}
        self.cache_events: List[Dict[str, Any]] = []
        self.warnings: List[str] = []
        self.workflow_env: Dict[str, str] = {}

    def warn(self, message: str) -> None:
        with self.lock:
            if message not in self.warnings:
                self.warnings.append(message)
        self.log.write("minici", f"warning: {message}")

    def github_context(self, job_id: str = "") -> Dict[str, Any]:
        ref = self.options.ref
        ref_name = ref.split("/", 2)[2] if ref.startswith("refs/") and ref.count("/") >= 2 else ref
        return {
            "event_name": self.options.event,
            "event": self.options.event_payload,
            "event_path": self.event_path,
            "ref": ref,
            "ref_name": ref_name,
            "ref_type": "tag" if ref.startswith("refs/tags/") else "branch",
            "base_ref": self.options.base_ref,
            "head_ref": "",
            "sha": self.options.sha,
            "repository": self.options.repository,
            "repository_owner": self.options.repository.split("/")[0],
            "actor": self.options.actor,
            "workflow": self.workflow.name,
            "run_id": self.run_id,
            "run_number": str(self.run_number),
            "run_attempt": "1",
            "job": job_id,
            "server_url": "https://github.com",
            "workspace": "",
        }

    def needs_context(self, job: Job) -> Dict[str, Any]:
        return {need: {"result": self.results.get(need, ""), "outputs": dict(self.job_outputs.get(need, {}))} for need in job.needs}

    def base_scope(self, job: Job) -> Scope:
        upstream = ancestors(self.workflow.jobs, job.id)
        return Scope(
            {"github": self.github_context(job.id), "needs": self.needs_context(job), "vars": self.options.variables, "inputs": self.options.inputs},
            status={
                "success": lambda: all(self.results.get(name) == "success" for name in upstream),
                "failure": lambda: any(self.results.get(name) == "failure" for name in upstream),
                "cancelled": lambda: False,
                "always": lambda: True,
            },
        )

    def record_skip(self, job: Job, result: str, reason: str) -> None:
        leg = JobRun(job, job.id, job.name or job.id, {}, {}, result=result, outcome=result, reason=reason)
        with self.lock:
            leg.finished = next(self.sequence) if result == "failure" else None
        self.legs[job.id] = [leg]
        self.results[job.id] = result
        self.job_outputs[job.id] = {}
        self.log.write(job.name or job.id, f"job {result}: {reason}")

    def start_job(self, job: Job) -> List[JobRun]:
        scope = self.base_scope(job)
        try:
            node, warning = condition_ast(job.condition)
            if warning:
                self.warn(f"jobs.{job.id}.{warning}")
            should_run = truthy(evaluate_node(node, scope))
        except ExpressionError as exc:
            self.record_skip(job, "failure", f"if: {exc}")
            return []
        if not should_run:
            blocked = [f"{name} was {self.results.get(name)}" for name in self.workflow.jobs if name in ancestors(self.workflow.jobs, job.id) and self.results.get(name) != "success"]
            if job.condition in (None, "") and blocked:
                reason = "needs " + ", ".join(blocked)
            else:
                reason = f"if: {to_text(job.condition) or 'success()'} evaluated to false"
            self.record_skip(job, "skipped", reason)
            return []
        try:
            strategy = interpolate(job.strategy, scope)
            combos = expand_matrix(strategy.get("matrix")) if "matrix" in strategy else [{}]
        except (ExpressionError, WorkflowError) as exc:
            self.record_skip(job, "failure", f"strategy: {exc}")
            return []
        fail_fast = truthy(strategy.get("fail-fast", True))
        max_parallel = int(to_number(strategy.get("max-parallel") or len(combos)))
        legs: List[JobRun] = []
        for index, combo in enumerate(combos):
            leg_scope = Scope(dict(scope.contexts, matrix=combo), scope.status)
            key = job.id if len(combos) == 1 and not combo else f"{job.id}-{index + 1}"
            strategy_context = {"fail-fast": fail_fast, "job-index": index, "job-total": len(combos), "max-parallel": max(1, max_parallel)}
            legs.append(JobRun(job, key, leg_display(job, combo, leg_scope), combo, strategy_context))
        self.legs[job.id] = legs
        return legs

    def make_dirs(self, leg: JobRun) -> Dict[str, str]:
        root = os.path.join(self.run_dir, "jobs", leg.key)
        dirs = {name: os.path.join(root, name) for name in ("workspace", "temp", "home", "files", "toolcache")}
        dirs["root"] = root
        for name, path in dirs.items():
            os.makedirs(path, exist_ok=True)
        return dirs

    def default_env(self, leg: JobRun, dirs: Dict[str, str]) -> Dict[str, str]:
        github = self.github_context(leg.job.id)
        return {
            "GITHUB_ACTIONS": "true",
            "GITHUB_WORKFLOW": self.workflow.name,
            "GITHUB_RUN_ID": self.run_id,
            "GITHUB_RUN_NUMBER": str(self.run_number),
            "GITHUB_RUN_ATTEMPT": "1",
            "GITHUB_JOB": leg.job.id,
            "GITHUB_REF": github["ref"],
            "GITHUB_REF_NAME": github["ref_name"],
            "GITHUB_REF_TYPE": github["ref_type"],
            "GITHUB_SHA": self.options.sha,
            "GITHUB_EVENT_NAME": self.options.event,
            "GITHUB_EVENT_PATH": self.event_path,
            "GITHUB_REPOSITORY": self.options.repository,
            "GITHUB_ACTOR": self.options.actor,
            "GITHUB_SERVER_URL": "https://github.com",
            "GITHUB_WORKSPACE": dirs["workspace"],
            "RUNNER_OS": runner_os(),
            "RUNNER_ARCH": runner_arch(),
            "RUNNER_NAME": "minici",
            "RUNNER_TEMP": dirs["temp"],
            "RUNNER_TOOL_CACHE": dirs["toolcache"],
        }

    def run_leg(self, leg: JobRun) -> None:
        with self.lock:
            leg.started = next(self.sequence)
        started = time.monotonic()
        dirs = self.make_dirs(leg)
        self.log.write(leg.display, f"job started in a fresh workspace {dirs['workspace']}")
        state = JobState()
        try:
            self.execute(leg, dirs, state)
        except Exception as exc:
            self.finish(leg, "failure", f"minici internal error: {exc!r}")
        finally:
            for process in state.groups:
                kill_group(process)
            leg.duration = time.monotonic() - started
            with self.lock:
                leg.finished = next(self.sequence)
            if not self.options.keep:
                shutil.rmtree(dirs["root"], ignore_errors=True)

    def finish(self, leg: JobRun, outcome: str, reason: str = "") -> None:
        leg.outcome = outcome
        leg.result = "success" if outcome == "failure" and leg.continue_on_error else outcome
        if reason:
            leg.reason = reason
        note = f" (outcome {outcome}, continue-on-error: true)" if leg.result != outcome else ""
        self.log.write(leg.display, f"job {leg.result}{note}" + (f": {leg.reason}" if leg.reason else ""))

    def execute(self, leg: JobRun, dirs: Dict[str, str], state: JobState) -> None:
        job = leg.job
        base = self.base_scope(job)
        github = dict(base.contexts["github"], workspace=dirs["workspace"])
        contexts = dict(base.contexts, github=github, matrix=leg.matrix, strategy=leg.strategy, secrets=dict(self.options.secrets))
        job_scope = Scope(dict(contexts))
        try:
            leg.continue_on_error = truthy(interpolate(job.continue_on_error, job_scope)) if job.continue_on_error is not None else False
            timeout = minutes(interpolate(job.timeout_minutes, job_scope), DEFAULT_TIMEOUT_MINUTES) or DEFAULT_TIMEOUT_MINUTES
            job_env = {key: to_text(interpolate(value, job_scope)) for key, value in job.env.items()}
        except (ExpressionError, StepFailure) as exc:
            self.finish(leg, "failure", f"set up job: {exc}")
            return
        unsupported = [step.uses for step in job.steps if step.uses and builtin_action(step.uses) is None]
        if unsupported:
            owner_repo, _, ref = (unsupported[0] or "").partition("@")
            self.finish(leg, "failure", f"set up job: minici runs run: steps and {', '.join(BUILTIN_ACTIONS)}; {unsupported[0]} would be downloaded from https://github.com/{owner_repo} at {ref or 'its default branch'} and run from its action.yml")
            return
        state.timeout = timeout
        state.deadline = time.monotonic() + timeout * 60
        for step in job.steps:
            if state.deadline is not None and time.monotonic() > state.deadline and state.status != "cancelled":
                self.time_out(leg, state)
            self.run_step(leg, step, state, dirs, job_env, contexts)
        for name, post in reversed(state.posts):
            self.log.write(leg.display, f"> Post {name}")
            try:
                post()
            except StepFailure as exc:
                self.warn(f"{leg.display}: post step {name}: {exc}")
        scope = self.step_scope(contexts, state, layered_env(self.workflow_env, job_env, state.env_files), dirs)
        for name, expression in job.outputs.items():
            try:
                value = to_text(interpolate(expression, scope))
            except ExpressionError as exc:
                self.warn(f"{leg.display}: output {name}: {exc}")
                continue
            if self.masker.reveals(value):
                self.warn(f"{leg.display}: Skip output '{name}' since it may contain secret.")
                continue
            leg.outputs[name] = value
        self.finish(leg, state.status, leg.reason)

    def time_out(self, leg: JobRun, state: JobState) -> None:
        state.status = "cancelled"
        leg.reason = f"the job exceeded timeout-minutes ({format_number(state.timeout)}) and was cancelled"
        leg.cancel.set()
        state.deadline = time.monotonic() + CLEANUP_GRACE_SECONDS

    def step_scope(self, contexts: Dict[str, Any], state: JobState, env_context: Dict[str, str], dirs: Dict[str, str]) -> Scope:
        values = dict(contexts)
        values["env"] = dict(env_context)
        values["steps"] = {key: dict(value) for key, value in state.steps.items()}
        values["job"] = {"status": state.status}
        values["runner"] = {"os": runner_os(), "arch": runner_arch(), "name": "minici", "temp": dirs["temp"], "tool_cache": dirs["toolcache"], "environment": "self-hosted"}
        return Scope(
            values,
            status={
                "success": lambda: state.status == "success",
                "failure": lambda: state.status == "failure",
                "cancelled": lambda: state.status == "cancelled",
                "always": lambda: True,
            },
            hash_files=lambda patterns: hash_files(dirs["workspace"], patterns),
        )

    def run_step(self, leg: JobRun, step: Step, state: JobState, dirs: Dict[str, str], job_env: Dict[str, str], contexts: Dict[str, Any]) -> None:
        if leg.cancel.is_set() and state.status != "cancelled":
            state.status = "cancelled"
            leg.reason = leg.reason or "cancelled"
        name = step.display()
        env_context = layered_env(self.workflow_env, job_env, state.env_files)
        scope = self.step_scope(contexts, state, env_context, dirs)
        try:
            node, warning = condition_ast(step.condition)
            if warning:
                self.warn(f"{leg.job.id}.steps[{step.index}].{warning}")
            should_run = truthy(evaluate_node(node, scope))
        except ExpressionError as exc:
            self.record_step(leg, step, state, name, "failure", "failure", 0.0, f"if: {exc}", {})
            return
        if not should_run:
            self.record_step(leg, step, state, name, "skipped", "skipped", 0.0, "", {})
            self.log.write(leg.display, f"- {name} (skipped: if {to_text(step.condition) or 'success()'})")
            return
        started = time.monotonic()
        self.log.write(leg.display, f"> {name}")
        outcome, error, outputs = "success", "", {}
        continue_on_error = False
        try:
            step_env = {key: to_text(interpolate(value, scope)) for key, value in step.env.items()}
            env_context = layered_env(self.workflow_env, job_env, state.env_files, step_env)
            scope = self.step_scope(contexts, state, env_context, dirs)
            continue_on_error = truthy(interpolate(step.continue_on_error, scope)) if step.continue_on_error is not None else False
            step_minutes = minutes(interpolate(step.timeout_minutes, scope), None)
            if step.run is not None:
                outputs = self.run_command(leg, step, state, dirs, scope, env_context, step_minutes)
            else:
                outputs = self.run_action(leg, step, state, dirs, scope)
        except StepFailure as exc:
            outcome, error, outputs = exc.outcome, str(exc), exc.outputs
        except ExpressionError as exc:
            outcome, error = "failure", str(exc)
        conclusion = "success" if outcome == "failure" and continue_on_error else outcome
        self.record_step(leg, step, state, name, outcome, conclusion, time.monotonic() - started, error, outputs)
        if error:
            self.log.write(leg.display, f"  error: {error}")
        suffix = f" (outcome {outcome}, continue-on-error)" if conclusion != outcome else ""
        self.log.write(leg.display, f"< {name}: {conclusion}{suffix}")

    def record_step(self, leg: JobRun, step: Step, state: JobState, name: str, outcome: str, conclusion: str, duration: float, error: str, outputs: Dict[str, str]) -> None:
        leg.steps.append(StepResult(step.index, name, step.id, outcome, conclusion, round(duration, 3), self.masker.mask(error)))
        if step.id:
            state.steps[step.id] = {"outputs": dict(outputs), "outcome": outcome, "conclusion": conclusion}
        if conclusion == "failure" and state.status == "success":
            state.status = "failure"
        if conclusion == "cancelled":
            state.status = "cancelled"

    def command_files(self, dirs: Dict[str, str]) -> Dict[str, str]:
        token = uuid.uuid4().hex
        files = {name: os.path.join(dirs["files"], f"{name.lower()}_{token}") for name in ("GITHUB_OUTPUT", "GITHUB_ENV", "GITHUB_PATH", "GITHUB_STEP_SUMMARY")}
        for path in files.values():
            open(path, "w").close()
        return files

    def process_env(self, leg: JobRun, step: Step, dirs: Dict[str, str], state: JobState, env_context: Dict[str, str], files: Dict[str, str]) -> Dict[str, str]:
        env = {
            "PATH": os.environ.get("PATH", os.defpath),
            "LANG": os.environ.get("LANG", "C.UTF-8"),
            "HOME": dirs["home"],
            "TMPDIR": dirs["temp"],
            "CI": "true",
        }
        env.update(env_context)
        env.update(self.default_env(leg, dirs))
        env.update(files)
        env["GITHUB_ACTION"] = step.id or f"__run_{step.index}"
        if state.path_prefix:
            env["PATH"] = os.pathsep.join(state.path_prefix + [env["PATH"]])
        return env

    def run_command(self, leg: JobRun, step: Step, state: JobState, dirs: Dict[str, str], scope: Scope, env_context: Dict[str, str], step_minutes: Optional[float]) -> Dict[str, str]:
        script = to_text(interpolate(step.run, scope))
        run_defaults = layered_env(dict(self.workflow.defaults.get("run") or {}), dict(leg.job.defaults.get("run") or {}))
        shell = step.shell if step.shell is not None else to_text(run_defaults.get("shell"))
        working = step.working_directory if step.working_directory is not None else run_defaults.get("working-directory")
        cwd = dirs["workspace"]
        if working:
            cwd = os.path.normpath(os.path.join(dirs["workspace"], to_text(interpolate(working, scope))))
            if not os.path.isdir(cwd):
                raise StepFailure(f"working-directory {to_text(working)!r} does not exist in the workspace")
        files = self.command_files(dirs)
        env = self.process_env(leg, step, dirs, state, env_context, files)
        argv, suffix = shell_command(to_text(shell), env["PATH"])
        script_path = os.path.join(dirs["temp"], f"{uuid.uuid4().hex}{suffix}")
        with open(script_path, "w", encoding="utf-8") as handle:
            handle.write(script)
        argv = [script_path if part == "{0}" else part for part in argv]
        step_deadline = time.monotonic() + step_minutes * 60 if step_minutes else None
        job_deadline = state.deadline
        code, why = self.spawn(leg, state, argv, cwd, env, step_deadline, job_deadline)
        outputs = self.apply_file_commands(leg, state, files)
        if why == "timeout":
            raise StepFailure(f"the step exceeded its timeout-minutes ({format_number(step_minutes or 0)}) and was killed", outputs=outputs)
        if why == "job-timeout":
            self.time_out(leg, state)
            raise StepFailure(leg.reason, outcome="cancelled", outputs=outputs)
        if why == "cancelled":
            raise StepFailure("the step was cancelled", outcome="cancelled", outputs=outputs)
        if code != 0:
            raise StepFailure(f"Process completed with exit code {code}.", outputs=outputs)
        return outputs

    def spawn(self, leg: JobRun, state: JobState, argv: List[str], cwd: str, env: Dict[str, str], step_deadline: Optional[float], job_deadline: Optional[float]) -> Tuple[int, str]:
        try:
            process = subprocess.Popen(
                argv, cwd=cwd, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, start_new_session=os.name == "posix"
            )
        except OSError as exc:
            raise StepFailure(f"cannot start {argv[0]}: {exc.strerror}")
        state.groups.append(process)
        verdict = {"why": ""}
        cancel = None if leg.cancel.is_set() else leg.cancel
        watcher = threading.Thread(target=watch, args=(process, step_deadline, job_deadline, cancel, verdict), daemon=True)
        watcher.start()
        stream = process.stdout

        def pump() -> None:
            if stream is None:
                return
            for raw in iter(stream.readline, b""):
                self.handle_output(leg, state, raw.decode("utf-8", "replace").rstrip("\r\n"))

        reader = threading.Thread(target=pump, daemon=True)
        reader.start()
        code = process.wait()
        watcher.join()
        reader.join(OUTPUT_DRAIN_SECONDS)
        if reader.is_alive():
            self.warn(f"{leg.display}: a background process still holds the step's output; redirect it (cmd > file 2>&1 &) so the step can end cleanly")
        elif stream is not None:
            stream.close()
        return code, verdict["why"]

    def handle_output(self, leg: JobRun, state: JobState, line: str) -> None:
        if state.stop_token:
            if line == f"::{state.stop_token}::":
                state.stop_token = ""
            self.log.write(leg.display, "  | " + line)
            return
        match = COMMAND_RE.match(line)
        if match:
            command, properties, value = match.group(1).lower(), match.group(2) or "", match.group(3)
            if command == "add-mask":
                self.masker.add(value)
                self.log.write(leg.display, "  | ::add-mask::***")
                return
            if command in ("error", "warning", "notice"):
                leg.annotations.append({"level": command, "properties": properties, "message": self.masker.mask(value)})
            elif command == "stop-commands":
                state.stop_token = value
            elif command in ("set-env", "add-path"):
                self.warn(f"{leg.display}: the {command} command is disabled because any printed text could use it; write to $GITHUB_ENV or $GITHUB_PATH")
            elif command == "set-output":
                name = properties.split("=", 1)[1] if properties.startswith("name=") else properties
                state.pending_outputs[name] = value
                self.warn(f"{leg.display}: ::set-output is deprecated; write name=value to $GITHUB_OUTPUT")
        self.log.write(leg.display, "  | " + line)

    def apply_file_commands(self, leg: JobRun, state: JobState, files: Dict[str, str]) -> Dict[str, str]:
        def read(name: str) -> str:
            with open(files[name], encoding="utf-8", errors="replace") as handle:
                return handle.read()

        outputs = dict(state.pending_outputs)
        state.pending_outputs = {}
        for name, value in parse_file_command(read("GITHUB_OUTPUT"), "GITHUB_OUTPUT"):
            outputs[name] = value
        for name, value in parse_file_command(read("GITHUB_ENV"), "GITHUB_ENV"):
            if name.startswith(("GITHUB_", "RUNNER_")) or name == "NODE_OPTIONS":
                self.warn(f"{leg.display}: GITHUB_ENV cannot set {name}")
                continue
            state.env_files[name] = value
        for line in read("GITHUB_PATH").splitlines():
            if line.strip():
                state.path_prefix.insert(0, line.strip())
        leg.summary += read("GITHUB_STEP_SUMMARY")
        return outputs

    def run_action(self, leg: JobRun, step: Step, state: JobState, dirs: Dict[str, str], scope: Scope) -> Dict[str, str]:
        inputs = {key: to_text(interpolate(value, scope)) for key, value in step.inputs.items()}
        name = builtin_action(step.uses or "")
        handlers = {
            "actions/checkout": self.action_checkout,
            "actions/cache": self.action_cache,
            "actions/upload-artifact": self.action_upload,
            "actions/download-artifact": self.action_download,
        }
        return handlers[name or ""](leg, step, state, dirs, inputs)

    def action_checkout(self, leg: JobRun, step: Step, state: JobState, dirs: Dict[str, str], inputs: Dict[str, str]) -> Dict[str, str]:
        target = os.path.normpath(os.path.join(dirs["workspace"], inputs.get("path") or "."))
        if not inside(dirs["workspace"], target):
            raise StepFailure(f"checkout path {inputs.get('path')!r} leaves the workspace")
        count = copy_repository(self.options.repo, target, self.skip_paths)
        self.log.write(leg.display, f"  copied {count} files from {self.options.repo} (actions/checkout clones instead, and keeps the token in .git/config unless persist-credentials: false)")
        return {"ref": self.options.ref, "commit": self.options.sha}

    def cache_scopes(self) -> List[str]:
        scopes = [self.options.ref]
        if self.options.base_ref:
            scopes.append(self.options.base_ref if self.options.base_ref.startswith("refs/") else "refs/heads/" + self.options.base_ref)
        scopes.append("refs/heads/" + self.options.default_branch)
        return list(dict.fromkeys(scopes))

    def action_cache(self, leg: JobRun, step: Step, state: JobState, dirs: Dict[str, str], inputs: Dict[str, str]) -> Dict[str, str]:
        paths = [line.strip() for line in inputs.get("path", "").splitlines() if line.strip()]
        key = inputs.get("key", "").strip()
        if not paths or not key:
            raise StepFailure("actions/cache needs path: and key:")
        if "," in key or len(key) > 512:
            raise StepFailure(f"cache key {key!r} must be at most 512 characters and contain no commas")
        restore_keys = [line.strip() for line in inputs.get("restore-keys", "").splitlines() if line.strip()]
        version = cache_version(paths)
        roots = {"workspace": dirs["workspace"], "home": dirs["home"]}
        found = self.cache.find(key, restore_keys, version, self.cache_scopes())
        outputs = {"cache-primary-key": key, "cache-hit": ""}
        exact = False
        if found is not None:
            entry, exact = found
            count = extract_cache_archive(self.cache.blob(entry), roots)
            outputs["cache-hit"] = "true" if exact else "false"
            outputs["cache-matched-key"] = entry.key
            event = "restore-hit" if exact else "restore-partial"
            self.log.write(leg.display, f"  Cache restored from key: {entry.key} ({count} files, scope {entry.scope}, blob {entry.digest[:12]})")
        else:
            event = "restore-miss"
            if inputs.get("fail-on-cache-miss", "false").lower() == "true":
                raise StepFailure(f"Cache not found for input keys: {', '.join([key] + restore_keys)} (fail-on-cache-miss)")
            self.log.write(leg.display, f"  Cache not found for input keys: {', '.join([key] + restore_keys)}")
        self.record_cache(leg, event, key, found[0].key if found else "")
        if not exact:
            state.posts.append((step.display(), lambda: self.save_cache(leg, state, dirs, paths, key, version)))
        return outputs

    def save_cache(self, leg: JobRun, state: JobState, dirs: Dict[str, str], paths: List[str], key: str, version: str) -> None:
        if state.status != "success":
            self.log.write(leg.display, f"  not saving cache {key}: the job did not succeed")
            return
        targets = [target for target in cache_targets(paths, dirs["workspace"], dirs["home"]) if os.path.exists(target[2])]
        if not targets:
            self.log.write(leg.display, f"  not saving cache {key}: none of {', '.join(paths)} exists")
            return
        archive = os.path.join(dirs["temp"], f"cache-{uuid.uuid4().hex}.tgz")
        count = write_cache_archive(targets, archive)
        if self.cache.save(key, version, self.options.ref, archive):
            self.log.write(leg.display, f"  Cache saved with key: {key} ({count} files)")
            self.record_cache(leg, "save", key, key)
        else:
            self.log.write(leg.display, f"  cache {key} already exists; entries are immutable, so nothing was saved")
            self.record_cache(leg, "exists", key, key)

    def record_cache(self, leg: JobRun, event: str, key: str, matched: str) -> None:
        with self.lock:
            self.cache_events.append({"job": leg.key, "event": event, "key": key, "matched": matched})

    def action_upload(self, leg: JobRun, step: Step, state: JobState, dirs: Dict[str, str], inputs: Dict[str, str]) -> Dict[str, str]:
        name = inputs.get("name") or "artifact"
        paths = [line.strip() for line in inputs.get("path", "").splitlines() if line.strip()]
        if not paths:
            raise StepFailure("actions/upload-artifact needs path:")
        files = artifact_files(dirs["workspace"], paths)
        if not files:
            mode = inputs.get("if-no-files-found") or "warn"
            message = f"No files were found with the provided path: {', '.join(paths)}. No artifacts will be uploaded."
            if mode == "error":
                raise StepFailure(message)
            if mode == "warn":
                self.warn(f"{leg.display}: {message}")
            return {}
        count = self.artifacts.upload(name, files, inputs.get("overwrite", "false").lower() == "true")
        self.log.write(leg.display, f"  uploaded artifact {name!r}: {count} files")
        return {"artifact-id": name}

    def action_download(self, leg: JobRun, step: Step, state: JobState, dirs: Dict[str, str], inputs: Dict[str, str]) -> Dict[str, str]:
        target = os.path.normpath(os.path.join(dirs["workspace"], inputs.get("path") or "."))
        if not inside(dirs["workspace"], target):
            raise StepFailure(f"download path {inputs.get('path')!r} leaves the workspace")
        names = [inputs["name"]] if inputs.get("name") else self.artifacts.names()
        for name in names:
            destination = target if inputs.get("name") else os.path.join(target, name)
            count = self.artifacts.download(name, destination)
            self.log.write(leg.display, f"  downloaded artifact {name!r}: {count} files")
        return {"download-path": target}

    def run(self) -> Dict[str, Any]:
        scope = Scope({"github": self.github_context(), "secrets": dict(self.options.secrets), "vars": self.options.variables, "inputs": self.options.inputs})
        try:
            self.workflow_env = {key: to_text(interpolate(value, scope)) for key, value in self.workflow.env.items()}
        except ExpressionError as exc:
            raise WorkflowError([f"env: {exc}"])
        self.log.write("minici", f"run {self.run_number} of {self.workflow.name}: {self.options.event} {self.options.ref} {self.options.sha[:7]}, up to {self.options.parallel} jobs at once")
        pending = list(self.workflow.jobs)
        queue: List[JobRun] = []
        running: Dict["concurrent.futures.Future[None]", JobRun] = {}
        with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, self.options.parallel)) as pool:
            while pending or queue or running:
                progressed = True
                while progressed:
                    progressed = False
                    for job_id in list(pending):
                        job = self.workflow.jobs[job_id]
                        if all(need in self.results for need in job.needs):
                            pending.remove(job_id)
                            queue.extend(self.start_job(job))
                            progressed = True
                for leg in list(queue):
                    if len(running) >= max(1, self.options.parallel):
                        break
                    if leg.cancel.is_set():
                        queue.remove(leg)
                        leg.result = leg.outcome = "cancelled"
                        self.log.write(leg.display, f"job cancelled before it started: {leg.reason}")
                        self.leg_done(leg)
                        continue
                    busy = sum(1 for other in running.values() if other.job.id == leg.job.id)
                    if busy >= leg.strategy.get("max-parallel", 1):
                        continue
                    queue.remove(leg)
                    running[pool.submit(self.run_leg, leg)] = leg
                if not running:
                    continue
                done, _ = concurrent.futures.wait(list(running), return_when=concurrent.futures.FIRST_COMPLETED)
                for future in done:
                    leg = running.pop(future)
                    self.leg_done(leg)
        return self.summary()

    def leg_done(self, leg: JobRun) -> None:
        group = self.legs[leg.job.id]
        if leg.result == "failure" and leg.strategy.get("fail-fast", True) and len(group) > 1:
            for other in group:
                if other is not leg and not other.done and not other.cancel.is_set():
                    other.reason = f"cancelled by fail-fast after {leg.display} failed"
                    other.cancel.set()
        if all(other.done for other in group):
            outputs: Dict[str, str] = {}
            for other in sorted(group, key=lambda item: item.finished or 0):
                outputs.update(other.outputs)
            self.job_outputs[leg.job.id] = outputs
            self.results[leg.job.id] = aggregate(group)

    def summary(self) -> Dict[str, Any]:
        jobs: List[Dict[str, Any]] = []
        for job_id in self.workflow.jobs:
            for leg in self.legs.get(job_id, []):
                jobs.append(
                    {
                        "job": job_id,
                        "key": leg.key,
                        "name": leg.display,
                        "matrix": leg.matrix,
                        "needs": leg.job.needs,
                        "result": leg.result,
                        "outcome": leg.outcome,
                        "continue_on_error": leg.continue_on_error,
                        "started": leg.started,
                        "finished": leg.finished,
                        "duration": round(leg.duration, 3),
                        "reason": leg.reason,
                        "outputs": leg.outputs,
                        "steps": [step.__dict__ for step in leg.steps],
                        "annotations": leg.annotations,
                        "step_summary": leg.summary,
                    }
                )
        data = {
            "workflow": self.workflow.name,
            "file": self.workflow.path,
            "event": self.options.event,
            "ref": self.options.ref,
            "sha": self.options.sha,
            "run_number": self.run_number,
            "run_id": self.run_id,
            "run_dir": self.run_dir,
            "log": self.log.path,
            "conclusion": run_conclusion(self.results),
            "results": dict(self.results),
            "outputs": {job_id: dict(values) for job_id, values in self.job_outputs.items()},
            "waves": job_waves(self.workflow.jobs),
            "jobs": jobs,
            "cache": list(self.cache_events),
            "artifacts": self.artifacts.names(),
            "warnings": list(self.warnings),
        }
        return self.masker.scrub(data)


def order_violations(summary: Dict[str, Any]) -> List[str]:
    first: Dict[str, int] = {}
    last: Dict[str, int] = {}
    for leg in summary["jobs"]:
        if leg["started"] is not None:
            first[leg["job"]] = min(first.get(leg["job"], leg["started"]), leg["started"])
        if leg["finished"] is not None:
            last[leg["job"]] = max(last.get(leg["job"], leg["finished"]), leg["finished"])
    problems: List[str] = []
    for leg in summary["jobs"]:
        for need in leg["needs"]:
            if leg["job"] in first and need in last and first[leg["job"]] < last[need]:
                problems.append(f"{leg['job']} started (#{first[leg['job']]}) before {need} finished (#{last[need]})")
    return sorted(set(problems))


def plan_data(workflow: Workflow) -> Dict[str, Any]:
    waves = job_waves(workflow.jobs)
    jobs: Dict[str, Any] = {}
    for number, wave in enumerate(waves, start=1):
        for job_id in wave:
            job = workflow.jobs[job_id]
            matrix = job.strategy.get("matrix") if "matrix" in job.strategy else None
            legs: Optional[List[Dict[str, Any]]] = None
            if matrix is None:
                legs = [{}]
            elif not contains_expression(matrix):
                legs = expand_matrix(matrix)
            jobs[job_id] = {"wave": number, "needs": job.needs, "if": to_text(job.condition), "legs": legs, "steps": len(job.steps)}
    return {"workflow": workflow.name, "events": list(workflow.on), "waves": waves, "jobs": jobs, "critical_path": critical_path(workflow.jobs)}


def render_plan(plan: Dict[str, Any]) -> str:
    lines = [f"{plan['workflow']}: on {', '.join(plan['events'])}"]
    for number, wave in enumerate(plan["waves"], start=1):
        lines.append(f"wave {number}:")
        for job_id in wave:
            info = plan["jobs"][job_id]
            legs = info["legs"]
            count = "matrix from an expression, expanded at run time" if legs is None else (f"{len(legs)} legs" if legs != [{}] else "1 job")
            needs = f" needs {', '.join(info['needs'])}" if info["needs"] else ""
            condition = f" if {info['if']}" if info["if"] else ""
            lines.append(f"  {job_id:<16} {count}{needs}{condition}")
            if legs and legs != [{}]:
                for leg in legs:
                    lines.append("      " + ", ".join(f"{key}={to_text(value)}" for key, value in leg.items()))
    lines.append("critical path: " + " -> ".join(plan["critical_path"]))
    return "\n".join(lines)


def static_warnings(workflow: Workflow) -> List[str]:
    warnings: List[str] = []
    for job in workflow.jobs.values():
        _, warning = condition_ast(job.condition)
        if warning:
            warnings.append(f"jobs.{job.id}.{warning}")
        for step in job.steps:
            _, warning = condition_ast(step.condition)
            if warning:
                warnings.append(f"jobs.{job.id}.steps[{step.index}].{warning}")
            if step.uses and builtin_action(step.uses) is None:
                warnings.append(f"jobs.{job.id}.steps[{step.index}]: {step.uses} is not built into minici, so minici run fails this job at set-up")
    return warnings


def render_summary(summary: Dict[str, Any]) -> str:
    lines = [f"run {summary['run_number']} of {summary['workflow']} ({summary['event']} {summary['ref']} {summary['sha'][:7]}): {summary['conclusion']}"]
    width = max([len(job["name"]) for job in summary["jobs"]] + [4])
    lines.append(f"  {'job':<{width}}  {'result':<9} {'outcome':<9} {'order':<7} note")
    for job in summary["jobs"]:
        order = f"{job['started']}-{job['finished']}" if job["started"] is not None else "-"
        note = job["reason"] or (", ".join(f"{key}={value}" for key, value in job["outputs"].items()))
        lines.append(f"  {job['name']:<{width}}  {job['result']:<9} {job['outcome'] or '-':<9} {order:<7} {note}")
    for event in summary["cache"]:
        lines.append(f"  cache {event['event']}: {event['key']} ({event['job']})")
    for warning in summary["warnings"]:
        lines.append(f"  warning: {warning}")
    lines.append(f"  log: {summary['log']}")
    return "\n".join(lines)


def git_output(repo: str, *args: str) -> Optional[str]:
    if not os.path.exists(os.path.join(repo, ".git")):
        return None
    try:
        result = subprocess.run(["git", "-C", repo, *args], capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return result.stdout if result.returncode == 0 else None


def git_changed_files(repo: str) -> Optional[List[str]]:
    output = git_output(repo, "diff", "--name-only", "HEAD~1", "HEAD")
    if output is None:
        output = git_output(repo, "show", "--pretty=format:", "--name-only", "HEAD")
    return None if output is None else [line for line in output.splitlines() if line.strip()]


def default_repo(workflow_path: str) -> str:
    directory = os.path.dirname(os.path.abspath(workflow_path))
    if os.path.basename(directory) == "workflows" and os.path.basename(os.path.dirname(directory)) == ".github":
        return os.path.dirname(os.path.dirname(directory))
    return directory


def parse_pairs(items: Optional[Sequence[str]], what: str, from_env: bool = False) -> Dict[str, str]:
    pairs: Dict[str, str] = {}
    for item in items or []:
        name, has_value, value = item.partition("=")
        if not has_value:
            if not from_env or name not in os.environ:
                raise UsageError(f"{what} {name!r} needs a value: NAME=VALUE" + (", or NAME with NAME set in the environment" if from_env else ""))
            value = os.environ[name]
        if not NAME_RE.match(name):
            raise UsageError(f"{what} name {name!r} must contain only letters, digits, - and _")
        pairs[name] = value
    return pairs


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="minici", description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    run = commands.add_parser("run", help="run a workflow")
    plan = commands.add_parser("plan", help="print the job graph: waves, matrix legs, critical path")
    check = commands.add_parser("check", help="validate a workflow; exit 1 on warnings, 2 on errors")
    expr = commands.add_parser("expr", help="evaluate one expression against contexts from a JSON file")
    for sub in (run, plan, check):
        sub.add_argument("workflow", help="path to the workflow YAML file")
        sub.add_argument("--json", action="store_true", help="machine-readable output")
    expr.add_argument("expression")
    expr.add_argument("--context", help='JSON file such as {"github": {...}, "needs": {...}}')
    run.add_argument("--repo", help="directory actions/checkout copies (default: the directory that holds .github/workflows)")
    run.add_argument("--event", default="push", help="event name (default push)")
    run.add_argument("--ref", help="ref that triggered the run (default: the event payload's ref, then the repository HEAD, then refs/heads/main)")
    run.add_argument("--sha", help="commit sha (default: git rev-parse HEAD in --repo)")
    run.add_argument("--base-ref", default="", help="base branch for pull_request events")
    run.add_argument("--event-path", help="JSON event payload, available as github.event")
    run.add_argument("--changed", action="append", help="a changed file for paths filters (repeatable; default: git diff HEAD~1 HEAD)")
    run.add_argument("--secret", action="append", help="NAME=VALUE, or NAME to read the value from the environment")
    run.add_argument("--var", action="append", help="NAME=VALUE for the vars context")
    run.add_argument("--input", action="append", help="NAME=VALUE for the inputs context")
    run.add_argument("--runs-dir", default=".minici", help="where runs, logs and the cache live (default .minici)")
    run.add_argument("--cache-dir", help="cache store (default RUNS_DIR/cache)")
    run.add_argument("--parallel", type=int, default=4, help="how many jobs run at once (default 4)")
    run.add_argument("--default-branch", default="main")
    run.add_argument("--repository", default="local/repo", help="owner/name for github.repository")
    run.add_argument("--keep", action="store_true", help="keep each job's workspace after the run")
    return parser


def command_run(workflow: Workflow, args: argparse.Namespace) -> int:
    repo = os.path.abspath(args.repo or default_repo(args.workflow))
    payload: Dict[str, Any] = {}
    if args.event_path:
        try:
            with open(args.event_path, encoding="utf-8") as handle:
                payload = json.load(handle)
        except (OSError, json.JSONDecodeError) as exc:
            raise UsageError(f"--event-path {args.event_path}: {exc}")
    head = (git_output(repo, "symbolic-ref", "-q", "HEAD") or "").strip()
    ref = args.ref or to_text(payload.get("ref")) or head or "refs/heads/main"
    sha = args.sha or (git_output(repo, "rev-parse", "HEAD") or "").strip() or "0" * 40
    changed = [item for item in args.changed] if args.changed is not None else git_changed_files(repo)
    trigger = evaluate_trigger(workflow, args.event, ref, changed, args.base_ref)
    if not trigger.triggered:
        if args.json:
            print(json.dumps({"workflow": workflow.name, "triggered": False, "reason": trigger.reason, "conclusion": "skipped"}, indent=2))
        else:
            print(f"{workflow.name}: not triggered: {trigger.reason}")
        return 0
    options = RunOptions(
        repo=repo,
        event=args.event,
        ref=ref,
        sha=sha,
        base_ref=args.base_ref,
        event_payload=payload,
        secrets=parse_pairs(args.secret, "secret", from_env=True),
        variables=parse_pairs(args.var, "var"),
        inputs=parse_pairs(args.input, "input"),
        runs_dir=args.runs_dir,
        cache_dir=args.cache_dir,
        parallel=args.parallel,
        keep=args.keep,
        default_branch=args.default_branch,
        repository=args.repository,
        stream=sys.stderr if args.json else sys.stdout,
    )
    summary = Runner(workflow, options).run()
    summary["triggered"] = True
    summary["trigger"] = trigger.reason
    print(json.dumps(summary, indent=2) if args.json else render_summary(summary))
    return 0 if summary["conclusion"] == "success" else 1


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(list(sys.argv[1:] if argv is None else argv))
    try:
        if args.command == "expr":
            contexts: Dict[str, Any] = {}
            if args.context:
                with open(args.context, encoding="utf-8") as handle:
                    contexts = {str(key).lower(): value for key, value in json.load(handle).items()}
            status = {name: (lambda: True) if name in ("success", "always") else (lambda: False) for name in STATUS_FUNCTIONS}
            print(json.dumps(evaluate(args.expression.strip().removeprefix("${{").removesuffix("}}"), Scope(contexts, status))))
            return 0
        workflow = load_workflow(args.workflow)
        if args.command == "plan":
            plan = plan_data(workflow)
            print(json.dumps(plan, indent=2) if args.json else render_plan(plan))
            return 0
        if args.command == "check":
            warnings = static_warnings(workflow)
            if args.json:
                print(json.dumps({"ok": True, "jobs": list(workflow.jobs), "warnings": warnings, "notes": workflow.notes}, indent=2))
            else:
                print(f"{workflow.path}: {len(workflow.jobs)} jobs, {len(job_waves(workflow.jobs))} waves, no errors")
                for line in warnings:
                    print(f"warning: {line}")
                for line in workflow.notes:
                    print(f"note: {line}")
            return 1 if warnings else 0
        return command_run(workflow, args)
    except WorkflowError as exc:
        for problem in exc.problems:
            print(f"minici: {problem}", file=sys.stderr)
        return 2
    except (ExpressionError, UsageError, OSError) as exc:
        print(f"minici: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
