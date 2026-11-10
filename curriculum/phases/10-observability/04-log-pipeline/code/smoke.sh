#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."

fail() { echo "SMOKE FAIL: $*" >&2; exit 1; }
WORK="$(mktemp -d)"
chmod 755 "$WORK"
LOGS="$WORK/logs"
mkdir -p "$LOGS"
LOKI_IMAGE="${LOKI_IMAGE:-grafana/loki:3.7.9}"
ALLOY_IMAGE="${ALLOY_IMAGE:-grafana/alloy:v1.21.3}"
LOKI_NAME="cc-log-pipeline-loki-$$"
ALLOY_NAME="cc-log-pipeline-alloy-$$"
NET="cc-log-pipeline-net-$$"
PIDS=()
DOCKER_USED=""
cleanup() {
  for pid in ${PIDS[@]+"${PIDS[@]}"}; do kill "$pid" >/dev/null 2>&1 || true; done
  if [[ -n "$DOCKER_USED" ]]; then
    docker rm -f "$ALLOY_NAME" "$LOKI_NAME" >/dev/null 2>&1 || true
    docker network rm "$NET" >/dev/null 2>&1 || true
  fi
  rm -rf "$WORK"
}
trap cleanup EXIT

cat > "$WORK/check.py" <<'PY'
import hashlib, json, os, re, sys, time, urllib.error, urllib.parse, urllib.request
from collections import Counter
from datetime import datetime, timezone

NS = 10**9
IGNORED = {"service_name", "detected_level", "__error_details__", "__stream_shard__"}
SEQ = re.compile(r'\bseq"?[:=](\d+)')


def t_ns(text):
    return int(datetime.strptime(text, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).timestamp()) * NS


def get(base, path, params):
    url = base.rstrip("/") + path + "?" + urllib.parse.urlencode(params)
    try:
        with urllib.request.urlopen(url, timeout=30) as response:
            return json.loads(response.read().decode())
    except urllib.error.HTTPError as exc:
        raise SystemExit(f"HTTP {exc.code} from {url}: {exc.read().decode(errors='replace')[:500]}")


def norm(labels):
    return tuple(sorted((k, v) for k, v in labels.items() if k not in IGNORED))


def entries(base, query, start, end):
    doc = get(base, "/loki/api/v1/query_range", {"query": query, "start": start, "end": end, "limit": 5000, "direction": "forward"})
    return [(norm(s["stream"]), int(v[0]), v[1]) for s in doc["data"]["result"] for v in s["values"]]


def vector(base, query, at):
    doc = get(base, "/loki/api/v1/query", {"query": query, "time": at})
    return {norm(s["metric"]): float(s["value"][1]) for s in doc["data"]["result"]}


def matrix(base, query, start, end, step):
    doc = get(base, "/loki/api/v1/query_range", {"query": query, "start": start, "end": end, "step": step})
    return {norm(s["metric"]): {round(float(t) * 1000): float(v) for t, v in s["values"]} for s in doc["data"]["result"]}


def close(a, b):
    return abs(a - b) <= 1e-9 * max(1.0, abs(a), abs(b))


def expected(checkout_summary, payments_summary):
    c, p = json.load(open(checkout_summary)), json.load(open(payments_summary))
    return {"checkout": sorted(set(range(c["first"], c["last"] + 1)) - set(c["debug"])), "payments": list(range(p["first"], p["last"] + 1))}, p["tracebacks"]


def app_of(labels):
    return dict(labels).get("app")


def wait_status(path, want, seconds):
    deadline = time.time() + float(seconds)
    while time.time() < deadline:
        try:
            if json.load(open(path))["shipped"] >= int(want):
                return 0
        except (OSError, ValueError, KeyError):
            pass
        time.sleep(0.05)
    print(f"status never reached {want} shipped entries: {open(path).read() if os.path.exists(path) else 'no status file'}")
    return 1


