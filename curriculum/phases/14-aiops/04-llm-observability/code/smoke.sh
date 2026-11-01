#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."

fail() { echo "SMOKE FAIL: $*" >&2; exit 1; }
WORK="$(mktemp -d)"
COLLECTOR="cc-smoke-otelcol-$$"
cleanup() {
  if command -v docker >/dev/null; then docker rm -f "$COLLECTOR" >/dev/null 2>&1 || true; fi
  rm -rf "$WORK"
}
trap cleanup EXIT

echo "▸ convert the three fixture runs into OTLP/HTTP JSON"
for run in crashloop oomkilled maxsteps; do
  python3 code/main.py convert --trace "code/fixtures/$run.trace.jsonl" --outcome "code/fixtures/$run.outcome.json" --start 2026-11-01T02:03:00Z -o "$WORK/$run.otlp.json"
done
python3 - "$WORK/crashloop.otlp.json" <<'PY' || fail "OTLP shape"
import json, re, sys
payload = json.load(open(sys.argv[1]))
spans = payload["resourceSpans"][0]["scopeSpans"][0]["spans"]
assert len(spans) == 13, len(spans)
for span in spans:
    assert re.fullmatch(r"[0-9a-f]{32}", span["traceId"]) and re.fullmatch(r"[0-9a-f]{16}", span["spanId"]), span["name"]
    assert span["startTimeUnixNano"].isdigit() and span["endTimeUnixNano"].isdigit(), span["name"]
attributes = {a["key"]: a["value"] for a in spans[1]["attributes"]}
assert attributes["gen_ai.request.model"] == {"stringValue": "claude-opus-5-5"}, attributes
assert attributes["gen_ai.usage.input_tokens"] == {"intValue": "1800"}, attributes
print("  13 spans; ids and timestamps well formed; GenAI attributes typed")
PY

echo "▸ view: the run as a tree with durations, tokens and cost"
python3 code/main.py view "$WORK/crashloop.otlp.json" | tee "$WORK/view.txt"
grep -q "invoke_agent triage_agent" "$WORK/view.txt" || fail "root span missing from the tree"
grep -q "execute_tool list_events" "$WORK/view.txt" || fail "tool span missing from the tree"
grep -q '\$0.0774' "$WORK/view.txt" || fail "cost missing from the root line"
grep -q "refused  unknown tool  ERROR" "$WORK/view.txt" || fail "a refused tool call should be an ERROR span"

echo "▸ metrics over the three runs"
python3 code/main.py metrics "$WORK"/*.otlp.json | tee "$WORK/metrics.txt"
python3 code/main.py metrics "$WORK"/*.otlp.json --json > "$WORK/metrics.json"
python3 - "$WORK/metrics.json" <<'PY' || fail "metrics"
import json, sys
m = json.load(open(sys.argv[1]))
assert m["runs"] == 3, m["runs"]
assert m["latency_ms"]["claude-opus-5-5"]["p95"] == 5390.0, m["latency_ms"]
assert m["runs_without_report"] == 1, m
assert abs(m["cost_per_run_usd"]["total"] - 0.18418) < 1e-6, m["cost_per_run_usd"]
assert m["tool_error_rate"] == 0.25, m["tool_error_rate"]
print("  runs", m["runs"], " p95 opus", m["latency_ms"]["claude-opus-5-5"]["p95"], "ms  total $%.4f" % m["cost_per_run_usd"]["total"], " tool error rate", m["tool_error_rate"])
PY

AGENT="../02-incident-triage-agent/code/main.py"
SERVER="../01-mcp-kubectl-server/code/main.py"
REPLAY="../02-incident-triage-agent/code/fixtures/replay-crashloop.json"
if [[ -f "$AGENT" && -f "$SERVER" && -f "$REPLAY" ]]; then
  echo "▸ a fresh run of the real triage agent (replayed, fake kubectl), converted and viewed"
  mkdir -p "$WORK/bin"
  printf '#!/bin/sh\necho "fake-kubectl $*"\n' > "$WORK/bin/kubectl"
  chmod +x "$WORK/bin/kubectl"
  PATH="$WORK/bin:$PATH" python3 "$AGENT" "checkout is in CrashLoopBackOff" --mcp "python3 $SERVER --allow-namespaces default --quiet" --replay "$REPLAY" --namespaces default --trace "$WORK/live.jsonl" --json > "$WORK/live.json"
  python3 code/main.py convert --trace "$WORK/live.jsonl" --outcome "$WORK/live.json" -o "$WORK/live.otlp.json"
  python3 code/main.py view "$WORK/live.otlp.json" | tee "$WORK/live-view.txt"
  [[ "$(grep -c 'execute_tool' "$WORK/live-view.txt")" == "7" ]] || fail "expected 7 execute_tool spans from the replayed run"
  grep -q "stop report" "$WORK/live-view.txt" || fail "the replayed run should end in a report"
else
  echo "  (triage agent lesson not found next to this one; live conversion skipped)"
fi

probe() {
  python3 - "$1" <<'PY'
import sys, urllib.request
request = urllib.request.Request(f"http://127.0.0.1:{sys.argv[1]}/v1/traces", data=b"{}", headers={"content-type": "application/json"}, method="POST")
urllib.request.urlopen(request, timeout=2).read()
PY
}

if command -v docker >/dev/null; then
  echo "▸ Use It: export to an OpenTelemetry Collector (OTLP/HTTP receiver + debug exporter) running in Docker"
  PORT=$((20000 + RANDOM % 20000))
  cat > "$WORK/collector.yaml" <<'EOF'
receivers:
  otlp:
    protocols:
      http:
        endpoint: 0.0.0.0:4318
exporters:
  debug:
    verbosity: detailed
service:
  pipelines:
    traces:
      receivers: [otlp]
      exporters: [debug]
EOF
  docker run -d --name "$COLLECTOR" -p "127.0.0.1:$PORT:4318" -v "$WORK/collector.yaml:/etc/otelcol-contrib/config.yaml:ro" "${OTELCOL_IMAGE:-otel/opentelemetry-collector-contrib:0.163.2}" >/dev/null
  READY=0
  for _ in $(seq 1 60); do
    if probe "$PORT" 2>/dev/null; then READY=1; break; fi
    sleep 1
  done
  [[ "$READY" == "1" ]] || { docker logs "$COLLECTOR" 2>&1 | tail -20; fail "collector did not accept OTLP/HTTP on port $PORT within 60s"; }
  python3 code/main.py export "$WORK/crashloop.otlp.json" --endpoint "http://127.0.0.1:$PORT/v1/traces" | tee "$WORK/export.txt"
  grep -q "13 spans" "$WORK/export.txt" || fail "export did not report 13 accepted spans"
  sleep 2
  docker logs "$COLLECTOR" > "$WORK/collector.log" 2>&1
  grep -q "gen_ai.request.model" "$WORK/collector.log" || { tail -40 "$WORK/collector.log"; fail "debug exporter did not print gen_ai.request.model"; }
  grep -q "claude-opus-5-5" "$WORK/collector.log" || fail "model name missing from the collector output"
  grep -q "execute_tool list_events" "$WORK/collector.log" || fail "tool span missing from the collector output"
  echo "  collector printed $(grep -c 'gen_ai.operation.name' "$WORK/collector.log") spans carrying gen_ai.operation.name"
else
  echo "  (docker not installed; collector export skipped)"
fi

echo "SMOKE OK: aiops/llm-observability"
