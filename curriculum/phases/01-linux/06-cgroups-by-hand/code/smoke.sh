#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."

fail() { echo "SMOKE FAIL: $*" >&2; exit 1; }
[[ "$(uname -s)" == "Linux" ]] || fail "needs Linux (cgroup v2)"
CG=/sys/fs/cgroup
[[ -f "$CG/cgroup.controllers" ]] || fail "needs cgroup v2 mounted at $CG (cgroup v1 is legacy)"
SUDO=""; [[ "$(id -u)" == "0" ]] || SUDO="sudo"
BASE="cc-smoke-$$"
UNIT="cc-smoke-unit-$$"
WORK="$(mktemp -d)"
cleanup() {
  docker rm -f cc-smoke-cg >/dev/null 2>&1 || true
  $SUDO systemctl stop "$UNIT" >/dev/null 2>&1 || true
  if [[ -d "$CG/$BASE" ]]; then
    $SUDO python3 code/main.py delete "$BASE" >/dev/null 2>&1 || $SUDO find "$CG/$BASE" -depth -type d -exec rmdir {} \; 2>/dev/null || true
  fi
  rm -rf "$WORK"
}
trap cleanup EXIT
j() { python3 -c 'import json, sys
d = json.load(sys.stdin)
for k in sys.argv[1].split("."):
    d = d[k]
print(d)' "$1"; }

echo "▸ controllers cpu, memory and pids are handed down from the root"
for c in cpu memory pids; do
  grep -qw "$c" "$CG/cgroup.controllers" || fail "controller $c not available in $CG/cgroup.controllers"
  grep -qw "$c" "$CG/cgroup.subtree_control" || { echo "  enabling +$c at the root"; echo "+$c" | $SUDO tee "$CG/cgroup.subtree_control" >/dev/null; }
done
echo "  root subtree_control: $(cat "$CG/cgroup.subtree_control")"

echo "▸ create --dry-run prints the plan and touches nothing"
$SUDO python3 code/main.py create "$BASE/cpu" --cpu 0.2 --dry-run | tee "$WORK/plan.txt"
grep -q "write '20000 100000' to" "$WORK/plan.txt" || fail "dry-run plan should write cpu.max"
[[ ! -d "$CG/$BASE" ]] || fail "dry run must not create the group"

echo "▸ cpu.max 20000 100000: a 2 s busy loop gets about 0.4 s of CPU and is throttled"
$SUDO python3 code/main.py create "$BASE/cpu" --cpu 0.2
[[ "$(cat "$CG/$BASE/cpu/cpu.max")" == "20000 100000" ]] || fail "cpu.max not written: $(cat "$CG/$BASE/cpu/cpu.max")"
grep -qw cpu "$CG/$BASE/cgroup.subtree_control" || fail "+cpu should have been enabled in $BASE/cgroup.subtree_control"
cat > "$WORK/busy.py" <<'PY'
import time
end = time.time() + 2
while time.time() < end:
    pass
PY
$SUDO python3 code/main.py run "$BASE/cpu" -- python3 "$WORK/busy.py"
python3 code/main.py stat "$BASE/cpu" | tee "$WORK/cpu.txt"
python3 code/main.py stat "$BASE/cpu" --json > "$WORK/cpu.json"
USAGE=$(j cpu_stat.usage_usec < "$WORK/cpu.json")
THR=$(j cpu_stat.throttled_usec < "$WORK/cpu.json")
NTHR=$(j cpu_stat.nr_throttled < "$WORK/cpu.json")
[[ "$USAGE" -ge 250000 && "$USAGE" -le 700000 ]] || fail "expected about 400000 usec of CPU for a 2 s loop at 0.2 CPU, got $USAGE"
[[ "$THR" -gt 500000 && "$NTHR" -ge 5 ]] || fail "expected throttling (throttled_usec $THR, nr_throttled $NTHR)"
grep -q "CPU: throttled in" "$WORK/cpu.txt" || fail "stat verdict should name throttling"
echo "  usage ${USAGE}us, throttled ${THR}us over ${NTHR} periods"

echo "▸ memory.max 30M: a 100M allocation is OOM-killed, exit 137, memory.events oom_kill 1"
$SUDO python3 code/main.py create "$BASE/mem" --memory 30M
[[ "$(cat "$CG/$BASE/mem/memory.max")" == "31457280" ]] || fail "memory.max not written"
if [[ -f "$CG/$BASE/mem/memory.swap.max" ]]; then
  [[ "$(cat "$CG/$BASE/mem/memory.swap.max")" == "0" ]] || fail "memory.swap.max should be 0"
else
  echo "  (no memory.swap.max: swap accounting is off on this kernel)"
fi
set +e
$SUDO python3 code/main.py run "$BASE/mem" -- python3 -c 'x = b"x" * (100 << 20); print(len(x))' 2> "$WORK/mem.err"
CODE=$?
set -e
cat "$WORK/mem.err"
[[ "$CODE" == "137" ]] || fail "expected exit 137 from the OOM killer, got $CODE"
grep -q "OOM-killed 1 process" "$WORK/mem.err" || fail "run should attribute the 137 to memory.events"
[[ "$(python3 code/main.py stat "$BASE/mem" --json | j memory_events.oom_kill)" -ge 1 ]] || fail "memory.events oom_kill should be at least 1"
python3 code/main.py stat "$BASE/mem" | grep -q "memory: 1 OOM kill" || fail "stat verdict should name the OOM kill"

echo "▸ pids.max 5: fork() fails with EAGAIN and pids.events counts it"
$SUDO python3 code/main.py create "$BASE/pids" --pids 5
[[ "$(cat "$CG/$BASE/pids/pids.max")" == "5" ]] || fail "pids.max not written"
cat > "$WORK/forks.py" <<'PY'
import errno
import os
import time
children = []
failed = 0
for _ in range(20):
    try:
        pid = os.fork()
    except OSError as exc:
        if exc.errno != errno.EAGAIN:
            raise
        failed += 1
        continue
    if pid == 0:
        time.sleep(3)
        os._exit(0)
    children.append(pid)
print(f"forked {len(children)} eagain {failed}", flush=True)
for pid in children:
    os.kill(pid, 9)
    os.waitpid(pid, 0)
PY
$SUDO python3 code/main.py run "$BASE/pids" -- python3 "$WORK/forks.py" | tee "$WORK/forks.txt"
grep -qE "^forked [1-4] eagain [1-9][0-9]*$" "$WORK/forks.txt" || fail "expected at most 4 forks and some EAGAIN under pids.max 5: $(cat "$WORK/forks.txt")"
[[ "$(python3 code/main.py stat "$BASE/pids" --json | j pids_events.max)" -ge 1 ]] || fail "pids.events max should count the failures"

echo "▸ main.sh: the same group with mkdir, echo, cat and awk"
$SUDO bash code/main.sh create "$BASE/sh" 0.5 209715200 50 | tee "$WORK/sh.txt"
[[ "$(cat "$CG/$BASE/sh/cpu.max")" == "50000 100000" ]] || fail "main.sh cpu.max"
[[ "$(cat "$CG/$BASE/sh/memory.max")" == "209715200" ]] || fail "main.sh memory.max"
[[ "$(cat "$CG/$BASE/sh/pids.max")" == "50" ]] || fail "main.sh pids.max"
$SUDO bash code/main.sh run "$BASE/sh" -- cat /proc/self/cgroup | tee "$WORK/sh-run.txt"
grep -q "$BASE/sh" "$WORK/sh-run.txt" || fail "the process should see itself in $BASE/sh"
bash code/main.sh stat "$BASE/sh" | tee "$WORK/sh-stat.txt"
grep -qE "cpu.max +50000 100000" "$WORK/sh-stat.txt" || fail "main.sh stat should print cpu.max"
bash code/main.sh explain $$ | head -3

echo "▸ explain, top and --watch read the live tree without root"
python3 code/main.py explain $$ | tee "$WORK/explain.txt"
grep -q "^pid $$ " "$WORK/explain.txt" || fail "explain header"
grep -q "effective memory_max:" "$WORK/explain.txt" || fail "explain should print the effective limits"
python3 code/main.py top --interval 1 --sort cpu --limit 5 | tee "$WORK/top.txt"
[[ "$(wc -l < "$WORK/top.txt")" -ge 2 ]] || fail "top should list at least one group"
python3 code/main.py stat "$BASE/cpu" --watch 0.5 --count 1 | tee "$WORK/watch.txt"
grep -qE "^[0-9.]+s: cpu [0-9]+%" "$WORK/watch.txt" || fail "watch should print a delta line"

if command -v docker >/dev/null; then
  echo "▸ Use It: docker --cpus 0.5 --memory 200m --pids-limit 50 writes the same files"
  OUT="$(docker run --rm --cpus 0.5 --memory 200m --pids-limit 50 alpine:3.22 sh -c 'cat /sys/fs/cgroup/cpu.max /sys/fs/cgroup/memory.max /sys/fs/cgroup/pids.max /proc/self/cgroup')"
  echo "$OUT"
  grep -q "^50000 100000$" <<<"$OUT" || fail "docker --cpus 0.5 should be cpu.max 50000 100000"
  grep -q "^209715200$" <<<"$OUT" || fail "docker --memory 200m should be memory.max 209715200"
  grep -q "^50$" <<<"$OUT" || fail "docker --pids-limit 50 should be pids.max 50"
  grep -q "^0::/$" <<<"$OUT" || echo "  (container sees $(grep '^0::' <<<"$OUT" || true); no private cgroup namespace here)"
  docker run -d --name cc-smoke-cg --cpus 0.5 --memory 200m alpine:3.22 sh -c 'while :; do :; done' >/dev/null
  sleep 1
  PID="$(docker inspect -f '{{.State.Pid}}' cc-smoke-cg)"
  GROUP="$(sed -n 's/^0:://p' "/proc/$PID/cgroup")"
  echo "  container pid $PID in cgroup $GROUP"
  python3 code/main.py explain "$PID" | tee "$WORK/docker-explain.txt"
  grep -q "effective cpu_max: 50000 100000 (0.5 CPU" "$WORK/docker-explain.txt" || fail "explain should find cpu.max 50000 100000 on the container's chain"
  grep -q "effective memory_max: 200.0M" "$WORK/docker-explain.txt" || fail "explain should find memory.max 200M on the container's chain"
  python3 code/main.py stat "$GROUP" --watch 1 --count 1 | tee "$WORK/docker-watch.txt"
  tail -n1 "$WORK/docker-watch.txt" | grep -qE "cpu (3[0-9]|4[0-9]|5[0-9]|6[0-9]|70)%" || fail "a busy loop under --cpus 0.5 should show about 50% cpu: $(tail -n1 "$WORK/docker-watch.txt")"
  tail -n1 "$WORK/docker-watch.txt" | grep -qE "throttled [1-9][0-9]* period" || fail "a busy loop under --cpus 0.5 should be throttled every period"
  docker stats --no-stream cc-smoke-cg
  docker rm -f cc-smoke-cg >/dev/null
else
  echo "  (docker not installed; docker part skipped)"
fi

STATE="$(systemctl is-system-running 2>/dev/null || true)"
if command -v systemd-run >/dev/null && [[ "$STATE" == "running" || "$STATE" == "degraded" ]]; then
  echo "▸ Use It: systemd-run -p CPUQuota=50% -p MemoryMax=200M -p TasksMax=50 is the same four writes"
  $SUDO systemd-run --unit "$UNIT" -p CPUQuota=50% -p MemoryMax=200M -p TasksMax=50 sleep 300 >/dev/null 2>&1
  sleep 1
  MAINPID="$(systemctl show -p MainPID --value "$UNIT")"
  echo "  unit cgroup $(systemctl show -p ControlGroup --value "$UNIT"), main pid $MAINPID"
  python3 code/main.py explain "$MAINPID" | tee "$WORK/unit.txt"
  grep -q "effective cpu_max: 50000 100000 (0.5 CPU" "$WORK/unit.txt" || fail "CPUQuota=50% should be cpu.max 50000 100000"
  grep -q "effective memory_max: 200.0M" "$WORK/unit.txt" || fail "MemoryMax=200M should be memory.max 209715200"
  grep -q "effective pids_max: 50 " "$WORK/unit.txt" || fail "TasksMax=50 should be pids.max 50"
  if command -v systemd-cgtop >/dev/null; then systemd-cgtop -n 1 -b 2>/dev/null | head -5 || true; fi
  $SUDO systemctl stop "$UNIT"
else
  echo "  (systemd not running here; systemd-run part skipped)"
fi

echo "▸ delete removes the whole subtree, deepest first"
$SUDO python3 code/main.py delete "$BASE" | tee "$WORK/delete.txt"
[[ ! -d "$CG/$BASE" ]] || fail "cgroup $BASE still exists after delete"
[[ "$(grep -c '^removed ' "$WORK/delete.txt")" == "5" ]] || fail "expected 5 groups removed (4 leaves + parent): $(cat "$WORK/delete.txt")"

echo "SMOKE OK: linux/cgroups-by-hand"
