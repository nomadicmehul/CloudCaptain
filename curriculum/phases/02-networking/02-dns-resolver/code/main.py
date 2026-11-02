"""minidig: the DNS wire format from RFC 1035 with EDNS0, queries over UDP with retry and TCP fallback, an iterative resolver that walks from root hints with a TTL and negative cache, and the search and ndots rules of resolv.conf."""
from __future__ import annotations

import argparse
import ipaddress
import json
import os
import re
import secrets
import socket
import struct
import sys
import time
from dataclasses import dataclass, field, replace
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

TYPES = {"A": 1, "NS": 2, "CNAME": 5, "SOA": 6, "PTR": 12, "MX": 15, "TXT": 16, "AAAA": 28, "SRV": 33, "OPT": 41, "ANY": 255}
TYPE_NAMES = {number: name for name, number in TYPES.items()}
A, NS, CNAME, SOA, PTR, MX, TXT, AAAA, SRV, OPT, ANY = (TYPES[t] for t in ("A", "NS", "CNAME", "SOA", "PTR", "MX", "TXT", "AAAA", "SRV", "OPT", "ANY"))
CLASSES = {"IN": 1, "CH": 3, "HS": 4}
CLASS_NAMES = {number: name for name, number in CLASSES.items()}
IN = 1
RCODES = {0: "NOERROR", 1: "FORMERR", 2: "SERVFAIL", 3: "NXDOMAIN", 4: "NOTIMP", 5: "REFUSED", 9: "NOTAUTH", 16: "BADVERS"}
NOERROR, FORMERR, SERVFAIL, NXDOMAIN, REFUSED = 0, 1, 2, 3, 5
OPCODES = {0: "QUERY", 1: "IQUERY", 2: "STATUS", 4: "NOTIFY", 5: "UPDATE"}
COMPRESSIBLE_RDATA = (NS, CNAME, PTR, MX, SOA)
EDNS_OPTIONS = {3: "NSID", 8: "CLIENT-SUBNET", 10: "COOKIE", 12: "PADDING", 15: "EDE"}
NAME_ESCAPES = '.\\"();@$'
DEFAULT_BUFSIZE = 1232
CLASSIC_UDP_SIZE = 512
MAX_LABEL = 63
MAX_NAME = 255
MAX_REFERRALS = 16
MAX_CNAMES = 8
MAX_GLUELESS_DEPTH = 3
MAX_NAMESERVERS = 3
MAX_NDOTS = 15
ROOT_HINTS = [
    ("a.root-servers.net.", "198.41.0.4"),
    ("b.root-servers.net.", "170.247.170.2"),
    ("c.root-servers.net.", "192.33.4.12"),
    ("d.root-servers.net.", "199.7.91.13"),
    ("e.root-servers.net.", "192.203.230.10"),
    ("f.root-servers.net.", "192.5.5.241"),
    ("g.root-servers.net.", "192.112.36.4"),
    ("h.root-servers.net.", "198.97.190.53"),
    ("i.root-servers.net.", "192.36.148.17"),
    ("j.root-servers.net.", "192.58.128.30"),
    ("k.root-servers.net.", "193.0.14.129"),
    ("l.root-servers.net.", "199.7.83.42"),
    ("m.root-servers.net.", "202.12.27.33"),
]
TXT_STRING = re.compile(r'"((?:[^"\\]|\\.)*)"|(\S+)')


class InputError(ValueError):
    pass


class WireError(ValueError):
    pass


class QueryError(Exception):
    pass


@dataclass(frozen=True)
class Soa:
    mname: str
    rname: str
    serial: int
    refresh: int
    retry: int
    expire: int
    minimum: int


@dataclass
class RR:
    name: str
    rtype: int
    rclass: int
    ttl: int
    data: Any


@dataclass
class Question:
    name: str
    qtype: int
    qclass: int = IN


@dataclass
class Edns:
    udp_size: int = DEFAULT_BUFSIZE
    version: int = 0
    do: bool = False
    ext_rcode: int = 0
    options: List[Tuple[int, bytes]] = field(default_factory=list)

    @property
    def payload_limit(self) -> int:
        return max(self.udp_size, CLASSIC_UDP_SIZE)


@dataclass
class Message:
    id: int = 0
    qr: bool = False
    opcode: int = 0
    aa: bool = False
    tc: bool = False
    rd: bool = False
    ra: bool = False
    ad: bool = False
    cd: bool = False
    rcode: int = NOERROR
    question: List[Question] = field(default_factory=list)
    answer: List[RR] = field(default_factory=list)
    authority: List[RR] = field(default_factory=list)
    additional: List[RR] = field(default_factory=list)
    edns: Optional[Edns] = None


def type_code(text: str) -> int:
    word = str(text).upper()
    if word in TYPES:
        return TYPES[word]
    if re.fullmatch(r"TYPE\d{1,5}", word) and int(word[4:]) < 65536:
        return int(word[4:])
    raise InputError(f"unknown record type {text!r} (known: {', '.join(t for t in TYPES if t != 'OPT')}, or TYPEnnn)")


def type_name(number: int) -> str:
    return TYPE_NAMES.get(number, f"TYPE{number}")


def class_name(number: int) -> str:
    return CLASS_NAMES.get(number, f"CLASS{number}")


def rcode_name(number: int) -> str:
    return RCODES.get(number, f"RCODE{number}")


def normalize(name: str) -> str:
    text = str(name).strip()
    if text in ("", "."):
        return "."
    if text.endswith(".") and not text.endswith("\\."):
        return text
    return text + "."


def same_name(a: str, b: str) -> bool:
    return normalize(a).lower() == normalize(b).lower()


def is_subdomain(name: str, zone: str) -> bool:
    child, parent = normalize(name).lower(), normalize(zone).lower()
    return parent == "." or child == parent or child.endswith("." + parent)


def ancestors(name: str) -> List[str]:
    text = normalize(name)
    if text == ".":
        return ["."]
    labels = text[:-1].split(".")
    return [".".join(labels[i:]) + "." for i in range(len(labels))] + ["."]


def name_to_labels(name: str) -> List[bytes]:
    text = normalize(name)
    if text == ".":
        return []
    labels: List[bytes] = []
    label = bytearray()
    i = 0
    while i < len(text):
        ch = text[i]
        if ch == "\\":
            digits = text[i + 1:i + 4]
            if len(digits) == 3 and digits.isdigit() and int(digits) < 256:
                label.append(int(digits))
                i += 4
                continue
            if i + 1 >= len(text):
                raise InputError(f"{name!r} ends with a lone backslash")
            label.extend(text[i + 1].encode("latin-1"))
            i += 2
            continue
        if ch == ".":
            if not label:
                raise InputError(f"{name!r} has an empty label (two dots in a row)")
            labels.append(bytes(label))
            label = bytearray()
        else:
            try:
                label.extend(ch.encode("ascii"))
            except UnicodeEncodeError:
                raise InputError(f"{name!r} is not ASCII; write internationalised names as punycode (xn--...)")
        i += 1
    for item in labels:
        if len(item) > MAX_LABEL:
            raise InputError(f"label {item[:16].decode('latin-1')}... is {len(item)} octets; a label holds at most {MAX_LABEL}")
    if sum(len(item) + 1 for item in labels) + 1 > MAX_NAME:
        raise InputError(f"{name!r} is longer than {MAX_NAME} octets on the wire")
    return labels


def label_text(label: bytes) -> str:
    out = []
    for byte in label:
        ch = chr(byte)
        if ch in NAME_ESCAPES:
            out.append("\\" + ch)
        elif 0x21 <= byte <= 0x7E:
            out.append(ch)
        else:
            out.append(f"\\{byte:03d}")
    return "".join(out)


