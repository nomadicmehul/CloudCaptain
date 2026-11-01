#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."

fail() { echo "SMOKE FAIL: $*" >&2; exit 1; }
WORK="$(mktemp -d)"
NS="cc-reconciler"
CTX="${CC_SMOKE_CONTEXT:-kind-cc-smoke}"
NS_CREATED=""
k() { kubectl --context "$CTX" "$@"; }
cleanup() {
  if [[ -n "$NS_CREATED" ]]; then k delete namespace "$NS" --ignore-not-found --wait=false >/dev/null 2>&1 || true; fi
  rm -rf "$WORK"
}
trap cleanup EXIT

echo "▸ minictl: every scenario runs and prints JSON"
python3 code/main.py list > "$WORK/list.txt" || fail "minictl list"
for s in self-heal rolling rollback conflict quarantine gc resync relist; do
  grep -q "^$s " "$WORK/list.txt" || fail "scenario $s is missing from: $(cat "$WORK/list.txt")"
  python3 code/main.py run "$s" --json > "$WORK/$s.json" 2> "$WORK/$s.err" || fail "scenario $s failed: $(cat "$WORK/$s.err")"
done

echo "▸ the event logs and summaries say what the controllers did"
python3 - "$WORK" <<'PY' || fail "scenario assertions"
import json, sys
work = sys.argv[1]
def load(name):
    return json.load(open("%s/%s.json" % (work, name)))
def reasons(data):
    return [e["reason"] for e in data["events"]]

data = load("self-heal")
s = data["summary"]
assert s["victim_replaced"] and s["same_replicaset"], s
assert reasons(data).count("SuccessfulCreate") >= 6, reasons(data)
assert any(e["reason"] == "ScalingReplicaSet" and "to 3 from 0" in e["message"] for e in data["events"]), data["events"][:3]
stats = data["stats"]["replicaset-controller"]
assert stats["events"] > stats["reconciles"], stats
print("  self-heal: victim %s replaced by %s under the same ReplicaSet; %d events -> %d reconciles" % (s["victim"], s["replacement"], stats["events"], stats["reconciles"]))

data = load("rolling")
s = data["summary"]
assert (s["max_pods"], s["min_ready"], s["revisions"]) == (4, 3, [1, 2]), s
scaling = [e["message"] for e in data["events"] if e["reason"] == "ScalingReplicaSet"]
first_down = next(i for i, m in enumerate(scaling) if m.startswith("Scaled down"))
assert any(m.startswith("Scaled up") and "to 1 from 0" in m for m in scaling[:first_down]), scaling
assert "successfully rolled out" in s["status"], s
print("  rolling: at most %d pods, at least %d ready, revisions %s" % (s["max_pods"], s["min_ready"], s["revisions"]))

data = load("rollback")
s = data["summary"]
assert "exceeded its progress deadline" in s["stuck_message"], s
assert s["pods_while_stuck"] == 4 and s["min_ready"] == 3, s
assert s["history_before_undo"] == [1, 2] and s["history_after_undo"] == [2, 3], s
assert s["reused_first_replicaset"] and s["image_after"] == "web:1.0", s
print("  rollback: stalled with 3 old pods serving, undo reused the first ReplicaSet, history %s" % s["history_after_undo"])

data = load("conflict")
s = data["summary"]
assert "the object has been modified" in s["stale_update_error"], s
assert s["controller_conflicts"] == 1 and s["ready_pods"] == 4, s
assert any(e["reason"] == "Conflict" and "requeue in 1 tick(s)" in e["message"] for e in data["events"]), data["events"]
print("  conflict: stale write got 409; the controller's lost race was requeued once and succeeded")

data = load("quarantine")
s = data["summary"]
assert s["quarantined_owner"] is None and s["quarantined_still_running"] and len(s["serving_pods"]) == 3, s
assert s["quarantined"] not in s["serving_pods"], s
print("  quarantine: %s released, still running, replaced" % s["quarantined"])

