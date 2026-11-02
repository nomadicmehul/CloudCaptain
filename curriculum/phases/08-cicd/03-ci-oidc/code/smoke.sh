#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."

LESSON_DIR="$PWD"
MINI=(python3 "$LESSON_DIR/code/main.py")
FIX="$LESSON_DIR/code/fixtures"
TP="$FIX/trust-policies"
MOTO_VERSION="5.2.5"
ACCOUNT_ID="123456789012"
ROLE_ARN="arn:aws:iam::$ACCOUNT_ID:role/cc-deploy"
PROVIDER_ARN="arn:aws:iam::$ACCOUNT_ID:oidc-provider/token.actions.githubusercontent.com"
ISSUER="https://token.actions.githubusercontent.com"
WORK="$(mktemp -d)"
PIDS=()
fail() {
  echo "SMOKE FAIL: $*" >&2
  for log in issuer moto; do
    if [[ -f "$WORK/$log.log" ]]; then echo "--- $log log (last 20 lines)" >&2; tail -20 "$WORK/$log.log" >&2; fi
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
field() {
  python3 - "$@" <<'PY'
import json, sys
value = json.load(open(sys.argv[1]))
for key in sys.argv[2:]:
    value = value[int(key)] if isinstance(value, list) else value[key]
print(json.dumps(value) if isinstance(value, (dict, list)) else value)
PY
}
assume() {
  local out="$1" token="$2" policy="$3"
  shift 3
  local code=0
  "${MINI[@]}" assume --token "$token" --role-arn "$ROLE_ARN" --trust-policy "$policy" --provider "${PROVIDER_FILE:-$FIX/providers/github.json}" \
    --discovery-url "$BASE/.well-known/openid-configuration" --json "$@" > "$out" 2> "$out.err" || code=$?
  echo "$code"
}
outcome_code() { python3 -c 'import json,sys; d=json.load(open(sys.argv[1])); print("allowed" if "Credentials" in d else d["Error"]["Code"])' "$1" 2>/dev/null || echo "unparsable"; }
request_token() {
  local job="$1"
  [[ -s "$WORK/env/$job.env" ]] || fail "serve did not write $WORK/env/$job.env"
  (
    # shellcheck disable=SC1090
    source "$WORK/env/$job.env"
    curl -sf -H "Authorization: bearer $ACTIONS_ID_TOKEN_REQUEST_TOKEN" "$ACTIONS_ID_TOKEN_REQUEST_URL&audience=sts.amazonaws.com" > "$WORK/$job.resp"
  ) || fail "curl idtoken for $job"
  python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["value"])' "$WORK/$job.resp" > "$WORK/$job.jwt" || fail "no value in $(cat "$WORK/$job.resp")"
}

echo "▸ minioidc on 27 trust decisions documented by AWS, GitHub and GitLab (fresh 2048-bit keys)"
"${MINI[@]}" cases "$FIX/aws-doc-cases.json" --json > "$WORK/cases.json" || fail "documented cases: $("${MINI[@]}" cases "$FIX/aws-doc-cases.json" | grep FAIL)"
python3 - "$WORK/cases.json" <<'PY' || fail "documented cases: $(cat "$WORK/cases.json")"
import json, sys
rows = json.load(open(sys.argv[1]))
assert len(rows) >= 25 and all(r["ok"] for r in rows), [r for r in rows if not r["ok"]]
codes = {r["got"] for r in rows}
assert codes == {"allowed", "AccessDenied", "InvalidIdentityToken", "ExpiredTokenException"}, codes
print("  %d/%d as documented: %s" % (sum(r["ok"] for r in rows), len(rows), ", ".join(sorted(codes))))
PY

echo "▸ keygen: a 2048-bit issuer key, written only to the temp dir"
"${MINI[@]}" keygen --kid cc-smoke-1 --out "$WORK/issuer.json" > "$WORK/keygen.txt" || fail "keygen: $(cat "$WORK/keygen.txt")"
[[ "$(stat -c %a "$WORK/issuer.json" 2>/dev/null || stat -f %Lp "$WORK/issuer.json")" == "600" ]] || fail "the private key file should be mode 600"

