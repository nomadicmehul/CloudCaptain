"""capcheck: decode capability bitmasks into names, evaluate file permissions for a user, and explain what a process may do from /proc/PID/status."""
from __future__ import annotations

import argparse
import grp
import json
import os
import pwd
import re
import stat
import sys
from dataclasses import asdict, dataclass
from typing import Dict, List, Optional, Sequence, Tuple

CAPABILITIES = (
    "CAP_CHOWN",
    "CAP_DAC_OVERRIDE",
    "CAP_DAC_READ_SEARCH",
    "CAP_FOWNER",
    "CAP_FSETID",
    "CAP_KILL",
    "CAP_SETGID",
    "CAP_SETUID",
    "CAP_SETPCAP",
    "CAP_LINUX_IMMUTABLE",
    "CAP_NET_BIND_SERVICE",
    "CAP_NET_BROADCAST",
    "CAP_NET_ADMIN",
    "CAP_NET_RAW",
    "CAP_IPC_LOCK",
    "CAP_IPC_OWNER",
    "CAP_SYS_MODULE",
    "CAP_SYS_RAWIO",
    "CAP_SYS_CHROOT",
    "CAP_SYS_PTRACE",
    "CAP_SYS_PACCT",
    "CAP_SYS_ADMIN",
    "CAP_SYS_BOOT",
    "CAP_SYS_NICE",
    "CAP_SYS_RESOURCE",
    "CAP_SYS_TIME",
    "CAP_SYS_TTY_CONFIG",
    "CAP_MKNOD",
    "CAP_LEASE",
    "CAP_AUDIT_WRITE",
    "CAP_AUDIT_CONTROL",
    "CAP_SETFCAP",
    "CAP_MAC_OVERRIDE",
    "CAP_MAC_ADMIN",
    "CAP_SYSLOG",
    "CAP_WAKE_ALARM",
    "CAP_BLOCK_SUSPEND",
    "CAP_AUDIT_READ",
    "CAP_PERFMON",
    "CAP_BPF",
    "CAP_CHECKPOINT_RESTORE",
)
CAPABILITY_COUNT = len(CAPABILITIES)
ALL_CAPS = (1 << CAPABILITY_COUNT) - 1
DOCKER_DEFAULT = 0x00000000A80425FB
ALIASES = {"all": ALL_CAPS, "none": 0, "docker-default": DOCKER_DEFAULT}
INDEX = {name: bit for bit, name in enumerate(CAPABILITIES)}

