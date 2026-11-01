"""usetop: sample /proc twice, turn the counters into utilization, saturation and errors per resource (the USE method) and say which resource, if any, is saturated."""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from dataclasses import asdict, dataclass, field
from typing import Callable, Dict, List, Optional, Sequence, Tuple

SKIP_DEVICES = ("loop", "ram", "sr", "fd", "zram")
SECTOR_BYTES = 512
RESOURCES = ("cpu", "memory", "io")
THRESHOLDS = {
    "cpu_busy_pct": 80.0,
    "steal_pct": 5.0,
    "psi_some_pct": 10.0,
    "psi_full_pct": 5.0,
    "memory_available_pct": 10.0,
    "disk_busy_pct": 80.0,
}


@dataclass
class CpuTimes:
    user: int = 0
    nice: int = 0
    system: int = 0
    idle: int = 0
    iowait: int = 0
    irq: int = 0
    softirq: int = 0
    steal: int = 0
    guest: int = 0
    guest_nice: int = 0

    @property
    def total(self) -> int:
        return self.user + self.nice + self.system + self.idle + self.iowait + self.irq + self.softirq + self.steal


@dataclass
class DiskTimes:
    reads: int = 0
    reads_merged: int = 0
    sectors_read: int = 0
    ms_reading: int = 0
    writes: int = 0
    writes_merged: int = 0
    sectors_written: int = 0
    ms_writing: int = 0
    in_flight: int = 0
    ms_busy: int = 0
    ms_weighted: int = 0


@dataclass
class ProcTimes:
    pid: int
    comm: str
    state: str
    utime: int
    stime: int
    threads: int
    rss_pages: int


@dataclass
class Sample:
    taken_at: float
    cpus: Dict[str, CpuTimes]
    procs_running: int
    procs_blocked: int
    context_switches: int
    load: Tuple[float, float, float]
    meminfo: Dict[str, int]
    vmstat: Dict[str, int]
    pressure: Dict[str, Dict[str, Dict[str, float]]]
    disks: Dict[str, DiskTimes]
    processes: Dict[int, ProcTimes]


@dataclass
class CpuUsage:
    busy: float
    user: float
    system: float
    iowait: float
    steal: float
    idle: float
    irq: float


@dataclass
class MemoryView:
    total_kb: int
    available_kb: int
    available_pct: float
    free_kb: int
    cached_kb: int
    dirty_kb: int
    writeback_kb: int
    swap_total_kb: int
    swap_used_kb: int
    swap_in_kb_per_s: float
    swap_out_kb_per_s: float
    major_faults_per_s: float
    oom_kills: int
    oom_kills_total: int


@dataclass
class PressureView:
    resource: str
    some_avg10: float
    full_avg10: float
    some_window_pct: float
    full_window_pct: float


@dataclass
class DiskUsage:
    device: str
    reads_per_s: float
    writes_per_s: float
    read_kb_per_s: float
    write_kb_per_s: float
    busy_pct: float
    queue: float
    await_ms: float
    in_flight: int


@dataclass
class ProcUsage:
    pid: int
    comm: str
    state: str
    cpu_pct: float
    rss_mb: float
    threads: int


@dataclass
class UseRow:
    resource: str
    utilization: str
    saturation: str
    errors: str


@dataclass
class Report:
    elapsed: float
    ncpu: int
    load: Tuple[float, float, float]
    run_queue: int
    blocked: int
    context_switches_per_s: float
    cpu: CpuUsage
    per_cpu: Dict[str, CpuUsage]
    memory: MemoryView
    pressure: Dict[str, PressureView]
    disks: Dict[str, DiskUsage]
    top: List[ProcUsage]
    blocked_processes: List[ProcUsage]
    rows: List[UseRow] = field(default_factory=list)
    verdicts: List[str] = field(default_factory=list)


def parse_stat(text: str) -> Tuple[Dict[str, CpuTimes], Dict[str, int]]:
    cpus: Dict[str, CpuTimes] = {}
    counters: Dict[str, int] = {}
    for line in text.splitlines():
        parts = line.split()
        if not parts:
            continue
        if parts[0].startswith("cpu"):
            values = [int(v) for v in parts[1:11]]
            values += [0] * (10 - len(values))
            cpus[parts[0]] = CpuTimes(*values)
        elif len(parts) > 1 and parts[0] in ("ctxt", "processes", "procs_running", "procs_blocked", "btime"):
            counters[parts[0]] = int(parts[1])
    if "cpu" not in cpus:
        raise ValueError("stat: no aggregate 'cpu' line")
    return cpus, counters


