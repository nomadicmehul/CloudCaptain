#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."

fail() { echo "SMOKE FAIL: $*" >&2; exit 1; }
[[ "$(uname -s)" == "Linux" ]] || fail "needs Linux (ptrace)"
WORK="$(mktemp -d)"
CONTAINERS=()
cleanup() {
  rm -rf "$WORK"
  for c in "${CONTAINERS[@]:-}"; do
    [[ -n "$c" ]] || continue
    docker rm -f "$c" >/dev/null 2>&1 || true
  done
}
trap cleanup EXIT
export LC_ALL=C

if ! command -v strace >/dev/null; then
  echo "▸ strace not installed; installing with apt-get"
  command -v apt-get >/dev/null || fail "strace not installed and no apt-get to install it"
  sudo apt-get update -qq >/dev/null 2>&1 || true
  sudo apt-get install -y -qq strace >/dev/null 2>&1 || fail "could not install strace"
fi
echo "▸ $(strace -V | head -1)"

echo "▸ Build It on a fixture: the verdict names the last path, the errno and the search order"
python3 code/main.py code/tests/fixtures/missing-config.txt --verdict | tee "$WORK/fixture.txt"
grep -q "after trying 3 locations" "$WORK/fixture.txt" || fail "fixture verdict should count 3 locations"

echo "▸ strace cat /nonexistent, then tracelens on the raw output"
set +e
strace -f -tt -T -y -s 200 -e trace=%file,%network,%process,write -o "$WORK/cat.txt" cat /nonexistent >/dev/null 2>&1
set -e
python3 code/main.py "$WORK/cat.txt" | tee "$WORK/cat.report"
grep -qE "verdict: cat /nonexistent \(pid [0-9]+\) exited with code 1 because openat\(\) of /nonexistent returned ENOENT" "$WORK/cat.report" || fail "verdict should name /nonexistent, ENOENT and exit 1"

echo "▸ a Python program tries three config locations: --run traces it and finds the search path"
cat > "$WORK/app.py" <<'PY'
import os
import sys

CANDIDATES = ["/etc/cc-smoke/app.yml", os.path.expanduser("~/.config/cc-smoke/app.yml"), os.path.join(os.getcwd(), "app.yml")]
for path in CANDIDATES:
    try:
        with open(path) as handle:
            print("loaded", path)
            sys.exit(0)
    except FileNotFoundError:
        continue
sys.exit(f"no config found in {len(CANDIDATES)} locations")
PY
python3 code/main.py --out "$WORK/app.trace" --run -- python3 "$WORK/app.py" | tee "$WORK/app.report"
grep -q "after trying 3 locations" "$WORK/app.report" || fail "verdict should count 3 locations"
grep -q "app.yml returned ENOENT" "$WORK/app.report" || fail "verdict should name app.yml and ENOENT"
grep -q "exited with code 1" "$WORK/app.report" || fail "verdict should report exit 1"
python3 code/main.py "$WORK/app.trace" --json > "$WORK/app.json"
python3 - "$WORK/app.json" <<'PY' || fail "json report shape"
import json, sys
report = json.load(open(sys.argv[1]))
assert report["verdict"]["errno"] == "ENOENT", report["verdict"]
assert len(report["verdict"]["attempts"]) == 3, report["verdict"]["attempts"]
assert report["exits"] and report["exits"][-1]["code"] == 1, report["exits"]
assert any(row["syscall"] == "openat" for row in report["histogram"])
print("  attempts:", report["verdict"]["attempts"])
print("  stderr:", report["stderr"][-1]["text"] if report["stderr"] else "(write not traced)")
PY

echo "▸ main.sh (strace, grep, awk, sed) agrees on the search path"
bash code/main.sh python3 "$WORK/app.py" | tee "$WORK/sh.txt"
grep -q "cc-smoke/app.yml" "$WORK/sh.txt" || fail "main.sh should list the ENOENT paths"
grep -qE "ENOENT +[0-9]+" "$WORK/sh.txt" || fail "main.sh should count ENOENT"

