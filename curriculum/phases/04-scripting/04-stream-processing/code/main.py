"""ministream: a jq subset and an awk subset written from scratch. Both read their input as a stream (one JSON value or one line at a time) so memory stays flat however large the file is, and explain prints the program each one parsed."""
from __future__ import annotations

import argparse
import functools
import json
import math
import re
import signal
import string
import sys
import time
from typing import Any, Callable, Dict, Iterable, Iterator, List, Optional, TextIO, Tuple

EXIT_OK = 0
EXIT_RUNTIME = 1
EXIT_USAGE = 2
READ_CHUNK = 65536
IDENT_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
NOT_SPACE_RE = re.compile(r"\S")
JQ_NUMBER_RE = re.compile(r"\d+(?:\.\d*)?(?:[eE][-+]?\d+)?")
AWK_NUMBER_RE = re.compile(r"(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?")
LEADING_NUMBER_RE = re.compile(r"[ \t\n\r\f\v]*([-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?)")
WHOLE_NUMBER_RE = re.compile(r"[ \t\n\r\f\v]*[-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?[ \t\n\r\f\v]*\Z")
DEFAULT_FS_RE = re.compile(r"[ \t\n]+")
FORMAT_SPEC_RE = re.compile(r"%([-+ #0]*)(\*|\d+)?(?:\.(\*|\d*))?([a-zA-Z%])")
ASCII_LOWER = str.maketrans(string.ascii_uppercase, string.ascii_lowercase)
ASCII_UPPER = str.maketrans(string.ascii_lowercase, string.ascii_uppercase)


class StreamError(Exception):
    exit_code = EXIT_RUNTIME


class ProgramError(StreamError):
    exit_code = EXIT_USAGE

    def __init__(self, source: str, position: int, message: str) -> None:
        line = source.count("\n", 0, position) + 1
        column = position - source.rfind("\n", 0, position)
        super().__init__(f"syntax error at line {line}, column {column}: {message}")
        self.line = line
        self.column = column


class JqError(StreamError):
    pass


class AwkError(StreamError):
    pass


class InputError(StreamError):
    pass


def is_node(value: Any) -> bool:
    return isinstance(value, tuple) and bool(value) and isinstance(value[0], str)


def explain_lines(tree: tuple, depth: int = 0) -> List[str]:
    labels = [json.dumps(part) for part in tree[1:] if not is_node(part) and not isinstance(part, list) and (part is not None or tree[0] == "literal")]
    lines = ["  " * depth + " ".join([tree[0]] + labels)]
    for part in tree[1:]:
        for child in part if isinstance(part, list) else [part]:
            if is_node(child):
                lines.extend(explain_lines(child, depth + 1))
    return lines


def tree_to_json(tree: Any) -> Any:
    if is_node(tree):
        return {"node": tree[0], "args": [tree_to_json(part) for part in tree[1:]]}
    if isinstance(tree, list):
        return [tree_to_json(part) for part in tree]
    return tree


def iter_json_values(stream: Any, chunk_size: int = READ_CHUNK) -> Iterator[Any]:
    decoder = json.JSONDecoder()
    buffer = ""
    position = 0
    consumed = 0
    finished = False
    while True:
        match = NOT_SPACE_RE.search(buffer, position)
        if match is None:
            if finished:
                return
            consumed += len(buffer)
            buffer = stream.read(chunk_size)
            position = 0
            finished = not buffer
            continue
        position = match.start()
        try:
            value, end = decoder.raw_decode(buffer, position)
        except json.JSONDecodeError as error:
            if finished:
                raise InputError(f"invalid JSON input at byte {consumed + error.pos}: {error.msg}") from None
            more = stream.read(max(chunk_size, len(buffer) - position))
            consumed += position
            buffer = buffer[position:] + more
            position = 0
            finished = not more
            continue
        if end == len(buffer) and not finished and is_number(value):
            more = stream.read(chunk_size)
            consumed += position
            buffer = buffer[position:] + more
            position = 0
            finished = not more
            continue
        position = end
        yield value


def is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def jq_type(value: Any) -> str:
    if value is None:
        return "null"
    if value is True or value is False:
        return "boolean"
    if isinstance(value, (int, float)):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    return "object"


def jq_rank(value: Any) -> int:
    if value is None:
        return 0
    if value is False:
        return 1
    if value is True:
        return 2
    if isinstance(value, (int, float)):
        return 3
    if isinstance(value, str):
        return 4
    if isinstance(value, list):
        return 5
    return 6


def jq_compare(left: Any, right: Any) -> int:
    left_rank, right_rank = jq_rank(left), jq_rank(right)
    if left_rank != right_rank:
        return -1 if left_rank < right_rank else 1
    if left_rank in (3, 4):
        return (left > right) - (left < right)
    if left_rank == 5:
        for left_item, right_item in zip(left, right):
            order = jq_compare(left_item, right_item)
            if order:
                return order
        return (len(left) > len(right)) - (len(left) < len(right))
    if left_rank == 6:
        left_keys, right_keys = sorted(left), sorted(right)
        order = jq_compare(left_keys, right_keys)
        if order:
            return order
        for key in left_keys:
            order = jq_compare(left[key], right[key])
            if order:
                return order
    return 0


jq_sort_key = functools.cmp_to_key(jq_compare)


def truthy(value: Any) -> bool:
    return value is not None and value is not False


def normalize(value: Any) -> Any:
    if isinstance(value, float):
        if math.isnan(value):
            return None
        if math.isinf(value):
            return math.copysign(1.7976931348623157e308, value)
        if value.is_integer() and abs(value) < 1e17:
            return int(value)
        return value
    if isinstance(value, list):
        return [normalize(item) for item in value]
    if isinstance(value, dict):
        return {key: normalize(item) for key, item in value.items()}
    return value


def compact_json(value: Any) -> str:
    return json.dumps(normalize(value), ensure_ascii=False, separators=(",", ":"))


def render_jq_output(value: Any, raw: bool, compact: bool) -> str:
    if raw and isinstance(value, str):
        return value
    if compact:
        return compact_json(value)
    return json.dumps(normalize(value), ensure_ascii=False, indent=2)


def jq_describe(value: Any) -> str:
    text = compact_json(value)
    if len(text) > 11:
        text = text[:10] + "..."
    return f"{jq_type(value)} ({text})"


def iterate_error(value: Any) -> JqError:
    if value is None:
        return JqError("Cannot iterate over null")
    return JqError(f"Cannot iterate over {jq_describe(value)}")


def iterable_items(value: Any) -> List[Any]:
    if isinstance(value, list):
        return value
    if isinstance(value, dict):
        return list(value.values())
    raise iterate_error(value)


def require_array(value: Any, verb: str) -> List[Any]:
    if not isinstance(value, list):
        raise JqError(f"{jq_describe(value)} cannot be {verb}, as it is not an array")
    return value


def index_value(container: Any, key: Any) -> Any:
    if isinstance(key, str):
        if isinstance(container, dict):
            return container.get(key)
        if container is None:
            return None
        raise JqError(f'Cannot index {jq_type(container)} with "{key}"')
    if is_number(key):
        if isinstance(container, list):
            position = math.floor(key)
            if position < 0:
                position += len(container)
            return container[position] if 0 <= position < len(container) else None
        if container is None:
            return None
        raise JqError(f"Cannot index {jq_type(container)} with number")
    raise JqError(f"Cannot index {jq_type(container)} with {jq_type(key)}")


def slice_value(container: Any, start: Any, end: Any) -> Any:
    if container is None:
        return None
    if not isinstance(container, (list, str)):
        raise JqError(f"Cannot index {jq_type(container)} with object")
    for bound in (start, end):
        if bound is not None and not is_number(bound):
            raise JqError("Start and end indices of an array slice must be numbers")
    low = None if start is None else math.floor(start)
    high = None if end is None else math.ceil(end)
    return container[low:high]


def jq_arith(op: str, left: Any, right: Any) -> Any:
    if op == "+":
        if left is None:
            return right
        if right is None:
            return left
        if is_number(left) and is_number(right):
            return left + right
        for kind in (str, list):
            if isinstance(left, kind) and isinstance(right, kind):
                return left + right
        if isinstance(left, dict) and isinstance(right, dict):
            return {**left, **right}
        raise JqError(f"{jq_describe(left)} and {jq_describe(right)} cannot be added")
    if op == "-":
        if is_number(left) and is_number(right):
            return left - right
        if isinstance(left, list) and isinstance(right, list):
            return [item for item in left if all(jq_compare(item, other) != 0 for other in right)]
        raise JqError(f"{jq_describe(left)} and {jq_describe(right)} cannot be subtracted")
    if op == "*":
        if is_number(left) and is_number(right):
            return left * right
        if isinstance(left, dict) and isinstance(right, dict):
            return deep_merge(left, right)
        if isinstance(left, str) and is_number(right) or is_number(left) and isinstance(right, str):
            text, times = (left, right) if isinstance(left, str) else (right, left)
            return text * math.ceil(times) if times > 0 else None
        raise JqError(f"{jq_describe(left)} and {jq_describe(right)} cannot be multiplied")
    if op == "/":
        if is_number(left) and is_number(right):
            if right == 0:
                raise JqError(f"{jq_describe(left)} and {jq_describe(right)} cannot be divided because the divisor is zero")
            return left / right
        if isinstance(left, str) and isinstance(right, str):
            return jq_split(left, right)
        raise JqError(f"{jq_describe(left)} and {jq_describe(right)} cannot be divided")
    if is_number(left) and is_number(right):
        if int(right) == 0:
            raise JqError(f"{jq_describe(left)} and {jq_describe(right)} cannot be divided because the divisor is zero")
        return int(math.fmod(int(left), int(right)))
    raise JqError(f"{jq_describe(left)} and {jq_describe(right)} cannot be divided")


def deep_merge(left: Dict[str, Any], right: Dict[str, Any]) -> Dict[str, Any]:
    merged = dict(left)
    for key, value in right.items():
        if isinstance(merged.get(key), dict) and isinstance(value, dict):
            merged[key] = deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


JQ_COMPARISONS = {
    "==": lambda order: order == 0,
    "!=": lambda order: order != 0,
    "<": lambda order: order < 0,
    "<=": lambda order: order <= 0,
    ">": lambda order: order > 0,
    ">=": lambda order: order >= 0,
}
JQ_RESERVED = {"and", "or", "if", "then", "elif", "else", "end", "as", "reduce", "foreach", "def", "try", "catch", "label", "import", "include", "__loc__"}
JQ_FORMATS = ("csv", "json", "text", "tsv")
JQ_ESCAPES = {'"': '"', "\\": "\\", "/": "/", "b": "\b", "f": "\f", "n": "\n", "r": "\r", "t": "\t"}


def fix_surrogates(text: str) -> str:
    try:
        return text.encode("utf-16", "surrogatepass").decode("utf-16")
    except UnicodeDecodeError:
        return text


