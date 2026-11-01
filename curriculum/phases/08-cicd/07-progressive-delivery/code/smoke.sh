#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."

fail() { echo "SMOKE FAIL: $*" >&2; exit 1; }
WORK="$(mktemp -d)"
NS="cc-progressive-delivery"
CTX="${CC_SMOKE_CONTEXT:-kind-cc-smoke}"
ROLLOUTS_VERSION="v1.10.1"
ROLLOUTS_INSTALL_SHA256=149148429a6f8258b789003ffb9bc6698a526d611ecb1b185624217cf51bae7f
ROLLOUTS_NS="argo-rollouts"
F="code/fixtures"
PIDS=()
CLUSTER=""
NS_CREATED=""
ROLLOUTS_INSTALLED=""
ROLLOUTS_NS_CREATED=""
mkdir -p "$WORK/bin" "$WORK/dl"
export PATH="$WORK/bin:$PATH"

k() { kubectl --context "$CTX" "$@"; }

cleanup() {
  set +e
  for pid in ${PIDS[@]+"${PIDS[@]}"}; do { kill "$pid"; wait "$pid"; } 2>/dev/null; done
  if [[ -n "$CLUSTER" ]]; then
    if [[ -n "$NS_CREATED" ]]; then k delete namespace "$NS" --ignore-not-found --wait=true --timeout=120s >/dev/null 2>&1; fi
    if [[ -n "$ROLLOUTS_INSTALLED" ]]; then k delete -n "$ROLLOUTS_NS" -f "$WORK/install.yaml" --ignore-not-found --wait=true --timeout=120s >/dev/null 2>&1; fi
    if [[ -n "$ROLLOUTS_NS_CREATED" ]]; then k delete namespace "$ROLLOUTS_NS" --ignore-not-found --wait=true --timeout=120s >/dev/null 2>&1; fi
  fi
  rm -rf "$WORK"
}
trap cleanup EXIT

sha256_of() { if command -v sha256sum >/dev/null; then sha256sum "$1" | awk '{print $1}'; else shasum -a 256 "$1" | awk '{print $1}'; fi; }
wait_line() { for _ in $(seq 1 150); do grep -q "$2" "$1" 2>/dev/null && return 0; sleep 0.1; done; fail "no '$2' in $1: $(cat "$1" 2>/dev/null)"; }
url_of() { sed -n 's/.* listening on //p' "$1"; }
free_port() { python3 -c 'import socket; s=socket.socket(); s.bind(("127.0.0.1", 0)); print(s.getsockname()[1]); s.close()'; }
start() { local log="$WORK/$1.log"; shift; python3 code/main.py "$@" > "$log" 2>&1 & PIDS+=("$!"); }
check() { python3 -c 'import sys; v = float(sys.argv[1]); sys.exit(0 if eval(sys.argv[2]) else 1)' "$1" "$2"; }

field() {
  python3 - "$@" <<'PY'
import json, sys
value = json.load(open(sys.argv[1]))
for key in sys.argv[2:]:
    value = value[int(key)] if isinstance(value, list) else value[key]
print(json.dumps(value) if isinstance(value, (dict, list)) or value is None else value)
PY
}

promql() {
  python3 code/main.py query "$1" "$2" --json > "$WORK/q.json" 2> "$WORK/q.err" || fail "query $2 on $1: $(cat "$WORK/q.err")"
  python3 - "$WORK/q.json" <<'PY'
import json, sys
data = json.load(open(sys.argv[1]))
result = data.get("result") or []
if data.get("resultType") == "scalar":
    print(result[1])
elif result:
    print(result[0]["value"][1])
else:
    print("none")
PY
}

measured() {
  python3 - "$1" "$2" <<'PY'
import json, sys
status, metric = json.load(open(sys.argv[1])), sys.argv[2]
values = [m["value"][0] for run in status["runs"] for r in run["metrics"] if r["name"] == metric for m in r["measurements"] if m["value"]]
print(values[-1] if values else "none")
PY
}

python3 - "$F" "$WORK" <<'PY'
import json, sys
fixtures, work = sys.argv[1:3]
rollout = json.load(open(f"{fixtures}/rollout.json"))
analysis = json.load(open(f"{fixtures}/analysis.json"))


def write(path, doc):
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(doc, handle, indent=2)


