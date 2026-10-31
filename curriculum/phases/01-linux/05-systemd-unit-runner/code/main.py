"""miniunit: parse a subset of systemd unit files, order them by their dependencies, run them through the unit state machine with restart policy, stop timeouts and a journal, and show status like systemctl."""
from __future__ import annotations

import argparse
import json
import os
import queue
import re
import shlex
import shutil
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Dict, Iterable, List, Optional, Sequence, Set, Tuple

RESTART_POLICIES = ("no", "on-failure", "always")
SERVICE_TYPES = ("simple", "oneshot")
LIST_KEYS = {("Unit", "After"), ("Unit", "Requires"), ("Unit", "Wants"), ("Service", "ExecStartPre"), ("Service", "ExecStart"), ("Service", "ExecStopPost"), ("Service", "Environment"), ("Service", "EnvironmentFile")}
KNOWN_KEYS = {
    "Unit": {"Description", "After", "Requires", "Wants", "StartLimitBurst", "StartLimitIntervalSec", "Documentation"},
    "Service": {"Type", "ExecStartPre", "ExecStart", "ExecStopPost", "Restart", "RestartSec", "TimeoutStopSec", "Environment", "EnvironmentFile", "WorkingDirectory", "RemainAfterExit"},
    "Install": {"WantedBy", "RequiredBy", "Alias"},
}
TIME_UNITS = {"us": 1e-6, "usec": 1e-6, "ms": 1e-3, "msec": 1e-3, "s": 1.0, "sec": 1.0, "second": 1.0, "seconds": 1.0, "m": 60.0, "min": 60.0, "minute": 60.0, "minutes": 60.0, "h": 3600.0, "hr": 3600.0, "hour": 3600.0, "hours": 3600.0, "d": 86400.0, "day": 86400.0, "days": 86400.0}
TIME_SPAN = re.compile(r"\s*(\d+(?:\.\d+)?)\s*([a-z]*)")
CLEAN_SIGNALS = {int(signal.SIGHUP), int(signal.SIGINT), int(signal.SIGTERM), int(signal.SIGPIPE)}
EXIT_EXEC = 203
DEFAULT_PATH = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
GLYPHS = {"active": "●", "inactive": "○", "failed": "×", "activating": "↻", "deactivating": "↻"}


class UnitError(Exception):
    pass


@dataclass
class Exec:
    argv: List[str]
    ignore_failure: bool = False

    def render(self) -> str:
        return ("-" if self.ignore_failure else "") + " ".join(shlex.quote(a) for a in self.argv)


@dataclass
class Unit:
    name: str
    path: str = ""
    description: str = ""
    after: List[str] = field(default_factory=list)
    requires: List[str] = field(default_factory=list)
    wants: List[str] = field(default_factory=list)
    service_type: str = "simple"
    exec_start_pre: List[Exec] = field(default_factory=list)
    exec_start: List[Exec] = field(default_factory=list)
    exec_stop_post: List[Exec] = field(default_factory=list)
    restart: str = "no"
    restart_sec: float = 0.1
    timeout_stop_sec: Optional[float] = 90.0
    start_limit_burst: int = 5
    start_limit_interval: Optional[float] = 10.0
    environment: Dict[str, str] = field(default_factory=dict)
    environment_files: List[Tuple[str, bool]] = field(default_factory=list)
    working_directory: Optional[str] = None
    remain_after_exit: bool = False
    warnings: List[str] = field(default_factory=list)


def parse_time_span(text: str) -> Optional[float]:
    text = text.strip()
    if text == "infinity":
        return None
    if not text:
        raise UnitError("empty time span")
    total = 0.0
    position = 0
    while position < len(text):
        match = TIME_SPAN.match(text, position)
        if not match:
            raise UnitError(f"invalid time span {text!r}")
        unit = match.group(2) or "s"
        if unit not in TIME_UNITS:
            raise UnitError(f"invalid time span {text!r}: unknown unit {unit!r}")
        total += float(match.group(1)) * TIME_UNITS[unit]
        position = match.end()
    return total


def parse_exec(value: str) -> Exec:
    ignore = value.startswith("-")
    if ignore:
        value = value[1:]
    if value and value[0] in "@:+!":
        raise UnitError(f"Exec prefix {value[0]!r} is not supported here (only '-')")
    argv = shlex.split(value)
    if not argv:
        raise UnitError("empty Exec line")
    return Exec(argv, ignore)


def parse_bool(value: str) -> bool:
    lowered = value.strip().lower()
    if lowered in ("yes", "true", "on", "1"):
        return True
    if lowered in ("no", "false", "off", "0"):
        return False
    raise UnitError(f"not a boolean: {value!r}")


