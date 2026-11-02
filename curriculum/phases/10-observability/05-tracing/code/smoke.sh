#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."

fail() { echo "SMOKE FAIL: $*" >&2; exit 1; }
WORK="$(mktemp -d)"
OTELCOL_IMAGE="${OTELCOL_IMAGE:-otel/opentelemetry-collector-contrib:0.164.0}"
JAEGER_IMAGE="${JAEGER_IMAGE:-jaegertracing/jaeger:2.22.0}"
OTEL_PY=(opentelemetry-api==1.45.0 opentelemetry-sdk==1.45.0 opentelemetry-exporter-otlp-proto-http==1.45.0 opentelemetry-distro==0.66b0 opentelemetry-instrumentation-urllib==0.66b0)
OTELCOL="cc-tracing-otelcol-$$"
JAEGER="cc-tracing-jaeger-$$"
PATTERN="ok,db-slow,ok,ok,db-error,ok,api-slow,ok,ok,unsampled+db-error"
PIDS=()
cleanup() {
  set +e
  for pid in ${PIDS[@]+"${PIDS[@]}"}; do kill "$pid" 2>/dev/null; wait "$pid" 2>/dev/null; done
  if command -v docker >/dev/null; then docker rm -f "$OTELCOL" "$JAEGER" >/dev/null 2>&1; fi
  rm -rf "$WORK"
}
trap cleanup EXIT