class JqParser:
    def __init__(self, source: str) -> None:
        self.source = source
        self.pos = 0
        self.postfix_cache: Dict[int, Tuple[tuple, int]] = {}

    def error(self, message: str, position: Optional[int] = None) -> ProgramError:
        return ProgramError(self.source, self.pos if position is None else position, message)

    def skip(self) -> None:
        source = self.source
        while self.pos < len(source):
            char = source[self.pos]
            if char in " \t\r\n":
                self.pos += 1
            elif char == "#":
                end = source.find("\n", self.pos)
                self.pos = len(source) if end < 0 else end
            else:
                return

    def found(self) -> str:
        self.skip()
        if self.pos >= len(self.source):
            return "end of program"
        return repr(self.source[self.pos:self.pos + 12])

    def peek(self, text: str) -> bool:
        self.skip()
        return self.source.startswith(text, self.pos)

    def accept(self, text: str) -> bool:
        if self.peek(text):
            self.pos += len(text)
            return True
        return False

    def expect(self, text: str) -> None:
        if not self.accept(text):
            raise self.error(f"expected {text!r}, found {self.found()}")

    def peek_word(self) -> Optional[str]:
        self.skip()
        match = IDENT_RE.match(self.source, self.pos)
        return match.group() if match else None

    def accept_word(self, word: str) -> bool:
        if self.peek_word() == word:
            self.pos += len(word)
            return True
        return False

    def expect_word(self, word: str) -> None:
        if not self.accept_word(word):
            raise self.error(f"expected {word!r}, found {self.found()}")

    def parse(self) -> tuple:
        tree = self.parse_pipe()
        if self.pos < len(self.source):
            raise self.error(f"unexpected {self.found()}")
        return tree

    def parse_pipe(self) -> tuple:
        if self.peek_word() == "def":
            raise self.error("def is not supported")
        start = self.pos
        try:
            term: Optional[tuple] = self.parse_postfix()
        except ProgramError:
            term = None
        if term is not None and self.accept_word("as"):
            name = self.parse_variable()
            self.expect("|")
            return ("bind", term, name, self.parse_pipe())
        self.pos = start
        left = self.parse_comma()
        if self.peek("|="):
            raise self.error("update-assignment |= is not supported")
        if self.accept("|"):
            return ("pipe", left, self.parse_pipe())
        return left

    def parse_variable(self) -> str:
        self.expect("$")
        match = IDENT_RE.match(self.source, self.pos)
        if not match:
            raise self.error("expected a variable name after $")
        self.pos = match.end()
        return match.group()

    def parse_comma(self) -> tuple:
        left = self.parse_alternative()
        while self.accept(","):
            left = ("comma", left, self.parse_alternative())
        return left

    def parse_alternative(self) -> tuple:
        left = self.parse_or()
        if self.accept("//"):
            return ("alternative", left, self.parse_alternative())
        return left

    def parse_or(self) -> tuple:
        left = self.parse_and()
        while self.accept_word("or"):
            left = ("or", left, self.parse_and())
        return left

    def parse_and(self) -> tuple:
        left = self.parse_comparison()
        while self.accept_word("and"):
            left = ("and", left, self.parse_comparison())
        return left

    def parse_comparison(self) -> tuple:
        left = self.parse_additive()
        for op in ("==", "!=", "<=", ">=", "<", ">"):
            if self.accept(op):
                return ("compare", op, left, self.parse_additive())
        return left

    def parse_additive(self) -> tuple:
        left = self.parse_multiplicative()
        while True:
            if self.accept("+"):
                left = ("arith", "+", left, self.parse_multiplicative())
            elif self.accept("-"):
                left = ("arith", "-", left, self.parse_multiplicative())
            else:
                return left

    def parse_multiplicative(self) -> tuple:
        left = self.parse_unary()
        while True:
            self.skip()
            char = self.source[self.pos:self.pos + 1]
            if char in ("*", "%") or char == "/" and not self.source.startswith("//", self.pos):
                self.pos += 1
                left = ("arith", char, left, self.parse_unary())
            else:
                return left

    def parse_unary(self) -> tuple:
        if self.accept("-"):
            return ("negate", self.parse_postfix())
        return self.parse_postfix()

    def parse_postfix(self) -> tuple:
        self.skip()
        start = self.pos
        cached = self.postfix_cache.get(start)
        if cached is not None:
            self.pos = cached[1]
            return cached[0]
        term = self.parse_term()
        while True:
            self.skip()
            source, pos = self.source, self.pos
            field = IDENT_RE.match(source, pos + 1) if source.startswith(".", pos) else None
            if source.startswith("?", pos) and not source.startswith("?//", pos):
                self.pos += 1
                term = ("try", term)
            elif source.startswith(".[", pos):
                self.pos += 1
                term = self.parse_bracket(term)
            elif source.startswith("[", pos):
                term = self.parse_bracket(term)
            elif source.startswith('."', pos):
                self.pos += 1
                term = ("index", term, self.parse_string())
            elif field:
                self.pos = field.end()
                term = ("index", term, ("literal", field.group()))
            else:
                break
        self.postfix_cache[start] = (term, self.pos)
        return term

    def parse_bracket(self, target: tuple) -> tuple:
        self.expect("[")
        if self.accept("]"):
            return ("iterate", target)
        if self.accept(":"):
            end = self.parse_pipe()
            self.expect("]")
            return ("slice", target, None, end)
        index = self.parse_pipe()
        if self.accept(":"):
            end = None if self.peek("]") else self.parse_pipe()
            self.expect("]")
            return ("slice", target, index, end)
        self.expect("]")
        return ("index", target, index)

    def parse_term(self) -> tuple:
        self.skip()
        source, pos = self.source, self.pos
        char = source[pos:pos + 1]
        if not char:
            raise self.error("unexpected end of program")
        if source.startswith("..", pos):
            raise self.error("recursive descent .. is not supported")
        if char == ".":
            field = IDENT_RE.match(source, pos + 1)
            if field:
                self.pos = field.end()
                return ("index", ("identity",), ("literal", field.group()))
            if source.startswith('"', pos + 1):
                self.pos += 1
                return ("index", ("identity",), self.parse_string())
            if source.startswith("[", pos + 1):
                self.pos += 1
                return self.parse_bracket(("identity",))
            self.pos += 1
            return ("identity",)
        if char.isdigit():
            number = JQ_NUMBER_RE.match(source, pos)
            self.pos = number.end()
            text = number.group()
            return ("literal", float(text) if any(mark in text for mark in ".eE") else int(text))
        if char == '"':
            return self.parse_string()
        if char == "(":
            self.pos += 1
            inner = self.parse_pipe()
            self.expect(")")
            return inner
        if char == "[":
            self.pos += 1
            if self.accept("]"):
                return ("array", None)
            inner = self.parse_pipe()
            self.expect("]")
            return ("array", inner)
        if char == "{":
            return self.parse_object()
        if char == "$":
            return ("variable", self.parse_variable())
        if char == "@":
            name = IDENT_RE.match(source, pos + 1)
            if not name or name.group() not in JQ_FORMATS:
                raise self.error(f"unknown format, expected one of {', '.join('@' + known for known in JQ_FORMATS)}")
            self.pos = name.end()
            return ("format", name.group())
        word = IDENT_RE.match(source, pos)
        if not word:
            raise self.error(f"unexpected {self.found()}")
        name = word.group()
        self.pos = word.end()
        if name in ("null", "true", "false"):
            return ("literal", {"null": None, "true": True, "false": False}[name])
        if name == "if":
            return self.parse_if()
        if name == "reduce":
            return self.parse_reduce()
        if name in JQ_RESERVED:
            raise self.error(f"{name!r} is not supported here", pos)
        args: List[tuple] = []
        if self.accept("("):
            args.append(self.parse_pipe())
            while self.accept(";"):
                args.append(self.parse_pipe())
            self.expect(")")
        if (name, len(args)) not in JQ_BUILTINS:
            raise self.error(f"{name}/{len(args)} is not defined", pos)
        return ("call", name, args)

    def parse_string(self) -> tuple:
        self.expect('"')
        source = self.source
        parts: List[tuple] = []
        text: List[str] = []
        while True:
            if self.pos >= len(source):
                raise self.error("unterminated string")
            char = source[self.pos]
            if char == '"':
                self.pos += 1
                break
            if char != "\\":
                text.append(char)
                self.pos += 1
                continue
            escape = source[self.pos + 1:self.pos + 2]
            if escape == "(":
                self.pos += 2
                if text:
                    parts.append(("literal", fix_surrogates("".join(text))))
                    text = []
                parts.append(self.parse_pipe())
                self.expect(")")
            elif escape == "u":
                digits = source[self.pos + 2:self.pos + 6]
                if not re.fullmatch(r"[0-9a-fA-F]{4}", digits):
                    raise self.error("bad \\u escape, expected four hex digits")
                text.append(chr(int(digits, 16)))
                self.pos += 6
            elif escape in JQ_ESCAPES:
                text.append(JQ_ESCAPES[escape])
                self.pos += 2
            else:
                raise self.error(f"invalid escape \\{escape}")
        if text or not parts:
            parts.append(("literal", fix_surrogates("".join(text))))
        if len(parts) == 1 and parts[0][0] == "literal":
            return parts[0]
        return ("interpolate", parts)

    def parse_object(self) -> tuple:
        self.expect("{")
        entries: List[tuple] = []
        if self.accept("}"):
            return ("object", entries)
        while True:
            self.skip()
            char = self.source[self.pos:self.pos + 1]
            if char == "$":
                name = self.parse_variable()
                entries.append(("entry", ("literal", name), ("variable", name)))
            else:
                if char == '"':
                    key = self.parse_string()
                elif char == "(":
                    self.pos += 1
                    key = self.parse_pipe()
                    self.expect(")")
                else:
                    word = IDENT_RE.match(self.source, self.pos)
                    if not word:
                        raise self.error(f"expected an object key, found {self.found()}")
                    self.pos = word.end()
                    key = ("literal", word.group())
                if self.accept(":"):
                    value = self.parse_object_value()
                elif char == "(":
                    raise self.error("a computed key needs a value: {(key): value}")
                else:
                    value = ("index", ("identity",), key)
                entries.append(("entry", key, value))
            if self.accept(","):
                continue
            self.expect("}")
            return ("object", entries)

    def parse_object_value(self) -> tuple:
        value = self.parse_alternative()
        while self.accept("|"):
            value = ("pipe", value, self.parse_alternative())
        return value

    def parse_if(self) -> tuple:
        condition = self.parse_pipe()
        self.expect_word("then")
        then_branch = self.parse_pipe()
        if self.accept_word("elif"):
            return ("if", condition, then_branch, self.parse_if())
        if self.accept_word("else"):
            else_branch = self.parse_pipe()
            self.expect_word("end")
            return ("if", condition, then_branch, else_branch)
        self.expect_word("end")
        return ("if", condition, then_branch, None)

    def parse_reduce(self) -> tuple:
        source = self.parse_postfix()
        self.expect_word("as")
        name = self.parse_variable()
        self.expect("(")
        initial = self.parse_pipe()
        self.expect(";")
        update = self.parse_pipe()
        self.expect(")")
        return ("reduce", source, name, initial, update)


def unbound_variables(tree: tuple, bound: frozenset) -> List[str]:
    kind = tree[0]
    if kind == "variable":
        return [] if tree[1] in bound else [tree[1]]
    if kind == "bind":
        return unbound_variables(tree[1], bound) + unbound_variables(tree[3], bound | {tree[2]})
    if kind == "reduce":
        return unbound_variables(tree[1], bound) + unbound_variables(tree[3], bound) + unbound_variables(tree[4], bound | {tree[2]})
    missing: List[str] = []
    for part in tree[1:]:
        for child in part if isinstance(part, list) else [part]:
            if is_node(child):
                missing.extend(unbound_variables(child, bound))
    return missing


