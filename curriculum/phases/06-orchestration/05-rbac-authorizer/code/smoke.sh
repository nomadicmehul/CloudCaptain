#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."

fail() { echo "SMOKE FAIL: $*" >&2; exit 1; }
expect_exit() { local want="$1" got=0; shift; "$@" || got=$?; [[ "$got" == "$want" ]] || fail "expected exit $want, got $got from: $*"; }

LESSON_DIR="$PWD"
CANHAZ=(python3 "$LESSON_DIR/code/main.py")
FIXTURE="$LESSON_DIR/code/fixtures/rbac.json"
NS="cc-rbac-authorizer"
AGG_ROLE="cc-rbac-authorizer-leases-view"
WORK="$(mktemp -d)"
K=(kubectl)
CREATED=0
cleanup() {
  if [[ "$CREATED" == "1" ]]; then
    "${K[@]}" delete clusterrole "$AGG_ROLE" --ignore-not-found >/dev/null 2>&1 || true
    "${K[@]}" delete namespace "$NS" --ignore-not-found --wait=false >/dev/null 2>&1 || true
  fi
  rm -rf "$WORK"
}
trap cleanup EXIT

echo "▸ canhaz on the fixture: an allow names the binding, a deny prints the 403 text, exit codes follow kubectl auth can-i"
DASH="system:serviceaccount:shop:dashboard"
"${CANHAZ[@]}" can-i get secrets/shop-db-readonly -n shop --as "$DASH" -f "$FIXTURE" > "$WORK/allow.txt" || fail "expected yes: $(cat "$WORK/allow.txt")"
grep -q 'allowed by RoleBinding "pod-reader/shop" of Role "pod-reader" to ServiceAccount "dashboard/shop"' "$WORK/allow.txt" || fail "allow reason: $(cat "$WORK/allow.txt")"
expect_exit 1 "${CANHAZ[@]}" can-i create pods/web-1 --subresource exec -n shop --as "$DASH" -f "$FIXTURE" > "$WORK/deny.txt"
grep -q 'pods "web-1" is forbidden: User "system:serviceaccount:shop:dashboard" cannot create resource "pods/exec"' "$WORK/deny.txt" || fail "deny text: $(cat "$WORK/deny.txt")"
"${CANHAZ[@]}" can-i list leases.coordination.k8s.io -n shop --as "$DASH" -f "$FIXTURE" > "$WORK/agg.txt" || fail "aggregated view should grant leases: $(cat "$WORK/agg.txt")"
expect_exit 1 "${CANHAZ[@]}" can-bind --clusterrole edit -n shop --as system:serviceaccount:shop:ci -f "$FIXTURE" --json > "$WORK/bind.json"
"${CANHAZ[@]}" who-can create pods --subresource exec -n shop -f "$FIXTURE" --json > "$WORK/who.json" 2>/dev/null || fail "who-can on the fixture"
python3 - "$WORK/bind.json" "$WORK/who.json" <<'PY' || fail "fixture escalation and who-can"
import json, sys
bind = json.load(open(sys.argv[1]))
assert bind["how"] == "escalation", bind
assert {"apiGroups": [""], "resources": ["pods/exec"], "verbs": ["create", "get"]} in bind["missing"], bind["missing"]
who = {(s["kind"], s["name"]) for s in json.load(open(sys.argv[2]))["subjects"]}
assert ("Group", "shop-oncall") in who and ("ServiceAccount", "dashboard") not in who, who
print("  escalation refused with %d missing rules; who can exec in shop: %s" % (len(bind["missing"]), sorted(who)))
PY
"${CANHAZ[@]}" can-i --list -n shop --as "$DASH" -f "$FIXTURE" > "$WORK/list.txt"
head -3 "$WORK/list.txt"

if ! command -v kubectl >/dev/null; then
  echo "  (kubectl not installed; cluster comparison skipped)"
  echo "SMOKE OK: orchestration/rbac-authorizer (fixture only)"
  exit 0
