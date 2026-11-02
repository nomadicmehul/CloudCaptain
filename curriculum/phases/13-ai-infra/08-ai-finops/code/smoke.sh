#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."

fail() { echo "SMOKE FAIL: $*" >&2; exit 1; }
WORK="$(mktemp -d)"
F=code/fixtures
TODAY=2026-11-01
CTX="kind-cc-smoke"
NS="cc-ai-finops"
RESOURCE="cc-ai-finops.example.com/gpu"
RESOURCE_PATH="cc-ai-finops.example.com~1gpu"
SERVER="cc-smoke-ai-finops-llm-$$"
LLAMA_IMAGE="${LLAMA_IMAGE:-ghcr.io/ggml-org/llama.cpp:server-b11706}"
MODEL_REPO="bartowski/SmolLM2-135M-Instruct-GGUF"
MODEL_REVISION="09816acd5d99df7be770d85ea30822623dab342c"
MODEL_FILE="SmolLM2-135M-Instruct-Q8_0.gguf"
MODEL_SHA256="5a1395716f7913741cc51d98581b9b1228d80987a9f7d3664106742eb06bba83"
MODELS="${CC_MODEL_CACHE:-$WORK/models}"
NODE=""
PATCHED=""
NS_CREATED=""

k() { kubectl --context "$CTX" "$@"; }

cleanup() {
  set +e
  if command -v docker >/dev/null; then docker rm -f "$SERVER" >/dev/null 2>&1; fi
  if [[ -n "$NS_CREATED" ]]; then k delete namespace "$NS" --ignore-not-found --wait=true --timeout=120s >/dev/null 2>&1; fi
  if [[ -n "$PATCHED" ]]; then k patch node "$NODE" --subresource=status --type=json -p "[{\"op\":\"remove\",\"path\":\"/status/capacity/$RESOURCE_PATH\"}]" >/dev/null 2>&1; fi
  rm -rf "$WORK"
}
trap cleanup EXIT

free_port() { python3 -c 'import socket; s=socket.socket(); s.bind(("127.0.0.1", 0)); print(s.getsockname()[1]); s.close()'; }

echo "▸ prices: every entry dated and sourced; an old one is a warning, a missing source an error"
python3 code/main.py prices "$F/prices.json" --today "$TODAY" > "$WORK/prices.txt" 2> "$WORK/prices.err" || fail "prices: $(cat "$WORK/prices.err")"
grep -q "gpu-l4-1x" "$WORK/prices.txt" || fail "prices table: $(cat "$WORK/prices.txt")"
[[ ! -s "$WORK/prices.err" ]] || fail "fresh prices should not warn: $(cat "$WORK/prices.err")"
python3 code/main.py prices "$F/prices.json" --today 2027-06-01 > /dev/null 2> "$WORK/stale.err" || fail "stale prices must still load: $(cat "$WORK/stale.err")"
grep -q "216 days old" "$WORK/stale.err" || fail "no stale warning: $(cat "$WORK/stale.err")"
[[ "$(grep -c '^WARNING' "$WORK/stale.err")" == "6" ]] || fail "expected six stale warnings: $(cat "$WORK/stale.err")"
python3 - "$F/prices.json" "$WORK/unsourced.json" <<'PY'
import json, sys
data = json.load(open(sys.argv[1]))
data["apis"][0]["source"] = ""
json.dump(data, open(sys.argv[2], "w"))
PY
set +e
python3 code/main.py prices "$WORK/unsourced.json" --today "$TODAY" > /dev/null 2> "$WORK/unsourced.err"
CODE=$?
set -e
[[ "$CODE" == "2" ]] && grep -q "no source" "$WORK/unsourced.err" || fail "an unsourced price must exit 2 (got $CODE): $(cat "$WORK/unsourced.err")"
echo "  6 entries load; 216 days later all six warn; an empty source exits 2"

