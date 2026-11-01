"""ccnet: the pure half of a from-scratch container network. Allocates addresses, renders the exact ip/nft/iptables commands for a plan, and verifies a running container's plumbing from ip -j and nft -j output."""
from __future__ import annotations

import argparse
import ipaddress
import json
import os
import shlex
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Sequence

DEFAULT_CIDR = "10.90.0.0/24"
DEFAULT_BRIDGE = "cc0"
DEFAULT_DNS = "1.1.1.1"
TABLE = "ccnet"
CHAIN = "CCNET"
NAME_MAX = 10
PROTOCOLS = ("tcp", "udp")
BACKENDS = ("nft", "iptables")
NAT_TARGETS = {"DNAT": "dnat", "MASQUERADE": "masquerade", "SNAT": "snat"}
IPTABLES_FLAGS_WITHOUT_VALUE = {"-f", "--syn", "--random", "--random-fully", "--persistent"}
FIXTURE_FILES = ("addr.json", "route.json", "host.json", "nat.json", "nat.txt")


def default_state_path() -> str:
    return os.path.join(os.environ.get("CCNET_STATE", "/var/lib/ccnet"), "state.json")


@dataclass
class Publish:
    host_port: int
    container_port: int
    proto: str = "tcp"

    def __str__(self) -> str:
        return f"{self.host_port}:{self.container_port}/{self.proto}"


@dataclass
class Container:
    name: str
    ip: str
    publish: List[Publish] = field(default_factory=list)

    @property
    def veth(self) -> str:
        return f"veth-{self.name}"


@dataclass
class Network:
    cidr: str = DEFAULT_CIDR
    bridge: str = DEFAULT_BRIDGE
    containers: Dict[str, Container] = field(default_factory=dict)

    @property
    def subnet(self) -> ipaddress.IPv4Network:
        return ipaddress.IPv4Network(self.cidr)

    @property
    def gateway(self) -> str:
        return str(next(self.subnet.hosts()))

    @property
    def prefixlen(self) -> int:
        return self.subnet.prefixlen


def parse_publish(text: str) -> Publish:
    spec, _, proto = text.partition("/")
    proto = proto or "tcp"
    host, sep, container = spec.partition(":")
    if not sep or not host.isdigit() or not container.isdigit():
        raise ValueError(f"invalid publish spec {text!r}; use HOSTPORT:CONTAINERPORT[/tcp|/udp], e.g. 8080:80")
    if proto not in PROTOCOLS:
        raise ValueError(f"invalid protocol {proto!r} in {text!r}; use tcp or udp")
    ports = (int(host), int(container))
    if not all(1 <= port <= 65535 for port in ports):
        raise ValueError(f"port out of range in {text!r}")
    return Publish(ports[0], ports[1], proto)


def validate_name(name: str) -> str:
    shape_ok = 1 <= len(name) <= NAME_MAX and name[0].isalnum() and all(c.isalnum() or c == "-" for c in name)
    if not shape_ok or name != name.lower():
        raise ValueError(f"invalid name {name!r}; use 1-{NAME_MAX} lowercase letters, digits or '-' so that veth-{name[:NAME_MAX]} fits the 15-character interface name limit")
    return name


def load_state(path: str) -> Network:
    if not os.path.exists(path):
        return Network()
    with open(path, encoding="utf-8") as handle:
        raw = json.load(handle)
    containers = {
        name: Container(name, item["ip"], [parse_publish(p) for p in item.get("publish", [])])
        for name, item in raw.get("containers", {}).items()
    }
    return Network(raw.get("cidr", DEFAULT_CIDR), raw.get("bridge", DEFAULT_BRIDGE), containers)


def save_state(net: Network, path: str) -> None:
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    raw = {
        "cidr": net.cidr,
        "gateway": net.gateway,
        "bridge": net.bridge,
        "containers": {name: {"ip": c.ip, "publish": [str(p) for p in c.publish]} for name, c in sorted(net.containers.items())},
    }
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(raw, handle, indent=2)
        handle.write("\n")


