"""minifalco: a runtime detection rule engine. Reads process, file and network events (JSON fixtures or strace -f -tt -yy output), evaluates a Falco-style condition language over them, and reports matching rules with priorities, rendered outputs and rule tuning."""
from __future__ import annotations

import argparse
import fnmatch
import json
import operator
import os
import re
import sys
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple


class RuleError(Exception):
    pass


class InputError(Exception):
    pass


PRIORITIES = ["EMERGENCY", "ALERT", "CRITICAL", "ERROR", "WARNING", "NOTICE", "INFORMATIONAL", "DEBUG"]
PRIORITY_RANK = {name: index for index, name in enumerate(PRIORITIES)}
PRIORITY_ALIASES = {"INFO": "INFORMATIONAL", "WARN": "WARNING"}

DEFAULT_RULES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "rules", "minifalco_rules.json")

COMPARISONS = {"=", "==", "!=", "<", "<=", ">", ">="}
NUMERIC = {"<": operator.lt, "<=": operator.le, ">": operator.gt, ">=": operator.ge}
STRING_OPS = {"contains", "icontains", "startswith", "endswith", "glob", "bcontains"}
SET_OPS = {"in", "pmatch", "intersects"}
KEYWORDS = {"and", "or", "not"}
UNARY_FIELD_OPS = {"exists"}


def normalize_priority(name: str) -> str:
    upper = str(name).strip().upper()
    upper = PRIORITY_ALIASES.get(upper, upper)
    if upper not in PRIORITY_RANK:
        raise RuleError(f"unknown priority {name!r} (one of {', '.join(PRIORITIES)})")
    return upper


def tokenize(text: str) -> List[str]:
    tokens: List[str] = []
    i = 0
    length = len(text)
    while i < length:
        ch = text[i]
        if ch.isspace():
            i += 1
            continue
        if ch in "(),":
            tokens.append(ch)
            i += 1
            continue
        if ch in "\"'":
            quote = ch
            j = i + 1
            while j < length and text[j] != quote:
                j += 1
            if j >= length:
                raise RuleError(f"unterminated string in condition: {text[i:]!r}")
            tokens.append(quote + text[i + 1:j] + quote)
            i = j + 1
            continue
        if ch in "=!<>":
            j = i
            while j < length and text[j] in "=!<>":
                j += 1
            tokens.append(text[i:j])
            i = j
            continue
        j = i
        while j < length and not text[j].isspace() and text[j] not in "(),\"'" and text[j] not in "=!<>":
            j += 1
        tokens.append(text[i:j])
        i = j
    return tokens


def literal(token: str) -> str:
    if len(token) >= 2 and token[0] in "\"'" and token[-1] == token[0]:
        return token[1:-1]
    return token


class Node:
    def evaluate(self, event: "Event") -> bool:
        raise NotImplementedError


@dataclass
class And(Node):
    children: List[Node]

    def evaluate(self, event: "Event") -> bool:
        return all(child.evaluate(event) for child in self.children)


@dataclass
class Or(Node):
    children: List[Node]

    def evaluate(self, event: "Event") -> bool:
        return any(child.evaluate(event) for child in self.children)


@dataclass
class Not(Node):
    child: Node

    def evaluate(self, event: "Event") -> bool:
        return not self.child.evaluate(event)


@dataclass
class Exists(Node):
    field_name: str

    def evaluate(self, event: "Event") -> bool:
        value = event.get(self.field_name)
        return value is not None and value != ""


@dataclass
class Compare(Node):
    field_name: str
    op: str
    value: str

    def evaluate(self, event: "Event") -> bool:
        raw = event.get(self.field_name)
        return apply_comparison(raw, self.op, self.value)


@dataclass
class Membership(Node):
    field_name: str
    op: str
    values: Tuple[str, ...]

    def evaluate(self, event: "Event") -> bool:
        raw = event.get(self.field_name)
        if raw is None:
            return False
        text = stringify(raw)
        if self.op == "in":
            return text in self.values
        if self.op == "pmatch":
            return path_matches(text, self.values)
        if self.op == "intersects":
            if isinstance(raw, (list, tuple, set)):
                return any(stringify(item) in self.values for item in raw)
            return text in self.values
        return False


