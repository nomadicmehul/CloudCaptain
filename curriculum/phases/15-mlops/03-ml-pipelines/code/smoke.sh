#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."

fail() { echo "SMOKE FAIL: $*" >&2; exit 1; }
WORK="$(mktemp -d)"
NS="cc-ml-pipelines"
CTX="${CC_SMOKE_CONTEXT:-kind-cc-smoke}"
LABEL_KEY="cc-lesson"
LABEL_VALUE="ml-pipelines"
ARGO_VERSION="v4.1.6"
ARGO_MANIFEST_SHA256="acf23714e633a9c9808e9e72550e1ce13e8cb4ea20398a4d9b540022c6469164"
ARGO_MINIMAL_CRDS="
71b0cd5251bc7ae49e21887fb65530a90b7450bb7af041b9c17b92af8e32041d clusterworkflowtemplates
b6cb5e7bd172514be90812feb33ebc1942ea74e7b2472e1e8992f5ad48de4f8d cronworkflows
173dcdf008ef247077819633536c043f13839acfdf8a2672e2bcb9edd9f80136 workflowartifactgctasks
533f6cf376c8b2b3c4f29adb54dd1b2da0a593c646c6b511fedf28c707609746 workfloweventbindings
358d5f18eefbef7fca670b13ab0948cfd35deee6a1d0ed62260f5abf810bc3bb workflows
69490cddb4dcc71a6d0464e0db39b9e0d71315859d63d2bccbeeec4e947e88cc workflowtaskresults
64cb326512436752f68b185031cdf10f52d609d1b0ed06fb6c33d7baf5c48524 workflowtasksets
8bbb80d961a262760e229af37fba7895a1e95289449981e28467738c0f06ba26 workflowtemplates
"
IMAGE="python:3.12-alpine"
CLUSTER=""
NS_CREATED=""
CLUSTER_OBJECTS_CREATED=""
DOCKER_USED=""
STEP_PREFIX="cc-ml-pipelines-step-"

k() { kubectl --context "$CTX" "$@"; }

cleanup() {
  set +e
  if [[ -n "$CLUSTER" ]]; then
    if [[ -n "$NS_CREATED" ]]; then
      k -n "$NS" delete workflows --all --wait=true --timeout=60s >/dev/null 2>&1
      k -n "$NS" get workflows -o name 2>/dev/null | while read -r wf; do k -n "$NS" patch "$wf" --type=merge -p '{"metadata":{"finalizers":null}}' >/dev/null 2>&1; done
      k delete namespace "$NS" --ignore-not-found --wait=true --timeout=120s >/dev/null 2>&1; fi
    if [[ -n "$CLUSTER_OBJECTS_CREATED" ]]; then k delete crd,priorityclass -l "$LABEL_KEY=$LABEL_VALUE" --ignore-not-found --wait=true --timeout=120s >/dev/null 2>&1; fi
  fi
  if [[ -n "$DOCKER_USED" ]]; then
    docker ps -aq --filter "name=^$STEP_PREFIX" > "$WORK/leftover.txt" 2>/dev/null
    [[ -s "$WORK/leftover.txt" ]] && xargs docker rm -f < "$WORK/leftover.txt" >/dev/null 2>&1
  fi
  rm -rf "$WORK"
}
trap cleanup EXIT

PY="$(command -v python3)" || fail "needs python3"
"$PY" -c 'import sys; assert sys.version_info >= (3, 9), sys.version' || fail "needs python3 >= 3.9"
sha256_of() { if command -v sha256sum >/dev/null; then sha256sum "$1" | awk '{print $1}'; else shasum -a 256 "$1" | awk '{print $1}'; fi; }
minipipe() { "$PY" code/main.py "$@"; }

run_pipeline() {
  local name="$1"; shift
  local code=0
  minipipe run code/pipeline.py --json "$@" > "$WORK/$name.json" 2> "$WORK/$name.err" || code=$?
  [[ "$code" == "0" ]] || fail "minipipe run $* exited $code: $(cat "$WORK/$name.err") $(head -c 2000 "$WORK/$name.json")"
}

statuses() {
  "$PY" - "$1" <<'PY'
import json, sys
report = json.load(open(sys.argv[1]))
print(" ".join(f"{t['path']}={t['status']}" for t in sorted(report["tasks"], key=lambda t: t["path"])))
PY
}