for version, error, latency in (("v1", "0", "0"), ("v2", "0", "0"), ("v3", "0.5", "0"), ("v4", "0", "400"), ("v5", "0", "0"), ("v6", "0.5", "0")):
    doc = json.loads(json.dumps(rollout))
    values = {"APP_VERSION": version, "ERROR_RATE": error, "LATENCY_MS": latency}
    for item in doc["spec"]["template"]["spec"]["containers"][0]["env"]:
        item["value"] = values[item["name"]]
    for step in doc["spec"]["strategy"]["canary"]["steps"]:
        if "analysis" in step:
            step["analysis"]["args"] = [{"name": "version", "value": version}]
    write(f"{work}/rollout-{version}.json", doc)
fast = json.loads(json.dumps(analysis))
for metric in fast["spec"]["metrics"]:
    metric["interval"], metric["initialDelay"] = "1s", "2s"
    metric["provider"]["prometheus"]["query"] = metric["provider"]["prometheus"]["query"].replace("[20s]", "[5s]")
write(f"{work}/analysis-fast.json", fast)
PY

echo "▸ minirollout plan: steps, whole-pod counts and the resolved PromQL"
python3 code/main.py plan "$F/rollout.json" --analysis "$F/analysis.json" > "$WORK/plan.txt" 2>&1 || fail "plan: $(cat "$WORK/plan.txt")"
cat "$WORK/plan.txt"
grep -Eq '^1 +setWeight: 25 +1 +3 +25% +25%$' "$WORK/plan.txt" || fail "plan row 1 should be 1 new and 3 stable pods: $(cat "$WORK/plan.txt")"
python3 code/main.py plan "$F/rollout.json" --analysis "$F/analysis.json" --replicas 3 --json > "$WORK/plan3.json"
[[ "$(field "$WORK/plan3.json" steps 0 new_pods)" == "1" && "$(field "$WORK/plan3.json" steps 0 stable_pods)" == "3" ]] || fail "3 replicas at 25% should surge to 1+3: $(cat "$WORK/plan3.json")"

echo "▸ minirollout simulate: a version that fails 30% of requests, three step plans"
python3 code/main.py simulate "$F/rollout.json" --analysis "$F/analysis.json" --error-rate 0.3 --plan 100 --plan "5%,analysis,25%,analysis,50%,analysis" --json > "$WORK/sim.json" || fail "simulate: $(cat "$WORK/sim.json")"
python3 - "$WORK/sim.json" <<'PY' || fail "simulate results: $(cat "$WORK/sim.json")"
import json, sys
fixture, rolling, careful = json.load(open(sys.argv[1]))
assert (fixture["outcome"], rolling["outcome"], careful["outcome"]) == ("aborted", "promoted", "aborted"), (fixture["outcome"], rolling["outcome"], careful["outcome"])
assert careful["new_requests"] < fixture["new_requests"] < rolling["new_requests"], (careful["new_requests"], fixture["new_requests"], rolling["new_requests"])
for row in (fixture, rolling, careful):
    print(f"  {row['plan']:<40} {row['outcome']:<9} requests that reached the bad version: {row['new_requests']}")
PY

echo "▸ local data plane: apps v1 to v4 (v3 fails half its requests, v4 answers in 400ms), the weighted proxy, a load generator"
start v1 app --version v1 --port 0
start v2 app --version v2 --port 0
start v3 app --version v3 --port 0 --error-rate 0.5
start v4 app --version v4 --port 0 --latency-ms 400
for v in v1 v2 v3 v4; do wait_line "$WORK/$v.log" "listening on"; done
V1="$(url_of "$WORK/v1.log")"
V2="$(url_of "$WORK/v2.log")"
V3="$(url_of "$WORK/v3.log")"
V4="$(url_of "$WORK/v4.log")"
start proxy proxy --stable "$V1" --listen 127.0.0.1:0 --scrape "$V3/metrics"
wait_line "$WORK/proxy.log" "listening on"
PROXY="$(url_of "$WORK/proxy.log")"
start load load --url "$PROXY/" --rps 40 --concurrency 8
wait_line "$WORK/load.log" "req/s"
curl -fsS -D "$WORK/headers.txt" -o "$WORK/body.json" "$PROXY/" || fail "the proxy does not answer"
grep -qi '^x-version: v1' "$WORK/headers.txt" && grep -qi '^x-upstream: stable' "$WORK/headers.txt" || fail "proxy response headers: $(cat "$WORK/headers.txt")"

