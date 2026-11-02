"""minibench: run CIS Kubernetes Benchmark checks, written as data, against a node (static pod manifests, file modes and owners, the kubelet's effective configuration) and report PASS, FAIL or WARN with evidence as text, JSON or JUnit, plus a diff between two runs or between a run and kube-bench --json."""
from __future__ import annotations

import argparse
import fnmatch
import grp
import json
import os
import pwd
import re
import stat
import subprocess
import sys
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

SNAPSHOT_VERSION = 1
REPORT_VERSION = 1
STATES = ("PASS", "WARN", "FAIL")
RANK = {state: index for index, state in enumerate(STATES)}
TARGETS = ("master", "controlplane", "etcd", "node")
CONTROL_PLANE = ("kube-apiserver", "kube-controller-manager", "kube-scheduler", "etcd")
COMPONENTS = CONTROL_PLANE + ("kubelet", "kube-proxy")
MANIFESTS = {component: f"/etc/kubernetes/manifests/{component}.yaml" for component in CONTROL_PLANE}
ADMIN_KUBECONFIGS = ("/etc/kubernetes/admin.conf", "/etc/kubernetes/super-admin.conf")
KUBECONFIGS = {"scheduler-kubeconfig": "/etc/kubernetes/scheduler.conf", "controller-manager-kubeconfig": "/etc/kubernetes/controller-manager.conf"}
KUBELET_SERVICE_FILES = ("/etc/systemd/system/kubelet.service.d/10-kubeadm.conf", "/etc/systemd/system/kubelet.service", "/lib/systemd/system/kubelet.service")
KUBELET_CONFIG_DEFAULT = "/var/lib/kubelet/config.yaml"
KUBELET_KUBECONFIG_DEFAULT = "/etc/kubernetes/kubelet.conf"
KUBELET_CA_DEFAULT = "/etc/kubernetes/pki/ca.crt"
ETCD_DATA_DIR_DEFAULT = "/var/lib/etcd/default.etcd"
PKI_DIR = "/etc/kubernetes/pki"
STAT_FORMAT = "%a|%U|%G|%F|%n"
PROC_SCRIPT = "for f in /proc/[0-9]*/cmdline; do tr '\\000' '\\037' < \"$f\" 2>/dev/null; echo; done"
ARG_SEPARATOR = "\x1f"
BOOLEAN_FLAGS = frozenset({
    "--anonymous-auth", "--profiling", "--service-account-lookup", "--service-account-extend-token-expiration",
    "--use-service-account-credentials", "--client-cert-auth", "--peer-client-cert-auth", "--auto-tls", "--peer-auto-tls",
    "--make-iptables-util-chains", "--rotate-certificates", "--protect-kernel-defaults", "--seccomp-default",
    "--allow-privileged", "--enable-bootstrap-token-auth", "--leader-elect", "--fail-swap-on",
})
AUDIT_KINDS = ("flag", "flags", "kubelet", "file", "files", "tree", "manual")
OPS = ("set", "unset", "eq", "noteq", "has", "nothave", "gte", "lte", "subset", "max-mode", "owner")
FILE_REFS = ("kubelet-config", "kubelet-kubeconfig", "kubelet-service", "kubelet-ca", "etcd-data") + tuple(KUBECONFIGS)
DURATION_UNITS = {"ns": 1e-9, "us": 1e-6, "µs": 1e-6, "ms": 1e-3, "s": 1.0, "m": 60.0, "h": 3600.0}
DURATION_PART = re.compile(r"(\d+(?:\.\d+)?)(ns|us|µs|ms|s|m|h)")
SECTION_RE = re.compile(r"^\d+(\.\d+)*$")
INT_RE = re.compile(r"^-?(0|[1-9][0-9]*)$")
FLOAT_RE = re.compile(r"^-?[0-9]+\.[0-9]+$")
ESCAPES = {"n": "\n", "t": "\t", "r": "\r", '"': '"', "\\": "\\", "/": "/", "0": "\0"}
CHECKS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "checks.json")


class BenchError(Exception):
    pass


class YamlError(ValueError):
    def __init__(self, line: int, message: str) -> None:
        super().__init__(f"line {line}: {message}")
        self.line = line


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
        if not text.strip() or text.strip() == "---":
            continue
        indent = len(text) - len(text.lstrip(" "))
        lines.append(Line(number, indent, text.strip()))
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
    if text in ("", "~", "null", "Null", "NULL"):
        return None
    if text in ("true", "True", "TRUE"):
        return True
    if text in ("false", "False", "FALSE"):
        return False
    if INT_RE.match(text):
        return int(text)
    if FLOAT_RE.match(text):
        return float(text)
    return text


def parse_flow(text: str, number: int) -> Tuple[Any, str]:
    text = text.lstrip()
    if text.startswith("["):
        items: List[Any] = []
        rest = text[1:].lstrip()
        while True:
            if rest.startswith("]"):
                return items, rest[1:].lstrip()
            if not rest:
                raise YamlError(number, "unterminated flow sequence; missing ]")
            value, rest = parse_flow_value(rest, number, ",]")
            items.append(value)
            rest = rest.lstrip()
            if rest.startswith(","):
                rest = rest[1:].lstrip()
            elif not rest.startswith("]"):
                raise YamlError(number, "expected , or ] in flow sequence")
    if text.startswith("{"):
        mapping: Dict[str, Any] = {}
        rest = text[1:].lstrip()
        while True:
            if rest.startswith("}"):
                return mapping, rest[1:].lstrip()
            if not rest:
                raise YamlError(number, "unterminated flow mapping; missing }")
            key, rest = parse_flow_value(rest, number, ":,}")
            rest = rest.lstrip()
            value = None
            if rest.startswith(":"):
                value, rest = parse_flow_value(rest[1:].lstrip(), number, ",}")
            mapping[str(key)] = value
            rest = rest.lstrip()
            if rest.startswith(","):
                rest = rest[1:].lstrip()
            elif not rest.startswith("}"):
                raise YamlError(number, "expected , or } in flow mapping")
    raise YamlError(number, "not a flow collection")


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
        raise YamlError(number, "anchors and aliases are not supported")
    if text in ("|", ">") or text.startswith(("|-", ">-", "|+", ">+")):
        raise YamlError(number, "block scalars are not supported")
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