MEANINGS = {
    "CAP_CHOWN": "change the owner and group of any file",
    "CAP_DAC_OVERRIDE": "bypass read, write and execute permission checks (execute still needs one x bit on files)",
    "CAP_DAC_READ_SEARCH": "bypass read checks on files and read and search checks on directories",
    "CAP_FOWNER": "act as the owner of any file: chmod, chattr, utimes, delete in sticky directories",
    "CAP_FSETID": "keep setuid and setgid bits when writing a file; set setgid for a group you are not in",
    "CAP_KILL": "send signals to processes owned by other users",
    "CAP_SETGID": "change gid and supplementary groups; write gid_map for a user namespace",
    "CAP_SETUID": "change uid; write uid_map for a user namespace",
    "CAP_SETPCAP": "change the bounding set and securebits; add capabilities to the inheritable set",
    "CAP_LINUX_IMMUTABLE": "set the immutable and append-only file attributes",
    "CAP_NET_BIND_SERVICE": "bind to ports below 1024",
    "CAP_NET_BROADCAST": "broadcast and listen to multicast (unused in current kernels)",
    "CAP_NET_ADMIN": "configure interfaces, routes, firewall rules and promiscuous mode",
    "CAP_NET_RAW": "raw and packet sockets: ping, tcpdump, ARP spoofing",
    "CAP_IPC_LOCK": "lock memory with mlock and mlockall",
    "CAP_IPC_OWNER": "bypass permission checks on System V IPC objects",
    "CAP_SYS_MODULE": "load and unload kernel modules",
    "CAP_SYS_RAWIO": "raw I/O: ioperm, iopl, /dev/mem, raw block devices",
    "CAP_SYS_CHROOT": "chroot and switch mount namespaces with setns",
    "CAP_SYS_PTRACE": "trace and read the memory of any process",
    "CAP_SYS_PACCT": "configure process accounting",
    "CAP_SYS_ADMIN": "mount, swapon, namespaces, setns, quotas and many ioctls: the catch-all",
    "CAP_SYS_BOOT": "reboot and kexec",
    "CAP_SYS_NICE": "raise priority, real-time scheduling and CPU affinity of other processes",
    "CAP_SYS_RESOURCE": "override resource limits, quotas and reserved space",
    "CAP_SYS_TIME": "set the system clock and the real-time clock",
    "CAP_SYS_TTY_CONFIG": "vhangup and configure virtual terminals",
    "CAP_MKNOD": "create device nodes with mknod",
    "CAP_LEASE": "establish file leases on files owned by others",
    "CAP_AUDIT_WRITE": "write records to the kernel audit log",
    "CAP_AUDIT_CONTROL": "enable, disable and configure kernel auditing",
    "CAP_SETFCAP": "set file capabilities on any file",
    "CAP_MAC_OVERRIDE": "override mandatory access control (Smack)",
    "CAP_MAC_ADMIN": "configure mandatory access control (Smack)",
    "CAP_SYSLOG": "privileged syslog operations and reading kernel addresses from /proc",
    "CAP_WAKE_ALARM": "timers that wake the system from suspend",
    "CAP_BLOCK_SUSPEND": "block system suspend",
    "CAP_AUDIT_READ": "read the audit log through a netlink multicast socket",
    "CAP_PERFMON": "perf_event_open and other performance monitoring",
    "CAP_BPF": "load BPF programs and create maps (with CAP_PERFMON or CAP_NET_ADMIN to attach them)",
    "CAP_CHECKPOINT_RESTORE": "checkpoint and restore processes: set_tid, /proc/PID/map_files",
}
ROOT_EQUIVALENT = frozenset({
    "CAP_CHOWN",
    "CAP_DAC_OVERRIDE",
    "CAP_DAC_READ_SEARCH",
    "CAP_FOWNER",
    "CAP_SETUID",
    "CAP_SETGID",
    "CAP_SETFCAP",
    "CAP_SETPCAP",
    "CAP_SYS_ADMIN",
    "CAP_SYS_PTRACE",
    "CAP_SYS_MODULE",
    "CAP_SYS_RAWIO",
    "CAP_SYS_BOOT",
    "CAP_NET_ADMIN",
    "CAP_MKNOD",
    "CAP_BPF",
})
SECCOMP_MODES = {0: "disabled", 1: "strict: only read, write, exit and sigreturn", 2: "filter: a BPF program decides per system call"}
HEX_RE = re.compile(r"^(0x)?[0-9a-fA-F]{1,16}$")
LS_RE = re.compile(r"^([-dlcbps])?([r-][w-][xsS-])([r-][w-][xsS-])([r-][w-][xtT-])[.+@]?$")
KIND_CHARS = {"-": "file", "d": "directory", "l": "symlink", "c": "char device", "b": "block device", "p": "fifo", "s": "socket"}
CHAR_KINDS = {kind: char for char, kind in KIND_CHARS.items()}


def normalize_name(name: str) -> str:
    upper = name.strip().upper()
    full = upper if upper.startswith("CAP_") else "CAP_" + upper
    if full not in INDEX:
        raise ValueError(f"unknown capability {name!r}; run with 'all' to list the {CAPABILITY_COUNT} names")
    return full


def parse_mask(text: str) -> int:
    value = text.strip()
    if not value:
        raise ValueError("empty capability mask")
    if value.lower() in ALIASES:
        return ALIASES[value.lower()]
    if "=" in value:
        left, _, _ = value.partition("=")
        value = left.strip() if HEX_RE.match(left.strip()) else left.rpartition(":")[2].strip()
    if HEX_RE.match(value):
        return int(value, 16)
    mask = 0
    for part in re.split(r"[,\s]+", value):
        if part:
            mask |= 1 << INDEX[normalize_name(part)]
    return mask