class JqProgram:
    def __init__(self, source: str, variables: Iterable[str] = ()) -> None:
        self.source = source
        self.tree = JqParser(source).parse()
        missing = unbound_variables(self.tree, frozenset(variables))
        if missing:
            raise ProgramError(source, max(source.find("$" + missing[0]), 0), f"${missing[0]} is not defined")
        self.dispatch: Dict[str, Callable[..., Iterator[Any]]] = {name[5:]: getattr(self, name) for name in dir(self) if name.startswith("eval_")}

    def run(self, value: Any, variables: Optional[Dict[str, Any]] = None) -> Iterator[Any]:
        return self.evaluate(self.tree, value, variables or {})

    def evaluate(self, node: tuple, value: Any, env: Dict[str, Any]) -> Iterator[Any]:
        return self.dispatch[node[0]](node, value, env)

    def eval_identity(self, node: tuple, value: Any, env: Dict[str, Any]) -> Iterator[Any]:
        yield value

    def eval_literal(self, node: tuple, value: Any, env: Dict[str, Any]) -> Iterator[Any]:
        yield node[1]

    def eval_variable(self, node: tuple, value: Any, env: Dict[str, Any]) -> Iterator[Any]:
        yield env[node[1]]

    def eval_index(self, node: tuple, value: Any, env: Dict[str, Any]) -> Iterator[Any]:
        for key in self.evaluate(node[2], value, env):
            for container in self.evaluate(node[1], value, env):
                yield index_value(container, key)

    def eval_iterate(self, node: tuple, value: Any, env: Dict[str, Any]) -> Iterator[Any]:
        for container in self.evaluate(node[1], value, env):
            if isinstance(container, list):
                yield from container
            elif isinstance(container, dict):
                yield from list(container.values())
            else:
                raise iterate_error(container)

    def eval_slice(self, node: tuple, value: Any, env: Dict[str, Any]) -> Iterator[Any]:
        starts = list(self.evaluate(node[2], value, env)) if node[2] else [None]
        ends = list(self.evaluate(node[3], value, env)) if node[3] else [None]
        for end in ends:
            for start in starts:
                for container in self.evaluate(node[1], value, env):
                    yield slice_value(container, start, end)

    def eval_try(self, node: tuple, value: Any, env: Dict[str, Any]) -> Iterator[Any]:
        try:
            yield from self.evaluate(node[1], value, env)
        except JqError:
            return

    def eval_pipe(self, node: tuple, value: Any, env: Dict[str, Any]) -> Iterator[Any]:
        for left in self.evaluate(node[1], value, env):
            yield from self.evaluate(node[2], left, env)

    def eval_comma(self, node: tuple, value: Any, env: Dict[str, Any]) -> Iterator[Any]:
        yield from self.evaluate(node[1], value, env)
        yield from self.evaluate(node[2], value, env)

    def eval_bind(self, node: tuple, value: Any, env: Dict[str, Any]) -> Iterator[Any]:
        for bound in self.evaluate(node[1], value, env):
            yield from self.evaluate(node[3], value, {**env, node[2]: bound})

    def eval_reduce(self, node: tuple, value: Any, env: Dict[str, Any]) -> Iterator[Any]:
        _, source, name, initial, update = node
        for accumulator in self.evaluate(initial, value, env):
            for item in self.evaluate(source, value, env):
                results = list(self.evaluate(update, accumulator, {**env, name: item}))
                accumulator = results[-1] if results else None
            yield accumulator

    def eval_negate(self, node: tuple, value: Any, env: Dict[str, Any]) -> Iterator[Any]:
        for operand in self.evaluate(node[1], value, env):
            if not is_number(operand):
                raise JqError(f"{jq_describe(operand)} cannot be negated")
            yield -operand

    def eval_arith(self, node: tuple, value: Any, env: Dict[str, Any]) -> Iterator[Any]:
        for right in self.evaluate(node[3], value, env):
            for left in self.evaluate(node[2], value, env):
                yield jq_arith(node[1], left, right)

    def eval_compare(self, node: tuple, value: Any, env: Dict[str, Any]) -> Iterator[Any]:
        test = JQ_COMPARISONS[node[1]]
        for right in self.evaluate(node[3], value, env):
            for left in self.evaluate(node[2], value, env):
                yield test(jq_compare(left, right))

    def eval_and(self, node: tuple, value: Any, env: Dict[str, Any]) -> Iterator[Any]:
        for left in self.evaluate(node[1], value, env):
            if not truthy(left):
                yield False
                continue
            for right in self.evaluate(node[2], value, env):
                yield truthy(right)

    def eval_or(self, node: tuple, value: Any, env: Dict[str, Any]) -> Iterator[Any]:
        for left in self.evaluate(node[1], value, env):
            if truthy(left):
                yield True
                continue
            for right in self.evaluate(node[2], value, env):
                yield truthy(right)

    def eval_alternative(self, node: tuple, value: Any, env: Dict[str, Any]) -> Iterator[Any]:
        found = False
        try:
            for left in self.evaluate(node[1], value, env):
                if truthy(left):
                    found = True
                    yield left
        except JqError:
            pass
        if not found:
            yield from self.evaluate(node[2], value, env)

    def eval_if(self, node: tuple, value: Any, env: Dict[str, Any]) -> Iterator[Any]:
        for condition in self.evaluate(node[1], value, env):
            if truthy(condition):
                yield from self.evaluate(node[2], value, env)
            elif node[3] is None:
                yield value
            else:
                yield from self.evaluate(node[3], value, env)

    def eval_array(self, node: tuple, value: Any, env: Dict[str, Any]) -> Iterator[Any]:
        yield [] if node[1] is None else list(self.evaluate(node[1], value, env))

    def eval_object(self, node: tuple, value: Any, env: Dict[str, Any]) -> Iterator[Any]:
        results: List[Dict[str, Any]] = [{}]
        for _, key_node, value_node in node[1]:
            keys = list(self.evaluate(key_node, value, env))
            for key in keys:
                if not isinstance(key, str):
                    raise JqError(f"Object keys must be strings, got {jq_describe(key)}")
            values = list(self.evaluate(value_node, value, env))
            results = [{**partial, key: item} for partial in results for key in keys for item in values]
        yield from results

    def eval_interpolate(self, node: tuple, value: Any, env: Dict[str, Any]) -> Iterator[Any]:
        results = [""]
        for part in node[1]:
            pieces = [item if isinstance(item, str) else compact_json(item) for item in self.evaluate(part, value, env)]
            results = [prefix + piece for prefix in results for piece in pieces]
        yield from results

    def eval_format(self, node: tuple, value: Any, env: Dict[str, Any]) -> Iterator[Any]:
        yield apply_format(node[1], value)

    def eval_call(self, node: tuple, value: Any, env: Dict[str, Any]) -> Iterator[Any]:
        return JQ_BUILTINS[(node[1], len(node[2]))](self, node[2], value, env)


def apply_format(name: str, value: Any) -> str:
    if name == "json":
        return compact_json(value)
    if name == "text":
        return value if isinstance(value, str) else compact_json(value)
    if not isinstance(value, list):
        raise JqError(f"{jq_describe(value)} cannot be {name}-formatted, only an array can be")
    cells = []
    for item in value:
        if isinstance(item, (list, dict)):
            raise JqError(f"{jq_describe(item)} is not valid in a {name} row")
        if item is None:
            cells.append("")
        elif isinstance(item, str):
            if name == "csv":
                cells.append('"' + item.replace('"', '""') + '"')
            else:
                cells.append(item.replace("\\", "\\\\").replace("\t", "\\t").replace("\n", "\\n").replace("\r", "\\r"))
        else:
            cells.append(compact_json(item))
    return ("," if name == "csv" else "\t").join(cells)


def jq_length(value: Any) -> Any:
    if value is None:
        return 0
    if isinstance(value, bool):
        raise JqError(f"{jq_describe(value)} has no length")
    if is_number(value):
        return abs(value)
    return len(value)


def jq_keys(value: Any, ordered: bool = True) -> List[Any]:
    if isinstance(value, dict):
        return sorted(value) if ordered else list(value)
    if isinstance(value, list):
        return list(range(len(value)))
    raise JqError(f"{jq_describe(value)} has no keys")


def jq_has(value: Any, key: Any) -> bool:
    if isinstance(value, dict) and isinstance(key, str):
        return key in value
    if isinstance(value, list) and is_number(key):
        return 0 <= key < len(value)
    raise JqError(f"Cannot check whether {jq_type(value)} has a {jq_type(key)} key")


def jq_to_entries(value: Any) -> List[Dict[str, Any]]:
    if not isinstance(value, dict):
        raise JqError(f"{jq_describe(value)} has no keys")
    return [{"key": key, "value": item} for key, item in value.items()]


def jq_from_entries(value: Any) -> Dict[str, Any]:
    result: Dict[str, Any] = {}
    for entry in iterable_items(value):
        if not isinstance(entry, dict):
            raise JqError(f'Cannot index {jq_type(entry)} with "key"')
        key = entry.get("key")
        if key is None:
            key = next((entry[name] for name in ("k", "name", "Name", "K", "Key") if truthy(entry.get(name))), None)
        if not isinstance(key, str):
            key = compact_json(key)
        result[key] = entry["value"] if "value" in entry else entry["v"] if "v" in entry else entry.get("Value")
    return result


def jq_add(value: Any) -> Any:
    total = None
    for item in iterable_items(value):
        total = jq_arith("+", total, item)
    return total


def jq_unique(items: List[Any]) -> List[Any]:
    result: List[Any] = []
    for item in sorted(items, key=jq_sort_key):
        if not result or jq_compare(result[-1], item) != 0:
            result.append(item)
    return result


def pick_extreme(items: List[Any], keys: List[Any], want_min: bool) -> Any:
    best, best_key, found = None, None, False
    for item, key in zip(items, keys):
        if not found:
            best, best_key, found = item, key, True
            continue
        order = jq_compare(key, best_key)
        if (want_min and order < 0) or (not want_min and order >= 0):
            best, best_key = item, key
    return best


def jq_split(value: Any, separator: Any) -> List[str]:
    if not isinstance(value, str) or not isinstance(separator, str):
        raise JqError("split input and separator must be strings")
    if value == "":
        return []
    return list(value) if separator == "" else value.split(separator)


def jq_join(value: Any, separator: Any) -> str:
    if not isinstance(separator, str):
        raise JqError(f"{jq_describe(separator)} cannot be used as a join separator")
    parts = []
    for item in iterable_items(value):
        if item is None:
            parts.append("")
        elif isinstance(item, str):
            parts.append(item)
        elif isinstance(item, (list, dict)):
            raise JqError(f"Cannot join with {jq_type(item)}")
        else:
            parts.append(compact_json(item))
    return separator.join(parts)


def jq_require_string(value: Any, name: str) -> str:
    if not isinstance(value, str):
        raise JqError(f"{jq_describe(value)} cannot be {name}, as it is not a string")
    return value


def jq_tonumber(value: Any) -> Any:
    if is_number(value):
        return value
    if isinstance(value, str):
        try:
            number = json.loads(value)
        except ValueError:
            number = None
        if is_number(number):
            return number
    raise JqError(f"Cannot parse {jq_describe(value)} as a number")


def jq_reverse(value: Any) -> Any:
    if value is None:
        return []
    if isinstance(value, (list, str)):
        return value[::-1]
    raise JqError(f"Cannot reverse {jq_describe(value)}")


