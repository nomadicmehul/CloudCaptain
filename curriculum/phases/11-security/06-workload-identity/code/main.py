"""miniid: SPIFFE workload identity from scratch. SPIFFE IDs, X.509 SVIDs issued by a small CA built with a DER encoder and ECDSA P-256 written from the curve parameters (RFC 6979 nonces), SPIFFE trust bundles, validation per trust domain, mutual TLS with Python's ssl authorised by SPIFFE ID, a Workload API on a Unix socket that attests callers with SO_PEERCRED and /proc, and rotation at half the lifetime."""
from __future__ import annotations

import argparse
import base64
import contextlib
import datetime as dt
import grp
import hashlib
import hmac
import json
import os
import pwd
import re
import secrets
import socket
import ssl
import struct
import sys
import tempfile
import threading
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Callable, Dict, Iterable, Iterator, List, Optional, Sequence, Set, Tuple

UTC = dt.timezone.utc
SCHEME = "spiffe://"
MAX_ID_BYTES = 2048
MAX_TRUST_DOMAIN_BYTES = 255
TRUST_DOMAIN_RE = re.compile(r"[a-z0-9._-]+")
SEGMENT_RE = re.compile(r"[a-zA-Z0-9._-]+")
NOT_BEFORE_CUSHION = 10
DEFAULT_SVID_TTL = 3600
DEFAULT_CA_TTL = 86400
ROTATE_AT = 0.5
WORKLOAD_HEADER = "workload.spiffe.io"
MAX_CHAIN = 8

OID_EC_PUBLIC_KEY = "1.2.840.10045.2.1"
OID_RSA = "1.2.840.113549.1.1.1"
OID_BASIC_CONSTRAINTS = "2.5.29.19"
OID_KEY_USAGE = "2.5.29.15"
OID_EXT_KEY_USAGE = "2.5.29.37"
OID_SAN = "2.5.29.17"
OID_SKI = "2.5.29.14"
OID_AKI = "2.5.29.35"
OID_SERVER_AUTH = "1.3.6.1.5.5.7.3.1"
OID_CLIENT_AUTH = "1.3.6.1.5.5.7.3.2"
OID_COUNTRY = "2.5.4.6"
SIGNATURE_SCHEMES = {
    "1.2.840.10045.4.3.2": ("ecdsa", "sha256", "ecdsa-with-SHA256"),
    "1.2.840.10045.4.3.3": ("ecdsa", "sha384", "ecdsa-with-SHA384"),
    "1.2.840.10045.4.3.4": ("ecdsa", "sha512", "ecdsa-with-SHA512"),
    "1.2.840.113549.1.1.11": ("rsa", "sha256", "sha256WithRSAEncryption"),
    "1.2.840.113549.1.1.12": ("rsa", "sha384", "sha384WithRSAEncryption"),
    "1.2.840.113549.1.1.13": ("rsa", "sha512", "sha512WithRSAEncryption"),
}
ECDSA_OID_FOR_HASH = {"sha256": "1.2.840.10045.4.3.2", "sha384": "1.2.840.10045.4.3.3"}
HASH_OIDS = {"sha256": "2.16.840.1.101.3.4.2.1", "sha384": "2.16.840.1.101.3.4.2.2", "sha512": "2.16.840.1.101.3.4.2.3"}
KEY_USAGE_BITS = ["digitalSignature", "nonRepudiation", "keyEncipherment", "dataEncipherment", "keyAgreement", "keyCertSign", "cRLSign", "encipherOnly", "decipherOnly"]
EKU_NAMES = {OID_SERVER_AUTH: "serverAuth", OID_CLIENT_AUTH: "clientAuth"}
NAME_LABELS = {"2.5.4.3": "CN", OID_COUNTRY: "C", "2.5.4.10": "O", "2.5.4.11": "OU", "2.5.4.5": "serialNumber", "2.5.4.7": "L", "2.5.4.8": "ST", "2.5.4.45": "x500UniqueIdentifier"}
VERIFY_REASONS = {2: "unknown-authority", 7: "bad-signature", 9: "not-yet-valid", 10: "expired", 18: "unknown-authority", 19: "unknown-authority", 20: "unknown-authority", 21: "unknown-authority", 24: "not-a-ca", 26: "wrong-purpose", 79: "not-a-ca"}
ALERT_REASONS = [
    ("CERTIFICATE_EXPIRED", "expired"),
    ("UNKNOWN_CA", "unknown-authority"),
    ("CERTIFICATE_REQUIRED", "no-certificate"),
    ("PEER_DID_NOT_RETURN_A_CERTIFICATE", "no-certificate"),
    ("NO_CERTIFICATE_RETURNED", "no-certificate"),
    ("UNSUPPORTED_CERTIFICATE", "wrong-purpose"),
    ("BAD_CERTIFICATE", "bad-certificate"),
    ("CERTIFICATE_UNKNOWN", "bad-certificate"),
    ("DECRYPT_ERROR", "bad-signature"),
]


class InputError(ValueError):
    pass


class SpiffeIdError(InputError):
    pass


class DerError(InputError):
    pass


class AttestationError(Exception):
    pass


class WorkloadApiError(Exception):
    pass


@dataclass(frozen=True)
class SpiffeId:
    trust_domain: str
    path: str = ""

    def __str__(self) -> str:
        return SCHEME + self.trust_domain + self.path

    @property
    def segments(self) -> List[str]:
        return self.path.split("/")[1:] if self.path else []


def check_trust_domain(name: str) -> str:
    if len(name.encode("utf-8")) > MAX_TRUST_DOMAIN_BYTES:
        raise SpiffeIdError(f"trust domain of {len(name)} bytes; the limit is {MAX_TRUST_DOMAIN_BYTES}")
    if not TRUST_DOMAIN_RE.fullmatch(name):
        raise SpiffeIdError(f"trust domain {name!r} may contain only lowercase a-z, 0-9, '.', '-' and '_'")
    return name


def parse_spiffe_id(text: str) -> SpiffeId:
    size = len(text.encode("utf-8"))
    if size > MAX_ID_BYTES:
        raise SpiffeIdError(f"{size} bytes; SPIFFE IDs are limited to {MAX_ID_BYTES}")
    if not text.startswith(SCHEME):
        raise SpiffeIdError(f"{text!r} does not start with {SCHEME!r} (the scheme is lowercase spiffe)")
    for char, what in (("?", "a query"), ("#", "a fragment"), ("%", "percent-encoding")):
        if char in text:
            raise SpiffeIdError(f"{text!r} contains {what}, which SPIFFE IDs may not")
    authority, slash, rest = text[len(SCHEME):].partition("/")
    if not authority:
        raise SpiffeIdError(f"{text!r} has an empty trust domain")
    if "@" in authority or ":" in authority:
        raise SpiffeIdError(f"{authority!r} carries userinfo or a port; a trust domain is a bare name")
    check_trust_domain(authority)
    path = slash + rest
    if path.endswith("/"):
        raise SpiffeIdError(f"{text!r} ends with '/'")
    for segment in path.split("/")[1:]:
        if segment in ("", ".", ".."):
            raise SpiffeIdError(f"{text!r} has an empty, '.' or '..' path segment")
        if not SEGMENT_RE.fullmatch(segment):
            raise SpiffeIdError(f"path segment {segment!r} may contain only a-z, A-Z, 0-9, '.', '-' and '_'")
    return SpiffeId(authority, path)


def parse_trust_domain(text: str) -> str:
    if text.startswith(SCHEME):
        spiffe_id = parse_spiffe_id(text)
        if spiffe_id.path:
            raise SpiffeIdError(f"{text!r} names a workload, not a trust domain")
        return spiffe_id.trust_domain
    return check_trust_domain(text)


