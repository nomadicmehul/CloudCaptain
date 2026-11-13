"""minilambda: a local Lambda platform (the Invoke API, one Runtime API per execution environment, cold, warm and suppressed inits, freezing, timeouts, reserved and provisioned concurrency, Lambda-style logs) and a custom runtime (bootstrap) that runs Python handlers against it or against the AWS Runtime Interface Emulator."""
from __future__ import annotations

import argparse
import base64
import importlib
import json
import logging
import math
import os
import re
import select
import shlex
import signal
import subprocess
import sys
import threading
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import IO, Any, Callable, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

THIS_FILE = os.path.abspath(__file__)
RUNTIME_PREFIX = "/2018-06-01/runtime"
INVOKE_PATH = re.compile(r"^/2015-03-31/functions/([^/]+)/invocations$")
MAX_RESPONSE_BYTES = 6 * 1024 * 1024 + 100
LOG_TAIL_BYTES = 4096
DRAIN_GRACE = 0.25
ERROR_TYPE_PATTERN = re.compile(r"(Runtime|Function)\.[A-Z][a-zA-Z]+")
SIGNAL_NAMES = {1: "hangup", 2: "interrupt", 6: "aborted", 9: "killed", 11: "segmentation fault", 15: "terminated"}
RESET_OUTCOMES = ("crash", "timeout")
CURRENT_REQUEST = {"id": ""}


class LambdaError(Exception):
    pass


def iso_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def json_bytes(value: Any) -> bytes:
    return json.dumps(value).encode("utf-8")


def billed_ms(duration_ms: float, init_ms: Optional[float] = None) -> int:
    return int(math.ceil(round(duration_ms + (init_ms or 0.0), 6)))


def normalize_error_type(raw: Optional[str]) -> str:
    text = raw or ""
    if ERROR_TYPE_PATTERN.search(text):
        return text
    return "Function.Unknown" if text.startswith("Function.") else "Runtime.Unknown"


def platform_error(error_type: str, message: str, request_id: str = "") -> Dict[str, str]:
    prefix = f"RequestId: {request_id} Error: " if request_id else "Error: "
    return {"errorType": error_type, "errorMessage": prefix + message}


def describe_exit(returncode: int) -> str:
    if returncode >= 0:
        return f"exit status {returncode}"
    return "signal: " + SIGNAL_NAMES.get(-returncode, f"signal {-returncode}")


def exit_error(returncode: int, request_id: str = "") -> Dict[str, str]:
    if returncode == 0:
        return platform_error("Runtime.ExitError", "Runtime exited without providing a reason", request_id)
    return platform_error("Runtime.ExitError", f"Runtime exited with error: {describe_exit(returncode)}", request_id)


def timeout_error(timeout: float, request_id: str = "") -> Dict[str, str]:
    return platform_error("Sandbox.Timedout", f"Task timed out after {timeout:.2f} seconds", request_id)


def throttle_body(reason: str) -> Dict[str, str]:
    return {"Reason": reason, "Type": "User", "message": "Rate Exceeded."}


def report_line(request_id: str, duration_ms: float, memory_mb: int, max_memory_mb: int, init_ms: Optional[float] = None, status: str = "", error_type: str = "") -> str:
    parts = [
        f"REPORT RequestId: {request_id}",
        f"Duration: {duration_ms:.2f} ms",
        f"Billed Duration: {billed_ms(duration_ms, init_ms)} ms",
        f"Memory Size: {memory_mb} MB",
        f"Max Memory Used: {max_memory_mb} MB",
    ]
    if init_ms is not None:
        parts.append(f"Init Duration: {init_ms:.2f} ms")
    if status:
        parts.append(f"Status: {status}")
    if error_type:
        parts.append(f"Error Type: {error_type}")
    return "\t".join(parts)


def init_report_line(init_ms: float, phase: str = "", status: str = "", error_type: str = "") -> str:
    parts = [f"INIT_REPORT Init Duration: {init_ms:.2f} ms"]
    if phase:
        parts.append(f"Phase: {phase}")
    if status:
        parts.append(f"Status: {status}")
    if error_type:
        parts.append(f"Error Type: {error_type}")
    return "\t".join(parts)


REPORT_NUMBERS = re.compile(r"(Init Duration|Restore Duration|Billed Duration|Max Memory Used|Memory Size|Duration): ([\d.]+) (?:ms|MB)")
REPORT_KEYS = {"Init Duration": "init_ms", "Restore Duration": "restore_ms", "Billed Duration": "billed_ms", "Max Memory Used": "max_memory_mb", "Memory Size": "memory_mb", "Duration": "duration_ms"}
JSON_METRICS = {"initDurationMs": "init_ms", "restoreDurationMs": "restore_ms", "billedDurationMs": "billed_ms", "maxMemoryUsedMB": "max_memory_mb", "memorySizeMB": "memory_mb", "durationMs": "duration_ms"}


def parse_log_line(line: str) -> Optional[Dict[str, Any]]:
    text = line.strip()
    start = text.find("{")
    if start >= 0 and '"platform.' in text:
        try:
            event = json.loads(text[start:])
        except ValueError:
            event = None
        if isinstance(event, dict) and event.get("type") in ("platform.report", "platform.initReport"):
            record = event.get("record") or {}
            entry: Dict[str, Any] = {"kind": "report" if event["type"] == "platform.report" else "init_report"}
            for key, name in JSON_METRICS.items():
                if key in (record.get("metrics") or {}):
                    entry[name] = float(record["metrics"][key])
            entry["request_id"] = record.get("requestId", "")
            entry["status"] = record.get("status", "")
            entry["error_type"] = record.get("errorType", "")
            entry["phase"] = record.get("phase", "")
            if entry["kind"] == "init_report" and "duration_ms" in entry:
                entry["init_ms"] = entry.pop("duration_ms")
            return entry
    for marker, kind in (("INIT_REPORT ", "init_report"), ("REPORT RequestId:", "report")):
        position = text.find(marker)
        if position < 0:
            continue
        body = text[position:]
        entry = {"kind": kind}
        for key, value in REPORT_NUMBERS.findall(body):
            entry[REPORT_KEYS[key]] = float(value)
        for key, pattern in (("request_id", r"RequestId: (\S+)"), ("phase", r"Phase: (\w+)"), ("status", r"Status: (\w+)"), ("error_type", r"Error Type: (\S+)")):
            found = re.search(pattern, body)
            entry[key] = found.group(1) if found else ""
        return entry
    return None


def percentile(values: Sequence[float], share: float) -> Optional[float]:
    if not values:
        return None
    ordered = sorted(values)
    rank = max(1, int(math.ceil(share * len(ordered))))
    return ordered[rank - 1]


def summarize(entries: Iterable[Dict[str, Any]]) -> Dict[str, Any]:
    items = list(entries)
    reports = [e for e in items if e["kind"] == "report"]
    init_reports = [e for e in items if e["kind"] == "init_report"]
    inits = [e["init_ms"] for e in reports if "init_ms" in e]
    durations = [e["duration_ms"] for e in reports if "duration_ms" in e]
    billed = sum(e.get("billed_ms", 0.0) for e in reports)
    statuses: Dict[str, int] = {}
    for entry in reports:
        key = entry.get("status") or "success"
        statuses[key] = statuses.get(key, 0) + 1
    init_failures: Dict[str, int] = {}
    for entry in init_reports:
        if entry.get("status") and entry["status"] != "success":
            key = f"{entry.get('phase') or '?'}:{entry['status']}:{entry.get('error_type') or '-'}"
            init_failures[key] = init_failures.get(key, 0) + 1
    return {
        "invocations": len(reports),
        "cold_starts": len(inits),
        "cold_start_rate": round(len(inits) / len(reports), 4) if reports else 0.0,
        "init_ms": {"p50": percentile(inits, 0.5), "p95": percentile(inits, 0.95), "max": max(inits) if inits else None},
        "duration_ms": {"p50": percentile(durations, 0.5), "p95": percentile(durations, 0.95), "max": max(durations) if durations else None},
        "billed_ms_total": int(billed),
        "init_share_of_billed": round(sum(inits) / billed, 4) if billed else 0.0,
        "max_memory_mb": max((e.get("max_memory_mb", 0.0) for e in reports), default=None),
        "memory_mb": max((e.get("memory_mb", 0.0) for e in reports), default=None),
        "statuses": statuses,
        "init_failures": init_failures,
        "provisioned_inits": sum(1 for e in init_reports if not e.get("status") or e.get("status") == "success"),
    }