def decode(mask: int) -> List[str]:
    if mask < 0:
        raise ValueError("a capability mask cannot be negative")
    names = []
    for bit in range(max(CAPABILITY_COUNT, mask.bit_length())):
        if mask >> bit & 1:
            names.append(CAPABILITIES[bit] if bit < CAPABILITY_COUNT else f"bit{bit}")
    return names


def encode(names: Sequence[str]) -> int:
    mask = 0
    for name in names:
        mask |= 1 << INDEX[normalize_name(name)]
    return mask


def has_cap(mask: int, name: str) -> bool:
    return bool(mask >> INDEX[name] & 1)


def format_mask(mask: int) -> str:
    return f"0x{mask:016x}"


def label_for(mask: int) -> str:
    if mask == ALL_CAPS:
        return f"all {CAPABILITY_COUNT} capabilities: full root, what --privileged grants"
    if mask == 0:
        return "no capabilities: what --cap-drop ALL leaves"
    if mask == DOCKER_DEFAULT:
        return f"{len(decode(mask))} of {CAPABILITY_COUNT} capabilities: the Docker default set"
    return f"{len(decode(mask))} of {CAPABILITY_COUNT} capabilities"


def diff_masks(a: int, b: int) -> Dict[str, List[str]]:
    return {"only_a": decode(a & ~b), "only_b": decode(b & ~a), "both": decode(a & b)}


def render_cap_lines(mask: int, indent: str = "  ") -> List[str]:
    lines = []
    for name in decode(mask):
        bit = INDEX.get(name, -1)
        flag = "!" if name in ROOT_EQUIVALENT else " "
        lines.append(f"{indent}{bit:>2}  {name:<24}{flag}  {MEANINGS.get(name, '')}".rstrip())
    return lines


def render_caps(mask: int) -> str:
    lines = [f"{format_mask(mask)}: {label_for(mask)}"] + render_cap_lines(mask)
    risky = [name for name in decode(mask) if name in ROOT_EQUIVALENT]
    if mask and not risky:
        lines.append("none of these is root-equivalent on its own")
    elif risky:
        lines.append(f"root-equivalent in practice ({len(risky)}, marked !): {', '.join(risky)}")
    return "\n".join(lines)


def render_diff(a: int, b: int) -> str:
    d = diff_masks(a, b)
    lines = [f"A {format_mask(a)} ({len(decode(a))})  B {format_mask(b)} ({len(decode(b))})"]
    lines.append(f"dropped going A -> B ({len(d['only_a'])}): {', '.join(d['only_a']) or 'none'}")
    lines.append(f"added going A -> B ({len(d['only_b'])}): {', '.join(d['only_b']) or 'none'}")
    lines.append(f"kept ({len(d['both'])}): {', '.join(d['both']) or 'none'}")
    return "\n".join(lines)


@dataclass
class Mode:
    perm: int
    kind: str = "file"

    @property
    def setuid(self) -> bool:
        return bool(self.perm & stat.S_ISUID)

    @property
    def setgid(self) -> bool:
        return bool(self.perm & stat.S_ISGID)

    @property
    def sticky(self) -> bool:
        return bool(self.perm & stat.S_ISVTX)

    def bits_for(self, klass: str) -> int:
        shift = {"owner": 6, "group": 3, "other": 0}[klass]
        return (self.perm >> shift) & 7


@dataclass
class Entry:
    mode: Mode
    owner: str
    group: str


@dataclass
class Identity:
    user: str
    groups: List[str]


@dataclass
class Decision:
    klass: str
    bits: str
    read: bool
    write: bool
    execute: bool
    traverse: Optional[bool]
    notes: List[str]


