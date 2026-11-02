"""minitsdb: parse the Prometheus text format and OpenMetrics, scrape targets with relabelling, honor_labels and staleness markers, keep samples in an in-memory TSDB with a write-ahead log and Gorilla-compressed chunks, and report cardinality."""
from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import math
import os
import re
import signal
import struct
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Set, Tuple

Labels = Tuple[Tuple[str, str], ...]
Row = Tuple[Labels, int, int]
Fetch = Callable[[str, float], Tuple[str, bytes]]

TEXT_FORMAT = "text"
OPENMETRICS = "openmetrics"
METRIC_NAME = re.compile(r"[a-zA-Z_:][a-zA-Z0-9_:]*")
LABEL_NAME = re.compile(r"[a-zA-Z_][a-zA-Z0-9_]*")
FLOAT_TEXT = re.compile(r"[+-]?(?:(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?|[iI][nN][fF](?:[iI][nN][iI][tT][yY])?)|[nN][aA][nN]")
INT_TEXT = re.compile(r"[+-]?\d+")
METADATA = re.compile(r"#[ \t]+(HELP|TYPE|UNIT)[ \t]+(\S+)(?:[ \t](.*))?$")
DURATION_TEXT = re.compile(r"(?:(\d+)y)?(?:(\d+)w)?(?:(\d+)d)?(?:(\d+)h)?(?:(\d+)m)?(?:(\d+)s)?(?:(\d+)ms)?")
DURATION_UNITS = (("y", 365 * 86400000), ("w", 7 * 86400000), ("d", 86400000), ("h", 3600000), ("m", 60000), ("s", 1000), ("ms", 1))
TEMPLATE_NAME = re.compile(r"[A-Za-z0-9_]+")
CAMEL_CASE = re.compile(r"[a-z][A-Z]")
UNBOUNDED_LABEL = re.compile(r"(^|_)(id|uuid|guid|email|session|token|ip|url|uri)$")
LABEL_ESCAPES = {"\\": "\\", '"': '"', "n": "\n"}

TEXT_TYPES = ("counter", "gauge", "histogram", "summary", "untyped")
OPENMETRICS_TYPES = ("counter", "gauge", "histogram", "gaugehistogram", "stateset", "info", "summary", "unknown")
TEXT_SUFFIXES = {"histogram": ("_bucket", "_sum", "_count"), "summary": ("_sum", "_count")}
OPENMETRICS_SUFFIXES = {
    "counter": ("_total", "_created"),
    "histogram": ("_bucket", "_sum", "_count", "_created"),
    "gaugehistogram": ("_bucket", "_gsum", "_gcount"),
    "summary": ("_sum", "_count", "_created"),
    "info": ("_info",),
}
ALL_SUFFIXES = ("_bucket", "_count", "_sum", "_created", "_total", "_info", "_gcount", "_gsum")
EXACT_NAME_TYPES = ("gauge", "unknown", "stateset", "summary")

UNITS = {
    "amperes": "amperes", "bytes": "bytes", "celsius": "celsius", "grams": "grams", "joules": "joules",
    "kelvin": "kelvin", "meters": "meters", "metres": "metres", "seconds": "seconds", "volts": "volts",
    "minutes": "seconds", "hours": "seconds", "days": "seconds", "weeks": "seconds",
    "kelvins": "kelvin", "fahrenheit": "celsius", "rankine": "celsius",
    "inches": "meters", "yards": "meters", "miles": "meters", "bits": "bytes",
    "calories": "joules", "pounds": "grams", "ounces": "grams",
}
UNIT_PREFIXES = ("pico", "nano", "micro", "milli", "centi", "deci", "deca", "hecto", "kilo", "kibi", "mega", "mibi", "giga", "gibi", "tera", "tebi", "peta", "pebi")
UNIT_ABBREVIATIONS = ("s", "ms", "us", "ns", "sec", "b", "kb", "mb", "gb", "tb", "pb", "m", "h", "d")

STALE_NAN = 0x7FF0000000000002
NORMAL_NAN = 0x7FF8000000000001
MASK64 = (1 << 64) - 1
SAMPLES_PER_CHUNK = 120
DOD_BUCKETS = ((0b10, 2, 14), (0b110, 3, 17), (0b1110, 4, 20))
LOOKBACK_MS = 5 * 60 * 1000

PAGE_SIZE = 32 * 1024
RECORD_HEADER = 7
SEGMENT_SIZE = 128 * 1024 * 1024
REC_PAGE_TERM, REC_FULL, REC_FIRST, REC_MIDDLE, REC_LAST = 0, 1, 2, 3, 4
RECORD_SERIES, RECORD_SAMPLES = 1, 2

TEXT_CONTENT_TYPE = "text/plain; version=0.0.4; charset=utf-8"
ACCEPT = "application/openmetrics-text;version=1.0.0;q=0.5,text/plain;version=0.0.4;q=0.4,*/*;q=0.1"
FALLBACK_PROTOCOLS = {"PrometheusText0.0.4": TEXT_FORMAT, "PrometheusText1.0.0": TEXT_FORMAT, "OpenMetricsText0.0.1": OPENMETRICS, "OpenMetricsText1.0.0": OPENMETRICS}
REPORT_METRICS = ("up", "scrape_duration_seconds", "scrape_samples_scraped", "scrape_samples_post_metric_relabeling", "scrape_series_added")
RELABEL_ACTIONS = ("replace", "keep", "drop", "keepequal", "dropequal", "hashmod", "labelmap", "labeldrop", "labelkeep", "lowercase", "uppercase")
RELABEL_KEYS = {"action", "source_labels", "separator", "regex", "target_label", "replacement", "modulus"}
SCRAPE_KEYS = {
    "job_name", "scrape_interval", "scrape_timeout", "metrics_path", "scheme", "honor_labels", "honor_timestamps",
    "params", "static_configs", "relabel_configs", "metric_relabel_configs", "fallback_scrape_protocol",
}

DEMO_BUCKETS = ("0.05", "0.1", "0.25", "0.5", "1", "+Inf")
DEMO_LATENCY = (("/", (120, 180, 195, 199, 200, 200), 9.5), ("/api", (10, 40, 80, 95, 99, 100), 31.2), ("/login", (0, 2, 10, 30, 48, 50), 27.4))
DEMO_QUEUES = (("default", 4), ("batch", 17), ('email "bulk"', 0))
DEMO_USERS = 25
DEMO_POOLS = ("small", "medium", "large", "huge")


class InputError(ValueError):
    pass


class ParseError(InputError):
    def __init__(self, line: int, message: str):
        super().__init__(f"line {line}: {message}")
        self.line = line


class ScrapeError(Exception):
    pass


def complain(where: Optional[int], message: str) -> InputError:
    return ParseError(where, message) if where is not None else InputError(message)


def float_bits(value: float) -> int:
    return struct.unpack(">Q", struct.pack(">d", value))[0]


def bits_float(bits: int) -> float:
    return struct.unpack(">d", struct.pack(">Q", bits))[0]


def value_bits(value: float) -> int:
    return NORMAL_NAN if math.isnan(value) else float_bits(value)


def freeze(labels: Dict[str, str]) -> Labels:
    return tuple(sorted((k, v) for k, v in labels.items() if v != ""))


