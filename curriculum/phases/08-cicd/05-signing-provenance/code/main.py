"""minisign: sign and verify the way cosign does, with nothing but the standard library. ECDSA P-256 from the curve parameters with RFC 6979 nonces, Sigstore bundles, DSSE envelopes carrying in-toto SLSA provenance, a verification policy, and an RFC 6962 transparency log with signed checkpoints, inclusion and consistency proofs, all checked offline."""
from __future__ import annotations

import argparse
import base64
import binascii
import hashlib
import hmac
import json
import os
import re
import secrets
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, List, Optional, Sequence, Tuple

P = 0xFFFFFFFF00000001000000000000000000000000FFFFFFFFFFFFFFFFFFFFFFFF
A = P - 3
B = 0x5AC635D8AA3A93E7B3EBBD55769886BC651D06B0CC53B0F63BCE3C3E27D2604B
N = 0xFFFFFFFF00000000FFFFFFFFFFFFFFFFBCE6FAADA7179E84F3B9CAC2FC632551
G = (
    0x6B17D1F2E12C4247F8BCE6E563A440F277037D812DEB33A0F4A13945D898C296,
    0x4FE342E2FE1A7F9B8EE7EB4A7C0F9E162BCE33576B315ECECBB6406837BF51F5,
)
OID_EC_PUBLIC_KEY = "1.2.840.10045.2.1"
OID_P256 = "1.2.840.10045.3.1.7"
OID_SUBJECT_ALT_NAME = "2.5.29.17"
OID_FULCIO_ISSUER_V1 = "1.3.6.1.4.1.57264.1.1"
OID_FULCIO_ISSUER_V2 = "1.3.6.1.4.1.57264.1.8"
OID_NAMES = {
    "1.2.840.113549.1.1.1": "RSA",
    "1.3.101.112": "Ed25519",
    "1.3.132.0.34": "P-384",
    "1.3.132.0.35": "P-521",
    "1.3.132.0.10": "secp256k1",
}
BUNDLE_V03 = "application/vnd.dev.sigstore.bundle.v0.3+json"
BUNDLE_MEDIA_TYPES = (
    BUNDLE_V03,
    "application/vnd.dev.sigstore.bundle+json;version=0.2",
    "application/vnd.dev.sigstore.bundle+json;version=0.1",
)
INTOTO_PAYLOAD_TYPE = "application/vnd.in-toto+json"
STATEMENT_V1 = "https://in-toto.io/Statement/v1"
STATEMENT_TYPES = (STATEMENT_V1, "https://in-toto.io/Statement/v0.1")
SLSA_V1 = "https://slsa.dev/provenance/v1"
SLSA_V02 = "https://slsa.dev/provenance/v0.2"
BUILD_TYPE = "https://cloudcaptain.io/learn/cicd/signing-provenance"
NOTE_SIGNATURE_PREFIX = "\u2014 "
Point = Optional[Tuple[int, int]]


class InputError(ValueError):
    pass


def sha256(data: bytes) -> bytes:
    return hashlib.sha256(data).digest()


def b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def b64decode(text: Any, what: str = "base64 value") -> bytes:
    if not isinstance(text, str):
        raise InputError(f"{what}: expected a base64 string, got {type(text).__name__}")
    cleaned = "".join(text.split()).replace("-", "+").replace("_", "/")
    try:
        return base64.b64decode(cleaned + "=" * (-len(cleaned) % 4), validate=True)
    except (binascii.Error, ValueError) as exc:
        raise InputError(f"{what}: not base64 ({exc})") from None


