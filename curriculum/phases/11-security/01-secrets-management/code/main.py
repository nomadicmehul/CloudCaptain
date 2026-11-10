"""minivault: a secrets service that speaks a subset of OpenBao's HTTP API, built from the standard library. A versioned KV store (the KV v2 model), envelope encryption with ChaCha20-Poly1305 from RFC 8439, Shamir secret sharing over GF(2^8) for the unseal key, tokens with TTLs and parent-child revocation, path policies with globs and segment wildcards, dynamic database credentials as leases, and an audit log that HMACs every secret value."""
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
import shlex
import signal
import string
import struct
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple
from urllib import error as urlerror
from urllib import request as urlrequest
from urllib.parse import parse_qs, urlsplit

MASK32 = 0xFFFFFFFF
SIGMA = (0x61707865, 0x3320646E, 0x79622D32, 0x6B206574)
QUARTER_ROUNDS = (
    (0, 4, 8, 12),
    (1, 5, 9, 13),
    (2, 6, 10, 14),
    (3, 7, 11, 15),
    (0, 5, 10, 15),
    (1, 6, 11, 12),
    (2, 7, 8, 13),
    (3, 4, 9, 14),
)
POLY1305_PRIME = (1 << 130) - 5
POLY1305_CLAMP = 0x0FFFFFFC0FFFFFFC0FFFFFFC0FFFFFFF
KEY_SIZE = 32
NONCE_SIZE = 12
TAG_SIZE = 16
GF_REDUCTION = 0x11B
ALNUM = string.ascii_letters + string.digits
SYSTEM_MAX_TTL = 768 * 3600
DEFAULT_MAX_VERSIONS = 10
CAPABILITY_ORDER = ("sudo", "read", "list", "update", "delete", "create", "patch")
VALID_CAPABILITIES = frozenset(("deny", "create", "read", "update", "delete", "list", "sudo", "patch"))
OLD_STYLE_POLICIES = {
    "deny": ["deny"],
    "read": ["read", "list"],
    "write": ["create", "read", "update", "delete", "list"],
    "sudo": ["create", "read", "update", "delete", "list", "sudo"],
}
UNAUTHENTICATED_PATHS = ("sys/seal-status", "sys/unseal", "sys/init", "sys/health")
SUDO_PATHS = ("auth/token/revoke-orphan", "sys/rotate", "sys/rewrap", "sys/seal")
SUDO_PREFIXES = ("sys/leases/revoke-prefix/",)
STATE_FILE = "minivault.json"
AUDIT_DEVICE = "file"
DEFAULT_POLICY = """
path "auth/token/lookup-self" {
  capabilities = ["read"]
}
path "auth/token/renew-self" {
  capabilities = ["update"]
}
path "auth/token/revoke-self" {
  capabilities = ["update"]
}
path "sys/capabilities-self" {
  capabilities = ["update"]
}
path "sys/leases/renew" {
  capabilities = ["update"]
}
path "sys/leases/lookup" {
  capabilities = ["update"]
}
path "cubbyhole/*" {
  capabilities = ["create", "read", "update", "delete", "list"]
}
"""
DEFAULT_RENEW_STATEMENTS = ["ALTER ROLE \"{{name}}\" VALID UNTIL '{{expiration}}';"]
DEFAULT_REVOCATION_STATEMENTS = [
    'REVOKE ALL PRIVILEGES ON ALL TABLES IN SCHEMA public FROM "{{name}}";',
    'DROP ROLE IF EXISTS "{{name}}";',
]


class InputError(ValueError):
    pass


class DecryptionError(ValueError):
    pass


class EngineError(RuntimeError):
    pass


class ApiError(Exception):
    def __init__(self, status: int, message: str = "") -> None:
        super().__init__(message)
        self.status = status
        self.message = message


