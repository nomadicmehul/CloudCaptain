---
name: s3-access-triage
description: Triage 4xx errors from Amazon S3 or an S3-compatible endpoint (SignatureDoesNotMatch, AccessDenied, expired presigned URLs, clock and region errors) by error code, without widening policies or rotating keys first
version: 1.0.0
phase: cloud
lesson: cloud/s3-signing
tags: [aws, s3, sigv4, presigned-url, iam, bucket-policy, incident, troubleshooting]
---

# S3 access triage

Use this when a request to S3 (or MinIO, Ceph RGW, moto, any S3-compatible endpoint) fails with a 400 or 403: `SignatureDoesNotMatch`, `AccessDenied`, `InvalidAccessKeyId`, `AuthorizationHeaderMalformed`, `RequestTimeTooSkewed`, `Request has expired`, `XAmzContentSHA256Mismatch`, `BadDigest`. Do not rotate keys, raise `X-Amz-Expires`, add `s3:*` or make a bucket public as a first step; each error code names a different stage of the request, and only one of them is about permissions.

## 1. Capture the failing request and the full response

The response body is evidence. Get it verbatim, with the request ID headers:

```bash
aws s3api get-object --bucket BUCKET --key KEY out.bin --debug 2> debug.txt   # SDK/CLI: logs CanonicalRequest, StringToSign, the response
grep -n -A 12 'CanonicalRequest:' debug.txt | head -40
grep -n -A 5 'StringToSign:' debug.txt | head -20
curl -sS -D headers.txt "PRESIGNED_URL" -o body.xml; cat headers.txt body.xml  # presigned URL: keep x-amz-request-id and x-amz-id-2
```

Record: the error `Code` and `Message`, `x-amz-request-id`, `x-amz-id-2`, the endpoint (host, path-style or virtual-hosted, HTTP or HTTPS, a proxy or CDN in between), the client and its version, the time on the client. Never paste the secret access key, a session token or a full presigned URL into a ticket; redact `X-Amz-Signature` and `X-Amz-Security-Token`.

## 2. Establish the identity and the target

```bash
aws sts get-caller-identity                                  # which account, user or role the client's credentials belong to
aws configure list                                           # where the credentials and region come from (env, profile, IMDS)
aws s3api get-bucket-location --bucket BUCKET                # the bucket's region (null means us-east-1)
python3 code/main.py inspect "PRESIGNED_URL"                 # minis3 from cloud/s3-signing: access key, scope, signed headers, expiry
```

For a presigned URL, the access key in `X-Amz-Credential` is the signer, not the person using the URL. `ASIA...` keys are temporary (role or STS) credentials; `AKIA...` keys are long-term IAM user keys.

## 3. Sort by error code

| Code (status) | Stage | What it means | Next step |
|---|---|---|---|
| `InvalidAccessKeyId` (403) | authentication | the access key does not exist in this endpoint's records (deleted key, wrong partition, a test key against real AWS, an AWS key against MinIO) | check step 2's identity against the endpoint; nothing in the policy can fix this |
| `InvalidToken` / `ExpiredToken` (400) | authentication | the session token is malformed or the temporary credentials expired | refresh the credentials (role session, SSO login); for presigned URLs, re-sign |
| `AuthorizationHeaderMalformed` (400) with `<Region>` | scope | the signature was scoped to another region (or service, or date) | fix the client's region to the one in `<Region>`; SDKs retry this on their own, raw clients do not |
| `RequestTimeTooSkewed` (403) | scope | client clock more than 15 minutes from the server's | fix NTP on the client (`timedatectl`, container host clock); compare `RequestTime` and `ServerTime` |
| `AccessDenied: Request has expired` (403) | presigned | now is past `X-Amz-Date` + `X-Amz-Expires` | step 5 |
| `AuthorizationQueryParametersError` (400) | presigned | missing `X-Amz-*` parameters or `X-Amz-Expires` over 604800 | the URL was truncated or built by hand; re-sign with an SDK |
| `SignatureDoesNotMatch` (403) | signature | the server's HMAC over its canonical request differs from the one sent | step 4 |
| `AccessDenied` with `HeadersNotSigned` (403) | signature | an `x-amz-*` header was sent but not signed (often added by a proxy or after signing) | sign it, or stop adding it after signing |
| `XAmzContentSHA256Mismatch`, `BadDigest` (400) | payload | the body differs from the hash, MD5 or checksum the client declared | the body was changed in transit or re-encoded (gzip, newline conversion, a retry with a partial body) |
| `AccessDenied` (403), any other message | authorization | the signature was valid; a policy said no | step 6 |
| `NoSuchKey` / `NoSuchBucket` (404) | operation | authorized, but the key or bucket is not there; a delete marker also gives `NoSuchKey` with `x-amz-delete-marker: true` | check versions: `aws s3api list-object-versions --bucket B --prefix KEY` |

A 403 with an empty body on HEAD is still one of the codes above; repeat the request as GET to read the body.

## 4. SignatureDoesNotMatch: diff the two canonical requests

