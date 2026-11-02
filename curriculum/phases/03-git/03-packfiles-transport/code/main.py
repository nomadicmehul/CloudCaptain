"""minipack: Git packfiles, .idx v2 indexes, deltas, and a smart-HTTP protocol v2 client for full, shallow and partial clones."""
from __future__ import annotations

import argparse
import dataclasses
import hashlib
import heapq
import http.server
import json
import os
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import zlib
from dataclasses import dataclass, field
from typing import Callable, Dict, Iterable, List, Optional, Sequence, Set, Tuple

AGENT = "minipack/1.0"
USER_AGENT = "git/minipack-1.0"
OBJ_COMMIT, OBJ_TREE, OBJ_BLOB, OBJ_TAG, OBJ_OFS_DELTA, OBJ_REF_DELTA = 1, 2, 3, 4, 6, 7
BASE_TYPES = {OBJ_COMMIT: "commit", OBJ_TREE: "tree", OBJ_BLOB: "blob", OBJ_TAG: "tag"}
TYPE_CODES = {name: code for code, name in BASE_TYPES.items()}
TYPE_ORDER = {"commit": 0, "tag": 1, "tree": 2, "blob": 3}
FLUSH, DELIM, RESPONSE_END = b"0000", b"0001", b"0002"
SPECIAL_PACKETS = {0: "flush", 1: "delim", 2: "response-end"}
SPECIAL_TEXT = {"flush": "0000", "delim": "0001", "response-end": "0002"}
MAX_PKT_PAYLOAD = 65516
PACK_SIGNATURE = b"PACK"
IDX_SIGNATURE = b"\xfftOc"
COPY_MAX = 0x10000
INSERT_MAX = 0x7F
DELTA_BLOCK = 16
DELTA_BUCKET = 64
OFFSET32_LIMIT = 0x7FFFFFFF
CACHE_LIMIT = 1024
UPLOAD_PACK = "git-upload-pack"
DEFAULT_REFSPEC = "+refs/heads/*:refs/remotes/origin/*"
HEX40 = re.compile(r"^[0-9a-f]{40}$")
PKT_LENGTH = re.compile(rb"^[0-9a-fA-F]{4}$")
FILTER_SPEC = re.compile(r"^(blob:none|blob:limit=\d+[kmg]?|tree:\d+|object:type=(blob|tree|commit|tag))$")
TRACE_LINE = re.compile(r"packet:\s+(?P<who>[\w.-]+)(?P<direction>[<>]) ?(?P<text>.*)$")


class MiniError(Exception):
    code = 2


class InputError(MiniError):
    code = 2


class PackError(MiniError):
    code = 1


class ProtocolError(MiniError):
    code = 1


class MissingObject(MiniError):
    code = 1

    def __init__(self, oid: str):
        super().__init__(f"object {oid} is not in the repository")
        self.oid = oid


def object_id(type_name: str, data: bytes) -> str:
    return hashlib.sha1(b"%s %d\x00" % (type_name.encode(), len(data)) + data).hexdigest()


def read_bytes(path: str) -> bytes:
    try:
        with open(path, "rb") as handle:
            return handle.read()
    except OSError as error:
        raise InputError(f"cannot read {path}: {error.strerror}") from None


def write_file(path: str, data: bytes, mode: int = 0o644) -> None:
    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, exist_ok=True)
    handle, temporary = tempfile.mkstemp(dir=directory, prefix=".tmp-")
    with os.fdopen(handle, "wb") as out:
        out.write(data)
    os.chmod(temporary, mode)
    os.replace(temporary, path)


def unique(items: Iterable[str]) -> List[str]:
    seen: Set[str] = set()
    out = []
    for item in items:
        if item and item not in seen:
            seen.add(item)
            out.append(item)
    return out


def pkt_line(payload: object) -> bytes:
    data = payload.encode() if isinstance(payload, str) else bytes(payload)
    if len(data) > MAX_PKT_PAYLOAD:
        raise ProtocolError(f"pkt-line payload of {len(data)} bytes is over the limit of {MAX_PKT_PAYLOAD}")
    return b"%04x" % (len(data) + 4) + data


@dataclass
class Packet:
    kind: str
    data: bytes = b""

    def text(self) -> str:
        return self.data.decode("utf-8", "replace").rstrip("\n")

    def show(self) -> str:
        if self.kind != "data":
            return SPECIAL_TEXT[self.kind]
        if self.data.startswith(PACK_SIGNATURE):
            return "PACK ..."
        return self.text()


class PktReader:
    def __init__(self, data: bytes, on_packet: Optional[Callable[[Packet], None]] = None):
        self.data = data
        self.pos = 0
        self.on_packet = on_packet

    def at_end(self) -> bool:
        return self.pos >= len(self.data)

    def read(self) -> Packet:
        header = bytes(self.data[self.pos:self.pos + 4])
        if len(header) < 4:
            raise ProtocolError(f"the response ends inside a pkt-line length at byte {self.pos} ({header!r})")
        if not PKT_LENGTH.match(header):
            raise ProtocolError(f"bad pkt-line length {header!r} at byte {self.pos}: want four hex digits")
        length = int(header, 16)
        start = self.pos
        self.pos += 4
        if length in SPECIAL_PACKETS:
            packet = Packet(SPECIAL_PACKETS[length])
        elif length < 4:
            raise ProtocolError(f"invalid pkt-line length {length} at byte {start}")
        else:
            payload = bytes(self.data[self.pos:self.pos + length - 4])
            if len(payload) < length - 4:
                raise ProtocolError(f"pkt-line at byte {start} promises {length - 4} bytes, only {len(payload)} arrived")
            self.pos += length - 4
            packet = Packet("data", payload)
        if self.on_packet:
            self.on_packet(packet)
        return packet


def read_packets(data: bytes) -> List[Packet]:
    reader = PktReader(data)
    packets = []
    while not reader.at_end():
        packets.append(reader.read())
    return packets


class Trace:
    def __init__(self, who: str = "minipack", sink: Optional[Callable[[str], None]] = None):
        self.who = who
        self.sink = sink
        self.lines: List[str] = []
        self.pack_seen = False

    def emit(self, label: str, text: str) -> None:
        line = f"packet: {label} {text}"
        self.lines.append(line)
        if self.sink:
            self.sink(line)

    def sent(self, text: str) -> None:
        self.emit(f"{self.who}>", text)

    def received(self, packet: Packet) -> None:
        self.emit(f"{self.who}<", packet.show())

    def sideband(self, band: int, payload: bytes) -> None:
        if band == 1:
            if payload.startswith(PACK_SIGNATURE) and not self.pack_seen:
                self.pack_seen = True
                self.emit("sideband<", "PACK ...")
            return
        self.emit("sideband<", payload.decode("utf-8", "replace").rstrip())


def trace_sink(path: str) -> Optional[Callable[[str], None]]:
    target = path or os.environ.get("GIT_TRACE_PACKET", "")
    if not target or target.lower() in ("0", "false", "no"):
        return None

    def stamp(line: str) -> str:
        now = time.time()
        return f"{time.strftime('%H:%M:%S', time.localtime(now))}.{int(now % 1 * 1e6):06d} minipack.py          {line}\n"

    if target.lower() in ("1", "2", "true", "yes"):
        return lambda line: sys.stderr.write(stamp(line))
    if not os.path.isabs(target) and not path:
        return None

    def append(line: str) -> None:
        with open(target, "a", encoding="utf-8") as handle:
            handle.write(stamp(line))

    return append


def parse_trace(lines: Iterable[str], who: str = "") -> List[Dict[str, object]]:
    requests: List[Dict[str, object]] = []
    current: Optional[Dict[str, object]] = None
    stage = "capabilities"
    for line in lines:
        match = TRACE_LINE.search(line)
        if not match or match.group("direction") != ">":
            continue
        name, text = match.group("who"), match.group("text").rstrip()
        if not who:
            if name == "git" or not text.startswith("command="):
                continue
            who = name
        if name != who:
            continue
        if text.startswith("command="):
            current = {"command": text[len("command="):], "capabilities": [], "arguments": []}
            requests.append(current)
            stage = "capabilities"
        elif current is None:
            continue
        elif text == "0001":
            stage = "arguments"
        elif text == "0000":
            current = None
        else:
            current[stage].append(text)
    return requests


def encode_size(n: int) -> bytes:
    out = bytearray()
    while True:
        byte = n & 0x7F
        n >>= 7
        if n:
            out.append(byte | 0x80)
        else:
            out.append(byte)
            return bytes(out)


def decode_size(buf: bytes, pos: int) -> Tuple[int, int]:
    value = shift = 0
    while True:
        if pos >= len(buf):
            raise PackError("the delta ends inside its size header")
        byte = buf[pos]
        pos += 1
        value |= (byte & 0x7F) << shift
        shift += 7
        if not byte & 0x80:
            return value, pos


def copy_op(offset: int, size: int) -> bytes:
    if not 0 < size <= COPY_MAX or not 0 <= offset <= 0xFFFFFFFF:
        raise PackError(f"cannot encode a copy of {size} bytes at offset {offset}")
    op = 0x80
    args = bytearray()
    for i in range(4):
        byte = (offset >> (8 * i)) & 0xFF
        if byte:
            op |= 1 << i
            args.append(byte)
    stored = 0 if size == COPY_MAX else size
    for i in range(2):
        byte = (stored >> (8 * i)) & 0xFF
        if byte:
            op |= 0x10 << i
            args.append(byte)
    return bytes([op]) + bytes(args)


def insert_ops(literal: bytes) -> bytes:
    out = bytearray()
    for start in range(0, len(literal), INSERT_MAX):
        chunk = literal[start:start + INSERT_MAX]
        out.append(len(chunk))
        out += chunk
    return bytes(out)


def delta_index(base: bytes) -> Dict[bytes, List[int]]:
    index: Dict[bytes, List[int]] = {}
    for offset in range(0, len(base) - DELTA_BLOCK + 1, DELTA_BLOCK):
        bucket = index.setdefault(base[offset:offset + DELTA_BLOCK], [])
        if len(bucket) < DELTA_BUCKET:
            bucket.append(offset)
    return index


