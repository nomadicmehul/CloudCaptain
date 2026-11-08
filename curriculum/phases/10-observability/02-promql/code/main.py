"""miniql: evaluate a PromQL subset over series loaded from OpenMetrics the way Prometheus does (lookback, extrapolated rate(), aggregation, vector matching, histogram_quantile(), range queries), explain which samples every value used, serve the query API, run promtool-style rule tests and compare answers with a real Prometheus."""
from __future__ import annotations

import argparse
import bisect
import calendar
import json
import math
import re
import sys
import threading
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Set, Tuple, Union

NAME = "__name__"
DEFAULT_LOOKBACK_MS = 300_000
DEFAULT_START = 1767225600
MAX_POINTS = 11000
SMALL_DELTA_TOLERANCE = 1e-12
MIN_NORMAL = 2.2250738585072014e-308
DURATION_UNITS = {"y": 31_536_000_000, "w": 604_800_000, "d": 86_400_000, "h": 3_600_000, "m": 60_000, "s": 1000, "ms": 1}
DURATION_TEXT = re.compile(r"(?:\d+(?:ms|[smhdwy]))+")
DURATION_PART = re.compile(r"(\d+)(ms|[smhdwy])")
NUMBER_TEXT = re.compile(r"0[xX][0-9a-fA-F]+|(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?")
IDENT_TEXT = re.compile(r"[a-zA-Z_:][a-zA-Z0-9_:]*")
METRIC_NAME = re.compile(r"[a-zA-Z_:][a-zA-Z0-9_:]*")
LABEL_NAME = re.compile(r"[a-zA-Z_][a-zA-Z0-9_]*")
OPERATORS = ("=~", "!~", "!=", "==", "<=", ">=", "=", "<", ">", "+", "-", "*", "/", "%", "^", "(", ")", "{", "}", "[", "]", ",", "@")
ARITHMETIC = ("+", "-", "*", "/", "%", "^", "atan2")
COMPARISON = ("==", "!=", ">", "<", ">=", "<=")
SET_OPERATORS = ("and", "or", "unless")
PRECEDENCE = {"or": 1, "and": 2, "unless": 2, "==": 3, "!=": 3, "<": 3, ">": 3, "<=": 3, ">=": 3, "+": 4, "-": 4, "*": 5, "/": 5, "%": 5, "atan2": 5, "^": 6}
AGGREGATIONS = ("sum", "avg", "min", "max", "count", "topk", "bottomk")
UNSUPPORTED_AGGREGATIONS = ("stddev", "stdvar", "quantile", "count_values", "group", "limitk", "limit_ratio")
KEYWORDS = ("by", "without", "on", "ignoring", "group_left", "group_right", "bool", "offset") + SET_OPERATORS + ("atan2",)
SCALAR, VECTOR, MATRIX, STRING = "scalar", "instant vector", "range vector", "string"
RATE_FUNCTIONS = ("rate", "increase", "delta", "irate")
OVER_TIME_FUNCTIONS = ("avg_over_time", "min_over_time", "max_over_time", "sum_over_time", "count_over_time", "last_over_time", "quantile_over_time")
FUNCTIONS: Dict[str, Tuple[Tuple[str, ...], str]] = {
    "rate": ((MATRIX,), VECTOR),
    "increase": ((MATRIX,), VECTOR),
    "delta": ((MATRIX,), VECTOR),
    "irate": ((MATRIX,), VECTOR),
    "avg_over_time": ((MATRIX,), VECTOR),
    "min_over_time": ((MATRIX,), VECTOR),
    "max_over_time": ((MATRIX,), VECTOR),
    "sum_over_time": ((MATRIX,), VECTOR),
    "count_over_time": ((MATRIX,), VECTOR),
    "last_over_time": ((MATRIX,), VECTOR),
    "quantile_over_time": ((SCALAR, MATRIX), VECTOR),
    "histogram_quantile": ((SCALAR, VECTOR), VECTOR),
    "abs": ((VECTOR,), VECTOR),
    "vector": ((SCALAR,), VECTOR),
    "scalar": ((VECTOR,), SCALAR),
    "time": ((), SCALAR),
}
UNSUPPORTED_FUNCTIONS = (
    "absent", "absent_over_time", "ceil", "changes", "clamp", "clamp_max", "clamp_min", "day_of_month", "day_of_week",
    "deriv", "double_exponential_smoothing", "exp", "floor", "histogram_avg", "histogram_count", "histogram_fraction",
    "histogram_sum", "holt_winters", "idelta", "label_join", "label_replace", "ln", "log10", "log2", "predict_linear",
    "present_over_time", "resets", "round", "sgn", "sort", "sort_desc", "sqrt", "stddev_over_time", "stdvar_over_time",
    "timestamp",
)
COUNTER_SUFFIXES = ("_total", "_sum", "_count", "_bucket")
BASE_PLACEHOLDER = re.compile(r"\$\{base([+-]\d+)?\}")
EXPANDING = re.compile(r"([+-]?(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?)([+-])((?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?)x(\d+)")
REPEATING = re.compile(r"([+-]?(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?|[+-]?Inf|NaN)x(\d+)")
MISSING_RUN = re.compile(r"_x(\d+)")
STALE = "stale"

Labels = Tuple[Tuple[str, str], ...]


class ParseError(ValueError):
    pass


class InputError(ValueError):
    pass


class EvalError(RuntimeError):
    pass


def parse_duration(text: str) -> int:
    text = text.strip()
    if not text or not DURATION_TEXT.fullmatch(text):
        raise InputError(f"not a duration: {text!r} (examples: 30s, 5m, 1h30m, 500ms)")
    return sum(int(amount) * DURATION_UNITS[unit] for amount, unit in DURATION_PART.findall(text))


def format_duration(ms: int) -> str:
    if ms == 0:
        return "0s"
    sign, ms = ("-", -ms) if ms < 0 else ("", ms)
    parts = []
    for unit, size in DURATION_UNITS.items():
        if ms >= size:
            parts.append(f"{ms // size}{unit}")
            ms %= size
    return sign + "".join(parts)


def go_round(value: float) -> int:
    return int(math.copysign(math.floor(abs(value) + 0.5), value))


def shortest_digits(value: float) -> Tuple[str, int]:
    sign, digits, exponent = Decimal(repr(abs(value))).normalize().as_tuple()
    text = "".join(str(d) for d in digits)
    return text, len(text) + int(exponent)


def fmt_value(value: float) -> str:
    if math.isnan(value):
        return "NaN"
    if math.isinf(value):
        return "+Inf" if value > 0 else "-Inf"
    if value == 0:
        return "-0" if math.copysign(1.0, value) < 0 else "0"
    digits, point = shortest_digits(value)
    sign = "-" if value < 0 else ""
    if point <= 0:
        return f"{sign}0.{'0' * -point}{digits}"
    if point >= len(digits):
        return sign + digits + "0" * (point - len(digits))
    return f"{sign}{digits[:point]}.{digits[point:]}"


def go_shortest_g(value: float) -> str:
    if value == 0:
        return "0"
    digits, point = shortest_digits(value)
    sign = "-" if value < 0 else ""
    exponent = point - 1
    if exponent < -4 or exponent >= 6:
        mantissa = digits[0] + ("." + digits[1:] if len(digits) > 1 else "")
        return f"{sign}{mantissa}e{'+' if exponent >= 0 else '-'}{abs(exponent):02d}"
    return fmt_value(value)


def openmetrics_float(value: float) -> str:
    if math.isnan(value):
        return "NaN"
    if math.isinf(value):
        return "+Inf" if value > 0 else "-Inf"
    text = go_shortest_g(value)
    if "e" in text or "." in text:
        return text
    return text + ".0"


def parse_go_float(text: str) -> float:
    cleaned = text.strip()
    if not cleaned or "_" in cleaned:
        raise ValueError(f"not a number: {text!r}")
    return float(cleaned)