def peak_rss_mb(pid: int, proc_root: str = "/proc") -> Optional[int]:
    try:
        with open(os.path.join(proc_root, str(pid), "status"), encoding="utf-8") as handle:
            for line in handle:
                if line.startswith("VmHWM:"):
                    return int(math.ceil(int(line.split()[1]) / 1024))
    except (OSError, ValueError, IndexError):
        return None
    return None


def default_memory_probe(pid: int) -> Optional[int]:
    peak = peak_rss_mb(pid)
    if peak is not None:
        return peak
    try:
        out = subprocess.run(["ps", "-o", "rss=", "-p", str(pid)], capture_output=True, text=True, timeout=2).stdout.strip()
        return int(math.ceil(int(out) / 1024)) if out else None
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


def signal_group(process: subprocess.Popen, signum: Optional[int]) -> bool:
    if signum is None or process.poll() is not None:
        return False
    try:
        if hasattr(os, "killpg"):
            os.killpg(process.pid, signum)
        else:
            process.send_signal(signum)
        return True
    except (ProcessLookupError, PermissionError, OSError):
        return False


@dataclass
class EnvView:
    id: int
    status: str
    last_used: float
    provisioned: bool = False


@dataclass
class Placement:
    action: str
    env_id: Optional[int] = None
    reason: str = ""


def place(envs: Sequence[EnvView], reserved: Optional[int], account_limit: int) -> Placement:
    in_flight = sum(1 for env in envs if env.status == "busy")
    if reserved is not None and in_flight >= reserved:
        return Placement("throttle", reason="ReservedFunctionConcurrentInvocationLimitExceeded")
    if in_flight >= account_limit:
        return Placement("throttle", reason="ConcurrentInvocationLimitExceeded")
    for status, action in (("idle", "warm"), ("reset", "reinit")):
        candidates = [env for env in envs if env.status == status]
        if candidates:
            chosen = max(candidates, key=lambda env: (env.provisioned, env.last_used, -env.id))
            return Placement(action, chosen.id)
    return Placement("cold")


@dataclass
class FunctionConfig:
    handler: str
    task_root: str
    command: List[str] = field(default_factory=lambda: [sys.executable, THIS_FILE, "bootstrap"])
    name: str = "minilambda"
    timeout: float = 3.0
    memory_mb: int = 128
    reserved_concurrency: Optional[int] = None
    account_concurrency: int = 10
    init_timeout: float = 10.0
    idle_ttl: Optional[float] = None
    max_response_bytes: int = MAX_RESPONSE_BYTES
    log_format: str = "Text"
    freeze: bool = True
    environment: Dict[str, str] = field(default_factory=dict)
    runtime_api_port: int = 0
    region: str = "us-east-1"
    account: str = "000000000000"

    @property
    def arn(self) -> str:
        return f"arn:aws:lambda:{self.region}:{self.account}:function:{self.name}"


@dataclass
class Invocation:
    request_id: str
    payload: bytes
    trace_id: str = ""
    client_context: str = ""
    invocation_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    deadline_ms: int = 0
    finished: bool = False
    finished_at: float = 0.0
    body: bytes = b""
    error_type: str = ""
    outcome: str = "success"
    logs: List[str] = field(default_factory=list)

    def finish(self, body: bytes, error_type: str = "", outcome: str = "success") -> bool:
        if self.finished:
            return False
        self.finished, self.finished_at = True, time.monotonic()
        self.body, self.error_type, self.outcome = body, error_type, outcome
        return True


@dataclass
class InvokeResult:
    status: int
    body: bytes
    headers: Dict[str, str]
    request_id: str
    outcome: str = "success"
    cold: bool = False
    init_ms: Optional[float] = None
    duration_ms: float = 0.0
    billed_ms: int = 0
    environment: Optional[int] = None
    logs: List[str] = field(default_factory=list)

    def json(self) -> Any:
        return json.loads(self.body.decode("utf-8"))

    def log_tail(self) -> str:
        text = "".join(line + "\n" for line in self.logs).encode("utf-8")[-LOG_TAIL_BYTES:]
        return base64.b64encode(text).decode("ascii")


def rapi_error(error_type: str, message: str) -> Dict[str, str]:
    return {"errorType": error_type, "errorMessage": message}