def b64e(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def b64d(text: str, what: str = "value") -> bytes:
    try:
        return base64.b64decode(text, validate=True)
    except (binascii.Error, ValueError, TypeError) as exc:
        raise InputError(f"{what}: not base64 ({exc})") from None


def decode_key_material(text: str, what: str = "key") -> bytes:
    cleaned = text.strip()
    if re.fullmatch(r"(?:[0-9a-fA-F]{2})+", cleaned):
        return bytes.fromhex(cleaned)
    return b64d(cleaned, what)


def rotl32(value: int, count: int) -> int:
    return ((value << count) & MASK32) | (value >> (32 - count))


def quarter_round(a: int, b: int, c: int, d: int) -> Tuple[int, int, int, int]:
    a = (a + b) & MASK32
    d = rotl32(d ^ a, 16)
    c = (c + d) & MASK32
    b = rotl32(b ^ c, 12)
    a = (a + b) & MASK32
    d = rotl32(d ^ a, 8)
    c = (c + d) & MASK32
    b = rotl32(b ^ c, 7)
    return a, b, c, d


def chacha20_block(key: bytes, counter: int, nonce: bytes) -> bytes:
    if len(key) != KEY_SIZE or len(nonce) != NONCE_SIZE:
        raise ValueError(f"ChaCha20 needs a 32-byte key and a 12-byte nonce, got {len(key)} and {len(nonce)}")
    state = list(SIGMA) + list(struct.unpack("<8L", key)) + [counter & MASK32] + list(struct.unpack("<3L", nonce))
    working = state[:]
    for _ in range(10):
        for a, b, c, d in QUARTER_ROUNDS:
            working[a], working[b], working[c], working[d] = quarter_round(working[a], working[b], working[c], working[d])
    return struct.pack("<16L", *((w + s) & MASK32 for w, s in zip(working, state)))


def chacha20_xor(key: bytes, counter: int, nonce: bytes, data: bytes) -> bytes:
    blocks = (len(data) + 63) // 64
    if counter + blocks > 1 << 32:
        raise ValueError("ChaCha20 block counter would wrap: at most 256 GiB per key and nonce")
    out = bytearray()
    for index in range(blocks):
        chunk = data[index * 64:(index + 1) * 64]
        stream = chacha20_block(key, counter + index, nonce)[:len(chunk)]
        out += (int.from_bytes(chunk, "little") ^ int.from_bytes(stream, "little")).to_bytes(len(chunk), "little")
    return bytes(out)


def poly1305_mac(message: bytes, key: bytes) -> bytes:
    if len(key) != KEY_SIZE:
        raise ValueError("Poly1305 needs a 32-byte one-time key")
    r = int.from_bytes(key[:16], "little") & POLY1305_CLAMP
    s = int.from_bytes(key[16:], "little")
    accumulator = 0
    for offset in range(0, len(message), 16):
        block = message[offset:offset + 16] + b"\x01"
        accumulator = (accumulator + int.from_bytes(block, "little")) * r % POLY1305_PRIME
    return ((accumulator + s) & ((1 << 128) - 1)).to_bytes(16, "little")


def poly1305_key_gen(key: bytes, nonce: bytes) -> bytes:
    return chacha20_block(key, 0, nonce)[:32]


def pad16(data: bytes) -> bytes:
    return b"\x00" * (-len(data) % 16)


def aead_tag(one_time_key: bytes, aad: bytes, ciphertext: bytes) -> bytes:
    mac_data = aad + pad16(aad) + ciphertext + pad16(ciphertext) + struct.pack("<QQ", len(aad), len(ciphertext))
    return poly1305_mac(mac_data, one_time_key)


def aead_encrypt(key: bytes, nonce: bytes, plaintext: bytes, aad: bytes = b"") -> bytes:
    ciphertext = chacha20_xor(key, 1, nonce, plaintext)
    return ciphertext + aead_tag(poly1305_key_gen(key, nonce), aad, ciphertext)


def aead_decrypt(key: bytes, nonce: bytes, sealed: bytes, aad: bytes = b"") -> bytes:
    if len(sealed) < TAG_SIZE:
        raise DecryptionError("ciphertext is shorter than the 16-byte tag")
    ciphertext, tag = sealed[:-TAG_SIZE], sealed[-TAG_SIZE:]
    if not hmac.compare_digest(aead_tag(poly1305_key_gen(key, nonce), aad, ciphertext), tag):
        raise DecryptionError("authentication tag mismatch: wrong key, wrong associated data, or the ciphertext was changed")
    return chacha20_xor(key, 1, nonce, ciphertext)


def seal_box(key: bytes, plaintext: bytes, aad: bytes, rng: Callable[[int], bytes] = secrets.token_bytes) -> bytes:
    nonce = rng(NONCE_SIZE)
    return nonce + aead_encrypt(key, nonce, plaintext, aad)


def open_box(key: bytes, box: bytes, aad: bytes) -> bytes:
    if len(box) < NONCE_SIZE + TAG_SIZE:
        raise DecryptionError("sealed box is too short to hold a nonce and a tag")
    return aead_decrypt(key, box[:NONCE_SIZE], box[NONCE_SIZE:], aad)


@dataclass
class Keyring:
    terms: Dict[int, bytes]
    active: int

    @classmethod
    def generate(cls, rng: Callable[[int], bytes] = secrets.token_bytes) -> "Keyring":
        return cls({1: rng(KEY_SIZE)}, 1)

    def to_json(self) -> Dict[str, Any]:
        return {"active": self.active, "terms": {str(t): b64e(k) for t, k in sorted(self.terms.items())}}

    @classmethod
    def from_json(cls, raw: Dict[str, Any]) -> "Keyring":
        return cls({int(t): b64d(k, "keyring term") for t, k in raw["terms"].items()}, int(raw["active"]))

    def rotate(self, rng: Callable[[int], bytes] = secrets.token_bytes) -> int:
        self.active = max(self.terms) + 1
        self.terms[self.active] = rng(KEY_SIZE)
        return self.active

    def seal(self, plaintext: bytes, aad: str, rng: Callable[[int], bytes] = secrets.token_bytes) -> Dict[str, Any]:
        data_key = rng(KEY_SIZE)
        context = aad.encode("utf-8")
        return {
            "term": self.active,
            "wrapped_key": b64e(seal_box(self.terms[self.active], data_key, context + b"#data-key", rng)),
            "ciphertext": b64e(seal_box(data_key, plaintext, context, rng)),
        }

    def unwrap(self, record: Dict[str, Any], aad: str) -> bytes:
        term = int(record["term"])
        if term not in self.terms:
            raise DecryptionError(f"keyring has no term {term}")
        return open_box(self.terms[term], b64d(record["wrapped_key"], "wrapped key"), aad.encode("utf-8") + b"#data-key")

    def open(self, record: Dict[str, Any], aad: str) -> bytes:
        return open_box(self.unwrap(record, aad), b64d(record["ciphertext"], "ciphertext"), aad.encode("utf-8"))

    def rewrap(self, record: Dict[str, Any], aad: str, rng: Callable[[int], bytes] = secrets.token_bytes) -> Dict[str, Any]:
        data_key = self.unwrap(record, aad)
        wrapped = seal_box(self.terms[self.active], data_key, aad.encode("utf-8") + b"#data-key", rng)
        return {"term": self.active, "wrapped_key": b64e(wrapped), "ciphertext": record["ciphertext"]}


def gf_mul(a: int, b: int) -> int:
    product = 0
    while b:
        if b & 1:
            product ^= a
        a <<= 1
        if a & 0x100:
            a ^= GF_REDUCTION
        b >>= 1
    return product


def gf_inverse(a: int) -> int:
    if a == 0:
        raise ZeroDivisionError("0 has no inverse in GF(2^8)")
    result, base, exponent = 1, a, 254
    while exponent:
        if exponent & 1:
            result = gf_mul(result, base)
        base = gf_mul(base, base)
        exponent >>= 1
    return result


def gf_div(a: int, b: int) -> int:
    return gf_mul(a, gf_inverse(b))


def gf_poly_eval(coefficients: Sequence[int], x: int) -> int:
    result = 0
    for coefficient in reversed(coefficients):
        result = gf_mul(result, x) ^ coefficient
    return result


def gf_interpolate_at_zero(xs: Sequence[int], ys: Sequence[int]) -> int:
    result = 0
    for i, (xi, yi) in enumerate(zip(xs, ys)):
        basis = 1
        for j, xj in enumerate(xs):
            if i != j:
                basis = gf_mul(basis, gf_div(xj, xi ^ xj))
        result ^= gf_mul(yi, basis)
    return result


def shuffled_x_coordinates(randbelow: Callable[[int], int] = secrets.randbelow) -> List[int]:
    xs = list(range(1, 256))
    for i in range(len(xs) - 1, 0, -1):
        j = randbelow(i)
        xs[i], xs[j] = xs[j], xs[i]
    return xs


def shamir_split(secret: bytes, shares: int, threshold: int, rng: Callable[[int], bytes] = secrets.token_bytes, randbelow: Callable[[int], int] = secrets.randbelow) -> List[bytes]:
    if shares < threshold:
        raise InputError("parts cannot be less than threshold")
    if shares > 255:
        raise InputError("parts cannot exceed 255")
    if threshold < 2:
        raise InputError("threshold must be at least 2")
    if not secret:
        raise InputError("cannot split an empty secret")
    xs = shuffled_x_coordinates(randbelow)[:shares]
    out = [bytearray(len(secret) + 1) for _ in range(shares)]
    for part, x in zip(out, xs):
        part[-1] = x
    for index, value in enumerate(secret):
        coefficients = [value] + list(rng(threshold - 1))
        for part, x in zip(out, xs):
            part[index] = gf_poly_eval(coefficients, x)
    return [bytes(part) for part in out]


def shamir_combine(parts: Sequence[bytes]) -> bytes:
    if len(parts) < 2:
        raise InputError("less than two parts cannot be used to reconstruct the secret")
    length = len(parts[0])
    if length < 2:
        raise InputError("parts must be at least two bytes")
    if any(len(part) != length for part in parts):
        raise InputError("all parts must be the same length")
    xs = [part[-1] for part in parts]
    if len(set(xs)) != len(xs):
        raise InputError("duplicate part detected")
    return bytes(gf_interpolate_at_zero(xs, [part[i] for part in parts]) for i in range(length - 1))


DURATION_PART = re.compile(r"(\d+(?:\.\d+)?)(ms|s|m|h|d)")
DURATION_UNITS = {"ms": 0.001, "s": 1, "m": 60, "h": 3600, "d": 86400}


def parse_duration(value: Any) -> int:
    if value is None or value == "":
        return 0
    if isinstance(value, bool):
        raise InputError(f"invalid duration {value!r}")
    if isinstance(value, (int, float)):
        return int(value)
    text = str(value).strip()
    if re.fullmatch(r"\d+", text):
        return int(text)
    total, position = 0.0, 0
    for match in DURATION_PART.finditer(text):
        if match.start() != position:
            break
        total += float(match.group(1)) * DURATION_UNITS[match.group(2)]
        position = match.end()
    if position != len(text) or not text:
        raise InputError(f"invalid duration {value!r} (examples: 30s, 15m, 1h, 768h, 3600)")
    return int(total)


def rfc3339(timestamp: Optional[float]) -> str:
    if timestamp is None:
        return ""
    return datetime.fromtimestamp(timestamp, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def sql_expiration(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp, timezone.utc).strftime("%Y-%m-%d %H:%M:%S%z")


def random_string(length: int) -> str:
    return "".join(secrets.choice(ALNUM) for _ in range(length))


HCL_TOKEN = re.compile(r'\s+|#[^\n]*|//[^\n]*|/\*.*?\*/|"(?:[^"\\\n]|\\.)*"|[A-Za-z_][A-Za-z0-9_\-]*|-?\d+|[{}\[\]=,]', re.S)


def hcl_tokens(text: str) -> List[str]:
    tokens, position = [], 0
    while position < len(text):
        match = HCL_TOKEN.match(text, position)
        if not match:
            line = text.count("\n", 0, position) + 1
            raise InputError(f"policy: unexpected {text[position]!r} on line {line}")
        token = match.group(0)
        position = match.end()
        if not (token[0].isspace() or token.startswith(("#", "//", "/*"))):
            tokens.append(token)
    return tokens


def parse_hcl_policy(text: str) -> List[Tuple[str, Dict[str, Any]]]:
    tokens = hcl_tokens(text)
    stanzas: List[Tuple[str, Dict[str, Any]]] = []
    i = 0

    def take(expected: Optional[str] = None) -> str:
        nonlocal i
        if i >= len(tokens):
            raise InputError(f"policy: unexpected end of input{f', expected {expected!r}' if expected else ''}")
        token = tokens[i]
        if expected is not None and token != expected:
            raise InputError(f"policy: expected {expected!r}, got {token!r}")
        i += 1
        return token

    def value() -> Any:
        token = take()
        if token.startswith('"'):
            return json.loads(token)
        if token == "[":
            items: List[Any] = []
            while i < len(tokens) and tokens[i] != "]":
                item = take()
                if not item.startswith('"'):
                    raise InputError(f"policy: list items must be strings, got {item!r}")
                items.append(json.loads(item))
                if i < len(tokens) and tokens[i] == ",":
                    take(",")
            take("]")
            return items
        if token in ("true", "false"):
            return token == "true"
        if re.fullmatch(r"-?\d+", token):
            return int(token)
        raise InputError(f"policy: unsupported value {token!r}")

    while i < len(tokens):
        keyword = take()
        if keyword != "path":
            raise InputError(f"policy: expected 'path', got {keyword!r} (minivault policies only contain path stanzas)")
        pattern = take()
        if not pattern.startswith('"'):
            raise InputError(f"policy: path needs a quoted pattern, got {pattern!r}")
        take("{")
        attributes: Dict[str, Any] = {}
        while i < len(tokens) and tokens[i] != "}":
            name = take()
            take("=")
            attributes[name] = value()
        take("}")
        stanzas.append((json.loads(pattern), attributes))
    return stanzas


def parse_policy_text(text: str) -> List[Tuple[str, Dict[str, Any]]]:
    stripped = text.strip()
    if stripped.startswith("{"):
        try:
            raw = json.loads(stripped)
        except json.JSONDecodeError as exc:
            raise InputError(f"policy: invalid JSON ({exc})") from None
        paths = raw.get("path") if isinstance(raw, dict) else None
        if not isinstance(paths, dict):
            raise InputError('policy: JSON policies look like {"path": {"secret/*": {"capabilities": ["read"]}}}')
        return [(pattern, attrs) for pattern, attrs in paths.items()]
    return parse_hcl_policy(text)


@dataclass
class PathRule:
    policy: str
    pattern: str
    key: str
    kind: str
    capabilities: frozenset


def compile_rules(policy: str, text: str) -> List[PathRule]:
    rules = []
    for pattern, attributes in parse_policy_text(text):
        unknown = sorted(set(attributes) - {"capabilities", "policy"})
        if unknown:
            raise InputError(f"path {pattern!r}: minivault supports only 'capabilities' (and the old 'policy'), not {', '.join(unknown)}")
        if "+*" in pattern:
            raise InputError(f"path {pattern!r}: invalid use of wildcards ('+*' is forbidden)")
        capabilities = list(attributes.get("capabilities") or [])
        if "policy" in attributes:
            old = OLD_STYLE_POLICIES.get(attributes["policy"])
            if old is None:
                raise InputError(f"path {pattern!r}: invalid policy {attributes['policy']!r}")
            capabilities += old
        for capability in capabilities:
            if capability not in VALID_CAPABILITIES:
                raise InputError(f"path {pattern!r}: invalid capability {capability!r}")
        if "deny" in capabilities:
            capabilities = ["deny"]
        segment = pattern == "+" or "/+" in pattern or pattern.startswith("+/")
        if segment:
            kind, key = "segment", pattern
        elif pattern.endswith("*"):
            kind, key = "prefix", pattern[:-1]
        else:
            kind, key = "exact", pattern
        rules.append(PathRule(policy, pattern, key, kind, frozenset(capabilities)))
    return rules


@dataclass
class Permission:
    capabilities: frozenset
    patterns: List[str]
    policies: List[str]
    kind: str


@dataclass
class Match:
    permission: Optional[Permission]
    note: str


class ACL:
    def __init__(self, rules: Sequence[PathRule], root: bool = False) -> None:
        self.root = root
        self.tables: Dict[str, Dict[str, Permission]] = {"exact": {}, "prefix": {}, "segment": {}}
        for rule in rules:
            table = self.tables[rule.kind]
            existing = table.get(rule.key)
            if existing is None:
                table[rule.key] = Permission(rule.capabilities, [rule.pattern], [rule.policy], rule.kind)
                continue
            existing.policies.append(rule.policy)
            existing.patterns.append(rule.pattern)
            if "deny" in existing.capabilities:
                continue
            if "deny" in rule.capabilities:
                existing.capabilities = frozenset(["deny"])
            else:
                existing.capabilities = existing.capabilities | rule.capabilities

    def non_exact(self, path: str) -> Optional[Permission]:
        candidates: List[Tuple[Tuple[int, bool, int, int, str], Permission]] = []
        prefixes = [key for key in self.tables["prefix"] if path.startswith(key)]
        if prefixes:
            longest = max(prefixes, key=len)
            if not self.tables["segment"]:
                return self.tables["prefix"][longest]
            candidates.append(((len(longest), False, 0, len(longest), longest), self.tables["prefix"][longest]))
        parts = path.split("/")
        for full, permission in self.tables["segment"].items():
            first = full.index("+")
            current, is_prefix = (full[:-1], True) if full.endswith("*") else (full, False)
            acl_parts = current.split("/")
            if len(parts) < len(acl_parts) or (not is_prefix and len(parts) != len(acl_parts)):
                continue
            wildcards, matched = 0, True
            for index, acl_part in enumerate(acl_parts):
                if acl_part == "+":
                    wildcards += 1
                elif acl_part == parts[index]:
                    continue
                elif is_prefix and index == len(acl_parts) - 1 and parts[index].startswith(acl_part):
                    continue
                else:
                    matched = False
                    break
            if matched:
                candidates.append(((first, not is_prefix, -wildcards, len(current), current), permission))
        if not candidates:
            return None
        return max(candidates, key=lambda item: item[0])[1]

    def lookup(self, path: str, list_operation: bool) -> Optional[Permission]:
        path = path.lstrip("/")
        exact = self.tables["exact"]
        if path in exact:
            return exact[path]
        if list_operation and path.endswith("/") and path[:-1] in exact:
            return exact[path[:-1]]
        found = self.non_exact(path)
        if found is None and list_operation and path.endswith("/"):
            found = self.non_exact(path[:-1])
        return found

    def allows(self, path: str, operation: str) -> bool:
        if self.root:
            return True
        permission = self.lookup(path, operation == "list")
        if permission is None or "deny" in permission.capabilities or operation not in permission.capabilities:
            return False
        if needs_sudo(path):
            return "sudo" in permission.capabilities
        return True

    def capabilities(self, path: str) -> List[str]:
        if self.root:
            return ["root"]
        permission = self.lookup(path, True)
        granted = [] if permission is None else [c for c in CAPABILITY_ORDER if c in permission.capabilities]
        if permission is None or "deny" in permission.capabilities or not granted:
            return ["deny"]
        return granted

    def explain(self, path: str, operation: str) -> Dict[str, Any]:
        if self.root:
            return {"path": path, "operation": operation, "allowed": True, "capabilities": ["root"], "rule": None, "reason": "the root policy allows everything"}
        permission = self.lookup(path, operation == "list")
        allowed = self.allows(path, operation)
        if permission is None:
            reason = "no exact, glob or + rule matches this path, so it is denied by default"
            rule = None
        else:
            rule = {"kind": permission.kind, "patterns": sorted(set(permission.patterns)), "policies": sorted(set(permission.policies)), "capabilities": sorted(permission.capabilities)}
            if "deny" in permission.capabilities:
                reason = f"{permission.kind} rule {rule['patterns'][0]!r} says deny, and deny overrides every other capability on that rule"
            elif allowed:
                reason = f"{permission.kind} rule {rule['patterns'][0]!r} grants {operation}"
            elif operation in permission.capabilities and needs_sudo(path):
                reason = f"{path} is a root-protected path: it needs sudo as well as {operation}"
            else:
                reason = f"the most specific rule {rule['patterns'][0]!r} ({permission.kind}) does not grant {operation}; less specific rules are not consulted"
        return {"path": path, "operation": operation, "allowed": allowed, "capabilities": self.capabilities(path), "rule": rule, "reason": reason}


def needs_sudo(path: str) -> bool:
    return path in SUDO_PATHS or path.startswith(SUDO_PREFIXES)


def split_statements(value: Any) -> List[str]:
    if value is None or value == "":
        return []
    items = value if isinstance(value, list) else [value]
    statements: List[str] = []
    for item in items:
        if not isinstance(item, str):
            raise InputError("statements must be strings")
        current, quoted = [], False
        for char in item:
            if char == "'":
                quoted = not quoted
            if char == ";" and not quoted:
                statements.append("".join(current).strip())
                current = []
            else:
                current.append(char)
        statements.append("".join(current).strip())
    return [s + ";" for s in statements if s]


def render_statements(statements: Sequence[str], **values: str) -> List[str]:
    out = []
    for statement in statements:
        for key, value in values.items():
            statement = statement.replace("{{" + key + "}}", value)
        out.append(statement)
    return out


def redact_sql_secrets(text: str) -> str:
    return re.sub(r"(PASSWORD\s+')[^']*'", r"\1<redacted>'", text, flags=re.I)


class FakeDatabase:
    CREATE = re.compile(r'^CREATE\s+(?:ROLE|USER)\s+"([^"]+)"\s+WITH\s+LOGIN\s+PASSWORD\s+\'([^\']*)\'(?:\s+VALID\s+UNTIL\s+\'([^\']+)\')?\s*;$', re.I)
    ALTER = re.compile(r'^ALTER\s+(?:ROLE|USER)\s+"([^"]+)"\s+VALID\s+UNTIL\s+\'([^\']+)\'\s*;$', re.I)
    DROP = re.compile(r'^DROP\s+(?:ROLE|USER)\s+(IF\s+EXISTS\s+)?"([^"]+)"\s*;$', re.I)
    PRIVILEGES = re.compile(r"^(GRANT|REVOKE)\s", re.I)

    def __init__(self, path: Optional[str] = None, clock: Callable[[], float] = time.time) -> None:
        self.path = Path(path) if path else None
        self.clock = clock
        self.users: Dict[str, Dict[str, Any]] = {}
        self.statements = 0
        self.reload()

    def reload(self) -> None:
        if self.path and self.path.exists():
            self.users = json.loads(self.path.read_text(encoding="utf-8")).get("users", {})

    def save(self) -> None:
        if self.path:
            write_atomic(self.path, json.dumps({"users": self.users}, indent=2, sort_keys=True))

    def execute(self, statements: Sequence[str]) -> None:
        self.reload()
        for statement in statements:
            self.statements += 1
            text = " ".join(statement.split())
            create, alter, drop = self.CREATE.match(text), self.ALTER.match(text), self.DROP.match(text)
            if create:
                name, password, valid_until = create.groups()
                if name in self.users:
                    raise EngineError(f'fake database: role "{name}" already exists')
                salt = secrets.token_hex(8)
                self.users[name] = {"salt": salt, "verifier": hashlib.sha256((salt + password).encode()).hexdigest(), "valid_until": valid_until}
            elif alter:
                name, valid_until = alter.groups()
                if name not in self.users:
                    raise EngineError(f'fake database: role "{name}" does not exist')
                self.users[name]["valid_until"] = valid_until
            elif drop:
                if_exists, name = drop.groups()
                if name not in self.users and not if_exists:
                    raise EngineError(f'fake database: role "{name}" does not exist')
                self.users.pop(name, None)
            elif not self.PRIVILEGES.match(text):
                raise EngineError(f"fake database: unsupported statement: {redact_sql_secrets(text)[:80]}")
        self.save()

    def login(self, username: str, password: str) -> Tuple[bool, str]:
        self.reload()
        user = self.users.get(username)
        failed = f'password authentication failed for user "{username}"'
        if user is None or not hmac.compare_digest(user["verifier"], hashlib.sha256((user["salt"] + password).encode()).hexdigest()):
            return False, failed
        if user.get("valid_until"):
            until = datetime.strptime(user["valid_until"], "%Y-%m-%d %H:%M:%S%z").timestamp()
            if until <= self.clock():
                return False, failed + " (password expired: VALID UNTIL is in the past)"
        return True, f"logged in as {username}"


class CommandDatabase:
    def __init__(self, argv: Sequence[str], runner: Callable[..., Any] = subprocess.run) -> None:
        if not argv:
            raise InputError("--db-command needs a command, for example: docker exec -i pg psql -v ON_ERROR_STOP=1 -U postgres")
        self.argv = list(argv)
        self.runner = runner

    def execute(self, statements: Sequence[str]) -> None:
        script = "\n".join(statements) + "\n"
        try:
            result = self.runner(self.argv, input=script, capture_output=True, text=True, timeout=60)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise EngineError(f"database command {self.argv[0]!r} did not run: {exc}") from None
        if result.returncode != 0:
            detail = redact_sql_secrets((result.stderr or result.stdout or "").strip())
            raise EngineError(f"database command exited {result.returncode}: {detail[:500]}")


def write_atomic(path: Path, text: str, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    descriptor = os.open(str(temporary), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(text)
    os.replace(str(temporary), str(path))


class AuditLog:
    def __init__(self, key: bytes, path: Optional[str]) -> None:
        self.key = key
        self.path = Path(path) if path else None

    def hash(self, value: str) -> str:
        return "hmac-sha256:" + hmac.new(self.key, value.encode("utf-8"), hashlib.sha256).hexdigest()

    def scrub(self, value: Any) -> Any:
        if isinstance(value, str):
            return self.hash(value)
        if isinstance(value, dict):
            return {k: self.scrub(v) for k, v in value.items()}
        if isinstance(value, list):
            return [self.scrub(v) for v in value]
        return value

    def write(self, entry: Dict[str, Any]) -> None:
        if self.path is None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, sort_keys=True) + "\n")


@dataclass
class Token:
    id_hash: str
    accessor: str
    policies: List[str]
    parent: str
    display_name: str
    creation_time: float
    creation_ttl: int
    expire_time: Optional[float]
    explicit_max_ttl: int = 0
    renewable: bool = True
    num_uses: int = 0
    meta: Optional[Dict[str, str]] = None
    path: str = "auth/token/create"

    def expired(self, now: float) -> bool:
        return self.expire_time is not None and self.expire_time <= now

    def ttl_left(self, now: float) -> int:
        return 0 if self.expire_time is None else max(0, int(self.expire_time - now))


@dataclass
class Lease:
    lease_id: str
    mount: str
    role: str
    username: str
    token_hash: str
    issue_time: float
    expire_time: float
    max_expire_time: float
    renewable: bool = True
    last_renewal: Optional[float] = None
    revoke_error: str = ""
    retry_at: float = 0.0


@dataclass
class Request:
    method: str
    path: str
    query: Dict[str, str] = field(default_factory=dict)
    body: Optional[Dict[str, Any]] = None
    token: str = ""
    remote: str = "127.0.0.1"


@dataclass
class Response:
    status: int
    body: Optional[Dict[str, Any]] = None


@dataclass
class Route:
    handlers: Dict[str, Callable[["Call"], Response]]
    exists: Optional[Callable[[], bool]] = None


@dataclass
class Call:
    request: Request
    path: str
    operation: str
    token_id: str
    token: Token
    acl: ACL
    body: Dict[str, Any]
    restricted: bool = False


def logical(data: Any = None, auth: Any = None, lease_id: str = "", lease_duration: int = 0, renewable: bool = False, warnings: Optional[List[str]] = None) -> Dict[str, Any]:
    return {"request_id": str(uuid.uuid4()), "lease_id": lease_id, "renewable": renewable, "lease_duration": lease_duration, "data": data, "wrap_info": None, "warnings": warnings or None, "auth": auth}


def ok(data: Any = None, **kwargs: Any) -> Response:
    return Response(200, logical(data, **kwargs))


def sys_ok(data: Dict[str, Any]) -> Response:
    return Response(200, {**data, **logical(data)})


def no_content() -> Response:
    return Response(204)


def not_found() -> Response:
    return Response(404, {"errors": []})


def operation_for(request: Request) -> str:
    method = request.method.upper()
    if method == "GET":
        return "list" if request.query.get("list") in ("true", "1") else "read"
    if method == "LIST":
        return "list"
    if method in ("POST", "PUT"):
        return "update"
    if method == "DELETE":
        return "delete"
    raise ApiError(405, f"unsupported method {request.method}")


def token_hash(token_id: str) -> str:
    return hashlib.sha256(b"minivault-token:" + token_id.encode("utf-8")).hexdigest()


def as_bool(value: Any, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str) and value.lower() in ("true", "false", "1", "0"):
        return value.lower() in ("true", "1")
    raise InputError(f"expected a boolean, got {value!r}")


def as_list(value: Any) -> List[str]:
    if value is None or value == "":
        return []
    if isinstance(value, str):
        return [item.strip() for item in value.split(",") if item.strip()]
    if isinstance(value, list) and all(isinstance(item, (str, int)) for item in value):
        return [str(item).strip() for item in value if str(item).strip()]
    raise InputError(f"expected a list or a comma-separated string, got {value!r}")


def as_versions(value: Any) -> List[int]:
    try:
        return [int(item) for item in as_list(value)]
    except ValueError:
        raise InputError(f"versions must be integers, got {value!r}") from None


def new_kv_mount() -> Dict[str, Any]:
    return {"type": "kv", "options": {"version": "2"}, "config": {"max_versions": 0, "cas_required": False, "delete_version_after": "0s"}, "keys": {}}


def new_database_mount() -> Dict[str, Any]:
    return {"type": "database", "options": {}, "connections": {}, "roles": {}}


class Vault:
    def __init__(self, clock: Callable[[], float] = time.time, data_dir: Optional[str] = None, audit_path: Optional[str] = None, database: Any = None, rng: Callable[[int], bytes] = secrets.token_bytes) -> None:
        self.clock = clock
        self.rng = rng
        self.lock = threading.RLock()
        self.data_dir = Path(data_dir) if data_dir else None
        self.database = database if database is not None else FakeDatabase(clock=clock)
        self.audit_path = audit_path
        self.audit: Optional[AuditLog] = None
        self.initialized = False
        self.sealed = True
        self.seal_config = {"shares": 0, "threshold": 0}
        self.sealed_root = ""
        self.sealed_keyring = ""
        self.unseal_parts: List[bytes] = []
        self.root_key: Optional[bytes] = None
        self.keyring: Optional[Keyring] = None
        self.mounts: Dict[str, Dict[str, Any]] = {}
        self.policies: Dict[str, str] = {}
        self.tokens: Dict[str, Token] = {}
        self.leases: Dict[str, Lease] = {}
        if self.data_dir and (self.data_dir / STATE_FILE).exists():
            self.load()

    @classmethod
    def dev(cls, root_token: str = "root", **kwargs: Any) -> Tuple["Vault", Dict[str, Any]]:
        vault = cls(**kwargs)
        result = vault.initialize(1, 1, root_token_id=root_token)
        vault.submit_unseal_key(result["keys"][0])
        root = vault.tokens[token_hash(result["root_token"])]
        root.display_name, root.path = "token", "auth/token/create"
        vault.save()
        return vault, result

    def save(self) -> None:
        if not self.data_dir:
            return
        state = {
            "format": 1,
            "initialized": self.initialized,
            "seal": self.seal_config,
            "sealed_root": self.sealed_root,
            "sealed_keyring": self.sealed_keyring,
            "mounts": self.mounts,
            "policies": self.policies,
            "tokens": {h: asdict(t) for h, t in self.tokens.items()},
            "leases": {i: asdict(lease) for i, lease in self.leases.items()},
        }
        write_atomic(self.data_dir / STATE_FILE, json.dumps(state, indent=2, sort_keys=True))

    def load(self) -> None:
        path = self.data_dir / STATE_FILE
        try:
            state = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise InputError(f"{path}: cannot read minivault state ({exc})") from None
        if state.get("format") != 1:
            raise InputError(f"{path}: unknown state format {state.get('format')!r}")
        self.initialized = state["initialized"]
        self.seal_config = state["seal"]
        self.sealed_root = state["sealed_root"]
        self.sealed_keyring = state["sealed_keyring"]
        self.mounts = state["mounts"]
        self.policies = state["policies"]
        self.tokens = {h: Token(**t) for h, t in state["tokens"].items()}
        self.leases = {i: Lease(**lease) for i, lease in state["leases"].items()}
        self.sealed = True

    def seal_keyring(self) -> None:
        self.sealed_keyring = b64e(seal_box(self.root_key, json.dumps(self.keyring.to_json()).encode("utf-8"), b"minivault/keyring", self.rng))

    def initialize(self, shares: int, threshold: int, root_token_id: Optional[str] = None) -> Dict[str, Any]:
        if self.initialized:
            raise ApiError(400, "Vault is already initialized")
        if shares < 1 or threshold < 1 or threshold > shares or shares > 255:
            raise ApiError(400, f"invalid seal configuration: need 1 <= threshold ({threshold}) <= shares ({shares}) <= 255")
        if shares > 1 and threshold == 1:
            raise ApiError(400, "threshold must be greater than one for multiple shares")
        unseal_key = self.rng(KEY_SIZE)
        self.root_key = self.rng(KEY_SIZE)
        self.keyring = Keyring.generate(self.rng)
        material = json.dumps({"root_key": b64e(self.root_key), "audit_key": b64e(self.rng(KEY_SIZE))}).encode("utf-8")
        self.sealed_root = b64e(seal_box(unseal_key, material, b"minivault/root-key", self.rng))
        self.seal_keyring()
        parts = [unseal_key] if shares == 1 else shamir_split(unseal_key, shares, threshold, self.rng)
        self.seal_config = {"shares": shares, "threshold": threshold}
        self.initialized = True
        self.mounts = {"secret/": new_kv_mount()}
        self.policies = {"default": DEFAULT_POLICY.strip() + "\n"}
        root_id, _ = self.issue_token(["root"], "", "root", 0, 0, False, 0, None, "auth/token/root", root_token_id)
        self.seal()
        self.save()
        return {"keys": [p.hex() for p in parts], "keys_base64": [b64e(p) for p in parts], "root_token": root_id}

    def seal(self) -> None:
        self.root_key = None
        self.keyring = None
        self.audit = None
        self.sealed = True
        self.unseal_parts = []

    def seal_status(self) -> Dict[str, Any]:
        return {
            "type": "shamir",
            "initialized": self.initialized,
            "sealed": self.sealed,
            "t": self.seal_config["threshold"],
            "n": self.seal_config["shares"],
            "progress": len(self.unseal_parts),
            "nonce": "",
            "version": "minivault",
            "migration": False,
            "recovery_seal": False,
            "storage_type": "file" if self.data_dir else "inmem",
        }

    def submit_unseal_key(self, key_text: str) -> Dict[str, Any]:
        if not self.initialized:
            raise ApiError(400, "Vault is not initialized")
        if not self.sealed:
            return self.seal_status()
        try:
            key = decode_key_material(key_text, "unseal key")
        except InputError:
            raise ApiError(400, "'key' must be a valid hex or base64 string") from None
        if not 16 <= len(key) <= KEY_SIZE + 1:
            raise ApiError(400, f"unseal key has {len(key)} bytes; a whole key has {KEY_SIZE} and a Shamir share {KEY_SIZE + 1}")
        if key not in self.unseal_parts:
            self.unseal_parts.append(key)
        threshold = self.seal_config["threshold"]
        if len(self.unseal_parts) < threshold:
            return self.seal_status()
        parts, self.unseal_parts = self.unseal_parts, []
        try:
            combined = parts[0] if threshold == 1 else shamir_combine(parts)
            material = json.loads(open_box(combined, b64d(self.sealed_root), b"minivault/root-key"))
            root_key = b64d(material["root_key"])
            keyring = Keyring.from_json(json.loads(open_box(root_key, b64d(self.sealed_keyring), b"minivault/keyring")))
        except (InputError, DecryptionError, ValueError, KeyError) as exc:
            raise ApiError(400, f"unseal failed: the combined key does not decrypt the root key ({exc})") from None
        self.root_key, self.keyring = root_key, keyring
        self.audit = AuditLog(b64d(material["audit_key"]), self.audit_path)
        self.sealed = False
        return self.seal_status()

    def issue_token(self, policies: Sequence[str], parent: str, display_name: str, ttl: int, explicit_max_ttl: int, renewable: bool, num_uses: int, meta: Optional[Dict[str, str]], path: str = "auth/token/create", token_id: Optional[str] = None) -> Tuple[str, Token]:
        token_id = token_id or "s." + random_string(24)
        now = self.clock()
        token = Token(token_hash(token_id), random_string(24), sorted(set(policies)), parent, display_name, now, ttl, None if ttl == 0 else now + ttl, explicit_max_ttl, renewable, num_uses, meta, path)
        self.tokens[token.id_hash] = token
        return token_id, token

    def lookup_token(self, token_id: str) -> Optional[Token]:
        if not token_id:
            return None
        token = self.tokens.get(token_hash(token_id))
        if token is None or token.expired(self.clock()):
            return None
        return token

    def acl_for(self, token: Token) -> ACL:
        if "root" in token.policies:
            return ACL([], root=True)
        rules: List[PathRule] = []
        for name in token.policies:
            if name in self.policies:
                rules.extend(compile_rules(name, self.policies[name]))
        return ACL(rules)

    def revoke_token(self, id_hash: str, orphan_children: bool = False) -> None:
        token = self.tokens.get(id_hash)
        if token is None:
            return
        for child_hash, child in list(self.tokens.items()):
            if child.parent == id_hash:
                if orphan_children:
                    child.parent = ""
                else:
                    self.revoke_token(child_hash)
        for lease in [lease for lease in self.leases.values() if lease.token_hash == id_hash]:
            self.revoke_lease_quietly(lease)
        self.tokens.pop(id_hash, None)

    def revoke_lease(self, lease_id: str) -> None:
        lease = self.leases.get(lease_id)
        if lease is None:
            return
        engine = self.mounts.get(lease.mount) or {}
        role = (engine.get("roles") or {}).get(lease.role)
        statements = role["revocation_statements"] if role else DEFAULT_REVOCATION_STATEMENTS
        self.database.execute(render_statements(statements, name=lease.username, username=lease.username))
        self.leases.pop(lease_id, None)

    def revoke_lease_quietly(self, lease: Lease) -> None:
        try:
            self.revoke_lease(lease.lease_id)
        except EngineError as exc:
            lease.revoke_error = str(exc)
            lease.retry_at = self.clock() + 5
            lease.expire_time = min(lease.expire_time, self.clock())

    def tick(self) -> bool:
        if self.sealed:
            return False
        now = self.clock()
        changed = False
        for id_hash, token in list(self.tokens.items()):
            if id_hash in self.tokens and token.expired(now):
                self.revoke_token(id_hash)
                changed = True
        for lease in list(self.leases.values()):
            if lease.lease_id in self.leases and lease.expire_time <= now and lease.retry_at <= now:
                self.revoke_lease_quietly(lease)
                changed = True
        if changed:
            self.save()
        return changed

    def handle(self, request: Request) -> Response:
        with self.lock:
            self.tick()
            trace: Dict[str, Any] = {"path": request.path.lstrip("/"), "operation": ""}
            try:
                response = self.dispatch(request, trace)
            except ApiError as exc:
                response = Response(exc.status, {"errors": [exc.message] if exc.message else []})
            except InputError as exc:
                response = Response(400, {"errors": [str(exc)]})
            except EngineError as exc:
                response = Response(500, {"errors": [str(exc)]})
            self.write_audit(request, trace, response)
            exhausted = trace.get("exhausted")
            if exhausted:
                self.revoke_token(exhausted)
            self.save()
            return response

    def dispatch(self, request: Request, trace: Dict[str, Any]) -> Response:
        path = request.path.lstrip("/")
        operation = operation_for(request)
        if operation == "list" and not path.endswith("/"):
            path += "/"
        trace.update(path=path, operation=operation)
        bare = path.rstrip("/")
        if bare in UNAUTHENTICATED_PATHS:
            return self.unauthenticated(bare, operation, request.body or {})
        if not self.initialized or self.sealed:
            raise ApiError(503, "Vault is sealed")
        token = self.lookup_token(request.token)
        if token is None:
            raise ApiError(403, "permission denied")
        trace.update(token=token, token_id=request.token)
        acl = self.acl_for(token)
        route = self.resolve(path)
        if route is not None and operation == "update" and route.exists is not None and not route.exists():
            operation = "create"
        trace["operation"] = operation
        if not acl.allows(path, operation):
            raise ApiError(403, "permission denied")
        restricted = token.num_uses > 0
        if restricted:
            token.num_uses -= 1
            if token.num_uses == 0:
                trace["exhausted"] = token.id_hash
        if route is None:
            raise ApiError(404, f"no handler for route {path!r}. route entry not found.")
        handler = route.handlers.get(operation) or (route.handlers.get("update") if operation == "create" else None)
        if handler is None:
            raise ApiError(405, "unsupported operation")
        body = request.body if request.body is not None else {}
        if not isinstance(body, dict):
            raise InputError("the request body must be a JSON object")
        return handler(Call(request, path, operation, request.token, token, acl, body, restricted))

    def unauthenticated(self, path: str, operation: str, body: Dict[str, Any]) -> Response:
        if path == "sys/seal-status":
            return Response(200, self.seal_status())
        if path == "sys/health":
            status = 501 if not self.initialized else 503 if self.sealed else 200
            return Response(status, {"initialized": self.initialized, "sealed": self.sealed, "standby": False, "version": "minivault", "server_time_utc": int(self.clock())})
        if path == "sys/init":
            if operation == "read":
                return Response(200, {"initialized": self.initialized})
            shares = int(body.get("secret_shares") or 0)
            threshold = int(body.get("secret_threshold") or 0)
            return Response(200, self.initialize(shares, threshold))
        if operation != "update":
            raise ApiError(405, "unsupported operation")
        if as_bool(body.get("reset"), False):
            self.unseal_parts = []
            return Response(200, self.seal_status())
        if not body.get("key"):
            raise ApiError(400, "'key' must be specified in request body as JSON, or 'reset' set to true")
        return Response(200, self.submit_unseal_key(str(body["key"])))

    def mount_for(self, path: str) -> Optional[str]:
        matches = [m for m in self.mounts if path.startswith(m) or path + "/" == m]
        return max(matches, key=len) if matches else None

    def resolve(self, path: str) -> Optional[Route]:
        fixed: Dict[str, Dict[str, Callable[[Call], Response]]] = {
            "auth/token/create": {"update": self.h_token_create},
            "auth/token/create-orphan": {"update": self.h_token_create},
            "auth/token/lookup-self": {"read": self.h_token_lookup_self},
            "auth/token/lookup": {"update": self.h_token_lookup},
            "auth/token/renew-self": {"update": self.h_token_renew_self},
            "auth/token/renew": {"update": self.h_token_renew},
            "auth/token/revoke-self": {"update": self.h_token_revoke_self},
            "auth/token/revoke": {"update": self.h_token_revoke},
            "auth/token/revoke-orphan": {"update": self.h_token_revoke_orphan},
            "auth/token/revoke-accessor": {"update": self.h_token_revoke_accessor},
            "sys/capabilities-self": {"update": self.h_capabilities_self},
            "sys/capabilities": {"update": self.h_capabilities},
            "sys/policies/acl/": {"list": self.h_policy_list},
            "sys/mounts": {"read": self.h_mounts_list},
            "sys/leases/lookup": {"update": self.h_lease_lookup},
            "sys/leases/renew": {"update": self.h_lease_renew},
            "sys/leases/revoke": {"update": self.h_lease_revoke},
            "sys/rotate": {"update": self.h_rotate},
            "sys/key-status": {"read": self.h_key_status},
            "sys/rewrap": {"update": self.h_rewrap},
            "sys/seal": {"update": self.h_seal},
        }
        if path in fixed:
            return Route(fixed[path])
        if path.startswith("sys/policies/acl/"):
            name = path[len("sys/policies/acl/"):].lower()
            return Route({"read": lambda c: self.h_policy_read(name), "update": lambda c: self.h_policy_write(name, c), "delete": lambda c: self.h_policy_delete(name)})
        if path.startswith("sys/mounts/"):
            name = path[len("sys/mounts/"):].strip("/") + "/"
            return Route({"update": lambda c: self.h_mount_enable(name, c), "delete": lambda c: self.h_mount_disable(name)})
        if path.startswith("sys/leases/revoke-prefix/"):
            prefix = path[len("sys/leases/revoke-prefix/"):]
            return Route({"update": lambda c: self.h_lease_revoke_prefix(prefix)})
        if path.startswith("sys/audit-hash/"):
            device = path[len("sys/audit-hash/"):].strip("/")
            return Route({"update": lambda c: self.h_audit_hash(device, c)})
        mount = self.mount_for(path)
        if mount is None:
            return None
        engine = self.mounts[mount]
        rest = path[len(mount):]
        if engine["type"] == "kv":
            return self.kv_route(mount, engine, rest)
        if engine["type"] == "database":
            return self.db_route(mount, engine, rest)
        return None

    def h_token_create(self, call: Call) -> Response:
        body, parent = call.body, call.token
        requested = [p.lower() for p in as_list(body.get("policies"))]
        no_default = as_bool(body.get("no_default_policy"), False)
        is_sudo = call.acl.root or has_sudo(call.acl, call.path)
        if call.restricted:
            raise ApiError(400, "restricted use token cannot generate child tokens")
        if not requested:
            policies = list(parent.policies)
        elif not is_sudo:
            if not set(requested) - {"default"} <= set(parent.policies) - {"default"}:
                raise ApiError(400, "child policies must be subset of parent")
            policies = requested + (["default"] if not no_default and "default" in parent.policies else [])
        else:
            policies = requested + ([] if no_default else ["default"])
        if "root" in policies and "root" not in parent.policies:
            raise ApiError(400, "root tokens may not be created without parent token being root")
        orphan = call.path.endswith("create-orphan") or as_bool(body.get("no_parent"), False)
        if as_bool(body.get("no_parent"), False) and not is_sudo:
            raise ApiError(400, "root or sudo privileges required to create orphan token")
        ttl = parse_duration(body.get("ttl"))
        explicit = parse_duration(body.get("explicit_max_ttl"))
        renewable = as_bool(body.get("renewable"), True)
        num_uses = int(body.get("num_uses") or 0)
        if num_uses < 0:
            raise ApiError(400, "number of uses cannot be negative")
        warnings: List[str] = []
        if ttl == 0 and "root" not in policies:
            ttl = SYSTEM_MAX_TTL
        if ttl > 0:
            cap = min(SYSTEM_MAX_TTL, explicit) if explicit else SYSTEM_MAX_TTL
            if ttl > cap:
                warnings.append(f"TTL of {ttl}s exceeded the effective max_ttl of {cap}s; TTL value is capped accordingly")
                ttl = cap
        if ttl == 0 and explicit > 0:
            ttl = explicit
        if ttl == 0:
            renewable = False
        name = str(body.get("display_name") or "")
        display = re.sub(r"[^a-zA-Z0-9-]", "-", "token-" + name) if name else "token"
        display = display[:-1] if display.endswith("-") else display
        meta = body.get("meta") or None
        token_id, token = self.issue_token(policies, "" if orphan else parent.id_hash, display, ttl, explicit, renewable, num_uses, meta)
        return ok(auth=self.auth_block(token_id, token, ttl), warnings=warnings)

    def auth_block(self, token_id: str, token: Token, lease_duration: int) -> Dict[str, Any]:
        return {
            "client_token": token_id,
            "accessor": token.accessor,
            "policies": token.policies,
            "token_policies": token.policies,
            "metadata": token.meta,
            "lease_duration": lease_duration,
            "renewable": token.renewable,
            "entity_id": "",
            "token_type": "service",
            "orphan": token.parent == "",
            "mfa_requirement": None,
            "num_uses": token.num_uses,
        }

    def token_data(self, token_id: str, token: Token) -> Dict[str, Any]:
        return {
            "accessor": token.accessor,
            "creation_time": int(token.creation_time),
            "creation_ttl": token.creation_ttl,
            "display_name": token.display_name,
            "entity_id": "",
            "expire_time": rfc3339(token.expire_time) or None,
            "explicit_max_ttl": token.explicit_max_ttl,
            "id": token_id,
            "issue_time": rfc3339(token.creation_time),
            "meta": token.meta,
            "num_uses": token.num_uses,
            "orphan": token.parent == "",
            "path": token.path,
            "policies": token.policies,
            "renewable": token.renewable,
            "ttl": token.ttl_left(self.clock()),
            "type": "service",
        }

    def other_token(self, call: Call) -> Tuple[str, Token]:
        token_id = str(call.body.get("token") or "")
        if not token_id:
            raise ApiError(400, "missing token ID")
        token = self.lookup_token(token_id)
        if token is None:
            raise ApiError(403, "bad token")
        return token_id, token

    def h_token_lookup_self(self, call: Call) -> Response:
        return ok(self.token_data(call.token_id, call.token))

    def h_token_lookup(self, call: Call) -> Response:
        return ok(self.token_data(*self.other_token(call)))

    def renew_token(self, token_id: str, token: Token, increment: int) -> Response:
        if token.expire_time is None or not token.renewable:
            raise ApiError(400, "lease is not renewable")
        now = self.clock()
        ttl = increment or token.creation_ttl
        max_ttl = min(SYSTEM_MAX_TTL, token.explicit_max_ttl) if token.explicit_max_ttl else SYSTEM_MAX_TTL
        remaining = token.creation_time + max_ttl - now
        warnings = []
        if ttl > remaining:
            warnings.append(f"TTL of {ttl}s exceeded the effective max_ttl of {max_ttl}s; TTL value is capped accordingly")
            ttl = int(remaining)
        if ttl <= 0:
            raise ApiError(400, "past the max TTL, cannot renew")
        token.expire_time = now + ttl
        return ok(auth=self.auth_block(token_id, token, ttl), warnings=warnings)

    def h_token_renew_self(self, call: Call) -> Response:
        return self.renew_token(call.token_id, call.token, parse_duration(call.body.get("increment")))

    def h_token_renew(self, call: Call) -> Response:
        token_id, token = self.other_token(call)
        return self.renew_token(token_id, token, parse_duration(call.body.get("increment")))

    def h_token_revoke_self(self, call: Call) -> Response:
        self.revoke_token(call.token.id_hash)
        return no_content()

    def h_token_revoke(self, call: Call) -> Response:
        token = self.lookup_token(str(call.body.get("token") or ""))
        if not call.body.get("token"):
            raise ApiError(400, "missing token ID")
        if token is not None:
            self.revoke_token(token.id_hash)
        return no_content()

    def h_token_revoke_orphan(self, call: Call) -> Response:
        _, token = self.other_token(call)
        self.revoke_token(token.id_hash, orphan_children=True)
        return no_content()

    def h_token_revoke_accessor(self, call: Call) -> Response:
        accessor = str(call.body.get("accessor") or "")
        for id_hash, token in list(self.tokens.items()):
            if token.accessor == accessor:
                self.revoke_token(id_hash)
                return no_content()
        raise ApiError(400, "invalid accessor")

    def h_capabilities_self(self, call: Call) -> Response:
        return self.capabilities_response(call.acl, call.body)

    def h_capabilities(self, call: Call) -> Response:
        token = self.lookup_token(str(call.body.get("token") or ""))
        if token is None:
            raise ApiError(400, "invalid token")
        return self.capabilities_response(self.acl_for(token), call.body)

    def capabilities_response(self, acl: ACL, body: Dict[str, Any]) -> Response:
        paths = as_list(body.get("paths")) or as_list(body.get("path"))
        if not paths:
            raise ApiError(400, "paths must be supplied")
        data: Dict[str, Any] = {p: acl.capabilities(p) for p in paths}
        if len(paths) == 1:
            data["capabilities"] = data[paths[0]]
        return sys_ok(data)

    def h_policy_list(self, call: Call) -> Response:
        names = sorted(set(self.policies) | {"root"})
        return sys_ok({"keys": names, "policies": names})

    def h_policy_read(self, name: str) -> Response:
        if name not in self.policies:
            return not_found()
        return sys_ok({"name": name, "policy": self.policies[name]})

    def h_policy_write(self, name: str, call: Call) -> Response:
        text = call.body.get("policy")
        if not isinstance(text, str) or not text.strip():
            raise ApiError(400, "'policy' parameter not supplied or empty")
        if name == "root":
            raise ApiError(400, 'cannot update "root" policy')
        try:
            compile_rules(name, text)
        except InputError as exc:
            raise ApiError(400, f"failed to parse policy: {exc}") from None
        self.policies[name] = text
        return no_content()

    def h_policy_delete(self, name: str) -> Response:
        if name in ("root", "default"):
            raise ApiError(400, f'cannot delete "{name}" policy')
        self.policies.pop(name, None)
        return no_content()

    def h_mounts_list(self, call: Call) -> Response:
        return sys_ok({m: {"type": e["type"], "options": e["options"]} for m, e in sorted(self.mounts.items())})

    def h_mount_enable(self, name: str, call: Call) -> Response:
        kind = str(call.body.get("type") or "")
        options = call.body.get("options") or {}
        if name in self.mounts:
            raise ApiError(400, f"path is already in use at {name}")
        if kind == "kv" or kind == "kv-v2":
            if kind == "kv" and str(options.get("version", "1")) != "2":
                raise ApiError(400, "minivault implements KV version 2 only: pass options.version=2 or type kv-v2")
            self.mounts[name] = new_kv_mount()
        elif kind == "database":
            self.mounts[name] = new_database_mount()
        else:
            raise ApiError(400, f"plugin not found in the catalog: {kind or '(empty type)'}")
        return no_content()

    def h_mount_disable(self, name: str) -> Response:
        for lease in [lease for lease in self.leases.values() if lease.mount == name]:
            self.revoke_lease(lease.lease_id)
        self.mounts.pop(name, None)
        return no_content()

    def lease_from(self, call: Call) -> Lease:
        lease_id = str(call.body.get("lease_id") or call.body.get("url_lease_id") or "")
        if not lease_id:
            raise ApiError(400, "lease_id must be specified")
        lease = self.leases.get(lease_id)
        if lease is None:
            raise ApiError(400, "invalid lease")
        return lease

    def h_lease_lookup(self, call: Call) -> Response:
        lease = self.lease_from(call)
        data = {
            "id": lease.lease_id,
            "issue_time": rfc3339(lease.issue_time),
            "expire_time": rfc3339(lease.expire_time),
            "last_renewal": rfc3339(lease.last_renewal) or None,
            "renewable": lease.renewable,
            "ttl": max(0, int(lease.expire_time - self.clock())),
        }
        if lease.revoke_error:
            data["revoke_error"] = lease.revoke_error
        return sys_ok(data)

    def h_lease_renew(self, call: Call) -> Response:
        lease = self.lease_from(call)
        if not lease.renewable:
            raise ApiError(400, "lease is not renewable")
        role = self.mounts[lease.mount]["roles"].get(lease.role)
        if role is None:
            raise ApiError(400, f"role {lease.role!r} no longer exists")
        now = self.clock()
        ttl = parse_duration(call.body.get("increment")) or role["default_ttl"] or SYSTEM_MAX_TTL
        warnings = []
        if now + ttl > lease.max_expire_time:
            ttl = max(0, int(lease.max_expire_time - now))
            warnings.append(f"TTL exceeded the role's max_ttl; capped to {ttl}s")
        statements = render_statements(role["renew_statements"], name=lease.username, username=lease.username, expiration=sql_expiration(now + ttl))
        self.database.execute(statements)
        lease.expire_time = now + ttl
        lease.last_renewal = now
        return ok(lease_id=lease.lease_id, lease_duration=ttl, renewable=True, warnings=warnings)

    def h_lease_revoke(self, call: Call) -> Response:
        lease_id = str(call.body.get("lease_id") or "")
        if not lease_id:
            raise ApiError(400, "lease_id must be specified")
        self.revoke_lease(lease_id)
        return no_content()

    def h_lease_revoke_prefix(self, prefix: str) -> Response:
        for lease_id in [i for i in self.leases if i.startswith(prefix)]:
            self.revoke_lease(lease_id)
        return no_content()

    def h_rotate(self, call: Call) -> Response:
        self.keyring.rotate(self.rng)
        self.seal_keyring()
        return no_content()

    def h_key_status(self, call: Call) -> Response:
        return sys_ok({"term": self.keyring.active, "terms": sorted(self.keyring.terms)})

    def h_rewrap(self, call: Call) -> Response:
        count = 0
        for mount, engine in self.mounts.items():
            if engine["type"] != "kv":
                continue
            for key, meta in engine["keys"].items():
                for number, version in meta["versions"].items():
                    record = version.get("record")
                    if record and record["term"] != self.keyring.active:
                        version["record"] = self.keyring.rewrap(record, version_aad(mount, key, number), self.rng)
                        count += 1
        return sys_ok({"rewrapped": count, "term": self.keyring.active})

    def h_seal(self, call: Call) -> Response:
        self.seal()
        return no_content()

    def h_audit_hash(self, device: str, call: Call) -> Response:
        if device != AUDIT_DEVICE or self.audit is None or self.audit.path is None:
            raise ApiError(400, f"unknown audit backend {device!r} (start minivault with --audit-log FILE to enable the 'file' device)")
        value = call.body.get("input")
        if not isinstance(value, str):
            raise ApiError(400, "'input' must be a string")
        return sys_ok({"hash": self.audit.hash(value)})

    def write_audit(self, request: Request, trace: Dict[str, Any], response: Response) -> None:
        if self.audit is None or self.audit.path is None or trace["path"].rstrip("/") in UNAUTHENTICATED_PATHS:
            return
        token = trace.get("token")
        auth: Dict[str, Any] = {}
        if token is not None:
            auth = {"client_token": self.audit.hash(trace["token_id"]), "accessor": self.audit.hash(token.accessor), "display_name": token.display_name, "policies": token.policies, "token_ttl": token.creation_ttl}
        body = response.body or {}
        request_id = body.get("request_id") or str(uuid.uuid4())
        entry = {"id": request_id, "operation": trace["operation"], "path": trace["path"], "remote_address": request.remote, "data": self.audit.scrub(request.body) if request.body else None}
        now = rfc3339(self.clock())
        self.audit.write({"time": now, "type": "request", "auth": auth, "request": entry})
        returned_auth = body.get("auth")
        if isinstance(returned_auth, dict):
            returned_auth = {**returned_auth, "client_token": self.audit.hash(returned_auth["client_token"]), "accessor": self.audit.hash(returned_auth["accessor"])}
        result = {"status": response.status, "data": self.audit.scrub(body.get("data")), "auth": returned_auth, "lease_id": body.get("lease_id")}
        self.audit.write({"time": now, "type": "response", "auth": auth, "request": entry, "response": result, "error": "; ".join(body.get("errors") or [])})

    def kv_route(self, mount: str, engine: Dict[str, Any], rest: str) -> Optional[Route]:
        keys = engine["keys"]
        if rest == "config":
            return Route({"read": lambda c: ok(dict(engine["config"])), "update": lambda c: self.kv_config_write(engine, c)})
        prefix = next((p for p in ("data/", "metadata/", "delete/", "undelete/", "destroy/") if rest.startswith(p) or rest == p[:-1]), None)
        if prefix is None:
            return None
        key = rest[len(prefix):]
        if prefix == "metadata/":
            return Route({
                "read": lambda c: self.kv_metadata_read(engine, key),
                "update": lambda c: self.kv_metadata_write(engine, key, c),
                "delete": lambda c: self.kv_metadata_delete(engine, key),
                "list": lambda c: self.kv_list(engine, key),
            }, lambda: key in keys)
        if not key:
            return None
        if prefix == "data/":
            return Route({
                "read": lambda c: self.kv_read(mount, engine, key, c),
                "update": lambda c: self.kv_write(mount, engine, key, c),
                "delete": lambda c: self.kv_delete_latest(engine, key),
            }, lambda: key in keys)
        action = {"delete/": self.kv_delete_versions, "undelete/": self.kv_undelete, "destroy/": self.kv_destroy}[prefix]
        return Route({"update": lambda c: action(engine, key, c)})

    def kv_new_meta(self, now: float) -> Dict[str, Any]:
        return {"current_version": 0, "oldest_version": 0, "created_time": now, "updated_time": now, "max_versions": 0, "cas_required": False, "delete_version_after": "0s", "custom_metadata": None, "versions": {}}

    def kv_config_write(self, engine: Dict[str, Any], call: Call) -> Response:
        config = engine["config"]
        if "max_versions" in call.body:
            config["max_versions"] = int(call.body["max_versions"])
        if "cas_required" in call.body:
            config["cas_required"] = as_bool(call.body["cas_required"], False)
        return no_content()

    def kv_read(self, mount: str, engine: Dict[str, Any], key: str, call: Call) -> Response:
        meta = engine["keys"].get(key)
        if meta is None:
            return not_found()
        try:
            number = int(call.request.query.get("version") or 0) or meta["current_version"]
        except ValueError:
            raise ApiError(400, "version must be an integer") from None
        version = meta["versions"].get(str(number))
        if version is None:
            return not_found()
        metadata = {"version": number, "created_time": rfc3339(version["created_time"]), "deletion_time": rfc3339(version["deletion_time"]), "destroyed": version["destroyed"], "custom_metadata": meta["custom_metadata"]}
        deleted = version["deletion_time"] is not None and version["deletion_time"] <= self.clock()
        if deleted or version["destroyed"]:
            return Response(404, logical({"data": None, "metadata": metadata}))
        data = json.loads(self.keyring.open(version["record"], version_aad(mount, key, number)))
        return ok({"data": data, "metadata": metadata})

    def kv_write(self, mount: str, engine: Dict[str, Any], key: str, call: Call) -> Response:
        if call.body.get("data") is None:
            raise ApiError(400, "no data provided")
        data = call.body["data"]
        if not isinstance(data, dict):
            raise ApiError(400, "data must be a JSON object of key/value pairs")
        now = self.clock()
        meta = engine["keys"].get(key) or self.kv_new_meta(now)
        options = call.body.get("options") or {}
        if "cas" in options:
            try:
                cas = int(options["cas"])
            except (TypeError, ValueError):
                raise ApiError(400, "error parsing check-and-set parameter") from None
            if cas != meta["current_version"]:
                raise ApiError(400, "check-and-set parameter did not match the current version")
        elif engine["config"]["cas_required"] or meta["cas_required"]:
            raise ApiError(400, "check-and-set parameter required for this call")
        number = meta["current_version"] + 1
        record = self.keyring.seal(json.dumps(data).encode("utf-8"), version_aad(mount, key, number), self.rng)
        meta["current_version"] = number
        meta["versions"][str(number)] = {"created_time": now, "deletion_time": None, "destroyed": False, "record": record}
        meta["updated_time"] = now
        limit = max(meta["max_versions"], engine["config"]["max_versions"]) or DEFAULT_MAX_VERSIONS
        if number - meta["oldest_version"] >= limit:
            last = number - limit
            for old in range(meta["oldest_version"], last + 1):
                meta["versions"].pop(str(old), None)
            meta["oldest_version"] = last + 1
        engine["keys"][key] = meta
        return ok({"version": number, "created_time": rfc3339(now), "deletion_time": "", "destroyed": False, "custom_metadata": meta["custom_metadata"]})

    def live_version(self, meta: Dict[str, Any], number: int) -> Optional[Dict[str, Any]]:
        version = meta["versions"].get(str(number))
        if version is None or version["destroyed"]:
            return None
        if version["deletion_time"] is not None and version["deletion_time"] <= self.clock():
            return None
        return version

    def kv_delete_latest(self, engine: Dict[str, Any], key: str) -> Response:
        meta = engine["keys"].get(key)
        version = self.live_version(meta, meta["current_version"]) if meta else None
        if version is not None:
            version["deletion_time"] = self.clock()
        return no_content()

    def requested_versions(self, call: Call) -> List[int]:
        versions = as_versions(call.body.get("versions"))
        if not versions:
            raise ApiError(400, "No version number provided")
        return versions

    def kv_delete_versions(self, engine: Dict[str, Any], key: str, call: Call) -> Response:
        versions = self.requested_versions(call)
        meta = engine["keys"].get(key)
        for number in versions if meta else []:
            version = self.live_version(meta, number)
            if version is not None:
                version["deletion_time"] = self.clock()
        return no_content()

    def kv_undelete(self, engine: Dict[str, Any], key: str, call: Call) -> Response:
        versions = self.requested_versions(call)
        meta = engine["keys"].get(key)
        for number in versions if meta else []:
            version = meta["versions"].get(str(number))
            if version is not None and not version["destroyed"]:
                version["deletion_time"] = None
        return no_content()

    def kv_destroy(self, engine: Dict[str, Any], key: str, call: Call) -> Response:
        versions = self.requested_versions(call)
        meta = engine["keys"].get(key)
        for number in versions if meta else []:
            version = meta["versions"].get(str(number))
            if version is not None:
                version["destroyed"] = True
                version["record"] = None
        return no_content()

    def kv_metadata_read(self, engine: Dict[str, Any], key: str) -> Response:
        meta = engine["keys"].get(key) if key else None
        if meta is None:
            return not_found()
        versions = {n: {"created_time": rfc3339(v["created_time"]), "deletion_time": rfc3339(v["deletion_time"]), "destroyed": v["destroyed"]} for n, v in meta["versions"].items()}
        return ok({
            "versions": versions,
            "current_version": meta["current_version"],
            "current_metadata_version": 0,
            "oldest_version": meta["oldest_version"],
            "created_time": rfc3339(meta["created_time"]),
            "updated_time": rfc3339(meta["updated_time"]),
            "max_versions": meta["max_versions"],
            "cas_required": meta["cas_required"],
            "metadata_cas_required": False,
            "delete_version_after": meta["delete_version_after"],
            "custom_metadata": meta["custom_metadata"],
        })

    def kv_metadata_write(self, engine: Dict[str, Any], key: str, call: Call) -> Response:
        if not key:
            raise ApiError(400, "missing path")
        meta = engine["keys"].get(key) or self.kv_new_meta(self.clock())
        if "max_versions" in call.body:
            meta["max_versions"] = int(call.body["max_versions"])
        if "cas_required" in call.body:
            meta["cas_required"] = as_bool(call.body["cas_required"], False)
        if "custom_metadata" in call.body:
            custom = call.body["custom_metadata"]
            if custom is not None and not (isinstance(custom, dict) and all(isinstance(v, str) for v in custom.values())):
                raise ApiError(400, "custom_metadata must map strings to strings")
            meta["custom_metadata"] = custom
        meta["updated_time"] = self.clock()
        engine["keys"][key] = meta
        return no_content()

    def kv_metadata_delete(self, engine: Dict[str, Any], key: str) -> Response:
        engine["keys"].pop(key, None)
        return no_content()

    def kv_list(self, engine: Dict[str, Any], prefix: str) -> Response:
        children = set()
        for key in engine["keys"]:
            if key.startswith(prefix) and key != prefix:
                rest = key[len(prefix):]
                children.add(rest.split("/", 1)[0] + "/" if "/" in rest else rest)
        if not children:
            return not_found()
        return ok({"keys": sorted(children)})

    def db_route(self, mount: str, engine: Dict[str, Any], rest: str) -> Optional[Route]:
        tables = {"config/": engine["connections"], "roles/": engine["roles"]}
        if rest in tables:
            table = tables[rest]
            return Route({"list": lambda c: ok({"keys": sorted(table)}) if table else not_found()})
        for prefix, table in tables.items():
            if rest.startswith(prefix):
                name = rest[len(prefix):].strip("/")
                write = self.db_config_write if prefix == "config/" else self.db_role_write
                return Route({
                    "read": lambda c: ok(table[name]) if name in table else not_found(),
                    "update": lambda c: write(engine, name, c),
                    "delete": lambda c: (table.pop(name, None), no_content())[1],
                }, lambda: name in table)
        if rest.startswith("creds/") and rest[len("creds/"):]:
            role = rest[len("creds/"):]
            return Route({"read": lambda c: self.db_creds(mount, engine, role, c)})
        return None

    def db_config_write(self, engine: Dict[str, Any], name: str, call: Call) -> Response:
        plugin = str(call.body.get("plugin_name") or "")
        if not plugin:
            raise ApiError(400, "empty plugin name")
        engine["connections"][name] = {
            "plugin_name": plugin,
            "connection_details": {"connection_url": str(call.body.get("connection_url") or ""), "username": str(call.body.get("username") or "")},
            "allowed_roles": as_list(call.body.get("allowed_roles")),
            "verify_connection": as_bool(call.body.get("verify_connection"), True),
        }
        return no_content()

    def db_role_write(self, engine: Dict[str, Any], name: str, call: Call) -> Response:
        body = call.body
        db_name = str(body.get("db_name") or "")
        if not db_name:
            raise ApiError(400, "empty database name attribute")
        creation = split_statements(body.get("creation_statements"))
        if not creation:
            raise ApiError(400, "empty creation statements")
        role = {
            "db_name": db_name,
            "creation_statements": creation,
            "revocation_statements": split_statements(body.get("revocation_statements")) or list(DEFAULT_REVOCATION_STATEMENTS),
            "renew_statements": split_statements(body.get("renew_statements")) or list(DEFAULT_RENEW_STATEMENTS),
            "default_ttl": parse_duration(body.get("default_ttl")),
            "max_ttl": parse_duration(body.get("max_ttl")),
        }
        if role["max_ttl"] and role["default_ttl"] > role["max_ttl"]:
            raise ApiError(400, "ttl cannot be greater than max_ttl")
        engine["roles"][name] = role
        return no_content()

    def db_creds(self, mount: str, engine: Dict[str, Any], role_name: str, call: Call) -> Response:
        role = engine["roles"].get(role_name)
        if role is None:
            raise ApiError(400, f"unknown role: {role_name}")
        connection = engine["connections"].get(role["db_name"])
        if connection is None:
            raise ApiError(400, f"database configuration {role['db_name']!r} does not exist")
        if "*" not in connection["allowed_roles"] and role_name not in connection["allowed_roles"]:
            raise ApiError(400, f"{role_name!r} is not an allowed role")
        now = self.clock()
        max_ttl = role["max_ttl"] or SYSTEM_MAX_TTL
        ttl = min(role["default_ttl"] or SYSTEM_MAX_TTL, max_ttl)
        username = f"v-{call.token.display_name[:8]}-{role_name[:8]}-{random_string(20)}-{int(now)}"[:63]
        password = random_string(20)
        statements = render_statements(role["creation_statements"], name=username, username=username, password=password, expiration=sql_expiration(now + ttl))
        self.database.execute(statements)
        lease = Lease(f"{mount}creds/{role_name}/{random_string(24)}", mount, role_name, username, call.token.id_hash, now, now + ttl, now + max_ttl)
        self.leases[lease.lease_id] = lease
        return ok({"username": username, "password": password}, lease_id=lease.lease_id, lease_duration=ttl, renewable=True)


def has_sudo(acl: ACL, path: str) -> bool:
    permission = acl.lookup(path, False)
    return permission is not None and "sudo" in permission.capabilities


def version_aad(mount: str, key: str, number: Any) -> str:
    return f"{mount}{key}#v{number}"


class ApiHandler(BaseHTTPRequestHandler):
    server_version = "minivault"
    protocol_version = "HTTP/1.1"

    def handle_api(self) -> None:
        parts = urlsplit(self.path)
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length > 0 else b""
        if not parts.path.startswith("/v1/"):
            self.reply(Response(404, {"errors": ["minivault serves its API under /v1/"]}))
            return
        body = None
        if raw.strip():
            try:
                body = json.loads(raw)
            except json.JSONDecodeError as exc:
                self.reply(Response(400, {"errors": [f"failed to parse JSON input: {exc}"]}))
                return
        token = self.headers.get("X-Vault-Token") or ""
        authorization = self.headers.get("Authorization") or ""
        if not token and authorization.lower().startswith("bearer "):
            token = authorization[7:].strip()
        query = {k: v[-1] for k, v in parse_qs(parts.query).items()}
        request = Request(self.command, parts.path[len("/v1/"):], query, body, token, self.client_address[0])
        self.reply(self.server.vault.handle(request))

    def reply(self, response: Response) -> None:
        payload = b"" if response.status == 204 or response.body is None else json.dumps(response.body).encode("utf-8")
        self.send_response(response.status)
        if payload:
            self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if payload:
            self.wfile.write(payload)

    do_GET = do_POST = do_PUT = do_DELETE = do_LIST = handle_api

    def log_message(self, format: str, *args: Any) -> None:
        if getattr(self.server, "verbose", False):
            sys.stderr.write("minivault: %s %s\n" % (self.address_string(), format % args))


class VaultServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address: Tuple[str, int], vault: Vault, verbose: bool = False) -> None:
        super().__init__(address, ApiHandler)
        self.vault = vault
        self.verbose = verbose


def start_ticker(vault: Vault, interval: float = 0.25) -> threading.Event:
    stop = threading.Event()

    def run() -> None:
        while not stop.wait(interval):
            with vault.lock:
                vault.tick()

    threading.Thread(target=run, name="minivault-expiry", daemon=True).start()
    return stop


VARIABLE = re.compile(r"\$\{([A-Za-z0-9_]+)\}")


def substitute(value: Any, variables: Dict[str, Any]) -> Any:
    if isinstance(value, str):
        whole = VARIABLE.fullmatch(value)
        if whole and whole.group(1) in variables:
            return variables[whole.group(1)]

        def one(match: "re.Match[str]") -> str:
            if match.group(1) not in variables:
                raise InputError(f"replay: variable ${{{match.group(1)}}} is not set (pass --var {match.group(1)}=... or save it in an earlier step)")
            return str(variables[match.group(1)])

        return VARIABLE.sub(one, value)
    if isinstance(value, dict):
        return {k: substitute(v, variables) for k, v in value.items()}
    if isinstance(value, list):
        return [substitute(v, variables) for v in value]
    return value


def dig(value: Any, path: str) -> Any:
    current = value
    for part in path.split(".") if path else []:
        if isinstance(current, dict) and part in current:
            current = current[part]
        elif isinstance(current, list) and part.isdigit() and int(part) < len(current):
            current = current[int(part)]
        else:
            return None
    return current


def apply_filter(value: Any, spec: str) -> Any:
    name, _, argument = spec.partition(":")
    if not name:
        return value
    if name == "set":
        return value is not None and value != "" and value != [] and value != {}
    if name == "sorted":
        return sorted(value) if isinstance(value, list) else value
    if name == "keys":
        return sorted(value) if isinstance(value, dict) else None
    if name == "len":
        return len(value) if isinstance(value, (list, dict, str)) else None
    if name in ("le", "ge"):
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return False
        return value <= float(argument) if name == "le" else value >= float(argument)
    if name == "startswith":
        return isinstance(value, str) and value.startswith(argument)
    if name == "contains":
        return isinstance(value, str) and argument in value
    raise InputError(f"replay: unknown filter {name!r} (set, sorted, keys, len, le:N, ge:N, startswith:S, contains:S)")


def observe(body: Any, spec: str) -> Any:
    path, _, filter_spec = spec.partition("|")
    return apply_filter(dig(body, path), filter_spec)


NO_PROXY = urlrequest.build_opener(urlrequest.ProxyHandler({}))


def http_call(address: str, method: str, path: str, token: str = "", body: Any = None, timeout: float = 15.0) -> Tuple[int, Any]:
    url = address.rstrip("/") + "/v1/" + path.lstrip("/")
    data = None if body is None else json.dumps(body).encode("utf-8")
    request = urlrequest.Request(url, data=data, method=method.upper())
    if token:
        request.add_header("X-Vault-Token", token)
    if data is not None:
        request.add_header("Content-Type", "application/json")
    try:
        with NO_PROXY.open(request, timeout=timeout) as response:
            status, raw = response.status, response.read()
    except urlerror.HTTPError as exc:
        status, raw = exc.code, exc.read()
    except (urlerror.URLError, OSError) as exc:
        raise InputError(f"{method.upper()} {url}: {exc}") from None
    text = raw.decode("utf-8", "replace").strip()
    try:
        return status, json.loads(text) if text else None
    except json.JSONDecodeError:
        return status, {"raw": text}


def run_step(address: str, step: Dict[str, Any], variables: Dict[str, Any], sleep: Callable[[float], None] = time.sleep) -> Dict[str, Any]:
    method = str(step.get("method", "GET")).upper()
    path = substitute(step["path"], variables)
    token = substitute(step.get("token", "${root}"), variables)
    body = substitute(step["body"], variables) if "body" in step else None
    retry = step.get("retry") or {}
    deadline = time.monotonic() + float(retry.get("timeout", 0))
    attempts = 0
    while True:
        attempts += 1
        status, parsed = http_call(address, method, path, token, body)
        if not retry or status == retry.get("until_status") or time.monotonic() >= deadline:
            break
        sleep(float(retry.get("interval", 0.5)))
    expect = substitute(step.get("expect") or {}, variables)
    specs = [s for s in expect if s != "status"] + [s for s in step.get("observe") or [] if s not in expect]
    observed = {spec: observe(parsed, spec) for spec in specs}
    problems = []
    wanted = expect.get("status")
    if wanted is not None and status not in (wanted if isinstance(wanted, list) else [wanted]):
        problems.append(f"status {status}, want {wanted}")
    for spec, want in expect.items():
        if spec != "status" and observed[spec] != want:
            problems.append(f"{spec} = {observed[spec]!r}, want {want!r}")
    for name, spec in (step.get("save") or {}).items():
        value = observe(parsed, spec)
        if value is None:
            problems.append(f"save {name}: nothing at {spec}")
        else:
            variables[name] = value
    errors = parsed.get("errors") if isinstance(parsed, dict) else None
    return {"name": step["name"], "method": method, "path": path, "status": status, "observed": observed, "attempts": attempts, "ok": not problems, "problems": problems, "errors": errors}


def read_json(path: str) -> Any:
    try:
        text = sys.stdin.read() if path == "-" else Path(path).read_text(encoding="utf-8")
    except OSError as exc:
        raise InputError(f"{path}: {exc.strerror or exc}") from None
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise InputError(f"{path}: not JSON ({exc})") from None


def parse_var(item: str) -> Tuple[str, Any]:
    name, sep, raw = item.partition("=")
    if not sep or not re.fullmatch(r"[A-Za-z0-9_]+", name):
        raise InputError(f"--var wants NAME=VALUE, got {item!r}")
    try:
        return name, json.loads(raw)
    except json.JSONDecodeError:
        return name, raw


def parse_listen(text: str) -> Tuple[str, int]:
    host, sep, port = text.rpartition(":")
    if not sep or not port.isdigit():
        raise InputError(f"--listen wants HOST:PORT, got {text!r}")
    return host or "127.0.0.1", int(port)


def emit(args: argparse.Namespace, value: Any, text: str) -> None:
    print(json.dumps(value, indent=2) if getattr(args, "json", False) else text)


def cmd_server(args: argparse.Namespace) -> int:
    database: Any = CommandDatabase(shlex.split(args.db_command)) if args.db_command else FakeDatabase(args.fake_db)
    host, port = parse_listen(args.listen)
    if args.dev:
        if args.data:
            raise InputError("--dev keeps everything in memory; drop --data, or drop --dev to use the sealed, file-backed mode")
        vault, init = Vault.dev(args.dev_root_token_id, audit_path=args.audit_log, database=database)
        banner = f"minivault dev server on http://{host}:{port}\n  root token: {init['root_token']}\n  unseal key (base64): {init['keys_base64'][0]}\n  KV v2 at secret/; everything is in memory and lost on exit"
    else:
        if not args.data:
            raise InputError("pass --dev for an in-memory server, or --data DIR for a sealed, file-backed one")
        vault = Vault(data_dir=args.data, audit_path=args.audit_log, database=database)
        state = "not initialized: run 'minivault operator init --data DIR' or POST /v1/sys/init" if not vault.initialized else f"sealed: POST {vault.seal_config['threshold']} of {vault.seal_config['shares']} unseal key shares to /v1/sys/unseal"
        banner = f"minivault on http://{host}:{port} ({args.data})\n  {state}"
    try:
        server = VaultServer((host, port), vault, args.verbose)
    except OSError as exc:
        raise InputError(f"cannot listen on {host}:{port}: {exc.strerror or exc}") from None
    stop = start_ticker(vault)

    def terminate(signum: int, frame: Any) -> None:
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, terminate)
    print(banner, flush=True)
    try:
        server.serve_forever(poll_interval=0.2)
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        server.server_close()
    return 0


