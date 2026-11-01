#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."

fail() { echo "SMOKE FAIL: $*" >&2; exit 1; }
[[ "$(uname -s)" == "Linux" ]] || fail "needs Linux"
WORK="$(mktemp -d)"
MANAGER=""
TRANSIENT="cc-smoke-$$"
cleanup() {
  if [[ -n "$MANAGER" ]] && kill -0 "$MANAGER" 2>/dev/null; then
    kill -TERM "$MANAGER" 2>/dev/null || true
    wait "$MANAGER" 2>/dev/null || true
  fi
  if command -v systemctl >/dev/null 2>&1 && sudo -n true 2>/dev/null; then
    sudo -n systemctl stop "$TRANSIENT" 2>/dev/null || true
    sudo -n systemctl reset-failed "$TRANSIENT" 2>/dev/null || true
  fi
  rm -rf "$WORK"
}
trap cleanup EXIT
MU=(python3 code/main.py --units code/units)

echo "▸ verify: four sample units parse and the dependency graph is sound"
"${MU[@]}" verify | tee "$WORK/verify.txt"
grep -q "4 unit(s), 0 error(s)" "$WORK/verify.txt" || fail "verify should report 4 units and 0 errors"

echo "▸ graph: After= decides the order, Requires=/Wants= pull units in"
"${MU[@]}" graph web.service | tee "$WORK/graph.txt"
grep -q "^start order: db.service -> migrate.service -> web.service$" "$WORK/graph.txt" || fail "start order"
grep -qE "db.service +Requires After" "$WORK/graph.txt" || fail "graph should show Requires After for db.service"

echo "▸ start web.service (pulls in db + the migrate oneshot) and the flaky oneshot, stop after 7 s"
set +e
"${MU[@]}" --state "$WORK/run1" start web.service flaky.service --for 7 --verbose 2>"$WORK/run1.log"
CODE=$?
set -e
[[ "$CODE" == "1" ]] || { cat "$WORK/run1.log" >&2; fail "expected exit 1 because flaky.service failed, got $CODE"; }
python3 code/main.py --state "$WORK/run1" journal | tee "$WORK/journal1.txt"
python3 code/main.py --state "$WORK/run1" status --json > "$WORK/status1.json"
python3 - "$WORK/status1.json" "$WORK/journal1.txt" <<'PY' || fail "run 1 did not end in the expected states"
import json, sys
units = json.load(open(sys.argv[1]))
journal = open(sys.argv[2]).read().splitlines()
def row(name):
    u = units[name]
    return (u["active"], u["sub"], u["result"], u["restarts"])
assert row("web.service") == ("inactive", "dead", "success", 0), row("web.service")
assert row("db.service") == ("inactive", "dead", "success", 0), row("db.service")
assert row("migrate.service") == ("inactive", "dead", "success", 0), row("migrate.service")
assert row("flaky.service") == ("failed", "failed", "start-limit-hit", 3), row("flaky.service")
def first(text):
    return next(i for i, line in enumerate(journal) if text in line)
assert first("migration done") < first("hello from web on port 8080"), "web must wait for the oneshot it is After="
assert first("preparing /tmp/cc-demo-db") < first("db listening on 5432"), "ExecStartPre runs before ExecStart"
assert sum("flaky attempt" in line for line in journal) == 3, "StartLimitBurst=3 allows three starts"
assert any("Start request repeated too quickly" in line for line in journal)
assert any("web stopped: success killed TERM" in line for line in journal), "ExecStopPost sees $SERVICE_RESULT $EXIT_CODE $EXIT_STATUS"
assert first("Stopped web.service") < first("Stopping db.service"), "stop runs in reverse order: the dependent first"
print("  web/db/migrate stopped cleanly; flaky.service hit the start limit after 3 attempts")
PY
set +e
python3 code/main.py --state "$WORK/run1" status flaky.service > "$WORK/status1.txt"
set -e
grep -q "× flaky.service" "$WORK/status1.txt" || fail "status glyph for a failed unit"
grep -q "(Result: start-limit-hit)" "$WORK/status1.txt" || fail "status should show the result"