def der_length(length: int) -> bytes:
    if length < 0x80:
        return bytes([length])
    raw = length.to_bytes((length.bit_length() + 7) // 8, "big")
    return bytes([0x80 | len(raw)]) + raw


def der(tag: int, *parts: bytes) -> bytes:
    content = b"".join(parts)
    return bytes([tag]) + der_length(len(content)) + content


def der_int(value: int) -> bytes:
    return der(0x02, value.to_bytes(value.bit_length() // 8 + 1, "big", signed=True))


def der_oid(dotted: str) -> bytes:
    arcs = [int(arc) for arc in dotted.split(".")]
    body = bytearray()
    for number in [arcs[0] * 40 + arcs[1]] + arcs[2:]:
        chunk = [number & 0x7F]
        number >>= 7
        while number:
            chunk.append(0x80 | (number & 0x7F))
            number >>= 7
        body += bytes(reversed(chunk))
    return der(0x06, bytes(body))


def der_time(moment: dt.datetime) -> bytes:
    moment = moment.astimezone(UTC)
    if 1950 <= moment.year < 2050:
        return der(0x17, moment.strftime("%y%m%d%H%M%SZ").encode("ascii"))
    return der(0x18, moment.strftime("%Y%m%d%H%M%SZ").encode("ascii"))


def der_bits(positions: Iterable[int]) -> bytes:
    ordered = sorted(set(positions))
    if not ordered:
        return der(0x03, b"\x00")
    raw = bytearray(ordered[-1] // 8 + 1)
    for position in ordered:
        raw[position // 8] |= 0x80 >> (position % 8)
    return der(0x03, bytes([7 - ordered[-1] % 8]), bytes(raw))


def der_name(pairs: Sequence[Tuple[str, str]]) -> bytes:
    rdns = []
    for oid, value in pairs:
        string_tag = 0x13 if oid == OID_COUNTRY else 0x0C
        rdns.append(der(0x31, der(0x30, der_oid(oid), der(string_tag, value.encode("utf-8")))))
    return der(0x30, *rdns)


def der_extension(oid: str, value: bytes, critical: bool = False) -> bytes:
    return der(0x30, der_oid(oid), der(0x01, b"\xff") if critical else b"", der(0x04, value))


@dataclass
class Tlv:
    tag: int
    value: bytes
    raw: bytes

    def children(self) -> List["Tlv"]:
        return read_all(self.value)


def read_tlv(data: bytes, pos: int = 0) -> Tuple[Tlv, int]:
    if pos + 2 > len(data):
        raise DerError(f"DER ends at offset {pos} inside a tag or a length")
    tag, first = data[pos], data[pos + 1]
    cursor = pos + 2
    if first == 0x80:
        raise DerError(f"offset {pos}: indefinite length is BER, and DER forbids it")
    if first < 0x80:
        length = first
    else:
        count = first & 0x7F
        if count > 4 or cursor + count > len(data):
            raise DerError(f"offset {pos}: impossible length of length {count}")
        length = int.from_bytes(data[cursor:cursor + count], "big")
        cursor += count
    end = cursor + length
    if end > len(data):
        raise DerError(f"offset {pos}: an element of {length} bytes runs past the end ({len(data) - cursor} left)")
    return Tlv(tag, data[cursor:end], data[pos:end]), end


def read_all(data: bytes) -> List[Tlv]:
    items, pos = [], 0
    while pos < len(data):
        item, pos = read_tlv(data, pos)
        items.append(item)
    return items


def read_one(data: bytes, tag: int, what: str) -> Tlv:
    item, end = read_tlv(data)
    if item.tag != tag or end != len(data):
        raise DerError(f"{what}: expected exactly one element with tag 0x{tag:02x}, found 0x{item.tag:02x}")
    return item


def decode_oid(value: bytes) -> str:
    if not value or value[-1] & 0x80:
        raise DerError("malformed OBJECT IDENTIFIER")
    numbers, current = [], 0
    for byte in value:
        current = (current << 7) | (byte & 0x7F)
        if not byte & 0x80:
            numbers.append(current)
            current = 0
    first = min(numbers[0] // 40, 2)
    return ".".join(str(n) for n in [first, numbers[0] - 40 * first] + numbers[1:])


def decode_time(item: Tlv) -> dt.datetime:
    text = item.value.decode("ascii", "replace")
    if item.tag == 0x17 and re.fullmatch(r"\d{12}Z", text):
        year = int(text[:2])
        return dt.datetime(year + (1900 if year >= 50 else 2000), *(int(text[i:i + 2]) for i in range(2, 12, 2)), tzinfo=UTC)
    if item.tag == 0x18 and re.fullmatch(r"\d{14}Z", text):
        return dt.datetime(int(text[:4]), *(int(text[i:i + 2]) for i in range(4, 14, 2)), tzinfo=UTC)
    raise DerError(f"time {text!r} is neither UTCTime nor GeneralizedTime in UTC")


def name_text(item: Tlv) -> str:
    parts = []
    for rdn in item.children():
        for attribute in rdn.children():
            oid_item, value = attribute.children()[:2]
            oid = decode_oid(oid_item.value)
            codec = "utf-16-be" if value.tag == 0x1E else "utf-8"
            parts.append(f"{NAME_LABELS.get(oid, oid)}={value.value.decode(codec, 'replace')}")
    return ", ".join(parts)


@dataclass(frozen=True)
class Curve:
    name: str
    oid: str
    hash_name: str
    p: int
    b: int
    gx: int
    gy: int
    n: int

    @property
    def generator(self) -> Tuple[int, int]:
        return (self.gx, self.gy)

    @property
    def size(self) -> int:
        return (self.p.bit_length() + 7) // 8


P256 = Curve(
    "P-256", "1.2.840.10045.3.1.7", "sha256",
    p=2 ** 256 - 2 ** 224 + 2 ** 192 + 2 ** 96 - 1,
    b=0x5AC635D8AA3A93E7B3EBBD55769886BC651D06B0CC53B0F63BCE3C3E27D2604B,
    gx=0x6B17D1F2E12C4247F8BCE6E563A440F277037D812DEB33A0F4A13945D898C296,
    gy=0x4FE342E2FE1A7F9B8EE7EB4A7C0F9E162BCE33576B315ECECBB6406837BF51F5,
    n=0xFFFFFFFF00000000FFFFFFFFFFFFFFFFBCE6FAADA7179E84F3B9CAC2FC632551,
)
P384 = Curve(
    "P-384", "1.3.132.0.34", "sha384",
    p=2 ** 384 - 2 ** 128 - 2 ** 96 + 2 ** 32 - 1,
    b=0xB3312FA7E23EE7E4988E056BE3F82D19181D9C6EFE8141120314088F5013875AC656398D8A2ED19D2A85C8EDD3EC2AEF,
    gx=0xAA87CA22BE8B05378EB1C71EF320AD746E1D3B628BA79B9859F741E082542A385502F25DBF55296C3A545E3872760AB7,
    gy=0x3617DE4A96262C6F5D9E98BF9292DC29F8F41DBD289A147CE9DA3113B5F0B8C00A60B1CE1D7E819D7A431D7C90EA0E5F,
    n=0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFC7634D81F4372DDF581A0DB248B0A77AECEC196ACCC52973,
)
CURVES = {curve.oid: curve for curve in (P256, P384)}

Point = Optional[Tuple[int, int]]


def on_curve(curve: Curve, point: Tuple[int, int]) -> bool:
    x, y = point
    return 0 <= x < curve.p and 0 <= y < curve.p and (y * y - (x ** 3 - 3 * x + curve.b)) % curve.p == 0


def point_add(curve: Curve, first: Point, second: Point) -> Point:
    if first is None:
        return second
    if second is None:
        return first
    p = curve.p
    (x1, y1), (x2, y2) = first, second
    if x1 == x2 and (y1 + y2) % p == 0:
        return None
    if first == second:
        slope = (3 * x1 * x1 - 3) * pow(2 * y1, -1, p) % p
    else:
        slope = (y2 - y1) * pow(x2 - x1, -1, p) % p
    x3 = (slope * slope - x1 - x2) % p
    return (x3, (slope * (x1 - x3) - y1) % p)


def scalar_mult(curve: Curve, k: int, point: Point) -> Point:
    result: Point = None
    while k:
        if k & 1:
            result = point_add(curve, result, point)
        point = point_add(curve, point, point)
        k >>= 1
    return result


@dataclass(frozen=True)
class EcKey:
    d: int
    public: Tuple[int, int]
    curve: Curve = P256


def ec_key(d: int, curve: Curve = P256) -> EcKey:
    if not 1 <= d < curve.n:
        raise InputError(f"a {curve.name} private key is a number between 1 and n-1")
    point = scalar_mult(curve, d, curve.generator)
    assert point is not None
    return EcKey(d, point, curve)


def generate_key(curve: Curve = P256) -> EcKey:
    return ec_key(secrets.randbelow(curve.n - 1) + 1, curve)


def bits_to_int(data: bytes, n: int) -> int:
    value = int.from_bytes(data, "big")
    excess = len(data) * 8 - n.bit_length()
    return value >> excess if excess > 0 else value


def rfc6979_nonces(curve: Curve, d: int, digest: bytes) -> Iterator[int]:
    size = (curve.n.bit_length() + 7) // 8
    mac = lambda key, data: hmac.new(key, data, curve.hash_name).digest()
    x = d.to_bytes(size, "big")
    h1 = (bits_to_int(digest, curve.n) % curve.n).to_bytes(size, "big")
    k = b"\x00" * hashlib.new(curve.hash_name).digest_size
    v = b"\x01" * len(k)
    for separator in (b"\x00", b"\x01"):
        k = mac(k, v + separator + x + h1)
        v = mac(k, v)
    while True:
        t = b""
        while len(t) < size:
            v = mac(k, v)
            t += v
        candidate = bits_to_int(t, curve.n)
        if 1 <= candidate < curve.n:
            yield candidate
        k = mac(k, v + b"\x00")
        v = mac(k, v)


def ecdsa_sign(key: EcKey, digest: bytes) -> Tuple[int, int]:
    curve = key.curve
    z = bits_to_int(digest, curve.n)
    for k in rfc6979_nonces(curve, key.d, digest):
        point = scalar_mult(curve, k, curve.generator)
        assert point is not None
        r = point[0] % curve.n
        s = pow(k, -1, curve.n) * (z + r * key.d) % curve.n
        if r and s:
            return r, s
    raise AssertionError("the nonce stream never ends")


def ecdsa_verify(curve: Curve, public: Tuple[int, int], digest: bytes, r: int, s: int) -> bool:
    n = curve.n
    if not (1 <= r < n and 1 <= s < n) or not on_curve(curve, public):
        return False
    w = pow(s, -1, n)
    point = point_add(curve, scalar_mult(curve, bits_to_int(digest, n) * w % n, curve.generator), scalar_mult(curve, r * w % n, public))
    return point is not None and point[0] % n == r


def der_signature(r: int, s: int) -> bytes:
    return der(0x30, der_int(r), der_int(s))


def parse_der_signature(raw: bytes) -> Tuple[int, int]:
    parts = read_one(raw, 0x30, "ECDSA signature").children()
    if len(parts) != 2 or any(part.tag != 0x02 for part in parts):
        raise DerError("an ECDSA signature is SEQUENCE { INTEGER r, INTEGER s }")
    return int.from_bytes(parts[0].value, "big"), int.from_bytes(parts[1].value, "big")


def digest_info(hash_name: str, digest: bytes) -> bytes:
    return der(0x30, der(0x30, der_oid(HASH_OIDS[hash_name]), der(0x05)), der(0x04, digest))


def rsa_verify(n: int, e: int, digest: bytes, hash_name: str, signature: bytes) -> bool:
    k = (n.bit_length() + 7) // 8
    s = int.from_bytes(signature, "big")
    info = digest_info(hash_name, digest)
    if len(signature) != k or s >= n or k < len(info) + 11:
        return False
    expected = b"\x00\x01" + b"\xff" * (k - len(info) - 3) + b"\x00" + info
    return hmac.compare_digest(pow(s, e, n).to_bytes(k, "big"), expected)


@dataclass(frozen=True)
class PublicKey:
    kind: str
    curve: Optional[Curve] = None
    point: Optional[Tuple[int, int]] = None
    n: int = 0
    e: int = 0

    def describe(self) -> str:
        if self.kind == "EC" and self.curve:
            return f"EC {self.curve.name}"
        return f"RSA {self.n.bit_length()}"


def ec_point_bytes(curve: Curve, point: Tuple[int, int]) -> bytes:
    return b"\x04" + point[0].to_bytes(curve.size, "big") + point[1].to_bytes(curve.size, "big")


def spki_der(key: EcKey) -> bytes:
    algorithm = der(0x30, der_oid(OID_EC_PUBLIC_KEY), der_oid(key.curve.oid))
    return der(0x30, algorithm, der(0x03, b"\x00", ec_point_bytes(key.curve, key.public)))


def decode_point(curve: Curve, raw: bytes) -> Tuple[int, int]:
    if len(raw) != 1 + 2 * curve.size or raw[0] != 4:
        raise DerError(f"a {curve.name} public key must be an uncompressed point of {1 + 2 * curve.size} bytes")
    point = (int.from_bytes(raw[1:1 + curve.size], "big"), int.from_bytes(raw[1 + curve.size:], "big"))
    if not on_curve(curve, point):
        raise DerError(f"the public key is not a point on {curve.name}")
    return point


def parse_spki(item: Tlv) -> PublicKey:
    algorithm, bits = item.children()[:2]
    parts = algorithm.children()
    oid = decode_oid(parts[0].value)
    raw = bits.value[1:]
    if oid == OID_EC_PUBLIC_KEY:
        curve_oid = decode_oid(parts[1].value)
        curve = CURVES.get(curve_oid)
        if curve is None:
            raise DerError(f"EC curve {curve_oid} is not supported (P-256 and P-384 are)")
        return PublicKey("EC", curve, decode_point(curve, raw))
    if oid == OID_RSA:
        modulus, exponent = read_one(raw, 0x30, "RSAPublicKey").children()[:2]
        return PublicKey("RSA", n=int.from_bytes(modulus.value, "big"), e=int.from_bytes(exponent.value, "big"))
    raise DerError(f"public key algorithm {oid} is not supported")


def pkcs8_der(key: EcKey) -> bytes:
    public = der(0xA1, der(0x03, b"\x00", ec_point_bytes(key.curve, key.public)))
    sec1 = der(0x30, der_int(1), der(0x04, key.d.to_bytes(key.curve.size, "big")), public)
    algorithm = der(0x30, der_oid(OID_EC_PUBLIC_KEY), der_oid(key.curve.oid))
    return der(0x30, der_int(0), algorithm, der(0x04, sec1))


def parse_sec1(data: bytes, curve: Optional[Curve]) -> EcKey:
    items = read_one(data, 0x30, "ECPrivateKey").children()
    if len(items) < 2 or items[0].value != b"\x01" or items[1].tag != 0x04:
        raise DerError("not an RFC 5915 ECPrivateKey")
    for item in items[2:]:
        if item.tag == 0xA0:
            curve_oid = decode_oid(item.children()[0].value)
            curve = CURVES.get(curve_oid)
            if curve is None:
                raise DerError(f"EC curve {curve_oid} is not supported")
    return ec_key(int.from_bytes(items[1].value, "big"), curve or P256)


def parse_private_key(label: str, data: bytes) -> EcKey:
    if label == "EC PRIVATE KEY":
        return parse_sec1(data, None)
    if label != "PRIVATE KEY":
        raise InputError(f"{label} is not supported; expected an unencrypted PRIVATE KEY (PKCS#8) or EC PRIVATE KEY")
    items = read_one(data, 0x30, "PKCS#8 PrivateKeyInfo").children()
    if len(items) < 3 or items[1].tag != 0x30 or items[2].tag != 0x04:
        raise DerError("not a PKCS#8 PrivateKeyInfo")
    parts = items[1].children()
    if decode_oid(parts[0].value) != OID_EC_PUBLIC_KEY:
        raise InputError("only EC private keys are supported (SPIRE and miniid issue EC P-256 by default)")
    curve = CURVES.get(decode_oid(parts[1].value)) if len(parts) > 1 else None
    return parse_sec1(items[2].value, curve)


PEM_RE = re.compile(r"-----BEGIN ([A-Z0-9 ]+)-----(.*?)-----END \1-----", re.S)


def pem(label: str, data: bytes) -> str:
    text = base64.b64encode(data).decode("ascii")
    return f"-----BEGIN {label}-----\n" + "\n".join(text[i:i + 64] for i in range(0, len(text), 64)) + f"\n-----END {label}-----\n"


def pem_blocks(text: str) -> List[Tuple[str, bytes]]:
    blocks = []
    for match in PEM_RE.finditer(text):
        try:
            blocks.append((match.group(1), base64.b64decode(re.sub(r"\s+", "", match.group(2)), validate=True)))
        except ValueError as exc:
            raise InputError(f"PEM block {match.group(1)} is not valid base64: {exc}") from exc
    return blocks


@dataclass
class Certificate:
    der: bytes
    tbs: bytes
    serial: int
    signature_oid: str
    signature: bytes
    issuer_der: bytes
    subject_der: bytes
    issuer: str
    subject: str
    not_before: dt.datetime
    not_after: dt.datetime
    public_key: PublicKey
    is_ca: Optional[bool] = None
    path_len: Optional[int] = None
    key_usage: Optional[List[str]] = None
    ext_key_usage: Optional[List[str]] = None
    uris: List[str] = field(default_factory=list)
    dns_names: List[str] = field(default_factory=list)
    subject_key_id: bytes = b""
    authority_key_id: bytes = b""
    critical: List[str] = field(default_factory=list)

    @property
    def fingerprint(self) -> str:
        return hashlib.sha256(self.der).hexdigest()

    @property
    def lifetime(self) -> int:
        return int((self.not_after - self.not_before).total_seconds())

    def pem(self) -> str:
        return pem("CERTIFICATE", self.der)


def apply_extension(cert: Certificate, extension: Tlv) -> None:
    parts = extension.children()
    oid = decode_oid(parts[0].value)
    if len(parts) == 3 and parts[1].tag == 0x01 and parts[1].value != b"\x00":
        cert.critical.append(oid)
    inner = parts[-1].value
    if oid == OID_BASIC_CONSTRAINTS:
        values = read_one(inner, 0x30, "basicConstraints").children()
        cert.is_ca = bool(values and values[0].tag == 0x01 and values[0].value != b"\x00")
        lengths = [value for value in values if value.tag == 0x02]
        cert.path_len = int.from_bytes(lengths[0].value, "big") if lengths else None
    elif oid == OID_KEY_USAGE:
        bits = read_one(inner, 0x03, "keyUsage").value[1:]
        cert.key_usage = [name for i, name in enumerate(KEY_USAGE_BITS) if i // 8 < len(bits) and bits[i // 8] & (0x80 >> (i % 8))]
    elif oid == OID_EXT_KEY_USAGE:
        cert.ext_key_usage = [decode_oid(item.value) for item in read_one(inner, 0x30, "extendedKeyUsage").children()]
    elif oid == OID_SAN:
        for name in read_one(inner, 0x30, "subjectAltName").children():
            if name.tag == 0x86:
                cert.uris.append(name.value.decode("ascii", "replace"))
            elif name.tag == 0x82:
                cert.dns_names.append(name.value.decode("ascii", "replace"))
    elif oid == OID_SKI:
        cert.subject_key_id = read_one(inner, 0x04, "subjectKeyIdentifier").value
    elif oid == OID_AKI:
        for item in read_one(inner, 0x30, "authorityKeyIdentifier").children():
            if item.tag == 0x80:
                cert.authority_key_id = item.value


def parse_certificate(data: bytes) -> Certificate:
    try:
        outer = read_one(bytes(data), 0x30, "Certificate").children()
        tbs, algorithm, signature = outer
        fields = tbs.children()
        index = 1 if fields[0].tag == 0xA0 else 0
        serial, _, issuer, validity, subject, spki = fields[index:index + 6]
        not_before, not_after = (decode_time(item) for item in validity.children())
        cert = Certificate(
            der=bytes(data), tbs=tbs.raw, serial=int.from_bytes(serial.value, "big"),
            signature_oid=decode_oid(algorithm.children()[0].value), signature=signature.value[1:],
            issuer_der=issuer.raw, subject_der=subject.raw, issuer=name_text(issuer), subject=name_text(subject),
            not_before=not_before, not_after=not_after, public_key=parse_spki(spki),
        )
        for item in fields[index + 6:]:
            if item.tag == 0xA3:
                for extension in read_one(item.value, 0x30, "extensions").children():
                    apply_extension(cert, extension)
        return cert
    except DerError:
        raise
    except (ValueError, IndexError) as exc:
        raise DerError(f"not an X.509 certificate: {exc}") from exc


def split_certificates(data: bytes) -> List[Certificate]:
    return [parse_certificate(item.raw) for item in read_all(data)]


def load_certificates(text: str, source: str) -> List[Certificate]:
    certs = [parse_certificate(data) for label, data in pem_blocks(text) if label == "CERTIFICATE"]
    if not certs:
        raise InputError(f"{source}: no CERTIFICATE PEM blocks")
    return certs


def load_private_key(text: str, source: str) -> EcKey:
    for label, data in pem_blocks(text):
        if "PRIVATE KEY" in label:
            return parse_private_key(label, data)
    raise InputError(f"{source}: no PRIVATE KEY PEM block")


def key_matches(key: EcKey, cert: Certificate) -> bool:
    return cert.public_key.kind == "EC" and cert.public_key.curve == key.curve and cert.public_key.point == key.public


def signature_valid(cert: Certificate, key: PublicKey) -> bool:
    scheme = SIGNATURE_SCHEMES.get(cert.signature_oid)
    if scheme is None:
        return False
    kind, hash_name, _ = scheme
    digest = hashlib.new(hash_name, cert.tbs).digest()
    if kind == "ecdsa" and key.kind == "EC" and key.curve and key.point:
        try:
            r, s = parse_der_signature(cert.signature)
        except DerError:
            return False
        return ecdsa_verify(key.curve, key.point, digest, r, s)
    if kind == "rsa" and key.kind == "RSA":
        return rsa_verify(key.n, key.e, digest, hash_name, cert.signature)
    return False


def signed_by(cert: Certificate, issuer: Certificate) -> bool:
    if cert.issuer_der != issuer.subject_der:
        return False
    if cert.authority_key_id and issuer.subject_key_id and cert.authority_key_id != issuer.subject_key_id:
        return False
    return signature_valid(cert, issuer.public_key)


def whole_seconds(moment: dt.datetime) -> dt.datetime:
    return moment.astimezone(UTC).replace(microsecond=0)


def key_id(key: EcKey) -> bytes:
    return hashlib.sha1(ec_point_bytes(key.curve, key.public)).digest()


def sign_certificate(subject: bytes, issuer: bytes, key: EcKey, signer: EcKey, not_before: dt.datetime, not_after: dt.datetime, extensions: Sequence[bytes]) -> Certificate:
    algorithm = der(0x30, der_oid(ECDSA_OID_FOR_HASH[signer.curve.hash_name]))
    tbs = der(
        0x30,
        der(0xA0, der_int(2)),
        der_int(secrets.randbits(127) + 1),
        algorithm,
        issuer,
        der(0x30, der_time(not_before), der_time(not_after)),
        subject,
        spki_der(key),
        der(0xA3, der(0x30, *extensions)),
    )
    r, s = ecdsa_sign(signer, hashlib.new(signer.curve.hash_name, tbs).digest())
    return parse_certificate(der(0x30, tbs, algorithm, der(0x03, b"\x00", der_signature(r, s))))


@dataclass
class Bundle:
    trust_domain: str
    authorities: List[Certificate]
    sequence: Optional[int] = None
    refresh_hint: Optional[int] = None

    def pem(self) -> str:
        return "".join(cert.pem() for cert in self.authorities)

    def to_spiffe(self) -> Dict[str, Any]:
        keys = []
        for cert in self.authorities:
            key = cert.public_key
            entry: Dict[str, Any] = {"use": "x509-svid"}
            if key.kind == "EC" and key.curve and key.point:
                entry.update(kty="EC", crv=key.curve.name, x=b64url(key.point[0].to_bytes(key.curve.size, "big")), y=b64url(key.point[1].to_bytes(key.curve.size, "big")))
            else:
                entry.update(kty="RSA", n=b64url(key.n.to_bytes((key.n.bit_length() + 7) // 8, "big")), e=b64url(key.e.to_bytes((key.e.bit_length() + 7) // 8, "big")))
            entry["x5c"] = [base64.b64encode(cert.der).decode("ascii")]
            keys.append(entry)
        document: Dict[str, Any] = {"keys": keys}
        if self.sequence is not None:
            document["spiffe_sequence"] = self.sequence
        if self.refresh_hint is not None:
            document["spiffe_refresh_hint"] = self.refresh_hint
        return document


def b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def authority_trust_domain(cert: Certificate) -> Optional[str]:
    for uri in cert.uris:
        with contextlib.suppress(SpiffeIdError):
            return parse_spiffe_id(uri).trust_domain
    return None


def parse_bundle(data: bytes, trust_domain: Optional[str], source: str) -> Bundle:
    text = data.decode("utf-8", "replace")
    sequence = refresh_hint = None
    if text.lstrip().startswith("{"):
        try:
            document = json.loads(text)
        except ValueError as exc:
            raise InputError(f"{source}: not JSON: {exc}") from exc
        certs = []
        for entry in document.get("keys") or []:
            if entry.get("use") == "x509-svid" and entry.get("x5c"):
                certs.append(parse_certificate(base64.b64decode(entry["x5c"][0])))
        if not certs:
            raise InputError(f"{source}: this SPIFFE bundle has no x509-svid authorities")
        sequence, refresh_hint = document.get("spiffe_sequence"), document.get("spiffe_refresh_hint")
    else:
        certs = load_certificates(text, source)
    if trust_domain is None:
        found = sorted({td for td in (authority_trust_domain(cert) for cert in certs) if td})
        if len(found) != 1:
            raise InputError(f"{source}: cannot tell the trust domain from the CA certificates ({', '.join(found) or 'no SPIFFE URI SAN'}); write --bundle TRUST_DOMAIN={source}")
        trust_domain = found[0]
    return Bundle(parse_trust_domain(trust_domain), certs, sequence, refresh_hint)


def bundle_set(bundles: Iterable[Bundle]) -> Dict[str, Bundle]:
    merged: Dict[str, Bundle] = {}
    for bundle in bundles:
        if bundle.trust_domain in merged:
            known = {cert.fingerprint for cert in merged[bundle.trust_domain].authorities}
            merged[bundle.trust_domain].authorities += [cert for cert in bundle.authorities if cert.fingerprint not in known]
        else:
            merged[bundle.trust_domain] = Bundle(bundle.trust_domain, list(bundle.authorities), bundle.sequence, bundle.refresh_hint)
    return merged


@dataclass
class Authority:
    cert: Certificate
    key: EcKey

    @property
    def trust_domain(self) -> str:
        td = authority_trust_domain(self.cert)
        if td is None:
            raise InputError(f"the CA certificate {self.cert.subject} has no SPIFFE URI SAN naming its trust domain")
        return td

    def bundle(self) -> Bundle:
        return Bundle(self.trust_domain, [self.cert], 1)


@dataclass
class Svid:
    spiffe_id: SpiffeId
    chain: List[Certificate]
    key: EcKey

    @property
    def leaf(self) -> Certificate:
        return self.chain[0]

    def chain_pem(self) -> str:
        return "".join(cert.pem() for cert in self.chain)

    def key_pem(self) -> str:
        return pem("PRIVATE KEY", pkcs8_der(self.key))


def new_authority(trust_domain: str, now: dt.datetime, ttl: int = DEFAULT_CA_TTL, key: Optional[EcKey] = None) -> Authority:
    check_trust_domain(trust_domain)
    key = key or generate_key()
    name = der_name([("2.5.4.10", "miniid"), ("2.5.4.3", f"{trust_domain} root CA")])
    start = whole_seconds(now)
    extensions = [
        der_extension(OID_BASIC_CONSTRAINTS, der(0x30, der(0x01, b"\xff")), critical=True),
        der_extension(OID_KEY_USAGE, der_bits([5, 6]), critical=True),
        der_extension(OID_SAN, der(0x30, der(0x86, f"{SCHEME}{trust_domain}".encode("ascii")))),
        der_extension(OID_SKI, der(0x04, key_id(key))),
    ]
    cert = sign_certificate(name, name, key, key, start - dt.timedelta(seconds=NOT_BEFORE_CUSHION), start + dt.timedelta(seconds=ttl), extensions)
    return Authority(cert, key)


def issue_svid(authority: Authority, spiffe_id: SpiffeId, now: dt.datetime, ttl: int = DEFAULT_SVID_TTL, key: Optional[EcKey] = None, dns_names: Sequence[str] = (), any_trust_domain: bool = False) -> Svid:
    if not spiffe_id.path:
        raise InputError(f"{spiffe_id} names a trust domain; a workload SVID needs a path")
    if spiffe_id.trust_domain != authority.trust_domain and not any_trust_domain:
        raise InputError(f"{spiffe_id} is not in {authority.trust_domain}; this CA issues only for its own trust domain")
    if ttl <= 0:
        raise InputError("the TTL must be a positive number of seconds")
    key = key or generate_key()
    start = whole_seconds(now)
    not_after = min(start + dt.timedelta(seconds=ttl), authority.cert.not_after)
    san = der(0x30, der(0x86, str(spiffe_id).encode("ascii")), *(der(0x82, name.encode("ascii")) for name in dns_names))
    extensions = [
        der_extension(OID_BASIC_CONSTRAINTS, der(0x30), critical=True),
        der_extension(OID_KEY_USAGE, der_bits([0]), critical=True),
        der_extension(OID_EXT_KEY_USAGE, der(0x30, der_oid(OID_SERVER_AUTH), der_oid(OID_CLIENT_AUTH))),
        der_extension(OID_SAN, san),
        der_extension(OID_SKI, der(0x04, key_id(key))),
        der_extension(OID_AKI, der(0x30, der(0x80, authority.cert.subject_key_id))),
    ]
    subject = der_name([("2.5.4.10", "miniid")])
    cert = sign_certificate(subject, authority.cert.subject_der, key, authority.key, start - dt.timedelta(seconds=NOT_BEFORE_CUSHION), not_after, extensions)
    return Svid(spiffe_id, [cert], key)


def leaf_problems(cert: Certificate) -> List[Tuple[str, str]]:
    problems = []
    if len(cert.uris) != 1:
        problems.append(("uri-san-count", f"the certificate has {len(cert.uris)} URI SANs; an X509-SVID has exactly one"))
    if cert.is_ca:
        problems.append(("not-a-leaf", "basicConstraints says CA:TRUE; a signing certificate cannot authenticate a workload"))
    signing = [usage for usage in cert.key_usage or [] if usage in ("keyCertSign", "cRLSign")]
    if signing:
        problems.append(("not-a-leaf", f"keyUsage has {', '.join(signing)}, which a leaf SVID must not set"))
    if len(cert.uris) == 1:
        try:
            spiffe_id = parse_spiffe_id(cert.uris[0])
        except SpiffeIdError as exc:
            problems.append(("bad-id", f"the URI SAN is not a SPIFFE ID: {exc}"))
        else:
            if not spiffe_id.path:
                problems.append(("bad-id", f"{spiffe_id} has no path; a leaf SVID names a workload, not a trust domain"))
    return problems


def svid_problems(cert: Certificate) -> List[str]:
    problems = []
    if cert.is_ca:
        if "keyCertSign" not in (cert.key_usage or []):
            problems.append("a signing certificate must set keyCertSign")
        for uri in cert.uris:
            with contextlib.suppress(SpiffeIdError):
                if parse_spiffe_id(uri).path:
                    problems.append(f"a signing certificate's SPIFFE ID must not have a path ({uri})")
    else:
        problems += [detail for _, detail in leaf_problems(cert)]
        if cert.is_ca is None:
            problems.append("basicConstraints is missing; a leaf SVID must set CA:FALSE")
        if "digitalSignature" not in (cert.key_usage or []):
            problems.append("a leaf SVID must set digitalSignature in keyUsage")
        if cert.ext_key_usage is not None and not {OID_SERVER_AUTH, OID_CLIENT_AUTH} <= set(cert.ext_key_usage):
            problems.append("extendedKeyUsage, when present, must include both serverAuth and clientAuth")
    if cert.key_usage is None:
        problems.append("keyUsage is missing; every SVID must carry it, marked critical")
    elif OID_KEY_USAGE not in cert.critical:
        problems.append("keyUsage must be marked critical")
    if cert.subject_der == b"\x30\x00" and OID_SAN not in cert.critical:
        problems.append("the subject is empty, so the subjectAltName must be critical")
    return problems


@dataclass
class Verdict:
    ok: bool
    reason: str
    detail: str
    spiffe_id: Optional[str] = None
    trust_domain: Optional[str] = None
    chain: List[Certificate] = field(default_factory=list)

    def to_json(self) -> Dict[str, Any]:
        return {"ok": self.ok, "reason": self.reason, "detail": self.detail, "spiffe_id": self.spiffe_id, "trust_domain": self.trust_domain, "chain": [certificate_summary(cert) for cert in self.chain]}


def iso(moment: dt.datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def verify_svid(chain: Sequence[Certificate], bundles: Dict[str, Bundle], at: dt.datetime) -> Verdict:
    if not chain:
        return Verdict(False, "malformed", "no certificate was presented")
    leaf = chain[0]
    problems = leaf_problems(leaf)
    if problems:
        return Verdict(False, problems[0][0], problems[0][1])
    spiffe_id = parse_spiffe_id(leaf.uris[0])
    td = spiffe_id.trust_domain
    bundle = bundles.get(td)
    if bundle is None:
        return Verdict(False, "no-bundle", f"no trust bundle for {td} (have: {', '.join(sorted(bundles)) or 'none'}); the trust domain in the SVID selects the bundle", str(spiffe_id), td)
    path, pool, current = [leaf], list(chain[1:]), leaf
    for _ in range(MAX_CHAIN):
        anchor = next((cert for cert in bundle.authorities if signed_by(current, cert)), None)
        if anchor is not None:
            path.append(anchor)
            break
        step = next((cert for cert in pool if cert.is_ca and "keyCertSign" in (cert.key_usage or []) and signed_by(current, cert)), None)
        if step is None:
            elsewhere = sorted(other for other, candidate in bundles.items() if other != td and any(signed_by(current, cert) for cert in candidate.authorities))
            hint = f"; it was signed by an authority of {elsewhere[0]}, which cannot vouch for {td}" if elsewhere else ""
            return Verdict(False, "unknown-authority", f"{current.subject or 'the certificate'} was not signed by any authority in the {td} bundle{hint}", str(spiffe_id), td)
        path.append(step)
        pool.remove(step)
        current = step
    else:
        return Verdict(False, "unknown-authority", f"no path to the {td} bundle within {MAX_CHAIN} certificates", str(spiffe_id), td)
    for depth, cert in enumerate(path):
        if at < cert.not_before:
            return Verdict(False, "not-yet-valid", f"depth {depth} ({cert.subject}) is valid from {iso(cert.not_before)}", str(spiffe_id), td, path)
        if at > cert.not_after:
            return Verdict(False, "expired", f"depth {depth} ({cert.subject}) expired at {iso(cert.not_after)}", str(spiffe_id), td, path)
    return Verdict(True, "ok", f"{spiffe_id} chains to the {td} bundle", str(spiffe_id), td, path)


@dataclass
class Authorizer:
    ids: Set[str] = field(default_factory=set)
    trust_domains: Set[str] = field(default_factory=set)

    def allows(self, spiffe_id: str) -> bool:
        return spiffe_id in self.ids or parse_spiffe_id(spiffe_id).trust_domain in self.trust_domains

    def describe(self) -> str:
        parts = sorted(self.ids) + [f"any ID in {td}" for td in sorted(self.trust_domains)]
        return ", ".join(parts) or "nobody"


def make_authorizer(ids: Sequence[str] = (), trust_domains: Sequence[str] = ()) -> Authorizer:
    authorizer = Authorizer({str(parse_spiffe_id(item)) for item in ids}, {parse_trust_domain(item) for item in trust_domains})
    if not authorizer.ids and not authorizer.trust_domains:
        raise InputError("say who is allowed: --allow SPIFFE_ID or --allow-td TRUST_DOMAIN (an mTLS peer without a policy is just encrypted, not authorised)")
    return authorizer


def authorize(verdict: Verdict, authorizer: Authorizer) -> Verdict:
    if not verdict.ok or verdict.spiffe_id is None or authorizer.allows(verdict.spiffe_id):
        return verdict
    return Verdict(False, "not-allowed", f"{verdict.spiffe_id} is authenticated but not allowed (allowed: {authorizer.describe()})", verdict.spiffe_id, verdict.trust_domain, verdict.chain)


def rotation_time(cert: Certificate, fraction: float = ROTATE_AT) -> dt.datetime:
    return cert.not_before + (cert.not_after - cert.not_before) * fraction


def rotation_due(cert: Certificate, at: dt.datetime, fraction: float = ROTATE_AT) -> bool:
    return at >= rotation_time(cert, fraction)


def from_epoch(seconds: float) -> dt.datetime:
    return dt.datetime.fromtimestamp(seconds, UTC)


def utcnow() -> dt.datetime:
    return dt.datetime.now(UTC)


@dataclass
class Identity:
    svid: Svid
    bundles: Dict[str, Bundle]

    @property
    def spiffe_id(self) -> str:
        return str(self.svid.spiffe_id)

    def trust_pem(self) -> str:
        return "".join(bundle.pem() for bundle in self.bundles.values())

    def cache_key(self) -> str:
        return self.svid.leaf.fingerprint + "".join(cert.fingerprint for bundle in self.bundles.values() for cert in bundle.authorities)


class Rotator:
    def __init__(self, fetch: Callable[[], Identity], clock: Callable[[], float] = time.time, fraction: float = ROTATE_AT):
        self.fetch = fetch
        self.clock = clock
        self.fraction = fraction
        self.rotations = 0
        self._identity: Optional[Identity] = None
        self._lock = threading.Lock()

    def current(self) -> Identity:
        with self._lock:
            if self._identity is None or rotation_due(self._identity.svid.leaf, from_epoch(self.clock()), self.fraction):
                self._identity = self.fetch()
                self.rotations += 1
            return self._identity


def write_file(path: str, text: str, private: bool = False) -> None:
    folder = os.path.dirname(path)
    if folder:
        os.makedirs(folder, exist_ok=True)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600 if private else 0o644)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(text)
    if private:
        os.chmod(path, 0o600)


def read_file(path: str) -> bytes:
    try:
        with open(path, "rb") as handle:
            return handle.read()
    except OSError as exc:
        raise InputError(f"cannot read {path}: {exc.strerror or exc}") from exc


def tls_context(identity: Identity, server_side: bool) -> ssl.SSLContext:
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER if server_side else ssl.PROTOCOL_TLS_CLIENT)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.check_hostname = False
    context.verify_mode = ssl.CERT_REQUIRED
    context.load_verify_locations(cadata=identity.trust_pem())
    with tempfile.TemporaryDirectory(prefix="miniid-") as folder:
        cert_path, key_path = os.path.join(folder, "svid.pem"), os.path.join(folder, "key.pem")
        write_file(cert_path, identity.svid.chain_pem())
        write_file(key_path, identity.svid.key_pem(), private=True)
        context.load_cert_chain(cert_path, key_path)
    return context


def tls_failure(exc: BaseException) -> Tuple[str, str, Optional[int]]:
    code = getattr(exc, "verify_code", None)
    if isinstance(exc, ssl.SSLCertVerificationError) and code:
        return VERIFY_REASONS.get(code, f"verify-{code}"), f"verify error {code}: {exc.verify_message}", code
    text = f"{getattr(exc, 'reason', '') or ''} {exc}".upper().replace(" ", "_")
    for token, reason in ALERT_REASONS:
        if token in text:
            return reason, str(exc), None
    if isinstance(exc, (ConnectionError, EOFError)) or "EOF" in text:
        return "connection-closed", str(exc) or type(exc).__name__, None
    return "handshake-failed", str(exc) or type(exc).__name__, None


def send_json(sock: socket.socket, payload: Dict[str, Any]) -> None:
    sock.sendall(json.dumps(payload).encode("utf-8") + b"\n")


def read_json_line(sock: socket.socket, limit: int = 1 << 20) -> Dict[str, Any]:
    data = b""
    while not data.endswith(b"\n") and len(data) < limit:
        chunk = sock.recv(65536)
        if not chunk:
            break
        data += chunk
    if not data.strip():
        raise WorkloadApiError("the peer closed the connection without a message")
    try:
        return json.loads(data)
    except ValueError as exc:
        raise WorkloadApiError(f"the peer sent something that is not JSON: {data[:60]!r}") from exc


@dataclass
class Decision:
    decision: str
    stage: str
    reason: str
    detail: str
    spiffe_id: Optional[str] = None
    verify_code: Optional[int] = None
    peer: str = ""
    at: str = ""

    def to_json(self) -> Dict[str, Any]:
        return asdict(self)


def accept_loop(listener: socket.socket, handle: Callable[[socket.socket, Any], None], stop: threading.Event, max_connections: Optional[int], timeout: float) -> None:
    listener.settimeout(0.2)
    workers: List[threading.Thread] = []
    while not stop.is_set() and (max_connections is None or len(workers) < max_connections):
        try:
            conn, address = listener.accept()
        except socket.timeout:
            continue
        except OSError:
            break
        conn.settimeout(timeout)
        worker = threading.Thread(target=handle, args=(conn, address), daemon=True)
        worker.start()
        workers.append(worker)
    for worker in workers:
        worker.join(timeout)


class MtlsServer:
    def __init__(self, identity: Callable[[], Identity], authorizer: Authorizer, host: str = "127.0.0.1", port: int = 0, clock: Callable[[], float] = time.time, enforce_expiry: bool = False, record: Optional[Callable[[Decision], None]] = None, timeout: float = 10.0, idle_timeout: float = 300.0):
        self.identity = identity
        self.authorizer = authorizer
        self.clock = clock
        self.enforce_expiry = enforce_expiry
        self.on_record = record
        self.timeout = timeout
        self.idle_timeout = idle_timeout
        self.decisions: List[Decision] = []
        self.stop = threading.Event()
        self.lock = threading.Lock()
        self._contexts: Dict[str, ssl.SSLContext] = {}
        self.listener = socket.create_server((host, port))
        self.address: Tuple[str, int] = self.listener.getsockname()[:2]

    def now(self) -> dt.datetime:
        return from_epoch(self.clock())

    def context(self) -> Tuple[Identity, ssl.SSLContext]:
        identity = self.identity()
        key = identity.cache_key()
        with self.lock:
            if key not in self._contexts:
                self._contexts = {key: tls_context(identity, server_side=True)}
            return identity, self._contexts[key]

    def record(self, decision: Decision) -> None:
        decision.at = iso(self.now())
        with self.lock:
            self.decisions.append(decision)
        if self.on_record:
            self.on_record(decision)

    def serve(self, max_connections: Optional[int] = None) -> None:
        accept_loop(self.listener, self.handle, self.stop, max_connections, self.timeout)

    def start(self) -> threading.Thread:
        thread = threading.Thread(target=self.serve, daemon=True)
        thread.start()
        return thread

    def close(self) -> None:
        self.stop.set()
        with contextlib.suppress(OSError):
            self.listener.close()

    def handle(self, raw: socket.socket, address: Any) -> None:
        peer = f"{address[0]}:{address[1]}"
        try:
            identity, context = self.context()
        except (InputError, WorkloadApiError, ssl.SSLError) as exc:
            self.record(Decision("reject", "server", "no-identity", f"this server has no usable SVID: {exc}", peer=peer))
            raw.close()
            return
        try:
            tls = context.wrap_socket(raw, server_side=True)
        except (ssl.SSLError, OSError) as exc:
            reason, detail, code = tls_failure(exc)
            self.record(Decision("reject", "handshake", reason, detail, verify_code=code, peer=peer))
            return
        with tls, contextlib.suppress(OSError, ValueError):
            tls.settimeout(self.idle_timeout)
            leaf = parse_certificate(tls.getpeercert(binary_form=True) or b"")
            verdict = authorize(verify_svid([leaf], identity.bundles, self.now()), self.authorizer)
            if not verdict.ok:
                self.record(Decision("reject", "authorize", verdict.reason, verdict.detail, verdict.spiffe_id, peer=peer))
                send_json(tls, {"error": f"permission denied: {verdict.detail}", "reason": verdict.reason})
                return
            self.record(Decision("accept", "authorize", "ok", f"{verdict.spiffe_id} via the {verdict.trust_domain} bundle", verdict.spiffe_id, peer=peer))
            send_json(tls, {"me": identity.spiffe_id, "you": verdict.spiffe_id})
            self.converse(tls, leaf, str(verdict.spiffe_id), identity, peer)

    def converse(self, tls: ssl.SSLSocket, leaf: Certificate, peer_id: str, identity: Identity, peer: str) -> None:
        reader = tls.makefile("rb")
        for line in iter(reader.readline, b""):
            if self.enforce_expiry and self.now() > leaf.not_after:
                self.record(Decision("reject", "expiry", "expired", f"{peer_id}'s certificate expired at {iso(leaf.not_after)} while the connection was open", peer_id, peer=peer))
                send_json(tls, {"error": "your certificate expired during this connection; reconnect with a fresh SVID", "reason": "expired"})
                return
            try:
                message = json.loads(line).get("message")
            except (ValueError, AttributeError):
                message = None
            send_json(tls, {"you": peer_id, "me": identity.spiffe_id, "echo": message})


@dataclass
class CallResult:
    decision: str
    stage: str
    reason: str
    detail: str
    server_id: Optional[str] = None
    replies: List[Dict[str, Any]] = field(default_factory=list)

    def to_json(self) -> Dict[str, Any]:
        return asdict(self)


def read_reply(reader: Any, result: CallResult) -> Optional[CallResult]:
    try:
        line = reader.readline()
    except (ssl.SSLError, OSError) as exc:
        reason, detail, _ = tls_failure(exc)
        return CallResult("reject", "peer-alert", reason, detail, result.server_id, result.replies)
    if not line:
        return CallResult("reject", "peer-alert", "connection-closed", "the server closed the connection without answering", result.server_id, result.replies)
    try:
        reply = json.loads(line)
    except ValueError:
        return CallResult("reject", "denied", "malformed", f"the server answered {line[:60]!r}", result.server_id, result.replies)
    result.replies.append(reply)
    if "error" in reply:
        return CallResult("reject", "denied", str(reply.get("reason", "denied")), str(reply["error"]), result.server_id, result.replies)
    return None


def call(address: Tuple[str, int], identity: Identity, expect: Authorizer, messages: Sequence[str] = ("hello",), interval: float = 0.0, timeout: float = 10.0, clock: Callable[[], float] = time.time) -> CallResult:
    context = tls_context(identity, server_side=False)
    try:
        raw = socket.create_connection(address, timeout=timeout)
    except OSError as exc:
        raise InputError(f"cannot connect to {address[0]}:{address[1]}: {exc.strerror or exc}") from exc
    try:
        tls = context.wrap_socket(raw)
    except (ssl.SSLError, OSError) as exc:
        reason, detail, _ = tls_failure(exc)
        return CallResult("reject", "handshake" if isinstance(exc, ssl.SSLCertVerificationError) else "peer-alert", reason, detail)
    with tls:
        try:
            leaf = parse_certificate(tls.getpeercert(binary_form=True) or b"")
        except InputError as exc:
            return CallResult("reject", "server-identity", "malformed", str(exc))
        verdict = authorize(verify_svid([leaf], identity.bundles, from_epoch(clock())), expect)
        if not verdict.ok:
            return CallResult("reject", "server-identity", verdict.reason, verdict.detail, verdict.spiffe_id)
        result = CallResult("accept", "conversation", "ok", f"mutual TLS with {verdict.spiffe_id}", verdict.spiffe_id)
        reader = tls.makefile("rb")
        failed = read_reply(reader, result)
        if failed:
            return failed
        for index, message in enumerate(messages):
            if index and interval:
                time.sleep(interval)
            try:
                send_json(tls, {"message": message})
            except (ssl.SSLError, OSError) as exc:
                reason, detail, _ = tls_failure(exc)
                return CallResult("reject", "peer-alert", reason, detail, result.server_id, result.replies)
            failed = read_reply(reader, result)
            if failed:
                return failed
        return result


@dataclass(frozen=True)
class Credentials:
    pid: int
    uid: int
    gid: int


def peer_credentials(conn: socket.socket) -> Credentials:
    option = getattr(socket, "SO_PEERCRED", None)
    if option is None or not sys.platform.startswith("linux"):
        raise AttestationError("SO_PEERCRED exists only on Linux; on this platform run the agent with --static-selectors")
    pid, uid, gid = struct.unpack("3i", conn.getsockopt(socket.SOL_SOCKET, option, struct.calcsize("3i")))
    return Credentials(pid, uid, gid)


class ProcFS:
    def __init__(self, root: str = "/proc"):
        self.root = root

    def path(self, pid: int, *parts: str) -> str:
        return os.path.join(self.root, str(pid), *parts)

    def status(self, pid: int) -> Dict[str, List[int]]:
        fields: Dict[str, List[int]] = {}
        with open(self.path(pid, "status"), encoding="utf-8", errors="replace") as handle:
            for line in handle:
                key, _, value = line.partition(":")
                if key in ("Uid", "Gid", "Groups"):
                    fields[key] = [int(number) for number in value.split()]
        return fields

    def starttime(self, pid: int) -> int:
        with open(self.path(pid, "stat"), encoding="utf-8", errors="replace") as handle:
            text = handle.read()
        return int(text[text.rindex(")") + 2:].split()[19])

    def exe(self, pid: int) -> str:
        return os.readlink(self.path(pid, "exe"))

    def owner(self, pid: int) -> int:
        return os.stat(self.path(pid)).st_uid


def user_name(uid: int) -> Optional[str]:
    try:
        return pwd.getpwuid(uid).pw_name
    except KeyError:
        return None


def group_name(gid: int) -> Optional[str]:
    try:
        return grp.getgrgid(gid).gr_name
    except KeyError:
        return None


def unix_selectors(creds: Credentials, proc: ProcFS, discover_path: bool = False, users: Callable[[int], Optional[str]] = user_name, groups: Callable[[int], Optional[str]] = group_name) -> List[str]:
    try:
        status = proc.status(creds.pid)
    except OSError as exc:
        raise AttestationError(f"cannot read {proc.path(creds.pid, 'status')}: {exc.strerror or exc}") from exc
    uids, gids = status.get("Uid") or [creds.uid], status.get("Gid") or [creds.gid]
    uid, gid = uids[1] if len(uids) > 1 else uids[0], gids[1] if len(gids) > 1 else gids[0]
    selectors = [f"unix:uid:{uid}"]
    if users(uid):
        selectors.append(f"unix:user:{users(uid)}")
    selectors.append(f"unix:gid:{gid}")
    if groups(gid):
        selectors.append(f"unix:group:{groups(gid)}")
    for extra in status.get("Groups", []):
        selectors.append(f"unix:supplementary_gid:{extra}")
        if groups(extra):
            selectors.append(f"unix:supplementary_group:{groups(extra)}")
    if discover_path:
        try:
            selectors.append(f"unix:path:{proc.exe(creds.pid)}")
        except OSError as exc:
            raise AttestationError(f"cannot read {proc.path(creds.pid, 'exe')} ({exc.strerror or exc}); path discovery needs the same user or CAP_SYS_PTRACE") from exc
    return selectors


def attest_unix(creds: Credentials, proc: ProcFS, discover_path: bool = False, users: Callable[[int], Optional[str]] = user_name, groups: Callable[[int], Optional[str]] = group_name) -> List[str]:
    try:
        started = proc.starttime(creds.pid)
    except (OSError, ValueError, IndexError) as exc:
        raise AttestationError(f"pid {creds.pid}: cannot read its start time ({exc})") from exc
    selectors = unix_selectors(creds, proc, discover_path, users, groups)
    try:
        same = proc.starttime(creds.pid) == started and proc.owner(creds.pid) == creds.uid
    except (OSError, ValueError, IndexError):
        same = False
    if not same:
        raise AttestationError(f"pid {creds.pid} exited or was replaced while it was being attested; nothing issued")
    return selectors


@dataclass
class Entry:
    spiffe_id: SpiffeId
    selectors: List[str]
    ttl: int = DEFAULT_SVID_TTL
    dns_names: List[str] = field(default_factory=list)


def parse_entries(document: Any) -> List[Entry]:
    if not isinstance(document, list):
        raise InputError("entries: expected a JSON list of {spiffe_id, selectors, ttl}")
    entries = []
    for number, item in enumerate(document, 1):
        selectors = item.get("selectors") if isinstance(item, dict) else None
        if not selectors or not all(isinstance(s, str) and s.count(":") >= 2 for s in selectors):
            raise InputError(f"entry {number}: selectors must be a non-empty list of type:key:value strings such as unix:uid:1000")
        entries.append(Entry(parse_spiffe_id(str(item.get("spiffe_id", ""))), list(selectors), int(item.get("ttl", DEFAULT_SVID_TTL)), list(item.get("dns_names", []))))
    return entries


def matching_entries(entries: Sequence[Entry], selectors: Sequence[str]) -> List[Entry]:
    discovered = set(selectors)
    return [entry for entry in entries if set(entry.selectors) <= discovered]


def b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


class WorkloadApi:
    def __init__(self, authority: Authority, entries: Sequence[Entry], attestor: Callable[[socket.socket], List[str]], clock: Callable[[], float] = time.time, federated: Sequence[Bundle] = (), log: Callable[[str], None] = lambda line: None):
        self.authority = authority
        self.entries = list(entries)
        self.attestor = attestor
        self.clock = clock
        self.federated = list(federated)
        self.log = log
        self.stop = threading.Event()
        self.listener: Optional[socket.socket] = None

    def respond(self, request: Dict[str, Any], selectors: Sequence[str]) -> Dict[str, Any]:
        if str(request.get(WORKLOAD_HEADER, "")).lower() != "true":
            return {"code": "InvalidArgument", "error": "security header missing from request"}
        if request.get("method") != "FetchX509SVID":
            return {"code": "Unimplemented", "error": f"method {request.get('method')!r} is not served; use FetchX509SVID"}
        entries = matching_entries(self.entries, selectors)
        if not entries:
            return {"code": "PermissionDenied", "error": "no identity issued"}
        now, bundle = from_epoch(self.clock()), b64(self.authority.cert.der)
        svids = []
        for entry in entries:
            svid = issue_svid(self.authority, entry.spiffe_id, now, entry.ttl, dns_names=entry.dns_names)
            svids.append({"spiffe_id": str(entry.spiffe_id), "x509_svid": b64(b"".join(cert.der for cert in svid.chain)), "x509_svid_key": b64(pkcs8_der(svid.key)), "bundle": bundle})
        federated = {f"{SCHEME}{item.trust_domain}": b64(b"".join(cert.der for cert in item.authorities)) for item in self.federated}
        return {"svids": svids, "federated_bundles": federated}

    def handle(self, conn: socket.socket, address: Any = None) -> None:
        with conn:
            try:
                selectors = self.attestor(conn)
            except AttestationError as exc:
                self.log(json.dumps({"attested": False, "error": str(exc)}))
                with contextlib.suppress(OSError):
                    send_json(conn, {"code": "PermissionDenied", "error": f"attestation failed: {exc}"})
                return
            try:
                request = read_json_line(conn)
            except (WorkloadApiError, OSError) as exc:
                self.log(json.dumps({"attested": True, "selectors": selectors, "error": str(exc)}))
                return
            response = self.respond(request, selectors)
            self.log(json.dumps({"attested": True, "selectors": list(selectors), "issued": [item["spiffe_id"] for item in response.get("svids", [])], "error": response.get("error")}))
            with contextlib.suppress(OSError):
                send_json(conn, response)

    def bind(self, path: str) -> socket.socket:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(path)
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        listener.bind(path)
        os.chmod(path, 0o777)
        listener.listen(16)
        self.listener = listener
        return listener

    def serve(self, path: str, max_connections: Optional[int] = None) -> None:
        listener = self.listener or self.bind(path)
        try:
            accept_loop(listener, self.handle, self.stop, max_connections, 10.0)
        finally:
            listener.close()
            with contextlib.suppress(OSError):
                os.unlink(path)

    def close(self) -> None:
        self.stop.set()


def fetch_identities(path: str, timeout: float = 10.0) -> List[Identity]:
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as conn:
        conn.settimeout(timeout)
        try:
            conn.connect(path)
        except OSError as exc:
            raise InputError(f"cannot connect to the Workload API at {path}: {exc.strerror or exc}") from exc
        send_json(conn, {"method": "FetchX509SVID", WORKLOAD_HEADER: "true"})
        response = read_json_line(conn)
    if "error" in response:
        raise WorkloadApiError(f"{response.get('code', 'Unknown')}: {response['error']}")
    federated = [Bundle(parse_trust_domain(td), split_certificates(base64.b64decode(data))) for td, data in (response.get("federated_bundles") or {}).items()]
    identities = []
    for item in response.get("svids") or []:
        spiffe_id = parse_spiffe_id(item["spiffe_id"])
        chain = split_certificates(base64.b64decode(item["x509_svid"]))
        key = parse_private_key("PRIVATE KEY", base64.b64decode(item["x509_svid_key"]))
        own = Bundle(spiffe_id.trust_domain, split_certificates(base64.b64decode(item["bundle"])))
        identities.append(Identity(Svid(spiffe_id, chain, key), bundle_set([own] + federated)))
    if not identities:
        raise WorkloadApiError("the Workload API answered with no SVIDs")
    return identities


def certificate_summary(cert: Certificate) -> Dict[str, Any]:
    scheme = SIGNATURE_SCHEMES.get(cert.signature_oid)
    spiffe_id = cert.uris[0] if len(cert.uris) == 1 else None
    trust_domain = None
    if spiffe_id:
        with contextlib.suppress(SpiffeIdError):
            trust_domain = parse_spiffe_id(spiffe_id).trust_domain
    return {
        "role": "ca" if cert.is_ca else "leaf",
        "spiffe_id": spiffe_id,
        "trust_domain": trust_domain,
        "uris": cert.uris,
        "dns_names": cert.dns_names,
        "subject": cert.subject,
        "issuer": cert.issuer,
        "serial": f"{cert.serial:x}",
        "not_before": iso(cert.not_before),
        "not_after": iso(cert.not_after),
        "not_before_epoch": int(cert.not_before.timestamp()),
        "not_after_epoch": int(cert.not_after.timestamp()),
        "lifetime_seconds": cert.lifetime,
        "rotate_at": iso(rotation_time(cert)),
        "is_ca": cert.is_ca,
        "path_len": cert.path_len,
        "key_usage": cert.key_usage,
        "ext_key_usage": [EKU_NAMES.get(oid, oid) for oid in cert.ext_key_usage] if cert.ext_key_usage is not None else None,
        "critical": cert.critical,
        "public_key": cert.public_key.describe(),
        "signature_algorithm": scheme[2] if scheme else cert.signature_oid,
        "subject_key_id": cert.subject_key_id.hex(),
        "authority_key_id": cert.authority_key_id.hex(),
        "sha256": cert.fingerprint,
        "problems": svid_problems(cert),
    }


def render_certificate(index: int, cert: Certificate) -> str:
    info = certificate_summary(cert)
    usage = ", ".join(info["key_usage"] or ["no keyUsage"])
    if info["ext_key_usage"] is not None:
        usage += "; " + ", ".join(info["ext_key_usage"])
    usage += "; CA:TRUE" if cert.is_ca else "; CA:FALSE"
    lines = [
        f"[{index}] {info['role']:4}  {', '.join(cert.uris) or '(no URI SAN)'}",
        f"    subject   {cert.subject or '(empty)'}",
        f"    issuer    {cert.issuer}",
        f"    valid     {info['not_before']} .. {info['not_after']} ({info['lifetime_seconds']} s), rotate at {info['rotate_at']}",
        f"    key       {info['public_key']}, signed with {info['signature_algorithm']}",
        f"    usage     {usage}",
        f"    sha256    {cert.fingerprint}",
        f"    problems  {'; '.join(info['problems']) or 'none'}",
    ]
    return "\n".join(lines)


def render_verdict(verdict: Verdict) -> str:
    if verdict.ok:
        return f"OK  {verdict.detail} ({len(verdict.chain)} certificates)"
    return f"REJECT  {verdict.reason}: {verdict.detail}"


def render_call(result: CallResult) -> str:
    lines = [f"{result.decision.upper()}  stage={result.stage} reason={result.reason}  {result.detail}"]
    for reply in result.replies:
        lines.append(f"  reply {json.dumps(reply)}")
    return "\n".join(lines)


def parse_at(text: Optional[str]) -> dt.datetime:
    if not text:
        return utcnow()
    try:
        if text.startswith("@"):
            return from_epoch(float(text[1:]))
        moment = dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise InputError(f"--at {text!r}: use 2026-11-02T10:00:00Z or @EPOCH_SECONDS") from exc
    return moment if moment.tzinfo else moment.replace(tzinfo=UTC)


def split_address(text: str) -> Tuple[str, int]:
    host, colon, port = text.rpartition(":")
    if not colon or not port.isdigit():
        raise InputError(f"{text!r} is not HOST:PORT")
    return host or "127.0.0.1", int(port)


def load_bundle_spec(spec: str) -> Bundle:
    trust_domain, equals, path = spec.partition("=")
    if not equals:
        trust_domain, path = "", spec
    return parse_bundle(read_file(path), trust_domain or None, path)


def load_authority(folder: str) -> Authority:
    cert = load_certificates(read_file(os.path.join(folder, "ca.pem")).decode("utf-8", "replace"), os.path.join(folder, "ca.pem"))[0]
    key = load_private_key(read_file(os.path.join(folder, "ca-key.pem")).decode("utf-8", "replace"), os.path.join(folder, "ca-key.pem"))
    if not key_matches(key, cert):
        raise InputError(f"{folder}: ca-key.pem does not belong to ca.pem")
    return Authority(cert, key)


def identity_from_files(svid_path: str, key_path: str, bundle_specs: Sequence[str]) -> Identity:
    chain = load_certificates(read_file(svid_path).decode("utf-8", "replace"), svid_path)
    key = load_private_key(read_file(key_path).decode("utf-8", "replace"), key_path)
    if not key_matches(key, chain[0]):
        raise InputError(f"{key_path} does not belong to the first certificate in {svid_path}")
    if len(chain[0].uris) != 1:
        raise InputError(f"{svid_path}: the first certificate has {len(chain[0].uris)} URI SANs; an SVID has exactly one")
    if not bundle_specs:
        raise InputError("give at least one --bundle: the CAs of the trust domains you accept peers from")
    return Identity(Svid(parse_spiffe_id(chain[0].uris[0]), chain, key), bundle_set(load_bundle_spec(spec) for spec in bundle_specs))


def identity_source(args: argparse.Namespace) -> Callable[[], Identity]:
    if args.workload_api:
        return Rotator(lambda: fetch_identities(args.workload_api)[0]).current
    if not (args.svid and args.key):
        raise InputError("give --svid and --key (and --bundle), or --workload-api SOCKET")
    identity = identity_from_files(args.svid, args.key, args.bundle)
    return lambda: identity


def emit(args: argparse.Namespace, payload: Any, text: str) -> None:
    print(json.dumps(payload, indent=2) if getattr(args, "json", False) else text)


def command_id(args: argparse.Namespace) -> int:
    try:
        spiffe_id = parse_spiffe_id(args.spiffe_id)
    except SpiffeIdError as exc:
        emit(args, {"valid": False, "error": str(exc)}, f"invalid: {exc}")
        return 1
    payload = {"valid": True, "spiffe_id": str(spiffe_id), "trust_domain": spiffe_id.trust_domain, "path": spiffe_id.path, "segments": spiffe_id.segments}
    emit(args, payload, f"{spiffe_id}  trust domain {spiffe_id.trust_domain}  path {spiffe_id.path or '(none: this is the trust domain ID)'}")
    return 0


def command_ca(args: argparse.Namespace) -> int:
    authority = new_authority(args.trust_domain, parse_at(args.at), args.ttl)
    bundle = authority.bundle()
    files = {name: os.path.join(args.out, name) for name in ("ca.pem", "ca-key.pem", "bundle.pem", "bundle.json")}
    write_file(files["ca.pem"], authority.cert.pem())
    write_file(files["ca-key.pem"], pem("PRIVATE KEY", pkcs8_der(authority.key)), private=True)
    write_file(files["bundle.pem"], bundle.pem())
    write_file(files["bundle.json"], json.dumps(bundle.to_spiffe(), indent=2) + "\n")
    emit(args, {"trust_domain": authority.trust_domain, "certificate": certificate_summary(authority.cert), "files": files}, render_certificate(0, authority.cert) + "\n" + "\n".join(f"wrote {path}" for path in files.values()))
    return 0


def command_issue(args: argparse.Namespace) -> int:
    authority = load_authority(args.ca)
    svid = issue_svid(authority, parse_spiffe_id(args.id), parse_at(args.at), args.ttl, dns_names=args.dns, any_trust_domain=args.any_trust_domain)
    files = {name: os.path.join(args.out, name) for name in ("svid.pem", "key.pem", "bundle.pem")}
    write_file(files["svid.pem"], svid.chain_pem())
    write_file(files["key.pem"], svid.key_pem(), private=True)
    write_file(files["bundle.pem"], authority.cert.pem())
    emit(args, {"spiffe_id": str(svid.spiffe_id), "certificate": certificate_summary(svid.leaf), "files": files}, render_certificate(0, svid.leaf) + "\n" + "\n".join(f"wrote {path}" for path in files.values()))
    return 0


def command_inspect(args: argparse.Namespace) -> int:
    data = read_file(args.file)
    text = data.decode("utf-8", "replace")
    if text.lstrip().startswith("{"):
        bundle = parse_bundle(data, args.trust_domain, args.file)
        certs, kind = bundle.authorities, f"SPIFFE bundle for {bundle.trust_domain} (sequence {bundle.sequence})"
    else:
        certs, kind = load_certificates(text, args.file), "PEM"
    keys = [parse_private_key(label, raw) for label, raw in pem_blocks(text) if "PRIVATE KEY" in label]
    summaries = [certificate_summary(cert) for cert in certs]
    payload = {"source": args.file, "kind": kind, "certificates": summaries, "keys": [{"curve": key.curve.name, "matches": [i for i, cert in enumerate(certs) if key_matches(key, cert)]} for key in keys]}
    lines = [f"{args.file}: {kind}, {len(certs)} certificate(s)"] + [render_certificate(i, cert) for i, cert in enumerate(certs)]
    lines += [f"private key {key.curve.name}: {'matches certificate ' + str(item['matches']) if item['matches'] else 'matches no certificate here'}" for key, item in zip(keys, payload["keys"])]
    emit(args, payload, "\n".join(lines))
    return 1 if any(summary["problems"] for summary in summaries) else 0


def command_verify(args: argparse.Namespace) -> int:
    chain = load_certificates(read_file(args.svid).decode("utf-8", "replace"), args.svid)
    if not args.bundle:
        raise InputError("give at least one --bundle [TRUST_DOMAIN=]FILE")
    verdict = verify_svid(chain, bundle_set(load_bundle_spec(spec) for spec in args.bundle), parse_at(args.at))
    if args.allow or args.allow_td:
        verdict = authorize(verdict, make_authorizer(args.allow, args.allow_td))
    emit(args, verdict.to_json(), render_verdict(verdict))
    return 0 if verdict.ok else 1


def command_serve(args: argparse.Namespace) -> int:
    authorizer = make_authorizer(args.allow, args.allow_td)
    source = identity_source(args)
    host, port = split_address(args.listen)
    log = open(args.decisions, "a", encoding="utf-8") if args.decisions else None

    def record(decision: Decision) -> None:
        if log:
            log.write(json.dumps(decision.to_json()) + "\n")
            log.flush()
        print(f"{decision.decision:6} {decision.stage:9} {decision.reason:18} {decision.spiffe_id or '-'}  {decision.detail}", file=sys.stderr, flush=True)

    server = MtlsServer(source, authorizer, host, port, enforce_expiry=args.enforce_expiry, record=record)
    if args.port_file:
        write_file(args.port_file, f"{server.address[1]}\n")
    print(f"miniid: mTLS on {server.address[0]}:{server.address[1]} as {source().spiffe_id}, allowing {authorizer.describe()}", file=sys.stderr, flush=True)
    try:
        server.serve(args.max_connections)
    finally:
        server.close()
        if log:
            log.close()
    return 0


def command_call(args: argparse.Namespace) -> int:
    expect = make_authorizer(args.expect_server, args.expect_server_td)
    result = call(split_address(args.address), identity_source(args)(), expect, [args.message] * args.count, args.interval, args.timeout)
    emit(args, result.to_json(), render_call(result))
    return 0 if result.decision == "accept" else 1


def command_agent(args: argparse.Namespace) -> int:
    authority = load_authority(args.ca)
    try:
        entries = parse_entries(json.loads(read_file(args.entries)))
    except ValueError as exc:
        if isinstance(exc, InputError):
            raise
        raise InputError(f"{args.entries}: not JSON: {exc}") from exc
    if args.static_selectors:
        fixed = [item.strip() for item in args.static_selectors.split(",") if item.strip()]
        attestor: Callable[[socket.socket], List[str]] = lambda conn: fixed
    elif not sys.platform.startswith("linux"):
        raise InputError("attestation by SO_PEERCRED needs Linux; on this platform pass --static-selectors unix:uid:1000,...")
    else:
        proc = ProcFS(args.proc)
        attestor = lambda conn: attest_unix(peer_credentials(conn), proc, args.discover_path)
    log_handle = open(args.log, "a", encoding="utf-8") if args.log else sys.stderr
    lock = threading.Lock()

    def log(line: str) -> None:
        with lock:
            print(line, file=log_handle, flush=True)

    api = WorkloadApi(authority, entries, attestor, federated=[load_bundle_spec(spec) for spec in args.federate], log=log)
    api.bind(args.socket)
    print(f"miniid agent: Workload API on {args.socket} for {authority.trust_domain}, {len(entries)} entries", file=sys.stderr, flush=True)
    api.serve(args.socket, args.max_connections)
    return 0


def command_fetch(args: argparse.Namespace) -> int:
    identities = fetch_identities(args.socket, args.timeout)
    rows, lines = [], []
    for index, identity in enumerate(identities):
        verdict = verify_svid(identity.svid.chain, identity.bundles, utcnow())
        if not verdict.ok:
            raise WorkloadApiError(f"the Workload API returned an SVID that does not verify: {verdict.reason}: {verdict.detail}")
        files: Dict[str, str] = {}
        if args.out:
            files = {"svid": os.path.join(args.out, f"svid.{index}.pem"), "key": os.path.join(args.out, f"svid.{index}.key"), "bundle": os.path.join(args.out, f"bundle.{index}.pem")}
            write_file(files["svid"], identity.svid.chain_pem())
            write_file(files["key"], identity.svid.key_pem(), private=True)
            write_file(files["bundle"], identity.bundles[identity.svid.spiffe_id.trust_domain].pem())
            for number, td in enumerate(sorted(td for td in identity.bundles if td != identity.svid.spiffe_id.trust_domain)):
                files[f"federated:{td}"] = os.path.join(args.out, f"federated_bundle.{index}.{number}.pem")
                write_file(files[f"federated:{td}"], identity.bundles[td].pem())
        leaf = identity.svid.leaf
        rows.append({"spiffe_id": identity.spiffe_id, "certificate": certificate_summary(leaf), "trust_domains": sorted(identity.bundles), "files": files})
        lines.append(f"{identity.spiffe_id}  valid {iso(leaf.not_before)} .. {iso(leaf.not_after)} ({leaf.lifetime} s), rotate at {iso(rotation_time(leaf))}, bundles for {', '.join(sorted(identity.bundles))}")
        lines += [f"  wrote {path}" for path in files.values()]
    emit(args, {"svids": rows}, "\n".join(lines))
    return 0


def add_identity_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--svid", help="PEM file with the SVID (leaf first)")
    parser.add_argument("--key", help="PEM file with its private key")
    parser.add_argument("--bundle", action="append", default=[], metavar="[TD=]FILE", help="trust bundle (PEM or SPIFFE JSON); repeat to federate")
    parser.add_argument("--workload-api", metavar="SOCKET", help="fetch the SVID and bundles from a miniid agent instead of files, rotating at half the lifetime")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="miniid", description="SPIFFE IDs, X.509 SVIDs, attestation and mutual TLS authorised by SPIFFE ID, from scratch.")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("id", help="check a SPIFFE ID against the specification")
    p.add_argument("spiffe_id")
    p.add_argument("--json", action="store_true")

    p = sub.add_parser("ca", help="create a self-signed CA for a trust domain")
    p.add_argument("--trust-domain", required=True)
    p.add_argument("--out", required=True)
    p.add_argument("--ttl", type=int, default=DEFAULT_CA_TTL)
    p.add_argument("--at", help="issue as if now were this time (ISO 8601 or @EPOCH)")
    p.add_argument("--json", action="store_true")

    p = sub.add_parser("issue", help="issue an X509-SVID from a CA directory")
    p.add_argument("--ca", required=True, help="directory with ca.pem and ca-key.pem")
    p.add_argument("--id", required=True)
    p.add_argument("--out", required=True)
    p.add_argument("--ttl", type=int, default=DEFAULT_SVID_TTL)
    p.add_argument("--dns", action="append", default=[])
    p.add_argument("--at", help="issue as if now were this time (ISO 8601 or @EPOCH)")
    p.add_argument("--any-trust-domain", action="store_true", help="let this CA sign an ID outside its trust domain (to demonstrate the attack)")
    p.add_argument("--json", action="store_true")

    p = sub.add_parser("inspect", help="print certificates, keys or a SPIFFE bundle and check them against the X509-SVID rules")
    p.add_argument("file")
    p.add_argument("--trust-domain")
    p.add_argument("--json", action="store_true")

    p = sub.add_parser("verify", help="validate an SVID against the bundle of its own trust domain")
    p.add_argument("svid")
    p.add_argument("--bundle", action="append", default=[], metavar="[TD=]FILE")
    p.add_argument("--allow", action="append", default=[])
    p.add_argument("--allow-td", action="append", default=[])
    p.add_argument("--at")
    p.add_argument("--json", action="store_true")

    p = sub.add_parser("serve", help="mutual TLS server that authorises clients by SPIFFE ID")
    add_identity_arguments(p)
    p.add_argument("--allow", action="append", default=[])
    p.add_argument("--allow-td", action="append", default=[])
    p.add_argument("--listen", default="127.0.0.1:8443")
    p.add_argument("--port-file")
    p.add_argument("--decisions", help="append one JSON line per decision to this file")
    p.add_argument("--max-connections", type=int)
    p.add_argument("--enforce-expiry", action="store_true", help="close a connection once the client's certificate has expired")

    p = sub.add_parser("call", help="mutual TLS client that authorises the server by SPIFFE ID")
    p.add_argument("address")
    add_identity_arguments(p)
    p.add_argument("--expect-server", action="append", default=[])
    p.add_argument("--expect-server-td", action="append", default=[])
    p.add_argument("--message", default="hello")
    p.add_argument("--count", type=int, default=1)
    p.add_argument("--interval", type=float, default=0.0)
    p.add_argument("--timeout", type=float, default=10.0)
    p.add_argument("--json", action="store_true")

    p = sub.add_parser("agent", help="serve a Workload API on a Unix socket, attesting callers by SO_PEERCRED")
    p.add_argument("--socket", required=True)
    p.add_argument("--ca", required=True)
    p.add_argument("--entries", required=True, help="JSON list of {spiffe_id, selectors, ttl}")
    p.add_argument("--federate", action="append", default=[], metavar="[TD=]FILE")
    p.add_argument("--discover-path", action="store_true", help="add unix:path from /proc/PID/exe")
    p.add_argument("--static-selectors", help="attest every caller with these selectors (for platforms without SO_PEERCRED)")
    p.add_argument("--proc", default="/proc")
    p.add_argument("--max-connections", type=int)
    p.add_argument("--log")

    p = sub.add_parser("fetch", help="fetch X509-SVIDs from a miniid agent")
    p.add_argument("--socket", required=True)
    p.add_argument("--out")
    p.add_argument("--timeout", type=float, default=10.0)
    p.add_argument("--json", action="store_true")
    return parser


COMMANDS = {"id": command_id, "ca": command_ca, "issue": command_issue, "inspect": command_inspect, "verify": command_verify, "serve": command_serve, "call": command_call, "agent": command_agent, "fetch": command_fetch}


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(list(sys.argv[1:] if argv is None else argv))
    try:
        return COMMANDS[args.command](args)
    except WorkloadApiError as exc:
        print(f"miniid: {exc}", file=sys.stderr)
        return 1
    except InputError as exc:
        print(f"miniid: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
