"""routelens: CIDR arithmetic for IPv4 and IPv6, parsers for ip -j addr/route/rule/neigh, and a from-scratch ip route get: policy rules in priority order, longest-prefix match, metrics, source selection, ECMP, forwarding, rp_filter and a neighbour check."""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass, field
from typing import Callable, Dict, Iterable, List, Optional, Sequence, Tuple

BITS = {4: 32, 6: 128}
SCOPES = {"global": 0, "universe": 0, "site": 200, "link": 253, "host": 254, "nowhere": 255}
TABLE_NAMES = {"253": "default", "254": "main", "255": "local"}
ERRORS = {
    "blackhole": ("EINVAL", "Invalid argument"),
    "unreachable": ("EHOSTUNREACH", "No route to host"),
    "prohibit": ("EACCES", "Permission denied"),
    "noroute": ("ENETUNREACH", "Network is unreachable"),
    "noforward": ("EHOSTUNREACH", "No route to host"),
    "rpfilter": ("EXDEV", "Invalid cross-device link"),
    "martian": ("EINVAL", "Invalid argument"),
    "nodev": ("ENODEV", "No such device"),
}
DEFAULT_RULES = {4: [(0, "local"), (32766, "main"), (32767, "default")], 6: [(0, "local"), (32766, "main")]}
NEIGH_BAD = {"FAILED", "INCOMPLETE"}
UNMODELLED_RULE_KEYS = ("ipproto", "sport", "dport", "uidrange", "l3mdev", "tun_id", "tos", "dscp", "suppress_ifgroup")
SNAPSHOT_COMMANDS = {
    "addr.json": ["-j", "addr"],
    "route.json": ["-4", "-j", "route", "show", "table", "all"],
    "route6.json": ["-6", "-j", "route", "show", "table", "all"],
    "rule.json": ["-4", "-j", "rule"],
    "rule6.json": ["-6", "-j", "rule"],
    "neigh.json": ["-j", "neigh"],
}
REQUIRED_FILES = ("addr.json", "route.json")
OCTET = re.compile(r"[0-9]{1,3}")
HEXTET = re.compile(r"[0-9a-fA-F]{1,4}")


class InputError(ValueError):
    pass


def parse_ipv4(text: str) -> int:
    parts = text.split(".")
    if len(parts) != 4:
        raise InputError(f"not an IPv4 address: {text!r} (four decimal octets, e.g. 10.0.1.10)")
    value = 0
    for part in parts:
        if not OCTET.fullmatch(part) or int(part) > 255 or (len(part) > 1 and part[0] == "0"):
            raise InputError(f"not an IPv4 address: {text!r} (octet {part!r}; 0-255, no leading zeros, which inet_aton would read as octal)")
        value = value << 8 | int(part)
    return value


def hextets(parts: List[str], allow_v4_tail: bool, text: str) -> List[int]:
    out: List[int] = []
    for index, part in enumerate(parts):
        if "." in part and allow_v4_tail and index == len(parts) - 1:
            v4 = parse_ipv4(part)
            out += [v4 >> 16, v4 & 0xFFFF]
        elif HEXTET.fullmatch(part):
            out.append(int(part, 16))
        else:
            raise InputError(f"not an IPv6 address: {text!r} (group {part!r})")
    return out


def parse_ipv6(text: str) -> int:
    raw = text.split("%", 1)[0]
    if raw.count("::") > 1:
        raise InputError(f"not an IPv6 address: {text!r} (only one '::' is allowed)")
    if "::" in raw:
        head, tail = raw.split("::")
        left = hextets(head.split(":") if head else [], False, text)
        right = hextets(tail.split(":") if tail else [], True, text)
        if len(left) + len(right) > 7:
            raise InputError(f"not an IPv6 address: {text!r} ('::' must stand for at least one zero group)")
        groups = left + [0] * (8 - len(left) - len(right)) + right
    else:
        groups = hextets(raw.split(":"), True, text)
        if len(groups) != 8:
            raise InputError(f"not an IPv6 address: {text!r} (eight groups, or '::' for a run of zeros)")
    value = 0
    for group in groups:
        value = value << 16 | group
    return value


def parse_ip(text: str) -> Tuple[int, int]:
    text = str(text).strip()
    if ":" in text:
        return 6, parse_ipv6(text)
    return 4, parse_ipv4(text)


def format_ip(version: int, value: int) -> str:
    if version == 4:
        return ".".join(str(value >> shift & 0xFF) for shift in (24, 16, 8, 0))
    groups = [value >> (112 - 16 * i) & 0xFFFF for i in range(8)]
    best_start, best_len, i = -1, 0, 0
    while i < 8:
        if groups[i]:
            i += 1
            continue
        j = i
        while j < 8 and groups[j] == 0:
            j += 1
        if j - i > best_len:
            best_start, best_len = i, j - i
        i = j
    words = [format(g, "x") for g in groups]
    if best_len < 2:
        return ":".join(words)
    return ":".join(words[:best_start]) + "::" + ":".join(words[best_start + best_len:])


def mask_for(version: int, length: int) -> int:
    bits = BITS[version]
    return ((1 << bits) - 1) ^ ((1 << (bits - length)) - 1)


