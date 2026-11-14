"""A small AWS API client for the grader: SigV4-signed requests to a local moto.

moto routes a request to a service by the credential scope in the Authorization
header, so every request carries a well-formed Signature Version 4 header even
though moto does not verify it. The client refuses any endpoint that is not on
this machine.
"""
from __future__ import annotations

import datetime
import hashlib
import hmac
import json
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from typing import Any, Callable, Dict, List, Optional, Tuple

REGION = "eu-central-1"
ACCESS_KEY = "testing"
SECRET_KEY = "testing"
LOCAL_HOSTS = ("127.0.0.1", "localhost", "::1")
EC2_VERSION = "2016-11-15"
IAM_VERSION = "2010-05-08"
LIST_TAGS = ("item", "member")

Opener = Callable[[urllib.request.Request, float], Tuple[int, Dict[str, str], bytes]]


class EndpointError(Exception):
    pass


class AwsError(Exception):
    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(f"{status} {code}: {message}")
        self.status = status
        self.code = code
        self.message = message


def require_local(endpoint: str) -> str:
    parsed = urllib.parse.urlparse(endpoint.strip())
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise EndpointError(f"LZ_AWS_ENDPOINT={endpoint!r} is not a URL like http://127.0.0.1:5000")
    if parsed.hostname not in LOCAL_HOSTS:
        raise EndpointError(f"LZ_AWS_ENDPOINT={endpoint!r} is not local: the grader only talks to moto on 127.0.0.1 or localhost, never to a real AWS endpoint")
    return f"{parsed.scheme}://{parsed.netloc}"


def _hmac(key: bytes, text: str) -> bytes:
    return hmac.new(key, text.encode("utf-8"), hashlib.sha256).digest()


def signing_key(secret: str, day: str, region: str, service: str) -> bytes:
    key = _hmac(("AWS4" + secret).encode("utf-8"), day)
    for part in (region, service, "aws4_request"):
        key = _hmac(key, part)
    return key


def canonical_query(query: str) -> str:
    pairs = urllib.parse.parse_qsl(query, keep_blank_values=True)
    return "&".join(f"{urllib.parse.quote(k, safe='-_.~')}={urllib.parse.quote(v, safe='-_.~')}" for k, v in sorted(pairs))


def sign(method: str, url: str, headers: Dict[str, str], body: bytes, service: str, region: str = REGION,
         now: Optional[datetime.datetime] = None, access_key: str = ACCESS_KEY, secret_key: str = SECRET_KEY) -> Dict[str, str]:
    moment = now or datetime.datetime.now(datetime.timezone.utc)
    stamp = moment.strftime("%Y%m%dT%H%M%SZ")
    day = stamp[:8]
    parsed = urllib.parse.urlparse(url)
    payload_hash = hashlib.sha256(body).hexdigest()
    signed = {k.lower(): " ".join(str(v).split()) for k, v in headers.items()}
    signed["host"] = parsed.netloc
    signed["x-amz-date"] = stamp
    signed["x-amz-content-sha256"] = payload_hash
    names = sorted(signed)
    canonical = "\n".join([
        method,
        urllib.parse.quote(parsed.path or "/", safe="/-_.~"),
        canonical_query(parsed.query),
        "".join(f"{name}:{signed[name]}\n" for name in names),
        ";".join(names),
        payload_hash,
    ])
    scope = f"{day}/{region}/{service}/aws4_request"
    to_sign = "\n".join(["AWS4-HMAC-SHA256", stamp, scope, hashlib.sha256(canonical.encode("utf-8")).hexdigest()])
    signature = hmac.new(signing_key(secret_key, day, region, service), to_sign.encode("utf-8"), hashlib.sha256).hexdigest()
    out = dict(headers)
    out["X-Amz-Date"] = stamp
    out["X-Amz-Content-Sha256"] = payload_hash
    out["Authorization"] = f"AWS4-HMAC-SHA256 Credential={access_key}/{scope}, SignedHeaders={';'.join(names)}, Signature={signature}"
    return out


def urlopen(request: urllib.request.Request, timeout: float) -> Tuple[int, Dict[str, str], bytes]:
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, dict(response.headers), response.read()
    except urllib.error.HTTPError as error:
        return error.code, dict(error.headers or {}), error.read()


def local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def xml_value(element: ET.Element) -> Any:
    children = list(element)
    if not children:
        return element.text if element.text is not None and element.text.strip() != "" else None
    names = [local_name(child.tag) for child in children]
    if all(name in LIST_TAGS for name in names):
        return [xml_value(child) for child in children]
    result: Dict[str, Any] = {}
    for name, child in zip(names, children):
        value = xml_value(child)
        if names.count(name) > 1:
            result.setdefault(name, []).append(value)
        else:
            result[name] = value
    return result


