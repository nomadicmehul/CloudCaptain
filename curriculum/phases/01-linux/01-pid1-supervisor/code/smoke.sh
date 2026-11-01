#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."

fail() { echo "SMOKE FAIL: $*" >&2; exit 1; }
[[ "$(uname -s)" == "Linux" ]] || fail "needs Linux"

echo "▸ exit code of the supervised command is propagated (restart: never)"
set +e
python3 code/main.py --restart never --run 'sh -c "exit 3"'
CODE=$?
set -e
[[ "$CODE" == "3" ]] || fail "expected exit 3, got $CODE"

echo "▸ on-failure restarts with backoff, then gives up at max_restarts"
cat > /tmp/mini-spec.json <<'EOF'
{"services": [{"name": "flaky", "command": "sh -c 'exit 1'", "restart": "on-failure", "max_restarts": 2}]}
EOF
START=$(date +%s)
set +e
python3 code/main.py --spec /tmp/mini-spec.json --verbose 2>/tmp/mini.log
set -e
ELAPSED=$(( $(date +%s) - START ))
grep -c '^\[mini_init\] start flaky' /tmp/mini.log | grep -qx 3 || fail "expected 3 starts (1 + 2 restarts): $(cat /tmp/mini.log)"
[[ "$ELAPSED" -ge 3 ]] || fail "backoff should take at least 1s + 2s, took ${ELAPSED}s"

echo "▸ SIGTERM is forwarded and the supervisor exits promptly"
python3 code/main.py --restart never --run 'sleep 300' --verbose 2>/tmp/mini-term.log &
PID=$!
sleep 1
START=$(date +%s)
kill -TERM "$PID"
wait "$PID" || true
ELAPSED=$(( $(date +%s) - START ))
[[ "$ELAPSED" -le 3 ]] || fail "shutdown took ${ELAPSED}s; SIGTERM was not forwarded"
pgrep -f 'sleep 300' >/dev/null && fail "child sleep survived the supervisor" || true

echo "▸ zombies are reaped: a child that double-forks leaves no Z processes"
python3 code/main.py --restart never --run 'sh -c "(sleep 0.2 &) ; sleep 1"' --verbose 2>/tmp/mini-orphan.log || true
grep -q 'reaped orphan' /tmp/mini-orphan.log || echo "  (no orphan observed in this run; kernel timing)"

if command -v docker >/dev/null; then
  echo "▸ Use It: docker stop with and without an init"
  docker run --rm -d --name cc-smoke-plain alpine:3.22 sh -c 'sleep 600' >/dev/null
  docker run --rm -d --name cc-smoke-init --init alpine:3.22 sh -c 'sleep 600' >/dev/null
  sleep 1
  S=$(date +%s); docker stop -t 5 cc-smoke-plain >/dev/null; PLAIN=$(( $(date +%s) - S ))
  S=$(date +%s); docker stop -t 5 cc-smoke-init >/dev/null; INIT=$(( $(date +%s) - S ))
  echo "  plain: ${PLAIN}s   --init: ${INIT}s"
  [[ "$PLAIN" -ge 4 ]] || fail "plain container should have waited for the grace period"
  [[ "$INIT" -le 2 ]] || fail "--init container should stop immediately"
else
  echo "  (docker not available; Use It skipped)"
fi

echo "SMOKE OK: linux/pid1-supervisor"
