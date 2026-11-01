"""fdwatch: read a process's open file descriptors from /proc, classify them, measure them against the limit and catch leaks between snapshots."""
from __future__ import annotations

import argparse
import json
import os
import re
import stat
import sys
import time
from collections import Counter
from dataclasses import asdict, dataclass
from typing import Callable, Dict, List, Optional, Sequence, Tuple

PSEUDO = re.compile(r"^(socket|pipe|anon_inode):\[?([^\]]*)\]?$")
DELETED_SUFFIX = " (deleted)"
ANON_KINDS = {
    "eventpoll": "epoll",
    "eventfd": "eventfd",
    "timerfd": "timerfd",
    "signalfd": "signalfd",
    "inotify": "inotify",
    "io_uring": "io_uring",
    "pidfd": "pidfd",
    "bpf-map": "bpf",
    "bpf-prog": "bpf",
}
FLAG_NAMES = ((0o2000000, "cloexec"), (0o4000, "nonblock"), (0o2000, "append"))
PATH_KINDS = ("file", "directory", "device")


@dataclass
class Descriptor:
    fd: int
    target: str
    kind: str
    deleted: bool = False
    size: Optional[int] = None
    flags: Optional[int] = None
    position: Optional[int] = None


@dataclass
class Snapshot:
    pid: int
    comm: str
    descriptors: List[Descriptor]
    soft_limit: Optional[int]
    hard_limit: Optional[int]
    taken_at: float

    @property
    def count(self) -> int:
        return len(self.descriptors)

    @property
    def usage(self) -> Optional[float]:
        return self.count / self.soft_limit if self.soft_limit else None


@dataclass
class Delta:
    elapsed: float
    before: int
    after: int
    by_kind: Dict[str, int]
    rate_per_minute: float
    new_targets: List[str]
    verdict: str
    seconds_to_limit: Optional[float]


def classify(target: str) -> Tuple[str, bool]:
    deleted = target.endswith(DELETED_SUFFIX)
    if deleted:
        target = target[: -len(DELETED_SUFFIX)]
    match = PSEUDO.match(target)
    if match:
        family, detail = match.group(1), match.group(2)
        if family == "anon_inode":
            return ANON_KINDS.get(detail, "anon_inode"), deleted
        return family, deleted
    if target.startswith("/dev/"):
        return "device", deleted
    return "file", deleted


def describe_flags(flags: int) -> str:
    access = ("r", "w", "rw")[flags & 0o3] if (flags & 0o3) < 3 else "?"
    words = [access] + [name for bit, name in FLAG_NAMES if flags & bit]
    return " ".join(words)


def parse_limits(text: str) -> Tuple[Optional[int], Optional[int]]:
    for line in text.splitlines():
        if line.startswith("Max open files"):
            parts = line.split()
            if len(parts) < 5:
                break
            soft, hard = parts[3], parts[4]
            return (None if soft == "unlimited" else int(soft)), (None if hard == "unlimited" else int(hard))
    return None, None


def parse_fdinfo(text: str) -> Dict[str, int]:
    info: Dict[str, int] = {}
    for line in text.splitlines():
        key, _, value = line.partition(":")
        value = value.strip()
        if key == "pos" and value.isdigit():
            info["pos"] = int(value)
        elif key == "flags" and value:
            info["flags"] = int(value, 8)
    return info


def read_text(path: str) -> str:
    try:
        with open(path, encoding="utf-8", errors="replace") as handle:
            return handle.read()
    except OSError:
        return ""


def refine_with_stat(descriptor: Descriptor, link: str) -> None:
    try:
        st = os.stat(link)
    except OSError:
        return
    mode = st.st_mode
    if stat.S_ISDIR(mode):
        descriptor.kind = "directory"
    elif stat.S_ISREG(mode):
        descriptor.kind = "file"
        descriptor.size = st.st_size
    elif stat.S_ISCHR(mode) or stat.S_ISBLK(mode):
        descriptor.kind = "device"
    elif stat.S_ISFIFO(mode):
        descriptor.kind = "pipe"
    elif stat.S_ISSOCK(mode):
        descriptor.kind = "socket"