def parse_unit_text(text: str, name: str, path: str = "") -> Unit:
    sections: Dict[str, Dict[str, List[str]]] = {}
    warnings: List[str] = []
    section = ""
    pending = ""
    for number, raw in enumerate(text.splitlines(), 1):
        line = pending + raw
        pending = ""
        stripped = line.strip()
        if not stripped or stripped[0] in "#;":
            continue
        if stripped.endswith("\\"):
            pending = stripped[:-1] + " "
            continue
        if stripped.startswith("[") and stripped.endswith("]"):
            section = stripped[1:-1]
            if section not in KNOWN_KEYS:
                warnings.append(f"line {number}: unknown section [{section}] ignored")
            sections.setdefault(section, {})
            continue
        if "=" not in stripped:
            raise UnitError(f"{name} line {number}: expected Key=Value, got {stripped!r}")
        if not section:
            raise UnitError(f"{name} line {number}: assignment outside of a section")
        key, _, value = stripped.partition("=")
        key, value = key.strip(), value.strip()
        if section in KNOWN_KEYS and key not in KNOWN_KEYS[section]:
            warnings.append(f"line {number}: unknown key {key}= in [{section}] ignored")
            continue
        values = sections.setdefault(section, {}).setdefault(key, [])
        if (section, key) in LIST_KEYS:
            if value == "":
                values.clear()
            else:
                values.append(value)
        else:
            values[:] = [value]
    if pending:
        raise UnitError(f"{name}: file ends inside a line continuation")
    unit = Unit(name=name, path=path, warnings=warnings)
    unit_section = sections.get("Unit", {})
    service = sections.get("Service", {})
    unit.description = unit_section.get("Description", [""])[0]
    for key, target in (("After", unit.after), ("Requires", unit.requires), ("Wants", unit.wants)):
        for value in unit_section.get(key, []):
            target.extend(value.split())
    if "StartLimitBurst" in unit_section:
        unit.start_limit_burst = int(unit_section["StartLimitBurst"][0])
    if "StartLimitIntervalSec" in unit_section:
        unit.start_limit_interval = parse_time_span(unit_section["StartLimitIntervalSec"][0])
    unit.service_type = service.get("Type", ["simple"])[0]
    if unit.service_type not in SERVICE_TYPES:
        raise UnitError(f"{name}: Type={unit.service_type} is not supported (use {' or '.join(SERVICE_TYPES)})")
    unit.exec_start_pre = [parse_exec(v) for v in service.get("ExecStartPre", [])]
    unit.exec_start = [parse_exec(v) for v in service.get("ExecStart", [])]
    unit.exec_stop_post = [parse_exec(v) for v in service.get("ExecStopPost", [])]
    unit.restart = service.get("Restart", ["no"])[0]
    if unit.restart not in RESTART_POLICIES:
        raise UnitError(f"{name}: Restart={unit.restart} is not supported (use {', '.join(RESTART_POLICIES)})")
    if "RestartSec" in service:
        unit.restart_sec = parse_time_span(service["RestartSec"][0]) or 0.0
    if "TimeoutStopSec" in service:
        unit.timeout_stop_sec = parse_time_span(service["TimeoutStopSec"][0])
    for value in service.get("Environment", []):
        for assignment in shlex.split(value):
            key, eq, val = assignment.partition("=")
            if not eq or not key:
                raise UnitError(f"{name}: Environment entry {assignment!r} is not KEY=VALUE")
            unit.environment[key] = val
    for value in service.get("EnvironmentFile", []):
        optional = value.startswith("-")
        unit.environment_files.append((value[1:] if optional else value, optional))
    if "WorkingDirectory" in service:
        unit.working_directory = service["WorkingDirectory"][0]
    if "RemainAfterExit" in service:
        unit.remain_after_exit = parse_bool(service["RemainAfterExit"][0])
    if not unit.exec_start:
        raise UnitError(f"{name}: [Service] needs ExecStart=")
    if unit.service_type == "simple" and len(unit.exec_start) > 1:
        raise UnitError(f"{name}: Type=simple allows exactly one ExecStart= (found {len(unit.exec_start)}); use Type=oneshot for a sequence")
    if unit.service_type == "oneshot" and unit.restart == "always":
        raise UnitError(f"{name}: Restart=always is not allowed with Type=oneshot (a oneshot that exits 0 is finished); use on-failure")
    if unit.remain_after_exit and unit.service_type != "oneshot":
        unit.warnings.append("RemainAfterExit= only matters for Type=oneshot")
    return unit


def parse_environment_file(text: str) -> Dict[str, str]:
    env: Dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line[0] in "#;":
            continue
        if line.startswith("export "):
            line = line[len("export "):]
        for assignment in shlex.split(line):
            key, eq, value = assignment.partition("=")
            if eq and key:
                env[key] = value
    return env


def unit_environment(unit: Unit, read_file: Callable[[str], str]) -> Dict[str, str]:
    env = {"PATH": DEFAULT_PATH, "LANG": os.environ.get("LANG", "C.UTF-8")}
    for path, optional in unit.environment_files:
        try:
            env.update(parse_environment_file(read_file(path)))
        except OSError as exc:
            if not optional:
                raise UnitError(f"{unit.name}: EnvironmentFile={path}: {exc.strerror or exc}")
    env.update(unit.environment)
    return env


def read_text(path: str) -> str:
    with open(path, encoding="utf-8") as handle:
        return handle.read()


def load_units(directory: str) -> Dict[str, Unit]:
    if not os.path.isdir(directory):
        raise UnitError(f"units directory {directory} not found")
    units: Dict[str, Unit] = {}
    for name in sorted(os.listdir(directory)):
        if name.endswith(".service"):
            path = os.path.join(directory, name)
            units[name] = parse_unit_text(read_text(path), name, path)
    if not units:
        raise UnitError(f"no *.service files in {directory}")
    return units


def transaction(units: Dict[str, Unit], names: Iterable[str]) -> List[str]:
    selected: List[str] = []
    queued = list(names)
    while queued:
        name = queued.pop(0)
        if name in selected:
            continue
        if name not in units:
            raise UnitError(f"Unit {name} not found")
        selected.append(name)
        unit = units[name]
        for dep in unit.requires:
            if dep not in units:
                raise UnitError(f"{name}: Requires={dep} but {dep} does not exist")
            queued.append(dep)
        for dep in unit.wants:
            if dep in units:
                queued.append(dep)
    return selected


