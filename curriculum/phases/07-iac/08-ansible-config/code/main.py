"""miniansible: an inventory, a playbook runner and modules that check before they change, over a local, docker exec or fake connection, with check mode, diff mode, handlers, facts and a PLAY RECAP."""
from __future__ import annotations

import argparse
import ast
import difflib
import fnmatch
import glob as globlib
import hashlib
import json
import os
import posixpath
import re
import shlex
import shutil
import stat as statlib
import subprocess
import sys
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

NULL_WORDS = {"", "~", "null", "Null", "NULL"}
TRUE_WORDS = {"true", "True", "TRUE", "yes", "Yes", "YES", "on", "On", "ON"}
FALSE_WORDS = {"false", "False", "FALSE", "no", "No", "NO", "off", "Off", "OFF"}
INT_RE = re.compile(r"^[-+]?(0|[1-9][0-9]*)$")
OCTAL_RE = re.compile(r"^0[0-7]+$")
FLOAT_RE = re.compile(r"^[-+]?([0-9]+\.[0-9]*|\.[0-9]+)$")
BLOCK_SCALAR_RE = re.compile(r"^([|>])([-+]?)$")
ESCAPES = {"n": "\n", "t": "\t", "r": "\r", '"': '"', "\\": "\\", "/": "/", "0": "\0"}


class MiniAnsibleError(Exception):
    pass


class YamlError(MiniAnsibleError):
    def __init__(self, line: int, message: str) -> None:
        super().__init__(f"line {line}: {message}")
        self.line = line


class TemplateError(MiniAnsibleError):
    pass


class InventoryError(MiniAnsibleError):
    pass


class PlaybookError(MiniAnsibleError):
    pass


class ModuleFailure(MiniAnsibleError):
    pass


class Unreachable(MiniAnsibleError):
    pass


@dataclass
class Line:
    number: int
    indent: int
    text: str


def strip_comment(text: str) -> str:
    quote = None
    for index, char in enumerate(text):
        if quote:
            if char == quote:
                quote = None
        elif char in "\"'":
            quote = char
        elif char == "#" and (index == 0 or text[index - 1] in " \t"):
            return text[:index].rstrip()
    return text.rstrip()


def split_lines(source: str) -> List[Line]:
    lines: List[Line] = []
    for number, raw in enumerate(source.splitlines(), start=1):
        if "\t" in raw[: len(raw) - len(raw.lstrip())]:
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
            if index + 1 >= len(text):
                raise YamlError(number, "unfinished escape sequence")
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
    if text in NULL_WORDS:
        return None
    if text in TRUE_WORDS:
        return True
    if text in FALSE_WORDS:
        return False
    if OCTAL_RE.match(text):
        return int(text, 8)
    if INT_RE.match(text):
        return int(text)
    if FLOAT_RE.match(text):
        return float(text)
    return text


def parse_flow(text: str, number: int) -> Tuple[Any, str]:
    text = text.lstrip()
    opener, closer = ("[", "]") if text.startswith("[") else ("{", "}")
    if not text.startswith(opener):
        raise YamlError(number, "not a flow collection")
    items: List[Any] = []
    mapping: Dict[str, Any] = {}
    rest = text[1:].lstrip()
    while True:
        if rest.startswith(closer):
            return (items if opener == "[" else mapping), rest[1:].lstrip()
        if not rest:
            raise YamlError(number, f"unterminated flow collection; missing {closer}")
        if opener == "[":
            value, rest = parse_flow_value(rest, number, ",]")
            items.append(value)
        else:
            key, rest = parse_flow_value(rest, number, ":,}")
            rest = rest.lstrip()
            value = None
            if rest.startswith(":"):
                value, rest = parse_flow_value(rest[1:].lstrip(), number, ",}")
            mapping[str(key)] = value
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
    if text.startswith(("&", "*")):
        raise YamlError(number, "anchors and aliases are not supported; repeat the value")
    if text.startswith("{{"):
        raise YamlError(number, f'a value that starts with {{{{ must be quoted: "{text}"')
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
            return key, text[index + 1 :].strip()
    return None


def is_item(text: str) -> bool:
    return text == "-" or text.startswith("- ")


def fold_lines(lines: List[str]) -> str:
    folded = ""
    for index, text in enumerate(lines):
        if index == 0:
            folded = text
        elif text == "":
            folded += "\n"
        elif lines[index - 1] == "":
            folded += text
        else:
            folded += " " + text
    return folded


class Parser:
    def __init__(self, source: str) -> None:
        self.source = source.splitlines()
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
            return parse_scalar(line.text, line.number)
        return self.parse_mapping(indent)

    def value(self, rest: str, line: Line, indent: int) -> Any:
        match = BLOCK_SCALAR_RE.match(rest)
        if match:
            return self.block_scalar(match.group(1), match.group(2), line, indent)
        return parse_scalar(rest, line.number)

    def block_scalar(self, style: str, chomp: str, line: Line, indent: int) -> str:
        if chomp == "+":
            raise YamlError(line.number, "keep chomping (|+) is not supported; use | or |-")
        collected: List[str] = []
        last = line.number
        for number in range(line.number + 1, len(self.source) + 1):
            raw = self.source[number - 1]
            if raw.strip() and len(raw) - len(raw.lstrip(" ")) <= indent:
                break
            collected.append(raw)
            last = number
        while collected and not collected[-1].strip():
            collected.pop()
        while self.position < len(self.lines) and self.lines[self.position].number <= last:
            self.position += 1
        if not collected:
            return ""
        first = next(raw for raw in collected if raw.strip())
        column = len(first) - len(first.lstrip(" "))
        texts: List[str] = []
        for offset, raw in enumerate(collected, start=line.number + 1):
            if not raw.strip():
                texts.append("")
                continue
            if len(raw) - len(raw.lstrip(" ")) < column:
                raise YamlError(offset, f"block scalar line is indented less than its first line ({column} spaces)")
            texts.append(raw[column:])
        body = "\n".join(texts) if style == "|" else fold_lines(texts)
        return body if chomp == "-" else body + "\n"

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
            if BLOCK_SCALAR_RE.match(rest):
                self.position += 1
                items.append(self.value(rest, line, indent))
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
            if rest:
                mapping[key] = self.value(rest, line, indent)
                continue
            nested = self.peek()
            if nested is None or nested.indent < indent:
                mapping[key] = None
            elif nested.indent == indent:
                mapping[key] = self.parse_sequence(indent, compact=True) if is_item(nested.text) else None
            else:
                mapping[key] = self.parse_block(nested.indent)


def parse_yaml(source: str) -> Any:
    return Parser(source).parse_document()


TOKEN_RE = re.compile(r"\s*(?:(\d+\.\d+|\d+)|('(?:[^'\\]|\\.)*'|\"(?:[^\"\\]|\\.)*\")|([A-Za-z_][A-Za-z0-9_]*)|(==|!=|<=|>=|[<>()\[\]{}.,|~+\-*/%:=]))")
COMPARISONS = ("==", "!=", "<", "<=", ">", ">=")
LITERALS = {"true": True, "True": True, "false": False, "False": False, "none": None, "None": None}


class Undefined:
    def __init__(self, message: str) -> None:
        self.message = message

    def __repr__(self) -> str:
        return f"Undefined({self.message!r})"


def strict(value: Any) -> Any:
    if isinstance(value, Undefined):
        raise TemplateError(value.message)
    return value


def unescape(text: str) -> str:
    return re.sub(r"\\(.)", lambda m: ESCAPES.get(m.group(1), m.group(1)), text)


def tokenize(source: str) -> List[Tuple[str, Any]]:
    tokens: List[Tuple[str, Any]] = []
    text = source.rstrip()
    position = 0
    while position < len(text):
        match = TOKEN_RE.match(text, position)
        if not match:
            raise TemplateError(f"unexpected character {text[position:].strip()[:1]!r} in expression {source.strip()!r}")
        number, string, name, op = match.groups()
        if number is not None:
            tokens.append(("num", float(number) if "." in number else int(number)))
        elif string is not None:
            tokens.append(("str", unescape(string[1:-1])))
        elif name is not None:
            tokens.append(("name", name))
        else:
            tokens.append(("op", op))
        position = match.end()
    tokens.append(("end", None))
    return tokens


class ExpressionParser:
    def __init__(self, source: str) -> None:
        self.source = source
        self.tokens = tokenize(source)
        self.position = 0

    def peek(self, offset: int = 0) -> Tuple[str, Any]:
        return self.tokens[min(self.position + offset, len(self.tokens) - 1)]

    def accept(self, kind: str, value: Any = None) -> bool:
        token = self.peek()
        if token[0] == kind and (value is None or token[1] == value):
            self.position += 1
            return True
        return False

    def expect(self, kind: str, value: Any = None) -> Tuple[str, Any]:
        token = self.peek()
        if token[0] != kind or (value is not None and token[1] != value):
            wanted = value if value is not None else kind
            found = token[1] if token[0] != "end" else "end of expression"
            raise TemplateError(f"expected {wanted!r} but found {found!r} in expression {self.source.strip()!r}")
        self.position += 1
        return token

    def parse(self) -> tuple:
        node = self.expression()
        if self.peek()[0] != "end":
            raise TemplateError(f"unexpected {self.peek()[1]!r} in expression {self.source.strip()!r}")
        return node

    def expression(self) -> tuple:
        node = self.or_expr()
        if self.accept("name", "if"):
            test = self.or_expr()
            other: tuple = ("lit", None)
            if self.accept("name", "else"):
                other = self.or_expr()
            node = ("cond", test, node, other)
        return node

    def or_expr(self) -> tuple:
        node = self.and_expr()
        while self.accept("name", "or"):
            node = ("or", node, self.and_expr())
        return node

    def and_expr(self) -> tuple:
        node = self.not_expr()
        while self.accept("name", "and"):
            node = ("and", node, self.not_expr())
        return node

    def not_expr(self) -> tuple:
        if self.accept("name", "not"):
            return ("not", self.not_expr())
        return self.comparison()

    def comparison(self) -> tuple:
        node = self.additive()
        while True:
            token = self.peek()
            if token[0] == "op" and token[1] in COMPARISONS:
                self.position += 1
                node = ("cmp", token[1], node, self.additive())
            elif token == ("name", "in"):
                self.position += 1
                node = ("in", node, self.additive(), False)
            elif token == ("name", "not") and self.peek(1) == ("name", "in"):
                self.position += 2
                node = ("in", node, self.additive(), True)
            else:
                return node

    def additive(self) -> tuple:
        node = self.concat()
        while self.peek()[0] == "op" and self.peek()[1] in ("+", "-"):
            op = self.expect("op")[1]
            node = ("arith", op, node, self.concat())
        return node

    def concat(self) -> tuple:
        node = self.unary()
        while self.accept("op", "~"):
            node = ("concat", node, self.unary())
        return node

    def unary(self) -> tuple:
        if self.accept("op", "-"):
            return ("neg", self.unary())
        return self.filtered()

    def filtered(self) -> tuple:
        node = self.postfix(self.primary())
        while True:
            if self.accept("op", "|"):
                name = self.expect("name")[1]
                args, kwargs = self.arguments() if self.peek() == ("op", "(") else ([], {})
                node = ("filter", node, name, args, kwargs)
            elif self.accept("name", "is"):
                negate = self.accept("name", "not")
                name = self.expect("name")[1]
                node = ("test", node, name, negate)
            else:
                return node

    def arguments(self) -> Tuple[List[tuple], Dict[str, tuple]]:
        self.expect("op", "(")
        args: List[tuple] = []
        kwargs: Dict[str, tuple] = {}
        while not self.accept("op", ")"):
            if self.peek()[0] == "name" and self.peek(1) == ("op", "="):
                name = self.expect("name")[1]
                self.expect("op", "=")
                kwargs[name] = self.expression()
            else:
                args.append(self.expression())
            if not self.accept("op", ","):
                self.expect("op", ")")
                break
        return args, kwargs

    def postfix(self, node: tuple) -> tuple:
        while True:
            if self.accept("op", "."):
                name = self.expect("name")[1]
                if self.peek() == ("op", "("):
                    args, _ = self.arguments()
                    node = ("call", node, name, args)
                else:
                    node = ("attr", node, name)
            elif self.accept("op", "["):
                key = self.expression()
                self.expect("op", "]")
                node = ("item", node, key)
            else:
                return node

    def primary(self) -> tuple:
        kind, value = self.peek()
        if kind in ("num", "str"):
            self.position += 1
            return ("lit", value)
        if kind == "name":
            self.position += 1
            if value in LITERALS:
                return ("lit", LITERALS[value])
            return ("name", value)
        if self.accept("op", "("):
            node = self.expression()
            self.expect("op", ")")
            return node
        if self.accept("op", "["):
            items: List[tuple] = []
            while not self.accept("op", "]"):
                items.append(self.expression())
                if not self.accept("op", ","):
                    self.expect("op", "]")
                    break
            return ("list", items)
        if self.accept("op", "{"):
            pairs: List[Tuple[tuple, tuple]] = []
            while not self.accept("op", "}"):
                key = self.expression()
                self.expect("op", ":")
                pairs.append((key, self.expression()))
                if not self.accept("op", ","):
                    self.expect("op", "}")
                    break
            return ("dict", pairs)
        found = value if kind != "end" else "end of expression"
        raise TemplateError(f"unexpected {found!r} in expression {self.source.strip()!r}")


