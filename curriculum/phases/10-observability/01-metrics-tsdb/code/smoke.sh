#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."

fail() { echo "SMOKE FAIL: $*" >&2; exit 1; }
WORK="$(mktemp -d)"
F="code/fixtures"
PROM_IMAGE="prom/prometheus:v3.15.1"
NODE_IMAGE="prom/node-exporter:v1.12.1"
PROM_NAME="cc-metrics-tsdb-prom"
NODE_NAME="cc-metrics-tsdb-node"
PIDS=()
CONTAINERS=()

cleanup() {
  set +e
  for pid in ${PIDS[@]+"${PIDS[@]}"}; do
    kill "$pid" 2>/dev/null
    wait "$pid" 2>/dev/null
  done
  for name in ${CONTAINERS[@]+"${CONTAINERS[@]}"}; do docker rm -f -v "$name" >/dev/null 2>&1; done
  rm -rf "$WORK"
}
trap cleanup EXIT

mini() { python3 code/main.py "$@"; }

echo "▸ minitsdb parse: the exposition-format example, escapes decoded, le=\"1\" stored as le=\"1.0\""
mini parse "$F/sample.prom" --json > "$WORK/sample.json" || fail "parse sample.prom: $(cat "$WORK/sample.json")"
python3 - "$WORK/sample.json" <<'PY' || fail "sample.prom parsed wrongly: $(head -c 2000 "$WORK/sample.json")"
import json, sys
doc = json.load(open(sys.argv[1]))
assert doc["format"] == "text" and doc["problems"] == [], doc["problems"]
got = [(f["name"], f["type"], f["series"]) for f in doc["families"]]
assert got == [("http_requests_total", "counter", 2), ("msdos_file_access_time_seconds", "gauge", 1),
               ("metric_without_timestamp_and_labels", "gauge", 1), ("something_weird", "untyped", 1),
               ("http_request_duration_seconds", "histogram", 8), ("rpc_duration_seconds", "summary", 7)], got
les = [s["labels"]["le"] for s in doc["families"][4]["samples"] if "le" in s["labels"]]
assert les == ["0.05", "0.1", "0.2", "0.5", "1.0", "+Inf"], les
assert doc["families"][1]["samples"][0]["labels"]["error"] == 'Cannot find file:\n"FILE.TXT"'
print("  6 families, 20 samples; le values:", " ".join(les))
PY

echo "▸ minitsdb parse: OpenMetrics with _created, exemplars and # EOF; a page without # EOF is an error"
mini parse "$F/openmetrics.txt" --json > "$WORK/om.json" || fail "parse openmetrics.txt: $(cat "$WORK/om.json")"
python3 - "$WORK/om.json" <<'PY' || fail "openmetrics.txt parsed wrongly: $(head -c 2000 "$WORK/om.json")"
import json, sys
doc = json.load(open(sys.argv[1]))
fams = {f["name"]: f for f in doc["families"]}
assert doc["format"] == "openmetrics", doc["format"]
assert [s["name"] for s in fams["process_cpu_seconds"]["samples"]] == ["process_cpu_seconds_total", "process_cpu_seconds_created"]
bucket = [s for s in fams["request_duration_seconds"]["samples"] if s["labels"].get("le") == "1.0"][0]
assert bucket["exemplar"]["labels"] == {"trace_id": "0d482-ac43e"}, bucket
print("  exemplar on le=1.0:", bucket["exemplar"])
PY
sed '$d' "$F/openmetrics.txt" > "$WORK/truncated.txt"
CODE=0
mini parse "$WORK/truncated.txt" --format openmetrics > /dev/null 2> "$WORK/truncated.err" || CODE=$?
[[ "$CODE" == "2" ]] && grep -q "must end with # EOF" "$WORK/truncated.err" || fail "a truncated OpenMetrics page should exit 2: code $CODE, $(cat "$WORK/truncated.err")"

echo "▸ minitsdb parse: a broken classic histogram is reported with line numbers"
CODE=0
mini parse "$F/bad-histogram.prom" > "$WORK/bad.txt" || CODE=$?
[[ "$CODE" == "1" ]] || fail "bad-histogram.prom should exit 1: code $CODE, $(cat "$WORK/bad.txt")"
for words in 'no le="+Inf" bucket' 'buckets are cumulative' '_count is 3 but the +Inf bucket holds 2' 'is negative'; do
  grep -qF "$words" "$WORK/bad.txt" || fail "expected '$words' in: $(cat "$WORK/bad.txt")"
done
grep '^problem' "$WORK/bad.txt" | sed 's/^/  /'

