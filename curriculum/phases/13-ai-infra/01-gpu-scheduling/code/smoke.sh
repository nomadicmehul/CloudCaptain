#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."

fail() { echo "SMOKE FAIL: $*" >&2; exit 1; }
WORK="$(mktemp -d)"
NS="cc-gpu-scheduling"
CTX="${CC_SMOKE_CONTEXT:-kind-cc-smoke}"
F="code/fixtures"
RES="example.com/gpu"
RES_PATH="example.com~1gpu"
CLASS="cc-gpu-scheduling"
SLICE_LABEL="cloudcaptain.io/lesson=gpu-scheduling"
NODE=""
PATCHED=""
NS_CREATED=""
DRA_CREATED=""
k() { kubectl --context "$CTX" "$@"; }
cleanup() {
  if [[ -n "$NS_CREATED" ]]; then
    k delete pods --all -n "$NS" --grace-period=0 --force --wait=false >/dev/null 2>&1 || true
    k delete namespace "$NS" --ignore-not-found --wait=false >/dev/null 2>&1 || true
  fi
  if [[ -n "$DRA_CREATED" ]]; then
    k delete resourceslices -l "$SLICE_LABEL" --ignore-not-found >/dev/null 2>&1 || true
    k delete deviceclass "$CLASS" --ignore-not-found >/dev/null 2>&1 || true
  fi
  if [[ -n "$PATCHED" ]]; then
    k patch node "$NODE" --subresource=status --type=json -p "[{\"op\":\"remove\",\"path\":\"/status/capacity/$RES_PATH\"}]" >/dev/null 2>&1 || true
    k patch node "$NODE" --subresource=status --type=json -p "[{\"op\":\"remove\",\"path\":\"/status/allocatable/$RES_PATH\"}]" >/dev/null 2>&1 || true
  fi
  rm -rf "$WORK"
}
trap cleanup EXIT

gs() {
  local out="$1"
  shift
  local code=0
  python3 code/main.py "$@" > "$out" 2> "$out.err" || code=$?
  echo "$code"
}
field() {
  python3 - "$@" <<'PY'
import json, sys
value = json.load(open(sys.argv[1]))
for key in sys.argv[2:]:
    if isinstance(value, list) and not key.isdigit():
        value = [item for item in value if item.get("pod") == key or item.get("name") == key or item.get("claim") == key][0]
    else:
        value = value[int(key)] if isinstance(value, list) else value[key]
print(json.dumps(value, sort_keys=True) if isinstance(value, (dict, list)) else value)
PY
}

echo "▸ gpusched place: bin-pack keeps a whole NVLink node free for the 8-GPU job"
CODE="$(gs "$WORK/binpack.json" place "$F/cluster.json" "$F/pods.json" --json)"
[[ "$CODE" == "0" ]] || fail "binpack should place every pod (exit $CODE): $(cat "$WORK/binpack.json" "$WORK/binpack.json.err")"
[[ "$(field "$WORK/binpack.json" decisions train-8 placement node)" == "dgx-b" ]] || fail "train-8 should land on dgx-b: $(field "$WORK/binpack.json" decisions train-8)"
python3 code/main.py place "$F/cluster.json" "$F/pods.json" > "$WORK/binpack.txt"
grep -q "all-reduce of 14 GB over 300 GB/s: 0.082 s per step" "$WORK/binpack.txt" || fail "train-8 all-reduce estimate missing: $(cat "$WORK/binpack.txt")"
echo "  train-8 -> dgx-b, all-reduce 0.082 s per step over NVLink"

echo "▸ gpusched place: spread puts two notebooks on every node and leaves the 8-GPU job Pending"
CODE="$(gs "$WORK/spread.json" place "$F/cluster.json" "$F/pods.json" --policy spread --json)"
[[ "$CODE" == "1" ]] || fail "spread should leave train-8 Pending (exit $CODE)"
MSG="$(field "$WORK/spread.json" decisions train-8 message)"
[[ "$MSG" == "0/5 nodes are available: 1 GPU model is not A100-SXM4-80GB, 1 node is time-sliced, 3 Insufficient nvidia.com/gpu." ]] || fail "spread message: $MSG"
[[ "$(field "$WORK/spread.json" fragmentation rows 3 stranded_pct)" == "100.0" ]] || fail "spread should strand every free GPU for an 8-GPU job: $(field "$WORK/spread.json" fragmentation rows)"
echo "  $MSG"

