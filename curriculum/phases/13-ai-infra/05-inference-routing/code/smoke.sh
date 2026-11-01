#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."

fail() { echo "SMOKE FAIL: $*" >&2; exit 1; }
WORK="$(mktemp -d)"
NS="cc-inference-routing"
CTX="kind-cc-smoke"
SIM_IMAGE="ghcr.io/llm-d/llm-d-inference-sim:v1.0.0"
PY_IMAGE="python:3.12-alpine"
GIE_VERSION="v1.6.2"
SIM_ARGS=(--model cc-sim --port 8000 --enable-kvcache --kv-cache-size 64 --block-size 16 --latency-calculator per-token --prefill-overhead 5ms --prefill-time-per-token 1ms)
PIDS=()
CONTAINERS=()
CLUSTER=""
CRDS_APPLIED=""

k() { kubectl --context "$CTX" "$@"; }

stop_pids() {
  for pid in ${PIDS[@]+"${PIDS[@]}"}; do { kill "$pid"; wait "$pid"; } 2>/dev/null || true; done
  PIDS=()
}

cleanup() {
  set +e
  stop_pids
  for c in ${CONTAINERS[@]+"${CONTAINERS[@]}"}; do docker rm -f "$c" >/dev/null 2>&1; done
  if [[ -n "$CLUSTER" ]]; then
    k delete namespace "$NS" --ignore-not-found --wait=true --timeout=120s >/dev/null 2>&1
    if [[ -n "$CRDS_APPLIED" ]]; then k delete -f "$WORK/gie-manifests.yaml" --ignore-not-found --wait=true --timeout=60s >/dev/null 2>&1; fi
  fi
  rm -rf "$WORK"
}
trap cleanup EXIT

wait_line() { for _ in $(seq 1 100); do grep -q "$2" "$1" 2>/dev/null && return 0; sleep 0.1; done; fail "no '$2' in $1: $(cat "$1" 2>/dev/null)"; }

start_router() {
  local policy="$1" log="$2"
  shift 2
  local args=()
  for ep in "$@"; do args+=(--endpoint "$ep"); done
  python3 code/main.py serve "${args[@]}" --policy "$policy" --port 0 --scrape-interval 0.5 --seed 1 > "$log" 2>&1 &
  PIDS+=("$!")
  wait_line "$log" "url="
  ROUTER_URL="$(sed -n 's/.*url=\([^ ]*\).*/\1/p' "$log")"
}

replay_json() {
  local out="$1" url="$2"
  shift 2
  local args=()
  for ep in "$@"; do args+=(--metrics "$ep"); done
  python3 code/main.py replay "$WORK/seq.jsonl" --url "$url" "${args[@]}" --json > "$out" 2> "$out.err" || fail "replay through $url: $(cat "$out" "$out.err")"
}

cat > "$WORK/compare.py" <<'PY'
import json
import sys

rr_path, prefix_path, label, need_sync = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4] == "sync"
rr, prefix = json.load(open(rr_path)), json.load(open(prefix_path))
for name, s in (("round-robin", rr), ("prefix", prefix)):
    assert s["errors"] == 0, (label, name, s["first_error"])
    assert s["server"]["queries"] > 0, (label, name, "the servers counted no prefix cache queries", s["server"])
    if need_sync:
        assert s["server"]["synced"], (label, name, s["server"], s["prompt_tokens"])
assert prefix["server"]["hit_rate"] >= rr["server"]["hit_rate"] + 0.15, (label, "server hit rate", prefix["server"]["hit_rate"], rr["server"]["hit_rate"])
assert prefix["groups_on_one_endpoint"] == prefix["groups_total"], (label, "prefix routing split a tenant", prefix["groups"])
assert rr["groups_on_one_endpoint"] < prefix["groups_on_one_endpoint"], (label, rr["groups"])
assert len(prefix["per_endpoint"]) == 3, (label, "every replica should own some tenants", prefix["per_endpoint"])
assert prefix["latency_ms"]["mean"] < rr["latency_ms"]["mean"], (label, prefix["latency_ms"], rr["latency_ms"])
print(f"  {label}: servers' own hit rate round-robin {rr['server']['hit_rate']:.1%} -> prefix {prefix['server']['hit_rate']:.1%}; "
      f"tenants on one replica {rr['groups_on_one_endpoint']}/{rr['groups_total']} -> {prefix['groups_on_one_endpoint']}/{prefix['groups_total']}; "
      f"mean latency {rr['latency_ms']['mean']:.0f} -> {prefix['latency_ms']['mean']:.0f} ms; usage cached_tokens {prefix['client_hit_rate']:.1%}")