echo "▸ minitsdb encode: a series scraped on time with an unchanged value costs 2 bits per sample"
python3 -c 'print(" ".join("%d:1" % (1700000000000 + 15000 * i) for i in range(120)))' > "$WORK/samples.txt"
read -r -a SAMPLES < "$WORK/samples.txt"
mini encode "${SAMPLES[@]}" --json > "$WORK/encode.json" || fail "encode: $(head -c 1000 "$WORK/encode.json")"
python3 - "$WORK/encode.json" <<'PY' || fail "unexpected chunk costs: $(head -c 1500 "$WORK/encode.json")"
import json, sys
doc = json.load(open(sys.argv[1]))
costs = {(r["timestamp_bits"], r["value_bits"]) for r in doc["samples"][2:]}
assert costs == {(1, 1)}, costs
assert doc["bytes"] <= 50 and len(doc["decoded"]) == 120, doc["bytes"]
print("  120 samples in %d bytes (raw %d)" % (doc["bytes"], doc["raw_bytes"]))
PY

if ! command -v docker >/dev/null; then
  echo "  (docker not installed; the Prometheus comparison is skipped)"
  echo "SMOKE OK: observability/metrics-tsdb (minitsdb only)"
  exit 0
fi

PORTS="$(python3 -c 'import socket; s=[socket.socket() for _ in range(4)]; [x.bind(("127.0.0.1", 0)) for x in s]; print(" ".join(str(x.getsockname()[1]) for x in s))')"
read -r PA PB PP PN <<< "$PORTS"
PROM="http://127.0.0.1:$PP"

wait_http() {
  local deadline=$((SECONDS + $2))
  until curl -fsS -o /dev/null "$1" 2>/dev/null; do
    (( SECONDS < deadline )) || return 1
    sleep 0.3
  done
}
prom_logs() {
  docker logs "$PROM_NAME" > "$WORK/prom-logs.txt" 2>&1 || true
  tail -n 25 "$WORK/prom-logs.txt"
}
promq() {
  local args=(--get --data-urlencode "query=$1")
  if [[ -n "${2:-}" ]]; then args+=(--data-urlencode "time=$2"); fi
  curl -fsS "${args[@]}" "$PROM/api/v1/query"
}
miniq() {
  local args=(query --data "$WORK/data" "$1" --json)
  if [[ -n "${2:-}" ]]; then args+=(--time "$2"); fi
  python3 code/main.py "${args[@]}" 2>> "$WORK/mini-warnings.txt"
}
value_of() {
  python3 - "$1" <<'PY'
import json, sys
try:
    result = json.load(open(sys.argv[1]))["data"]["result"]
except Exception as error:
    print("error:" + type(error).__name__)
    raise SystemExit(0)
print("none" if not result else result[0]["value"][1] if len(result) == 1 else "many:%d" % len(result))
PY
}
both() {
  promq "$1" > "$WORK/both-prom.json" 2>/dev/null || echo '{}' > "$WORK/both-prom.json"
  miniq "$1" > "$WORK/both-mini.json" || echo '{}' > "$WORK/both-mini.json"
  echo "$(value_of "$WORK/both-prom.json") $(value_of "$WORK/both-mini.json")"
}
wait_both() {
  local deadline=$((SECONDS + $1)) got=""
  while (( SECONDS < deadline )); do
    got="$(both "$2")"
    if [[ "$got" == "$3 $3" ]]; then
      echo "$got"
      return 0
    fi
    sleep 0.5
  done
  echo "$got"
  return 1
}
wait_lines() {
  local deadline=$((SECONDS + $3)) count=0
  while (( SECONDS < deadline )); do
    count="$(grep -cF -- "$1" "$WORK/scrape.txt" || true)"
    [[ "$count" -ge "$2" ]] && return 0
    sleep 0.5
  done
  return 1
}

echo "▸ two demo apps on 127.0.0.1:$PA and 127.0.0.1:$PB, one config file for both scrapers"
python3 code/main.py demo --port "$PA" > "$WORK/app-a.txt" 2>&1 &
PIDS+=("$!")
python3 code/main.py demo --port "$PB" > "$WORK/app-b.txt" 2>&1 &
APP_B="$!"
PIDS+=("$APP_B")
wait_http "http://127.0.0.1:$PA/metrics" 20 || fail "demo app A did not start: $(cat "$WORK/app-a.txt")"
wait_http "http://127.0.0.1:$PB/metrics" 20 || fail "demo app B did not start: $(cat "$WORK/app-b.txt")"
mkdir -p "$WORK/prom"
python3 - "$F/prometheus.json" "$WORK/prom/prometheus.yml" "$PA" "$PB" <<'PY' || fail "could not write the config"
import json, sys
source, target, a, b = sys.argv[1:]
doc = json.load(open(source))
ports = {"127.0.0.1:18481": "127.0.0.1:" + a, "127.0.0.1:18482": "127.0.0.1:" + b}
for job in doc["scrape_configs"]:
    for group in job["static_configs"]:
        group["targets"] = [ports.get(t, t) for t in group["targets"]]