def find_cycle(edges: Dict[str, Set[str]]) -> List[str]:
    state: Dict[str, int] = {}
    stack: List[str] = []

    def visit(node: str) -> Optional[List[str]]:
        state[node] = 1
        stack.append(node)
        for nxt in sorted(edges.get(node, ())):
            if state.get(nxt) == 1:
                return stack[stack.index(nxt):] + [nxt]
            if nxt not in state:
                found = visit(nxt)
                if found:
                    return found
        stack.pop()
        state[node] = 2
        return None

    for start in sorted(edges):
        if start not in state:
            found = visit(start)
            if found:
                return found
    return []


def start_order(units: Dict[str, Unit], names: Sequence[str]) -> List[str]:
    chosen = set(names)
    before: Dict[str, Set[str]] = {n: set() for n in names}
    for name in names:
        for dep in units[name].after:
            if dep in chosen:
                before[name].add(dep)
    order: List[str] = []
    remaining = dict(before)
    while remaining:
        ready = sorted(n for n, deps in remaining.items() if not deps)
        if not ready:
            cycle = find_cycle({n: {d for d in deps} for n, deps in remaining.items()})
            raise UnitError("ordering cycle: " + " -> ".join(cycle))
        for name in ready:
            order.append(name)
            del remaining[name]
        for deps in remaining.values():
            deps.difference_update(ready)
    return order


def dependents(units: Dict[str, Unit], name: str) -> List[str]:
    return sorted(n for n, u in units.items() if name in u.requires)


def is_clean_exit(code: int) -> bool:
    return code == 0 or (code < 0 and -code in CLEAN_SIGNALS)


def describe_exit(code: int) -> str:
    if code < 0:
        try:
            label = signal.Signals(-code).name[3:]
        except ValueError:
            label = str(-code)
        return f"code=killed, signal={-code}/{label}"
    return f"code=exited, status={code}/{'SUCCESS' if code == 0 else 'EXEC' if code == EXIT_EXEC else 'FAILURE'}"


@dataclass
class ExecResult:
    step: str
    argv: List[str]
    pid: int
    code: int


@dataclass
class JournalEntry:
    at: float
    unit: str
    pid: int
    stream: str
    line: str


class Journal:
    def __init__(self, sink: Optional[Callable[[JournalEntry], None]] = None) -> None:
        self.entries: List[JournalEntry] = []
        self.sink = sink

    def record(self, unit: str, pid: int, stream: str, line: str, at: float) -> JournalEntry:
        entry = JournalEntry(at, unit, pid, stream, line)
        self.entries.append(entry)
        if self.sink:
            self.sink(entry)
        return entry

    def for_unit(self, unit: Optional[str], last: int = 0) -> List[JournalEntry]:
        entries = [e for e in self.entries if unit is None or e.unit == unit]
        return entries[-last:] if last else entries


def format_journal_line(entry: JournalEntry) -> str:
    stamp = time.strftime("%b %d %H:%M:%S", time.localtime(entry.at))
    return f"{stamp} {entry.unit}[{entry.pid}]: {entry.line}"


@dataclass
class UnitState:
    unit: Unit
    active: str = "inactive"
    sub: str = "dead"
    result: str = "success"
    job: Optional[str] = None
    main_pid: Optional[int] = None
    control_pid: Optional[int] = None
    restarts: int = 0
    since: Optional[float] = None
    steps: List[Tuple[str, Exec]] = field(default_factory=list)
    current: Optional[Tuple[str, Exec]] = None
    execs: List[ExecResult] = field(default_factory=list)
    start_times: List[float] = field(default_factory=list)
    restart_at: Optional[float] = None
    stop_deadline: Optional[float] = None
    stop_requested: bool = False

    @property
    def name(self) -> str:
        return self.unit.name

    def settled(self) -> bool:
        return self.active in ("inactive", "failed") and self.job is None


