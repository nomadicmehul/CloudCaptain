#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."

MOTO_VERSION=5.2.5
M="$PWD/code/main.py"
F="$PWD/code/fixtures"
WORK="$(mktemp -d)"
PIDS=()
BUCKET=cc-s3-signing
fail() {
  echo "SMOKE FAIL: $*" >&2
  for log in minis3.log moto.log moto-auth.log; do
    if [[ -f "$WORK/$log" ]]; then echo "--- $log (last 15 lines)" >&2; tail -15 "$WORK/$log" >&2; fi
  done
  exit 1
}
cleanup() {
  for pid in ${PIDS[@]+"${PIDS[@]}"}; do kill "$pid" 2>/dev/null || true; done
  wait 2>/dev/null || true
  rm -rf "$WORK"
}
trap cleanup EXIT

free_port() { python3 -c 'import socket; s = socket.socket(); s.bind(("127.0.0.1", 0)); print(s.getsockname()[1]); s.close()'; }
wait_http() { for _ in $(seq 1 150); do curl -s -o /dev/null "$1" && return 0; sleep 0.2; done; return 1; }
code_of() { curl -s -o "$WORK/body" -w '%{http_code}' "$@"; }
flip_last() { python3 -c 'import sys; u = sys.argv[1]; print(u[:-1] + ("0" if u[-1] != "0" else "1"))' "$1"; }

echo "▸ AWS's published SigV4 test vectors (aws-c-auth test suite) and the S3 API Reference examples"
python3 "$M" vectors "$F/aws-sigv4-test-suite.json" "$F/s3-api-examples.json" > "$WORK/vectors.txt" || fail "vectors: $(cat "$WORK/vectors.txt")"
grep -q "^81/81 signatures match" "$WORK/vectors.txt" || fail "expected 81/81: $(cat "$WORK/vectors.txt")"
tail -1 "$WORK/vectors.txt"

echo "▸ install moto[server]==$MOTO_VERSION into a venv (and the AWS CLI if the runner has none)"
python3 -m venv "$WORK/venv"
"$WORK/venv/bin/pip" install -q "moto[server]==$MOTO_VERSION" > "$WORK/pip.txt" 2>&1 || fail "pip install moto: $(tail -20 "$WORK/pip.txt")"
if command -v aws >/dev/null; then
  AWS_BIN="$(command -v aws)"
else
  echo "  (aws CLI not installed; installing awscli v1 into the venv)"
  "$WORK/venv/bin/pip" install -q awscli > "$WORK/pip-cli.txt" 2>&1 || fail "pip install awscli: $(tail -20 "$WORK/pip-cli.txt")"
  AWS_BIN="$WORK/venv/bin/aws"
fi
"$AWS_BIN" --version

export AWS_ACCESS_KEY_ID=AKIACCSMOKE00000001
export AWS_SECRET_ACCESS_KEY="smoke-secret-$RANDOM-$RANDOM"
export AWS_DEFAULT_REGION=us-east-1 AWS_PAGER="" AWS_EC2_METADATA_DISABLED=true
export AWS_CONFIG_FILE="$WORK/aws-config" AWS_SHARED_CREDENTIALS_FILE="$WORK/no-credentials"
cat > "$AWS_CONFIG_FILE" <<'EOF'
[default]
region = us-east-1
s3 =
  addressing_style = path
  signature_version = s3v4
  multipart_threshold = 8MB
  multipart_chunksize = 8MB
  preferred_transfer_client = classic
EOF
PARTNER_KEY=AKIACCPARTNER000001
PARTNER_SECRET="partner-secret-$RANDOM"

echo "▸ start moto (no auth), moto with IAM auth after 3 requests, and minis3"
P_MOTO=$(free_port); P_AUTH=$(free_port); P_MINI=$(free_port)
"$WORK/venv/bin/moto_server" -H 127.0.0.1 -p "$P_MOTO" > "$WORK/moto.log" 2>&1 &
PIDS+=("$!")
INITIAL_NO_AUTH_ACTION_COUNT=3 "$WORK/venv/bin/moto_server" -H 127.0.0.1 -p "$P_AUTH" > "$WORK/moto-auth.log" 2>&1 &
PIDS+=("$!")
python3 "$M" serve --port "$P_MINI" \
  --key "$AWS_ACCESS_KEY_ID:$AWS_SECRET_ACCESS_KEY:arn:aws:iam::123456789012:user/smoke" \
  --key "$PARTNER_KEY:$PARTNER_SECRET:arn:aws:iam::444455556666:user/partner" > "$WORK/minis3.log" 2>&1 &