def used_addresses(net: Network) -> set:
    return {net.gateway} | {c.ip for c in net.containers.values()}


def next_free(net: Network) -> str:
    used = used_addresses(net)
    for host in net.subnet.hosts():
        if str(host) not in used:
            return str(host)
    raise ValueError(f"no free address in {net.cidr}: {len(net.containers)} container(s) hold every host address except the gateway")


def allocate(net: Network, name: str, publish: Sequence[Publish] = ()) -> Container:
    validate_name(name)
    if name in net.containers:
        raise ValueError(f"{name} already has {net.containers[name].ip}; run down {name} first")
    taken = {(p.host_port, p.proto): c.name for c in net.containers.values() for p in c.publish}
    for p in publish:
        if (p.host_port, p.proto) in taken:
            raise ValueError(f"host port {p.host_port}/{p.proto} is already published by {taken[(p.host_port, p.proto)]}")
    container = Container(name, next_free(net), list(publish))
    net.containers[name] = container
    return container


def release(net: Network, name: str) -> Container:
    if name not in net.containers:
        raise KeyError(f"no container named {name!r} in the state file")
    return net.containers.pop(name)


def detect_backend(which: Callable[[str], Optional[str]] = shutil.which) -> str:
    for tool in BACKENDS:
        if which(tool):
            return tool
    raise RuntimeError("neither nft nor iptables is installed; install the nftables package")


def plan_network(net: Network, backend: str) -> List[str]:
    bridge, cidr, gateway = net.bridge, net.cidr, net.gateway
    lines = [
        f"ip link add {bridge} type bridge",
        f"ip addr add {gateway}/{net.prefixlen} dev {bridge}",
        f"ip link set {bridge} up",
        "sysctl -qw net.ipv4.ip_forward=1",
        f"sysctl -qw net.ipv4.conf.{bridge}.route_localnet=1",
    ]
    if backend == "nft":
        lines += [
            f"nft add table ip {TABLE}",
            f"nft 'add chain ip {TABLE} prerouting {{ type nat hook prerouting priority -100; policy accept; }}'",
            f"nft 'add chain ip {TABLE} output {{ type nat hook output priority -100; policy accept; }}'",
            f"nft 'add chain ip {TABLE} postrouting {{ type nat hook postrouting priority 100; policy accept; }}'",
            f"nft add chain ip {TABLE} publish",
            f"nft add rule ip {TABLE} prerouting fib daddr type local jump publish",
            f"nft add rule ip {TABLE} output fib daddr type local jump publish",
            f"nft add rule ip {TABLE} postrouting ip saddr {cidr} oifname != {bridge} masquerade",
            f"nft add rule ip {TABLE} postrouting oifname {bridge} fib saddr type local masquerade",
        ]
    else:
        lines += [
            f"iptables -t nat -N {CHAIN}",
            f"iptables -t nat -A PREROUTING -m addrtype --dst-type LOCAL -j {CHAIN}",
            f"iptables -t nat -A OUTPUT -m addrtype --dst-type LOCAL -j {CHAIN}",
            f"iptables -t nat -A POSTROUTING -s {cidr} ! -o {bridge} -j MASQUERADE",
            f"iptables -t nat -A POSTROUTING -o {bridge} -m addrtype --src-type LOCAL -j MASQUERADE",
        ]
    return lines


def publish_rule(container: Container, p: Publish, backend: str, delete: bool = False) -> str:
    if backend == "nft":
        return f'nft add rule ip {TABLE} publish {p.proto} dport {p.host_port} dnat to {container.ip}:{p.container_port} comment \\"ccnet:{container.name}\\"'
    verb = "-D" if delete else "-A"
    return f"iptables -t nat {verb} {CHAIN} -p {p.proto} --dport {p.host_port} -m comment --comment ccnet:{container.name} -j DNAT --to-destination {container.ip}:{p.container_port}"


