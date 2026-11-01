"""cgtool: create, populate, measure and explain cgroup v2 groups by reading and writing the files under /sys/fs/cgroup."""
from __future__ import annotations

import argparse
import json
import os
import signal
import sys
import time
from dataclasses import asdict, dataclass, field
from typing import Callable, Dict, List, Optional, Sequence, Tuple

DEFAULT_ROOT = "/sys/fs/cgroup"
DEFAULT_PROC = "/proc"
PERIOD_USEC = 100000
UNITS: Dict[str, int] = {"": 1, "K": 1024, "M": 1024 ** 2, "G": 1024 ** 3}
CONTROLLER_FOR_LIMIT: Dict[str, str] = {"cpu": "cpu", "memory": "memory", "pids": "pids"}
OPTIONAL_FILES = ("memory.swap.max",)
ENABLE_HINT = "echo +{controller} > {path}"


@dataclass
class Limits:
    cpu: Optional[str] = None
    memory: Optional[str] = None
    pids: Optional[str] = None

    def controllers(self) -> List[str]:
        return [CONTROLLER_FOR_LIMIT[name] for name in ("cpu", "memory", "pids") if getattr(self, name) is not None]


@dataclass
class Plan:
    group: str
    mkdirs: List[str] = field(default_factory=list)
    enables: List[Tuple[str, str]] = field(default_factory=list)
    writes: List[Tuple[str, str]] = field(default_factory=list)
    problems: List[str] = field(default_factory=list)


@dataclass
class GroupStats:
    name: str
    path: str
    controllers: List[str]
    subtree_control: List[str]
    populated: Optional[bool]
    procs: List[int]
    cpu_max: Optional[str]
    cpu_weight: Optional[int]
    cpu_stat: Dict[str, int]
    memory_current: Optional[int]
    memory_peak: Optional[int]
    memory_max: Optional[str]
    memory_high: Optional[str]
    memory_swap_current: Optional[int]
    memory_swap_max: Optional[str]
    memory_events: Dict[str, int]
    memory_oom_group: Optional[int]
    pids_current: Optional[int]
    pids_max: Optional[str]
    pids_events: Dict[str, int]
    taken_at: float


@dataclass
class Level:
    name: str
    cpu_max: Optional[str]
    cpu_weight: Optional[int]
    memory_max: Optional[str]
    memory_high: Optional[str]
    memory_swap_max: Optional[str]
    pids_max: Optional[str]
    memory_current: Optional[int]
    procs: int


@dataclass
class Explanation:
    pid: int
    comm: str
    path: str
    levels: List[Level]
    effective: Dict[str, str]
    notes: List[str]


@dataclass
class TopRow:
    name: str
    cpu_percent: Optional[float]
    cpu_seconds: float
    throttled_percent: Optional[float]
    memory_current: int
    pids_current: int


def parse_size(value: str) -> int:
    text = value.strip().upper()
    if text.endswith("B"):
        text = text[:-1]
    digits = text.rstrip("KMG")
    unit = text[len(digits):]
    if not digits.isdigit() or unit not in UNITS:
        raise ValueError(f"invalid size {value!r}; use a number with K, M or G, e.g. 200M")
    return int(digits) * UNITS[unit]


def memory_setting(value: str) -> str:
    return "max" if value.strip().lower() == "max" else str(parse_size(value))


def pids_setting(value: str) -> str:
    text = value.strip().lower()
    if text == "max":
        return "max"
    if not text.isdigit() or int(text) < 1:
        raise ValueError(f"invalid pids limit {value!r}; use a positive integer or max")
    return text


def cpu_max_setting(value: str, period: int = PERIOD_USEC) -> str:
    text = value.strip().lower()
    if text == "max":
        return f"max {period}"
    try:
        cpus = float(text)
    except ValueError:
        raise ValueError(f"invalid cpu limit {value!r}; use a number of CPUs such as 0.5 or 2, or max") from None
    if cpus <= 0:
        raise ValueError(f"cpu limit must be above 0, got {value}")
    return f"{int(round(cpus * period))} {period}"


def parse_cpu_max(text: str) -> Tuple[Optional[int], int]:
    parts = text.split()
    if not parts:
        return None, PERIOD_USEC
    quota = None if parts[0] == "max" else int(parts[0])
    period = int(parts[1]) if len(parts) > 1 else PERIOD_USEC
    return quota, period


def cpus_from_cpu_max(text: Optional[str]) -> Optional[float]:
    if text is None:
        return None
    quota, period = parse_cpu_max(text)
    return None if quota is None else quota / period


def millicores_to_cpu_max(millicores: int, period: int = PERIOD_USEC) -> str:
    return f"{millicores * period // 1000} {period}"