json.dump(doc, open(target, "w"), indent=2)
PY
chmod 0755 "$WORK" "$WORK/prom"
chmod 0644 "$WORK/prom/prometheus.yml"
mini targets --config "$WORK/prom/prometheus.yml" > "$WORK/targets.txt" || fail "minitsdb targets: $(cat "$WORK/targets.txt")"
sed 's/^/  /' "$WORK/targets.txt"

echo "▸ Prometheus $PROM_IMAGE on 127.0.0.1:$PP reads the same file (JSON is YAML)"
docker pull -q "$PROM_IMAGE" > "$WORK/pull.txt" 2>&1 || fail "docker pull $PROM_IMAGE: $(cat "$WORK/pull.txt")"
docker run --rm -v "$WORK/prom:/etc/cc:ro" --entrypoint promtool "$PROM_IMAGE" check config /etc/cc/prometheus.yml > "$WORK/check-config.txt" 2>&1 \
  || fail "promtool check config rejected the JSON config: $(cat "$WORK/check-config.txt")"
docker rm -f -v "$PROM_NAME" > /dev/null 2>&1 || true
CONTAINERS+=("$PROM_NAME")
docker run -d --name "$PROM_NAME" --network host -v "$WORK/prom:/etc/cc:ro" "$PROM_IMAGE" \
  --config.file=/etc/cc/prometheus.yml --storage.tsdb.path=/prometheus \
  --web.listen-address="127.0.0.1:$PP" --web.enable-admin-api > "$WORK/prom-run.txt" 2>&1 \
  || fail "docker run $PROM_IMAGE: $(cat "$WORK/prom-run.txt")"
wait_http "$PROM/-/ready" 90 || fail "Prometheus never became ready: $(prom_logs)"

echo "▸ minitsdb scrape runs next to Prometheus against the same targets"
python3 code/main.py scrape --config "$WORK/prom/prometheus.yml" --data "$WORK/data" --duration 240 > "$WORK/scrape.txt" 2>&1 &
MINI_PID="$!"
PIDS+=("$MINI_PID")
for port in "$PA" "$PB"; do
  OUT="$(wait_both 60 "up{job=\"demo\",instance=\"127.0.0.1:$port\"}" 1)" \
    || fail "up for 127.0.0.1:$port should be 1 in both (prometheus minitsdb): $OUT; minitsdb: $(tail -n 3 "$WORK/scrape.txt"); prometheus: $(prom_logs)"
done
OUT="$(wait_both 30 'up{job="demo-honor"}' 1)" || fail "up for job demo-honor should be 1 in both: $OUT"
echo "  up (prometheus minitsdb): $OUT for all three targets"

echo "▸ staleness: target A stops exposing cc_queue_depth{queue=\"batch\"}"
SEL_BATCH="cc_queue_depth{job=\"demo\",instance=\"127.0.0.1:$PA\",queue=\"batch\"}"
OUT="$(wait_both 30 "$SEL_BATCH" 17)" || fail "before hiding, both should read 17: $OUT"
curl -fsS -X POST "http://127.0.0.1:$PA/hide?queue=batch" > /dev/null || fail "POST /hide on target A"
OUT="$(wait_both 30 "$SEL_BATCH" none)" || fail "after hiding, an instant query must return nothing in both well inside the 5m lookback (staleness marker): $OUT"
echo "  both return no value within one scrape (prometheus minitsdb): $OUT"