def unpublish_by_handle(name: str) -> str:
    return f"nft -a list chain ip {TABLE} publish | awk '/\"ccnet:{name}\"/ {{print $NF}}' | xargs -rn1 nft delete rule ip {TABLE} publish handle"


def plan_container(net: Network, container: Container, backend: str, dns: str = DEFAULT_DNS) -> List[str]:
    name = container.name
    lines = [
        f"ip netns add {name}",
        f"ip link add veth-{name} type veth peer name ceth-{name}",
        f"ip link set ceth-{name} netns {name}",
        f"ip -n {name} link set ceth-{name} name eth0",
        f"ip link set veth-{name} master {net.bridge} up",
        f"ip -n {name} addr add {container.ip}/{net.prefixlen} dev eth0",
        f"ip -n {name} link set lo up",
        f"ip -n {name} link set eth0 up",
        f"ip -n {name} route add default via {net.gateway}",
        f"mkdir -p /etc/netns/{name}",
        f"echo 'nameserver {dns}' > /etc/netns/{name}/resolv.conf",
    ]
    return lines + [publish_rule(container, p, backend) for p in container.publish]


def plan_teardown(container: Container, backend: str) -> List[str]:
    name = container.name
    lines = [f"ip netns pids {name} | xargs -r kill"]
    if container.publish:
        if backend == "nft":
            lines.append(unpublish_by_handle(name))
        else:
            lines += [publish_rule(container, p, backend, delete=True) for p in container.publish]
    return lines + [f"ip netns del {name}", f"rm -rf /etc/netns/{name}"]


def plan_purge(net: Network, backend: str) -> List[str]:
    if backend == "nft":
        lines = [f"nft delete table ip {TABLE}"]
    else:
        lines = [
            f"iptables -t nat -D PREROUTING -m addrtype --dst-type LOCAL -j {CHAIN}",
            f"iptables -t nat -D OUTPUT -m addrtype --dst-type LOCAL -j {CHAIN}",
            f"iptables -t nat -D POSTROUTING -s {net.cidr} ! -o {net.bridge} -j MASQUERADE",
            f"iptables -t nat -D POSTROUTING -o {net.bridge} -m addrtype --src-type LOCAL -j MASQUERADE",
            f"iptables -t nat -F {CHAIN}",
            f"iptables -t nat -X {CHAIN}",
        ]
    return lines + [f"ip link del {net.bridge}"]


@dataclass
class Interface:
    name: str
    ifindex: int
    flags: List[str]
    operstate: str
    link_index: Optional[int] = None
    master: Optional[str] = None
    addresses: List[str] = field(default_factory=list)

    @property
    def up(self) -> bool:
        return "UP" in self.flags

    def has_address(self, ip: str) -> bool:
        return any(a.split("/")[0] == ip for a in self.addresses)


def parse_interfaces(text: str) -> Dict[str, Interface]:
    result: Dict[str, Interface] = {}
    for item in json.loads(text or "[]"):
        name = str(item.get("ifname", "?")).split("@")[0]
        addresses = [f"{a['local']}/{a['prefixlen']}" for a in item.get("addr_info", []) if a.get("family") == "inet"]
        result[name] = Interface(
            name,
            int(item.get("ifindex", 0)),
            list(item.get("flags", [])),
            str(item.get("operstate", "UNKNOWN")),
            item.get("link_index"),
            item.get("master"),
            addresses,
        )
    return result


@dataclass
class Route:
    dst: str
    dev: str
    gateway: Optional[str] = None


def parse_routes(text: str) -> List[Route]:
    return [Route(str(r.get("dst", "?")), str(r.get("dev", "?")), r.get("gateway")) for r in json.loads(text or "[]")]


@dataclass
class NatRule:
    kind: str
    chain: str
    proto: Optional[str] = None
    dport: Optional[int] = None
    to_ip: Optional[str] = None
    to_port: Optional[int] = None
    source: Optional[str] = None
    out_iface: Optional[str] = None
    out_negated: bool = False
    comment: Optional[str] = None

    def covers_source(self, ip: str) -> bool:
        if self.source is None:
            return True
        return ipaddress.IPv4Address(ip) in ipaddress.IPv4Network(self.source, strict=False)