echo "▸ gpusched place: topology-aware puts single GPUs on the PCIe pairs and keeps DGX nodes whole"
CODE="$(gs "$WORK/topology.json" place "$F/cluster.json" "$F/pods.json" --policy topology --json)"
[[ "$CODE" == "0" ]] || fail "topology should place every pod (exit $CODE)"
[[ "$(field "$WORK/topology.json" decisions notebook-5 placement node)" == "pcie-a" && "$(field "$WORK/topology.json" decisions train-8 placement node)" == "dgx-a" ]] || fail "topology placements: $(cat "$WORK/topology.json")"
[[ "$(field "$WORK/topology.json" fragmentation largest_in_island)" == "7" ]] || fail "topology should leave a 7-GPU island: $(field "$WORK/topology.json" fragmentation)"

echo "▸ gpusched place --view extended: the count fits, the GPU does not"
CODE="$(gs "$WORK/extended.json" place "$F/cluster.json" "$F/pods.json" --view extended --json)"
[[ "$(field "$WORK/extended.json" decisions embed placement node)" == "l4-a" ]] || fail "embed should land on the time-sliced L4 when only counts are visible: $(field "$WORK/extended.json" decisions embed)"
grep -q "out of memory when the weights load" "$WORK/extended.json" || fail "the extended view should report the OOM: $(field "$WORK/extended.json" decisions embed)"
python3 code/main.py place "$F/cluster.json" "$F/pods.json" --policy topology --view extended > "$WORK/bad.txt" 2>&1 && fail "topology with --view extended should be refused"
echo "  embed -> l4-a: $(field "$WORK/extended.json" decisions embed placement problems 0)"

echo "▸ gpusched frag: free GPUs that no 4-GPU NVLink job can use, and MIG slices no 3g.40gb fits into"
CODE="$(gs "$WORK/frag.json" frag "$F/fragmented.json" --json)"
[[ "$CODE" == "0" ]] || fail "frag exit $CODE"
[[ "$(field "$WORK/frag.json" rows 2 by_count)" == "1" && "$(field "$WORK/frag.json" rows 2 by_island)" == "0" ]] || fail "size 4: $(field "$WORK/frag.json" rows 2)"
[[ "$(field "$WORK/frag.json" mig 0 repartition_could_add)" == '["1g.10gb", "2g.20gb"]' ]] || fail "MIG repartition: $(field "$WORK/frag.json" mig 0)"

echo "▸ gpusched gang: pod-by-pod deadlocks, all-or-nothing finishes, a timeout costs idle GPU-hours"
CODE="$(gs "$WORK/pod.json" gang "$F/gang-cluster.json" "$F/jobs.json" --mode pod --json)"
[[ "$CODE" == "1" && "$(field "$WORK/pod.json" outcome)" == "deadlock" ]] || fail "pod mode should deadlock (exit $CODE): $(cat "$WORK/pod.json")"
CODE="$(gs "$WORK/gang.json" gang "$F/gang-cluster.json" "$F/jobs.json" --mode gang --json)"
[[ "$CODE" == "0" && "$(field "$WORK/gang.json" makespan)" == "6600.0" && "$(field "$WORK/gang.json" idle_gpu_hours)" == "0.0" ]] || fail "gang mode: $(cat "$WORK/gang.json")"
CODE="$(gs "$WORK/hold.json" gang "$F/gang-cluster.json" "$F/jobs.json" --mode gang --timeout 300 --json)"
[[ "$CODE" == "0" && "$(field "$WORK/hold.json" idle_gpu_hours)" == "2.644" ]] || fail "gang with timeout: $(cat "$WORK/hold.json")"
echo "  pod: $(field "$WORK/pod.json" detail)"

echo "▸ gpusched dra: selectors, matchAttribute and MIG devices against the cluster model"
CODE="$(gs "$WORK/dra.json" dra --cluster "$F/cluster.json" --objects "$F/dra.json" --json)"
[[ "$CODE" == "1" ]] || fail "the h200 claim should not be allocatable (exit $CODE)"
[[ "$(field "$WORK/dra.json" claims ml/nvlink-pair allocation devices results 1 device)" == "gpu-1" && "$(field "$WORK/dra.json" claims ml/rerank-mig node)" == "mig-a" ]] || fail "dra allocations: $(cat "$WORK/dra.json")"
[[ "$(field "$WORK/dra.json" claims ml/h200 allocated)" == "False" ]] || fail "h200: $(field "$WORK/dra.json" claims ml/h200)"

