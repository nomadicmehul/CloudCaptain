"""hardencheck: read a seccomp profile, generate one from an strace log, and score the runtime hardening of a Docker container or a Kubernetes pod with the fix for every gap."""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from collections import Counter
from dataclasses import asdict, dataclass
from typing import Callable, Dict, List, Optional, Sequence, Set, Tuple

ALLOW = "SCMP_ACT_ALLOW"
ERRNO = "SCMP_ACT_ERRNO"
DOCKER_DEFAULT_CAPS = frozenset(
    {
        "CAP_AUDIT_WRITE",
        "CAP_CHOWN",
        "CAP_DAC_OVERRIDE",
        "CAP_FOWNER",
        "CAP_FSETID",
        "CAP_KILL",
        "CAP_MKNOD",
        "CAP_NET_BIND_SERVICE",
        "CAP_NET_RAW",
        "CAP_SETFCAP",
        "CAP_SETGID",
        "CAP_SETPCAP",
        "CAP_SETUID",
        "CAP_SYS_CHROOT",
    }
)
NOTABLE = {
    "mount": "change the mount table: bind the host filesystem into reach",
    "umount2": "remove a mount, for example a read-only or masked one",
    "pivot_root": "swap the root filesystem",
    "chroot": "change the root directory",
    "ptrace": "attach to another process and read or change its memory",
    "process_vm_readv": "read another process's memory without attaching",
    "unshare": "create new namespaces",
    "setns": "enter another process's namespaces",
    "clone": "fork, with flags that can create namespaces (CLONE_NEWUSER)",
    "bpf": "load eBPF programs into the kernel",
    "perf_event_open": "kernel profiling and a long history of information leaks",
    "init_module": "load a kernel module",
    "finit_module": "load a kernel module from a file descriptor",
    "delete_module": "unload a kernel module",
    "kexec_load": "replace the running kernel",
    "reboot": "reboot or halt the host",
    "open_by_handle_at": "open a file by handle, bypassing path lookup (the shocker escape)",
    "name_to_handle_at": "obtain the handles used by open_by_handle_at",
    "keyctl": "the kernel keyring, shared across containers on older kernels",
    "add_key": "add to the kernel keyring",
    "request_key": "read from the kernel keyring",
    "settimeofday": "set the system clock",
    "clock_settime": "set a system clock",
    "swapon": "enable a swap device",
    "acct": "process accounting",
    "iopl": "raw I/O port access",
    "ioperm": "raw I/O port permissions",
    "userfaultfd": "handle page faults in user space, a common exploit primitive",
    "personality": "change the execution domain, used to disable ASLR",
    "quotactl": "disk quota control",
    "uselib": "load an a.out shared library",
    "vm86": "legacy 16-bit emulation",
    "sysfs": "legacy filesystem type queries",
    "mkdir": "create a directory (allowed by default; a good canary for a custom profile)",
}
ALWAYS_NEEDED = (
    "access",
    "arch_prctl",
    "brk",
    "capget",
    "capset",
    "chdir",
    "clock_gettime",
    "clone",
    "clone3",
    "close",
    "close_range",
    "dup",
    "dup2",
    "dup3",
    "epoll_create1",
    "epoll_ctl",
    "epoll_pwait",
    "epoll_wait",
    "execve",
    "exit",
    "exit_group",
    "fchdir",
    "fcntl",
    "fstat",
    "fstatfs",
    "futex",
    "getcwd",
    "getdents64",
    "getegid",
    "geteuid",
    "getgid",
    "getpid",
    "getppid",
    "getrandom",
    "gettid",
    "getuid",
    "ioctl",
    "lseek",
    "madvise",
    "mmap",
    "mprotect",
    "mremap",
    "munmap",
    "nanosleep",
    "newfstatat",
    "open",
    "openat",
    "pipe2",
    "prctl",
    "pread64",
    "prlimit64",
    "read",
    "readlink",
    "readlinkat",
    "rseq",
    "rt_sigaction",
    "rt_sigprocmask",
    "rt_sigreturn",
    "sched_getaffinity",
    "sched_yield",
    "set_robust_list",
    "set_tid_address",
    "setgid",
    "setgroups",
    "setresgid",
    "setresuid",
    "setuid",
    "sigaltstack",
    "statfs",
    "statx",
    "tgkill",
    "uname",
    "wait4",
    "write",
)
DANGEROUS_CAPS = frozenset(
    {
        "SYS_ADMIN",
        "SYS_PTRACE",
        "SYS_MODULE",
        "SYS_RAWIO",
        "SYS_BOOT",
        "SYS_TIME",
        "NET_ADMIN",
        "DAC_READ_SEARCH",
        "SETFCAP",
        "MAC_ADMIN",
        "MAC_OVERRIDE",
        "BPF",
        "PERFMON",
        "LINUX_IMMUTABLE",
    }
)
SENSITIVE_SOURCES = (
    "/var/run/docker.sock",
    "/run/docker.sock",
    "/run/containerd/containerd.sock",
    "/run/crio/crio.sock",
    "/var/run/crio/crio.sock",
    "/",
    "/proc",
    "/sys",
    "/dev",
    "/etc",
    "/boot",
    "/root",
    "/home",
    "/usr",
    "/bin",
    "/sbin",
    "/lib",
    "/lib64",
    "/var/lib/docker",
    "/var/lib/containerd",
    "/var/lib/kubelet",
    "/var/run",
    "/run",
)
RESTRICTED_VOLUME_TYPES = frozenset({"configMap", "csi", "downwardAPI", "emptyDir", "ephemeral", "persistentVolumeClaim", "projected", "secret"})
SAFE_SYSCTLS = frozenset(
    {
        "kernel.shm_rmid_forced",
        "net.ipv4.ip_local_port_range",
        "net.ipv4.ip_unprivileged_port_start",
        "net.ipv4.tcp_syncookies",
        "net.ipv4.ping_group_range",
        "net.ipv4.ip_local_reserved_ports",
        "net.ipv4.tcp_keepalive_time",
        "net.ipv4.tcp_fin_timeout",
        "net.ipv4.tcp_keepalive_intvl",
        "net.ipv4.tcp_keepalive_probes",
        "net.ipv4.tcp_rmem",
        "net.ipv4.tcp_wmem",
    }
)
RESTRICTED_POD_CONTEXT = {"runAsNonRoot": True, "runAsUser": 10001, "runAsGroup": 10001, "seccompProfile": {"type": "RuntimeDefault"}}
RESTRICTED_CONTAINER_CONTEXT = {"allowPrivilegeEscalation": False, "readOnlyRootFilesystem": True, "capabilities": {"drop": ["ALL"]}}
TEMPLATE_PATHS = {
    "Pod": (),
    "Deployment": ("template",),
    "StatefulSet": ("template",),
    "DaemonSet": ("template",),
    "ReplicaSet": ("template",),
    "Job": ("template",),
    "CronJob": ("jobTemplate", "spec", "template"),
}
TRACE_LINE = re.compile(r"^(?:\[pid\s+\d+\]\s*)?(?:\d+\s+)?(?:\d{2}:\d{2}:\d{2}(?:\.\d+)?\s+)?(?:\d+\.\d+\s+)?([a-z_][a-z0-9_]*)\(")
RESUMED = re.compile(r"<\.\.\.\s+([a-z_][a-z0-9_]*)\s+resumed>")
LINUX_ERRNO = {1: "EPERM", 2: "ENOENT", 13: "EACCES", 22: "EINVAL", 38: "ENOSYS", 95: "EOPNOTSUPP"}
Runner = Callable[[Sequence[str]], Tuple[int, str, str]]


