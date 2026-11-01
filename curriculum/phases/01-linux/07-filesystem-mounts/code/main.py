"""mountlens: read /proc/self/mountinfo, build the mount tree, find the mount a path resolves through, dissect overlayfs stacks, spot shadowed mounts, report blocks and inodes per mount, and diff two snapshots."""
from __future__ import annotations

import argparse
import json
import os
import posixpath
import sys
from dataclasses import asdict, dataclass, field
from typing import Any, Callable, Dict, Iterator, List, Optional, Sequence, Tuple

MOUNTINFO = "/proc/self/mountinfo"
ESCAPES = (("\\040", " "), ("\\011", "\t"), ("\\012", "\n"), ("\\134", "\\"))
PSEUDO_FSTYPES = frozenset(
    {
        "proc", "sysfs", "devpts", "mqueue", "cgroup", "cgroup2", "securityfs", "pstore", "bpf", "debugfs", "tracefs",
        "configfs", "fusectl", "autofs", "binfmt_misc", "hugetlbfs", "nsfs", "rpc_pipefs", "efivarfs", "selinuxfs", "devtmpfs",
    }
)
FLAG_WORDS = ("ro", "noexec", "nosuid", "nodev")
WARN_PERCENT = 90.0


@dataclass
class Mount:
    mount_id: int
    parent_id: int
    device: str
    root: str
    mount_point: str
    options: List[str]
    optional: List[str]
    fstype: str
    source: str
    super_options: List[str]
    order: int = 0

    @property
    def read_only(self) -> bool:
        return "ro" in self.options

    def has(self, flag: str) -> bool:
        return flag in self.options

    @property
    def is_bind(self) -> bool:
        return self.root != "/"

    @property
    def tags(self) -> Dict[str, Optional[int]]:
        found: Dict[str, Optional[int]] = {}
        for item in self.optional:
            name, _, value = item.partition(":")
            found[name] = int(value) if value.isdigit() else None
        return found

    @property
    def propagation(self) -> str:
        tags = self.tags
        words = [word for word, tag in (("shared", "shared"), ("slave", "master"), ("unbindable", "unbindable")) if tag in tags]
        return ",".join(words) or "private"

    @property
    def peer_group(self) -> Optional[int]:
        return self.tags.get("shared")

    @property
    def master_group(self) -> Optional[int]:
        return self.tags.get("master")

    def super_option(self, key: str) -> Optional[str]:
        prefix = key + "="
        for option in self.super_options:
            if option.startswith(prefix):
                return option[len(prefix):]
        return None

    @property
    def label(self) -> str:
        return f"{self.source}[{self.root}]" if self.is_bind else self.source

    @property
    def short_label(self) -> str:
        if not self.is_bind or len(self.label) <= 48:
            return self.label
        return f"{self.source}[.../{self.root.rstrip('/').split('/')[-1]}]"


@dataclass
class Overlay:
    mount_point: str
    lower: List[str]
    upper: Optional[str]
    work: Optional[str]
    read_only: bool

    @property
    def writable(self) -> bool:
        return self.upper is not None and not self.read_only


@dataclass
class Location:
    path: str
    mount_point: str
    mount_id: int
    path_in_mount: str
    source: str
    fstype: str
    device: str
    root: str
    bind: bool
    path_on_source: str
    options: List[str]
    read_only: bool
    noexec: bool
    nosuid: bool
    nodev: bool
    propagation: str
    super_options: List[str]
    overlay: Optional[Overlay] = None


@dataclass
class Usage:
    mount_point: str
    fstype: str
    source: str
    size: int = 0
    used: int = 0
    avail: int = 0
    inodes: int = 0
    inodes_used: int = 0
    inodes_avail: int = 0
    read_only: bool = False
    error: Optional[str] = None

    @property
    def use_percent(self) -> float:
        total = self.used + self.avail
        return self.used / total * 100 if total else 0.0

    @property
    def inode_percent(self) -> float:
        return self.inodes_used / self.inodes * 100 if self.inodes else 0.0


@dataclass
class Change:
    mount_point: str
    what: str
    before: str
    after: str


@dataclass
class Diff:
    added: List[Mount] = field(default_factory=list)
    removed: List[Mount] = field(default_factory=list)
    changed: List[Change] = field(default_factory=list)


def unescape(text: str) -> str:
    for code, char in ESCAPES:
        text = text.replace(code, char)
    return text