echo "▸ target B dies: up drops to 0 and all of B's series go stale in both"
kill "$APP_B"
wait "$APP_B" 2>/dev/null || true
OUT="$(wait_both 30 "up{job=\"demo\",instance=\"127.0.0.1:$PB\"}" 0)" || fail "up for the dead target should be 0 in both: $OUT"
OUT="$(wait_both 15 "cc_queue_depth{job=\"demo\",instance=\"127.0.0.1:$PB\",queue=\"default\"}" none)" || fail "series of the dead target should be stale in both: $OUT"
wait_lines "127.0.0.1:$PB up=0" 3 30 || fail "minitsdb should log three failed scrapes of B: $(tail -n 8 "$WORK/scrape.txt")"
kill -TERM "$MINI_PID"
CODE=0
wait "$MINI_PID" || CODE=$?
[[ "$CODE" == "0" ]] || fail "minitsdb scrape should exit 0 on SIGTERM, got $CODE: $(tail -n 20 "$WORK/scrape.txt")"
grep -qF "error: Get \"http://127.0.0.1:$PB/metrics\"" "$WORK/scrape.txt" || fail "minitsdb should log why B failed: $(tail -n 8 "$WORK/scrape.txt")"
tail -n 1 "$WORK/scrape.txt" | sed 's/^/  /'

echo "▸ the same series set from /api/v1/series and minitsdb series"
NOW="$(date +%s)"
curl -fsS --get --data-urlencode 'match[]={instance=~"127.0.0.1:.*"}' --data-urlencode "start=$((NOW - 3600))" --data-urlencode "end=$((NOW + 60))" \
  "$PROM/api/v1/series" > "$WORK/series-prom.json" || fail "/api/v1/series: $(cat "$WORK/series-prom.json")"
mini series --data "$WORK/data" --match '{instance=~"127.0.0.1:.*"}' --json > "$WORK/series-mini.json" 2>> "$WORK/mini-warnings.txt" \
  || fail "minitsdb series: $(cat "$WORK/mini-warnings.txt")"
python3 - "$WORK/series-prom.json" "$WORK/series-mini.json" "$PB" <<'PY' || fail "the series sets differ (details above)"
import json, sys
prom = {tuple(sorted(s.items())) for s in json.load(open(sys.argv[1]))["data"]}
mini = {tuple(sorted(s.items())) for s in json.load(open(sys.argv[2]))["data"]}
only_prom, only_mini = sorted(prom - mini), sorted(mini - prom)
for s in only_prom[:20]:
    print("  only in Prometheus:", dict(s))
for s in only_mini[:20]:
    print("  only in minitsdb:", dict(s))
assert not only_mini, "minitsdb stored series Prometheus did not"
assert all(dict(s)["__name__"].startswith("scrape_") for s in only_prom), "Prometheus stored application series minitsdb did not"
both = [dict(s) for s in prom & mini]
names = {s["__name__"] for s in both}
assert not any(n.startswith("cc_debug_") for n in names), "metric_relabel_configs should drop cc_debug_*"
assert any(s["__name__"] == "cc_build_info" and s.get("exported_job") == "batch-runner" and s["job"] == "demo" for s in both), "honor_labels: false should rename job to exported_job"
assert any(s["__name__"] == "cc_build_info" and s["job"] == "batch-runner" for s in both), "honor_labels: true should keep the scraped job"
assert any(s.get("le") == "1.0" for s in both), "le should be normalised to 1.0"
assert any(s.get("queue") == 'email "bulk"' for s in both), "escaped label values should round-trip"
assert any(s["__name__"] == "cc_queue_depth" and s.get("queue") == "batch" for s in both), "the hidden series still exists, it is only stale"
assert all(s.get("team") == "payments" and s.get("port") for s in both if s["job"] == "demo"), "labelmap and replace should add team and port"
print(f"  {len(mini)} series in minitsdb, {len(prom)} in Prometheus, {len(prom & mini)} identical; no cc_debug_*, exported_job and le=1.0 in both")
PY

echo "▸ values at minitsdb's last scrape: static series equal, growing counters within one scrape interval"
mini dump --data "$WORK/data" --match "up{job=\"demo\",instance=\"127.0.0.1:$PA\"}" --json > "$WORK/up-a.json" 2>> "$WORK/mini-warnings.txt" || fail "minitsdb dump"
T_END="$(python3 -c 'import json, sys; s = json.load(open(sys.argv[1]))[0]["samples"]; print("%.3f" % (s[-1][0] / 1000))' "$WORK/up-a.json")" || fail "no up samples for A: $(cat "$WORK/up-a.json")"
SEL_A="{job=\"demo\",instance=\"127.0.0.1:$PA\"}"
promq "$SEL_A" "$T_END" > "$WORK/values-prom.json" || fail "query $SEL_A at $T_END"
miniq "$SEL_A" "$T_END" > "$WORK/values-mini.json" || fail "minitsdb query $SEL_A at $T_END"
python3 - "$WORK/values-prom.json" "$WORK/values-mini.json" <<'PY' || fail "values differ (details above)"
import json, sys
def load(path):
    return {tuple(sorted(r["metric"].items())): float(r["value"][1]) for r in json.load(open(path))["data"]["result"]}