def parse_expression(source: str) -> tuple:
    return ExpressionParser(source).parse()


def has_template(text: Any) -> bool:
    return isinstance(text, str) and ("{{" in text or "{%" in text)


def contains_template(value: Any) -> bool:
    if isinstance(value, dict):
        return any(contains_template(item) for item in value.values())
    if isinstance(value, list):
        return any(contains_template(item) for item in value)
    return has_template(value)


class Context:
    def __init__(self, variables: Dict[str, Any], parent: Optional["Context"] = None) -> None:
        self.variables = variables
        self.parent = parent
        self.state: Dict[str, Any] = parent.state if parent is not None else {"resolving": set(), "depth": 0}

    def child(self, variables: Dict[str, Any]) -> "Context":
        return Context(dict(variables), self)

    def lookup(self, name: str) -> Any:
        scope: Optional[Context] = self
        while scope is not None:
            if name in scope.variables:
                return self.finalize(scope.variables[name], name)
            scope = scope.parent
        return Undefined(f"'{name}' is undefined")

    def finalize(self, value: Any, name: str = "") -> Any:
        if not contains_template(value):
            return value
        if name and name in self.state["resolving"]:
            raise TemplateError(f"recursive loop detected in template: {name} refers to itself")
        if self.state["depth"] > 40:
            raise TemplateError("template nesting is deeper than 40 levels")
        if name:
            self.state["resolving"].add(name)
        self.state["depth"] += 1
        try:
            return template_value(value, self)
        finally:
            self.state["depth"] -= 1
            self.state["resolving"].discard(name)

    def flatten(self) -> Dict[str, Any]:
        merged = self.parent.flatten() if self.parent is not None else {}
        merged.update(self.variables)
        return merged


def type_name(value: Any) -> str:
    return type(value).__name__


def truth(value: Any) -> bool:
    return bool(strict(value))


def get_member(obj: Any, key: Any, ctx: Context) -> Any:
    if isinstance(obj, Undefined):
        return obj
    if isinstance(obj, dict):
        if key in obj:
            return ctx.finalize(obj[key])
        return Undefined(f"object of type 'dict' has no attribute {key!r}")
    if isinstance(obj, (list, tuple, str)) and isinstance(key, int) and not isinstance(key, bool):
        try:
            return ctx.finalize(obj[key])
        except IndexError:
            return Undefined(f"list object has no element {key}")
    return Undefined(f"object of type {type_name(obj)!r} has no attribute {key!r}")


def to_int(value: Any, default: int = 0) -> int:
    try:
        return int(float(value)) if isinstance(value, str) and "." in value else int(value)
    except (TypeError, ValueError):
        return default


def to_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    return str(value).strip().lower() in ("yes", "true", "1", "on", "y", "t")


def filter_default(value: Any, default: Any = "", boolean: bool = False) -> Any:
    if isinstance(value, Undefined) or (boolean and not value):
        return default
    return value


def filter_join(value: Any, separator: str = "") -> str:
    return separator.join(to_text(item) for item in value)


FILTERS: Dict[str, Callable[..., Any]] = {
    "default": filter_default,
    "d": filter_default,
    "upper": lambda value: to_text(value).upper(),
    "lower": lambda value: to_text(value).lower(),
    "trim": lambda value: to_text(value).strip(),
    "length": len,
    "count": len,
    "join": filter_join,
    "int": to_int,
    "bool": to_bool,
    "string": lambda value: to_text(value),
    "replace": lambda value, old, new: to_text(value).replace(old, new),
    "first": lambda value: value[0],
    "last": lambda value: value[-1],
    "sort": lambda value: sorted(value),
    "unique": lambda value: list(dict.fromkeys(value)),
    "basename": lambda value: posixpath.basename(to_text(value)),
    "dirname": lambda value: posixpath.dirname(to_text(value)),
    "quote": lambda value: shlex.quote(to_text(value)),
    "to_json": lambda value: json.dumps(value, sort_keys=True),
    "dict2items": lambda value: [{"key": key, "value": item} for key, item in value.items()],
    "regex_replace": lambda value, pattern, replacement="": re.sub(pattern, replacement, to_text(value)),
}

TESTS: Dict[str, Callable[[Any], bool]] = {
    "defined": lambda value: not isinstance(value, Undefined),
    "undefined": lambda value: isinstance(value, Undefined),
    "none": lambda value: value is None,
    "string": lambda value: isinstance(value, str),
    "number": lambda value: isinstance(value, (int, float)) and not isinstance(value, bool),
    "mapping": lambda value: isinstance(value, dict),
    "sequence": lambda value: isinstance(value, (list, tuple, str)),
}

METHODS = {
    dict: ("items", "keys", "values", "get"),
    str: ("upper", "lower", "strip", "startswith", "endswith", "split", "replace"),
}


def call_method(obj: Any, name: str, args: List[Any]) -> Any:
    for kind, names in METHODS.items():
        if isinstance(obj, kind) and name in names:
            result = getattr(obj, name)(*args)
            return [list(pair) for pair in result] if name == "items" else (list(result) if name in ("keys", "values", "split") else result)
    raise TemplateError(f"method {name}() on {type_name(obj)} is not supported by miniansible")


def evaluate(node: tuple, ctx: Context) -> Any:
    kind = node[0]
    try:
        if kind == "lit":
            return node[1]
        if kind == "name":
            return ctx.lookup(node[1])
        if kind == "attr":
            return get_member(evaluate(node[1], ctx), node[2], ctx)
        if kind == "item":
            return get_member(evaluate(node[1], ctx), strict(evaluate(node[2], ctx)), ctx)
        if kind == "call":
            return call_method(strict(evaluate(node[1], ctx)), node[2], [strict(evaluate(arg, ctx)) for arg in node[3]])
        if kind == "list":
            return [strict(evaluate(item, ctx)) for item in node[1]]
        if kind == "dict":
            return {strict(evaluate(key, ctx)): strict(evaluate(value, ctx)) for key, value in node[1]}
        if kind == "filter":
            _, inner, name, args, kwargs = node
            if name not in FILTERS:
                raise TemplateError(f"no filter named {name!r}; miniansible knows: {', '.join(sorted(FILTERS))}")
            value = evaluate(inner, ctx)
            if name not in ("default", "d"):
                value = strict(value)
            return FILTERS[name](value, *[strict(evaluate(a, ctx)) for a in args], **{k: strict(evaluate(v, ctx)) for k, v in kwargs.items()})
        if kind == "test":
            _, inner, name, negate = node
            if name not in TESTS:
                raise TemplateError(f"no test named {name!r}; miniansible knows: {', '.join(sorted(TESTS))}")
            outcome = TESTS[name](evaluate(inner, ctx))
            return not outcome if negate else outcome
        if kind == "not":
            return not truth(evaluate(node[1], ctx))
        if kind == "and":
            left = evaluate(node[1], ctx)
            return evaluate(node[2], ctx) if truth(left) else left
        if kind == "or":
            left = evaluate(node[1], ctx)
            return left if truth(left) else evaluate(node[2], ctx)
        if kind == "cmp":
            left, right = strict(evaluate(node[2], ctx)), strict(evaluate(node[3], ctx))
            op = node[1]
            return {"==": lambda: left == right, "!=": lambda: left != right, "<": lambda: left < right, "<=": lambda: left <= right, ">": lambda: left > right, ">=": lambda: left >= right}[op]()
        if kind == "in":
            needle, haystack = strict(evaluate(node[1], ctx)), strict(evaluate(node[2], ctx))
            found = needle in haystack
            return not found if node[3] else found
        if kind == "concat":
            return to_text(evaluate(node[1], ctx)) + to_text(evaluate(node[2], ctx))
        if kind == "arith":
            left, right = strict(evaluate(node[2], ctx)), strict(evaluate(node[3], ctx))
            return left + right if node[1] == "+" else left - right
        if kind == "neg":
            return -strict(evaluate(node[1], ctx))
        if kind == "cond":
            return evaluate(node[2], ctx) if truth(evaluate(node[1], ctx)) else evaluate(node[3], ctx)
    except (TypeError, KeyError, IndexError, ValueError, AttributeError) as exc:
        raise TemplateError(f"cannot evaluate {kind}: {exc}")
    raise TemplateError(f"unknown expression node {kind}")


def to_text(value: Any) -> str:
    value = strict(value)
    if value is None:
        return ""
    if isinstance(value, bool):
        return "True" if value else "False"
    return str(value)


TAG_START_RE = re.compile(r"\{\{|\{%|\{#")
CLOSERS = {"{{": "}}", "{%": "%}", "{#": "#}"}


def lex_template(source: str, trim_blocks: bool = True) -> List[Tuple[str, str, int]]:
    tokens: List[Tuple[str, str, int]] = []
    position = 0
    pending = ""
    while True:
        match = TAG_START_RE.search(source, position)
        text = source[position : match.start() if match else len(source)]
        if pending == "strip":
            text = text.lstrip()
        elif pending == "newline" and text.startswith("\n"):
            text = text[1:]
        if match is None:
            tokens.append(("text", text, 0))
            return tokens
        opener = match.group(0)
        start = match.end()
        line = source.count("\n", 0, match.start()) + 1
        if source.startswith("-", start):
            start += 1
            text = text.rstrip()
        tokens.append(("text", text, line))
        end = source.find(CLOSERS[opener], start)
        if end < 0:
            raise TemplateError(f"line {line}: {opener} is never closed with {CLOSERS[opener]}")
        inner = source[start:end]
        pending = "strip" if inner.endswith("-") else ("newline" if trim_blocks and opener != "{{" else "")
        inner = inner[:-1] if inner.endswith("-") else inner
        if opener == "{{":
            tokens.append(("var", inner.strip(), line))
        elif opener == "{%":
            tokens.append(("block", inner.strip(), line))
        position = end + 2