def parse_loadavg(text: str) -> Tuple[Tuple[float, float, float], int, int]:
    parts = text.split()
    if len(parts) < 4 or "/" not in parts[3]:
        raise ValueError("loadavg: expected '1min 5min 15min running/total lastpid'")
    running, _, total = parts[3].partition("/")
    return (float(parts[0]), float(parts[1]), float(parts[2])), int(running), int(total)


def parse_meminfo(text: str) -> Dict[str, int]:
    info: Dict[str, int] = {}
    for line in text.splitlines():
        key, sep, rest = line.partition(":")
        fields = rest.split()
        if sep and fields and fields[0].isdigit():
            info[key.strip()] = int(fields[0])
    return info


def parse_vmstat(text: str) -> Dict[str, int]:
    counters: Dict[str, int] = {}
    for line in text.splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[1].lstrip("-").isdigit():
            counters[parts[0]] = int(parts[1])
    return counters


def parse_pressure(text: str) -> Dict[str, Dict[str, float]]:
    lines: Dict[str, Dict[str, float]] = {}
    for line in text.splitlines():
        parts = line.split()
        if len(parts) < 2 or parts[0] not in ("some", "full"):
            continue
        values: Dict[str, float] = {}
        for item in parts[1:]:
            key, sep, value = item.partition("=")
            if sep:
                values[key] = float(value)
        lines[parts[0]] = values
    return lines


def parse_diskstats(text: str) -> Dict[str, DiskTimes]:
    disks: Dict[str, DiskTimes] = {}
    for line in text.splitlines():
        parts = line.split()
        if len(parts) < 14:
            continue
        disks[parts[2]] = DiskTimes(*[int(v) for v in parts[3:14]])
    return disks


def is_partition(name: str, names: Sequence[str]) -> bool:
    for other in names:
        if other == name or not name.startswith(other):
            continue
        rest = name[len(other):]
        if other[-1].isdigit():
            if rest.startswith("p") and rest[1:].isdigit():
                return True
        elif rest.isdigit():
            return True
    return False


def whole_disks(disks: Dict[str, DiskTimes]) -> Dict[str, DiskTimes]:
    names = list(disks)
    return {name: times for name, times in disks.items() if not name.startswith(SKIP_DEVICES) and not is_partition(name, names)}


def parse_pid_stat(text: str) -> ProcTimes:
    head, sep, rest = text.rpartition(")")
    if not sep:
        raise ValueError("stat: missing (comm) field")
    pid_text, _, comm = head.partition("(")
    fields = rest.split()
    if len(fields) < 22:
        raise ValueError("stat: too few fields after comm")
    return ProcTimes(int(pid_text), comm, fields[0], int(fields[11]), int(fields[12]), int(fields[17]), int(fields[21]))


def read_text(path: str) -> str:
    try:
        with open(path, encoding="utf-8", errors="replace") as handle:
            return handle.read()
    except OSError:
        return ""


def read_processes(root: str) -> Dict[int, ProcTimes]:
    processes: Dict[int, ProcTimes] = {}
    for name in os.listdir(root):
        if not name.isdigit():
            continue
        text = read_text(os.path.join(root, name, "stat"))
        if not text:
            continue
        try:
            proc = parse_pid_stat(text)
        except ValueError:
            continue
        processes[proc.pid] = proc
    return processes