class Manager:
    def __init__(
        self,
        units: Dict[str, Unit],
        spawn: Callable[[str, List[str], Dict[str, str], Optional[str]], int],
        kill: Callable[[int, int], None],
        now: Callable[[], float] = time.time,
        journal: Optional[Journal] = None,
        read_file: Callable[[str], str] = read_text,
        log: Callable[[str], None] = lambda line: None,
    ) -> None:
        self.units = units
        self.spawn = spawn
        self.kill = kill
        self.now = now
        self.journal = journal or Journal()
        self.read_file = read_file
        self.log = log
        self.states: Dict[str, UnitState] = {name: UnitState(unit) for name, unit in units.items()}
        self.changed = False

    def _note(self, line: str) -> None:
        self.log(line)
        self.journal.record("miniunit", 0, "manager", line, self.now())
        self.changed = True

    def start(self, names: Sequence[str]) -> List[str]:
        selected = transaction(self.units, names)
        order = start_order(self.units, selected)
        for name in order:
            state = self.states[name]
            if state.active in ("inactive", "failed") and state.job is None:
                state.job = "start"
                self.changed = True
        return order

    def stop(self, names: Sequence[str]) -> List[str]:
        stopping: List[str] = []
        queued = list(names)
        while queued:
            name = queued.pop(0)
            if name in stopping:
                continue
            if name not in self.states:
                raise UnitError(f"Unit {name} not found")
            stopping.append(name)
            queued.extend(dependents(self.units, name))
        for name in stopping:
            state = self.states[name]
            if state.settled():
                continue
            if state.job == "start":
                state.job = None
                state.active, state.sub = "inactive", "dead"
                self._note(f"Cancelled start job for {name}")
                continue
            state.job = "stop"
            self.changed = True
        return stopping

    def stop_all(self) -> List[str]:
        return self.stop([n for n, s in self.states.items() if not s.settled()])

    def apply_control(self, lines: Iterable[str]) -> None:
        for raw in lines:
            if not raw.strip():
                continue
            command = json.loads(raw)
            names = command.get("units") or list(self.states)
            if command.get("action") == "stop":
                self.stop(names)
            elif command.get("action") == "start":
                self.start(names)

    def busy(self) -> bool:
        return any(not s.settled() for s in self.states.values())

    def next_deadline(self) -> Optional[float]:
        deadlines = [s.restart_at for s in self.states.values() if s.restart_at is not None]
        deadlines += [s.stop_deadline for s in self.states.values() if s.stop_deadline is not None]
        return min(deadlines) if deadlines else None

    def tick(self) -> None:
        now = self.now()
        for state in self.states.values():
            if state.sub == "auto-restart" and state.restart_at is not None and state.restart_at <= now:
                state.restart_at = None
                self._begin_activation(state)
        for state in list(self.states.values()):
            if state.job == "stop":
                self._try_deactivate(state)
        for state in list(self.states.values()):
            if state.job == "start" and state.active in ("inactive", "failed"):
                self._try_activate(state)
        for state in self.states.values():
            if state.sub == "stop-sigterm" and state.stop_deadline is not None and state.stop_deadline <= now and state.main_pid is not None:
                self._note(f"{state.name}: State 'stop-sigterm' timed out. Killing.")
                self.kill(state.main_pid, int(signal.SIGKILL))
                state.sub = "stop-sigkill"
                state.result = "timeout"
                state.stop_deadline = None

    def _try_activate(self, state: UnitState) -> None:
        for dep in state.unit.after:
            dep_state = self.states.get(dep)
            if dep_state is None:
                continue
            if dep_state.job == "start" or dep_state.active in ("activating",):
                return
            dep_failed = dep_state.active == "failed" or (dep_state.active == "inactive" and dep_state.result != "success")
            if dep_failed and dep in state.unit.requires:
                state.job = None
                state.active, state.sub, state.result = "inactive", "dead", "dependency"
                self._note(f"Dependency failed for {state.name}: {dep} failed to start")
                return
        state.job = None
        self._begin_activation(state)

    def _rate_limited(self, state: UnitState) -> bool:
        now = self.now()
        interval = state.unit.start_limit_interval
        if interval is None:
            state.start_times.append(now)
            return len(state.start_times) > state.unit.start_limit_burst
        state.start_times = [t for t in state.start_times if now - t < interval]
        state.start_times.append(now)
        return len(state.start_times) > state.unit.start_limit_burst

    def _begin_activation(self, state: UnitState) -> None:
        unit = state.unit
        if self._rate_limited(state):
            state.active, state.sub, state.result = "failed", "failed", "start-limit-hit"
            state.restart_at = None
            state.since = self.now()
            self._note(f"{state.name}: Start request repeated too quickly ({unit.start_limit_burst} starts within {unit.start_limit_interval}s)")
            self._note(f"Failed to start {state.name}: start-limit-hit")
            return
        state.result = "success"
        state.execs = []
        state.main_pid = state.control_pid = None
        state.stop_requested = False
        state.since = self.now()
        state.steps = [("start-pre", e) for e in unit.exec_start_pre] + [("start", e) for e in unit.exec_start]
        state.active = "activating"
        self._note(f"Starting {state.name}" + (f" - {unit.description}" if unit.description else ""))
        self._advance(state)

    def _advance(self, state: UnitState) -> None:
        if not state.steps:
            self._activation_complete(state)
            return
        step, command = state.steps.pop(0)
        state.sub = step
        state.current = (step, command)
        pid = self._spawn_step(state, step, command)
        if pid is None:
            return
        if step == "start" and state.unit.service_type == "simple":
            state.main_pid = pid
            state.active, state.sub = "active", "running"
            self._note(f"Started {state.name}")
        elif step == "start":
            state.main_pid = pid
        else:
            state.control_pid = pid

    def _spawn_step(self, state: UnitState, step: str, command: Exec, extra_env: Optional[Dict[str, str]] = None) -> Optional[int]:
        try:
            env = unit_environment(state.unit, self.read_file)
            env.update(extra_env or {})
            pid = self.spawn(state.name, command.argv, env, state.unit.working_directory)
        except (OSError, UnitError) as exc:
            self.journal.record(state.name, 0, "stderr", f"Failed to execute {command.argv[0]}: {exc}", self.now())
            state.execs.append(ExecResult(step, command.argv, 0, EXIT_EXEC))
            self._step_exited(state, step, command, EXIT_EXEC)
            return None
        self.changed = True
        return pid

    def _activation_complete(self, state: UnitState) -> None:
        if state.unit.remain_after_exit:
            state.active, state.sub = "active", "exited"
            self._note(f"Finished {state.name}")
        else:
            self._note(f"Finished {state.name}")
            self._begin_stop_post(state)

    def handle_exit(self, pid: int, code: int) -> None:
        for state in self.states.values():
            if state.control_pid == pid:
                state.control_pid = None
                step, command = state.current if state.current else (state.sub, Exec(["?"]))
                state.execs.append(ExecResult(step, command.argv, pid, code))
                self._step_exited(state, step, command, code)
                return
            if state.main_pid == pid:
                state.main_pid = None
                command = state.current[1] if state.current else state.unit.exec_start[0]
                state.execs.append(ExecResult("start", command.argv, pid, code))
                self._main_exited(state, code)
                return
        self.journal.record("miniunit", pid, "manager", f"reaped unknown pid {pid} ({describe_exit(code)})", self.now())

    def _step_exited(self, state: UnitState, step: str, command: Exec, code: int) -> None:
        ignore = command.ignore_failure
        if state.stop_requested and step in ("start-pre", "start"):
            state.result = "success" if is_clean_exit(code) else ("signal" if code < 0 else "exit-code")
            self._begin_stop_post(state)
            return
        if step in ("start-pre", "start"):
            if code == 0 or ignore:
                self._advance(state)
            else:
                state.result = "signal" if code < 0 else "exit-code"
                self._note(f"{state.name}: Control process exited, {describe_exit(code)}")
                self._note(f"Failed to start {state.name}: {state.result}")
                self._begin_stop_post(state)
        elif step == "stop-post":
            if code != 0 and not ignore and state.result == "success":
                state.result = "exit-code"
            self._run_next_stop_post(state)
        else:
            self.journal.record("miniunit", 0, "manager", f"unexpected step {step} for {state.name} ({' '.join(command.argv)})", self.now())

    def _main_exited(self, state: UnitState, code: int) -> None:
        if state.active == "activating" and state.sub == "start":
            if state.stop_requested:
                state.result = "success" if is_clean_exit(code) else ("signal" if code < 0 else "exit-code")
                self._begin_stop_post(state)
            elif code == 0:
                self._advance(state)
            else:
                state.result = "signal" if code < 0 else "exit-code"
                self._note(f"{state.name}: Main process exited, {describe_exit(code)}")
                self._note(f"Failed to start {state.name}: {state.result}")
                self._begin_stop_post(state)
            return
        if state.result == "success" and not is_clean_exit(code):
            state.result = "signal" if code < 0 else "exit-code"
        self._note(f"{state.name}: Main process exited, {describe_exit(code)}")
        self._begin_stop_post(state)

    def _begin_stop_post(self, state: UnitState) -> None:
        state.active = "deactivating"
        state.stop_deadline = None
        state.steps = [("stop-post", e) for e in state.unit.exec_stop_post]
        self._run_next_stop_post(state)

    def _stop_post_environment(self, state: UnitState) -> Dict[str, str]:
        main = [e for e in state.execs if e.step == "start"]
        code = main[-1].code if main else 0
        status = str(code) if code >= 0 else describe_exit(code).rsplit("/", 1)[-1]
        return {"SERVICE_RESULT": state.result, "EXIT_CODE": "exited" if code >= 0 else "killed", "EXIT_STATUS": status}

    def _run_next_stop_post(self, state: UnitState) -> None:
        if state.steps:
            step, command = state.steps.pop(0)
            state.sub = step
            state.current = (step, command)
            pid = self._spawn_step(state, step, command, self._stop_post_environment(state))
            if pid is not None:
                state.control_pid = pid
            return
        self._settle(state)

    def _settle(self, state: UnitState) -> None:
        unit = state.unit
        failed = state.result != "success"
        stopped_by_hand = state.stop_requested or state.job == "stop"
        state.job = None
        state.stop_requested = False
        state.main_pid = state.control_pid = None
        state.current = None
        state.since = self.now()
        if stopped_by_hand:
            state.active = "failed" if failed else "inactive"
            state.sub = "failed" if failed else "dead"
            self._note(f"Stopped {state.name}" + (f" (Result: {state.result})" if failed else ""))
            return
        if unit.restart == "always" or (unit.restart == "on-failure" and failed):
            state.restarts += 1
            state.active, state.sub = "activating", "auto-restart"
            state.restart_at = self.now() + unit.restart_sec
            self._note(f"{state.name}: Scheduled restart job, restart counter is at {state.restarts}.")
            return
        state.active = "failed" if failed else "inactive"
        state.sub = "failed" if failed else "dead"
        if failed:
            self._note(f"{state.name}: Failed with result '{state.result}'.")

    def _try_deactivate(self, state: UnitState) -> None:
        for other in self.states.values():
            if other is state or other.settled() or not (other.job == "stop" or other.stop_requested):
                continue
            if state.name in other.unit.requires or state.name in other.unit.after:
                return
        state.job = None
        self._begin_deactivation(state)

    def _begin_deactivation(self, state: UnitState) -> None:
        if state.sub == "auto-restart":
            state.restart_at = None
            state.active, state.sub = "inactive", "dead"
            self._note(f"Stopped {state.name} (restart cancelled)")
            return
        if state.active == "active" and state.sub == "exited":
            state.stop_requested = True
            self._note(f"Stopping {state.name}")
            self._begin_stop_post(state)
            return
        state.stop_requested = True
        self._note(f"Stopping {state.name}")
        if state.main_pid is not None:
            self.kill(state.main_pid, int(signal.SIGTERM))
            state.active, state.sub = "deactivating", "stop-sigterm"
            state.stop_deadline = None if state.unit.timeout_stop_sec is None else self.now() + state.unit.timeout_stop_sec
        elif state.control_pid is not None:
            self.kill(state.control_pid, int(signal.SIGTERM))
            state.active = "deactivating"
        else:
            self._begin_stop_post(state)

    def snapshot(self) -> Dict[str, dict]:
        return {name: self.status_dict(name) for name in self.states}

    def status_dict(self, name: str) -> dict:
        state = self.states[name]
        return {
            "name": name,
            "description": state.unit.description,
            "loaded_from": state.unit.path,
            "active": state.active,
            "sub": state.sub,
            "result": state.result,
            "job": state.job,
            "since": state.since,
            "main_pid": state.main_pid,
            "restarts": state.restarts,
            "restart": state.unit.restart,
            "type": state.unit.service_type,
            "execs": [{"step": e.step, "argv": e.argv, "pid": e.pid, "code": e.code} for e in state.execs],
        }


