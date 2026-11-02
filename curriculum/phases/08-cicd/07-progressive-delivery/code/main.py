"""minirollout: a progressive delivery controller in the shape of Argo Rollouts. It reads a Rollout and AnalysisTemplates (JSON), moves traffic to the new version step by step (whole pods through kubectl, or exact weights through its own proxy), measures the new version with PromQL against a Prometheus-style query API, then promotes it or aborts. The same file runs the demo app, the weighted proxy, a load generator that serves the query API, and a simulator for how many requests a bad version reaches before rollback."""
from __future__ import annotations

import argparse
import bisect
import copy
import dataclasses
import hashlib
import json
import math
import operator
import os
import re
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple

DEFAULT_BUCKETS = (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0)
REQUESTS = "http_requests_total"
LATENCY = "http_request_duration_seconds"
LOOKBACK = 300.0
ERROR_RETRY = 10.0
CONSECUTIVE_ERROR_LIMIT = 4
SUCCESSFUL, RUNNING, PENDING, INCONCLUSIVE, ERROR, FAILED = "Successful", "Running", "Pending", "Inconclusive", "Error", "Failed"
BEST_TO_WORST = (SUCCESSFUL, RUNNING, PENDING, INCONCLUSIVE, ERROR, FAILED)
COMPLETED = (SUCCESSFUL, INCONCLUSIVE, ERROR, FAILED)
HEALTHY, PROGRESSING, PAUSED, DEGRADED = "Healthy", "Progressing", "Paused", "Degraded"
DONE, WAIT, ABORTED = "done", "wait", "aborted"
EXIT_PAUSED = 3
TEMPLATE_KINDS = ("AnalysisTemplate", "ClusterAnalysisTemplate")
REVISION_ANNOTATION = "minirollout/revision"
UNITS = {"ms": 0.001, "s": 1.0, "m": 60.0, "h": 3600.0, "d": 86400.0, "w": 604800.0, "y": 31536000.0}
DURATION_PART = re.compile(r"(\d+(?:\.\d+)?)(ms|s|m|h|d|w|y)")
HOP_BY_HOP = {"connection", "keep-alive", "proxy-connection", "transfer-encoding", "te", "trailer", "upgrade", "host", "content-length"}
AGGREGATIONS = ("sum", "avg", "min", "max", "count")
FUNCTIONS = ("rate", "increase", "histogram_quantile", "vector")
ARG_PATTERN = re.compile(r"\{\{\s*args\.([A-Za-z0-9_.-]+)\s*\}\}")
METRIC_NAME = re.compile(r"[A-Za-z_:][A-Za-z0-9_:]*")
LABEL_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
ESCAPES = {"\\": "\\", '"': '"', "'": "'", "n": "\n"}
PROMQL_TOKEN = re.compile(
    r"""
    (?P<space>\s+)
  | (?P<duration>(?:\d+(?:ms|s|m|h|d|w|y))+)(?![\w:])
  | (?P<number>(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?)
  | (?P<ident>[A-Za-z_:][\w:]*)
  | (?P<string>"(?:[^"\\]|\\.)*"|'(?:[^'\\]|\\.)*')
  | (?P<op>=~|!~|!=|==|[{}()\[\],=+\-*/])
""",
    re.X,
)
CONDITION_TOKEN = re.compile(r"\s*(?:(?P<number>(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?)|(?P<name>[A-Za-z_]\w*)|(?P<op>&&|\|\||<=|>=|==|!=|[<>!()\[\],+\-*/]))")
COMPARE = {"<": operator.lt, "<=": operator.le, ">": operator.gt, ">=": operator.ge, "==": operator.eq, "!=": operator.ne}
NO_PROXY = urllib.request.build_opener(urllib.request.ProxyHandler({}))

Labels = Tuple[Tuple[str, str], ...]
Sample = Tuple[str, Dict[str, str], float]
Vector = List[Tuple[Dict[str, str], float]]
Runner = Callable[[Sequence[str], Optional[str]], Tuple[int, str, str]]


class InputError(ValueError):
    pass


class QueryError(ValueError):
    pass


class ConditionError(ValueError):
    pass


class MeasurementError(RuntimeError):
    pass


class TargetError(RuntimeError):
    pass


def parse_duration(value: Any) -> float:
    if isinstance(value, bool):
        raise InputError(f"not a duration: {value!r}")
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value or "").strip()
    if re.fullmatch(r"\d+(?:\.\d+)?", text):
        return float(text)
    total, position = 0.0, 0
    for match in DURATION_PART.finditer(text):
        if match.start() != position:
            break
        total += float(match.group(1)) * UNITS[match.group(2)]
        position = match.end()
    if not text or position != len(text):
        raise InputError(f"not a duration: {value!r} (examples: 30, 30s, 1m30s, 500ms)")
    return total


def format_number(value: float) -> str:
    if math.isnan(value):
        return "NaN"
    if math.isinf(value):
        return "+Inf" if value > 0 else "-Inf"
    if value == int(value) and abs(value) < 1e15:
        return str(int(value))
    return format(Decimal(repr(value)), "f")


def format_seconds(seconds: Optional[float]) -> str:
    if seconds is None:
        return "-"
    if seconds >= 60 and seconds % 60 == 0:
        return f"{int(seconds // 60)}m"
    return f"{format_number(seconds)}s"


def show_value(value: Any) -> str:
    if value is None:
        return "-"
    if isinstance(value, list):
        return "[" + ",".join(show_value(v) for v in value) + "]"
    if isinstance(value, float):
        return format_number(round(value, 4))
    return str(value)


def jsonable(value: Any) -> Any:
    if isinstance(value, list):
        return [jsonable(v) for v in value]
    if isinstance(value, float) and not math.isfinite(value):
        return format_number(value)
    return value


def freeze(labels: Dict[str, str]) -> Labels:
    return tuple(sorted(labels.items()))