def as_list(value: Any) -> List[Any]:
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def tag_map(tags: Any) -> Dict[str, str]:
    result = {}
    for tag in as_list(tags):
        if isinstance(tag, dict):
            key = tag.get("key", tag.get("Key"))
            if key is not None:
                result[str(key)] = str(tag.get("value", tag.get("Value")) or "")
    return result


def error_of(status: int, body: bytes) -> AwsError:
    text = body.decode("utf-8", "replace")
    try:
        root = ET.fromstring(text)
        code = next((e.text for e in root.iter() if local_name(e.tag) == "Code"), "") or ""
        message = next((e.text for e in root.iter() if local_name(e.tag) == "Message"), "") or ""
        return AwsError(status, code, message or text[:300])
    except ET.ParseError:
        try:
            data = json.loads(text)
            return AwsError(status, str(data.get("__type", "")).rsplit("#", 1)[-1], str(data.get("message", data.get("Message", ""))))
        except ValueError:
            return AwsError(status, "", text[:300])


class Client:
    def __init__(self, endpoint: str, region: str = REGION, opener: Opener = urlopen, timeout: float = 30.0) -> None:
        self.endpoint = require_local(endpoint)
        self.region = region
        self.opener = opener
        self.timeout = timeout

    def call(self, method: str, service: str, path: str = "/", query: str = "", body: bytes = b"", headers: Optional[Dict[str, str]] = None) -> Tuple[int, Dict[str, str], bytes]:
        url = self.endpoint + path + (("?" + query) if query else "")
        headers = dict(headers or {})
        if body and not any(k.lower() == "content-type" for k in headers):
            headers["Content-Type"] = "application/octet-stream"
        signed = sign(method, url, headers, body, service, self.region)
        request = urllib.request.Request(url, data=body if method in ("POST", "PUT") else None, headers=signed, method=method)
        return self.opener(request, self.timeout)

    def query(self, service: str, action: str, version: str, params: Optional[Dict[str, str]] = None) -> Any:
        form = {"Action": action, "Version": version}
        form.update(params or {})
        body = urllib.parse.urlencode(form).encode("utf-8")
        status, _, data = self.call("POST", service, body=body, headers={"Content-Type": "application/x-www-form-urlencoded; charset=utf-8"})
        if status >= 300:
            raise error_of(status, data)
        return xml_value(ET.fromstring(data))

    def ec2(self, action: str, params: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
        return self.query("ec2", action, EC2_VERSION, params) or {}

    def iam(self, action: str, params: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
        result = self.query("iam", action, IAM_VERSION, params) or {}
        return result.get(f"{action}Result") or {}

    def ec2_items(self, action: str, key: str, filters: Optional[Dict[str, str]] = None) -> List[Dict[str, Any]]:
        params: Dict[str, str] = {}
        for index, (name, value) in enumerate(sorted((filters or {}).items()), 1):
            params[f"Filter.{index}.Name"] = name
            params[f"Filter.{index}.Value.1"] = value
        return [item for item in as_list(self.ec2(action, params).get(key)) if isinstance(item, dict)]

    def s3_buckets(self) -> List[str]:
        status, _, data = self.call("GET", "s3", "/")
        if status >= 300:
            raise error_of(status, data)
        root = ET.fromstring(data)
        return [e.text or "" for e in root.iter() if local_name(e.tag) == "Name"]

    def s3_keys(self, bucket: str) -> List[str]:
        status, _, data = self.call("GET", "s3", "/" + urllib.parse.quote(bucket), query="list-type=2")
        if status >= 300:
            raise error_of(status, data)
        root = ET.fromstring(data)
        return [e.text or "" for e in root.iter() if local_name(e.tag) == "Key"]

    def dynamodb(self, operation: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        body = json.dumps(payload).encode("utf-8")
        headers = {"Content-Type": "application/x-amz-json-1.0", "X-Amz-Target": f"DynamoDB_20120810.{operation}"}
        status, _, data = self.call("POST", "dynamodb", body=body, headers=headers)
        if status >= 300:
            raise error_of(status, data)
        return json.loads(data.decode("utf-8") or "{}")

    def moto_reset(self) -> None:
        status, _, data = self.call("POST", "s3", "/moto-api/reset")
        if status >= 300:
            raise AwsError(status, "MotoReset", data.decode("utf-8", "replace")[:200])

    def is_moto(self) -> bool:
        try:
            status, _, _ = self.call("GET", "s3", "/moto-api/data.json")
        except OSError:
            return False
        return status == 200