def wait_count(base, selector, want, seconds):
    deadline, got = time.time() + float(seconds), None
    while time.time() < deadline:
        try:
            got = sum(vector(base, f"sum(count_over_time({selector} [3h]))", time.time_ns()).values())
        except (OSError, SystemExit) as exc:
            got = f"error: {exc}"
        if got == float(want):
            return 0
        time.sleep(0.5)
    print(f"{base}: {selector} has {got} entries, expected {want}")
    return 1


def seqs(base, t0, checkout_summary, payments_summary):
    want, tracebacks = expected(checkout_summary, payments_summary)
    rows = entries(base, '{job="cc-minilog"}', t_ns(t0), t_ns(t0) + 600 * NS)
    got = {"checkout": [], "payments": []}
    streams = Counter()
    for labels, ts, line in rows:
        got[app_of(labels)].append(int(SEQ.search(line).group(1)))
        streams[tuple((k, v) for k, v in labels if k in ("app", "filename", "level"))] += 1
    problems = []
    for app in ("checkout", "payments"):
        counts = Counter(got[app])
        dupes = sorted(s for s, n in counts.items() if n > 1)
        lost = sorted(set(want[app]) - set(counts))
        extra = sorted(set(counts) - set(want[app]))
        print(f"  {app:<9} {len(got[app])} entries, expected {len(want[app])}; duplicates {dupes[:10]}, lost {lost[:10]}, unexpected {extra[:10]}")
        if dupes or lost or extra:
            problems.append(app)
    traces = [l for _, _, l in rows if "Traceback" in l]
    trace_seqs = sorted(int(SEQ.search(l).group(1)) for l in traces)
    print(f"  traceback entries {trace_seqs} with {sorted({l.count(chr(10)) + 1 for l in traces})} lines each")
    if trace_seqs != tracebacks or any(l.count("\n") != 6 for l in traces):
        problems.append("tracebacks")
    for key, n in sorted(streams.items()):
        print(f"    {n:>4}  {dict(key)}")
    if problems:
        print(f"problems: {problems}")
        return 1
    return 0


def compare(base_a, base_b, t0):
    start, end = t_ns(t0), t_ns(t0) + 600 * NS
    trace = hashlib.sha256(b"checkout-42").hexdigest()[:32]
    logs = [
        '{job="cc-minilog"}',
        '{job="cc-minilog", app="payments"} |= "Traceback"',
        '{job="cc-minilog", app="checkout"} | json | status >= 500',
        '{job="cc-minilog", app="checkout"} | json | status >= 500 | line_format "{{.status}} {{.method}} {{.path}} seq={{.seq}}"',
        '{job="cc-minilog", app="payments"} != "Traceback" | logfmt | level="error"',
        '{job="cc-minilog", app="payments"} |~ "card=(visa|amex)" != "status=200"',
        '{job="cc-minilog", app="checkout"} | trace_id="%s"' % trace,
        '{job="cc-minilog", app=~"check.*|pay.*", level!="info"}',
        '{job="cc-minilog", app="checkout"} | json | method="POST" | duration_ms > 800',
    ]
    vectors = [
        'sum by (app, filename) (count_over_time({job="cc-minilog"}[30m]))',
        'sum by (app, level) (count_over_time({job="cc-minilog"}[30m]))',
        'sum(rate({job="cc-minilog", app="checkout"} | json | status >= 500 [30m]))',
        'sum by (level) (bytes_over_time({job="cc-minilog", app="payments"}[30m]))',
        'count_over_time({job="cc-minilog", app="payments"} |= "Traceback" [30m])',
        'sum by (status) (count_over_time({job="cc-minilog", app="payments"} != "Traceback" | logfmt [30m]))',
    ]
    matrices = [
        'sum by (app) (count_over_time({job="cc-minilog"}[1m]))',
        'sum by (level) (count_over_time({job="cc-minilog", app="checkout"} | json | status >= 400 [2m]))',
    ]
    failures = 0
    print(f"  {'query':<112} {'minilog':>8} {'loki':>8}")
    for query in logs:
        a, b = Counter(entries(base_a, query, start, end)), Counter(entries(base_b, query, start, end))
        same = a == b and max(list(a.values()) + [1]) == 1 and max(list(b.values()) + [1]) == 1
        print(f"  {query[:112]:<112} {sum(a.values()):>8} {sum(b.values()):>8}  {'same' if same else 'DIFFERENT'}")
        if not same:
            failures += 1
            for name, x, y in (("only in minilog", a, b), ("only in loki", b, a)):
                for item in list((x - y).elements())[:3]:
                    print(f"      {name}: {item}")
    for query in vectors:
        at = end
        a, b = vector(base_a, query, at), vector(base_b, query, at)
        same = a.keys() == b.keys() and all(close(a[k], b[k]) for k in a)
        print(f"  {query[:112]:<112} {len(a):>8} {len(b):>8}  {'same' if same else 'DIFFERENT'}")
        if not same:
            failures += 1
            print(f"      minilog: {sorted(a.items())[:4]}\n      loki:    {sorted(b.items())[:4]}")
    for query in matrices:
        a, b = matrix(base_a, query, start, start + 360 * NS, "60s"), matrix(base_b, query, start, start + 360 * NS, "60s")
        same = a.keys() == b.keys() and all(a[k].keys() == b[k].keys() and all(close(a[k][t], b[k][t]) for t in a[k]) for k in a)
        print(f"  {query[:112]:<112} {sum(len(v) for v in a.values()):>8} {sum(len(v) for v in b.values()):>8}  {'same' if same else 'DIFFERENT'}")
        if not same:
            failures += 1
            print(f"      minilog: {sorted(a.items())[:2]}\n      loki:    {sorted(b.items())[:2]}")
    total = len(logs) + len(vectors) + len(matrices)
    print(f"  {total - failures}/{total} queries agree")
    return 1 if failures else 0


