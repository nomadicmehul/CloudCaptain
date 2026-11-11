#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."

fail() { echo "SMOKE FAIL: $*" >&2; exit 1; }
WORK="$(mktemp -d)"
URLLIB3_VERSION="2.8.0"
URLLIB3_WHEEL_SHA256="0cf3cae568d36aa9576b28dfb35f11328f1cb974ca7647d9475ebb86c75ac6e3"
URLLIB3_SDIST_SHA256="63bf2ead4c879426ebf22ef2a781eeb4aa3b4ae798a0435506f8687fd5bb9b63"
SCENARIOS="code/fixtures/scenarios.json"
PIDS=()

cleanup() {
  set +e
  for pid in ${PIDS[@]+"${PIDS[@]}"}; do kill "$pid" 2>/dev/null; wait "$pid" 2>/dev/null; done
  rm -rf "$WORK"
}
trap cleanup EXIT

wait_line() { for _ in $(seq 1 100); do grep -q "$2" "$1" 2>/dev/null && return 0; sleep 0.1; done; fail "no '$2' in $1: $(cat "$1" 2>/dev/null)"; }
for tool in curl python3; do command -v "$tool" >/dev/null || fail "$tool is required"; done
python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' && HAVE_PY310=1 || HAVE_PY310=""

if [[ -n "$HAVE_PY310" ]]; then
  echo "▸ background: pip install urllib3==$URLLIB3_VERSION --require-hashes into a venv"
  printf 'urllib3==%s \\\n    --hash=sha256:%s \\\n    --hash=sha256:%s\n' "$URLLIB3_VERSION" "$URLLIB3_WHEEL_SHA256" "$URLLIB3_SDIST_SHA256" > "$WORK/requirements.txt"
  ( set +e; python3 -m venv "$WORK/venv" && "$WORK/venv/bin/pip" install -q --disable-pip-version-check --require-hashes --only-binary :all: -r "$WORK/requirements.txt"; echo "$?" > "$WORK/pip.exit" ) > "$WORK/pip.log" 2>&1 &
  PIP_PID="$!"
  PIDS+=("$PIP_PID")
fi

echo "▸ apiclient request --dry-run: a POST is retry-safe only with an Idempotency-Key"
python3 code/main.py request --dry-run --json POST http://api.invalid/orders > "$WORK/plan-post.json"
python3 code/main.py request --dry-run --json --auto-idempotency-key POST http://api.invalid/orders > "$WORK/plan-key.json"
python3 - "$WORK/plan-post.json" "$WORK/plan-key.json" <<'PY' || fail "dry-run plans: $(cat "$WORK/plan-post.json" "$WORK/plan-key.json")"
import json, sys
post, keyed = (json.load(open(p)) for p in sys.argv[1:3])
assert (post["retry_safe"], post["max_attempts"]) == (False, 1), post
assert (keyed["retry_safe"], keyed["max_attempts"]) == (True, 4) and len(keyed["idempotency_key"]) == 36, keyed
print(f"  POST: {post['why']}; with --auto-idempotency-key: {keyed['max_attempts']} attempts, backoff ceilings {keyed['backoff_ceilings_s']}")
PY
CODE=0
python3 code/main.py request GET ftp://example.com > "$WORK/bad.out" 2> "$WORK/bad.err" || CODE=$?
[[ "$CODE" == "2" && ! -s "$WORK/bad.out" ]] && grep -q "http:// or https://" "$WORK/bad.err" || fail "a bad URL should exit 2 with stderr only: code $CODE, $(cat "$WORK/bad.out" "$WORK/bad.err")"
echo "  bad URL: exit 2, message on stderr, stdout empty"

echo "▸ start the fault server"
python3 code/main.py serve --port 0 > "$WORK/server.out" 2> "$WORK/server.err" &
PIDS+=("$!")
wait_line "$WORK/server.out" "listening on"
URL="$(sed -n 's/.*listening on //p' "$WORK/server.out")"
curl -sf "$URL/__hits/probe" > "$WORK/probe.json" || fail "fault server at $URL does not answer: $(cat "$WORK/server.err")"
grep -q '"hits"' "$WORK/probe.json" || fail "$URL is not the fault server: $(cat "$WORK/probe.json")"
echo "  $URL"

