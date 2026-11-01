"""pcaplens: read pcap and pcapng captures, decode Ethernet, Linux cooked, IPv4, IPv6, TCP, UDP and DNS, filter them with a tcpdump-style expression and summarise every connection."""
from __future__ import annotations

import argparse
import ipaddress
import json
import re
import struct
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable, Dict, Iterable, List, Optional, Sequence, Tuple, Union

NS = 1_000_000_000
MASK32 = 0xFFFFFFFF
MAX_SNAPLEN = 262144

PCAP_MAGIC_MICRO = 0xA1B2C3D4
PCAP_MAGIC_NANO = 0xA1B23C4D
PCAPNG_SHB = 0x0A0D0D0A
PCAPNG_IDB = 0x00000001
PCAPNG_SPB = 0x00000003
PCAPNG_EPB = 0x00000006
PCAPNG_BYTE_ORDER_MAGIC = 0x1A2B3C4D
PCAPNG_OPT_TSRESOL = 9

LINKTYPE_NULL = 0
LINKTYPE_ETHERNET = 1
LINKTYPE_RAW = 101
LINKTYPE_LINUX_SLL = 113
LINKTYPE_LINUX_SLL2 = 276
LINKTYPES = {
    LINKTYPE_NULL: "NULL",
    LINKTYPE_ETHERNET: "EN10MB",
    LINKTYPE_RAW: "RAW",
    LINKTYPE_LINUX_SLL: "LINUX_SLL",
    LINKTYPE_LINUX_SLL2: "LINUX_SLL2",
}
LINK_BY_NAME = {"null": LINKTYPE_NULL, "ether": LINKTYPE_ETHERNET, "raw": LINKTYPE_RAW, "sll": LINKTYPE_LINUX_SLL, "sll2": LINKTYPE_LINUX_SLL2}

ETHERTYPE_IPV4 = 0x0800
ETHERTYPE_ARP = 0x0806
ETHERTYPE_IPV6 = 0x86DD
VLAN_ETHERTYPES = (0x8100, 0x88A8)
ARPHRD_LOOPBACK = 772
BSD_AF_INET = 2
BSD_AF_INET6 = (24, 28, 30)
SLL_PACKET_TYPES = {0: "In", 1: "B", 2: "M", 3: "P", 4: "Out"}

IPPROTO_ICMP = 1
IPPROTO_TCP = 6
IPPROTO_UDP = 17
IPPROTO_ICMPV6 = 58
IPV6_FRAGMENT = 44
IPV6_AUTH = 51
IPV6_EIGHT_BYTE_UNITS = (0, 43, 60)
PROTO_NAMES = {IPPROTO_ICMP: "icmp", IPPROTO_TCP: "tcp", IPPROTO_UDP: "udp", IPPROTO_ICMPV6: "icmp6"}
ICMP_TYPES = {0: "echo reply", 3: "unreachable", 8: "echo request", 11: "time exceeded"}
ICMP6_TYPES = {1: "unreachable", 3: "time exceeded", 128: "echo request", 129: "echo reply", 135: "neighbor solicitation", 136: "neighbor advertisement"}

FIN, SYN, RST, PSH, ACK, URG, ECE, CWR = 0x01, 0x02, 0x04, 0x08, 0x10, 0x20, 0x40, 0x80
FLAG_LETTERS = ((FIN, "F"), (SYN, "S"), (RST, "R"), (PSH, "P"), (ACK, "."), (URG, "U"), (ECE, "E"), (CWR, "W"))

DNS_TYPES = {1: "A", 2: "NS", 5: "CNAME", 6: "SOA", 12: "PTR", 15: "MX", 16: "TXT", 28: "AAAA", 33: "SRV", 41: "OPT", 64: "SVCB", 65: "HTTPS", 255: "ANY"}
DNS_TYPE_BY_NAME = {name: number for number, name in DNS_TYPES.items()}
DNS_RCODES = {0: "NOERROR", 1: "FORMERR", 2: "SERVFAIL", 3: "NXDOMAIN", 4: "NOTIMP", 5: "REFUSED"}
TCPDUMP_RCODES = {1: "FormErr", 2: "ServFail", 3: "NXDomain", 4: "NotImp", 5: "Refused"}

SERVICES = {"ssh": 22, "domain": 53, "http": 80, "ntp": 123, "https": 443}
FILTER_CONSTANTS = {
    "tcpflags": 13,
    "tcp-fin": FIN,
    "tcp-syn": SYN,
    "tcp-rst": RST,
    "tcp-push": PSH,
    "tcp-ack": ACK,
    "tcp-urg": URG,
    "tcp-ece": ECE,
    "tcp-cwr": CWR,
    "icmptype": 0,
    "icmpcode": 1,
    "icmp-echoreply": 0,
    "icmp-unreach": 3,
    "icmp-echo": 8,
    "icmp-timxceed": 11,
}
PROTO_KEYWORDS = {"ip", "ip6", "arp", "tcp", "udp", "icmp", "icmp6"}
DIR_KEYWORDS = {"src", "dst"}
TYPE_KEYWORDS = {"host", "net", "port", "portrange"}
LOGIC_KEYWORDS = {"and", "or", "not", "&&", "||", "!"}
KEYWORDS = PROTO_KEYWORDS | DIR_KEYWORDS | TYPE_KEYWORDS | LOGIC_KEYWORDS
UNSUPPORTED_KEYWORDS = {"ether", "vlan", "less", "greater", "len", "broadcast", "multicast", "gateway", "proto", "sctp", "inbound", "outbound", "mpls", "pppoes", "geneve"}
ACCESSOR_BASES = {"ip": None, "ip6": None, "tcp": IPPROTO_TCP, "udp": IPPROTO_UDP, "icmp": IPPROTO_ICMP}
RELOPS = {
    "=": lambda a, b: a == b,
    "==": lambda a, b: a == b,
    "!=": lambda a, b: a != b,
    ">": lambda a, b: a > b,
    "<": lambda a, b: a < b,
    ">=": lambda a, b: a >= b,
    "<=": lambda a, b: a <= b,
}
OPERATOR_TOKENS = {"(", ")", "[", "]", "&", "|"} | set(RELOPS)
TOKEN = re.compile(r"\s*(?:(&&|\|\||==|!=|>=|<=|[()\[\]&|!<>=])|([A-Za-z0-9_.:/\-]+))")

IPAddress = Union[ipaddress.IPv4Address, ipaddress.IPv6Address]
Endpoint = Tuple[str, int]


class CaptureError(ValueError):
    pass


class FilterError(ValueError):
    pass


class Truncated(Exception):
    pass


class OutOfPacket(Exception):
    pass


@dataclass
class Packet:
    index: int
    ts_ns: int
    linktype: int
    data: bytes
    orig_len: int
    interface: int = 0

    @property
    def truncated(self) -> bool:
        return len(self.data) < self.orig_len


@dataclass
class Capture:
    format: str
    byte_order: str
    precision: str
    version: str
    snaplen: int
    linktypes: List[int]
    packets: List[Packet] = field(default_factory=list)
    damage: Optional[str] = None


def pcap_byte_order(magic: bytes) -> Tuple[str, int]:
    for prefix in ("<", ">"):
        value = struct.unpack(prefix + "I", magic)[0]
        if value in (PCAP_MAGIC_MICRO, PCAP_MAGIC_NANO):
            return prefix, value
    if magic[:2] == b"\x1f\x8b":
        raise CaptureError("the file is gzip-compressed; run gunzip first")
    raise CaptureError(f"unknown magic number {magic.hex()}: not a pcap (a1b2c3d4, a1b23c4d in either byte order) or pcapng (0a0d0d0a) file")


def read_pcap(data: bytes) -> Capture:
    if len(data) < 24:
        raise CaptureError(f"a pcap global header is 24 bytes, the file has {len(data)}")
    prefix, magic = pcap_byte_order(data[:4])
    major, minor, _zone, _sigfigs, snaplen, network = struct.unpack(prefix + "HHiIII", data[4:24])
    units = 1 if magic == PCAP_MAGIC_NANO else 1000
    capture = Capture("pcap", "big" if prefix == ">" else "little", "ns" if units == 1 else "us", f"{major}.{minor}", snaplen, [network & 0xFFFF])
    record = struct.Struct(prefix + "IIII")
    offset = 24
    while offset < len(data):
        if len(data) - offset < record.size:
            capture.damage = f"truncated dump file; tried to read {record.size} header bytes, only got {len(data) - offset}"
            break
        seconds, fraction, caplen, origlen = record.unpack_from(data, offset)
        if caplen > MAX_SNAPLEN:
            capture.damage = f"packet {len(capture.packets) + 1} claims {caplen} captured bytes, more than the maximum {MAX_SNAPLEN}: the file is corrupt"
            break
        start = offset + record.size
        if start + caplen > len(data):
            capture.damage = f"truncated dump file; tried to read {caplen} captured bytes, only got {len(data) - start}"
            break
        capture.packets.append(Packet(len(capture.packets) + 1, seconds * NS + fraction * units, capture.linktypes[0], data[start:start + caplen], origlen))
        offset = start + caplen
    return capture


def pcapng_options(raw: bytes, prefix: str) -> Dict[int, bytes]:
    options: Dict[int, bytes] = {}
    offset = 0
    while offset + 4 <= len(raw):
        code, length = struct.unpack_from(prefix + "HH", raw, offset)
        if code == 0:
            break
        options.setdefault(code, raw[offset + 4:offset + 4 + length])
        offset += 4 + (length + 3) // 4 * 4
    return options


def tsresol_units(value: Optional[bytes]) -> int:
    if not value:
        return 1_000_000
    exponent = value[0] & 0x7F
    return 2 ** exponent if value[0] & 0x80 else 10 ** exponent


def precision_name(units: int) -> str:
    return {1_000_000: "us", NS: "ns", 1000: "ms"}.get(units, f"1/{units} s")