kube_ready() {
  command -v kubectl >/dev/null || { echo "  (kubectl not installed; the Argo Workflows steps are skipped)"; return 1; }
  kubectl config get-contexts -o name > "$WORK/contexts.txt" 2>/dev/null || true
  grep -qx "$CTX" "$WORK/contexts.txt" || { echo "  (context $CTX not found; the Argo Workflows steps are skipped)"; return 1; }
  k cluster-info > "$WORK/cluster-info.txt" 2>&1 || fail "context $CTX is not reachable: $(cat "$WORK/cluster-info.txt")"
  return 0
}

echo "▸ minipipe compiles the pipeline to a KFP-shaped IR: 5 leaf tasks, a condition and a fan-out"
minipipe compile code/pipeline.py -o "$WORK/mini.json" > "$WORK/compile.txt" 2>&1 || fail "compile: $(cat "$WORK/compile.txt")"
minipipe dag "$WORK/mini.json" --json > "$WORK/dag.json" || fail "dag"
minipipe dag "$WORK/mini.json" | sed 's/^/  /'
"$PY" - "$WORK/dag.json" <<'PY' || fail "dag shape: $(cat "$WORK/dag.json")"
import json, sys
rows = {r["task"]: r for r in json.load(open(sys.argv[1]))}
assert sorted(rows) == ["condition-1/register-model", "evaluate-model", "for-loop-3/score-slice", "make-dataset", "train-model"], sorted(rows)
assert rows["train-model"]["deps"] == ["make-dataset"] and rows["train-model"]["retries"] == 2, rows["train-model"]
assert rows["condition-1/register-model"]["deps"] == ["evaluate-model", "train-model"], rows["condition-1/register-model"]
assert "pipelinechannel--max_rmse" in rows["condition-1/register-model"]["condition"], rows["condition-1/register-model"]
PY

echo "▸ Use It: kfp 2.18.0 in a hash-pinned venv compiles the same file; the DAG must be identical"
if "$PY" -c 'import sys; sys.exit(0 if sys.version_info[:2] == (3, 12) else 1)'; then
  "$PY" -m venv "$WORK/kfp"
  "$WORK/kfp/bin/pip" install --quiet --disable-pip-version-check --retries 3 --require-hashes --only-binary=:all: -r code/requirements-build.txt > "$WORK/pip-build.txt" 2>&1 || fail "pip install setuptools: $(tail -20 "$WORK/pip-build.txt")"
  "$WORK/kfp/bin/pip" install --quiet --disable-pip-version-check --retries 3 --require-hashes --only-binary=:all: --no-binary=kfp-server-api --no-build-isolation -r code/requirements-kfp.txt > "$WORK/pip-kfp.txt" 2>&1 || fail "pip install kfp: $(tail -20 "$WORK/pip-kfp.txt")"
  "$WORK/kfp/bin/pip" check > "$WORK/pip-check.txt" 2>&1 || fail "pip check: $(cat "$WORK/pip-check.txt")"
  "$WORK/kfp/bin/python" -c 'import kfp; print("  kfp", kfp.__version__)'
  "$WORK/kfp/bin/python" code/kfp_compile.py code/pipeline.py "$WORK/kfp.json" > "$WORK/kfp-compile.txt" 2>&1 || fail "kfp compile: $(cat "$WORK/kfp-compile.txt")"
  "$WORK/kfp/bin/python" code/kfp_compile.py code/fixtures/features_pipeline.py "$WORK/kfp-features.json" --pipeline features > "$WORK/kfp-features.txt" 2>&1 || fail "kfp compile features: $(cat "$WORK/kfp-features.txt")"
  minipipe compile code/fixtures/features_pipeline.py -o "$WORK/mini-features.json" > /dev/null || fail "minipipe compile features"
  "$PY" - "$WORK" code/fixtures/kfp-2.18.0 <<'PY' || fail "minipipe and kfp disagree (see above)"
import json, sys
work, fixtures = sys.argv[1:3]