echo "▸ serve the issuer as $ISSUER on 127.0.0.1 (discovery, JWKS, the runner's idtoken endpoint)"
"${MINI[@]}" serve --key "$WORK/issuer.json" --issuer "$ISSUER" --jobs "$FIX/jobs.json" --env-dir "$WORK/env" --port 0 --port-file "$WORK/issuer.port" > "$WORK/issuer.log" 2>&1 &
PIDS+=("$!")
for _ in $(seq 1 100); do [[ -s "$WORK/issuer.port" ]] && break; sleep 0.1; done
[[ -s "$WORK/issuer.port" ]] || fail "the issuer did not start"
BASE="http://127.0.0.1:$(cat "$WORK/issuer.port")"
curl -sf -D "$WORK/discovery.headers" -o "$WORK/discovery.json" "$BASE/.well-known/openid-configuration" || fail "GET discovery"
curl -sf -o "$WORK/jwks.json" "$BASE/.well-known/jwks" || fail "GET jwks"
python3 - "$WORK/discovery.json" "$WORK/jwks.json" "$ISSUER" "$BASE" <<'PY' || fail "discovery/JWKS: $(cat "$WORK/discovery.json" "$WORK/jwks.json")"
import json, sys
discovery, jwks = json.load(open(sys.argv[1])), json.load(open(sys.argv[2]))
assert discovery["issuer"] == sys.argv[3], discovery
assert discovery["jwks_uri"] == sys.argv[4] + "/.well-known/jwks", discovery
assert discovery["id_token_signing_alg_values_supported"] == ["RS256"]
assert [k["kid"] for k in jwks["keys"]] == ["cc-smoke-1"], jwks
assert not set(jwks["keys"][0]) & {"d", "p", "q", "dp", "dq", "qi"}, "private members published"
print("  issuer %s, jwks_uri %s, key %s" % (discovery["issuer"], discovery["jwks_uri"], jwks["keys"][0]["kid"]))
PY
grep -qi "^cache-control: public, max-age=3600" "$WORK/discovery.headers" || fail "discovery should be cacheable for an hour: $(cat "$WORK/discovery.headers")"

echo "▸ request tokens the way the runner does: bearer request token plus audience"
CODE=$(curl -s -o /dev/null -w '%{http_code}' "$BASE/idtoken?api-version=2.0&audience=sts.amazonaws.com")
[[ "$CODE" == "401" ]] || fail "a request without the runner's bearer token should be 401, got $CODE"
for job in deploy-prod feature-push stranger; do
  request_token "$job"
  "${MINI[@]}" decode "$WORK/$job.jwt" --json > "$WORK/$job.decoded"
  echo "  $job: sub $(field "$WORK/$job.decoded" claims sub)"
done
[[ "$(field "$WORK/deploy-prod.decoded" claims sub)" == "repo:octo-org/octo-repo:environment:prod" ]] || fail "deploy-prod sub: $(cat "$WORK/deploy-prod.decoded")"
[[ "$(field "$WORK/deploy-prod.decoded" claims aud)" == "sts.amazonaws.com" && "$(field "$WORK/deploy-prod.decoded" header kid)" == "cc-smoke-1" ]] || fail "aud/kid: $(cat "$WORK/deploy-prod.decoded")"
(
  # shellcheck disable=SC1090
  source "$WORK/env/deploy-prod.env"
  "${MINI[@]}" request --audience api://AzureADTokenExchange > "$WORK/azure.jwt"
) || fail "minioidc request"
[[ "$("${MINI[@]}" decode "$WORK/azure.jwt" --json | python3 -c 'import json,sys; print(json.load(sys.stdin)["claims"]["aud"])')" == "api://AzureADTokenExchange" ]] || fail "request --audience was not honoured"
DEPLOY="$(cat "$WORK/deploy-prod.jwt")"
FEATURE="$(cat "$WORK/feature-push.jwt")"
STRANGER="$(cat "$WORK/stranger.jwt")"

