from __future__ import annotations

import datetime
import hashlib
import hmac
import urllib.error
import urllib.parse
import urllib.request
from typing import Dict, Optional, Tuple

LOCAL_HOSTS = ("127.0.0.1", "localhost", "::1")
FAKE_KEY = "testing"


class EndpointError(Exception):
    pass


def local_endpoint(value: str) -> str:
    parsed = urllib.parse.urlparse(value.strip())
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise EndpointError(f"LZ_AWS_ENDPOINT={value!r} is not a URL such as http://127.0.0.1:5000")
    if parsed.hostname not in LOCAL_HOSTS:
        raise EndpointError(f"LZ_AWS_ENDPOINT={value!r} is not on this machine; lz only overrides endpoints for a local emulator. Unset it to use a real account.")
    return f"{parsed.scheme}://{parsed.netloc}"


def _mac(key: bytes, text: str) -> bytes:
    return hmac.new(key, text.encode("utf-8"), hashlib.sha256).digest()


def authorization(method: str, url: str, body: bytes, region: str, service: str, now: datetime.datetime, key: str = FAKE_KEY, secret: str = FAKE_KEY) -> Dict[str, str]:
    stamp = now.strftime("%Y%m%dT%H%M%SZ")
    day = stamp[:8]
    parsed = urllib.parse.urlparse(url)
    payload = hashlib.sha256(body).hexdigest()
    headers = {"host": parsed.netloc, "x-amz-content-sha256": payload, "x-amz-date": stamp}
    names = sorted(headers)
    canonical = "\n".join([method, parsed.path or "/", parsed.query, "".join(f"{n}:{headers[n]}\n" for n in names), ";".join(names), payload])
    scope = f"{day}/{region}/{service}/aws4_request"
    text = "\n".join(["AWS4-HMAC-SHA256", stamp, scope, hashlib.sha256(canonical.encode("utf-8")).hexdigest()])
    signing = _mac(("AWS4" + secret).encode("utf-8"), day)
    for part in (region, service, "aws4_request"):
        signing = _mac(signing, part)
    signature = hmac.new(signing, text.encode("utf-8"), hashlib.sha256).hexdigest()
    return {
        "X-Amz-Date": stamp,
        "X-Amz-Content-Sha256": payload,
        "Authorization": f"AWS4-HMAC-SHA256 Credential={key}/{scope}, SignedHeaders={';'.join(names)}, Signature={signature}",
    }


def send(method: str, url: str, body: bytes, region: str, service: str, now: Optional[datetime.datetime] = None) -> Tuple[int, bytes]:
    headers = authorization(method, url, body, region, service, now or datetime.datetime.now(datetime.timezone.utc))
    headers["Content-Type"] = "application/xml"
    request = urllib.request.Request(url, data=body if method == "PUT" else None, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as error:
        return error.code, error.read()


def location_body(region: str) -> bytes:
    if region == "us-east-1":
        return b""
    return (
        '<CreateBucketConfiguration xmlns="http://s3.amazonaws.com/doc/2006-03-01/">'
        f"<LocationConstraint>{region}</LocationConstraint></CreateBucketConfiguration>"
    ).encode("utf-8")


def ensure_bucket(endpoint: str, bucket: str, region: str) -> str:
    url = f"{endpoint}/{urllib.parse.quote(bucket)}"
    status, _ = send("HEAD", url, b"", region, "s3")
    if status == 200:
        return "exists"
    status, body = send("PUT", url, location_body(region), region, "s3")
    if status in (200, 409):
        return "created" if status == 200 else "exists"
    raise EndpointError(f"could not create state bucket {bucket} on {endpoint}: HTTP {status} {body[:200]!r}")