def jq_flatten(value: Any) -> List[Any]:
    flat: List[Any] = []
    for item in require_array(value, "flattened"):
        if isinstance(item, list):
            flat.extend(jq_flatten(item))
        else:
            flat.append(item)
    return flat


def jq_test(value: Any, pattern: Any) -> bool:
    try:
        compiled = re.compile(jq_require_string(pattern, "used as a regex"))
    except re.error as error:
        raise JqError(f"{pattern} is not a valid regex: {error}") from None
    return compiled.search(jq_require_string(value, "matched")) is not None


def jq_startswith(value: Any, prefix: Any) -> bool:
    if not isinstance(value, str) or not isinstance(prefix, str):
        raise JqError("startswith() requires string inputs")
    return value.startswith(prefix)


def jq_endswith(value: Any, suffix: Any) -> bool:
    if not isinstance(value, str) or not isinstance(suffix, str):
        raise JqError("endswith() requires string inputs")
    return value.endswith(suffix)


def jq_ltrimstr(value: Any, prefix: Any) -> Any:
    if isinstance(value, str) and isinstance(prefix, str) and value.startswith(prefix):
        return value[len(prefix):]
    return value


def jq_rtrimstr(value: Any, suffix: Any) -> Any:
    if isinstance(value, str) and isinstance(suffix, str) and suffix and value.endswith(suffix):
        return value[:-len(suffix)]
    return value


def jq_fromjson(value: Any) -> Any:
    try:
        return json.loads(jq_require_string(value, "parsed"))
    except ValueError as error:
        raise JqError(f"{jq_describe(value)} cannot be parsed: {error}") from None


def jq_number_function(function: Callable[[float], Any]) -> Callable[[Any], Any]:
    def apply(value: Any) -> Any:
        if not is_number(value):
            raise JqError(f"{jq_describe(value)} number required")
        return function(value)
    return apply


def unary(function: Callable[[Any], Any]) -> Callable[..., Iterator[Any]]:
    def run(program: JqProgram, args: List[tuple], value: Any, env: Dict[str, Any]) -> Iterator[Any]:
        yield function(value)
    return run


def binary(function: Callable[[Any, Any], Any]) -> Callable[..., Iterator[Any]]:
    def run(program: JqProgram, args: List[tuple], value: Any, env: Dict[str, Any]) -> Iterator[Any]:
        for argument in program.evaluate(args[0], value, env):
            yield function(value, argument)
    return run


def type_filter(name: str) -> Callable[..., Iterator[Any]]:
    def run(program: JqProgram, args: List[tuple], value: Any, env: Dict[str, Any]) -> Iterator[Any]:
        if jq_type(value) == name:
            yield value
    return run


def keyed(program: JqProgram, items: List[Any], node: tuple, env: Dict[str, Any]) -> List[Tuple[List[Any], Any]]:
    return [(list(program.evaluate(node, item, env)), item) for item in items]


def builtin_empty(program: JqProgram, args: List[tuple], value: Any, env: Dict[str, Any]) -> Iterator[Any]:
    return iter(())


def builtin_error(program: JqProgram, args: List[tuple], value: Any, env: Dict[str, Any]) -> Iterator[Any]:
    for message in program.evaluate(args[0], value, env):
        raise JqError(message if isinstance(message, str) else compact_json(message))
    return iter(())


def builtin_values(program: JqProgram, args: List[tuple], value: Any, env: Dict[str, Any]) -> Iterator[Any]:
    if value is not None:
        yield value


def builtin_map(program: JqProgram, args: List[tuple], value: Any, env: Dict[str, Any]) -> Iterator[Any]:
    yield [output for item in iterable_items(value) for output in program.evaluate(args[0], item, env)]


def builtin_map_values(program: JqProgram, args: List[tuple], value: Any, env: Dict[str, Any]) -> Iterator[Any]:
    missing = object()
    if isinstance(value, dict):
        mapped = {key: next(iter(program.evaluate(args[0], item, env)), missing) for key, item in value.items()}
        yield {key: item for key, item in mapped.items() if item is not missing}
    else:
        outputs = [next(iter(program.evaluate(args[0], item, env)), missing) for item in iterable_items(value)]
        yield [item for item in outputs if item is not missing]


def builtin_select(program: JqProgram, args: List[tuple], value: Any, env: Dict[str, Any]) -> Iterator[Any]:
    for condition in program.evaluate(args[0], value, env):
        if truthy(condition):
            yield value


def builtin_sort_by(program: JqProgram, args: List[tuple], value: Any, env: Dict[str, Any]) -> Iterator[Any]:
    pairs = keyed(program, require_array(value, "sorted"), args[0], env)
    pairs.sort(key=lambda pair: jq_sort_key(pair[0]))
    yield [item for _, item in pairs]


def group_pairs(program: JqProgram, value: Any, node: tuple, env: Dict[str, Any]) -> List[List[Any]]:
    pairs = keyed(program, require_array(value, "grouped"), node, env)
    pairs.sort(key=lambda pair: jq_sort_key(pair[0]))
    groups: List[List[Any]] = []
    previous: Any = None
    for key, item in pairs:
        if groups and jq_compare(previous, key) == 0:
            groups[-1].append(item)
        else:
            groups.append([item])
            previous = key
    return groups


def builtin_group_by(program: JqProgram, args: List[tuple], value: Any, env: Dict[str, Any]) -> Iterator[Any]:
    yield group_pairs(program, value, args[0], env)


def builtin_unique_by(program: JqProgram, args: List[tuple], value: Any, env: Dict[str, Any]) -> Iterator[Any]:
    yield [group[0] for group in group_pairs(program, value, args[0], env)]


def extreme_by(want_min: bool) -> Callable[..., Iterator[Any]]:
    def run(program: JqProgram, args: List[tuple], value: Any, env: Dict[str, Any]) -> Iterator[Any]:
        pairs = keyed(program, require_array(value, "compared"), args[0], env)
        yield pick_extreme([item for _, item in pairs], [key for key, _ in pairs], want_min)
    return run


def builtin_with_entries(program: JqProgram, args: List[tuple], value: Any, env: Dict[str, Any]) -> Iterator[Any]:
    yield jq_from_entries([output for entry in jq_to_entries(value) for output in program.evaluate(args[0], entry, env)])


def quantifier(combine: Callable[[Iterable[bool]], bool]) -> Callable[..., Iterator[Any]]:
    def run(program: JqProgram, args: List[tuple], value: Any, env: Dict[str, Any]) -> Iterator[Any]:
        items = iterable_items(value)
        if args:
            yield combine(truthy(output) for item in items for output in program.evaluate(args[0], item, env))
        else:
            yield combine(truthy(item) for item in items)
    return run


def builtin_first_of(program: JqProgram, args: List[tuple], value: Any, env: Dict[str, Any]) -> Iterator[Any]:
    for output in program.evaluate(args[0], value, env):
        yield output
        return


def builtin_limit(program: JqProgram, args: List[tuple], value: Any, env: Dict[str, Any]) -> Iterator[Any]:
    for count in program.evaluate(args[0], value, env):
        if not is_number(count) or count <= 0:
            continue
        emitted = 0
        for output in program.evaluate(args[1], value, env):
            yield output
            emitted += 1
            if emitted >= count:
                break


def builtin_range(program: JqProgram, args: List[tuple], value: Any, env: Dict[str, Any]) -> Iterator[Any]:
    if len(args) == 1:
        bounds = [(0, upper) for upper in program.evaluate(args[0], value, env)]
    else:
        bounds = [(lower, upper) for lower in program.evaluate(args[0], value, env) for upper in program.evaluate(args[1], value, env)]
    for lower, upper in bounds:
        if not is_number(lower) or not is_number(upper):
            raise JqError("Range bounds must be numeric")
        current = lower
        while current < upper:
            yield current
            current += 1


JQ_BUILTINS: Dict[Tuple[str, int], Callable[..., Iterator[Any]]] = {
    ("empty", 0): builtin_empty,
    ("error", 1): builtin_error,
    ("not", 0): unary(lambda value: not truthy(value)),
    ("length", 0): unary(jq_length),
    ("keys", 0): unary(jq_keys),
    ("keys_unsorted", 0): unary(lambda value: jq_keys(value, ordered=False)),
    ("has", 1): binary(jq_has),
    ("to_entries", 0): unary(jq_to_entries),
    ("from_entries", 0): unary(jq_from_entries),
    ("with_entries", 1): builtin_with_entries,
    ("add", 0): unary(jq_add),
    ("map", 1): builtin_map,
    ("map_values", 1): builtin_map_values,
    ("select", 1): builtin_select,
    ("sort", 0): unary(lambda value: sorted(require_array(value, "sorted"), key=jq_sort_key)),
    ("sort_by", 1): builtin_sort_by,
    ("group_by", 1): builtin_group_by,
    ("unique", 0): unary(lambda value: jq_unique(require_array(value, "sorted"))),
    ("unique_by", 1): builtin_unique_by,
    ("min", 0): unary(lambda value: pick_extreme(require_array(value, "compared"), value, True)),
    ("max", 0): unary(lambda value: pick_extreme(require_array(value, "compared"), value, False)),
    ("min_by", 1): extreme_by(True),
    ("max_by", 1): extreme_by(False),
    ("reverse", 0): unary(jq_reverse),
    ("flatten", 0): unary(jq_flatten),
    ("first", 0): unary(lambda value: index_value(value, 0)),
    ("last", 0): unary(lambda value: index_value(value, -1)),
    ("first", 1): builtin_first_of,
    ("limit", 2): builtin_limit,
    ("range", 1): builtin_range,
    ("range", 2): builtin_range,
    ("any", 0): quantifier(any),
    ("any", 1): quantifier(any),
    ("all", 0): quantifier(all),
    ("all", 1): quantifier(all),
    ("type", 0): unary(jq_type),
    ("tostring", 0): unary(lambda value: value if isinstance(value, str) else compact_json(value)),
    ("tonumber", 0): unary(jq_tonumber),
    ("tojson", 0): unary(compact_json),
    ("fromjson", 0): unary(jq_fromjson),
    ("ascii_downcase", 0): unary(lambda value: jq_require_string(value, "lowercased").translate(ASCII_LOWER)),
    ("ascii_upcase", 0): unary(lambda value: jq_require_string(value, "uppercased").translate(ASCII_UPPER)),
    ("floor", 0): unary(jq_number_function(math.floor)),
    ("sqrt", 0): unary(jq_number_function(math.sqrt)),
    ("test", 1): binary(jq_test),
    ("split", 1): binary(jq_split),
    ("join", 1): binary(jq_join),
    ("startswith", 1): binary(jq_startswith),
    ("endswith", 1): binary(jq_endswith),
    ("ltrimstr", 1): binary(jq_ltrimstr),
    ("rtrimstr", 1): binary(jq_rtrimstr),
    ("values", 0): builtin_values,
    ("nulls", 0): type_filter("null"),
    ("booleans", 0): type_filter("boolean"),
    ("numbers", 0): type_filter("number"),
    ("strings", 0): type_filter("string"),
    ("arrays", 0): type_filter("array"),
    ("objects", 0): type_filter("object"),
}