def human_ago(seconds: float) -> str:
    seconds = int(max(0, seconds))
    if seconds < 60:
        return f"{seconds}s ago"
    if seconds < 3600:
        return f"{seconds // 60}min {seconds % 60}s ago"
    return f"{seconds // 3600}h {(seconds % 3600) // 60}min ago"


def render_status(info: dict, entries: Sequence[JournalEntry], now: float) -> str:
    glyph = GLYPHS.get(info["active"], "○")
    title = f"{glyph} {info['name']}" + (f" - {info['description']}" if info.get("description") else "")
    lines = [title, f"     Loaded: loaded ({info.get('loaded_from') or 'memory'}; Restart={info.get('restart', 'no')}; Type={info.get('type', 'simple')})"]
    active = f"{info['active']} ({info['sub']})"
    if info.get("since") is not None:
        stamp = time.strftime("%a %Y-%m-%d %H:%M:%S", time.localtime(info["since"]))
        active += f" since {stamp}; {human_ago(now - info['since'])}"
    if info["active"] in ("failed", "inactive") and info["result"] != "success":
        active += f" (Result: {info['result']})"
    lines.append(f"     Active: {active}")
    step_names = {"start-pre": "ExecStartPre", "start": "ExecStart", "stop-post": "ExecStopPost"}
    for result in info.get("execs", []):
        argv = " ".join(shlex.quote(a) for a in result["argv"])
        lines.append(f"    Process: {result['pid']} {step_names.get(result['step'], result['step'])}={argv} ({describe_exit(result['code'])})")
    if info.get("main_pid"):
        lines.append(f"   Main PID: {info['main_pid']}")
    lines.append(f"   Restarts: {info.get('restarts', 0)}")
    lines.append(f"     Result: {info['result']}")
    if entries:
        lines.append("")
        lines.extend(format_journal_line(e) for e in entries)
    return "\n".join(lines)