data = load("gc")
s = data["summary"]
assert s["web_objects_left"] == 0 and s["db_owners"][0] is None and len(s["db_objects_left"]) == 2, s
assert any(e["source"] == "garbage-collector" for e in data["events"]), data["events"]
print("  gc: web cascaded away, db survived orphaned")

data = load("resync")
s = data["summary"]
assert s["pods_while_blind"] == 2 and s["healed_at_tick"] > s["deleted_at_tick"] + 3, s
print("  resync: blind at tick %d with 2 pods, healed at tick %d (period %d)" % (s["deleted_at_tick"], s["healed_at_tick"], s["resync_period"]))

data = load("relist")
s = data["summary"]
assert s["relists"] == 2 and "too old resource version" in s["gone"] and s["ready_pods"] == 3, s
print("  relist: %s -> relisted and converged" % s["gone"])
PY

echo "▸ select evaluates label selectors; bad input exits 2"
cat > "$WORK/items.json" <<'EOF'
{"kind": "List", "items": [
  {"metadata": {"name": "a", "labels": {"app": "web", "env": "prod"}}},
  {"metadata": {"name": "b", "labels": {"app": "web"}}},
  {"metadata": {"name": "c", "labels": {"app": "db", "env": "prod"}}}
]}
EOF
[[ "$(python3 code/main.py select 'app=web,env!=dev' --file "$WORK/items.json" | tr '\n' ' ')" == "a b " ]] || fail "select app=web,env!=dev"
[[ "$(python3 code/main.py select 'env in (prod),!canary' --file "$WORK/items.json" | tr '\n' ' ')" == "a c " ]] || fail "select env in (prod),!canary"
CODE=0
python3 code/main.py run no-such-scenario > /dev/null 2> "$WORK/nope.err" || CODE=$?
[[ "$CODE" == "2" ]] && grep -q "unknown scenario" "$WORK/nope.err" || fail "unknown scenario should exit 2 (got $CODE): $(cat "$WORK/nope.err")"
CODE=0
python3 code/main.py select 'not a selector' --file "$WORK/items.json" > /dev/null 2> "$WORK/bad.err" || CODE=$?
[[ "$CODE" == "2" ]] || fail "an unparsable selector should exit 2 (got $CODE)"

if ! command -v kubectl >/dev/null; then
  echo "  (kubectl not installed; cluster checks skipped)"
  echo "SMOKE OK: orchestration/reconciler (minictl only)"
  exit 0
fi
kubectl config get-contexts -o name > "$WORK/contexts.txt" 2>/dev/null || true
if ! grep -qx "$CTX" "$WORK/contexts.txt"; then
  echo "  (context $CTX not found; the cluster checks only run against the throwaway kind cluster)"
  echo "SMOKE OK: orchestration/reconciler (minictl only)"
  exit 0
fi

poll() {
  local what="$1" tries="$2"
  shift 2
  for _ in $(seq 1 "$tries"); do
    if "$@" >/dev/null 2>&1; then return 0; fi
    sleep 1
  done
  fail "timed out waiting for $what"
}
ready_names() {
  k get pods -n "$NS" -l "app=$1" -o json | python3 -c '
import json, sys
for pod in json.load(sys.stdin)["items"]:
    if pod["metadata"].get("deletionTimestamp"):
        continue
    conditions = pod.get("status", {}).get("conditions", [])
    if any(c["type"] == "Ready" and c["status"] == "True" for c in conditions):
        print(pod["metadata"]["name"])
'
}
ready_without() {
  local names count
  names="$(ready_names "$1")"
  count="$(printf '%s\n' "$names" | grep -c . || true)"
  [[ "$count" == "$2" ]] || return 1
  [[ -z "${3:-}" ]] || ! grep -qx "$3" <<<"$names"
}
orphaned() {
  [[ "$(k get rs -n "$NS" -l "app=$1" -o name | wc -l | tr -d ' ')" == "1" ]] || return 1
  [[ -z "$(k get rs -n "$NS" -l "app=$1" -o jsonpath='{.items[0].metadata.ownerReferences}')" ]]
}
gone() {
  [[ -z "$(k get rs,pods -n "$NS" -l "app=$1" -o name)" ]]
}
released() {
  [[ -z "$(k get pod "$1" -n "$NS" -o jsonpath='{.metadata.ownerReferences}')" ]]
}