def run_jq(program: JqProgram, streams: Iterable[Tuple[str, Any]], out: TextIO, err: TextIO, raw: bool = False, compact: bool = False, slurp: bool = False, null_input: bool = False, variables: Optional[Dict[str, Any]] = None, stats: Optional[Dict[str, Any]] = None) -> int:
    stats = {} if stats is None else stats
    stats.setdefault("records", 0)
    stats.setdefault("outputs", 0)
    failures = 0

    def values() -> Iterator[Any]:
        for _, stream in streams:
            yield from iter_json_values(stream)

    def emit(value: Any, record: int) -> None:
        nonlocal failures
        try:
            for result in program.run(value, variables):
                out.write(render_jq_output(result, raw, compact))
                out.write("\n")
                stats["outputs"] += 1
        except JqError as error:
            failures += 1
            err.write(f"ministream: jq: error (at record {record}): {error}\n")

    if null_input:
        emit(None, 0)
    elif slurp:
        items = list(values())
        stats["records"] = len(items)
        emit(items, 1)
    else:
        for record, value in enumerate(values(), 1):
            stats["records"] = record
            emit(value, record)
    return EXIT_RUNTIME if failures else EXIT_OK


AWK_KEYWORDS = {"BEGIN", "END", "if", "else", "while", "for", "do", "in", "next", "nextfile", "exit", "delete", "print", "printf", "break", "continue", "function", "func", "getline", "return"}
AWK_UNSUPPORTED = {"do": "do-while loops", "function": "user-defined functions", "func": "user-defined functions", "getline": "getline", "return": "user-defined functions", "nextfile": "nextfile"}
AWK_BUILTINS = {"length": (0, 1), "substr": (2, 3), "index": (2, 2), "split": (2, 3), "sub": (2, 3), "gsub": (2, 3), "match": (2, 2), "sprintf": (1, 255), "tolower": (1, 1), "toupper": (1, 1), "int": (1, 1), "sqrt": (1, 1), "exp": (1, 1), "log": (1, 1), "sin": (1, 1), "cos": (1, 1), "atan2": (2, 2)}
AWK_OPERATORS = sorted(["+=", "-=", "*=", "/=", "%=", "^=", "==", "<=", ">=", "!=", "++", "--", "&&", "||", "!~", "+", "-", "*", "/", "%", "^", "=", "<", ">", "!", "~", "?", ":", ",", ";", "(", ")", "{", "}", "[", "]", "$", "|"], key=len, reverse=True)
AWK_ASSIGNMENTS = {"=", "+=", "-=", "*=", "/=", "%=", "^="}
AWK_ESCAPES = {"n": "\n", "t": "\t", "r": "\r", "\\": "\\", '"': '"', "/": "/", "a": "\a", "b": "\b", "f": "\f", "v": "\v"}
ERE_CLASSES = {"[:alpha:]": "a-zA-Z", "[:digit:]": "0-9", "[:alnum:]": "a-zA-Z0-9", "[:upper:]": "A-Z", "[:lower:]": "a-z", "[:space:]": " \\t\\n\\r\\f\\v", "[:blank:]": " \\t", "[:punct:]": "!-/:-@\\[-`{-~", "[:xdigit:]": "0-9A-Fa-f"}


@functools.lru_cache(maxsize=512)
def compile_ere(pattern: str) -> "re.Pattern[str]":
    translated = pattern
    for name, replacement in ERE_CLASSES.items():
        translated = translated.replace(name, replacement)
    return re.compile(translated)


def awk_unescape(text: str) -> str:
    out: List[str] = []
    index = 0
    while index < len(text):
        char = text[index]
        if char == "\\" and index + 1 < len(text):
            escape = text[index + 1]
            if escape in AWK_ESCAPES:
                out.append(AWK_ESCAPES[escape])
                index += 2
                continue
            octal = re.match(r"[0-7]{1,3}", text[index + 1:])
            if octal:
                out.append(chr(int(octal.group(), 8)))
                index += 1 + len(octal.group())
                continue
            out.append("\\" + escape)
            index += 2
            continue
        out.append(char)
        index += 1
    return "".join(out)


class AwkParser:
    def __init__(self, source: str) -> None:
        self.source = source
        self.pos = 0
        self.no_greater = False

    def error(self, message: str) -> ProgramError:
        return ProgramError(self.source, self.pos, message)

    def skip(self, newlines: bool = False) -> None:
        source = self.source
        while self.pos < len(source):
            char = source[self.pos]
            if char in " \t\r":
                self.pos += 1
            elif char == "\\" and source.startswith("\n", self.pos + 1):
                self.pos += 2
            elif char == "\\" and source.startswith("\r\n", self.pos + 1):
                self.pos += 3
            elif char == "#":
                end = source.find("\n", self.pos)
                self.pos = len(source) if end < 0 else end
            elif char == "\n" and newlines:
                self.pos += 1
            else:
                return

    def skip_terminators(self) -> None:
        while True:
            self.skip(newlines=True)
            if not self.source.startswith(";", self.pos):
                return
            self.pos += 1

    def found(self) -> str:
        self.skip()
        if self.pos >= len(self.source):
            return "end of program"
        if self.source[self.pos] == "\n":
            return "newline"
        return repr(self.source[self.pos:self.pos + 12])

    def operator(self) -> Optional[str]:
        self.skip()
        for op in AWK_OPERATORS:
            if self.source.startswith(op, self.pos):
                return op
        return None

    def accept(self, op: str, newlines_after: bool = False) -> bool:
        if self.operator() != op:
            return False
        self.pos += len(op)
        if newlines_after:
            self.skip(newlines=True)
        return True

    def expect(self, op: str) -> None:
        if not self.accept(op):
            raise self.error(f"expected {op!r}, found {self.found()}")

    def word(self) -> Optional[str]:
        self.skip()
        match = IDENT_RE.match(self.source, self.pos)
        return match.group() if match else None

    def accept_word(self, word: str) -> bool:
        if self.word() == word:
            self.pos += len(word)
            return True
        return False

    def nested(self, parse: Callable[[], Any]) -> Any:
        previous = self.no_greater
        self.no_greater = False
        try:
            return parse()
        finally:
            self.no_greater = previous

    def parse(self) -> tuple:
        items: List[tuple] = []
        self.skip_terminators()
        while self.pos < len(self.source):
            items.append(self.parse_item())
            self.skip_terminators()
        return ("program", items)

    def parse_item(self) -> tuple:
        word = self.word()
        if word in ("BEGIN", "END"):
            self.pos += len(word)
            self.skip(newlines=True)
            if not self.source.startswith("{", self.pos):
                raise self.error(f"{word} needs an action in braces")
            return (word.lower(), self.parse_block())
        if word in ("function", "func"):
            raise self.error("user-defined functions are not supported")
        if self.operator() == "{":
            return ("rule", None, self.parse_block())
        pattern = self.parse_expression()
        if self.operator() == ",":
            raise self.error("range patterns (start, stop) are not supported")
        if self.operator() == "{":
            return ("rule", pattern, self.parse_block())
        return ("rule", pattern, None)

    def parse_block(self) -> tuple:
        self.expect("{")
        statements: List[tuple] = []
        while True:
            self.skip_terminators()
            if self.accept("}"):
                return ("block", statements)
            if self.pos >= len(self.source):
                raise self.error("missing } at end of program")
            statements.append(self.parse_statement())

    def parse_statement(self) -> tuple:
        if self.operator() == "{":
            return self.parse_block()
        if self.accept(";"):
            return ("block", [])
        word = self.word()
        if word in ("if", "while"):
            self.pos += len(word)
            self.expect("(")
            condition = self.nested(self.parse_expression)
            self.expect(")")
            self.skip(newlines=True)
            body = self.parse_statement()
            if word == "while":
                return ("while", condition, body)
            saved = self.pos
            self.skip_terminators()
            if self.accept_word("else"):
                self.skip(newlines=True)
                return ("if", condition, body, self.parse_statement())
            self.pos = saved
            return ("if", condition, body, None)
        if word == "for":
            return self.parse_for()
        if word in AWK_UNSUPPORTED:
            raise self.error(f"{AWK_UNSUPPORTED[word]} are not supported")
        statement = self.parse_simple_statement()
        self.end_simple_statement()
        return statement

    def parse_for(self) -> tuple:
        self.pos += 3
        self.expect("(")
        saved = self.pos
        name = self.word()
        if name and name not in AWK_KEYWORDS:
            self.pos += len(name)
            if self.accept_word("in"):
                array = self.word()
                if array and array not in AWK_KEYWORDS:
                    self.pos += len(array)
                    if self.accept(")"):
                        self.skip(newlines=True)
                        return ("for_in", name, array, self.parse_statement())
        self.pos = saved
        initial = None if self.operator() == ";" else self.parse_simple_statement()
        self.expect(";")
        self.skip(newlines=True)
        condition = None if self.operator() == ";" else self.parse_expression()
        self.expect(";")
        self.skip(newlines=True)
        step = None if self.operator() == ")" else self.parse_simple_statement()
        self.expect(")")
        self.skip(newlines=True)
        return ("for", initial, condition, step, self.parse_statement())

    def at_statement_end(self) -> bool:
        self.skip()
        return self.pos >= len(self.source) or self.source[self.pos] in ";\n}"

    def end_simple_statement(self) -> None:
        if self.at_statement_end():
            if self.pos < len(self.source) and self.source[self.pos] in ";\n":
                self.pos += 1
            return
        if self.operator() in (">", "|"):
            raise self.error("output redirection and pipes (> file, | command) are not supported")
        raise self.error(f"unexpected {self.found()}")

    def parse_simple_statement(self) -> tuple:
        word = self.word()
        if word in ("print", "printf"):
            self.pos += len(word)
            arguments = self.parse_print_arguments()
            if word == "printf" and not arguments:
                raise self.error("printf needs a format")
            return (word, arguments)
        if word in ("next", "break", "continue"):
            self.pos += len(word)
            return (word,)
        if word == "exit":
            self.pos += 4
            return ("exit", None if self.at_statement_end() else self.parse_expression())
        if word == "delete":
            self.pos += 6
            name = self.word()
            if not name or name in AWK_KEYWORDS:
                raise self.error("delete needs an array name")
            self.pos += len(name)
            if self.source.startswith("[", self.pos):
                self.pos += 1
                subscripts = self.nested(self.parse_expression_list)
                self.expect("]")
                return ("delete", name, subscripts)
            return ("delete", name, None)
        return ("expression", self.parse_expression())

    def parse_print_arguments(self) -> List[tuple]:
        if self.at_statement_end():
            return []
        if self.operator() == "(":
            saved = self.pos
            try:
                self.pos += 1
                arguments = self.nested(self.parse_expression_list)
                self.expect(")")
                if self.at_statement_end() or self.operator() in (">", "|"):
                    return arguments
            except ProgramError:
                pass
            self.pos = saved
        previous = self.no_greater
        self.no_greater = True
        try:
            return self.parse_expression_list()
        finally:
            self.no_greater = previous

    def parse_expression_list(self) -> List[tuple]:
        items = [self.parse_expression()]
        while self.accept(",", newlines_after=True):
            items.append(self.parse_expression())
        return items

    def parse_expression(self) -> tuple:
        left = self.parse_ternary()
        op = self.operator()
        if op in AWK_ASSIGNMENTS:
            if left[0] not in ("var", "element", "field"):
                raise self.error(f"cannot assign to {left[0]}")
            self.pos += len(op)
            return ("assign", op, left, self.parse_expression())
        return left

    def parse_ternary(self) -> tuple:
        condition = self.parse_or()
        if self.accept("?", newlines_after=True):
            when_true = self.parse_ternary()
            self.skip(newlines=True)
            self.expect(":")
            self.skip(newlines=True)
            return ("ternary", condition, when_true, self.parse_ternary())
        return condition

    def parse_or(self) -> tuple:
        left = self.parse_and()
        while self.accept("||", newlines_after=True):
            left = ("or", left, self.parse_and())
        return left

    def parse_and(self) -> tuple:
        left = self.parse_in()
        while self.accept("&&", newlines_after=True):
            left = ("and", left, self.parse_in())
        return left

    def parse_in(self) -> tuple:
        left = self.parse_match()
        while self.word() == "in":
            self.pos += 2
            name = self.word()
            if not name or name in AWK_KEYWORDS:
                raise self.error("in needs an array name")
            self.pos += len(name)
            left = ("in", left, name)
        return left

    def parse_match(self) -> tuple:
        left = self.parse_relational()
        while True:
            op = self.operator()
            if op not in ("~", "!~"):
                return left
            self.pos += len(op)
            left = ("match", op, left, self.parse_relational())

    def parse_relational(self) -> tuple:
        left = self.parse_concatenation()
        op = self.operator()
        if op in ("<", "<=", "==", "!=", ">=", ">"):
            if op == ">" and self.no_greater:
                return left
            self.pos += len(op)
            return ("compare", op, left, self.parse_concatenation())
        return left

    def parse_concatenation(self) -> tuple:
        left = self.parse_additive()
        while self.starts_operand():
            left = ("concat", left, self.parse_additive())
        return left

    def starts_operand(self) -> bool:
        self.skip()
        source, pos = self.source, self.pos
        char = source[pos:pos + 1]
        if not char:
            return False
        if char in '"$(' or char.isdigit():
            return True
        if char == "." and source[pos + 1:pos + 2].isdigit():
            return True
        word = IDENT_RE.match(source, pos)
        return bool(word) and word.group() not in AWK_KEYWORDS

    def parse_additive(self) -> tuple:
        left = self.parse_multiplicative()
        while True:
            op = self.operator()
            if op not in ("+", "-"):
                return left
            self.pos += 1
            left = ("arith", op, left, self.parse_multiplicative())

    def parse_multiplicative(self) -> tuple:
        left = self.parse_unary()
        while True:
            op = self.operator()
            if op not in ("*", "/", "%"):
                return left
            self.pos += 1
            left = ("arith", op, left, self.parse_unary())

    def parse_unary(self) -> tuple:
        op = self.operator()
        if op in ("!", "-", "+"):
            self.pos += 1
            return ({"!": "not", "-": "negate", "+": "plus"}[op], self.parse_unary())
        return self.parse_power()

    def parse_power(self) -> tuple:
        base = self.parse_increment()
        if self.accept("^"):
            return ("arith", "^", base, self.parse_unary())
        return base

    def parse_increment(self) -> tuple:
        op = self.operator()
        if op in ("++", "--"):
            self.pos += 2
            target = self.parse_primary()
            if target[0] not in ("var", "element", "field"):
                raise self.error(f"{op} needs a variable, an array element or a field")
            return ("pre", op, target)
        primary = self.parse_primary()
        if primary[0] in ("var", "element", "field") and self.operator() in ("++", "--"):
            op = self.operator()
            self.pos += 2
            return ("post", op, primary)
        return primary

    def parse_primary(self) -> tuple:
        self.skip()
        source, pos = self.source, self.pos
        char = source[pos:pos + 1]
        if not char or char == "\n":
            raise self.error(f"unexpected {self.found()}")
        if char == '"':
            return ("string", self.parse_string())
        if char == "/":
            return ("regex", self.parse_regex())
        if char.isdigit() or char == "." and source[pos + 1:pos + 2].isdigit():
            number = AWK_NUMBER_RE.match(source, pos)
            self.pos = number.end()
            return ("number", float(number.group()))
        if char == "$":
            self.pos += 1
            if self.operator() in ("++", "--"):
                return ("field", self.parse_increment())
            return ("field", self.parse_primary())
        if char == "(":
            self.pos += 1
            inner = self.nested(self.parse_expression_list)
            self.expect(")")
            if len(inner) > 1:
                raise self.error("(a, b) grouping is only valid before in, which is not supported")
            return inner[0]
        word = IDENT_RE.match(source, pos)
        if not word:
            raise self.error(f"unexpected {self.found()}")
        name = word.group()
        if name in AWK_BUILTINS:
            self.pos = word.end()
            return self.parse_call(name)
        if name in AWK_KEYWORDS:
            raise self.error(f"{AWK_UNSUPPORTED[name]} are not supported" if name in AWK_UNSUPPORTED else f"unexpected keyword {name!r}")
        self.pos = word.end()
        if source.startswith("[", self.pos):
            self.pos += 1
            subscripts = self.nested(self.parse_expression_list)
            self.expect("]")
            return ("element", name, subscripts)
        if source.startswith("(", self.pos):
            raise self.error(f"{name}() is not a builtin and user-defined functions are not supported")
        return ("var", name)

    def parse_call(self, name: str) -> tuple:
        low, high = AWK_BUILTINS[name]
        if self.operator() != "(":
            if name == "length":
                return ("call", name, [])
            raise self.error(f"{name} needs (arguments)")
        self.pos += 1
        arguments = [] if self.operator() == ")" else self.nested(self.parse_expression_list)
        self.expect(")")
        if not low <= len(arguments) <= high:
            raise self.error(f"{name} takes {low} to {high} arguments, got {len(arguments)}")
        if name == "split" and arguments[1][0] != "var":
            raise self.error("split needs an array name as its second argument")
        if name in ("sub", "gsub") and len(arguments) == 3 and arguments[2][0] not in ("var", "element", "field"):
            raise self.error(f"{name} needs a variable, an array element or a field as its third argument")
        return ("call", name, arguments)

    def parse_string(self) -> str:
        start = self.pos
        self.pos += 1
        source = self.source
        while True:
            if self.pos >= len(source) or source[self.pos] == "\n":
                self.pos = start
                raise self.error("unterminated string")
            char = source[self.pos]
            if char == "\\":
                self.pos += 2
                continue
            if char == '"':
                self.pos += 1
                return awk_unescape(source[start + 1:self.pos - 1])
            self.pos += 1

    def parse_regex(self) -> str:
        start = self.pos
        self.pos += 1
        source = self.source
        text: List[str] = []
        in_bracket = False
        while True:
            if self.pos >= len(source) or source[self.pos] == "\n":
                self.pos = start
                raise self.error("unterminated regular expression")
            char = source[self.pos]
            if char == "\\" and self.pos + 1 < len(source):
                following = source[self.pos + 1]
                text.append("/" if following == "/" else "\\" + following)
                self.pos += 2
                continue
            if char == "/" and not in_bracket:
                self.pos += 1
                break
            if char == "[" and not in_bracket:
                in_bracket = True
            elif char == "]" and in_bracket:
                in_bracket = False
            text.append(char)
            self.pos += 1
        pattern = "".join(text)
        try:
            compile_ere(pattern)
        except re.error as error:
            self.pos = start
            raise self.error(f"bad regular expression /{pattern}/: {error}") from None
        return pattern