def escape_value(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def render_series(labels: Any) -> str:
    items = dict(labels)
    name = items.pop("__name__", "")
    inner = ", ".join(f'{k}="{escape_value(v)}"' for k, v in sorted(items.items()))
    return f"{name}{{{inner}}}" if inner or not name else name


def format_value(value: float) -> str:
    if math.isnan(value):
        return "NaN"
    if math.isinf(value):
        return "+Inf" if value > 0 else "-Inf"
    text = format(Decimal(repr(value)), "f")
    return text.rstrip("0").rstrip(".") if "." in text else text


def go_shortest_g(value: float) -> str:
    if value == 0:
        return "0"
    sign = "-" if value < 0 else ""
    parts = Decimal(repr(abs(value))).normalize().as_tuple()
    digits = "".join(str(d) for d in parts.digits)
    point = len(digits) + int(parts.exponent)
    exponent = point - 1
    if exponent < -4 or exponent >= 6:
        mantissa = digits[0] + ("." + digits[1:] if len(digits) > 1 else "")
        return f"{sign}{mantissa}e{'-' if exponent < 0 else '+'}{abs(exponent):02d}"
    if point <= 0:
        return f"{sign}0.{'0' * -point}{digits}"
    if point >= len(digits):
        return f"{sign}{digits}{'0' * (point - len(digits))}"
    return f"{sign}{digits[:point]}.{digits[point:]}"


def format_openmetrics_float(value: float) -> str:
    if math.isnan(value):
        return "NaN"
    if math.isinf(value):
        return "+Inf" if value > 0 else "-Inf"
    if value in (1, 0, -1):
        return {1: "1.0", 0: "0.0", -1: "-1.0"}[int(value)]
    text = go_shortest_g(value)
    return text if ("e" in text or "." in text) else text + ".0"


def parse_duration(text: Any) -> int:
    raw = str(text).strip()
    if raw == "0":
        return 0
    match = DURATION_TEXT.fullmatch(raw)
    if not raw or not match or not any(match.groups()):
        raise InputError(f"not a Prometheus duration: {raw!r} (examples: 15s, 1m, 1h30m, 500ms, 15d)")
    return sum(int(part) * unit for part, (_, unit) in zip(match.groups(), DURATION_UNITS) if part)


def format_duration(ms: int) -> str:
    if ms == 0:
        return "0s"
    out = ""
    for unit, size in DURATION_UNITS:
        if unit in ("y", "w") and ms % size:
            continue
        if ms >= size:
            out += f"{ms // size}{unit}"
            ms %= size
    return out


@dataclass
class Exemplar:
    labels: Dict[str, str]
    value: float
    timestamp: Optional[float] = None


@dataclass
class Sample:
    name: str
    labels: Dict[str, str]
    value: float
    timestamp_ms: Optional[int] = None
    line: int = 0
    exemplar: Optional[Exemplar] = None


@dataclass
class Family:
    name: str
    type: str
    help: Optional[str] = None
    unit: Optional[str] = None
    declared: bool = False
    samples: List[Sample] = field(default_factory=list)


@dataclass
class Exposition:
    format: str
    families: List[Family]

    def samples(self) -> List[Sample]:
        return [sample for family in self.families for sample in family.samples]


def skip_spaces(text: str, pos: int) -> int:
    while pos < len(text) and text[pos] in " \t":
        pos += 1
    return pos


def read_quoted(text: str, pos: int, where: Optional[int], strict: bool) -> Tuple[str, int]:
    out: List[str] = []
    pos += 1
    while pos < len(text):
        char = text[pos]
        if char == '"':
            return "".join(out), pos + 1
        if char == "\\" and pos + 1 < len(text):
            escaped = text[pos + 1]
            if escaped in LABEL_ESCAPES:
                out.append(LABEL_ESCAPES[escaped])
                pos += 2
                continue
            if strict:
                raise complain(where, f"invalid escape \\{escaped} in a label value (only \\\\, \\\" and \\n are allowed)")
        out.append(char)
        pos += 1
    raise complain(where, "unterminated label value: a closing quote is missing")


def parse_label_block(text: str, pos: int, where: Optional[int], operators: bool = False, strict: bool = False) -> Tuple[List[Tuple[str, str, str]], int]:
    items: List[Tuple[str, str, str]] = []
    pos += 1
    while True:
        pos = skip_spaces(text, pos)
        if pos >= len(text):
            raise complain(where, "unterminated label set: expected }")
        if text[pos] == "}":
            return items, pos + 1
        match = LABEL_NAME.match(text, pos)
        if not match:
            raise complain(where, f"expected a label name at {text[pos:pos + 12]!r}")
        name = match.group()
        pos = skip_spaces(text, match.end())
        op = ""
        for candidate in (("=~", "!~", "!=", "=") if operators else ("=",)):
            if text.startswith(candidate, pos):
                op = candidate
                break
        if not op:
            raise complain(where, f"expected {'a matcher (=, !=, =~, !~)' if operators else '='} after label name {name}")
        pos = skip_spaces(text, pos + len(op))
        if pos >= len(text) or text[pos] != '"':
            raise complain(where, f"expected a quoted value for label {name}")
        value, pos = read_quoted(text, pos, where, strict)
        items.append((name, op, value))
        pos = skip_spaces(text, pos)
        if pos < len(text) and text[pos] == ",":
            pos += 1
        elif pos >= len(text) or text[pos] != "}":
            raise complain(where, f"expected , or }} after the value of label {name}")


def parse_float(text: str, line: int) -> float:
    if not FLOAT_TEXT.fullmatch(text):
        raise ParseError(line, f"invalid value {text!r}: expected a float, +Inf, -Inf or NaN")
    return float(text)


def parse_timestamp(text: str, line: int, openmetrics: bool) -> int:
    if openmetrics:
        seconds = parse_float(text, line)
        if math.isnan(seconds) or math.isinf(seconds):
            raise ParseError(line, f"invalid timestamp {text!r}")
        return int(round(seconds * 1000))
    if not INT_TEXT.fullmatch(text):
        raise ParseError(line, f"invalid timestamp {text!r}: the text format uses integer milliseconds")
    return int(text)


def unescape_help(text: str, openmetrics: bool) -> str:
    escapes = {"\\": "\\", "n": "\n", '"': '"'} if openmetrics else {"\\": "\\", "n": "\n"}
    out: List[str] = []
    pos = 0
    while pos < len(text):
        if text[pos] == "\\" and pos + 1 < len(text) and text[pos + 1] in escapes:
            out.append(escapes[text[pos + 1]])
            pos += 2
        else:
            out.append(text[pos])
            pos += 1
    return "".join(out)


def parse_exemplar(text: str, line: int) -> Exemplar:
    text = text.strip()
    if not text.startswith("{"):
        raise ParseError(line, "an exemplar starts with a label set: # {trace_id=\"...\"} VALUE [TIMESTAMP]")
    items, pos = parse_label_block(text, 0, line, strict=True)
    labels = {name: value for name, _, value in items}
    if sum(len(k) + len(v) for k, v in labels.items()) > 128:
        raise ParseError(line, "exemplar labels are longer than 128 characters")
    fields = text[pos:].split()
    if not fields or len(fields) > 2:
        raise ParseError(line, "an exemplar needs a value and an optional timestamp")
    stamp = parse_float(fields[1], line) if len(fields) == 2 else None
    return Exemplar(labels, parse_float(fields[0], line), stamp)


def parse_sample_line(line: str, number: int, openmetrics: bool) -> Sample:
    match = METRIC_NAME.match(line)
    if not match:
        raise ParseError(number, f"expected a metric name at the start of {line[:40]!r}")
    name, pos = match.group(), match.end()
    labels: Dict[str, str] = {}
    if pos < len(line) and line[pos] == "{":
        items, pos = parse_label_block(line, pos, number, strict=openmetrics)
        for label, _, value in items:
            if label in labels:
                raise ParseError(number, f"duplicate label name {label} in {name}")
            if label == "__name__":
                raise ParseError(number, "label name __name__ is reserved")
            labels[label] = value
    rest = line[pos:]
    exemplar = None
    if openmetrics and " # " in rest:
        rest, exemplar_text = rest.split(" # ", 1)
        exemplar = parse_exemplar(exemplar_text, number)
    if rest[:1] not in (" ", "\t"):
        raise ParseError(number, f"expected a space and a value after {name}")
    fields = rest.split()
    if not fields:
        raise ParseError(number, f"{name} has no value")
    if len(fields) > 2:
        hint = " (exemplars need the OpenMetrics format)" if "#" in fields[1:] else ""
        raise ParseError(number, f"unexpected {fields[2]!r} after the timestamp{hint}")
    value = parse_float(fields[0], number)
    stamp = parse_timestamp(fields[1], number, openmetrics) if len(fields) == 2 else None
    return Sample(name, labels, value, stamp, number, exemplar)


def resolve_family(name: str, families: Dict[str, Family], openmetrics: bool) -> Optional[Family]:
    if name in families:
        return families[name]
    table = OPENMETRICS_SUFFIXES if openmetrics else TEXT_SUFFIXES
    for suffix in ALL_SUFFIXES:
        if name.endswith(suffix):
            base = families.get(name[: -len(suffix)])
            if base is not None and suffix in table.get(base.type, ()):
                return base
    return None


def normalise_labels(family: Family, labels: Dict[str, str]) -> Dict[str, str]:
    key = "le" if family.type == "histogram" else "quantile" if family.type == "summary" else ""
    if key and key in labels:
        try:
            labels[key] = format_openmetrics_float(float(labels[key]))
        except ValueError:
            pass
    return labels


def parse_exposition(text: str, fmt: str = TEXT_FORMAT) -> Exposition:
    openmetrics = fmt == OPENMETRICS
    families: Dict[str, Family] = {}
    closed: Set[str] = set()
    current: Optional[str] = None
    saw_eof = False
    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()

    def enter(name: str, number: int) -> None:
        nonlocal current
        if not openmetrics or current == name:
            current = name
            return
        if name in closed:
            raise ParseError(number, f"metric family {name} appears twice: OpenMetrics families must be contiguous")
        if current is not None:
            closed.add(current)
        current = name

    for number, line in enumerate(lines, 1):
        if saw_eof:
            raise ParseError(number, "content after # EOF")
        if not line.strip():
            if openmetrics:
                raise ParseError(number, "empty lines are not allowed in OpenMetrics")
            continue
        if line.startswith("#"):
            if openmetrics and line.rstrip() == "# EOF":
                saw_eof = True
                continue
            meta = METADATA.match(line)
            if meta is None or (meta.group(1) == "UNIT" and not openmetrics):
                if openmetrics:
                    raise ParseError(number, "OpenMetrics allows only # HELP, # TYPE, # UNIT and # EOF lines, not free comments")
                continue
            kind, name, rest = meta.group(1), meta.group(2), meta.group(3) or ""
            if not METRIC_NAME.fullmatch(name):
                raise ParseError(number, f"invalid metric name {name!r} in # {kind}")
            family = families.get(name)
            if family is None:
                family = families[name] = Family(name, "unknown" if openmetrics else "untyped")
            enter(name, number)
            if kind == "HELP":
                if family.help is not None:
                    raise ParseError(number, f"second HELP line for {name}")
                family.help = unescape_help(rest, openmetrics)
            elif kind == "TYPE":
                allowed = OPENMETRICS_TYPES if openmetrics else TEXT_TYPES
                if rest.strip() not in allowed:
                    raise ParseError(number, f"invalid metric type {rest.strip()!r} for {name}; expected one of {', '.join(allowed)}")
                if family.declared or family.samples:
                    raise ParseError(number, f"second TYPE line for {name}, or TYPE after its samples")
                family.type, family.declared = rest.strip(), True
            else:
                family.unit = rest.strip()
                if family.unit and not name.endswith("_" + family.unit):
                    raise ParseError(number, f"unit {family.unit!r} must be the suffix of the metric name {name}")
            continue
        sample = parse_sample_line(line, number, openmetrics)
        family = resolve_family(sample.name, families, openmetrics)
        if family is None:
            family = families[sample.name] = Family(sample.name, "unknown" if openmetrics else "untyped")
        enter(family.name, number)
        if openmetrics and sample.name == family.name and family.type not in EXACT_NAME_TYPES:
            raise ParseError(number, f"{family.type} sample {sample.name} needs one of the suffixes {', '.join(OPENMETRICS_SUFFIXES[family.type])}")
        if sample.exemplar is not None and not (sample.name.endswith("_bucket") or (family.type == "counter" and sample.name.endswith("_total"))):
            raise ParseError(number, "exemplars are only allowed on counter _total and histogram _bucket samples")
        sample.labels = normalise_labels(family, sample.labels)
        family.samples.append(sample)
    if openmetrics and not saw_eof:
        raise ParseError(len(lines) + 1, "OpenMetrics input must end with # EOF")
    return Exposition(fmt, list(families.values()))


def detect_format(text: str) -> str:
    return OPENMETRICS if any(line.rstrip() == "# EOF" for line in text.split("\n")) else TEXT_FORMAT


def check_histogram(family: Family) -> List[str]:
    groups: Dict[Labels, Dict[str, Any]] = {}
    problems: List[str] = []
    for sample in family.samples:
        base = freeze({k: v for k, v in sample.labels.items() if k != "le"})
        group = groups.setdefault(base, {"buckets": [], "count": None, "line": sample.line})
        if sample.name.endswith("_bucket"):
            if "le" not in sample.labels:
                problems.append(f"line {sample.line}: {sample.name} has no le label")
                continue
            try:
                bound = float(sample.labels["le"])
            except ValueError:
                problems.append(f"line {sample.line}: le={sample.labels['le']!r} is not a number")
                continue
            group["buckets"].append((bound, sample.value, sample.line))
        elif sample.name.endswith(("_count", "_gcount")):
            group["count"] = (sample.value, sample.line)
    for base, group in groups.items():
        shown = render_series(dict(base, __name__=family.name))
        buckets = sorted(group["buckets"])
        if not buckets:
            problems.append(f"line {group['line']}: {shown} has no _bucket samples")
            continue
        if buckets[-1][0] != math.inf:
            problems.append(f"line {buckets[-1][2]}: {shown} has no le=\"+Inf\" bucket")
        for (low, low_count, _), (high, high_count, line) in zip(buckets, buckets[1:]):
            if low == high:
                problems.append(f"line {line}: {shown} repeats the bucket le={format_openmetrics_float(high)}")
            elif high_count < low_count:
                problems.append(f"line {line}: {shown} bucket le={format_openmetrics_float(high)} holds {format_value(high_count)}, less than {format_value(low_count)} in le={format_openmetrics_float(low)}; buckets are cumulative")
        if group["count"] is not None and buckets[-1][0] == math.inf and group["count"][0] != buckets[-1][1]:
            problems.append(f"line {group['count'][1]}: {shown} _count is {format_value(group['count'][0])} but the +Inf bucket holds {format_value(buckets[-1][1])}")
    return problems


def validate(exposition: Exposition) -> List[str]:
    problems: List[str] = []
    for family in exposition.families:
        seen: Dict[Tuple[str, Labels], int] = {}
        for sample in family.samples:
            key = (sample.name, freeze(sample.labels))
            if key in seen:
                problems.append(f"line {sample.line}: duplicate series {render_series(dict(sample.labels, __name__=sample.name))} (first on line {seen[key]})")
            else:
                seen[key] = sample.line
            if family.type == "counter" and sample.value < 0:
                problems.append(f"line {sample.line}: counter {sample.name} is negative ({format_value(sample.value)})")
            if family.type == "summary" and "quantile" in sample.labels:
                quantile = float(sample.labels["quantile"]) if FLOAT_TEXT.fullmatch(sample.labels["quantile"]) else math.nan
                if not 0 <= quantile <= 1:
                    problems.append(f"line {sample.line}: quantile={sample.labels['quantile']!r} is outside [0, 1]")
        if family.type in ("histogram", "gaugehistogram"):
            problems.extend(check_histogram(family))
    return problems


def metric_unit(name: str) -> Optional[Tuple[str, str]]:
    for part in name.split("_"):
        if part in UNITS:
            return part, UNITS[part]
        for prefix in UNIT_PREFIXES:
            if part.startswith(prefix) and part[len(prefix):] in UNITS:
                return part, UNITS[part[len(prefix):]]
    return None


def lint_label_sets(family: Family) -> List[List[str]]:
    if family.type in ("histogram", "summary"):
        drop = "le" if family.type == "histogram" else "quantile"
        unique = dict.fromkeys(freeze({k: v for k, v in s.labels.items() if k != drop}) for s in family.samples)
        return [[name for name, _ in key] for key in unique]
    return [list(sample.labels) for sample in family.samples]


def lint_family(family: Family) -> List[str]:
    name, kind = family.name, family.type
    lowered = name.lower()
    label_sets = lint_label_sets(family)
    out: List[str] = []
    if family.help is None:
        out.append("no help text")
    unit = metric_unit(name)
    if unit and unit[0] != unit[1]:
        out.append(f'use base unit "{unit[1]}" instead of "{unit[0]}"')
    if kind == "counter" and not name.endswith("_total"):
        out.append('counter metrics should have "_total" suffix')
    elif kind not in ("counter", "untyped") and name.endswith("_total"):
        out.append('non-counter metrics should not have "_total" suffix')
    if kind != "untyped":
        if kind != "histogram" and name.endswith("_bucket"):
            out.append('non-histogram metrics should not have "_bucket" suffix')
        if kind not in ("histogram", "summary") and name.endswith("_count"):
            out.append('non-histogram and non-summary metrics should not have "_count" suffix')
        if kind not in ("histogram", "summary") and name.endswith("_sum"):
            out.append('non-histogram and non-summary metrics should not have "_sum" suffix')
        for labels in label_sets:
            for label in labels:
                if kind != "histogram" and label == "le":
                    out.append('non-histogram metrics should not have "le" label')
                if kind != "summary" and label == "quantile":
                    out.append('non-summary metrics should not have "quantile" label')
        if f"_{kind}_" in lowered or lowered.endswith(f"_{kind}"):
            out.append(f"metric name should not include type '{kind}'")
    if ":" in name:
        out.append("metric names should not contain ':'")
    if CAMEL_CASE.search(name):
        out.append("metric names should be written in 'snake_case' not 'camelCase'")
    for labels in label_sets:
        out.extend("label names should be written in 'snake_case' not 'camelCase'" for label in labels if CAMEL_CASE.search(label))
    for abbreviation in UNIT_ABBREVIATIONS:
        if f"_{abbreviation}_" in lowered or lowered.endswith("_" + abbreviation):
            out.append("metric names should not contain abbreviated units")
    if kind not in ("histogram", "summary"):
        keys = [freeze(s.labels) for s in family.samples]
        out.extend("metric not unique" for i, key in enumerate(keys) if key in keys[i + 1:])
    return out


def lint(exposition: Exposition) -> List[Tuple[str, str]]:
    problems = [(family.name, text) for family in exposition.families for text in lint_family(family)]
    return sorted(problems, key=lambda problem: (problem[0], problem[1]))


@dataclass
class Matcher:
    name: str
    op: str
    value: str
    pattern: Any = field(default=None, repr=False)

    def __post_init__(self) -> None:
        if self.op in ("=~", "!~"):
            try:
                self.pattern = re.compile("(?s:" + self.value + ")")
            except re.error as error:
                raise InputError(f"invalid regex {self.value!r} for {self.name}: {error}")

    def matches(self, actual: str) -> bool:
        if self.op == "=":
            return actual == self.value
        if self.op == "!=":
            return actual != self.value
        hit = self.pattern.fullmatch(actual) is not None
        return hit if self.op == "=~" else not hit


def parse_selector(text: str) -> List[Matcher]:
    text = text.strip()
    matchers: List[Matcher] = []
    pos = 0
    match = METRIC_NAME.match(text)
    if match:
        matchers.append(Matcher("__name__", "=", match.group()))
        pos = match.end()
    pos = skip_spaces(text, pos)
    if pos < len(text):
        if text[pos] != "{":
            raise InputError(f"cannot parse selector {text!r}: expected {{ after the metric name")
        items, pos = parse_label_block(text, pos, None, operators=True)
        matchers.extend(Matcher(name, op, value) for name, op, value in items)
        if skip_spaces(text, pos) != len(text):
            raise InputError(f"unexpected text after the selector: {text[pos:]!r}")
    if not matchers or all(m.matches("") for m in matchers):
        raise InputError(f"selector {text!r} must contain at least one matcher that does not match the empty string")
    return matchers


def expand_template(template: str, match: Any) -> str:
    out: List[str] = []
    pos = 0
    while pos < len(template):
        char = template[pos]
        if char != "$":
            out.append(char)
            pos += 1
            continue
        if template.startswith("$$", pos):
            out.append("$")
            pos += 2
            continue
        name, end = "", pos + 1
        if template.startswith("${", pos):
            close = template.find("}", pos + 2)
            if close != -1 and TEMPLATE_NAME.fullmatch(template[pos + 2:close]):
                name, end = template[pos + 2:close], close + 1
        else:
            word = TEMPLATE_NAME.match(template, pos + 1)
            if word:
                name, end = word.group(), word.end()
        if not name:
            out.append("$")
            pos += 1
            continue
        if name.isdigit():
            number = int(name)
            out.append((match.group(number) or "") if number <= match.re.groups else "")
        else:
            out.append(match.groupdict().get(name) or "")
        pos = end
    return "".join(out)


@dataclass
class RelabelRule:
    action: str = "replace"
    source_labels: List[str] = field(default_factory=list)
    separator: str = ";"
    regex: str = "(.*)"
    target_label: str = ""
    replacement: str = "$1"
    modulus: int = 0
    pattern: Any = field(default=None, repr=False)

    def __post_init__(self) -> None:
        self.action = self.action.lower()
        if self.action not in RELABEL_ACTIONS:
            raise InputError(f"unknown relabel action {self.action!r}; expected one of {', '.join(RELABEL_ACTIONS)}")
        try:
            self.pattern = re.compile("(?s:" + self.regex + ")")
        except re.error as error:
            raise InputError(f"invalid relabel regex {self.regex!r}: {error}")
        if self.action in ("replace", "hashmod", "lowercase", "uppercase", "keepequal", "dropequal") and not self.target_label:
            raise InputError(f"relabel configuration for {self.action} action requires 'target_label' value")
        if self.action == "hashmod" and self.modulus <= 0:
            raise InputError("relabel configuration for hashmod requires non-zero modulus")

    @classmethod
    def from_dict(cls, doc: Dict[str, Any]) -> "RelabelRule":
        unknown = sorted(set(doc) - RELABEL_KEYS)
        if unknown:
            raise InputError(f"unknown relabel field(s) {', '.join(unknown)}")
        return cls(
            action=str(doc.get("action", "replace")),
            source_labels=[str(name) for name in doc.get("source_labels") or []],
            separator=str(doc.get("separator", ";")),
            regex=str(doc.get("regex", "(.*)")),
            target_label=str(doc.get("target_label", "")),
            replacement=str(doc.get("replacement", "$1")),
            modulus=int(doc.get("modulus", 0)),
        )


def set_label(labels: Dict[str, str], name: str, value: str) -> None:
    if value == "":
        labels.pop(name, None)
    elif name:
        labels[name] = value


def apply_rule(rule: RelabelRule, labels: Dict[str, str]) -> bool:
    value = rule.separator.join(labels.get(name, "") for name in rule.source_labels)
    action = rule.action
    if action == "drop":
        return rule.pattern.fullmatch(value) is None
    if action == "keep":
        return rule.pattern.fullmatch(value) is not None
    if action == "dropequal":
        return labels.get(rule.target_label, "") != value
    if action == "keepequal":
        return labels.get(rule.target_label, "") == value
    if action == "replace":
        if value == "" and rule.regex == "(.*)" and "$" not in rule.target_label and "$" not in rule.replacement:
            set_label(labels, rule.target_label, rule.replacement)
            return True
        match = rule.pattern.fullmatch(value)
        if match is None:
            return True
        target = expand_template(rule.target_label, match)
        if target:
            set_label(labels, target, expand_template(rule.replacement, match))
    elif action in ("lowercase", "uppercase"):
        set_label(labels, rule.target_label, value.lower() if action == "lowercase" else value.upper())
    elif action == "hashmod":
        digest = hashlib.md5(value.encode("utf-8")).digest()
        set_label(labels, rule.target_label, str(int.from_bytes(digest[8:], "big") % rule.modulus))
    elif action == "labelmap":
        for name, label_value in sorted(labels.items()):
            match = rule.pattern.fullmatch(name)
            if match:
                set_label(labels, expand_template(rule.replacement, match), label_value)
    else:
        for name in sorted(labels):
            hit = rule.pattern.fullmatch(name) is not None
            if hit == (action == "labeldrop"):
                del labels[name]
    return True


def relabel(labels: Dict[str, str], rules: Sequence[RelabelRule]) -> Optional[Dict[str, str]]:
    current = {k: v for k, v in labels.items() if v != ""}
    for rule in rules:
        if not apply_rule(rule, current):
            return None
    return current


@dataclass
class StaticGroup:
    targets: List[str]
    labels: Dict[str, str]


@dataclass
class ScrapeConfig:
    job_name: str
    interval_ms: int
    timeout_ms: int
    metrics_path: str = "/metrics"
    scheme: str = "http"
    honor_labels: bool = False
    honor_timestamps: bool = True
    params: Dict[str, List[str]] = field(default_factory=dict)
    groups: List[StaticGroup] = field(default_factory=list)
    relabel_configs: List[RelabelRule] = field(default_factory=list)
    metric_relabel_configs: List[RelabelRule] = field(default_factory=list)
    fallback_scrape_protocol: str = ""


def load_config(doc: Any, warn: Callable[[str], None] = lambda message: None) -> List[ScrapeConfig]:
    if not isinstance(doc, dict) or not isinstance(doc.get("scrape_configs", []), list):
        raise InputError("the config must be a mapping with a scrape_configs list (the shape of prometheus.yml)")
    defaults = doc.get("global") or {}
    interval = parse_duration(defaults.get("scrape_interval", "1m"))
    timeout = parse_duration(defaults.get("scrape_timeout", "10s"))
    configs: List[ScrapeConfig] = []
    for raw in doc.get("scrape_configs") or []:
        name = str(raw.get("job_name") or "")
        if not name:
            raise InputError("every scrape config needs a job_name")
        if any(c.job_name == name for c in configs):
            raise InputError(f'found multiple scrape configs with job name "{name}"')
        ignored = sorted(set(raw) - SCRAPE_KEYS)
        if ignored:
            warn(f"job {name}: minitsdb ignores {', '.join(ignored)}; only static_configs are supported")
        job_interval = parse_duration(raw["scrape_interval"]) if "scrape_interval" in raw else interval
        if "scrape_timeout" in raw:
            job_timeout = parse_duration(raw["scrape_timeout"])
        else:
            job_timeout = job_interval if timeout > job_interval else timeout
        if job_timeout > job_interval:
            raise InputError(f'scrape timeout greater than scrape interval for scrape config with job name "{name}"')
        fallback = str(raw.get("fallback_scrape_protocol", ""))
        if fallback and fallback not in FALLBACK_PROTOCOLS:
            raise InputError(f"unknown fallback_scrape_protocol {fallback!r}; expected one of {', '.join(FALLBACK_PROTOCOLS)}")
        groups = [StaticGroup([str(t) for t in g.get("targets") or []], {str(k): str(v) for k, v in (g.get("labels") or {}).items()}) for g in raw.get("static_configs") or []]
        configs.append(ScrapeConfig(
            job_name=name,
            interval_ms=job_interval,
            timeout_ms=job_timeout,
            metrics_path=str(raw.get("metrics_path", "/metrics")),
            scheme=str(raw.get("scheme", "http")),
            honor_labels=bool(raw.get("honor_labels", False)),
            honor_timestamps=bool(raw.get("honor_timestamps", True)),
            params={str(k): [str(x) for x in (v if isinstance(v, list) else [v])] for k, v in (raw.get("params") or {}).items()},
            groups=groups,
            relabel_configs=[RelabelRule.from_dict(r) for r in raw.get("relabel_configs") or []],
            metric_relabel_configs=[RelabelRule.from_dict(r) for r in raw.get("metric_relabel_configs") or []],
            fallback_scrape_protocol=fallback,
        ))
    return configs


@dataclass
class Target:
    config: ScrapeConfig
    discovered: Dict[str, str]
    labels: Dict[str, str]
    url: str
    interval_ms: int
    timeout_ms: int
    tracked: Set[Labels] = field(default_factory=set)
    health: str = "unknown"
    last_error: str = ""
    last_scrape_ms: int = 0
    last_duration: float = 0.0

    @property
    def job(self) -> str:
        return self.config.job_name

    @property
    def instance(self) -> str:
        return self.labels.get("instance", "")


def discovered_labels(config: ScrapeConfig, address: str, group_labels: Dict[str, str]) -> Dict[str, str]:
    labels = {"__address__": address}
    for name, value in group_labels.items():
        labels.setdefault(name, value)
    defaults = {
        "job": config.job_name,
        "__scrape_interval__": format_duration(config.interval_ms),
        "__scrape_timeout__": format_duration(config.timeout_ms),
        "__metrics_path__": config.metrics_path,
        "__scheme__": config.scheme,
    }
    for name, value in defaults.items():
        if not labels.get(name):
            labels[name] = value
    for name, values in config.params.items():
        if values and not labels.get("__param_" + name):
            labels["__param_" + name] = values[0]
    return labels


def build_target(config: ScrapeConfig, discovered: Dict[str, str], labels: Dict[str, str]) -> Target:
    address = labels.get("__address__", "")
    if not address:
        raise InputError(f"job {config.job_name}: a target has no address after relabelling")
    interval = parse_duration(labels.get("__scrape_interval__", ""))
    timeout = parse_duration(labels.get("__scrape_timeout__", ""))
    if interval == 0 or timeout == 0:
        raise InputError(f"job {config.job_name}: scrape interval and timeout cannot be 0")
    if timeout > interval:
        raise InputError(f"job {config.job_name}: scrape timeout cannot be greater than scrape interval")
    labels = {k: v for k, v in labels.items() if not k.startswith("__meta_")}
    if not labels.get("instance"):
        labels["instance"] = address
    params = {k[len("__param_"):]: v for k, v in sorted(labels.items()) if k.startswith("__param_")}
    query = "?" + urllib.parse.urlencode(params) if params else ""
    url = f"{labels.get('__scheme__', 'http')}://{address}{labels.get('__metrics_path__', '/metrics')}{query}"
    public = {k: v for k, v in labels.items() if not k.startswith("__")}
    return Target(config, discovered, public, url, interval, timeout)


def discover(configs: Sequence[ScrapeConfig]) -> Tuple[List[Target], List[Tuple[str, Dict[str, str]]]]:
    active: List[Target] = []
    dropped: List[Tuple[str, Dict[str, str]]] = []
    for config in configs:
        for group in config.groups:
            for address in group.targets:
                discovered = discovered_labels(config, address, group.labels)
                labels = relabel(discovered, config.relabel_configs)
                if labels is None:
                    dropped.append((config.job_name, discovered))
                else:
                    active.append(build_target(config, discovered, labels))
    return active, dropped


def targets_document(active: Sequence[Target], dropped: Sequence[Tuple[str, Dict[str, str]]]) -> Dict[str, Any]:
    return {"status": "success", "data": {
        "activeTargets": [{
            "discoveredLabels": t.discovered, "labels": t.labels, "scrapePool": t.job, "scrapeUrl": t.url,
            "lastError": t.last_error, "health": t.health,
            "scrapeInterval": format_duration(t.interval_ms), "scrapeTimeout": format_duration(t.timeout_ms),
        } for t in active],
        "droppedTargets": [{"discoveredLabels": labels, "scrapePool": job} for job, labels in dropped],
    }}


def http_fetch(url: str, timeout: float) -> Tuple[str, bytes]:
    request = urllib.request.Request(url, headers={
        "Accept": ACCEPT,
        "Accept-Encoding": "gzip",
        "User-Agent": "minitsdb",
        "X-Prometheus-Scrape-Timeout-Seconds": f"{timeout:g}",
    })
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read()
            if (response.headers.get("Content-Encoding") or "").lower() == "gzip":
                body = gzip.decompress(body)
            return response.headers.get("Content-Type") or "", body
    except urllib.error.HTTPError as error:
        raise ScrapeError(f"server returned HTTP status {error.code} {error.reason}")
    except (urllib.error.URLError, OSError) as error:
        reason = getattr(error, "reason", error)
        raise ScrapeError(f'Get "{url}": {reason}')


def scrape_protocol(content_type: str, fallback: str = "") -> str:
    if not content_type.strip():
        if fallback:
            return FALLBACK_PROTOCOLS[fallback]
        raise ScrapeError("non-compliant scrape target sending blank Content-Type and no fallback_scrape_protocol specified for target")
    media = content_type.split(";")[0].strip().lower()
    if media == "text/plain":
        return TEXT_FORMAT
    if media == "application/openmetrics-text":
        return OPENMETRICS
    if fallback:
        return FALLBACK_PROTOCOLS[fallback]
    raise ScrapeError(f'received unsupported Content-Type "{content_type}" and no fallback_scrape_protocol specified for target')


def sample_labels(sample: Sample, target: Target) -> Optional[Dict[str, str]]:
    labels = {k: v for k, v in sample.labels.items() if v != ""}
    labels["__name__"] = sample.name
    if target.config.honor_labels:
        for name, value in sorted(target.labels.items()):
            if name not in labels:
                set_label(labels, name, value)
    else:
        conflicts = sorted(((name, labels[name]) for name in sorted(target.labels) if labels.get(name)), key=lambda item: len(item[0]))
        for name, value in sorted(target.labels.items()):
            set_label(labels, name, value)
        for name, value in conflicts:
            renamed = name
            while True:
                renamed = "exported_" + renamed
                if not labels.get(renamed):
                    labels[renamed] = value
                    break
    return relabel(labels, target.config.metric_relabel_configs)


@dataclass
class ScrapeReport:
    target: Target
    timestamp_ms: int
    up: bool
    duration: float
    scraped: int
    post_relabel: int
    series_added: int
    stale: int
    error: str
    rejected: int = 0

    def line(self) -> str:
        clock = time.strftime("%H:%M:%S", time.gmtime(self.timestamp_ms / 1000)) + f".{self.timestamp_ms % 1000:03d}"
        text = (f"{clock} {self.target.job} {self.target.instance} up={int(self.up)} scraped={self.scraped} "
                f"kept={self.post_relabel} new={self.series_added} stale={self.stale} {self.duration * 1000:.1f}ms")
        if self.rejected:
            text += f" rejected={self.rejected}"
        return text + (f" error: {self.error}" if self.error else "")


class Scraper:
    def __init__(self, db: "TSDB", targets: Sequence[Target], fetch: Fetch = http_fetch, clock: Callable[[], float] = time.time):
        self.db = db
        self.targets = list(targets)
        self.fetch = fetch
        self.clock = clock

    def scrape(self, target: Target) -> ScrapeReport:
        started = self.clock()
        timestamp = int(round(started * 1000))
        error = ""
        samples: List[Sample] = []
        try:
            content_type, body = self.fetch(target.url, target.timeout_ms / 1000)
            protocol = scrape_protocol(content_type, target.config.fallback_scrape_protocol)
            samples = parse_exposition(body.decode("utf-8"), protocol).samples()
        except (ScrapeError, InputError, UnicodeDecodeError) as problem:
            error = str(problem)
        duration = self.clock() - started
        if not error and duration > target.timeout_ms / 1000:
            error, samples = "context deadline exceeded", []
        rows: List[Row] = []
        current: Set[Labels] = set()
        tracked: Set[Labels] = set()
        kept = 0
        for sample in samples:
            labels = sample_labels(sample, target)
            if labels is None:
                continue
            kept += 1
            key = freeze(labels)
            if key in current:
                continue
            current.add(key)
            explicit = target.config.honor_timestamps and sample.timestamp_ms is not None
            rows.append((key, sample.timestamp_ms if explicit else timestamp, value_bits(sample.value)))
            if not explicit:
                tracked.add(key)
        stale = sorted(target.tracked - tracked)
        rows.extend((key, timestamp, STALE_NAN) for key in stale)
        added = len(tracked - target.tracked)
        values = (1.0 if not error else 0.0, duration, float(len(samples)), float(kept), float(added))
        for name, value in zip(REPORT_METRICS, values):
            rows.append((freeze(dict(target.labels, __name__=name)), timestamp, value_bits(value)))
        result = self.db.commit(rows)
        target.tracked = tracked
        target.health = "down" if error else "up"
        target.last_error, target.last_scrape_ms, target.last_duration = error, timestamp, duration
        return ScrapeReport(target, timestamp, not error, duration, len(samples), kept, added, len(stale), error, result.out_of_order + result.duplicates)

    def run(self, duration: Optional[float] = None, rounds: Optional[int] = None, sleep: Callable[[float], None] = time.sleep,
            should_stop: Callable[[], bool] = lambda: False, report: Callable[[ScrapeReport], None] = lambda r: None,
            retention_ms: Optional[int] = None) -> int:
        if not self.targets:
            raise InputError("no targets to scrape: every target was dropped by relabel_configs or none is configured")
        start = self.clock()
        due = [start] * len(self.targets)
        counts = [0] * len(self.targets)
        while not should_stop():
            pending = [i for i in range(len(self.targets)) if rounds is None or counts[i] < rounds]
            if not pending:
                break
            index = min(pending, key=lambda i: (due[i], i))
            if duration is not None and due[index] - start >= duration:
                break
            wait = due[index] - self.clock()
            if wait > 0:
                sleep(min(wait, 0.25))
                continue
            target = self.targets[index]
            report(self.scrape(target))
            counts[index] += 1
            interval = target.interval_ms / 1000
            due[index] += interval
            now = self.clock()
            while due[index] < now:
                due[index] += interval
            if retention_ms:
                self.db.apply_retention(int(now * 1000) - retention_ms)
        return sum(counts)


class BitWriter:
    def __init__(self) -> None:
        self.data = bytearray()
        self.length = 0

    def write_bit(self, bit: int) -> None:
        if self.length % 8 == 0:
            self.data.append(0)
        if bit:
            self.data[-1] |= 0x80 >> (self.length % 8)
        self.length += 1

    def write_bits(self, value: int, count: int) -> None:
        for shift in range(count - 1, -1, -1):
            self.write_bit((value >> shift) & 1)

    def write_bytes(self, data: bytes) -> None:
        for byte in data:
            self.write_bits(byte, 8)


class BitReader:
    def __init__(self, data: bytes) -> None:
        self.data = data
        self.position = 0

    def read_bit(self) -> int:
        index = self.position >> 3
        if index >= len(self.data):
            raise InputError("chunk ends before its last sample")
        self.position += 1
        return (self.data[index] >> (7 - ((self.position - 1) & 7))) & 1

    def read_bits(self, count: int) -> int:
        value = 0
        for _ in range(count):
            value = (value << 1) | self.read_bit()
        return value

    def read_byte(self) -> int:
        return self.read_bits(8)


def encode_uvarint(value: int) -> bytes:
    out = bytearray()
    while value >= 0x80:
        out.append((value & 0x7F) | 0x80)
        value >>= 7
    out.append(value)
    return bytes(out)


def encode_varint(value: int) -> bytes:
    return encode_uvarint(value << 1 if value >= 0 else ((~value) << 1) | 1)


def read_uvarint_from(next_byte: Callable[[], int]) -> int:
    result = shift = 0
    while True:
        byte = next_byte()
        result |= (byte & 0x7F) << shift
        if byte < 0x80:
            return result
        shift += 7
        if shift > 63:
            raise InputError("varint overflows 64 bits")


def read_varint_from(next_byte: Callable[[], int]) -> int:
    raw = read_uvarint_from(next_byte)
    return (raw >> 1) ^ -(raw & 1)


class Cursor:
    def __init__(self, data: bytes, pos: int = 0):
        self.data = data
        self.pos = pos

    def byte(self) -> int:
        return self.take(1)[0]

    def take(self, size: int) -> bytes:
        if self.pos + size > len(self.data):
            raise InputError("record ends early: the WAL is corrupt")
        self.pos += size
        return self.data[self.pos - size:self.pos]

    def done(self) -> bool:
        return self.pos >= len(self.data)


def leading_zeros(value: int) -> int:
    return 64 - value.bit_length()


def trailing_zeros(value: int) -> int:
    return (value & -value).bit_length() - 1


class XORChunk:
    def __init__(self) -> None:
        self.stream = BitWriter()
        self.count = 0
        self.min_t = 0
        self.max_t = 0
        self.t = 0
        self.t_delta = 0
        self.value = 0
        self.leading = 0xFF
        self.trailing = 0

    def append(self, t: int, bits: int) -> Tuple[int, int]:
        start = self.stream.length
        delta = 0
        if self.count == 0:
            self.stream.write_bytes(encode_varint(t))
            middle = self.stream.length
            self.stream.write_bits(bits, 64)
            self.min_t = t
        elif self.count == 1:
            delta = t - self.t
            self.stream.write_bytes(encode_uvarint(delta))
            middle = self.stream.length
            self.write_value(bits)
        else:
            delta = t - self.t
            self.write_dod(delta - self.t_delta)
            middle = self.stream.length
            self.write_value(bits)
        self.t, self.value, self.t_delta, self.max_t = t, bits, delta, t
        self.count += 1
        return middle - start, self.stream.length - middle

    def write_dod(self, dod: int) -> None:
        if dod == 0:
            self.stream.write_bit(0)
            return
        for prefix, width, size in DOD_BUCKETS:
            if -((1 << (size - 1)) - 1) <= dod <= 1 << (size - 1):
                self.stream.write_bits(prefix, width)
                self.stream.write_bits(dod & ((1 << size) - 1), size)
                return
        self.stream.write_bits(0b1111, 4)
        self.stream.write_bits(dod & MASK64, 64)

    def write_value(self, bits: int) -> None:
        delta = bits ^ self.value
        if delta == 0:
            self.stream.write_bit(0)
            return
        self.stream.write_bit(1)
        leading = min(leading_zeros(delta), 31)
        trailing = trailing_zeros(delta)
        if self.leading != 0xFF and leading >= self.leading and trailing >= self.trailing:
            self.stream.write_bit(0)
            self.stream.write_bits(delta >> self.trailing, 64 - self.leading - self.trailing)
            return
        self.leading, self.trailing = leading, trailing
        significant = 64 - leading - trailing
        self.stream.write_bit(1)
        self.stream.write_bits(leading, 5)
        self.stream.write_bits(significant & 0x3F, 6)
        self.stream.write_bits(delta >> trailing, significant)

    def to_bytes(self) -> bytes:
        return self.count.to_bytes(2, "big") + bytes(self.stream.data)


def read_dod(reader: BitReader) -> int:
    prefix = 0
    for _ in range(4):
        prefix <<= 1
        if reader.read_bit() == 0:
            break
        prefix |= 1
    size = {0b0: 0, 0b10: 14, 0b110: 17, 0b1110: 20, 0b1111: 64}[prefix]
    if size == 0:
        return 0
    bits = reader.read_bits(size)
    if size == 64:
        return bits - (1 << 64) if bits >> 63 else bits
    return bits - (1 << size) if bits > (1 << (size - 1)) else bits


def read_value(reader: BitReader, previous: int, leading: int, trailing: int) -> Tuple[int, int, int]:
    if reader.read_bit() == 0:
        return previous, leading, trailing
    if reader.read_bit() == 1:
        leading = reader.read_bits(5)
        significant = reader.read_bits(6) or 64
        trailing = 64 - leading - significant
    significant = 64 - leading - trailing
    return previous ^ (reader.read_bits(significant) << trailing), leading, trailing


def decode_chunk(data: bytes) -> List[Tuple[int, int]]:
    count = int.from_bytes(data[:2], "big")
    reader = BitReader(data[2:])
    out: List[Tuple[int, int]] = []
    if count == 0:
        return out
    t = read_varint_from(reader.read_byte)
    value = reader.read_bits(64)
    out.append((t, value))
    leading = trailing = delta = 0
    for index in range(1, count):
        delta = read_uvarint_from(reader.read_byte) if index == 1 else delta + read_dod(reader)
        t += delta
        value, leading, trailing = read_value(reader, value, leading, trailing)
        out.append((t, value))
    return out


@dataclass
class ChunkMeta:
    data: bytes
    min_t: int
    max_t: int
    count: int


class Series:
    def __init__(self, ref: int, labels: Labels):
        self.ref = ref
        self.labels = labels
        self.chunks: List[ChunkMeta] = []
        self.head = XORChunk()
        self.last_t: Optional[int] = None
        self.last_bits = 0

    def append(self, t: int, bits: int) -> None:
        if self.head.count >= SAMPLES_PER_CHUNK:
            self.chunks.append(ChunkMeta(self.head.to_bytes(), self.head.min_t, self.head.max_t, self.head.count))
            self.head = XORChunk()
        self.head.append(t, bits)
        self.last_t, self.last_bits = t, bits

    def all_chunks(self) -> List[ChunkMeta]:
        chunks = list(self.chunks)
        if self.head.count:
            chunks.append(ChunkMeta(self.head.to_bytes(), self.head.min_t, self.head.max_t, self.head.count))
        return chunks

    def samples(self, start: Optional[int] = None, end: Optional[int] = None) -> List[Tuple[int, int]]:
        out: List[Tuple[int, int]] = []
        for chunk in self.all_chunks():
            if (start is not None and chunk.max_t < start) or (end is not None and chunk.min_t > end):
                continue
            out.extend((t, b) for t, b in decode_chunk(chunk.data) if (start is None or t >= start) and (end is None or t <= end))
        return out


def order_verdict(previous: Tuple[Optional[int], int], t: int, bits: int) -> str:
    last_t, last_bits = previous
    if last_t is None or t > last_t:
        return "ok"
    if t < last_t:
        return "out_of_order"
    return "ignored" if bits == last_bits else "duplicate"


@dataclass
class CommitResult:
    new_series: int = 0
    appended: int = 0
    out_of_order: int = 0
    duplicates: int = 0


def build_crc32c_table() -> List[int]:
    table = []
    for byte in range(256):
        crc = byte
        for _ in range(8):
            crc = (crc >> 1) ^ 0x82F63B78 if crc & 1 else crc >> 1
        table.append(crc)
    return table


CRC32C_TABLE = build_crc32c_table()


def crc32c(data: bytes) -> int:
    crc = 0xFFFFFFFF
    for byte in data:
        crc = CRC32C_TABLE[(crc ^ byte) & 0xFF] ^ (crc >> 8)
    return crc ^ 0xFFFFFFFF


def encode_series_record(series: Sequence[Tuple[int, Labels]]) -> bytes:
    out = bytearray([RECORD_SERIES])
    for ref, labels in series:
        out += ref.to_bytes(8, "big") + encode_uvarint(len(labels))
        for name, value in labels:
            for text in (name.encode("utf-8"), value.encode("utf-8")):
                out += encode_uvarint(len(text)) + text
    return bytes(out)


def encode_samples_record(samples: Sequence[Tuple[int, int, int]]) -> bytes:
    out = bytearray([RECORD_SAMPLES])
    if not samples:
        return bytes(out)
    first_ref, first_t = samples[0][0], samples[0][1]
    out += first_ref.to_bytes(8, "big") + first_t.to_bytes(8, "big", signed=True)
    for ref, t, bits in samples:
        out += encode_varint(ref - first_ref) + encode_varint(t - first_t) + bits.to_bytes(8, "big")
    return bytes(out)


def decode_record(record: bytes) -> Tuple[int, List[Any]]:
    kind = record[0] if record else 0
    items: List[Any] = []
    if kind == RECORD_SERIES:
        cursor = Cursor(record, 1)
        while not cursor.done():
            ref = int.from_bytes(cursor.take(8), "big")
            labels = []
            for _ in range(read_uvarint_from(cursor.byte)):
                name = cursor.take(read_uvarint_from(cursor.byte)).decode("utf-8")
                value = cursor.take(read_uvarint_from(cursor.byte)).decode("utf-8")
                labels.append((name, value))
            items.append((ref, tuple(labels)))
    elif kind == RECORD_SAMPLES and len(record) > 1:
        cursor = Cursor(record, 1)
        first_ref = int.from_bytes(cursor.take(8), "big")
        first_t = int.from_bytes(cursor.take(8), "big", signed=True)
        while not cursor.done():
            ref = first_ref + read_varint_from(cursor.byte)
            t = first_t + read_varint_from(cursor.byte)
            items.append((ref, t, int.from_bytes(cursor.take(8), "big")))
    return kind, items


@dataclass
class WALProblem:
    segment: str
    offset: int
    reason: str

    def describe(self) -> str:
        return f"{self.reason} in segment {os.path.basename(self.segment)} at byte {self.offset}"


@dataclass
class WALReplay:
    records: List[bytes]
    problem: Optional[WALProblem]
    segments: int
    size: int


def wal_segments(directory: str) -> List[Tuple[int, str]]:
    if not os.path.isdir(directory):
        return []
    return sorted((int(name), os.path.join(directory, name)) for name in os.listdir(directory) if name.isdigit())


def read_wal(directory: str) -> WALReplay:
    records: List[bytes] = []
    segments = wal_segments(directory)
    size = 0
    for _, path in segments:
        with open(path, "rb") as handle:
            data = handle.read()
        size += len(data)
        pos = record_start = 0
        pending: Optional[bytearray] = None

        def broken(reason: str) -> WALReplay:
            return WALReplay(records, WALProblem(path, record_start if pending is not None else pos, reason), len(segments), size)

        while pos < len(data):
            room = PAGE_SIZE - pos % PAGE_SIZE
            if room < RECORD_HEADER or data[pos] == REC_PAGE_TERM:
                if any(data[pos:pos + room]):
                    return broken("non-zero bytes in page padding")
                pos += room
                continue
            if pos + RECORD_HEADER > len(data):
                return broken("torn record header")
            kind = data[pos]
            length = int.from_bytes(data[pos + 1:pos + 3], "big")
            checksum = int.from_bytes(data[pos + 3:pos + 7], "big")
            body = data[pos + RECORD_HEADER:pos + RECORD_HEADER + length]
            if kind > REC_LAST:
                return broken(f"unknown fragment type {kind}")
            if len(body) < length:
                return broken(f"torn record: {len(body)} of {length} bytes")
            if crc32c(body) != checksum:
                return broken("checksum mismatch")
            if (kind in (REC_FULL, REC_FIRST)) != (pending is None):
                return broken("fragments out of sequence")
            if kind == REC_FULL:
                records.append(bytes(body))
            elif kind == REC_FIRST:
                pending, record_start = bytearray(body), pos
            else:
                pending = (pending or bytearray()) + body
                if kind == REC_LAST:
                    records.append(bytes(pending))
                    pending = None
            pos += RECORD_HEADER + length
        if pending is not None:
            return broken("torn record: its last fragment is missing")
    return WALReplay(records, None, len(segments), size)


def repair_wal(directory: str, problem: WALProblem) -> int:
    removed = 0
    with open(problem.segment, "r+b") as handle:
        handle.seek(0, os.SEEK_END)
        removed += handle.tell() - problem.offset
        handle.truncate(problem.offset)
    index = int(os.path.basename(problem.segment))
    for number, path in wal_segments(directory):
        if number > index:
            removed += os.path.getsize(path)
            os.remove(path)
    return removed


class WAL:
    def __init__(self, directory: str, segment_size: int = SEGMENT_SIZE, sync: bool = False):
        os.makedirs(directory, exist_ok=True)
        existing = wal_segments(directory)
        self.directory = directory
        self.segment_size = segment_size
        self.sync = sync
        self.index = existing[-1][0] + 1 if existing else 0
        self.open_segment()

    def open_segment(self) -> None:
        self.path = os.path.join(self.directory, f"{self.index:08d}")
        self.handle = open(self.path, "ab")
        self.offset = self.handle.tell()

    def log(self, record: bytes) -> None:
        estimate = len(record) + RECORD_HEADER * (len(record) // (PAGE_SIZE - RECORD_HEADER) + 3)
        if self.offset and self.offset + estimate > self.segment_size:
            self.handle.close()
            self.index += 1
            self.open_segment()
        out = bytearray()
        data = record
        first = True
        while first or data:
            room = PAGE_SIZE - (self.offset + len(out)) % PAGE_SIZE
            if room < RECORD_HEADER:
                out += bytes(room)
                continue
            part, data = data[:room - RECORD_HEADER], data[room - RECORD_HEADER:]
            kind = (REC_FULL if not data else REC_FIRST) if first else (REC_LAST if not data else REC_MIDDLE)
            out += bytes([kind]) + len(part).to_bytes(2, "big") + crc32c(part).to_bytes(4, "big") + part
            first = False
        self.handle.write(out)
        self.offset += len(out)

    def flush(self) -> None:
        self.handle.flush()
        if self.sync:
            os.fsync(self.handle.fileno())

    def close(self) -> None:
        self.handle.close()


def ranked(counts: Dict[str, int], top: int, key: str, value: str) -> List[Dict[str, Any]]:
    return [{key: name, value: count} for name, count in sorted(counts.items(), key=lambda item: (-item[1], item[0]))[:top]]


class TSDB:
    def __init__(self, wal: Optional[WAL] = None):
        self.series: Dict[int, Series] = {}
        self.refs: Dict[Labels, int] = {}
        self.postings: Dict[Tuple[str, str], Set[int]] = {}
        self.next_ref = 1
        self.wal = wal
        self.replay_problem = ""
        self.replay_skipped = 0

    @classmethod
    def open(cls, directory: str, writable: bool = False, segment_size: int = SEGMENT_SIZE, sync: bool = False) -> "TSDB":
        wal_dir = os.path.join(directory, "wal")
        if not writable and not os.path.isdir(wal_dir):
            raise InputError(f"{directory} has no wal/ directory; run `minitsdb scrape --data {directory}` first")
        db = cls()
        replay = read_wal(wal_dir)
        for record in replay.records:
            db.apply_record(record)
        if replay.problem:
            db.replay_problem = replay.problem.describe()
            if writable:
                removed = repair_wal(wal_dir, replay.problem)
                db.replay_problem += f"; truncated {removed} bytes"
        if writable:
            db.wal = WAL(wal_dir, segment_size, sync)
        return db

    def create(self, labels: Labels, ref: Optional[int] = None) -> Series:
        ref = self.next_ref if ref is None else ref
        self.next_ref = max(self.next_ref, ref + 1)
        series = self.series[ref] = Series(ref, labels)
        self.refs[labels] = ref
        for pair in labels:
            self.postings.setdefault(pair, set()).add(ref)
        return series

    def drop(self, series: Series) -> None:
        del self.series[series.ref]
        del self.refs[series.labels]
        for pair in series.labels:
            refs = self.postings.get(pair, set())
            refs.discard(series.ref)
            if not refs:
                self.postings.pop(pair, None)

    def apply_record(self, record: bytes) -> None:
        kind, items = decode_record(record)
        if kind == RECORD_SERIES:
            for ref, labels in items:
                if labels not in self.refs:
                    self.create(labels, ref)
        elif kind == RECORD_SAMPLES:
            for ref, t, bits in items:
                series = self.series.get(ref)
                if series is None or order_verdict((series.last_t, series.last_bits), t, bits) != "ok":
                    self.replay_skipped += 1
                    continue
                series.append(t, bits)

    def commit(self, rows: Iterable[Row]) -> CommitResult:
        result = CommitResult()
        created: List[Tuple[int, Labels]] = []
        accepted: List[Tuple[Series, int, int]] = []
        last: Dict[int, Tuple[Optional[int], int]] = {}
        for labels, t, bits in rows:
            ref = self.refs.get(labels)
            if ref is None:
                series = self.create(labels)
                created.append((series.ref, labels))
            else:
                series = self.series[ref]
            verdict = order_verdict(last.get(series.ref, (series.last_t, series.last_bits)), t, bits)
            if verdict == "ok":
                accepted.append((series, t, bits))
                last[series.ref] = (t, bits)
            elif verdict == "out_of_order":
                result.out_of_order += 1
            elif verdict == "duplicate":
                result.duplicates += 1
        if self.wal is not None:
            if created:
                self.wal.log(encode_series_record(created))
            if accepted:
                self.wal.log(encode_samples_record([(s.ref, t, b) for s, t, b in accepted]))
            self.wal.flush()
        for series, t, bits in accepted:
            series.append(t, bits)
        result.new_series, result.appended = len(created), len(accepted)
        return result

    def select(self, matchers: Sequence[Matcher]) -> List[Series]:
        candidates: Optional[Set[int]] = None
        for matcher in matchers:
            if matcher.op == "=" and matcher.value:
                refs = self.postings.get((matcher.name, matcher.value), set())
                candidates = set(refs) if candidates is None else candidates & refs
        pool = list(self.series.values()) if candidates is None else [self.series[ref] for ref in candidates]
        chosen = [s for s in pool if all(m.matches(dict(s.labels).get(m.name, "")) for m in matchers)]
        return sorted(chosen, key=lambda s: s.labels)

    def instant(self, matchers: Sequence[Matcher], at_ms: int, lookback_ms: int = LOOKBACK_MS) -> List[Tuple[Labels, int, float]]:
        out = []
        for series in self.select(matchers):
            window = series.samples(at_ms - lookback_ms + 1, at_ms)
            if window and window[-1][1] != STALE_NAN:
                out.append((series.labels, window[-1][0], bits_float(window[-1][1])))
        return out

    def max_time(self) -> Optional[int]:
        times = [s.last_t for s in self.series.values() if s.last_t is not None]
        return max(times) if times else None

    def apply_retention(self, min_t: int) -> Tuple[int, int]:
        chunks = dropped = 0
        for series in list(self.series.values()):
            keep = [chunk for chunk in series.chunks if chunk.max_t >= min_t]
            chunks += len(series.chunks) - len(keep)
            series.chunks = keep
            if not keep and (series.head.count == 0 or series.head.max_t < min_t):
                chunks += 1 if series.head.count else 0
                self.drop(series)
                dropped += 1
        return chunks, dropped

    def cardinality(self, top: int = 10, max_values: int = 20) -> Dict[str, Any]:
        metrics: Dict[str, int] = {}
        values: Dict[str, Set[str]] = {}
        pairs: Dict[str, int] = {}
        entries = 0
        for series in self.series.values():
            for name, value in series.labels:
                values.setdefault(name, set()).add(value)
                pairs[f"{name}={value}"] = pairs.get(f"{name}={value}", 0) + 1
                entries += 1
                if name == "__name__":
                    metrics[value] = metrics.get(value, 0) + 1
        total = len(self.series)
        offenders = []
        for name, seen in values.items():
            if name == "__name__" or (len(seen) < max_values and not UNBOUNDED_LABEL.search(name)):
                continue
            carrying = [dict(s.labels) for s in self.series.values() if name in dict(s.labels)]
            offenders.append({
                "label": name,
                "values": len(seen),
                "series": len(carrying),
                "share": round(len(carrying) / total, 4) if total else 0.0,
                "series_per_value": round(len(carrying) / len(seen), 2),
                "metrics": sorted({labels.get("__name__", "") for labels in carrying}),
            })
        offenders.sort(key=lambda o: (-o["series"], o["label"]))
        return {
            "total_series": total,
            "label_names": len(values),
            "label_pairs": len(pairs),
            "postings_entries": entries,
            "metrics": ranked(metrics, top, "name", "series"),
            "labels": ranked({k: len(v) for k, v in values.items()}, top, "label", "values"),
            "pairs": ranked(pairs, top, "pair", "series"),
            "offenders": offenders,
        }

    def chunk_stats(self) -> Dict[str, Any]:
        rows = []
        for series in sorted(self.series.values(), key=lambda s: s.labels):
            chunks = series.all_chunks()
            rows.append({"series": render_series(series.labels), "samples": sum(c.count for c in chunks), "chunks": len(chunks), "bytes": sum(len(c.data) for c in chunks)})
        samples = sum(r["samples"] for r in rows)
        size = sum(r["bytes"] for r in rows)
        worst = sorted((r for r in rows if r["samples"]), key=lambda r: (-r["bytes"] / r["samples"], r["series"]))[:10]
        return {
            "series": len(rows),
            "samples": samples,
            "chunks": sum(r["chunks"] for r in rows),
            "bytes": size,
            "raw_bytes": samples * 16,
            "bytes_per_sample": round(size / samples, 3) if samples else 0.0,
            "worst": worst,
        }


def render_demo(elapsed: float, hidden: Iterable[str] = ()) -> str:
    hidden = set(hidden)
    t = max(0.0, elapsed)
    lines = [
        "# HELP cc_http_requests_total HTTP requests served, by method and status code.",
        "# TYPE cc_http_requests_total counter",
        f'cc_http_requests_total{{method="GET",code="200"}} {int(t * 10)}',
        'cc_http_requests_total{method="GET",code="500"} 7',
        f'cc_http_requests_total{{method="POST",code="200"}} {int(t * 2)}',
        'cc_http_requests_total{method="POST",code="201"} 42',
        'cc_http_requests_total{method="DELETE",code="204"} 3',
        "# HELP cc_http_request_duration_seconds Time to serve a request.",
        "# TYPE cc_http_request_duration_seconds histogram",
    ]
    for path, counts, total in DEMO_LATENCY:
        lines += [f'cc_http_request_duration_seconds_bucket{{path="{path}",le="{le}"}} {count}' for le, count in zip(DEMO_BUCKETS, counts)]
        lines += [f'cc_http_request_duration_seconds_sum{{path="{path}"}} {total}', f'cc_http_request_duration_seconds_count{{path="{path}"}} {counts[-1]}']
    lines += [
        "# HELP cc_rpc_duration_seconds RPC latency quantiles over the last ten minutes.",
        "# TYPE cc_rpc_duration_seconds summary",
        'cc_rpc_duration_seconds{quantile="0.5"} 0.012',
        'cc_rpc_duration_seconds{quantile="0.9"} 0.045',
        'cc_rpc_duration_seconds{quantile="0.99"} 0.21',
        "cc_rpc_duration_seconds_sum 18.75",
        "cc_rpc_duration_seconds_count 1200",
        "# HELP cc_queue_depth Jobs waiting, by queue.",
        "# TYPE cc_queue_depth gauge",
    ]
    lines += [f'cc_queue_depth{{queue="{escape_value(queue)}"}} {depth}' for queue, depth in DEMO_QUEUES if queue not in hidden]
    lines += ["# HELP cc_user_logins_total Logins per user. Never do this: user_id is unbounded.", "# TYPE cc_user_logins_total counter"]
    lines += [f'cc_user_logins_total{{user_id="u{n:03d}"}} {n}' for n in range(1, DEMO_USERS + 1)]
    lines += [
        "# HELP cc_jobs_processed Jobs processed since start.",
        "# TYPE cc_jobs_processed counter",
        f"cc_jobs_processed {int(t)}",
        "# HELP cc_cache_latency_milliseconds Cache lookup latency.",
        "# TYPE cc_cache_latency_milliseconds gauge",
        "cc_cache_latency_milliseconds 1.5",
        "# TYPE cc_build_info gauge",
        'cc_build_info{job="batch-runner",version="1.4.2"} 1',
        "# HELP cc_debug_allocations_total Allocations by memory pool, for debugging only.",
        "# TYPE cc_debug_allocations_total counter",
    ]
    lines += [f'cc_debug_allocations_total{{pool="{pool}"}} {1024 * (i + 1)}' for i, pool in enumerate(DEMO_POOLS)]
    return "\n".join(lines) + "\n"


class DemoApp:
    def __init__(self, clock: Callable[[], float] = time.time):
        self.clock = clock
        self.started = clock()
        self.hidden: Set[str] = set()
        self.lock = threading.Lock()

    def metrics(self) -> str:
        with self.lock:
            return render_demo(self.clock() - self.started, self.hidden)

    def hide(self, queue: str) -> None:
        with self.lock:
            self.hidden.add(queue)


def make_demo_server(host: str, port: int, app: DemoApp) -> ThreadingHTTPServer:
    class Handler(BaseHTTPRequestHandler):
        def reply(self, status: int, body: str, content_type: str = "text/plain; charset=utf-8") -> None:
            data = body.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self) -> None:
            path = urllib.parse.urlsplit(self.path).path
            if path == "/metrics":
                self.reply(200, app.metrics(), TEXT_CONTENT_TYPE)
            elif path == "/":
                self.reply(200, "minitsdb demo: GET /metrics, POST /hide?queue=batch\n")
            else:
                self.reply(404, "not found\n")

        def do_POST(self) -> None:
            parts = urllib.parse.urlsplit(self.path)
            if parts.path != "/hide":
                self.reply(404, "not found\n")
                return
            queue = urllib.parse.parse_qs(parts.query).get("queue", ["batch"])[0]
            app.hide(queue)
            self.reply(200, f"hidden queue={queue}\n")

        def log_message(self, *args: Any) -> None:
            pass

    server = ThreadingHTTPServer((host, port), Handler)
    server.daemon_threads = True
    return server


def read_text(path: str) -> str:
    if path == "-":
        return sys.stdin.read()
    with open(path, encoding="utf-8") as handle:
        return handle.read()


def read_config(path: str) -> List[ScrapeConfig]:
    try:
        doc = json.loads(read_text(path))
    except json.JSONDecodeError as error:
        raise InputError(f"{path}: minitsdb reads the config as JSON (which is also valid YAML for Prometheus): {error}")
    return load_config(doc, lambda message: print(f"minitsdb: {message}", file=sys.stderr))


def emit(doc: Any) -> None:
    print(json.dumps(doc, indent=2, ensure_ascii=False))


def parse_time(text: str) -> int:
    if text == "now":
        return int(round(time.time() * 1000))
    try:
        return int(round(float(text) * 1000))
    except ValueError:
        raise InputError(f"--time takes Unix seconds or now, not {text!r}")


def open_for_reading(args: argparse.Namespace) -> TSDB:
    db = TSDB.open(args.data)
    if db.replay_problem:
        print(f"minitsdb: WAL replay stopped early: {db.replay_problem}", file=sys.stderr)
    retention = getattr(args, "retention", None)
    newest = db.max_time()
    if retention and newest is not None:
        db.apply_retention(newest - parse_duration(retention))
    return db


def family_json(family: Family) -> Dict[str, Any]:
    return {
        "name": family.name, "type": family.type, "help": family.help, "unit": family.unit, "series": len(family.samples),
        "samples": [{
            "name": s.name, "labels": s.labels, "value": format_value(s.value), "timestamp_ms": s.timestamp_ms,
            "exemplar": None if s.exemplar is None else {"labels": s.exemplar.labels, "value": format_value(s.exemplar.value), "timestamp": s.exemplar.timestamp},
        } for s in family.samples],
    }


def cmd_parse(args: argparse.Namespace) -> int:
    text = read_text(args.file)
    fmt = detect_format(text) if args.format == "auto" else args.format
    exposition = parse_exposition(text, fmt)
    problems = validate(exposition)
    if args.json:
        emit({"format": fmt, "families": [family_json(f) for f in exposition.families], "problems": problems})
    else:
        print(f"{fmt} format: {len(exposition.families)} families, {len(exposition.samples())} samples")
        for family in exposition.families:
            print(f"  {family.type:<14} {family.name:<44} {len(family.samples):>5} series" + (f"  {family.help}".replace("\n", "\\n") if family.help else ""))
        for problem in problems:
            print(f"problem: {problem}")
    return 1 if problems else 0


def cmd_lint(args: argparse.Namespace) -> int:
    problems = lint(parse_exposition(read_text(args.file), TEXT_FORMAT))
    if args.json:
        emit([{"metric": metric, "text": text} for metric, text in problems])
    else:
        for metric, text in problems:
            print(metric, text)
    return 1 if problems else 0


def cmd_targets(args: argparse.Namespace) -> int:
    active, dropped = discover(read_config(args.config))
    if args.json:
        emit(targets_document(active, dropped))
        return 0
    for target in active:
        print(f"active   {target.job:<12} {target.url:<40} {render_series(target.labels)}  every {format_duration(target.interval_ms)}, timeout {format_duration(target.timeout_ms)}")
    for job, labels in dropped:
        print(f"dropped  {job:<12} {labels.get('__address__', ''):<40} {render_series(labels)}")
    return 0


def adhoc_config(args: argparse.Namespace) -> List[ScrapeConfig]:
    interval = parse_duration(args.interval)
    return [ScrapeConfig(args.job, interval, min(interval, parse_duration(args.timeout)), groups=[StaticGroup(list(args.target), {})])]


def cmd_scrape(args: argparse.Namespace) -> int:
    if not args.config and not args.target:
        raise InputError("give --config prometheus.json or at least one --target host:port")
    configs = read_config(args.config) if args.config else adhoc_config(args)
    active, dropped = discover(configs)
    db = TSDB.open(args.data, writable=True)
    if db.replay_problem:
        print(f"minitsdb: WAL repaired: {db.replay_problem}", file=sys.stderr)
    stop = threading.Event()
    for signum in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(signum, lambda *_: stop.set())
        except ValueError:
            pass
    print(f"scraping {len(active)} target(s), {len(dropped)} dropped by relabel_configs; WAL in {os.path.join(args.data, 'wal')}", flush=True)
    retention = parse_duration(args.retention) if args.retention else None

    def show(report: ScrapeReport) -> None:
        if not args.quiet:
            print(report.line(), flush=True)

    scraper = Scraper(db, active)
    total = scraper.run(duration=args.duration, rounds=args.scrapes, should_stop=stop.is_set, report=show, retention_ms=retention)
    if db.wal is not None:
        db.wal.close()
    stats = db.chunk_stats()
    print(f"{total} scrapes, {stats['series']} series, {stats['samples']} samples in {stats['chunks']} chunks, {stats['bytes_per_sample']} bytes per sample", flush=True)
    return 0


def cmd_series(args: argparse.Namespace) -> int:
    db = open_for_reading(args)
    start = parse_time(args.start) if args.start else None
    end = parse_time(args.end) if args.end else None
    chosen: Dict[Labels, Series] = {}
    for selector in args.match or ['{__name__=~".+"}']:
        for series in db.select(parse_selector(selector)):
            if start is None and end is None or series.samples(start, end):
                chosen[series.labels] = series
    labels = [dict(key) for key in sorted(chosen)]
    if args.json:
        emit({"status": "success", "data": labels})
    else:
        for item in labels:
            print(render_series(item))
    return 0


def cmd_query(args: argparse.Namespace) -> int:
    db = open_for_reading(args)
    at = parse_time(args.time)
    results = db.instant(parse_selector(args.selector), at, parse_duration(args.lookback))
    if args.json:
        emit({"status": "success", "data": {"resultType": "vector", "result": [
            {"metric": dict(labels), "value": [at / 1000, format_value(value)]} for labels, _, value in results]}})
    else:
        for labels, sampled, value in results:
            print(f"{render_series(labels)} => {format_value(value)} (sample {(at - sampled) / 1000:g}s old)")
    return 0


def cmd_dump(args: argparse.Namespace) -> int:
    db = open_for_reading(args)
    chosen: Dict[Labels, Series] = {}
    for selector in args.match or ['{__name__=~".+"}']:
        for series in db.select(parse_selector(selector)):
            chosen[series.labels] = series
    rows = [{"labels": dict(key), "samples": [[t, format_value(bits_float(b)), b == STALE_NAN] for t, b in chosen[key].samples()]} for key in sorted(chosen)]
    if args.json:
        emit(rows)
    else:
        for row in rows:
            for t, value, stale in row["samples"]:
                print(f"{render_series(row['labels'])} {'stale' if stale else value} {t}")
    return 0


def cmd_cardinality(args: argparse.Namespace) -> int:
    report = open_for_reading(args).cardinality(args.top, args.max_values)
    if args.json:
        emit(report)
        return 0
    print(f"Total series: {report['total_series']}  label names: {report['label_names']}  label pairs: {report['label_pairs']}  postings entries: {report['postings_entries']}")
    for title, rows, key, value in (("Highest cardinality metric names", report["metrics"], "name", "series"),
                                    ("Highest cardinality labels", report["labels"], "label", "values"),
                                    ("Most common label pairs", report["pairs"], "pair", "series")):
        print(f"\n{title}:")
        for row in rows:
            print(f"{row[value]:>6} {row[key]}")
    print("\nWorst offenders:")
    if not report["offenders"]:
        print("  none: no label has unbounded-looking values")
    for o in report["offenders"]:
        print(f"  {o['label']}: {o['values']} values, {o['series']} series ({o['share'] * 100:.1f}% of all) in {', '.join(o['metrics'])}; each new value adds {o['series_per_value']:g} series")
    return 0


def cmd_chunks(args: argparse.Namespace) -> int:
    stats = open_for_reading(args).chunk_stats()
    if args.json:
        emit(stats)
        return 0
    ratio = stats["raw_bytes"] / stats["bytes"] if stats["bytes"] else 0
    print(f"{stats['series']} series, {stats['samples']} samples, {stats['chunks']} chunks, {stats['bytes']} bytes: {stats['bytes_per_sample']} bytes per sample, {ratio:.1f}x smaller than 16-byte (timestamp, float) pairs")
    for row in stats["worst"]:
        print(f"  {row['bytes'] / row['samples']:6.2f} B/sample  {row['samples']:>5} samples  {row['series']}")
    return 0


def cmd_encode(args: argparse.Namespace) -> int:
    chunk = XORChunk()
    rows = []
    previous_t: Optional[int] = None
    previous_delta = 0
    for index, item in enumerate(args.samples):
        t_text, _, v_text = item.partition(":")
        if not v_text or not INT_TEXT.fullmatch(t_text) or not FLOAT_TEXT.fullmatch(v_text):
            raise InputError(f"samples look like TIMESTAMP_MS:VALUE, for example 15000:1.5, not {item!r}")
        t, value = int(t_text), float(v_text)
        if previous_t is not None and t <= previous_t:
            raise InputError(f"timestamps must increase: {t} after {previous_t}")
        delta = t - previous_t if previous_t is not None else 0
        dod = delta - previous_delta if index >= 2 else None
        ts_bits, v_bits = chunk.append(t, value_bits(value))
        rows.append({"t": t, "value": format_value(value), "delta": delta if index else None, "dod": dod, "timestamp_bits": ts_bits, "value_bits": v_bits})
        previous_t, previous_delta = t, delta
    data = chunk.to_bytes()
    doc = {"samples": rows, "bytes": len(data), "raw_bytes": 16 * len(rows), "hex": data.hex(), "decoded": [[t, format_value(bits_float(b))] for t, b in decode_chunk(data)]}
    if args.json:
        emit(doc)
        return 0
    print(f"{'#':>3} {'timestamp':>14} {'value':>10} {'delta':>8} {'dod':>8} {'t bits':>7} {'v bits':>7}")
    for i, row in enumerate(rows):
        print(f"{i:>3} {row['t']:>14} {row['value']:>10} {'' if row['delta'] is None else row['delta']:>8} {'' if row['dod'] is None else row['dod']:>8} {row['timestamp_bits']:>7} {row['value_bits']:>7}")
    print(f"{len(data)} bytes for {len(rows)} samples (2-byte header included); raw: {16 * len(rows)} bytes")
    return 0


def cmd_demo(args: argparse.Namespace) -> int:
    server = make_demo_server(args.host, args.port, DemoApp())
    for signum in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(signum, lambda *_: threading.Thread(target=server.shutdown, daemon=True).start())
        except ValueError:
            pass
    print(f"demo app on http://{args.host}:{server.server_address[1]}/metrics (POST /hide?queue=batch to remove a series)", flush=True)
    try:
        server.serve_forever(poll_interval=0.2)
    finally:
        server.server_close()
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="minitsdb", description="Parse expositions, scrape targets and store samples the way Prometheus does.")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("parse", help="parse a /metrics page (text format or OpenMetrics) and check histograms")
    p.add_argument("file", help="exposition file, or - for stdin")
    p.add_argument("--format", choices=("auto", TEXT_FORMAT, OPENMETRICS), default="auto")
    p.add_argument("--json", action="store_true")
    p.set_defaults(run=cmd_parse)

    p = sub.add_parser("lint", help="the promlint rules that promtool check metrics applies")
    p.add_argument("file")
    p.add_argument("--json", action="store_true")
    p.set_defaults(run=cmd_lint)

    p = sub.add_parser("targets", help="targets after relabel_configs, in the shape of /api/v1/targets")
    p.add_argument("--config", required=True, help="prometheus.yml written as JSON")
    p.add_argument("--json", action="store_true")
    p.set_defaults(run=cmd_targets)

    p = sub.add_parser("scrape", help="scrape targets into a data directory (WAL)")
    p.add_argument("--config", help="prometheus.yml written as JSON")
    p.add_argument("--target", action="append", default=[], help="host:port to scrape without a config (repeatable)")
    p.add_argument("--job", default="minitsdb")
    p.add_argument("--interval", default="15s")
    p.add_argument("--timeout", default="10s")
    p.add_argument("--data", required=True)
    p.add_argument("--duration", type=float, help="stop after this many seconds")
    p.add_argument("--scrapes", type=int, help="stop after this many scrapes per target")
    p.add_argument("--retention", help="drop chunks older than this, for example 2h")
    p.add_argument("--quiet", action="store_true")
    p.set_defaults(run=cmd_scrape)

    for name, helptext, run in (("series", "series names and label sets, like /api/v1/series", cmd_series),
                                ("dump", "every stored sample including staleness markers", cmd_dump)):
        p = sub.add_parser(name, help=helptext)
        p.add_argument("--data", required=True)
        p.add_argument("--match", action="append", help='selector such as up{job="demo"} (repeatable)')
        p.add_argument("--retention")
        p.add_argument("--json", action="store_true")
        p.set_defaults(run=run)
        if name == "series":
            p.add_argument("--start", help="Unix seconds")
            p.add_argument("--end", help="Unix seconds")

    p = sub.add_parser("query", help="instant vector selector with the 5m lookback and staleness")
    p.add_argument("selector")
    p.add_argument("--data", required=True)
    p.add_argument("--time", default="now", help="Unix seconds or now")
    p.add_argument("--lookback", default="5m")
    p.add_argument("--retention")
    p.add_argument("--json", action="store_true")
    p.set_defaults(run=cmd_query)

    p = sub.add_parser("cardinality", help="series per metric, values per label, the worst offenders")
    p.add_argument("--data", required=True)
    p.add_argument("--top", type=int, default=10)
    p.add_argument("--max-values", type=int, default=20, help="flag labels with at least this many values")
    p.add_argument("--retention")
    p.add_argument("--json", action="store_true")
    p.set_defaults(run=cmd_cardinality)

    p = sub.add_parser("chunks", help="Gorilla compression: bytes per sample")
    p.add_argument("--data", required=True)
    p.add_argument("--retention")
    p.add_argument("--json", action="store_true")
    p.set_defaults(run=cmd_chunks)

    p = sub.add_parser("encode", help="encode TIMESTAMP_MS:VALUE samples into one XOR chunk and show the bits per sample")
    p.add_argument("samples", nargs="+")
    p.add_argument("--json", action="store_true")
    p.set_defaults(run=cmd_encode)

    p = sub.add_parser("demo", help="serve a /metrics page with known counters, gauges, a histogram and a summary")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=18481)
    p.set_defaults(run=cmd_demo)
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(sys.argv[1:] if argv is None else argv)
    try:
        return args.run(args)
    except (InputError, OSError) as error:
        print(f"minitsdb: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
