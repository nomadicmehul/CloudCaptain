"""tracelens: parse strace output (-f -tt -T -y or plain), group failures by errno, find the slowest calls, build the strace -c histogram and say in one line why the program failed."""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from collections import Counter, OrderedDict
from dataclasses import asdict, dataclass, field
from typing import Callable, Dict, List, Optional, Sequence, Tuple

LINE_RE = re.compile(
    r"^(?:\[pid\s+(?P<bracket_pid>\d+)\]\s*|(?P<bare_pid>\d+)\s+)?"
    r"(?:(?P<clock>\d{2}:\d{2}:\d{2}(?:\.\d+)?|\d+\.\d+)\s+)?"
    r"(?P<body>.*)$"
)
CALL_RE = re.compile(r"^(?P<name>[A-Za-z_][A-Za-z0-9_]*)\((?P<rest>.*)$")
RESUMED_RE = re.compile(r"^<\.\.\. (?P<name>[A-Za-z_][A-Za-z0-9_]*) resumed>\s?(?P<rest>.*)$")
UNFINISHED_SUFFIX = "<unfinished ...>"
RESULT_RE = re.compile(r"^(?P<value>0x[0-9a-fA-F]+|-?\d+|\?)(?:\s+(?P<errno>E[A-Z0-9_]+)(?:\s+\((?P<text>[^)]*)\))?)?(?P<tail>.*)$")
DURATION_RE = re.compile(r"\s*<(?P<duration>\d+\.\d+)>\s*$")
EXIT_RE = re.compile(r"^\+\+\+ (?:exited with (?P<code>\d+)|killed by (?P<signal>SIG[A-Z0-9]+)(?: \(core dumped\))?|(?P<other>.+?)) \+\+\+$")
SIGNAL_RE = re.compile(r"^--- (?P<name>SIG[A-Z0-9]+|stopped by SIG[A-Z0-9]+)\s*(?P<detail>.*?)\s*---$")
ATTACHED_RE = re.compile(r"^Process (?P<pid>\d+) attached")
FD_DECORATION_RE = re.compile(r"^(?P<fd>\d+)(?:<(?P<target>.+)>)?$")
FAMILY_RE = re.compile(r"sa_family=(AF_[A-Z0-9]+)")
PORT_RE = re.compile(r"sin6?_port=htons\((\d+)\)")
INET_RE = re.compile(r'inet_addr\("([^"]+)"\)')
INET6_RE = re.compile(r'inet_pton\(AF_INET6, "([^"]+)"')
UNIX_RE = re.compile(r'sun_path=(@?"[^"]*")')
ESCAPES = {"n": "\n", "t": "\t", "r": "\r", "\\": "\\", '"': '"', "a": "\a", "b": "\b", "f": "\f", "v": "\v"}