PY

echo "▸ prefixroute trace + simulate: prefix-aware routing against round robin on shared system prompts"
python3 code/main.py trace -o "$WORK/trace.jsonl" > /dev/null
python3 code/main.py trace --rate 0 -o "$WORK/seq.jsonl" > /dev/null
python3 code/main.py simulate "$WORK/trace.jsonl" --json > "$WORK/sim.json" || fail "simulate: $(cat "$WORK/sim.json")"
python3 - "$WORK/sim.json" <<'PY' || fail "simulate: $(head -c 2000 "$WORK/sim.json")"
import json, sys
r = {x["policy"]: x for x in json.load(open(sys.argv[1]))}
p, rr = r["prefix"], r["round-robin"]
assert p["hit_rate"] > rr["hit_rate"] + 0.15, (p["hit_rate"], rr["hit_rate"])
assert p["groups_on_one_endpoint"] == p["groups_total"], p["groups"]
assert p["ttft_ms"]["mean"] < rr["ttft_ms"]["mean"], (p["ttft_ms"], rr["ttft_ms"])
print(f"  simulated hit rate: prefix {p['hit_rate']:.1%}, round-robin {rr['hit_rate']:.1%}, least-queue {r['least-queue']['hit_rate']:.1%}, prefix-only {r['prefix-only']['hit_rate']:.1%}")
PY
python3 code/main.py trace --hot 0.7 --rate 40 --requests 200 -o "$WORK/hot.jsonl" > /dev/null
python3 code/main.py simulate "$WORK/hot.jsonl" --slots 2 --json > "$WORK/hot.json"
python3 - "$WORK/hot.json" <<'PY' || fail "hot tenant: $(head -c 2000 "$WORK/hot.json")"
import json, sys
r = {x["policy"]: x for x in json.load(open(sys.argv[1]))}
only, weighted = r["prefix-only"], r["prefix"]
assert max(only["per_endpoint"].values()) > max(weighted["per_endpoint"].values()), (only["per_endpoint"], weighted["per_endpoint"])
assert only["ttft_ms"]["p95"] > weighted["ttft_ms"]["p95"], (only["ttft_ms"], weighted["ttft_ms"])
print(f"  hot tenant: prefix-only puts {max(only['per_endpoint'].values())}/200 on one replica (TTFT p95 {only['ttft_ms']['p95']:.0f} ms), weighted {max(weighted['per_endpoint'].values())}/200 ({weighted['ttft_ms']['p95']:.0f} ms)")
PY

echo "▸ prefixroute disagg: KV bytes per token and transfer time against prefill"
python3 code/main.py disagg --json --link-gbps 10 --link-gbps 100 > "$WORK/disagg.json"
python3 - "$WORK/disagg.json" <<'PY' || fail "disagg: $(cat "$WORK/disagg.json")"
import json, sys
d = json.load(open(sys.argv[1]))
slow, fast = d["plans"]
assert slow["kv_bytes_per_token"] == 131072 and slow["kv_production_gbps"] == 12.6, slow
assert not slow["link_keeps_up"] and fast["link_keeps_up"], (slow, fast)
assert slow["transfer_ms"] > slow["prefill_ms"] > fast["transfer_ms"], (slow, fast)
print(f"  llama-3.1-8b: 128 KiB/token, 4000 tokens = {slow['kv_transfer_mb']} MB; transfer {slow['transfer_ms']} ms at 10 Gb/s, {fast['transfer_ms']} ms at 100 Gb/s; prefill {slow['prefill_ms']} ms")
PY

echo "▸ prefixroute serve over three fake servers: round robin, then prefix, read from the servers' own counters"
for policy in round-robin prefix; do
  EPS=()
  for n in a b c; do
    python3 code/main.py fake-server --name "fake-$n" --port 0 > "$WORK/fake-$n-$policy.log" 2>&1 &
    PIDS+=("$!")
    wait_line "$WORK/fake-$n-$policy.log" "listening on"
    EPS+=("fake-$n=$(sed -n 's/.*listening on //p' "$WORK/fake-$n-$policy.log")")
  done
  start_router "$policy" "$WORK/router-fake-$policy.log" "${EPS[@]}"
  replay_json "$WORK/fake-$policy.json" "$ROUTER_URL" "${EPS[@]}"
  stop_pids