PIDS+=("$!")
wait_http "http://127.0.0.1:$P_MOTO/moto-api/" || fail "moto did not start"
wait_http "http://127.0.0.1:$P_AUTH/moto-api/" || fail "moto (auth) did not start"
wait_http "http://127.0.0.1:$P_MINI/" || fail "minis3 did not start"
endpoint() { if [[ "$1" == "moto" ]]; then echo "http://127.0.0.1:$P_MOTO"; else echo "http://127.0.0.1:$P_MINI"; fi; }
on() { local target="$1"; shift; "$AWS_BIN" --endpoint-url "$(endpoint "$target")" "$@"; }

printf 'hello from cc-s3-signing\n' > "$WORK/small.txt"
python3 -c 'import random, sys; open(sys.argv[1], "wb").write(random.Random(7).randbytes(9 * 1024 * 1024))' "$WORK/big.bin"

echo "▸ the same aws s3api / aws s3 commands against moto and minis3: create, put, multipart cp, list with delimiter, download"
for target in moto minis3; do
  on "$target" s3api create-bucket --bucket "$BUCKET" > "$WORK/$target-create.json" || fail "$target create-bucket: $(cat "$WORK/$target-create.json")"
  for key in docs/readme.txt photos/2026/a.jpg photos/2026/b.jpg photos/2025/c.jpg top.txt; do
    on "$target" s3api put-object --bucket "$BUCKET" --key "$key" --body "$WORK/small.txt" > /dev/null || fail "$target put-object $key"
  done
  on "$target" s3 cp "$WORK/big.bin" "s3://$BUCKET/photos/2026/big.bin" --only-show-errors || fail "$target s3 cp (multipart)"
  on "$target" s3api list-objects-v2 --bucket "$BUCKET" --delimiter / > "$WORK/$target-root.json" || fail "$target list root"
  on "$target" s3api list-objects-v2 --bucket "$BUCKET" --prefix photos/ --delimiter / > "$WORK/$target-photos.json" || fail "$target list photos/"
  on "$target" s3api list-objects-v2 --bucket "$BUCKET" --prefix photos/2026/ > "$WORK/$target-2026.json" || fail "$target list photos/2026/"
  on "$target" s3 ls "s3://$BUCKET/photos/2026/" > "$WORK/$target-ls.txt" || fail "$target s3 ls"
  grep -q "big.bin" "$WORK/$target-ls.txt" || fail "$target s3 ls should show big.bin: $(cat "$WORK/$target-ls.txt")"
  on "$target" s3 cp "s3://$BUCKET/photos/2026/big.bin" "$WORK/big.$target" --only-show-errors || fail "$target download"
  cmp -s "$WORK/big.bin" "$WORK/big.$target" || fail "$target returned different bytes for big.bin"
done
python3 - "$WORK" <<'PY' || fail "moto and minis3 disagree on listings or ETags"
import hashlib, json, os, sys
work = sys.argv[1]
def load(target, name):
    return json.load(open(os.path.join(work, f"{target}-{name}.json")))
for name in ("root", "photos"):
    a, b = load("moto", name), load("minis3", name)
    keys = lambda d: [c["Key"] for c in d.get("Contents", [])]
    prefixes = lambda d: [p["Prefix"] for p in d.get("CommonPrefixes", [])]
    assert keys(a) == keys(b) and prefixes(a) == prefixes(b), (name, keys(a), keys(b), prefixes(a), prefixes(b))
