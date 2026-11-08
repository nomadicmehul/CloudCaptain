#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."

fail() { echo "SMOKE FAIL: $*" >&2; exit 1; }
[[ "$(uname -s)" == "Linux" ]] || fail "needs Linux (strace, Docker, eBPF)"
WORK="$(mktemp -d)"
FALCO_IMAGE="falcosecurity/falco:0.45.2"
TARGET_IMAGE="alpine:3.22"
CONTAINERS=(cc-rd-falco cc-rd-target cc-rd-attack)
cleanup() {
  for c in "${CONTAINERS[@]}"; do docker rm -f "$c" >/dev/null 2>&1 || true; done
  rm -rf "$WORK"
}
trap cleanup EXIT
export LC_ALL=C

jsonq() { python3 -c "import json, sys; d = json.load(open(sys.argv[1])); print($2)" "$1"; }

echo "▸ Build It: minifalco on the attack and benign JSON fixtures"
python3 code/main.py run code/fixtures/events-attack.json --json > "$WORK/attack.json" || true
python3 - "$WORK/attack.json" <<'PY' || fail "attack fixture did not fire all six rules"
import json, sys
data = json.load(open(sys.argv[1]))
names = sorted({a["rule"] for a in data["alerts"]})
want = sorted({"Shell spawned in a container", "Read sensitive file untrusted", "Write below etc",
               "Execution from world-writable directory", "Unexpected outbound connection",
               "Container runtime socket accessed"})
assert names == want, (names, want)
print("  attack fixture:", len(data["alerts"]), "alerts,", len(names), "rules")
PY
python3 code/main.py run code/fixtures/events-benign.json --json > "$WORK/benign.json"
[[ "$(jsonq "$WORK/benign.json" 'len(d["alerts"])')" == "0" ]] || fail "benign fixture should raise nothing: $(cat "$WORK/benign.json")"
echo "  benign fixture: 0 alerts"

echo "▸ Build It: --min-priority floor and bad input exit codes"
python3 code/main.py run code/fixtures/events-attack.json --min-priority ERROR --json > "$WORK/severe.json" || true
SEVERE="$(jsonq "$WORK/severe.json" 'sorted(a["rule"] for a in d["alerts"])')"
[[ "$SEVERE" == "['Container runtime socket accessed', 'Write below etc']" ]] || fail "min-priority ERROR should keep only the two severe rules: $SEVERE"
CODE=0; python3 code/main.py run "$WORK/nope.json" >/dev/null 2>"$WORK/err.txt" || CODE=$?
{ [[ "$CODE" == "2" ]] && grep -q "not found" "$WORK/err.txt"; } || fail "missing events file should exit 2: code $CODE"

echo "▸ Build It: the committed strace capture reproduces the same six rule names"
python3 code/main.py run code/fixtures/capture-attack.strace --container-id a1b2c3d4e5f6 --user-name root --json > "$WORK/capture.json" || true
python3 - "$WORK/capture.json" <<'PY' || fail "committed strace capture did not match the fixture rules"
import json, sys
names = sorted({a["rule"] for a in json.load(open(sys.argv[1]))["alerts"]})
assert len(names) == 6, names
print("  strace capture:", len(names), "rules")
PY

if ! command -v docker >/dev/null; then
  echo "  (docker not available; Use It and live capture skipped)"
  echo "SMOKE OK: security/runtime-detection (minifalco only)"
  exit 0
fi
docker rm -f "${CONTAINERS[@]}" >/dev/null 2>&1 || true
docker pull -q "$TARGET_IMAGE" >/dev/null || fail "cannot pull $TARGET_IMAGE"

echo "▸ Use It (live strace): run the scripted attack under strace inside a container, feed the capture to minifalco"
ATTACK='
cat /etc/shadow >/dev/null 2>&1 || true
touch /etc/cc-rd-backdoor 2>/dev/null || true
cp /bin/busybox /tmp/evil 2>/dev/null && /tmp/evil true 2>/dev/null || true
nc -w 1 203.0.113.10 4444 </dev/null >/dev/null 2>&1 || true
curl -s --max-time 2 --unix-socket /var/run/docker.sock http://localhost/_ping >/dev/null 2>&1 || true
'
docker run --name cc-rd-attack --rm \
  --cap-add SYS_PTRACE \
  -v /var/run/docker.sock:/var/run/docker.sock \
  "$TARGET_IMAGE" sh -c "apk add -q --no-cache strace curl >/dev/null 2>&1 && strace -f -tt -yy -s 200 -o /trace.log sh -c '$ATTACK'; cat /trace.log" > "$WORK/live.strace" || fail "could not capture the attack with strace"
[[ -s "$WORK/live.strace" ]] || fail "empty strace capture"
python3 code/main.py run "$WORK/live.strace" --container-id cc-rd-live --user-name root --json > "$WORK/live.json" || true
python3 - "$WORK/live.json" <<'PY' || fail "live strace capture did not raise the expected rules: $(cat "$WORK/live.json" 2>/dev/null)"
import json, sys
names = {a["rule"] for a in json.load(open(sys.argv[1]))["alerts"]}
required = {"Shell spawned in a container", "Read sensitive file untrusted",
            "Write below etc", "Execution from world-writable directory"}