@dataclass(frozen=True)
class Prefix:
    version: int
    network: int
    length: int

    @property
    def bits(self) -> int:
        return BITS[self.version]

    @property
    def mask(self) -> int:
        return mask_for(self.version, self.length)

    @property
    def hostmask(self) -> int:
        return (1 << (self.bits - self.length)) - 1

    @property
    def last(self) -> int:
        return self.network | self.hostmask

    @property
    def size(self) -> int:
        return 1 << (self.bits - self.length)

    @property
    def broadcast(self) -> Optional[int]:
        return self.last if self.version == 4 and self.length <= 30 else None

    @property
    def usable(self) -> Tuple[int, int, int]:
        if self.version == 6 or self.length >= 31:
            return self.network, self.last, self.size
        return self.network + 1, self.last - 1, self.size - 2

    def contains(self, version: int, address: int) -> bool:
        return version == self.version and address & self.mask == self.network

    def covers(self, other: "Prefix") -> bool:
        return other.version == self.version and other.length >= self.length and other.network & self.mask == self.network

    def overlaps(self, other: "Prefix") -> bool:
        return self.covers(other) or other.covers(self)

    def __str__(self) -> str:
        return f"{format_ip(self.version, self.network)}/{self.length}"


def parse_prefix(text: str, version: Optional[int] = None, strict: bool = False) -> Prefix:
    raw = str(text).strip()
    if raw in ("default", "all", "any"):
        return Prefix(version or 4, 0, 0)
    address, sep, length_text = raw.partition("/")
    family, value = parse_ip(address)
    if version and family != version:
        raise InputError(f"{raw!r} is IPv{family}, expected IPv{version}")
    length = BITS[family]
    if sep:
        if not OCTET.fullmatch(length_text) or int(length_text) > BITS[family]:
            raise InputError(f"bad prefix length in {raw!r} (0-{BITS[family]})")
        length = int(length_text)
    prefix = Prefix(family, value & mask_for(family, length), length)
    if strict and prefix.network != value:
        raise InputError(f"{raw!r} has host bits set; the network is {prefix}")
    return prefix


def short(prefix: Prefix) -> str:
    return format_ip(prefix.version, prefix.network) if prefix.length == prefix.bits else str(prefix)


def show_prefix(prefix: Prefix) -> str:
    return "default" if prefix.length == 0 else short(prefix)


def range_to_prefixes(version: int, first: int, last: int) -> List[Prefix]:
    bits = BITS[version]
    out: List[Prefix] = []
    while first <= last:
        length = bits
        while length > 0:
            size = 1 << (bits - length + 1)
            if first & (size - 1) or first + size - 1 > last:
                break
            length -= 1
        out.append(Prefix(version, first, length))
        first += 1 << (bits - length)
    return out


def collapse(prefixes: Iterable[Prefix]) -> List[Prefix]:
    items = list(prefixes)
    out: List[Prefix] = []
    for version in (4, 6):
        spans = sorted((p.network, p.last) for p in items if p.version == version)
        merged: List[List[int]] = []
        for first, last in spans:
            if merged and first <= merged[-1][1] + 1:
                merged[-1][1] = max(merged[-1][1], last)
            else:
                merged.append([first, last])
        for first, last in merged:
            out += range_to_prefixes(version, first, last)
    return out


def supernet(prefixes: Sequence[Prefix]) -> Prefix:
    versions = {p.version for p in prefixes}
    if len(versions) != 1:
        raise InputError("summarise one address family at a time")
    version = versions.pop()
    first = min(p.network for p in prefixes)
    last = max(p.last for p in prefixes)
    length = BITS[version] - (first ^ last).bit_length()
    return Prefix(version, first & mask_for(version, length), length)


def load_json_list(text: str, what: str) -> List[dict]:
    if not (text or "").strip():
        return []
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise InputError(f"{what}: not JSON ({exc.msg} at line {exc.lineno}); capture it with ip -j") from None
    if not isinstance(data, list):
        raise InputError(f"{what}: expected a JSON list, as ip -j prints")
    return [item for item in data if isinstance(item, dict)]


def normalise_table(value: object) -> str:
    return TABLE_NAMES.get(str(value), str(value))


def parse_int(value: object) -> int:
    return value if isinstance(value, int) else int(str(value), 0)


@dataclass
class Address:
    dev: str
    version: int
    value: int
    prefix: Prefix
    scope: str = "global"
    secondary: bool = False
    tentative: bool = False
    deprecated: bool = False

    @property
    def text(self) -> str:
        return format_ip(self.version, self.value)


@dataclass
class Interface:
    name: str
    ifindex: int
    flags: List[str]
    addresses: List[Address] = field(default_factory=list)

    @property
    def up(self) -> bool:
        return "UP" in self.flags


def parse_addr(text: str) -> List[Interface]:
    interfaces: List[Interface] = []
    for item in load_json_list(text, "ip -j addr"):
        name = str(item.get("ifname", "?")).split("@")[0]
        addresses: List[Address] = []
        for info in item.get("addr_info") or []:
            if info.get("family") not in ("inet", "inet6") or "local" not in info:
                continue
            version, value = parse_ip(str(info["local"]))
            length = int(info.get("prefixlen", BITS[version]))
            addresses.append(Address(name, version, value, Prefix(version, value & mask_for(version, length), length), str(info.get("scope", "global")),
                                     bool(info.get("secondary")), bool(info.get("tentative") or info.get("dadfailed")), bool(info.get("deprecated"))))
        interfaces.append(Interface(name, int(item.get("ifindex", 0)), [str(f) for f in item.get("flags") or []], addresses))
    return sorted(interfaces, key=lambda i: i.ifindex)


@dataclass
class NextHop:
    gateway: Optional[str] = None
    dev: Optional[str] = None
    weight: int = 1
    dead: bool = False