class HardenError(Exception):
    pass


@dataclass
class Rule:
    names: List[str]
    action: str
    args: List[dict]
    includes: dict
    excludes: dict
    index: int


@dataclass
class Profile:
    default_action: str
    default_errno: int
    arches: List[str]
    rules: List[Rule]
    source: str


@dataclass
class Decision:
    syscall: str
    action: str
    conditional: bool
    rule: Optional[int]
    reason: str
    unlock: str = ""


@dataclass
class Finding:
    check: str
    level: str
    status: str
    detail: str
    fix: str = ""
    flag: str = ""
    weight: int = 0


def errno_name(value: int) -> str:
    return LINUX_ERRNO.get(value, f"errno {value}")


def read_json(target: str, what: str = "input") -> object:
    try:
        if target == "-":
            text = sys.stdin.read()
        else:
            with open(target, encoding="utf-8") as handle:
                text = handle.read()
    except OSError as exc:
        raise HardenError(f"cannot read {what} {target}: {exc.strerror}") from exc
    try:
        return json.loads(text)
    except ValueError as exc:
        hint = "; YAML is not JSON: convert with `kubectl create --dry-run=client --validate=false -o json -f FILE`" if text.lstrip().startswith(("apiVersion:", "kind:", "---")) else ""
        raise HardenError(f"{target} is not valid JSON ({exc}){hint}") from exc


def parse_profile(data: object, source: str = "profile") -> Profile:
    if not isinstance(data, dict) or "defaultAction" not in data:
        raise HardenError(f"{source}: not a seccomp profile (no defaultAction)")
    arches = [str(a) for a in data.get("architectures") or []]
    for entry in data.get("archMap") or []:
        arches.append(str(entry.get("architecture", "")))
        arches.extend(str(a) for a in entry.get("subArchitectures") or [])
    rules: List[Rule] = []
    for index, raw in enumerate(data.get("syscalls") or []):
        names = raw.get("names") or ([raw["name"]] if raw.get("name") else [])
        rules.append(Rule([str(n) for n in names], str(raw.get("action", ALLOW)), list(raw.get("args") or []), dict(raw.get("includes") or {}), dict(raw.get("excludes") or {}), index))
    return Profile(str(data["defaultAction"]), int(data.get("defaultErrnoRet", 1)), [a for a in arches if a], rules, source)


def load_profile(path: str) -> Profile:
    return parse_profile(read_json(path, "profile"), path)


def parse_caps(text: str) -> Set[str]:
    caps: Set[str] = set()
    for part in re.split(r"[,\s]+", text.strip()):
        if not part:
            continue
        lowered = part.lower()
        if lowered == "docker-default":
            caps |= DOCKER_DEFAULT_CAPS
        elif lowered == "none":
            continue
        else:
            name = part.upper()
            caps.add(name if name.startswith("CAP_") else "CAP_" + name)
    return caps


def kernel_at_least(version: Optional[str], minimum: str) -> bool:
    if not version:
        return True

    def parts(text: str) -> List[int]:
        numbers = [int(p) for p in re.findall(r"\d+", text)[:2]]
        return numbers + [0] * (2 - len(numbers))

    return parts(version) >= parts(minimum)


def rule_applies(rule: Rule, caps: Set[str], arch: str, kernel: Optional[str] = None) -> bool:
    excludes, includes = rule.excludes, rule.includes
    if arch in (excludes.get("arches") or []):
        return False
    if any(cap in caps for cap in excludes.get("caps") or []):
        return False
    if excludes.get("minKernel") and kernel_at_least(kernel, str(excludes["minKernel"])):
        return False
    if includes.get("arches") and arch not in includes["arches"]:
        return False
    if any(cap not in caps for cap in includes.get("caps") or []):
        return False
    if includes.get("minKernel") and not kernel_at_least(kernel, str(includes["minKernel"])):
        return False
    return True