fi
kubectl config get-contexts -o name > "$WORK/contexts" 2>/dev/null || true
if grep -qx kind-cc-smoke "$WORK/contexts"; then K=(kubectl --context kind-cc-smoke); fi
if ! "${K[@]}" cluster-info > "$WORK/cluster-info" 2>&1; then
  echo "  (no reachable cluster; cluster comparison skipped)"
  echo "SMOKE OK: orchestration/rbac-authorizer (fixture only)"
  exit 0
fi

echo "▸ Use It: namespace $NS with two ServiceAccounts, Roles, RoleBindings and a ClusterRole that aggregates into view"
CREATED=1
"${K[@]}" delete clusterrole "$AGG_ROLE" --ignore-not-found >/dev/null 2>&1 || true
"${K[@]}" delete namespace "$NS" --ignore-not-found --wait=true --timeout=120s >/dev/null 2>&1 || true
"${K[@]}" create namespace "$NS" > "$WORK/ns.txt" 2>&1 || fail "create namespace: $(cat "$WORK/ns.txt")"
cat > "$WORK/setup.yaml" <<EOF
apiVersion: v1
kind: ServiceAccount
metadata:
  name: cc-reader
  namespace: $NS
---
apiVersion: v1
kind: ServiceAccount
metadata:
  name: cc-binder
  namespace: $NS
---
apiVersion: rbac.authorization.k8s.io/v1
kind: Role
metadata:
  name: cc-reader
  namespace: $NS
rules:
  - apiGroups: [""]
    resources: ["pods"]
    verbs: ["get", "list"]
  - apiGroups: [""]
    resources: ["pods/log"]
    verbs: ["get"]
  - apiGroups: [""]
    resources: ["secrets"]
    resourceNames: ["cc-allowed"]
    verbs: ["get"]
---
apiVersion: rbac.authorization.k8s.io/v1
kind: RoleBinding
metadata:
  name: cc-reader
  namespace: $NS
roleRef:
  apiGroup: rbac.authorization.k8s.io
  kind: Role
  name: cc-reader
subjects:
  - kind: ServiceAccount
    name: cc-reader
    namespace: $NS
---
apiVersion: rbac.authorization.k8s.io/v1
kind: RoleBinding
metadata:
  name: cc-reader-view
  namespace: $NS
roleRef:
  apiGroup: rbac.authorization.k8s.io
  kind: ClusterRole
  name: view
subjects:
  - kind: ServiceAccount
    name: cc-reader
---
apiVersion: rbac.authorization.k8s.io/v1
kind: RoleBinding
metadata:
  name: cc-auditors-view
  namespace: $NS
roleRef:
  apiGroup: rbac.authorization.k8s.io
  kind: ClusterRole
  name: view
subjects:
  - apiGroup: rbac.authorization.k8s.io
    kind: Group
    name: cc-auditors
---
apiVersion: rbac.authorization.k8s.io/v1
kind: RoleBinding
metadata:
  name: cc-dangling
  namespace: $NS
roleRef:
  apiGroup: rbac.authorization.k8s.io
  kind: ClusterRole
  name: cc-rbac-authorizer-missing
subjects:
  - apiGroup: rbac.authorization.k8s.io
    kind: Group
    name: cc-auditors
---
apiVersion: rbac.authorization.k8s.io/v1
kind: Role
metadata:
  name: cc-binder
  namespace: $NS
rules:
  - apiGroups: ["rbac.authorization.k8s.io"]
    resources: ["rolebindings"]
    verbs: ["create"]
  - apiGroups: [""]
    resources: ["pods"]
    verbs: ["get", "list"]
  - apiGroups: [""]
    resources: ["pods/log"]
    verbs: ["get"]
  - apiGroups: [""]
    resources: ["secrets"]
    resourceNames: ["cc-allowed"]
    verbs: ["get"]
---
apiVersion: rbac.authorization.k8s.io/v1
kind: RoleBinding
metadata:
  name: cc-binder
  namespace: $NS
roleRef:
  apiGroup: rbac.authorization.k8s.io
  kind: Role
  name: cc-binder
subjects:
  - kind: ServiceAccount
    name: cc-binder
    namespace: $NS
---
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRole
metadata:
  name: $AGG_ROLE
  labels:
    rbac.authorization.k8s.io/aggregate-to-view: "true"