def labels_to_name(labels: Sequence[bytes]) -> str:
    return ".".join(label_text(label) for label in labels) + "." if labels else "."


class Writer:
    def __init__(self, compress: bool = True):
        self.buf = bytearray()
        self.compress = compress
        self.offsets: Dict[Tuple[bytes, ...], int] = {}

    def name(self, name: str, compress: bool = True) -> None:
        labels = name_to_labels(name)
        for i in range(len(labels)):
            suffix = tuple(label.lower() for label in labels[i:])
            if compress and self.compress and suffix in self.offsets:
                self.buf += struct.pack("!H", 0xC000 | self.offsets[suffix])
                return
            if len(self.buf) < 0x4000:
                self.offsets.setdefault(suffix, len(self.buf))
            self.buf.append(len(labels[i]))
            self.buf += labels[i]
        self.buf.append(0)


def read_name(data: bytes, offset: int) -> Tuple[str, int]:
    labels: List[bytes] = []
    pos, end, wire_length = offset, None, 1
    visited = set()
    while True:
        if pos >= len(data):
            raise WireError(f"name at offset {offset} runs past the end of the message")
        length = data[pos]
        if length & 0xC0 == 0xC0:
            if pos + 1 >= len(data):
                raise WireError(f"compression pointer at offset {pos} is cut off")
            target = ((length & 0x3F) << 8) | data[pos + 1]
            if target >= pos:
                raise WireError(f"compression pointer at offset {pos} points forward to {target}; pointers must point to an earlier name")
            if target in visited:
                raise WireError(f"compression loop: offset {target} reached twice while reading the name at {offset}")
            visited.add(target)
            if end is None:
                end = pos + 2
            pos = target
            continue
        if length & 0xC0:
            raise WireError(f"label type {length >> 6:02b} at offset {pos} is not a length or a pointer")
        if length == 0:
            return labels_to_name(labels), (pos + 1 if end is None else end)
        label = data[pos + 1:pos + 1 + length]
        if len(label) < length:
            raise WireError(f"label at offset {pos} runs past the end of the message")
        wire_length += length + 1
        if wire_length > MAX_NAME:
            raise WireError(f"name at offset {offset} is longer than {MAX_NAME} octets")
        labels.append(label)
        pos += 1 + length


def quote_txt(value: bytes) -> str:
    out = []
    for byte in value:
        ch = chr(byte)
        if ch in '"\\':
            out.append("\\" + ch)
        elif 0x20 <= byte <= 0x7E:
            out.append(ch)
        else:
            out.append(f"\\{byte:03d}")
    return '"' + "".join(out) + '"'


def unescape_txt(raw: str) -> bytes:
    out = bytearray()
    i = 0
    while i < len(raw):
        if raw[i] == "\\" and i + 1 < len(raw):
            digits = raw[i + 1:i + 4]
            if len(digits) == 3 and digits.isdigit() and int(digits) < 256:
                out.append(int(digits))
                i += 4
                continue
            out.extend(raw[i + 1].encode("utf-8"))
            i += 2
            continue
        out.extend(raw[i].encode("utf-8"))
        i += 1
    return bytes(out)


def parse_txt(text: str) -> Tuple[bytes, ...]:
    parts = []
    for match in TXT_STRING.finditer(text):
        raw = match.group(1) if match.group(1) is not None else match.group(2)
        value = unescape_txt(raw)
        if len(value) > 255:
            raise InputError(f"a TXT character-string holds at most 255 octets, got {len(value)}; split it into several quoted strings")
        parts.append(value)
    if not parts:
        raise InputError("TXT data needs at least one string")
    return tuple(parts)


def parse_rdata(rtype: int, text: str) -> Any:
    words = text.split()
    try:
        if words[:1] == ["\\#"]:
            raw = bytes.fromhex("".join(words[2:]))
            if len(raw) != int(words[1]):
                raise InputError(f"\\# says {words[1]} octets, the hex has {len(raw)}")
            return raw
        if rtype == A:
            return str(ipaddress.IPv4Address(text.strip()))
        if rtype == AAAA:
            return str(ipaddress.IPv6Address(text.strip()))
        if rtype in (NS, CNAME, PTR):
            return normalize(words[0])
        if rtype == MX:
            return (int(words[0]), normalize(words[1]))
        if rtype == SRV:
            return (int(words[0]), int(words[1]), int(words[2]), normalize(words[3]))
        if rtype == SOA:
            numbers = [int(w) for w in words[2:7]]
            if len(numbers) != 5:
                raise InputError("SOA needs mname, rname and five numbers")
            return Soa(normalize(words[0]), normalize(words[1]), *numbers)
        if rtype == TXT:
            return parse_txt(text)
    except (ValueError, IndexError) as exc:
        if isinstance(exc, InputError):
            raise
        raise InputError(f"bad {type_name(rtype)} data {text!r}: {exc}")
    raise InputError(f"no text form for {type_name(rtype)}; use the RFC 3597 form \\# LENGTH HEX")


def make_rr(name: str, ttl: int, rtype: str, text: str, rclass: str = "IN") -> RR:
    code = type_code(rtype)
    return RR(normalize(name), code, CLASSES[rclass], int(ttl), parse_rdata(code, text))


def rdata_text(rr: RR) -> str:
    data = rr.data
    if isinstance(data, bytes):
        return f"\\# {len(data)} {data.hex().upper()}".rstrip()
    if rr.rtype in (A, AAAA, NS, CNAME, PTR):
        return str(data)
    if rr.rtype == MX:
        return f"{data[0]} {data[1]}"
    if rr.rtype == SRV:
        return f"{data[0]} {data[1]} {data[2]} {data[3]}"
    if rr.rtype == SOA:
        return f"{data.mname} {data.rname} {data.serial} {data.refresh} {data.retry} {data.expire} {data.minimum}"
    if rr.rtype == TXT:
        return " ".join(quote_txt(part) for part in data)
    if rr.rtype == OPT:
        return " ".join(f"{EDNS_OPTIONS.get(code, code)}={value.hex()}" for code, value in data)
    return str(data)


def rr_text(rr: RR) -> str:
    return f"{rr.name}\t{rr.ttl}\t{class_name(rr.rclass)}\t{type_name(rr.rtype)}\t{rdata_text(rr)}"


def rr_json(rr: RR) -> Dict[str, Any]:
    return {"name": rr.name, "ttl": rr.ttl, "class": class_name(rr.rclass), "type": type_name(rr.rtype), "data": rdata_text(rr)}


def rr_from_json(obj: Dict[str, Any]) -> RR:
    code = type_code(obj["type"])
    return RR(normalize(obj["name"]), code, CLASSES.get(obj.get("class", "IN"), IN), int(obj["ttl"]), parse_rdata(code, obj["data"]))


def parse_records(text: str) -> List[RR]:
    records = []
    for number, raw in enumerate(text.splitlines(), 1):
        line = raw.split(";", 1)[0].strip()
        if not line or line.startswith("$"):
            continue
        words = line.split()
        try:
            name, rest = words[0], words[1:]
            ttl = int(rest.pop(0)) if rest and rest[0].isdigit() else 0
            rclass = CLASSES[rest.pop(0).upper()] if rest and rest[0].upper() in CLASSES else IN
            code = type_code(rest.pop(0))
            records.append(RR(normalize(name), code, rclass, ttl, parse_rdata(code, " ".join(rest))))
        except (IndexError, InputError) as exc:
            raise InputError(f"line {number}: {raw.strip()!r}: {exc}")
    return records