class Environment:
    def __init__(self, platform: "Platform", env_id: int, provisioned: bool = False, port: int = 0) -> None:
        self.platform = platform
        self.id = env_id
        self.provisioned = provisioned
        self.process: Optional[subprocess.Popen] = None
        self.generation = 0
        self.alive = False
        self.initialized = False
        self.init_done_at = 0.0
        self.init_error: Optional[Tuple[str, bytes]] = None
        self.exit_code: Optional[int] = None
        self.claimed: Optional[Invocation] = None
        self.pending: Optional[Invocation] = None
        self.current: Optional[Invocation] = None
        self.frozen = False
        self.closing = False
        self.last_used = time.monotonic()
        self.invocations = 0
        self.max_memory_mb = 0
        self.reading = threading.Lock()
        self.server = RuntimeAPIServer(("127.0.0.1", port), self)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()

    @property
    def status(self) -> str:
        if self.claimed is not None:
            return "busy"
        if not self.alive:
            return "reset"
        return "idle" if self.initialized else "initializing"

    def view(self) -> EnvView:
        return EnvView(self.id, self.status, self.last_used, self.provisioned)

    def describe(self) -> Dict[str, Any]:
        return {"id": self.id, "status": self.status, "provisioned": self.provisioned, "generation": self.generation, "pid": self.process.pid if self.process and self.alive else None, "frozen": self.frozen, "invocations": self.invocations, "runtime_api": f"127.0.0.1:{self.port}"}

    def start(self, initialization_type: str) -> None:
        config = self.platform.config
        self.generation += 1
        self.alive, self.initialized, self.init_error, self.exit_code = True, False, None, None
        self.current = self.pending = None
        self.frozen = False
        try:
            process = subprocess.Popen(config.command, cwd=config.task_root, env=self.platform.runtime_environment(self, initialization_type), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, start_new_session=True, bufsize=0)
        except OSError as exc:
            self.alive = False
            self.init_error = ("Runtime.InvalidEntrypoint", json_bytes(platform_error("Runtime.InvalidEntrypoint", f"Couldn't find valid bootstrap(s): {config.command} ({exc.strerror or exc})")))
            return
        self.process = process
        threading.Thread(target=self.pump, args=(process,), daemon=True).start()
        threading.Thread(target=self.watch, args=(process, self.generation), daemon=True).start()

    def kill(self) -> None:
        self.alive = False
        self.frozen = False
        if self.process is not None:
            signal_group(self.process, signal.SIGKILL)

    def shutdown(self) -> None:
        self.closing = True
        self.kill()
        threading.Thread(target=self.stop_server, daemon=True).start()

    def stop_server(self) -> None:
        self.server.shutdown()
        self.server.server_close()

    def freeze(self) -> None:
        if self.process is not None and not self.frozen and signal_group(self.process, getattr(signal, "SIGSTOP", None)):
            self.frozen = True

    def thaw(self) -> None:
        if self.frozen and self.process is not None:
            signal_group(self.process, getattr(signal, "SIGCONT", None))
        self.frozen = False

    def pump(self, process: subprocess.Popen) -> None:
        assert process.stdout is not None
        fd = process.stdout.fileno()
        partial = b""
        while True:
            try:
                ready = select.select([fd], [], [], 0.05)[0]
            except (OSError, ValueError):
                break
            if not ready:
                continue
            with self.reading:
                chunk = os.read(fd, 65536)
                lines = (partial + chunk).split(b"\n")
                partial = lines.pop() if chunk else b""
                for raw in lines if chunk else [line for line in lines if line]:
                    self.emit_output(raw)
            if not chunk:
                break
        process.stdout.close()

    def emit_output(self, raw: bytes) -> None:
        line = raw.decode("utf-8", "replace").rstrip("\r")
        with self.platform.cond:
            target = self.current or self.pending or (self.claimed if self.claimed is not None and not self.claimed.finished else None)
            self.platform.emit(line, target)

    def drain_output(self, limit: float = 0.2) -> None:
        process = self.process
        deadline = time.monotonic() + limit
        while process is not None and process.stdout is not None and not process.stdout.closed and time.monotonic() < deadline:
            if self.reading.acquire(timeout=max(0.0, deadline - time.monotonic())):
                try:
                    pending = bool(select.select([process.stdout], [], [], 0)[0])
                except (OSError, ValueError):
                    pending = False
                finally:
                    self.reading.release()
                if not pending:
                    return
            time.sleep(0.005)

    def watch(self, process: subprocess.Popen, generation: int) -> None:
        code = process.wait()
        with self.platform.cond:
            if generation != self.generation:
                return
            self.exit_code = code
            self.alive = False
            self.frozen = False
            for invocation in (self.current, self.pending):
                if invocation is not None:
                    invocation.finish(json_bytes(exit_error(code, invocation.request_id)), "Runtime.ExitError", "crash")
            self.current = self.pending = None
            if self.claimed is not None and self.claimed.finished:
                self.claimed = None
            self.platform.cond.notify_all()

    def await_init(self, timeout: float, request_id: str = "") -> Tuple[str, str, bytes]:
        cond = self.platform.cond
        with cond:
            cond.wait_for(lambda: self.initialized or self.init_error is not None or not self.alive, timeout=max(timeout, 0.0))
            if self.init_error is not None:
                error_type, body = self.init_error
                self.kill()
                return "error", error_type, body
            if self.initialized and self.alive:
                return "success", "", b""
            if not self.alive:
                code = self.exit_code if self.exit_code is not None else -9
                return "error", "Runtime.ExitError", json_bytes(exit_error(code, request_id))
            self.kill()
            return "timeout", "Sandbox.Timedout", json_bytes(timeout_error(timeout, request_id))

    def assign(self, invocation: Invocation) -> None:
        if not self.alive:
            invocation.finish(json_bytes(exit_error(self.exit_code if self.exit_code is not None else -9, invocation.request_id)), "Runtime.ExitError", "crash")
        else:
            self.pending = invocation
        self.platform.cond.notify_all()

    def next_invocation(self) -> Tuple[int, Optional[Invocation], Dict[str, str]]:
        cond = self.platform.cond
        with cond:
            generation = self.generation
            if self.current is not None and not self.current.finished:
                return 403, None, rapi_error("InvalidStateTransition", f"State transition from Invoking to Ready failed: no response or error was posted for {self.current.request_id}")
            self.current = None
            if not self.initialized:
                self.initialized, self.init_done_at = True, time.monotonic()
            if self.claimed is not None and self.claimed.finished:
                self.claimed = None
                self.last_used = time.monotonic()
            cond.notify_all()
            if self.pending is None and self.claimed is None and self.platform.config.freeze:
                self.freeze()
            cond.wait_for(lambda: self.pending is not None or self.closing or self.generation != generation or not self.alive)
            if self.pending is None or self.generation != generation or not self.alive:
                return 500, None, rapi_error("Runtime.Shutdown", "the execution environment is shutting down; exit")
            self.thaw()
            invocation, self.pending = self.pending, None
            self.current = invocation
            return 200, invocation, {}

    def answer(self, request_id: str, invocation_id: str) -> Tuple[Optional[Invocation], int, Dict[str, str]]:
        invocation = self.current
        if invocation is None or invocation.finished:
            return None, 403, rapi_error("InvalidStateTransition", "no invocation is waiting for a response in this execution environment")
        if request_id != invocation.request_id:
            return None, 400, rapi_error("InvalidRequestID", "Invalid request ID")
        if invocation_id and invocation_id != invocation.invocation_id:
            return None, 400, rapi_error("InvalidInvocationId", "Lambda-Runtime-Invocation-Id does not match the active invocation")
        return invocation, 202, {"status": "OK"}

    def respond(self, request_id: str, invocation_id: str, body: bytes) -> Tuple[int, Dict[str, str]]:
        limit = self.platform.config.max_response_bytes
        with self.platform.cond:
            invocation, status, payload = self.answer(request_id, invocation_id)
            if invocation is None:
                return status, payload
            if len(body) > limit:
                message = f"Response payload size exceeded maximum allowed payload size ({limit} bytes)."
                invocation.finish(json_bytes({"errorType": "Function.ResponseSizeTooLarge", "errorMessage": message}), "Function.ResponseSizeTooLarge", "error")
                self.platform.cond.notify_all()
                return 413, rapi_error("RequestEntityTooLarge", f"Exceeded maximum allowed payload size ({limit} bytes).")
            invocation.finish(body)
            self.platform.cond.notify_all()
            return status, payload

    def fail(self, request_id: str, invocation_id: str, error_type: str, body: bytes) -> Tuple[int, Dict[str, str]]:
        with self.platform.cond:
            invocation, status, payload = self.answer(request_id, invocation_id)
            if invocation is None:
                return status, payload
            invocation.finish(body or json_bytes(platform_error(normalize_error_type(error_type), "the runtime reported an error without a body", request_id)), normalize_error_type(error_type), "error")
            self.platform.cond.notify_all()
            return status, payload

    def init_failed(self, error_type: str, body: bytes) -> Tuple[int, Dict[str, str]]:
        with self.platform.cond:
            if self.initialized:
                return 403, rapi_error("InvalidStateTransition", "init/error is only accepted before the runtime's first call to next")
            self.init_error = (normalize_error_type(error_type), body or json_bytes(platform_error("Runtime.Unknown", "init failed without a reason")))
            self.platform.cond.notify_all()
            return 202, {"status": "OK"}


class RuntimeAPIServer(ThreadingHTTPServer):
    daemon_threads = True
    block_on_close = False
    allow_reuse_address = True

    def __init__(self, address: Tuple[str, int], env: Environment) -> None:
        super().__init__(address, RuntimeAPIHandler)
        self.env = env