def describe_rule(rule: Rule) -> str:
    bits = [f"rule {rule.index}"]
    if rule.includes.get("caps"):
        bits.append("needs " + " and ".join(rule.includes["caps"]))
    if rule.includes.get("minKernel"):
        bits.append(f"kernel >= {rule.includes['minKernel']}")
    if rule.excludes.get("caps"):
        bits.append("unless " + " or ".join(rule.excludes["caps"]))
    if rule.args:
        bits.append("only when " + ", ".join(describe_arg(a) for a in rule.args))
    return ", ".join(bits)


def describe_arg(arg: dict) -> str:
    op = str(arg.get("op", "?")).replace("SCMP_CMP_", "")
    if op == "MASKED_EQ":
        return f"arg{arg.get('index')} & {hex(int(arg.get('value', 0)))} == {arg.get('valueTwo', 0)}"
    return f"arg{arg.get('index')} {op.lower()} {arg.get('value')}"


def resolve(profile: Profile, name: str, caps: Set[str], arch: str = "amd64", kernel: Optional[str] = None) -> Decision:
    conditional: Optional[Rule] = None
    locked: List[str] = []
    for rule in profile.rules:
        if name not in rule.names:
            continue
        if not rule_applies(rule, caps, arch, kernel):
            missing = [cap for cap in rule.includes.get("caps") or [] if cap not in caps]
            if missing and rule.action == ALLOW:
                locked.append(" and ".join(missing))
            continue
        if rule.args:
            if conditional is None:
                conditional = rule
            continue
        return Decision(name, rule.action, False, rule.index, describe_rule(rule))
    unlock = f"allowed with {' or '.join(locked)}" if locked else ""
    if conditional is not None:
        return Decision(name, conditional.action, True, conditional.index, describe_rule(conditional), unlock)
    return Decision(name, profile.default_action, False, None, "default action", unlock)


def listed_syscalls(profile: Profile) -> List[str]:
    return sorted({name for rule in profile.rules for name in rule.names})


def classify(profile: Profile, caps: Set[str], arch: str = "amd64", kernel: Optional[str] = None) -> Dict[str, List[str]]:
    groups: Dict[str, List[str]] = {"allowed": [], "conditional": [], "denied": [], "default": []}
    for name in listed_syscalls(profile):
        decision = resolve(profile, name, caps, arch, kernel)
        if decision.rule is None:
            groups["default"].append(name)
        elif decision.action != ALLOW:
            groups["denied"].append(name)
        elif decision.conditional:
            groups["conditional"].append(name)
        else:
            groups["allowed"].append(name)
    return groups


def allowed_names(profile: Profile, caps: Set[str], arch: str = "amd64", kernel: Optional[str] = None) -> Set[str]:
    groups = classify(profile, caps, arch, kernel)
    allowed = set(groups["allowed"]) | set(groups["conditional"])
    if profile.default_action == ALLOW:
        allowed |= set(groups["default"])
    return allowed


def diff_profiles(a: Profile, b: Profile, caps: Set[str], arch: str = "amd64", kernel: Optional[str] = None) -> dict:
    allowed_a, allowed_b = allowed_names(a, caps, arch, kernel), allowed_names(b, caps, arch, kernel)
    return {
        "a": a.source,
        "b": b.source,
        "default_a": a.default_action,
        "default_b": b.default_action,
        "only_a": sorted(allowed_a - allowed_b),
        "only_b": sorted(allowed_b - allowed_a),
        "both": len(allowed_a & allowed_b),
    }


def syscalls_in_trace(text: str) -> Dict[str, int]:
    counts: Counter = Counter()
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        resumed = RESUMED.search(line)
        if resumed:
            counts.setdefault(resumed.group(1), 0)
            continue
        match = TRACE_LINE.match(line)
        if match and not match.group(1).startswith("syscall_"):
            counts[match.group(1)] += 1
    return dict(counts)


def generate_profile(traced: Sequence[str], errno_value: int = 1) -> Tuple[dict, List[str]]:
    names = set(traced)
    added = sorted(set(ALWAYS_NEEDED) - names)
    profile = {"defaultAction": ERRNO, "defaultErrnoRet": errno_value, "syscalls": [{"names": sorted(names | set(ALWAYS_NEEDED)), "action": ALLOW}]}
    return profile, added


def normalize_cap(name: str) -> str:
    upper = str(name).upper()
    return upper[4:] if upper.startswith("CAP_") else upper


def security_option(options: Sequence[str], key: str) -> Optional[str]:
    for option in options:
        if option == key:
            return ""
        for separator in ("=", ":"):
            if option.startswith(key + separator):
                return option[len(key) + 1 :]
    return None


def is_sensitive(source: str) -> bool:
    path = source.rstrip("/") or "/"
    for prefix in SENSITIVE_SOURCES:
        if path == prefix or (prefix != "/" and path.startswith(prefix + "/")):
            return True
    return path == "/"


