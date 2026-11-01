#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."

fail() { echo "SMOKE FAIL: $*" >&2; exit 1; }
WORK="$(mktemp -d)"
CONTAINER="cc-smoke-ai-gateway-$$"
IMAGE="${LLAMA_CPP_IMAGE:-ghcr.io/ggml-org/llama.cpp:server-b11706}"
IMAGE_DIGEST="sha256:f41d47eee678c02656eefc9ed8cce76cab584aa7032805eeef0e6639fcfda485"
MODEL_REPO="bartowski/SmolLM2-135M-Instruct-GGUF"
MODEL_REVISION="09816acd5d99df7be770d85ea30822623dab342c"
MODEL_FILE="SmolLM2-135M-Instruct-Q8_0.gguf"
MODEL_SHA256="5a1395716f7913741cc51d98581b9b1228d80987a9f7d3664106742eb06bba83"
MODEL_DIR="${CC_MODEL_CACHE:-$WORK/models}"
LITELLM_VERSION="1.106.6"
LITELLM_MASTER_KEY="sk-cc-litellm-master"
export FAKE_API_KEY="sk-fake-upstream"
F="code/fixtures"
PIDS=()
GATEWAY_PID=""

cleanup() {
  set +e
  for pid in ${PIDS[@]+"${PIDS[@]}"}; do kill "$pid" 2>/dev/null; wait "$pid" 2>/dev/null; done
  if command -v docker >/dev/null; then docker rm -f "$CONTAINER" >/dev/null 2>&1; fi
  rm -rf "$WORK"
}
trap cleanup EXIT

free_port() { python3 -c 'import socket; s=socket.socket(); s.bind(("127.0.0.1", 0)); print(s.getsockname()[1]); s.close()'; }
wait_line() { for _ in $(seq 1 100); do grep -q "$2" "$1" 2>/dev/null && return 0; sleep 0.1; done; fail "no '$2' in $1: $(cat "$1" 2>/dev/null)"; }
sha256_of() {
  if command -v sha256sum >/dev/null; then sha256sum "$1" | cut -d' ' -f1; else shasum -a 256 "$1" | cut -d' ' -f1; fi
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
start_gateway() {
  local log="$1" llama="$2"
  python3 code/main.py serve --config "$F/gateway.json" --backend "fake=$FAKE_URL" --backend "llama=$llama" --port 0 --log "$log" > "$log.out" 2> "$log.err" &
  GATEWAY_PID="$!"
  PIDS+=("$GATEWAY_PID")
  wait_line "$log.out" "url="
  GATEWAY_URL="$(sed -n 's/.*url=\([^ ]*\).*/\1/p' "$log.out")"
}
stop_gateway() {
  if [[ -n "$GATEWAY_PID" ]]; then kill "$GATEWAY_PID" 2>/dev/null || true; wait "$GATEWAY_PID" 2>/dev/null || true; GATEWAY_PID=""; fi
}

HAVE_DOCKER=""
if command -v docker >/dev/null && docker info >/dev/null 2>&1; then HAVE_DOCKER=1; fi
HAVE_PY310=""
if python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)'; then HAVE_PY310=1; fi

if [[ -n "$HAVE_PY310" ]]; then
  echo "▸ background: pip install litellm[proxy]==$LITELLM_VERSION into a venv"
  ( set +e; python3 -m venv "$WORK/venv" && "$WORK/venv/bin/pip" install -q --disable-pip-version-check "litellm[proxy]==$LITELLM_VERSION"; echo "$?" > "$WORK/pip.exit" ) > "$WORK/pip.log" 2>&1 &
  PIP_PID="$!"
  PIDS+=("$PIP_PID")
