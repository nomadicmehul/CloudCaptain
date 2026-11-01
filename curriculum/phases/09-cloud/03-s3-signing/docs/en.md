# An S3 Request Is a Signed String

*Every S3 request carries an HMAC of its own canonical form; rebuild that string on both sides and SignatureDoesNotMatch says what changed.*

## Metadata

| Field | Value |
|---|---|
| Type | Build |
| Languages | Python |
| Prerequisites | [Every AWS API Call Is a Policy Evaluation](https://cloudcaptain.io/learn/cloud/iam-policy-evaluator) |
| Time | 90 minutes |
| Tools | aws, moto, curl |

<Quiz stage="pre" />

## The Problem

Tuesday, 09:15. The expenses app uploads receipt photos straight to S3: the mobile client asks a backend function for a presigned PUT URL, then sends the photo to that URL. Since Monday's release about a third of uploads fail with `403 SignatureDoesNotMatch`, and the rest work. The on-call engineer rotates the access key behind the function, which changes nothing. Someone raises `X-Amz-Expires` from 15 minutes to 7 days, in case the URLs "expire too early"; the failure rate stays the same and every leaked URL now works for a week. Someone else widens the bucket policy to `s3:*` for the function's role. Still a third. A pull request to make the bucket public-write is open by lunch.

The answer was in the response body nobody opened. S3 returns its own `<CanonicalRequest>` and `<StringToSign>` with every SignatureDoesNotMatch. For the failing uploads the path in it read `/receipts/2026/Taxi%2BHotel.jpg`, while the backend had signed `/receipts/2026/Taxi%20Hotel.jpg`: the release had added a step that turned spaces in file names into `+` before the URL went to the phone, and only file names with a space failed. A signature is an HMAC over a precise text rendering of the request. Change one byte of that text (the path, a query parameter, a signed header, the method, the date, the region) and the server's HMAC no longer matches, no matter how valid the key or how generous the policy. Keys, expiry and policy are checked separately, and each has its own error code.

Every S3 client in every language does the same thing: build the canonical request, hash it into a string to sign, derive a key that only works for one day, one region and one service, and HMAC the string with it. You are going to build `minis3`: AWS Signature Version 4 from the specification, checked against AWS's own published test vectors; a client that signs requests and presigns URLs; and a small S3-compatible server that verifies those signatures and implements objects, listings with delimiters, versioning, multipart ETags and a bucket policy check. The real AWS CLI then talks to it and to moto, the AWS emulator, and the two agree.

## The Concept

Think of a notary sealing a description of a document instead of the document itself. Both parties write down the same description independently, following a strict form: the title in capitals, the pages in order, every amount in one format. The notary's seal goes on the description. Anyone holding the seal key can check it by rewriting the description from the document in front of them and resealing it. If the two seals differ, either the document changed or one of them filled in the form differently, and comparing the two descriptions line by line shows which.

SigV4 is that form, with HMAC-SHA256 as the seal. The **canonical request** is six lines: the HTTP method; the **canonical URI** (the path, each byte outside `A-Z a-z 0-9 - _ . ~` written as `%XX`, slashes kept); the **canonical query string** (every parameter name and value encoded the same way, then sorted, `name=` for a parameter with no value); the **canonical headers** (the signed headers in lowercase, values trimmed with runs of spaces collapsed, sorted, repeats joined with commas); the list of signed header names; and the **payload hash** (the hex SHA-256 of the body, or the literal `UNSIGNED-PAYLOAD`). S3 requires `host` and every `x-amz-*` header the request carries to be among the signed headers. S3 differs from every other AWS service in one place: it signs the object key exactly as given, encoded once, while other services normalize the path (`.`, `..`, double slashes) and encode it twice. That is why a key named `a/../b` is a real, distinct S3 object.

The **string to sign** is the algorithm name, the timestamp, the **credential scope** (`20261101/us-east-1/s3/aws4_request`) and the SHA-256 of the canonical request. The **signing key** is a chain of HMACs: the secret (prefixed with `AWS4`) signs the date, that result signs the region, that result signs the service, that result signs `aws4_request`. The secret never travels and never signs a request directly; a derived key is good for one day, one region and one service, so the server can check the scope before doing any cryptography. The signature is the hex HMAC of the string to sign under the signing key.

The signature goes in one of two places. In the **Authorization header** it travels with `X-Amz-Date` and `x-amz-content-sha256`, and the server allows 15 minutes of clock difference. In a **presigned URL** the algorithm, credential, date, expiry and signed header list become query parameters (`X-Amz-Algorithm`, `X-Amz-Credential`, `X-Amz-Date`, `X-Amz-Expires`, `X-Amz-SignedHeaders`), are themselves part of the canonical query string, and `X-Amz-Signature` is appended last. S3 presigned URLs sign `UNSIGNED-PAYLOAD`, so the URL does not fix the bytes uploaded to it; it fixes the method, bucket, key, signed headers and lifetime. A presigned URL is a bearer capability: whoever holds it acts as the signer, until it expires or until the signer's credentials do.

```mermaid
flowchart LR
  REQ["request: method, path, query, headers, body"] --> CR["canonical request (6 lines)"]
  CR --> STS["string to sign: algorithm, date, scope, sha256(canonical)"]
  KEY["secret: HMAC chain date, region, s3, aws4_request"] --> SIG["signature = HMAC(signing key, string to sign)"]
  STS --> SIG
  SIG --> WIRE["Authorization header or X-Amz-Signature query parameter"]
  WIRE --> SRV["server rebuilds the canonical request from what arrived"]
  SRV --> CMP{"same HMAC?"}
  CMP -->|no| ERR["403 SignatureDoesNotMatch with CanonicalRequest and StringToSign"]
  CMP -->|yes| POL["bucket policy: explicit deny, allow, same account"]
  POL --> OP["operation: PUT, GET, LIST, versions, multipart"]
```

Authentication only says who sent the request. Authorization is the evaluation from the previous lesson, run with the principal the signature proved: an explicit Deny in the bucket policy wins, a principal in the bucket owner's account needs an Allow in its identity policy or in the bucket policy (`minis3` models every key as an administrator in its own account), and a principal from another account or an anonymous caller needs an Allow in the bucket policy. The bucket policy can see how the request was authenticated (`s3:authType`, `s3:signatureAge`), which is how a bucket refuses presigned URLs or old signatures. Only then does the operation run.

## Build It

`code/main.py` is `minis3`: the signer, an `S3Service` over an in-memory `Store`, an HTTP handler, an `S3Client`, and a CLI. The tests start the server on a free port in a thread and drive it over real HTTP. `code/fixtures/aws-sigv4-test-suite.json` is AWS's SigV4 test suite, copied verbatim from the `awslabs/aws-c-auth` repository (the signer behind the AWS Common Runtime SDKs, Apache-2.0): 38 requests, each with the expected canonical request, string to sign and signature for both header and query signing. `code/fixtures/s3-api-examples.json` holds the five worked examples from the S3 API Reference (GET with a Range header, PUT, `?lifecycle`, a listing, and a presigned GET).

### Step 1: encode exactly the way AWS does

On Python 3.7 and later `urllib.parse.quote(text, safe="/")` happens to give the same result, but many encoders do not: `urlencode` and HTML forms write a space as `+`, and JavaScript's `encodeURIComponent` leaves `!'()*` alone. SigV4 defines the rule itself, so `minis3` spells it out on bytes:

```python
def uri_encode(text: str, keep_slash: bool = False) -> str:
    out = []
    for byte in text.encode("utf-8"):
        if byte in UNRESERVED or (keep_slash and byte == 0x2F):
            out.append(chr(byte))
        else:
            out.append(f"%{byte:02X}")
    return "".join(out)


def canonical_uri(path: str, normalize: bool = False) -> str:
    path = path or "/"
    return uri_encode(normalize_path(path) if normalize else path, keep_slash=True)
```

A space is `%20`, never `+`; hex digits are uppercase; a multi-byte character is one escape per UTF-8 byte (`ሴ` is `%E1%88%B4`). Paths keep their slashes, query values do not, so a key `a/b` in a query parameter is `a%2Fb`. `normalize` is off for S3; the test suite's `-normalized` vectors switch it on, and `test_s3_signs_the_key_as_given_while_other_services_normalize_the_path` shows `/bucket//a/./b/../c` staying as it is.

### Step 2: the query and the headers in canonical order

```python
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
```

Sorting happens after encoding, so `Zeta` sorts before `max-keys` (uppercase bytes come first) and `%E1%88%B4` before `Param`. A subresource such as `?uploads` or `?versioning` becomes `uploads=`. Header names are compared in lowercase, a header sent three times becomes one line with three comma-separated values in the order sent, and `"a   b   c"` becomes `"a b c"`, quotes or not. Headers that are not in the signed list are ignored entirely, which is why a proxy may add `User-Agent` or `Via` but not change `Host`.

### Step 3: canonical request, string to sign, signing key

```python
def canonical_request(method: str, path: str, query: Sequence[Tuple[str, str]], headers: Sequence[Tuple[str, str]], signed_headers: Sequence[str], payload_hash: str, normalize: bool = False) -> str:
    signed = sorted({name.lower() for name in signed_headers})
    return "\n".join([method.upper(), canonical_uri(path, normalize), canonical_query(query), canonical_headers(headers, signed), ";".join(signed), payload_hash])


def string_to_sign(amz_date: str, scope: str, creq: str) -> str:
    return "\n".join([ALGORITHM, amz_date, scope, sha256_hex(creq.encode("utf-8"))])


def signing_key(secret: str, date: str, region: str, service: str) -> bytes:
    key = hmac_sha256(("AWS4" + secret).encode("utf-8"), date)
    for part in (region, service, TERMINATOR):
        key = hmac_sha256(key, part)
    return key
```

Because every canonical header line ends in a newline, joining the six parts produces the blank line between the headers and the signed header list that you will see in every SignatureDoesNotMatch body. The string to sign never contains the canonical request itself, only its hash, which is why S3 sends you both.

### Step 4: two placements of the same signature

```python
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
```

The date, the payload hash and a session token are headers like any other, so they are signed. A presigned URL moves the same fields into the query before signing, so they are covered too, and only the signature itself is added afterwards:

```python
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
```

`presign()` refuses an expiry outside 1 to 604800 seconds (7 days), the same limit S3 enforces, and defaults the payload hash to `UNSIGNED-PAYLOAD`.

### Step 5: prove it with AWS's test vectors

`run_vector()` parses each suite request (`request.txt`, including a header folded over three lines and raw UTF-8 in the path), signs it in both modes with the suite's fixed credentials and timestamp (`AKIDEXAMPLE`, 2015-08-30 12:36:00 UTC, service `service`), and compares every expected file: canonical request, string to sign, signature and the final `Authorization` header or URL. The suite also covers session tokens signed before and added after signing (`post-sts-header-before`, `post-sts-header-after`) and a signed form body (`post-x-www-form-urlencoded`).

```bash
python3 code/main.py vectors code/fixtures/aws-sigv4-test-suite.json code/fixtures/s3-api-examples.json
# 81/81 signatures match the published vectors
python3 code/main.py vectors code/fixtures/s3-api-examples.json --verbose
# PASS s3-get-object-range (header) ... PASS s3-presigned-get (query) ...
```

38 vectors times two modes is 76 checks, plus the five S3 examples, whose `examplebucket` presigned GET expects signature `aeeed9bb...d404` over `UNSIGNED-PAYLOAD`. On a failure, `vectors` prints your canonical request, which you diff against the expected file. `vectors` also accepts a directory in the suite's own layout, so a fresh `git clone` of `aws-c-auth` can be pointed at it directly.

### Step 6: the server rebuilds the string from what arrived

`authenticate()` picks the scheme (an `Authorization` header starting with `AWS4-HMAC-SHA256`, or `X-Amz-Algorithm` in the query; both at once is `InvalidArgument`, and the retired SigV2 form `AWS AKID:sig` is refused with S3's `Please use AWS4-HMAC-SHA256`). Then:

```python
def verify_header_auth(req: Incoming, fields: Dict[str, str], keys: Dict[str, Key], region: str, now: float) -> Identity:
    stamp = req.header("x-amz-date") or ""
    when = stamp_time(stamp)
    key, scope = check_scope(fields["Credential"], stamp, keys, region, "AuthorizationHeaderMalformed")
    if abs(now - when) > MAX_SKEW_SECONDS:
        raise S3Error(403, "RequestTimeTooSkewed", "The difference between the request time and the current time is too large.", RequestTime=stamp, ServerTime=iso8601(now), MaxAllowedSkewMilliseconds=MAX_SKEW_SECONDS * 1000)
    signed = fields["SignedHeaders"].split(";")
    require_signed(req, signed)
```

The order is the triage order. An unknown access key is `InvalidAccessKeyId`. A scope with the wrong region is `400 AuthorizationHeaderMalformed`, with `<Region>` naming the right one. A clock more than 15 minutes off is `RequestTimeTooSkewed`. An `x-amz-*` header that is not in `SignedHeaders` is `AccessDenied` with `<HeadersNotSigned>`. Only then is the HMAC compared, with `hmac.compare_digest`, and a mismatch returns S3's error with the server's `CanonicalRequest` and `StringToSign`. Presigned requests follow the same path with three extra checks: all six `X-Amz-*` parameters present, `X-Amz-Expires` at most 604800, and `now` before `X-Amz-Date + X-Amz-Expires`, otherwise `AccessDenied: Request has expired`. After a valid signature, `payload_body()` checks the body itself: a hex `x-amz-content-sha256` that does not match the bytes is `XAmzContentSHA256Mismatch`, a wrong `Content-MD5` or `x-amz-checksum-crc32` is `BadDigest`, and an `aws-chunked` body (`STREAMING-UNSIGNED-PAYLOAD-TRAILER`, what SDKs send for uploads over TLS) is decoded and its trailing checksum verified.

### Step 7: keys, prefixes and ETags

S3 has no directories. A bucket is a sorted map from key to object, and a "folder" is a prefix plus a delimiter applied at list time:

```python
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
```

Every key under `photos/` with delimiter `/` collapses into one `CommonPrefixes` entry, and that entry counts once against `max-keys`, so a page of 1000 can stand for millions of keys. Keys with the same prefix are contiguous in sorted order, which makes the roll-up a single pass, and the continuation token remembers whether the last item was a prefix so the next page skips everything under it. `photos-old.txt` sorts before `photos/`, because `-` is byte 0x2D and `/` is 0x2F. `encoding-type=url`, which the AWS CLI always sends, makes the server percent-encode keys in the XML.

The ETag of a single PUT is the MD5 of the bytes. A multipart upload has no single MD5, so S3 stores the MD5 of the concatenated binary MD5s of the parts, followed by `-` and the part count:

```python
def multipart_etag(part_etags: Sequence[str]) -> str:
    digests = b"".join(binascii.unhexlify(etag.strip('"')) for etag in part_etags)
    return f'"{hashlib.md5(digests).hexdigest()}-{len(part_etags)}"'
```

The same file uploaded with different part sizes gets a different ETag, which is why comparing an ETag with `md5sum` only works for single-part, unencrypted-with-KMS objects. Every part except the last must be at least 5 MiB, enforced at CompleteMultipartUpload as `EntityTooSmall` (`serve --min-part-size` lowers it for experiments, and the tests use 1 KiB).

### Step 8: versions and delete markers

A versioned key is a list. PUT appends a version with a new ID; DELETE without a version ID appends a **delete marker**, a version with no data that makes GET answer `404 NoSuchKey` with `x-amz-delete-marker: true`; DELETE with a version ID removes that version for good, and removing the marker brings the object back:

```python
if not bucket.versioning:
    bucket.objects.pop(key, None)
    return "", False
marker = ObjectVersion(key, self.new_version_id(bucket), b"", "", self.clock(), True)
self.store_version(bucket, marker)
return marker.version_id, True
```

A bucket is unversioned until versioning is first enabled, and can then only be `Enabled` or `Suspended`, never unversioned again. Suspended writes the version ID `null`, and `store_version()` replaces any earlier `null` version, so older real versions survive while new writes overwrite each other.

### Step 9: the bucket policy, the evaluator idea again

```python
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
```

`statement_matches()` is a compact version of the previous lesson's evaluator: `Principal` (`*`, an ARN, an account ID or its `:root` ARN), `Action`/`NotAction` with case-insensitive wildcards, `Resource`/`NotResource`, and conditions (`StringEquals`, `StringLike`, `Numeric*`, `Bool`, `IpAddress`, the negated forms and `...IfExists`). The request context carries `aws:SourceIp`, `aws:SecureTransport`, `aws:PrincipalArn`, `s3:prefix` for listings, `s3:authType` (`REST-HEADER` or `REST-QUERY-STRING`) and, for presigned URLs only, `s3:signatureAge` in milliseconds. Two S3 rules are built in: the owner account's root user can always get, put and delete the bucket policy, so a Deny-everything policy cannot lock the account out; and only same-account callers get the detailed AccessDenied message (`with an explicit deny in a resource-based policy`, plus the statement in `minis3`), while other accounts and anonymous callers get `Access Denied`.

### Step 10: run it

```bash
export AWS_ACCESS_KEY_ID=AKIAIOSFODNN7EXAMPLE AWS_SECRET_ACCESS_KEY=wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY
python3 code/main.py serve --port 9000 --key "$AWS_ACCESS_KEY_ID:$AWS_SECRET_ACCESS_KEY:arn:aws:iam::111122223333:user/alice" &
E=http://127.0.0.1:9000
python3 code/main.py send PUT $E/shop-assets                                         # HTTP 200: CreateBucket
python3 code/main.py send PUT "$E/shop-assets/docs/read me.txt" --data-file lesson.json  # ETag: the MD5 of the file
python3 code/main.py sign GET "$E/shop-assets/docs/read me.txt"                      # the canonical request, string to sign and headers
U=$(python3 code/main.py presign "$E/shop-assets/docs/read me.txt" --expires 300)
curl -s "$U"                                                                         # the object, no credentials needed
python3 code/main.py inspect "$U" --secret-key "$AWS_SECRET_ACCESS_KEY"              # scope, expiry, signature matches
curl -s "${U%?}0"                                                                    # one changed byte: SignatureDoesNotMatch
python3 code/main.py send GET "$E/shop-assets?list-type=2" --region eu-west-1        # 400 AuthorizationHeaderMalformed, expecting 'us-east-1'
kill %1
```

The tampered URL answers with the evidence:

```text
<Error><Code>SignatureDoesNotMatch</Code><Message>The request signature we calculated does not match the signature you provided. ...</Message>
<StringToSign>AWS4-HMAC-SHA256
20261101T201212Z
20261101/us-east-1/s3/aws4_request
f71928ba59c1efed0c47028933e982d2fb93b27a414ecc3671164e7cd57a0729</StringToSign>
<SignatureProvided>23a54d5d...b060</SignatureProvided>
<CanonicalRequest>GET
/shop-assets/docs/read%20me.txt
X-Amz-Algorithm=AWS4-HMAC-SHA256&amp;X-Amz-Credential=AKIAIOSFODNN7EXAMPLE%2F20261101%2Fus-east-1%2Fs3%2Faws4_request&amp;...
host:127.0.0.1:9000

host
UNSIGNED-PAYLOAD</CanonicalRequest>
```

Exit codes: 0 for success, 1 for an HTTP error, an expired or unverified URL, or a failed vector, 2 for bad arguments.

<RunTests />

## Use It

Run the same jobs with the real AWS CLI against two servers: moto, the open-source AWS emulator, in server mode, and `minis3`. There is no AWS account involved; both accept any key you configure. Pin moto and give the CLI a config that uses path-style URLs, which is what both emulators expect on `127.0.0.1`:

```bash
python3 -m venv .venv && .venv/bin/pip install 'moto[server]==5.2.5'
.venv/bin/moto_server -H 127.0.0.1 -p 5000 &
python3 code/main.py serve --port 9000 --key "AKIACCSMOKE00000001:local-secret:arn:aws:iam::123456789012:user/smoke" &
export AWS_ACCESS_KEY_ID=AKIACCSMOKE00000001 AWS_SECRET_ACCESS_KEY=local-secret AWS_DEFAULT_REGION=us-east-1
cat > aws-config <<'EOF'
[default]
region = us-east-1
s3 =
  addressing_style = path
  signature_version = s3v4
EOF
export AWS_CONFIG_FILE=$PWD/aws-config
```

Now the same commands against each endpoint (`E=http://127.0.0.1:5000` for moto, `E=http://127.0.0.1:9000` for `minis3`):

```bash
aws --endpoint-url $E s3api create-bucket --bucket shop-assets
aws --endpoint-url $E s3api put-object --bucket shop-assets --key photos/2026/a.jpg --body a.jpg
aws --endpoint-url $E s3 cp big.bin s3://shop-assets/photos/2026/big.bin          # over 8 MB: CreateMultipartUpload, UploadPart x2, Complete
aws --endpoint-url $E s3api list-objects-v2 --bucket shop-assets --delimiter /     # CommonPrefixes: photos/
aws --endpoint-url $E s3api list-objects-v2 --bucket shop-assets --prefix photos/ --delimiter /
aws --endpoint-url $E s3 ls s3://shop-assets/photos/2026/                           # aws s3 ls is ListObjectsV2 with delimiter /
aws --endpoint-url $E s3api head-object --bucket shop-assets --key photos/2026/big.bin --query ETag   # "...-2" on both, the same value
aws --endpoint-url $E s3 cp s3://shop-assets/photos/2026/big.bin big.out             # ranged GETs, reassembled
```

The multipart ETag is identical on moto and `minis3` because both apply Step 7's rule to the same 8 MiB parts (the CLI's default `multipart_threshold` and `multipart_chunksize`). Versioning, the delete marker and reading an old version:

```bash
aws --endpoint-url $E s3api put-bucket-versioning --bucket shop-assets --versioning-configuration Status=Enabled
V1=$(aws --endpoint-url $E s3api put-object --bucket shop-assets --key config.json --body v1.json --query VersionId --output text)
aws --endpoint-url $E s3api put-object --bucket shop-assets --key config.json --body v2.json
aws --endpoint-url $E s3api delete-object --bucket shop-assets --key config.json           # "DeleteMarker": true
aws --endpoint-url $E s3api get-object --bucket shop-assets --key config.json out.json     # NoSuchKey
aws --endpoint-url $E s3api get-object --bucket shop-assets --key config.json --version-id $V1 out.json
aws --endpoint-url $E s3api list-object-versions --bucket shop-assets --prefix config.json # 2 Versions, 1 DeleteMarker (IsLatest)
```

Presigned URLs in both directions. `aws s3 presign` creates GET URLs only (an SDK call such as boto3's `generate_presigned_url("put_object", ...)` creates PUT URLs), and `minis3 presign --method PUT` does both:

```bash
URL=$(aws --endpoint-url http://127.0.0.1:9000 s3 presign s3://shop-assets/photos/2026/a.jpg --expires-in 300)
curl -s -o /dev/null -w '%{http_code}\n' "$URL"                          # 200: minis3 verified botocore's signature
python3 code/main.py inspect "$URL" --secret-key "$AWS_SECRET_ACCESS_KEY"
curl -s "${URL%?}0" | grep -o '<Code>[^<]*'                              # SignatureDoesNotMatch
PUT=$(python3 code/main.py presign http://127.0.0.1:5000/shop-assets/uploads/r.jpg --method PUT --expires 300)
curl -s -T receipt.jpg "$PUT"                                            # upload to moto with no credentials
```

To watch the client side of the string, add `--debug`: botocore logs `CanonicalRequest:`, `StringToSign:` and `Signature:` for every request, which is exactly what you diff against the error body. Note what the client sends over plain HTTP: `x-amz-content-sha256` is the real SHA-256 of the body, plus an `x-amz-checksum-crc32` header that current SDKs and the CLI add to uploads by default. Over HTTPS, botocore stops hashing upload bodies for the signature: it sends `STREAMING-UNSIGNED-PAYLOAD-TRAILER` and moves the checksum into an `aws-chunked` trailer, relying on TLS plus the checksum for integrity, while other requests keep a real payload hash. `minis3` decodes both upload forms. Ask with the wrong region and watch the SDK recover on its own:

```bash
aws --endpoint-url http://127.0.0.1:9000 --region eu-west-1 s3api list-objects-v2 --bucket shop-assets
```

`minis3`'s log shows `400 AuthorizationHeaderMalformed` followed by `200`: botocore read the `<Region>` element, re-signed for `us-east-1` and retried. Your own HTTP client would have stopped at the 400.

**What the emulator does and does not check.** moto parses and stores everything above, but by default it verifies no signatures, and it never verifies presigned URLs: change a byte of a presigned URL's signature and moto still answers 200. Start it with `INITIAL_NO_AUTH_ACTION_COUNT=3` and it turns on IAM-style access control after three requests: create an IAM user, an access key and a policy in those three, and from then on moto recomputes every header signature with botocore and returns SignatureDoesNotMatch on a mismatch. That is how the smoke test proves `minis3`'s header signer against an independent implementation (in that mode moto 5.2.5 answers presigned URLs with a 500). moto's bucket-policy check looks only at the action and the resource, so a public `Allow` and an explicit `Deny` behave as on S3, but the principal and the request context are never consulted: cross-account rules and conditions on `s3:authType` or `s3:signatureAge` are tested against fixtures and `minis3` only. MinIO is a self-hosted, S3-compatible server that does verify SigV4 (it runs in Docker as `minio/minio`, and `mc` is its client); check the current release and distribution status of its community edition before depending on it.

**What S3 adds that `minis3` does not.** Since December 2020 S3 has strong read-after-write consistency for PUT and DELETE of objects, so a successful PUT is visible to the next GET and LIST, and a deleted key disappears from the next listing; two simultaneous PUTs to one key still race, and the later timestamp wins. Bucket configuration is eventually consistent: AWS recommends waiting 15 minutes after enabling versioning before writing objects. Versioning keeps every overwritten and deleted version, and you pay for all of them; lifecycle rules are what expire them, and what clean up multipart uploads that were started and never completed (their parts are stored and billed, but invisible to `ls`):

```bash
cat > lifecycle.json <<'EOF'
{"Rules": [{"ID": "expire-old-versions", "Status": "Enabled", "Filter": {"Prefix": ""},
  "NoncurrentVersionExpiration": {"NoncurrentDays": 30},
  "AbortIncompleteMultipartUpload": {"DaysAfterInitiation": 7}}]}
EOF
aws --endpoint-url http://127.0.0.1:5000 s3api put-bucket-lifecycle-configuration --bucket shop-assets --lifecycle-configuration file://lifecycle.json
aws --endpoint-url http://127.0.0.1:5000 s3api get-bucket-lifecycle-configuration --bucket shop-assets
```

A presigned URL lives no longer than the credentials that signed it. AWS documents the limits: at most 7 days with the CLI or an SDK and long-term keys, 12 hours from the console, and for role or STS credentials only until the session ends, whatever `X-Amz-Expires` says. A URL signed by a Lambda function's role therefore dies with that role session. Path-style URLs (`https://s3.REGION.amazonaws.com/BUCKET/KEY`) still work: the deprecation AWS announced was postponed in September 2020, and virtual-hosted style (`https://BUCKET.s3.REGION.amazonaws.com/KEY`) is the recommended form. The host differs between the two, and `host` is always signed, so a URL signed for one style fails in the other.

Azure and Google Cloud solve the same problem with the same shape. An Azure **shared access signature** is a query string (`sv`, `sp` for permissions, `se` for expiry, `sig`) holding an HMAC-SHA256 over a canonical string-to-sign; a user delegation SAS is signed with a key obtained through Microsoft Entra ID instead of the account key, which is the one to prefer: `az storage blob generate-sas --account-name ACCOUNT --container-name receipts --name r.jpg --permissions r --expiry 2026-11-02T00:00Z --auth-mode login --as-user --full-uri`. A **GCS signed URL** uses Google's V4 signing, modelled on SigV4: `X-Goog-Algorithm` (`GOOG4-RSA-SHA256` with a service account key, or `GOOG4-HMAC-SHA256` with HMAC keys), `X-Goog-Credential` with a scope ending in `goog4_request`, `X-Goog-Date`, `X-Goog-Expires` up to 7 days, `X-Goog-SignedHeaders` and `X-Goog-Signature`: `gcloud storage sign-url gs://receipts/r.jpg --duration=10m --private-key-file=key.json`.

| What you built | Real tool | Azure / GCP | Note |
|---|---|---|---|
| `canonical_request()`, `string_to_sign()`, `signing_key()` | the SigV4 signer in every SDK and the CLI (`aws --debug` prints both strings) | SAS string-to-sign; GCS V4 canonical request | S3 encodes the path once and never normalizes it |
| `sign_with_headers()` | `Authorization: AWS4-HMAC-SHA256 ...`, `x-amz-content-sha256` | `Authorization: SharedKey`; OAuth tokens for GCS | 15 minutes of clock skew allowed |
| `presign()`, `presign --method PUT` | `aws s3 presign`, `generate_presigned_url` | `az storage blob generate-sas`; `gcloud storage sign-url` | max 7 days, and never longer than the signer's session |
| `inspect` | reading `X-Amz-Credential`, `X-Amz-Date`, `X-Amz-Expires` by hand | `se`, `sp`; `X-Goog-Expires` | check expiry and scope before blaming the key |
| `vectors` with the aws-c-auth suite | the test suites SDK signers are built against | Google publishes V4 signing samples | a failing vector shows the canonical request to diff |
| `verify_header_auth()`, `verify_query_auth()` | S3's authentication, its error codes and the `CanonicalRequest` in the body | Blob `AuthenticationFailed`; GCS `SignatureDoesNotMatch` | the server answer is the ground truth |
| `list_objects()` with delimiter | `aws s3api list-objects-v2 --delimiter /`, `aws s3 ls` | Blob list with `delimiter`; GCS `delimiter` and `prefixes` | folders are prefixes |
| `multipart_etag()` | multipart uploads above `multipart_threshold` (8 MB in the CLI) | Blob block lists; GCS composite objects with CRC32C | an ETag is an MD5 only for single-part, non-KMS objects |
| versions and delete markers | `put-bucket-versioning`, `list-object-versions` | Blob versioning and soft delete; GCS object versioning | lifecycle expires noncurrent versions |
| `authorize()` with `s3:authType`, `s3:signatureAge` | bucket policies and the S3 SigV4 condition keys | Azure RBAC and SAS policies; GCS IAM conditions | explicit deny beats the owner; the root can always fix the policy |

<Quiz stage="check" />

## Ship It

Two artifacts in `outputs/`:

- **`skill-s3-access-triage.md`**: a runbook for any 4xx from S3 or an S3-compatible endpoint. It establishes who the request really came from and where it went (account, region, endpoint style), sorts the failure by error code (InvalidAccessKeyId, AuthorizationHeaderMalformed, RequestTimeTooSkewed, SignatureDoesNotMatch, expired or malformed presigned URL, AccessDenied explicit or implicit, checksum errors), diffs the client's canonical request against the server's for signature failures, walks the policy chain for denials, and ends with a fixed report and the change that prevents a repeat.
- **`prompt-explain-signaturedoesnotmatch.md`**: paste the error XML (with `CanonicalRequest` and `StringToSign`) and, if you have it, the client's `--debug` output or the presigned URL, and get the field that differs, the mechanism that changed it (encoding, a proxy, a header, the clock, the region, the method), and the smallest fix, without ever echoing a secret.

<ShipIt />

## Exercises

### Easy

Add conditional writes: `PUT` with `If-None-Match: *` succeeds only if the key has no current version and otherwise returns `412 PreconditionFailed`; `If-Match: "ETAG"` succeeds only if the current ETag matches. This is the create-if-absent primitive the OpenTofu S3 backend uses for its lock file. Write the tests first: two clients racing to create `lock.json` (one gets 200, the other 412), an `If-Match` with a stale ETag fails, and a delete marker counts as "no current version".

### Medium

Add lifecycle rules on the store's virtual clock: `PUT ?lifecycle` with `Expiration.Days`, `NoncurrentVersionExpiration.NoncurrentDays` and `AbortIncompleteMultipartUpload.DaysAfterInitiation`, filtered by prefix, and a `Store.run_lifecycle(now)` that applies them. Tests first: a current version older than its rule gains a delete marker in a versioned bucket and disappears in an unversioned one, a noncurrent version is removed N days after it became noncurrent (not after it was created), and an incomplete upload's parts are freed. Then compare your XML with what `aws s3api get-bucket-lifecycle-configuration` prints from moto.

### Hard

Implement signed chunked uploads, `x-amz-content-sha256: STREAMING-AWS4-HMAC-SHA256-PAYLOAD`, which some SDKs use to stream a body of unknown hash. The header signature (the seed) covers the request; each chunk is sent as `HEX-SIZE;chunk-signature=SIG` and its signature is an HMAC over `AWS4-HMAC-SHA256-PAYLOAD`, the date, the scope, the previous signature, the hash of the empty string and the hash of the chunk, so the chunks form a chain. Add it to the client and the server (replace the 501 in `payload_body()`), check it against the multiple-chunks example in the S3 API Reference, and prove with tests that a changed byte in chunk three fails at chunk three, that reordering two chunks fails, and that a missing final zero-length chunk is rejected.

<Quiz stage="post" />

## Key Terms

| Term | Common misconception | Precise definition |
|---|---|---|
| Canonical request | "The raw HTTP request, hashed." | Six newline-separated lines rebuilt from the request in a fixed form: method, encoded path, sorted encoded query, signed headers in lowercase with collapsed values, the signed header list, and the payload hash. Client and server each build it; the signature only matches if every byte agrees. |
| String to sign | "The secret plus the URL." | `AWS4-HMAC-SHA256`, the timestamp, the credential scope and the SHA-256 of the canonical request, one per line. It is what the signing key HMACs; S3 returns it in every SignatureDoesNotMatch. |
| Signing key and credential scope | "The secret access key signs each request." | An HMAC chain from `AWS4` plus the secret over the date, region, service and `aws4_request`. The resulting key is valid only for that scope, so a wrong region or date fails before any signature is compared. |
| Presigned URL | "A temporary public link to an object." | A URL whose query carries the SigV4 parameters and a signature over method, bucket, key, signed headers and expiry. Anyone holding it acts as the signer until `X-Amz-Expires` passes (at most 7 days) or the signer's credentials expire, whichever is first. |
| `UNSIGNED-PAYLOAD` | "The request is not authenticated." | The payload-hash value that tells S3 the body is not covered by the signature; everything else still is. Presigned URLs use it, and SDKs use its streaming form, with a checksum trailer, for uploads over TLS. |
| SignatureDoesNotMatch | "The secret key is wrong." | The server's HMAC over its own canonical request differs from the one sent. A wrong secret is one cause; a changed path encoding, a header a proxy rewrote, a different method or a signed host that changed are the others. |
| ETag | "The MD5 of the object." | An entity tag that changes when the content changes. It is the MD5 for single-part objects stored without SSE-KMS or SSE-C; for multipart uploads it is the MD5 of the parts' MD5s plus `-N`, and depends on the part size. |
| Delete marker | "Deleting in a versioned bucket removes the object." | A data-less version that becomes the latest, so GET returns 404 while every older version stays (and is billed). Deleting the marker by version ID restores the object. |
| Common prefix | "S3 folders." | The part of a key up to and including the first delimiter after the requested prefix. A listing returns it once instead of every key below it, and it counts once against `max-keys`. |

## Further Reading

- [AWS Databases and Storage](https://cloudcaptain.io/docs/cloud/aws/databases-storage): S3 storage classes, lifecycle policies, encryption and access control, with the CLI commands used here.
- [AWS Security and IAM](https://cloudcaptain.io/docs/cloud/aws/security-iam): roles, temporary credentials and policy structure, the identities that sign every S3 request.
- [AWS CLI Cheat Sheet](https://cloudcaptain.io/docs/cloud/aws/cheatsheet): the `aws s3` and `aws s3api` commands on one page.
- [Cloud Security Fundamentals](https://cloudcaptain.io/docs/cloud/cloud-security/fundamentals): why bearer capabilities such as presigned URLs and SAS tokens need short lifetimes and narrow scope.
- [Create a signed AWS API request](https://docs.aws.amazon.com/IAM/latest/UserGuide/reference_sigv-create-signed-request.html): AWS's own description of the canonical request, string to sign and signing key you implemented.
- [aws-c-auth SigV4 test suite](https://github.com/awslabs/aws-c-auth/tree/main/tests/aws-signing-test-suite/v4): the published vectors in `code/fixtures/aws-sigv4-test-suite.json`, one folder per request, plus a SigV4a suite for multi-Region signing.
- [Amazon S3: Signature calculations for the Authorization header](https://docs.aws.amazon.com/AmazonS3/latest/API/sig-v4-header-based-auth.html): the S3-specific rules (single encoding, `x-amz-content-sha256`) and the `examplebucket` examples in `s3-api-examples.json`.
- [Amazon S3: Sharing objects with presigned URLs](https://docs.aws.amazon.com/AmazonS3/latest/userguide/ShareObjectPreSignedURL.html): who can presign, the 7-day and 12-hour limits, and why role credentials shorten every URL.
- [Amazon S3: Troubleshoot access denied (403 Forbidden) errors](https://docs.aws.amazon.com/AmazonS3/latest/userguide/troubleshoot-403-errors.html): the enhanced AccessDenied messages and how to read each policy type in them.
- [moto: server mode](https://docs.getmoto.org/en/latest/docs/server_mode.html) and [IAM-like access control](https://docs.getmoto.org/en/latest/docs/iam.html): running the emulator as a process, and the `INITIAL_NO_AUTH_ACTION_COUNT` mode in which it verifies signatures.
