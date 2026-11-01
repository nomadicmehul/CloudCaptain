"""tcpwatch: read the kernel's TCP socket table from /proc/net/tcp, /proc/net/tcp6 or `ss -tanpi`, diagnose CLOSE_WAIT leaks, full accept queues, half-open and silent connections, retransmitting peers and ephemeral port exhaustion, walk the TCP state machine, and probe a server with connect, read and total timeouts."""
from __future__ import annotations

import argparse
import errno
import ipaddress
import json
import math
import os
import re
import socket
import sys
import threading
import time
from collections import Counter
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple

PROC_STATES = {
    0x01: "ESTABLISHED",
    0x02: "SYN_SENT",
    0x03: "SYN_RECV",
    0x04: "FIN_WAIT1",
    0x05: "FIN_WAIT2",
    0x06: "TIME_WAIT",
    0x07: "CLOSE",
    0x08: "CLOSE_WAIT",
    0x09: "LAST_ACK",
    0x0A: "LISTEN",
    0x0B: "CLOSING",
    0x0C: "SYN_RECV",
}
SS_STATES = {
    "ESTAB": "ESTABLISHED",
    "SYN-SENT": "SYN_SENT",
    "SYN-RECV": "SYN_RECV",
    "FIN-WAIT-1": "FIN_WAIT1",
    "FIN-WAIT-2": "FIN_WAIT2",
    "TIME-WAIT": "TIME_WAIT",
    "UNCONN": "CLOSE",
    "CLOSE-WAIT": "CLOSE_WAIT",
    "LAST-ACK": "LAST_ACK",
    "LISTEN": "LISTEN",
    "CLOSING": "CLOSING",
}
STATE_ORDER = ("LISTEN", "SYN_SENT", "SYN_RECV", "ESTABLISHED", "FIN_WAIT1", "FIN_WAIT2", "CLOSING", "TIME_WAIT", "CLOSE_WAIT", "LAST_ACK", "CLOSE")
TIMERS = {0: "off", 1: "on", 2: "keepalive", 3: "timewait", 4: "persist"}
USER_HZ = 100
SS_HEADERS = ("State", "Netid", "Recv-Q")
SS_NETIDS = ("tcp", "udp", "raw", "u_str", "u_dgr", "u_seq", "p_raw", "p_dgr", "nl", "mptcp", "sctp", "tipc", "v_str", "v_dgr", "xdp")
SS_TOKEN = re.compile(r"users:\(\(.*?\)\)(?=\s|$)|timer:\([^)]*\)|\S+")
SS_USER = re.compile(r'\("((?:[^"\\]|\\.)*)",pid=(\d+),fd=(\d+)\)')
SS_PAIRS = ("send", "pacing_rate", "delivery_rate")
SS_MS_QUIRK = re.compile(r"(\d+)\.(\d{3})ms")
DURATION = re.compile(r"(\d+(?:\.\d+)?)(ms|sec|min|hr|s|m|h)")
UNIT_SECONDS = {"ms": 0.001, "sec": 1.0, "s": 1.0, "min": 60.0, "m": 60.0, "hr": 3600.0, "h": 3600.0}
SOCKET_LINK = re.compile(r"socket:\[(\d+)\]")
DEFAULT_PORT_RANGE = (32768, 60999)
SENDING = ("ESTABLISHED", "FIN_WAIT1", "CLOSE_WAIT", "CLOSING", "LAST_ACK")
SEVERITY_RANK = {"critical": 0, "warning": 1, "info": 2}
UNREACHABLE = (errno.EHOSTUNREACH, errno.ENETUNREACH)
DARWIN_TCP_KEEPALIVE = 0x10
TCP_RTO_MAX = 120.0
TCP_RTO_MIN = 0.2
TCP_TIMEOUT_INIT = 1.0
TIME_WAIT_SECONDS = 60.0
FIN_TIMEOUT_SECONDS = 60.0
ONE_WAY_DELAY = 0.010
WATCHED_COUNTERS = ("TcpExtListenOverflows", "TcpExtListenDrops", "TcpExtTCPSynRetrans", "TcpExtTCPTimeouts", "TcpExtTCPAbortOnTimeout", "TcpRetransSegs", "TcpActiveOpens", "TcpAttemptFails", "TcpEstabResets", "TcpCurrEstab")

FIX_CLOSE_WAIT = "find the code path that drops the socket without close(): error branches, early returns, a missing with/finally, a pool that never releases; no kernel timer ever clears CLOSE_WAIT, a restart only resets the count"
FIX_QUEUED = "the listener is not calling accept() fast enough; see the accept-queue finding for the same port"
FIX_ACCEPT = "the process is not calling accept() fast enough (a blocked event loop, too few workers, a deadlock); fix the accept rate first, then size the backlog (the listen() argument, capped by net.core.somaxconn)"
FIX_SYN_RECV = "a rising TcpExtListenOverflows means the accept queue was full when the final ACK arrived; otherwise suspect the return path (asymmetric routing, a firewall dropping the client's ACK) or a SYN flood (keep net.ipv4.tcp_syncookies=1)"
FIX_SYN_SENT = "a firewall or security group dropping SYNs, a blackhole route, a host that is down, or a server whose accept queue is full; set a connect timeout so callers fail in seconds instead of waiting for net.ipv4.tcp_syn_retries"
FIX_FIN_WAIT2 = "look at the peer for CLOSE_WAIT (`ss -tanp state close-wait` there); orphaned FIN_WAIT2 sockets on this side are freed after net.ipv4.tcp_fin_timeout"
FIX_RETRANS = "look at the path and the peer (`ss -ti dst HOST` for rtt and retrans, tcpdump); the kernel aborts such a connection only after net.ipv4.tcp_retries2 (about 15 minutes by default), so bound it with TCP_USER_TIMEOUT or an application read timeout"
FIX_ZERO_WINDOW = "look at the reading side: its Recv-Q is full because its application stopped reading; a write or read timeout on this side bounds the wait"
FIX_PORTS = "every connection to one destination needs its own local port: reuse connections (a pool, HTTP keep-alive), spread over more destination or source addresses, widen net.ipv4.ip_local_port_range; TIME_WAIT sits on the side that closed first, and net.ipv4.tcp_tw_reuse helps only outgoing connections"


class InputError(ValueError):
    pass


@dataclass
class Process:
    name: str
    pid: int
    fd: int


@dataclass
class Sock:
    state: str
    local: str
    lport: int
    remote: str
    rport: int
    recv_q: int = 0
    send_q: int = 0
    timer: str = "off"
    expires: float = 0.0
    retransmits: int = 0
    uid: Optional[int] = None
    inode: int = 0
    processes: List[Process] = field(default_factory=list)
    info: Dict[str, str] = field(default_factory=dict)
    source: str = "proc"

    @property
    def peer(self) -> str:
        return endpoint(self.remote, self.rport)

    @property
    def where(self) -> str:
        return endpoint(self.local, self.lport)


@dataclass
class Table:
    socks: List[Sock]
    owners_known: bool = False
    counters: Dict[str, int] = field(default_factory=dict)
    port_range: Tuple[int, int] = DEFAULT_PORT_RANGE
    backlogs: Dict[int, int] = field(default_factory=dict)


@dataclass
class Limits:
    close_wait: int = 5
    syn_recv: int = 16
    port_warning: float = 0.5
    port_critical: float = 0.9