def parse_template_nodes(tokens: List[Tuple[str, str, int]], index: int, stop: Tuple[str, ...]) -> Tuple[List[tuple], int, str, str]:
    nodes: List[tuple] = []
    while index < len(tokens):
        kind, value, line = tokens[index]
        index += 1
        if kind == "text":
            if value:
                nodes.append(("text", value))
            continue
        if kind == "var":
            nodes.append(("out", parse_expression(value), line))
            continue
        word, _, rest = value.partition(" ")
        if word in stop:
            return nodes, index, word, rest.strip()
        if word == "if":
            branches: List[Tuple[tuple, List[tuple]]] = []
            condition = rest
            while True:
                body, index, ended, remainder = parse_template_nodes(tokens, index, ("elif", "else", "endif"))
                branches.append((parse_expression(condition), body))
                if ended == "elif":
                    condition = remainder
                    continue
                otherwise: List[tuple] = []
                if ended == "else":
                    otherwise, index, ended, _ = parse_template_nodes(tokens, index, ("endif",))
                if ended != "endif":
                    raise TemplateError(f"line {line}: {{% if %}} is never closed with {{% endif %}}")
                nodes.append(("if", branches, otherwise))
                break
        elif word == "for":
            match = re.match(r"^([A-Za-z_]\w*(?:\s*,\s*[A-Za-z_]\w*)*)\s+in\s+(.+)$", rest)
            if not match:
                raise TemplateError(f"line {line}: expected {{% for NAME in EXPRESSION %}}, got {{% {value} %}}")
            names = [name.strip() for name in match.group(1).split(",")]
            body, index, ended, _ = parse_template_nodes(tokens, index, ("else", "endfor"))
            otherwise = []
            if ended == "else":
                otherwise, index, ended, _ = parse_template_nodes(tokens, index, ("endfor",))
            if ended != "endfor":
                raise TemplateError(f"line {line}: {{% for %}} is never closed with {{% endfor %}}")
            nodes.append(("for", names, parse_expression(match.group(2)), body, otherwise))
        elif word == "set":
            name, sep, expression = rest.partition("=")
            if not sep or not re.match(r"^[A-Za-z_]\w*$", name.strip()):
                raise TemplateError(f"line {line}: expected {{% set NAME = EXPRESSION %}}")
            nodes.append(("set", name.strip(), parse_expression(expression)))
        else:
            raise TemplateError(f"line {line}: unknown tag {word!r}; miniansible supports if, elif, else, endif, for, endfor and set")
    return nodes, index, "", ""


def render_nodes(nodes: List[tuple], ctx: Context, out: List[str]) -> None:
    for node in nodes:
        kind = node[0]
        if kind == "text":
            out.append(node[1])
        elif kind == "out":
            try:
                out.append(to_text(evaluate(node[1], ctx)))
            except TemplateError as exc:
                raise TemplateError(f"line {node[2]}: {exc}")
        elif kind == "if":
            for condition, body in node[1]:
                if truth(evaluate(condition, ctx)):
                    render_nodes(body, ctx, out)
                    break
            else:
                render_nodes(node[2], ctx, out)
        elif kind == "for":
            _, names, iterable, body, otherwise = node
            items = strict(evaluate(iterable, ctx))
            sequence = list(items.keys()) if isinstance(items, dict) else list(items)
            if not sequence:
                render_nodes(otherwise, ctx, out)
            for index, item in enumerate(sequence):
                loop = {"index": index + 1, "index0": index, "first": index == 0, "last": index == len(sequence) - 1, "length": len(sequence), "revindex": len(sequence) - index}
                scope: Dict[str, Any] = {"loop": loop}
                if len(names) == 1:
                    scope[names[0]] = item
                else:
                    if len(item) != len(names):
                        raise TemplateError(f"cannot unpack {item!r} into {', '.join(names)}")
                    scope.update(zip(names, item))
                render_nodes(body, ctx.child(scope), out)
        elif kind == "set":
            ctx.variables[node[1]] = strict(evaluate(node[2], ctx))


def trailing_newlines(text: str) -> int:
    return len(text) - len(text.rstrip("\n"))


def render_template(source: str, ctx: Context) -> str:
    body = source[:-1] if source.endswith("\n") else source
    nodes = parse_template_nodes(lex_template(body), 0, ())[0]
    out: List[str] = []
    render_nodes(nodes, ctx.child({}), out)
    result = "".join(out)
    missing = trailing_newlines(source) - trailing_newlines(result)
    return result + "\n" * missing if missing > 0 else result


SINGLE_EXPRESSION_RE = re.compile(r"^\s*\{\{(.*?)\}\}\s*$", re.S)


def template_value(value: Any, ctx: Context) -> Any:
    if isinstance(value, dict):
        return {key: template_value(item, ctx) for key, item in value.items()}
    if isinstance(value, list):
        return [template_value(item, ctx) for item in value]
    if not has_template(value):
        return value
    match = SINGLE_EXPRESSION_RE.match(value)
    if match and "{{" not in match.group(1) and "}}" not in match.group(1) and "{%" not in value:
        return strict(evaluate(parse_expression(match.group(1)), ctx))
    return render_template(value, ctx)


def evaluate_condition(expression: Any, ctx: Context) -> bool:
    if isinstance(expression, bool):
        return expression
    text = str(expression).strip()
    match = SINGLE_EXPRESSION_RE.match(text)
    if match:
        text = match.group(1)
    value = strict(evaluate(parse_expression(text), ctx))
    if not isinstance(value, bool):
        raise TemplateError(f"Conditional result ({bool(value)}) was derived from value of type {type_name(value)!r}. Conditionals must have a boolean result.")
    return value


SECTION_RE = re.compile(r"^\[([^\]:\s]+)(?::(\w+))?\]\s*(?:[#;].*)?$")
RANGE_RE = re.compile(r"^(.*?)\[([0-9a-zA-Z]+):([0-9a-zA-Z]+)(?::([0-9]+))?\](.*)$")
HOST_PORT_RE = re.compile(r"^(.*?)(?::([0-9]+))?$")
GROUP_KEYS = {"hosts", "vars", "children"}


@dataclass
class Group:
    name: str
    hosts: List[str] = field(default_factory=list)
    children: List[str] = field(default_factory=list)
    vars: Dict[str, Any] = field(default_factory=dict)
    priority: int = 1


def expand_hosts(pattern: str) -> List[str]:
    match = RANGE_RE.match(pattern)
    if not match:
        return [pattern]
    head, start, end, step, tail = match.groups()
    stride = int(step) if step else 1
    if start.isdigit() and end.isdigit():
        width = len(start) if start.startswith("0") else 0
        values = [str(n).zfill(width) for n in range(int(start), int(end) + 1, stride)]
    elif len(start) == 1 and len(end) == 1 and start.isalpha() and end.isalpha():
        values = [chr(n) for n in range(ord(start), ord(end) + 1, stride)]
    else:
        raise InventoryError(f"invalid host range [{start}:{end}] in {pattern!r}")
    if not values:
        raise InventoryError(f"empty host range [{start}:{end}] in {pattern!r}")
    return [name for value in values for name in expand_hosts(head + value + tail)]


def ini_value(text: str) -> Any:
    try:
        return ast.literal_eval(text)
    except (ValueError, SyntaxError):
        return text


class Inventory:
    def __init__(self) -> None:
        self.groups: Dict[str, Group] = {"all": Group("all"), "ungrouped": Group("ungrouped")}
        self.groups["all"].children.append("ungrouped")
        self.hosts: Dict[str, Dict[str, Any]] = {}

    def add_group(self, name: str) -> Group:
        if name not in self.groups:
            self.groups[name] = Group(name)
        return self.groups[name]

    def add_child(self, parent: str, child: str) -> None:
        if parent == child:
            raise InventoryError(f"group {parent!r} cannot be a child of itself")
        group = self.add_group(parent)
        self.add_group(child)
        if child in self.ancestors(parent):
            raise InventoryError(f"adding {child!r} as a child of {parent!r} would create a cycle")
        if child not in group.children:
            group.children.append(child)

    def add_host(self, name: str, group: str, variables: Optional[Dict[str, Any]] = None) -> None:
        self.hosts.setdefault(name, {}).update(variables or {})
        members = self.add_group(group).hosts
        if name not in members:
            members.append(name)

    def set_group_var(self, group: str, key: str, value: Any) -> None:
        target = self.add_group(group)
        if key == "ansible_group_priority":
            target.priority = to_int(value, 1)
        else:
            target.vars[key] = value

    def parents(self, name: str) -> List[str]:
        return [group.name for group in self.groups.values() if name in group.children]

    def ancestors(self, name: str) -> List[str]:
        found: List[str] = []
        pending = self.parents(name)
        while pending:
            current = pending.pop()
            if current not in found:
                found.append(current)
                pending.extend(self.parents(current))
        return found

    def reconcile(self) -> "Inventory":
        for group in list(self.groups.values()):
            if group.name != "all" and not self.parents(group.name):
                self.groups["all"].children.append(group.name)
        ungrouped = self.groups["ungrouped"]
        for host in self.hosts:
            direct = [g.name for g in self.groups.values() if host in g.hosts and g.name not in ("all", "ungrouped")]
            if direct and host in ungrouped.hosts:
                ungrouped.hosts.remove(host)
            elif not direct and host not in ungrouped.hosts:
                ungrouped.hosts.append(host)
            if host in self.groups["all"].hosts:
                self.groups["all"].hosts.remove(host)
        return self

    def depths(self) -> Dict[str, int]:
        depth = {"all": 0}
        changed = True
        while changed:
            changed = False
            for group in self.groups.values():
                if group.name not in depth:
                    continue
                for child in group.children:
                    if depth.get(child, -1) < depth[group.name] + 1:
                        depth[child] = depth[group.name] + 1
                        changed = True
        return depth

    def groups_of(self, host: str) -> List[str]:
        direct = [g.name for g in self.groups.values() if host in g.hosts]
        found = set(direct) | {"all"}
        for name in direct:
            found.update(self.ancestors(name))
        return sorted(found)

    def hosts_in(self, name: str) -> List[str]:
        if name == "all":
            return list(self.hosts)
        ordered: List[str] = []
        pending = [name]
        seen: set = set()
        while pending:
            current = pending.pop(0)
            if current in seen or current not in self.groups:
                continue
            seen.add(current)
            ordered.extend(h for h in self.groups[current].hosts if h not in ordered)
            pending.extend(self.groups[current].children)
        return [h for h in self.hosts if h in ordered]

    def var_layers(self, host: str) -> List[Tuple[str, Dict[str, Any]]]:
        depth = self.depths()
        names = sorted(self.groups_of(host), key=lambda g: (depth.get(g, 0), self.groups[g].priority, g))
        layers = [(f"group_vars ({name})", self.groups[name].vars) for name in names]
        layers.append(("host vars", self.hosts.get(host, {})))
        return layers

    def host_vars(self, host: str) -> Dict[str, Any]:
        merged: Dict[str, Any] = {}
        for _, layer in self.var_layers(host):
            merged.update(layer)
        return merged

    def match(self, pattern: str) -> List[str]:
        selected: List[str] = []
        for index, part in enumerate(p for p in re.split(r"[:,]", pattern) if p):
            if part.startswith("&"):
                keep = set(self.match_one(part[1:]))
                selected = [h for h in selected if h in keep]
            elif part.startswith("!"):
                drop = set(self.match_one(part[1:]))
                selected = [h for h in selected if h not in drop]
            else:
                selected += [h for h in self.match_one(part) if h not in selected]
        return selected

    def match_one(self, part: str) -> List[str]:
        if part in ("all", "*"):
            return list(self.hosts)
        if part in self.groups:
            return self.hosts_in(part)
        if part in self.hosts:
            return [part]
        if any(c in part for c in "*?["):
            names = [h for h in self.hosts if fnmatch.fnmatchcase(h, part)]
            for group in self.groups:
                if fnmatch.fnmatchcase(group, part):
                    names += [h for h in self.hosts_in(group) if h not in names]
            return names
        return []

    def graph(self, name: str = "all") -> str:
        if name not in self.groups:
            raise InventoryError(f"pattern must be a valid group name when using --graph, got {name!r}")
        lines: List[str] = []

        def walk(group: str, depth: int) -> None:
            prefix = "  |" * depth + "--" if depth else ""
            lines.append(f"{prefix}@{group}:")
            for child in self.groups[group].children:
                walk(child, depth + 1)
            if group != "all":
                for host in self.groups[group].hosts:
                    lines.append("  |" * (depth + 1) + "--" + host)

        walk(name, 0)
        return "\n".join(lines)

    def to_list(self) -> Dict[str, Any]:
        result: Dict[str, Any] = {}
        for name, group in self.groups.items():
            entry: Dict[str, Any] = {}
            if name != "all" and group.hosts:
                entry["hosts"] = list(group.hosts)
            if group.children:
                entry["children"] = list(group.children)
            if entry:
                result[name] = entry
        result["_meta"] = {"hostvars": {host: self.host_vars(host) for host in self.hosts if self.host_vars(host)}}
        return result