def parse_mode(text: str, directory: bool = False) -> Mode:
    value = text.strip()
    match = LS_RE.match(value)
    if match:
        type_char, user, group, other = match.groups()
        perm = 0
        for triple, shift, special in ((user, 6, stat.S_ISUID), (group, 3, stat.S_ISGID), (other, 0, stat.S_ISVTX)):
            if triple[0] == "r":
                perm |= 4 << shift
            if triple[1] == "w":
                perm |= 2 << shift
            if triple[2] in "xst":
                perm |= 1 << shift
            if triple[2] in "sStT":
                perm |= special
        kind = KIND_CHARS[type_char] if type_char else ("directory" if directory else "file")
        return Mode(perm, kind)
    digits = value[2:] if value.lower().startswith("0o") else value
    if not digits or not all(c in "01234567" for c in digits) or len(digits) > 5:
        raise ValueError(f"cannot read mode {text!r}: use octal (0755, 4755, 1777) or an ls -l string (-rwsr-xr-x)")
    perm = int(digits, 8)
    if perm > 0o7777:
        raise ValueError(f"mode {text!r} is larger than 7777")
    return Mode(perm, "directory" if directory else "file")


def format_mode(mode: Mode) -> str:
    out = [CHAR_KINDS.get(mode.kind, "?")]
    for shift, special, letter in ((6, stat.S_ISUID, "s"), (3, stat.S_ISGID, "s"), (0, stat.S_ISVTX, "t")):
        bits = (mode.perm >> shift) & 7
        out.append("r" if bits & 4 else "-")
        out.append("w" if bits & 2 else "-")
        has_x = bool(bits & 1)
        if mode.perm & special:
            out.append(letter if has_x else letter.upper())
        else:
            out.append("x" if has_x else "-")
    return "".join(out)


def triple(bits: int) -> str:
    return ("r" if bits & 4 else "-") + ("w" if bits & 2 else "-") + ("x" if bits & 1 else "-")


def permission_class(entry: Entry, who: Identity) -> str:
    if who.user == entry.owner:
        return "owner"
    if entry.group in who.groups:
        return "group"
    return "other"


def access(entry: Entry, who: Identity, caps: int = 0) -> Decision:
    klass = permission_class(entry, who)
    bits = entry.mode.bits_for(klass)
    read, write, execute = bool(bits & 4), bool(bits & 2), bool(bits & 1)
    is_dir = entry.mode.kind == "directory"
    notes = [f"checked as {klass} ({triple(bits)}); only that class's bits are consulted"]
    if klass == "owner" and entry.mode.bits_for("group") & ~bits:
        notes.append(f"the group bits ({triple(entry.mode.bits_for('group'))}) are wider but do not apply: a uid match ends the search")
    if has_cap(caps, "CAP_DAC_OVERRIDE"):
        read = write = True
        execute = True if is_dir else (execute or bool(entry.mode.perm & 0o111))
        notes.append("CAP_DAC_OVERRIDE: read and write checks bypassed" + ("" if is_dir else "; execute needs at least one x bit on the file"))
    elif has_cap(caps, "CAP_DAC_READ_SEARCH"):
        read = True
        execute = True if is_dir else execute
        notes.append("CAP_DAC_READ_SEARCH: read" + (" and traverse" if is_dir else "") + " checks bypassed; write is still checked")
    traverse: Optional[bool] = None
    if entry.mode.kind == "symlink":
        notes.append("symlink: these bits are ignored, the target's mode decides")
    if is_dir:
        traverse = execute
        if write and not execute:
            notes.append("w without x: cannot create or delete entries; both bits are needed")
        if read and not execute:
            notes.append("r without x: names can be listed but not opened or stat'ed")
        if execute and not read:
            notes.append("x without r: entries are reachable by exact name but the directory cannot be listed")
        write = write and execute
        if entry.mode.sticky and write:
            if who.user == entry.owner or has_cap(caps, "CAP_FOWNER"):
                notes.append("sticky: may delete any entry (directory owner or CAP_FOWNER)")
            else:
                notes.append("sticky: may delete only entries it owns")
        if entry.mode.setgid:
            notes.append(f"setgid directory: new entries get group {entry.group}")
    else:
        if entry.mode.setuid and execute:
            notes.append(f"setuid: runs with effective uid {entry.owner}; ignored for scripts, on nosuid mounts and when NoNewPrivs=1")
        if entry.mode.setgid and entry.mode.perm & 0o010 and execute:
            notes.append(f"setgid: runs with effective gid {entry.group}")
        if execute and not read:
            notes.append("x without r: a binary runs, a script does not (the interpreter has to read it)")
        if has_cap(caps, "CAP_FOWNER"):
            notes.append("CAP_FOWNER: owner-only operations (chmod, chown group, utimes) allowed")
    return Decision(klass, triple(bits), read, write, execute, traverse, notes)