@dataclass
class Route:
    prefix: Prefix
    table: str = "main"
    type: str = "unicast"
    metric: int = 0
    scope: str = "global"
    protocol: str = ""
    prefsrc: Optional[str] = None
    nexthops: List[NextHop] = field(default_factory=list)
    order: int = 0

    @property
    def scope_value(self) -> int:
        return SCOPES.get(self.scope, 0)

    def describe(self) -> str:
        words = [] if self.type == "unicast" else [self.type]
        words.append(show_prefix(self.prefix))
        if len(self.nexthops) == 1:
            hop = self.nexthops[0]
            words += (["via", hop.gateway] if hop.gateway else []) + (["dev", hop.dev] if hop.dev else [])
        for hop in self.nexthops if len(self.nexthops) > 1 else []:
            words += ["nexthop", "via", hop.gateway or "-", "dev", hop.dev or "-", "weight", str(hop.weight)]
        if self.table != "main":
            words += ["table", self.table]
        if self.scope not in ("global", "universe"):
            words += ["scope", self.scope]
        if self.prefsrc:
            words += ["src", self.prefsrc]
        if self.metric:
            words += ["metric", str(self.metric)]
        return " ".join(words)


def route_version(item: dict, forced: Optional[int]) -> int:
    if forced:
        return forced
    probes = [item.get("dst"), item.get("gateway"), item.get("prefsrc")] + [h.get("gateway") for h in item.get("nexthops") or []]
    return 6 if "pref" in item or any(isinstance(p, str) and ":" in p for p in probes) else 4


def parse_routes(text: str, version: Optional[int] = None) -> List[Route]:
    routes: List[Route] = []
    for order, item in enumerate(load_json_list(text, "ip -j route show table all")):
        family = route_version(item, version)
        hops = [NextHop(h.get("gateway"), h.get("dev"), int(h.get("weight", 1)), "dead" in (h.get("flags") or [])) for h in item.get("nexthops") or []]
        if not hops and (item.get("gateway") or item.get("dev")):
            hops = [NextHop(item.get("gateway"), item.get("dev"), 1, "dead" in (item.get("flags") or []))]
        routes.append(Route(parse_prefix(str(item.get("dst", "default")), family), normalise_table(item.get("table", "main")), str(item.get("type", "unicast")),
                            int(item.get("metric", 0)), str(item.get("scope", "global")), str(item.get("protocol", "")), item.get("prefsrc"), hops, order))
    return routes


@dataclass
class Rule:
    priority: int
    version: int = 4
    src: Optional[Prefix] = None
    dst: Optional[Prefix] = None
    iif: Optional[str] = None
    oif: Optional[str] = None
    fwmark: Optional[int] = None
    fwmask: int = 0xFFFFFFFF
    table: Optional[str] = None
    action: str = "lookup"
    invert: bool = False
    suppress_prefixlength: Optional[int] = None
    goto: Optional[int] = None
    unmodelled: List[str] = field(default_factory=list)

    def describe(self) -> str:
        words = [f"{self.priority}:"] + (["not"] if self.invert else []) + ["from", short(self.src) if self.src else "all"]
        if self.dst:
            words += ["to", short(self.dst)]
        if self.fwmark is not None:
            words += ["fwmark", hex(self.fwmark) + ("" if self.fwmask == 0xFFFFFFFF else f"/{hex(self.fwmask)}")]
        words += (["iif", self.iif] if self.iif else []) + (["oif", self.oif] if self.oif else [])
        if self.action == "lookup":
            words += ["lookup", self.table or "?"]
        elif self.action == "goto":
            words += ["goto", str(self.goto)]
        else:
            words.append(self.action)
        if self.suppress_prefixlength is not None:
            words += ["suppress_prefixlength", str(self.suppress_prefixlength)]
        return " ".join(words)


def parse_rules(text: str, version: Optional[int] = None) -> List[Rule]:
    rules: List[Rule] = []
    for item in load_json_list(text, "ip -j rule"):
        family = version or (6 if any(isinstance(item.get(k), str) and ":" in item[k] for k in ("src", "dst")) else 4)
        rule = Rule(int(item.get("priority", 0)), family)
        for key, length_key, attr in (("src", "srclen", "src"), ("dst", "dstlen", "dst")):
            value = item.get(key)
            if value and value != "all":
                setattr(rule, attr, parse_prefix(f"{value}/{item.get(length_key, BITS[family])}", family))
        rule.iif, rule.oif = item.get("iif"), item.get("oif")
        if "fwmark" in item:
            rule.fwmark = parse_int(item["fwmark"])
            rule.fwmask = parse_int(item.get("fwmask", 0xFFFFFFFF))
        if "table" in item:
            rule.table = normalise_table(item["table"])
        if item.get("action") in ("blackhole", "unreachable", "prohibit"):
            rule.action = item["action"]
        elif "goto" in item:
            rule.action, rule.goto = "goto", int(item["goto"])
        elif "nop" in item:
            rule.action = "nop"
        rule.invert = "not" in item
        if "suppress_prefixlength" in item:
            rule.suppress_prefixlength = int(item["suppress_prefixlength"])
        rule.unmodelled = [key for key in UNMODELLED_RULE_KEYS if key in item]
        rules.append(rule)
    return sorted(rules, key=lambda r: r.priority)


@dataclass
class Neighbour:
    address: str
    dev: str
    state: List[str]
    lladdr: Optional[str] = None


def parse_neigh(text: str) -> List[Neighbour]:
    out: List[Neighbour] = []
    for item in load_json_list(text, "ip -j neigh"):
        state = item.get("state") or []
        out.append(Neighbour(str(item.get("dst")), str(item.get("dev", "?")), [str(s) for s in ([state] if isinstance(state, str) else state)], item.get("lladdr")))
    return out