fi
if [[ -n "$HAVE_DOCKER" ]]; then
  echo "▸ background: docker pull $IMAGE and download $MODEL_REPO@$MODEL_REVISION/$MODEL_FILE"
  ( set +e; docker pull -q "$IMAGE"; echo "$?" > "$WORK/pull.exit" ) > "$WORK/pull.log" 2>&1 &
  PULL_PID="$!"
  PIDS+=("$PULL_PID")
  mkdir -p "$MODEL_DIR"
  ( set +e; if [[ ! -f "$MODEL_DIR/$MODEL_FILE" || "$(sha256_of "$MODEL_DIR/$MODEL_FILE")" != "$MODEL_SHA256" ]]; then
      curl -fsSL --retry 3 -o "$MODEL_DIR/$MODEL_FILE.part" "https://huggingface.co/$MODEL_REPO/resolve/$MODEL_REVISION/$MODEL_FILE" && mv "$MODEL_DIR/$MODEL_FILE.part" "$MODEL_DIR/$MODEL_FILE"
    fi; echo "$?" > "$WORK/model.exit" ) > "$WORK/model.log" 2>&1 &
  MODEL_PID="$!"
  PIDS+=("$MODEL_PID")
fi

cat > "$WORK/probe.py" <<'PY'
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request

mode, gateway, fake, llama, log, metrics_url = sys.argv[1:7]
real = mode == "llama"
TEAM, TIGHT = "sk-cc-team-a", "sk-cc-tight"
opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def check(condition, what, detail):
    if not condition:
        raise SystemExit(f"SMOKE FAIL: {mode}: {what}: {detail}")


def call(base, path, body=None, key=TEAM, headers=None):
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(base + path, data=data, method="POST" if data is not None else "GET", headers=dict({"Authorization": f"Bearer {key}", "Content-Type": "application/json"}, **(headers or {})))
    try:
        with opener.open(request, timeout=120) as response:
            return response.status, {k.lower(): v for k, v in response.getheaders()}, response.read().decode()
    except urllib.error.HTTPError as err:
        return err.code, {k.lower(): v for k, v in err.headers.items()}, err.read().decode()


def complete(body, key=TEAM, headers=None):
    return call(gateway, "/v1/chat/completions", body, key, headers)


def stream(body):
    request = urllib.request.Request(gateway + "/v1/chat/completions", data=json.dumps(body).encode(), headers={"Authorization": f"Bearer {TEAM}", "Content-Type": "application/json"})
    with opener.open(request, timeout=120) as response:
        return response.headers.get("Content-Type", ""), response.read().decode()


def chat(model, text="Name one Linux signal.", **extra):
    return dict({"model": model, "messages": [{"role": "user", "content": text}]}, **extra)


def content(body):
    return json.loads(body)["choices"][0]["message"]["content"] or ""


def stats(base):
    return json.loads(call(base, "/fake/stats")[2])


def records():
    with open(log, encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]


def predicted():
    with opener.open(metrics_url, timeout=10) as response:
        for line in response.read().decode().splitlines():
            if line.startswith("llamacpp:tokens_predicted_total"):
                return float(line.split()[-1])
    raise SystemExit(f"SMOKE FAIL: {mode}: no llamacpp:tokens_predicted_total in {metrics_url}")


def settle_metrics(start, wanted):
    value = predicted()
    for _ in range(50):
        if value - start >= wanted:
            break
        time.sleep(0.1)
        value = predicted()
    return value


call(fake, "/fake/reset", {})
status, headers, body = complete(chat("local", max_tokens=16))
check(status == 200 and headers.get("x-tokengate-deployment") == "llama/smollm2", "model local routes to llama/smollm2", (status, headers, body[:300]))
local_text = content(body)
check(not local_text.startswith("[fake]"), "local must not be answered by the fake", local_text)
if real:
    usage = json.loads(body).get("usage") or {}
    check(usage.get("completion_tokens", 0) > 0 and usage.get("prompt_tokens", 0) > 0, "llama.cpp reports usage", body[:400])