local_run() {
  local version="$1" canary="$2" code=0
  python3 code/main.py run "$WORK/rollout-$version.json" --analysis "$WORK/analysis-fast.json" --target proxy --proxy "$PROXY" --canary-url "$canary" \
    --state "$WORK/local-state.json" --prometheus-url "$PROXY" --tick 0.5 --timeout 90 > "$WORK/local-$version.txt" 2>&1 || code=$?
  grep -E "measurement|AnalysisRunFailed|RolloutAborted|RolloutCompleted|^rollout " "$WORK/local-$version.txt" | sed 's/^/    /' || true
  return "$code"
}

echo "▸ minirollout run --target proxy: v1 is the first version and skips the steps"
local_run v1 "$V1" || fail "v1 should be Healthy at once: $(cat "$WORK/local-v1.txt")"

echo "▸ v2 is healthy: 25%, analysis, 50%, pause 2s, analysis, then promoted"
local_run v2 "$V2" || fail "v2 should be promoted: $(cat "$WORK/local-v2.txt")"
curl -fsS "$PROXY/admin/config" > "$WORK/config.json"
[[ "$(field "$WORK/config.json" stable)" == "$V2" && "$(field "$WORK/config.json" weight)" == "0" ]] || fail "after promotion the proxy's stable should be v2: $(cat "$WORK/config.json")"

echo "▸ v3 fails half its requests: aborted on the first measurement, all traffic back on v2"
CODE=0
local_run v3 "$V3" || CODE=$?
[[ "$CODE" == "1" ]] || fail "v3 should be aborted with exit 1, got $CODE: $(cat "$WORK/local-v3.txt")"
grep -q 'Metric "success-rate" assessed Failed due to failed (1) > failureLimit (0)' "$WORK/local-v3.txt" || fail "abort message: $(cat "$WORK/local-v3.txt")"
RATE="$(measured "$WORK/local-state.json" success-rate)"
check "$RATE" "0.3 <= v <= 0.7" || fail "measured success rate of v3 should be about 0.5, got $RATE"
curl -fsS "$PROXY/admin/config" > "$WORK/config.json"
[[ "$(field "$WORK/config.json" stable)" == "$V2" && "$(field "$WORK/config.json" weight)" == "0" ]] || fail "after the abort the proxy should send nothing to v3: $(cat "$WORK/config.json")"
echo "  measured success rate of v3: $RATE"

echo "▸ the proxy's view of v3 and v3's own /metrics count the same failed requests"
sleep 2
SEEN="$(promql "$PROXY" 'sum(http_requests_total{instance="",version="v3",code="500"})')"
SELF="$(promql "$PROXY" 'sum(http_requests_total{instance!="",code="500"})')"
[[ "$SEEN" != "none" && "$SEEN" == "$SELF" ]] || fail "proxy counted $SEEN failed v3 requests, v3 reported $SELF"
echo "  proxy: $SEEN, v3 /metrics: $SELF"

echo "▸ v4 answers in 400ms: aborted by the p95 latency metric"
CODE=0
local_run v4 "$V4" || CODE=$?
[[ "$CODE" == "1" ]] && grep -q 'Metric "p95-latency" assessed Failed' "$WORK/local-v4.txt" || fail "v4 should fail on p95-latency (exit $CODE): $(cat "$WORK/local-v4.txt")"
P95="$(measured "$WORK/local-state.json" p95-latency)"
check "$P95" "v >= 0.25" || fail "p95 of v4 should be above 250ms, got $P95"
ERRORS="$(promql "$PROXY" 'sum(rate(http_requests_total{instance="",version="v2",code!~"2.."}[5s])) or vector(0)')"
SERVED="$(promql "$PROXY" 'sum(rate(http_requests_total{instance="",version="v2"}[5s]))')"
check "$ERRORS" "v == 0" && check "$SERVED" "v > 10" || fail "stable v2 should keep serving without errors: errors $ERRORS/s, served $SERVED/s"
echo "  v4 p95 $P95 s; stable v2 serves $SERVED req/s with $ERRORS errors/s"

