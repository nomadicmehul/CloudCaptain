"""userns: compute the uid/gid map a rootless container gets from /etc/subuid and /etc/subgid, translate ids across the boundary, explain a running process's map, and on Linux run a command as uid 0 in a user namespace you own."""
from __future__ import annotations

import argparse
import ctypes
import json
import os
import pwd
import sys
from dataclasses import asdict, dataclass
from typing import Dict, List, Optional, Sequence, Tuple

CLONE_NEWNS = 0x00020000
CLONE_NEWUTS = 0x04000000
CLONE_NEWIPC = 0x08000000
CLONE_NEWUSER = 0x10000000
CLONE_NEWPID = 0x20000000
CLONE_NEWNET = 0x40000000
MS_NOSUID = 0x2
MS_NODEV = 0x4
MS_NOEXEC = 0x8
MS_REC = 0x4000
MS_PRIVATE = 0x40000
EPERM = 1

EXTRA_NAMESPACES: Dict[str, int] = {
    "mnt": CLONE_NEWNS,
    "uts": CLONE_NEWUTS,
    "ipc": CLONE_NEWIPC,
    "pid": CLONE_NEWPID,
    "net": CLONE_NEWNET,
}
ID_LIMIT = 2 ** 32
INITIAL_COUNT = ID_LIMIT - 1
DEFAULT_OVERFLOW_ID = 65534
DEFAULT_RANGE_START = 100000
DEFAULT_RANGE_COUNT = 65536
MAX_MAP_LINES = 340
SUBUID_PATH = "/etc/subuid"
SUBGID_PATH = "/etc/subgid"


@dataclass(frozen=True)
class SubidRange:
    owner: str
    start: int
    count: int

    @property
    def last(self) -> int:
        return self.start + self.count - 1


@dataclass(frozen=True)
class MapEntry:
    inside: int
    outside: int
    count: int

    @property
    def inside_last(self) -> int:
        return self.inside + self.count - 1

    @property
    def outside_last(self) -> int:
        return self.outside + self.count - 1


def parse_subid(text: str, source: str = SUBUID_PATH) -> List[SubidRange]:
    ranges: List[SubidRange] = []
    for number, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split(":")
        if len(parts) != 3 or not parts[0] or not parts[1].isdigit() or not parts[2].isdigit():
            raise ValueError(f"{source}:{number}: expected OWNER:START:COUNT, got {raw!r}")
        start, count = int(parts[1]), int(parts[2])
        if count < 1 or start + count > ID_LIMIT:
            raise ValueError(f"{source}:{number}: range {start}+{count} is empty or exceeds 32-bit ids")
        ranges.append(SubidRange(parts[0], start, count))
    return ranges


def ranges_for(ranges: Sequence[SubidRange], user: str, uid: Optional[int] = None) -> List[SubidRange]:
    owners = {user}
    if uid is not None:
        owners.add(str(uid))
    return [r for r in ranges if r.owner in owners]


def suggest_range(ranges: Sequence[SubidRange]) -> SubidRange:
    start = max((r.last for r in ranges), default=DEFAULT_RANGE_START - 1) + 1
    return SubidRange("", start, DEFAULT_RANGE_COUNT)


def build_mapping(own_id: int, ranges: Sequence[SubidRange]) -> List[MapEntry]:
    entries = [MapEntry(0, own_id, 1)]
    next_inside = 1
    for r in ranges:
        entries.append(MapEntry(next_inside, r.start, r.count))
        next_inside += r.count
    validate_mapping(entries)
    return entries