def canonical_json(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def on_curve(point: Point) -> bool:
    if point is None:
        return True
    x, y = point
    return 0 <= x < P and 0 <= y < P and (y * y - x * x * x - A * x - B) % P == 0


def point_add(left: Point, right: Point) -> Point:
    if left is None:
        return right
    if right is None:
        return left
    (x1, y1), (x2, y2) = left, right
    if x1 == x2 and (y1 + y2) % P == 0:
        return None
    if x1 == x2:
        slope = (3 * x1 * x1 + A) * pow(2 * y1, -1, P) % P
    else:
        slope = (y2 - y1) * pow(x2 - x1, -1, P) % P
    x3 = (slope * slope - x1 - x2) % P
    return x3, (slope * (x1 - x3) - y1) % P


def scalar_mult(k: int, point: Point) -> Point:
    result: Point = None
    addend = point
    k %= N
    while k:
        if k & 1:
            result = point_add(result, addend)
        addend = point_add(addend, addend)
        k >>= 1
    return result


@dataclass(frozen=True)
class PublicKey:
    x: int
    y: int

    @property
    def point(self) -> Tuple[int, int]:
        return (self.x, self.y)

    def der(self) -> bytes:
        return spki_der(self)

    def pem(self) -> str:
        return pem_encode("PUBLIC KEY", self.der())

    @property
    def log_id(self) -> bytes:
        return sha256(self.der())

    @property
    def fingerprint(self) -> str:
        return "sha256:" + self.log_id.hex()


@dataclass(frozen=True)
class PrivateKey:
    d: int
    public: PublicKey


def private_key_from_int(d: int) -> PrivateKey:
    if not 1 <= d < N:
        raise InputError("private key: the scalar must be between 1 and n-1")
    x, y = scalar_mult(d, G)
    return PrivateKey(d, PublicKey(x, y))


def generate_key(randbelow: Callable[[int], int] = secrets.randbelow) -> PrivateKey:
    return private_key_from_int(randbelow(N - 1) + 1)


def bits_to_int(data: bytes) -> int:
    value = int.from_bytes(data, "big")
    excess = len(data) * 8 - N.bit_length()
    return value >> excess if excess > 0 else value


def rfc6979_nonces(d: int, digest: bytes) -> Iterator[int]:
    x = d.to_bytes(32, "big")
    h1 = (bits_to_int(digest) % N).to_bytes(32, "big")
    k = b"\x00" * 32
    v = b"\x01" * 32
    for separator in (b"\x00", b"\x01"):
        k = hmac.new(k, v + separator + x + h1, hashlib.sha256).digest()
        v = hmac.new(k, v, hashlib.sha256).digest()
    while True:
        v = hmac.new(k, v, hashlib.sha256).digest()
        candidate = bits_to_int(v)
        if 1 <= candidate < N:
            yield candidate
        k = hmac.new(k, v + b"\x00", hashlib.sha256).digest()
        v = hmac.new(k, v, hashlib.sha256).digest()


def sign_digest(key: PrivateKey, digest: bytes) -> Tuple[int, int]:
    z = bits_to_int(digest)
    for k in rfc6979_nonces(key.d, digest):
        x, _ = scalar_mult(k, G)
        r = x % N
        s = pow(k, -1, N) * (z + r * key.d) % N
        if r and s:
            return r, s
    raise AssertionError("the nonce generator never ends")


def verify_digest(public: PublicKey, digest: bytes, r: int, s: int) -> bool:
    if not (0 < r < N and 0 < s < N) or not on_curve(public.point):
        return False
    w = pow(s, -1, N)
    point = point_add(scalar_mult(bits_to_int(digest) * w % N, G), scalar_mult(r * w % N, public.point))
    return point is not None and point[0] % N == r


def sign(key: PrivateKey, message: bytes) -> bytes:
    return der_signature(*sign_digest(key, sha256(message)))


def verify(public: PublicKey, message: bytes, signature: bytes) -> bool:
    try:
        r, s = parse_der_signature(signature)
    except InputError:
        return False
    return verify_digest(public, sha256(message), r, s)


def der_length(length: int) -> bytes:
    if length < 0x80:
        return bytes([length])
    body = length.to_bytes((length.bit_length() + 7) // 8, "big")
    return bytes([0x80 | len(body)]) + body


def der_tlv(tag: int, body: bytes) -> bytes:
    return bytes([tag]) + der_length(len(body)) + body


def der_integer(value: int) -> bytes:
    return der_tlv(0x02, value.to_bytes(value.bit_length() // 8 + 1, "big"))


def der_read(data: bytes, offset: int = 0) -> Tuple[int, bytes, int]:
    if offset + 2 > len(data):
        raise InputError("DER: truncated header")
    tag, length = data[offset], data[offset + 1]
    offset += 2
    if length & 0x80:
        count = length & 0x7F
        if not 1 <= count <= 4 or offset + count > len(data) or data[offset] == 0:
            raise InputError("DER: bad length encoding")
        length = int.from_bytes(data[offset:offset + count], "big")
        if length < 0x80:
            raise InputError("DER: long-form length used for a short value")
        offset += count
    if offset + length > len(data):
        raise InputError("DER: value runs past the end of the input")
    return tag, data[offset:offset + length], offset + length


def der_children(body: bytes) -> List[Tuple[int, bytes]]:
    items = []
    offset = 0
    while offset < len(body):
        tag, value, offset = der_read(body, offset)
        items.append((tag, value))
    return items


def der_single(data: bytes, tag: int, what: str) -> bytes:
    found, body, end = der_read(data)
    if found != tag or end != len(data):
        raise InputError(f"{what}: expected exactly one DER element with tag 0x{tag:02x}")
    return body


def der_parse_integer(value: bytes) -> int:
    if not value or value[0] & 0x80:
        raise InputError("DER: INTEGER is empty or negative")
    if len(value) > 1 and value[0] == 0 and not value[1] & 0x80:
        raise InputError("DER: INTEGER has a superfluous leading zero")
    return int.from_bytes(value, "big")


def der_signature(r: int, s: int) -> bytes:
    return der_tlv(0x30, der_integer(r) + der_integer(s))


def parse_der_signature(signature: bytes) -> Tuple[int, int]:
    items = der_children(der_single(signature, 0x30, "signature"))
    if [tag for tag, _ in items] != [0x02, 0x02]:
        raise InputError("signature: want SEQUENCE { INTEGER r, INTEGER s }")
    return der_parse_integer(items[0][1]), der_parse_integer(items[1][1])


def oid_der(text: str) -> bytes:
    parts = [int(part) for part in text.split(".")]
    body = b""
    for value in [parts[0] * 40 + parts[1]] + parts[2:]:
        chunk = [value & 0x7F]
        value >>= 7
        while value:
            chunk.append(0x80 | (value & 0x7F))
            value >>= 7
        body += bytes(reversed(chunk))
    return der_tlv(0x06, body)


def oid_text(body: bytes) -> str:
    subids = []
    value = 0
    for byte in body:
        value = (value << 7) | (byte & 0x7F)
        if not byte & 0x80:
            subids.append(value)
            value = 0
    if not subids or value:
        raise InputError("DER: malformed OBJECT IDENTIFIER")
    first = min(subids[0] // 40, 2)
    return ".".join(str(part) for part in [first, subids[0] - 40 * first] + subids[1:])


def describe_oid(oid: str) -> str:
    return f"{oid} ({OID_NAMES.get(oid, 'unknown')})"


def encode_point(public: PublicKey) -> bytes:
    return b"\x04" + public.x.to_bytes(32, "big") + public.y.to_bytes(32, "big")


def decode_point(data: bytes) -> PublicKey:
    if len(data) == 65 and data[0] == 4:
        x, y = int.from_bytes(data[1:33], "big"), int.from_bytes(data[33:], "big")
    elif len(data) == 33 and data[0] in (2, 3):
        x = int.from_bytes(data[1:], "big")
        y = pow((x * x * x + A * x + B) % P, (P + 1) // 4, P)
        if y & 1 != data[0] & 1:
            y = P - y
    else:
        raise InputError(f"point: want 65 bytes starting 04 or 33 bytes starting 02/03, got {len(data)} bytes")
    if not on_curve((x, y)):
        raise InputError("point: not on the P-256 curve")
    return PublicKey(x, y)


def algorithm_identifier() -> bytes:
    return der_tlv(0x30, oid_der(OID_EC_PUBLIC_KEY) + oid_der(OID_P256))


def check_algorithm(body: bytes) -> None:
    items = der_children(body)
    if not items or items[0][0] != 0x06:
        raise InputError("key: AlgorithmIdentifier without an OID")
    algorithm = oid_text(items[0][1])
    if algorithm != OID_EC_PUBLIC_KEY:
        raise InputError(f"key: algorithm {describe_oid(algorithm)} is not an EC key; minisign speaks ECDSA P-256 only")
    curve = oid_text(items[1][1]) if len(items) > 1 and items[1][0] == 0x06 else "missing"
    if curve != OID_P256:
        raise InputError(f"key: curve {describe_oid(curve)} is not P-256 ({OID_P256})")


def spki_der(public: PublicKey) -> bytes:
    return der_tlv(0x30, algorithm_identifier() + der_tlv(0x03, b"\x00" + encode_point(public)))


def parse_spki(der: bytes) -> PublicKey:
    items = der_children(der_single(der, 0x30, "SubjectPublicKeyInfo"))
    if len(items) != 2 or items[0][0] != 0x30 or items[1][0] != 0x03:
        raise InputError("key: want SEQUENCE { AlgorithmIdentifier, BIT STRING }")
    check_algorithm(items[0][1])
    bits = items[1][1]
    if not bits or bits[0] != 0:
        raise InputError("key: BIT STRING with unused bits")
    return decode_point(bits[1:])


def sec1_der(key: PrivateKey) -> bytes:
    public = der_tlv(0xA1, der_tlv(0x03, b"\x00" + encode_point(key.public)))
    return der_tlv(0x30, der_integer(1) + der_tlv(0x04, key.d.to_bytes(32, "big")) + public)


def pkcs8_der(key: PrivateKey) -> bytes:
    return der_tlv(0x30, der_integer(0) + algorithm_identifier() + der_tlv(0x04, sec1_der(key)))


def parse_sec1(der: bytes) -> PrivateKey:
    items = der_children(der_single(der, 0x30, "ECPrivateKey"))
    if len(items) < 2 or items[0] != (0x02, b"\x01") or items[1][0] != 0x04:
        raise InputError("key: not an RFC 5915 ECPrivateKey")
    key = private_key_from_int(int.from_bytes(items[1][1], "big"))
    for tag, value in items[2:]:
        if tag == 0xA0:
            curve = oid_text(der_single(value, 0x06, "ECPrivateKey parameters"))
            if curve != OID_P256:
                raise InputError(f"key: curve {describe_oid(curve)} is not P-256 ({OID_P256})")
        if tag == 0xA1:
            bits = der_single(value, 0x03, "ECPrivateKey publicKey")
            if decode_point(bits[1:]) != key.public:
                raise InputError("key: the embedded public key does not belong to the private scalar")
    return key


def parse_pkcs8(der: bytes) -> PrivateKey:
    items = der_children(der_single(der, 0x30, "PrivateKeyInfo"))
    if len(items) < 3 or items[0][0] != 0x02 or items[1][0] != 0x30 or items[2][0] != 0x04:
        raise InputError("key: not a PKCS#8 PrivateKeyInfo")
    check_algorithm(items[1][1])
    return parse_sec1(items[2][1])


PEM_BLOCK = re.compile(r"-----BEGIN ([A-Z0-9 ]+)-----(.*?)-----END \1-----", re.S)


def pem_encode(label: str, der: bytes) -> str:
    text = b64(der)
    lines = [text[i:i + 64] for i in range(0, len(text), 64)]
    return f"-----BEGIN {label}-----\n" + "\n".join(lines) + f"\n-----END {label}-----\n"


def pem_blocks(text: str) -> List[Tuple[str, bytes]]:
    return [(match.group(1), b64decode(match.group(2), f"PEM {match.group(1)}")) for match in PEM_BLOCK.finditer(text)]


def load_private_key(text: str) -> PrivateKey:
    for label, der in pem_blocks(text):
        if label == "PRIVATE KEY":
            return parse_pkcs8(der)
        if label == "EC PRIVATE KEY":
            return parse_sec1(der)
        if label == "ENCRYPTED SIGSTORE PRIVATE KEY":
            raise InputError("this is a cosign key (scrypt and secretbox encrypted); minisign does not decrypt it. Sign with cosign, or create a key with `minisign keygen`")
        if label == "ENCRYPTED PRIVATE KEY":
            raise InputError("encrypted PKCS#8 key: decrypt it first with `openssl pkey -in KEY -out plain.pem`")
    raise InputError("no PRIVATE KEY or EC PRIVATE KEY PEM block found")


def load_public_key(text: str) -> PublicKey:
    blocks = pem_blocks(text)
    for label, der in blocks:
        if label == "PUBLIC KEY":
            return parse_spki(der)
        if label == "CERTIFICATE":
            return parse_certificate(der).public_key
        if label in ("PRIVATE KEY", "EC PRIVATE KEY"):
            return load_private_key(text).public
    labels = ", ".join(label for label, _ in blocks) or "none"
    raise InputError(f"no PUBLIC KEY PEM block found (blocks: {labels})")


@dataclass
class Certificate:
    public_key: PublicKey
    subject_alt_names: List[str]
    issuer: str
    not_before: str
    not_after: str


def parse_certificate(der: bytes) -> Certificate:
    parts = der_children(der_single(der, 0x30, "certificate"))
    if not parts or parts[0][0] != 0x30:
        raise InputError("certificate: no TBSCertificate")
    tbs = der_children(parts[0][1])
    fields = [item for item in tbs if item[0] != 0xA0]
    if len(fields) < 6:
        raise InputError("certificate: TBSCertificate is too short")
    names: List[str] = []
    issuer = ""
    for tag, value in tbs:
        if tag != 0xA3:
            continue
        for _, extension in der_children(der_single(value, 0x30, "extensions")):
            items = der_children(extension)
            oid = oid_text(items[0][1])
            content = items[-1][1]
            if oid == OID_SUBJECT_ALT_NAME:
                names += [name.decode("utf-8", "replace") for kind, name in der_children(der_single(content, 0x30, "subjectAltName")) if kind in (0x81, 0x82, 0x86)]
            elif oid == OID_FULCIO_ISSUER_V2:
                issuer = der_read(content)[1].decode("utf-8", "replace")
            elif oid == OID_FULCIO_ISSUER_V1 and not issuer:
                issuer = content.decode("utf-8", "replace")
    validity = der_children(fields[3][1])
    return Certificate(parse_spki(der_tlv(0x30, fields[5][1])), names, issuer, validity[0][1].decode(), validity[1][1].decode())


@dataclass
class Bundle:
    media_type: str
    hint: str = ""
    certificate: Optional[Certificate] = None
    digest: Optional[bytes] = None
    signature: bytes = b""
    envelope: Optional[Dict[str, Any]] = None
    tlog_entries: List[Dict[str, Any]] = field(default_factory=list)


def message_bundle(public: PublicKey, signature: bytes, digest: bytes) -> Dict[str, Any]:
    return {
        "mediaType": BUNDLE_V03,
        "verificationMaterial": {"publicKey": {"hint": b64(public.log_id)}},
        "messageSignature": {"messageDigest": {"algorithm": "SHA2_256", "digest": b64(digest)}, "signature": b64(signature)},
    }


def dsse_bundle(public: PublicKey, envelope: Dict[str, Any]) -> Dict[str, Any]:
    return {"mediaType": BUNDLE_V03, "verificationMaterial": {"publicKey": {"hint": b64(public.log_id)}}, "dsseEnvelope": envelope}


def parse_bundle(data: Any) -> Bundle:
    if not isinstance(data, dict):
        raise InputError("bundle: not a JSON object")
    if "base64Signature" in data:
        return Bundle("cosign legacy bundle", signature=b64decode(data["base64Signature"], "base64Signature"))
    if "payloadType" in data and "signatures" in data:
        return Bundle("DSSE envelope", envelope=data)
    media = data.get("mediaType", "")
    if media not in BUNDLE_MEDIA_TYPES:
        raise InputError(f"bundle: unknown mediaType {media!r}; expected one of: {', '.join(BUNDLE_MEDIA_TYPES)}")
    material = data.get("verificationMaterial") or {}
    bundle = Bundle(media, tlog_entries=list(material.get("tlogEntries") or []))
    if material.get("publicKey") is not None:
        bundle.hint = material["publicKey"].get("hint", "")
    elif material.get("certificate"):
        bundle.certificate = parse_certificate(b64decode(material["certificate"].get("rawBytes"), "certificate"))
    elif material.get("x509CertificateChain"):
        leaf = (material["x509CertificateChain"].get("certificates") or [{}])[0]
        bundle.certificate = parse_certificate(b64decode(leaf.get("rawBytes"), "certificate"))
    else:
        raise InputError("bundle: verificationMaterial has no publicKey, certificate or x509CertificateChain")
    if data.get("messageSignature"):
        message = data["messageSignature"]
        digest = message.get("messageDigest") or {}
        if digest and digest.get("algorithm") != "SHA2_256":
            raise InputError(f"bundle: digest algorithm {digest.get('algorithm')} is not SHA2_256")
        bundle.digest = b64decode(digest["digest"], "messageDigest") if digest else None
        bundle.signature = b64decode(message.get("signature"), "signature")
    elif data.get("dsseEnvelope"):
        bundle.envelope = data["dsseEnvelope"]
    else:
        raise InputError("bundle: neither messageSignature nor dsseEnvelope")
    return bundle


def read_signature(raw: bytes) -> bytes:
    if raw[:1] == b"\x30":
        try:
            parse_der_signature(raw)
            return raw
        except InputError:
            pass
    return b64decode(raw.decode("ascii", "replace"), "signature")


def pae(payload_type: str, payload: bytes) -> bytes:
    kind = payload_type.encode("utf-8")
    return b"DSSEv1 %d %s %d %s" % (len(kind), kind, len(payload), payload)


def sign_envelope(key: PrivateKey, payload: bytes, payload_type: str = INTOTO_PAYLOAD_TYPE) -> Dict[str, Any]:
    return {"payload": b64(payload), "payloadType": payload_type, "signatures": [{"keyid": "", "sig": b64(sign(key, pae(payload_type, payload)))}]}


def envelope_signer(envelope: Dict[str, Any], keys: Sequence[PublicKey]) -> Optional[PublicKey]:
    payload = b64decode(envelope.get("payload"), "envelope payload")
    message = pae(str(envelope.get("payloadType", "")), payload)
    for signature in envelope.get("signatures") or []:
        raw = b64decode(signature.get("sig"), "envelope signature")
        for key in keys:
            if verify(key, message, raw):
                return key
    return None


def provenance_statement(
    subjects: Sequence[Tuple[str, bytes]],
    builder_id: str,
    build_type: str = BUILD_TYPE,
    external_parameters: Optional[Dict[str, Any]] = None,
    resolved_dependencies: Optional[List[Dict[str, Any]]] = None,
    invocation_id: str = "",
    started_on: str = "",
    finished_on: str = "",
) -> Dict[str, Any]:
    metadata = {key: value for key, value in (("invocationId", invocation_id), ("startedOn", started_on), ("finishedOn", finished_on)) if value}
    run: Dict[str, Any] = {"builder": {"id": builder_id}}
    if metadata:
        run["metadata"] = metadata
    return {
        "_type": STATEMENT_V1,
        "subject": [{"name": name, "digest": {"sha256": digest.hex()}} for name, digest in subjects],
        "predicateType": SLSA_V1,
        "predicate": {
            "buildDefinition": {
                "buildType": build_type,
                "externalParameters": dict(external_parameters or {}),
                "resolvedDependencies": list(resolved_dependencies or []),
            },
            "runDetails": run,
        },
    }


def provenance_facts(statement: Dict[str, Any]) -> Dict[str, Any]:
    predicate = statement.get("predicate") or {}
    if statement.get("predicateType") == SLSA_V02:
        config = ((predicate.get("invocation") or {}).get("configSource") or {}).get("uri", "")
        sources = [m.get("uri", "") for m in predicate.get("materials") or []] + ([config] if config else [])
        return {"builder": (predicate.get("builder") or {}).get("id", ""), "buildType": predicate.get("buildType", ""), "sources": sources}
    definition = predicate.get("buildDefinition") or {}
    run = predicate.get("runDetails") or {}
    sources = [d.get("uri", "") for d in definition.get("resolvedDependencies") or []]
    return {"builder": (run.get("builder") or {}).get("id", ""), "buildType": definition.get("buildType", ""), "sources": sources}


def source_matches(expected: str, uris: Sequence[str]) -> bool:
    return any(uri == expected or uri.rpartition("@")[0] == expected for uri in uris)


@dataclass
class Check:
    name: str
    ok: Optional[bool]
    detail: str


@dataclass
class Report:
    subject: str
    checks: List[Check] = field(default_factory=list)

    def check(self, name: str, ok: bool, detail: str) -> bool:
        self.checks.append(Check(name, bool(ok), detail))
        return bool(ok)

    def skip(self, name: str, detail: str) -> None:
        self.checks.append(Check(name, None, detail))

    @property
    def ok(self) -> bool:
        return any(c.ok for c in self.checks) and all(c.ok is not False for c in self.checks)

    @property
    def failures(self) -> List[Check]:
        return [c for c in self.checks if c.ok is False]


@dataclass
class Policy:
    builder_id: str
    predicate_type: str = SLSA_V1
    build_type: str = ""
    source: str = ""
    signers: List[str] = field(default_factory=list)


@dataclass
class VerifyOptions:
    identity: str = ""
    issuer: str = ""
    ignore_chain: bool = False
    log_key: Optional[PublicKey] = None
    tlog_entry: Optional[Dict[str, Any]] = None


def candidate_keys(bundle: Bundle, keys: Sequence[PublicKey], options: VerifyOptions, report: Report) -> List[PublicKey]:
    if keys:
        return list(keys)
    if bundle.certificate is None:
        raise InputError("no trusted key: pass --key PUB; a bundle carries only a hint, because a key that travels with the signature proves nothing")
    cert = bundle.certificate
    if not options.identity or not options.issuer:
        raise InputError("keyless bundle: pass --certificate-identity and --certificate-oidc-issuer, the signer you expect")
    report.check("identity", options.identity in cert.subject_alt_names, f"certificate SAN {', '.join(cert.subject_alt_names) or '(none)'}; want {options.identity}")
    report.check("issuer", cert.issuer == options.issuer, f"certificate OIDC issuer {cert.issuer or '(none)'}; want {options.issuer}")
    if options.ignore_chain:
        report.skip("certificate-chain", "NOT verified (--insecure-ignore-chain): nothing here proves Fulcio issued this certificate")
    else:
        report.check("certificate-chain", False, "minisign does not verify X.509 chains; verify with cosign and the Sigstore trusted root, or pass --insecure-ignore-chain to see the other checks")
    return [cert.public_key]


def verify_blob(artifact: bytes, bundle: Bundle, keys: Sequence[PublicKey], options: VerifyOptions) -> Report:
    if bundle.envelope is not None:
        raise InputError("this bundle holds a DSSE envelope (an attestation): use verify-attestation")
    digest = sha256(artifact)
    report = Report(f"blob sha256:{digest.hex()}")
    candidates = candidate_keys(bundle, keys, options, report)
    signer = next((key for key in candidates if verify(key, artifact, bundle.signature)), None)
    report.check("signature", signer is not None, f"ECDSA P-256 SHA-256 signature verifies with {signer.fingerprint}" if signer else f"no trusted key verifies this signature over this artifact ({len(candidates)} tried)")
    if bundle.digest is None:
        report.skip("message-digest", "detached signature: no digest to compare")
    else:
        same = hmac.compare_digest(bundle.digest, digest)
        report.check("message-digest", same, "bundle digest equals sha256(artifact)" if same else f"bundle says sha256:{bundle.digest.hex()}, artifact is sha256:{digest.hex()}")
    check_tlog(report, bundle, options)
    return report


def verify_attestation(bundle: Bundle, keys: Sequence[PublicKey], artifact_digest: bytes, policy: Policy, options: VerifyOptions) -> Tuple[Report, Optional[Dict[str, Any]]]:
    envelope = bundle.envelope
    if envelope is None:
        raise InputError("this bundle holds a message signature, not a DSSE envelope: use verify-blob")
    report = Report(f"attestation for sha256:{artifact_digest.hex()}")
    candidates = candidate_keys(bundle, keys, options, report)
    signer = envelope_signer(envelope, candidates)
    if not report.check("envelope-signature", signer is not None, f"DSSE signature over PAE(payloadType, payload) verifies with {signer.fingerprint}" if signer else "no trusted key verifies the DSSE signature; the payload was not read"):
        return report, None
    if policy.signers:
        report.check("signer", signer.fingerprint in policy.signers, f"{signer.fingerprint} {'is' if signer.fingerprint in policy.signers else 'is not'} in the policy's signers")
    report.check("payload-type", envelope.get("payloadType") == INTOTO_PAYLOAD_TYPE, f"payloadType {envelope.get('payloadType')!r}")
    try:
        statement = json.loads(b64decode(envelope.get("payload"), "payload"))
    except ValueError:
        report.check("statement", False, "the signed payload is not JSON")
        return report, None
    report.check("statement", statement.get("_type") in STATEMENT_TYPES, f"_type {statement.get('_type')!r}")
    report.check("predicate-type", statement.get("predicateType") == policy.predicate_type, f"predicateType {statement.get('predicateType')!r}; want {policy.predicate_type}")
    names = [s.get("name", "") for s in statement.get("subject") or [] if str((s.get("digest") or {}).get("sha256", "")).lower() == artifact_digest.hex()]
    report.check("subject", bool(names), f"subject {names[0]!r} has sha256:{artifact_digest.hex()}" if names else f"no subject has sha256:{artifact_digest.hex()}; this provenance describes a different artifact")
    facts = provenance_facts(statement)
    report.check("builder", facts["builder"] == policy.builder_id, f"builder.id {facts['builder'] or '(none)'}; want {policy.builder_id}")
    if policy.build_type:
        report.check("build-type", facts["buildType"] == policy.build_type, f"buildType {facts['buildType'] or '(none)'}; want {policy.build_type}")
    if policy.source:
        report.check("source", source_matches(policy.source, facts["sources"]), f"sources {', '.join(facts['sources']) or '(none)'}; want {policy.source}")
    check_tlog(report, bundle, options)
    return report, statement


def check_tlog(report: Report, bundle: Bundle, options: VerifyOptions) -> None:
    if options.log_key is None:
        report.skip("tlog", "not checked (no --log-key); like cosign --insecure-ignore-tlog, nothing shows the signature was ever published")
        return
    entry = options.tlog_entry or (bundle.tlog_entries[0] if bundle.tlog_entries else None)
    if entry is None:
        report.check("tlog", False, "no transparency log entry in the bundle and no --tlog-entry")
        return
    report.checks.extend(verify_tlog_entry(entry, options.log_key, bundle))


def leaf_hash(entry: bytes) -> bytes:
    return sha256(b"\x00" + entry)


def node_hash(left: bytes, right: bytes) -> bytes:
    return sha256(b"\x01" + left + right)


def split_point(size: int) -> int:
    k = 1
    while k << 1 < size:
        k <<= 1
    return k


def merkle_root(leaves: Sequence[bytes]) -> bytes:
    if not leaves:
        return sha256(b"")
    if len(leaves) == 1:
        return leaves[0]
    k = split_point(len(leaves))
    return node_hash(merkle_root(leaves[:k]), merkle_root(leaves[k:]))


def inclusion_path(leaves: Sequence[bytes], index: int) -> List[bytes]:
    if len(leaves) <= 1:
        return []
    k = split_point(len(leaves))
    if index < k:
        return inclusion_path(leaves[:k], index) + [merkle_root(leaves[k:])]
    return inclusion_path(leaves[k:], index - k) + [merkle_root(leaves[:k])]


def consistency_path(leaves: Sequence[bytes], old_size: int) -> List[bytes]:
    def subproof(m: int, part: Sequence[bytes], complete: bool) -> List[bytes]:
        if m == len(part):
            return [] if complete else [merkle_root(part)]
        k = split_point(len(part))
        if m <= k:
            return subproof(m, part[:k], complete) + [merkle_root(part[k:])]
        return subproof(m - k, part[k:], False) + [merkle_root(part[:k])]

    if not 0 < old_size < len(leaves):
        return []
    return subproof(old_size, leaves, True)


def root_from_inclusion(leaf: bytes, index: int, size: int, proof: Sequence[bytes]) -> Optional[bytes]:
    if not 0 <= index < size:
        return None
    fn, sn, root = index, size - 1, leaf
    for sibling in proof:
        if sn == 0:
            return None
        if fn & 1 or fn == sn:
            root = node_hash(sibling, root)
            while fn and not fn & 1:
                fn, sn = fn >> 1, sn >> 1
        else:
            root = node_hash(root, sibling)
        fn, sn = fn >> 1, sn >> 1
    return root if sn == 0 else None


def verify_inclusion(leaf: bytes, index: int, size: int, proof: Sequence[bytes], root: bytes) -> bool:
    computed = root_from_inclusion(leaf, index, size, proof)
    return computed is not None and hmac.compare_digest(computed, root)


def verify_consistency(old_size: int, new_size: int, proof: Sequence[bytes], old_root: bytes, new_root: bytes) -> bool:
    if not 0 <= old_size <= new_size:
        return False
    if old_size == new_size:
        return not proof and hmac.compare_digest(old_root, new_root)
    if old_size == 0:
        return not proof
    if not proof:
        return False
    path = list(proof)
    if old_size & (old_size - 1) == 0:
        path.insert(0, old_root)
    fn, sn = old_size - 1, new_size - 1
    while fn & 1:
        fn, sn = fn >> 1, sn >> 1
    first = second = path[0]
    for sibling in path[1:]:
        if sn == 0:
            return False
        if fn & 1 or fn == sn:
            first, second = node_hash(sibling, first), node_hash(sibling, second)
            while fn and not fn & 1:
                fn, sn = fn >> 1, sn >> 1
        else:
            second = node_hash(second, sibling)
        fn, sn = fn >> 1, sn >> 1
    return sn == 0 and hmac.compare_digest(first, old_root) and hmac.compare_digest(second, new_root)


@dataclass
class Checkpoint:
    origin: str
    size: int
    root: bytes
    body: str
    signatures: List[Tuple[str, bytes, bytes]]


def checkpoint_body(origin: str, size: int, root: bytes) -> str:
    return f"{origin}\n{size}\n{b64(root)}\n"


def sign_checkpoint(key: PrivateKey, origin: str, size: int, root: bytes) -> str:
    body = checkpoint_body(origin, size, root)
    signature = key.public.log_id[:4] + sign(key, body.encode("utf-8"))
    return f"{body}\n{NOTE_SIGNATURE_PREFIX}{origin} {b64(signature)}\n"


def parse_checkpoint(text: str) -> Checkpoint:
    body, separator, signature_block = text.partition("\n\n")
    lines = body.split("\n")
    if not separator or len(lines) < 3:
        raise InputError("checkpoint: want origin, tree size and root hash lines, a blank line, then signature lines")
    if not lines[1].isdigit():
        raise InputError(f"checkpoint: tree size {lines[1]!r} is not a number")
    root = b64decode(lines[2], "checkpoint root hash")
    if len(root) != 32:
        raise InputError("checkpoint: the root hash is not 32 bytes")
    signatures = []
    for line in signature_block.split("\n"):
        if not line:
            continue
        if not line.startswith(NOTE_SIGNATURE_PREFIX):
            raise InputError(f"checkpoint: signature lines start with U+2014 and a space: {line!r}")
        name, _, value = line[len(NOTE_SIGNATURE_PREFIX):].rpartition(" ")
        raw = b64decode(value, "checkpoint signature")
        signatures.append((name, raw[:4], raw[4:]))
    return Checkpoint(lines[0], int(lines[1]), root, body + "\n", signatures)


def verify_checkpoint(checkpoint: Checkpoint, log_key: PublicKey) -> bool:
    hint = log_key.log_id[:4]
    message = checkpoint.body.encode("utf-8")
    return any(found == hint and verify(log_key, message, signature) for _, found, signature in checkpoint.signatures)


def promise_payload(body_b64: str, integrated_time: int, log_id: bytes, index: int) -> bytes:
    return canonical_json({"body": body_b64, "integratedTime": integrated_time, "logID": log_id.hex(), "logIndex": index})


def hashedrekord_body(digest: bytes, signature: bytes, public: PublicKey) -> bytes:
    return canonical_json({
        "apiVersion": "0.0.1",
        "kind": "hashedrekord",
        "spec": {
            "data": {"hash": {"algorithm": "sha256", "value": digest.hex()}},
            "signature": {"content": b64(signature), "publicKey": {"content": b64(public.pem().encode())}},
        },
    })


def dsse_body(envelope: Dict[str, Any], public: PublicKey) -> bytes:
    payload = b64decode(envelope.get("payload"), "envelope payload")
    return canonical_json({
        "apiVersion": "0.0.1",
        "kind": "dsse",
        "spec": {
            "envelopeHash": {"algorithm": "sha256", "value": sha256(canonical_json(envelope)).hex()},
            "payloadHash": {"algorithm": "sha256", "value": sha256(payload).hex()},
            "signatures": [{"signature": s.get("sig", ""), "verifier": b64(public.pem().encode())} for s in envelope.get("signatures") or []],
        },
    })


def entry_matches_bundle(body: bytes, bundle: Bundle) -> Tuple[bool, str]:
    try:
        entry = json.loads(body)
    except ValueError:
        return False, "the log entry body is not JSON"
    kind, spec = entry.get("kind"), entry.get("spec") or {}
    if kind == "hashedrekord":
        logged_digest = ((spec.get("data") or {}).get("hash") or {}).get("value", "")
        logged_signature = b64decode(((spec.get("signature") or {}).get("content")) or "", "logged signature")
        ok = bundle.digest is not None and logged_digest == bundle.digest.hex() and logged_signature == bundle.signature
        return ok, "the logged hashedrekord holds this digest and signature" if ok else f"the log entry records digest {logged_digest[:16]}... and another signature: it is not this bundle's entry"
    if kind == "dsse" and bundle.envelope is not None:
        payload_hash = ((spec.get("payloadHash") or {}).get("value")) or ""
        logged = {b64decode(s.get("signature", ""), "logged signature") for s in spec.get("signatures") or []}
        mine = {b64decode(s.get("sig", ""), "envelope signature") for s in bundle.envelope.get("signatures") or []}
        ok = payload_hash == sha256(b64decode(bundle.envelope.get("payload"), "payload")).hex() and bool(mine) and mine <= logged
        return ok, "the logged dsse entry holds this payload hash and signature" if ok else "the log entry records another payload or signature"
    return False, f"entry kind {kind!r} cannot be matched to this bundle (minisign knows hashedrekord and dsse)"


def verify_tlog_entry(entry: Dict[str, Any], log_key: PublicKey, bundle: Optional[Bundle] = None) -> List[Check]:
    checks = []
    body = b64decode(entry.get("canonicalizedBody"), "canonicalizedBody")
    log_id = b64decode(((entry.get("logId") or {}).get("keyId")) or "", "logId.keyId")
    checks.append(Check("tlog-log-id", log_id == log_key.log_id, f"entry logId {b64(log_id)[:12]}...; the trusted log key's id is {b64(log_key.log_id)[:12]}..."))
    promise = (entry.get("inclusionPromise") or {}).get("signedEntryTimestamp")
    if promise:
        payload = promise_payload(entry["canonicalizedBody"], int(entry.get("integratedTime", 0)), log_id, int(entry.get("logIndex", 0)))
        promised = verify(log_key, payload, b64decode(promise, "signedEntryTimestamp"))
        checks.append(Check("tlog-promise", promised, f"signed entry timestamp: the log promised entry {entry.get('logIndex')} at integratedTime {entry.get('integratedTime')}" if promised else "the signed entry timestamp does not verify with the log key"))
    proof = entry.get("inclusionProof")
    if not proof:
        checks.append(Check("tlog-inclusion", False, "no inclusionProof: a promise says the log will include the entry; only a proof against a signed checkpoint shows it did"))
    else:
        index, size = int(proof.get("logIndex", -1)), int(proof.get("treeSize", 0))
        root = b64decode(proof.get("rootHash"), "rootHash")
        hashes = [b64decode(h, "proof hash") for h in proof.get("hashes") or []]
        computed = root_from_inclusion(leaf_hash(body), index, size, hashes)
        included = computed is not None and hmac.compare_digest(computed, root)
        checks.append(Check("tlog-inclusion", included, f"leaf {index} of {size}: {len(hashes)} sibling hashes lead to the proof's root {b64(root)[:12]}..." if included else f"leaf {index} of {size}: the {len(hashes)} hashes lead to {b64(computed)[:12] + '...' if computed else 'nothing (wrong shape)'}, not {b64(root)[:12]}..."))
        checkpoint = parse_checkpoint(((proof.get("checkpoint") or {}).get("envelope")) or "")
        signed = verify_checkpoint(checkpoint, log_key)
        matches = checkpoint.size == size and hmac.compare_digest(checkpoint.root, root)
        detail = f"checkpoint {checkpoint.origin!r} size {checkpoint.size} is signed by the log key"
        if not signed:
            detail = "the checkpoint is not signed by the trusted log key"
        elif not matches:
            detail = f"the signed checkpoint says size {checkpoint.size} root {b64(checkpoint.root)[:12]}..., the proof says size {size} root {b64(root)[:12]}..."
        checks.append(Check("tlog-checkpoint", signed and matches, detail))
    if bundle is not None:
        ok, detail = entry_matches_bundle(body, bundle)
        checks.append(Check("tlog-entry-matches", ok, detail))
    return checks


class TransparencyLog:
    def __init__(self, directory: Any):
        self.directory = Path(directory)
        self.entries_dir = self.directory / "entries"
        self.checkpoints_dir = self.directory / "checkpoints"
        self.state_path = self.directory / "log.json"

    def create(self, key: PrivateKey, origin: str) -> str:
        if self.state_path.exists():
            raise InputError(f"{self.directory} already holds a log")
        self.entries_dir.mkdir(parents=True, exist_ok=True)
        self.checkpoints_dir.mkdir(parents=True, exist_ok=True)
        self.save_state({"origin": origin, "logId": b64(key.public.log_id), "integratedTimes": [], "promises": []})
        return self.write_checkpoint(key, 0)

    def state(self) -> Dict[str, Any]:
        if not self.state_path.exists():
            raise InputError(f"{self.directory}: no log here; run `minisign log init` first")
        return json.loads(self.state_path.read_text())

    def save_state(self, state: Dict[str, Any]) -> None:
        self.state_path.write_text(json.dumps(state, indent=2) + "\n")

    def entry_path(self, index: int) -> Path:
        return self.entries_dir / f"{index:08d}.json"

    def size(self) -> int:
        return len(self.state()["integratedTimes"])

    def leaves(self) -> List[bytes]:
        return [leaf_hash(self.entry_path(i).read_bytes()) for i in range(self.size())]

    def append(self, key: PrivateKey, body: bytes, now: int) -> int:
        state = self.state()
        if b64(key.public.log_id) != state["logId"]:
            raise InputError("this is not the key the log was created with")
        index = len(state["integratedTimes"])
        self.entry_path(index).write_bytes(body)
        state["integratedTimes"].append(now)
        state["promises"].append(b64(sign(key, promise_payload(b64(body), now, key.public.log_id, index))))
        self.save_state(state)
        self.write_checkpoint(key, index + 1)
        return index

    def write_checkpoint(self, key: PrivateKey, size: int) -> str:
        text = sign_checkpoint(key, self.state()["origin"], size, merkle_root(self.leaves()[:size]))
        (self.checkpoints_dir / f"{size}.txt").write_text(text)
        return text

    def checkpoint(self, size: int) -> str:
        path = self.checkpoints_dir / f"{size}.txt"
        if not path.exists():
            raise InputError(f"{self.directory}: no checkpoint for tree size {size}")
        return path.read_text()

    def prove(self, index: int, size: Optional[int] = None) -> Dict[str, Any]:
        state = self.state()
        total = len(state["integratedTimes"])
        size = total if size is None else size
        if not 0 <= index < size <= total:
            raise InputError(f"entry {index} is not in a tree of size {size} (the log holds {total} entries)")
        leaves = self.leaves()[:size]
        body = self.entry_path(index).read_bytes()
        return {
            "logIndex": str(index),
            "logId": {"keyId": state["logId"]},
            "kindVersion": {"kind": json.loads(body).get("kind", ""), "version": "0.0.1"},
            "integratedTime": str(state["integratedTimes"][index]),
            "inclusionPromise": {"signedEntryTimestamp": state["promises"][index]},
            "inclusionProof": {
                "logIndex": str(index),
                "rootHash": b64(merkle_root(leaves)),
                "treeSize": str(size),
                "hashes": [b64(h) for h in inclusion_path(leaves, index)],
                "checkpoint": {"envelope": self.checkpoint(size)},
            },
            "canonicalizedBody": b64(body),
        }

    def consistency(self, old_size: int, new_size: Optional[int] = None) -> Dict[str, Any]:
        total = self.size()
        new_size = total if new_size is None else new_size
        if not 0 <= old_size <= new_size <= total:
            raise InputError(f"need 0 <= old <= new <= {total}, got old {old_size} new {new_size}")
        leaves = self.leaves()[:new_size]
        return {
            "oldSize": str(old_size),
            "newSize": str(new_size),
            "hashes": [b64(h) for h in consistency_path(leaves, old_size)],
            "oldCheckpoint": self.checkpoint(old_size),
            "newCheckpoint": self.checkpoint(new_size),
        }


def verify_consistency_proof(proof: Dict[str, Any], log_key: PublicKey, trusted: str = "") -> Report:
    old = parse_checkpoint(trusted or proof.get("oldCheckpoint", ""))
    new = parse_checkpoint(proof.get("newCheckpoint", ""))
    report = Report(f"consistency {old.size} -> {new.size}")
    report.check("old-checkpoint", verify_checkpoint(old, log_key), f"size {old.size} {'signed' if verify_checkpoint(old, log_key) else 'NOT signed'} by the log key{' (the one you remembered)' if trusted else ''}")
    report.check("new-checkpoint", verify_checkpoint(new, log_key), f"size {new.size} {'signed' if verify_checkpoint(new, log_key) else 'NOT signed'} by the log key")
    hashes = [b64decode(h, "consistency hash") for h in proof.get("hashes") or []]
    sizes = int(proof.get("oldSize", -1)) == old.size and int(proof.get("newSize", -1)) == new.size
    consistent = sizes and verify_consistency(old.size, new.size, hashes, old.root, new.root)
    report.check("consistency", consistent, f"tree {old.size} is a prefix of tree {new.size} ({len(hashes)} hashes)" if consistent else f"tree {old.size} is NOT a prefix of tree {new.size}: the log rewrote or forked its history")
    return report


def audit_log(log: TransparencyLog, log_key: PublicKey) -> Report:
    leaves = log.leaves()
    report = Report(f"log {log.directory} ({len(leaves)} entries)")
    for size in sorted(int(path.stem) for path in log.checkpoints_dir.glob("*.txt")):
        checkpoint = parse_checkpoint(log.checkpoint(size))
        signed = verify_checkpoint(checkpoint, log_key)
        root = merkle_root(leaves[:size])
        same = checkpoint.size == size and hmac.compare_digest(checkpoint.root, root)
        report.check(f"checkpoint-{size}", signed and same, "signed and matches the stored entries" if signed and same else f"{'signed' if signed else 'NOT signed'}; stored entries hash to {b64(root)[:12]}..., the checkpoint says {b64(checkpoint.root)[:12]}...")
    return report


def report_json(report: Report) -> Dict[str, Any]:
    return {"subject": report.subject, "ok": report.ok, "checks": [{"name": c.name, "ok": c.ok, "detail": c.detail} for c in report.checks]}


def render_report(report: Report) -> str:
    lines = [report.subject]
    for c in report.checks:
        status = "SKIP" if c.ok is None else "PASS" if c.ok else "FAIL"
        lines.append(f"  {status}  {c.name:<20} {c.detail}")
    lines.append("Verified OK" if report.ok else f"Verification FAILED: {len(report.failures)} check(s) failed")
    return "\n".join(lines)


def read_bytes(path: str) -> bytes:
    if path == "-":
        return sys.stdin.buffer.read()
    try:
        return Path(path).read_bytes()
    except OSError as exc:
        raise InputError(f"{path}: {exc.strerror}") from None


def read_text(path: str) -> str:
    try:
        return read_bytes(path).decode("utf-8")
    except UnicodeDecodeError:
        raise InputError(f"{path}: not UTF-8 text") from None


def read_json(path: str) -> Any:
    try:
        return json.loads(read_text(path))
    except json.JSONDecodeError as exc:
        raise InputError(f"{path}: not JSON ({exc.msg} at line {exc.lineno})") from None


def write_file(path: str, data: bytes, private: bool = False) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    if private:
        descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
    else:
        target.write_bytes(data)


def write_json(path: str, value: Any) -> None:
    write_file(path, (json.dumps(value, indent=2) + "\n").encode("utf-8"))


def emit(args: argparse.Namespace, value: Any, text: str) -> None:
    print(json.dumps(value, indent=2) if args.json else text)


def load_keys(paths: Sequence[str]) -> List[PublicKey]:
    return [load_public_key(read_text(path)) for path in paths or []]


def options_from(args: argparse.Namespace) -> VerifyOptions:
    return VerifyOptions(
        identity=getattr(args, "certificate_identity", "") or "",
        issuer=getattr(args, "certificate_oidc_issuer", "") or "",
        ignore_chain=bool(getattr(args, "insecure_ignore_chain", False)),
        log_key=load_public_key(read_text(args.log_key)) if getattr(args, "log_key", None) else None,
        tlog_entry=read_json(args.tlog_entry) if getattr(args, "tlog_entry", None) else None,
    )


def finish(args: argparse.Namespace, report: Report) -> int:
    emit(args, report_json(report), render_report(report))
    return 0 if report.ok else 1


def parse_digest(text: str) -> bytes:
    algorithm, _, value = text.partition(":")
    if algorithm != "sha256" or not re.fullmatch(r"[0-9a-fA-F]{64}", value):
        raise InputError(f"digest {text!r}: want sha256:<64 hex characters>")
    return bytes.fromhex(value)


def parse_pairs(items: Sequence[str], what: str) -> Dict[str, str]:
    pairs = {}
    for item in items or []:
        key, separator, value = item.partition("=")
        if not separator or not key:
            raise InputError(f"{what} {item!r}: want KEY=VALUE")
        pairs[key] = value
    return pairs


def cmd_keygen(args: argparse.Namespace) -> int:
    key_path, pub_path = args.output_key_prefix + ".key", args.output_key_prefix + ".pub"
    for path in (key_path, pub_path):
        if Path(path).exists() and not args.force:
            raise InputError(f"{path} exists; pass --force to overwrite it")
    key = generate_key()
    write_file(key_path, pem_encode("PRIVATE KEY", pkcs8_der(key)).encode(), private=True)
    write_file(pub_path, key.public.pem().encode())
    emit(args, {"privateKey": key_path, "publicKey": pub_path, "fingerprint": key.public.fingerprint}, f"Private key written to {key_path}\nPublic key written to {pub_path}\nfingerprint {key.public.fingerprint}")
    return 0


def cmd_public_key(args: argparse.Namespace) -> int:
    public = load_public_key(read_text(args.key))
    emit(args, {"fingerprint": public.fingerprint, "pem": public.pem(), "x": f"{public.x:064x}", "y": f"{public.y:064x}"}, public.pem().rstrip("\n"))
    return 0


def cmd_sign_blob(args: argparse.Namespace) -> int:
    key = load_private_key(read_text(args.key))
    artifact = read_bytes(args.file)
    signature = sign(key, artifact)
    if args.bundle:
        write_json(args.bundle, message_bundle(key.public, signature, sha256(artifact)))
    if args.output_signature:
        write_file(args.output_signature, b64(signature).encode())
    emit(args, {"signature": b64(signature), "digest": "sha256:" + sha256(artifact).hex(), "bundle": args.bundle or None, "key": key.public.fingerprint}, b64(signature))
    return 0


def cmd_verify_blob(args: argparse.Namespace) -> int:
    if args.bundle:
        bundle = parse_bundle(read_json(args.bundle))
    elif args.signature:
        bundle = Bundle("detached signature", signature=read_signature(read_bytes(args.signature)))
    else:
        raise InputError("pass --bundle FILE or --signature FILE")
    return finish(args, verify_blob(read_bytes(args.file), bundle, load_keys(args.key), options_from(args)))


def cmd_provenance(args: argparse.Namespace) -> int:
    subjects = [(Path(path).name, sha256(read_bytes(path))) for path in args.subject]
    dependencies: List[Dict[str, Any]] = []
    if args.source:
        dependencies.append({"uri": args.source, "digest": {"gitCommit": args.commit} if args.commit else {}})
    for uri, digest in parse_pairs(args.dependency, "--dependency").items():
        dependencies.append({"uri": uri, "digest": {"sha256": parse_digest(digest).hex()}})
    statement = provenance_statement(subjects, args.builder_id, args.build_type, parse_pairs(args.param, "--param"), dependencies, args.invocation_id, args.started_on, args.finished_on)
    text = json.dumps(statement, indent=2) + "\n"
    if args.output:
        write_file(args.output, text.encode())
    else:
        sys.stdout.write(text)
    return 0


def cmd_attest(args: argparse.Namespace) -> int:
    key = load_private_key(read_text(args.key))
    payload = read_bytes(args.statement)
    try:
        statement = json.loads(payload)
    except ValueError:
        raise InputError(f"{args.statement}: the statement is not JSON") from None
    if not isinstance(statement, dict) or statement.get("_type") not in STATEMENT_TYPES or not statement.get("subject"):
        raise InputError(f"{args.statement}: not an in-toto Statement with _type {STATEMENT_V1} and at least one subject")
    envelope = sign_envelope(key, payload)
    if args.bundle:
        write_json(args.bundle, dsse_bundle(key.public, envelope))
    if args.envelope:
        write_json(args.envelope, envelope)
    if not args.bundle and not args.envelope:
        print(json.dumps(envelope, indent=2))
    elif args.json:
        print(json.dumps({"bundle": args.bundle, "envelope": args.envelope, "key": key.public.fingerprint, "predicateType": statement.get("predicateType")}, indent=2))
    return 0


def policy_from(args: argparse.Namespace) -> Policy:
    data = read_json(args.policy) if args.policy else {}
    if not isinstance(data, dict):
        raise InputError(f"{args.policy}: a policy is a JSON object")
    unknown = sorted(set(data) - {"builderId", "predicateType", "buildType", "source", "signers"})
    if unknown:
        raise InputError(f"{args.policy}: unknown field(s) {', '.join(unknown)}; allowed: builderId, predicateType, buildType, source, signers")
    policy = Policy(
        builder_id=args.builder_id or data.get("builderId", ""),
        predicate_type=args.predicate_type or data.get("predicateType", SLSA_V1),
        build_type=args.build_type or data.get("buildType", ""),
        source=args.source or data.get("source", ""),
        signers=list(data.get("signers") or []),
    )
    if not policy.builder_id:
        raise InputError("policy: builderId is required; provenance without a pinned builder accepts a statement anyone with a key can write")
    return policy


def cmd_verify_attestation(args: argparse.Namespace) -> int:
    source = args.bundle or args.envelope
    if not source:
        raise InputError("pass --bundle FILE or --envelope FILE")
    bundle = parse_bundle(read_json(source))
    if args.artifact:
        digest = sha256(read_bytes(args.artifact))
    elif args.digest:
        digest = parse_digest(args.digest)
    else:
        raise InputError("pass --artifact FILE or --digest sha256:HEX: provenance is only meaningful for a specific artifact")
    report, _ = verify_attestation(bundle, load_keys(args.key), digest, policy_from(args), options_from(args))
    return finish(args, report)


def inspect_value(text: str) -> Dict[str, Any]:
    if "-----BEGIN" in text:
        public = load_public_key(text)
        return {"type": "key", "algorithm": "ECDSA P-256", "fingerprint": public.fingerprint, "x": f"{public.x:064x}", "y": f"{public.y:064x}"}
    if NOTE_SIGNATURE_PREFIX in text and not text.lstrip().startswith("{"):
        checkpoint = parse_checkpoint(text)
        return {"type": "checkpoint", "origin": checkpoint.origin, "treeSize": checkpoint.size, "rootHash": b64(checkpoint.root), "signatures": [{"name": n, "keyHint": h.hex()} for n, h, _ in checkpoint.signatures]}
    try:
        data = json.loads(text)
    except ValueError:
        raise InputError("not a PEM key, a checkpoint or JSON") from None
    if isinstance(data, dict) and "canonicalizedBody" in data:
        proof = data.get("inclusionProof") or {}
        return {"type": "tlog entry", "logIndex": data.get("logIndex"), "kind": (data.get("kindVersion") or {}).get("kind"), "body": json.loads(b64decode(data["canonicalizedBody"], "canonicalizedBody")), "treeSize": proof.get("treeSize"), "proofHashes": len(proof.get("hashes") or [])}
    if isinstance(data, dict) and "_type" in data:
        return {"type": "statement", "statement": data, "facts": provenance_facts(data)}
    bundle = parse_bundle(data)
    summary: Dict[str, Any] = {"type": "bundle", "mediaType": bundle.media_type, "tlogEntries": len(bundle.tlog_entries)}
    if bundle.certificate:
        summary["certificate"] = {"subjectAltNames": bundle.certificate.subject_alt_names, "issuer": bundle.certificate.issuer, "notBefore": bundle.certificate.not_before, "notAfter": bundle.certificate.not_after, "keyFingerprint": bundle.certificate.public_key.fingerprint}
    else:
        summary["keyHint"] = bundle.hint
    if bundle.envelope is not None:
        statement = json.loads(b64decode(bundle.envelope.get("payload"), "payload"))
        summary.update({"content": "dsseEnvelope", "payloadType": bundle.envelope.get("payloadType"), "statement": statement, "facts": provenance_facts(statement)})
    else:
        summary.update({"content": "messageSignature", "digest": "sha256:" + bundle.digest.hex() if bundle.digest else None, "signatureBytes": len(bundle.signature)})
    return summary


def cmd_inspect(args: argparse.Namespace) -> int:
    summary = inspect_value(read_text(args.file))
    if args.json:
        print(json.dumps(summary, indent=2))
        return 0
    for key, value in summary.items():
        print(f"{key}: {json.dumps(value, indent=2) if isinstance(value, (dict, list)) else value}")
    return 0


def cmd_log_init(args: argparse.Namespace) -> int:
    key = load_private_key(read_text(args.key))
    text = TransparencyLog(args.log).create(key, args.origin)
    emit(args, {"log": args.log, "origin": args.origin, "logId": b64(key.public.log_id), "checkpoint": text}, text.rstrip("\n"))
    return 0


def cmd_log_add(args: argparse.Namespace) -> int:
    key = load_private_key(read_text(args.key))
    signer = load_public_key(read_text(args.public_key))
    bundle = parse_bundle(read_json(args.bundle))
    if bundle.envelope is not None:
        if envelope_signer(bundle.envelope, [signer]) is None:
            raise InputError("the DSSE signature does not verify with --public-key; the log refuses entries it cannot check")
        body = dsse_body(bundle.envelope, signer)
    else:
        if bundle.digest is None or not verify_digest(signer, bundle.digest, *parse_der_signature(bundle.signature)):
            raise InputError("the signature does not verify over the bundle's digest with --public-key; the log refuses entries it cannot check")
        body = hashedrekord_body(bundle.digest, bundle.signature, signer)
    log = TransparencyLog(args.log)
    index = log.append(key, body, int(time.time()))
    entry = log.prove(index)
    if args.output:
        write_json(args.output, entry)
    emit(args, entry, f"logIndex {index}\n{entry['inclusionProof']['checkpoint']['envelope'].rstrip()}")
    return 0


def cmd_log_prove(args: argparse.Namespace) -> int:
    entry = TransparencyLog(args.log).prove(args.index, args.size)
    if args.output:
        write_json(args.output, entry)
    print(json.dumps(entry, indent=2))
    return 0


def cmd_log_consistency(args: argparse.Namespace) -> int:
    proof = TransparencyLog(args.log).consistency(args.old, args.new)
    if args.output:
        write_json(args.output, proof)
    print(json.dumps(proof, indent=2))
    return 0


def cmd_log_verify(args: argparse.Namespace) -> int:
    entry = read_json(args.entry)
    bundle = parse_bundle(read_json(args.bundle)) if args.bundle else None
    report = Report(f"tlog entry {entry.get('logIndex')}")
    report.checks.extend(verify_tlog_entry(entry, load_public_key(read_text(args.log_key)), bundle))
    return finish(args, report)


def cmd_log_verify_consistency(args: argparse.Namespace) -> int:
    trusted = read_text(args.trusted_checkpoint) if args.trusted_checkpoint else ""
    return finish(args, verify_consistency_proof(read_json(args.proof), load_public_key(read_text(args.log_key)), trusted))


def cmd_log_audit(args: argparse.Namespace) -> int:
    return finish(args, audit_log(TransparencyLog(args.log), load_public_key(read_text(args.log_key))))


def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--json", action="store_true", help="machine-readable output")
    keyless = argparse.ArgumentParser(add_help=False)
    keyless.add_argument("--key", action="append", default=[], help="trusted public key PEM (repeatable)")
    keyless.add_argument("--certificate-identity", help="keyless: the SAN you expect in the signing certificate")
    keyless.add_argument("--certificate-oidc-issuer", help="keyless: the OIDC issuer you expect in the certificate")
    keyless.add_argument("--insecure-ignore-chain", action="store_true", help="keyless: report the other checks although the certificate chain is not verified")
    keyless.add_argument("--log-key", help="transparency log public key: verify the entry, its inclusion proof and checkpoint")
    keyless.add_argument("--tlog-entry", help="a log entry file from `minisign log add` (default: the bundle's own tlogEntries)")

    parser = argparse.ArgumentParser(prog="minisign", description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)

    p = commands.add_parser("keygen", parents=[common], help="create an ECDSA P-256 key pair")
    p.add_argument("--output-key-prefix", default="minisign", help="writes PREFIX.key (PKCS#8, mode 0600) and PREFIX.pub")
    p.add_argument("--force", action="store_true", help="overwrite existing files")
    p.set_defaults(handler=cmd_keygen)

    p = commands.add_parser("public-key", parents=[common], help="print the SPKI public key of a key or certificate")
    p.add_argument("--key", required=True)
    p.set_defaults(handler=cmd_public_key)

    p = commands.add_parser("sign-blob", parents=[common], help="sign a file; prints the base64 DER signature")
    p.add_argument("--key", required=True, help="private key PEM (PKCS#8 or SEC1)")
    p.add_argument("--bundle", help="write a Sigstore v0.3 bundle")
    p.add_argument("--output-signature", help="write the base64 signature")
    p.add_argument("file")
    p.set_defaults(handler=cmd_sign_blob)

    p = commands.add_parser("verify-blob", parents=[common, keyless], help="verify a blob against a bundle or a detached signature")
    p.add_argument("--bundle")
    p.add_argument("--signature", help="detached signature, base64 or raw DER")
    p.add_argument("file")
    p.set_defaults(handler=cmd_verify_blob)

    p = commands.add_parser("provenance", parents=[common], help="write an in-toto Statement with a SLSA provenance v1 predicate")
    p.add_argument("--subject", action="append", required=True, help="artifact file (repeatable)")
    p.add_argument("--builder-id", required=True)
    p.add_argument("--build-type", default=BUILD_TYPE)
    p.add_argument("--source", help="source URI, e.g. git+https://git.example/acme/app@refs/heads/main")
    p.add_argument("--commit", help="the source commit (gitCommit digest)")
    p.add_argument("--dependency", action="append", default=[], help="URI=sha256:HEX (repeatable)")
    p.add_argument("--param", action="append", default=[], help="external parameter KEY=VALUE (repeatable)")
    p.add_argument("--invocation-id", default="")
    p.add_argument("--started-on", default="")
    p.add_argument("--finished-on", default="")
    p.add_argument("--output")
    p.set_defaults(handler=cmd_provenance)

    p = commands.add_parser("attest", parents=[common], help="sign an in-toto statement into a DSSE envelope")
    p.add_argument("--key", required=True)
    p.add_argument("--statement", required=True)
    p.add_argument("--bundle", help="write a Sigstore v0.3 bundle with the envelope")
    p.add_argument("--envelope", help="write the bare DSSE envelope")
    p.set_defaults(handler=cmd_attest)

    p = commands.add_parser("verify-attestation", parents=[common, keyless], help="verify a DSSE provenance attestation against a policy")
    p.add_argument("--bundle")
    p.add_argument("--envelope")
    p.add_argument("--artifact", help="the artifact the provenance must describe")
    p.add_argument("--digest", help="sha256:HEX instead of --artifact")
    p.add_argument("--policy", help="JSON policy: builderId, predicateType, buildType, source, signers")
    p.add_argument("--builder-id")
    p.add_argument("--build-type")
    p.add_argument("--source")
    p.add_argument("--predicate-type")
    p.set_defaults(handler=cmd_verify_attestation)

    p = commands.add_parser("inspect", parents=[common], help="explain a key, bundle, envelope, statement, log entry or checkpoint")
    p.add_argument("file")
    p.set_defaults(handler=cmd_inspect)

    log = commands.add_parser("log", help="an RFC 6962 transparency log in a directory")
    log_commands = log.add_subparsers(dest="log_command", required=True)
    p = log_commands.add_parser("init", parents=[common])
    p.add_argument("--log", required=True)
    p.add_argument("--key", required=True, help="the log's private key")
    p.add_argument("--origin", default="minisign.local", help="the checkpoint origin line")
    p.set_defaults(handler=cmd_log_init)
    p = log_commands.add_parser("add", parents=[common])
    p.add_argument("--log", required=True)
    p.add_argument("--key", required=True, help="the log's private key")
    p.add_argument("--bundle", required=True)
    p.add_argument("--public-key", required=True, help="the signer's public key, recorded in the entry")
    p.add_argument("--output", help="write the log entry with its inclusion proof")
    p.set_defaults(handler=cmd_log_add)
    p = log_commands.add_parser("prove", parents=[common])
    p.add_argument("--log", required=True)
    p.add_argument("--index", type=int, required=True)
    p.add_argument("--size", type=int)
    p.add_argument("--output")
    p.set_defaults(handler=cmd_log_prove)
    p = log_commands.add_parser("consistency", parents=[common])
    p.add_argument("--log", required=True)
    p.add_argument("--old", type=int, required=True)
    p.add_argument("--new", type=int)
    p.add_argument("--output")
    p.set_defaults(handler=cmd_log_consistency)
    p = log_commands.add_parser("verify", parents=[common])
    p.add_argument("--log-key", required=True)
    p.add_argument("--entry", required=True)
    p.add_argument("--bundle", help="also check that the entry is this bundle's")
    p.set_defaults(handler=cmd_log_verify)
    p = log_commands.add_parser("verify-consistency", parents=[common])
    p.add_argument("--log-key", required=True)
    p.add_argument("--proof", required=True)
    p.add_argument("--trusted-checkpoint", help="the checkpoint you saw earlier; the proof must extend it")
    p.set_defaults(handler=cmd_log_verify_consistency)
    p = log_commands.add_parser("audit", parents=[common])
    p.add_argument("--log", required=True)
    p.add_argument("--log-key", required=True)
    p.set_defaults(handler=cmd_log_audit)
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(list(sys.argv[1:] if argv is None else argv))
    try:
        return args.handler(args)
    except InputError as exc:
        print(f"minisign: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