def parse_sysctl(text: str) -> Dict[str, int]:
    values: Dict[str, int] = {}
    for line in (text or "").splitlines():
        key, sep, value = line.partition("=")
        if sep and re.fullmatch(r"-?[0-9]+", value.strip()):
            values[key.strip()] = int(value.strip())
    return values


@dataclass
class Snapshot:
    interfaces: List[Interface]
    routes: List[Route]
    rules: Dict[int, List[Rule]]
    neighbours: List[Neighbour] = field(default_factory=list)
    sysctl: Dict[str, int] = field(default_factory=dict)

    def interface(self, name: Optional[str]) -> Optional[Interface]:
        return next((i for i in self.interfaces if i.name == name), None)

    def table(self, name: Optional[str], version: int) -> List[Route]:
        return [r for r in self.routes if r.table == name and r.prefix.version == version]

    def rules_for(self, version: int) -> List[Rule]:
        return self.rules.get(version) or [Rule(p, version, table=t) for p, t in DEFAULT_RULES[version]]

    def is_local(self, version: int, value: int) -> bool:
        return any(a.version == version and a.value == value for i in self.interfaces for a in i.addresses)

    def sysctl_value(self, key: str) -> Optional[int]:
        if key in self.sysctl:
            return self.sysctl[key]
        parts = key.split(".")
        if len(parts) > 5 and parts[2] == "conf":
            alt = ".".join(parts[:3] + [".".join(parts[3:-1]).replace(".", "/"), parts[-1]])
            return self.sysctl.get(alt)
        return None


def build_snapshot(files: Dict[str, str]) -> Snapshot:
    missing = [name for name in REQUIRED_FILES if name not in files]
    if missing:
        raise InputError(f"snapshot needs {', '.join(missing)}: ip -j addr > addr.json; ip -4 -j route show table all > route.json (routelens snapshot DIR writes them all)")
    routes = parse_routes(files["route.json"], 4) + parse_routes(files.get("route6.json", ""), 6)
    rules = {4: parse_rules(files.get("rule.json", ""), 4), 6: parse_rules(files.get("rule6.json", ""), 6)}
    return Snapshot(parse_addr(files["addr.json"]), routes, rules, parse_neigh(files.get("neigh.json", "")), parse_sysctl(files.get("sysctl.txt", "")))


def read_dir(directory: str) -> Dict[str, str]:
    if not os.path.isdir(directory):
        raise InputError(f"{directory} is not a directory; create one with routelens snapshot DIR")
    files: Dict[str, str] = {}
    for name in list(SNAPSHOT_COMMANDS) + ["sysctl.txt"]:
        path = os.path.join(directory, name)
        if os.path.exists(path):
            with open(path, encoding="utf-8") as handle:
                files[name] = handle.read()
    return files


def run_command(argv: Sequence[str]) -> str:
    return subprocess.run(list(argv), check=True, capture_output=True, text=True).stdout


def sysctl_paths(names: Iterable[str]) -> Dict[str, str]:
    paths = {"/proc/sys/net/ipv4/ip_forward": "net.ipv4.ip_forward"}
    for name in ["all", "default"] + list(names):
        for knob in ("forwarding", "rp_filter"):
            paths[f"/proc/sys/net/ipv4/conf/{name}/{knob}"] = f"net.ipv4.conf.{name}.{knob}"
    return paths


def gather(netns: Optional[str] = None, run: Callable[[Sequence[str]], str] = run_command) -> Dict[str, str]:
    prefix = ["ip"] + (["-n", netns] if netns else [])
    files: Dict[str, str] = {}
    for name, args in SNAPSHOT_COMMANDS.items():
        try:
            files[name] = run(prefix + args)
        except subprocess.CalledProcessError:
            if name in REQUIRED_FILES:
                raise
            files[name] = ""
    paths = sysctl_paths(i.name for i in parse_addr(files["addr.json"]))
    reader = ["sh", "-c", 'for f; do printf "%s = %s\\n" "$f" "$(cat "$f" 2>/dev/null)"; done', "sh"] + list(paths)
    raw = run((["ip", "netns", "exec", netns] if netns else []) + reader)
    lines = []
    for line in raw.splitlines():
        path, _, value = line.partition(" = ")
        if path in paths and value.strip():
            lines.append(f"{paths[path]} = {value.strip()}")
    files["sysctl.txt"] = "\n".join(lines) + "\n"
    return files


@dataclass
class Query:
    dst: str
    src: Optional[str] = None
    iif: Optional[str] = None
    mark: int = 0


@dataclass
class Flow:
    version: int
    dst: int
    src: Optional[int]
    iif: str = "lo"
    mark: int = 0
    arriving: bool = False

    @property
    def dst_text(self) -> str:
        return format_ip(self.version, self.dst)

    @property
    def src_text(self) -> str:
        return format_ip(self.version, self.src) if self.src is not None else f"unset ({format_ip(self.version, 0)})"


@dataclass
class Walk:
    rule: Optional[Rule] = None
    route: Optional[Route] = None
    error: Optional[str] = None
    detail: str = ""
    trace: List[str] = field(default_factory=list)


@dataclass
class Hop:
    gateway: Optional[str]
    dev: Optional[str]
    weight: int = 1
    src: Optional[str] = None


@dataclass
class Decision:
    query: Query
    ok: bool = False
    type: str = "unicast"
    dst: str = ""
    table: Optional[str] = None
    route: Optional[Route] = None
    hops: List[Hop] = field(default_factory=list)
    src: Optional[str] = None
    errno: Optional[str] = None
    message: Optional[str] = None
    reason: str = ""
    trace: List[str] = field(default_factory=list)
    neighbours: List[str] = field(default_factory=list)
    neighbour_ok: Optional[bool] = None


