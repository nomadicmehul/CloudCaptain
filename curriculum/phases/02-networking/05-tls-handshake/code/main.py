"""tlslens: parse a TLS ClientHello or ServerHello from raw bytes, read X.509 certificates from DER, build a chain to a trust anchor, check RSA and ECDSA signatures with pow(), match hostnames per RFC 6125, and report failures with OpenSSL's verify codes."""
from __future__ import annotations

import argparse
import base64
import binascii
import contextlib
import datetime as dt
import hashlib
import hmac
import ipaddress
import json
import os
import re
import socket
import sys
import threading
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

UTC = dt.timezone.utc

CONTENT_TYPES = {20: "change_cipher_spec", 21: "alert", 22: "handshake", 23: "application_data", 24: "heartbeat"}
HANDSHAKE_TYPES = {
    1: "client_hello",
    2: "server_hello",
    4: "new_session_ticket",
    8: "encrypted_extensions",
    11: "certificate",
    12: "server_key_exchange",
    13: "certificate_request",
    14: "server_hello_done",
    15: "certificate_verify",
    16: "client_key_exchange",
    20: "finished",
}
VERSIONS = {0x0300: "SSL 3.0", 0x0301: "TLS 1.0", 0x0302: "TLS 1.1", 0x0303: "TLS 1.2", 0x0304: "TLS 1.3"}
CIPHER_SUITES = {
    0x1301: "TLS_AES_128_GCM_SHA256",
    0x1302: "TLS_AES_256_GCM_SHA384",
    0x1303: "TLS_CHACHA20_POLY1305_SHA256",
    0x1304: "TLS_AES_128_CCM_SHA256",
    0x1305: "TLS_AES_128_CCM_8_SHA256",
    0xC02B: "TLS_ECDHE_ECDSA_WITH_AES_128_GCM_SHA256",
    0xC02F: "TLS_ECDHE_RSA_WITH_AES_128_GCM_SHA256",
    0xC02C: "TLS_ECDHE_ECDSA_WITH_AES_256_GCM_SHA384",
    0xC030: "TLS_ECDHE_RSA_WITH_AES_256_GCM_SHA384",
    0xCCA9: "TLS_ECDHE_ECDSA_WITH_CHACHA20_POLY1305_SHA256",
    0xCCA8: "TLS_ECDHE_RSA_WITH_CHACHA20_POLY1305_SHA256",
    0xCCAA: "TLS_DHE_RSA_WITH_CHACHA20_POLY1305_SHA256",
    0xC009: "TLS_ECDHE_ECDSA_WITH_AES_128_CBC_SHA",
    0xC00A: "TLS_ECDHE_ECDSA_WITH_AES_256_CBC_SHA",
    0xC013: "TLS_ECDHE_RSA_WITH_AES_128_CBC_SHA",
    0xC014: "TLS_ECDHE_RSA_WITH_AES_256_CBC_SHA",
    0xC023: "TLS_ECDHE_ECDSA_WITH_AES_128_CBC_SHA256",
    0xC024: "TLS_ECDHE_ECDSA_WITH_AES_256_CBC_SHA384",
    0xC027: "TLS_ECDHE_RSA_WITH_AES_128_CBC_SHA256",
    0xC028: "TLS_ECDHE_RSA_WITH_AES_256_CBC_SHA384",
    0x009E: "TLS_DHE_RSA_WITH_AES_128_GCM_SHA256",
    0x009F: "TLS_DHE_RSA_WITH_AES_256_GCM_SHA384",
    0x0033: "TLS_DHE_RSA_WITH_AES_128_CBC_SHA",
    0x0039: "TLS_DHE_RSA_WITH_AES_256_CBC_SHA",
    0x0067: "TLS_DHE_RSA_WITH_AES_128_CBC_SHA256",
    0x006B: "TLS_DHE_RSA_WITH_AES_256_CBC_SHA256",
    0x009C: "TLS_RSA_WITH_AES_128_GCM_SHA256",
    0x009D: "TLS_RSA_WITH_AES_256_GCM_SHA384",
    0x002F: "TLS_RSA_WITH_AES_128_CBC_SHA",
    0x0035: "TLS_RSA_WITH_AES_256_CBC_SHA",
    0x003C: "TLS_RSA_WITH_AES_128_CBC_SHA256",
    0x003D: "TLS_RSA_WITH_AES_256_CBC_SHA256",
    0x000A: "TLS_RSA_WITH_3DES_EDE_CBC_SHA",
    0x00FF: "TLS_EMPTY_RENEGOTIATION_INFO_SCSV",
    0x5600: "TLS_FALLBACK_SCSV",
}
GROUPS = {
    0x0017: "secp256r1",
    0x0018: "secp384r1",
    0x0019: "secp521r1",
    0x001D: "x25519",
    0x001E: "x448",
    0x001F: "brainpoolP256r1tls13",
    0x0020: "brainpoolP384r1tls13",
    0x0021: "brainpoolP512r1tls13",
    0x0029: "curveSM2",
    0x0100: "ffdhe2048",
    0x0101: "ffdhe3072",
    0x0102: "ffdhe4096",
    0x0103: "ffdhe6144",
    0x0104: "ffdhe8192",
    0x0200: "MLKEM512",
    0x0201: "MLKEM768",
    0x0202: "MLKEM1024",
    0x11EB: "SecP256r1MLKEM768",
    0x11EC: "X25519MLKEM768",
    0x11ED: "SecP384r1MLKEM1024",
    0x11EE: "curveSM2MLKEM768",
    0x6399: "X25519Kyber768Draft00",
}
SIGNATURE_SCHEMES = {
    0x0201: "rsa_pkcs1_sha1",
    0x0203: "ecdsa_sha1",
    0x0301: "rsa_pkcs1_sha224",
    0x0302: "dsa_sha224",
    0x0303: "ecdsa_sha224",
    0x0402: "dsa_sha256",
    0x0502: "dsa_sha384",
    0x0602: "dsa_sha512",
    0x0708: "sm2sig_sm3",
    0x0401: "rsa_pkcs1_sha256",
    0x0501: "rsa_pkcs1_sha384",
    0x0601: "rsa_pkcs1_sha512",
    0x0403: "ecdsa_secp256r1_sha256",
    0x0503: "ecdsa_secp384r1_sha384",
    0x0603: "ecdsa_secp521r1_sha512",
    0x0804: "rsa_pss_rsae_sha256",
    0x0805: "rsa_pss_rsae_sha384",
    0x0806: "rsa_pss_rsae_sha512",
    0x0807: "ed25519",
    0x0808: "ed448",
    0x0809: "rsa_pss_pss_sha256",
    0x080A: "rsa_pss_pss_sha384",
    0x080B: "rsa_pss_pss_sha512",
    0x081A: "ecdsa_brainpoolP256r1tls13_sha256",
    0x081B: "ecdsa_brainpoolP384r1tls13_sha384",
    0x081C: "ecdsa_brainpoolP512r1tls13_sha512",
    0x0904: "mldsa44",
    0x0905: "mldsa65",
    0x0906: "mldsa87",
}
EXTENSIONS = {
    0: "server_name",
    1: "max_fragment_length",
    5: "status_request",
    10: "supported_groups",
    11: "ec_point_formats",
    13: "signature_algorithms",
    14: "use_srtp",
    15: "heartbeat",
    16: "alpn",
    18: "signed_certificate_timestamp",
    21: "padding",
    22: "encrypt_then_mac",
    23: "extended_master_secret",
    27: "compress_certificate",
    28: "record_size_limit",
    35: "session_ticket",
    41: "pre_shared_key",
    42: "early_data",
    43: "supported_versions",
    44: "cookie",
    45: "psk_key_exchange_modes",
    47: "certificate_authorities",
    49: "post_handshake_auth",
    50: "signature_algorithms_cert",
    51: "key_share",
    0x3374: "next_protocol_negotiation",
    0x4469: "application_settings_old",
    0x44CD: "application_settings",
    0xFE0D: "encrypted_client_hello",
    0xFF01: "renegotiation_info",
}
ALERTS = {
    0: "close_notify",
    10: "unexpected_message",
    20: "bad_record_mac",
    22: "record_overflow",
    40: "handshake_failure",
    42: "bad_certificate",
    43: "unsupported_certificate",
    44: "certificate_revoked",
    45: "certificate_expired",
    46: "certificate_unknown",
    47: "illegal_parameter",
    48: "unknown_ca",
    49: "access_denied",
    50: "decode_error",
    51: "decrypt_error",
    70: "protocol_version",
    71: "insufficient_security",
    80: "internal_error",
    86: "inappropriate_fallback",
    90: "user_canceled",
    109: "missing_extension",
    110: "unsupported_extension",
    112: "unrecognized_name",
    116: "certificate_required",
    120: "no_application_protocol",
}
ALERT_CODES = {name: code for code, name in ALERTS.items()}
HRR_RANDOM = bytes.fromhex("cf21ad74e59a6111be1d8c021e65b891c2a211167abb8c5e079e09e2c8a8339c")
DOWNGRADE_SENTINELS = {b"DOWNGRD\x01": "TLS 1.2 chosen by a server that also speaks TLS 1.3", b"DOWNGRD\x00": "TLS 1.1 or older chosen by a server that speaks TLS 1.2 or newer"}
PLAINTEXT_PREFIXES = (b"GET ", b"POST ", b"HEAD ", b"PUT ", b"DELETE ", b"OPTIONS ", b"PATCH ", b"CONNECT ", b"PRI * ", b"HTTP/")
MAX_RECORD = 2 ** 14 + 2048
CAPTURE_LIMIT = 1 << 20

PROBE_SUITES = [0xC02B, 0xC02F, 0xC02C, 0xC030, 0xCCA9, 0xCCA8, 0xC009, 0xC013, 0xC00A, 0xC014, 0x009C, 0x009D, 0x002F, 0x0035]
PROBE_GROUPS = [0x001D, 0x0017, 0x0018]
PROBE_SIGNATURE_SCHEMES = [0x0403, 0x0503, 0x0804, 0x0805, 0x0806, 0x0401, 0x0501, 0x0601]

