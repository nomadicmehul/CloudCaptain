#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."

fail() { echo "SMOKE FAIL: $*" >&2; exit 1; }
WORK="$(mktemp -d)"
NS="cc-operator"
CRD="staticsites.learn.cloudcaptain.io"
SA="system:serviceaccount:$NS:minioperator"
CTX=()
CLUSTER=""
PROXY_PID=""
OP_PID=""

k() { kubectl ${CTX[@]+"${CTX[@]}"} "$@"; }
stop_operator() {
  if [[ -n "$OP_PID" ]]; then
    kill "$OP_PID" 2>/dev/null || true
    wait "$OP_PID" 2>/dev/null || true
    OP_PID=""
  fi
}
reset_cluster() {
  for site in $(k get staticsites -n "$NS" -o name 2>/dev/null || true); do
    k patch "$site" -n "$NS" --type=merge -p '{"metadata":{"finalizers":null}}' >/dev/null 2>&1 || true
  done
  k delete crd "$CRD" --ignore-not-found --timeout=60s >/dev/null 2>&1 || true
  k delete namespace "$NS" --ignore-not-found --wait="$1" --timeout=90s >/dev/null 2>&1 || true
}
cleanup() {
  stop_operator
  if [[ -n "$PROXY_PID" ]]; then kill "$PROXY_PID" 2>/dev/null || true; fi
  if [[ -n "$CLUSTER" ]]; then reset_cluster false; fi
  rm -rf "$WORK"
}
trap cleanup EXIT

echo "▸ the CRD and the Role come from the same code the controller uses"
python3 code/main.py crd > "$WORK/crd.json"
python3 code/main.py rbac --namespace "$NS" > "$WORK/rbac.json"
python3 - "$WORK/crd.json" "$WORK/rbac.json" <<'PY' || fail "crd or rbac shape"
import json, sys
crd = json.load(open(sys.argv[1]))
version = crd["spec"]["versions"][0]
assert version["subresources"] == {"status": {}}, version
assert version["schema"]["openAPIV3Schema"]["properties"]["spec"]["x-kubernetes-validations"], version
kinds = [item["kind"] for item in json.load(open(sys.argv[2]))["items"]]
assert kinds == ["ServiceAccount", "Role", "RoleBinding"], kinds
print("  CRD", crd["metadata"]["name"], "with a status subresource and a CEL rule; RBAC:", ", ".join(kinds))
PY
python3 code/main.py cel "self.tier != 'prod' || self.replicas >= 2" --self '{"tier":"prod","replicas":1}' > "$WORK/cel.txt" || fail "cel exited non-zero"
[[ "$(cat "$WORK/cel.txt")" == "false" ]] || fail "the CEL subset should reject prod with 1 replica, got: $(cat "$WORK/cel.txt")"

echo "▸ minioperator against the fake API: finalizer, owned objects, Ready, generation 2, 409, 410, CEL, cleanup, GC"
python3 code/main.py demo --json > "$WORK/demo.json" || fail "demo exited non-zero (a call the Role does not allow?)"
python3 - "$WORK/demo.json" <<'PY' || fail "demo event log: $(head -c 3000 "$WORK/demo.json")"
import json, sys
d = json.load(open(sys.argv[1]))
actions = [e["action"] for e in d["events"]]
for wanted in ("finalizer added", "created", "status", "conflict", "watch expired", "rejected", "error", "external record deleted", "finalizer removed", "garbage collected"):
    assert wanted in actions, (wanted, actions)
assert actions.index("external record deleted") < actions.index("finalizer removed") < actions.index("garbage collected"), actions
statuses = [e["detail"] for e in d["events"] if e["action"] == "status"]
assert any("Ready=True" in s and "observedGeneration 2" in s for s in statuses), statuses
rejected = next(e for e in d["events"] if e["action"] == "rejected")
assert "prod sites need at least 2 replicas" in rejected["detail"], rejected
assert d["remaining"] == [] and d["records"] == [], d
assert all(c["allowed"] for c in d["calls"]), d["calls"]
print("  %d events, %d distinct API calls, all allowed by the Role" % (len(d["events"]), len(d["calls"])))
PY

