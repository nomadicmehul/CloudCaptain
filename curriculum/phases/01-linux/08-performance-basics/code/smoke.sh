#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."

fail() { echo "SMOKE FAIL: $*" >&2; exit 1; }
[[ "$(uname -s)" == "Linux" ]] || fail "needs Linux (/proc)"
WORK="$(mktemp -d)"
PIDS=()
CONTAINER="cc-smoke-usetop"
cleanup() {
  if [[ "${#PIDS[@]}" -gt 0 ]]; then
    kill "${PIDS[@]}" 2>/dev/null || true
    wait "${PIDS[@]}" 2>/dev/null || true
  fi
  if command -v docker >/dev/null; then docker rm -f "$CONTAINER" >/dev/null 2>&1 || true; fi
  rm -rf "$WORK"
}
trap cleanup EXIT
stop_helpers() {
  if [[ "${#PIDS[@]}" -gt 0 ]]; then
    kill "${PIDS[@]}" 2>/dev/null || true
    wait "${PIDS[@]}" 2>/dev/null || true
  fi
  PIDS=()
}
available_kb() { python3 code/main.py --interval 1 --count 1 --json | python3 -c 'import json,sys; print(json.load(sys.stdin)["memory"]["available_kb"])'; }

echo "▸ replaying recorded /proc snapshots reproduces the verdicts (this also runs on macOS)"
python3 code/main.py --replay code/fixtures/saturated > "$WORK/replay.txt"
grep -q "VERDICT: CPU saturated: run queue 14 on 4 cpus, PSI cpu some 38%" "$WORK/replay.txt" || fail "saturated replay verdict: $(tail -1 "$WORK/replay.txt")"
python3 code/main.py --replay code/fixtures/idle | grep -q "VERDICT: No saturation" || fail "idle replay verdict"
tail -1 "$WORK/replay.txt"

echo "▸ a live 1-second sample of this host"
python3 code/main.py --interval 1 --count 1 | tee "$WORK/base.txt"
python3 code/main.py --interval 1 --count 1 --json > "$WORK/base.json"
python3 - "$WORK/base.json" <<'PY' || fail "live report shape"
import json, os, sys
d = json.load(open(sys.argv[1]))
assert d["ncpu"] == os.cpu_count(), (d["ncpu"], os.cpu_count())
assert 0 <= d["cpu"]["busy"] <= 100, d["cpu"]
assert d["memory"]["total_kb"] > 0 and d["memory"]["available_kb"] > 0, d["memory"]
assert [r["resource"] for r in d["rows"][:2]] == ["CPU", "Memory"], d["rows"]
assert any(r["resource"].startswith("Disk ") for r in d["rows"]), d["rows"]
print("  cpus", d["ncpu"], "busy", d["cpu"]["busy"], "%, available", d["memory"]["available_pct"], "%, verdict:", d["verdicts"][0])
PY
BASE_BUSY=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["cpu"]["busy"])' "$WORK/base.json")

echo "▸ two busy loops for 3 seconds: utilization rises and the top list names python"
python3 -c 'while True: pass' &
PIDS+=("$!")
python3 -c 'while True: pass' &
PIDS+=("$!")
sleep 0.5
python3 code/main.py --interval 2 --count 1 --json > "$WORK/busy.json"
python3 - "$WORK/busy.json" "$BASE_BUSY" <<'PY' || fail "busy loops not visible in the report"
import json, sys
d = json.load(open(sys.argv[1]))
base = float(sys.argv[2])
expected = min(90.0, 2 / d["ncpu"] * 100 * 0.6)
assert d["cpu"]["busy"] >= expected, (d["cpu"], expected)
assert d["cpu"]["busy"] > base, (d["cpu"]["busy"], base)
loops = [p for p in d["top"][:3] if "python" in p["comm"] and p["cpu_pct"] > 60]
assert len(loops) >= 2, d["top"]
print("  busy", base, "->", d["cpu"]["busy"], "%  top:", [(p["comm"], p["cpu_pct"]) for p in d["top"][:3]])
print("  verdict:", d["verdicts"][0])
PY
stop_helpers

