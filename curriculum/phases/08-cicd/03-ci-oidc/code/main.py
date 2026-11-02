"""minioidc: both halves of CI-to-cloud federation. An OpenID Connect issuer like the one CI providers run (discovery document, JWKS, RS256 JWTs with GitHub's claims, the runner's idtoken endpoint), and the verifier a cloud runs before it trades the token for a short-lived session (JWKS cache, signature, issuer, audience, time, trust policy conditions, session)."""
from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import json
import math
import os
import re
import secrets
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

GITHUB_ISSUER = "https://token.actions.githubusercontent.com"
AWS_AUDIENCE = "sts.amazonaws.com"
ASSUME_ACTION = "sts:AssumeRoleWithWebIdentity"
DEFAULT_ACCOUNT = "123456789012"
ALGORITHM = "RS256"
PUBLIC_EXPONENT = 65537
SHA256_DIGEST_INFO = bytes.fromhex("3031300d060960864801650304020105000420")
RSA_ENCRYPTION_OID = bytes.fromhex("2a864886f70d010101")
PRIVATE_JWK_MEMBERS = ("d", "p", "q", "dp", "dq", "qi", "oth", "k")
MAX_JWKS_RSA_KEYS = 100
MIN_KEY_BITS = 1024
TOKEN_TTL = 300
NOT_BEFORE_SLACK = 600
LEEWAY = 60
JWKS_TTL = 3600
JWKS_MIN_REFRESH = 60
MIN_SESSION = 900
DEFAULT_SESSION = 3600
MAX_SESSION = 43200
DEFAULT_SESSION_NAME = "GitHubActions"
SESSION_NAME = re.compile(r"^[\w+=,.@-]{2,64}$")
ROLE_ARN = re.compile(r"^arn:(?P<partition>aws[\w-]*):iam::(?P<account>\d{12}):role/(?:[\w+=,.@/-]*/)?(?P<name>[\w+=,.@-]{1,64})$")
B64URL = re.compile(r"^[A-Za-z0-9_-]*$")
PEM_BLOCK = re.compile(r"-----BEGIN ([A-Z ]+)-----(.*?)-----END \1-----", re.S)
KEY_RETRIEVAL = "Couldn't retrieve verification key from your identity provider, please reference AssumeRoleWithWebIdentity documentation for requirements"
NOT_AUTHORIZED = "Not authorized to perform sts:AssumeRoleWithWebIdentity"
TOO_LONG = "The requested DurationSeconds exceeds the MaxSessionDuration set for this role."
DEFAULT_CONDITION_CLAIMS = ("amr", "aud", "email", "oaud", "sub")
EXTRA_CONDITION_CLAIMS = {
    "token.actions.githubusercontent.com": ("job_workflow_ref",),
    "gitlab.com": ("namespace_id", "project_id", "user_id", "user_login", "user_email", "user_access_level", "ref_protected", "pipeline_source", "runner_environment"),
}
IDENTITY_PROVIDER_CONTROLS = {
    "token.actions.githubusercontent.com": ("sub", "job_workflow_ref"),
    "gitlab.com": ("sub",),
    "app.terraform.io": ("sub",),
    "agent.buildkite.com": ("sub",),
    "cognito-identity.amazonaws.com": ("aud",),
}
STRING_OPERATORS = ("StringEquals", "StringNotEquals", "StringEqualsIgnoreCase", "StringNotEqualsIgnoreCase", "StringLike", "StringNotLike")
NEGATED = {"StringNotEquals": "StringEquals", "StringNotEqualsIgnoreCase": "StringEqualsIgnoreCase", "StringNotLike": "StringLike"}
SET_QUALIFIERS = ("", "ForAnyValue", "ForAllValues")
SEVERITIES = ("CRITICAL", "HIGH", "MEDIUM", "LOW")
CLAIMS_SUPPORTED = [
    "sub", "aud", "exp", "iat", "iss", "jti", "nbf", "ref", "sha", "repository", "repository_id", "repository_owner",
    "repository_owner_id", "run_id", "run_number", "run_attempt", "actor", "actor_id", "workflow", "workflow_ref",
    "workflow_sha", "head_ref", "base_ref", "event_name", "ref_type", "environment", "job_workflow_ref",
    "job_workflow_sha", "repository_visibility", "runner_environment",
]
JOB_DEFAULTS: Dict[str, Any] = {
    "ref": "refs/heads/main",
    "sha": "8e1f2c4a9b7d6e5f4a3b2c1d0e9f8a7b6c5d4e3f",
    "event_name": "push",
    "environment": "",
    "workflow": "deploy",
    "workflow_file": ".github/workflows/deploy.yml",
    "job_workflow_ref": "",
    "actor": "octocat",
    "actor_id": "12",
    "repository_id": "74",
    "repository_owner_id": "65",
    "repository_visibility": "private",
    "runner_environment": "github-hosted",
    "run_id": "1",
    "run_number": "1",
    "run_attempt": "1",
    "head_ref": "",
    "base_ref": "",
    "immutable_subject": False,
    "include_claim_keys": [],
}

Clock = Callable[[], float]
Fetch = Callable[[str], bytes]


class InputError(ValueError):
    pass


class PolicyError(ValueError):
    pass


class CommandError(Exception):
    pass


class StsError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message

    def to_json(self) -> Dict[str, str]:
        return {"Code": self.code, "Message": self.message}

    def cli_text(self) -> str:
        return f"An error occurred ({self.code}) when calling the AssumeRoleWithWebIdentity operation: {self.message}"


