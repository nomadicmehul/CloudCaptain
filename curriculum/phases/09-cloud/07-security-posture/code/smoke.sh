#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."

LESSON="$PWD"
POSTURE=(python3 "$LESSON/code/main.py")
FIXTURE="$LESSON/code/fixtures/moto-bundle.json"
MOTO_VERSION="5.2.5"
CHECKOV_VERSION="3.3.29"
CHECKOV_WHEEL_SHA256="9a8b3918788faf8e18b181d826d284744b22b9d99c53cf1b67a2e84f0596796f"
ACCOUNT="123456789012"
REGION="us-east-1"
WORK="$(mktemp -d)"
PIDS=()
fail() {
  echo "SMOKE FAIL: $*" >&2
  if [[ -f "$WORK/moto.log" ]]; then echo "--- moto_server log (last 15 lines)" >&2; tail -15 "$WORK/moto.log" >&2; fi
  exit 1
}
cleanup() {
  for pid in ${PIDS[@]+"${PIDS[@]}"}; do kill "$pid" 2>/dev/null || true; done
  wait 2>/dev/null || true
  rm -rf "$WORK"
}
trap cleanup EXIT
sha256_of() { if command -v sha256sum >/dev/null; then sha256sum "$1" | awk '{print $1}'; else shasum -a 256 "$1" | awk '{print $1}'; fi; }

cat > "$WORK/named.py" <<'PY'
import json, sys

def named(path):
    return {(f["check"], f["name"]) for f in json.load(open(path))["findings"] if f["name"].startswith("cc-") or f["name"] in ("account", "root")}

if __name__ == "__main__":
    print(json.dumps(sorted(named(sys.argv[1]))))
PY

echo "▸ posture on the committed moto fixture: exit 1, both CIS numberings, the findings the lesson describes"
CODE=0; "${POSTURE[@]}" scan "$FIXTURE" > "$WORK/fixture.txt" || CODE=$?
[[ "$CODE" == "1" ]] || fail "scan of the fixture should exit 1 (findings at or above high), got $CODE: $(tail -5 "$WORK/fixture.txt")"
grep -q "sections are CIS AWS Foundations Benchmark v5.0.0" "$WORK/fixture.txt" || fail "text report header: $(head -3 "$WORK/fixture.txt")"
"${POSTURE[@]}" scan "$FIXTURE" --benchmark 3.0.0 --fail-on none > "$WORK/fixture-v3.txt" || fail "scan --fail-on none should exit 0"
grep -q "sg-admin-ipv4 .*CIS 5.2 \[EC2.53\]" "$WORK/fixture-v3.txt" || fail "v3.0.0 numbering for EC2.53 should be 5.2: $(grep sg-admin "$WORK/fixture-v3.txt")"
"${POSTURE[@]}" scan "$FIXTURE" --now +120d --json > "$WORK/fixture.json" || true
python3 - "$WORK/fixture.json" <<'PY' || fail "fixture findings"
import json, sys
sys.path.insert(0, sys.argv[1].rsplit("/", 1)[0])
from named import named
found = named(sys.argv[1])
must = {("s3-public-policy", "cc-vpc-wildcard"), ("nacl-admin-open", "cc-wide-range"), ("sg-admin-ipv4", "cc-all-open"), ("user-key-old", "cc-ci"), ("cloudtrail-no-multi-region", "account")}
never = {("nacl-admin-open", "cc-ssh-denied-first"), ("sg-admin-ipv4", "cc-web"), ("kms-rotation-off", "cc-rotated"), ("ebs-unencrypted", "cc-data-enc")}
assert must <= found, sorted(must - found)
assert not (never & found), sorted(never & found)
print("  %d named findings in the fixture at +120 days" % len(found))
PY

if ! python3 -m venv "$WORK/venv" > "$WORK/venv.log" 2>&1; then
  echo "  (python3 -m venv not available; moto and Checkov comparisons skipped)"
  echo "SMOKE OK: cloud/security-posture (fixture only)"
  exit 0
fi

echo "▸ background: Checkov $CHECKOV_VERSION into its own venv (wheel sha256 checked against the pinned digest)"
(
  set +e
  python3 -m venv "$WORK/checkov-venv" \
    && "$WORK/checkov-venv/bin/pip" download -q --disable-pip-version-check --no-deps --only-binary=:all: "checkov==$CHECKOV_VERSION" -d "$WORK/dl" \
    && [[ "$(sha256_of "$WORK/dl/checkov-$CHECKOV_VERSION-py3-none-any.whl")" == "$CHECKOV_WHEEL_SHA256" ]] \
    && "$WORK/checkov-venv/bin/pip" install -q --disable-pip-version-check "$WORK/dl/checkov-$CHECKOV_VERSION-py3-none-any.whl"
  echo "$?" > "$WORK/checkov.exit"
) > "$WORK/checkov-pip.log" 2>&1 &
PIDS+=("$!")
CHECKOV_PID="$!"