def render_graph(units: Dict[str, Unit], names: Sequence[str]) -> str:
    lines: List[str] = []
    for name in names:
        lines.append(name)
        deps = []
        for dep in sorted(set(units[name].requires) | set(units[name].wants) | set(units[name].after)):
            kinds = [k for k, lst in (("Requires", units[name].requires), ("Wants", units[name].wants), ("After", units[name].after)) if dep in lst]
            marker = ""
            if dep not in units:
                marker = "  (not a local unit: " + ("error)" if "Requires" in kinds else "ignored)" if "Wants" in kinds else "ordering only)")
            deps.append((dep, " ".join(kinds) + marker))
        for index, (dep, kinds) in enumerate(deps):
            branch = "└─" if index == len(deps) - 1 else "├─"
            lines.append(f"{branch}{dep:<24} {kinds}")
    selected = transaction(units, names)
    lines.append("")
    lines.append("start order: " + " -> ".join(start_order(units, selected)))
    return "\n".join(lines)


def verify(units: Dict[str, Unit]) -> Tuple[List[str], List[str]]:
    errors: List[str] = []
    warnings: List[str] = []
    for name, unit in units.items():
        warnings.extend(f"{name}: {w}" for w in unit.warnings)
        for dep in unit.requires:
            if dep not in units:
                errors.append(f"{name}: Requires={dep} but {dep} does not exist")
        for dep in unit.wants:
            if dep not in units:
                warnings.append(f"{name}: Wants={dep} but {dep} does not exist; it will be ignored")
        for dep in unit.requires:
            if dep in units and dep not in unit.after:
                warnings.append(f"{name}: Requires={dep} without After={dep}: both start at the same time and {name} is not held back if {dep} fails")
        for command in unit.exec_start_pre + unit.exec_start + unit.exec_stop_post:
            executable = command.argv[0]
            if (os.path.isabs(executable) and not os.path.exists(executable)) or (not os.path.isabs(executable) and shutil.which(executable, path=DEFAULT_PATH) is None):
                warnings.append(f"{name}: executable {executable} not found")
    try:
        start_order(units, list(units))
    except UnitError as exc:
        errors.append(str(exc))
    return errors, warnings