if command -v openssl >/dev/null; then
  echo "▸ openssl verifies minioidc's RS256 signature with the key exported from the JWKS"
  "${MINI[@]}" jwk-to-pem --jwks "$BASE/.well-known/jwks" --kid cc-smoke-1 > "$WORK/issuer.pub.pem" || fail "jwk-to-pem"
  openssl pkey -pubin -in "$WORK/issuer.pub.pem" -noout -text > "$WORK/pkey.txt" 2>&1 || fail "openssl cannot read the exported PEM: $(cat "$WORK/pkey.txt")"
  grep -q "2048 bit" "$WORK/pkey.txt" || fail "expected a 2048-bit key: $(head -2 "$WORK/pkey.txt")"
  printf '%s' "${DEPLOY%.*}" > "$WORK/signed.txt"
  python3 -c 'import base64,sys; s=sys.argv[1]; sys.stdout.buffer.write(base64.urlsafe_b64decode(s + "=" * (-len(s) % 4)))' "${DEPLOY##*.}" > "$WORK/signature.bin"
  openssl dgst -sha256 -verify "$WORK/issuer.pub.pem" -signature "$WORK/signature.bin" "$WORK/signed.txt" > "$WORK/dgst.txt" 2>&1 || fail "openssl dgst -verify: $(cat "$WORK/dgst.txt")"
  grep -q "Verified OK" "$WORK/dgst.txt" || fail "openssl: $(cat "$WORK/dgst.txt")"
  printf 'x' >> "$WORK/signed.txt"
  if openssl dgst -sha256 -verify "$WORK/issuer.pub.pem" -signature "$WORK/signature.bin" "$WORK/signed.txt" > "$WORK/dgst-bad.txt" 2>&1; then fail "openssl accepted a tampered payload"; fi
  echo "  $(cat "$WORK/dgst.txt"); one changed byte: $(grep -o 'Verification failure' "$WORK/dgst-bad.txt" | head -1)"

  echo "▸ minioidc verifies a token whose signature openssl made"
  openssl genpkey -algorithm RSA -pkeyopt rsa_keygen_bits:2048 -out "$WORK/openssl.key" > /dev/null 2>&1 || fail "openssl genpkey"
  openssl pkey -in "$WORK/openssl.key" -pubout -out "$WORK/openssl.pub" || fail "openssl pkey -pubout"
  "${MINI[@]}" pem-to-jwk "$WORK/openssl.pub" --kid openssl-key > "$WORK/openssl-jwks.json" || fail "pem-to-jwk"
  python3 - "$WORK/deploy-prod.decoded" "$WORK/openssl.key" "$WORK/openssl.jwt" <<'PY' || fail "sign with openssl"
import base64, json, subprocess, sys
b64 = lambda data: base64.urlsafe_b64encode(data).rstrip(b"=").decode()
claims = json.load(open(sys.argv[1]))["claims"]
signing_input = b64(json.dumps({"alg": "RS256", "kid": "openssl-key", "typ": "JWT"}).encode()) + "." + b64(json.dumps(claims).encode())
signature = subprocess.run(["openssl", "dgst", "-sha256", "-sign", sys.argv[2]], input=signing_input.encode(), capture_output=True, check=True).stdout
open(sys.argv[3], "w").write(signing_input + "." + b64(signature))
PY
  "${MINI[@]}" verify "$WORK/openssl.jwt" --provider "$FIX/providers/github.json" --jwks "$WORK/openssl-jwks.json" --json > "$WORK/openssl-verify.json" || fail "minioidc rejected openssl's RS256 signature: $(cat "$WORK/openssl-verify.json")"
  [[ "$(field "$WORK/openssl-verify.json" kid)" == "openssl-key" ]] || fail "verify: $(cat "$WORK/openssl-verify.json")"
  if "${MINI[@]}" verify "$WORK/openssl.jwt" --provider "$FIX/providers/github.json" --jwks "$WORK/jwks.json" > "$WORK/wrong-jwks.txt"; then fail "a token signed by another key verified against the issuer's JWKS"; fi
  echo "  openssl-signed token verified; against the issuer's JWKS: $(cut -c1-60 "$WORK/wrong-jwks.txt")"
else
  echo "  (openssl not installed; signature interop checks skipped)"
fi

echo "▸ minioidc's STS against the live issuer: environment-pinned trust policy"
CODE=$(assume "$WORK/mini-deploy.json" "$DEPLOY" "$TP/github-environment.json" --duration-seconds 900)
[[ "$CODE" == "0" ]] || fail "deploy-prod should get a session: $(cat "$WORK/mini-deploy.json" "$WORK/mini-deploy.json.err")"
MINI_ARN="$(field "$WORK/mini-deploy.json" AssumedRoleUser Arn)"
[[ "$MINI_ARN" == "arn:aws:sts::$ACCOUNT_ID:assumed-role/cc-deploy/GitHubActions" ]] || fail "assumed-role ARN: $MINI_ARN"
for row in "feature-push:$FEATURE:AccessDenied" "stranger:$STRANGER:AccessDenied"; do
  IFS=: read -r name token want <<< "$row"
  CODE=$(assume "$WORK/mini-$name.json" "$token" "$TP/github-environment.json")
  [[ "$CODE" == "1" && "$(outcome_code "$WORK/mini-$name.json")" == "$want" ]] || fail "$name should be $want: $(cat "$WORK/mini-$name.json")"