echo "▸ install moto[server]==$MOTO_VERSION into a venv and start moto_server on a free port"
PACKAGES=("moto[server]==$MOTO_VERSION")
if command -v aws >/dev/null; then AWS_BIN="$(command -v aws)"; else PACKAGES+=("awscli"); AWS_BIN="$WORK/venv/bin/aws"; fi
"$WORK/venv/bin/pip" install -q --disable-pip-version-check "${PACKAGES[@]}" > "$WORK/pip.log" 2>&1 || fail "pip install ${PACKAGES[*]}: $(tail -20 "$WORK/pip.log")"
"$WORK/venv/bin/python" -c 'import moto; print("  moto", moto.__version__)'
"$AWS_BIN" --version > "$WORK/aws-version.txt" 2>&1 || true
sed -n '1s/^/  /p' "$WORK/aws-version.txt"
PORT="$(python3 -c 'import socket; s = socket.socket(); s.bind(("127.0.0.1", 0)); print(s.getsockname()[1]); s.close()')"
"$WORK/venv/bin/moto_server" -H 127.0.0.1 -p "$PORT" > "$WORK/moto.log" 2>&1 &
PIDS+=("$!")
for _ in $(seq 1 150); do curl -s -o /dev/null "http://127.0.0.1:$PORT/moto-api/" && break; sleep 0.2; done
curl -s -o /dev/null "http://127.0.0.1:$PORT/moto-api/" || fail "moto_server did not start on port $PORT"

unset AWS_PROFILE AWS_SESSION_TOKEN AWS_ENDPOINT_URL
export AWS_ACCESS_KEY_ID=test AWS_SECRET_ACCESS_KEY=test AWS_DEFAULT_REGION="$REGION" AWS_PAGER=""
export AWS_CONFIG_FILE="$WORK/aws-config" AWS_SHARED_CREDENTIALS_FILE="$WORK/aws-credentials"
printf '[default]\nregion = %s\ns3 =\n  addressing_style = path\n' "$REGION" > "$AWS_CONFIG_FILE"
: > "$AWS_SHARED_CREDENTIALS_FILE"
EP="http://localhost:$PORT"
AWSCLI=("$AWS_BIN" --endpoint-url "$EP")
run() { "${AWSCLI[@]}" "$@" >> "$WORK/setup.log" 2>&1 || fail "aws $*: $(tail -5 "$WORK/setup.log")"; }
get() { "${AWSCLI[@]}" "$@" --output text 2>> "$WORK/setup.log" || fail "aws $*: $(tail -5 "$WORK/setup.log")"; }
[[ "$(get sts get-caller-identity --query Account)" == "$ACCOUNT" ]] || fail "the scenario assumes moto's default account $ACCOUNT"
S3CONTROL=1
if ! python3 -c 'import socket, sys; socket.getaddrinfo(sys.argv[1], 80)' "$ACCOUNT.localhost" 2>/dev/null; then
  S3CONTROL=0
  echo "  ($ACCOUNT.localhost does not resolve here; s3control puts the account ID in the host name, so the account-level Block Public Access call becomes a gap)"
fi

