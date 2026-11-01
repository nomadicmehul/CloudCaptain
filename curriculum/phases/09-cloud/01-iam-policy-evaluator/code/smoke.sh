#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."

LESSON_DIR="$PWD"
IAMEVAL=(python3 "$LESSON_DIR/code/main.py")
FIX="$LESSON_DIR/code/fixtures"
POL="$FIX/policies"
MOTO_VERSION="5.2.5"
ACCOUNT_ID="123456789012"
WORK="$(mktemp -d)"
MOTO_PID=""
fail() {
  echo "SMOKE FAIL: $*" >&2
  if [[ -f "$WORK/moto.log" ]]; then echo "--- moto_server log (last 20 lines)" >&2; tail -20 "$WORK/moto.log" >&2; fi
  exit 1
}
cleanup() {
  if [[ -n "$MOTO_PID" ]]; then
    kill "$MOTO_PID" 2>/dev/null || true
    wait "$MOTO_PID" 2>/dev/null || true
  fi
  rm -rf "$WORK"
}
trap cleanup EXIT
expect_exit() { local want="$1" got=0; shift; "$@" || got=$?; [[ "$got" == "$want" ]] || fail "expected exit $want, got $got from: $*"; }

echo "▸ iameval on the decisions AWS documents (evaluation logic, boundaries, cross-account, Principal, variables, AccessDenied messages)"
"${IAMEVAL[@]}" scenarios "$FIX/aws-doc-scenarios.json" --json > "$WORK/scenarios.json" || fail "documented scenarios: $("${IAMEVAL[@]}" scenarios "$FIX/aws-doc-scenarios.json" | grep FAIL -A2)"
python3 - "$WORK/scenarios.json" "$FIX/aws-doc-conditions.json" "$LESSON_DIR/code" <<'PY' || fail "documented examples"
import json, sys
rows = json.load(open(sys.argv[1]))
assert len(rows) >= 40 and all(r["ok"] for r in rows), [r for r in rows if not r["ok"]]
kinds = {r["policy_type"] for r in rows}
assert {"identity-based policy", "resource-based policy", "permissions boundary", "session policy", "service control policy", "resource control policy", "role trust policy"} <= kinds, kinds
sys.path.insert(0, sys.argv[3])
import main
conditions = json.load(open(sys.argv[2]))["rows"]
wrong = [c for c in conditions if main.condition_result(c["condition"], main.Context(c["context"]), True)[0] != c["expect"]]
assert not wrong, wrong
print("  %d scenario decisions and %d condition-table rows from docs.aws.amazon.com, all as documented" % (len(rows), len(conditions)))
PY

echo "▸ iameval on the lesson account: the deciding statement and the AccessDenied text"
"${IAMEVAL[@]}" eval --principal "arn:aws:iam::$ACCOUNT_ID:user/alice" --action s3:GetObject --resource arn:aws:s3:::cc-data/public/a.txt --auth-details "$FIX/account.json" > "$WORK/allow.txt" || fail "alice should read public/a.txt: $(cat "$WORK/allow.txt")"
grep -q 'statement "ReadData" (Allow) in identity-based policy arn:aws:iam::123456789012:policy/ReadData (via group readers)' "$WORK/allow.txt" || fail "allow reason: $(cat "$WORK/allow.txt")"
expect_exit 1 "${IAMEVAL[@]}" eval --principal "arn:aws:iam::$ACCOUNT_ID:user/alice" --action s3:GetObject --resource arn:aws:s3:::cc-data/secret/b.txt --auth-details "$FIX/account.json" > "$WORK/deny.txt"
grep -q 'AccessDenied: User: arn:aws:iam::123456789012:user/alice is not authorized to perform: s3:GetObject on resource: arn:aws:s3:::cc-data/secret/b.txt with an explicit deny in an identity-based policy' "$WORK/deny.txt" || fail "deny text: $(cat "$WORK/deny.txt")"
expect_exit 1 "${IAMEVAL[@]}" validate "$POL/bob-poweruser.json" > "$WORK/validate.txt"
grep -q "SECURITY_WARNING *allow-with-notaction" "$WORK/validate.txt" || fail "validate: $(cat "$WORK/validate.txt")"
"${IAMEVAL[@]}" explain "User: arn:aws:iam::123456789012:user/John is not authorized to perform: codecommit:ListRepositories because no service control policy allows the codecommit:ListRepositories action" --json > "$WORK/explain.json" || fail "explain"
python3 -c 'import json,sys; d=json.load(open(sys.argv[1])); assert (d["decision"], d["policy_type"]) == ("implicitDeny", "service control policy"), d' "$WORK/explain.json" || fail "explain: $(cat "$WORK/explain.json")"
tail -1 "$WORK/deny.txt"