echo "▸ estimate: the four GPU options regenerate from datasheet numbers"
est() { python3 code/main.py estimate --prices "$F/prices.json" --today "$TODAY" --model llama-8b --params-b 8 --layers 32 --kv-heads 8 --head-dim 128 --context 4096 --input-tokens 600 --output-tokens 200 "$@"; }
est --instance gpu-l4-1x --quantization Q4_K --batches 1,4,8,16,24,31 -o "$WORK/l4-llama8b-q4_k.json"
est --instance gpu-l4-1x --quantization Q8_0 --batches 1,4,8,16,24 -o "$WORK/l4-llama8b-q8_0.json"
est --instance gpu-l4-1x --quantization F16 --batches 1,4,8,10 -o "$WORK/l4-llama8b-f16.json"
est --instance gpu-h100-1x --quantization F16 --batches 1,8,16,32,64 -o "$WORK/h100-llama8b-f16.json"
python3 - "$WORK" "$F" <<'PY' || fail "estimates differ from the fixtures"
import json, os, sys
batches = {}
for name in ("l4-llama8b-f16.json", "l4-llama8b-q8_0.json", "l4-llama8b-q4_k.json", "h100-llama8b-f16.json"):
    new, old = (json.load(open(os.path.join(folder, name))) for folder in sys.argv[1:3])
    assert new["points"] == old["points"] and new["max_batch"] == old["max_batch"], (name, new, old)
    batches[new["name"]] = new["max_batch"]
print("  max batch: " + ", ".join(f"{name} {value}" for name, value in batches.items()))
PY

echo "▸ plan: the budget review on the fixtures matches expected-plan.json"
GPU_OPTIONS=("$WORK/l4-llama8b-f16.json" "$WORK/l4-llama8b-q8_0.json" "$WORK/l4-llama8b-q4_k.json" "$WORK/h100-llama8b-f16.json")
python3 code/main.py plan --prices "$F/prices.json" --today "$TODAY" --scenario "$F/scenario.json" --measurements "${GPU_OPTIONS[@]}" --json -o "$WORK/plan.json"
python3 code/main.py plan --prices "$F/prices.json" --today "$TODAY" --scenario "$F/scenario.json" --measurements "${GPU_OPTIONS[@]}" > "$WORK/plan.txt"
python3 - "$WORK/plan.json" "$F/expected-plan.json" <<'PY' || fail "plan differs from expected-plan.json"
import json, sys
sys.path.insert(0, "code/tests")
from test_main import summarize

def close(expected, actual, path):
    if isinstance(expected, dict):
        assert sorted(expected) == sorted(actual), (path, sorted(expected), sorted(actual))
        for key in expected:
            close(expected[key], actual[key], f"{path}.{key}")
    elif isinstance(expected, float) or isinstance(actual, float):
        assert abs(expected - actual) <= max(1e-6, abs(expected) * 1e-3), (path, expected, actual)
    else:
        assert expected == actual, (path, expected, actual)

report = json.load(open(sys.argv[1]))
close(json.load(open(sys.argv[2])), summarize(report), "report")
print("  cheapest:", report["cheapest"]["name"], report["cheapest"]["monthly"], report["currency"] + "/month")
PY
grep -q "Budget review: support-chat" "$WORK/plan.txt" || fail "text report: $(head -5 "$WORK/plan.txt")"
grep -q "gpu-l4-1x llama-8b Q4_K vs api-small: 0.708x current traffic" "$WORK/plan.txt" || fail "break-even line: $(grep break -A6 "$WORK/plan.txt")"
grep -q "not eligible: no point meets 15 tokens/s" "$WORK/plan.txt" || fail "F16 on an L4 should miss the SLO: $(cat "$WORK/plan.txt")"

echo "▸ Use It: a real GPU price from the Azure Retail Prices API becomes a dated, sourced entry"
if curl -fsSG --max-time 20 https://prices.azure.com/api/retail/prices --data-urlencode "\$filter=armSkuName eq 'Standard_NC24ads_A100_v4' and armRegionName eq 'eastus' and priceType eq 'Consumption'" -o "$WORK/azure.json" 2>/dev/null; then
  python3 - "$WORK/azure.json" "$WORK/azure-prices.json" <<'PY' || fail "Azure price: $(head -c 600 "$WORK/azure.json")"
import datetime, json, sys
items = json.load(open(sys.argv[1]))["Items"]
linux = [i for i in items if "Windows" not in i["productName"] and "Spot" not in i["skuName"] and "Low Priority" not in i["skuName"]]
assert linux and linux[0]["retailPrice"] > 0, items
item = linux[0]
entry = {"name": "azure-nc24ads-a100-v4", "hourly_price": item["retailPrice"], "accelerators": 1, "accelerator": "A100", "as_of": datetime.date.today().isoformat(), "source": "https://prices.azure.com/api/retail/prices armSkuName Standard_NC24ads_A100_v4, eastus, Linux, Consumption"}
json.dump({"currency": item["currencyCode"], "instances": [entry]}, open(sys.argv[2], "w"), indent=2)
print(f"  Standard_NC24ads_A100_v4 in eastus, Linux on-demand: {item['retailPrice']} {item['currencyCode']}/{item['unitOfMeasure']}")
PY
  python3 code/main.py prices "$WORK/azure-prices.json" > "$WORK/azure-prices.txt" 2>&1 || fail "tokencost rejected the live price: $(cat "$WORK/azure-prices.txt")"
  grep -q "azure-nc24ads-a100-v4" "$WORK/azure-prices.txt" || fail "live price missing: $(cat "$WORK/azure-prices.txt")"