if ! command -v kubectl >/dev/null; then
  echo "  (kubectl not installed; the kind and Argo Rollouts steps are skipped)"
  echo "SMOKE OK: cicd/progressive-delivery (local only)"
  exit 0
fi
kubectl config get-contexts -o name > "$WORK/contexts.txt" 2>/dev/null || true
if ! grep -qx "$CTX" "$WORK/contexts.txt"; then
  echo "  (context $CTX not found; the kind and Argo Rollouts steps are skipped)"
  echo "SMOKE OK: cicd/progressive-delivery (local only)"
  exit 0
fi
k cluster-info > "$WORK/cluster-info.txt" 2>&1 || fail "context $CTX is not reachable: $(cat "$WORK/cluster-info.txt")"
CLUSTER=1

echo "▸ namespace $NS: the demo Service, the app code in a ConfigMap, an in-cluster load generator that serves /api/v1/query"
k delete namespace "$NS" --ignore-not-found --wait=true --timeout=120s >/dev/null 2>&1 || true
NS_CREATED=1
k create namespace "$NS" >/dev/null || fail "create namespace $NS"
k -n "$NS" create configmap minirollout-code --from-file=main.py=code/main.py >/dev/null || fail "create the code ConfigMap"
cat > "$WORK/base.json" <<EOF
{"apiVersion": "v1", "kind": "List", "items": [
  {"apiVersion": "v1", "kind": "Service", "metadata": {"name": "demo"}, "spec": {"selector": {"app": "demo"}, "ports": [{"port": 8080, "targetPort": 8080}]}},
  {"apiVersion": "v1", "kind": "Service", "metadata": {"name": "loadgen"}, "spec": {"selector": {"app": "loadgen"}, "ports": [{"port": 9090, "targetPort": 9090}]}},
  {"apiVersion": "apps/v1", "kind": "Deployment", "metadata": {"name": "loadgen"}, "spec": {"replicas": 1, "selector": {"matchLabels": {"app": "loadgen"}},
    "template": {"metadata": {"labels": {"app": "loadgen"}}, "spec": {"containers": [{"name": "load", "image": "python:3.12-alpine",
      "command": ["python3", "/app/main.py", "load", "--url", "http://demo.$NS.svc.cluster.local:8080/", "--rps", "40", "--concurrency", "8", "--listen", "0.0.0.0:9090"],
      "ports": [{"containerPort": 9090}], "readinessProbe": {"httpGet": {"path": "/-/ready", "port": 9090}, "periodSeconds": 2},
      "resources": {"requests": {"cpu": "20m", "memory": "32Mi"}}, "volumeMounts": [{"name": "code", "mountPath": "/app"}]}],
      "volumes": [{"name": "code", "configMap": {"name": "minirollout-code"}}]}}}}
]}
EOF
k -n "$NS" apply -f "$WORK/base.json" > "$WORK/base.txt" 2>&1 || fail "apply base objects: $(cat "$WORK/base.txt")"
k -n "$NS" rollout status deployment/loadgen --timeout=180s >/dev/null || fail "loadgen not ready: $(k -n "$NS" get pods -o wide 2>&1)"
PF_PORT="$(free_port)"
k -n "$NS" port-forward svc/loadgen "$PF_PORT:9090" --address 127.0.0.1 > "$WORK/port-forward.log" 2>&1 &
PIDS+=("$!")
wait_line "$WORK/port-forward.log" "Forwarding from"
PROM="http://127.0.0.1:$PF_PORT"

kube_run() {
  local version="$1" code=0
  python3 code/main.py run "$WORK/rollout-$version.json" --analysis "$F/analysis.json" --target kube --namespace "$NS" --context "$CTX" \
    --state "$WORK/kube-state.json" --prometheus-url "$PROM" --tick 1 --timeout 240 > "$WORK/kube-$version.txt" 2>&1 || code=$?
  grep -E "TrafficWeightUpdated|measurement|AnalysisRun(Successful|Failed)|RolloutAborted|RolloutCompleted|^rollout " "$WORK/kube-$version.txt" | sed 's/^/    /' || true
  return "$code"
}
deployment_state() {
  k -n "$NS" get deployment demo-stable demo-canary -o json > "$WORK/deploy.json"
  python3 - "$WORK/deploy.json" <<'PY'
import json, sys
for item in json.load(open(sys.argv[1]))["items"]:
    env = {e["name"]: e.get("value") for e in item["spec"]["template"]["spec"]["containers"][0].get("env", [])}
    print(item["metadata"]["name"], item["spec"]["replicas"], item["status"].get("readyReplicas", 0), env.get("APP_VERSION"))
PY
}