def validate_mapping(entries: Sequence[MapEntry]) -> None:
    if not entries:
        raise ValueError("a mapping needs at least one line")
    if len(entries) > MAX_MAP_LINES:
        raise ValueError(f"{len(entries)} lines; the kernel accepts at most {MAX_MAP_LINES}")
    for e in entries:
        if e.count < 1 or e.inside < 0 or e.outside < 0 or e.inside_last >= ID_LIMIT or e.outside_last >= ID_LIMIT:
            raise ValueError(f"line '{e.inside} {e.outside} {e.count}': ids must be 32-bit and count at least 1")
    for side in ("inside", "outside"):
        ordered = sorted(entries, key=lambda e: getattr(e, side))
        for a, b in zip(ordered, ordered[1:]):
            if getattr(a, side) + a.count > getattr(b, side):
                raise ValueError(f"{side} ranges overlap: '{a.inside} {a.outside} {a.count}' and '{b.inside} {b.outside} {b.count}'")


def parse_id_map(text: str, source: str = "uid_map") -> List[MapEntry]:
    entries: List[MapEntry] = []
    for number, raw in enumerate(text.splitlines(), 1):
        parts = raw.split()
        if not parts:
            continue
        if len(parts) != 3 or not all(p.isdigit() for p in parts):
            raise ValueError(f"{source}:{number}: expected INSIDE OUTSIDE COUNT, got {raw!r}")
        entries.append(MapEntry(int(parts[0]), int(parts[1]), int(parts[2])))
    validate_mapping(entries)
    return entries


def parse_map_option(values: Sequence[Sequence[str]]) -> List[MapEntry]:
    entries: List[MapEntry] = []
    for triple in values:
        if len(triple) != 3 or not all(str(v).isdigit() for v in triple):
            raise ValueError(f"--map wants INSIDE OUTSIDE COUNT, got {' '.join(map(str, triple))!r}")
        entries.append(MapEntry(int(triple[0]), int(triple[1]), int(triple[2])))
    validate_mapping(entries)
    return entries


def render_id_map(entries: Sequence[MapEntry]) -> str:
    return "".join(f"{e.inside:>10} {e.outside:>10} {e.count:>10}\n" for e in entries)


def newidmap_args(tool: str, pid: str, entries: Sequence[MapEntry]) -> List[str]:
    args = [tool, str(pid)]
    for e in entries:
        args.extend([str(e.inside), str(e.outside), str(e.count)])
    return args


def to_host(entries: Sequence[MapEntry], inside: int) -> Optional[int]:
    for e in entries:
        if e.inside <= inside <= e.inside_last:
            return e.outside + (inside - e.inside)
    return None


def to_container(entries: Sequence[MapEntry], outside: int) -> Optional[int]:
    for e in entries:
        if e.outside <= outside <= e.outside_last:
            return e.inside + (outside - e.outside)
    return None


def is_initial(entries: Sequence[MapEntry]) -> bool:
    return list(entries) == [MapEntry(0, 0, INITIAL_COUNT)]


def mapped_count(entries: Sequence[MapEntry]) -> int:
    return sum(e.count for e in entries)


def describe(entries: Sequence[MapEntry], kind: str = "uid", overflow: int = DEFAULT_OVERFLOW_ID, own_id: Optional[int] = None, own_name: Optional[str] = None) -> List[str]:
    if is_initial(entries):
        return [f"identity map of the initial user namespace: {kind} 0 inside is {kind} 0 on the host, so this is not rootless"]
    lines: List[str] = []
    for e in sorted(entries, key=lambda e: e.inside):
        tag = f" ({own_name})" if own_name and e.count == 1 and e.outside == own_id else ""
        if e.count == 1:
            lines.append(f"container {kind} {e.inside} is host {kind} {e.outside}{tag}")
        else:
            lines.append(f"container {kind}s {e.inside}..{e.inside_last} are host {kind}s {e.outside}..{e.outside_last}")
    if to_container(entries, 0) is None:
        lines.append(f"host {kind} 0 is unmapped: inside it appears as {overflow} (the overflow id, usually nobody) and nothing can chown to it")
    top = max(e.inside_last for e in entries)
    if top < ID_LIMIT - 2:
        lines.append(f"container {kind}s above {top} are unmapped: no process can hold them and no file can be owned by them")
    lines.append(f"{mapped_count(entries)} ids in {len(entries)} line(s) (kernel limit {MAX_MAP_LINES})")
    return lines