else
  echo "  (prices.azure.com not reachable; live price input skipped)"
fi

if command -v docker >/dev/null; then
  echo "▸ Use It: download the pinned model ($MODEL_REPO@${MODEL_REVISION:0:8}) and check its digest"
  mkdir -p "$MODELS"
  if ! echo "$MODEL_SHA256  $MODELS/$MODEL_FILE" | sha256sum -c - >/dev/null 2>&1; then
    curl -fsSL --retry 3 -o "$MODELS/$MODEL_FILE" "https://huggingface.co/$MODEL_REPO/resolve/$MODEL_REVISION/$MODEL_FILE" || fail "model download failed"
    echo "$MODEL_SHA256  $MODELS/$MODEL_FILE" | sha256sum -c - > "$WORK/sha.txt" 2>&1 || fail "model digest mismatch: $(cat "$WORK/sha.txt")"
  fi
  echo "  $(du -h "$MODELS/$MODEL_FILE" | cut -f1) $MODEL_FILE, sha256 ok"

  echo "▸ Use It: llama.cpp server on CPU ($LLAMA_IMAGE), 4 slots, --metrics"
  PORT="$(free_port)"
  docker run -d --name "$SERVER" -p "127.0.0.1:$PORT:8080" -v "$MODELS:/models:ro" "$LLAMA_IMAGE" \
    -m "/models/$MODEL_FILE" -c 4096 --parallel 4 --metrics -t "$(nproc)" > /dev/null
  READY=0
  for _ in $(seq 1 180); do
    if curl -sf "http://127.0.0.1:$PORT/health" > /dev/null 2>&1; then READY=1; break; fi
    sleep 1
  done
  [[ "$READY" == "1" ]] || { docker logs "$SERVER" > "$WORK/server.log" 2>&1; tail -30 "$WORK/server.log"; fail "llama.cpp server not healthy within 180s"; }

  echo "▸ tokencost measure at concurrency 1 and 4 (prompt cache off, exactly 96 output tokens)"
  python3 code/main.py measure --url "http://127.0.0.1:$PORT" --instance cpu-4vcpu --model SmolLM2-135M-Instruct --quantization Q8_0 \
    --concurrency 1,4 --requests 8 --max-tokens 96 --ignore-eos --no-prompt-cache --metrics-url "http://127.0.0.1:$PORT/metrics" \
    -o "$WORK/cpu.json" 2> "$WORK/measure.err" || fail "measure: $(cat "$WORK/measure.err")"
  sed 's/^/  /' "$WORK/measure.err"
  python3 - "$WORK/cpu.json" <<'PY' || fail "measurement: $(cat "$WORK/cpu.json")"
import json, sys
data = json.load(open(sys.argv[1]))
one, four = data["points"]
assert (one["concurrency"], four["concurrency"]) == (1, 4), data["points"]
for point in (one, four):
    assert point["requests_per_second"] > 0 and point["output_tokens"] == 96 and point["input_tokens"] > 10, point
    assert point["prefill_tokens_per_second"] and point["prefill_tokens_per_second"] > 0, point
metrics = data["server_metrics"]
assert metrics["delta"] == metrics["client_completion_tokens"] == (1 + 8 + 8) * 96, metrics
gain = four["requests_per_second"] / one["requests_per_second"]
print(f"  batching x{gain:.2f} throughput, per-request speed {one['stream_tokens_per_second']:.0f} -> {four['stream_tokens_per_second']:.0f} tok/s; server counted {metrics['delta']:.0f} generated tokens, the client {metrics['client_completion_tokens']}")
PY
  curl -s "http://127.0.0.1:$PORT/metrics" > "$WORK/metrics.txt"
  grep -q '^llamacpp:n_busy_slots_per_decode ' "$WORK/metrics.txt" || fail "no n_busy_slots_per_decode in /metrics: $(head -40 "$WORK/metrics.txt")"
  grep -q '^llamacpp:prompt_tokens_total ' "$WORK/metrics.txt" || fail "no prompt_tokens_total in /metrics"

  echo "▸ tokencost plan on the measured points: cost per token falls as utilisation rises, break-even moves as the model says"
  python3 code/main.py plan --prices "$F/prices.json" --today "$TODAY" --scenario "$F/scenario-cpu.json" --measurements "$WORK/cpu.json" > "$WORK/cpu-plan.txt"
  sed 's/^/  /' "$WORK/cpu-plan.txt"
  python3 code/main.py plan --prices "$F/prices.json" --today "$TODAY" --scenario "$F/scenario-cpu.json" --measurements "$WORK/cpu.json" --json -o "$WORK/cpu-plan.json"
  python3 - "$F/prices.json" "$WORK/cheaper-api.json" <<'PY'