def inspect_findings(container: dict) -> List[Finding]:
    config = container.get("Config") or {}
    host = container.get("HostConfig") or {}
    options = [str(o) for o in host.get("SecurityOpt") or []]
    privileged = bool(host.get("Privileged"))
    out: List[Finding] = []

    user = str(config.get("User") or "")
    as_root = user == "" or user.split(":")[0] in ("0", "root")
    out.append(Finding("user", "high", "fail" if as_root else "pass", "runs as root (Config.User is empty)" if user == "" else f"runs as {user}", "run as a fixed non-root uid; fix file ownership in the image, not with chmod 777" if as_root else "", "--user 1000" if as_root else "", 10))

    out.append(Finding("privileged", "high", "fail" if privileged else "pass", "privileged: all 41 capabilities, every host device, no seccomp, no AppArmor" if privileged else "not privileged", "remove --privileged; add the one capability or --device the workload proved it needs" if privileged else "", "", 15))

    cap_add = [normalize_cap(c) for c in host.get("CapAdd") or []]
    cap_drop = [normalize_cap(c) for c in host.get("CapDrop") or []]
    dangerous = sorted(c for c in cap_add if c in DANGEROUS_CAPS)
    if privileged:
        caps = Finding("capabilities", "high", "fail", "privileged grants every capability", "see privileged", "", 10)
    elif dangerous:
        caps = Finding("capabilities", "high", "fail", f"adds root-equivalent capabilities: {', '.join(dangerous)}", "redesign so the workload does not need them; CAP_SYS_ADMIN and friends are root by another name", "--cap-drop ALL", 10)
    elif "ALL" not in cap_drop:
        caps = Finding("capabilities", "high", "fail", "keeps the 14 default capabilities" + (f" and adds {', '.join(cap_add)}" if cap_add else ""), "drop everything, then add back only what an EPERM proved necessary", "--cap-drop ALL", 10)
    else:
        caps = Finding("capabilities", "high", "pass", "drops ALL" + (f", adds {', '.join(cap_add)}" if cap_add else ""), "", "", 10)
    out.append(caps)

    nnp = security_option(options, "no-new-privileges")
    has_nnp = nnp is not None and nnp.lower() not in ("false", "0")
    out.append(Finding("no-new-privileges", "medium", "pass" if has_nnp else "fail", "no_new_privs set: setuid binaries and file capabilities cannot raise privilege" if has_nnp else "execve can still gain privilege through setuid binaries and file capabilities", "" if has_nnp else "set no_new_privs", "" if has_nnp else "--security-opt no-new-privileges", 8))

    readonly = bool(host.get("ReadonlyRootfs"))
    out.append(Finding("read-only-rootfs", "medium", "pass" if readonly else "fail", "root filesystem is read-only" if readonly else "root filesystem is writable: a shell can drop binaries and edit configuration", "" if readonly else "mount the root read-only and give the application a tmpfs or volume for what it writes", "" if readonly else "--read-only --tmpfs /tmp", 8))

    seccomp = security_option(options, "seccomp")
    if privileged:
        out.append(Finding("seccomp", "high", "fail", "privileged disables the seccomp filter", "see privileged", "", 10))
    elif seccomp is not None and seccomp.strip().lower() == "unconfined":
        out.append(Finding("seccomp", "high", "fail", "seccomp=unconfined: every syscall reaches the kernel", "keep the runtime default profile, or pass a profile generated from a trace of the workload", "--security-opt seccomp=profile.json", 10))
    elif seccomp and seccomp.lstrip().startswith("{"):
        try:
            profile = parse_profile(json.loads(seccomp), "inline")
            out.append(Finding("seccomp", "high", "pass", f"custom profile: default {profile.default_action}, {len(listed_syscalls(profile))} syscalls in {len(profile.rules)} rules", "", "", 10))
        except (ValueError, HardenError):
            out.append(Finding("seccomp", "high", "fail", "custom seccomp profile is not valid JSON", "fix the profile; the daemon would refuse to start the container", "", 10))
    else:
        out.append(Finding("seccomp", "high", "pass", "runtime default profile" if not seccomp else f"seccomp={seccomp}", "", "", 10))

    apparmor = str(container.get("AppArmorProfile") or "")
    apparmor_opt = security_option(options, "apparmor")
    label = security_option(options, "label")
    if privileged or apparmor.lower() == "unconfined" or (apparmor_opt or "").lower() == "unconfined":
        out.append(Finding("lsm", "low", "fail", "AppArmor unconfined", "keep the docker-default AppArmor profile or load your own", "", 4))
    elif label and label.lower() == "disable":
        out.append(Finding("lsm", "low", "fail", "SELinux labelling disabled (label=disable)", "remove label=disable; use :z or :Z on the volume that needed it", "", 4))
    elif apparmor:
        out.append(Finding("lsm", "low", "pass", f"AppArmor profile {apparmor}", "", "", 4))
    else:
        out.append(Finding("lsm", "low", "skip", "no AppArmor profile reported (host without AppArmor; SELinux hosts label instead)", "", "", 4))

    shared = [f"{key}={host.get(key)}" for key in ("PidMode", "IpcMode", "NetworkMode", "UTSMode") if str(host.get(key) or "") == "host"]
    out.append(Finding("host-namespaces", "high", "fail" if shared else "pass", "shares host namespaces: " + ", ".join(shared) if shared else "own pid, ipc, network and uts namespaces", "" if not shared else "drop --pid=host, --ipc=host, --network=host; publish ports with -p instead", "", 8))

    sensitive = []
    for mount in container.get("Mounts") or []:
        if str(mount.get("Type") or "bind") == "bind" and is_sensitive(str(mount.get("Source") or "")):
            sensitive.append(f"{mount.get('Source')} ({'rw' if mount.get('RW', True) else 'ro'})")
    out.append(Finding("sensitive-mounts", "high", "fail" if sensitive else "pass", "mounts " + ", ".join(sensitive) if sensitive else "no sensitive host paths mounted", "" if not sensitive else "remove the mount; for the Docker API use a socket proxy with an allow-list; mount only the sub-path you need, read-only", "", 12))

    devices = [str(d.get("PathOnHost") or "") for d in host.get("Devices") or []]
    out.append(Finding("devices", "low", "fail" if devices else "pass", "host devices passed: " + ", ".join(devices) if devices else "no host devices", "" if not devices else "remove --device unless the workload drives that hardware", "", 4))

    pids = host.get("PidsLimit") or 0
    out.append(Finding("pids-limit", "low", "pass" if int(pids) > 0 else "fail", f"pids limit {pids}" if int(pids) > 0 else "no pid limit: a fork bomb takes the node's pid space", "" if int(pids) > 0 else "cap the number of processes", "" if int(pids) > 0 else "--pids-limit 100", 5))

    memory = int(host.get("Memory") or 0)
    out.append(Finding("memory-limit", "low", "pass" if memory > 0 else "fail", f"memory limit {memory // (1024 * 1024)} MiB" if memory > 0 else "no memory limit: one leak evicts its neighbours", "" if memory > 0 else "cap memory so the OOM killer picks this container, not the node", "" if memory > 0 else "--memory 256m", 5))

    public = []
    for port, bindings in (host.get("PortBindings") or {}).items():
        for binding in bindings or []:
            ip = str(binding.get("HostIp") or "")
            if ip in ("", "0.0.0.0", "::"):
                public.append(f"{ip or '0.0.0.0'}:{binding.get('HostPort')}->{port}")
    out.append(Finding("ports", "low", "fail" if public else "pass", "published on every interface: " + ", ".join(public) if public else "no ports published on all interfaces", "" if not public else "bind to 127.0.0.1 or a private interface unless the port must be reachable from outside", "" if not public else "-p 127.0.0.1:8080:80", 3))
    return out