echo "▸ live manager: status while running, then 'stop db.service' propagates to web.service through Requires="
"${MU[@]}" --state "$WORK/run2" start web.service &
MANAGER=$!
for _ in $(seq 1 100); do
  python3 code/main.py --state "$WORK/run2" status --json web.service 2>/dev/null | grep -q '"active": "active"' && break
  sleep 0.1
done
python3 code/main.py --state "$WORK/run2" status web.service | tee "$WORK/status2.txt"
grep -q "manager: running, pid $MANAGER" "$WORK/status2.txt" || fail "status should see the running manager"
grep -q "Active: active (running) since" "$WORK/status2.txt" || fail "web.service should be active (running)"
python3 code/main.py --state "$WORK/run2" stop db.service | tee "$WORK/stop2.txt"
grep -q "^db.service: inactive (dead)$" "$WORK/stop2.txt" || fail "db.service should be stopped"
python3 code/main.py --state "$WORK/run2" status --json > "$WORK/status2.json"
python3 - "$WORK/status2.json" <<'PY' || fail "Requires= propagation"
import json, sys
units = json.load(open(sys.argv[1]))
assert units["web.service"]["active"] == "inactive", units["web.service"]
assert units["migrate.service"]["active"] == "active" and units["migrate.service"]["sub"] == "exited", units["migrate.service"]
print("  web.service stopped with db.service; migrate.service (only Wanted) is still active (exited)")
PY
python3 code/main.py --state "$WORK/run2" journal -u miniunit | grep -E "Stopping (web|db)" | tee "$WORK/order2.txt"
[[ "$(head -1 "$WORK/order2.txt")" == *"Stopping web.service" ]] || fail "the dependent must stop first"
python3 code/main.py --state "$WORK/run2" stop
wait "$MANAGER"
CODE=$?
MANAGER=""
[[ "$CODE" == "0" ]] || fail "manager should exit 0 after a clean stop, got $CODE"

if command -v systemd-analyze >/dev/null 2>&1; then
  echo "▸ Use It: systemd-analyze verify on the same unit files"
  systemd-analyze verify "$PWD/code/units/web.service" "$PWD/code/units/flaky.service" 2>&1 | tee "$WORK/analyze.txt" || fail "systemd-analyze verify rejected the sample units"
  echo "  systemd-analyze verify accepted web.service (with db + migrate) and flaky.service"
  if [[ "$(ps -o comm= -p 1 2>/dev/null | tr -d ' ')" == "systemd" ]] && command -v systemd-run >/dev/null 2>&1 && sudo -n true 2>/dev/null; then
    echo "▸ Use It: a transient unit with systemd-run, read back with systemctl show and journalctl"
    sudo systemd-run --unit "$TRANSIENT" --remain-after-exit --property=Restart=no /bin/sh -c 'echo hello from a transient unit'
    sleep 1
    systemctl show "$TRANSIENT" -p ActiveState -p SubState -p Result -p NRestarts | tee "$WORK/show.txt"
    grep -q "^ActiveState=active$" "$WORK/show.txt" || fail "transient unit should be active (RemainAfterExit)"
    grep -q "^SubState=exited$" "$WORK/show.txt" || fail "transient unit should be in sub-state exited"
    grep -q "^Result=success$" "$WORK/show.txt" || fail "transient unit result should be success"
    systemctl status "$TRANSIENT" --no-pager || true
    FOUND=""
    for _ in $(seq 1 10); do
      if sudo journalctl -u "$TRANSIENT" --no-pager -o cat 2>/dev/null | grep -q "hello from a transient unit"; then FOUND=yes; break; fi
      sleep 1
    done
    [[ -n "$FOUND" ]] || fail "journalctl -u $TRANSIENT did not show the unit's stdout"
    sudo systemctl stop "$TRANSIENT"
    systemctl show "$TRANSIENT" -p LoadState | tee "$WORK/load.txt"
    grep -q "LoadState=not-found" "$WORK/load.txt" || echo "  (transient unit still loaded after stop; reset-failed runs in cleanup)"
  else
    echo "  (systemd is not PID 1 here or sudo is unavailable; systemd-run skipped)"
  fi
else
  echo "  (systemd-analyze not installed; Use It systemd checks skipped)"
fi

echo "SMOKE OK: linux/systemd-unit-runner"