assert [p["Prefix"] for p in load("minis3", "root")["CommonPrefixes"]] == ["docs/", "photos/"]
assert [p["Prefix"] for p in load("minis3", "photos")["CommonPrefixes"]] == ["photos/2025/", "photos/2026/"]
etags = {t: {c["Key"]: (c["ETag"], c["Size"]) for c in load(t, "2026")["Contents"]} for t in ("moto", "minis3")}
assert etags["moto"] == etags["minis3"], etags
big = etags["minis3"]["photos/2026/big.bin"][0].strip('"')
data = open(os.path.join(work, "big.bin"), "rb").read()
parts = [data[i:i + 8 * 1024 * 1024] for i in range(0, len(data), 8 * 1024 * 1024)]
expected = hashlib.md5(b"".join(hashlib.md5(p).digest() for p in parts)).hexdigest() + f"-{len(parts)}"
assert big == expected, (big, expected)
small = etags["minis3"]["photos/2026/a.jpg"][0].strip('"')
assert small == hashlib.md5(open(os.path.join(work, "small.txt"), "rb").read()).hexdigest(), small
print("  same keys and common prefixes; ETags equal on both: single part", small, "| multipart", big)
PY

echo "▸ versioning on both: two versions, a delete marker, get by version id"
for target in moto minis3; do
  on "$target" s3api put-bucket-versioning --bucket "$BUCKET" --versioning-configuration Status=Enabled || fail "$target put-bucket-versioning"
  printf v1 > "$WORK/v1.txt"; printf v2 > "$WORK/v2.txt"
  V1=$(on "$target" s3api put-object --bucket "$BUCKET" --key config.json --body "$WORK/v1.txt" --query VersionId --output text) || fail "$target put v1"
  on "$target" s3api put-object --bucket "$BUCKET" --key config.json --body "$WORK/v2.txt" > /dev/null || fail "$target put v2"
  on "$target" s3api delete-object --bucket "$BUCKET" --key config.json > "$WORK/$target-delete.json" || fail "$target delete-object"
  RC=0; on "$target" s3api get-object --bucket "$BUCKET" --key config.json "$WORK/$target-deleted.out" > /dev/null 2> "$WORK/$target-deleted.err" || RC=$?
  [[ "$RC" != "0" ]] && grep -q "NoSuchKey" "$WORK/$target-deleted.err" || fail "$target: get after delete should be NoSuchKey: rc=$RC $(cat "$WORK/$target-deleted.err")"
  on "$target" s3api get-object --bucket "$BUCKET" --key config.json --version-id "$V1" "$WORK/$target-v1.out" > /dev/null || fail "$target get --version-id"
  [[ "$(cat "$WORK/$target-v1.out")" == "v1" ]] || fail "$target: version $V1 should hold v1"
  on "$target" s3api list-object-versions --bucket "$BUCKET" --prefix config.json > "$WORK/$target-versions.json" || fail "$target list-object-versions"
  python3 - "$WORK/$target-versions.json" "$WORK/$target-delete.json" "$target" <<'PY' || fail "$target versions: $(cat "$WORK/$target-versions.json")"
import json, sys
versions, deleted, target = json.load(open(sys.argv[1])), json.load(open(sys.argv[2])), sys.argv[3]
assert deleted.get("DeleteMarker") is True, deleted
assert len(versions.get("Versions", [])) == 2 and len(versions.get("DeleteMarkers", [])) == 1, versions
assert versions["DeleteMarkers"][0]["IsLatest"] is True, versions["DeleteMarkers"]
print(f"  {target}: 2 versions, 1 delete marker (latest)")
PY
done