def parse_ini_inventory(text: str) -> Inventory:
    inventory = Inventory()
    group, state = "ungrouped", "hosts"
    declared = {"all", "ungrouped"}
    pending_children: List[Tuple[int, str, str]] = []
    pending_vars: Dict[str, int] = {}
    for number, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line or line[0] in "#;":
            continue
        section = SECTION_RE.match(line)
        if section:
            group, state = section.group(1), section.group(2) or "hosts"
            if state not in ("hosts", "children", "vars"):
                raise InventoryError(f"line {number}: section [{group}:{state}] has unknown type {state!r}; use hosts, children or vars")
            if state == "vars":
                pending_vars.setdefault(group, number)
            else:
                declared.add(group)
            inventory.add_group(group)
            continue
        if line.startswith("["):
            raise InventoryError(f"line {number}: invalid section {line!r}; no spaces inside the brackets")
        if state == "hosts":
            try:
                tokens = shlex.split(line, comments=True)
            except ValueError as exc:
                raise InventoryError(f"line {number}: cannot parse host line: {exc}")
            name, port = HOST_PORT_RE.match(tokens[0]).groups()
            variables: Dict[str, Any] = {}
            for token in tokens[1:]:
                if "=" not in token:
                    raise InventoryError(f"line {number}: expected key=value host variable, got {token!r}")
                key, value = token.split("=", 1)
                variables[key] = ini_value(value)
            if port:
                variables = {"ansible_port": int(port), **variables}
            for host in expand_hosts(name):
                inventory.add_host(host, group, variables)
        elif state == "vars":
            if "=" not in line:
                raise InventoryError(f"line {number}: expected key=value in [{group}:vars], got {line!r}")
            key, value = (part.strip() for part in line.split("=", 1))
            inventory.set_group_var(group, key, ini_value(value))
        else:
            child = shlex.split(line, comments=True)[0]
            pending_children.append((number, group, child))
    for number, parent, child in pending_children:
        if child not in declared:
            raise InventoryError(f"line {number}: [{parent}:children] includes undefined group {child!r}")
        inventory.add_child(parent, child)
    for name, number in pending_vars.items():
        if name not in declared and name not in {child for _, _, child in pending_children}:
            raise InventoryError(f"line {number}: [{name}:vars] is not valid for undefined group {name!r}")
    return inventory.reconcile()


def load_yaml_group(inventory: Inventory, name: str, body: Any, parent: Optional[str]) -> None:
    inventory.add_group(name)
    if parent is not None:
        inventory.add_child(parent, name)
    if body is None:
        return
    if not isinstance(body, dict):
        raise InventoryError(f"group {name!r}: expected a mapping with hosts:, vars: or children:, got {type_name(body)}")
    unknown = sorted(set(body) - GROUP_KEYS)
    if unknown:
        raise InventoryError(f"group {name!r}: unknown key(s) {', '.join(unknown)}; a group has only hosts:, vars: and children:")
    for pattern, variables in (body.get("hosts") or {}).items():
        if variables is not None and not isinstance(variables, dict):
            raise InventoryError(f"host {pattern!r} in group {name!r}: host variables must be a mapping")
        for host in expand_hosts(str(pattern)):
            inventory.add_host(host, name, dict(variables or {}))
    for key, value in (body.get("vars") or {}).items():
        inventory.set_group_var(name, key, value)
    for child, child_body in (body.get("children") or {}).items():
        load_yaml_group(inventory, str(child), child_body, name)


def parse_yaml_inventory(text: str) -> Inventory:
    data = parse_yaml(text)
    if not isinstance(data, dict):
        raise InventoryError("a YAML inventory is a mapping of group names, usually starting with all:")
    inventory = Inventory()
    for name, body in data.items():
        load_yaml_group(inventory, str(name), body, None)
    return inventory.reconcile()


def load_inventory(path: Optional[str]) -> Inventory:
    if not path:
        return Inventory().reconcile()
    if "," in path and not os.path.exists(path):
        inventory = Inventory()
        for host in (h.strip() for h in path.split(",") if h.strip()):
            inventory.add_host(host, "ungrouped")
        return inventory.reconcile()
    try:
        with open(path, encoding="utf-8") as handle:
            text = handle.read()
    except OSError as exc:
        raise InventoryError(f"cannot read inventory {path}: {exc.strerror}")
    try:
        if path.endswith((".yml", ".yaml", ".json")):
            return parse_yaml_inventory(text) if not path.endswith(".json") else load_json_inventory(text)
        return parse_ini_inventory(text)
    except YamlError as exc:
        raise InventoryError(f"{path}:{exc.line}: {str(exc).split(': ', 1)[1]}")
    except InventoryError as exc:
        raise InventoryError(f"{path}: {exc}")


def load_json_inventory(text: str) -> Inventory:
    data = json.loads(text)
    inventory = Inventory()
    for name, body in data.items():
        load_yaml_group(inventory, str(name), body, None)
    return inventory.reconcile()


DISTRIBUTIONS = {
    "ubuntu": "Ubuntu",
    "debian": "Debian",
    "alpine": "Alpine",
    "fedora": "Fedora",
    "rhel": "RedHat",
    "centos": "CentOS",
    "rocky": "Rocky",
    "almalinux": "AlmaLinux",
    "amzn": "Amazon",
    "arch": "Archlinux",
    "opensuse-leap": "openSUSE Leap",
    "sles": "SLES",
}
OS_FAMILIES = {
    "Ubuntu": "Debian",
    "Debian": "Debian",
    "Alpine": "Alpine",
    "Fedora": "RedHat",
    "RedHat": "RedHat",
    "CentOS": "RedHat",
    "Rocky": "RedHat",
    "AlmaLinux": "RedHat",
    "Amazon": "RedHat",
    "Archlinux": "Archlinux",
    "openSUSE Leap": "Suse",
    "SLES": "Suse",
    "MacOSX": "Darwin",
}


@dataclass
class CommandResult:
    rc: int
    stdout: str = ""
    stderr: str = ""


@dataclass
class FileStat:
    kind: str
    mode: int


Runner = Callable[[Sequence[str], Optional[str]], CommandResult]


def subprocess_runner(argv: Sequence[str], stdin: Optional[str] = None) -> CommandResult:
    try:
        completed = subprocess.run(
            list(argv),
            input=None if stdin is None else stdin.encode("utf-8", "surrogateescape"),
            stdin=subprocess.DEVNULL if stdin is None else None,
            capture_output=True,
            check=False,
        )
    except FileNotFoundError as exc:
        return CommandResult(2, "", f"[Errno 2] No such file or directory: {exc.filename!r}")
    return CommandResult(completed.returncode, completed.stdout.decode("utf-8", "surrogateescape"), completed.stderr.decode("utf-8", "surrogateescape"))


class Connection:
    name = "base"

    def stat(self, path: str) -> Optional[FileStat]:
        raise NotImplementedError

    def read(self, path: str) -> str:
        raise NotImplementedError

    def write(self, path: str, text: str) -> None:
        raise NotImplementedError

    def mkdir(self, path: str) -> None:
        raise NotImplementedError

    def chmod(self, path: str, mode: int) -> None:
        raise NotImplementedError

    def remove(self, path: str) -> None:
        raise NotImplementedError

    def run(self, argv: Sequence[str], chdir: Optional[str] = None) -> CommandResult:
        raise NotImplementedError

    def exists(self, pattern: str) -> bool:
        raise NotImplementedError


class LocalConnection(Connection):
    name = "local"

    def stat(self, path: str) -> Optional[FileStat]:
        try:
            st = os.stat(path)
        except (FileNotFoundError, NotADirectoryError):
            return None
        kind = "directory" if statlib.S_ISDIR(st.st_mode) else "file" if statlib.S_ISREG(st.st_mode) else "other"
        return FileStat(kind, statlib.S_IMODE(st.st_mode))

    def read(self, path: str) -> str:
        with open(path, encoding="utf-8", errors="surrogateescape", newline="") as handle:
            return handle.read()

    def write(self, path: str, text: str) -> None:
        with open(path, "w", encoding="utf-8", errors="surrogateescape", newline="") as handle:
            handle.write(text)

    def mkdir(self, path: str) -> None:
        os.mkdir(path)

    def chmod(self, path: str, mode: int) -> None:
        os.chmod(path, mode)

    def remove(self, path: str) -> None:
        if os.path.isdir(path) and not os.path.islink(path):
            shutil.rmtree(path)
        else:
            os.remove(path)

    def run(self, argv: Sequence[str], chdir: Optional[str] = None) -> CommandResult:
        try:
            completed = subprocess.run(list(argv), cwd=chdir, stdin=subprocess.DEVNULL, capture_output=True, check=False)
        except FileNotFoundError as exc:
            return CommandResult(2, "", f"[Errno 2] No such file or directory: {exc.filename!r}")
        return CommandResult(completed.returncode, completed.stdout.decode("utf-8", "surrogateescape"), completed.stderr.decode("utf-8", "surrogateescape"))

    def exists(self, pattern: str) -> bool:
        return bool(globlib.glob(pattern))


STAT_SCRIPT = 'if [ -d "$1" ]; then t=directory; elif [ -f "$1" ]; then t=file; elif [ -e "$1" ]; then t=other; else exit 3; fi; printf "%s %s\\n" "$t" "$(stat -c %a "$1")"'
EXISTS_SCRIPT = 'IFS=; for f in $1; do [ -e "$f" ] && exit 0; done; exit 1'


class DockerConnection(Connection):
    name = "docker"

    def __init__(self, container: str, runner: Runner = subprocess_runner, docker: str = "docker") -> None:
        self.container = container
        self.runner = runner
        self.docker = docker

    def exec_argv(self, argv: Sequence[str], interactive: bool = False, chdir: Optional[str] = None) -> List[str]:
        args = [self.docker, "exec"]
        if interactive:
            args.append("-i")
        if chdir:
            args += ["-w", chdir]
        return args + [self.container] + list(argv)

    def call(self, argv: Sequence[str], stdin: Optional[str] = None, chdir: Optional[str] = None) -> CommandResult:
        result = self.runner(self.exec_argv(argv, stdin is not None, chdir), stdin)
        if result.rc != 0 and ("No such container" in result.stderr or "is not running" in result.stderr):
            raise Unreachable(f"container {self.container!r} is not reachable: {result.stderr.strip()}")
        return result

    def sh(self, script: str, *args: str, stdin: Optional[str] = None) -> CommandResult:
        return self.call(["sh", "-c", script, "sh", *args], stdin)

    def checked(self, result: CommandResult, action: str, path: str) -> None:
        if result.rc != 0:
            raise ModuleFailure(f"{action} {path} in container {self.container}: {result.stderr.strip() or 'exit ' + str(result.rc)}")

    def stat(self, path: str) -> Optional[FileStat]:
        result = self.sh(STAT_SCRIPT, path)
        if result.rc == 3:
            return None
        self.checked(result, "stat", path)
        kind, _, mode = result.stdout.strip().partition(" ")
        return FileStat(kind, int(mode, 8))

    def read(self, path: str) -> str:
        result = self.sh('cat -- "$1"', path)
        if result.rc != 0:
            raise FileNotFoundError(f"{path}: {result.stderr.strip()}")
        return result.stdout

    def write(self, path: str, text: str) -> None:
        self.checked(self.sh('cat > "$1"', path, stdin=text), "write", path)

    def mkdir(self, path: str) -> None:
        self.checked(self.sh('mkdir -- "$1"', path), "mkdir", path)

    def chmod(self, path: str, mode: int) -> None:
        self.checked(self.sh('chmod "$1" "$2"', format(mode, "o"), path), "chmod", path)

    def remove(self, path: str) -> None:
        self.checked(self.sh('rm -rf -- "$1"', path), "remove", path)

    def run(self, argv: Sequence[str], chdir: Optional[str] = None) -> CommandResult:
        return self.call(argv, chdir=chdir)

    def exists(self, pattern: str) -> bool:
        return self.sh(EXISTS_SCRIPT, pattern).rc == 0