def shares_to_weight(shares: int) -> int:
    shares = min(max(shares, 2), 262144)
    return 1 + ((shares - 2) * 9999) // 262142


def parse_kv(text: Optional[str]) -> Dict[str, int]:
    values: Dict[str, int] = {}
    for line in (text or "").splitlines():
        parts = line.split()
        if len(parts) == 2 and (parts[1].isdigit() or parts[1].lstrip("-").isdigit()):
            values[parts[0]] = int(parts[1])
    return values


def parse_limit(text: Optional[str]) -> Optional[int]:
    if text is None or text.strip() == "max":
        return None
    return int(text.strip().split()[0])


def parse_proc_cgroup(text: str) -> Tuple[Optional[str], List[str]]:
    v2_path = None
    v1_lines: List[str] = []
    for line in text.splitlines():
        parts = line.strip().split(":", 2)
        if len(parts) != 3:
            continue
        hierarchy, controllers, path = parts
        if hierarchy == "0" and controllers == "":
            v2_path = path
        else:
            v1_lines.append(line.strip())
    return v2_path, v1_lines


def human_size(size: Optional[int]) -> str:
    if size is None:
        return "max"
    value = float(size)
    for unit in ("B", "K", "M", "G"):
        if value < 1024 or unit == "G":
            return f"{int(value)}B" if unit == "B" else f"{value:.1f}{unit}"
        value /= 1024
    return f"{value:.1f}G"


def limit_text(text: Optional[str], kind: str = "size") -> str:
    if text is None:
        return "(file missing)"
    value = parse_limit(text)
    if value is None:
        return "max"
    return human_size(value) if kind == "size" else str(value)


def read_text(path: str) -> Optional[str]:
    try:
        with open(path, encoding="utf-8", errors="replace") as handle:
            return handle.read()
    except OSError:
        return None


def read_int(path: str) -> Optional[int]:
    text = read_text(path)
    if text is None:
        return None
    text = text.strip()
    return int(text) if text.lstrip("-").isdigit() else None


def read_limit(path: str) -> Optional[str]:
    text = read_text(path)
    return None if text is None else text.strip()


def read_words(path: str) -> List[str]:
    return (read_text(path) or "").split()


def group_path(root: str, name: str) -> str:
    cleaned = name.strip().strip("/")
    parts = [part for part in cleaned.split("/") if part]
    if any(part in (".", "..") or part.startswith("cgroup.") for part in parts):
        raise ValueError(f"invalid cgroup name {name!r}")
    return os.path.join(root, *parts) if parts else root


def relative_name(root: str, path: str) -> str:
    rel = os.path.relpath(path, root)
    return "/" if rel == "." else "/" + rel.replace(os.sep, "/")


def check_root(root: str) -> Optional[str]:
    if not os.path.isdir(root):
        return f"{root} not found; cgroup v2 lives under /sys/fs/cgroup on Linux (on macOS run the tests, or use --root with a fake tree)"
    if not os.path.exists(os.path.join(root, "cgroup.controllers")):
        return f"{root} has no cgroup.controllers file, so it is not a cgroup v2 root; a cgroup v1 (legacy) mount has one directory per controller such as {root}/memory; boot with systemd.unified_cgroup_hierarchy=1"
    return None


def ancestors(root: str, group: str) -> List[str]:
    chain: List[str] = []
    current = os.path.dirname(group)
    while len(current) >= len(root):
        chain.append(current)
        if current == root:
            break
        current = os.path.dirname(current)
    return list(reversed(chain))


def plan_create(root: str, name: str, limits: Limits) -> Plan:
    group = group_path(root, name)
    plan = Plan(group)
    if group == root:
        plan.problems.append("refusing to change the root cgroup; give the group a name")
        return plan
    needed = limits.controllers()
    available = set(read_words(os.path.join(root, "cgroup.controllers")))
    for controller in needed:
        if controller not in available:
            plan.problems.append(f"controller {controller} is not available in {root}/cgroup.controllers; the kernel or the mount does not offer it (check: mount | grep cgroup)")
    if plan.problems:
        return plan
    for ancestor in ancestors(root, group):
        if not os.path.isdir(ancestor):
            plan.mkdirs.append(ancestor)
            enabled: set = set()
        else:
            enabled = set(read_words(os.path.join(ancestor, "cgroup.subtree_control")))
        missing = [controller for controller in needed if controller not in enabled]
        if not missing:
            continue
        procs = read_words(os.path.join(ancestor, "cgroup.procs")) if os.path.isdir(ancestor) else []
        if procs and ancestor != root:
            plan.problems.append(
                f"cannot enable {' '.join(missing)} in {relative_name(root, ancestor)}: it holds {len(procs)} process(es), and a cgroup cannot both contain processes and hand controllers to children (the no-internal-processes rule); move them into a leaf group or pick another parent"
            )
            continue
        control = os.path.join(ancestor, "cgroup.subtree_control")
        plan.enables.append((control, " ".join(f"+{controller}" for controller in missing)))
    if plan.problems:
        return plan
    if not os.path.isdir(group):
        plan.mkdirs.append(group)
    if limits.cpu is not None:
        plan.writes.append((os.path.join(group, "cpu.max"), cpu_max_setting(limits.cpu)))
    if limits.memory is not None:
        plan.writes.append((os.path.join(group, "memory.max"), memory_setting(limits.memory)))
        plan.writes.append((os.path.join(group, "memory.swap.max"), "0"))
    if limits.pids is not None:
        plan.writes.append((os.path.join(group, "pids.max"), pids_setting(limits.pids)))
    return plan