class ProcessBackend:
    def __init__(self) -> None:
        self.procs: Dict[int, Tuple[str, subprocess.Popen, List[threading.Thread]]] = {}
        self.lines: "queue.Queue[Tuple[str, int, str, str, float]]" = queue.Queue()

    def _pump(self, unit: str, pid: int, stream_name: str, stream) -> None:
        for raw in iter(stream.readline, b""):
            self.lines.put((unit, pid, stream_name, raw.decode("utf-8", errors="replace").rstrip("\r\n"), time.time()))
        stream.close()

    def spawn(self, unit: str, argv: List[str], env: Dict[str, str], cwd: Optional[str]) -> int:
        proc = subprocess.Popen(argv, env=env, cwd=cwd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
        threads = [threading.Thread(target=self._pump, args=(unit, proc.pid, name, stream), daemon=True) for name, stream in (("stdout", proc.stdout), ("stderr", proc.stderr))]
        for thread in threads:
            thread.start()
        self.procs[proc.pid] = (unit, proc, threads)
        return proc.pid

    def kill(self, pid: int, signum: int) -> None:
        try:
            os.killpg(pid, signum)
        except ProcessLookupError:
            pass

    def reap(self) -> List[Tuple[int, int]]:
        finished = []
        for pid, (_, proc, threads) in list(self.procs.items()):
            code = proc.poll()
            if code is None:
                continue
            for thread in threads:
                thread.join(timeout=2.0)
            del self.procs[pid]
            finished.append((pid, code))
        return finished

    def drain(self) -> List[Tuple[str, int, str, str, float]]:
        items = []
        while True:
            try:
                items.append(self.lines.get_nowait())
            except queue.Empty:
                return items

    def kill_everything(self) -> None:
        for pid in list(self.procs):
            self.kill(pid, int(signal.SIGKILL))


class StateDir:
    def __init__(self, path: str) -> None:
        self.path = path
        self.state_file = os.path.join(path, "state.json")
        self.journal_file = os.path.join(path, "journal.jsonl")
        self.pid_file = os.path.join(path, "manager.pid")
        self.control_file = os.path.join(path, "control.jsonl")

    def prepare(self) -> None:
        os.makedirs(self.path, exist_ok=True)
        for name in (self.state_file, self.journal_file, self.control_file):
            if os.path.exists(name):
                os.remove(name)
        with open(self.pid_file, "w", encoding="utf-8") as handle:
            handle.write(str(os.getpid()))

    def save(self, snapshot: Dict[str, dict]) -> None:
        tmp = self.state_file + ".tmp"
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump({"updated": time.time(), "manager_pid": os.getpid(), "units": snapshot}, handle, indent=2)
        os.replace(tmp, self.state_file)

    def append_journal(self, entry: JournalEntry) -> None:
        with open(self.journal_file, "a", encoding="utf-8") as handle:
            handle.write(json.dumps({"at": entry.at, "unit": entry.unit, "pid": entry.pid, "stream": entry.stream, "line": entry.line}) + "\n")

    def load_state(self) -> dict:
        if not os.path.exists(self.state_file):
            raise UnitError(f"no state in {self.path}; run 'miniunit start' first (or pass --state DIR)")
        return json.loads(read_text(self.state_file))

    def load_journal(self) -> List[JournalEntry]:
        if not os.path.exists(self.journal_file):
            return []
        entries = []
        for raw in read_text(self.journal_file).splitlines():
            if raw.strip():
                item = json.loads(raw)
                entries.append(JournalEntry(item["at"], item["unit"], item["pid"], item["stream"], item["line"]))
        return entries

    def manager_pid(self) -> Optional[int]:
        if not os.path.exists(self.pid_file):
            return None
        pid = int(read_text(self.pid_file).strip() or 0)
        try:
            os.kill(pid, 0)
        except (ProcessLookupError, PermissionError, ValueError):
            return None
        return pid

    def send(self, action: str, names: Sequence[str]) -> int:
        pid = self.manager_pid()
        if pid is None:
            raise UnitError(f"no running manager in {self.path}; nothing to {action}")
        with open(self.control_file, "a", encoding="utf-8") as handle:
            handle.write(json.dumps({"action": action, "units": list(names)}) + "\n")
        os.kill(pid, signal.SIGUSR1)
        return pid

    def take_control(self) -> List[str]:
        if not os.path.exists(self.control_file):
            return []
        lines = read_text(self.control_file).splitlines()
        os.remove(self.control_file)
        return lines

    def finish(self) -> None:
        if os.path.exists(self.pid_file):
            os.remove(self.pid_file)


def run(units: Dict[str, Unit], names: Sequence[str], state_dir: StateDir, duration: Optional[float], verbose: bool) -> int:
    log = (lambda line: print(f"[miniunit] {line}", file=sys.stderr, flush=True)) if verbose else (lambda line: None)
    state_dir.prepare()
    backend = ProcessBackend()
    journal = Journal(sink=state_dir.append_journal)
    manager = Manager(units, backend.spawn, backend.kill, time.time, journal, read_text, log)
    flags = {"stop": False, "control": False}

    def on_signal(signum, _frame):
        if signum == signal.SIGUSR1:
            flags["control"] = True
        else:
            flags["stop"] = True

    for signum in (signal.SIGTERM, signal.SIGINT, signal.SIGUSR1):
        signal.signal(signum, on_signal)
    started_at = time.time()
    try:
        manager.start(names)
        while True:
            if flags["stop"]:
                flags["stop"] = False
                duration = None
                manager.stop_all()
            if flags["control"]:
                flags["control"] = False
                manager.apply_control(state_dir.take_control())
            finished = backend.reap()
            for unit, pid, stream, line, at in backend.drain():
                journal.record(unit, pid, stream, line, at)
            for pid, code in finished:
                manager.handle_exit(pid, code)
            manager.tick()
            if manager.changed:
                state_dir.save(manager.snapshot())
                manager.changed = False
            if duration is not None and time.time() - started_at >= duration:
                duration = None
                manager.stop_all()
                continue
            if not manager.busy() and not backend.procs:
                break
            deadline = manager.next_deadline()
            timeout = 0.2 if deadline is None else max(0.02, min(0.2, deadline - time.time()))
            time.sleep(timeout)
    finally:
        backend.kill_everything()
        state_dir.save(manager.snapshot())
        state_dir.finish()
    failed = [n for n, s in manager.states.items() if s.result != "success"]
    if verbose or failed:
        for name in names:
            print(render_status(manager.status_dict(name), journal.for_unit(name, 5), time.time()), file=sys.stderr)
    return 1 if failed else 0


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="miniunit", description=__doc__)
    common = argparse.ArgumentParser(add_help=False)
    for owner, default in ((parser, "./units"), (common, argparse.SUPPRESS)):
        owner.add_argument("--units", default=default, metavar="DIR", help="directory with *.service files")
    for owner, default in ((parser, ".miniunit"), (common, argparse.SUPPRESS)):
        owner.add_argument("--state", default=default, metavar="DIR", help="where the running manager keeps state.json, journal.jsonl and manager.pid")
    for owner, default in ((parser, False), (common, argparse.SUPPRESS)):
        owner.add_argument("--json", action="store_true", default=default, help="machine-readable output")
    sub = parser.add_subparsers(dest="command", required=True)
    p_start = sub.add_parser("start", parents=[common], help="start units (and what they Require/Want) and supervise them in the foreground")
    p_start.add_argument("names", nargs="*", help="unit names; default: every unit in --units")
    p_start.add_argument("--for", dest="duration", type=float, metavar="SECONDS", help="stop everything after SECONDS")
    p_start.add_argument("--verbose", action="store_true")
    p_stop = sub.add_parser("stop", parents=[common], help="stop units in a running manager (and units that Require them), or the whole manager")
    p_stop.add_argument("names", nargs="*")
    p_stop.add_argument("--timeout", type=float, default=120.0)
    p_status = sub.add_parser("status", parents=[common], help="systemctl status-like view from the state directory")
    p_status.add_argument("names", nargs="*")
    p_graph = sub.add_parser("graph", parents=[common], help="dependencies and start order")
    p_graph.add_argument("names", nargs="*")
    p_journal = sub.add_parser("journal", parents=[common], help="captured stdout/stderr per unit")
    p_journal.add_argument("-u", "--unit", help="only this unit")
    p_journal.add_argument("-n", "--lines", type=int, default=0, help="last N entries")
    sub.add_parser("verify", parents=[common], help="parse every unit and check the dependency graph, like systemd-analyze verify")
    args = parser.parse_args(list(sys.argv[1:] if argv is None else argv))
    state_dir = StateDir(args.state)
    try:
        if args.command in ("status", "journal"):
            return _show(args, state_dir)
        if args.command == "stop":
            return _stop(args, state_dir)
        units = load_units(args.units)
        if args.command == "verify":
            errors, warnings = verify(units)
            if args.json:
                print(json.dumps({"units": sorted(units), "errors": errors, "warnings": warnings}, indent=2))
            else:
                for line in warnings:
                    print(f"warning: {line}")
                for line in errors:
                    print(f"error: {line}")
                print(f"{len(units)} unit(s), {len(errors)} error(s), {len(warnings)} warning(s)")
            return 1 if errors else 0
        names = list(args.names) if args.names else sorted(units)
        if args.command == "graph":
            if args.json:
                selected = transaction(units, names)
                print(json.dumps({"order": start_order(units, selected), "units": {n: {"after": units[n].after, "requires": units[n].requires, "wants": units[n].wants} for n in selected}}, indent=2))
            else:
                print(render_graph(units, names))
            return 0
        for name in names:
            if name not in units:
                raise UnitError(f"Unit {name} not found in {args.units}")
        return run(units, names, state_dir, args.duration, args.verbose)
    except UnitError as exc:
        print(f"miniunit: {exc}", file=sys.stderr)
        return 2