def b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def b64url_decode(text: Any) -> bytes:
    if not isinstance(text, str) or not B64URL.match(text) or len(text) % 4 == 1:
        raise ValueError(f"not base64url without padding: {str(text)[:24]!r}")
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def int_bytes(value: int, length: int = 0) -> bytes:
    return value.to_bytes(length or max(1, (value.bit_length() + 7) // 8), "big")


def bytes_int(data: bytes) -> int:
    return int.from_bytes(data, "big")


def compact_json(value: Any) -> bytes:
    return json.dumps(value, separators=(",", ":")).encode("utf-8")


def iso(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def as_list(value: Any) -> List[Any]:
    if value is None:
        return []
    return list(value) if isinstance(value, list) else [value]


def sieve(limit: int) -> List[int]:
    flags = bytearray([1]) * (limit + 1)
    flags[0:2] = b"\x00\x00"
    for n in range(2, math.isqrt(limit) + 1):
        if flags[n]:
            flags[n * n :: n] = bytearray(len(flags[n * n :: n]))
    return [n for n, prime in enumerate(flags) if prime]


SMALL_PRIMES = sieve(2000)


def is_probable_prime(n: int, rounds: int = 40, bases: Optional[Sequence[int]] = None) -> bool:
    if n < 2:
        return False
    for p in SMALL_PRIMES:
        if n % p == 0:
            return n == p
    d, s = n - 1, 0
    while d % 2 == 0:
        d //= 2
        s += 1
    witnesses = list(bases) if bases is not None else [2 + secrets.randbelow(n - 3) for _ in range(rounds)]
    for a in witnesses:
        x = pow(a, d, n)
        if x in (1, n - 1):
            continue
        for _ in range(s - 1):
            x = pow(x, 2, n)
            if x == n - 1:
                break
        else:
            return False
    return True


def generate_prime(bits: int) -> int:
    while True:
        candidate = secrets.randbits(bits) | (3 << (bits - 2)) | 1
        if candidate % PUBLIC_EXPONENT != 1 and is_probable_prime(candidate):
            return candidate


@dataclass
class RsaKey:
    n: int
    e: int
    kid: str = ""
    d: int = 0
    p: int = 0
    q: int = 0
    dp: int = 0
    dq: int = 0
    qi: int = 0

    @property
    def size(self) -> int:
        return (self.n.bit_length() + 7) // 8

    @property
    def bits(self) -> int:
        return self.n.bit_length()

    @property
    def private(self) -> bool:
        return self.d != 0

    def public(self) -> "RsaKey":
        return RsaKey(self.n, self.e, self.kid)

    def public_jwk(self) -> Dict[str, str]:
        return {"kty": "RSA", "alg": ALGORITHM, "use": "sig", "kid": self.kid, "n": b64url_encode(int_bytes(self.n)), "e": b64url_encode(int_bytes(self.e))}

    def private_jwk(self) -> Dict[str, str]:
        jwk = self.public_jwk()
        for name in ("d", "p", "q", "dp", "dq", "qi"):
            jwk[name] = b64url_encode(int_bytes(getattr(self, name)))
        return jwk


def jwk_thumbprint(jwk: Dict[str, Any]) -> str:
    canonical = json.dumps({"e": jwk["e"], "kty": jwk["kty"], "n": jwk["n"]}, separators=(",", ":"), sort_keys=True)
    return b64url_encode(hashlib.sha256(canonical.encode("utf-8")).digest())


def generate_key(bits: int = 2048, kid: str = "") -> RsaKey:
    if bits < MIN_KEY_BITS or bits % 16:
        raise InputError(f"RS256 keys need at least {MIN_KEY_BITS} bits in a multiple of 16 (use 2048 or more outside tests), not {bits}")
    while True:
        p, q = generate_prime(bits // 2), generate_prime(bits // 2)
        n = p * q
        if p == q or n.bit_length() != bits:
            continue
        carmichael = (p - 1) * (q - 1) // math.gcd(p - 1, q - 1)
        d = pow(PUBLIC_EXPONENT, -1, carmichael)
        key = RsaKey(n, PUBLIC_EXPONENT, "", d, p, q, d % (p - 1), d % (q - 1), pow(q, -1, p))
        key.kid = kid or jwk_thumbprint(key.public_jwk())
        return key


def key_from_jwk(jwk: Any) -> RsaKey:
    if not isinstance(jwk, dict) or jwk.get("kty") != "RSA":
        raise InputError("expected an RSA JWK: an object with kty RSA, n and e")
    try:
        values = {name: bytes_int(b64url_decode(jwk[name])) for name in ("n", "e", "d", "p", "q", "dp", "dq", "qi") if name in jwk}
    except ValueError as exc:
        raise InputError(f"JWK {jwk.get('kid', '')!r}: {exc}")
    if "n" not in values or "e" not in values:
        raise InputError(f"JWK {jwk.get('kid', '')!r} has no n or e")
    key = RsaKey(values["n"], values["e"], str(jwk.get("kid") or ""), *(values.get(name, 0) for name in ("d", "p", "q", "dp", "dq", "qi")))
    key.kid = key.kid or jwk_thumbprint(jwk)
    return key


def rsa_private_op(key: RsaKey, m: int) -> int:
    if key.p and key.q:
        m1, m2 = pow(m, key.dp, key.p), pow(m, key.dq, key.q)
        s = m2 + ((key.qi * (m1 - m2)) % key.p) * key.q
    else:
        s = pow(m, key.d, key.n)
    if pow(s, key.e, key.n) != m:
        raise RuntimeError(f"the RSA private operation with key {key.kid} gave a wrong result; a faulty CRT signature leaks the key, so it is not released")
    return s


def emsa_pkcs1_v15(message: bytes, length: int) -> bytes:
    digest_info = SHA256_DIGEST_INFO + hashlib.sha256(message).digest()
    padding = length - len(digest_info) - 3
    if padding < 8:
        raise InputError(f"a {length * 8}-bit modulus is too small for SHA-256 PKCS#1 v1.5")
    return b"\x00\x01" + b"\xff" * padding + b"\x00" + digest_info


def rsa_sign(key: RsaKey, message: bytes) -> bytes:
    if not key.private:
        raise InputError(f"key {key.kid} has no private part; sign with the issuer's private key file")
    encoded = emsa_pkcs1_v15(message, key.size)
    return int_bytes(rsa_private_op(key, bytes_int(encoded)), key.size)


def rsa_verify(key: RsaKey, message: bytes, signature: bytes) -> bool:
    if len(signature) != key.size:
        return False
    s = bytes_int(signature)
    if s >= key.n:
        return False
    return hmac.compare_digest(int_bytes(pow(s, key.e, key.n), key.size), emsa_pkcs1_v15(message, key.size))


def der(tag: int, content: bytes) -> bytes:
    if len(content) < 0x80:
        head = bytes([len(content)])
    else:
        raw = int_bytes(len(content))
        head = bytes([0x80 | len(raw)]) + raw
    return bytes([tag]) + head + content


def der_integer(value: int) -> bytes:
    return der(0x02, value.to_bytes(value.bit_length() // 8 + 1, "big"))


def spki_der(key: RsaKey) -> bytes:
    rsa_public = der(0x30, der_integer(key.n) + der_integer(key.e))
    algorithm = der(0x30, der(0x06, RSA_ENCRYPTION_OID) + der(0x05, b""))
    return der(0x30, algorithm + der(0x03, b"\x00" + rsa_public))


def to_pem(label: str, data: bytes) -> str:
    body = base64.b64encode(data).decode("ascii")
    lines = [body[i : i + 64] for i in range(0, len(body), 64)]
    return f"-----BEGIN {label}-----\n" + "\n".join(lines) + f"\n-----END {label}-----\n"


def der_read(data: bytes, offset: int = 0) -> Tuple[int, bytes, int]:
    if offset + 2 > len(data):
        raise InputError("truncated DER")
    tag, length = data[offset], data[offset + 1]
    offset += 2
    if length & 0x80:
        count = length & 0x7F
        if count == 0 or count > 4 or offset + count > len(data):
            raise InputError("unsupported DER length")
        length = bytes_int(data[offset : offset + count])
        offset += count
    if offset + length > len(data):
        raise InputError("truncated DER")
    return tag, data[offset : offset + length], offset + length


def der_items(data: bytes) -> List[Tuple[int, bytes]]:
    items: List[Tuple[int, bytes]] = []
    offset = 0
    while offset < len(data):
        tag, content, offset = der_read(data, offset)
        items.append((tag, content))
    return items


def public_key_from_pem(text: str, kid: str = "") -> RsaKey:
    match = PEM_BLOCK.search(text)
    if not match:
        raise InputError("no PEM block found; export the public key with: openssl rsa -in key.pem -pubout -out pub.pem")
    label = match.group(1)
    try:
        data = base64.b64decode("".join(match.group(2).split()), validate=True)
    except ValueError:
        raise InputError("the PEM body is not base64")
    if label == "PUBLIC KEY":
        tag, content, _ = der_read(data)
        parts = der_items(content)
        if tag != 0x30 or len(parts) != 2 or parts[0][0] != 0x30 or parts[1][0] != 0x03:
            raise InputError("not a SubjectPublicKeyInfo")
        if der_items(parts[0][1])[:1] != [(0x06, RSA_ENCRYPTION_OID)]:
            raise InputError("the key is not rsaEncryption; RS256 needs an RSA key (EC keys sign ES256)")
        if not parts[1][1] or parts[1][1][0] != 0:
            raise InputError("malformed BIT STRING in SubjectPublicKeyInfo")
        data = parts[1][1][1:]
    elif label != "RSA PUBLIC KEY":
        raise InputError(f"PEM block {label} is not a public key; export one with openssl rsa -pubout")
    tag, content, _ = der_read(data)
    numbers = der_items(content)
    if tag != 0x30 or len(numbers) != 2 or any(t != 0x02 for t, _ in numbers):
        raise InputError("not an RSAPublicKey (SEQUENCE of modulus and exponent)")
    key = RsaKey(bytes_int(numbers[0][1]), bytes_int(numbers[1][1]), kid)
    key.kid = key.kid or jwk_thumbprint(key.public_jwk())
    return key


@dataclass
class Jwt:
    header: Dict[str, Any]
    claims: Dict[str, Any]
    signing_input: bytes
    signature: bytes


def encode_jwt(claims: Dict[str, Any], key: RsaKey, header: Optional[Dict[str, Any]] = None) -> str:
    head: Dict[str, Any] = {"typ": "JWT", "alg": ALGORITHM, "kid": key.kid}
    head.update(header or {})
    signing_input = b64url_encode(compact_json(head)) + "." + b64url_encode(compact_json(claims))
    return signing_input + "." + b64url_encode(rsa_sign(key, signing_input.encode("ascii")))


def decode_jwt(token: str) -> Jwt:
    parts = token.strip().split(".")
    if len(parts) != 3:
        raise StsError("InvalidIdentityToken", f"a JWT is three base64url parts joined by dots; this one has {len(parts)}")
    try:
        header = json.loads(b64url_decode(parts[0]))
        claims = json.loads(b64url_decode(parts[1]))
        signature = b64url_decode(parts[2])
    except (ValueError, UnicodeDecodeError) as exc:
        raise StsError("InvalidIdentityToken", f"not a JWS compact serialization: {exc}")
    if not isinstance(header, dict) or not isinstance(claims, dict):
        raise StsError("InvalidIdentityToken", "the JWT header and payload must be JSON objects")
    return Jwt(header, claims, (parts[0] + "." + parts[1]).encode("ascii"), signature)


def load_job(value: Any, name: str = "job") -> Dict[str, Any]:
    if not isinstance(value, dict) or not isinstance(value.get("repository"), str) or value["repository"].count("/") != 1:
        raise InputError(f"{name}: a job context needs repository as OWNER/NAME")
    job = dict(JOB_DEFAULTS)
    job.update(value)
    return job


def load_jobs(doc: Any) -> Dict[str, Dict[str, Any]]:
    if not isinstance(doc, dict):
        raise InputError("a jobs file maps job names to job contexts")
    return {name: load_job(value, name) for name, value in doc.items() if not name.startswith("_")}


def escape_subject(value: str) -> str:
    return value.replace(":", "%3A")


def subject_context(job: Dict[str, Any]) -> str:
    if job["environment"]:
        return "environment:" + escape_subject(job["environment"])
    if job["event_name"] == "pull_request":
        return "pull_request"
    return "ref:" + escape_subject(job["ref"])


def subject_repo(job: Dict[str, Any]) -> str:
    if not job["immutable_subject"]:
        return job["repository"]
    owner, name = job["repository"].split("/")
    return f"{owner}@{job['repository_owner_id']}/{name}@{job['repository_id']}"


def github_subject(job: Dict[str, Any], claims: Dict[str, Any]) -> str:
    parts = []
    for key in job["include_claim_keys"] or ["repo", "context"]:
        if key == "repo":
            parts.append("repo:" + subject_repo(job))
        elif key == "context":
            parts.append(subject_context(job))
        elif key in claims:
            parts.append(f"{key}:{escape_subject(str(claims[key]))}")
        else:
            raise InputError(f"include_claim_keys names {key!r}, which is not a claim of this token")
    return ":".join(parts)


def github_claims(job: Dict[str, Any], audience: str, issuer: str, now: int, ttl: int = TOKEN_TTL, jti: str = "") -> Dict[str, Any]:
    owner = job["repository"].split("/")[0]
    workflow_ref = f"{job['repository']}/{job['workflow_file']}@{job['ref']}"
    claims: Dict[str, Any] = {
        "jti": jti or str(uuid.uuid4()),
        "sub": "",
        "aud": audience or f"https://github.com/{owner}",
        "ref": job["ref"],
        "sha": job["sha"],
        "repository": job["repository"],
        "repository_owner": owner,
        "repository_owner_id": str(job["repository_owner_id"]),
        "repository_id": str(job["repository_id"]),
        "repository_visibility": job["repository_visibility"],
        "actor": job["actor"],
        "actor_id": str(job["actor_id"]),
        "run_id": str(job["run_id"]),
        "run_number": str(job["run_number"]),
        "run_attempt": str(job["run_attempt"]),
        "runner_environment": job["runner_environment"],
        "workflow": job["workflow"],
        "workflow_ref": workflow_ref,
        "workflow_sha": job["sha"],
        "job_workflow_ref": job["job_workflow_ref"] or workflow_ref,
        "job_workflow_sha": job["sha"],
        "head_ref": job["head_ref"],
        "base_ref": job["base_ref"],
        "event_name": job["event_name"],
        "ref_type": "tag" if job["ref"].startswith("refs/tags/") else "branch",
        "iss": issuer,
        "nbf": now - NOT_BEFORE_SLACK,
        "exp": now + ttl,
        "iat": now,
    }
    if job["environment"]:
        claims["environment"] = job["environment"]
    claims["sub"] = github_subject(job, claims)
    return claims


class Issuer:
    def __init__(self, issuer: str, keys: Sequence[RsaKey], ttl: int = TOKEN_TTL, clock: Clock = time.time) -> None:
        if not keys or not all(key.private for key in keys):
            raise InputError("an issuer needs private keys; the first one signs, all of them are published")
        self.issuer = issuer.rstrip("/")
        self.keys = list(keys)
        self.ttl = ttl
        self.clock = clock
        self.jobs: Dict[str, Tuple[str, Dict[str, Any]]] = {}

    @property
    def signing_key(self) -> RsaKey:
        return self.keys[0]

    def discovery(self, base_url: str = "") -> Dict[str, Any]:
        base = (base_url or self.issuer).rstrip("/")
        return {
            "issuer": self.issuer,
            "jwks_uri": base + "/.well-known/jwks",
            "subject_types_supported": ["public", "pairwise"],
            "response_types_supported": ["id_token"],
            "claims_supported": CLAIMS_SUPPORTED,
            "id_token_signing_alg_values_supported": [ALGORITHM],
            "scopes_supported": ["openid"],
        }

    def jwks(self) -> Dict[str, Any]:
        return {"keys": [key.public_jwk() for key in self.keys]}

    def register(self, name: str, job: Dict[str, Any]) -> str:
        bearer = secrets.token_urlsafe(32)
        self.jobs[bearer] = (name, job)
        return bearer

    def mint(self, job: Dict[str, Any], audience: str = "", now: Optional[int] = None) -> str:
        at = int(self.clock()) if now is None else now
        return encode_jwt(github_claims(job, audience, self.issuer, at, self.ttl), self.signing_key)

    def rotate(self, key: RsaKey) -> None:
        self.keys.insert(0, key)

    def retire(self, kid: str) -> None:
        self.keys = [key for key in self.keys if key.kid != kid] or self.keys


class IssuerServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address: Tuple[str, int], issuer: Issuer, log: Optional[Callable[[str], None]] = None) -> None:
        super().__init__(address, IssuerHandler)
        self.issuer = issuer
        self.log = log
        host, port = self.server_address[:2]
        self.base_url = f"http://{host}:{port}"

    @property
    def token_url(self) -> str:
        return self.base_url + "/idtoken?api-version=2.0"


class IssuerHandler(BaseHTTPRequestHandler):
    server: IssuerServer

    def log_message(self, fmt: str, *args: Any) -> None:
        pass

    def note(self, line: str) -> None:
        if self.server.log:
            self.server.log(line)

    def send_json(self, status: int, value: Any, cache: bool = False) -> None:
        body = json.dumps(value, indent=2).encode("utf-8") + b"\n"
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "public, max-age=3600" if cache else "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        url = urllib.parse.urlsplit(self.path)
        issuer = self.server.issuer
        if url.path == "/.well-known/openid-configuration":
            self.note("GET /.well-known/openid-configuration 200")
            self.send_json(200, issuer.discovery(self.server.base_url), cache=True)
        elif url.path == "/.well-known/jwks":
            self.note(f"GET /.well-known/jwks 200 keys={','.join(key.kid for key in issuer.keys)}")
            self.send_json(200, issuer.jwks(), cache=True)
        elif url.path == "/idtoken":
            self.id_token(urllib.parse.parse_qs(url.query))
        else:
            self.send_json(404, {"message": f"no route {url.path}"})

    def id_token(self, query: Dict[str, List[str]]) -> None:
        scheme, _, bearer = self.headers.get("Authorization", "").partition(" ")
        entry = self.server.issuer.jobs.get(bearer.strip()) if scheme.lower() == "bearer" else None
        if entry is None:
            self.note("GET /idtoken 401")
            self.send_json(401, {"message": "missing or unknown bearer token: the runner sets ACTIONS_ID_TOKEN_REQUEST_TOKEN only for jobs with permissions: id-token: write"})
            return
        name, job = entry
        token = self.server.issuer.mint(job, (query.get("audience") or [""])[0])
        claims = decode_jwt(token).claims
        self.note(f"GET /idtoken 200 job={name} aud={claims['aud']} sub={claims['sub']}")
        self.send_json(200, {"value": token})


def make_server(issuer: Issuer, host: str = "127.0.0.1", port: int = 0, log: Optional[Callable[[str], None]] = None) -> IssuerServer:
    return IssuerServer((host, port), issuer, log)


def request_id_token(url: str, bearer: str, audience: str = "", timeout: float = 10.0) -> str:
    if audience:
        url += ("&" if "?" in url else "?") + "audience=" + urllib.parse.quote(audience, safe="")
    request = urllib.request.Request(url, headers={"Authorization": f"bearer {bearer}", "Accept": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = json.loads(response.read())
    except urllib.error.HTTPError as exc:
        raise CommandError(f"the issuer answered {exc.code}: {exc.read().decode('utf-8', 'replace').strip()}")
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise CommandError(f"cannot request a token from {url}: {getattr(exc, 'reason', exc)}")
    if not isinstance(body, dict) or not isinstance(body.get("value"), str):
        raise CommandError("the issuer's answer has no value field")
    return body["value"]


def load_url(url: str, timeout: float = 5.0) -> bytes:
    if not url.startswith(("http://", "https://")):
        try:
            with open(url, "rb") as handle:
                return handle.read()
        except OSError as exc:
            raise StsError("IDPCommunicationError", f"cannot read {url}: {exc.strerror}")
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers={"Accept": "application/json"}), timeout=timeout) as response:
            return response.read(1 << 20)
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise StsError("IDPCommunicationError", f"cannot fetch {url}: {getattr(exc, 'reason', exc)}")


def fetch_json(fetch: Fetch, url: str) -> Any:
    raw = fetch(url)
    try:
        return json.loads(raw)
    except ValueError:
        raise StsError("InvalidIdentityToken", KEY_RETRIEVAL + f" ({url} did not return JSON)")


def issuer_fetch(*issuers: Issuer) -> Fetch:
    routes: Dict[str, Callable[[], Any]] = {}
    for issuer in issuers:
        routes[issuer.issuer + "/.well-known/openid-configuration"] = issuer.discovery
        routes[issuer.issuer + "/.well-known/jwks"] = issuer.jwks

    def fetch(url: str) -> bytes:
        if url not in routes:
            raise StsError("IDPCommunicationError", f"cannot fetch {url}: no issuer in this lab serves it")
        return compact_json(routes[url]())

    return fetch


def parse_jwks(doc: Any) -> Dict[str, RsaKey]:
    keys = doc.get("keys") if isinstance(doc, dict) else None
    if not isinstance(keys, list) or not keys:
        raise StsError("InvalidIdentityToken", KEY_RETRIEVAL + " (the JWKS has no keys)")
    leaked = [str(jwk.get("kid", "?")) for jwk in keys if isinstance(jwk, dict) and any(member in jwk for member in PRIVATE_JWK_MEMBERS)]
    if leaked:
        raise StsError("InvalidIdentityToken", KEY_RETRIEVAL + f" (the JWKS publishes private key members for kid {', '.join(leaked)}; treat those keys as compromised)")
    rsa = [jwk for jwk in keys if isinstance(jwk, dict) and jwk.get("kty") == "RSA"]
    if len(rsa) > MAX_JWKS_RSA_KEYS:
        raise StsError("InvalidIdentityToken", KEY_RETRIEVAL + f" (more than {MAX_JWKS_RSA_KEYS} RSA keys)")
    result: Dict[str, RsaKey] = {}
    for jwk in rsa:
        if jwk.get("use", "sig") != "sig" or jwk.get("alg", ALGORITHM) != ALGORITHM:
            continue
        try:
            key = key_from_jwk(jwk)
        except InputError:
            continue
        if key.bits >= MIN_KEY_BITS:
            result[key.kid] = key
    return result


class JwksCache:
    def __init__(self, issuer: str, fetch: Fetch = load_url, discovery_url: str = "", jwks_url: str = "", ttl: float = JWKS_TTL, min_refresh: float = JWKS_MIN_REFRESH, clock: Clock = time.time) -> None:
        self.issuer = issuer
        self.fetch = fetch
        self.discovery_url = discovery_url or issuer.rstrip("/") + "/.well-known/openid-configuration"
        self.jwks_url = jwks_url
        self.ttl = ttl
        self.min_refresh = min_refresh
        self.clock = clock
        self.keys: Dict[str, RsaKey] = {}
        self.fetched_at: Optional[float] = None
        self.fetches = 0
        self.lock = threading.Lock()

    def refresh(self) -> None:
        jwks_uri = self.jwks_url
        if not jwks_uri:
            discovery = fetch_json(self.fetch, self.discovery_url)
            named = discovery.get("issuer") if isinstance(discovery, dict) else None
            if named != self.issuer:
                raise StsError("InvalidIdentityToken", f"the discovery document at {self.discovery_url} names issuer {named!r}, not {self.issuer!r}")
            jwks_uri = discovery.get("jwks_uri")
            if not isinstance(jwks_uri, str) or not jwks_uri:
                raise StsError("InvalidIdentityToken", KEY_RETRIEVAL + " (the discovery document has no jwks_uri)")
        self.keys = parse_jwks(fetch_json(self.fetch, jwks_uri))
        self.fetched_at = self.clock()
        self.fetches += 1

    def key(self, kid: str) -> RsaKey:
        with self.lock:
            now = self.clock()
            if self.fetched_at is None or now - self.fetched_at >= self.ttl:
                self.refresh()
            elif kid not in self.keys and now - self.fetched_at >= self.min_refresh:
                self.refresh()
            if kid not in self.keys:
                raise StsError("InvalidIdentityToken", f"no key with kid {kid!r} in the JWKS of {self.issuer} (it has {', '.join(sorted(self.keys)) or 'none'})")
            return self.keys[kid]


@dataclass
class Provider:
    url: str
    client_ids: List[str]
    arn: str

    @property
    def issuer(self) -> str:
        return "https://" + self.url


def load_provider(doc: Any, account: str = DEFAULT_ACCOUNT) -> Provider:
    if not isinstance(doc, dict) or not isinstance(doc.get("Url"), str) or not doc["Url"]:
        raise InputError('a provider is the output of aws iam get-open-id-connect-provider: {"Url": "...", "ClientIDList": [...]}')
    url = re.sub(r"^https://", "", doc["Url"]).rstrip("/")
    clients = doc.get("ClientIDList") or []
    if not isinstance(clients, list) or not all(isinstance(c, str) for c in clients):
        raise InputError(f"provider {url}: ClientIDList must be a list of strings")
    return Provider(url, list(clients), doc.get("Arn") or f"arn:aws:iam::{account}:oidc-provider/{url}")


def audiences(claims: Dict[str, Any]) -> List[str]:
    if isinstance(claims.get("azp"), str) and claims["azp"]:
        return [claims["azp"]]
    return [value for value in as_list(claims.get("aud")) if isinstance(value, str)]


def is_timestamp(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


@dataclass
class Verified:
    claims: Dict[str, Any]
    header: Dict[str, Any]
    provider: Provider
    kid: str

    @property
    def audience(self) -> str:
        return (audiences(self.claims) or [""])[0]


class Verifier:
    def __init__(self, providers: Sequence[Provider], fetch: Fetch = load_url, leeway: int = LEEWAY, clock: Clock = time.time, discovery_url: str = "", jwks_url: str = "", jwks_ttl: float = JWKS_TTL) -> None:
        self.providers = {provider.issuer: provider for provider in providers}
        self.fetch = fetch
        self.leeway = leeway
        self.clock = clock
        self.discovery_url = discovery_url
        self.jwks_url = jwks_url
        self.jwks_ttl = jwks_ttl
        self.caches: Dict[str, JwksCache] = {}

    def cache_for(self, provider: Provider) -> JwksCache:
        if provider.issuer not in self.caches:
            self.caches[provider.issuer] = JwksCache(provider.issuer, self.fetch, self.discovery_url, self.jwks_url, self.jwks_ttl, clock=self.clock)
        return self.caches[provider.issuer]

    def verify(self, token: str) -> Verified:
        jwt = decode_jwt(token)
        alg = jwt.header.get("alg")
        if alg != ALGORITHM:
            raise StsError("InvalidIdentityToken", f"unsupported alg {alg!r}: only {ALGORITHM} is accepted, so 'none' and HMAC tokens are refused before any key is chosen")
        kid = jwt.header.get("kid")
        if not isinstance(kid, str) or not kid:
            raise StsError("InvalidIdentityToken", "the token header has no kid, so no published key can be chosen")
        issuer = jwt.claims.get("iss")
        provider = self.providers.get(issuer) if isinstance(issuer, str) else None
        if provider is None:
            raise StsError("InvalidIdentityToken", f"No OpenIDConnect provider found in your account for {issuer}")
        key = self.cache_for(provider).key(kid)
        if not rsa_verify(key, jwt.signing_input, jwt.signature):
            raise StsError("InvalidIdentityToken", f"the signature does not verify against key {kid} of {issuer}")
        self.check_time(jwt.claims)
        self.check_audience(jwt.claims, provider)
        if not isinstance(jwt.claims.get("sub"), str) or not jwt.claims["sub"]:
            raise StsError("InvalidIdentityToken", "the token has no sub claim")
        return Verified(jwt.claims, jwt.header, provider, kid)

    def check_time(self, claims: Dict[str, Any]) -> None:
        now = int(self.clock())
        for name in ("exp", "iat", "nbf"):
            if (name != "nbf" or name in claims) and not is_timestamp(claims.get(name)):
                raise StsError("InvalidIdentityToken", f"claim {name} must be an integer timestamp, not {claims.get(name)!r}")
        if now >= claims["exp"] + self.leeway:
            raise StsError("ExpiredTokenException", f"Token expired: current date/time {iso(now)} must be before the expiration date/time {iso(claims['exp'])}")
        if "nbf" in claims and now + self.leeway < claims["nbf"]:
            raise StsError("InvalidIdentityToken", f"the token is not valid before {iso(claims['nbf'])} (now {iso(now)}, leeway {self.leeway}s)")
        if claims["iat"] > now + self.leeway:
            raise StsError("InvalidIdentityToken", f"the token was issued in the future ({iso(claims['iat'])}, now {iso(now)}); check the issuer's clock")

    def check_audience(self, claims: Dict[str, Any], provider: Provider) -> None:
        values = audiences(claims)
        if not values:
            raise StsError("InvalidIdentityToken", "the token has no aud claim")
        if provider.client_ids and not any(value in provider.client_ids for value in values):
            raise StsError("InvalidIdentityToken", f"Incorrect token audience: {', '.join(values)} is not in the ClientIDList of {provider.arn} ({', '.join(provider.client_ids)})")


def parse_operator(name: str) -> Tuple[str, str, bool]:
    qualifier, separator, base = name.partition(":")
    if not separator:
        qualifier, base = "", name
    if qualifier not in SET_QUALIFIERS:
        raise PolicyError(f"unknown set qualifier in condition operator {name}")
    if_exists = base.endswith("IfExists")
    if if_exists:
        base = base[: -len("IfExists")]
    if base == "Null" and (if_exists or qualifier):
        raise PolicyError(f"{name}: Null takes no qualifier or IfExists")
    if base not in STRING_OPERATORS and base != "Null":
        raise PolicyError(f"condition operator {name} is not supported here; web identity trust policies use the String operators and Null")
    return qualifier, base, if_exists


def glob_regex(pattern: str, ignore_case: bool = False) -> "re.Pattern[str]":
    body = "".join(".*" if c == "*" else "." if c == "?" else re.escape(c) for c in pattern)
    return re.compile(body, re.DOTALL | (re.IGNORECASE if ignore_case else 0))


def value_matches(base: str, value: str, wanted: str) -> bool:
    if base == "StringEquals":
        return value == wanted
    if base == "StringEqualsIgnoreCase":
        return value.lower() == wanted.lower()
    return glob_regex(wanted).fullmatch(value) is not None


def key_holds(operator: str, wanted: Sequence[str], present: Sequence[str]) -> bool:
    qualifier, base, if_exists = parse_operator(operator)
    if base == "Null":
        return any((value.lower() == "true") == (not present) for value in wanted)
    if not present:
        if if_exists or qualifier == "ForAllValues":
            return True
        if qualifier == "ForAnyValue":
            return False
        return base in NEGATED
    negated = base in NEGATED
    positive = NEGATED.get(base, base)

    def one(value: str) -> bool:
        hit = any(value_matches(positive, value, item) for item in wanted)
        return not hit if negated else hit

    if qualifier == "ForAllValues":
        return all(one(value) for value in present)
    if qualifier == "ForAnyValue":
        return any(one(value) for value in present)
    return all(one(value) for value in present) if negated else any(one(value) for value in present)


@dataclass
class Check:
    operator: str
    key: str
    wanted: List[str]
    actual: List[str]
    passed: bool

    def describe(self) -> str:
        got = ", ".join(self.actual) if self.actual else "(missing)"
        return f"{'pass' if self.passed else 'FAIL'}  {self.operator} {self.key} {' | '.join(self.wanted)}  <- {got}"

    def to_json(self) -> Dict[str, Any]:
        return {"operator": self.operator, "key": self.key, "wanted": self.wanted, "actual": self.actual, "passed": self.passed}


def condition_text(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def condition_checks(block: Any, context: Dict[str, List[str]]) -> List[Check]:
    if block is None:
        return []
    if not isinstance(block, dict):
        raise PolicyError("Condition must be an object of operators")
    checks = []
    for operator, keys in block.items():
        if not isinstance(keys, dict):
            raise PolicyError(f"{operator} must map condition keys to values")
        for key, wanted in keys.items():
            values = [condition_text(value) for value in as_list(wanted)]
            actual = context.get(key.lower(), [])
            checks.append(Check(operator, key, values, list(actual), key_holds(operator, values, actual)))
    return checks


def condition_claims(host: str) -> Tuple[str, ...]:
    return DEFAULT_CONDITION_CLAIMS + EXTRA_CONDITION_CLAIMS.get(host.lower(), ())


def request_context(claims: Dict[str, Any], provider: Provider) -> Dict[str, List[str]]:
    context: Dict[str, List[str]] = {}
    for claim in condition_claims(provider.url):
        if claim == "aud":
            values: List[Any] = audiences(claims)
        elif claim == "oaud":
            values = as_list(claims.get("aud"))
        else:
            values = as_list(claims.get(claim))
        strings = [condition_text(value) for value in values if isinstance(value, (str, int, bool))]
        if strings:
            context[f"{provider.url}:{claim}".lower()] = strings
    return context


def statements_of(policy: Any) -> List[Dict[str, Any]]:
    if not isinstance(policy, dict) or "Statement" not in policy:
        raise PolicyError("a trust policy is a JSON object with a Statement")
    statements = as_list(policy["Statement"])
    if not statements or not all(isinstance(s, dict) for s in statements):
        raise PolicyError("Statement must be an object or a list of objects")
    return statements


def action_matches(element: Any, action: str) -> bool:
    return any(glob_regex(str(pattern), ignore_case=True).fullmatch(action) for pattern in as_list(element))


def federated_names(element: Any) -> List[str]:
    if element == "*":
        return ["*"]
    if not isinstance(element, dict):
        return []
    return [str(value) for value in as_list(element.get("Federated"))]


def principal_matches(element: Any, provider: Provider) -> bool:
    names = federated_names(element)
    return "*" in names or provider.arn in names or provider.url in names


@dataclass
class StatementResult:
    index: int
    sid: str
    effect: str
    principal: bool
    action: bool
    checks: List[Check]

    @property
    def applies(self) -> bool:
        return self.principal and self.action and all(check.passed for check in self.checks)

    @property
    def label(self) -> str:
        return f"statement {self.index}" + (f' "{self.sid}"' if self.sid else "")

    def to_json(self) -> Dict[str, Any]:
        return {"index": self.index, "sid": self.sid, "effect": self.effect, "principal": self.principal, "action": self.action, "applies": self.applies, "conditions": [c.to_json() for c in self.checks]}


@dataclass
class TrustDecision:
    decision: str
    statements: List[StatementResult]
    context: Dict[str, List[str]]

    @property
    def allowed(self) -> bool:
        return self.decision == "allowed"


def evaluate_trust(policy: Any, claims: Dict[str, Any], provider: Provider) -> TrustDecision:
    context = request_context(claims, provider)
    results = []
    for index, statement in enumerate(statements_of(policy)):
        effect = statement.get("Effect")
        if effect not in ("Allow", "Deny"):
            raise PolicyError(f"statement {index}: Effect must be Allow or Deny, not {effect!r}")
        if "NotAction" in statement:
            action = not action_matches(statement["NotAction"], ASSUME_ACTION)
        else:
            action = action_matches(statement.get("Action"), ASSUME_ACTION)
        principal = principal_matches(statement.get("Principal"), provider)
        checks = condition_checks(statement.get("Condition"), context)
        results.append(StatementResult(index, str(statement.get("Sid", "")), effect, principal, action, checks))
    if any(r.applies and r.effect == "Deny" for r in results):
        decision = "explicitDeny"
    elif any(r.applies and r.effect == "Allow" for r in results):
        decision = "allowed"
    else:
        decision = "implicitDeny"
    return TrustDecision(decision, results, context)


@dataclass
class Session:
    access_key_id: str
    secret_access_key: str
    session_token: str
    expiration: int
    arn: str
    assumed_role_id: str
    subject: str
    audience: str
    provider: str

    def to_json(self) -> Dict[str, Any]:
        return {
            "Credentials": {"AccessKeyId": self.access_key_id, "SecretAccessKey": self.secret_access_key, "SessionToken": self.session_token, "Expiration": iso(self.expiration)},
            "SubjectFromWebIdentityToken": self.subject,
            "AssumedRoleUser": {"Arn": self.arn, "AssumedRoleId": self.assumed_role_id},
            "Provider": self.provider,
            "Audience": self.audience,
        }


def role_unique_id(role_arn: str) -> str:
    return "AROA" + base64.b32encode(hashlib.sha256(role_arn.encode("utf-8")).digest()).decode("ascii")[:17]


def session_mac(secret: bytes, payload: bytes) -> bytes:
    return hmac.new(secret, payload, hashlib.sha256).digest()


def mint_session(role_arn: str, session_name: str, verified: Verified, duration: int, now: int, secret: bytes) -> Session:
    match = ROLE_ARN.match(role_arn)
    if not match:
        raise StsError("ValidationError", f"{role_arn} is not a role ARN (arn:aws:iam::ACCOUNT:role/NAME)")
    arn = f"arn:{match.group('partition')}:sts::{match.group('account')}:assumed-role/{match.group('name')}/{session_name}"
    access_key_id = "ASIA" + base64.b32encode(secrets.token_bytes(10)).decode("ascii")
    expiration = now + duration
    payload = compact_json({"arn": arn, "sub": verified.claims["sub"], "akid": access_key_id, "exp": expiration})
    token = b64url_encode(payload) + "." + b64url_encode(session_mac(secret, payload))
    secret_key = base64.b64encode(secrets.token_bytes(30)).decode("ascii")
    return Session(access_key_id, secret_key, token, expiration, arn, f"{role_unique_id(role_arn)}:{session_name}", verified.claims["sub"], verified.audience, verified.claims["iss"])


def session_identity(token: str, secret: bytes, now: int) -> Dict[str, Any]:
    body, _, mac = token.partition(".")
    try:
        payload, given = b64url_decode(body), b64url_decode(mac)
    except ValueError:
        raise StsError("InvalidClientTokenId", "The security token included in the request is invalid.")
    if not hmac.compare_digest(given, session_mac(secret, payload)):
        raise StsError("InvalidClientTokenId", "The security token included in the request is invalid.")
    data = json.loads(payload)
    if now >= data["exp"]:
        raise StsError("ExpiredToken", "The security token included in the request is expired")
    return data


@dataclass
class Role:
    arn: str
    trust_policy: Dict[str, Any]
    max_session: int = DEFAULT_SESSION


@dataclass
class Outcome:
    role_arn: str
    session_name: str
    error: Optional[StsError] = None
    verified: Optional[Verified] = None
    trust: Optional[TrustDecision] = None
    session: Optional[Session] = None

    @property
    def allowed(self) -> bool:
        return self.session is not None

    @property
    def code(self) -> str:
        return "allowed" if self.session else (self.error.code if self.error else "unknown")

    def to_json(self) -> Dict[str, Any]:
        doc: Dict[str, Any] = self.session.to_json() if self.session else {"Error": self.error.to_json() if self.error else {}}
        doc["minioidc"] = {
            "role": self.role_arn,
            "decision": self.trust.decision if self.trust else None,
            "claims": self.verified.claims if self.verified else None,
            "kid": self.verified.kid if self.verified else None,
            "context": self.trust.context if self.trust else None,
            "statements": [s.to_json() for s in self.trust.statements] if self.trust else [],
        }
        return doc


class Sts:
    def __init__(self, verifier: Verifier, roles: Sequence[Role], secret: Optional[bytes] = None, clock: Clock = time.time) -> None:
        self.verifier = verifier
        self.roles = {role.arn: role for role in roles}
        self.secret = secret or secrets.token_bytes(32)
        self.clock = clock

    def assume(self, token: str, role_arn: str, session_name: str = DEFAULT_SESSION_NAME, duration: int = DEFAULT_SESSION) -> Outcome:
        outcome = Outcome(role_arn, session_name)
        try:
            if not SESSION_NAME.match(session_name):
                raise StsError("ValidationError", f"RoleSessionName {session_name!r} must be 2 to 64 characters of letters, digits and =,.@_-")
            if not MIN_SESSION <= duration <= MAX_SESSION:
                raise StsError("ValidationError", f"DurationSeconds {duration} must be between {MIN_SESSION} and {MAX_SESSION}")
            role = self.roles.get(role_arn)
            if role is None:
                raise StsError("AccessDenied", NOT_AUTHORIZED)
            if duration > role.max_session:
                raise StsError("ValidationError", TOO_LONG)
            outcome.verified = self.verifier.verify(token)
            outcome.trust = evaluate_trust(role.trust_policy, outcome.verified.claims, outcome.verified.provider)
            if not outcome.trust.allowed:
                raise StsError("AccessDenied", NOT_AUTHORIZED)
            outcome.session = mint_session(role_arn, session_name, outcome.verified, duration, int(self.clock()), self.secret)
        except StsError as exc:
            outcome.error = exc
        return outcome


@dataclass
class Finding:
    severity: str
    code: str
    where: str
    message: str
    fix: str

    def to_json(self) -> Dict[str, str]:
        return {"severity": self.severity, "code": self.code, "where": self.where, "message": self.message, "fix": self.fix}


def flat_conditions(block: Any) -> List[Tuple[str, str, List[str]]]:
    if not isinstance(block, dict):
        return []
    return [(operator, key, [condition_text(v) for v in as_list(wanted)]) for operator, keys in block.items() if isinstance(keys, dict) for key, wanted in keys.items()]


def is_positive(operator: str) -> bool:
    qualifier, base, if_exists = parse_operator(operator)
    return base not in NEGATED and base != "Null" and not if_exists and qualifier != "ForAllValues"


def wildcard_findings(host: str, operator: str, value: str, where: str) -> List[Finding]:
    base = parse_operator(operator)[1]
    has_wildcard = "*" in value or "?" in value
    if value and set(value) <= {"*", "?"} and base == "StringLike":
        return [Finding("CRITICAL", "wildcard-subject", where, f"{host}:sub matches anything ({value!r}): every repository on the issuer can assume the role; IAM refuses to save this for shared providers", f"pin {host}:sub with StringEquals to repo:OWNER/REPO:environment:NAME")]
    if base == "StringEquals" and has_wildcard:
        return [Finding("LOW", "literal-wildcard", where, f"StringEquals compares {value!r} literally, so this condition never matches a real token", "use StringEquals with the exact subject (or StringLike if a pattern is really meant)")]
    if base != "StringLike" or not has_wildcard or not value.startswith("repo:"):
        if value.endswith(":pull_request") and base in ("StringEquals", "StringLike"):
            return [Finding("MEDIUM", "pull-request-subject", where, f"{value!r} gives the role to pull_request runs, which execute code that has not been reviewed or merged", "keep this only on a read-only role (plan, test); deploy roles pin an environment")]
        return []
    rest = value[len("repo:"):]
    owner, slash, after = rest.partition("/")
    if not slash or any(c in owner for c in "*?"):
        example = "repo:" + re.sub(r"[*?].*", "", owner) + "-lookalike/any"
        return [Finding("HIGH", "wildcard-owner", where, f"{value!r} has a wildcard in the owner name: anyone who registers a matching organization ({example}) can assume the role", "end the owner name with a slash: repo:OWNER/REPO:...")]
    name, colon, context = after.partition(":")
    if any(c in name for c in "*?"):
        return [Finding("MEDIUM", "wildcard-repository", where, f"{value!r} trusts every repository of {owner}, including new ones any member can create", "list the repositories that deploy, one subject each")]
    if colon and any(c in context for c in "*?"):
        return [Finding("MEDIUM", "wildcard-context", where, f"{value!r} trusts every branch, tag, pull_request run and environment of {owner}/{name}", "pin :environment:NAME (with protection rules) or :ref:refs/heads/main")]
    return []


def audit_trust_policy(policy: Any, provider: Optional[Provider] = None) -> List[Finding]:
    findings: List[Finding] = []
    for index, statement in enumerate(statements_of(policy)):
        sid = statement.get("Sid")
        where = f"statement {index}" + (f' "{sid}"' if sid else "")
        if statement.get("Effect") != "Allow" or not action_matches(statement.get("Action"), ASSUME_ACTION):
            continue
        names = federated_names(statement.get("Principal"))
        if "*" in names:
            findings.append(Finding("CRITICAL", "public-principal", where, 'Principal "*" lets a token from any issuer try this role', "name the provider: {\"Federated\": \"arn:aws:iam::ACCOUNT:oidc-provider/HOST\"}"))
        hosts = [name.split("oidc-provider/", 1)[1] if "oidc-provider/" in name else name for name in names if name != "*"]
        if not hosts and provider:
            hosts = [provider.url]
        conditions = flat_conditions(statement.get("Condition"))
        for host in hosts:
            findings.extend(audit_host(host, conditions, where, provider))
    if provider is not None:
        findings.extend(audit_provider(provider))
    return sorted(findings, key=lambda f: (SEVERITIES.index(f.severity), f.where, f.code))


def audit_host(host: str, conditions: List[Tuple[str, str, List[str]]], where: str, provider: Optional[Provider]) -> List[Finding]:
    findings: List[Finding] = []
    prefix = host.lower() + ":"
    controls = IDENTITY_PROVIDER_CONTROLS.get(host.lower(), ("sub",))
    mapped = condition_claims(host)
    own = [(op, key, values) for op, key, values in conditions if key.lower().startswith(prefix)]
    for operator, key, _ in conditions:
        if ":" in key and not key.lower().startswith((prefix, "aws:", "sts:")):
            findings.append(Finding("MEDIUM", "foreign-key", where, f"{key} belongs to another provider; for tokens from {host} it is always missing, so {operator} {'passes' if not is_positive(operator) else 'fails'}", f"write the key as {host}:CLAIM"))
    positive_subject = [(op, values) for op, key, values in own if key[len(prefix):].lower() in controls and is_positive(op)]
    negative_subject = [op for op, key, _ in own if key[len(prefix):].lower() in controls and not is_positive(op)]
    if not positive_subject:
        shared = host.lower() in IDENTITY_PROVIDER_CONTROLS
        message = f"no positive condition on {host}:{' or :'.join(controls)}: every workflow of every repository that can get a token from {host} can assume this role"
        if shared:
            message += "; IAM now refuses to create or update such a policy (MalformedPolicyDocument), but roles saved before that check still accept it"
        findings.append(Finding("CRITICAL", "missing-subject", where, message, f"add StringEquals {host}:{controls[0]} with the exact subject, e.g. repo:OWNER/REPO:environment:prod"))
        if negative_subject:
            findings.append(Finding("HIGH", "negated-subject", where, f"{', '.join(negative_subject)} on {host}:sub excludes the listed subjects and allows every other one", "replace the exclusion with an allow-list on StringEquals"))
    for operator, values in positive_subject:
        for value in values:
            findings.extend(wildcard_findings(host, operator, value, where))
    if not any(key[len(prefix):].lower() == "aud" and is_positive(op) for op, key, _ in own):
        lone = provider is not None and provider.client_ids == [AWS_AUDIENCE]
        findings.append(Finding("LOW" if lone else "MEDIUM", "missing-audience", where, f"no condition on {host}:aud: the role relies on the provider's ClientIDList alone, so a token minted for any audience listed there is accepted", f"add StringEquals {host}:aud {AWS_AUDIENCE}"))
    for operator, key, _ in own:
        claim = key[len(prefix):].lower()
        if claim in mapped:
            continue
        if is_positive(operator):
            findings.append(Finding("LOW", "unmapped-key-fails", where, f"AWS does not map the claim {claim} of {host} to a condition key; {key} is always missing, so {operator} never matches and the role cannot be assumed", f"move the requirement into the sub claim (GitHub's include_claim_keys) or use a mapped key ({', '.join(mapped)})"))
        else:
            findings.append(Finding("HIGH", "unmapped-key-passes", where, f"AWS does not map the claim {claim} of {host} to a condition key; {key} is always missing, so {operator} always passes and restricts nothing", f"move the requirement into the sub claim or use a mapped key ({', '.join(mapped)})"))
    return findings


def audit_provider(provider: Provider) -> List[Finding]:
    where = f"provider {provider.url}"
    if not provider.client_ids:
        return [Finding("HIGH", "provider-any-audience", where, "the provider has no client IDs, so this verifier accepts a token minted for any audience, including one a workflow sent to another service", f"set ClientIDList to [{AWS_AUDIENCE}]")]
    if len(provider.client_ids) > 1:
        return [Finding("LOW", "provider-many-audiences", where, f"the provider accepts {len(provider.client_ids)} audiences ({', '.join(provider.client_ids)}); a role without an :aud condition accepts all of them", "pin :aud in every role that trusts this provider")]
    return []


def read_json(path: str) -> Any:
    try:
        if path == "-":
            return json.load(sys.stdin)
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)
    except OSError as exc:
        raise InputError(f"cannot read {path}: {exc.strerror}")
    except ValueError as exc:
        raise InputError(f"{path} is not JSON: {exc}")


def read_token(value: str) -> str:
    if value == "-":
        return sys.stdin.read().strip()
    if os.path.isfile(value):
        with open(value, encoding="utf-8") as handle:
            return handle.read().strip()
    return value.strip()


def load_private_key(path: str) -> RsaKey:
    key = key_from_jwk(read_json(path))
    if not key.private:
        raise InputError(f"{path} holds a public key; the issuer needs the private key file written by keygen")
    return key


def run_cases(doc: Any, base_dir: str, bits: int = 1024, now: Optional[int] = None) -> List[Dict[str, Any]]:
    if not isinstance(doc, dict) or not isinstance(doc.get("cases"), list):
        raise InputError("a cases file has a cases list")
    at = int(time.time()) if now is None else now
    account = doc.get("account", DEFAULT_ACCOUNT)
    jobs = load_jobs(read_json(os.path.join(base_dir, doc.get("jobs", "jobs.json"))))
    issuers: Dict[str, Issuer] = {}
    rows = []
    for case in doc["cases"]:
        url = case.get("issuer", GITHUB_ISSUER)
        if url not in issuers:
            issuers[url] = Issuer(url, [generate_key(bits)], clock=lambda: at)
        issuer = issuers[url]
        minted = at + int(case.get("minted_offset", 0))
        if "claims" in case:
            claims = dict(case["claims"])
            claims.update({"iss": url, "iat": minted, "nbf": minted, "exp": minted + TOKEN_TTL})
            token = encode_jwt(claims, issuer.signing_key)
        else:
            if case["job"] not in jobs:
                raise InputError(f"case {case.get('name')}: unknown job {case['job']}")
            token = issuer.mint(jobs[case["job"]], case.get("audience", AWS_AUDIENCE), minted)
        providers = [load_provider(read_json(os.path.join(base_dir, p)), account) for p in case.get("providers", doc.get("providers", ["providers/github.json"]))]
        verifier = Verifier(providers, fetch=issuer_fetch(*issuers.values()), clock=lambda: at)
        role_arn = f"arn:aws:iam::{account}:role/cc-deploy"
        sts = Sts(verifier, [Role(role_arn, read_json(os.path.join(base_dir, case["policy"])))], clock=lambda: at)
        outcome = sts.assume(token, role_arn)
        detail = outcome.session.arn if outcome.session else (outcome.error.message if outcome.error else "")
        rows.append({"name": case.get("name", ""), "policy": case["policy"], "expect": case["expect"], "got": outcome.code, "ok": outcome.code == case["expect"], "detail": detail, "source": case.get("source", "")})
    return rows


def render_outcome(outcome: Outcome) -> str:
    lines = [f"AssumeRoleWithWebIdentity {outcome.role_arn} as {outcome.session_name}"]
    if outcome.verified:
        c = outcome.verified.claims
        lines.append(f"  token      iss {c.get('iss')}  kid {outcome.verified.kid}")
        lines.append(f"             sub {c.get('sub')}  aud {outcome.verified.audience}  exp {iso(c['exp'])}")
        lines.append("  verified   signature RS256, issuer has an IAM OIDC provider, audience in ClientIDList, exp/nbf/iat")
    if outcome.trust:
        for statement in outcome.trust.statements:
            state = "applies" if statement.applies else "does not apply"
            lines.append(f"  {statement.label} ({statement.effect})  principal {'ok' if statement.principal else 'no'}  action {'ok' if statement.action else 'no'}  {state}")
            lines.extend(f"    {check.describe()}" for check in statement.checks)
        lines.append(f"  trust      {outcome.trust.decision}")
    if outcome.session:
        s = outcome.session
        lines.append(f"  session    {s.arn}  AccessKeyId {s.access_key_id}  expires {iso(s.expiration)}")
    elif outcome.error:
        lines.append("  " + outcome.error.cli_text())
    return "\n".join(lines)


def render_findings(findings: List[Finding]) -> str:
    if not findings:
        return "no findings"
    lines = []
    for f in findings:
        lines.append(f"{f.severity:<8} {f.code:<22} {f.where}")
        lines.append(f"         {f.message}")
        lines.append(f"         fix: {f.fix}")
    return "\n".join(lines)


def emit(value: Any) -> None:
    print(json.dumps(value, indent=2))


def cmd_keygen(args: argparse.Namespace) -> int:
    key = generate_key(args.bits, args.kid or "")
    with open(args.out, "w", encoding="utf-8") as handle:
        json.dump(key.private_jwk(), handle, indent=2)
    os.chmod(args.out, 0o600)
    print(f"wrote a {key.bits}-bit RSA private key to {args.out} (kid {key.kid}); keep it out of git")
    return 0


def cmd_jwks(args: argparse.Namespace) -> int:
    emit({"keys": [load_private_key(path).public_jwk() for path in args.key]})
    return 0


def cmd_jwk_to_pem(args: argparse.Namespace) -> int:
    try:
        keys = parse_jwks(fetch_json(load_url, args.jwks))
    except StsError as exc:
        raise CommandError(exc.message)
    if args.kid and args.kid not in keys:
        raise CommandError(f"no key {args.kid} in {args.jwks} (it has {', '.join(sorted(keys))})")
    key = keys[args.kid] if args.kid else next(iter(keys.values()))
    sys.stdout.write(to_pem("PUBLIC KEY", spki_der(key)))
    return 0


def cmd_pem_to_jwk(args: argparse.Namespace) -> int:
    with open(args.pem, encoding="utf-8") as handle:
        key = public_key_from_pem(handle.read(), args.kid or "")
    emit({"keys": [key.public_jwk()]})
    return 0


def cmd_mint(args: argparse.Namespace) -> int:
    jobs = load_jobs(read_json(args.jobs))
    if args.job not in jobs:
        raise InputError(f"no job {args.job} in {args.jobs} (jobs: {', '.join(sorted(jobs))})")
    issuer = Issuer(args.issuer, [load_private_key(args.key)], args.ttl)
    print(issuer.mint(jobs[args.job], args.audience, args.now))
    return 0


def cmd_serve(args: argparse.Namespace) -> int:
    issuer = Issuer(args.issuer or "http://pending", [load_private_key(path) for path in args.key], args.ttl)

    def log(line: str) -> None:
        print(line, flush=True)

    server = make_server(issuer, args.host, args.port, log)
    issuer.issuer = (args.issuer or server.base_url).rstrip("/")
    jobs = load_jobs(read_json(args.jobs)) if args.jobs else {}
    if args.env_dir:
        os.makedirs(args.env_dir, exist_ok=True)
    for name, job in jobs.items():
        bearer = issuer.register(name, job)
        if args.env_dir:
            path = os.path.join(args.env_dir, f"{name}.env")
            with open(path, "w", encoding="utf-8") as handle:
                handle.write(f"export ACTIONS_ID_TOKEN_REQUEST_URL='{server.token_url}'\nexport ACTIONS_ID_TOKEN_REQUEST_TOKEN='{bearer}'\n")
            os.chmod(path, 0o600)
    if args.port_file:
        temporary = args.port_file + ".tmp"
        with open(temporary, "w", encoding="utf-8") as handle:
            handle.write(str(server.server_address[1]))
        os.replace(temporary, args.port_file)
    log(f"minioidc issuer {issuer.issuer} serving on {server.base_url} (discovery, jwks, idtoken; {len(jobs)} jobs)")
    try:
        server.serve_forever(poll_interval=0.2)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


def cmd_request(args: argparse.Namespace) -> int:
    url = args.url or os.environ.get("ACTIONS_ID_TOKEN_REQUEST_URL", "")
    bearer = args.bearer or os.environ.get("ACTIONS_ID_TOKEN_REQUEST_TOKEN", "")
    if not url or not bearer:
        raise InputError("ACTIONS_ID_TOKEN_REQUEST_URL and ACTIONS_ID_TOKEN_REQUEST_TOKEN are not set: the runner sets them only when the job has permissions: id-token: write")
    print(request_id_token(url, bearer, args.audience))
    return 0


def cmd_decode(args: argparse.Namespace) -> int:
    jwt = decode_jwt(read_token(args.token))
    if args.json:
        emit({"verified": False, "header": jwt.header, "claims": jwt.claims})
        return 0
    print("UNVERIFIED: decoding proves nothing about who signed this token")
    print(json.dumps(jwt.header, indent=2))
    print(json.dumps(jwt.claims, indent=2))
    return 0


def providers_from(args: argparse.Namespace) -> List[Provider]:
    return [load_provider(read_json(path), args.account) for path in args.provider]


def verifier_from(args: argparse.Namespace, clock: Clock) -> Verifier:
    return Verifier(providers_from(args), leeway=args.leeway, clock=clock, discovery_url=args.discovery_url or "", jwks_url=args.jwks or "")


def clock_from(args: argparse.Namespace) -> Clock:
    return (lambda: float(args.now)) if args.now is not None else time.time


def cmd_verify(args: argparse.Namespace) -> int:
    try:
        verified = verifier_from(args, clock_from(args)).verify(read_token(args.token))
    except StsError as exc:
        if args.json:
            emit({"Error": exc.to_json()})
        else:
            print(f"{exc.code}: {exc.message}")
        return 1
    if args.json:
        emit({"verified": True, "kid": verified.kid, "provider": verified.provider.arn, "header": verified.header, "claims": verified.claims})
    else:
        print(f"verified {verified.claims['sub']} (aud {verified.audience}, kid {verified.kid}, provider {verified.provider.arn})")
    return 0


def cmd_assume(args: argparse.Namespace) -> int:
    clock = clock_from(args)
    role = Role(args.role_arn, read_json(args.trust_policy), args.max_session_duration)
    statements_of(role.trust_policy)
    sts = Sts(verifier_from(args, clock), [role], clock=clock)
    outcome = sts.assume(read_token(args.token), args.role_arn, args.role_session_name, args.duration_seconds)
    if args.json:
        emit(outcome.to_json())
    else:
        print(render_outcome(outcome))
    return 0 if outcome.allowed else 1


def cmd_audit(args: argparse.Namespace) -> int:
    provider = load_provider(read_json(args.provider), args.account) if args.provider else None
    findings = audit_trust_policy(read_json(args.trust_policy), provider)
    if args.json:
        emit([f.to_json() for f in findings])
    else:
        print(render_findings(findings))
    return 1 if any(f.severity in ("CRITICAL", "HIGH") for f in findings) else 0


def cmd_cases(args: argparse.Namespace) -> int:
    rows = run_cases(read_json(args.file), os.path.dirname(os.path.abspath(args.file)), args.bits, args.now)
    if args.json:
        emit(rows)
    else:
        width = max([len(row["name"]) for row in rows] + [10])
        for row in rows:
            print(f"{'ok  ' if row['ok'] else 'FAIL'} {row['name']:<{width}}  expect {row['expect']:<22} got {row['got']}")
        print(f"{sum(r['ok'] for r in rows)}/{len(rows)} cases as documented")
    return 0 if all(r["ok"] for r in rows) else 1


def add_verifier_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--provider", action="append", required=True, help="output of aws iam get-open-id-connect-provider (repeatable)")
    parser.add_argument("--account", default=DEFAULT_ACCOUNT, help="account for provider ARNs when the file has no Arn")
    parser.add_argument("--discovery-url", help="fetch the discovery document here instead of ISSUER/.well-known/openid-configuration")
    parser.add_argument("--jwks", help="JWKS file or URL to use instead of discovery")
    parser.add_argument("--leeway", type=int, default=LEEWAY, help="clock skew allowed on exp, nbf and iat, in seconds")
    parser.add_argument("--now", type=int, help="evaluate at this Unix time")
    parser.add_argument("--json", action="store_true")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="minioidc", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("keygen", help="generate an RSA key for the issuer (private JWK; never commit it)")
    p.add_argument("--bits", type=int, default=2048)
    p.add_argument("--kid")
    p.add_argument("--out", required=True)
    p.set_defaults(run=cmd_keygen)

    p = sub.add_parser("jwks", help="print the public JWKS for private key files")
    p.add_argument("--key", action="append", required=True)
    p.set_defaults(run=cmd_jwks)

    p = sub.add_parser("jwk-to-pem", help="export a JWKS key as a PEM SubjectPublicKeyInfo for openssl")
    p.add_argument("--jwks", required=True, help="JWKS file or URL")
    p.add_argument("--kid")
    p.set_defaults(run=cmd_jwk_to_pem)

    p = sub.add_parser("pem-to-jwk", help="turn an openssl public key into a JWKS")
    p.add_argument("pem")
    p.add_argument("--kid")
    p.set_defaults(run=cmd_pem_to_jwk)

    p = sub.add_parser("mint", help="sign a token for a job offline")
    p.add_argument("--key", required=True)
    p.add_argument("--jobs", required=True)
    p.add_argument("--job", required=True)
    p.add_argument("--audience", default="")
    p.add_argument("--issuer", default=GITHUB_ISSUER)
    p.add_argument("--ttl", type=int, default=TOKEN_TTL)
    p.add_argument("--now", type=int)
    p.set_defaults(run=cmd_mint)

    p = sub.add_parser("serve", help="run the issuer: discovery, JWKS and the runner's idtoken endpoint")
    p.add_argument("--key", action="append", required=True, help="private key files; the first signs, all are published")
    p.add_argument("--issuer", help="iss claim and discovery issuer (default: the server's own URL)")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=0)
    p.add_argument("--port-file")
    p.add_argument("--jobs", help="job contexts to register, each with its own bearer token")
    p.add_argument("--env-dir", help="write NAME.env with ACTIONS_ID_TOKEN_REQUEST_URL and _TOKEN per job")
    p.add_argument("--ttl", type=int, default=TOKEN_TTL)
    p.set_defaults(run=cmd_serve)

    p = sub.add_parser("request", help="fetch a token the way core.getIDToken() does")
    p.add_argument("--audience", default="")
    p.add_argument("--url")
    p.add_argument("--bearer")
    p.set_defaults(run=cmd_request)

    p = sub.add_parser("decode", help="print header and claims without verifying anything")
    p.add_argument("token")
    p.add_argument("--json", action="store_true")
    p.set_defaults(run=cmd_decode)

    p = sub.add_parser("verify", help="verify a token as a cloud would: keys, signature, issuer, audience, time")
    p.add_argument("token")
    add_verifier_options(p)
    p.set_defaults(run=cmd_verify)

    p = sub.add_parser("assume", help="AssumeRoleWithWebIdentity: verify, evaluate the trust policy, mint a session")
    p.add_argument("--token", required=True, help="the JWT, a file holding it, or - for stdin")
    p.add_argument("--role-arn", required=True)
    p.add_argument("--trust-policy", required=True)
    p.add_argument("--role-session-name", default=DEFAULT_SESSION_NAME)
    p.add_argument("--duration-seconds", type=int, default=DEFAULT_SESSION)
    p.add_argument("--max-session-duration", type=int, default=DEFAULT_SESSION)
    add_verifier_options(p)
    p.set_defaults(run=cmd_assume)

    p = sub.add_parser("audit", help="find the trust-policy mistakes that let other repositories in")
    p.add_argument("--trust-policy", required=True)
    p.add_argument("--provider")
    p.add_argument("--account", default=DEFAULT_ACCOUNT)
    p.add_argument("--json", action="store_true")
    p.set_defaults(run=cmd_audit)

    p = sub.add_parser("cases", help="run documented trust decisions end to end")
    p.add_argument("file")
    p.add_argument("--bits", type=int, default=2048)
    p.add_argument("--now", type=int)
    p.add_argument("--json", action="store_true")
    p.set_defaults(run=cmd_cases)
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.run(args)
    except (InputError, PolicyError) as exc:
        print(f"minioidc: {exc}", file=sys.stderr)
        return 2
    except CommandError as exc:
        print(f"minioidc: {exc}", file=sys.stderr)
        return 1
    except StsError as exc:
        print(f"minioidc: {exc.code}: {exc.message}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