status, headers, body = complete(chat("fake", max_tokens=4))
check(status == 200 and content(body) == "[fake] w1 w2 w3" and headers.get("x-tokengate-deployment") == "fake/fake-small", "model fake routes to the fake", (status, headers, body[:300]))
seen = stats(fake)
check(seen["models"] == {"fake-small": 1}, "the gateway rewrites model to the deployment's name", seen["models"])
check(seen["last_authorization"] == "Bearer " + os.environ["FAKE_API_KEY"], "the backend gets the gateway's credential, not the client's key", seen["last_authorization"])
check(complete(chat("fake"), key="sk-wrong")[0] == 401, "unknown key is 401", "")
check(complete(chat("gpt-unknown"))[0] == 404, "unknown model is 404", "")
print(f"  routing: local -> llama/smollm2 ({local_text[:40]!r}), fake -> fake/fake-small with model rewritten and the upstream key; 401 and 404 for bad key and model")

start = predicted() if real else 0.0
kind, text = stream(chat("local", "Count to five.", stream=True, max_tokens=24))
chunks = [json.loads(line[6:]) for line in text.splitlines() if line.startswith("data: {")]
pieces = "".join((c["choices"][0].get("delta") or {}).get("content") or "" for c in chunks if c.get("choices"))
check(kind.startswith("text/event-stream") and text.rstrip().endswith("data: [DONE]") and pieces, "SSE passes through to [DONE]", text[-400:])
check(not any(c.get("usage") for c in chunks), "the usage event tokengate asked for is not forwarded", text[-400:])
record = [r for r in records() if r.get("stream") and r.get("route") == "local"][-1]
check(record["usage_source"] == ("reported" if real else "estimated") and record["completion_tokens"] > 0 and record.get("stream_done"), "the stream is counted", record)
line = f"  stream local: {len(chunks)} events, counted {record['prompt_tokens']} in / {record['completion_tokens']} out ({record['usage_source']}), ttft {record.get('ttft_ms')} ms"
if real:
    after = settle_metrics(start, record["completion_tokens"])
    check(abs((after - start) - record["completion_tokens"]) <= 1, "llama.cpp tokens_predicted_total moved by the completion tokens tokengate billed", (start, after, record["completion_tokens"]))
    line += f"; llama.cpp tokens_predicted_total +{after - start:.0f}"
print(line)
kind, text = stream(chat("fake", stream=True, max_tokens=5))
record = [r for r in records() if r.get("stream") and r.get("route") == "fake"][-1]
check((record["usage_source"], record["completion_tokens"]) == ("reported", 5), "a stream from a server that reports usage is billed exactly", record)
check(stats(fake)["last_request"].get("stream_options") == {"include_usage": True}, "tokengate injects include_usage upstream", stats(fake)["last_request"])

body = chat("local", "What is a 429?", max_tokens=16, temperature=0)
before = predicted() if real else stats(llama)["requests"]
first = complete(body)
check(first[0] == 200 and first[1].get("x-tokengate-cache") == "miss", "first deterministic request is a cache miss", first[:2])
if real:
    middle = settle_metrics(before, (json.loads(first[2]).get("usage") or {}).get("completion_tokens", 0))
else:
    middle = stats(llama)["requests"]
second = complete(body)
time.sleep(0.5)
after = predicted() if real else stats(llama)["requests"]
check(second[0] == 200 and second[1].get("x-tokengate-cache") == "hit" and second[2] == first[2], "the repeat is a cache hit with the same body", second[:2])
check(after == middle, "a cache hit never reaches the backend", (before, middle, after))
print("  cache: miss then hit, identical body, the backend saw only the miss")

status, _, _ = complete(chat("fake", "Ticket from maria@example.com at 10.20.30.40, token sk-live-4f9a8b7c6d5e", max_tokens=4))
check(status == 200, "redaction request", status)
with open(log, encoding="utf-8") as handle:
    raw = handle.read()