echo "▸ Use It: build the insecure account in moto with the AWS CLI (buckets, VPCs, groups, ACLs, volumes, instances, users, keys, trails)"
for bucket in cc-public-acl cc-public-policy cc-private cc-vpc-wildcard; do run s3api create-bucket --bucket "$bucket"; done
run s3api delete-public-access-block --bucket cc-public-acl
run s3api put-bucket-ownership-controls --bucket cc-public-acl --ownership-controls 'Rules=[{ObjectOwnership=BucketOwnerPreferred}]'
run s3api put-bucket-acl --bucket cc-public-acl --acl public-read
run s3api put-public-access-block --bucket cc-public-policy --public-access-block-configuration BlockPublicAcls=false,IgnorePublicAcls=false,BlockPublicPolicy=false,RestrictPublicBuckets=false
run s3api put-bucket-policy --bucket cc-public-policy --policy '{"Version":"2012-10-17","Statement":[{"Sid":"PublicRead","Effect":"Allow","Principal":"*","Action":"s3:GetObject","Resource":"arn:aws:s3:::cc-public-policy/*"}]}'
run s3api put-public-access-block --bucket cc-vpc-wildcard --public-access-block-configuration BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=false,RestrictPublicBuckets=false
run s3api put-bucket-policy --bucket cc-vpc-wildcard --policy '{"Version":"2012-10-17","Statement":[{"Sid":"AnyVpc","Effect":"Allow","Principal":"*","Action":"s3:GetObject","Resource":"arn:aws:s3:::cc-vpc-wildcard/*","Condition":{"StringLike":{"aws:SourceVpc":"vpc-*"}}}]}'
run s3api put-public-access-block --bucket cc-private --public-access-block-configuration BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true
run s3api put-bucket-policy --bucket cc-private --policy '{"Version":"2012-10-17","Statement":[{"Sid":"DenyInsecureTransport","Effect":"Deny","Principal":"*","Action":"s3:*","Resource":["arn:aws:s3:::cc-private","arn:aws:s3:::cc-private/*"],"Condition":{"Bool":{"aws:SecureTransport":"false"}}},{"Sid":"CloudTrailWrite","Effect":"Allow","Principal":{"Service":"cloudtrail.amazonaws.com"},"Action":"s3:PutObject","Resource":"arn:aws:s3:::cc-private/AWSLogs/*","Condition":{"StringEquals":{"aws:SourceAccount":"123456789012"}}}]}'
APP="$(get ec2 create-vpc --cidr-block 10.20.0.0/16 --tag-specifications 'ResourceType=vpc,Tags=[{Key=Name,Value=cc-app}]' --query Vpc.VpcId)"
LEGACY="$(get ec2 create-vpc --cidr-block 10.30.0.0/16 --tag-specifications 'ResourceType=vpc,Tags=[{Key=Name,Value=cc-legacy}]' --query Vpc.VpcId)"
run ec2 create-flow-logs --resource-type VPC --resource-ids "$APP" --traffic-type ALL --log-destination-type s3 --log-destination arn:aws:s3:::cc-private
DEFAULT_SG="$(get ec2 describe-security-groups --filters "Name=vpc-id,Values=$APP" Name=group-name,Values=default --query 'SecurityGroups[0].GroupId')"
run ec2 revoke-security-group-egress --group-id "$DEFAULT_SG" --ip-permissions '[{"IpProtocol":"-1","IpRanges":[{"CidrIp":"0.0.0.0/0"}]}]'
sg() { get ec2 create-security-group --group-name "$1" --description "$1" --vpc-id "$APP" --query GroupId; }
SSH="$(sg cc-ssh-open)"; RDP="$(sg cc-rdp-open-v6)"; ALL="$(sg cc-all-open)"; WEB="$(sg cc-web)"
run ec2 authorize-security-group-ingress --group-id "$SSH" --ip-permissions 'IpProtocol=tcp,FromPort=22,ToPort=22,IpRanges=[{CidrIp=0.0.0.0/0}]'
run ec2 authorize-security-group-ingress --group-id "$RDP" --ip-permissions 'IpProtocol=tcp,FromPort=3389,ToPort=3389,Ipv6Ranges=[{CidrIpv6=::/0}]'
run ec2 authorize-security-group-ingress --group-id "$ALL" --ip-permissions 'IpProtocol=-1,IpRanges=[{CidrIp=0.0.0.0/0}]'
run ec2 authorize-security-group-ingress --group-id "$WEB" --ip-permissions 'IpProtocol=tcp,FromPort=443,ToPort=443,IpRanges=[{CidrIp=0.0.0.0/0}]'
nacl() { get ec2 create-network-acl --vpc-id "$APP" --tag-specifications "ResourceType=network-acl,Tags=[{Key=Name,Value=$1}]" --query NetworkAcl.NetworkAclId; }
entry() { run ec2 create-network-acl-entry --network-acl-id "$1" --ingress --rule-number "$2" --protocol tcp --port-range "From=$3,To=$4" --cidr-block 0.0.0.0/0 --rule-action "$5"; }
OPEN="$(nacl cc-admin-open)"; DENIED="$(nacl cc-ssh-denied-first)"; WIDE="$(nacl cc-wide-range)"
entry "$OPEN" 100 22 22 allow
entry "$DENIED" 90 22 22 deny
entry "$DENIED" 100 22 22 allow
entry "$WIDE" 100 0 65535 allow
run ec2 create-volume --availability-zone "${REGION}a" --size 8 --tag-specifications 'ResourceType=volume,Tags=[{Key=Name,Value=cc-data-plain}]'
run ec2 create-volume --availability-zone "${REGION}a" --size 8 --encrypted --tag-specifications 'ResourceType=volume,Tags=[{Key=Name,Value=cc-data-enc}]'
run ec2 run-instances --image-id ami-12c6146b --instance-type t3.micro --metadata-options HttpTokens=optional --tag-specifications 'ResourceType=instance,Tags=[{Key=Name,Value=cc-imdsv1}]'
run ec2 run-instances --image-id ami-12c6146b --instance-type t3.micro --metadata-options HttpTokens=required,HttpEndpoint=enabled --tag-specifications 'ResourceType=instance,Tags=[{Key=Name,Value=cc-imdsv2}]'
run iam create-user --user-name cc-alice
run iam create-login-profile --user-name cc-alice --password 'cc-Alice-Passw0rd-2026!'
run iam create-access-key --user-name cc-alice
run iam create-user --user-name cc-bob
run iam create-login-profile --user-name cc-bob --password 'cc-Bob-Passw0rd-2026!'
SERIAL="$(get iam create-virtual-mfa-device --virtual-mfa-device-name cc-bob --outfile "$WORK/cc-bob-mfa.png" --bootstrap-method QRCodePNG --query VirtualMFADevice.SerialNumber)"
run iam enable-mfa-device --user-name cc-bob --serial-number "$SERIAL" --authentication-code1 123456 --authentication-code2 654321
run iam create-user --user-name cc-ci
run iam create-access-key --user-name cc-ci
run iam create-access-key --user-name cc-ci
run iam update-account-password-policy --minimum-password-length 8 --password-reuse-prevention 5
NOROT="$(get kms create-key --description cc-no-rotation --query KeyMetadata.KeyId)"
ROT="$(get kms create-key --description cc-rotated --query KeyMetadata.KeyId)"
run kms create-alias --alias-name alias/cc-no-rotation --target-key-id "$NOROT"
run kms create-alias --alias-name alias/cc-rotated --target-key-id "$ROT"
run kms enable-key-rotation --key-id "$ROT"
run cloudtrail create-trail --name cc-trail --s3-bucket-name cc-private
run cloudtrail start-logging --name cc-trail
run cloudtrail create-trail --name cc-org-trail --s3-bucket-name cc-private --is-multi-region-trail --enable-log-file-validation --kms-key-id alias/cc-rotated
echo "  VPCs $APP (cc-app) and $LEGACY (cc-legacy); the multi-Region trail exists but was never started"