def escape_label(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def render_labels(labels: Dict[str, str]) -> str:
    if not labels:
        return ""
    names = sorted(k for k in labels if k != "le") + (["le"] if "le" in labels else [])
    return "{" + ",".join(f'{k}="{escape_label(labels[k])}"' for k in names) + "}"


@dataclass
class Histogram:
    counts: List[float]
    total: float = 0.0


class Registry:
    def __init__(self, buckets: Sequence[float] = DEFAULT_BUCKETS):
        self.buckets = tuple(buckets)
        self.lock = threading.Lock()
        self.requests: Dict[Labels, float] = {}
        self.latency: Dict[Labels, Histogram] = {}

    def observe(self, labels: Dict[str, str], code: Any, seconds: float, times: int = 1) -> None:
        if times <= 0:
            return
        with self.lock:
            key = freeze(dict(labels, code=str(code)))
            self.requests[key] = self.requests.get(key, 0.0) + times
            histogram = self.latency.setdefault(freeze(labels), Histogram([0.0] * (len(self.buckets) + 1)))
            histogram.counts[bisect.bisect_left(self.buckets, seconds)] += times
            histogram.total += seconds * times

    def samples(self) -> List[Sample]:
        with self.lock:
            out: List[Sample] = [(REQUESTS, dict(key), value) for key, value in sorted(self.requests.items())]
            for key, histogram in sorted(self.latency.items(), key=lambda item: item[0]):
                running = 0.0
                for bound, count in zip(self.buckets + (math.inf,), histogram.counts):
                    running += count
                    out.append((LATENCY + "_bucket", dict(key, le=format_number(bound)), running))
                out.append((LATENCY + "_sum", dict(key), histogram.total))
                out.append((LATENCY + "_count", dict(key), running))
            return out

    def render(self) -> str:
        families = {REQUESTS: ("counter", "HTTP requests by status code."), LATENCY: ("histogram", "HTTP request latency in seconds.")}
        lines: List[str] = []
        current = ""
        for name, labels, value in self.samples():
            family = LATENCY if name.startswith(LATENCY) else name
            if family != current:
                kind, text = families[family]
                lines += [f"# HELP {family} {text}", f"# TYPE {family} {kind}"]
                current = family
            lines.append(f"{name}{render_labels(labels)} {format_number(value)}")
        return "\n".join(lines) + "\n"


def read_quoted(text: str, position: int) -> Tuple[str, int]:
    quote = text[position]
    out: List[str] = []
    position += 1
    while position < len(text):
        char = text[position]
        if char == "\\" and position + 1 < len(text):
            out.append(ESCAPES.get(text[position + 1], "\\" + text[position + 1]))
            position += 2
        elif char == quote:
            return "".join(out), position + 1
        else:
            out.append(char)
            position += 1
    raise InputError("unterminated string")


def parse_label_set(line: str, position: int) -> Tuple[Dict[str, str], int]:
    labels: Dict[str, str] = {}
    while True:
        while position < len(line) and line[position] in " ,":
            position += 1
        if position < len(line) and line[position] == "}":
            return labels, position + 1
        match = LABEL_NAME.match(line, position)
        if not match:
            raise InputError("expected a label name or }")
        name, position = match.group(0), match.end()
        while position < len(line) and line[position] == " ":
            position += 1
        if line[position:position + 1] != "=":
            raise InputError(f"expected = after label {name}")
        position += 1
        while position < len(line) and line[position] == " ":
            position += 1
        if line[position:position + 1] != '"':
            raise InputError(f"label {name}: the value must be in double quotes")
        labels[name], position = read_quoted(line, position)


def parse_sample_line(line: str) -> Sample:
    match = METRIC_NAME.match(line)
    if not match:
        raise InputError("expected a metric name")
    name, position = match.group(0), match.end()
    labels: Dict[str, str] = {}
    if line[position:position + 1] == "{":
        labels, position = parse_label_set(line, position + 1)
    parts = line[position:].split()
    if len(parts) not in (1, 2):
        raise InputError("expected a value and an optional timestamp")
    try:
        value = float(parts[0])
    except ValueError:
        raise InputError(f"not a sample value: {parts[0]!r}")
    if len(parts) == 2 and not re.fullmatch(r"-?\d+", parts[1]):
        raise InputError(f"not a timestamp in milliseconds: {parts[1]!r}")
    return name, labels, value


def parse_exposition(text: str) -> List[Sample]:
    samples: List[Sample] = []
    for number, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        try:
            samples.append(parse_sample_line(line))
        except InputError as exc:
            raise InputError(f"line {number}: {exc}: {raw!r}")
    return samples


class TSDB:
    def __init__(self, retention: float = 900.0):
        self.retention = retention
        self.lock = threading.Lock()
        self.series: Dict[Tuple[str, Labels], List[Tuple[float, float]]] = {}

    def append(self, at: float, samples: Iterable[Sample]) -> None:
        with self.lock:
            for name, labels, value in samples:
                points = self.series.setdefault((name, freeze(labels)), [])
                points.append((at, value))
                while points and points[0][0] < at - self.retention:
                    points.pop(0)

    def select(self, name: str, matchers: Sequence["Matcher"], start: float, end: float) -> List[Tuple[Dict[str, str], List[Tuple[float, float]]]]:
        found = []
        with self.lock:
            for (series_name, labels), points in sorted(self.series.items()):
                if series_name != name:
                    continue
                as_dict = dict(labels)
                if not all(m.matches(as_dict.get(m.label, "")) for m in matchers):
                    continue
                window = [p for p in points if start < p[0] <= end]
                if window:
                    found.append((as_dict, window))
        return found


@dataclass
class Matcher:
    label: str
    op: str
    value: str
    pattern: Any = None

    def __post_init__(self) -> None:
        if self.op in ("=~", "!~"):
            try:
                self.pattern = re.compile(self.value)
            except re.error as exc:
                raise QueryError(f"invalid regular expression {self.value!r}: {exc}")

    def matches(self, actual: str) -> bool:
        if self.op == "=":
            return actual == self.value
        if self.op == "!=":
            return actual != self.value
        found = self.pattern.fullmatch(actual) is not None
        return found if self.op == "=~" else not found


@dataclass
class NumberLiteral:
    value: float


@dataclass
class Selector:
    name: str
    matchers: List[Matcher]
    window: Optional[float] = None


@dataclass
class Call:
    function: str
    args: List[Any]


@dataclass
class Aggregate:
    op: str
    grouping: Optional[List[str]]
    expr: Any


@dataclass
class Binary:
    op: str
    left: Any
    right: Any


def tokenize_query(text: str) -> List[Tuple[str, str]]:
    tokens: List[Tuple[str, str]] = []
    position = 0
    while position < len(text):
        match = PROMQL_TOKEN.match(text, position)
        if not match:
            raise QueryError(f"unexpected character {text[position]!r} at position {position} of {text!r}")
        position = match.end()
        if match.lastgroup != "space":
            tokens.append((match.lastgroup or "", match.group()))
    tokens.append(("end", ""))
    return tokens


class QueryParser:
    def __init__(self, text: str):
        self.text = text
        self.tokens = tokenize_query(text)
        self.index = 0

    def peek(self, offset: int = 0) -> Tuple[str, str]:
        return self.tokens[min(self.index + offset, len(self.tokens) - 1)]

    def at(self, kind: str, value: str) -> bool:
        return self.peek() == (kind, value)

    def take(self, kind: str, value: Optional[str] = None) -> str:
        found_kind, found = self.peek()
        if found_kind != kind or (value is not None and found != value):
            wanted = repr(value) if value is not None else kind
            where = f"found {found!r}" if found else "reached the end"
            raise QueryError(f"expected {wanted} but {where} in {self.text!r}")
        self.index += 1
        return found

    def parse(self) -> Any:
        node = self.union()
        self.take("end")
        return node

    def union(self) -> Any:
        node = self.additive()
        while self.at("ident", "or"):
            self.index += 1
            node = Binary("or", node, self.additive())
        return node

    def additive(self) -> Any:
        node = self.multiplicative()
        while self.peek() in (("op", "+"), ("op", "-")):
            node = Binary(self.take("op"), node, self.multiplicative())
        return node

    def multiplicative(self) -> Any:
        node = self.unary()
        while self.peek() in (("op", "*"), ("op", "/")):
            node = Binary(self.take("op"), node, self.unary())
        return node

    def unary(self) -> Any:
        if self.at("op", "-"):
            self.index += 1
            return Binary("-", NumberLiteral(0.0), self.unary())
        return self.primary()

    def primary(self) -> Any:
        kind, value = self.peek()
        if kind == "number":
            self.index += 1
            return NumberLiteral(float(value))
        if (kind, value) == ("op", "("):
            self.index += 1
            node = self.union()
            self.take("op", ")")
            return node
        if kind == "ident" and value in AGGREGATIONS:
            return self.aggregate()
        if kind == "ident" and value in FUNCTIONS and self.peek(1) == ("op", "("):
            return self.call()
        if kind == "ident":
            return self.selector()
        raise QueryError(f"unexpected {value!r} in {self.text!r}" if value else f"unexpected end of {self.text!r}")

    def grouping(self) -> Optional[List[str]]:
        if self.at("ident", "without"):
            raise QueryError("without (...) is not in this PromQL subset; use by (...)")
        if not self.at("ident", "by"):
            return None
        self.index += 1
        self.take("op", "(")
        labels: List[str] = []
        while not self.at("op", ")"):
            labels.append(self.take("ident"))
            if self.at("op", ","):
                self.index += 1
        self.take("op", ")")
        return labels

    def aggregate(self) -> Aggregate:
        op = self.take("ident")
        grouping = self.grouping()
        self.take("op", "(")
        node = self.union()
        self.take("op", ")")
        if grouping is None:
            grouping = self.grouping()
        return Aggregate(op, grouping, node)

    def call(self) -> Call:
        name = self.take("ident")
        self.take("op", "(")
        args: List[Any] = []
        if not self.at("op", ")"):
            args.append(self.union())
            while self.at("op", ","):
                self.index += 1
                args.append(self.union())
        self.take("op", ")")
        if name in ("rate", "increase") and (len(args) != 1 or not isinstance(args[0], Selector) or args[0].window is None):
            raise QueryError(f"{name}() takes one range vector such as http_requests_total[1m]")
        if name == "histogram_quantile" and len(args) != 2:
            raise QueryError("histogram_quantile() takes a quantile and a vector of _bucket series")
        if name == "vector" and len(args) != 1:
            raise QueryError("vector() takes one number")
        return Call(name, args)

    def selector(self) -> Selector:
        name = self.take("ident")
        matchers: List[Matcher] = []
        if self.at("op", "{"):
            self.index += 1
            while not self.at("op", "}"):
                label = self.take("ident")
                op = self.take("op")
                if op not in ("=", "!=", "=~", "!~"):
                    raise QueryError(f"unknown label matcher {op!r} after {label}")
                matchers.append(Matcher(label, op, read_quoted(self.take("string"), 0)[0]))
                if self.at("op", ","):
                    self.index += 1
            self.take("op", "}")
        window = None
        if self.at("op", "["):
            self.index += 1
            window = parse_duration(self.take("duration"))
            self.take("op", "]")
        return Selector(name, matchers, window)


def parse_query(text: str) -> Any:
    return QueryParser(text).parse()


def counter_increase(points: Sequence[Tuple[float, float]]) -> float:
    total = 0.0
    for (_, before), (_, after) in zip(points, points[1:]):
        total += after - before if after >= before else after
    return total


def extrapolated_increase(points: Sequence[Tuple[float, float]], range_start: float, range_end: float) -> Optional[float]:
    if len(points) < 2:
        return None
    sampled = points[-1][0] - points[0][0]
    if sampled <= 0:
        return None
    delta = counter_increase(points)
    average = sampled / (len(points) - 1)
    threshold = average * 1.1
    to_start, to_end = points[0][0] - range_start, range_end - points[-1][0]
    if to_start >= threshold:
        to_start = average / 2
    if delta > 0 and points[0][1] >= 0:
        to_start = min(to_start, sampled * points[0][1] / delta)
    if to_end >= threshold:
        to_end = average / 2
    return delta * (sampled + to_start + to_end) / sampled


def arithmetic(op: str, a: float, b: float) -> float:
    if op == "+":
        return a + b
    if op == "-":
        return a - b
    if op == "*":
        return a * b
    if b == 0:
        return math.nan if a == 0 or math.isnan(a) else math.copysign(math.inf, a)
    return a / b


def as_vector(value: Any, where: str) -> Vector:
    if isinstance(value, list):
        return value
    raise QueryError(f"{where} expects an instant vector, got the number {format_number(value)}")


def as_scalar(value: Any, where: str) -> float:
    if isinstance(value, float):
        return value
    raise QueryError(f"{where} expects a number, got a vector")


def bucket_quantile(q: float, buckets: List[Tuple[float, float]]) -> float:
    if math.isnan(q):
        return math.nan
    if q < 0:
        return -math.inf
    if q > 1:
        return math.inf
    buckets = sorted(buckets)
    if len(buckets) < 2 or not math.isinf(buckets[-1][0]):
        return math.nan
    counts: List[float] = []
    running = 0.0
    for _, count in buckets:
        running = max(running, count)
        counts.append(running)
    total = counts[-1]
    if total == 0:
        return math.nan
    rank = q * total
    index = next((i for i in range(len(buckets) - 1) if counts[i] >= rank), len(buckets) - 1)
    if index == len(buckets) - 1:
        return buckets[-2][0]
    if index == 0 and buckets[0][0] <= 0:
        return buckets[0][0]
    start = buckets[index - 1][0] if index > 0 else 0.0
    below = counts[index - 1] if index > 0 else 0.0
    in_bucket = counts[index] - below
    if in_bucket == 0:
        return buckets[index][0]
    return start + (buckets[index][0] - start) * (rank - below) / in_bucket


def histogram_quantile(q: float, vector: Vector) -> Vector:
    groups: Dict[Labels, List[Tuple[float, float]]] = {}
    for labels, value in vector:
        try:
            bound = float(labels.get("le", ""))
        except ValueError:
            continue
        rest = freeze({k: v for k, v in labels.items() if k != "le"})
        groups.setdefault(rest, []).append((bound, value))
    return [(dict(key), bucket_quantile(q, buckets)) for key, buckets in sorted(groups.items())]


def aggregate(op: str, grouping: Optional[List[str]], vector: Vector) -> Vector:
    groups: Dict[Labels, List[float]] = {}
    for labels, value in vector:
        groups.setdefault(freeze({k: labels[k] for k in grouping or [] if k in labels}), []).append(value)
    reducers: Dict[str, Callable[[List[float]], float]] = {
        "sum": lambda v: float(sum(v)),
        "count": lambda v: float(len(v)),
        "min": min,
        "max": max,
        "avg": lambda v: sum(v) / len(v),
    }
    return [(dict(key), float(reducers[op](values))) for key, values in sorted(groups.items())]


def binary(op: str, left: Any, right: Any) -> Any:
    if op == "or":
        if not isinstance(left, list) or not isinstance(right, list):
            raise QueryError("or needs a vector on both sides, as in sum(...) or vector(0)")
        seen = {freeze(labels) for labels, _ in left}
        return left + [(labels, value) for labels, value in right if freeze(labels) not in seen]
    if not isinstance(left, list) and not isinstance(right, list):
        return arithmetic(op, left, right)
    if not isinstance(left, list):
        return [(labels, arithmetic(op, left, value)) for labels, value in right]
    if not isinstance(right, list):
        return [(labels, arithmetic(op, value, right)) for labels, value in left]
    by_labels = {freeze(labels): value for labels, value in right}
    return [(labels, arithmetic(op, value, by_labels[freeze(labels)])) for labels, value in left if freeze(labels) in by_labels]


def evaluate_query(node: Any, db: TSDB, at: float) -> Any:
    if isinstance(node, NumberLiteral):
        return node.value
    if isinstance(node, Selector):
        if node.window is not None:
            raise QueryError(f"{node.name}[{format_seconds(node.window)}] is a range vector; wrap it in rate() or increase()")
        return [(labels, points[-1][1]) for labels, points in db.select(node.name, node.matchers, at - LOOKBACK, at)]
    if isinstance(node, Aggregate):
        return aggregate(node.op, node.grouping, as_vector(evaluate_query(node.expr, db, at), f"{node.op}()"))
    if isinstance(node, Binary):
        return binary(node.op, evaluate_query(node.left, db, at), evaluate_query(node.right, db, at))
    if node.function in ("rate", "increase"):
        selector = node.args[0]
        out: Vector = []
        for labels, points in db.select(selector.name, selector.matchers, at - selector.window, at):
            value = extrapolated_increase(points, at - selector.window, at)
            if value is not None:
                out.append((labels, value / selector.window if node.function == "rate" else value))
        return out
    if node.function == "vector":
        return [({}, as_scalar(evaluate_query(node.args[0], db, at), "vector()"))]
    q = as_scalar(evaluate_query(node.args[0], db, at), "histogram_quantile()")
    return histogram_quantile(q, as_vector(evaluate_query(node.args[1], db, at), "histogram_quantile()"))


def query_response(db: TSDB, expression: str, at: float) -> Tuple[int, Dict[str, Any]]:
    try:
        value = evaluate_query(parse_query(expression), db, at)
    except (QueryError, InputError) as exc:
        return 400, {"status": "error", "errorType": "bad_data", "error": str(exc)}
    stamp = round(at, 3)
    if isinstance(value, float):
        return 200, {"status": "success", "data": {"resultType": "scalar", "result": [stamp, format_number(value)]}}
    result = [{"metric": labels, "value": [stamp, format_number(v)]} for labels, v in value]
    return 200, {"status": "success", "data": {"resultType": "vector", "result": result}}


def result_values(data: Dict[str, Any]) -> Any:
    kind = data.get("resultType")
    if kind == "scalar":
        return float(data["result"][1])
    if kind == "vector":
        return [float(item["value"][1]) for item in data.get("result") or []]
    if kind == "matrix":
        return [float(v[1]) for item in data.get("result") or [] for v in item.get("values") or []]
    raise MeasurementError(f"Prometheus metric type not supported: {kind}")


def prometheus_request(address: str, query: str, timeout: float = 10.0) -> Dict[str, Any]:
    if not address:
        raise MeasurementError("no Prometheus address: set provider.prometheus.address or pass --prometheus-url")
    url = address.rstrip("/") + "/api/v1/query"
    body = urllib.parse.urlencode({"query": query}).encode()
    request = urllib.request.Request(url, data=body, headers={"Content-Type": "application/x-www-form-urlencoded"})
    try:
        with NO_PROXY.open(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode())
    except urllib.error.HTTPError as exc:
        text = exc.read().decode(errors="replace")
        try:
            message = json.loads(text).get("error") or text
        except ValueError:
            message = text
        raise MeasurementError(f"query {url}: HTTP {exc.code}: {message.strip()[:300]}")
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise MeasurementError(f"query {url}: {exc}")
    if payload.get("status") != "success":
        raise MeasurementError(f"query {url}: {payload.get('error') or payload}")
    return payload.get("data") or {}


def prometheus_query(address: str, query: str, timeout: float = 10.0) -> Any:
    return result_values(prometheus_request(address, query, timeout))


def local_query(db: TSDB, query: str, at: float) -> Any:
    code, payload = query_response(db, query, at)
    if code != 200:
        raise MeasurementError(payload["error"])
    return result_values(payload["data"])


def number(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        hint = "; a Prometheus vector is a list, so compare result[0]" if isinstance(value, list) else ""
        raise ConditionError(f"expected a number, got {type(value).__name__} {show_value(value)}{hint}")
    return float(value)


def truth(value: Any) -> bool:
    if not isinstance(value, bool):
        raise ConditionError(f"expected true or false, got {show_value(value)}")
    return value


CONDITION_FUNCTIONS: Dict[str, Callable[[Any], Any]] = {
    "isNaN": lambda v: math.isnan(number(v)),
    "isInf": lambda v: math.isinf(number(v)),
    "asFloat": lambda v: float(v if isinstance(v, str) else number(v)),
    "asInt": lambda v: int(v if isinstance(v, str) else number(v)),
    "len": len,
}


class Condition:
    def __init__(self, text: str):
        self.text = text
        self.tokens = self.tokenize(text)
        self.index = 0
        self.program = self.disjunction()
        if self.peek()[0] != "end":
            raise ConditionError(f"unexpected {self.peek()[1]!r} in condition {text!r}")

    @staticmethod
    def tokenize(text: str) -> List[Tuple[str, str]]:
        tokens: List[Tuple[str, str]] = []
        position = 0
        while text[position:].strip():
            match = CONDITION_TOKEN.match(text, position)
            if not match or match.end() == position:
                raise ConditionError(f"unexpected character in condition {text!r} at position {position}")
            position = match.end()
            tokens.append((match.lastgroup or "", match.group(match.lastgroup or 0)))
        tokens.append(("end", ""))
        return tokens

    def peek(self) -> Tuple[str, str]:
        return self.tokens[self.index]

    def take(self) -> Tuple[str, str]:
        token = self.tokens[self.index]
        if token[0] != "end":
            self.index += 1
        return token

    def expect(self, value: str) -> None:
        if self.take() != ("op", value):
            raise ConditionError(f"expected {value!r} in condition {self.text!r}")

    def evaluate(self, result: Any) -> bool:
        try:
            value = self.program(result)
        except (TypeError, ValueError, OverflowError) as exc:
            if isinstance(exc, ConditionError):
                raise
            raise ConditionError(str(exc))
        if not isinstance(value, bool):
            raise ConditionError(f"expected bool, but got {type(value).__name__}")
        return value

    def disjunction(self) -> Callable[[Any], Any]:
        parts = [self.conjunction()]
        while self.peek() in (("op", "||"), ("name", "or")):
            self.take()
            parts.append(self.conjunction())
        return parts[0] if len(parts) == 1 else (lambda r: any(truth(p(r)) for p in parts))

    def conjunction(self) -> Callable[[Any], Any]:
        parts = [self.negation()]
        while self.peek() in (("op", "&&"), ("name", "and")):
            self.take()
            parts.append(self.negation())
        return parts[0] if len(parts) == 1 else (lambda r: all(truth(p(r)) for p in parts))

    def negation(self) -> Callable[[Any], Any]:
        if self.peek() in (("op", "!"), ("name", "not")):
            self.take()
            inner = self.negation()
            return lambda r: not truth(inner(r))
        return self.comparison()

    def comparison(self) -> Callable[[Any], Any]:
        left = self.sum()
        kind, op = self.peek()
        if kind == "op" and op in COMPARE:
            self.take()
            right, compare = self.sum(), COMPARE[op]
            return lambda r: compare(number(left(r)), number(right(r)))
        return left

    def sum(self) -> Callable[[Any], Any]:
        node = self.product()
        while self.peek() in (("op", "+"), ("op", "-")):
            node = self.arithmetic_node(self.take()[1], node, self.product())
        return node

    def product(self) -> Callable[[Any], Any]:
        node = self.unary()
        while self.peek() in (("op", "*"), ("op", "/")):
            node = self.arithmetic_node(self.take()[1], node, self.unary())
        return node

    @staticmethod
    def arithmetic_node(op: str, left: Callable[[Any], Any], right: Callable[[Any], Any]) -> Callable[[Any], Any]:
        return lambda r: arithmetic(op, number(left(r)), number(right(r)))

    def unary(self) -> Callable[[Any], Any]:
        if self.peek() == ("op", "-"):
            self.take()
            inner = self.unary()
            return lambda r: -number(inner(r))
        return self.postfix()

    def postfix(self) -> Callable[[Any], Any]:
        node = self.atom()
        while self.peek() == ("op", "["):
            self.take()
            position = self.disjunction()
            self.expect("]")
            node = self.index_node(node, position)
        return node

    @staticmethod
    def index_node(target: Callable[[Any], Any], position: Callable[[Any], Any]) -> Callable[[Any], Any]:
        def run(result: Any) -> Any:
            sequence, index = target(result), int(number(position(result)))
            if not isinstance(sequence, list):
                raise ConditionError(f"cannot index {type(sequence).__name__} {show_value(sequence)}")
            if not 0 <= index < len(sequence):
                raise ConditionError(f"index out of range: {index} (array length {len(sequence)})")
            return sequence[index]

        return run

    def atom(self) -> Callable[[Any], Any]:
        kind, value = self.take()
        if kind == "number":
            constant = float(value)
            return lambda r: constant
        if kind == "name" and value == "result":
            return lambda r: r
        if kind == "name" and value in ("true", "false"):
            flag = value == "true"
            return lambda r: flag
        if kind == "name" and value in CONDITION_FUNCTIONS and self.peek() == ("op", "("):
            self.take()
            argument, function = self.disjunction(), CONDITION_FUNCTIONS[value]
            self.expect(")")
            return lambda r: function(argument(r))
        if (kind, value) == ("op", "("):
            node = self.disjunction()
            self.expect(")")
            return node
        if kind == "name":
            raise ConditionError(f"unknown name {value!r} in condition {self.text!r} (known: result, true, false, {', '.join(CONDITION_FUNCTIONS)})")
        raise ConditionError(f"unexpected {value or 'end'!r} in condition {self.text!r}")


@dataclass
class MetricSpec:
    name: str
    query: str
    address: str
    interval: float
    count: Optional[int]
    initial_delay: float
    success: Optional[Condition]
    failure: Optional[Condition]
    failure_limit: int = 0
    inconclusive_limit: int = 0
    consecutive_error_limit: int = CONSECUTIVE_ERROR_LIMIT

    def describe(self) -> str:
        cadence = f"every {format_seconds(self.interval)}" if self.interval else "once"
        if self.count is None:
            cadence += " until the rollout ends"
        elif self.count > 1:
            cadence += f" x{self.count}"
        delay = f" after {format_seconds(self.initial_delay)}" if self.initial_delay else ""
        rules = [f"success when {self.success.text}" if self.success else "", f"failure when {self.failure.text}" if self.failure else ""]
        return f"{self.name}: {cadence}{delay}; {'; '.join(r for r in rules if r) or 'always successful'}; failureLimit {self.failure_limit}"


def substitute(value: Any, args: Dict[str, Optional[str]], template: str) -> Any:
    if isinstance(value, dict):
        return {k: substitute(v, args, template) for k, v in value.items()}
    if isinstance(value, list):
        return [substitute(v, args, template) for v in value]
    if not isinstance(value, str):
        return value

    def lookup(match: "re.Match[str]") -> str:
        name = match.group(1)
        if name not in args:
            raise InputError("analysis template %s uses {{args.%s}} but does not declare it in spec.args" % (template, name))
        return str(args[name])

    return ARG_PATTERN.sub(lookup, value)


def metric_spec(raw: Dict[str, Any], template: str) -> MetricSpec:
    name = str(raw.get("name") or "")
    where = f"analysis template {template}, metric {name or '?'}"
    if not name:
        raise InputError(f"{where}: missing name")
    provider = raw.get("provider") or {}
    if "prometheus" not in provider:
        raise InputError(f"{where}: minirollout implements the prometheus provider only (found {', '.join(provider) or 'none'})")
    prometheus = provider.get("prometheus") or {}
    query = str(prometheus.get("query") or "").strip()
    try:
        parse_query(query)
        success = Condition(raw["successCondition"]) if raw.get("successCondition") else None
        failure = Condition(raw["failureCondition"]) if raw.get("failureCondition") else None
        interval = parse_duration(raw["interval"]) if raw.get("interval") not in (None, "") else 0.0
        delay = parse_duration(raw["initialDelay"]) if raw.get("initialDelay") not in (None, "") else 0.0
        count: Optional[int] = int(raw["count"]) if raw.get("count") not in (None, "") else 0
        limits = [int(raw.get(key, default)) for key, default in (("failureLimit", 0), ("inconclusiveLimit", 0), ("consecutiveErrorLimit", CONSECUTIVE_ERROR_LIMIT))]
    except (QueryError, ConditionError, InputError, ValueError, TypeError) as exc:
        raise InputError(f"{where}: {exc}")
    if count == 0:
        count = None if interval else 1
    if count is not None and count > 1 and not interval:
        raise InputError(f"{where}: count {count} needs an interval")
    return MetricSpec(name, query, str(prometheus.get("address") or ""), interval, count, delay, success, failure, *limits)


@dataclass
class AnalysisTemplate:
    name: str
    defaults: Dict[str, Optional[str]]
    metrics: List[Dict[str, Any]]

    def resolve(self, args: Dict[str, str]) -> List[MetricSpec]:
        values: Dict[str, Optional[str]] = dict(self.defaults)
        values.update(args)
        missing = [k for k, v in values.items() if v is None]
        if missing:
            raise InputError(f"analysis template {self.name}: argument {missing[0]!r} has no value; pass it in the step's args")
        return [metric_spec(substitute(raw, values, self.name), self.name) for raw in self.metrics]


def parse_template(doc: Dict[str, Any]) -> AnalysisTemplate:
    if doc.get("kind") not in TEMPLATE_KINDS:
        raise InputError(f"expected kind AnalysisTemplate, got {doc.get('kind')!r}")
    name = str((doc.get("metadata") or {}).get("name") or "")
    spec = doc.get("spec") or {}
    metrics = spec.get("metrics") or []
    if not name or not metrics:
        raise InputError(f"analysis template {name or '?'}: needs metadata.name and at least one entry in spec.metrics")
    defaults: Dict[str, Optional[str]] = {}
    for arg in spec.get("args") or []:
        if "valueFrom" in arg:
            raise InputError(f"analysis template {name}: arg {arg.get('name')!r} uses valueFrom, which minirollout does not resolve; give a value")
        defaults[str(arg["name"])] = None if arg.get("value") is None else str(arg["value"])
    return AnalysisTemplate(name, defaults, list(metrics))


@dataclass
class Measurement:
    at: float
    phase: str
    value: Any = None
    message: str = ""


def evaluate_result(value: Any, spec: MetricSpec) -> str:
    outcomes: Dict[str, bool] = {}
    for which, condition in (("successCondition", spec.success), ("failureCondition", spec.failure)):
        if condition is None:
            continue
        try:
            outcomes[which] = condition.evaluate(value)
        except ConditionError as exc:
            if value is None or value == []:
                raise ConditionError(f'could not evaluate {which} "{condition.text}": metric result is nil or empty: no data returned from the metric provider')
            raise ConditionError(f'could not evaluate {which} "{condition.text}": {exc}')
    if not outcomes:
        return SUCCESSFUL
    success = outcomes.get("successCondition", not outcomes.get("failureCondition", False))
    failure = outcomes.get("failureCondition", not outcomes.get("successCondition", True))
    if failure:
        return FAILED
    if not success:
        return INCONCLUSIVE
    return SUCCESSFUL


def take_measurement(spec: MetricSpec, measure: Callable[[MetricSpec], Any], now: float) -> Measurement:
    try:
        value = measure(spec)
    except MeasurementError as exc:
        return Measurement(now, ERROR, None, str(exc))
    try:
        return Measurement(now, evaluate_result(value, spec), value)
    except ConditionError as exc:
        return Measurement(now, ERROR, value, str(exc))


@dataclass
class MetricRun:
    spec: MetricSpec
    measurements: List[Measurement] = field(default_factory=list)
    phase: str = PENDING
    message: str = ""

    def tally(self, phase: str) -> int:
        return sum(1 for m in self.measurements if m.phase == phase)

    def consecutive_errors(self) -> int:
        count = 0
        for measurement in reversed(self.measurements):
            if measurement.phase != ERROR:
                break
            count += 1
        return count

    def counted(self) -> int:
        return sum(1 for m in self.measurements if m.phase != ERROR)

    def due(self, started: float) -> Optional[float]:
        if self.phase in COMPLETED:
            return None
        if not self.measurements:
            return started + self.spec.initial_delay
        if self.spec.count is not None and self.counted() >= self.spec.count:
            return None
        last = self.measurements[-1]
        return last.at + (ERROR_RETRY if last.phase == ERROR else self.spec.interval or ERROR_RETRY)


def assess_metric(run: MetricRun) -> Tuple[str, str]:
    spec = run.spec
    phase, message = "", ""
    failed, inconclusive, errors = run.tally(FAILED), run.tally(INCONCLUSIVE), run.consecutive_errors()
    if spec.failure_limit >= 0 and failed > spec.failure_limit:
        phase, message = FAILED, f"failed ({failed}) > failureLimit ({spec.failure_limit})"
    if inconclusive > spec.inconclusive_limit:
        phase, message = INCONCLUSIVE, f"inconclusive ({inconclusive}) > inconclusiveLimit ({spec.inconclusive_limit})"
    if errors > spec.consecutive_error_limit:
        phase, message = ERROR, f"consecutiveErrors ({errors}) > consecutiveErrorLimit ({spec.consecutive_error_limit})"
    if phase:
        return phase, message
    if spec.count is not None and run.counted() >= spec.count:
        return SUCCESSFUL, ""
    return RUNNING, ""


def assess_run(metrics: Sequence[MetricRun], terminating: bool) -> Tuple[str, str]:
    worst, message, everything_completed = "", "", True
    for run in metrics:
        if run.phase not in COMPLETED:
            everything_completed = False
            continue
        if not worst or BEST_TO_WORST.index(run.phase) > BEST_TO_WORST.index(worst):
            worst = run.phase
            if run.message:
                message = f'Metric "{run.spec.name}" assessed {run.phase} due to {run.message}'
                last = run.measurements[-1].message if run.measurements else ""
                if last:
                    message += f': "Error Message: {last}"'
    if terminating:
        return worst or SUCCESSFUL, message
    if not everything_completed or not worst:
        return RUNNING, ""
    return worst, message


class AnalysisRun:
    def __init__(self, name: str, metrics: Sequence[MetricSpec], started: float, observer: Optional[Callable[[str, Measurement], None]] = None):
        self.name = name
        self.started = started
        self.metrics = [MetricRun(m) for m in metrics]
        self.phase = RUNNING
        self.message = ""
        self.observer = observer

    def tick(self, now: float, measure: Callable[[MetricSpec], Any]) -> str:
        if self.phase in COMPLETED:
            return self.phase
        for run in self.metrics:
            due = run.due(self.started)
            if due is None or now + 1e-6 < due:
                continue
            measurement = take_measurement(run.spec, measure, now)
            run.measurements.append(measurement)
            if self.observer:
                self.observer(run.spec.name, measurement)
            run.phase, run.message = assess_metric(run)
        terminating = any(run.phase in (FAILED, ERROR, INCONCLUSIVE) for run in self.metrics)
        if terminating:
            self.settle_running()
        self.phase, self.message = assess_run(self.metrics, terminating)
        return self.phase

    def settle_running(self) -> None:
        for run in self.metrics:
            if run.phase not in COMPLETED:
                run.phase = SUCCESSFUL

    def terminate(self) -> str:
        if self.phase not in COMPLETED:
            self.settle_running()
            self.phase, self.message = assess_run(self.metrics, True)
        return self.phase

    def summary(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "phase": self.phase,
            "message": self.message,
            "metrics": [
                {
                    "name": run.spec.name,
                    "phase": run.phase,
                    "message": run.message,
                    "measurements": [{"at": round(m.at, 3), "phase": m.phase, "value": jsonable(m.value), "message": m.message} for m in run.measurements],
                }
                for run in self.metrics
            ],
        }


@dataclass
class Step:
    kind: str
    weight: int = 0
    duration: Optional[float] = None
    templates: List[str] = field(default_factory=list)
    args: Dict[str, str] = field(default_factory=dict)
    reason: str = "CanaryPauseStep"
    note: str = ""

    @property
    def label(self) -> str:
        if self.kind == "setWeight":
            return self.note or f"setWeight: {self.weight}"
        if self.kind == "pause":
            return f"pause: {format_seconds(self.duration)}" if self.duration is not None else "pause: until promoted"
        if self.kind == "analysis":
            return f"{self.note or 'analysis'}: {', '.join(self.templates)}"
        return "preview: new version scaled up, no live traffic"

    @property
    def compact(self) -> str:
        if self.kind == "setWeight":
            return f"{self.weight}%"
        if self.kind == "pause":
            return "pause" if self.duration is None else f"pause={format_seconds(self.duration)}"
        return self.kind


def template_hash(template: Dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(template, sort_keys=True, separators=(",", ":")).encode()).hexdigest()[:10]


@dataclass
class RolloutSpec:
    name: str
    strategy: str
    replicas: int
    max_surge: int
    selector: Dict[str, str]
    template: Dict[str, Any]
    steps: List[Step]
    background: Optional[Step] = None
    starting_step: int = 0

    @property
    def revision(self) -> str:
        return template_hash(self.template)


def resolve_surge(value: Any, replicas: int, rollout: str) -> int:
    text = str(value).strip()
    try:
        if text.endswith("%"):
            return math.ceil(replicas * float(text[:-1]) / 100)
        return int(text)
    except ValueError:
        raise InputError(f"rollout {rollout}: maxSurge must be an integer or a percentage, got {value!r}")


def analysis_step(raw: Dict[str, Any], where: str, note: str) -> Step:
    names = [str(t.get("templateName") or "") for t in raw.get("templates") or []]
    if not names or not all(names):
        raise InputError(f"{where}: analysis needs templates: [{{templateName: NAME}}]")
    args: Dict[str, str] = {}
    for arg in raw.get("args") or []:
        if "value" not in arg:
            raise InputError(f"{where}: arg {arg.get('name')!r} has no value (valueFrom is not supported)")
        args[str(arg["name"])] = str(arg["value"])
    return Step("analysis", templates=names, args=args, note=note)


def parse_step(raw: Any, index: int, rollout: str) -> Step:
    where = f"rollout {rollout}: step {index + 1}"
    if not isinstance(raw, dict) or len(raw) != 1:
        raise InputError(f"{where}: a step has exactly one key: setWeight, pause or analysis")
    kind, value = next(iter(raw.items()))
    if kind == "setWeight":
        try:
            weight = int(value)
        except (TypeError, ValueError):
            raise InputError(f"{where}: setWeight must be an integer, got {value!r}")
        if not 0 <= weight <= 100:
            raise InputError(f"{where}: setWeight must be between 0 and 100, got {weight}")
        return Step("setWeight", weight)
    if kind == "pause":
        value = value or {}
        duration = value.get("duration")
        return Step("pause", duration=None if duration in (None, "") else parse_duration(duration))
    if kind == "analysis":
        return analysis_step(value or {}, where, "analysis")
    raise InputError(f"{where}: {kind} is not implemented by minirollout (it implements setWeight, pause and analysis)")


def parse_rollout(doc: Dict[str, Any]) -> RolloutSpec:
    if doc.get("kind") != "Rollout":
        raise InputError(f"expected kind Rollout (argoproj.io/v1alpha1), got {doc.get('kind')!r}")
    name = str((doc.get("metadata") or {}).get("name") or "")
    spec = doc.get("spec") or {}
    template = spec.get("template")
    if not name or not isinstance(template, dict):
        raise InputError("a Rollout needs metadata.name and spec.template (minirollout does not follow workloadRef)")
    try:
        replicas = int(spec.get("replicas", 1))
    except (TypeError, ValueError):
        raise InputError(f"rollout {name}: replicas must be an integer")
    selector = dict((spec.get("selector") or {}).get("matchLabels") or (template.get("metadata") or {}).get("labels") or {})
    strategy = spec.get("strategy") or {}
    if ("canary" in strategy) == ("blueGreen" in strategy):
        raise InputError(f"rollout {name}: spec.strategy needs exactly one of canary or blueGreen")
    if "canary" in strategy:
        canary = strategy.get("canary") or {}
        steps = [parse_step(raw, i, name) for i, raw in enumerate(canary.get("steps") or [])]
        background, starting = None, 0
        if canary.get("analysis"):
            background = analysis_step(canary["analysis"], f"rollout {name}: canary.analysis", "background analysis")
            starting = int(canary["analysis"].get("startingStep", 0))
        surge = resolve_surge(canary.get("maxSurge", "25%"), replicas, name)
        return RolloutSpec(name, "canary", replicas, surge, selector, template, steps, background, starting)
    blue = strategy.get("blueGreen") or {}
    if not blue.get("activeService"):
        raise InputError(f"rollout {name}: blueGreen.activeService is required")
    steps = [Step("preview")]
    if blue.get("prePromotionAnalysis"):
        steps.append(analysis_step(blue["prePromotionAnalysis"], f"rollout {name}: prePromotionAnalysis", "prePromotionAnalysis"))
    if blue.get("autoPromotionEnabled", True) is False:
        steps.append(Step("pause", reason="BlueGreenPause"))
    elif blue.get("autoPromotionSeconds"):
        steps.append(Step("pause", duration=float(blue["autoPromotionSeconds"]), reason="BlueGreenPause"))
    steps.append(Step("setWeight", 100, note=f"switch {blue['activeService']} to the new version"))
    if blue.get("postPromotionAnalysis"):
        steps.append(analysis_step(blue["postPromotionAnalysis"], f"rollout {name}: postPromotionAnalysis", "postPromotionAnalysis"))
    return RolloutSpec(name, "blueGreen", replicas, 0, selector, template, steps)


def floor_ceiling_tied(weight: int, total: int) -> bool:
    return math.modf(total * (weight / 100))[0] == 0.5


def canary_counts(replicas: int, weight: int, max_surge: int) -> Tuple[int, int]:
    if replicas <= 0:
        return 0, 0
    exact = replicas * weight / 100
    ceil_count, floor_count = math.ceil(exact), math.floor(exact)
    zero_allowed = weight in (0, 100) or (replicas == 1 and max_surge == 0)
    options: List[Tuple[int, int]] = []
    if ceil_count < replicas or zero_allowed:
        options.append((ceil_count, replicas))
    if not floor_ceiling_tied(weight, replicas) and (floor_count != 0 or zero_allowed):
        options.append((floor_count, replicas))
    if max_surge > 0:
        options.append((ceil_count, replicas + 1))
        if not floor_ceiling_tied(weight, replicas + 1) and (floor_count != 0 or zero_allowed):
            options.append((floor_count, replicas + 1))
    if not options:
        return 0, replicas
    best = min(options, key=lambda option: abs(100 * option[0] / option[1] - weight))
    return best[0], best[1] - best[0]


def effective_weight(canary: int, stable: int) -> float:
    return 100.0 * canary / (canary + stable) if canary + stable else 0.0


class ExactShare:
    def __init__(self, share: float = 0.0):
        self.lock = threading.Lock()
        self.share = share
        self.count = 0
        self.hits = 0

    def set(self, share: float) -> None:
        with self.lock:
            if share != self.share:
                self.share, self.count, self.hits = share, 0, 0

    def next(self) -> bool:
        with self.lock:
            self.count += 1
            if math.floor(self.count * self.share + 1e-9) > self.hits:
                self.hits += 1
                return True
            return False


@dataclass
class Desired:
    stable: str
    stable_template: Dict[str, Any]
    canary: str = ""
    canary_template: Optional[Dict[str, Any]] = None
    weight: int = 0
    preview: bool = False
    promoting: bool = False


@dataclass
class Status:
    rollout: str = ""
    phase: str = ""
    message: str = ""
    ready: bool = False
    revision: int = 0
    stable: str = ""
    stable_template: Optional[Dict[str, Any]] = None
    canary: str = ""
    step: int = 0
    steps: int = 0
    weight: int = 0
    preview: bool = False
    abort: bool = False
    skip_pause: bool = False
    promote_full: bool = False
    step_started: Optional[float] = None
    paused_at: Optional[float] = None
    events: List[Dict[str, Any]] = field(default_factory=list)
    runs: List[Dict[str, Any]] = field(default_factory=list)

    def to_json(self) -> Dict[str, Any]:
        return dataclasses.asdict(self)

    @classmethod
    def from_json(cls, data: Dict[str, Any]) -> "Status":
        names = {f.name for f in dataclasses.fields(cls)}
        return cls(**{k: v for k, v in data.items() if k in names})


class Controller:
    def __init__(self, spec: RolloutSpec, templates: Dict[str, AnalysisTemplate], target: Any, measure: Callable[[MetricSpec], Any], status: Optional[Status] = None):
        self.spec = spec
        self.templates = templates
        self.target = target
        self.measure = measure
        self.status = status or Status()
        self.status.rollout = spec.name
        self.status.steps = len(spec.steps)
        self.step_run: Optional[AnalysisRun] = None
        self.background_run: Optional[AnalysisRun] = None
        self.on_event: Optional[Callable[[Dict[str, Any]], None]] = None
        self.on_measurement: Optional[Callable[[str, str, Measurement], None]] = None

    def event(self, now: float, reason: str, message: str) -> None:
        entry = {"at": round(now, 3), "reason": reason, "message": message}
        self.status.events.append(entry)
        if self.on_event:
            self.on_event(entry)

    def desired(self, promoting: bool = False) -> Desired:
        st = self.status
        stable_template = st.stable_template or self.spec.template
        if promoting:
            return Desired(st.canary, self.spec.template, promoting=True)
        if not st.canary:
            return Desired(st.stable, stable_template)
        return Desired(st.stable, stable_template, st.canary, self.spec.template, st.weight, st.preview)

    def sync(self, now: float, promoting: bool = False) -> bool:
        self.status.ready = bool(self.target.sync(self.desired(promoting), now))
        return self.status.ready

    def reset_steps(self, canary: str) -> None:
        st = self.status
        st.canary, st.abort, st.weight, st.step, st.preview = canary, False, 0, 0, False
        st.step_started = st.paused_at = None
        st.skip_pause = st.promote_full = False

    def reconcile(self, now: float) -> Status:
        st, spec = self.status, self.spec
        revision = spec.revision
        if not st.stable:
            st.revision, st.stable, st.stable_template = 1, revision, copy.deepcopy(spec.template)
            self.event(now, "RolloutUpdated", f"Rollout created at revision 1 ({revision}); the first version skips the steps")
        if revision == st.stable and (st.canary or st.abort):
            self.finish_runs()
            self.event(now, "RolloutUpdated", f"template is back at the stable revision {revision}; canary {st.canary} removed")
            self.reset_steps("")
        elif revision != st.stable and revision != st.canary:
            if st.canary:
                replaced = "aborted canary" if st.abort else "canary in progress"
                self.event(now, "RolloutUpdated", f"a new template replaces the {replaced} {st.canary}; the steps start again")
            self.finish_runs()
            st.revision += 1
            self.reset_steps(revision)
            self.event(now, "RolloutUpdated", f"Rollout updated to revision {st.revision} ({revision})")
        if not st.canary:
            ready = self.sync(now)
            st.phase, st.message = (HEALTHY, "Rollout is healthy") if ready else (PROGRESSING, "waiting for the stable version to become ready")
            return st
        if st.abort:
            self.sync(now)
            st.phase = DEGRADED
            return st
        if st.promote_full:
            st.promote_full = False
            self.finish_runs()
            st.step, st.paused_at = len(spec.steps), None
            self.event(now, "RolloutResumed", "full promotion requested: the remaining steps are skipped")
        if not self.background(now):
            return st
        while st.step < len(spec.steps):
            step = spec.steps[st.step]
            if st.step_started is None:
                st.step_started = now
            if self.run_step(step, now) != DONE:
                return st
            self.event(now, "RolloutStepCompleted", f"Rollout step {st.step + 1}/{len(spec.steps)} completed ({step.label})")
            st.step += 1
            st.step_started = None
        return self.promote(now)

    def run_step(self, step: Step, now: float) -> str:
        st = self.status
        if step.kind in ("setWeight", "preview"):
            weight, preview = (0, True) if step.kind == "preview" else (step.weight, False)
            if (st.weight, st.preview) != (weight, preview):
                st.weight, st.preview = weight, preview
                self.event(now, "TrafficWeightUpdated", f"new version at {weight}% ({self.target.explain(self.desired())})")
            if not self.sync(now):
                st.phase, st.message = PROGRESSING, f"waiting for the new version at {step.label}"
                return WAIT
            return DONE
        self.sync(now)
        if step.kind == "pause":
            return self.pause(step, now)
        return self.analysis(step, now)

    def pause(self, step: Step, now: float) -> str:
        st = self.status
        started = now if st.step_started is None else st.step_started
        elapsed = step.duration is not None and now - started >= step.duration - 1e-6
        if st.skip_pause or elapsed:
            how = "promoted" if st.skip_pause else "duration elapsed"
            st.skip_pause = False
            if st.paused_at is not None:
                self.event(now, "RolloutResumed", f"Rollout is resumed ({how})")
            st.paused_at = None
            return DONE
        if st.paused_at is None:
            st.paused_at = now
            self.event(now, "RolloutPaused", f"Rollout is paused ({step.reason})")
        st.phase, st.message = PAUSED, step.reason
        return WAIT

    def accept_inconclusive(self, now: float, name: str) -> None:
        st = self.status
        st.skip_pause, st.paused_at = False, None
        self.event(now, "RolloutResumed", f"inconclusive AnalysisRun {name} accepted by promote")

    def analysis(self, step: Step, now: float) -> str:
        st = self.status
        name = f"{self.spec.name}-{st.revision}-{st.step + 1}"
        if self.step_run is None and st.skip_pause and st.paused_at is not None:
            self.accept_inconclusive(now, name)
            return DONE
        if self.step_run is None:
            self.step_run = self.start_run(step, now, name)
        run = self.step_run
        phase = run.tick(now, self.measure)
        if phase == SUCCESSFUL:
            self.archive(run)
            self.event(now, "AnalysisRunSuccessful", f"AnalysisRun {run.name} Successful")
            return DONE
        if phase in (FAILED, ERROR):
            self.archive(run)
            self.event(now, "AnalysisRunFailed", f"AnalysisRun {run.name} {phase}: {run.message}")
            self.abort(now, run.message)
            return ABORTED
        if phase == INCONCLUSIVE:
            if st.skip_pause:
                self.archive(run)
                self.accept_inconclusive(now, run.name)
                return DONE
            if st.paused_at is None:
                st.paused_at = now
                self.event(now, "RolloutPaused", f"Rollout is paused (InconclusiveAnalysisRun): {run.message}")
            st.phase, st.message = PAUSED, "InconclusiveAnalysisRun"
            return WAIT
        st.phase, st.message = PROGRESSING, f"AnalysisRun {run.name} running"
        return WAIT

    def background(self, now: float) -> bool:
        spec, st = self.spec, self.status
        if spec.background is None or st.step < spec.starting_step or st.step >= len(spec.steps):
            return True
        if self.background_run is None:
            self.background_run = self.start_run(spec.background, now, f"{spec.name}-{st.revision}-background")
        run = self.background_run
        phase = run.tick(now, self.measure)
        if phase in (FAILED, ERROR):
            self.archive(run)
            self.event(now, "AnalysisRunFailed", f"AnalysisRun {run.name} {phase}: {run.message}")
            self.abort(now, run.message)
            return False
        if phase == INCONCLUSIVE:
            if st.skip_pause:
                self.archive(run)
                self.accept_inconclusive(now, run.name)
                return True
            if st.paused_at is None:
                st.paused_at = now
                self.event(now, "RolloutPaused", f"Rollout is paused (InconclusiveAnalysisRun): {run.message}")
            st.phase, st.message = PAUSED, "InconclusiveAnalysisRun"
            return False
        return True

    def start_run(self, step: Step, now: float, name: str) -> AnalysisRun:
        metrics: List[MetricSpec] = []
        for template in step.templates:
            metrics += self.templates[template].resolve(step.args)
        observer = None
        if self.on_measurement:
            report = self.on_measurement
            observer = lambda metric, measurement: report(name, metric, measurement)
        self.event(now, "AnalysisRunRunning", f"AnalysisRun {name} started ({', '.join(m.name for m in metrics)})")
        return AnalysisRun(name, metrics, now, observer)

    def archive(self, run: AnalysisRun, terminate: bool = False) -> None:
        if terminate:
            run.terminate()
        self.status.runs.append(run.summary())
        if run is self.step_run:
            self.step_run = None
        if run is self.background_run:
            self.background_run = None

    def finish_runs(self) -> None:
        for run in (self.step_run, self.background_run):
            if run is not None:
                self.archive(run, terminate=True)

    def abort(self, now: float, reason: str) -> None:
        st = self.status
        self.finish_runs()
        st.abort, st.weight, st.preview, st.paused_at = True, 0, False, None
        st.phase = DEGRADED
        st.message = f"RolloutAborted: Rollout aborted update to revision {st.revision}" + (f": {reason}" if reason else "")
        self.event(now, "RolloutAborted", st.message)
        self.sync(now)

    def request_abort(self, now: float) -> bool:
        if not self.status.canary or self.status.abort:
            return False
        self.abort(now, "")
        return True

    def promote(self, now: float) -> Status:
        st = self.status
        self.finish_runs()
        if not self.sync(now, promoting=True):
            st.phase, st.message = PROGRESSING, "promoting: the new version becomes the stable one"
            return st
        self.event(now, "RolloutCompleted", f"Rollout completed update to revision {st.revision} ({st.canary}): completed all {len(self.spec.steps)} steps")
        st.stable, st.stable_template = st.canary, copy.deepcopy(self.spec.template)
        self.reset_steps("")
        st.phase, st.message = HEALTHY, "Rollout is healthy"
        return st

    def waiting_for_human(self) -> bool:
        st = self.status
        if st.phase != PAUSED:
            return False
        if st.message == "InconclusiveAnalysisRun":
            return True
        return st.step < len(self.spec.steps) and self.spec.steps[st.step].kind == "pause" and self.spec.steps[st.step].duration is None


class SimTarget:
    def __init__(self, replicas: int = 4, max_surge: int = 1, by_replicas: bool = False, ready_delay: float = 0.0):
        self.replicas = replicas
        self.max_surge = max_surge
        self.by_replicas = by_replicas
        self.ready_delay = ready_delay
        self.pods = 0
        self.live = 0.0
        self.waiting_since: Optional[float] = None

    def plan(self, desired: Desired) -> Tuple[int, float]:
        if desired.promoting:
            return self.replicas, 100.0
        if not desired.canary:
            return 0, 0.0
        if desired.preview:
            return self.replicas, 0.0
        if self.by_replicas:
            canary, stable = canary_counts(self.replicas, desired.weight, self.max_surge)
            return canary, effective_weight(canary, stable)
        return (math.ceil(self.replicas * desired.weight / 100) if desired.weight else 0), float(desired.weight)

    def explain(self, desired: Desired) -> str:
        if self.by_replicas and desired.canary and not desired.preview:
            canary, stable = canary_counts(self.replicas, desired.weight, self.max_surge)
            return f"{canary} new and {stable} stable pods, {format_number(round(effective_weight(canary, stable), 1))}% of traffic"
        return "exact weight"

    def sync(self, desired: Desired, now: float) -> bool:
        pods, share = self.plan(desired)
        if pods > self.pods:
            if self.waiting_since is None:
                self.waiting_since = now
            if now - self.waiting_since < self.ready_delay - 1e-6:
                return False
        self.waiting_since = None
        self.pods, self.live = pods, share
        return True


def http_json(method: str, url: str, body: Optional[Dict[str, Any]] = None, timeout: float = 10.0) -> Dict[str, Any]:
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(url, data=data, method=method, headers={"Content-Type": "application/json"})
    try:
        with NO_PROXY.open(request, timeout=timeout) as response:
            return json.loads(response.read().decode() or "{}")
    except urllib.error.HTTPError as exc:
        raise TargetError(f"{method} {url}: HTTP {exc.code}: {exc.read().decode(errors='replace').strip()[:200]}")
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise TargetError(f"{method} {url}: {exc}")


class ProxyTarget:
    def __init__(self, proxy: str, canary_url: str):
        self.proxy = proxy.rstrip("/")
        self.canary_url = canary_url.rstrip("/")

    def explain(self, desired: Desired) -> str:
        return f"exact weight on {self.proxy}"

    def sync(self, desired: Desired, now: float) -> bool:
        current = http_json("GET", self.proxy + "/admin/config")
        if desired.promoting:
            wanted = {"stable": self.canary_url, "canary": None, "weight": 0}
        elif desired.canary:
            wanted = {"stable": current.get("stable"), "canary": self.canary_url, "weight": desired.weight}
        else:
            wanted = {"stable": current.get("stable"), "canary": None, "weight": 0}
        if any(current.get(key) != value for key, value in wanted.items()):
            http_json("POST", self.proxy + "/admin/config", wanted)
        return True


def run_command(argv: Sequence[str], stdin: Optional[str] = None) -> Tuple[int, str, str]:
    try:
        proc = subprocess.run(list(argv), input=stdin, capture_output=True, text=True, timeout=60)
    except FileNotFoundError:
        return 127, "", f"{argv[0]}: command not found"
    except subprocess.TimeoutExpired:
        return 124, "", f"{' '.join(argv)}: timed out after 60s"
    return proc.returncode, proc.stdout, proc.stderr


def settled(live: Optional[Dict[str, Any]], replicas: int, revision: str) -> bool:
    if live is None:
        return replicas == 0
    meta, spec, status = live.get("metadata") or {}, live.get("spec") or {}, live.get("status") or {}
    if (meta.get("annotations") or {}).get(REVISION_ANNOTATION) != revision or spec.get("replicas") != replicas:
        return False
    if int(status.get("observedGeneration") or 0) < int(meta.get("generation") or 0):
        return False
    return all(int(status.get(key) or 0) == replicas for key in ("replicas", "updatedReplicas", "readyReplicas"))


class KubeTarget:
    def __init__(self, spec: RolloutSpec, namespace: str, context: Optional[str] = None, runner: Runner = run_command):
        self.spec = spec
        self.namespace = namespace
        self.context = context
        self.runner = runner
        self.applied: Dict[str, str] = {}

    def kubectl(self, args: Sequence[str], stdin: Optional[str] = None) -> str:
        argv = ["kubectl"] + (["--context", self.context] if self.context else []) + ["-n", self.namespace] + list(args)
        code, out, err = self.runner(argv, stdin)
        if code != 0:
            raise TargetError(f"{' '.join(argv)}: {(err or out).strip()}")
        return out

    def deployment_name(self, track: str) -> str:
        return f"{self.spec.name}-{track}"

    def observe(self) -> Dict[str, Dict[str, Any]]:
        out = self.kubectl(["get", "deployment", self.deployment_name("stable"), self.deployment_name("canary"), "-o", "json", "--ignore-not-found"])
        found: Dict[str, Dict[str, Any]] = {}
        for item in documents(json.loads(out)) if out.strip() else []:
            for track in ("stable", "canary"):
                if (item.get("metadata") or {}).get("name") == self.deployment_name(track):
                    found[track] = item
        return found

    def manifest(self, track: str, template: Dict[str, Any], revision: str, replicas: int) -> Dict[str, Any]:
        pod = copy.deepcopy(template)
        meta = pod.setdefault("metadata", {})
        meta["labels"] = dict(meta.get("labels") or {}, track=track)
        return {
            "apiVersion": "apps/v1",
            "kind": "Deployment",
            "metadata": {
                "name": self.deployment_name(track),
                "namespace": self.namespace,
                "labels": {"app.kubernetes.io/managed-by": "minirollout", "minirollout/rollout": self.spec.name, "track": track},
                "annotations": {REVISION_ANNOTATION: revision},
            },
            "spec": {"replicas": replicas, "selector": {"matchLabels": dict(self.spec.selector, track=track)}, "template": pod},
        }

    def apply(self, wanted: Dict[str, Dict[str, Any]], live: Dict[str, Dict[str, Any]]) -> None:
        stale = []
        for track, manifest in wanted.items():
            text = json.dumps(manifest, sort_keys=True)
            current = live.get(track) or {}
            in_sync = (current.get("spec") or {}).get("replicas") == manifest["spec"]["replicas"] and ((current.get("metadata") or {}).get("annotations") or {}).get(REVISION_ANNOTATION) == manifest["metadata"]["annotations"][REVISION_ANNOTATION]
            if self.applied.get(track) != text or not in_sync:
                stale.append((track, text, manifest))
        if not stale:
            return
        body = json.dumps({"apiVersion": "v1", "kind": "List", "items": [m for _, _, m in stale]})
        self.kubectl(["apply", "--server-side", "--field-manager=minirollout", "--force-conflicts", "-f", "-"], body)
        for track, text, _ in stale:
            self.applied[track] = text

    def explain(self, desired: Desired) -> str:
        canary, stable = canary_counts(self.spec.replicas, desired.weight, self.spec.max_surge)
        return f"{canary} new and {stable} stable pods behind one Service, about {format_number(round(effective_weight(canary, stable), 1))}% of connections"

    def sync(self, desired: Desired, now: float) -> bool:
        if desired.preview:
            raise TargetError("the kube target splits traffic by pod count behind one Service; blue/green needs a Service selector switch, which it does not implement (use --target proxy)")
        replicas = self.spec.replicas
        live = self.observe()
        if desired.promoting:
            canary_now = int(((live.get("canary") or {}).get("spec") or {}).get("replicas") or 0)
            stable = self.manifest("stable", desired.stable_template, desired.stable, replicas)
            if not settled(live.get("stable"), replicas, desired.stable):
                self.apply({"stable": stable, "canary": self.manifest("canary", desired.stable_template, desired.stable, canary_now)}, live)
                return False
            self.apply({"stable": stable, "canary": self.manifest("canary", desired.stable_template, desired.stable, 0)}, live)
            return settled(self.observe().get("canary"), 0, desired.stable)
        if desired.canary:
            canary_count, stable_count = canary_counts(replicas, desired.weight, self.spec.max_surge)
            canary = self.manifest("canary", desired.canary_template or desired.stable_template, desired.canary, canary_count)
        else:
            canary_count, stable_count = 0, replicas
            canary = self.manifest("canary", desired.stable_template, desired.stable, 0)
        self.apply({"stable": self.manifest("stable", desired.stable_template, desired.stable, stable_count), "canary": canary}, live)
        after = self.observe()
        canary_revision = canary["metadata"]["annotations"][REVISION_ANNOTATION]
        return settled(after.get("stable"), stable_count, desired.stable) and settled(after.get("canary"), canary_count, canary_revision)


class QuietHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, format: str, *args: Any) -> None:
        pass

    def read_body(self) -> bytes:
        length = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(length) if length > 0 else b""

    def reply(self, code: int, body: bytes, content_type: str = "application/json", headers: Optional[Dict[str, str]] = None) -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def reply_json(self, code: int, payload: Any, headers: Optional[Dict[str, str]] = None) -> None:
        self.reply(code, (json.dumps(payload) + "\n").encode(), "application/json", headers)

    def serve_observability(self, registry: Registry, db: TSDB, body: bytes) -> bool:
        parts = urllib.parse.urlsplit(self.path)
        if parts.path == "/metrics":
            self.reply(200, registry.render().encode(), "text/plain; version=0.0.4; charset=utf-8")
            return True
        if parts.path in ("/-/ready", "/-/healthy"):
            self.reply(200, b"ready\n", "text/plain")
            return True
        if parts.path != "/api/v1/query":
            return False
        params = urllib.parse.parse_qs(parts.query)
        if self.command == "POST":
            params.update(urllib.parse.parse_qs(body.decode("utf-8", "replace")))
        try:
            at = float(params["time"][0]) if params.get("time") else time.time()
        except ValueError:
            self.reply_json(400, {"status": "error", "errorType": "bad_data", "error": "time must be a Unix timestamp in seconds"})
            return True
        code, payload = query_response(db, (params.get("query") or [""])[0], at)
        self.reply_json(code, payload)
        return True

    def do_GET(self) -> None:
        self.handle_any()

    def do_POST(self) -> None:
        self.handle_any()

    def do_HEAD(self) -> None:
        self.handle_any()

    def do_PUT(self) -> None:
        self.handle_any()

    def do_DELETE(self) -> None:
        self.handle_any()

    def handle_any(self) -> None:
        self.reply_json(404, {"error": "not found"})


class AppServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address: Tuple[str, int], version: str, error_rate: float = 0.0, latency: float = 0.0):
        super().__init__(address, AppHandler)
        self.version = version
        self.latency = latency
        self.errors = ExactShare(error_rate)
        self.registry = Registry()