check("maria@example.com" not in raw and "sk-live-4f9a8b7c6d5e" not in raw and TEAM not in raw, "secrets never reach the log", "found in log")
logged = [r for r in records() if "[EMAIL]" in (r.get("prompt") or {}).get("text", "")][-1]
check("[IP]" in logged["prompt"]["text"] and "[API_KEY]" in logged["prompt"]["text"] and logged["cost_usd"] > 0, "prompt redacted and priced", logged)
print(f"  log: {logged['prompt']['text']!r}, cost ${logged['cost_usd']:.8f}")

long_prompt = " ".join(["word"] * 100)
sent = stats(fake)["requests"]
results = [complete(chat("fake", long_prompt, max_tokens=16), key=TIGHT) for _ in range(3)]
check([r[0] for r in results] == [200, 200, 429], "the tight key's token budget runs out on the third request", [(r[0], r[2][:200]) for r in results])
limited = results[2]
check(limited[1].get("x-tokengate-limited-by") == "tokens" and int(limited[1].get("retry-after", "0")) >= 1 and json.loads(limited[2])["error"]["code"] == "rate_limit_exceeded", "429 names the token bucket and carries retry-after", limited)
check(stats(fake)["requests"] - sent == 2, "the refused request never reached the backend", stats(fake)["requests"] - sent)
too_large = complete(chat("fake", max_tokens=400), key=TIGHT)
check(too_large[0] == 400 and json.loads(too_large[2])["error"]["code"] == "request_too_large", "a hold larger than the bucket is a 400", too_large)
print(f"  budget: tight key 200 200 429 (retry-after {limited[1]['retry-after']}s, remaining {limited[1].get('x-ratelimit-remaining-tokens')}), max_tokens 400 -> 400 request_too_large")

call(fake, "/fake/fail", {"status": 429, "times": 1, "retry_after": "30"})
sent = stats(fake)["requests"]
status, headers, body = complete(chat("chat", max_tokens=16))
check(status == 200 and headers.get("x-tokengate-attempts") == "fake/fake-small:429,llama/smollm2:200" and not content(body).startswith("[fake]"), "429 from the fake falls back to llama", (status, headers, body[:300]))
status, headers, body = complete(chat("chat", max_tokens=16))
check(status == 200 and headers.get("x-tokengate-attempts") == "llama/smollm2:200" and stats(fake)["requests"] == sent + 1, "the cooling deployment is skipped", (headers, stats(fake)["requests"] - sent))
status, headers, body = complete(chat("fake", max_tokens=4))
check(status == 429 and json.loads(body)["error"]["code"] == "upstream_cooldown" and 20 <= int(headers.get("retry-after", "0")) <= 30, "a route with only the cooling deployment answers 429 upstream_cooldown", (status, headers, body))
print("  fallback: fake 429 (retry-after 30) -> llama/smollm2; next chat skips the fake; model fake -> 429 upstream_cooldown")

usage = json.loads(call(gateway, "/tokengate/usage")[2])["usage"]
check(usage["streams"] == 2 and usage["cache_hits"] == 1, "usage endpoint counts streams and cache hits", usage)
rows = json.loads(subprocess.run([sys.executable, "code/main.py", "report", log, "--json"], check=True, capture_output=True, text=True).stdout)
by = {(r["key"], r["route"]): r for r in rows}
check(by[("team-a", "chat")]["fallbacks"] == 1 and by[("tight", "fake")]["limited"] == 1 and by[("tight", "fake")]["errors"] == 1, "report per key and model", rows)
print(f"  report: {len(rows)} rows; team-a chat fallbacks 1; tight fake 2 ok, 1 x 429, 1 x 400")
PY

