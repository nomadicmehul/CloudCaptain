#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."

fail() { echo "SMOKE FAIL: $*" >&2; exit 1; }
WORK="$(mktemp -d)"
PREFIX="cc-preview-environments"
REPO="shop"
NS1="$PREFIX-$REPO-pr-101"
NS2="$PREFIX-$REPO-pr-102"
NS3="$PREFIX-$REPO-pr-103"
MARK="cc-smoke=preview-environments"
SELECTOR="app.kubernetes.io/managed-by=minienv,preview.cloudcaptain.io/repo=$REPO"
CTX=(--context kind-cc-smoke)
CLUSTER=""

k() { kubectl "${CTX[@]}" "$@"; }
minienv() {
  local command="$1"
  shift
  python3 code/main.py "$command" --context kind-cc-smoke --repo "$REPO" --prefix "$PREFIX" --image-template nginx:1.30-alpine --port 80 "$@"
}
reset_cluster() {
  k delete clusterrolebindings.rbac.authorization.k8s.io,persistentvolumes -l "$MARK" --ignore-not-found --wait=false >/dev/null 2>&1 || true
  k get namespaces -o name > "$WORK/all-ns.txt" 2>/dev/null || true
  local extra
  extra="$(sed -n "s#^namespace/\\($PREFIX-.*\\)#\\1#p" "$WORK/all-ns.txt" 2>/dev/null || true)"
  for ns in "$NS1" "$NS2" "$NS3" $extra; do
    k delete namespace "$ns" --ignore-not-found --wait="$1" --timeout=120s >/dev/null 2>&1 || true
  done
}
cleanup() {
  if [[ -n "$CLUSTER" ]]; then reset_cluster false; fi
  rm -rf "$WORK"
}
trap cleanup EXIT

echo "▸ minienv against the in-memory cluster: create, no-op, push, admission, leftovers, cost, reap"
python3 code/main.py demo --json > "$WORK/demo.json" || fail "demo exited non-zero"
python3 - "$WORK/demo.json" <<'PY' || fail "demo result: $(head -c 3000 "$WORK/demo.json")"
import json, sys
d = json.load(open(sys.argv[1]))
assert d["writes"] == [27, 0, 2], d["writes"]
assert d["remaining"] == ["preview-shop-pr-101"], d["remaining"]
greedy, huge = d["rejections"]
assert "exceeded quota: preview-quota, requested: requests.cpu=1200m" in greedy and "limited: requests.cpu=1" in greedy, greedy
assert "maximum cpu usage per Container is 1, but limit is 2" in huge, huge
reaped = next(e for e in d["events"] if e["step"].startswith("reap at"))["detail"]
assert "preview-shop-pr-103" in reaped and "PersistentVolume/preview-shop-pr-103-data" in reaped and "pr-101" not in reaped, reaped
print("  writes per reconcile", d["writes"], "; remaining", d["remaining"])
PY

echo "▸ render one environment and run admission offline"
python3 code/main.py render --prs code/fixtures/pulls.json --number 102 --prefix "$PREFIX" > "$WORK/render.json" || fail "render exited non-zero"
python3 - "$WORK/render.json" "$PREFIX" <<'PY' || fail "render shape: $(head -c 2000 "$WORK/render.json")"
import json, sys
items = json.load(open(sys.argv[1]))["items"]
kinds = sorted(o["kind"] for o in items)
assert kinds == ["Deployment", "LimitRange", "Namespace", "NetworkPolicy", "NetworkPolicy", "NetworkPolicy", "ResourceQuota", "RoleBinding", "Service"], kinds
ns = items[0]["metadata"]
assert ns["name"] == sys.argv[2] + "-shop-pr-102" and ns["labels"]["preview.cloudcaptain.io/owner"] == "bob", ns
print("  %d objects for %s, expires %s" % (len(items), ns["name"], ns["annotations"]["preview.cloudcaptain.io/expires-at"]))
PY
python3 - "$WORK" <<'PY'
import json, sys
def pod(name, *containers):
    return {"apiVersion": "v1", "kind": "Pod", "metadata": {"name": name},
            "spec": {"containers": [{"name": c, "image": "registry.k8s.io/pause:3.10", "resources": r} for c, r in containers]}}