if ! command -v kubectl >/dev/null; then
  echo "  (kubectl not installed; Use It skipped)"
  echo "SMOKE OK: orchestration/operator (fake API only)"
  exit 0
fi
kubectl config get-contexts -o name > "$WORK/contexts.txt" 2>/dev/null || true
if grep -qx kind-cc-smoke "$WORK/contexts.txt"; then CTX=(--context kind-cc-smoke); fi
if ! k cluster-info >/dev/null 2>&1; then
  echo "  (no reachable cluster; Use It skipped)"
  echo "SMOKE OK: orchestration/operator (fake API only)"
  exit 0
fi
CLUSTER=1
reset_cluster true

echo "▸ kubectl apply the CRD; it becomes Established and kubectl explain reads its schema"
k apply -f "$WORK/crd.json" > "$WORK/apply-crd.out" 2>&1 || fail "apply crd: $(cat "$WORK/apply-crd.out")"
k wait --for=condition=Established "crd/$CRD" --timeout=60s >/dev/null || fail "CRD not Established"
k create namespace "$NS" >/dev/null || fail "create namespace $NS"
EXPLAINED=""
for _ in $(seq 1 30); do
  if k explain staticsite.spec > "$WORK/explain.txt" 2>&1 && grep -q "replicas" "$WORK/explain.txt"; then EXPLAINED=1; break; fi
  sleep 1
done
[[ -n "$EXPLAINED" ]] || fail "kubectl explain staticsite.spec: $(cat "$WORK/explain.txt")"
grep -q "content" "$WORK/explain.txt" || fail "explain does not show spec.content: $(cat "$WORK/explain.txt")"

echo "▸ the operator's Role, and kubectl proxy impersonating its service account"
k apply -f "$WORK/rbac.json" > "$WORK/apply-rbac.out" 2>&1 || fail "apply rbac: $(cat "$WORK/apply-rbac.out")"
k auth can-i update staticsites --subresource=status -n "$NS" --as "$SA" > "$WORK/can-status.txt" 2>&1 || true
k auth can-i delete deployments -n "$NS" --as "$SA" > "$WORK/can-delete.txt" 2>&1 || true
[[ "$(cat "$WORK/can-status.txt")" == "yes" ]] || fail "the service account should update staticsites/status: $(cat "$WORK/can-status.txt")"
[[ "$(cat "$WORK/can-delete.txt")" == "no" ]] || fail "the service account should not delete deployments: $(cat "$WORK/can-delete.txt")"
kubectl ${CTX[@]+"${CTX[@]}"} proxy --port=0 --as "$SA" > "$WORK/proxy.out" 2>&1 &
PROXY_PID=$!
for _ in $(seq 1 100); do grep -q "Starting to serve on" "$WORK/proxy.out" 2>/dev/null && break; sleep 0.1; done
PORT="$(sed -n 's/.*Starting to serve on 127\.0\.0\.1:\([0-9][0-9]*\).*/\1/p' "$WORK/proxy.out")"
[[ -n "$PORT" ]] || fail "kubectl proxy did not start: $(cat "$WORK/proxy.out")"
API="http://127.0.0.1:$PORT"
python3 - "$API" "$NS" <<'PY' || fail "the proxy should run as the operator's service account"
import sys, urllib.error, urllib.request
try:
    urllib.request.urlopen(f"{sys.argv[1]}/api/v1/namespaces/{sys.argv[2]}/secrets", timeout=10)
except urllib.error.HTTPError as exc:
    assert exc.code == 403, exc.code
    print(f"  proxy on {sys.argv[1]}: GET secrets is 403 Forbidden, so every call the operator makes uses the Role")
else:
    raise SystemExit("listing secrets succeeded through the proxy; it is not impersonating")
PY

start_operator() {
  local before
  before="$(grep -c "watching staticsites" "$WORK/operator.log" 2>/dev/null || true)"
  before="${before:-0}"
  python3 code/main.py run --server "$API" --namespace "$NS" --records-dir "$WORK/records" --resync 20 --watch-timeout 30 >> "$WORK/operator.log" 2>&1 &
  OP_PID=$!
  for _ in $(seq 1 100); do
    local now
    now="$(grep -c "watching staticsites" "$WORK/operator.log" 2>/dev/null || true)"
    if [[ "${now:-0}" -gt "$before" ]]; then return 0; fi
    kill -0 "$OP_PID" 2>/dev/null || fail "operator exited: $(tail -20 "$WORK/operator.log")"
    sleep 0.1
  done
  fail "operator did not start: $(tail -20 "$WORK/operator.log")"
}
oplog() { tail -25 "$WORK/operator.log"; }
touch "$WORK/operator.log"
start_operator