def escape_label_value(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def format_labels(labels: Labels) -> str:
    name = ""
    rest = []
    for key, value in labels:
        if key == NAME:
            name = value
        else:
            rest.append(f'{key}="{escape_label_value(value)}"')
    if not rest:
        return name or "{}"
    return name + "{" + ",".join(rest) + "}"


def drop_name(labels: Labels) -> Labels:
    return tuple(pair for pair in labels if pair[0] != NAME)


def label_dict(labels: Labels) -> Dict[str, str]:
    return dict(labels)


def as_labels(mapping: Dict[str, str]) -> Labels:
    return tuple(sorted((key, value) for key, value in mapping.items() if value != ""))


def relative(ms: int) -> str:
    if ms == 0:
        return "0s"
    return f"{'+' if ms > 0 else '-'}{fmt_value(abs(ms) / 1000)}s"


def iso(ms: int) -> str:
    stamp = datetime.fromtimestamp(ms / 1000, tz=timezone.utc)
    text = stamp.strftime("%Y-%m-%dT%H:%M:%S")
    if ms % 1000:
        text += f".{ms % 1000:03d}"
    return text + "Z"


def indent(lines: Iterable[str], depth: int = 1) -> Tuple[str, ...]:
    pad = "  " * depth
    return tuple(pad + line for line in lines)


def kahan_sum_inc(increment: float, total: float, compensation: float) -> Tuple[float, float]:
    result = total + increment
    if math.isinf(result):
        compensation = 0.0
    elif abs(total) >= abs(increment):
        compensation += (total - result) + increment
    else:
        compensation += (increment - result) + total
    return result, compensation


def almost_equal(a: float, b: float, epsilon: float) -> bool:
    if math.isnan(a) and math.isnan(b):
        return True
    if a == b:
        return True
    total = abs(a) + abs(b)
    difference = abs(a - b)
    if a == 0 or b == 0 or total < MIN_NORMAL:
        return difference < epsilon * MIN_NORMAL
    return difference / min(total, sys.float_info.max) < epsilon


@dataclass
class Token:
    kind: str
    text: str
    pos: int
    value: Any = None


def read_string(text: str, start: int) -> Tuple[str, int]:
    quote = text[start]
    i = start + 1
    if quote == "`":
        end = text.find("`", i)
        if end < 0:
            raise ParseError(f"parse error at char {start + 1}: unterminated raw string")
        return text[i:end], end + 1
    out = []
    simple = {"\\": "\\", '"': '"', "'": "'", "n": "\n", "t": "\t", "r": "\r", "a": "\a", "b": "\b", "f": "\f", "v": "\v"}
    while i < len(text):
        ch = text[i]
        if ch == quote:
            return "".join(out), i + 1
        if ch == "\n":
            break
        if ch == "\\":
            i += 1
            if i >= len(text):
                break
            escape = text[i]
            if escape in simple:
                out.append(simple[escape])
                i += 1
                continue
            widths = {"x": 2, "u": 4, "U": 8}
            if escape in widths:
                digits = text[i + 1:i + 1 + widths[escape]]
                if len(digits) != widths[escape] or not re.fullmatch(r"[0-9a-fA-F]+", digits):
                    raise ParseError(f"parse error at char {i}: invalid escape sequence \\{escape}{digits}")
                out.append(chr(int(digits, 16)))
                i += 1 + widths[escape]
                continue
            raise ParseError(f"parse error at char {i}: unknown escape sequence \\{escape} (write \\\\{escape} for a regex)")
        out.append(ch)
        i += 1
    raise ParseError(f"parse error at char {start + 1}: unterminated quoted string")


def tokenize(text: str) -> List[Token]:
    tokens: List[Token] = []
    i = 0
    while i < len(text):
        ch = text[i]
        if ch.isspace():
            i += 1
            continue
        if ch == "#":
            end = text.find("\n", i)
            i = len(text) if end < 0 else end
            continue
        if ch in "\"'`":
            value, end = read_string(text, i)
            tokens.append(Token("string", text[i:end], i, value))
            i = end
            continue
        if ch.isdigit() or (ch == "." and i + 1 < len(text) and text[i + 1].isdigit()):
            found = DURATION_TEXT.match(text, i)
            if found and not (found.end() < len(text) and (text[found.end()].isalnum() or text[found.end()] in "_.")):
                tokens.append(Token("duration", found.group(), i, parse_duration(found.group())))
                i = found.end()
                continue
            found = NUMBER_TEXT.match(text, i)
            number = found.group()
            value = float(int(number, 16)) if number.lower().startswith("0x") else float(number)
            tokens.append(Token("number", number, i, value))
            i = found.end()
            continue
        found = IDENT_TEXT.match(text, i)
        if found:
            tokens.append(Token("ident", found.group(), i))
            i = found.end()
            continue
        for operator in OPERATORS:
            if text.startswith(operator, i):
                tokens.append(Token("op", operator, i))
                i += len(operator)
                break
        else:
            raise ParseError(f"parse error at char {i + 1}: unexpected character {ch!r}")
    tokens.append(Token("eof", "", len(text)))
    return tokens


@dataclass
class Matcher:
    name: str
    op: str
    value: str
    pattern: Any = None

    def __post_init__(self) -> None:
        if self.op in ("=~", "!~"):
            try:
                self.pattern = re.compile(self.value, re.DOTALL)
            except re.error as exc:
                raise ParseError(f'invalid regular expression in {self.name}{self.op}"{self.value}": {exc}')

    def matches(self, value: str) -> bool:
        if self.op == "=":
            return value == self.value
        if self.op == "!=":
            return value != self.value
        found = self.pattern.fullmatch(value) is not None
        return found if self.op == "=~" else not found

    def text(self) -> str:
        return f'{self.name}{self.op}"{escape_label_value(self.value)}"'


@dataclass
class NumberLiteral:
    value: float


@dataclass
class StringLiteral:
    value: str


@dataclass
class VectorSelector:
    name: Optional[str]
    matchers: List[Matcher]
    offset_ms: int = 0
    has_offset: bool = False
    at: Optional[Union[float, str]] = None


@dataclass
class MatrixSelector:
    selector: VectorSelector
    range_ms: int


@dataclass
class Call:
    func: str
    args: List[Any]


@dataclass
class Aggregate:
    op: str
    expr: Any
    param: Any
    grouping: List[str]
    without: bool
    grouped: bool


@dataclass
class Unary:
    op: str
    expr: Any


@dataclass
class VectorMatching:
    card: str
    on: bool
    labels: List[str]
    include: List[str]
    explicit: bool = False


@dataclass
class Binary:
    op: str
    lhs: Any
    rhs: Any
    return_bool: bool
    matching: VectorMatching


@dataclass
class Paren:
    expr: Any


def value_type(node: Any) -> str:
    if isinstance(node, NumberLiteral):
        return SCALAR
    if isinstance(node, StringLiteral):
        return STRING
    if isinstance(node, VectorSelector):
        return VECTOR
    if isinstance(node, MatrixSelector):
        return MATRIX
    if isinstance(node, (Paren, Unary)):
        return value_type(node.expr)
    if isinstance(node, Call):
        return FUNCTIONS[node.func][1]
    if isinstance(node, Aggregate):
        return VECTOR
    if isinstance(node, Binary):
        return SCALAR if value_type(node.lhs) == SCALAR and value_type(node.rhs) == SCALAR else VECTOR
    raise TypeError(f"not a PromQL node: {node!r}")


def render_modifiers(selector: VectorSelector) -> str:
    text = ""
    if selector.at is not None:
        text += f" @ {selector.at}()" if isinstance(selector.at, str) else f" @ {selector.at:.3f}"
    if selector.has_offset:
        text += f" offset {format_duration(selector.offset_ms)}"
    return text


def render_selector(selector: VectorSelector) -> str:
    shown = [m.text() for m in selector.matchers if not (m.name == NAME and m.op == "=" and m.value == selector.name)]
    body = "{" + ",".join(shown) + "}" if shown or not selector.name else ""
    return (selector.name or "") + body


def render(node: Any) -> str:
    if isinstance(node, NumberLiteral):
        return fmt_value(node.value)
    if isinstance(node, StringLiteral):
        return json.dumps(node.value)
    if isinstance(node, VectorSelector):
        return render_selector(node) + render_modifiers(node)
    if isinstance(node, MatrixSelector):
        return f"{render_selector(node.selector)}[{format_duration(node.range_ms)}]{render_modifiers(node.selector)}"
    if isinstance(node, Paren):
        return f"({render(node.expr)})"
    if isinstance(node, Unary):
        return f"-{render(node.expr)}"
    if isinstance(node, Call):
        return f"{node.func}({', '.join(render(arg) for arg in node.args)})"
    if isinstance(node, Aggregate):
        grouping = f" {'without' if node.without else 'by'} ({', '.join(node.grouping)})" if node.grouped else ""
        param = f"{render(node.param)}, " if node.param is not None else ""
        return f"{node.op}{grouping} ({param}{render(node.expr)})"
    if isinstance(node, Binary):
        modifier = " bool" if node.return_bool else ""
        matching = node.matching
        if matching.explicit:
            modifier += f" {'on' if matching.on else 'ignoring'} ({', '.join(matching.labels)})"
            if matching.card == "many-to-one":
                modifier += f" group_left ({', '.join(matching.include)})"
            elif matching.card == "one-to-many":
                modifier += f" group_right ({', '.join(matching.include)})"
        return f"{render(node.lhs)} {node.op}{modifier} {render(node.rhs)}"
    raise TypeError(f"not a PromQL node: {node!r}")


class Parser:
    def __init__(self, text: str) -> None:
        self.text = text
        self.tokens = tokenize(text)
        self.index = 0

    def peek(self, ahead: int = 0) -> Token:
        return self.tokens[min(self.index + ahead, len(self.tokens) - 1)]

    def next(self) -> Token:
        token = self.peek()
        self.index = min(self.index + 1, len(self.tokens) - 1)
        return token

    def is_op(self, text: str, ahead: int = 0) -> bool:
        token = self.peek(ahead)
        return token.kind == "op" and token.text == text

    def is_word(self, *words: str) -> bool:
        token = self.peek()
        return token.kind == "ident" and token.text.lower() in words

    def error(self, message: str, token: Optional[Token] = None) -> ParseError:
        where = (token or self.peek()).pos + 1
        return ParseError(f"parse error at char {where}: {message}")

    def describe(self, token: Token) -> str:
        if token.kind == "eof":
            return "end of input"
        return f"{token.kind} {token.text!r}"

    def expect(self, text: str, context: str) -> Token:
        token = self.next()
        if token.kind != "op" or token.text != text:
            raise self.error(f'unexpected {self.describe(token)} {context}, expected "{text}"', token)
        return token

    def parse(self) -> Any:
        node = self.parse_expr(0)
        if self.peek().kind != "eof":
            raise self.error(f"unexpected {self.describe(self.peek())}")
        return node

    def binary_operator(self) -> Optional[str]:
        token = self.peek()
        if token.kind == "op" and token.text in PRECEDENCE:
            return token.text
        if token.kind == "ident" and token.text.lower() in ("and", "or", "unless", "atan2"):
            return token.text.lower()
        return None

    def parse_expr(self, min_precedence: int) -> Any:
        left = self.parse_unary()
        while True:
            op = self.binary_operator()
            if op is None or PRECEDENCE[op] < min_precedence:
                return left
            token = self.next()
            return_bool, matching = self.parse_binary_modifiers(op)
            right = self.parse_expr(PRECEDENCE[op] if op == "^" else PRECEDENCE[op] + 1)
            left = self.make_binary(op, left, right, return_bool, matching, token)

    def parse_unary(self) -> Any:
        token = self.peek()
        if token.kind == "op" and token.text in ("-", "+"):
            self.next()
            operand = self.parse_expr(PRECEDENCE["^"])
            kind = value_type(operand)
            if kind not in (SCALAR, VECTOR):
                raise self.error(f"unary expression only allowed on expressions of type scalar or instant vector, got {kind}", token)
            if token.text == "+":
                return operand
            if isinstance(operand, NumberLiteral):
                return NumberLiteral(-operand.value)
            return Unary("-", operand)
        return self.parse_postfix(self.parse_primary())

    def parse_primary(self) -> Any:
        token = self.next()
        if token.kind == "number":
            return NumberLiteral(token.value)
        if token.kind == "string":
            return StringLiteral(token.value)
        if token.kind == "op" and token.text == "(":
            inner = self.parse_expr(0)
            self.expect(")", "in parenthesized expression")
            return Paren(inner)
        if token.kind == "op" and token.text == "{":
            return self.parse_selector(None, token)
        if token.kind == "ident":
            word = token.text
            lowered = word.lower()
            following = self.peek()
            opens = following.kind == "op" and following.text == "("
            if lowered in AGGREGATIONS + UNSUPPORTED_AGGREGATIONS and (opens or self.is_word("by", "without")):
                if lowered in UNSUPPORTED_AGGREGATIONS:
                    raise self.error(f'aggregation "{lowered}" is PromQL but not part of the miniql subset', token)
                return self.parse_aggregation(lowered, token)
            if opens:
                return self.parse_call(word, token)
            if lowered in ("inf", "nan"):
                return NumberLiteral(float(lowered))
            if lowered in KEYWORDS:
                raise self.error(f'unexpected keyword "{word}"', token)
            return self.parse_selector(word, token)
        raise self.error(f"unexpected {self.describe(token)}", token)

    def parse_selector(self, name: Optional[str], token: Token) -> VectorSelector:
        matchers: List[Matcher] = []
        if name is None:
            matchers = self.parse_matchers()
        elif self.is_op("{"):
            self.next()
            matchers = self.parse_matchers()
        names = [m for m in matchers if m.name == NAME]
        if name is not None:
            if names:
                raise self.error(f'metric name must not be set twice: "{name}" or "{names[0].value}"', token)
            matchers.insert(0, Matcher(NAME, "=", name))
        elif len(names) == 1 and names[0].op == "=":
            name = names[0].value
        if all(m.matches("") for m in matchers):
            raise self.error("vector selector must contain at least one non-empty matcher", token)
        return VectorSelector(name, matchers)

    def parse_matchers(self) -> List[Matcher]:
        matchers: List[Matcher] = []
        while True:
            if self.is_op("}"):
                self.next()
                return matchers
            token = self.next()
            if token.kind == "string" and (self.is_op(",") or self.is_op("}")):
                matchers.append(Matcher(NAME, "=", token.value))
            else:
                if token.kind == "ident" and LABEL_NAME.fullmatch(token.text):
                    label = token.text
                elif token.kind == "string":
                    label = token.value
                else:
                    raise self.error(f"unexpected {self.describe(token)} in label matching, expected label name", token)
                operator = self.next()
                if operator.kind != "op" or operator.text not in ("=", "!=", "=~", "!~"):
                    raise self.error(f"unexpected {self.describe(operator)} in label matching, expected one of =, !=, =~, !~", operator)
                value = self.next()
                if value.kind != "string":
                    raise self.error(f"unexpected {self.describe(value)} in label matching, expected string", value)
                matchers.append(Matcher(label, operator.text, value.value))
            if self.is_op(","):
                self.next()
            elif not self.is_op("}"):
                raise self.error(f"unexpected {self.describe(self.peek())} in label matching, expected \",\" or \"}}\"")

    def parse_label_list(self, context: str) -> List[str]:
        self.expect("(", context)
        names: List[str] = []
        while not self.is_op(")"):
            token = self.next()
            if token.kind == "ident" and LABEL_NAME.fullmatch(token.text):
                names.append(token.text)
            elif token.kind == "string":
                names.append(token.value)
            else:
                raise self.error(f"unexpected {self.describe(token)} in {context}, expected label", token)
            if not self.is_op(","):
                break
            self.next()
        self.expect(")", context)
        return names

    def parse_postfix(self, node: Any) -> Any:
        while True:
            token = self.peek()
            if token.kind == "op" and token.text == "[":
                self.next()
                duration = self.next()
                if duration.kind != "duration":
                    raise self.error(f"unexpected {self.describe(duration)} in range, expected a duration such as 5m", duration)
                after = self.peek()
                if after.text.startswith(":"):
                    raise self.error("subqueries (expr[range:step]) are PromQL but not part of the miniql subset", after)
                self.expect("]", "in range")
                if not isinstance(node, VectorSelector):
                    raise self.error("ranges only allowed for vector selectors", token)
                if node.has_offset or node.at is not None:
                    raise self.error("no offset or @ modifiers allowed before range", token)
                node = MatrixSelector(node, duration.value)
                continue
            if token.kind == "ident" and token.text.lower() == "offset":
                self.next()
                target = node if isinstance(node, VectorSelector) else node.selector if isinstance(node, MatrixSelector) else None
                if target is None:
                    raise self.error("offset modifier must be preceded by an instant vector selector or range vector selector or a subquery", token)
                if target.has_offset:
                    raise self.error("offset may not be set multiple times", token)
                sign = 1
                if self.is_op("-") or self.is_op("+"):
                    sign = -1 if self.next().text == "-" else 1
                duration = self.next()
                if duration.kind != "duration":
                    raise self.error(f"unexpected {self.describe(duration)} in offset, expected a duration", duration)
                target.offset_ms = sign * duration.value
                target.has_offset = True
                continue
            if token.kind == "op" and token.text == "@":
                self.next()
                target = node if isinstance(node, VectorSelector) else node.selector if isinstance(node, MatrixSelector) else None
                if target is None:
                    raise self.error("@ modifier must be preceded by an instant vector selector or range vector selector or a subquery", token)
                if target.at is not None:
                    raise self.error("@ <timestamp> may not be set multiple times", token)
                sign = 1.0
                if self.is_op("-") or self.is_op("+"):
                    sign = -1.0 if self.next().text == "-" else 1.0
                stamp = self.next()
                if stamp.kind == "number":
                    target.at = sign * stamp.value
                elif stamp.kind == "ident" and stamp.text in ("start", "end") and sign > 0:
                    self.expect("(", "in @ modifier")
                    self.expect(")", "in @ modifier")
                    target.at = stamp.text
                else:
                    raise self.error(f"unexpected {self.describe(stamp)} in @, expected timestamp, start() or end()", stamp)
                continue
            return node

    def parse_call(self, name: str, token: Token) -> Call:
        if name not in FUNCTIONS:
            if name in UNSUPPORTED_FUNCTIONS:
                raise self.error(f'function "{name}" is PromQL but not part of the miniql subset', token)
            raise self.error(f'unknown function with name "{name}"', token)
        self.expect("(", f'in call to "{name}"')
        args: List[Any] = []
        if not self.is_op(")"):
            while True:
                args.append(self.parse_expr(0))
                if not self.is_op(","):
                    break
                self.next()
        self.expect(")", f'in call to "{name}"')
        expected = FUNCTIONS[name][0]
        if len(args) != len(expected):
            raise self.error(f'expected {len(expected)} argument(s) in call to "{name}", got {len(args)}', token)
        for arg, kind in zip(args, expected):
            got = value_type(arg)
            if got != kind:
                raise self.error(f'expected type {kind} in call to function "{name}", got {got}', token)
        return Call(name, args)

    def parse_aggregation(self, op: str, token: Token) -> Aggregate:
        grouping: List[str] = []
        without = False
        grouped = False
        if self.is_word("by", "without"):
            without = self.next().text.lower() == "without"
            grouping = self.parse_label_list("grouping opts")
            grouped = True
        self.expect("(", f"in aggregation {op}")
        param = None
        if op in ("topk", "bottomk"):
            param = self.parse_expr(0)
            self.expect(",", f"in aggregation {op}")
        expr = self.parse_expr(0)
        self.expect(")", f"in aggregation {op}")
        if self.is_word("by", "without"):
            if grouped:
                raise self.error("grouping may only be given once", self.peek())
            without = self.next().text.lower() == "without"
            grouping = self.parse_label_list("grouping opts")
            grouped = True
        if value_type(expr) != VECTOR:
            raise self.error(f"expected type instant vector in aggregation expression, got {value_type(expr)}", token)
        if param is not None and value_type(param) != SCALAR:
            raise self.error(f"expected type scalar in aggregation parameter, got {value_type(param)}", token)
        return Aggregate(op, expr, param, grouping, without, grouped)

    def parse_binary_modifiers(self, op: str) -> Tuple[bool, Optional[VectorMatching]]:
        return_bool = False
        if self.is_word("bool"):
            token = self.next()
            if op not in COMPARISON:
                raise self.error("bool modifier can only be used on comparison operators", token)
            return_bool = True
        matching = None
        if self.is_word("on", "ignoring"):
            on = self.next().text.lower() == "on"
            matching = VectorMatching("one-to-one", on, self.parse_label_list("grouping opts"), [], True)
            if self.is_word("group_left", "group_right"):
                token = self.next()
                if op in SET_OPERATORS:
                    raise self.error(f'no grouping allowed for "{op}" operation', token)
                matching.card = "many-to-one" if token.text.lower() == "group_left" else "one-to-many"
                if self.is_op("("):
                    matching.include = self.parse_label_list("grouping opts")
                for name in matching.include:
                    if matching.on and name in matching.labels:
                        raise self.error(f'label "{name}" must not occur in ON and GROUP clause at once', token)
        elif self.is_word("group_left", "group_right"):
            raise self.error(f"unexpected {self.peek().text}: it needs on (...) or ignoring (...) first")
        return return_bool, matching

    def make_binary(self, op: str, lhs: Any, rhs: Any, return_bool: bool, matching: Optional[VectorMatching], token: Token) -> Binary:
        left, right = value_type(lhs), value_type(rhs)
        if left not in (SCALAR, VECTOR) or right not in (SCALAR, VECTOR):
            raise self.error("binary expression must contain only scalar and instant vector types", token)
        if op in COMPARISON and not return_bool and left == SCALAR and right == SCALAR:
            raise self.error("comparisons between scalars must use BOOL modifier", token)
        if op in SET_OPERATORS and SCALAR in (left, right):
            raise self.error(f'set operator "{op}" not allowed in binary scalar expression', token)
        if matching is not None and SCALAR in (left, right):
            raise self.error("vector matching only allowed between instant vectors", token)
        if matching is None:
            matching = VectorMatching("one-to-one", False, [], [])
        if op in SET_OPERATORS:
            matching.card = "many-to-many"
        return Binary(op, lhs, rhs, return_bool, matching)


def parse(text: str) -> Any:
    return Parser(text).parse()


class Series:
    __slots__ = ("labels", "times", "values", "stale")

    def __init__(self, labels: Labels) -> None:
        self.labels = labels
        self.times: List[int] = []
        self.values: List[float] = []
        self.stale: List[bool] = []

    def append(self, t: int, value: float, stale: bool = False, where: str = "") -> None:
        if self.times:
            last = self.times[-1]
            if t < last:
                raise InputError(f"{where}out of order sample for {format_labels(self.labels)}: {t} after {last}")
            if t == last:
                same = self.stale[-1] == stale and (self.values[-1] == value or (math.isnan(value) and math.isnan(self.values[-1])))
                if same:
                    return
                raise InputError(f"{where}duplicate sample for timestamp {t} with a different value in {format_labels(self.labels)}")
        self.times.append(t)
        self.values.append(value)
        self.stale.append(stale)

    def latest(self, ref: int, lookback_ms: int) -> Optional[Tuple[int, float]]:
        i = bisect.bisect_right(self.times, ref) - 1
        if i < 0 or self.times[i] <= ref - lookback_ms or self.stale[i]:
            return None
        return self.times[i], self.values[i]

    def window(self, after: int, until: int) -> List[Tuple[int, float]]:
        first = bisect.bisect_right(self.times, after)
        last = bisect.bisect_right(self.times, until)
        return [(self.times[i], self.values[i]) for i in range(first, last) if not self.stale[i]]


class Store:
    def __init__(self) -> None:
        self.series: Dict[Labels, Series] = {}
        self.cache: Dict[Tuple[Tuple[str, str, str], ...], List[Series]] = {}

    def add(self, labels: Labels, t: int, value: float, stale: bool = False, where: str = "") -> None:
        series = self.series.get(labels)
        if series is None:
            series = self.series[labels] = Series(labels)
            self.cache.clear()
        series.append(t, value, stale, where)

    def select(self, matchers: Sequence[Matcher]) -> List[Series]:
        key = tuple((m.name, m.op, m.value) for m in matchers)
        found = self.cache.get(key)
        if found is None:
            found = []
            for labels in sorted(self.series):
                values = dict(labels)
                if all(m.matches(values.get(m.name, "")) for m in matchers):
                    found.append(self.series[labels])
            self.cache[key] = found
        return found

    def bounds(self) -> Tuple[int, int]:
        times = [t for series in self.series.values() for t in (series.times[:1] + series.times[-1:])]
        if not times:
            raise InputError("the series file holds no samples")
        return min(times), max(times)

    def stats(self) -> Dict[str, Any]:
        metrics: Dict[str, int] = {}
        samples = 0
        for labels, series in self.series.items():
            name = dict(labels).get(NAME, "")
            metrics[name] = metrics.get(name, 0) + 1
            samples += sum(1 for flag in series.stale if not flag)
        low, high = self.bounds()
        return {"series": len(self.series), "samples": samples, "min_time": low / 1000, "max_time": high / 1000, "series_per_metric": dict(sorted(metrics.items()))}


def read_label_value(line: str, i: int, where: str) -> Tuple[str, int]:
    out = []
    while i < len(line):
        ch = line[i]
        if ch == '"':
            return "".join(out), i + 1
        if ch == "\\" and i + 1 < len(line):
            escape = line[i + 1]
            out.append({"n": "\n", "\\": "\\", '"': '"'}.get(escape, "\\" + escape))
            i += 2
            continue
        out.append(ch)
        i += 1
    raise InputError(f"{where}unterminated label value")


def parse_metric(text: str, where: str = "") -> Tuple[Dict[str, str], int]:
    labels: Dict[str, str] = {}
    i = 0
    found = METRIC_NAME.match(text)
    if found:
        labels[NAME] = found.group()
        i = found.end()
    if i < len(text) and text[i] == "{":
        i += 1
        while True:
            while i < len(text) and text[i] in " \t":
                i += 1
            if i < len(text) and text[i] == "}":
                i += 1
                break
            label = LABEL_NAME.match(text, i)
            if not label:
                raise InputError(f"{where}expected a label name or }} in {text!r}")
            i = label.end()
            while i < len(text) and text[i] in " \t":
                i += 1
            if text[i:i + 2] != '="':
                raise InputError(f"{where}label {label.group()}: expected =\"value\" in {text!r}")
            value, i = read_label_value(text, i + 2, where)
            labels[label.group()] = value
            while i < len(text) and text[i] in " \t":
                i += 1
            if i < len(text) and text[i] == ",":
                i += 1
            elif not (i < len(text) and text[i] == "}"):
                raise InputError(f"{where}expected , or }} after label {label.group()} in {text!r}")
    if NAME not in labels and i == 0:
        raise InputError(f"{where}expected a metric name or {{labels}}: {text!r}")
    return labels, i


def parse_sample_line(line: str, where: str) -> Tuple[Dict[str, str], float, Optional[float]]:
    labels, end = parse_metric(line, where)
    if NAME not in labels:
        raise InputError(f"{where}a sample needs a metric name: {line!r}")
    rest = line[end:]
    if " # " in rest:
        rest = rest.split(" # ", 1)[0]
    fields = rest.split()
    if len(fields) not in (1, 2):
        raise InputError(f"{where}expected a value and an optional timestamp: {line!r}")
    try:
        value = parse_go_float(fields[0])
    except ValueError:
        raise InputError(f"{where}not a sample value: {fields[0]!r}")
    if len(fields) == 1:
        return labels, value, None
    try:
        return labels, value, float(fields[1])
    except ValueError:
        raise InputError(f"{where}not a timestamp: {fields[1]!r}")


def normalize_float_labels(labels: Dict[str, str], types: Dict[str, str]) -> Dict[str, str]:
    name = labels.get(NAME, "")
    keys = []
    if name.endswith("_bucket") and types.get(name[: -len("_bucket")]) in ("histogram", "gaugehistogram"):
        keys.append("le")
    if types.get(name) == "summary":
        keys.append("quantile")
    for key in keys:
        if key in labels:
            try:
                labels[key] = openmetrics_float(parse_go_float(labels[key]))
            except ValueError:
                pass
    return labels


def load_exposition(text: str, source: str = "input", store: Optional[Store] = None) -> Store:
    lines = text.splitlines()
    openmetrics = any(line.strip() == "# EOF" for line in lines)
    types: Dict[str, str] = {}
    store = store if store is not None else Store()
    for number, raw in enumerate(lines, 1):
        line = raw.strip()
        if not line:
            continue
        where = f"{source}:{number}: "
        if line.startswith("#"):
            if line == "# EOF":
                break
            parts = line.split(None, 3)
            if len(parts) == 4 and parts[1] == "TYPE":
                types[parts[2]] = parts[3].strip()
            continue
        labels, value, stamp = parse_sample_line(line, where)
        if stamp is None:
            raise InputError(f"{where}sample without a timestamp: a series file is history, so every sample needs one")
        t = int(stamp * 1000) if openmetrics else int(stamp)
        store.add(as_labels(normalize_float_labels(labels, types)), t, value, where=where)
    return store


def load_series_file(path: str) -> Store:
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError as exc:
        raise InputError(f"cannot read {path}: {exc.strerror}")
    return load_exposition(text, path)


@dataclass
class Sample:
    labels: Labels
    value: float
    why: Tuple[str, ...] = ()


@dataclass
class Scalar:
    value: float
    why: Tuple[str, ...] = ()


@dataclass
class RangeSeries:
    labels: Labels
    points: List[Tuple[int, float]]
    after: int
    until: int


class Vector(list):
    pass


class Matrix(list):
    pass


@dataclass
class Extrapolation:
    increase: float
    resets: List[Tuple[int, float, float]]
    sampled: float
    average: float
    to_start: float
    to_end: float
    start_rule: str
    end_rule: str
    value: float


def extrapolate(points: Sequence[Tuple[int, float]], range_start: int, range_end: int, range_ms: int, is_counter: bool, is_rate: bool) -> Optional[Extrapolation]:
    if len(points) < 2:
        return None
    first_t, first_v = points[0]
    last_t, last_v = points[-1]
    result = last_v - first_v
    resets: List[Tuple[int, float, float]] = []
    if is_counter:
        previous = first_v
        for t, value in points[1:]:
            if value < previous:
                result += previous
                resets.append((t, previous, value))
            previous = value
    duration_to_start = (first_t - range_start) / 1000
    duration_to_end = (range_end - last_t) / 1000
    sampled = (last_t - first_t) / 1000
    average = sampled / (len(points) - 1)
    threshold = average * 1.1
    start_rule = end_rule = "edge"
    if duration_to_start >= threshold:
        duration_to_start = average / 2
        start_rule = "half"
    if is_counter and result > 0 and first_v >= 0:
        duration_to_zero = sampled * (first_v / result)
        if duration_to_zero < duration_to_start:
            duration_to_start = duration_to_zero
            start_rule = "zero"
    if duration_to_end >= threshold:
        duration_to_end = average / 2
        end_rule = "half"
    factor = (sampled + duration_to_start + duration_to_end) / sampled
    if is_rate:
        factor /= range_ms / 1000
    return Extrapolation(result, resets, sampled, average, duration_to_start, duration_to_end, start_rule, end_rule, result * factor)


def extrapolation_notes(e: Extrapolation, points: Sequence[Tuple[int, float]], range_start: int, range_end: int, range_ms: int, is_counter: bool, is_rate: bool) -> List[str]:
    change = "increase" if is_counter else "change"
    raw = points[-1][1] - points[0][1]
    if e.resets:
        described = ", ".join(f"at {relative(t - range_end)} {fmt_value(before)} -> {fmt_value(after)}" for t, before, after in e.resets)
        notes = [f"{change} last - first = {fmt_value(raw)}, plus {fmt_value(sum(r[1] for r in e.resets))} added back for {len(e.resets)} counter reset(s) ({described}) = {fmt_value(e.increase)}"]
    else:
        notes = [f"{change} last - first = {fmt_value(e.increase)}" + (", no counter resets" if is_counter else "")]
    gap_start = (points[0][0] - range_start) / 1000
    gap_end = (range_end - points[-1][0]) / 1000
    spacing = fmt_value(e.average)
    notes.append({
        "edge": f"first sample {fmt_value(gap_start)}s after the window start, under 1.1 x the {spacing}s spacing: extrapolate to the start",
        "half": f"first sample {fmt_value(gap_start)}s after the window start, the series starts inside it: extrapolate half a spacing, {fmt_value(e.to_start)}s",
        "zero": f"the counter would reach 0 only {fmt_value(e.to_start)}s before the first sample: extrapolate that far and no further",
    }[e.start_rule])
    notes.append({
        "edge": f"last sample {fmt_value(gap_end)}s before the window end: extrapolate to the end",
        "half": f"last sample {fmt_value(gap_end)}s before the window end, the series ends inside it: extrapolate half a spacing, {fmt_value(e.to_end)}s",
    }[e.end_rule])
    covered = e.sampled + e.to_start + e.to_end
    line = f"{fmt_value(e.increase)} x {fmt_value(covered)}s / {fmt_value(e.sampled)}s sampled = {fmt_value(e.increase * covered / e.sampled)}"
    if is_rate:
        line += f", per second over {format_duration(range_ms)}: {fmt_value(e.value)}"
    notes.append(line)
    return notes


def extrapolated_rate(points: Sequence[Tuple[int, float]], range_start: int, range_end: int, range_ms: int, is_counter: bool, is_rate: bool, notes: Optional[List[str]] = None) -> Optional[float]:
    e = extrapolate(points, range_start, range_end, range_ms, is_counter, is_rate)
    if e is None:
        if notes is not None:
            notes.append(f"{len(points)} sample in the window: two are needed, so this series has no result")
        return None
    if notes is not None:
        notes.extend(extrapolation_notes(e, points, range_start, range_end, range_ms, is_counter, is_rate))
    return e.value


def instant_rate(points: Sequence[Tuple[int, float]], is_rate: bool, notes: Optional[List[str]] = None) -> Optional[float]:
    if len(points) < 2:
        if notes is not None:
            notes.append(f"{len(points)} sample in the window: irate needs the last two")
        return None
    (previous_t, previous_v), (last_t, last_v) = points[-2], points[-1]
    if is_rate and last_v < previous_v:
        result = last_v
        reason = f"counter reset ({fmt_value(previous_v)} -> {fmt_value(last_v)}): the increase is the new value {fmt_value(last_v)}"
    else:
        result = last_v - previous_v
        reason = f"{fmt_value(last_v)} - {fmt_value(previous_v)} = {fmt_value(result)}"
    interval = last_t - previous_t
    if interval == 0:
        return None
    if is_rate:
        result /= interval / 1000
    if notes is not None:
        notes.append(f"last two samples {fmt_value(interval / 1000)}s apart: {reason}, / {fmt_value(interval / 1000)}s = {fmt_value(result)}")
    return result


def quantile(q: float, values: Sequence[float]) -> float:
    if not values or math.isnan(q):
        return math.nan
    if q < 0:
        return -math.inf
    if q > 1:
        return math.inf
    ordered = sorted(values, key=lambda v: (not math.isnan(v), v))
    n = float(len(ordered))
    rank = q * (n - 1)
    lower = max(0.0, math.floor(rank))
    upper = min(n - 1, lower + 1)
    weight = rank - math.floor(rank)
    return ordered[int(lower)] * (1 - weight) + ordered[int(upper)] * weight


def over_time(name: str, values: List[float], q: float = math.nan) -> float:
    if name in ("avg_over_time", "sum_over_time"):
        total, compensation = (values[0], 0.0) if name == "avg_over_time" else (0.0, 0.0)
        for value in (values[1:] if name == "avg_over_time" else values):
            total, compensation = kahan_sum_inc(value, total, compensation)
        if name == "sum_over_time":
            return total if math.isinf(total) else total + compensation
        count = float(len(values))
        return total / count + compensation / count
    if name == "count_over_time":
        return float(len(values))
    if name == "last_over_time":
        return values[-1]
    if name in ("min_over_time", "max_over_time"):
        best = values[0]
        for value in values:
            better = value < best if name == "min_over_time" else value > best
            if better or math.isnan(best):
                best = value
        return best
    return quantile(q, values)


def bucket_quantile(q: float, buckets: List[Tuple[float, float]], notes: Optional[List[str]] = None) -> float:
    if math.isnan(q):
        return math.nan
    if q < 0:
        return -math.inf
    if q > 1:
        return math.inf
    ordered = [list(b) for b in sorted(buckets, key=lambda b: b[0])]
    if not (math.isinf(ordered[-1][0]) and ordered[-1][0] > 0):
        if notes is not None:
            notes.append('no le="+Inf" bucket: the total count is unknown, so the result is NaN')
        return math.nan
    merged = [ordered[0]]
    for bucket in ordered[1:]:
        if bucket[0] == merged[-1][0]:
            merged[-1][1] += bucket[1]
        else:
            merged.append(bucket)
    previous = merged[0][1]
    for bucket in merged[1:]:
        current = bucket[1]
        if current == previous:
            continue
        if almost_equal(previous, current, SMALL_DELTA_TOLERANCE) or current < previous:
            bucket[1] = previous
            continue
        previous = current
    if len(merged) < 2:
        return math.nan
    observations = merged[-1][1]
    if observations == 0:
        if notes is not None:
            notes.append("zero observations in the window: NaN")
        return math.nan
    rank = q * observations
    index = len(merged) - 1
    for i in range(len(merged) - 1):
        if merged[i][1] >= rank:
            index = i
            break
    listing = ", ".join(f"le={openmetrics_float(b[0])}: {fmt_value(b[1])}" for b in merged)
    if notes is not None:
        notes.append(f"cumulative buckets {listing}")
        notes.append(f"rank = {fmt_value(q)} x {fmt_value(observations)} observations = {fmt_value(rank)}")
    if index == len(merged) - 1:
        if notes is not None:
            notes.append(f"the rank falls in the +Inf bucket: answer the highest finite bound, {fmt_value(merged[-2][0])}")
        return merged[-2][0]
    if index == 0 and merged[0][0] <= 0:
        return merged[0][0]
    start = 0.0
    end = merged[index][0]
    count = merged[index][1]
    if index > 0:
        start = merged[index - 1][0]
        count -= merged[index - 1][1]
        rank -= merged[index - 1][1]
    value = start + (end - start) * (rank / count)
    if notes is not None:
        notes.append(f"bucket ({fmt_value(start)}, {fmt_value(end)}] holds {fmt_value(count)}; linear inside it: {fmt_value(start)} + {fmt_value(end - start)} x {fmt_value(rank)}/{fmt_value(count)} = {fmt_value(value)}")
    return value


def go_div(a: float, b: float) -> float:
    if b == 0:
        if a == 0 or math.isnan(a):
            return math.nan
        return math.copysign(math.inf, a) * math.copysign(1.0, b)
    return a / b


def go_mod(a: float, b: float) -> float:
    if math.isinf(a) or math.isnan(a) or math.isnan(b) or b == 0:
        return math.nan
    return math.fmod(a, b)


def go_pow(a: float, b: float) -> float:
    try:
        return math.pow(a, b)
    except OverflowError:
        odd = b == int(b) and int(b) % 2 == 1
        return -math.inf if a < 0 and odd else math.inf
    except ValueError:
        if a == 0 and b < 0:
            odd = b == int(b) and int(b) % 2 == 1
            return math.copysign(math.inf, a) if odd else math.inf
        return math.nan


def arithmetic(op: str, a: float, b: float) -> float:
    if op == "+":
        return a + b
    if op == "-":
        return a - b
    if op == "*":
        return a * b
    if op == "/":
        return go_div(a, b)
    if op == "%":
        return go_mod(a, b)
    if op == "^":
        return go_pow(a, b)
    return math.atan2(a, b)


def compare_values(op: str, a: float, b: float) -> bool:
    return {"==": a == b, "!=": a != b, ">": a > b, "<": a < b, ">=": a >= b, "<=": a <= b}[op]


def binop(op: str, a: float, b: float) -> Tuple[float, bool]:
    if op in COMPARISON:
        return a, compare_values(op, a, b)
    return arithmetic(op, a, b), True


def signature(labels: Labels, on: bool, names: Sequence[str]) -> Labels:
    if on:
        return tuple(pair for pair in labels if pair[0] in names)
    return tuple(pair for pair in labels if pair[0] != NAME and pair[0] not in names)


def result_labels(many: Labels, one: Labels, op: str, matching: VectorMatching) -> Labels:
    labels = dict(many)
    if op in ARITHMETIC:
        labels.pop(NAME, None)
    if matching.card == "one-to-one":
        if matching.on:
            labels = {k: v for k, v in labels.items() if k in matching.labels}
        else:
            for name in matching.labels:
                labels.pop(name, None)
    other = dict(one)
    for name in matching.include:
        if other.get(name):
            labels[name] = other[name]
        else:
            labels.pop(name, None)
    return tuple(sorted(labels.items()))


def group_key(labels: Labels, grouping: Sequence[str], without: bool) -> Labels:
    if without:
        dropped = set(grouping) | {NAME}
        return tuple(pair for pair in labels if pair[0] not in dropped)
    kept = set(grouping)
    return tuple(pair for pair in labels if pair[0] in kept)


def check_unique(vector: Sequence[Sample]) -> None:
    seen: Set[Labels] = set()
    for sample in vector:
        if sample.labels in seen:
            raise EvalError(f"vector cannot contain metrics with the same labelset: {format_labels(sample.labels)}")
        seen.add(sample.labels)


class Evaluator:
    def __init__(self, store: Store, start_ms: int, end_ms: int, lookback_ms: int = DEFAULT_LOOKBACK_MS, explain: bool = False) -> None:
        self.store = store
        self.start_ms = start_ms
        self.end_ms = end_ms
        self.lookback_ms = lookback_ms
        self.explain = explain
        self.infos: List[str] = []
        self.dropped: List[str] = []

    def info(self, message: str) -> None:
        if message not in self.infos:
            self.infos.append(message)

    def drop(self, message: str) -> None:
        if self.explain and message not in self.dropped:
            self.dropped.append(message)

    def eval(self, node: Any, t: int) -> Any:
        if isinstance(node, NumberLiteral):
            return Scalar(node.value)
        if isinstance(node, StringLiteral):
            return node.value
        if isinstance(node, Paren):
            return self.eval(node.expr, t)
        if isinstance(node, VectorSelector):
            return self.eval_vector_selector(node, t)
        if isinstance(node, MatrixSelector):
            return self.eval_matrix(node, t)
        if isinstance(node, Call):
            return self.eval_call(node, t)
        if isinstance(node, Aggregate):
            return self.eval_aggregate(node, t)
        if isinstance(node, Unary):
            return self.eval_unary(node, t)
        if isinstance(node, Binary):
            return self.eval_binary(node, t)
        raise TypeError(f"not a PromQL node: {node!r}")

    def reference_time(self, selector: VectorSelector, t: int) -> int:
        if selector.at == "start":
            base = self.start_ms
        elif selector.at == "end":
            base = self.end_ms
        elif selector.at is not None:
            base = go_round(float(selector.at) * 1000)
        else:
            base = t
        return base - selector.offset_ms

    def eval_vector_selector(self, selector: VectorSelector, t: int) -> Vector:
        ref = self.reference_time(selector, t)
        out = Vector()
        for series in self.store.select(selector.matchers):
            found = series.latest(ref, self.lookback_ms)
            if found is None:
                self.drop(f"{format_labels(series.labels)}: no sample in the {format_duration(self.lookback_ms)} lookback ending at {relative(ref - t)}")
                continue
            stamp, value = found
            why = (f"{format_labels(series.labels)} = {fmt_value(value)}, the sample at {relative(stamp - t)}",) if self.explain else ()
            out.append(Sample(series.labels, value, why))
        return out

    def eval_matrix(self, node: MatrixSelector, t: int) -> Matrix:
        ref = self.reference_time(node.selector, t)
        after = ref - node.range_ms
        out = Matrix()
        for series in self.store.select(node.selector.matchers):
            points = series.window(after, ref)
            if points:
                out.append(RangeSeries(series.labels, points, after, ref))
            else:
                self.drop(f"{format_labels(series.labels)}: no sample in ({relative(after - t)}, {relative(ref - t)}]")
        return out

    def describe_points(self, series: RangeSeries, t: int) -> str:
        shown = [f"{relative(stamp - t)} {fmt_value(value)}" for stamp, value in series.points]
        if len(shown) > 8:
            shown = shown[:3] + ["..."] + shown[-3:]
        return f"{len(series.points)} sample(s) in ({relative(series.after - t)}, {relative(series.until - t)}]: " + ", ".join(shown)

    def eval_call(self, call: Call, t: int) -> Any:
        name = call.func
        if name == "time":
            return Scalar(t / 1000, (f"time() = {fmt_value(t / 1000)}",) if self.explain else ())
        if name == "vector":
            scalar = self.eval(call.args[0], t)
            return Vector([Sample((), scalar.value, scalar.why)])
        if name == "scalar":
            vector = self.eval(call.args[0], t)
            if len(vector) == 1:
                return Scalar(vector[0].value, vector[0].why)
            return Scalar(math.nan, (f"scalar() of {len(vector)} samples is NaN",) if self.explain else ())
        if name == "abs":
            vector = self.eval(call.args[0], t)
            out = Vector(Sample(drop_name(s.labels), abs(s.value), s.why) for s in vector)
            check_unique(out)
            return out
        if name == "histogram_quantile":
            q = self.eval(call.args[0], t).value
            return self.histogram_quantile(q, self.eval(call.args[1], t))
        q = math.nan
        if name == "quantile_over_time":
            q = self.eval(call.args[0], t).value
        node = call.args[-1]
        matrix = self.eval_matrix(node, t)
        out = Vector()
        for series in matrix:
            notes: Optional[List[str]] = [] if self.explain else None
            if name in RATE_FUNCTIONS:
                metric = dict(series.labels).get(NAME, "")
                if name != "delta" and metric and not metric.endswith(COUNTER_SUFFIXES):
                    self.info(f'PromQL info: metric might not be a counter, name does not end in _total/_sum/_count/_bucket: "{metric}"')
                if name == "irate":
                    value = instant_rate(series.points, True, notes)
                else:
                    value = extrapolated_rate(series.points, series.after, series.until, node.range_ms, name != "delta", name == "rate", notes)
            else:
                value = over_time(name, [v for _, v in series.points], q)
                if notes is not None:
                    notes.append(f"{name} of {len(series.points)} value(s) = {fmt_value(value)}")
            head = f"{name}({format_labels(series.labels)}[{format_duration(node.range_ms)}])"
            if value is None:
                self.drop(f"{head}: " + "; ".join(notes or []))
                continue
            labels = series.labels if name == "last_over_time" else drop_name(series.labels)
            why: Tuple[str, ...] = ()
            if self.explain:
                why = (f"{head} = {fmt_value(value)}",) + indent([self.describe_points(series, t)] + (notes or []))
            out.append(Sample(labels, value, why))
        check_unique(out)
        return out

    def histogram_quantile(self, q: float, vector: Vector) -> Vector:
        groups: Dict[Labels, Tuple[Labels, List[Tuple[float, float]], List[Sample]]] = {}
        for sample in vector:
            le = dict(sample.labels).get("le")
            try:
                upper = parse_go_float(le or "")
            except ValueError:
                self.info(f'PromQL warning: bucket label "le" is missing or has a malformed value of "{le or ""}" for metric name "{dict(sample.labels).get(NAME, "")}"')
                continue
            key = tuple(pair for pair in sample.labels if pair[0] != "le")
            if key not in groups:
                groups[key] = (tuple(pair for pair in key if pair[0] != NAME), [], [])
            groups[key][1].append((upper, sample.value))
            groups[key][2].append(sample)
        out = Vector()
        for labels, buckets, members in groups.values():
            notes: Optional[List[str]] = [] if self.explain else None
            value = bucket_quantile(q, buckets, notes)
            why: Tuple[str, ...] = ()
            if self.explain:
                why = (f"histogram_quantile({fmt_value(q)}) over {len(buckets)} bucket(s) = {fmt_value(value)}",) + indent(notes or [])
                why += indent([line for member in members for line in member.why], 1)
            out.append(Sample(labels, value, why))
        check_unique(out)
        return out

    def eval_aggregate(self, node: Aggregate, t: int) -> Vector:
        vector = self.eval(node.expr, t)
        groups: Dict[Labels, List[Sample]] = {}
        for sample in vector:
            groups.setdefault(group_key(sample.labels, node.grouping, node.without), []).append(sample)
        out = Vector()
        heading = node.op + (f" {'without' if node.without else 'by'} ({', '.join(node.grouping)})" if node.grouped else "")
        if node.op in ("topk", "bottomk"):
            param = self.eval(node.param, t).value
            if math.isnan(param) or abs(param) >= 2.0**63:
                raise EvalError(f"Scalar value {fmt_value(param)} overflows int64")
            k = int(param)
            if k < 1:
                return out
            for members in groups.values():
                ranked = sorted(members, key=lambda s: (math.isnan(s.value), -s.value if node.op == "topk" else s.value))
                for sample in ranked[:k]:
                    why = ((f"{node.op}({k}) of {len(members)} in the group keeps {format_labels(sample.labels)} = {fmt_value(sample.value)}",) + indent(sample.why)) if self.explain else ()
                    out.append(Sample(sample.labels, sample.value, why))
                for sample in ranked[k:]:
                    self.drop(f"{format_labels(sample.labels)} = {fmt_value(sample.value)}: not in the {node.op}({k}) of its group")
            return out
        for key, members in groups.items():
            values = [s.value for s in members]
            if node.op == "count":
                value = float(len(values))
            elif node.op in ("sum", "avg"):
                total, compensation = values[0], 0.0
                for v in values[1:]:
                    total, compensation = kahan_sum_inc(v, total, compensation)
                if node.op == "sum":
                    value = total if math.isinf(total) else total + compensation
                else:
                    value = total / len(values) + compensation / len(values)
            else:
                value = values[0]
                for v in values[1:]:
                    if (v > value if node.op == "max" else v < value) or math.isnan(value):
                        value = v
            why: Tuple[str, ...] = ()
            if self.explain:
                why = (f"{heading} over {len(members)} series = {fmt_value(value)}",) + indent([line for s in members for line in s.why])
            out.append(Sample(key, value, why))
        return out

    def eval_unary(self, node: Unary, t: int) -> Any:
        value = self.eval(node.expr, t)
        if isinstance(value, Scalar):
            return Scalar(-value.value, value.why)
        out = Vector(Sample(drop_name(s.labels), -s.value, s.why) for s in value)
        check_unique(out)
        return out

    def eval_binary(self, node: Binary, t: int) -> Any:
        lhs = self.eval(node.lhs, t)
        rhs = self.eval(node.rhs, t)
        if isinstance(lhs, Scalar) and isinstance(rhs, Scalar):
            value, keep = binop(node.op, lhs.value, rhs.value)
            if node.op in COMPARISON:
                value = 1.0 if keep else 0.0
            why = ((f"{fmt_value(lhs.value)} {node.op} {fmt_value(rhs.value)} = {fmt_value(value)}",) + indent(lhs.why + rhs.why)) if self.explain else ()
            return Scalar(value, why)
        if node.op in SET_OPERATORS:
            return self.set_operation(node, lhs, rhs)
        if isinstance(lhs, Scalar) or isinstance(rhs, Scalar):
            return self.vector_scalar(node, lhs, rhs)
        return self.vector_vector(node, lhs, rhs)

    def vector_scalar(self, node: Binary, lhs: Any, rhs: Any) -> Vector:
        swap = isinstance(lhs, Scalar)
        vector, scalar = (rhs, lhs) if swap else (lhs, rhs)
        out = Vector()
        for sample in vector:
            a, b = (scalar.value, sample.value) if swap else (sample.value, scalar.value)
            value, keep = binop(node.op, a, b)
            if node.op in COMPARISON and swap:
                value = sample.value
            if node.return_bool:
                value, keep = (1.0 if keep else 0.0), True
            if not keep:
                self.drop(f"{format_labels(sample.labels)}: {fmt_value(a)} {node.op} {fmt_value(b)} is false, filtered out")
                continue
            labels = drop_name(sample.labels) if node.op in ARITHMETIC or node.return_bool else sample.labels
            why: Tuple[str, ...] = ()
            if self.explain:
                why = (f"{fmt_value(a)} {node.op}{' bool' if node.return_bool else ''} {fmt_value(b)} -> {fmt_value(value)}",) + indent(sample.why + scalar.why)
            out.append(Sample(labels, value, why))
        check_unique(out)
        return out

    def vector_vector(self, node: Binary, lhs: Vector, rhs: Vector) -> Vector:
        matching = node.matching
        out = Vector()
        if not lhs or not rhs:
            return out
        many, one = (rhs, lhs) if matching.card == "one-to-many" else (lhs, rhs)
        one_side = "left" if matching.card == "one-to-many" else "right"
        by_signature: Dict[Labels, Sample] = {}
        for sample in one:
            key = signature(sample.labels, matching.on, matching.labels)
            if key in by_signature:
                raise EvalError(
                    f"found duplicate series for the match group {format_labels(key)} on the {one_side} hand-side of the operation: "
                    f"[{format_labels(sample.labels)}, {format_labels(by_signature[key].labels)}];"
                    "many-to-many matching not allowed: matching labels must be unique on one side"
                )
            by_signature[key] = sample
        matched: Dict[Labels, Set[Labels]] = {}
        for sample in many:
            key = signature(sample.labels, matching.on, matching.labels)
            partner = by_signature.get(key)
            if partner is None:
                self.drop(f"{format_labels(sample.labels)}: nothing on the other side matches {format_labels(key)}")
                continue
            left_value, right_value = sample.value, partner.value
            if matching.card == "one-to-many":
                left_value, right_value = right_value, left_value
            value, keep = binop(node.op, left_value, right_value)
            if node.return_bool:
                value = 1.0 if keep else 0.0
            elif not keep:
                self.drop(f"{format_labels(sample.labels)}: {fmt_value(left_value)} {node.op} {fmt_value(right_value)} is false, filtered out")
                continue
            labels = result_labels(sample.labels, partner.labels, node.op, matching)
            if node.return_bool:
                labels = drop_name(labels)
            if matching.card == "one-to-one":
                if key in matched:
                    raise EvalError("multiple matches for labels: many-to-one matching must be explicit (group_left/group_right)")
                matched[key] = set()
            else:
                seen = matched.setdefault(key, set())
                if labels in seen:
                    raise EvalError("multiple matches for labels: grouping labels must ensure unique matches")
                seen.add(labels)
            why: Tuple[str, ...] = ()
            if self.explain:
                left_why, right_why = (partner.why, sample.why) if matching.card == "one-to-many" else (sample.why, partner.why)
                why = (f"{fmt_value(left_value)} {node.op}{' bool' if node.return_bool else ''} {fmt_value(right_value)} -> {fmt_value(value)}, matched on {format_labels(key)}",) + indent(left_why + right_why)
            out.append(Sample(labels, value, why))
        check_unique(out)
        return out

    def set_operation(self, node: Binary, lhs: Vector, rhs: Vector) -> Vector:
        matching = node.matching
        def key(sample: Sample) -> Labels:
            return signature(sample.labels, matching.on, matching.labels)
        if node.op == "or":
            left_keys = {key(s) for s in lhs}
            return Vector(list(lhs) + [s for s in rhs if key(s) not in left_keys])
        right_keys = {key(s) for s in rhs}
        if node.op == "and":
            return Vector(s for s in lhs if key(s) in right_keys)
        return Vector(s for s in lhs if key(s) not in right_keys)


def instant_query(store: Store, expr: str, t_ms: int, lookback_ms: int = DEFAULT_LOOKBACK_MS, explain: bool = False) -> Tuple[Any, Evaluator]:
    node = parse(expr)
    evaluator = Evaluator(store, t_ms, t_ms, lookback_ms, explain)
    return evaluator.eval(node, t_ms), evaluator


def range_query(store: Store, expr: str, start_ms: int, end_ms: int, step_ms: int, lookback_ms: int = DEFAULT_LOOKBACK_MS) -> Tuple[Dict[Labels, List[Tuple[int, float]]], Evaluator]:
    node = parse(expr)
    kind = value_type(node)
    if kind not in (SCALAR, VECTOR):
        raise ParseError(f'invalid expression type "{kind}" for range query, must be Scalar or instant Vector')
    if end_ms < start_ms:
        raise InputError("end timestamp must not be before start time")
    if step_ms <= 0:
        raise InputError("zero or negative query resolution step widths are not accepted. Try a positive integer")
    if (end_ms - start_ms) // step_ms > MAX_POINTS:
        raise InputError("exceeded maximum resolution of 11,000 points per timeseries. Try decreasing the query resolution (?step=XX)")
    evaluator = Evaluator(store, start_ms, end_ms, lookback_ms)
    series: Dict[Labels, List[Tuple[int, float]]] = {}
    t = start_ms
    while t <= end_ms:
        value = evaluator.eval(node, t)
        for sample in ([Sample((), value.value)] if isinstance(value, Scalar) else value):
            series.setdefault(sample.labels, []).append((t, sample.value))
        t += step_ms
    return dict(sorted(series.items())), evaluator


def api_time(ms: int) -> Union[int, float]:
    return ms // 1000 if ms % 1000 == 0 else ms / 1000


def api_data(value: Any, t_ms: int) -> Dict[str, Any]:
    if isinstance(value, Scalar):
        return {"resultType": "scalar", "result": [api_time(t_ms), fmt_value(value.value)]}
    if isinstance(value, str):
        return {"resultType": "string", "result": [api_time(t_ms), value]}
    if isinstance(value, Matrix):
        return {"resultType": "matrix", "result": [{"metric": dict(s.labels), "values": [[api_time(p), fmt_value(v)] for p, v in s.points]} for s in value]}
    return {"resultType": "vector", "result": [{"metric": dict(s.labels), "value": [api_time(t_ms), fmt_value(s.value)]} for s in sorted(value, key=lambda s: s.labels)]}


def parse_time_param(text: str) -> int:
    text = text.strip()
    try:
        return go_round(float(text) * 1000)
    except ValueError:
        pass
    try:
        stamp = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        raise InputError(f"cannot parse {text!r} to a valid timestamp (unix seconds or RFC3339)")
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    return calendar.timegm(stamp.utctimetuple()) * 1000 + stamp.microsecond // 1000


def parse_step_param(text: str) -> int:
    try:
        return go_round(float(text) * 1000)
    except ValueError:
        return parse_duration(text)


def api_error(kind: str, message: str) -> Dict[str, Any]:
    return {"status": "error", "errorType": kind, "error": message}


def api_query(store: Store, path: str, params: Dict[str, str], lookback_ms: int = DEFAULT_LOOKBACK_MS) -> Tuple[int, Dict[str, Any]]:
    try:
        query = params.get("query")
        if not query:
            raise InputError("invalid parameter \"query\": missing")
        if path.rstrip("/").endswith("/query_range"):
            for name in ("start", "end", "step"):
                if not params.get(name):
                    raise InputError(f'invalid parameter "{name}": missing')
            start, end = parse_time_param(params["start"]), parse_time_param(params["end"])
            series, evaluator = range_query(store, query, start, end, parse_step_param(params["step"]), lookback_ms)
            body: Dict[str, Any] = {"status": "success", "data": {"resultType": "matrix", "result": [{"metric": dict(labels), "values": [[api_time(t), fmt_value(v)] for t, v in points]} for labels, points in series.items()]}}
        else:
            t = parse_time_param(params["time"]) if params.get("time") else store.bounds()[1]
            value, evaluator = instant_query(store, query, t, lookback_ms)
            body = {"status": "success", "data": api_data(value, t)}
        if evaluator.infos:
            body["infos"] = evaluator.infos
        return 200, body
    except (ParseError, InputError) as exc:
        return 400, api_error("bad_data", str(exc))
    except EvalError as exc:
        return 422, api_error("execution", str(exc))


def make_handler(store: Store, lookback_ms: int) -> type:
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args: Any) -> None:
            pass

        def send(self, status: int, body: Dict[str, Any]) -> None:
            data = json.dumps(body).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def answer(self, raw: str) -> None:
            path = urllib.parse.urlsplit(self.path).path
            if path in ("/-/ready", "/-/healthy"):
                data = b"miniql is Ready.\n"
                self.send_response(200)
                self.send_header("Content-Type", "text/plain")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
                return
            if path not in ("/api/v1/query", "/api/v1/query_range"):
                self.send(404, api_error("not_found", f"miniql serves /api/v1/query and /api/v1/query_range, not {path}"))
                return
            params = {key: values[-1] for key, values in urllib.parse.parse_qs(raw, keep_blank_values=True).items()}
            self.send(*api_query(store, path, params, lookback_ms))

        def do_GET(self) -> None:
            self.answer(urllib.parse.urlsplit(self.path).query)

        def do_POST(self) -> None:
            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(length).decode() if length else ""
            self.answer("&".join(part for part in (urllib.parse.urlsplit(self.path).query, body) if part))

    return Handler