work = sys.argv[1]
json.dump(pod("plain", ("app", {})), open(f"{work}/plain.json", "w"))
json.dump(pod("huge", ("app", {"limits": {"cpu": "2"}})), open(f"{work}/huge.json", "w"))
json.dump(pod("greedy", ("a", {"limits": {"cpu": "600m"}}), ("b", {"limits": {"cpu": "600m"}})), open(f"{work}/greedy.json", "w"))
PY
python3 - "$WORK/render.json" > "$WORK/offline-objects.json" <<'PY'
import json, sys
items = [o for o in json.load(open(sys.argv[1]))["items"] if o["kind"] in ("LimitRange", "ResourceQuota")]
for o in items:
    if o["kind"] == "ResourceQuota":
        o["status"] = {"hard": dict(o["spec"]["hard"]), "used": {"requests.cpu": "100m", "pods": "1"}}
print(json.dumps({"apiVersion": "v1", "kind": "List", "items": items}))
PY
set +e
python3 code/main.py admit "$WORK/greedy.json" --objects "$WORK/offline-objects.json" > "$WORK/admit.out" 2> "$WORK/admit.err"
CODE=$?
set -e
[[ "$CODE" == "1" ]] || fail "admit should refuse the greedy pod with exit 1, got $CODE: $(cat "$WORK/admit.out" "$WORK/admit.err")"
grep -q "requested: requests.cpu=1200m, used: requests.cpu=100m, limited: requests.cpu=1" "$WORK/admit.err" || fail "offline admission message: $(cat "$WORK/admit.err")"
echo "  $(cat "$WORK/admit.err")"

if ! command -v kubectl >/dev/null; then
  echo "  (kubectl not installed; kind part skipped)"
  echo "SMOKE OK: platform/preview-environments (offline only)"
  exit 0
fi
kubectl config get-contexts -o name > "$WORK/contexts.txt" 2>/dev/null || true
if ! grep -qx kind-cc-smoke "$WORK/contexts.txt"; then
  echo "  (no kind-cc-smoke context; kind part skipped)"
  echo "SMOKE OK: platform/preview-environments (offline only)"
  exit 0
fi
if ! k cluster-info > "$WORK/cluster-info.txt" 2>&1; then
  echo "  (kind-cc-smoke is not reachable; kind part skipped)"
  echo "SMOKE OK: platform/preview-environments (offline only)"
  exit 0
fi
CLUSTER=1
reset_cluster true

NOW="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
LATER="$(python3 -c 'import datetime as d, sys; t = d.datetime.strptime(sys.argv[1], "%Y-%m-%dT%H:%M:%SZ"); print((t + d.timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%SZ"))' "$NOW")"
python3 - "$NOW" > "$WORK/prs.json" <<'PY'
import json, sys
now = sys.argv[1]
def pr(number, login, labels):
    return {"number": number, "state": "open", "user": {"login": login}, "head": {"ref": f"feature/{number}", "sha": f"{number:07d}" + "abcdef0123456789"},
            "labels": [{"name": name} for name in labels], "updated_at": now}
print(json.dumps([pr(101, "alice", ["preview"]), pr(102, "bob", ["preview"]), pr(103, "carol", ["preview", "preview-ttl-1h"])]))
PY

echo "▸ reconcile three pull requests on kind: three namespaces, nine objects each"
minienv reconcile --prs "$WORK/prs.json" --now "$NOW" --json > "$WORK/reconcile1.json" 2> "$WORK/reconcile1.err" || fail "reconcile: $(cat "$WORK/reconcile1.err")"
python3 - "$WORK/reconcile1.json" <<'PY' || fail "first reconcile: $(head -c 3000 "$WORK/reconcile1.json")"
import json, sys
r = json.load(open(sys.argv[1]))
assert [e["pr"] for e in r["environments"]] == [101, 102, 103], r["environments"]
assert all(set(e["actions"].values()) == {"created"} and len(e["actions"]) == 9 for e in r["environments"]), r["environments"]
assert r["writes"] == 27, r["writes"]
print("  " + ", ".join(e["namespace"] + " (" + e["owner"] + ")" for e in r["environments"]))
PY
for ns in "$NS1" "$NS2" "$NS3"; do
  k rollout status deployment/app -n "$ns" --timeout=180s > "$WORK/rollout.txt" 2>&1 || fail "rollout in $ns: $(cat "$WORK/rollout.txt") $(k get pods,events -n "$ns" 2>&1 | tail -20)"