def prefix_text(right: object) -> Optional[str]:
    if isinstance(right, dict) and "prefix" in right:
        return f"{right['prefix']['addr']}/{right['prefix']['len']}"
    if isinstance(right, str):
        return right
    return None


def parse_nft_ruleset(text: str) -> List[NatRule]:
    rules: List[NatRule] = []
    if not text:
        return rules
    for entry in json.loads(text).get("nftables", []):
        rule = entry.get("rule")
        if not rule:
            continue
        found = NatRule("", f"{rule.get('table', '?')}/{rule.get('chain', '?')}", comment=rule.get("comment"))
        for expr in rule.get("expr", []):
            if "match" in expr:
                left, right, op = expr["match"].get("left", {}), expr["match"].get("right"), expr["match"].get("op", "==")
                payload = left.get("payload", {}) if isinstance(left, dict) else {}
                meta = left.get("meta", {}) if isinstance(left, dict) else {}
                if payload.get("field") == "dport" and isinstance(right, int):
                    found.proto, found.dport = payload.get("protocol"), right
                elif payload.get("field") == "saddr":
                    found.source = prefix_text(right)
                elif meta.get("key") == "oifname" and isinstance(right, str):
                    found.out_iface, found.out_negated = right, op == "!="
            elif "dnat" in expr:
                target = expr["dnat"] or {}
                found.kind, found.to_ip, found.to_port = "dnat", str(target.get("addr")), target.get("port")
            elif "masquerade" in expr:
                found.kind = "masquerade"
            elif "snat" in expr:
                target = expr["snat"] or {}
                found.kind, found.to_ip = "snat", str(target.get("addr"))
        if found.kind:
            rules.append(found)
    return rules


def parse_iptables_save(text: str) -> List[NatRule]:
    rules: List[NatRule] = []
    for line in (text or "").splitlines():
        if not line.startswith("-A "):
            continue
        words = shlex.split(line)
        found = NatRule("", f"nat/{words[1]}")
        negate = False
        index = 2
        while index < len(words):
            word = words[index]
            value = words[index + 1] if index + 1 < len(words) else None
            if word == "!":
                negate = True
                index += 1
                continue
            if word == "-s":
                found.source = value
            elif word == "-p":
                found.proto = value
            elif word == "--dport" and value and value.isdigit():
                found.dport = int(value)
            elif word == "-o":
                found.out_iface, found.out_negated = value, negate
            elif word == "--comment":
                found.comment = value
            elif word == "-j" and value in NAT_TARGETS:
                found.kind = NAT_TARGETS[value]
            elif word in ("--to-destination", "--to-source") and value:
                ip, _, port = value.partition(":")
                found.to_ip = ip
                found.to_port = int(port.split("-")[0]) if port else None
            negate = False
            index += 1 if word in IPTABLES_FLAGS_WITHOUT_VALUE else 2
        if found.kind:
            rules.append(found)
    return rules


@dataclass
class Expectation:
    name: str
    ip: str
    gateway: str
    bridge: str
    publish: List[Publish] = field(default_factory=list)


@dataclass
class Check:
    ok: bool
    text: str