def hints_from_records(records: Sequence[RR]) -> List[Tuple[str, str]]:
    names = [normalize(rr.data).lower() for rr in records if rr.rtype == NS and rr.name == "."]
    hints = [(rr.name.lower(), rr.data) for name in names for rr in records if rr.rtype == A and rr.name.lower() == name]
    if not hints:
        raise InputError("the hints file has no '. NS name' record with an A record for that name")
    return hints


def write_rdata(w: Writer, rr: RR) -> None:
    data = rr.data
    squeeze = rr.rtype in COMPRESSIBLE_RDATA
    if isinstance(data, bytes):
        w.buf += data
    elif rr.rtype == A:
        w.buf += ipaddress.IPv4Address(data).packed
    elif rr.rtype == AAAA:
        w.buf += ipaddress.IPv6Address(data).packed
    elif rr.rtype in (NS, CNAME, PTR):
        w.name(data, squeeze)
    elif rr.rtype == MX:
        w.buf += struct.pack("!H", data[0])
        w.name(data[1], squeeze)
    elif rr.rtype == SRV:
        w.buf += struct.pack("!3H", data[0], data[1], data[2])
        w.name(data[3], squeeze)
    elif rr.rtype == SOA:
        w.name(data.mname, squeeze)
        w.name(data.rname, squeeze)
        w.buf += struct.pack("!5I", data.serial, data.refresh, data.retry, data.expire, data.minimum)
    elif rr.rtype == TXT:
        for part in data:
            w.buf.append(len(part))
            w.buf += part
    elif rr.rtype == OPT:
        for code, value in data:
            w.buf += struct.pack("!HH", code, len(value)) + value
    else:
        raise InputError(f"cannot encode {type_name(rr.rtype)} data {data!r}")


def write_rr(w: Writer, rr: RR) -> None:
    w.name(rr.name)
    w.buf += struct.pack("!HHI", rr.rtype, rr.rclass, rr.ttl & 0xFFFFFFFF)
    length_at = len(w.buf)
    w.buf += b"\0\0"
    write_rdata(w, rr)
    struct.pack_into("!H", w.buf, length_at, len(w.buf) - length_at - 2)


def pack_flags(m: Message) -> int:
    return (m.qr << 15) | ((m.opcode & 0xF) << 11) | (m.aa << 10) | (m.tc << 9) | (m.rd << 8) | (m.ra << 7) | (m.ad << 5) | (m.cd << 4) | (m.rcode & 0xF)


def opt_record(edns: Edns, rcode: int) -> RR:
    ttl = ((rcode >> 4) << 24) | ((edns.version & 0xFF) << 16) | (0x8000 if edns.do else 0)
    return RR(".", OPT, edns.udp_size, ttl, list(edns.options))


def encode(m: Message, compress: bool = True) -> bytes:
    if m.rcode > 0xF and m.edns is None:
        raise InputError(f"rcode {m.rcode} needs EDNS: only 4 bits fit in the header")
    w = Writer(compress)
    additional = list(m.additional) + ([opt_record(m.edns, m.rcode)] if m.edns else [])
    w.buf += struct.pack("!6H", m.id, pack_flags(m), len(m.question), len(m.answer), len(m.authority), len(additional))
    for q in m.question:
        w.name(q.name)
        w.buf += struct.pack("!HH", q.qtype, q.qclass)
    for rr in list(m.answer) + list(m.authority) + additional:
        write_rr(w, rr)
    return bytes(w.buf)


def expect_end(after: int, end: int, what: str) -> None:
    if after != end:
        raise WireError(f"{what}: RDLENGTH says the data ends at {end}, it ends at {after}")


def read_rdata(data: bytes, pos: int, length: int, rtype: int, owner: str) -> Any:
    end = pos + length
    chunk = data[pos:end]
    what = f"{owner} {type_name(rtype)}"
    if rtype == A:
        if length != 4:
            raise WireError(f"{what}: an A record holds 4 octets, got {length}")
        return str(ipaddress.IPv4Address(chunk))
    if rtype == AAAA:
        if length != 16:
            raise WireError(f"{what}: an AAAA record holds 16 octets, got {length}")
        return str(ipaddress.IPv6Address(chunk))
    if rtype in (NS, CNAME, PTR):
        target, after = read_name(data, pos)
        expect_end(after, end, what)
        return target
    if rtype == MX:
        if length < 3:
            raise WireError(f"{what}: too short")
        exchange, after = read_name(data, pos + 2)
        expect_end(after, end, what)
        return (struct.unpack_from("!H", data, pos)[0], exchange)
    if rtype == SRV:
        if length < 7:
            raise WireError(f"{what}: too short")
        priority, weight, port = struct.unpack_from("!3H", data, pos)
        target, after = read_name(data, pos + 6)
        expect_end(after, end, what)
        return (priority, weight, port, target)
    if rtype == SOA:
        mname, after = read_name(data, pos)
        rname, after = read_name(data, after)
        expect_end(after + 20, end, what)
        return Soa(mname, rname, *struct.unpack_from("!5I", data, after))
    if rtype == TXT:
        parts, i = [], pos
        while i < end:
            size = data[i]
            if i + 1 + size > end:
                raise WireError(f"{what}: character-string runs past the record")
            parts.append(bytes(data[i + 1:i + 1 + size]))
            i += 1 + size
        return tuple(parts)
    if rtype == OPT:
        options, i = [], pos
        while i < end:
            if i + 4 > end:
                raise WireError(f"{what}: option header cut off")
            code, size = struct.unpack_from("!HH", data, i)
            if i + 4 + size > end:
                raise WireError(f"{what}: option {code} runs past the record")
            options.append((code, bytes(data[i + 4:i + 4 + size])))
            i += 4 + size
        return options
    return bytes(chunk)


def read_rr(data: bytes, pos: int) -> Tuple[RR, int]:
    name, pos = read_name(data, pos)
    if pos + 10 > len(data):
        raise WireError(f"record header for {name} runs past the end of the message")
    rtype, rclass, ttl, length = struct.unpack_from("!HHIH", data, pos)
    pos += 10
    if pos + length > len(data):
        raise WireError(f"{name} {type_name(rtype)}: RDLENGTH {length} runs past the end of the message")
    if ttl & 0x80000000 and rtype != OPT:
        ttl = 0
    return RR(name, rtype, rclass, ttl, read_rdata(data, pos, length, rtype, name)), pos + length


def decode(data: bytes) -> Message:
    if len(data) < 12:
        raise WireError(f"message is {len(data)} octets; the header alone is 12")
    ident, flags, qdcount, ancount, nscount, arcount = struct.unpack_from("!6H", data)
    m = Message(
        id=ident,
        qr=bool(flags & 0x8000),
        opcode=(flags >> 11) & 0xF,
        aa=bool(flags & 0x0400),
        tc=bool(flags & 0x0200),
        rd=bool(flags & 0x0100),
        ra=bool(flags & 0x0080),
        ad=bool(flags & 0x0020),
        cd=bool(flags & 0x0010),
        rcode=flags & 0xF,
    )
    pos = 12
    for _ in range(qdcount):
        name, pos = read_name(data, pos)
        if pos + 4 > len(data):
            raise WireError(f"question for {name} is cut off")
        qtype, qclass = struct.unpack_from("!HH", data, pos)
        m.question.append(Question(name, qtype, qclass))
        pos += 4
    sections: List[List[RR]] = [m.answer, m.authority, m.additional]
    for section, count in zip(sections, (ancount, nscount, arcount)):
        for _ in range(count):
            rr, pos = read_rr(data, pos)
            section.append(rr)
    opts = [rr for rr in m.additional if rr.rtype == OPT]
    if len(opts) > 1:
        raise WireError("more than one OPT record; RFC 6891 makes that a FORMERR")
    if opts:
        opt = opts[0]
        if opt.name != ".":
            raise WireError(f"the OPT record must be owned by the root, not {opt.name}")
        m.edns = Edns(opt.rclass, (opt.ttl >> 16) & 0xFF, bool(opt.ttl & 0x8000), opt.ttl >> 24, list(opt.data))
        m.rcode |= m.edns.ext_rcode << 4
        m.additional = [rr for rr in m.additional if rr.rtype != OPT]
    return m