PATH_ARG = {
    "open": 0, "stat": 0, "lstat": 0, "access": 0, "readlink": 0, "execve": 0, "unlink": 0, "chdir": 0,
    "mkdir": 0, "rmdir": 0, "chmod": 0, "chown": 0, "lchown": 0, "truncate": 0, "creat": 0, "statfs": 0,
    "rename": 0, "link": 0, "symlink": 0, "mknod": 0, "utimes": 0, "chroot": 0, "acct": 0, "swapon": 0,
    "openat": 1, "openat2": 1, "newfstatat": 1, "fstatat64": 1, "statx": 1, "faccessat": 1, "faccessat2": 1,
    "readlinkat": 1, "unlinkat": 1, "mkdirat": 1, "fchmodat": 1, "fchmodat2": 1, "fchownat": 1, "utimensat": 1,
    "renameat": 1, "renameat2": 1, "linkat": 1, "symlinkat": 1, "mknodat": 1, "execveat": 1, "name_to_handle_at": 1,
    "inotify_add_watch": 1, "mount": 1,
}
ADDRESS_CALLS = frozenset({"connect", "bind", "sendto", "sendmsg", "recvfrom", "accept", "accept4", "getpeername", "getsockname"})
OPEN_CALLS = frozenset({"open", "openat", "openat2", "creat"})
FORK_CALLS = frozenset({"clone", "clone3", "fork", "vfork"})
BLOCKING_CALLS = frozenset({
    "wait4", "waitpid", "waitid", "poll", "ppoll", "select", "pselect6", "epoll_wait", "epoll_pwait", "epoll_pwait2",
    "nanosleep", "clock_nanosleep", "futex", "pause", "rt_sigtimedwait", "rt_sigsuspend", "sigsuspend", "accept", "accept4",
    "recvfrom", "recvmsg", "recvmmsg", "read", "readv", "io_uring_enter", "flock", "msgrcv", "semop", "restart_syscall",
})
LOW_SIGNAL_ERRNOS = frozenset({
    "EAGAIN", "EWOULDBLOCK", "EINTR", "EINPROGRESS", "EALREADY", "ECHILD", "ENOTTY", "EEXIST", "ENOSYS", "EINVAL",
    "ESPIPE", "ENOTSUP", "EOPNOTSUPP", "ENODATA", "ENOTCONN", "ERESTARTSYS", "ERESTARTNOINTR", "ERESTARTNOHAND",
    "ERESTART_RESTARTBLOCK",
})
NOISE_PATHS = ("/etc/ld.so.preload",)
HINTS = {
    "ENOENT": "the name does not exist: check the path exactly as traced, the order it was tried in, the process cwd (/proc/PID/cwd), and in a container whether the file is in the image or a volume",
    "ENOTDIR": "a component of the path is not a directory: a file bind-mounted where a directory was expected, or a path built from the wrong variable",
    "EACCES": "permission denied by file mode, a directory without the x bit, or AppArmor/SELinux: ls -ld every component, compare with the Uid line of /proc/PID/status, check dmesg or the audit log",
    "EPERM": "a capability or seccomp rule said no: CapEff in /proc/PID/status, the container's --cap-add/--cap-drop and seccomp profile, kernel.yama.ptrace_scope for ptrace",
    "ECONNREFUSED": "the SYN was answered with RST: nothing listens on that address and port from this network namespace; ss -ltn on the target, and remember localhost inside a container is the container",
    "ETIMEDOUT": "packets were dropped: a firewall, security group or NetworkPolicy, or no route; ip route get ADDR and the peer's listener",
    "EHOSTUNREACH": "no route or ARP failed: ip route get ADDR, the netns default route, the CNI",
    "ENETUNREACH": "no route to that network from this namespace: ip route, the container network",
    "EADDRINUSE": "the port is bound already: ss -ltnp, a previous instance still running, or SO_REUSEADDR missing",
    "EMFILE": "this process's descriptor table is full: see the file-descriptors lesson (fdwatch), the fix is the leak not the ulimit",
    "ENFILE": "the whole machine is out of file handles: fs.file-nr against fs.file-max",
    "ENOSPC": "no blocks or no inodes left: df -h and df -i on the filesystem of that path",
    "EDQUOT": "a quota is exhausted: quota -s, the project or user quota on that filesystem",
    "EROFS": "the filesystem is read-only: --read-only, readOnlyRootFilesystem, a remounted ro disk; write to a volume or tmpfs instead",
    "ENOEXEC": "not an executable the kernel understands: wrong architecture (arm64 vs amd64 image), missing shebang, or a text file",
    "ETXTBSY": "the binary is open for writing by someone else: a deploy overwriting a running executable",
    "ENOMEM": "the kernel refused memory: cgroup memory.max, overcommit, or a huge mmap",
    "ECONNRESET": "the peer closed abruptly: its logs, a proxy idle timeout, a load balancer draining",
    "EPIPE": "wrote to a pipe or socket whose reader is gone: the downstream process exited first",
    "ELOOP": "too many symlinks or a symlink where O_NOFOLLOW was given: readlink -f the path",
    "ENAMETOOLONG": "a path or name longer than the limit: pathconf, or a runaway string in the config",
}
DEFAULT_TRACE_SET = "%file,%network,%process,write"


@dataclass
class Call:
    line: int
    pid: Optional[int]
    clock: Optional[str]
    name: str
    args: List[str]
    result: Optional[int]
    errno: Optional[str]
    error_text: Optional[str]
    duration: Optional[float]
    injected: bool = False
    target: Optional[str] = None

    @property
    def failed(self) -> bool:
        return self.errno is not None

    @property
    def family(self) -> str:
        if self.name in PATH_ARG:
            return "file"
        if self.name in ADDRESS_CALLS:
            return "network"
        return self.name