def parse_line(line: str, order: int = 0) -> Mount:
    fields = line.split()
    if len(fields) < 9:
        raise ValueError(f"mountinfo line too short: {line!r}")
    try:
        separator = fields.index("-", 6)
    except ValueError:
        raise ValueError(f"mountinfo line without the '-' separator: {line!r}") from None
    if len(fields) < separator + 3:
        raise ValueError(f"mountinfo line without fstype and source: {line!r}")
    mount_id, parent_id, device, root, mount_point, options = fields[:6]
    super_options = fields[separator + 3] if len(fields) > separator + 3 else ""
    return Mount(
        int(mount_id),
        int(parent_id),
        device,
        unescape(root),
        unescape(mount_point),
        options.split(","),
        fields[6:separator],
        fields[separator + 1],
        unescape(fields[separator + 2]),
        super_options.split(",") if super_options else [],
        order,
    )


def parse_mountinfo(text: str) -> List[Mount]:
    mounts: List[Mount] = []
    for raw in text.splitlines():
        line = raw.strip()
        if line:
            mounts.append(parse_line(line, len(mounts)))
    return mounts


def load(path: str) -> List[Mount]:
    with open(path, encoding="utf-8", errors="replace") as handle:
        return parse_mountinfo(handle.read())


def roots(mounts: Sequence[Mount]) -> List[Mount]:
    ids = {m.mount_id for m in mounts}
    return [m for m in mounts if m.parent_id not in ids or m.parent_id == m.mount_id]


def walk(mounts: Sequence[Mount]) -> Iterator[Tuple[int, Mount]]:
    by_parent: Dict[int, List[Mount]] = {}
    for m in mounts:
        by_parent.setdefault(m.parent_id, []).append(m)
    seen = set()

    def visit(mount: Mount, depth: int) -> Iterator[Tuple[int, Mount]]:
        if mount.mount_id in seen:
            return
        seen.add(mount.mount_id)
        yield depth, mount
        for child in sorted(by_parent.get(mount.mount_id, []), key=lambda c: c.order):
            if child.mount_id != mount.mount_id:
                yield from visit(child, depth + 1)

    for root in roots(mounts):
        yield from visit(root, 0)


def covers(mount_point: str, path: str) -> bool:
    return mount_point == "/" or path == mount_point or path.startswith(mount_point.rstrip("/") + "/")


def is_tree_ancestor(candidate: Mount, mount: Mount, by_id: Dict[int, Mount]) -> bool:
    node = by_id.get(mount.parent_id)
    seen = set()
    while node is not None and node.mount_id not in seen and node.mount_id != mount.mount_id:
        if node.mount_id == candidate.mount_id:
            return True
        seen.add(node.mount_id)
        node = by_id.get(node.parent_id)
    return False


def shadowed(mounts: Sequence[Mount]) -> List[Tuple[Mount, Mount]]:
    by_id = {m.mount_id: m for m in mounts}
    hidden: List[Tuple[Mount, Mount]] = []
    for mount in mounts:
        for later in mounts:
            if later.order <= mount.order or not covers(later.mount_point, mount.mount_point):
                continue
            if not is_tree_ancestor(later, mount, by_id):
                hidden.append((mount, later))
                break
    return hidden


def visible(mounts: Sequence[Mount]) -> List[Mount]:
    hidden = {m.mount_id for m, _ in shadowed(mounts)}
    return [m for m in mounts if m.mount_id not in hidden]


def normalize(path: str) -> str:
    if not path.startswith("/"):
        path = posixpath.join(os.getcwd(), path)
    return posixpath.normpath(path)


def find_mount(mounts: Sequence[Mount], path: str) -> Mount:
    path = normalize(path)
    candidates = [m for m in visible(mounts) if covers(m.mount_point, path)]
    if not candidates:
        raise LookupError(f"no mount covers {path}; is this mountinfo from the right namespace?")
    return max(candidates, key=lambda m: (len(m.mount_point), m.order))


def overlay_of(mount: Mount) -> Optional[Overlay]:
    if mount.fstype != "overlay":
        return None
    lower = mount.super_option("lowerdir")
    layers = [unescape(layer) for layer in lower.split(":") if layer] if lower else []
    return Overlay(mount.mount_point, layers, mount.super_option("upperdir"), mount.super_option("workdir"), mount.read_only)


def overlays(mounts: Sequence[Mount]) -> List[Overlay]:
    return [overlay for overlay in (overlay_of(m) for m in mounts) if overlay is not None]