def diff(a, b, path):
    if isinstance(a, dict) and isinstance(b, dict):
        out = []
        for key in sorted(set(a) | set(b)):
            if key not in a or key not in b:
                out.append(f"{path}.{key}: only in {'kfp' if key in b else 'minipipe'}")
            else:
                out += diff(a[key], b[key], f"{path}.{key}")
        return out
    if isinstance(a, list) and isinstance(b, list) and len(a) == len(b):
        return [d for i, (x, y) in enumerate(zip(a, b)) for d in diff(x, y, f"{path}[{i}]")]
    return [] if a == b else [f"{path}: minipipe {json.dumps(a)[:100]} / kfp {json.dumps(b)[:100]}"]


def shape(ir):
    executors = {k: {"image": v["container"]["image"], "args": v["container"]["args"]} for k, v in ir["deploymentSpec"]["executors"].items()}
    return {"components": ir["components"], "root": ir["root"], "pipelineInfo": ir["pipelineInfo"], "executors": executors}


problems = []
for mini, kfp, fixture in (("mini.json", "kfp.json", "house-prices.json"), ("mini-features.json", "kfp-features.json", "features.json")):
    live = shape(json.load(open(f"{work}/{kfp}")))
    problems += diff(shape(json.load(open(f"{work}/{mini}"))), live, mini)
    problems += diff(shape(json.load(open(f"{fixtures}/{fixture}"))), live, f"fixture {fixture}")
    tasks = live["root"]["dag"]["tasks"]
    print(f"  {kfp}: {len(live['components'])} components, root tasks {sorted(tasks)}: identical to {mini}")
if problems:
    print("\n".join(problems))
    sys.exit(1)
PY
else
  echo "  (python3 is $("$PY" -c 'import sys; print("%d.%d" % sys.version_info[:2])'), the pinned wheels are cp312; kfp comparison skipped)"
fi

echo "▸ minipipe run, first time: every task executes in a subprocess, the condition is true, the loop fans out to 3"
STORE="$WORK/store"
run_pipeline run1 --store "$STORE"
S1="$(statuses "$WORK/run1.json")"
echo "  $S1"
[[ "$S1" == "condition-1/register-model=executed evaluate-model=executed for-loop-3[0]/score-slice=executed for-loop-3[1]/score-slice=executed for-loop-3[2]/score-slice=executed make-dataset=executed train-model=executed" ]] || fail "first run: $S1"
"$PY" - "$WORK/run1.json" "$WORK/dag.json" <<'PY' || fail "first run order or outputs: $(cat "$WORK/run1.json" | head -c 3000)"
import json, sys
report, dag = (json.load(open(p)) for p in sys.argv[1:3])
deps = {r["task"].split("/")[-1]: [d.split("/")[-1] for d in r["deps"]] for r in dag}
runs = report["tasks"]
for t in runs:
    for d in deps[t["task"]]:
        for u in (u for u in runs if u["task"] == d):
            assert u["finished"] <= t["started"], (t["path"], "started before", u["path"], "finished")
evaluate = next(t for t in runs if t["task"] == "evaluate-model")["parameters"]["Output"]
assert 0.2 < evaluate < 0.5, evaluate
print(f"  every task started after its dependencies finished; test RMSE {evaluate}")
PY

echo "▸ minipipe run, second time: all 7 tasks are cache hits with identical outputs"
run_pipeline run2 --store "$STORE"
S2="$(statuses "$WORK/run2.json")"
[[ "$S2" == "${S1//=executed/=cached}" ]] || fail "second run should be all cache hits: $S2"
"$PY" - "$WORK/run1.json" "$WORK/run2.json" <<'PY' || fail "cached outputs differ"
import json, sys
a, b = ({t["path"]: (t["cache_key"], t["parameters"], t["artifacts"]) for t in json.load(open(p))["tasks"]} for p in sys.argv[1:3])
assert a == b, {k: (a[k], b.get(k)) for k in a if a[k] != b.get(k)}
print(f"  {len(a)} tasks: same cache keys, parameters and artifact digests")
PY

