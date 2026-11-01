"""mini_init: a PID 1 that starts services, reaps every child, forwards signals and restarts on failure with backoff."""
from __future__ import annotations

import argparse
import json
import os
import shlex
import signal
import sys
import time
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Sequence, Tuple

RESTART_POLICIES = ("always", "on-failure", "never")
MAX_BACKOFF_SECONDS = 30


@dataclass
class Service:
    name: str
    command: List[str]
    restart: str = "on-failure"
    max_restarts: int = 5


@dataclass
class Child:
    service: Service
    pid: int
    started_at: float
    restarts: int = 0


@dataclass
class Exit:
    name: str
    pid: int
    kind: str
    value: int
    at: float


def decode_status(status: int) -> Tuple[str, int]:
    if os.WIFEXITED(status):
        return "exited", os.WEXITSTATUS(status)
    if os.WIFSIGNALED(status):
        return "signaled", os.WTERMSIG(status)
    return "unknown", status


def backoff(attempt: int) -> float:
    return float(min(MAX_BACKOFF_SECONDS, 2 ** max(0, attempt)))


def should_restart(service: Service, kind: str, value: int, restarts: int, shutting_down: bool) -> bool:
    if shutting_down or restarts >= service.max_restarts:
        return False
    if service.restart == "always":
        return True
    if service.restart == "on-failure":
        return not (kind == "exited" and value == 0)
    return False


def parse_spec(path: Optional[str], runs: Sequence[str], restart: str) -> List[Service]:
    services: List[Service] = []
    if path:
        with open(path, encoding="utf-8") as handle:
            for item in json.load(handle).get("services", []):
                services.append(Service(item["name"], shlex.split(item["command"]) if isinstance(item["command"], str) else list(item["command"]), item.get("restart", "on-failure"), int(item.get("max_restarts", 5))))
    for index, run in enumerate(runs):
        services.append(Service(f"svc{index + 1}", shlex.split(run), restart))
    for service in services:
        if service.restart not in RESTART_POLICIES:
            raise ValueError(f"{service.name}: restart must be one of {RESTART_POLICIES}")
        if not service.command:
            raise ValueError(f"{service.name}: empty command")
    if not services:
        raise ValueError("nothing to supervise: pass --run CMD or --spec file.json")
    return services


class Supervisor:
    def __init__(
        self,
        services: Sequence[Service],
        spawn: Callable[[Sequence[str]], int],
        kill: Callable[[int, int], None],
        now: Callable[[], float] = time.monotonic,
        grace_seconds: float = 10.0,
        log: Callable[[str], None] = lambda line: None,
    ) -> None:
        self.services = list(services)
        self.spawn = spawn
        self.kill = kill
        self.now = now
        self.grace_seconds = grace_seconds
        self.log = log
        self.children: Dict[int, Child] = {}
        self.pending: List[Tuple[float, Service, int]] = []
        self.exits: List[Exit] = []
        self.reaped_orphans = 0
        self.shutting_down = False
        self.shutdown_started: Optional[float] = None
        self.last_exit_code = 0

    def start_all(self) -> None:
        for service in self.services:
            self._start(service, restarts=0)

    def _start(self, service: Service, restarts: int) -> None:
        pid = self.spawn(service.command)
        self.children[pid] = Child(service, pid, self.now(), restarts)
        self.log(f"start {service.name} pid={pid} restarts={restarts}")

    def handle_exit(self, pid: int, status: int) -> None:
        kind, value = decode_status(status)
        child = self.children.pop(pid, None)
        if child is None:
            self.reaped_orphans += 1
            self.log(f"reaped orphan pid={pid} ({kind} {value})")
            return
        self.exits.append(Exit(child.service.name, pid, kind, value, self.now()))
        self.last_exit_code = value if kind == "exited" else 128 + value
        self.log(f"exit {child.service.name} pid={pid} {kind} {value}")
        if should_restart(child.service, kind, value, child.restarts, self.shutting_down):
            delay = backoff(child.restarts)
            self.pending.append((self.now() + delay, child.service, child.restarts + 1))
            self.log(f"restart {child.service.name} in {delay:.0f}s (attempt {child.restarts + 1}/{child.service.max_restarts})")

    def tick(self) -> None:
        due = [item for item in self.pending if item[0] <= self.now()]
        self.pending = [item for item in self.pending if item[0] > self.now()]
        for _, service, restarts in due:
            self._start(service, restarts)
        if self.shutting_down and self.children and self.shutdown_started is not None and self.now() - self.shutdown_started >= self.grace_seconds:
            for pid in list(self.children):
                self.kill(pid, signal.SIGKILL)
            self.log(f"grace period over: SIGKILL to {len(self.children)} child(ren)")
            self.shutdown_started = self.now() + 3600

    def forward_signal(self, signum: int) -> List[int]:
        targets = list(self.children)
        for pid in targets:
            try:
                self.kill(pid, signum)
            except ProcessLookupError:
                pass
        return targets

    def begin_shutdown(self, signum: int) -> None:
        if not self.shutting_down:
            self.shutting_down = True
            self.shutdown_started = self.now()
            self.pending.clear()
        self.forward_signal(signum)

    def next_deadline(self) -> Optional[float]:
        deadlines = [item[0] for item in self.pending]
        if self.shutting_down and self.children and self.shutdown_started is not None:
            deadlines.append(self.shutdown_started + self.grace_seconds)
        return min(deadlines) if deadlines else None

    @property
    def finished(self) -> bool:
        return not self.children and not self.pending

    def summary(self) -> str:
        lines = [f"services: {len(self.services)}  exits: {len(self.exits)}  orphans reaped: {self.reaped_orphans}"]
        for e in self.exits:
            lines.append(f"  {e.name:<8} pid={e.pid:<6} {e.kind} {e.value}")
        return "\n".join(lines)