class AppHandler(QuietHandler):
    server: AppServer

    def handle_any(self) -> None:
        self.read_body()
        parts = urllib.parse.urlsplit(self.path)
        server = self.server
        if parts.path == "/healthz":
            return self.reply(200, b"ok\n", "text/plain")
        if parts.path == "/metrics":
            return self.reply(200, server.registry.render().encode(), "text/plain; version=0.0.4; charset=utf-8")
        if parts.path == "/admin/errors" and self.command == "POST":
            try:
                rate = float((urllib.parse.parse_qs(parts.query).get("rate") or [""])[0])
            except ValueError:
                return self.reply_json(400, {"error": "use POST /admin/errors?rate=0.25"})
            server.errors.set(rate)
            return self.reply_json(200, {"version": server.version, "error_rate": rate})
        started = time.monotonic()
        if server.latency:
            time.sleep(server.latency)
        code = 500 if server.errors.next() else 200
        body = (json.dumps({"version": server.version, "status": code, "path": parts.path}) + "\n").encode()
        server.registry.observe({"version": server.version}, code, time.monotonic() - started)
        self.reply(code, body, "application/json", {"X-Version": server.version})


def forward(url: str, method: str, headers: Dict[str, str], body: Optional[bytes], timeout: float) -> Tuple[int, List[Tuple[str, str]], bytes]:
    request = urllib.request.Request(url, data=body, method=method, headers=headers)
    try:
        with NO_PROXY.open(request, timeout=timeout) as response:
            return response.status, list(response.getheaders()), response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, list(exc.headers.items()), exc.read()
    except (urllib.error.URLError, OSError) as exc:
        return 502, [("Content-Type", "application/json")], (json.dumps({"error": f"upstream {url}: {exc}"}) + "\n").encode()