def render_plan(root: str, plan: Plan) -> str:
    lines = [f"group {relative_name(root, plan.group)}"]
    for problem in plan.problems:
        lines.append(f"  problem: {problem}")
    for path in plan.mkdirs:
        lines.append(f"  mkdir {path}")
    for path, value in plan.enables:
        lines.append(f"  enable {value} in {path}  ({ENABLE_HINT.format(controller=value.replace('+', '').replace(' ', ' +'), path=path)})")
    for path, value in plan.writes:
        lines.append(f"  write {value!r} to {path}")
    return "\n".join(lines)


def write_file(path: str, value: str) -> None:
    with open(path, "w", encoding="ascii") as handle:
        handle.write(value)


def apply_plan(plan: Plan, log: Callable[[str], None] = lambda line: None) -> List[str]:
    errors: List[str] = []
    for path in plan.mkdirs:
        try:
            os.makedirs(path, exist_ok=True)
        except OSError as exc:
            errors.append(f"mkdir {path}: {exc.strerror}" + ("; run as root" if isinstance(exc, PermissionError) else ""))
            return errors
    for path, value in plan.enables + plan.writes:
        if os.path.basename(path) in OPTIONAL_FILES and not os.path.exists(path):
            log(f"skip {path}: swap accounting is off on this kernel, so memory.max is already a hard ceiling")
            continue
        try:
            write_file(path, value)
            log(f"wrote {value!r} to {path}")
        except OSError as exc:
            hint = ""
            if isinstance(exc, PermissionError):
                hint = "; run as root, or delegate the parent group to your user"
            elif exc.errno == 16:
                hint = "; EBUSY means the no-internal-processes rule: a group with processes cannot enable controllers for children, and a group that enables them cannot take processes"
            elif exc.errno == 22:
                hint = f"; EINVAL means the kernel rejected {value!r} for this file"
            errors.append(f"write {value!r} to {path}: {exc.strerror}{hint}")
    return errors


def plan_run(root: str, name: str) -> List[str]:
    group = group_path(root, name)
    if not os.path.isdir(group):
        return [f"no cgroup {relative_name(root, group)}; create it first: cgtool create {name.strip('/')} --cpu 0.5"]
    distributed = read_words(os.path.join(group, "cgroup.subtree_control"))
    if distributed and group != root:
        return [f"{relative_name(root, group)} hands {' '.join(distributed)} to its children (cgroup.subtree_control), so it cannot hold processes itself; run in a leaf such as {name.strip('/')}/leaf"]
    return []


def attach(root: str, name: str, pid: int) -> str:
    target = os.path.join(group_path(root, name), "cgroup.procs")
    write_file(target, str(pid))
    return target


def exit_code_from_status(status: int) -> int:
    if os.WIFSIGNALED(status):
        return 128 + os.WTERMSIG(status)
    return os.WEXITSTATUS(status)


def default_spawn(command: Sequence[str], before_exec: Callable[[], None]) -> int:
    pid = os.fork()
    if pid == 0:
        try:
            before_exec()
            os.execvp(command[0], list(command))
        except OSError as exc:
            print(f"cgtool: cannot start {command[0]}: {exc}", file=sys.stderr, flush=True)
            os._exit(127)
    return pid