echo "▸ presigned URLs: aws s3 presign (botocore) against minis3, minis3's presign against moto and minis3"
URL=$(on minis3 s3 presign "s3://$BUCKET/top.txt" --expires-in 300) || fail "aws s3 presign"
[[ "$(code_of "$URL")" == "200" ]] || fail "botocore's presigned URL should work on minis3: $(cat "$WORK/body")"
grep -q "hello from cc-s3-signing" "$WORK/body" || fail "presigned GET returned the wrong body"
python3 "$M" inspect "$URL" --secret-key "$AWS_SECRET_ACCESS_KEY" > "$WORK/inspect.txt" || fail "minis3 inspect should verify botocore's URL: $(cat "$WORK/inspect.txt")"
grep -q "signature      matches" "$WORK/inspect.txt" || fail "inspect: $(cat "$WORK/inspect.txt")"
BAD=$(flip_last "$URL")
[[ "$(code_of "$BAD")" == "403" ]] && grep -q "<Code>SignatureDoesNotMatch</Code>" "$WORK/body" || fail "one changed signature byte should be SignatureDoesNotMatch: $(cat "$WORK/body")"
grep -q "<CanonicalRequest>GET" "$WORK/body" || fail "the error should carry the server's canonical request: $(cat "$WORK/body")"
echo "  botocore's URL: 200 on minis3; one changed byte: 403 SignatureDoesNotMatch with the CanonicalRequest"
OLD=$(python3 "$M" presign "http://127.0.0.1:$P_MINI/$BUCKET/top.txt" --expires 60 --date 20200101T000000Z) || fail "minis3 presign --date"
[[ "$(code_of "$OLD")" == "403" ]] && grep -q "Request has expired" "$WORK/body" || fail "an expired URL should say Request has expired: $(cat "$WORK/body")"
for target in moto minis3; do
  GET=$(python3 "$M" presign "$(endpoint "$target")/$BUCKET/top.txt" --expires 300) || fail "minis3 presign for $target"
  [[ "$(code_of "$GET")" == "200" ]] && grep -q "hello from cc-s3-signing" "$WORK/body" || fail "minis3's presigned GET on $target: $(cat "$WORK/body")"
  PUT=$(python3 "$M" presign "$(endpoint "$target")/$BUCKET/uploads/presigned.txt" --method PUT --expires 300) || fail "minis3 presign PUT for $target"
  printf 'uploaded with a presigned PUT\n' > "$WORK/presigned.txt"
  [[ "$(code_of -T "$WORK/presigned.txt" "$PUT")" == "200" ]] || fail "presigned PUT on $target: $(cat "$WORK/body")"
  on "$target" s3api get-object --bucket "$BUCKET" --key uploads/presigned.txt "$WORK/presigned.$target" > /dev/null || fail "$target get the presigned upload"
  cmp -s "$WORK/presigned.txt" "$WORK/presigned.$target" || fail "$target stored different bytes for the presigned PUT"
done
TAMPERED=$(python3 "$M" presign "http://127.0.0.1:$P_MOTO/$BUCKET/top.txt") || fail "minis3 presign for moto"
TAMPERED=$(flip_last "$TAMPERED")
echo "  minis3's GET and PUT URLs work on moto and minis3; moto answers $(code_of "$TAMPERED") to a tampered URL (moto does not verify presigned signatures)"

echo "▸ moto with IAM auth verifies header signatures: minis3's signer is accepted, one changed byte is not"
"$AWS_BIN" --endpoint-url "http://127.0.0.1:$P_AUTH" iam create-user --user-name smoke > /dev/null || fail "iam create-user"
"$AWS_BIN" --endpoint-url "http://127.0.0.1:$P_AUTH" iam create-access-key --user-name smoke > "$WORK/iam-key.json" || fail "iam create-access-key"
"$AWS_BIN" --endpoint-url "http://127.0.0.1:$P_AUTH" iam put-user-policy --user-name smoke --policy-name s3-all \
  --policy-document '{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Action":"s3:*","Resource":"*"}]}' || fail "iam put-user-policy"
IAM_AK=$(python3 -c 'import json, sys; print(json.load(open(sys.argv[1]))["AccessKey"]["AccessKeyId"])' "$WORK/iam-key.json")
IAM_SK=$(python3 -c 'import json, sys; print(json.load(open(sys.argv[1]))["AccessKey"]["SecretAccessKey"])' "$WORK/iam-key.json")
AUTH="http://127.0.0.1:$P_AUTH"
python3 "$M" send PUT "$AUTH/$BUCKET" --access-key "$IAM_AK" --secret-key "$IAM_SK" > "$WORK/auth-create.txt" 2>&1 || fail "minis3-signed CreateBucket on moto: $(cat "$WORK/auth-create.txt")"
python3 "$M" send PUT "$AUTH/$BUCKET/signed by minis3.txt" --data-file "$WORK/small.txt" --access-key "$IAM_AK" --secret-key "$IAM_SK" > "$WORK/auth-put.txt" 2>&1 || fail "minis3-signed PutObject on moto: $(cat "$WORK/auth-put.txt")"
python3 "$M" send GET "$AUTH/$BUCKET/signed by minis3.txt" --access-key "$IAM_AK" --secret-key "$IAM_SK" --output "$WORK/auth-get.out" > "$WORK/auth-get.txt" 2>&1 || fail "minis3-signed GetObject on moto: $(cat "$WORK/auth-get.txt")"
cmp -s "$WORK/small.txt" "$WORK/auth-get.out" || fail "moto returned different bytes"
RC=0; python3 "$M" send GET "$AUTH/$BUCKET/signed by minis3.txt" --access-key "$IAM_AK" --secret-key wrong-secret > "$WORK/auth-wrong.txt" 2>&1 || RC=$?
[[ "$RC" == "1" ]] && grep -q "SignatureDoesNotMatch" "$WORK/auth-wrong.txt" || fail "a wrong secret should be SignatureDoesNotMatch on moto: rc=$RC $(cat "$WORK/auth-wrong.txt")"
python3 "$M" sign GET "$AUTH/$BUCKET/signed by minis3.txt" --access-key "$IAM_AK" --secret-key "$IAM_SK" --json > "$WORK/signed.json" || fail "minis3 sign --json"
python3 - "$WORK/signed.json" > "$WORK/curl-headers.txt" <<'PY'
import json, sys
signed = json.load(open(sys.argv[1]))
for name, value in signed["headers"]:
    if name == "Authorization":
        value = value[:-1] + ("0" if value[-1] != "0" else "1")
    print(f"{name}: {value}")