def scrape_target(url: str, timeout: float = 2.0) -> List[Sample]:
    instance = urllib.parse.urlsplit(url).netloc
    try:
        with NO_PROXY.open(url, timeout=timeout) as response:
            samples = parse_exposition(response.read().decode("utf-8", "replace"))
    except (urllib.error.URLError, OSError, InputError, ValueError):
        return [("up", {"instance": instance}, 0.0)]
    return [("up", {"instance": instance}, 1.0)] + [(name, dict(labels, instance=instance), value) for name, labels, value in samples]


class ProxyServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address: Tuple[str, int], stable: str, canary: Optional[str] = None, weight: int = 0, timeout: float = 10.0, scrape: Sequence[str] = ()):
        super().__init__(address, ProxyHandler)
        self.lock = threading.Lock()
        self.split = ExactShare()
        self.stable, self.canary, self.weight = "", None, 0
        self.timeout = timeout
        self.registry = Registry()
        self.db = TSDB()
        self.scrape = list(scrape)
        self.configure({"stable": stable, "canary": canary, "weight": weight})

    def configure(self, body: Any) -> Dict[str, Any]:
        if not isinstance(body, dict):
            raise InputError('send {"stable": URL, "canary": URL or null, "weight": 0-100}')
        stable, canary = body.get("stable"), body.get("canary") or None
        for name, url in (("stable", stable), ("canary", canary)):
            if (url is not None or name == "stable") and not (isinstance(url, str) and url.startswith(("http://", "https://"))):
                raise InputError(f"{name} must be an http(s) URL" + (" or null" if name == "canary" else ""))
        try:
            weight = int(body.get("weight", 0))
        except (TypeError, ValueError):
            raise InputError("weight must be an integer between 0 and 100")
        if not 0 <= weight <= 100:
            raise InputError("weight must be an integer between 0 and 100")
        if weight and not canary:
            raise InputError("a weight above 0 needs a canary URL")
        with self.lock:
            self.stable, self.canary, self.weight = stable.rstrip("/"), canary.rstrip("/") if canary else None, weight
        self.split.set(weight / 100)
        return self.config()

    def config(self) -> Dict[str, Any]:
        with self.lock:
            return {"stable": self.stable, "canary": self.canary, "weight": self.weight}

    def choose(self, always_canary: bool) -> Tuple[str, str]:
        with self.lock:
            stable, canary = self.stable, self.canary
        if canary and (always_canary or self.split.next()):
            return "canary", canary
        return "stable", stable

    def sample(self, now: float) -> None:
        samples = self.registry.samples()
        for url in self.scrape:
            samples += scrape_target(url)
        self.db.append(now, samples)