echo "▸ install moto[server]==$MOTO_VERSION into a venv and start moto_server"
if ! python3 -m venv "$WORK/venv" > "$WORK/venv.log" 2>&1; then
  echo "  (python3 -m venv not available; moto comparison skipped)"
  echo "SMOKE OK: cloud/iam-policy-evaluator (fixtures only)"
  exit 0
fi
PACKAGES=("moto[server]==$MOTO_VERSION")
if command -v aws >/dev/null; then AWS_BIN="$(command -v aws)"; else PACKAGES+=("awscli"); AWS_BIN="$WORK/venv/bin/aws"; fi
"$WORK/venv/bin/pip" install -q --disable-pip-version-check "${PACKAGES[@]}" > "$WORK/pip.log" 2>&1 || fail "pip install ${PACKAGES[*]}: $(tail -20 "$WORK/pip.log")"
"$WORK/venv/bin/python" -c 'import moto; print("  moto", moto.__version__)'
"$AWS_BIN" --version 2>&1 | sed 's/^/  /'
PORT="$(python3 -c 'import socket; s = socket.socket(); s.bind(("127.0.0.1", 0)); print(s.getsockname()[1]); s.close()')"
"$WORK/venv/bin/moto_server" -H 127.0.0.1 -p "$PORT" > "$WORK/moto.log" 2>&1 &
MOTO_PID=$!
ENDPOINT="http://127.0.0.1:$PORT"
for _ in $(seq 1 150); do curl -sf -o /dev/null "$ENDPOINT/moto-api/data.json" && break; sleep 0.2; done
curl -sf -o /dev/null "$ENDPOINT/moto-api/data.json" || fail "moto_server did not answer on $ENDPOINT"

touch "$WORK/aws-config" "$WORK/aws-credentials"
export AWS_CONFIG_FILE="$WORK/aws-config" AWS_SHARED_CREDENTIALS_FILE="$WORK/aws-credentials"
export AWS_ACCESS_KEY_ID=setup AWS_SECRET_ACCESS_KEY=setup AWS_DEFAULT_REGION=us-east-1 AWS_PAGER="" AWS_EC2_METADATA_DISABLED=true
export AWS_REQUEST_CHECKSUM_CALCULATION=when_required AWS_RESPONSE_CHECKSUM_VALIDATION=when_required
unset AWS_PROFILE AWS_SESSION_TOKEN 2>/dev/null || true
AWSCLI=("$AWS_BIN" --endpoint-url "$ENDPOINT")
run() { "$@" > "$WORK/last.out" 2>&1 || fail "$*: $(cat "$WORK/last.out")"; }