@dataclass
class Finding:
    kind: str
    severity: str
    summary: str
    fix: str
    sockets: int = 0
    port: Optional[int] = None
    peer: str = ""
    owner: str = ""


def format_ip(address: Any) -> str:
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        return f"::ffff:{address.ipv4_mapped}"
    return str(address)


def normalize_host(host: str) -> str:
    if host == "*":
        return "::"
    try:
        return format_ip(ipaddress.ip_address(host))
    except ValueError:
        return host


def endpoint(host: str, port: int) -> str:
    shown = f"[{host}]" if ":" in host else host
    return f"{shown}:{port if port else '*'}"


def decode_address(text: str, byteorder: str = sys.byteorder) -> str:
    try:
        raw = bytes.fromhex(text)
    except ValueError:
        raw = b""
    if len(raw) not in (4, 16):
        raise InputError(f"not a /proc/net/tcp address: {text!r} (expected 8 or 32 hex digits)")
    if byteorder == "little":
        raw = b"".join(raw[i:i + 4][::-1] for i in range(0, len(raw), 4))
    return format_ip(ipaddress.ip_address(raw))


def split_hex_endpoint(text: str, byteorder: str) -> Tuple[str, int]:
    address, _, port = text.partition(":")
    return decode_address(address, byteorder), int(port, 16)


def parse_proc_row(fields: Sequence[str], byteorder: str) -> Sock:
    local, lport = split_hex_endpoint(fields[1], byteorder)
    remote, rport = split_hex_endpoint(fields[2], byteorder)
    tx_queue, rx_queue = (int(x, 16) for x in fields[4].split(":"))
    timer, when = fields[5].split(":")
    return Sock(
        state=PROC_STATES.get(int(fields[3], 16), f"UNKNOWN_{fields[3]}"),
        local=local,
        lport=lport,
        remote=remote,
        rport=rport,
        recv_q=rx_queue,
        send_q=tx_queue,
        timer=TIMERS.get(int(timer, 16), "unknown"),
        expires=int(when, 16) / USER_HZ,
        retransmits=int(fields[6], 16),
        uid=int(fields[7]),
        inode=int(fields[9]),
        source="proc",
    )


def parse_proc_net_tcp(text: str, byteorder: str = sys.byteorder) -> List[Sock]:
    socks = []
    for number, line in enumerate(text.splitlines(), 1):
        fields = line.split()
        if not fields or fields[0] == "sl":
            continue
        if len(fields) < 10 or not fields[0].endswith(":"):
            raise InputError(f"line {number}: not a /proc/net/tcp row: {line.strip()!r}")
        try:
            socks.append(parse_proc_row(fields, byteorder))
        except ValueError as error:
            raise InputError(f"line {number}: {error}") from None
    return socks


def parse_duration(text: str) -> float:
    quirk = SS_MS_QUIRK.fullmatch(text)
    if quirk:
        return int(quirk.group(1)) + int(quirk.group(2)) / 1000
    return round(sum(float(number) * UNIT_SECONDS[unit] for number, unit in DURATION.findall(text)), 3)


def split_endpoint(text: str) -> Tuple[str, int]:
    host, sep, port = text.rpartition(":")
    if not sep or not (port == "*" or port.isdigit()):
        raise InputError(f"not an ss address:port: {text!r}")
    if host.startswith("[") and host.endswith("]"):
        host = host[1:-1]
    host = host.split("%", 1)[0]
    return normalize_host(host), 0 if port == "*" else int(port)


def retransmits_from_info(info: Dict[str, str]) -> int:
    current = info.get("retrans", "").split("/")[0]
    if current.isdigit():
        return int(current)
    backoff = info.get("backoff", "")
    return int(backoff) if backoff.isdigit() else 0


def ss_record(tokens: List[str]) -> Sock:
    if len(tokens) < 5:
        raise InputError(f"ss row has {len(tokens)} fields, expected State Recv-Q Send-Q Local Peer: {' '.join(tokens)!r}")
    local, lport = split_endpoint(tokens[3])
    remote, rport = split_endpoint(tokens[4])
    sock = Sock(SS_STATES[tokens[0]], local, lport, remote, rport, int(tokens[1]), int(tokens[2]), source="ss")
    rest = tokens[5:]
    index = 0
    while index < len(rest):
        token = rest[index]
        if token.startswith("users:("):
            sock.processes = [Process(name, int(pid), int(fd)) for name, pid, fd in SS_USER.findall(token)]
        elif token.startswith("timer:("):
            name, expires, retrans = (token[len("timer:("):-1].split(",") + ["", "", "0"])[:3]
            sock.timer, sock.expires, sock.retransmits = name, parse_duration(expires), int(retrans or 0)
        elif token in SS_PAIRS and index + 1 < len(rest):
            sock.info[token] = rest[index + 1]
            index += 1
        elif token.startswith("ino:"):
            sock.inode = int(token[4:])
        elif token.startswith("uid:"):
            sock.uid = int(token[4:])
        elif ":" in token:
            key, _, value = token.partition(":")
            sock.info[key] = value
        else:
            sock.info[token] = ""
        index += 1
    if sock.retransmits == 0:
        sock.retransmits = retransmits_from_info(sock.info)
    return sock


def parse_ss(text: str) -> List[Sock]:
    records: List[List[str]] = []
    current: Optional[List[str]] = None
    for number, line in enumerate(text.splitlines(), 1):
        tokens = SS_TOKEN.findall(line)
        if not tokens:
            continue
        if line[:1].isspace():
            if current is not None:
                current.extend(tokens)
            continue
        if tokens[0] in SS_HEADERS:
            current = None
            continue
        if tokens[0] in SS_NETIDS:
            if tokens[0] != "tcp":
                current = None
                continue
            tokens = tokens[1:]
        if tokens and tokens[0] in SS_STATES:
            current = list(tokens)
            records.append(current)
        elif current is not None:
            current.extend(tokens)
        else:
            raise InputError(f"line {number}: expected an ss row starting with a state such as ESTAB or LISTEN, got {line.strip()!r}")
    return [ss_record(tokens) for tokens in records]


def parse_proc_counters(text: str) -> Dict[str, int]:
    counters: Dict[str, int] = {}
    rows = [line.split() for line in text.splitlines() if line.strip()]
    for header, values in zip(rows[0::2], rows[1::2]):
        if header[0] != values[0] or not header[0].endswith(":") or len(header) != len(values):
            raise InputError(f"not a /proc/net/netstat or /proc/net/snmp pair of lines: {' '.join(header[:3])} ... / {' '.join(values[:3])} ...")
        prefix = header[0][:-1]
        for name, value in zip(header[1:], values[1:]):
            counters[prefix + name] = int(value)
    return counters


def parse_nstat(text: str) -> Dict[str, int]:
    counters: Dict[str, int] = {}
    for line in text.splitlines():
        fields = line.split()
        if len(fields) >= 2 and not fields[0].startswith("#") and fields[1].lstrip("-").isdigit():
            counters[fields[0]] = int(fields[1])
    return counters