rules:
  - apiGroups: ["coordination.k8s.io"]
    resources: ["leases"]
    verbs: ["get", "list", "watch"]
EOF
"${K[@]}" apply -f "$WORK/setup.yaml" > "$WORK/apply.txt" 2>&1 || fail "apply: $(cat "$WORK/apply.txt")"

cat > "$WORK/wait_for.py" <<'PY'
import json, subprocess, sys, time

timeout, kubectl, checks = float(sys.argv[1]), json.loads(sys.argv[2]), json.loads(sys.argv[3])
deadline = time.time() + timeout
pending = list(checks)
while pending and time.time() < deadline:
    still = []
    for check in pending:
        if check[0] == "rule":
            proc = subprocess.run(kubectl + ["get", "clusterrole", check[1], "-o", "json"], capture_output=True, text=True)
            ok = proc.returncode == 0 and check[2] in json.loads(proc.stdout).get("rules", [])
        else:
            proc = subprocess.run(kubectl + ["auth", "can-i"] + check[1:], capture_output=True, text=True)
            ok = proc.stdout.strip().startswith("yes")
        if not ok:
            still.append(check)
    pending = still
    if pending:
        time.sleep(1)
if pending:
    sys.exit("still waiting after %ss for: %s" % (timeout, pending))
PY
KJSON="$(python3 -c 'import json,sys; print(json.dumps(sys.argv[1:]))' "${K[@]}")"
SA="system:serviceaccount:${NS}:cc-reader"
BINDER="system:serviceaccount:${NS}:cc-binder"
LEASES_RULE='{"apiGroups":["coordination.k8s.io"],"resources":["leases"],"verbs":["get","list","watch"]}'

echo "▸ wait (90s max) for the aggregation controller to copy the leases rule into view, edit and admin, and for the authorizer to see the bindings"
python3 "$WORK/wait_for.py" 90 "$KJSON" "[[\"rule\",\"view\",$LEASES_RULE],[\"rule\",\"edit\",$LEASES_RULE],[\"rule\",\"admin\",$LEASES_RULE],[\"can-i\",\"list\",\"leases.coordination.k8s.io\",\"-n\",\"$NS\",\"--as\",\"$SA\"],[\"can-i\",\"get\",\"secrets/cc-allowed\",\"-n\",\"$NS\",\"--as\",\"$SA\"],[\"can-i\",\"create\",\"rolebindings\",\"-n\",\"$NS\",\"--as\",\"$BINDER\"],[\"can-i\",\"list\",\"configmaps\",\"-n\",\"$NS\",\"--as\",\"jane\",\"--as-group\",\"cc-auditors\"]]" \
  || fail "aggregation or bindings never became visible"

echo "▸ dump RBAC exactly as the lesson does"
"${K[@]}" get roles,rolebindings,clusterroles,clusterrolebindings -A -o json > "$WORK/rbac.json" || fail "RBAC dump"
python3 -c 'import json,sys; d=json.load(open(sys.argv[1])); print("  %d RBAC objects" % len(d["items"]))' "$WORK/rbac.json"

cat > "$WORK/matrix.py" <<'PY'
import json, subprocess, sys