def run_in_group(
    root: str,
    name: str,
    command: Sequence[str],
    spawn: Callable[[Sequence[str], Callable[[], None]], int] = default_spawn,
    waitpid: Callable[[int, int], Tuple[int, int]] = os.waitpid,
    log: Callable[[str], None] = lambda line: None,
) -> int:
    problems = plan_run(root, name)
    if problems:
        for problem in problems:
            log(problem)
        return 2
    group = group_path(root, name)
    before = parse_kv(read_text(os.path.join(group, "memory.events")))
    pid = spawn(command, lambda: attach(root, name, os.getpid()))
    _, status = waitpid(pid, 0)
    code = exit_code_from_status(status)
    after = parse_kv(read_text(os.path.join(group, "memory.events")))
    killed = after.get("oom_kill", 0) - before.get("oom_kill", 0)
    if killed > 0:
        log(f"exit {code}: the kernel OOM-killed {killed} process(es) in {relative_name(root, group)} (memory.events oom_kill +{killed}, memory.max {limit_text(read_limit(os.path.join(group, 'memory.max')))})")
    elif code > 128:
        log(f"exit {code}: killed by signal {code - 128}")
    else:
        log(f"exit {code}")
    return code


def read_group(root: str, name: str, now: Callable[[], float] = time.time) -> GroupStats:
    group = group_path(root, name)
    if not os.path.isdir(group):
        raise FileNotFoundError(f"no cgroup {relative_name(root, group)} under {root}")
    events = parse_kv(read_text(os.path.join(group, "cgroup.events")))
    return GroupStats(
        name=relative_name(root, group),
        path=group,
        controllers=read_words(os.path.join(group, "cgroup.controllers")),
        subtree_control=read_words(os.path.join(group, "cgroup.subtree_control")),
        populated=None if "populated" not in events else bool(events["populated"]),
        procs=[int(p) for p in read_words(os.path.join(group, "cgroup.procs")) if p.isdigit()],
        cpu_max=read_limit(os.path.join(group, "cpu.max")),
        cpu_weight=read_int(os.path.join(group, "cpu.weight")),
        cpu_stat=parse_kv(read_text(os.path.join(group, "cpu.stat"))),
        memory_current=read_int(os.path.join(group, "memory.current")),
        memory_peak=read_int(os.path.join(group, "memory.peak")),
        memory_max=read_limit(os.path.join(group, "memory.max")),
        memory_high=read_limit(os.path.join(group, "memory.high")),
        memory_swap_current=read_int(os.path.join(group, "memory.swap.current")),
        memory_swap_max=read_limit(os.path.join(group, "memory.swap.max")),
        memory_events=parse_kv(read_text(os.path.join(group, "memory.events"))),
        memory_oom_group=read_int(os.path.join(group, "memory.oom.group")),
        pids_current=read_int(os.path.join(group, "pids.current")),
        pids_max=read_limit(os.path.join(group, "pids.max")),
        pids_events=parse_kv(read_text(os.path.join(group, "pids.events"))),
        taken_at=now(),
    )


def verdicts(stats: GroupStats) -> List[str]:
    lines: List[str] = []
    periods = stats.cpu_stat.get("nr_periods", 0)
    throttled = stats.cpu_stat.get("nr_throttled", 0)
    cpus = cpus_from_cpu_max(stats.cpu_max)
    if throttled > 0 and periods > 0:
        waited = stats.cpu_stat.get("throttled_usec", 0) / 1e6
        lines.append(f"CPU: throttled in {throttled * 100 / periods:.0f}% of {periods} periods, {waited:.2f}s spent waiting for quota (cpu.max {stats.cpu_max}); the quota is the bottleneck, not the machine")
    elif cpus is not None:
        lines.append(f"CPU: quota of {cpus:g} CPU(s) never hit")
    if "cpu" not in stats.controllers and stats.cpu_max is None:
        lines.append("CPU: controller not enabled here, so there is no cpu.max to hit")
    kills = stats.memory_events.get("oom_kill", 0)
    hits = stats.memory_events.get("max", 0)
    highs = stats.memory_events.get("high", 0)
    if kills > 0:
        lines.append(f"memory: {kills} OOM kill(s) at memory.max {limit_text(stats.memory_max)}; an exit code 137 in this group was the kernel, not the application")
    elif hits > 0:
        lines.append(f"memory: memory.max {limit_text(stats.memory_max)} was reached {hits} time(s) and reclaim kept the group alive; the next spike may be a kill")
    if highs > 0:
        lines.append(f"memory: memory.high {limit_text(stats.memory_high)} crossed {highs} time(s); the group is being slowed by reclaim before any kill")
    limit = parse_limit(stats.memory_max)
    if limit and stats.memory_current is not None and stats.memory_current >= limit * 0.9:
        lines.append(f"memory: {human_size(stats.memory_current)} of {human_size(limit)} in use ({stats.memory_current * 100 / limit:.0f}%)")
    if limit and stats.memory_swap_max is not None and parse_limit(stats.memory_swap_max) != 0:
        lines.append(f"memory: memory.swap.max is {limit_text(stats.memory_swap_max)}, so the group can page out past memory.max; set it to 0 for a hard ceiling")
    failures = stats.pids_events.get("max", 0)
    pids_limit = parse_limit(stats.pids_max)
    if failures > 0:
        lines.append(f"pids: fork() failed {failures} time(s) with EAGAIN at pids.max {limit_text(stats.pids_max, 'count')}")
    elif pids_limit and stats.pids_current is not None and stats.pids_current >= pids_limit:
        lines.append(f"pids: at the limit ({stats.pids_current}/{pids_limit}); the next fork() returns EAGAIN")
    if stats.populated is False:
        lines.append("no processes in this group right now; counters are history")
    return lines


