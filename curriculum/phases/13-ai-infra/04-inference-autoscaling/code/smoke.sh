#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."

fail() { echo "SMOKE FAIL: $*" >&2; exit 1; }
WORK="$(mktemp -d)"
NS="cc-inference-autoscaling"
CTX="${CC_SMOKE_CONTEXT:-kind-cc-smoke}"
SIM_IMAGE="ghcr.io/llm-d/llm-d-inference-sim:v1.0.0"
DOCKER_NAME="cc-infscale-sim"
K8S="code/fixtures/k8s"
F="code/fixtures"
STATE="$WORK/state.json"
POLICY=(--metric queue --target 4 --min 1 --max 3 --down-window 20)
PIDS=()
NS_CREATED=""
DOCKER_STARTED=""

k() { kubectl --context "$CTX" "$@"; }

cleanup() {
  set +e
  for pid in ${PIDS[@]+"${PIDS[@]}"}; do { kill "$pid"; wait "$pid"; } 2>/dev/null; done
  if [[ -n "$DOCKER_STARTED" ]]; then docker rm -f "$DOCKER_NAME" >/dev/null 2>&1; fi
  if [[ -n "$NS_CREATED" ]]; then k delete namespace "$NS" --ignore-not-found --wait=false >/dev/null 2>&1; fi
  rm -rf "$WORK"
}
trap cleanup EXIT

field() {
  python3 - "$@" <<'PY'
import json, sys
value = json.load(open(sys.argv[1]))
for key in sys.argv[2:]:
    value = value[int(key)] if isinstance(value, list) else value[key]
print(json.dumps(value) if isinstance(value, (dict, list)) else value)
PY
}

echo "▸ infscale parse: vLLM, llama.cpp, SGLang and simulator pages become the same signals"
python3 code/main.py parse "$F/vllm-busy.prom" "$F/llamacpp.prom" "$F/sglang.prom" "$F/sim-loaded.prom" --json > "$WORK/parse.json" || fail "parse: $(cat "$WORK/parse.json")"
python3 - "$WORK/parse.json" <<'PY' || fail "parse signals: $(cat "$WORK/parse.json")"
import json, sys
rows = json.load(open(sys.argv[1]))
got = [(r["server"], r["running"], r["waiting"]) for r in rows]
assert got == [("vllm", 7.0, 12.0), ("llama.cpp", 4.0, 3.0), ("sglang", 162.0, 2826.0), ("vllm", 2.0, 10.0)], got
print("  " + ", ".join(f"{s} running {r:g} waiting {w:g}" for s, r, w in got))
PY

echo "▸ infscale decide: 12 in flight at a target of 4 per replica means 3 replicas"
python3 code/main.py decide "$F/sim-loaded.prom" --target 4 --max 3 --json > "$WORK/decide.json" || fail "decide: $(cat "$WORK/decide.json")"
[[ "$(field "$WORK/decide.json" desired)" == "3" && "$(field "$WORK/decide.json" direction)" == "up" ]] || fail "decide should scale 1 -> 3: $(cat "$WORK/decide.json")"
CODE=0
python3 code/main.py decide --metric cpu --min 0 --target 60 > /dev/null 2> "$WORK/cpu-zero.err" || CODE=$?
[[ "$CODE" == "2" ]] && grep -q "cannot scale from zero" "$WORK/cpu-zero.err" || fail "cpu with min 0 should exit 2: code $CODE, $(cat "$WORK/cpu-zero.err")"
echo "  queue 12 / 4 = 3; cpu with --min 0 refused: $(cat "$WORK/cpu-zero.err")"

echo "▸ infscale simulate: CPU-based against queue-based policies on the default spike trace"
python3 code/main.py simulate > "$WORK/simulate.txt" || fail "simulate: $(cat "$WORK/simulate.txt")"
sed -n '/^policy/,$p' "$WORK/simulate.txt"
python3 code/main.py simulate --json > "$WORK/simulate.json"
python3 - "$WORK/simulate.json" <<'PY' || fail "simulate comparison: $(cat "$WORK/simulate.json")"
import json, sys
r = {x["policy"]: x for x in json.load(open(sys.argv[1]))["results"]}
cpu60, cpu25, queue, look, zero = r["cpu:60"], r["cpu:25"], r["queue:24"], r["queue:24,lookahead=90"], r["queue:24,min=0,cooldown=120"]
assert cpu60["peak_replicas"] == 1 and cpu60["scale_events"] == 0 and cpu60["miss_rate"] > 0.5, ("cpu:60 should never scale and miss most SLOs", cpu60)
assert cpu25["peak_replicas"] == 8, ("cpu:25 should run to max", cpu25)
assert queue["miss_rate"] < 0.1 and queue["replica_minutes"] < cpu25["replica_minutes"], ("queue:24 should meet the SLO more cheaply than cpu:25", queue, cpu25)
assert look["misses"] <= queue["misses"], ("lookahead should not miss more", look, queue)
assert zero["replica_minutes"] < queue["replica_minutes"], ("scale to zero should save replica-minutes", zero, queue)
print(f"  cpu:60 misses {cpu60['miss_rate']:.0%}; cpu:25 {cpu25['replica_minutes']:.0f} replica-min; queue:24 misses {queue['miss_rate']:.1%} at {queue['replica_minutes']:.0f} replica-min")
PY