echo "▸ minirollout run --target kube: v1 creates demo-stable (4 pods) and an empty demo-canary behind one Service"
kube_run v1 || fail "v1 should become Healthy: $(cat "$WORK/kube-v1.txt")"
deployment_state > "$WORK/state-v1.txt"
grep -qx "demo-stable 4 4 v1" "$WORK/state-v1.txt" && grep -qx "demo-canary 0 0 v1" "$WORK/state-v1.txt" || fail "after v1: $(cat "$WORK/state-v1.txt")"

echo "▸ v2 is healthy: 1 of 4 pods, analysis, 2 of 4, pause, analysis, then demo-stable is rolled to v2"
kube_run v2 || fail "v2 should be promoted: $(cat "$WORK/kube-v2.txt")"
deployment_state > "$WORK/state-v2.txt"
cat "$WORK/state-v2.txt"
grep -qx "demo-stable 4 4 v2" "$WORK/state-v2.txt" && grep -qx "demo-canary 0 0 v2" "$WORK/state-v2.txt" || fail "after v2: $(cat "$WORK/state-v2.txt")"
RATE="$(measured "$WORK/kube-state.json" success-rate)"
check "$RATE" "v >= 0.95" || fail "v2 success rate should be at least 0.95, got $RATE"

echo "▸ v3 fails half its requests: minirollout aborts, demo-canary goes back to 0, demo-stable keeps v2 on 4 pods"
CODE=0
kube_run v3 || CODE=$?
[[ "$CODE" == "1" ]] || fail "v3 should be aborted (exit 1), got $CODE: $(cat "$WORK/kube-v3.txt")"
[[ "$(field "$WORK/kube-state.json" message)" == *'Metric "success-rate" assessed Failed due to failed (1) > failureLimit (0)'* ]] || fail "abort message: $(field "$WORK/kube-state.json" message)"
MINI="$(measured "$WORK/kube-state.json" success-rate)"
check "$MINI" "0.3 <= v <= 0.7" || fail "v3 success rate behind the Service should be about 0.5, got $MINI"
deployment_state > "$WORK/state-v3.txt"
grep -qx "demo-stable 4 4 v2" "$WORK/state-v3.txt" && grep -Eq "^demo-canary 0 0 v3$" "$WORK/state-v3.txt" || fail "after the abort: $(cat "$WORK/state-v3.txt")"
sleep 3
ERRORS="$(promql "$PROM" 'sum(rate(http_requests_total{version="v2",code!~"2.."}[10s])) or vector(0)')"
SERVED="$(promql "$PROM" 'sum(rate(http_requests_total{version="v2"}[10s]))')"
check "$ERRORS" "v == 0" && check "$SERVED" "v > 10" || fail "v2 should keep serving: errors $ERRORS/s, served $SERVED/s"
echo "  minirollout measured $MINI for v3; stable v2 serves $SERVED req/s with $ERRORS errors/s"
k -n "$NS" get deployments -L track > "$WORK/deployments.txt"
cat "$WORK/deployments.txt"