def sample(root: str = "/proc", now: Callable[[], float] = time.monotonic) -> Sample:
    stat_text = read_text(os.path.join(root, "stat"))
    if not stat_text:
        raise FileNotFoundError(f"{os.path.join(root, 'stat')} is missing or unreadable")
    cpus, counters = parse_stat(stat_text)
    load, _, _ = parse_loadavg(read_text(os.path.join(root, "loadavg")) or "0 0 0 0/0 0")
    pressure: Dict[str, Dict[str, Dict[str, float]]] = {}
    for resource in RESOURCES:
        text = read_text(os.path.join(root, "pressure", resource))
        if text:
            pressure[resource] = parse_pressure(text)
    uptime = read_text(os.path.join(root, "uptime")).split()
    taken_at = float(uptime[0]) if uptime else now()
    return Sample(
        taken_at,
        cpus,
        counters.get("procs_running", 0),
        counters.get("procs_blocked", 0),
        counters.get("ctxt", 0),
        load,
        parse_meminfo(read_text(os.path.join(root, "meminfo"))),
        parse_vmstat(read_text(os.path.join(root, "vmstat"))),
        pressure,
        parse_diskstats(read_text(os.path.join(root, "diskstats"))),
        read_processes(root),
    )


def ncpus(sample_: Sample) -> int:
    return len([name for name in sample_.cpus if name != "cpu"]) or 1


def cpu_usage(before: CpuTimes, after: CpuTimes) -> CpuUsage:
    total = after.total - before.total
    if total <= 0:
        return CpuUsage(0.0, 0.0, 0.0, 0.0, 0.0, 100.0, 0.0)

    def delta(*names: str) -> int:
        return sum(getattr(after, n) - getattr(before, n) for n in names)

    def share(*names: str) -> float:
        return round(delta(*names) / total * 100, 2)

    busy = round((total - delta("idle", "iowait")) / total * 100, 2)
    return CpuUsage(busy, share("user", "nice"), share("system"), share("iowait"), share("steal"), share("idle"), share("irq", "softirq"))


def memory_view(before: Sample, after: Sample, elapsed: float, page_kb: int = 4) -> MemoryView:
    info = after.meminfo
    total = info.get("MemTotal", 0)
    available = info.get("MemAvailable", info.get("MemFree", 0))
    swap_total = info.get("SwapTotal", 0)

    def rate(key: str) -> float:
        return (after.vmstat.get(key, 0) - before.vmstat.get(key, 0)) / elapsed

    return MemoryView(
        total,
        available,
        round(available / total * 100, 2) if total else 0.0,
        info.get("MemFree", 0),
        info.get("Cached", 0),
        info.get("Dirty", 0),
        info.get("Writeback", 0),
        swap_total,
        swap_total - info.get("SwapFree", 0),
        round(rate("pswpin") * page_kb, 1),
        round(rate("pswpout") * page_kb, 1),
        round(rate("pgmajfault"), 1),
        after.vmstat.get("oom_kill", 0) - before.vmstat.get("oom_kill", 0),
        after.vmstat.get("oom_kill", 0),
    )


def pressure_view(resource: str, before: Sample, after: Sample, elapsed: float) -> Optional[PressureView]:
    now_lines = after.pressure.get(resource)
    if not now_lines:
        return None
    then_lines = before.pressure.get(resource, {})

    def window(kind: str) -> float:
        stalled_us = now_lines.get(kind, {}).get("total", 0.0) - then_lines.get(kind, {}).get("total", 0.0)
        return round(max(stalled_us, 0.0) / (elapsed * 1_000_000) * 100, 2)

    return PressureView(resource, now_lines.get("some", {}).get("avg10", 0.0), now_lines.get("full", {}).get("avg10", 0.0), window("some"), window("full"))


def disk_usage(device: str, before: DiskTimes, after: DiskTimes, elapsed: float) -> DiskUsage:
    reads = after.reads - before.reads
    writes = after.writes - before.writes
    waited_ms = (after.ms_reading - before.ms_reading) + (after.ms_writing - before.ms_writing)
    elapsed_ms = elapsed * 1000
    return DiskUsage(
        device,
        round(reads / elapsed, 1),
        round(writes / elapsed, 1),
        round((after.sectors_read - before.sectors_read) * SECTOR_BYTES / 1024 / elapsed, 1),
        round((after.sectors_written - before.sectors_written) * SECTOR_BYTES / 1024 / elapsed, 1),
        round(min(100.0, (after.ms_busy - before.ms_busy) / elapsed_ms * 100), 2),
        round((after.ms_weighted - before.ms_weighted) / elapsed_ms, 2),
        round(waited_ms / (reads + writes), 2) if reads + writes else 0.0,
        after.in_flight,
    )