def locate(mounts: Sequence[Mount], path: str) -> Location:
    path = normalize(path)
    mount = find_mount(mounts, path)
    relative = path[len(mount.mount_point):].lstrip("/") if mount.mount_point != "/" else path.lstrip("/")
    on_source = posixpath.normpath(posixpath.join(mount.root, relative)) if relative else mount.root
    return Location(
        path,
        mount.mount_point,
        mount.mount_id,
        relative or ".",
        mount.source,
        mount.fstype,
        mount.device,
        mount.root,
        mount.is_bind,
        on_source,
        list(mount.options),
        mount.read_only,
        mount.has("noexec"),
        mount.has("nosuid"),
        mount.has("nodev"),
        mount.propagation,
        list(mount.super_options),
        overlay_of(mount),
    )


def usage(mounts: Sequence[Mount], statvfs: Callable[[str], Any] = os.statvfs, include_all: bool = False) -> List[Usage]:
    report: List[Usage] = []
    seen_devices = set()
    for mount in visible(mounts):
        if not include_all and (mount.fstype in PSEUDO_FSTYPES or mount.device in seen_devices):
            continue
        seen_devices.add(mount.device)
        entry = Usage(mount.mount_point, mount.fstype, mount.label, read_only=mount.read_only)
        try:
            stat = statvfs(mount.mount_point)
        except OSError as exc:
            entry.error = exc.strerror or str(exc)
        else:
            unit = stat.f_frsize or stat.f_bsize
            entry.size = stat.f_blocks * unit
            entry.used = (stat.f_blocks - stat.f_bfree) * unit
            entry.avail = stat.f_bavail * unit
            entry.inodes = stat.f_files
            entry.inodes_used = stat.f_files - stat.f_ffree
            entry.inodes_avail = stat.f_favail
        if include_all or entry.error or entry.size:
            report.append(entry)
    return report


def usage_warnings(report: Sequence[Usage]) -> List[str]:
    warnings: List[str] = []
    for entry in report:
        if entry.error or entry.read_only:
            continue
        if entry.inodes and entry.inode_percent >= WARN_PERCENT:
            warnings.append(
                f"WARNING: {entry.mount_point} has {entry.inodes_avail} of {entry.inodes} inodes free ({entry.inode_percent:.0f}% used) "
                f"with {100 - entry.use_percent:.0f}% of its bytes free: 'No space left on device' here is inode exhaustion"
            )
        elif entry.use_percent >= WARN_PERCENT:
            warnings.append(f"WARNING: {entry.mount_point} is {entry.use_percent:.0f}% full ({human_size(entry.avail)} left)")
    return warnings


def signature(mount: Mount) -> Tuple[str, ...]:
    return (mount.source, mount.root, mount.fstype, ",".join(mount.options), mount.propagation)


def diff(before: Sequence[Mount], after: Sequence[Mount]) -> Diff:
    old = {m.mount_point: m for m in visible(before)}
    new = {m.mount_point: m for m in visible(after)}
    result = Diff()
    result.added = [new[p] for p in new if p not in old]
    result.removed = [old[p] for p in old if p not in new]
    for point in old:
        if point not in new:
            continue
        a, b = old[point], new[point]
        for what, x, y in (
            ("source", a.label, b.label),
            ("fstype", a.fstype, b.fstype),
            ("options", ",".join(a.options), ",".join(b.options)),
            ("propagation", a.propagation, b.propagation),
        ):
            if x != y:
                result.changed.append(Change(point, what, x, y))
    return result


def human_size(size: int) -> str:
    value = float(size)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            return f"{int(value)} B" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} TB"


def columns(rows: Sequence[Sequence[str]], header: Sequence[str]) -> str:
    table = [list(header)] + [list(r) for r in rows]
    widths = [min(max(len(row[i]) for row in table), 72) for i in range(len(header))]
    lines = []
    for row in table:
        cells = [cell.ljust(widths[i]) if i < len(row) - 1 else cell for i, cell in enumerate(row)]
        lines.append("  ".join(cells).rstrip())
    return "\n".join(lines)