def cmd_operator_init(args: argparse.Namespace) -> int:
    vault = Vault(data_dir=args.data)
    try:
        result = vault.initialize(args.shares, args.threshold)
    except ApiError as exc:
        raise InputError(exc.message) from None
    lines = [f"Unseal Key {i + 1}: {key}" for i, key in enumerate(result["keys_base64"])]
    lines += ["", f"Initial Root Token: {result['root_token']}", "", f"minivault is initialized with {args.shares} key shares and a key threshold of {args.threshold}.", f"Start it with 'minivault server --data {args.data}' and unseal it with any {args.threshold} of these keys."]
    emit(args, {"unseal_keys_b64": result["keys_base64"], "unseal_keys_hex": result["keys"], "unseal_shares": args.shares, "unseal_threshold": args.threshold, "root_token": result["root_token"]}, "\n".join(lines))
    return 0


def cmd_shamir_split(args: argparse.Namespace) -> int:
    if args.hex:
        secret = decode_key_material(args.hex, "--hex")
    elif args.base64:
        secret = b64d(args.base64.strip(), "--base64")
    else:
        secret = secrets.token_bytes(args.random_bytes)
    shares = shamir_split(secret, args.shares, args.threshold)
    emit(args, {"secret_hex": secret.hex() if args.random_bytes and not (args.hex or args.base64) else None, "shares_hex": [s.hex() for s in shares], "shares_b64": [b64e(s) for s in shares], "threshold": args.threshold}, "\n".join(s.hex() for s in shares))
    return 0