PY
HEADERS=(); while IFS= read -r line; do HEADERS+=(-H "$line"); done < "$WORK/curl-headers.txt"
SIGNED_URL=$(python3 -c 'import json, sys; print(json.load(open(sys.argv[1]))["url"])' "$WORK/signed.json")
[[ "$(code_of "${HEADERS[@]}" "$SIGNED_URL")" == "403" ]] && grep -q "SignatureDoesNotMatch" "$WORK/body" || fail "one changed byte in the Authorization header should be SignatureDoesNotMatch on moto: $(cat "$WORK/body")"
echo "  moto recomputed minis3's signatures (PUT, GET: 200); wrong secret and one changed byte: 403 SignatureDoesNotMatch"

echo "▸ the same changed byte in a header-signed request to minis3"
python3 "$M" sign GET "http://127.0.0.1:$P_MINI/$BUCKET/top.txt" --json > "$WORK/signed-mini.json" || fail "minis3 sign --json"
python3 - "$WORK/signed-mini.json" > "$WORK/curl-headers-mini.txt" <<'PY'
import json, sys
for name, value in json.load(open(sys.argv[1]))["headers"]:
    print(f"{name}: {value[:-1] + ('0' if value[-1] != '0' else '1') if name == 'Authorization' else value}")
PY
HEADERS=(); while IFS= read -r line; do HEADERS+=(-H "$line"); done < "$WORK/curl-headers-mini.txt"
[[ "$(code_of "${HEADERS[@]}" "http://127.0.0.1:$P_MINI/$BUCKET/top.txt")" == "403" ]] && grep -q "<Code>SignatureDoesNotMatch</Code>" "$WORK/body" || fail "minis3 should refuse the changed byte: $(cat "$WORK/body")"
echo "  403 SignatureDoesNotMatch"

echo "▸ bucket policy: public prefix for anonymous curl on both, explicit deny beats the owner on both"
cat > "$WORK/policy.json" <<EOF
{"Version": "2012-10-17", "Statement": [
  {"Sid": "PublicPhotos", "Effect": "Allow", "Principal": "*", "Action": "s3:GetObject", "Resource": "arn:aws:s3:::$BUCKET/photos/*"},
  {"Sid": "NoDocs", "Effect": "Deny", "Principal": "*", "Action": "s3:GetObject", "Resource": "arn:aws:s3:::$BUCKET/docs/*"}
]}
EOF
for target in moto minis3; do
  [[ "$(code_of "$(endpoint "$target")/$BUCKET/photos/2026/a.jpg")" == "403" ]] || fail "$target: anonymous GET before the policy should be 403"
  on "$target" s3api put-bucket-policy --bucket "$BUCKET" --policy "file://$WORK/policy.json" || fail "$target put-bucket-policy"
  [[ "$(code_of "$(endpoint "$target")/$BUCKET/photos/2026/a.jpg")" == "200" ]] || fail "$target: anonymous GET on photos/ should be 200 after the policy: $(cat "$WORK/body")"
  [[ "$(code_of "$(endpoint "$target")/$BUCKET/top.txt")" == "403" ]] || fail "$target: anonymous GET outside photos/ should stay 403"
  RC=0; on "$target" s3api get-object --bucket "$BUCKET" --key docs/readme.txt "$WORK/denied.out" > /dev/null 2> "$WORK/denied.err" || RC=$?
  [[ "$RC" != "0" ]] && grep -qi "AccessDenied\|Forbidden\|403" "$WORK/denied.err" || fail "$target: the explicit deny should beat the owner: rc=$RC $(cat "$WORK/denied.err")"
done
echo "  anonymous: 403, then 200 on photos/ and 403 elsewhere; owner on docs/: AccessDenied (both)"

echo "▸ minis3 only (moto does not evaluate these): cross-account access and a deny on s3:authType"
RC=0; AWS_ACCESS_KEY_ID=$PARTNER_KEY AWS_SECRET_ACCESS_KEY=$PARTNER_SECRET on minis3 s3api get-object --bucket "$BUCKET" --key top.txt "$WORK/partner.out" > /dev/null 2> "$WORK/partner.err" || RC=$?
[[ "$RC" != "0" ]] && grep -q "AccessDenied" "$WORK/partner.err" || fail "another account without a grant should get AccessDenied: $(cat "$WORK/partner.err")"
cat > "$WORK/policy2.json" <<EOF
{"Version": "2012-10-17", "Statement": [
  {"Sid": "Partner", "Effect": "Allow", "Principal": {"AWS": "arn:aws:iam::444455556666:user/partner"}, "Action": "s3:GetObject", "Resource": "arn:aws:s3:::$BUCKET/*"},
  {"Sid": "NoPresignedUrls", "Effect": "Deny", "Principal": "*", "Action": "s3:GetObject", "Resource": "arn:aws:s3:::$BUCKET/*", "Condition": {"StringEquals": {"s3:authType": "REST-QUERY-STRING"}}}
]}
EOF
on minis3 s3api put-bucket-policy --bucket "$BUCKET" --policy "file://$WORK/policy2.json" || fail "put-bucket-policy (partner)"
AWS_ACCESS_KEY_ID=$PARTNER_KEY AWS_SECRET_ACCESS_KEY=$PARTNER_SECRET on minis3 s3api get-object --bucket "$BUCKET" --key top.txt "$WORK/partner.out" > /dev/null || fail "the partner should read top.txt once the policy names it"
URL=$(on minis3 s3 presign "s3://$BUCKET/top.txt") || fail "aws s3 presign"
[[ "$(code_of "$URL")" == "403" ]] && grep -q "NoPresignedUrls" "$WORK/body" || fail "the s3:authType deny should block presigned URLs: $(cat "$WORK/body")"
on minis3 s3api get-object --bucket "$BUCKET" --key top.txt "$WORK/owner.out" > /dev/null || fail "header-signed requests should still work"
echo "  partner: AccessDenied, then allowed; presigned URL denied by NoPresignedUrls while header auth works"

echo "▸ lifecycle on moto (minis3 does not implement it)"
cat > "$WORK/lifecycle.json" <<'EOF'
{"Rules": [{"ID": "expire-old-versions", "Status": "Enabled", "Filter": {"Prefix": ""},
  "NoncurrentVersionExpiration": {"NoncurrentDays": 30},
  "AbortIncompleteMultipartUpload": {"DaysAfterInitiation": 7}}]}
EOF
on moto s3api put-bucket-lifecycle-configuration --bucket "$BUCKET" --lifecycle-configuration "file://$WORK/lifecycle.json" || fail "put-bucket-lifecycle-configuration"
on moto s3api get-bucket-lifecycle-configuration --bucket "$BUCKET" > "$WORK/lifecycle-out.json" || fail "get-bucket-lifecycle-configuration"
python3 -c 'import json, sys; r = json.load(open(sys.argv[1]))["Rules"][0]; assert r["NoncurrentVersionExpiration"]["NoncurrentDays"] == 30, r' "$WORK/lifecycle-out.json" || fail "lifecycle round trip: $(cat "$WORK/lifecycle-out.json")"
echo "  NoncurrentVersionExpiration 30 days, AbortIncompleteMultipartUpload 7 days"

echo "SMOKE OK: cloud/s3-signing"