def explain(exp: Expectation, ns_ifaces: Dict[str, Interface], ns_routes: List[Route], host_ifaces: Dict[str, Interface], nat: List[NatRule]) -> List[Check]:
    checks: List[Check] = []
    eth0 = ns_ifaces.get("eth0")
    if eth0 is None:
        checks.append(Check(False, "eth0: missing inside the namespace (no veth end was moved in, or it was not renamed)"))
    else:
        checks.append(Check(eth0.has_address(exp.ip), f"eth0 address: {', '.join(eth0.addresses) or 'none'} (expected {exp.ip})"))
        checks.append(Check(eth0.up, f"eth0 state: {'UP' if eth0.up else 'DOWN'}"))
    lo = ns_ifaces.get("lo")
    lo_up = bool(lo and lo.up)
    checks.append(Check(lo_up, f"lo state: {'UP' if lo_up else 'DOWN'} (localhost inside the container)"))
    default = next((r for r in ns_routes if r.dst == "default"), None)
    if default is None:
        checks.append(Check(False, f"default route: missing (expected via {exp.gateway})"))
    else:
        checks.append(Check(default.gateway == exp.gateway, f"default route: via {default.gateway} dev {default.dev} (expected {exp.gateway})"))
    peer = None
    if eth0 is not None and eth0.link_index is not None:
        peer = next((i for i in host_ifaces.values() if i.ifindex == eth0.link_index), None)
    if peer is None:
        checks.append(Check(False, "veth peer: not found on the host (eth0 is not one end of a veth pair, or the other end is in another namespace)"))
    else:
        attached = peer.master == exp.bridge and peer.up
        checks.append(Check(attached, f"veth peer: {peer.name} (ifindex {peer.ifindex}) master {peer.master or 'none'} {'UP' if peer.up else 'DOWN'} (expected bridge {exp.bridge})"))
    bridge = host_ifaces.get(exp.bridge)
    bridge_ok = bool(bridge and bridge.up and bridge.has_address(exp.gateway))
    bridge_text = f"{', '.join(bridge.addresses) or 'no address'} {'UP' if bridge.up else 'DOWN'}" if bridge else "missing"
    checks.append(Check(bridge_ok, f"bridge {exp.bridge}: {bridge_text} (expected {exp.gateway}, UP)"))
    masq = [r for r in nat if r.kind == "masquerade" and r.covers_source(exp.ip) and not (r.out_iface == exp.bridge and not r.out_negated)]
    if masq:
        rule = masq[0]
        checks.append(Check(True, f"masquerade: source {rule.source or 'any'} leaving via {'any interface but ' + rule.out_iface if rule.out_iface else 'any interface'} ({rule.chain})"))
    else:
        checks.append(Check(False, f"masquerade: no rule covers {exp.ip} leaving the host; replies to egress traffic would never come back"))
    for p in exp.publish:
        same_port = [r for r in nat if r.kind == "dnat" and r.dport == p.host_port and (r.proto or "tcp") == p.proto]
        hit = next((r for r in same_port if r.to_ip == exp.ip and r.to_port == p.container_port), None)
        if hit:
            checks.append(Check(True, f"published {p.host_port}/{p.proto}: DNAT to {hit.to_ip}:{hit.to_port} ({hit.chain})"))
        elif same_port:
            other = same_port[0]
            checks.append(Check(False, f"published {p.host_port}/{p.proto}: DNAT goes to {other.to_ip}:{other.to_port} in {other.chain}, not to {exp.ip}:{p.container_port}"))
        else:
            checks.append(Check(False, f"published {p.host_port}/{p.proto}: no DNAT rule to {exp.ip}:{p.container_port}; the host port is not forwarded"))
    return checks


def render_explain(exp: Expectation, checks: List[Check]) -> str:
    lines = [f"{exp.name} ({exp.ip} on {exp.bridge}, gateway {exp.gateway})"]
    for check in checks:
        lines.append(f"  {'ok  ' if check.ok else 'FAIL'}  {check.text}")
    failed = [c for c in checks if not c.ok]
    if failed:
        lines.append(f"{exp.name}: {len(failed)} of {len(checks)} checks failed; fix the first FAIL line, then run explain again")
    else:
        published = ", ".join(f"{p.host_port}->{p.container_port}/{p.proto}" for p in exp.publish) or "nothing published"
        lines.append(f"{exp.name}: all {len(checks)} checks passed; reachable at {exp.ip}, {published}")
    return "\n".join(lines)


def run_command(argv: Sequence[str]) -> str:
    return subprocess.run(list(argv), check=True, capture_output=True, text=True).stdout