def cmd_shamir_combine(args: argparse.Namespace) -> int:
    parts = [decode_key_material(share, "share") for share in args.shares]
    secret = shamir_combine(parts)
    emit(args, {"secret_hex": secret.hex(), "secret_b64": b64e(secret), "x_coordinates": [p[-1] for p in parts]}, secret.hex())
    return 0


def cmd_policy_check(args: argparse.Namespace) -> int:
    rules: List[PathRule] = [] if args.no_default else compile_rules("default", DEFAULT_POLICY)
    for item in args.policy:
        name, sep, path = item.partition("=")
        if not sep:
            raise InputError(f"--policy wants NAME=FILE, got {item!r}")
        try:
            text = Path(path).read_text(encoding="utf-8")
        except OSError as exc:
            raise InputError(f"{path}: {exc.strerror or exc}") from None
        rules.extend(compile_rules(name, text))
    acl = ACL(rules)
    results = [acl.explain(path, args.operation) for path in args.path]
    lines = []
    for result in results:
        verdict = "ALLOW" if result["allowed"] else "DENY "
        lines.append(f"{verdict} {args.operation:<6} {result['path']}  capabilities={','.join(result['capabilities'])}  {result['reason']}")
    emit(args, results, "\n".join(lines))
    return 0 if all(r["allowed"] for r in results) else 1