done
k get namespaces -l "$SELECTOR" -L preview.cloudcaptain.io/owner,preview.cloudcaptain.io/pr,preview.cloudcaptain.io/expires > "$WORK/ns-table.txt"
cat "$WORK/ns-table.txt"

echo "▸ the author's RoleBinding allows the author in their own environment and nobody else"
can() { k auth can-i "$1" "$2" -n "$3" --as "$4" 2>/dev/null || true; }
[[ "$(can create deployments.apps "$NS1" alice)" == "yes" ]] || fail "alice should create deployments in $NS1"
[[ "$(can get pods "$NS1" alice)" == "yes" ]] || fail "alice should get pods in $NS1"
[[ "$(can create deployments.apps "$NS1" bob)" == "no" ]] || fail "bob must not create deployments in $NS1"
[[ "$(can create deployments.apps "$NS2" alice)" == "no" ]] || fail "alice must not create deployments in bob's $NS2"
[[ "$(can create rolebindings.rbac.authorization.k8s.io "$NS1" alice)" == "no" ]] || fail "edit must not let alice create RoleBindings"
[[ "$(can get pods "$NS1" mallory)" == "no" ]] || fail "an unrelated user must not read $NS1"
echo "  alice: yes in $NS1, no in $NS2, no RoleBindings; bob and mallory: no in $NS1"

echo "▸ quota and LimitRange on the real API server, compared with minienv admit"
DEADLINE=$((SECONDS + 90))
until [[ "$(k get resourcequota preview-quota -n "$NS1" -o jsonpath='{.status.used.pods}' 2>/dev/null)" == "1" ]]; do
  [[ $SECONDS -lt $DEADLINE ]] || fail "quota usage never showed the app pod: $(k describe resourcequota preview-quota -n "$NS1" 2>&1)"
  sleep 1
done
k get limitranges,resourcequotas -n "$NS1" -o json > "$WORK/live-objects.json"
k create -n "$NS1" --dry-run=server -o json -f "$WORK/plain.json" > "$WORK/plain-live.json" 2> "$WORK/plain-live.err" || fail "plain pod refused: $(cat "$WORK/plain-live.err")"
python3 code/main.py admit "$WORK/plain.json" --objects "$WORK/live-objects.json" > "$WORK/plain-local.json" || fail "minienv admit refused the plain pod"
python3 - "$WORK/plain-live.json" "$WORK/plain-local.json" <<'PY' || fail "LimitRange defaults differ"
import json, sys
sys.path.insert(0, "code")
import main
live, local = (json.load(open(p))["spec"]["containers"][0]["resources"] for p in sys.argv[1:3])
norm = lambda r: {s: {k: main.parse_quantity(v)[0] for k, v in r.get(s, {}).items()} for s in ("requests", "limits")}
assert norm(live) == norm(local), (live, local)
print("  plain pod defaulted by the API server and by minienv alike:", live)
PY
for name in huge greedy; do
  set +e
  k create -n "$NS1" --dry-run=server -f "$WORK/$name.json" > "$WORK/$name-live.out" 2>&1
  LIVE=$?
  python3 code/main.py admit "$WORK/$name.json" --objects "$WORK/live-objects.json" > /dev/null 2> "$WORK/$name-local.err"
  LOCAL=$?
  set -e
  [[ "$LIVE" != "0" ]] || fail "the API server admitted the $name pod: $(cat "$WORK/$name-live.out")"
  [[ "$LOCAL" == "1" ]] || fail "minienv admitted the $name pod: $(cat "$WORK/$name-local.err")"
  grep -q "Forbidden" "$WORK/$name-live.out" || fail "$name: expected Forbidden, got $(cat "$WORK/$name-live.out")"
  echo "  API server: $(cat "$WORK/$name-live.out")"
  echo "  minienv:    $(cat "$WORK/$name-local.err")"