def common_length(a: bytes, a_pos: int, b: bytes, b_pos: int) -> int:
    limit = min(len(a) - a_pos, len(b) - b_pos)
    n = 0
    step = 256
    while n + step <= limit and a[a_pos + n:a_pos + n + step] == b[b_pos + n:b_pos + n + step]:
        n += step
    while n < limit and a[a_pos + n] == b[b_pos + n]:
        n += 1
    return n


def longest_match(index: Dict[bytes, List[int]], base: bytes, target: bytes, pos: int) -> Tuple[int, int]:
    best_offset, best_length = -1, 0
    for offset in index.get(target[pos:pos + DELTA_BLOCK], ()):
        length = common_length(target, pos, base, offset)
        if length > best_length:
            best_offset, best_length = offset, length
    return best_offset, best_length


def make_delta(base: bytes, target: bytes, index: Optional[Dict[bytes, List[int]]] = None) -> bytes:
    index = delta_index(base) if index is None else index
    out = bytearray(encode_size(len(base)) + encode_size(len(target)))
    literal = bytearray()
    pos = 0
    while pos < len(target):
        offset, length = longest_match(index, base, target, pos) if pos + DELTA_BLOCK <= len(target) else (-1, 0)
        if offset < 0:
            literal.append(target[pos])
            pos += 1
            continue
        while literal and offset > 0 and base[offset - 1] == literal[-1]:
            literal.pop()
            offset -= 1
            pos -= 1
            length += 1
        out += insert_ops(bytes(literal))
        literal.clear()
        done = 0
        while done < length:
            size = min(COPY_MAX, length - done)
            out += copy_op(offset + done, size)
            done += size
        pos += length
    out += insert_ops(bytes(literal))
    return bytes(out)


@dataclass
class DeltaOp:
    op: str
    offset: int
    size: int
    data: bytes = b""


def parse_delta(delta: bytes) -> Tuple[int, int, List[DeltaOp]]:
    base_size, pos = decode_size(delta, 0)
    result_size, pos = decode_size(delta, pos)
    ops: List[DeltaOp] = []
    while pos < len(delta):
        cmd = delta[pos]
        pos += 1
        if cmd & 0x80:
            offset = size = 0
            for i in range(4):
                if cmd & (1 << i):
                    if pos >= len(delta):
                        raise PackError("the delta ends inside a copy instruction")
                    offset |= delta[pos] << (8 * i)
                    pos += 1
            for i in range(3):
                if cmd & (0x10 << i):
                    if pos >= len(delta):
                        raise PackError("the delta ends inside a copy instruction")
                    size |= delta[pos] << (8 * i)
                    pos += 1
            ops.append(DeltaOp("copy", offset, size or COPY_MAX))
        elif cmd:
            if pos + cmd > len(delta):
                raise PackError(f"an insert of {cmd} bytes runs past the end of the delta")
            ops.append(DeltaOp("insert", 0, cmd, bytes(delta[pos:pos + cmd])))
            pos += cmd
        else:
            raise PackError("delta opcode 0 is reserved")
    return base_size, result_size, ops


def apply_delta(base: bytes, delta: bytes) -> bytes:
    base_size, result_size, ops = parse_delta(delta)
    if base_size != len(base):
        raise PackError(f"the delta expects a base of {base_size} bytes, the base has {len(base)}")
    out = bytearray()
    for op in ops:
        if op.op == "copy":
            if op.offset + op.size > len(base):
                raise PackError(f"a copy of {op.size} bytes at offset {op.offset} runs past the {len(base)}-byte base")
            out += base[op.offset:op.offset + op.size]
        else:
            out += op.data
    if len(out) != result_size:
        raise PackError(f"the delta produced {len(out)} bytes, its header promised {result_size}")
    return bytes(out)


def encode_entry_header(type_code: int, size: int) -> bytes:
    byte = (type_code << 4) | (size & 0x0F)
    size >>= 4
    out = bytearray()
    while size:
        out.append(byte | 0x80)
        byte = size & 0x7F
        size >>= 7
    out.append(byte)
    return bytes(out)


def decode_entry_header(buf: bytes, pos: int) -> Tuple[int, int, int]:
    if pos >= len(buf):
        raise PackError(f"expected an object header at byte {pos}, the pack ends there")
    byte = buf[pos]
    pos += 1
    type_code = (byte >> 4) & 0x07
    size = byte & 0x0F
    shift = 4
    while byte & 0x80:
        if pos >= len(buf):
            raise PackError("the pack ends inside an object header")
        byte = buf[pos]
        pos += 1
        size |= (byte & 0x7F) << shift
        shift += 7
    return type_code, size, pos


def encode_ofs(distance: int) -> bytes:
    out = [distance & 0x7F]
    distance >>= 7
    while distance:
        distance -= 1
        out.append(0x80 | (distance & 0x7F))
        distance >>= 7
    return bytes(reversed(out))


def decode_ofs(buf: bytes, pos: int) -> Tuple[int, int]:
    if pos >= len(buf):
        raise PackError("the pack ends inside an OFS_DELTA offset")
    byte = buf[pos]
    pos += 1
    value = byte & 0x7F
    while byte & 0x80:
        if pos >= len(buf):
            raise PackError("the pack ends inside an OFS_DELTA offset")
        byte = buf[pos]
        pos += 1
        value = ((value + 1) << 7) | (byte & 0x7F)
    return value, pos


def inflate_at(buf: bytes, pos: int, size: int) -> Tuple[bytes, int]:
    view = memoryview(buf)
    stream = zlib.decompressobj()
    parts = []
    cursor = pos
    chunk = size + 64
    try:
        while not stream.eof:
            if cursor >= len(buf):
                raise PackError(f"the zlib stream that starts at byte {pos} is truncated")
            piece = view[cursor:cursor + chunk]
            parts.append(stream.decompress(piece))
            cursor += len(piece)
            chunk = max(chunk * 2, 4096)
    except zlib.error as error:
        raise PackError(f"bad zlib stream at byte {pos}: {error}") from None
    end = cursor - len(stream.unused_data)
    data = b"".join(parts)
    if len(data) != size:
        raise PackError(f"the zlib stream at byte {pos} inflates to {len(data)} bytes, the object header says {size}")
    return data, end


@dataclass
class PackEntry:
    offset: int
    type_code: int
    size: int
    end: int
    data: bytes
    crc32: int
    base_offset: int = -1
    base_oid: str = ""
    oid: str = ""
    real_type: str = ""
    depth: int = 0
    base: str = ""


@dataclass
class IndexedPack:
    checksum: str
    size: int
    entries: List[PackEntry]
    objects: Dict[str, Tuple[str, bytes]]
    thin_bases: List[str] = field(default_factory=list)

    def records(self) -> List[Tuple[str, int, int]]:
        return [(e.oid, e.crc32, e.offset) for e in self.entries]


def read_pack_header(data: bytes) -> int:
    if len(data) < 32:
        raise PackError(f"{len(data)} bytes is too short for a pack (12-byte header plus 20-byte trailer)")
    if bytes(data[:4]) != PACK_SIGNATURE:
        raise PackError(f"not a packfile: it starts with {bytes(data[:4])!r}, a pack starts with b'PACK'")
    version, count = struct.unpack(">II", data[4:12])
    if version not in (2, 3):
        raise PackError(f"pack version {version} is not supported (Git writes version 2)")
    return count


def parse_pack(data: bytes) -> Tuple[List[PackEntry], str]:
    count = read_pack_header(data)
    trailer_at = len(data) - 20
    checksum = hashlib.sha1(memoryview(data)[:trailer_at]).digest()
    if checksum != bytes(data[trailer_at:]):
        raise PackError(f"pack checksum mismatch: the trailer says {bytes(data[trailer_at:]).hex()}, the content hashes to {checksum.hex()} (truncated or corrupted)")
    entries: List[PackEntry] = []
    pos = 12
    for _ in range(count):
        if pos >= trailer_at:
            raise PackError(f"the pack header promises {count} objects, the data ends after {len(entries)}")
        start = pos
        type_code, size, pos = decode_entry_header(data, pos)
        base_offset, base_oid = -1, ""
        if type_code == OBJ_OFS_DELTA:
            distance, pos = decode_ofs(data, pos)
            base_offset = start - distance
            if distance == 0 or base_offset < 12:
                raise PackError(f"the delta at byte {start} points {distance} bytes back, outside the pack")
        elif type_code == OBJ_REF_DELTA:
            base_oid = bytes(data[pos:pos + 20]).hex()
            pos += 20
        elif type_code not in BASE_TYPES:
            raise PackError(f"the object at byte {start} has type {type_code}, which is not a Git object type")
        payload, pos = inflate_at(data, pos, size)
        entries.append(PackEntry(start, type_code, size, pos, payload, zlib.crc32(memoryview(data)[start:pos]) & 0xFFFFFFFF, base_offset, base_oid))
    if pos != trailer_at:
        raise PackError(f"{trailer_at - pos} unexpected bytes after the last object")
    return entries, checksum.hex()


ExternalLookup = Callable[[str], Optional[Tuple[str, bytes]]]


def index_pack(data: bytes, external: Optional[ExternalLookup] = None) -> IndexedPack:
    entries, checksum = parse_pack(data)
    by_offset = {e.offset: e for e in entries}
    resolved: Dict[int, Tuple[str, bytes]] = {}
    by_oid: Dict[str, int] = {}
    thin: Dict[str, Tuple[str, bytes]] = {}
    for entry in entries:
        if entry.type_code in BASE_TYPES:
            entry.real_type = BASE_TYPES[entry.type_code]
            entry.oid = object_id(entry.real_type, entry.data)
            resolved[entry.offset] = (entry.real_type, entry.data)
            by_oid.setdefault(entry.oid, entry.offset)
    pending = [e for e in entries if e.offset not in resolved]
    while pending:
        waiting = []
        for entry in pending:
            base: Optional[Tuple[str, bytes]] = None
            depth, base_name = 0, entry.base_oid
            if entry.type_code == OBJ_OFS_DELTA:
                if entry.base_offset not in by_offset:
                    raise PackError(f"the delta at byte {entry.offset} points to byte {entry.base_offset}, where no object starts")
                if entry.base_offset in resolved:
                    base = resolved[entry.base_offset]
                    depth, base_name = by_offset[entry.base_offset].depth, by_offset[entry.base_offset].oid
            elif entry.base_oid in by_oid:
                base = resolved[by_oid[entry.base_oid]]
                depth = by_offset[by_oid[entry.base_oid]].depth
            elif entry.base_oid in thin:
                base = thin[entry.base_oid]
            elif external is not None:
                found = external(entry.base_oid)
                if found is not None:
                    thin[entry.base_oid] = found
                    base = found
            if base is None:
                waiting.append(entry)
                continue
            type_name, body = base[0], apply_delta(base[1], entry.data)
            entry.real_type, entry.depth, entry.base = type_name, depth + 1, base_name
            entry.oid = object_id(type_name, body)
            resolved[entry.offset] = (type_name, body)
            by_oid.setdefault(entry.oid, entry.offset)
        if len(waiting) == len(pending):
            missing = sorted({e.base_oid for e in waiting if e.base_oid and e.base_oid not in by_oid})
            raise PackError(f"{len(waiting)} deltas cannot be resolved; bases not in the pack: {', '.join(missing[:5]) or 'none'} (a thin pack needs them from the local repository)")
        pending = waiting
    objects = {by_offset[offset].oid: value for offset, value in resolved.items()}
    return IndexedPack(checksum, len(data), entries, objects, sorted(thin))