canhaz, dump, ns, kubectl = json.loads(sys.argv[1]), sys.argv[2], sys.argv[3], json.loads(sys.argv[4])
SA = ["--as", "system:serviceaccount:%s:cc-reader" % ns]
AUDITOR = ["--as", "jane", "--as-group", "cc-auditors"]
JANE = ["--as", "jane"]
N, D, A = ["-n", ns], ["-n", "default"], ["-A"]
LOG, EXEC = ["--subresource", "log"], ["--subresource", "exec"]
QUESTIONS = [
    (SA, "get", "pods", N, True),
    (SA, "list", "pods", N, True),
    (SA, "watch", "pods", N, True),
    (SA, "get", "pods", N + LOG, True),
    (SA, "create", "pods", N + EXEC, False),
    (SA, "delete", "pods", N, False),
    (SA, "list", "pods", D, False),
    (SA, "list", "pods", A, False),
    (SA, "get", "secrets/cc-allowed", N, True),
    (SA, "get", "secrets/cc-other", N, False),
    (SA, "list", "secrets", N, False),
    (SA, "get", "configmaps", N, True),
    (SA, "update", "configmaps", N, False),
    (SA, "list", "leases.coordination.k8s.io", N, True),
    (SA, "list", "leases.coordination.k8s.io", D, False),
    (SA, "get", "deployments.apps", N, True),
    (SA, "list", "nodes", A, False),
    (SA, "create", "selfsubjectaccessreviews.authorization.k8s.io", A, None),
    (SA, "get", "/healthz", [], None),
    (SA, "get", "/metrics", [], None),
    (SA, "impersonate", "users", A, False),
    (SA, "bind", "clusterroles", A, False),
    (AUDITOR, "list", "configmaps", N, True),
    (AUDITOR, "get", "pods", N + LOG, True),
    (AUDITOR, "list", "configmaps", D, False),
    (AUDITOR, "list", "secrets", N, False),
    (JANE, "list", "configmaps", N, False),
]
mismatches = []
for who, verb, target, scope, expected in QUESTIONS:
    args = [verb, target] + scope + who
    real = subprocess.run(kubectl + ["auth", "can-i"] + args, capture_output=True, text=True)
    mine = subprocess.run(canhaz + ["can-i"] + args + ["-f", dump, "--json"], capture_output=True, text=True)
    words = real.stdout.split()
    if not words or words[0] not in ("yes", "no") or mine.returncode not in (0, 1):
        sys.exit("SMOKE FAIL: unreadable answer for %s\n kubectl: %r %r\n canhaz: %r %r" % (args, real.stdout, real.stderr, mine.stdout, mine.stderr))
    real_yes, mine_doc = words[0] == "yes", json.loads(mine.stdout)
    status = "ok"
    if real_yes != mine_doc["allowed"] or real.returncode != mine.returncode:
        status = "MISMATCH"
    elif expected is not None and real_yes != expected:
        status = "UNEXPECTED"
    print("  %-8s kubectl=%-3s canhaz=%-3s %s" % (status, "yes" if real_yes else "no", "yes" if mine_doc["allowed"] else "no", " ".join(args)))
    if status != "ok":
        mismatches.append((args, real.stdout.strip(), mine_doc.get("reason") or mine_doc.get("forbidden")))
    if target == "secrets" and who is AUDITOR:
        if "cc-rbac-authorizer-missing" not in real.stdout or "not found" not in real.stdout or "cc-rbac-authorizer-missing" not in mine_doc["reason"]:
            mismatches.append((args, "dangling ClusterRole not reported", real.stdout.strip() + " | " + mine_doc["reason"]))
        else:
            print("           kubectl: %s\n           canhaz:  no - %s" % (real.stdout.strip(), mine_doc["reason"]))
if mismatches:
    sys.exit("SMOKE FAIL: %d of %d answers differ: %s" % (len(mismatches), len(QUESTIONS), mismatches))
print("  all %d answers and exit codes match" % len(QUESTIONS))
PY
CJSON="$(python3 -c 'import json,sys; print(json.dumps(sys.argv[1:]))' "${CANHAZ[@]}")"

echo "▸ canhaz versus kubectl auth can-i on 27 questions (service account, a group via --as-group, a plain user)"
python3 "$WORK/matrix.py" "$CJSON" "$WORK/rbac.json" "$NS" "$KJSON" || fail "can-i matrix"

echo "▸ --list: the same permissions, row for row"
"${K[@]}" auth can-i --list -n "$NS" --as "$SA" > "$WORK/real-list.txt" 2>"$WORK/real-list.err" || fail "kubectl auth can-i --list: $(cat "$WORK/real-list.err")"
"${CANHAZ[@]}" can-i --list -n "$NS" --as "$SA" -f "$WORK/rbac.json" --json > "$WORK/mine-list.json" || fail "canhaz --list"
python3 - "$WORK/real-list.txt" "$WORK/mine-list.json" <<'PY' || fail "--list differs"
import json, re, sys
real = {}
lines = open(sys.argv[1]).read().splitlines()
for line in lines[1:]:
    if not line.strip():
        continue
    m = re.match(r"^(\S*)\s+\[([^\]]*)\]\s+\[([^\]]*)\]\s+\[([^\]]*)\]\s*$", line)
    assert m, "unparsed kubectl --list line: %r" % line
    resource, urls, names, verbs = m.groups()
    real.setdefault((resource, tuple(urls.split()), tuple(names.split())), set()).update(verbs.split())