k delete namespace "$NS" --ignore-not-found --wait=true --timeout=90s >/dev/null 2>&1 || true
NS_CREATED=1
k create namespace "$NS" >/dev/null || fail "create namespace $NS"
for _ in $(seq 1 30); do k get serviceaccount default -n "$NS" >/dev/null 2>&1 && break; sleep 1; done
k get serviceaccount default -n "$NS" >/dev/null 2>&1 || fail "the default ServiceAccount never appeared in $NS"

echo "▸ Use It: a Deployment of 3 becomes one ReplicaSet named by the template hash, owned by the Deployment"
k create deployment web --image=registry.k8s.io/pause:3.10 --replicas=3 -n "$NS" >/dev/null || fail "create deployment web"
k rollout status deployment/web -n "$NS" --timeout=180s > "$WORK/rollout0.txt" 2>&1 || fail "web did not roll out: $(cat "$WORK/rollout0.txt")"
k get rs -n "$NS" -l app=web -o json > "$WORK/rs0.json"
k get pods -n "$NS" -l app=web -o json > "$WORK/pods0.json"
python3 - "$WORK" <<'PY' || fail "initial structure"
import json, sys
work = sys.argv[1]
sets = json.load(open(work + "/rs0.json"))["items"]
assert len(sets) == 1, [s["metadata"]["name"] for s in sets]
rs = sets[0]
digest = rs["metadata"]["labels"]["pod-template-hash"]
assert rs["metadata"]["name"] == "web-" + digest, rs["metadata"]["name"]
ref = rs["metadata"]["ownerReferences"][0]
assert (ref["kind"], ref["name"], ref["controller"], ref["blockOwnerDeletion"]) == ("Deployment", "web", True, True), ref
assert rs["metadata"]["annotations"]["deployment.kubernetes.io/revision"] == "1"
pods = json.load(open(work + "/pods0.json"))["items"]
assert len(pods) == 3, len(pods)
for pod in pods:
    assert pod["metadata"]["labels"]["pod-template-hash"] == digest
    ref = pod["metadata"]["ownerReferences"][0]
    assert (ref["kind"], ref["name"], ref["controller"]) == ("ReplicaSet", rs["metadata"]["name"], True), ref
print("  ReplicaSet %s owned by Deployment/web at revision 1; 3 pods carry pod-template-hash=%s and name it as their controller" % (rs["metadata"]["name"], digest))
PY
FIRST_RS="$(k get rs -n "$NS" -l app=web -o jsonpath='{.items[0].metadata.name}')"

echo "▸ delete a pod: the ReplicaSet creates a replacement under the same owner"
k get pods -n "$NS" -l app=web -o jsonpath='{range .items[*]}{.metadata.name}{"\n"}{end}' > "$WORK/original-pods.txt"
VICTIM="$(head -1 "$WORK/original-pods.txt")"
k delete pod "$VICTIM" -n "$NS" --wait=true >/dev/null || fail "delete pod $VICTIM"
poll "3 ready pods without $VICTIM" 120 ready_without web 3 "$VICTIM"
OWNERS="$(k get pods -n "$NS" -l app=web -o jsonpath='{range .items[*]}{.metadata.ownerReferences[0].name}{"\n"}{end}' | sort -u)"
[[ "$OWNERS" == "$FIRST_RS" ]] || fail "every pod should be owned by $FIRST_RS, got: $OWNERS"
k get events -n "$NS" --field-selector reason=SuccessfulCreate -o json > "$WORK/events-create.json"
python3 - "$WORK/events-create.json" <<'PY' || fail "SuccessfulCreate events"
import json, sys
items = json.load(open(sys.argv[1]))["items"]
assert len(items) >= 4, [i["message"] for i in items]
assert all(i["message"].startswith("Created pod: web-") for i in items), [i["message"] for i in items]
print("  %d SuccessfulCreate events (3 initial + the replacement)" % len(items))
PY