def make_query(name: str, qtype: int, rd: bool = True, bufsize: int = DEFAULT_BUFSIZE, do: bool = False, ident: Optional[int] = None) -> Message:
    edns = Edns(udp_size=bufsize, do=do) if bufsize else None
    question = Question(labels_to_name(name_to_labels(name)), qtype, IN)
    return Message(id=secrets.randbelow(65536) if ident is None else ident, rd=rd, question=[question], edns=edns)


def flags_text(m: Message) -> str:
    return " ".join(flag for flag, on in (("qr", m.qr), ("aa", m.aa), ("tc", m.tc), ("rd", m.rd), ("ra", m.ra), ("ad", m.ad), ("cd", m.cd)) if on)


def response_matches(query: Message, response: Message) -> bool:
    if not response.qr or response.id != query.id:
        return False
    if not response.question:
        return response.rcode != NOERROR
    asked, got = query.question[0], response.question[0]
    return same_name(asked.name, got.name) and asked.qtype == got.qtype and asked.qclass == got.qclass


@dataclass
class Exchange:
    response: Message
    server: str
    port: int
    transport: str
    truncated_udp: bool
    attempts: int
    size: int
    elapsed_ms: float


def family_of(address: str) -> int:
    return socket.AF_INET6 if ":" in address else socket.AF_INET


def udp_exchange(server: str, port: int, query: Message, wire: bytes, timeout: float, tries: int) -> Tuple[Message, int, int]:
    with socket.socket(family_of(server), socket.SOCK_DGRAM) as sock:
        try:
            sock.connect((server, port))
        except OSError as exc:
            raise QueryError(f"{server}#{port}: cannot reach it over UDP: {exc}")
        try:
            for attempt in range(1, tries + 1):
                sock.send(wire)
                deadline = time.monotonic() + timeout
                while True:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        break
                    sock.settimeout(remaining)
                    try:
                        data = sock.recv(65535)
                    except socket.timeout:
                        break
                    try:
                        response = decode(data)
                    except WireError:
                        continue
                    if response_matches(query, response):
                        return response, attempt, len(data)
        except ConnectionRefusedError:
            raise QueryError(f"{server}#{port}: connection refused (ICMP port unreachable): nothing listens on UDP port {port}")
    raise QueryError(f"{server}#{port}: no response over UDP after {tries} tries of {timeout:g}s")


def recv_exact(sock: socket.socket, size: int) -> bytes:
    chunks, got = [], 0
    while got < size:
        chunk = sock.recv(size - got)
        if not chunk:
            raise QueryError(f"TCP connection closed after {got} of {size} octets")
        chunks.append(chunk)
        got += len(chunk)
    return b"".join(chunks)


def tcp_exchange(server: str, port: int, query: Message, wire: bytes, timeout: float) -> Tuple[Message, int]:
    try:
        with socket.create_connection((server, port), timeout=timeout) as sock:
            sock.sendall(struct.pack("!H", len(wire)) + wire)
            length = struct.unpack("!H", recv_exact(sock, 2))[0]
            data = recv_exact(sock, length)
    except ConnectionRefusedError:
        raise QueryError(f"{server}#{port}: TCP connection refused: the server does not listen on TCP port {port} (or a firewall rejects it)")
    except socket.timeout:
        raise QueryError(f"{server}#{port}: no response over TCP within {timeout:g}s")
    except OSError as exc:
        raise QueryError(f"{server}#{port}: TCP failed: {exc}")
    response = decode(data)
    if not response_matches(query, response):
        raise QueryError(f"{server}#{port}: the TCP response does not match the query (id or question differs)")
    return response, len(data)


class Transport:
    def __init__(self, timeout: float = 2.0, tries: int = 3, tcp: bool = False, ignore_tc: bool = False):
        self.timeout = timeout
        self.tries = max(1, tries)
        self.tcp = tcp
        self.ignore_tc = ignore_tc

    def exchange(self, server: str, port: int, query: Message) -> Exchange:
        wire = encode(query)
        started = time.monotonic()
        if self.tcp:
            response, size = tcp_exchange(server, port, query, wire, self.timeout)
            return Exchange(response, server, port, "tcp", False, 1, size, elapsed_ms(started))
        response, attempts, size = udp_exchange(server, port, query, wire, self.timeout, self.tries)
        if response.tc and not self.ignore_tc:
            response, size = tcp_exchange(server, port, query, wire, self.timeout)
            return Exchange(response, server, port, "tcp", True, attempts, size, elapsed_ms(started))
        return Exchange(response, server, port, "udp", False, attempts, size, elapsed_ms(started))


def elapsed_ms(started: float) -> float:
    return round((time.monotonic() - started) * 1000, 1)


@dataclass
class Outcome:
    kind: str
    rcode: int = NOERROR
    records: List[RR] = field(default_factory=list)
    target: str = ""
    zone: str = ""
    ns: List[RR] = field(default_factory=list)
    glue: List[RR] = field(default_factory=list)
    soa: Optional[RR] = None
    ttl: Optional[int] = None
    note: str = ""


def follow_answer(m: Message, qname: str, qtype: int) -> Tuple[List[RR], str, bool]:
    records: List[RR] = []
    name = normalize(qname)
    seen = {name.lower()}
    while True:
        matches = [rr for rr in m.answer if same_name(rr.name, name) and (rr.rtype == qtype or qtype == ANY)]
        if matches:
            return records + matches, name, True
        aliases = [rr for rr in m.answer if same_name(rr.name, name) and rr.rtype == CNAME]
        if not aliases:
            return records, name, False
        records.append(aliases[0])
        name = normalize(aliases[0].data)
        if name.lower() in seen:
            return records, name, False
        seen.add(name.lower())


def classify(m: Message, qname: str, qtype: int, zone: str) -> Outcome:
    records, final, found = follow_answer(m, qname, qtype)
    soa = next((rr for rr in m.authority if rr.rtype == SOA and isinstance(rr.data, Soa)), None)
    if m.rcode == NXDOMAIN:
        return Outcome("nxdomain", NXDOMAIN, records, final, soa=soa)
    if m.rcode != NOERROR:
        return Outcome("error", m.rcode, note=f"{rcode_name(m.rcode)} from the server")
    if found:
        return Outcome("answer", NOERROR, records, final)
    if records:
        return Outcome("cname", NOERROR, records, final)
    delegations = [rr for rr in m.authority if rr.rtype == NS]
    if delegations and soa is None:
        child = normalize(delegations[0].name)
        if same_name(child, zone) or not is_subdomain(child, zone) or not is_subdomain(final, child):
            return Outcome("lame", NOERROR, zone=child, ns=delegations, note=f"lame delegation: a server for {zone} referred {final} to {child}, which is not below {zone}")
        ns = [rr for rr in delegations if same_name(rr.name, child)]
        glue = [
            rr
            for target in (normalize(n.data).lower() for n in ns)
            for rr in m.additional
            if rr.rtype in (A, AAAA) and rr.name.lower() == target and is_subdomain(rr.name, zone)
        ]
        return Outcome("referral", NOERROR, target=final, zone=child, ns=ns, glue=glue)
    return Outcome("nodata", NOERROR, records, final, soa=soa)


def negative_ttl(soa: RR) -> int:
    return min(soa.ttl, soa.data.minimum)


