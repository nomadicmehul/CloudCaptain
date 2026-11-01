#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."

fail() { echo "SMOKE FAIL: $*" >&2; exit 1; }
WORK="$(mktemp -d)"
CONTAINER="cc-smoke-llm-serving-$$"
IMAGE="${LLAMA_CPP_IMAGE:-ghcr.io/ggml-org/llama.cpp:server-b11706}"
IMAGE_DIGEST="sha256:f41d47eee678c02656eefc9ed8cce76cab584aa7032805eeef0e6639fcfda485"
MODEL_REPO="bartowski/SmolLM2-135M-Instruct-GGUF"
MODEL_REVISION="09816acd5d99df7be770d85ea30822623dab342c"
MODEL_FILE="SmolLM2-135M-Instruct-Q8_0.gguf"
MODEL_SHA256="5a1395716f7913741cc51d98581b9b1228d80987a9f7d3664106742eb06bba83"
MODEL_DIR="${CC_MODEL_CACHE:-$WORK/models}"
MAX_TOKENS=64
cleanup() {
  if command -v docker >/dev/null; then docker rm -f "$CONTAINER" >/dev/null 2>&1 || true; fi
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
sha256_of() {
  if command -v sha256sum >/dev/null; then sha256sum "$1" | cut -d' ' -f1; else shasum -a 256 "$1" | cut -d' ' -f1; fi
}
sim() {
  python3 code/main.py simulate --hardware cpu-4vcpu --model smollm2-135m-q8_0 --kv-tokens 4096 --max-num-seqs 4 "$@" --json
}

echo "▸ servesim capacity: KV bytes per token, blocks, and a model that does not fit"
python3 code/main.py capacity --hardware h100-sxm-80gb --model llama-3.1-8b --json > "$WORK/cap.json"
[[ "$(field "$WORK/cap.json" kv_bytes_per_token)" == "131072" ]] || fail "Llama 3.1 8B KV per token should be 131072 bytes: $(cat "$WORK/cap.json")"
[[ "$(field "$WORK/cap.json" kv_tokens)" == "447952" ]] || fail "H100 KV tokens: $(cat "$WORK/cap.json")"
CODE=0
python3 code/main.py capacity --model llama-3.1-70b > /dev/null 2> "$WORK/70b.err" || CODE=$?
[[ "$CODE" == "1" ]] && grep -q "does not fit" "$WORK/70b.err" || fail "70B in BF16 on one H100 should not fit (exit $CODE): $(cat "$WORK/70b.err")"
echo "  128 KiB per token, 447,952 tokens on an H100; 70B: $(cat "$WORK/70b.err")"

echo "▸ servesim sweep on the H100 preset: throughput rises with concurrency, TPOT gets worse"
python3 code/main.py sweep --requests 128 --concurrency 1,8,64 --json > "$WORK/sweep.json"
python3 - "$WORK/sweep.json" <<'PY' || fail "sweep direction: $(cat "$WORK/sweep.json")"
import json, sys
rows = json.load(open(sys.argv[1]))
thr = [r["throughput"]["output_tokens_per_s"] for r in rows]
tpot = [r["tpot_ms"]["p50"] for r in rows]
assert thr[0] < thr[1] < thr[2], thr
assert tpot[0] <= tpot[1] < tpot[2], tpot
assert thr[1] / thr[0] > 5, ("batching 8 sequences should be nearly free on a memory-bound decode", thr)
print("  out tok/s", [round(t) for t in thr], " tpot p50 ms", [round(t, 1) for t in tpot])
PY

echo "▸ servesim on the burst trace: prefix caching removes preemptions on a full L4 cache"
python3 code/main.py simulate --hardware l4-24gb --trace code/fixtures/burst.jsonl --json > "$WORK/burst.json"
python3 code/main.py simulate --hardware l4-24gb --trace code/fixtures/burst.jsonl --prefix-caching --json > "$WORK/burst-pc.json"
python3 - "$WORK/burst.json" "$WORK/burst-pc.json" <<'PY' || fail "burst trace"
import json, sys
off, on = (json.load(open(path)) for path in sys.argv[1:])
assert off["completed"] == on["completed"] == 48, (off["completed"], on["completed"])
assert off["engine"]["preemptions"] > 0 and off["engine"]["kv_peak_pct"] == 100.0, off["engine"]
assert on["engine"]["preemptions"] == 0 and on["engine"]["prefix_hit_pct"] > 50, on["engine"]
assert on["ttft_ms"]["p50"] < off["ttft_ms"]["p50"] / 2, (on["ttft_ms"], off["ttft_ms"])
print("  without: %d preemptions, ttft p50 %.0f ms; with: %.0f%% prefix hits, ttft p50 %.0f ms" % (off["engine"]["preemptions"], off["ttft_ms"]["p50"], on["engine"]["prefix_hit_pct"], on["ttft_ms"]["p50"]))
PY

echo "▸ loadgen against a server that is not there exits 1 with the docker run hint"
CODE=0
python3 code/main.py loadgen --url http://127.0.0.1:9 --requests 1 > /dev/null 2> "$WORK/down.err" || CODE=$?
[[ "$CODE" == "1" ]] && grep -q "docker run" "$WORK/down.err" || fail "unreachable server (exit $CODE): $(cat "$WORK/down.err")"

if ! command -v docker >/dev/null || ! docker info >/dev/null 2>&1; then
  echo "  (docker not available; llama.cpp server checks skipped)"
  echo "SMOKE OK: ai-infra/llm-serving (servesim only)"
  exit 0
fi

echo "▸ download $MODEL_REPO@$MODEL_REVISION/$MODEL_FILE and check its sha256"
mkdir -p "$MODEL_DIR"
if [[ ! -f "$MODEL_DIR/$MODEL_FILE" || "$(sha256_of "$MODEL_DIR/$MODEL_FILE")" != "$MODEL_SHA256" ]]; then
  curl -fsSL --retry 3 -o "$MODEL_DIR/$MODEL_FILE.part" "https://huggingface.co/$MODEL_REPO/resolve/$MODEL_REVISION/$MODEL_FILE" || fail "model download failed"
  mv "$MODEL_DIR/$MODEL_FILE.part" "$MODEL_DIR/$MODEL_FILE"
fi
GOT="$(sha256_of "$MODEL_DIR/$MODEL_FILE")"
[[ "$GOT" == "$MODEL_SHA256" ]] || fail "sha256 mismatch for $MODEL_FILE: got $GOT, want $MODEL_SHA256"
echo "  $(wc -c < "$MODEL_DIR/$MODEL_FILE") bytes, sha256 $GOT"

echo "▸ Use It: llama-server on CPU with --parallel 4 --ctx-size 4096 -cb --metrics"
PORT="$(python3 -c 'import socket; s=socket.socket(); s.bind(("127.0.0.1", 0)); print(s.getsockname()[1]); s.close()')"
NPROC="$(nproc 2>/dev/null || echo 4)"
THREADS=$(( NPROC > 4 ? 4 : NPROC ))
docker pull -q "$IMAGE" > /dev/null || fail "docker pull $IMAGE"
DIGESTS="$(docker image inspect --format '{{json .RepoDigests}}' "$IMAGE")"
if [[ -z "${LLAMA_CPP_IMAGE:-}" && "$DIGESTS" != *"$IMAGE_DIGEST"* ]]; then fail "$IMAGE no longer points at the pinned index $IMAGE_DIGEST: $DIGESTS"; fi
echo "  $IMAGE ($IMAGE_DIGEST), $THREADS threads"
docker run -d --name "$CONTAINER" -p "127.0.0.1:$PORT:8080" -v "$MODEL_DIR:/models:ro" "$IMAGE" \
  -m "/models/$MODEL_FILE" --alias smollm2-135m --parallel 4 --ctx-size 4096 -cb --metrics --threads "$THREADS" > /dev/null || fail "docker run $IMAGE"
BASE="http://127.0.0.1:$PORT"
READY=0
for _ in $(seq 1 120); do
  if curl -sf "$BASE/health" > "$WORK/health.json" 2>/dev/null; then READY=1; break; fi
  [[ "$(docker inspect -f '{{.State.Running}}' "$CONTAINER" 2>/dev/null)" == "true" ]] || break
  sleep 1
done
docker logs "$CONTAINER" > "$WORK/server.log" 2>&1 || true
[[ "$READY" == "1" ]] || fail "llama-server not healthy within 120s: $(tail -30 "$WORK/server.log")"
grep -E "n_slots|n_ctx_slot" "$WORK/server.log" > "$WORK/slots.log" || true
sed 's/^/  /' "$WORK/slots.log"

echo "▸ /v1/models, /slots: the alias, four slots and the per-slot context"
curl -sf "$BASE/v1/models" > "$WORK/models.json" || fail "/v1/models"
[[ "$(field "$WORK/models.json" data 0 id)" == "smollm2-135m" ]] || fail "model id should be the alias: $(cat "$WORK/models.json")"
curl -sf "$BASE/slots" > "$WORK/slots.json" || fail "/slots"
python3 - "$WORK/slots.json" <<'PY' || fail "/slots: $(cat "$WORK/slots.json")"
import json, sys
slots = json.load(open(sys.argv[1]))
assert len(slots) == 4, len(slots)
contexts = [slot["n_ctx"] for slot in slots]
assert all(0 < n <= 4096 for n in contexts), contexts
print("  4 slots, n_ctx per slot", contexts)
PY

echo "▸ a raw streamed chat completion: chunks, finish_reason, usage, timings, [DONE]"
curl -sfN "$BASE/v1/chat/completions" -H 'content-type: application/json' \
  -d '{"model":"smollm2-135m","messages":[{"role":"user","content":"What is a KV cache?"}],"max_tokens":8,"ignore_eos":true,"temperature":0,"stream":true,"stream_options":{"include_usage":true}}' > "$WORK/stream.txt" || fail "streaming request"
python3 - "$WORK/stream.txt" <<'PY' || fail "stream format: $(cat "$WORK/stream.txt")"
import json, sys
lines = [line[5:].strip() for line in open(sys.argv[1]) if line.startswith("data:")]
assert lines[-1] == "[DONE]", lines[-1]
events = [json.loads(line) for line in lines[:-1]]
assert all(e["object"] == "chat.completion.chunk" for e in events)
content = [e for e in events if any((c.get("delta") or {}).get("content") for c in e["choices"])]
finish = [c["finish_reason"] for e in events for c in e["choices"] if c.get("finish_reason")]
usage = [e for e in events if e.get("usage")]
assert content and finish == ["length"] and usage, (len(content), finish, len(usage))
assert usage[-1]["usage"]["completion_tokens"] == 8, usage[-1]["usage"]
timings = usage[-1].get("timings") or {}
assert timings.get("predicted_n", 0) >= 1 and "predicted_per_token_ms" in timings, timings
print("  %d content chunks, finish_reason length, usage %s, server TPOT %.1f ms" % (len(content), usage[-1]["usage"], timings["predicted_per_token_ms"]))
PY

echo "▸ /metrics exposes llama.cpp's batching counters"
curl -sf "$BASE/metrics" > "$WORK/metrics.txt" || fail "/metrics (is --metrics set?)"
for name in llamacpp:n_decode_total llamacpp:n_busy_slots_per_decode llamacpp:requests_processing llamacpp:requests_deferred llamacpp:tokens_predicted_total llamacpp:prompt_tokens_total; do
  grep -q "^$name " "$WORK/metrics.txt" || fail "$name missing from /metrics: $(grep -v '^#' "$WORK/metrics.txt")"
done

echo "▸ loadgen at concurrency 1, 4 and 8 (64 tokens each, ignore_eos)"
python3 code/main.py loadgen --url "$BASE" --concurrency 1 --requests 2 --max-tokens 8 --ignore-eos > /dev/null || fail "warm-up requests"
for c in 1 4 8; do
  n=$(( c == 1 ? 8 : 16 ))
  python3 code/main.py loadgen --url "$BASE" --concurrency "$c" --requests "$n" --max-tokens "$MAX_TOKENS" --ignore-eos --json > "$WORK/c$c.json" || fail "loadgen concurrency $c: $(cat "$WORK/c$c.json")"
done
python3 - "$WORK/c1.json" "$WORK/c4.json" "$WORK/c8.json" "$MAX_TOKENS" <<'PY' || fail "loadgen measurements: $(cat "$WORK/c1.json" "$WORK/c4.json" "$WORK/c8.json")"
import json, sys
c1, c4, c8 = (json.load(open(path)) for path in sys.argv[1:4])
max_tokens = int(sys.argv[4])
for name, r in (("c1", c1), ("c4", c4), ("c8", c8)):
    assert r["completed"] == r["requests"] and not r["errors"], (name, r["errors"])
    assert r["usage_reported"] and r["output_tokens"] == r["requests"] * max_tokens, (name, r["output_tokens"])
    assert r["ttft_ms"]["p50"] > 0 and r["throughput"]["output_tokens_per_s"] > 0, name
    assert r["server"] and r["server"]["implementation"] == "llama.cpp", (name, r["server"])
t1, t4, t8 = (r["throughput"]["output_tokens_per_s"] for r in (c1, c4, c8))
p1, p4 = c1["tpot_ms"]["p50"], c4["tpot_ms"]["p50"]
b1, b4 = c1["server"]["mean_batch_per_decode"], c4["server"]["mean_batch_per_decode"]
print("  %-11s %10s %10s %10s %10s %12s %12s" % ("concurrency", "out tok/s", "ttft p50", "tpot p50", "itl p95", "batch/decode", "max waiting"))
for name, r in (("1", c1), ("4", c4), ("8", c8)):
    s = r["server"]
    print("  %-11s %10.1f %10.1f %10.1f %10.1f %12.2f %12g" % (name, r["throughput"]["output_tokens_per_s"], r["ttft_ms"]["p50"], r["tpot_ms"]["p50"], r["itl_ms"]["p95"], s.get("mean_batch_per_decode", 0), s.get("max_waiting", 0)))
assert t4 > 1.05 * t1, ("throughput at concurrency 4 should beat concurrency 1", t1, t4)
assert p4 > p1, ("per-request TPOT should get worse with a batch of 4", p1, p4)
assert b1 < 1.3 and b4 > 1.5, ("llama.cpp should batch 1 vs about 4 slots per decode", b1, b4)
assert c8["server"].get("max_waiting", 0) >= 1, ("8 clients on 4 slots must defer requests", c8["server"])
assert c8["ttft_ms"]["p50"] > c4["ttft_ms"]["p50"], ("waiting for a slot shows up in TTFT", c4["ttft_ms"], c8["ttft_ms"])
print("  throughput x%.2f and TPOT x%.2f from 1 to 4; at 8 the extra clients wait (max deferred %g)" % (t4 / t1, p4 / p1, c8["server"]["max_waiting"]))
PY

echo "▸ servesim on the same shape (4 slots, 4096-token cache, the measured prompt length) points the same way"
PROMPT="$(python3 -c 'import json,sys; r=json.load(open(sys.argv[1])); print(max(1, round(r["prompt_tokens"] / r["completed"])))' "$WORK/c1.json")"
for c in 1 4 8; do
  n=$(( c == 1 ? 8 : 16 ))
  sim --prompt-tokens "$PROMPT" --output-tokens "$MAX_TOKENS" --requests "$n" --concurrency "$c" > "$WORK/sim$c.json" || fail "servesim concurrency $c"
done
python3 - "$WORK/sim1.json" "$WORK/sim4.json" "$WORK/sim8.json" "$PROMPT" <<'PY' || fail "servesim direction on the llama.cpp shape"
import json, sys
s1, s4, s8 = (json.load(open(path)) for path in sys.argv[1:4])
t1, t4, t8 = (r["throughput"]["output_tokens_per_s"] for r in (s1, s4, s8))
assert t4 > t1 and s4["tpot_ms"]["p50"] > s1["tpot_ms"]["p50"], (t1, t4, s1["tpot_ms"], s4["tpot_ms"])
assert s8["queue_ms"]["p95"] > 0 and s8["ttft_ms"]["p50"] > s4["ttft_ms"]["p50"], (s8["queue_ms"], s4["ttft_ms"], s8["ttft_ms"])
assert abs(t8 - t4) < 0.05 * t4 and s4["engine"]["mean_batch_seqs"] > 3.5, (t4, t8, s4["engine"]["mean_batch_seqs"])
print("  prompt %s tokens: simulated throughput x%.2f and TPOT x%.2f from 1 to 4; at 8 queue p95 %.0f ms" % (sys.argv[4], t4 / t1, s4["tpot_ms"]["p50"] / s1["tpot_ms"]["p50"], s8["queue_ms"]["p95"]))
PY

echo "SMOKE OK: ai-infra/llm-serving"
