#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."

fail() { echo "SMOKE FAIL: $*" >&2; exit 1; }
WORK="$(mktemp -d)"
F="code/fixtures"
IMAGE="${PROMETHEUS_IMAGE:-prom/prometheus:v3.15.1}"
PROM="cc-smoke-promql-$$"
BASE=1767225600
MINI_PID=""
cleanup() {
  if [[ -n "$MINI_PID" ]]; then kill "$MINI_PID" >/dev/null 2>&1 || true; fi
  if command -v docker >/dev/null; then docker rm -f "$PROM" >/dev/null 2>&1 || true; fi
  rm -rf "$WORK" 2>/dev/null || true
}
trap cleanup EXIT

mini() { python3 code/main.py "$@"; }
free_port() { python3 -c 'import socket; s = socket.socket(); s.bind(("127.0.0.1", 0)); print(s.getsockname()[1]); s.close()'; }
wait_ready() {
  for _ in $(seq 1 90); do
    if python3 - "$1" >/dev/null 2>&1 <<'PY'
import sys, urllib.request
urllib.request.urlopen(sys.argv[1] + "/-/ready", timeout=2).read()
PY
    then
      return 0
    fi
    sleep 1
  done
  return 1
}
api_get() {
  local url="$1" out="$2"
  shift 2
  curl -sS -G "$url" "$@" -o "$out" || fail "curl $url: $(cat "$out" 2>/dev/null)"
}
vector_value() {
  python3 - "$@" <<'PY'
import json, sys
body = json.load(open(sys.argv[1]))
assert body.get("status") == "success", body
result = body["data"]["result"]
wanted = dict(pair.split("=", 1) for pair in sys.argv[2:])
matches = [r for r in result if all(r["metric"].get(k) == v for k, v in wanted.items())]
assert len(matches) == 1, (wanted, result)
print(matches[0]["value"][1])
PY
}
near() {
  python3 - "$1" "$2" <<'PY'
import sys
a, b = float(sys.argv[1]), float(sys.argv[2])
sys.exit(0 if abs(a - b) <= 1e-9 * max(1.0, abs(a), abs(b)) else 1)
PY
}

echo "▸ miniql generate: deterministic OpenMetrics (a counter reset, a late series, a histogram, an info metric)"
mini generate -o "$WORK/series.om"
tail -n 1 "$WORK/series.om" > "$WORK/last-line.txt"
[[ "$(cat "$WORK/last-line.txt")" == "# EOF" ]] || fail "the series file must end with # EOF, got: $(cat "$WORK/last-line.txt")"
mini generate -o "$WORK/again.om"
cmp -s "$WORK/series.om" "$WORK/again.om" || fail "two generator runs differ"
mini stats --series "$WORK/series.om" --json > "$WORK/stats.json"
SERIES="$(python3 -c 'import json, sys; print(json.load(open(sys.argv[1]))["series"])' "$WORK/stats.json")"
SAMPLES="$(python3 -c 'import json, sys; print(json.load(open(sys.argv[1]))["samples"])' "$WORK/stats.json")"
[[ "$SERIES" == "26" && "$SAMPLES" == "2036" ]] || fail "expected 26 series and 2036 samples: $(cat "$WORK/stats.json")"
echo "  $SERIES series, $SAMPLES samples"

echo "▸ miniql query: an extrapolated rate, an increase across the reset, and why a short window is empty"
mini query --series "$WORK/series.om" --at +15m --json 'rate(cc_http_requests_total{instance="a",code="200"}[5m])' > "$WORK/q-rate.json"
RATE="$(vector_value "$WORK/q-rate.json" instance=a code=200)"
near "$RATE" 1 || fail "rate at +15m should be 1, got $RATE"
mini query --series "$WORK/series.om" --at +22m --explain 'increase(cc_http_requests_total{instance="b",code="200"}[5m])' > "$WORK/explain.txt"
cat "$WORK/explain.txt"
grep -q "=> 555.5555555555555" "$WORK/explain.txt" || fail "increase across the reset should be 500 x 300 / 270: $(cat "$WORK/explain.txt")"
grep -q "plus 2840 added back for 1 counter reset(s)" "$WORK/explain.txt" || fail "explain should show the reset correction: $(cat "$WORK/explain.txt")"
mini query --series "$WORK/series.om" --at +15m --explain 'rate(cc_http_requests_total{instance="a"}[30s])' > "$WORK/short.txt"
grep -q "(empty vector)" "$WORK/short.txt" && grep -q "two are needed" "$WORK/short.txt" || fail "a 30s window over 30s scrapes should be empty with a reason: $(cat "$WORK/short.txt")"