echo "▸ tokengate check and plan on the fixture config"
python3 code/main.py check --config "$F/gateway.json" --json > "$WORK/check.json" || fail "check: $(cat "$WORK/check.json")"
[[ "$(field "$WORK/check.json" routes chat 1 deployment)" == "llama/smollm2" ]] || fail "chat should fall back to llama/smollm2: $(cat "$WORK/check.json")"
grep -q "sk-cc-team-a" "$WORK/check.json" && fail "check must not print key secrets"
python3 code/main.py plan --config "$F/gateway.json" --key team-a "$F/request-chat.json" --json > "$WORK/plan.json" || fail "plan: $(cat "$WORK/plan.json")"
[[ "$(field "$WORK/plan.json" estimate total)" == "119" && "$(field "$WORK/plan.json" cache)" == "use" ]] || fail "plan estimate/cache: $(cat "$WORK/plan.json")"
grep -q "maria@example.com" "$WORK/plan.json" && fail "plan printed an unredacted email: $(cat "$WORK/plan.json")"
CODE=0
python3 code/main.py check --config "$F/gateway.json" --backend nope=http://x > /dev/null 2> "$WORK/bad.err" || CODE=$?
[[ "$CODE" == "2" ]] && grep -q "no backend named 'nope'" "$WORK/bad.err" || fail "unknown backend override should exit 2: code $CODE, $(cat "$WORK/bad.err")"
echo "  chat = fake/fake-small then llama/smollm2; request-chat.json holds 119 tokens; bad input exits 2"

echo "▸ Build It on real sockets: tokengate in front of two fakes (one reports no usage)"
python3 code/main.py fake --name fake --model fake-small --port 0 > "$WORK/fake.log" 2>&1 &
PIDS+=("$!")
python3 code/main.py fake --name llama --model smollm2 --no-usage --port 0 > "$WORK/fake-llama.log" 2>&1 &
PIDS+=("$!")
wait_line "$WORK/fake.log" "listening on"
wait_line "$WORK/fake-llama.log" "listening on"
FAKE_URL="$(sed -n 's/.*listening on //p' "$WORK/fake.log")"
FAKE_LLAMA_URL="$(sed -n 's/.*listening on //p' "$WORK/fake-llama.log")"
start_gateway "$WORK/gw-fakes.jsonl" "$FAKE_LLAMA_URL"
python3 "$WORK/probe.py" fakes "$GATEWAY_URL" "$FAKE_URL" "$FAKE_LLAMA_URL" "$WORK/gw-fakes.jsonl" "" || fail "tokengate with fakes; gateway stderr: $(tail -20 "$WORK/gw-fakes.jsonl.err")"
sed -n '1,3p' "$WORK/gw-fakes.jsonl.err" | sed 's/^/  /'
stop_gateway
LLAMA_URL="$FAKE_LLAMA_URL"