UBUNTU_OS_RELEASE = 'NAME="Ubuntu"\nVERSION_ID="24.04"\nID=ubuntu\nID_LIKE=debian\n'


class FakeConnection(Connection):
    name = "fake"

    def __init__(self, files: Optional[Dict[str, str]] = None, hostname: str = "fakehost", commands: Optional[Dict[str, CommandResult]] = None) -> None:
        self.files: Dict[str, str] = {"/etc/os-release": UBUNTU_OS_RELEASE}
        self.files.update(files or {})
        self.dirs: set = {"/", "/etc", "/tmp"}
        for path in self.files:
            parent = posixpath.dirname(path)
            while parent not in self.dirs:
                self.dirs.add(parent)
                parent = posixpath.dirname(parent)
        self.modes: Dict[str, int] = {}
        self.hostname = hostname
        self.commands = dict(commands or {})
        self.ran: List[List[str]] = []
        self.writes: List[str] = []

    def require_parent(self, path: str) -> None:
        parent = posixpath.dirname(path) or "/"
        if parent not in self.dirs:
            raise FileNotFoundError(f"[Errno 2] No such file or directory: {parent!r}")

    def stat(self, path: str) -> Optional[FileStat]:
        if path in self.dirs:
            return FileStat("directory", self.modes.get(path, 0o755))
        if path in self.files:
            return FileStat("file", self.modes.get(path, 0o644))
        return None

    def read(self, path: str) -> str:
        if path not in self.files:
            raise FileNotFoundError(path)
        return self.files[path]

    def write(self, path: str, text: str) -> None:
        self.require_parent(path)
        self.files[path] = text
        self.writes.append(path)

    def mkdir(self, path: str) -> None:
        self.require_parent(path)
        if path in self.dirs or path in self.files:
            raise FileExistsError(path)
        self.dirs.add(path)

    def chmod(self, path: str, mode: int) -> None:
        if self.stat(path) is None:
            raise FileNotFoundError(path)
        self.modes[path] = mode

    def remove(self, path: str) -> None:
        prefix = path.rstrip("/") + "/"
        for name in [p for p in self.files if p == path or p.startswith(prefix)]:
            del self.files[name]
        self.dirs = {d for d in self.dirs if d != path and not d.startswith(prefix)}

    def run(self, argv: Sequence[str], chdir: Optional[str] = None) -> CommandResult:
        self.ran.append(list(argv))
        if list(argv[:1]) == ["uname"]:
            fields = {"-s": "Linux", "-n": self.hostname, "-r": "6.8.0", "-m": "x86_64"}
            return CommandResult(0, " ".join(fields[flag] for flag in argv[1:] if flag in fields) + "\n")
        key = " ".join(argv)
        if list(argv[:1]) == ["touch"] and not any(fnmatch.fnmatchcase(key, p) for p in self.commands):
            for path in argv[1:]:
                self.require_parent(path)
                self.files.setdefault(path, "")
            return CommandResult(0)
        for pattern, result in self.commands.items():
            if fnmatch.fnmatchcase(key, pattern):
                return result
        return CommandResult(0, "", "")

    def exists(self, pattern: str) -> bool:
        return any(fnmatch.fnmatchcase(path, pattern) for path in list(self.files) + list(self.dirs))


def connect(host: str, variables: Dict[str, Any], default: Optional[str] = None, runner: Runner = subprocess_runner) -> Connection:
    kind = str(variables.get("ansible_connection") or default or ("local" if host in ("localhost", "127.0.0.1") else "ssh"))
    if kind in ("local", "ansible.builtin.local"):
        return LocalConnection()
    if kind in ("docker", "community.docker.docker"):
        return DockerConnection(str(variables.get("ansible_docker_host") or variables.get("ansible_host") or host), runner)
    if kind == "fake":
        return FakeConnection(hostname=host)
    raise Unreachable(f"connection {kind!r} is not implemented in miniansible (it has local, docker and fake); set ansible_connection or pass -c")


def parse_os_release(text: str) -> Dict[str, str]:
    values: Dict[str, str] = {}
    for line in text.splitlines():
        key, sep, value = line.strip().partition("=")
        if sep and key and not key.startswith("#"):
            values[key] = value.strip().strip('"').strip("'")
    return values


def gather_facts(conn: Connection) -> Dict[str, Any]:
    uname = conn.run(["uname", "-s", "-n", "-r", "-m"])
    if uname.rc != 0:
        raise ModuleFailure(f"uname failed on the target: {uname.stderr.strip()}")
    system, nodename, kernel, machine = (uname.stdout.split() + ["", "", "", ""])[:4]
    release = parse_os_release(conn.read("/etc/os-release")) if conn.stat("/etc/os-release") else {}
    if system == "Darwin":
        distribution = "MacOSX"
    else:
        distribution = DISTRIBUTIONS.get(release.get("ID", ""), release.get("NAME", system))
    version = release.get("VERSION_ID", "")
    return {
        "hostname": nodename.split(".")[0],
        "nodename": nodename,
        "system": system,
        "kernel": kernel,
        "architecture": machine,
        "distribution": distribution,
        "distribution_version": version,
        "distribution_major_version": version.split(".")[0] if version else "",
        "os_family": OS_FAMILIES.get(distribution, distribution),
    }


@dataclass
class Result:
    changed: bool = False
    failed: bool = False
    skipped: bool = False
    unreachable: bool = False
    msg: str = ""
    diffs: List[Dict[str, str]] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    data: Dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> Dict[str, Any]:
        result = dict(self.data)
        result["changed"] = self.changed
        result["failed"] = self.failed
        if self.skipped:
            result["skipped"] = True
        if self.unreachable:
            result["unreachable"] = True
        if self.msg:
            result["msg"] = self.msg
        return result


@dataclass
class ModuleContext:
    conn: Connection
    check: bool = False
    diff: bool = False
    playbook_dir: str = "."
    variables: Context = field(default_factory=lambda: Context({}))


@dataclass
class Module:
    run: Callable[[ModuleContext, Dict[str, Any]], Result]
    params: Tuple[str, ...]
    aliases: Dict[str, str] = field(default_factory=dict)
    free_form: bool = False


def require(args: Dict[str, Any], name: str, module: str) -> Any:
    value = args.get(name)
    if value is None or value == "":
        raise ModuleFailure(f"missing required arguments: {name} (module {module})")
    return value


def parse_mode(value: Any, warnings: List[str]) -> Optional[int]:
    if value is None:
        return None
    if isinstance(value, bool):
        raise ModuleFailure(f"mode must be an octal string such as '0644', got {value!r}")
    if isinstance(value, int):
        if value > 0o777 and re.fullmatch(r"[0-7]{3,4}", str(value)):
            warnings.append(f"mode {value} is the decimal number {value} (octal {value:o}); YAML read an unquoted 644 as a number. Quote it: mode: '0{value}'")
        return value
    text = str(value).strip()
    if re.fullmatch(r"0?[0-7]{3,4}", text):
        return int(text, 8)
    raise ModuleFailure(f"mode {value!r} is not an octal mode such as '0644' (symbolic modes like u=rw are not implemented in miniansible)")