def socket_owners(proc: Path) -> Dict[int, List[Process]]:
    owners: Dict[int, List[Process]] = {}
    try:
        pids = sorted((p for p in proc.iterdir() if p.name.isdigit()), key=lambda p: int(p.name))
    except OSError:
        return owners
    for pid_dir in pids:
        try:
            name = (pid_dir / "comm").read_text().strip()
            fds = sorted((f for f in (pid_dir / "fd").iterdir() if f.name.isdigit()), key=lambda f: int(f.name))
        except OSError:
            continue
        for fd in fds:
            try:
                match = SOCKET_LINK.fullmatch(os.readlink(fd))
            except OSError:
                continue
            if match:
                owners.setdefault(int(match.group(1)), []).append(Process(name, int(pid_dir.name), int(fd.name)))
    return owners


def attach_owners(socks: Iterable[Sock], owners: Dict[int, List[Process]]) -> None:
    for sock in socks:
        if sock.inode and not sock.processes:
            sock.processes = list(owners.get(sock.inode, []))


def parse_port_range(text: str) -> Tuple[int, int]:
    parts = text.replace("-", " ").split()
    if len(parts) != 2 or not all(p.isdigit() for p in parts):
        raise InputError(f"port range must be LOW-HIGH like 32768-60999, got {text!r}")
    low, high = int(parts[0]), int(parts[1])
    if not 1 <= low <= high <= 65535:
        raise InputError(f"port range {low}-{high} must satisfy 1 <= low <= high <= 65535")
    return low, high


def parse_backlogs(items: Sequence[str]) -> Dict[int, int]:
    backlogs = {}
    for item in items:
        port, sep, value = item.partition("=")
        if not sep or not port.isdigit() or not value.isdigit():
            raise InputError(f"--backlog takes PORT=N, for example 8082=128, got {item!r}")
        backlogs[int(port)] = int(value)
    return backlogs


def read_port_range(root: Path) -> Tuple[int, int]:
    path = root / "proc/sys/net/ipv4/ip_local_port_range"
    try:
        return parse_port_range(path.read_text())
    except OSError:
        return DEFAULT_PORT_RANGE


def load_live(root: Path, byteorder: str = sys.byteorder) -> Table:
    net = root / "proc" / "net"
    tcp = net / "tcp"
    if not tcp.exists():
        raise InputError(f"{tcp} not found: scan reads the live socket table on Linux; elsewhere capture `cat /proc/net/tcp` or `ss -tanpi` on the host and run diagnose on the files")
    socks = parse_proc_net_tcp(tcp.read_text(), byteorder)
    tcp6 = net / "tcp6"
    if tcp6.exists():
        socks += parse_proc_net_tcp(tcp6.read_text(), byteorder)
    counters: Dict[str, int] = {}
    for name in ("snmp", "netstat"):
        path = net / name
        if path.exists():
            counters.update(parse_proc_counters(path.read_text()))
    attach_owners(socks, socket_owners(root / "proc"))
    return Table(socks, owners_known=True, counters=counters, port_range=read_port_range(root))


def owner_label(sock: Sock, owners_known: bool) -> str:
    if sock.processes:
        labels: List[str] = []
        for process in sock.processes:
            label = f"{process.name} (pid {process.pid})"
            if label not in labels:
                labels.append(label)
        return ", ".join(labels)
    if sock.source == "proc":
        return "no process" if sock.inode == 0 else "unknown process"
    return "no process" if owners_known else "unknown process"


def grouped(socks: Iterable[Sock], key: Callable[[Sock], Any]) -> Dict[Any, List[Sock]]:
    groups: Dict[Any, List[Sock]] = {}
    for sock in socks:
        groups.setdefault(key(sock), []).append(sock)
    return groups


def close_wait_findings(table: Table, limits: Limits) -> List[Finding]:
    listeners = {s.lport for s in table.socks if s.state == "LISTEN"}
    findings = []
    waiting = (s for s in table.socks if s.state == "CLOSE_WAIT")
    for (owner, port), socks in grouped(waiting, lambda s: (owner_label(s, table.owners_known), s.lport)).items():
        count = len(socks)
        severity = "warning" if count >= limits.close_wait else "info"
        if owner == "no process" and port in listeners:
            summary = f"{count} CLOSE_WAIT on :{port} with no process: still in the accept queue, the client gave up before accept() was called"
            findings.append(Finding("close-wait", severity, summary, FIX_QUEUED, count, port, owner=owner))
            continue
        summary = f"{count} CLOSE_WAIT on :{port} owned by {owner}: the peer closed and the process has not called close()"
        if all(s.recv_q > 0 for s in socks):
            summary += f"; Recv-Q > 0 on all {count}, so it has not even read to end of file (the FIN counts as one byte until read)"
        findings.append(Finding("close-wait", severity, summary, FIX_CLOSE_WAIT, count, port, owner=owner))
    return findings


def accept_queue_findings(table: Table) -> List[Finding]:
    findings = []
    for sock in table.socks:
        if sock.state != "LISTEN":
            continue
        backlog = sock.send_q if sock.source == "ss" else table.backlogs.get(sock.lport)
        queued = sock.recv_q
        owner = owner_label(sock, table.owners_known)
        if backlog is not None and queued > backlog:
            summary = f"accept queue of {sock.where} is full: Recv-Q {queued} > backlog {backlog}; new SYNs are dropped, so clients see connect timeouts, not refusals"
            findings.append(Finding("accept-queue", "critical", summary, FIX_ACCEPT, queued, sock.lport, owner=owner))
        elif backlog and queued == backlog:
            summary = f"accept queue of {sock.where} is at its limit: Recv-Q {queued} = backlog {backlog}; one more connection fills it"
            findings.append(Finding("accept-queue", "warning", summary, FIX_ACCEPT, queued, sock.lport, owner=owner))
        elif queued > 0:
            summary = f"{queued} connection(s) waiting in the accept queue of {sock.where}"
            if backlog is None:
                summary += "; /proc/net/tcp does not show the backlog: read Send-Q in `ss -lnt` or pass --backlog PORT=N"
            findings.append(Finding("accept-queue", "info", summary, FIX_ACCEPT, queued, sock.lport, owner=owner))
    return findings


def syn_recv_findings(table: Table, limits: Limits) -> List[Finding]:
    findings = []
    half_open = (s for s in table.socks if s.state == "SYN_RECV")
    for port, socks in grouped(half_open, lambda s: s.lport).items():
        count, worst = len(socks), max(s.retransmits for s in socks)
        severity = "warning" if count >= limits.syn_recv or worst >= 2 else "info"
        summary = f"{count} half-open connection(s) on :{port} (SYN-ACK resent up to {worst} times): clients sent SYN but their final ACK has not arrived"
        findings.append(Finding("syn-recv", severity, summary, FIX_SYN_RECV, count, port))
    return findings


def syn_sent_findings(table: Table) -> List[Finding]:
    findings = []
    connecting = (s for s in table.socks if s.state == "SYN_SENT")
    for peer, socks in grouped(connecting, lambda s: s.peer).items():
        count, worst = len(socks), max(s.retransmits for s in socks)
        owner = owner_label(socks[0], table.owners_known)
        if worst >= 1:
            summary = f"connect() to {peer} has resent its SYN up to {worst} times on {count} socket(s): nothing answers, not even a RST"
            findings.append(Finding("syn-sent", "warning", summary, FIX_SYN_SENT, count, peer=peer, owner=owner))
        else:
            findings.append(Finding("syn-sent", "info", f"{count} connect() in progress to {peer}", FIX_SYN_SENT, count, peer=peer, owner=owner))
    return findings