done
grep -q "maximum cpu usage per Container is 1" "$WORK/huge-live.out" && grep -q "limit is 2" "$WORK/huge-live.out" || fail "LimitRange max message: $(cat "$WORK/huge-live.out")"
python3 - "$WORK/greedy-live.out" "$WORK/greedy-local.err" <<'PY' || fail "quota rejections differ"
import sys
sys.path.insert(0, "code")
import main
live, local = (main.parse_rejection(open(p).read()) for p in sys.argv[1:3])
assert live, "the API server's message is not an 'exceeded quota' message"
assert live == local, (live, local)
assert set(live["requested"]) == {"requests.cpu"} and live["limited"]["requests.cpu"] == 1, live
print("  same quota, same exceeded resources, same requested/used/limited values")
PY

echo "▸ a second reconcile is a no-op: no writes, resourceVersions unchanged"
snapshot() {
  k get namespaces,resourcequotas,limitranges,networkpolicies.networking.k8s.io,rolebindings.rbac.authorization.k8s.io,deployments.apps,services \
    -A -l "$SELECTOR" -o json --show-managed-fields > "$1"
}
snapshot "$WORK/before.json"
minienv reconcile --prs "$WORK/prs.json" --now "$NOW" --json > "$WORK/reconcile2.json" 2> "$WORK/reconcile2.err" || fail "second reconcile: $(cat "$WORK/reconcile2.err")"
snapshot "$WORK/after.json"
python3 - "$WORK/reconcile2.json" "$WORK/before.json" "$WORK/after.json" "$PREFIX" <<'PY' || fail "second reconcile was not a no-op"
import json, sys
report = json.load(open(sys.argv[1]))
assert report["writes"] == 0, [e["actions"] for e in report["environments"]]
def index(path):
    out = {}
    for o in json.load(open(path))["items"]:
        m = o["metadata"]
        if (m.get("namespace") or m["name"]).startswith(sys.argv[4]):
            mine = [f for f in m.get("managedFields") or [] if f.get("manager") == "minienv"]
            out[(o["kind"], m.get("namespace", ""), m["name"])] = (m["resourceVersion"], json.dumps(mine, sort_keys=True))
    return out
before, after = index(sys.argv[2]), index(sys.argv[3])
assert len(before) == 27 and before.keys() == after.keys(), (len(before), sorted(set(before) ^ set(after)))
same = [key for key in before if before[key][0] == after[key][0]]
status_only = [key for key in before if before[key][0] != after[key][0]]
for key in status_only:
    assert before[key][1] == after[key][1], ("minienv's managed fields changed", key)
print(f"  0 writes; {len(same)} of {len(before)} resourceVersions identical" + (f", {len(status_only)} changed by a controller's status update only: {status_only}" if status_only else ""))
PY

echo "▸ cost per environment from the live requests"
minienv cost --now "$NOW" --json > "$WORK/cost.json" || fail "cost exited non-zero"
python3 - "$WORK/cost.json" <<'PY' || fail "cost report: $(cat "$WORK/cost.json")"
import json, sys
c = json.load(open(sys.argv[1]))
rows = {r["pr"]: r for r in c["environments"]}
assert sorted(rows) == ["101", "102", "103"], rows
expected = 0.1 * 0.0316 + 0.125 * 0.0042
for r in rows.values():
    assert (r["requests_cpu"], r["requests_memory"]) == ("100m", "128Mi"), r
    assert abs(r["hourly"] - expected) < 1e-9, r
assert abs(rows["103"]["hours_left"] - 1.0) < 0.02 and abs(rows["101"]["hours_left"] - 72.0) < 0.02, rows
print("  %.6f %s/h each; projected %.2f until expiry" % (expected, c["currency"], c["total_projected"]))
PY