SEQUENCE, SET, INTEGER, BIT_STRING, OCTET_STRING, NULL, OID, BOOLEAN = 0x30, 0x31, 0x02, 0x03, 0x04, 0x05, 0x06, 0x01
UTC_TIME, GENERALIZED_TIME = 0x17, 0x18
STRING_CODECS = {0x0C: "utf-8", 0x12: "ascii", 0x13: "ascii", 0x14: "latin-1", 0x16: "ascii", 0x1A: "ascii", 0x1C: "utf-32-be", 0x1E: "utf-16-be"}
NAME_LABELS = {
    "2.5.4.3": "CN",
    "2.5.4.6": "C",
    "2.5.4.7": "L",
    "2.5.4.8": "ST",
    "2.5.4.9": "street",
    "2.5.4.10": "O",
    "2.5.4.11": "OU",
    "2.5.4.5": "serialNumber",
    "2.5.4.4": "SN",
    "2.5.4.42": "GN",
    "2.5.4.12": "title",
    "2.5.4.15": "businessCategory",
    "2.5.4.97": "organizationIdentifier",
    "0.9.2342.19200300.100.1.25": "DC",
    "0.9.2342.19200300.100.1.1": "UID",
    "1.2.840.113549.1.9.1": "emailAddress",
    "1.3.6.1.4.1.311.60.2.1.3": "jurisdictionC",
}
RFC4514_LABELS = {"CN", "C", "L", "ST", "O", "OU", "street", "DC", "UID"}
CN_OID = "2.5.4.3"
RSA_OID = "1.2.840.113549.1.1.1"
EC_OID = "1.2.840.10045.2.1"
KEY_ALGORITHMS = {RSA_OID: "RSA", EC_OID: "EC", "1.3.101.112": "Ed25519", "1.3.101.113": "Ed448", "1.2.840.113549.1.1.10": "RSA-PSS"}
SIGNATURE_ALGORITHMS = {
    "1.2.840.113549.1.1.5": ("RSA", "sha1", "sha1WithRSAEncryption"),
    "1.2.840.113549.1.1.11": ("RSA", "sha256", "sha256WithRSAEncryption"),
    "1.2.840.113549.1.1.12": ("RSA", "sha384", "sha384WithRSAEncryption"),
    "1.2.840.113549.1.1.13": ("RSA", "sha512", "sha512WithRSAEncryption"),
    "1.2.840.10045.4.1": ("EC", "sha1", "ecdsa-with-SHA1"),
    "1.2.840.10045.4.3.2": ("EC", "sha256", "ecdsa-with-SHA256"),
    "1.2.840.10045.4.3.3": ("EC", "sha384", "ecdsa-with-SHA384"),
    "1.2.840.10045.4.3.4": ("EC", "sha512", "ecdsa-with-SHA512"),
    "1.2.840.113549.1.1.10": ("RSA-PSS", "", "rsassaPss"),
    "1.3.101.112": ("Ed25519", "", "ED25519"),
}
HASH_OIDS = {"sha1": "1.3.14.3.2.26", "sha256": "2.16.840.1.101.3.4.2.1", "sha384": "2.16.840.1.101.3.4.2.2", "sha512": "2.16.840.1.101.3.4.2.3"}
EXT_SKI, EXT_KU, EXT_SAN, EXT_BC, EXT_NAME_CONSTRAINTS, EXT_POLICIES, EXT_AKI, EXT_EKU = (
    "2.5.29.14", "2.5.29.15", "2.5.29.17", "2.5.29.19", "2.5.29.30", "2.5.29.32", "2.5.29.35", "2.5.29.37")
EXT_AIA = "1.3.6.1.5.5.7.1.1"
EXTENSION_NAMES = {
    EXT_SKI: "subjectKeyIdentifier",
    EXT_KU: "keyUsage",
    EXT_SAN: "subjectAltName",
    EXT_BC: "basicConstraints",
    EXT_NAME_CONSTRAINTS: "nameConstraints",
    "2.5.29.31": "cRLDistributionPoints",
    EXT_POLICIES: "certificatePolicies",
    EXT_AKI: "authorityKeyIdentifier",
    EXT_EKU: "extendedKeyUsage",
    EXT_AIA: "authorityInfoAccess",
    "1.3.6.1.4.1.11129.2.4.2": "ctPrecertificateSCTs",
    "1.3.6.1.5.5.7.1.24": "tlsFeature",
}
HANDLED_CRITICAL = {EXT_SKI, EXT_KU, EXT_SAN, EXT_BC, EXT_POLICIES, EXT_AKI, EXT_EKU}
KEY_USAGE_BITS = ["digitalSignature", "nonRepudiation", "keyEncipherment", "dataEncipherment", "keyAgreement", "keyCertSign", "cRLSign", "encipherOnly", "decipherOnly"]
EKU_NAMES = {
    "1.3.6.1.5.5.7.3.1": "serverAuth",
    "1.3.6.1.5.5.7.3.2": "clientAuth",
    "1.3.6.1.5.5.7.3.3": "codeSigning",
    "1.3.6.1.5.5.7.3.4": "emailProtection",
    "1.3.6.1.5.5.7.3.8": "timeStamping",
    "1.3.6.1.5.5.7.3.9": "OCSPSigning",
    "2.5.29.37.0": "anyExtendedKeyUsage",
}
OCSP_OID, CA_ISSUERS_OID = "1.3.6.1.5.5.7.48.1", "1.3.6.1.5.5.7.48.2"

VERIFY_MESSAGES = {
    0: "ok",
    2: "unable to get issuer certificate",
    7: "certificate signature failure",
    9: "certificate is not yet valid or the system clock is incorrect",
    10: "certificate has expired",
    18: "self-signed certificate",
    19: "self-signed certificate in certificate chain",
    20: "unable to get local issuer certificate",
    21: "unable to verify the first certificate",
    22: "certificate chain too long",
    25: "path length constraint exceeded",
    26: "unsuitable certificate purpose",
    32: "key usage does not include certificate signing",
    34: "unhandled critical extension",
    62: "hostname mismatch",
    64: "IP address mismatch",
    76: "Cannot find certificate signature algorithm",
    79: "invalid CA certificate",
}
DEFAULT_BUNDLES = (
    "/etc/ssl/certs/ca-certificates.crt",
    "/etc/pki/tls/certs/ca-bundle.crt",
    "/etc/ssl/cert.pem",
    "/opt/homebrew/etc/ca-certificates/cert.pem",
)


class InputError(ValueError):
    pass


class TlsError(InputError):
    pass


class DerError(InputError):
    pass


class Reader:
    def __init__(self, data: bytes, what: str = "data"):
        self.data = data
        self.pos = 0
        self.what = what

    def take(self, count: int) -> bytes:
        if self.pos + count > len(self.data):
            raise TlsError(f"{self.what} is truncated: needs {count} bytes at offset {self.pos}, {len(self.data) - self.pos} left")
        chunk = self.data[self.pos:self.pos + count]
        self.pos += count
        return chunk

    def uint(self, size: int) -> int:
        return int.from_bytes(self.take(size), "big")

    def vector(self, length_size: int) -> bytes:
        return self.take(self.uint(length_size))

    def done(self) -> bool:
        return self.pos >= len(self.data)


@dataclass
class Record:
    content_type: int
    version: int
    fragment: bytes


@dataclass
class Handshake:
    msg_type: int
    body: bytes

    @property
    def name(self) -> str:
        return HANDSHAKE_TYPES.get(self.msg_type, f"handshake_{self.msg_type}")


@dataclass
class Flight:
    records: List[Record]
    messages: List[Handshake]
    alerts: List[Tuple[int, int]]
    change_cipher_spec: int
    encrypted: int
    leftover: int


@dataclass
class Extension:
    type: int
    data: bytes

    @property
    def name(self) -> str:
        if is_grease(self.type):
            return "GREASE"
        return EXTENSIONS.get(self.type, f"unknown_{self.type}")


@dataclass
class Hello:
    kind: str
    legacy_version: int
    random: bytes
    session_id: bytes
    cipher_suites: List[int]
    compression_methods: List[int]
    extensions: List[Extension]

    def extension(self, ext_type: int) -> Optional[Extension]:
        return next((e for e in self.extensions if e.type == ext_type), None)


def is_grease(value: int) -> bool:
    return value & 0x0F0F == 0x0A0A and value >> 8 == value & 0xFF


def label(table: Dict[int, str], value: int) -> str:
    if is_grease(value):
        return "GREASE"
    return table.get(value) or f"0x{value:04x}"


def version_name(value: int) -> str:
    return label(VERSIONS, value)


def parse_records(data: bytes) -> Tuple[List[Record], bytes]:
    if data.startswith(PLAINTEXT_PREFIXES):
        raise TlsError(f"the bytes start with {data[:16]!r}: this is plaintext HTTP, not TLS (wrong port, or http:// instead of https://)")
    if data[:1] == b"\x80":
        raise TlsError("an SSLv2-format hello: only clients from before TLS 1.0 send this")
    records: List[Record] = []
    pos = 0
    while len(data) - pos >= 5:
        content_type = data[pos]
        if content_type not in CONTENT_TYPES:
            raise TlsError(f"byte {pos}: {content_type} is not a TLS record type (20-24); the stream is not TLS or is out of step")
        version = int.from_bytes(data[pos + 1:pos + 3], "big")
        length = int.from_bytes(data[pos + 3:pos + 5], "big")
        if length > MAX_RECORD:
            raise TlsError(f"byte {pos}: record length {length} exceeds 2^14+2048")
        if pos + 5 + length > len(data):
            break
        records.append(Record(content_type, version, data[pos + 5:pos + 5 + length]))
        pos += 5 + length
    return records, data[pos:]