echo "▸ bad input exits 2 with the reason"
echo '[{"name": "x", "mig": "9g.90gb"}]' > "$WORK/bad-pods.json"
CODE="$(gs "$WORK/bad.out" place "$F/cluster.json" "$WORK/bad-pods.json")"
[[ "$CODE" == "2" ]] && grep -q "unknown MIG profile" "$WORK/bad.out.err" || fail "bad pods should exit 2: code $CODE, $(cat "$WORK/bad.out.err")"

if ! command -v kubectl >/dev/null; then
  echo "  (kubectl not installed; cluster checks skipped)"
  echo "SMOKE OK: ai-infra/gpu-scheduling (gpusched only)"
  exit 0
fi
kubectl config get-contexts -o name > "$WORK/contexts.txt" 2>/dev/null || true
if ! grep -qx "$CTX" "$WORK/contexts.txt"; then
  echo "  (context $CTX not found; this smoke patches node status, so it only runs against the throwaway kind cluster)"
  echo "SMOKE OK: ai-infra/gpu-scheduling (gpusched only)"
  exit 0
fi

snapshot() {
  k get nodes -o json > "$WORK/nodes.json"
  k get pods -A -o json > "$WORK/pods.json"
}
write_pod() {
  python3 - "$WORK/$1.json" "$NS" "$1" "$2" <<'PY'
import json, sys
path, namespace, name, gpus = sys.argv[1:]
resources = {"limits": {"example.com/gpu": gpus}} if gpus else {}
spec = {"terminationGracePeriodSeconds": 0, "containers": [{"name": "main", "image": "registry.k8s.io/pause:3.10", "resources": resources}]}
if not gpus:
    spec["resourceClaims"] = [{"name": "gpus", "resourceClaimName": "pair"}]
    spec["containers"][0]["resources"] = {"claims": [{"name": "gpus"}]}
with open(path, "w", encoding="utf-8") as handle:
    json.dump({"apiVersion": "v1", "kind": "Pod", "metadata": {"name": name, "namespace": namespace}, "spec": spec}, handle, indent=2)
PY
}
scheduled_message() {
  k get pod "$1" -n "$NS" -o jsonpath='{.status.conditions[?(@.type=="PodScheduled")].message}'
}
first_sentence() {
  python3 -c 'import sys; text = sys.argv[1]; head = text.split(". ")[0]; print(head if head.endswith(".") else head + ".")' "$1"
}

schedulable_node() {
  python3 - "$WORK/nodes.json" <<'PY'
import json, sys
for item in json.load(open(sys.argv[1]))["items"]:
    spec = item.get("spec") or {}
    if spec.get("unschedulable") or any(t.get("effect") in ("NoSchedule", "NoExecute") for t in spec.get("taints") or []):
        continue
    print(item["metadata"]["name"])
    break
PY
}
# A node that just turned Ready keeps node.kubernetes.io/not-ready:NoSchedule until the
# node lifecycle controller removes it a few seconds later, so give a fresh cluster time.
NODE=""
for _ in $(seq 1 45); do
  snapshot
  NODE="$(schedulable_node)"
  [[ -n "$NODE" ]] && break
  sleep 2
done
[[ -n "$NODE" ]] || fail "no untainted, schedulable node in $CTX after 90 s: $(k get nodes -o custom-columns=NAME:.metadata.name,UNSCHEDULABLE:.spec.unschedulable,TAINTS:.spec.taints 2>&1)"
N="$(python3 -c 'import json, sys; print(len(json.load(open(sys.argv[1]))["items"]))' "$WORK/nodes.json")"
echo "▸ Use It on $CTX: advertise 4 x $RES on $NODE through the node's status subresource ($N node(s))"
k delete namespace "$NS" --ignore-not-found --wait=true --timeout=90s >/dev/null 2>&1 || true
NS_CREATED=1
k create namespace "$NS" >/dev/null || fail "create namespace $NS"
for _ in $(seq 1 30); do k get serviceaccount default -n "$NS" >/dev/null 2>&1 && break; sleep 1; done
k get serviceaccount default -n "$NS" >/dev/null 2>&1 || fail "the default ServiceAccount never appeared in $NS"
PATCHED=1
k patch node "$NODE" --subresource=status --type=json -p "[{\"op\":\"add\",\"path\":\"/status/capacity/$RES_PATH\",\"value\":\"4\"},{\"op\":\"add\",\"path\":\"/status/allocatable/$RES_PATH\",\"value\":\"4\"}]" >/dev/null || fail "patch node status of $NODE"
k wait "node/$NODE" --for=jsonpath='{.status.allocatable.example\.com/gpu}'=4 --timeout=60s >/dev/null 2>&1 || true
k get node "$NODE" -o json > "$WORK/node.json"
python3 - "$WORK/node.json" <<'PY2' || fail "capacity and allocatable should both say $RES=4: $(k get node "$NODE" -o jsonpath='{.status.capacity}{" "}{.status.allocatable}')"
import json, sys
status = json.load(open(sys.argv[1]))["status"]
print("  capacity    %s" % {k: v for k, v in status["capacity"].items() if "/" in k or k in ("cpu", "memory")})
print("  allocatable %s" % {k: v for k, v in status["allocatable"].items() if "/" in k or k in ("cpu", "memory")})
sys.exit(0 if status["capacity"].get("example.com/gpu") == "4" and status["allocatable"].get("example.com/gpu") == "4" else 1)
PY2