class Parser:
    def __init__(self, lines: List[Line]) -> None:
        self.lines = lines
        self.position = 0

    def peek(self) -> Optional[Line]:
        return self.lines[self.position] if self.position < len(self.lines) else None

    def parse_document(self) -> Any:
        if not self.lines:
            return {}
        value = self.parse_block(self.lines[0].indent)
        extra = self.peek()
        if extra is not None:
            raise YamlError(extra.number, f"unexpected indentation ({extra.indent} spaces)")
        return value

    def parse_block(self, indent: int) -> Any:
        line = self.peek()
        if line is None:
            return None
        if line.text == "-" or line.text.startswith("- "):
            return self.parse_sequence(indent)
        if split_key(line.text, line.number) is None:
            self.position += 1
            return parse_scalar(line.text, line.number)
        return self.parse_mapping(indent)

    def parse_sequence(self, indent: int, compact: bool = False) -> List[Any]:
        items: List[Any] = []
        while True:
            line = self.peek()
            if line is None or line.indent < indent:
                return items
            if line.indent > indent:
                raise YamlError(line.number, f"bad indentation in sequence (expected {indent} spaces, got {line.indent})")
            if not (line.text == "-" or line.text.startswith("- ")):
                if compact:
                    return items
                raise YamlError(line.number, "expected a sequence item starting with '- '")
            rest = line.text[1:].strip()
            if not rest:
                self.position += 1
                nested = self.peek()
                items.append(self.parse_block(nested.indent) if nested is not None and nested.indent > indent else None)
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
                mapping[key] = parse_scalar(rest, line.number)
                continue
            nested = self.peek()
            if nested is None or nested.indent < indent:
                mapping[key] = None
            elif nested.indent == indent:
                mapping[key] = self.parse_sequence(indent, compact=True) if nested.text == "-" or nested.text.startswith("- ") else None
            else:
                mapping[key] = self.parse_block(nested.indent)


def parse_yaml(source: str) -> Any:
    return Parser(split_lines(source)).parse_document()


def parse_document(text: str) -> Any:
    stripped = text.lstrip()
    if stripped.startswith("{"):
        try:
            return json.loads(text)
        except ValueError:
            pass
    return parse_yaml(text)


def parse_duration(text: Any) -> Optional[float]:
    raw = str(text).strip()
    if raw in ("0", "+0", "-0"):
        return 0.0
    sign = -1.0 if raw.startswith("-") else 1.0
    body = raw.lstrip("+-")
    total = 0.0
    position = 0
    for match in DURATION_PART.finditer(body):
        if match.start() != position:
            return None
        total += float(match.group(1)) * DURATION_UNITS[match.group(2)]
        position = match.end()
    if position == 0 or position != len(body):
        return None
    return sign * total


def scalar_text(value: Any) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, list):
        return ",".join(scalar_text(item) or "" for item in value)
    if isinstance(value, dict):
        return ",".join(f"{key}={scalar_text(item)}" for key, item in value.items())
    return str(value)


def dig(data: Any, dotted: str) -> Any:
    for key in dotted.split("."):
        if not isinstance(data, dict) or key not in data:
            return None
        data = data[key]
    return data


def items(text: str) -> List[str]:
    return [part.strip() for part in str(text).split(",") if part.strip()]


def parse_flags(argv: Sequence[str]) -> Dict[str, str]:
    flags: Dict[str, str] = {}
    index = 1
    while index < len(argv):
        arg = argv[index]
        if arg.startswith("--") and len(arg) > 2:
            name, sep, value = arg.partition("=")
            if not sep:
                following = argv[index + 1] if index + 1 < len(argv) else ""
                if name not in BOOLEAN_FLAGS and following and not following.startswith("-"):
                    value = following
                    index += 1
                else:
                    value = "true"
            flags[name] = value
        index += 1
    return flags


def container_argv(pod: Any) -> Optional[List[str]]:
    containers = ((pod or {}).get("spec") or {}).get("containers") if isinstance(pod, dict) else None
    if not containers or not isinstance(containers[0], dict):
        return None
    first = containers[0]
    argv = [str(arg) for arg in first.get("command") or []] + [str(arg) for arg in first.get("args") or []]
    return argv or None


def section_key(check_id: str) -> Tuple[int, Tuple[int, ...], str]:
    if SECTION_RE.match(check_id):
        return 0, tuple(int(part) for part in check_id.split(".")), ""
    return 1, (), check_id


def iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass
class Check:
    id: str
    title: str
    group: str
    target: str
    scored: bool
    audit: Dict[str, Any]
    expect: Dict[str, Any]
    remediation: str
    note: str = ""

    def to_json(self) -> Dict[str, Any]:
        out = {"id": self.id, "title": self.title, "group": self.group, "target": self.target, "scored": self.scored,
               "audit": self.audit, "expect": self.expect, "remediation": self.remediation}
        if self.note:
            out["note"] = self.note
        return out


@dataclass
class Catalogue:
    benchmark: str
    kube_bench: str
    groups: Dict[str, Dict[str, str]]
    checks: List[Check]

    def group_title(self, group: str) -> str:
        return self.groups.get(group, {}).get("title", "")


def group_of(check_id: str, groups: Dict[str, Any]) -> str:
    parts = check_id.split(".")
    for size in range(len(parts) - 1, 0, -1):
        prefix = ".".join(parts[:size])
        if prefix in groups:
            return prefix
    return ""