echo "▸ Use It by hand: the benchmark's questions as AWS CLI queries, and what each one cannot see"
get ec2 describe-security-groups --filters Name=ip-permission.cidr,Values=0.0.0.0/0 Name=ip-permission.from-port,Values=22 --query 'SecurityGroups[].GroupName' > "$WORK/hand-sg22.txt"
get ec2 describe-network-acls --filters Name=default,Values=false Name=entry.cidr,Values=0.0.0.0/0 Name=entry.rule-action,Values=allow --query 'NetworkAcls[].Tags[?Key==`Name`]|[].Value' > "$WORK/hand-nacl.txt"
get ec2 describe-volumes --filters Name=encrypted,Values=false --query 'Volumes[].VolumeId' > "$WORK/hand-volumes.txt"
get ec2 describe-instances --query 'Reservations[].Instances[?MetadataOptions.HttpTokens!=`required`][].InstanceId' > "$WORK/hand-imds.txt"
run iam generate-credential-report
get iam get-credential-report --query Content > "$WORK/hand-report.b64"
python3 -c 'import base64, sys; sys.stdout.write(base64.b64decode(open(sys.argv[1]).read()).decode())' "$WORK/hand-report.b64" > "$WORK/hand-report.csv"
get cloudtrail get-trail-status --name cc-org-trail --query IsLogging > "$WORK/hand-logging.txt"
for bucket in cc-public-policy cc-private cc-vpc-wildcard; do
  printf '%s %s\n' "$bucket" "$("${AWSCLI[@]}" s3api get-bucket-policy-status --bucket "$bucket" --query PolicyStatus.IsPublic --output text 2>/dev/null || echo unsupported)"
done > "$WORK/hand-policy-status.txt"
grep -qw cc-ssh-open "$WORK/hand-sg22.txt" || fail "from-port=22 filter should find cc-ssh-open: $(cat "$WORK/hand-sg22.txt")"
grep -qw cc-ssh-denied-first "$WORK/hand-nacl.txt" || fail "the ACL filter cannot see rule order and should list cc-ssh-denied-first: $(cat "$WORK/hand-nacl.txt")"
grep -q '^cc-alice,.*' "$WORK/hand-report.csv" || fail "credential report should list cc-alice: $(head -3 "$WORK/hand-report.csv")"
[[ "$(tr -d '[:space:]' < "$WORK/hand-logging.txt")" == "False" ]] || fail "cc-org-trail should not be logging yet: $(cat "$WORK/hand-logging.txt")"
echo "  from-port=22 filter: $(tr '\t\n' '  ' < "$WORK/hand-sg22.txt")| allow-from-world ACLs: $(tr '\t\n' '  ' < "$WORK/hand-nacl.txt")"
sed 's/^/  policy status: /' "$WORK/hand-policy-status.txt"

echo "▸ posture collect against moto, then scan at +120 days: the same findings as the committed fixture"
"${POSTURE[@]}" collect --endpoint-url "$EP" --region "$REGION" --aws "$AWS_BIN" -o "$WORK/bundle1.json" 2> "$WORK/collect1.err" || fail "posture collect: $(cat "$WORK/collect1.err")"
cat "$WORK/collect1.err" | sed 's/^/  /'
"${POSTURE[@]}" scan "$WORK/bundle1.json" --now +120d --json > "$WORK/scan1.json" || true
python3 - "$WORK/scan1.json" "$WORK/fixture.json" "$S3CONTROL" "$WORK" <<'PY' || fail "fresh scan differs from the fixture"
import json, sys
sys.path.insert(0, sys.argv[4])
from named import named
fresh, expected = named(sys.argv[1]), named(sys.argv[2])
if sys.argv[3] == "0":
    expected.discard(("s3-account-bpa-off", "account"))
assert fresh == expected, ("only fresh", sorted(fresh - expected), "only fixture", sorted(expected - fresh))
counts = json.load(open(sys.argv[1]))["counts"]
print("  %d named findings, identical to the fixture; all findings by severity: %s" % (len(fresh), counts))
PY