def current_identity() -> Identity:
    uid = os.getuid()
    try:
        user = pwd.getpwuid(uid).pw_name
    except KeyError:
        user = str(uid)
    groups = []
    for gid in sorted(set(os.getgroups()) | {os.getgid()}):
        try:
            groups.append(grp.getgrgid(gid).gr_name)
        except KeyError:
            groups.append(str(gid))
    return Identity(user, groups)


def entry_from_path(path: str) -> Entry:
    st = os.stat(path)
    if stat.S_ISDIR(st.st_mode):
        kind = "directory"
    elif stat.S_ISCHR(st.st_mode):
        kind = "char device"
    elif stat.S_ISBLK(st.st_mode):
        kind = "block device"
    elif stat.S_ISFIFO(st.st_mode):
        kind = "fifo"
    elif stat.S_ISSOCK(st.st_mode):
        kind = "socket"
    else:
        kind = "file"
    try:
        owner = pwd.getpwuid(st.st_uid).pw_name
    except KeyError:
        owner = str(st.st_uid)
    try:
        group = grp.getgrgid(st.st_gid).gr_name
    except KeyError:
        group = str(st.st_gid)
    return Entry(Mode(stat.S_IMODE(st.st_mode), kind), owner, group)


def yes_no(value: Optional[bool]) -> str:
    return "-" if value is None else ("yes" if value else "no")


def render_access(entry: Entry, who: Identity, decision: Decision, caps: int = 0) -> str:
    header = f"{format_mode(entry.mode)} {entry.owner} {entry.group}  (mode {entry.mode.perm:04o}, {entry.mode.kind}) as {who.user} (groups: {', '.join(who.groups) or 'none'})"
    if caps:
        header += f" with {len(decode(caps))} capabilities"
    lines = [header]
    if entry.mode.kind == "directory":
        lines.append(f"  list      {yes_no(decision.read)}   (r: read the names)")
        lines.append(f"  traverse  {yes_no(decision.traverse)}   (x: reach an entry by name, cd into it)")
        lines.append(f"  modify    {yes_no(decision.write)}   (w and x: create, rename and delete entries)")
    else:
        lines.append(f"  read      {yes_no(decision.read)}")
        lines.append(f"  write     {yes_no(decision.write)}")
        lines.append(f"  execute   {yes_no(decision.execute)}")
        lines.append("  (reaching the file also needs traverse on every parent directory)")
    for note in decision.notes:
        lines.append(f"  - {note}")
    return "\n".join(lines)


def access_json(entry: Entry, who: Identity, decision: Decision, caps: int) -> dict:
    return {
        "ls": format_mode(entry.mode),
        "mode": f"{entry.mode.perm:04o}",
        "kind": entry.mode.kind,
        "owner": entry.owner,
        "group": entry.group,
        "user": who.user,
        "groups": who.groups,
        "caps": decode(caps),
        **asdict(decision),
    }


@dataclass
class Status:
    name: str
    pid: Optional[int]
    uid: Tuple[int, int, int, int]
    gid: Tuple[int, int, int, int]
    groups: List[int]
    inheritable: int
    permitted: int
    effective: int
    bounding: int
    ambient: int
    no_new_privs: Optional[int]
    seccomp: Optional[int]
    seccomp_filters: Optional[int]