def cmd_fake_db_login(args: argparse.Namespace) -> int:
    database = FakeDatabase(args.file)
    success, message = database.login(args.username, args.password)
    print(message, file=sys.stdout if success else sys.stderr)
    return 0 if success else 1


def cmd_replay(args: argparse.Namespace) -> int:
    script = read_json(args.script)
    scenarios = script.get("scenarios") if isinstance(script, dict) else None
    if not isinstance(scenarios, dict):
        raise InputError(f"{args.script}: expected {{\"scenarios\": {{\"name\": [steps]}}}}")
    names = args.scenario or list(scenarios)
    variables: Dict[str, Any] = {"root": args.token}
    if args.vars_in:
        variables.update(read_json(args.vars_in))
    for item in args.var:
        name, value = parse_var(item)
        variables[name] = value
    results = []
    for name in names:
        if name not in scenarios:
            raise InputError(f"unknown scenario {name!r}; {args.script} has {', '.join(scenarios)}")
        for step in scenarios[name]:
            result = run_step(args.addr, step, variables)
            result["scenario"] = name
            results.append(result)
    if args.vars_out:
        write_atomic(Path(args.vars_out), json.dumps(variables, indent=2, sort_keys=True))
    passed = all(r["ok"] for r in results)
    lines = []
    for r in results:
        detail = "; ".join(r["problems"]) if r["problems"] else ", ".join(f"{k}={json.dumps(v)}" for k, v in r["observed"].items())
        lines.append(f"{'PASS' if r['ok'] else 'FAIL'}  {r['scenario']:<10} {r['status']}  {r['name']}  {detail}")
    lines.append(f"{sum(r['ok'] for r in results)}/{len(results)} steps as expected against {args.addr}")
    emit(args, {"ok": passed, "address": args.addr, "steps": results}, "\n".join(lines))
    return 0 if passed else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="minivault", description="A secrets service with OpenBao's API shape: KV v2, envelope encryption, Shamir unseal, tokens, policies, leases and an HMAC audit log.")
    sub = parser.add_subparsers(dest="command", required=True)
    server = sub.add_parser("server", help="run the HTTP API")
    server.add_argument("--dev", action="store_true", help="in memory, initialized and unsealed, KV v2 at secret/")
    server.add_argument("--dev-root-token-id", default="root")
    server.add_argument("--listen", default="127.0.0.1:8200", help="HOST:PORT (default 127.0.0.1:8200)")
    server.add_argument("--data", help="directory for the sealed state file (not with --dev)")
    server.add_argument("--audit-log", help="enable the 'file' audit device at this path")
    server.add_argument("--db-command", help="run database statements through this command's stdin, e.g. 'docker exec -i pg psql -v ON_ERROR_STOP=1 -U postgres'")
    server.add_argument("--fake-db", help="JSON file for the built-in fake database (default: in memory)")
    server.add_argument("--verbose", action="store_true")
    server.set_defaults(handler=cmd_server)
    operator = sub.add_parser("operator", help="offline operator commands").add_subparsers(dest="operator", required=True)
    init = operator.add_parser("init", help="initialize a --data directory and print the unseal key shares")
    init.add_argument("--data", required=True)
    init.add_argument("--shares", type=int, default=5)
    init.add_argument("--threshold", type=int, default=3)
    init.add_argument("--json", action="store_true")
    init.set_defaults(handler=cmd_operator_init)
    shamir = sub.add_parser("shamir", help="Shamir secret sharing over GF(2^8), share format compatible with OpenBao").add_subparsers(dest="shamir", required=True)
    split = shamir.add_parser("split")
    source = split.add_mutually_exclusive_group()
    source.add_argument("--hex")
    source.add_argument("--base64")
    split.add_argument("--random-bytes", type=int, default=32)
    split.add_argument("--shares", type=int, default=5)
    split.add_argument("--threshold", type=int, default=3)
    split.add_argument("--json", action="store_true")
    split.set_defaults(handler=cmd_shamir_split)
    combine = shamir.add_parser("combine")
    combine.add_argument("shares", nargs="+", help="shares in hex or base64")
    combine.add_argument("--json", action="store_true")
    combine.set_defaults(handler=cmd_shamir_combine)
    policy = sub.add_parser("policy", help="offline policy evaluation").add_subparsers(dest="policy_command", required=True)
    check = policy.add_parser("check", help="which rule decides a path, and why")
    check.add_argument("--policy", action="append", default=[], help="NAME=FILE (HCL or JSON), repeatable")
    check.add_argument("--path", action="append", required=True)
    check.add_argument("--operation", default="read", choices=["create", "read", "update", "delete", "list", "sudo"])
    check.add_argument("--no-default", action="store_true", help="leave out the built-in default policy")
    check.add_argument("--json", action="store_true")
    check.set_defaults(handler=cmd_policy_check)
    fake = sub.add_parser("fake-db", help="the built-in fake database").add_subparsers(dest="fake_command", required=True)
    login = fake.add_parser("login", help="check a username and password against a --fake-db file")
    login.add_argument("--file", required=True)
    login.add_argument("--username", required=True)
    login.add_argument("--password", required=True)
    login.set_defaults(handler=cmd_fake_db_login)
    replay = sub.add_parser("replay", help="run a scripted scenario against minivault or OpenBao and check each response")
    replay.add_argument("--addr", required=True, help="http://127.0.0.1:8200")
    replay.add_argument("--token", default="root")
    replay.add_argument("--script", required=True)
    replay.add_argument("--scenario", action="append", default=[])
    replay.add_argument("--var", action="append", default=[], help="NAME=VALUE (JSON or a string)")
    replay.add_argument("--vars-in")
    replay.add_argument("--vars-out")
    replay.add_argument("--json", action="store_true")
    replay.set_defaults(handler=cmd_replay)
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(list(sys.argv[1:] if argv is None else argv))
    try:
        return args.handler(args)
    except (InputError, EngineError) as exc:
        print(f"minivault: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