echo "▸ a new learning rate: make-dataset stays cached, train-model and everything downstream rerun"
run_pipeline run3 --store "$STORE" --param lr=0.4
S3="$(statuses "$WORK/run3.json")"
echo "  $S3"
[[ "$S3" == *"make-dataset=cached"* && "$S3" == *"train-model=executed"* && "$S3" == *"evaluate-model=executed"* && "$S3" == *"for-loop-3[2]/score-slice=executed"* ]] || fail "lr change: $S3"
minipipe explain train-model --store "$STORE" > "$WORK/explain.txt" || fail "explain"
sed 's/^/  /' "$WORK/explain.txt"
grep -q 'parameters.lr: 0.5 -> 0.4' "$WORK/explain.txt" || fail "explain should name parameters.lr: $(cat "$WORK/explain.txt")"
minipipe explain evaluate-model --store "$STORE" > "$WORK/explain-eval.txt" || fail "explain evaluate-model"
grep -q 'artifacts.model: ' "$WORK/explain-eval.txt" || fail "evaluate-model reran because its input artifact changed: $(cat "$WORK/explain-eval.txt")"

echo "▸ a stricter gate: the condition is false and register-model is skipped; the rest is cached"
run_pipeline run4 --store "$STORE" --param max_rmse=0.1
S4="$(statuses "$WORK/run4.json")"
[[ "$S4" == "condition-1/register-model=skipped evaluate-model=cached for-loop-3[0]/score-slice=cached for-loop-3[1]/score-slice=cached for-loop-3[2]/score-slice=cached make-dataset=cached train-model=cached" ]] || fail "stricter gate: $S4"
echo "  $S4"

echo "▸ lineage is optional: with no tracking server listening the run still succeeds and says so"
PORT="$("$PY" -c 'import socket; s=socket.socket(); s.bind(("127.0.0.1", 0)); print(s.getsockname()[1]); s.close()')"
run_pipeline run5 --store "$STORE" --tracking-uri "http://127.0.0.1:$PORT"
grep -q 'continuing without lineage' "$WORK/run5.err" || fail "expected the lineage message: $(cat "$WORK/run5.err")"
grep -m1 'lineage' "$WORK/run5.err" | sed 's/^/  /'

if command -v docker >/dev/null && docker info >/dev/null 2>&1; then
  echo "▸ the same pipeline with --runner docker in $IMAGE: the same digests from a musl container"
  docker pull -q "$IMAGE" > "$WORK/pull.txt" 2>&1 || fail "docker pull $IMAGE: $(cat "$WORK/pull.txt")"
  DOCKER_USED=1
  run_pipeline docker --store "$WORK/store-docker" --runner docker --container-prefix "$STEP_PREFIX"
  "$PY" - "$WORK/run1.json" "$WORK/docker.json" <<'PY' || fail "docker runner outputs differ from the subprocess run"
import json, sys
a, b = ({t["path"]: (t["status"], t["parameters"], {k: v["sha256"] for k, v in t["artifacts"].items()}) for t in json.load(open(p))["tasks"]} for p in sys.argv[1:3])
assert a == b, {k: (a[k], b.get(k)) for k in a if a[k] != b.get(k)}
print(f"  {len(b)} tasks executed in containers; parameters and artifact digests equal the subprocess run")
PY
else
  echo "  (docker not available; the container runner step is skipped)"
fi

if ! kube_ready; then
  echo "SMOKE OK: mlops/ml-pipelines (local only)"
  exit 0
fi
CLUSTER=1

echo "▸ Argo Workflows $ARGO_VERSION: namespace-install.yaml and the repository's minimal CRDs, each checked against a sha256 pinned here (the release checksums cover only the CLI)"
curl -fsSL --retry 3 -o "$WORK/namespace-install.yaml" "https://github.com/argoproj/argo-workflows/releases/download/$ARGO_VERSION/namespace-install.yaml" || fail "download namespace-install.yaml"
GOT="$(sha256_of "$WORK/namespace-install.yaml")"
[[ "$GOT" == "$ARGO_MANIFEST_SHA256" ]] || fail "namespace-install.yaml checksum mismatch: want $ARGO_MANIFEST_SHA256 got $GOT"
mkdir -p "$WORK/crds"
while read -r want crd; do
  [[ -n "$crd" ]] || continue
  file="$WORK/crds/$crd.yaml"
  curl -fsSL --retry 3 -o "$file" "https://raw.githubusercontent.com/argoproj/argo-workflows/$ARGO_VERSION/manifests/base/crds/minimal/argoproj.io_$crd.yaml" || fail "download the minimal CRD $crd"
  GOT="$(sha256_of "$file")"
  [[ "$GOT" == "$want" ]] || fail "minimal CRD $crd checksum mismatch: want $want got $GOT"