def verdict(uid_entries: Sequence[MapEntry]) -> str:
    if is_initial(uid_entries):
        return "not rootless: this is the initial user namespace, uid 0 inside is the host's root"
    root_outside = to_host(uid_entries, 0)
    if root_outside is None:
        return "rootless, but uid 0 is not mapped: nothing inside can be root"
    extra = mapped_count(uid_entries) - 1
    if extra == 0:
        return f"rootless single-user map: uid 0 inside is host uid {root_outside} and every other uid is unmapped (what unshare -Ur gives you)"
    return f"rootless with subordinate ids: uid 0 inside is host uid {root_outside}; {extra} more uids come from /etc/subuid ranges (what newuidmap and podman give you)"


def read_text(path: str) -> str:
    with open(path, encoding="utf-8") as handle:
        return handle.read()


def read_optional(path: str) -> Optional[str]:
    try:
        return read_text(path)
    except OSError:
        return None


def read_overflow(root: str) -> int:
    text = read_optional(os.path.join(root, "sys", "kernel", "overflowuid"))
    return int(text.strip()) if text and text.strip().isdigit() else DEFAULT_OVERFLOW_ID


def resolve_user(user: Optional[str], uid: Optional[int], gid: Optional[int]) -> Tuple[str, int, int]:
    if user is None and uid is None:
        uid = os.getuid()
    record = None
    try:
        record = pwd.getpwnam(user) if user is not None else pwd.getpwuid(uid)
    except KeyError:
        pass
    if record is not None:
        user = record.pw_name
        uid = record.pw_uid if uid is None else uid
        gid = record.pw_gid if gid is None else gid
    if uid is None:
        raise ValueError(f"unknown user {user!r} on this host; pass --uid N (and --gid N) to compute the map for another machine")
    if user is None:
        user = str(uid)
    return user, uid, uid if gid is None else gid


def plan_user(user: str, uid: int, gid: int, subuid_text: Optional[str], subgid_text: Optional[str], pid: str = "PID") -> dict:
    uid_ranges = ranges_for(parse_subid(subuid_text or "", SUBUID_PATH), user, uid)
    gid_ranges = ranges_for(parse_subid(subgid_text or "", SUBGID_PATH), user, uid)
    uid_entries = build_mapping(uid, uid_ranges)
    gid_entries = build_mapping(gid, gid_ranges)
    all_known = parse_subid(subuid_text or "", SUBUID_PATH) + parse_subid(subgid_text or "", SUBGID_PATH)
    suggestion = suggest_range(all_known)
    return {
        "user": user,
        "uid": uid,
        "gid": gid,
        "subuid_present": subuid_text is not None,
        "subgid_present": subgid_text is not None,
        "subuid_ranges": [asdict(r) for r in uid_ranges],
        "subgid_ranges": [asdict(r) for r in gid_ranges],
        "uid_map": [asdict(e) for e in uid_entries],
        "gid_map": [asdict(e) for e in gid_entries],
        "uid_notes": describe(uid_entries, "uid", own_id=uid, own_name=user),
        "gid_notes": describe(gid_entries, "gid", own_id=gid, own_name=user),
        "newuidmap": newidmap_args("newuidmap", pid, uid_entries),
        "newgidmap": newidmap_args("newgidmap", pid, gid_entries),
        "complete": bool(uid_ranges) and bool(gid_ranges),
        "fix": f"sudo usermod --add-subuids {suggestion.start}-{suggestion.last} --add-subgids {suggestion.start}-{suggestion.last} {user} && podman system migrate",
        "verdict": verdict(uid_entries),
    }