def process_usage(before: Sample, after: Sample, elapsed: float, hz: int = 100, page_kb: int = 4) -> List[ProcUsage]:
    usages: List[ProcUsage] = []
    for pid, proc in after.processes.items():
        previous = before.processes.get(pid)
        if previous is None:
            continue
        ticks = (proc.utime + proc.stime) - (previous.utime + previous.stime)
        usages.append(ProcUsage(pid, proc.comm, proc.state, round(ticks / hz / elapsed * 100, 1), round(proc.rss_pages * page_kb / 1024, 1), proc.threads))
    return usages


def top_processes(usages: Sequence[ProcUsage], limit: int = 5) -> List[ProcUsage]:
    return sorted(usages, key=lambda u: (-u.cpu_pct, u.pid))[:limit]


def blocked_processes(usages: Sequence[ProcUsage]) -> List[ProcUsage]:
    return sorted((u for u in usages if u.state == "D"), key=lambda u: u.pid)


def busiest_disk(disks: Dict[str, DiskUsage]) -> Optional[DiskUsage]:
    if not disks:
        return None
    return max(disks.values(), key=lambda d: (d.busy_pct, d.queue))


def use_rows(report: Report) -> List[UseRow]:
    cpu, mem = report.cpu, report.memory
    cpu_sat = f"run queue {report.run_queue} on {report.ncpu} cpus, load {report.load[0]:.2f}"
    if "cpu" in report.pressure:
        cpu_sat += f"; PSI some {report.pressure['cpu'].some_avg10:.1f}%"
    rows = [UseRow("CPU", f"{cpu.busy:.0f}% (us {cpu.user:.0f} sy {cpu.system:.0f} wa {cpu.iowait:.0f} st {cpu.steal:.0f})", cpu_sat, "none in /proc; check dmesg, mcelog")]
    mem_sat = f"swap in {mem.swap_in_kb_per_s:.0f} out {mem.swap_out_kb_per_s:.0f} kB/s, major faults {mem.major_faults_per_s:.0f}/s"
    if "memory" in report.pressure:
        mem_sat += f"; PSI full {report.pressure['memory'].full_avg10:.1f}%"
    rows.append(UseRow("Memory", f"{100 - mem.available_pct:.0f}% used, {mem.available_pct:.0f}% available of {mem.total_kb // 1024} MB", mem_sat, f"oom_kill +{mem.oom_kills} (total {mem.oom_kills_total})"))
    for index, disk in enumerate(sorted(report.disks.values(), key=lambda d: (-d.busy_pct, d.device))):
        sat = f"queue {disk.queue:.1f}, await {disk.await_ms:.1f} ms, {disk.in_flight} in flight"
        if index == 0 and "io" in report.pressure:
            sat += f"; PSI io full {report.pressure['io'].full_avg10:.1f}%"
        if index == 0 and report.blocked_processes:
            sat += f"; {len(report.blocked_processes)} in D state"
        throughput = (disk.read_kb_per_s + disk.write_kb_per_s) / 1024
        rows.append(UseRow(f"Disk {disk.device}", f"{disk.busy_pct:.0f}% busy ({disk.reads_per_s:.0f} r/s {disk.writes_per_s:.0f} w/s, {throughput:.1f} MB/s)", sat, "none in /proc; check dmesg, smartctl"))
    return rows