def fin_wait2_findings(table: Table, limits: Limits) -> List[Finding]:
    findings = []
    closing = (s for s in table.socks if s.state == "FIN_WAIT2")
    for peer, socks in grouped(closing, lambda s: s.peer).items():
        count = len(socks)
        severity = "warning" if count >= limits.close_wait else "info"
        summary = f"{count} FIN_WAIT2 to {peer}: this side closed, the peer acknowledged but has not closed its end"
        findings.append(Finding("fin-wait2", severity, summary, FIX_FIN_WAIT2, count, peer=peer))
    return findings


def retransmit_findings(table: Table) -> List[Finding]:
    findings = []
    sending = [s for s in table.socks if s.state in SENDING]
    retrying = (s for s in sending if s.retransmits >= 1 and s.timer != "persist")
    for peer, socks in grouped(retrying, lambda s: s.peer).items():
        worst, queued = max(s.retransmits for s in socks), max(s.send_q for s in socks)
        summary = f"{len(socks)} connection(s) to {peer} are retransmitting (up to {worst} times, up to {queued} bytes unacknowledged): the peer or the path is not acknowledging"
        findings.append(Finding("retransmit", "warning", summary, FIX_RETRANS, len(socks), peer=peer, owner=owner_label(socks[0], table.owners_known)))
    probing = (s for s in sending if s.timer == "persist")
    for peer, socks in grouped(probing, lambda s: s.peer).items():
        queued = max(s.send_q for s in socks)
        summary = f"{len(socks)} connection(s) to {peer} are probing a zero window with up to {queued} bytes queued: the peer's application stopped reading"
        findings.append(Finding("zero-window", "warning", summary, FIX_ZERO_WINDOW, len(socks), peer=peer, owner=owner_label(socks[0], table.owners_known)))
    return findings


def port_findings(table: Table, limits: Limits) -> List[Finding]:
    low, high = table.port_range
    size = high - low + 1
    findings = []
    outgoing = (s for s in table.socks if s.state not in ("LISTEN", "CLOSE") and s.rport and low <= s.lport <= high)
    for (local, peer), socks in grouped(outgoing, lambda s: (s.local, s.peer)).items():
        share = len(socks) / size
        if share < limits.port_warning:
            continue
        severity = "critical" if share >= limits.port_critical else "warning"
        time_wait = sum(1 for s in socks if s.state == "TIME_WAIT")
        summary = f"{local} -> {peer} holds {len(socks)} of {size} ephemeral ports ({share:.0%}), {time_wait} in TIME_WAIT: new connections to it fail with EADDRNOTAVAIL when the range runs out"
        findings.append(Finding("ephemeral-ports", severity, summary, FIX_PORTS, len(socks), peer=peer))
    return findings


COUNTER_RULES = (
    ("TcpExtListenOverflows", "warning", "the kernel dropped {n} connection attempt(s) because an accept queue was full (TcpExtListenOverflows, counted since boot in this network namespace)", "compare two readings (`nstat` prints the change since its last run); a rising value is a listener that does not accept fast enough"),
    ("TcpExtTCPAbortOnTimeout", "warning", "{n} connection(s) were aborted after exhausting their retransmissions or keepalive probes (TcpExtTCPAbortOnTimeout)", "a peer or the path went silent mid-connection; find which destinations with `ss -tio` while it happens"),
    ("TcpExtTCPSynRetrans", "info", "{n} SYN and SYN-ACK retransmission(s) since boot (TcpExtTCPSynRetrans)", "a rising value means handshakes go unanswered; look for SYN_SENT and SYN_RECV sockets"),
    ("TcpExtTCPTimeouts", "info", "{n} retransmission timeout(s) fired since boot (TcpExtTCPTimeouts)", "a rising value with little traffic means loss or silent peers"),
)


def counter_findings(table: Table) -> List[Finding]:
    findings = []
    for name, severity, summary, fix in COUNTER_RULES:
        value = table.counters.get(name, 0)
        if value > 0:
            findings.append(Finding("counter", severity, summary.format(n=value), fix, value, owner=name))
    return findings


def diagnose(table: Table, limits: Optional[Limits] = None) -> List[Finding]:
    limits = limits or Limits()
    findings = (
        close_wait_findings(table, limits)
        + accept_queue_findings(table)
        + syn_recv_findings(table, limits)
        + syn_sent_findings(table)
        + fin_wait2_findings(table, limits)
        + retransmit_findings(table)
        + port_findings(table, limits)
        + counter_findings(table)
    )
    return sorted(findings, key=lambda f: (SEVERITY_RANK[f.severity], f.kind == "counter", -f.sockets, f.kind))


def state_counts(socks: Iterable[Sock]) -> Dict[str, int]:
    counts = Counter(s.state for s in socks)
    ordered = {state: counts[state] for state in STATE_ORDER if counts[state]}
    ordered.update({state: n for state, n in sorted(counts.items()) if state not in ordered})
    return ordered


def report(table: Table, findings: List[Finding]) -> Dict[str, Any]:
    sockets = []
    for sock in table.socks:
        row = asdict(sock)
        row["owner"] = owner_label(sock, table.owners_known)
        sockets.append(row)
    return {
        "states": state_counts(table.socks),
        "findings": [asdict(f) for f in findings],
        "counters": {name: table.counters[name] for name in WATCHED_COUNTERS if name in table.counters},
        "port_range": list(table.port_range),
        "sockets": sockets,
    }


def render_report(table: Table, findings: List[Finding], show_sockets: bool) -> str:
    states = ", ".join(f"{state} {n}" for state, n in state_counts(table.socks).items())
    lines = [f"tcpwatch: {len(table.socks)} sockets: {states or 'none'}"]
    if show_sockets:
        lines.append(f"  {'STATE':<12} {'RECV-Q':>6} {'SEND-Q':>6} {'LOCAL':<28} {'PEER':<28} {'TIMER':<20} OWNER")
        for s in table.socks:
            timer = f"{s.timer},{s.expires:g}s,{s.retransmits}" if s.timer != "off" else "-"
            lines.append(f"  {s.state:<12} {s.recv_q:>6} {s.send_q:>6} {s.where:<28} {s.peer:<28} {timer:<20} {owner_label(s, table.owners_known)}")
    if not findings:
        lines.append("no findings")
    for finding in findings:
        lines.append(f"{finding.severity:<9} {finding.kind:<16} {finding.summary}")
        lines.append(f"{'':<9} {'':<16} fix: {finding.fix}")
    return "\n".join(lines)