echo "▸ gpu-a (2) and gpu-b (1) fit: gpusched predicts the node, kube-scheduler binds there"
for spec in gpu-a:2 gpu-b:1; do
  name="${spec%%:*}"
  count="${spec##*:}"
  write_pod "$name" "$count"
  snapshot
  CODE="$(gs "$WORK/predict-$name.json" k8s --nodes "$WORK/nodes.json" --pods "$WORK/pods.json" --pod "$WORK/$name.json" --json)"
  [[ "$CODE" == "0" ]] || fail "gpusched says $name does not fit: $(cat "$WORK/predict-$name.json" "$WORK/predict-$name.json.err")"
  PREDICTED="$(field "$WORK/predict-$name.json" node)"
  k apply -f "$WORK/$name.json" >/dev/null || fail "apply $name"
  k wait --for=condition=PodScheduled "pod/$name" -n "$NS" --timeout=60s >/dev/null || fail "$name should be bound: $(k get pod "$name" -n "$NS" -o wide 2>&1)"
  BOUND="$(k get pod "$name" -n "$NS" -o jsonpath='{.spec.nodeName}')"
  [[ "$BOUND" == "$PREDICTED" && "$BOUND" == "$NODE" ]] || fail "$name bound to $BOUND, gpusched predicted $PREDICTED, only $NODE advertises $RES"
  echo "  $name ($count) -> $BOUND"
done

echo "▸ gpu-c (2) does not fit: the same Pending reason from gpusched and from kube-scheduler"
write_pod gpu-c 2
snapshot
CODE="$(gs "$WORK/predict-gpu-c.json" k8s --nodes "$WORK/nodes.json" --pods "$WORK/pods.json" --pod "$WORK/gpu-c.json" --json)"
MINI="$(field "$WORK/predict-gpu-c.json" message)"
[[ "$CODE" == "1" && "$MINI" == *"Insufficient $RES"* ]] || fail "gpusched should predict Insufficient $RES (exit $CODE): $MINI"
k apply -f "$WORK/gpu-c.json" >/dev/null || fail "apply gpu-c"
k wait --for=condition=PodScheduled=false pod/gpu-c -n "$NS" --timeout=60s >/dev/null || fail "gpu-c should have PodScheduled=False: $(k get pod gpu-c -n "$NS" -o wide 2>&1)"
REAL="$(scheduled_message gpu-c)"
echo "  gpusched:       $MINI"
echo "  kube-scheduler: $REAL"
[[ "$(first_sentence "$REAL")" == "$MINI" ]] || fail "first sentences differ: gpusched '$MINI', kube-scheduler '$REAL'"
k describe node "$NODE" > "$WORK/describe.txt"
sed -n '/Allocated resources/,/Events/p' "$WORK/describe.txt" | grep "example.com/gpu" > "$WORK/allocated.txt" || fail "describe node should list $RES under Allocated resources: $(cat "$WORK/describe.txt")"
echo "  Allocated resources:$(cat "$WORK/allocated.txt")"

echo "▸ delete gpu-a: two GPUs come back and the Pending pod is bound without being recreated"
k delete pod gpu-a -n "$NS" --wait=true --timeout=60s >/dev/null || fail "delete gpu-a"
k wait --for=condition=PodScheduled pod/gpu-c -n "$NS" --timeout=90s >/dev/null || fail "gpu-c should be bound once gpu-a is gone: $(k get pod gpu-c -n "$NS" -o wide 2>&1)"