def _stop(args, state_dir: StateDir) -> int:
    pid = state_dir.send("stop", args.names)
    deadline = time.time() + args.timeout
    while time.time() < deadline:
        state = state_dir.load_state()
        targets = args.names or list(state["units"])
        unknown = [n for n in targets if n not in state["units"]]
        if unknown:
            raise UnitError(f"unknown unit(s): {', '.join(unknown)}")
        if all(state["units"][n]["active"] in ("inactive", "failed") for n in targets):
            for name in targets:
                print(f"{name}: {state['units'][name]['active']} ({state['units'][name]['sub']})")
            return 0
        time.sleep(0.1)
    print(f"miniunit: manager pid {pid} did not stop within {args.timeout}s", file=sys.stderr)
    return 1


def _show(args, state_dir: StateDir) -> int:
    if args.command == "journal":
        entries = state_dir.load_journal()
        if args.unit:
            entries = [e for e in entries if e.unit == args.unit]
        if args.lines:
            entries = entries[-args.lines:]
        for entry in entries:
            print(json.dumps(entry.__dict__) if args.json else format_journal_line(entry))
        return 0
    state = state_dir.load_state()
    names = args.names or list(state["units"])
    missing = [n for n in names if n not in state["units"]]
    if missing:
        raise UnitError(f"unknown unit(s): {', '.join(missing)}")
    if args.json:
        print(json.dumps({n: state["units"][n] for n in names}, indent=2))
        return 0
    entries = state_dir.load_journal()
    running = state_dir.manager_pid()
    print(f"manager: {'running, pid ' + str(running) if running else 'not running (last run shown)'}")
    for name in names:
        tail = [e for e in entries if e.unit == name][-5:]
        print(render_status(state["units"][name], tail, time.time()))
        print()
    return 0 if all(state["units"][n]["result"] == "success" for n in names) else 1


if __name__ == "__main__":
    raise SystemExit(main())