def rule_matches(rule: Rule, flow: Flow) -> Tuple[bool, str]:
    misses: List[str] = []
    if rule.iif is not None and rule.iif != flow.iif:
        misses.append(f"iif is {flow.iif}, the rule wants {rule.iif}")
    if rule.oif is not None:
        misses.append(f"oif {rule.oif} matches only sockets bound to that device")
    if rule.fwmark is not None and (rule.fwmark ^ flow.mark) & rule.fwmask:
        misses.append(f"mark {hex(flow.mark)} is not {hex(rule.fwmark)}")
    if rule.src is not None and not rule.src.contains(flow.version, flow.src or 0):
        misses.append(f"source {flow.src_text} is not in {short(rule.src)}")
    if rule.dst is not None and not rule.dst.contains(flow.version, flow.dst):
        misses.append(f"destination {flow.dst_text} is not in {short(rule.dst)}")
    if rule.unmodelled:
        misses.append(f"selector {', '.join(rule.unmodelled)} is not modelled, treated as no match")
    if rule.invert:
        return bool(misses), "'not' inverts: " + ("; ".join(misses) if misses else "every selector matched")
    return not misses, "; ".join(misses)


def usable(route: Route) -> bool:
    return not route.nexthops or any(not h.dead for h in route.nexthops)


def lookup_table(routes: List[Route], flow: Flow) -> Tuple[Optional[Route], List[Route]]:
    candidates = sorted((r for r in routes if r.prefix.contains(flow.version, flow.dst)), key=lambda r: (-r.prefix.length, r.metric, r.order))
    return next((r for r in candidates if usable(r)), None), candidates


def why_this_route(route: Route, candidates: List[Route]) -> str:
    if len(candidates) == 1:
        return "the only matching prefix"
    listed = ", ".join(show_prefix(c.prefix) + (f" metric {c.metric}" if c.metric else "") for c in candidates)
    text = f"/{route.prefix.length} is the longest of {len(candidates)} matching prefixes: {listed}"
    same = [c for c in candidates if c is not route and c.prefix.length == route.prefix.length]
    if same:
        text += f"; metric {route.metric} beats {', '.join(str(c.metric) for c in same)} for the same prefix"
    if any(c.prefix.length > route.prefix.length for c in candidates):
        text += "; longer prefixes have no live next hop"
    return text


def walk(snap: Snapshot, flow: Flow) -> Walk:
    result = Walk()
    rules = snap.rules_for(flow.version)
    index = 0
    while index < len(rules):
        rule = rules[index]
        index += 1
        label = f"rule {rule.describe()}"
        matched, why = rule_matches(rule, flow)
        if not matched:
            result.trace.append(f"{label}: skipped, {why}")
            continue
        if rule.action in ("blackhole", "unreachable", "prohibit"):
            result.trace.append(f"{label}: matched, the rule itself answers {rule.action}")
            result.rule, result.error, result.detail = rule, rule.action, f"rule {rule.priority} is a {rule.action} rule"
            return result
        if rule.action == "nop":
            result.trace.append(f"{label}: matched, nop")
            continue
        if rule.action == "goto":
            target = next((i for i, r in enumerate(rules) if r.priority == rule.goto and i >= index), None)
            result.trace.append(f"{label}: matched, " + (f"jump to rule {rule.goto}" if target is not None else f"no later rule {rule.goto}, continue"))
            index = target if target is not None else index
            continue
        routes = snap.table(rule.table, flow.version)
        route, candidates = lookup_table(routes, flow)
        if route is None:
            result.trace.append(f"{label}: matched, table {rule.table} has no route for {flow.dst_text} ({len(routes)} routes in it), continue")
            continue
        if route.type == "throw":
            result.trace.append(f"{label}: matched, {route.describe()} hands the lookup back to the rules, continue")
            continue
        if rule.suppress_prefixlength is not None and route.prefix.length <= rule.suppress_prefixlength:
            result.trace.append(f"{label}: matched, {route.describe()} suppressed (/{route.prefix.length} <= suppress_prefixlength {rule.suppress_prefixlength}), continue")
            continue
        result.trace.append(f"{label}: matched, table {rule.table}: {route.describe()} ({why_this_route(route, candidates)})")
        result.rule, result.route = rule, route
        if route.type in ("blackhole", "unreachable", "prohibit"):
            result.error, result.detail = route.type, f"{route.describe()} in table {route.table}"
        return result
    result.error, result.detail = "noroute", f"none of the tables the {len(rules)} rules consulted has a route for {flow.dst_text}"
    return result


def select_source_v4(snap: Snapshot, dev: Optional[str], gateway: Optional[str], scope: int) -> Optional[str]:
    gw = parse_ip(gateway)[1] if gateway else None
    iface = snap.interface(dev)
    fallback = None
    for address in iface.addresses if iface else []:
        if address.version != 4 or address.secondary or SCOPES.get(address.scope, 0) > scope:
            continue
        if gw is None or address.prefix.contains(4, gw):
            return address.text
        fallback = fallback or address.text
    if fallback:
        return fallback
    for other in snap.interfaces:
        for address in other.addresses:
            if address.version == 4 and not address.secondary and address.scope != "link" and SCOPES.get(address.scope, 0) <= scope:
                return address.text
    return None


def v6_scope(value: int) -> int:
    if value == 1:
        return SCOPES["host"]
    return SCOPES["link"] if value >> 118 == 0x3FA else SCOPES["global"]


