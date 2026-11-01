"""agentbox: an egress proxy with a host allow-list and a JSON audit, a renderer that turns a small sandbox policy into exact docker arguments, and a drift check that compares a running container with that policy."""
from __future__ import annotations

import argparse
import json
import os
import re
import selectors
import shlex
import signal
import socket
import subprocess
import sys
import threading
import time
from dataclasses import asdict, dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple
from urllib.parse import urlsplit

DEFAULT_LISTEN = "127.0.0.1:3128"
CONNECT_TIMEOUT = 10.0
IDLE_TIMEOUT = 120.0
CHUNK = 65536
HOP_BY_HOP = {"connection", "keep-alive", "proxy-connection", "proxy-authorization", "proxy-authenticate", "te", "trailer", "upgrade"}
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")
MEMORY_UNITS = {"": 1, "b": 1, "k": 1024, "m": 1024 ** 2, "g": 1024 ** 3}
SENSITIVE_SOURCES = ("/var/run/docker.sock", "/run/docker.sock", "/run/containerd", "/var/run/containerd", "/var/lib/docker", "/proc", "/sys", "/dev", "/etc", "/root", "/boot")
CREDENTIAL_DIRS = (".aws", ".config/gcloud", ".azure", ".ssh", ".docker", ".kube")
NO_PROXY = "localhost,127.0.0.1"

Runner = Callable[[Sequence[str]], Tuple[int, str, str]]


@dataclass(frozen=True)
class HostRule:
    text: str
    pattern: str
    port: Optional[int]

    @classmethod
    def parse(cls, text: str) -> "HostRule":
        cleaned = text.strip().lower().rstrip(".")
        if not cleaned:
            raise ValueError("empty host rule")
        host, sep, port = cleaned.rpartition(":")
        if sep and host and port.isdigit():
            return cls(cleaned, host, int(port))
        if sep and not port.isdigit():
            raise ValueError(f"host rule {text!r}: port must be a number")
        return cls(cleaned, cleaned, None)

    def matches(self, host: str, port: int) -> bool:
        if self.port is not None and self.port != port:
            return False
        if self.pattern == "*":
            return True
        if self.pattern.startswith("*."):
            suffix = self.pattern[1:]
            return host.endswith(suffix) and host != suffix[1:]
        return host == self.pattern


@dataclass
class Decision:
    allowed: bool
    reason: str


class EgressPolicy:
    def __init__(self, rules: Sequence[str]) -> None:
        self.rules = [HostRule.parse(rule) for rule in rules]

    @property
    def texts(self) -> List[str]:
        return [rule.text for rule in self.rules]

    def decide(self, host: str, port: int) -> Decision:
        host = host.strip().lower().rstrip(".")
        for rule in self.rules:
            if rule.matches(host, port):
                return Decision(True, f"matches rule {rule.text}")
        return Decision(False, f"{host}:{port} is not in the allow-list")


def format_entry(entry: Dict[str, Any]) -> str:
    verdict = "ALLOW" if entry["allowed"] else "DENY "
    return f"{entry['method']:<7} {entry['host']}:{entry['port']:<5} {verdict} {entry['status']} out={entry['bytes_out']} in={entry['bytes_in']} {entry['duration_ms']}ms  {entry['reason']}"