@dataclass
class CacheEntry:
    records: List[RR]
    expires: float
    negative: str = ""
    soa: Optional[RR] = None


class Cache:
    def __init__(self, clock: Callable[[], float] = time.time):
        self.clock = clock
        self.entries: Dict[Tuple[str, int], CacheEntry] = {}

    def put(self, records: Sequence[RR]) -> None:
        now = self.clock()
        groups: Dict[Tuple[str, int], List[RR]] = {}
        for rr in records:
            if rr.rtype != OPT:
                groups.setdefault((rr.name.lower(), rr.rtype), []).append(rr)
        for key, group in groups.items():
            ttl = min(rr.ttl for rr in group)
            if ttl > 0:
                self.entries[key] = CacheEntry([replace(rr, ttl=ttl) for rr in group], now + ttl)

    def put_negative(self, name: str, qtype: int, kind: str, soa: Optional[RR]) -> Optional[int]:
        if soa is None:
            return None
        ttl = negative_ttl(soa)
        if ttl > 0:
            key = (normalize(name).lower(), 0 if kind == "nxdomain" else qtype)
            self.entries[key] = CacheEntry([], self.clock() + ttl, kind, replace(soa, ttl=ttl))
        return ttl

    def get(self, name: str, qtype: int) -> Optional[CacheEntry]:
        now = self.clock()
        owner = normalize(name).lower()
        for key in ((owner, 0), (owner, qtype)):
            entry = self.entries.get(key)
            if entry is None:
                continue
            if entry.expires <= now:
                del self.entries[key]
                continue
            left = int(entry.expires - now)
            soa = replace(entry.soa, ttl=left) if entry.soa else None
            return CacheEntry([replace(rr, ttl=left) for rr in entry.records], entry.expires, entry.negative, soa)
        return None

    def delegation(self, name: str) -> Optional[Tuple[str, List[Tuple[str, str]]]]:
        for zone in ancestors(name)[:-1]:
            ns = self.get(zone, NS)
            if ns is None or ns.negative:
                continue
            servers = []
            for rr in ns.records:
                addresses = self.get(rr.data, A)
                if addresses is not None and not addresses.negative:
                    servers += [(normalize(rr.data), a.data) for a in addresses.records if a.rtype == A]
            if servers:
                return normalize(zone), servers
        return None

    def dump(self) -> Dict[str, Any]:
        rows = []
        for (owner, qtype), entry in sorted(self.entries.items()):
            rows.append({"name": owner, "type": qtype, "expires": entry.expires, "negative": entry.negative, "records": [rr_json(rr) for rr in entry.records], "soa": rr_json(entry.soa) if entry.soa else None})
        return {"entries": rows}

    def load(self, obj: Dict[str, Any]) -> None:
        now = self.clock()
        for row in obj.get("entries", []):
            if row["expires"] > now:
                soa = rr_from_json(row["soa"]) if row.get("soa") else None
                self.entries[(row["name"], int(row["type"]))] = CacheEntry([rr_from_json(r) for r in row["records"]], float(row["expires"]), row.get("negative", ""), soa)


@dataclass
class Step:
    zone: str
    server: str
    server_name: str
    port: int
    qname: str
    qtype: int
    kind: str
    rcode: str = ""
    aa: bool = False
    transport: str = ""
    truncated_udp: bool = False
    size: int = 0
    elapsed_ms: float = 0.0
    records: List[RR] = field(default_factory=list)
    referral: str = ""
    depth: int = 0
    note: str = ""


@dataclass
class Resolution:
    name: str
    qtype: int
    status: str
    answer: List[RR] = field(default_factory=list)
    soa: Optional[RR] = None
    negative_ttl: Optional[int] = None
    steps: List[Step] = field(default_factory=list)
    error: str = ""

    @property
    def queries(self) -> int:
        return sum(1 for s in self.steps if s.kind != "cache")

    @property
    def from_cache(self) -> bool:
        return bool(self.steps) and self.queries == 0


class Resolver:
    def __init__(self, transport: Any, hints: Sequence[Tuple[str, str]], port: int = 53, cache: Optional[Cache] = None, bufsize: int = DEFAULT_BUFSIZE, max_referrals: int = MAX_REFERRALS, max_cnames: int = MAX_CNAMES):
        if not hints:
            raise InputError("no root hints: pass --root IP or --hints FILE")
        self.transport = transport
        self.hints = [(normalize(n), a) for n, a in hints]
        self.port = port
        self.cache = cache if cache is not None else Cache()
        self.bufsize = bufsize
        self.max_referrals = max_referrals
        self.max_cnames = max_cnames

    def resolve(self, name: str, qtype: int, depth: int = 0, steps: Optional[List[Step]] = None) -> Resolution:
        steps = [] if steps is None else steps
        qname = labels_to_name(name_to_labels(name))
        answer: List[RR] = []
        current = qname
        seen = [qname.lower()]
        while True:
            outcome = self.walk(current, qtype, steps, depth)
            answer += outcome.records
            if outcome.kind == "answer":
                return Resolution(qname, qtype, "NOERROR", answer, steps=steps)
            if outcome.kind == "cname":
                target = outcome.target.lower()
                if target in seen:
                    return Resolution(qname, qtype, "SERVFAIL", answer, steps=steps, error=f"CNAME loop: {' -> '.join(seen + [target])}")
                if sum(1 for rr in answer if rr.rtype == CNAME) > self.max_cnames:
                    return Resolution(qname, qtype, "SERVFAIL", answer, steps=steps, error=f"more than {self.max_cnames} CNAMEs in a row")
                seen.append(target)
                current = outcome.target
                continue
            if outcome.kind in ("nxdomain", "nodata"):
                return Resolution(qname, qtype, outcome.kind.upper(), answer, outcome.soa, outcome.ttl, steps)
            return Resolution(qname, qtype, "SERVFAIL", answer, steps=steps, error=outcome.note)

    def walk(self, qname: str, qtype: int, steps: List[Step], depth: int) -> Outcome:
        cached = self.cache.get(qname, qtype)
        if cached is not None:
            shown = cached.records or ([cached.soa] if cached.soa else [])
            steps.append(Step("", "cache", "", 0, qname, qtype, "cache", records=shown, depth=depth, note=cached.negative or "answer"))
            if cached.negative:
                return Outcome(cached.negative, NXDOMAIN if cached.negative == "nxdomain" else NOERROR, target=qname, soa=cached.soa, ttl=cached.soa.ttl if cached.soa else None)
            return Outcome("answer", records=cached.records, target=qname)
        if qtype != CNAME:
            alias = self.cache.get(qname, CNAME)
            if alias is not None and not alias.negative:
                steps.append(Step("", "cache", "", 0, qname, qtype, "cache", records=alias.records, depth=depth, note="cname"))
                return Outcome("cname", records=alias.records, target=normalize(alias.records[0].data))
        zone, servers = self.cache.delegation(qname) or (".", self.hints)
        for _ in range(self.max_referrals):
            outcome = self.ask(zone, servers, qname, qtype, steps, depth)
            if outcome.kind == "referral":
                self.cache.put(outcome.ns + outcome.glue)
                servers = [(rr.name, rr.data) for rr in outcome.glue if rr.rtype == A] or self.lookup_ns(outcome, steps, depth)
                if not servers:
                    return Outcome("error", SERVFAIL, note=f"no address for any name server of {outcome.zone} ({', '.join(rr.data for rr in outcome.ns)}): glue missing and the names do not resolve")
                zone = outcome.zone
                continue
            if outcome.kind in ("answer", "cname"):
                self.cache.put(outcome.records)
                return outcome
            if outcome.kind in ("nxdomain", "nodata"):
                self.cache.put([rr for rr in outcome.records if rr.rtype == CNAME])
                outcome.ttl = self.cache.put_negative(outcome.target or qname, qtype, outcome.kind, outcome.soa)
            return outcome
        return Outcome("error", SERVFAIL, note=f"more than {self.max_referrals} referrals for {qname}")

    def lookup_ns(self, referral: Outcome, steps: List[Step], depth: int) -> List[Tuple[str, str]]:
        if depth >= MAX_GLUELESS_DEPTH:
            return []
        for rr in referral.ns:
            target = normalize(rr.data)
            found = self.resolve(target, A, depth + 1, steps)
            servers = [(target, a.data) for a in found.answer if a.rtype == A]
            if servers:
                return servers
        return []

    def ask(self, zone: str, servers: Sequence[Tuple[str, str]], qname: str, qtype: int, steps: List[Step], depth: int) -> Outcome:
        problems = []
        for server_name, address in servers:
            query = make_query(qname, qtype, rd=False, bufsize=self.bufsize)
            try:
                exchange = self.transport.exchange(address, self.port, query)
            except QueryError as exc:
                steps.append(Step(zone, address, server_name, self.port, qname, qtype, "no-response", depth=depth, note=str(exc)))
                problems.append(str(exc))
                continue
            outcome = classify(exchange.response, qname, qtype, zone)
            response = exchange.response
            shown = outcome.records if outcome.kind in ("answer", "cname") else outcome.ns + outcome.glue if outcome.kind == "referral" else [outcome.soa] if outcome.soa else []
            steps.append(Step(zone, address, server_name, exchange.port, qname, qtype, outcome.kind, rcode_name(response.rcode), response.aa, exchange.transport, exchange.truncated_udp, exchange.size, exchange.elapsed_ms, shown, outcome.zone if outcome.kind == "referral" else "", depth, outcome.note))
            if outcome.kind in ("error", "lame"):
                problems.append(f"{address}: {outcome.note}")
                continue
            return outcome
        return Outcome("error", SERVFAIL, note=f"every server for {zone} failed: " + "; ".join(problems))