TRANSITIONS: Dict[Tuple[str, str], Tuple[str, str]] = {
    ("CLOSED", "listen"): ("LISTEN", ""),
    ("CLOSED", "connect"): ("SYN_SENT", "SYN"),
    ("CLOSED", "SYN"): ("CLOSED", "RST"),
    ("LISTEN", "SYN"): ("SYN_RECV", "SYN+ACK"),
    ("LISTEN", "close"): ("CLOSED", ""),
    ("SYN_SENT", "SYN+ACK"): ("ESTABLISHED", "ACK"),
    ("SYN_SENT", "SYN"): ("SYN_RECV", "SYN+ACK"),
    ("SYN_SENT", "RST"): ("CLOSED", ""),
    ("SYN_SENT", "timeout"): ("CLOSED", ""),
    ("SYN_SENT", "close"): ("CLOSED", ""),
    ("SYN_RECV", "ACK"): ("ESTABLISHED", ""),
    ("SYN_RECV", "SYN+ACK"): ("ESTABLISHED", "ACK"),
    ("SYN_RECV", "RST"): ("LISTEN", ""),
    ("SYN_RECV", "close"): ("FIN_WAIT1", "FIN"),
    ("ESTABLISHED", "close"): ("FIN_WAIT1", "FIN"),
    ("ESTABLISHED", "FIN"): ("CLOSE_WAIT", "ACK"),
    ("FIN_WAIT1", "ACK"): ("FIN_WAIT2", ""),
    ("FIN_WAIT1", "FIN"): ("CLOSING", "ACK"),
    ("FIN_WAIT1", "FIN+ACK"): ("TIME_WAIT", "ACK"),
    ("FIN_WAIT2", "FIN"): ("TIME_WAIT", "ACK"),
    ("FIN_WAIT2", "timeout"): ("CLOSED", ""),
    ("CLOSING", "ACK"): ("TIME_WAIT", ""),
    ("CLOSE_WAIT", "close"): ("LAST_ACK", "FIN"),
    ("LAST_ACK", "ACK"): ("CLOSED", ""),
    ("TIME_WAIT", "FIN"): ("TIME_WAIT", "ACK"),
    ("TIME_WAIT", "timeout"): ("CLOSED", ""),
}
SYNCHRONIZED = ("ESTABLISHED", "FIN_WAIT1", "FIN_WAIT2", "CLOSE_WAIT", "CLOSING", "LAST_ACK")
NOTES = {
    ("CLOSED", "listen"): "listen(): ready for SYNs",
    ("CLOSED", "connect"): "connect() sends SYN",
    ("CLOSED", "SYN"): "nobody listens on the port: the kernel answers RST",
    ("LISTEN", "SYN"): "the request enters the SYN queue",
    ("SYN_SENT", "SYN+ACK"): "connect() returns",
    ("SYN_SENT", "RST"): "connect() fails with ECONNREFUSED",
    ("SYN_SENT", "timeout"): "connect() fails with ETIMEDOUT",
    ("SYN_RECV", "ACK"): "handshake done: the connection waits in the accept queue for accept()",
    ("ESTABLISHED", "close"): "close() sends FIN",
    ("ESTABLISHED", "FIN"): "recv() returns 0 bytes; the socket stays in CLOSE_WAIT until the application calls close()",
    ("ESTABLISHED", "RST"): "the next recv() or send() fails with ECONNRESET",
    ("ESTABLISHED", "abort"): "close() with SO_LINGER 0: RST instead of FIN, no TIME_WAIT",
    ("FIN_WAIT1", "ACK"): "half-closed: waiting for the peer's FIN",
    ("FIN_WAIT1", "FIN"): "both sides closed at once",
    ("FIN_WAIT2", "FIN"): "keeps the 4-tuple for 60 s so a delayed segment cannot land in a new connection",
    ("FIN_WAIT2", "timeout"): "net.ipv4.tcp_fin_timeout frees the orphaned socket; the server is still in CLOSE_WAIT",
    ("CLOSING", "ACK"): "keeps the 4-tuple for 60 s",
    ("CLOSE_WAIT", "close"): "close() sends the second FIN",
    ("LAST_ACK", "ACK"): "fully closed",
    ("TIME_WAIT", "timeout"): "60 s later the 4-tuple is free again",
}


@dataclass
class Row:
    t: float
    side: str
    event: str
    sends: str
    client: str
    server: str
    note: str = ""


def step(state: str, event: str) -> Tuple[str, str]:
    if (state, event) in TRANSITIONS:
        return TRANSITIONS[(state, event)]
    if event == "RST" and state in SYNCHRONIZED:
        return "CLOSED", ""
    if event == "abort" and state in SYNCHRONIZED:
        return "CLOSED", "RST"
    raise InputError(f"{event} is not a valid event in {state}")


def model_timeout(boundary: int, rto_base: float, rto_max: float = TCP_RTO_MAX) -> float:
    threshold = int(math.log2(rto_max / rto_base))
    if boundary <= threshold:
        return round(((2 << boundary) - 1) * rto_base, 3)
    return round(((2 << threshold) - 1) * rto_base + (boundary - threshold) * rto_max, 3)


def syn_schedule(retries: int, rto: float = TCP_TIMEOUT_INIT) -> List[float]:
    return [model_timeout(n, rto) for n in range(retries)]


class Wire:
    def __init__(self, drop_to: Sequence[str] = ()):
        self.states = {"client": "CLOSED", "server": "CLOSED"}
        self.queue: List[Tuple[str, str]] = []
        self.rows: List[Row] = []
        self.t = 0.0
        self.drop_to = set(drop_to)

    def record(self, side: str, event: str, sends: str, note: str) -> None:
        self.rows.append(Row(round(self.t, 3), side, event, sends or "-", self.states["client"], self.states["server"], note))

    def act(self, side: str, event: str) -> None:
        before = self.states[side]
        self.states[side], sends = step(before, event)
        self.record(side, event, sends, NOTES.get((before, event), ""))
        if sends:
            self.queue.append(("server" if side == "client" else "client", sends))

    def deliver(self) -> None:
        while self.queue:
            side, segment = self.queue.pop(0)
            self.t += ONE_WAY_DELAY
            if side in self.drop_to:
                self.record(side, segment, "", f"dropped before it reached the {side}")
                continue
            self.act(side, segment)

    def syn_retries(self, retries: int) -> None:
        start = self.t - ONE_WAY_DELAY
        for number, at in enumerate(syn_schedule(retries), 1):
            self.t = start + at
            self.record("client", "rto", "SYN", f"retransmission {number}, the RTO doubles to {2 ** number:g} s")
            self.queue.append(("server", "SYN"))
            self.deliver()
        self.t = start + model_timeout(retries, TCP_TIMEOUT_INIT)
        self.act("client", "timeout")


HANDSHAKE = [("app", "server", "listen"), ("app", "client", "connect")]
SCENARIOS: Dict[str, Tuple[str, List[Tuple[Any, ...]], Tuple[str, ...]]] = {
    "handshake": ("SYN, SYN+ACK, ACK: connect() returns after one round trip, the server side waits in the accept queue", HANDSHAKE, ()),
    "close": ("the client closes first, the server closes after reading end of file; the client holds TIME_WAIT for 60 s", HANDSHAKE + [("app", "client", "close"), ("app", "server", "close"), ("timer", "client", TIME_WAIT_SECONDS)], ()),
    "close-wait-leak": ("the client closes, the server reads end of file and never calls close(): CLOSE_WAIT has no timer", HANDSHAKE + [("app", "client", "close"), ("timer", "client", FIN_TIMEOUT_SECONDS)], ()),
    "simultaneous-close": ("both sides call close() before either FIN arrives: both pass through CLOSING and both hold TIME_WAIT", HANDSHAKE + [("both", "close"), ("timer", "client", TIME_WAIT_SECONDS), ("timer", "server", 0.0)], ()),
    "refused": ("nothing listens on the port: the server's kernel answers the SYN with RST", [("app", "client", "connect")], ()),
    "reset": ("the server aborts an established connection with SO_LINGER 0: RST, no FIN, no TIME_WAIT", HANDSHAKE + [("app", "server", "abort")], ()),
    "blackhole": ("every SYN is dropped on the way: the client retransmits on a doubling timer until tcp_syn_retries runs out", [("app", "client", "connect"), ("syn-retries",)], ("server",)),
}