echo "▸ Use It: build the lesson account in moto with the AWS CLI (users, a group, managed and inline policies, two roles, a boundary, a bucket policy)"
[[ "$("${AWSCLI[@]}" sts get-caller-identity --query Account --output text)" == "$ACCOUNT_ID" ]] || fail "the policy fixtures assume moto's default account $ACCOUNT_ID"
echo "hello" > "$WORK/hello.txt"
run "${AWSCLI[@]}" s3api create-bucket --bucket cc-data
run "${AWSCLI[@]}" s3api create-bucket --bucket cc-logs
for key in public/a.txt secret/b.txt home/carol/notes.txt tmp/keep.txt; do run "${AWSCLI[@]}" s3api put-object --bucket cc-data --key "$key" --body "$WORK/hello.txt"; done
run "${AWSCLI[@]}" s3api put-object --bucket cc-logs --key app/2026-11-01.txt --body "$WORK/hello.txt"
run "${AWSCLI[@]}" s3api put-bucket-policy --bucket cc-logs --policy "file://$POL/logs-bucket-policy.json"
"${AWSCLI[@]}" sqs create-queue --queue-name cc-jobs --query QueueUrl --output text > "$WORK/queue-url" 2>&1 || fail "create-queue: $(cat "$WORK/queue-url")"
run "${AWSCLI[@]}" iam create-policy --policy-name ReadData --policy-document "file://$POL/read-data.json"
run "${AWSCLI[@]}" iam create-policy --policy-name ListOnly --policy-document "file://$POL/list-only.json"
run "${AWSCLI[@]}" iam create-group --group-name readers
run "${AWSCLI[@]}" iam attach-group-policy --group-name readers --policy-arn "arn:aws:iam::$ACCOUNT_ID:policy/ReadData"
run "${AWSCLI[@]}" iam create-user --user-name alice --tags Key=team,Value=data
run "${AWSCLI[@]}" iam add-user-to-group --group-name readers --user-name alice
run "${AWSCLI[@]}" iam put-user-policy --user-name alice --policy-name deny-secret --policy-document "file://$POL/alice-deny-secret.json"
run "${AWSCLI[@]}" iam put-user-policy --user-name alice --policy-name assume-roles --policy-document "file://$POL/alice-assume-roles.json"
run "${AWSCLI[@]}" iam create-user --user-name bob
run "${AWSCLI[@]}" iam put-user-policy --user-name bob --policy-name poweruser --policy-document "file://$POL/bob-poweruser.json"
run "${AWSCLI[@]}" iam put-user-policy --user-name bob --policy-name no-delete --policy-document "file://$POL/bob-no-delete.json"
run "${AWSCLI[@]}" iam create-user --user-name carol
run "${AWSCLI[@]}" iam put-user-policy --user-name carol --policy-name home --policy-document "file://$POL/carol-home.json"
run "${AWSCLI[@]}" iam create-user --user-name erin
run "${AWSCLI[@]}" iam create-role --role-name reader --assume-role-policy-document "file://$POL/reader-trust.json" --permissions-boundary "arn:aws:iam::$ACCOUNT_ID:policy/ListOnly" --tags Key=team,Value=data
run "${AWSCLI[@]}" iam put-role-policy --role-name reader --policy-name read --policy-document "file://$POL/reader-read.json"
run "${AWSCLI[@]}" iam create-role --role-name auditor --assume-role-policy-document "file://$POL/auditor-trust.json"
run "${AWSCLI[@]}" iam attach-role-policy --role-name auditor --policy-arn "arn:aws:iam::$ACCOUNT_ID:policy/ReadData"
mkdir -p "$WORK/keys"
for user in alice bob carol erin; do "${AWSCLI[@]}" iam create-access-key --user-name "$user" > "$WORK/keys/$user.json" 2> "$WORK/key.err" || fail "create-access-key $user: $(cat "$WORK/key.err")"; done

echo "▸ aws iam get-account-authorization-details: the dump moto returns holds exactly the fixture's policies"
"${AWSCLI[@]}" iam get-account-authorization-details > "$WORK/account.json" 2> "$WORK/dump.err" || fail "get-account-authorization-details: $(cat "$WORK/dump.err")"
python3 - "$WORK/account.json" "$FIX/account.json" <<'PY' || fail "the exported dump differs from code/fixtures/account.json"
import json, sys
real, fixture = (json.load(open(p)) for p in sys.argv[1:3])
def users(d):
    return {u["UserName"]: (sorted(u.get("GroupList") or []), {p["PolicyName"]: p["PolicyDocument"] for p in u.get("UserPolicyList") or []}, sorted(a["PolicyArn"] for a in u.get("AttachedManagedPolicies") or []), {t["Key"]: t["Value"] for t in u.get("Tags") or []}) for u in d["UserDetailList"]}
