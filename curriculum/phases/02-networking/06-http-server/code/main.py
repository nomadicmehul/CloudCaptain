"""minihttp: an HTTP/1.1 server and reverse proxy on raw sockets. It frames requests by Content-Length or chunked coding, refuses ambiguous framing, enforces header, body and keep-alive deadlines, serves static files with ETags and byte ranges, proxies path prefixes to upstreams, and probes any server with the same request matrix."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import socket
import sys
import threading
import time
import urllib.parse
from dataclasses import asdict, dataclass, field
from email.utils import formatdate, parsedate_to_datetime
from http import HTTPStatus
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

SERVER_NAME = "minihttp"
TOKEN = re.compile(rb"[!#$%&'*+\-.^_`|~0-9A-Za-z]+")
VERSION = re.compile(rb"HTTP/([0-9])\.([0-9])")
STATUS_LINE = re.compile(rb"HTTP/([0-9])\.([0-9]) ([0-9]{3})(?: (.*))?")
HEAD_END = re.compile(rb"\r?\n\r?\n")
HOST = re.compile(r"(?:[A-Za-z0-9._~-]+|\[[0-9A-Fa-f:.]+\])(?::[0-9]{1,5})?")
CHUNK_SIZE = re.compile(rb"[0-9A-Fa-f]{1,15}")
DIGITS = re.compile(r"[0-9]{1,18}")
ENTITY_TAG = re.compile(r'(?:W/)?"[^"]*"')
RANGE_SPEC = re.compile(r"([0-9]*)-([0-9]*)")
LOG_LINE = re.compile(r'(\S+) \S+ (\S+) \[([^\]]+)\] "((?:[^"\\]|\\.)*)" ([0-9]{3}) ([0-9]+|-) "((?:[^"\\]|\\.)*)" "((?:[^"\\]|\\.)*)"')
HOP_BY_HOP = frozenset({"connection", "keep-alive", "proxy-connection", "te", "trailer", "transfer-encoding", "upgrade"})
MEDIA_TYPES = {
    ".html": "text/html",
    ".htm": "text/html",
    ".txt": "text/plain",
    ".css": "text/css",
    ".js": "application/javascript",
    ".json": "application/json",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".gif": "image/gif",
    ".pdf": "application/pdf",
}
REASONS = {
    100: "Continue",
    200: "OK",
    206: "Partial Content",
    301: "Moved Permanently",
    304: "Not Modified",
    400: "Bad Request",
    403: "Forbidden",
    404: "Not Found",
    405: "Method Not Allowed",
    408: "Request Timeout",
    412: "Precondition Failed",
    413: "Content Too Large",
    414: "URI Too Long",
    416: "Range Not Satisfiable",
    431: "Request Header Fields Too Large",
    500: "Internal Server Error",
    501: "Not Implemented",
    502: "Bad Gateway",
    504: "Gateway Timeout",
    505: "HTTP Version Not Supported",
}
UNSATISFIABLE = (-1, -1)
UPSTREAM_CHUNKS = (b"alpha ", b"beta ", b"gamma\n")
CHUNK_LINE_LIMIT = 4096


class InputError(Exception):
    pass


class Timeout(Exception):
    pass


class Closed(Exception):
    pass


def phrase(status: int) -> str:
    if status in REASONS:
        return REASONS[status]
    try:
        return HTTPStatus(status).phrase
    except ValueError:
        return "Unknown"


class HTTPError(Exception):
    def __init__(self, status: int, reason: str = "") -> None:
        super().__init__(reason or phrase(status))
        self.status = status
        self.reason = reason or phrase(status)


@dataclass
class Config:
    root: str = "."
    host: str = "127.0.0.1"
    port: int = 8080
    proxies: List[Tuple[str, str]] = field(default_factory=list)
    header_timeout: float = 10.0
    body_timeout: float = 10.0
    send_timeout: float = 10.0
    keepalive_timeout: float = 15.0
    keepalive_requests: int = 100
    max_request_line: int = 8192
    max_header_bytes: int = 32768
    max_headers: int = 100
    max_body: int = 1048576
    upstream_connect_timeout: float = 5.0
    upstream_timeout: float = 30.0
    max_upstream_body: int = 67108864
    name: str = SERVER_NAME


def field_values(headers: Sequence[Tuple[str, str]], name: str) -> List[str]:
    return [value for key, value in headers if key.lower() == name]


def comma_tokens(values: Sequence[str]) -> List[str]:
    return [part.strip().lower() for value in values for part in value.split(",") if part.strip()]


@dataclass
class Head:
    method: str
    target: str
    version: Tuple[int, int]
    headers: List[Tuple[str, str]]
    path: str = "/"
    query: str = ""
    authority: str = ""
    host: str = ""
    framing: str = "none"
    length: int = 0
    keep_alive: bool = True
    expect_continue: bool = False

    def values(self, name: str) -> List[str]:
        return field_values(self.headers, name)

    def header(self, name: str) -> str:
        found = self.values(name)
        return found[0] if found else ""

    def tokens(self, name: str) -> List[str]:
        return comma_tokens(self.values(name))


@dataclass
class Request:
    head: Head
    body: bytes
    client: str
    local: Tuple[str, int]


@dataclass
class Response:
    status: int
    headers: List[Tuple[str, str]] = field(default_factory=list)
    body: bytes = b""
    file: Optional[Tuple[str, int, int]] = None
    chunks: Optional[List[bytes]] = None
    close: bool = False

    def header(self, name: str) -> str:
        found = field_values(self.headers, name)
        return found[0] if found else ""


@dataclass
class Reply:
    status: int
    reason: str
    version: Tuple[int, int]
    headers: List[Tuple[str, str]]
    body: bytes = b""
    interim: List[int] = field(default_factory=list)

    def header(self, name: str) -> str:
        found = field_values(self.headers, name)
        return ", ".join(found)


class Wire:
    def __init__(self, sock: Any) -> None:
        self.sock = sock
        self.buffer = bytearray()

    def fill(self, timeout: float) -> None:
        if timeout <= 0:
            raise Timeout()
        try:
            self.sock.settimeout(timeout)
            data = self.sock.recv(65536)
        except socket.timeout:
            raise Timeout() from None
        except OSError:
            raise Closed() from None
        if not data:
            raise Closed()
        self.buffer += data

    def read_head(self, timeout: Callable[[], float], max_line: int, max_head: int) -> bytes:
        while True:
            while self.buffer[:2] == b"\r\n" or self.buffer[:1] == b"\n":
                del self.buffer[: 2 if self.buffer[:1] == b"\r" else 1]
            line_end = self.buffer.find(b"\n")
            if line_end > max_line or (line_end == -1 and len(self.buffer) > max_line):
                raise HTTPError(414, f"request line longer than {max_line} bytes")
            end = HEAD_END.search(self.buffer)
            if end is not None:
                if end.end() > max_head:
                    raise HTTPError(431, f"header section larger than {max_head} bytes")
                head = bytes(self.buffer[: end.end()])
                del self.buffer[: end.end()]
                return head
            if len(self.buffer) > max_head:
                raise HTTPError(431, f"header section larger than {max_head} bytes")
            self.fill(timeout())

    def read_exact(self, size: int, idle: float) -> bytes:
        while len(self.buffer) < size:
            self.fill(idle)
        data = bytes(self.buffer[:size])
        del self.buffer[:size]
        return data

    def read_line(self, idle: float, limit: int) -> bytes:
        while True:
            end = self.buffer.find(b"\n")
            if end != -1:
                if end == 0 or self.buffer[end - 1] != 0x0D:
                    raise HTTPError(400, "chunked framing lines must end with CRLF, not a bare LF")
                line = bytes(self.buffer[: end - 1])
                del self.buffer[: end + 1]
                return line
            if len(self.buffer) > limit:
                raise HTTPError(400, f"chunked framing line longer than {limit} bytes")
            self.fill(idle)

    def read_to_close(self, idle: float, limit: int) -> bytes:
        while True:
            try:
                self.fill(idle)
            except Closed:
                break
            if len(self.buffer) > limit:
                raise HTTPError(502, f"body larger than {limit} bytes")
        data = bytes(self.buffer)
        self.buffer.clear()
        return data


class ByteSocket:
    def __init__(self, data: bytes, piece: int = 65536) -> None:
        self.data = data
        self.offset = 0
        self.piece = piece
        self.sent = bytearray()

    def settimeout(self, value: Optional[float]) -> None:
        return None

    def recv(self, size: int) -> bytes:
        chunk = self.data[self.offset : self.offset + min(size, self.piece)]
        self.offset += len(chunk)
        return chunk

    def sendall(self, data: bytes) -> None:
        self.sent += data


def split_lines(raw: bytes) -> List[bytes]:
    lines = []
    for line in raw.split(b"\n"):
        if line.endswith(b"\r"):
            line = line[:-1]
        if b"\r" in line:
            raise HTTPError(400, "bare CR inside the header section")
        lines.append(line)
    while lines and not lines[-1]:
        lines.pop()
    while lines and not lines[0]:
        lines.pop(0)
    return lines


def parse_request_line(line: bytes) -> Tuple[str, str, Tuple[int, int]]:
    parts = line.split(b" ")
    if len(parts) != 3 or not all(parts):
        raise HTTPError(400, "request line must be METHOD SP request-target SP HTTP-version with single spaces")
    method, target, version = parts
    if not TOKEN.fullmatch(method):
        raise HTTPError(400, f"invalid method {method[:20]!r}")
    match = VERSION.fullmatch(version)
    if not match:
        raise HTTPError(400, f"invalid HTTP version {version[:20]!r}")
    if match.group(1) != b"1":
        raise HTTPError(505, f"HTTP/{match.group(1).decode()} cannot arrive as an HTTP/1.x request line")
    if any(byte < 0x21 or byte == 0x7F for byte in target):
        raise HTTPError(400, "control character in the request target")
    return method.decode("ascii"), target.decode("latin-1"), (1, int(match.group(2)))


def parse_fields(lines: Sequence[bytes], max_headers: int) -> List[Tuple[str, str]]:
    fields: List[Tuple[str, str]] = []
    for line in lines:
        if line[:1] in (b" ", b"\t"):
            raise HTTPError(400, "obsolete line folding (a field line starting with whitespace) is not allowed")
        name, colon, value = line.partition(b":")
        if not colon:
            raise HTTPError(400, f"field line without a colon: {line[:40]!r}")
        if not TOKEN.fullmatch(name):
            raise HTTPError(400, f"invalid field name {name[:40]!r}: no whitespace is allowed before the colon")
        value = value.strip(b" \t")
        if any((byte < 0x20 and byte != 0x09) or byte == 0x7F for byte in value):
            raise HTTPError(400, f"control character in the value of {name.decode('ascii')}")
        fields.append((name.decode("ascii"), value.decode("latin-1")))
        if len(fields) > max_headers:
            raise HTTPError(431, f"more than {max_headers} header fields")
    return fields


def message_framing(head: Head, max_body: int) -> Tuple[str, int]:
    codings = head.tokens("transfer-encoding")
    lengths = head.values("content-length")
    if head.values("transfer-encoding"):
        if head.version < (1, 1):
            raise HTTPError(400, "Transfer-Encoding in an HTTP/1.0 request: the framing cannot be trusted")
        if lengths:
            raise HTTPError(400, "both Content-Length and Transfer-Encoding: refusing a request that two parsers could split differently")
        if codings.count("chunked") != 1 or codings[-1] != "chunked":
            raise HTTPError(400, f"Transfer-Encoding {', '.join(codings) or '(empty)'}: chunked must be the final coding, applied once")
        if codings != ["chunked"]:
            raise HTTPError(501, f"transfer coding {', '.join(codings[:-1])} is not implemented")
        return "chunked", 0
    if lengths:
        if len(lengths) > 1 or not DIGITS.fullmatch(lengths[0]):
            raise HTTPError(400, f"Content-Length must be one decimal number, got {', '.join(lengths)[:40]!r}")
        length = int(lengths[0])
        if length > max_body:
            raise HTTPError(413, f"Content-Length {length} exceeds the {max_body}-byte limit")
        return ("length", length) if length else ("none", 0)
    return "none", 0


def persistent(version: Tuple[int, int], connection: Sequence[str]) -> bool:
    if "close" in connection:
        return False
    if version >= (1, 1):
        return True
    return "keep-alive" in connection


def split_target(head: Head) -> None:
    target = head.target
    if target.startswith("/"):
        head.path, _, head.query = target.partition("?")
    elif target == "*":
        if head.method != "OPTIONS":
            raise HTTPError(400, "the asterisk-form target is only valid for OPTIONS")
        head.path = "*"
    elif target.lower().startswith(("http://", "https://")):
        parts = urllib.parse.urlsplit(target)
        head.path, head.query, head.authority = parts.path or "/", parts.query, parts.netloc
    else:
        raise HTTPError(400, f"request target {target[:40]!r} is not origin-form, absolute-form or *")


def check_host(head: Head) -> None:
    hosts = head.values("host")
    if len(hosts) > 1:
        raise HTTPError(400, "more than one Host header")
    if not hosts and head.version >= (1, 1):
        raise HTTPError(400, "HTTP/1.1 request without a Host header")
    if hosts and hosts[0] and not HOST.fullmatch(hosts[0]):
        raise HTTPError(400, f"invalid Host {hosts[0][:60]!r}")
    head.host = head.authority or (hosts[0] if hosts else "")


def parse_head(raw: bytes, config: Config) -> Head:
    lines = split_lines(raw)
    if not lines:
        raise HTTPError(400, "empty request")
    method, target, version = parse_request_line(lines[0])
    if method == "CONNECT":
        raise HTTPError(405, "CONNECT is not supported")
    head = Head(method, target, version, parse_fields(lines[1:], config.max_headers))
    split_target(head)
    check_host(head)
    head.framing, head.length = message_framing(head, config.max_body)
    head.keep_alive = persistent(head.version, head.tokens("connection"))
    head.expect_continue = head.header("expect").lower() == "100-continue" and head.version >= (1, 1) and head.framing != "none"
    return head


def read_chunked(wire: Wire, idle: float, max_body: int, max_trailer: int) -> bytes:
    body = bytearray()
    while True:
        line = wire.read_line(idle, CHUNK_LINE_LIMIT)
        size_text = line.split(b";", 1)[0].rstrip(b" \t")
        if not CHUNK_SIZE.fullmatch(size_text):
            raise HTTPError(400, f"invalid chunk size {size_text[:20]!r}")
        size = int(size_text, 16)
        if size == 0:
            break
        if len(body) + size > max_body:
            raise HTTPError(413, f"chunked body larger than {max_body} bytes")
        body += wire.read_exact(size, idle)
        if wire.read_exact(2, idle) != b"\r\n":
            raise HTTPError(400, "chunk data must be followed by CRLF")
    trailer = 0
    while True:
        line = wire.read_line(idle, max_trailer)
        if not line:
            return bytes(body)
        trailer += len(line)
        if trailer > max_trailer:
            raise HTTPError(431, "trailer section too large")


def read_body(wire: Wire, head: Head, config: Config) -> bytes:
    if head.framing == "none":
        return b""
    if head.expect_continue and not wire.buffer:
        wire.sock.sendall(b"HTTP/1.1 100 Continue\r\n\r\n")
    if head.framing == "length":
        return wire.read_exact(head.length, config.body_timeout)
    return read_chunked(wire, config.body_timeout, config.max_body, config.max_header_bytes)


def parse_status_head(raw: bytes) -> Reply:
    lines = split_lines(raw)
    match = STATUS_LINE.fullmatch(lines[0]) if lines else None
    if not match:
        raise HTTPError(502, f"invalid status line {(lines or [b''])[0][:60]!r}")
    headers = parse_fields(lines[1:], 1000)
    return Reply(int(match.group(3)), (match.group(4) or b"").decode("latin-1"), (int(match.group(1)), int(match.group(2))), headers)


def response_framing(method: str, reply: Reply) -> Tuple[str, int]:
    if method == "HEAD" or reply.status in (204, 304) or reply.status < 200:
        return "none", 0
    codings = comma_tokens(field_values(reply.headers, "transfer-encoding"))
    if codings:
        return ("chunked", 0) if codings[-1] == "chunked" else ("close", 0)
    lengths = [value.strip() for value in field_values(reply.headers, "content-length")]
    if lengths:
        if len(set(lengths)) != 1 or not DIGITS.fullmatch(lengths[0]):
            raise HTTPError(502, f"invalid Content-Length {', '.join(lengths)[:40]!r}")
        return "length", int(lengths[0])
    return "close", 0


def read_response(wire: Wire, method: str, idle: float, max_body: int = 67108864) -> Reply:
    interim: List[int] = []
    while True:
        reply = parse_status_head(wire.read_head(lambda: idle, 65536, 262144))
        if 100 <= reply.status < 200 and reply.status != 101:
            interim.append(reply.status)
            continue
        break
    kind, length = response_framing(method, reply)
    if kind == "length":
        if length > max_body:
            raise HTTPError(502, f"Content-Length {length} exceeds {max_body} bytes")
        reply.body = wire.read_exact(length, idle)
    elif kind == "chunked":
        reply.body = read_chunked(wire, idle, max_body, 65536)
    elif kind == "close":
        reply.body = wire.read_to_close(idle, max_body)
    reply.interim = interim
    return reply


def error_response(status: int, reason: str, close: bool = True, headers: Sequence[Tuple[str, str]] = ()) -> Response:
    body = f"{status} {phrase(status)}: {reason}\n".encode("utf-8")
    return Response(status, [("Content-Type", "text/plain; charset=utf-8")] + list(headers), body, close=close)


def send_response(sock: Any, response: Response, method: str, version: Tuple[int, int], keep_alive: bool, config: Config) -> int:
    bodyless = method == "HEAD" or response.status in (204, 304) or response.status < 200
    chunked = response.chunks is not None and version >= (1, 1)
    payload = b"".join(response.chunks) if response.chunks is not None else response.body
    size = response.file[2] if response.file else len(payload)
    explicit = any(key.lower() == "content-length" for key, _ in response.headers)
    lines = [f"HTTP/1.1 {response.status} {phrase(response.status)}", f"Server: {config.name}", f"Date: {formatdate(usegmt=True)}"]
    lines += [f"{key}: {value}" for key, value in response.headers]
    if chunked:
        lines.append("Transfer-Encoding: chunked")
    elif not explicit and response.status not in (204, 304) and response.status >= 200:
        lines.append(f"Content-Length: {size}")
    if not keep_alive:
        lines.append("Connection: close")
    elif version < (1, 1):
        lines.append("Connection: keep-alive")
    sock.settimeout(config.send_timeout)
    sock.sendall(("\r\n".join(lines) + "\r\n\r\n").encode("latin-1"))
    if bodyless:
        return 0
    if response.file:
        path, offset, count = response.file
        if not count:
            return 0
        with open(path, "rb") as handle:
            return sock.sendfile(handle, offset, count)
    if chunked:
        for piece in response.chunks or []:
            if piece:
                sock.sendall(b"%x\r\n%s\r\n" % (len(piece), piece))
        sock.sendall(b"0\r\n\r\n")
        return size
    sock.sendall(payload)
    return size


LINGER_SECONDS = 1.0


def linger_close(sock: Any, seconds: float = LINGER_SECONDS) -> None:
    try:
        sock.shutdown(socket.SHUT_WR)
        deadline = time.monotonic() + seconds
        drained = 0
        while drained < 262144:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            sock.settimeout(remaining)
            data = sock.recv(65536)
            if not data:
                break
            drained += len(data)
    except OSError:
        pass
    finally:
        sock.close()


def normalize_path(raw: str) -> str:
    try:
        decoded = urllib.parse.unquote(raw, errors="strict")
    except UnicodeDecodeError:
        raise HTTPError(400, "the path is not valid UTF-8 after percent-decoding") from None
    if "\x00" in decoded:
        raise HTTPError(400, "NUL byte in the path")
    segments: List[str] = []
    for segment in decoded.split("/"):
        if segment in ("", "."):
            continue
        if segment == "..":
            if not segments:
                raise HTTPError(400, f"{raw[:60]} climbs above the document root")
            segments.pop()
            continue
        segments.append(segment)
    path = "/" + "/".join(segments)
    if segments and decoded.endswith(("/", "/.", "/..")):
        path += "/"
    return path


def make_etag(mtime: float, size: int) -> str:
    return '"%x-%x"' % (int(mtime), size)


def http_date(timestamp: float) -> str:
    return formatdate(timestamp, usegmt=True)


def parse_http_date(value: str) -> Optional[int]:
    if not value:
        return None
    try:
        parsed = parsedate_to_datetime(value)
    except (TypeError, ValueError, IndexError):
        return None
    if parsed is None or parsed.tzinfo is None:
        return None
    return int(parsed.timestamp())


def etag_matches(field_value: str, etag: str, weak: bool) -> bool:
    if field_value.strip() == "*":
        return True
    for candidate in ENTITY_TAG.findall(field_value):
        if weak and candidate.removeprefix("W/") == etag.removeprefix("W/"):
            return True
        if not weak and candidate == etag and not etag.startswith("W/"):
            return True
    return False


def evaluate_preconditions(head: Head, etag: str, mtime: int) -> Optional[int]:
    safe = head.method in ("GET", "HEAD")
    if_match = head.header("if-match")
    if if_match:
        if not etag_matches(if_match, etag, weak=False):
            return 412
    else:
        since = parse_http_date(head.header("if-unmodified-since"))
        if since is not None and mtime > since:
            return 412
    if_none_match = head.header("if-none-match")
    if if_none_match:
        if etag_matches(if_none_match, etag, weak=True):
            return 304 if safe else 412
    elif safe:
        since = parse_http_date(head.header("if-modified-since"))
        if since is not None and mtime <= since:
            return 304
    return None


def parse_range(value: str, size: int) -> Optional[List[Tuple[int, int]]]:
    unit, equals, specs = value.partition("=")
    if not equals or unit.strip().lower() != "bytes":
        return None
    spans: List[Tuple[int, int]] = []
    for spec in specs.split(","):
        match = RANGE_SPEC.fullmatch(spec.strip())
        if not match or match.group(1) == match.group(2) == "":
            return None
        first, last = match.group(1), match.group(2)
        if first:
            start = int(first)
            end = int(last) if last else size - 1
            if last and end < start:
                return None
            if start < size:
                spans.append((start, min(end, size - 1)))
        elif int(last) > 0 and size > 0:
            spans.append((max(0, size - int(last)), size - 1))
    return spans


def choose_range(head: Head, size: int, etag: str, last_modified: str) -> Optional[Tuple[int, int]]:
    value = head.header("range")
    if not value or head.method != "GET":
        return None
    if_range = head.header("if-range")
    if if_range:
        validator = etag if if_range.startswith(('"', "W/")) else last_modified
        if if_range.startswith("W/") or if_range != validator:
            return None
    spans = parse_range(value, size)
    if spans is None or len(spans) > 1:
        return None
    return spans[0] if spans else UNSATISFIABLE


def media_type(path: str) -> str:
    return MEDIA_TYPES.get(os.path.splitext(path)[1].lower(), "application/octet-stream")


def file_response(head: Head, target: str) -> Response:
    info = os.stat(target)
    size = info.st_size
    etag, last_modified = make_etag(info.st_mtime, size), http_date(info.st_mtime)
    validators = [("Last-Modified", last_modified), ("ETag", etag)]
    verdict = evaluate_preconditions(head, etag, int(info.st_mtime))
    if verdict == 304:
        return Response(304, validators)
    if verdict == 412:
        return error_response(412, "a precondition in If-Match or If-Unmodified-Since failed", close=False)
    headers = [("Content-Type", media_type(target))] + validators + [("Accept-Ranges", "bytes")]
    span = choose_range(head, size, etag, last_modified)
    if span == UNSATISFIABLE:
        return Response(416, [("Content-Range", f"bytes */{size}"), ("Content-Type", "text/plain")], b"416 Range Not Satisfiable\n")
    if span:
        start, end = span
        return Response(206, headers + [("Content-Range", f"bytes {start}-{end}/{size}")], file=(target, start, end - start + 1))
    return Response(200, headers, file=(target, 0, size))


def static(request: Request, path: str, config: Config) -> Response:
    head = request.head
    if head.method not in ("GET", "HEAD"):
        return error_response(405, f"{head.method} is not allowed on a static file", close=False, headers=[("Allow", "GET, HEAD")])
    target = os.path.join(config.root, *[segment for segment in path.split("/") if segment])
    if os.path.isdir(target):
        if not path.endswith("/"):
            authority = head.host or "%s:%d" % request.local
            location = f"http://{authority}{urllib.parse.quote(path)}/" + (f"?{head.query}" if head.query else "")
            return Response(301, [("Location", location), ("Content-Type", "text/plain")], f"moved to {location}\n".encode("utf-8"))
        target = os.path.join(target, "index.html")
        if not os.path.isfile(target):
            return error_response(403, f"{path} is a directory without index.html", close=False)
    if not os.path.isfile(target):
        return error_response(404, f"{path} not found under the document root", close=False)
    return file_response(head, target)


@dataclass
class Upstream:
    host: str
    port: int

    @property
    def label(self) -> str:
        return f"{self.host}:{self.port}"


def parse_upstream(url: str) -> Upstream:
    parts = urllib.parse.urlsplit(url)
    if parts.scheme != "http" or not parts.hostname:
        raise InputError(f"upstream {url!r}: expected http://HOST:PORT")
    if parts.path not in ("", "/") or parts.query:
        raise InputError(f"upstream {url!r}: minihttp forwards the request URI unchanged, like proxy_pass without a URI; drop the path")
    try:
        port = parts.port or 80
    except ValueError:
        raise InputError(f"upstream {url!r}: invalid port") from None
    return Upstream(parts.hostname, port)


def parse_route(value: str) -> Tuple[str, str]:
    prefix, equals, url = value.partition("=")
    if not equals or not prefix.startswith("/") or not prefix.endswith("/"):
        raise InputError(f"--proxy {value!r}: expected /PREFIX/=http://HOST:PORT")
    parse_upstream(url)
    return prefix, url


def forward_headers(head: Head, client: str, body: bytes, upstream: Upstream, name: str) -> List[Tuple[str, str]]:
    dropped = HOP_BY_HOP | set(head.tokens("connection")) | {"content-length", "expect", "x-forwarded-for", "x-forwarded-proto", "via"}
    headers = [(key, value) for key, value in head.headers if key.lower() not in dropped]
    if not head.values("host"):
        headers.insert(0, ("Host", head.host or upstream.label))
    prior = ", ".join(head.values("x-forwarded-for"))
    headers.append(("X-Forwarded-For", f"{prior}, {client}" if prior else client))
    headers.append(("X-Forwarded-Proto", "http"))
    via = ", ".join(head.values("via"))
    headers.append(("Via", f"{via}, 1.1 {name}" if via else f"1.1 {name}"))
    if head.framing != "none":
        headers.append(("Content-Length", str(len(body))))
    headers.append(("Connection", "close"))
    return headers


def backward_headers(headers: Sequence[Tuple[str, str]], name: str, keep_length: bool) -> List[Tuple[str, str]]:
    dropped = HOP_BY_HOP | set(comma_tokens(field_values(headers, "connection"))) | {"date", "server", "via"}
    if not keep_length:
        dropped = dropped | {"content-length"}
    kept = [(key, value) for key, value in headers if key.lower() not in dropped]
    via = ", ".join(field_values(headers, "via"))
    kept.append(("Via", f"{via}, 1.1 {name}" if via else f"1.1 {name}"))
    return kept


def gateway_error(status: int, reason: str) -> Response:
    return error_response(status, reason, close=False)


def proxy(request: Request, upstream: Upstream, config: Config) -> Response:
    head = request.head
    fields = forward_headers(head, request.client, request.body, upstream, config.name)
    lines = [f"{head.method} {head.target} HTTP/1.1"] + [f"{key}: {value}" for key, value in fields]
    data = ("\r\n".join(lines) + "\r\n\r\n").encode("latin-1") + request.body
    try:
        sock = socket.create_connection((upstream.host, upstream.port), timeout=config.upstream_connect_timeout)
    except socket.timeout:
        return gateway_error(504, f"connecting to {upstream.label} took longer than {config.upstream_connect_timeout:g}s")
    except OSError as exc:
        return gateway_error(502, f"connecting to {upstream.label} failed: {exc.strerror or exc}")
    try:
        sock.settimeout(config.upstream_timeout)
        sock.sendall(data)
        reply = read_response(Wire(sock), head.method, config.upstream_timeout, config.max_upstream_body)
    except Timeout:
        return gateway_error(504, f"{upstream.label} sent nothing for {config.upstream_timeout:g}s")
    except Closed:
        return gateway_error(502, f"{upstream.label} closed the connection before a complete response")
    except HTTPError as err:
        return gateway_error(502, f"{upstream.label} sent an invalid response: {err.reason}")
    except socket.timeout:
        return gateway_error(504, f"sending the request to {upstream.label} took longer than {config.upstream_timeout:g}s")
    except OSError as exc:
        return gateway_error(502, f"{upstream.label}: {exc}")
    finally:
        sock.close()
    return Response(reply.status, backward_headers(reply.headers, config.name, head.method == "HEAD"), reply.body)


class Site:
    def __init__(self, config: Config) -> None:
        self.config = config
        self.routes = sorted(((prefix, parse_upstream(url)) for prefix, url in config.proxies), key=lambda route: -len(route[0]))

    def __call__(self, request: Request) -> Response:
        head = request.head
        if head.path == "*":
            return Response(200, [("Allow", "GET, HEAD, OPTIONS")])
        try:
            path = normalize_path(head.path)
        except HTTPError as err:
            return error_response(err.status, err.reason)
        for prefix, upstream in self.routes:
            if path.startswith(prefix):
                return proxy(request, upstream, self.config)
        return static(request, path, self.config)


def log_escape(text: str) -> str:
    return "".join(f"\\x{ord(c):02X}" if ord(c) < 0x20 or ord(c) >= 0x7F or c in '"\\' else c for c in text)


def log_line(client: str, request_line: str, status: int, sent: int, referer: str, agent: str, when: Optional[float] = None) -> str:
    stamp = time.strftime("%d/%b/%Y:%H:%M:%S %z", time.localtime(time.time() if when is None else when))
    return f'{client} - - [{stamp}] "{log_escape(request_line) or "-"}" {status} {sent} "{log_escape(referer) or "-"}" "{log_escape(agent) or "-"}"'


def parse_log_line(line: str) -> Optional[Dict[str, Any]]:
    match = LOG_LINE.match(line.strip())
    if not match:
        return None
    sent = match.group(6)
    return {
        "client": match.group(1),
        "user": match.group(2),
        "time": match.group(3),
        "request": match.group(4),
        "status": int(match.group(5)),
        "bytes": 0 if sent == "-" else int(sent),
        "referer": match.group(7),
        "agent": match.group(8),
    }


def log_stats(lines: Sequence[str]) -> Dict[str, Any]:
    entries = [parse_log_line(line) for line in lines if line.strip()]
    parsed = [entry for entry in entries if entry]
    status: Dict[str, int] = {}
    for entry in parsed:
        status[str(entry["status"])] = status.get(str(entry["status"]), 0) + 1
    return {"lines": len(entries), "parsed": len(parsed), "skipped": len(entries) - len(parsed), "status": dict(sorted(status.items())), "requests": [[entry["status"], entry["request"]] for entry in parsed]}


class AccessLog:
    def __init__(self, target: Optional[str]) -> None:
        self.lock = threading.Lock()
        self.handle = None
        if target == "-":
            self.handle = sys.stdout
        elif target:
            self.handle = open(target, "a", encoding="utf-8")

    def __call__(self, line: str) -> None:
        if self.handle is None:
            return
        with self.lock:
            self.handle.write(line + "\n")
            self.handle.flush()


def first_line(data: Any) -> str:
    raw = bytes(data)
    if b"\n" not in raw:
        return ""
    return raw.split(b"\n", 1)[0].rstrip(b"\r").decode("latin-1")


class Server:
    def __init__(self, config: Config, app: Optional[Callable[[Request], Response]] = None, log: Optional[Callable[[str], None]] = None, clock: Callable[[], float] = time.monotonic) -> None:
        self.config = config
        self.app = app or Site(config)
        self.log = log or (lambda line: None)
        self.clock = clock
        self.listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.listener.bind((config.host, config.port))
        self.listener.listen(128)
        self.listener.settimeout(0.2)
        self.address: Tuple[str, int] = self.listener.getsockname()[:2]
        self.running = True
        self.lock = threading.Lock()
        self.active: set = set()
        self.thread: Optional[threading.Thread] = None

    def start(self) -> "Server":
        self.thread = threading.Thread(target=self.serve_forever, daemon=True)
        self.thread.start()
        return self

    def serve_forever(self) -> None:
        while self.running:
            try:
                conn, address = self.listener.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            threading.Thread(target=self.handle, args=(conn, address), daemon=True).start()

    def stop(self) -> None:
        self.running = False
        if self.thread:
            self.thread.join(2)
        self.listener.close()
        with self.lock:
            for conn in list(self.active):
                try:
                    conn.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass

    def handle(self, conn: socket.socket, address: Tuple[str, int]) -> None:
        with self.lock:
            self.active.add(conn)
        wire = Wire(conn)
        started = self.clock()
        served = 0
        try:
            while self.running:
                if served:
                    if not wire.buffer:
                        try:
                            wire.fill(self.config.keepalive_timeout)
                        except (Timeout, Closed):
                            break
                    started = self.clock()
                if not self.exchange(wire, address[0], served, started):
                    break
                served += 1
        finally:
            with self.lock:
                self.active.discard(conn)
            linger_close(conn)

    def exchange(self, wire: Wire, client: str, served: int, started: float) -> bool:
        config = self.config
        deadline = started + config.header_timeout
        try:
            raw = wire.read_head(lambda: deadline - self.clock(), config.max_request_line, config.max_header_bytes)
        except Timeout:
            if bytes(wire.buffer).strip():
                self.refuse(wire, client, first_line(wire.buffer), HTTPError(408, f"header section not complete within {config.header_timeout:g}s"))
            return False
        except Closed:
            return False
        except HTTPError as err:
            self.refuse(wire, client, first_line(wire.buffer), err)
            return False
        line = first_line(raw)
        try:
            head = parse_head(raw, config)
            body = read_body(wire, head, config)
        except HTTPError as err:
            self.refuse(wire, client, line, err)
            return False
        except Timeout:
            self.refuse(wire, client, line, HTTPError(408, f"no body bytes for {config.body_timeout:g}s"))
            return False
        except Closed:
            return False
        try:
            response = self.app(Request(head, body, client, wire.sock.getsockname()[:2]))
        except Exception as exc:
            response = error_response(500, f"{type(exc).__name__}: {exc}")
        keep = head.keep_alive and not response.close and served + 1 < config.keepalive_requests and self.running
        try:
            sent = send_response(wire.sock, response, head.method, head.version, keep, config)
        except OSError:
            return False
        self.log(log_line(client, line, response.status, sent, head.header("referer"), head.header("user-agent")))
        return keep

    def refuse(self, wire: Wire, client: str, line: str, err: HTTPError) -> None:
        try:
            sent = send_response(wire.sock, error_response(err.status, err.reason), "GET", (1, 1), False, self.config)
        except OSError:
            sent = 0
        self.log(log_line(client, line, err.status, sent, "", ""))


def echo_app(request: Request) -> Response:
    head = request.head
    if head.path.endswith("/slow"):
        try:
            delay = float(urllib.parse.parse_qs(head.query).get("ms", ["1000"])[0]) / 1000
        except ValueError:
            return error_response(400, "ms must be a number", close=False)
        time.sleep(min(60.0, max(0.0, delay)))
    if head.path.endswith("/chunked"):
        return Response(200, [("Content-Type", "text/plain")], chunks=list(UPSTREAM_CHUNKS))
    status = re.search(r"/status/([1-5][0-9][0-9])$", head.path)
    if status:
        return Response(int(status.group(1)), [("Content-Type", "text/plain")], b"status\n")
    seen = {
        "method": head.method,
        "target": head.target,
        "version": "HTTP/%d.%d" % head.version,
        "headers": [[key, value] for key, value in head.headers],
        "framing": head.framing,
        "body_length": len(request.body),
        "body_sha256": hashlib.sha256(request.body).hexdigest(),
        "client": request.client,
    }
    return Response(200, [("Content-Type", "application/json")], (json.dumps(seen, sort_keys=True) + "\n").encode("utf-8"))


def nginx_seconds(value: float) -> str:
    return f"{int(value)}s" if float(value).is_integer() else f"{int(round(value * 1000))}ms"


def nginx_directives(config: Config) -> List[Tuple[str, str, str]]:
    buffers = max(1, -(-config.max_header_bytes // config.max_request_line))
    return [
        ("header_timeout", "client_header_timeout", nginx_seconds(config.header_timeout)),
        ("body_timeout", "client_body_timeout", nginx_seconds(config.body_timeout)),
        ("send_timeout", "send_timeout", nginx_seconds(config.send_timeout)),
        ("keepalive_timeout", "keepalive_timeout", nginx_seconds(config.keepalive_timeout)),
        ("keepalive_requests", "keepalive_requests", str(config.keepalive_requests)),
        ("max_request_line, max_header_bytes", "large_client_header_buffers", f"{buffers} {config.max_request_line}"),
        ("max_headers", "max_headers", str(config.max_headers)),
        ("max_body", "client_max_body_size", str(config.max_body)),
        ("linger_close()", "lingering_timeout", nginx_seconds(LINGER_SECONDS)),
    ]


def nginx_proxy_directives(config: Config, upstream: Upstream) -> List[Tuple[str, str]]:
    return [
        ("proxy_pass", f"http://{upstream.label}"),
        ("proxy_http_version", "1.1"),
        ("proxy_set_header", "Host $http_host"),
        ("proxy_set_header", "X-Forwarded-For $proxy_add_x_forwarded_for"),
        ("proxy_set_header", "X-Forwarded-Proto $scheme"),
        ("proxy_set_header", "Via $via_chain"),
        ("proxy_connect_timeout", nginx_seconds(config.upstream_connect_timeout)),
        ("proxy_read_timeout", nginx_seconds(config.upstream_timeout)),
        ("proxy_send_timeout", nginx_seconds(config.upstream_timeout)),
    ]


def nginx_conf(config: Config, root: str, listen: str, name: str = "nginx") -> str:
    out = [
        "worker_processes 1;",
        "pid /tmp/nginx.pid;",
        "error_log /var/log/nginx/error.log info;",
        "",
        "events {",
        "    worker_connections 1024;",
        "}",
        "",
        "http {",
        "    include /etc/nginx/mime.types;",
        "    default_type application/octet-stream;",
        "    access_log /var/log/nginx/access.log combined;",
        "    sendfile on;",
    ]
    out += [f"    {directive} {value};" for _, directive, value in nginx_directives(config)]
    out += [
        "",
        "    map $http_via $via_chain {",
        f'        ""      "1.1 {name}";',
        f'        default "$http_via, 1.1 {name}";',
        "    }",
        "",
        "    server {",
        f"        listen {listen};",
        "        server_name _;",
        f"        root {root};",
        "        index index.html;",
    ]
    for prefix, url in config.proxies:
        out += ["", f"        location {prefix} {{"]
        out += [f"            {directive} {value};" for directive, value in nginx_proxy_directives(config, parse_upstream(url))]
        out += ["        }"]
    out += ["    }", "}"]
    return "\n".join(out) + "\n"


def location_summary(config: Config, upstream: Upstream) -> Dict[str, str]:
    return {directive: value for directive, value in nginx_proxy_directives(config, upstream) if directive != "proxy_set_header"}


def config_report(config: Config) -> Dict[str, Any]:
    report = asdict(config)
    report["proxies"] = [{"prefix": prefix, "upstream": url} for prefix, url in config.proxies]
    return {
        "minihttp": report,
        "nginx_equivalent": {directive: value for _, directive, value in nginx_directives(config)},
        "nginx_locations": {prefix: location_summary(config, parse_upstream(url)) for prefix, url in config.proxies},
    }


def render_config(config: Config) -> str:
    rows = [("setting", "value", "nginx")]
    for setting, directive, value in nginx_directives(config):
        own = ", ".join(str(getattr(config, part.strip())) for part in setting.split(","))
        rows.append((setting, own, f"{directive} {value};"))
    for prefix, url in config.proxies:
        upstream = parse_upstream(url)
        rows.append((f"proxy {prefix}", url, f"location {prefix} {{ proxy_pass http://{upstream.label}; proxy_read_timeout {nginx_seconds(config.upstream_timeout)}; }}"))
    width = [max(len(row[i]) for row in rows) for i in range(2)]
    return "\n".join(f"{a:<{width[0]}}  {b:<{width[1]}}  {c}" for a, b, c in rows)


def parse_stream(data: bytes, config: Config) -> List[Dict[str, Any]]:
    wire = Wire(ByteSocket(data))
    found: List[Dict[str, Any]] = []
    while len(found) < 20:
        try:
            raw = wire.read_head(lambda: 1.0, config.max_request_line, config.max_header_bytes)
        except Closed:
            if bytes(wire.buffer).strip():
                found.append({"status": 400, "reason": "the stream ends inside a header section", "close": True})
            break
        except HTTPError as err:
            found.append({"status": err.status, "reason": err.reason, "close": True})
            break
        line = first_line(raw)
        try:
            head = parse_head(raw, config)
            body = read_body(wire, head, config)
        except HTTPError as err:
            found.append({"request_line": line, "status": err.status, "reason": err.reason, "close": True})
            break
        except Closed:
            found.append({"request_line": line, "status": None, "reason": "the stream ends inside the body", "close": True})
            break
        found.append({
            "request_line": line,
            "method": head.method,
            "target": head.target,
            "host": head.host,
            "framing": head.framing,
            "body_length": len(body),
            "keep_alive": head.keep_alive,
            "expect_continue": head.expect_continue,
            "headers": [[key, value] for key, value in head.headers],
        })
        if not head.keep_alive:
            if bytes(wire.buffer).strip():
                found.append({"status": None, "reason": f"{len(wire.buffer)} bytes after a request that closes the connection are never read", "close": True})
            break
    return found


def render_stream(found: Sequence[Dict[str, Any]]) -> str:
    lines = []
    for index, entry in enumerate(found, 1):
        if "framing" in entry:
            persistence = "keep-alive" if entry["keep_alive"] else "close"
            lines.append(f"{index}  {entry['request_line']}  host={entry['host'] or '-'} framing={entry['framing']} body={entry['body_length']} {persistence}")
        else:
            status = f"{entry['status']} {phrase(entry['status'])}" if entry["status"] else "stop"
            prefix = f"{entry['request_line']}  " if entry.get("request_line") else ""
            lines.append(f"{index}  {prefix}{status}: {entry['reason']}; the connection closes here")
    return "\n".join(lines)


@dataclass
class ProbeContext:
    address: Tuple[str, int]
    config: Config
    file: str = "/hello.txt"
    directory: str = "/docs"
    api: str = "/api/"
    dead: str = "/dead/"
    timeout: float = 10.0
    size: int = 0
    content: bytes = b""
    etag: str = ""
    last_modified: str = ""


class Client:
    def __init__(self, ctx: ProbeContext) -> None:
        self.sock = socket.create_connection(ctx.address, timeout=ctx.timeout)
        self.wire = Wire(self.sock)
        self.timeout = ctx.timeout

    def __enter__(self) -> "Client":
        return self

    def __exit__(self, *exc: Any) -> None:
        try:
            self.sock.close()
        except OSError:
            pass

    def send(self, data: bytes) -> None:
        self.sock.settimeout(self.timeout)
        self.sock.sendall(data)

    def reply(self, method: str = "GET") -> Reply:
        return read_response(self.wire, method, self.timeout)

    def ask(self, data: bytes, method: str = "GET") -> Reply:
        self.send(data)
        return self.reply(method)

    def closed_within(self, limit: float) -> Optional[float]:
        started = time.monotonic()
        while True:
            try:
                self.wire.fill(limit - (time.monotonic() - started))
            except Closed:
                return round(time.monotonic() - started, 3)
            except Timeout:
                return None


def raw_request(method: str, target: str, headers: Sequence[Tuple[str, str]] = (), body: bytes = b"", version: str = "HTTP/1.1", host: Optional[str] = "probe.test") -> bytes:
    lines = [f"{method} {target} {version}"]
    if host is not None:
        lines.append(f"Host: {host}")
    lines += [f"{key}: {value}" for key, value in headers]
    return ("\r\n".join(lines) + "\r\n\r\n").encode("latin-1") + body


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def observe(reply: Reply) -> Dict[str, Any]:
    seen = {
        "status": [reply.status],
        "content_type": reply.header("content-type").split(";")[0].strip(),
        "content_length": reply.header("content-length"),
        "content_range": reply.header("content-range"),
        "etag": reply.header("etag"),
        "last_modified": reply.header("last-modified"),
        "location_path": urllib.parse.urlsplit(reply.header("location")).path,
        "connection": reply.header("connection").lower(),
        "body_length": len(reply.body),
        "body_sha256": sha256(reply.body),
    }
    if len(reply.body) <= 256:
        seen["body"] = reply.body.decode("latin-1")
    return seen


def upstream_view(reply: Reply) -> Dict[str, Any]:
    if reply.status != 200 or "json" not in reply.header("content-type"):
        return {}
    seen = json.loads(reply.body.decode("utf-8"))
    merged: Dict[str, str] = {}
    for key, value in seen.get("headers", []):
        merged[key.lower()] = f"{merged[key.lower()]}, {value}" if key.lower() in merged else value
    seen["fields"] = merged
    return seen


def one(ctx: ProbeContext, method: str, target: str, headers: Sequence[Tuple[str, str]] = (), body: bytes = b"", version: str = "HTTP/1.1", host: Optional[str] = "probe.test") -> Reply:
    with Client(ctx) as client:
        return client.ask(raw_request(method, target, headers, body, version, host), method)


def prime(ctx: ProbeContext) -> None:
    reply = one(ctx, "GET", ctx.file)
    if reply.status != 200:
        raise InputError(f"probe: GET {ctx.file} answered {reply.status}; point --file at a file the server serves")
    ctx.content, ctx.size = reply.body, len(reply.body)
    ctx.etag, ctx.last_modified = reply.header("etag"), reply.header("last-modified")


def probe_keepalive(ctx: ProbeContext) -> Dict[str, Any]:
    with Client(ctx) as client:
        replies = [client.ask(raw_request("GET", ctx.file)) for _ in range(3)]
        closed = client.closed_within(2.0)
    return {"status": [r.status for r in replies], "connection": [r.header("connection").lower() for r in replies], "closed_after_last": closed is not None}


def probe_idle(ctx: ProbeContext) -> Dict[str, Any]:
    with Client(ctx) as client:
        reply = client.ask(raw_request("GET", ctx.file))
        closed = client.closed_within(ctx.config.keepalive_timeout + 3)
    return {"status": [reply.status], "idle_closed_after": closed}


def probe_http10(ctx: ProbeContext) -> Dict[str, Any]:
    with Client(ctx) as client:
        first = client.ask(raw_request("GET", ctx.file, [("Connection", "keep-alive")], version="HTTP/1.0", host=None))
        still_open = client.closed_within(0.3) is None
        second = client.ask(raw_request("GET", ctx.file, version="HTTP/1.0", host=None))
        closed = client.closed_within(2.0)
    return {"status": [first.status, second.status], "open_after_keep_alive": still_open, "closed_after_plain": closed is not None}


def probe_head(ctx: ProbeContext) -> Dict[str, Any]:
    with Client(ctx) as client:
        head = client.ask(raw_request("HEAD", ctx.file), "HEAD")
        get = client.ask(raw_request("GET", ctx.file))
    return {"status": [head.status, get.status], "head_content_length": head.header("content-length"), "head_body_length": len(head.body), "get_body_length": len(get.body)}


def probe_simple(method: str, target: Callable[[ProbeContext], str], headers: Callable[[ProbeContext], Sequence[Tuple[str, str]]] = lambda ctx: (), body: bytes = b"", version: str = "HTTP/1.1", host: Optional[str] = "probe.test") -> Callable[[ProbeContext], Dict[str, Any]]:
    def case(ctx: ProbeContext) -> Dict[str, Any]:
        reply = one(ctx, method, target(ctx), headers(ctx), body, version, host if host != "$address" else "%s:%d" % ctx.address)
        return observe(reply)
    return case


def later_by_a_day(value: str) -> str:
    stamp = parse_http_date(value)
    return http_date((stamp or 0) + 86400)


def probe_closing(method: str, target: Callable[[ProbeContext], str], headers: Callable[[ProbeContext], Sequence[Tuple[str, str]]], body: bytes = b"", version: str = "HTTP/1.1", host: Optional[str] = "probe.test") -> Callable[[ProbeContext], Dict[str, Any]]:
    def case(ctx: ProbeContext) -> Dict[str, Any]:
        with Client(ctx) as client:
            reply = client.ask(raw_request(method, target(ctx), headers(ctx), body, version, host), method)
            closed = client.closed_within(3.0)
            extra = bytes(client.wire.buffer)
        return {"status": [reply.status], "closed": closed is not None, "responses": 1 + extra.count(b"HTTP/1.")}
    return case


def smuggling_body(ctx: ProbeContext) -> bytes:
    return b"0\r\n\r\n" + raw_request("GET", ctx.file)


def probe_both_lengths(ctx: ProbeContext) -> Dict[str, Any]:
    body = smuggling_body(ctx)
    return probe_closing("POST", lambda c: c.api + "echo", lambda c: [("Content-Length", "5"), ("Transfer-Encoding", "chunked")], body)(ctx)


def probe_chunked_upload(ctx: ProbeContext) -> Dict[str, Any]:
    pieces = [b"minihttp ", b"chunked ", b"upload\n"]
    body = b"".join(b"%x\r\n%s\r\n" % (len(piece), piece) for piece in pieces) + b"0\r\n\r\n"
    reply = one(ctx, "POST", ctx.api + "echo", [("Transfer-Encoding", "chunked"), ("Content-Type", "text/plain")], body)
    seen = upstream_view(reply)
    fields = seen.get("fields", {})
    return {
        "status": [reply.status],
        "sent_sha256": sha256(b"".join(pieces)),
        "upstream_sha256": seen.get("body_sha256"),
        "upstream_framing": seen.get("framing"),
        "upstream_content_length": fields.get("content-length", ""),
        "upstream_transfer_encoding": fields.get("transfer-encoding", ""),
    }


def probe_expect(ctx: ProbeContext) -> Dict[str, Any]:
    payload = b"hello world"
    with Client(ctx) as client:
        client.send(raw_request("POST", ctx.api + "echo", [("Content-Length", str(len(payload))), ("Expect", "100-continue")]))
        interim: List[int] = []
        try:
            early = parse_status_head(client.wire.read_head(lambda: 1.5, 8192, 65536))
        except Timeout:
            early = None
        if early is not None and early.status != 100:
            return {"status": [early.status], "interim": []}
        if early is not None:
            interim.append(100)
        client.send(payload)
        reply = client.reply()
    seen = upstream_view(reply)
    return {"status": [reply.status], "interim": interim, "upstream_body_length": seen.get("body_length"), "upstream_expect": seen.get("fields", {}).get("expect", "")}


def probe_forwarding(ctx: ProbeContext) -> Dict[str, Any]:
    headers = [
        ("Connection", "keep-alive, X-Hop"),
        ("X-Hop", "secret"),
        ("Keep-Alive", "timeout=5"),
        ("TE", "trailers"),
        ("X-Forwarded-For", "203.0.113.7"),
        ("Via", "1.0 edge"),
        ("X-End-To-End", "kept"),
    ]
    reply = one(ctx, "GET", ctx.api + "echo", headers)
    fields = upstream_view(reply).get("fields", {})
    return {
        "status": [reply.status],
        "host": fields.get("host", ""),
        "x_forwarded_for": fields.get("x-forwarded-for", ""),
        "via": fields.get("via", ""),
        "x_hop": fields.get("x-hop", ""),
        "keep_alive": fields.get("keep-alive", ""),
        "te": fields.get("te", ""),
        "x_end_to_end": fields.get("x-end-to-end", ""),
    }


def probe_slow_upstream(ctx: ProbeContext) -> Dict[str, Any]:
    delay = int((ctx.config.upstream_timeout + 2) * 1000)
    started = time.monotonic()
    reply = one(ctx, "GET", f"{ctx.api}slow?ms={delay}")
    return {"status": [reply.status], "answered_after": round(time.monotonic() - started, 3), "upstream_delay_ms": delay}


def probe_slow_header(ctx: ProbeContext) -> Dict[str, Any]:
    limit = ctx.config.header_timeout
    drip = max(0.05, limit / 4)
    started = time.monotonic()
    sock = socket.create_connection(ctx.address, timeout=ctx.timeout)
    wire = Wire(sock)
    drips = 0
    closed_after: Optional[float] = None
    try:
        sock.sendall(f"GET {ctx.file} HTTP/1.1\r\nHost: probe.test\r\n".encode("latin-1"))
        while time.monotonic() - started < limit + 4:
            try:
                wire.fill(drip)
            except Timeout:
                try:
                    sock.sendall(b"X-Drip-%d: 1\r\n" % drips)
                    drips += 1
                except OSError:
                    closed_after = round(time.monotonic() - started, 3)
                    break
            except Closed:
                closed_after = round(time.monotonic() - started, 3)
                break
    finally:
        sock.close()
    status = []
    match = STATUS_LINE.match(bytes(wire.buffer).split(b"\r\n", 1)[0])
    if match:
        status.append(int(match.group(3)))
    return {"status": status, "closed_after": closed_after, "drips": drips}


CASES: List[Tuple[str, Callable[[ProbeContext], Dict[str, Any]]]] = [
    ("keepalive", probe_keepalive),
    ("idle-close", probe_idle),
    ("http10", probe_http10),
    ("head", probe_head),
    ("range", probe_simple("GET", lambda c: c.file, lambda c: [("Range", "bytes=0-4")])),
    ("suffix-range", probe_simple("GET", lambda c: c.file, lambda c: [("Range", "bytes=-5")])),
    ("range-416", probe_simple("GET", lambda c: c.file, lambda c: [("Range", f"bytes={c.size}-")])),
    ("range-multi", probe_simple("GET", lambda c: c.file, lambda c: [("Range", "bytes=0-1,4-5")])),
    ("if-range-stale", probe_simple("GET", lambda c: c.file, lambda c: [("Range", "bytes=0-4"), ("If-Range", '"stale"')])),
    ("etag-304", probe_simple("GET", lambda c: c.file, lambda c: [("If-None-Match", c.etag)])),
    ("etag-miss", probe_simple("GET", lambda c: c.file, lambda c: [("If-None-Match", '"nope"')])),
    ("ims-exact", probe_simple("GET", lambda c: c.file, lambda c: [("If-Modified-Since", c.last_modified)])),
    ("ims-later", probe_simple("GET", lambda c: c.file, lambda c: [("If-Modified-Since", later_by_a_day(c.last_modified))])),
    ("dot-segments", probe_simple("GET", lambda c: f"{c.directory}/..{c.file}")),
    ("traversal", probe_simple("GET", lambda c: "/../../etc/passwd")),
    ("missing", probe_simple("GET", lambda c: "/does-not-exist.txt")),
    ("dir-redirect", probe_simple("GET", lambda c: c.directory, host="$address")),
    ("dir-index", probe_simple("GET", lambda c: c.directory + "/")),
    ("post-static", probe_simple("POST", lambda c: c.file, lambda c: [("Content-Length", "0")])),
    ("missing-host", probe_simple("GET", lambda c: c.file, host=None)),
    ("both-lengths", probe_both_lengths),
    ("te-unknown", probe_closing("POST", lambda c: c.api + "echo", lambda c: [("Transfer-Encoding", "gzip, chunked")], b"0\r\n\r\n")),
    ("http10-te", probe_closing("POST", lambda c: c.api + "echo", lambda c: [("Transfer-Encoding", "chunked")], b"0\r\n\r\n", version="HTTP/1.0", host=None)),
    ("bad-chunk", probe_closing("POST", lambda c: c.api + "echo", lambda c: [("Transfer-Encoding", "chunked")], b"zz\r\nhello\r\n0\r\n\r\n")),
    ("too-large", probe_closing("POST", lambda c: c.api + "echo", lambda c: [("Content-Length", str(c.config.max_body + 1))])),
    ("chunked-upload", probe_chunked_upload),
    ("expect-continue", probe_expect),
    ("forwarding", probe_forwarding),
    ("upstream-chunked", probe_simple("GET", lambda c: c.api + "chunked")),
    ("slow-upstream", probe_slow_upstream),
    ("dead-upstream", probe_simple("GET", lambda c: c.dead + "anything")),
    ("slow-header", probe_slow_header),
]

PROBE_EXPECT: Dict[str, List[int]] = {
    "keepalive": [200, 200, 200],
    "idle-close": [200],
    "http10": [200, 200],
    "head": [200, 200],
    "range": [206],
    "suffix-range": [206],
    "range-416": [416],
    "range-multi": [200],
    "if-range-stale": [200],
    "etag-304": [304],
    "etag-miss": [200],
    "ims-exact": [304],
    "ims-later": [304],
    "dot-segments": [200],
    "traversal": [400],
    "missing": [404],
    "dir-redirect": [301],
    "dir-index": [200],
    "post-static": [405],
    "missing-host": [400],
    "both-lengths": [400],
    "te-unknown": [501],
    "http10-te": [400],
    "bad-chunk": [400],
    "too-large": [413],
    "chunked-upload": [200],
    "expect-continue": [200],
    "forwarding": [200],
    "upstream-chunked": [200],
    "slow-upstream": [504],
    "dead-upstream": [502],
    "slow-header": [408],
}


def run_probe(ctx: ProbeContext, only: Sequence[str] = ()) -> List[Dict[str, Any]]:
    unknown = [name for name in only if name not in PROBE_EXPECT]
    if unknown:
        raise InputError(f"probe: unknown case(s) {', '.join(unknown)}; known: {', '.join(PROBE_EXPECT)}")
    prime(ctx)
    results = []
    for name, case in CASES:
        if only and name not in only:
            continue
        started = time.monotonic()
        try:
            result = case(ctx)
        except (Timeout, Closed, HTTPError, OSError, ValueError) as exc:
            result = {"status": [], "error": f"{type(exc).__name__}: {exc}"}
        expected = PROBE_EXPECT[name]
        results.append({"case": name, **result, "expected": expected, "verdict": "ok" if result["status"] == expected else "differs", "elapsed": round(time.monotonic() - started, 3)})
    return results


def summarize(result: Dict[str, Any]) -> str:
    skip = {"case", "status", "expected", "verdict", "elapsed", "body", "body_sha256", "sent_sha256", "upstream_sha256"}
    parts = [f"{key}={value}" for key, value in result.items() if key not in skip and value not in ("", None, [], {})]
    text = " ".join(parts)
    return text if len(text) <= 110 else text[:107] + "..."


def render_probe(results: Sequence[Dict[str, Any]]) -> str:
    lines = [f"{'case':<17} {'status':<13} {'minihttp':<13} {'verdict':<8} details"]
    for result in results:
        status = " ".join(str(s) for s in result["status"]) or "-"
        expected = " ".join(str(s) for s in result["expected"])
        lines.append(f"{result['case']:<17} {status:<13} {expected:<13} {result['verdict']:<8} {summarize(result)}")
    differs = sum(1 for result in results if result["verdict"] != "ok")
    lines.append(f"{len(results)} cases, {differs} differ from minihttp's expected status")
    return "\n".join(lines)


def parse_address(value: str) -> Tuple[str, int]:
    host, colon, port = value.rpartition(":")
    if not colon or not port.isdigit():
        raise InputError(f"--target {value!r}: expected HOST:PORT")
    return host or "127.0.0.1", int(port)


def add_server_options(parser: argparse.ArgumentParser) -> None:
    defaults = Config()
    parser.add_argument("--root", default=defaults.root, help="document root for static files")
    parser.add_argument("--host", default=defaults.host, help="address to listen on")
    parser.add_argument("--port", type=int, default=defaults.port, help="port to listen on (0 picks a free one)")
    parser.add_argument("--proxy", action="append", default=[], metavar="/PREFIX/=http://HOST:PORT", help="forward a path prefix to an upstream, repeatable")
    parser.add_argument("--header-timeout", type=float, default=defaults.header_timeout, help="seconds to receive a whole header section (nginx client_header_timeout)")
    parser.add_argument("--body-timeout", type=float, default=defaults.body_timeout, help="seconds between two reads of a body (client_body_timeout)")
    parser.add_argument("--send-timeout", type=float, default=defaults.send_timeout, help="seconds between two writes of a response (send_timeout)")
    parser.add_argument("--keepalive-timeout", type=float, default=defaults.keepalive_timeout, help="seconds an idle persistent connection stays open (keepalive_timeout)")
    parser.add_argument("--keepalive-requests", type=int, default=defaults.keepalive_requests, help="requests per connection before it is closed (keepalive_requests)")
    parser.add_argument("--max-request-line", type=int, default=defaults.max_request_line, help="bytes; longer request lines get 414")
    parser.add_argument("--max-header-bytes", type=int, default=defaults.max_header_bytes, help="bytes; larger header sections get 431")
    parser.add_argument("--max-headers", type=int, default=defaults.max_headers, help="header fields; more get 431 (nginx max_headers answers 400)")
    parser.add_argument("--max-body", type=int, default=defaults.max_body, help="bytes; larger bodies get 413 (client_max_body_size)")
    parser.add_argument("--upstream-connect-timeout", type=float, default=defaults.upstream_connect_timeout, help="seconds to connect to an upstream (proxy_connect_timeout)")
    parser.add_argument("--upstream-timeout", type=float, default=defaults.upstream_timeout, help="seconds between two reads from an upstream (proxy_read_timeout)")
    parser.add_argument("--name", default=defaults.name, help="name used in Server and Via")


def config_from(args: argparse.Namespace) -> Config:
    config = Config(
        root=args.root,
        host=args.host,
        port=args.port,
        proxies=[parse_route(value) for value in args.proxy],
        header_timeout=args.header_timeout,
        body_timeout=args.body_timeout,
        send_timeout=args.send_timeout,
        keepalive_timeout=args.keepalive_timeout,
        keepalive_requests=args.keepalive_requests,
        max_request_line=args.max_request_line,
        max_header_bytes=args.max_header_bytes,
        max_headers=args.max_headers,
        max_body=args.max_body,
        upstream_connect_timeout=args.upstream_connect_timeout,
        upstream_timeout=args.upstream_timeout,
        name=args.name,
    )
    for name in ("header_timeout", "body_timeout", "send_timeout", "keepalive_timeout", "upstream_connect_timeout", "upstream_timeout"):
        if getattr(config, name) <= 0:
            raise InputError(f"--{name.replace('_', '-')} must be greater than 0")
    for name in ("keepalive_requests", "max_request_line", "max_header_bytes", "max_headers", "max_body"):
        if getattr(config, name) < 1:
            raise InputError(f"--{name.replace('_', '-')} must be at least 1")
    return config


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="minihttp", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    serve = sub.add_parser("serve", help="serve static files and proxy prefixes on raw sockets")
    add_server_options(serve)
    serve.add_argument("--access-log", default="-", help="file for the combined-format access log, - for stdout, empty to disable")
    serve.add_argument("--port-file", help="write the bound port here once listening")
    config = sub.add_parser("config", help="print the effective configuration and its nginx equivalent")
    add_server_options(config)
    config.add_argument("--json", action="store_true", help="the configuration dump as JSON")
    config.add_argument("--nginx", action="store_true", help="print an equivalent nginx.conf")
    config.add_argument("--nginx-root", help="document root as nginx sees it (default: --root)")
    config.add_argument("--nginx-listen", help="nginx listen address (default: HOST:PORT)")
    config.add_argument("--nginx-name", default="nginx", help="name nginx appends to Via")
    parse = sub.add_parser("parse", help="split a raw byte stream into requests the way minihttp would")
    add_server_options(parse)
    parse.add_argument("file", help="file with raw request bytes, - for stdin")
    parse.add_argument("--json", action="store_true")
    probe = sub.add_parser("probe", help="send the request matrix to any HTTP/1.1 server and compare with minihttp's answers")
    add_server_options(probe)
    probe.add_argument("--target", required=True, help="HOST:PORT of the server to probe")
    probe.add_argument("--file", default="/hello.txt", help="a static file the server serves")
    probe.add_argument("--dir", default="/docs", help="a directory with an index.html")
    probe.add_argument("--api", default="/api/", help="a prefix proxied to 'minihttp upstream'")
    probe.add_argument("--dead", default="/dead/", help="a prefix proxied to a port where nothing listens")
    probe.add_argument("--timeout", type=float, default=10.0, help="seconds to wait for any single response")
    probe.add_argument("--only", action="append", default=[], help="run only this case, repeatable")
    probe.add_argument("--json", action="store_true")
    probe.add_argument("--strict", action="store_true", help="exit 1 when any case differs from minihttp's expected status")
    upstream = sub.add_parser("upstream", help="an echo upstream: JSON of what it received, /slow?ms=N, /chunked, /status/NNN")
    upstream.add_argument("--host", default="127.0.0.1")
    upstream.add_argument("--port", type=int, default=9000)
    upstream.add_argument("--port-file", help="write the bound port here once listening")
    upstream.add_argument("--keepalive-timeout", type=float, default=75.0, help="idle seconds before the upstream closes a connection; keep it above the proxy's pool timeout (nginx: 60s)")
    logstat = sub.add_parser("logstat", help="count statuses in a combined-format access log (minihttp's or nginx's)")
    logstat.add_argument("file", help="log file, - for stdin")
    logstat.add_argument("--json", action="store_true")
    return parser


def write_port(path: Optional[str], port: int) -> None:
    if path:
        with open(path + ".tmp", "w", encoding="utf-8") as handle:
            handle.write(f"{port}\n")
        os.replace(path + ".tmp", path)


def wait_forever(server: Server) -> int:
    server.start()
    try:
        while server.thread and server.thread.is_alive():
            server.thread.join(1)
    except KeyboardInterrupt:
        server.stop()
    return 0


def read_input(path: str) -> bytes:
    if path == "-":
        return sys.stdin.buffer.read()
    try:
        with open(path, "rb") as handle:
            return handle.read()
    except OSError as exc:
        raise InputError(f"{path}: {exc.strerror}") from None


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(list(sys.argv[1:] if argv is None else argv))
    try:
        if args.command == "upstream":
            server = Server(Config(host=args.host, port=args.port, name="upstream", keepalive_timeout=args.keepalive_timeout, keepalive_requests=1000), app=echo_app)
            write_port(args.port_file, server.address[1])
            print(f"upstream listening on http://{server.address[0]}:{server.address[1]}", file=sys.stderr, flush=True)
            return wait_forever(server)
        if args.command == "logstat":
            stats = log_stats(read_input(args.file).decode("utf-8", "replace").splitlines())
            if args.json:
                print(json.dumps(stats, indent=2))
            else:
                print(f"{stats['parsed']} of {stats['lines']} lines in combined format")
                for status, count in stats["status"].items():
                    print(f"  {status} {phrase(int(status)):<32} {count}")
            return 0
        config = config_from(args)
        if args.command == "config":
            if args.nginx:
                print(nginx_conf(config, args.nginx_root or os.path.abspath(config.root), args.nginx_listen or f"{config.host}:{config.port}", args.nginx_name), end="")
            elif args.json:
                print(json.dumps(config_report(config), indent=2))
            else:
                print(render_config(config))
            return 0
        if args.command == "parse":
            found = parse_stream(read_input(args.file), config)
            print(json.dumps(found, indent=2) if args.json else render_stream(found))
            return 0
        if args.command == "probe":
            ctx = ProbeContext(parse_address(args.target), config, args.file, args.dir, args.api, args.dead, args.timeout)
            results = run_probe(ctx, args.only)
            print(json.dumps(results, indent=2) if args.json else render_probe(results))
            return 1 if args.strict and any(result["verdict"] != "ok" for result in results) else 0
        if not os.path.isdir(config.root):
            raise InputError(f"--root {config.root}: not a directory")
        server = Server(config, log=AccessLog(args.access_log))
        write_port(args.port_file, server.address[1])
        routes = ", ".join(f"{prefix} -> {url}" for prefix, url in config.proxies) or "none"
        print(f"minihttp listening on http://{server.address[0]}:{server.address[1]} root={os.path.abspath(config.root)} proxies: {routes}", file=sys.stderr, flush=True)
        return wait_forever(server)
    except InputError as exc:
        print(f"minihttp: {exc}", file=sys.stderr)
        return 2
    except OSError as exc:
        print(f"minihttp: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