echo "▸ posture against the by-hand answers: it finds what each query finds, and what the queries miss"
python3 - "$WORK/scan1.json" "$WORK" <<'PY' || fail "posture versus the CLI queries"
import csv, json, sys
work = sys.argv[2]
findings = json.load(open(sys.argv[1]))["findings"]
def names(check, port=None):
    return {f["name"] for f in findings if f["check"] == check and (port is None or port in f["details"].get("ports", []))}
def words(name):
    return set(open("%s/%s" % (work, name)).read().split())
sg22 = names("sg-admin-ipv4", 22) | names("sg-admin-ipv6", 22)
assert words("hand-sg22.txt") <= sg22 and "cc-all-open" in sg22 and "cc-all-open" not in words("hand-sg22.txt"), (sg22, words("hand-sg22.txt"))
print("  ports 22 open to the world: posture %s; the from-port filter missed cc-all-open (IpProtocol -1 has no port)" % sorted(sg22))
nacl = names("nacl-admin-open")
assert "cc-ssh-denied-first" in words("hand-nacl.txt") and "cc-ssh-denied-first" not in nacl, nacl
print("  ACLs letting the internet reach an admin port: posture %s; rule order rules out cc-ssh-denied-first" % sorted(nacl))
volumes = {f["resource"] for f in findings if f["check"] == "ebs-unencrypted"}
assert volumes == words("hand-volumes.txt"), (volumes, words("hand-volumes.txt"))
imds = {f["resource"] for f in findings if f["check"] == "ec2-imdsv1"}
assert imds == words("hand-imds.txt"), (imds, words("hand-imds.txt"))
rows = {r["user"]: r for r in csv.DictReader(open("%s/hand-report.csv" % work))}
no_mfa = {u for u, r in rows.items() if r["password_enabled"] == "true" and r["mfa_active"] != "true"}
assert no_mfa == names("user-console-no-mfa") == {"cc-alice"}, (no_mfa, names("user-console-no-mfa"))
status = dict(line.split() for line in open("%s/hand-policy-status.txt" % work) if line.strip())
public = names("s3-public-policy")
assert {"cc-public-policy", "cc-vpc-wildcard"} <= public and "cc-private" not in public, public
if status.get("cc-public-policy") in ("True", "False"):
    assert status["cc-public-policy"] == "True" and status["cc-private"] == "False", status
    print("  get-bucket-policy-status agrees on cc-public-policy (public) and cc-private (not public)")
    if status.get("cc-vpc-wildcard") != "True":
        print("  known emulator difference: moto calls cc-vpc-wildcard not public; AWS documents StringLike vpc-* as public, and so does posture")
else:
    print("  (get-bucket-policy-status not implemented by this moto; posture's public-policy rule is covered by the unit tests)")
print("  unencrypted volumes, IMDSv1 instances and console users without MFA match the queries exactly")
PY

echo "▸ run posture's own fix commands against moto, collect again, and check each fixed finding is gone"
cat > "$WORK/apply.py" <<'PY'
import json, subprocess, sys

aws, report_path, out_path = json.loads(sys.argv[1]), sys.argv[2], sys.argv[3]
APPLY = {"s3-public-acl", "s3-public-policy", "s3-bucket-bpa-off", "s3-account-bpa-off", "sg-admin-ipv4", "sg-admin-ipv6", "default-sg-open",
         "nacl-admin-open", "ebs-default-encryption-off", "ec2-imdsv1", "kms-rotation-off", "cloudtrail-no-validation", "cloudtrail-no-multi-region",
         "password-policy-length", "password-policy-reuse", "user-key-old"}
EMULATOR_GAPS = {"ec2-imdsv1"}
applied, refused = [], []
for finding in json.load(open(report_path))["findings"]:
    if finding["check"] not in APPLY or not finding["commands"] or not (finding["name"].startswith("cc-") or finding["name"] == "account"):
        continue
    ok = True
    for command in finding["commands"]:
        proc = subprocess.run(aws + command, capture_output=True, text=True)
        if proc.returncode != 0:
            ok = False
            if finding["check"] not in EMULATOR_GAPS:
                sys.exit("fix for %s %s failed: aws %s\n%s" % (finding["check"], finding["name"], " ".join(command), proc.stderr.strip()[-400:]))
            refused.append([finding["check"], finding["name"]])
            break
    if ok:
        applied.append([finding["check"], finding["name"]])
