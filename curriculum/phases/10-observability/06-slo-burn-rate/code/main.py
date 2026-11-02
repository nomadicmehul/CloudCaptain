"""minislo: SLOs as error budgets. It reads Sloth's prometheus/v1 spec or an OpenSLO v1 SLO (YAML or JSON), does the error budget arithmetic, prints the multiwindow multi-burn-rate alert table from the Google SRE workbook, generates Prometheus recording and alerting rules in Sloth's naming convention, replays an error pattern through Prometheus' own rate() arithmetic to show when each alert fires and how much budget is left, applies an error budget policy, and writes a promtool unit test that checks any rule set against those predictions."""
from __future__ import annotations

import argparse
import json
import math
import re
import sys
from bisect import bisect_right
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Dict, List, Optional, Sequence, Tuple

SLI_METRIC = "slo:sli_error:ratio_rate"
OBJECTIVE_METRIC = "slo:objective:ratio"
BUDGET_METRIC = "slo:error_budget:ratio"
PERIOD_DAYS_METRIC = "slo:time_period:days"
CURRENT_BURN_METRIC = "slo:current_burn_rate:ratio"
PERIOD_BURN_METRIC = "slo:period_burn_rate:ratio"
REMAINING_METRIC = "slo:period_error_budget_remaining:ratio"
ID_LABEL, SLO_LABEL, SERVICE_LABEL, WINDOW_LABEL, SEVERITY_LABEL = "sloth_id", "sloth_slo", "sloth_service", "sloth_window", "sloth_severity"
SLI_GROUP, META_GROUP, ALERT_GROUP = "sloth-slo-sli-recordings-", "sloth-slo-meta-recordings-", "sloth-slo-alerts-"
PAGE, TICKET = "page", "ticket"
UNITS = {"ms": 0.001, "s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800, "y": 31536000}
DURATION_PART = re.compile(r"(\d+)(ms|s|m|h|d|w|y)")
DURATION_FULL = re.compile(r"(?:\d+(?:ms|s|m|h|d|w|y))+")
CALENDAR_UNITS = {"w": ("week", 7 * 86400), "M": ("month", 30 * 86400), "Q": ("quarter", 90 * 86400)}
NAME_PATTERN = re.compile(r"^[A-Za-z0-9][-A-Za-z0-9_.]*[A-Za-z0-9]$")
LABEL_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
WINDOW_VARIABLE = re.compile(r"\{\{\s*\.window\s*\}\}")
SELECTOR_ONLY = re.compile(r"^[A-Za-z_:][A-Za-z0-9_:]*(\{[^{}]*\})?$")
TEMPLATE_LABEL = re.compile(r"\{\{-?\s*(?:\$labels|\.Labels)\.([A-Za-z_][A-Za-z0-9_]*)\s*-?\}\}")
TEMPLATE_ANY = re.compile(r"\{\{.*?\}\}")
ALERT_CONDITION = re.compile(SLI_METRIC.replace(":", r"\:") + r"([0-9a-z]+)\{[^}]*\}\s*>\s*(\([0-9.eE+\-*/\s]+\)|[0-9.eE+\-]+)")
PLAIN_SAFE = re.compile(r"[A-Za-z_./][A-Za-z0-9_./:@-]*")
KEY_SAFE = re.compile(r"[A-Za-z_][A-Za-z0-9_./-]*")
INT_PLAIN = re.compile(r"[-+]?[0-9]+")
FLOAT_PLAIN = re.compile(r"[-+]?(\.[0-9]+|[0-9]+(\.[0-9]*)?)([eE][-+]?[0-9]+)?")
ESCAPES = {"0": "\0", "a": "\a", "b": "\b", "t": "\t", "\t": "\t", "n": "\n", "v": "\v", "f": "\f", "r": "\r", "e": "\x1b", " ": " ", '"': '"', "/": "/", "\\": "\\", "N": "\x85", "_": "\xa0", "L": " ", "P": " "}
DEFAULT_TITLE = "({severity}) {{{{$labels.sloth_service}}}} {{{{$labels.sloth_slo}}}} SLO error budget burn rate is too fast."
DEFAULT_SUMMARY = "{{$labels.sloth_service}} {{$labels.sloth_slo}} SLO error budget burn rate is over expected."
AMBIGUOUS = 1e-6


class InputError(ValueError):
    pass


def parse_duration(text: Any) -> float:
    raw = str(text).strip()
    if not DURATION_FULL.fullmatch(raw):
        raise InputError(f"not a duration: {raw!r} (examples: 5m, 1h, 6h, 1d, 30d, 4w, 1h30m)")
    return float(sum(int(count) * UNITS[unit] for count, unit in DURATION_PART.findall(raw)))


def prom_duration(seconds: float) -> str:
    ms = int(round(seconds * 1000))
    if ms == 0:
        return "0s"
    out = ""
    for unit, size, exact in (("y", 31536000000, True), ("w", 604800000, True), ("d", 86400000, False), ("h", 3600000, False), ("m", 60000, False), ("s", 1000, False), ("ms", 1, False)):
        if exact and ms % size:
            continue
        count = ms // size
        if count:
            out += f"{count}{unit}"
            ms -= count * size
    return out


def human_duration(seconds: float) -> str:
    return prom_duration(round(seconds)) if seconds >= 1 else f"{seconds:.2g}s"


def format_number(value: float) -> str:
    return f"{value:.12g}"


def percent(value: Optional[float], digits: int = 1) -> str:
    if value is None or math.isnan(value):
        return "-"
    return f"{value * 100:.{digits}f}%"


class YamlError(InputError):
    pass


def parse_yaml(text: str) -> List[Any]:
    documents: List[List[str]] = [[]]
    for raw in text.replace("\r\n", "\n").split("\n"):
        if raw.rstrip() == "---" or raw.startswith("--- "):
            documents.append([raw[4:]] if raw[4:].strip() else [])
        elif raw.rstrip() != "..." and not raw.startswith("%"):
            documents[-1].append(raw)
    out = []
    for lines in documents:
        value = YamlParser(lines).document()
        if value is not None:
            out.append(value)
    return out


def is_item(text: str) -> bool:
    return text == "-" or text.startswith("- ")


def closing_quote(text: str, quote: str) -> Optional[int]:
    i = 0
    while i < len(text):
        if quote == '"' and text[i] == "\\":
            i += 2
            continue
        if text[i] == quote:
            if quote == "'" and text[i + 1:i + 2] == "'":
                i += 2
                continue
            return i
        i += 1
    return None


def split_key(text: str) -> Optional[Tuple[str, str]]:
    if not text or text[0] in "[{" or is_item(text):
        return None
    if text[0] in "\"'":
        end = closing_quote(text[1:], text[0])
        if end is None:
            return None
        after = text[end + 2:].lstrip()
        if after == ":" or after.startswith(": "):
            return unquote(text[:end + 2]), after[1:].strip()
        return None
    start = 0
    while True:
        colon = text.find(":", start)
        if colon < 0:
            return None
        if colon + 1 == len(text) or text[colon + 1] == " ":
            key = text[:colon].rstrip()
            return (key, text[colon + 1:].strip()) if key and " #" not in key else None
        start = colon + 1


def strip_comment(text: str) -> str:
    if text.startswith("#"):
        return ""
    hit = text.find(" #")
    return (text[:hit] if hit >= 0 else text).strip()


def unquote(token: str) -> str:
    body = token[1:-1]
    if token[0] == "'":
        return body.replace("''", "'")
    return unescape(body)


def unescape(body: str) -> str:
    out, i = [], 0
    while i < len(body):
        char = body[i]
        if char != "\\":
            out.append(char)
            i += 1
            continue
        code = body[i + 1:i + 2]
        if code in ESCAPES:
            out.append(ESCAPES[code])
            i += 2
        elif code in ("x", "u", "U"):
            width = {"x": 2, "u": 4, "U": 8}[code]
            digits = body[i + 2:i + 2 + width]
            if len(digits) != width or not all(c in "0123456789abcdefABCDEF" for c in digits):
                raise YamlError(f"bad escape \\{code}{digits} in a double-quoted string")
            out.append(chr(int(digits, 16)))
            i += 2 + width
        else:
            raise YamlError(f"unknown escape \\{code} in a double-quoted string")
    return "".join(out)


def resolve_plain(text: str) -> Any:
    if text in ("", "~", "null", "Null", "NULL"):
        return None
    if text in ("true", "True", "TRUE"):
        return True
    if text in ("false", "False", "FALSE"):
        return False
    if INT_PLAIN.fullmatch(text):
        return int(text)
    if FLOAT_PLAIN.fullmatch(text):
        return float(text)
    if text.lower() in (".inf", "+.inf"):
        return math.inf
    if text.lower() == "-.inf":
        return -math.inf
    if text.lower() == ".nan":
        return math.nan
    return text


def fold_lines(lines: Sequence[str], escapes: bool = False) -> str:
    out = ""
    breaks = 0
    joined = False
    for index, raw in enumerate(lines):
        line = raw if index == 0 else raw.lstrip(" \t")
        if index < len(lines) - 1:
            line = line.rstrip(" \t")
        if index > 0 and not line and index < len(lines) - 1:
            breaks += 1
            continue
        if index > 0:
            if breaks:
                out += "\n" * breaks
            elif not joined:
                out += " "
        breaks = 0
        joined = False
        if escapes and index < len(lines) - 1 and (len(line) - len(line.rstrip("\\"))) % 2 == 1:
            line, joined = line[:-1], True
        out += line
    return out


def fold_block(lines: Sequence[str]) -> str:
    out = ""
    previous: Optional[str] = None
    for line in lines:
        if previous is None:
            out = line
        elif line == "":
            out += "\n"
        elif previous == "" or line.startswith(" ") or previous.startswith(" "):
            out += ("" if previous == "" else "\n") + line
        else:
            out += " " + line
        previous = line
    return out


def parse_flow(text: str, i: int) -> Tuple[Any, int]:
    while i < len(text) and text[i] == " ":
        i += 1
    if i >= len(text):
        raise YamlError(f"unterminated flow collection: {text!r}")
    if text[i] in "[{":
        closer = "]" if text[i] == "[" else "}"
        items: List[Any] = []
        mapping: Dict[str, Any] = {}
        i += 1
        while True:
            while i < len(text) and text[i] == " ":
                i += 1
            if i < len(text) and text[i] == closer:
                return (items if closer == "]" else mapping), i + 1
            if closer == "]":
                value, i = parse_flow(text, i)
                items.append(value)
            else:
                key, i = flow_scalar(text, i, ":")
                if i >= len(text) or text[i] != ":":
                    raise YamlError(f"expected ':' after key {key!r} in {text!r}")
                value, i = parse_flow(text, i + 1)
                mapping[str(key)] = value
            while i < len(text) and text[i] == " ":
                i += 1
            if i < len(text) and text[i] == ",":
                i += 1
            elif i >= len(text) or text[i] != closer:
                raise YamlError(f"expected ',' or '{closer}' in {text!r}")
    return flow_scalar(text, i, "")


def flow_scalar(text: str, i: int, extra_stop: str) -> Tuple[Any, int]:
    if text[i] in "\"'":
        end = closing_quote(text[i + 1:], text[i])
        if end is None:
            raise YamlError(f"unterminated quoted string in {text!r}")
        return unquote(text[i:i + end + 2]), i + end + 2
    start = i
    while i < len(text) and text[i] not in ",]}" and not (text[i] in extra_stop and (i + 1 == len(text) or text[i + 1] in " ,}")):
        i += 1
    return resolve_plain(text[start:i].strip()), i


class YamlParser:
    def __init__(self, lines: Sequence[str]) -> None:
        self.lines = list(lines)
        self.pos = 0

    def fail(self, message: str) -> YamlError:
        return YamlError(f"YAML line {self.pos + 1}: {message}")

    def peek(self) -> Optional[Tuple[int, str]]:
        while self.pos < len(self.lines):
            line = self.lines[self.pos]
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                self.pos += 1
                continue
            indent = len(line) - len(line.lstrip(" "))
            if line[indent] == "\t":
                raise self.fail("tabs are not allowed for indentation")
            return indent, line[indent:].rstrip()
        return None

    def document(self) -> Any:
        head = self.peek()
        if head is None:
            return None
        value = self.node(head[0])
        if self.peek() is not None:
            raise self.fail(f"unexpected content {self.peek()[1]!r}; check the indentation")
        return value

    def node(self, indent: int) -> Any:
        head = self.peek()
        if head is None or head[0] < indent:
            return None
        column, text = head
        if is_item(text):
            return self.sequence(column)
        if split_key(text) is not None:
            return self.mapping(column)
        self.pos += 1
        return self.scalar(text, column - 1)

    def mapping(self, indent: int) -> Dict[str, Any]:
        result: Dict[str, Any] = {}
        while True:
            head = self.peek()
            if head is None or head[0] < indent or (head[0] == indent and is_item(head[1])):
                return result
            column, text = head
            if column > indent:
                raise self.fail(f"unexpected indentation before {text!r}")
            pair = split_key(text)
            if pair is None:
                raise self.fail(f"expected 'key: value', found {text!r}")
            key, rest = pair
            if key in result:
                raise self.fail(f"duplicate key {key!r}")
            self.pos += 1
            result[key] = self.value(rest, indent)

    def value(self, rest: str, indent: int) -> Any:
        if not rest or rest.startswith("#"):
            head = self.peek()
            if head is None:
                return None
            if head[0] > indent:
                return self.node(head[0])
            if head[0] == indent and is_item(head[1]):
                return self.sequence(indent)
            return None
        if rest[0] in "|>":
            return self.block(rest, indent)
        return self.scalar(rest, indent)

    def sequence(self, indent: int) -> List[Any]:
        items: List[Any] = []
        while True:
            head = self.peek()
            if head is None or head[0] != indent or not is_item(head[1]):
                if head is not None and head[0] > indent:
                    raise self.fail(f"unexpected indentation before {head[1]!r}")
                return items
            text = head[1]
            rest = text[1:].lstrip()
            if not rest or rest.startswith("#"):
                self.pos += 1
                following = self.peek()
                items.append(self.node(following[0]) if following and following[0] > indent else None)
                continue
            offset = indent + len(text) - len(rest)
            if is_item(rest) or split_key(rest) is not None:
                self.lines[self.pos] = " " * offset + rest
                items.append(self.node(offset))
                continue
            self.pos += 1
            items.append(self.block(rest, indent) if rest[0] in "|>" else self.scalar(rest, indent))

    def block(self, header: str, indent: int) -> str:
        match = re.fullmatch(r"([|>])([+-]?)([1-9]?)([+-]?)\s*(#.*)?", header)
        if not match:
            raise self.fail(f"bad block scalar header {header!r}")
        style, chomp = match.group(1), match.group(2) or match.group(4)
        width: Optional[int] = indent + int(match.group(3)) if match.group(3) else None
        body: List[str] = []
        while self.pos < len(self.lines):
            line = self.lines[self.pos]
            if not line.strip():
                body.append("")
                self.pos += 1
                continue
            column = len(line) - len(line.lstrip(" "))
            if width is None:
                if column <= indent:
                    break
                width = column
            if column < width:
                break
            body.append(line[width:])
            self.pos += 1
        end = len(body)
        while end and body[end - 1] == "":
            end -= 1
        content, trailing = body[:end], body[end:]
        text = "\n".join(content) if style == "|" else fold_block(content)
        if chomp == "-" or not content:
            return text if chomp != "+" else "\n" * len(trailing)
        return text + "\n" + ("\n" * len(trailing) if chomp == "+" else "")

    def scalar(self, rest: str, indent: int) -> Any:
        if rest[0] in "\"'":
            return self.quoted(rest)
        if rest[0] in "[{":
            text = strip_comment(rest)
            while text.count(rest[0]) > text.count("]" if rest[0] == "[" else "}") and self.pos < len(self.lines):
                text += " " + self.lines[self.pos].strip()
                self.pos += 1
            value, end = parse_flow(text, 0)
            if strip_comment(text[end:].strip()):
                raise self.fail(f"unexpected text after a flow collection: {text[end:]!r}")
            return value
        if rest[0] in "&*!":
            raise self.fail("anchors, aliases and tags are not supported")
        parts = [strip_comment(rest)]
        breaks = 0
        while self.pos < len(self.lines):
            line = self.lines[self.pos]
            stripped = line.strip()
            if not stripped:
                breaks += 1
                self.pos += 1
                continue
            column = len(line) - len(line.lstrip(" "))
            if column <= indent or stripped.startswith("#") or ": " in stripped or stripped.endswith(":"):
                self.pos -= breaks
                break
            parts.append(("\n" * breaks if breaks else " ") + strip_comment(stripped))
            breaks = 0
            self.pos += 1
        return resolve_plain("".join(parts))

    def quoted(self, rest: str) -> str:
        quote = rest[0]
        pieces: List[str] = []
        current = rest[1:]
        while True:
            end = closing_quote(current, quote)
            if end is not None:
                pieces.append(current[:end])
                after = current[end + 1:].strip()
                break
            pieces.append(current)
            if self.pos >= len(self.lines):
                raise self.fail("unterminated quoted string")
            current = self.lines[self.pos]
            self.pos += 1
        if after and not after.startswith("#"):
            raise self.fail(f"unexpected text after a closing quote: {after!r}")
        text = fold_lines(pieces, escapes=quote == '"')
        return text.replace("''", "'") if quote == "'" else unescape(text)


def yaml_scalar(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        if math.isnan(value):
            return ".nan"
        if math.isinf(value):
            return ".inf" if value > 0 else "-.inf"
        return repr(value)
    text = str(value)
    if PLAIN_SAFE.fullmatch(text) and not text.endswith(":") and resolve_plain(text) == text:
        return text
    return json.dumps(text, ensure_ascii=False)


def yaml_key(key: Any) -> str:
    text = str(key)
    return text if KEY_SAFE.fullmatch(text) and resolve_plain(text) == text else json.dumps(text, ensure_ascii=False)


def yaml_block(text: str, indent: int) -> Optional[List[str]]:
    if "\n" not in text or text.startswith((" ", "\n")) or "\t" in text or text.rstrip("\n") != text.rstrip("\n").rstrip(" "):
        return None
    body = text[:-1] if text.endswith("\n") else text
    if body.endswith("\n"):
        return None
    header = "|" if text.endswith("\n") else "|-"
    return [header] + [(" " * indent + line) if line else "" for line in body.split("\n")]


def dump_yaml(value: Any) -> str:
    lines: List[str] = []
    emit_yaml(value, 0, lines)
    return "\n".join(lines) + "\n"


def emit_yaml(value: Any, indent: int, lines: List[str]) -> None:
    pad = " " * indent
    if isinstance(value, dict):
        if not value:
            lines.append(pad + "{}")
        for key, item in value.items():
            emit_entry(pad + yaml_key(key) + ":", item, indent, lines)
    elif isinstance(value, list):
        if not value:
            lines.append(pad + "[]")
        for item in value:
            if isinstance(item, (dict, list)) and item:
                sub: List[str] = []
                emit_yaml(item, indent + 2, sub)
                sub[0] = pad + "- " + sub[0][indent + 2:]
                lines.extend(sub)
            else:
                emit_entry(pad + "-", item, indent, lines)
    else:
        lines.append(pad + yaml_scalar(value))


def emit_entry(prefix: str, item: Any, indent: int, lines: List[str]) -> None:
    if isinstance(item, (dict, list)) and item:
        lines.append(prefix)
        emit_yaml(item, indent + 2, lines)
        return
    if isinstance(item, dict):
        lines.append(prefix + " {}")
        return
    if isinstance(item, list):
        lines.append(prefix + " []")
        return
    block = yaml_block(item, indent + 2) if isinstance(item, str) else None
    if block:
        lines.append(prefix + " " + block[0])
        lines.extend(block[1:])
    else:
        lines.append(prefix + " " + yaml_scalar(item))


def load_documents(text: str) -> List[Any]:
    if text.lstrip().startswith(("{", "[")):
        try:
            doc = json.loads(text)
        except json.JSONDecodeError as exc:
            raise InputError(f"not valid JSON: {exc}") from None
        return doc if isinstance(doc, list) else [doc]
    return parse_yaml(text)


def read_text(path: str) -> str:
    try:
        if path == "-":
            return sys.stdin.read()
        with open(path, encoding="utf-8") as handle:
            return handle.read()
    except OSError as exc:
        raise InputError(f"cannot read {path}: {exc.strerror}") from None


@dataclass
class AlertMeta:
    name: str
    labels: Dict[str, str] = field(default_factory=dict)
    annotations: Dict[str, str] = field(default_factory=dict)


@dataclass
class SLO:
    service: str
    name: str
    objective: float
    period: float
    error_query: str = ""
    total_query: str = ""
    error_ratio_query: str = ""
    labels: Dict[str, str] = field(default_factory=dict)
    page: Optional[AlertMeta] = None
    ticket: Optional[AlertMeta] = None
    description: str = ""
    calendar: str = ""
    source: str = ""

    @property
    def id(self) -> str:
        return f"{self.service}-{self.name}"

    @property
    def target(self) -> float:
        return float(Decimal(repr(self.objective)) / 100)

    @property
    def budget(self) -> float:
        return float((Decimal(100) - Decimal(repr(self.objective))) / 100)

    def id_labels(self) -> Dict[str, str]:
        return {ID_LABEL: self.id, SERVICE_LABEL: self.service, SLO_LABEL: self.name}

    def alert(self, severity: str) -> Optional[AlertMeta]:
        return self.page if severity == PAGE else self.ticket


def string_map(value: Any, where: str) -> Dict[str, str]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise InputError(f"{where} must be a mapping of strings")
    return {str(k): str(v) for k, v in value.items()}


def merge(*maps: Dict[str, str]) -> Dict[str, str]:
    out: Dict[str, str] = {}
    for item in maps:
        out.update(item)
    return out


def window_query(query: str) -> str:
    query = str(query).strip()
    if WINDOW_VARIABLE.search(query):
        return query
    if SELECTOR_ONLY.fullmatch(query):
        return f"sum(rate({query}[{{{{.window}}}}]))"
    raise InputError(f"query {query!r} needs a {{{{.window}}}} placeholder, or must be a bare counter selector that minislo wraps in sum(rate(...))")


def from_sloth(doc: Dict[str, Any], period: float) -> List[SLO]:
    service = str(doc.get("service") or "")
    common = string_map(doc.get("labels"), "labels")
    out = []
    for index, item in enumerate(doc.get("slos") or []):
        where = f"slos[{index}]"
        if not isinstance(item, dict):
            raise InputError(f"{where} must be a mapping")
        sli = item.get("sli") or {}
        if sli.get("plugin"):
            raise InputError(f"{where}.sli.plugin: SLI plugins are Sloth-specific; minislo reads sli.events and sli.raw")
        events, raw = sli.get("events") or {}, sli.get("raw") or {}
        alerting = item.get("alerting") or {}

        def meta(kind: str) -> Optional[AlertMeta]:
            sub = alerting.get(f"{kind}_alert") or {}
            if sub.get("disable"):
                return None
            return AlertMeta(
                str(alerting.get("name") or ""),
                merge(string_map(alerting.get("labels"), f"{where}.alerting.labels"), string_map(sub.get("labels"), f"{where}.alerting.{kind}_alert.labels")),
                merge(string_map(alerting.get("annotations"), f"{where}.alerting.annotations"), string_map(sub.get("annotations"), f"{where}.alerting.{kind}_alert.annotations")),
            )

        objective = item.get("objective")
        if isinstance(objective, bool) or not isinstance(objective, (int, float)):
            raise InputError(f"{where}.objective must be a number of percent such as 99.9, got {objective!r}")
        out.append(SLO(
            service=service,
            name=str(item.get("name") or ""),
            objective=float(objective),
            period=period,
            error_query=str(events.get("error_query") or ""),
            total_query=str(events.get("total_query") or ""),
            error_ratio_query=str(raw.get("error_ratio_query") or ""),
            labels=merge(common, string_map(item.get("labels"), f"{where}.labels")),
            page=meta(PAGE),
            ticket=meta(TICKET),
            description=str(item.get("description") or ""),
            source="sloth prometheus/v1",
        ))
    return out


def openslo_query(metric: Any, where: str) -> str:
    source = (metric or {}).get("metricSource") or {}
    query = (source.get("spec") or {}).get("query") or (metric or {}).get("query")
    if not query:
        raise InputError(f"{where}: needs metricSource.spec.query")
    return window_query(query)


def openslo_window(spec: Dict[str, Any]) -> Tuple[float, str]:
    windows = spec.get("timeWindow") or spec.get("timeWindows") or []
    if len(windows) != 1:
        raise InputError("spec.timeWindow must list exactly one window")
    window = windows[0]
    if "count" in window:
        if window.get("unit") not in ("Day", "day", "d"):
            raise InputError("openslo/v1alpha timeWindows must be counted in Day")
        return float(window["count"]) * 86400, ""
    duration = str(window.get("duration") or "")
    rolling = window.get("isRolling", "calendar" not in window)
    if not rolling:
        if duration[:-1] != "1" or duration[-1:] not in CALENDAR_UNITS:
            raise InputError(f"calendar windows must be 1w, 1M or 1Q, got {duration!r}")
        name, seconds = CALENDAR_UNITS[duration[-1]]
        return float(seconds), name
    if duration.endswith(("M", "Q", "Y")):
        raise InputError(f"a rolling window is counted in d or w, got {duration!r}; use isRolling: false for calendar months")
    return parse_duration(duration), ""


def camel(text: str) -> str:
    return "".join(part[:1].upper() + part[1:] for part in re.split(r"[-_. ]+", text) if part)


def from_openslo(doc: Dict[str, Any]) -> SLO:
    spec = doc.get("spec") or {}
    name = str((doc.get("metadata") or {}).get("name") or "")
    if spec.get("indicatorRef"):
        raise InputError(f"SLO {name}: inline the SLI under spec.indicator; minislo reads one file")
    method = spec.get("budgetingMethod", "Occurrences")
    if method != "Occurrences":
        raise InputError(f"SLO {name}: budgetingMethod {method} is not supported; burn-rate alerts count events (Occurrences)")
    objectives = spec.get("objectives") or []
    if len(objectives) != 1:
        raise InputError(f"SLO {name}: exactly one objective is supported, found {len(objectives)}")
    objective = objectives[0]
    if "targetPercent" in objective:
        percent_value = float(objective["targetPercent"])
    elif "target" in objective:
        percent_value = float(Decimal(repr(float(objective["target"]))) * 100)
    else:
        raise InputError(f"SLO {name}: the objective needs target or targetPercent")
    if doc.get("apiVersion") == "openslo/v1alpha":
        ratio = objective.get("ratioMetrics") or {}
    else:
        indicator = (spec.get("indicator") or {}).get("spec") or {}
        if indicator.get("thresholdMetric"):
            raise InputError(f"SLO {name}: thresholdMetric is not an event ratio; use ratioMetric")
        ratio = indicator.get("ratioMetric") or {}
    period, calendar = openslo_window(spec)
    alert_name = camel(f"{spec.get('service', '')}-{name}") + "BudgetBurn"
    slo = SLO(service=str(spec.get("service") or ""), name=name, objective=percent_value, period=period, calendar=calendar,
              description=str(spec.get("description") or ""), page=AlertMeta(alert_name), ticket=AlertMeta(alert_name), source=str(doc.get("apiVersion")))
    if ratio.get("total") and ratio.get("bad"):
        slo.error_query, slo.total_query = openslo_query(ratio["bad"], f"SLO {name} bad"), openslo_query(ratio["total"], f"SLO {name} total")
    elif ratio.get("total") and ratio.get("good"):
        good, total = openslo_query(ratio["good"], f"SLO {name} good"), openslo_query(ratio["total"], f"SLO {name} total")
        slo.error_ratio_query = f"1 - (({good}) / ({total}))"
    else:
        raise InputError(f"SLO {name}: ratioMetric needs total and one of good or bad")
    return slo


def validate(slo: SLO) -> None:
    where = f"SLO {slo.id}"
    for what, value in (("service", slo.service), ("name", slo.name)):
        if not NAME_PATTERN.match(value):
            raise InputError(f"{where}: {what} {value!r} must start and end with a letter or digit and contain only letters, digits, '.', '_' and '-'")
    if not 0 < slo.objective < 100:
        raise InputError(f"{where}: objective {slo.objective} must be above 0 and below 100 percent; 100% leaves no error budget to burn")
    if bool(slo.error_query or slo.total_query) == bool(slo.error_ratio_query):
        raise InputError(f"{where}: give either sli.events (error_query and total_query) or sli.raw (error_ratio_query)")
    if not slo.error_ratio_query:
        for what, query in (("error_query", slo.error_query), ("total_query", slo.total_query)):
            if not query:
                raise InputError(f"{where}: sli.events.{what} is empty")
    for what, query in (("error_query", slo.error_query), ("total_query", slo.total_query), ("error_ratio_query", slo.error_ratio_query)):
        if query and not WINDOW_VARIABLE.search(query):
            raise InputError(f"{where}: {what} must contain the {{{{.window}}}} placeholder, for example rate(x[{{{{.window}}}}])")
    if slo.error_query and slo.error_query == slo.total_query:
        raise InputError(f"{where}: error_query and total_query are the same query")
    for key, value in slo.labels.items():
        if not LABEL_NAME.match(key) or not value:
            raise InputError(f"{where}: label {key}={value!r} is not a valid Prometheus label")
    for severity in (PAGE, TICKET):
        meta = slo.alert(severity)
        if meta is not None and not meta.name:
            raise InputError(f"{where}: alerting.name is required unless {severity}_alert.disable is true")


def load_slos(text: str, period: str = "30d", extra_labels: Optional[Dict[str, str]] = None) -> List[SLO]:
    slos: List[SLO] = []
    for doc in load_documents(text):
        if not isinstance(doc, dict):
            raise InputError("every document must be a mapping")
        if doc.get("version") == "prometheus/v1":
            slos.extend(from_sloth(doc, parse_duration(period)))
        elif str(doc.get("apiVersion", "")).startswith("openslo/") and doc.get("kind") == "SLO":
            slos.append(from_openslo(doc))
        elif str(doc.get("apiVersion", "")).startswith("openslo/"):
            continue
        else:
            raise InputError("not an SLO spec: expected version: prometheus/v1 (Sloth) or apiVersion: openslo/v1 with kind: SLO")
    if not slos:
        raise InputError("the spec contains no SLO")
    for slo in slos:
        slo.labels.update(extra_labels or {})
        validate(slo)
    return slos


def pick(slos: List[SLO], name: Optional[str]) -> SLO:
    if name:
        for slo in slos:
            if name in (slo.name, slo.id):
                return slo
        raise InputError(f"no SLO named {name!r}; the spec has {', '.join(s.id for s in slos)}")
    if len(slos) != 1:
        raise InputError(f"the spec has {len(slos)} SLOs ({', '.join(s.id for s in slos)}); choose one with --slo")
    return slos[0]


@dataclass
class AlertWindow:
    severity: str
    speed: str
    budget_percent: float
    short: float
    long: float


GOOGLE_WINDOWS = (
    AlertWindow(PAGE, "quick", 2, 300, 3600),
    AlertWindow(PAGE, "slow", 5, 1800, 21600),
    AlertWindow(TICKET, "quick", 10, 7200, 86400),
    AlertWindow(TICKET, "slow", 10, 21600, 259200),
)


@dataclass
class TableRow:
    severity: str
    speed: str
    budget_percent: float
    short: float
    long: float
    factor: float
    threshold: float
    threshold_text: str

    def detection(self, error_ratio: float) -> Optional[float]:
        return self.long * self.threshold / error_ratio if error_ratio > self.threshold else None

    def as_dict(self, budget: float, period: float) -> Dict[str, Any]:
        return {
            "severity": self.severity, "speed": self.speed, "budget_percent": self.budget_percent,
            "long": prom_duration(self.long), "short": prom_duration(self.short), "burn_factor": self.factor,
            "error_ratio_threshold": self.threshold, "detection_seconds_at_100_percent_errors": self.detection(1.0),
            "budget_spent_when_it_fires": self.long * self.factor / period,
        }


def burn_factor(window: AlertWindow, period: float) -> float:
    return (window.budget_percent * (period / 3600) / 100) / (window.long / 3600)


def alert_table(slo: SLO, windows: Sequence[AlertWindow] = GOOGLE_WINDOWS) -> List[TableRow]:
    rows = []
    for window in windows:
        factor = burn_factor(window, slo.period)
        text = format_number(factor * slo.budget)
        rows.append(TableRow(window.severity, window.speed, window.budget_percent, window.short, window.long, factor, float(text), text))
    return rows


def sli_windows(rows: Sequence[TableRow]) -> List[float]:
    return sorted({row.short for row in rows} | {row.long for row in rows})


def label_selector(labels: Dict[str, str]) -> str:
    return "{" + ", ".join(f"{k}={json.dumps(v)}" for k, v in sorted(labels.items())) + "}"


def render_window(query: str, seconds: float) -> str:
    return WINDOW_VARIABLE.sub(prom_duration(seconds), query)


def sli_expression(slo: SLO, window: float) -> str:
    if slo.error_ratio_query:
        return f"({render_window(slo.error_ratio_query, window)})"
    return f"({render_window(slo.error_query, window)})\n/\n({render_window(slo.total_query, window)})\n"


def period_expression(slo: SLO, short: float) -> str:
    selector = f"{SLI_METRIC}{prom_duration(short)}{label_selector(slo.id_labels())}"
    period = prom_duration(slo.period)
    return f"sum_over_time({selector}[{period}])\n/ ignoring ({WINDOW_LABEL})\ncount_over_time({selector}[{period}])\n"


def alert_expression(slo: SLO, quick: TableRow, slow: TableRow) -> str:
    ids = label_selector(slo.id_labels())

    def burning(window: float, row: TableRow) -> str:
        return f"max without ({WINDOW_LABEL}) ({SLI_METRIC}{prom_duration(window)}{ids} > {row.threshold_text})"

    return (f"(\n  {burning(quick.short, quick)}\n  and\n  {burning(quick.long, quick)}\n)\nor\n"
            f"(\n  {burning(slow.short, slow)}\n  and\n  {burning(slow.long, slow)}\n)\n")


def default_annotations(severity: str) -> Dict[str, str]:
    return {"title": DEFAULT_TITLE.format(severity=severity), "summary": DEFAULT_SUMMARY}


def generate_groups(slo: SLO, windows: Sequence[AlertWindow] = GOOGLE_WINDOWS, optimize: bool = True) -> List[Dict[str, Any]]:
    rows = alert_table(slo, windows)
    short = rows[0].short
    ids = slo.id_labels()
    selector = label_selector(ids)
    sli_rules = []
    for window in sli_windows(rows) + ([slo.period] if slo.period not in sli_windows(rows) else []):
        expr = period_expression(slo, short) if optimize and window == slo.period else sli_expression(slo, window)
        sli_rules.append({"record": f"{SLI_METRIC}{prom_duration(window)}", "expr": expr, "labels": merge(ids, {WINDOW_LABEL: prom_duration(window)}, slo.labels)})
    meta_labels = merge(ids, slo.labels)
    on = f"on({ID_LABEL}, {SLO_LABEL}, {SERVICE_LABEL}) group_left"

    def burn(window: float) -> str:
        return f"{SLI_METRIC}{prom_duration(window)}{selector}\n/ {on}\n{BUDGET_METRIC}{selector}\n"

    meta_rules = [
        {"record": OBJECTIVE_METRIC, "expr": f"vector({format_number(slo.target)})", "labels": meta_labels},
        {"record": BUDGET_METRIC, "expr": f"vector({format_number(slo.budget)})", "labels": meta_labels},
        {"record": PERIOD_DAYS_METRIC, "expr": f"vector({format_number(slo.period / 86400)})", "labels": meta_labels},
        {"record": CURRENT_BURN_METRIC, "expr": burn(short), "labels": meta_labels},
        {"record": PERIOD_BURN_METRIC, "expr": burn(slo.period), "labels": meta_labels},
        {"record": REMAINING_METRIC, "expr": f"1 - {PERIOD_BURN_METRIC}{selector}", "labels": meta_labels},
    ]
    alert_rules = []
    for severity in (PAGE, TICKET):
        meta = slo.alert(severity)
        pair = [row for row in rows if row.severity == severity]
        if meta is None or len(pair) != 2:
            continue
        alert_rules.append({
            "alert": meta.name,
            "expr": alert_expression(slo, pair[0], pair[1]),
            "labels": merge({SEVERITY_LABEL: severity}, meta.labels),
            "annotations": merge(default_annotations(severity), meta.annotations),
        })
    groups = [{"name": SLI_GROUP + slo.id, "rules": sli_rules}, {"name": META_GROUP + slo.id, "rules": meta_rules}]
    if alert_rules:
        groups.append({"name": ALERT_GROUP + slo.id, "rules": alert_rules})
    return groups


def rules_text(slos: Sequence[SLO], source: str, optimize: bool = True) -> str:
    groups = [group for slo in slos for group in generate_groups(slo, optimize=optimize)]
    return f"# Generated by minislo from {source}. Edit the SLO spec, not this file.\n" + dump_yaml({"groups": groups})


def arithmetic(text: str) -> float:
    tokens = re.findall(r"\d+\.?\d*(?:[eE][-+]?\d+)?|\.\d+(?:[eE][-+]?\d+)?|[-+*/()]", text)
    if "".join(tokens) != re.sub(r"\s+", "", text):
        raise InputError(f"not a plain arithmetic expression: {text!r}")
    position = 0

    def take() -> str:
        nonlocal position
        position += 1
        return tokens[position - 1]

    def atom() -> float:
        token = take()
        if token == "(":
            value = expression()
            take()
            return value
        if token in "+-":
            return atom() if token == "+" else -atom()
        return float(token)

    def term() -> float:
        value = atom()
        while position < len(tokens) and tokens[position] in "*/":
            value = value * atom() if take() == "*" else value / atom()
        return value

    def expression() -> float:
        value = term()
        while position < len(tokens) and tokens[position] in "+-":
            value = value + term() if take() == "+" else value - term()
        return value

    return expression()


def inspect_rules(doc: Any) -> Dict[str, Any]:
    if not isinstance(doc, dict) or not isinstance(doc.get("groups"), list):
        raise InputError("not a Prometheus rule file: expected a top-level groups list")
    groups, records, alerts = [], [], []
    budget: Optional[float] = None
    period_days: Optional[float] = None
    optimized = False
    for group in doc["groups"]:
        groups.append(str(group.get("name")))
        for rule in group.get("rules") or []:
            labels = string_map(rule.get("labels"), "labels")
            expr = str(rule.get("expr", ""))
            if "record" in rule:
                records.append({"record": rule["record"], "window": labels.get(WINDOW_LABEL, ""), "slo": labels.get(ID_LABEL, "")})
                vector = re.fullmatch(r"\s*vector\((.*)\)\s*", expr)
                if rule["record"] == BUDGET_METRIC and vector:
                    budget = arithmetic(vector.group(1))
                if rule["record"] == PERIOD_DAYS_METRIC and vector:
                    period_days = arithmetic(vector.group(1))
                if str(rule["record"]).startswith(SLI_METRIC) and "sum_over_time" in expr:
                    optimized = True
            elif "alert" in rule:
                found = ALERT_CONDITION.findall(expr)
                conditions = []
                for index in range(0, len(found) - 1, 2):
                    (short, low), (long, high) = found[index], found[index + 1]
                    threshold = arithmetic(low.strip("()"))
                    conditions.append({"short": short, "long": long, "threshold": threshold, "burn_factor": threshold / budget if budget else None})
                alerts.append({"alert": rule["alert"], "severity": labels.get(SEVERITY_LABEL, ""), "for": str(rule.get("for", "0s")),
                               "conditions": conditions, "labels": labels, "annotations": string_map(rule.get("annotations"), "annotations")})
    return {"groups": groups, "records": records, "alerts": alerts, "budget": budget, "period_days": period_days, "optimized": optimized}


@dataclass
class Counter:
    times: List[float]
    values: List[float]
    resets: List[float] = field(default_factory=list)

    def __post_init__(self) -> None:
        total, self.resets = 0.0, [0.0]
        for previous, current in zip(self.values, self.values[1:]):
            if current < previous:
                total += previous
            self.resets.append(total)

    def value_at(self, t: float) -> float:
        index = bisect_right(self.times, t) - 1
        return self.values[index] if index >= 0 else 0.0


def extrapolated_rate(series: Counter, at: float, window: float) -> Optional[float]:
    start = at - window
    first = bisect_right(series.times, start)
    last = bisect_right(series.times, at) - 1
    if last - first < 1:
        return None
    first_t, last_t, first_v = series.times[first], series.times[last], series.values[first]
    delta = series.values[last] - first_v + series.resets[last] - series.resets[first]
    sampled = last_t - first_t
    average = sampled / (last - first)
    to_start, to_end = first_t - start, at - last_t
    if to_start >= average * 1.1:
        to_start = average / 2
    if delta > 0 and first_v >= 0:
        to_start = min(to_start, sampled * (first_v / delta))
    if to_end >= average * 1.1:
        to_end = average / 2
    factor = (sampled + to_start + to_end) / sampled
    factor /= window
    return delta * factor


def error_ratio(good: Counter, bad: Counter, at: float, window: float) -> Optional[float]:
    bad_rate, good_rate = extrapolated_rate(bad, at, window), extrapolated_rate(good, at, window)
    if bad_rate is None or good_rate is None:
        return None
    total = good_rate + bad_rate
    return bad_rate / total if total else math.nan


@dataclass
class Phase:
    duration: float
    error_ratio: Optional[float] = None
    burn_rate: Optional[float] = None
    events: Optional[float] = None

    def ratio(self, budget: float) -> float:
        return self.error_ratio if self.error_ratio is not None else (self.burn_rate or 0.0) * budget

    def label(self) -> str:
        what = f"{self.burn_rate:g}x" if self.burn_rate is not None else percent(self.error_ratio, 2)
        return f"{prom_duration(self.duration)} at {what}" + (f" ({self.events:g}/interval)" if self.events is not None else "")


@dataclass
class Scenario:
    name: str
    phases: List[Phase]
    events: float = 10000.0
    interval: float = 60.0
    good_series: str = 'http_requests_total{code="200"}'
    bad_series: str = 'http_requests_total{code="500"}'
    start: datetime = datetime(2026, 1, 1, tzinfo=timezone.utc)
    description: str = ""

    @property
    def duration(self) -> float:
        return sum(phase.duration for phase in self.phases)


def whole_number(value: Any, where: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0 or float(value) != int(value):
        raise InputError(f"{where} must be a whole number of events, got {value!r}")
    return float(value)


def load_scenarios(text: str) -> List[Scenario]:
    docs = load_documents(text)
    doc = docs[0] if docs else None
    if not isinstance(doc, dict) or not isinstance(doc.get("scenarios"), list):
        raise InputError("a scenario file needs a scenarios list")
    series = doc.get("series") or {}
    start = doc.get("start")
    try:
        begin = datetime.fromisoformat(str(start).replace("Z", "+00:00")) if start else Scenario.start
    except ValueError:
        raise InputError(f"start must be an ISO 8601 time such as 2026-10-01T00:00:00Z, got {start!r}") from None
    if begin.tzinfo is None:
        begin = begin.replace(tzinfo=timezone.utc)
    out = []
    for index, item in enumerate(doc["scenarios"]):
        phases = []
        for number, raw in enumerate(item.get("phases") or []):
            where = f"scenarios[{index}].phases[{number}]"
            if ("error_ratio" in raw) == ("burn_rate" in raw):
                raise InputError(f"{where}: give exactly one of error_ratio or burn_rate")
            phases.append(Phase(parse_duration(raw.get("duration", "")),
                                float(raw["error_ratio"]) if "error_ratio" in raw else None,
                                float(raw["burn_rate"]) if "burn_rate" in raw else None,
                                whole_number(raw["events"], f"{where}.events") if "events" in raw else None))
        if not phases:
            raise InputError(f"scenarios[{index}] has no phases")
        out.append(Scenario(str(item.get("name") or f"scenario-{index}"), phases, whole_number(doc.get("events_per_interval", 10000), "events_per_interval"),
                            parse_duration(doc.get("interval", "1m")), str(series.get("good") or Scenario.good_series), str(series.get("bad") or Scenario.bad_series),
                            begin, str(item.get("description") or "")))
    return out


def build_counters(scenario: Scenario, budget: float) -> Tuple[List[float], Counter, Counter]:
    times, good, bad = [0.0], [0.0], [0.0]
    carry = 0.0
    for phase in scenario.phases:
        events = phase.events if phase.events is not None else scenario.events
        ratio = phase.ratio(budget)
        if not 0 <= ratio <= 1:
            raise InputError(f"phase {phase.label()} has an error ratio of {ratio}, outside 0..1")
        for _ in range(int(round(phase.duration / scenario.interval))):
            carry += events * ratio
            failed = math.floor(carry + 1e-9)
            carry -= failed
            times.append(times[-1] + scenario.interval)
            bad.append(bad[-1] + failed)
            good.append(good[-1] + events - failed)
    return times, Counter(times, good), Counter(times, bad)


def calendar_bounds(moment: datetime, unit: str) -> Tuple[datetime, datetime]:
    day = moment.replace(hour=0, minute=0, second=0, microsecond=0)
    if unit == "week":
        start = day - timedelta(days=day.weekday())
        return start, start + timedelta(days=7)
    months = 3 if unit == "quarter" else 1
    start = day.replace(day=1, month=day.month - (day.month - 1) % months)
    after = start.month - 1 + months
    return start, start.replace(year=start.year + after // 12, month=after % 12 + 1)


@dataclass
class Evaluation:
    t: float
    ratios: Dict[float, Optional[float]]
    period_ratio: Optional[float]
    current_burn: Optional[float]
    remaining: Optional[float]
    firing: Dict[str, List[str]]
    margin: float
    bad: float
    total: float
    covered: float
    length: float
    budget: float

    @property
    def event_remaining(self) -> float:
        if not self.total or not self.covered:
            return 1.0
        return 1 - self.bad / (self.budget * self.total * self.length / self.covered)


@dataclass
class Simulation:
    slo: SLO
    scenario: Scenario
    rows: List[TableRow]
    good: Counter
    bad: Counter
    evaluations: List[Evaluation]
    transitions: List[Dict[str, Any]]


def condition_margin(value: Optional[float], threshold: float) -> float:
    if value is None or math.isnan(value):
        return math.inf
    return abs(value - threshold) / threshold


def simulate(slo: SLO, scenario: Scenario, windows: Sequence[AlertWindow] = GOOGLE_WINDOWS, optimize: bool = True) -> Simulation:
    rows = alert_table(slo, windows)
    times, good, bad = build_counters(scenario, slo.budget)
    short = rows[0].short
    recorded_times: List[float] = []
    recorded_sums: List[float] = [0.0]
    evaluations: List[Evaluation] = []
    transitions: List[Dict[str, Any]] = []
    previous: Dict[str, List[str]] = {PAGE: [], TICKET: []}
    for t in times:
        ratios = {window: error_ratio(good, bad, t, window) for window in sli_windows(rows)}
        if ratios[short] is not None:
            recorded_times.append(t)
            recorded_sums.append(recorded_sums[-1] + ratios[short])
        if optimize:
            first = bisect_right(recorded_times, t - slo.period)
            count = len(recorded_times) - first
            period_ratio = (recorded_sums[-1] - recorded_sums[first]) / count if count else None
        else:
            period_ratio = error_ratio(good, bad, t, slo.period)
        firing: Dict[str, List[str]] = {PAGE: [], TICKET: []}
        margin = math.inf
        for row in rows:
            low, high = ratios[row.short], ratios[row.long]
            margin = min(margin, condition_margin(low, row.threshold), condition_margin(high, row.threshold))
            if slo.alert(row.severity) and low is not None and high is not None and low > row.threshold and high > row.threshold:
                firing[row.severity].append(row.speed)
        if slo.calendar:
            begin, end = calendar_bounds(scenario.start + timedelta(seconds=t), slo.calendar)
            boundary = (begin - scenario.start).total_seconds()
            length = (end - begin).total_seconds()
            covered = t - max(boundary, 0.0)
        else:
            boundary, length, covered = t - slo.period, slo.period, min(t, slo.period)
        bad_events = bad.value_at(t) - bad.value_at(boundary)
        total_events = bad_events + good.value_at(t) - good.value_at(boundary)
        current = ratios[short] / slo.budget if ratios[short] is not None else None
        remaining = 1 - period_ratio / slo.budget if period_ratio is not None else None
        evaluation = Evaluation(t, ratios, period_ratio, current, remaining, firing, margin, bad_events, total_events, covered, length, slo.budget)
        evaluations.append(evaluation)
        for severity in (PAGE, TICKET):
            if bool(firing[severity]) != bool(previous[severity]):
                transitions.append({"t": t, "severity": severity, "state": "firing" if firing[severity] else "resolved", "rows": firing[severity] or previous[severity],
                                    "burn": {prom_duration(w): (r / slo.budget if r is not None else None) for w, r in ratios.items()}, "remaining": evaluation.event_remaining})
        previous = firing
    return Simulation(slo, scenario, rows, good, bad, evaluations, transitions)


@dataclass
class PolicyLevel:
    max_remaining: float
    action: str
    rule: str


DEFAULT_POLICY = [
    PolicyLevel(0.0, "freeze", "error budget exhausted: freeze feature releases; ship only reliability fixes and security patches until the budget is positive again"),
    PolicyLevel(0.25, "slow down", "under 25% of the budget left: no risky launches or experiments; every release goes out as a canary with a tested rollback"),
]
SHIP = "budget available: release at the normal pace and spend the budget on purpose (experiments, migrations, faster rollouts)"
INCIDENT = "a page is firing: stop releases and mitigate first; the postmortem decides how much of the budget the incident cost"
BLOCKING = ("freeze", "incident")


def load_policy(text: str) -> List[PolicyLevel]:
    docs = load_documents(text)
    levels = (docs[0] or {}).get("levels") if docs and isinstance(docs[0], dict) else None
    if not isinstance(levels, list) or not levels:
        raise InputError("a policy file needs a levels list of {max_remaining, action, rule}")
    try:
        return sorted((PolicyLevel(float(level["max_remaining"]), str(level["action"]), str(level.get("rule", ""))) for level in levels), key=lambda level: level.max_remaining)
    except (KeyError, TypeError, ValueError):
        raise InputError("every policy level needs a numeric max_remaining and an action") from None


def decide(remaining: Optional[float], page_firing: bool, policy: Sequence[PolicyLevel] = DEFAULT_POLICY) -> Tuple[str, str]:
    if page_firing:
        return "incident", INCIDENT
    for level in sorted(policy, key=lambda item: item.max_remaining):
        if remaining is not None and remaining <= level.max_remaining:
            return level.action, level.rule
    return "ship", SHIP


def budget_report(slo: SLO, total: float, bad: float, elapsed: Optional[float] = None, page_firing: bool = False, policy: Sequence[PolicyLevel] = DEFAULT_POLICY) -> Dict[str, Any]:
    if total <= 0 or bad < 0 or bad > total:
        raise InputError(f"need 0 <= bad <= total and total > 0, got bad={bad} total={total}")
    elapsed = slo.period if elapsed is None else elapsed
    if not 0 < elapsed <= slo.period:
        raise InputError(f"elapsed must be above 0 and at most the SLO period ({prom_duration(slo.period)})")
    projected = total * slo.period / elapsed
    allowed = slo.budget * projected
    consumed = bad / allowed
    burn = (bad / total) / slo.budget
    exhausted_in = (1 - consumed) * slo.period / burn if burn > 0 and consumed < 1 else (0.0 if consumed >= 1 else None)
    action, rule = decide(1 - consumed, page_firing, policy)
    return {
        "slo": slo.id, "objective_percent": slo.objective, "period": prom_duration(slo.period), "calendar": slo.calendar or None,
        "elapsed": prom_duration(elapsed), "events": total, "bad_events": bad, "error_budget_ratio": slo.budget,
        "allowed_bad_events_in_period": allowed, "bad_events_left": allowed - bad, "consumed": consumed, "remaining": 1 - consumed,
        "burn_rate": burn, "exhausted_in_seconds": exhausted_in, "within_period": exhausted_in is not None and exhausted_in <= slo.period - elapsed,
        "full_outage_allowed_seconds": slo.period * slo.budget, "action": action, "rule": rule,
    }


def expand_template(text: str, labels: Dict[str, str]) -> str:
    expanded = TEMPLATE_LABEL.sub(lambda m: labels.get(m.group(1), "<no value>"), text)
    leftover = TEMPLATE_ANY.search(expanded)
    if leftover:
        raise InputError(f"annotation {text!r} uses {leftover.group(0)}; the promtool test compares annotations exactly, so only $labels.NAME can be predicted")
    return expanded


def expected_alert(slo: SLO, severity: str) -> Dict[str, Any]:
    meta = slo.alert(severity)
    if meta is None:
        raise InputError(f"SLO {slo.id} has no {severity} alert")
    series = merge(slo.id_labels(), slo.labels)
    labels = merge(series, {SEVERITY_LABEL: severity}, {k: expand_template(v, series) for k, v in meta.labels.items()})
    annotations = {k: expand_template(v, series) for k, v in merge(default_annotations(severity), meta.annotations).items()}
    return {"exp_labels": dict(sorted(labels.items())), "exp_annotations": dict(sorted(annotations.items()))}


def number_text(value: float) -> str:
    return str(int(value)) if value == int(value) and abs(value) < 1e15 else repr(value)


def series_values(values: Sequence[float]) -> str:
    parts, i = [], 0
    while i < len(values):
        if i + 1 == len(values):
            parts.append(number_text(values[i]))
            break
        step, j = values[i + 1] - values[i], i + 1
        while j + 1 < len(values) and values[j + 1] - values[j] == step:
            j += 1
        parts.append(f"{number_text(values[i])}{'+' if step >= 0 else '-'}{number_text(abs(step))}x{j - i}")
        i = j + 1
    return " ".join(parts)


def value_check(metric: str, selector: Dict[str, str], value: float, t: float) -> Dict[str, Any]:
    tolerance = 1e-9 + 1e-6 * abs(value)
    offset = f"- {value!r}" if value >= 0 else f"+ {-value!r}"
    return {"expr": f"count(abs({metric}{label_selector(selector)} {offset}) <= {tolerance!r})", "eval_time": prom_duration(t), "exp_samples": [{"labels": "{}", "value": 1}]}


def checkpoints(sim: Simulation, every: float) -> Tuple[List[Evaluation], int]:
    wanted = {t for t in sim.good.times if t > 0 and round(t / every, 9) % 1 == 0}
    for change in sim.transitions:
        wanted |= {change["t"], change["t"] - sim.scenario.interval}
    chosen = [e for e in sim.evaluations if e.t in wanted and e.t > 0]
    clear = [e for e in chosen if e.margin >= AMBIGUOUS]
    return clear, len(chosen) - len(clear)


def promtool_test(slo: SLO, scenarios: Sequence[Scenario], rules_file: str, rules_doc: Any, every: float = 1800, values_every: float = 7200) -> Tuple[Dict[str, Any], List[str]]:
    summary = inspect_rules(rules_doc)
    intervals = {scenario.interval for scenario in scenarios}
    if len(intervals) != 1:
        raise InputError("every scenario in one promtool test file must use the same interval")
    names = sorted({meta.name for meta in (slo.page, slo.ticket) if meta})
    missing = [name for name in names if name not in {alert["alert"] for alert in summary["alerts"]}]
    if missing:
        raise InputError(f"{rules_file} has no alert named {', '.join(missing)}; generate the rules from the same spec")
    notes, tests = [], []
    for scenario in scenarios:
        sim = simulate(slo, scenario, optimize=summary["optimized"])
        clear, skipped = checkpoints(sim, every)
        if skipped:
            notes.append(f"{scenario.name}: skipped {skipped} checkpoint(s) where a window sits within {AMBIGUOUS:g} of its threshold")
        alert_tests = []
        for evaluation in clear:
            for name in names:
                expected = [expected_alert(slo, severity) for severity in (PAGE, TICKET) if evaluation.firing[severity] and slo.alert(severity).name == name]
                alert_tests.append({"eval_time": prom_duration(evaluation.t), "alertname": name, "exp_alerts": expected})
        value_tests = []
        ids = {ID_LABEL: slo.id}
        for evaluation in clear:
            if round(evaluation.t / values_every, 9) % 1:
                continue
            for window, value in sorted(evaluation.ratios.items()):
                if value is not None and not math.isnan(value) and window in (sim.rows[0].short, sim.rows[0].long, sim.rows[1].long, sim.rows[3].long):
                    value_tests.append(value_check(f"{SLI_METRIC}{prom_duration(window)}", merge(ids, {WINDOW_LABEL: prom_duration(window)}), value, evaluation.t))
            for metric, value in ((f"{SLI_METRIC}{prom_duration(slo.period)}", evaluation.period_ratio), (CURRENT_BURN_METRIC, evaluation.current_burn), (REMAINING_METRIC, evaluation.remaining)):
                if value is not None and not math.isnan(value):
                    value_tests.append(value_check(metric, ids, value, evaluation.t))
        tests.append({
            "name": scenario.name,
            "interval": prom_duration(scenario.interval),
            "input_series": [{"series": scenario.good_series, "values": series_values(sim.good.values)}, {"series": scenario.bad_series, "values": series_values(sim.bad.values)}],
            "alert_rule_test": alert_tests,
            "promql_expr_test": value_tests,
        })
    doc = {"rule_files": [rules_file], "evaluation_interval": prom_duration(intervals.pop()), "group_eval_order": summary["groups"], "tests": tests}
    return doc, notes


def burn_text(value: Optional[float]) -> str:
    if value is None or math.isnan(value):
        return "-"
    return f"{value:.1f}x" if value < 100 else f"{value:.0f}x"


def table_lines(slo: SLO, rows: Sequence[TableRow]) -> List[str]:
    kind = f"calendar {slo.calendar}, alerts sized for {prom_duration(slo.period)}" if slo.calendar else f"{prom_duration(slo.period)} rolling"
    lines = [f"SLO {slo.id}: {slo.objective:g}% over {kind}; error budget {percent(slo.budget, 3)} of events = {human_duration(slo.period * slo.budget)} of full outage",
             f"{'severity':<9}{'long':>5}{'short':>6}{'budget':>8}{'burn':>7}{'error ratio':>13}{'fires after (100% errors)':>27}{'budget spent when it fires':>28}"]
    for row in rows:
        lines.append(f"{row.severity:<9}{prom_duration(row.long):>5}{prom_duration(row.short):>6}{row.budget_percent:>7g}%{row.factor:>7.4g}{percent(row.threshold, 3):>13}"
                     f"{human_duration(row.detection(1.0) or 0):>27}{percent(row.long * row.factor / slo.period, 1):>28}")
    return lines


def simulation_summary(sim: Simulation, policy: Sequence[PolicyLevel] = DEFAULT_POLICY) -> Dict[str, Any]:
    last = sim.evaluations[-1]
    fired: Dict[str, Dict[str, Any]] = {}
    for severity in (PAGE, TICKET):
        starts = [c["t"] for c in sim.transitions if c["severity"] == severity and c["state"] == "firing"]
        ends = [c["t"] for c in sim.transitions if c["severity"] == severity and c["state"] == "resolved"]
        firing_time = sum((ends[i] if i < len(ends) else last.t) - start for i, start in enumerate(starts))
        fired[severity] = {"times": len(starts), "first": prom_duration(starts[0]) if starts else None, "firing_seconds": firing_time}
    action, rule = decide(last.event_remaining, bool(last.firing[PAGE]), policy)
    return {"scenario": sim.scenario.name, "slo": sim.slo.id, "duration": prom_duration(sim.scenario.duration), "alerts": fired,
            "remaining_by_events": last.event_remaining, "remaining_by_rules": last.remaining,
            "lowest_remaining_by_events": min((e.event_remaining for e in sim.evaluations if e.event_remaining is not None), default=None),
            "window_covered": prom_duration(last.covered), "action": action, "rule": rule,
            "action_by_rules": decide(last.remaining, bool(last.firing[PAGE]), policy)[0]}


def simulation_lines(sim: Simulation, summary: Dict[str, Any]) -> List[str]:
    windows = sli_windows(sim.rows)
    lines = [f"{sim.scenario.name}: {', '.join(p.label() for p in sim.scenario.phases)}; {sim.scenario.events:g} events per {prom_duration(sim.scenario.interval)}; SLO {sim.slo.id} {sim.slo.objective:g}% over {prom_duration(sim.slo.period)}",
             f"{'time':>9}  {'alert':<22}" + "".join(f"{prom_duration(w):>7}" for w in windows) + "   budget left"]
    for change in sim.transitions:
        what = f"{change['severity']} {'fires' if change['state'] == 'firing' else 'resolves'} ({'+'.join(change['rows'])})"
        lines.append(f"{prom_duration(change['t']):>9}  {what:<22}" + "".join(f"{burn_text(change['burn'][prom_duration(w)]):>7}" for w in windows) + f"   {percent(change['remaining'])}")
    if not sim.transitions:
        lines.append(f"{'':>9}  no alert fired")
    alerts = summary["alerts"]
    lines.append(f"after {summary['duration']}: page fired {alerts[PAGE]['times']}x ({human_duration(alerts[PAGE]['firing_seconds']) if alerts[PAGE]['times'] else 'never'}), "
                 f"ticket fired {alerts[TICKET]['times']}x ({human_duration(alerts[TICKET]['firing_seconds']) if alerts[TICKET]['times'] else 'never'}); "
                 f"budget left {percent(summary['remaining_by_events'])} of the period's budget by events, {percent(summary['remaining_by_rules'])} by {REMAINING_METRIC} over {summary['window_covered']} of data")
    lines.append(f"policy: {summary['action']}: {summary['rule']}")
    if summary["action_by_rules"] != summary["action"]:
        lines.append(f"        a policy reading {REMAINING_METRIC} would say {summary['action_by_rules']}: the recording is 1 - burn rate over the data it has, a projection until the window holds a full period")
    return lines


def budget_lines(report: Dict[str, Any]) -> List[str]:
    exhausted = report["exhausted_in_seconds"]
    if exhausted is None:
        at_this_rate = "never at this rate"
    elif exhausted == 0:
        at_this_rate = "already exhausted"
    else:
        at_this_rate = f"exhausted in {human_duration(exhausted)}" + ("" if report["within_period"] else " (after this period ends)")
    return [
        f"SLO {report['slo']}: {report['objective_percent']:g}% over {report['period']}{' (calendar ' + report['calendar'] + ')' if report['calendar'] else ''}, {report['elapsed']} elapsed",
        f"  error budget     {percent(report['error_budget_ratio'], 3)} of events: {report['allowed_bad_events_in_period']:.0f} bad events allowed this period",
        f"  spent            {report['bad_events']:.0f} bad events = {percent(report['consumed'])} of the budget",
        f"  left             {percent(report['remaining'])} ({report['bad_events_left']:.0f} bad events)",
        f"  burn rate        {report['burn_rate']:.2f}x: {at_this_rate}",
        f"  full outage      {human_duration(report['full_outage_allowed_seconds'])} per {report['period']} would spend all of it",
        f"  policy           {report['action']}: {report['rule']}",
    ]


def label_pairs(values: Sequence[str]) -> Dict[str, str]:
    out = {}
    for value in values or []:
        key, sep, item = value.partition("=")
        if not sep or not LABEL_NAME.match(key):
            raise InputError(f"--extra-label must be key=value, got {value!r}")
        out[key] = item
    return out


def emit_json(value: Any) -> None:
    print(json.dumps(value, indent=2, sort_keys=False, default=str))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="minislo", description="SLOs as error budgets: burn-rate tables, Prometheus rules, simulations, a budget policy and promtool tests")
    sub = parser.add_subparsers(dest="command", required=True)

    def spec_command(name: str, text: str) -> argparse.ArgumentParser:
        command = sub.add_parser(name, help=text)
        command.add_argument("spec", help="Sloth prometheus/v1 or OpenSLO v1 SLO, YAML or JSON ('-' for stdin)")
        command.add_argument("--period", default="30d", help="SLO period for Sloth specs (Sloth's --default-slo-period)")
        command.add_argument("-l", "--extra-label", action="append", default=[], help="key=value added to every SLO's labels (Sloth's --extra-labels)")
        command.add_argument("--json", action="store_true")
        return command

    spec_command("validate", "parse and check a spec, print each SLO's budget")
    table = spec_command("table", "the multiwindow multi-burn-rate alert table")
    table.add_argument("--slo")
    generate = spec_command("generate", "write Prometheus recording and alerting rules")
    generate.add_argument("-o", "--out", default="-")
    generate.add_argument("--no-optimize", action="store_true", help="compute the period SLI from the raw queries, like Sloth's disableOptimized")
    budget = spec_command("budget", "error budget arithmetic and the budget policy decision")
    budget.add_argument("--slo")
    budget.add_argument("--total", type=float, required=True, help="events in the window so far")
    budget.add_argument("--bad", type=float, required=True, help="bad events in the window so far")
    budget.add_argument("--elapsed", help="time covered by those events (default: the whole period)")
    budget.add_argument("--page-firing", action="store_true")
    budget.add_argument("--policy", help="policy file with levels of {max_remaining, action, rule}")
    simulate_command = spec_command("simulate", "replay error patterns and show when each alert fires")
    simulate_command.add_argument("--slo")
    simulate_command.add_argument("--scenarios", required=True)
    simulate_command.add_argument("--scenario", action="append", default=[], help="only these scenario names")
    simulate_command.add_argument("--no-optimize", action="store_true")
    simulate_command.add_argument("--policy")
    test_file = spec_command("test-file", "write a promtool rule test whose expectations come from the simulator")
    test_file.add_argument("--slo")
    test_file.add_argument("--scenarios", required=True)
    test_file.add_argument("--scenario", action="append", default=[])
    test_file.add_argument("--rules", required=True, help="the rule file under test (minislo's or Sloth's)")
    test_file.add_argument("-o", "--out", default="-")
    test_file.add_argument("--every", default="30m", help="alert checkpoint spacing; every alert transition is checked as well")
    test_file.add_argument("--values-every", default="2h", help="recording rule value checkpoint spacing")
    inspect = sub.add_parser("inspect", help="summarise a rule file: records, windows, alerts, thresholds and burn factors")
    inspect.add_argument("rules")
    inspect.add_argument("--json", action="store_true")
    return parser


def write_output(path: str, text: str) -> None:
    if path == "-":
        sys.stdout.write(text)
        return
    try:
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(text)
    except OSError as exc:
        raise InputError(f"cannot write {path}: {exc.strerror}") from None


def chosen_scenarios(args: argparse.Namespace) -> List[Scenario]:
    scenarios = load_scenarios(read_text(args.scenarios))
    if args.scenario:
        unknown = set(args.scenario) - {s.name for s in scenarios}
        if unknown:
            raise InputError(f"unknown scenario {', '.join(sorted(unknown))}; the file has {', '.join(s.name for s in scenarios)}")
        scenarios = [s for s in scenarios if s.name in args.scenario]
    return scenarios


def run(args: argparse.Namespace) -> int:
    if args.command == "inspect":
        summary = inspect_rules((load_documents(read_text(args.rules)) or [None])[0])
        if args.json:
            emit_json(summary)
            return 0
        print(f"groups: {', '.join(summary['groups'])}")
        print(f"records: {len(summary['records'])}; SLI windows: {', '.join(r['window'] for r in summary['records'] if r['window'])}; period SLI {'averages the short window' if summary['optimized'] else 'uses the raw queries'}")
        for alert in summary["alerts"]:
            rows = "; ".join(f"{c['long']}+{c['short']} > {c['threshold']:.6g}" + (f" ({c['burn_factor']:.4g}x)" if c["burn_factor"] else "") for c in alert["conditions"])
            print(f"alert {alert['alert']} [{alert['severity'] or '-'}] for {alert['for']}: {rows}")
        return 0
    slos = load_slos(read_text(args.spec), args.period, label_pairs(args.extra_label))
    if args.command == "validate":
        if args.json:
            emit_json([{"id": s.id, "objective_percent": s.objective, "error_budget_ratio": s.budget, "period": prom_duration(s.period), "calendar": s.calendar or None,
                        "full_outage_allowed_seconds": s.period * s.budget, "source": s.source, "alerts": {sev: (s.alert(sev).name if s.alert(sev) else None) for sev in (PAGE, TICKET)}} for s in slos])
            return 0
        for slo in slos:
            print(f"ok  {slo.id}: {slo.objective:g}% over {prom_duration(slo.period)}{' (calendar ' + slo.calendar + ')' if slo.calendar else ''}, budget {percent(slo.budget, 3)} = "
                  f"{human_duration(slo.period * slo.budget)} of full outage, from {slo.source}")
        return 0
    if args.command == "generate":
        for slo in slos:
            if slo.calendar:
                print(f"warning: {slo.id} uses a calendar {slo.calendar}; Prometheus ranges are rolling, so the period rules cover the last {prom_duration(slo.period)}, not the {slo.calendar} so far", file=sys.stderr)
        write_output(args.out, rules_text(slos, args.spec, optimize=not args.no_optimize))
        return 0
    if args.command == "table":
        selected = [pick(slos, args.slo)] if args.slo else slos
        if args.json:
            emit_json({s.id: [row.as_dict(s.budget, s.period) for row in alert_table(s)] for s in selected})
            return 0
        for slo in selected:
            print("\n".join(table_lines(slo, alert_table(slo))))
        return 0
    slo = pick(slos, args.slo)
    policy = load_policy(read_text(args.policy)) if getattr(args, "policy", None) else DEFAULT_POLICY
    if args.command == "budget":
        report = budget_report(slo, args.total, args.bad, parse_duration(args.elapsed) if args.elapsed else None, args.page_firing, policy)
        if args.json:
            emit_json(report)
        else:
            print("\n".join(budget_lines(report)))
        return 1 if report["action"] in BLOCKING else 0
    if args.command == "simulate":
        results = []
        for scenario in chosen_scenarios(args):
            sim = simulate(slo, scenario, optimize=not args.no_optimize)
            summary = simulation_summary(sim, policy)
            if args.json:
                results.append({"summary": summary, "transitions": [dict(c, time=prom_duration(c["t"])) for c in sim.transitions]})
            else:
                print("\n".join(simulation_lines(sim, summary)) + "\n")
        if args.json:
            emit_json(results)
        return 0
    rules_doc = (load_documents(read_text(args.rules)) or [None])[0]
    doc, notes = promtool_test(slo, chosen_scenarios(args), args.rules.rsplit("/", 1)[-1], rules_doc, parse_duration(args.every), parse_duration(args.values_every))
    for note in notes:
        print(f"note: {note}", file=sys.stderr)
    write_output(args.out, f"# promtool test rules for {args.rules}; expectations predicted by minislo simulate from {args.scenarios}\n" + dump_yaml(doc))
    return 0


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    try:
        args = parser.parse_args(list(sys.argv[1:] if argv is None else argv))
    except SystemExit as exc:
        return int(exc.code or 0)
    try:
        return run(args)
    except InputError as exc:
        print(f"minislo: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