def read_flight(data: bytes) -> Flight:
    records, rest = parse_records(data)
    messages: List[Handshake] = []
    alerts: List[Tuple[int, int]] = []
    buffer = b""
    ccs = encrypted = 0
    for record in records:
        kind, fragment = record.content_type, record.fragment
        if kind == 20:
            ccs += 1
        elif kind == 21 and len(fragment) == 2:
            alerts.append((fragment[0], fragment[1]))
        elif kind == 22 and (not ccs or plaintext_hello(fragment)):
            buffer += fragment
            while len(buffer) >= 4 and len(buffer) >= 4 + int.from_bytes(buffer[1:4], "big"):
                length = int.from_bytes(buffer[1:4], "big")
                messages.append(Handshake(buffer[0], buffer[4:4 + length]))
                buffer = buffer[4 + length:]
        else:
            encrypted += 1
    return Flight(records, messages, alerts, ccs, encrypted, len(rest) + len(buffer))


def plaintext_hello(fragment: bytes) -> bool:
    return len(fragment) >= 4 and fragment[0] in (1, 2) and int.from_bytes(fragment[1:4], "big") == len(fragment) - 4


def parse_extensions(reader: Reader) -> List[Extension]:
    if reader.done():
        return []
    block = Reader(reader.vector(2), "extensions")
    extensions: List[Extension] = []
    while not block.done():
        ext_type = block.uint(2)
        if any(e.type == ext_type for e in extensions):
            raise TlsError(f"extension {label(EXTENSIONS, ext_type)} appears twice; RFC 8446 forbids duplicates")
        extensions.append(Extension(ext_type, block.vector(2)))
    return extensions


def parse_client_hello(body: bytes) -> Hello:
    reader = Reader(body, "ClientHello")
    version = reader.uint(2)
    random = reader.take(32)
    session_id = reader.vector(1)
    suites = u16_list(reader.vector(2), "cipher_suites")
    compression = list(reader.vector(1))
    return Hello("client_hello", version, random, session_id, suites, compression, parse_extensions(reader))


def parse_server_hello(body: bytes) -> Hello:
    reader = Reader(body, "ServerHello")
    version = reader.uint(2)
    random = reader.take(32)
    session_id = reader.vector(1)
    suite = reader.uint(2)
    compression = reader.uint(1)
    kind = "hello_retry_request" if random == HRR_RANDOM else "server_hello"
    return Hello(kind, version, random, session_id, [suite], [compression], parse_extensions(reader))


def u16_list(raw: bytes, what: str) -> List[int]:
    if len(raw) % 2:
        raise TlsError(f"{what} has an odd length ({len(raw)} bytes)")
    return [int.from_bytes(raw[i:i + 2], "big") for i in range(0, len(raw), 2)]


def server_names(ext: Optional[Extension]) -> List[str]:
    if ext is None or not ext.data:
        return []
    entries = Reader(Reader(ext.data, "server_name").vector(2), "server_name list")
    names = []
    while not entries.done():
        name_type = entries.uint(1)
        name = entries.vector(2)
        if name_type == 0:
            names.append(name.decode("ascii", "replace"))
    return names


def alpn_protocols(ext: Optional[Extension]) -> List[str]:
    if ext is None:
        return []
    entries = Reader(Reader(ext.data, "alpn").vector(2), "alpn list")
    protocols = []
    while not entries.done():
        protocols.append(entries.vector(1).decode("ascii", "replace"))
    return protocols


def supported_versions(ext: Optional[Extension], kind: str) -> List[int]:
    if ext is None:
        return []
    if kind == "client_hello":
        return u16_list(Reader(ext.data, "supported_versions").vector(1), "supported_versions")
    return [Reader(ext.data, "supported_versions").uint(2)]


def key_shares(ext: Optional[Extension], kind: str) -> List[Tuple[int, int]]:
    if ext is None:
        return []
    reader = Reader(ext.data, "key_share")
    if kind == "hello_retry_request":
        return [(reader.uint(2), 0)]
    if kind == "server_hello":
        group = reader.uint(2)
        return [(group, len(reader.vector(2)))]
    entries = Reader(reader.vector(2), "key_share entries")
    shares = []
    while not entries.done():
        group = entries.uint(2)
        shares.append((group, len(entries.vector(2))))
    return shares


def u16_extension(ext: Optional[Extension]) -> List[int]:
    if ext is None:
        return []
    return u16_list(Reader(ext.data, "extension").vector(2), "extension list")


def point_formats(ext: Optional[Extension]) -> List[int]:
    return list(Reader(ext.data, "ec_point_formats").vector(1)) if ext else []


def ja3(hello: Hello) -> str:
    fields = [
        [hello.legacy_version],
        [s for s in hello.cipher_suites if not is_grease(s)],
        [e.type for e in hello.extensions if not is_grease(e.type)],
        [g for g in u16_extension(hello.extension(10)) if not is_grease(g)],
        point_formats(hello.extension(11)),
    ]
    return ",".join("-".join(str(v) for v in values) for values in fields)


def describe_hello(hello: Hello) -> Dict[str, Any]:
    out: Dict[str, Any] = {
        "type": hello.kind,
        "legacy_version": version_name(hello.legacy_version),
        "session_id_bytes": len(hello.session_id),
        "extensions": [e.name for e in hello.extensions],
        "alpn": alpn_protocols(hello.extension(16)),
    }
    versions = supported_versions(hello.extension(43), hello.kind)
    if hello.kind == "client_hello":
        offered = [v for v in versions if not is_grease(v)] or [hello.legacy_version]
        names = server_names(hello.extension(0))
        fingerprint = ja3(hello)
        out.update({
            "versions": [version_name(v) for v in offered],
            "max_version": version_name(max(offered)),
            "sni": names[0] if names else None,
            "cipher_suites": [label(CIPHER_SUITES, s) for s in hello.cipher_suites],
            "groups": [label(GROUPS, g) for g in u16_extension(hello.extension(10))],
            "key_shares": [{"group": label(GROUPS, g), "bytes": n} for g, n in key_shares(hello.extension(51), hello.kind)],
            "signature_algorithms": [label(SIGNATURE_SCHEMES, s) for s in u16_extension(hello.extension(13))],
            "grease": any(is_grease(v) for v in hello.cipher_suites + [e.type for e in hello.extensions]),
            "ja3": fingerprint,
            "ja3_md5": hashlib.md5(fingerprint.encode(), usedforsecurity=False).hexdigest(),
        })
        return out
    selected = versions[0] if versions else hello.legacy_version
    shares = key_shares(hello.extension(51), hello.kind)
    out.update({
        "version": version_name(selected),
        "version_from": "supported_versions" if versions else "legacy_version",
        "cipher_suite": label(CIPHER_SUITES, hello.cipher_suites[0]),
        "cipher_suite_id": hello.cipher_suites[0],
        "key_share": label(GROUPS, shares[0][0]) if shares else None,
        "downgrade_sentinel": DOWNGRADE_SENTINELS.get(hello.random[-8:]),
    })
    out["alpn"] = out["alpn"][0] if out["alpn"] else None
    return out


def certificate_list(body: bytes) -> List[bytes]:
    entries = Reader(Reader(body, "Certificate message").vector(3), "certificate_list")
    ders = []
    while not entries.done():
        ders.append(entries.vector(3))
    return ders


def describe_flight(data: bytes) -> Dict[str, Any]:
    flight = read_flight(data)
    out: Dict[str, Any] = {
        "bytes": len(data),
        "records": [CONTENT_TYPES[r.content_type] for r in flight.records],
        "handshake": [m.name for m in flight.messages],
        "hellos": [],
        "alerts": [{"level": "fatal" if level == 2 else "warning", "description": label(ALERTS, code), "code": code} for level, code in flight.alerts],
        "certificates": [],
        "key_exchange_group": None,
        "change_cipher_spec": flight.change_cipher_spec,
        "encrypted_records": flight.encrypted,
        "incomplete_bytes": flight.leftover,
    }
    for message in flight.messages:
        if message.msg_type == 1:
            out["hellos"].append(describe_hello(parse_client_hello(message.body)))
        elif message.msg_type == 2:
            out["hellos"].append(describe_hello(parse_server_hello(message.body)))
        elif message.msg_type == 11:
            out["certificates"] = [certificate_summary(parse_certificate(der)) for der in certificate_list(message.body)]
        elif message.msg_type == 12 and len(message.body) >= 3 and message.body[0] == 3:
            out["key_exchange_group"] = label(GROUPS, int.from_bytes(message.body[1:3], "big"))
    return out


def flight_certificates(data: bytes) -> List["Certificate"]:
    for message in read_flight(data).messages:
        if message.msg_type == 11:
            return [parse_certificate(der) for der in certificate_list(message.body)]
    return []


def vec(length_size: int, payload: bytes) -> bytes:
    return len(payload).to_bytes(length_size, "big") + payload


def tls_extension(ext_type: int, payload: bytes) -> bytes:
    return ext_type.to_bytes(2, "big") + vec(2, payload)


def u16s(values: Iterable[int]) -> bytes:
    return b"".join(v.to_bytes(2, "big") for v in values)


def build_client_hello(server_name: Optional[str], alpn: Sequence[str], random: bytes) -> bytes:
    extensions = b""
    if server_name and not is_ip(server_name):
        extensions += tls_extension(0, vec(2, b"\x00" + vec(2, server_name.encode("ascii"))))
    extensions += tls_extension(10, vec(2, u16s(PROBE_GROUPS)))
    extensions += tls_extension(11, vec(1, b"\x00"))
    extensions += tls_extension(13, vec(2, u16s(PROBE_SIGNATURE_SCHEMES)))
    if alpn:
        extensions += tls_extension(16, vec(2, b"".join(vec(1, p.encode("ascii")) for p in alpn)))
    extensions += tls_extension(23, b"")
    extensions += tls_extension(0xFF01, vec(1, b""))
    body = (0x0303).to_bytes(2, "big") + random + vec(1, b"") + vec(2, u16s(PROBE_SUITES)) + vec(1, b"\x00") + vec(2, extensions)
    return b"\x16\x03\x01" + vec(2, b"\x01" + vec(3, body))


def alert_record(code: int) -> bytes:
    return b"\x15\x03\x03\x00\x02\x02" + bytes([code])


