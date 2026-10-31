#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."

fail() { echo "SMOKE FAIL: $*" >&2; exit 1; }
[[ "$(uname -s)" == "Linux" ]] || fail "needs Linux (/proc)"
WORK="$(mktemp -d)"
PIDS=()
cleanup() { kill "${PIDS[@]}" 2>/dev/null || true; rm -rf "$WORK"; }
trap cleanup EXIT
wait_ready() { for _ in $(seq 1 100); do grep -q ready "$1" 2>/dev/null && return 0; sleep 0.1; done; fail "helper process did not start: $1"; }

echo "▸ a process holding 120 files: count, kinds, limit"
python3 -c 'import sys,time; fs=[open(f"{sys.argv[1]}/leak{i}.tmp","w") for i in range(120)]; print("ready",flush=True); time.sleep(300)' "$WORK" > "$WORK/ready1" &
HOLDER=$!; PIDS+=("$HOLDER")
wait_ready "$WORK/ready1"
python3 code/main.py "$HOLDER" | tee "$WORK/report.txt"
python3 code/main.py "$HOLDER" --json > "$WORK/report.json"
python3 - "$WORK/report.json" <<'PY' || fail "report shape"
import json, resource, sys
d = json.load(open(sys.argv[1]))
soft = resource.getrlimit(resource.RLIMIT_NOFILE)[0]
assert d["kinds"]["file"] >= 120, d["kinds"]
assert d["count"] >= 123, d["count"]
assert d["soft_limit"] == soft, (d["soft_limit"], soft)
print("  count", d["count"], "soft limit", d["soft_limit"], "kinds", d["kinds"])
PY

echo "▸ main.sh (ls, readlink, awk) agrees"
bash code/main.sh "$HOLDER" | tee "$WORK/sh.txt"
grep -qE "file +12[0-9]" "$WORK/sh.txt" || fail "main.sh by-kind table"

echo "▸ a leak of ~20 descriptors/s is caught by --watch with a time-to-limit forecast"
cat > "$WORK/leaky.py" <<'PY'
import sys, time
fs = []
while True:
    fs.append(open(f"{sys.argv[1]}/g{len(fs)}.tmp", "w"))
    time.sleep(0.05)
PY
python3 "$WORK/leaky.py" "$WORK" &
LEAK=$!; PIDS+=("$LEAK")
sleep 0.5
python3 code/main.py "$LEAK" --watch 1 --count 2 --json > "$WORK/watch.jsonl"
python3 - "$WORK/watch.jsonl" <<'PY' || fail "watch output"
import json, sys
lines = [json.loads(l) for l in open(sys.argv[1]) if l.strip()]
assert len(lines) == 3, len(lines)
deltas = lines[1:]
assert all(d["verdict"] == "growing" for d in deltas), deltas
assert deltas[-1]["rate_per_minute"] > 300, deltas[-1]
assert deltas[-1]["seconds_to_limit"] is not None, deltas[-1]
print("  rate/min", round(deltas[-1]["rate_per_minute"]), " seconds to soft limit", round(deltas[-1]["seconds_to_limit"]))
PY

echo "▸ a deleted file held open is found by --deleted and freed by truncating through /proc"
python3 -c 'import sys,time; f=open(sys.argv[1],"w"); f.write("x"*1048576); f.flush(); print("ready",flush=True); time.sleep(300)' "$WORK/big.log" > "$WORK/ready2" &
HOLD2=$!; PIDS+=("$HOLD2")
wait_ready "$WORK/ready2"
rm "$WORK/big.log"
python3 code/main.py --deleted | tee "$WORK/deleted.txt"
grep -q "big.log" "$WORK/deleted.txt" || fail "--deleted did not list big.log"
FD=$(python3 code/main.py "$HOLD2" --json | python3 -c 'import json,sys; print(json.load(sys.stdin)["deleted"][0]["fd"])')
BEFORE=$(python3 code/main.py "$HOLD2" --json | python3 -c 'import json,sys; print(json.load(sys.stdin)["deleted"][0]["size"])')
: > "/proc/$HOLD2/fd/$FD"
AFTER=$(python3 code/main.py "$HOLD2" --json | python3 -c 'import json,sys; print(json.load(sys.stdin)["deleted"][0]["size"])')
[[ "$BEFORE" == "1048576" && "$AFTER" == "0" ]] || fail "truncating through /proc/PID/fd should free the blocks ($BEFORE -> $AFTER)"
echo "  fd $FD: $BEFORE -> $AFTER bytes, process still alive: $(kill -0 "$HOLD2" && echo yes)"

if command -v prlimit >/dev/null; then
  echo "▸ prlimit raises the soft limit of a running process without a restart"
  SOFT=$(python3 code/main.py "$HOLDER" --json | python3 -c 'import json,sys; print(json.load(sys.stdin)["soft_limit"])')
  HARD=$(python3 code/main.py "$HOLDER" --json | python3 -c 'import json,sys; print(json.load(sys.stdin)["hard_limit"])')
  if [[ "$HARD" =~ ^[0-9]+$ && "$SOFT" -lt "$HARD" ]]; then
    prlimit --pid "$HOLDER" --nofile="$HARD:$HARD"
    NEW=$(python3 code/main.py "$HOLDER" --json | python3 -c 'import json,sys; print(json.load(sys.stdin)["soft_limit"])')
    [[ "$NEW" == "$HARD" ]] || fail "prlimit change not visible in /proc (got $NEW)"
    echo "  soft limit $SOFT -> $NEW live"
  else
    echo "  (soft already equals hard: $SOFT/$HARD; nothing to raise)"
  fi
fi

if command -v docker >/dev/null; then
  echo "▸ Use It: the limit is per process and set by whoever starts it"
  LIM=$(docker run --rm --ulimit nofile=4096:4096 alpine:3.22 sh -c 'ulimit -n')
  [[ "$LIM" == "4096" ]] || fail "--ulimit nofile should show as ulimit -n (got $LIM)"
  set +e
  ERR=$(docker run --rm --ulimit nofile=16:16 python:3.12-alpine python3 -c 'fs=[open(f"/tmp/{i}","w") for i in range(100)]' 2>&1)
  CODE=$?
  set -e
  [[ "$CODE" != "0" ]] || fail "opening 100 files under a 16-descriptor limit should fail"
  grep -q "Too many open files" <<<"$ERR" || fail "expected EMFILE, got: $ERR"
  echo "  EMFILE reproduced under --ulimit nofile=16:16; default in a container here: $(docker run --rm alpine:3.22 sh -c 'ulimit -n')"
else
  echo "  (docker not available; Use It skipped)"
fi

echo "SMOKE OK: linux/file-descriptors"