def verdicts(report: Report) -> List[str]:
    found: List[str] = []
    cpu, mem = report.cpu, report.memory
    cpu_psi = report.pressure["cpu"].some_avg10 if "cpu" in report.pressure else None
    if report.run_queue > report.ncpu or (cpu_psi is not None and cpu_psi >= THRESHOLDS["psi_some_pct"]):
        text = f"CPU saturated: run queue {report.run_queue} on {report.ncpu} cpus"
        if cpu_psi is not None:
            text += f", PSI cpu some {cpu_psi:.0f}%"
        found.append(text)
    elif cpu.busy >= THRESHOLDS["cpu_busy_pct"]:
        found.append(f"CPU busy, not saturated: {cpu.busy:.0f}% utilization, run queue {report.run_queue} on {report.ncpu} cpus")
    if cpu.steal >= THRESHOLDS["steal_pct"]:
        found.append(f"CPU steal {cpu.steal:.0f}%: the hypervisor is not scheduling this VM when it is runnable; nothing inside the VM fixes that")
    mem_psi = report.pressure["memory"].full_avg10 if "memory" in report.pressure else None
    swapping = mem.swap_in_kb_per_s > 0 or mem.swap_out_kb_per_s > 0
    if mem.available_pct < THRESHOLDS["memory_available_pct"] or (mem_psi is not None and mem_psi >= THRESHOLDS["psi_full_pct"]) or swapping:
        text = f"Memory saturated: {mem.available_pct:.0f}% available"
        if mem_psi is not None:
            text += f", PSI memory full {mem_psi:.0f}%"
        if swapping:
            text += f", swap in/out {mem.swap_in_kb_per_s:.0f}/{mem.swap_out_kb_per_s:.0f} kB/s"
        if mem.oom_kills:
            text += f", {mem.oom_kills} OOM kill(s)"
        found.append(text)
    elif mem.oom_kills:
        found.append(f"Memory errors: oom_kill rose by {mem.oom_kills} with {mem.available_pct:.0f}% available; a cgroup limit ran out, not the host")
    disk = busiest_disk(report.disks)
    io_psi = report.pressure["io"].full_avg10 if "io" in report.pressure else None
    if disk and (disk.busy_pct >= THRESHOLDS["disk_busy_pct"] or (io_psi is not None and io_psi >= THRESHOLDS["psi_full_pct"])):
        text = f"Disk saturated: {disk.device} {disk.busy_pct:.0f}% busy, queue {disk.queue:.1f}, await {disk.await_ms:.1f} ms"
        if io_psi is not None:
            text += f", PSI io full {io_psi:.0f}%"
        found.append(text)
    if report.blocked_processes:
        names = ", ".join(f"{p.comm} ({p.pid})" for p in report.blocked_processes[:4])
        found.append(f"{len(report.blocked_processes)} process(es) in D state waiting on I/O: {names}")
    if not found:
        disk_text = f", busiest disk {disk.device} {disk.busy_pct:.0f}%" if disk else ""
        found.append(f"No saturation: CPU {cpu.busy:.0f}%, memory {mem.available_pct:.0f}% available{disk_text}")
    return found


def analyse(before: Sample, after: Sample, top: int = 5, hz: int = 100, page_kb: int = 4) -> Report:
    elapsed = max(after.taken_at - before.taken_at, 1e-9)
    per_cpu = {name: cpu_usage(before.cpus.get(name, CpuTimes()), times) for name, times in after.cpus.items() if name != "cpu"}
    pressure = {}
    for resource in RESOURCES:
        view = pressure_view(resource, before, after, elapsed)
        if view:
            pressure[resource] = view
    disks = {name: disk_usage(name, before.disks.get(name, DiskTimes()), times, elapsed) for name, times in whole_disks(after.disks).items()}
    usages = process_usage(before, after, elapsed, hz, page_kb)
    report = Report(
        round(elapsed, 2),
        ncpus(after),
        after.load,
        after.procs_running,
        after.procs_blocked,
        round((after.context_switches - before.context_switches) / elapsed, 1),
        cpu_usage(before.cpus["cpu"], after.cpus["cpu"]),
        per_cpu,
        memory_view(before, after, elapsed, page_kb),
        pressure,
        disks,
        top_processes(usages, top),
        blocked_processes(usages),
    )
    report.rows = use_rows(report)
    report.verdicts = verdicts(report)
    return report


