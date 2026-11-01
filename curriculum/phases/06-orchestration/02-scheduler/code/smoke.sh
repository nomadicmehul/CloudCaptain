#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."

fail() { echo "SMOKE FAIL: $*" >&2; exit 1; }
WORK="$(mktemp -d)"
NS="cc-scheduler"
CTX="${CC_SMOKE_CONTEXT:-kind-cc-smoke}"
F="code/fixtures"
NODES=()
NS_CREATED=""
CORDONED=""
k() { kubectl --context "$CTX" "$@"; }
cleanup() {
  for n in ${NODES[@]+"${NODES[@]}"}; do
    k taint nodes "$n" cc-smoke- >/dev/null 2>&1 || true
    if [[ -n "$CORDONED" ]]; then k uncordon "$n" >/dev/null 2>&1 || true; fi
  done
  if [[ -n "$NS_CREATED" ]]; then k delete namespace "$NS" --ignore-not-found --wait=false >/dev/null 2>&1 || true; fi
  rm -rf "$WORK"
}
trap cleanup EXIT

mini() {
  local out="$1"
  shift
  local code=0
  python3 code/main.py "$@" --json > "$out" || code=$?
  echo "$code"
}
field() {
  python3 - "$@" <<'PY'
import json, sys
value = json.load(open(sys.argv[1]))
for key in sys.argv[2:]:
    value = value[int(key)] if isinstance(value, list) else value[key]
print(json.dumps(value) if isinstance(value, (dict, list)) else value)
PY
}

echo "▸ minisched on the fixture cluster: a pod that fits is bound, with per-plugin scores"
python3 code/main.py --nodes "$F/nodes.json" --pods "$F/pods.json" --pod "$F/pod-web.json" > "$WORK/web.txt" || fail "web should schedule: $(cat "$WORK/web.txt")"
cat "$WORK/web.txt"
grep -q "^bind default/web to worker-a$" "$WORK/web.txt" || fail "LeastAllocated should pick worker-a: $(cat "$WORK/web.txt")"
CODE="$(mini "$WORK/web-most.json" --nodes "$F/nodes.json" --pods "$F/pods.json" --pod "$F/pod-web.json" --strategy MostAllocated)"
[[ "$CODE" == "0" && "$(field "$WORK/web-most.json" node)" == "worker-b" ]] || fail "MostAllocated should bin-pack onto worker-b: $(cat "$WORK/web-most.json")"
echo "  LeastAllocated -> worker-a, MostAllocated -> worker-b"

echo "▸ FailedScheduling messages in kube-scheduler's format, first failing filter per node"
CODE="$(mini "$WORK/big.json" --nodes "$F/nodes.json" --pods "$F/pods.json" --pod "$F/pod-big.json")"
[[ "$CODE" == "1" ]] || fail "big should not schedule (exit $CODE)"
WANT="0/3 nodes are available: 1 node(s) had untolerated taint(s), 2 Insufficient cpu."
[[ "$(field "$WORK/big.json" filter_message)" == "$WANT" ]] || fail "big: $(field "$WORK/big.json" filter_message)"
echo "  $(field "$WORK/big.json" message)"
CODE="$(mini "$WORK/gpu.json" --nodes "$F/nodes.json" --pods "$F/pods.json" --pod "$F/pod-gpu.json")"
[[ "$CODE" == "1" && "$(field "$WORK/gpu.json" filter_message)" == *"2 node(s) didn't match Pod's node affinity/selector."* ]] || fail "gpu: $(cat "$WORK/gpu.json")"
CODE="$(mini "$WORK/spread.json" --nodes "$F/nodes.json" --pods "$F/pods.json" --pod "$F/pod-spread.json")"
[[ "$CODE" == "1" && "$(field "$WORK/spread.json" filter_message)" == *"2 node(s) didn't match pod topology spread constraints."* ]] || fail "spread: $(cat "$WORK/spread.json")"
sed 's/"DoNotSchedule"/"DoNotSchedule", "nodeTaintsPolicy": "Honor"/' "$F/pod-spread.json" > "$WORK/spread-honor.json"
CODE="$(mini "$WORK/spread-honor.json.out" --nodes "$F/nodes.json" --pods "$F/pods.json" --pod "$WORK/spread-honor.json")"
[[ "$CODE" == "0" ]] || fail "nodeTaintsPolicy: Honor should drop the tainted domain from the skew: $(cat "$WORK/spread-honor.json.out")"
echo "  gpu: NodeAffinity before NodeResourcesFit; spread: tainted cp-1 counts as an empty domain until nodeTaintsPolicy is Honor"