if command -v docker >/dev/null && docker info >/dev/null 2>&1; then
  echo "▸ Docker: $SIM_IMAGE with max-num-seqs 2, loaded by infscale load, scraped by infscale decide"
  PORT="$(python3 -c 'import socket; s=socket.socket(); s.bind(("127.0.0.1", 0)); print(s.getsockname()[1]); s.close()')"
  docker rm -f "$DOCKER_NAME" >/dev/null 2>&1 || true
  DOCKER_STARTED=1
  docker run -d --name "$DOCKER_NAME" -p "127.0.0.1:$PORT:8000" "$SIM_IMAGE" --model cc-tiny --port 8000 --max-num-seqs 2 --time-to-first-token 1s --inter-token-latency 20ms > "$WORK/docker-run.txt" 2>&1 || fail "docker run: $(cat "$WORK/docker-run.txt")"
  for _ in $(seq 1 60); do curl -fsS "http://127.0.0.1:$PORT/health" > /dev/null 2>&1 && break; sleep 0.5; done
  curl -fsS "http://127.0.0.1:$PORT/health" > /dev/null 2>&1 || fail "simulator never became healthy: $(docker logs "$DOCKER_NAME" 2>&1 | tail -20)"
  curl -fsS "http://127.0.0.1:$PORT/v1/models" > "$WORK/models.json" || fail "GET /v1/models"
  [[ "$(field "$WORK/models.json" data 0 id)" == "cc-tiny" ]] || fail "/v1/models: $(cat "$WORK/models.json")"
  python3 code/main.py load --url "http://127.0.0.1:$PORT" --concurrency 8 --duration 12 --max-tokens 16 --json > "$WORK/docker-load.json" 2> "$WORK/docker-load.err" &
  PIDS+=("$!")
  LOAD_PID="$!"
  WAITING=0
  for _ in $(seq 1 30); do
    python3 code/main.py decide "http://127.0.0.1:$PORT/metrics" "${POLICY[@]}" --json > "$WORK/docker-decide.json" 2>&1 || fail "decide against the simulator: $(cat "$WORK/docker-decide.json")"
    WAITING="$(field "$WORK/docker-decide.json" replicas 0 waiting)"
    python3 -c "import sys; sys.exit(0 if float(sys.argv[1]) >= 4 else 1)" "$WAITING" && break
    sleep 0.5
  done
  python3 -c "import sys; sys.exit(0 if float(sys.argv[1]) >= 4 else 1)" "$WAITING" || fail "8 concurrent requests on max-num-seqs 2 should leave requests waiting: $(cat "$WORK/docker-decide.json")"
  [[ "$(field "$WORK/docker-decide.json" replicas 0 running)" == "2.0" ]] || fail "running should be capped at max-num-seqs 2: $(cat "$WORK/docker-decide.json")"
  [[ "$(field "$WORK/docker-decide.json" desired)" -ge 2 ]] || fail "decide should scale up under load: $(cat "$WORK/docker-decide.json")"
  echo "  running 2, waiting $WAITING -> desired $(field "$WORK/docker-decide.json" desired)"
  wait "$LOAD_PID" || fail "load against the simulator: $(cat "$WORK/docker-load.json" "$WORK/docker-load.err")"
  PIDS=()
  [[ "$(field "$WORK/docker-load.json" ok)" -ge 10 && "$(field "$WORK/docker-load.json" model)" == "cc-tiny" ]] || fail "load report: $(cat "$WORK/docker-load.json")"
  echo "  load: $(field "$WORK/docker-load.json" ok) ok, $(field "$WORK/docker-load.json" requests_per_s) req/s, p95 $(field "$WORK/docker-load.json" latency_p95_s) s"
  curl -fsS "http://127.0.0.1:$PORT/metrics" > "$WORK/sim-metrics.txt" || fail "GET /metrics"
  grep -E '^vllm:(num_requests_running|num_requests_waiting|kv_cache_usage_perc|generation_tokens_total)' "$WORK/sim-metrics.txt" | sed 's/^/  /'
  docker rm -f "$DOCKER_NAME" >/dev/null 2>&1 || true
  DOCKER_STARTED=""
