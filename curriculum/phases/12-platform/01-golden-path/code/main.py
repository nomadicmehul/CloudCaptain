"""scaffolder: a golden-path generator. It reads a copier-format template (questions in copier.yml, files under a skeleton directory), validates typed answers, renders file contents and file names with a Jinja subset, runs post-generation tasks, and updates services generated from an older template version with a per-file three-way merge that keeps the service's own changes."""
from __future__ import annotations

import argparse
import fnmatch
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

Runner = Callable[[List[str], Optional[str]], Tuple[int, bytes, str]]
Shell = Callable[[str, str], Tuple[int, str]]

ANSWERS_FILE = ".copier-answers.yml"
CONFIG_FILES = ("copier.yml", "copier.yaml")
DEFAULT_EXCLUDE = ("copier.yaml", "copier.yml", "~*", "*.py[co]", "__pycache__", ".git", ".DS_Store", ".svn")
SETTINGS = ("_subdirectory", "_templates_suffix", "_envops", "_tasks", "_exclude", "_min_copier_version")
ENVOPS = ("trim_blocks", "lstrip_blocks", "keep_trailing_newline", "autoescape")
QUESTION_KEYS = ("type", "help", "default", "choices", "validator", "when", "placeholder", "multiline", "secret")
TYPES = ("str", "int", "float", "bool", "yaml", "json")
MARK_OURS = "<<<<<<< before updating"
MARK_SPLIT = "======="
MARK_THEIRS = ">>>>>>> after updating"
HERE = os.path.dirname(os.path.abspath(__file__))
LESSON_TEMPLATE = os.path.join(HERE, "template")
LESSON_RELEASE = os.path.join(HERE, "fixtures", "template-v1.1.0.patch")
VERSION_TAG = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)$")


class ScaffoldError(Exception):
    pass


class TemplateError(ScaffoldError):
    pass


class UndefinedError(TemplateError):
    pass


class YamlError(ScaffoldError):
    def __init__(self, line: int, message: str) -> None:
        super().__init__(f"line {line}: {message}")


class AnswerError(ScaffoldError):
    def __init__(self, problems: List[str]) -> None:
        super().__init__("invalid answers:\n" + "\n".join("  " + p for p in problems))
        self.problems = problems


class GitError(ScaffoldError):
    pass


NULL_WORDS = {"", "~", "null", "Null", "NULL"}
TRUE_WORDS = {"true", "True", "TRUE"}
FALSE_WORDS = {"false", "False", "FALSE"}
INT_RE = re.compile(r"^[-+]?[0-9]+$")
FLOAT_RE = re.compile(r"^[-+]?(\.[0-9]+|[0-9]+(\.[0-9]*)?)([eE][-+]?[0-9]+)?$")
ESCAPES = {"n": "\n", "t": "\t", "r": "\r", '"': '"', "\\": "\\", "/": "/", "0": "\0", " ": " "}
BLOCK_HEADER_RE = re.compile(r"^([|>])([-+]?)$")


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
    if INT_RE.match(text):
        return int(text)
    if FLOAT_RE.match(text):
        return float(text)
    return text


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


def parse_quoted(text: str, number: int) -> Tuple[str, str]:
    quote, out, index = text[0], [], 1
    while index < len(text):
        char = text[index]
        if quote == '"' and char == "\\":
            code = text[index + 1 : index + 2]
            if code in ("x", "u"):
                width = 2 if code == "x" else 4
                out.append(chr(int(text[index + 2 : index + 2 + width], 16)))
                index += 2 + width
                continue
            out.append(ESCAPES.get(code, code))
            index += 2
            continue
        if char == quote:
            if quote == "'" and text[index + 1 : index + 2] == "'":
                out.append("'")
                index += 2
                continue
            return "".join(out), text[index + 1 :]
        out.append(char)
        index += 1
    raise YamlError(number, "unterminated quoted string")


def parse_flow_value(text: str, number: int, terminators: str) -> Tuple[Any, str]:
    if text[:1] in "\"'":
        return parse_quoted(text, number)
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
            mapping[str(key)] = value
        rest = rest.lstrip()
        if rest.startswith(","):
            rest = rest[1:].lstrip()
        elif not rest.startswith(closer):
            raise YamlError(number, f"expected , or {closer} in flow collection")


def parse_scalar(text: str, number: int) -> Any:
    if text.startswith(("&", "*", "!", "<<")):
        raise YamlError(number, "anchors, aliases, tags and merge keys are not supported")
    if text[:1] in "\"'":
        value, rest = parse_quoted(text, number)
    elif text[:1] in "[{":
        value, rest = parse_flow(text, number)
    else:
        return plain_scalar(text)
    if rest.strip():
        raise YamlError(number, f"unexpected text after value: {rest.strip()!r}")
    return value


def split_key(text: str, number: int) -> Optional[Tuple[str, str]]:
    if text[:1] in "\"'":
        key, rest = parse_quoted(text, number)
        rest = rest.lstrip()
        if rest.startswith(":") and (len(rest) == 1 or rest[1] == " "):
            return key, rest[1:].strip()
        return None
    if text[:1] in "[{":
        return None
    for index, char in enumerate(text):
        if char == ":" and (index + 1 == len(text) or text[index + 1] == " "):
            return text[:index].strip(), text[index + 1 :].strip()
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

    def document(self) -> Any:
        if not self.lines:
            return None
        value = self.block(self.lines[0].indent)
        extra = self.peek()
        if extra is not None:
            raise YamlError(extra.number, f"unexpected indentation ({extra.indent} spaces)")
        return value

    def block(self, indent: int) -> Any:
        line = self.peek()
        if line is None:
            return None
        if is_item(line.text):
            return self.sequence(indent)
        if split_key(line.text, line.number) is None:
            self.position += 1
            return parse_scalar(line.text, line.number)
        return self.mapping(indent)

    def sequence(self, indent: int, compact: bool = False) -> List[Any]:
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
                items.append(self.block(nested.indent) if nested is not None and nested.indent > indent else None)
                continue
            column = indent + (len(line.text) - len(line.text[1:].lstrip()))
            self.lines[self.position] = Line(line.number, column, rest)
            items.append(self.block(column))

    def mapping(self, indent: int) -> Dict[str, Any]:
        result: Dict[str, Any] = {}
        while True:
            line = self.peek()
            if line is None or line.indent < indent:
                return result
            if line.indent > indent:
                raise YamlError(line.number, f"bad indentation in mapping (expected {indent} spaces, got {line.indent})")
            parts = split_key(line.text, line.number)
            if parts is None:
                raise YamlError(line.number, f"expected 'key: value', got {line.text!r}")
            key, rest = parts
            if key in result:
                raise YamlError(line.number, f"duplicate key {key!r}")
            self.position += 1
            if rest and BLOCK_HEADER_RE.match(rest):
                result[key] = self.block_scalar(rest, line.number, indent)
            elif rest:
                result[key] = parse_scalar(rest, line.number)
            else:
                nested = self.peek()
                if nested is None or nested.indent < indent:
                    result[key] = None
                elif nested.indent == indent:
                    result[key] = self.sequence(indent, compact=True) if is_item(nested.text) else None
                else:
                    result[key] = self.block(nested.indent)

    def block_scalar(self, header: str, number: int, parent: int) -> str:
        style, chomp = BLOCK_HEADER_RE.match(header).groups()
        body: List[str] = []
        index, indent = number, None
        while index < len(self.raw):
            raw = self.raw[index]
            if raw.strip():
                current = len(raw) - len(raw.lstrip(" "))
                if indent is None:
                    if current <= parent:
                        break
                    indent = current
                if current < indent:
                    break
                body.append(raw[indent:])
            else:
                body.append("")
            index += 1
        while self.position < len(self.lines) and self.lines[self.position].number <= index:
            self.position += 1
        while body and body[-1] == "":
            body.pop()
        text = "\n".join(body) if style == "|" else " ".join(line for line in body if line)
        return text if chomp == "-" else text + "\n"


def parse_yaml(source: str) -> Any:
    return YamlParser(source).document()


def parse_documents(source: str) -> List[Any]:
    return [parse_yaml(piece) for piece in re.split(r"(?m)^---[ \t]*$", source) if piece.strip()]