done
"${MINI[@]}" mint --key "$WORK/issuer.json" --jobs "$FIX/jobs.json" --job deploy-prod --audience sts.amazonaws.com --now "$(( $(date +%s) - 1200 ))" > "$WORK/expired.jwt"
CODE=$(assume "$WORK/mini-expired.json" "$(cat "$WORK/expired.jwt")" "$TP/github-environment.json")
[[ "$CODE" == "1" && "$(outcome_code "$WORK/mini-expired.json")" == "ExpiredTokenException" ]] || fail "expired token: $(cat "$WORK/mini-expired.json")"
CODE=$(assume "$WORK/mini-long.json" "$DEPLOY" "$TP/github-environment.json" --duration-seconds 7200)
[[ "$CODE" == "1" && "$(outcome_code "$WORK/mini-long.json")" == "ValidationError" ]] || fail "7200 s above MaxSessionDuration: $(cat "$WORK/mini-long.json")"
echo "  deploy-prod -> $MINI_ARN; feature-push, stranger -> AccessDenied; 20-minute-old token -> ExpiredTokenException; 7200 s -> ValidationError"

echo "▸ audit: the documented policy is clean, the aud-only one is CRITICAL"
"${MINI[@]}" audit --trust-policy "$TP/github-environment.json" --provider "$FIX/providers/github.json" > "$WORK/audit-ok.txt" || fail "github-environment should pass the audit: $(cat "$WORK/audit-ok.txt")"
CODE=0
"${MINI[@]}" audit --trust-policy "$TP/github-no-sub.json" --provider "$FIX/providers/github.json" --json > "$WORK/audit-no-sub.json" || CODE=$?
[[ "$CODE" == "1" && "$(field "$WORK/audit-no-sub.json" 0 code)" == "missing-subject" && "$(field "$WORK/audit-no-sub.json" 0 severity)" == "CRITICAL" ]] || fail "audit github-no-sub: $(cat "$WORK/audit-no-sub.json")"

echo "▸ install moto[server]==$MOTO_VERSION into a venv and start moto_server"
if ! python3 -m venv "$WORK/venv" > "$WORK/venv.log" 2>&1; then
  echo "  (python3 -m venv not available; moto comparison skipped)"
  echo "SMOKE OK: cicd/ci-oidc (minioidc and openssl only)"
  exit 0
fi
PACKAGES=("moto[server]==$MOTO_VERSION")
if command -v aws >/dev/null; then AWS_BIN="$(command -v aws)"; else PACKAGES+=("awscli"); AWS_BIN="$WORK/venv/bin/aws"; fi
"$WORK/venv/bin/pip" install -q --disable-pip-version-check "${PACKAGES[@]}" > "$WORK/pip.log" 2>&1 || fail "pip install ${PACKAGES[*]}: $(tail -20 "$WORK/pip.log")"
"$WORK/venv/bin/python" -c 'import moto; print("  moto", moto.__version__)'
"$AWS_BIN" --version 2>&1 | sed 's/^/  /'
MOTO_PORT="$(free_port)"
"$WORK/venv/bin/moto_server" -H 127.0.0.1 -p "$MOTO_PORT" > "$WORK/moto.log" 2>&1 &
PIDS+=("$!")
ENDPOINT="http://127.0.0.1:$MOTO_PORT"
for _ in $(seq 1 150); do curl -sf -o /dev/null "$ENDPOINT/moto-api/data.json" && break; sleep 0.2; done
curl -sf -o /dev/null "$ENDPOINT/moto-api/data.json" || fail "moto_server did not answer on $ENDPOINT"
touch "$WORK/aws-config" "$WORK/aws-credentials"
export AWS_CONFIG_FILE="$WORK/aws-config" AWS_SHARED_CREDENTIALS_FILE="$WORK/aws-credentials"
export AWS_ACCESS_KEY_ID=setup AWS_SECRET_ACCESS_KEY=setup AWS_DEFAULT_REGION=us-east-1 AWS_PAGER="" AWS_EC2_METADATA_DISABLED=true
unset AWS_PROFILE AWS_SESSION_TOKEN 2>/dev/null || true
AWSCLI=("$AWS_BIN" --endpoint-url "$ENDPOINT")
run() { "$@" > "$WORK/last.out" 2>&1 || fail "$*: $(cat "$WORK/last.out")"; }
for job in deploy-prod feature-push stranger; do request_token "$job"; done
DEPLOY="$(cat "$WORK/deploy-prod.jwt")"
FEATURE="$(cat "$WORK/feature-push.jwt")"
STRANGER="$(cat "$WORK/stranger.jwt")"
echo "  fresh tokens requested for the moto comparison (they expire 300 s after issue)"