echo "▸ a watch from a resourceVersion replays what happened since"
k get --raw "/api/v1/namespaces/$NS/pods" > "$WORK/pods-list.json" || fail "raw pod list"
RV="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["metadata"]["resourceVersion"])' "$WORK/pods-list.json")"
[[ -n "$RV" ]] || fail "the raw list carried no resourceVersion"
LISTRV="$(k get pods -n "$NS" -o jsonpath='{.metadata.resourceVersion}')"
echo "  list resourceVersion $RV from the API server; kubectl's own List wrapper says '${LISTRV}'"
k get pods -n "$NS" -l app=web -o jsonpath='{range .items[*]}{.metadata.name}{"\n"}{end}' > "$WORK/before-watch.txt"
VICTIM2="$(head -1 "$WORK/before-watch.txt")"
k delete pod "$VICTIM2" -n "$NS" --wait=true >/dev/null || fail "delete pod $VICTIM2"
poll "3 ready pods without $VICTIM2" 120 ready_without web 3 "$VICTIM2"
k get --raw "/api/v1/namespaces/$NS/pods?watch=1&resourceVersion=$RV&timeoutSeconds=4" > "$WORK/watch.jsonl" 2> "$WORK/watch.err" || true
python3 - "$WORK" "$VICTIM2" <<'PY' || fail "watch stream"
import json, sys
work, victim = sys.argv[1], sys.argv[2]
before = set(open(work + "/before-watch.txt").read().split())
events = [json.loads(line) for line in open(work + "/watch.jsonl") if line.strip()]
assert events, "the watch returned nothing: " + open(work + "/watch.err").read()
assert all(e["type"] in ("ADDED", "MODIFIED", "DELETED", "BOOKMARK") for e in events), [e["type"] for e in events]
kinds = [(e["type"], e["object"]["metadata"]["name"]) for e in events if e["type"] != "BOOKMARK"]
assert ("DELETED", victim) in kinds, kinds
assert any(t == "ADDED" and n not in before for t, n in kinds), kinds
assert all(e["object"]["metadata"].get("resourceVersion") for e in events)
print("  %d events replayed from the old version: DELETED %s, ADDED a replacement" % (len(events), victim))
PY
k get --raw "/api/v1/namespaces/$NS/pods?watch=1&resourceVersion=1&timeoutSeconds=2" > "$WORK/watch-old.txt" 2>&1 || true
if grep -qE '410|Expired|too old resource version|Gone' "$WORK/watch-old.txt"; then
  echo "  a watch from resourceVersion=1 is answered with 410/Expired, the real Gone"
else
  echo "  (note: resourceVersion=1 was not answered with 410 here: $(head -c 200 "$WORK/watch-old.txt"))"
fi

echo "▸ label selectors: minictl select and kubectl -l agree on the same objects"
cat > "$WORK/cms.json" <<'EOF'
{"apiVersion": "v1", "kind": "List", "items": [
  {"apiVersion": "v1", "kind": "ConfigMap", "metadata": {"name": "cc-a", "labels": {"app": "web", "tier": "frontend", "env": "prod"}}},
  {"apiVersion": "v1", "kind": "ConfigMap", "metadata": {"name": "cc-b", "labels": {"app": "web", "tier": "backend"}}},
  {"apiVersion": "v1", "kind": "ConfigMap", "metadata": {"name": "cc-c", "labels": {"app": "db", "tier": "backend", "env": "prod"}}},
  {"apiVersion": "v1", "kind": "ConfigMap", "metadata": {"name": "cc-d", "labels": {"other": "1"}}}
]}
EOF
k apply -n "$NS" -f "$WORK/cms.json" >/dev/null || fail "apply the ConfigMaps"
k get cm -n "$NS" -o json > "$WORK/cm-live.json"
SELECTORS=('app=web' 'app==web' 'app!=web' 'tier in (frontend,backend)' 'tier notin (frontend)' 'env' '!env' 'app=web,env=prod' 'app=web,env!=prod' 'app in (web,db),!env')
for SEL in "${SELECTORS[@]}"; do
  k get cm -n "$NS" -l "$SEL" -o jsonpath='{range .items[*]}{.metadata.name}{"\n"}{end}' | sort > "$WORK/sel-real.txt"
  python3 code/main.py select "$SEL" --file "$WORK/cm-live.json" | sort > "$WORK/sel-mine.txt"
  cmp -s "$WORK/sel-real.txt" "$WORK/sel-mine.txt" || fail "selector '$SEL': kubectl returned [$(tr '\n' ' ' < "$WORK/sel-real.txt")] but minictl returned [$(tr '\n' ' ' < "$WORK/sel-mine.txt")]"