def start_server(store: Store, host: str, port: int, lookback_ms: int = DEFAULT_LOOKBACK_MS) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer((host, port), make_handler(store, lookback_ms))
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def http_fetcher(base_url: str, timeout: float = 15.0) -> Callable[[str, Dict[str, str]], Dict[str, Any]]:
    def fetch(path: str, params: Dict[str, str]) -> Dict[str, Any]:
        url = base_url.rstrip("/") + path + "?" + urllib.parse.urlencode(params)
        try:
            with urllib.request.urlopen(url, timeout=timeout) as response:
                return json.loads(response.read().decode())
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode(errors="replace")
            try:
                return json.loads(raw)
            except ValueError:
                return api_error(f"http {exc.code}", raw[:300])
        except (urllib.error.URLError, OSError) as exc:
            raise InputError(f"cannot reach {base_url}: {getattr(exc, 'reason', exc)}")
    return fetch


def canonical_label(name: str, value: str) -> str:
    if name in ("le", "quantile"):
        try:
            return openmetrics_float(parse_go_float(value))
        except ValueError:
            return value
    return value


def series_key(metric: Dict[str, str]) -> Labels:
    return tuple(sorted((k, canonical_label(k, v)) for k, v in metric.items()))


def close_enough(a: str, b: str, tolerance: float) -> bool:
    x, y = parse_go_float(a), parse_go_float(b)
    if math.isnan(x) or math.isnan(y):
        return math.isnan(x) and math.isnan(y)
    if math.isinf(x) or math.isinf(y):
        return x == y
    return abs(x - y) <= max(tolerance * max(abs(x), abs(y)), 1e-12)