mine = {}
for row in json.load(open(sys.argv[2]))["rules"]:
    mine.setdefault((row["resource"], tuple(row["non_resource_urls"]), tuple(row["resource_names"])), set()).update(row["verbs"])
only_real = {k: v for k, v in real.items() if mine.get(k) != v}
only_mine = {k: v for k, v in mine.items() if real.get(k) != v}
assert not only_real and not only_mine, ("kubectl only", only_real, "canhaz only", only_mine)
assert (("leases.coordination.k8s.io", (), ()) in mine) and (("secrets", (), ("cc-allowed",)) in mine), sorted(mine)
print("  %d rows identical, including leases.coordination.k8s.io (aggregated into view) and secrets [cc-allowed]" % len(mine))
PY

if "${K[@]}" auth whoami --help >/dev/null 2>&1 && "${CANHAZ[@]}" can-i create selfsubjectreviews.authentication.k8s.io -A --as "$SA" -f "$WORK/rbac.json" > /dev/null; then
  echo "▸ kubectl auth whoami agrees with the identity canhaz evaluates (canhaz says the service account may create selfsubjectreviews)"
  "${K[@]}" auth whoami --as "$SA" -o json > "$WORK/whoami.json" || fail "kubectl auth whoami"
  "${CANHAZ[@]}" whoami --as "$SA" --json > "$WORK/mine-whoami.json"
  python3 - "$WORK/whoami.json" "$WORK/mine-whoami.json" <<'PY' || fail "whoami differs"
import json, sys
real = json.load(open(sys.argv[1]))["status"]["userInfo"]
mine = json.load(open(sys.argv[2]))
assert real["username"] == mine["username"] and set(real.get("groups", [])) == set(mine["groups"]), (real, mine)
print("  %s in %s" % (mine["username"], sorted(mine["groups"])))
PY
else
  echo "  (kubectl auth whoami not available, or this cluster grants no selfsubjectreviews; skipped)"
fi

echo "▸ SubjectAccessReview: the reason names the binding canhaz names; without groups the answer changes"
cat > "$WORK/sar-groups.yaml" <<EOF
apiVersion: authorization.k8s.io/v1
kind: SubjectAccessReview
spec:
  user: $SA
  groups: ["system:serviceaccounts", "system:serviceaccounts:$NS", "system:authenticated"]
  resourceAttributes:
    namespace: $NS
    verb: get
    resource: secrets
    name: cc-allowed
EOF
cat > "$WORK/sar-nogroups.yaml" <<EOF
apiVersion: authorization.k8s.io/v1
kind: SubjectAccessReview
spec:
  user: $SA
  resourceAttributes:
    verb: create
    group: authorization.k8s.io
    resource: selfsubjectaccessreviews
EOF
"${K[@]}" create -f "$WORK/sar-groups.yaml" -o json > "$WORK/sar-groups.json" || fail "SubjectAccessReview"
"${K[@]}" create -f "$WORK/sar-nogroups.yaml" -o json > "$WORK/sar-nogroups.json" || fail "SubjectAccessReview without groups"
"${CANHAZ[@]}" can-i get secrets/cc-allowed -n "$NS" --as "$SA" -f "$WORK/rbac.json" --json > "$WORK/mine-sar.json" || fail "canhaz should allow the named secret"
expect_exit 1 "${CANHAZ[@]}" can-i create selfsubjectaccessreviews.authorization.k8s.io -A --as "$SA" --no-default-groups -f "$WORK/rbac.json" --json > "$WORK/mine-nogroups.json"
expect_exit 0 "${CANHAZ[@]}" can-i create selfsubjectaccessreviews.authorization.k8s.io -A --as "$SA" -f "$WORK/rbac.json" --json > /dev/null
python3 - "$WORK/sar-groups.json" "$WORK/mine-sar.json" "$WORK/sar-nogroups.json" "$NS" <<'PY' || fail "SubjectAccessReview comparison"
import json, sys
real, mine, bare, ns = json.load(open(sys.argv[1]))["status"], json.load(open(sys.argv[2])), json.load(open(sys.argv[3]))["status"], sys.argv[4]
assert real.get("allowed") is True, real
reason = real.get("reason", "")
assert 'RoleBinding "cc-reader/%s"' % ns in reason and 'Role "cc-reader"' in reason, reason
print("  real:   " + reason)
print("  canhaz: " + mine["reason"] + ("   (identical)" if mine["reason"] == reason else "   (differs)"))
assert bare.get("allowed") is not True, bare
print("  without groups the API server says allowed=%s for create selfsubjectaccessreviews, and so does canhaz --no-default-groups" % bare.get("allowed", False))
PY