done <<< "$ARGO_MINIMAL_CRDS"
"$PY" - "$WORK/namespace-install.yaml" "$WORK" <<'PY' || fail "split the Argo manifest"
import glob, re, sys
text, work = open(sys.argv[1]).read(), sys.argv[2]
cluster = [open(path).read().strip() for path in sorted(glob.glob(f"{work}/crds/*.yaml"))]
namespaced = []
keep = {("ServiceAccount", "argo"), ("Role", "argo-role"), ("RoleBinding", "argo-binding"), ("ConfigMap", "workflow-controller-configmap"), ("Deployment", "workflow-controller")}
for doc in re.split(r"(?m)^---\s*$", text):
    kind, name = re.search(r"(?m)^kind: (\S+)", doc), re.search(r"(?m)^  name: (\S+)", doc)
    if not kind or not name:
        continue
    key = (kind.group(1), name.group(1))
    if key[0] == "PriorityClass":
        cluster.append(doc.strip())
    elif key in keep:
        namespaced.append(doc.strip())
open(f"{work}/argo-cluster.yaml", "w").write("\n---\n".join(cluster) + "\n")
open(f"{work}/argo-namespaced.yaml", "w").write("\n---\n".join(namespaced) + "\n")
print(f"  {len(cluster)} cluster-scoped objects (8 minimal CRDs without schemas, the PriorityClass), {len(namespaced)} namespaced (the controller, no argo-server)")
PY

k delete namespace "$NS" --ignore-not-found --wait=true --timeout=120s >/dev/null 2>&1 || true
NS_CREATED=1
k create namespace "$NS" >/dev/null || fail "create namespace $NS"
if k get crd workflows.argoproj.io >/dev/null 2>&1 && [[ "$(k get crd workflows.argoproj.io -o jsonpath="{.metadata.labels.$LABEL_KEY}")" != "$LABEL_VALUE" ]]; then
  echo "  (Argo Workflows CRDs already installed by someone else; using and keeping them)"
else
  CLUSTER_OBJECTS_CREATED=1
  k apply --server-side --force-conflicts -f "$WORK/argo-cluster.yaml" > "$WORK/apply-cluster.txt" 2>&1 || fail "apply CRDs: $(tail -20 "$WORK/apply-cluster.txt")"
  k label --overwrite -f "$WORK/argo-cluster.yaml" "$LABEL_KEY=$LABEL_VALUE" > /dev/null || fail "label the cluster-scoped Argo objects"
  for crd in workflows workflowtaskresults workflowtemplates workflowtasksets; do
    deadline=$((SECONDS + 60))
    until [[ "$(k get crd "$crd.argoproj.io" -o jsonpath='{.status.conditions[?(@.type=="Established")].status}' 2>/dev/null)" == "True" ]]; do
      (( SECONDS < deadline )) || fail "CRD $crd.argoproj.io not Established"
      sleep 1
    done
  done
fi
k -n "$NS" apply --server-side -f "$WORK/argo-namespaced.yaml" > "$WORK/apply-ns.txt" 2>&1 || fail "apply the controller: $(cat "$WORK/apply-ns.txt")"
cat > "$WORK/executor-rbac.json" <<'EOF'
{"apiVersion": "v1", "kind": "List", "items": [
  {"apiVersion": "v1", "kind": "ServiceAccount", "metadata": {"name": "minipipe"}},
  {"apiVersion": "rbac.authorization.k8s.io/v1", "kind": "Role", "metadata": {"name": "minipipe-executor"},
   "rules": [{"apiGroups": ["argoproj.io"], "resources": ["workflowtaskresults"], "verbs": ["create", "patch"]}]},
  {"apiVersion": "rbac.authorization.k8s.io/v1", "kind": "RoleBinding", "metadata": {"name": "minipipe-executor"},
   "roleRef": {"apiGroup": "rbac.authorization.k8s.io", "kind": "Role", "name": "minipipe-executor"},
   "subjects": [{"kind": "ServiceAccount", "name": "minipipe"}]}
]}
EOF
k -n "$NS" apply -f "$WORK/executor-rbac.json" > "$WORK/apply-rbac.txt" 2>&1 || fail "executor RBAC: $(cat "$WORK/apply-rbac.txt")"
k -n "$NS" rollout status deployment/workflow-controller --timeout=180s > /dev/null || fail "workflow-controller not ready: $(k -n "$NS" get pods -o wide 2>&1)"
echo "  workflow-controller ready in $NS (--namespaced)"