def parse_status(text: str) -> Status:
    fields: Dict[str, str] = {}
    for line in text.splitlines():
        key, sep, value = line.partition(":")
        if sep:
            fields[key.strip()] = value.strip()
    if "Uid" not in fields or "CapEff" not in fields:
        raise ValueError("not a /proc/PID/status file: no Uid and CapEff lines")

    def ids(key: str) -> Tuple[int, int, int, int]:
        parts = [int(p) for p in fields.get(key, "").split()[:4]]
        while len(parts) < 4:
            parts.append(parts[-1] if parts else 0)
        return parts[0], parts[1], parts[2], parts[3]

    def mask(key: str) -> int:
        return int(fields.get(key, "0") or "0", 16)

    def number(key: str) -> Optional[int]:
        value = fields.get(key)
        return int(value) if value and value.isdigit() else None

    return Status(
        fields.get("Name", "?"),
        number("Pid"),
        ids("Uid"),
        ids("Gid"),
        [int(g) for g in fields.get("Groups", "").split()],
        mask("CapInh"),
        mask("CapPrm"),
        mask("CapEff"),
        mask("CapBnd"),
        mask("CapAmb"),
        number("NoNewPrivs"),
        number("Seccomp"),
        number("Seccomp_filters"),
    )


def read_status(target: str, root: str = "/proc") -> Status:
    path = target if os.path.isfile(target) else os.path.join(root, target, "status")
    if not os.path.isfile(path):
        raise FileNotFoundError(f"no status file at {path}; pass a PID, 'self', or a saved copy of /proc/PID/status")
    with open(path, encoding="utf-8", errors="replace") as handle:
        return parse_status(handle.read())


def verdict(status: Status) -> str:
    euid = status.uid[1]
    effective = status.effective
    risky = [name for name in decode(effective) if name in ROOT_EQUIVALENT]
    if euid == 0 and effective == ALL_CAPS:
        return "full root: every capability is effective (a host root shell or --privileged); file permission bits do not apply to it"
    if euid == 0 and effective == 0:
        return "uid 0 with no capabilities (--cap-drop ALL): root in name only; EACCES on files it does not own, EPERM on chown, mknod and ports below 1024"
    if euid == 0:
        tail = f"root-equivalent bits present: {', '.join(risky)}" if risky else "none of them root-equivalent"
        return f"uid 0 with {len(decode(effective))} of {CAPABILITY_COUNT} capabilities; {tail}"
    if effective == 0:
        gainable = decode(status.bounding)
        if status.no_new_privs:
            path = "and NoNewPrivs=1 closes even that path"
        elif gainable:
            path = f"the bounding set would let file capabilities grant {len(gainable)}: {', '.join(gainable[:4])}{'...' if len(gainable) > 4 else ''}"
        else:
            path = "and the bounding set is empty, so nothing can ever be gained"
        return f"unprivileged uid {euid}: permission bits decide everything; {path}"
    return f"uid {euid} holding {', '.join(decode(effective))}: granted by file capabilities or the ambient set" + (f"; root-equivalent: {', '.join(risky)}" if risky else "")


def explain(status: Status) -> str:
    uid, euid = status.uid[0], status.uid[1]
    who = f"uid {uid}" + (f" (effective {euid}, saved {status.uid[2]})" if euid != uid or status.uid[2] != uid else "")
    lines = [f"pid {status.pid if status.pid is not None else '?'} ({status.name}): {who}, gid {status.gid[1]}, groups {' '.join(map(str, status.groups)) or 'none'}"]
    lines.append(f"effective    {format_mask(status.effective)}  {label_for(status.effective)}")
    lines.extend(render_cap_lines(status.effective, "    "))
    if status.permitted == status.effective:
        lines.append("permitted    same as effective")
    else:
        lines.append(f"permitted    {format_mask(status.permitted)}  {label_for(status.permitted)}; not effective: {', '.join(decode(status.permitted & ~status.effective)) or 'none'}")
    lines.append(f"inheritable  {format_mask(status.inheritable)}  {', '.join(decode(status.inheritable)) or 'none'}")
    missing = decode(ALL_CAPS & ~status.bounding)
    lines.append(f"bounding     {format_mask(status.bounding)}  {len(decode(status.bounding))} of {CAPABILITY_COUNT} allowed; {len(missing)} can never be gained, not even by a setuid binary")
    lines.append(f"ambient      {format_mask(status.ambient)}  {', '.join(decode(status.ambient)) or 'none (a non-root program gains capabilities only from file capabilities)'}")
    if status.no_new_privs is None:
        lines.append("NoNewPrivs   unknown (old kernel)")
    elif status.no_new_privs:
        lines.append("NoNewPrivs   1: execve cannot add privileges; setuid bits and file capabilities are ignored")
    else:
        lines.append("NoNewPrivs   0: execve may add privileges through setuid bits and file capabilities")
    if status.seccomp is None:
        lines.append("Seccomp      unknown")
    else:
        filters = f", {status.seccomp_filters} filter(s)" if status.seccomp_filters is not None else ""
        lines.append(f"Seccomp      {status.seccomp}: {SECCOMP_MODES.get(status.seccomp, 'unknown mode')}{filters}")
    lines.append(f"verdict: {verdict(status)}")
    return "\n".join(lines)