def simulate(name: str, syn_retries: int = 6) -> List[Row]:
    if name not in SCENARIOS:
        raise InputError(f"unknown scenario {name!r}; choose one of: {', '.join(SCENARIOS)}")
    _, steps, drop_to = SCENARIOS[name]
    wire = Wire(drop_to)
    for action in steps:
        if action[0] == "app":
            wire.act(action[1], action[2])
        elif action[0] == "both":
            wire.act("client", action[1])
            wire.act("server", action[1])
        elif action[0] == "timer":
            wire.t += action[2]
            wire.act(action[1], "timeout")
        elif action[0] == "syn-retries":
            wire.syn_retries(syn_retries)
        wire.deliver()
    return wire.rows


def render_simulation(name: str, rows: List[Row]) -> str:
    lines = [f"scenario {name}: {SCENARIOS[name][0]}", f"{'t':>8}  {'side':<7} {'event':<8} {'sends':<8} {'client':<12} {'server':<12} note"]
    for r in rows:
        lines.append(f"{r.t:>8.3f}  {r.side:<7} {r.event:<8} {r.sends:<8} {r.client:<12} {r.server:<12} {r.note}")
    return "\n".join(lines)


SYSCTLS = (
    "net.ipv4.tcp_syn_retries",
    "net.ipv4.tcp_syn_linear_timeouts",
    "net.ipv4.tcp_synack_retries",
    "net.ipv4.tcp_retries2",
    "net.ipv4.tcp_fin_timeout",
    "net.ipv4.tcp_keepalive_time",
    "net.ipv4.tcp_keepalive_intvl",
    "net.ipv4.tcp_keepalive_probes",
    "net.ipv4.ip_local_port_range",
    "net.ipv4.tcp_tw_reuse",
    "net.core.somaxconn",
    "net.ipv4.tcp_max_syn_backlog",
    "net.ipv4.tcp_abort_on_overflow",
    "net.ipv4.tcp_syncookies",
)


def read_sysctls(root: Path) -> Dict[str, str]:
    values = {}
    for name in SYSCTLS:
        try:
            values[name] = " ".join((root / "proc/sys" / name.replace(".", "/")).read_text().split())
        except OSError:
            continue
    return values


def kernel_timeouts(values: Dict[str, str]) -> Dict[str, Any]:
    def number(name: str) -> Optional[int]:
        text = values.get(name, "").split()
        return int(text[0]) if text and text[0].lstrip("-").isdigit() else None

    derived: Dict[str, Any] = {"time_wait": TIME_WAIT_SECONDS}
    syn, synack, retries2 = number("net.ipv4.tcp_syn_retries"), number("net.ipv4.tcp_synack_retries"), number("net.ipv4.tcp_retries2")
    if syn is not None:
        derived["syn_timeout"] = model_timeout(syn, TCP_TIMEOUT_INIT)
    if number("net.ipv4.tcp_syn_linear_timeouts"):
        derived["syn_linear_timeouts"] = number("net.ipv4.tcp_syn_linear_timeouts")
    if synack is not None:
        derived["synack_timeout"] = model_timeout(synack, TCP_TIMEOUT_INIT)
    if retries2 is not None:
        derived["retries2_timeout"] = model_timeout(retries2, TCP_RTO_MIN)
    idle, interval, probes = (number(f"net.ipv4.tcp_keepalive_{k}") for k in ("time", "intvl", "probes"))
    if None not in (idle, interval, probes):
        derived["keepalive_detection"] = idle + interval * probes
    if number("net.ipv4.tcp_fin_timeout") is not None:
        derived["fin_timeout"] = number("net.ipv4.tcp_fin_timeout")
    if "net.ipv4.ip_local_port_range" in values:
        low, high = parse_port_range(values["net.ipv4.ip_local_port_range"])
        derived["ephemeral_ports"] = high - low + 1
    if number("net.core.somaxconn") is not None:
        derived["somaxconn"] = number("net.core.somaxconn")
    return derived


def render_sysctls(values: Dict[str, str], derived: Dict[str, Any]) -> str:
    meaning = {
        "net.ipv4.tcp_syn_retries": f"connect() to a host that never answers fails after {derived.get('syn_timeout', '?')} s (1 s first RTO, doubling)",
        "net.ipv4.tcp_syn_linear_timeouts": "this kernel spaces the first SYN retries linearly, so the connect() figure above differs; measure it with `ss -tano state syn-sent`",
        "net.ipv4.tcp_synack_retries": f"a half-open SYN_RECV entry is dropped after about {derived.get('synack_timeout', '?')} s",
        "net.ipv4.tcp_retries2": f"data nobody acknowledges: the connection is aborted after at least {derived.get('retries2_timeout', '?')} s",
        "net.ipv4.tcp_fin_timeout": "an orphaned FIN_WAIT2 socket is freed after this many seconds",
        "net.ipv4.tcp_keepalive_time": f"idle seconds before the first probe; a dead idle peer is found after {derived.get('keepalive_detection', '?')} s, and only on sockets with SO_KEEPALIVE",
        "net.ipv4.tcp_keepalive_intvl": "seconds between unanswered probes",
        "net.ipv4.tcp_keepalive_probes": "unanswered probes before the connection is dropped",
        "net.ipv4.ip_local_port_range": f"{derived.get('ephemeral_ports', '?')} local ports per source address and destination",
        "net.ipv4.tcp_tw_reuse": "1: reuse TIME_WAIT for new outgoing connections, 2: loopback only",
        "net.core.somaxconn": "the cap on every listen() backlog",
        "net.ipv4.tcp_max_syn_backlog": "the SYN queue limit per listener",
        "net.ipv4.tcp_abort_on_overflow": "1: answer RST when the accept queue is full instead of dropping",
        "net.ipv4.tcp_syncookies": "1: answer with SYN cookies when the SYN queue overflows",
    }
    pairs = [(f"{name} = {value}", meaning[name]) for name, value in values.items()]
    pairs.append(("TIME_WAIT = 60", "fixed in the kernel (TCP_TIMEWAIT_LEN), not a sysctl"))
    width = max(len(left) for left, _ in pairs)
    return "\n".join(f"{left:<{width}}  {right}" for left, right in pairs)


@dataclass
class Timeouts:
    connect: Optional[float] = None
    read: Optional[float] = None
    total: Optional[float] = None


@dataclass
class Keepalive:
    idle: int
    interval: int
    count: int


@dataclass
class ProbeResult:
    outcome: str = "ok"
    elapsed: float = 0.0
    connected: bool = False
    received: int = 0
    error: str = ""
    options: Dict[str, int] = field(default_factory=dict)


Connector = Callable[[Tuple[str, int], Optional[float]], socket.socket]


def keepalive_options(keepalive: Keepalive) -> List[Tuple[str, int, int]]:
    idle = getattr(socket, "TCP_KEEPIDLE", None)
    if idle is None and sys.platform == "darwin":
        idle = getattr(socket, "TCP_KEEPALIVE", DARWIN_TCP_KEEPALIVE)
    candidates = [
        ("TCP_KEEPIDLE", idle, keepalive.idle),
        ("TCP_KEEPINTVL", getattr(socket, "TCP_KEEPINTVL", None), keepalive.interval),
        ("TCP_KEEPCNT", getattr(socket, "TCP_KEEPCNT", None), keepalive.count),
    ]
    return [(name, option, value) for name, option, value in candidates if option is not None]