prom, mini = load(sys.argv[1]), load(sys.argv[2])
assert set(prom) == set(mini), sorted(set(prom) ^ set(mini))[:10]
rates = {("cc_http_requests_total", "GET", "200"): 10, ("cc_http_requests_total", "POST", "200"): 2, ("cc_jobs_processed", "", ""): 1}
bad = []
for key, value in sorted(mini.items()):
    labels = dict(key)
    if labels["__name__"] in ("scrape_duration_seconds", "scrape_series_added"):
        continue
    rate = rates.get((labels["__name__"], labels.get("method", ""), labels.get("code", "")), 0)
    if abs(prom[key] - value) > rate * 3:
        bad.append((labels, prom[key], value))
for row in bad:
    print("  differs (labels, prometheus, minitsdb):", row)
assert not bad
by_name = {dict(k)["__name__"]: v for k, v in mini.items()}
assert (by_name["scrape_samples_scraped"], by_name["scrape_samples_post_metric_relabeling"]) == (68.0, 64.0), by_name
print(f"  {len(mini)} series agree; scrape_samples_scraped 68 and post_metric_relabeling 64 in both (4 cc_debug_ series dropped)")
PY
curl -fsS "http://127.0.0.1:$PA/metrics" > "$WORK/demo.prom" || fail "fetch the demo page"
DEBUG="$(grep -c '^cc_debug_allocations_total' "$WORK/demo.prom" || true)"
[[ "$DEBUG" == "4" ]] || fail "the raw page should still expose 4 cc_debug series, found $DEBUG"

echo "▸ the dropped and relabelled targets: /api/v1/targets against minitsdb targets"
curl -fsS "$PROM/api/v1/targets" > "$WORK/targets-prom.json" || fail "/api/v1/targets"
mini targets --config "$WORK/prom/prometheus.yml" --json > "$WORK/targets-mini.json" || fail "minitsdb targets --json"
python3 - "$WORK/targets-prom.json" "$WORK/targets-mini.json" "$PB" <<'PY' || fail "targets differ: $(head -c 3000 "$WORK/targets-prom.json")"
import json, sys
prom, mini = (json.load(open(p))["data"] for p in sys.argv[1:3])
def active(doc):
    return {(t["scrapePool"], tuple(sorted(t["labels"].items()))) for t in doc["activeTargets"]}
def dropped(doc):
    return {(t["scrapePool"], t["discoveredLabels"]["__address__"]) for t in doc["droppedTargets"]}
assert active(prom) == active(mini), (sorted(active(prom)), sorted(active(mini)))
assert dropped(prom) == dropped(mini) == {("demo", "127.0.0.1:9")}, (dropped(prom), dropped(mini))
dead = [t for t in prom["activeTargets"] if t["labels"]["instance"] == "127.0.0.1:" + sys.argv[3]][0]
assert dead["health"] == "down", dead
print(f"  {len(active(mini))} active targets with identical labels, 1 dropped; Prometheus says B is down: {dead['lastError'][:80]}")
PY

echo "▸ the staleness marker itself, and retention by whole chunks"
mini dump --data "$WORK/data" --match "$SEL_BATCH" --json > "$WORK/dump-batch.json" 2>> "$WORK/mini-warnings.txt" || fail "dump $SEL_BATCH"
python3 - "$WORK/dump-batch.json" <<'PY' || fail "expected 17 then a staleness marker: $(head -c 1500 "$WORK/dump-batch.json")"
import json, sys
samples = json.load(open(sys.argv[1]))[0]["samples"]
assert samples[-1][2] is True and samples[-2][1] == "17" and not any(s[2] for s in samples[:-1]), samples[-4:]
print("  last samples:", samples[-2:])
PY
mini series --data "$WORK/data" --match "{instance=\"127.0.0.1:$PB\"}" --retention 3s --json > "$WORK/retained.json" 2>> "$WORK/mini-warnings.txt" || fail "series --retention"
python3 - "$WORK/retained.json" <<'PY' || fail "after a 3s retention only B's report series should remain: $(head -c 1500 "$WORK/retained.json")"
import json, sys
names = sorted({s["__name__"] for s in json.load(open(sys.argv[1]))["data"]})
assert names == sorted(["up", "scrape_duration_seconds", "scrape_samples_scraped", "scrape_samples_post_metric_relabeling", "scrape_series_added"]), names
print("  B with --retention 3s:", ", ".join(names))
PY
mini chunks --data "$WORK/data" --json > "$WORK/chunks.json" || fail "minitsdb chunks"
python3 - "$WORK/chunks.json" <<'PY' || fail "compression worse than expected: $(head -c 800 "$WORK/chunks.json")"
import json, sys
stats = json.load(open(sys.argv[1]))
assert 0 < stats["bytes_per_sample"] < 8, stats["bytes_per_sample"]
print(f"  {stats['samples']} samples in {stats['bytes']} bytes: {stats['bytes_per_sample']} bytes per sample (raw 16)")
PY