def status_json(status: Status) -> dict:
    data = asdict(status)
    for key in ("inheritable", "permitted", "effective", "bounding", "ambient"):
        data[key] = {"mask": format_mask(getattr(status, key)), "names": decode(getattr(status, key))}
    data["verdict"] = verdict(status)
    return data


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="capcheck", description=__doc__)
    parser.add_argument("mask", nargs="?", help="capability mask to decode: hex from /proc/PID/status or capsh, a list of names, 'all', 'none' or 'docker-default'")
    parser.add_argument("--diff", nargs=2, metavar=("A", "B"), help="compare two masks")
    parser.add_argument("--perms", metavar="MODE", help="file mode as octal (0755, 4755, 1777) or ls -l string (-rwsr-xr-x)")
    parser.add_argument("--dir", action="store_true", help="treat an octal --perms mode as a directory")
    parser.add_argument("--owner", default="root", help="file owner for --perms")
    parser.add_argument("--group", default="root", help="file group for --perms")
    parser.add_argument("--user", help="who is asking (default: you)")
    parser.add_argument("--groups", help="comma-separated groups of --user (default: the user's name)")
    parser.add_argument("--caps", default="none", help="effective capabilities of the asking process: mask, names, all, docker-default")
    parser.add_argument("--path", help="evaluate a real file for the current user instead of --perms")
    parser.add_argument("--explain", metavar="PID|FILE", help="read /proc/PID/status (or a saved copy) and explain it")
    parser.add_argument("--root", default="/proc", help="procfs root for --explain (a fake tree in tests)")
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    args = parser.parse_args(list(sys.argv[1:] if argv is None else argv))
    try:
        if args.diff:
            a, b = parse_mask(args.diff[0]), parse_mask(args.diff[1])
            print(json.dumps({"a": format_mask(a), "b": format_mask(b), **diff_masks(a, b)}, indent=2) if args.json else render_diff(a, b))
            return 0
        if args.perms or args.path:
            caps = parse_mask(args.caps)
            if args.path:
                entry = entry_from_path(args.path)
                who = current_identity()
            else:
                entry = Entry(parse_mode(args.perms, args.dir), args.owner, args.group)
                who = Identity(args.user, args.groups.split(",") if args.groups else [args.user]) if args.user else current_identity()
            decision = access(entry, who, caps)
            print(json.dumps(access_json(entry, who, decision, caps), indent=2) if args.json else render_access(entry, who, decision, caps))
            return 0
        if args.explain:
            if not os.path.isfile(args.explain) and not os.path.isdir(args.root):
                print(f"capcheck: {args.root} not found; this needs Linux procfs. Save /proc/PID/status on a Linux box and pass the file instead", file=sys.stderr)
                return 2
            status = read_status(args.explain, args.root)
            print(json.dumps(status_json(status), indent=2) if args.json else explain(status))
            return 0
        if args.mask is not None:
            mask = parse_mask(args.mask)
            print(json.dumps({"mask": format_mask(mask), "count": len(decode(mask)), "names": decode(mask), "root_equivalent": [n for n in decode(mask) if n in ROOT_EQUIVALENT]}, indent=2) if args.json else render_caps(mask))
            return 0
    except ValueError as exc:
        print(f"capcheck: {exc}", file=sys.stderr)
        return 2
    except OSError as exc:
        hint = "; run with sudo to read another user's process" if isinstance(exc, PermissionError) else ""
        print(f"capcheck: {exc}{hint}", file=sys.stderr)
        return 1
    parser.error("pass a MASK, --diff A B, --perms MODE, --path FILE or --explain PID")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