def roles(d):
    return {r["RoleName"]: (r["AssumeRolePolicyDocument"], {p["PolicyName"]: p["PolicyDocument"] for p in r.get("RolePolicyList") or []}, sorted(a["PolicyArn"] for a in r.get("AttachedManagedPolicies") or []), (r.get("PermissionsBoundary") or {}).get("PermissionsBoundaryArn")) for r in d["RoleDetailList"]}
def groups(d):
    return {g["GroupName"]: sorted(a["PolicyArn"] for a in g.get("AttachedManagedPolicies") or []) for g in d["GroupDetailList"]}
def managed(d):
    return {p["Arn"]: next(v["Document"] for v in p["PolicyVersionList"] if v["IsDefaultVersion"]) for p in d["Policies"]}
for name, view in (("users", users), ("roles", roles), ("groups", groups), ("managed policies", managed)):
    assert view(real) == view(fixture), (name, view(real), view(fixture))
print("  users %d, groups %d, roles %d, managed policies %d: identical to the fixture" % (len(real["UserDetailList"]), len(real["GroupDetailList"]), len(real["RoleDetailList"]), len(real["Policies"])))
PY

echo "▸ aws iam simulate-custom-policy against moto"
if "${AWSCLI[@]}" iam simulate-custom-policy --policy-input-list "file://$POL/read-data.json" --action-names s3:GetObject s3:PutObject --resource-arns arn:aws:s3:::cc-data/public/a.txt > "$WORK/simulate.json" 2> "$WORK/simulate.err"; then
  "${IAMEVAL[@]}" eval --principal "arn:aws:iam::$ACCOUNT_ID:user/probe" --policy "$POL/read-data.json" --action s3:GetObject --action s3:PutObject --resource arn:aws:s3:::cc-data/public/a.txt --json > "$WORK/mine-simulate.json" || true
  python3 - "$WORK/simulate.json" "$WORK/mine-simulate.json" <<'PY' || fail "simulate-custom-policy differs from iameval"
import json, sys
real = {r["EvalActionName"]: r["EvalDecision"] for r in json.load(open(sys.argv[1]))["EvaluationResults"]}
mine = {r["EvalActionName"]: r["EvalDecision"] for r in json.load(open(sys.argv[2]))["EvaluationResults"]}
assert real == mine, (real, mine)
print("  simulate-custom-policy and iameval agree:", real)
PY
else
  echo "  (moto $MOTO_VERSION does not implement iam simulate-custom-policy; iameval is compared with moto's enforcement instead)"
fi

echo "▸ switch on moto's IAM enforcement (POST /moto-api/reset-auth 0): unknown keys are now refused"
curl -sf -X POST -H 'Content-Type: text/plain' --data-binary 0 "$ENDPOINT/moto-api/reset-auth" > "$WORK/reset.json" || fail "reset-auth"
if "${AWSCLI[@]}" iam list-users > "$WORK/setup-creds.out" 2>&1; then fail "the setup credentials should be refused once enforcement is on"; fi
grep -q "InvalidClientTokenId" "$WORK/setup-creds.out" || fail "expected InvalidClientTokenId: $(cat "$WORK/setup-creds.out")"

cat > "$WORK/matrix.py" <<'PY'
import json, os, re, subprocess, sys

aws, iameval, work, pol, account = json.loads(sys.argv[1]), json.loads(sys.argv[2]), sys.argv[3], sys.argv[4], sys.argv[5]
dump, logs = os.path.join(work, "account.json"), os.path.join(pol, "logs-bucket-policy.json")
queue_url = open(os.path.join(work, "queue-url")).read().strip()
queue = "arn:aws:sqs:us-east-1:%s:cc-jobs" % account
creds = {}
for user in ("alice", "bob", "carol", "erin"):
    key = json.load(open(os.path.join(work, "keys", user + ".json")))["AccessKey"]
    creds[user] = ("arn:aws:iam::%s:user/%s" % (account, user), {"AWS_ACCESS_KEY_ID": key["AccessKeyId"], "AWS_SECRET_ACCESS_KEY": key["SecretAccessKey"]})