def score(findings: Sequence[Finding]) -> Optional[int]:
    scored = [f for f in findings if f.status != "skip"]
    total = sum(f.weight for f in scored)
    if total == 0:
        return None
    return round(100 * sum(f.weight for f in scored if f.status == "pass") / total)


def render_findings(title: str, findings: Sequence[Finding]) -> List[str]:
    passed = sum(1 for f in findings if f.status == "pass")
    scored = sum(1 for f in findings if f.status != "skip")
    value = score(findings)
    lines = [f"{title}: score {value if value is not None else '?'}/100, {passed} of {scored} checks pass"]
    for f in findings:
        lines.append(f"  {f.status.upper():<4} {f.level:<10} {f.check:<26} {f.detail}")
        if f.status == "fail" and (f.fix or f.flag):
            fix = f"{f.flag}  ({f.fix})" if f.flag and f.fix else f.flag or f.fix
            lines.append(f"       {'':<10} {'':<26} fix: {fix}")
    return lines


def container_title(container: dict) -> str:
    name = str(container.get("Name") or container.get("Id") or "container").lstrip("/")
    image = str((container.get("Config") or {}).get("Image") or "")
    return f"container {name}" + (f" ({image})" if image else "")


def hardened_flags(findings: Sequence[Finding]) -> List[str]:
    return [f.flag for f in findings if f.status == "fail" and f.flag]


def pod_templates(document: object) -> List[Tuple[str, str, str, dict]]:
    if isinstance(document, list):
        found: List[Tuple[str, str, str, dict]] = []
        for item in document:
            found.extend(pod_templates(item))
        return found
    if not isinstance(document, dict):
        raise HardenError("manifest is not a Kubernetes object")
    kind = str(document.get("kind") or "")
    if kind == "List":
        return pod_templates(document.get("items") or [])
    if kind not in TEMPLATE_PATHS:
        raise HardenError(f"kind {kind or '?'} has no pod template (supported: {', '.join(TEMPLATE_PATHS)})")
    metadata = document.get("metadata") or {}
    node = document.get("spec") or {}
    for key in TEMPLATE_PATHS[kind]:
        node = node.get(key) or {}
    spec = node if kind == "Pod" else node.get("spec") or {}
    return [(kind, str(metadata.get("name") or "?"), str(metadata.get("namespace") or ""), spec)]