def same_response(mine: Dict[str, Any], theirs: Dict[str, Any], tolerance: float) -> Tuple[bool, str]:
    mine_failed, theirs_failed = mine.get("status") != "success", theirs.get("status") != "success"
    if mine_failed or theirs_failed:
        if mine_failed and theirs_failed:
            return True, f"both rejected it; miniql: {mine.get('error')} | prometheus: {theirs.get('error')}"
        failed, message = ("miniql", mine.get("error")) if mine_failed else ("prometheus", theirs.get("error"))
        return False, f"only {failed} rejected it: {message}"
    kind, other = mine["data"]["resultType"], theirs["data"]["resultType"]
    if kind != other:
        return False, f"result type {kind} in miniql, {other} in prometheus"
    if kind in ("scalar", "string"):
        a, b = mine["data"]["result"][1], theirs["data"]["result"][1]
        ok = a == b if kind == "string" else close_enough(a, b, tolerance)
        return ok, f"{a} vs {b}"
    field_name = "value" if kind == "vector" else "values"
    a = {series_key(r["metric"]): r[field_name] for r in mine["data"]["result"]}
    b = {series_key(r["metric"]): r[field_name] for r in theirs["data"]["result"]}
    if set(a) != set(b):
        only_mine = [format_labels(k) for k in sorted(set(a) - set(b))]
        only_theirs = [format_labels(k) for k in sorted(set(b) - set(a))]
        return False, f"series only in miniql: {only_mine}; only in prometheus: {only_theirs}"
    for key in sorted(a):
        mine_points = [a[key]] if kind == "vector" else a[key]
        their_points = [b[key]] if kind == "vector" else b[key]
        if [float(p[0]) for p in mine_points] != [float(p[0]) for p in their_points]:
            return False, f"{format_labels(key)}: timestamps {[p[0] for p in mine_points]} vs {[p[0] for p in their_points]}"
        for (stamp, x), (_, y) in zip(mine_points, their_points):
            if not close_enough(x, y, tolerance):
                return False, f"{format_labels(key)} at {stamp}: miniql {x}, prometheus {y}"
    points = sum(len(v) for v in a.values()) if kind == "matrix" else len(a)
    return True, f"{len(a)} series" + (f", {points} points" if kind == "matrix" else "")