class ProxyHandler(QuietHandler):
    server: ProxyServer

    def handle_any(self) -> None:
        body = self.read_body()
        server = self.server
        if urllib.parse.urlsplit(self.path).path == "/admin/config":
            if self.command == "GET":
                return self.reply_json(200, server.config())
            try:
                return self.reply_json(200, server.configure(json.loads(body.decode() or "{}")))
            except (InputError, ValueError) as exc:
                return self.reply_json(400, {"error": str(exc)})
        if self.serve_observability(server.registry, server.db, body):
            return
        upstream, base = server.choose(self.headers.get("X-Canary", "") == "always")
        headers = {k: v for k, v in self.headers.items() if k.lower() not in HOP_BY_HOP}
        started = time.monotonic()
        code, response_headers, payload = forward(base + self.path, self.command, headers, body or None, server.timeout)
        lookup = {k.lower(): v for k, v in response_headers}
        server.registry.observe({"upstream": upstream, "version": lookup.get("x-version", "unknown")}, code, time.monotonic() - started)
        passed = {k: v for k, v in response_headers if k.lower() not in HOP_BY_HOP and k.lower() not in ("content-type", "date", "server")}
        passed["X-Upstream"] = upstream
        self.reply(code, payload, lookup.get("content-type", "application/octet-stream"), passed)


class MetricsServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address: Tuple[str, int], registry: Registry, db: TSDB):
        super().__init__(address, MetricsHandler)
        self.registry = registry
        self.db = db


class MetricsHandler(QuietHandler):
    server: MetricsServer

    def handle_any(self) -> None:
        body = self.read_body()
        if not self.serve_observability(self.server.registry, self.server.db, body):
            self.reply_json(404, {"error": "this server answers /metrics, /api/v1/query and /-/ready"})


class LoadGenerator:
    def __init__(self, url: str, rps: float, concurrency: int, registry: Registry, headers: Optional[Dict[str, str]] = None, timeout: float = 5.0):
        self.url = url
        self.rps = rps
        self.concurrency = max(1, concurrency)
        self.registry = registry
        self.headers = dict(headers or {})
        self.timeout = timeout
        self.stop = threading.Event()
        self.lock = threading.Lock()
        self.sent = 0

    def start(self) -> None:
        for index in range(self.concurrency):
            threading.Thread(target=self.worker, args=(index,), daemon=True).start()

    def worker(self, index: int) -> None:
        period = self.concurrency / self.rps
        next_at = time.monotonic() + period * index / self.concurrency
        while not self.stop.is_set():
            delay = next_at - time.monotonic()
            if delay > 0 and self.stop.wait(delay):
                return
            next_at = max(next_at + period, time.monotonic() - period)
            started = time.monotonic()
            code, version = self.hit()
            self.registry.observe({"version": version}, code, time.monotonic() - started)
            with self.lock:
                self.sent += 1

    def hit(self) -> Tuple[int, str]:
        request = urllib.request.Request(self.url, headers=self.headers)
        try:
            with NO_PROXY.open(request, timeout=self.timeout) as response:
                response.read()
                return response.status, response.headers.get("X-Version", "unknown")
        except urllib.error.HTTPError as exc:
            exc.read()
            return exc.code, exc.headers.get("X-Version", "unknown")
        except (urllib.error.URLError, OSError):
            return 0, "unknown"