class StrNum(str):
    pass


class Uninitialized(StrNum):
    pass


UNINITIALIZED = Uninitialized("")


class NextRecord(Exception):
    pass


class BreakLoop(Exception):
    pass


class ContinueLoop(Exception):
    pass


class AwkExit(Exception):
    def __init__(self, code: Optional[int]) -> None:
        super().__init__(code)
        self.code = code


def awk_number(value: Any) -> float:
    if isinstance(value, float):
        return value
    if value.isascii() and value.isdigit():
        return float(value)
    match = LEADING_NUMBER_RE.match(value)
    return float(match.group(1)) if match else 0.0


def awk_numeric_view(value: Any) -> Optional[float]:
    if isinstance(value, float):
        return value
    if isinstance(value, StrNum):
        if isinstance(value, Uninitialized):
            return 0.0
        if value.isascii() and value.isdigit():
            return float(value)
        return float(value) if WHOLE_NUMBER_RE.match(value) else None
    return None


def awk_format_number(number: float, fmt: str) -> str:
    if math.isnan(number):
        return "nan" if math.copysign(1.0, number) > 0 else "-nan"
    if math.isinf(number):
        return "inf" if number > 0 else "-inf"
    if number == int(number) and abs(number) < 1e16:
        return str(int(number))
    return fmt % number


def awk_string(value: Any, fmt: str = "%.6g") -> str:
    if isinstance(value, float):
        return awk_format_number(value, fmt)
    return value


def awk_truth(value: Any) -> bool:
    if isinstance(value, float):
        return value != 0.0
    if isinstance(value, StrNum):
        number = awk_numeric_view(value)
        return number != 0.0 if number is not None else value != ""
    return value != ""


def awk_compare(left: Any, right: Any, convfmt: str) -> int:
    left_number, right_number = awk_numeric_view(left), awk_numeric_view(right)
    if left_number is not None and right_number is not None:
        return (left_number > right_number) - (left_number < right_number)
    left_text, right_text = awk_string(left, convfmt), awk_string(right, convfmt)
    return (left_text > right_text) - (left_text < right_text)


def awk_arith(op: str, left: float, right: float) -> float:
    if op == "+":
        return left + right
    if op == "-":
        return left - right
    if op == "*":
        return left * right
    if op == "/":
        if right == 0:
            raise AwkError("division by zero")
        return left / right
    if op == "%":
        if right == 0:
            raise AwkError("division by zero in %")
        return math.fmod(left, right)
    try:
        return math.pow(left, right)
    except OverflowError:
        return math.inf
    except ValueError:
        return math.nan


def awk_round(number: float) -> int:
    if math.isnan(number):
        return 0
    if math.isinf(number):
        return 2 ** 62 if number > 0 else -(2 ** 62)
    return int(math.floor(number + 0.5))


def regex_split(pattern: str, text: str) -> List[str]:
    parts: List[str] = []
    last = 0
    for match in compile_ere(pattern).finditer(text):
        if match.end() == match.start():
            continue
        parts.append(text[last:match.start()])
        last = match.end()
    parts.append(text[last:])
    return parts


def split_fields(text: str, separator: str, as_regex: bool = False) -> List[str]:
    if text == "":
        return []
    if not as_regex:
        if separator == " ":
            stripped = text.strip(" \t\n")
            return DEFAULT_FS_RE.split(stripped) if stripped else []
        if len(separator) == 1 and separator != "\\":
            return text.split(separator)
    return regex_split(separator, text)


def expand_replacement(replacement: str, matched: str) -> str:
    out: List[str] = []
    index = 0
    while index < len(replacement):
        char = replacement[index]
        if char == "\\" and replacement[index + 1:index + 2] in ("&", "\\"):
            out.append(replacement[index + 1])
            index += 2
        elif char == "&":
            out.append(matched)
            index += 1
        else:
            out.append(char)
            index += 1
    return "".join(out)