YAML11_BOOL = {"yes", "Yes", "YES", "no", "No", "NO", "true", "True", "TRUE", "false", "False", "FALSE", "on", "On", "ON", "off", "Off", "OFF"}
YAML11_INT = re.compile(r"^[-+]?(0b[0-1_]+|0[0-7_]+|(0|[1-9][0-9_]*)|0x[0-9a-fA-F_]+|[1-9][0-9_]*(:[0-5]?[0-9])+)$")
YAML11_FLOAT = re.compile(r"^([-+]?([0-9][0-9_]*)?\.[0-9.]*([eE][-+][0-9]+)?|[-+]?[0-9][0-9_]*(:[0-5]?[0-9])+\.[0-9_]*|[-+]?\.(inf|Inf|INF)|\.(nan|NaN|NAN))$")
YAML11_TIMESTAMP = re.compile(r"^[0-9][0-9][0-9][0-9]-[0-9][0-9]?-[0-9][0-9]?")


def yaml11_plain(text: str) -> bool:
    if not text or text != text.strip() or "\n" in text:
        return False
    if text in YAML11_BOOL or text in NULL_WORDS or text in ("<<", "=") or YAML11_INT.match(text) or YAML11_FLOAT.match(text) or YAML11_TIMESTAMP.match(text):
        return False
    if text[0] in "#,[]{}&*!|>'\"%@`" or (text[0] in "-?:" and (len(text) == 1 or text[1] == " ")):
        return False
    return ": " not in text and " #" not in text and not text.endswith(":") and all(ch.isprintable() for ch in text)