def render_plan(plan: dict) -> str:
    lines = [f"user {plan['user']} (uid {plan['uid']}, gid {plan['gid']})"]
    for label, present, ranges in ((SUBUID_PATH, plan["subuid_present"], plan["subuid_ranges"]), (SUBGID_PATH, plan["subgid_present"], plan["subgid_ranges"])):
        if not present:
            lines.append(f"{label}: file not found (install the uidmap or shadow-utils package)")
        elif not ranges:
            lines.append(f"{label}: no range for {plan['user']} (uid {plan['uid']})")
        else:
            lines.append(f"{label}: " + ", ".join(f"{r['owner']}:{r['start']}:{r['count']}" for r in ranges))
    for kind in ("uid", "gid"):
        lines.append("")
        lines.append(f"{kind}_map (what new{kind}map writes to /proc/PID/{kind}_map)")
        entries = [MapEntry(**e) for e in plan[f"{kind}_map"]]
        lines.extend(render_id_map(entries).rstrip("\n").splitlines())
        lines.extend(f"  {note}" for note in plan[f"{kind}_notes"])
    lines.append("")
    if plan["complete"]:
        lines.append(" ".join(plan["newuidmap"]))
        lines.append(" ".join(plan["newgidmap"]))
    else:
        lines.append("only the single-user map is possible (unshare -Ur): uid 0 inside and nothing else; images with other users will fail to unpack")
        lines.append(f"fix: {plan['fix']}")
    lines.append(f"verdict: {plan['verdict']}")
    return "\n".join(lines)


def explain_maps(uid_entries: Sequence[MapEntry], gid_entries: Optional[Sequence[MapEntry]], overflow: int, label: str) -> dict:
    report = {
        "label": label,
        "overflow_id": overflow,
        "initial": is_initial(uid_entries),
        "uid_map": [asdict(e) for e in uid_entries],
        "uid_notes": describe(uid_entries, "uid", overflow),
        "verdict": verdict(uid_entries),
    }
    if gid_entries is not None:
        report["gid_map"] = [asdict(e) for e in gid_entries]
        report["gid_notes"] = describe(gid_entries, "gid", overflow)
    return report


def explain_process(pid: int, root: str = "/proc") -> dict:
    base = os.path.join(root, str(pid))
    uid_text = read_optional(os.path.join(base, "uid_map"))
    if uid_text is None:
        raise FileNotFoundError(f"no readable {base}/uid_map (wrong PID, or run with sudo)")
    gid_text = read_optional(os.path.join(base, "gid_map"))
    uid_entries = parse_id_map(uid_text, f"{base}/uid_map")
    gid_entries = parse_id_map(gid_text, f"{base}/gid_map") if gid_text is not None else None
    return explain_maps(uid_entries, gid_entries, read_overflow(root), f"pid {pid}")


def explain_files(uid_map_path: str, gid_map_path: Optional[str] = None) -> dict:
    uid_entries = parse_id_map(read_text(uid_map_path), uid_map_path)
    gid_entries = parse_id_map(read_text(gid_map_path), gid_map_path) if gid_map_path else None
    return explain_maps(uid_entries, gid_entries, DEFAULT_OVERFLOW_ID, uid_map_path)


def render_report(report: dict) -> str:
    lines = [f"{report['label']}: uid_map"]
    lines.extend(render_id_map([MapEntry(**e) for e in report["uid_map"]]).rstrip("\n").splitlines())
    lines.extend(f"  {note}" for note in report["uid_notes"])
    if "gid_map" in report:
        lines.append("gid_map")
        lines.extend(render_id_map([MapEntry(**e) for e in report["gid_map"]]).rstrip("\n").splitlines())
        lines.extend(f"  {note}" for note in report["gid_notes"])
    lines.append(f"verdict: {report['verdict']}")
    return "\n".join(lines)


def translate_ids(entries: Sequence[MapEntry], ids: Sequence[int], from_host: bool, kind: str = "uid", overflow: int = DEFAULT_OVERFLOW_ID) -> List[dict]:
    results: List[dict] = []
    for value in ids:
        mapped = to_container(entries, value) if from_host else to_host(entries, value)
        if from_host:
            text = f"host {kind} {value} -> container {kind} {mapped}" if mapped is not None else f"host {kind} {value} -> unmapped (appears inside as {overflow})"
        else:
            text = f"container {kind} {value} -> host {kind} {mapped}" if mapped is not None else f"container {kind} {value} -> unmapped (no process or file inside can have it)"
        results.append({"kind": kind, "from_host": from_host, "id": value, "mapped": mapped, "text": text})
    return results