class Audit:
    def __init__(self, rules: Sequence[str], path: Optional[str] = None, log: Optional[Callable[[str], None]] = None, clock: Callable[[], float] = time.time) -> None:
        self.rules = list(rules)
        self.path = path
        self.log = log
        self.clock = clock
        self.started_at = clock()
        self.requests: List[Dict[str, Any]] = []
        self.hosts: Dict[str, Dict[str, int]] = {}
        self.lock = threading.Lock()

    def record(self, method: str, host: str, port: int, decision: Decision, status: int, bytes_out: int, bytes_in: int, started: float) -> Dict[str, Any]:
        entry = {
            "at": round(self.clock(), 3),
            "method": method,
            "host": host,
            "port": port,
            "allowed": decision.allowed,
            "status": status,
            "reason": decision.reason,
            "bytes_out": bytes_out,
            "bytes_in": bytes_in,
            "duration_ms": int((time.monotonic() - started) * 1000),
        }
        with self.lock:
            self.requests.append(entry)
            stats = self.hosts.setdefault(f"{host}:{port}", {"allowed": 0, "denied": 0, "bytes_out": 0, "bytes_in": 0})
            stats["allowed" if decision.allowed else "denied"] += 1
            stats["bytes_out"] += bytes_out
            stats["bytes_in"] += bytes_in
            if self.path:
                self._write()
        if self.log:
            self.log(format_entry(entry))
        return entry

    def snapshot(self) -> Dict[str, Any]:
        with self.lock:
            return self._snapshot()

    def _snapshot(self) -> Dict[str, Any]:
        allowed = sum(1 for entry in self.requests if entry["allowed"])
        return {
            "started_at": round(self.started_at, 3),
            "allowed_hosts": list(self.rules),
            "summary": {"requests": len(self.requests), "allowed": allowed, "denied": len(self.requests) - allowed, "bytes_out": sum(e["bytes_out"] for e in self.requests)},
            "hosts": {key: dict(value) for key, value in self.hosts.items()},
            "requests": [dict(entry) for entry in self.requests],
        }

    def _write(self) -> None:
        assert self.path is not None
        tmp = f"{self.path}.tmp"
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(self._snapshot(), handle, indent=2)
        os.replace(tmp, self.path)

    def flush(self) -> None:
        with self.lock:
            if self.path:
                self._write()


def split_hostport(text: str, default_port: int) -> Tuple[str, int]:
    host, sep, port = text.rpartition(":")
    if sep and host and port.isdigit():
        return host, int(port)
    return text, default_port


def relay(client: socket.socket, upstream: socket.socket, idle_timeout: float = IDLE_TIMEOUT) -> Tuple[int, int]:
    bytes_out = bytes_in = 0
    with selectors.DefaultSelector() as selector:
        selector.register(client, selectors.EVENT_READ)
        selector.register(upstream, selectors.EVENT_READ)
        while True:
            events = selector.select(idle_timeout)
            if not events:
                return bytes_out, bytes_in
            for key, _ in events:
                source = key.fileobj
                target = upstream if source is client else client
                try:
                    chunk = source.recv(CHUNK)
                    if not chunk:
                        return bytes_out, bytes_in
                    target.sendall(chunk)
                except OSError:
                    return bytes_out, bytes_in
                if source is client:
                    bytes_out += len(chunk)
                else:
                    bytes_in += len(chunk)


def parse_status(head: bytes) -> int:
    line = head.split(b"\r\n", 1)[0].split()
    return int(line[1]) if len(line) >= 2 and line[1].isdigit() else 0


class ProxyHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "agentbox/1.0"

    def log_message(self, fmt: str, *args: Any) -> None:
        return None

    @property
    def policy(self) -> EgressPolicy:
        return self.server.policy  # type: ignore[attr-defined]

    @property
    def audit(self) -> Audit:
        return self.server.audit  # type: ignore[attr-defined]

    def do_CONNECT(self) -> None:
        started = time.monotonic()
        host, port = split_hostport(self.path, 443)
        decision = self.policy.decide(host, port)
        if not decision.allowed:
            self._refuse(403, "Egress denied", "CONNECT", host, port, decision, started)
            return
        try:
            upstream = socket.create_connection((host, port), timeout=CONNECT_TIMEOUT)
        except OSError as exc:
            self._refuse(502, "Bad gateway", "CONNECT", host, port, Decision(True, f"{decision.reason}; upstream unreachable: {exc}"), started)
            return
        upstream.settimeout(None)
        self.send_response(200, "Connection established")
        self.end_headers()
        with upstream:
            bytes_out, bytes_in = relay(self.connection, upstream)
        self.close_connection = True
        self.audit.record("CONNECT", host, port, decision, 200, bytes_out, bytes_in, started)

    def _forward(self) -> None:
        started = time.monotonic()
        parts = urlsplit(self.path)
        if parts.scheme != "http" or not parts.hostname:
            self._refuse(400, "Bad request", self.command, parts.hostname or "-", parts.port or 0, Decision(False, "a forward proxy needs an absolute http:// URL or CONNECT"), started)
            return
        host, port = parts.hostname, parts.port or 80
        decision = self.policy.decide(host, port)
        if not decision.allowed:
            self._refuse(403, "Egress denied", self.command, host, port, decision, started)
            return
        if "chunked" in (self.headers.get("Transfer-Encoding") or "").lower():
            self._refuse(411, "Length required", self.command, host, port, Decision(False, "chunked request bodies are not supported; send Content-Length"), started)
            return
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""
        request = self._rewrite_request(parts.path or "/", parts.query, parts.netloc, body)
        try:
            upstream = socket.create_connection((host, port), timeout=CONNECT_TIMEOUT)
        except OSError as exc:
            self._refuse(502, "Bad gateway", self.command, host, port, Decision(True, f"{decision.reason}; upstream unreachable: {exc}"), started)
            return
        status, bytes_in, head = 0, 0, b""
        with upstream:
            upstream.sendall(request)
            while True:
                try:
                    chunk = upstream.recv(CHUNK)
                except OSError:
                    break
                if not chunk:
                    break
                if status == 0:
                    head += chunk
                    if b"\r\n" in head:
                        status = parse_status(head)
                try:
                    self.wfile.write(chunk)
                except OSError:
                    break
                bytes_in += len(chunk)
        self.close_connection = True
        self.audit.record(self.command, host, port, decision, status, len(request), bytes_in, started)

    do_GET = do_POST = do_PUT = do_PATCH = do_DELETE = do_HEAD = do_OPTIONS = _forward

    def _rewrite_request(self, path: str, query: str, netloc: str, body: bytes) -> bytes:
        target = f"{path}?{query}" if query else path
        lines = [f"{self.command} {target} HTTP/1.1"]
        has_host = False
        for name, value in self.headers.items():
            if name.lower() in HOP_BY_HOP:
                continue
            has_host = has_host or name.lower() == "host"
            lines.append(f"{name}: {value}")
        if not has_host:
            lines.append(f"Host: {netloc}")
        lines.append("Connection: close")
        return ("\r\n".join(lines) + "\r\n\r\n").encode("latin-1") + body

    def _refuse(self, status: int, text: str, method: str, host: str, port: int, decision: Decision, started: float) -> None:
        payload = json.dumps({"error": text.lower(), "host": host, "port": port, "reason": decision.reason}).encode("utf-8")
        self.send_response(status, text)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("X-Egress-Decision", "denied" if status == 403 else "error")
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(payload)
        self.close_connection = True
        self.audit.record(method, host, port, Decision(False, decision.reason), status, 0, 0, started)


class ProxyServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address: Tuple[str, int], policy: EgressPolicy, audit: Audit) -> None:
        super().__init__(address, ProxyHandler)
        self.policy = policy
        self.audit = audit


def start_proxy(policy: EgressPolicy, audit: Audit, host: str = "127.0.0.1", port: int = 0) -> ProxyServer:
    server = ProxyServer((host, port), policy, audit)
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
    thread.start()
    return server


@dataclass
class Policy:
    name: str
    image: str
    allowed_hosts: List[str] = field(default_factory=list)
    kubeconfig: Optional[str] = None
    kubeconfig_target: str = "/home/agent/.kube/config"
    tmpfs: List[str] = field(default_factory=lambda: ["/tmp"])
    pids: int = 64
    memory: str = "256m"
    cpus: str = "1"
    user: str = "1000"
    env: Dict[str, str] = field(default_factory=dict)
    proxy_image: str = "python:3.12-alpine"
    proxy_port: int = 3128

    @property
    def network(self) -> str:
        return f"{self.name}-net"

    @property
    def egress_network(self) -> str:
        return f"{self.name}-egress"

    @property
    def proxy_container(self) -> str:
        return f"{self.name}-proxy"

    @property
    def agent_container(self) -> str:
        return f"{self.name}-agent"

    @property
    def proxy_url(self) -> str:
        return f"http://{self.proxy_container}:{self.proxy_port}"