@dataclass
class Event:
    line: int
    pid: Optional[int]
    kind: str
    detail: str
    code: Optional[int] = None


@dataclass
class Trace:
    calls: List[Call]
    events: List[Event]
    lines: int
    source: str = "-"

    @property
    def exits(self) -> List[Event]:
        return [e for e in self.events if e.kind in ("exit", "killed")]

    @property
    def unparsed(self) -> List[Event]:
        return [e for e in self.events if e.kind == "unparsed"]


@dataclass
class Process:
    pid: Optional[int]
    parent: Optional[int] = None
    program: Optional[str] = None
    argv: List[str] = field(default_factory=list)
    thread: bool = False
    exit_code: Optional[int] = None
    signal: Optional[str] = None


@dataclass
class FailedTarget:
    syscall: str
    target: Optional[str]
    count: int
    pids: List[Optional[int]]
    first_line: int


@dataclass
class FailureGroup:
    errno: str
    text: str
    calls: int
    low_signal: bool
    targets: List[FailedTarget]


@dataclass
class HistogramRow:
    syscall: str
    calls: int
    errors: int
    seconds: Optional[float]
    percent: Optional[float]


@dataclass
class Verdict:
    text: str
    pid: Optional[int] = None
    syscall: Optional[str] = None
    errno: Optional[str] = None
    target: Optional[str] = None
    attempts: List[str] = field(default_factory=list)
    exit_code: Optional[int] = None
    signal: Optional[str] = None
    hint: Optional[str] = None


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
        elif text.startswith("/*", i):
            end = text.find("*/", i + 2)
            i = len(text) if end < 0 else end + 2
            continue
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
        elif text.startswith("/*", i):
            end = text.find("*/", i + 2)
            i = len(text) if end < 0 else end + 2
            continue
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
    return [p for p in parts if p]


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
            if nxt in "01234567":
                j = i + 1
                while j < len(text) and j < i + 4 and text[j] in "01234567":
                    j += 1
                out.append(chr(int(text[i + 1:j], 8)))
                i = j
                continue
            if nxt == "x" and i + 3 < len(text):
                out.append(chr(int(text[i + 2:i + 4], 16)))
                i += 4
                continue
            out.append(ESCAPES.get(nxt, nxt))
            i += 2
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def parse_address(arg: str) -> Optional[str]:
    if not arg.startswith("{"):
        return None
    family = FAMILY_RE.search(arg)
    if not family:
        return None
    name = family.group(1)
    port = PORT_RE.search(arg)
    if name == "AF_INET":
        host = INET_RE.search(arg)
        return f"{host.group(1)}:{port.group(1)}" if host and port else name
    if name == "AF_INET6":
        host = INET6_RE.search(arg)
        return f"[{host.group(1)}]:{port.group(1)}" if host and port else name
    if name in ("AF_UNIX", "AF_LOCAL"):
        path = UNIX_RE.search(arg)
        return (unquote(path.group(1)) or "(abstract socket)") if path else name
    return name


def fd_of(arg: str) -> Optional[int]:
    match = FD_DECORATION_RE.match(arg.strip())
    return int(match.group("fd")) if match else None


def describe_target(name: str, args: List[str]) -> Optional[str]:
    index = PATH_ARG.get(name)
    if index is not None and index < len(args):
        path = unquote(args[index])
        if path is not None:
            if index == 1 and not path.startswith("/"):
                dirfd = FD_DECORATION_RE.match(args[0])
                if dirfd and dirfd.group("target") and dirfd.group("target").startswith("/"):
                    path = os.path.join(dirfd.group("target"), path)
            return path
    if name in ADDRESS_CALLS:
        for arg in args[1:]:
            address = parse_address(arg)
            if address:
                return address
    if args:
        decorated = FD_DECORATION_RE.match(args[0])
        if decorated and decorated.group("target"):
            return decorated.group("target")
    return None


def parse_result(text: str) -> Tuple[Optional[int], Optional[str], Optional[str], Optional[float], bool]:
    duration = None
    match = DURATION_RE.search(text)
    if match:
        duration = float(match.group("duration"))
        text = text[: match.start()]
    result = RESULT_RE.match(text.strip())
    if not result:
        return None, None, None, duration, False
    value = result.group("value")
    number = None if value == "?" else int(value, 0)
    return number, result.group("errno"), result.group("text"), duration, "INJECTED" in result.group("tail")