json.dump({"applied": applied, "refused": refused}, open(out_path, "w"))
print("  ran the commands of %d findings%s" % (len(applied), "; the emulator refused %s" % refused if refused else ""))
PY
python3 "$WORK/apply.py" "$(python3 -c 'import json, sys; print(json.dumps(sys.argv[1:]))' "${AWSCLI[@]}")" "$WORK/scan1.json" "$WORK/applied.json" || fail "applying fixes"
"${POSTURE[@]}" collect --endpoint-url "$EP" --region "$REGION" --aws "$AWS_BIN" -o "$WORK/bundle2.json" 2> "$WORK/collect2.err" || fail "second posture collect: $(cat "$WORK/collect2.err")"
"${POSTURE[@]}" scan "$WORK/bundle2.json" --now +120d --json > "$WORK/scan2.json" || true
python3 - "$WORK/applied.json" "$WORK/scan1.json" "$WORK/scan2.json" "$WORK" <<'PY' || fail "a fix did not clear its finding"
import json, sys
sys.path.insert(0, sys.argv[4])
from named import named
applied = [tuple(a) for a in json.load(open(sys.argv[1]))["applied"]]
before, after = named(sys.argv[2]), named(sys.argv[3])
severity = {(f["check"], f["name"]): f["severity"] for f in json.load(open(sys.argv[3]))["findings"]}
assert len(applied) >= 15, applied
for key in applied:
    if key[0] == "s3-public-policy":
        assert severity.get(key) == "low", (key, severity.get(key))
    else:
        assert key not in after, ("still reported after its fix ran", key)
assert ("s3-public-policy", "cc-vpc-wildcard") in after and severity[("s3-public-policy", "cc-vpc-wildcard")] == "low", severity.get(("s3-public-policy", "cc-vpc-wildcard"))
assert after <= before, ("new findings after fixing", sorted(after - before))
assert ("user-two-active-keys", "cc-ci") not in after, "deactivating cc-ci's old keys also clears two-active-keys"
print("  %d fixed findings gone; public policies now low (RestrictPublicBuckets fences them in); still open: %s" % (len(applied), sorted(after)))
PY

if [[ ! -f "$WORK/checkov.exit" ]]; then
  echo "▸ wait for the Checkov install"
  wait "$CHECKOV_PID" 2>/dev/null || true
fi
[[ "$(cat "$WORK/checkov.exit" 2>/dev/null)" == "0" ]] || fail "install checkov==$CHECKOV_VERSION (or its sha256 $CHECKOV_WHEEL_SHA256 did not match): $(tail -20 "$WORK/checkov-pip.log")"

echo "▸ the same misconfigurations as Terraform: Checkov $CHECKOV_VERSION before deploy versus posture after deploy"
TF="$WORK/tf"
mkdir -p "$TF"
cat > "$TF/main.tf" <<'EOF'
terraform {
  required_providers {
    aws = { source = "hashicorp/aws" }
  }
}

provider "aws" {
  region = "us-east-1"
}

resource "aws_vpc" "cc_app" {
  cidr_block = "10.20.0.0/16"
  tags       = { Name = "cc-app" }
}

resource "aws_vpc" "cc_legacy" {
  cidr_block = "10.30.0.0/16"
  tags       = { Name = "cc-legacy" }
}

resource "aws_default_security_group" "cc_app" {
  vpc_id = aws_vpc.cc_app.id
}

resource "aws_flow_log" "cc_app" {
  vpc_id               = aws_vpc.cc_app.id
  traffic_type         = "ALL"
  log_destination_type = "s3"
  log_destination      = aws_s3_bucket.cc_private.arn
}

resource "aws_security_group" "cc_ssh_open" {
  name        = "cc-ssh-open"
  description = "cc-ssh-open"
  vpc_id      = aws_vpc.cc_app.id
  ingress {
    from_port   = 22
    to_port     = 22
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }
}

resource "aws_security_group" "cc_rdp_open_v6" {
  name        = "cc-rdp-open-v6"
  description = "cc-rdp-open-v6"
  vpc_id      = aws_vpc.cc_app.id
  ingress {
    from_port        = 3389
    to_port          = 3389
    protocol         = "tcp"
    ipv6_cidr_blocks = ["::/0"]
  }
}

resource "aws_security_group" "cc_all_open" {
  name        = "cc-all-open"
  description = "cc-all-open"
  vpc_id      = aws_vpc.cc_app.id
  ingress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
}

resource "aws_security_group" "cc_web" {
  name        = "cc-web"
  description = "cc-web"
  vpc_id      = aws_vpc.cc_app.id
  ingress {
    from_port   = 443
    to_port     = 443
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }
}

resource "aws_network_acl" "cc_admin_open" {
  vpc_id = aws_vpc.cc_app.id
  tags   = { Name = "cc-admin-open" }
  ingress {
    rule_no    = 100
    protocol   = "tcp"
    action     = "allow"
    cidr_block = "0.0.0.0/0"
    from_port  = 22
    to_port    = 22
  }
}

resource "aws_network_acl" "cc_ssh_denied_first" {
  vpc_id = aws_vpc.cc_app.id
  tags   = { Name = "cc-ssh-denied-first" }
  ingress {
    rule_no    = 90
    protocol   = "tcp"
    action     = "deny"
    cidr_block = "0.0.0.0/0"
    from_port  = 22
    to_port    = 22
  }
  ingress {
    rule_no    = 100
    protocol   = "tcp"
    action     = "allow"
    cidr_block = "0.0.0.0/0"
    from_port  = 22
    to_port    = 22
  }
}