class RuntimeAPIHandler(BaseHTTPRequestHandler):
    server: RuntimeAPIServer
    server_version = "minilambda-runtime-api/1.0"

    def log_message(self, fmt: str, *args: Any) -> None:
        pass

    def reply(self, status: int, payload: Any, headers: Optional[Dict[str, str]] = None, raw: Optional[bytes] = None) -> None:
        body = raw if raw is not None else json_bytes(payload)
        self.send_response(status)
        self.send_header("Content-Type", (headers or {}).pop("Content-Type", "application/json"))
        self.send_header("Content-Length", str(len(body)))
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def read_body(self) -> bytes:
        length = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(length) if length > 0 else b""

    def do_GET(self) -> None:
        path = urllib.parse.urlsplit(self.path).path
        if path != RUNTIME_PREFIX + "/invocation/next":
            self.reply(404, rapi_error("NotFound", f"unknown Runtime API path {path}"))
            return
        env = self.server.env
        status, invocation, error = env.next_invocation()
        if invocation is None:
            self.reply(status, error)
            return
        config = env.platform.config
        headers = {
            "Content-Type": "application/json",
            "Lambda-Runtime-Aws-Request-Id": invocation.request_id,
            "Lambda-Runtime-Deadline-Ms": str(invocation.deadline_ms),
            "Lambda-Runtime-Invoked-Function-Arn": config.arn,
            "Lambda-Runtime-Trace-Id": invocation.trace_id,
            "Lambda-Runtime-Invocation-Id": invocation.invocation_id,
        }
        if invocation.client_context:
            headers["Lambda-Runtime-Client-Context"] = invocation.client_context
        self.reply(200, None, headers, invocation.payload)

    def do_POST(self) -> None:
        path = urllib.parse.urlsplit(self.path).path
        body = self.read_body()
        env = self.server.env
        error_type = self.headers.get("Lambda-Runtime-Function-Error-Type", "")
        invocation_id = self.headers.get("Lambda-Runtime-Invocation-Id", "")
        if path == RUNTIME_PREFIX + "/init/error":
            status, payload = env.init_failed(error_type, body)
            self.reply(status, payload)
            return
        match = re.match(re.escape(RUNTIME_PREFIX) + r"/invocation/([^/]+)/(response|error)$", path)
        if not match:
            self.reply(404, rapi_error("NotFound", f"unknown Runtime API path {path}"))
            return
        request_id = urllib.parse.unquote(match.group(1))
        if match.group(2) == "response":
            status, payload = env.respond(request_id, invocation_id, body)
        else:
            status, payload = env.fail(request_id, invocation_id, error_type, body)
        self.reply(status, payload)


def new_trace_id() -> str:
    return f"Root=1-{int(time.time()):08x}-{uuid.uuid4().hex[:24]};Parent={uuid.uuid4().hex[:16]};Sampled=0"


class Platform:
    def __init__(self, config: FunctionConfig, sink: Optional[IO[str]] = None, memory_probe: Optional[Callable[[int], Optional[int]]] = None) -> None:
        self.config = config
        self.sink = sink if sink is not None else sys.stderr
        self.memory_probe = memory_probe or default_memory_probe
        self.cond = threading.Condition()
        self.envs: List[Environment] = []
        self.next_id = 1
        self.closed = False
        self.log_stream = datetime.now(timezone.utc).strftime("%Y/%m/%d") + "/[$LATEST]" + uuid.uuid4().hex

    def runtime_environment(self, env: Environment, initialization_type: str) -> Dict[str, str]:
        config = self.config
        values = {"PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"), "LANG": os.environ.get("LANG", "C.UTF-8"), "TZ": ":UTC"}
        values.update(config.environment)
        values.update({
            "AWS_LAMBDA_RUNTIME_API": f"127.0.0.1:{env.port}",
            "_HANDLER": config.handler,
            "LAMBDA_TASK_ROOT": os.path.abspath(config.task_root),
            "LAMBDA_RUNTIME_DIR": os.path.dirname(THIS_FILE),
            "AWS_LAMBDA_FUNCTION_NAME": config.name,
            "AWS_LAMBDA_FUNCTION_VERSION": "$LATEST",
            "AWS_LAMBDA_FUNCTION_MEMORY_SIZE": str(config.memory_mb),
            "AWS_LAMBDA_LOG_GROUP_NAME": f"/aws/lambda/{config.name}",
            "AWS_LAMBDA_LOG_STREAM_NAME": self.log_stream,
            "AWS_LAMBDA_LOG_FORMAT": config.log_format,
            "AWS_LAMBDA_INITIALIZATION_TYPE": initialization_type,
            "AWS_REGION": config.region,
            "AWS_DEFAULT_REGION": config.region,
        })
        return values

    def emit(self, line: str, invocation: Optional[Invocation] = None) -> None:
        with self.cond:
            if invocation is not None:
                invocation.logs.append(line)
            self.sink.write(line + "\n")
            self.sink.flush()

    def platform_event(self, kind: str, record: Dict[str, Any], text: str, invocation: Optional[Invocation] = None) -> None:
        if self.config.log_format == "JSON":
            if kind:
                self.emit(json.dumps({"time": iso_now(), "type": kind, "record": record}), invocation)
        elif text:
            self.emit(text, invocation)

    def create_environment(self, provisioned: bool = False) -> Environment:
        port = self.config.runtime_api_port if not self.envs else 0
        env = Environment(self, self.next_id, provisioned, port)
        self.next_id += 1
        self.envs.append(env)
        return env

    def find(self, env_id: Optional[int]) -> Environment:
        for env in self.envs:
            if env.id == env_id:
                return env
        raise LambdaError(f"no execution environment {env_id}")

    def draining(self) -> bool:
        return any(env.alive and env.claimed is not None and env.claimed.finished for env in self.envs)

    def state(self) -> Dict[str, Any]:
        with self.cond:
            return {"function": self.config.arn, "environments": [env.describe() for env in self.envs]}

    def reap_idle(self) -> None:
        ttl = self.config.idle_ttl
        if ttl is None:
            return
        now = time.monotonic()
        for env in list(self.envs):
            if not env.provisioned and env.status in ("idle", "reset") and now - env.last_used >= ttl:
                env.shutdown()
                self.envs.remove(env)

    def init_report(self, init_ms: float, phase: str, status: str, error_type: str, initialization_type: str = "on-demand", invocation: Optional[Invocation] = None) -> None:
        record: Dict[str, Any] = {"initializationType": initialization_type, "phase": phase, "status": status, "metrics": {"durationMs": round(init_ms, 3)}}
        if error_type:
            record["errorType"] = error_type
        text = init_report_line(init_ms, phase, status, error_type) if status != "success" else init_report_line(init_ms)
        self.platform_event("platform.initReport", record, text, invocation)

    def initialize(self, env: Environment, initialization_type: str, phase: str, timeout: float, request_id: str, invocation: Optional[Invocation] = None) -> Tuple[Optional[float], Optional[Tuple[str, str, bytes]]]:
        with self.cond:
            began = time.monotonic()
            env.start(initialization_type)
        status, error_type, body = env.await_init(timeout, request_id)
        if status == "success":
            return (env.init_done_at - began) * 1000.0, None
        self.init_report((time.monotonic() - began) * 1000.0, phase, status, error_type, initialization_type, invocation)
        return None, (status, error_type, body)

    def prewarm(self, count: int) -> List[Optional[float]]:
        durations: List[Optional[float]] = []
        for _ in range(count):
            with self.cond:
                env = self.create_environment(provisioned=True)
            init_ms, failure = self.initialize(env, "provisioned-concurrency", "init", max(self.config.init_timeout, 60.0), "")
            if failure is None and init_ms is not None:
                self.init_report(init_ms, "init", "success", "", "provisioned-concurrency")
            with self.cond:
                env.last_used = time.monotonic()
            durations.append(init_ms)
        return durations

    def invoke(self, payload: bytes, request_id: Optional[str] = None, trace_id: str = "", client_context: str = "") -> InvokeResult:
        config = self.config
        invocation = Invocation(request_id or str(uuid.uuid4()), payload, trace_id or new_trace_id(), client_context)
        with self.cond:
            if self.closed:
                raise LambdaError("minilambda is shutting down")
            self.reap_idle()
            decision = place([env.view() for env in self.envs], config.reserved_concurrency, config.account_concurrency)
            if decision.action in ("cold", "throttle") and self.draining():
                self.cond.wait_for(lambda: not self.draining(), timeout=DRAIN_GRACE)
                decision = place([env.view() for env in self.envs], config.reserved_concurrency, config.account_concurrency)
            if decision.action == "throttle":
                return InvokeResult(429, json_bytes(throttle_body(decision.reason)), {"Content-Type": "application/json", "X-Amzn-ErrorType": "TooManyRequestsException", "X-Amzn-RequestId": invocation.request_id}, invocation.request_id, "throttled")
            env = self.create_environment() if decision.action == "cold" else self.find(decision.env_id)
            env.claimed = invocation
            env.last_used = time.monotonic()
        init_ms: Optional[float] = None
        needs_init = decision.action == "reinit"
        if decision.action == "cold":
            init_ms, failure = self.initialize(env, "on-demand", "init", config.init_timeout, "", invocation)
            needs_init = failure is not None
        self.platform_event("platform.start", {"requestId": invocation.request_id, "version": "$LATEST"}, f"START RequestId: {invocation.request_id} Version: $LATEST", invocation)
        began = time.monotonic()
        deadline = began + config.timeout
        if needs_init:
            _, failure = self.initialize(env, "on-demand", "invoke", deadline - time.monotonic(), invocation.request_id, invocation)
            if failure is not None:
                status, error_type, body = failure
                with self.cond:
                    if status == "timeout":
                        invocation.finish(json_bytes(timeout_error(config.timeout, invocation.request_id)), "Sandbox.Timedout", "timeout")
                    else:
                        invocation.finish(body, error_type, "crash")
        if not invocation.finished:
            self.dispatch(env, invocation, deadline)
        return self.complete(env, invocation, began, init_ms, decision.action)

    def dispatch(self, env: Environment, invocation: Invocation, deadline: float) -> None:
        with self.cond:
            invocation.deadline_ms = int((time.time() + max(0.0, deadline - time.monotonic())) * 1000)
            env.assign(invocation)
            self.cond.wait_for(lambda: invocation.finished, timeout=max(0.0, deadline - time.monotonic()))
            if invocation.finish(json_bytes(timeout_error(self.config.timeout, invocation.request_id)), "Sandbox.Timedout", "timeout"):
                env.current = env.pending = None
                env.kill()
                self.platform_event("", {}, f"{iso_now()} {invocation.request_id} Task timed out after {self.config.timeout:.2f} seconds", invocation)

    def measure(self, env: Environment) -> int:
        if env.process is not None and env.alive:
            used = self.memory_probe(env.process.pid)
            if used is not None:
                env.max_memory_mb = max(env.max_memory_mb, used)
        return env.max_memory_mb

    def complete(self, env: Environment, invocation: Invocation, began: float, init_ms: Optional[float], action: str) -> InvokeResult:
        config = self.config
        env.drain_output()
        with self.cond:
            if invocation.outcome in RESET_OUTCOMES:
                if env.claimed is invocation:
                    env.claimed = None
                if env.alive:
                    env.kill()
            env.invocations += 1
            duration = max(0.0, (invocation.finished_at - began) * 1000.0)
            memory = self.measure(env)
            status = invocation.outcome if invocation.outcome in RESET_OUTCOMES else ""
            text_status = "error" if status == "crash" else status
            error_type = invocation.error_type if status == "crash" else ""
            metrics: Dict[str, Any] = {"durationMs": round(duration, 3), "billedDurationMs": billed_ms(duration, init_ms), "memorySizeMB": config.memory_mb, "maxMemoryUsedMB": memory}
            if init_ms is not None:
                metrics["initDurationMs"] = round(init_ms, 3)
            record: Dict[str, Any] = {"requestId": invocation.request_id, "metrics": metrics, "status": "success" if invocation.outcome == "success" else ("timeout" if status == "timeout" else "error")}
            if invocation.outcome != "success":
                record["errorType"] = invocation.error_type
            self.platform_event("", {}, f"END RequestId: {invocation.request_id}", invocation)
            self.platform_event("platform.report", record, report_line(invocation.request_id, duration, config.memory_mb, memory, init_ms, text_status, error_type), invocation)
            headers = {
                "Content-Type": "application/json",
                "X-Amzn-RequestId": invocation.request_id,
                "X-Amz-Executed-Version": "$LATEST",
                "X-Minilambda-Environment": str(env.id),
                "X-Minilambda-Cold": "true" if action != "warm" else "false",
            }
            if init_ms is not None:
                headers["X-Minilambda-Init-Duration-Ms"] = f"{init_ms:.2f}"
            if invocation.outcome != "success":
                headers["X-Amz-Function-Error"] = "Unhandled"
            return InvokeResult(200, invocation.body, headers, invocation.request_id, invocation.outcome, action != "warm", init_ms, duration, billed_ms(duration, init_ms), env.id, list(invocation.logs))

    def close(self) -> None:
        with self.cond:
            self.closed = True
            processes = [env.process for env in self.envs if env.process is not None]
            for env in self.envs:
                env.shutdown()
            self.cond.notify_all()
        for process in processes:
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass


def split_function_name(name: str) -> Tuple[str, str]:
    if name.startswith("arn:"):
        parts = name.split(":")
        return (parts[6] if len(parts) > 6 else ""), (parts[7] if len(parts) > 7 else "$LATEST")
    bare, _, qualifier = name.partition(":")
    return bare, qualifier or "$LATEST"


class InvokeServer(ThreadingHTTPServer):
    daemon_threads = True
    block_on_close = False
    allow_reuse_address = True

    def __init__(self, address: Tuple[str, int], platform: Platform) -> None:
        super().__init__(address, InvokeHandler)
        self.platform = platform


class InvokeHandler(BaseHTTPRequestHandler):
    server: InvokeServer
    server_version = "minilambda/1.0"

    def log_message(self, fmt: str, *args: Any) -> None:
        pass

    def reply(self, status: int, body: bytes, headers: Dict[str, str]) -> None:
        self.send_response(status)
        for key, value in headers.items():
            self.send_header(key, value)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def user_error(self, status: int, error_type: str, message: str) -> None:
        self.reply(status, json_bytes({"Type": "User", "message": message}), {"Content-Type": "application/json", "X-Amzn-ErrorType": error_type})

    def do_GET(self) -> None:
        if urllib.parse.urlsplit(self.path).path == "/minilambda/state":
            self.reply(200, json_bytes(self.server.platform.state()), {"Content-Type": "application/json"})
            return
        self.user_error(404, "ResourceNotFoundException", f"unknown path {self.path}")

    def target_matches(self, name: str) -> bool:
        bare, qualifier = split_function_name(name)
        return bare in ("function", self.server.platform.config.name) and qualifier == "$LATEST"

    def do_POST(self) -> None:
        platform = self.server.platform
        match = INVOKE_PATH.match(urllib.parse.urlsplit(self.path).path)
        body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        if not match:
            self.user_error(404, "ResourceNotFoundException", f"unknown path {self.path}")
            return
        name = urllib.parse.unquote(match.group(1))
        if not self.target_matches(name):
            self.user_error(404, "ResourceNotFoundException", f"Function not found: {platform.config.arn if name in ('function', platform.config.name) else name}")
            return
        if body.strip():
            try:
                json.loads(body.decode("utf-8"))
            except (UnicodeDecodeError, ValueError) as exc:
                self.user_error(400, "InvalidRequestContentException", f"Could not parse request body into json: {exc}")
                return
        else:
            body = b"{}"
        client_context = ""
        if self.headers.get("X-Amz-Client-Context"):
            try:
                client_context = base64.b64decode(self.headers["X-Amz-Client-Context"]).decode("utf-8")
            except (ValueError, UnicodeDecodeError):
                self.user_error(400, "InvalidRequestContentException", "X-Amz-Client-Context must be base64-encoded JSON")
                return
        request_id = self.headers.get("X-Amzn-RequestId") or None
        trace_id = self.headers.get("X-Amzn-Trace-Id", "")
        invocation_type = self.headers.get("X-Amz-Invocation-Type", "RequestResponse")
        if invocation_type == "DryRun":
            self.reply(204, b"", {"X-Amzn-RequestId": request_id or str(uuid.uuid4())})
            return
        if invocation_type == "Event":
            event_id = request_id or str(uuid.uuid4())
            threading.Thread(target=platform.invoke, args=(body, event_id, trace_id, client_context), daemon=True).start()
            self.reply(202, b"", {"X-Amzn-RequestId": event_id})
            return
        try:
            result = platform.invoke(body, request_id, trace_id, client_context)
        except LambdaError as exc:
            self.user_error(503, "ServiceException", str(exc))
            return
        headers = dict(result.headers)
        if self.headers.get("X-Amz-Log-Type") == "Tail" and result.status == 200:
            headers["X-Amz-Log-Result"] = result.log_tail()
        self.reply(result.status, result.body, headers)