echo "▸ strace -C: the summary of the same run matches the tracelens histogram"
set +e
strace -f -C -tt -T -y -o "$WORK/both.txt" python3 "$WORK/app.py" >/dev/null 2>"$WORK/both.err"
set -e
SUMMARY="$WORK/both.txt"
grep -q "% time" "$SUMMARY" || SUMMARY="$WORK/both.err"
python3 code/main.py "$WORK/both.txt" --json > "$WORK/both.json"
python3 - "$SUMMARY" "$WORK/both.json" <<'PY' || fail "histogram does not match strace -c"
import json, sys
counts = {}
for line in open(sys.argv[1]):
    parts = line.split()
    if len(parts) >= 5 and parts[-1] not in ("syscall", "total") and parts[0].replace(".", "", 1).isdigit():
        counts[parts[-1]] = int(parts[3])
if not counts:
    print("  (no strace -c summary found; skipped)")
    sys.exit(0)
ours = {row["syscall"]: row["calls"] for row in json.load(open(sys.argv[2]))["histogram"]}
for name in ("openat", "execve"):
    assert counts.get(name) == ours.get(name), (name, counts.get(name), ours.get(name))
print("  openat", ours["openat"], "execve", ours["execve"], "match strace -c")
PY

if command -v docker >/dev/null; then
  echo "▸ Use It: tracing your own child works under Docker's default profile; strace -p 1 needs CAP_SYS_PTRACE"
  CONTAINERS+=(cc-smoke-strace cc-smoke-strace-cap)
  docker rm -f cc-smoke-strace cc-smoke-strace-cap >/dev/null 2>&1 || true
  docker run -d --name cc-smoke-strace python:3.12-alpine sleep 300 >/dev/null
  docker run -d --name cc-smoke-strace-cap --cap-add SYS_PTRACE python:3.12-alpine sleep 300 >/dev/null
  for c in cc-smoke-strace cc-smoke-strace-cap; do
    docker exec "$c" apk add --no-cache -q strace >/dev/null 2>&1 || fail "apk add strace in $c"
  done
  docker cp "$WORK/app.py" cc-smoke-strace:/app.py
  docker exec cc-smoke-strace sh -c 'strace -f -e trace=%file -o /trace.txt python3 /app.py >/dev/null 2>&1; cat /trace.txt' > "$WORK/in-container.txt" || fail "strace of a child inside the container failed"
  python3 code/main.py "$WORK/in-container.txt" --verdict | tee "$WORK/in-container.verdict"
  grep -q "after trying 3 locations" "$WORK/in-container.verdict" || fail "strace of a child inside the container should work without extra capabilities"

  attach() { docker exec "$1" sh -c 'strace -o /tmp/attach.txt -p 1 2>/tmp/attach.err & S=$!; sleep 1; kill -INT $S 2>/dev/null; wait $S 2>/dev/null; cat /tmp/attach.err /tmp/attach.txt 2>/dev/null'; }
  NOCAP="$(attach cc-smoke-strace || true)"
  WITHCAP="$(attach cc-smoke-strace-cap || true)"
  grep -qE "attached|detached|nanosleep|restart_syscall" <<<"$WITHCAP" || fail "strace -p 1 with CAP_SYS_PTRACE should attach (got: $WITHCAP)"
  echo "  with --cap-add SYS_PTRACE: $(head -1 <<<"$WITHCAP")"
  SCOPE="$(cat /proc/sys/kernel/yama/ptrace_scope 2>/dev/null || echo '?')"
  if grep -q "Operation not permitted" <<<"$NOCAP"; then
    echo "  without the capability: $(head -1 <<<"$NOCAP")"
    python3 code/main.py --verdict - <<<"$NOCAP"
  elif [[ "$SCOPE" == "1" ]]; then
    fail "with yama ptrace_scope=1 and no CAP_SYS_PTRACE, attaching to PID 1 should fail (got: $NOCAP)"
  else
    echo "  (kernel.yama.ptrace_scope=$SCOPE on this host: attach without the capability was allowed)"
  fi
else
  echo "  (docker not available; Use It container checks skipped)"
fi

echo "SMOKE OK: linux/syscalls-strace"