def k8s_findings(spec: dict) -> List[Finding]:
    pod_context = spec.get("securityContext") or {}
    containers = [(c, "container") for c in spec.get("containers") or []] + [(c, "initContainer") for c in spec.get("initContainers") or []] + [(c, "ephemeralContainer") for c in spec.get("ephemeralContainers") or []]
    out: List[Finding] = []

    def check(name: str, level: str, failures: List[str], ok: str, fix: str, weight: int) -> None:
        out.append(Finding(name, level, "fail" if failures else "pass", "; ".join(failures) if failures else ok, fix if failures else "", "", weight))

    def context(container: dict) -> dict:
        return container.get("securityContext") or {}

    check("privileged", "baseline", [f'{kind} "{c.get("name")}" must not set securityContext.privileged=true' for c, kind in containers if context(c).get("privileged")], "no privileged containers", "remove privileged: true; add a capability or a device instead", 15)
    shared = [f"{key}=true" for key in ("hostNetwork", "hostPID", "hostIPC") if spec.get(key)]
    check("host-namespaces", "baseline", shared, "own network, pid and ipc namespaces", "remove hostNetwork, hostPID and hostIPC", 10)

    host_paths, other_types = [], []
    for volume in spec.get("volumes") or []:
        types = [k for k in volume if k != "name"]
        for kind in types:
            if kind == "hostPath":
                host_paths.append(f'volume "{volume.get("name")}" uses hostPath {(volume.get("hostPath") or {}).get("path")}')
            elif kind not in RESTRICTED_VOLUME_TYPES:
                other_types.append(f'volume "{volume.get("name")}" uses restricted volume type "{kind}"')
    check("hostPath-volumes", "baseline", host_paths, "no hostPath volumes", "replace hostPath with a PVC, configMap or emptyDir; a hostPath of / or the container socket is the node", 12)
    check("volume-types", "restricted", other_types, "only configMap, csi, downwardAPI, emptyDir, ephemeral, PVC, projected and secret volumes", "use a PVC through a CSI driver instead of an in-tree volume type", 5)

    host_ports = [f'{kind} "{c.get("name")}" uses hostPort {p.get("hostPort")}' for c, kind in containers for p in c.get("ports") or [] if p.get("hostPort")]
    check("hostPort", "baseline", host_ports, "no hostPort", "expose the pod through a Service instead of hostPort", 5)
    check("procMount", "baseline", [f'{kind} "{c.get("name")}" must not set securityContext.procMount={context(c).get("procMount")}' for c, kind in containers if context(c).get("procMount") not in (None, "Default")], "default /proc masks kept", "remove procMount: Unmasked", 5)
    check("sysctls", "baseline", [f"forbidden sysctl {s.get('name')}" for s in pod_context.get("sysctls") or [] if s.get("name") not in SAFE_SYSCTLS], "only safe sysctls", "remove unsafe sysctls or set them on the node", 5)

    check("allowPrivilegeEscalation", "restricted", [f'{kind} "{c.get("name")}" must set securityContext.allowPrivilegeEscalation=false' for c, kind in containers if context(c).get("allowPrivilegeEscalation") is not False], "allowPrivilegeEscalation=false everywhere", "set securityContext.allowPrivilegeEscalation: false on every container", 10)

    cap_failures = []
    for c, kind in containers:
        capabilities = context(c).get("capabilities") or {}
        drops = {normalize_cap(x) for x in capabilities.get("drop") or []}
        if "ALL" not in drops:
            cap_failures.append(f'{kind} "{c.get("name")}" must set securityContext.capabilities.drop=["ALL"]')
        for added in capabilities.get("add") or []:
            if normalize_cap(added) != "NET_BIND_SERVICE":
                cap_failures.append(f'{kind} "{c.get("name")}" must not include "{added}" in securityContext.capabilities.add')
    check("capabilities", "restricted", cap_failures, "drop ALL, adds at most NET_BIND_SERVICE", "drop: [ALL]; the only add restricted permits is NET_BIND_SERVICE", 10)

    non_root = [f'pod or {kind} "{c.get("name")}" must set securityContext.runAsNonRoot=true' for c, kind in containers if context(c).get("runAsNonRoot", pod_context.get("runAsNonRoot")) is not True]
    check("runAsNonRoot", "restricted", non_root, "runAsNonRoot=true", "set runAsNonRoot: true at pod level (the image must then declare a numeric USER or set runAsUser)", 10)
    uid_failures = ["pod must not set runAsUser=0"] if pod_context.get("runAsUser") == 0 else []
    uid_failures += [f'{kind} "{c.get("name")}" must not set runAsUser=0' for c, kind in containers if context(c).get("runAsUser") == 0]
    check("runAsUser", "restricted", uid_failures, "no runAsUser=0", "use a non-zero uid", 5)

    seccomp_failures = []
    pod_seccomp = (pod_context.get("seccompProfile") or {}).get("type")
    for c, kind in containers:
        effective = (context(c).get("seccompProfile") or {}).get("type") or pod_seccomp
        if effective not in ("RuntimeDefault", "Localhost"):
            seccomp_failures.append(f'pod or {kind} "{c.get("name")}" must set securityContext.seccompProfile.type to "RuntimeDefault" or "Localhost"' + (" (is Unconfined)" if effective == "Unconfined" else ""))
    check("seccompProfile", "restricted", seccomp_failures, "RuntimeDefault or Localhost seccomp on every container", "set securityContext.seccompProfile.type: RuntimeDefault at pod level", 10)

    check("readOnlyRootFilesystem", "extra", [f'{kind} "{c.get("name")}": set securityContext.readOnlyRootFilesystem=true and mount an emptyDir where it writes' for c, kind in containers if kind == "container" and context(c).get("readOnlyRootFilesystem") is not True], "read-only root filesystem", "not part of the restricted standard, but the difference between a shell and a foothold", 5)
    check("memory-limit", "extra", [f'{kind} "{c.get("name")}": set resources.limits.memory' for c, kind in containers if kind == "container" and not ((c.get("resources") or {}).get("limits") or {}).get("memory")], "memory limits set", "a limit makes the OOM killer pick this pod, not the node", 5)
    check("serviceAccountToken", "extra", [] if spec.get("automountServiceAccountToken") is False else ["automountServiceAccountToken is not false: a shell in the pod holds a cluster credential"], "no service account token mounted", "set automountServiceAccountToken: false unless the pod talks to the API server", 3)
    return out


def rejected(findings: Sequence[Finding]) -> List[Finding]:
    return [f for f in findings if f.status == "fail" and f.level in ("baseline", "restricted")]


def k8s_title(kind: str, name: str, namespace: str, findings: Sequence[Finding]) -> str:
    violations = rejected(findings)
    where = f" in namespace {namespace}" if namespace else ""
    verdict = f"REJECTED by pod-security.kubernetes.io/enforce=restricted ({len(violations)} violation{'s' if len(violations) != 1 else ''})" if violations else "admitted under pod-security.kubernetes.io/enforce=restricted"
    return f"{kind} {name}{where}: {verdict}"