def render_stat(stats: GroupStats) -> str:
    usage = stats.cpu_stat.get("usage_usec", 0) / 1e6
    lines = [
        f"cgroup {stats.name}  procs {len(stats.procs)}  controllers {' '.join(stats.controllers) or '-'}  subtree_control {' '.join(stats.subtree_control) or '-'}",
        f"  cpu.max {stats.cpu_max or '(file missing)'}  cpu.weight {stats.cpu_weight if stats.cpu_weight is not None else '-'}  usage {usage:.3f}s  periods {stats.cpu_stat.get('nr_periods', 0)}  throttled {stats.cpu_stat.get('nr_throttled', 0)}  throttled_usec {stats.cpu_stat.get('throttled_usec', 0)}",
        f"  memory.current {human_size(stats.memory_current) if stats.memory_current is not None else '-'}  peak {human_size(stats.memory_peak) if stats.memory_peak is not None else '-'}  max {limit_text(stats.memory_max)}  high {limit_text(stats.memory_high)}  swap.max {limit_text(stats.memory_swap_max)}  events {' '.join(f'{k}={v}' for k, v in stats.memory_events.items()) or '-'}",
        f"  pids.current {stats.pids_current if stats.pids_current is not None else '-'}  pids.max {limit_text(stats.pids_max, 'count')}  events {' '.join(f'{k}={v}' for k, v in stats.pids_events.items()) or '-'}",
    ]
    for line in verdicts(stats):
        lines.append(f"  -> {line}")
    return "\n".join(lines)


def stats_to_json(stats: GroupStats) -> dict:
    data = asdict(stats)
    data["cpus"] = cpus_from_cpu_max(stats.cpu_max)
    data["verdicts"] = verdicts(stats)
    return data


def diff_stats(before: GroupStats, after: GroupStats) -> dict:
    elapsed = max(after.taken_at - before.taken_at, 1e-9)
    usage = after.cpu_stat.get("usage_usec", 0) - before.cpu_stat.get("usage_usec", 0)
    return {
        "elapsed": elapsed,
        "cpu_percent": usage / (elapsed * 1e6) * 100,
        "throttled_usec": after.cpu_stat.get("throttled_usec", 0) - before.cpu_stat.get("throttled_usec", 0),
        "nr_throttled": after.cpu_stat.get("nr_throttled", 0) - before.cpu_stat.get("nr_throttled", 0),
        "memory_current": after.memory_current,
        "memory_delta": (after.memory_current or 0) - (before.memory_current or 0),
        "oom_kill": after.memory_events.get("oom_kill", 0) - before.memory_events.get("oom_kill", 0),
        "pids_current": after.pids_current,
        "pids_failures": after.pids_events.get("max", 0) - before.pids_events.get("max", 0),
    }


def render_delta(delta: dict) -> str:
    sign = "+" if delta["memory_delta"] >= 0 else "-"
    parts = [
        f"{delta['elapsed']:.1f}s: cpu {delta['cpu_percent']:.0f}%",
        f"throttled {delta['nr_throttled']} period(s) / {delta['throttled_usec'] / 1e6:.2f}s",
        f"memory {human_size(delta['memory_current']) if delta['memory_current'] is not None else '-'} ({sign}{human_size(abs(delta['memory_delta']))})",
        f"pids {delta['pids_current'] if delta['pids_current'] is not None else '-'}",
    ]
    if delta["oom_kill"]:
        parts.append(f"OOM kills +{delta['oom_kill']}")
    if delta["pids_failures"]:
        parts.append(f"fork failures +{delta['pids_failures']}")
    return "  ".join(parts)


def watch_group(
    take: Callable[[], GroupStats],
    interval: float,
    count: int,
    sleep: Callable[[float], None] = time.sleep,
    emit: Callable[[str], None] = print,
    as_json: bool = False,
) -> List[dict]:
    previous = take()
    emit(json.dumps(stats_to_json(previous)) if as_json else render_stat(previous))
    deltas: List[dict] = []
    rounds = 0
    try:
        while count <= 0 or rounds < count:
            sleep(interval)
            current = take()
            delta = diff_stats(previous, current)
            deltas.append(delta)
            emit(json.dumps(delta) if as_json else render_delta(delta))
            previous = current
            rounds += 1
    except KeyboardInterrupt:
        pass
    return deltas