def snapshot(pid: int, root: str = "/proc", now: Callable[[], float] = time.time) -> Snapshot:
    base = os.path.join(root, str(pid))
    fd_dir = os.path.join(base, "fd")
    if not os.path.isdir(fd_dir):
        raise FileNotFoundError(f"no readable process {pid} under {root}")
    comm = read_text(os.path.join(base, "comm")).strip() or "?"
    soft, hard = parse_limits(read_text(os.path.join(base, "limits")))
    descriptors: List[Descriptor] = []
    names = sorted((n for n in os.listdir(fd_dir) if n.isdigit()), key=int)
    for name in names:
        link = os.path.join(fd_dir, name)
        try:
            target = os.readlink(link)
        except OSError:
            continue
        kind, deleted = classify(target)
        descriptor = Descriptor(int(name), target[: -len(DELETED_SUFFIX)] if deleted else target, kind, deleted)
        if kind in PATH_KINDS:
            refine_with_stat(descriptor, link)
        info = parse_fdinfo(read_text(os.path.join(base, "fdinfo", name)))
        descriptor.flags = info.get("flags")
        descriptor.position = info.get("pos")
        descriptors.append(descriptor)
    return Snapshot(pid, comm, descriptors, soft, hard, now())


def kinds(snap: Snapshot) -> Dict[str, int]:
    counter = Counter(d.kind + (" (deleted)" if d.deleted else "") for d in snap.descriptors)
    return dict(counter.most_common())


def top_targets(snap: Snapshot, limit: int = 5) -> List[Tuple[str, int]]:
    counter = Counter(d.target for d in snap.descriptors if d.kind in PATH_KINDS)
    return counter.most_common(limit)


def deleted_open(snap: Snapshot) -> List[Descriptor]:
    return [d for d in snap.descriptors if d.deleted]


def without_cloexec(snap: Snapshot) -> List[Descriptor]:
    return [d for d in snap.descriptors if d.flags is not None and not d.flags & 0o2000000 and d.fd > 2]


def diff(before: Snapshot, after: Snapshot) -> Delta:
    elapsed = max(after.taken_at - before.taken_at, 1e-9)
    by_kind = Counter(kinds(after))
    by_kind.subtract(Counter(kinds(before)))
    changes = {kind: delta for kind, delta in by_kind.items() if delta}
    growth = after.count - before.count
    rate_per_minute = growth / elapsed * 60
    seen = {d.target for d in before.descriptors}
    new_targets = sorted({d.target for d in after.descriptors if d.target not in seen and d.kind in PATH_KINDS})[:5]
    verdict = "growing" if growth > 0 else "shrinking" if growth < 0 else "stable"
    seconds_to_limit = None
    if growth > 0 and after.soft_limit:
        seconds_to_limit = (after.soft_limit - after.count) / (growth / elapsed)
    return Delta(elapsed, before.count, after.count, changes, rate_per_minute, new_targets, verdict, seconds_to_limit)


def human_size(size: Optional[int]) -> str:
    if size is None:
        return "?"
    value = float(size)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{int(value)} B" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} GB"


def human_duration(seconds: float) -> str:
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m {seconds % 60:02d}s"
    return f"{seconds // 3600}h {(seconds % 3600) // 60:02d}m"


def render(snap: Snapshot, top: int = 5) -> str:
    limit = f"soft limit {snap.soft_limit}" if snap.soft_limit else "soft limit unlimited"
    usage = f" ({snap.usage * 100:.1f}%)" if snap.usage is not None else ""
    hard = f", hard limit {snap.hard_limit if snap.hard_limit else 'unlimited'}"
    lines = [f"pid {snap.pid} ({snap.comm}): {snap.count} open file descriptors, {limit}{usage}{hard}"]
    if snap.usage is not None and snap.usage >= 0.8:
        lines.append(f"WARNING: within {100 - snap.usage * 100:.0f}% of the soft limit; the next open() may fail with EMFILE")
    for kind, count in kinds(snap).items():
        lines.append(f"  {kind:<20} {count:>6}")
    targets = [(t, c) for t, c in top_targets(snap, top) if c > 1]
    if targets:
        lines.append("same path open more than once:")
        for target, count in targets:
            lines.append(f"  {count:>6}  {target}")
    deleted = deleted_open(snap)
    if deleted:
        lines.append("deleted but still open (space is freed only when these close):")
        for d in deleted:
            lines.append(f"  fd {d.fd:<4} {human_size(d.size):>10}  {d.target}")
    leaking = without_cloexec(snap)
    if leaking:
        lines.append(f"{len(leaking)} descriptor(s) without O_CLOEXEC would be inherited by any child this process execs")
    return "\n".join(lines)