echo "▸ miniql test: the promtool unit-test file passes, a copy with one wrong expectation fails"
mini test "$F/rules-test.json" > "$WORK/mini-test.txt" || fail "miniql test: $(cat "$WORK/mini-test.txt")"
grep -q "^  SUCCESS$" "$WORK/mini-test.txt" || fail "miniql test should report SUCCESS: $(cat "$WORK/mini-test.txt")"
cp "$F/rules.json" "$WORK/rules.json"
python3 - "$F/rules-test.json" "$WORK/rules-test-broken.json" <<'PY'
import json, sys
spec = json.load(open(sys.argv[1]))
case = spec["tests"][0]["promql_expr_test"][2]
assert case["expr"] == 'rate(cc_requests_total{instance="b"}[5m])', case
case["exp_samples"][0]["value"] = 2
json.dump(spec, open(sys.argv[2], "w"), indent=2)
PY
CODE=0
mini test "$WORK/rules-test-broken.json" > "$WORK/mini-broken.txt" || CODE=$?
[[ "$CODE" == "1" ]] && grep -q "FAILED:" "$WORK/mini-broken.txt" && grep -q "1.5" "$WORK/mini-broken.txt" || fail "the broken copy should fail with got 1.5 (exit $CODE): $(cat "$WORK/mini-broken.txt")"

echo "▸ miniql serve: Prometheus' query API, checked with curl"
MINI_PORT="$(free_port)"
python3 code/main.py serve --series "$WORK/series.om" --listen "127.0.0.1:$MINI_PORT" > "$WORK/serve.log" 2>&1 &
MINI_PID=$!
disown "$MINI_PID" 2>/dev/null || true
wait_ready "http://127.0.0.1:$MINI_PORT" || fail "miniql serve did not answer: $(cat "$WORK/serve.log")"
MINI_URL="http://127.0.0.1:$MINI_PORT"
api_get "$MINI_URL/api/v1/query" "$WORK/mini-increase.json" --data-urlencode 'query=increase(cc_http_requests_total{instance="b",code="200"}[5m])' --data-urlencode "time=$((BASE + 1320))"
MINI_INCREASE="$(vector_value "$WORK/mini-increase.json" instance=b code=200)"
near "$MINI_INCREASE" 555.5555555555555 || fail "miniql serve increase: $(cat "$WORK/mini-increase.json")"
api_get "$MINI_URL/api/v1/query" "$WORK/mini-bad.json" --data-urlencode 'query=rate(sum(cc_http_requests_total)[5m])'
python3 - "$WORK/mini-bad.json" <<'PY' || fail "a bad query should be bad_data: $(cat "$WORK/mini-bad.json")"
import json, sys
body = json.load(open(sys.argv[1]))
assert body["status"] == "error" and body["errorType"] == "bad_data", body
PY
mini compare --series "$WORK/series.om" --queries "$F/queries.json" --url "$MINI_URL" > "$WORK/self-compare.txt" || fail "miniql against itself: $(cat "$WORK/self-compare.txt")"
tail -n 1 "$WORK/self-compare.txt"

if ! command -v docker >/dev/null; then
  echo "  (docker not installed; the Prometheus half is skipped)"
  echo "SMOKE OK: observability/promql (miniql only)"
  exit 0
fi

echo "▸ promtool tsdb create-blocks-from openmetrics: backfill the same file ($IMAGE)"
docker pull -q "$IMAGE" > "$WORK/pull.txt" 2>&1 || fail "docker pull $IMAGE: $(cat "$WORK/pull.txt")"
USER_FLAG="$(id -u):$(id -g)"
mkdir -p "$WORK/data"
docker run --rm --user "$USER_FLAG" -v "$WORK:/work" --entrypoint /bin/promtool "$IMAGE" \
  tsdb create-blocks-from openmetrics /work/series.om /work/data > "$WORK/backfill.txt" 2>&1 || fail "backfill: $(cat "$WORK/backfill.txt")"
python3 - "$WORK/data" "$WORK/stats.json" <<'PY' || fail "backfilled blocks: $(cat "$WORK/backfill.txt"; ls -la "$WORK/data")"
import glob, json, os, sys
metas = [json.load(open(path)) for path in glob.glob(os.path.join(sys.argv[1], "*", "meta.json"))]
assert metas, "promtool wrote no block"
stats = json.load(open(sys.argv[2]))
samples = sum(m["stats"]["numSamples"] for m in metas)
assert samples == stats["samples"], ("samples", samples, stats["samples"])
if len(metas) == 1:
    assert metas[0]["stats"]["numSeries"] == stats["series"], ("series", metas[0]["stats"], stats["series"])