def parse_namespaces(spec: str) -> int:
    flags = 0
    for name in (part.strip() for part in spec.split(",") if part.strip()):
        if name not in EXTRA_NAMESPACES:
            raise ValueError(f"unknown namespace {name!r}; choose from {','.join(EXTRA_NAMESPACES)}")
        flags |= EXTRA_NAMESPACES[name]
    return flags


def single_user_map(uid: int, gid: int) -> List[Tuple[str, str]]:
    return [
        ("/proc/self/setgroups", "deny"),
        ("/proc/self/uid_map", render_id_map([MapEntry(0, uid, 1)])),
        ("/proc/self/gid_map", render_id_map([MapEntry(0, gid, 1)])),
    ]


def write_once(path: str, text: str) -> None:
    fd = os.open(path, os.O_WRONLY)
    try:
        os.write(fd, text.encode("ascii"))
    finally:
        os.close(fd)


def child(command: Sequence[str], flags: int, libc: ctypes.CDLL) -> None:
    if flags & CLONE_NEWNS:
        libc.mount(None, b"/", None, MS_REC | MS_PRIVATE, None)
        if flags & CLONE_NEWPID and libc.mount(b"proc", b"/proc", b"proc", MS_NOSUID | MS_NODEV | MS_NOEXEC, None) != 0:
            errno = ctypes.get_errno()
            print(f"userns: mount /proc failed: {os.strerror(errno)}", file=sys.stderr)
            os._exit(1)
    try:
        os.execvp(command[0], list(command))
    except OSError as exc:
        print(f"userns: cannot exec {command[0]}: {exc}", file=sys.stderr)
        os._exit(127)


def run(command: Sequence[str], namespaces: str = "") -> int:
    flags = parse_namespaces(namespaces)
    if sys.platform != "linux":
        raise OSError("run needs Linux; map, translate and explain work anywhere")
    uid, gid = os.getuid(), os.getgid()
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.unshare(CLONE_NEWUSER | flags) != 0:
        errno = ctypes.get_errno()
        hint = "; unprivileged user namespaces are disabled here: check sysctl kernel.unprivileged_userns_clone and kernel.apparmor_restrict_unprivileged_userns" if errno == EPERM else ""
        raise OSError(errno, f"unshare(CLONE_NEWUSER) failed: {os.strerror(errno)}{hint}")
    for path, text in single_user_map(uid, gid):
        write_once(path, text)
    pid = os.fork()
    if pid == 0:
        child(command, flags, libc)
    _, status = os.waitpid(pid, 0)
    return os.waitstatus_to_exitcode(status)


def cmd_map(args: argparse.Namespace) -> int:
    user, uid, gid = resolve_user(args.user, args.uid, args.gid)
    plan = plan_user(user, uid, gid, read_optional(args.subuid), read_optional(args.subgid), args.pid)
    print(json.dumps(plan, indent=2) if args.json else render_plan(plan))
    return 0 if plan["complete"] else 1


def mapping_from_args(args: argparse.Namespace) -> List[MapEntry]:
    if args.map:
        return parse_map_option(args.map)
    if args.uid_map:
        return parse_id_map(read_text(args.uid_map), args.uid_map)
    user, uid, gid = resolve_user(args.user, args.uid, args.gid)
    if args.kind == "gid":
        return build_mapping(gid, ranges_for(parse_subid(read_optional(args.subgid) or "", SUBGID_PATH), user, uid))
    return build_mapping(uid, ranges_for(parse_subid(read_optional(args.subuid) or "", SUBUID_PATH), user, uid))