def substitute_base(query: str, base: int) -> str:
    return BASE_PLACEHOLDER.sub(lambda m: str(base + int(m.group(1) or 0)), query)


def compare_queries(store: Store, spec: Dict[str, Any], fetch: Callable[[str, Dict[str, str]], Dict[str, Any]], base: Optional[int] = None, tolerance: float = 1e-9, lookback_ms: int = DEFAULT_LOOKBACK_MS) -> List[Dict[str, Any]]:
    base = int(spec.get("base", DEFAULT_START)) if base is None else base
    results = []
    for entry in spec.get("queries", []):
        query = substitute_base(entry["query"], base)
        if "at" in entry:
            path = "/api/v1/query"
            params = {"query": query, "time": str(base + entry["at"])}
            when = f"+{entry['at']}s"
        else:
            path = "/api/v1/query_range"
            params = {"query": query, "start": str(base + entry["start"]), "end": str(base + entry["end"]), "step": str(entry["step"])}
            when = f"+{entry['start']}s..+{entry['end']}s/{entry['step']}s"
        _, mine = api_query(store, path, params, lookback_ms)
        theirs = fetch(path, params)
        agree, detail = same_response(mine, theirs, tolerance)
        results.append({"name": entry.get("name", query), "query": query, "when": when, "agree": agree, "detail": detail, "miniql": mine, "prometheus": theirs})
    return results