def load_catalogue(path: str = CHECKS_FILE) -> Catalogue:
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
    except OSError as exc:
        raise BenchError(f"cannot read {path}: {exc.strerror}") from exc
    except ValueError as exc:
        raise BenchError(f"{path} is not JSON: {exc}") from exc
    groups = data.get("groups") or {}
    checks: List[Check] = []
    seen = set()
    for raw in data.get("checks") or []:
        check_id = str(raw.get("id", ""))
        where = f"{path}: check {check_id or '?'}"
        if not check_id or check_id in seen:
            raise BenchError(f"{where}: missing or duplicate id")
        seen.add(check_id)
        group = raw.get("group") or group_of(check_id, groups)
        if group not in groups:
            raise BenchError(f"{where}: no group in 'groups' covers this id")
        audit = raw.get("audit") or {}
        kinds = [kind for kind in AUDIT_KINDS if kind in audit and not (kind == "flag" and "kubelet" in audit)]
        if len(kinds) != 1:
            raise BenchError(f"{where}: audit needs exactly one of {', '.join(AUDIT_KINDS)}, got {sorted(audit)}")
        expect = raw.get("expect") or {}
        if kinds[0] != "manual" and expect.get("op") not in OPS:
            raise BenchError(f"{where}: expect.op must be one of {', '.join(OPS)}")
        if kinds[0] in ("flag", "flags") and audit.get("component") not in COMPONENTS:
            raise BenchError(f"{where}: audit.component must be one of {', '.join(COMPONENTS)}")
        if kinds[0] in ("file", "files", "tree") and audit.get("property") not in ("mode", "owner"):
            raise BenchError(f"{where}: audit.property must be mode or owner")
        checks.append(Check(check_id, str(raw.get("title", "")), group, groups[group].get("target", ""), bool(raw.get("scored", True)),
                            audit, expect, str(raw.get("remediation", "")), str(raw.get("note", ""))))
    checks.sort(key=lambda c: section_key(c.id))
    return Catalogue(str(data.get("benchmark", "")), str(data.get("kube_bench", "")), groups, checks)


class Snapshot:
    def __init__(self, data: Dict[str, Any]) -> None:
        if not isinstance(data, dict) or data.get("minibench_snapshot") != SNAPSHOT_VERSION:
            raise BenchError("not a minibench snapshot (make one with: minibench collect --node ...)")
        self.data = data
        self.processes: Dict[str, List[str]] = data.get("processes") or {}
        self.files: Dict[str, Dict[str, Any]] = data.get("files") or {}
        self.trees: Dict[str, List[Dict[str, Any]]] = data.get("trees") or {}
        self.problems: List[str] = []

    def file(self, path: str) -> Optional[Dict[str, Any]]:
        entry = self.files.get(path)
        return entry if entry and entry.get("exists") else None

    def document(self, path: str) -> Optional[Any]:
        entry = self.file(path)
        if entry is None or "content" not in entry:
            return None
        try:
            return parse_document(entry["content"])
        except YamlError as exc:
            problem = f"{path}: cannot parse: {exc}"
            if problem not in self.problems:
                self.problems.append(problem)
            return None

    def args(self, component: str) -> Tuple[Optional[List[str]], str]:
        if component in MANIFESTS:
            argv = container_argv(self.document(MANIFESTS[component]))
            if argv:
                return argv, f"manifest {MANIFESTS[component]}"
        argv = self.processes.get(component)
        if argv:
            return list(argv), "running process"
        return None, ""

    def flags(self, component: str) -> Tuple[Optional[Dict[str, str]], str]:
        argv, where = self.args(component)
        return (parse_flags(argv), where) if argv is not None else (None, where)

    def kubelet_flags(self) -> Dict[str, str]:
        return self.flags("kubelet")[0] or {}

    def kubelet_config(self) -> Tuple[Optional[Dict[str, Any]], str]:
        path = self.resolve("kubelet-config")
        if self.processes.get("kubelet") and "--config" not in self.kubelet_flags():
            return None, f"{path} (the kubelet runs without --config, so it loads no file)"
        document = self.document(path)
        return (document, path) if isinstance(document, dict) else (None, path)

    def resolve(self, ref: str) -> str:
        if ref.startswith("manifest:"):
            component = ref.split(":", 1)[1]
            if component not in MANIFESTS:
                raise BenchError(f"unknown manifest {component!r}")
            return MANIFESTS[component]
        if ref.startswith("/"):
            return ref
        if ref in KUBECONFIGS:
            return KUBECONFIGS[ref]
        kubelet = self.kubelet_flags()
        if ref == "kubelet-config":
            return kubelet.get("--config") or KUBELET_CONFIG_DEFAULT
        if ref == "kubelet-kubeconfig":
            return kubelet.get("--kubeconfig") or KUBELET_KUBECONFIG_DEFAULT
        if ref == "kubelet-service":
            return next((path for path in KUBELET_SERVICE_FILES if self.file(path)), KUBELET_SERVICE_FILES[0])
        if ref == "kubelet-ca":
            config, _ = self.kubelet_config()
            return kubelet.get("--client-ca-file") or scalar_text(dig(config, "authentication.x509.clientCAFile")) or KUBELET_CA_DEFAULT
        if ref == "etcd-data":
            return (self.flags("etcd")[0] or {}).get("--data-dir") or ETCD_DATA_DIR_DEFAULT
        raise BenchError(f"unknown file reference {ref!r} (use an absolute path, manifest:COMPONENT or one of {', '.join(FILE_REFS)})")

    def targets(self) -> Dict[str, bool]:
        apiserver = self.args("kube-apiserver")[0] is not None
        return {"master": apiserver, "controlplane": apiserver, "etcd": self.args("etcd")[0] is not None,
                "node": self.args("kubelet")[0] is not None or self.file(KUBELET_CONFIG_DEFAULT) is not None}


def same(actual: str, wanted: str) -> bool:
    if actual.lower() in ("true", "false") or wanted.lower() in ("true", "false"):
        return actual.lower() == wanted.lower()
    return actual == wanted


def mode_within(mode: str, allowed: str) -> bool:
    try:
        return int(mode, 8) & ~int(allowed, 8) == 0
    except ValueError:
        return False