def alloy(loki, t0, checkout_summary, payments_summary, seconds):
    want, tracebacks = expected(checkout_summary, payments_summary)
    target = {"checkout": float(len(want["checkout"])), "payments": float(len(want["payments"]))}
    deadline, got = time.time() + float(seconds), {}
    while time.time() < deadline:
        got = {app_of(k): v for k, v in vector(loki, 'sum by (app) (count_over_time({job="cc-alloy"}[3h]))', time.time_ns()).items()}
        if got == target:
            break
        time.sleep(1)
    print(f"  alloy -> loki entries per app {got}, expected {target}")
    traces = sum(vector(loki, 'sum(count_over_time({job="cc-alloy", app="payments"} |= "Traceback" [3h]))', time.time_ns()).values())
    print(f"  alloy traceback entries: {traces:g}, expected {len(tracebacks)}")
    start, end = t_ns(t0), t_ns(t0) + 600 * NS
    mine = {(app_of(l), ts, line) for l, ts, line in entries(loki, '{job="cc-minilog"}', start, end)}
    theirs = {(app_of(l), ts, line) for l, ts, line in entries(loki, '{job="cc-alloy"}', start, end)}
    print(f"  (timestamp, line) pairs identical to minilog's: {'yes' if mine == theirs else 'no, %d differ' % len(mine ^ theirs)}")
    return 0 if got == target and traces == len(tracebacks) else 1


def field(path, *keys):
    value = json.load(open(path))
    for key in keys:
        value = value[int(key)] if isinstance(value, list) else value[key]
    print(json.dumps(value) if isinstance(value, (dict, list)) else value)
    return 0


modes = {"wait-status": wait_status, "wait-count": wait_count, "seqs": seqs, "compare": compare, "alloy": alloy, "field": field}
sys.exit(modes[sys.argv[1]](*sys.argv[2:]))
PY
check() { python3 "$WORK/check.py" "$@"; }

T0="$(python3 -c 'import time; print(time.strftime("%Y-%m-%dT%H:%M:00Z", time.gmtime((int(time.time()) // 60 - 20) * 60)))')"
echo "▸ timeline starts at $T0 (twenty minutes ago, on the minute, inside Loki's acceptance window)"