def start_sampler(sample: Callable[[float], None], interval: float = 1.0) -> threading.Event:
    stop = threading.Event()

    def loop() -> None:
        sample(time.time())
        while not stop.wait(interval):
            sample(time.time())

    threading.Thread(target=loop, daemon=True).start()
    return stop


def serve_in_thread(server: ThreadingHTTPServer) -> threading.Thread:
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return thread


def base_url(server: ThreadingHTTPServer) -> str:
    host, port = server.server_address[:2]
    return f"http://{host}:{port}"


@dataclass
class Behaviour:
    error_rate: float = 0.0
    latency: float = 0.02


@dataclass
class SimResult:
    plan: str
    outcome: str
    at: Optional[float]
    new_requests: int
    new_errors: int
    total_requests: int
    message: str
    events: List[Dict[str, Any]]
    runs: List[Dict[str, Any]]

    def to_json(self) -> Dict[str, Any]:
        return dataclasses.asdict(self)


def default_args(spec: RolloutSpec) -> Dict[str, str]:
    found: Dict[str, str] = {}
    for step in spec.steps + ([spec.background] if spec.background else []):
        for key, value in step.args.items():
            found.setdefault(key, value)
    return found


def parse_plan(text: str, templates: Dict[str, AnalysisTemplate], args: Dict[str, str]) -> List[Step]:
    steps: List[Step] = []
    for raw in (part.strip() for part in text.split(",")):
        if not raw:
            continue
        if re.fullmatch(r"\d+%?", raw):
            weight = int(raw.rstrip("%"))
            if weight > 100:
                raise InputError(f"plan step {raw!r}: a weight is 0-100")
            steps.append(Step("setWeight", weight))
        elif raw == "pause":
            steps.append(Step("pause"))
        elif raw.startswith("pause="):
            steps.append(Step("pause", duration=parse_duration(raw[len("pause="):])))
        elif raw == "analysis" or raw.startswith("analysis="):
            names = raw[len("analysis="):].split("+") if "=" in raw else sorted(templates)
            if not names or not all(n in templates for n in names):
                raise InputError(f"plan step {raw!r}: analysis needs loaded templates (have: {', '.join(sorted(templates)) or 'none'})")
            steps.append(Step("analysis", templates=names, args=dict(args)))
        else:
            raise InputError(f"plan step {raw!r}: use N% (setWeight), pause, pause=30s, analysis or analysis=NAME+NAME")
    return steps


def simulate(spec: RolloutSpec, templates: Dict[str, AnalysisTemplate], rps: float = 50.0, new: Optional[Behaviour] = None, old: Optional[Behaviour] = None, duration: float = 600.0, tick: float = 1.0, by_replicas: bool = False, ready_delay: float = 0.0, plan: str = "") -> SimResult:
    new, old = new or Behaviour(0.2), old or Behaviour(0.0)
    label = default_args(spec).get("version", "canary")
    db, registry = TSDB(retention=duration + LOOKBACK), Registry()
    clock = {"now": 0.0}
    status = Status(phase=HEALTHY, ready=True, revision=1, stable="previous", stable_template={})
    target = SimTarget(spec.replicas, spec.max_surge, by_replicas, ready_delay)
    controller = Controller(spec, templates, target, lambda metric: local_query(db, metric.query, clock["now"]), status)
    split, errors_new, errors_old = ExactShare(), ExactShare(new.error_rate), ExactShare(old.error_rate)
    total = new_requests = new_errors = 0
    carry, outcome, at, t = 0.0, "running", None, 0.0
    while t <= duration + 1e-9:
        clock["now"] = t
        controller.reconcile(t)
        promoted = status.stable == spec.revision
        split.set((100.0 if promoted else target.live) / 100)
        carry += rps * tick
        count = int(carry + 1e-9)
        carry -= count
        served = {"new": [0, 0], "old": [0, 0]}
        for _ in range(count):
            side = "new" if split.next() else "old"
            failed = (errors_new if side == "new" else errors_old).next()
            served[side][1 if failed else 0] += 1
        for side, behaviour, version in (("new", new, label), ("old", old, "stable")):
            registry.observe({"version": version}, 200, behaviour.latency, served[side][0])
            registry.observe({"version": version}, 500, behaviour.latency, served[side][1])
        total += count
        new_requests += sum(served["new"])
        new_errors += served["new"][1]
        db.append(t, registry.samples())
        if status.phase == DEGRADED:
            outcome, at = "aborted", t
            break
        if promoted and outcome == "running":
            outcome, at = "promoted", t
        t += tick
    if outcome == "running" and status.phase == PAUSED:
        outcome = "paused"
    return SimResult(plan or ",".join(s.compact for s in spec.steps), outcome, at, new_requests, new_errors, total, status.message, status.events, status.runs)


def read_json(path: str) -> Any:
    try:
        if path == "-":
            return json.load(sys.stdin)
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)
    except OSError as exc:
        raise InputError(f"cannot read {path}: {exc.strerror}")
    except ValueError as exc:
        raise InputError(f"{path} is not JSON ({exc}); minirollout reads JSON manifests, which kubectl applies as well")


def documents(data: Any) -> List[Dict[str, Any]]:
    if isinstance(data, list):
        items = data
    elif isinstance(data, dict) and data.get("kind") == "List":
        items = data.get("items") or []
    else:
        items = [data]
    return [item for item in items if isinstance(item, dict)]


def check_templates(spec: RolloutSpec, templates: Dict[str, AnalysisTemplate]) -> None:
    steps = [(f"step {i + 1}", s) for i, s in enumerate(spec.steps) if s.kind == "analysis"]
    if spec.background:
        steps.append(("canary.analysis", spec.background))
    for where, step in steps:
        for name in step.templates:
            if name not in templates:
                raise InputError(f"rollout {spec.name} {where} uses analysis template {name!r}, which is not loaded (pass --analysis FILE; loaded: {', '.join(sorted(templates)) or 'none'})")
            templates[name].resolve(step.args)


def load_inputs(rollout_path: str, analysis_paths: Sequence[str]) -> Tuple[RolloutSpec, Dict[str, AnalysisTemplate]]:
    docs = documents(read_json(rollout_path))
    rollouts = [d for d in docs if d.get("kind") == "Rollout"]
    if len(rollouts) != 1:
        raise InputError(f"{rollout_path}: expected exactly one Rollout, found {len(rollouts)}")
    spec = parse_rollout(rollouts[0])
    templates: Dict[str, AnalysisTemplate] = {}
    extra = [d for path in analysis_paths for d in documents(read_json(path))]
    for doc in [d for d in docs if d.get("kind") in TEMPLATE_KINDS] + extra:
        template = parse_template(doc)
        templates[template.name] = template
    check_templates(spec, templates)
    return spec, templates


def load_state(path: str) -> Status:
    if not os.path.exists(path):
        return Status()
    data = read_json(path)
    if not isinstance(data, dict):
        raise InputError(f"{path} is not a minirollout state file")
    return Status.from_json(data)


def save_state(path: str, status: Status) -> None:
    folder = os.path.dirname(os.path.abspath(path))
    os.makedirs(folder, exist_ok=True)
    temporary = path + ".tmp"
    with open(temporary, "w", encoding="utf-8") as handle:
        json.dump(status.to_json(), handle, indent=2)
    os.replace(temporary, path)


def apply_requests(controller: Controller, state_path: str, now: float) -> None:
    for kind in ("abort", "promote-full", "promote"):
        marker = f"{state_path}.{kind}"
        if not os.path.exists(marker):
            continue
        os.remove(marker)
        if kind == "abort":
            controller.request_abort(now)
        elif kind == "promote-full":
            controller.status.promote_full = True
        else:
            controller.status.skip_pause = True


def render_plan(spec: RolloutSpec, templates: Dict[str, AnalysisTemplate], replicas: int) -> str:
    lines = [f"rollout {spec.name}: {spec.strategy}, {replicas} replicas, maxSurge {spec.max_surge}, revision {spec.revision}"]
    lines.append(f"{'step':<5} {'action':<44} {'new pods':>8} {'stable pods':>11} {'by pods':>8} {'exact':>6}")
    weight = 0
    for index, step in enumerate(spec.steps, 1):
        if step.kind == "setWeight":
            weight = step.weight
        elif step.kind == "preview":
            weight = 0
        canary, stable = canary_counts(replicas, weight, spec.max_surge) if step.kind != "preview" else (replicas, replicas)
        by_pods = effective_weight(canary, stable) if step.kind != "preview" else 0.0
        lines.append(f"{index:<5} {step.label[:44]:<44} {canary:>8} {stable:>11} {format_number(round(by_pods, 1)) + '%':>8} {str(weight) + '%':>6}")
        if step.kind == "analysis":
            for template in step.templates:
                for metric in templates[template].resolve(step.args):
                    lines.append(f"        {metric.describe()}")
                    lines.append(f"        query: {metric.query}")
    lines.append(f"{'end':<5} {'promote: new version becomes stable':<44} {replicas:>8} {0:>11} {'100%':>8} {'100%':>6}")
    if spec.background:
        lines.append(f"background analysis from step {spec.starting_step + 1}: {', '.join(spec.background.templates)}")
    return "\n".join(lines)


def render_status(status: Status, with_events: bool = True) -> str:
    lines = [f"rollout {status.rollout or '?'}  revision {status.revision}  phase {status.phase or '-'}"]
    if status.message:
        lines.append(f"message  {status.message}")
    lines.append(f"stable {status.stable or '-'}  canary {status.canary or '-'}  step {status.step}/{status.steps}  weight {status.weight}%")
    for run in status.runs:
        lines.append(f"analysis {run['name']}: {run['phase']}" + (f" ({run['message']})" if run["message"] else ""))
        for metric in run["metrics"]:
            values = ", ".join(f"{show_value(m['value'])} {m['phase']}" for m in metric["measurements"]) or "no measurements"
            lines.append(f"  {metric['name']}: {values}")
    start = status.events[0]["at"] if status.events else 0.0
    for event in status.events if with_events else []:
        lines.append(f"  +{event['at'] - start:7.1f}s  {event['reason']:<22} {event['message']}")
    return "\n".join(lines)


