#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."

fail() { echo "SMOKE FAIL: $*" >&2; exit 1; }
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

echo "▸ full JSON-RPC session over stdio with a fake kubectl on PATH"
mkdir -p "$WORK/bin"
cat > "$WORK/bin/kubectl" <<'EOF'
#!/bin/sh
echo "fake-kubectl $*"
EOF
chmod +x "$WORK/bin/kubectl"
SESSION='{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"smoke","version":"1"}}}
{"jsonrpc":"2.0","method":"notifications/initialized"}
{"jsonrpc":"2.0","id":2,"method":"tools/list"}
{"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"list_pods","arguments":{"namespace":"dev"}}}
{"jsonrpc":"2.0","id":4,"method":"tools/call","params":{"name":"list_pods","arguments":{"namespace":"prod"}}}'
OUT="$(printf '%s\n' "$SESSION" | PATH="$WORK/bin:$PATH" python3 code/main.py --allow-namespaces dev,staging --quiet)"
echo "$OUT" | python3 -c '
import json, sys
lines = [json.loads(l) for l in sys.stdin if l.strip()]
assert [l["id"] for l in lines] == [1, 2, 3, 4], lines
assert lines[0]["result"]["serverInfo"]["name"] == "kubectl-readonly"
assert {t["name"] for t in lines[1]["result"]["tools"]} == {"list_pods", "describe_pod", "pod_logs", "list_events"}
assert "fake-kubectl get pods -n dev" in lines[2]["result"]["content"][0]["text"], lines[2]
assert lines[3]["result"]["isError"] is True and "allow-list" in lines[3]["result"]["content"][0]["text"], lines[3]
print("  handshake, tools/list, tools/call and the allow-list all behave")
' || fail "stdio session"

echo "▸ stdout carries only protocol messages (logs go to stderr)"
printf '%s\n' '{"jsonrpc":"2.0","id":1,"method":"ping"}' | python3 code/main.py 2>/dev/null | python3 -c 'import json,sys; [json.loads(l) for l in sys.stdin if l.strip()]; print("  stdout is clean JSON")' || fail "stdout polluted"

if command -v kubectl >/dev/null && kubectl cluster-info >/dev/null 2>&1; then
  echo "▸ Use It against the real cluster in this environment"
  REAL="$(printf '%s\n' '{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"list_pods","arguments":{"namespace":"kube-system"}}}' | python3 code/main.py --quiet)"
  echo "$REAL" | python3 -c '
import json, sys
r = json.loads(sys.stdin.readline())
text = r["result"]["content"][0]["text"]
assert r["result"]["isError"] is False, text
assert "kube-system" in text or "coredns" in text or "NAME" in text, text
print("  real kubectl answered through the server")
' || fail "real cluster call"
else
  echo "  (no reachable cluster; real-cluster step skipped)"
fi

echo "SMOKE OK: aiops/mcp-kubectl-server"