start() { local name="$1"; shift; python3 code/main.py "$@" > "$WORK/$name.log" 2>&1 & PIDS+=("$!"); }
wait_line() { for _ in $(seq 1 100); do grep -q "$2" "$1" 2>/dev/null && return 0; sleep 0.1; done; fail "no '$2' in $1: $(cat "$1" 2>/dev/null)"; }
url_of() { awk '/ listening on /{print $4; exit}' "$1"; }
free_ports() {
  python3 - "$1" <<'PY'
import socket, sys
sockets = [socket.socket() for _ in range(int(sys.argv[1]))]
for s in sockets:
    s.bind(("127.0.0.1", 0))
print(" ".join(str(s.getsockname()[1]) for s in sockets))
for s in sockets:
    s.close()
PY
}
otlp_ready() {
  for _ in $(seq 1 60); do
    if python3 - "$1" 2>/dev/null <<'PY'
import sys, urllib.request
request = urllib.request.Request(f"http://127.0.0.1:{sys.argv[1]}/v1/traces", data=b"{}", headers={"content-type": "application/json"}, method="POST")
urllib.request.urlopen(request, timeout=2).read()
PY
    then return 0; fi
    sleep 1
  done
  return 1
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

echo "▸ context: the W3C example parses; an all-zero parent-id restarts the trace"
python3 code/main.py context --traceparent 00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01 --tracestate congo=t61rcWkgMzE --set-tracestate cc=1 --baggage cc.fault=db-slow --json > "$WORK/ctx.json" || fail "context: $(cat "$WORK/ctx.json")"
[[ "$(field "$WORK/ctx.json" incoming trace_id)" == "4bf92f3577b34da6a3ce929d0e0e4736" && "$(field "$WORK/ctx.json" incoming sampled)" == "True" ]] || fail "context fields: $(cat "$WORK/ctx.json")"
[[ "$(field "$WORK/ctx.json" outgoing tracestate)" == "cc=1,congo=t61rcWkgMzE" && "$(field "$WORK/ctx.json" outgoing baggage)" == "cc.fault=db-slow" ]] || fail "next hop headers: $(cat "$WORK/ctx.json")"
CODE=0
python3 code/main.py context --traceparent 00-4bf92f3577b34da6a3ce929d0e0e4736-0000000000000000-01 > "$WORK/ctx-bad.txt" || CODE=$?
[[ "$CODE" == "1" ]] && grep -q "parent-id is all zeros" "$WORK/ctx-bad.txt" && grep -q "new trace" "$WORK/ctx-bad.txt" || fail "an all-zero parent-id should exit 1 and restart: code $CODE, $(cat "$WORK/ctx-bad.txt")"
echo "  next hop: $(field "$WORK/ctx.json" outgoing traceparent)"

echo "▸ analyze the fixture: the slow hop, the error branch and a broken tree"
python3 code/main.py analyze code/fixtures/checkout.otlp.jsonl --json > "$WORK/fixture.json" || fail "analyze fixture: $(cat "$WORK/fixture.json")"
python3 - "$WORK/fixture.json" <<'PY' || fail "fixture analysis: $(cat "$WORK/fixture.json")"
import json, sys
slow, failed, broken = json.load(open(sys.argv[1]))["traces"]
assert (slow["slowest_hop"]["service"], slow["slowest_hop"]["name"], slow["slowest_hop"]["ms"]) == ("db", "SELECT cart", 400.4), slow["slowest_hop"]
assert abs(sum(step["ms"] for step in slow["critical_path"]) - slow["duration_ms"]) < 0.01, slow["critical_path"]
assert failed["errors"] and failed["slowest_hop"]["name"] == "score", failed
assert broken["single_tree"] is False and broken["missing_parents"] == ["a3ce929d0e0e4736"], broken
print("  db SELECT cart holds", slow["slowest_hop"]["ms"], "ms of", slow["duration_ms"], "ms; the error trace's critical path avoids the failing branch; one trace names its missing parent")
PY
CODE=0
python3 code/main.py analyze code/fixtures/checkout.otlp.jsonl --strict > "$WORK/strict.txt" || CODE=$?
[[ "$CODE" == "1" ]] || fail "--strict should exit 1 on the broken fixture trace: code $CODE, $(cat "$WORK/strict.txt")"

HAVE_DOCKER=""
if command -v docker >/dev/null && docker info >/dev/null 2>&1; then HAVE_DOCKER=1; fi

echo "▸ minitrace collector: decision_wait 3s, keep errors, keep traces >= 300 ms, hash-sample 25% with salt cc-smoke"
start collector collector --decision-wait 3 --latency-ms 300 --sample-percent 25 --hash-salt cc-smoke --out "$WORK/mini-kept.jsonl"
wait_line "$WORK/collector.log" "listening on"
MINI="$(url_of "$WORK/collector.log")"
EXPORTS=(--export "$MINI/v1/traces")

if [[ -n "$HAVE_DOCKER" ]]; then
  echo "▸ OpenTelemetry Collector $OTELCOL_IMAGE: otlp receiver, the same tail_sampling policies, debug and file exporters"
  read -r OPORT JPORT QPORT < <(free_ports 3)
  mkdir -p "$WORK/otel-out"
  cat > "$WORK/otelcol.yaml" <<'EOF'
receivers:
  otlp:
    protocols:
      http:
        endpoint: 0.0.0.0:4318
processors:
  tail_sampling:
    decision_wait: 3s
    num_traces: 10000
    policies:
      - name: errors
        type: status_code
        status_code: {status_codes: [ERROR]}
      - name: slow
        type: latency
        latency: {threshold_ms: 300}
      - name: sample-the-rest
        type: probabilistic
        probabilistic: {hash_salt: cc-smoke, sampling_percentage: 25}
exporters:
  debug:
    verbosity: basic
  file:
    path: /out/traces.jsonl
    flush_interval: 500ms
service:
  pipelines:
    traces:
      receivers: [otlp]
      processors: [tail_sampling]
      exporters: [debug, file]
EOF
  docker run -d --name "$OTELCOL" --user "$(id -u):$(id -g)" -p "127.0.0.1:$OPORT:4318" \
    -v "$WORK/otelcol.yaml:/etc/otelcol-contrib/config.yaml:ro" -v "$WORK/otel-out:/out" "$OTELCOL_IMAGE" > "$WORK/otelcol.id" 2>&1 || fail "docker run $OTELCOL_IMAGE: $(cat "$WORK/otelcol.id")"
  if ! otlp_ready "$OPORT"; then
    docker logs "$OTELCOL" > "$WORK/otelcol.log" 2>&1 || true
    fail "the Collector did not accept OTLP/HTTP on 127.0.0.1:$OPORT within 60s: $(tail -n 30 "$WORK/otelcol.log")"
  fi
  EXPORTS+=(--export "http://127.0.0.1:$OPORT/v1/traces")
else
  echo "  (docker not available; OpenTelemetry Collector, auto-instrumentation and Jaeger checks skipped)"
fi

echo "▸ three services (db, api, frontend) exporting to $((${#EXPORTS[@]} / 2)) endpoint(s)"
start db serve db "${EXPORTS[@]}"
wait_line "$WORK/db.log" "listening on"
DB="$(url_of "$WORK/db.log")"
start api serve api --db "$DB" "${EXPORTS[@]}"
wait_line "$WORK/api.log" "listening on"
API="$(url_of "$WORK/api.log")"
start frontend serve frontend --api "$API" "${EXPORTS[@]}"
wait_line "$WORK/frontend.log" "listening on"
FE="$(url_of "$WORK/frontend.log")"
echo "  frontend $FE -> api $API -> db $DB"

echo "▸ load: 40 requests, faults carried in baggage, 4 of them unsampled at the root"
python3 code/main.py load --url "$FE/checkout" --requests 40 --seed 7 --pattern "$PATTERN" "${EXPORTS[@]}" --json > "$WORK/load.json" 2> "$WORK/load.err" || fail "load: $(cat "$WORK/load.err")"
python3 - "$WORK/load.json" <<'PY' || fail "load results: $(cat "$WORK/load.json")"
import collections, json, sys
rows = json.load(open(sys.argv[1]))
assert len(rows) == 40, len(rows)
for row in rows:
    want = 502 if row["fault"] == "db-error" else 200
    assert row["status"] == want, row
counts = collections.Counter((row["fault"], row["sampled"]) for row in rows)
print("  " + ", ".join(f"{fault}{'' if sampled else ' (unsampled)'} x{n}" for (fault, sampled), n in sorted(counts.items())))
PY

echo "▸ minitrace collector: every sampled trace decided, for the right reasons"
for _ in $(seq 1 30); do
  if python3 - "$MINI" "$WORK/decisions.json" <<'PY'
import json, sys, urllib.request
decisions = json.load(urllib.request.urlopen(sys.argv[1] + "/api/decisions", timeout=5))
json.dump(decisions, open(sys.argv[2], "w"))
sys.exit(0 if decisions["pending"] == 0 and len(decisions["decisions"]) >= 36 else 1)
PY
  then break; fi
  sleep 1
done
python3 - "$WORK/load.json" "$WORK/decisions.json" "$WORK/db.log" <<'PY' || fail "minitrace decisions: $(cat "$WORK/decisions.json")"
import json, sys
rows = json.load(open(sys.argv[1]))
decisions = json.load(open(sys.argv[2]))
db_log = open(sys.argv[3]).read().splitlines()
made = decisions["decisions"]
sampled = {row["trace_id"] for row in rows if row["sampled"]}
assert decisions["pending"] == 0 and set(made) == sampled, (decisions["pending"], len(made), len(sampled))
assert decisions["rejected_spans"] == 0, decisions["rejected_spans"]
for row in rows:
    tid = row["trace_id"]
    if not row["sampled"]:
        assert tid not in made, tid
        seen = [line for line in db_log if f"traceparent=00-{tid}-" in line]
        assert len(seen) == 1 and "-00 " in seen[0] and "status=503" in seen[0], (tid, seen)
        continue
    reasons = made[tid]["reasons"]
    assert made[tid]["spans"] == 10, (tid, made[tid])
    if row["fault"] == "db-error":
        assert "errors" in reasons, (row, reasons)
    elif row["fault"] in ("db-slow", "api-slow"):
        assert "slow" in reasons, (row, reasons)
    else:
        assert "errors" not in reasons, (row, reasons)
healthy = [made[row["trace_id"]]["keep"] for row in rows if row["sampled"] and row["fault"] == "ok"]
assert 0 < sum(healthy) < len(healthy), healthy
print(f"  {len(made)} traces decided, {len(decisions['kept'])} kept ({sum(healthy)} of {len(healthy)} healthy ones by the hash); 4 unsampled traces reached the db with flags 00 and were recorded nowhere")
PY

echo "▸ minitrace analyze: every kept trace is one tree; the critical path names the injected slow hop"
python3 code/main.py analyze "$WORK/mini-kept.jsonl" --strict --json > "$WORK/mini-analysis.json" || fail "kept traces are not all single trees: $(head -c 3000 "$WORK/mini-analysis.json")"
python3 - "$WORK/load.json" "$WORK/decisions.json" "$WORK/mini-analysis.json" <<'PY' || fail "minitrace analysis: $(head -c 3000 "$WORK/mini-analysis.json")"
import json, sys
fault = {row["trace_id"]: row["fault"] for row in json.load(open(sys.argv[1]))}
kept = set(json.load(open(sys.argv[2]))["kept"])
traces = {t["trace_id"]: t for t in json.load(open(sys.argv[3]))["traces"]}
assert set(traces) == kept, (sorted(set(traces) ^ kept))
for tid, t in traces.items():
    assert t["single_tree"] and t["spans"] == 10 and t["services"] == ["api", "db", "frontend", "loadgen"], t
    assert t["root"]["service"] == "loadgen", t["root"]
    hop = t["slowest_hop"]
    if fault[tid] == "db-slow":
        assert (hop["service"], hop["name"]) == ("db", "SELECT cart") and hop["ms"] >= 380, hop
    if fault[tid] == "api-slow":
        assert (hop["service"], hop["name"]) == ("api", "score") and hop["ms"] >= 330, hop
    if fault[tid] == "db-error":
        assert "db SELECT cart" in t["errors"], t["errors"]
print(f"  {len(traces)} kept traces, 10 spans and 4 services each; db-slow -> db SELECT cart, api-slow -> api score")
PY
FIRST="$(field "$WORK/mini-analysis.json" traces 0 trace_id)"
python3 code/main.py analyze "$WORK/mini-kept.jsonl" --trace "$FIRST" > "$WORK/waterfall.txt"
cat "$WORK/waterfall.txt"

if [[ -z "$HAVE_DOCKER" ]]; then
  echo "SMOKE OK: observability/tracing (minitrace only)"
  exit 0
fi

echo "▸ the Collector's tail_sampling kept exactly the same traces"
SAME=""
for _ in $(seq 1 40); do
  if [[ -s "$WORK/otel-out/traces.jsonl" ]] && python3 code/main.py analyze "$WORK/otel-out/traces.jsonl" --json > "$WORK/otel-analysis.json" 2>/dev/null \
    && python3 - "$WORK/mini-analysis.json" "$WORK/otel-analysis.json" <<'PY'
import json, sys
mine = {t["trace_id"] for t in json.load(open(sys.argv[1]))["traces"]}
theirs = {t["trace_id"] for t in json.load(open(sys.argv[2]))["traces"]}
sys.exit(0 if mine == theirs else 1)
PY
  then SAME=1; break; fi
  sleep 1
done
sleep 2
python3 code/main.py analyze "$WORK/otel-out/traces.jsonl" --strict --json > "$WORK/otel-analysis.json" 2> "$WORK/otel-analysis.err" || fail "Collector output is not all single trees or not readable: $(cat "$WORK/otel-analysis.err") $(head -c 2000 "$WORK/otel-analysis.json")"
python3 - "$WORK/mini-analysis.json" "$WORK/otel-analysis.json" "$WORK/decisions.json" <<'PY' || { docker logs "$OTELCOL" > "$WORK/otelcol.log" 2>&1 || true; fail "kept sets differ (stable after polling: ${SAME:-no}); Collector log tail: $(tail -n 20 "$WORK/otelcol.log")"; }
import json, sys
mine = {t["trace_id"]: t for t in json.load(open(sys.argv[1]))["traces"]}
theirs = {t["trace_id"]: t for t in json.load(open(sys.argv[2]))["traces"]}
reasons = json.load(open(sys.argv[3]))["decisions"]
only_mine, only_theirs = sorted(set(mine) - set(theirs)), sorted(set(theirs) - set(mine))
if only_mine or only_theirs:
    print("  kept only by minitrace:", [(tid, reasons.get(tid, {}).get("reasons")) for tid in only_mine])
    print("  kept only by the Collector:", [(tid, reasons.get(tid, {}).get("reasons")) for tid in only_theirs])
    raise SystemExit(1)
for tid, a in mine.items():
    b = theirs[tid]
    assert (a["spans"], a["services"], a["single_tree"]) == (b["spans"], b["services"], b["single_tree"]), (tid, a["spans"], b["spans"])
    assert [(s["service"], s["name"]) for s in a["critical_path"]] == [(s["service"], s["name"]) for s in b["critical_path"]], (tid, a["critical_path"], b["critical_path"])
    assert abs(a["duration_ms"] - b["duration_ms"]) < 0.001 and a["slowest_hop"]["span_id"] == b["slowest_hop"]["span_id"], (tid, a["slowest_hop"], b["slowest_hop"])
by_reason = {}
for tid in mine:
    by_reason.setdefault(",".join(reasons[tid]["reasons"]), 0)
    by_reason[",".join(reasons[tid]["reasons"])] += 1
print(f"  both kept the same {len(mine)} traces {by_reason}; span counts, critical paths and slowest hops identical")
PY
docker logs "$OTELCOL" > "$WORK/otelcol.log" 2>&1 || true
grep -m 1 -i "traces" "$WORK/otelcol.log" > "$WORK/otelcol-debug.txt" || true
echo "  debug exporter: $(cut -c1-160 "$WORK/otelcol-debug.txt")"

echo "▸ auto-instrumentation: an opentelemetry-instrument client continues into minitrace's services"
python3 -m venv "$WORK/venv" || fail "python3 -m venv failed"
"$WORK/venv/bin/pip" install --quiet --disable-pip-version-check "${OTEL_PY[@]}" > "$WORK/pip.log" 2>&1 || fail "pip install ${OTEL_PY[*]}: $(tail -n 20 "$WORK/pip.log")"
cat > "$WORK/probe.py" <<'EOF'
import sys, urllib.error, urllib.request
from opentelemetry import baggage, context, trace
with trace.get_tracer("cc-probe").start_as_current_span("checkout-probe") as span:
    token = context.attach(baggage.set_baggage("cc.fault", "db-error"))
    try:
        urllib.request.urlopen(sys.argv[1], timeout=10).read()
    except urllib.error.HTTPError as exc:
        print("frontend answered", exc.code, file=sys.stderr)
    finally:
        context.detach(token)
    print(format(span.get_span_context().trace_id, "032x"))
EOF
OTEL_SERVICE_NAME=checkout-probe OTEL_TRACES_EXPORTER=otlp OTEL_EXPORTER_OTLP_TRACES_PROTOCOL=http/protobuf \
OTEL_EXPORTER_OTLP_TRACES_ENDPOINT="http://127.0.0.1:$OPORT/v1/traces" OTEL_METRICS_EXPORTER=none OTEL_LOGS_EXPORTER=none \
  "$WORK/venv/bin/opentelemetry-instrument" "$WORK/venv/bin/python" "$WORK/probe.py" "$FE/checkout" > "$WORK/probe.out" 2> "$WORK/probe.err" || fail "instrumented probe: $(cat "$WORK/probe.err")"
PROBE="$(tail -n 1 "$WORK/probe.out")"
[[ "$PROBE" =~ ^[0-9a-f]{32}$ ]] || fail "the probe printed no trace id: $(cat "$WORK/probe.out") $(cat "$WORK/probe.err")"
grep -q "frontend answered 502" "$WORK/probe.err" || fail "the db-error fault in the SDK's baggage should make checkout answer 502: $(cat "$WORK/probe.err")"
FOUND=""
for _ in $(seq 1 30); do
  if python3 - "$WORK/otel-out/traces.jsonl" "$PROBE" 2>/dev/null <<'PY'
import sys
sys.path.insert(0, "code")
import main
spans = main.group_traces(main.load_otlp_files([sys.argv[1]])).get(sys.argv[2], [])
sys.exit(0 if len(spans) >= 11 else 1)
PY
  then FOUND=1; break; fi
  sleep 1
done
[[ -n "$FOUND" ]] || fail "trace $PROBE did not reach the Collector's file exporter with 11 spans: $(cat "$WORK/probe.err")"
python3 - "$WORK/otel-out/traces.jsonl" "$PROBE" <<'PY' || fail "the SDK and minitrace spans do not form one tree for $PROBE"
import sys
sys.path.insert(0, "code")
import main
spans = main.group_traces(main.load_otlp_files([sys.argv[1]]))[sys.argv[2]]
by_id = {s.span_id: s for s in spans}
sdk = [s for s in spans if s.service == "checkout-probe"]
client = [s for s in sdk if s.kind == main.KIND_CLIENT]
server = [s for s in spans if s.service == "frontend" and s.kind == main.KIND_SERVER]
assert len(sdk) == 2 and len(client) == 1 and len(server) == 1, [(s.service, s.name, s.kind) for s in spans]
assert server[0].parent_span_id == client[0].span_id, (server[0].parent_span_id, client[0].span_id)
assert by_id[client[0].parent_span_id].name == "checkout-probe", client[0].parent_span_id
db = [s for s in spans if s.service == "db" and s.kind == main.KIND_SERVER]
assert db and db[0].attributes.get("cc.fault") == "db-error", db[0].attributes if db else "no db span"
summary = main.summarize(spans)
assert summary["single_tree"] and summary["root"]["name"] == "checkout-probe", summary
print(f"  one tree of {len(spans)} spans: checkout-probe -> SDK {client[0].name} CLIENT {client[0].span_id} -> frontend SERVER (parent {server[0].parent_span_id}); baggage cc.fault reached the db")
PY
for _ in $(seq 1 20); do
  python3 - "$MINI" "$PROBE" "$WORK/mini-probe.json" 2>/dev/null <<'PY' && break
import json, sys, urllib.request
summary = json.load(urllib.request.urlopen(f"{sys.argv[1]}/api/traces/{sys.argv[2]}", timeout=5))
json.dump(summary, open(sys.argv[3], "w"))
sys.exit(0 if summary.get("decision") else 1)
PY
  sleep 1
done
python3 - "$WORK/mini-probe.json" "$WORK/otel-out/traces.jsonl" "$PROBE" <<'PY' || fail "minitrace's view of $PROBE: $(cat "$WORK/mini-probe.json" 2>/dev/null)"
import json, sys
sys.path.insert(0, "code")
import main
mine = json.load(open(sys.argv[1]))
spans = main.group_traces(main.load_otlp_files([sys.argv[2]]))[sys.argv[3]]
client = [s for s in spans if s.service == "checkout-probe" and s.kind == main.KIND_CLIENT][0]
assert mine["spans"] == 9 and mine["single_tree"] is False and mine["missing_parents"] == [client.span_id], mine
assert mine["decision"]["keep"] and "errors" in mine["decision"]["reasons"], mine["decision"]
print(f"  minitrace's collector holds the 9 minitrace spans and names the missing parent {client.span_id}: the SDK exported only to the Collector")
PY

echo "▸ Jaeger $JAEGER_IMAGE: replay the kept traces over OTLP and read one back from the query API"
docker run -d --name "$JAEGER" -p "127.0.0.1:$JPORT:4318" -p "127.0.0.1:$QPORT:16686" "$JAEGER_IMAGE" > "$WORK/jaeger.id" 2>&1 || fail "docker run $JAEGER_IMAGE: $(cat "$WORK/jaeger.id")"
if ! otlp_ready "$JPORT"; then
  docker logs "$JAEGER" > "$WORK/jaeger.log" 2>&1 || true
  fail "Jaeger did not accept OTLP/HTTP on 127.0.0.1:$JPORT within 60s: $(tail -n 30 "$WORK/jaeger.log")"
fi
python3 code/main.py export "$WORK/mini-kept.jsonl" --endpoint "http://127.0.0.1:$JPORT/v1/traces" > "$WORK/jaeger-export.txt" 2>&1 || fail "export to Jaeger: $(cat "$WORK/jaeger-export.txt")"
cat "$WORK/jaeger-export.txt"
SLOW="$(python3 -c 'import json,sys; kept=set(t["trace_id"] for t in json.load(open(sys.argv[2]))["traces"]); print(next(r["trace_id"] for r in json.load(open(sys.argv[1])) if r["fault"] == "db-slow" and r["trace_id"] in kept))' "$WORK/load.json" "$WORK/mini-analysis.json")"
GOT=""
for _ in $(seq 1 30); do
  if python3 - "$QPORT" "$SLOW" "$WORK/jaeger-trace.json" 2>/dev/null <<'PY'
import json, sys, urllib.request
body = json.load(urllib.request.urlopen(f"http://127.0.0.1:{sys.argv[1]}/api/traces/{sys.argv[2]}", timeout=5))
json.dump(body, open(sys.argv[3], "w"))
sys.exit(0 if body.get("data") and len(body["data"][0].get("spans") or []) >= 10 else 1)
PY
  then GOT=1; break; fi
  sleep 1
done
[[ -n "$GOT" ]] || fail "Jaeger has no complete trace $SLOW: $(head -c 1500 "$WORK/jaeger-trace.json" 2>/dev/null)"
python3 - "$WORK/jaeger-trace.json" <<'PY' || fail "Jaeger trace: $(head -c 1500 "$WORK/jaeger-trace.json")"
import json, sys
trace = json.load(open(sys.argv[1]))["data"][0]
services = sorted({p["serviceName"] for p in trace["processes"].values()})
query = [s for s in trace["spans"] if s["operationName"] == "SELECT cart"]
assert len(trace["spans"]) == 10 and services == ["api", "db", "frontend", "loadgen"], (len(trace["spans"]), services)
assert query and query[0]["duration"] >= 380_000, query
print(f"  Jaeger: {len(trace['spans'])} spans from {services}; SELECT cart {query[0]['duration'] / 1000:.1f} ms")
PY

echo "SMOKE OK: observability/tracing"