def run(services: Sequence[Service], grace_seconds: float, verbose: bool) -> int:
    log = (lambda line: print(f"[mini_init] {line}", file=sys.stderr, flush=True)) if verbose else (lambda line: None)

    def spawn(command: Sequence[str]) -> int:
        pid = os.fork()
        if pid == 0:
            signal.signal(signal.SIGTERM, signal.SIG_DFL)
            signal.signal(signal.SIGINT, signal.SIG_DFL)
            try:
                os.execvp(command[0], list(command))
            except OSError as exc:
                print(f"[mini_init] cannot exec {command[0]}: {exc}", file=sys.stderr, flush=True)
                os._exit(127)
        return pid

    supervisor = Supervisor(services, spawn, os.kill, time.monotonic, grace_seconds, log)
    wake = {"flag": False}

    def on_signal(signum, _frame):
        if signum == signal.SIGCHLD:
            wake["flag"] = True
        else:
            supervisor.begin_shutdown(signum)

    for signum in (signal.SIGTERM, signal.SIGINT, signal.SIGCHLD):
        signal.signal(signum, on_signal)

    supervisor.start_all()
    while not supervisor.finished:
        while True:
            try:
                pid, status = os.waitpid(-1, os.WNOHANG)
            except ChildProcessError:
                break
            if pid == 0:
                break
            supervisor.handle_exit(pid, status)
        supervisor.tick()
        if supervisor.finished:
            break
        deadline = supervisor.next_deadline()
        timeout = max(0.05, min(1.0, deadline - time.monotonic())) if deadline is not None else 1.0
        time.sleep(timeout)

    if verbose:
        print(supervisor.summary(), file=sys.stderr, flush=True)
    return supervisor.last_exit_code


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="mini_init", description=__doc__)
    parser.add_argument("--run", action="append", default=[], metavar="CMD", help="command to supervise (repeatable)")
    parser.add_argument("--spec", help="JSON file: {\"services\": [{\"name\", \"command\", \"restart\", \"max_restarts\"}]}")
    parser.add_argument("--restart", default="on-failure", choices=RESTART_POLICIES, help="policy for --run services")
    parser.add_argument("--grace", type=float, default=10.0, help="seconds between SIGTERM and SIGKILL on shutdown")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args(list(sys.argv[1:] if argv is None else argv))
    services = parse_spec(args.spec, args.run, args.restart)
    return run(services, args.grace, args.verbose)


if __name__ == "__main__":
    raise SystemExit(main())