echo "▸ cardinality: minitsdb against /api/v1/status/tsdb and promtool tsdb analyze on a snapshot"
curl -fsS "$PROM/api/v1/status/tsdb" > "$WORK/status-tsdb.json" || fail "/api/v1/status/tsdb"
curl -fsS -X POST "$PROM/api/v1/admin/tsdb/snapshot" > "$WORK/snapshot.json" || fail "snapshot: $(cat "$WORK/snapshot.json")"
SNAP="$(python3 -c 'import json, sys; print(json.load(open(sys.argv[1]))["data"]["name"])' "$WORK/snapshot.json")" || fail "snapshot response: $(cat "$WORK/snapshot.json")"
docker exec "$PROM_NAME" promtool tsdb analyze "/prometheus/snapshots/$SNAP" > "$WORK/analyze.txt" 2>&1 || fail "promtool tsdb analyze: $(cat "$WORK/analyze.txt")"
mini cardinality --data "$WORK/data" --json > "$WORK/card-mini.json" 2>> "$WORK/mini-warnings.txt" || fail "minitsdb cardinality"
python3 - "$WORK/analyze.txt" "$WORK/status-tsdb.json" "$WORK/card-mini.json" "$WORK/series-prom.json" <<'PY' || fail "cardinality reports disagree: $(cat "$WORK/analyze.txt")"
import json, re, sys
sections, current, total = {}, None, None
for line in open(sys.argv[1]):
    line = line.rstrip("\n")
    match = re.match(r"^\s*(\d+)\s+(\S+)\s*$", line)
    if match and current:
        sections[current].append((match.group(2), int(match.group(1))))
    elif line.endswith(":"):
        current = line[:-1].strip()
        sections[current] = []
    elif line.startswith("Total Series:"):
        total = int(line.split(":")[1])
status = json.load(open(sys.argv[2]))["data"]
mini = json.load(open(sys.argv[3]))
prom_series = len(json.load(open(sys.argv[4]))["data"])
analyze_metrics = sections["Highest cardinality metric names"][:3]
mini_metrics = [(m["name"], m["series"]) for m in mini["metrics"][:3]]
status_metrics = [(m["name"], m["value"]) for m in status["seriesCountByMetricName"][:3]]
print("  top metrics  promtool:", analyze_metrics, "\n               status:  ", status_metrics, "\n               minitsdb:", mini_metrics)
assert analyze_metrics == mini_metrics == status_metrics == [("cc_user_logins_total", 50), ("cc_http_request_duration_seconds_bucket", 36), ("cc_http_requests_total", 10)]
analyze_labels = sections["Highest cardinality labels"][:3]
mini_labels = [(l["label"], l["values"]) for l in mini["labels"][:3]]
print("  top labels   promtool:", analyze_labels, "\n               minitsdb:", mini_labels)
assert [n for n, _ in analyze_labels] == [n for n, _ in mini_labels] == ["user_id", "__name__", "le"]
assert analyze_labels[0] == mini_labels[0] == ("user_id", 25) and analyze_labels[2] == mini_labels[2] == ("le", 6)
assert mini["offenders"][0]["label"] == "user_id", mini["offenders"]
assert total == prom_series, (total, prom_series)
print(f"  Total Series: promtool {total}, /api/v1/series {prom_series}, minitsdb {mini['total_series']}; worst offender: user_id")
PY