done
python3 "$WORK/compare.py" "$WORK/fake-round-robin.json" "$WORK/fake-prefix.json" fake-server sync || fail "fake servers: $(cat "$WORK/fake-round-robin.json" "$WORK/fake-prefix.json")"

if ! command -v docker >/dev/null || ! docker info > "$WORK/docker-info.txt" 2>&1; then
  echo "  (docker not available; llm-d-inference-sim steps skipped)"
else
  echo "▸ llm-d-inference-sim: pull $SIM_IMAGE"
  docker pull -q "$SIM_IMAGE" > "$WORK/pull.txt" 2>&1 || fail "docker pull $SIM_IMAGE: $(cat "$WORK/pull.txt")"

  start_sims() {
    SIM_EPS=()
    for n in a b c; do
      docker rm -f "cc-ir-sim-$n" > /dev/null 2>&1 || true
      docker run -d --name "cc-ir-sim-$n" -p 127.0.0.1::8000 -e POD_IP=127.0.0.1 -e POD_NAME="sim-$n" "$SIM_IMAGE" "${SIM_ARGS[@]}" > "$WORK/run-$n.txt" 2>&1 || fail "docker run sim-$n: $(cat "$WORK/run-$n.txt")"
      CONTAINERS+=("cc-ir-sim-$n")
    done
    for n in a b c; do
      docker port "cc-ir-sim-$n" 8000/tcp > "$WORK/port-$n.txt" 2>&1 || fail "docker port sim-$n: $(cat "$WORK/port-$n.txt")"
      local addr
      addr="$(sed -n '1p' "$WORK/port-$n.txt")"
      for _ in $(seq 1 120); do
        curl -fsS -o /dev/null "http://$addr/health" 2>/dev/null && break
        sleep 0.5
      done
      curl -fsS -o /dev/null "http://$addr/health" || fail "sim-$n never became healthy: $(docker logs "cc-ir-sim-$n" 2>&1 | tail -20)"
      SIM_EPS+=("sim-$n=http://$addr")
    done
  }

  echo "▸ three simulator replicas (vLLM API, prefix cache of 64 blocks, 1 ms per uncached prompt token)"
  start_sims
  SIM_A="${SIM_EPS[0]#sim-a=}"
  curl -fsS "$SIM_A/v1/models" > "$WORK/models.json" || fail "GET /v1/models"
  python3 - "$WORK/models.json" <<'PY' || fail "/v1/models: $(cat "$WORK/models.json")"
import json, sys
ids = [m["id"] for m in json.load(open(sys.argv[1]))["data"]]
assert "cc-sim" in ids, ids
print(f"  /v1/models: {ids}")
PY

  echo "▸ the simulator's own prefix cache: one request twice, cached_tokens and vllm:prefix_cache_* move"
  python3 - "$WORK/seq.jsonl" "$SIM_A" <<'PY' || fail "simulator prefix cache did not hit on a repeated request"
import json, sys, urllib.request
body = json.dumps(json.loads(open(sys.argv[1]).readline())["body"]).encode()
opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
def post():
    request = urllib.request.Request(sys.argv[2] + "/v1/chat/completions", data=body, headers={"Content-Type": "application/json"})
    with opener.open(request, timeout=30) as response:
        return response.headers.get("x-inference-pod"), json.load(response)["usage"]
_, first = post()
pod, second = post()
cached = [(u.get("prompt_tokens_details") or {}).get("cached_tokens", 0) for u in (first, second)]
assert cached[1] > cached[0] and cached[1] >= 16, (first, second)
print(f"  {pod}: prompt {second['prompt_tokens']} tokens, cached_tokens {cached[0]} then {cached[1]}")
PY
  for _ in $(seq 1 50); do
    curl -fsS "$SIM_A/metrics" > "$WORK/sim-metrics.txt" 2>/dev/null && grep -Eq '^vllm:prefix_cache_hits_total(\{[^}]*\})? [1-9]' "$WORK/sim-metrics.txt" && break
    sleep 0.2
  done
  grep -Eq '^vllm:prefix_cache_hits_total(\{[^}]*\})? [1-9]' "$WORK/sim-metrics.txt" || fail "vllm:prefix_cache_hits_total never rose above 0: $(grep '^vllm:' "$WORK/sim-metrics.txt" 2>/dev/null)"
  python3 code/main.py scrape "sim-a=$SIM_A" --json > "$WORK/scrape.json" || fail "prefixroute scrape: $(cat "$WORK/scrape.json")"
  python3 - "$WORK/scrape.json" <<'PY' || fail "prefixroute scrape of the simulator: $(cat "$WORK/scrape.json")"