def command_plan(args: argparse.Namespace) -> int:
    spec, templates = load_inputs(args.rollout, args.analysis)
    replicas = args.replicas or spec.replicas
    if args.json:
        rows = []
        weight = 0
        for step in spec.steps:
            weight = step.weight if step.kind == "setWeight" else (0 if step.kind == "preview" else weight)
            canary, stable = canary_counts(replicas, weight, spec.max_surge)
            rows.append({"step": step.label, "weight": weight, "new_pods": canary, "stable_pods": stable, "weight_by_pods": round(effective_weight(canary, stable), 2)})
        print(json.dumps({"rollout": spec.name, "strategy": spec.strategy, "replicas": replicas, "max_surge": spec.max_surge, "revision": spec.revision, "steps": rows}, indent=2))
    else:
        print(render_plan(spec, templates, replicas))
    return 0


def command_run(args: argparse.Namespace) -> int:
    spec, templates = load_inputs(args.rollout, args.analysis)
    if args.target == "proxy":
        if not args.proxy or not args.canary_url:
            raise InputError("--target proxy needs --proxy URL (the minirollout proxy) and --canary-url URL (the new version)")
        target: Any = ProxyTarget(args.proxy, args.canary_url)
    else:
        target = KubeTarget(spec, args.namespace, args.context)
    override = args.prometheus_url
    controller = Controller(spec, templates, target, lambda metric: prometheus_query(override or metric.address, metric.query), load_state(args.state))
    started = time.time()
    quiet = args.json

    def say(line: str) -> None:
        if not quiet:
            print(line, flush=True)

    controller.on_event = lambda e: say(f"+{max(0.0, e['at'] - started):6.1f}s  {e['reason']:<22} {e['message']}")
    controller.on_measurement = lambda run, metric, m: say(f"+{max(0.0, m.at - started):6.1f}s  {'measurement':<22} {run} {metric} = {show_value(m.value)} {m.phase}" + (f" ({m.message})" if m.message else ""))
    deadline = started + args.timeout
    code = 1
    while True:
        now = time.time()
        apply_requests(controller, args.state, now)
        status = controller.reconcile(now)
        save_state(args.state, status)
        if status.phase == HEALTHY and not status.canary and status.ready:
            code = 0
            break
        if status.phase == DEGRADED and status.ready:
            code = 1
            break
        if controller.waiting_for_human():
            say(f"paused ({status.message}): run `minirollout promote --state {args.state}` or `minirollout abort --state {args.state}`, then run again")
            code = EXIT_PAUSED
            break
        if now >= deadline:
            say(f"timed out after {format_seconds(args.timeout)} in phase {status.phase}: {status.message}")
            code = 1
            break
        time.sleep(args.tick)
    if args.json:
        print(json.dumps(controller.status.to_json(), indent=2))
    else:
        print(render_status(controller.status, with_events=False))
    return code


def command_request(args: argparse.Namespace) -> int:
    kind = "abort" if args.command == "abort" else ("promote-full" if args.full else "promote")
    with open(f"{args.state}.{kind}", "w", encoding="utf-8") as handle:
        handle.write(kind + "\n")
    print(f"{kind} requested for {args.state}: a running `minirollout run` applies it on its next tick, otherwise the next run does")
    return 0


def command_status(args: argparse.Namespace) -> int:
    if not os.path.exists(args.state):
        raise InputError(f"{args.state} does not exist; `minirollout run` writes it")
    status = load_state(args.state)
    print(json.dumps(status.to_json(), indent=2) if args.json else render_status(status))
    return 0


def command_simulate(args: argparse.Namespace) -> int:
    spec, templates = load_inputs(args.rollout, args.analysis)
    if args.replicas:
        spec = dataclasses.replace(spec, replicas=args.replicas, max_surge=resolve_surge("25%", args.replicas, spec.name))
    plans = [("", spec)]
    for text in args.plan:
        steps = parse_plan(text, templates, default_args(spec))
        plans.append((text, dataclasses.replace(spec, steps=steps, background=None)))
    new = Behaviour(args.error_rate, args.latency_ms / 1000)
    old = Behaviour(0.0, args.stable_latency_ms / 1000)
    results = [simulate(s, templates, args.rps, new, old, args.duration, 1.0, bool(args.replicas), args.ready_delay, text) for text, s in plans]
    if args.json:
        print(json.dumps([r.to_json() for r in results], indent=2))
        return 0
    mode = f"{spec.replicas} pods, weights rounded to whole pods" if args.replicas else "exact weights"
    print(f"{format_number(args.rps)} req/s for up to {format_seconds(args.duration)}; new version fails {format_number(args.error_rate * 100)}% at {format_number(args.latency_ms)}ms; {mode}")
    print(f"{'plan':<44} {'outcome':<9} {'at':>6} {'to new':>8} {'errors':>7} {'share':>7}")
    for r in results:
        share = 100.0 * r.new_requests / r.total_requests if r.total_requests else 0.0
        print(f"{r.plan[:44]:<44} {r.outcome:<9} {format_seconds(r.at) if r.at is not None else '-':>6} {r.new_requests:>8} {r.new_errors:>7} {format_number(round(share, 1)) + '%':>7}")
    return 0


def command_query(args: argparse.Namespace) -> int:
    try:
        data = prometheus_request(args.url, args.expression)
    except MeasurementError as exc:
        print(f"minirollout: {exc}", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(data, indent=2))
    elif data.get("resultType") == "scalar":
        print(data["result"][1])
    else:
        for item in data.get("result") or []:
            print(f"{render_labels(item['metric']) or '{}'} {item['value'][1]}")
    return 0


def parse_listen(text: str) -> Tuple[str, int]:
    host, _, port = text.rpartition(":")
    try:
        return host or "127.0.0.1", int(port)
    except ValueError:
        raise InputError(f"--listen wants HOST:PORT, got {text!r}")


def serve_until_interrupted(server: ThreadingHTTPServer, label: str) -> int:
    print(f"{label} listening on {base_url(server)}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


def command_app(args: argparse.Namespace) -> int:
    server = AppServer((args.host, args.port), args.version, args.error_rate, args.latency_ms / 1000)
    return serve_until_interrupted(server, f"app {args.version} (error rate {format_number(args.error_rate)}, latency {format_number(args.latency_ms)}ms)")


def command_proxy(args: argparse.Namespace) -> int:
    server = ProxyServer(parse_listen(args.listen), args.stable, args.canary, args.weight, args.timeout, args.scrape)
    start_sampler(server.sample, 1.0)
    return serve_until_interrupted(server, "proxy")


def command_load(args: argparse.Namespace) -> int:
    headers = {}
    for raw in args.header:
        key, _, value = raw.partition(":")
        headers[key.strip()] = value.strip()
    registry, db = Registry(), TSDB()
    generator = LoadGenerator(args.url, args.rps, args.concurrency, registry, headers)
    start_sampler(lambda now: db.append(now, registry.samples()), 1.0)
    if args.listen:
        server = MetricsServer(parse_listen(args.listen), registry, db)
        serve_in_thread(server)
        print(f"load metrics listening on {base_url(server)}", flush=True)
    print(f"load: {format_number(args.rps)} req/s to {args.url} with {generator.concurrency} workers, one connection per request", flush=True)
    generator.start()
    try:
        if args.duration:
            time.sleep(args.duration)
        else:
            while True:
                time.sleep(3600)
    except KeyboardInterrupt:
        pass
    generator.stop.set()
    counts: Dict[str, float] = {}
    for name, labels, value in registry.samples():
        if name == REQUESTS:
            key = f"{labels.get('version')} {labels.get('code')}"
            counts[key] = counts.get(key, 0) + value
    print(json.dumps({"sent": generator.sent, "by_version_and_code": counts}, sort_keys=True), flush=True)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="minirollout", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    plan = sub.add_parser("plan", help="print the steps of a Rollout with pod counts and traffic share")
    plan.add_argument("rollout", help="Rollout JSON (may also contain AnalysisTemplates in a List)")
    plan.add_argument("--analysis", action="append", default=[], help="AnalysisTemplate JSON file (repeatable)")
    plan.add_argument("--replicas", type=int, help="override spec.replicas to see how rounding to whole pods changes the share")
    plan.add_argument("--json", action="store_true")

    run = sub.add_parser("run", help="drive the rollout: exit 0 when Healthy, 1 when aborted, 3 when paused for a human")
    run.add_argument("rollout")
    run.add_argument("--analysis", action="append", default=[])
    run.add_argument("--target", choices=("proxy", "kube"), required=True)
    run.add_argument("--state", required=True, help="state file, the equivalent of the Rollout's status")
    run.add_argument("--proxy", help="proxy target: base URL of `minirollout proxy`")
    run.add_argument("--canary-url", help="proxy target: base URL of the new version")
    run.add_argument("--namespace", default="default", help="kube target: namespace of the NAME-stable and NAME-canary Deployments")
    run.add_argument("--context", help="kube target: kubectl context")
    run.add_argument("--prometheus-url", help="query this Prometheus-compatible API instead of each metric's address")
    run.add_argument("--tick", type=float, default=1.0, help="seconds between reconciles (default 1)")
    run.add_argument("--timeout", type=float, default=600.0)
    run.add_argument("--json", action="store_true")

    for name, text in (("promote", "skip the current pause or accept an inconclusive analysis"), ("abort", "abort the rollout in progress")):
        request = sub.add_parser(name, help=text)
        request.add_argument("--state", required=True)
        if name == "promote":
            request.add_argument("--full", action="store_true", help="skip every remaining step")

    status = sub.add_parser("status", help="print a state file")
    status.add_argument("--state", required=True)
    status.add_argument("--json", action="store_true")

    simulate_parser = sub.add_parser("simulate", help="replay a rollout against a bad version and count who saw it")
    simulate_parser.add_argument("rollout")
    simulate_parser.add_argument("--analysis", action="append", default=[])
    simulate_parser.add_argument("--plan", action="append", default=[], help='extra step plans to compare, e.g. "5%%,analysis,25%%,analysis" or "100"')
    simulate_parser.add_argument("--rps", type=float, default=50.0)
    simulate_parser.add_argument("--error-rate", type=float, default=0.2, help="share of the new version's requests that fail (default 0.2)")
    simulate_parser.add_argument("--latency-ms", type=float, default=20.0, help="latency of the new version")
    simulate_parser.add_argument("--stable-latency-ms", type=float, default=20.0)
    simulate_parser.add_argument("--replicas", type=int, help="round weights to whole pods of this many replicas (basic canary without a traffic router)")
    simulate_parser.add_argument("--ready-delay", type=float, default=0.0, help="seconds a new pod takes to become ready")
    simulate_parser.add_argument("--duration", type=float, default=600.0)
    simulate_parser.add_argument("--json", action="store_true")

    query = sub.add_parser("query", help="run a PromQL query against a Prometheus-compatible API")
    query.add_argument("url")
    query.add_argument("expression")
    query.add_argument("--json", action="store_true")

    app = sub.add_parser("app", help="the demo app: answers with X-Version, fails a configured share of requests, serves /metrics")
    app.add_argument("--version", default=os.environ.get("APP_VERSION", "v1"))
    app.add_argument("--host", default="127.0.0.1")
    app.add_argument("--port", type=int, default=8080)
    app.add_argument("--error-rate", type=float, default=float(os.environ.get("ERROR_RATE") or 0))
    app.add_argument("--latency-ms", type=float, default=float(os.environ.get("LATENCY_MS") or 0))

    proxy = sub.add_parser("proxy", help="weighted reverse proxy with /admin/config, /metrics and /api/v1/query")
    proxy.add_argument("--stable", required=True)
    proxy.add_argument("--canary")
    proxy.add_argument("--weight", type=int, default=0)
    proxy.add_argument("--listen", default="127.0.0.1:8000")
    proxy.add_argument("--scrape", action="append", default=[], help="also scrape this /metrics URL into the query API (repeatable)")
    proxy.add_argument("--timeout", type=float, default=10.0, help="upstream timeout in seconds")

    load = sub.add_parser("load", help="send traffic, record what came back, serve /metrics and /api/v1/query")
    load.add_argument("--url", required=True)
    load.add_argument("--rps", type=float, default=40.0)
    load.add_argument("--concurrency", type=int, default=8)
    load.add_argument("--listen", help="HOST:PORT for /metrics and /api/v1/query")
    load.add_argument("--duration", type=float, default=0.0, help="stop after this many seconds (default: run until interrupted)")
    load.add_argument("--header", action="append", default=[], help="extra request header, e.g. 'X-Canary: always'")
    return parser


COMMANDS: Dict[str, Callable[[argparse.Namespace], int]] = {
    "plan": command_plan,
    "run": command_run,
    "promote": command_request,
    "abort": command_request,
    "status": command_status,
    "simulate": command_simulate,
    "query": command_query,
    "app": command_app,
    "proxy": command_proxy,
    "load": command_load,
}


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(list(sys.argv[1:] if argv is None else argv))
    try:
        return COMMANDS[args.command](args)
    except (InputError, TargetError) as exc:
        print(f"minirollout: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