def parse_call(line: int, pid: Optional[int], clock: Optional[str], body: str) -> Optional[Call]:
    match = CALL_RE.match(body)
    if not match:
        return None
    name, rest = match.group("name"), match.group("rest")
    close = find_close(rest)
    if close < 0:
        return None
    after = rest[close + 1:].strip()
    if not after.startswith("="):
        return None
    result, errno, text, duration, injected = parse_result(after[1:])
    args = split_args(rest[:close])
    return Call(line, pid, clock, name, args, result, errno, text, duration, injected, describe_target(name, args))


def parse_marker(line: int, pid: Optional[int], body: str) -> Event:
    exit_match = EXIT_RE.match(body)
    if exit_match:
        if exit_match.group("code") is not None:
            return Event(line, pid, "exit", exit_match.group("code"), int(exit_match.group("code")))
        if exit_match.group("signal"):
            return Event(line, pid, "killed", exit_match.group("signal"))
        return Event(line, pid, "exit", exit_match.group("other") or body)
    signal_match = SIGNAL_RE.match(body)
    if signal_match:
        return Event(line, pid, "signal", signal_match.group("name").replace("stopped by ", ""))
    return Event(line, pid, "unparsed", body)


def pop_pending(pending: "OrderedDict[Tuple[Optional[int], str], Tuple[int, Optional[str], str]]", pid: Optional[int], name: str):
    if (pid, name) in pending:
        return (pid, name), pending.pop((pid, name))
    for key in list(pending):
        if key[1] == name and (pid is None or key[0] is None):
            return key, pending.pop(key)
    return None


def resolve_leader(calls: List[Call], events: List[Event]) -> Optional[int]:
    known = {c.pid for c in calls if c.pid is not None} | {e.pid for e in events if e.pid is not None}
    anonymous = any(c.pid is None for c in calls) or any(e.pid is None for e in events)
    if not known or not anonymous:
        return None
    children = {c.result for c in calls if c.name in FORK_CALLS and c.result is not None and c.result > 0}
    for event in events:
        if event.kind == "message":
            attached = ATTACHED_RE.match(event.detail)
            if attached:
                children.add(int(attached.group("pid")))
    leaders = known - children
    if len(leaders) != 1:
        return None
    leader = next(iter(leaders))
    for call in calls:
        if call.pid is None:
            call.pid = leader
    for event in events:
        if event.pid is None:
            event.pid = leader
    return leader


def parse(text: str, source: str = "-") -> Trace:
    calls: List[Call] = []
    events: List[Event] = []
    pending: "OrderedDict[Tuple[Optional[int], str], Tuple[int, Optional[str], str]]" = OrderedDict()
    count = 0
    for count, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line:
            continue
        match = LINE_RE.match(line)
        pid_text = match.group("bracket_pid") or match.group("bare_pid")
        pid = int(pid_text) if pid_text else None
        clock = match.group("clock")
        body = match.group("body").strip()
        number = count
        if body.startswith("+++") or body.startswith("---"):
            events.append(parse_marker(number, pid, body))
            continue
        if body.startswith("strace:"):
            events.append(Event(number, pid, "message", body[len("strace:"):].strip()))
            continue
        resumed = RESUMED_RE.match(body)
        if resumed:
            found = pop_pending(pending, pid, resumed.group("name"))
            if found is None:
                events.append(Event(number, pid, "unparsed", line))
                continue
            key, head = found
            number, clock, partial = head[0], head[1] or clock, head[2]
            pid = pid if pid is not None else key[0]
            body = f"{resumed.group('name')}({partial}{resumed.group('rest')}"
        elif body.endswith(UNFINISHED_SUFFIX):
            head = CALL_RE.match(body[: -len(UNFINISHED_SUFFIX)])
            if head:
                pending[(pid, head.group("name"))] = (number, clock, head.group("rest"))
                continue
        call = parse_call(number, pid, clock, body)
        if call is None:
            events.append(Event(number, pid, "unparsed", line))
        else:
            calls.append(call)
    for (pid, name), (number, _clock, partial) in pending.items():
        events.append(Event(number, pid, "unparsed", f"{name}({partial}{UNFINISHED_SUFFIX}"))
    events.sort(key=lambda e: e.line)
    calls.sort(key=lambda c: c.line)
    resolve_leader(calls, events)
    return Trace(calls, events, count, source)