def write_idx(records: Iterable[Tuple[str, int, int]], pack_checksum: str) -> bytes:
    rows = sorted(records)
    out = bytearray(IDX_SIGNATURE + struct.pack(">I", 2))
    counts = [0] * 256
    for oid, _, _ in rows:
        counts[int(oid[:2], 16)] += 1
    total = 0
    for count in counts:
        total += count
        out += struct.pack(">I", total)
    for oid, _, _ in rows:
        out += bytes.fromhex(oid)
    for _, crc, _ in rows:
        out += struct.pack(">I", crc)
    large: List[int] = []
    for _, _, offset in rows:
        if offset > OFFSET32_LIMIT:
            out += struct.pack(">I", 0x80000000 | len(large))
            large.append(offset)
        else:
            out += struct.pack(">I", offset)
    for offset in large:
        out += struct.pack(">Q", offset)
    out += bytes.fromhex(pack_checksum)
    out += hashlib.sha1(out).digest()
    return bytes(out)


@dataclass
class IdxRecord:
    oid: str
    crc32: int
    offset: int


def read_idx(data: bytes) -> Tuple[List[IdxRecord], str]:
    if bytes(data[:4]) != IDX_SIGNATURE:
        raise PackError("not a version 2 .idx: it does not start with \\377tOc (version 1 indexes have no signature; rewrite with git index-pack)")
    version = struct.unpack(">I", data[4:8])[0]
    if version != 2:
        raise PackError(f".idx version {version} is not supported (want 2)")
    if hashlib.sha1(data[:-20]).digest() != bytes(data[-20:]):
        raise PackError(".idx checksum mismatch: the file is truncated or corrupted")
    fanout = struct.unpack(">256I", data[8:8 + 1024])
    if any(fanout[i] > fanout[i + 1] for i in range(255)):
        raise PackError("the .idx fanout table is not monotonic")
    count = fanout[255]
    pos = 8 + 1024
    oids = [bytes(data[pos + 20 * i:pos + 20 * i + 20]).hex() for i in range(count)]
    pos += 20 * count
    crcs = struct.unpack(f">{count}I", data[pos:pos + 4 * count])
    pos += 4 * count
    small = struct.unpack(f">{count}I", data[pos:pos + 4 * count])
    pos += 4 * count
    large_count = sum(1 for value in small if value & 0x80000000)
    large = struct.unpack(f">{large_count}Q", data[pos:pos + 8 * large_count])
    pos += 8 * large_count
    if pos + 40 != len(data):
        raise PackError(f"the .idx has {len(data) - pos - 40} unexpected bytes before its trailer")
    if oids != sorted(oids):
        raise PackError("the object names in the .idx are not sorted")
    for i, oid in enumerate(oids):
        first = int(oid[:2], 16)
        if not (fanout[first - 1] if first else 0) <= i < fanout[first]:
            raise PackError(f"the fanout table does not cover {oid}")
    offsets = [large[value & 0x7FFFFFFF] if value & 0x80000000 else value for value in small]
    records = [IdxRecord(oid, crc, offset) for oid, crc, offset in zip(oids, crcs, offsets)]
    return records, bytes(data[pos:pos + 20]).hex()


class PackFile:
    def __init__(self, pack_path: str, idx_path: str):
        self.pack_path = pack_path
        records, self.checksum = read_idx(read_bytes(idx_path))
        self.offsets = {r.oid: r.offset for r in records}
        self._data: Optional[bytes] = None
        self._cache: Dict[int, Tuple[str, bytes]] = {}

    @property
    def data(self) -> bytes:
        if self._data is None:
            data = read_bytes(self.pack_path)
            read_pack_header(data)
            if data[-20:].hex() != self.checksum:
                raise PackError(f"{self.pack_path}: trailer {data[-20:].hex()} does not match its .idx ({self.checksum})")
            self._data = data
        return self._data

    def __contains__(self, oid: str) -> bool:
        return oid in self.offsets

    def read(self, oid: str, lookup: Callable[[str], Tuple[str, bytes]]) -> Tuple[str, bytes]:
        return self.read_at(self.offsets[oid], lookup)

    def read_at(self, offset: int, lookup: Callable[[str], Tuple[str, bytes]]) -> Tuple[str, bytes]:
        data = self.data
        deltas: List[Tuple[int, bytes]] = []
        cursor = offset
        while True:
            if cursor in self._cache:
                base = self._cache[cursor]
                break
            type_code, size, pos = decode_entry_header(data, cursor)
            if type_code == OBJ_OFS_DELTA:
                distance, pos = decode_ofs(data, pos)
                if distance == 0 or cursor - distance < 12:
                    raise PackError(f"{self.pack_path}: the delta at byte {cursor} points outside the pack")
                deltas.append((cursor, inflate_at(data, pos, size)[0]))
                cursor -= distance
                continue
            if type_code == OBJ_REF_DELTA:
                base_oid = data[pos:pos + 20].hex()
                deltas.append((cursor, inflate_at(data, pos + 20, size)[0]))
                base = self.read(base_oid, lookup) if base_oid in self.offsets else lookup(base_oid)
                break
            if type_code not in BASE_TYPES:
                raise PackError(f"{self.pack_path}: invalid object type {type_code} at byte {cursor}")
            base = (BASE_TYPES[type_code], inflate_at(data, pos, size)[0])
            self._remember(cursor, base)
            break
        type_name, body = base
        for at, delta in reversed(deltas):
            body = apply_delta(body, delta)
            self._remember(at, (type_name, body))
        return type_name, body

    def _remember(self, offset: int, value: Tuple[str, bytes]) -> None:
        if len(self._cache) >= CACHE_LIMIT:
            self._cache.pop(next(iter(self._cache)))
        self._cache[offset] = value


def parse_config(text: str) -> Dict[str, str]:
    values: Dict[str, str] = {}
    section = ""
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line[0] in "#;":
            continue
        header = re.match(r'^\[([A-Za-z0-9.-]+)(?:\s+"((?:[^"\\]|\\.)*)")?\]$', line)
        if header:
            section = header.group(1).lower() + ("." + header.group(2) if header.group(2) is not None else "")
            continue
        key, _, value = line.partition("=")
        values[f"{section}.{key.strip().lower()}"] = value.strip().strip('"') if _ else "true"
    return values


class Repository:
    def __init__(self, path: str):
        root = os.path.abspath(path)
        dotgit = os.path.join(root, ".git")
        if os.path.isdir(dotgit):
            self.gitdir = dotgit
        elif os.path.isfile(os.path.join(root, "HEAD")) and os.path.isdir(os.path.join(root, "objects")):
            self.gitdir = root
        else:
            raise InputError(f"{path} is not a Git repository (no .git/, and no HEAD and objects/ of a bare repository)")
        self.objects_dir = os.path.join(self.gitdir, "objects")
        self._packs: Optional[List[PackFile]] = None

    @classmethod
    def init(cls, path: str) -> "Repository":
        gitdir = os.path.join(path, ".git")
        for sub in ("objects/pack", "objects/info", "refs/heads", "refs/tags"):
            os.makedirs(os.path.join(gitdir, sub), exist_ok=True)
        if not os.path.exists(os.path.join(gitdir, "HEAD")):
            write_file(os.path.join(gitdir, "HEAD"), b"ref: refs/heads/main\n")
        if not os.path.exists(os.path.join(gitdir, "config")):
            write_file(os.path.join(gitdir, "config"), b"[core]\n\trepositoryformatversion = 0\n\tbare = false\n")
        return cls(path)

    def packs(self) -> List[PackFile]:
        if self._packs is None:
            directory = os.path.join(self.objects_dir, "pack")
            found = []
            if os.path.isdir(directory):
                for name in sorted(os.listdir(directory)):
                    pack_path = os.path.join(directory, name[:-4] + ".pack")
                    if name.endswith(".idx") and os.path.exists(pack_path):
                        found.append(PackFile(pack_path, os.path.join(directory, name)))
            self._packs = found
        return self._packs

    def reload_packs(self) -> None:
        self._packs = None

    def loose_path(self, oid: str) -> str:
        return os.path.join(self.objects_dir, oid[:2], oid[2:])

    def has(self, oid: str) -> bool:
        return os.path.exists(self.loose_path(oid)) or any(oid in pack for pack in self.packs())

    def read(self, oid: str) -> Tuple[str, bytes]:
        path = self.loose_path(oid)
        if os.path.exists(path):
            try:
                raw = zlib.decompress(read_bytes(path))
            except zlib.error as error:
                raise PackError(f"loose object {oid} is corrupt: {error}") from None
            header, _, body = raw.partition(b"\x00")
            type_name, _, size = header.decode("ascii", "replace").partition(" ")
            if not size.isdigit() or int(size) != len(body):
                raise PackError(f"loose object {oid} has a bad header {header[:40]!r}")
            return type_name, body
        for pack in self.packs():
            if oid in pack:
                return pack.read(oid, self.read)
        raise MissingObject(oid)

    def write(self, type_name: str, data: bytes) -> str:
        oid = object_id(type_name, data)
        if not self.has(oid):
            write_file(self.loose_path(oid), zlib.compress(b"%s %d\x00" % (type_name.encode(), len(data)) + data), 0o444)
        return oid

    def all_objects(self) -> Set[str]:
        found: Set[str] = set()
        if os.path.isdir(self.objects_dir):
            for prefix in os.listdir(self.objects_dir):
                directory = os.path.join(self.objects_dir, prefix)
                if len(prefix) == 2 and os.path.isdir(directory):
                    found.update(prefix + name for name in os.listdir(directory) if HEX40.match(prefix + name))
        for pack in self.packs():
            found.update(pack.offsets)
        return found

    def packed_refs(self) -> Dict[str, str]:
        path = os.path.join(self.gitdir, "packed-refs")
        refs: Dict[str, str] = {}
        if os.path.exists(path):
            for line in read_bytes(path).decode().splitlines():
                oid, _, name = line.partition(" ")
                if HEX40.match(oid) and name:
                    refs[name] = oid
        return refs

    def read_ref(self, name: str, hops: int = 0) -> Optional[str]:
        if hops > 5:
            raise InputError(f"symbolic ref loop at {name}")
        path = os.path.join(self.gitdir, name)
        if os.path.isfile(path):
            content = read_bytes(path).decode().strip()
            if content.startswith("ref: "):
                return self.read_ref(content[5:], hops + 1)
            return content if HEX40.match(content) else None
        return self.packed_refs().get(name)

    def symref(self, name: str) -> str:
        path = os.path.join(self.gitdir, name)
        if os.path.isfile(path):
            content = read_bytes(path).decode().strip()
            if content.startswith("ref: "):
                return content[5:]
        return ""

    def refs(self) -> Dict[str, str]:
        found = dict(self.packed_refs())
        refs_dir = os.path.join(self.gitdir, "refs")
        for directory, _, files in os.walk(refs_dir):
            for file_name in files:
                name = os.path.relpath(os.path.join(directory, file_name), self.gitdir).replace(os.sep, "/")
                oid = self.read_ref(name)
                if oid:
                    found[name] = oid
        return dict(sorted(found.items()))

    def write_ref(self, name: str, oid: str) -> None:
        write_file(os.path.join(self.gitdir, name), f"{oid}\n".encode())

    def write_symref(self, name: str, target: str) -> None:
        write_file(os.path.join(self.gitdir, name), f"ref: {target}\n".encode())

    def shallow(self) -> Set[str]:
        path = os.path.join(self.gitdir, "shallow")
        if not os.path.exists(path):
            return set()
        return {line.strip() for line in read_bytes(path).decode().splitlines() if HEX40.match(line.strip())}

    def write_shallow(self, oids: Set[str]) -> None:
        path = os.path.join(self.gitdir, "shallow")
        if oids:
            write_file(path, "".join(f"{oid}\n" for oid in sorted(oids)).encode())
        elif os.path.exists(path):
            os.remove(path)

    def config(self) -> Dict[str, str]:
        path = os.path.join(self.gitdir, "config")
        return parse_config(read_bytes(path).decode()) if os.path.exists(path) else {}