import json, sys
data = json.load(open(sys.argv[1]))
for api in data["apis"]:
    for key in ("input_per_mtok", "output_per_mtok", "cached_input_per_mtok"):
        api[key] = round(api[key] * 0.8, 6)
json.dump(data, open(sys.argv[2], "w"))
PY
  python3 code/main.py plan --prices "$WORK/cheaper-api.json" --today "$TODAY" --scenario "$F/scenario-cpu.json" --measurements "$WORK/cpu.json" --json -o "$WORK/cpu-plan-cheaper.json"
  python3 - "$WORK/cpu.json" "$WORK/cpu-plan.json" "$WORK/cpu-plan-cheaper.json" "$F/prices.json" "$F/scenario-cpu.json" <<'PY' || fail "cost model claims on the measured points; plan: $(cat "$WORK/cpu-plan.txt")"
import datetime, json, sys
sys.path.insert(0, "code")
import main

measured, plan, cheaper, prices, scenario_file = sys.argv[1:6]
plan, cheaper = json.load(open(plan)), json.load(open(cheaper))
book = main.load_prices(json.load(open(prices)), datetime.date(2026, 11, 1))
scenario = main.scenario_from(json.load(open(scenario_file)))
option = main.option_from(json.load(open(measured)), measured)
instance, api = book.instances["cpu-4vcpu"], book.apis["api-small"]
infinity = float("inf")


def break_even(point):
    scale = point["break_even"][0]["scale"]
    return infinity if scale is None else scale


points = {p["concurrency"]: p for p in plan["self_hosted"][0]["points"]}
for concurrency, point in points.items():
    costs = [s["per_mtok_blended"] for s in point["sweep"]]
    utils = [s["utilization"] for s in point["sweep"]]
    assert all(a >= b for a, b in zip(costs, costs[1:])) and costs[-1] < costs[0], (concurrency, costs)
    assert all(a <= b for a, b in zip(utils, utils[1:])) and utils[-1] > utils[0], (concurrency, utils)
one, four = points[1], points[4]
faster, slower = (four, one) if four["requests_per_second_per_replica"] >= one["requests_per_second_per_replica"] else (one, four)
assert faster["sweep"][-1]["per_mtok_blended"] <= slower["sweep"][-1]["per_mtok_blended"], (faster["sweep"], slower["sweep"])
assert break_even(faster) <= break_even(slower), (faster["break_even"], slower["break_even"])
cheaper_points = {p["concurrency"]: p for p in cheaper["self_hosted"][0]["points"]}
for concurrency in points:
    assert break_even(cheaper_points[concurrency]) >= break_even(points[concurrency]), (concurrency, points[concurrency]["break_even"], cheaper_points[concurrency]["break_even"])
for point in option.points:
    scale = break_even(points[point.concurrency])
    if scale != infinity:
        cheaper = lambda s, p=point: main.self_hosted(instance, p, scenario, s, bounded=False)["monthly"] <= main.api_cost(api, scenario, s)["monthly"] + 1e-6
        assert all(cheaper(scale * (1 + i / 1000)) for i in range(3000)), ("self-hosting should stay cheaper from the break-even up", point.concurrency, scale)
        assert any(not cheaper(scale * i / 2000) for i in range(1, 2000)), ("self-hosting should be dearer somewhere below the break-even", point.concurrency, scale)
def shown(point):
    scale = break_even(point)
    return "never" if scale == infinity else f"{scale:g}x"


for concurrency, point in sorted(points.items()):
    sweep = point["sweep"]
    print(f"  c={concurrency}: {point['requests_per_second_per_replica']:.2f} req/s per replica; cost per Mtok {sweep[0]['per_mtok_blended']:.4f} at {sweep[0]['utilization']:.0%} -> {sweep[-1]['per_mtok_blended']:.4f} at {sweep[-1]['utilization']:.0%}; break-even vs api-small {shown(point)}, {shown(cheaper_points[concurrency])} if the API is 20% cheaper")
PY
  docker rm -f "$SERVER" > /dev/null
else
  echo "  (docker not installed; the llama.cpp measurement skipped)"