def set_keepalive(sock: socket.socket, keepalive: Keepalive) -> Dict[str, int]:
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
    applied = {"SO_KEEPALIVE": 1}
    for name, option, value in keepalive_options(keepalive):
        sock.setsockopt(socket.IPPROTO_TCP, option, value)
        applied[name] = value
    return applied


def probe(host: str, port: int, timeouts: Timeouts, payload: bytes = b"", keepalive: Optional[Keepalive] = None, user_timeout_ms: Optional[int] = None, connector: Connector = socket.create_connection, clock: Callable[[], float] = time.monotonic) -> ProbeResult:
    start = clock()
    deadline = None if timeouts.total is None else start + timeouts.total
    result = ProbeResult()

    def finish(outcome: str, error: Optional[BaseException] = None) -> ProbeResult:
        result.outcome, result.elapsed = outcome, round(clock() - start, 3)
        result.error = f"{type(error).__name__}: {error}" if error else ""
        return result

    def budget(limit: Optional[float], kind: str) -> Tuple[Optional[float], str]:
        if deadline is None:
            return limit, kind
        remaining = deadline - clock()
        if limit is None or remaining < limit:
            return remaining, "total"
        return limit, kind

    wait, kind = budget(timeouts.connect, "connect")
    if wait is not None and wait <= 0:
        return finish("total-timeout")
    try:
        sock = connector((host, port), wait)
    except socket.timeout as error:
        return finish(f"{kind}-timeout", error)
    except ConnectionRefusedError as error:
        return finish("refused", error)
    except OSError as error:
        return finish("unreachable" if error.errno in UNREACHABLE else "error", error)
    result.connected = True
    with sock:
        try:
            if keepalive is not None:
                result.options.update(set_keepalive(sock, keepalive))
            if user_timeout_ms is not None and hasattr(socket, "TCP_USER_TIMEOUT"):
                sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_USER_TIMEOUT, user_timeout_ms)
                result.options["TCP_USER_TIMEOUT"] = user_timeout_ms
            if payload:
                wait, kind = budget(timeouts.read, "write")
                sock.settimeout(wait)
                sock.sendall(payload)
            while True:
                wait, kind = budget(timeouts.read, "read")
                if wait is not None and wait <= 0:
                    return finish("total-timeout")
                sock.settimeout(wait)
                data = sock.recv(65536)
                if not data:
                    return finish("ok")
                result.received += len(data)
        except socket.timeout as error:
            return finish(f"{kind}-timeout", error)
        except ConnectionResetError as error:
            return finish("reset", error)
        except OSError as error:
            return finish("error", error)