echo "▸ preemption: a higher-priority pod nominates a node and names its victims"
CODE="$(mini "$WORK/urgent.json" --nodes "$F/nodes.json" --pods "$F/pods.json" --pod "$F/pod-urgent.json")"
[[ "$CODE" == "1" ]] || fail "urgent is still unschedulable in this cycle (exit $CODE)"
[[ "$(field "$WORK/urgent.json" preemption node)" == "worker-a" && "$(field "$WORK/urgent.json" preemption victims)" == '["default/batch-1"]' ]] || fail "urgent preemption: $(field "$WORK/urgent.json" preemption)"
echo "  $(field "$WORK/urgent.json" preemption message)"

echo "▸ bad input exits 2 with the reason"
echo '{"kind": "Pod", "metadata": {"name": "x"}, "spec": {"containers": [{"name": "a", "resources": {"requests": {"cpu": "two"}}}]}}' > "$WORK/bad.json"
CODE=0
python3 code/main.py --nodes "$F/nodes.json" --pod "$WORK/bad.json" > /dev/null 2> "$WORK/bad.err" || CODE=$?
[[ "$CODE" == "2" ]] && grep -q "not a Kubernetes quantity" "$WORK/bad.err" || fail "bad quantity should exit 2: code $CODE, $(cat "$WORK/bad.err")"

if ! command -v kubectl >/dev/null; then
  echo "  (kubectl not installed; cluster checks skipped)"
  echo "SMOKE OK: orchestration/scheduler (minisched only)"
  exit 0
fi
kubectl config get-contexts -o name > "$WORK/contexts.txt" 2>/dev/null || true
if ! grep -qx "$CTX" "$WORK/contexts.txt"; then
  echo "  (context $CTX not found; this smoke taints and cordons nodes, so it only runs against the throwaway kind cluster)"
  echo "SMOKE OK: orchestration/scheduler (minisched only)"
  exit 0
fi
k get nodes -o jsonpath='{range .items[*]}{.metadata.name}{"\n"}{end}' > "$WORK/node-names.txt" || fail "cannot list nodes in $CTX"
while IFS= read -r line; do [[ -n "$line" ]] && NODES+=("$line"); done < "$WORK/node-names.txt"
N="${#NODES[@]}"
[[ "$N" -ge 1 ]] || fail "no nodes in $CTX"
for n in "${NODES[@]}"; do k taint nodes "$n" cc-smoke- >/dev/null 2>&1 || true; done

snapshot() {
  k get nodes -o json > "$WORK/nodes.json"
  k get pods -A -o json > "$WORK/pods.json"
}
write_pod() {
  python3 - "$WORK/$1.json" "$NS" "$1" "$2" "${3:-}" <<'PY'
import json, sys
path, namespace, name, cpu, tolerate = sys.argv[1:]
spec = {"terminationGracePeriodSeconds": 0, "containers": [{"name": "main", "image": "registry.k8s.io/pause:3.10", "resources": {"requests": {"cpu": cpu}}}]}
if tolerate:
    spec["tolerations"] = [{"key": "cc-smoke", "operator": "Equal", "value": "1", "effect": "NoSchedule"}]
with open(path, "w", encoding="utf-8") as handle:
    json.dump({"apiVersion": "v1", "kind": "Pod", "metadata": {"name": name, "namespace": namespace, "labels": {"app": name}}, "spec": spec}, handle, indent=2)
PY
}
expect_pending() {
  k wait --for=condition=PodScheduled=false "pod/$1" -n "$NS" --timeout=60s >/dev/null || fail "$1 should have PodScheduled=False: $(k get pod "$1" -n "$NS" -o wide 2>&1)"
}
event_message() {
  for _ in $(seq 1 30); do
    k get events -n "$NS" --field-selector "reason=FailedScheduling,involvedObject.name=$1" -o json > "$WORK/events.json" 2>/dev/null || true
    if python3 - "$WORK/events.json" > "$WORK/event.txt" 2>/dev/null <<'PY'
import json, sys
items = json.load(open(sys.argv[1])).get("items") or []
if not items:
    raise SystemExit(1)
items.sort(key=lambda e: e.get("lastTimestamp") or e.get("eventTime") or "")
print(items[-1].get("message", ""))
PY
    then
      cat "$WORK/event.txt"
      return 0
    fi
    sleep 1
  done
  return 1
}
same_first_sentence() {
  local cut=" preemption:"
  local mine="${1%%"$cut"*}" theirs="${2%%"$cut"*}"
  if [[ "$mine" == "$theirs" ]]; then
    echo "  first sentence identical: $mine"
  else
    echo "  (note: first sentences differ; minisched: $mine | kube-scheduler: $theirs)"
  fi
}