if [[ -n "$HAVE_DOCKER" ]]; then
  echo "▸ Use It: llama.cpp server with the pinned image and model"
  wait "$PULL_PID" || true
  [[ "$(cat "$WORK/pull.exit" 2>/dev/null)" == "0" ]] || fail "docker pull $IMAGE: $(cat "$WORK/pull.log")"
  DIGESTS="$(docker image inspect --format '{{json .RepoDigests}}' "$IMAGE")"
  if [[ -z "${LLAMA_CPP_IMAGE:-}" && "$DIGESTS" != *"$IMAGE_DIGEST"* ]]; then fail "$IMAGE no longer points at the pinned index $IMAGE_DIGEST: $DIGESTS"; fi
  wait "$MODEL_PID" || true
  [[ "$(cat "$WORK/model.exit" 2>/dev/null)" == "0" ]] || fail "model download: $(cat "$WORK/model.log")"
  GOT="$(sha256_of "$MODEL_DIR/$MODEL_FILE")"
  [[ "$GOT" == "$MODEL_SHA256" ]] || fail "sha256 mismatch for $MODEL_FILE: got $GOT, want $MODEL_SHA256"
  echo "  $IMAGE ($IMAGE_DIGEST); $MODEL_FILE $(wc -c < "$MODEL_DIR/$MODEL_FILE") bytes, sha256 ok"
  PORT="$(free_port)"
  NPROC="$(nproc 2>/dev/null || echo 4)"
  THREADS=$(( NPROC > 4 ? 4 : NPROC ))
  docker run -d --name "$CONTAINER" -p "127.0.0.1:$PORT:8080" -v "$MODEL_DIR:/models:ro" "$IMAGE" \
    -m "/models/$MODEL_FILE" --alias smollm2 --ctx-size 4096 --parallel 2 --metrics --threads "$THREADS" > /dev/null || fail "docker run $IMAGE"
  LLAMA_URL="http://127.0.0.1:$PORT"
  READY=0
  for _ in $(seq 1 120); do
    if curl -sf "$LLAMA_URL/health" > "$WORK/health.json" 2>/dev/null; then READY=1; break; fi
    [[ "$(docker inspect -f '{{.State.Running}}' "$CONTAINER" 2>/dev/null)" == "true" ]] || break
    sleep 1
  done
  docker logs "$CONTAINER" > "$WORK/server.log" 2>&1 || true
  [[ "$READY" == "1" ]] || fail "llama-server not healthy within 120s: $(tail -30 "$WORK/server.log")"
  curl -sf "$LLAMA_URL/v1/models" > "$WORK/models.json" || fail "/v1/models"
  [[ "$(field "$WORK/models.json" data 0 id)" == "smollm2" ]] || fail "model id should be the alias smollm2: $(cat "$WORK/models.json")"
  echo "  /health ok, /v1/models lists smollm2"

  echo "▸ Use It: tokengate in front of llama.cpp and the fake"
  start_gateway "$WORK/gw-llama.jsonl" "$LLAMA_URL"
  python3 "$WORK/probe.py" llama "$GATEWAY_URL" "$FAKE_URL" "$LLAMA_URL" "$WORK/gw-llama.jsonl" "$LLAMA_URL/metrics" || fail "tokengate with llama.cpp; gateway stderr: $(tail -20 "$WORK/gw-llama.jsonl.err"); server: $(docker logs --tail 20 "$CONTAINER" 2>&1)"
  stop_gateway
else
  echo "  (docker not available; llama.cpp checks skipped, LiteLLM runs against the fake llama)"
fi

if [[ -z "$HAVE_PY310" ]]; then
  echo "  (python3 is older than 3.10; LiteLLM needs 3.10+, LiteLLM checks skipped)"
  echo "SMOKE OK: ai-infra/ai-gateway (tokengate only)"
  exit 0
fi

echo "▸ Use It: LiteLLM proxy $LITELLM_VERSION with the same routes and fallback"
wait "$PIP_PID" || true
[[ "$(cat "$WORK/pip.exit" 2>/dev/null)" == "0" ]] || fail "pip install litellm[proxy]==$LITELLM_VERSION: $(tail -30 "$WORK/pip.log")"
LITELLM_PORT="$(free_port)"
LITELLM_URL="http://127.0.0.1:$LITELLM_PORT"
CONFIG="$PWD/$F/litellm.yaml"
( cd "$WORK" && exec env LLAMA_API_BASE="$LLAMA_URL/v1" FAKE_API_BASE="$FAKE_URL/v1" LITELLM_MASTER_KEY="$LITELLM_MASTER_KEY" LITELLM_LOCAL_MODEL_COST_MAP=True LITELLM_LOG=ERROR \
  "$WORK/venv/bin/litellm" --config "$CONFIG" --host 127.0.0.1 --port "$LITELLM_PORT" ) > "$WORK/litellm.log" 2>&1 &
LITELLM_PID="$!"
PIDS+=("$LITELLM_PID")
READY=0
for _ in $(seq 1 120); do
  if curl -sf "$LITELLM_URL/health/liveliness" > "$WORK/liveliness.txt" 2>/dev/null; then READY=1; break; fi
  kill -0 "$LITELLM_PID" 2>/dev/null || break
  sleep 1