def load_policy(data: Dict[str, Any]) -> Policy:
    name = str(data.get("name", "")).strip()
    if not NAME_RE.match(name):
        raise ValueError("policy: name must be lowercase letters, digits and dashes (it prefixes the network and container names)")
    image = str(data.get("image", "")).strip()
    if not image:
        raise ValueError("policy: image is required")
    hosts = data.get("allowed_hosts", [])
    if not isinstance(hosts, list) or not all(isinstance(host, str) for host in hosts):
        raise ValueError("policy: allowed_hosts must be a list of host or host:port strings")
    EgressPolicy(hosts)
    limits = data.get("limits") or {}
    policy = Policy(
        name=name,
        image=image,
        allowed_hosts=list(hosts),
        kubeconfig=os.path.expanduser(data["kubeconfig"]) if data.get("kubeconfig") else None,
        kubeconfig_target=str(data.get("kubeconfig_target", "/home/agent/.kube/config")),
        tmpfs=list(data.get("tmpfs", ["/tmp"])),
        pids=int(limits.get("pids", 64)),
        memory=str(limits.get("memory", "256m")),
        cpus=str(limits.get("cpus", "1")),
        user=str(data.get("user", "1000")),
        env={str(key): str(value) for key, value in (data.get("env") or {}).items()},
        proxy_image=str(data.get("proxy_image", "python:3.12-alpine")),
        proxy_port=int(data.get("proxy_port", 3128)),
    )
    memory_bytes(policy.memory)
    nano_cpus(policy.cpus)
    if policy.kubeconfig and "KUBECONFIG" not in policy.env:
        policy.env["KUBECONFIG"] = policy.kubeconfig_target
    return policy


def read_policy(path: str) -> Policy:
    with open(path, encoding="utf-8") as handle:
        return load_policy(json.load(handle))


def memory_bytes(text: str) -> int:
    match = re.fullmatch(r"\s*([0-9]+(?:\.[0-9]+)?)\s*([kmgKMGbB]?)[bB]?\s*", text)
    if not match:
        raise ValueError(f"memory {text!r}: use bytes or a number with k, m or g")
    return int(float(match.group(1)) * MEMORY_UNITS[match.group(2).lower()])


def nano_cpus(text: str) -> int:
    try:
        return int(round(float(text) * 1_000_000_000))
    except ValueError as exc:
        raise ValueError(f"cpus {text!r}: use a number such as 0.5 or 2") from exc


def proxy_env(policy: Policy) -> Dict[str, str]:
    return {"HTTPS_PROXY": policy.proxy_url, "HTTP_PROXY": policy.proxy_url, "https_proxy": policy.proxy_url, "http_proxy": policy.proxy_url, "NO_PROXY": NO_PROXY, "no_proxy": NO_PROXY}


def network_argv(policy: Policy) -> List[List[str]]:
    return [["docker", "network", "create", "--internal", policy.network], ["docker", "network", "create", policy.egress_network]]


def proxy_argv(policy: Policy, main_py: Optional[str] = None) -> List[str]:
    source = main_py or os.path.abspath(__file__)
    argv = ["docker", "run", "-d", "--rm", "--name", policy.proxy_container, "--network", policy.egress_network, "--read-only", "--tmpfs", "/tmp", "--cap-drop", "ALL", "--security-opt", "no-new-privileges", "--pids-limit", "64", "--memory", "128m", "--user", policy.user, "-v", f"{source}:/main.py:ro", policy.proxy_image, "python3", "/main.py", "proxy", "--listen", f"0.0.0.0:{policy.proxy_port}", "--audit", "/tmp/audit.json"]
    for host in policy.allowed_hosts:
        argv += ["--allow", host]
    return argv


def connect_argv(policy: Policy) -> List[str]:
    return ["docker", "network", "connect", policy.network, policy.proxy_container]