HAVE_DOCKER=""
if command -v docker >/dev/null && docker info >/dev/null 2>&1; then HAVE_DOCKER=1; fi
LOKI=""
if [[ -n "$HAVE_DOCKER" ]]; then
  echo "▸ Use It: Grafana Loki ($LOKI_IMAGE) with structured metadata on and service_name/level discovery off"
  cat > "$WORK/loki.yaml" <<'EOF'
auth_enabled: false
server:
  http_listen_port: 3100
  log_level: warn
common:
  instance_addr: 127.0.0.1
  path_prefix: /loki
  storage:
    filesystem:
      chunks_directory: /loki/chunks
      rules_directory: /loki/rules
  replication_factor: 1
  ring:
    kvstore:
      store: inmemory
schema_config:
  configs:
    - from: 2020-10-24
      store: tsdb
      object_store: filesystem
      schema: v13
      index:
        prefix: index_
        period: 24h
limits_config:
  allow_structured_metadata: true
  discover_service_name: []
  discover_log_levels: false
analytics:
  reporting_enabled: false
EOF
  chmod 644 "$WORK/loki.yaml"
  DOCKER_USED=1
  docker network create "$NET" >/dev/null || fail "docker network create $NET"
  LOKI_PORT=$((20000 + RANDOM % 10000))
  docker run -d --name "$LOKI_NAME" --network "$NET" -p "127.0.0.1:$LOKI_PORT:3100" -v "$WORK/loki.yaml:/etc/loki/config.yaml:ro" "$LOKI_IMAGE" -config.file=/etc/loki/config.yaml > "$WORK/loki-run.txt" 2>&1 || fail "docker run loki: $(cat "$WORK/loki-run.txt")"
  LOKI="http://127.0.0.1:$LOKI_PORT"
  READY=""
  for _ in $(seq 1 120); do
    if curl -fsS "$LOKI/ready" > "$WORK/ready.txt" 2>/dev/null && grep -q ready "$WORK/ready.txt"; then READY=1; break; fi
    sleep 1
  done
  [[ -n "$READY" ]] || { docker logs "$LOKI_NAME" > "$WORK/loki.log" 2>&1 || true; tail -30 "$WORK/loki.log"; fail "Loki did not report ready on $LOKI within 120s"; }
  echo "  Loki ready on $LOKI"
else
  echo "  (docker not available; Loki and Alloy comparisons skipped)"
fi

echo "▸ minilog's store: the same push and query API over label-indexed compressed chunks"
python3 code/main.py serve --port 0 --port-file "$WORK/store.port" --chunk-bytes 16384 2> "$WORK/store.err" &
PIDS+=("$!")
for _ in $(seq 1 100); do [[ -s "$WORK/store.port" ]] && break; sleep 0.1; done
[[ -s "$WORK/store.port" ]] || fail "minilog serve did not start: $(cat "$WORK/store.err")"
STORE="http://127.0.0.1:$(cat "$WORK/store.port")"
curl -fsS "$STORE/ready" | grep -q ready || fail "minilog store not ready on $STORE"
echo "  store on $STORE"

python3 - code/fixtures/pipeline.json "$WORK/pipeline.json" <<'PY2'
import json, sys
doc = json.load(open(sys.argv[1]))
doc["labels"]["job"] = "cc-minilog"
doc["batch_wait"] = 0.2
json.dump(doc, open(sys.argv[2], "w"), indent=2)
PY2
grep -q '"cc-minilog"' "$WORK/pipeline.json" || fail "could not prepare the pipeline from code/fixtures/pipeline.json"
PUSH=(--push "$STORE")
[[ -n "$LOKI" ]] && PUSH+=(--push "$LOKI")
SHIP=(python3 code/main.py ship --config "$WORK/pipeline.json" "${PUSH[@]}" --positions "$WORK/positions.json" --poll 0.05 --status "$WORK/status.json" --json)