echo "▸ a real 403 carries the message canhaz predicts"
if "${K[@]}" get secrets -n "$NS" --as "$SA" > "$WORK/forbidden.out" 2> "$WORK/forbidden.err"; then fail "listing secrets as $SA should be forbidden"; fi
expect_exit 1 "${CANHAZ[@]}" can-i list secrets -n "$NS" --as "$SA" -f "$WORK/rbac.json" --json > "$WORK/mine-forbidden.json"
python3 - "$WORK/forbidden.err" "$WORK/mine-forbidden.json" <<'PY' || fail "forbidden message differs"
import json, sys
real, predicted = open(sys.argv[1]).read(), json.load(open(sys.argv[2]))["forbidden"]
assert "Forbidden" in real and predicted in real, (real, predicted)
print("  " + real.strip())
PY

echo "▸ escalation: cc-binder may bind a Role it covers, is refused edit, and may bind edit once it holds bind on clusterroles/edit"
"${CANHAZ[@]}" can-bind --role cc-reader -n "$NS" --as "$BINDER" -f "$WORK/rbac.json" --json > "$WORK/bind-reader.json" || fail "canhaz should allow binding cc-reader: $(cat "$WORK/bind-reader.json")"
"${K[@]}" create rolebinding cc-grant-reader --role=cc-reader --user=cc-nobody -n "$NS" --as "$BINDER" > "$WORK/k-reader.txt" 2>&1 || fail "API refused a binding canhaz allowed: $(cat "$WORK/k-reader.txt")"
expect_exit 1 "${CANHAZ[@]}" can-bind --clusterrole edit -n "$NS" --as "$BINDER" -f "$WORK/rbac.json" --json > "$WORK/bind-edit.json"
if "${K[@]}" create rolebinding cc-grant-edit --clusterrole=edit --user=cc-nobody -n "$NS" --as "$BINDER" > "$WORK/k-edit.txt" 2>&1; then fail "API allowed binding edit without bind or the permissions"; fi
python3 - "$WORK/k-edit.txt" "$WORK/bind-edit.json" <<'PY' || fail "escalation details differ"
import json, re, sys
real, mine = open(sys.argv[1]).read(), json.load(open(sys.argv[2]))
assert "Forbidden" in real or "forbidden" in real, real
assert "attempting to grant RBAC permissions not currently held" in real, real
assert mine["how"] == "escalation", mine

def field(text, name):
    m = re.search(name + r':\[([^\]]*)\]', text)
    return tuple(re.findall(r'"((?:[^"\\]|\\.)*)"', m.group(1))) if m else ()

def key(groups, resources, urls, names):
    return (groups, resources, urls, names)

real_missing = {}
for block in re.findall(r"\{(?:APIGroups|NonResourceURLs):[^{}]*\}", real):
    k = key(field(block, "APIGroups"), field(block, "Resources"), field(block, "NonResourceURLs"), field(block, "ResourceNames"))
    real_missing.setdefault(k, set()).update(field(block, "Verbs"))
mine_missing = {}
for rule in mine["missing"]:
    k = key(tuple(rule.get("apiGroups", [])), tuple(rule.get("resources", [])), tuple(rule.get("nonResourceURLs", [])), tuple(rule.get("resourceNames", [])))
    mine_missing.setdefault(k, set()).update(rule["verbs"])