def awk_sprintf(fmt: str, values: List[Any], convfmt: str) -> str:
    remaining = list(values)

    def take() -> Any:
        return remaining.pop(0) if remaining else UNINITIALIZED

    pieces: List[str] = []
    position = 0
    for match in FORMAT_SPEC_RE.finditer(fmt):
        pieces.append(fmt[position:match.start()])
        position = match.end()
        flags, width, precision, conversion = match.groups()
        if conversion == "%":
            pieces.append("%")
            continue
        if width == "*":
            width = str(int(awk_number(take())))
        if precision == "*":
            precision = str(int(awk_number(take())))
        spec = "%" + flags + (width or "") + ("" if precision is None else "." + (precision or "0"))
        value = take()
        if conversion in "di":
            pieces.append((spec + "d") % awk_round_toward_zero(awk_number(value)))
        elif conversion in "ouxX":
            pieces.append((spec + ("d" if conversion == "u" else conversion)) % awk_round_toward_zero(awk_number(value)))
        elif conversion in "eEfFgG":
            pieces.append((spec + conversion) % awk_number(value))
        elif conversion == "c":
            character = chr(int(value)) if isinstance(value, float) else awk_string(value, convfmt)[:1]
            pieces.append((spec + "s") % character)
        elif conversion == "s":
            pieces.append((spec + "s") % awk_string(value, convfmt))
        else:
            raise AwkError(f"unknown format %{conversion} in {fmt!r}")
    pieces.append(fmt[position:])
    return "".join(pieces)


def awk_round_toward_zero(number: float) -> int:
    if math.isnan(number) or math.isinf(number):
        return 0
    return math.trunc(number)


class AwkProgram:
    def __init__(self, source: str) -> None:
        self.source = source
        self.tree = AwkParser(source).parse()
        self.begin = [item[1] for item in self.tree[1] if item[0] == "begin"]
        self.end = [item[1] for item in self.tree[1] if item[0] == "end"]
        self.rules = [(item[1], item[2]) for item in self.tree[1] if item[0] == "rule"]
        self.statements = {name[5:]: getattr(self, name) for name in dir(self) if name.startswith("exec_")}
        self.expressions = {name[5:]: getattr(self, name) for name in dir(self) if name.startswith("eval_")}
        self.out: Any = None
        self.variables: Dict[str, Any] = {}
        self.arrays: Dict[str, Dict[str, Any]] = {}
        self.record = ""
        self.record_fs = " "
        self.fields: Optional[List[str]] = None

    def run(self, streams: Iterable[Tuple[str, Iterable[str]]], out: Any, assignments: Optional[Dict[str, str]] = None, field_separator: Optional[str] = None, stats: Optional[Dict[str, Any]] = None) -> int:
        stats = {} if stats is None else stats
        stats.setdefault("records", 0)
        stats.setdefault("outputs", 0)
        self.out = out
        self.stats = stats
        self.variables = {"FS": " ", "OFS": " ", "ORS": "\n", "RS": "\n", "SUBSEP": "\x1c", "CONVFMT": "%.6g", "OFMT": "%.6g", "NR": 0.0, "FNR": 0.0, "RSTART": 0.0, "RLENGTH": -1.0, "FILENAME": ""}
        self.arrays = {}
        self.set_record("")
        if field_separator is not None:
            self.variables["FS"] = field_separator
        for name, value in (assignments or {}).items():
            self.variables[name] = StrNum(value)
        exit_code = 0
        try:
            try:
                for block in self.begin:
                    self.execute(block)
                if self.rules or self.end:
                    self.read_records(streams)
            except AwkExit as stop:
                exit_code = stop.code if stop.code is not None else exit_code
            try:
                for block in self.end:
                    self.execute(block)
            except AwkExit as stop:
                exit_code = stop.code if stop.code is not None else exit_code
        except NextRecord:
            raise AwkError("next is only valid in a rule, not in BEGIN or END") from None
        except (BreakLoop, ContinueLoop):
            raise AwkError("break and continue are only valid inside a loop") from None
        except AwkError as error:
            raise AwkError(f"{error} (at record {int(awk_number(self.variables['NR']))})") from None
        return exit_code

    def read_records(self, streams: Iterable[Tuple[str, Iterable[str]]]) -> None:
        variables = self.variables
        if awk_string(variables["RS"]) != "\n":
            raise AwkError('only RS = "\\n" is supported')
        for name, stream in streams:
            variables["FILENAME"] = name
            variables["FNR"] = 0.0
            for line in stream:
                if line.endswith("\n"):
                    line = line[:-1]
                variables["NR"] = awk_number(variables["NR"]) + 1.0
                variables["FNR"] = awk_number(variables["FNR"]) + 1.0
                self.stats["records"] += 1
                self.set_record(line)
                try:
                    for pattern, action in self.rules:
                        if pattern is None or awk_truth(self.evaluate(pattern)):
                            if action is None:
                                self.write(self.record + awk_string(variables["ORS"]))
                            else:
                                self.execute(action)
                except NextRecord:
                    continue

    def write(self, text: str) -> None:
        self.out.write(text)
        self.stats["outputs"] += 1

    def convfmt(self) -> str:
        return awk_string(self.variables["CONVFMT"])

    def set_record(self, text: str) -> None:
        self.record = text
        self.fields = None
        self.record_fs = awk_string(self.variables.get("FS", " "))

    def get_fields(self) -> List[str]:
        if self.fields is None:
            self.fields = split_fields(self.record, self.record_fs)
        return self.fields

    def get_field(self, index: int) -> Any:
        if index == 0:
            return StrNum(self.record)
        if index < 0:
            raise AwkError(f"attempt to access field {index}")
        fields = self.get_fields()
        return StrNum(fields[index - 1]) if index <= len(fields) else UNINITIALIZED

    def set_field(self, index: int, value: Any) -> None:
        if index == 0:
            self.set_record(awk_string(value, self.convfmt()))
            return
        if index < 0:
            raise AwkError(f"attempt to access field {index}")
        fields = self.get_fields()
        while len(fields) < index:
            fields.append("")
        fields[index - 1] = awk_string(value, self.convfmt())
        self.record = awk_string(self.variables["OFS"]).join(fields)

    def set_nf(self, value: Any) -> None:
        count = int(awk_number(value))
        fields = self.get_fields()
        del fields[count:]
        while len(fields) < count:
            fields.append("")
        self.record = awk_string(self.variables["OFS"]).join(fields)

    def array(self, name: str) -> Dict[str, Any]:
        if name in self.variables:
            raise AwkError(f"cannot use scalar {name} as an array")
        return self.arrays.setdefault(name, {})

    def assign_variable(self, name: str, value: Any) -> None:
        if name in self.arrays:
            raise AwkError(f"cannot assign to array {name}")
        if name == "NF":
            self.set_nf(value)
        else:
            self.variables[name] = value

    def subscript(self, nodes: List[tuple]) -> str:
        convfmt = self.convfmt()
        parts = [awk_string(self.evaluate(node), convfmt) for node in nodes]
        return parts[0] if len(parts) == 1 else awk_string(self.variables["SUBSEP"]).join(parts)

    def field_index(self, node: tuple) -> int:
        return int(awk_number(self.evaluate(node)))

    def store(self, target: tuple, value: Any) -> None:
        if target[0] == "var":
            self.assign_variable(target[1], value)
        elif target[0] == "element":
            self.array(target[1])[self.subscript(target[2])] = value
        else:
            self.set_field(self.field_index(target[1]), value)

    def regex_of(self, node: tuple) -> "re.Pattern[str]":
        pattern = node[1] if node[0] == "regex" else awk_string(self.evaluate(node), self.convfmt())
        try:
            return compile_ere(pattern)
        except re.error as error:
            raise AwkError(f"bad regular expression {pattern!r}: {error}") from None

    def string_of(self, node: tuple) -> str:
        return awk_string(self.evaluate(node), self.convfmt())

    def execute(self, node: tuple) -> None:
        self.statements[node[0]](node)

    def exec_block(self, node: tuple) -> None:
        for statement in node[1]:
            self.statements[statement[0]](statement)

    def exec_expression(self, node: tuple) -> None:
        self.evaluate(node[1])

    def exec_print(self, node: tuple) -> None:
        variables = self.variables
        if not node[1]:
            text = self.record
        else:
            ofmt = awk_string(variables["OFMT"])
            text = awk_string(variables["OFS"]).join(awk_string(self.evaluate(argument), ofmt) for argument in node[1])
        self.write(text + awk_string(variables["ORS"]))

    def exec_printf(self, node: tuple) -> None:
        values = [self.evaluate(argument) for argument in node[1]]
        convfmt = self.convfmt()
        self.write(awk_sprintf(awk_string(values[0], convfmt), values[1:], convfmt))

    def exec_if(self, node: tuple) -> None:
        if awk_truth(self.evaluate(node[1])):
            self.execute(node[2])
        elif node[3] is not None:
            self.execute(node[3])

    def exec_while(self, node: tuple) -> None:
        while awk_truth(self.evaluate(node[1])):
            try:
                self.execute(node[2])
            except BreakLoop:
                break
            except ContinueLoop:
                continue

    def exec_for(self, node: tuple) -> None:
        _, initial, condition, step, body = node
        if initial is not None:
            self.execute(initial)
        while condition is None or awk_truth(self.evaluate(condition)):
            try:
                self.execute(body)
            except BreakLoop:
                break
            except ContinueLoop:
                pass
            if step is not None:
                self.execute(step)

    def exec_for_in(self, node: tuple) -> None:
        _, name, array_name, body = node
        for key in list(self.array(array_name)):
            self.assign_variable(name, key)
            try:
                self.execute(body)
            except BreakLoop:
                break
            except ContinueLoop:
                continue

    def exec_next(self, node: tuple) -> None:
        raise NextRecord()

    def exec_break(self, node: tuple) -> None:
        raise BreakLoop()

    def exec_continue(self, node: tuple) -> None:
        raise ContinueLoop()

    def exec_exit(self, node: tuple) -> None:
        raise AwkExit(None if node[1] is None else int(awk_number(self.evaluate(node[1]))))

    def exec_delete(self, node: tuple) -> None:
        array = self.array(node[1])
        if node[2] is None:
            array.clear()
        else:
            array.pop(self.subscript(node[2]), None)

    def evaluate(self, node: tuple) -> Any:
        return self.expressions[node[0]](node)

    def eval_number(self, node: tuple) -> Any:
        return node[1]

    def eval_string(self, node: tuple) -> Any:
        return node[1]

    def eval_regex(self, node: tuple) -> Any:
        return 1.0 if compile_ere(node[1]).search(self.record) else 0.0

    def eval_var(self, node: tuple) -> Any:
        name = node[1]
        if name == "NF":
            return float(len(self.get_fields()))
        value = self.variables.get(name)
        if value is None:
            if name in self.arrays:
                raise AwkError(f"cannot use array {name} in a scalar context")
            return UNINITIALIZED
        return value

    def eval_element(self, node: tuple) -> Any:
        array = self.array(node[1])
        key = self.subscript(node[2])
        value = array.get(key)
        if value is None:
            array[key] = UNINITIALIZED
            return UNINITIALIZED
        return value

    def eval_field(self, node: tuple) -> Any:
        return self.get_field(self.field_index(node[1]))

    def eval_assign(self, node: tuple) -> Any:
        _, op, target, expression = node
        value = self.evaluate(expression)
        if op != "=":
            value = awk_arith(op[0], awk_number(self.evaluate(target)), awk_number(value))
        self.store(target, value)
        return value

    def eval_ternary(self, node: tuple) -> Any:
        return self.evaluate(node[2] if awk_truth(self.evaluate(node[1])) else node[3])

    def eval_or(self, node: tuple) -> Any:
        return 1.0 if awk_truth(self.evaluate(node[1])) or awk_truth(self.evaluate(node[2])) else 0.0

    def eval_and(self, node: tuple) -> Any:
        return 1.0 if awk_truth(self.evaluate(node[1])) and awk_truth(self.evaluate(node[2])) else 0.0

    def eval_not(self, node: tuple) -> Any:
        return 0.0 if awk_truth(self.evaluate(node[1])) else 1.0

    def eval_negate(self, node: tuple) -> Any:
        return -awk_number(self.evaluate(node[1]))

    def eval_plus(self, node: tuple) -> Any:
        return awk_number(self.evaluate(node[1]))

    def eval_arith(self, node: tuple) -> Any:
        return awk_arith(node[1], awk_number(self.evaluate(node[2])), awk_number(self.evaluate(node[3])))

    def eval_concat(self, node: tuple) -> Any:
        convfmt = self.convfmt()
        return awk_string(self.evaluate(node[1]), convfmt) + awk_string(self.evaluate(node[2]), convfmt)

    def eval_compare(self, node: tuple) -> Any:
        order = awk_compare(self.evaluate(node[2]), self.evaluate(node[3]), self.convfmt())
        return 1.0 if JQ_COMPARISONS[node[1]](order) else 0.0

    def eval_match(self, node: tuple) -> Any:
        text = self.string_of(node[2])
        found = self.regex_of(node[3]).search(text) is not None
        return 1.0 if found == (node[1] == "~") else 0.0

    def eval_in(self, node: tuple) -> Any:
        return 1.0 if self.subscript([node[1]]) in self.arrays.get(node[2], {}) else 0.0

    def eval_pre(self, node: tuple) -> Any:
        value = awk_number(self.evaluate(node[2])) + (1.0 if node[1] == "++" else -1.0)
        self.store(node[2], value)
        return value

    def eval_post(self, node: tuple) -> Any:
        old = awk_number(self.evaluate(node[2]))
        self.store(node[2], old + (1.0 if node[1] == "++" else -1.0))
        return old

    def eval_call(self, node: tuple) -> Any:
        return getattr(self, "builtin_" + node[1])(node[2])

    def builtin_length(self, arguments: List[tuple]) -> float:
        if not arguments:
            return float(len(self.record))
        argument = arguments[0]
        if argument[0] == "var" and argument[1] in self.arrays:
            return float(len(self.arrays[argument[1]]))
        return float(len(self.string_of(argument)))

    def builtin_substr(self, arguments: List[tuple]) -> str:
        text = self.string_of(arguments[0])
        start = awk_round(awk_number(self.evaluate(arguments[1])))
        end = start + awk_round(awk_number(self.evaluate(arguments[2]))) if len(arguments) == 3 else len(text) + 1
        start, end = max(start, 1), min(end, len(text) + 1)
        return text[start - 1:end - 1] if end > start else ""

    def builtin_index(self, arguments: List[tuple]) -> float:
        return float(self.string_of(arguments[0]).find(self.string_of(arguments[1])) + 1)

    def builtin_split(self, arguments: List[tuple]) -> float:
        text = self.string_of(arguments[0])
        if len(arguments) == 3 and arguments[2][0] == "regex":
            parts = split_fields(text, arguments[2][1], as_regex=True)
        else:
            separator = self.string_of(arguments[2]) if len(arguments) == 3 else awk_string(self.variables["FS"])
            parts = split_fields(text, separator)
        array = self.array(arguments[1][1])
        array.clear()
        for index, part in enumerate(parts, 1):
            array[str(index)] = StrNum(part)
        return float(len(parts))

    def builtin_sub(self, arguments: List[tuple]) -> float:
        return self.substitute(arguments, 1)

    def builtin_gsub(self, arguments: List[tuple]) -> float:
        return self.substitute(arguments, 0)

    def substitute(self, arguments: List[tuple], count: int) -> float:
        pattern = self.regex_of(arguments[0])
        replacement = self.string_of(arguments[1])
        target = arguments[2] if len(arguments) == 3 else ("field", ("number", 0.0))
        result, replaced = pattern.subn(lambda match: expand_replacement(replacement, match.group()), self.string_of(target), count=count)
        if replaced:
            self.store(target, result)
        return float(replaced)

    def builtin_match(self, arguments: List[tuple]) -> float:
        found = self.regex_of(arguments[1]).search(self.string_of(arguments[0]))
        start, length = (float(found.start() + 1), float(found.end() - found.start())) if found else (0.0, -1.0)
        self.variables["RSTART"] = start
        self.variables["RLENGTH"] = length
        return start

    def builtin_sprintf(self, arguments: List[tuple]) -> str:
        values = [self.evaluate(argument) for argument in arguments]
        convfmt = self.convfmt()
        return awk_sprintf(awk_string(values[0], convfmt), values[1:], convfmt)

    def builtin_tolower(self, arguments: List[tuple]) -> str:
        return self.string_of(arguments[0]).lower()

    def builtin_toupper(self, arguments: List[tuple]) -> str:
        return self.string_of(arguments[0]).upper()

    def builtin_int(self, arguments: List[tuple]) -> float:
        number = awk_number(self.evaluate(arguments[0]))
        return number if math.isnan(number) or math.isinf(number) else float(math.trunc(number))

    def math_function(self, function: Callable[..., float], arguments: List[tuple]) -> float:
        try:
            return function(*(awk_number(self.evaluate(argument)) for argument in arguments))
        except (ValueError, OverflowError):
            return math.nan

    def builtin_sqrt(self, arguments: List[tuple]) -> float:
        return self.math_function(math.sqrt, arguments)

    def builtin_exp(self, arguments: List[tuple]) -> float:
        return self.math_function(math.exp, arguments)

    def builtin_log(self, arguments: List[tuple]) -> float:
        return self.math_function(math.log, arguments)

    def builtin_sin(self, arguments: List[tuple]) -> float:
        return self.math_function(math.sin, arguments)

    def builtin_cos(self, arguments: List[tuple]) -> float:
        return self.math_function(math.cos, arguments)

    def builtin_atan2(self, arguments: List[tuple]) -> float:
        return self.math_function(math.atan2, arguments)