def processes(trace: Trace) -> List[Process]:
    table: "OrderedDict[Optional[int], Process]" = OrderedDict()

    def ensure(pid: Optional[int]) -> Process:
        if pid not in table:
            table[pid] = Process(pid)
        return table[pid]

    for call in trace.calls:
        proc = ensure(call.pid)
        if call.name in FORK_CALLS and call.result is not None and call.result > 0:
            child = ensure(call.result)
            child.parent = call.pid
            child.thread = "CLONE_THREAD" in " ".join(call.args)
            if child.program is None:
                child.program, child.argv = proc.program, list(proc.argv)
        if call.name in ("execve", "execveat") and call.result == 0:
            proc.program = call.target
            proc.argv = parse_argv(call.args[1]) if len(call.args) > 1 else []
    for event in trace.events:
        if event.kind in ("exit", "killed", "signal"):
            proc = ensure(event.pid)
            if event.kind == "exit":
                proc.exit_code = event.code
            elif event.kind == "killed":
                proc.signal = event.detail
    return list(table.values())


def parse_argv(text: str) -> List[str]:
    text = text.strip()
    if not text.startswith("["):
        return []
    inner = text[1:text.rfind("]")] if "]" in text else text[1:]
    return [item for item in (unquote(part) for part in split_args(inner)) if item is not None]


def is_low_signal(call: Call) -> bool:
    return call.errno in LOW_SIGNAL_ERRNOS or (call.target or "").startswith(NOISE_PATHS)


def meaningful_failures(trace: Trace) -> List[Call]:
    return [c for c in trace.calls if c.failed and not is_low_signal(c)]


def failures_by_errno(trace: Trace, include_low: bool = False) -> List[FailureGroup]:
    groups: "OrderedDict[str, FailureGroup]" = OrderedDict()
    index: Dict[Tuple[str, str, str], FailedTarget] = {}
    for call in trace.calls:
        if not call.failed:
            continue
        low = is_low_signal(call)
        if low and not include_low:
            continue
        assert call.errno is not None
        group = groups.get(call.errno)
        if group is None:
            group = groups[call.errno] = FailureGroup(call.errno, call.error_text or "", 0, low, [])
        group.low_signal = group.low_signal and low
        group.calls += 1
        key = (call.errno, call.name, call.target or "")
        entry = index.get(key)
        if entry is None:
            entry = index[key] = FailedTarget(call.name, call.target, 0, [], call.line)
            group.targets.append(entry)
        entry.count += 1
        if call.pid not in entry.pids:
            entry.pids.append(call.pid)
    return sorted(groups.values(), key=lambda g: (g.low_signal, -g.calls, g.errno))


def slowest(trace: Trace, top: int = 10) -> List[Call]:
    timed = [c for c in trace.calls if c.duration is not None]
    return sorted(timed, key=lambda c: -c.duration)[:top]


def histogram(trace: Trace) -> List[HistogramRow]:
    calls: Counter = Counter()
    errors: Counter = Counter()
    seconds: Dict[str, float] = {}
    timed = False
    for call in trace.calls:
        calls[call.name] += 1
        if call.failed:
            errors[call.name] += 1
        if call.duration is not None:
            timed = True
            seconds[call.name] = seconds.get(call.name, 0.0) + call.duration
    total = sum(seconds.values())
    rows = []
    for name, count in calls.items():
        spent = seconds.get(name, 0.0) if timed else None
        percent = (spent / total * 100 if total else 0.0) if timed else None
        rows.append(HistogramRow(name, count, errors[name], spent, percent))
    rows.sort(key=lambda r: ((-(r.seconds or 0.0)) if timed else 0.0, -r.calls, r.syscall))
    return rows


def opened_files(trace: Trace) -> "OrderedDict[Optional[int], List[str]]":
    opened: "OrderedDict[Optional[int], List[str]]" = OrderedDict()
    for call in trace.calls:
        if call.name in OPEN_CALLS and call.result is not None and call.result >= 0 and call.target:
            paths = opened.setdefault(call.pid, [])
            if call.target not in paths:
                paths.append(call.target)
    return opened