def stringify(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if value is None:
        return ""
    return str(value)


def as_number(value: Any) -> Optional[float]:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return None


def apply_comparison(raw: Any, op: str, expected: str) -> bool:
    if op in ("=", "=="):
        return stringify(raw).lower() == expected.lower() if isinstance(raw, bool) else stringify(raw) == expected
    if op == "!=":
        if raw is None:
            return True
        return stringify(raw) != expected
    if op in NUMERIC:
        left, right = as_number(raw), as_number(expected)
        return False if left is None or right is None else NUMERIC[op](left, right)
    if raw is None:
        return False
    text = stringify(raw)
    if op == "contains":
        return expected in text
    if op in ("icontains", "bcontains"):
        return expected.lower() in text.lower()
    if op == "startswith":
        return text.startswith(expected)
    if op == "endswith":
        return text.endswith(expected)
    if op == "glob":
        return fnmatch.fnmatchcase(text, expected)
    raise RuleError(f"unknown operator {op!r}")


def path_matches(value: str, prefixes: Iterable[str]) -> bool:
    for prefix in prefixes:
        clean = prefix.rstrip("/")
        if value == prefix or value == clean or value.startswith(clean + "/"):
            return True
    return False


class Parser:
    def __init__(self, tokens: List[str], macros: Dict[str, Node], lists: Dict[str, List[str]], stack: Tuple[str, ...] = ()):
        self.tokens = tokens
        self.pos = 0
        self.macros = macros
        self.lists = lists
        self.stack = stack

    def peek(self) -> Optional[str]:
        return self.tokens[self.pos] if self.pos < len(self.tokens) else None

    def next(self) -> str:
        token = self.tokens[self.pos]
        self.pos += 1
        return token

    def expect(self, token: str) -> None:
        got = self.peek()
        if got != token:
            raise RuleError(f"expected {token!r}, got {got!r}")
        self.pos += 1

    def parse(self) -> Node:
        node = self.parse_or()
        if self.pos != len(self.tokens):
            raise RuleError(f"unexpected token {self.peek()!r} in condition")
        return node

    def parse_or(self) -> Node:
        nodes = [self.parse_and()]
        while self.peek() == "or":
            self.next()
            nodes.append(self.parse_and())
        return nodes[0] if len(nodes) == 1 else Or(nodes)

    def parse_and(self) -> Node:
        nodes = [self.parse_unary()]
        while self.peek() == "and":
            self.next()
            nodes.append(self.parse_unary())
        return nodes[0] if len(nodes) == 1 else And(nodes)

    def parse_unary(self) -> Node:
        if self.peek() == "not":
            self.next()
            return Not(self.parse_unary())
        return self.parse_atom()

    def parse_atom(self) -> Node:
        token = self.peek()
        if token is None:
            raise RuleError("unexpected end of condition")
        if token == "(":
            self.next()
            node = self.parse_or()
            self.expect(")")
            return node
        name = self.next()
        following = self.peek()
        if following in COMPARISONS or following in STRING_OPS or following in SET_OPS or following in UNARY_FIELD_OPS:
            return self.parse_predicate(name)
        return self.expand_macro(name)

    def parse_predicate(self, field_name: str) -> Node:
        op = self.next()
        if op in UNARY_FIELD_OPS:
            return Exists(field_name)
        if op in SET_OPS:
            self.expect("(")
            values = self.parse_list()
            self.expect(")")
            return Membership(field_name, op, tuple(values))
        if op in COMPARISONS or op in STRING_OPS:
            value_token = self.next()
            if value_token in ("(", ")", ","):
                raise RuleError(f"expected a value after {op!r}, got {value_token!r}")
            return Compare(field_name, op, literal(value_token))
        raise RuleError(f"unknown operator {op!r}")

    def parse_list(self) -> List[str]:
        values: List[str] = []
        while self.peek() != ")":
            token = self.peek()
            if token is None:
                raise RuleError("unterminated list in condition")
            if token == ",":
                self.next()
                continue
            self.next()
            bare = literal(token)
            if token == bare and bare in self.lists:
                values.extend(self.lists[bare])
            else:
                values.append(bare)
        return values

    def expand_macro(self, name: str) -> Node:
        if name not in self.macros:
            raise RuleError(f"'{name}' is neither a field comparison nor a known macro")
        if name in self.stack:
            raise RuleError(f"macro cycle: {' -> '.join(self.stack + (name,))}")
        return self.macros[name]


def compile_condition(condition: str, macros: Dict[str, Node], lists: Dict[str, List[str]], stack: Tuple[str, ...] = ()) -> Node:
    tokens = tokenize(condition)
    if not tokens:
        raise RuleError("empty condition")
    return Parser(tokens, macros, lists, stack).parse()


@dataclass
class Exception_:
    field_name: str
    op: str
    values: Tuple[str, ...]

    def matches(self, event: "Event") -> bool:
        raw = event.get(self.field_name)
        if self.op in ("in", "pmatch", "intersects"):
            return Membership(self.field_name, self.op, self.values).evaluate(event)
        if self.op in UNARY_FIELD_OPS:
            return raw is not None and raw != ""
        expected = self.values[0] if self.values else ""
        return apply_comparison(raw, self.op, expected)


@dataclass
class Rule:
    name: str
    condition: str
    output: str
    priority: str
    tags: List[str]
    enabled: bool
    node: Node
    exceptions: List[Exception_]

    def matches(self, event: "Event") -> bool:
        if not self.enabled:
            return False
        if not self.node.evaluate(event):
            return False
        return not any(exception.matches(event) for exception in self.exceptions)


@dataclass
class Ruleset:
    rules: List[Rule]
    lists: Dict[str, List[str]]
    macro_sources: Dict[str, str]


def build_exceptions(raw: Any, rule_name: str, lists: Dict[str, List[str]]) -> List[Exception_]:
    out: List[Exception_] = []
    for entry in raw or []:
        field_name = entry.get("field")
        if not field_name:
            raise RuleError(f"rule {rule_name!r}: an exception needs a 'field'")
        op = str(entry.get("op") or "in")
        values: List[str] = []
        for item in entry.get("values") or ([entry["value"]] if "value" in entry else []):
            if isinstance(item, str) and item in lists:
                values.extend(lists[item])
            else:
                values.append(str(item))
        out.append(Exception_(str(field_name), op, tuple(values)))
    return out


def load_ruleset(sources: Sequence[str]) -> Ruleset:
    lists: Dict[str, List[str]] = {}
    macro_sources: Dict[str, str] = {}
    macros: Dict[str, Node] = {}
    rules: List[Rule] = []
    overrides: Dict[str, Dict[str, Any]] = {}
    documents: List[Dict[str, Any]] = []
    for source in sources:
        try:
            with open(source, encoding="utf-8") as handle:
                data = json.load(handle)
        except FileNotFoundError:
            raise InputError(f"rules file not found: {source}")
        except json.JSONDecodeError as exc:
            raise InputError(f"{source}: not valid JSON ({exc})")
        if not isinstance(data, dict):
            raise InputError(f"{source}: a rules file must be a JSON object with lists, macros and rules")
        documents.append(data)
    for data in documents:
        lists.update({name: [str(item) for item in items] for name, items in (data.get("lists") or {}).items()})
    for data in documents:
        for name, text in (data.get("macros") or {}).items():
            macro_sources[name] = str(text)
    for name in list(macro_sources):
        macros[name] = compile_condition(macro_sources[name], macros, lists, (name,))
    for data in documents:
        for name, patch in (data.get("overrides") or {}).items():
            overrides.setdefault(name, {}).update(patch)
    for data in documents:
        for raw in data.get("rules") or []:
            name = str(raw.get("rule") or raw.get("name") or "")
            if not name:
                raise RuleError("a rule needs a 'rule' name")
            merged = dict(raw)
            merged.update(overrides.get(name, {}))
            condition = str(merged.get("condition") or "")
            if not condition:
                raise RuleError(f"rule {name!r}: missing condition")
            node = compile_condition(condition, macros, lists)
            rules.append(Rule(
                name=name,
                condition=condition,
                output=str(merged.get("output") or name),
                priority=normalize_priority(merged.get("priority") or "NOTICE"),
                tags=[str(tag) for tag in (merged.get("tags") or [])],
                enabled=bool(merged.get("enabled", True)),
                node=node,
                exceptions=build_exceptions(merged.get("exceptions"), name, lists),
            ))
    if not rules:
        raise RuleError("no rules loaded")
    return Ruleset(rules, lists, macro_sources)


FIELD_DEFAULTS = {"container.id": "host"}
INDEX_RE = re.compile(r"^(?P<base>[A-Za-z0-9_.]+)\[(?P<index>\d+)\]$")


class Event:
    def __init__(self, data: Dict[str, Any], index: int = 0):
        self.data = data
        self.index = index

    def get(self, name: str) -> Any:
        match = INDEX_RE.match(name)
        if match:
            base = self._lookup(match.group("base"))
            if isinstance(base, (list, tuple)):
                position = int(match.group("index"))
                if 0 <= position < len(base):
                    return base[position]
            return None
        value = self._lookup(name)
        if value is None and name in FIELD_DEFAULTS:
            return FIELD_DEFAULTS[name]
        return value

    def _lookup(self, name: str) -> Any:
        if name in self.data:
            return self.data[name]
        parts = name.split(".")
        current: Any = self.data
        for part in parts:
            if isinstance(current, dict) and part in current:
                current = current[part]
            else:
                return None
        return current


def load_json_events(text: str, source: str, defaults: Dict[str, Any]) -> List[Event]:
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise InputError(f"{source}: not valid JSON ({exc})")
    if isinstance(data, dict) and "events" in data:
        data = data["events"]
    if not isinstance(data, list):
        raise InputError(f"{source}: expected a JSON array of events (or an object with an 'events' array)")
    events: List[Event] = []
    for position, item in enumerate(data):
        if not isinstance(item, dict):
            raise InputError(f"{source}: event {position} is not an object")
        merged = dict(item)
        for key, value in defaults.items():
            merged.setdefault(key, value)
        events.append(Event(merged, position))
    return events


STRACE_LINE = re.compile(
    r"^(?:\[pid\s+(?P<bracket_pid>\d+)\]\s*|(?P<bare_pid>\d+)\s+)?"
    r"(?:\d{2}:\d{2}:\d{2}(?:\.\d+)?\s+|\d+\.\d+\s+)?"
    r"(?P<body>.*)$"
)
STRACE_CALL = re.compile(r"^(?P<name>[a-z_][a-z0-9_]*)\((?P<rest>.*)$")
RESULT_RE = re.compile(r"^(?P<value>0x[0-9a-fA-F]+|-?\d+|\?)")
FD_DECORATION = re.compile(r'^(?P<fd>\d+)(?:<(?P<target>.*)>)?$')
FAMILY_RE = re.compile(r"sa_family=(AF_[A-Z0-9]+)")
PORT_RE = re.compile(r"sin6?_port=htons\((\d+)\)")
INET_RE = re.compile(r'inet_addr\("([^"]+)"\)')
INET6_RE = re.compile(r'inet_pton\(AF_INET6,\s*"([^"]+)"')
UNIX_RE = re.compile(r'sun_path=(@?"[^"]*")')
OPEN_CALLS = {"open", "openat", "openat2", "creat"}
EXEC_CALLS = {"execve", "execveat"}
FORK_CALLS = {"clone", "clone3", "fork", "vfork"}
WRITE_FLAGS = ("O_WRONLY", "O_RDWR", "O_CREAT", "O_APPEND", "O_TRUNC")
READ_FLAGS = ("O_RDONLY", "O_RDWR")


def find_close(text: str) -> int:
    depth = 0
    quoted = False
    i = 0
    while i < len(text):
        ch = text[i]
        if quoted:
            if ch == "\\":
                i += 2
                continue
            if ch == '"':
                quoted = False
        elif ch == '"':
            quoted = True
        elif ch in "([{":
            depth += 1
        elif ch in ")]}":
            if depth == 0:
                return i
            depth -= 1
        i += 1
    return -1


def split_args(text: str) -> List[str]:
    parts: List[str] = []
    depth = 0
    quoted = False
    start = 0
    i = 0
    while i < len(text):
        ch = text[i]
        if quoted:
            if ch == "\\":
                i += 2
                continue
            if ch == '"':
                quoted = False
        elif ch == '"':
            quoted = True
        elif ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth -= 1
        elif ch == "," and depth == 0:
            parts.append(text[start:i].strip())
            start = i + 1
        i += 1
    tail = text[start:].strip()
    if tail:
        parts.append(tail)
    return parts


def unquote(text: str) -> Optional[str]:
    text = text.strip()
    if text.startswith('@"'):
        text = text[1:]
    if not text.startswith('"'):
        return None
    out: List[str] = []
    i = 1
    while i < len(text):
        ch = text[i]
        if ch == '"':
            break
        if ch == "\\" and i + 1 < len(text):
            nxt = text[i + 1]
            mapping = {"n": "\n", "t": "\t", "r": "\r", "\\": "\\", '"': '"'}
            if nxt in "01234567":
                j = i + 1
                while j < len(text) and j < i + 4 and text[j] in "01234567":
                    j += 1
                out.append(chr(int(text[i + 1:j], 8)))
                i = j
                continue
            out.append(mapping.get(nxt, nxt))
            i += 2
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def parse_argv(text: str) -> List[str]:
    text = text.strip()
    if not text.startswith("["):
        return []
    inner = text[1:text.rfind("]")] if "]" in text else text[1:]
    return [item for item in (unquote(part) for part in split_args(inner)) if item is not None]


def open_flags(args: List[str], name: str) -> str:
    index = 2 if name in ("openat", "openat2") else 1
    if index < len(args):
        return args[index]
    return ""


def open_path(args: List[str], name: str) -> Optional[str]:
    index = 1 if name in ("openat", "openat2", "newfstatat") else 0
    if index < len(args):
        return unquote(args[index])
    return None


def parse_sockaddr(args: List[str]) -> Dict[str, Any]:
    joined = ",".join(args)
    family = FAMILY_RE.search(joined)
    result: Dict[str, Any] = {}
    if not family:
        return result
    name = family.group(1)
    port = PORT_RE.search(joined)
    if name == "AF_INET":
        host = INET_RE.search(joined)
        if host:
            result["fd.sip"] = host.group(1)
        if port:
            result["fd.sport"] = int(port.group(1))
    elif name == "AF_INET6":
        host = INET6_RE.search(joined)
        if host:
            result["fd.sip"] = host.group(1)
        if port:
            result["fd.sport"] = int(port.group(1))
    elif name in ("AF_UNIX", "AF_LOCAL"):
        path = UNIX_RE.search(joined)
        if path:
            unix_path = unquote(path.group(1))
            if unix_path:
                result["fd.name"] = unix_path
    return result


def parse_strace_events(text: str, defaults: Dict[str, Any]) -> List[Event]:
    programs: Dict[Optional[int], str] = {}
    names: Dict[Optional[int], str] = {}
    parents: Dict[Optional[int], Optional[int]] = {}
    events: List[Event] = []
    position = 0
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("+++") or line.startswith("---") or line.startswith("strace:"):
            continue
        header = STRACE_LINE.match(line)
        pid_text = header.group("bracket_pid") or header.group("bare_pid")
        pid = int(pid_text) if pid_text else None
        body = header.group("body").strip()
        if body.startswith("<..."):
            continue
        call = STRACE_CALL.match(body)
        if not call:
            continue
        name, rest = call.group("name"), call.group("rest")
        close = find_close(rest)
        if close < 0:
            continue
        after = rest[close + 1:].strip()
        if not after.startswith("="):
            continue
        result = RESULT_RE.match(after[1:].strip())
        if not result:
            continue
        value = result.group("value")
        code = None if value == "?" else int(value, 0)
        args = split_args(rest[:close])
        base = dict(defaults)
        if name in FORK_CALLS and code is not None and code > 0:
            parents[code] = pid
            if pid in programs:
                programs[code] = programs[pid]
                names[code] = names.get(pid, "")
            continue
        base["proc.pid"] = pid
        parent_pid = parents.get(pid)
        if parent_pid in names:
            base["proc.pname"] = names[parent_pid]
        if name in EXEC_CALLS and code == 0:
            path = open_path(args, name)
            argv = parse_argv(args[1]) if len(args) > 1 else []
            if path is None and argv:
                path = argv[0]
            if path is None:
                continue
            proc_name = os.path.basename(argv[0]) if argv else os.path.basename(path)
            programs[pid] = path
            names[pid] = proc_name
            base["evt.type"] = "execve"
            base["proc.exe"] = path
            base["proc.name"] = proc_name
            base["proc.args"] = " ".join(argv[1:])
            base["proc.cmdline"] = " ".join(argv) if argv else proc_name
            events.append(Event(base, position))
            position += 1
            continue
        if pid in programs:
            base["proc.name"] = names.get(pid, os.path.basename(programs[pid]))
            base["proc.exe"] = programs[pid]
        if name in OPEN_CALLS and code is not None and code >= 0:
            path = open_path(args, name)
            if path is None:
                continue
            flags = open_flags(args, name)
            base["evt.type"] = "openat" if name in ("openat", "openat2") else name
            base["fd.name"] = path
            base["fd.directory"] = os.path.dirname(path)
            base["evt.is_open_write"] = any(token in flags for token in WRITE_FLAGS)
            base["evt.is_open_read"] = (not base["evt.is_open_write"]) or ("O_RDWR" in flags)
            events.append(Event(base, position))
            position += 1
            continue
        if name == "connect":
            base["evt.type"] = "connect"
            base.update(parse_sockaddr(args))
            if "fd.name" in base or "fd.sip" in base:
                events.append(Event(base, position))
                position += 1
            continue
    return events


@dataclass
class Alert:
    rule: str
    priority: str
    output: str
    tags: List[str]
    event_index: int
    fields: Dict[str, Any] = field(default_factory=dict)


OUTPUT_FIELD = re.compile(r"%([A-Za-z0-9_.]+(?:\[\d+\])?)")


def render_output(template: str, event: Event) -> Tuple[str, Dict[str, Any]]:
    used: Dict[str, Any] = {}

    def replace(match: "re.Match[str]") -> str:
        name = match.group(1)
        value = event.get(name)
        used[name] = value
        return stringify(value) if value is not None and value != "" else "<NA>"

    return OUTPUT_FIELD.sub(replace, template), used


def evaluate(ruleset: Ruleset, events: Sequence[Event], min_priority: Optional[str] = None) -> List[Alert]:
    ceiling = PRIORITY_RANK[normalize_priority(min_priority)] if min_priority else len(PRIORITIES)
    alerts: List[Alert] = []
    for event in events:
        for rule in ruleset.rules:
            if PRIORITY_RANK[rule.priority] > ceiling:
                continue
            if rule.matches(event):
                rendered, used = render_output(rule.output, event)
                alerts.append(Alert(rule.name, rule.priority, rendered, rule.tags, event.index, used))
    return alerts


def tune(ruleset: Ruleset, events: Sequence[Event]) -> List[Dict[str, Any]]:
    counts: Dict[str, int] = {rule.name: 0 for rule in ruleset.rules}
    by_proc: Dict[str, Dict[str, int]] = {rule.name: {} for rule in ruleset.rules}
    for event in events:
        for rule in ruleset.rules:
            if rule.matches(event):
                counts[rule.name] += 1
                proc = stringify(event.get("proc.name")) or "<unknown>"
                by_proc[rule.name][proc] = by_proc[rule.name].get(proc, 0) + 1
    report = []
    for rule in ruleset.rules:
        top = sorted(by_proc[rule.name].items(), key=lambda item: (-item[1], item[0]))
        report.append({
            "rule": rule.name,
            "priority": rule.priority,
            "matches": counts[rule.name],
            "top_processes": [{"proc.name": proc, "count": n} for proc, n in top[:5]],
        })
    report.sort(key=lambda row: (-row["matches"], row["rule"]))
    return report


def read_source(path: str) -> Tuple[str, str]:
    if path == "-":
        return sys.stdin.read(), "stdin"
    try:
        with open(path, encoding="utf-8", errors="replace") as handle:
            return handle.read(), path
    except FileNotFoundError:
        raise InputError(f"events file not found: {path}")


def load_events(path: str, source_kind: str, defaults: Dict[str, Any]) -> List[Event]:
    text, name = read_source(path)
    if source_kind == "auto":
        if name.endswith(".json"):
            source_kind = "json"
        elif name.endswith((".strace", ".txt", ".log", ".out", ".trace")):
            source_kind = "strace"
        else:
            try:
                json.loads(text)
                source_kind = "json"
            except json.JSONDecodeError:
                source_kind = "strace"
    if source_kind == "json":
        return load_json_events(text, name, defaults)
    return parse_strace_events(text, defaults)


def collect_defaults(args: argparse.Namespace) -> Dict[str, Any]:
    defaults: Dict[str, Any] = {}
    if getattr(args, "container_id", None):
        defaults["container.id"] = args.container_id
    if getattr(args, "proc_uid", None) is not None:
        defaults["user.uid"] = args.proc_uid
    if getattr(args, "user_name", None):
        defaults["user.name"] = args.user_name
    return defaults


def cmd_run(args: argparse.Namespace) -> int:
    ruleset = load_ruleset(args.rules)
    events = load_events(args.events, args.source, collect_defaults(args))
    alerts = evaluate(ruleset, events, args.min_priority)
    if args.json:
        print(json.dumps({
            "events": len(events),
            "alerts": [{"rule": a.rule, "priority": a.priority, "output": a.output, "tags": a.tags, "event": a.event_index, "fields": a.fields} for a in alerts],
        }, indent=2))
    else:
        print(f"minifalco: {len(events)} event(s), {len(alerts)} alert(s)")
        for alert in alerts:
            print(f"  {alert.priority:<9} {alert.rule}: {alert.output}")
    return 1 if alerts else 0


def cmd_tune(args: argparse.Namespace) -> int:
    ruleset = load_ruleset(args.rules)
    events = load_events(args.events, args.source, collect_defaults(args))
    report = tune(ruleset, events)
    if args.json:
        print(json.dumps({"events": len(events), "rules": report}, indent=2))
    else:
        print(f"minifalco tune: {len(events)} event(s)")
        print(f"  {'matches':>7}  {'priority':<9}  rule")
        for row in report:
            print(f"  {row['matches']:>7}  {row['priority']:<9}  {row['rule']}")
            for proc in row["top_processes"]:
                print(f"            {proc['count']:>5}x  {proc['proc.name']}")
    return 0


def cmd_list(args: argparse.Namespace) -> int:
    ruleset = load_ruleset(args.rules)
    if args.json:
        print(json.dumps({"rules": [{"rule": r.name, "priority": r.priority, "tags": r.tags, "condition": r.condition, "enabled": r.enabled} for r in ruleset.rules]}, indent=2))
    else:
        for rule in ruleset.rules:
            state = "" if rule.enabled else " [disabled]"
            print(f"{rule.priority:<9} {rule.name}{state}")
            print(f"          condition: {rule.condition}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="minifalco", description=__doc__)
    sub = parser.add_subparsers(dest="command")

    def add_common(sp: argparse.ArgumentParser, with_events: bool = True) -> None:
        if with_events:
            sp.add_argument("events", help="events file (.json or strace output), or - for stdin")
            sp.add_argument("--source", choices=["auto", "json", "strace"], default="auto")
            sp.add_argument("--container-id", help="container id applied to every event (strace has no container context)")
            sp.add_argument("--proc-uid", type=int, help="user.uid applied to every event")
            sp.add_argument("--user-name", help="user.name applied to every event")
        sp.add_argument("-r", "--rules", action="append", default=None, help="rules file (repeatable); defaults to the built-in ruleset")
        sp.add_argument("--json", action="store_true", help="machine-readable output")

    run = sub.add_parser("run", help="evaluate rules over events and print alerts")
    add_common(run)
    run.add_argument("--min-priority", help="only report alerts at or above this priority")
    run.set_defaults(func=cmd_run)

    tune_parser = sub.add_parser("tune", help="count matches per rule to find noisy rules")
    add_common(tune_parser)
    tune_parser.set_defaults(func=cmd_tune)

    list_parser = sub.add_parser("list-rules", help="print the loaded rules (like falco -L)")
    add_common(list_parser, with_events=False)
    list_parser.set_defaults(func=cmd_list)
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(list(sys.argv[1:] if argv is None else argv))
    if not getattr(args, "command", None):
        parser.print_help(sys.stderr)
        return 2
    if args.rules is None:
        args.rules = [DEFAULT_RULES]
    try:
        return args.func(args)
    except (RuleError, InputError) as exc:
        print(f"minifalco: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