class FaultError(Exception):
    def __init__(self, error_type: str, message: str, trace: Optional[List[str]] = None) -> None:
        super().__init__(message)
        self.error_type = error_type
        self.message = message
        self.trace = trace or []


class RuntimeAPIError(Exception):
    def __init__(self, status: int, body: bytes) -> None:
        super().__init__(f"HTTP {status}: {body.decode('utf-8', 'replace')[:300]}")
        self.status = status


class RuntimeAPIClient:
    def __init__(self, address: str) -> None:
        self.base = f"http://{address}{RUNTIME_PREFIX}"
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def call(self, method: str, path: str, body: Optional[bytes] = None, headers: Optional[Dict[str, str]] = None) -> Tuple[int, Dict[str, str], bytes]:
        request = urllib.request.Request(self.base + path, data=body, headers=headers or {}, method=method)
        try:
            with self.opener.open(request) as response:
                return response.status, {k.lower(): v for k, v in response.headers.items()}, response.read()
        except urllib.error.HTTPError as exc:
            return exc.code, {k.lower(): v for k, v in (exc.headers or {}).items()}, exc.read()

    def next(self) -> Tuple[Dict[str, str], bytes]:
        status, headers, body = self.call("GET", "/invocation/next")
        if status != 200:
            raise RuntimeAPIError(status, body)
        return headers, body

    def respond(self, request_id: str, body: bytes, invocation_id: str = "") -> int:
        headers = {"Content-Type": "application/json"}
        if invocation_id:
            headers["Lambda-Runtime-Invocation-Id"] = invocation_id
        return self.call("POST", f"/invocation/{urllib.parse.quote(request_id)}/response", body, headers)[0]

    def error(self, request_id: str, payload: Dict[str, Any], invocation_id: str = "") -> int:
        headers = {"Content-Type": "application/json", "Lambda-Runtime-Function-Error-Type": str(payload.get("errorType", ""))}
        if invocation_id:
            headers["Lambda-Runtime-Invocation-Id"] = invocation_id
        return self.call("POST", f"/invocation/{urllib.parse.quote(request_id)}/error", json_bytes(payload), headers)[0]

    def init_error(self, payload: Dict[str, Any]) -> int:
        headers = {"Content-Type": "application/json", "Lambda-Runtime-Function-Error-Type": str(payload.get("errorType", ""))}
        return self.call("POST", "/init/error", json_bytes(payload), headers)[0]


class LambdaContext:
    def __init__(self, headers: Mapping[str, str], environ: Mapping[str, str], clock: Callable[[], float] = time.time) -> None:
        self.aws_request_id = headers.get("lambda-runtime-aws-request-id", "")
        self.invoked_function_arn = headers.get("lambda-runtime-invoked-function-arn", "")
        self.deadline_ms = int(headers.get("lambda-runtime-deadline-ms") or 0)
        self.function_name = environ.get("AWS_LAMBDA_FUNCTION_NAME", "")
        self.function_version = environ.get("AWS_LAMBDA_FUNCTION_VERSION", "$LATEST")
        self.memory_limit_in_mb = environ.get("AWS_LAMBDA_FUNCTION_MEMORY_SIZE", "")
        self.log_group_name = environ.get("AWS_LAMBDA_LOG_GROUP_NAME", "")
        self.log_stream_name = environ.get("AWS_LAMBDA_LOG_STREAM_NAME", "")
        raw_context = headers.get("lambda-runtime-client-context")
        self.client_context = json.loads(raw_context) if raw_context else None
        self.identity = None
        self.clock = clock

    def get_remaining_time_in_millis(self) -> int:
        return max(0, self.deadline_ms - int(self.clock() * 1000))


def load_handler(spec: str) -> Callable[[Any, Any], Any]:
    try:
        module_name, function_name = spec.rsplit(".", 1)
    except ValueError as exc:
        raise FaultError("Runtime.MalformedHandlerName", f"Bad handler '{spec}': {exc}")
    if module_name.split(".")[0] in sys.builtin_module_names:
        raise FaultError("Runtime.BuiltInModuleConflict", f"Cannot use built-in module {module_name} as a handler module")
    try:
        module = importlib.import_module(module_name.replace("/", "."))
    except ImportError as exc:
        raise FaultError("Runtime.ImportModuleError", f"Unable to import module '{module_name}': {exc}")
    except SyntaxError as exc:
        raise FaultError("Runtime.UserCodeSyntaxError", f"Syntax error in module '{module_name}': {exc}", [f'  File "{exc.filename}" Line {exc.lineno}\n    {exc.text}'])
    handler = getattr(module, function_name, None)
    if handler is None:
        raise FaultError("Runtime.HandlerNotFound", f"Handler '{function_name}' missing on module '{module_name}'")
    return handler


def error_payload(exc: BaseException, request_id: str = "") -> Dict[str, Any]:
    if isinstance(exc, FaultError):
        return {"errorMessage": exc.message, "errorType": exc.error_type, "requestId": request_id, "stackTrace": exc.trace}
    frames = traceback.extract_tb(exc.__traceback__)
    while frames and os.path.abspath(frames[0].filename) == THIS_FILE:
        frames.pop(0)
    return {"errorMessage": str(exc), "errorType": type(exc).__name__, "requestId": request_id, "stackTrace": traceback.format_list(frames)}


class RequestIdFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.aws_request_id = CURRENT_REQUEST["id"]
        return True


class JsonLogFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        entry: Dict[str, Any] = {"timestamp": datetime.fromtimestamp(record.created, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), "level": record.levelname, "message": record.getMessage(), "logger": record.name, "requestId": getattr(record, "aws_request_id", "")}
        if record.exc_info and record.exc_info[0] is not None:
            entry["errorType"] = record.exc_info[0].__name__
            entry["stackTrace"] = traceback.format_exception(*record.exc_info)
        return json.dumps(entry)


def configure_logging(log_format: str, log_level: str, stream: IO[str]) -> logging.Handler:
    handler = logging.StreamHandler(stream)
    if log_format == "JSON":
        handler.setFormatter(JsonLogFormatter())
    else:
        formatter = logging.Formatter("[%(levelname)s]\t%(asctime)s.%(msecs)03dZ\t%(aws_request_id)s\t%(message)s", "%Y-%m-%dT%H:%M:%S")
        formatter.converter = time.gmtime
        handler.setFormatter(formatter)
    handler.addFilter(RequestIdFilter())
    root = logging.getLogger()
    root.addHandler(handler)
    level = log_level.upper() if log_level else ("INFO" if log_format == "JSON" else "")
    if level in ("TRACE", "DEBUG", "INFO", "WARN", "WARNING", "ERROR", "FATAL", "CRITICAL"):
        root.setLevel({"TRACE": "DEBUG", "WARN": "WARNING", "FATAL": "CRITICAL"}.get(level, level))
    return handler