def judge(expect: Dict[str, Any], actual: Optional[str]) -> bool:
    op = expect["op"]
    wanted = scalar_text(expect.get("value")) or ""
    if op == "set":
        return actual is not None
    if op == "unset":
        return actual is None
    if actual is None:
        return expect.get("unset") == "pass"
    if expect.get("type") == "duration" and op in ("eq", "noteq"):
        left, right = parse_duration(actual), parse_duration(wanted)
        equal = left is not None and left == right
        return equal if op == "eq" else not equal
    if op == "eq":
        return same(actual, wanted)
    if op == "noteq":
        return not same(actual, wanted)
    if op == "has":
        return wanted in items(actual)
    if op == "nothave":
        return wanted not in items(actual)
    if op in ("gte", "lte"):
        try:
            number, limit = int(actual), int(wanted)
        except ValueError:
            return False
        return number >= limit if op == "gte" else number <= limit
    if op == "subset":
        chosen = items(actual)
        return bool(chosen) and all(item in items(wanted) for item in chosen)
    if op == "max-mode":
        return mode_within(actual, wanted)
    if op == "owner":
        return actual == wanted
    raise BenchError(f"unknown op {op!r}")


def describe(expect: Dict[str, Any], subject: str) -> str:
    wanted = scalar_text(expect.get("value")) or ""
    verb = "are" if " and " in subject else "is"
    text = {
        "set": f"{subject} {verb} set", "unset": f"{subject} {verb} not set", "eq": f"{subject} is {wanted}",
        "noteq": f"{subject} is not {wanted}", "has": f"{subject} includes {wanted}", "nothave": f"{subject} does not include {wanted}",
        "gte": f"{subject} >= {wanted}", "lte": f"{subject} <= {wanted}", "subset": f"{subject} lists only allowed values",
        "max-mode": f"{subject} has mode {wanted} or stricter", "owner": f"{subject} is owned by {wanted}",
    }.get(expect.get("op", ""), subject)
    if expect.get("unset") == "pass" and expect.get("op") not in ("set", "unset"):
        text += ", or is not set"
    return text


@dataclass
class Result:
    check: Check
    state: str
    evidence: List[str]
    expected: str
    value: Optional[str] = None

    def to_json(self) -> Dict[str, Any]:
        return {"id": self.check.id, "title": self.check.title, "group": self.check.group, "target": self.check.target,
                "scored": self.check.scored, "state": self.state, "value": self.value, "expected": self.expected,
                "evidence": list(self.evidence), "remediation": self.check.remediation}


def verdict(check: Check, ok: bool) -> str:
    if ok:
        return "PASS"
    return "FAIL" if check.scored else "WARN"


def flag_audit(check: Check, snapshot: Snapshot) -> Result:
    audit = check.audit
    component = audit["component"]
    flags, where = snapshot.flags(component)
    names = audit.get("flags") or [audit["flag"]]
    subject = " and ".join(names)
    if flags is None:
        return Result(check, verdict(check, False), [f"{component}: no static pod manifest and no running process found"], describe(check.expect, subject))
    evidence: List[str] = []
    ok = True
    values: List[Optional[str]] = []
    for name in names:
        if name in flags:
            actual: Optional[str] = flags[name]
            evidence.append(f"{component} ({where}): {name}={actual}")
        elif "default" in audit:
            actual = scalar_text(audit["default"])
            evidence.append(f"{component} ({where}): {name} not set; the default is {actual}")
        else:
            actual = None
            evidence.append(f"{component} ({where}): {name} not set")
        values.append(actual)
        ok = judge(check.expect, actual) and ok
    other = check.expect.get("unlike")
    if ok and other:
        other_flags = snapshot.flags(other["component"])[0] or {}
        theirs = other_flags.get(other["flag"])
        if theirs is not None and theirs in values:
            ok = False
            evidence.append(f"{other['component']} {other['flag']}={theirs} is the same file, so the CA is not unique")
    return Result(check, verdict(check, ok), evidence, describe(check.expect, subject), values[0] if len(values) == 1 else None)


def kubelet_value(setting: Dict[str, Any], snapshot: Snapshot) -> Tuple[Optional[str], str]:
    key = setting["kubelet"]
    flag = setting.get("flag", "")
    flags = snapshot.kubelet_flags()
    if flag and flag in flags:
        return flags[flag], f"kubelet flag {flag}={flags[flag]} (a flag overrides the config file)"
    config, path = snapshot.kubelet_config()
    if config is not None:
        text = scalar_text(dig(config, key))
        if text:
            zero = parse_duration(text) == 0 if setting.get("zero_means_default") else False
            if zero and "default" in setting:
                return scalar_text(setting["default"]), f"{path}: {key}: {text}, which the kubelet replaces with its default {scalar_text(setting['default'])}"
            return text, f"{path}: {key}: {text}"
        if "default" in setting:
            return scalar_text(setting["default"]), f"{path}: {key} not set; the config file default is {scalar_text(setting['default'])}"
        return None, f"{path}: {key} not set" + (f" and no {flag} flag" if flag else "")
    if "flag_default" in setting:
        return scalar_text(setting["flag_default"]), f"no kubelet config file: {path}; {flag} defaults to {scalar_text(setting['flag_default'])}"
    return None, f"no kubelet config file: {path}" + (f" and no {flag} flag" if flag else "")


def kubelet_audit(check: Check, snapshot: Snapshot) -> Result:
    settings = [check.audit] + list(check.audit.get("and") or [])
    evidence: List[str] = []
    values: List[Optional[str]] = []
    ok = True
    for setting in settings:
        actual, line = kubelet_value(setting, snapshot)
        values.append(actual)
        evidence.append(line)
        ok = judge(check.expect, actual) and ok
    subject = " and ".join(setting["kubelet"] for setting in settings)
    return Result(check, verdict(check, ok), evidence, describe(check.expect, subject), values[0] if len(values) == 1 else None)