def agent_argv(policy: Policy, command: Sequence[str], detach: bool = False) -> List[str]:
    argv = ["docker", "run", "--rm"]
    if detach:
        argv.append("-d")
    argv += ["--name", policy.agent_container, "--network", policy.network, "--read-only"]
    for path in policy.tmpfs:
        argv += ["--tmpfs", path]
    argv += ["--cap-drop", "ALL", "--security-opt", "no-new-privileges", "--pids-limit", str(policy.pids), "--memory", policy.memory, "--cpus", policy.cpus, "--user", policy.user]
    for key, value in proxy_env(policy).items():
        argv += ["-e", f"{key}={value}"]
    for key, value in sorted(policy.env.items()):
        argv += ["-e", f"{key}={value}"]
    if policy.kubeconfig:
        argv += ["-v", f"{policy.kubeconfig}:{policy.kubeconfig_target}:ro"]
    argv.append(policy.image)
    argv += list(command)
    return argv


def render(policy: Policy, command: Sequence[str], detach: bool = False, main_py: Optional[str] = None) -> Dict[str, Any]:
    return {"network": network_argv(policy), "proxy": proxy_argv(policy, main_py), "connect": connect_argv(policy), "agent": agent_argv(policy, command, detach)}


def render_text(plan: Dict[str, Any]) -> str:
    lines = [shlex.join(argv) for argv in plan["network"]]
    lines += [shlex.join(plan["proxy"]), shlex.join(plan["connect"]), shlex.join(plan["agent"])]
    return "\n".join(lines)


@dataclass
class Finding:
    check: str
    expected: str
    actual: str
    fix: str


def is_sensitive_source(source: str) -> Optional[str]:
    if not source.strip():
        return None
    normalized = source.rstrip("/") or "/"
    if normalized == "/":
        return "the host root filesystem"
    for prefix in SENSITIVE_SOURCES:
        if normalized == prefix or normalized.startswith(prefix + "/"):
            return f"host path under {prefix}"
    for directory in CREDENTIAL_DIRS:
        if normalized.endswith("/" + directory) or f"/{directory}/" in normalized + "/":
            return f"credential directory {directory}"
    return None