def stderr_writes(trace: Trace) -> List[Tuple[Optional[int], str]]:
    found = []
    for call in trace.calls:
        if call.name == "write" and len(call.args) >= 2 and fd_of(call.args[0]) == 2:
            text = unquote(call.args[1])
            if text and text.strip():
                found.append((call.pid, text.rstrip("\n")))
    return found


def signals(trace: Trace) -> Dict[str, int]:
    return dict(Counter(e.detail for e in trace.events if e.kind == "signal"))


def pid_label(pid: Optional[int]) -> str:
    return f"pid {pid}" if pid is not None else "the traced process"


def program_label(proc: Optional[Process], pid: Optional[int]) -> str:
    if proc and proc.argv:
        return f"{' '.join(proc.argv[:2])} ({pid_label(pid)})"
    if proc and proc.program:
        return f"{os.path.basename(proc.program)} ({pid_label(pid)})"
    return pid_label(pid)


def same_group(a: Call, b: Call) -> bool:
    if a.pid != b.pid or a.errno != b.errno or a.family != b.family:
        return False
    if a.family == "file":
        return os.path.basename(a.target or "") == os.path.basename(b.target or "")
    return a.target == b.target


def verdict(trace: Trace) -> Verdict:
    procs = {p.pid: p for p in processes(trace)}
    denied = [e for e in trace.events if e.kind == "message" and "Operation not permitted" in e.detail]
    if denied and not trace.calls:
        return Verdict(
            f"strace could not attach: {denied[0].detail}. Attaching to a process you did not start needs CAP_SYS_PTRACE "
            "(docker run --cap-add SYS_PTRACE, kubectl debug --profile=general) or kernel.yama.ptrace_scope=0; "
            "tracing a child you start yourself needs neither",
            errno="EPERM", hint=HINTS["EPERM"],
        )
    candidates = meaningful_failures(trace)
    low = sum(1 for c in trace.calls if c.failed and is_low_signal(c))
    exits = ", ".join(f"{pid_label(e.pid)} {'exit ' + str(e.code) if e.kind == 'exit' else 'killed by ' + e.detail}" for e in trace.exits)
    if not candidates:
        text = f"no failing system calls worth reporting: {len(trace.calls)} calls, {low} low-signal errors hidden"
        return Verdict(text + (f"; {exits}" if exits else "; no exit recorded"))
    culprit = candidates[-1]
    siblings = [c for c in candidates if same_group(c, culprit)]
    attempts: List[str] = []
    for call in siblings:
        if call.target and call.target not in attempts:
            attempts.append(call.target)
    proc = procs.get(culprit.pid)
    who = program_label(proc, culprit.pid)
    if proc and proc.parent is not None:
        who = who[:-1] + f", child of {pid_label(proc.parent)})"
    joiner = "to" if culprit.family == "network" else "of"
    cause = f"{culprit.name}() {joiner} {culprit.target}" if culprit.target else f"{culprit.name}()"
    outcome = f"returned {culprit.errno} ({culprit.error_text})" if culprit.error_text else f"returned {culprit.errno}"
    if culprit.injected:
        outcome += " [injected by strace]"
    if len(attempts) > 1:
        tail = f" after trying {len(attempts)} locations: {', '.join(attempts)}"
    elif len(siblings) > 1:
        tail = f" after {len(siblings)} attempts"
    else:
        tail = ""
    exit_code = proc.exit_code if proc else None
    signal_name = proc.signal if proc else None
    if signal_name:
        text = f"{who} was killed by {signal_name}; the last failing call was {cause}, which {outcome}{tail}"
    elif exit_code is None:
        text = f"{who} has not exited in this trace; the last failing call was {cause}, which {outcome}{tail}"
    elif exit_code == 0:
        text = f"{who} exited with code 0; the last failing call, {cause}, {outcome}{tail}, and the program carried on"
    else:
        text = f"{who} exited with code {exit_code} because {cause} {outcome}{tail}"
    said = [t for p, t in stderr_writes(trace) if p == culprit.pid]
    if said:
        snippet = said[-1].splitlines()[-1] if said[-1].splitlines() else said[-1]
        text += f'; it wrote to stderr: "{snippet[:140]}"'
    return Verdict(text, culprit.pid, culprit.name, culprit.errno, culprit.target, attempts, exit_code, signal_name, HINTS.get(culprit.errno or ""))