def select_source_v6(snap: Snapshot, dev: Optional[str], dst: int) -> Optional[str]:
    iface = snap.interface(dev)
    want = v6_scope(dst)
    for pool in (iface.addresses if iface else [], [a for i in snap.interfaces for a in i.addresses]):
        candidates = [a for a in pool if a.version == 6 and not a.tentative]
        if candidates:
            best = max(candidates, key=lambda a: (a.value == dst, SCOPES.get(a.scope, 0) == want, not a.deprecated, 128 - (a.value ^ dst).bit_length()))
            return best.text
    return None


def source_for(snap: Snapshot, flow: Flow, route: Route, hop: NextHop) -> Optional[str]:
    if flow.src is not None:
        return format_ip(flow.version, flow.src)
    if route.prefsrc:
        return route.prefsrc
    if flow.version == 4:
        return select_source_v4(snap, hop.dev, hop.gateway, route.scope_value)
    return select_source_v6(snap, hop.dev, flow.dst)


def check_input(snap: Snapshot, flow: Flow, out_dev: Optional[str], local: bool) -> Tuple[Optional[str], str]:
    if not local:
        forwarding = snap.sysctl_value(f"net.ipv4.conf.{flow.iif}.forwarding")
        if forwarding is None:
            forwarding = snap.sysctl_value("net.ipv4.ip_forward")
        if forwarding == 0:
            return "noforward", f"forwarding is off on {flow.iif} (net.ipv4.conf.{flow.iif}.forwarding = 0): a router that does not forward answers EHOSTUNREACH"
    mode = max(snap.sysctl_value("net.ipv4.conf.all.rp_filter") or 0, snap.sysctl_value(f"net.ipv4.conf.{flow.iif}.rp_filter") or 0)
    if mode == 0:
        return None, f"rp_filter 0 on {flow.iif}: the source address is not checked"
    back = walk(snap, Flow(4, flow.src or 0, flow.dst, out_dev or "lo"))
    src = flow.src_text
    if back.route is None or back.error:
        return "rpfilter", f"rp_filter {mode} on {flow.iif}: no route back to {src}"
    if back.route.type == "local":
        return "martian", f"rp_filter {mode} on {flow.iif}: {src} is one of this host's own addresses (martian source)"
    devs = [h.dev or "?" for h in back.route.nexthops]
    if flow.iif in devs:
        return None, f"rp_filter {mode} on {flow.iif}: the route back to {src} ({back.route.describe()}) leaves through {flow.iif}"
    if mode == 2:
        return None, f"rp_filter 2 (loose) on {flow.iif}: a route back to {src} exists via {', '.join(devs)}; loose mode accepts any interface"
    return "rpfilter", f"rp_filter 1 (strict) on {flow.iif}: the route back to {src} leaves through {', '.join(devs)}, not {flow.iif}"


def check_neighbour(snap: Snapshot, version: int, hop: Hop, dst: int) -> Tuple[str, Optional[bool]]:
    target = hop.gateway or format_ip(version, dst)
    iface = snap.interface(hop.dev)
    if iface and ("NOARP" in iface.flags or "LOOPBACK" in iface.flags):
        return f"neighbour: {hop.dev} is NOARP, no link-layer address needed", True
    want = parse_ip(target)
    for entry in snap.neighbours:
        try:
            same = parse_ip(entry.address) == want
        except InputError:
            continue
        if not same or entry.dev != hop.dev:
            continue
        text = f"neighbour {target} dev {hop.dev}: {'/'.join(entry.state) or 'NONE'}" + (f" lladdr {entry.lladdr}" if entry.lladdr else "")
        if "FAILED" in entry.state:
            return text + ": nothing answered ARP/NDP; the route is right, the next hop is not there", False
        if "INCOMPLETE" in entry.state:
            return text + ": a request went out, no reply yet", False
        return text, True
    return f"neighbour {target} dev {hop.dev}: no entry yet; the kernel resolves it on the first packet", None


def make_flow(snap: Snapshot, query: Query) -> Flow:
    version, dst = parse_ip(query.dst)
    src: Optional[int] = None
    if query.src:
        src_version, src = parse_ip(query.src)
        if src_version != version:
            raise InputError(f"--from {query.src} is IPv{src_version}, the destination is IPv{version}")
    if query.iif:
        if version != 4:
            raise InputError("--iif (a packet being forwarded) is modelled for IPv4 only")
        if src is None:
            raise InputError("--iif simulates a packet arriving from outside; give its source with --from")
    elif src is not None and not snap.is_local(version, src):
        raise InputError(f"--from {query.src} is not an address of this host; for local traffic the kernel refuses it (use --iif DEV for a forwarded packet)")
    return Flow(version, dst, src, query.iif or "lo", query.mark, bool(query.iif))


def fail(decision: Decision, key: str, detail: str) -> Decision:
    decision.ok = False
    decision.errno, decision.message = ERRORS[key]
    decision.reason = detail
    return decision