def listen(host: str, port: int, backlog: int) -> socket.socket:
    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    sock = socket.socket(family, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind((host, port))
    sock.listen(backlog)
    return sock


def serve_leak(listener: socket.socket, stop: threading.Event, held: List[socket.socket]) -> None:
    listener.settimeout(0.1)
    while not stop.is_set():
        try:
            conn, _ = listener.accept()
        except socket.timeout:
            continue
        except OSError:
            return
        held.append(conn)


def serve_never_accept(listener: socket.socket, stop: threading.Event, held: List[socket.socket]) -> None:
    stop.wait()


def trickle(conn: socket.socket, stop: threading.Event, interval: float, duration: float) -> None:
    end = time.monotonic() + duration
    with conn:
        while not stop.is_set() and time.monotonic() < end:
            try:
                conn.sendall(b".")
            except OSError:
                return
            stop.wait(interval)


def serve_trickle(listener: socket.socket, stop: threading.Event, held: List[socket.socket], interval: float = 0.5, duration: float = 30.0) -> None:
    listener.settimeout(0.1)
    while not stop.is_set():
        try:
            conn, _ = listener.accept()
        except socket.timeout:
            continue
        except OSError:
            return
        threading.Thread(target=trickle, args=(conn, stop, interval, duration), daemon=True).start()


def open_clients(host: str, port: int, count: int, connect_timeout: Optional[float], connector: Connector = socket.create_connection) -> Tuple[List[socket.socket], Dict[str, int]]:
    held: List[socket.socket] = []
    tally = {"connected": 0, "connect-timeout": 0, "refused": 0, "error": 0}
    for _ in range(count):
        try:
            held.append(connector((host, port), connect_timeout))
            tally["connected"] += 1
        except socket.timeout:
            tally["connect-timeout"] += 1
        except ConnectionRefusedError:
            tally["refused"] += 1
        except OSError:
            tally["error"] += 1
    return held, tally


def parse_keepalive(text: str) -> Keepalive:
    parts = text.split(",")
    if len(parts) != 3 or not all(p.isdigit() and int(p) > 0 for p in parts):
        raise InputError(f"--keepalive takes IDLE,INTERVAL,COUNT in seconds and probes, for example 30,10,3, got {text!r}")
    return Keepalive(*(int(p) for p in parts))


def read_input(path: str) -> str:
    if path == "-":
        return sys.stdin.read()
    try:
        return Path(path).read_text()
    except OSError as error:
        raise InputError(f"cannot read {path}: {error.strerror or error}") from None


def table_from_files(args: argparse.Namespace) -> Table:
    if bool(args.proc) == bool(args.ss):
        raise InputError("pass either --proc FILE (repeat for tcp and tcp6) or --ss FILE")
    if args.ss:
        socks = parse_ss(read_input(args.ss))
        owners_known = any(s.processes for s in socks)
    else:
        socks = [s for path in args.proc for s in parse_proc_net_tcp(read_input(path), args.byteorder)]
        owners_known = False
    counters: Dict[str, int] = {}
    for path in args.netstat or []:
        counters.update(parse_proc_counters(read_input(path)))
    if args.nstat:
        counters.update(parse_nstat(read_input(args.nstat)))
    return Table(socks, owners_known, counters, parse_port_range(args.port_range or "-".join(map(str, DEFAULT_PORT_RANGE))), parse_backlogs(args.backlog or []))


def emit_diagnosis(table: Table, args: argparse.Namespace) -> int:
    limits = Limits(close_wait=args.close_wait_threshold)
    findings = diagnose(table, limits)
    if args.json:
        print(json.dumps(report(table, findings), indent=2))
    else:
        print(render_report(table, findings, args.sockets))
    return 1 if any(f.severity in ("critical", "warning") for f in findings) else 0


def cmd_diagnose(args: argparse.Namespace) -> int:
    return emit_diagnosis(table_from_files(args), args)


def cmd_scan(args: argparse.Namespace) -> int:
    table = load_live(Path(args.root), args.byteorder)
    if args.port_range:
        table.port_range = parse_port_range(args.port_range)
    table.backlogs = parse_backlogs(args.backlog or [])
    return emit_diagnosis(table, args)


def cmd_simulate(args: argparse.Namespace) -> int:
    if args.list or not args.scenario:
        for name, (description, _, _) in SCENARIOS.items():
            print(f"{name:<20} {description}")
        return 0
    rows = simulate(args.scenario, args.syn_retries)
    if args.json:
        print(json.dumps({"scenario": args.scenario, "description": SCENARIOS[args.scenario][0], "rows": [asdict(r) for r in rows]}, indent=2))
    else:
        print(render_simulation(args.scenario, rows))
    return 0


def cmd_sysctl(args: argparse.Namespace) -> int:
    values = read_sysctls(Path(args.root))
    if not values:
        raise InputError(f"no TCP sysctls under {Path(args.root) / 'proc/sys'}: run on Linux or pass --root to a copied tree")
    derived = kernel_timeouts(values)
    if args.json:
        print(json.dumps({"values": values, "derived": derived}, indent=2))
    else:
        print(render_sysctls(values, derived))
    return 0


def cmd_probe(args: argparse.Namespace) -> int:
    timeouts = Timeouts() if args.no_timeouts else Timeouts(args.connect_timeout, args.read_timeout, args.max_time)
    keepalive = parse_keepalive(args.keepalive) if args.keepalive else None
    result = probe(args.host, args.port, timeouts, args.send.encode(), keepalive, args.user_timeout)
    if args.json:
        print(json.dumps({"host": args.host, "port": args.port, "timeouts": asdict(timeouts), **asdict(result)}, indent=2))
    else:
        limits = ", ".join(f"{k} {v:g}s" for k, v in asdict(timeouts).items() if v is not None) or "no timeouts"
        print(f"probe {endpoint(args.host, args.port)} ({limits})")
        print(f"outcome {result.outcome} after {result.elapsed} s (connected: {'yes' if result.connected else 'no'}, received {result.received} bytes){' ' + result.error if result.error else ''}")
        if result.options:
            print("socket options: " + ", ".join(f"{k}={v}" for k, v in result.options.items()))
    return 0 if result.outcome == "ok" else 1


def cmd_lab_server(args: argparse.Namespace) -> int:
    servers = {"leak": serve_leak, "backlog": serve_never_accept, "trickle": serve_trickle}
    try:
        listener = listen(args.host, args.port, args.backlog)
    except OSError as error:
        raise InputError(f"cannot listen on {endpoint(args.host, args.port)}: {error.strerror or error}") from None
    stop = threading.Event()
    held: List[socket.socket] = []
    extra = (args.interval, args.duration) if args.kind == "trickle" else ()
    threading.Thread(target=servers[args.kind], args=(listener, stop, held) + extra, daemon=True).start()
    host, port = listener.getsockname()[:2]
    print(json.dumps({"lab": args.kind, "listening": endpoint(host, port), "port": port, "pid": os.getpid(), "backlog": args.backlog}), flush=True)
    try:
        stop.wait(args.seconds if args.seconds > 0 else None)
    except KeyboardInterrupt:
        pass
    stop.set()
    listener.close()
    for conn in held:
        conn.close()
    return 0


def cmd_lab_clients(args: argparse.Namespace) -> int:
    held, tally = open_clients(args.host, args.port, args.count, args.connect_timeout)
    print(json.dumps({"lab": "clients", "target": endpoint(args.host, args.port), "pid": os.getpid(), **tally}), flush=True)
    try:
        time.sleep(args.hold)
    except KeyboardInterrupt:
        pass
    for conn in held:
        conn.close()
    return 0


def add_table_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--port-range", help="ephemeral range LOW-HIGH (net.ipv4.ip_local_port_range)")
    parser.add_argument("--backlog", action="append", metavar="PORT=N", help="listen() backlog of a port, for /proc input where it is not shown")
    parser.add_argument("--close-wait-threshold", type=int, default=Limits.close_wait, help="CLOSE_WAIT sockets per process and port that count as a leak")
    parser.add_argument("--byteorder", choices=("little", "big"), default=sys.byteorder, help="byte order of the host that wrote /proc/net/tcp")
    parser.add_argument("--sockets", action="store_true", help="print every socket")
    parser.add_argument("--json", action="store_true")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="tcpwatch", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    diag = sub.add_parser("diagnose", help="diagnose a captured /proc/net/tcp or ss -tanpi table")
    diag.add_argument("--proc", action="append", metavar="FILE", help="a copy of /proc/net/tcp or /proc/net/tcp6 (repeatable)")
    diag.add_argument("--ss", metavar="FILE", help="output of ss -tanpi or ss -tanpio, - for stdin")
    diag.add_argument("--netstat", action="append", metavar="FILE", help="a copy of /proc/net/netstat or /proc/net/snmp (repeatable)")
    diag.add_argument("--nstat", metavar="FILE", help="output of nstat -az")
    add_table_options(diag)
    diag.set_defaults(func=cmd_diagnose)

    scan = sub.add_parser("scan", help="diagnose the live socket table of this network namespace (Linux)")
    scan.add_argument("--root", default="/", help="read ROOT/proc instead of /proc")
    add_table_options(scan)
    scan.set_defaults(func=cmd_scan)

    sim = sub.add_parser("simulate", help="walk the TCP state machine through a scenario")
    sim.add_argument("scenario", nargs="?", help=", ".join(SCENARIOS))
    sim.add_argument("--list", action="store_true")
    sim.add_argument("--syn-retries", type=int, default=6)
    sim.add_argument("--json", action="store_true")
    sim.set_defaults(func=cmd_simulate)

    ctl = sub.add_parser("sysctl", help="read the TCP sysctls and turn them into timeouts")
    ctl.add_argument("--root", default="/")
    ctl.add_argument("--json", action="store_true")
    ctl.set_defaults(func=cmd_sysctl)

    prb = sub.add_parser("probe", help="connect, send, read until the server closes, with connect, read and total timeouts")
    prb.add_argument("host")
    prb.add_argument("port", type=int)
    prb.add_argument("--connect-timeout", type=float, default=3.0)
    prb.add_argument("--read-timeout", type=float, default=10.0)
    prb.add_argument("--max-time", type=float, default=30.0)
    prb.add_argument("--no-timeouts", action="store_true", help="block like a client that never set any")
    prb.add_argument("--send", default="", help="text to send after connecting")
    prb.add_argument("--keepalive", metavar="IDLE,INTERVAL,COUNT", help="enable SO_KEEPALIVE with these values")
    prb.add_argument("--user-timeout", type=int, metavar="MS", help="TCP_USER_TIMEOUT where the platform has it")
    prb.add_argument("--json", action="store_true")
    prb.set_defaults(func=cmd_probe)

    lab = sub.add_parser("lab", help="reproduce the failures: leak, backlog, trickle servers and a client batch")
    lab_sub = lab.add_subparsers(dest="kind", required=True)
    for kind, text in (("leak", "accept and never close"), ("backlog", "listen and never accept"), ("trickle", "send one byte per interval")):
        server = lab_sub.add_parser(kind, help=text)
        server.add_argument("--host", default="127.0.0.1")
        server.add_argument("--port", type=int, default=0)
        server.add_argument("--backlog", type=int, default=128)
        server.add_argument("--interval", type=float, default=0.5)
        server.add_argument("--duration", type=float, default=30.0)
        server.add_argument("--seconds", type=float, default=0.0, help="exit after this long (0: run until interrupted)")
        server.set_defaults(func=cmd_lab_server)
    clients = lab_sub.add_parser("clients", help="open N connections, report how many connected, hold, close")
    clients.add_argument("host")
    clients.add_argument("port", type=int)
    clients.add_argument("--count", type=int, default=5)
    clients.add_argument("--connect-timeout", type=float, default=2.0)
    clients.add_argument("--hold", type=float, default=0.0)
    clients.set_defaults(func=cmd_lab_clients)
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(sys.argv[1:] if argv is None else argv)
    try:
        return args.func(args)
    except InputError as error:
        print(f"tcpwatch: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