def render(report: Report) -> str:
    load = " ".join(f"{value:.2f}" for value in report.load)
    lines = [f"usetop: {report.elapsed:.1f}s window, {report.ncpu} cpus, load {load}, {report.context_switches_per_s:.0f} context switches/s"]
    widths = (14, 46, 62)
    lines.append(f"{'Resource':<{widths[0]}} {'Utilization':<{widths[1]}} {'Saturation':<{widths[2]}} Errors")
    for row in report.rows:
        lines.append(f"{row.resource:<{widths[0]}} {row.utilization:<{widths[1]}} {row.saturation:<{widths[2]}} {row.errors}")
    if report.pressure:
        parts = [f"{name} some {view.some_avg10:.1f}% full {view.full_avg10:.1f}% (this window: {view.some_window_pct:.1f}% / {view.full_window_pct:.1f}%)" for name, view in report.pressure.items()]
        lines.append("PSI avg10:  " + " | ".join(parts))
    else:
        lines.append("PSI: not available (kernel without CONFIG_PSI, or booted with psi=0)")
    if report.top:
        lines.append("top by CPU:")
        for proc in report.top:
            lines.append(f"  {proc.pid:>7} {proc.comm:<16} {proc.state} {proc.cpu_pct:>6.1f}% {proc.rss_mb:>9.1f} MB {proc.threads:>4} thr")
    if report.blocked_processes:
        lines.append("D state (uninterruptible, waiting on I/O):")
        for proc in report.blocked_processes:
            lines.append(f"  {proc.pid:>7} {proc.comm:<16} {proc.cpu_pct:>6.1f}%")
    lines.append("VERDICT: " + " | ".join(report.verdicts))
    return "\n".join(lines)


def to_json(report: Report) -> dict:
    return asdict(report)


def runtime_constants() -> Tuple[int, int]:
    try:
        return int(os.sysconf("SC_CLK_TCK")), int(os.sysconf("SC_PAGE_SIZE")) // 1024
    except (AttributeError, OSError, ValueError):
        return 100, 4


def watch(take: Callable[[], Sample], interval: float, count: int, sleep: Callable[[float], None] = time.sleep, emit: Callable[[str], None] = print, as_json: bool = False, top: int = 5, hz: int = 100, page_kb: int = 4) -> List[Report]:
    previous = take()
    reports: List[Report] = []
    rounds = 0
    try:
        while count <= 0 or rounds < count:
            sleep(interval)
            current = take()
            report = analyse(previous, current, top, hz, page_kb)
            reports.append(report)
            emit(json.dumps(to_json(report)) if as_json else render(report))
            previous = current
            rounds += 1
    except KeyboardInterrupt:
        pass
    return reports


def replay(directory: str, emit: Callable[[str], None] = print, as_json: bool = False, top: int = 5) -> List[Report]:
    snapshots = sorted(os.path.join(directory, name) for name in os.listdir(directory) if os.path.isdir(os.path.join(directory, name)))
    if len(snapshots) < 2:
        raise ValueError(f"{directory}: need at least two snapshot directories (found {len(snapshots)})")
    for path in snapshots:
        if not os.path.isfile(os.path.join(path, "stat")):
            raise ValueError(f"{path} is not a /proc snapshot (no stat file); pass the directory that holds t0, t1, ...")
    samples = [sample(path) for path in snapshots]
    reports = [analyse(before, after, top) for before, after in zip(samples, samples[1:])]
    for report in reports:
        emit(json.dumps(to_json(report)) if as_json else render(report))
    return reports


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="usetop", description=__doc__)
    parser.add_argument("--interval", type=float, default=1.0, metavar="SECONDS", help="seconds between the two samples of each round")
    parser.add_argument("--count", type=int, default=1, help="rounds to print (0 = until Ctrl-C)")
    parser.add_argument("--top", type=int, default=5, help="processes to list by CPU")
    parser.add_argument("--json", action="store_true", help="machine-readable output, one JSON document per round")
    parser.add_argument("--root", default="/proc", help="procfs root (a fake tree in tests)")
    parser.add_argument("--replay", metavar="DIR", help="analyse recorded snapshots (sorted subdirectories of DIR) instead of the live /proc")
    args = parser.parse_args(list(sys.argv[1:] if argv is None else argv))
    if args.replay:
        try:
            replay(args.replay, as_json=args.json, top=args.top)
        except (OSError, ValueError) as exc:
            print(f"usetop: {exc}", file=sys.stderr)
            return 1
        return 0
    if not os.path.isfile(os.path.join(args.root, "stat")):
        print(f"usetop: {args.root}/stat not found; this needs Linux procfs (on macOS try: --replay code/fixtures/saturated)", file=sys.stderr)
        return 2
    if args.interval <= 0:
        parser.error("--interval must be positive")
    hz, page_kb = runtime_constants()
    watch(lambda: sample(args.root), args.interval, args.count, as_json=args.json, top=args.top, hz=hz, page_kb=page_kb)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