echo "▸ Use It: the IAM OIDC provider and the role, created with the AWS CLI"
"${AWSCLI[@]}" iam create-open-id-connect-provider --url "$ISSUER" --client-id-list sts.amazonaws.com > "$WORK/provider-created.json" 2>&1 || fail "create-open-id-connect-provider: $(cat "$WORK/provider-created.json")"
[[ "$(field "$WORK/provider-created.json" OpenIDConnectProviderArn)" == "$PROVIDER_ARN" ]] || fail "provider ARN: $(cat "$WORK/provider-created.json")"
run "${AWSCLI[@]}" iam create-role --role-name cc-deploy --assume-role-policy-document "file://$TP/github-environment.json"
"${AWSCLI[@]}" iam get-open-id-connect-provider --open-id-connect-provider-arn "$PROVIDER_ARN" > "$WORK/provider.json" 2>&1 || fail "get-open-id-connect-provider: $(cat "$WORK/provider.json")"
"${AWSCLI[@]}" iam get-role --role-name cc-deploy --query Role.AssumeRolePolicyDocument > "$WORK/trust.json" 2>&1 || fail "get-role: $(cat "$WORK/trust.json")"
python3 - "$WORK/provider.json" "$WORK/trust.json" "$TP/github-environment.json" <<'PY' || fail "exported documents: $(cat "$WORK/provider.json" "$WORK/trust.json")"
import json, sys
provider, trust, fixture = (json.load(open(p)) for p in sys.argv[1:4])
assert provider["Url"] == "token.actions.githubusercontent.com" and provider["ClientIDList"] == ["sts.amazonaws.com"], provider
assert trust == fixture, (trust, fixture)
print("  provider Url %s, ClientIDList %s; the stored trust policy equals the fixture" % (provider["Url"], provider["ClientIDList"]))
PY
PROVIDER_FILE="$WORK/provider.json"
CODE=$(assume "$WORK/mini-exported.json" "$DEPLOY" "$WORK/trust.json" --duration-seconds 900)
[[ "$CODE" == "0" ]] || fail "minioidc should accept deploy-prod with the documents moto exported: $(cat "$WORK/mini-exported.json")"