echo "▸ create a StaticSite: owned objects with ownerReferences, Ready, observedGeneration 1"
cat > "$WORK/hello.yaml" <<'EOF'
apiVersion: learn.cloudcaptain.io/v1alpha1
kind: StaticSite
metadata:
  name: hello
spec:
  content: "<h1>hello from cc-smoke</h1>"
  replicas: 2
EOF
k apply -n "$NS" -f "$WORK/hello.yaml" >/dev/null || fail "apply hello"
k wait -n "$NS" --for=condition=Ready staticsite/hello --timeout=180s >/dev/null || fail "hello never became Ready: $(k get staticsite hello -n "$NS" -o json 2>&1 | tail -30) $(oplog)"
k get staticsite hello -n "$NS" -o json > "$WORK/site.json"
k get configmap/hello-content deployment/hello service/hello -n "$NS" -o json > "$WORK/owned.json"
python3 - "$WORK/site.json" "$WORK/owned.json" <<'PY' || fail "owned objects or status: $(oplog)"
import json, sys
site = json.load(open(sys.argv[1]))
owned = json.load(open(sys.argv[2]))["items"]
uid = site["metadata"]["uid"]
assert site["metadata"]["generation"] == 1 and site["status"]["observedGeneration"] == 1, site["status"]
assert site["metadata"]["finalizers"] == ["learn.cloudcaptain.io/external-record"], site["metadata"]
assert site["spec"]["tier"] == "dev", site["spec"]
ready = next(c for c in site["status"]["conditions"] if c["type"] == "Ready")
assert ready["status"] == "True" and ready["observedGeneration"] == 1, ready
assert sorted(item["kind"] for item in owned) == ["ConfigMap", "Deployment", "Service"], owned
for item in owned:
    refs = item["metadata"].get("ownerReferences") or []
    assert any(r["uid"] == uid and r["kind"] == "StaticSite" and r.get("controller") for r in refs), (item["kind"], refs)
deployment = next(item for item in owned if item["kind"] == "Deployment")
assert deployment["spec"]["template"]["spec"]["containers"][0]["image"] == "nginx:1.30-alpine", deployment["spec"]
assert deployment["status"]["readyReplicas"] == 2, deployment["status"]
print("  ConfigMap, Deployment and Service owned by uid", uid, "; Ready:", ready["message"])
PY
[[ -f "$WORK/records/${NS}_hello.json" ]] || fail "external record was not published"
k get staticsites -n "$NS" > "$WORK/columns.txt"
grep -q "REPLICAS" "$WORK/columns.txt" && grep -q "Available" "$WORK/columns.txt" || fail "printer columns: $(cat "$WORK/columns.txt")"
k exec -n "$NS" deploy/hello -- wget -qO- http://127.0.0.1/ > "$WORK/page.html" 2>&1 || fail "nginx did not serve: $(cat "$WORK/page.html")"
grep -q "hello from cc-smoke" "$WORK/page.html" || fail "served page: $(cat "$WORK/page.html")"

echo "▸ change the spec: generation 2, observedGeneration follows, the Deployment scales"
k patch staticsite hello -n "$NS" --type=merge -p '{"spec":{"replicas":3}}' >/dev/null || fail "patch replicas"
k wait -n "$NS" --for=jsonpath='{.status.observedGeneration}'=2 staticsite/hello --timeout=60s >/dev/null || fail "observedGeneration never reached 2: $(oplog)"
k wait -n "$NS" --for=condition=Ready staticsite/hello --timeout=120s >/dev/null || fail "not Ready after scaling: $(oplog)"
READY="$(k get deployment hello -n "$NS" -o jsonpath='{.status.readyReplicas}')"
[[ "$READY" == "3" ]] || fail "deployment readyReplicas $READY, want 3"