@dataclass
class ResolvConf:
    nameservers: List[str] = field(default_factory=list)
    search: List[str] = field(default_factory=list)
    ndots: int = 1
    timeout: int = 5
    attempts: int = 2
    options: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)


def apply_options(conf: ResolvConf, words: Sequence[str]) -> None:
    limits = {"ndots": MAX_NDOTS, "timeout": 30, "attempts": 5}
    for word in words:
        key, _, value = word.partition(":")
        if key in limits and value:
            if not value.isdigit():
                conf.warnings.append(f"options {word}: not a number, ignored")
                continue
            setattr(conf, key, min(int(value), limits[key]))
        elif word not in conf.options:
            conf.options.append(word)


def parse_resolv_conf(text: str, hostname: str = "", env: Optional[Dict[str, str]] = None) -> ResolvConf:
    conf = ResolvConf()
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line[0] in "#;":
            continue
        words = line.split()
        key = words[0]
        if key == "nameserver" and len(words) > 1:
            address = words[1].split("%", 1)[0]
            try:
                ipaddress.ip_address(address)
            except ValueError:
                conf.warnings.append(f"nameserver {words[1]}: not an IP address, ignored")
                continue
            if len(conf.nameservers) >= MAX_NAMESERVERS:
                conf.warnings.append(f"nameserver {address}: ignored, glibc reads only the first {MAX_NAMESERVERS}")
                continue
            conf.nameservers.append(address)
        elif key == "domain" and len(words) > 1:
            conf.search = [words[1].rstrip(".")]
        elif key == "search":
            conf.search = [w.rstrip(".") or "." for w in words[1:]]
        elif key == "options":
            apply_options(conf, words[1:])
        elif key not in ("sortlist",):
            conf.warnings.append(f"unknown keyword {key!r}, ignored")
    env = env or {}
    if env.get("LOCALDOMAIN"):
        conf.search = [w.rstrip(".") for w in env["LOCALDOMAIN"].split()]
    if env.get("RES_OPTIONS"):
        apply_options(conf, env["RES_OPTIONS"].split())
    if not conf.search and "." in hostname.strip("."):
        conf.search = [hostname.strip(".").split(".", 1)[1]]
    if not conf.nameservers:
        conf.nameservers = ["127.0.0.1"]
        conf.warnings.append("no nameserver line: glibc falls back to 127.0.0.1")
    return conf


@dataclass
class Candidate:
    name: str
    reason: str
    phase: str


def plan_search(name: str, conf: ResolvConf) -> List[Candidate]:
    text = name.strip()
    if not text or text == ".":
        raise InputError("give a name to look up")
    name_to_labels(text)
    if text.endswith("."):
        return [Candidate(text, "trailing dot: an absolute name, the search list is skipped", "absolute")]
    dots = text.count(".")
    plan: List[Candidate] = []
    if dots >= conf.ndots:
        plan.append(Candidate(text + ".", f"as-is first: {dots} dot(s) >= ndots:{conf.ndots}", "as-is-first"))
    for domain in conf.search:
        fqdn = text + "." if domain == "." else f"{text}.{domain}."
        plan.append(Candidate(fqdn, f"search {domain}", "search"))
    if dots < conf.ndots and not (dots == 0 and "no-tld-query" in conf.options):
        plan.append(Candidate(text + ".", f"as-is last: {dots} dot(s) < ndots:{conf.ndots}", "as-is-last"))
    unique: List[Candidate] = []
    for candidate in plan:
        if all(c.name.lower() != candidate.name.lower() for c in unique):
            unique.append(candidate)
    return unique


def run_search(plan: Sequence[Candidate], ask: Callable[[str], Tuple[str, int]]) -> Tuple[List[Dict[str, Any]], bool]:
    sent: List[Dict[str, Any]] = []
    stop_search = False
    for candidate in plan:
        if candidate.phase == "search" and stop_search:
            continue
        status, answers = ask(candidate.name)
        sent.append({"name": candidate.name, "reason": candidate.reason, "status": status, "answers": answers})
        if status == "NOERROR" and answers > 0:
            return sent, True
        if candidate.phase == "absolute":
            break
        if candidate.phase == "search" and status not in ("NXDOMAIN", "NOERROR", "SERVFAIL"):
            stop_search = True
    return sent, False


def resolution_json(res: Resolution) -> Dict[str, Any]:
    return {
        "name": res.name,
        "type": type_name(res.qtype),
        "status": res.status,
        "answer": [rr_json(rr) for rr in res.answer],
        "negative_ttl": res.negative_ttl,
        "soa": rr_json(res.soa) if res.soa else None,
        "from_cache": res.from_cache,
        "queries": res.queries,
        "error": res.error,
        "steps": [
            {
                "zone": s.zone,
                "server": s.server,
                "server_name": s.server_name,
                "port": s.port,
                "qname": s.qname,
                "qtype": type_name(s.qtype),
                "kind": s.kind,
                "rcode": s.rcode,
                "aa": s.aa,
                "transport": s.transport,
                "truncated_udp": s.truncated_udp,
                "size": s.size,
                "elapsed_ms": s.elapsed_ms,
                "referral": s.referral,
                "records": [rr_json(rr) for rr in s.records],
                "depth": s.depth,
                "note": s.note,
            }
            for s in res.steps
        ],
    }