echo "▸ Argo Rollouts $ROLLOUTS_VERSION: install.yaml checked against its pinned sha256 (the release publishes none for it), the kubectl plugin against argo-rollouts-checksums.txt"
BASE="https://github.com/argoproj/argo-rollouts/releases/download/$ROLLOUTS_VERSION"
case "$(uname -m)" in x86_64|amd64) ARCH=amd64 ;; aarch64|arm64) ARCH=arm64 ;; *) fail "unsupported architecture $(uname -m)" ;; esac
PLUGIN="kubectl-argo-rollouts-linux-$ARCH"
curl -fsSL --retry 3 -o "$WORK/dl/checksums.txt" "$BASE/argo-rollouts-checksums.txt" || fail "download $BASE/argo-rollouts-checksums.txt"
curl -fsSL --retry 3 -o "$WORK/install.yaml" "$BASE/install.yaml" || fail "download $BASE/install.yaml"
curl -fsSL --retry 3 -o "$WORK/bin/kubectl-argo-rollouts" "$BASE/$PLUGIN" || fail "download $BASE/$PLUGIN"
GOT="$(sha256_of "$WORK/install.yaml")"
[[ "$GOT" == "$ROLLOUTS_INSTALL_SHA256" ]] || fail "install.yaml checksum mismatch: want '$ROLLOUTS_INSTALL_SHA256' got '$GOT'"
WANT="$(awk -v f="$PLUGIN" '$2 == f {print $1}' "$WORK/dl/checksums.txt")"
GOT="$(sha256_of "$WORK/bin/kubectl-argo-rollouts")"
[[ -n "$WANT" && "$WANT" == "$GOT" ]] || fail "$PLUGIN checksum mismatch: want '$WANT' got '$GOT'"
chmod +x "$WORK/bin/kubectl-argo-rollouts"
kubectl argo rollouts version > "$WORK/plugin-version.txt" 2>&1 || fail "kubectl argo rollouts version: $(cat "$WORK/plugin-version.txt")"
sed 's/^/  /' "$WORK/plugin-version.txt"
grep -q "$ROLLOUTS_VERSION" "$WORK/plugin-version.txt" || fail "the plugin is not $ROLLOUTS_VERSION: $(cat "$WORK/plugin-version.txt")"

if k get crd rollouts.argoproj.io >/dev/null 2>&1; then
  echo "  (Argo Rollouts CRDs already installed; using and keeping them)"
else
  k get namespace "$ROLLOUTS_NS" >/dev/null 2>&1 || ROLLOUTS_NS_CREATED=1
  [[ -z "$ROLLOUTS_NS_CREATED" ]] || k create namespace "$ROLLOUTS_NS" >/dev/null
  ROLLOUTS_INSTALLED=1
  k apply -n "$ROLLOUTS_NS" --server-side -f "$WORK/install.yaml" > "$WORK/install.txt" 2>&1 || fail "apply install.yaml: $(tail -20 "$WORK/install.txt")"
  k wait --for=condition=Established --timeout=60s crd/rollouts.argoproj.io crd/analysistemplates.argoproj.io crd/analysisruns.argoproj.io >/dev/null || fail "Argo Rollouts CRDs not established"
fi
k -n "$ROLLOUTS_NS" rollout status deployment/argo-rollouts --timeout=180s >/dev/null || fail "the Argo Rollouts controller is not ready: $(k -n "$ROLLOUTS_NS" get pods -o wide 2>&1)"

echo "▸ hand the demo Service to Argo Rollouts: remove minirollout's Deployments, apply the same AnalysisTemplate and Rollout"
k -n "$NS" delete deployment demo-stable demo-canary --wait=true --timeout=120s >/dev/null || fail "delete minirollout's Deployments"
k -n "$NS" wait --for=delete pod -l app=demo --timeout=120s >/dev/null 2>&1 || true
k -n "$NS" apply -f "$F/analysis.json" > "$WORK/apply-template.txt" 2>&1 || fail "apply AnalysisTemplate: $(cat "$WORK/apply-template.txt")"
k -n "$NS" apply -f "$WORK/rollout-v5.json" > "$WORK/apply-v5.txt" 2>&1 || fail "apply Rollout v5: $(cat "$WORK/apply-v5.txt")"
k -n "$NS" wait --for=jsonpath='{.status.phase}'=Healthy rollout/demo --timeout=180s >/dev/null || fail "Rollout v5 never became Healthy: $(k -n "$NS" get rollout demo -o yaml 2>&1 | tail -40)"
echo "  v5 Healthy: a first version skips the steps, as in minirollout"

echo "▸ v6 fails half its requests: Argo Rollouts runs the same canary and the same query, and aborts"
k -n "$NS" apply -f "$WORK/rollout-v6.json" > "$WORK/apply-v6.txt" 2>&1 || fail "apply Rollout v6: $(cat "$WORK/apply-v6.txt")"
if ! k -n "$NS" wait --for=jsonpath='{.status.abort}'=true rollout/demo --timeout=180s >/dev/null; then
  kubectl argo rollouts get rollout demo -n "$NS" --context "$CTX" > "$WORK/get.txt" 2>&1 || true
  fail "Argo Rollouts did not abort v6: $(cat "$WORK/get.txt")"