def parse_commit(data: bytes) -> Tuple[str, List[str], int]:
    tree, parents, when = "", [], 0
    for line in data.split(b"\n"):
        if not line:
            break
        key, _, value = line.partition(b" ")
        if key == b"tree":
            tree = value.decode()
        elif key == b"parent":
            parents.append(value.decode())
        elif key == b"committer":
            stamp = re.search(rb"> (\d+) [+-]\d{4}$", value)
            when = int(stamp.group(1)) if stamp else 0
    if not HEX40.match(tree):
        raise PackError("a commit without a valid tree line")
    return tree, parents, when


def parse_tree(data: bytes) -> List[Tuple[str, str, str]]:
    entries = []
    pos = 0
    try:
        while pos < len(data):
            space = data.index(b" ", pos)
            nul = data.index(b"\x00", space)
            if nul + 21 > len(data):
                raise ValueError
            entries.append((data[pos:space].decode(), data[space + 1:nul].decode("utf-8", "surrogateescape"), data[nul + 1:nul + 21].hex()))
            pos = nul + 21
    except ValueError:
        raise PackError(f"corrupt tree entry at byte {pos}") from None
    return entries


def parse_tag(data: bytes) -> Tuple[str, str]:
    target = kind = ""
    for line in data.split(b"\n"):
        if not line:
            break
        key, _, value = line.partition(b" ")
        if key == b"object":
            target = value.decode()
        elif key == b"type":
            kind = value.decode()
    if not HEX40.match(target):
        raise PackError("a tag without a valid object line")
    return target, kind


@dataclass
class Walked:
    oid: str
    type: str
    path: str = ""
    missing: bool = False


def walk_objects(repo: Repository, tips: Sequence[str], shallow: Optional[Set[str]] = None, allow_missing: bool = False) -> List[Walked]:
    shallow = repo.shallow() if shallow is None else shallow
    seen: Set[str] = set()
    out: List[Walked] = []
    roots: List[str] = []
    heap: List[Tuple[int, int, str, List[str], str]] = []
    counter = 0

    def read(oid: str, kind: str) -> Optional[Tuple[str, bytes]]:
        try:
            return repo.read(oid)
        except MissingObject:
            if not allow_missing:
                raise MissingObject(oid) from None
            out.append(Walked(oid, kind, missing=True))
            return None

    def push(oid: str) -> None:
        nonlocal counter
        while oid not in seen:
            found = read(oid, "commit")
            if found is None:
                seen.add(oid)
                return
            kind, data = found
            if kind == "tag":
                seen.add(oid)
                out.append(Walked(oid, "tag"))
                oid = parse_tag(data)[0]
                continue
            if kind == "commit":
                tree, parents, when = parse_commit(data)
                seen.add(oid)
                heapq.heappush(heap, (-when, counter, oid, parents, tree))
                counter += 1
            elif kind == "tree":
                roots.append(oid)
            else:
                seen.add(oid)
                out.append(Walked(oid, kind))
            return

    for tip in tips:
        push(tip)
    while heap:
        _, _, oid, parents, tree = heapq.heappop(heap)
        out.append(Walked(oid, "commit"))
        roots.append(tree)
        if oid not in shallow:
            for parent in parents:
                push(parent)

    def visit(oid: str, path: str) -> None:
        if oid in seen:
            return
        seen.add(oid)
        found = read(oid, "tree")
        if found is None:
            return
        out.append(Walked(oid, "tree", path))
        for mode, name, child in parse_tree(found[1]):
            child_path = f"{path}/{name}" if path else name
            if mode == "160000":
                continue
            if mode in ("40000", "040000"):
                visit(child, child_path)
            elif child not in seen:
                seen.add(child)
                present = repo.has(child)
                if not present and not allow_missing:
                    raise MissingObject(child)
                out.append(Walked(child, "blob", child_path, missing=not present))

    for root in roots:
        visit(root, "")
    return out


@dataclass
class PackPlan:
    oid: str
    type: str
    data: bytes
    path: str = ""
    base: str = ""
    delta: bytes = b""
    depth: int = 0
    index: Optional[Dict[bytes, List[int]]] = None


def plan_pack(objects: Sequence[Tuple[str, str, bytes, str]], use_delta: bool = True, window: int = 10, max_depth: int = 10) -> List[PackPlan]:
    plans = [PackPlan(oid, kind, data, path) for oid, kind, data, path in objects]
    first = sorted((p for p in plans if p.type in ("commit", "tag")), key=lambda p: TYPE_ORDER[p.type])
    rest = sorted((p for p in plans if p.type not in ("commit", "tag")), key=lambda p: (TYPE_ORDER[p.type], os.path.basename(p.path), p.path, -len(p.data), p.oid))
    recent: Dict[str, List[PackPlan]] = {}
    for plan in rest:
        candidates = recent.setdefault(plan.type, [])
        if use_delta and window > 0:
            best: Optional[Tuple[PackPlan, bytes]] = None
            for candidate in reversed(candidates[-window:]):
                if candidate.depth >= max_depth:
                    continue
                if candidate.index is None:
                    candidate.index = delta_index(candidate.data)
                delta = make_delta(candidate.data, plan.data, candidate.index)
                if len(delta) < len(plan.data) // 2 and (best is None or len(delta) < len(best[1])):
                    best = (candidate, delta)
            if best:
                plan.base, plan.delta, plan.depth = best[0].oid, best[1], best[0].depth + 1
        candidates.append(plan)
    for plan in rest:
        plan.index = None
    return first + rest


def write_pack(plans: Sequence[PackPlan], ofs_delta: bool = True) -> Tuple[bytes, List[Tuple[str, int, int]], str]:
    out = bytearray(PACK_SIGNATURE + struct.pack(">II", 2, len(plans)))
    offsets: Dict[str, int] = {}
    records = []
    for plan in plans:
        start = len(out)
        if plan.base:
            if ofs_delta:
                if plan.base not in offsets:
                    raise PackError(f"the delta base {plan.base} of {plan.oid} must be written before it")
                entry = encode_entry_header(OBJ_OFS_DELTA, len(plan.delta)) + encode_ofs(start - offsets[plan.base])
            else:
                entry = encode_entry_header(OBJ_REF_DELTA, len(plan.delta)) + bytes.fromhex(plan.base)
            entry += zlib.compress(plan.delta)
        else:
            entry = encode_entry_header(TYPE_CODES[plan.type], len(plan.data)) + zlib.compress(plan.data)
        out += entry
        offsets[plan.oid] = start
        records.append((plan.oid, zlib.crc32(entry) & 0xFFFFFFFF, start))
    checksum = hashlib.sha1(out).digest()
    out += checksum
    return bytes(out), records, checksum.hex()


def resolve_rev(repo: Repository, name: str) -> str:
    if HEX40.match(name):
        if not repo.has(name):
            raise InputError(f"object {name} is not in {repo.gitdir}")
        return name
    for candidate in (name, f"refs/{name}", f"refs/heads/{name}", f"refs/tags/{name}", f"refs/remotes/{name}"):
        oid = repo.read_ref(candidate)
        if oid:
            return oid
    raise InputError(f"unknown revision {name!r} (give a ref name such as main or v1.0, or a full object id)")


def pack_tips(repo: Repository, revs: Sequence[str], everything: bool) -> List[str]:
    tips = [resolve_rev(repo, rev) for rev in revs]
    if everything:
        head = repo.read_ref("HEAD")
        tips += ([head] if head else []) + list(repo.refs().values())
    if not tips:
        raise InputError("nothing to pack: name revisions or pass --all")
    return unique(tips)