echo "▸ minitsdb lint against promtool check metrics, and per-family counts against check metrics --extended"
CODE=0
mini lint "$WORK/demo.prom" > "$WORK/lint-mini.txt" || CODE=$?
[[ "$CODE" == "1" ]] || fail "minitsdb lint should exit 1 on the demo page, got $CODE: $(cat "$WORK/lint-mini.txt")"
CODE=0
docker exec -i "$PROM_NAME" promtool check metrics < "$WORK/demo.prom" > "$WORK/lint-prom.txt" 2>&1 || CODE=$?
[[ "$CODE" == "3" ]] || fail "promtool check metrics should exit 3 (lint problems), got $CODE: $(cat "$WORK/lint-prom.txt")"
CODE=0
docker exec -i "$PROM_NAME" promtool check metrics --extended < "$WORK/demo.prom" > "$WORK/extended.txt" 2> "$WORK/extended.err" || CODE=$?
mini parse "$WORK/demo.prom" --json > "$WORK/demo-parse.json" || fail "minitsdb parse of the demo page: $(head -c 800 "$WORK/demo-parse.json")"
python3 - "$WORK/lint-mini.txt" "$WORK/lint-prom.txt" "$WORK/extended.txt" "$WORK/demo-parse.json" <<'PY' || fail "lint or --extended differ: promtool said $(cat "$WORK/lint-prom.txt") / $(cat "$WORK/extended.txt")"
import json, re, sys
def problems(path):
    out = set()
    for line in open(path):
        if line.strip():
            metric, _, text = line.strip().partition(" ")
            out.add((metric.rstrip(":"), text.strip()))
    return out
mini, prom = problems(sys.argv[1]), problems(sys.argv[2])
print("  minitsdb:", sorted(mini))
print("  promtool:", sorted(prom))
assert mini == prom
counts, total = {}, None
for line in open(sys.argv[3]):
    match = re.match(r"^(\S+)\s+(\d+)\s+[\d.]+%", line.strip())
    if match and match.group(1) == "Total":
        total = int(match.group(2))
    elif match:
        counts[match.group(1)] = int(match.group(2))
families = {f["name"]: f["series"] for f in json.load(open(sys.argv[4]))["families"]}
assert counts == families and total == sum(families.values()) == 68, (counts, families, total)
print(f"  --extended: {len(counts)} families, Total {total}, identical per-family counts")
PY

echo "▸ minitsdb parses Prometheus's own /metrics"
curl -fsS "$PROM/metrics" > "$WORK/self.prom" || fail "fetch $PROM/metrics"
CODE=0
mini parse "$WORK/self.prom" --format text --json > "$WORK/self.json" 2> "$WORK/self.err" || CODE=$?
[[ "$CODE" != "2" ]] || fail "minitsdb could not parse Prometheus's text /metrics: $(cat "$WORK/self.err")"
python3 - "$WORK/self.json" "$WORK/series-prom.json" <<'PY' || fail "unexpected self-parse result: $(head -c 1500 "$WORK/self.json")"
import json, sys
doc = json.load(open(sys.argv[1]))
fams = {f["name"]: f for f in doc["families"]}
head = float(fams["prometheus_tsdb_head_series"]["samples"][0]["value"])
assert len(fams) > 50, len(fams)
print(f"  {len(fams)} families, {sum(f['series'] for f in doc['families'])} samples, {len(doc['problems'])} problem(s); prometheus_tsdb_head_series {head:g} vs {len(json.load(open(sys.argv[2]))['data'])} from /api/v1/series")
for problem in doc["problems"][:5]:
    print("  problem:", problem)
PY
curl -fsS -D "$WORK/om-headers.txt" -H 'Accept: application/openmetrics-text; version=1.0.0' "$PROM/metrics" > "$WORK/self.om" || fail "fetch OpenMetrics"
if grep -qi '^content-type: application/openmetrics-text' "$WORK/om-headers.txt"; then
  CODE=0
  mini parse "$WORK/self.om" --format openmetrics > "$WORK/self-om.txt" 2>&1 || CODE=$?
  if [[ "$CODE" == "2" ]]; then
    echo "  (note: the OpenMetrics page did not parse: $(head -c 300 "$WORK/self-om.txt"))"
  else
    head -n 1 "$WORK/self-om.txt" | sed 's/^/  /'
  fi
else
  echo "  (note: Prometheus answered without OpenMetrics: $(grep -i '^content-type' "$WORK/om-headers.txt" || true))"
fi

echo "▸ node_exporter $NODE_IMAGE: host metrics scraped and ranked by minitsdb"
docker pull -q "$NODE_IMAGE" > "$WORK/pull-node.txt" 2>&1 || fail "docker pull $NODE_IMAGE: $(cat "$WORK/pull-node.txt")"
docker rm -f -v "$NODE_NAME" > /dev/null 2>&1 || true
CONTAINERS+=("$NODE_NAME")
docker run -d --name "$NODE_NAME" --network host --pid host -v /:/host:ro,rslave "$NODE_IMAGE" \
  --path.rootfs=/host --web.listen-address="127.0.0.1:$PN" > "$WORK/node-run.txt" 2>&1 || fail "docker run $NODE_IMAGE: $(cat "$WORK/node-run.txt")"
