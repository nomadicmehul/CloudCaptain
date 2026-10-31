"""mini_container: run a command inside its own UTS, PID and mount namespaces, with an optional memory budget."""
from __future__ import annotations

import argparse
import ctypes
import os
import sys
from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence, Tuple

CLONE_NEWNS = 0x00020000
CLONE_NEWUTS = 0x04000000
CLONE_NEWIPC = 0x08000000
CLONE_NEWPID = 0x20000000
CLONE_NEWNET = 0x40000000

MS_REC = 0x4000
MS_PRIVATE = 0x40000

NAMESPACE_FLAGS: Dict[str, int] = {
    "mnt": CLONE_NEWNS,
    "uts": CLONE_NEWUTS,
    "ipc": CLONE_NEWIPC,
    "pid": CLONE_NEWPID,
    "net": CLONE_NEWNET,
}

UNITS: Dict[str, int] = {"": 1, "K": 1024, "M": 1024 ** 2, "G": 1024 ** 3}
DEFAULT_NAMESPACES: Tuple[str, ...] = ("uts", "pid", "mnt")


@dataclass
class ContainerSpec:
    command: List[str]
    name: str = "mini"
    hostname: str = "box"
    rootfs: Optional[str] = None
    namespaces: Tuple[str, ...] = DEFAULT_NAMESPACES
    memory: Optional[str] = None
    cgroup_base: str = "/sys/fs/cgroup"
    dry_run: bool = False


def parse_memory(value: str) -> int:
    text = value.strip().upper()
    if text.endswith("B"):
        text = text[:-1]
    digits = text.rstrip("KMG")
    unit = text[len(digits):]
    if not digits.isdigit() or unit not in UNITS:
        raise ValueError(f"invalid memory size {value!r}; use a number with K, M or G, e.g. 100M")
    return int(digits) * UNITS[unit]


def unshare_flags(namespaces: Sequence[str]) -> int:
    flags = 0
    for namespace in namespaces:
        if namespace not in NAMESPACE_FLAGS:
            raise ValueError(f"unknown namespace {namespace!r}; choose from {sorted(NAMESPACE_FLAGS)}")
        flags |= NAMESPACE_FLAGS[namespace]
    return flags


def cgroup_files(spec: ContainerSpec, pid: int) -> Dict[str, str]:
    if spec.memory is None:
        return {}
    group = os.path.join(spec.cgroup_base, spec.name)
    return {
        os.path.join(group, "memory.max"): str(parse_memory(spec.memory)),
        os.path.join(group, "cgroup.procs"): str(pid),
    }


def explain(spec: ContainerSpec) -> List[str]:
    lines = [f"process: {' '.join(spec.command)}"]
    if "uts" in spec.namespaces:
        lines.append(f"uts namespace: hostname inside becomes {spec.hostname!r}; the host keeps its own")
    if "pid" in spec.namespaces:
        lines.append("pid namespace: the command sees itself as PID 1 and cannot see host processes")
    if "mnt" in spec.namespaces:
        lines.append("mnt namespace: mounts made inside (such as /proc) stay inside")
    if "net" in spec.namespaces:
        lines.append("net namespace: a fresh network stack with only a loopback interface")
    if "ipc" in spec.namespaces:
        lines.append("ipc namespace: separate System V IPC and POSIX message queues")
    if spec.rootfs:
        lines.append(f"chroot: / inside becomes {spec.rootfs}")
    if spec.memory:
        lines.append(f"cgroup: {spec.name}/memory.max = {parse_memory(spec.memory)} bytes ({spec.memory})")
    return lines


def parse_args(argv: Sequence[str]) -> ContainerSpec:
    parser = argparse.ArgumentParser(prog="mini_container", description=__doc__)
    parser.add_argument("--name", default="mini", help="cgroup name under /sys/fs/cgroup")
    parser.add_argument("--hostname", default="box")
    parser.add_argument("--rootfs", help="directory to chroot into (for example an exported image)")
    parser.add_argument("--ns", default=",".join(DEFAULT_NAMESPACES), help="comma-separated: uts,pid,mnt,net,ipc")
    parser.add_argument("--memory", help="cgroup v2 memory.max, for example 100M")
    parser.add_argument("--explain", action="store_true", help="print what would be isolated and exit")
    parser.add_argument("command", nargs=argparse.REMAINDER)
    parsed = parser.parse_args(list(argv))
    command = list(parsed.command)
    if command and command[0] == "--":
        command = command[1:]
    if not command:
        command = ["/bin/sh"]
    spec = ContainerSpec(
        command=command,
        name=parsed.name,
        hostname=parsed.hostname,
        rootfs=parsed.rootfs,
        namespaces=tuple(part.strip() for part in parsed.ns.split(",") if part.strip()),
        memory=parsed.memory,
        dry_run=parsed.explain,
    )
    unshare_flags(spec.namespaces)
    if spec.memory is not None:
        parse_memory(spec.memory)
    return spec


def write_cgroup(files: Dict[str, str]) -> None:
    for file_path, value in files.items():
        os.makedirs(os.path.dirname(file_path), exist_ok=True)
        with open(file_path, "w", encoding="ascii") as handle:
            handle.write(value)


def child(spec: ContainerSpec, libc: ctypes.CDLL) -> None:
    if "uts" in spec.namespaces:
        name = spec.hostname.encode()
        libc.sethostname(name, len(name))
    if "mnt" in spec.namespaces:
        libc.mount(None, b"/", None, MS_REC | MS_PRIVATE, None)
    if spec.rootfs:
        os.chroot(spec.rootfs)
        os.chdir("/")
    if "mnt" in spec.namespaces and "pid" in spec.namespaces:
        os.makedirs("/proc", exist_ok=True)
        libc.mount(b"proc", b"/proc", b"proc", 0, None)
    os.execvp(spec.command[0], spec.command)


def run(spec: ContainerSpec) -> int:
    if sys.platform != "linux":
        raise SystemExit("run needs Linux; use --explain on other systems")
    if os.geteuid() != 0:
        raise SystemExit("run needs root for unshare(2) and cgroups: sudo python3 main.py ...")
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.unshare(unshare_flags(spec.namespaces)) != 0:
        errno = ctypes.get_errno()
        raise OSError(errno, f"unshare failed: {os.strerror(errno)}")
    pid = os.fork()
    if pid == 0:
        child(spec, libc)
    write_cgroup(cgroup_files(spec, pid))
    _, status = os.waitpid(pid, 0)
    return os.waitstatus_to_exitcode(status)


def main(argv: Optional[Sequence[str]] = None) -> int:
    spec = parse_args(sys.argv[1:] if argv is None else argv)
    if spec.dry_run:
        print("\n".join(explain(spec)))
        return 0
    return run(spec)


if __name__ == "__main__":
    raise SystemExit(main())