echo "▸ ship while the apps write: checkout rotates by rename (with a late write to the old descriptor), payments by copytruncate with tracebacks"
"${SHIP[@]}" > "$WORK/ship1.json" 2> "$WORK/ship1.err" &
SHIP1=$!
PIDS+=("$SHIP1")
python3 code/main.py generate "$LOGS/checkout.log" --format json --count 600 --start "$T0" --rotate-every 120 --rotate rename --pace 0.005 --json > "$WORK/checkout.json" 2> "$WORK/gen1.err" &
GEN1=$!
python3 code/main.py generate "$LOGS/payments.log" --format logfmt --count 400 --start "$T0" --rotate-every 100 --rotate copytruncate --traceback-every 150 --pace 0.0075 --json > "$WORK/payments.json" 2> "$WORK/gen2.err" &
GEN2=$!
PIDS+=("$GEN1" "$GEN2")

echo "▸ restart the shipper mid-stream (SIGTERM): it pushes what it has, saves positions and resumes from them"
check wait-status "$WORK/status.json" 250 30 || fail "the first shipper never shipped 250 entries: $(cat "$WORK/ship1.err")"
kill -TERM "$SHIP1"
CODE=0
wait "$SHIP1" || CODE=$?
[[ "$CODE" == "0" ]] || fail "the first shipper exited $CODE on SIGTERM: $(cat "$WORK/ship1.err")"
echo "  first run shipped $(check field "$WORK/ship1.json" shipped) entries, rotation $(check field "$WORK/ship1.json" rotation)"
"${SHIP[@]}" > "$WORK/ship2.json" 2> "$WORK/ship2.err" &
SHIP2=$!
PIDS+=("$SHIP2")
wait "$GEN1" || fail "checkout generator: $(cat "$WORK/gen1.err")"
wait "$GEN2" || fail "payments generator: $(cat "$WORK/gen2.err")"
echo "  checkout: $(check field "$WORK/checkout.json" files)"
echo "  payments: $(check field "$WORK/payments.json" files)"
EXPECTED=$(( 600 - $(check field "$WORK/checkout.json" debug | python3 -c 'import json,sys; print(len(json.load(sys.stdin)))') + 400 ))
check wait-count "$STORE" '{job="cc-minilog"}' "$EXPECTED" 30 || fail "minilog store never reached $EXPECTED entries: $(cat "$WORK/ship2.err")"
kill -TERM "$SHIP2"
CODE=0
wait "$SHIP2" || CODE=$?
[[ "$CODE" == "0" ]] || fail "the second shipper exited $CODE (undelivered batches?): $(cat "$WORK/ship2.err")"
echo "  second run shipped $(check field "$WORK/ship2.json" shipped) entries, rotation $(check field "$WORK/ship2.json" rotation)"
RENAMED=$(( $(check field "$WORK/ship1.json" rotation renamed) + $(check field "$WORK/ship2.json" rotation renamed) ))
COPIES=$(( $(check field "$WORK/ship1.json" rotation copies) + $(check field "$WORK/ship2.json" rotation copies) ))
LOST=$(( $(check field "$WORK/ship1.json" rotation lost_bytes) + $(check field "$WORK/ship2.json" rotation lost_bytes) ))
[[ "$RENAMED" -ge 1 && "$COPIES" -ge 1 && "$LOST" == "0" ]] || fail "expected renames and copytruncate handovers without lost bytes: renamed $RENAMED, copies $COPIES, lost $LOST"
echo "  inode keys followed $RENAMED renames; fingerprints handed $COPIES copytruncate copies over at their old offsets; 0 bytes lost"
DUPES="$(curl -fsS "$STORE/minilog/stats" | python3 -c 'import json,sys; d=json.load(sys.stdin); print(d["duplicates_dropped"], d["chunks"], d["compression_ratio"])')"
echo "  store: duplicates dropped, chunks, compression ratio = $DUPES"
[[ "${DUPES%% *}" == "0" ]] || fail "the store dropped duplicate pushes, so the shipper sent an entry twice: $DUPES"

echo "▸ minilog's store: every sequence number exactly once per app, each traceback one entry"
check seqs "$STORE" "$T0" "$WORK/checkout.json" "$WORK/payments.json" || fail "losses or duplicates in minilog's store (see above)"