out = os.path.join(work, "object.out")
role = lambda name: "arn:aws:iam::%s:role/%s" % (account, name)
obj = lambda bucket, key: "arn:aws:s3:::%s/%s" % (bucket, key)
SAME = None
MOTO_VARIABLES = ("denied", "moto does not substitute policy variables such as ${aws:username}")
MOTO_CONDITIONS = ("allowed", "moto ignores conditions outside role trust policies")
MOTO_RBP = ("denied", "moto never lets a bucket policy grant, and applies its Deny statements to object requests with conditions ignored")
MOTO_BOUNDARY = ("allowed", "moto stores permissions boundaries but does not enforce them")
MOTO_SESSION = ("allowed", "moto ignores session policies passed to AssumeRole")
MOTO_MISSING_KEY = ("allowed", "moto checks trust-policy conditions only against values the request carries")
ROWS = [
    ("alice", ["s3api", "list-objects-v2", "--bucket", "cc-data"], "s3:ListBucket", "arn:aws:s3:::cc-data", {}, [], "allowed", SAME),
    ("alice", ["s3api", "get-object", "--bucket", "cc-data", "--key", "public/a.txt", out], "s3:GetObject", obj("cc-data", "public/a.txt"), {}, [], "allowed", SAME),
    ("alice", ["s3api", "get-object", "--bucket", "cc-data", "--key", "secret/b.txt", out], "s3:GetObject", obj("cc-data", "secret/b.txt"), {}, [], "explicitDeny", SAME),
    ("alice", ["s3api", "put-object", "--bucket", "cc-data", "--key", "public/new.txt"], "s3:PutObject", obj("cc-data", "public/new.txt"), {}, [], "implicitDeny", SAME),
    ("alice", ["s3api", "list-objects-v2", "--bucket", "cc-logs"], "s3:ListBucket", "arn:aws:s3:::cc-logs", {}, ["--resource-policy", logs], "implicitDeny", SAME),
    ("alice", ["iam", "list-users"], "iam:ListUsers", "*", {}, [], "implicitDeny", SAME),
    ("alice", ["sqs", "send-message", "--queue-url", queue_url, "--message-body", "hi"], "sqs:SendMessage", queue, {}, [], "implicitDeny", SAME),
    ("alice", ["sts", "assume-role", "--role-arn", role("reader"), "--role-session-name", "cc-smoke", "--external-id", "wrong"], "sts:AssumeRole", role("reader"), {"sts:ExternalId": "wrong"}, [], "implicitDeny", SAME),
    ("alice", ["sts", "assume-role", "--role-arn", role("reader"), "--role-session-name", "cc-smoke"], "sts:AssumeRole", role("reader"), {}, [], "implicitDeny", MOTO_MISSING_KEY),
    ("alice", ["sts", "assume-role", "--role-arn", role("reader"), "--role-session-name", "cc-smoke", "--external-id", "cc-42"], "sts:AssumeRole", role("reader"), {"sts:ExternalId": "cc-42"}, [], "allowed", SAME, "reader"),
    ("alice", ["sts", "assume-role", "--role-arn", role("auditor"), "--role-session-name", "cc-smoke", "--policy", "file://" + os.path.join(pol, "session-list-only.json")], "sts:AssumeRole", role("auditor"), {}, [], "allowed", SAME, "auditor"),
    ("bob", ["s3api", "put-object", "--bucket", "cc-data", "--key", "tmp/bob.txt"], "s3:PutObject", obj("cc-data", "tmp/bob.txt"), {}, [], "allowed", SAME),
    ("bob", ["s3api", "delete-object", "--bucket", "cc-data", "--key", "tmp/keep.txt"], "s3:DeleteObject", obj("cc-data", "tmp/keep.txt"), {}, [], "explicitDeny", SAME),
    ("bob", ["iam", "list-users"], "iam:ListUsers", "*", {}, [], "implicitDeny", SAME),
    ("bob", ["sqs", "send-message", "--queue-url", queue_url, "--message-body", "hi"], "sqs:SendMessage", queue, {}, [], "allowed", SAME),
    ("bob", ["sts", "assume-role", "--role-arn", role("auditor"), "--role-session-name", "cc-smoke"], "sts:AssumeRole", role("auditor"), {}, [], "implicitDeny", SAME),
    ("carol", ["s3api", "put-object", "--bucket", "cc-data", "--key", "home/carol/new.txt"], "s3:PutObject", obj("cc-data", "home/carol/new.txt"), {}, [], "allowed", MOTO_VARIABLES),
    ("carol", ["s3api", "list-objects-v2", "--bucket", "cc-data", "--prefix", "home/carol/"], "s3:ListBucket", "arn:aws:s3:::cc-data", {"s3:prefix": "home/carol/"}, [], "allowed", SAME),
    ("carol", ["s3api", "list-objects-v2", "--bucket", "cc-data", "--prefix", "home/bob/"], "s3:ListBucket", "arn:aws:s3:::cc-data", {"s3:prefix": "home/bob/"}, [], "implicitDeny", MOTO_CONDITIONS),
    ("erin", ["s3api", "get-object", "--bucket", "cc-logs", "--key", "app/2026-11-01.txt", out], "s3:GetObject", obj("cc-logs", "app/2026-11-01.txt"), {}, ["--resource-policy", logs], "allowed", MOTO_RBP),
    ("erin", ["s3api", "get-object", "--bucket", "cc-data", "--key", "public/a.txt", out], "s3:GetObject", obj("cc-data", "public/a.txt"), {}, [], "implicitDeny", SAME),
    ("reader", ["s3api", "list-objects-v2", "--bucket", "cc-data"], "s3:ListBucket", "arn:aws:s3:::cc-data", {}, [], "allowed", SAME),
    ("reader", ["s3api", "get-object", "--bucket", "cc-data", "--key", "public/a.txt", out], "s3:GetObject", obj("cc-data", "public/a.txt"), {}, [], "implicitDeny", MOTO_BOUNDARY),
    ("reader", ["s3api", "put-object", "--bucket", "cc-data", "--key", "tmp/reader.txt"], "s3:PutObject", obj("cc-data", "tmp/reader.txt"), {}, [], "implicitDeny", SAME),
    ("auditor", ["s3api", "list-objects-v2", "--bucket", "cc-data"], "s3:ListBucket", "arn:aws:s3:::cc-data", {}, [], "allowed", SAME),
    ("auditor", ["s3api", "get-object", "--bucket", "cc-data", "--key", "public/a.txt", out], "s3:GetObject", obj("cc-data", "public/a.txt"), {}, ["--session-policy", os.path.join(pol, "session-list-only.json")], "implicitDeny", MOTO_SESSION),
]
failures, agreed, diverged, denials = [], 0, 0, {}
for row in ROWS:
    who, cli, action, resource, context, extra, expect, moto = row[:8]
    principal, env_creds = creds[who]
    real = subprocess.run(aws + cli, env=dict(os.environ, **env_creds), capture_output=True, text=True)
    text = real.stdout + real.stderr
    if real.returncode == 0:
        moto_says = "allowed"
    elif "AccessDenied" in text or "(403)" in text:
        moto_says = "denied"
    else:
        sys.exit("SMOKE FAIL: %s %s failed without AccessDenied (rc %d): %s" % (who, " ".join(cli[:2]), real.returncode, text.strip()[-400:]))
    if len(row) > 8 and moto_says == "allowed":
        session = json.loads(real.stdout)
        c = session["Credentials"]
        creds[row[8]] = (session["AssumedRoleUser"]["Arn"], {"AWS_ACCESS_KEY_ID": c["AccessKeyId"], "AWS_SECRET_ACCESS_KEY": c["SecretAccessKey"], "AWS_SESSION_TOKEN": c["SessionToken"]})
    args = ["eval", "--principal", principal, "--action", action, "--resource", resource, "--auth-details", dump, "--json"] + extra
    for key, value in context.items():
        args += ["-c", "%s=%s" % (key, value)]
    mine = subprocess.run(iameval + args, capture_output=True, text=True)
    if mine.returncode not in (0, 1):
        sys.exit("SMOKE FAIL: iameval %s: %s" % (" ".join(args), mine.stderr))
    result = json.loads(mine.stdout)["EvaluationResults"][0]
    decision = result["EvalDecision"]
    moto_expected = ("allowed" if expect == "allowed" else "denied") if moto is None else moto[0]
    status = "agree" if moto is None else "differs"
    if decision != expect:
        failures.append("%s %s %s: iameval %s, AWS documents %s" % (who, action, resource, decision, expect))
        status = "IAMEVAL"
    if moto_says != moto_expected:
        failures.append("%s %s %s: moto %s, expected %s%s" % (who, action, resource, moto_says, moto_expected, "" if moto is None else " (" + moto[1] + "; did moto change?)"))
        status = "MOTO"
    agreed += moto is None and status == "agree"
    diverged += moto is not None and status == "differs"
    if moto_says == "denied" and "is not authorized to perform" in text:
        denials[(who, action)] = (text, result["Message"])
    print("  %-8s %-7s moto=%-7s iameval=%-13s %s %s%s" % (status, who, moto_says, decision, action, resource, "" if moto is None else "   [" + moto[1] + "]"))