def gather_live(name: str, backend: str, run: Callable[[Sequence[str]], str] = run_command) -> Dict[str, str]:
    data = {
        "addr.json": run(["ip", "-n", name, "-j", "addr"]),
        "route.json": run(["ip", "-n", name, "-j", "route"]),
        "host.json": run(["ip", "-j", "addr"]),
    }
    if backend == "nft":
        data["nat.json"] = run(["nft", "-j", "list", "ruleset"])
    else:
        data["nat.txt"] = run(["iptables-save", "-t", "nat"])
    return data


def gather_dir(directory: str) -> Dict[str, str]:
    data: Dict[str, str] = {}
    for filename in FIXTURE_FILES:
        path = os.path.join(directory, filename)
        if os.path.exists(path):
            with open(path, encoding="utf-8") as handle:
                data[filename] = handle.read()
    missing = [f for f in ("addr.json", "route.json", "host.json") if f not in data]
    if missing or not ("nat.json" in data or "nat.txt" in data):
        raise FileNotFoundError(f"{directory} needs addr.json, route.json, host.json and nat.json or nat.txt (missing: {', '.join(missing) or 'the nat dump'})")
    return data


def explain_from_data(exp: Expectation, data: Dict[str, str]) -> List[Check]:
    nat = parse_nft_ruleset(data.get("nat.json", "")) + parse_iptables_save(data.get("nat.txt", ""))
    return explain(exp, parse_interfaces(data["addr.json"]), parse_routes(data["route.json"]), parse_interfaces(data["host.json"]), nat)


def render_ls(net: Network) -> str:
    if not net.containers:
        return f"no containers on {net.bridge} ({net.cidr}, gateway {net.gateway})"
    lines = [f"{'NAME':<12} {'ADDRESS':<18} {'VETH':<16} PUBLISHED"]
    for c in sorted(net.containers.values(), key=lambda c: ipaddress.IPv4Address(c.ip)):
        lines.append(f"{c.name:<12} {c.ip + '/' + str(net.prefixlen):<18} {c.veth:<16} {', '.join(str(p) for p in c.publish) or '-'}")
    return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="ccnet", description=__doc__)
    parser.add_argument("--state", default=default_state_path(), help="state file (default $CCNET_STATE/state.json or /var/lib/ccnet/state.json)")
    sub = parser.add_subparsers(dest="command", required=True)

    alloc = sub.add_parser("allocate", help="reserve the next free address for NAME and record its published ports")
    alloc.add_argument("name")
    alloc.add_argument("--publish", "-p", action="append", default=[], metavar="HOST:CONTAINER[/PROTO]")
    alloc.add_argument("--json", action="store_true")

    rel = sub.add_parser("release", help="give NAME's address back")
    rel.add_argument("name")

    ls = sub.add_parser("ls", help="list allocated containers")
    ls.add_argument("--json", action="store_true")
    ls.add_argument("--names", action="store_true", help="names only, one per line")

    plan = sub.add_parser("plan", help="print the commands for a step without running anything")
    plan.add_argument("step", choices=["network", "up", "down", "purge"])
    plan.add_argument("name", nargs="?")
    plan.add_argument("--ip", help="address to use for up (default: the next free one, or the recorded one)")
    plan.add_argument("--publish", "-p", action="append", default=[], metavar="HOST:CONTAINER[/PROTO]")
    plan.add_argument("--backend", choices=BACKENDS, help="default: nft if installed, else iptables")
    plan.add_argument("--dns", default=DEFAULT_DNS)

    exp = sub.add_parser("explain", help="verify a container's plumbing: address, route, veth peer, bridge, masquerade, DNAT")
    exp.add_argument("name")
    exp.add_argument("--from-dir", help="directory with addr.json, route.json, host.json and nat.json/nat.txt instead of live commands")
    exp.add_argument("--ip")
    exp.add_argument("--gateway")
    exp.add_argument("--bridge")
    exp.add_argument("--publish", "-p", action="append", default=[], metavar="HOST:CONTAINER[/PROTO]")
    exp.add_argument("--backend", choices=BACKENDS)
    exp.add_argument("--json", action="store_true")
    return parser