def cmd_translate(args: argparse.Namespace) -> int:
    entries = mapping_from_args(args)
    results = translate_ids(entries, args.ids, args.from_host, args.kind)
    print(json.dumps(results, indent=2) if args.json else "\n".join(r["text"] for r in results))
    return 0 if all(r["mapped"] is not None for r in results) else 1


def cmd_explain(args: argparse.Namespace) -> int:
    if args.uid_map:
        report = explain_files(args.uid_map, args.gid_map)
    elif args.pid is not None:
        report = explain_process(args.pid, args.root)
    else:
        raise ValueError("pass a PID or --uid-map FILE")
    print(json.dumps(report, indent=2) if args.json else render_report(report))
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    command = list(args.command)
    if command and command[0] == "--":
        command = command[1:]
    if not command:
        command = ["/bin/sh"]
    parse_namespaces(args.ns)
    if sys.platform != "linux":
        print("userns: run needs Linux (unshare(2) with CLONE_NEWUSER); map, translate and explain work anywhere", file=sys.stderr)
        return 2
    return run(command, args.ns)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="userns", description=__doc__)
    sub = parser.add_subparsers(dest="command_name", required=True)

    def add_user_options(p: argparse.ArgumentParser) -> None:
        p.add_argument("--user", help="account name (default: you)")
        p.add_argument("--uid", type=int, help="its uid, when the account does not exist on this host")
        p.add_argument("--gid", type=int, help="its primary gid (default: the account's, else the uid)")
        p.add_argument("--subuid", default=SUBUID_PATH, help="path of /etc/subuid (a fixture in tests)")
        p.add_argument("--subgid", default=SUBGID_PATH, help="path of /etc/subgid")

    p_map = sub.add_parser("map", help="compute the uid_map/gid_map and newuidmap/newgidmap arguments for a user")
    add_user_options(p_map)
    p_map.add_argument("--pid", default="PID", help="PID to put in the newuidmap/newgidmap command lines")
    p_map.add_argument("--json", action="store_true")
    p_map.set_defaults(handler=cmd_map)

    p_tr = sub.add_parser("translate", help="which host id is container id N, and back")
    add_user_options(p_tr)
    p_tr.add_argument("--map", action="append", nargs=3, metavar=("INSIDE", "OUTSIDE", "COUNT"), help="an explicit map line (repeatable) instead of /etc/subuid")
    p_tr.add_argument("--uid-map", help="a saved /proc/PID/uid_map (or gid_map) file instead of /etc/subuid")
    p_tr.add_argument("--kind", choices=("uid", "gid"), default="uid")
    p_tr.add_argument("--from-host", action="store_true", help="translate host ids to container ids (default: container to host)")
    p_tr.add_argument("--json", action="store_true")
    p_tr.add_argument("ids", nargs="+", type=int)
    p_tr.set_defaults(handler=cmd_translate)

    p_ex = sub.add_parser("explain", help="read /proc/PID/uid_map and gid_map and say what they mean")
    p_ex.add_argument("pid", nargs="?", type=int)
    p_ex.add_argument("--uid-map", help="explain a saved uid_map file instead of a PID")
    p_ex.add_argument("--gid-map", help="a saved gid_map file to go with --uid-map")
    p_ex.add_argument("--root", default="/proc", help="procfs root (a fake tree in tests)")
    p_ex.add_argument("--json", action="store_true")
    p_ex.set_defaults(handler=cmd_explain)

    p_run = sub.add_parser("run", help="Linux: unshare a user namespace, map yourself to uid 0 and exec a command")
    p_run.add_argument("--ns", default="", help="extra namespaces to unshare as the new root: mnt,pid,uts,ipc,net")
    p_run.add_argument("command", nargs=argparse.REMAINDER)
    p_run.set_defaults(handler=cmd_run)
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(list(sys.argv[1:] if argv is None else argv))
    try:
        return args.handler(args)
    except ValueError as exc:
        print(f"userns: {exc}", file=sys.stderr)
        return 2
    except OSError as exc:
        print(f"userns: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