print(f"  {len(metas)} block(s), {samples} samples, {metas[0]['stats']['numSeries']} series: the counts miniql loaded")
PY

echo "▸ promtool check rules and promtool test rules on the same fixtures miniql tested"
PROMTOOL=(docker run --rm --user "$USER_FLAG" -v "$PWD/$F:/fixtures:ro" -v "$WORK:/work" --entrypoint /bin/promtool "$IMAGE")
"${PROMTOOL[@]}" check rules /fixtures/rules.json > "$WORK/check-rules.txt" 2>&1 || fail "promtool check rules: $(cat "$WORK/check-rules.txt")"
grep -q "SUCCESS" "$WORK/check-rules.txt" || fail "promtool check rules: $(cat "$WORK/check-rules.txt")"
sed 's/^/  /' "$WORK/check-rules.txt"
"${PROMTOOL[@]}" test rules /fixtures/rules-test.json > "$WORK/prom-test.txt" 2>&1 || fail "promtool test rules should pass where miniql test passes: $(cat "$WORK/prom-test.txt")"
grep -q "SUCCESS" "$WORK/prom-test.txt" || fail "promtool test rules: $(cat "$WORK/prom-test.txt")"
CODE=0
"${PROMTOOL[@]}" test rules /work/rules-test-broken.json > "$WORK/prom-broken.txt" 2>&1 || CODE=$?
[[ "$CODE" != "0" ]] && grep -q "FAILED" "$WORK/prom-broken.txt" || fail "promtool should fail the broken copy like miniql (exit $CODE): $(cat "$WORK/prom-broken.txt")"
grep -q "1.5" "$WORK/prom-broken.txt" || fail "promtool should report the value it got, 1.5: $(cat "$WORK/prom-broken.txt")"
echo "  both pass the fixture and both fail the copy that expects 2 instead of 1.5"

echo "▸ Prometheus serves the backfilled block"
PORT="$(free_port)"
printf 'global:\n  scrape_interval: 1m\n' > "$WORK/prometheus.yml"
docker run -d --name "$PROM" --user "$USER_FLAG" -p "127.0.0.1:$PORT:9090" -v "$WORK:/work" "$IMAGE" \
  --config.file=/work/prometheus.yml --storage.tsdb.path=/work/data --storage.tsdb.retention.time=36500d > "$WORK/run.txt" 2>&1 \
  || fail "docker run $IMAGE: $(cat "$WORK/run.txt")"
PROM_URL="http://127.0.0.1:$PORT"
if ! wait_ready "$PROM_URL"; then
  docker logs "$PROM" > "$WORK/prometheus.log" 2>&1 || true
  tail -n 30 "$WORK/prometheus.log" >&2
  fail "Prometheus did not become ready on port $PORT"
fi
api_get "$PROM_URL/api/v1/query" "$WORK/count.json" --data-urlencode 'query=count({__name__=~"cc_.+"})' --data-urlencode "time=$((BASE + 1700))"
LOADED="$(python3 -c 'import json, sys; r = json.load(open(sys.argv[1]))["data"]["result"]; print(r[0]["value"][1] if r else 0)' "$WORK/count.json")"
[[ "$LOADED" == "26" ]] || fail "Prometheus should see 26 series at +1700s, got $LOADED: $(cat "$WORK/count.json")"
echo "  $LOADED series visible at +1700s"

echo "▸ miniql compare: the fixture's queries through miniql and Prometheus at fixed evaluation times"
CODE=0
mini compare --series "$WORK/series.om" --queries "$F/queries.json" --url "$PROM_URL" --json > "$WORK/compare.json" || CODE=$?
python3 - "$WORK/compare.json" "$CODE" <<'PY' || fail "miniql and Prometheus disagree (see above)"
import json, sys
report = json.load(open(sys.argv[1]))
for r in report["results"]:
    print(("  agree " if r["agree"] else "  DIFF  ") + r["name"].ljust(40) + " " + r["when"].ljust(22) + " " + r["detail"][:150])
bad = [r for r in report["results"] if not r["agree"]]
for r in bad:
    print("    miniql:     " + json.dumps(r["miniql"])[:600])
    print("    prometheus: " + json.dumps(r["prometheus"])[:600])