resource "aws_network_acl" "cc_wide_range" {
  vpc_id = aws_vpc.cc_app.id
  tags   = { Name = "cc-wide-range" }
  ingress {
    rule_no    = 100
    protocol   = "tcp"
    action     = "allow"
    cidr_block = "0.0.0.0/0"
    from_port  = 0
    to_port    = 65535
  }
}

resource "aws_ebs_encryption_by_default" "account" {
  enabled = false
}

resource "aws_ebs_volume" "cc_data_plain" {
  availability_zone = "us-east-1a"
  size              = 8
  tags              = { Name = "cc-data-plain" }
}

resource "aws_ebs_volume" "cc_data_enc" {
  availability_zone = "us-east-1a"
  size              = 8
  encrypted         = true
  kms_key_id        = aws_kms_key.cc_rotated.arn
  tags              = { Name = "cc-data-enc" }
}

resource "aws_instance" "cc_imdsv1" {
  ami           = "ami-12c6146b"
  instance_type = "t3.micro"
  tags          = { Name = "cc-imdsv1" }
  metadata_options {
    http_tokens = "optional"
  }
}

resource "aws_instance" "cc_imdsv2" {
  ami           = "ami-12c6146b"
  instance_type = "t3.micro"
  tags          = { Name = "cc-imdsv2" }
  metadata_options {
    http_tokens   = "required"
    http_endpoint = "enabled"
  }
}

resource "aws_s3_bucket" "cc_public_acl" {
  bucket = "cc-public-acl"
}

resource "aws_s3_bucket_ownership_controls" "cc_public_acl" {
  bucket = aws_s3_bucket.cc_public_acl.id
  rule {
    object_ownership = "BucketOwnerPreferred"
  }
}

resource "aws_s3_bucket_acl" "cc_public_acl" {
  bucket = aws_s3_bucket.cc_public_acl.id
  acl    = "public-read"
}

resource "aws_s3_bucket" "cc_public_policy" {
  bucket = "cc-public-policy"
}

resource "aws_s3_bucket_public_access_block" "cc_public_policy" {
  bucket                  = aws_s3_bucket.cc_public_policy.id
  block_public_acls       = false
  ignore_public_acls      = false
  block_public_policy     = false
  restrict_public_buckets = false
}

resource "aws_s3_bucket_policy" "cc_public_policy" {
  bucket = aws_s3_bucket.cc_public_policy.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid       = "PublicRead"
      Effect    = "Allow"
      Principal = "*"
      Action    = "s3:GetObject"
      Resource  = "${aws_s3_bucket.cc_public_policy.arn}/*"
    }]
  })
}

resource "aws_s3_bucket" "cc_private" {
  bucket = "cc-private"
}

resource "aws_s3_bucket_public_access_block" "cc_private" {
  bucket                  = aws_s3_bucket.cc_private.id
  block_public_acls       = true
  ignore_public_acls      = true
  block_public_policy     = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_policy" "cc_private" {
  bucket = aws_s3_bucket.cc_private.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid       = "DenyInsecureTransport"
        Effect    = "Deny"
        Principal = "*"
        Action    = "s3:*"
        Resource  = [aws_s3_bucket.cc_private.arn, "${aws_s3_bucket.cc_private.arn}/*"]
        Condition = { Bool = { "aws:SecureTransport" = "false" } }
      },
      {
        Sid       = "CloudTrailWrite"
        Effect    = "Allow"
        Principal = { Service = "cloudtrail.amazonaws.com" }
        Action    = "s3:PutObject"
        Resource  = "${aws_s3_bucket.cc_private.arn}/AWSLogs/*"
        Condition = { StringEquals = { "aws:SourceAccount" = "123456789012" } }
      }
    ]
  })
}

resource "aws_iam_account_password_policy" "account" {
  minimum_password_length   = 8
  password_reuse_prevention = 5
}

resource "aws_kms_key" "cc_no_rotation" {
  description = "cc-no-rotation"
}

resource "aws_kms_key" "cc_rotated" {
  description         = "cc-rotated"
  enable_key_rotation = true
}

resource "aws_cloudtrail" "cc_trail" {
  name           = "cc-trail"
  s3_bucket_name = aws_s3_bucket.cc_private.id
}

resource "aws_cloudtrail" "cc_org_trail" {
  name                       = "cc-org-trail"
  s3_bucket_name             = aws_s3_bucket.cc_private.id
  is_multi_region_trail      = true
  enable_log_file_validation = true
  kms_key_id                 = aws_kms_key.cc_rotated.arn
}
EOF
if command -v tofu >/dev/null; then
  tofu fmt -write=false -list=false "$TF" > "$WORK/fmt.txt" 2>&1 || fail "tofu fmt cannot parse main.tf: $(cat "$WORK/fmt.txt")"
  echo "  tofu fmt parses main.tf (syntax only; no provider download)"
else
  echo "  (tofu not installed; HCL syntax check skipped)"