done
echo "  ${#SELECTORS[@]} selectors, identical results (including the negative operators on objects that lack the key)"

echo "▸ set image: a second ReplicaSet with a different template hash, rolled out without a gap"
CNAME="$(k get deploy web -n "$NS" -o jsonpath='{.spec.template.spec.containers[0].name}')"
LIMITS="$(k get deploy web -n "$NS" -o jsonpath='{.spec.strategy.rollingUpdate.maxSurge}/{.spec.strategy.rollingUpdate.maxUnavailable}')"
[[ "$LIMITS" == "25%/25%" ]] || fail "default rolling update limits should be 25%/25%, got $LIMITS"
python3 - <<'PY' || fail "minictl limits for 3 replicas"
import sys
sys.path.insert(0, "code")
import main
limits = main.rolling_limits(main.deployment_manifest("web", "i", 3, max_surge="25%", max_unavailable="25%"))
assert limits == (1, 0), limits
print("  25%/25% of 3 replicas is surge %d, unavailable %d (surge rounds up, unavailable rounds down)" % limits)
PY
k set image deployment/web "$CNAME=registry.k8s.io/pause:3.9" -n "$NS" >/dev/null || fail "set image"
k rollout status deployment/web -n "$NS" --timeout=180s > "$WORK/rollout1.txt" 2>&1 || fail "rollout did not finish: $(cat "$WORK/rollout1.txt")"
grep -q "successfully rolled out" "$WORK/rollout1.txt" || fail "rollout status output: $(cat "$WORK/rollout1.txt")"
k get rs -n "$NS" -l app=web -o json > "$WORK/rs1.json"
python3 - "$WORK/rs1.json" <<'PY' || fail "two ReplicaSets"
import json, sys
sets = json.load(open(sys.argv[1]))["items"]
assert len(sets) == 2, [s["metadata"]["name"] for s in sets]
hashes = {s["metadata"]["labels"]["pod-template-hash"] for s in sets}
assert len(hashes) == 2, hashes
assert sorted(s["spec"]["replicas"] for s in sets) == [0, 3], [s["spec"]["replicas"] for s in sets]
assert {s["metadata"]["annotations"]["deployment.kubernetes.io/revision"] for s in sets} == {"1", "2"}
for s in sets:
    assert s["metadata"]["name"] == "web-" + s["metadata"]["labels"]["pod-template-hash"]
print("  two ReplicaSets %s; the old one is kept at 0 replicas for rollback" % sorted(s["metadata"]["name"] for s in sets))
PY
k rollout history deployment/web -n "$NS" > "$WORK/history1.txt"
REVS="$(awk '/^[0-9]+/ {print $1}' "$WORK/history1.txt" | tr '\n' ' ')"
[[ "$REVS" == "1 2 " ]] || fail "rollout history should list revisions 1 and 2, got: $(cat "$WORK/history1.txt")"
k get events -n "$NS" --field-selector reason=ScalingReplicaSet -o json > "$WORK/events-scaling.json"
python3 - "$WORK/events-scaling.json" <<'PY' || fail "ScalingReplicaSet events"
import json, sys
messages = [i["message"] for i in json.load(open(sys.argv[1]))["items"]]
assert any(m.startswith("Scaled up replica set web-") for m in messages), messages
assert any(m.startswith("Scaled down replica set web-") for m in messages), messages
print("  deployment-controller events: " + "; ".join(sorted(messages))[:200])
PY