missing = sorted(required - names)
assert not missing, "missing from live capture: %s (got %s)" % (missing, sorted(names))
best_effort = {"Unexpected outbound connection", "Container runtime socket accessed"}
print("  live capture raised:", sorted(names))
for rule in sorted(best_effort - names):
    print("  (did not observe %r in this environment; the nc/curl connect may not have been traced)" % rule)
PY

echo "▸ Use It: Falco $FALCO_IMAGE, modern eBPF, default rules"
docker pull -q "$FALCO_IMAGE" >/dev/null || fail "cannot pull $FALCO_IMAGE"
docker run --rm "$FALCO_IMAGE" falco --version > "$WORK/falco-version.txt" 2>&1 || fail "falco --version failed"
grep -i "Falco version" "$WORK/falco-version.txt" | head -1 | sed 's/^/  /'
docker run -d --name cc-rd-falco --privileged \
  -v /var/run/docker.sock:/host/var/run/docker.sock \
  -v /proc:/host/proc:ro \
  -v /etc:/host/etc:ro \
  "$FALCO_IMAGE" falco -o json_output=true -o stdout_output.enabled=true -o log_level=info >/dev/null \
  || fail "could not start Falco"

READY=""
FALCO_BROKEN=""
for _ in $(seq 1 60); do
  docker logs cc-rd-falco > "$WORK/falco.log" 2>&1 || true
  if grep -qiE "Opening .?syscall.? source|Events detected|Starting .*webserver" "$WORK/falco.log"; then READY=1; break; fi
  if grep -qiE "runtime error|unable to load|failed to open|cannot open|modern bpf probe.*(error|not supported)|BTF" "$WORK/falco.log"; then FALCO_BROKEN=1; break; fi
  if [[ "$(docker inspect -f '{{.State.Running}}' cc-rd-falco 2>/dev/null || echo false)" != "true" ]]; then FALCO_BROKEN=1; break; fi
  sleep 1
done

if [[ -z "$READY" ]]; then
  echo "  (this kernel cannot run Falco's modern eBPF driver; the Falco half is skipped)"
  echo "  last Falco log lines:"; tail -5 "$WORK/falco.log" | sed 's/^/    /'
  echo "SMOKE OK: security/runtime-detection (Falco driver unavailable; minifalco + live strace verified)"
  exit 0
fi
echo "  Falco is watching syscalls"
sleep 3

# Docker mounts /dev/shm noexec, so the exec below would fail with EACCES and Falco
# (which only sees successful execs) would stay quiet. Mount it exec, as on a host
# or a runtime that does not harden /dev/shm.
docker run -d --name cc-rd-target --tmpfs /dev/shm:rw,exec,nosuid,nodev,size=64m "$TARGET_IMAGE" sleep 600 >/dev/null || fail "could not start the target container"
sleep 2
docker exec cc-rd-target cat /etc/shadow >/dev/null 2>&1 || true
docker exec cc-rd-target sh -c 'cp /bin/busybox /dev/shm/busybox 2>/dev/null && /dev/shm/busybox true 2>/dev/null' || true
docker exec -t cc-rd-target sh -c 'id' >/dev/null 2>&1 || true
docker exec cc-rd-target cat /etc/hostname >/dev/null 2>&1 || true
sleep 4
docker logs cc-rd-falco > "$WORK/falco.log" 2>&1 || true

grep -q "Read sensitive file untrusted" "$WORK/falco.log" || fail "Falco should raise 'Read sensitive file untrusted' for cat /etc/shadow; log tail: $(tail -6 "$WORK/falco.log")"
echo "  Falco raised: Read sensitive file untrusted  -> minifalco: Read sensitive file untrusted"
grep -q "Execution from /dev/shm" "$WORK/falco.log" || fail "Falco should raise 'Execution from /dev/shm'; log tail: $(tail -6 "$WORK/falco.log")"
echo "  Falco raised: Execution from /dev/shm         -> minifalco: Execution from world-writable directory"
if grep -q "Terminal shell in container" "$WORK/falco.log"; then
  echo "  Falco raised: Terminal shell in container     -> minifalco: Shell spawned in a container"
else
  echo "  (Falco did not raise 'Terminal shell in container'; a tty-attached exec is needed and CI may lack one)"
fi

grep "Sensitive file opened" "$WORK/falco.log" > "$WORK/sensitive.txt" 2>/dev/null || true
if grep -q "/etc/hostname" "$WORK/sensitive.txt"; then fail "a benign cat /etc/hostname was wrongly flagged as a sensitive file"; fi
echo "  benign cat /etc/hostname raised no sensitive-file alert ($(wc -l < "$WORK/sensitive.txt" | tr -d ' ') sensitive-file alerts, all /etc/shadow)"

echo "SMOKE OK: security/runtime-detection"