def sha1(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8", "surrogateescape")).hexdigest()


def content_diff(before: str, after: str, path: str) -> Dict[str, str]:
    return {"before": before, "after": after, "before_header": path, "after_header": path}


def render_diff(diff: Dict[str, str]) -> str:
    lines = difflib.unified_diff(
        diff["before"].splitlines(True),
        diff["after"].splitlines(True),
        fromfile=f"before: {diff['before_header']}",
        tofile=f"after: {diff['after_header']}",
    )
    return "".join(line if line.endswith("\n") else line + "\n\\ No newline at end of file\n" for line in lines)


def apply_mode(ctx: ModuleContext, path: str, mode: Optional[int], current: Optional[FileStat]) -> bool:
    if mode is None or (current is not None and current.mode == mode):
        return False
    if not ctx.check:
        ctx.conn.chmod(path, mode)
    return True


def ensure_content(ctx: ModuleContext, dest: str, content: str, mode: Optional[int], source: str) -> Result:
    current = ctx.conn.stat(dest)
    if current is not None and current.kind == "directory":
        raise ModuleFailure(f"dest {dest} is a directory; give the full path of the file")
    parent = posixpath.dirname(dest)
    if parent and ctx.conn.stat(parent) is None:
        raise ModuleFailure(f"Destination directory {parent} does not exist")
    before = ctx.conn.read(dest) if current is not None else ""
    checksum = sha1(content)
    result = Result(data={"dest": dest, "checksum": checksum})
    if current is None or sha1(before) != checksum:
        result.changed = True
        result.diffs.append({"before": before, "after": content, "before_header": dest, "after_header": source})
        if not ctx.check:
            ctx.conn.write(dest, content)
    written = current if current is not None or ctx.check else ctx.conn.stat(dest)
    if apply_mode(ctx, dest, mode, written):
        result.changed = True
    return result


def module_file(ctx: ModuleContext, args: Dict[str, Any]) -> Result:
    path = str(require(args, "path", "file"))
    warnings: List[str] = []
    mode = parse_mode(args.get("mode"), warnings)
    current = ctx.conn.stat(path)
    state = args.get("state") or ("file" if current is None else current.kind if current.kind in ("file", "directory") else "file")
    before = "absent" if current is None else current.kind
    result = Result(warnings=warnings, data={"path": path, "state": state})
    if state not in ("file", "directory", "absent"):
        raise ModuleFailure(f"state {state!r} is not implemented in miniansible; use file, directory or absent")
    if state == "absent":
        if current is not None:
            result.changed = True
            result.diffs.append(content_diff(f"path: {path}\nstate: {before}\n", f"path: {path}\nstate: absent\n", path))
            if not ctx.check:
                ctx.conn.remove(path)
        return result
    if state == "file":
        if current is None:
            raise ModuleFailure(f"file ({path}) is absent, cannot continue")
        if current.kind != "file":
            raise ModuleFailure(f"{path} is a {current.kind}, not a file")
        result.changed = apply_mode(ctx, path, mode, current)
        return result
    if current is not None and current.kind != "directory":
        raise ModuleFailure(f"{path} already exists as a {current.kind}")
    if current is None:
        missing: List[str] = []
        probe = path.rstrip("/") or "/"
        while probe not in ("", "/") and ctx.conn.stat(probe) is None:
            missing.insert(0, probe)
            probe = posixpath.dirname(probe)
        result.changed = True
        result.data["created"] = missing
        result.diffs.append(content_diff(f"path: {path}\nstate: absent\n", f"path: {path}\nstate: directory\n", path))
        if not ctx.check:
            for directory in missing:
                ctx.conn.mkdir(directory)
                if mode is not None:
                    ctx.conn.chmod(directory, mode)
        return result
    result.changed = apply_mode(ctx, path, mode, current)
    return result


def make_parents(conn: Connection, path: str) -> None:
    missing: List[str] = []
    while path not in ("", "/") and conn.stat(path) is None:
        missing.insert(0, path)
        path = posixpath.dirname(path)
    for directory in missing:
        conn.mkdir(directory)


def find_local(playbook_dir: str, folder: str, name: str) -> str:
    candidates = [name] if os.path.isabs(name) else [os.path.join(playbook_dir, folder, name), os.path.join(playbook_dir, name)]
    for candidate in candidates:
        if os.path.isfile(candidate):
            return candidate
    raise ModuleFailure(f"could not find {name!r}; looked in {', '.join(candidates)}")


def read_local(path: str) -> str:
    with open(path, encoding="utf-8", errors="surrogateescape", newline="") as handle:
        return handle.read()


def module_copy(ctx: ModuleContext, args: Dict[str, Any]) -> Result:
    dest = str(require(args, "dest", "copy"))
    warnings: List[str] = []
    mode = parse_mode(args.get("mode"), warnings)
    if args.get("content") is not None:
        raw = args["content"]
        content = json.dumps(raw, indent=4, sort_keys=True) if isinstance(raw, (dict, list)) else to_text(raw)
        source = dest
    elif args.get("src"):
        source = find_local(ctx.playbook_dir, "files", str(args["src"]))
        content = read_local(source)
        current = ctx.conn.stat(dest)
        if current is not None and current.kind == "directory":
            dest = posixpath.join(dest, os.path.basename(source))
    else:
        raise ModuleFailure("one of the following is required: src, content (module copy)")
    result = ensure_content(ctx, dest, content, mode, source)
    result.warnings = warnings
    return result


def module_template(ctx: ModuleContext, args: Dict[str, Any]) -> Result:
    source = find_local(ctx.playbook_dir, "templates", str(require(args, "src", "template")))
    dest = str(require(args, "dest", "template"))
    warnings: List[str] = []
    mode = parse_mode(args.get("mode"), warnings)
    try:
        content = render_template(read_local(source), ctx.variables)
    except TemplateError as exc:
        raise ModuleFailure(f"{os.path.basename(source)}: {exc}")
    result = ensure_content(ctx, dest, content, mode, source)
    result.warnings = warnings
    return result


def lineinfile_present(lines: List[str], line: str, regexp: Optional[str] = None, insertafter: Optional[str] = None) -> Tuple[List[str], str]:
    found = -1
    if regexp is not None:
        pattern = re.compile(regexp)
        for number, current in enumerate(lines):
            if pattern.search(current):
                found = number
    anchor = -1
    if found == -1:
        after = re.compile(insertafter) if insertafter not in (None, "EOF", "BOF") else None
        for number, current in enumerate(lines):
            if current.rstrip("\r\n") == line:
                found = number
            elif after is not None and after.search(current):
                anchor = number + 1
    new = list(lines)
    if found != -1:
        if new[found] != line + "\n":
            new[found] = line + "\n"
            return new, "line replaced"
        return new, ""
    if insertafter == "BOF":
        return [line + "\n"] + new, "line added"
    if anchor == -1:
        if new and not new[-1].endswith(("\n", "\r")):
            new[-1] += "\n"
        return new + [line + "\n"], "line added"
    if anchor < len(new) and new[anchor].rstrip("\r\n") == line:
        return new, ""
    if anchor == len(new) and new[-1].rstrip("\r\n") == line:
        return new, ""
    if anchor == len(new) and not new[-1].endswith("\n"):
        new[-1] += "\n"
    new.insert(anchor, line + "\n")
    return new, "line added"


def lineinfile_absent(lines: List[str], line: Optional[str] = None, regexp: Optional[str] = None) -> Tuple[List[str], str]:
    pattern = re.compile(regexp) if regexp is not None else None
    kept = [current for current in lines if not (pattern.search(current) if pattern else current.rstrip("\r\n") == line)]
    removed = len(lines) - len(kept)
    return kept, f"{removed} line(s) removed" if removed else ""


def module_lineinfile(ctx: ModuleContext, args: Dict[str, Any]) -> Result:
    path = str(require(args, "path", "lineinfile"))
    state = args.get("state") or "present"
    regexp = args.get("regexp")
    line = args.get("line")
    current = ctx.conn.stat(path)
    if current is None:
        if state == "absent":
            return Result(msg="file not present", data={"path": path})
        if not to_bool(args.get("create", False)):
            raise ModuleFailure(f"Destination {path} does not exist !")
        before = ""
        parent = posixpath.dirname(path)
        if parent and not ctx.check and ctx.conn.stat(parent) is None:
            make_parents(ctx.conn, parent)
    elif current.kind != "file":
        raise ModuleFailure(f"Path {path} is a {current.kind} !")
    else:
        before = ctx.conn.read(path)
    lines = before.splitlines(True)
    if state == "present":
        if line is None:
            raise ModuleFailure("line is required with state=present (module lineinfile)")
        new, message = lineinfile_present(lines, str(line), None if regexp is None else str(regexp), args.get("insertafter"))
    elif state == "absent":
        if line is None and regexp is None:
            raise ModuleFailure("one of line or regexp is required with state=absent (module lineinfile)")
        new, message = lineinfile_absent(lines, None if line is None else str(line), None if regexp is None else str(regexp))
    else:
        raise ModuleFailure(f"state must be present or absent, got {state!r}")
    after = "".join(new)
    warnings: List[str] = []
    mode = parse_mode(args.get("mode"), warnings)
    result = Result(msg=message, warnings=warnings, data={"path": path})
    if after != before or current is None:
        result.changed = True
        result.diffs.append(content_diff(before, after, path))
        if not ctx.check:
            ctx.conn.write(path, after)
    written = current if current is not None or ctx.check else ctx.conn.stat(path)
    if apply_mode(ctx, path, mode, written):
        result.changed = True
    return result


FREE_FORM_KEYS = ("creates", "removes", "chdir")


def module_command(ctx: ModuleContext, args: Dict[str, Any], shell: bool = False) -> Result:
    name = "shell" if shell else "command"
    raw = args.get("_raw_params") or args.get("cmd")
    if shell:
        if not raw:
            raise ModuleFailure("no command given (module shell)")
        argv = ["/bin/sh", "-c", str(raw)]
    else:
        argv = [str(item) for item in args["argv"]] if args.get("argv") else shlex.split(str(raw or ""))
        if not argv:
            raise ModuleFailure("no command given (module command)")
    creates, removes, chdir = args.get("creates"), args.get("removes"), args.get("chdir")
    verb = "Would" if ctx.check else "Did"
    if creates and ctx.conn.exists(str(creates)):
        return Result(msg=f"{verb} not run command since '{creates}' exists", data={"rc": 0, "stdout": f"skipped, since {creates} exists", "cmd": argv})
    if removes and not ctx.conn.exists(str(removes)):
        return Result(msg=f"{verb} not run command since '{removes}' does not exist", data={"rc": 0, "stdout": f"skipped, since {removes} does not exist", "cmd": argv})
    if ctx.check:
        if creates is None and removes is None:
            return Result(skipped=True, msg="Command would have run if not in check mode", data={"cmd": argv})
        return Result(changed=True, msg="Command would have run if not in check mode", data={"rc": 0, "cmd": argv})
    outcome = ctx.conn.run(argv, chdir=None if chdir is None else str(chdir))
    stdout, stderr = outcome.stdout.rstrip("\r\n"), outcome.stderr.rstrip("\r\n")
    data = {"rc": outcome.rc, "stdout": stdout, "stderr": stderr, "stdout_lines": stdout.splitlines(), "cmd": argv}
    if outcome.rc != 0:
        return Result(changed=True, failed=True, msg=f"The command exited with a non-zero return code ({name}).", data=data)
    return Result(changed=True, data=data)


SERVICE_DB = "/tmp/miniansible-services.json"


def module_service(ctx: ModuleContext, args: Dict[str, Any]) -> Result:
    name = str(require(args, "name", "service"))
    state, enabled = args.get("state"), args.get("enabled")
    if state not in (None, "started", "stopped", "restarted", "reloaded"):
        raise ModuleFailure(f"state must be started, stopped, restarted or reloaded, got {state!r}")
    configured = ctx.variables.lookup("miniansible_service_db")
    db_path = SERVICE_DB if isinstance(configured, Undefined) else str(configured)
    database = json.loads(ctx.conn.read(db_path)) if ctx.conn.stat(db_path) else {}
    entry = database.get(name, {"state": "stopped", "enabled": False, "restarts": 0})
    new = dict(entry)
    if state == "started":
        new["state"] = "running"
    elif state == "stopped":
        new["state"] = "stopped"
    elif state in ("restarted", "reloaded"):
        new["state"] = "running"
        new["restarts"] = entry["restarts"] + 1
    if enabled is not None:
        new["enabled"] = to_bool(enabled)
    result = Result(changed=new != entry, data={"name": name, "state": new["state"], "enabled": new["enabled"], "restarts": new["restarts"]})
    if result.changed:
        result.diffs.append(content_diff(json.dumps(entry, sort_keys=True) + "\n", json.dumps(new, sort_keys=True) + "\n", f"service {name}"))
        if not ctx.check:
            database[name] = new
            ctx.conn.write(db_path, json.dumps(database, indent=2, sort_keys=True) + "\n")
    return result


def module_setup(ctx: ModuleContext, args: Dict[str, Any]) -> Result:
    facts = {f"ansible_{key}": value for key, value in gather_facts(ctx.conn).items()}
    wanted = args.get("filter")
    if wanted:
        patterns = wanted if isinstance(wanted, list) else str(wanted).split(",")
        facts = {key: value for key, value in facts.items() if any(fnmatch.fnmatchcase(key, p) for p in patterns)}
    return Result(data={"ansible_facts": facts})


def module_debug(ctx: ModuleContext, args: Dict[str, Any]) -> Result:
    if args.get("var"):
        name = str(args["var"])
        try:
            value = template_value("{{ " + name + " }}", ctx.variables)
        except TemplateError:
            value = "VARIABLE IS NOT DEFINED!"
        return Result(data={name: value})
    return Result(data={"msg": args.get("msg", "Hello world!")})


def module_ping(ctx: ModuleContext, args: Dict[str, Any]) -> Result:
    return Result(data={"ping": args.get("data", "pong")})


MODULES: Dict[str, Module] = {
    "file": Module(module_file, ("path", "state", "mode"), {"dest": "path", "name": "path"}),
    "copy": Module(module_copy, ("dest", "content", "src", "mode")),
    "template": Module(module_template, ("src", "dest", "mode")),
    "lineinfile": Module(module_lineinfile, ("path", "line", "regexp", "insertafter", "state", "create", "mode"), {"dest": "path", "name": "path"}),
    "command": Module(module_command, ("_raw_params", "cmd", "argv", "creates", "removes", "chdir"), free_form=True),
    "shell": Module(lambda ctx, args: module_command(ctx, args, shell=True), ("_raw_params", "cmd", "creates", "removes", "chdir"), free_form=True),
    "service": Module(module_service, ("name", "state", "enabled")),
    "setup": Module(module_setup, ("filter", "gather_subset")),
    "debug": Module(module_debug, ("msg", "var")),
    "ping": Module(module_ping, ("data",)),
}


def module_name(name: str) -> str:
    for prefix in ("ansible.builtin.", "ansible.legacy."):
        if name.startswith(prefix):
            return name[len(prefix) :]
    return name


def split_args(text: str) -> List[str]:
    tokens: List[str] = []
    current = ""
    quote: Optional[str] = None
    depth = 0
    index = 0
    while index < len(text):
        pair, char = text[index : index + 2], text[index]
        if quote:
            current += char
            if char == quote and text[index - 1] != "\\":
                quote = None
        elif pair in ("{{", "{%"):
            depth += 1
            current += pair
            index += 2
            continue
        elif pair in ("}}", "%}") and depth:
            depth -= 1
            current += pair
            index += 2
            continue
        elif char in "\"'" and not depth:
            quote = char
            current += char
        elif char.isspace() and not depth:
            if current:
                tokens.append(current)
            current = ""
        else:
            current += char
        index += 1
    if quote or depth:
        raise PlaybookError(f"unbalanced quotes or template braces in {text!r}")
    if current:
        tokens.append(current)
    return tokens


def unquote(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        return value[1:-1]
    return value


def split_free_form(text: str, module: Module) -> Dict[str, Any]:
    args: Dict[str, Any] = {}
    raw: List[str] = []
    for token in split_args(text):
        key, sep, value = token.partition("=")
        if module.free_form:
            if sep and key in FREE_FORM_KEYS:
                args[key] = unquote(value)
            else:
                raw.append(token)
        elif not sep:
            raise PlaybookError(f"expected key=value module arguments, got {token!r}")
        else:
            args[key] = unquote(value)
    if module.free_form:
        args["_raw_params"] = " ".join(raw)
    return args


def normalise_args(name: str, raw: Any) -> Dict[str, Any]:
    module = MODULES[name]
    if raw is None:
        args: Dict[str, Any] = {}
    elif isinstance(raw, dict):
        args = dict(raw)
    elif isinstance(raw, str):
        args = split_free_form(raw, module)
    else:
        raise PlaybookError(f"arguments of module {name} must be a mapping or a key=value string, got {type_name(raw)}")
    for alias, target in module.aliases.items():
        if alias in args and target not in args:
            args[target] = args.pop(alias)
    unknown = sorted(key for key in args if key not in module.params)
    if unknown:
        supported = ", ".join(sorted(p for p in module.params if not p.startswith("_")) + sorted(module.aliases))
        raise PlaybookError(f"Unsupported parameters for ({name}) module: {', '.join(unknown)}. Supported parameters include: {supported}.")
    return args


TASK_KEYWORDS = ("name", "when", "notify", "register", "loop", "vars", "changed_when", "failed_when", "check_mode", "ignore_errors", "listen", "tags", "become", "become_user")
PLAY_KEYWORDS = ("name", "hosts", "gather_facts", "vars", "tasks", "handlers", "connection", "become", "become_user", "tags")
IGNORED_KEYWORDS = ("become", "become_user", "tags")
RECAP_RE = re.compile(r"^(\S+)\s+:\s+ok=(\d+)\s+changed=(\d+)\s+unreachable=(\d+)\s+failed=(\d+)\s+skipped=(\d+)\s+rescued=(\d+)\s+ignored=(\d+)", re.M)
RECAP_FIELDS = ("ok", "changed", "unreachable", "failed", "skipped", "rescued", "ignored")


def as_list(value: Any) -> List[Any]:
    if value is None:
        return []
    return list(value) if isinstance(value, list) else [value]


@dataclass
class Task:
    name: str
    module: str
    args: Dict[str, Any]
    when: List[Any] = field(default_factory=list)
    notify: List[str] = field(default_factory=list)
    register: Optional[str] = None
    loop: Any = None
    vars: Dict[str, Any] = field(default_factory=dict)
    changed_when: Optional[List[Any]] = None
    failed_when: Optional[List[Any]] = None
    check_mode: Optional[bool] = None
    ignore_errors: bool = False
    listen: List[str] = field(default_factory=list)


@dataclass
class Play:
    name: str
    hosts: str
    tasks: List[Task]
    handlers: List[Task] = field(default_factory=list)
    vars: Dict[str, Any] = field(default_factory=dict)
    gather_facts: bool = True
    connection: Optional[str] = None


GATHER_FACTS = Task(name="Gathering Facts", module="setup", args={})


def parse_task(raw: Any, where: str, warnings: List[str]) -> Task:
    if not isinstance(raw, dict):
        raise PlaybookError(f"{where}: a task is a mapping such as name: ... plus one module, got {type_name(raw)}")
    actions = [key for key in raw if key not in TASK_KEYWORDS]
    if not actions:
        raise PlaybookError(f"{where}: no module/action detected in task (keys: {', '.join(raw)})")
    if len(actions) > 1:
        raise PlaybookError(f"{where}: conflicting action statements: {', '.join(actions)}")
    action = actions[0]
    name = module_name(action)
    if name not in MODULES:
        raise PlaybookError(f"{where}: couldn't resolve module/action {action!r}; miniansible implements {', '.join(sorted(MODULES))}")
    try:
        args = normalise_args(name, raw[action])
    except PlaybookError as exc:
        raise PlaybookError(f"{where}: {exc}")
    task_vars = raw.get("vars") or {}
    if not isinstance(task_vars, dict):
        raise PlaybookError(f"{where}: vars must be a mapping")
    for keyword in IGNORED_KEYWORDS:
        if keyword in raw:
            warnings.append(f"{where}: {keyword} is accepted and ignored by miniansible")
    return Task(
        name=str(raw.get("name") or action),
        module=name,
        args=args,
        when=as_list(raw.get("when")),
        notify=[str(item) for item in as_list(raw.get("notify"))],
        register=None if raw.get("register") is None else str(raw["register"]),
        loop=raw.get("loop"),
        vars=dict(task_vars),
        changed_when=None if "changed_when" not in raw else as_list(raw["changed_when"]),
        failed_when=None if "failed_when" not in raw else as_list(raw["failed_when"]),
        check_mode=None if raw.get("check_mode") is None else to_bool(raw["check_mode"]),
        ignore_errors=to_bool(raw.get("ignore_errors", False)),
        listen=[str(item) for item in as_list(raw.get("listen"))],
    )


def parse_play(raw: Any, index: int, warnings: List[str]) -> Play:
    where = f"play {index}"
    if not isinstance(raw, dict):
        raise PlaybookError(f"{where}: a play is a mapping with hosts: and tasks:, got {type_name(raw)}")
    for key in raw:
        if key not in PLAY_KEYWORDS:
            raise PlaybookError(f"{where}: {key!r} is not a valid attribute for a Play")
    hosts = raw.get("hosts")
    if not hosts:
        raise PlaybookError(f"{where}: hosts: is required (a group, a host, all, or a pattern such as web:&prod)")
    tasks = [parse_task(item, f"{where} task {number}", warnings) for number, item in enumerate(as_list(raw.get("tasks")), start=1)]
    handlers = [parse_task(item, f"{where} handler {number}", warnings) for number, item in enumerate(as_list(raw.get("handlers")), start=1)]
    topics = {handler.name for handler in handlers} | {topic for handler in handlers for topic in handler.listen}
    for task in tasks + handlers:
        for name in task.notify:
            if name not in topics:
                raise PlaybookError(f"{where}: task {task.name!r} notifies {name!r}, but no handler has that name or listens to it")
    play_vars = raw.get("vars") or {}
    if not isinstance(play_vars, dict):
        raise PlaybookError(f"{where}: vars must be a mapping")
    for keyword in IGNORED_KEYWORDS:
        if keyword in raw:
            warnings.append(f"{where}: {keyword} is accepted and ignored by miniansible")
    return Play(
        name=str(raw.get("name") or ""),
        hosts=":".join(str(h) for h in hosts) if isinstance(hosts, list) else str(hosts),
        tasks=tasks,
        handlers=handlers,
        vars=dict(play_vars),
        gather_facts=to_bool(raw.get("gather_facts", True)),
        connection=None if raw.get("connection") is None else str(raw["connection"]),
    )


def parse_playbook(text: str) -> Tuple[List[Play], List[str]]:
    data = parse_yaml(text)
    if not isinstance(data, list):
        raise PlaybookError("a playbook is a list of plays; start each play with '- name:' or '- hosts:'")
    warnings: List[str] = []
    return [parse_play(raw, number, warnings) for number, raw in enumerate(data, start=1)], warnings


def load_playbook(path: str) -> Tuple[List[Play], List[str]]:
    try:
        with open(path, encoding="utf-8") as handle:
            text = handle.read()
    except OSError as exc:
        raise PlaybookError(f"cannot read playbook {path}: {exc.strerror}")
    try:
        return parse_playbook(text)
    except YamlError as exc:
        raise PlaybookError(f"{path}:{exc.line}: {str(exc).split(': ', 1)[1]}")
    except PlaybookError as exc:
        raise PlaybookError(f"{path}: {exc}")


@dataclass
class Stats:
    ok: int = 0
    changed: int = 0
    unreachable: int = 0
    failed: int = 0
    skipped: int = 0
    rescued: int = 0
    ignored: int = 0


@dataclass
class HostState:
    name: str
    facts: Dict[str, Any] = field(default_factory=dict)
    registered: Dict[str, Any] = field(default_factory=dict)
    notified: List[str] = field(default_factory=list)
    handled: List[str] = field(default_factory=list)
    failed: bool = False
    unreachable: bool = False
    conn: Optional[Connection] = None

    @property
    def active(self) -> bool:
        return not self.failed and not self.unreachable


def banner(text: str) -> str:
    return f"{text} " + "*" * max(3, 79 - len(text))


def compact(result: Result) -> str:
    return json.dumps(result.as_dict(), sort_keys=True, default=str)


def recap_line(host: str, stats: Stats) -> str:
    return f"{host:<26} : " + " ".join(f"{name}={getattr(stats, name):<4}" for name in RECAP_FIELDS)


def parse_recap(text: str) -> Dict[str, Dict[str, int]]:
    return {m.group(1): dict(zip(RECAP_FIELDS, (int(v) for v in m.groups()[1:]))) for m in RECAP_RE.finditer(text)}


class PlaybookRunner:
    def __init__(
        self,
        inventory: Inventory,
        check: bool = False,
        diff: bool = False,
        extra_vars: Optional[Dict[str, Any]] = None,
        connection: Optional[str] = None,
        connect_fn: Callable[[str, Dict[str, Any], Optional[str]], Connection] = connect,
        emit: Callable[[str], None] = print,
        warn: Callable[[str], None] = lambda message: print(f"[WARNING]: {message}", file=sys.stderr),
        limit: Optional[str] = None,
        playbook_dir: str = ".",
        adhoc: bool = False,
    ) -> None:
        self.inventory = inventory
        self.check = check
        self.diff = diff
        self.extra_vars = dict(extra_vars or {})
        self.connection = connection
        self.connect_fn = connect_fn
        self.emit = emit
        self.warn = warn
        self.limit = limit
        self.playbook_dir = playbook_dir
        self.adhoc = adhoc
        self.stats: Dict[str, Stats] = {}
        self.records: List[Dict[str, Any]] = []
        self.states: Dict[str, HostState] = {}

    def hosts_for(self, play: Play) -> List[str]:
        hosts = self.inventory.match(play.hosts)
        if not hosts and play.hosts in ("localhost", "127.0.0.1"):
            hosts = [play.hosts]
        if self.limit:
            allowed = set(self.inventory.match(self.limit)) | ({self.limit} if self.limit in hosts else set())
            hosts = [h for h in hosts if h in allowed]
        return hosts

    def layers(self, state: HostState, play: Play, task: Optional[Task] = None) -> List[Tuple[str, Dict[str, Any]]]:
        if state.name in self.inventory.hosts:
            layers = self.inventory.var_layers(state.name)
        else:
            layers = [("implicit localhost", {"ansible_connection": "local"})]
        if state.facts:
            facts: Dict[str, Any] = {"ansible_facts": dict(state.facts)}
            facts.update({f"ansible_{key}": value for key, value in state.facts.items()})
            layers.append(("facts", facts))
        layers.append(("play vars", play.vars))
        if task is not None and task.vars:
            layers.append(("task vars", task.vars))
        layers.append(("registered vars", state.registered))
        layers.append(("magic vars", self.magic(state)))
        layers.append(("extra vars", self.extra_vars))
        return layers

    def magic(self, state: HostState) -> Dict[str, Any]:
        hostvars = {host: self.inventory.host_vars(host) for host in self.inventory.hosts}
        for name, other in self.states.items():
            if other.facts:
                hostvars.setdefault(name, {})["ansible_facts"] = dict(other.facts)
        known = state.name in self.inventory.hosts
        return {
            "inventory_hostname": state.name,
            "inventory_hostname_short": state.name.split(".")[0],
            "group_names": [g for g in self.inventory.groups_of(state.name) if g != "all"] if known else [],
            "groups": {name: self.inventory.hosts_in(name) for name in self.inventory.groups},
            "hostvars": hostvars,
            "playbook_dir": os.path.abspath(self.playbook_dir),
            "ansible_check_mode": self.check,
            "ansible_diff_mode": self.diff,
            "ansible_play_hosts": [name for name, other in self.states.items() if other.active],
        }

    def context(self, state: HostState, play: Play, task: Optional[Task] = None) -> Context:
        merged: Dict[str, Any] = {}
        for _, layer in self.layers(state, play, task):
            merged.update(layer)
        return Context(merged)

    def connection_for(self, state: HostState, ctx: Context, play: Play) -> Connection:
        if state.conn is None:
            variables = {}
            for key in ("ansible_connection", "ansible_host", "ansible_docker_host"):
                value = ctx.lookup(key)
                if not isinstance(value, Undefined):
                    variables[key] = value
            state.conn = self.connect_fn(state.name, variables, play.connection or self.connection)
        return state.conn

    def execute(self, task: Task, play: Play, state: HostState) -> Tuple[Result, List[Tuple[Any, Result]]]:
        ctx = self.context(state, play, task)
        try:
            conn = self.connection_for(state, ctx, play)
            if task.loop is None:
                return self.execute_once(task, state, ctx, conn, True), []
            try:
                items = template_value(task.loop, ctx)
            except TemplateError as exc:
                return Result(failed=True, msg=f"loop: {exc}"), []
            if not isinstance(items, list):
                return Result(failed=True, msg=f"loop expects a list, got {type_name(items)}: {items!r}"), []
            results = [(item, self.execute_once(task, state, ctx.child({"item": item}), conn, False)) for item in items]
            total = Result(
                changed=any(r.changed for _, r in results),
                failed=any(r.failed for _, r in results),
                skipped=all(r.skipped for _, r in results),
                msg="All items completed" if results else "No items in the list",
                data={"results": [dict(r.as_dict(), item=item) for item, r in results]},
            )
            if task.register:
                state.registered[task.register] = total.as_dict()
            return total, results
        except Unreachable as exc:
            return Result(unreachable=True, msg=str(exc)), []

    def execute_once(self, task: Task, state: HostState, ctx: Context, conn: Connection, register: bool) -> Result:
        for condition in task.when:
            try:
                if not evaluate_condition(condition, ctx):
                    return Result(skipped=True, msg="Conditional result was False", data={"false_condition": condition})
            except TemplateError as exc:
                return Result(failed=True, msg=f"The conditional check {condition!r} failed. The error was: {exc}")
        try:
            args = template_value(task.args, ctx)
        except TemplateError as exc:
            return Result(failed=True, msg=f"The task includes an undefined variable or a template error: {exc}")
        check = self.check if task.check_mode is None else task.check_mode
        module_ctx = ModuleContext(conn, check, self.diff, self.playbook_dir, ctx)
        try:
            result = MODULES[task.module].run(module_ctx, args)
        except ModuleFailure as exc:
            result = Result(failed=True, msg=str(exc))
        except OSError as exc:
            result = Result(failed=True, msg=f"{type(exc).__name__}: {exc}")
        if task.module == "setup" and not result.failed:
            state.facts.update({key[len("ansible_") :]: value for key, value in result.data["ansible_facts"].items()})
        verdict = ctx.child({task.register: result.as_dict()}) if task.register else ctx
        try:
            if task.changed_when is not None and not result.skipped:
                result.changed = all(evaluate_condition(e, verdict) for e in task.changed_when)
            if task.failed_when is not None and not result.skipped:
                result.failed = all(evaluate_condition(e, verdict) for e in task.failed_when)
        except TemplateError as exc:
            result = Result(failed=True, msg=f"changed_when/failed_when: {exc}")
        if task.register and register:
            state.registered[task.register] = result.as_dict()
        return result

    def status_line(self, host: str, result: Result, task: Task, item: Any = None) -> str:
        label = "" if item is None else f" => (item={to_text(item) if not isinstance(item, (dict, list)) else json.dumps(item, sort_keys=True)})"
        if self.adhoc:
            word = "UNREACHABLE!" if result.unreachable else "FAILED!" if result.failed else "SKIPPED" if result.skipped else "CHANGED" if result.changed else "SUCCESS"
            return f"{host} | {word}" if result.skipped else f"{host} | {word} => {json.dumps(result.as_dict(), indent=4, sort_keys=True, default=str)}"
        if result.unreachable:
            return f"fatal: [{host}]: UNREACHABLE! => {compact(result)}"
        if result.failed:
            prefix = "fatal" if item is None else "failed"
            line = f"{prefix}: [{host}]{'' if item is None else ' (item=' + to_text(item) + ')'}: FAILED! => {compact(result)}"
            return line + ("\n...ignoring" if task.ignore_errors else "")
        if result.skipped:
            return f"skipping: [{host}]{label}"
        word = "changed" if result.changed else "ok"
        if task.module == "debug":
            return f"{word}: [{host}]{label} => {json.dumps(result.data, indent=4, sort_keys=True, default=str)}"
        return f"{word}: [{host}]{label}"

    def report(self, host: str, result: Result, items: List[Tuple[Any, Result]], task: Task) -> None:
        for item, item_result in items or [(None, result)]:
            for warning in item_result.warnings:
                self.warn(warning)
            if self.diff and item_result.changed:
                for diff in item_result.diffs:
                    self.emit(render_diff(diff).rstrip("\n"))
            self.emit(self.status_line(host, item_result, task, item))

    def account(self, state: HostState, task: Task, result: Result) -> str:
        stats = self.stats.setdefault(state.name, Stats())
        if result.unreachable:
            stats.unreachable += 1
            state.unreachable = True
            return "unreachable"
        if result.failed:
            if task.ignore_errors:
                stats.ok += 1
                stats.ignored += 1
                return "ignored"
            stats.failed += 1
            state.failed = True
            return "failed"
        if result.skipped:
            stats.skipped += 1
            return "skipped"
        stats.ok += 1
        if not result.changed:
            return "ok"
        stats.changed += 1
        for name in task.notify:
            if name not in state.notified:
                state.notified.append(name)
        return "changed"

    def run_task(self, task: Task, play: Play, states: List[HostState], label: str = "TASK") -> None:
        if not self.adhoc:
            self.emit("")
            self.emit(banner(f"{label} [{task.name}]"))
        record: Dict[str, Any] = {"task": task.name, "module": task.module, "handler": label != "TASK", "hosts": {}}
        for state in [s for s in states if s.active]:
            result, items = self.execute(task, play, state)
            self.report(state.name, result, items, task)
            status = self.account(state, task, result)
            record["hosts"][state.name] = dict(result.as_dict(), status=status)
        if self.records:
            self.records[-1]["tasks"].append(record)

    def run_play(self, play: Play) -> None:
        hosts = self.hosts_for(play)
        title = play.name or play.hosts
        self.records.append({"play": title, "hosts": hosts, "tasks": []})
        if not self.adhoc:
            self.emit("")
            self.emit(banner(f"PLAY [{title}]"))
        if not hosts:
            self.emit("skipping: no hosts matched")
            return
        states = [HostState(host) for host in hosts]
        self.states = {state.name: state for state in states}
        for host in hosts:
            self.stats.setdefault(host, Stats())
        for task in ([GATHER_FACTS] if play.gather_facts else []) + play.tasks:
            if not any(state.active for state in states):
                self.emit("")
                self.emit(banner("NO MORE HOSTS LEFT"))
                break
            self.run_task(task, play, states)
        for handler in play.handlers:
            names = {handler.name, *handler.listen}
            targets = [s for s in states if s.active and handler.name not in s.handled and any(n in names for n in s.notified)]
            if targets:
                for state in targets:
                    state.handled.append(handler.name)
                self.run_task(handler, play, targets, "RUNNING HANDLER")

    def recap(self) -> List[str]:
        return ["", banner("PLAY RECAP")] + [recap_line(host, self.stats[host]) for host in sorted(self.stats)]

    def run(self, plays: List[Play]) -> int:
        for play in plays:
            self.run_play(play)
        if not self.adhoc:
            for line in self.recap():
                self.emit(line)
        return self.exit_code()

    def exit_code(self) -> int:
        if any(s.failed for s in self.stats.values()):
            return 2
        if any(s.unreachable for s in self.stats.values()):
            return 4
        return 0

    def summary(self) -> Dict[str, Any]:
        return {"plays": self.records, "stats": {host: vars(stats) for host, stats in sorted(self.stats.items())}}


def parse_extra_vars(values: Sequence[str]) -> Dict[str, Any]:
    merged: Dict[str, Any] = {}
    for value in values:
        if value.startswith("@"):
            path = value[1:]
            try:
                with open(path, encoding="utf-8") as handle:
                    text = handle.read()
            except OSError as exc:
                raise PlaybookError(f"cannot read extra vars file {path}: {exc.strerror}")
            data = json.loads(text) if path.endswith(".json") else parse_yaml(text)
        elif value.lstrip().startswith("{"):
            data = json.loads(value)
        else:
            data = {}
            for token in shlex.split(value):
                key, sep, item = token.partition("=")
                if not sep:
                    raise PlaybookError(f"extra vars must be KEY=VALUE, a JSON object or @FILE, got {token!r}")
                data[key] = item
        if not isinstance(data, dict):
            raise PlaybookError(f"extra vars must be a mapping, got {type_name(data)}")
        merged.update(data)
    return merged


def explain_host(inventory: Inventory, host: str) -> Dict[str, Dict[str, Any]]:
    explained: Dict[str, Dict[str, Any]] = {}
    for source, layer in inventory.var_layers(host):
        for key, value in layer.items():
            overridden = [explained[key]["source"]] + explained[key]["overrides"] if key in explained else []
            explained[key] = {"value": value, "source": source, "overrides": overridden}
    return explained


def add_run_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("-i", "--inventory", help="INI or YAML inventory file, or a comma-separated host list")
    parser.add_argument("-e", "--extra-vars", action="append", default=[], help="KEY=VALUE, a JSON object or @FILE; highest precedence")
    parser.add_argument("-c", "--connection", help="default connection: local, docker or fake (ansible_connection in the inventory wins)")
    parser.add_argument("-l", "--limit", help="further limit the hosts with a pattern")
    parser.add_argument("-C", "--check", action="store_true", help="report what would change without changing anything")
    parser.add_argument("-D", "--diff", action="store_true", help="show the changes to file contents")
    parser.add_argument("--json", action="store_true", help="machine-readable result instead of the text output")


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="miniansible", description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    playbook = commands.add_parser("playbook", help="run a playbook (ansible-playbook)")
    playbook.add_argument("playbook")
    add_run_options(playbook)
    adhoc = commands.add_parser("adhoc", help="run one module on a host pattern (ansible PATTERN -m MODULE)")
    adhoc.add_argument("pattern")
    adhoc.add_argument("-m", "--module-name", default="command")
    adhoc.add_argument("-a", "--args", default="")
    add_run_options(adhoc)
    inventory_cmd = commands.add_parser("inventory", help="show an inventory (ansible-inventory)")
    inventory_cmd.add_argument("-i", "--inventory", required=True)
    shown = inventory_cmd.add_mutually_exclusive_group(required=True)
    shown.add_argument("--graph", nargs="?", const="all", metavar="GROUP")
    shown.add_argument("--list", action="store_true")
    shown.add_argument("--host")
    inventory_cmd.add_argument("--explain", action="store_true", help="with --host: the source of each variable and what it overrode")
    args = parser.parse_args(list(sys.argv[1:] if argv is None else argv))
    try:
        inventory = load_inventory(args.inventory)
        if args.command == "inventory":
            if args.graph:
                print(inventory.graph(args.graph))
            elif args.list:
                print(json.dumps(inventory.to_list(), indent=4, sort_keys=True))
            else:
                if args.host not in inventory.hosts:
                    raise InventoryError(f"host {args.host!r} is not in the inventory")
                print(json.dumps(explain_host(inventory, args.host) if args.explain else inventory.host_vars(args.host), indent=4, sort_keys=True, default=str))
            return 0
        lines: List[str] = []
        emit: Callable[[str], None] = lines.append if args.json else print
        extra = parse_extra_vars(args.extra_vars)
        if args.command == "adhoc":
            name = module_name(args.module_name)
            if name not in MODULES:
                raise PlaybookError(f"couldn't resolve module/action {args.module_name!r}; miniansible implements {', '.join(sorted(MODULES))}")
            task = Task(name=args.module_name, module=name, args=normalise_args(name, args.args or None))
            plays = [Play(name="adhoc", hosts=args.pattern, tasks=[task], gather_facts=False)]
            runner = PlaybookRunner(inventory, args.check, args.diff, extra, args.connection, emit=emit, limit=args.limit, adhoc=True)
        else:
            plays, warnings = load_playbook(args.playbook)
            runner = PlaybookRunner(inventory, args.check, args.diff, extra, args.connection, emit=emit, limit=args.limit, playbook_dir=os.path.dirname(os.path.abspath(args.playbook)))
            for warning in warnings:
                runner.warn(warning)
        code = runner.run(plays)
        if args.json:
            print(json.dumps(runner.summary(), indent=2, sort_keys=True, default=str))
        return code
    except MiniAnsibleError as exc:
        print(f"miniansible: ERROR! {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