def cmd_allocate(args: argparse.Namespace) -> int:
    net = load_state(args.state)
    container = allocate(net, args.name, [parse_publish(p) for p in args.publish])
    save_state(net, args.state)
    if args.json:
        print(json.dumps({"name": container.name, "ip": container.ip, "prefixlen": net.prefixlen, "gateway": net.gateway, "bridge": net.bridge, "publish": [str(p) for p in container.publish]}))
    else:
        print(container.ip)
    return 0


def cmd_release(args: argparse.Namespace) -> int:
    net = load_state(args.state)
    container = release(net, args.name)
    save_state(net, args.state)
    print(f"released {container.ip} from {container.name}")
    return 0


def cmd_ls(args: argparse.Namespace) -> int:
    net = load_state(args.state)
    if args.names:
        print("\n".join(sorted(net.containers)))
    elif args.json:
        print(json.dumps({"cidr": net.cidr, "gateway": net.gateway, "bridge": net.bridge, "containers": {n: {"ip": c.ip, "veth": c.veth, "publish": [str(p) for p in c.publish]} for n, c in sorted(net.containers.items())}}, indent=2))
    else:
        print(render_ls(net))
    return 0


def cmd_plan(args: argparse.Namespace) -> int:
    net = load_state(args.state)
    backend = args.backend or detect_backend()
    if args.step == "network":
        lines = plan_network(net, backend)
    elif args.step == "purge":
        lines = plan_purge(net, backend)
    else:
        if not args.name:
            raise ValueError(f"plan {args.step} needs a container name")
        recorded = net.containers.get(args.name)
        publish = [parse_publish(p) for p in args.publish] or (recorded.publish if recorded else [])
        ip = args.ip or (recorded.ip if recorded else next_free(net))
        container = Container(validate_name(args.name), ip, publish)
        lines = plan_container(net, container, backend, args.dns) if args.step == "up" else plan_teardown(container, backend)
    print("\n".join(lines))
    return 0


def cmd_explain(args: argparse.Namespace) -> int:
    net = load_state(args.state)
    recorded = net.containers.get(args.name)
    ip = args.ip or (recorded.ip if recorded else None)
    if ip is None:
        raise ValueError(f"{args.name} is not in {args.state}; pass --ip (and --gateway/--bridge/--publish) to verify a container ccnet did not create")
    publish = [parse_publish(p) for p in args.publish] or (recorded.publish if recorded else [])
    exp = Expectation(args.name, ip, args.gateway or net.gateway, args.bridge or net.bridge, publish)
    if args.from_dir:
        data = gather_dir(args.from_dir)
    else:
        if sys.platform != "linux":
            print("ccnet explain: live checks need Linux; capture ip -n NAME -j addr/route, ip -j addr and nft -j list ruleset (or iptables-save -t nat) into a directory and pass --from-dir", file=sys.stderr)
            return 2
        data = gather_live(args.name, args.backend or detect_backend())
    checks = explain_from_data(exp, data)
    if args.json:
        print(json.dumps({"name": exp.name, "ip": exp.ip, "gateway": exp.gateway, "bridge": exp.bridge, "ok": all(c.ok for c in checks), "checks": [{"ok": c.ok, "text": c.text} for c in checks]}, indent=2))
    else:
        print(render_explain(exp, checks))
    return 0 if all(c.ok for c in checks) else 1


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(list(sys.argv[1:] if argv is None else argv))
    handlers = {"allocate": cmd_allocate, "release": cmd_release, "ls": cmd_ls, "plan": cmd_plan, "explain": cmd_explain}
    try:
        return handlers[args.command](args)
    except (ValueError, KeyError, FileNotFoundError, RuntimeError) as exc:
        print(f"ccnet: {exc.args[0] if exc.args else exc}", file=sys.stderr)
        return 1
    except subprocess.CalledProcessError as exc:
        print(f"ccnet: {' '.join(exc.cmd)} failed: {(exc.stderr or '').strip() or exc.returncode}; are you root, and does the namespace exist?", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