echo "▸ minipipe emits a Workflow from the same IR; Argo runs it with the store on a PersistentVolumeClaim"
minipipe argo "$WORK/mini.json" --service-account minipipe -o "$WORK/workflow.json" | sed 's/^/  /'
WF="$(k -n "$NS" create -f "$WORK/workflow.json" -o jsonpath='{.metadata.name}' 2> "$WORK/create.err")" || fail "create Workflow: $(cat "$WORK/create.err")"
echo "  submitted $WF"
deadline=$((SECONDS + 300))
PHASE=""
while (( SECONDS < deadline )); do
  PHASE="$(k -n "$NS" get workflow "$WF" -o jsonpath='{.status.phase}' 2>/dev/null || true)"
  [[ "$PHASE" == "Succeeded" || "$PHASE" == "Failed" || "$PHASE" == "Error" ]] && break
  sleep 3
done
k -n "$NS" get workflow "$WF" -o json > "$WORK/workflow-status.json"
if [[ "$PHASE" != "Succeeded" ]]; then
  k -n "$NS" get pods -o wide > "$WORK/pods.txt" 2>&1 || true
  "$PY" - "$WORK/workflow-status.json" <<'PY' || true
import json, sys
status = json.load(open(sys.argv[1])).get("status", {})
print("workflow:", status.get("phase"), status.get("message"))
for node in status.get("nodes", {}).values():
    print(" ", node.get("displayName"), node.get("type"), node.get("phase"), node.get("message", ""))
PY
  fail "Workflow $WF ended as '${PHASE:-unknown}' (deadline 300s): $(cat "$WORK/pods.txt")"
fi

echo "▸ Argo ran the same 7 steps in dependency order with the same outputs as the local run"
"$PY" - "$WORK/workflow-status.json" "$WORK/run1.json" "$WORK/dag.json" <<'PY' || fail "Argo and minipipe disagree"
import json, re, sys
from datetime import datetime

workflow, report, dag = (json.load(open(p)) for p in sys.argv[1:4])
deps = {r["task"].split("/")[-1]: [d.split("/")[-1] for d in r["deps"]] for r in dag}


def when(stamp):
    return datetime.fromisoformat(stamp.replace("Z", "+00:00"))


pods = [n for n in workflow["status"]["nodes"].values() if n.get("type") == "Pod" and n.get("phase") == "Succeeded"]
argo = {}
for node in pods:
    task = re.sub(r"-retry-\d+$", "", node["templateName"])[len("exec-"):]
    values = {p["name"]: p.get("value") for p in node.get("outputs", {}).get("parameters", [])}
    argo.setdefault(task, []).append((node, values))
local = {}
for t in report["tasks"]:
    values = {k: (repr(v) if isinstance(v, float) else str(v)) for k, v in t["parameters"].items()}
    values.update({f"{k}-sha256": v["sha256"] for k, v in t["artifacts"].items()})
    local.setdefault(t["task"], []).append(values)
assert sorted(argo) == sorted(local), (sorted(argo), sorted(local))
for task in sorted(local):
    mine = sorted(json.dumps(v, sort_keys=True) for v in local[task])
    theirs = sorted(json.dumps({k: v for k, v in values.items() if not k.endswith("-uri")}, sort_keys=True) for _, values in argo[task])
    assert mine == theirs, (task, mine, theirs)
    for node, _ in argo[task]:
        for dep in deps[task]:
            for other, _ in argo[dep]:
                assert when(other["finishedAt"]) <= when(node["startedAt"]), (task, node["startedAt"], dep, other["finishedAt"])
    print(f"  {task:<15} x{len(argo[task])}  outputs equal, started after {', '.join(deps[task]) or 'nothing'}")
skipped = [n["displayName"] for n in workflow["status"]["nodes"].values() if n.get("type") == "Skipped"]
assert not skipped, skipped
PY

echo "SMOKE OK: mlops/ml-pipelines"