def walk_groups(root: str, depth: Optional[int] = None) -> List[str]:
    names: List[str] = []
    for current, dirs, files in os.walk(root):
        dirs.sort()
        if "cgroup.procs" not in files:
            dirs[:] = []
            continue
        name = relative_name(root, current)
        level = 0 if name == "/" else name.count("/")
        names.append(name)
        if depth is not None and level >= depth:
            dirs[:] = []
    return names


def sample_tree(root: str, depth: Optional[int] = None, now: Callable[[], float] = time.time) -> Dict[str, GroupStats]:
    return {name: read_group(root, name, now) for name in walk_groups(root, depth)}


def top_rows(before: Dict[str, GroupStats], after: Dict[str, GroupStats], sort: str = "memory", limit: int = 15) -> List[TopRow]:
    rows: List[TopRow] = []
    for name, stats in after.items():
        earlier = before.get(name)
        cpu_percent = None
        throttled_percent = None
        if earlier is not None and stats.taken_at > earlier.taken_at:
            elapsed = stats.taken_at - earlier.taken_at
            cpu_percent = (stats.cpu_stat.get("usage_usec", 0) - earlier.cpu_stat.get("usage_usec", 0)) / (elapsed * 1e6) * 100
            periods = stats.cpu_stat.get("nr_periods", 0) - earlier.cpu_stat.get("nr_periods", 0)
            if periods > 0:
                throttled_percent = (stats.cpu_stat.get("nr_throttled", 0) - earlier.cpu_stat.get("nr_throttled", 0)) * 100 / periods
        rows.append(TopRow(name, cpu_percent, stats.cpu_stat.get("usage_usec", 0) / 1e6, throttled_percent, stats.memory_current or 0, stats.pids_current or len(stats.procs)))
    keys = {
        "memory": lambda row: row.memory_current,
        "cpu": lambda row: row.cpu_percent if row.cpu_percent is not None else row.cpu_seconds,
        "pids": lambda row: row.pids_current,
    }
    if sort not in keys:
        raise ValueError(f"sort must be one of {sorted(keys)}")
    rows.sort(key=keys[sort], reverse=True)
    return rows[:limit]


def render_top(rows: Sequence[TopRow]) -> str:
    lines = [f"{'cgroup':<48} {'cpu%':>6} {'cpu s':>9} {'thr%':>5} {'memory':>9} {'pids':>5}"]
    for row in rows:
        cpu = f"{row.cpu_percent:.0f}" if row.cpu_percent is not None else "-"
        thr = f"{row.throttled_percent:.0f}" if row.throttled_percent is not None else "-"
        name = row.name if len(row.name) <= 48 else "..." + row.name[-45:]
        lines.append(f"{name:<48} {cpu:>6} {row.cpu_seconds:>9.2f} {thr:>5} {human_size(row.memory_current):>9} {row.pids_current:>5}")
    return "\n".join(lines)


def explain_pid(pid: int, proc_root: str = DEFAULT_PROC, root: str = DEFAULT_ROOT) -> Explanation:
    text = read_text(os.path.join(proc_root, str(pid), "cgroup"))
    if text is None:
        raise FileNotFoundError(f"no readable {proc_root}/{pid}/cgroup")
    comm = (read_text(os.path.join(proc_root, str(pid), "comm")) or "?").strip()
    path, v1_lines = parse_proc_cgroup(text)
    notes: List[str] = []
    if v1_lines:
        notes.append(f"{len(v1_lines)} cgroup v1 (legacy) line(s) in /proc/{pid}/cgroup: this host runs a hybrid or v1 hierarchy; v2 is the current model")
    if path is None:
        return Explanation(pid, comm, "", [], {}, notes + ["no cgroup v2 membership (0::/path line) found"])
    if not os.path.isdir(group_path(root, path)):
        notes.append(f"{path} is not visible under {root}; a container in its own cgroup namespace sees 0::/ while the host sees the full path")
    levels: List[Level] = []
    parts = [part for part in path.split("/") if part]
    chain = [""] + ["/".join(parts[: index + 1]) for index in range(len(parts))]
    for name in chain:
        group = group_path(root, name)
        if not os.path.isdir(group):
            continue
        levels.append(
            Level(
                name=relative_name(root, group),
                cpu_max=read_limit(os.path.join(group, "cpu.max")),
                cpu_weight=read_int(os.path.join(group, "cpu.weight")),
                memory_max=read_limit(os.path.join(group, "memory.max")),
                memory_high=read_limit(os.path.join(group, "memory.high")),
                memory_swap_max=read_limit(os.path.join(group, "memory.swap.max")),
                pids_max=read_limit(os.path.join(group, "pids.max")),
                memory_current=read_int(os.path.join(group, "memory.current")),
                procs=len(read_words(os.path.join(group, "cgroup.procs"))),
            )
        )
    return Explanation(pid, comm, path, levels, effective_limits(levels), notes)


