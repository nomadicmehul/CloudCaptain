#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."

fail() { echo "SMOKE FAIL: $*" >&2; exit 1; }
SERVER="../01-mcp-kubectl-server/code/main.py"
[[ -f "$SERVER" ]] || fail "needs the MCP kubectl lesson next to this one"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
mkdir -p "$WORK/bin"
cat > "$WORK/bin/kubectl" <<'EOF'
#!/bin/sh
case "$1 $2" in
  "rollout restart") echo "deployment.apps/${3#deployment/} restarted" ;;
  *) echo "fake-kubectl $*" ;;
esac
EOF
chmod +x "$WORK/bin/kubectl"
REAL_PATH="$PATH"
export PATH="$WORK/bin:$PATH"
REPLAY=code/fixtures/replay-crashloop.json
MCP="python3 $SERVER --allow-namespaces default --quiet"
INCIDENT="checkout is in CrashLoopBackOff"

echo "▸ replayed investigation through the real MCP server (no API key needed)"
python3 code/main.py "$INCIDENT" --mcp "$MCP" --replay "$REPLAY" --namespaces default --trace "$WORK/trace.jsonl" --json > "$WORK/run1.json"
python3 - "$WORK/run1.json" "$WORK/trace.jsonl" <<'PY' || fail "replay outcome"
import json, sys
r = json.load(open(sys.argv[1]))
assert r["stop"] == "report" and r["report"]["severity"] == "sev2", r["stop"]
outcomes = [e["outcome"] for e in r["audit"]]
assert outcomes == ["ok", "ok", "ok", "ok", "refused", "refused", "report"], outcomes
assert r["violations"] == ["step 4: unknown tool 'run_kubectl'"], r["violations"]
assert "fake-kubectl" in r["audit"][0]["detail"], r["audit"][0]
assert "--previous" in r["audit"][3]["detail"], r["audit"][3]
trace = [json.loads(line) for line in open(sys.argv[2]) if line.strip()]
assert len(trace) == 5 and trace[0]["gen_ai.usage.input_tokens"] == 1800, trace[:1]
print("  steps", r["steps"], "tool calls", outcomes.count("ok"), "violations", len(r["violations"]), "cost $%.4f" % r["cost_usd"], "trace lines", len(trace))
PY

echo "▸ the proposed restart: refused when not enabled, printed when approved, executed only with --execute"
python3 code/main.py "$INCIDENT" --mcp "$MCP" --replay "$REPLAY" --namespaces default --allow-action rollout_restart --yes --json > "$WORK/run2.json"
python3 -c 'import json,sys; e=json.load(open(sys.argv[1]))["audit"][5]; assert e["outcome"]=="approved" and e["detail"]=="kubectl rollout restart deployment/checkout -n default", e; print("  approved ->", e["detail"])' "$WORK/run2.json" || fail "approval without --execute"
python3 code/main.py "$INCIDENT" --mcp "$MCP" --replay "$REPLAY" --namespaces default --allow-action rollout_restart --yes --execute --json > "$WORK/run3.json" 2>"$WORK/run3.err"
grep -q "WARNING --yes with --execute" "$WORK/run3.err" || fail "the loop should warn when the human is removed"
python3 -c 'import json,sys; e=json.load(open(sys.argv[1]))["audit"][5]; assert e["outcome"]=="executed" and "restarted" in e["detail"], e; print("  executed ->", e["detail"])' "$WORK/run3.json" || fail "execution with --execute"

echo "▸ the human-readable report"
python3 code/main.py "$INCIDENT" --mcp "$MCP" --replay "$REPLAY" --namespaces default | tee "$WORK/report.txt"
grep -q "REPORT \[sev2\] confidence medium" "$WORK/report.txt" || fail "report header"
grep -q "VIOLATION step 4: unknown tool 'run_kubectl'" "$WORK/report.txt" || fail "violation line"

export PATH="$REAL_PATH"
if command -v kubectl >/dev/null && kubectl cluster-info >/dev/null 2>&1; then
  echo "▸ the same replay against the real cluster: tool results are real kubectl output"
  kubectl run cc-smoke-broken --image=alpine:3.22 --restart=Always -- sh -c 'echo boom; exit 1' >/dev/null 2>&1 || true
  sleep 8
  python3 code/main.py "$INCIDENT" --mcp "$MCP" --replay "$REPLAY" --namespaces default --json > "$WORK/run4.json"
  python3 -c 'import json,sys; r=json.load(open(sys.argv[1])); pods=r["audit"][1]["detail"]; assert "cc-smoke-broken" in pods, pods; print("  real list_pods saw cc-smoke-broken; stop", r["stop"])' "$WORK/run4.json" || fail "real-cluster replay"
  kubectl delete pod cc-smoke-broken --wait=false >/dev/null 2>&1 || true
else
  echo "  (no reachable cluster; real-cluster replay skipped)"
fi

if [[ -n "${ANTHROPIC_API_KEY:-}" ]]; then
  echo "▸ live run with the model (ANTHROPIC_API_KEY is set)"
  export PATH="$WORK/bin:$PATH"
  python3 code/main.py "pods in default are crashing" --mcp "$MCP" --namespaces default --max-steps 8 --max-tool-calls 8 --json > "$WORK/live.json" || true
  python3 -c 'import json,sys; r=json.load(open(sys.argv[1])); assert r["stop"] in ("report","end_turn"), r["stop"]; print("  live stop", r["stop"], "cost $%.4f" % r["cost_usd"])' "$WORK/live.json" || fail "live run"
else
  echo "  (ANTHROPIC_API_KEY not set; live run skipped)"
fi

echo "SMOKE OK: aiops/incident-triage-agent"
