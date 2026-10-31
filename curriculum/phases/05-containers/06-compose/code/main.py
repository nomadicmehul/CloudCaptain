"""minicompose: parse a compose file with a hand-written YAML subset parser, validate it, order the services as a dependency graph and run them as docker run commands, waiting for health before dependents."""
from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import subprocess
import sys
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

CONDITIONS = ("service_started", "service_healthy", "service_completed_successfully")
RESTART_POLICIES = ("no", "always", "on-failure", "unless-stopped")
KNOWN_SERVICE_KEYS = {"image", "command", "ports", "environment", "volumes", "depends_on", "healthcheck", "networks", "restart"}
LABEL_PROJECT = "com.docker.compose.project"
LABEL_SERVICE = "com.docker.compose.service"
LABEL_NETWORK = "com.docker.compose.network"
LABEL_VOLUME = "com.docker.compose.volume"
INT_RE = re.compile(r"^-?(0|[1-9][0-9]*)$")
FLOAT_RE = re.compile(r"^-?[0-9]+\.[0-9]+$")
DURATION_RE = re.compile(r"(\d+(?:\.\d+)?)(ms|s|m|h)")
DURATION_UNITS = {"ms": 0.001, "s": 1.0, "m": 60.0, "h": 3600.0}
ESCAPES = {"n": "\n", "t": "\t", "r": "\r", '"': '"', "\\": "\\", "/": "/", "0": "\0"}


class YamlError(ValueError):
    def __init__(self, line: int, message: str) -> None:
        super().__init__(f"line {line}: {message}")
        self.line = line


class ComposeError(ValueError):
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
        raise YamlError(number, "block scalars are not supported; use a quoted string")
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


def parse_duration(value: Any, where: str) -> float:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    text = str(value).strip()
    if not text or not re.fullmatch(r"(\d+(?:\.\d+)?(ms|s|m|h))+", text):
        raise ComposeError(f"{where}: cannot parse duration {value!r}; use forms like 500ms, 10s, 1m30s")
    return sum(float(amount) * DURATION_UNITS[unit] for amount, unit in DURATION_RE.findall(text))


def format_duration(seconds: float) -> str:
    if seconds == int(seconds):
        return f"{int(seconds)}s"
    return f"{int(round(seconds * 1000))}ms"


@dataclass
class HealthCheck:
    test: List[str]
    interval: float = 30.0
    timeout: float = 30.0
    retries: int = 3
    start_period: float = 0.0

    def command(self) -> Optional[str]:
        if not self.test or self.test[0] == "NONE":
            return None
        if self.test[0] == "CMD-SHELL":
            return " ".join(self.test[1:])
        if self.test[0] == "CMD":
            return shlex.join(self.test[1:])
        return shlex.join(self.test)


@dataclass
class Service:
    name: str
    image: str
    command: Optional[List[str]] = None
    ports: List[str] = field(default_factory=list)
    environment: Dict[str, Optional[str]] = field(default_factory=dict)
    volumes: List[str] = field(default_factory=list)
    depends_on: Dict[str, str] = field(default_factory=dict)
    healthcheck: Optional[HealthCheck] = None
    networks: List[str] = field(default_factory=lambda: ["default"])
    restart: str = "no"
    ignored_keys: List[str] = field(default_factory=list)


@dataclass
class Project:
    name: str
    services: Dict[str, Service]
    networks: List[str]
    volumes: List[str]
    base_dir: str = "."

    def container_name(self, service: str) -> str:
        return f"{self.name}-{service}-1"

    def network_name(self, network: str) -> str:
        return f"{self.name}_{network}"

    def volume_name(self, volume: str) -> str:
        return f"{self.name}_{volume}"