def pack_repository(repo: Repository, tips: Sequence[str], use_delta: bool = True, ofs_delta: bool = True, window: int = 10, max_depth: int = 10) -> Tuple[bytes, bytes, List[PackPlan]]:
    walked = walk_objects(repo, tips)
    objects = [(w.oid, w.type, repo.read(w.oid)[1], w.path) for w in walked]
    plans = plan_pack(objects, use_delta, window, max_depth)
    pack, records, checksum = write_pack(plans, ofs_delta)
    return pack, write_idx(records, checksum), plans


def pack_summary(indexed: IndexedPack) -> Dict[str, object]:
    by_type: Dict[str, int] = {}
    chains: Dict[str, int] = {}
    for entry in indexed.entries:
        by_type[entry.real_type] = by_type.get(entry.real_type, 0) + 1
        if entry.depth:
            chains[str(entry.depth)] = chains.get(str(entry.depth), 0) + 1
    return {
        "checksum": indexed.checksum,
        "bytes": indexed.size,
        "objects": len(indexed.entries),
        "by_type": dict(sorted(by_type.items(), key=lambda kv: TYPE_ORDER.get(kv[0], 9))),
        "deltas": sum(chains.values()),
        "chains": chains,
        "thin_bases": len(indexed.thin_bases),
    }


def repo_lookup(repo: Repository) -> ExternalLookup:
    def lookup(oid: str) -> Optional[Tuple[str, bytes]]:
        try:
            return repo.read(oid)
        except MissingObject:
            return None
    return lookup


def store_pack(repo: Repository, pack: bytes, keep: bool, promisor: bool = False, promisor_lines: Sequence[str] = ()) -> Dict[str, object]:
    indexed = index_pack(pack, None if keep else repo_lookup(repo))
    summary = pack_summary(indexed)
    if keep:
        base = os.path.join(repo.objects_dir, "pack", f"pack-{indexed.checksum}")
        write_file(base + ".pack", pack, 0o444)
        write_file(base + ".idx", write_idx(indexed.records(), indexed.checksum), 0o444)
        if promisor:
            write_file(base + ".promisor", "".join(f"{line}\n" for line in promisor_lines).encode(), 0o444)
        repo.reload_packs()
        summary.update(storage="pack", pack_file=base + ".pack")
    else:
        for type_name, data in indexed.objects.values():
            repo.write(type_name, data)
        summary.update(storage="loose", pack_file="")
    return summary


@dataclass
class RemoteRef:
    name: str
    oid: str
    symref_target: str = ""
    peeled: str = ""


def parse_ref_line(text: str) -> RemoteRef:
    if text.startswith("ERR "):
        raise ProtocolError(f"server error: {text[4:]}")
    fields = text.split(" ")
    if len(fields) < 2 or not (HEX40.match(fields[0]) or fields[0] == "unborn"):
        raise ProtocolError(f"bad ls-refs line {text[:80]!r}: want '<oid> <refname> [attributes]'")
    ref = RemoteRef(fields[1], "" if fields[0] == "unborn" else fields[0])
    for attribute in fields[2:]:
        key, _, value = attribute.partition(":")
        if key == "symref-target":
            ref.symref_target = value
        elif key == "peeled":
            ref.peeled = value
    return ref


@dataclass
class FetchArgs:
    wants: List[str]
    haves: List[str] = field(default_factory=list)
    done: bool = True
    depth: int = 0
    shallow: List[str] = field(default_factory=list)
    filter_spec: str = ""
    include_tag: bool = False


def fetch_arguments(args: FetchArgs) -> List[str]:
    out = ["no-progress"]
    if args.include_tag:
        out.append("include-tag")
    out.append("ofs-delta")
    out += [f"shallow {oid}" for oid in args.shallow]
    if args.depth:
        out.append(f"deepen {args.depth}")
    if args.filter_spec:
        out.append(f"filter {args.filter_spec}")
    out += [f"want {oid}" for oid in args.wants]
    out += [f"have {oid}" for oid in args.haves]
    if args.done:
        out.append("done")
    return out


@dataclass
class FetchResult:
    sections: List[str] = field(default_factory=list)
    acks: List[str] = field(default_factory=list)
    nak: bool = False
    ready: bool = False
    shallow: List[str] = field(default_factory=list)
    unshallow: List[str] = field(default_factory=list)
    progress: List[str] = field(default_factory=list)
    pack: bytes = b""


def read_sideband(reader: PktReader, result: FetchResult, trace: Optional[Trace] = None) -> bytes:
    pack = bytearray()
    while True:
        packet = reader.read()
        if packet.kind == "flush":
            if trace:
                trace.emit("sideband<", "0000")
            return bytes(pack)
        if packet.kind != "data" or not packet.data:
            raise ProtocolError(f"unexpected {packet.kind} packet inside the packfile section")
        band, payload = packet.data[0], packet.data[1:]
        if trace:
            trace.sideband(band, payload)
        if band == 1:
            pack += payload
        elif band == 2:
            result.progress.append(payload.decode("utf-8", "replace").rstrip())
        elif band == 3:
            raise ProtocolError(f"server error: {payload.decode('utf-8', 'replace').strip()}")
        else:
            raise ProtocolError(f"unknown sideband channel {band}")


def parse_fetch_response(reader: PktReader, trace: Optional[Trace] = None) -> FetchResult:
    result = FetchResult()
    known = ("acknowledgments", "shallow-info", "wanted-refs", "packfile-uris")
    while True:
        packet = reader.read()
        if packet.kind == "flush":
            return result
        header = packet.text()
        if header.startswith("ERR "):
            raise ProtocolError(f"server error: {header[4:]}")
        result.sections.append(header)
        if header == "packfile":
            reader.on_packet = None
            result.pack = read_sideband(reader, result, trace)
            return result
        if header not in known:
            raise ProtocolError(f"unknown section {header!r} in the fetch response")
        while True:
            packet = reader.read()
            if packet.kind == "flush":
                return result
            if packet.kind == "delim":
                break
            line = packet.text()
            if header == "acknowledgments":
                if line == "NAK":
                    result.nak = True
                elif line == "ready":
                    result.ready = True
                elif line.startswith("ACK ") and HEX40.match(line[4:]):
                    result.acks.append(line[4:])
                else:
                    raise ProtocolError(f"bad acknowledgments line {line!r}")
            elif header == "shallow-info":
                kind, _, oid = line.partition(" ")
                if kind not in ("shallow", "unshallow") or not HEX40.match(oid):
                    raise ProtocolError(f"bad shallow-info line {line!r}")
                (result.shallow if kind == "shallow" else result.unshallow).append(oid)
            elif header == "packfile-uris":
                raise ProtocolError("the server sent packfile-uris, which minipack never asks for")


Transport = Callable[[str, str, Dict[str, str], Optional[bytes]], Tuple[int, Dict[str, str], bytes]]