def message_json(ex: Exchange) -> Dict[str, Any]:
    m = ex.response
    edns = None
    if m.edns:
        edns = {"version": m.edns.version, "do": m.edns.do, "udp": m.edns.udp_size, "options": [{"code": c, "name": EDNS_OPTIONS.get(c, str(c)), "data": v.hex()} for c, v in m.edns.options]}
    return {
        "server": ex.server,
        "port": ex.port,
        "transport": ex.transport,
        "truncated_udp": ex.truncated_udp,
        "attempts": ex.attempts,
        "size": ex.size,
        "elapsed_ms": ex.elapsed_ms,
        "id": m.id,
        "opcode": OPCODES.get(m.opcode, str(m.opcode)),
        "status": rcode_name(m.rcode),
        "flags": flags_text(m).split(),
        "question": [{"name": q.name, "type": type_name(q.qtype), "class": class_name(q.qclass)} for q in m.question],
        "answer": [rr_json(rr) for rr in m.answer],
        "authority": [rr_json(rr) for rr in m.authority],
        "additional": [rr_json(rr) for rr in m.additional],
        "edns": edns,
    }


def render_message(ex: Exchange, command: str) -> str:
    m = ex.response
    additional = len(m.additional) + (1 if m.edns else 0)
    lines = [
        f"; <<>> minidig <<>> {command}",
        f";; ->>HEADER<<- opcode: {OPCODES.get(m.opcode, m.opcode)}, status: {rcode_name(m.rcode)}, id: {m.id}",
        f";; flags: {flags_text(m)}; QUERY: {len(m.question)}, ANSWER: {len(m.answer)}, AUTHORITY: {len(m.authority)}, ADDITIONAL: {additional}",
    ]
    if m.edns:
        flags = "do" if m.edns.do else ""
        lines += ["", ";; OPT PSEUDOSECTION:", f"; EDNS: version: {m.edns.version}, flags:{(' ' + flags) if flags else ''}; udp: {m.edns.udp_size}"]
        lines += [f"; {EDNS_OPTIONS.get(c, 'OPT=' + str(c))}: {v.hex()}" for c, v in m.edns.options]
    lines += ["", ";; QUESTION SECTION:"] + [f";{q.name}\t\t{class_name(q.qclass)}\t{type_name(q.qtype)}" for q in m.question]
    for title, section in (("ANSWER", m.answer), ("AUTHORITY", m.authority), ("ADDITIONAL", m.additional)):
        if section:
            lines += ["", f";; {title} SECTION:"] + [rr_text(rr) for rr in section]
    via = f"{ex.transport.upper()}" + (", after a truncated UDP reply" if ex.truncated_udp else "")
    lines += ["", f";; Query time: {round(ex.elapsed_ms)} msec", f";; SERVER: {ex.server}#{ex.port}({ex.server}) ({via})", f";; MSG SIZE  rcvd: {ex.size}"]
    return "\n".join(lines)


def describe_step(step: Step) -> str:
    if step.kind == "referral":
        return f"referral to {step.referral}"
    if step.kind == "answer":
        return "answer" + (" (authoritative)" if step.aa else "")
    if step.kind == "cname":
        return "CNAME, follow the target"
    if step.kind in ("nxdomain", "nodata"):
        return step.kind.upper()
    return f"{step.rcode or step.kind}: {step.note}"


def render_trace(res: Resolution) -> str:
    lines = [f"; <<>> minidig +trace <<>> {res.name} {type_name(res.qtype)}", ""]
    for step in res.steps:
        prefix = ";; [name server lookup] " if step.depth else ";; "
        if step.kind == "cache":
            lines += [rr_text(rr) for rr in step.records]
            lines += [f"{prefix}from cache: {step.qname} {type_name(step.qtype)} ({step.note}), no query sent", ""]
            continue
        if step.kind == "no-response":
            lines += [f"{prefix}no response from {step.server}#{step.port}({step.server_name}) for zone {step.zone}: {step.note}", ""]
            continue
        lines += [rr_text(rr) for rr in step.records]
        via = " over TCP after a truncated UDP reply" if step.truncated_udp else f" over {step.transport.upper()}"
        lines += [f"{prefix}Received {step.size} bytes from {step.server}#{step.port}({step.server_name}) in {round(step.elapsed_ms)} ms{via}: {describe_step(step)}", ""]
    lines.append(summary_line(res))
    return "\n".join(lines)


def summary_line(res: Resolution) -> str:
    where = "from cache" if res.from_cache else f"after {res.queries} quer{'y' if res.queries == 1 else 'ies'}"
    if res.status == "NOERROR":
        return f";; NOERROR {res.name} {type_name(res.qtype)}: {len(res.answer)} record(s) {where}"
    if res.status in ("NXDOMAIN", "NODATA"):
        if res.soa is None:
            return f";; {res.status} {res.name} {type_name(res.qtype)} {where}: no SOA in the response, so it is not cached"
        return f";; {res.status} {res.name} {type_name(res.qtype)} {where}: negative-cached for {res.negative_ttl} s (the smaller of the SOA TTL and its MINIMUM {res.soa.data.minimum})"
    return f";; SERVFAIL {res.name} {type_name(res.qtype)} {where}: {res.error}"


def render_resolution(res: Resolution) -> str:
    lines = [rr_text(rr) for rr in res.answer]
    if res.soa is not None and res.status != "NOERROR":
        lines.append(rr_text(res.soa))
    lines.append(summary_line(res))
    return "\n".join(lines)


def parse_dig_words(words: Sequence[str]) -> Tuple[Optional[str], str, int, List[str]]:
    server, name, qtype, plus = None, "", A, []
    positional = []
    for word in words:
        if word.startswith("@"):
            server = word[1:]
        elif word.startswith("+"):
            plus.append(word[1:].lower())
        else:
            positional.append(word)
    if not positional:
        raise InputError("give a name to query, for example: query @127.0.10.3 www.cc.test A")
    name = positional[0]
    if len(positional) > 1:
        qtype = type_code(positional[1])
    if len(positional) > 2:
        raise InputError(f"unexpected {positional[2]!r}: the form is [@SERVER] NAME [TYPE] [+option ...]")
    return server, name, qtype, plus


def read_text(path: str) -> str:
    try:
        with open(path, encoding="utf-8") as handle:
            return handle.read()
    except OSError as exc:
        raise InputError(f"cannot read {path}: {exc.strerror}")


def default_server(path: str) -> str:
    conf = parse_resolv_conf(read_text(path))
    return conf.nameservers[0]


def cmd_query(args: argparse.Namespace, transport: Any, clock: Any) -> int:
    server, name, qtype, plus = parse_dig_words(args.words)
    server = args.server or server or default_server(args.resolv_conf)
    bufsize, recurse, tcp, ignore, short, do = args.bufsize, not args.norecurse, args.tcp, args.ignore, args.short, args.dnssec
    for option in plus:
        if option in ("norecurse", "norec"):
            recurse = False
        elif option in ("recurse", "rec"):
            recurse = True
        elif option in ("tcp", "vc"):
            tcp = True
        elif option == "noedns":
            bufsize = 0
        elif option.startswith("bufsize="):
            value = option.split("=", 1)[1]
            if not value.isdigit() or int(value) > 65535:
                raise InputError(f"+bufsize={value}: expected 0 to 65535")
            bufsize = int(value)
        elif option == "ignore":
            ignore = True
        elif option == "short":
            short = True
        elif option == "dnssec":
            do = True
        else:
            raise InputError(f"unknown option +{option} (known: +norecurse, +tcp, +noedns, +bufsize=N, +ignore, +short, +dnssec)")
    try:
        ipaddress.ip_address(server)
    except ValueError:
        raise InputError(f"server {server!r} must be an IP address")
    query = make_query(name, qtype, rd=recurse, bufsize=bufsize, do=do)
    link = transport or Transport(args.timeout, args.tries, tcp=tcp, ignore_tc=ignore)
    exchange = link.exchange(server, args.port, query)
    if args.json:
        print(json.dumps(message_json(exchange), indent=2))
    elif short:
        print("\n".join(rdata_text(rr) for rr in exchange.response.answer))
    else:
        print(render_message(exchange, " ".join(args.words)))
    return 0