def file_entries(check: Check, snapshot: Snapshot) -> Tuple[List[Tuple[str, Dict[str, Any]]], List[str]]:
    audit = check.audit
    if "file" in audit:
        path = snapshot.resolve(audit["file"])
        entry = snapshot.file(path)
        return ([(path, entry)] if entry else []), [path]
    if "files" in audit:
        paths = [snapshot.resolve(ref) for ref in audit["files"]]
        return [(path, snapshot.file(path)) for path in paths if snapshot.file(path)], paths
    root = audit["tree"]
    entries = snapshot.trees.get(root) or []
    pattern = audit.get("match")
    chosen = [entry for entry in entries if not pattern or fnmatch.fnmatch(os.path.basename(entry.get("path", "")), pattern)]
    return [(entry["path"], entry) for entry in chosen], [root + (f"/{pattern}" if pattern else "")]


def file_audit(check: Check, snapshot: Snapshot) -> Result:
    prop = check.audit["property"]
    entries, looked_at = file_entries(check, snapshot)
    subject = ", ".join(looked_at)
    expected = describe(check.expect, subject)
    if not entries:
        rule = check.audit.get("missing", "fail")
        return Result(check, verdict(check, rule == "pass"), [f"not found: {subject}" + (" (nothing to check)" if rule == "pass" else "")], expected)
    failing: List[str] = []
    passing: List[str] = []
    for path, entry in entries:
        actual = entry.get("mode", "") if prop == "mode" else f"{entry.get('owner', '')}:{entry.get('group', '')}"
        line = f"{path}: {'mode' if prop == 'mode' else 'owner'} {actual}"
        (passing if judge(check.expect, actual) else failing).append(line)
    evidence = failing + passing if len(entries) <= 3 else failing + [f"{len(passing)} of {len(entries)} entries pass"]
    value = None
    if len(entries) == 1:
        entry = entries[0][1]
        value = entry.get("mode") if prop == "mode" else f"{entry.get('owner')}:{entry.get('group')}"
    return Result(check, verdict(check, not failing), evidence, expected, value)


def evaluate(check: Check, snapshot: Snapshot) -> Result:
    audit = check.audit
    if "manual" in audit:
        return Result(check, "WARN", [f"manual: {audit['manual']}"], "a person reviews this")
    if "kubelet" in audit:
        return kubelet_audit(check, snapshot)
    if "flag" in audit or "flags" in audit:
        return flag_audit(check, snapshot)
    return file_audit(check, snapshot)


def selected(check_id: str, selection: Sequence[str]) -> bool:
    return not selection or any(check_id == wanted or check_id.startswith(wanted + ".") for wanted in selection)


def drift(snapshot: Snapshot) -> List[Dict[str, Any]]:
    found: List[Dict[str, Any]] = []
    for component in CONTROL_PLANE:
        written = container_argv(snapshot.document(MANIFESTS[component]))
        running = snapshot.processes.get(component)
        if not written or not running:
            continue
        manifest, process = parse_flags(written), parse_flags(running)
        changed = sorted(name for name in set(manifest) | set(process) if manifest.get(name) != process.get(name))
        if changed:
            found.append({"component": component, "flags": changed, "manifest": {n: manifest.get(n) for n in changed}, "process": {n: process.get(n) for n in changed}})
    return found


@dataclass
class Report:
    catalogue: Catalogue
    snapshot: Snapshot
    results: List[Result]
    skipped: List[Dict[str, str]] = field(default_factory=list)
    drift: List[Dict[str, Any]] = field(default_factory=list)

    def totals(self) -> Dict[str, int]:
        return {state: sum(1 for r in self.results if r.state == state) for state in STATES}

    def to_json(self) -> Dict[str, Any]:
        return {"minibench_report": REPORT_VERSION, "benchmark": self.catalogue.benchmark, "kube_bench": self.catalogue.kube_bench,
                "node": self.snapshot.data.get("node", ""), "collected_at": self.snapshot.data.get("collected_at", ""),
                "totals": self.totals(), "results": [r.to_json() for r in self.results], "skipped": self.skipped,
                "drift": self.drift, "problems": self.snapshot.problems}


def run_checks(catalogue: Catalogue, snapshot: Snapshot, targets: Sequence[str] = (), selection: Sequence[str] = ()) -> Report:
    present = snapshot.targets()
    for target in targets:
        if target not in TARGETS:
            raise BenchError(f"unknown target {target!r} (choose from {', '.join(TARGETS)})")
        if not present[target]:
            needs = {"master": "kube-apiserver", "controlplane": "kube-apiserver", "etcd": "etcd", "node": "kubelet"}[target]
            raise BenchError(f"target {target} was requested, but the snapshot has no {needs} manifest or process")
    wanted = list(targets) or [target for target in TARGETS if present[target]]
    results: List[Result] = []
    skipped: List[Dict[str, str]] = []
    for check in catalogue.checks:
        if not selected(check.id, selection):
            continue
        if check.target not in wanted:
            skipped.append({"id": check.id, "reason": f"target {check.target} is not on this node"})
            continue
        results.append(evaluate(check, snapshot))
    return Report(catalogue, snapshot, results, skipped, drift(snapshot))


def render_text(report: Report, verbose: bool = False) -> str:
    cat = report.catalogue
    lines = [f"minibench: {cat.benchmark} (kube-bench {cat.kube_bench}) on {report.snapshot.data.get('node', '?')}, collected {report.snapshot.data.get('collected_at', '?')}"]
    group = None
    for result in report.results:
        if result.check.group != group:
            group = result.check.group
            lines.append(f"== {group} {cat.group_title(group)}")
        lines.append(f"[{result.state}] {result.check.id} {result.check.title}")
        if verbose or result.state != "PASS":
            lines.extend(f"       evidence: {line}" for line in result.evidence)
            lines.append(f"       expected: {result.expected}")
        if result.state != "PASS":
            lines.append(f"       fix: {result.check.remediation}")
    for item in report.drift:
        lines.append(f"drift: {item['component']} runs with different flags than its manifest: {', '.join(item['flags'])}")
    for problem in report.snapshot.problems:
        lines.append(f"problem: {problem}")
    totals = report.totals()
    lines.append(f"== Summary: {totals['PASS']} PASS, {totals['FAIL']} FAIL, {totals['WARN']} WARN ({len(report.results)} checks, {len(report.skipped)} skipped)")
    return "\n".join(lines)