fi
CODE=0
"$WORK/checkov-venv/bin/checkov" -d "$TF" --framework terraform -o json --compact --skip-download > "$WORK/checkov.json" 2> "$WORK/checkov.err" || CODE=$?
[[ "$CODE" == "0" || "$CODE" == "1" ]] || fail "checkov exited $CODE: $(tail -20 "$WORK/checkov.err")"
python3 - "$WORK/scan1.json" "$WORK/checkov.json" <<'PY' || fail "posture and Checkov disagree on an overlapping check"
import json, sys

PAIRS = [
    (("sg-admin-ipv4", "sg-admin-ipv6"), 22, "CKV_AWS_24", "aws_security_group"),
    (("sg-admin-ipv4", "sg-admin-ipv6"), 3389, "CKV_AWS_25", "aws_security_group"),
    (("nacl-admin-open",), 22, "CKV_AWS_232", "aws_network_acl"),
    (("nacl-admin-open",), 3389, "CKV_AWS_231", "aws_network_acl"),
    (("default-sg-open",), None, "CKV2_AWS_12", "aws_vpc"),
    (("vpc-flow-logs-off",), None, "CKV2_AWS_11", "aws_vpc"),
    (("ebs-unencrypted",), None, "CKV_AWS_3", "aws_ebs_volume"),
    (("ebs-default-encryption-off",), None, "CKV_AWS_106", "aws_ebs_encryption_by_default"),
    (("ec2-imdsv1",), None, "CKV_AWS_79", "aws_instance"),
    (("s3-public-acl",), None, "CKV_AWS_20", "aws_s3_bucket"),
    (("s3-public-policy",), None, "CKV_AWS_70", "aws_s3_bucket_policy"),
    (("s3-bucket-bpa-off",), None, "CKV2_AWS_6", "aws_s3_bucket"),
    (("password-policy-length",), None, "CKV_AWS_10", "aws_iam_account_password_policy"),
    (("password-policy-reuse",), None, "CKV_AWS_13", "aws_iam_account_password_policy"),
    (("kms-rotation-off",), None, "CKV_AWS_7", "aws_kms_key"),
    (("cloudtrail-no-validation",), None, "CKV_AWS_36", "aws_cloudtrail"),
    (("cloudtrail-no-kms",), None, "CKV_AWS_35", "aws_cloudtrail"),
]
KNOWN = {("CKV_AWS_231", "cc_wide_range"), ("CKV_AWS_232", "cc_wide_range")}
findings = json.load(open(sys.argv[1]))["findings"]
doc = json.load(open(sys.argv[2]))
doc = doc[0] if isinstance(doc, list) else doc
print("  checkov %s: %s" % (doc.get("summary", {}).get("checkov_version"), {k: doc.get("summary", {}).get(k) for k in ("passed", "failed", "resource_count")}))
seen, failed = {}, {}
for key in ("passed_checks", "failed_checks"):
    for check in doc["results"].get(key) or []:
        rtype, _, name = check["resource"].partition(".")
        seen.setdefault((check["check_id"], rtype), set()).add(name)
        if key == "failed_checks":
            failed.setdefault(check["check_id"], set()).add(name)
problems = []
for checks, port, ckv, rtype in PAIRS:
    universe = seen.get((ckv, rtype), set())
    if not universe:
        problems.append("%s evaluated no %s resources" % (ckv, rtype))
        continue
    mine = {f["name"].replace("-", "_") for f in findings if f["check"] in checks and (port is None or port in f["details"].get("ports", []))} & universe
    theirs = failed.get(ckv, set()) & universe
    mine, theirs = {n for n in mine if (ckv, n) not in KNOWN}, {n for n in theirs if (ckv, n) not in KNOWN}
    label = "/".join(checks) + (" port %d" % port if port else "")
    print("  %-6s %-46s %-12s %s" % ("agree" if mine == theirs else "DIFFER", label, ckv, sorted(mine) or "-"))
    if mine != theirs:
        problems.append("%s vs %s: posture %s, checkov %s" % (label, ckv, sorted(mine), sorted(theirs)))
wide = {f["details"].get("ports", [None])[0] for f in findings if f["check"] == "nacl-admin-open" and f["name"] == "cc-wide-range"}
assert wide, "posture must flag cc-wide-range (allow tcp 0-65535 from the world)"
for ckv, name in sorted(KNOWN):
    print("  known  %s on %s: posture flagged, checkov %s (Checkov 3.3.29 tests `from_port and to_port`, and port 0 is falsy)" % (ckv, name, "flagged" if name in failed.get(ckv, set()) else "passed"))
trail = "cc_trail" in failed.get("CKV_AWS_67", set())
multi = any(f["check"] == "cloudtrail-no-multi-region" for f in findings)
assert multi, "posture should report the account without a logging multi-Region trail"
print("  known  CKV_AWS_67 fails each single-Region trail (cc_trail: %s); CIS 3.1 is per account and needs IsLogging, which only the running account has" % ("flagged" if trail else "passed"))
if problems:
    sys.exit("disagreements: " + "; ".join(problems))
print("  %d of %d overlapping checks agree" % (len(PAIRS), len(PAIRS)))
PY

echo "SMOKE OK: cloud/security-posture"