done
[[ "$READY" == "1" ]] || fail "LiteLLM proxy did not become live: $(tail -30 "$WORK/litellm.log")"
curl -s -X POST -d '{}' "$FAKE_URL/fake/reset" > /dev/null
lite() {
  curl -s -D "$WORK/$1.headers" -o "$WORK/$1.json" -H "Authorization: Bearer $LITELLM_MASTER_KEY" -H 'Content-Type: application/json' -d "$2" "$LITELLM_URL/v1/chat/completions" || fail "curl LiteLLM $1"
}
header() { python3 -c 'import sys; name = sys.argv[2].lower(); print(next((l.split(":", 1)[1].strip() for l in open(sys.argv[1], encoding="utf-8", errors="replace") if l.lower().startswith(name + ":")), ""))' "$WORK/$1.headers" "$2"; }
answer() { python3 -c 'import json, sys; print(json.load(open(sys.argv[1]))["choices"][0]["message"]["content"] or "")' "$WORK/$1.json" 2>/dev/null || echo "<no answer: $(cat "$WORK/$1.json")>"; }
lite local '{"model": "local", "messages": [{"role": "user", "content": "Name one Linux signal."}], "max_tokens": 16}'
[[ "$(header local x-litellm-model-group)" == "local" && "$(header local x-litellm-model-api-base)" == "$LLAMA_URL/v1" ]] || fail "LiteLLM local should go to $LLAMA_URL/v1: $(cat "$WORK/local.headers") $(cat "$WORK/local.json")"
[[ "$(answer local)" != "[fake]"* ]] || fail "LiteLLM local answered by the fake: $(answer local)"
lite fake '{"model": "fake", "messages": [{"role": "user", "content": "Name one Linux signal."}], "max_tokens": 4}'
[[ "$(answer fake)" == "[fake] w1 w2 w3" && "$(header fake x-litellm-model-api-base)" == "$FAKE_URL/v1" ]] || fail "LiteLLM fake route: $(cat "$WORK/fake.headers") $(cat "$WORK/fake.json")"
curl -s "$FAKE_URL/fake/stats" > "$WORK/stats.json"
[[ "$(field "$WORK/stats.json" models fake-small)" == "1" && "$(field "$WORK/stats.json" last_authorization)" == "Bearer $FAKE_API_KEY" ]] || fail "LiteLLM should send fake-small with the upstream key: $(cat "$WORK/stats.json")"
echo "  routing: local -> $(header local x-litellm-model-api-base), fake -> $(header fake x-litellm-model-api-base) with model fake-small"
curl -s -X POST -d '{"status": 429, "times": 2, "retry_after": "30"}' "$FAKE_URL/fake/fail" > /dev/null
lite chat '{"model": "chat", "messages": [{"role": "user", "content": "Name one Linux signal."}], "max_tokens": 16}'
[[ "$(header chat x-litellm-attempted-fallbacks)" == "1" && "$(header chat x-litellm-model-group)" == "local" && "$(header chat x-litellm-model-api-base)" == "$LLAMA_URL/v1" ]] || fail "LiteLLM should fall back from chat to local: $(cat "$WORK/chat.headers") $(cat "$WORK/chat.json")"
[[ "$(answer chat)" != "[fake]"* ]] || fail "LiteLLM fallback answered by the fake: $(answer chat)"
curl -s "$FAKE_URL/fake/stats" > "$WORK/stats.json"
[[ "$(field "$WORK/stats.json" failed)" -ge 1 ]] || fail "the fake should have refused LiteLLM with 429: $(cat "$WORK/stats.json")"
echo "  fallback: fake 429 -> model group local (x-litellm-attempted-fallbacks: 1), the same path tokengate took"

echo "SMOKE OK: ai-infra/ai-gateway"