fi
k -n "$NS" wait --for=jsonpath='{.status.phase}'=Degraded rollout/demo --timeout=60s >/dev/null || fail "an aborted Rollout should be Degraded: $(k -n "$NS" get rollout demo -o jsonpath='{.status.phase} {.status.message}' 2>&1)"
k -n "$NS" get rollout demo -o json > "$WORK/rollout-live.json"
[[ "$(field "$WORK/rollout-live.json" status message)" == "RolloutAborted: Rollout aborted update to revision 2"* ]] || fail "Rollout message: $(field "$WORK/rollout-live.json" status message)"
k -n "$NS" get analysisruns -o json > "$WORK/analysisruns.json"
python3 - "$WORK/analysisruns.json" "$MINI" <<'PY' || fail "AnalysisRun: $(cat "$WORK/analysisruns.json")"
import json, sys
runs = json.load(open(sys.argv[1]))["items"]
failed = [r for r in runs if (r.get("status") or {}).get("phase") == "Failed"]
assert failed, [(r["metadata"]["name"], (r.get("status") or {}).get("phase")) for r in runs]
run = failed[-1]
message = run["status"].get("message", "")
assert 'Metric "success-rate" assessed Failed due to failed (1) > failureLimit (0)' in message, message
result = next(m for m in run["status"]["metricResults"] if m["name"] == "success-rate")
argo = json.loads(result["measurements"][0]["value"])[0]
assert 0.3 <= argo <= 0.7, argo
print(f"  AnalysisRun {run['metadata']['name']}: {message}")
print(f"  success rate measured by Argo Rollouts (v6): {argo:.3f}, by minirollout (v3): {float(sys.argv[2]):.3f}")
PY

echo "▸ after the abort the stable ReplicaSet serves on 4 pods and the canary ReplicaSet is scaled to 0"
python3 - "$CTX" "$NS" <<'PY' || fail "ReplicaSets after the abort: $(k -n "$NS" get rs -o wide 2>&1)"
import json, subprocess, sys, time
ctx, ns = sys.argv[1:3]


def kubectl(*args):
    return json.loads(subprocess.run(["kubectl", "--context", ctx, "-n", ns, *args], capture_output=True, text=True, check=True).stdout)


deadline = time.time() + 90
while True:
    rollout = kubectl("get", "rollout", "demo", "-o", "json")["status"]
    stable, current = rollout.get("stableRS"), rollout.get("currentPodHash")
    sets = {rs["metadata"]["labels"].get("rollouts-pod-template-hash"): rs for rs in kubectl("get", "rs", "-l", "app=demo", "-o", "json")["items"]}
    ready = (sets.get(stable) or {}).get("status", {}).get("readyReplicas", 0)
    canary = (sets.get(current) or {}).get("spec", {}).get("replicas", -1)
    if (ready, canary) == (4, 0) or time.time() > deadline:
        break
    time.sleep(2)
assert stable != current, (stable, current)
assert (ready, canary) == (4, 0), {"stable": stable, "ready": ready, "canary": current, "canary_replicas": canary}
print(f"  stable ReplicaSet {stable}: 4 ready; canary ReplicaSet {current}: 0 replicas")
PY
sleep 3
ERRORS="$(promql "$PROM" 'sum(rate(http_requests_total{version="v5",code!~"2.."}[10s])) or vector(0)')"
SERVED="$(promql "$PROM" 'sum(rate(http_requests_total{version="v5"}[10s]))')"
check "$ERRORS" "v == 0" && check "$SERVED" "v > 10" || fail "v5 should keep serving after the abort: errors $ERRORS/s, served $SERVED/s"
kubectl argo rollouts get rollout demo -n "$NS" --context "$CTX" > "$WORK/get.txt" 2>&1 || fail "kubectl argo rollouts get rollout: $(cat "$WORK/get.txt")"
grep -q "Degraded" "$WORK/get.txt" || fail "the plugin should show Degraded: $(cat "$WORK/get.txt")"
sed -n '1,30p' "$WORK/get.txt"

echo "SMOKE OK: cicd/progressive-delivery"