assert not bad and sys.argv[2] == "0", f"{len(bad)} queries differ"
assert report["total"] >= 25, report["total"]
by_name = {r["name"]: r for r in report["results"]}
for name in ("extrapolated rate", "increase across the reset", "rate of a series that starts", "histogram_quantile, summed buckets",
             "histogram_quantile across the reset", "group_left join", "group_right join", "range: rate per instance", "range: p90 per instance"):
    assert by_name[name]["prometheus"]["data"]["result"], f"{name}: Prometheus returned no series, so agreement proves nothing"
for name in ("sum before rate is a parse error", "selector without a non-empty matcher"):
    assert by_name[name]["prometheus"].get("errorType") == "bad_data", (name, by_name[name]["prometheus"])
assert by_name["many-to-many is an error"]["prometheus"].get("errorType") == "execution", by_name["many-to-many is an error"]["prometheus"]
assert by_name["lookback is left-open"]["prometheus"]["data"]["result"] == [], "a sample exactly 5m old must be outside the lookback"
joined = by_name["group_left join"]["prometheus"]["data"]["result"]
assert all({"instance", "version", "zone"} <= set(s["metric"]) for s in joined), joined
print(f"  {report['agree']} of {report['total']} queries agree")
PY

echo "▸ Use It: curl the query API and promtool query, and check the hand-computed answers"
api_get "$PROM_URL/api/v1/query" "$WORK/p-rate.json" --data-urlencode 'query=rate(cc_http_requests_total{instance="a",code="200"}[5m])' --data-urlencode "time=$((BASE + 900))"
P_RATE="$(vector_value "$WORK/p-rate.json" instance=a code=200)"
near "$P_RATE" 1 || fail "Prometheus rate should be 270 x 300/270 / 300 s = 1, got $P_RATE"
api_get "$PROM_URL/api/v1/query" "$WORK/p-increase.json" --data-urlencode 'query=increase(cc_http_requests_total{instance="b",code="200"}[5m])' --data-urlencode 'time=2026-01-01T00:22:00Z'
P_INCREASE="$(vector_value "$WORK/p-increase.json" instance=b code=200)"
near "$P_INCREASE" "$MINI_INCREASE" || fail "increase across the reset: Prometheus $P_INCREASE, miniql $MINI_INCREASE"
echo "  rate $P_RATE, increase across the reset $P_INCREASE (miniql $MINI_INCREASE)"
api_get "$PROM_URL/api/v1/query" "$WORK/p-gauge.json" --data-urlencode 'query=rate(cc_queue_depth[5m])' --data-urlencode "time=$((BASE + 1200))"
python3 - "$WORK/p-gauge.json" <<'PY' || fail "rate() of a gauge: $(cat "$WORK/p-gauge.json")"
import json, sys
body = json.load(open(sys.argv[1]))
notes = " ".join(body.get("infos", []) + body.get("warnings", []))
print("  rate of a gauge: " + (notes[:140] if notes else "(no annotation in this version)"))
assert len(body["data"]["result"]) == 2, body
PY
api_get "$PROM_URL/api/v1/query_range" "$WORK/p-range.json" --data-urlencode 'query=sum by (instance) (rate(cc_http_requests_total[5m]))' \
  --data-urlencode "start=$((BASE + 900))" --data-urlencode "end=$((BASE + 1800))" --data-urlencode step=60
python3 - "$WORK/p-range.json" <<'PY' || fail "query_range: $(head -c 800 "$WORK/p-range.json")"
import json, sys
body = json.load(open(sys.argv[1]))
assert body["data"]["resultType"] == "matrix", body["data"]["resultType"]
points = {s["metric"]["instance"]: len(s["values"]) for s in body["data"]["result"]}
assert points == {"a": 16, "b": 16, "c": 5}, points
print(f"  query_range points per instance: {points}")
PY
docker exec "$PROM" /bin/promtool query instant --time "$((BASE + 900))" http://127.0.0.1:9090 'rate(cc_http_requests_total{instance="a",code="200"}[5m])' > "$WORK/promtool-query.txt" 2>&1 \
  || fail "promtool query instant: $(cat "$WORK/promtool-query.txt")"
python3 - "$WORK/promtool-query.txt" <<'PY' || fail "promtool query instant should print 1 for instance a: $(cat "$WORK/promtool-query.txt")"
import re, sys
text = open(sys.argv[1]).read()
values = [float(v) for v in re.findall(r"=>\s*([-+0-9.eE]+)", text)]
assert 'instance="a"' in text and values and abs(values[0] - 1) < 1e-9, text
print("  promtool query instant: " + text.strip().splitlines()[0][:140])
PY

echo "SMOKE OK: observability/promql"