def is_ip(text: str) -> bool:
    try:
        ipaddress.ip_address(text.strip("[]"))
    except ValueError:
        return False
    return True


def flight_finished(data: bytes, last_types: Sequence[int]) -> bool:
    try:
        flight = read_flight(data)
    except TlsError:
        return True
    return any(level == 2 for level, _ in flight.alerts) or any(m.msg_type in last_types for m in flight.messages)


def connect(host: str, port: int, timeout: float) -> socket.socket:
    try:
        return socket.create_connection((host, port), timeout=timeout)
    except OSError as exc:
        raise InputError(f"cannot connect to {host}:{port}: {exc.strerror or exc}")


def probe(host: str, port: int, server_name: Optional[str], alpn: Sequence[str], timeout: float = 10.0) -> bytes:
    received = b""
    with connect(host, port, timeout) as sock:
        sock.sendall(build_client_hello(server_name, alpn, os.urandom(32)))
        with contextlib.suppress(socket.timeout, ConnectionError):
            while not flight_finished(received, (14,)):
                chunk = sock.recv(65536)
                if not chunk:
                    break
                received += chunk
    return received


def read_client_hello(conn: socket.socket, timeout: float) -> bytes:
    conn.settimeout(timeout)
    data = b""
    with contextlib.suppress(socket.timeout, ConnectionError):
        while not flight_finished(data, (1,)):
            chunk = conn.recv(65536)
            if not chunk:
                break
            data += chunk
    return data


def relay(client: socket.socket, upstream: Tuple[str, int], timeout: float) -> Tuple[bytes, bytes]:
    server = connect(upstream[0], upstream[1], timeout)
    captured = {"client": bytearray(), "server": bytearray()}

    def pump(source: socket.socket, sink: socket.socket, key: str) -> None:
        try:
            while True:
                chunk = source.recv(65536)
                if not chunk:
                    break
                captured[key] += chunk[:max(0, CAPTURE_LIMIT - len(captured[key]))]
                sink.sendall(chunk)
        except OSError:
            pass
        finally:
            with contextlib.suppress(OSError):
                sink.shutdown(socket.SHUT_WR)

    client.settimeout(timeout)
    threads = [
        threading.Thread(target=pump, args=(client, server, "client"), daemon=True),
        threading.Thread(target=pump, args=(server, client, "server"), daemon=True),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout * 3)
    server.close()
    return bytes(captured["client"]), bytes(captured["server"])