else
  echo "  (docker not available; local simulator checks skipped)"
fi

if ! command -v kubectl >/dev/null; then
  echo "  (kubectl not installed; cluster checks skipped)"
  echo "SMOKE OK: ai-infra/inference-autoscaling (cluster checks skipped)"
  exit 0
fi
kubectl config get-contexts -o name > "$WORK/contexts.txt" 2>/dev/null || true
if ! grep -qx "$CTX" "$WORK/contexts.txt"; then
  echo "  (context $CTX not found; cluster checks skipped)"
  echo "SMOKE OK: ai-infra/inference-autoscaling (cluster checks skipped)"
  exit 0
fi

kube() {
  python3 code/main.py kube --context "$CTX" -n "$NS" --deployment cc-sim --port 8000 --state "$STATE" "${POLICY[@]}" --json "$@" > "$WORK/kube.json" 2> "$WORK/kube.err" || fail "infscale kube $*: $(cat "$WORK/kube.err")"
}

echo "▸ kind ($CTX): the simulator Deployment and Service in $NS, plus an HPA on an External metric"
k delete namespace "$NS" --ignore-not-found --wait=true --timeout=90s >/dev/null 2>&1 || true
NS_CREATED=1
k create namespace "$NS" >/dev/null || fail "create namespace $NS"
k -n "$NS" create configmap infscale --from-file=main.py=code/main.py >/dev/null || fail "configmap"
k -n "$NS" apply -f "$K8S/sim.yaml" > "$WORK/apply.txt" 2>&1 || fail "apply sim.yaml: $(cat "$WORK/apply.txt")"
k -n "$NS" apply -f "$K8S/hpa-external.yaml" > "$WORK/apply-hpa.txt" 2>&1 || fail "apply hpa-external.yaml: $(cat "$WORK/apply-hpa.txt")"
k -n "$NS" rollout status deployment/cc-sim --timeout=180s > /dev/null || fail "cc-sim not ready: $(k -n "$NS" get pods -o wide 2>&1; k -n "$NS" describe pods 2>&1 | tail -30)"

echo "▸ the HPA cannot read vllm_requests_in_flight: no external metrics adapter is installed"
ACTIVE=""
for _ in $(seq 1 45); do
  ACTIVE="$(k -n "$NS" get hpa cc-sim -o jsonpath='{.status.conditions[?(@.type=="ScalingActive")].status}' 2>/dev/null || true)"
  [[ "$ACTIVE" == "False" ]] && break
  sleep 2
done
REASON="$(k -n "$NS" get hpa cc-sim -o jsonpath='{.status.conditions[?(@.type=="ScalingActive")].reason}' 2>/dev/null || true)"
[[ "$ACTIVE" == "False" ]] || fail "HPA ScalingActive should be False without an adapter: $(k -n "$NS" describe hpa cc-sim 2>&1 | tail -20)"
echo "  ScalingActive=False reason=$REASON; replicas unchanged at $(k -n "$NS" get deployment cc-sim -o jsonpath='{.spec.replicas}')"
k -n "$NS" delete hpa cc-sim >/dev/null || fail "delete hpa"
if k get crd scaledobjects.keda.sh >/dev/null 2>&1; then
  k -n "$NS" apply --dry-run=server -f "$K8S/keda-scaledobject.yaml" > "$WORK/keda.txt" 2>&1 || fail "KEDA ScaledObject dry run: $(cat "$WORK/keda.txt")"
  echo "  KEDA is installed: the ScaledObject passes a server-side dry run"
else
  echo "  (KEDA not installed; ScaledObject dry run skipped)"
fi

echo "▸ idle: infscale kube scrapes the pod through the API server proxy and keeps 1 replica"
kube
[[ "$(field "$WORK/kube.json" ready)" == "1" && "$(field "$WORK/kube.json" desired)" == "1" ]] || fail "idle decision: $(cat "$WORK/kube.json")"
k get --raw "/api/v1/namespaces/$NS/pods/$(k -n "$NS" get pods -l app=cc-sim -o jsonpath='{.items[0].metadata.name}'):8000/proxy/metrics" > "$WORK/pod-metrics.txt" || fail "API server proxy to /metrics"
grep -q '^vllm:num_requests_waiting' "$WORK/pod-metrics.txt" || fail "pod /metrics has no vllm:num_requests_waiting: $(head -20 "$WORK/pod-metrics.txt")"