echo "▸ extended resources are integers: a fractional request is refused by the API server"
python3 - "$WORK/frac.json" "$NS" <<'PY'
import json, sys
spec = {"containers": [{"name": "main", "image": "registry.k8s.io/pause:3.10", "resources": {"limits": {"example.com/gpu": "500m"}}}]}
json.dump({"apiVersion": "v1", "kind": "Pod", "metadata": {"name": "frac", "namespace": sys.argv[2]}, "spec": spec}, open(sys.argv[1], "w"))
PY
if k apply --dry-run=server -f "$WORK/frac.json" > "$WORK/frac.out" 2>&1; then
  fail "a request of 500m $RES should be rejected: $(cat "$WORK/frac.out")"
fi
grep -qi "integer" "$WORK/frac.out" || echo "  (note: rejection did not mention 'integer': $(cat "$WORK/frac.out"))"
echo "  $(head -c 200 "$WORK/frac.out")"

k api-versions > "$WORK/apis.txt" 2>/dev/null || true
API=""
for candidate in resource.k8s.io/v1 resource.k8s.io/v1beta2; do
  if grep -qx "$candidate" "$WORK/apis.txt"; then API="$candidate"; break; fi
done
if [[ -z "$API" ]]; then
  echo "  (resource.k8s.io/v1 is not served by this cluster; Dynamic Resource Allocation checks skipped)"
  echo "SMOKE OK: ai-infra/gpu-scheduling (extended resources only)"
  exit 0
fi

echo "▸ DRA ($API): a DeviceClass and a ResourceClaim with no driver behind them"
k get resourceslices -o json > "$WORK/slices-before.json" || fail "list resourceslices"
python3 - "$WORK/dra-objects.json" "$API" "$NS" "$CLASS" <<'PY'
import json, sys
path, api, namespace, name = sys.argv[1:]
device_class = {"apiVersion": api, "kind": "DeviceClass", "metadata": {"name": name, "labels": {"cloudcaptain.io/lesson": "gpu-scheduling"}},
                "spec": {"selectors": [{"cel": {"expression": 'device.driver == "gpu.example.com"'}}]}}
request = {"name": "gpus", "exactly": {"deviceClassName": name, "allocationMode": "ExactCount", "count": 2,
           "selectors": [{"cel": {"expression": 'device.capacity["gpu.example.com"].memory.compareTo(quantity("40Gi")) >= 0'}}]}}
claim = {"apiVersion": api, "kind": "ResourceClaim", "metadata": {"name": "pair", "namespace": namespace},
         "spec": {"devices": {"requests": [request], "constraints": [{"requests": ["gpus"], "matchAttribute": "gpu.example.com/nvlinkIsland"}]}}}
json.dump({"apiVersion": "v1", "kind": "List", "items": [device_class, claim]}, open(path, "w"), indent=2)
PY
DRA_CREATED=1
k apply -f "$WORK/dra-objects.json" >/dev/null || fail "apply DeviceClass and ResourceClaim: $(cat "$WORK/dra-objects.json")"
write_pod cc-dra ""
k apply -f "$WORK/cc-dra.json" >/dev/null || fail "apply cc-dra"
k wait --for=condition=PodScheduled=false pod/cc-dra -n "$NS" --timeout=60s >/dev/null || fail "cc-dra should have PodScheduled=False: $(k get pod cc-dra -n "$NS" -o wide 2>&1)"
REAL="$(scheduled_message cc-dra)"
echo "  kube-scheduler: $REAL"
[[ "$(echo "$REAL" | tr 'A-Z' 'a-z')" == *claim* ]] || fail "the Pending reason should name the claim: $REAL"
k get resourceclaim pair -n "$NS" -o json > "$WORK/claim.json"
python3 -c 'import json, sys; c = json.load(open(sys.argv[1])); sys.exit(1 if (c.get("status") or {}).get("allocation") else 0)' "$WORK/claim.json" || fail "the claim should stay unallocated: $(cat "$WORK/claim.json")"
snapshot
k get deviceclasses,resourceslices -o json > "$WORK/cluster-dra.json"
k get resourceclaims -n "$NS" -o json > "$WORK/claims.json"
CODE="$(gs "$WORK/predict-dra.json" k8s --nodes "$WORK/nodes.json" --pods "$WORK/pods.json" --pod "$WORK/cc-dra.json" --objects "$WORK/cluster-dra.json" --objects "$WORK/claims.json" --json)"
MINI="$(field "$WORK/predict-dra.json" message)"
echo "  gpusched:       $MINI"
[[ "$CODE" == "1" && "$MINI" == *"cannot allocate all claims"* ]] || fail "gpusched should predict an unallocatable claim (exit $CODE): $(cat "$WORK/predict-dra.json" "$WORK/predict-dra.json.err")"