def render(trace: Trace, top: int = 10, include_low: bool = False) -> str:
    procs = processes(trace)
    lines = [f"tracelens: {trace.source}: {trace.lines} lines, {len(trace.calls)} calls, {len([p for p in procs if not p.thread])} process(es), {len(trace.unparsed)} unparsed"]
    result = verdict(trace)
    lines.append(f"verdict: {result.text}")
    if result.hint:
        lines.append(f"hint ({result.errno}): {result.hint}")
    if procs:
        lines.append("")
        lines.append("processes")
        for proc in procs:
            depth = 0
            parent = proc.parent
            seen = set()
            while parent is not None and parent not in seen:
                seen.add(parent)
                depth += 1
                parent = next((p.parent for p in procs if p.pid == parent), None)
            command = " ".join(proc.argv) if proc.argv else (proc.program or "?")
            ending = f"exit {proc.exit_code}" if proc.exit_code is not None else (f"killed by {proc.signal}" if proc.signal else ("thread" if proc.thread else "no exit recorded"))
            lines.append(f"  {'  ' * depth}{pid_label(proc.pid):<12} {command[:60]:<60} {ending}")
    groups = failures_by_errno(trace, include_low)
    hidden = sum(1 for c in trace.calls if c.failed and is_low_signal(c))
    lines.append("")
    note = f" ({hidden} low-signal hidden; --all shows them)" if hidden and not include_low else ""
    lines.append(f"failures by errno: {sum(g.calls for g in groups)} calls in {len(groups)} group(s){note}")
    for group in groups:
        label = f"{group.errno} ({group.text})" if group.text else group.errno
        lines.append(f"  {label}: {group.calls} call(s){' [low signal]' if group.low_signal else ''}")
        for entry in group.targets[:top]:
            pids = ",".join(str(p) if p is not None else "?" for p in entry.pids)
            lines.append(f"    {entry.syscall:<12} {(entry.target or '-')[:70]:<70} x{entry.count:<4} pid {pids}")
        if len(group.targets) > top:
            lines.append(f"    ... {len(group.targets) - top} more target(s)")
    slow = slowest(trace, top)
    if slow:
        lines.append("")
        lines.append("slowest calls (-T)")
        for call in slow:
            note = "  waits for another process or I/O" if call.name in BLOCKING_CALLS else (f"  {call.errno}" if call.failed else "")
            what = f"{call.name}({(call.target or '')[:50]})"
            lines.append(f"  {call.duration:10.6f}s  {what:<64} {pid_label(call.pid)}{note}")
    rows = histogram(trace)
    if rows:
        lines.append("")
        timed = rows[0].seconds is not None
        lines.append("syscall histogram (like strace -c)")
        lines.append("  % time     seconds  calls  errors  syscall" if timed else "  calls  errors  syscall")
        for row in rows[: max(top, 15)]:
            if timed:
                lines.append(f"  {row.percent:6.2f}  {row.seconds:10.6f}  {row.calls:5d}  {row.errors:6d}  {row.syscall}")
            else:
                lines.append(f"  {row.calls:5d}  {row.errors:6d}  {row.syscall}")
        if len(rows) > max(top, 15):
            lines.append(f"  ... {len(rows) - max(top, 15)} more syscall(s)")
    opened = opened_files(trace)
    if opened:
        lines.append("")
        lines.append("files opened per process")
        for pid, paths in opened.items():
            proc = next((p for p in procs if p.pid == pid), None)
            lines.append(f"  {program_label(proc, pid)}: {len(paths)} opened" + (f", showing {top}" if len(paths) > top else ""))
            for path in paths[:top]:
                lines.append(f"    {path}")
    sigs = signals(trace)
    messages = [e.detail for e in trace.events if e.kind == "message"]
    if sigs or messages:
        lines.append("")
        if sigs:
            lines.append("signals: " + ", ".join(f"{name} x{n}" for name, n in sigs.items()))
        for message in messages:
            lines.append(f"strace: {message}")
    return "\n".join(lines)