def k8s_patch(kind: str, spec: dict) -> dict:
    pod_spec = {"securityContext": dict(RESTRICTED_POD_CONTEXT), "automountServiceAccountToken": False, "containers": [{"name": c.get("name"), "securityContext": json.loads(json.dumps(RESTRICTED_CONTAINER_CONTEXT))} for c in spec.get("containers") or []]}
    if spec.get("initContainers"):
        pod_spec["initContainers"] = [{"name": c.get("name"), "securityContext": {"allowPrivilegeEscalation": False, "capabilities": {"drop": ["ALL"]}}} for c in spec["initContainers"]]
    patch: dict = pod_spec
    path = ("spec",) + TEMPLATE_PATHS[kind] + (("spec",) if kind != "Pod" else ())
    for key in reversed(path):
        patch = {key: patch}
    return patch


def run_command(argv: Sequence[str]) -> Tuple[int, str, str]:
    if shutil.which(argv[0]) is None:
        return 127, "", f"{argv[0]} is not installed"
    completed = subprocess.run(list(argv), capture_output=True, text=True)
    return completed.returncode, completed.stdout, completed.stderr


def load_containers(target: str, runner: Runner) -> List[dict]:
    if target == "-" or os.path.exists(target):
        data = read_json(target, "docker inspect output")
    else:
        code, stdout, stderr = runner(["docker", "inspect", target])
        if code != 0:
            raise HardenError(f"docker inspect {target}: {stderr.strip() or 'failed'} (pass a file saved with `docker inspect NAME > c.json`)")
        try:
            data = json.loads(stdout)
        except ValueError as exc:
            raise HardenError(f"docker inspect {target} did not return JSON") from exc
    containers = data if isinstance(data, list) else [data]
    if not containers or not all(isinstance(c, dict) and "HostConfig" in c for c in containers):
        raise HardenError(f"{target}: not docker inspect output (no HostConfig)")
    return containers


def cmd_seccomp(args: argparse.Namespace, runner: Runner) -> int:
    profile = load_profile(args.profile)
    caps = parse_caps(args.caps)
    groups = classify(profile, caps, args.arch, args.kernel)
    checks = [resolve(profile, name, caps, args.arch, args.kernel) for name in re.split(r"[,\s]+", args.check or "") if name]
    notable = {name: resolve(profile, name, caps, args.arch, args.kernel) for name in NOTABLE}
    if args.json:
        print(json.dumps({"profile": profile.source, "default_action": profile.default_action, "default_errno": profile.default_errno, "arches": profile.arches, "rules": len(profile.rules), "caps": sorted(caps), "allowed": groups["allowed"], "conditional": groups["conditional"], "denied": groups["denied"], "notable": {k: asdict(v) for k, v in notable.items()}, "checks": [asdict(d) for d in checks]}, indent=2))
        return 0
    default = profile.default_action + (f" ({errno_name(profile.default_errno)})" if profile.default_action == ERRNO else "")
    print(f"profile {profile.source}: default {default}, {len(profile.arches)} architecture(s), {len(profile.rules)} rules")
    print(f"capabilities assumed: {', '.join(sorted(c[4:] for c in caps)) or 'none'}")
    print(f"  allowed       {len(groups['allowed']):>4}")
    print(f"  conditional   {len(groups['conditional']):>4}  {' '.join(groups['conditional'])}".rstrip())
    print(f"  denied        {len(groups['denied']):>4}  {' '.join(groups['denied'])}".rstrip())
    print(f"  anything else  ->  {profile.default_action}")
    print("notable syscalls:")
    for name, decision in notable.items():
        action = decision.action.replace("SCMP_ACT_", "") + ("*" if decision.conditional else "")
        extra = f" ({decision.unlock})" if decision.unlock and decision.action != ALLOW else ""
        print(f"  {name:<20} {action:<8} {NOTABLE[name]}{extra}")
    if checks:
        print("checks:")
        for decision in checks:
            action = decision.action.replace("SCMP_ACT_", "") + ("*" if decision.conditional else "")
            unlock = f"; {decision.unlock}" if decision.unlock and decision.action != ALLOW else ""
            print(f"  {decision.syscall:<20} {action:<8} {decision.reason}{unlock}")
    if groups["conditional"] or any(d.conditional for d in checks):
        print("* allowed only when the argument filter matches")
    return 0


def cmd_seccomp_diff(args: argparse.Namespace, runner: Runner) -> int:
    caps = parse_caps(args.caps)
    result = diff_profiles(load_profile(args.a), load_profile(args.b), caps, args.arch, args.kernel)
    if args.json:
        print(json.dumps(result, indent=2))
        return 0
    print(f"a: {result['a']} (default {result['default_a']})")
    print(f"b: {result['b']} (default {result['default_b']})")
    print(f"allowed in both: {result['both']}")
    print(f"allowed only in a ({len(result['only_a'])}): {' '.join(result['only_a']) or '-'}")
    print(f"allowed only in b ({len(result['only_b'])}): {' '.join(result['only_b']) or '-'}")
    return 0