import json, sys
m = json.load(open(sys.argv[1]))["sim-a"]
assert m["prefix_hits"] > 0 and m["prefix_queries"] > m["prefix_hits"], m
for name in ("vllm:num_requests_waiting", "vllm:num_requests_running", "vllm:kv_cache_usage_perc"):
    assert name in m["found"], (name, m["found"])
print(f"  prefixroute scrape: hits {m['prefix_hits']:.0f}/{m['prefix_queries']:.0f} tokens, block_size {m['block_size']}, {len(m['found'])} vllm:* metrics")
PY

  echo "▸ prefixroute in front of fresh simulator replicas: round robin, then prefix"
  for policy in round-robin prefix; do
    start_sims
    start_router "$policy" "$WORK/router-sim-$policy.log" "${SIM_EPS[@]}"
    replay_json "$WORK/sim-$policy.json" "$ROUTER_URL" "${SIM_EPS[@]}"
    stop_pids
  done
  python3 "$WORK/compare.py" "$WORK/sim-round-robin.json" "$WORK/sim-prefix.json" llm-d-inference-sim nosync || fail "simulator replicas: $(cat "$WORK/sim-round-robin.json" "$WORK/sim-prefix.json")"
  python3 code/main.py simulate "$WORK/seq.jsonl" --slots 1 --json > "$WORK/sim-shape.json"
  python3 - "$WORK/sim-shape.json" "$WORK/sim-round-robin.json" "$WORK/sim-prefix.json" <<'PY' || fail "prefixroute simulate and the simulator disagree on direction"
import json, sys
r = {x["policy"]: x for x in json.load(open(sys.argv[1]))}
real_rr, real_prefix = (json.load(open(p))["server"]["hit_rate"] for p in sys.argv[2:4])
assert r["prefix"]["hit_rate"] > r["round-robin"]["hit_rate"] and real_prefix > real_rr
print(f"  same shape in prefixroute simulate: round-robin {r['round-robin']['hit_rate']:.1%} -> prefix {r['prefix']['hit_rate']:.1%}; simulator replicas {real_rr:.1%} -> {real_prefix:.1%}")
PY
  for c in ${CONTAINERS[@]+"${CONTAINERS[@]}"}; do docker rm -f "$c" > /dev/null 2>&1 || true; done
fi

kubectl config get-contexts -o name > "$WORK/contexts.txt" 2>/dev/null || true
if ! command -v kubectl > /dev/null || ! grep -qx "$CTX" "$WORK/contexts.txt"; then
  echo "  (no $CTX context; InferencePool steps skipped)"
  echo "SMOKE OK: ai-infra/inference-routing"
  exit 0
fi
k cluster-info > "$WORK/cluster-info.txt" 2>&1 || fail "context $CTX is not reachable: $(cat "$WORK/cluster-info.txt")"
CLUSTER=1

echo "▸ Gateway API Inference Extension $GIE_VERSION CRDs (InferencePool), server-side apply"
if k get crd inferencepools.inference.networking.k8s.io > /dev/null 2>&1; then
  echo "  (InferencePool CRD already installed; using and keeping it)"
else
  curl -fsSL -o "$WORK/gie-manifests.yaml" "https://github.com/kubernetes-sigs/gateway-api-inference-extension/releases/download/$GIE_VERSION/manifests.yaml" || fail "download $GIE_VERSION manifests.yaml"
  CRDS_APPLIED=1
  k apply --server-side -f "$WORK/gie-manifests.yaml" > "$WORK/crds.txt" 2>&1 || fail "apply CRDs: $(cat "$WORK/crds.txt")"
  k wait --for=condition=Established --timeout=60s crd/inferencepools.inference.networking.k8s.io > /dev/null || fail "InferencePool CRD not established"
fi