echo "▸ allocating 512 MB drops MemAvailable by about that much"
AVAIL_BEFORE=$(available_kb)
python3 -c 'import time; block = b"\x01" * (512 << 20); print("ready", flush=True); time.sleep(300)' > "$WORK/ready-mem" &
PIDS+=("$!")
for _ in $(seq 1 100); do grep -q ready "$WORK/ready-mem" 2>/dev/null && break; sleep 0.1; done
grep -q ready "$WORK/ready-mem" || fail "memory holder did not start"
AVAIL_AFTER=$(available_kb)
DROP_MB=$(( (AVAIL_BEFORE - AVAIL_AFTER) / 1024 ))
echo "  MemAvailable $((AVAIL_BEFORE / 1024)) MB -> $((AVAIL_AFTER / 1024)) MB (-${DROP_MB} MB)"
[[ "$DROP_MB" -ge 300 ]] || fail "MemAvailable dropped only ${DROP_MB} MB after a 512 MB allocation"
stop_helpers

echo "▸ pressure stall information"
if [[ -r /proc/pressure/cpu ]]; then
  head -n1 /proc/pressure/cpu /proc/pressure/memory /proc/pressure/io
  python3 - "$WORK/busy.json" <<'PY' || fail "PSI missing from the report although /proc/pressure exists"
import json, sys
d = json.load(open(sys.argv[1]))
assert set(d["pressure"]) >= {"cpu", "memory", "io"}, d["pressure"]
for name, view in d["pressure"].items():
    assert view["some_avg10"] >= 0 and view["some_window_pct"] >= 0, (name, view)
print("  PSI in the busy report:", {k: (v["some_avg10"], v["some_window_pct"]) for k, v in d["pressure"].items()})
PY
else
  echo "  (no /proc/pressure on this kernel; PSI skipped)"
fi

echo "▸ main.sh: the same numbers with cat, awk, ps and vmstat"
bash code/main.sh 1 | tee "$WORK/sh.txt"
grep -qE "busy [0-9]+\.[0-9]%" "$WORK/sh.txt" || fail "main.sh did not print the CPU line"
grep -qE "available [0-9]+%" "$WORK/sh.txt" || fail "main.sh did not print MemAvailable"

if command -v vmstat >/dev/null; then
  echo "▸ vmstat and usetop over the same two seconds agree on idle within 30 points"
  vmstat 1 3 > "$WORK/vmstat.txt" &
  VM=$!
  PIDS+=("$VM")
  python3 code/main.py --interval 2 --count 1 --json > "$WORK/same.json"
  wait "$VM" || true
  PIDS=()
  VM_IDLE=$(awk 'NR > 2 {idle = $15} END {print idle}' "$WORK/vmstat.txt")
  OUR_IDLE=$(python3 -c 'import json,sys; print(round(json.load(open(sys.argv[1]))["cpu"]["idle"]))' "$WORK/same.json")
  echo "  vmstat id=$VM_IDLE  usetop idle=$OUR_IDLE"
  DIFF=$(( VM_IDLE > OUR_IDLE ? VM_IDLE - OUR_IDLE : OUR_IDLE - VM_IDLE ))
  [[ "$DIFF" -le 30 ]] || fail "vmstat and usetop disagree on idle by $DIFF points"
else
  echo "  (vmstat not installed; comparison skipped)"
fi

if command -v mpstat >/dev/null; then
  echo "▸ mpstat -P ALL 1 1: per-cpu view (usetop keeps the same split in per_cpu)"
  mpstat -P ALL 1 1 | sed -n '1,8p'
else
  echo "  (mpstat not installed; apt-get install sysstat; skipped)"
fi

if command -v docker >/dev/null; then
  echo "▸ Use It: docker stats is cgroup data; usetop sees the same process on the host at the --cpus cap"
  docker rm -f "$CONTAINER" >/dev/null 2>&1 || true
  docker run -d --name "$CONTAINER" --cpus 0.5 python:3.12-alpine python3 -c 'while True: pass' >/dev/null
  sleep 2
  python3 code/main.py --interval 2 --count 1 --json > "$WORK/docker.json"
  STATS=$(docker stats --no-stream --format '{{.CPUPerc}}' "$CONTAINER")
  python3 - "$WORK/docker.json" "$STATS" <<'PY' || fail "capped container not seen near 50% on the host"
import json, sys
d = json.load(open(sys.argv[1]))
capped = [p for p in d["top"] if "python" in p["comm"] and 20 <= p["cpu_pct"] <= 80]
assert capped, d["top"]
print("  docker stats CPU%:", sys.argv[2].strip(), " usetop host view:", [(p["pid"], p["comm"], p["cpu_pct"]) for p in capped])
PY
  docker rm -f "$CONTAINER" >/dev/null
else
  echo "  (docker not available; Use It skipped)"
fi

echo "SMOKE OK: linux/performance-basics"