def render_junit(report: Report) -> str:
    totals = report.totals()
    root = ET.Element("testsuites", name=f"minibench {report.catalogue.benchmark}", tests=str(len(report.results)),
                      failures=str(totals["FAIL"]), skipped=str(totals["WARN"]))
    by_group: Dict[str, List[Result]] = {}
    for result in report.results:
        by_group.setdefault(result.check.group, []).append(result)
    for group, results in by_group.items():
        name = f"{group} {report.catalogue.group_title(group)}".strip()
        suite = ET.SubElement(root, "testsuite", name=name, tests=str(len(results)),
                              failures=str(sum(1 for r in results if r.state == "FAIL")), skipped=str(sum(1 for r in results if r.state == "WARN")))
        for result in results:
            case = ET.SubElement(suite, "testcase", classname=name, name=f"{result.check.id} {result.check.title}")
            if result.state == "FAIL":
                failure = ET.SubElement(case, "failure", message=result.check.remediation)
                failure.text = "\n".join(result.evidence)
            elif result.state == "WARN":
                ET.SubElement(case, "skipped", message="; ".join(result.evidence))
            out = ET.SubElement(case, "system-out")
            out.text = "\n".join(result.evidence + [f"expected: {result.expected}"])
    ET.indent(root)
    return ET.tostring(root, encoding="unicode")


@dataclass
class CommandResult:
    returncode: int
    stdout: str = ""
    stderr: str = ""


Runner = Callable[[Sequence[str]], CommandResult]


def subprocess_runner(argv: Sequence[str]) -> CommandResult:
    try:
        done = subprocess.run(list(argv), capture_output=True, text=True, errors="replace", timeout=60)
    except FileNotFoundError as exc:
        raise BenchError(f"{argv[0]} not found; install it or choose another --node") from exc
    except subprocess.TimeoutExpired as exc:
        raise BenchError(f"timed out after 60s: {' '.join(argv)}") from exc
    return CommandResult(done.returncode, done.stdout, done.stderr)


def file_type(text: str) -> str:
    if text == "directory":
        return "dir"
    if text == "symbolic link":
        return "link"
    return "file" if "regular" in text else text


def parse_stat_line(line: str) -> Optional[Dict[str, Any]]:
    parts = line.rstrip("\n").split("|", 4)
    if len(parts) != 5:
        return None
    mode, owner, group, kind, path = parts
    return {"path": path, "exists": True, "mode": mode, "owner": owner, "group": group, "type": file_type(kind)}


def parse_proc_output(text: str) -> List[List[str]]:
    argvs: List[List[str]] = []
    for line in text.splitlines():
        argv = [part for part in line.rstrip(ARG_SEPARATOR).split(ARG_SEPARATOR)]
        if argv and argv[0]:
            argvs.append(argv)
    return argvs


class ExecSource:
    def __init__(self, runner: Runner, prefix: Sequence[str], name: str) -> None:
        self.runner = runner
        self.prefix = list(prefix)
        self.name = name

    def run(self, argv: Sequence[str]) -> CommandResult:
        return self.runner(self.prefix + list(argv))

    def check(self) -> None:
        result = self.run(["true"])
        if result.returncode != 0:
            raise BenchError(f"cannot run commands on {self.name}: {(result.stderr or result.stdout).strip()[-300:] or f'exit {result.returncode}'}")

    def read(self, path: str) -> Optional[str]:
        result = self.run(["cat", "--", path])
        return result.stdout if result.returncode == 0 else None

    def stat(self, path: str) -> Optional[Dict[str, Any]]:
        result = self.run(["stat", "-c", STAT_FORMAT, "--", path])
        lines = result.stdout.splitlines()
        return parse_stat_line(lines[0]) if result.returncode == 0 and lines else None

    def walk(self, path: str) -> Optional[List[Dict[str, Any]]]:
        result = self.run(["find", path, "-exec", "stat", "-c", STAT_FORMAT, "{}", "+"])
        entries = [entry for entry in (parse_stat_line(line) for line in result.stdout.splitlines() if line.strip()) if entry]
        return entries if entries or result.returncode == 0 else None

    def processes(self) -> List[List[str]]:
        return parse_proc_output(self.run(["sh", "-c", PROC_SCRIPT]).stdout)


def stat_entry(path: str, info: os.stat_result) -> Dict[str, Any]:
    try:
        owner = pwd.getpwuid(info.st_uid).pw_name
    except KeyError:
        owner = "UNKNOWN"
    try:
        group = grp.getgrgid(info.st_gid).gr_name
    except KeyError:
        group = "UNKNOWN"
    kind = "dir" if stat.S_ISDIR(info.st_mode) else "link" if stat.S_ISLNK(info.st_mode) else "file"
    return {"path": path, "exists": True, "mode": format(stat.S_IMODE(info.st_mode), "o"), "owner": owner, "group": group, "type": kind}


class TreeSource:
    def __init__(self, root: str = "/", name: str = "") -> None:
        self.root = root
        self.name = name or ("local" if root == "/" else f"root:{root}")

    def host(self, path: str) -> str:
        return os.path.join(self.root, path.lstrip("/"))

    def check(self) -> None:
        if not os.path.isdir(self.root):
            raise BenchError(f"{self.root} is not a directory")

    def read(self, path: str) -> Optional[str]:
        try:
            with open(self.host(path), encoding="utf-8", errors="replace") as handle:
                return handle.read()
        except OSError:
            return None

    def stat(self, path: str) -> Optional[Dict[str, Any]]:
        try:
            return stat_entry(path, os.lstat(self.host(path)))
        except OSError:
            return None

    def walk(self, path: str) -> Optional[List[Dict[str, Any]]]:
        top = self.host(path)
        if not os.path.isdir(top):
            return None
        entries = [self.stat(path)]
        for directory, dirnames, filenames in os.walk(top):
            dirnames.sort()
            relative = os.path.relpath(directory, top)
            base = path.rstrip("/") if relative == "." else f"{path.rstrip('/')}/{relative}"
            for name in sorted(dirnames + filenames):
                entries.append(self.stat(f"{base}/{name}"))
        return [entry for entry in entries if entry]

    def processes(self) -> List[List[str]]:
        proc = self.host("/proc")
        if not os.path.isdir(proc):
            return []
        argvs: List[List[str]] = []
        for pid in sorted((entry for entry in os.listdir(proc) if entry.isdigit()), key=int):
            try:
                with open(os.path.join(proc, pid, "cmdline"), "rb") as handle:
                    raw = handle.read()
            except OSError:
                continue
            argv = [part.decode("utf-8", "replace") for part in raw.rstrip(b"\0").split(b"\0")]
            if argv and argv[0]:
                argvs.append(argv)
        return argvs