echo "▸ Use It on $CTX: capacity versus allocatable on $N node(s)"
snapshot
python3 - "$WORK/nodes.json" <<'PY'
import json, sys
for item in json.load(open(sys.argv[1]))["items"]:
    cap, alloc = item["status"]["capacity"], item["status"]["allocatable"]
    print("  %s  capacity cpu=%s memory=%s pods=%s  allocatable cpu=%s memory=%s pods=%s  taints=%s" % (
        item["metadata"]["name"], cap["cpu"], cap["memory"], cap["pods"], alloc["cpu"], alloc["memory"], alloc["pods"], item["spec"].get("taints", [])))
PY
k delete namespace "$NS" --ignore-not-found --wait=true --timeout=90s >/dev/null 2>&1 || true
NS_CREATED=1
k create namespace "$NS" >/dev/null || fail "create namespace $NS"
for _ in $(seq 1 30); do k get serviceaccount default -n "$NS" >/dev/null 2>&1 && break; sleep 1; done
k get serviceaccount default -n "$NS" >/dev/null 2>&1 || fail "the default ServiceAccount never appeared in $NS"

echo "▸ 1000 CPUs: minisched on the real node JSON predicts Insufficient cpu"
write_pod cc-big 1000
CODE="$(mini "$WORK/mini-big.json" --nodes "$WORK/nodes.json" --pods "$WORK/pods.json" --pod "$WORK/cc-big.json")"
[[ "$CODE" == "1" ]] || fail "minisched should refuse cc-big (exit $CODE): $(cat "$WORK/mini-big.json")"
MINI="$(field "$WORK/mini-big.json" message)"
echo "  minisched:      $MINI"
[[ "$MINI" == *"0/$N nodes are available: $N Insufficient cpu."* ]] || fail "minisched message for cc-big: $MINI"

echo "▸ the same pod on the cluster: PodScheduled=False and a FailedScheduling event that says Insufficient cpu"
k apply -f "$WORK/cc-big.json" >/dev/null || fail "apply cc-big"
expect_pending cc-big
REASON="$(k get pod cc-big -n "$NS" -o jsonpath='{.status.conditions[?(@.type=="PodScheduled")].reason}')"
[[ "$REASON" == "Unschedulable" ]] || fail "PodScheduled reason should be Unschedulable, got: $REASON"
REAL="$(event_message cc-big)" || fail "no FailedScheduling event for cc-big: $(k get events -n "$NS" 2>&1)"
echo "  kube-scheduler: $REAL"
[[ "$REAL" == *"Insufficient cpu"* && "$REAL" == *"nodes are available"* ]] || fail "event should say Insufficient cpu: $REAL"
same_first_sentence "$MINI" "$REAL"
k describe pod cc-big -n "$NS" > "$WORK/describe.txt"
grep -q "FailedScheduling" "$WORK/describe.txt" || fail "kubectl describe should list the FailedScheduling event: $(cat "$WORK/describe.txt")"
k delete pod cc-big -n "$NS" --wait=false >/dev/null

echo "▸ taint every node cc-smoke=1:NoSchedule: a plain pod stays Pending; both report the taint, minisched's per-node detail names it"
for n in "${NODES[@]}"; do k taint nodes "$n" cc-smoke=1:NoSchedule --overwrite >/dev/null || fail "taint $n"; done
snapshot
write_pod cc-plain 50m
write_pod cc-tolerates 50m tolerate
CODE="$(mini "$WORK/mini-plain.json" --nodes "$WORK/nodes.json" --pods "$WORK/pods.json" --pod "$WORK/cc-plain.json")"
MINI="$(field "$WORK/mini-plain.json" message)"
[[ "$CODE" == "1" && "$MINI" == *"$N node(s) had untolerated taint(s)"* ]] || fail "minisched should report the untolerated taint (exit $CODE): $MINI"
grep -q "cc-smoke=1:NoSchedule" "$WORK/mini-plain.json" || fail "minisched's per-node detail should name the cc-smoke taint: $(cat "$WORK/mini-plain.json")"
echo "  minisched:      $MINI"
k apply -f "$WORK/cc-plain.json" >/dev/null || fail "apply cc-plain"
expect_pending cc-plain
REAL="$(event_message cc-plain)" || fail "no FailedScheduling event for cc-plain"
echo "  kube-scheduler: $REAL"
[[ "$REAL" == *"untolerated taint"* ]] || fail "event should report an untolerated taint: $REAL"
same_first_sentence "$MINI" "$REAL"