def render_tree(mounts: Sequence[Mount]) -> str:
    hidden = {m.mount_id: by.mount_id for m, by in shadowed(mounts)}
    rows = []
    for depth, mount in walk(mounts):
        note = ""
        if mount.mount_id in hidden:
            note = f"shadowed by mount {hidden[mount.mount_id]}"
        else:
            overlay = overlay_of(mount)
            if overlay is not None:
                note = f"{len(overlay.lower)} lower layer(s)" + (" + upperdir" if overlay.upper else ", no upperdir")
        rows.append(("  " * depth + mount.mount_point, mount.short_label, mount.fstype, ",".join(mount.options), mount.propagation, note))
    return columns(rows, ("TARGET", "SOURCE", "FSTYPE", "OPTIONS", "PROPAGATION", "NOTE"))


def render_location(loc: Location) -> str:
    lines = [loc.path, f"  mount        {loc.mount_point}  (mount id {loc.mount_id}; path inside the mount: {loc.path_in_mount})"]
    lines.append(f"  source       {loc.source}  {loc.fstype}  device {loc.device}")
    if loc.bind:
        lines.append(f"  bind of      {loc.root} on that filesystem, so the path is {loc.path_on_source} there")
    flags = "  ".join(f"{word}: {'yes' if getattr(loc, 'read_only' if word == 'ro' else word) else 'no'}" for word in FLAG_WORDS)
    lines.append(f"  options      {','.join(loc.options)}   {flags}")
    lines.append(f"  propagation  {loc.propagation}")
    lines.append(f"  superblock   {','.join(loc.super_options) or '-'}")
    if loc.overlay is not None:
        upper = "upperdir (writable layer)" if loc.overlay.upper else "no upperdir"
        lines.append(f"  overlay      {len(loc.overlay.lower)} lower layer(s) + {upper}" + ("; mounted ro" if loc.overlay.read_only else ""))
    if loc.read_only and "rw" in loc.super_options:
        lines.append("  note         the filesystem itself is rw; ro is set on this mount point only (a per-mount flag, not a permission)")
    if loc.noexec:
        lines.append("  note         noexec: binaries and scripts under this mount cannot be executed, whatever their mode bits say")
    return "\n".join(lines)


def render_overlays(found: Sequence[Overlay]) -> str:
    if not found:
        return "no overlay mounts"
    lines = []
    for overlay in found:
        state = "read-only (ro flag)" if overlay.read_only else ("writable through upperdir" if overlay.upper else "read-only (no upperdir)")
        lines.append(f"{overlay.mount_point}: {len(overlay.lower)} lower layer(s), {state}")
        for index, layer in enumerate(overlay.lower):
            lines.append(f"  lower[{index}]  {layer}")
        if overlay.upper:
            lines.append(f"  upperdir  {overlay.upper}")
        if overlay.work:
            lines.append(f"  workdir   {overlay.work}")
    return "\n".join(lines)


def render_shadowed(hidden: Sequence[Tuple[Mount, Mount]]) -> str:
    if not hidden:
        return "no shadowed mounts: every mount is reachable by path"
    lines = [f"{len(hidden)} mount(s) hidden by later mounts (data there is still allocated but unreachable by path):"]
    for mount, by in hidden:
        lines.append(f"  {mount.mount_point}  {mount.label} {mount.fstype} (mount {mount.mount_id})  hidden by  {by.mount_point}  {by.label} {by.fstype} (mount {by.mount_id})")
    return "\n".join(lines)


def render_usage(report: Sequence[Usage]) -> str:
    rows = []
    for entry in report:
        if entry.error:
            rows.append((entry.mount_point, entry.fstype, "-", "-", "-", "-", "-", "-", f"unavailable: {entry.error}"))
        else:
            rows.append(
                (
                    entry.mount_point, entry.fstype, human_size(entry.size), human_size(entry.used), human_size(entry.avail),
                    f"{entry.use_percent:.0f}%", str(entry.inodes), str(entry.inodes_used), f"{entry.inode_percent:.0f}%",
                )
            )
    text = columns(rows, ("MOUNT", "FSTYPE", "SIZE", "USED", "AVAIL", "USE%", "INODES", "IUSED", "IUSE%"))
    warnings = usage_warnings(report)
    return text + ("\n" + "\n".join(warnings) if warnings else "")


def render_diff(result: Diff) -> str:
    lines = [f"mountinfo diff: {len(result.added)} added, {len(result.removed)} removed, {len(result.changed)} changed"]
    for mount in result.added:
        lines.append(f"+ {mount.mount_point}  {mount.label}  {mount.fstype}  {','.join(mount.options)}  {mount.propagation}")
    for mount in result.removed:
        lines.append(f"- {mount.mount_point}  {mount.label}  {mount.fstype}  {','.join(mount.options)}  {mount.propagation}")
    groups: Dict[Tuple[str, str, str], List[str]] = {}
    for change in result.changed:
        groups.setdefault((change.what, change.before, change.after), []).append(change.mount_point)
    for (what, before, after), points in groups.items():
        if len(points) > 3:
            lines.append(f"~ {what}: {before} -> {after} on {len(points)} mounts ({', '.join(points[:3])}, ...)")
        else:
            for point in points:
                lines.append(f"~ {point}  {what}: {before} -> {after}")
    return "\n".join(lines)