echo "▸ publish a ResourceSlice by hand (gpusched slices): islands {0,2} and {1,3}, so matchAttribute must pick gpu-0 and gpu-2"
python3 - "$WORK/kind-model.json" "$NODE" <<'PY'
import json, sys
gpu = {"model": "A100-PCIE-80GB", "count": 4, "memory_gib": 80, "islands": [[0, 2], [1, 3]], "nvlink_gbps": 300, "pcie_gbps": 32}
json.dump({"nodes": [{"name": sys.argv[2], "gpu": gpu}]}, open(sys.argv[1], "w"))
PY
python3 code/main.py slices "$WORK/kind-model.json" --api-version "$API" > "$WORK/slice.json" || fail "gpusched slices"
CODE="$(gs "$WORK/predict-slice.json" k8s --nodes "$WORK/nodes.json" --pods "$WORK/pods.json" --pod "$WORK/cc-dra.json" --objects "$WORK/cluster-dra.json" --objects "$WORK/claims.json" --objects "$WORK/slice.json" --json)"
[[ "$CODE" == "0" ]] || fail "gpusched should allocate the claim once the slice exists: $(cat "$WORK/predict-slice.json" "$WORK/predict-slice.json.err")"
PREDICTED="$(python3 -c 'import json, sys; r = json.load(open(sys.argv[1])); row = [n for n in r["nodes"] if n["feasible"]][0]; print(",".join(d["device"] for d in row["allocations"][0]["allocation"]["devices"]["results"]))' "$WORK/predict-slice.json")"
[[ "$PREDICTED" == "gpu-0,gpu-2" ]] || fail "gpusched picked $PREDICTED"
k apply -f "$WORK/slice.json" >/dev/null || fail "apply the ResourceSlice: $(cat "$WORK/slice.json")"
if ! k wait --for=condition=PodScheduled pod/cc-dra -n "$NS" --timeout=90s >/dev/null 2>&1; then
  if ! k get resourceslices -l "$SLICE_LABEL" -o name > "$WORK/slice-left.txt" 2>/dev/null || [[ ! -s "$WORK/slice-left.txt" ]]; then
    echo "  (the hand-published ResourceSlice was removed before allocation; real-allocator comparison skipped)"
    echo "SMOKE OK: ai-infra/gpu-scheduling (allocation comparison skipped)"
    exit 0
  fi
  echo "  (not requeued within 90s; recreating the pod)"
  k delete pod cc-dra -n "$NS" --grace-period=0 --force --wait=true --timeout=60s >/dev/null 2>&1 || true
  k apply -f "$WORK/cc-dra.json" >/dev/null || fail "re-apply cc-dra"
  k wait --for=condition=PodScheduled pod/cc-dra -n "$NS" --timeout=60s >/dev/null || fail "cc-dra should be bound once a slice can satisfy the claim: $(scheduled_message cc-dra)"
fi
k get resourceclaim pair -n "$NS" -o json > "$WORK/claim.json"
REAL="$(python3 -c 'import json, sys; c = json.load(open(sys.argv[1])); print(",".join(r["device"] for r in c["status"]["allocation"]["devices"]["results"]))' "$WORK/claim.json")" || fail "claim has no allocation: $(cat "$WORK/claim.json")"
echo "  gpusched: $PREDICTED   DynamicResources plugin: $REAL"
[[ "$REAL" == "$PREDICTED" ]] || fail "allocations differ: gpusched $PREDICTED, kube-scheduler $REAL"
[[ "$(k get pod cc-dra -n "$NS" -o jsonpath='{.spec.nodeName}')" == "$NODE" ]] || fail "cc-dra should be bound to $NODE"
PHASE="$(k get pod cc-dra -n "$NS" -o jsonpath='{.status.phase}')"
k get events -n "$NS" --field-selector involvedObject.name=cc-dra -o json > "$WORK/dra-events.json" 2>/dev/null || true
python3 - "$WORK/dra-events.json" <<'PY' || true
import json, sys
items = json.load(open(sys.argv[1])).get("items") or []
for item in items[-3:]:
    print("  event %s: %s" % (item.get("reason"), (item.get("message") or "")[:160]))
PY
echo "  pod phase $PHASE: the scheduler allocated, but no kubelet plugin for gpu.example.com exists to prepare the devices"

echo "SMOKE OK: ai-infra/gpu-scheduling"