def counter_values(steps: int) -> Dict[Labels, List[Optional[float]]]:
    def labels(instance: str, code: str) -> Labels:
        return as_labels({NAME: "cc_http_requests_total", "job": "api", "instance": instance, "code": code})
    series: Dict[Labels, List[Optional[float]]] = {}
    series[labels("a", "200")] = [1000.0 + 30 * i for i in range(steps)]
    series[labels("a", "500")] = [10.0 + 3 * i if i <= 50 else 160.0 + 9 * (i - 50) for i in range(steps)]
    series[labels("b", "200")] = [500.0 + 60 * i if i < 40 else 20.0 + 60 * (i - 40) for i in range(steps)]
    series[labels("b", "500")] = [5.0 + 6 * i if i < 40 else 6.0 * (i - 40) for i in range(steps)]
    series[labels("c", "200")] = [45.0 + 45 * (i - 50) if 50 <= i <= 60 else None for i in range(steps)]
    return series


HISTOGRAM_BOUNDS = (0.05, 0.1, 0.25, 0.5, 1.0, math.inf)
HISTOGRAM_MIDPOINTS = (0.03, 0.07, 0.17, 0.37, 0.72, 1.6)
LATENCY_FAST = (10, 6, 3, 1, 0, 0)
LATENCY_NORMAL = (2, 4, 10, 8, 4, 2)
LATENCY_SLOW = (0, 1, 4, 10, 10, 5)
QUEUE_PATTERN = (3, 5, 8, 13, 9, 4, 2, 7, 11, 6)
BUILD_INFO = {"a": ("1.4.2", "eu-west-1a"), "b": ("1.5.0", "eu-west-1b"), "c": ("1.5.0", "eu-west-1a")}