def mount_to_json(mount: Mount) -> dict:
    data = asdict(mount)
    data.update({"propagation": mount.propagation, "read_only": mount.read_only, "bind": mount.is_bind, "peer_group": mount.peer_group, "master_group": mount.master_group})
    return data


def usage_to_json(entry: Usage) -> dict:
    data = asdict(entry)
    data.update({"use_percent": round(entry.use_percent, 1), "inode_percent": round(entry.inode_percent, 1)})
    return data


def read_mounts(path: str) -> List[Mount]:
    if not os.path.exists(path):
        if path == MOUNTINFO:
            raise FileNotFoundError(f"{path} not found; this needs Linux procfs (on macOS pass --file with a saved mountinfo, e.g. code/tests/fixtures/container.mountinfo)")
        raise FileNotFoundError(f"{path} not found")
    return load(path)


def main(argv: Optional[Sequence[str]] = None) -> int:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--file", default=MOUNTINFO, help="mountinfo to read (default /proc/self/mountinfo; a saved copy in tests)")
    common.add_argument("--json", action="store_true", help="machine-readable output")
    parser = argparse.ArgumentParser(prog="mountlens", description=__doc__)
    commands = parser.add_subparsers(dest="command", metavar="COMMAND")
    commands.add_parser("tree", parents=[common], help="the mount table as a tree with source, fstype, options and propagation")
    which = commands.add_parser("which", parents=[common], help="the mount a path resolves through and what that mount allows")
    which.add_argument("path")
    commands.add_parser("overlay", parents=[common], help="lowerdir/upperdir/workdir of every overlayfs mount")
    commands.add_parser("shadowed", parents=[common], help="mounts hidden by later mounts on the same or a covering path")
    use = commands.add_parser("usage", parents=[common], help="bytes and inodes per mount from statvfs")
    use.add_argument("--all", action="store_true", help="include pseudo filesystems and every bind of the same device")
    cmp = commands.add_parser("diff", help="what changed between two saved mountinfo files")
    cmp.add_argument("before")
    cmp.add_argument("after")
    cmp.add_argument("--json", action="store_true", help="machine-readable output")
    args = parser.parse_args(list(sys.argv[1:] if argv is None else argv))
    if args.command is None:
        parser.print_help()
        return 2
    try:
        if args.command == "diff":
            result = diff(read_mounts(args.before), read_mounts(args.after))
            if args.json:
                print(json.dumps({"added": [mount_to_json(m) for m in result.added], "removed": [mount_to_json(m) for m in result.removed], "changed": [asdict(c) for c in result.changed]}, indent=2))
            else:
                print(render_diff(result))
            return 0
        mounts = read_mounts(args.file)
    except FileNotFoundError as exc:
        print(f"mountlens: {exc}", file=sys.stderr)
        return 2
    except (OSError, ValueError) as exc:
        print(f"mountlens: {exc}", file=sys.stderr)
        return 1
    if args.command == "tree":
        print(json.dumps([mount_to_json(m) for m in mounts], indent=2) if args.json else render_tree(mounts))
    elif args.command == "which":
        try:
            loc = locate(mounts, args.path)
        except LookupError as exc:
            print(f"mountlens: {exc}", file=sys.stderr)
            return 1
        print(json.dumps(asdict(loc), indent=2) if args.json else render_location(loc))
    elif args.command == "overlay":
        found = overlays(mounts)
        print(json.dumps([asdict(o) for o in found], indent=2) if args.json else render_overlays(found))
    elif args.command == "shadowed":
        hidden = shadowed(mounts)
        print(json.dumps([{"hidden": mount_to_json(m), "by": mount_to_json(b)} for m, b in hidden], indent=2) if args.json else render_shadowed(hidden))
    elif args.command == "usage":
        report = usage(mounts, include_all=args.all)
        print(json.dumps({"mounts": [usage_to_json(u) for u in report], "warnings": usage_warnings(report)}, indent=2) if args.json else render_usage(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