def format_error_log(payload: Dict[str, Any], log_format: str) -> str:
    if log_format == "JSON":
        return json.dumps({"timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), "level": "ERROR", **payload})
    lines = [f"[ERROR] {payload['errorType']}: {payload['errorMessage']}" if payload.get("errorMessage") else f"[ERROR] {payload['errorType']}"]
    if payload.get("stackTrace") is not None:
        lines.append("Traceback (most recent call last):")
        lines.extend(part.rstrip("\n") for part in payload["stackTrace"])
    return "\n".join(lines)


def handle_invocation(client: RuntimeAPIClient, handler: Callable[[Any, Any], Any], headers: Mapping[str, str], body: bytes, environ: Mapping[str, str], out: IO[str], clock: Callable[[], float] = time.time) -> Tuple[str, Dict[str, Any]]:
    request_id = headers.get("lambda-runtime-aws-request-id", "")
    invocation_id = headers.get("lambda-runtime-invocation-id", "")
    CURRENT_REQUEST["id"] = request_id
    if headers.get("lambda-runtime-trace-id"):
        os.environ["_X_AMZN_TRACE_ID"] = headers["lambda-runtime-trace-id"]
    try:
        context = LambdaContext(headers, environ, clock)
        try:
            event = json.loads(body.decode("utf-8"))
        except ValueError as exc:
            raise FaultError("Runtime.UnmarshalError", f"Unable to unmarshal input: {exc}")
        result = handler(event, context)
        try:
            payload = json.dumps(result).encode("utf-8")
        except (TypeError, ValueError) as exc:
            raise FaultError("Runtime.MarshalError", f"Unable to marshal response: {exc}")
    except Exception as exc:
        error = error_payload(exc, request_id)
        out.write(format_error_log(error, environ.get("AWS_LAMBDA_LOG_FORMAT", "Text")) + "\n")
        out.flush()
        client.error(request_id, error, invocation_id)
        return "error", error
    status = client.respond(request_id, payload, invocation_id)
    if status != 202:
        out.write(f"[ERROR] the Runtime API refused the response for {request_id} with HTTP {status}\n")
        out.flush()
        return "refused", {"status": status}
    return "success", {"bytes": len(payload)}


def run_runtime(client: RuntimeAPIClient, handler_spec: str, task_root: str, environ: Mapping[str, str], out: IO[str], max_invocations: Optional[int] = None) -> int:
    log_format = environ.get("AWS_LAMBDA_LOG_FORMAT", "Text")
    log_handler = configure_logging(log_format, environ.get("AWS_LAMBDA_LOG_LEVEL", ""), out)
    if task_root not in sys.path:
        sys.path.insert(0, task_root)
    try:
        try:
            handler = load_handler(handler_spec)
        except Exception as exc:
            payload = error_payload(exc, "")
            out.write(format_error_log(payload, log_format) + "\n")
            out.flush()
            client.init_error(payload)
            return 1
        served = 0
        while max_invocations is None or served < max_invocations:
            try:
                headers, body = client.next()
            except (RuntimeAPIError, OSError) as exc:
                out.write(f"bootstrap: cannot fetch the next invocation, exiting: {exc}\n")
                out.flush()
                return 1
            handle_invocation(client, handler, headers, body, environ, out)
            served += 1
        return 0
    finally:
        logging.getLogger().removeHandler(log_handler)


def parse_env_pairs(pairs: Sequence[str]) -> Dict[str, str]:
    values: Dict[str, str] = {}
    for pair in pairs:
        key, sep, value = pair.partition("=")
        if not sep or not key:
            raise LambdaError(f"--env needs KEY=VALUE, got {pair!r}")
        values[key] = value
    return values


def config_from(args: argparse.Namespace) -> FunctionConfig:
    if args.timeout <= 0 or args.timeout > 900:
        raise LambdaError(f"--timeout must be between 0 and 900 seconds, got {args.timeout}")
    if not 128 <= args.memory <= 10240:
        raise LambdaError(f"--memory must be between 128 and 10240 MB, got {args.memory}")
    if args.reserved_concurrency is not None and args.reserved_concurrency < 0:
        raise LambdaError("--reserved-concurrency must be 0 or more (0 throttles every invoke)")
    if not os.path.isdir(args.task_root):
        raise LambdaError(f"--task-root {args.task_root} is not a directory")
    command = shlex.split(args.runtime) if args.runtime else [sys.executable, THIS_FILE, "bootstrap"]
    return FunctionConfig(
        handler=args.handler,
        task_root=os.path.abspath(args.task_root),
        command=command,
        name=args.name,
        timeout=args.timeout,
        memory_mb=args.memory,
        reserved_concurrency=args.reserved_concurrency,
        account_concurrency=args.account_concurrency,
        init_timeout=args.init_timeout,
        idle_ttl=args.idle_ttl,
        log_format=args.log_format,
        freeze=not args.no_freeze,
        environment=parse_env_pairs(args.env),
        runtime_api_port=args.runtime_api_port,
    )


def stop_on_sigterm(signum: int, frame: Any) -> None:
    raise KeyboardInterrupt


def cmd_serve(args: argparse.Namespace) -> int:
    config = config_from(args)
    sink = open(args.log_file, "a", encoding="utf-8") if args.log_file else sys.stderr
    platform = Platform(config, sink)
    server = InvokeServer((args.host, args.port), platform)
    if threading.current_thread() is threading.main_thread():
        signal.signal(signal.SIGTERM, stop_on_sigterm)
    try:
        if args.provisioned:
            platform.prewarm(args.provisioned)
        port = server.server_address[1]
        if args.port_file:
            with open(args.port_file + ".tmp", "w", encoding="utf-8") as handle:
                handle.write(f"{port}\n")
            os.replace(args.port_file + ".tmp", args.port_file)
        reserved = "unreserved" if config.reserved_concurrency is None else f"reserved concurrency {config.reserved_concurrency}"
        print(f"minilambda: {config.arn} on http://{args.host}:{port}/2015-03-31/functions/function/invocations (handler {config.handler}, timeout {config.timeout:g}s, {config.memory_mb} MB, {reserved}, provisioned {args.provisioned})", flush=True)
        server.serve_forever(poll_interval=0.1)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        platform.close()
        if sink is not sys.stderr:
            sink.close()
    return 0


def http_invoke(url: str, payload: bytes, headers: Dict[str, str], timeout: float) -> Tuple[int, Dict[str, str], bytes]:
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    request = urllib.request.Request(url, data=payload, headers=headers, method="POST")
    try:
        with opener.open(request, timeout=timeout) as response:
            return response.status, dict(response.headers.items()), response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, dict((exc.headers or {}).items()), exc.read()


def decode_body(body: bytes) -> Any:
    try:
        return json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return body.decode("utf-8", "replace")


def cmd_invoke(args: argparse.Namespace) -> int:
    if args.payload_file:
        with open(args.payload_file, "rb") as handle:
            payload = handle.read()
    else:
        payload = args.payload.encode("utf-8")
    headers = {"Content-Type": "application/json"}
    if args.log_tail:
        headers["X-Amz-Log-Type"] = "Tail"
    if args.request_id:
        headers["X-Amzn-RequestId"] = args.request_id
    url = args.url.rstrip("/") + f"/2015-03-31/functions/{urllib.parse.quote(args.function)}/invocations"
    try:
        status, response_headers, body = http_invoke(url, payload, headers, args.timeout)
    except OSError as exc:
        raise LambdaError(f"cannot reach {url}: {getattr(exc, 'reason', exc)}")
    lowered = {k.lower(): v for k, v in response_headers.items()}
    function_error = lowered.get("x-amz-function-error", "")
    log = base64.b64decode(lowered["x-amz-log-result"]).decode("utf-8", "replace") if lowered.get("x-amz-log-result") else ""
    if args.json:
        print(json.dumps({"status": status, "function_error": function_error, "request_id": lowered.get("x-amzn-requestid", ""), "cold": lowered.get("x-minilambda-cold"), "environment": lowered.get("x-minilambda-environment"), "error_type": lowered.get("x-amzn-errortype", ""), "body": decode_body(body), "log": log}, indent=2))
    else:
        sys.stdout.write(body.decode("utf-8", "replace") + "\n")
        print(f"HTTP {status}" + (f", function error {function_error}" if function_error else "") + (f", {lowered['x-amzn-errortype']}" if lowered.get("x-amzn-errortype") else ""), file=sys.stderr)
        if log:
            sys.stderr.write(log)
    return 0 if status == 200 and not function_error else 1


def render_summary(summary: Dict[str, Any]) -> str:
    def show(value: Optional[float]) -> str:
        return "-" if value is None else f"{value:.2f}"
    lines = [
        f"invocations       {summary['invocations']}",
        f"cold starts       {summary['cold_starts']} ({summary['cold_start_rate'] * 100:.1f}%)",
        f"init ms           p50 {show(summary['init_ms']['p50'])}  p95 {show(summary['init_ms']['p95'])}  max {show(summary['init_ms']['max'])}",
        f"duration ms       p50 {show(summary['duration_ms']['p50'])}  p95 {show(summary['duration_ms']['p95'])}  max {show(summary['duration_ms']['max'])}",
        f"billed ms total   {summary['billed_ms_total']} (init share {summary['init_share_of_billed'] * 100:.1f}%)",
        f"memory MB         max used {show(summary['max_memory_mb'])} of {show(summary['memory_mb'])}",
        f"statuses          " + (", ".join(f"{k} {v}" for k, v in sorted(summary["statuses"].items())) or "-"),
        f"init failures     " + (", ".join(f"{k} x{v}" for k, v in sorted(summary["init_failures"].items())) or "-"),
    ]
    return "\n".join(lines)


def cmd_report(args: argparse.Namespace) -> int:
    entries: List[Dict[str, Any]] = []
    for path in args.files:
        handle = sys.stdin if path == "-" else open(path, encoding="utf-8", errors="replace")
        try:
            for line in handle:
                entry = parse_log_line(line)
                if entry is not None:
                    entries.append(entry)
        finally:
            if handle is not sys.stdin:
                handle.close()
    if not entries:
        print("minilambda: no REPORT or INIT_REPORT lines found", file=sys.stderr)
        return 1
    summary = summarize(entries)
    print(json.dumps(summary, indent=2) if args.json else render_summary(summary))
    return 0


def cmd_bootstrap(args: argparse.Namespace) -> int:
    address = os.environ.get("AWS_LAMBDA_RUNTIME_API")
    if not address:
        print("bootstrap: AWS_LAMBDA_RUNTIME_API is not set; this command is started by `minilambda serve` or by aws-lambda-rie, not by hand", file=sys.stderr)
        return 2
    handler = args.handler or os.environ.get("_HANDLER", "")
    if not handler:
        print("bootstrap: no handler: pass MODULE.FUNCTION or set _HANDLER", file=sys.stderr)
        return 2
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(line_buffering=True)
    task_root = os.environ.get("LAMBDA_TASK_ROOT") or os.getcwd()
    return run_runtime(RuntimeAPIClient(address), handler, task_root, os.environ, sys.stdout, args.max_invocations)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="minilambda", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    serve = sub.add_parser("serve", help="run the platform: the Invoke API plus one Runtime API per execution environment")
    serve.add_argument("--handler", required=True, help="MODULE.FUNCTION, passed to the runtime as _HANDLER")
    serve.add_argument("--task-root", required=True, help="directory with the function code (LAMBDA_TASK_ROOT)")
    serve.add_argument("--runtime", help="bootstrap command (default: this file's bootstrap subcommand)")
    serve.add_argument("--name", default="minilambda", help="function name")
    serve.add_argument("--timeout", type=float, default=3.0, help="seconds per invocation (Lambda's default is 3)")
    serve.add_argument("--memory", type=int, default=128, help="memory size in MB (reported, not enforced)")
    serve.add_argument("--reserved-concurrency", type=int, help="maximum concurrent invocations; 0 throttles everything")
    serve.add_argument("--account-concurrency", type=int, default=10, help="the account's unreserved pool")
    serve.add_argument("--provisioned", type=int, default=0, help="environments to initialise before the first invoke")
    serve.add_argument("--init-timeout", type=float, default=10.0, help="init phase limit before the retry inside the invoke")
    serve.add_argument("--idle-ttl", type=float, help="destroy environments idle this many seconds")
    serve.add_argument("--log-format", choices=["Text", "JSON"], default="Text")
    serve.add_argument("--log-file", help="append platform and function logs here (default: stderr)")
    serve.add_argument("--no-freeze", action="store_true", help="do not SIGSTOP idle environments")
    serve.add_argument("--env", action="append", default=[], help="KEY=VALUE for the function's environment (repeatable)")
    serve.add_argument("--runtime-api-port", type=int, default=0, help="fixed port for the first environment's Runtime API (to be the runtime yourself with curl)")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=9000, help="0 picks a free port")
    serve.add_argument("--port-file", help="write the bound port here once listening")

    invoke = sub.add_parser("invoke", help="call the Invoke API of minilambda or of the Runtime Interface Emulator")
    invoke.add_argument("--url", default="http://127.0.0.1:9000")
    invoke.add_argument("--function", default="function", help="function name in the path (the RIE only knows 'function')")
    invoke.add_argument("--payload", default="{}", help="JSON event")
    invoke.add_argument("--payload-file", help="read the event from a file")
    invoke.add_argument("--log-tail", action="store_true", help="ask for the last 4 KB of the invocation's log (X-Amz-Log-Type: Tail)")
    invoke.add_argument("--request-id", help="request ID to use (X-Amzn-RequestId)")
    invoke.add_argument("--timeout", type=float, default=930.0, help="client-side read timeout in seconds")
    invoke.add_argument("--json", action="store_true", help="machine-readable output")

    report = sub.add_parser("report", help="summarise REPORT and INIT_REPORT lines from minilambda, the RIE or CloudWatch Logs")
    report.add_argument("files", nargs="+", help="log files, or - for stdin")
    report.add_argument("--json", action="store_true")

    bootstrap = sub.add_parser("bootstrap", help="the custom runtime: run a Python handler against the Runtime API in AWS_LAMBDA_RUNTIME_API")
    bootstrap.add_argument("handler", nargs="?", help="MODULE.FUNCTION (default: $_HANDLER)")
    bootstrap.add_argument("--max-invocations", type=int, help="exit after this many invocations")
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    try:
        args = parser.parse_args(list(sys.argv[1:] if argv is None else argv))
    except SystemExit as exc:
        return int(exc.code or 0)
    commands: Dict[str, Callable[[argparse.Namespace], int]] = {"serve": cmd_serve, "invoke": cmd_invoke, "report": cmd_report, "bootstrap": cmd_bootstrap}
    try:
        return commands[args.command](args)
    except LambdaError as exc:
        print(f"minilambda: {exc}", file=sys.stderr)
        return 2
    except OSError as exc:
        print(f"minilambda: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