def make_source(node: str, runner: Runner = subprocess_runner) -> Any:
    if node.startswith("docker:") and len(node) > len("docker:"):
        return ExecSource(runner, ["docker", "exec", node.split(":", 1)[1]], node)
    if node == "local":
        return TreeSource("/")
    if node.startswith("root:") and len(node) > len("root:"):
        return TreeSource(node.split(":", 1)[1])
    raise BenchError(f"--node must be docker:CONTAINER, local or root:/PATH, got {node!r}")


def find_components(argvs: Sequence[Sequence[str]]) -> Dict[str, List[str]]:
    found: Dict[str, List[str]] = {}
    for argv in argvs:
        name = os.path.basename(argv[0]) if argv else ""
        if name in COMPONENTS and name not in found:
            found[name] = list(argv)
    return found


def collect(source: Any, now: Optional[datetime] = None) -> Dict[str, Any]:
    source.check()
    data: Dict[str, Any] = {"minibench_snapshot": SNAPSHOT_VERSION, "node": source.name, "collected_at": iso(now or datetime.now(timezone.utc)),
                            "processes": find_components(source.processes()), "files": {}, "trees": {}}

    def add(path: str, content: bool = False) -> None:
        if path in data["files"]:
            return
        info = source.stat(path)
        entry: Dict[str, Any] = {"exists": False}
        if info is not None:
            entry = {key: info[key] for key in ("exists", "mode", "owner", "group", "type")}
            if content and info["type"] == "file":
                text = source.read(path)
                if text is not None:
                    entry["content"] = text
        data["files"][path] = entry

    for path in MANIFESTS.values():
        add(path, content=True)
    for path in KUBELET_SERVICE_FILES:
        add(path)
    view = Snapshot(data)
    add(view.resolve("kubelet-config"), content=True)
    for ref in ("kubelet-kubeconfig", "kubelet-ca", "etcd-data") + tuple(KUBECONFIGS):
        add(view.resolve(ref))
    for path in ADMIN_KUBECONFIGS:
        add(path)
    tree = source.walk(PKI_DIR)
    if tree is not None:
        data["trees"][PKI_DIR] = tree
    return data


def read_json(path: str) -> Any:
    try:
        with (sys.stdin if path == "-" else open(path, encoding="utf-8")) as handle:
            text = handle.read()
    except OSError as exc:
        raise BenchError(f"cannot read {path}: {exc.strerror}") from exc
    try:
        return json.loads(text)
    except ValueError:
        pass
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith(("{", "[")):
            try:
                return json.loads(stripped)
            except ValueError:
                continue
    raise BenchError(f"{path} is not JSON")


def kube_bench_controls(data: Any) -> Optional[List[Dict[str, Any]]]:
    if isinstance(data, dict) and isinstance(data.get("Controls"), list):
        return data["Controls"]
    if isinstance(data, dict) and isinstance(data.get("tests"), list):
        return [data]
    if isinstance(data, list) and data and all(isinstance(item, dict) and "tests" in item for item in data):
        return data
    return None


def load_results(path: str) -> Tuple[str, Dict[str, Dict[str, Any]]]:
    data = read_json(path)
    if isinstance(data, dict) and "minibench_report" in data:
        return "minibench", {r["id"]: {"state": r["state"], "title": r.get("title", ""), "evidence": list(r.get("evidence") or [])} for r in data.get("results") or []}
    controls = kube_bench_controls(data)
    if controls is None:
        raise BenchError(f"{path}: neither a minibench report (run --json) nor kube-bench --json output")
    results: Dict[str, Dict[str, Any]] = {}
    for control in controls:
        for group in control.get("tests") or []:
            for item in group.get("results") or []:
                evidence = [f"{label}: {item[key]}" for key, label in (("actual_value", "actual"), ("expected_result", "expected"), ("reason", "reason")) if item.get(key)]
                results[str(item.get("test_number"))] = {"state": str(item.get("status", "")), "title": str(item.get("test_desc", "")), "evidence": evidence}
    return "kube-bench", results