echo "▸ load: a pod running infscale load at concurrency 12 through the Service"
k -n "$NS" apply -f "$K8S/load-pod.yaml" > /dev/null || fail "apply load-pod.yaml"
k -n "$NS" wait --for=condition=Ready pod/cc-load --timeout=120s > /dev/null || fail "load pod not running: $(k -n "$NS" describe pod cc-load 2>&1 | tail -20)"
DESIRED=1
for _ in $(seq 1 30); do
  kube
  DESIRED="$(field "$WORK/kube.json" desired)"
  [[ "$DESIRED" == "3" ]] && break
  sleep 2
done
[[ "$DESIRED" == "3" ]] || fail "under load infscale should want 3 replicas: $(cat "$WORK/kube.json")"
python3 - "$WORK/kube.json" <<'PY'
import json, sys
d = json.load(open(sys.argv[1]))
r = d["replicas"][0]
cpu = "-" if r.get("cpu_percent") is None else f"{r['cpu_percent']:.0f}% of its 50m request"
print(f"  1 replica: running {r['running']:g}, waiting {r['waiting']:g}, cpu {cpu} -> desired {d['desired']} ({d['reason']})")
PY

echo "▸ apply: kubectl scale to 3, the new replicas become Ready and take traffic"
kube --apply
[[ "$(field "$WORK/kube.json" applied)" == "True" && "$(field "$WORK/kube.json" desired)" == "3" ]] || fail "apply: $(cat "$WORK/kube.json")"
[[ "$(k -n "$NS" get deployment cc-sim -o jsonpath='{.spec.replicas}')" == "3" ]] || fail "spec.replicas should be 3"
k -n "$NS" rollout status deployment/cc-sim --timeout=120s > /dev/null || fail "scaled replicas not ready: $(k -n "$NS" get pods -o wide 2>&1)"
SERVED=""
for _ in $(seq 1 30); do
  kube
  SERVED="$(python3 - "$WORK/kube.json" <<'PY'
import json, sys
d = json.load(open(sys.argv[1]))
rows = d["replicas"]
ok = len(rows) == 3 and all((r.get("generation_tokens") or 0) > 0 for r in rows)
print("yes" if ok else "no")
PY
)"
  [[ "$SERVED" == "yes" ]] && break
  sleep 2
done
[[ "$SERVED" == "yes" ]] || fail "every replica should have generated tokens: $(cat "$WORK/kube.json")"
python3 - "$WORK/kube.json" <<'PY'
import json, sys
d = json.load(open(sys.argv[1]))
for r in d["replicas"]:
    print(f"  {r['name']}: running {r['running']:g} waiting {r['waiting']:g} generated {r['generation_tokens']:g}")
print(f"  desired {d['desired']} ({d['reason']})")
PY

echo "▸ after the load: the 20 s scale-down window holds 3, then infscale scales back to 1"
PHASE=""
for _ in $(seq 1 75); do
  PHASE="$(k -n "$NS" get pod cc-load -o jsonpath='{.status.phase}')"
  [[ "$PHASE" == "Succeeded" || "$PHASE" == "Failed" ]] && break
  kube --apply
  sleep 2
done
[[ "$PHASE" == "Succeeded" ]] || fail "load pod phase $PHASE: $(k -n "$NS" logs cc-load 2>&1 | tail -20)"
k -n "$NS" logs cc-load > "$WORK/load.json" || fail "logs cc-load"
python3 - "$WORK/load.json" <<'PY' || fail "load report: $(cat "$WORK/load.json")"
import json, sys
report = json.load(open(sys.argv[1]))
assert report["ok"] >= 20 and set(report["statuses"]) == {"200"}, report
print(f"  load pod: {report['ok']} ok at concurrency {report['concurrency']}, {report['requests_per_s']} req/s, p95 {report['latency_p95_s']:.2f} s")
PY
HELD=""
FINAL=""
for _ in $(seq 1 30); do
  kube --apply
  FINAL="$(field "$WORK/kube.json" desired)"
  if [[ "$FINAL" == "3" && "$(field "$WORK/kube.json" reason)" == "stabilised" ]]; then HELD=1; fi
  [[ "$FINAL" == "1" ]] && break
  sleep 3
done
[[ -n "$HELD" ]] || fail "the scale-down window should have held 3 at least once: $(cat "$WORK/kube.json")"
[[ "$FINAL" == "1" && "$(k -n "$NS" get deployment cc-sim -o jsonpath='{.spec.replicas}')" == "1" ]] || fail "should scale back to 1: $(cat "$WORK/kube.json")"
field "$WORK/kube.json" steps | python3 -c 'import json,sys; [print("  " + s) for s in json.load(sys.stdin)]'
k -n "$NS" rollout status deployment/cc-sim --timeout=60s > /dev/null || fail "rollout after scale-down"

echo "SMOKE OK: ai-infra/inference-autoscaling"