fi

kubectl config get-contexts -o name > "$WORK/contexts.txt" 2>/dev/null || true
if command -v kubectl >/dev/null && grep -qx "$CTX" "$WORK/contexts.txt"; then
  echo "▸ Use It: GPU showback on kind: 4 fake GPUs, pods requesting 2 and 1, a third pod Pending"
  NODE="$(k get nodes -o jsonpath='{.items[0].metadata.name}')"
  k create namespace "$NS" > /dev/null
  NS_CREATED=1
  k patch node "$NODE" --subresource=status --type=json -p "[{\"op\":\"add\",\"path\":\"/status/capacity/$RESOURCE_PATH\",\"value\":\"4\"}]" > /dev/null
  PATCHED=1
  pod() {
    cat <<EOF
apiVersion: v1
kind: Pod
metadata:
  name: $1
  namespace: $NS
  labels:
    team: $2
spec:
  terminationGracePeriodSeconds: 0
  containers:
    - name: main
      image: registry.k8s.io/pause:3.10
      resources:
        limits:
          $RESOURCE: "$3"
EOF
  }
  { pod llm-0 assistant 2; echo "---"; pod embed-0 search 1; } | k apply -f - > /dev/null
  k -n "$NS" wait --for=condition=Ready pod/llm-0 pod/embed-0 --timeout=120s > "$WORK/wait.txt" 2>&1 || { k -n "$NS" describe pods > "$WORK/describe.txt" 2>&1; tail -30 "$WORK/describe.txt"; fail "GPU pods did not become Ready: $(cat "$WORK/wait.txt")"; }
  pod llm-1 assistant 2 | k apply -f - > /dev/null
  k -n "$NS" wait --for=condition=PodScheduled=false pod/llm-1 --timeout=60s > "$WORK/wait-pending.txt" 2>&1 || fail "llm-1 should be unschedulable: $(cat "$WORK/wait-pending.txt")"
  k -n "$NS" get pod llm-1 -o jsonpath='{.status.conditions[?(@.type=="PodScheduled")].message}' > "$WORK/pending.txt"
  grep -q "Insufficient $RESOURCE" "$WORK/pending.txt" || fail "llm-1 Pending for another reason: $(cat "$WORK/pending.txt")"
  echo "  llm-1: $(cat "$WORK/pending.txt")"
  k get nodes -o json > "$WORK/nodes.json"
  k get pods -A -o json > "$WORK/pods.json"
  python3 code/main.py allocate --prices "$F/prices.json" --today "$TODAY" --nodes "$WORK/nodes.json" --pods "$WORK/pods.json" \
    --resource "$RESOURCE" --instance "$NODE=gpu-l4-4x" --by label:team --hours 24 --focus "$WORK/focus.csv" --period-start 2026-10-31T00:00:00Z > "$WORK/alloc.txt"
  sed 's/^/  /' "$WORK/alloc.txt"
  python3 code/main.py allocate --prices "$F/prices.json" --today "$TODAY" --nodes "$WORK/nodes.json" --pods "$WORK/pods.json" \
    --resource "$RESOURCE" --instance "$NODE=gpu-l4-4x" --by label:team --hours 24 --json > "$WORK/alloc.json"
  python3 - "$WORK/alloc.json" "$WORK/focus.csv" <<'PY' || fail "showback: $(cat "$WORK/alloc.txt")"
import csv, json, sys
report = json.load(open(sys.argv[1]))
rows = {row["name"]: row for row in report["allocations"]}
assert {name: row["gpus"] for name, row in rows.items()} == {"assistant": 2, "search": 1, "__idle__": 1}, rows
assert abs(rows["assistant"]["cost"] - 2 * 4.60 / 4 * 24) < 1e-6 and abs(rows["__idle__"]["cost"] - 4.60 / 4 * 24) < 1e-6, rows
assert abs(report["total"] - 4.60 * 24) < 1e-6, report["total"]
focus = list(csv.DictReader(open(sys.argv[2])))
assert [row["AllocatedResourceId"] for row in focus] == ["assistant", "search", ""], focus
assert abs(sum(float(row["BilledCost"]) for row in focus) - 110.4) < 1e-3 and focus[0]["ConsumedUnit"] == "GPU-Hours", focus
print("  the Pending pod costs nothing; 2 + 1 GPUs charged by request, 1 idle; FOCUS rows sum to the node's 110.40")
PY
else
  echo "  (kind context $CTX not found; GPU showback on a cluster skipped)"
fi

echo "SMOKE OK: ai-infra/ai-finops"