echo "▸ a pipeline that labels user_id warns twice: at config load and when values pile up"
python3 - "$WORK/pipeline.json" "$WORK/bad-pipeline.json" <<'PY'
import json, sys
doc = json.load(open(sys.argv[1]))
doc["sources"] = [doc["sources"][0]]
next(st for st in doc["sources"][0]["stages"] if st["type"] == "labels")["names"] = ["level", "user_id"]
json.dump(doc, open(sys.argv[2], "w"))
PY
python3 code/main.py ship --config "$WORK/bad-pipeline.json" --once --dry-run > "$WORK/dry.out" 2> "$WORK/dry.err" || fail "dry run: $(cat "$WORK/dry.err")"
grep -q "looks unbounded" "$WORK/dry.err" && grep -q "more than 25 distinct values" "$WORK/dry.err" || fail "missing cardinality warnings: $(cat "$WORK/dry.err")"
STREAMS="$(python3 -c 'import json,sys; print(sum(len(json.loads(l)["streams"]) for l in open(sys.argv[1]) if l.strip()))' "$WORK/dry.out")"
echo "  labelling user_id turns 3 checkout streams into $STREAMS"
[[ "$STREAMS" -gt 100 ]] || fail "expected one stream per user_id value, got $STREAMS"

if [[ -n "$LOKI" ]]; then
  echo "▸ Loki holds the same entries: every sequence number exactly once, tracebacks whole"
  check wait-count "$LOKI" '{job="cc-minilog"}' "$EXPECTED" 30 || fail "Loki never reached $EXPECTED entries"
  check seqs "$LOKI" "$T0" "$WORK/checkout.json" "$WORK/payments.json" || fail "losses or duplicates in Loki (see above)"

  echo "▸ the same LogQL against both: log queries, instant and range metric queries"
  check compare "$STORE" "$LOKI" "$T0" || fail "minilog and Loki disagree (see the table above)"

  echo "▸ Loki's API by hand: push one line with structured metadata, read it back, list labels and series"
  NOW_NS="$(python3 -c 'import time; print(time.time_ns())')"
  curl -fsS -H 'Content-Type: application/json' -X POST "$LOKI/loki/api/v1/push" \
    --data "{\"streams\":[{\"stream\":{\"job\":\"cc-curl\",\"app\":\"smoke\"},\"values\":[[\"$NOW_NS\",\"hello from curl\",{\"trace_id\":\"abc123\"}]]}]}" -o "$WORK/push.out" -w '%{http_code}' > "$WORK/push.code" || fail "curl push: $(cat "$WORK/push.out")"
  [[ "$(cat "$WORK/push.code")" == "204" ]] || fail "push returned $(cat "$WORK/push.code"): $(cat "$WORK/push.out")"
  check wait-count "$LOKI" '{job="cc-curl"} | trace_id="abc123"' 1 20 || fail "the curl entry is not queryable by its structured metadata"
  curl -fsS -G "$LOKI/loki/api/v1/query_range" --data-urlencode 'query={job="cc-curl"}' --data-urlencode "start=$((NOW_NS - 60000000000))" --data-urlencode "end=$((NOW_NS + 1000000000))" > "$WORK/curl-query.json"
  python3 - "$WORK/curl-query.json" <<'PY' || fail "curl query_range: $(cat "$WORK/curl-query.json")"