def route_get(snap: Snapshot, query: Query) -> Decision:
    flow = make_flow(snap, query)
    decision = Decision(query, dst=flow.dst_text)
    path = f"arriving on {flow.iif} from {flow.src_text}" if flow.arriving else f"locally generated (iif lo), source {flow.src_text}"
    decision.trace.append(f"flow: to {flow.dst_text}, {path}, mark {hex(flow.mark)}")
    if flow.arriving and snap.interface(flow.iif) is None:
        return fail(decision, "nodev", f"no interface named {flow.iif} in this snapshot")
    result = walk(snap, flow)
    decision.trace += result.trace
    if result.route is not None:
        decision.table, decision.route, decision.type = result.route.table, result.route, result.route.type
    if result.error or result.route is None or result.rule is None:
        return fail(decision, result.error or "noroute", result.detail)
    route = result.route
    hops = [h for h in route.nexthops if not h.dead] or [NextHop()]
    if route.type == "local":
        decision.hops = [Hop(None, "lo", 1, None)]
        decision.src = format_ip(flow.version, flow.src) if flow.src is not None else (route.prefsrc or flow.dst_text)
    else:
        decision.hops = [Hop(h.gateway, h.dev, h.weight, source_for(snap, flow, route, h)) for h in hops]
        sources = {h.src for h in decision.hops}
        decision.src = sources.pop() if len(sources) == 1 else None
    if flow.arriving:
        if not snap.sysctl:
            decision.trace.append("no sysctl.txt in the snapshot: forwarding assumed on, rp_filter assumed 0")
        error, note = check_input(snap, flow, hops[0].dev, route.type == "local")
        decision.trace.append(note)
        if error:
            return fail(decision, error, note)
    decision.ok = True
    decision.reason = f"rule {result.rule.priority} sent the lookup to table {route.table}, where {route.describe()} is the best match"
    if route.type == "unicast":
        verdicts = []
        for hop in decision.hops:
            text, ok = check_neighbour(snap, flow.version, hop, flow.dst)
            decision.neighbours.append(text)
            verdicts.append(ok)
        decision.neighbour_ok = False if False in verdicts else (True if all(v is True for v in verdicts) else None)
    return decision


def route_line(d: Decision) -> str:
    q = d.query
    words = ([] if d.type == "unicast" else [d.type]) + [d.dst] + (["from", q.src] if q.src else [])
    if len(d.hops) > 1:
        options = [f"via {h.gateway or '-'} dev {h.dev or '-'} weight {h.weight}" + (f" src {h.src}" if h.src and not q.src else "") for h in d.hops]
        table = f" table {d.table}" if d.table and d.table != "main" else ""
        return " ".join(words) + table + " ECMP, the kernel hashes each flow onto one of: " + "; ".join(options)
    hop = d.hops[0] if d.hops else Hop(None, None)
    words += (["via", hop.gateway] if hop.gateway else []) + (["dev", hop.dev] if hop.dev else [])
    words += ["table", d.table] if d.table and d.table != "main" else []
    words += ["src", d.src] if d.src and not q.src else []
    words += (["iif", q.iif] if q.iif else []) + (["mark", hex(q.mark)] if q.mark else [])
    return " ".join(words)


def render_decision(d: Decision) -> str:
    lines = [route_line(d) if d.ok else f"{d.dst}: {d.message} ({d.errno})", f"  why: {d.reason}"]
    lines += [f"  {n}. {step}" for n, step in enumerate(d.trace, 1)]
    lines += [f"  {text}" for text in d.neighbours]
    return "\n".join(lines)


def decision_json(d: Decision) -> Dict[str, object]:
    single = d.hops[0] if len(d.hops) == 1 else None
    return {
        "query": {"dst": d.query.dst, "from": d.query.src, "iif": d.query.iif, "mark": d.query.mark},
        "ok": d.ok,
        "type": d.type,
        "dst": d.dst,
        "table": d.table,
        "route": d.route.describe() if d.route else None,
        "gateway": single.gateway if single else None,
        "dev": single.dev if single else None,
        "src": d.src,
        "nexthops": [{"gateway": h.gateway, "dev": h.dev, "weight": h.weight, "src": h.src} for h in d.hops],
        "errno": d.errno,
        "error": d.message,
        "reason": d.reason,
        "trace": d.trace,
        "neighbours": d.neighbours,
        "neighbour_ok": d.neighbour_ok,
    }


def prefix_report(text: str) -> Dict[str, object]:
    prefix = parse_prefix(text)
    v = prefix.version
    first, last, hosts = prefix.usable
    report: Dict[str, object] = {
        "input": text,
        "prefix": str(prefix),
        "version": v,
        "netmask": format_ip(v, prefix.mask),
        "hostmask": format_ip(v, prefix.hostmask),
        "broadcast": format_ip(v, prefix.broadcast) if prefix.broadcast is not None else None,
        "first": format_ip(v, first),
        "last": format_ip(v, last),
        "addresses": prefix.size,
        "usable": hosts,
    }
    address = parse_ip(text.partition("/")[0])[1]
    if address != prefix.network:
        report["host"] = format_ip(v, address)
    return report


def relation(a: Prefix, b: Prefix) -> str:
    if a.version != b.version:
        return f"{a} and {b} are different address families"
    if a == b:
        return f"{a} and {b} are the same block"
    if a.covers(b):
        return f"{a} contains {b}"
    if b.covers(a):
        return f"{b} contains {a}"
    return f"{a} and {b} are disjoint"


def cmd_cidr(args: argparse.Namespace) -> int:
    reports = [prefix_report(text) for text in args.prefixes]
    prefixes = [parse_prefix(text) for text in args.prefixes]
    relations = [relation(a, b) for i, a in enumerate(prefixes) for b in prefixes[i + 1:]]
    if args.json:
        print(json.dumps({"prefixes": reports, "relations": relations}, indent=2))
        return 0
    for r in reports:
        print(f"{r['prefix']}  (IPv{r['version']}{', host ' + str(r['host']) if 'host' in r else ''})")
        print(f"  netmask    {r['netmask']}  hostmask {r['hostmask']}")
        if r["broadcast"]:
            print(f"  broadcast  {r['broadcast']}")
        print(f"  hosts      {r['first']} - {r['last']} ({r['usable']} usable of {r['addresses']})")
    for line in relations:
        print(line)
    return 0