def effective_limits(levels: Sequence[Level]) -> Dict[str, str]:
    effective: Dict[str, str] = {}
    best_cpu: Optional[Tuple[float, str, str]] = None
    best_mem: Optional[Tuple[int, str, str]] = None
    best_pids: Optional[Tuple[int, str, str]] = None
    for level in levels:
        cpus = cpus_from_cpu_max(level.cpu_max)
        if cpus is not None and (best_cpu is None or cpus < best_cpu[0]):
            best_cpu = (cpus, level.cpu_max or "", level.name)
        mem = parse_limit(level.memory_max)
        if mem is not None and (best_mem is None or mem < best_mem[0]):
            best_mem = (mem, level.memory_max or "", level.name)
        pids = parse_limit(level.pids_max)
        if pids is not None and (best_pids is None or pids < best_pids[0]):
            best_pids = (pids, level.pids_max or "", level.name)
    effective["cpu_max"] = f"{best_cpu[1]} ({best_cpu[0]:g} CPU, set at {best_cpu[2]})" if best_cpu else "max (no CPU quota anywhere in the chain)"
    effective["memory_max"] = f"{human_size(best_mem[0])} (set at {best_mem[2]})" if best_mem else "max (no memory ceiling anywhere in the chain)"
    effective["pids_max"] = f"{best_pids[0]} (set at {best_pids[2]})" if best_pids else "max (no pids ceiling anywhere in the chain)"
    return effective


def render_explain(exp: Explanation) -> str:
    lines = [f"pid {exp.pid} ({exp.comm}) is in cgroup {exp.path or '?'}"]
    for level in exp.levels:
        lines.append(
            f"  {level.name:<40} cpu.max {level.cpu_max or '-':<14} cpu.weight {level.cpu_weight if level.cpu_weight is not None else '-':<5} memory.max {limit_text(level.memory_max):<8} memory.high {limit_text(level.memory_high):<8} swap.max {limit_text(level.memory_swap_max):<8} pids.max {limit_text(level.pids_max, 'count'):<6} current {human_size(level.memory_current) if level.memory_current is not None else '-':<8} procs {level.procs}"
        )
    for key, value in exp.effective.items():
        lines.append(f"  effective {key}: {value}")
    for note in exp.notes:
        lines.append(f"  note: {note}")
    return "\n".join(lines)


def subtree(group: str) -> List[str]:
    found: List[str] = []
    for current, dirs, files in os.walk(group):
        dirs.sort()
        if current == group or "cgroup.procs" in files:
            found.append(current)
        else:
            dirs[:] = []
    return found


def default_kill(group: str) -> None:
    kill_file = os.path.join(group, "cgroup.kill")
    if os.path.exists(kill_file):
        write_file(kill_file, "1")
        return
    for pid in read_words(os.path.join(group, "cgroup.procs")):
        try:
            os.kill(int(pid), signal.SIGKILL)
        except (ProcessLookupError, ValueError):
            pass


def delete_group(
    root: str,
    name: str,
    kill: Callable[[str], None] = default_kill,
    rmdir: Callable[[str], None] = os.rmdir,
    sleep: Callable[[float], None] = time.sleep,
    timeout: float = 5.0,
) -> List[str]:
    group = group_path(root, name)
    if group == root:
        raise ValueError("refusing to delete the root cgroup")
    subgroups = subtree(group)
    if not subgroups:
        raise FileNotFoundError(f"no cgroup {relative_name(root, group)} under {root}")
    subgroups.sort(key=lambda path: path.count(os.sep), reverse=True)
    for path in subgroups:
        kill(path)
    waited = 0.0
    while any(read_words(os.path.join(path, "cgroup.procs")) for path in subgroups) and waited < timeout:
        sleep(0.05)
        waited += 0.05
    removed: List[str] = []
    for path in subgroups:
        rmdir(path)
        removed.append(path)
    return removed