def make_listener(host: str, port: int) -> socket.socket:
    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    listener = socket.socket(family, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        listener.bind((host, port))
    except OSError as exc:
        listener.close()
        raise InputError(f"cannot listen on {host}:{port}: {exc.strerror or exc}")
    listener.listen(1)
    return listener


def capture(listener: socket.socket, upstream: Optional[Tuple[str, int]] = None, alert: int = 40, timeout: float = 10.0, wait: float = 60.0) -> Tuple[bytes, bytes]:
    listener.settimeout(wait)
    try:
        conn, _ = listener.accept()
    except socket.timeout:
        raise InputError(f"no client connected within {wait:g} seconds")
    with conn:
        if upstream:
            return relay(conn, upstream, timeout)
        client_bytes = read_client_hello(conn, timeout)
        with contextlib.suppress(OSError):
            conn.sendall(alert_record(alert))
        return client_bytes, b""


@dataclass(frozen=True)
class Curve:
    name: str
    oid: str
    p: int
    b: int
    gx: int
    gy: int
    n: int

    @property
    def generator(self) -> Tuple[int, int]:
        return (self.gx, self.gy)


P256 = Curve(
    "P-256", "1.2.840.10045.3.1.7",
    p=2 ** 256 - 2 ** 224 + 2 ** 192 + 2 ** 96 - 1,
    b=0x5AC635D8AA3A93E7B3EBBD55769886BC651D06B0CC53B0F63BCE3C3E27D2604B,
    gx=0x6B17D1F2E12C4247F8BCE6E563A440F277037D812DEB33A0F4A13945D898C296,
    gy=0x4FE342E2FE1A7F9B8EE7EB4A7C0F9E162BCE33576B315ECECBB6406837BF51F5,
    n=0xFFFFFFFF00000000FFFFFFFFFFFFFFFFBCE6FAADA7179E84F3B9CAC2FC632551,
)
P384 = Curve(
    "P-384", "1.3.132.0.34",
    p=2 ** 384 - 2 ** 128 - 2 ** 96 + 2 ** 32 - 1,
    b=0xB3312FA7E23EE7E4988E056BE3F82D19181D9C6EFE8141120314088F5013875AC656398D8A2ED19D2A85C8EDD3EC2AEF,
    gx=0xAA87CA22BE8B05378EB1C71EF320AD746E1D3B628BA79B9859F741E082542A385502F25DBF55296C3A545E3872760AB7,
    gy=0x3617DE4A96262C6F5D9E98BF9292DC29F8F41DBD289A147CE9DA3113B5F0B8C00A60B1CE1D7E819D7A431D7C90EA0E5F,
    n=0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFC7634D81F4372DDF581A0DB248B0A77AECEC196ACCC52973,
)
CURVES = {c.oid: c for c in (P256, P384)}
UNSUPPORTED_CURVES = {"1.3.132.0.35": "P-521", "1.3.36.3.3.2.8.1.1.7": "brainpoolP256r1"}

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


def decode_point(curve: Curve, data: bytes) -> Tuple[int, int]:
    size = (curve.p.bit_length() + 7) // 8
    if len(data) == 1 + 2 * size and data[0] == 4:
        point = (int.from_bytes(data[1:1 + size], "big"), int.from_bytes(data[1 + size:], "big"))
    elif len(data) == 1 + size and data[0] in (2, 3):
        x = int.from_bytes(data[1:], "big")
        y = pow((x ** 3 - 3 * x + curve.b) % curve.p, (curve.p + 1) // 4, curve.p)
        point = (x, y if y % 2 == data[0] % 2 else curve.p - y)
    else:
        raise DerError(f"a {curve.name} public key of {len(data)} bytes is neither compressed nor uncompressed")
    if not on_curve(curve, point):
        raise DerError(f"the public key is not a point on {curve.name}")
    return point


def ecdsa_verify(curve: Curve, public: Tuple[int, int], digest: bytes, r: int, s: int) -> bool:
    n = curve.n
    if not (1 <= r < n and 1 <= s < n):
        return False
    e = int.from_bytes(digest, "big") >> max(0, len(digest) * 8 - n.bit_length())
    w = pow(s, -1, n)
    point = point_add(curve, scalar_mult(curve, e * w % n, curve.generator), scalar_mult(curve, r * w % n, public))
    return point is not None and point[0] % n == r


def der_length(length: int) -> bytes:
    if length < 0x80:
        return bytes([length])
    raw = length.to_bytes((length.bit_length() + 7) // 8, "big")
    return bytes([0x80 | len(raw)]) + raw


def der(tag: int, content: bytes) -> bytes:
    return bytes([tag]) + der_length(len(content)) + content


def der_oid(dotted: str) -> bytes:
    numbers = [int(n) for n in dotted.split(".")]
    body = b""
    for number in [numbers[0] * 40 + numbers[1]] + numbers[2:]:
        chunk = [number & 0x7F]
        number >>= 7
        while number:
            chunk.append(0x80 | (number & 0x7F))
            number >>= 7
        body += bytes(reversed(chunk))
    return der(OID, body)


def der_int(value: int) -> bytes:
    return der(INTEGER, value.to_bytes(value.bit_length() // 8 + 1, "big", signed=True))


def digest_info(hash_name: str, digest: bytes) -> bytes:
    return der(SEQUENCE, der(SEQUENCE, der_oid(HASH_OIDS[hash_name]) + der(NULL, b"")) + der(OCTET_STRING, digest))


def rsa_pkcs1_verify(n: int, e: int, signature: bytes, hash_name: str, message: bytes) -> bool:
    k = (n.bit_length() + 7) // 8
    if len(signature) != k:
        return False
    s = int.from_bytes(signature, "big")
    if s >= n:
        return False
    encoded = pow(s, e, n).to_bytes(k, "big")
    t = digest_info(hash_name, hashlib.new(hash_name, message).digest())
    if k < len(t) + 11:
        return False
    expected = b"\x00\x01" + b"\xff" * (k - len(t) - 3) + b"\x00" + t
    return hmac.compare_digest(encoded, expected)


@dataclass
class Tlv:
    tag: int
    value: bytes
    raw: bytes

    def children(self) -> List["Tlv"]:
        if not self.tag & 0x20:
            raise DerError(f"tag 0x{self.tag:02x} is primitive and has no children")
        return read_all(self.value)


def read_tlv(data: bytes, pos: int = 0) -> Tuple[Tlv, int]:
    if pos + 2 > len(data):
        raise DerError(f"DER ends at offset {pos} in the middle of a tag and length")
    tag, first = data[pos], data[pos + 1]
    if tag & 0x1F == 0x1F:
        raise DerError(f"offset {pos}: multi-byte tags do not occur in X.509")
    cursor = pos + 2
    if first == 0x80:
        raise DerError(f"offset {pos}: indefinite length is BER; DER forbids it")
    if first < 0x80:
        length = first
    else:
        count = first & 0x7F
        if count > 4 or cursor + count > len(data):
            raise DerError(f"offset {pos}: impossible length-of-length {count}")
        length = int.from_bytes(data[cursor:cursor + count], "big")
        cursor += count
    end = cursor + length
    if end > len(data):
        raise DerError(f"offset {pos}: an element of {length} bytes runs past the end ({len(data) - cursor} bytes left)")
    return Tlv(tag, data[cursor:end], data[pos:end]), end


def read_all(data: bytes) -> List[Tlv]:
    items, pos = [], 0
    while pos < len(data):
        item, pos = read_tlv(data, pos)
        items.append(item)
    return items


def expect(tlv: Tlv, tag: int, what: str) -> Tlv:
    if tlv.tag != tag:
        raise DerError(f"{what}: expected tag 0x{tag:02x}, found 0x{tlv.tag:02x}")
    return tlv


def decode_oid(value: bytes) -> str:
    if not value or value[-1] & 0x80:
        raise DerError("malformed OBJECT IDENTIFIER")
    numbers, current = [], 0
    for byte in value:
        current = (current << 7) | (byte & 0x7F)
        if not byte & 0x80:
            numbers.append(current)
            current = 0
    arc = min(numbers[0] // 40, 2)
    return ".".join(str(n) for n in [arc, numbers[0] - 40 * arc] + numbers[1:])


def decode_int(value: bytes) -> int:
    if not value:
        raise DerError("empty INTEGER")
    return int.from_bytes(value, "big", signed=True)


def decode_time(tlv: Tlv) -> dt.datetime:
    text = tlv.value.decode("ascii", "replace")
    if tlv.tag == UTC_TIME:
        match = re.fullmatch(r"(\d{2})(\d{2})(\d{2})(\d{2})(\d{2})(\d{2})Z", text)
        if match:
            year = int(match.group(1))
            year += 1900 if year >= 50 else 2000
            return dt.datetime(year, *(int(g) for g in match.groups()[1:]), tzinfo=UTC)
    elif tlv.tag == GENERALIZED_TIME:
        match = re.fullmatch(r"(\d{4})(\d{2})(\d{2})(\d{2})(\d{2})(\d{2})(?:\.\d+)?Z", text)
        if match:
            return dt.datetime(*(int(g) for g in match.groups()), tzinfo=UTC)
    raise DerError(f"time {text!r} is not UTCTime YYMMDDHHMMSSZ or GeneralizedTime YYYYMMDDHHMMSSZ")


def decode_string(tlv: Tlv) -> str:
    codec = STRING_CODECS.get(tlv.tag)
    if codec is None:
        return "#" + tlv.raw.hex()
    return tlv.value.decode(codec, "replace")


def bit_string_bytes(tlv: Tlv) -> bytes:
    expect(tlv, BIT_STRING, "BIT STRING")
    return tlv.value[1:]


def escape_rfc4514(value: str) -> str:
    out = re.sub(r'([,+"\\<>;])', r"\\\1", value)
    if out.startswith(("#", " ")):
        out = "\\" + out
    if out.endswith(" ") and not out.endswith("\\ "):
        out = out[:-1] + "\\ "
    return out


@dataclass
class Name:
    rdns: List[List[Tuple[str, str]]]
    raw: bytes = b""

    def get(self, oid: str) -> List[str]:
        return [value for rdn in self.rdns for attr, value in rdn if attr == oid]

    def text(self) -> str:
        return ", ".join(" + ".join(f"{NAME_LABELS.get(o, o)}={v}" for o, v in rdn) for rdn in self.rdns)

    def rfc4514(self) -> str:
        parts = []
        for rdn in reversed(self.rdns):
            atoms = []
            for oid, value in rdn:
                name = NAME_LABELS.get(oid, oid)
                atoms.append(f"{name if name in RFC4514_LABELS else oid}={escape_rfc4514(value)}")
            parts.append("+".join(atoms))
        return ",".join(parts)

    def key(self) -> Tuple[Tuple[Tuple[str, str], ...], ...]:
        return tuple(tuple(sorted((o, " ".join(v.split()).casefold()) for o, v in rdn)) for rdn in self.rdns)


def parse_name(tlv: Tlv) -> Name:
    expect(tlv, SEQUENCE, "Name")
    rdns = []
    for rdn in tlv.children():
        expect(rdn, SET, "RelativeDistinguishedName")
        attributes = []
        for pair in rdn.children():
            kind, value = pair.children()[:2]
            attributes.append((decode_oid(expect(kind, OID, "attribute type").value), decode_string(value)))
        rdns.append(attributes)
    return Name(rdns, tlv.raw)


@dataclass
class PublicKey:
    algorithm: str
    bits: int
    rsa: Optional[Tuple[int, int]] = None
    curve: Optional[Curve] = None
    point: Optional[Tuple[int, int]] = None
    curve_name: str = ""


def parse_public_key(spki: Tlv) -> PublicKey:
    algorithm, key_bits = spki.children()[:2]
    parts = algorithm.children()
    oid = decode_oid(expect(parts[0], OID, "public key algorithm").value)
    key_bytes = bit_string_bytes(key_bits)
    if oid == RSA_OID:
        modulus, exponent = read_tlv(key_bytes)[0].children()[:2]
        n, e = decode_int(modulus.value), decode_int(exponent.value)
        return PublicKey("RSA", n.bit_length(), rsa=(n, e))
    if oid == EC_OID:
        curve_oid = decode_oid(parts[1].value) if len(parts) > 1 and parts[1].tag == OID else ""
        curve = CURVES.get(curve_oid)
        if curve is None:
            name = UNSUPPORTED_CURVES.get(curve_oid, curve_oid or "explicit parameters")
            return PublicKey("EC", 0, curve_name=name)
        return PublicKey("EC", curve.n.bit_length(), curve=curve, point=decode_point(curve, key_bytes), curve_name=curve.name)
    return PublicKey(KEY_ALGORITHMS.get(oid, oid), len(key_bytes) * 8)


@dataclass
class X509Extension:
    oid: str
    critical: bool
    value: bytes

    @property
    def name(self) -> str:
        return EXTENSION_NAMES.get(self.oid, self.oid)


@dataclass
class Certificate:
    der: bytes
    tbs: bytes
    version: int
    serial: int
    signature_oid: str
    signature: bytes
    issuer: Name
    subject: Name
    not_before: dt.datetime
    not_after: dt.datetime
    public_key: PublicKey
    extensions: List[X509Extension]
    dns_names: List[str] = field(default_factory=list)
    ip_addresses: List[str] = field(default_factory=list)
    emails: List[str] = field(default_factory=list)
    uris: List[str] = field(default_factory=list)
    is_ca: Optional[bool] = None
    path_len: Optional[int] = None
    key_usage: Optional[List[str]] = None
    ext_key_usage: Optional[List[str]] = None
    subject_key_id: Optional[bytes] = None
    authority_key_id: Optional[bytes] = None
    authority_serial: Optional[int] = None
    ca_issuers: List[str] = field(default_factory=list)
    ocsp: List[str] = field(default_factory=list)

    @property
    def fingerprint(self) -> str:
        return colon_hex(hashlib.sha256(self.der).digest())

    @property
    def signature_name(self) -> str:
        return SIGNATURE_ALGORITHMS.get(self.signature_oid, ("", "", self.signature_oid))[2]

    @property
    def label(self) -> str:
        names = self.subject.get(CN_OID)
        return names[-1] if names else (self.subject.text() or "(empty subject)")


def colon_hex(data: bytes) -> str:
    return ":".join(f"{b:02X}" for b in data)


def parse_extension(tlv: Tlv) -> X509Extension:
    parts = expect(tlv, SEQUENCE, "Extension").children()
    oid = decode_oid(expect(parts[0], OID, "extnID").value)
    critical = len(parts) == 3 and expect(parts[1], BOOLEAN, "critical").value != b"\x00"
    return X509Extension(oid, critical, expect(parts[-1], OCTET_STRING, "extnValue").value)


def apply_extension(cert: Certificate, ext: X509Extension) -> None:
    inner = read_tlv(ext.value)[0]
    if ext.oid == EXT_SAN:
        for name in inner.children():
            if name.tag == 0x82:
                cert.dns_names.append(name.value.decode("ascii", "replace"))
            elif name.tag == 0x87:
                cert.ip_addresses.append(str(ipaddress.ip_address(name.value)))
            elif name.tag == 0x81:
                cert.emails.append(name.value.decode("ascii", "replace"))
            elif name.tag == 0x86:
                cert.uris.append(name.value.decode("ascii", "replace"))
    elif ext.oid == EXT_BC:
        parts = inner.children()
        cert.is_ca = bool(parts and parts[0].tag == BOOLEAN and parts[0].value != b"\x00")
        lengths = [decode_int(p.value) for p in parts if p.tag == INTEGER]
        cert.path_len = lengths[0] if lengths else None
    elif ext.oid == EXT_KU:
        raw = bit_string_bytes(inner) or b"\x00"
        bits, width = int.from_bytes(raw, "big"), len(raw) * 8
        cert.key_usage = [n for i, n in enumerate(KEY_USAGE_BITS) if i < width and bits >> (width - 1 - i) & 1]
    elif ext.oid == EXT_EKU:
        cert.ext_key_usage = [EKU_NAMES.get(o, o) for o in (decode_oid(item.value) for item in inner.children())]
    elif ext.oid == EXT_SKI:
        cert.subject_key_id = expect(inner, OCTET_STRING, "subjectKeyIdentifier").value
    elif ext.oid == EXT_AKI:
        for part in inner.children():
            if part.tag == 0x80:
                cert.authority_key_id = part.value
            elif part.tag == 0x82:
                cert.authority_serial = decode_int(part.value)
    elif ext.oid == EXT_AIA:
        for access in inner.children():
            method, location = access.children()[:2]
            target = cert.ocsp if decode_oid(method.value) == OCSP_OID else cert.ca_issuers
            if location.tag == 0x86:
                target.append(location.value.decode("ascii", "replace"))


def parse_certificate(data: bytes) -> Certificate:
    try:
        outer, _ = read_tlv(data)
        parts = expect(outer, SEQUENCE, "Certificate").children()
        if len(parts) != 3:
            raise DerError(f"a Certificate has 3 parts (tbsCertificate, signatureAlgorithm, signatureValue), this one has {len(parts)}")
        tbs, algorithm, signature = parts
        fields = expect(tbs, SEQUENCE, "tbsCertificate").children()
        version, index = 1, 0
        if fields[0].tag == 0xA0:
            version, index = decode_int(fields[0].children()[0].value) + 1, 1
        serial = decode_int(expect(fields[index], INTEGER, "serialNumber").value)
        not_before, not_after = (decode_time(t) for t in fields[index + 3].children()[:2])
        cert = Certificate(
            der=outer.raw,
            tbs=tbs.raw,
            version=version,
            serial=serial,
            signature_oid=decode_oid(algorithm.children()[0].value),
            signature=bit_string_bytes(signature),
            issuer=parse_name(fields[index + 2]),
            subject=parse_name(fields[index + 4]),
            not_before=not_before,
            not_after=not_after,
            public_key=parse_public_key(fields[index + 5]),
            extensions=[parse_extension(e) for f in fields[index + 6:] if f.tag == 0xA3 for e in f.children()[0].children()],
        )
        for ext in cert.extensions:
            apply_extension(cert, ext)
        return cert
    except (IndexError, ValueError, UnicodeDecodeError) as exc:
        if isinstance(exc, InputError):
            raise
        raise DerError(f"not a well-formed X.509 certificate ({exc})")


PEM_BLOCK = re.compile(rb"-----BEGIN ((?:TRUSTED )?CERTIFICATE)-----(.*?)-----END \1-----", re.S)


def load_certificates(data: bytes, source: str, lenient: bool = False) -> List[Certificate]:
    if b"-----BEGIN" in data:
        blocks = PEM_BLOCK.findall(data)
        if not blocks:
            raise InputError(f"{source}: PEM without a CERTIFICATE block (a private key or a CSR?)")
        try:
            ders = [read_tlv(base64.b64decode(b"".join(body.split()), validate=True))[0].raw for _, body in blocks]
        except binascii.Error as exc:
            raise InputError(f"{source}: broken base64 in a PEM block ({exc})")
    else:
        ders = [t.raw for t in read_all(data)]
    certs = []
    for index, der_bytes in enumerate(ders):
        try:
            certs.append(parse_certificate(der_bytes))
        except InputError as exc:
            if not lenient:
                raise InputError(f"{source}: certificate {index}: {exc}")
    return certs


def to_pem(der_bytes: bytes) -> str:
    text = base64.b64encode(der_bytes).decode()
    return "-----BEGIN CERTIFICATE-----\n" + "\n".join(text[i:i + 64] for i in range(0, len(text), 64)) + "\n-----END CERTIFICATE-----\n"


@dataclass
class Policy:
    at: dt.datetime
    host: Optional[str] = None
    purpose: str = "sslserver"
    partial_chain: bool = False
    cn_fallback: bool = False
    max_depth: int = 100


@dataclass
class VerifyError:
    code: int
    depth: int
    subject: str
    detail: str = ""

    @property
    def message(self) -> str:
        return VERIFY_MESSAGES[self.code]

    def openssl_line(self) -> str:
        return f"error {self.code} at {self.depth} depth lookup: {self.message}"


@dataclass
class Verdict:
    chain: List[Certificate]
    trusted: bool
    errors: List[VerifyError]
    warnings: List[str]
    policy: Policy
    host_match: str = ""

    @property
    def ok(self) -> bool:
        return not self.errors

    @property
    def code(self) -> int:
        return self.errors[0].code if self.errors else 0


def issued_by(child: Certificate, issuer: Certificate) -> bool:
    if child.issuer.key() != issuer.subject.key():
        return False
    if child.authority_key_id and issuer.subject_key_id and child.authority_key_id != issuer.subject_key_id:
        return False
    return child.authority_serial is None or child is issuer or child.authority_serial == issuer.serial


def self_signed(cert: Certificate) -> bool:
    return issued_by(cert, cert)


def valid_at(cert: Certificate, at: dt.datetime) -> bool:
    return cert.not_before <= at <= cert.not_after


def find_issuer(cert: Certificate, pool: Sequence[Certificate], chain: Sequence[Certificate], at: dt.datetime) -> Optional[Certificate]:
    used = {c.fingerprint for c in chain}
    candidates = [c for c in pool if c.fingerprint not in used and issued_by(cert, c)]
    current = [c for c in candidates if valid_at(c, at)]
    return (current or candidates or [None])[0]


def build_chain(leaf: Certificate, trusted: Sequence[Certificate], untrusted: Sequence[Certificate], policy: Policy) -> Tuple[List[Certificate], bool, List[VerifyError]]:
    anchors = {c.fingerprint for c in trusted}
    chain = [leaf]
    while True:
        current = chain[-1]
        if current.fingerprint in anchors and (self_signed(current) or policy.partial_chain):
            return chain, True, []
        if self_signed(current):
            break
        if len(chain) > policy.max_depth:
            return chain, False, [VerifyError(22, len(chain) - 1, current.subject.text(), f"more than {policy.max_depth} certificates")]
        issuer = find_issuer(current, trusted, chain, policy.at) or find_issuer(current, untrusted, chain, policy.at)
        if issuer is None:
            break
        chain.append(issuer)
    top, depth = chain[-1], len(chain) - 1
    if self_signed(top):
        code = 18 if depth == 0 else 19
        detail = f"the chain ends at {top.label!r}, which signs itself but is not in the trust store"
    elif any(c.fingerprint in anchors for c in chain):
        code = 2
        detail = f"{top.label!r} is trusted, but its issuer {top.issuer.text()} is not; add the root or use --partial-chain"
    else:
        code = 20
        detail = f"no certificate named {top.issuer.text()} among the presented, untrusted or trusted certificates"
        if top.ca_issuers:
            detail += f"; its authorityInfoAccess points to {top.ca_issuers[0]}"
    return chain, False, [VerifyError(code, depth, top.subject.text(), detail)]


def acts_as_ca(cert: Certificate) -> bool:
    return bool(cert.is_ca) or (cert.version == 1 and self_signed(cert))


def purpose_problem(cert: Certificate, depth: int) -> str:
    eku = cert.ext_key_usage
    if eku is not None and not {"serverAuth", "anyExtendedKeyUsage"} & set(eku):
        return f"extendedKeyUsage is {', '.join(eku)}, not serverAuth"
    usage = cert.key_usage
    if depth == 0 and usage is not None and not {"digitalSignature", "keyEncipherment", "keyAgreement"} & set(usage):
        return f"keyUsage {', '.join(usage) or '(empty)'} allows no TLS key exchange or signature"
    return ""


def check_extensions(chain: Sequence[Certificate], policy: Policy) -> List[VerifyError]:
    errors: List[VerifyError] = []
    below = 0
    for depth, cert in enumerate(chain):
        subject = cert.subject.text()
        for ext in cert.extensions:
            if ext.critical and ext.oid not in HANDLED_CRITICAL:
                errors.append(VerifyError(34, depth, subject, f"{ext.name} is critical and tlslens does not implement it"))
        if depth > 0 and not acts_as_ca(cert):
            errors.append(VerifyError(79, depth, subject, "basicConstraints CA:TRUE is missing, so it may not sign certificates"))
        elif depth > 0 and cert.key_usage is not None and "keyCertSign" not in cert.key_usage:
            errors.append(VerifyError(32, depth, subject, f"keyUsage is {', '.join(cert.key_usage)}"))
        problem = purpose_problem(cert, depth) if policy.purpose == "sslserver" else ""
        if problem:
            errors.append(VerifyError(26, depth, subject, problem))
        if depth > 1 and cert.path_len is not None and below > cert.path_len:
            errors.append(VerifyError(25, depth, subject, f"pathlen:{cert.path_len} allows {cert.path_len} CA certificates between it and the leaf, the chain has {below}"))
        if depth > 0 and not self_signed(cert):
            below += 1
    return errors


def hostname_matches(pattern: str, host: str) -> bool:
    pattern, host = pattern.rstrip(".").lower(), host.rstrip(".").lower()
    if not pattern or not host:
        return False
    if "*" not in pattern:
        return pattern == host
    if not pattern.startswith("*.") or "*" in pattern[2:] or "." not in pattern[2:]:
        return False
    first, dot, rest = host.partition(".")
    return bool(first) and bool(dot) and rest == pattern[2:]


def match_host(cert: Certificate, host: str, cn_fallback: bool = False) -> Tuple[str, Optional[VerifyError]]:
    subject = cert.subject.text()
    if is_ip(host):
        wanted = ipaddress.ip_address(host.strip("[]"))
        for address in cert.ip_addresses:
            if ipaddress.ip_address(address) == wanted:
                return f"IP:{address}", None
        return "", VerifyError(64, 0, subject, f"{wanted} is not among the IP SANs [{', '.join(cert.ip_addresses)}]; IP addresses never match DNS names")
    names = cert.dns_names or (cert.subject.get(CN_OID) if cn_fallback else [])
    for pattern in names:
        if hostname_matches(pattern, host):
            return f"DNS:{pattern}", None
    if not cert.dns_names:
        detail = f"no DNS names in subjectAltName; the CN {cert.label!r} is ignored unless --cn-fallback"
    else:
        detail = f"{host} is not covered by DNS:{', DNS:'.join(cert.dns_names)}"
    return "", VerifyError(62, 0, subject, detail)


def signature_problem(cert: Certificate, issuer: Certificate) -> Optional[Tuple[int, str]]:
    kind, hash_name, name = SIGNATURE_ALGORITHMS.get(cert.signature_oid, ("", "", cert.signature_oid))
    key = issuer.public_key
    if kind not in ("RSA", "EC"):
        return 76, f"tlslens checks RSA PKCS#1 v1.5 and ECDSA signatures, not {name}"
    if key.algorithm != kind:
        return 7, f"signed with {name} but the issuer's key is {key.algorithm}"
    if kind == "RSA" and key.rsa:
        ok = rsa_pkcs1_verify(key.rsa[0], key.rsa[1], cert.signature, hash_name, cert.tbs)
    elif key.curve is None or key.point is None:
        return 76, f"tlslens checks ECDSA on P-256 and P-384, not on {key.curve_name}"
    else:
        try:
            r_tlv, s_tlv = read_tlv(cert.signature)[0].children()[:2]
            r, s = decode_int(r_tlv.value), decode_int(s_tlv.value)
        except (DerError, ValueError):
            return 7, "the ECDSA signature is not a DER SEQUENCE of r and s"
        ok = ecdsa_verify(key.curve, key.point, hashlib.new(hash_name, cert.tbs).digest(), r, s)
    return None if ok else (7, f"{name} does not verify with the key of {issuer.label!r}")


def iso(moment: dt.datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def time_errors(cert: Certificate, depth: int, at: dt.datetime) -> List[VerifyError]:
    if at < cert.not_before:
        days = (cert.not_before - at).days
        return [VerifyError(9, depth, cert.subject.text(), f"notBefore {iso(cert.not_before)} is {days} days after {iso(at)}")]
    if at > cert.not_after:
        days = (at - cert.not_after).days
        return [VerifyError(10, depth, cert.subject.text(), f"notAfter {iso(cert.not_after)} was {days} days before {iso(at)}")]
    return []


def check_signatures_and_times(chain: Sequence[Certificate], trusted: bool, at: dt.datetime) -> List[VerifyError]:
    errors: List[VerifyError] = []
    top = len(chain) - 1
    for depth in range(top, -1, -1):
        cert = chain[depth]
        if depth < top:
            problem = signature_problem(cert, chain[depth + 1])
            if problem:
                errors.append(VerifyError(problem[0], depth, cert.subject.text(), problem[1]))
        elif depth == 0 and not trusted and not self_signed(cert):
            errors.append(VerifyError(21, 0, cert.subject.text(), "the only certificate has no issuer to check its signature against"))
        errors.extend(time_errors(cert, depth, at))
    return errors


def chain_warnings(chain: Sequence[Certificate], at: dt.datetime) -> List[str]:
    warnings = []
    leaf = chain[0]
    if not leaf.dns_names and not leaf.ip_addresses:
        warnings.append("the leaf has no subjectAltName; browsers, Go and curl ignore the CN, so it matches no hostname")
    remaining = (leaf.not_after - at).days
    if 0 <= remaining <= 30:
        warnings.append(f"the leaf expires in {remaining} days ({iso(leaf.not_after)})")
    for depth, cert in enumerate(chain):
        if SIGNATURE_ALGORITHMS.get(cert.signature_oid, ("", "", ""))[1] == "sha1" and not self_signed(cert):
            warnings.append(f"depth {depth}: SHA-1 signature, rejected by current browsers and TLS libraries")
        if cert.public_key.algorithm == "RSA" and cert.public_key.bits < 2048:
            warnings.append(f"depth {depth}: {cert.public_key.bits}-bit RSA key; public CAs require at least 2048")
    return warnings


def verify(presented: Sequence[Certificate], trusted: Sequence[Certificate], untrusted: Sequence[Certificate] = (), policy: Optional[Policy] = None) -> Verdict:
    if not presented:
        raise InputError("no certificate to verify")
    policy = policy or Policy(at=dt.datetime.now(UTC))
    leaf = presented[0]
    chain, anchored, errors = build_chain(leaf, trusted, list(presented[1:]) + list(untrusted), policy)
    errors += check_extensions(chain, policy)
    matched = ""
    if policy.host:
        matched, problem = match_host(leaf, policy.host, policy.cn_fallback)
        if problem:
            errors.append(problem)
    errors += check_signatures_and_times(chain, anchored, policy.at)
    return Verdict(chain, anchored, errors, chain_warnings(chain, policy.at), policy, matched)


def certificate_summary(cert: Certificate) -> Dict[str, Any]:
    key = cert.public_key
    return {
        "subject": cert.subject.text(),
        "subject_rfc4514": cert.subject.rfc4514(),
        "issuer": cert.issuer.text(),
        "issuer_rfc4514": cert.issuer.rfc4514(),
        "serial": f"{cert.serial:X}",
        "version": cert.version,
        "not_before": iso(cert.not_before),
        "not_after": iso(cert.not_after),
        "public_key": {"algorithm": key.algorithm, "bits": key.bits, "curve": key.curve_name or None},
        "signature_algorithm": cert.signature_name,
        "dns_names": cert.dns_names,
        "ip_addresses": cert.ip_addresses,
        "emails": cert.emails,
        "uris": cert.uris,
        "is_ca": cert.is_ca,
        "path_len": cert.path_len,
        "key_usage": cert.key_usage,
        "ext_key_usage": cert.ext_key_usage,
        "subject_key_id": colon_hex(cert.subject_key_id) if cert.subject_key_id else None,
        "authority_key_id": colon_hex(cert.authority_key_id) if cert.authority_key_id else None,
        "ca_issuers": cert.ca_issuers,
        "ocsp": cert.ocsp,
        "critical": [e.name for e in cert.extensions if e.critical],
        "self_signed": self_signed(cert),
        "sha256_fingerprint": cert.fingerprint,
    }


def verdict_json(verdict: Verdict) -> Dict[str, Any]:
    return {
        "ok": verdict.ok,
        "verify_code": verdict.code,
        "verify_message": VERIFY_MESSAGES[verdict.code],
        "trusted": verdict.trusted,
        "host": verdict.policy.host,
        "host_match": verdict.host_match or None,
        "at": iso(verdict.policy.at),
        "errors": [{"code": e.code, "depth": e.depth, "message": e.message, "subject": e.subject, "detail": e.detail} for e in verdict.errors],
        "warnings": verdict.warnings,
        "chain": [certificate_summary(c) for c in verdict.chain],
    }


def render_hello(hello: Dict[str, Any]) -> List[str]:
    if hello["type"] == "client_hello":
        lines = [f"client_hello  offers {', '.join(hello['versions'])} (legacy_version {hello['legacy_version']})"]
        rows = [
            ("sni", hello["sni"] or "(none: the server cannot pick a certificate by name)"),
            ("alpn", ", ".join(hello["alpn"]) or "(none)"),
            ("cipher suites", f"{len(hello['cipher_suites'])}: " + ", ".join(hello["cipher_suites"])),
            ("groups", ", ".join(hello["groups"])),
            ("key shares", ", ".join(f"{k['group']} ({k['bytes']} bytes)" for k in hello["key_shares"]) or "(none)"),
            ("signatures", ", ".join(hello["signature_algorithms"])),
            ("extensions", ", ".join(hello["extensions"])),
            ("ja3", f"{hello['ja3_md5']}  {hello['ja3']}"),
        ]
    else:
        lines = [f"{hello['type']}  {hello['version']} (from {hello['version_from']}), cipher {hello['cipher_suite']}"]
        rows = [
            ("key share", hello["key_share"] or "(none: TLS 1.2 sends the ECDHE key in ServerKeyExchange)"),
            ("alpn", hello["alpn"] or ("(not here: TLS 1.3 sends it in the encrypted EncryptedExtensions)" if hello["version"] == "TLS 1.3" else "(none)")),
            ("extensions", ", ".join(hello["extensions"]) or "(none)"),
        ]
        if hello["downgrade_sentinel"]:
            rows.append(("downgrade", hello["downgrade_sentinel"]))
    return lines + [f"  {name:<14} {value}" for name, value in rows]


def render_flight(flight: Dict[str, Any]) -> str:
    lines = [f"{flight['bytes']} bytes, {len(flight['records'])} records: {', '.join(flight['records']) or '(none complete)'}"]
    if flight["handshake"]:
        lines.append(f"handshake messages in clear: {', '.join(flight['handshake'])}")
    for hello in flight["hellos"]:
        lines.extend(render_hello(hello))
    if flight["key_exchange_group"]:
        lines.append(f"server_key_exchange  ECDHE on {flight['key_exchange_group']}")
    for index, cert in enumerate(flight["certificates"]):
        lines.append(f"certificate {index}  {cert['subject']}  (issuer {cert['issuer']})")
    for alert in flight["alerts"]:
        lines.append(f"alert  {alert['level']} {alert['description']} ({alert['code']})")
    if flight["encrypted_records"]:
        lines.append(f"then {flight['encrypted_records']} encrypted records: tlslens cannot read past this point")
    return "\n".join(lines)


def critical_mark(cert: Certificate, oid: str) -> str:
    return " (critical)" if any(e.oid == oid and e.critical for e in cert.extensions) else ""


def render_certificate(index: int, cert: Certificate) -> str:
    key = cert.public_key
    san = [f"DNS:{n}" for n in cert.dns_names] + [f"IP:{a}" for a in cert.ip_addresses] + [f"email:{e}" for e in cert.emails] + [f"URI:{u}" for u in cert.uris]
    rows = [
        ("issuer", cert.issuer.text()),
        ("serial", f"{cert.serial:X}"),
        ("validity", f"{iso(cert.not_before)} to {iso(cert.not_after)}"),
        ("public key", f"{key.algorithm} {key.bits} bits" + (f" on {key.curve_name}" if key.curve_name else "")),
        ("signature", cert.signature_name),
        ("SAN", ", ".join(san) + critical_mark(cert, EXT_SAN) if san else "(none)"),
    ]
    if cert.is_ca is not None:
        rows.append(("basic", ("CA:TRUE" if cert.is_ca else "CA:FALSE") + (f", pathlen:{cert.path_len}" if cert.path_len is not None else "") + critical_mark(cert, EXT_BC)))
    if cert.key_usage is not None:
        rows.append(("key usage", ", ".join(cert.key_usage) + critical_mark(cert, EXT_KU)))
    if cert.ext_key_usage is not None:
        rows.append(("ext usage", ", ".join(cert.ext_key_usage) + critical_mark(cert, EXT_EKU)))
    if cert.subject_key_id:
        rows.append(("SKI", colon_hex(cert.subject_key_id)))
    if cert.authority_key_id:
        rows.append(("AKI", colon_hex(cert.authority_key_id)))
    if cert.ca_issuers or cert.ocsp:
        rows.append(("AIA", ", ".join([f"caIssuers {u}" for u in cert.ca_issuers] + [f"OCSP {u}" for u in cert.ocsp])))
    rows.append(("sha256", cert.fingerprint))
    return "\n".join([f"certificate {index}: {cert.subject.text() or '(empty subject)'}"] + [f"  {name:<11} {value}" for name, value in rows])


def render_verdict(verdict: Verdict) -> str:
    state = "ends at a trust anchor" if verdict.trusted else "does not reach a trust anchor"
    lines = [f"chain of {len(verdict.chain)} {'certificate' if len(verdict.chain) == 1 else 'certificates'}, {state}"]
    for depth, cert in enumerate(verdict.chain):
        note = "trust anchor" if verdict.trusted and depth == len(verdict.chain) - 1 else f"until {iso(cert.not_after)}"
        lines.append(f"  {depth}  {cert.subject.text()}  [{cert.public_key.algorithm} {cert.public_key.bits}, {cert.signature_name}, {note}]")
    if verdict.host_match:
        lines.append(f"host {verdict.policy.host} matches {verdict.host_match}")
    for error in verdict.errors:
        lines.append(error.subject)
        lines.append(error.openssl_line())
        if error.detail:
            lines.append(f"  {error.detail}")
    for warning in verdict.warnings:
        lines.append(f"warning: {warning}")
    lines.append("OK" if verdict.ok else f"verification failed: error {verdict.code} ({VERIFY_MESSAGES[verdict.code]})")
    return "\n".join(lines)


def read_input(path: str) -> bytes:
    try:
        if path == "-":
            return sys.stdin.buffer.read()
        with open(path, "rb") as handle:
            return handle.read()
    except OSError as exc:
        raise InputError(f"cannot read {path}: {exc.strerror}")


def load_bytes(path: str) -> bytes:
    data = read_input(path)
    text = data.strip()
    if text and re.fullmatch(rb"[0-9a-fA-F:\s]+", text):
        try:
            return bytes.fromhex(re.sub(rb"[:\s]", b"", text).decode())
        except ValueError:
            raise InputError(f"{path}: looks like hex but has an odd number of digits")
    return data


def split_address(text: str, default_port: int = 443) -> Tuple[str, int]:
    match = re.fullmatch(r"\[([^\]]+)\](?::(\d+))?|([^:]+)(?::(\d+))?", text)
    if not match:
        raise InputError(f"{text!r} is not HOST:PORT")
    host = match.group(1) or match.group(3)
    port = match.group(2) or match.group(4)
    return host, int(port) if port else default_port


def parse_at(text: Optional[str]) -> dt.datetime:
    if not text:
        return dt.datetime.now(UTC)
    if re.fullmatch(r"@?\d+", text):
        return dt.datetime.fromtimestamp(int(text.lstrip("@")), UTC)
    try:
        moment = dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        raise InputError(f"--at {text!r}: use 2027-06-01, 2027-06-01T12:00:00Z or @EPOCH")
    return moment if moment.tzinfo else moment.replace(tzinfo=UTC)


def trust_store(paths: Sequence[str]) -> List[Certificate]:
    if paths:
        anchors = [c for path in paths for c in load_certificates(read_input(path), path)]
        if not anchors:
            raise InputError(f"no certificates in {', '.join(paths)}")
        return anchors
    for path in DEFAULT_BUNDLES:
        if os.path.exists(path):
            return load_certificates(read_input(path), path, lenient=True)
    raise InputError("no --CAfile given and no system CA bundle found in " + ", ".join(DEFAULT_BUNDLES))


def write_file(path: str, data: bytes) -> None:
    try:
        with open(path, "wb") as handle:
            handle.write(data)
    except OSError as exc:
        raise InputError(f"cannot write {path}: {exc.strerror}")


def emit(args: argparse.Namespace, payload: Dict[str, Any], text: str) -> None:
    print(json.dumps(payload, indent=2) if args.json else text, flush=True)


def command_hello(args: argparse.Namespace) -> int:
    flight = describe_flight(load_bytes(args.file))
    emit(args, flight, render_flight(flight))
    return 0


def command_listen(args: argparse.Namespace) -> int:
    upstream = split_address(args.upstream) if args.upstream else None
    listener = make_listener(args.host, args.port)
    with listener:
        print(f"listening on {args.host}:{listener.getsockname()[1]}", file=sys.stderr, flush=True)
        client_bytes, server_bytes = capture(listener, upstream, ALERT_CODES[args.alert], args.timeout, args.wait)
    write_file(args.out, client_bytes)
    if args.server_out:
        write_file(args.server_out, server_bytes)
    client = describe_flight(client_bytes)
    server = describe_flight(server_bytes) if upstream else None
    text = "client sent:\n" + render_flight(client)
    if server is not None:
        text += "\nserver sent:\n" + render_flight(server)
    emit(args, {"client": client, "server": server}, text)
    return 0


def command_probe(args: argparse.Namespace) -> int:
    host, port = split_address(args.address)
    flight_bytes = probe(host, port, args.servername or host, [p for p in args.alpn.split(",") if p] if args.alpn else [], args.timeout)
    if args.save_flight:
        write_file(args.save_flight, flight_bytes)
    flight = describe_flight(flight_bytes)
    if args.save_chain:
        write_file(args.save_chain, "".join(to_pem(c.der) for c in flight_certificates(flight_bytes)).encode())
    emit(args, flight, render_flight(flight))
    return 0 if flight["certificates"] else 1


def presented_chain(args: argparse.Namespace) -> List[Certificate]:
    if args.connect:
        host, port = split_address(args.connect)
        flight_bytes = probe(host, port, args.servername or host, [p for p in args.alpn.split(",") if p] if args.alpn else [], args.timeout)
        certs = flight_certificates(flight_bytes)
        fatal = [code for level, code in read_flight(flight_bytes).alerts if level == 2]
        if not certs and fatal:
            raise InputError(f"{args.connect} answered the TLS 1.2 ClientHello with alert {label(ALERTS, fatal[0])} ({fatal[0]}); a TLS 1.3-only server hides its chain, use openssl s_client -showcerts")
        if not certs:
            raise InputError(f"{args.connect} sent no Certificate message")
        return certs
    if not args.file:
        raise InputError("give a certificate file or --connect HOST:PORT")
    return load_certificates(read_input(args.file), args.file)


def command_verify(args: argparse.Namespace) -> int:
    presented = presented_chain(args)
    untrusted = [c for path in args.untrusted for c in load_certificates(read_input(path), path)]
    host = args.host or (args.servername if args.connect else None)
    policy = Policy(at=parse_at(args.at), host=host, purpose=args.purpose, partial_chain=args.partial_chain, cn_fallback=args.cn_fallback)
    verdict = verify(presented, trust_store(args.cafile), untrusted, policy)
    emit(args, verdict_json(verdict), render_verdict(verdict))
    return 0 if verdict.ok else 1


def command_x509(args: argparse.Namespace) -> int:
    certs = [c for path in args.files for c in load_certificates(read_input(path), path)]
    emit(args, {"certificates": [certificate_summary(c) for c in certs]}, "\n".join(render_certificate(i, c) for i, c in enumerate(certs)))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="tlslens", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    hello = sub.add_parser("hello", help="decode captured TLS bytes (raw or hex): records, hellos, alerts, a TLS 1.2 certificate chain")
    hello.add_argument("file", help="file with the bytes one side sent; - reads stdin")
    listen = sub.add_parser("listen", help="accept one connection and record what the client sends; answer with an alert, or relay to --upstream and record both sides")
    listen.add_argument("--host", default="127.0.0.1")
    listen.add_argument("--port", type=int, default=0, help="0 picks a free port and prints it")
    listen.add_argument("--out", required=True, help="where to write the client's bytes")
    listen.add_argument("--upstream", help="HOST:PORT of a real TLS server to relay to")
    listen.add_argument("--server-out", help="where to write the server's bytes (with --upstream)")
    listen.add_argument("--alert", default="handshake_failure", choices=sorted(ALERT_CODES), help="alert sent back without --upstream")
    listen.add_argument("--timeout", type=float, default=10.0, help="seconds of silence before giving up on a connection")
    listen.add_argument("--wait", type=float, default=60.0, help="seconds to wait for a client to connect")
    probe_cmd = sub.add_parser("probe", help="send a TLS 1.2 ClientHello and read ServerHello to ServerHelloDone, where the chain is still in clear")
    probe_cmd.add_argument("address", help="HOST:PORT")
    probe_cmd.add_argument("--save-chain", help="write the server's certificates as PEM")
    probe_cmd.add_argument("--save-flight", help="write the server's raw bytes")
    x509 = sub.add_parser("x509", help="print the fields of PEM or DER certificates")
    x509.add_argument("files", nargs="+")
    check = sub.add_parser("verify", help="build and verify a chain like openssl verify; the first certificate in FILE is the leaf, the rest are untrusted")
    check.add_argument("file", nargs="?", help="PEM or DER: leaf first, then intermediates (openssl s_client -showcerts output works)")
    check.add_argument("--connect", help="HOST:PORT: fetch the chain with a TLS 1.2 probe instead of reading FILE")
    check.add_argument("--CAfile", dest="cafile", action="append", default=[], help="trust anchors (repeatable); default: the system bundle")
    check.add_argument("--untrusted", action="append", default=[], help="extra intermediates (repeatable)")
    check.add_argument("--host", help="hostname or IP address the leaf must match")
    check.add_argument("--at", help="check validity at this time (2027-06-01, ISO 8601 or @EPOCH) instead of now")
    check.add_argument("--purpose", choices=("sslserver", "any"), default="sslserver")
    check.add_argument("--partial-chain", action="store_true", help="accept a trusted intermediate as the end of the chain")
    check.add_argument("--cn-fallback", action="store_true", help="match the CN when there are no DNS SANs, as OpenSSL does and browsers do not")
    for command in (probe_cmd, check):
        command.add_argument("--servername", help="SNI to send (default: the host part of the address)")
        command.add_argument("--alpn", help="comma-separated protocols to offer, e.g. h2,http/1.1")
        command.add_argument("--timeout", type=float, default=10.0)
    for command in (hello, listen, probe_cmd, x509, check):
        command.add_argument("--json", action="store_true", help="machine-readable output")
    return parser


COMMANDS = {"hello": command_hello, "listen": command_listen, "probe": command_probe, "x509": command_x509, "verify": command_verify}


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(list(sys.argv[1:] if argv is None else argv))
    try:
        return COMMANDS[args.command](args)
    except InputError as exc:
        print(f"tlslens: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