def read_pcapng(data: bytes) -> Capture:
    capture = Capture("pcapng", "little", "us", "", 0, [])
    interfaces: List[Tuple[int, int, int]] = []
    prefix = "<"
    offset = 0
    while offset < len(data):
        if len(data) - offset < 12:
            capture.damage = f"truncated dump file; {len(data) - offset} bytes left where a block header needs 12"
            break
        if data[offset:offset + 4] == b"\x0a\x0d\x0d\x0a":
            magic = data[offset + 8:offset + 12]
            if magic == struct.pack("<I", PCAPNG_BYTE_ORDER_MAGIC):
                prefix = "<"
            elif magic == struct.pack(">I", PCAPNG_BYTE_ORDER_MAGIC):
                prefix = ">"
            else:
                raise CaptureError(f"section header at byte {offset} has byte-order magic {magic.hex()}, expected 1a2b3c4d in either order")
            interfaces = []
        elif offset == 0:
            raise CaptureError("a pcapng file must start with a section header block")
        block_type, total = struct.unpack_from(prefix + "II", data, offset)
        if total < 12 or total % 4 or offset + total > len(data):
            capture.damage = f"truncated dump file; block at byte {offset} (type 0x{block_type:08x}) claims {total} bytes, only {len(data) - offset} left"
            break
        body = data[offset + 8:offset + total - 4]
        if block_type == PCAPNG_SHB:
            major, minor = struct.unpack_from(prefix + "HH", body, 4)
            capture.byte_order = "big" if prefix == ">" else "little"
            capture.version = capture.version or f"{major}.{minor}"
        elif block_type == PCAPNG_IDB:
            linktype, _reserved, snaplen = struct.unpack_from(prefix + "HHI", body, 0)
            units = tsresol_units(pcapng_options(body[8:], prefix).get(PCAPNG_OPT_TSRESOL))
            interfaces.append((linktype, snaplen, units))
            capture.linktypes.append(linktype)
            if len(capture.linktypes) == 1:
                capture.snaplen, capture.precision = snaplen, precision_name(units)
        elif block_type in (PCAPNG_EPB, PCAPNG_SPB):
            packet = pcapng_packet(block_type, body, prefix, interfaces, len(capture.packets) + 1, offset)
            if packet is None:
                capture.damage = f"truncated dump file; packet block at byte {offset} is shorter than its captured length"
                break
            capture.packets.append(packet)
        offset += total
    return capture