def latency_increments(instance: str, i: int) -> Sequence[int]:
    if instance == "a":
        return LATENCY_FAST
    return LATENCY_SLOW if 20 <= i < 30 else LATENCY_NORMAL


def generate_openmetrics(start: int = DEFAULT_START, minutes: int = 40, interval: int = 30) -> str:
    steps = minutes * 60 // interval + 1
    times = [start + interval * i for i in range(steps)]
    lines: List[str] = []

    def emit(labels: Labels, value: float, t: int) -> None:
        name = dict(labels)[NAME]
        rest = [f'{k}="{escape_label_value(v)}"' for k, v in labels if k != NAME]
        number = fmt_value(value) if value != int(value) or abs(value) >= 1e15 else str(int(value))
        lines.append(f"{name}{{{','.join(rest)}}} {number} {t}")

    lines += ["# HELP cc_build_info Version and zone of each instance.", "# TYPE cc_build_info gauge"]
    for instance, (version, zone) in BUILD_INFO.items():
        labels = as_labels({NAME: "cc_build_info", "instance": instance, "job": "api", "version": version, "zone": zone})
        for t in times:
            emit(labels, 1, t)
    lines += ["# HELP cc_http_requests Requests served, by status code.", "# TYPE cc_http_requests counter"]
    for labels, values in counter_values(steps).items():
        for t, value in zip(times, values):
            if value is not None:
                emit(labels, value, t)
    lines += ["# HELP cc_http_request_duration_seconds Request latency.", "# TYPE cc_http_request_duration_seconds histogram"]
    for instance in ("a", "b"):
        counts = [5 * n for n in latency_increments(instance, 0)]
        total = round(sum(c * m for c, m in zip(counts, HISTOGRAM_MIDPOINTS)), 6)
        for i, t in enumerate(times):
            if i > 0:
                increments = latency_increments(instance, i)
                if instance == "b" and i == 40:
                    counts, total = [0] * len(counts), 0.0
                counts = [c + n for c, n in zip(counts, increments)]
                total = round(total + sum(n * m for n, m in zip(increments, HISTOGRAM_MIDPOINTS)), 6)
            cumulative = 0
            base = {"job": "api", "instance": instance}
            for bound, count in zip(HISTOGRAM_BOUNDS, counts):
                cumulative += count
                emit(as_labels(dict(base, **{NAME: "cc_http_request_duration_seconds_bucket", "le": openmetrics_float(bound)})), cumulative, t)
            emit(as_labels(dict(base, **{NAME: "cc_http_request_duration_seconds_count"})), cumulative, t)
            emit(as_labels(dict(base, **{NAME: "cc_http_request_duration_seconds_sum"})), total, t)
    lines += ["# HELP cc_queue_depth Jobs waiting in the queue.", "# TYPE cc_queue_depth gauge"]
    for worker, values in (("w1", [QUEUE_PATTERN[i % len(QUEUE_PATTERN)] for i in range(steps)]), ("w2", [(i * 7) % 17 for i in range(steps)])):
        labels = as_labels({NAME: "cc_queue_depth", "job": "worker", "instance": worker})
        for t, value in zip(times, values):
            emit(labels, value, t)
    lines.append("# EOF")
    return "\n".join(lines) + "\n"


def expand_series_values(text: str) -> List[Optional[Union[float, str]]]:
    values: List[Optional[Union[float, str]]] = []
    for token in text.split():
        if token == "_":
            values.append(None)
            continue
        if token == STALE:
            values.append(STALE)
            continue
        missing = MISSING_RUN.fullmatch(token)
        if missing:
            values.extend([None] * int(missing.group(1)))
            continue
        expanding = EXPANDING.fullmatch(token)
        if expanding:
            value = float(expanding.group(1))
            step = float(expanding.group(3)) * (-1 if expanding.group(2) == "-" else 1)
            values.append(value)
            for _ in range(int(expanding.group(4))):
                value += step
                values.append(value)
            continue
        repeating = REPEATING.fullmatch(token)
        if repeating:
            values.extend([float(repeating.group(1))] * (int(repeating.group(2)) + 1))
            continue
        try:
            values.append(float(token))
        except ValueError:
            raise InputError(f"cannot expand series value {token!r} (examples: 0+60x10, 1 2 _ 4, _x3, stale)")
    return values


@dataclass
class RecordingRule:
    record: str
    expr: str
    node: Any
    labels: Dict[str, str]


@dataclass
class RuleGroup:
    name: str
    rules: List[RecordingRule]
    skipped_alerts: List[str] = field(default_factory=list)


def load_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise InputError(f"cannot read {path}: {exc.strerror}")
    except ValueError as exc:
        raise InputError(f"{path}: miniql reads the JSON form of rule and test files (valid YAML too, so promtool reads the same file): {exc}")


def load_rule_groups(path: Path) -> List[RuleGroup]:
    groups = []
    for group in load_json(path).get("groups", []):
        rules = []
        skipped = []
        for rule in group.get("rules", []):
            if "record" in rule:
                if not METRIC_NAME.fullmatch(rule["record"]):
                    raise InputError(f"{path}: group {group.get('name')}: invalid recording rule name {rule['record']!r}")
                try:
                    node = parse(rule["expr"])
                except ParseError as exc:
                    raise InputError(f"{path}: rule {rule['record']}: {exc}")
                if value_type(node) not in (SCALAR, VECTOR):
                    raise InputError(f"{path}: rule {rule['record']}: invalid expression type {value_type(node)}, a rule needs a scalar or an instant vector")
                rules.append(RecordingRule(rule["record"], rule["expr"], node, dict(rule.get("labels") or {})))
            elif "alert" in rule:
                skipped.append(rule["alert"])
        groups.append(RuleGroup(group.get("name", ""), rules, skipped))
    return groups