cat > "$WORK/urllib3_one.py" <<'PY'
import json, sys
import urllib3
from urllib3.util import Retry, Timeout
method, url, key, retries, backoff, connect, read = sys.argv[1:8]
retry = Retry(total=int(retries), backoff_factor=float(backoff), status_forcelist=[408, 429, 500, 502, 503, 504],
              allowed_methods=Retry.DEFAULT_ALLOWED_METHODS, respect_retry_after_header=True, raise_on_status=False)
headers = {"Content-Type": "application/json"}
if key:
    headers["Idempotency-Key"] = key
http = urllib3.PoolManager(retries=retry)
try:
    response = http.request(method, url, body=b"{}" if method == "POST" else None, headers=headers, timeout=Timeout(connect=float(connect), read=float(read)))
    print(json.dumps({"status": response.status}))
except urllib3.exceptions.HTTPError as err:
    print(json.dumps({"status": 0, "error": type(err).__name__}))
PY

cat > "$WORK/drive.py" <<'PY'
import json, os, subprocess, sys, urllib.request
base, spec_path, work, clients = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4].split(",")
spec = json.load(open(spec_path))
venv_python = os.path.join(work, "venv", "bin", "python")


def control(path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    with urllib.request.urlopen(urllib.request.Request(base + path, data=data, method="POST" if data else "GET"), timeout=10) as reply:
        return json.loads(reply.read())


def run(client, scenario, name):
    method, url = scenario["method"], f"{base}/s/{name}"
    key = scenario.get("idempotency_key", "")
    read = str(scenario.get("read_timeout", spec["read_timeout"]))
    if client == "apiclient":
        argv = [sys.executable, "code/main.py", "request", method, url, "--json", "--seed", "7", "--retries", str(spec["retries"]),
                "--backoff-base", str(spec["backoff_base"]), "--connect-timeout", str(spec["connect_timeout"]), "--read-timeout", read,
                "--log", os.path.join(work, "apiclient.jsonl")]
        argv += ["-d", "{}"] if method == "POST" else []
        argv += ["--idempotency-key", key] if key else []
        done = subprocess.run(argv, capture_output=True, text=True, timeout=60)
        if done.returncode == 2:
            raise SystemExit(f"SMOKE FAIL: apiclient usage error: {done.stderr}")
        return json.loads(done.stdout)["status"] if done.stdout.strip() else 0, done.stderr.strip()
    if client == "urllib3":
        done = subprocess.run([venv_python, os.path.join(work, "urllib3_one.py"), method, url, key, str(spec["retries"]), str(spec["backoff_base"]),
                               str(spec["connect_timeout"]), read], capture_output=True, text=True, timeout=60)
        if done.returncode != 0:
            raise SystemExit(f"SMOKE FAIL: urllib3 driver: {done.stderr}")
        return json.loads(done.stdout)["status"], done.stdout.strip()
    argv = ["curl", "-sS", "-o", os.devnull, "-w", "%{http_code}", "--retry", str(spec["retries"]), "--max-time", read, "-X", method]
    argv += ["--retry-all-errors"] if client == "curl-all-errors" else []
    argv += ["-H", "Content-Type: application/json", "-d", "{}"] if method == "POST" else []
    argv += ["-H", f"Idempotency-Key: {key}"] if key else []
    done = subprocess.run(argv + [url], capture_output=True, text=True, timeout=60)
    return int(done.stdout.strip() or 0), f"exit {done.returncode} {done.stderr.strip()}"


failures, rows = [], []
for scenario in spec["scenarios"]:
    row = [scenario["name"]]
    for client in clients:
        expected = scenario["attempts"].get(client)
        if expected is None:
            continue
        name = f"{scenario['name']}--{client}"
        control(f"/__plan/{name}", {"plan": scenario["plan"]})
        final, detail = run(client, scenario, name)
        hits = control(f"/__hits/{name}")["hits"]
        gaps = [round(b["t"] - a["t"], 2) for a, b in zip(hits, hits[1:])]
        row.append(f"{client}: {len(hits)} -> {final or 'error'} {gaps}")
        if len(hits) != expected:
            failures.append(f"{name}: {len(hits)} attempts, expected {expected} ({detail})")
        if final != scenario["final"][client]:
            failures.append(f"{name}: final {final}, expected {scenario['final']} ({detail})")
        low = scenario.get("min_gap", {}).get(client)
        if low is not None and (not gaps or gaps[0] < low):
            failures.append(f"{name}: waited {gaps} before retrying, expected at least {low}s")
        high = scenario.get("max_gap", {}).get(client)
        if high is not None and (not gaps or gaps[0] >= high):
            failures.append(f"{name}: waited {gaps}, expected under {high}s")
        if scenario.get("idempotency_key") and client == "apiclient":
            keys = {h["idempotency_key"] for h in hits}
            if keys != {scenario["idempotency_key"]}:
                failures.append(f"{name}: idempotency keys {keys}")
        if scenario["method"] == "POST" and not scenario.get("idempotency_key") and client in ("apiclient", "urllib3") and len(hits) != 1:
            failures.append(f"{name}: a POST without an idempotency key was sent {len(hits)} times")
    rows.append(row)
for row in rows:
    print(f"  {row[0]:<22} " + " | ".join(row[1:]))
if failures:
    raise SystemExit("SMOKE FAIL: " + "; ".join(failures))
PY

echo "▸ scenario table: apiclient (Build It) against the fault server; attempts counted by the server"
python3 "$WORK/drive.py" "$URL" "$SCENARIOS" "$WORK" apiclient || fail "apiclient scenarios"
python3 - "$WORK/apiclient.jsonl" <<'PY' || fail "attempt log: $(head -5 "$WORK/apiclient.jsonl")"
import json, sys
records = [json.loads(line) for line in open(sys.argv[1])]
need = {"event", "method", "url", "attempt", "status", "error_kind", "ms", "decision", "reason", "delay_s", "idempotency_key", "breaker"}
assert records and all(need <= set(r) for r in records if r["event"] == "attempt"), records[:2]
reasons = sorted({r["reason"] for r in records})
assert "retry-after" in reasons and "x-ratelimit-reset" in reasons and "backoff" in reasons, reasons
assert any(r["reason"] == "POST without Idempotency-Key is not retried" for r in records), reasons
assert any(r["error_kind"] == "timeout" for r in records) and any(r["error_kind"] == "read" for r in records), reasons
print(f"  {len(records)} JSON log lines; reasons seen: {', '.join(reasons)}")
PY

echo "▸ scenario table: curl --retry (and --retry-all-errors for the reset)"
python3 "$WORK/drive.py" "$URL" "$SCENARIOS" "$WORK" curl || fail "curl scenarios"
python3 - "$URL" "$SCENARIOS" <<'PY' || fail "curl --retry-all-errors should retry the connection reset"
import json, subprocess, sys, urllib.request
base = sys.argv[1]
plan = json.dumps({"plan": [{"reset": True}]}).encode()
urllib.request.urlopen(urllib.request.Request(base + "/__plan/reset-all-errors", data=plan), timeout=10).read()
done = subprocess.run(["curl", "-sS", "-o", "/dev/null", "-w", "%{http_code}", "--retry", "2", "--retry-all-errors", base + "/s/reset-all-errors"], capture_output=True, text=True, timeout=60)
hits = json.loads(urllib.request.urlopen(base + "/__hits/reset-all-errors", timeout=10).read())["hits"]
assert (done.stdout, len(hits)) == ("200", 2), (done.stdout, done.stderr, len(hits))
print(f"  get-connection-reset with --retry-all-errors: 2 attempts -> 200 (plain --retry: 1 attempt, {done.stderr.strip() or 'curl error'} on the first try)")
PY

if [[ -n "$HAVE_PY310" ]]; then
  echo "▸ scenario table: urllib3 $URLLIB3_VERSION with the equivalent Retry"
  wait "$PIP_PID" || true
  [[ "$(cat "$WORK/pip.exit" 2>/dev/null)" == "0" ]] || fail "pip install urllib3==$URLLIB3_VERSION: $(tail -20 "$WORK/pip.log")"
  [[ "$("$WORK/venv/bin/python" -c 'import urllib3; print(urllib3.__version__)')" == "$URLLIB3_VERSION" ]] || fail "wrong urllib3 version in the venv"
  python3 "$WORK/drive.py" "$URL" "$SCENARIOS" "$WORK" urllib3 || fail "urllib3 scenarios"
else
  echo "  (python3 is older than 3.10; urllib3 $URLLIB3_VERSION needs 3.10+, urllib3 column skipped)"
fi

echo "▸ pagination: Link headers and cursors collect exactly the 23 planted items"
python3 code/main.py paginate "$URL/items?per_page=100" --json --quiet > "$WORK/link-items.json" || fail "paginate by Link"
python3 code/main.py paginate "$URL/cursor?limit=7" --cursor --json --log "$WORK/cursor.jsonl" > "$WORK/cursor-items.json" || fail "paginate by cursor"
NEXT="$URL/items?per_page=4"
: > "$WORK/curl-items.jsonl"
PAGES=0
while [[ -n "$NEXT" && "$PAGES" -lt 50 ]]; do
  curl -sSf -D "$WORK/page.headers" -o "$WORK/page.json" "$NEXT" || fail "curl $NEXT"
  python3 -c 'import json, sys; [print(json.dumps(i)) for i in json.load(open(sys.argv[1]))]' "$WORK/page.json" >> "$WORK/curl-items.jsonl"
  NEXT="$(tr -d '\r' < "$WORK/page.headers" | sed -n 's/^[Ll]ink:.*<\([^>]*\)>; rel="next".*/\1/p')"
  PAGES=$((PAGES + 1))
done
python3 - "$WORK/link-items.json" "$WORK/cursor-items.json" "$WORK/curl-items.jsonl" "$WORK/cursor.jsonl" "$PAGES" <<'PY' || fail "pagination results differ from the planted items"
import json, sys
link = [i["id"] for i in json.load(open(sys.argv[1]))]
cursor = [i["id"] for i in json.load(open(sys.argv[2]))]
walked = [json.loads(line)["id"] for line in open(sys.argv[3]) if line.strip()]
pages = [json.loads(line) for line in open(sys.argv[4]) if '"page"' in line]
planted = list(range(1, 24))
assert link == planted, link
assert cursor == planted and len(pages) == 4, (cursor, pages)
assert walked == planted and int(sys.argv[5]) == 6, (walked, sys.argv[5])
print(f"  Link (per_page=100 asked, server caps at 10): 23 items; cursor (limit=7): 23 items in {len(pages)} pages; curl + sed walking rel=\"next\": 23 items in {sys.argv[5]} pages")
PY

echo "▸ conditional requests: a 304 costs no rate limit, for apiclient and for curl --etag-compare"
python3 - "$URL" <<'PY' || fail "apiclient conditional requests"
import json, sys
sys.path.insert(0, "code")
import main
base = sys.argv[1]
client = main.Client(main.RetryPolicy(retries=1, backoff_base=0.1), main.Timeouts(1, 5))
first = client.get(base + "/conditional")
second = client.get(base + "/conditional")
assert (first.from_cache, second.from_cache) == (False, True), (first, second)
assert first.header("x-ratelimit-remaining") == second.header("x-ratelimit-remaining"), (first.headers, second.headers)
assert second.json() == first.json()
client.request("PUT", base + "/conditional", body=json.dumps({"name": "cloudcaptain", "version": 2}).encode())
third = client.get(base + "/conditional")
assert not third.from_cache and third.json()["version"] == 2, third
print(f"  apiclient: 200 then 304 (served from cache, remaining stays {second.header('x-ratelimit-remaining')}); after a PUT the ETag changes and the new body arrives")
PY
curl -sS -o /dev/null -D "$WORK/etag1.headers" --etag-save "$WORK/etag.txt" "$URL/conditional" || fail "curl --etag-save"
CODE2="$(curl -sS -o /dev/null -D "$WORK/etag2.headers" -w '%{http_code}' --etag-compare "$WORK/etag.txt" "$URL/conditional")" || fail "curl --etag-compare"
R1="$(tr -d '\r' < "$WORK/etag1.headers" | sed -n 's/^[Xx]-[Rr]ate[Ll]imit-[Rr]emaining: //p')"
R2="$(tr -d '\r' < "$WORK/etag2.headers" | sed -n 's/^[Xx]-[Rr]ate[Ll]imit-[Rr]emaining: //p')"
[[ "$CODE2" == "304" && -n "$R1" && "$R1" == "$R2" ]] || fail "curl --etag-compare should get 304 with remaining unchanged: code $CODE2, remaining $R1 then $R2"
echo "  curl: --etag-save then --etag-compare -> 304, X-RateLimit-Remaining $R1 then $R2"

echo "▸ token bucket and circuit breaker against the live server"
python3 - "$URL" <<'PY' || fail "token bucket or circuit breaker"
import json, sys, time, urllib.request
sys.path.insert(0, "code")
import main
base = sys.argv[1]


def plan(name, steps):
    urllib.request.urlopen(urllib.request.Request(f"{base}/__plan/{name}", data=json.dumps({"plan": steps}).encode()), timeout=10).read()


def hits(name):
    return json.loads(urllib.request.urlopen(f"{base}/__hits/{name}", timeout=10).read())["hits"]


logs = []
client = main.Client(main.RetryPolicy(retries=0), main.Timeouts(1, 5), bucket=main.TokenBucket(rate=5, burst=2), log=logs.append)
plan("bucket", [])
for _ in range(7):
    client.request("GET", base + "/s/bucket")
seen = hits("bucket")
span = seen[-1]["t"] - seen[0]["t"]
throttled = sum(1 for r in logs if r["event"] == "throttle")
# span >= 0.9 is what proves the 5/s limit; the throttle count is 5 at full speed but lower if
# the runner stalls between requests (the bucket refills meanwhile), so it is only bounded.
assert len(seen) == 7 and span >= 0.9 and throttled <= 5, (len(seen), span, throttled)
print(f"  token bucket 5/s, burst 2: 7 requests spread over {span:.2f}s (ideal 1.00s), {throttled} throttled (5 at full speed)")

breaker = main.CircuitBreaker(failures=3, reset_after=1.0)
client = main.Client(main.RetryPolicy(retries=0), main.Timeouts(1, 5), breaker=breaker)
plan("down", [{"status": 503, "times": 4}])
url = base + "/s/down"
statuses = [client.request("GET", url).status for _ in range(3)]
assert statuses == [503, 503, 503] and breaker.current() == "open", (statuses, breaker.current())


def refused():
    try:
        client.request("GET", url)
    except main.CircuitOpenError:
        return True
    return False


def wait_half_open():
    deadline = time.monotonic() + 5
    while breaker.current() != "half-open":
        assert time.monotonic() < deadline, breaker.current()
        time.sleep(0.05)


assert refused() and len(hits("down")) == 3, "an open circuit must not reach the server"
opened = time.monotonic()
wait_half_open()
waited = time.monotonic() - opened
trial = client.request("GET", url).status
assert trial == 503 and breaker.current() == "open" and len(hits("down")) == 4, (trial, breaker.current())
assert refused() and len(hits("down")) == 4
wait_half_open()
assert client.request("GET", url).status == 200 and breaker.current() == "closed" and len(hits("down")) == 5
assert client.request("GET", url).status == 200 and len(hits("down")) == 6
print(f"  breaker: 3 x 503 -> open (4th call refused locally), half-open after {waited:.2f}s, failed trial reopens, successful trial closes")
PY

echo "▸ connection refused: retried even for a POST, because the request never left"
PORT="$(python3 -c 'import socket; s = socket.socket(); s.bind(("127.0.0.1", 0)); print(s.getsockname()[1]); s.close()')"
CODE=0
python3 code/main.py request POST "http://127.0.0.1:$PORT/orders" -d '{}' --retries 2 --backoff-base 0.05 --log "$WORK/refused.jsonl" > /dev/null 2> "$WORK/refused.err" || CODE=$?
ATTEMPTS="$(grep -c '"error_kind": "connect"' "$WORK/refused.jsonl" || true)"
[[ "$CODE" == "1" && "$ATTEMPTS" == "3" ]] && grep -q "after 3 attempt" "$WORK/refused.err" || fail "refused POST: exit $CODE, $ATTEMPTS connect attempts, $(cat "$WORK/refused.err")"
echo "  exit 1 after 3 connect attempts: $(cat "$WORK/refused.err")"

echo "SMOKE OK: scripting/api-automation"