echo "▸ aws sts assume-role-with-web-identity in moto with the deploy-prod token"
NOW="$(date +%s)"
"${AWSCLI[@]}" sts assume-role-with-web-identity --role-arn "$ROLE_ARN" --role-session-name GitHubActions --web-identity-token "$DEPLOY" --duration-seconds 900 > "$WORK/moto-deploy.json" 2>&1 || fail "assume-role-with-web-identity: $(cat "$WORK/moto-deploy.json")"
python3 - "$WORK/moto-deploy.json" "$WORK/mini-deploy.json" "$NOW" <<'PY' || fail "moto and minioidc responses: $(cat "$WORK/moto-deploy.json")"
import json, sys
from datetime import datetime
moto, mini, now = json.load(open(sys.argv[1])), json.load(open(sys.argv[2])), int(sys.argv[3])
assert moto["Credentials"]["AccessKeyId"] and moto["Credentials"]["SessionToken"], moto
assert moto["AssumedRoleUser"]["Arn"] == mini["AssumedRoleUser"]["Arn"], (moto["AssumedRoleUser"], mini["AssumedRoleUser"])
expires = datetime.fromisoformat(moto["Credentials"]["Expiration"].replace("Z", "+00:00")).timestamp()
assert abs(expires - (now + 900)) < 120, (moto["Credentials"]["Expiration"], now)
missing = [f for f in ("SubjectFromWebIdentityToken", "Provider", "Audience") if f not in moto]
assert missing, "moto now returns %s; update the comparison table in docs/en.md" % [f for f in ("SubjectFromWebIdentityToken", "Provider", "Audience") if f in moto]
assert mini["SubjectFromWebIdentityToken"] == "repo:octo-org/octo-repo:environment:prod", mini
print("  both: %s, expiring in 900 s; moto omits %s, minioidc returns them" % (moto["AssumedRoleUser"]["Arn"], ", ".join(missing)))
PY
python3 - "$WORK/moto-deploy.json" > "$WORK/session.env" <<'PY'
import json, sys
c = json.load(open(sys.argv[1]))["Credentials"]
print("AWS_ACCESS_KEY_ID=%s AWS_SECRET_ACCESS_KEY=%s AWS_SESSION_TOKEN=%s" % (c["AccessKeyId"], c["SecretAccessKey"], c["SessionToken"]))
PY
# shellcheck disable=SC2046
env $(cat "$WORK/session.env") "${AWSCLI[@]}" sts get-caller-identity --query Arn --output text > "$WORK/whoami.txt" 2>&1 || fail "get-caller-identity with the session: $(cat "$WORK/whoami.txt")"
[[ "$(cat "$WORK/whoami.txt")" == "$MINI_ARN" ]] || fail "get-caller-identity: $(cat "$WORK/whoami.txt"), expected $MINI_ARN"
echo "  aws sts get-caller-identity with the session: $(cat "$WORK/whoami.txt")"

echo "▸ moto does not read the token: it returns credentials where STS refuses (and minioidc does)"
MOTO_ROWS=(
  "feature branch|$FEATURE|900|AccessDenied"
  "stranger's repository|$STRANGER|900|AccessDenied"
  "20-minute-old token|$(cat "$WORK/expired.jwt")|900|ExpiredTokenException"
  "not a JWT|not-a-jwt-at-all|900|InvalidIdentityToken"
  "7200 s above MaxSessionDuration|$DEPLOY|7200|ValidationError"
)
for row in "${MOTO_ROWS[@]}"; do
  IFS='|' read -r label token duration want <<< "$row"
  if ! "${AWSCLI[@]}" sts assume-role-with-web-identity --role-arn "$ROLE_ARN" --role-session-name GitHubActions --web-identity-token "$token" --duration-seconds "$duration" > "$WORK/moto-row.json" 2>&1; then
    fail "moto $MOTO_VERSION now refuses the $label ($(tail -1 "$WORK/moto-row.json")); update the comparison table in docs/en.md"
  fi
  CODE=$(assume "$WORK/mini-row.json" "$token" "$WORK/trust.json" --duration-seconds "$duration")
  GOT="$(outcome_code "$WORK/mini-row.json")"
  [[ "$CODE" == "1" && "$GOT" == "$want" ]] || fail "minioidc on the $label: exit $CODE, $GOT, expected $want: $(cat "$WORK/mini-row.json")"
  printf '  %-34s moto: credentials   minioidc: %s\n' "$label" "$GOT"
done

echo "▸ identity-provider controls: moto stores an aud-only trust policy that IAM refuses"
if ! "${AWSCLI[@]}" iam create-role --role-name cc-no-sub --assume-role-policy-document "file://$TP/github-no-sub.json" > "$WORK/no-sub.out" 2>&1; then
  grep -q "MalformedPolicyDocument" "$WORK/no-sub.out" || fail "create-role cc-no-sub: $(cat "$WORK/no-sub.out")"
  fail "moto $MOTO_VERSION now enforces identity-provider controls ($(tail -1 "$WORK/no-sub.out")); update the comparison table in docs/en.md"
fi
grep -q "MalformedPolicyDocument" "$WORK/audit-no-sub.json" || fail "the audit should say IAM answers MalformedPolicyDocument: $(cat "$WORK/audit-no-sub.json")"
echo "  moto: role cc-no-sub created; minioidc audit: $(field "$WORK/audit-no-sub.json" 0 severity) $(field "$WORK/audit-no-sub.json" 0 code)"

echo "SMOKE OK: cicd/ci-oidc"
