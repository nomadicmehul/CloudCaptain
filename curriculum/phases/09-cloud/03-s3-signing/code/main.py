"""minis3: AWS Signature Version 4 from the specification, an S3 client that signs and presigns, and a small S3-compatible server that verifies every signature."""
from __future__ import annotations

import argparse
import base64
import binascii
import hashlib
import hmac
import ipaddress
import json
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
import zlib
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.utils import formatdate
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple
from xml.etree import ElementTree
from xml.sax.saxutils import escape

ALGORITHM = "AWS4-HMAC-SHA256"
TERMINATOR = "aws4_request"
EMPTY_SHA256 = hashlib.sha256(b"").hexdigest()
UNSIGNED_PAYLOAD = "UNSIGNED-PAYLOAD"
STREAMING_UNSIGNED_TRAILER = "STREAMING-UNSIGNED-PAYLOAD-TRAILER"
MAX_SKEW_SECONDS = 900
MAX_EXPIRES_SECONDS = 604800
MIN_PART_SIZE = 5 * 1024 * 1024
MAX_KEYS = 1000
UNRESERVED = frozenset(b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_.~")
HEX64 = re.compile(r"^[0-9a-f]{64}$")
BUCKET_NAME = re.compile(r"^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$")
S3_NS = "http://s3.amazonaws.com/doc/2006-03-01/"
UNSIGNABLE = {"authorization", "user-agent", "expect", "x-amzn-trace-id", "transfer-encoding", "connection"}
QUOTE_ENTITY = {'"': "&quot;"}
POLICY_ACTIONS = {"s3:GetBucketPolicy", "s3:PutBucketPolicy", "s3:DeleteBucketPolicy"}
Pairs = List[Tuple[str, str]]


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def hmac_sha256(key: bytes, text: str) -> bytes:
    return hmac.new(key, text.encode("utf-8"), hashlib.sha256).digest()


def uri_encode(text: str, keep_slash: bool = False) -> str:
    out = []
    for byte in text.encode("utf-8"):
        if byte in UNRESERVED or (keep_slash and byte == 0x2F):
            out.append(chr(byte))
        else:
            out.append(f"%{byte:02X}")
    return "".join(out)


def normalize_path(path: str) -> str:
    segments: List[str] = []
    for segment in path.split("/"):
        if segment in ("", "."):
            continue
        if segment == "..":
            if segments:
                segments.pop()
            continue
        segments.append(segment)
    result = "/" + "/".join(segments)
    if segments and path.endswith("/"):
        result += "/"
    return result


def canonical_uri(path: str, normalize: bool = False) -> str:
    path = path or "/"
    return uri_encode(normalize_path(path) if normalize else path, keep_slash=True)


def canonical_query(params: Sequence[Tuple[str, str]]) -> str:
    encoded = sorted((uri_encode(key), uri_encode(value)) for key, value in params)
    return "&".join(f"{key}={value}" for key, value in encoded)


def collapse(value: str) -> str:
    return re.sub(r"\s+", " ", value.strip())


def canonical_headers(headers: Sequence[Tuple[str, str]], signed: Sequence[str]) -> str:
    values: Dict[str, List[str]] = {name: [] for name in signed}
    for name, value in headers:
        if name.lower() in values:
            values[name.lower()].append(collapse(value))
    return "".join(f"{name}:{','.join(values[name])}\n" for name in sorted(values))


def canonical_request(method: str, path: str, query: Sequence[Tuple[str, str]], headers: Sequence[Tuple[str, str]], signed_headers: Sequence[str], payload_hash: str, normalize: bool = False) -> str:
    signed = sorted({name.lower() for name in signed_headers})
    return "\n".join([method.upper(), canonical_uri(path, normalize), canonical_query(query), canonical_headers(headers, signed), ";".join(signed), payload_hash])


def credential_scope(date: str, region: str, service: str) -> str:
    return f"{date}/{region}/{service}/{TERMINATOR}"


def string_to_sign(amz_date: str, scope: str, creq: str) -> str:
    return "\n".join([ALGORITHM, amz_date, scope, sha256_hex(creq.encode("utf-8"))])


def signing_key(secret: str, date: str, region: str, service: str) -> bytes:
    key = hmac_sha256(("AWS4" + secret).encode("utf-8"), date)
    for part in (region, service, TERMINATOR):
        key = hmac_sha256(key, part)
    return key


def compute_signature(secret: str, scope: str, sts: str) -> str:
    date, region, service, _ = scope.split("/")
    return hmac.new(signing_key(secret, date, region, service), sts.encode("utf-8"), hashlib.sha256).hexdigest()


def amz_date(moment: float) -> str:
    return datetime.fromtimestamp(moment, timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def parse_amz_date(text: str) -> float:
    return datetime.strptime(text, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc).timestamp()


def iso8601(moment: float) -> str:
    return datetime.fromtimestamp(moment, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.") + f"{int(moment * 1000) % 1000:03d}Z"


@dataclass(frozen=True)
class Credentials:
    access_key: str
    secret_key: str
    session_token: str = ""


@dataclass
class Signed:
    method: str
    path: str
    query: Pairs
    headers: Pairs
    canonical_request: str
    string_to_sign: str
    signature: str
    authorization: str = ""

    def target(self) -> str:
        query = "&".join(f"{uri_encode(key)}={uri_encode(value)}" for key, value in self.query)
        return uri_encode(self.path or "/", keep_slash=True) + ("?" + query if query else "")


def sign_with_headers(method: str, path: str, query: Sequence[Tuple[str, str]], headers: Sequence[Tuple[str, str]], payload_hash: str, creds: Credentials, region: str, service: str, when: float, add_content_sha256: bool = True, normalize: bool = False, sign_token: bool = True) -> Signed:
    stamp = amz_date(when)
    final = list(headers) + [("X-Amz-Date", stamp)]
    if add_content_sha256:
        final.append(("X-Amz-Content-Sha256", payload_hash))
    if creds.session_token and sign_token:
        final.append(("X-Amz-Security-Token", creds.session_token))
    signed = sorted({name.lower() for name, _ in final} - UNSIGNABLE)
    scope = credential_scope(stamp[:8], region, service)
    creq = canonical_request(method, path, query, final, signed, payload_hash, normalize)
    sts = string_to_sign(stamp, scope, creq)
    signature = compute_signature(creds.secret_key, scope, sts)
    authorization = f"{ALGORITHM} Credential={creds.access_key}/{scope}, SignedHeaders={';'.join(signed)}, Signature={signature}"
    final.append(("Authorization", authorization))
    if creds.session_token and not sign_token:
        final.append(("X-Amz-Security-Token", creds.session_token))
    return Signed(method.upper(), path, list(query), final, creq, sts, signature, authorization)


def presign(method: str, path: str, query: Sequence[Tuple[str, str]], headers: Sequence[Tuple[str, str]], creds: Credentials, region: str, service: str, when: float, expires: int, payload_hash: str = UNSIGNED_PAYLOAD, normalize: bool = False, sign_token: bool = True) -> Signed:
    if not 1 <= expires <= MAX_EXPIRES_SECONDS:
        raise ValueError(f"expires must be between 1 and {MAX_EXPIRES_SECONDS} seconds (7 days), got {expires}")
    stamp = amz_date(when)
    scope = credential_scope(stamp[:8], region, service)
    signed = sorted({name.lower() for name, _ in headers} - UNSIGNABLE)
    params = list(query) + [
        ("X-Amz-Algorithm", ALGORITHM),
        ("X-Amz-Credential", f"{creds.access_key}/{scope}"),
        ("X-Amz-Date", stamp),
        ("X-Amz-Expires", str(expires)),
        ("X-Amz-SignedHeaders", ";".join(signed)),
    ]
    if creds.session_token and sign_token:
        params.append(("X-Amz-Security-Token", creds.session_token))
    creq = canonical_request(method, path, params, headers, signed, payload_hash, normalize)
    sts = string_to_sign(stamp, scope, creq)
    signature = compute_signature(creds.secret_key, scope, sts)
    params.append(("X-Amz-Signature", signature))
    if creds.session_token and not sign_token:
        params.append(("X-Amz-Security-Token", creds.session_token))
    return Signed(method.upper(), path, params, list(headers), creq, sts, signature)


def parse_query(raw: str) -> Pairs:
    pairs: Pairs = []
    for part in raw.split("&"):
        if part:
            key, _, value = part.partition("=")
            pairs.append((urllib.parse.unquote(key), urllib.parse.unquote(value)))
    return pairs


def parse_authorization(header: str) -> Dict[str, str]:
    fields: Dict[str, str] = {}
    for part in header[len(ALGORITHM):].split(","):
        name, sep, value = part.strip().partition("=")
        if sep:
            fields[name] = value
    missing = [name for name in ("Credential", "SignedHeaders", "Signature") if not fields.get(name)]
    if missing:
        raise S3Error(400, "AuthorizationHeaderMalformed", f"The authorization header is malformed; missing {', '.join(missing)}.")
    return fields


class S3Error(Exception):
    def __init__(self, status: int, code: str, message: str, **details: Any) -> None:
        super().__init__(f"{code}: {message}")
        self.status = status
        self.code = code
        self.message = message
        self.details = details
        self.headers: Dict[str, str] = {}

    def to_xml(self, resource: str, request_id: str) -> bytes:
        extra = "".join(f"<{name}>{escape(str(value))}</{name}>" for name, value in self.details.items())
        body = f"<Error><Code>{self.code}</Code><Message>{escape(self.message)}</Message>{extra}<Resource>{escape(resource)}</Resource><RequestId>{request_id}</RequestId></Error>"
        return ('<?xml version="1.0" encoding="UTF-8"?>\n' + body).encode("utf-8")


@dataclass(frozen=True)
class Key:
    access_key: str
    secret_key: str
    arn: str

    @property
    def account(self) -> str:
        return self.arn.split(":")[4]


@dataclass
class Identity:
    arn: str = ""
    account: str = ""
    access_key: str = ""
    auth_type: str = ""
    signature_age_ms: Optional[int] = None

    @property
    def anonymous(self) -> bool:
        return not self.arn


@dataclass
class Incoming:
    method: str
    path: str
    query: Pairs
    headers: Pairs
    body: bytes = b""
    client_ip: str = "127.0.0.1"
    secure: bool = False

    def header(self, name: str) -> Optional[str]:
        values = [value for key, value in self.headers if key.lower() == name.lower()]
        return ",".join(values) if values else None

    def param(self, name: str) -> Optional[str]:
        return next((value for key, value in self.query if key == name), None)

    def has(self, name: str) -> bool:
        return any(key == name for key, _ in self.query)


def parse_key_spec(spec: str) -> Key:
    parts = spec.split(":", 2)
    if len(parts) < 2 or not parts[0] or not parts[1]:
        raise ValueError(f"--key must be ACCESS_KEY:SECRET_KEY[:PRINCIPAL_ARN], got {spec!r}")
    arn = parts[2] if len(parts) == 3 else "arn:aws:iam::000000000000:root"
    if not re.match(r"^arn:aws:(iam|sts)::\d{12}:", arn):
        raise ValueError(f"principal must be an IAM ARN such as arn:aws:iam::111122223333:user/alice, got {arn!r}")
    return Key(parts[0], parts[1], arn)


def check_scope(credential: str, stamp: str, keys: Dict[str, Key], region: str, code: str) -> Tuple[Key, str]:
    parts = credential.split("/")
    if len(parts) != 5:
        raise S3Error(400, code, 'the Credential is mal-formed; expecting "<YOUR-AKID>/YYYYMMDD/REGION/SERVICE/aws4_request".')
    access_key, date, cred_region, service, terminator = parts
    key = keys.get(access_key)
    if key is None:
        raise S3Error(403, "InvalidAccessKeyId", "The AWS Access Key Id you provided does not exist in our records.", AWSAccessKeyId=access_key)
    if date != stamp[:8]:
        raise S3Error(400, code, f'Invalid credential date "{date}". This date is not the same as X-Amz-Date: "{stamp[:8]}".')
    if cred_region != region:
        raise S3Error(400, code, f"The authorization header is malformed; the region '{cred_region}' is wrong; expecting '{region}'", Region=region)
    if service != "s3":
        raise S3Error(400, code, f'The authorization header is malformed; incorrect service "{service}". This endpoint belongs to "s3".')
    if terminator != TERMINATOR:
        raise S3Error(400, code, f'The authorization header is malformed; incorrect terminal "{terminator}". This endpoint uses "{TERMINATOR}".')
    return key, "/".join(parts[1:])


def require_signed(req: Incoming, signed: Sequence[str]) -> None:
    if "host" not in signed:
        raise S3Error(403, "AccessDenied", "The host header must be signed")
    present = {name.lower() for name, _ in req.headers if name.lower().startswith("x-amz-")}
    unsigned = sorted(present - set(signed))
    if unsigned:
        raise S3Error(403, "AccessDenied", "There were headers present in the request which were not signed", HeadersNotSigned=", ".join(unsigned))


def signature_mismatch(access_key: str, provided: str, creq: str, sts: str) -> S3Error:
    return S3Error(403, "SignatureDoesNotMatch", "The request signature we calculated does not match the signature you provided. Check your key and signing method.", AWSAccessKeyId=access_key, StringToSign=sts, SignatureProvided=provided, CanonicalRequest=creq)


def stamp_time(stamp: Optional[str]) -> float:
    try:
        return parse_amz_date(stamp or "")
    except ValueError:
        raise S3Error(403, "AccessDenied", "AWS authentication requires a valid Date or x-amz-date header")


def authenticate(req: Incoming, keys: Dict[str, Key], region: str, now: float) -> Identity:
    authorization = req.header("authorization")
    query_auth = req.has("X-Amz-Algorithm") or req.has("X-Amz-Signature") or req.has("Signature")
    if authorization and query_auth:
        raise S3Error(400, "InvalidArgument", "Only one auth mechanism allowed; only the X-Amz-Algorithm query parameter, Signature query string parameter or the Authorization header should be specified")
    if authorization:
        if not authorization.startswith(ALGORITHM + " "):
            raise S3Error(400, "InvalidRequest", "The authorization mechanism you have provided is not supported. Please use AWS4-HMAC-SHA256.")
        return verify_header_auth(req, parse_authorization(authorization), keys, region, now)
    if query_auth:
        if req.param("X-Amz-Algorithm") != ALGORITHM:
            raise S3Error(400, "InvalidRequest", "The authorization mechanism you have provided is not supported. Please use AWS4-HMAC-SHA256.")
        return verify_query_auth(req, keys, region, now)
    return Identity()


def verify_header_auth(req: Incoming, fields: Dict[str, str], keys: Dict[str, Key], region: str, now: float) -> Identity:
    stamp = req.header("x-amz-date") or ""
    when = stamp_time(stamp)
    key, scope = check_scope(fields["Credential"], stamp, keys, region, "AuthorizationHeaderMalformed")
    if abs(now - when) > MAX_SKEW_SECONDS:
        raise S3Error(403, "RequestTimeTooSkewed", "The difference between the request time and the current time is too large.", RequestTime=stamp, ServerTime=iso8601(now), MaxAllowedSkewMilliseconds=MAX_SKEW_SECONDS * 1000)
    signed = fields["SignedHeaders"].split(";")
    require_signed(req, signed)
    payload = req.header("x-amz-content-sha256")
    if payload is None:
        raise S3Error(400, "InvalidRequest", "Missing required header for this request: x-amz-content-sha256")
    if not (HEX64.match(payload) or payload == UNSIGNED_PAYLOAD or payload.startswith("STREAMING-")):
        raise S3Error(400, "InvalidArgument", "x-amz-content-sha256 must be UNSIGNED-PAYLOAD, STREAMING-UNSIGNED-PAYLOAD-TRAILER, or a valid sha256 value.")
    creq = canonical_request(req.method, req.path, req.query, req.headers, signed, payload)
    sts = string_to_sign(stamp, scope, creq)
    expected = compute_signature(key.secret_key, scope, sts)
    if not hmac.compare_digest(expected, fields["Signature"]):
        raise signature_mismatch(key.access_key, fields["Signature"], creq, sts)
    return Identity(key.arn, key.account, key.access_key, "REST-HEADER", int((now - when) * 1000))


def verify_query_auth(req: Incoming, keys: Dict[str, Key], region: str, now: float) -> Identity:
    wanted = ["X-Amz-Credential", "X-Amz-Date", "X-Amz-Expires", "X-Amz-SignedHeaders", "X-Amz-Signature"]
    if any(req.param(name) is None for name in wanted):
        raise S3Error(400, "AuthorizationQueryParametersError", "Query-string authentication version 4 requires the X-Amz-Algorithm, X-Amz-Credential, X-Amz-Signature, X-Amz-Date, X-Amz-SignedHeaders, and X-Amz-Expires parameters.")
    stamp = req.param("X-Amz-Date") or ""
    when = stamp_time(stamp)
    key, scope = check_scope(req.param("X-Amz-Credential") or "", stamp, keys, region, "AuthorizationQueryParametersError")
    try:
        expires = int(req.param("X-Amz-Expires") or "")
    except ValueError:
        raise S3Error(400, "AuthorizationQueryParametersError", "X-Amz-Expires should be a number")
    if expires < 0:
        raise S3Error(400, "AuthorizationQueryParametersError", "X-Amz-Expires must be non-negative")
    if expires > MAX_EXPIRES_SECONDS:
        raise S3Error(400, "AuthorizationQueryParametersError", f"X-Amz-Expires must be less than a week (in seconds) that is {MAX_EXPIRES_SECONDS}")
    if now > when + expires:
        raise S3Error(403, "AccessDenied", "Request has expired", **{"X-Amz-Expires": expires, "Expires": iso8601(when + expires), "ServerTime": iso8601(now)})
    if when - now > MAX_SKEW_SECONDS:
        raise S3Error(403, "AccessDenied", "Request is not valid yet", **{"X-Amz-Date": stamp, "ServerTime": iso8601(now)})
    signed = (req.param("X-Amz-SignedHeaders") or "").split(";")
    require_signed(req, signed)
    provided = req.param("X-Amz-Signature") or ""
    query = [(name, value) for name, value in req.query if name != "X-Amz-Signature"]
    payload = req.header("x-amz-content-sha256") or UNSIGNED_PAYLOAD
    creq = canonical_request(req.method, req.path, query, req.headers, signed, payload)
    sts = string_to_sign(stamp, scope, creq)
    expected = compute_signature(key.secret_key, scope, sts)
    if not hmac.compare_digest(expected, provided):
        raise signature_mismatch(key.access_key, provided, creq, sts)
    return Identity(key.arn, key.account, key.access_key, "REST-QUERY-STRING", int((now - when) * 1000))


def crc32_b64(data: bytes) -> str:
    return base64.b64encode(zlib.crc32(data).to_bytes(4, "big")).decode("ascii")


def decode_aws_chunked(raw: bytes) -> Tuple[bytes, Dict[str, str]]:
    out, trailers, position = bytearray(), {}, 0
    while True:
        end = raw.find(b"\r\n", position)
        if end < 0:
            raise S3Error(400, "IncompleteBody", "aws-chunked body ended before the final chunk")
        size = int(raw[position:end].split(b";")[0], 16)
        position = end + 2
        if size == 0:
            break
        out += raw[position:position + size]
        position += size + 2
    for line in raw[position:].split(b"\r\n"):
        name, sep, value = line.decode("utf-8").partition(":")
        if sep:
            trailers[name.strip().lower()] = value.strip()
    return bytes(out), trailers


def payload_body(req: Incoming) -> bytes:
    declared = req.header("x-amz-content-sha256") or UNSIGNED_PAYLOAD
    body, checksums = req.body, {name.lower(): value for name, value in req.headers}
    if declared == STREAMING_UNSIGNED_TRAILER:
        body, trailers = decode_aws_chunked(req.body)
        checksums.update(trailers)
        decoded_length = req.header("x-amz-decoded-content-length")
        if decoded_length is not None and int(decoded_length) != len(body):
            raise S3Error(400, "IncompleteBody", f"x-amz-decoded-content-length is {decoded_length} but the chunks hold {len(body)} bytes")
    elif declared.startswith("STREAMING-"):
        raise S3Error(501, "NotImplemented", f"A header you provided implies functionality that is not implemented: x-amz-content-sha256 {declared} (signed aws-chunked uploads)")
    elif HEX64.match(declared) and sha256_hex(body) != declared:
        raise S3Error(400, "XAmzContentSHA256Mismatch", "The provided 'x-amz-content-sha256' header does not match what was computed.", ClientComputedContentSHA256=declared, S3ComputedContentSHA256=sha256_hex(body))
    if "x-amz-checksum-crc32" in checksums and checksums["x-amz-checksum-crc32"] != crc32_b64(body):
        raise S3Error(400, "BadDigest", "The CRC32 you specified did not match the calculated checksum.")
    md5 = req.header("content-md5")
    if md5 is not None:
        try:
            wanted = base64.b64decode(md5, validate=True)
        except binascii.Error:
            raise S3Error(400, "InvalidDigest", "The Content-MD5 you specified was invalid.")
        if wanted != hashlib.md5(body).digest():
            raise S3Error(400, "BadDigest", "The Content-MD5 you specified did not match what we received.")
    return body


def as_list(value: Any) -> List[Any]:
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def wildcard(pattern: str, value: str, ignore_case: bool = False) -> bool:
    regex = "".join(".*" if c == "*" else "." if c == "?" else re.escape(c) for c in pattern)
    return re.fullmatch(regex, value, re.DOTALL | (re.IGNORECASE if ignore_case else 0)) is not None


def numeric(test: Callable[[float, float], bool]) -> Callable[[str, str], bool]:
    def compare(actual: str, expected: str) -> bool:
        try:
            return test(float(actual), float(expected))
        except ValueError:
            return False
    return compare


def ip_in(actual: str, expected: str) -> bool:
    try:
        return ipaddress.ip_address(actual) in ipaddress.ip_network(expected, strict=False)
    except ValueError:
        return False


OPERATORS: Dict[str, Callable[[str, str], bool]] = {
    "StringEquals": lambda a, e: a == e,
    "StringEqualsIgnoreCase": lambda a, e: a.lower() == e.lower(),
    "StringLike": lambda a, e: wildcard(e, a),
    "ArnEquals": lambda a, e: a == e,
    "ArnLike": lambda a, e: wildcard(e, a),
    "NumericEquals": numeric(lambda a, e: a == e),
    "NumericLessThan": numeric(lambda a, e: a < e),
    "NumericLessThanEquals": numeric(lambda a, e: a <= e),
    "NumericGreaterThan": numeric(lambda a, e: a > e),
    "NumericGreaterThanEquals": numeric(lambda a, e: a >= e),
    "Bool": lambda a, e: a.lower() == e.lower(),
    "IpAddress": ip_in,
}
NEGATED = {
    "StringNotEquals": "StringEquals",
    "StringNotEqualsIgnoreCase": "StringEqualsIgnoreCase",
    "StringNotLike": "StringLike",
    "ArnNotEquals": "ArnEquals",
    "ArnNotLike": "ArnLike",
    "NumericNotEquals": "NumericEquals",
    "NotIpAddress": "IpAddress",
}


def split_operator(operator: str) -> Tuple[str, str, bool]:
    qualifier, _, base = operator.rpartition(":")
    if_exists = base.endswith("IfExists")
    return qualifier, base[:-8] if if_exists else base, if_exists


def condition_holds(operator: str, key: str, expected: Sequence[str], context: Dict[str, List[str]]) -> bool:
    qualifier, base, if_exists = split_operator(operator)
    actual = context.get(key)
    if base == "Null":
        return all((str(value).lower() == "true") == (actual is None) for value in expected)
    if actual is None:
        return if_exists or base in NEGATED or qualifier == "ForAllValues"
    if base in NEGATED:
        positive = OPERATORS[NEGATED[base]]
        return not any(positive(a, str(e)) for a in actual for e in expected)
    test = OPERATORS[base]
    if qualifier == "ForAllValues":
        return all(any(test(a, str(e)) for e in expected) for a in actual)
    return any(test(a, str(e)) for a in actual for e in expected)


def principal_matches(spec: Any, identity: Identity) -> bool:
    values = as_list(spec.get("AWS")) if isinstance(spec, dict) else as_list(spec)
    for value in values:
        if value == "*":
            return True
        if identity.anonymous:
            continue
        if value in (identity.arn, identity.account, f"arn:aws:iam::{identity.account}:root"):
            return True
    return False


def statement_matches(statement: Dict[str, Any], identity: Identity, action: str, resource: str, context: Dict[str, List[str]]) -> bool:
    if "Principal" in statement and not principal_matches(statement["Principal"], identity):
        return False
    if "NotPrincipal" in statement and principal_matches(statement["NotPrincipal"], identity):
        return False
    if "Action" in statement and not any(wildcard(p, action, ignore_case=True) for p in as_list(statement["Action"])):
        return False
    if "NotAction" in statement and any(wildcard(p, action, ignore_case=True) for p in as_list(statement["NotAction"])):
        return False
    if "Resource" in statement and not any(wildcard(p, resource) for p in as_list(statement["Resource"])):
        return False
    if "NotResource" in statement and any(wildcard(p, resource) for p in as_list(statement["NotResource"])):
        return False
    for operator, pairs in (statement.get("Condition") or {}).items():
        for key, expected in pairs.items():
            if not condition_holds(operator, key, [str(v) for v in as_list(expected)], context):
                return False
    return True


def validate_policy(document: Any, bucket: str) -> None:
    def malformed(message: str) -> S3Error:
        return S3Error(400, "MalformedPolicy", message)

    if not isinstance(document, dict) or not as_list(document.get("Statement")):
        raise malformed("Policies must be valid JSON and the first byte must be '{'; a policy needs a Statement")
    for statement in as_list(document["Statement"]):
        if not isinstance(statement, dict) or statement.get("Effect") not in ("Allow", "Deny"):
            raise malformed("Invalid effect: every statement needs Effect Allow or Deny")
        if "Principal" not in statement and "NotPrincipal" not in statement:
            raise malformed("Missing required field Principal")
        if "Action" not in statement and "NotAction" not in statement:
            raise malformed("Missing required field Action")
        for resource in as_list(statement.get("Resource")) + as_list(statement.get("NotResource")):
            if not wildcard(str(resource).split("/")[0], f"arn:aws:s3:::{bucket}"):
                raise malformed(f"Policy has invalid resource {resource}")
        for operator in (statement.get("Condition") or {}):
            _, base, _ = split_operator(operator)
            if base not in OPERATORS and base not in NEGATED and base != "Null":
                raise malformed(f"Policy has an invalid condition operator {operator}")


@dataclass
class Decision:
    allowed: bool
    reason: str
    statement: str = ""


def statement_label(statement: Dict[str, Any], index: int) -> str:
    return str(statement.get("Sid") or f"#{index}")


def authorize(owner: str, policy: Optional[Dict[str, Any]], identity: Identity, action: str, resource: str, context: Dict[str, List[str]]) -> Decision:
    deny = allow = None
    for index, statement in enumerate(as_list((policy or {}).get("Statement"))):
        if statement_matches(statement, identity, action, resource, context):
            if statement.get("Effect") == "Deny" and deny is None:
                deny = statement_label(statement, index)
            elif statement.get("Effect") == "Allow" and allow is None:
                allow = statement_label(statement, index)
    owner_root = identity.arn == f"arn:aws:iam::{owner}:root"
    if deny is not None and not (owner_root and action in POLICY_ACTIONS):
        return Decision(False, "explicit deny in a resource-based policy", deny)
    if identity.account and identity.account == owner:
        return Decision(True, "same account as the bucket owner; identity policies are modelled as allowing s3:*")
    if allow is not None:
        return Decision(True, "allowed by a resource-based policy", allow)
    return Decision(False, "no resource-based policy allows the action")


def access_denied(identity: Identity, owner: str, action: str, resource: str, decision: Decision) -> S3Error:
    if identity.account != owner or not decision.statement:
        return S3Error(403, "AccessDenied", "Access Denied")
    return S3Error(403, "AccessDenied", f'User: {identity.arn} is not authorized to perform: {action} on resource: "{resource}" with an explicit deny in a resource-based policy (statement {decision.statement})')


def md5_etag(data: bytes) -> str:
    return '"' + hashlib.md5(data).hexdigest() + '"'


def multipart_etag(part_etags: Sequence[str]) -> str:
    digests = b"".join(binascii.unhexlify(etag.strip('"')) for etag in part_etags)
    return f'"{hashlib.md5(digests).hexdigest()}-{len(part_etags)}"'


@dataclass
class ObjectVersion:
    key: str
    version_id: str
    data: bytes
    etag: str
    modified: float
    delete_marker: bool = False
    content_type: str = "binary/octet-stream"
    metadata: Dict[str, str] = field(default_factory=dict)


@dataclass
class Upload:
    upload_id: str
    key: str
    content_type: str
    metadata: Dict[str, str]
    parts: Dict[int, Tuple[str, bytes]] = field(default_factory=dict)


@dataclass
class Bucket:
    name: str
    owner: str
    created: float
    versioning: str = ""
    policy: Optional[Dict[str, Any]] = None
    objects: Dict[str, List[ObjectVersion]] = field(default_factory=dict)
    uploads: Dict[str, Upload] = field(default_factory=dict)

    def latest(self, key: str) -> Optional[ObjectVersion]:
        versions = self.objects.get(key)
        return versions[-1] if versions else None


@dataclass
class ListPage:
    contents: List[ObjectVersion]
    prefixes: List[str]
    truncated: bool
    next_token: str = ""


def encode_token(after: str, common: bool) -> str:
    return base64.urlsafe_b64encode(json.dumps([after, common]).encode("utf-8")).decode("ascii")


def decode_token(token: str) -> Tuple[str, bool]:
    try:
        after, common = json.loads(base64.urlsafe_b64decode(token.encode("ascii")).decode("utf-8"))
        return str(after), bool(common)
    except (ValueError, TypeError, binascii.Error):
        raise S3Error(400, "InvalidArgument", "The continuation token provided is incorrect")


class Store:
    def __init__(self, clock: Callable[[], float] = time.time, min_part_size: int = MIN_PART_SIZE) -> None:
        self.clock = clock
        self.min_part_size = min_part_size
        self.buckets: Dict[str, Bucket] = {}
        self.lock = threading.RLock()

    def bucket(self, name: str) -> Bucket:
        found = self.buckets.get(name)
        if found is None:
            raise S3Error(404, "NoSuchBucket", "The specified bucket does not exist", BucketName=name)
        return found

    def create_bucket(self, name: str, owner: str) -> Bucket:
        if not BUCKET_NAME.match(name) or ".." in name or re.match(r"^\d+\.\d+\.\d+\.\d+$", name):
            raise S3Error(400, "InvalidBucketName", "The specified bucket is not valid.", BucketName=name)
        existing = self.buckets.get(name)
        if existing is not None:
            if existing.owner == owner:
                raise S3Error(409, "BucketAlreadyOwnedByYou", "Your previous request to create the named bucket succeeded and you already own it.", BucketName=name)
            raise S3Error(409, "BucketAlreadyExists", "The requested bucket name is not available. The bucket namespace is shared by all users of the system. Please select a different name and try again.", BucketName=name)
        self.buckets[name] = Bucket(name, owner, self.clock())
        return self.buckets[name]

    def delete_bucket(self, name: str) -> None:
        bucket = self.bucket(name)
        if bucket.objects:
            raise S3Error(409, "BucketNotEmpty", "The bucket you tried to delete is not empty", BucketName=name)
        del self.buckets[name]

    def set_versioning(self, bucket: Bucket, status: str) -> None:
        if status not in ("Enabled", "Suspended"):
            raise S3Error(400, "MalformedXML", "The XML you provided was not well-formed or did not validate against our published schema")
        bucket.versioning = status

    def new_version_id(self, bucket: Bucket) -> str:
        return uuid.uuid4().hex if bucket.versioning == "Enabled" else "null"

    def store_version(self, bucket: Bucket, version: ObjectVersion) -> ObjectVersion:
        versions = bucket.objects.setdefault(version.key, [])
        if version.version_id == "null":
            versions[:] = [v for v in versions if v.version_id != "null"]
        versions.append(version)
        return version

    def put_object(self, bucket: Bucket, key: str, data: bytes, content_type: str = "binary/octet-stream", metadata: Optional[Dict[str, str]] = None, etag: Optional[str] = None) -> ObjectVersion:
        version = ObjectVersion(key, self.new_version_id(bucket), data, etag or md5_etag(data), self.clock(), False, content_type, dict(metadata or {}))
        return self.store_version(bucket, version)

    def get_object(self, bucket: Bucket, key: str, version_id: Optional[str] = None) -> ObjectVersion:
        versions = bucket.objects.get(key) or []
        if version_id is None:
            if not versions:
                raise S3Error(404, "NoSuchKey", "The specified key does not exist.", Key=key)
            latest = versions[-1]
            if latest.delete_marker:
                raise S3Error(404, "NoSuchKey", "The specified key does not exist.", Key=key)
            return latest
        found = next((v for v in versions if v.version_id == version_id), None)
        if found is None:
            raise S3Error(404, "NoSuchVersion", "The specified version does not exist.", Key=key, VersionId=version_id)
        if found.delete_marker:
            raise S3Error(405, "MethodNotAllowed", "The specified method is not allowed against this resource.", Method="GET", ResourceType="DeleteMarker")
        return found

    def delete_object(self, bucket: Bucket, key: str, version_id: Optional[str] = None) -> Tuple[str, bool]:
        versions = bucket.objects.get(key) or []
        if version_id is not None:
            found = next((v for v in versions if v.version_id == version_id), None)
            if found is None:
                return version_id, False
            versions.remove(found)
            if not versions:
                bucket.objects.pop(key, None)
            return version_id, found.delete_marker
        if not bucket.versioning:
            bucket.objects.pop(key, None)
            return "", False
        marker = ObjectVersion(key, self.new_version_id(bucket), b"", "", self.clock(), True)
        self.store_version(bucket, marker)
        return marker.version_id, True

    def list_objects(self, bucket: Bucket, prefix: str = "", delimiter: str = "", max_keys: int = MAX_KEYS, token: str = "", start_after: str = "") -> ListPage:
        after, after_common = decode_token(token) if token else (start_after, False)
        if max_keys <= 0:
            return ListPage([], [], False)
        contents: List[ObjectVersion] = []
        prefixes: List[str] = []
        last: Tuple[str, bool] = ("", False)
        for key in sorted(bucket.objects):
            latest = bucket.objects[key][-1]
            if latest.delete_marker or not key.startswith(prefix):
                continue
            if after and (key <= after or (after_common and key.startswith(after))):
                continue
            common = ""
            if delimiter:
                index = key.find(delimiter, len(prefix))
                if index >= 0:
                    common = key[: index + len(delimiter)]
            if common and prefixes and prefixes[-1] == common:
                continue
            if len(contents) + len(prefixes) >= max_keys:
                return ListPage(contents, prefixes, True, encode_token(*last))
            if common:
                prefixes.append(common)
                last = (common, True)
            else:
                contents.append(latest)
                last = (key, False)
        return ListPage(contents, prefixes, False)

    def list_versions(self, bucket: Bucket, prefix: str = "") -> List[Tuple[ObjectVersion, bool]]:
        rows: List[Tuple[ObjectVersion, bool]] = []
        for key in sorted(bucket.objects):
            if key.startswith(prefix):
                versions = bucket.objects[key]
                rows.extend((version, index == len(versions) - 1) for index, version in reversed(list(enumerate(versions))))
        return rows

    def create_upload(self, bucket: Bucket, key: str, content_type: str, metadata: Dict[str, str]) -> Upload:
        upload = Upload(uuid.uuid4().hex, key, content_type, metadata)
        bucket.uploads[upload.upload_id] = upload
        return upload

    def upload(self, bucket: Bucket, key: str, upload_id: str) -> Upload:
        upload = bucket.uploads.get(upload_id)
        if upload is None or upload.key != key:
            raise S3Error(404, "NoSuchUpload", "The specified upload does not exist. The upload ID may be invalid, or the upload may have been aborted or completed.", UploadId=upload_id)
        return upload

    def upload_part(self, bucket: Bucket, key: str, upload_id: str, number: int, data: bytes) -> str:
        if not 1 <= number <= 10000:
            raise S3Error(400, "InvalidArgument", "Part number must be an integer between 1 and 10000, inclusive")
        etag = md5_etag(data)
        self.upload(bucket, key, upload_id).parts[number] = (etag, data)
        return etag

    def complete_upload(self, bucket: Bucket, key: str, upload_id: str, listed: Sequence[Tuple[int, str]]) -> ObjectVersion:
        upload = self.upload(bucket, key, upload_id)
        if not listed:
            raise S3Error(400, "MalformedXML", "The XML you provided was not well-formed or did not validate against our published schema")
        numbers = [number for number, _ in listed]
        if numbers != sorted(set(numbers)):
            raise S3Error(400, "InvalidPartOrder", "The list of parts was not in ascending order. Parts must be ordered by part number.", UploadId=upload_id)
        chosen: List[Tuple[str, bytes]] = []
        for number, etag in listed:
            part = upload.parts.get(number)
            if part is None or part[0].strip('"') != etag.strip('"'):
                raise S3Error(400, "InvalidPart", "One or more of the specified parts could not be found. The part may not have been uploaded, or the specified entity tag may not match the part's entity tag.", UploadId=upload_id, PartNumber=number)
            chosen.append(part)
        for etag, data in chosen[:-1]:
            if len(data) < self.min_part_size:
                raise S3Error(400, "EntityTooSmall", "Your proposed upload is smaller than the minimum allowed object size.", ProposedSize=len(data), MinSizeAllowed=self.min_part_size)
        del bucket.uploads[upload_id]
        data = b"".join(part for _, part in chosen)
        return self.put_object(bucket, key, data, upload.content_type, upload.metadata, multipart_etag([etag for etag, _ in chosen]))


@dataclass
class Response:
    status: int
    headers: Dict[str, str] = field(default_factory=dict)
    body: bytes = b""


def xml_document(root: str, inner: str) -> bytes:
    return f'<?xml version="1.0" encoding="UTF-8"?>\n<{root} xmlns="{S3_NS}">{inner}</{root}>'.encode("utf-8")


def element(name: str, value: Any) -> str:
    return f"<{name}>{escape(str(value), QUOTE_ENTITY)}</{name}>"


def strip_ns(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def parse_xml(body: bytes) -> ElementTree.Element:
    try:
        return ElementTree.fromstring(body)
    except ElementTree.ParseError:
        raise S3Error(400, "MalformedXML", "The XML you provided was not well-formed or did not validate against our published schema")


def children(node: ElementTree.Element, name: str) -> List[ElementTree.Element]:
    return [child for child in node if strip_ns(child.tag) == name]


def child_text(node: ElementTree.Element, name: str, default: str = "") -> str:
    found = children(node, name)
    return (found[0].text or "") if found else default


def object_headers(version: ObjectVersion, versioned: bool) -> Dict[str, str]:
    headers = {
        "ETag": version.etag,
        "Last-Modified": formatdate(version.modified, usegmt=True),
        "Content-Type": version.content_type,
        "Accept-Ranges": "bytes",
    }
    if versioned:
        headers["x-amz-version-id"] = version.version_id
    headers.update({f"x-amz-meta-{name}": value for name, value in version.metadata.items()})
    return headers


def byte_range(header: str, size: int) -> Tuple[int, int]:
    match = re.fullmatch(r"bytes=(\d*)-(\d*)", header.strip())
    if not match or match.groups() == ("", ""):
        raise S3Error(416, "InvalidRange", "The requested range is not satisfiable", RangeRequested=header, ActualObjectSize=size)
    first, last = match.groups()
    if first == "":
        start, end = max(size - int(last), 0), size - 1
    else:
        start, end = int(first), min(int(last), size - 1) if last else size - 1
    if start >= size or start > end:
        raise S3Error(416, "InvalidRange", "The requested range is not satisfiable", RangeRequested=header, ActualObjectSize=size)
    return start, end


class S3Service:
    def __init__(self, store: Store, keys: Dict[str, Key], region: str = "us-east-1", clock: Callable[[], float] = time.time) -> None:
        self.store = store
        self.keys = keys
        self.region = region
        self.clock = clock

    def handle(self, req: Incoming) -> Response:
        identity = authenticate(req, self.keys, self.region, self.clock())
        parts = req.path.lstrip("/").split("/", 1)
        bucket_name = parts[0]
        key = parts[1] if len(parts) > 1 else ""
        with self.store.lock:
            if not bucket_name:
                return self.service_level(req, identity)
            if not key:
                return self.bucket_level(req, identity, bucket_name)
            return self.object_level(req, identity, bucket_name, key)

    def context(self, req: Incoming, identity: Identity, extra: Optional[Dict[str, str]] = None) -> Dict[str, List[str]]:
        context = {"aws:SecureTransport": ["true" if req.secure else "false"], "aws:SourceIp": [req.client_ip]}
        if not identity.anonymous:
            context.update({
                "aws:PrincipalArn": [identity.arn],
                "aws:PrincipalAccount": [identity.account],
                "s3:authType": [identity.auth_type],
                "s3:signatureversion": [ALGORITHM],
            })
            if identity.auth_type == "REST-QUERY-STRING":
                context["s3:signatureAge"] = [str(identity.signature_age_ms or 0)]
            if req.header("x-amz-content-sha256"):
                context["s3:x-amz-content-sha256"] = [req.header("x-amz-content-sha256") or ""]
        context.update({name: [value] for name, value in (extra or {}).items()})
        return context

    def require(self, bucket: Bucket, req: Incoming, identity: Identity, action: str, key: str = "", extra: Optional[Dict[str, str]] = None) -> Decision:
        resource = f"arn:aws:s3:::{bucket.name}" + (f"/{key}" if key else "")
        decision = authorize(bucket.owner, bucket.policy, identity, action, resource, self.context(req, identity, extra))
        if not decision.allowed:
            raise access_denied(identity, bucket.owner, action, resource, decision)
        return decision

    def service_level(self, req: Incoming, identity: Identity) -> Response:
        if req.method != "GET":
            raise S3Error(405, "MethodNotAllowed", "The specified method is not allowed against this resource.", Method=req.method, ResourceType="SERVICE")
        if identity.anonymous:
            raise S3Error(403, "AccessDenied", "Access Denied")
        owned = sorted((b for b in self.store.buckets.values() if b.owner == identity.account), key=lambda b: b.name)
        inner = "".join(f"<Bucket>{element('Name', b.name)}{element('CreationDate', iso8601(b.created))}</Bucket>" for b in owned)
        return Response(200, {"Content-Type": "application/xml"}, xml_document("ListAllMyBucketsResult", f"<Owner>{element('ID', identity.account)}</Owner><Buckets>{inner}</Buckets>"))

    def bucket_level(self, req: Incoming, identity: Identity, name: str) -> Response:
        method = req.method
        if method == "PUT" and not req.query:
            if identity.anonymous:
                raise S3Error(403, "AccessDenied", "Access Denied")
            payload_body(req)
            self.store.create_bucket(name, identity.account)
            return Response(200, {"Location": f"/{name}"})
        bucket = self.store.bucket(name)
        if req.has("versioning"):
            if method == "PUT":
                self.require(bucket, req, identity, "s3:PutBucketVersioning")
                self.store.set_versioning(bucket, child_text(parse_xml(payload_body(req)), "Status"))
                return Response(200)
            if method == "GET":
                self.require(bucket, req, identity, "s3:GetBucketVersioning")
                inner = element("Status", bucket.versioning) if bucket.versioning else ""
                return Response(200, {"Content-Type": "application/xml"}, xml_document("VersioningConfiguration", inner))
        if req.has("policy"):
            return self.bucket_policy(req, identity, bucket)
        if req.has("location") and method == "GET":
            self.require(bucket, req, identity, "s3:GetBucketLocation")
            location = "" if self.region == "us-east-1" else self.region
            return Response(200, {"Content-Type": "application/xml"}, xml_document("LocationConstraint", escape(location)))
        if req.has("versions") and method == "GET":
            self.require(bucket, req, identity, "s3:ListBucketVersions", extra={"s3:prefix": req.param("prefix") or ""})
            return self.list_versions(req, bucket)
        if method == "GET" and req.param("list-type") == "2":
            extra = {"s3:prefix": req.param("prefix") or "", "s3:delimiter": req.param("delimiter") or "", "s3:max-keys": req.param("max-keys") or str(MAX_KEYS)}
            self.require(bucket, req, identity, "s3:ListBucket", extra=extra)
            return self.list_objects_v2(req, bucket)
        if method == "HEAD" and not req.query:
            self.require(bucket, req, identity, "s3:ListBucket")
            return Response(200, {"x-amz-bucket-region": self.region})
        if method == "DELETE" and not req.query:
            self.require(bucket, req, identity, "s3:DeleteBucket")
            self.store.delete_bucket(name)
            return Response(204)
        raise S3Error(501, "NotImplemented", f"minis3 does not implement {method} on a bucket with {'&'.join(k for k, _ in req.query) or 'no subresource'} (ListObjects v1 needs list-type=2)")

    def bucket_policy(self, req: Incoming, identity: Identity, bucket: Bucket) -> Response:
        if req.method == "PUT":
            self.require(bucket, req, identity, "s3:PutBucketPolicy")
            try:
                document = json.loads(payload_body(req).decode("utf-8"))
            except (UnicodeDecodeError, ValueError):
                raise S3Error(400, "MalformedPolicy", "Policies must be valid JSON and the first byte must be '{'")
            validate_policy(document, bucket.name)
            bucket.policy = document
            return Response(204)
        if req.method == "GET":
            self.require(bucket, req, identity, "s3:GetBucketPolicy")
            if bucket.policy is None:
                raise S3Error(404, "NoSuchBucketPolicy", "The bucket policy does not exist", BucketName=bucket.name)
            return Response(200, {"Content-Type": "application/json"}, json.dumps(bucket.policy).encode("utf-8"))
        if req.method == "DELETE":
            self.require(bucket, req, identity, "s3:DeleteBucketPolicy")
            bucket.policy = None
            return Response(204)
        raise S3Error(405, "MethodNotAllowed", "The specified method is not allowed against this resource.", Method=req.method, ResourceType="BUCKETPOLICY")

    def list_objects_v2(self, req: Incoming, bucket: Bucket) -> Response:
        prefix, delimiter = req.param("prefix") or "", req.param("delimiter") or ""
        try:
            max_keys = int(req.param("max-keys") or MAX_KEYS)
        except ValueError:
            raise S3Error(400, "InvalidArgument", "Provided max-keys not an integer or within integer range")
        max_keys = max(0, min(max_keys, MAX_KEYS))
        token, start_after = req.param("continuation-token") or "", req.param("start-after") or ""
        page = self.store.list_objects(bucket, prefix, delimiter, max_keys, token, start_after)
        url = req.param("encoding-type") == "url"
        show = (lambda text: urllib.parse.quote(text, safe="")) if url else (lambda text: text)
        inner = [element("Name", bucket.name), element("Prefix", show(prefix))]
        if delimiter:
            inner.append(element("Delimiter", show(delimiter)))
        inner += [element("MaxKeys", max_keys), element("KeyCount", len(page.contents) + len(page.prefixes)), element("IsTruncated", str(page.truncated).lower())]
        if url:
            inner.append(element("EncodingType", "url"))
        if token:
            inner.append(element("ContinuationToken", token))
        if page.truncated:
            inner.append(element("NextContinuationToken", page.next_token))
        if start_after:
            inner.append(element("StartAfter", show(start_after)))
        for version in page.contents:
            inner.append(f"<Contents>{element('Key', show(version.key))}{element('LastModified', iso8601(version.modified))}{element('ETag', version.etag)}{element('Size', len(version.data))}{element('StorageClass', 'STANDARD')}</Contents>")
        inner += [f"<CommonPrefixes>{element('Prefix', show(common))}</CommonPrefixes>" for common in page.prefixes]
        return Response(200, {"Content-Type": "application/xml"}, xml_document("ListBucketResult", "".join(inner)))

    def list_versions(self, req: Incoming, bucket: Bucket) -> Response:
        prefix = req.param("prefix") or ""
        inner = [element("Name", bucket.name), element("Prefix", prefix), element("KeyMarker", ""), element("VersionIdMarker", ""), element("MaxKeys", MAX_KEYS), element("IsTruncated", "false")]
        for version, latest in self.store.list_versions(bucket, prefix):
            common = element("Key", version.key) + element("VersionId", version.version_id) + element("IsLatest", str(latest).lower()) + element("LastModified", iso8601(version.modified))
            if version.delete_marker:
                inner.append(f"<DeleteMarker>{common}</DeleteMarker>")
            else:
                inner.append(f"<Version>{common}{element('ETag', version.etag)}{element('Size', len(version.data))}{element('StorageClass', 'STANDARD')}</Version>")
        return Response(200, {"Content-Type": "application/xml"}, xml_document("ListVersionsResult", "".join(inner)))

    def object_level(self, req: Incoming, identity: Identity, name: str, key: str) -> Response:
        bucket = self.store.bucket(name)
        method = req.method
        version_id = req.param("versionId")
        versioned = bool(bucket.versioning)
        if method == "PUT" and req.has("partNumber") and req.has("uploadId"):
            self.require(bucket, req, identity, "s3:PutObject", key)
            try:
                number = int(req.param("partNumber") or "")
            except ValueError:
                raise S3Error(400, "InvalidArgument", "Part number must be an integer between 1 and 10000, inclusive")
            etag = self.store.upload_part(bucket, key, req.param("uploadId") or "", number, payload_body(req))
            return Response(200, {"ETag": etag})
        if method == "PUT":
            if req.header("x-amz-copy-source"):
                raise S3Error(501, "NotImplemented", "minis3 does not implement CopyObject")
            self.require(bucket, req, identity, "s3:PutObject", key)
            version = self.store.put_object(bucket, key, payload_body(req), req.header("content-type") or "binary/octet-stream", self.metadata(req))
            headers = {"ETag": version.etag}
            if versioned:
                headers["x-amz-version-id"] = version.version_id
            return Response(200, headers)
        if method == "POST" and req.has("uploads"):
            self.require(bucket, req, identity, "s3:PutObject", key)
            upload = self.store.create_upload(bucket, key, req.header("content-type") or "binary/octet-stream", self.metadata(req))
            inner = element("Bucket", name) + element("Key", key) + element("UploadId", upload.upload_id)
            return Response(200, {"Content-Type": "application/xml"}, xml_document("InitiateMultipartUploadResult", inner))
        if method == "POST" and req.has("uploadId"):
            self.require(bucket, req, identity, "s3:PutObject", key)
            root = parse_xml(payload_body(req))
            try:
                listed = [(int(child_text(part, "PartNumber")), child_text(part, "ETag")) for part in children(root, "Part")]
            except ValueError:
                raise S3Error(400, "MalformedXML", "The XML you provided was not well-formed or did not validate against our published schema")
            version = self.store.complete_upload(bucket, key, req.param("uploadId") or "", listed)
            inner = element("Location", f"/{name}/{key}") + element("Bucket", name) + element("Key", key) + element("ETag", version.etag)
            headers = {"Content-Type": "application/xml"}
            if versioned:
                headers["x-amz-version-id"] = version.version_id
            return Response(200, headers, xml_document("CompleteMultipartUploadResult", inner))
        if method == "DELETE" and req.has("uploadId"):
            self.require(bucket, req, identity, "s3:AbortMultipartUpload", key)
            self.store.upload(bucket, key, req.param("uploadId") or "")
            del bucket.uploads[req.param("uploadId") or ""]
            return Response(204)
        if method in ("GET", "HEAD"):
            action = "s3:GetObjectVersion" if version_id else "s3:GetObject"
            self.require(bucket, req, identity, action, key, {"s3:VersionId": version_id} if version_id else None)
            try:
                version = self.store.get_object(bucket, key, version_id)
            except S3Error as exc:
                latest = bucket.latest(key)
                if exc.code == "NoSuchKey" and latest is not None and latest.delete_marker:
                    exc.headers.update({"x-amz-delete-marker": "true", "x-amz-version-id": latest.version_id})
                raise
            headers = object_headers(version, versioned)
            data = version.data
            status = 200
            if req.header("range") and method == "GET":
                start, end = byte_range(req.header("range") or "", len(data))
                headers["Content-Range"] = f"bytes {start}-{end}/{len(data)}"
                data, status = data[start:end + 1], 206
            headers["Content-Length"] = str(len(data))
            return Response(status, headers, data)
        if method == "DELETE":
            action = "s3:DeleteObjectVersion" if version_id else "s3:DeleteObject"
            self.require(bucket, req, identity, action, key)
            removed, marker = self.store.delete_object(bucket, key, version_id)
            headers = {}
            if removed:
                headers["x-amz-version-id"] = removed
            if marker:
                headers["x-amz-delete-marker"] = "true"
            return Response(204, headers)
        raise S3Error(501, "NotImplemented", f"minis3 does not implement {method} on an object with {'&'.join(k for k, _ in req.query) or 'no subresource'}")

    @staticmethod
    def metadata(req: Incoming) -> Dict[str, str]:
        return {name.lower()[len("x-amz-meta-"):]: value for name, value in req.headers if name.lower().startswith("x-amz-meta-")}


class S3Server(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address: Tuple[str, int], service: S3Service, log: Optional[Callable[[str], None]] = None) -> None:
        super().__init__(address, S3Handler)
        self.service = service
        self.log = log


def make_server(service: S3Service, host: str = "127.0.0.1", port: int = 0, log: Optional[Callable[[str], None]] = None) -> S3Server:
    return S3Server((host, port), service, log)


class S3Handler(BaseHTTPRequestHandler):
    server: S3Server
    protocol_version = "HTTP/1.1"
    server_version = "minis3/1.0"

    def handle_any(self) -> None:
        request_id = secrets.token_hex(8).upper()
        split = urllib.parse.urlsplit(self.path)
        resource = urllib.parse.unquote(split.path)
        try:
            if self.headers.get("Transfer-Encoding", "").lower() == "chunked":
                self.close_connection = True
                raise S3Error(411, "MissingContentLength", "You must provide the Content-Length HTTP header.")
            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(length) if length > 0 else b""
            incoming = Incoming(self.command, resource, parse_query(split.query), list(self.headers.items()), body, self.client_address[0])
            response = self.server.service.handle(incoming)
            outcome = "ok"
        except S3Error as exc:
            body = b"" if self.command == "HEAD" else exc.to_xml(resource, request_id)
            response = Response(exc.status, dict(exc.headers, **{"Content-Type": "application/xml"}), body)
            outcome = exc.code
        response.headers.setdefault("x-amz-request-id", request_id)
        self.send_response(response.status)
        for name, value in response.headers.items():
            self.send_header(name, value)
        if "Content-Length" not in response.headers:
            self.send_header("Content-Length", str(len(response.body)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(response.body)
        if self.server.log:
            self.server.log(f"{self.client_address[0]} {self.command} {self.path.split('?')[0]} {response.status} {outcome}")

    do_GET = do_PUT = do_POST = do_DELETE = do_HEAD = handle_any

    def log_message(self, fmt: str, *args: Any) -> None:
        pass


class ClientError(Exception):
    pass


@dataclass
class HTTPResult:
    status: int
    headers: Dict[str, str]
    body: bytes

    def error_code(self) -> str:
        match = re.search(rb"<Code>([^<]+)</Code>", self.body)
        return match.group(1).decode("utf-8") if match else ""

    def error_field(self, name: str) -> str:
        match = re.search(f"<{name}>(.*?)</{name}>".encode("utf-8"), self.body, re.DOTALL)
        return match.group(1).decode("utf-8") if match else ""

    def xml(self) -> ElementTree.Element:
        return ElementTree.fromstring(self.body)


def split_url(url: str) -> Tuple[str, str, str, Pairs]:
    parts = urllib.parse.urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.netloc:
        raise ClientError(f"not an http(s) URL: {url!r}")
    return parts.scheme, parts.netloc, urllib.parse.unquote(parts.path) or "/", parse_query(parts.query)


def send_raw(method: str, url: str, headers: Sequence[Tuple[str, str]], body: bytes = b"", timeout: float = 30.0) -> HTTPResult:
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    request = urllib.request.Request(url, data=body if method in ("PUT", "POST") else None, method=method)
    for name, value in headers:
        request.add_unredirected_header(name, value)
    if request.data is not None and not request.has_header("Content-type"):
        request.add_unredirected_header("Content-type", "binary/octet-stream")
    try:
        with opener.open(request, timeout=timeout) as response:
            return HTTPResult(response.status, dict(response.headers), response.read())
    except urllib.error.HTTPError as exc:
        return HTTPResult(exc.code, dict(exc.headers or {}), exc.read())
    except OSError as exc:
        raise ClientError(f"cannot reach {url}: {getattr(exc, 'reason', exc)}")


class S3Client:
    def __init__(self, endpoint: str, creds: Optional[Credentials], region: str = "us-east-1", clock: Callable[[], float] = time.time, timeout: float = 30.0) -> None:
        self.endpoint = endpoint.rstrip("/")
        self.creds = creds
        self.region = region
        self.clock = clock
        self.timeout = timeout

    def url_for(self, bucket: str = "", key: str = "") -> str:
        return self.endpoint + "/" + (bucket + ("/" + key if key else "") if bucket else "")

    def sign(self, method: str, url: str, headers: Optional[Dict[str, str]] = None, body: bytes = b"", unsigned_payload: bool = False) -> Tuple[str, Signed]:
        scheme, netloc, path, query = split_url(url)
        pairs = [("Host", netloc)] + list((headers or {}).items())
        if self.creds is None:
            signed = Signed(method.upper(), path, query, pairs, "", "", "")
        else:
            payload = UNSIGNED_PAYLOAD if unsigned_payload else sha256_hex(body)
            signed = sign_with_headers(method, path, query, pairs, payload, self.creds, self.region, "s3", self.clock())
        return f"{scheme}://{netloc}{signed.target()}", signed

    def send(self, method: str, url: str, headers: Optional[Dict[str, str]] = None, body: bytes = b"", unsigned_payload: bool = False) -> HTTPResult:
        wire, signed = self.sign(method, url, headers, body, unsigned_payload)
        return send_raw(method.upper(), wire, signed.headers, body, self.timeout)

    def call(self, method: str, bucket: str = "", key: str = "", query: Optional[Sequence[Tuple[str, str]]] = None, body: bytes = b"", headers: Optional[Dict[str, str]] = None) -> HTTPResult:
        url = self.url_for(bucket, urllib.parse.quote(key, safe="/~"))
        if query:
            url += "?" + "&".join(f"{uri_encode(k)}={uri_encode(v)}" for k, v in query)
        return self.send(method, url, headers, body)

    def presign(self, method: str, bucket: str, key: str, expires: int = 3600, when: Optional[float] = None) -> str:
        if self.creds is None:
            raise ClientError("presigning needs credentials (AWS_ACCESS_KEY_ID and AWS_SECRET_ACCESS_KEY)")
        return presign_url(method, self.url_for(bucket, urllib.parse.quote(key, safe="/~")), self.creds, self.region, expires, self.clock() if when is None else when)

    def create_bucket(self, bucket: str) -> HTTPResult:
        return self.call("PUT", bucket)

    def put_object(self, bucket: str, key: str, data: bytes, headers: Optional[Dict[str, str]] = None) -> HTTPResult:
        return self.call("PUT", bucket, key, body=data, headers=headers)

    def get_object(self, bucket: str, key: str, version_id: Optional[str] = None, byte_range: Optional[str] = None) -> HTTPResult:
        return self.call("GET", bucket, key, [("versionId", version_id)] if version_id else None, headers={"Range": byte_range} if byte_range else None)

    def delete_object(self, bucket: str, key: str, version_id: Optional[str] = None) -> HTTPResult:
        return self.call("DELETE", bucket, key, [("versionId", version_id)] if version_id else None)

    def put_versioning(self, bucket: str, status: str) -> HTTPResult:
        body = f'<VersioningConfiguration xmlns="{S3_NS}"><Status>{status}</Status></VersioningConfiguration>'.encode("utf-8")
        return self.call("PUT", bucket, query=[("versioning", "")], body=body)

    def put_policy(self, bucket: str, policy: Dict[str, Any]) -> HTTPResult:
        return self.call("PUT", bucket, query=[("policy", "")], body=json.dumps(policy).encode("utf-8"))

    def list_objects_v2(self, bucket: str, prefix: str = "", delimiter: str = "", max_keys: Optional[int] = None, token: str = "", encoding: str = "") -> Dict[str, Any]:
        query = [("list-type", "2"), ("prefix", prefix)]
        if delimiter:
            query.append(("delimiter", delimiter))
        if max_keys is not None:
            query.append(("max-keys", str(max_keys)))
        if token:
            query.append(("continuation-token", token))
        if encoding:
            query.append(("encoding-type", encoding))
        result = self.call("GET", bucket, query=query)
        if result.status != 200:
            raise ClientError(f"ListObjectsV2 failed: HTTP {result.status} {result.error_code()}")
        root = result.xml()
        return {
            "keys": [child_text(c, "Key") for c in children(root, "Contents")],
            "etags": [child_text(c, "ETag") for c in children(root, "Contents")],
            "prefixes": [child_text(c, "Prefix") for c in children(root, "CommonPrefixes")],
            "truncated": child_text(root, "IsTruncated") == "true",
            "next": child_text(root, "NextContinuationToken"),
            "key_count": int(child_text(root, "KeyCount", "0")),
        }

    def multipart_upload(self, bucket: str, key: str, parts: Sequence[bytes]) -> HTTPResult:
        started = self.call("POST", bucket, key, [("uploads", "")])
        if started.status != 200:
            return started
        upload_id = child_text(started.xml(), "UploadId")
        listed = []
        for number, data in enumerate(parts, start=1):
            result = self.call("PUT", bucket, key, [("partNumber", str(number)), ("uploadId", upload_id)], body=data)
            if result.status != 200:
                return result
            listed.append(f"<Part><PartNumber>{number}</PartNumber><ETag>{escape(result.headers.get('ETag', ''))}</ETag></Part>")
        body = f"<CompleteMultipartUpload>{''.join(listed)}</CompleteMultipartUpload>".encode("utf-8")
        return self.call("POST", bucket, key, [("uploadId", upload_id)], body=body)


def presign_url(method: str, url: str, creds: Credentials, region: str, expires: int, when: float, headers: Optional[Dict[str, str]] = None) -> str:
    scheme, netloc, path, query = split_url(url)
    pairs = [("Host", netloc)] + list((headers or {}).items())
    signed = presign(method, path, query, pairs, creds, region, "s3", when, expires)
    return f"{scheme}://{netloc}{signed.target()}"


@dataclass
class UrlReport:
    access_key: str
    date: str
    region: str
    service: str
    signed_headers: List[str]
    expires: int
    valid_from: float
    valid_until: float
    seconds_left: float
    signature: str
    verified: Optional[bool] = None
    canonical_request: str = ""
    string_to_sign: str = ""


def inspect_presigned(url: str, now: float, secret: Optional[str] = None, method: str = "GET") -> UrlReport:
    _, netloc, path, query = split_url(url)
    params = dict(query)
    if params.get("X-Amz-Algorithm") != ALGORITHM:
        raise ClientError("not a SigV4 presigned URL: X-Amz-Algorithm=AWS4-HMAC-SHA256 is missing (a URL with AWSAccessKeyId and Signature is the retired SigV2 form)")
    try:
        access_key, date, region, service, _ = params["X-Amz-Credential"].split("/")
        start, expires = parse_amz_date(params["X-Amz-Date"]), int(params["X-Amz-Expires"])
        signed_headers, signature = params["X-Amz-SignedHeaders"].split(";"), params["X-Amz-Signature"]
    except (KeyError, ValueError):
        raise ClientError("presigned URL is missing or has malformed X-Amz-Credential, X-Amz-Date, X-Amz-Expires, X-Amz-SignedHeaders or X-Amz-Signature")
    report = UrlReport(access_key, date, region, service, signed_headers, expires, start, start + expires, start + expires - now, signature)
    if secret is not None:
        scope = credential_scope(date, region, service)
        rest = [(k, v) for k, v in query if k != "X-Amz-Signature"]
        report.canonical_request = canonical_request(method, path, rest, [("host", netloc)], signed_headers, UNSIGNED_PAYLOAD)
        report.string_to_sign = string_to_sign(params["X-Amz-Date"], scope, report.canonical_request)
        report.verified = hmac.compare_digest(compute_signature(secret, scope, report.string_to_sign), signature)
    return report


@dataclass
class Vector:
    name: str
    context: Dict[str, Any]
    request: str
    expected: Dict[str, Dict[str, str]]


@dataclass
class VectorResult:
    name: str
    mode: str
    passed: bool
    mismatches: List[str]
    canonical_request: str = ""


def parse_raw_request(text: str) -> Tuple[str, str, Pairs, Pairs, bytes]:
    head, _, body = text.replace("\r\n", "\n").partition("\n\n")
    lines = head.split("\n")
    method, _, rest = lines[0].partition(" ")
    target = rest.rsplit(" ", 1)[0]
    raw_path, _, raw_query = target.partition("?")
    headers: Pairs = []
    for line in lines[1:]:
        if line[:1] in (" ", "\t") and headers:
            name, value = headers[-1]
            headers[-1] = (name, value + " " + line.strip())
        elif line:
            name, _, value = line.partition(":")
            headers.append((name, value))
    return method, urllib.parse.unquote(raw_path), parse_query(raw_query), headers, body.encode("utf-8")


def run_vector(vector: Vector) -> List[VectorResult]:
    ctx = vector.context
    raw = ctx["credentials"]
    creds = Credentials(raw["access_key_id"], raw["secret_access_key"], raw.get("token", ""))
    when = datetime.strptime(ctx["timestamp"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).timestamp()
    method, path, query, headers, body = parse_raw_request(vector.request)
    normalize = bool(ctx.get("normalize", False))
    sign_token = not ctx.get("omit_session_token", False)
    results = []
    for mode, expected in sorted(vector.expected.items()):
        if mode == "header":
            payload = ctx.get("header_payload_hash") or sha256_hex(body)
            signed = sign_with_headers(method, path, query, headers, payload, creds, ctx["region"], ctx["service"], when, bool(ctx.get("sign_body")), normalize, sign_token)
            actual = {"canonical-request": signed.canonical_request, "string-to-sign": signed.string_to_sign, "signature": signed.signature, "authorization": signed.authorization}
        else:
            payload = ctx.get("query_payload_hash") or sha256_hex(body)
            signed = presign(method, path, query, headers, creds, ctx["region"], ctx["service"], when, int(ctx.get("expiration_in_seconds", 3600)), payload, normalize, sign_token)
            actual = {"canonical-request": signed.canonical_request, "string-to-sign": signed.string_to_sign, "signature": signed.signature, "query": sorted(signed.query)}
        mismatches = []
        for name, want in expected.items():
            if name == "signed-request":
                got = signed_request_value(want, mode)
                name, want = ("authorization", got) if mode == "header" else ("query", got)
            if want is not None and actual.get(name) != want:
                mismatches.append(name)
        results.append(VectorResult(vector.name, mode, not mismatches, mismatches, signed.canonical_request))
    return results


def signed_request_value(text: str, mode: str) -> Any:
    lines = text.replace("\r\n", "\n").split("\n")
    if mode == "header":
        return next((line.partition(":")[2] for line in lines if line.lower().startswith("authorization:")), None)
    target = lines[0].partition(" ")[2].rsplit(" ", 1)[0]
    return sorted(parse_query(target.partition("?")[2]))


VECTOR_FILES = {
    "header": {"canonical-request": "header-canonical-request.txt", "string-to-sign": "header-string-to-sign.txt", "signature": "header-signature.txt", "signed-request": "header-signed-request.txt"},
    "query": {"canonical-request": "query-canonical-request.txt", "string-to-sign": "query-string-to-sign.txt", "signature": "query-signature.txt", "signed-request": "query-signed-request.txt"},
}


def load_vectors(path: str) -> List[Vector]:
    if os.path.isfile(path):
        with open(path, encoding="utf-8") as handle:
            document = json.load(handle)
        return [Vector(name, item["context"], item["request"], item["expected"]) for name, item in sorted(document["vectors"].items())]
    vectors = []
    for name in sorted(os.listdir(path)):
        folder = os.path.join(path, name)
        if not os.path.isfile(os.path.join(folder, "request.txt")):
            continue
        with open(os.path.join(folder, "context.json"), encoding="utf-8") as handle:
            context = json.load(handle)
        with open(os.path.join(folder, "request.txt"), encoding="utf-8") as handle:
            request = handle.read()
        expected: Dict[str, Dict[str, str]] = {}
        for mode, files in VECTOR_FILES.items():
            for field_name, file_name in files.items():
                file_path = os.path.join(folder, file_name)
                if os.path.isfile(file_path):
                    with open(file_path, encoding="utf-8") as handle:
                        text = handle.read()
                    expected.setdefault(mode, {})[field_name] = text if field_name == "signed-request" else text.rstrip("\n")
        vectors.append(Vector(name, context, request, expected))
    return vectors


def env_credentials(args: argparse.Namespace) -> Optional[Credentials]:
    access = args.access_key or os.environ.get("AWS_ACCESS_KEY_ID")
    secret = args.secret_key or os.environ.get("AWS_SECRET_ACCESS_KEY")
    if not access or not secret:
        return None
    return Credentials(access, secret, os.environ.get("AWS_SESSION_TOKEN", ""))


def parse_headers(values: Sequence[str]) -> Dict[str, str]:
    headers: Dict[str, str] = {}
    for value in values:
        name, sep, text = value.partition(":")
        if not sep or not name.strip():
            raise ValueError(f"--header must be 'Name: value', got {value!r}")
        headers[name.strip()] = text.strip()
    return headers


def fixed_clock(args: argparse.Namespace) -> Callable[[], float]:
    if getattr(args, "date", None):
        moment = parse_amz_date(args.date)
        return lambda: moment
    return time.time


def read_data(path: Optional[str]) -> bytes:
    if not path:
        return b""
    if path == "-":
        return sys.stdin.buffer.read()
    with open(path, "rb") as handle:
        return handle.read()


def need_credentials(args: argparse.Namespace) -> Credentials:
    creds = env_credentials(args)
    if creds is None:
        raise ClientError("no credentials: pass --access-key and --secret-key or set AWS_ACCESS_KEY_ID and AWS_SECRET_ACCESS_KEY")
    return creds


def cmd_serve(args: argparse.Namespace) -> int:
    specs = list(args.key or [])
    if not specs and os.environ.get("AWS_ACCESS_KEY_ID") and os.environ.get("AWS_SECRET_ACCESS_KEY"):
        specs.append(f"{os.environ['AWS_ACCESS_KEY_ID']}:{os.environ['AWS_SECRET_ACCESS_KEY']}")
    if not specs:
        print("minis3: serve needs at least one --key ACCESS_KEY:SECRET_KEY[:PRINCIPAL_ARN] (or AWS_ACCESS_KEY_ID and AWS_SECRET_ACCESS_KEY)", file=sys.stderr)
        return 2
    keys = {key.access_key: key for key in (parse_key_spec(spec) for spec in specs)}
    log = None if args.quiet else (lambda line: print(line, file=sys.stderr, flush=True))
    service = S3Service(Store(min_part_size=args.min_part_size), keys, args.region)
    server = make_server(service, args.host, args.port, log)
    host, port = server.server_address[:2]
    if args.port_file:
        with open(args.port_file + ".tmp", "w", encoding="ascii") as handle:
            handle.write(f"{port}\n")
        os.replace(args.port_file + ".tmp", args.port_file)
    principals = ", ".join(f"{key.access_key} as {key.arn}" for key in keys.values())
    print(f"minis3 listening on http://{host}:{port} (region {args.region}; keys: {principals})", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


def cmd_presign(args: argparse.Namespace) -> int:
    url = presign_url(args.method, args.url, need_credentials(args), args.region, args.expires, fixed_clock(args)(), parse_headers(args.header or []))
    print(url)
    return 0


def cmd_sign(args: argparse.Namespace) -> int:
    client = S3Client("", need_credentials(args), args.region, fixed_clock(args))
    wire, signed = client.sign(args.method, args.url, parse_headers(args.header or []), read_data(args.data_file), args.unsigned_payload)
    if args.json:
        print(json.dumps({"url": wire, "canonical_request": signed.canonical_request, "string_to_sign": signed.string_to_sign, "signature": signed.signature, "headers": [[k, v] for k, v in signed.headers]}, indent=2))
        return 0
    print("# canonical request\n" + signed.canonical_request + "\n\n# string to sign\n" + signed.string_to_sign + "\n\n# headers to send")
    for name, value in signed.headers:
        print(f"{name}: {value}")
    return 0


def cmd_send(args: argparse.Namespace) -> int:
    creds = None if args.anonymous else need_credentials(args)
    client = S3Client("", creds, args.region, fixed_clock(args))
    result = client.send(args.method, args.url, parse_headers(args.header or []), read_data(args.data_file), args.unsigned_payload)
    if args.json:
        print(json.dumps({"status": result.status, "code": result.error_code(), "headers": result.headers, "body": result.body.decode("utf-8", "replace")}, indent=2))
    else:
        print(f"HTTP {result.status}", file=sys.stderr)
        for name in ("ETag", "x-amz-version-id", "x-amz-delete-marker", "Content-Range"):
            if name in result.headers:
                print(f"{name}: {result.headers[name]}", file=sys.stderr)
        if args.output:
            with open(args.output, "wb") as handle:
                handle.write(result.body)
        else:
            sys.stdout.buffer.write(result.body)
            sys.stdout.flush()
    return 0 if result.status < 300 else 1


def cmd_inspect(args: argparse.Namespace) -> int:
    report = inspect_presigned(args.url, fixed_clock(args)(), args.secret_key, args.method)
    if args.json:
        print(json.dumps(report.__dict__, indent=2))
    else:
        state = "valid" if report.seconds_left >= 0 else "EXPIRED"
        print(f"access key     {report.access_key}\nscope          {report.date}/{report.region}/{report.service}\nsigned headers {';'.join(report.signed_headers)}")
        print(f"valid          {iso8601(report.valid_from)} .. {iso8601(report.valid_until)} ({report.expires}s): {state}, {int(report.seconds_left)}s left")
        if report.verified is not None:
            print(f"signature      {'matches' if report.verified else 'DOES NOT MATCH'} the secret for {args.method}")
            if not report.verified:
                print("# canonical request\n" + report.canonical_request + "\n# string to sign\n" + report.string_to_sign)
    if report.verified is False or report.seconds_left < 0:
        return 1
    return 0


def cmd_vectors(args: argparse.Namespace) -> int:
    results = [result for path in args.path for vector in load_vectors(path) for result in run_vector(vector)]
    failed = [r for r in results if not r.passed]
    if args.json:
        print(json.dumps({"total": len(results), "passed": len(results) - len(failed), "failed": [r.__dict__ for r in failed]}, indent=2))
    else:
        for result in results:
            if not result.passed or args.verbose:
                print(f"{'PASS' if result.passed else 'FAIL'} {result.name} ({result.mode}){'' if result.passed else ': ' + ', '.join(result.mismatches)}")
            if not result.passed:
                print("  our canonical request:\n    " + result.canonical_request.replace("\n", "\n    "))
        print(f"{len(results) - len(failed)}/{len(results)} signatures match the published vectors")
    return 1 if failed or not results else 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="minis3", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    serve = sub.add_parser("serve", help="run the S3-compatible server")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=9000, help="0 picks a free port")
    serve.add_argument("--port-file", help="write the bound port here once listening")
    serve.add_argument("--region", default=os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION") or "us-east-1")
    serve.add_argument("--key", action="append", help="ACCESS_KEY:SECRET_KEY[:PRINCIPAL_ARN], repeatable")
    serve.add_argument("--min-part-size", type=int, default=MIN_PART_SIZE, help="smallest allowed multipart part except the last")
    serve.add_argument("--quiet", action="store_true")

    creds = argparse.ArgumentParser(add_help=False)
    creds.add_argument("--access-key", help="defaults to AWS_ACCESS_KEY_ID")
    creds.add_argument("--secret-key", help="defaults to AWS_SECRET_ACCESS_KEY")
    creds.add_argument("--region", default=os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION") or "us-east-1")
    creds.add_argument("--date", help="sign as if it were this time (YYYYMMDDTHHMMSSZ)")
    creds.add_argument("--header", "-H", action="append", help="'Name: value', repeatable")

    pre = sub.add_parser("presign", parents=[creds], help="print a presigned URL")
    pre.add_argument("url", help="http://HOST:PORT/BUCKET/KEY")
    pre.add_argument("--method", default="GET")
    pre.add_argument("--expires", type=int, default=3600, help="seconds, 1 to 604800")

    sign = sub.add_parser("sign", parents=[creds], help="show the canonical request, string to sign and headers for a request")
    sign.add_argument("method")
    sign.add_argument("url")
    sign.add_argument("--data-file", help="request body (- for stdin)")
    sign.add_argument("--unsigned-payload", action="store_true", help="x-amz-content-sha256: UNSIGNED-PAYLOAD")
    sign.add_argument("--json", action="store_true")

    send = sub.add_parser("send", parents=[creds], help="sign a request and send it")
    send.add_argument("method")
    send.add_argument("url")
    send.add_argument("--data-file", help="request body (- for stdin)")
    send.add_argument("--unsigned-payload", action="store_true")
    send.add_argument("--anonymous", action="store_true", help="send without signing")
    send.add_argument("--output", "-o", help="write the response body here")
    send.add_argument("--json", action="store_true")

    ins = sub.add_parser("inspect", help="decode a presigned URL and optionally verify it with the secret")
    ins.add_argument("url")
    ins.add_argument("--secret-key", help="recompute the signature with this secret")
    ins.add_argument("--method", default="GET")
    ins.add_argument("--date", help="evaluate expiry as of this time (YYYYMMDDTHHMMSSZ)")
    ins.add_argument("--json", action="store_true")

    vec = sub.add_parser("vectors", help="check the signer against SigV4 test vectors")
    vec.add_argument("path", nargs="+", help="a fixtures JSON file or a directory in the aws-c-auth test-suite layout")
    vec.add_argument("--verbose", action="store_true")
    vec.add_argument("--json", action="store_true")
    return parser


COMMANDS: Dict[str, Callable[[argparse.Namespace], int]] = {
    "serve": cmd_serve,
    "presign": cmd_presign,
    "sign": cmd_sign,
    "send": cmd_send,
    "inspect": cmd_inspect,
    "vectors": cmd_vectors,
}


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    try:
        args = parser.parse_args(list(sys.argv[1:] if argv is None else argv))
    except SystemExit as exc:
        return int(exc.code or 0)
    try:
        return COMMANDS[args.command](args)
    except ClientError as exc:
        print(f"minis3: {exc}", file=sys.stderr)
        return 1
    except (OSError, ValueError, KeyError) as exc:
        print(f"minis3: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