def as_list(value: Any, where: str) -> List[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return shlex.split(value)
    if isinstance(value, list):
        return [str(item) for item in value]
    raise ComposeError(f"{where}: expected a string or a list, got {type(value).__name__}")


def parse_environment(value: Any, where: str) -> Dict[str, Optional[str]]:
    env: Dict[str, Optional[str]] = {}
    if value is None:
        return env
    if isinstance(value, dict):
        for key, item in value.items():
            env[str(key)] = None if item is None else (str(item).lower() if isinstance(item, bool) else str(item))
        return env
    if isinstance(value, list):
        for item in value:
            key, sep, item_value = str(item).partition("=")
            env[key] = item_value if sep else None
        return env
    raise ComposeError(f"{where}: environment must be a mapping (KEY: value) or a list (- KEY=value)")


def parse_depends_on(value: Any, where: str) -> Dict[str, str]:
    deps: Dict[str, str] = {}
    if value is None:
        return deps
    if isinstance(value, list):
        for item in value:
            deps[str(item)] = "service_started"
        return deps
    if isinstance(value, dict):
        for name, spec in value.items():
            condition = "service_started"
            if isinstance(spec, dict):
                condition = str(spec.get("condition", condition))
            elif spec is not None:
                raise ComposeError(f"{where}.depends_on.{name}: expected a mapping with condition:")
            if condition not in CONDITIONS:
                raise ComposeError(f"{where}.depends_on.{name}: condition must be one of {', '.join(CONDITIONS)}, got {condition!r}")
            deps[str(name)] = condition
        return deps
    raise ComposeError(f"{where}: depends_on must be a list of names or a mapping of name: {{condition: ...}}")


def parse_healthcheck(value: Any, where: str) -> Optional[HealthCheck]:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ComposeError(f"{where}.healthcheck: expected a mapping with test:")
    if value.get("disable") is True:
        return HealthCheck(["NONE"])
    test = value.get("test")
    if isinstance(test, str):
        test = ["CMD-SHELL", test]
    if not isinstance(test, list) or not test:
        raise ComposeError(f"{where}.healthcheck.test: required; a string (shell) or a list starting with CMD or CMD-SHELL")
    test = [str(item) for item in test]
    if test[0] not in ("CMD", "CMD-SHELL", "NONE"):
        raise ComposeError(f"{where}.healthcheck.test: list form must start with CMD or CMD-SHELL, got {test[0]!r}")
    retries = value.get("retries", 3)
    if not isinstance(retries, int) or isinstance(retries, bool) or retries < 1:
        raise ComposeError(f"{where}.healthcheck.retries: must be a positive integer")
    return HealthCheck(
        test,
        parse_duration(value.get("interval", "30s"), f"{where}.healthcheck.interval"),
        parse_duration(value.get("timeout", "30s"), f"{where}.healthcheck.timeout"),
        retries,
        parse_duration(value.get("start_period", "0s"), f"{where}.healthcheck.start_period"),
    )


def parse_networks(value: Any, where: str) -> List[str]:
    if value is None:
        return ["default"]
    if isinstance(value, list):
        return [str(item) for item in value] or ["default"]
    if isinstance(value, dict):
        return [str(key) for key in value] or ["default"]
    raise ComposeError(f"{where}: networks must be a list or a mapping")


def parse_service(name: str, raw: Any) -> Service:
    where = f"services.{name}"
    if not isinstance(raw, dict):
        raise ComposeError(f"{where}: expected a mapping of keys such as image: and ports:")
    if "build" in raw and "image" not in raw:
        raise ComposeError(f"{where}: build: is not supported; run docker build -t NAME . first and set image: NAME")
    image = raw.get("image")
    if not isinstance(image, str) or not image:
        raise ComposeError(f"{where}: image: is required")
    restart = str(raw.get("restart", "no")).lower() if raw.get("restart") is not None else "no"
    if restart not in RESTART_POLICIES:
        raise ComposeError(f"{where}.restart: must be one of {', '.join(RESTART_POLICIES)}, got {restart!r}")
    ports = raw.get("ports")
    if ports is not None and not isinstance(ports, list):
        raise ComposeError(f"{where}.ports: must be a list such as - \"8080:80\" (quote it: YAML reads 8080:80 as a number in some parsers)")
    volumes = raw.get("volumes")
    if volumes is not None and not isinstance(volumes, list):
        raise ComposeError(f"{where}.volumes: must be a list of SOURCE:TARGET entries")
    command = raw.get("command")
    return Service(
        name=name,
        image=image,
        command=as_list(command, f"{where}.command") if command is not None else None,
        ports=[str(item) for item in ports or []],
        environment=parse_environment(raw.get("environment"), where),
        volumes=[str(item) for item in volumes or []],
        depends_on=parse_depends_on(raw.get("depends_on"), where),
        healthcheck=parse_healthcheck(raw.get("healthcheck"), where),
        networks=parse_networks(raw.get("networks"), f"{where}.networks"),
        restart=restart,
        ignored_keys=sorted(key for key in raw if key not in KNOWN_SERVICE_KEYS),
    )


def load_project(data: Any, name: Optional[str] = None, base_dir: str = ".") -> Project:
    if not isinstance(data, dict):
        raise ComposeError("the compose file must be a mapping with a services: key")
    raw_services = data.get("services")
    if not isinstance(raw_services, dict) or not raw_services:
        raise ComposeError("services: is required and must list at least one service")
    project_name = name or str(data.get("name") or os.path.basename(os.path.abspath(base_dir)))
    project_name = re.sub(r"[^a-z0-9_-]", "", project_name.lower())
    if not project_name or not project_name[0].isalnum():
        raise ComposeError("project name must start with a letter or digit and contain only lowercase letters, digits, - and _")
    services = {str(key): parse_service(str(key), raw) for key, raw in raw_services.items()}
    declared_networks = data.get("networks") or {}
    declared_volumes = data.get("volumes") or {}
    if not isinstance(declared_networks, dict) or not isinstance(declared_volumes, dict):
        raise ComposeError("top-level networks: and volumes: must be mappings")
    networks = sorted({net for service in services.values() for net in service.networks} | set(declared_networks))
    for service in services.values():
        for net in service.networks:
            if net != "default" and net not in declared_networks:
                raise ComposeError(f"services.{service.name}.networks: {net!r} is not declared under top-level networks:")
        for dep, condition in service.depends_on.items():
            if dep not in services:
                raise ComposeError(f"services.{service.name}.depends_on: {dep!r} is not a service in this file (known: {', '.join(sorted(services))})")
            if dep == service.name:
                raise ComposeError(f"services.{service.name}.depends_on: a service cannot depend on itself")
            if condition == "service_healthy" and (services[dep].healthcheck is None or services[dep].healthcheck.command() is None):
                raise ComposeError(f"services.{service.name}.depends_on.{dep}: condition service_healthy needs a healthcheck: on {dep} (image HEALTHCHECKs are not visible to minicompose)")
        for volume in service.volumes:
            source = volume.split(":", 1)[0]
            if source and not source.startswith((".", "/", "~")) and source not in declared_volumes:
                raise ComposeError(f"services.{service.name}.volumes: {source!r} is neither a path nor a top-level volume")
    return Project(project_name, services, networks, sorted(declared_volumes), base_dir)


def load_file(path: str, name: Optional[str] = None) -> Project:
    try:
        with open(path, encoding="utf-8") as handle:
            source = handle.read()
    except OSError as exc:
        raise ComposeError(f"cannot read {path}: {exc.strerror}")
    try:
        data = parse_yaml(source)
    except YamlError as exc:
        raise ComposeError(f"{path}:{exc.line}: {str(exc).split(': ', 1)[1]}")
    return load_project(data, name, os.path.dirname(os.path.abspath(path)))


def start_order(project: Project) -> List[str]:
    remaining = {name: set(service.depends_on) for name, service in project.services.items()}
    order: List[str] = []
    while remaining:
        ready = sorted(name for name, deps in remaining.items() if not deps)
        if not ready:
            raise ComposeError(f"dependency cycle: {describe_cycle(remaining)}")
        for name in ready:
            order.append(name)
            del remaining[name]
        for deps in remaining.values():
            deps.difference_update(ready)
    return order


def describe_cycle(remaining: Dict[str, set]) -> str:
    start = sorted(remaining)[0]
    path = [start]
    current = start
    while True:
        current = sorted(remaining[current])[0]
        if current in path:
            path = path[path.index(current) :]
            return " -> ".join(path + [current])
        path.append(current)


def stop_order(project: Project) -> List[str]:
    return list(reversed(start_order(project)))


def network_create_args(project: Project, network: str) -> List[str]:
    return ["docker", "network", "create", "--label", f"{LABEL_PROJECT}={project.name}", "--label", f"{LABEL_NETWORK}={network}", project.network_name(network)]


def volume_create_args(project: Project, volume: str) -> List[str]:
    return ["docker", "volume", "create", "--label", f"{LABEL_PROJECT}={project.name}", "--label", f"{LABEL_VOLUME}={volume}", project.volume_name(volume)]


def volume_arg(project: Project, spec: str) -> str:
    source, sep, rest = spec.partition(":")
    if not sep:
        return spec
    if source.startswith("~"):
        source = os.path.expanduser(source)
    if source.startswith("."):
        source = os.path.normpath(os.path.join(project.base_dir, source))
    elif not source.startswith("/"):
        source = project.volume_name(source)
    return f"{source}:{rest}"


def run_args(project: Project, service: Service) -> List[str]:
    args = ["docker", "run", "-d", "--name", project.container_name(service.name)]
    args += ["--label", f"{LABEL_PROJECT}={project.name}", "--label", f"{LABEL_SERVICE}={service.name}"]
    args += ["--network", project.network_name(service.networks[0]), "--network-alias", service.name]
    if service.restart != "no":
        args += ["--restart", service.restart]
    for port in service.ports:
        args += ["-p", port]
    for key, value in service.environment.items():
        args += ["-e", key if value is None else f"{key}={value}"]
    for volume in service.volumes:
        args += ["-v", volume_arg(project, volume)]
    if service.healthcheck is not None:
        command = service.healthcheck.command()
        if command is None:
            args += ["--no-healthcheck"]
        else:
            check = service.healthcheck
            args += ["--health-cmd", command, "--health-interval", format_duration(check.interval), "--health-timeout", format_duration(check.timeout), "--health-retries", str(check.retries)]
            if check.start_period:
                args += ["--health-start-period", format_duration(check.start_period)]
    args.append(service.image)
    if service.command:
        args += service.command
    return args


def connect_args(project: Project, service: Service) -> List[List[str]]:
    return [["docker", "network", "connect", "--alias", service.name, project.network_name(net), project.container_name(service.name)] for net in service.networks[1:]]


def render_plan(project: Project) -> List[List[str]]:
    plan = [network_create_args(project, net) for net in project.networks]
    plan += [volume_create_args(project, vol) for vol in project.volumes]
    for name in start_order(project):
        service = project.services[name]
        plan.append(run_args(project, service))
        plan.extend(connect_args(project, service))
    return plan


def normalised(project: Project) -> Dict[str, Any]:
    services: Dict[str, Any] = {}
    for name in sorted(project.services):
        service = project.services[name]
        entry: Dict[str, Any] = {"image": service.image, "networks": list(service.networks), "restart": service.restart}
        if service.command is not None:
            entry["command"] = list(service.command)
        if service.ports:
            entry["ports"] = list(service.ports)
        if service.environment:
            entry["environment"] = dict(service.environment)
        if service.volumes:
            entry["volumes"] = [volume_arg(project, volume) for volume in service.volumes]
        if service.depends_on:
            entry["depends_on"] = {dep: {"condition": condition} for dep, condition in sorted(service.depends_on.items())}
        if service.healthcheck is not None:
            check = service.healthcheck
            entry["healthcheck"] = {"test": list(check.test), "interval": format_duration(check.interval), "timeout": format_duration(check.timeout), "retries": check.retries, "start_period": format_duration(check.start_period)}
        services[name] = entry
    return {
        "name": project.name,
        "services": services,
        "networks": {net: {"name": project.network_name(net)} for net in project.networks},
        "volumes": {vol: {"name": project.volume_name(vol)} for vol in project.volumes},
    }


@dataclass
class CommandResult:
    returncode: int
    stdout: str = ""
    stderr: str = ""

    @property
    def ok(self) -> bool:
        return self.returncode == 0


Runner = Callable[[Sequence[str]], CommandResult]


def subprocess_runner(args: Sequence[str]) -> CommandResult:
    try:
        completed = subprocess.run(list(args), capture_output=True, text=True, check=False)
    except FileNotFoundError:
        return CommandResult(127, "", f"{args[0]}: command not found")
    return CommandResult(completed.returncode, completed.stdout, completed.stderr)


def shell_line(args: Sequence[str]) -> str:
    return " ".join(shlex.quote(arg) for arg in args)


@dataclass
class ContainerState:
    name: str
    service: str
    image: str
    state: str
    status: str
    health: str


class Engine:
    def __init__(
        self,
        project: Project,
        runner: Runner = subprocess_runner,
        now: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        log: Callable[[str], None] = lambda line: None,
        poll_interval: float = 1.0,
    ) -> None:
        self.project = project
        self.runner = runner
        self.now = now
        self.sleep = sleep
        self.log = log
        self.poll_interval = poll_interval
        self.executed: List[List[str]] = []

    def run(self, args: Sequence[str], ignore_missing: bool = False) -> CommandResult:
        self.executed.append(list(args))
        result = self.runner(args)
        if not result.ok and not (ignore_missing and "No such" in result.stderr):
            raise ComposeError(f"command failed ({result.returncode}): {shell_line(args)}\n{result.stderr.strip()}")
        return result

    def exists(self, kind: str, name: str) -> bool:
        args = ["docker", kind, "inspect", name] if kind != "container" else ["docker", "inspect", "--type", "container", name]
        self.executed.append(list(args))
        return self.runner(args).ok

    def ensure_networks(self) -> List[str]:
        created = []
        for net in self.project.networks:
            if not self.exists("network", self.project.network_name(net)):
                self.run(network_create_args(self.project, net))
                created.append(net)
                self.log(f"network {self.project.network_name(net)} created")
        for vol in self.project.volumes:
            if not self.exists("volume", self.project.volume_name(vol)):
                self.run(volume_create_args(self.project, vol))
                self.log(f"volume {self.project.volume_name(vol)} created")
        return created

    def inspect_field(self, container: str, template: str) -> str:
        return self.run(["docker", "inspect", "-f", template, container]).stdout.strip()

    def wait_for(self, dependency: str, condition: str, timeout: float) -> float:
        container = self.project.container_name(dependency)
        started = self.now()
        while True:
            if condition == "service_started":
                state = self.inspect_field(container, "{{.State.Status}}")
                if state == "running":
                    return self.now() - started
                if state in ("exited", "dead"):
                    raise ComposeError(f"dependency {dependency} exited before {container} was running; see: docker logs {container}")
            elif condition == "service_healthy":
                health = self.inspect_field(container, "{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}")
                if health == "healthy":
                    return self.now() - started
                if health == "unhealthy":
                    raise ComposeError(f"dependency {dependency} is unhealthy; see: docker inspect -f '{{{{json .State.Health.Log}}}}' {container}")
                if health == "none":
                    raise ComposeError(f"dependency {dependency} has no healthcheck; add healthcheck: or use condition: service_started")
            else:
                state = self.inspect_field(container, "{{.State.Status}} {{.State.ExitCode}}")
                status, _, code = state.partition(" ")
                if status == "exited":
                    if code == "0":
                        return self.now() - started
                    raise ComposeError(f"dependency {dependency} exited with code {code}; see: docker logs {container}")
            if self.now() - started >= timeout:
                raise ComposeError(f"timed out after {int(timeout)}s waiting for {dependency} to be {condition.replace('service_', '')}")
            self.sleep(self.poll_interval)

    def up(self, timeout: float = 60.0) -> List[str]:
        order = start_order(self.project)
        self.ensure_networks()
        started: List[str] = []
        for name in order:
            service = self.project.services[name]
            for dep, condition in service.depends_on.items():
                waited = self.wait_for(dep, condition, timeout)
                self.log(f"{name}: {dep} is {condition.replace('service_', '')} after {waited:.1f}s")
            container = self.project.container_name(name)
            if self.exists("container", container):
                self.run(["docker", "rm", "-f", container])
                self.log(f"{container} recreated")
            self.run(run_args(self.project, service))
            for args in connect_args(self.project, service):
                self.run(args)
            started.append(name)
            self.log(f"{container} started")
        return started

    def ps(self) -> List[ContainerState]:
        template = "{{.Names}}\t{{.Label \"com.docker.compose.service\"}}\t{{.Image}}\t{{.State}}\t{{.Status}}"
        result = self.run(["docker", "ps", "-a", "--filter", f"label={LABEL_PROJECT}={self.project.name}", "--format", template])
        rows: List[ContainerState] = []
        for line in result.stdout.splitlines():
            parts = line.split("\t")
            if len(parts) < 5:
                continue
            name, service, image, state, status = parts[:5]
            health = "healthy" if "(healthy)" in status else "unhealthy" if "(unhealthy)" in status else "starting" if "(health: starting)" in status else "-"
            rows.append(ContainerState(name, service, image, state, status, health))
        return sorted(rows, key=lambda row: row.name)

    def down(self, volumes: bool = False) -> List[str]:
        removed: List[str] = []
        for name in stop_order(self.project):
            container = self.project.container_name(name)
            self.run(["docker", "stop", container], ignore_missing=True)
            result = self.run(["docker", "rm", container], ignore_missing=True)
            if result.ok:
                removed.append(container)
                self.log(f"{container} removed")
        for net in self.project.networks:
            self.run(["docker", "network", "rm", self.project.network_name(net)], ignore_missing=True)
            self.log(f"network {self.project.network_name(net)} removed")
        if volumes:
            for vol in self.project.volumes:
                self.run(["docker", "volume", "rm", self.project.volume_name(vol)], ignore_missing=True)
                self.log(f"volume {self.project.volume_name(vol)} removed")
        return removed


def render_ps(rows: Sequence[ContainerState]) -> str:
    if not rows:
        return "no containers"
    width = max(len(row.name) for row in rows)
    lines = [f"{'NAME':<{width}}  {'SERVICE':<10} {'STATE':<9} {'HEALTH':<9} STATUS"]
    for row in rows:
        lines.append(f"{row.name:<{width}}  {row.service:<10} {row.state:<9} {row.health:<9} {row.status}")
    return "\n".join(lines)


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="minicompose", description=__doc__)
    parser.add_argument("-f", "--file", default="compose.yaml", help="compose file (default compose.yaml)")
    parser.add_argument("-p", "--project-name", help="project name (default: name: in the file, else the directory name)")
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    parser.add_argument("--verbose", action="store_true", help="log every step to stderr")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("config", help="validate and print the normalised model")
    sub.add_parser("order", help="print the start order")
    sub.add_parser("render", help="print the docker commands up would run")
    up = sub.add_parser("up", help="create networks and start services in dependency order")
    up.add_argument("--timeout", type=float, default=60.0, help="seconds to wait for each dependency condition")
    up.add_argument("--dry-run", action="store_true", help="print the commands instead of running them")
    sub.add_parser("ps", help="list this project's containers")
    down = sub.add_parser("down", help="stop and remove containers in reverse order, then networks")
    down.add_argument("-v", "--volumes", action="store_true", help="also remove named volumes")
    args = parser.parse_args(list(sys.argv[1:] if argv is None else argv))

    log = (lambda line: print(f"[minicompose] {line}", file=sys.stderr, flush=True)) if args.verbose else (lambda line: None)
    try:
        project = load_file(args.file, args.project_name)
        for service in project.services.values():
            if service.ignored_keys:
                print(f"minicompose: services.{service.name}: ignoring keys {', '.join(service.ignored_keys)}", file=sys.stderr)
        if args.command == "config":
            print(json.dumps(normalised(project), indent=2))
            return 0
        if args.command == "order":
            order = start_order(project)
            print(json.dumps(order) if args.json else "\n".join(f"{index}. {name}" for index, name in enumerate(order, start=1)))
            return 0
        if args.command == "render" or (args.command == "up" and args.dry_run):
            plan = render_plan(project)
            print(json.dumps(plan) if args.json else "\n".join(shell_line(step) for step in plan))
            return 0
    except ComposeError as exc:
        print(f"minicompose: {exc}", file=sys.stderr)
        return 2

    engine = Engine(project, log=log)
    try:
        if args.command == "up":
            started = engine.up(args.timeout)
            print(json.dumps(started) if args.json else "\n".join(f"started {project.container_name(name)}" for name in started))
        elif args.command == "ps":
            rows = engine.ps()
            print(json.dumps([row.__dict__ for row in rows], indent=2) if args.json else render_ps(rows))
        elif args.command == "down":
            removed = engine.down(args.volumes)
            print(json.dumps(removed) if args.json else "\n".join(f"removed {name}" for name in removed) or "nothing to remove")
    except ComposeError as exc:
        print(f"minicompose: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