def evaluate_rules(store: Store, groups: Sequence[RuleGroup], start_ms: int, end_ms: int, interval_ms: int) -> List[str]:
    errors: List[str] = []
    previous: Dict[Tuple[int, int], Set[Labels]] = {}
    t = start_ms
    while t <= end_ms:
        for group_index, group in enumerate(groups):
            for rule_index, rule in enumerate(group.rules):
                produced: Set[Labels] = set()
                try:
                    value = Evaluator(store, t, t).eval(rule.node, t)
                    samples = [Sample((), value.value)] if isinstance(value, Scalar) else value
                    for sample in samples:
                        labels = dict(sample.labels)
                        labels[NAME] = rule.record
                        labels.update(rule.labels)
                        key = as_labels(labels)
                        if key in produced:
                            raise EvalError("vector contains metrics with the same labelset after applying rule labels")
                        produced.add(key)
                        store.add(key, t, sample.value)
                except EvalError as exc:
                    errors.append(f"rule: {rule.record}, time: {format_duration(t)}, err: {exc}")
                for key in previous.get((group_index, rule_index), set()) - produced:
                    store.add(key, t, math.nan, stale=True)
                previous[(group_index, rule_index)] = produced
        t += interval_ms
    return errors


def run_rule_tests(test_path: Path) -> Dict[str, Any]:
    spec = load_json(test_path)
    evaluation_interval = parse_duration(str(spec.get("evaluation_interval", "1m")))
    groups: List[RuleGroup] = []
    for name in spec.get("rule_files", []):
        groups.extend(load_rule_groups(test_path.parent / name))
    report: Dict[str, Any] = {"file": str(test_path), "cases": [], "errors": [], "skipped_alerts": [a for g in groups for a in g.skipped_alerts]}
    for test_index, test in enumerate(spec.get("tests", [])):
        interval = parse_duration(str(test.get("interval", spec.get("evaluation_interval", "1m"))))
        store = Store()
        for entry in test.get("input_series", []):
            labels, _ = parse_metric(entry["series"], "input_series: ")
            for i, value in enumerate(expand_series_values(str(entry.get("values", "")))):
                if value is None:
                    continue
                if value == STALE:
                    store.add(as_labels(labels), i * interval, math.nan, stale=True)
                else:
                    store.add(as_labels(labels), i * interval, float(value))
        cases = test.get("promql_expr_test", [])
        last = max([parse_duration(str(c.get("eval_time", "0s"))) for c in cases] + [0])
        report["errors"].extend(evaluate_rules(store, groups, 0, last, evaluation_interval))
        for case in cases:
            t = parse_duration(str(case.get("eval_time", "0s")))
            expected = sorted((as_labels(parse_metric(s.get("labels") or "{}", "exp_samples: ")[0]), float(s["value"])) for s in case.get("exp_samples") or [])
            outcome: Dict[str, Any] = {"test": test_index, "expr": case["expr"], "eval_time": format_duration(t), "expected": [[format_labels(l), fmt_value(v)] for l, v in expected]}
            try:
                value, _ = instant_query(store, case["expr"], t)
                if isinstance(value, Scalar):
                    got = [((), value.value)]
                elif isinstance(value, Vector):
                    got = sorted((s.labels, s.value) for s in value)
                else:
                    raise EvalError(f"expected an instant vector or a scalar, got {value_type(parse(case['expr']))}")
                outcome["got"] = [[format_labels(l), fmt_value(v)] for l, v in got]
                outcome["ok"] = got == expected
            except (ParseError, EvalError) as exc:
                outcome["got"] = []
                outcome["error"] = str(exc)
                outcome["ok"] = False
            report["cases"].append(outcome)
    report["ok"] = not report["errors"] and all(c["ok"] for c in report["cases"])
    return report


def print_rule_report(report: Dict[str, Any]) -> None:
    print(f"Unit Testing:  {report['file']}")
    for case in report["cases"]:
        status = "ok    " if case["ok"] else "FAILED"
        print(f"  {status} {case['expr']} at {case['eval_time']}")
        if not case["ok"]:
            print(f"           exp: {', '.join(' '.join(pair) for pair in case['expected']) or '(nothing)'}")
            print(f"           got: {case.get('error') or ', '.join(' '.join(pair) for pair in case['got']) or '(nothing)'}")
    for error in report["errors"]:
        print(f"    {error}")
    for alert in report["skipped_alerts"]:
        print(f"  (alerting rule {alert} skipped: the alerting lesson evaluates those)")
    print("  SUCCESS" if report["ok"] else "  FAILED:")


def cli_time(text: Optional[str], store: Store) -> int:
    if not text:
        return store.bounds()[1]
    if text.startswith("+"):
        return store.bounds()[0] + parse_duration(text[1:])
    return parse_time_param(text)


def print_vector(value: Any, t: int, explain: bool) -> None:
    if isinstance(value, Scalar):
        print(f"scalar {fmt_value(value.value)}")
        for line in indent(value.why) if explain else ():
            print(line)
        return
    if isinstance(value, str):
        print(json.dumps(value))
        return
    if isinstance(value, Matrix):
        for series in value:
            print(format_labels(series.labels))
            for stamp, number in series.points:
                print(f"  {iso(stamp)} ({relative(stamp - t)}) {fmt_value(number)}")
        return
    if not value:
        print("(empty vector)")
    for sample in sorted(value, key=lambda s: s.labels):
        print(f"{format_labels(sample.labels)} {fmt_value(sample.value)}" if not explain else f"{format_labels(sample.labels)} => {fmt_value(sample.value)}")
        for line in indent(sample.why) if explain else ():
            print(line)


def command_query(args: argparse.Namespace) -> int:
    store = load_series_file(args.series)
    t = cli_time(args.at, store)
    lookback = parse_duration(args.lookback)
    value, evaluator = instant_query(store, args.expr, t, lookback, explain=args.explain)
    if args.json:
        body: Dict[str, Any] = {"status": "success", "data": api_data(value, t)}
        if evaluator.infos:
            body["infos"] = evaluator.infos
        if args.explain:
            body["explain"] = [{"metric": dict(s.labels), "why": list(s.why)} for s in value] if isinstance(value, Vector) else list(getattr(value, "why", ()))
            body["dropped"] = evaluator.dropped
        print(json.dumps(body, indent=2))
        return 0
    if args.explain:
        print(f"{args.expr} at {iso(t)}")
    print_vector(value, t, args.explain)
    if args.explain and evaluator.dropped:
        print("dropped:")
        for line in evaluator.dropped:
            print(f"  {line}")
    for line in evaluator.infos:
        print(line, file=sys.stderr)
    return 0


def command_range(args: argparse.Namespace) -> int:
    store = load_series_file(args.series)
    start, end = cli_time(args.start, store), cli_time(args.end, store)
    series, evaluator = range_query(store, args.expr, start, end, parse_step_param(args.step), parse_duration(args.lookback))
    if args.json:
        print(json.dumps({"status": "success", "data": {"resultType": "matrix", "result": [{"metric": dict(k), "values": [[api_time(t), fmt_value(v)] for t, v in points]} for k, points in series.items()]}}, indent=2))
        return 0
    for labels, points in series.items():
        print(format_labels(labels))
        for t, value in points:
            print(f"  {iso(t)} {fmt_value(value)}")
    if not series:
        print("(no series)")
    for line in evaluator.infos:
        print(line, file=sys.stderr)
    return 0


def command_serve(args: argparse.Namespace) -> int:
    store = load_series_file(args.series)
    host, _, port = args.listen.rpartition(":")
    server = ThreadingHTTPServer((host or "127.0.0.1", int(port)), make_handler(store, parse_duration(args.lookback)))
    print(f"miniql: {len(store.series)} series from {args.series}, query API on http://{args.listen}/api/v1/query", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


def command_compare(args: argparse.Namespace) -> int:
    store = load_series_file(args.series)
    spec = load_json(Path(args.queries))
    results = compare_queries(store, spec, http_fetcher(args.url), args.base, args.tolerance, parse_duration(args.lookback))
    agreed = sum(1 for r in results if r["agree"])
    if args.json:
        print(json.dumps({"total": len(results), "agree": agreed, "results": results}, indent=2))
    else:
        width = max([len(r["name"]) for r in results] + [10])
        for r in results:
            print(f"{'agree' if r['agree'] else 'DIFF '}  {r['name']:<{width}}  {r['when']:<22} {r['detail']}")
        print(f"{agreed} of {len(results)} queries agree")
    return 0 if agreed == len(results) else 1


def command_generate(args: argparse.Namespace) -> int:
    text = generate_openmetrics(args.start, args.minutes, args.interval)
    if args.output:
        Path(args.output).write_text(text, encoding="utf-8")
    else:
        sys.stdout.write(text)
    return 0


def command_test(args: argparse.Namespace) -> int:
    reports = [run_rule_tests(Path(path)) for path in args.files]
    if args.json:
        print(json.dumps(reports if len(reports) > 1 else reports[0], indent=2))
    else:
        for report in reports:
            print_rule_report(report)
    return 0 if all(r["ok"] for r in reports) else 1


def command_stats(args: argparse.Namespace) -> int:
    stats = load_series_file(args.series).stats()
    if args.json:
        print(json.dumps(stats, indent=2))
        return 0
    print(f"{stats['series']} series, {stats['samples']} samples, {iso(int(stats['min_time'] * 1000))} .. {iso(int(stats['max_time'] * 1000))}")
    for name, count in stats["series_per_metric"].items():
        print(f"  {count:>4}  {name}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="miniql", description="A PromQL subset evaluator over OpenMetrics series, with Prometheus' query API.")
    sub = parser.add_subparsers(dest="command", required=True)
    query = sub.add_parser("query", help="evaluate an instant query")
    query.add_argument("expr")
    query.add_argument("--series", required=True, help="OpenMetrics (seconds, ends with # EOF) or text format (milliseconds) with timestamps")
    query.add_argument("--at", help="unix seconds, RFC3339, or +DURATION after the first sample (default: the last sample)")
    query.add_argument("--lookback", default="5m")
    query.add_argument("--explain", action="store_true", help="show the samples and arithmetic behind every value")
    query.add_argument("--json", action="store_true")
    ranged = sub.add_parser("range", help="evaluate a range query")
    ranged.add_argument("expr")
    ranged.add_argument("--series", required=True)
    ranged.add_argument("--start", required=True)
    ranged.add_argument("--end", required=True)
    ranged.add_argument("--step", required=True, help="seconds or a duration such as 1m")
    ranged.add_argument("--lookback", default="5m")
    ranged.add_argument("--json", action="store_true")
    serve = sub.add_parser("serve", help="serve /api/v1/query and /api/v1/query_range")
    serve.add_argument("--series", required=True)
    serve.add_argument("--listen", default="127.0.0.1:9095")
    serve.add_argument("--lookback", default="5m")
    compare = sub.add_parser("compare", help="run a table of queries through miniql and a Prometheus API and compare the answers")
    compare.add_argument("--series", required=True)
    compare.add_argument("--queries", required=True)
    compare.add_argument("--url", required=True)
    compare.add_argument("--base", type=int, help="unix seconds that the table's times are relative to (default: the file's base)")
    compare.add_argument("--tolerance", type=float, default=1e-9)
    compare.add_argument("--lookback", default="5m")
    compare.add_argument("--json", action="store_true")
    generate = sub.add_parser("generate", help="write the lesson's deterministic OpenMetrics data")
    generate.add_argument("--start", type=int, default=DEFAULT_START)
    generate.add_argument("--minutes", type=int, default=40)
    generate.add_argument("--interval", type=int, default=30)
    generate.add_argument("-o", "--output")
    test = sub.add_parser("test", help="run promtool-style rule unit tests (promql_expr_test) from JSON files")
    test.add_argument("files", nargs="+")
    test.add_argument("--json", action="store_true")
    stats = sub.add_parser("stats", help="count series and samples in a series file")
    stats.add_argument("--series", required=True)
    stats.add_argument("--json", action="store_true")
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(sys.argv[1:] if argv is None else argv)
    commands = {"query": command_query, "range": command_range, "serve": command_serve, "compare": command_compare, "generate": command_generate, "test": command_test, "stats": command_stats}
    try:
        return commands[args.command](args)
    except (ParseError, InputError) as exc:
        print(f"miniql: {exc}", file=sys.stderr)
        return 2
    except EvalError as exc:
        print(f"miniql: execution error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