The error body carries `<CanonicalRequest>` and `<StringToSign>` as S3 computed them. Put the client's (from `--debug`, the SDK's debug log, or `python3 code/main.py sign METHOD URL`) next to the server's and compare line by line:

```bash
python3 - <<'PY'
import difflib, html, re
server = html.unescape(re.search(r"<CanonicalRequest>(.*?)</CanonicalRequest>", open("body.xml").read(), re.S).group(1))
client = open("client-canonical.txt").read()
print("\n".join(difflib.unified_diff(client.splitlines(), server.splitlines(), "client", "server", lineterm="")))
PY
```

| Line that differs | Usual cause | Fix |
|---|---|---|
| none, but `StringToSign` differs in the scope or date | wrong region or date at signing time, clock jump | sign with the bucket's region; check the clock |
| none at all | wrong secret key, or a secret with a trailing newline or space, or the access key paired with another key's secret | re-read the secret from its source; compare `aws configure list` sources |
| 1 (method) | a presigned PUT URL used for GET or the reverse; a client sending HEAD | presign for the method that will be used |
| 2 (path) | a space sent as `+`, `%2B` vs `+`, a key double-encoded by a proxy or framework, path normalized by a CDN (`//`, `./`), path-style vs virtual-hosted mismatch | sign the exact key; stop the intermediary from rewriting the path; one addressing style end to end |
| 3 (query) | a parameter added, removed or re-encoded after signing (a cache-buster, a CDN, `+` vs `%20`) | sign the final URL; strip nothing from a presigned URL; quote it in shells (`&` splits commands) |
| headers block | `host` changed by a proxy, load balancer or port; `Content-Type` or an `x-amz-*` header signed with one value and sent with another | set the signed values exactly as sent; for presigned PUTs, the client must send the headers that were signed |
| last line (payload hash) | body changed after hashing; `UNSIGNED-PAYLOAD` expected but a hash sent, or the reverse | hash the final bytes; let the SDK compute it |

If both canonical requests are identical and the string to sign is identical, the keys differ. Nothing else can produce that result.

## 5. Presigned URL lifetimes

```bash
python3 code/main.py inspect "PRESIGNED_URL" --secret-key "$SECRET"   # verifies the signature locally when you hold the signer's secret
```

| Evidence | Meaning | Action |
|---|---|---|
| `Request has expired` and `X-Amz-Date` + `X-Amz-Expires` is in the past | the URL is too old | mint URLs on demand, close to use; do not raise the expiry to hide latency |
| stops working early, credential starts with `ASIA` | signed with role or STS credentials, which expired first | sign with a role whose session outlives the URL, or shorten the URL to fit; AWS caps the URL at the credential's life |
| `X-Amz-Expires` above 604800 | invalid by definition | 7 days is the maximum for CLI and SDK URLs, 12 hours from the console |
| AccessDenied only for presigned requests | a bucket policy denies `s3:authType` `REST-QUERY-STRING` or an old `s3:signatureAge` | intended policy; use header-signed requests or a fresher URL |

## 6. AccessDenied: walk the policy chain

The signature proved the principal; now find the policy that refused it. Since S3 adds context for same-account and same-organization requests, read the message first: `with an explicit deny in a ... policy` names the policy type (and, for several types, the policy ARN); `because no ... policy allows the ... action` names the missing Allow. Cross-account and anonymous callers only see `Access Denied`.

```bash
aws s3api get-bucket-policy --bucket BUCKET --query Policy --output text | python3 -m json.tool
aws s3api get-public-access-block --bucket BUCKET
aws s3api get-bucket-ownership-controls --bucket BUCKET
aws iam simulate-principal-policy --policy-source-arn ROLE_OR_USER_ARN --action-names s3:GetObject --resource-arns arn:aws:s3:::BUCKET/KEY
```

Check in this order: an explicit Deny anywhere (SCP, RCP, permission boundary, session policy, bucket policy, VPC endpoint policy) wins; then, for same-account requests, an Allow in the identity policy or the bucket policy; for cross-account requests, an Allow in both; then Block Public Access for anonymous or public grants; then SSE-KMS: reading or writing a KMS-encrypted object also needs `kms:Decrypt` or `kms:GenerateDataKey` on the key, and the denial comes from KMS. CloudTrail data events (if enabled for the bucket) record the `errorCode`, `errorMessage` and the principal for each object request.

## 7. Fix narrowly, then prove it

Change one thing: the region, the clock, the signing code, a single statement, a single condition. Re-run the exact failing request and keep the before and after status codes. Never fix a signature error with a policy change, and never fix an AccessDenied by making the bucket public.

## Report

Write: the failing operation, bucket and key (redacted if sensitive), endpoint and addressing style, the principal (account and ARN), the error code and message verbatim, `x-amz-request-id`, the stage from step 3, the evidence (the differing canonical-request line, the expired timestamps, or the denying policy and statement), the fix applied, the before/after result of the same request, and the guard that prevents a repeat (a test that signs a key with spaces and `+`, NTP monitoring, short-lived URL minting, a policy unit test, an alarm on 403 rates).