echo "▸ two simulator replicas as a Deployment in $NS"
k create namespace "$NS" > /dev/null
python3 - "$NS" "$SIM_IMAGE" > "$WORK/deploy.json" <<'PY'
import json, sys
ns, image = sys.argv[1:3]
args = ["--model", "cc-sim", "--port", "8000", "--enable-kvcache", "--kv-cache-size", "256", "--block-size", "16", "--latency-calculator", "per-token", "--prefill-overhead", "5ms", "--prefill-time-per-token", "1ms"]
env = [{"name": "POD_IP", "valueFrom": {"fieldRef": {"fieldPath": "status.podIP"}}}, {"name": "POD_NAME", "valueFrom": {"fieldRef": {"fieldPath": "metadata.name"}}}]
container = {"name": "sim", "image": image, "args": args, "env": env, "ports": [{"name": "http", "containerPort": 8000}], "readinessProbe": {"httpGet": {"path": "/health", "port": 8000}, "periodSeconds": 2}, "resources": {"requests": {"cpu": "10m", "memory": "32Mi"}}}
print(json.dumps({"apiVersion": "apps/v1", "kind": "Deployment", "metadata": {"name": "cc-sim", "namespace": ns}, "spec": {"replicas": 2, "selector": {"matchLabels": {"app": "cc-sim"}}, "template": {"metadata": {"labels": {"app": "cc-sim"}}, "spec": {"containers": [container]}}}}))
PY
k apply -f "$WORK/deploy.json" > "$WORK/deploy.txt" 2>&1 || fail "apply Deployment: $(cat "$WORK/deploy.txt")"
k rollout status deployment/cc-sim -n "$NS" --timeout=180s > /dev/null || fail "simulator Deployment not ready: $(k get pods -n "$NS" -o wide 2>&1; k describe pods -n "$NS" 2>&1 | tail -20)"

echo "▸ InferencePool selecting the replicas; the API server and prefixroute pool refuse the same invalid pools"
cat > "$WORK/pool.yaml" <<EOF
apiVersion: inference.networking.k8s.io/v1
kind: InferencePool
metadata:
  name: sim-pool
  namespace: $NS
spec:
  selector:
    matchLabels:
      app: cc-sim
  targetPorts:
    - number: 8000
  endpointPickerRef:
    name: sim-epp
    port:
      number: 9002
EOF
k apply -f "$WORK/pool.yaml" > "$WORK/pool-apply.txt" 2>&1 || fail "apply InferencePool: $(cat "$WORK/pool-apply.txt")"
python3 - "$NS" "$WORK" <<'PY'
import json, sys
ns, work = sys.argv[1:3]
base = {"apiVersion": "inference.networking.k8s.io/v1", "kind": "InferencePool", "metadata": {"namespace": ns}}
dup = dict(base, metadata={"name": "dup-ports", "namespace": ns}, spec={"selector": {"matchLabels": {"app": "cc-sim"}}, "targetPorts": [{"number": 8000}, {"number": 8000}]})
noport = dict(base, metadata={"name": "no-port", "namespace": ns}, spec={"selector": {"matchLabels": {"app": "cc-sim"}}, "targetPorts": [{"number": 8000}], "endpointPickerRef": {"name": "sim-epp"}})
json.dump(dup, open(f"{work}/dup.json", "w"))
json.dump(noport, open(f"{work}/noport.json", "w"))
PY
for case in "dup:port number must be unique" "noport:port is required when kind is 'Service'"; do
  file="${case%%:*}"
  want="${case#*:}"
  if k apply --dry-run=server -f "$WORK/$file.json" > "$WORK/$file.api.txt" 2>&1; then fail "API server accepted $file: $(cat "$WORK/$file.api.txt")"; fi
  grep -qF "$want" "$WORK/$file.api.txt" || fail "API server rejected $file without '$want': $(cat "$WORK/$file.api.txt")"
  CODE=0
  python3 code/main.py pool "$WORK/$file.json" > /dev/null 2> "$WORK/$file.mine.txt" || CODE=$?
  [[ "$CODE" == "2" ]] && grep -qF "$want" "$WORK/$file.mine.txt" || fail "prefixroute pool on $file: exit $CODE, $(cat "$WORK/$file.mine.txt")"
  echo "  $file: API server and prefixroute both say '$want'"
done