wait_http "http://127.0.0.1:$PN/metrics" 30 || fail "node_exporter never answered: $(docker logs "$NODE_NAME" 2>&1 | tail -n 20)"
mini scrape --target "127.0.0.1:$PN" --job node --interval 5s --timeout 4s --scrapes 2 --data "$WORK/node" > "$WORK/node-scrape.txt" 2>&1 \
  || fail "minitsdb scrape of node_exporter: $(cat "$WORK/node-scrape.txt")"
python3 code/main.py query --data "$WORK/node" 'up{job="node"}' --json > "$WORK/node-up.json" || fail "query node up"
[[ "$(value_of "$WORK/node-up.json")" == "1" ]] || fail "node_exporter scrape should be up: $(cat "$WORK/node-scrape.txt")"
mini cardinality --data "$WORK/node" --top 5 --json > "$WORK/node-card.json" || fail "node cardinality"
python3 - "$WORK/node-card.json" "$WORK/node" <<'PY' || fail "unexpected node_exporter series: $(head -c 1500 "$WORK/node-card.json")"
import json, os, subprocess, sys
report = json.load(open(sys.argv[1]))
out = subprocess.run([sys.executable, "code/main.py", "series", "--data", sys.argv[2], "--match", "node_cpu_seconds_total", "--json"], capture_output=True, text=True, check=True)
cpu = json.loads(out.stdout)["data"]
modes = {s["mode"] for s in cpu}
assert len(cpu) >= 8 and {"idle", "user", "system"} <= modes, (len(cpu), modes)
print(f"  {report['total_series']} series; node_cpu_seconds_total {len(cpu)} series ({len(cpu) // len(modes)} CPU(s) x {len(modes)} modes); top:", [(m["name"], m["series"]) for m in report["metrics"][:3]])
PY
curl -fsS "http://127.0.0.1:$PN/metrics" > "$WORK/node.prom" || fail "fetch node_exporter /metrics"
mini lint "$WORK/node.prom" > "$WORK/node-lint-mini.txt" || true
docker exec -i "$PROM_NAME" promtool check metrics < "$WORK/node.prom" > "$WORK/node-lint-prom.txt" 2>&1 || true
if cmp -s <(sort "$WORK/node-lint-mini.txt") <(sort "$WORK/node-lint-prom.txt"); then
  echo "  lint: minitsdb and promtool agree on node_exporter ($(wc -l < "$WORK/node-lint-mini.txt" | tr -d ' ') problem lines)"
else
  echo "  (note: lint differs on node_exporter: minitsdb $(wc -l < "$WORK/node-lint-mini.txt" | tr -d ' ') lines, promtool $(wc -l < "$WORK/node-lint-prom.txt" | tr -d ' ') lines)"
  diff <(sort "$WORK/node-lint-mini.txt") <(sort "$WORK/node-lint-prom.txt") > "$WORK/node-lint.diff" || true
  head -n 10 "$WORK/node-lint.diff" | sed 's/^/    /'
fi

echo "▸ crash recovery: a torn WAL tail stops replay, and the next scrape repairs it"
cp -R "$WORK/data" "$WORK/torn"
ls "$WORK/torn/wal" > "$WORK/segments.txt"
SEGMENT="$(sort "$WORK/segments.txt" | tail -n 1)"
truncate -s -3 "$WORK/torn/wal/$SEGMENT"
mini series --data "$WORK/torn" --json > "$WORK/torn-series.json" 2> "$WORK/torn.err" || fail "series on a torn WAL: $(cat "$WORK/torn.err")"
grep -q "torn record" "$WORK/torn.err" || fail "replay should report the torn record: $(cat "$WORK/torn.err")"
mini scrape --data "$WORK/torn" --target "127.0.0.1:$PA" --job repair --interval 2s --timeout 1s --scrapes 1 > "$WORK/repair.txt" 2>&1 || fail "scrape on a torn WAL: $(cat "$WORK/repair.txt")"
grep -q "WAL repaired" "$WORK/repair.txt" || fail "the scraper should repair the WAL: $(cat "$WORK/repair.txt")"
mini series --data "$WORK/torn" --json > /dev/null 2> "$WORK/repaired.err" || fail "series after repair"
[[ ! -s "$WORK/repaired.err" ]] || fail "replay after repair should be clean: $(cat "$WORK/repaired.err")"
echo "  $(cat "$WORK/torn.err")"

echo "SMOKE OK: observability/metrics-tsdb"