echo "▸ cluster-scoped leftovers: a ClusterRoleBinding for 101 and 103, a Released-style PV claimed into 103"
python3 - "$NS1" "$NS3" "$MARK" > "$WORK/leftovers.json" <<'PY'
import json, sys
ns1, ns3, mark = sys.argv[1:4]
key, value = mark.split("=")
def crb(ns):
    return {"apiVersion": "rbac.authorization.k8s.io/v1", "kind": "ClusterRoleBinding",
            "metadata": {"name": ns + "-reader", "labels": {key: value, "preview.cloudcaptain.io/environment": ns}},
            "roleRef": {"apiGroup": "rbac.authorization.k8s.io", "kind": "ClusterRole", "name": "view"},
            "subjects": [{"apiGroup": "rbac.authorization.k8s.io", "kind": "User", "name": "preview-reader"}]}
pv = {"apiVersion": "v1", "kind": "PersistentVolume", "metadata": {"name": ns3 + "-data", "labels": {key: value}},
      "spec": {"capacity": {"storage": "1Gi"}, "accessModes": ["ReadWriteOnce"], "persistentVolumeReclaimPolicy": "Retain", "storageClassName": "",
               "hostPath": {"path": "/tmp/" + ns3}, "claimRef": {"namespace": ns3, "name": "data"}}}
print(json.dumps({"apiVersion": "v1", "kind": "List", "items": [crb(ns1), crb(ns3), pv]}))
PY
k apply -f "$WORK/leftovers.json" > "$WORK/leftovers.out" 2>&1 || fail "apply leftovers: $(cat "$WORK/leftovers.out")"

echo "▸ reap two hours later: dry run first, then for real; only the expired environment goes"
minienv reap --prs "$WORK/prs.json" --now "$LATER" --dry-run --json > "$WORK/reap-dry.json" || fail "reap --dry-run exited non-zero"
k get namespace "$NS3" -o jsonpath='{.status.phase}' > "$WORK/ns3-phase.txt" || fail "dry run deleted $NS3"
[[ "$(cat "$WORK/ns3-phase.txt")" == "Active" ]] || fail "$NS3 is $(cat "$WORK/ns3-phase.txt") after a dry run"
minienv reap --prs "$WORK/prs.json" --now "$LATER" --json > "$WORK/reap.json" || fail "reap exited non-zero"
python3 - "$WORK/reap-dry.json" "$WORK/reap.json" "$NS3" <<'PY' || fail "reap reports: $(cat "$WORK/reap.json")"
import json, sys
dry, real = (json.load(open(p)) for p in sys.argv[1:3])
ns3 = sys.argv[3]
for r in (dry, real):
    assert [d["namespace"] for d in r["deleted"]] == [ns3], r["deleted"]
    assert r["deleted"][0]["reason"].startswith("expired at"), r["deleted"]
    assert r["deleted"][0]["leftovers"] == [f"ClusterRoleBinding/{ns3}-reader", f"PersistentVolume/{ns3}-data"], r["deleted"]
    assert len(r["kept"]) == 2, r["kept"]
print("  deleted", ns3, "(" + real["deleted"][0]["reason"] + ") with", ", ".join(real["deleted"][0]["leftovers"]))
PY
k wait --for=delete "namespace/$NS3" --timeout=120s > /dev/null 2>&1 || fail "$NS3 was not deleted: $(k get namespace "$NS3" -o yaml 2>&1 | tail -20)"
k wait --for=delete "clusterrolebinding/$NS3-reader" "persistentvolume/$NS3-data" --timeout=60s > /dev/null 2>&1 || fail "leftovers of $NS3 survived: $(k get clusterrolebinding,pv -l "$MARK" 2>&1)"
for ns in "$NS1" "$NS2"; do
  PHASE="$(k get namespace "$ns" -o jsonpath='{.status.phase}' 2>&1 || true)"
  [[ "$PHASE" == "Active" ]] || fail "$ns should be untouched, phase: $PHASE"
done
k get clusterrolebinding "$NS1-reader" > /dev/null 2>&1 || fail "the reaper deleted $NS1's ClusterRoleBinding"
minienv list --now "$LATER" --json > "$WORK/list.json" || fail "list exited non-zero"
python3 - "$WORK/list.json" "$NS1" "$NS2" <<'PY' || fail "list after reap: $(cat "$WORK/list.json")"
import json, sys
names = [r["namespace"] for r in json.load(open(sys.argv[1]))]
assert names == sys.argv[2:4], names
print("  remaining:", ", ".join(names))
PY

echo "SMOKE OK: platform/preview-environments"