def diff_results(old: Dict[str, Dict[str, Any]], new: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
    changed: List[Dict[str, Any]] = []
    only_old: List[Dict[str, str]] = []
    only_new: List[Dict[str, str]] = []
    same_count = 0
    for check_id in sorted(set(old) | set(new), key=section_key):
        if check_id in old and check_id in new:
            before, after = old[check_id], new[check_id]
            if before["state"] == after["state"]:
                same_count += 1
                continue
            changed.append({"id": check_id, "title": after["title"] or before["title"], "old": before["state"], "new": after["state"],
                            "old_evidence": before["evidence"], "new_evidence": after["evidence"]})
        elif check_id in old:
            only_old.append({"id": check_id, "state": old[check_id]["state"], "title": old[check_id]["title"]})
        else:
            only_new.append({"id": check_id, "state": new[check_id]["state"], "title": new[check_id]["title"]})
    return {"same": same_count, "changed": changed, "only_old": only_old, "only_new": only_new}


def worse(change: Dict[str, Any]) -> bool:
    return RANK.get(change["new"], len(STATES)) > RANK.get(change["old"], len(STATES))


def diff_exit(result: Dict[str, Any], fail_on: str, common: bool) -> int:
    if fail_on == "none":
        return 0
    if fail_on == "any":
        return 1 if result["changed"] or (not common and (result["only_old"] or result["only_new"])) else 0
    regressions = [c for c in result["changed"] if worse(c)] + ([] if common else [n for n in result["only_new"] if n["state"] == "FAIL"])
    return 1 if regressions else 0


def render_diff(old_name: str, new_name: str, result: Dict[str, Any]) -> str:
    lines = [f"minibench diff: old={old_name} new={new_name}: {result['same']} same, {len(result['changed'])} changed, {len(result['only_old'])} only in old, {len(result['only_new'])} only in new"]
    for change in result["changed"]:
        lines.append(f"{change['id']:<10}{change['old']} -> {change['new']}  {change['title']}")
        lines.extend(f"    old: {line}" for line in change["old_evidence"])
        lines.extend(f"    new: {line}" for line in change["new_evidence"])
    for label, entries in (("only in old", result["only_old"]), ("only in new", result["only_new"])):
        if entries:
            shown = ", ".join(f"{i['id']} {i['state']}" for i in entries[:12])
            lines.append(f"{label}: {shown}" + (f", and {len(entries) - 12} more" if len(entries) > 12 else ""))
    return "\n".join(lines)


def render_checks(catalogue: Catalogue) -> str:
    lines = [f"{catalogue.benchmark} (kube-bench {catalogue.kube_bench}): {len(catalogue.checks)} checks", f"{'ID':<14}{'TARGET':<14}{'SCORED':<8}TITLE"]
    for check in catalogue.checks:
        lines.append(f"{check.id:<14}{check.target:<14}{'yes' if check.scored else 'no':<8}{check.title}")
    return "\n".join(lines)


def split_csv(text: str) -> List[str]:
    return [part.strip() for part in (text or "").split(",") if part.strip()]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="minibench", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    checks = sub.add_parser("checks", help="list the checks in the catalogue")
    checks.add_argument("--checks", default=CHECKS_FILE, help="catalogue file (default code/checks.json)")
    checks.add_argument("--json", action="store_true")
    col = sub.add_parser("collect", help="read manifests, file modes, the kubelet config and process arguments from a node into a snapshot")
    col.add_argument("--node", required=True, help="docker:CONTAINER (a kind node), local (this machine) or root:/PATH (a copy of a node's filesystem)")
    col.add_argument("-o", "--output", default="-", help="snapshot file (default stdout)")
    run = sub.add_parser("run", help="evaluate the checks against a snapshot or a node")
    run.add_argument("snapshot", nargs="?", help="snapshot from minibench collect (- for stdin)")
    run.add_argument("--node", default="", help="collect from this node first instead of reading a snapshot")
    run.add_argument("--checks", default=CHECKS_FILE, help="catalogue file (default code/checks.json)")
    run.add_argument("--targets", default="", help=f"comma-separated subset of {','.join(TARGETS)} (default: what the node runs)")
    run.add_argument("--check", default="", help="comma-separated ids or groups, e.g. 1.2,4.2.1")
    run.add_argument("--fail-on", choices=("fail", "warn", "none"), default="fail", help="exit 1 when a result is FAIL (default), WARN or worse, or never")
    run.add_argument("--verbose", action="store_true", help="show evidence for passing checks too")
    form = run.add_mutually_exclusive_group()
    form.add_argument("--json", action="store_true")
    form.add_argument("--junit", action="store_true")
    dif = sub.add_parser("diff", help="compare two runs: minibench run --json or kube-bench --json, in any combination")
    dif.add_argument("old")
    dif.add_argument("new")
    dif.add_argument("--common", action="store_true", help="only checks present in both decide the exit code (for comparing two tools)")
    dif.add_argument("--fail-on", choices=("regression", "any", "none"), default="regression", help="exit 1 on a check that got worse (default), on any change, or never")
    dif.add_argument("--json", action="store_true")
    return parser


def write_output(text: str, path: str) -> None:
    if path == "-":
        print(text)
        return
    try:
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(text + "\n")
    except OSError as exc:
        raise BenchError(f"cannot write {path}: {exc.strerror}") from exc


def command_run(args: argparse.Namespace, runner: Runner) -> int:
    catalogue = load_catalogue(args.checks)
    if args.node:
        data = collect(make_source(args.node, runner))
    elif args.snapshot:
        data = read_json(args.snapshot)
    else:
        raise BenchError("run needs a snapshot file or --node")
    report = run_checks(catalogue, Snapshot(data), split_csv(args.targets), split_csv(args.check))
    if args.json:
        print(json.dumps(report.to_json(), indent=2))
    elif args.junit:
        print(render_junit(report))
    else:
        print(render_text(report, args.verbose))
    totals = report.totals()
    if args.fail_on == "fail":
        return 1 if totals["FAIL"] else 0
    if args.fail_on == "warn":
        return 1 if totals["FAIL"] or totals["WARN"] else 0
    return 0


def main(argv: Optional[Sequence[str]] = None, runner: Runner = subprocess_runner) -> int:
    try:
        args = build_parser().parse_args(list(sys.argv[1:] if argv is None else argv))
    except SystemExit as exc:
        return int(exc.code or 0)
    try:
        if args.command == "checks":
            catalogue = load_catalogue(args.checks)
            print(json.dumps([c.to_json() for c in catalogue.checks], indent=2) if args.json else render_checks(catalogue))
            return 0
        if args.command == "collect":
            data = collect(make_source(args.node, runner))
            write_output(json.dumps(data, indent=2), args.output)
            present = sorted(data["processes"])
            print(f"minibench: {data['node']}: {len(data['files'])} paths, {len(data['trees'].get(PKI_DIR, []))} PKI entries, processes: {', '.join(present) or 'none'}", file=sys.stderr)
            return 0
        if args.command == "run":
            return command_run(args, runner)
        old_name, old = load_results(args.old)
        new_name, new = load_results(args.new)
        result = diff_results(old, new)
        if args.json:
            print(json.dumps({"old": old_name, "new": new_name, **result}, indent=2))
        else:
            print(render_diff(old_name, new_name, result))
        return diff_exit(result, args.fail_on, args.common)
    except BenchError as exc:
        print(f"minibench: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