def cmd_contains(args: argparse.Namespace) -> int:
    network = parse_prefix(args.network)
    item = parse_prefix(args.item)
    inside = network.covers(item)
    print(f"{'yes' if inside else 'no'}: {relation(network, item) if item.length < item.bits else short(item) + (' is in ' if inside else ' is not in ') + str(network)}")
    return 0 if inside else 1


def cmd_summarise(args: argparse.Namespace) -> int:
    prefixes = [parse_prefix(text) for text in args.prefixes]
    exact = collapse(prefixes)
    families = {p.version for p in prefixes}
    one = supernet(prefixes) if len(families) == 1 else None
    extra = one.size - sum(p.size for p in exact) if one else None
    if args.json:
        print(json.dumps({"collapsed": [str(p) for p in exact], "supernet": str(one) if one else None, "extra_addresses": extra}, indent=2))
        return 0
    print(f"exact:    {', '.join(str(p) for p in exact)} ({len(prefixes)} prefixes in, {len(exact)} out)")
    if one:
        print(f"one route: {one}" + (f", which also attracts {extra} addresses none of the inputs cover" if extra else ", exact"))
    return 0


def load_snapshot(args: argparse.Namespace) -> Snapshot:
    if args.dir:
        return build_snapshot(read_dir(args.dir))
    if not sys.platform.startswith("linux"):
        raise InputError("live mode reads the kernel with ip -j and needs Linux; capture a snapshot there and pass --dir")
    return build_snapshot(gather(args.netns))


def cmd_get(args: argparse.Namespace) -> int:
    decision = route_get(load_snapshot(args), Query(args.destination, args.src, args.iif, parse_int(args.mark)))
    print(json.dumps(decision_json(decision), indent=2) if args.json else render_decision(decision))
    return 0 if decision.ok else 1


def cmd_rules(args: argparse.Namespace) -> int:
    snap = load_snapshot(args)
    for version in (4, 6):
        rules = snap.rules_for(version)
        print(f"IPv{version} rules in lookup order{'' if snap.rules.get(version) else ' (no rule dump: kernel defaults)'}:")
        for rule in rules:
            count = f"  table {rule.table}: {len(snap.table(rule.table, version))} routes" if rule.action == "lookup" else ""
            print(f"  {rule.describe():<48}{count}")
    return 0


def cmd_snapshot(args: argparse.Namespace) -> int:
    if not sys.platform.startswith("linux"):
        raise InputError("snapshot runs ip -j and reads /proc/sys; it needs Linux")
    files = gather(args.netns)
    os.makedirs(args.out, exist_ok=True)
    for name, text in files.items():
        with open(os.path.join(args.out, name), "w", encoding="utf-8") as handle:
            handle.write(text)
    snap = build_snapshot(files)
    print(f"wrote {len(files)} files to {args.out}: {len(snap.interfaces)} interfaces, {len(snap.routes)} routes, {len(snap.rules[4]) + len(snap.rules[6])} rules, {len(snap.neighbours)} neighbours")
    return 0


def add_source(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--dir", help="snapshot directory (addr.json, route.json, rule.json, neigh.json, sysctl.txt ...) instead of the live kernel")
    parser.add_argument("--netns", help="live mode inside this network namespace (ip -n NAME)")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="routelens", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    cidr = sub.add_parser("cidr", help="network, masks, broadcast and host range of each prefix, and how they relate")
    cidr.add_argument("prefixes", nargs="+")
    cidr.add_argument("--json", action="store_true")
    contains = sub.add_parser("contains", help="exit 0 if NETWORK contains ADDRESS or PREFIX, 1 if not")
    contains.add_argument("network")
    contains.add_argument("item")
    summary = sub.add_parser("summarise", help="collapse prefixes exactly, and the single supernet that covers them")
    summary.add_argument("prefixes", nargs="+")
    summary.add_argument("--json", action="store_true")
    get = sub.add_parser("get", help="a from-scratch ip route get: rules, longest match, source, ECMP, rp_filter, neighbour")
    get.add_argument("destination")
    get.add_argument("--from", dest="src", help="source address (a local one, or the packet's source with --iif)")
    get.add_argument("--iif", help="simulate a packet arriving on this interface (the forwarding path)")
    get.add_argument("--mark", default="0", help="fwmark, decimal or 0x hex")
    get.add_argument("--json", action="store_true")
    add_source(get)
    rules = sub.add_parser("rules", help="policy rules in lookup order with the size of each table")
    add_source(rules)
    snap = sub.add_parser("snapshot", help="write ip -j addr/route/rule/neigh and the forwarding sysctls to a directory (Linux)")
    snap.add_argument("out")
    snap.add_argument("--netns")
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(list(sys.argv[1:] if argv is None else argv))
    handlers = {"cidr": cmd_cidr, "contains": cmd_contains, "summarise": cmd_summarise, "get": cmd_get, "rules": cmd_rules, "snapshot": cmd_snapshot}
    try:
        return handlers[args.command](args)
    except (InputError, ValueError) as exc:
        print(f"routelens: {exc}", file=sys.stderr)
        return 2
    except FileNotFoundError as exc:
        print(f"routelens: {exc.filename or exc}: not found; is iproute2 installed?", file=sys.stderr)
        return 2
    except subprocess.CalledProcessError as exc:
        print(f"routelens: {' '.join(exc.cmd)} failed: {(exc.stderr or '').strip() or exc.returncode}; are you root, and does the namespace exist?", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