echo "▸ rollout undo: the first ReplicaSet is reused and renumbered, history moves forward"
k rollout undo deployment/web -n "$NS" >/dev/null || fail "rollout undo"
k rollout status deployment/web -n "$NS" --timeout=180s > "$WORK/rollout2.txt" 2>&1 || fail "undo did not finish: $(cat "$WORK/rollout2.txt")"
k rollout history deployment/web -n "$NS" > "$WORK/history2.txt"
REVS="$(awk '/^[0-9]+/ {print $1}' "$WORK/history2.txt" | tr '\n' ' ')"
[[ "$REVS" == "2 3 " ]] || fail "after undo the history should be 2 and 3, got: $(cat "$WORK/history2.txt")"
k get rs -n "$NS" -l app=web -o json > "$WORK/rs2.json"
python3 - "$WORK/rs2.json" "$FIRST_RS" <<'PY' || fail "undo reuses the old ReplicaSet"
import json, sys
sets = json.load(open(sys.argv[1]))["items"]
assert len(sets) == 2, [s["metadata"]["name"] for s in sets]
active = [s for s in sets if s["spec"]["replicas"] == 3]
assert len(active) == 1 and active[0]["metadata"]["name"] == sys.argv[2], [s["metadata"]["name"] for s in sets]
assert active[0]["metadata"]["annotations"]["deployment.kubernetes.io/revision"] == "3", active[0]["metadata"]["annotations"]
print("  %s is active again as revision 3" % sys.argv[2])
PY
IMAGE="$(k get deploy web -n "$NS" -o jsonpath='{.spec.template.spec.containers[0].image}')"
[[ "$IMAGE" == "registry.k8s.io/pause:3.10" ]] || fail "undo should restore pause:3.10, got $IMAGE"
python3 - "$WORK/rollback.json" <<'PY' || fail "minictl rollback has the same shape"
import json, sys
s = json.load(open(sys.argv[1]))["summary"]
assert s["history_after_undo"] == [2, 3] and s["reused_first_replicaset"], s
print("  minictl rollback scenario: history %s, first ReplicaSet reused (the same shape)" % s["history_after_undo"])
PY

echo "▸ scale changes replicas only (no new ReplicaSet); rollout restart is a template annotation"
k scale deployment/web --replicas=5 -n "$NS" >/dev/null || fail "scale to 5"
k wait --for=jsonpath='{.status.readyReplicas}'=5 deployment/web -n "$NS" --timeout=120s >/dev/null || fail "5 replicas never became ready"
[[ "$(k get rs -n "$NS" -l app=web -o name | wc -l | tr -d ' ')" == "2" ]] || fail "scale must not create a ReplicaSet"
k scale deployment/web --replicas=2 -n "$NS" >/dev/null || fail "scale to 2"
poll "2 ready pods" 120 ready_without web 2
k rollout restart deployment/web -n "$NS" >/dev/null || fail "rollout restart"
k rollout status deployment/web -n "$NS" --timeout=180s > "$WORK/rollout3.txt" 2>&1 || fail "restart did not finish: $(cat "$WORK/rollout3.txt")"
k get deploy web -n "$NS" -o jsonpath='{.spec.template.metadata.annotations}' > "$WORK/restart-annotation.txt"
grep -q "kubectl.kubernetes.io/restartedAt" "$WORK/restart-annotation.txt" || fail "rollout restart should annotate the pod template: $(cat "$WORK/restart-annotation.txt")"
[[ "$(k get rs -n "$NS" -l app=web -o name | wc -l | tr -d ' ')" == "3" ]] || fail "rollout restart should create a third ReplicaSet (same image, new template hash)"
echo "  scale kept 2 ReplicaSets; rollout restart added a third with the same image and a new hash"