def render_delta(delta: Delta) -> str:
    sign = "+" if delta.after >= delta.before else ""
    parts = [f"{delta.before} -> {delta.after} ({sign}{delta.after - delta.before}) in {human_duration(delta.elapsed)}: {delta.verdict}"]
    if delta.by_kind:
        parts.append("  " + ", ".join(f"{kind} {'+' if n > 0 else ''}{n}" for kind, n in sorted(delta.by_kind.items())))
    if delta.verdict == "growing":
        parts.append(f"  rate {delta.rate_per_minute:.1f}/min")
        if delta.seconds_to_limit is not None:
            parts.append(f"  soft limit reached in about {human_duration(delta.seconds_to_limit)} at this rate")
        for target in delta.new_targets:
            parts.append(f"  new: {target}")
    return "\n".join(parts)


def to_json(snap: Snapshot) -> dict:
    return {
        "pid": snap.pid,
        "comm": snap.comm,
        "count": snap.count,
        "soft_limit": snap.soft_limit,
        "hard_limit": snap.hard_limit,
        "usage": snap.usage,
        "kinds": kinds(snap),
        "top_targets": [{"target": t, "count": c} for t, c in top_targets(snap)],
        "deleted": [{"fd": d.fd, "target": d.target, "size": d.size} for d in deleted_open(snap)],
        "without_cloexec": [d.fd for d in without_cloexec(snap)],
        "descriptors": [asdict(d) for d in snap.descriptors],
    }


def watch(take: Callable[[], Snapshot], interval: float, count: int, sleep: Callable[[float], None] = time.sleep, emit: Callable[[str], None] = print, as_json: bool = False) -> List[Delta]:
    previous = take()
    emit(json.dumps(to_json(previous)) if as_json else render(previous))
    deltas: List[Delta] = []
    rounds = 0
    try:
        while count <= 0 or rounds < count:
            sleep(interval)
            current = take()
            delta = diff(previous, current)
            deltas.append(delta)
            emit(json.dumps(asdict(delta)) if as_json else render_delta(delta))
            previous = current
            rounds += 1
    except KeyboardInterrupt:
        pass
    return deltas


def scan_deleted(root: str = "/proc") -> List[dict]:
    found: List[dict] = []
    for name in sorted((n for n in os.listdir(root) if n.isdigit()), key=int):
        try:
            snap = snapshot(int(name), root)
        except (OSError, ValueError):
            continue
        for d in deleted_open(snap):
            found.append({"pid": snap.pid, "comm": snap.comm, "fd": d.fd, "target": d.target, "size": d.size})
    return found


def render_deleted(found: Sequence[dict]) -> str:
    if not found:
        return "no deleted-but-open files"
    lines = [f"{len(found)} deleted file(s) still held open; truncate with: : > /proc/PID/fd/FD"]
    for item in sorted(found, key=lambda i: -(i["size"] or 0)):
        lines.append(f"  pid {item['pid']:<7} fd {item['fd']:<4} {item['comm']:<16} {human_size(item['size']):>10}  {item['target']}")
    return "\n".join(lines)


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="fdwatch", description=__doc__)
    parser.add_argument("pid", nargs="?", type=int, help="process to inspect")
    parser.add_argument("--watch", type=float, metavar="SECONDS", help="take a snapshot every SECONDS and report growth")
    parser.add_argument("--count", type=int, default=0, help="number of --watch rounds (0 = until Ctrl-C)")
    parser.add_argument("--deleted", action="store_true", help="scan every process for deleted-but-open files")
    parser.add_argument("--top", type=int, default=5, help="how many repeated paths to show")
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    parser.add_argument("--root", default="/proc", help="procfs root (a fake tree in tests)")
    args = parser.parse_args(list(sys.argv[1:] if argv is None else argv))
    if not os.path.isdir(args.root):
        print(f"fdwatch: {args.root} not found; this needs Linux procfs (on macOS use: lsof -p PID)", file=sys.stderr)
        return 2
    if args.deleted:
        found = scan_deleted(args.root)
        print(json.dumps(found, indent=2) if args.json else render_deleted(found))
        return 0
    if args.pid is None:
        parser.error("pass a PID or --deleted")
    try:
        if args.watch:
            watch(lambda: snapshot(args.pid, args.root), args.watch, args.count, as_json=args.json)
            return 0
        snap = snapshot(args.pid, args.root)
    except OSError as exc:
        hint = "; run with sudo to read another user's process" if isinstance(exc, PermissionError) else ""
        print(f"fdwatch: {exc}{hint}", file=sys.stderr)
        return 1
    print(json.dumps(to_json(snap), indent=2) if args.json else render(snap, args.top))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