def load_hints(args: argparse.Namespace) -> List[Tuple[str, str]]:
    if args.hints:
        return hints_from_records(parse_records(read_text(args.hints)))
    if args.root:
        hints = []
        for item in args.root:
            label, _, address = item.rpartition("=")
            try:
                ipaddress.ip_address(address)
            except ValueError:
                raise InputError(f"--root {item!r}: expected IP or NAME=IP")
            hints.append((label or address, address))
        return hints
    return list(ROOT_HINTS)


def cmd_resolve(args: argparse.Namespace, transport: Any, clock: Any) -> int:
    qtype = type_code(args.type)
    cache = Cache(clock or time.time)
    if args.cache_file and os.path.exists(args.cache_file):
        try:
            cache.load(json.loads(read_text(args.cache_file)))
        except (ValueError, KeyError) as exc:
            raise InputError(f"{args.cache_file} is not a minidig cache file: {exc}")
    link = transport or Transport(args.timeout, args.tries, tcp=args.tcp)
    resolver = Resolver(link, load_hints(args), args.port, cache, args.bufsize)
    results = [resolver.resolve(name, qtype) for name in args.names]
    if args.cache_file:
        with open(args.cache_file, "w", encoding="utf-8") as handle:
            json.dump(cache.dump(), handle, indent=2)
    if args.json:
        print(json.dumps({"results": [resolution_json(r) for r in results]}, indent=2))
    else:
        print("\n\n".join(render_trace(r) if args.trace else render_resolution(r) for r in results))
    return 0 if all(r.status == "NOERROR" for r in results) else 1


def cmd_search(args: argparse.Namespace, transport: Any, clock: Any) -> int:
    hostname = args.hostname if args.hostname is not None else socket.gethostname()
    conf = parse_resolv_conf(read_text(args.resolv_conf), hostname, dict(os.environ) if args.env else None)
    plan = plan_search(args.name, conf)
    qtype = type_code(args.type)
    sent: List[Dict[str, Any]] = []
    found = None
    if args.send:
        link = transport or Transport(float(conf.timeout), conf.attempts)
        bufsize = DEFAULT_BUFSIZE if "edns0" in conf.options else 0

        def ask(fqdn: str) -> Tuple[str, int]:
            for server in conf.nameservers:
                try:
                    exchange = link.exchange(server, args.port, make_query(fqdn, qtype, rd=True, bufsize=bufsize))
                except QueryError:
                    continue
                return rcode_name(exchange.response.rcode), len(exchange.response.answer)
            return "TIMEOUT", 0

        sent, found = run_search(plan, ask)
    dots = args.name.rstrip(".").count(".")
    if args.json:
        print(json.dumps({
            "name": args.name,
            "dots": dots,
            "ndots": conf.ndots,
            "search": conf.search,
            "nameservers": conf.nameservers,
            "options": conf.options,
            "warnings": conf.warnings,
            "candidates": [{"name": c.name, "reason": c.reason} for c in plan],
            "queries": len(plan),
            "sent": sent,
            "found": found,
        }, indent=2))
    else:
        lines = [f"; resolv.conf: nameserver {' '.join(conf.nameservers)}; search {' '.join(conf.search) or '(none)'}; ndots:{conf.ndots}"]
        lines += [f"; warning: {w}" for w in conf.warnings]
        width = max(len(c.name) for c in plan)
        for number, c in enumerate(plan, 1):
            lines.append(f"  {number}  {c.name:<{width}}  {c.reason}")
        lines.append(f"; up to {len(plan)} quer{'y' if len(plan) == 1 else 'ies'} for one lookup, {2 * len(plan)} packets when the application asks for A and AAAA")
        for row in sent:
            lines.append(f";; sent {row['name']}: {row['status']}, {row['answers']} answer(s)")
        if found is not None:
            lines.append(f";; {'found' if found else 'not found'} after {len(sent)} quer{'y' if len(sent) == 1 else 'ies'}")
        print("\n".join(lines))
    return 0 if found is not False else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="minidig", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    def network(p: argparse.ArgumentParser) -> None:
        p.add_argument("--port", type=int, default=53, help="server port (default 53)")
        p.add_argument("--timeout", type=float, default=2.0, help="seconds to wait per UDP try (default 2)")
        p.add_argument("--tries", type=int, default=3, help="UDP tries before giving up (default 3)")
        p.add_argument("--json", action="store_true", help="machine-readable output")

    q = sub.add_parser("query", help="send one query, like dig @SERVER NAME TYPE")
    q.add_argument("words", nargs="+", help="[@SERVER] NAME [TYPE] [+norecurse] [+tcp] [+noedns] [+bufsize=N] [+ignore] [+short] [+dnssec]")
    q.add_argument("--server", help="server address (default: the first nameserver in --resolv-conf)")
    q.add_argument("--resolv-conf", default="/etc/resolv.conf")
    q.add_argument("--norecurse", action="store_true", help="clear the RD bit, as dig +norecurse")
    q.add_argument("--tcp", action="store_true", help="use TCP from the start, as dig +tcp")
    q.add_argument("--ignore", action="store_true", help="keep a truncated UDP reply instead of retrying over TCP, as dig +ignore")
    q.add_argument("--bufsize", type=int, default=DEFAULT_BUFSIZE, help="EDNS0 UDP payload size; 0 sends no OPT record (default 1232)")
    q.add_argument("--dnssec", action="store_true", help="set the DO bit")
    q.add_argument("--short", action="store_true", help="print only the answer data")
    network(q)
    q.set_defaults(func=cmd_query)

    r = sub.add_parser("resolve", help="resolve names iteratively from root hints")
    r.add_argument("names", nargs="+")
    r.add_argument("--type", default="A")
    r.add_argument("--root", action="append", help="root server as IP or NAME=IP (repeatable; default: the 13 root servers)")
    r.add_argument("--hints", help="root hints file in named.root format")
    r.add_argument("--trace", action="store_true", help="print every step, like dig +trace")
    r.add_argument("--cache-file", help="load the cache from this JSON file and save it back")
    r.add_argument("--bufsize", type=int, default=DEFAULT_BUFSIZE, help="EDNS0 UDP payload size; 0 sends no OPT record")
    r.add_argument("--tcp", action="store_true", help="use TCP for every query")
    network(r)
    r.set_defaults(func=cmd_resolve)

    s = sub.add_parser("search", help="list the names a stub resolver really asks for under search and ndots")
    s.add_argument("name")
    s.add_argument("--resolv-conf", default="/etc/resolv.conf")
    s.add_argument("--hostname", help="hostname whose domain is the default search list (default: this host's)")
    s.add_argument("--env", action="store_true", help="apply LOCALDOMAIN and RES_OPTIONS from the environment, as glibc does")
    s.add_argument("--send", action="store_true", help="send the queries to the nameservers and stop at the first answer, as glibc does")
    s.add_argument("--type", default="A")
    s.add_argument("--port", type=int, default=53)
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_search)
    return parser


def main(argv: Optional[Sequence[str]] = None, transport: Any = None, clock: Optional[Callable[[], float]] = None) -> int:
    args = build_parser().parse_args(list(sys.argv[1:] if argv is None else argv))
    try:
        return args.func(args, transport, clock)
    except InputError as exc:
        print(f"minidig: {exc}", file=sys.stderr)
        return 2
    except WireError as exc:
        print(f"minidig: malformed message: {exc}", file=sys.stderr)
        return 1
    except QueryError as exc:
        print(f"minidig: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