echo "▸ a pod that tolerates the taint is bound where minisched predicts"
CODE="$(mini "$WORK/mini-tolerates.json" --nodes "$WORK/nodes.json" --pods "$WORK/pods.json" --pod "$WORK/cc-tolerates.json")"
[[ "$CODE" == "0" ]] || fail "minisched should bind cc-tolerates: $(cat "$WORK/mini-tolerates.json")"
PREDICTED="$(field "$WORK/mini-tolerates.json" node)"
k apply -f "$WORK/cc-tolerates.json" >/dev/null || fail "apply cc-tolerates"
k wait --for=condition=PodScheduled pod/cc-tolerates -n "$NS" --timeout=60s >/dev/null || fail "cc-tolerates should be bound: $(k get pod cc-tolerates -n "$NS" -o wide 2>&1)"
BOUND="$(k get pod cc-tolerates -n "$NS" -o jsonpath='{.spec.nodeName}')"
if [[ "$N" == "1" ]]; then
  [[ "$BOUND" == "$PREDICTED" ]] || fail "bound to $BOUND, minisched predicted $PREDICTED"
else
  [[ " ${NODES[*]} " == *" $BOUND "* ]] || fail "bound to unknown node $BOUND"
fi
echo "  minisched: $PREDICTED, kube-scheduler: $BOUND"

echo "▸ remove the taint: the Pending pod is requeued and bound without being recreated"
for n in "${NODES[@]}"; do k taint nodes "$n" cc-smoke- >/dev/null || fail "untaint $n"; done
k wait --for=condition=PodScheduled pod/cc-plain -n "$NS" --timeout=90s >/dev/null || fail "cc-plain should be bound once the taint is gone: $(k get pod cc-plain -n "$NS" -o wide 2>&1)"

echo "▸ cordon every node: NodeUnschedulable runs first and the message says so"
CORDONED=1
for n in "${NODES[@]}"; do k cordon "$n" >/dev/null || fail "cordon $n"; done
snapshot
write_pod cc-cordoned 50m
CODE="$(mini "$WORK/mini-cordoned.json" --nodes "$WORK/nodes.json" --pods "$WORK/pods.json" --pod "$WORK/cc-cordoned.json")"
MINI="$(field "$WORK/mini-cordoned.json" message)"
[[ "$CODE" == "1" && "$MINI" == *"$N node(s) were unschedulable"* ]] || fail "minisched should report the cordon (exit $CODE): $MINI"
echo "  minisched:      $MINI"
k apply -f "$WORK/cc-cordoned.json" >/dev/null || fail "apply cc-cordoned"
expect_pending cc-cordoned
REAL="$(event_message cc-cordoned)" || fail "no FailedScheduling event for cc-cordoned"
echo "  kube-scheduler: $REAL"
[[ "$REAL" == *"unschedulable"* ]] || fail "event should say the node(s) were unschedulable: $REAL"
same_first_sentence "$MINI" "$REAL"
for n in "${NODES[@]}"; do k uncordon "$n" >/dev/null || fail "uncordon $n"; done
CORDONED=""
k wait --for=condition=PodScheduled pod/cc-cordoned -n "$NS" --timeout=90s >/dev/null || fail "cc-cordoned should be bound after uncordon: $(k get pod cc-cordoned -n "$NS" -o wide 2>&1)"
k get pods -n "$NS" -o wide > "$WORK/wide.txt"
cat "$WORK/wide.txt"

if command -v docker >/dev/null && docker inspect "${NODES[0]}" >/dev/null 2>&1; then
  echo "▸ the scheduler itself: a static pod manifest on the kind node"
  docker exec "${NODES[0]}" cat /etc/kubernetes/manifests/kube-scheduler.yaml > "$WORK/kube-scheduler.yaml" || fail "read the kube-scheduler manifest"
  grep -q -- "- kube-scheduler" "$WORK/kube-scheduler.yaml" || fail "unexpected scheduler manifest: $(cat "$WORK/kube-scheduler.yaml")"
  grep -E -- "--(config|kubeconfig|leader-elect)" "$WORK/kube-scheduler.yaml" | sed 's/^ */  /' || true
else
  echo "  (docker or the kind node container not available; scheduler manifest check skipped)"
fi

echo "SMOKE OK: orchestration/scheduler"