def add_common(parser: argparse.ArgumentParser, top_level: bool) -> None:
    parser.add_argument("--root", default=DEFAULT_ROOT if top_level else argparse.SUPPRESS, help="cgroup v2 root (a fake tree in tests)")
    parser.add_argument("--proc", default=DEFAULT_PROC if top_level else argparse.SUPPRESS, help="procfs root (a fake tree in tests)")
    parser.add_argument("--json", action="store_true", default=False if top_level else argparse.SUPPRESS, help="machine-readable output")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="cgtool", description=__doc__)
    add_common(parser, True)
    sub = parser.add_subparsers(dest="command", required=True)

    create = sub.add_parser("create", help="create a group and write its limits")
    create.add_argument("name")
    create.add_argument("--cpu", help="CPUs, e.g. 0.5 (cpu.max '50000 100000') or max")
    create.add_argument("--memory", help="memory.max, e.g. 200M or max; memory.swap.max is set to 0")
    create.add_argument("--pids", help="pids.max, e.g. 50 or max")
    create.add_argument("--dry-run", action="store_true", help="print the plan without touching the tree")

    run = sub.add_parser("run", help="start a command inside an existing group")
    run.add_argument("name")
    run.add_argument("cmd", nargs=argparse.REMAINDER)

    stat = sub.add_parser("stat", help="limits, usage and events of one group with a verdict")
    stat.add_argument("name")
    stat.add_argument("--watch", type=float, metavar="SECONDS", help="sample every SECONDS and print deltas")
    stat.add_argument("--count", type=int, default=0, help="number of --watch rounds (0 = until Ctrl-C)")

    top = sub.add_parser("top", help="walk the tree and show the busiest groups")
    top.add_argument("--interval", type=float, default=0.0, help="seconds between two samples for cpu%% (0 = one sample)")
    top.add_argument("--sort", default="memory", choices=("memory", "cpu", "pids"))
    top.add_argument("--limit", type=int, default=15)
    top.add_argument("--depth", type=int, help="how deep to walk")

    explain = sub.add_parser("explain", help="the cgroup chain of a PID and the limits at each level")
    explain.add_argument("pid", type=int)

    delete = sub.add_parser("delete", help="kill every process in a group and remove it with its children")
    delete.add_argument("name")
    for child in (create, run, stat, top, explain, delete):
        add_common(child, False)
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(list(sys.argv[1:] if argv is None else argv))
    err = lambda line: print(f"cgtool: {line}", file=sys.stderr)
    problem = check_root(args.root)
    if problem:
        err(problem)
        return 2
    try:
        if args.command == "create":
            if args.cpu is None and args.memory is None and args.pids is None:
                err("nothing to set; pass --cpu, --memory and/or --pids")
                return 2
            plan = plan_create(args.root, args.name, Limits(args.cpu, args.memory, args.pids))
            if args.dry_run or plan.problems:
                print(json.dumps(asdict(plan), indent=2) if args.json else render_plan(args.root, plan))
                return 1 if plan.problems else 0
            errors = apply_plan(plan, log=lambda line: None if args.json else print(line))
            if errors:
                for line in errors:
                    err(line)
                return 1
            if args.json:
                print(json.dumps(stats_to_json(read_group(args.root, args.name)), indent=2))
            return 0
        if args.command == "run":
            command = list(args.cmd)
            if command and command[0] == "--":
                command = command[1:]
            if not command:
                err("pass a command after --")
                return 2
            if sys.platform != "linux":
                err("run needs Linux; on this machine use create --dry-run, stat and explain against a fake --root")
                return 2
            return run_in_group(args.root, args.name, command, log=err)
        if args.command == "stat":
            if args.watch:
                watch_group(lambda: read_group(args.root, args.name), args.watch, args.count, as_json=args.json)
                return 0
            stats = read_group(args.root, args.name)
            print(json.dumps(stats_to_json(stats), indent=2) if args.json else render_stat(stats))
            return 0
        if args.command == "top":
            before = sample_tree(args.root, args.depth)
            after = before
            if args.interval > 0:
                time.sleep(args.interval)
                after = sample_tree(args.root, args.depth)
            rows = top_rows(before, after, args.sort, args.limit)
            print(json.dumps([asdict(row) for row in rows], indent=2) if args.json else render_top(rows))
            return 0
        if args.command == "explain":
            exp = explain_pid(args.pid, args.proc, args.root)
            print(json.dumps(asdict(exp), indent=2) if args.json else render_explain(exp))
            return 0
        if args.command == "delete":
            removed = delete_group(args.root, args.name)
            print(json.dumps(removed) if args.json else "\n".join(f"removed {path}" for path in removed))
            return 0
    except (ValueError, FileNotFoundError) as exc:
        err(str(exc))
        return 1
    except PermissionError as exc:
        err(f"{exc}; run as root")
        return 1
    except OSError as exc:
        err(f"{exc.strerror}: {exc.filename}; a cgroup can only be removed once cgroup.procs is empty and every child group is gone (cgroup.events shows populated 0)")
        return 1
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