def to_json(trace: Trace, top: int = 10, include_low: bool = False) -> dict:
    return {
        "source": trace.source,
        "lines": trace.lines,
        "calls": len(trace.calls),
        "unparsed": len(trace.unparsed),
        "verdict": asdict(verdict(trace)),
        "exits": [{"pid": e.pid, "code": e.code, "signal": e.detail if e.kind == "killed" else None, "line": e.line} for e in trace.exits],
        "processes": [asdict(p) for p in processes(trace)],
        "failures": [asdict(g) for g in failures_by_errno(trace, include_low)],
        "slowest": [{"syscall": c.name, "target": c.target, "duration": c.duration, "pid": c.pid, "errno": c.errno, "blocking": c.name in BLOCKING_CALLS, "line": c.line} for c in slowest(trace, top)],
        "histogram": [asdict(r) for r in histogram(trace)],
        "files": {("main" if pid is None else str(pid)): paths for pid, paths in opened_files(trace).items()},
        "stderr": [{"pid": pid, "text": text} for pid, text in stderr_writes(trace)],
        "signals": signals(trace),
        "messages": [e.detail for e in trace.events if e.kind == "message"],
    }


def strace_command(command: Sequence[str], trace_set: str, output: str) -> List[str]:
    return ["strace", "-f", "-tt", "-T", "-y", "-s", "200", "-o", output, "-e", f"trace={trace_set}"] + list(command)


def run_strace(command: Sequence[str], trace_set: str = DEFAULT_TRACE_SET, output: Optional[str] = None, runner: Callable = subprocess.run, which: Callable[[str], Optional[str]] = shutil.which) -> Tuple[str, int]:
    if which("strace") is None:
        raise RuntimeError("strace not found; Debian/Ubuntu: sudo apt-get install -y strace; Alpine: apk add strace; macOS has no strace, run this inside a Linux container")
    path = output
    if path is None:
        handle, path = tempfile.mkstemp(prefix="tracelens-", suffix=".txt")
        os.close(handle)
    completed = runner(strace_command(command, trace_set, path))
    with open(path, encoding="utf-8", errors="replace") as source:
        text = source.read()
    if output is None:
        os.unlink(path)
    return text, completed.returncode


def main(argv: Optional[Sequence[str]] = None, run: Callable = run_strace) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    command: Optional[List[str]] = None
    if "--run" in argv:
        at = argv.index("--run")
        command = argv[at + 1:]
        argv = argv[:at]
        if command and command[0] == "--":
            command = command[1:]
    parser = argparse.ArgumentParser(prog="tracelens", description=__doc__, epilog="tracelens --run [--] CMD ARGS...  runs CMD under strace (Linux) and analyses the output")
    parser.add_argument("trace", nargs="?", help="strace output file, or - for stdin (omit with --run)")
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    parser.add_argument("--verdict", action="store_true", help="print only the one-line verdict")
    parser.add_argument("--all", action="store_true", help="include low-signal errnos (EAGAIN, EINTR, ENOSYS, EINVAL...) in the failure list")
    parser.add_argument("--top", type=int, default=10, help="rows per section")
    parser.add_argument("--trace-set", default=DEFAULT_TRACE_SET, help="strace -e trace= set used by --run")
    parser.add_argument("--out", help="with --run: keep the raw strace output in this file")
    args = parser.parse_args(argv)
    if command is not None:
        if not command:
            parser.error("--run needs a command")
        try:
            text, _code = run(command, args.trace_set, args.out)
        except RuntimeError as exc:
            print(f"tracelens: {exc}", file=sys.stderr)
            return 2
        source = args.out or f"strace of {' '.join(command)}"
    elif args.trace is None:
        parser.error("pass a trace file, - for stdin, or --run CMD")
    elif args.trace == "-":
        text, source = sys.stdin.read(), "stdin"
    else:
        try:
            with open(args.trace, encoding="utf-8", errors="replace") as handle:
                text = handle.read()
        except OSError as exc:
            print(f"tracelens: cannot read {args.trace}: {exc.strerror}", file=sys.stderr)
            return 1
        source = args.trace
    trace = parse(text, source)
    if not trace.calls and not trace.events:
        print(f"tracelens: no strace lines found in {source}; expected lines like: openat(AT_FDCWD, \"/etc/app.yml\", O_RDONLY) = -1 ENOENT (No such file or directory)", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(to_json(trace, args.top, args.all), indent=2))
    elif args.verdict:
        print(verdict(trace).text)
    else:
        print(render(trace, args.top, args.all))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