def urllib_transport(timeout: float = 60.0) -> Transport:
    def send(method: str, url: str, headers: Dict[str, str], body: Optional[bytes]) -> Tuple[int, Dict[str, str], bytes]:
        request = urllib.request.Request(url, data=body, method=method, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return response.status, {k.lower(): v for k, v in response.headers.items()}, response.read()
        except urllib.error.HTTPError as error:
            return error.code, {k.lower(): v for k, v in error.headers.items()}, error.read()
        except (urllib.error.URLError, OSError) as error:
            reason = getattr(error, "reason", error)
            raise InputError(f"cannot reach {url}: {reason}") from None
    return send


def check_status(status: int, url: str, body: bytes) -> None:
    if status == 200:
        return
    text = body[:200].decode("utf-8", "replace").strip()
    detail = f" ({text})" if text else ""
    if status in (401, 403):
        raise InputError(f"{url}: HTTP {status}: the server wants credentials or refuses this repository{detail}")
    if status == 404:
        raise InputError(f"{url}: HTTP 404: no repository at this URL{detail}")
    raise ProtocolError(f"{url}: HTTP {status}{detail}")


class Remote:
    def __init__(self, url: str, transport: Optional[Transport] = None, trace: Optional[Trace] = None):
        parts = urllib.parse.urlsplit(url)
        if parts.scheme not in ("http", "https") or not parts.netloc:
            raise InputError(f"{url!r} is not an http:// or https:// URL; minipack speaks smart HTTP only")
        self.url = url.rstrip("/")
        self.transport = transport or urllib_transport()
        self.trace = trace or Trace()
        self.caps: Dict[str, str] = {}

    def features(self, command: str) -> Set[str]:
        return set(self.caps.get(command, "").split())

    def discover(self) -> Dict[str, str]:
        url = f"{self.url}/info/refs?service={UPLOAD_PACK}"
        status, headers, body = self.transport("GET", url, {"Git-Protocol": "version=2", "User-Agent": USER_AGENT}, None)
        check_status(status, url, body)
        content_type = headers.get("content-type", "")
        if content_type != f"application/x-{UPLOAD_PACK}-advertisement":
            raise ProtocolError(f"{url} answered with Content-Type {content_type or '(none)'}, not application/x-{UPLOAD_PACK}-advertisement: a dumb HTTP server, or not a Git URL")
        reader = PktReader(body, self.trace.received)
        first = reader.read()
        if first.data.startswith(b"# service="):
            if reader.read().kind != "flush":
                raise ProtocolError("the service announcement is not followed by a flush packet")
            first = reader.read()
        if first.text() != "version 2":
            raise ProtocolError(f"the server answered with protocol v0/v1 ({first.text()[:60]!r}); it ignored Git-Protocol: version=2 (an old Git, or a proxy that drops the header)")
        caps: Dict[str, str] = {}
        while True:
            packet = reader.read()
            if packet.kind == "flush":
                break
            key, _, value = packet.text().partition("=")
            caps[key] = value
        if caps.get("object-format", "sha1") != "sha1":
            raise ProtocolError(f"the repository uses object-format={caps['object-format']}; minipack implements SHA-1 only")
        self.caps = caps
        return caps

    def request(self, command: str, arguments: Sequence[str]) -> PktReader:
        if not self.caps:
            self.discover()
        if command not in self.caps:
            raise ProtocolError(f"the server does not offer the {command} command (it offers {', '.join(sorted(self.caps))})")
        lines = [f"command={command}"]
        if "agent" in self.caps:
            lines.append(f"agent={AGENT}")
        if "object-format" in self.caps:
            lines.append("object-format=sha1")
        body = bytearray()
        for line in lines:
            body += pkt_line(line + "\n")
            self.trace.sent(line)
        body += DELIM
        self.trace.sent("0001")
        for line in arguments:
            body += pkt_line(line + "\n")
            self.trace.sent(line)
        body += FLUSH
        self.trace.sent("0000")
        url = f"{self.url}/{UPLOAD_PACK}"
        headers = {
            "Content-Type": f"application/x-{UPLOAD_PACK}-request",
            "Accept": f"application/x-{UPLOAD_PACK}-result",
            "Git-Protocol": "version=2",
            "User-Agent": USER_AGENT,
        }
        status, response_headers, data = self.transport("POST", url, headers, bytes(body))
        check_status(status, url, data)
        content_type = response_headers.get("content-type", "")
        if content_type != f"application/x-{UPLOAD_PACK}-result":
            raise ProtocolError(f"{url} answered with Content-Type {content_type or '(none)'}, not application/x-{UPLOAD_PACK}-result")
        return PktReader(data, self.trace.received)

    def ls_refs(self, prefixes: Sequence[str] = ("refs/heads/", "refs/tags/", "HEAD")) -> List[RemoteRef]:
        if not self.caps:
            self.discover()
        arguments = ["peel", "symrefs"]
        if "unborn" in self.features("ls-refs"):
            arguments.append("unborn")
        arguments += [f"ref-prefix {prefix}" for prefix in prefixes]
        reader = self.request("ls-refs", arguments)
        refs = []
        while True:
            packet = reader.read()
            if packet.kind == "flush":
                return refs
            if packet.kind != "data":
                raise ProtocolError(f"unexpected {packet.kind} packet in the ls-refs response")
            refs.append(parse_ref_line(packet.text()))

    def fetch(self, args: FetchArgs) -> FetchResult:
        features = self.features("fetch")
        if args.depth and "shallow" not in features:
            raise ProtocolError("the server does not support shallow fetches (its fetch capability lacks shallow)")
        if args.filter_spec and "filter" not in features:
            raise ProtocolError("the server does not support filters (uploadpack.allowFilter is not set)")
        self.trace.pack_seen = False
        return parse_fetch_response(self.request("fetch", fetch_arguments(args)), self.trace)


class HaveWalker:
    def __init__(self, repo: Repository, tips: Sequence[str], shallow: Optional[Set[str]] = None):
        self.repo = repo
        self.shallow = shallow or set()
        self.heap: List[Tuple[int, int, str, List[str]]] = []
        self.seen: Set[str] = set()
        self.common: Set[str] = set()
        self.counter = 0
        for tip in tips:
            self.push(tip)

    def commit(self, oid: str) -> Optional[Tuple[str, List[str], int]]:
        try:
            kind, data = self.repo.read(oid)
            while kind == "tag":
                oid = parse_tag(data)[0]
                kind, data = self.repo.read(oid)
        except MissingObject:
            return None
        if kind != "commit":
            return None
        tree, parents, when = parse_commit(data)
        return oid, parents, when

    def push(self, oid: str) -> None:
        found = self.commit(oid)
        if found is None or found[0] in self.seen:
            return
        oid, parents, when = found
        self.seen.add(oid)
        heapq.heappush(self.heap, (-when, self.counter, oid, parents))
        self.counter += 1

    def next_batch(self, size: int) -> List[str]:
        batch: List[str] = []
        while self.heap and len(batch) < size:
            _, _, oid, parents = heapq.heappop(self.heap)
            if oid in self.common:
                self.common.update(parents)
                continue
            batch.append(oid)
            if oid not in self.shallow:
                for parent in parents:
                    self.push(parent)
        return batch

    def mark_common(self, oids: Iterable[str]) -> None:
        for oid in oids:
            self.common.add(oid)
            found = self.commit(oid)
            if found:
                self.common.add(found[0])
                self.common.update(found[1])

    def exhausted(self) -> bool:
        return not self.heap


def negotiate(remote: Remote, args: FetchArgs, walker: HaveWalker, known: Sequence[str] = (), first_batch: int = 16, max_rounds: int = 64) -> Tuple[FetchResult, List[Dict[str, object]]]:
    common = unique(known)
    walker.mark_common(common)
    acked = False
    rounds: List[Dict[str, object]] = []
    batch = first_batch
    while len(rounds) < max_rounds:
        fresh = [] if acked or walker.exhausted() else walker.next_batch(batch)
        done = acked or walker.exhausted()
        result = remote.fetch(dataclasses.replace(args, haves=common + fresh, done=done))
        new = [oid for oid in result.acks if oid not in common]
        common += new
        walker.mark_common(new)
        acked = acked or bool(result.acks)
        rounds.append({"haves": len(fresh), "common": len(common), "acks": len(result.acks), "ready": result.ready, "done": done})
        if result.pack:
            return result, rounds
        if done or result.ready:
            raise ProtocolError("the server ended the negotiation without a packfile")
        batch *= 2
    raise ProtocolError(f"no common commits after {max_rounds} rounds of haves")


def apply_shallow(repo: Repository, result: FetchResult) -> List[str]:
    current = repo.shallow()
    if result.shallow or result.unshallow:
        current = (current | set(result.shallow)) - set(result.unshallow)
        repo.write_shallow(current)
    return sorted(current)


def clone_config(url: str, branch: str, refspec: str, filter_spec: str) -> bytes:
    lines = ["[core]", f"\trepositoryformatversion = {1 if filter_spec else 0}", "\tfilemode = true", "\tbare = false", "\tlogallrefupdates = true", '[remote "origin"]', f"\turl = {url}", f"\tfetch = {refspec}"]
    if filter_spec:
        lines += ["\tpromisor = true", f"\tpartialclonefilter = {filter_spec}"]
    if branch:
        lines += [f'[branch "{branch}"]', "\tremote = origin", f"\tmerge = refs/heads/{branch}"]
    return ("\n".join(lines) + "\n").encode()


def map_ref(refspec: str, name: str) -> Optional[str]:
    source, _, destination = refspec.lstrip("+").partition(":")
    if "*" not in source:
        return destination if name == source else None
    prefix, _, suffix = source.partition("*")
    if name.startswith(prefix) and name.endswith(suffix) and len(name) >= len(prefix) + len(suffix):
        return destination.replace("*", name[len(prefix):len(name) - len(suffix)], 1)
    return None


@dataclass
class ClonePlan:
    advertised: List[RemoteRef]
    head: Optional[RemoteRef]
    default: str
    branch: str
    single: bool
    wanted: List[RemoteRef]
    tags: List[RemoteRef]

    @property
    def head_refs(self) -> List[RemoteRef]:
        return [self.head] if self.head and self.head.oid else []

    @property
    def wants(self) -> List[str]:
        return unique(r.oid for r in self.head_refs + self.wanted)

    @property
    def refspec(self) -> str:
        return f"+{self.default}:refs/remotes/origin/{self.branch}" if self.single and self.branch else DEFAULT_REFSPEC


def plan_clone(advertised: List[RemoteRef], single: bool) -> ClonePlan:
    head = next((r for r in advertised if r.name == "HEAD"), None)
    branches = [r for r in advertised if r.name.startswith("refs/heads/") and r.oid]
    tags = [r for r in advertised if r.name.startswith("refs/tags/") and r.oid]
    default = head.symref_target if head and head.symref_target else (branches[0].name if branches else "refs/heads/main")
    branch = default[len("refs/heads/"):] if default.startswith("refs/heads/") else ""
    wanted = [r for r in branches if r.name == default] if single else branches + tags
    return ClonePlan(advertised, head, default, branch, single, wanted, tags)


def clone(url: str, directory: str, depth: int = 0, filter_spec: str = "", keep_pack: bool = False, transport: Optional[Transport] = None, trace: Optional[Trace] = None) -> Dict[str, object]:
    if depth < 0:
        raise InputError("--depth must be a positive number of commits")
    if filter_spec and not FILTER_SPEC.match(filter_spec):
        raise InputError(f"unsupported filter {filter_spec!r} (try blob:none, blob:limit=1m or tree:0)")
    if os.path.exists(directory) and (not os.path.isdir(directory) or os.listdir(directory)):
        raise InputError(f"destination path {directory!r} already exists and is not an empty directory")
    trace = trace or Trace()
    remote = Remote(url, transport, trace)
    remote.discover()
    warnings: List[str] = []
    if filter_spec and "filter" not in remote.features("fetch"):
        warnings.append("filtering not recognized by server, ignoring (the server needs uploadpack.allowFilter=true)")
        filter_spec = ""
    if depth and "shallow" not in remote.features("fetch"):
        raise ProtocolError("the server does not support shallow fetches (its fetch capability lacks shallow)")
    plan = plan_clone(remote.ls_refs(), depth > 0)
    summary: Dict[str, object] = {
        "url": url, "directory": directory, "head": plan.default, "depth": depth, "filter": filter_spec, "single_branch": plan.single,
        "advertised": [dataclasses.asdict(r) for r in plan.advertised], "wants": plan.wants, "warnings": warnings,
        "pack": {}, "shallow": [], "refs": {},
    }
    created = not os.path.exists(directory)
    try:
        populate_clone(remote, plan, directory, depth, filter_spec, keep_pack, summary)
    except MiniError:
        shutil.rmtree(directory if created else os.path.join(directory, ".git"), ignore_errors=True)
        raise
    summary["requests"] = parse_trace(trace.lines, trace.who)
    return summary


def populate_clone(remote: Remote, plan: ClonePlan, directory: str, depth: int, filter_spec: str, keep_pack: bool, summary: Dict[str, object]) -> None:
    repo = Repository.init(directory)
    write_file(os.path.join(repo.gitdir, "config"), clone_config(remote.url, plan.branch, plan.refspec, filter_spec))
    repo.write_symref("HEAD", plan.default)
    if not plan.wants:
        summary["warnings"].append("You appear to have cloned an empty repository.")
        return
    result = remote.fetch(FetchArgs(plan.wants, depth=depth, filter_spec=filter_spec, include_tag=plan.single))
    if not result.pack:
        raise ProtocolError("the server answered the fetch without a packfile")
    lines = [f"{r.oid} {r.name}" for r in plan.head_refs + plan.wanted]
    summary["pack"] = store_pack(repo, result.pack, keep_pack or bool(filter_spec), bool(filter_spec), lines)
    summary["shallow"] = apply_shallow(repo, result)
    written: Dict[str, str] = {}
    for ref in plan.wanted:
        if ref.name.startswith("refs/heads/"):
            written["refs/remotes/origin/" + ref.name[len("refs/heads/"):]] = ref.oid
    for ref in plan.tags:
        if repo.has(ref.oid):
            written[ref.name] = ref.oid
    if plan.head_refs and plan.branch:
        written[plan.default] = plan.head_refs[0].oid
    for name, oid in written.items():
        repo.write_ref(name, oid)
    if plan.head_refs and plan.branch:
        repo.write_symref("refs/remotes/origin/HEAD", f"refs/remotes/origin/{plan.branch}")
    elif plan.head_refs:
        repo.write_ref("HEAD", plan.head_refs[0].oid)
    summary["refs"] = dict(sorted(written.items()))


def fetch_into(directory: str, keep_pack: bool = False, transport: Optional[Transport] = None, trace: Optional[Trace] = None) -> Dict[str, object]:
    repo = Repository(directory)
    config = repo.config()
    url = config.get("remote.origin.url", "")
    if not url:
        raise InputError(f"{directory}: no remote.origin.url in the config; minipack fetch follows the origin remote only")
    refspec = config.get("remote.origin.fetch", DEFAULT_REFSPEC)
    promisor = config.get("remote.origin.promisor", "").lower() == "true"
    filter_spec = config.get("remote.origin.partialclonefilter", "") if promisor else ""
    trace = trace or Trace()
    remote = Remote(url, transport, trace)
    remote.discover()
    advertised = remote.ls_refs()
    mapped = [(map_ref(refspec, r.name), r) for r in advertised if r.oid]
    updates = [(local, r) for local, r in mapped if local]
    before = repo.refs()
    wants = unique(r.oid for _, r in updates if not repo.has(r.oid))
    shallow = repo.shallow()
    summary: Dict[str, object] = {"url": url, "directory": directory, "wants": wants, "rounds": [], "pack": {}, "updated": {}, "shallow": sorted(shallow)}
    if wants:
        walker = HaveWalker(repo, unique(before.values()), shallow)
        known = [found[0] for found in (walker.commit(r.oid) for r in advertised if r.oid and repo.has(r.oid)) if found]
        args = FetchArgs(wants, shallow=sorted(shallow), filter_spec=filter_spec, include_tag=True)
        result, rounds = negotiate(remote, args, walker, known)
        summary["rounds"] = rounds
        lines = [f"{r.oid} {r.name}" for _, r in updates]
        summary["pack"] = store_pack(repo, result.pack, keep_pack or promisor, promisor, lines)
        summary["shallow"] = apply_shallow(repo, result)
    updated: Dict[str, List[str]] = {}
    for local, ref in updates:
        if repo.has(ref.oid) and before.get(local) != ref.oid:
            repo.write_ref(local, ref.oid)
            updated[local] = [before.get(local, ""), ref.oid]
    for ref in advertised:
        if ref.name.startswith("refs/tags/") and ref.oid and ref.name not in before and repo.has(ref.oid):
            repo.write_ref(ref.name, ref.oid)
            updated[ref.name] = ["", ref.oid]
    summary["updated"] = dict(sorted(updated.items()))
    summary["requests"] = parse_trace(trace.lines, trace.who)
    return summary


def verify_pack(path: str) -> Dict[str, object]:
    stem = path[:-5] if path.endswith(".pack") else path[:-4] if path.endswith(".idx") else path
    pack_path, idx_path = stem + ".pack", stem + ".idx"
    if not os.path.exists(idx_path):
        raise InputError(f"{idx_path} does not exist; verify-pack checks a pack against its index (create one with index-pack)")
    data = read_bytes(pack_path)
    indexed = index_pack(data)
    problems: List[str] = []
    given = read_bytes(idx_path)
    computed = write_idx(indexed.records(), indexed.checksum)
    if given != computed:
        records, checksum = read_idx(given)
        if checksum != indexed.checksum:
            problems.append(f"the .idx names pack {checksum}, the pack is {indexed.checksum}")
        theirs = {r.oid: (r.crc32, r.offset) for r in records}
        ours = {oid: (crc, offset) for oid, crc, offset in indexed.records()}
        for oid in sorted(set(theirs) ^ set(ours)):
            problems.append(f"{oid} is {'only in the .idx' if oid in theirs else 'missing from the .idx'}")
        for oid in sorted(set(theirs) & set(ours)):
            if theirs[oid] != ours[oid]:
                problems.append(f"{oid}: .idx has crc32 {theirs[oid][0]:08x} offset {theirs[oid][1]}, the pack has crc32 {ours[oid][0]:08x} offset {ours[oid][1]}")
        if not problems:
            problems.append("the .idx content matches but its bytes differ from the canonical layout")
    ordered = sorted(indexed.entries, key=lambda e: e.offset)
    rows = []
    for i, entry in enumerate(ordered):
        end = ordered[i + 1].offset if i + 1 < len(ordered) else len(data) - 20
        rows.append({"oid": entry.oid, "type": entry.real_type, "size": entry.size, "packed": end - entry.offset, "offset": entry.offset, "depth": entry.depth, "base": entry.base})
    summary = pack_summary(indexed)
    return {"pack": pack_path, "idx": idx_path, "ok": not problems, "problems": problems, "objects": rows, "non_delta": len(rows) - int(summary["deltas"]), "chains": summary["chains"], "checksum": indexed.checksum}


def git_http_handler(root: str, git: str = "git", verbose: bool = False) -> type:
    class GitHttpHandler(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.0"

        def request_body(self) -> bytes:
            if "chunked" in self.headers.get("Transfer-Encoding", "").lower():
                parts = []
                while True:
                    size = int(self.rfile.readline().split(b";")[0].strip() or b"0", 16)
                    if size == 0:
                        while self.rfile.readline() not in (b"\r\n", b"\n", b""):
                            pass
                        return b"".join(parts)
                    parts.append(self.rfile.read(size))
                    self.rfile.readline()
            length = int(self.headers.get("Content-Length") or 0)
            return self.rfile.read(length) if length else b""

        def handle_git(self) -> None:
            url = urllib.parse.urlsplit(self.path)
            body = self.request_body()
            env = {
                "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
                "GIT_PROJECT_ROOT": root,
                "GIT_HTTP_EXPORT_ALL": "1",
                "REQUEST_METHOD": self.command,
                "PATH_INFO": urllib.parse.unquote(url.path),
                "QUERY_STRING": url.query,
                "CONTENT_TYPE": self.headers.get("Content-Type", ""),
                "CONTENT_LENGTH": str(len(body)),
                "REMOTE_ADDR": self.client_address[0],
            }
            if self.headers.get("Content-Encoding"):
                env["HTTP_CONTENT_ENCODING"] = self.headers["Content-Encoding"]
            if self.headers.get("Git-Protocol"):
                env["GIT_PROTOCOL"] = self.headers["Git-Protocol"]
            result = subprocess.run([git, "http-backend"], input=body, env=env, capture_output=True)
            head, separator, payload = result.stdout.partition(b"\r\n\r\n")
            if not separator:
                head, separator, payload = result.stdout.partition(b"\n\n")
            if not separator:
                self.send_error(500, f"git http-backend failed: {result.stderr.decode('utf-8', 'replace').strip()[:200]}")
                return
            status, headers = 200, []
            for line in head.decode("latin-1").splitlines():
                name, _, value = line.partition(":")
                if name.lower() == "status":
                    status = int(value.strip().split()[0])
                elif name:
                    headers.append((name, value.strip()))
            self.send_response(status)
            for name, value in headers:
                self.send_header(name, value)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        do_GET = handle_git
        do_POST = handle_git

        def log_message(self, fmt: str, *args: object) -> None:
            if verbose:
                sys.stderr.write("minipack serve %s %s\n" % (self.address_string(), fmt % args))

    return GitHttpHandler


def start_server(root: str, host: str = "127.0.0.1", port: int = 0, verbose: bool = False) -> http.server.ThreadingHTTPServer:
    server = http.server.ThreadingHTTPServer((host, port), git_http_handler(os.path.abspath(root), verbose=verbose))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def server_url(server: http.server.HTTPServer) -> str:
    host, port = server.server_address[:2]
    return f"http://{host}:{port}"


def short(oid: str) -> str:
    return oid[:7] if oid else "(none)"


def print_json(value: object) -> None:
    print(json.dumps(value, indent=2, sort_keys=False))


def describe_pack(pack: Dict[str, object]) -> str:
    if not pack:
        return "no pack"
    kinds = ", ".join(f"{count} {kind}" for kind, count in dict(pack["by_type"]).items())
    where = f"kept as {pack['pack_file']}" if pack.get("storage") == "pack" else "unpacked into loose objects"
    return f"{pack['objects']} objects ({kinds}), {pack['deltas']} deltas, {pack['bytes']} bytes; {where}"


def cmd_delta(args: argparse.Namespace, transport: Optional[Transport]) -> int:
    base, target = read_bytes(args.base), read_bytes(args.target)
    delta = make_delta(base, target)
    if apply_delta(base, delta) != target:
        raise PackError("the delta does not reproduce the target")
    _, _, ops = parse_delta(delta)
    if args.output:
        write_file(args.output, delta)
    copied = sum(op.size for op in ops if op.op == "copy")
    report = {"base": len(base), "target": len(target), "delta": len(delta), "copied": copied, "inserted": len(target) - copied,
              "ops": [{"op": op.op, "offset": op.offset, "size": op.size} for op in ops]}
    if args.json:
        print_json(report)
        return 0
    share = 100 * len(delta) // max(1, len(target))
    print(f"base {len(base)} bytes, target {len(target)} bytes, delta {len(delta)} bytes ({share}% of the target)")
    for op in ops:
        if op.op == "copy":
            print(f"  copy   {op.size:>7} bytes from base offset {op.offset}")
        else:
            print(f"  insert {op.size:>7} bytes {op.data[:24]!r}")
    return 0


def cmd_pack_objects(args: argparse.Namespace, transport: Optional[Transport]) -> int:
    repo = Repository(args.repo)
    tips = pack_tips(repo, args.revs, args.all)
    pack, idx, plans = pack_repository(repo, tips, not args.no_delta, not args.ref_delta, args.window, args.depth)
    checksum = pack[-20:].hex()
    stem = os.path.join(args.out, f"pack-{checksum}")
    write_file(stem + ".pack", pack, 0o444)
    write_file(stem + ".idx", idx, 0o444)
    by_type: Dict[str, int] = {}
    for plan in plans:
        by_type[plan.type] = by_type.get(plan.type, 0) + 1
    report = {"pack": stem + ".pack", "idx": stem + ".idx", "checksum": checksum, "objects": len(plans), "by_type": by_type,
              "deltas": sum(1 for p in plans if p.base), "bytes": len(pack), "delta_kind": "ref" if args.ref_delta else "ofs"}
    if args.json:
        print_json(report)
    else:
        print(f"{report['pack']}: {len(plans)} objects, {report['deltas']} {report['delta_kind']}-deltas, {len(pack)} bytes")
    return 0


def cmd_index_pack(args: argparse.Namespace, transport: Optional[Transport]) -> int:
    indexed = index_pack(read_bytes(args.pack))
    output = args.output or (args.pack[:-5] if args.pack.endswith(".pack") else args.pack) + ".idx"
    write_file(output, write_idx(indexed.records(), indexed.checksum), 0o444)
    if args.json:
        print_json(dict(pack_summary(indexed), idx=output))
    else:
        print(indexed.checksum)
    return 0


def cmd_verify_pack(args: argparse.Namespace, transport: Optional[Transport]) -> int:
    report = verify_pack(args.path)
    if args.json:
        print_json(report)
        return 0 if report["ok"] else 1
    if args.verbose:
        for row in report["objects"]:
            line = f"{row['oid']} {row['type']:<6} {row['size']} {row['packed']} {row['offset']}"
            print(line + (f" {row['depth']} {row['base']}" if row["depth"] else ""))
        print(f"non delta: {report['non_delta']} {'object' if report['non_delta'] == 1 else 'objects'}")
        for depth, count in sorted(dict(report["chains"]).items(), key=lambda kv: int(kv[0])):
            print(f"chain length = {depth}: {count} {'object' if count == 1 else 'objects'}")
    for problem in report["problems"]:
        print(f"minipack: {problem}", file=sys.stderr)
    print(f"{report['pack']}: {'ok' if report['ok'] else 'bad'}")
    return 0 if report["ok"] else 1


def cmd_unpack_objects(args: argparse.Namespace, transport: Optional[Transport]) -> int:
    repo = Repository(args.repo)
    summary = store_pack(repo, read_bytes(args.pack), keep=False)
    if args.json:
        print_json(summary)
    else:
        print(f"unpacked {describe_pack(summary)}")
    return 0


def make_trace(args: argparse.Namespace) -> Trace:
    return Trace(sink=trace_sink(getattr(args, "trace", "") or ""))


def cmd_ls_remote(args: argparse.Namespace, transport: Optional[Transport]) -> int:
    remote = Remote(args.url, transport, make_trace(args))
    remote.discover()
    refs = remote.ls_refs(prefixes=())
    if args.json:
        print_json({"capabilities": remote.caps, "refs": [dataclasses.asdict(r) for r in refs]})
        return 0
    for ref in refs:
        if ref.symref_target:
            print(f"ref: {ref.symref_target}\t{ref.name}")
        print(f"{ref.oid or 'unborn'}\t{ref.name}")
        if ref.peeled:
            print(f"{ref.peeled}\t{ref.name}^{{}}")
    return 0


def print_requests(requests: Sequence[Dict[str, object]]) -> None:
    for request in requests:
        arguments = list(request["arguments"])
        keywords = unique(str(a).split(" ")[0] for a in arguments)
        print(f"  {request['command']}: " + ", ".join(f"{k} x{sum(1 for a in arguments if str(a).split(' ')[0] == k)}" if k in ("want", "have", "ref-prefix", "shallow") else k for k in keywords))


def cmd_clone(args: argparse.Namespace, transport: Optional[Transport]) -> int:
    summary = clone(args.url, args.directory, args.depth, args.filter, args.keep_pack, transport, make_trace(args))
    for warning in summary["warnings"]:
        print(f"warning: {warning}", file=sys.stderr)
    if args.json:
        print_json(summary)
        return 0
    print(f"cloned {args.url} into {args.directory}")
    print_requests(summary["requests"])
    print(f"  pack: {describe_pack(summary['pack'])}")
    if summary["shallow"]:
        print(f"  shallow: {', '.join(short(o) for o in summary['shallow'])}")
    for name, oid in dict(summary["refs"]).items():
        print(f"  {short(oid)} {name}")
    print(f"  HEAD -> {summary['head']}")
    return 0


def cmd_fetch(args: argparse.Namespace, transport: Optional[Transport]) -> int:
    summary = fetch_into(args.directory, args.keep_pack, transport, make_trace(args))
    if args.json:
        print_json(summary)
        return 0
    if not summary["wants"]:
        print("already up to date")
    for number, round_ in enumerate(summary["rounds"], 1):
        print(f"  round {number}: {round_['haves']} new haves, {round_['common']} common, ready={round_['ready']}, done={round_['done']}")
    if summary["pack"]:
        print(f"  pack: {describe_pack(summary['pack'])}")
    for name, (old, new) in dict(summary["updated"]).items():
        print(f"  {short(old)}..{short(new)} {name}")
    return 0


def cmd_serve(args: argparse.Namespace, transport: Optional[Transport]) -> int:
    if not os.path.isdir(args.root):
        raise InputError(f"{args.root} is not a directory of repositories")
    server = http.server.ThreadingHTTPServer((args.host, args.port), git_http_handler(os.path.abspath(args.root), verbose=args.verbose))
    url = server_url(server)
    print(f"serving {args.root} at {url}/ (git http-backend)", flush=True)
    if args.url_file:
        write_file(args.url_file, (url + "\n").encode())
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


def cmd_parse_trace(args: argparse.Namespace, transport: Optional[Transport]) -> int:
    requests = parse_trace(read_bytes(args.file).decode("utf-8", "replace").splitlines(), args.who)
    if args.json:
        print_json(requests)
    else:
        print_requests(requests)
    return 0 if requests else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="minipack", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    delta = sub.add_parser("delta", help="encode TARGET as a delta against BASE and list the copy and insert instructions")
    delta.add_argument("base")
    delta.add_argument("target")
    delta.add_argument("-o", "--output", help="write the delta bytes to this file")
    pack = sub.add_parser("pack-objects", help="write pack-<sha>.pack and .idx with every object reachable from REVs")
    pack.add_argument("revs", nargs="*")
    pack.add_argument("--repo", default=".")
    pack.add_argument("--all", action="store_true", help="pack everything reachable from HEAD and all refs")
    pack.add_argument("--out", required=True, help="directory for the pack and its index")
    pack.add_argument("--no-delta", action="store_true", help="store every object whole")
    pack.add_argument("--ref-delta", action="store_true", help="name delta bases by object id (REF_DELTA) instead of offset")
    pack.add_argument("--window", type=int, default=10, help="how many earlier objects of the same type to try as delta bases")
    pack.add_argument("--depth", type=int, default=10, help="longest delta chain allowed")
    index = sub.add_parser("index-pack", help="compute the .idx v2 of a pack, as git index-pack does")
    index.add_argument("pack")
    index.add_argument("-o", "--output")
    verify = sub.add_parser("verify-pack", help="check a pack against its .idx; -v lists objects like git verify-pack -v")
    verify.add_argument("path")
    verify.add_argument("-v", "--verbose", action="store_true")
    unpack = sub.add_parser("unpack-objects", help="explode a pack into loose objects of a repository")
    unpack.add_argument("pack")
    unpack.add_argument("--repo", default=".")
    remote = sub.add_parser("ls-remote", help="list a remote's refs with protocol v2 ls-refs")
    remote.add_argument("url")
    clone_cmd = sub.add_parser("clone", help="clone over smart HTTP protocol v2 (no checkout)")
    clone_cmd.add_argument("url")
    clone_cmd.add_argument("directory")
    clone_cmd.add_argument("--depth", type=int, default=0, help="shallow clone of the default branch, N commits deep")
    clone_cmd.add_argument("--filter", default="", help="partial clone filter such as blob:none")
    clone_cmd.add_argument("--keep-pack", action="store_true", help="keep the received pack (+ .idx) instead of loose objects")
    fetch_cmd = sub.add_parser("fetch", help="fetch new objects from origin, negotiating with have lines")
    fetch_cmd.add_argument("directory", nargs="?", default=".")
    fetch_cmd.add_argument("--keep-pack", action="store_true")
    serve = sub.add_parser("serve", help="serve a directory of repositories over smart HTTP through git http-backend")
    serve.add_argument("root")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=0)
    serve.add_argument("--url-file", help="write the base URL here once the server listens")
    serve.add_argument("--verbose", action="store_true", help="log every request to stderr")
    parse = sub.add_parser("parse-trace", help="turn a GIT_TRACE_PACKET log (git's or minipack's) into its requests")
    parse.add_argument("file")
    parse.add_argument("--who", default="", help="the traced process name (clone, fetch, minipack); default: the first that sends a command")
    for command in (delta, pack, index, verify, unpack, remote, clone_cmd, fetch_cmd, parse):
        command.add_argument("--json", action="store_true", help="machine-readable output")
    for command in (remote, clone_cmd, fetch_cmd):
        command.add_argument("--trace", default="", help="append the pkt-line exchange to this file (GIT_TRACE_PACKET format)")
    return parser


COMMANDS = {
    "delta": cmd_delta,
    "pack-objects": cmd_pack_objects,
    "index-pack": cmd_index_pack,
    "verify-pack": cmd_verify_pack,
    "unpack-objects": cmd_unpack_objects,
    "ls-remote": cmd_ls_remote,
    "clone": cmd_clone,
    "fetch": cmd_fetch,
    "serve": cmd_serve,
    "parse-trace": cmd_parse_trace,
}


def main(argv: Optional[Sequence[str]] = None, transport: Optional[Transport] = None) -> int:
    args = build_parser().parse_args(list(sys.argv[1:] if argv is None else argv))
    try:
        return COMMANDS[args.command](args, transport)
    except MiniError as error:
        print(f"minipack: {error}", file=sys.stderr)
        return error.code


if __name__ == "__main__":
    raise SystemExit(main())