k get inferencepools.inference.networking.k8s.io,pods -n "$NS" -o json > "$WORK/live.json"
python3 code/main.py pool "$WORK/live.json" --pool sim-pool > "$WORK/pool.txt" || fail "prefixroute pool on live objects: $(cat "$WORK/pool.txt")"
cat "$WORK/pool.txt"
python3 code/main.py pool "$WORK/live.json" --pool sim-pool --json > "$WORK/pool.json"
k get pods -n "$NS" -l app=cc-sim -o jsonpath='{range .items[*]}{.status.podIP}{"\n"}{end}' > "$WORK/pod-ips.txt"
python3 - "$WORK/pool.json" "$WORK/pod-ips.txt" <<'PY' || fail "pool resolution differs from kubectl: $(cat "$WORK/pool.json" "$WORK/pod-ips.txt")"
import json, sys
endpoints = json.load(open(sys.argv[1]))["endpoints"]
ips = sorted(line.strip() for line in open(sys.argv[2]) if line.strip())
assert sorted(e["ip"] for e in endpoints if e["ready"]) == ips and len(ips) == 2, (endpoints, ips)
print(f"  prefixroute pool resolves the same {len(ips)} Pods kubectl selects: {', '.join(ip + ':8000' for ip in ips)}")
PY
ENDPOINTS="$(python3 code/main.py pool "$WORK/live.json" --pool sim-pool --args)"
METRICS="$(python3 -c 'import json, sys; print(" ".join("--metrics %s=%s" % (e["name"], e["url"]) for e in json.load(open(sys.argv[1]))["endpoints"] if e["ready"]))' "$WORK/pool.json")"

echo "▸ prefixroute inside the cluster, routing the pool's endpoints, the trace replayed through it"
k create configmap prefixroute -n "$NS" --from-file=main.py=code/main.py --from-file=trace.jsonl="$WORK/seq.jsonl" > /dev/null
python3 - "$NS" "$PY_IMAGE" "$ENDPOINTS" "$METRICS" > "$WORK/client.json" <<'PY'
import json, sys
ns, image, endpoints, metrics = sys.argv[1:5]
script = ("python3 /app/main.py serve $ENDPOINTS --policy prefix --port 8080 > /tmp/router.log 2>&1 & "
          "python3 /app/main.py replay /app/trace.jsonl --url http://127.0.0.1:8080 --wait-ready 30 $METRICS --json; "
          "status=$?; [ $status -eq 0 ] || cat /tmp/router.log; exit $status")
container = {"name": "run", "image": image, "command": ["sh", "-c", script], "env": [{"name": "ENDPOINTS", "value": endpoints}, {"name": "METRICS", "value": metrics}], "volumeMounts": [{"name": "app", "mountPath": "/app"}]}
print(json.dumps({"apiVersion": "v1", "kind": "Pod", "metadata": {"name": "router-client", "namespace": ns}, "spec": {"restartPolicy": "Never", "containers": [container], "volumes": [{"name": "app", "configMap": {"name": "prefixroute"}}]}}))
PY
k apply -f "$WORK/client.json" > /dev/null || fail "apply router-client pod"
PHASE=""
for _ in $(seq 1 120); do
  PHASE="$(k get pod router-client -n "$NS" -o jsonpath='{.status.phase}' 2>/dev/null || true)"
  [[ "$PHASE" == "Succeeded" || "$PHASE" == "Failed" ]] && break
  sleep 2
done
k logs router-client -n "$NS" > "$WORK/client.log" 2>&1 || true
[[ "$PHASE" == "Succeeded" ]] || fail "router-client pod phase '$PHASE': $(cat "$WORK/client.log"; k describe pod router-client -n "$NS" 2>&1 | tail -20)"
python3 - "$WORK/client.log" "$WORK/pool.json" <<'PY' || fail "in-cluster routing: $(cat "$WORK/client.log")"
import json, sys
summary = json.load(open(sys.argv[1]))
pods = {e["name"] for e in json.load(open(sys.argv[2]))["endpoints"] if e["ready"]}
assert summary["errors"] == 0, summary["first_error"]
assert set(summary["per_endpoint"]) == pods, (summary["per_endpoint"], pods)
assert summary["groups_on_one_endpoint"] == summary["groups_total"], summary["groups"]
assert summary["server"]["hit_rate"] >= 0.5, summary["server"]
print(f"  {summary['requests']} requests over the pool's {len(pods)} Pods {summary['per_endpoint']}; every tenant on one Pod; the Pods' own hit rate {summary['server']['hit_rate']:.1%}")
PY

echo "SMOKE OK: ai-infra/inference-routing"