for (who, action), (real_text, message) in denials.items():
    said = re.search(r"User: \S+ is not authorized to perform: [^\s<]+", real_text).group(0)
    if not message.startswith(said):
        failures.append("%s %s: moto says %r, iameval says %r" % (who, action, said, message))
if failures:
    sys.exit("SMOKE FAIL: %d problem(s):\n  %s" % (len(failures), "\n  ".join(failures)))
print("  %d rows: %d where moto enforces the same decision, %d where moto is known to differ and iameval follows AWS's documented logic" % (len(ROWS), agreed, diverged))
print("  moto's own AccessDenied text is the prefix of iameval's message for %d denials" % len(denials))
open(os.path.join(work, "moto-denial.txt"), "w").write(re.search(r"User: \S+ is not authorized to perform: [^\s<]+", next(iter(denials.values()))[0]).group(0) if denials else "")
PY

echo "▸ the same 26 requests through moto's enforcement and through iameval (users, a group, role sessions, a session policy)"
AJSON="$(python3 -c 'import json,sys; print(json.dumps(sys.argv[1:]))' "${AWSCLI[@]}")"
IJSON="$(python3 -c 'import json,sys; print(json.dumps(sys.argv[1:]))' "${IAMEVAL[@]}")"
python3 "$WORK/matrix.py" "$AJSON" "$IJSON" "$WORK" "$POL" "$ACCOUNT_ID" || fail "moto versus iameval"

echo "▸ iameval explain reads moto's AccessDenied message"
[[ -s "$WORK/moto-denial.txt" ]] || fail "no AccessDenied message with a principal was captured from moto"
"${IAMEVAL[@]}" explain "$(cat "$WORK/moto-denial.txt")" --json > "$WORK/explain-moto.json" || fail "explain could not parse: $(cat "$WORK/moto-denial.txt")"
python3 -c 'import json,sys; d=json.load(open(sys.argv[1])); assert d["recognized"] and d["principal"].startswith("arn:aws:"), d; print("  %s %s -> decision %s, policy type %s" % (d["principal"], d["action"], d["decision"], d["policy_type"] or "not stated (moto omits the reason)"))' "$WORK/explain-moto.json" || fail "explain: $(cat "$WORK/explain-moto.json")"

echo "SMOKE OK: cloud/iam-policy-evaluator"
