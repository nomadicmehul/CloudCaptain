"""minikubelet: a node agent that turns pod manifests in a directory into processes or docker containers, with init containers, restart policies, CrashLoopBackOff, startup/liveness/readiness probes, graceful stop and a kubectl-style status table."""
from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import os
import re
import signal
import socket
import ssl
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

RESTART_POLICIES = ("Always", "OnFailure", "Never")
PROBE_FIELDS = {"startupProbe": "startup", "livenessProbe": "liveness", "readinessProbe": "readiness"}
PROBE_HANDLERS = ("exec", "httpGet", "tcpSocket")
MANIFEST_SUFFIXES = (".json", ".yaml", ".yml")
PAUSE_IMAGE = "registry.k8s.io/pause:3.10"
LABEL_POD = "minikubelet.pod"
LABEL_CONTAINER = "minikubelet.container"
LABEL_ROLE = "minikubelet.role"
DNS_LABEL = re.compile(r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?$")
DNS_SUBDOMAIN = re.compile(r"^[a-z0-9]([-a-z0-9.]*[a-z0-9])?$")
INT_RE = re.compile(r"^-?(0|[1-9][0-9]*)$")
FLOAT_RE = re.compile(r"^-?[0-9]+\.[0-9]+$")
ESCAPES = {"n": "\n", "t": "\t", "r": "\r", '"': '"', "\\": "\\", "/": "/", "0": "\0"}
EPSILON = 1e-6


class YamlError(ValueError):
    def __init__(self, line: int, message: str) -> None:
        super().__init__(f"line {line}: {message}")
        self.line = line
        self.detail = message


class ManifestError(ValueError):
    pass


class StartError(Exception):
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
            elif not rest:
                raise YamlError(number, "unterminated flow sequence; missing ]")
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
            if rest.startswith(":"):
                value, rest = parse_flow_value(rest[1:].lstrip(), number, ",}")
            else:
                value = None
            mapping[str(key)] = value
            rest = rest.lstrip()
            if rest.startswith(","):
                rest = rest[1:].lstrip()
            elif not rest:
                raise YamlError(number, "unterminated flow mapping; missing }")
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
        raise YamlError(number, "anchors and aliases are not supported; repeat the value")
    if text in ("|", ">") or text.startswith(("|-", ">-", "|+", ">+")):
        raise YamlError(number, "block scalars are not supported; use a quoted string or a list")
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
                if nested.text == "-" or nested.text.startswith("- "):
                    mapping[key] = self.parse_sequence(indent, compact=True)
                else:
                    mapping[key] = None
            else:
                mapping[key] = self.parse_block(nested.indent)


def parse_yaml(source: str) -> Any:
    return Parser(split_lines(source)).parse_document()


def seconds_text(value: float) -> str:
    return f"{value:g}s"


def go_duration(value: float) -> str:
    total = int(value)
    if total < 60:
        return f"{total}s"
    if total < 3600:
        return f"{total // 60}m{total % 60}s"
    return f"{total // 3600}h{(total % 3600) // 60}m{total % 60}s"


@dataclass
class Probe:
    kind: str
    handler: str
    command: List[str] = field(default_factory=list)
    path: str = "/"
    port: int = 0
    host: str = ""
    scheme: str = "HTTP"
    initial_delay: float = 0.0
    period: float = 10.0
    timeout: float = 1.0
    failure_threshold: int = 3
    success_threshold: int = 1

    def target(self) -> str:
        if self.handler == "exec":
            return f"exec [{' '.join(self.command)}]"
        if self.handler == "httpGet":
            return f"http-get {self.scheme.lower()}://{self.host}:{self.port}{self.path}"
        return f"tcp-socket {self.host}:{self.port}"

    def describe(self) -> str:
        timing = f"delay={go_duration(self.initial_delay)} timeout={go_duration(self.timeout)} period={go_duration(self.period)}"
        return f"{self.target()} {timing} #success={self.success_threshold} #failure={self.failure_threshold}"


@dataclass
class ContainerSpec:
    name: str
    image: str
    command: List[str] = field(default_factory=list)
    args: List[str] = field(default_factory=list)
    env: Dict[str, str] = field(default_factory=dict)
    ports: Dict[str, int] = field(default_factory=dict)
    probes: Dict[str, Probe] = field(default_factory=dict)
    pre_stop: Optional[List[str]] = None

    def argv(self) -> List[str]:
        return self.command + self.args


@dataclass
class PodSpec:
    name: str
    namespace: str
    restart_policy: str
    grace_period: float
    init_containers: List[ContainerSpec]
    containers: List[ContainerSpec]
    source: str = ""
    node: str = ""
    config_hash: str = ""

    @property
    def full_name(self) -> str:
        return f"{self.name}-{self.node}" if self.node else self.name

    @property
    def key(self) -> str:
        return f"{self.namespace}/{self.full_name}"


def integer(raw: Dict[str, Any], key: str, default: int, where: str, minimum: int) -> int:
    value = raw.get(key, default)
    if not isinstance(value, int) or isinstance(value, bool) or value < minimum:
        raise ManifestError(f"{where}.{key}: must be an integer >= {minimum}, got {value!r}")
    return value


def string_list(value: Any, where: str) -> List[str]:
    if value is None:
        return []
    if not isinstance(value, list) or any(isinstance(item, (bool, dict, list)) or item is None for item in value):
        raise ManifestError(f"{where}: must be a list of strings such as [python3, -m, http.server]; quote true, false and null")
    return [str(item) for item in value]


def parse_env(value: Any, where: str) -> Dict[str, str]:
    env: Dict[str, str] = {}
    if value is None:
        return env
    if not isinstance(value, list):
        raise ManifestError(f"{where}.env: must be a list of {{name: ..., value: ...}} entries")
    for index, item in enumerate(value):
        if not isinstance(item, dict) or not isinstance(item.get("name"), str):
            raise ManifestError(f"{where}.env[{index}]: needs name:")
        if "valueFrom" in item:
            raise ManifestError(f"{where}.env[{index}].valueFrom: not supported; static pods cannot read ConfigMaps or Secrets either")
        raw = item.get("value", "")
        if not isinstance(raw, str):
            raise ManifestError(f"{where}.env[{index}].value: must be a string, got {raw!r}; quote numbers and booleans")
        env[item["name"]] = raw
    return env


def parse_ports(value: Any, where: str) -> Dict[str, int]:
    ports: Dict[str, int] = {}
    if value is None:
        return ports
    if not isinstance(value, list):
        raise ManifestError(f"{where}.ports: must be a list of {{containerPort: N, name: ...}} entries")
    for index, item in enumerate(value):
        port = item.get("containerPort") if isinstance(item, dict) else None
        if not isinstance(port, int) or isinstance(port, bool) or not 0 < port < 65536:
            raise ManifestError(f"{where}.ports[{index}].containerPort: must be an integer between 1 and 65535")
        name = item.get("name")
        ports[str(name) if name else str(port)] = port
    return ports


def resolve_port(value: Any, ports: Dict[str, int], where: str) -> int:
    if isinstance(value, int) and not isinstance(value, bool) and 0 < value < 65536:
        return value
    if isinstance(value, str) and value in ports:
        return ports[value]
    named = ", ".join(sorted(name for name in ports if not name.isdigit())) or "none"
    raise ManifestError(f"{where}: {value!r} is neither a port number nor a named containerPort (named ports: {named})")


def parse_probe(raw: Any, kind: str, where: str, ports: Dict[str, int]) -> Probe:
    if not isinstance(raw, dict):
        raise ManifestError(f"{where}: expected a mapping with one of exec, httpGet, tcpSocket")
    handlers = [name for name in PROBE_HANDLERS if name in raw]
    if len(handlers) != 1:
        raise ManifestError(f"{where}: needs exactly one of exec, httpGet, tcpSocket (found {', '.join(handlers) or 'none'})")
    handler = handlers[0]
    body = raw[handler]
    if not isinstance(body, dict):
        raise ManifestError(f"{where}.{handler}: expected a mapping")
    probe = Probe(
        kind,
        handler,
        initial_delay=float(integer(raw, "initialDelaySeconds", 0, where, 0)),
        period=float(integer(raw, "periodSeconds", 10, where, 1)),
        timeout=float(integer(raw, "timeoutSeconds", 1, where, 1)),
        failure_threshold=integer(raw, "failureThreshold", 3, where, 1),
        success_threshold=integer(raw, "successThreshold", 1, where, 1),
    )
    if kind in ("liveness", "startup") and probe.success_threshold != 1:
        raise ManifestError(f"{where}.successThreshold: must be 1 for {kind} probes; the API server rejects anything else")
    if handler == "exec":
        probe.command = string_list(body.get("command"), f"{where}.exec.command")
        if not probe.command:
            raise ManifestError(f"{where}.exec.command: required, a list such as [cat, /tmp/ready]")
        return probe
    probe.port = resolve_port(body.get("port"), ports, f"{where}.{handler}.port")
    probe.host = str(body.get("host") or "")
    if handler == "httpGet":
        path = str(body.get("path") or "/")
        probe.path = path if path.startswith("/") else "/" + path
        probe.scheme = str(body.get("scheme") or "HTTP").upper()
        if probe.scheme not in ("HTTP", "HTTPS"):
            raise ManifestError(f"{where}.httpGet.scheme: must be HTTP or HTTPS")
    return probe


def parse_container(raw: Any, where: str, init: bool) -> ContainerSpec:
    if not isinstance(raw, dict):
        raise ManifestError(f"{where}: expected a mapping with name: and image:")
    name = raw.get("name")
    if not isinstance(name, str) or len(name) > 63 or not DNS_LABEL.match(name):
        raise ManifestError(f"{where}.name: must be a DNS label (lowercase letters, digits and -), got {name!r}")
    image = raw.get("image")
    if not isinstance(image, str) or not image:
        raise ManifestError(f"{where}.image: required")
    if init and "restartPolicy" in raw:
        raise ManifestError(f"{where}.restartPolicy: sidecar init containers are not implemented in minikubelet (see Exercises)")
    ports = parse_ports(raw.get("ports"), where)
    probes: Dict[str, Probe] = {}
    for field_name, kind in PROBE_FIELDS.items():
        if field_name in raw:
            if init:
                raise ManifestError(f"{where}.{field_name}: init containers run to completion and cannot have probes")
            probes[kind] = parse_probe(raw[field_name], kind, f"{where}.{field_name}", ports)
    pre_stop = None
    lifecycle = raw.get("lifecycle")
    if lifecycle is not None:
        hook = lifecycle.get("preStop") if isinstance(lifecycle, dict) else None
        if init or hook is None or not isinstance(hook, dict) or not isinstance(hook.get("exec"), dict):
            raise ManifestError(f"{where}.lifecycle: only preStop exec hooks on app containers are implemented")
        pre_stop = string_list(hook["exec"].get("command"), f"{where}.lifecycle.preStop.exec.command")
        if not pre_stop:
            raise ManifestError(f"{where}.lifecycle.preStop.exec.command: required")
    return ContainerSpec(
        name,
        image,
        string_list(raw.get("command"), f"{where}.command"),
        string_list(raw.get("args"), f"{where}.args"),
        parse_env(raw.get("env"), where),
        ports,
        probes,
        pre_stop,
    )


def config_hash(data: Any, node: str) -> str:
    canonical = json.dumps({"node": node, "pod": data}, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:32]


def parse_pod(data: Any, source: str, node: str = "") -> PodSpec:
    if not isinstance(data, dict):
        raise ManifestError(f"{source}: expected a Pod object (a mapping with apiVersion, kind, metadata, spec)")
    if data.get("kind") != "Pod":
        raise ManifestError(f"{source}: kind must be Pod for a static pod manifest, got {data.get('kind')!r}")
    metadata = data.get("metadata")
    name = metadata.get("name") if isinstance(metadata, dict) else None
    if not isinstance(name, str) or not DNS_SUBDOMAIN.match(name):
        raise ManifestError(f"{source}: metadata.name must be lowercase letters, digits, - and ., got {name!r}")
    namespace = metadata.get("namespace") or "default"
    if not isinstance(namespace, str) or not DNS_LABEL.match(namespace):
        raise ManifestError(f"{source}: metadata.namespace must be a DNS label, got {namespace!r}")
    spec = data.get("spec")
    if not isinstance(spec, dict):
        raise ManifestError(f"{source}: spec is required")
    raw_containers = spec.get("containers")
    if not isinstance(raw_containers, list) or not raw_containers:
        raise ManifestError(f"{source}: spec.containers must list at least one container")
    raw_init = spec.get("initContainers") or []
    if not isinstance(raw_init, list):
        raise ManifestError(f"{source}: spec.initContainers must be a list")
    policy = spec.get("restartPolicy", "Always")
    if policy not in RESTART_POLICIES:
        raise ManifestError(f"{source}: spec.restartPolicy must be one of {', '.join(RESTART_POLICIES)}, got {policy!r}")
    grace = integer(spec, "terminationGracePeriodSeconds", 30, f"{source}: spec", 0)
    init = [parse_container(item, f"{source}: spec.initContainers[{index}]", True) for index, item in enumerate(raw_init)]
    containers = [parse_container(item, f"{source}: spec.containers[{index}]", False) for index, item in enumerate(raw_containers)]
    seen = set()
    for container in init + containers:
        if container.name in seen:
            raise ManifestError(f"{source}: container name {container.name!r} is used twice; names are unique across initContainers and containers")
        seen.add(container.name)
    return PodSpec(name, namespace, policy, float(grace), init, containers, source, node, config_hash(data, node))


def decode_manifest(path: str, text: str) -> Any:
    name = os.path.basename(path)
    if path.endswith(".json"):
        try:
            return json.loads(text)
        except json.JSONDecodeError as exc:
            raise ManifestError(f"{name}:{exc.lineno}: invalid JSON: {exc.msg}")
    try:
        return parse_yaml(text)
    except YamlError as exc:
        raise ManifestError(f"{name}:{exc.line}: {exc.detail}")


def load_manifest(path: str, node: str = "") -> PodSpec:
    try:
        with open(path, encoding="utf-8") as handle:
            text = handle.read()
    except OSError as exc:
        raise ManifestError(f"cannot read {path}: {exc.strerror}")
    return parse_pod(decode_manifest(path, text), os.path.basename(path), node)


def load_manifests(directory: str, node: str = "") -> Tuple[Dict[str, PodSpec], Dict[str, str]]:
    pods: Dict[str, PodSpec] = {}
    errors: Dict[str, str] = {}
    for name in sorted(os.listdir(directory)):
        path = os.path.join(directory, name)
        if name.startswith(".") or not name.endswith(MANIFEST_SUFFIXES) or not os.path.isfile(path):
            continue
        try:
            pod = load_manifest(path, node)
        except ManifestError as exc:
            errors[name] = str(exc)
            continue
        if pod.key in pods:
            errors[name] = f"{name}: pod {pod.key} is already defined in {pods[pod.key].source}; ignoring this file"
            continue
        pods[pod.key] = pod
    return pods, errors


@dataclass
class CommandResult:
    returncode: int
    stdout: str = ""
    stderr: str = ""


def subprocess_runner(args: Sequence[str], timeout: Optional[float] = None) -> CommandResult:
    try:
        completed = subprocess.run(list(args), capture_output=True, text=True, timeout=timeout, check=False)
    except FileNotFoundError:
        return CommandResult(127, "", f"{args[0]}: command not found")
    except subprocess.TimeoutExpired:
        return CommandResult(-1, "", f"timed out after {timeout:g}s")
    return CommandResult(completed.returncode, completed.stdout, completed.stderr)


def exit_code_of(returncode: int) -> int:
    return 128 - returncode if returncode < 0 else returncode


class ProcessRuntime:
    name = "process"

    def __init__(self, root: str) -> None:
        self.root = root
        self.procs: Dict[str, subprocess.Popen] = {}
        self.dirs: Dict[str, str] = {}
        self.envs: Dict[str, Dict[str, str]] = {}
        self.log_paths: Dict[str, str] = {}

    def run_sandbox(self, pod: PodSpec) -> str:
        sandbox = os.path.join(self.root, f"{pod.namespace}_{pod.full_name}_{pod.config_hash[:8]}")
        os.makedirs(sandbox, exist_ok=True)
        return sandbox

    def address(self, sandbox: str) -> str:
        return "127.0.0.1"

    def start(self, sandbox: str, pod: PodSpec, container: ContainerSpec, attempt: int) -> str:
        argv = container.argv()
        if not argv:
            raise StartError(f"the process runtime cannot read image entrypoints; set command: on {container.name}")
        cid = f"{os.path.basename(sandbox)}_{container.name}_{attempt}"
        env = dict(os.environ)
        env.update(container.env)
        log_path = os.path.join(sandbox, f"{container.name}-{attempt}.log")
        with open(log_path, "wb") as log:
            try:
                proc = subprocess.Popen(argv, cwd=sandbox, env=env, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
            except OSError as exc:
                raise StartError(f"exec: {argv[0]!r}: {exc.strerror}")
        self.procs[cid] = proc
        self.dirs[cid] = sandbox
        self.envs[cid] = env
        self.log_paths[cid] = log_path
        return cid

    def status(self, cid: str) -> Optional[int]:
        proc = self.procs.get(cid)
        if proc is None:
            return 255
        code = proc.poll()
        return None if code is None else exit_code_of(code)

    def kill(self, cid: str, sig: int) -> None:
        proc = self.procs.get(cid)
        if proc is None or proc.poll() is not None:
            return
        try:
            os.killpg(proc.pid, sig)
        except ProcessLookupError:
            pass
        except PermissionError:
            proc.send_signal(sig)

    def exec_sync(self, cid: str, argv: Sequence[str], timeout: float) -> Tuple[int, str]:
        try:
            done = subprocess.run(list(argv), cwd=self.dirs.get(cid), env=self.envs.get(cid), capture_output=True, text=True, timeout=timeout, check=False)
        except subprocess.TimeoutExpired:
            return -1, f"command timed out after {timeout:g}s"
        except OSError as exc:
            return 127, f"exec: {argv[0]!r}: {exc.strerror}"
        return done.returncode, (done.stdout + done.stderr).strip()

    def logs(self, cid: str) -> str:
        try:
            with open(self.log_paths[cid], encoding="utf-8", errors="replace") as handle:
                return handle.read()
        except (KeyError, OSError):
            return ""

    def remove(self, cid: str) -> None:
        proc = self.procs.pop(cid, None)
        if proc is None:
            return
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass

    def remove_sandbox(self, sandbox: str) -> None:
        return None


def docker_name(text: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_.-]", "_", text)


class DockerRuntime:
    name = "docker"

    def __init__(self, runner: Callable[..., CommandResult] = subprocess_runner, pause_image: str = PAUSE_IMAGE) -> None:
        self.runner = runner
        self.pause_image = pause_image

    def docker(self, *args: str, timeout: Optional[float] = None) -> CommandResult:
        return self.runner(["docker", *args], timeout=timeout)

    def run_sandbox(self, pod: PodSpec) -> str:
        name = docker_name(f"mk_{pod.namespace}_{pod.full_name}_{pod.config_hash[:8]}")
        self.docker("rm", "-f", name)
        result = self.docker("run", "-d", "--name", name, "--ipc", "shareable", "--label", f"{LABEL_POD}={pod.key}", "--label", f"{LABEL_ROLE}=sandbox", self.pause_image)
        if result.returncode != 0:
            raise StartError(f"RunPodSandbox: {result.stderr.strip() or 'docker run failed'}")
        return name

    def address(self, sandbox: str) -> str:
        result = self.docker("inspect", "-f", "{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}", sandbox)
        return result.stdout.strip() or "127.0.0.1"

    def start(self, sandbox: str, pod: PodSpec, container: ContainerSpec, attempt: int) -> str:
        name = f"{sandbox}_{container.name}_{attempt}"
        args = ["run", "-d", "--name", name, "--network", f"container:{sandbox}", "--ipc", f"container:{sandbox}"]
        args += ["--label", f"{LABEL_POD}={pod.key}", "--label", f"{LABEL_CONTAINER}={container.name}", "--label", f"{LABEL_ROLE}=container"]
        for key, value in container.env.items():
            args += ["-e", f"{key}={value}"]
        if container.command:
            args += ["--entrypoint", container.command[0]]
        args.append(container.image)
        args += container.command[1:] + container.args
        result = self.docker(*args)
        if result.returncode != 0:
            self.docker("rm", "-f", name)
            raise StartError(result.stderr.strip() or f"docker run exited {result.returncode}")
        return name

    def status(self, cid: str) -> Optional[int]:
        result = self.docker("inspect", "-f", "{{.State.Running}} {{.State.ExitCode}}", cid)
        if result.returncode != 0:
            return 255
        running, _, code = result.stdout.strip().partition(" ")
        if running == "true":
            return None
        return int(code) if INT_RE.match(code) else 255

    def kill(self, cid: str, sig: int) -> None:
        self.docker("kill", "--signal", signal.Signals(sig).name, cid)

    def exec_sync(self, cid: str, argv: Sequence[str], timeout: float) -> Tuple[int, str]:
        result = self.docker("exec", cid, *argv, timeout=timeout)
        if result.returncode == -1:
            return -1, f"command timed out after {timeout:g}s"
        return result.returncode, (result.stdout + result.stderr).strip()

    def logs(self, cid: str) -> str:
        result = self.docker("logs", cid)
        return result.stdout + result.stderr

    def remove(self, cid: str) -> None:
        self.docker("rm", "-f", cid)

    def remove_sandbox(self, sandbox: str) -> None:
        self.docker("rm", "-f", sandbox)


@dataclass
class ProbeResult:
    ok: bool
    detail: str


def http_get(url: str, timeout: float) -> ProbeResult:
    request = urllib.request.Request(url, headers={"User-Agent": "minikubelet-probe/1.0", "Accept": "*/*"})
    handlers: List[Any] = [urllib.request.ProxyHandler({})]
    if url.startswith("https://"):
        context = ssl.create_default_context()
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
        handlers.append(urllib.request.HTTPSHandler(context=context))
    try:
        with urllib.request.build_opener(*handlers).open(request, timeout=timeout) as response:
            code = response.status
    except urllib.error.HTTPError as exc:
        code = exc.code
        exc.close()
    except (urllib.error.URLError, OSError) as exc:
        reason = getattr(exc, "reason", exc)
        return ProbeResult(False, f'Get "{url}": {reason}')
    if 200 <= code < 400:
        return ProbeResult(True, f"HTTP {code}")
    return ProbeResult(False, f"HTTP probe failed with statuscode: {code}")


def tcp_connect(host: str, port: int, timeout: float) -> ProbeResult:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return ProbeResult(True, f"connected to {host}:{port}")
    except OSError as exc:
        return ProbeResult(False, f"dial tcp {host}:{port}: {exc.strerror or exc}")


class Prober:
    def __init__(self, runtime: Any, http: Callable[[str, float], ProbeResult] = http_get, tcp: Callable[[str, int, float], ProbeResult] = tcp_connect) -> None:
        self.runtime = runtime
        self.http = http
        self.tcp = tcp

    def run(self, probe: Probe, cid: str, pod_ip: str) -> ProbeResult:
        if probe.handler == "exec":
            code, output = self.runtime.exec_sync(cid, probe.command, probe.timeout)
            if code == 0:
                return ProbeResult(True, output)
            if code == -1:
                return ProbeResult(False, f"command {probe.target()[5:]} timed out after {seconds_text(probe.timeout)}")
            return ProbeResult(False, (output or f"command {probe.target()[5:]} exited with {code}")[:200])
        host = probe.host or pod_ip
        if probe.handler == "httpGet":
            return self.http(f"{probe.scheme.lower()}://{host}:{probe.port}{probe.path}", probe.timeout)
        return self.tcp(host, probe.port, probe.timeout)


@dataclass
class Backoff:
    base: float = 10.0
    cap: float = 300.0
    entries: Dict[str, Tuple[float, float]] = field(default_factory=dict)

    def expired(self, entry: Tuple[float, float], finished_at: float) -> bool:
        return finished_at - entry[1] > 2 * self.cap

    def remaining(self, key: str, finished_at: float, now: float) -> Optional[float]:
        entry = self.entries.get(key)
        if entry is None or self.expired(entry, finished_at):
            return None
        left = finished_at + entry[0] - now
        return left if left > EPSILON else None

    def next(self, key: str, finished_at: float, now: float) -> float:
        entry = self.entries.get(key)
        delay = self.base if entry is None or self.expired(entry, finished_at) else min(entry[0] * 2, self.cap)
        self.entries[key] = (delay, now)
        return delay

    def delay(self, key: str) -> float:
        return self.entries[key][0]


def backoff_schedule(base: float, cap: float, restarts: int) -> List[float]:
    backoff = Backoff(base, cap)
    clock = 0.0
    waits: List[float] = []
    for _ in range(restarts):
        finished = clock
        wait = backoff.remaining("container", finished, finished) or 0.0
        clock = finished + wait
        backoff.next("container", finished, clock)
        waits.append(round(wait, 6))
    return waits


@dataclass
class ProbeTracker:
    probe: Probe
    last_run: Optional[float] = None
    successes: int = 0
    failures: int = 0
    calls: int = 0

    def due(self, now: float, started_at: float) -> bool:
        if now + EPSILON < started_at + self.probe.initial_delay:
            return False
        return self.last_run is None or now + EPSILON >= self.last_run + self.probe.period

    def record(self, ok: bool, now: float) -> Optional[bool]:
        self.last_run = now
        self.calls += 1
        if ok:
            self.successes += 1
            self.failures = 0
            return True if self.successes >= self.probe.success_threshold else None
        self.failures += 1
        self.successes = 0
        return False if self.failures >= self.probe.failure_threshold else None


@dataclass
class ContainerState:
    spec: ContainerSpec
    init: bool = False
    cid: Optional[str] = None
    previous_cid: Optional[str] = None
    attempt: int = 0
    state: str = "waiting"
    reason: str = "ContainerCreating"
    message: str = ""
    started_at: Optional[float] = None
    terminations: List[Dict[str, Any]] = field(default_factory=list)
    started: bool = False
    ready: bool = False
    trackers: Dict[str, ProbeTracker] = field(default_factory=dict)
    kill_deadline: Optional[float] = None
    kill_grace: float = 0.0
    kill_escalated: bool = False

    @property
    def restart_count(self) -> int:
        return max(self.attempt - 1, 0)

    @property
    def last_exit(self) -> Optional[int]:
        return self.terminations[-1]["exitCode"] if self.terminations else None

    @property
    def succeeded(self) -> bool:
        return self.state == "terminated" and self.last_exit == 0


@dataclass
class PodState:
    spec: PodSpec
    created_at: float
    init: List[ContainerState]
    containers: List[ContainerState]
    sandbox: Optional[str] = None
    sandbox_error: str = ""
    pod_ip: str = ""
    deleting_since: Optional[float] = None
    replacement: Optional[PodSpec] = None
    init_failed: bool = False
    finished: bool = False

    def all(self) -> List[ContainerState]:
        return self.init + self.containers


@dataclass
class Event:
    at: float
    type: str
    reason: str
    pod: str
    container: str
    message: str
    delay: Optional[float] = None
    exit_code: Optional[int] = None


def should_restart(policy: str, exit_code: Optional[int]) -> bool:
    if policy == "Always":
        return True
    if policy == "OnFailure":
        return exit_code != 0
    return False


def init_complete(state: PodState) -> bool:
    return all(container.succeeded for container in state.init)


class Kubelet:
    def __init__(
        self,
        runtime: Any,
        node: str = "",
        backoff_base: float = 10.0,
        backoff_cap: float = 300.0,
        prober: Optional[Prober] = None,
        now: Callable[[], float] = time.time,
        emit: Optional[Callable[[Event], None]] = None,
        min_grace: float = 2.0,
    ) -> None:
        self.runtime = runtime
        self.node = node
        self.backoff = Backoff(backoff_base, backoff_cap)
        self.prober = prober or Prober(runtime)
        self.now = now
        self.emit = emit or (lambda event: None)
        self.min_grace = min_grace
        self.pods: Dict[str, PodState] = {}
        self.events: List[Event] = []

    def event(self, state: PodState, kind: str, reason: str, container: str, message: str, **extra: Any) -> None:
        event = Event(self.now(), kind, reason, state.spec.key, container, message, **extra)
        self.events.append(event)
        self.emit(event)

    def new_pod(self, spec: PodSpec, now: float) -> PodState:
        waiting = "PodInitializing" if spec.init_containers else "ContainerCreating"
        return PodState(
            spec,
            now,
            [ContainerState(container, init=True, reason="PodInitializing") for container in spec.init_containers],
            [ContainerState(container, reason=waiting) for container in spec.containers],
        )

    def sync(self, desired: Dict[str, PodSpec]) -> None:
        now = self.now()
        for key, spec in desired.items():
            state = self.pods.get(key)
            if state is None:
                self.pods[key] = self.new_pod(spec, now)
            elif state.deleting_since is None and state.spec.config_hash != spec.config_hash:
                state.replacement = spec
                self.begin_termination(state, now)
            elif state.replacement is not None:
                state.replacement = spec
        for key, state in self.pods.items():
            if key not in desired:
                state.replacement = None
                if state.deleting_since is None:
                    self.begin_termination(state, now)
        for key in list(self.pods):
            state = self.pods[key]
            self.sync_pod(state)
            if state.finished:
                if state.replacement is not None:
                    self.pods[key] = self.new_pod(state.replacement, self.now())
                else:
                    del self.pods[key]

    def sync_pod(self, state: PodState) -> None:
        now = self.now()
        if state.sandbox is None and state.deleting_since is None:
            try:
                state.sandbox = self.runtime.run_sandbox(state.spec)
                state.pod_ip = self.runtime.address(state.sandbox)
            except StartError as exc:
                if str(exc) != state.sandbox_error:
                    state.sandbox_error = str(exc)
                    self.event(state, "Warning", "FailedCreatePodSandBox", "", str(exc))
                return
        for container in state.all():
            self.refresh(state, container, now)
        if state.deleting_since is not None:
            self.continue_termination(state)
            return
        if not self.sync_init(state, now):
            return
        for container in state.containers:
            self.sync_container(state, container, now)

    def refresh(self, state: PodState, container: ContainerState, now: float) -> None:
        if container.state != "running" or container.cid is None:
            return
        code = self.runtime.status(container.cid)
        if code is not None:
            self.record_exit(state, container, code, now)
            return
        if container.kill_deadline is not None and not container.kill_escalated and now + EPSILON >= container.kill_deadline:
            self.runtime.kill(container.cid, signal.SIGKILL)
            container.kill_escalated = True
            self.event(state, "Normal", "GraceExpired", container.spec.name, f"still running {seconds_text(container.kill_grace)} after SIGTERM; sent SIGKILL")

    def record_exit(self, state: PodState, container: ContainerState, code: int, now: float, reason: Optional[str] = None, message: str = "") -> None:
        reason = reason or ("Completed" if code == 0 else "Error")
        termination = {"exitCode": code, "reason": reason, "startedAt": container.started_at, "finishedAt": now, "message": message}
        container.terminations = (container.terminations + [termination])[-2:]
        container.state = "terminated"
        container.reason = reason
        container.message = message
        container.ready = False
        container.started = False
        container.kill_deadline = None
        container.kill_escalated = False
        self.event(state, "Normal" if code == 0 else "Warning", "Exited", container.spec.name, f"exit code {code} ({reason})", exit_code=code)

    def sync_init(self, state: PodState, now: float) -> bool:
        if state.init_failed:
            return False
        for container in state.init:
            if container.succeeded:
                continue
            if container.state == "running":
                return False
            if container.terminations and state.spec.restart_policy == "Never":
                state.init_failed = True
                return False
            self.start_or_back_off(state, container, now)
            return False
        return True

    def sync_container(self, state: PodState, container: ContainerState, now: float) -> None:
        if container.state == "running":
            if container.kill_deadline is None:
                self.probe(state, container, now)
            return
        if container.attempt == 0:
            self.start(state, container, now)
            return
        if should_restart(state.spec.restart_policy, container.last_exit):
            self.start_or_back_off(state, container, now)

    def start_or_back_off(self, state: PodState, container: ContainerState, now: float) -> None:
        if container.terminations:
            key = f"{state.spec.key}/{container.spec.name}"
            finished = container.terminations[-1]["finishedAt"]
            if self.backoff.remaining(key, finished, now) is not None:
                if container.reason != "CrashLoopBackOff":
                    delay = self.backoff.delay(key)
                    name = container.spec.name
                    container.state = "waiting"
                    container.reason = "CrashLoopBackOff"
                    container.message = f"back-off {seconds_text(delay)} restarting failed container={name} pod={state.spec.full_name}_{state.spec.namespace}({state.spec.config_hash})"
                    self.event(state, "Warning", "BackOff", name, f"Back-off restarting failed container {name} in pod {state.spec.full_name}", delay=delay)
                return
            self.backoff.next(key, finished, now)
        self.start(state, container, now)

    def start(self, state: PodState, container: ContainerState, now: float) -> None:
        container.attempt += 1
        name = container.spec.name
        try:
            cid = self.runtime.start(state.sandbox, state.spec, container.spec, container.attempt)
        except StartError as exc:
            container.started_at = now
            self.event(state, "Warning", "Failed", name, f"Error: {exc}")
            self.record_exit(state, container, 128, now, reason="StartError", message=str(exc))
            return
        stale = container.previous_cid
        container.previous_cid = container.cid
        container.cid = cid
        if stale is not None:
            self.runtime.remove(stale)
        container.state = "running"
        container.reason = ""
        container.message = ""
        container.started_at = now
        container.ready = False
        container.started = "startup" not in container.spec.probes
        container.trackers = {kind: ProbeTracker(probe) for kind, probe in container.spec.probes.items()}
        self.event(state, "Normal", "Created", name, f"Created container {name}")
        self.event(state, "Normal", "Started", name, f"Started container {name}")

    def run_probe(self, state: PodState, container: ContainerState, tracker: ProbeTracker, now: float) -> Optional[bool]:
        result = self.prober.run(tracker.probe, container.cid, state.pod_ip)
        if not result.ok:
            self.event(state, "Warning", "Unhealthy", container.spec.name, f"{tracker.probe.kind.capitalize()} probe failed: {result.detail}")
        return tracker.record(result.ok, now)

    def probe(self, state: PodState, container: ContainerState, now: float) -> None:
        name = container.spec.name
        startup = container.trackers.get("startup")
        if startup is not None and not container.started:
            if not startup.due(now, container.started_at):
                return
            verdict = self.run_probe(state, container, startup, now)
            if verdict is False:
                self.kill(state, container, f"Container {name} failed startup probe, will be restarted")
                return
            if verdict is not True:
                return
            container.started = True
        liveness = container.trackers.get("liveness")
        if liveness is not None and liveness.due(now, container.started_at):
            if self.run_probe(state, container, liveness, now) is False:
                self.kill(state, container, f"Container {name} failed liveness probe, will be restarted")
                return
        readiness = container.trackers.get("readiness")
        if readiness is None:
            self.set_ready(state, container, True)
            return
        if readiness.due(now, container.started_at):
            verdict = self.run_probe(state, container, readiness, now)
            if verdict is not None:
                self.set_ready(state, container, verdict)

    def set_ready(self, state: PodState, container: ContainerState, ready: bool) -> None:
        if container.ready == ready:
            return
        container.ready = ready
        self.event(state, "Normal", "Ready" if ready else "NotReady", container.spec.name, "container is ready" if ready else "container is not ready")

    def kill(self, state: PodState, container: ContainerState, message: str) -> None:
        if container.cid is None or container.state != "running" or container.kill_deadline is not None:
            return
        began = self.now()
        grace = state.spec.grace_period
        self.event(state, "Normal", "Killing", container.spec.name, message)
        if container.spec.pre_stop and grace > 0:
            code, output = self.runtime.exec_sync(container.cid, container.spec.pre_stop, grace)
            if code != 0:
                self.event(state, "Warning", "FailedPreStopHook", container.spec.name, f"PreStopHook failed: {output or code}")
            grace -= self.now() - began
        grace = max(grace, self.min_grace)
        container.ready = False
        self.runtime.kill(container.cid, signal.SIGTERM)
        container.kill_grace = grace
        container.kill_deadline = self.now() + grace

    def begin_termination(self, state: PodState, now: float) -> None:
        state.deleting_since = now
        for container in state.all():
            if container.state == "running":
                self.kill(state, container, f"Stopping container {container.spec.name}")

    def continue_termination(self, state: PodState) -> None:
        if any(container.state == "running" for container in state.all()):
            return
        for container in state.all():
            for cid in (container.previous_cid, container.cid):
                if cid is not None:
                    self.runtime.remove(cid)
        if state.sandbox is not None:
            self.runtime.remove_sandbox(state.sandbox)
        state.finished = True
        self.event(state, "Normal", "PodRemoved", "", "all containers stopped; sandbox removed")

    def shutdown(self, sleep: Callable[[float], None] = time.sleep, interval: float = 0.1) -> None:
        if not self.pods:
            return
        longest = max(state.spec.grace_period for state in self.pods.values())
        deadline = self.now() + max(longest, self.min_grace) + 5.0
        while self.pods and self.now() < deadline:
            self.sync({})
            if self.pods:
                sleep(interval)


def iso(ts: Optional[float]) -> Optional[str]:
    if ts is None:
        return None
    return datetime.datetime.fromtimestamp(ts, tz=datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_time(text: Optional[str]) -> Optional[float]:
    if not text:
        return None
    try:
        return datetime.datetime.strptime(text[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=datetime.timezone.utc).timestamp()
    except ValueError:
        return None


def termination_object(termination: Dict[str, Any]) -> Dict[str, Any]:
    out = {"exitCode": termination["exitCode"], "reason": termination["reason"], "startedAt": iso(termination["startedAt"]), "finishedAt": iso(termination["finishedAt"])}
    if termination.get("message"):
        out["message"] = termination["message"]
    return out


def container_status(container: ContainerState, runtime_name: str = "") -> Dict[str, Any]:
    if container.state == "running":
        current: Dict[str, Any] = {"running": {"startedAt": iso(container.started_at)}}
        previous = container.terminations[-1] if container.terminations else None
    elif container.state == "terminated":
        current = {"terminated": termination_object(container.terminations[-1])}
        previous = container.terminations[-2] if len(container.terminations) > 1 else None
    else:
        waiting = {"reason": container.reason}
        if container.message:
            waiting["message"] = container.message
        current = {"waiting": waiting}
        previous = container.terminations[-1] if container.terminations else None
    status: Dict[str, Any] = {
        "name": container.spec.name,
        "image": container.spec.image,
        "ready": container.ready,
        "started": container.state == "running" and container.started,
        "restartCount": container.restart_count,
        "state": current,
        "lastState": {"terminated": termination_object(previous)} if previous else {},
    }
    if container.cid:
        status["containerID"] = f"{runtime_name}://{container.cid}" if runtime_name else container.cid
    return status


def pod_phase(state: PodState) -> str:
    if state.init_failed:
        return "Failed"
    if not init_complete(state):
        return "Pending"
    running = waiting = stopped = succeeded = 0
    for container in state.containers:
        if container.state == "running":
            running += 1
        elif container.state == "terminated":
            stopped += 1
            succeeded += 1 if container.last_exit == 0 else 0
        elif container.terminations:
            stopped += 1
        else:
            waiting += 1
    if waiting:
        return "Pending"
    if running:
        return "Running"
    if state.spec.restart_policy == "Always":
        return "Running"
    if stopped == succeeded:
        return "Succeeded"
    if state.spec.restart_policy == "Never":
        return "Failed"
    return "Running"


def condition(kind: str, ok: bool, reason: str = "", message: str = "") -> Dict[str, str]:
    out = {"type": kind, "status": "True" if ok else "False"}
    if not ok and reason:
        out["reason"] = reason
        out["message"] = message
    return out


def pod_conditions(state: PodState, phase: str) -> List[Dict[str, str]]:
    incomplete = [container.spec.name for container in state.init if not container.succeeded]
    unready = [container.spec.name for container in state.containers if not container.ready]
    ready_reason = "PodCompleted" if phase in ("Succeeded", "Failed") else "ContainersNotReady"
    return [
        condition("PodReadyToStartContainers", state.sandbox is not None),
        condition("Initialized", not incomplete, "ContainersNotInitialized", f"containers with incomplete status: [{' '.join(incomplete)}]"),
        condition("ContainersReady", not unready, ready_reason, f"containers with unready status: [{' '.join(unready)}]"),
        condition("Ready", not unready, ready_reason, f"containers with unready status: [{' '.join(unready)}]"),
    ]


def pod_object(state: PodState, runtime_name: str = "") -> Dict[str, Any]:
    spec = state.spec
    phase = pod_phase(state)
    metadata: Dict[str, Any] = {
        "name": spec.full_name,
        "namespace": spec.namespace,
        "uid": spec.config_hash,
        "creationTimestamp": iso(state.created_at),
        "annotations": {"kubernetes.io/config.hash": spec.config_hash, "kubernetes.io/config.source": "file", "minikubelet/manifest": spec.source},
    }
    if state.deleting_since is not None:
        metadata["deletionTimestamp"] = iso(state.deleting_since)
    pod_spec: Dict[str, Any] = {
        "nodeName": spec.node,
        "restartPolicy": spec.restart_policy,
        "terminationGracePeriodSeconds": int(spec.grace_period),
        "containers": [{"name": c.name, "image": c.image} for c in spec.containers],
    }
    status: Dict[str, Any] = {"phase": phase, "conditions": pod_conditions(state, phase), "podIP": state.pod_ip, "startTime": iso(state.created_at)}
    if spec.init_containers:
        pod_spec["initContainers"] = [{"name": c.name, "image": c.image} for c in spec.init_containers]
        status["initContainerStatuses"] = [container_status(c, runtime_name) for c in state.init]
    status["containerStatuses"] = [container_status(c, runtime_name) for c in state.containers]
    return {"apiVersion": "v1", "kind": "Pod", "metadata": metadata, "spec": pod_spec, "status": status}


def pod_list(kubelet: Kubelet) -> Dict[str, Any]:
    runtime_name = getattr(kubelet.runtime, "name", "")
    items = [pod_object(kubelet.pods[key], runtime_name) for key in sorted(kubelet.pods)]
    return {"apiVersion": "v1", "kind": "List", "items": items}


def ready_condition(status: Dict[str, Any]) -> bool:
    return any(c.get("type") == "Ready" and c.get("status") == "True" for c in status.get("conditions") or [])


def terminated_text(terminated: Dict[str, Any], prefix: str = "") -> str:
    if terminated.get("reason"):
        return prefix + terminated["reason"]
    if terminated.get("signal"):
        return f"{prefix}Signal:{terminated['signal']}"
    return f"{prefix}ExitCode:{terminated.get('exitCode')}"


def status_reason(pod: Dict[str, Any]) -> str:
    status = pod.get("status") or {}
    reason = status.get("reason") or status.get("phase") or "Unknown"
    init_statuses = status.get("initContainerStatuses") or []
    init_total = len((pod.get("spec") or {}).get("initContainers") or init_statuses)
    initializing = False
    for index, container in enumerate(init_statuses):
        state = container.get("state") or {}
        terminated = state.get("terminated")
        waiting = state.get("waiting")
        if terminated is not None and terminated.get("exitCode") == 0:
            continue
        if terminated is not None:
            reason = terminated_text(terminated, "Init:")
        elif waiting and waiting.get("reason") and waiting["reason"] != "PodInitializing":
            reason = "Init:" + waiting["reason"]
        else:
            reason = f"Init:{index}/{init_total}"
        initializing = True
        break
    if not initializing:
        has_running = False
        for container in reversed(status.get("containerStatuses") or []):
            state = container.get("state") or {}
            waiting = state.get("waiting")
            terminated = state.get("terminated")
            if waiting and waiting.get("reason"):
                reason = waiting["reason"]
            elif terminated is not None:
                reason = terminated_text(terminated)
            elif container.get("ready") and state.get("running") is not None:
                has_running = True
        if reason == "Completed" and has_running:
            reason = "Running" if ready_condition(status) else "NotReady"
    if (pod.get("metadata") or {}).get("deletionTimestamp") and status.get("phase") not in ("Succeeded", "Failed"):
        reason = "Terminating"
    return reason


def human_age(seconds: float) -> str:
    seconds = int(seconds)
    if seconds < 0:
        return "0s"
    if seconds < 120:
        return f"{seconds}s"
    minutes = seconds // 60
    if minutes < 10:
        return f"{minutes}m{seconds % 60}s" if seconds % 60 else f"{minutes}m"
    if minutes < 180:
        return f"{minutes}m"
    hours = minutes // 60
    if hours < 8:
        return f"{hours}h{minutes % 60}m" if minutes % 60 else f"{hours}h"
    if hours < 48:
        return f"{hours}h"
    if hours < 24 * 8:
        return f"{hours // 24}d{hours % 24}h" if hours % 24 else f"{hours // 24}d"
    return f"{hours // 24}d"


def table_rows(pods: Sequence[Dict[str, Any]], now: float) -> List[Dict[str, Any]]:
    rows = []
    for pod in pods:
        metadata = pod.get("metadata") or {}
        status = pod.get("status") or {}
        statuses = status.get("containerStatuses") or []
        total = len((pod.get("spec") or {}).get("containers") or statuses)
        ready = sum(1 for c in statuses if c.get("ready"))
        initializing = status_reason(pod).startswith("Init:")
        counted = (status.get("initContainerStatuses") or []) if initializing else statuses
        created = parse_time(metadata.get("creationTimestamp"))
        rows.append({
            "namespace": metadata.get("namespace", "default"),
            "name": metadata.get("name", "?"),
            "ready": f"{ready}/{total}",
            "status": status_reason(pod),
            "restarts": sum(int(c.get("restartCount") or 0) for c in counted),
            "age": human_age(now - created) if created is not None else "<unknown>",
        })
    return rows


def render_table(rows: Sequence[Dict[str, Any]]) -> str:
    if not rows:
        return "No resources found"
    header = ["NAMESPACE", "NAME", "READY", "STATUS", "RESTARTS", "AGE"]
    keys = ["namespace", "name", "ready", "status", "restarts", "age"]
    cells = [header] + [[str(row[key]) for key in keys] for row in rows]
    widths = [max(len(line[i]) for line in cells) for i in range(len(header))]
    return "\n".join("   ".join(cell.ljust(widths[i]) for i, cell in enumerate(line)).rstrip() for line in cells)


def load_pod_objects(path: str) -> List[Dict[str, Any]]:
    with open(path, encoding="utf-8") as handle:
        data = json.load(handle)
    if isinstance(data, list):
        return data
    if isinstance(data, dict) and isinstance(data.get("items"), list):
        return list(data["items"])
    if isinstance(data, dict) and "metadata" in data:
        return [data]
    raise ValueError(f"{path}: expected a Pod, a List of pods or a status file")


def event_object(event: Event, start: float) -> Dict[str, Any]:
    out = {"t": round(event.at - start, 3), "at": iso(event.at), "type": event.type, "reason": event.reason, "pod": event.pod, "container": event.container, "message": event.message}
    if event.delay is not None:
        out["delay"] = event.delay
    if event.exit_code is not None:
        out["exitCode"] = event.exit_code
    return out


def format_event(event: Event, start: float) -> str:
    return f"{event.at - start:7.1f}s  {event.type:<7}  {event.reason:<13} {event.pod} {event.container or '-'}: {event.message}"


class EventLog:
    def __init__(self, start: float, stream: Any = None, path: Optional[str] = None) -> None:
        self.start = start
        self.stream = stream
        self.path = path

    def __call__(self, event: Event) -> None:
        if self.stream is not None:
            print(format_event(event, self.start), file=self.stream, flush=True)
        if self.path:
            with open(self.path, "a", encoding="utf-8") as handle:
                handle.write(json.dumps(event_object(event, self.start)) + "\n")


def write_json_atomic(path: str, data: Any) -> None:
    folder = os.path.dirname(os.path.abspath(path))
    fd, temp = tempfile.mkstemp(prefix=".status-", dir=folder)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(data, handle, indent=2)
    os.replace(temp, path)


def default_node() -> str:
    name = re.sub(r"[^a-z0-9-]", "-", socket.gethostname().split(".")[0].lower()).strip("-")
    return name or "minikubelet"


def pod_summary(pod: PodSpec) -> Dict[str, Any]:
    def container(spec: ContainerSpec) -> Dict[str, Any]:
        out: Dict[str, Any] = {"name": spec.name, "image": spec.image, "argv": spec.argv()}
        if spec.env:
            out["env"] = dict(spec.env)
        if spec.probes:
            out["probes"] = {kind: probe.describe() for kind, probe in spec.probes.items()}
        if spec.pre_stop:
            out["preStop"] = list(spec.pre_stop)
        return out

    return {
        "key": pod.key,
        "name": pod.full_name,
        "namespace": pod.namespace,
        "source": pod.source,
        "configHash": pod.config_hash,
        "restartPolicy": pod.restart_policy,
        "terminationGracePeriodSeconds": int(pod.grace_period),
        "initContainers": [container(c) for c in pod.init_containers],
        "containers": [container(c) for c in pod.containers],
    }


def render_summary(pod: PodSpec) -> str:
    lines = [f"{pod.key}  from {pod.source}  hash {pod.config_hash[:12]}  restartPolicy {pod.restart_policy}  grace {seconds_text(pod.grace_period)}"]
    for spec, role in [(c, "init") for c in pod.init_containers] + [(c, "app") for c in pod.containers]:
        lines.append(f"  {role:<4} {spec.name:<14} {spec.image:<24} {' '.join(spec.argv()) or '(image entrypoint)'}")
        for kind in ("startup", "liveness", "readiness"):
            if kind in spec.probes:
                lines.append(f"       {kind.capitalize() + ':':<11} {spec.probes[kind].describe()}")
        if spec.pre_stop:
            lines.append(f"       {'PreStop:':<11} exec [{' '.join(spec.pre_stop)}]")
    return "\n".join(lines)


def cmd_validate(args: argparse.Namespace) -> int:
    pods: List[PodSpec] = []
    failures: List[str] = []
    for path in args.paths:
        if os.path.isdir(path):
            found, errors = load_manifests(path, args.node)
            pods.extend(found.values())
            failures.extend(errors.values())
        else:
            try:
                pods.append(load_manifest(path, args.node))
            except ManifestError as exc:
                failures.append(str(exc))
    for failure in failures:
        print(f"minikubelet: {failure}", file=sys.stderr)
    if args.json:
        print(json.dumps([pod_summary(pod) for pod in pods], indent=2))
    else:
        print("\n".join(render_summary(pod) for pod in pods) or "no pods")
    return 2 if failures else 0


def cmd_get(args: argparse.Namespace) -> int:
    try:
        pods = load_pod_objects(args.file)
    except (OSError, ValueError) as exc:
        print(f"minikubelet: {exc}", file=sys.stderr)
        return 2
    rows = table_rows(pods, time.time())
    print(json.dumps(rows, indent=2) if args.json else render_table(rows))
    return 0


def cmd_backoff(args: argparse.Namespace) -> int:
    waits = backoff_schedule(args.base, args.max, args.restarts)
    if args.json:
        print(json.dumps(waits))
        return 0
    elapsed = 0.0
    lines = ["RESTART   WAIT     SINCE FIRST CRASH"]
    for index, wait in enumerate(waits, start=1):
        elapsed += wait
        lines.append(f"{index:<9} {seconds_text(wait):<8} {seconds_text(round(elapsed, 3))}")
    print("\n".join(lines))
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    if not os.path.isdir(args.manifests):
        print(f"minikubelet: manifests directory {args.manifests} not found", file=sys.stderr)
        return 2
    if args.runtime == "docker":
        check = subprocess_runner(["docker", "version", "--format", "{{.Server.Version}}"], timeout=20)
        if check.returncode != 0:
            print(f"minikubelet: docker is not reachable: {check.stderr.strip() or check.returncode}", file=sys.stderr)
            return 1
        runtime: Any = DockerRuntime()
    else:
        runtime = ProcessRuntime(args.work or tempfile.mkdtemp(prefix="minikubelet-"))
    start = time.time()
    log = EventLog(start, None if args.quiet else sys.stdout, args.events)
    kubelet = Kubelet(runtime, args.node, args.backoff_base, args.backoff_max, emit=log, min_grace=args.min_grace)
    stop = {"requested": False}

    def request_stop(signum: int, frame: Any) -> None:
        stop["requested"] = True

    previous = {sig: signal.signal(sig, request_stop) for sig in (signal.SIGTERM, signal.SIGINT)}
    reported: Dict[str, str] = {}
    deadline = start + args.duration if args.duration > 0 else None
    snapshot = pod_list(kubelet)
    try:
        while not stop["requested"] and (deadline is None or time.time() < deadline):
            desired, errors = load_manifests(args.manifests, args.node)
            for name, message in errors.items():
                if reported.get(name) != message:
                    print(f"minikubelet: {message}", file=sys.stderr, flush=True)
            reported = dict(errors)
            kubelet.sync(desired)
            snapshot = pod_list(kubelet)
            if args.status_file:
                write_json_atomic(args.status_file, snapshot)
            time.sleep(args.sync)
        kubelet.shutdown()
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)
    if args.status_file:
        write_json_atomic(args.status_file, pod_list(kubelet))
    if args.json:
        print(json.dumps(snapshot, indent=2))
    elif not args.quiet:
        print(render_table(table_rows(snapshot["items"], time.time())))
    return 0


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="minikubelet", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run", help="sync pods from a manifests directory until --duration expires or SIGTERM")
    run.add_argument("--manifests", required=True, help="directory of Pod manifests (.json, .yaml, .yml), like the kubelet's staticPodPath")
    run.add_argument("--runtime", choices=("process", "docker"), default="process", help="process: plain child processes; docker: a pause sandbox plus one container per container")
    run.add_argument("--node", default=default_node(), help="node name appended to pod names, like mirror pods")
    run.add_argument("--sync", type=float, default=1.0, help="seconds between sync loop iterations (default 1)")
    run.add_argument("--duration", type=float, default=0.0, help="stop after this many seconds (default: until SIGTERM or Ctrl-C)")
    run.add_argument("--backoff-base", type=float, default=10.0, help="first CrashLoopBackOff delay in seconds (kubelet: 10)")
    run.add_argument("--backoff-max", type=float, default=300.0, help="CrashLoopBackOff cap in seconds (kubelet: 300)")
    run.add_argument("--min-grace", type=float, default=2.0, help="shortest SIGTERM-to-SIGKILL window (kubelet: 2)")
    run.add_argument("--status-file", help="write the pod list as JSON here after every sync")
    run.add_argument("--events", help="append every event as one JSON line to this file")
    run.add_argument("--work", help="process runtime: directory for sandboxes and container logs (default: a new temp dir)")
    run.add_argument("--json", action="store_true", help="print the last pod list as JSON on exit")
    run.add_argument("--quiet", action="store_true", help="do not print events")
    validate = sub.add_parser("validate", help="parse manifests and print the pods minikubelet would run")
    validate.add_argument("paths", nargs="+", help="manifest files or directories")
    validate.add_argument("--node", default=default_node())
    validate.add_argument("--json", action="store_true")
    get = sub.add_parser("get", help="print a kubectl get pods table from a status file or from kubectl get pods -o json")
    get.add_argument("resource", choices=("pods", "pod", "po"))
    get.add_argument("-f", "--file", required=True)
    get.add_argument("--json", action="store_true")
    backoff = sub.add_parser("backoff", help="print the CrashLoopBackOff restart schedule")
    backoff.add_argument("--base", type=float, default=10.0)
    backoff.add_argument("--max", type=float, default=300.0)
    backoff.add_argument("--restarts", type=int, default=9)
    backoff.add_argument("--json", action="store_true")
    args = parser.parse_args(list(sys.argv[1:] if argv is None else argv))
    if args.command == "validate":
        return cmd_validate(args)
    if args.command == "get":
        return cmd_get(args)
    if args.command == "backoff":
        return cmd_backoff(args)
    return cmd_run(args)


if __name__ == "__main__":
    raise SystemExit(main())