import json, sys
stream = json.load(open(sys.argv[1]))["data"]["result"][0]
assert stream["stream"].get("trace_id") == "abc123", stream["stream"]
assert stream["values"][0][1] == "hello from curl", stream["values"]
print("  structured metadata comes back as a label of the result stream:", stream["stream"])
PY
  curl -fsS "$LOKI/loki/api/v1/labels" > "$WORK/labels.json"
  python3 -c 'import json,sys; names=json.load(open(sys.argv[1]))["data"]; assert {"app","filename","job","level"} <= set(names), names; assert "trace_id" not in names, names; print("  index labels:", names)' "$WORK/labels.json" || fail "labels: $(cat "$WORK/labels.json")"
  python3 code/main.py cardinality --url "$LOKI" --match '{job="cc-minilog"}' > "$WORK/card.txt" || fail "cardinality against Loki: $(cat "$WORK/card.txt")"
  grep -q "^streams: 6$" "$WORK/card.txt" || fail "expected 6 streams (2 apps x 3 levels): $(cat "$WORK/card.txt")"
  sed 's/^/  /' "$WORK/card.txt"
  python3 code/main.py query --url "$LOKI" '{job="cc-minilog", app="payments"} |= "Traceback"' --start "$T0" --since 1h --end "$(python3 -c "import sys; from datetime import datetime, timedelta; print((datetime.strptime(sys.argv[1], '%Y-%m-%dT%H:%M:%SZ') + timedelta(minutes=10)).strftime('%Y-%m-%dT%H:%M:%SZ'))" "$T0")" > "$WORK/cli.txt" || fail "minilog query against Loki: $(cat "$WORK/cli.txt")"
  grep -q "^2 entries in" "$WORK/cli.txt" || fail "minilog query against Loki: $(tail -3 "$WORK/cli.txt")"
  echo "  minilog query against Loki: $(tail -1 "$WORK/cli.txt")"

  echo "▸ Use It: Grafana Alloy ($ALLOY_IMAGE) ships the rotated files to the same Loki with the same pipeline"
  cat > "$WORK/config.alloy" <<EOF
loki.source.file "checkout" {
  targets    = [{"__path__" = "/logs/checkout.log*", "app" = "checkout", "job" = "cc-alloy"}]
  forward_to = [loki.process.checkout.receiver]
  file_match {
    enabled     = true
    sync_period = "1s"
  }
}

loki.process "checkout" {
  stage.json {
    expressions = {ts = "", level = "", trace_id = ""}
  }
  stage.drop {
    source = "level"
    value  = "debug"
  }
  stage.timestamp {
    source = "ts"
    format = "RFC3339Nano"
  }
  stage.labels {
    values = {level = ""}
  }
  stage.structured_metadata {
    values = {trace_id = ""}
  }
  forward_to = [loki.write.local.receiver]
}

loki.source.file "payments" {
  targets    = [{"__path__" = "/logs/payments.log*", "app" = "payments", "job" = "cc-alloy"}]
  forward_to = [loki.process.payments.receiver]
  file_match {
    enabled     = true
    sync_period = "1s"
  }
}

loki.process "payments" {
  stage.multiline {
    firstline     = "^ts="
    max_wait_time = "2s"
  }
  stage.regex {
    expression = "^ts=(?P<ts>\\\\S+) level=(?P<level>\\\\w+)"
  }
  stage.timestamp {
    source = "ts"
    format = "RFC3339Nano"
  }
  stage.labels {
    values = {level = ""}
  }
  forward_to = [loki.write.local.receiver]
}

loki.write "local" {
  endpoint {
    url = "http://$LOKI_NAME:3100/loki/api/v1/push"
  }
}
EOF
  chmod 644 "$WORK/config.alloy"
  chmod -R a+rX "$LOGS"
  docker run -d --name "$ALLOY_NAME" --network "$NET" -v "$LOGS:/logs:ro" -v "$WORK/config.alloy:/etc/alloy/config.alloy:ro" "$ALLOY_IMAGE" run --storage.path=/var/lib/alloy/data /etc/alloy/config.alloy > "$WORK/alloy-run.txt" 2>&1 || fail "docker run alloy: $(cat "$WORK/alloy-run.txt")"
  if ! check alloy "$LOKI" "$T0" "$WORK/checkout.json" "$WORK/payments.json" 90; then
    docker logs "$ALLOY_NAME" > "$WORK/alloy.log" 2>&1 || true
    tail -40 "$WORK/alloy.log"
    fail "Alloy's entries in Loki do not match the generated ones (see above)"
  fi
fi

echo "SMOKE OK: observability/log-pipeline"