echo "▸ quarantine: removing the selector label releases the pod and the ReplicaSet creates a replacement"
SICK="$(k get pods -n "$NS" -l app=web -o jsonpath='{.items[0].metadata.name}')"
k label pod "$SICK" app- -n "$NS" >/dev/null || fail "remove the app label from $SICK"
poll "2 ready pods without $SICK" 120 ready_without web 2 "$SICK"
poll "$SICK to lose its owner reference" 60 released "$SICK"
PHASE="$(k get pod "$SICK" -n "$NS" -o jsonpath='{.status.phase}')"
[[ "$PHASE" == "Running" ]] || fail "the quarantined pod should keep running, phase is $PHASE"
python3 - "$WORK/quarantine.json" <<'PY' || fail "minictl quarantine has the same shape"
import json, sys
s = json.load(open(sys.argv[1]))["summary"]
assert s["quarantined_owner"] is None and s["quarantined_still_running"], s
print("  %s is Running with no owner; minictl quarantine scenario does the same" % "the pod")
PY

echo "▸ 409: a stale replace is refused with the same words as minictl's Conflict"
k get deployment web -n "$NS" -o json > "$WORK/saved.json"
k annotate deployment web -n "$NS" cc-smoke/touch=1 --overwrite >/dev/null || fail "annotate"
CODE=0
k replace -f "$WORK/saved.json" > "$WORK/conflict.txt" 2>&1 || CODE=$?
[[ "$CODE" != "0" ]] || fail "replace with a stale resourceVersion should fail"
grep -q "Conflict" "$WORK/conflict.txt" || fail "expected a Conflict error, got: $(cat "$WORK/conflict.txt")"
grep -q "the object has been modified" "$WORK/conflict.txt" || fail "expected 'the object has been modified', got: $(cat "$WORK/conflict.txt")"
sed -n 's/.*\(the object has been modified.*\)$/\1/p' "$WORK/conflict.txt" > "$WORK/conflict-sentence.txt"
SENTENCE="$(head -1 "$WORK/conflict-sentence.txt")"
python3 - "$WORK/conflict.json" "$SENTENCE" <<'PY' || fail "minictl's Conflict wording differs from the API server's"
import json, sys
message = json.load(open(sys.argv[1]))["summary"]["stale_update_error"]
assert sys.argv[2] in message, (sys.argv[2], message)
print("  both say: " + sys.argv[2])
PY
k get deployment web -n "$NS" -o json > "$WORK/fresh.json"
k replace -f "$WORK/fresh.json" >/dev/null || fail "replace with the fresh copy should succeed"

echo "▸ garbage collection follows ownerReferences; --cascade=orphan strips them"
k delete deployment web -n "$NS" --wait=true >/dev/null || fail "delete deployment web"
poll "web's ReplicaSets and pods to be garbage collected" 90 gone web
k create deployment keep --image=registry.k8s.io/pause:3.10 -n "$NS" >/dev/null || fail "create deployment keep"
k rollout status deployment/keep -n "$NS" --timeout=180s >/dev/null || fail "keep did not roll out"
k delete deployment keep -n "$NS" --cascade=orphan --wait=true >/dev/null || fail "orphan delete"
poll "keep's ReplicaSet to lose its owner reference" 60 orphaned keep
[[ "$(k get pods -n "$NS" -l app=keep -o name | wc -l | tr -d ' ')" == "1" ]] || fail "the orphaned pod should still exist"
python3 - "$WORK/gc.json" <<'PY' || fail "minictl gc has the same shape"
import json, sys
s = json.load(open(sys.argv[1]))["summary"]
assert s["web_objects_left"] == 0 and s["db_owners"][0] is None, s
print("  cascade removed web's ReplicaSet and pods; orphan kept keep's ReplicaSet and pod (minictl gc scenario agrees)")
PY

HOLDER="$(k -n kube-system get lease kube-controller-manager -o jsonpath='{.spec.holderIdentity}' 2>/dev/null || true)"
if [[ -n "$HOLDER" ]]; then
  echo "▸ the loops run in kube-controller-manager; Lease holder: $HOLDER"
else
  echo "  (lease kube-controller-manager is not readable here; skipped)"
fi

echo "SMOKE OK: orchestration/reconciler"