def yaml11_scalar(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return repr(value)
    text = str(value)
    if yaml11_plain(text):
        return text
    if all(ch.isprintable() for ch in text):
        return "'" + text.replace("'", "''") + "'"
    return json.dumps(text)


def nice_yaml(value: Any, indent: int = 4, level: int = 0) -> str:
    pad = " " * level
    if isinstance(value, dict):
        if not value:
            return pad + "{}\n"
        out = []
        for key in sorted(value):
            child = value[key]
            head = pad + yaml11_scalar(key) + ":"
            if isinstance(child, dict) and child:
                out.append(head + "\n" + nice_yaml(child, indent, level + indent))
            elif isinstance(child, list) and child:
                out.append(head + "\n" + nice_yaml(child, indent, level))
            else:
                out.append(head + " " + nice_yaml(child, indent, 0))
        return "".join(out)
    if isinstance(value, list):
        if not value:
            return pad + "[]\n"
        return "".join(pad + "- " + nice_yaml(item, indent, 0) for item in value)
    return pad + yaml11_scalar(value) + "\n"


TAG_RE = re.compile(r"\{\{|\{%|\{#")
CLOSERS = {"{{": "}}", "{%": "%}", "{#": "#}"}
ENDRAW_RE = re.compile(r"\{%(-?)\s*endraw\s*(-?)%\}")
EXPR_TOKEN_RE = re.compile(r"""\s*(?:(?P<number>\d+(?:\.\d+)?)|(?P<string>'(?:[^'\\]|\\.)*'|"(?:[^"\\]|\\.)*")|(?P<name>[A-Za-z_][A-Za-z0-9_]*)|(?P<op>==|!=|<=|>=|//|\*\*|[-+*/%~<>()\[\].,|:=]))""")


@dataclass
class Token:
    kind: str
    value: str
    line: int


def find_closer(src: str, start: int, closer: str) -> int:
    quote, index = None, start
    while index < len(src):
        char = src[index]
        if quote:
            if char == "\\":
                index += 2
                continue
            if char == quote:
                quote = None
        elif char in "\"'" and closer != "#}":
            quote = char
        elif src.startswith(closer, index):
            return index
        index += 1
    return -1


def tokenize(src: str, trim_blocks: bool, lstrip_blocks: bool, where: str) -> List[Token]:
    tokens: List[Token] = []
    pos, strip_next, trim_next = 0, False, False
    while True:
        match = TAG_RE.search(src, pos)
        end = match.start() if match else len(src)
        text = src[pos:end]
        if strip_next:
            text = text.lstrip()
        elif trim_next and text.startswith("\n"):
            text = text[1:]
        if match is None:
            if text:
                tokens.append(Token("text", text, src.count("\n", 0, pos) + 1))
            return tokens
        opener = match.group(0)
        line = src.count("\n", 0, match.start()) + 1
        inner_start = match.end()
        modifier = src[inner_start : inner_start + 1]
        if modifier == "-":
            text = text.rstrip()
        elif opener != "{{" and lstrip_blocks and modifier != "+":
            spaces = len(src[: match.start()]) - len(src[: match.start()].rstrip(" \t"))
            at_line_start = match.start() - spaces == 0 or src[match.start() - spaces - 1] == "\n"
            if spaces and at_line_start and text.endswith(src[match.start() - spaces : match.start()]):
                text = text[: len(text) - spaces]
        if text:
            tokens.append(Token("text", text, src.count("\n", 0, pos) + 1))
        close = find_closer(src, inner_start, CLOSERS[opener])
        if close < 0:
            raise TemplateError(f"{where}:{line}: {opener} is never closed with {CLOSERS[opener]}")
        inner = src[inner_start + (1 if modifier in "-+" and modifier else 0) : close]
        strip_next = inner.endswith("-")
        inner = inner[:-1] if strip_next else inner
        pos = close + 2
        trim_next = opener != "{{" and trim_blocks
        if opener == "{#":
            continue
        if opener == "{%" and inner.strip() == "raw":
            endraw = ENDRAW_RE.search(src, pos)
            if endraw is None:
                raise TemplateError(f"{where}:{line}: {{% raw %}} is never closed with {{% endraw %}}")
            raw = src[pos : endraw.start()]
            if strip_next:
                raw = raw.lstrip()
            if endraw.group(1) == "-":
                raw = raw.rstrip()
            elif lstrip_blocks:
                kept = raw.rstrip(" \t")
                if kept != raw and (kept.endswith("\n") or (not kept and src[pos - 1 : pos] == "\n")):
                    raw = kept
            if raw:
                tokens.append(Token("text", raw, line))
            strip_next = endraw.group(2) == "-"
            pos = endraw.end()
            continue
        tokens.append(Token("var" if opener == "{{" else "block", inner.strip(), line))


class ExprParser:
    def __init__(self, source: str, where: str) -> None:
        self.where = where
        self.tokens: List[Tuple[str, str]] = []
        pos = 0
        while pos < len(source):
            if not source[pos:].strip():
                break
            match = EXPR_TOKEN_RE.match(source, pos)
            if match is None or match.end() == pos:
                raise TemplateError(f"{where}: cannot read expression at {source[pos:]!r}")
            kind = match.lastgroup or "op"
            self.tokens.append((kind, match.group(kind)))
            pos = match.end()
        self.index = 0

    def fail(self, message: str) -> TemplateError:
        return TemplateError(f"{self.where}: {message}")

    def peek(self, offset: int = 0) -> Tuple[str, str]:
        index = self.index + offset
        return self.tokens[index] if index < len(self.tokens) else ("end", "")

    def take(self) -> Tuple[str, str]:
        token = self.peek()
        self.index += 1
        return token

    def accept(self, value: str) -> bool:
        kind, text = self.peek()
        if text == value and kind in ("op", "name"):
            self.index += 1
            return True
        return False

    def expect(self, value: str) -> None:
        if not self.accept(value):
            raise self.fail(f"expected {value!r}, got {self.peek()[1]!r}")

    def done(self) -> bool:
        return self.index >= len(self.tokens)

    def parse(self) -> Any:
        node = self.conditional()
        if not self.done():
            raise self.fail(f"unexpected {self.peek()[1]!r}")
        return node

    def conditional(self) -> Any:
        node = self.or_expr()
        while self.accept("if"):
            test = self.or_expr()
            other = self.conditional() if self.accept("else") else ("undefined",)
            node = ("cond", test, node, other)
        return node

    def or_expr(self) -> Any:
        node = self.and_expr()
        while self.accept("or"):
            node = ("or", node, self.and_expr())
        return node

    def and_expr(self) -> Any:
        node = self.not_expr()
        while self.accept("and"):
            node = ("and", node, self.not_expr())
        return node

    def not_expr(self) -> Any:
        if self.peek() == ("name", "not") and self.peek(1)[1] != "in":
            self.take()
            return ("not", self.not_expr())
        return self.compare()

    def compare(self) -> Any:
        node = self.math1()
        while True:
            kind, text = self.peek()
            if text in ("==", "!=", "<", "<=", ">", ">=") and kind == "op":
                self.take()
                node = ("cmp", text, node, self.math1())
            elif (kind, text) == ("name", "in"):
                self.take()
                node = ("cmp", "in", node, self.math1())
            elif (kind, text) == ("name", "not") and self.peek(1) == ("name", "in"):
                self.take()
                self.take()
                node = ("cmp", "not in", node, self.math1())
            else:
                return node

    def math1(self) -> Any:
        node = self.concat()
        while self.peek() in (("op", "+"), ("op", "-")):
            node = ("bin", self.take()[1], node, self.concat())
        return node

    def concat(self) -> Any:
        parts = [self.math2()]
        while self.accept("~"):
            parts.append(self.math2())
        return parts[0] if len(parts) == 1 else ("concat", parts)

    def math2(self) -> Any:
        node = self.unary()
        while self.peek() in (("op", "*"), ("op", "/"), ("op", "//"), ("op", "%")):
            node = ("bin", self.take()[1], node, self.unary())
        return node

    def unary(self) -> Any:
        if self.accept("-"):
            return ("neg", self.unary())
        node = self.postfix(self.primary())
        while True:
            if self.accept("|"):
                kind, name = self.take()
                if kind != "name":
                    raise self.fail(f"expected a filter name after |, got {name!r}")
                args, kwargs = self.call_args() if self.peek() == ("op", "(") else ([], {})
                node = ("filter", node, name, args, kwargs)
            elif self.accept("is"):
                negated = self.accept("not")
                kind, name = self.take()
                node = ("test", node, name, negated)
            else:
                return node

    def call_args(self) -> Tuple[List[Any], Dict[str, Any]]:
        self.expect("(")
        args: List[Any] = []
        kwargs: Dict[str, Any] = {}
        while not self.accept(")"):
            if self.peek()[0] == "name" and self.peek(1) == ("op", "="):
                name = self.take()[1]
                self.take()
                kwargs[name] = self.conditional()
            else:
                args.append(self.conditional())
            if not self.accept(","):
                self.expect(")")
                break
        return args, kwargs

    def primary(self) -> Any:
        kind, text = self.take()
        if kind == "number":
            return ("lit", float(text) if "." in text else int(text))
        if kind == "string":
            return ("lit", decode_string(text))
        if kind == "name":
            constants = {"true": True, "True": True, "false": False, "False": False, "none": None, "None": None}
            return ("lit", constants[text]) if text in constants else ("name", text)
        if text == "(":
            node = self.conditional()
            self.expect(")")
            return node
        if text == "[":
            items = []
            while not self.accept("]"):
                items.append(self.conditional())
                if not self.accept(","):
                    self.expect("]")
                    break
            return ("list", items)
        raise self.fail(f"unexpected {text or 'end of expression'!r}")

    def postfix(self, node: Any) -> Any:
        while True:
            if self.accept("."):
                kind, name = self.take()
                if kind not in ("name", "number"):
                    raise self.fail(f"expected an attribute name after ., got {name!r}")
                node = ("attr", node, name)
            elif self.peek() == ("op", "["):
                self.take()
                key = self.conditional()
                self.expect("]")
                node = ("item", node, key)
            else:
                return node


def decode_string(text: str) -> str:
    body, out, index = text[1:-1], [], 0
    while index < len(body):
        if body[index] == "\\" and index + 1 < len(body):
            out.append(ESCAPES.get(body[index + 1], "\\" + body[index + 1]))
            index += 2
        else:
            out.append(body[index])
            index += 1
    return "".join(out)


def to_text(value: Any) -> str:
    return value if isinstance(value, str) else str(value)


def tojson(value: Any) -> str:
    text = json.dumps(value, sort_keys=True)
    return text.replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026").replace("'", "\\u0027")


def regex_search(value: Any, pattern: str, ignorecase: bool = False, multiline: bool = False) -> Optional[str]:
    flags = (re.I if ignorecase else 0) | (re.M if multiline else 0)
    match = re.search(pattern, to_text(value), flags)
    return match.group(0) if match else None


def title(value: Any) -> str:
    return "".join(part[:1].upper() + part[1:].lower() for part in re.split(r"([-\s(\[{<]+)", to_text(value)))


FILTERS: Dict[str, Callable[..., Any]] = {
    "lower": lambda v: to_text(v).lower(),
    "upper": lambda v: to_text(v).upper(),
    "title": title,
    "capitalize": lambda v: to_text(v)[:1].upper() + to_text(v)[1:].lower(),
    "trim": lambda v: to_text(v).strip(),
    "length": len,
    "count": len,
    "join": lambda v, sep="": to_text(sep).join(to_text(i) for i in v),
    "replace": lambda v, old, new: to_text(v).replace(old, new),
    "first": lambda v: v[0],
    "last": lambda v: v[-1],
    "int": lambda v: int(v),
    "string": to_text,
    "tojson": tojson,
    "to_nice_yaml": lambda v, indent=4: nice_yaml(v, indent),
    "regex_search": regex_search,
}


class Template:
    def __init__(self, source: str, where: str = "<template>", trim_blocks: bool = False, lstrip_blocks: bool = False) -> None:
        self.where = where
        self.tokens = tokenize(source, trim_blocks, lstrip_blocks, where)
        self.index = 0
        self.body = self.parse_body(())
        if self.index < len(self.tokens):
            token = self.tokens[self.index]
            raise TemplateError(f"{where}:{token.line}: unexpected {{% {token.value} %}}")

    def expr(self, source: str, line: int) -> Any:
        return ExprParser(source, f"{self.where}:{line}").parse()

    def parse_body(self, stops: Tuple[str, ...]) -> List[Any]:
        nodes: List[Any] = []
        while self.index < len(self.tokens):
            token = self.tokens[self.index]
            if token.kind == "text":
                nodes.append(("text", token.value))
            elif token.kind == "var":
                nodes.append(("out", self.expr(token.value, token.line), token.line))
            else:
                keyword = token.value.split(None, 1)[0] if token.value else ""
                if keyword in stops:
                    return nodes
                self.index += 1
                nodes.append(self.statement(keyword, token))
                continue
            self.index += 1
        if stops:
            raise TemplateError(f"{self.where}: missing {{% {stops[-1]} %}}")
        return nodes

    def statement(self, keyword: str, token: Token) -> Any:
        rest = token.value[len(keyword):].strip()
        if keyword == "if":
            branches = [(self.expr(rest, token.line), self.parse_body(("elif", "else", "endif")))]
            otherwise: List[Any] = []
            while True:
                closing = self.tokens[self.index]
                self.index += 1
                word = closing.value.split(None, 1)[0]
                if word == "elif":
                    branches.append((self.expr(closing.value[4:], closing.line), self.parse_body(("elif", "else", "endif"))))
                elif word == "else":
                    otherwise = self.parse_body(("endif",))
                else:
                    return ("if", branches, otherwise)
        if keyword == "for":
            match = re.match(r"^([A-Za-z_][A-Za-z0-9_]*(?:\s*,\s*[A-Za-z_][A-Za-z0-9_]*)*)\s+in\s+(.+)$", rest)
            if match is None:
                raise TemplateError(f"{self.where}:{token.line}: expected 'for NAME in EXPRESSION'")
            names = [n.strip() for n in match.group(1).split(",")]
            body = self.parse_body(("else", "endfor"))
            otherwise = []
            if self.tokens[self.index].value.split(None, 1)[0] == "else":
                self.index += 1
                otherwise = self.parse_body(("endfor",))
            self.index += 1
            return ("for", names, self.expr(match.group(2), token.line), body, otherwise, token.line)
        if keyword == "set":
            match = re.match(r"^([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.+)$", rest)
            if match is None:
                raise TemplateError(f"{self.where}:{token.line}: expected 'set NAME = EXPRESSION'")
            return ("set", match.group(1), self.expr(match.group(2), token.line))
        raise TemplateError(f"{self.where}:{token.line}: unsupported tag {{% {keyword} %}}; this subset has if, elif, else, for, set and raw")

    def render(self, context: Dict[str, Any]) -> str:
        out: List[str] = []
        self.run(self.body, dict(context), out)
        return "".join(out)

    def run(self, nodes: List[Any], scope: Dict[str, Any], out: List[str]) -> None:
        for node in nodes:
            kind = node[0]
            if kind == "text":
                out.append(node[1])
            elif kind == "out":
                out.append(to_text(self.value(node[1], scope, node[2])))
            elif kind == "if":
                for test, body in node[1]:
                    if self.value(test, scope, 0):
                        self.run(body, scope, out)
                        break
                else:
                    self.run(node[2], scope, out)
            elif kind == "for":
                names, items = node[1], list(self.value(node[2], scope, node[5]))
                for index, item in enumerate(items):
                    inner = dict(scope)
                    values = [item] if len(names) == 1 else list(item)
                    inner.update(zip(names, values))
                    loop = {"index": index + 1, "index0": index, "first": index == 0, "last": index == len(items) - 1,
                            "length": len(items), "revindex": len(items) - index, "revindex0": len(items) - index - 1}
                    if index > 0:
                        loop["previtem"] = items[index - 1]
                    if index + 1 < len(items):
                        loop["nextitem"] = items[index + 1]
                    inner["loop"] = loop
                    self.run(node[3], inner, out)
                if not items:
                    self.run(node[4], scope, out)
            elif kind == "set":
                scope[node[1]] = self.value(node[2], scope, 0)

    def value(self, node: Any, scope: Dict[str, Any], line: int) -> Any:
        try:
            return evaluate(node, scope)
        except UndefinedError as error:
            raise UndefinedError(f"{self.where}: {error}") from None
        except TemplateError:
            raise
        except (TypeError, ValueError, KeyError, IndexError, re.error) as error:
            raise TemplateError(f"{self.where}: {type(error).__name__}: {error}") from None


def evaluate(node: Any, scope: Dict[str, Any]) -> Any:
    kind = node[0]
    if kind == "lit":
        return node[1]
    if kind == "name":
        if node[1] not in scope:
            raise UndefinedError(f"{node[1]!r} is undefined")
        return scope[node[1]]
    if kind == "list":
        return [evaluate(item, scope) for item in node[1]]
    if kind in ("attr", "item"):
        target = evaluate(node[1], scope)
        key = node[2] if kind == "attr" else evaluate(node[2], scope)
        if isinstance(target, dict):
            if key not in target:
                raise UndefinedError(f"{key!r} is undefined on {node_name(node[1])}")
            return target[key]
        if isinstance(target, (list, str)) and (isinstance(key, int) or (isinstance(key, str) and key.isdigit())):
            return target[int(key)]
        raise UndefinedError(f"{key!r} is undefined on {node_name(node[1])}")
    if kind == "not":
        return not evaluate(node[1], scope)
    if kind == "and":
        left = evaluate(node[1], scope)
        return evaluate(node[2], scope) if left else left
    if kind == "or":
        left = evaluate(node[1], scope)
        return left if left else evaluate(node[2], scope)
    if kind == "cond":
        return evaluate(node[2], scope) if evaluate(node[1], scope) else evaluate(node[3], scope)
    if kind == "undefined":
        return ""
    if kind == "cmp":
        op, left, right = node[1], evaluate(node[2], scope), evaluate(node[3], scope)
        return {"==": lambda: left == right, "!=": lambda: left != right, "<": lambda: left < right, "<=": lambda: left <= right,
                ">": lambda: left > right, ">=": lambda: left >= right, "in": lambda: left in right, "not in": lambda: left not in right}[op]()
    if kind == "bin":
        op, left, right = node[1], evaluate(node[2], scope), evaluate(node[3], scope)
        return {"+": lambda: left + right, "-": lambda: left - right, "*": lambda: left * right, "/": lambda: left / right,
                "//": lambda: left // right, "%": lambda: left % right}[op]()
    if kind == "neg":
        return -evaluate(node[1], scope)
    if kind == "concat":
        return "".join(to_text(evaluate(part, scope)) for part in node[1])
    if kind == "test":
        return run_test(node, scope)
    if kind == "filter":
        return run_filter(node, scope)
    raise TemplateError(f"cannot evaluate {kind}")


def node_name(node: Any) -> str:
    if node[0] == "name":
        return node[1]
    if node[0] == "attr":
        return node_name(node[1]) + "." + node[2]
    return "value"


def run_test(node: Any, scope: Dict[str, Any]) -> bool:
    _, target, name, negated = node
    if name in ("defined", "undefined"):
        try:
            evaluate(target, scope)
            result = name == "defined"
        except UndefinedError:
            result = name == "undefined"
    else:
        value = evaluate(target, scope)
        tests = {"none": value is None, "string": isinstance(value, str), "number": isinstance(value, (int, float)) and not isinstance(value, bool),
                 "true": value is True, "false": value is False}
        if name not in tests:
            raise TemplateError(f"unsupported test 'is {name}'")
        result = tests[name]
    return not result if negated else result


def run_filter(node: Any, scope: Dict[str, Any]) -> Any:
    _, target, name, args, kwargs = node
    values = [evaluate(a, scope) for a in args]
    options = {k: evaluate(v, scope) for k, v in kwargs.items()}
    if name in ("default", "d"):
        try:
            value = evaluate(target, scope)
        except UndefinedError:
            return values[0] if values else ""
        return (values[0] if values else "") if (values[1:2] == [True] and not value) else value
    if name not in FILTERS:
        raise TemplateError(f"unsupported filter {name!r}; this subset has {', '.join(sorted(FILTERS))} and default")
    return FILTERS[name](evaluate(target, scope), *values, **options)


def render_string(text: Any, context: Dict[str, Any], where: str) -> Any:
    if not isinstance(text, str) or ("{{" not in text and "{%" not in text):
        return text
    return Template(text, where).render(context)


@dataclass
class Question:
    name: str
    type: str
    help: str = ""
    default: Any = None
    choices: Optional[List[Any]] = None
    validator: Optional[str] = None
    when: Any = True
    secret: bool = False


@dataclass
class Task:
    command: str
    when: Any = True


@dataclass
class TemplateConfig:
    questions: List[Question]
    subdirectory: str = ""
    suffix: str = ".jinja"
    trim_blocks: bool = False
    lstrip_blocks: bool = False
    keep_trailing_newline: bool = True
    tasks: List[Task] = field(default_factory=list)
    exclude: List[str] = field(default_factory=list)

    def excluded(self, path: str) -> bool:
        patterns = list(self.exclude) + ([] if self.subdirectory else list(DEFAULT_EXCLUDE))
        parts = path.split("/")
        for pattern in patterns:
            if "/" in pattern.strip("/"):
                if fnmatch.fnmatchcase(path, pattern.strip("/")):
                    return True
            elif any(fnmatch.fnmatchcase(part, pattern.strip("/")) for part in parts):
                return True
        return False


def infer_type(default: Any) -> str:
    if isinstance(default, bool):
        return "bool"
    if isinstance(default, int):
        return "int"
    if isinstance(default, float):
        return "float"
    if isinstance(default, (list, dict)):
        return "yaml"
    return "str"


def load_config(text: str, where: str = "copier.yml") -> TemplateConfig:
    try:
        data = parse_yaml(text) or {}
    except YamlError as error:
        raise TemplateError(f"{where}: {error}") from None
    if not isinstance(data, dict):
        raise TemplateError(f"{where}: must be a mapping of settings and questions")
    config = TemplateConfig(questions=[])
    for key, value in data.items():
        if key.startswith("_"):
            if key not in SETTINGS:
                raise TemplateError(f"{where}: unsupported setting {key}; this subset knows {', '.join(SETTINGS)}")
            if key == "_subdirectory":
                config.subdirectory = str(value or "").strip("/")
            elif key == "_templates_suffix":
                config.suffix = str(value or "")
            elif key == "_envops":
                unknown = sorted(set(value or {}) - set(ENVOPS))
                if unknown:
                    raise TemplateError(f"{where}: unsupported _envops {', '.join(unknown)}; this subset knows {', '.join(ENVOPS)}")
                config.trim_blocks = bool((value or {}).get("trim_blocks", False))
                config.lstrip_blocks = bool((value or {}).get("lstrip_blocks", False))
                config.keep_trailing_newline = bool((value or {}).get("keep_trailing_newline", True))
            elif key == "_tasks":
                for item in value or []:
                    if isinstance(item, str):
                        config.tasks.append(Task(item))
                    elif isinstance(item, dict) and isinstance(item.get("command"), str):
                        config.tasks.append(Task(item["command"], item.get("when", True)))
                    else:
                        raise TemplateError(f"{where}: each _tasks entry is a command string or a mapping with command and when")
            elif key == "_exclude":
                config.exclude = [str(p) for p in value or []]
            continue
        if not re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", key):
            raise TemplateError(f"{where}: question name {key!r} must be a valid identifier")
        if not isinstance(value, dict):
            config.questions.append(Question(key, infer_type(value), default=value))
            continue
        unknown = sorted(set(value) - set(QUESTION_KEYS))
        if unknown:
            raise TemplateError(f"{where}: question {key}: unsupported key(s) {', '.join(unknown)}")
        kind = value.get("type") or infer_type(value.get("default"))
        if kind not in TYPES:
            raise TemplateError(f"{where}: question {key}: type {kind!r} is not one of {', '.join(TYPES)}")
        choices = value.get("choices")
        if isinstance(choices, dict):
            choices = list(choices.values())
        config.questions.append(Question(key, kind, str(value.get("help") or ""), value.get("default"), choices,
                                         value.get("validator"), value.get("when", True), bool(value.get("secret", False))))
    return config


def cast_bool(value: Any) -> bool:
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in ("y", "yes", "t", "true", "on", "1"):
            return True
        if lowered in ("n", "no", "f", "false", "off", "0", "", "~", "null", "none"):
            return False
        raise ValueError(f"{value!r} is not a boolean (use true or false)")
    return bool(value)


def cast(question: Question, value: Any) -> Any:
    kind = question.type
    if kind == "str":
        return value if isinstance(value, str) else to_text(value)
    if kind == "bool":
        return cast_bool(value)
    if kind == "int":
        if isinstance(value, bool) or (isinstance(value, float) and not value.is_integer()):
            raise ValueError(f"{value!r} is not an integer")
        return int(value)
    if kind == "float":
        return float(value)
    if kind == "json":
        return json.loads(value) if isinstance(value, str) else value
    if isinstance(value, str):
        try:
            return parse_scalar(value.strip(), 1) if value.strip() else None
        except YamlError as error:
            raise ValueError(f"{value!r} is not YAML this subset reads: {error}") from None
    return value


def resolve_answers(config: TemplateConfig, data: Dict[str, Any], previous: Optional[Dict[str, Any]] = None,
                    extra: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    previous = previous or {}
    names = {q.name for q in config.questions}
    problems = [f"{name}: not a question of this template (questions: {', '.join(q.name for q in config.questions)})" for name in data if name not in names]
    answers: Dict[str, Any] = {}
    context = dict(extra or {})
    for question in config.questions:
        where = f"copier.yml {question.name}"
        try:
            asked = cast_bool(render_string(question.when, {**context, **answers}, where + " when"))
        except (TemplateError, ValueError) as error:
            problems.append(f"{question.name}: when: {error}")
            continue
        if not asked:
            continue
        try:
            if question.name in data:
                value = cast(question, data[question.name])
            elif question.name in previous:
                value = cast(question, previous[question.name])
            elif question.default is not None:
                value = cast(question, render_string(question.default, {**context, **answers}, where + " default"))
            else:
                problems.append(f"{question.name}: required ({question.type}{': ' + question.help if question.help else ''})")
                continue
        except (TemplateError, ValueError, TypeError) as error:
            problems.append(f"{question.name}: {error}")
            continue
        if question.choices is not None:
            allowed = [cast(question, c) for c in question.choices]
            if value not in allowed:
                problems.append(f"{question.name}: {value!r} is not one of {', '.join(map(to_text, allowed))}")
                continue
        if question.validator:
            try:
                message = to_text(render_string(question.validator, {**context, **answers, question.name: value}, where + " validator")).strip()
            except TemplateError as error:
                message = f"validator failed: {error}"
            if message:
                problems.append(f"{question.name}: {value!r} {message}")
                continue
        answers[question.name] = value
    if problems:
        raise AnswerError(problems)
    return answers


@dataclass
class OutFile:
    data: bytes
    executable: bool = False


@dataclass
class Snapshot:
    files: Dict[str, OutFile]
    src_path: str
    label: Optional[str]
    sha: Optional[str]

    def config(self) -> TemplateConfig:
        for name in CONFIG_FILES:
            if name in self.files:
                return load_config(self.files[name].data.decode("utf-8"), name)
        raise TemplateError(f"{self.src_path}: no copier.yml at the root of the template")


def run_command(argv: List[str], cwd: Optional[str]) -> Tuple[int, bytes, str]:
    try:
        result = subprocess.run(argv, cwd=cwd, capture_output=True)
    except FileNotFoundError:
        return 127, b"", f"{argv[0]}: command not found"
    return result.returncode, result.stdout, result.stderr.decode("utf-8", "replace")


def run_shell(command: str, cwd: str) -> Tuple[int, str]:
    result = subprocess.run(command, shell=True, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    return result.returncode, result.stdout.decode("utf-8", "replace")


def git(runner: Runner, cwd: str, *args: str) -> str:
    code, out, err = runner(["git", *args], cwd)
    if code != 0:
        raise GitError(f"git {' '.join(args)} in {cwd}: {err.strip() or f'exit {code}'}")
    return out.decode("utf-8", "replace")


def is_git_repo(path: str) -> bool:
    return os.path.exists(os.path.join(path, ".git")) or (os.path.isfile(os.path.join(path, "HEAD")) and os.path.isdir(os.path.join(path, "objects")))


def version_key(tag: str) -> Optional[Tuple[int, int, int]]:
    match = VERSION_TAG.match(tag)
    return (int(match.group(1)), int(match.group(2)), int(match.group(3))) if match else None


def latest_tag(src: str, runner: Runner) -> Optional[str]:
    tags = [t for t in git(runner, src, "tag", "--list").split() if version_key(t)]
    return max(tags, key=version_key) if tags else None


def load_dir(root: str) -> Dict[str, OutFile]:
    files: Dict[str, OutFile] = {}
    for current, dirs, names in os.walk(root):
        dirs[:] = sorted(d for d in dirs if d != ".git")
        for name in sorted(names):
            path = os.path.join(current, name)
            rel = os.path.relpath(path, root).replace(os.sep, "/")
            with open(path, "rb") as handle:
                files[rel] = OutFile(handle.read(), bool(os.stat(path).st_mode & 0o111))
    return files


def open_template(src: str, ref: Optional[str], runner: Runner = run_command) -> Snapshot:
    if not os.path.isdir(src):
        raise ScaffoldError(f"template {src}: not a directory")
    if not is_git_repo(src):
        if ref:
            raise ScaffoldError(f"template {src} is not a Git repository, so --ref {ref} cannot be resolved")
        return Snapshot(load_dir(src), src, None, None)
    ref = ref or latest_tag(src, runner) or "HEAD"
    code, out, _ = runner(["git", "rev-parse", "--verify", "--quiet", ref + "^{commit}"], src)
    if code != 0:
        raise ScaffoldError(f"template {src}: no commit or tag named {ref}")
    sha = out.decode().strip()
    tagged = git(runner, src, "tag", "--points-at", sha).split()
    label = ref if ref in tagged else git(runner, src, "describe", "--tags", "--always", sha).strip()
    code, archive, err = runner(["git", "archive", "--format=tar", sha], src)
    if code != 0:
        raise GitError(f"git archive {sha} in {src}: {err.strip()}")
    files: Dict[str, OutFile] = {}
    with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
        for member in tar.getmembers():
            if member.isfile():
                handle = tar.extractfile(member)
                files[member.name] = OutFile(handle.read() if handle else b"", bool(member.mode & 0o111))
    return Snapshot(files, src, label, sha)


def render_project(snapshot: Snapshot, config: TemplateConfig, answers: Dict[str, Any], operation: str, folder: str) -> Dict[str, OutFile]:
    remembered: Dict[str, Any] = {}
    if snapshot.label:
        remembered["_commit"] = snapshot.label
    remembered["_src_path"] = snapshot.src_path
    secret = {q.name for q in config.questions if q.secret}
    remembered.update({k: v for k, v in answers.items() if k not in secret})
    context = dict(answers)
    context.update({"_copier_answers": remembered, "_copier_operation": operation, "_folder_name": folder,
                    "_copier_conf": {"answers_file": ANSWERS_FILE, "vcs_ref_hash": snapshot.sha or "", "src_path": snapshot.src_path}})
    prefix = config.subdirectory + "/" if config.subdirectory else ""
    out: Dict[str, OutFile] = {}
    origin: Dict[str, str] = {}
    for path in sorted(snapshot.files):
        if not path.startswith(prefix) or path in CONFIG_FILES and not prefix:
            continue
        rel = path[len(prefix):]
        if config.excluded(rel):
            continue
        templated = bool(config.suffix) and rel.endswith(config.suffix)
        name = rel[: -len(config.suffix)] if templated else rel
        parts = [Template(part, f"{path} (name)", config.trim_blocks, config.lstrip_blocks).render(context) for part in name.split("/")]
        if any(not part for part in parts):
            continue
        target = "/".join(parts)
        if target in out:
            raise TemplateError(f"{path} and {origin[target]} both render to {target}")
        data = snapshot.files[path].data
        if templated:
            try:
                text = data.decode("utf-8")
            except UnicodeDecodeError:
                raise TemplateError(f"{path}: a {config.suffix} file must be UTF-8 text") from None
            text = Template(text, path, config.trim_blocks, config.lstrip_blocks).render(context)
            if not config.keep_trailing_newline and text.endswith("\n"):
                text = text[:-1]
            data = text.encode("utf-8")
        out[target] = OutFile(data, snapshot.files[path].executable)
        origin[target] = path
    return out


def check_rendered(files: Dict[str, OutFile]) -> List[str]:
    problems = []
    for path, item in sorted(files.items()):
        if path.endswith((".yaml", ".yml")):
            try:
                parse_documents(item.data.decode("utf-8"))
            except (YamlError, UnicodeDecodeError) as error:
                problems.append(f"{path}: rendered YAML does not parse: {error}")
        elif path.endswith(".json"):
            try:
                json.loads(item.data.decode("utf-8"))
            except ValueError as error:
                problems.append(f"{path}: rendered JSON does not parse: {error}")
    return problems


def write_file(dest: str, path: str, item: OutFile) -> None:
    full = os.path.join(dest, *path.split("/"))
    os.makedirs(os.path.dirname(full), exist_ok=True)
    with open(full, "wb") as handle:
        handle.write(item.data)
    mode = os.stat(full).st_mode
    os.chmod(full, (mode | 0o111) if item.executable else (mode & ~0o111))


def read_file(dest: str, path: str) -> Optional[bytes]:
    full = os.path.join(dest, *path.split("/"))
    if not os.path.isfile(full):
        return None
    with open(full, "rb") as handle:
        return handle.read()


def run_tasks(config: TemplateConfig, context: Dict[str, Any], dest: str, shell: Shell) -> List[Dict[str, Any]]:
    ran = []
    for number, task in enumerate(config.tasks, start=1):
        if not cast_bool(render_string(task.when, context, f"_tasks[{number}] when")):
            continue
        command = render_string(task.command, context, f"_tasks[{number}]")
        code, output = shell(command, dest)
        ran.append({"command": command, "exit": code, "output": output.strip()})
        if code != 0:
            raise ScaffoldError(f"task {number} failed with exit {code}: {command}\n{output.strip()}")
    return ran


def check_trust(config: TemplateConfig, trust: bool, skip_tasks: bool) -> None:
    if config.tasks and not trust and not skip_tasks:
        commands = "; ".join(t.command for t in config.tasks)
        raise ScaffoldError(f"the template runs commands after rendering ({commands}); pass --trust if you trust it, or --skip-tasks")


@dataclass
class NewResult:
    dest: str
    version: Optional[str]
    answers: Dict[str, Any]
    files: List[str]
    tasks: List[Dict[str, Any]]
    dry_run: bool

    def to_dict(self) -> Dict[str, Any]:
        return {"dest": self.dest, "version": self.version, "answers": self.answers, "files": self.files, "tasks": self.tasks, "dry_run": self.dry_run}


def generate(src: str, dest: str, data: Dict[str, Any], ref: Optional[str] = None, trust: bool = False, skip_tasks: bool = False,
             dry_run: bool = False, runner: Runner = run_command, shell: Shell = run_shell) -> NewResult:
    if os.path.isdir(dest) and os.listdir(dest):
        raise ScaffoldError(f"{dest} exists and is not empty; use 'update' for a service that was already generated")
    snapshot = open_template(src, ref, runner)
    config = snapshot.config()
    check_trust(config, trust, skip_tasks)
    folder = os.path.basename(os.path.abspath(dest))
    answers = resolve_answers(config, data, extra={"_copier_operation": "copy", "_folder_name": folder})
    files = render_project(snapshot, config, answers, "copy", folder)
    problems = check_rendered(files)
    if problems:
        raise TemplateError("the template rendered broken files:\n" + "\n".join("  " + p for p in problems))
    tasks: List[Dict[str, Any]] = []
    if not dry_run:
        os.makedirs(dest, exist_ok=True)
        for path, item in files.items():
            write_file(dest, path, item)
        if not skip_tasks:
            context = dict(answers, _copier_operation="copy", _folder_name=folder)
            tasks = run_tasks(config, context, dest, shell)
    return NewResult(dest, snapshot.label, answers, sorted(files), tasks, dry_run)


def lcs_pairs(a: Sequence[str], b: Sequence[str]) -> List[Tuple[int, int]]:
    start = 0
    while start < len(a) and start < len(b) and a[start] == b[start]:
        start += 1
    end_a, end_b = len(a), len(b)
    while end_a > start and end_b > start and a[end_a - 1] == b[end_b - 1]:
        end_a -= 1
        end_b -= 1
    mid_a, mid_b = a[start:end_a], b[start:end_b]
    rows, cols = len(mid_a), len(mid_b)
    table = [[0] * (cols + 1) for _ in range(rows + 1)]
    for i in range(rows - 1, -1, -1):
        row, below = table[i], table[i + 1]
        for j in range(cols - 1, -1, -1):
            row[j] = below[j + 1] + 1 if mid_a[i] == mid_b[j] else max(below[j], row[j + 1])
    pairs = [(k, k) for k in range(start)]
    i = j = 0
    while i < rows and j < cols:
        if mid_a[i] == mid_b[j]:
            pairs.append((start + i, start + j))
            i += 1
            j += 1
        elif table[i + 1][j] >= table[i][j + 1]:
            i += 1
        else:
            j += 1
    pairs += [(end_a + k, end_b + k) for k in range(len(a) - end_a)]
    return pairs


@dataclass
class Hunk:
    base_start: int
    base_end: int
    side_start: int
    side_end: int
    side: str


def hunks(base: Sequence[str], other: Sequence[str], side: str) -> List[Hunk]:
    out: List[Hunk] = []
    previous_base = previous_other = 0
    for base_index, other_index in lcs_pairs(base, other) + [(len(base), len(other))]:
        if base_index > previous_base or other_index > previous_other:
            out.append(Hunk(previous_base, base_index, previous_other, other_index, side))
        previous_base, previous_other = base_index + 1, other_index + 1
    return out


def side_lines(lines: Sequence[str], group: List[Hunk], start: int, end: int, base: Sequence[str]) -> List[str]:
    if not group:
        return list(base[start:end])
    first, last = group[0], group[-1]
    return list(lines[first.side_start - (first.base_start - start) : last.side_end + (end - last.base_end)])


def with_newline(lines: List[str]) -> List[str]:
    return lines[:-1] + [lines[-1] + "\n"] if lines and not lines[-1].endswith("\n") else lines


def merge3(base: str, ours: str, theirs: str) -> Tuple[str, int]:
    b, o, t = base.splitlines(True), ours.splitlines(True), theirs.splitlines(True)
    changes = sorted(hunks(b, o, "ours") + hunks(b, t, "theirs"), key=lambda h: (h.base_start, h.base_end))
    out: List[str] = []
    conflicts = cursor = index = 0
    while index < len(changes):
        group = [changes[index]]
        start, end = changes[index].base_start, changes[index].base_end
        index += 1
        while index < len(changes) and changes[index].base_start <= end:
            group.append(changes[index])
            end = max(end, changes[index].base_end)
            index += 1
        out.extend(b[cursor:start])
        cursor = end
        mine = [h for h in group if h.side == "ours"]
        other = [h for h in group if h.side == "theirs"]
        ours_text = side_lines(o, mine, start, end, b)
        theirs_text = side_lines(t, other, start, end, b)
        if not mine or ours_text == theirs_text:
            out.extend(theirs_text)
        elif not other:
            out.extend(ours_text)
        else:
            head = 0
            while head < min(len(ours_text), len(theirs_text)) and ours_text[head] == theirs_text[head]:
                head += 1
            tail = 0
            while tail < min(len(ours_text), len(theirs_text)) - head and ours_text[-1 - tail] == theirs_text[-1 - tail]:
                tail += 1
            out.extend(ours_text[:head])
            out.append(MARK_OURS + "\n")
            out.extend(with_newline(ours_text[head : len(ours_text) - tail]))
            out.append(MARK_SPLIT + "\n")
            out.extend(with_newline(theirs_text[head : len(theirs_text) - tail]))
            out.append(MARK_THEIRS + "\n")
            out.extend(ours_text[len(ours_text) - tail :])
            conflicts += 1
    out.extend(b[cursor:])
    return "".join(out), conflicts


@dataclass
class Change:
    path: str
    action: str
    detail: str = ""


@dataclass
class UpdateResult:
    dest: str
    from_version: str
    to_version: str
    changes: List[Change]
    answers: Dict[str, Any]
    dry_run: bool

    @property
    def conflicts(self) -> List[Change]:
        return [c for c in self.changes if c.action in ("conflict", "orphaned")]

    def to_dict(self) -> Dict[str, Any]:
        return {"dest": self.dest, "from": self.from_version, "to": self.to_version, "dry_run": self.dry_run, "answers": self.answers,
                "changes": [{"path": c.path, "action": c.action, "detail": c.detail} for c in self.changes],
                "conflicts": [c.path for c in self.conflicts]}


def plan_file(path: str, base: Optional[OutFile], ours: Optional[bytes], theirs: Optional[OutFile]) -> Tuple[Change, Optional[bytes]]:
    if base is None:
        if theirs is None:
            return Change(path, "unchanged"), None
        if ours is None:
            return Change(path, "added", "new in the template"), theirs.data
        if ours == theirs.data:
            return Change(path, "unchanged", "added on both sides with the same content"), None
        return merge_texts(path, b"", ours, theirs.data, "added on both sides")
    if theirs is None:
        if ours is None:
            return Change(path, "unchanged", "deleted on both sides"), None
        if ours == base.data:
            return Change(path, "removed", "deleted from the template"), None
        return Change(path, "orphaned", "deleted from the template but changed here; kept, decide by hand"), None
    if ours is None:
        if theirs.data == base.data:
            return Change(path, "unchanged", "deleted here, unchanged in the template"), None
        return Change(path, "skipped", "deleted here, changed in the template; not recreated"), None
    if ours == base.data:
        if theirs.data == base.data:
            return Change(path, "unchanged"), None
        return Change(path, "updated", "template change, no local edits"), theirs.data
    if theirs.data == base.data:
        return Change(path, "kept", "local edits, template unchanged"), None
    if ours == theirs.data:
        return Change(path, "unchanged", "same change on both sides"), None
    return merge_texts(path, base.data, ours, theirs.data, "")


def merge_texts(path: str, base: bytes, ours: bytes, theirs: bytes, note: str) -> Tuple[Change, Optional[bytes]]:
    try:
        texts = [base.decode("utf-8"), ours.decode("utf-8"), theirs.decode("utf-8")]
    except UnicodeDecodeError:
        return Change(path, "conflict", "binary file changed on both sides; kept the local version"), None
    merged, conflicts = merge3(*texts)
    prefix = note + "; " if note else ""
    if conflicts:
        return Change(path, "conflict", f"{prefix}{conflicts} conflicting hunk(s) marked with {MARK_OURS!r}"), merged.encode("utf-8")
    return Change(path, "merged", f"{prefix}local edits and template change combined"), merged.encode("utf-8")


def read_answers(dest: str) -> Dict[str, Any]:
    raw = read_file(dest, ANSWERS_FILE)
    if raw is None:
        raise ScaffoldError(f"{dest}: no {ANSWERS_FILE}; this directory was not generated from a template")
    try:
        data = parse_yaml(raw.decode("utf-8")) or {}
    except YamlError as error:
        raise ScaffoldError(f"{dest}/{ANSWERS_FILE}: {error}") from None
    if not isinstance(data, dict) or not data.get("_src_path"):
        raise ScaffoldError(f"{dest}/{ANSWERS_FILE}: missing _src_path")
    if not data.get("_commit"):
        raise ScaffoldError(f"{dest}/{ANSWERS_FILE}: no _commit; the service was generated from a template that is not a Git repository, so there is no old version to merge against")
    return data


def update(dest: str, data: Optional[Dict[str, Any]] = None, ref: Optional[str] = None, trust: bool = False, skip_tasks: bool = False,
           dry_run: bool = False, src: Optional[str] = None, runner: Runner = run_command, shell: Shell = run_shell) -> UpdateResult:
    data = data or {}
    recorded = read_answers(dest)
    if not is_git_repo(dest):
        raise ScaffoldError(f"{dest}: not a Git repository; an update must be reviewable and revertible, so commit the service to Git first")
    dirty = git(runner, dest, "status", "--porcelain")
    if dirty.strip():
        raise ScaffoldError(f"{dest}: uncommitted changes; commit or stash them first:\n{dirty.rstrip()}")
    src = src or str(recorded["_src_path"])
    old = open_template(src, str(recorded["_commit"]), runner)
    new = open_template(src, ref, runner)
    old_config, new_config = old.config(), new.config()
    check_trust(new_config, trust, skip_tasks)
    folder = os.path.basename(os.path.abspath(dest))
    previous = {k: v for k, v in recorded.items() if not k.startswith("_")}
    extra = {"_copier_operation": "update", "_folder_name": folder}
    base_answers = resolve_answers(old_config, {}, previous, extra)
    new_answers = resolve_answers(new_config, data, previous, extra)
    old.src_path = new.src_path = str(recorded["_src_path"])
    base = render_project(old, old_config, base_answers, "update", folder)
    theirs = render_project(new, new_config, new_answers, "update", folder)
    problems = check_rendered(theirs)
    if problems:
        raise TemplateError("the new template version renders broken files:\n" + "\n".join("  " + p for p in problems))
    changes: List[Change] = []
    writes: Dict[str, bytes] = {}
    removals: List[str] = []
    for path in sorted(set(base) | set(theirs)):
        if path == ANSWERS_FILE:
            continue
        change, content = plan_file(path, base.get(path), read_file(dest, path), theirs.get(path))
        changes.append(change)
        if content is not None:
            writes[path] = content
        if change.action == "removed":
            removals.append(path)
    if ANSWERS_FILE in theirs:
        writes[ANSWERS_FILE] = theirs[ANSWERS_FILE].data
    if not dry_run:
        for path, content in writes.items():
            write_file(dest, path, OutFile(content, theirs[path].executable if path in theirs else False))
        for path in removals:
            os.remove(os.path.join(dest, *path.split("/")))
        if not skip_tasks:
            run_tasks(new_config, dict(new_answers, **extra), dest, shell)
    return UpdateResult(dest, str(recorded["_commit"]), new.label or "HEAD", changes, new_answers, dry_run)


def parse_data(pairs: Sequence[str], data_file: Optional[str]) -> Dict[str, Any]:
    data: Dict[str, Any] = {}
    if data_file:
        with open(data_file, encoding="utf-8") as handle:
            loaded = parse_yaml(handle.read()) or {}
        if not isinstance(loaded, dict):
            raise ScaffoldError(f"{data_file}: must be a mapping of answers")
        data.update(loaded)
    for pair in pairs:
        key, sep, value = pair.partition("=")
        if not sep or not key:
            raise ScaffoldError(f"--data {pair!r}: expected KEY=VALUE")
        data[key.strip()] = value
    return data


def build_template_repo(work: str, runner: Runner = run_command) -> str:
    repo = os.path.join(work, "service-template")
    shutil.copytree(LESSON_TEMPLATE, repo, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    identity = ["-c", "user.name=Platform Team", "-c", "user.email=platform@example.invalid", "-c", "commit.gpgsign=false", "-c", "tag.gpgsign=false"]
    git(runner, repo, "-c", "init.defaultBranch=main", "init", "--quiet")
    git(runner, repo, "add", "--all")
    git(runner, repo, *identity, "commit", "--quiet", "--message", "Release service template 1.0.0")
    git(runner, repo, *identity, "tag", "v1.0.0")
    git(runner, repo, "apply", LESSON_RELEASE)
    git(runner, repo, "add", "--all")
    git(runner, repo, *identity, "commit", "--quiet", "--message", "Release service template 1.1.0: probes, resources, Python 3.14")
    git(runner, repo, *identity, "tag", "v1.1.0")
    return repo


def edit(path: str, old: str, new: str) -> None:
    with open(path, encoding="utf-8") as handle:
        text = handle.read()
    if old not in text:
        raise ScaffoldError(f"{path}: expected text not found: {old!r}")
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(text.replace(old, new, 1))


def own_changes(service: str, base_image: bool) -> None:
    edit(os.path.join(service, "app", "main.py"), '            self.reply(200, {"status": "ok"})\n',
         '            self.reply(200, {"status": "ok"})\n        elif self.path == "/version":\n            self.reply(200, {"version": os.environ.get("VERSION", "dev")})\n')
    edit(os.path.join(service, "k8s", "deployment.yaml"), '              value: "8080"\n',
         '              value: "8080"\n            - name: FEATURE_FLAGS\n              value: checkout-v2\n')
    with open(os.path.join(service, "README.md"), "a", encoding="utf-8") as handle:
        handle.write("\n## Runbook\n\nPage the payments on-call; dashboards are linked from the catalog.\n")
    if base_image:
        edit(os.path.join(service, "Dockerfile"), "FROM python:3.13-slim\n", "FROM python:3.13-slim-trixie\n")


def demo(runner: Runner = run_command) -> Dict[str, Any]:
    environment = {"GIT_AUTHOR_NAME": "Dev", "GIT_AUTHOR_EMAIL": "dev@example.invalid", "GIT_COMMITTER_NAME": "Dev",
                   "GIT_COMMITTER_EMAIL": "dev@example.invalid", "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull}
    saved = {k: os.environ.get(k) for k in environment}
    os.environ.update(environment)
    work = tempfile.mkdtemp(prefix="scaffolder-demo-")
    try:
        repo = build_template_repo(work, runner)
        service = os.path.join(work, "checkout")
        answers = {"service_name": "checkout", "owner": "payments", "exposure": "public", "environments": "[staging, canary, production]"}
        created = generate(repo, service, answers, ref="v1.0.0", trust=True, runner=runner)
        own_changes(service, base_image=True)
        git(runner, service, "add", "--all")
        git(runner, service, "commit", "--quiet", "--message", "Add /version, a feature flag, a runbook and a pinned base image")
        result = update(service, ref="v1.1.0", trust=True, runner=runner)
        with open(os.path.join(service, "Dockerfile"), encoding="utf-8") as handle:
            dockerfile = handle.read()
        with open(os.path.join(service, "k8s", "deployment.yaml"), encoding="utf-8") as handle:
            deployment = handle.read()
        return {"created": created.to_dict(), "update": result.to_dict(), "dockerfile": dockerfile,
                "deployment_keeps_flag": "FEATURE_FLAGS" in deployment, "deployment_has_probes": "readinessProbe" in deployment}
    finally:
        shutil.rmtree(work, ignore_errors=True)
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def print_questions(config: TemplateConfig, out: Any) -> None:
    for q in config.questions:
        extras = []
        if q.default is not None:
            extras.append(f"default {q.default!r}")
        if q.choices:
            extras.append("one of " + ", ".join(map(to_text, q.choices)))
        if q.when is not True:
            extras.append(f"asked when {q.when}")
        if q.validator:
            extras.append("validated")
        print(f"{q.name:<14} {q.type:<5} {q.help}" + (f" ({'; '.join(extras)})" if extras else ""), file=out)


def print_update(result: UpdateResult, out: Any) -> None:
    verb = "would update" if result.dry_run else "updated"
    print(f"{verb} {result.dest} from template {result.from_version} to {result.to_version}", file=out)
    for change in result.changes:
        if change.action != "unchanged":
            print(f"  {change.action:<9} {change.path}" + (f"  ({change.detail})" if change.detail else ""), file=out)
    if result.conflicts:
        print(f"{len(result.conflicts)} file(s) need a decision: resolve the {MARK_OURS!r} markers, then git add and commit", file=out)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="scaffolder", description="Generate services from a copier-format template and keep them updated with a three-way merge.")
    sub = parser.add_subparsers(dest="command", required=True)
    questions = sub.add_parser("questions", help="list the template's parameters")
    questions.add_argument("--template", default=LESSON_TEMPLATE)
    questions.add_argument("--ref")
    questions.add_argument("--json", action="store_true")
    new = sub.add_parser("new", help="generate a service")
    new.add_argument("dest")
    new.add_argument("--template", default=LESSON_TEMPLATE)
    new.add_argument("--ref", help="template tag or commit (default: the highest version tag)")
    new.add_argument("-d", "--data", action="append", default=[], metavar="KEY=VALUE")
    new.add_argument("--data-file")
    new.add_argument("--trust", action="store_true", help="allow the template's post-generation tasks to run")
    new.add_argument("--skip-tasks", action="store_true")
    new.add_argument("--dry-run", action="store_true")
    new.add_argument("--json", action="store_true")
    upd = sub.add_parser("update", help="apply a newer template version to a generated service")
    upd.add_argument("dest", nargs="?", default=".")
    upd.add_argument("--ref", help="template version to update to (default: the highest version tag)")
    upd.add_argument("--template", help="template location if it moved since generation")
    upd.add_argument("-d", "--data", action="append", default=[], metavar="KEY=VALUE")
    upd.add_argument("--trust", action="store_true")
    upd.add_argument("--skip-tasks", action="store_true")
    upd.add_argument("--dry-run", action="store_true")
    upd.add_argument("--json", action="store_true")
    show = sub.add_parser("demo", help="generate, change, release 1.1.0 and update, in a temporary directory")
    show.add_argument("--json", action="store_true")
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(sys.argv[1:] if argv is None else argv)
    try:
        if args.command == "questions":
            config = open_template(args.template, args.ref).config()
            if args.json:
                print(json.dumps([q.__dict__ for q in config.questions], indent=2))
            else:
                print_questions(config, sys.stdout)
            return 0
        if args.command == "new":
            result = generate(args.template, args.dest, parse_data(args.data, args.data_file), args.ref, args.trust, args.skip_tasks, args.dry_run)
            if args.json:
                print(json.dumps(result.to_dict(), indent=2))
            else:
                verb = "would create" if result.dry_run else "created"
                print(f"{verb} {result.dest} from template {result.version or '(working directory)'}")
                for path in result.files:
                    print(f"  create  {path}")
                for task in result.tasks:
                    print(f"  task    {task['command']}")
            return 0
        if args.command == "update":
            result = update(args.dest, parse_data(args.data, None), args.ref, args.trust, args.skip_tasks, args.dry_run, args.template)
            if args.json:
                print(json.dumps(result.to_dict(), indent=2))
            else:
                print_update(result, sys.stdout)
            return 1 if result.conflicts else 0
        outcome = demo()
        if args.json:
            print(json.dumps(outcome, indent=2))
        else:
            print(f"generated checkout from template {outcome['created']['version']}: {len(outcome['created']['files'])} files, tasks: "
                  + "; ".join(t["command"] for t in outcome["created"]["tasks"]))
            print("own changes: /version route, FEATURE_FLAGS env var, a runbook section, base image pinned to python:3.13-slim-trixie")
            result = UpdateResult(**{"dest": "checkout", "from_version": outcome["update"]["from"], "to_version": outcome["update"]["to"],
                                     "changes": [Change(**c) for c in outcome["update"]["changes"]], "answers": outcome["update"]["answers"], "dry_run": False})
            print_update(result, sys.stdout)
            print("Dockerfile after the update:")
            print("    " + outcome["dockerfile"].rstrip("\n").replace("\n", "\n    "))
        return 0
    except AnswerError as error:
        print(f"scaffolder: {error}", file=sys.stderr)
        return 2
    except (ScaffoldError, OSError) as error:
        print(f"scaffolder: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