assert real_missing == mine_missing, ("kubectl only", {k: v for k, v in real_missing.items() if mine_missing.get(k) != v}, "canhaz only", {k: v for k, v in mine_missing.items() if real_missing.get(k) != v})
print("  both refuse edit with the same %d missing rules, e.g. %s" % (len(mine_missing), sorted(k[1][0] for k in mine_missing)[:4]))
PY
cat > "$WORK/bind-edit.yaml" <<EOF
apiVersion: rbac.authorization.k8s.io/v1
kind: Role
metadata:
  name: cc-binder-bind-edit
  namespace: $NS
rules:
  - apiGroups: ["rbac.authorization.k8s.io"]
    resources: ["clusterroles"]
    resourceNames: ["edit"]
    verbs: ["bind"]
---
apiVersion: rbac.authorization.k8s.io/v1
kind: RoleBinding
metadata:
  name: cc-binder-bind-edit
  namespace: $NS
roleRef:
  apiGroup: rbac.authorization.k8s.io
  kind: Role
  name: cc-binder-bind-edit
subjects:
  - kind: ServiceAccount
    name: cc-binder
    namespace: $NS
EOF
"${K[@]}" apply -f "$WORK/bind-edit.yaml" > "$WORK/apply2.txt" 2>&1 || fail "apply bind role: $(cat "$WORK/apply2.txt")"
python3 "$WORK/wait_for.py" 60 "$KJSON" "[[\"can-i\",\"bind\",\"clusterroles/edit\",\"-n\",\"$NS\",\"--as\",\"$BINDER\"]]" || fail "bind permission never became visible"
"${K[@]}" get roles,rolebindings,clusterroles,clusterrolebindings -A -o json > "$WORK/rbac2.json" || fail "second RBAC dump"
"${CANHAZ[@]}" can-bind --clusterrole edit -n "$NS" --as "$BINDER" -f "$WORK/rbac2.json" --json > "$WORK/bind-edit2.json" || fail "canhaz should allow binding edit with the bind verb: $(cat "$WORK/bind-edit2.json")"
python3 -c 'import json,sys; d=json.load(open(sys.argv[1])); assert d["how"]=="bind", d; print("  canhaz: " + d["via"])' "$WORK/bind-edit2.json" || fail "bind path"
"${K[@]}" create rolebinding cc-grant-edit --clusterrole=edit --user=cc-nobody -n "$NS" --as "$BINDER" > "$WORK/k-edit2.txt" 2>&1 || fail "API refused binding edit after bind was granted: $(cat "$WORK/k-edit2.txt")"
echo "  kubectl: $(cat "$WORK/k-edit2.txt")"

echo "▸ who-can: every subject canhaz lists is confirmed by kubectl auth can-i"
"${CANHAZ[@]}" who-can list leases.coordination.k8s.io -n "$NS" -f "$WORK/rbac2.json" --json > "$WORK/who-real.json" 2>/dev/null || fail "who-can on the cluster dump"
python3 - "$WORK/who-real.json" "$NS" "$KJSON" <<'PY' || fail "who-can"
import json, subprocess, sys
doc, ns, kubectl = json.load(open(sys.argv[1])), sys.argv[2], json.loads(sys.argv[3])
subjects = {(s["kind"], s["name"], s.get("namespace", "")) for s in doc["subjects"]}
assert ("ServiceAccount", "cc-reader", ns) in subjects and ("Group", "cc-auditors", "") in subjects, subjects
checked = 0
for kind, name, namespace in sorted(subjects)[:40]:
    who = {"User": ["--as", name], "ServiceAccount": ["--as", "system:serviceaccount:%s:%s" % (namespace, name)], "Group": ["--as", "cc-probe", "--as-group", name]}[kind]
    proc = subprocess.run(kubectl + ["auth", "can-i", "list", "leases.coordination.k8s.io", "-n", ns] + who, capture_output=True, text=True)
    assert proc.stdout.strip().startswith("yes"), (kind, name, namespace, proc.stdout, proc.stderr)
    checked += 1
print("  %d subjects, each confirmed: %s" % (checked, ", ".join("%s %s" % (k, n) for k, n, _ in sorted(subjects))))
PY

echo "SMOKE OK: orchestration/rbac-authorizer"