def check_container(policy: Policy, inspect: Dict[str, Any]) -> List[Finding]:
    host_config = inspect.get("HostConfig") or {}
    config = inspect.get("Config") or {}
    networks = (inspect.get("NetworkSettings") or {}).get("Networks") or {}
    findings: List[Finding] = []

    def expect(check: str, ok: bool, expected: str, actual: Any, fix: str) -> None:
        if not ok:
            findings.append(Finding(check, expected, str(actual), fix))

    expect("image", config.get("Image") == policy.image, policy.image, config.get("Image"), f"run {policy.image}")
    expect("read-only rootfs", host_config.get("ReadonlyRootfs") is True, "true", str(host_config.get("ReadonlyRootfs")).lower(), "--read-only")
    cap_drop = [str(cap).upper() for cap in host_config.get("CapDrop") or []]
    expect("capabilities dropped", "ALL" in cap_drop, "CapDrop [ALL]", f"CapDrop {host_config.get('CapDrop')}", "--cap-drop ALL")
    expect("no capabilities added", not (host_config.get("CapAdd") or []), "CapAdd []", f"CapAdd {host_config.get('CapAdd')}", "remove --cap-add")
    expect("not privileged", not host_config.get("Privileged"), "false", str(host_config.get("Privileged")).lower(), "remove --privileged")
    security = [str(opt) for opt in host_config.get("SecurityOpt") or []]
    expect("no-new-privileges", any(opt.startswith("no-new-privileges") and not opt.endswith(":false") for opt in security), "no-new-privileges", security, "--security-opt no-new-privileges")
    expect("seccomp confined", "seccomp=unconfined" not in security, "default or custom seccomp profile", security, "remove --security-opt seccomp=unconfined")
    expect("pids limit", host_config.get("PidsLimit") == policy.pids, str(policy.pids), host_config.get("PidsLimit"), f"--pids-limit {policy.pids}")
    expect("memory limit", host_config.get("Memory") == memory_bytes(policy.memory), f"{memory_bytes(policy.memory)} ({policy.memory})", host_config.get("Memory"), f"--memory {policy.memory}")
    expect("cpu limit", host_config.get("NanoCpus") == nano_cpus(policy.cpus), f"{nano_cpus(policy.cpus)} ({policy.cpus} cpu)", host_config.get("NanoCpus"), f"--cpus {policy.cpus}")
    attached = sorted(networks)
    expect("network", host_config.get("NetworkMode") == policy.network and attached == [policy.network], policy.network, f"{host_config.get('NetworkMode')} / attached {attached}", f"--network {policy.network}; docker network disconnect <other> {policy.agent_container}")
    expect("user", str(config.get("User") or "") == policy.user, policy.user, config.get("User") or "root (empty)", f"--user {policy.user}")
    env = [str(item) for item in config.get("Env") or []]
    expect("proxy environment", f"HTTPS_PROXY={policy.proxy_url}" in env, f"HTTPS_PROXY={policy.proxy_url}", [item for item in env if "PROXY" in item.upper()] or "unset", f"-e HTTPS_PROXY={policy.proxy_url}")
    tmpfs = sorted((host_config.get("Tmpfs") or {}).keys())
    expect("tmpfs", tmpfs == sorted(policy.tmpfs), ", ".join(sorted(policy.tmpfs)), ", ".join(tmpfs) or "none", " ".join(f"--tmpfs {path}" for path in policy.tmpfs))
    allowed_mounts = {policy.kubeconfig_target: False} if policy.kubeconfig else {}
    seen = set()
    for mount in inspect.get("Mounts") or []:
        source, destination, writable = str(mount.get("Source", "")), str(mount.get("Destination", "")), bool(mount.get("RW"))
        seen.add(destination)
        sensitive = None if destination in allowed_mounts else is_sensitive_source(source)
        if sensitive:
            findings.append(Finding("sensitive mount", "none", f"{source} -> {destination} ({sensitive})", f"remove -v {source}:{destination}"))
        elif destination not in allowed_mounts:
            findings.append(Finding("unexpected mount", "only the policy's mounts", f"{source} -> {destination}", f"remove -v {source}:{destination}"))
        elif source != policy.kubeconfig:
            findings.append(Finding("mount source", f"{policy.kubeconfig} -> {destination}", f"{source} -> {destination}", f"-v {policy.kubeconfig}:{destination}:ro"))
        elif writable and not allowed_mounts[destination]:
            findings.append(Finding("mount is writable", f"{destination} read-only", f"{destination} rw", f"-v {source}:{destination}:ro"))
    for destination in allowed_mounts:
        if destination not in seen:
            findings.append(Finding("missing mount", destination, "absent", f"-v {policy.kubeconfig}:{destination}:ro"))
    return findings


def render_findings(policy: Policy, findings: Sequence[Finding]) -> str:
    header = f"{policy.agent_container} vs policy {policy.name}: {len(findings)} drift finding(s)"
    if not findings:
        return header + "\n  no drift: the container matches the policy"
    lines = [header]
    for finding in findings:
        lines.append(f"  DRIFT {finding.check:<22} expected {finding.expected:<30} actual {finding.actual}")
        lines.append(f"        fix: {finding.fix}")
    return "\n".join(lines)


def subprocess_runner(argv: Sequence[str]) -> Tuple[int, str, str]:
    completed = subprocess.run(list(argv), capture_output=True, text=True, timeout=60)
    return completed.returncode, completed.stdout, completed.stderr


def inspect_container(name: str, runner: Runner = subprocess_runner) -> Dict[str, Any]:
    code, out, err = runner(["docker", "inspect", name])
    if code != 0:
        raise RuntimeError(f"docker inspect {name} failed: {err.strip() or out.strip()}")
    data = json.loads(out)
    if not isinstance(data, list) or not data:
        raise RuntimeError(f"docker inspect {name}: no such container")
    return data[0]


