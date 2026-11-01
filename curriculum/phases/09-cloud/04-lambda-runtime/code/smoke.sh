#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."

ML="$PWD/code/main.py"
FN="$PWD/code/fixtures/function"
IMAGE="public.ecr.aws/lambda/python:3.12.2026.10.15.13"
IMAGE_PYTHON="/var/lang/bin/python3.12"
WORK="$(mktemp -d)"
PIDS=()
CONTAINERS=()
BASE=""

fail() {
  echo "SMOKE FAIL: $*" >&2
  for f in "$WORK"/*-fn.txt; do
    if [[ -f "$f" ]]; then echo "--- $(basename "$f") (last 25 lines)" >&2; tail -25 "$f" >&2; fi
  done
  for c in ${CONTAINERS[@]+"${CONTAINERS[@]}"}; do
    echo "--- docker logs $c (last 25 lines)" >&2
    docker logs "$c" > "$WORK/fail-$c.txt" 2>&1 || true
    tail -25 "$WORK/fail-$c.txt" >&2 || true
  done
  exit 1
}
cleanup() {
  for pid in ${PIDS[@]+"${PIDS[@]}"}; do kill -TERM "$pid" 2>/dev/null || true; done
  for pid in ${PIDS[@]+"${PIDS[@]}"}; do wait "$pid" 2>/dev/null || true; done
  for c in ${CONTAINERS[@]+"${CONTAINERS[@]}"}; do docker rm -f "$c" >/dev/null 2>&1 || true; done
  pkill -KILL -f "$ML bootstrap" 2>/dev/null || true
  rm -rf "$WORK"
}
trap cleanup EXIT

now_ms() { python3 -c 'import time; print(int(time.time() * 1000))'; }
invoke() {
  curl -s -m 30 -o "$WORK/$2.body" -D "$WORK/$2.headers" -w '%{http_code}' -X POST "$1/2015-03-31/functions/function/invocations" -d "$3" || true
}
wait_invoke() {
  local code="000"
  for _ in $(seq 1 60); do
    code="$(invoke "$@")"
    [[ "$code" != "000" ]] && break
    sleep 0.5
  done
  echo "$code"
}
jfield() {
  python3 - "$1" "$2" <<'PY'
import json, sys
text = open(sys.argv[1], encoding="utf-8", errors="replace").read()
try:
    value = json.loads(text)
except ValueError:
    print("<not json: %s>" % text[:200])
    raise SystemExit(0)
value = value.get(sys.argv[2]) if isinstance(value, dict) else None
print(json.dumps(value) if value is None or isinstance(value, (bool, dict, list)) else value)
PY
}
expect_hello() {
  python3 - "$WORK/$1.body" "$2" "$3" <<'PY' || fail "$1: expected cold=$2 invocations=$3, got $(cat "$WORK/$1.body")"
import json, sys
body = json.load(open(sys.argv[1]))
want = {"message": "hello smoke", "cold": sys.argv[2] == "true", "invocations": int(sys.argv[3])}
assert body == want, (body, want)
PY
}
same_body() {
  python3 - "$WORK/$1.body" "$WORK/$2.body" "${3:-all}" <<'PY' || fail "$1 and $2 differ: $(cat "$WORK/$1.body") | $(cat "$WORK/$2.body")"
import json, re, sys
a, b = (json.load(open(path)) for path in sys.argv[1:3])
if sys.argv[3] in ("error", "platform"):
    a, b = ({k: v.get(k) for k in ("errorType", "errorMessage")} for v in (a, b))
if sys.argv[3] == "platform":
    a, b = ({k: re.sub(r"RequestId: \S+ ", "", v or "") for k, v in d.items()} for d in (a, b))
assert a == b, (a, b)
PY
}
start_minilambda() {
  local name="$1"
  shift
  python3 "$ML" serve --task-root "$FN" --port 0 --port-file "$WORK/$name.port" --log-file "$WORK/$name-fn.txt" "$@" > "$WORK/$name.out" 2>&1 &
  PIDS+=("$!")
  for _ in $(seq 1 100); do [[ -s "$WORK/$name.port" ]] && break; sleep 0.1; done
  [[ -s "$WORK/$name.port" ]] || fail "minilambda $name did not start: $(cat "$WORK/$name.out")"
  BASE="http://127.0.0.1:$(cat "$WORK/$name.port")"
}
run_sequence() {
  local label="$1" url="$2" code started elapsed
  code="$(wait_invoke "$url" "$label-hello1" '{"name": "smoke"}')"
  [[ "$code" == "200" ]] || fail "$label: first invoke HTTP $code: $(cat "$WORK/$label-hello1.body" 2>/dev/null)"
  expect_hello "$label-hello1" true 1
  code="$(invoke "$url" "$label-hello2" '{"name": "smoke"}')"
  expect_hello "$label-hello2" false 2
  code="$(invoke "$url" "$label-fail" '{"action": "fail", "reason": "smoke"}')"
  [[ "$code" == "200" ]] || fail "$label: a handler error should still be HTTP 200, got $code"
  [[ "$(jfield "$WORK/$label-fail.body" errorType)" == "ValueError" && "$(jfield "$WORK/$label-fail.body" errorMessage)" == "bad input: smoke" ]] || fail "$label: handler error body: $(cat "$WORK/$label-fail.body")"
  code="$(invoke "$url" "$label-hello3" '{"name": "smoke"}')"
  expect_hello "$label-hello3" false 4
  started="$(now_ms)"
  code="$(invoke "$url" "$label-timeout" '{"action": "sleep", "seconds": 5}')"
  elapsed=$(( $(now_ms) - started ))
  grep -q "Task timed out after 1.00 seconds" "$WORK/$label-timeout.body" || fail "$label: timeout body (HTTP $code): $(cat "$WORK/$label-timeout.body")"
  [[ "$elapsed" -lt 4000 ]] || fail "$label: the 1 s timeout took ${elapsed} ms to answer"
  code="$(invoke "$url" "$label-hello4" '{"name": "smoke"}')"
  expect_hello "$label-hello4" true 1
  code="$(invoke "$url" "$label-crash" '{"action": "exit", "code": 3}')"
  [[ "$(jfield "$WORK/$label-crash.body" errorType)" == "Runtime.ExitError" ]] || fail "$label: crash body (HTTP $code): $(cat "$WORK/$label-crash.body")"
  grep -q "Runtime exited with error: exit status 3" "$WORK/$label-crash.body" || fail "$label: crash message: $(cat "$WORK/$label-crash.body")"
  CRASH_CODE="$code"
  code="$(invoke "$url" "$label-hello5" '{"name": "smoke"}')"
  expect_hello "$label-hello5" true 1
  echo "  $label: cold, warm, handler error stays warm, timeout ${elapsed} ms then re-init, crash (HTTP $CRASH_CODE) then re-init"
}

echo "▸ minilambda + bootstrap: cold, warm, handler error, timeout, crash"
start_minilambda ml --handler app.handler --timeout 1 --reserved-concurrency 1
ML_URL="$BASE"
run_sequence ml "$ML_URL"
grep -qi '^x-minilambda-cold: true' "$WORK/ml-hello1.headers" || fail "first invoke should be marked cold: $(cat "$WORK/ml-hello1.headers")"
grep -qi '^x-amz-function-error: Unhandled' "$WORK/ml-fail.headers" || fail "a handler error needs X-Amz-Function-Error: $(cat "$WORK/ml-fail.headers")"
[[ "$(jfield "$WORK/ml-timeout.body" errorType)" == "Sandbox.Timedout" ]] || fail "minilambda timeout body: $(cat "$WORK/ml-timeout.body")"

echo "▸ reserved concurrency 1: a concurrent second invoke is throttled before any process runs"
curl -s -m 30 -o "$WORK/ml-slow.body" -X POST "$ML_URL/2015-03-31/functions/function/invocations" -d '{"action": "sleep", "seconds": 0.6}' &
SLOW=$!
for _ in $(seq 1 60); do
  curl -s -m 5 "$ML_URL/minilambda/state" > "$WORK/state.json" || true
  grep -q '"status": "busy"' "$WORK/state.json" && break
  sleep 0.05
done
CODE="$(invoke "$ML_URL" ml-throttled '{"name": "smoke"}')"
wait "$SLOW" || true
[[ "$CODE" == "429" ]] || fail "expected 429, got $CODE: $(cat "$WORK/ml-throttled.body")"
grep -qi '^x-amzn-errortype: TooManyRequestsException' "$WORK/ml-throttled.headers" || fail "throttle headers: $(cat "$WORK/ml-throttled.headers")"
[[ "$(jfield "$WORK/ml-throttled.body" Reason)" == "ReservedFunctionConcurrentInvocationLimitExceeded" ]] || fail "throttle body: $(cat "$WORK/ml-throttled.body")"
REQ="$(grep -i '^x-amzn-requestid:' "$WORK/ml-throttled.headers" | tr -d '\r' | awk '{print $2}')"
[[ -n "$REQ" ]] || fail "the 429 has no X-Amzn-RequestId: $(cat "$WORK/ml-throttled.headers")"
if grep -q "$REQ" "$WORK/ml-fn.txt"; then fail "a throttled request must not reach the function's log ($REQ)"; fi
echo "  429 ReservedFunctionConcurrentInvocationLimitExceeded, nothing logged for $REQ"

echo "▸ minilambda's REPORT lines: one cold start, a timeout and a crash, suppressed inits without Init Duration"
python3 "$ML" report "$WORK/ml-fn.txt" --json > "$WORK/ml-report.json" || fail "minilambda report: $(cat "$WORK/ml-report.json")"
python3 - "$WORK/ml-report.json" <<'PY' || fail "minilambda report: $(cat "$WORK/ml-report.json")"
import json, sys
summary = json.load(open(sys.argv[1]))
assert summary["cold_starts"] == 1, summary
assert summary["invocations"] >= 8, summary
assert summary["statuses"].get("timeout") == 1 and summary["statuses"].get("error") == 1, summary
print("  invocations", summary["invocations"], "cold starts", summary["cold_starts"], "init ms", summary["init_ms"]["max"], "statuses", summary["statuses"])
PY

echo "▸ an import error: Runtime.ImportModuleError, init tried in the init phase and again inside the invoke"
start_minilambda ml-broken --handler broken.handler --timeout 3
CODE="$(invoke "$BASE" ml-broken '{}')"
[[ "$(jfield "$WORK/ml-broken.body" errorType)" == "Runtime.ImportModuleError" ]] || fail "import error body (HTTP $CODE): $(cat "$WORK/ml-broken.body")"
grep -q "INIT_REPORT.*Phase: init.*Status: error.*Runtime.ImportModuleError" "$WORK/ml-broken-fn.txt" || fail "no INIT_REPORT for the init phase"
grep -q "INIT_REPORT.*Phase: invoke.*Status: error" "$WORK/ml-broken-fn.txt" || fail "no INIT_REPORT for the retry inside the invoke"
echo "  $(jfield "$WORK/ml-broken.body" errorMessage)"

if command -v aws >/dev/null; then
  echo "▸ the AWS CLI speaks the same Invoke API: aws lambda invoke --endpoint-url, with a log tail"
  export AWS_ACCESS_KEY_ID=test AWS_SECRET_ACCESS_KEY=test AWS_DEFAULT_REGION=us-east-1 AWS_PAGER=""
  echo '{"name": "smoke"}' > "$WORK/event.json"
  echo '{"action": "fail", "reason": "smoke"}' > "$WORK/event-fail.json"
  aws lambda invoke --endpoint-url "$ML_URL" --function-name minilambda --payload "fileb://$WORK/event.json" --log-type Tail --output json "$WORK/cli-out.json" > "$WORK/cli-meta.json" 2> "$WORK/cli.err" || fail "aws lambda invoke: $(cat "$WORK/cli.err")"
  aws lambda invoke --endpoint-url "$ML_URL" --function-name minilambda --payload "fileb://$WORK/event-fail.json" --output json "$WORK/cli-fail.json" > "$WORK/cli-fail-meta.json" 2> "$WORK/cli.err" || fail "aws lambda invoke (error): $(cat "$WORK/cli.err")"
  python3 - "$WORK/cli-meta.json" "$WORK/cli-out.json" "$WORK/cli-fail-meta.json" "$WORK/cli-fail.json" <<'PY' || fail "aws lambda invoke output: $(cat "$WORK/cli-meta.json" "$WORK/cli-fail-meta.json")"
import base64, json, sys
meta, out, fail_meta, fail_out = (json.load(open(p)) for p in sys.argv[1:5])
assert meta["StatusCode"] == 200 and "FunctionError" not in meta, meta
assert out["message"] == "hello smoke", out
log = base64.b64decode(meta["LogResult"]).decode()
assert log.startswith("START RequestId:") and "REPORT RequestId:" in log, log
assert fail_meta["FunctionError"] == "Unhandled" and fail_out["errorType"] == "ValueError", (fail_meta, fail_out)
print("  StatusCode", meta["StatusCode"], "ExecutedVersion", meta.get("ExecutedVersion"), "| error run FunctionError", fail_meta["FunctionError"])
PY
else
  echo "  (aws CLI not installed; aws lambda invoke check skipped)"
fi

if ! command -v docker >/dev/null || ! docker info >/dev/null 2>&1; then
  echo "  (docker not available; the Runtime Interface Emulator comparison was skipped)"
  echo "SMOKE OK: cloud/lambda-runtime (minilambda only)"
  exit 0
fi

start_container() {
  local name="$1"
  shift
  docker rm -f "$name" >/dev/null 2>&1 || true
  docker run -d --name "$name" -p 127.0.0.1::8080 -v "$FN:/var/task:ro" -e AWS_LAMBDA_FUNCTION_TIMEOUT=1 "$@" > "$WORK/$name.id" 2> "$WORK/$name.err" || fail "docker run $name: $(cat "$WORK/$name.err")"
  CONTAINERS+=("$name")
  docker port "$name" 8080/tcp > "$WORK/$name.port" || fail "docker port $name"
  BASE="http://$(head -1 "$WORK/$name.port")"
}

echo "▸ pull the Lambda Python base image (pinned): $IMAGE"
docker pull -q "$IMAGE" > "$WORK/pull.txt" 2>&1 || fail "docker pull: $(cat "$WORK/pull.txt")"
docker run --rm --entrypoint /bin/sh "$IMAGE" -c "test -x $IMAGE_PYTHON && test -x /usr/local/bin/aws-lambda-rie && test -x /var/runtime/bootstrap" || fail "the image no longer has $IMAGE_PYTHON, aws-lambda-rie and /var/runtime/bootstrap where the lesson expects them"

echo "▸ the AWS Python runtime under aws-lambda-rie: the same sequence"
start_container cc-lambda-runtime-aws "$IMAGE" app.handler
AWS_URL="$BASE"
run_sequence aws "$AWS_URL"

echo "▸ your bootstrap under aws-lambda-rie: the same sequence"
start_container cc-lambda-runtime-mini -v "$PWD/code:/opt/minilambda:ro" --entrypoint /usr/local/bin/aws-lambda-rie "$IMAGE" "$IMAGE_PYTHON" /opt/minilambda/main.py bootstrap app.handler
MINI_URL="$BASE"
run_sequence mini "$MINI_URL"

echo "▸ same responses and the same errors from minilambda, the AWS runtime and your bootstrap"
for step in hello1 hello2 hello3 hello4 hello5; do
  same_body "ml-$step" "aws-$step"
  same_body "ml-$step" "mini-$step"
done
same_body ml-fail aws-fail error
same_body ml-fail mini-fail error
same_body ml-crash aws-crash platform
same_body ml-crash mini-crash platform
echo "  hello x5, ValueError, Runtime.ExitError: identical across the three"

echo "▸ the emulator's REPORT lines: Init Duration on the first invocation only, also after the timeout reset"
for c in cc-lambda-runtime-aws cc-lambda-runtime-mini; do
  docker logs "$c" > "$WORK/$c.logs.txt" 2>&1 || fail "docker logs $c"
  grep -q "START RequestId" "$WORK/$c.logs.txt" || fail "$c: no START lines: $(tail -20 "$WORK/$c.logs.txt")"
  python3 "$ML" report "$WORK/$c.logs.txt" --json > "$WORK/$c.report.json" || fail "$c: minilambda report found no REPORT lines: $(tail -20 "$WORK/$c.logs.txt")"
  python3 - "$WORK/$c.report.json" "$c" <<'PY' || fail "$c report: $(cat "$WORK/$c.report.json")"
import json, sys
summary = json.load(open(sys.argv[1]))
assert summary["cold_starts"] == 1, summary
assert summary["invocations"] >= 6, summary
print("  %s: %d REPORT lines, Init Duration on %d (%.2f ms, the emulator's own setup)" % (sys.argv[2], summary["invocations"], summary["cold_starts"], summary["init_ms"]["max"]))
PY
done

echo "▸ an import error under the emulator: the same Runtime.ImportModuleError from both runtimes"
start_container cc-lambda-runtime-aws-broken "$IMAGE" broken.handler
AWS_BROKEN="$(wait_invoke "$BASE" aws-broken '{}')"
start_container cc-lambda-runtime-mini-broken -v "$PWD/code:/opt/minilambda:ro" --entrypoint /usr/local/bin/aws-lambda-rie "$IMAGE" "$IMAGE_PYTHON" /opt/minilambda/main.py bootstrap broken.handler
MINI_BROKEN="$(wait_invoke "$BASE" mini-broken '{}')"
[[ "$(jfield "$WORK/aws-broken.body" errorType)" == "Runtime.ImportModuleError" ]] || fail "AWS runtime import error (HTTP $AWS_BROKEN): $(cat "$WORK/aws-broken.body")"
[[ "$(jfield "$WORK/mini-broken.body" errorType)" == "Runtime.ImportModuleError" ]] || fail "bootstrap import error (HTTP $MINI_BROKEN): $(cat "$WORK/mini-broken.body")"
same_body ml-broken aws-broken error
same_body ml-broken mini-broken error
echo "  HTTP from the emulator: AWS runtime $AWS_BROKEN, your bootstrap $MINI_BROKEN (minilambda answers 200 with X-Amz-Function-Error, as Lambda does)"

echo "SMOKE OK: cloud/lambda-runtime"