def cmd_generate(args: argparse.Namespace, runner: Runner) -> int:
    try:
        with open(args.trace, encoding="utf-8", errors="replace") as handle:
            text = handle.read()
    except OSError as exc:
        raise HardenError(f"cannot read trace {args.trace}: {exc.strerror}") from exc
    counts = syscalls_in_trace(text)
    if not counts:
        raise HardenError(f"{args.trace}: no syscall lines found; expected strace output such as `strace -f -qq -o trace.log CMD`")
    profile, added = generate_profile(sorted(counts), args.errno)
    summary = f"trace {args.trace}: {sum(counts.values())} syscall lines, {len(counts)} distinct syscalls\nprofile: {len(counts)} from the trace + {len(added)} always needed = {len(profile['syscalls'][0]['names'])} allowed, default {ERRNO} ({errno_name(args.errno)})"
    if args.json:
        print(json.dumps({"from_trace": sorted(counts), "added": added, "profile": profile}, indent=2))
        return 0
    if args.out:
        with open(args.out, "w", encoding="utf-8") as handle:
            json.dump(profile, handle, indent=2)
            handle.write("\n")
        print(summary)
        print(f"wrote {args.out}; run: docker run --rm --security-opt seccomp={args.out} IMAGE CMD")
        return 0
    print(summary, file=sys.stderr)
    print(json.dumps(profile, indent=2))
    return 0


def cmd_inspect(args: argparse.Namespace, runner: Runner) -> int:
    containers = load_containers(args.target, runner)
    reports = [(container, inspect_findings(container)) for container in containers]
    if args.json:
        print(json.dumps([{"name": str(c.get("Name") or "").lstrip("/"), "image": (c.get("Config") or {}).get("Image"), "score": score(f), "flags": hardened_flags(f), "findings": [asdict(x) for x in f]} for c, f in reports], indent=2))
    else:
        for index, (container, findings) in enumerate(reports):
            if index:
                print()
            print("\n".join(render_findings(container_title(container), findings)))
            flags = hardened_flags(findings)
            if flags:
                print(f"  hardened run: docker run {' '.join(flags)} {(container.get('Config') or {}).get('Image') or 'IMAGE'}")
    lowest = min((score(f) or 0) for _, f in reports)
    return 1 if args.fail_under is not None and lowest < args.fail_under else 0


def cmd_k8s(args: argparse.Namespace, runner: Runner) -> int:
    templates = pod_templates(read_json(args.target, "manifest"))
    reports = [(kind, name, namespace, spec, k8s_findings(spec)) for kind, name, namespace, spec in templates]
    if args.fix:
        patches = [k8s_patch(kind, spec) for kind, _, _, spec, _ in reports]
        print(json.dumps(patches[0] if len(patches) == 1 else patches, indent=2))
    elif args.json:
        print(json.dumps([{"kind": k, "name": n, "namespace": ns, "score": score(f), "rejected": bool(rejected(f)), "violations": [x.detail for x in rejected(f)], "findings": [asdict(x) for x in f]} for k, n, ns, _, f in reports], indent=2))
    else:
        for index, (kind, name, namespace, _, findings) in enumerate(reports):
            if index:
                print()
            print("\n".join(render_findings(k8s_title(kind, name, namespace, findings), findings)))
    lowest = min((score(f) or 0) for *_, f in reports)
    return 1 if args.fail_under is not None and lowest < args.fail_under else 0


COMMANDS = {"seccomp": cmd_seccomp, "seccomp-diff": cmd_seccomp_diff, "generate": cmd_generate, "inspect": cmd_inspect, "k8s": cmd_k8s}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="hardencheck", description=__doc__)
    sub = parser.add_subparsers(dest="command", metavar="COMMAND")

    def common(p: argparse.ArgumentParser) -> None:
        p.add_argument("--caps", default="docker-default", help="capabilities the container holds: docker-default, none, or names (default: docker-default)")
        p.add_argument("--arch", default="amd64", help="architecture for includes/excludes.arches (default: amd64)")
        p.add_argument("--kernel", help="kernel version for minKernel rules (default: assume current)")
        p.add_argument("--json", action="store_true", help="machine-readable output")

    p = sub.add_parser("seccomp", help="read a seccomp profile and resolve what it allows")
    p.add_argument("profile", help="profile JSON (Docker or OCI format)")
    p.add_argument("--check", metavar="SYSCALLS", help="comma-separated syscalls to resolve")
    common(p)
    p = sub.add_parser("seccomp-diff", help="syscalls allowed by one profile and not the other")
    p.add_argument("a")
    p.add_argument("b")
    common(p)
    p = sub.add_parser("generate", help="build an allow-list profile from an strace log")
    p.add_argument("trace", help="output of strace -f -qq -o FILE CMD")
    p.add_argument("--out", help="write the profile here (default: stdout)")
    p.add_argument("--errno", type=int, default=1, help="errno returned for denied syscalls (default: 1 EPERM)")
    p.add_argument("--json", action="store_true", help="print the syscall lists and the profile as one JSON document")
    p = sub.add_parser("inspect", help="score a container from docker inspect output")
    p.add_argument("target", help="docker inspect JSON file, - for stdin, or a container name")
    p.add_argument("--json", action="store_true")
    p.add_argument("--fail-under", type=int, metavar="SCORE", help="exit 1 when the score is lower")
    p = sub.add_parser("k8s", help="score a Pod or workload manifest against the restricted Pod Security Standard")
    p.add_argument("target", help="manifest JSON file (Pod, Deployment, StatefulSet, DaemonSet, Job, CronJob, List) or - for stdin")
    p.add_argument("--json", action="store_true")
    p.add_argument("--fix", action="store_true", help="print the strategic merge patch that makes it pass")
    p.add_argument("--fail-under", type=int, metavar="SCORE", help="exit 1 when the score is lower")
    return parser


def main(argv: Optional[Sequence[str]] = None, runner: Optional[Runner] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(list(sys.argv[1:] if argv is None else argv))
    if not args.command:
        parser.print_help(sys.stderr)
        return 2
    try:
        return COMMANDS[args.command](args, runner or run_command)
    except HardenError as exc:
        print(f"hardencheck: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