def parse_listen(text: str) -> Tuple[str, int]:
    host, sep, port = text.rpartition(":")
    if not sep or not port.isdigit():
        raise ValueError(f"--listen {text!r}: use HOST:PORT, for example 127.0.0.1:3128")
    return host or "0.0.0.0", int(port)


def run_proxy(args: argparse.Namespace) -> int:
    host, port = parse_listen(args.listen)
    rules = list(args.allow)
    if args.policy:
        rules += read_policy(args.policy).allowed_hosts
    policy = EgressPolicy(rules)
    log = None if args.quiet else (lambda line: print(line, file=sys.stderr, flush=True))
    audit = Audit(policy.texts, args.audit, log)
    server = ProxyServer((host, port), policy, audit)
    audit.flush()
    print(f"agentbox proxy listening on {host}:{server.server_address[1]} with {len(policy.rules)} allow rule(s): {', '.join(policy.texts) or 'none (everything is denied)'}", file=sys.stderr, flush=True)

    def stop(signum: int, _frame: Any) -> None:
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        server.serve_forever(poll_interval=0.2)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        audit.flush()
        summary = audit.snapshot()["summary"]
        print(f"agentbox proxy stopped: {summary['requests']} request(s), {summary['allowed']} allowed, {summary['denied']} denied, {summary['bytes_out']} bytes out", file=sys.stderr, flush=True)
    return 0


def run_render(args: argparse.Namespace) -> int:
    policy = read_policy(args.policy)
    command = list(args.command)
    if command and command[0] == "--":
        command = command[1:]
    plan = render(policy, command, args.detach)
    print(json.dumps(plan, indent=2) if args.json else render_text(plan))
    return 0


def run_check(args: argparse.Namespace, runner: Runner = subprocess_runner) -> int:
    policy = read_policy(args.policy)
    if args.inspect:
        with open(args.inspect, encoding="utf-8") as handle:
            data = json.load(handle)
        inspect = data[0] if isinstance(data, list) else data
    else:
        inspect = inspect_container(args.container, runner)
    findings = check_container(policy, inspect)
    if args.json:
        print(json.dumps({"container": policy.agent_container, "policy": policy.name, "drift": len(findings), "findings": [asdict(finding) for finding in findings]}, indent=2))
    else:
        print(render_findings(policy, findings))
    return 1 if findings else 0


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="agentbox", description=__doc__)
    subparsers = parser.add_subparsers(dest="action", required=True)

    proxy = subparsers.add_parser("proxy", help="run the egress proxy")
    proxy.add_argument("--listen", default=DEFAULT_LISTEN, metavar="HOST:PORT")
    proxy.add_argument("--allow", action="append", default=[], metavar="HOST[:PORT]", help="host, host:port, *.domain or * (repeat)")
    proxy.add_argument("--policy", metavar="FILE", help="take allowed_hosts from a policy file as well")
    proxy.add_argument("--audit", metavar="FILE", help="JSON audit rewritten after every request")
    proxy.add_argument("--quiet", action="store_true", help="no per-request log line on stderr")

    rendering = subparsers.add_parser("render", help="print the docker commands a policy means")
    rendering.add_argument("--policy", required=True, metavar="FILE")
    rendering.add_argument("--detach", action="store_true", help="render the agent container with -d")
    rendering.add_argument("--json", action="store_true")
    rendering.add_argument("command", nargs=argparse.REMAINDER, help="command to run inside the agent container")

    checking = subparsers.add_parser("check", help="compare a container with the policy and list drift")
    checking.add_argument("--policy", required=True, metavar="FILE")
    source = checking.add_mutually_exclusive_group(required=True)
    source.add_argument("--container", metavar="NAME", help="docker inspect this running container")
    source.add_argument("--inspect", metavar="FILE", help="a saved docker inspect JSON instead")
    checking.add_argument("--json", action="store_true")

    args = parser.parse_args(list(sys.argv[1:] if argv is None else argv))
    try:
        if args.action == "proxy":
            return run_proxy(args)
        if args.action == "render":
            return run_render(args)
        return run_check(args)
    except (OSError, ValueError, RuntimeError, json.JSONDecodeError) as exc:
        print(f"agentbox: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