def open_inputs(paths: List[str], stdin: Any) -> Iterator[Tuple[str, Any]]:
    if not paths:
        yield "-", stdin
        return
    for path in paths:
        if path == "-":
            yield "-", stdin
            continue
        with open(path, encoding="utf-8", errors="surrogateescape", newline="\n") as handle:
            yield path, handle


def parse_assignment(text: str) -> Tuple[str, str]:
    name, separator, value = text.partition("=")
    if not separator or not IDENT_RE.fullmatch(name):
        raise ValueError(f"-v expects NAME=VALUE, got {text!r}")
    return name, awk_unescape(value)


def peak_rss_kb() -> Optional[int]:
    try:
        import resource
    except ImportError:
        return None
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return peak // 1024 if sys.platform == "darwin" else peak


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="ministream", description="A jq subset and an awk subset that stream their input in constant memory.", epilog="exit codes: 0 ok, 1 runtime or input error, 2 usage or syntax error")
    parser.add_argument("--stats", action="store_true", help="print records, outputs, seconds and peak memory as one JSON line on stderr")
    commands = parser.add_subparsers(dest="command", metavar="{jq,awk,explain}")
    commands.required = True
    jq = commands.add_parser("jq", help="run a jq filter over a stream of JSON values")
    jq.add_argument("-r", "--raw-output", action="store_true", help="print strings without quotes")
    jq.add_argument("-c", "--compact-output", action="store_true", help="one JSON value per line")
    jq.add_argument("-s", "--slurp", action="store_true", help="read every input value into one array first (not constant memory)")
    jq.add_argument("-n", "--null-input", action="store_true", help="run the filter once with null as input")
    jq.add_argument("--arg", nargs=2, action="append", default=[], metavar=("NAME", "VALUE"), help="bind $NAME to the string VALUE")
    jq.add_argument("filter")
    jq.add_argument("files", nargs="*")
    awk = commands.add_parser("awk", help="run an awk program over lines of text")
    awk.add_argument("-F", dest="field_separator", help="field separator: one character, a regular expression, or ' ' for runs of blanks")
    awk.add_argument("-v", dest="assignments", action="append", default=[], metavar="NAME=VALUE", help="set a variable before BEGIN")
    awk.add_argument("program")
    awk.add_argument("files", nargs="*")
    explain = commands.add_parser("explain", help="print the parsed program as a tree")
    explain.add_argument("--json", action="store_true", help="print the tree as JSON")
    explain.add_argument("language", choices=("jq", "awk"))
    explain.add_argument("program")
    return parser


def standard_input() -> Any:
    if hasattr(sys.stdin, "reconfigure"):
        sys.stdin.reconfigure(errors="surrogateescape", newline="\n")
    return sys.stdin


def standard_output() -> Any:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="surrogateescape")
    return sys.stdout


def run_command(args: argparse.Namespace, stdin: Any, stdout: Any, stderr: Any, stats: Dict[str, Any]) -> int:
    if args.command == "explain":
        program = JqProgram(args.program) if args.language == "jq" else AwkProgram(args.program)
        if args.json:
            stdout.write(json.dumps(tree_to_json(program.tree), indent=2) + "\n")
        else:
            stdout.write("\n".join(explain_lines(program.tree)) + "\n")
        return EXIT_OK
    if args.command == "jq":
        variables = {name: value for name, value in args.arg}
        jq_program = JqProgram(args.filter, variables)
        return run_jq(jq_program, open_inputs(args.files, stdin), stdout, stderr, raw=args.raw_output, compact=args.compact_output, slurp=args.slurp, null_input=args.null_input, variables=variables, stats=stats)
    assignments = dict(parse_assignment(item) for item in args.assignments)
    awk_program = AwkProgram(args.program)
    field_separator = None if args.field_separator is None else awk_unescape(args.field_separator)
    return awk_program.run(open_inputs(args.files, stdin), stdout, assignments, field_separator, stats)


def main(argv: Optional[List[str]] = None, stdin: Any = None, stdout: Any = None, stderr: Any = None) -> int:
    argv = sys.argv[1:] if argv is None else list(argv)
    stderr = sys.stderr if stderr is None else stderr
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exit_request:
        return EXIT_USAGE if exit_request.code else EXIT_OK
    stdin = standard_input() if stdin is None else stdin
    stdout = standard_output() if stdout is None else stdout
    language = args.language if args.command == "explain" else args.command
    stats: Dict[str, Any] = {"tool": language, "records": 0, "outputs": 0}
    started = time.monotonic()
    try:
        code = run_command(args, stdin, stdout, stderr, stats)
    except StreamError as error:
        stderr.write(f"ministream: {language}: {error}\n")
        code = error.exit_code
    except ValueError as error:
        stderr.write(f"ministream: {language}: {error}\n")
        code = EXIT_USAGE
    except OSError as error:
        stderr.write(f"ministream: {language}: {error.filename or ''}: {error.strerror}\n")
        code = EXIT_USAGE
    stdout.flush()
    if args.stats:
        stats["seconds"] = round(time.monotonic() - started, 3)
        stats["max_rss_kb"] = peak_rss_kb()
        stderr.write(json.dumps(stats) + "\n")
    return code


def restore_default_sigpipe() -> None:
    if hasattr(signal, "SIGPIPE"):
        signal.signal(signal.SIGPIPE, signal.SIG_DFL)


if __name__ == "__main__":
    restore_default_sigpipe()
    raise SystemExit(main())