echo "▸ status belongs to the controller: a manual status patch is reconciled back"
k patch staticsite hello -n "$NS" --subresource=status --type=merge -p '{"status":{"readyReplicas":0}}' >/dev/null || fail "status patch"
k wait -n "$NS" --for=jsonpath='{.status.readyReplicas}'=3 staticsite/hello --timeout=60s >/dev/null || fail "operator did not repair status: $(oplog)"

echo "▸ an invalid spec is rejected by the CEL rule, and minioperator validate says the same"
cat > "$WORK/bad.json" <<'EOF'
{"apiVersion": "learn.cloudcaptain.io/v1alpha1", "kind": "StaticSite", "metadata": {"name": "bad"}, "spec": {"content": "<p>shop</p>", "tier": "prod", "replicas": 1}}
EOF
set +e
k apply -n "$NS" -f "$WORK/bad.json" > "$WORK/bad.out" 2>&1
CODE=$?
python3 code/main.py validate "$WORK/bad.json" > "$WORK/bad-local.out" 2>&1
LOCAL=$?
set -e
[[ "$CODE" != "0" ]] || fail "the API server accepted a prod site with 1 replica"
grep -q "prod sites need at least 2 replicas" "$WORK/bad.out" || fail "expected the CEL message from the API server, got: $(cat "$WORK/bad.out")"
[[ "$LOCAL" == "1" ]] && grep -q "prod sites need at least 2 replicas" "$WORK/bad-local.out" || fail "minioperator validate: exit $LOCAL, $(cat "$WORK/bad-local.out")"
echo "  API server: $(cat "$WORK/bad.out")"

echo "▸ a stuck deletion: with the operator stopped, the finalizer holds the object until it comes back"
sed 's/name: hello/name: stuck/; s/replicas: 2/replicas: 1/' "$WORK/hello.yaml" > "$WORK/stuck.yaml"
k apply -n "$NS" -f "$WORK/stuck.yaml" >/dev/null || fail "apply stuck"
k wait -n "$NS" --for=jsonpath='{.status.observedGeneration}'=1 staticsite/stuck --timeout=60s >/dev/null || fail "stuck was not reconciled: $(oplog)"
[[ -f "$WORK/records/${NS}_stuck.json" ]] || fail "no external record for stuck"
stop_operator
k delete staticsite stuck -n "$NS" --wait=false >/dev/null || fail "delete stuck"
k get staticsite stuck -n "$NS" -o json > "$WORK/stuck.json" || fail "stuck vanished without its finalizer running"
python3 - "$WORK/stuck.json" <<'PY' || fail "stuck deletion shape"
import json, sys
meta = json.load(open(sys.argv[1]))["metadata"]
assert meta.get("deletionTimestamp"), meta
assert "learn.cloudcaptain.io/external-record" in meta.get("finalizers", []), meta
print("  deletionTimestamp", meta["deletionTimestamp"], "finalizers", meta["finalizers"])
PY
[[ -f "$WORK/records/${NS}_stuck.json" ]] || fail "the external record should survive while the operator is down"
start_operator
k wait -n "$NS" --for=delete staticsite/stuck --timeout=60s >/dev/null || fail "stuck still there after the operator restarted: $(oplog)"
[[ ! -e "$WORK/records/${NS}_stuck.json" ]] || fail "external record for stuck was not removed"

echo "▸ delete hello: the finalizer removes the external record, then the garbage collector removes the children"
k delete staticsite hello -n "$NS" --wait=false >/dev/null || fail "delete hello"
k wait -n "$NS" --for=delete staticsite/hello --timeout=60s >/dev/null || fail "hello was not deleted: $(oplog)"
[[ ! -e "$WORK/records/${NS}_hello.json" ]] || fail "external record for hello survived the deletion"
k wait -n "$NS" --for=delete configmap/hello-content deployment/hello service/hello --timeout=120s >/dev/null || fail "children were not garbage collected"
grep -q "hello: finalizer removed" "$WORK/operator.log" || fail "operator log has no finalizer line: $(oplog)"
grep -c "conflict" "$WORK/operator.log" > "$WORK/conflicts.txt" || true
echo "  operator log: $(wc -l < "$WORK/operator.log") lines, $(cat "$WORK/conflicts.txt") conflict(s) requeued"

echo "SMOKE OK: orchestration/operator"