def pcapng_packet(block_type: int, body: bytes, prefix: str, interfaces: List[Tuple[int, int, int]], index: int, offset: int) -> Optional[Packet]:
    if block_type == PCAPNG_EPB:
        interface, high, low, caplen, origlen = struct.unpack_from(prefix + "IIIII", body, 0)
        start, ticks = 20, (high << 32) | low
    else:
        origlen = struct.unpack_from(prefix + "I", body, 0)[0]
        interface, start, ticks = 0, 4, 0
        caplen = min(origlen, (interfaces[0][1] if interfaces else 0) or origlen, len(body) - start)
    if interface >= len(interfaces):
        raise CaptureError(f"packet block at byte {offset} refers to interface {interface}, but only {len(interfaces)} interface description block(s) came before it")
    linktype, _snaplen, units = interfaces[interface]
    if start + caplen > len(body):
        return None
    return Packet(index, ticks * NS // units, linktype, body[start:start + caplen], origlen, interface)


def read_capture(data: bytes) -> Capture:
    if len(data) < 4:
        raise CaptureError(f"{len(data)} bytes is too short for a capture file header")
    if data[:4] == b"\x0a\x0d\x0d\x0a":
        return read_pcapng(data)
    return read_pcap(data)


def write_pcap(frames: Sequence[Tuple[int, bytes]], linktype: int = LINKTYPE_ETHERNET, snaplen: int = MAX_SNAPLEN, nanoseconds: bool = False, big_endian: bool = False) -> bytes:
    prefix = ">" if big_endian else "<"
    out = [struct.pack(prefix + "IHHiIII", PCAP_MAGIC_NANO if nanoseconds else PCAP_MAGIC_MICRO, 2, 4, 0, 0, snaplen, linktype)]
    for ts_ns, frame in frames:
        seconds, rest = divmod(ts_ns, NS)
        kept = frame[:snaplen]
        out.append(struct.pack(prefix + "IIII", seconds, rest if nanoseconds else rest // 1000, len(kept), len(frame)))
        out.append(kept)
    return b"".join(out)


def pcapng_block(block_type: int, body: bytes, prefix: str) -> bytes:
    body += b"\0" * (-len(body) % 4)
    total = len(body) + 12
    return struct.pack(prefix + "II", block_type, total) + body + struct.pack(prefix + "I", total)


def write_pcapng(frames: Sequence[Tuple[int, bytes]], linktype: int = LINKTYPE_ETHERNET, snaplen: int = MAX_SNAPLEN, big_endian: bool = False) -> bytes:
    prefix = ">" if big_endian else "<"
    blocks = [pcapng_block(PCAPNG_SHB, struct.pack(prefix + "IHHq", PCAPNG_BYTE_ORDER_MAGIC, 1, 0, -1), prefix)]
    tsresol = struct.pack(prefix + "HH", PCAPNG_OPT_TSRESOL, 1) + b"\x09\0\0\0" + struct.pack(prefix + "HH", 0, 0)
    blocks.append(pcapng_block(PCAPNG_IDB, struct.pack(prefix + "HHI", linktype, 0, snaplen) + tsresol, prefix))
    for ts_ns, frame in frames:
        kept = frame[:snaplen]
        blocks.append(pcapng_block(PCAPNG_EPB, struct.pack(prefix + "IIIII", 0, ts_ns >> 32, ts_ns & MASK32, len(kept), len(frame)) + kept, prefix))
    return b"".join(blocks)


def inet_checksum(data: bytes) -> int:
    if len(data) % 2:
        data += b"\0"
    total = sum(struct.unpack(f"!{len(data) // 2}H", data))
    while total >> 16:
        total = (total & 0xFFFF) + (total >> 16)
    return ~total & 0xFFFF


def pseudo_header(src: str, dst: str, proto: int, length: int) -> bytes:
    source, destination = ipaddress.ip_address(src).packed, ipaddress.ip_address(dst).packed
    if len(source) == 4:
        return source + destination + struct.pack("!BBH", 0, proto, length)
    return source + destination + struct.pack("!I3xB", length, proto)


def tcp_options(mss: Optional[int] = None, sack_ok: bool = False, timestamps: Optional[Tuple[int, int]] = None, wscale: Optional[int] = None, sack: Sequence[Tuple[int, int]] = ()) -> bytes:
    out = b""
    if mss is not None:
        out += struct.pack("!BBH", 2, 4, mss)
    if sack_ok:
        out += b"\x04\x02"
    if timestamps is not None:
        out += struct.pack("!BBII", 8, 10, *timestamps)
    if wscale is not None:
        out += b"\x01" + struct.pack("!BBB", 3, 3, wscale)
    if sack:
        out += b"\x01\x01" + struct.pack("!BB", 5, 2 + 8 * len(sack)) + b"".join(struct.pack("!II", left, right) for left, right in sack)
    return out + b"\x01" * (-len(out) % 4)


def tcp_segment(src: str, dst: str, sport: int, dport: int, seq: int, ack: int = 0, flags: int = SYN, window: int = 65535, options: bytes = b"", payload: bytes = b"") -> bytes:
    header_len = 20 + len(options)
    header = struct.pack("!HHIIBBHHH", sport, dport, seq & MASK32, ack & MASK32, (header_len // 4) << 4, flags, window, 0, 0) + options
    checksum = inet_checksum(pseudo_header(src, dst, IPPROTO_TCP, header_len + len(payload)) + header + payload)
    return header[:16] + struct.pack("!H", checksum) + header[18:] + payload


def udp_datagram(src: str, dst: str, sport: int, dport: int, payload: bytes) -> bytes:
    header = struct.pack("!HHHH", sport, dport, 8 + len(payload), 0)
    checksum = inet_checksum(pseudo_header(src, dst, IPPROTO_UDP, 8 + len(payload)) + header + payload) or 0xFFFF
    return header[:6] + struct.pack("!H", checksum) + payload


def ip_packet(src: str, dst: str, proto: int, payload: bytes, ttl: int = 64, ident: int = 0, frag: int = 0x4000) -> bytes:
    source, destination = ipaddress.ip_address(src).packed, ipaddress.ip_address(dst).packed
    if len(source) == 16:
        return struct.pack("!IHBB", 6 << 28, len(payload), proto, ttl) + source + destination + payload
    header = struct.pack("!BBHHHBBH4s4s", 0x45, 0, 20 + len(payload), ident, frag, ttl, proto, 0, source, destination)
    return header[:10] + struct.pack("!H", inet_checksum(header)) + header[12:] + payload


def link_frame(packet: bytes, linktype: int, outgoing: bool = False, ifindex: int = 1) -> bytes:
    version = packet[0] >> 4
    ethertype = ETHERTYPE_IPV6 if version == 6 else ETHERTYPE_IPV4
    packet_type = 4 if outgoing else 0
    if linktype == LINKTYPE_ETHERNET:
        return b"\0" * 12 + struct.pack("!H", ethertype) + packet
    if linktype == LINKTYPE_LINUX_SLL:
        return struct.pack("!HHH8sH", packet_type, ARPHRD_LOOPBACK, 6, b"", ethertype) + packet
    if linktype == LINKTYPE_LINUX_SLL2:
        return struct.pack("!HHIHBB8s", ethertype, 0, ifindex, ARPHRD_LOOPBACK, packet_type, 6, b"") + packet
    if linktype == LINKTYPE_NULL:
        return struct.pack("<I", 30 if version == 6 else BSD_AF_INET) + packet
    if linktype == LINKTYPE_RAW:
        return packet
    raise CaptureError(f"cannot build frames for link type {linktype}")


@dataclass
class Tcp:
    seq: int
    ack: int
    flags: int
    window: int
    header_len: int
    mss: Optional[int] = None
    wscale: Optional[int] = None
    sack_ok: bool = False
    sack: List[Tuple[int, int]] = field(default_factory=list)
    timestamps: Optional[Tuple[int, int]] = None
    options: List[str] = field(default_factory=list)


@dataclass
class Decoded:
    packet: Packet
    link: str = ""
    direction: Optional[str] = None
    ifindex: Optional[int] = None
    ethertype: Optional[int] = None
    net_offset: Optional[int] = None
    version: Optional[int] = None
    src: Optional[IPAddress] = None
    dst: Optional[IPAddress] = None
    ttl: Optional[int] = None
    proto: Optional[int] = None
    fragment: bool = False
    first_fragment: bool = True
    l4_offset: Optional[int] = None
    l4_len: int = 0
    sport: Optional[int] = None
    dport: Optional[int] = None
    tcp: Optional[Tcp] = None
    icmp: Optional[Tuple[int, int]] = None
    arp_op: Optional[int] = None
    arp_mac: Optional[str] = None
    payload: bytes = b""
    payload_len: int = 0
    problem: Optional[str] = None


def need(data: bytes, end: int, what: str) -> None:
    if len(data) < end:
        raise Truncated(f"[|{what}]")


def decode_link(d: Decoded, data: bytes) -> Tuple[int, Optional[int]]:
    linktype = d.packet.linktype
    if linktype == LINKTYPE_ETHERNET:
        d.link = "ether"
        need(data, 14, "ether")
        offset, ethertype = 14, struct.unpack_from("!H", data, 12)[0]
        while ethertype in VLAN_ETHERTYPES:
            need(data, offset + 4, "vlan")
            ethertype = struct.unpack_from("!H", data, offset + 2)[0]
            offset += 4
    elif linktype == LINKTYPE_LINUX_SLL:
        d.link = "sll"
        need(data, 16, "sll")
        packet_type, ethertype = struct.unpack_from("!H", data, 0)[0], struct.unpack_from("!H", data, 14)[0]
        d.direction, offset = SLL_PACKET_TYPES.get(packet_type, str(packet_type)), 16
    elif linktype == LINKTYPE_LINUX_SLL2:
        d.link = "sll2"
        need(data, 20, "sll2")
        ethertype, _reserved, d.ifindex = struct.unpack_from("!HHI", data, 0)
        d.direction, offset = SLL_PACKET_TYPES.get(data[10], str(data[10])), 20
    elif linktype == LINKTYPE_NULL:
        d.link = "null"
        need(data, 4, "null")
        family = struct.unpack_from("<I", data, 0)[0]
        if family > 0xFFFF:
            family = struct.unpack_from(">I", data, 0)[0]
        offset = 4
        ethertype = ETHERTYPE_IPV4 if family == BSD_AF_INET else ETHERTYPE_IPV6 if family in BSD_AF_INET6 else None
    elif linktype == LINKTYPE_RAW:
        d.link = "raw"
        need(data, 1, "ip")
        offset, ethertype = 0, {4: ETHERTYPE_IPV4, 6: ETHERTYPE_IPV6}.get(data[0] >> 4)
    else:
        raise Truncated(f"[link type {linktype} not supported]")
    d.ethertype, d.net_offset = ethertype, offset
    return offset, ethertype


def decode_ipv4(d: Decoded, data: bytes, offset: int) -> None:
    need(data, offset + 20, "ip")
    version_ihl, _tos, total, _ident, frag, ttl, proto = struct.unpack_from("!BBHHHBB", data, offset)
    header_len = (version_ihl & 0x0F) * 4
    d.version, d.ttl, d.proto = 4, ttl, proto
    d.src = ipaddress.IPv4Address(data[offset + 12:offset + 16])
    d.dst = ipaddress.IPv4Address(data[offset + 16:offset + 20])
    d.first_fragment = frag & 0x1FFF == 0
    d.fragment = bool(frag & 0x2000) or not d.first_fragment
    d.l4_len = max(0, total - header_len)
    if d.first_fragment:
        d.l4_offset = offset + header_len


def decode_ipv6(d: Decoded, data: bytes, offset: int) -> None:
    need(data, offset + 40, "ip6")
    payload_len, next_header, hop_limit = struct.unpack_from("!HBB", data, offset + 4)
    d.version, d.ttl = 6, hop_limit
    d.src = ipaddress.IPv6Address(data[offset + 8:offset + 24])
    d.dst = ipaddress.IPv6Address(data[offset + 24:offset + 40])
    cursor, remaining = offset + 40, payload_len
    while next_header in IPV6_EIGHT_BYTE_UNITS or next_header in (IPV6_FRAGMENT, IPV6_AUTH):
        need(data, cursor + 8, "ip6 extension")
        if next_header == IPV6_FRAGMENT:
            d.fragment = True
            d.first_fragment = struct.unpack_from("!H", data, cursor + 2)[0] & 0xFFF8 == 0
            size = 8
        elif next_header == IPV6_AUTH:
            size = (data[cursor + 1] + 2) * 4
        else:
            size = (data[cursor + 1] + 1) * 8
        next_header = data[cursor]
        cursor, remaining = cursor + size, remaining - size
    d.proto, d.l4_len = next_header, max(0, remaining)
    if d.first_fragment:
        d.l4_offset = cursor


def decode_arp(d: Decoded, data: bytes, offset: int) -> None:
    need(data, offset + 28, "arp")
    _htype, ptype, hlen, plen, operation = struct.unpack_from("!HHBBH", data, offset)
    if ptype == ETHERTYPE_IPV4 and hlen == 6 and plen == 4:
        d.arp_op = operation
        d.arp_mac = ":".join(f"{b:02x}" for b in data[offset + 8:offset + 14])
        d.src = ipaddress.IPv4Address(data[offset + 14:offset + 18])
        d.dst = ipaddress.IPv4Address(data[offset + 24:offset + 28])


def parse_tcp_options(tcp: Tcp, raw: bytes, complete: bool = True) -> None:
    i = 0
    while i < len(raw):
        kind = raw[i]
        if kind == 0:
            tcp.options.append("eol")
            break
        if kind == 1:
            tcp.options.append("nop")
            i += 1
            continue
        if i + 1 >= len(raw) or raw[i + 1] < 2 or i + raw[i + 1] > len(raw):
            if complete:
                tcp.options.append("[bad opt]")
            break
        length = raw[i + 1]
        value = raw[i + 2:i + length]
        if kind == 2 and length == 4:
            tcp.mss = struct.unpack("!H", value)[0]
            tcp.options.append(f"mss {tcp.mss}")
        elif kind == 3 and length == 3:
            tcp.wscale = value[0]
            tcp.options.append(f"wscale {tcp.wscale}")
        elif kind == 4 and length == 2:
            tcp.sack_ok = True
            tcp.options.append("sackOK")
        elif kind == 5 and (length - 2) % 8 == 0:
            tcp.sack = [struct.unpack_from("!II", value, j) for j in range(0, len(value), 8)]
            tcp.options.append(f"sack {len(tcp.sack)} " + " ".join(f"{{{left}:{right}}}" for left, right in tcp.sack))
        elif kind == 8 and length == 10:
            tcp.timestamps = struct.unpack("!II", value)
            tcp.options.append(f"TS val {tcp.timestamps[0]} ecr {tcp.timestamps[1]}")
        else:
            tcp.options.append(f"opt-{kind}")
        i += length


def decode_transport(d: Decoded, data: bytes) -> None:
    start = d.l4_offset
    if start is None:
        return
    if d.proto in (IPPROTO_TCP, IPPROTO_UDP):
        need(data, start + 4, PROTO_NAMES[d.proto])
        d.sport, d.dport = struct.unpack_from("!HH", data, start)
    if d.proto == IPPROTO_TCP:
        need(data, start + 20, "tcp")
        seq, ack, offset_byte, flags, window = struct.unpack_from("!IIBBH", data, start + 4)
        tcp = Tcp(seq, ack, flags, window, (offset_byte >> 4) * 4)
        d.tcp = tcp
        d.payload_len = max(0, d.l4_len - tcp.header_len)
        d.payload = data[start + tcp.header_len:start + tcp.header_len + d.payload_len]
        parse_tcp_options(tcp, data[start + 20:start + tcp.header_len], complete=len(data) >= start + tcp.header_len)
        need(data, start + tcp.header_len, "tcp")
    elif d.proto == IPPROTO_UDP:
        need(data, start + 8, "udp")
        length = struct.unpack_from("!H", data, start + 4)[0]
        d.payload_len = max(0, min(length, d.l4_len) - 8)
        d.payload = data[start + 8:start + 8 + d.payload_len]
    elif d.proto in (IPPROTO_ICMP, IPPROTO_ICMPV6):
        need(data, start + 2, "icmp")
        d.icmp = (data[start], data[start + 1])
        d.payload_len = max(0, d.l4_len - 8)


def decode(packet: Packet) -> Decoded:
    d = Decoded(packet)
    data = packet.data
    try:
        offset, ethertype = decode_link(d, data)
        if ethertype == ETHERTYPE_IPV4:
            decode_ipv4(d, data, offset)
        elif ethertype == ETHERTYPE_IPV6:
            decode_ipv6(d, data, offset)
        elif ethertype == ETHERTYPE_ARP:
            decode_arp(d, data, offset)
        decode_transport(d, data)
    except Truncated as exc:
        d.problem = str(exc)
    return d


@dataclass
class Node:
    text: str
    test: Callable[[Decoded], bool]
    explain: str = ""
    children: List["Node"] = field(default_factory=list)


@dataclass
class Value:
    text: str
    get: Callable[[Decoded], Optional[int]]


def tokenize(text: str) -> List[str]:
    tokens: List[str] = []
    text = text.strip()
    pos = 0
    while pos < len(text):
        match = TOKEN.match(text, pos)
        if not match or match.end() == pos:
            raise FilterError(f"unexpected character {text[pos]!r} at position {pos + 1}")
        tokens.append(match.group(1) or match.group(2))
        pos = match.end()
    return tokens


def constant(text: str) -> int:
    if text in FILTER_CONSTANTS:
        return FILTER_CONSTANTS[text]
    try:
        if re.fullmatch(r"0[0-7]+", text):
            return int(text, 8)
        return int(text, 0)
    except ValueError:
        raise FilterError(f"'{text}' is not a number or a known constant ({', '.join(sorted(FILTER_CONSTANTS))})") from None


def is_number(text: Optional[str]) -> bool:
    return text is not None and (text in FILTER_CONSTANTS or re.fullmatch(r"0[xX][0-9a-fA-F]+|[0-9]+", text) is not None)


def load(d: Decoded, base: str, offset: int, size: int) -> Optional[int]:
    if base == "ip":
        start = d.net_offset if d.version == 4 else None
    elif base == "ip6":
        start = d.net_offset if d.version == 6 else None
    else:
        start = d.l4_offset if d.version == 4 and d.proto == ACCESSOR_BASES[base] else None
    if start is None:
        return None
    end = start + offset + size
    if end > len(d.packet.data):
        raise OutOfPacket(f"{base}[{offset}:{size}] reads past the {len(d.packet.data)} captured bytes")
    return int.from_bytes(d.packet.data[start + offset:end], "big")


def direction_test(direction: str, on_src: bool, on_dst: bool) -> bool:
    if direction == "src":
        return on_src
    if direction == "dst":
        return on_dst
    if direction == "src and dst":
        return on_src and on_dst
    return on_src or on_dst


def protocol_node(proto: str) -> Node:
    tests: Dict[str, Tuple[Callable[[Decoded], bool], str]] = {
        "ip": (lambda d: d.version == 4, "ethertype 0x0800 (IPv4)"),
        "ip6": (lambda d: d.version == 6, "ethertype 0x86dd (IPv6)"),
        "arp": (lambda d: d.ethertype == ETHERTYPE_ARP, "ethertype 0x0806 (ARP)"),
        "tcp": (lambda d: d.version in (4, 6) and d.proto == IPPROTO_TCP, "IPv4 protocol or IPv6 next header 6"),
        "udp": (lambda d: d.version in (4, 6) and d.proto == IPPROTO_UDP, "IPv4 protocol or IPv6 next header 17"),
        "icmp": (lambda d: d.version == 4 and d.proto == IPPROTO_ICMP, "IPv4 protocol 1"),
        "icmp6": (lambda d: d.version == 6 and d.proto == IPPROTO_ICMPV6, "IPv6 next header 58"),
    }
    test, explain = tests[proto]
    return Node(proto, test, explain)


def address_node(proto: Optional[str], direction: str, kind: str, value: str, label: str) -> Node:
    if proto not in (None, "ip", "ip6", "arp"):
        raise FilterError(f"'{proto} {kind}' is not valid: {kind} takes the qualifier ip, ip6 or arp, not {proto}")
    try:
        target: Union[IPAddress, ipaddress.IPv4Network, ipaddress.IPv6Network] = ipaddress.ip_address(value) if kind == "host" else ipaddress.ip_network(value, strict=True)
    except ValueError as exc:
        if kind == "host":
            raise FilterError(f"'{value}' is not an IP address; pcaplens does not resolve names (tcpdump would, once, before capturing)") from None
        raise FilterError(f"bad network '{value}': {exc}") from None
    if (proto == "ip6" and target.version != 6) or (proto in ("ip", "arp") and target.version != 4):
        raise FilterError(f"'{label}': {proto} needs an IPv{6 if proto == 'ip6' else 4} {kind}")
    matches = (lambda a: a == target) if kind == "host" else (lambda a: a.version == target.version and a in target)

    def test(d: Decoded) -> bool:
        if d.src is None or d.dst is None:
            return False
        if proto == "ip" and d.version != 4 or proto == "ip6" and d.version != 6 or proto == "arp" and d.arp_op is None:
            return False
        if proto is None and d.arp_op is not None and target.version != 4:
            return False
        return direction_test(direction, matches(d.src), matches(d.dst))

    scope = {"ip": "IPv4", "ip6": "IPv6", "arp": "ARP"}.get(proto or "", "IPv4, IPv6 or ARP")
    return Node(label, test, f"{scope} {direction} address {'==' if kind == 'host' else 'in'} {target}")


def port_node(proto: Optional[str], direction: str, kind: str, value: str, label: str) -> Node:
    if proto not in (None, "tcp", "udp"):
        raise FilterError(f"'{proto} {kind}' is not valid: {kind} takes the qualifier tcp or udp, not {proto}")
    low_text, _, high_text = value.partition("-") if kind == "portrange" else (value, "", value)
    try:
        low, high = (SERVICES.get(t) if t in SERVICES else int(t) for t in (low_text, high_text or low_text))
    except ValueError:
        raise FilterError(f"'{value}' is not a port number or one of {', '.join(sorted(SERVICES))}") from None
    if not 0 <= low <= high <= 65535:
        raise FilterError(f"'{value}' is not a valid {kind} (0-65535)")
    allowed = {"tcp": (IPPROTO_TCP,), "udp": (IPPROTO_UDP,)}.get(proto or "", (IPPROTO_TCP, IPPROTO_UDP))

    def test(d: Decoded) -> bool:
        if d.sport is None or d.dport is None or d.proto not in allowed:
            return False
        return direction_test(direction, low <= d.sport <= high, low <= d.dport <= high)

    shown = f"== {low}" if low == high else f"in {low}-{high}"
    scope = {"tcp": "TCP", "udp": "UDP"}.get(proto or "", "TCP or UDP")
    return Node(label, test, f"{scope} {direction} port {shown}; IPv4 fragments after the first never match")


class FilterParser:
    def __init__(self, text: str) -> None:
        self.tokens = tokenize(text)
        self.pos = 0
        self.last: Optional[Tuple[Optional[str], str, str]] = None

    def peek(self, ahead: int = 0) -> Optional[str]:
        index = self.pos + ahead
        return self.tokens[index] if index < len(self.tokens) else None

    def take(self) -> str:
        token = self.peek()
        if token is None:
            raise FilterError("the expression ends where more was expected")
        self.pos += 1
        return token

    def expect(self, token: str) -> None:
        found = self.peek()
        if found != token:
            raise FilterError(f"expected '{token}', found {repr(found) if found else 'the end of the expression'}")
        self.pos += 1

    def parse(self) -> Node:
        node = self.parse_expression()
        if self.peek() is not None:
            raise FilterError(f"unexpected '{self.peek()}' after a complete expression")
        return node

    def parse_expression(self) -> Node:
        node = self.parse_not()
        while self.peek() in ("and", "&&", "or", "||"):
            both = self.take() in ("and", "&&")
            left, right = node, self.parse_not()
            if both:
                node = Node("and", lambda d, a=left, b=right: a.test(d) and b.test(d), children=[left, right])
            else:
                node = Node("or", lambda d, a=left, b=right: a.test(d) or b.test(d), children=[left, right])
        return node

    def parse_not(self) -> Node:
        if self.peek() in ("not", "!"):
            self.take()
            inner = self.parse_not()
            return Node("not", lambda d, a=inner: not a.test(d), children=[inner])
        return self.parse_primary()

    def parse_primary(self) -> Node:
        token = self.peek()
        if token is None:
            raise FilterError("the expression ends where a primitive was expected")
        if token == "(":
            self.take()
            node = self.parse_expression()
            self.expect(")")
            return node
        if token in UNSUPPORTED_KEYWORDS:
            raise FilterError(f"'{token}' is a tcpdump keyword outside pcaplens' subset (host, net, port, portrange, src, dst, ip, ip6, arp, tcp, udp, icmp, icmp6, and, or, not, proto[offset:size])")
        if (token in ACCESSOR_BASES and self.peek(1) == "[") or (is_number(token) and self.peek(1) in set(RELOPS) | {"&", "|"}):
            return self.parse_comparison()
        return self.parse_primitive()

    def parse_primitive(self) -> Node:
        proto: Optional[str] = None
        direction: Optional[str] = None
        kind: Optional[str] = None
        if self.peek() in PROTO_KEYWORDS:
            proto = self.take()
            if self.peek() not in DIR_KEYWORDS | TYPE_KEYWORDS:
                return protocol_node(proto)
        if self.peek() in DIR_KEYWORDS:
            direction = self.take()
            if self.peek() in ("or", "and") and self.peek(1) in DIR_KEYWORDS - {direction}:
                direction = f"src {self.take()} dst"
                self.take()
        if self.peek() in TYPE_KEYWORDS:
            kind = self.take()
        value = self.peek()
        if value is None or value in KEYWORDS or value in OPERATOR_TOKENS:
            written = " ".join(p for p in (proto, direction, kind) if p) or "the start"
            raise FilterError(f"expected an address, network or port after {written}, found {repr(value) if value else 'the end of the expression'}")
        self.take()
        if proto is None and direction is None and kind is None and self.last is not None:
            proto, direction, kind = self.last
        direction, kind = direction or "src or dst", kind or "host"
        self.last = (proto, direction, kind)
        label = " ".join(p for p in (proto, None if direction == "src or dst" else direction, kind, value) if p)
        if kind in ("host", "net"):
            return address_node(proto, direction, kind, value, label)
        return port_node(proto, direction, kind, value, label)

    def parse_comparison(self) -> Node:
        left = self.parse_bitor()
        op = self.peek()
        if op not in RELOPS:
            raise FilterError(f"expected a comparison (=, ==, !=, <, >, <=, >=) after {left.text}, found {repr(op) if op else 'the end of the expression'}")
        self.take()
        right = self.parse_bitor()
        compare = RELOPS[op]

        def test(d: Decoded) -> bool:
            a, b = left.get(d), right.get(d)
            return a is not None and b is not None and compare(a, b)

        return Node(f"{left.text} {op} {right.text}", test, "byte loads are IPv4-only for tcp, udp and icmp, first fragment only; a load past the captured bytes rejects the packet")

    def parse_bitor(self) -> Value:
        value = self.parse_bitand()
        while self.peek() == "|":
            self.take()
            left, right = value, self.parse_bitand()
            value = Value(f"{left.text} | {right.text}", lambda d, a=left, b=right: combine(a.get(d), b.get(d), lambda x, y: x | y))
        return value

    def parse_bitand(self) -> Value:
        value = self.parse_operand()
        while self.peek() == "&":
            self.take()
            left, right = value, self.parse_operand()
            value = Value(f"{left.text} & {right.text}", lambda d, a=left, b=right: combine(a.get(d), b.get(d), lambda x, y: x & y))
        return value

    def parse_operand(self) -> Value:
        token = self.take()
        if token == "(":
            inner = self.parse_bitor()
            self.expect(")")
            return Value(f"({inner.text})", inner.get)
        if token in ACCESSOR_BASES and self.peek() == "[":
            self.take()
            spec = self.take()
            self.expect("]")
            offset_text, _, size_text = spec.partition(":")
            offset, size = constant(offset_text), int(size_text) if size_text.isdigit() else 1
            if size_text and size_text not in ("1", "2", "4"):
                raise FilterError(f"{token}[{spec}]: the size after ':' must be 1, 2 or 4")
            return Value(f"{token}[{spec}]", lambda d, b=token, o=offset, s=size: load(d, b, o, s))
        number = constant(token)
        return Value(token, lambda d, n=number: n)


def combine(a: Optional[int], b: Optional[int], op: Callable[[int, int], int]) -> Optional[int]:
    return None if a is None or b is None else op(a, b)


@dataclass
class Filter:
    text: str
    root: Optional[Node]

    def __call__(self, d: Decoded) -> bool:
        if self.root is None:
            return True
        try:
            return self.root.test(d)
        except OutOfPacket:
            return False


def compile_filter(text: str) -> Filter:
    text = text.strip()
    return Filter(text, FilterParser(text).parse() if text else None)


def filter_tree(node: Node, depth: int = 0) -> List[str]:
    line = "  " * depth + node.text + (f"    -- {node.explain}" if node.explain else "")
    return [line] + [l for child in node.children for l in filter_tree(child, depth + 1)]


def filter_json(node: Node) -> Dict[str, object]:
    if node.children:
        return {"op": node.text, "children": [filter_json(c) for c in node.children]}
    return {"primitive": node.text, "checks": node.explain}


def unpack(fmt: str, data: bytes, offset: int) -> Tuple[int, ...]:
    try:
        return struct.unpack_from(fmt, data, offset)
    except struct.error:
        raise ValueError(f"message ends inside a field at byte {offset}") from None


def read_name(message: bytes, offset: int) -> Tuple[str, int]:
    labels: List[str] = []
    end: Optional[int] = None
    jumps = 0
    while True:
        if offset >= len(message):
            raise ValueError("a name runs past the end of the message")
        length = message[offset]
        if length & 0xC0 == 0xC0:
            pointer = unpack("!H", message, offset)[0] & 0x3FFF
            if end is None:
                end = offset + 2
            jumps += 1
            if jumps > 32:
                raise ValueError("compression pointer loop")
            offset = pointer
            continue
        if length & 0xC0:
            raise ValueError(f"label type 0x{length & 0xC0:02x} is not supported")
        if length == 0:
            return ".".join(labels) + ".", end if end is not None else offset + 1
        if offset + 1 + length > len(message):
            raise ValueError("a label runs past the end of the message")
        labels.append(message[offset + 1:offset + 1 + length].decode("ascii", "replace"))
        offset += 1 + length


@dataclass
class DnsRecord:
    name: str
    rtype: int
    rclass: int
    ttl: int
    data: str

    @property
    def type_name(self) -> str:
        return DNS_TYPES.get(self.rtype, f"TYPE{self.rtype}")


@dataclass
class DnsMessage:
    ident: int
    response: bool
    opcode: int
    aa: bool
    tc: bool
    rd: bool
    ra: bool
    rcode: int
    ad: bool = False
    cd: bool = False
    questions: List[Tuple[str, int]] = field(default_factory=list)
    answers: List[DnsRecord] = field(default_factory=list)
    authority: List[DnsRecord] = field(default_factory=list)
    additional: List[DnsRecord] = field(default_factory=list)

    @property
    def rcode_name(self) -> str:
        return DNS_RCODES.get(self.rcode, f"RCODE{self.rcode}")


def rdata_text(message: bytes, offset: int, rtype: int, rclass: int, length: int) -> str:
    rdata = message[offset:offset + length]
    if rtype == 1 and length == 4:
        return str(ipaddress.IPv4Address(rdata))
    if rtype == 28 and length == 16:
        return str(ipaddress.IPv6Address(rdata))
    if rtype in (2, 5, 12):
        return read_name(message, offset)[0]
    if rtype == 15:
        return f"{unpack('!H', message, offset)[0]} {read_name(message, offset + 2)[0]}"
    if rtype == 33:
        priority, weight, port = unpack("!HHH", message, offset)
        return f"{priority} {weight} {port} {read_name(message, offset + 6)[0]}"
    if rtype == 6:
        mname, cursor = read_name(message, offset)
        rname, cursor = read_name(message, cursor)
        return " ".join([mname, rname] + [str(n) for n in unpack("!5I", message, cursor)])
    if rtype == 16:
        strings, i = [], 0
        while i < len(rdata):
            strings.append('"' + rdata[i + 1:i + 1 + rdata[i]].decode("utf-8", "replace") + '"')
            i += 1 + rdata[i]
        return " ".join(strings)
    if rtype == 41:
        return f"udp payload size {rclass}, {length} bytes of options"
    return rdata.hex()


def parse_dns(message: bytes) -> DnsMessage:
    if len(message) < 12:
        raise ValueError(f"a DNS header is 12 bytes, the payload has {len(message)}")
    ident, flags, qdcount, ancount, nscount, arcount = struct.unpack_from("!6H", message)
    msg = DnsMessage(ident, bool(flags & 0x8000), (flags >> 11) & 0xF, bool(flags & 0x0400), bool(flags & 0x0200), bool(flags & 0x0100), bool(flags & 0x0080), flags & 0xF, bool(flags & 0x0020), bool(flags & 0x0010))
    offset = 12
    for _ in range(qdcount):
        name, offset = read_name(message, offset)
        qtype, _qclass = unpack("!HH", message, offset)
        msg.questions.append((name, qtype))
        offset += 4
    for section, count in ((msg.answers, ancount), (msg.authority, nscount), (msg.additional, arcount)):
        for _ in range(count):
            name, offset = read_name(message, offset)
            rtype, rclass, ttl, rdlength = unpack("!HHIH", message, offset)
            offset += 10
            if offset + rdlength > len(message):
                raise ValueError(f"record {name} {DNS_TYPES.get(rtype, rtype)} claims {rdlength} bytes of data, {len(message) - offset} left")
            section.append(DnsRecord(name, rtype, rclass, ttl, rdata_text(message, offset, rtype, rclass, rdlength)))
            offset += rdlength
    return msg


def dns_name(name: str) -> bytes:
    out = b""
    for label in name.rstrip(".").split("."):
        if label:
            out += bytes([len(label)]) + label.encode("ascii")
    return out + b"\0"


def dns_message(ident: int, flags: int, questions: Sequence[Tuple[str, int]], answers: Sequence[Tuple[str, int, int, bytes]] = (), authority: Sequence[Tuple[str, int, int, bytes]] = ()) -> bytes:
    out = struct.pack("!6H", ident, flags, len(questions), len(answers), len(authority), 0)
    for name, qtype in questions:
        out += dns_name(name) + struct.pack("!HH", qtype, 1)
    first = questions[0][0].rstrip(".") if questions else None
    for name, rtype, ttl, rdata in list(answers) + list(authority):
        owner = b"\xc0\x0c" if name.rstrip(".") == first else dns_name(name)
        out += owner + struct.pack("!HHIH", rtype, 1, ttl, len(rdata)) + rdata
    return out


def is_dns(d: Decoded, port: int = 53) -> bool:
    return d.proto == IPPROTO_UDP and d.sport is not None and port in (d.sport, d.dport)


def dns_text(msg: DnsMessage, length: int) -> str:
    if not msg.response:
        name, qtype = msg.questions[0] if msg.questions else ("?", 0)
        extra = f" [{len(msg.additional)}au]" if msg.additional else ""
        return f"{msg.ident}{'+' if msg.rd else ''}{'%' if msg.cd else ''}{extra} {DNS_TYPES.get(qtype, qtype)}? {name} ({length})"
    marks = ("*" if msg.aa else "") + ("" if msg.ra else "-") + ("|" if msg.tc else "") + ("$" if msg.ad else "")
    rcode = f" {TCPDUMP_RCODES.get(msg.rcode, msg.rcode_name)}" if msg.rcode else ""
    answers = ", ".join(f"{r.type_name} {r.data}" for r in msg.answers)
    counts = f"{len(msg.answers)}/{len(msg.authority)}/{len(msg.additional)}"
    return f"{msg.ident}{rcode}{marks} {counts}{' ' + answers if answers else ''} ({length})"


def flags_text(flags: int) -> str:
    return "".join(letter for bit, letter in FLAG_LETTERS if flags & bit) or "none"


def tcp_text(d: Decoded) -> str:
    t = d.tcp
    assert t is not None
    parts = [f"Flags [{flags_text(t.flags)}]"]
    if d.payload_len:
        parts.append(f"seq {t.seq}:{(t.seq + d.payload_len) & MASK32}")
    elif t.flags & (SYN | FIN | RST):
        parts.append(f"seq {t.seq}")
    if t.flags & ACK:
        parts.append(f"ack {t.ack}")
    parts.append(f"win {t.window}")
    if t.options:
        parts.append(f"options [{','.join(t.options)}]")
    parts.append(f"length {d.payload_len}")
    return ", ".join(parts)


def summary(d: Decoded, dns_port: int = 53) -> str:
    prefix = ""
    if d.link in ("sll", "sll2"):
        prefix = (f"ifindex {d.ifindex} " if d.ifindex is not None else "") + f"{d.direction} "
    if d.arp_op is not None:
        what = f"Request who-has {d.dst} tell {d.src}" if d.arp_op == 1 else f"Reply {d.src} is-at {d.arp_mac}"
        return f"{prefix}ARP, {what}, length {d.packet.orig_len - (d.net_offset or 0)}"
    if d.version is None:
        kind = f"ethertype 0x{d.ethertype:04x}" if d.ethertype is not None else "unknown link payload"
        return f"{prefix}{kind}{' ' + d.problem if d.problem else ''}, length {d.packet.orig_len}"
    family = "IP" if d.version == 4 else "IP6"
    ends = f"{d.src}.{d.sport} > {d.dst}.{d.dport}" if d.sport is not None else f"{d.src} > {d.dst}"
    if d.tcp is not None:
        body = tcp_text(d)
    elif d.proto == IPPROTO_UDP and d.sport is not None and d.problem is None:
        body = f"UDP, length {d.payload_len}"
        if is_dns(d, dns_port):
            try:
                body = dns_text(parse_dns(d.payload), d.payload_len)
            except ValueError:
                body = f"[|domain] UDP, length {d.payload_len}"
    elif d.icmp is not None:
        names = ICMP_TYPES if d.version == 4 else ICMP6_TYPES
        body = f"ICMP{'' if d.version == 4 else '6'} {names.get(d.icmp[0], f'type {d.icmp[0]} code {d.icmp[1]}')}, length {d.l4_len}"
    elif not d.first_fragment:
        body = f"fragment of {PROTO_NAMES.get(d.proto or 0, f'ip-proto-{d.proto}')}, length {d.l4_len}"
    else:
        body = f"{PROTO_NAMES.get(d.proto or 0, f'ip-proto-{d.proto}')}, length {d.l4_len}"
    if d.problem:
        body = f"{body} {d.problem}"
    return f"{prefix}{family} {ends}: {body}"


def format_time(ts_ns: int, mode: str = "abs", first_ns: int = 0) -> str:
    if mode == "rel":
        ts_ns -= first_ns
    if mode in ("rel", "epoch"):
        return f"{ts_ns // NS}.{ts_ns % NS // 1000:06d}"
    stamp = datetime.fromtimestamp(ts_ns // NS, tz=timezone.utc)
    return stamp.strftime("%Y-%m-%d %H:%M:%S") + f".{ts_ns % NS // 1000:06d}"


def endpoint_text(endpoint: Endpoint) -> str:
    host, port = endpoint
    return f"[{host}]:{port}" if ":" in host else f"{host}:{port}"


def seq_lt(a: int, b: int) -> bool:
    return a != b and ((b - a) & MASK32) < 0x80000000


@dataclass
class Half:
    packets: int = 0
    bytes: int = 0
    next_seq: Optional[int] = None
    last_ack: Optional[int] = None
    last_window: Optional[int] = None
    mss: Optional[int] = None
    wscale: Optional[int] = None
    sack_ok: bool = False
    syns: int = 0
    fins: int = 0
    rsts: int = 0
    retransmissions: int = 0
    gaps: int = 0
    dup_acks: int = 0
    zero_windows: int = 0
    keepalives: int = 0


@dataclass
class Flow:
    proto: str
    client: Endpoint
    server: Endpoint
    first_ns: int
    last_ns: int
    guessed: bool = False
    up: Half = field(default_factory=Half)
    down: Half = field(default_factory=Half)
    syn_ns: Optional[int] = None
    synack_ns: Optional[int] = None
    ack_ns: Optional[int] = None
    client_isn: Optional[int] = None
    server_isn: Optional[int] = None
    closed_by: Optional[str] = None
    closed_ns: Optional[int] = None

    @property
    def state(self) -> str:
        if self.proto == "udp":
            return "udp"
        if self.syn_ns is not None and self.synack_ns is None:
            return "refused" if self.down.rsts else "no answer"
        if self.up.rsts or self.down.rsts:
            return "reset"
        if self.up.fins and self.down.fins:
            return "closed"
        if self.up.fins or self.down.fins:
            return "half-closed"
        if self.syn_ns is None:
            return "mid-stream"
        return "open" if self.ack_ns is not None else "handshake incomplete"

    @property
    def rtt_ns(self) -> Optional[int]:
        return self.ack_ns - self.syn_ns if self.ack_ns is not None and self.syn_ns is not None else None


def flow_key(d: Decoded) -> Tuple[int, Endpoint, Endpoint]:
    a, b = sorted([(str(d.src), d.sport or 0), (str(d.dst), d.dport or 0)])
    return (d.proto or 0, a, b)


def new_flow(d: Decoded, sender: Endpoint, receiver: Endpoint) -> Flow:
    if d.tcp is None:
        return Flow("udp", sender, receiver, d.packet.ts_ns, d.packet.ts_ns)
    if d.tcp.flags & SYN:
        client, server = (receiver, sender) if d.tcp.flags & ACK else (sender, receiver)
        return Flow("tcp", client, server, d.packet.ts_ns, d.packet.ts_ns)
    client, server = (sender, receiver) if sender[1] > receiver[1] else (receiver, sender)
    return Flow("tcp", client, server, d.packet.ts_ns, d.packet.ts_ns, guessed=True)


def starts_new_connection(flow: Flow, d: Decoded) -> bool:
    t = d.tcp
    return t is not None and t.flags & (SYN | ACK) == SYN and flow.closed_by is not None and t.seq != flow.client_isn


def classify_segment(half: Half, other: Half, t: Tcp, payload_len: int) -> None:
    seg_len = payload_len + (1 if t.flags & SYN else 0) + (1 if t.flags & FIN else 0)
    seq_end = (t.seq + seg_len) & MASK32
    if t.flags & RST:
        return
    if half.next_seq is None:
        half.next_seq = seq_end
    elif payload_len <= 1 and not t.flags & (SYN | FIN) and t.seq == (half.next_seq - 1) & MASK32:
        half.keepalives += 1
    elif seg_len > 0 and seq_lt(t.seq, half.next_seq):
        half.retransmissions += 1
        if seq_lt(half.next_seq, seq_end):
            half.next_seq = seq_end
    elif seg_len > 0:
        if t.seq != half.next_seq:
            half.gaps += 1
        half.next_seq = seq_end
    if t.flags & ACK:
        pure = payload_len == 0 and not t.flags & (SYN | FIN)
        if pure and half.last_ack == t.ack and half.last_window == t.window and other.next_seq is not None and seq_lt(t.ack, other.next_seq):
            half.dup_acks += 1
        half.last_ack, half.last_window = t.ack, t.window


def track_tcp(flow: Flow, half: Half, other: Half, d: Decoded, from_client: bool) -> None:
    t = d.tcp
    assert t is not None
    ts = d.packet.ts_ns
    if t.flags & SYN:
        half.syns += 1
        half.mss = t.mss if t.mss is not None else half.mss
        half.wscale = t.wscale if t.wscale is not None else half.wscale
        half.sack_ok = half.sack_ok or t.sack_ok
        if from_client and not t.flags & ACK and flow.syn_ns is None:
            flow.syn_ns, flow.client_isn = ts, t.seq
        if not from_client and t.flags & ACK and flow.synack_ns is None:
            flow.synack_ns, flow.server_isn = ts, t.seq
    if from_client and flow.server_isn is not None and flow.ack_ns is None and t.flags & (SYN | ACK) == ACK and t.ack == (flow.server_isn + 1) & MASK32:
        flow.ack_ns = ts
    if t.flags & RST:
        half.rsts += 1
    if t.flags & FIN:
        half.fins += 1
    if t.flags & (FIN | RST) and flow.closed_by is None:
        flow.closed_by = f"{'client' if from_client else 'server'} {'RST' if t.flags & RST else 'FIN'}"
        flow.closed_ns = ts
    if t.window == 0 and not t.flags & (SYN | FIN | RST):
        half.zero_windows += 1
    classify_segment(half, other, t, d.payload_len)


def build_flows(decoded: Iterable[Decoded]) -> List[Flow]:
    flows: Dict[Tuple[int, Endpoint, Endpoint], Flow] = {}
    order: List[Flow] = []
    for d in decoded:
        if d.sport is None or d.dport is None or d.proto not in (IPPROTO_TCP, IPPROTO_UDP):
            continue
        if d.proto == IPPROTO_TCP and d.tcp is None:
            continue
        key = flow_key(d)
        sender, receiver = (str(d.src), d.sport), (str(d.dst), d.dport)
        flow = flows.get(key)
        if flow is None or starts_new_connection(flow, d):
            flow = new_flow(d, sender, receiver)
            flows[key] = flow
            order.append(flow)
        flow.last_ns = d.packet.ts_ns
        from_client = sender == flow.client
        half, other = (flow.up, flow.down) if from_client else (flow.down, flow.up)
        half.packets += 1
        half.bytes += d.payload_len
        if d.tcp is not None:
            track_tcp(flow, half, other, d, from_client)
    return order


def ms(value: Optional[int]) -> Optional[float]:
    return None if value is None else round(value / 1_000_000, 3)


def flow_notes(f: Flow) -> List[str]:
    notes: List[str] = []
    server = endpoint_text(f.server)
    if f.state == "refused":
        notes.append(f"SYN answered with RST: nothing listens on {server}, or a firewall rejects with RST")
    if f.state == "no answer":
        notes.append(f"{f.up.syns} SYN(s) and no reply: a firewall or security group drops them, the route is wrong, or the host is down")
    for name, half in (("client", f.up), ("server", f.down)):
        if half.retransmissions and f.state != "no answer":
            notes.append(f"{name} retransmitted {half.retransmissions} segment(s): loss or delay on the path from the {name}")
        if half.gaps:
            notes.append(f"{half.gaps} {name} segment(s) arrived after a hole in the sequence space: lost before the capture point, or dropped by the capture itself")
        if half.dup_acks:
            notes.append(f"{name} sent {half.dup_acks} duplicate ACK(s): it is waiting for a segment from the other side")
        if half.zero_windows:
            notes.append(f"{name} advertised a zero window {half.zero_windows} time(s): its application stopped reading")
        if half.keepalives:
            notes.append(f"{name} sent {half.keepalives} keep-alive or window probe(s)")
    if f.state == "reset" and f.closed_by and f.closed_ns is not None:
        notes.append(f"reset by the {f.closed_by.split()[0]} {(f.closed_ns - f.first_ns) / NS:.3f} s after the first packet")
    if f.guessed:
        notes.append("no SYN in the capture: client guessed from the higher port, handshake RTT and window scale unknown")
    return notes


def flow_json(f: Flow) -> Dict[str, object]:
    sides = {"client": f.up, "server": f.down}

    def each(attribute: str) -> Dict[str, object]:
        return {name: getattr(half, attribute) for name, half in sides.items()}

    out: Dict[str, object] = {
        "proto": f.proto,
        "client": endpoint_text(f.client),
        "server": endpoint_text(f.server),
        "state": f.state,
        "duration_ms": ms(f.last_ns - f.first_ns),
        "packets": each("packets"),
        "bytes": each("bytes"),
    }
    if f.proto == "tcp":
        out.update({
            "handshake": None if f.syn_ns is None else {"syn_to_synack_ms": ms(None if f.synack_ns is None else f.synack_ns - f.syn_ns), "rtt_ms": ms(f.rtt_ns)},
            "mss": each("mss"),
            "wscale": each("wscale"),
            "sack_permitted": each("sack_ok"),
            "syns": each("syns"),
            "retransmissions": each("retransmissions"),
            "gaps": each("gaps"),
            "dup_acks": each("dup_acks"),
            "zero_windows": each("zero_windows"),
            "keepalives": each("keepalives"),
            "fins": each("fins"),
            "rsts": each("rsts"),
            "closed_by": f.closed_by,
            "direction_guessed": f.guessed,
        })
    out["notes"] = flow_notes(f)
    return out


def render_flows(flows: List[Flow]) -> str:
    lines = [f"{'proto':5} {'client':24} {'server':24} {'state':12} {'pkts':>5} {'c>s bytes':>10} {'s>c bytes':>10} {'rtt ms':>8} {'retx':>5} {'dupack':>6} {'zwin':>5}  closed by"]
    for f in flows:
        tcp = f.proto == "tcp"
        rtt = ms(f.rtt_ns)
        lines.append(" ".join([
            f"{f.proto:5}",
            f"{endpoint_text(f.client):24}",
            f"{endpoint_text(f.server):24}",
            f"{f.state:12}",
            f"{f.up.packets + f.down.packets:>5}",
            f"{f.up.bytes:>10}",
            f"{f.down.bytes:>10}",
            f"{'-' if rtt is None else f'{rtt:.3f}':>8}",
            f"{f.up.retransmissions + f.down.retransmissions if tcp else '-':>5}",
            f"{f.up.dup_acks + f.down.dup_acks if tcp else '-':>6}",
            f"{f.up.zero_windows + f.down.zero_windows if tcp else '-':>5}",
            f" {f.closed_by or '-'}",
        ]))
        lines.extend(f"      - {note}" for note in flow_notes(f))
    return "\n".join(lines)


@dataclass
class DnsTransaction:
    ident: int
    client: Endpoint
    server: Endpoint
    qname: str
    qtype: str
    query_ns: Optional[int]
    response_ns: Optional[int] = None
    retries: int = 0
    rcode: Optional[str] = None
    flags: str = ""
    answers: List[DnsRecord] = field(default_factory=list)
    authority: List[DnsRecord] = field(default_factory=list)

    @property
    def latency_ns(self) -> Optional[int]:
        if self.query_ns is None or self.response_ns is None:
            return None
        return self.response_ns - self.query_ns


def dns_transactions(decoded: Iterable[Decoded], port: int = 53) -> List[DnsTransaction]:
    pending: Dict[Tuple[Endpoint, Endpoint, int], DnsTransaction] = {}
    out: List[DnsTransaction] = []
    for d in decoded:
        if not is_dns(d, port):
            continue
        try:
            msg = parse_dns(d.payload)
        except ValueError:
            continue
        sender, receiver = (str(d.src), d.sport or 0), (str(d.dst), d.dport or 0)
        qname, qtype = msg.questions[0] if msg.questions else ("?", 0)
        if not msg.response:
            key = (sender, receiver, msg.ident)
            if key in pending:
                pending[key].retries += 1
                continue
            pending[key] = DnsTransaction(msg.ident, sender, receiver, qname, DNS_TYPES.get(qtype, str(qtype)), d.packet.ts_ns)
            out.append(pending[key])
            continue
        transaction = pending.pop((receiver, sender, msg.ident), None)
        if transaction is None:
            transaction = DnsTransaction(msg.ident, receiver, sender, qname, DNS_TYPES.get(qtype, str(qtype)), None)
            out.append(transaction)
        transaction.response_ns, transaction.rcode = d.packet.ts_ns, msg.rcode_name
        transaction.flags = " ".join(n for n, on in (("aa", msg.aa), ("tc", msg.tc), ("rd", msg.rd), ("ra", msg.ra)) if on)
        transaction.answers, transaction.authority = msg.answers, msg.authority
    return out


def record_json(r: DnsRecord) -> Dict[str, object]:
    return {"name": r.name, "type": r.type_name, "ttl": r.ttl, "data": r.data}


def transaction_json(t: DnsTransaction) -> Dict[str, object]:
    return {
        "id": t.ident,
        "client": endpoint_text(t.client),
        "server": endpoint_text(t.server),
        "question": {"name": t.qname, "type": t.qtype},
        "query_time": None if t.query_ns is None else format_time(t.query_ns),
        "latency_ms": ms(t.latency_ns),
        "retries": t.retries,
        "rcode": t.rcode,
        "flags": t.flags,
        "answers": [record_json(r) for r in t.answers],
        "authority": [record_json(r) for r in t.authority],
    }


def render_dns(transactions: List[DnsTransaction]) -> str:
    lines = [f"{'time':26} {'id':>5} {'client':22} {'query':34} {'rcode':9} {'ms':>9} {'retry':>5}  answers"]
    for t in transactions:
        when = format_time(t.query_ns if t.query_ns is not None else t.response_ns or 0)
        latency = ms(t.latency_ns)
        answers = ", ".join(f"{r.type_name} {r.data} ttl {r.ttl}" for r in t.answers) or ("no answer; authority " + "; ".join(f"{r.type_name} {r.data}" for r in t.authority) if t.authority else "-")
        lines.append(f"{when:26} {t.ident:>5} {endpoint_text(t.client):22} {(t.qname + ' ' + t.qtype):34} {(t.rcode or 'no reply'):9} {'-' if latency is None else f'{latency:.3f}':>9} {t.retries:>5}  {answers}")
    return "\n".join(lines)


def packet_json(d: Decoded, dns_port: int = 53) -> Dict[str, object]:
    out: Dict[str, object] = {
        "index": d.packet.index,
        "ts_ns": d.packet.ts_ns,
        "time": format_time(d.packet.ts_ns),
        "link": d.link,
        "direction": d.direction,
        "ifindex": d.ifindex,
        "caplen": len(d.packet.data),
        "len": d.packet.orig_len,
        "version": d.version,
        "src": None if d.src is None else str(d.src),
        "dst": None if d.dst is None else str(d.dst),
        "proto": PROTO_NAMES.get(d.proto or 0, d.proto),
        "sport": d.sport,
        "dport": d.dport,
        "payload_len": d.payload_len,
        "problem": d.problem,
        "summary": summary(d, dns_port),
    }
    if d.tcp is not None:
        t = d.tcp
        out["tcp"] = {"flags": flags_text(t.flags), "seq": t.seq, "ack": t.ack, "window": t.window, "mss": t.mss, "wscale": t.wscale, "sack_ok": t.sack_ok, "timestamps": t.timestamps, "options": t.options}
    if is_dns(d, dns_port):
        try:
            msg = parse_dns(d.payload)
            out["dns"] = {"id": msg.ident, "response": msg.response, "rcode": msg.rcode_name, "questions": [{"name": n, "type": DNS_TYPES.get(q, q)} for n, q in msg.questions], "answers": [record_json(r) for r in msg.answers]}
        except ValueError as exc:
            out["dns"] = {"error": str(exc)}
    return out


def capture_info(capture: Capture) -> Dict[str, object]:
    packets = capture.packets
    first = packets[0].ts_ns if packets else None
    last = packets[-1].ts_ns if packets else None
    return {
        "format": capture.format,
        "version": capture.version,
        "byte_order": capture.byte_order,
        "precision": capture.precision,
        "snaplen": capture.snaplen,
        "linktypes": [{"id": lt, "name": LINKTYPES.get(lt, "unknown")} for lt in capture.linktypes],
        "packets": len(packets),
        "truncated_packets": sum(1 for p in packets if p.truncated),
        "bytes_captured": sum(len(p.data) for p in packets),
        "bytes_on_wire": sum(p.orig_len for p in packets),
        "first": None if first is None else format_time(first),
        "last": None if last is None else format_time(last),
        "duration_s": None if first is None or last is None else round((last - first) / NS, 6),
        "damage": capture.damage,
    }


def demo_frames() -> List[Tuple[int, bytes]]:
    start = 1793523600 * NS
    packets: List[Tuple[int, bytes]] = []
    client, web, dead, resolver = "10.0.0.1", "10.0.0.2", "10.0.0.3", "10.0.0.53"

    def tcp(at_ms: float, src: str, dst: str, sport: int, dport: int, seq: int, ack: int, flags: int, window: int = 502, options: bytes = b"", payload: bytes = b"") -> None:
        packets.append((start + int(round(at_ms * 1_000_000)), ip_packet(src, dst, IPPROTO_TCP, tcp_segment(src, dst, sport, dport, seq, ack, flags, window, options, payload))))

    def udp(at_ms: float, src: str, dst: str, sport: int, dport: int, payload: bytes) -> None:
        packets.append((start + int(round(at_ms * 1_000_000)), ip_packet(src, dst, IPPROTO_UDP, udp_datagram(src, dst, sport, dport, payload))))

    syn_options = tcp_options(mss=1460, sack_ok=True, timestamps=(1000, 0), wscale=7)
    request = b"GET /report.csv HTTP/1.1\r\nHost: 10.0.0.2\r\nUser-Agent: curl/8.5.0\r\nAccept: */*\r\n\r\n"
    c_seq, s_seq = 1001, 5001
    end = c_seq + len(request)
    tcp(0, client, web, 43512, 80, 1000, 0, SYN, 64240, syn_options)
    tcp(20, web, client, 80, 43512, 5000, c_seq, SYN | ACK, 65160, tcp_options(mss=1460, sack_ok=True, timestamps=(9000, 1000), wscale=7))
    tcp(20.1, client, web, 43512, 80, c_seq, s_seq, ACK)
    tcp(20.2, client, web, 43512, 80, c_seq, s_seq, PSH | ACK, payload=request)
    tcp(40.3, web, client, 80, 43512, s_seq, end, ACK, 509)
    tcp(40.5, web, client, 80, 43512, s_seq, end, ACK, 509, payload=b"H" * 1448)
    tcp(40.6, client, web, 43512, 80, end, s_seq + 1448, ACK)
    for i, at in enumerate((40.7, 40.9, 41.1)):
        size = 1000 if i == 2 else 1448
        seq = s_seq + 1448 * (i + 2)
        tcp(at, web, client, 80, 43512, seq, end, (PSH | ACK) if i == 2 else ACK, 509, payload=b"D" * size)
        tcp(at + 0.05, client, web, 43512, 80, end, s_seq + 1448, ACK, options=tcp_options(sack=[(s_seq + 2896, seq + size)]))
    tcp(61.2, web, client, 80, 43512, s_seq + 1448, end, ACK, 509, payload=b"R" * 1448)
    total = s_seq + 1448 * 4 + 1000
    tcp(61.3, client, web, 43512, 80, end, total, ACK)
    tcp(61.4, client, web, 43512, 80, end, total, FIN | ACK)
    tcp(81.4, web, client, 80, 43512, total, end + 1, FIN | ACK, 509)
    tcp(81.5, client, web, 43512, 80, end + 1, total + 1, ACK)

    tcp(100, client, web, 43514, 5432, 3000, 0, SYN, 64240, syn_options)
    tcp(100.2, web, client, 5432, 43514, 0, 3001, RST | ACK, 0)

    for at in (200, 1200, 3200):
        tcp(at, client, dead, 43516, 443, 4000, 0, SYN, 64240, syn_options)

    tcp(300, client, web, 43518, 9000, 7000, 0, SYN, 64240, syn_options)
    tcp(300.5, web, client, 9000, 43518, 9000, 7001, SYN | ACK, 65160, tcp_options(mss=1460, sack_ok=True, wscale=7))
    tcp(300.6, client, web, 43518, 9000, 7001, 9001, ACK)
    tcp(301, web, client, 9000, 43518, 9001, 7001, ACK, 509, payload=b"Z" * 1448)
    tcp(301.1, client, web, 43518, 9000, 7001, 10449, ACK, 11)
    tcp(301.5, web, client, 9000, 43518, 10449, 7001, PSH | ACK, 509, payload=b"Z" * 1448)
    tcp(301.6, client, web, 43518, 9000, 7001, 11897, ACK, 0)
    tcp(502, web, client, 9000, 43518, 11896, 7001, ACK, 509)
    tcp(502.5, client, web, 43518, 9000, 7001, 11897, ACK, 0)
    tcp(1500, client, web, 43518, 9000, 7001, 11897, RST | ACK, 0)

    a_record = ipaddress.IPv4Address("203.0.113.10").packed
    soa = dns_name("ns1.example.com") + dns_name("hostmaster.example.com") + struct.pack("!5I", 2026110101, 7200, 3600, 1209600, 60)
    udp(400, client, resolver, 53000, 53, dns_message(0x1A2B, 0x0100, [("app.example.com", 1)]))
    udp(412, resolver, client, 53, 53000, dns_message(0x1A2B, 0x8180, [("app.example.com", 1)], [("app.example.com", 1, 300, a_record)]))
    udp(420, client, resolver, 53001, 53, dns_message(0x1A2C, 0x0100, [("app.example.com", 28)]))
    udp(431, resolver, client, 53, 53001, dns_message(0x1A2C, 0x8180, [("app.example.com", 28)], authority=[("example.com", 6, 60, soa)]))
    udp(500, client, resolver, 53002, 53, dns_message(0x2B3C, 0x0100, [("nosuch.example.com", 1)]))
    udp(509, resolver, client, 53, 53002, dns_message(0x2B3C, 0x8183, [("nosuch.example.com", 1)], authority=[("example.com", 6, 60, soa)]))
    udp(600, client, resolver, 53003, 53, dns_message(0x3C4D, 0x0100, [("db.example.com", 1)]))
    udp(5600, client, resolver, 53003, 53, dns_message(0x3C4D, 0x0100, [("db.example.com", 1)]))
    udp(5610, resolver, client, 53, 53003, dns_message(0x3C4D, 0x8180, [("db.example.com", 1)], [("db.example.com", 1, 30, ipaddress.IPv4Address("203.0.113.20").packed)]))

    v6_client, v6_server = "2001:db8::1", "2001:db8::2"
    tcp(700, v6_client, v6_server, 50000, 443, 100, 0, SYN, 64800, tcp_options(mss=1440, sack_ok=True, wscale=7))
    tcp(710, v6_server, v6_client, 443, 50000, 200, 101, SYN | ACK, 64800, tcp_options(mss=1440, sack_ok=True, wscale=7))
    tcp(710.1, v6_client, v6_server, 50000, 443, 101, 201, ACK)
    tcp(720, v6_client, v6_server, 50000, 443, 101, 201, FIN | ACK)
    tcp(730, v6_server, v6_client, 443, 50000, 201, 102, FIN | ACK)
    tcp(730.1, v6_client, v6_server, 50000, 443, 102, 202, ACK)
    return sorted(packets, key=lambda item: item[0])


def demo_capture(linktype: int = LINKTYPE_ETHERNET, fmt: str = "pcap") -> bytes:
    local = ("10.0.0.1", "2001:db8::1")
    frames = []
    for ts, packet in demo_frames():
        version = packet[0] >> 4
        src = str(ipaddress.ip_address(packet[12:16] if version == 4 else packet[8:24]))
        frames.append((ts, link_frame(packet, linktype, outgoing=src in local)))
    return write_pcapng(frames, linktype) if fmt == "pcapng" else write_pcap(frames, linktype)


def read_bytes(path: str) -> bytes:
    if path == "-":
        return sys.stdin.buffer.read()
    with open(path, "rb") as handle:
        return handle.read()


def load_capture(args: argparse.Namespace) -> Tuple[Capture, List[Decoded]]:
    capture = read_capture(read_bytes(args.file))
    selected = compile_filter(" ".join(getattr(args, "filter", None) or []))
    return capture, [d for d in (decode(p) for p in capture.packets) if selected(d)]


def finish(capture: Capture) -> int:
    if capture.damage:
        print(f"pcaplens: {capture.damage}", file=sys.stderr)
        return 1
    return 0


def cmd_info(args: argparse.Namespace) -> int:
    capture = read_capture(read_bytes(args.file))
    data = capture_info(capture)
    if args.json:
        print(json.dumps(data, indent=2))
    else:
        links = ", ".join(f"{l['name']} ({l['id']})" for l in data["linktypes"])
        print(f"{args.file}: {capture.format} {capture.version}, {capture.byte_order}-endian, {capture.precision} timestamps, snaplen {capture.snaplen}, link type {links}")
        print(f"  {data['packets']} packets, {data['truncated_packets']} cut by the snaplen, {data['bytes_captured']} bytes captured of {data['bytes_on_wire']} on the wire")
        print(f"  {data['first']} to {data['last']} ({data['duration_s']} s)")
    return finish(capture)


def cmd_read(args: argparse.Namespace) -> int:
    capture, decoded = load_capture(args)
    if args.limit:
        decoded = decoded[:args.limit]
    if args.count:
        print(len(decoded))
    elif args.json:
        print(json.dumps([packet_json(d, args.dns_port) for d in decoded], indent=2))
    else:
        first = capture.packets[0].ts_ns if capture.packets else 0
        for d in decoded:
            print(f"{format_time(d.packet.ts_ns, args.time, first)} {summary(d, args.dns_port)}")
    return finish(capture)


def cmd_flows(args: argparse.Namespace) -> int:
    capture, decoded = load_capture(args)
    flows = build_flows(decoded)
    print(json.dumps([flow_json(f) for f in flows], indent=2) if args.json else render_flows(flows))
    return finish(capture)


def cmd_dns(args: argparse.Namespace) -> int:
    capture, decoded = load_capture(args)
    transactions = dns_transactions(decoded, args.port)
    print(json.dumps([transaction_json(t) for t in transactions], indent=2) if args.json else render_dns(transactions))
    return finish(capture)


def cmd_filter(args: argparse.Namespace) -> int:
    compiled = compile_filter(" ".join(args.expression))
    if compiled.root is None:
        print(json.dumps({"op": "accept all"}) if args.json else "(empty filter: every packet matches)")
    elif args.json:
        print(json.dumps(filter_json(compiled.root), indent=2))
    else:
        print("\n".join(filter_tree(compiled.root)))
    return 0


def cmd_demo(args: argparse.Namespace) -> int:
    linktype = LINK_BY_NAME[args.link]
    data = demo_capture(linktype, args.format)
    with open(args.out, "wb") as handle:
        handle.write(data)
    print(f"wrote {args.out}: {args.format}, link type {LINKTYPES[linktype]}, {len(demo_frames())} packets, {len(data)} bytes")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="pcaplens", description="Read pcap and pcapng files the way tcpdump -r does: decode, filter, follow connections and DNS. Filters go after the file, as one quoted argument or several words.")
    sub = parser.add_subparsers(dest="command", required=True)

    def with_capture(name: str, help_text: str, takes_filter: bool = True) -> argparse.ArgumentParser:
        command = sub.add_parser(name, help=help_text)
        command.add_argument("file", help="a .pcap or .pcapng file, or - for stdin (tcpdump -w - | pcaplens read -)")
        if takes_filter:
            command.add_argument("filter", nargs="*", help="tcpdump-style filter, e.g. 'tcp port 443 and host 10.0.0.5'")
        command.add_argument("--json", action="store_true", help="machine-readable output")
        return command

    with_capture("info", "file format, link type, snaplen, packet and truncation counts (like capinfos)", takes_filter=False)
    read = with_capture("read", "one line per matching packet, in the style of tcpdump -nn -S -r")
    read.add_argument("-c", dest="limit", type=int, default=0, help="stop after this many matching packets")
    read.add_argument("--count", action="store_true", help="print only the number of matching packets")
    read.add_argument("--time", choices=["abs", "epoch", "rel"], default="abs", help="abs: UTC date and time (tcpdump -tttt with TZ=UTC), epoch: tcpdump -tt, rel: seconds since the first packet")
    read.add_argument("--dns-port", type=int, default=53, help="UDP port whose payload is decoded as DNS")
    with_capture("flows", "one row per TCP or UDP 4-tuple: handshake RTT, bytes, retransmissions, duplicate ACKs, zero windows, who closed")
    dns = with_capture("dns", "DNS queries on UDP paired with their responses: latency, retries, rcode, answers")
    dns.add_argument("--port", type=int, default=53, help="UDP port to decode as DNS")
    expression = sub.add_parser("filter", help="parse a filter and show what each primitive checks (pcaplens' answer to tcpdump -d)")
    expression.add_argument("expression", nargs="+")
    expression.add_argument("--json", action="store_true")
    demo = sub.add_parser("demo", help="write a synthetic capture: a lossy HTTP transfer, a refused and an unanswered connection, a zero window, an RST, DNS and IPv6")
    demo.add_argument("out")
    demo.add_argument("--format", choices=["pcap", "pcapng"], default="pcap")
    demo.add_argument("--link", choices=sorted(LINK_BY_NAME), default="ether")
    return parser


COMMANDS: Dict[str, Callable[[argparse.Namespace], int]] = {
    "info": cmd_info,
    "read": cmd_read,
    "flows": cmd_flows,
    "dns": cmd_dns,
    "filter": cmd_filter,
    "demo": cmd_demo,
}


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(list(sys.argv[1:] if argv is None else argv))
    try:
        return COMMANDS[args.command](args)
    except (CaptureError, FilterError) as exc:
        kind = "filter" if isinstance(exc, FilterError) else "capture"
        print(f"pcaplens: {kind}: {exc}", file=sys.stderr)
        return 2
    except OSError as exc:
        print(f"pcaplens: {exc.filename or args.command}: {exc.strerror or exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
