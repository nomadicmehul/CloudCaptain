#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."

fail() { echo "SMOKE FAIL: $*" >&2; exit 1; }
WORK="$(mktemp -d)"
NS="cc-job-scheduling"
CTX="${CC_SMOKE_CONTEXT:-kind-cc-smoke}"
PGROUPS=()
PROCS=()
TIMER=""
NS_CREATED=""
CRONITER_VERSION="6.2.4"
cleanup() {
  for g in ${PGROUPS[@]+"${PGROUPS[@]}"}; do kill -TERM -- "-$g" 2>/dev/null || true; done
  for p in ${PROCS[@]+"${PROCS[@]}"}; do kill -TERM "$p" 2>/dev/null || true; done
  if [[ -n "$TIMER" ]]; then
    sudo -n systemctl stop "$TIMER.timer" "$TIMER.service" >/dev/null 2>&1 || true
    sudo -n systemctl reset-failed "$TIMER.timer" "$TIMER.service" >/dev/null 2>&1 || true
  fi
  if [[ -n "$NS_CREATED" ]]; then kubectl --context "$CTX" delete namespace "$NS" --ignore-not-found --wait=false >/dev/null 2>&1 || true; fi
  rm -rf "$WORK"
}
trap cleanup EXIT

mc() { python3 code/main.py "$@"; }
# The probe holds the lock for a moment, so it must only race with holders that wait
# for the lock (flock without -n) or that already have it.
lock_free() { flock -n "$1" true 2>/dev/null; }
wait_until() {
  local deadline=$(( $(date +%s) + $1 )); shift
  until "$@"; do
    [[ "$(date +%s)" -lt "$deadline" ]] || return 1
    sleep 0.2
  done
}
not() { ! "$@"; }

echo "▸ minicron: the day rule depends on the dialect"
mc explain '0 0 */2 * 1' > "$WORK/explain.txt" || fail "explain: $(cat "$WORK/explain.txt")"
cat "$WORK/explain.txt"
grep -q "AND" "$WORK/explain.txt" && grep -q "different days" "$WORK/explain.txt" || fail "explain should warn that the dialects disagree: $(cat "$WORK/explain.txt")"

echo "▸ minicron: Berlin's spring gap and autumn fold under the cronie rule"
mc next '30 2 * * *' --tz Europe/Berlin --from 2026-03-27T12:00Z -n 3 --json > "$WORK/gap.json" || fail "next (gap): $(cat "$WORK/gap.json")"
mc next '30 2 * * *' --tz Europe/Berlin --from 2026-10-24T12:00Z -n 2 --json > "$WORK/fold.json" || fail "next (fold): $(cat "$WORK/fold.json")"
python3 - "$WORK/gap.json" "$WORK/fold.json" <<'PY' || fail "DST notes: $(cat "$WORK/gap.json" "$WORK/fold.json")"
import json, sys
gap, fold = (json.load(open(p)) for p in sys.argv[1:])
assert gap["runs"][1]["local"] == "2026-03-29T03:00:00+02:00" and gap["runs"][1]["note"].startswith("gap"), gap["runs"][1]
assert [r["utc"] for r in fold["runs"]] == ["2026-10-25T00:30:00Z", "2026-10-26T01:30:00Z"], fold["runs"]
print("  29 March: 02:30 does not exist -> 03:00 CEST;  25 October: 02:30 runs once (first occurrence)")
PY

echo "▸ minicron: simulation of the fixture jobs across three hours of scheduler downtime"
mc simulate --jobs code/fixtures/jobs.json --from 2026-03-27T23:00Z --to 2026-03-28T04:00Z --down 2026-03-28T00:10Z/2026-03-28T03:05Z --json > "$WORK/sim.json" || fail "simulate: $(cat "$WORK/sim.json")"
python3 - "$WORK/sim.json" <<'PY' || fail "simulation summary: $(cat "$WORK/sim.json")"
import json, sys
data = json.load(open(sys.argv[1]))
summary = data["summary"]
catch_up = [(e["job"], e["scheduled"][11:16]) for e in data["journal"] if e["reason"] == "catch-up"]
assert ("backup", "01:30") in catch_up and [j for j, _ in catch_up].count("backup") == 1, catch_up
assert [s for j, s in catch_up if j == "rollup"] == ["01:00", "02:00", "03:00"], catch_up
assert summary["sync"]["skip"] > 0 and summary["refresh"]["kill"] > 0, summary
for name, counts in sorted(summary.items()):
    print(f"  {name:<8} " + ", ".join(f"{k} {v}" for k, v in counts.items()))
PY

echo "▸ minicron: bad input exits 2 with the reason on stderr"
CODE=0
mc next '61 * * * *' > "$WORK/bad.out" 2> "$WORK/bad.err" || CODE=$?
[[ "$CODE" == "2" && ! -s "$WORK/bad.out" ]] && grep -q "minute: 61 is outside 0-59" "$WORK/bad.err" || fail "bad expression: code $CODE, $(cat "$WORK/bad.out" "$WORK/bad.err")"

if command -v systemd-analyze >/dev/null 2>&1; then
  systemd-analyze calendar --help > "$WORK/sa-help.txt" 2>&1 || true
  if grep -q -- "--base-time" "$WORK/sa-help.txt"; then
    echo "▸ Use It: OnCalendar next elapses and normalized forms against systemd-analyze calendar --iterations=10"
    python3 - <<'PY' || fail "minicron and systemd-analyze disagree (see above)"
import json, os, re, subprocess, sys
cases = [
    ("UTC", "Mon..Fri 09:00", "2026-11-11 08:00:00 UTC"),
    ("UTC", "daily", "2026-11-11 08:00:00 UTC"),
    ("UTC", "weekly", "2026-01-01 00:00:00 UTC"),
    ("UTC", "monthly", "2026-01-01 00:00:00 UTC"),
    ("UTC", "quarterly", "2026-01-01 00:00:00 UTC"),
    ("UTC", "semiannually", "2026-01-01 00:00:00 UTC"),
    ("UTC", "*:0/15", "2026-11-11 08:00:00 UTC"),
    ("UTC", "Sat,Sun *-*-* 10:00/30", "2026-11-11 08:00:00 UTC"),
    ("UTC", "*-*~01", "2026-01-01 00:00:00 UTC"),
    ("UTC", "*-02~03", "2026-01-01 00:00:00 UTC"),
    ("UTC", "Fri *-*-13 00:00", "2026-01-01 00:00:00 UTC"),
    ("UTC", "Mon *-*-1..7", "2026-01-01 00:00:00 UTC"),
    ("UTC", "*-*-* 0..23/6:00", "2026-01-01 00:00:00 UTC"),
    ("UTC", "*-*-* *:*:0/20", "2026-01-01 00:00:00 UTC"),
    ("UTC", "*-2-29 0:0", "2026-01-01 00:00:00 UTC"),
    ("UTC", "Mon,Tue,Wed 1:2", "2026-01-01 00:00:00 UTC"),
    ("UTC", "Sun,Mon", "2026-01-01 00:00:00 UTC"),
    ("UTC", "2026..2027-*-* 12:00", "2026-12-25 00:00:00 UTC"),
    ("UTC", "*-*-* 02:30 Europe/Berlin", "2026-03-27 12:00:00 UTC"),
    ("UTC", "Mon..Fri 9..17:0/30 Europe/Berlin", "2026-10-23 12:00:00 UTC"),
    ("Europe/Berlin", "*-*-* 02:30", "2026-03-27 12:00:00 UTC"),
    ("Europe/Berlin", "hourly", "2026-03-28 23:30:00 UTC"),
    ("Europe/Berlin", "*-*-* 02:30", "2026-10-24 12:00:00 UTC"),
    ("Europe/Berlin", "*:0/30", "2026-10-24 23:20:00 UTC"),
    ("America/New_York", "*-*-* 01:30", "2026-10-31 12:00:00 UTC"),
    ("America/New_York", "*-*-* 02:30", "2026-03-07 12:00:00 UTC"),
    ("America/New_York", "hourly", "2026-11-01 04:30:00 UTC"),
    ("America/New_York", "hourly", "2026-03-08 05:30:00 UTC"),
]
line = re.compile(r"^\s*(?:Next elapse|Iteration #\d+|\(in UTC\)):\s+\w+ (\d{4}-\d\d-\d\d \d\d:\d\d:\d\d) (\S+)\s*$")
problems = 0
for zone, spec, base in cases:
    env = dict(os.environ, TZ=zone)
    real = subprocess.run(["systemd-analyze", "calendar", "--iterations=10", f"--base-time={base}", spec], env=env, capture_output=True, text=True)
    if real.returncode != 0:
        print(f"  systemd-analyze rejected {spec!r}: {real.stderr.strip()}")
        problems += 1
        continue
    theirs = [m.group(1) for m in (line.match(l) for l in real.stdout.splitlines()) if m and m.group(2) == "UTC"]
    normalized = re.search(r"Normalized form: (.*)", real.stdout).group(1).strip()
    iso = base.replace(" UTC", "Z").replace(" ", "T")
    mine_run = subprocess.run([sys.executable, "code/main.py", "calendar", spec, "--tz", zone, "--base-time", iso, "--iterations", "10", "--json"], capture_output=True, text=True)
    if mine_run.returncode != 0:
        print(f"  minicron failed on {spec!r}: {mine_run.stderr.strip()}")
        problems += 1
        continue
    mine = json.loads(mine_run.stdout)
    mine_times = [r["utc"].replace("T", " ").rstrip("Z") for r in mine["runs"]]
    ok = mine_times == theirs and mine["normalized"] == normalized
    problems += 0 if ok else 1
    label = "ok " if ok else "DIFF"
    print(f"  {label} TZ={zone:<16} {spec!r:<38} {normalized}")
    if not ok:
        print(f"       systemd:  {normalized!r} {theirs}\n       minicron: {mine['normalized']!r} {mine_times}")
print(f"  {len(cases) - problems}/{len(cases)} specs agree on 10 elapses and the normalized form")
raise SystemExit(1 if problems else 0)
PY
  else
    echo "  (systemd-analyze calendar has no --base-time here; OnCalendar comparison skipped)"
  fi
else
  echo "  (systemd-analyze not installed; OnCalendar comparison skipped)"
fi

echo "▸ Use It: cron next runs against croniter $CRONITER_VERSION, DST transitions in Europe/Berlin and America/New_York"
python3 -m venv "$WORK/venv"
cat > "$WORK/requirements.txt" <<EOF
croniter==$CRONITER_VERSION --hash=sha256:8ef3d544107a5c05a150a2d78f8bf5a8eb9c5c4d93405a736b824109574e3f4d --hash=sha256:fc124f751b1b04805c2a04b061898b436b45ab2320b045e1e052ea895de65189
python-dateutil==2.9.0.post0 --hash=sha256:a8b2bc7bffae282281c8140a97d3aa9c14da0b136dfe83f850eea9a5f7470427 --hash=sha256:37dd54208da7e1cd875388217d5e00ebd4179249f90fb72437e91a35459a0ad3
six==1.17.0 --hash=sha256:4721f391ed90541fddacab5acf947aa0d3dc7d27b2e1e8eda2be8970586c3274 --hash=sha256:ff70335d468e7eb6ec65b95b99d3a2836546063f63acc5171de367e834932a81
EOF
"$WORK/venv/bin/pip" install --quiet --disable-pip-version-check --require-hashes --only-binary :all: -r "$WORK/requirements.txt" > "$WORK/pip.txt" 2>&1 || fail "pip install croniter: $(cat "$WORK/pip.txt")"
"$WORK/venv/bin/python" - <<'PY' || fail "minicron and croniter disagree (see above)"
import json, subprocess, sys
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from croniter import croniter
cases = [
    ("30 2 * * *", "Europe/Berlin", "2026-03-27T12:00:00+00:00", 4),
    ("0 * * * *", "Europe/Berlin", "2026-03-28T23:30:00+00:00", 4),
    ("*/15 2 * * *", "Europe/Berlin", "2026-03-28T22:00:00+00:00", 6),
    ("*/30 * * * *", "Europe/Berlin", "2026-03-28T23:20:00+00:00", 5),
    ("30 2 * * *", "Europe/Berlin", "2026-10-24T12:00:00+00:00", 4),
    ("0 * * * *", "Europe/Berlin", "2026-10-24T22:30:00+00:00", 6),
    ("*/30 * * * *", "Europe/Berlin", "2026-10-24T23:20:00+00:00", 8),
    ("30 1 * * *", "America/New_York", "2026-10-31T12:00:00+00:00", 3),
    ("0 * * * *", "America/New_York", "2026-11-01T03:30:00+00:00", 5),
    ("30 2 * * *", "America/New_York", "2026-03-07T12:00:00+00:00", 3),
    ("0 * * * *", "America/New_York", "2026-03-08T05:30:00+00:00", 4),
    ("15 2,14 * * 0", "America/New_York", "2026-03-07T00:00:00+00:00", 4),
    ("*/15 9-17 * * mon-fri", "UTC", "2026-11-13T17:40:00+00:00", 5),
    ("0 0 13 * 5", "UTC", "2026-02-01T00:00:00+00:00", 6),
    ("0 0 */2 * 1", "UTC", "2026-01-01T00:00:00+00:00", 8),
    ("0 0 1-7 * 1", "UTC", "2026-01-01T00:00:00+00:00", 8),
    ("0 12 1 jan,jul *", "UTC", "2026-01-01T00:00:00+00:00", 4),
    ("5/20 * * * *", "UTC", "2026-01-01T00:00:00+00:00", 6),
    ("0 0 29 2 *", "UTC", "2026-01-01T00:00:00+00:00", 2),
    ("@weekly", "UTC", "2026-01-01T00:00:00+00:00", 3),
    ("@monthly", "Europe/Berlin", "2026-01-01T00:00:00+00:00", 3),
    ("0 22 * * 1-5", "America/New_York", "2026-03-06T00:00:00+00:00", 6),
]
problems = 0
for expr, zone, start, count in cases:
    base = datetime.fromisoformat(start).astimezone(ZoneInfo(zone))
    it = croniter(expr, base)
    theirs = [it.get_next(datetime).astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ") for _ in range(count)]
    run = subprocess.run([sys.executable, "code/main.py", "next", expr, "--tz", zone, "--from", start, "-n", str(count), "--dialect", "robfig", "--gap", "shift", "--fold", "both", "--json"], capture_output=True, text=True)
    if run.returncode != 0:
        print(f"  minicron failed on {expr!r}: {run.stderr.strip()}")
        problems += 1
        continue
    mine = [r["utc"] for r in json.loads(run.stdout)["runs"]]
    ok = mine == theirs
    problems += 0 if ok else 1
    print(f"  {'ok ' if ok else 'DIFF'} {expr!r:<26} {zone:<17} {count} runs from {start[:16]}")
    if not ok:
        print(f"       croniter: {theirs}\n       minicron: {mine}")
print(f"  {len(cases) - problems}/{len(cases)} expressions agree with croniter (minicron --dialect robfig --gap shift --fold both)")

def minicron(*args):
    out = subprocess.run([sys.executable, "code/main.py", "next", *args, "--json"], capture_output=True, text=True, check=True)
    return [r["utc"] for r in json.loads(out.stdout)["runs"]]

fold_cronie = minicron("30 2 * * *", "--tz", "Europe/Berlin", "--from", "2026-10-24T12:00Z", "-n", "2")
fold_croniter = [d.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ") for d in (lambda it: [it.get_next(datetime), it.get_next(datetime)])(croniter("30 2 * * *", datetime(2026, 10, 24, 12, tzinfo=timezone.utc).astimezone(ZoneInfo("Europe/Berlin"))))]
assert fold_cronie != fold_croniter, (fold_cronie, fold_croniter)
print(f"  documented difference: 30 2 * * * on 25 Oct, cronie rule {fold_cronie[:1]} vs croniter {fold_croniter}")
vixie = minicron("0 0 */2 * 1", "--from", "2026-01-01T00:00Z", "-n", "3", "--dialect", "vixie")
robfig = minicron("0 0 */2 * 1", "--from", "2026-01-01T00:00Z", "-n", "3", "--dialect", "robfig")
assert vixie != robfig, (vixie, robfig)
print(f"  documented difference: 0 0 */2 * 1, vixie {vixie} vs robfig/croniter {robfig}")
raise SystemExit(1 if problems else 0)
PY

if command -v flock >/dev/null 2>&1 && command -v setsid >/dev/null 2>&1 && command -v pgrep >/dev/null 2>&1; then
  echo "▸ Use It: flock -n and minicron share the same kernel lock"
  LOCK="$WORK/sync.lock"
  setsid flock "$LOCK" sleep 60 &
  HOLDER=$!
  PGROUPS+=("$HOLDER")
  wait_until 10 not lock_free "$LOCK" || fail "flock never took $LOCK"
  CODE=0
  mc run --name sync --lock "$LOCK" --journal "$WORK/runs.jsonl" -- true 2> "$WORK/busy.err" || CODE=$?
  [[ "$CODE" == "75" ]] && grep -q "is busy" "$WORK/busy.err" || fail "minicron should refuse a lock held by flock(1) with 75: code $CODE, $(cat "$WORK/busy.err")"
  CODE=0
  flock -n -E 75 "$LOCK" true || CODE=$?
  [[ "$CODE" == "75" ]] || fail "flock -n -E 75 should exit 75 on a held lock, got $CODE"
  kill -TERM -- "-$HOLDER" 2>/dev/null || true
  wait "$HOLDER" 2>/dev/null || true
  wait_until 10 lock_free "$LOCK" || fail "$LOCK still held after the flock(1) holder was killed"
  echo "  held by flock(1): minicron exit 75, flock -n -E 75 exit 75; released when the holder died"

  echo "▸ minicron passes the lock to the job: killing the wrapper does not free it, killing the job does"
  python3 code/main.py run --name long --lock "$LOCK" -- sleep 60 > /dev/null 2>&1 &
  WRAPPER=$!
  PROCS+=("$WRAPPER")
  # minicron starts the job only after flock(2) succeeded, so wait for the child before
  # probing: a probe any earlier could make minicron's non-blocking flock fail.
  child_pid() { pgrep -P "$WRAPPER" > "$WORK/child.txt" 2>/dev/null; }
  wait_until 10 child_pid || fail "no child process under minicron $WRAPPER"
  if lock_free "$LOCK"; then fail "minicron started the job without holding $LOCK"; fi
  CHILD="$(head -1 "$WORK/child.txt")"
  PROCS+=("$CHILD")
  [[ "$(head -1 "$LOCK" | cut -d' ' -f1)" == "$WRAPPER" ]] || fail "the lock file should name minicron's pid $WRAPPER: $(cat "$LOCK")"
  kill -TERM "$WRAPPER"
  wait "$WRAPPER" 2>/dev/null || true
  if lock_free "$LOCK"; then fail "the lock was released although the job (pid $CHILD) still runs"; fi
  kill -TERM "$CHILD"
  wait_until 10 lock_free "$LOCK" || fail "$LOCK still held after the job died"
  [[ -f "$LOCK" ]] || fail "the lock file must never be deleted"
  echo "  wrapper $WRAPPER killed: lock still held by job $CHILD; job killed: lock free; file kept"
else
  echo "  (flock, setsid or pgrep not installed; flock checks skipped)"
fi

echo "▸ timeouts and stale pid files"
CODE=0
mc run --name hang --timeout 1 --journal "$WORK/runs.jsonl" -- sleep 30 2> "$WORK/timeout.err" || CODE=$?
[[ "$CODE" == "124" ]] || fail "minicron --timeout should exit 124, got $CODE: $(cat "$WORK/timeout.err")"
if command -v timeout >/dev/null 2>&1; then
  CODE=0
  timeout 1 sleep 30 || CODE=$?
  [[ "$CODE" == "124" ]] || fail "timeout(1) should exit 124, got $CODE"
fi
sh -c 'exit 0' &
DEAD=$!
wait "$DEAD" || true
echo "$DEAD 1000" > "$WORK/job.pid"
mc run --name tidy --pidfile "$WORK/job.pid" --journal "$WORK/runs.jsonl" -- true 2> "$WORK/stale.err" || fail "a stale pid file should be taken over: $(cat "$WORK/stale.err")"
grep -q "stale lock left by pid $DEAD" "$WORK/stale.err" || fail "stale lock note missing: $(cat "$WORK/stale.err")"
[[ ! -e "$WORK/job.pid" ]] || fail "minicron should remove its own pid file after the run"
python3 - "$WORK/runs.jsonl" <<'PY' || fail "run journal: $(cat "$WORK/runs.jsonl")"
import json, sys
outcomes = [json.loads(line)["outcome"] for line in open(sys.argv[1])]
assert outcomes[-2:] == ["timeout", "succeeded"], outcomes
print("  run journal outcomes: " + ", ".join(outcomes))
PY

if [[ "$(ps -o comm= -p 1 2>/dev/null | tr -d ' ')" == "systemd" ]] && command -v systemd-run >/dev/null 2>&1 && sudo -n true 2>/dev/null; then
  echo "▸ Use It: a transient systemd timer with RandomizedDelaySec, next elapse checked against minicron"
  TIMER="cc-job-scheduling-$$"
  NOW="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  sudo -n systemd-run --unit "$TIMER" --on-calendar='*-*-* 04:17:00' --timer-property=RandomizedDelaySec=1h --timer-property=AccuracySec=1s /bin/true > "$WORK/run.txt" 2>&1 || fail "systemd-run: $(cat "$WORK/run.txt")"
  TZ=UTC systemctl show "$TIMER.timer" -p NextElapseUSecRealtime --value > "$WORK/next.txt" || fail "systemctl show $TIMER.timer"
  systemctl list-timers --all --no-pager > "$WORK/timers.txt" 2>&1 || true
  grep "$TIMER" "$WORK/timers.txt" | sed 's/^/  /' || true
  mc calendar '*-*-* 04:17:00' --base-time "$NOW" --iterations 2 --json > "$WORK/mine-timer.json" || fail "minicron calendar for the timer"
  python3 - "$WORK/next.txt" "$WORK/mine-timer.json" <<'PY' || fail "timer next elapse: $(cat "$WORK/next.txt")"
import json, re, sys
from datetime import datetime, timedelta, timezone
text = open(sys.argv[1]).read().strip()
match = re.search(r"(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d)", text)
assert match, text
real = datetime.strptime(match.group(1), "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
runs = [datetime.strptime(r["utc"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc) for r in json.load(open(sys.argv[2]))["runs"]]
window = [(r, r + timedelta(hours=1, minutes=1)) for r in runs]
assert any(lo <= real <= hi for lo, hi in window), (text, runs)
base = next(lo for lo, hi in window if lo <= real <= hi)
print(f"  minicron next {base:%Y-%m-%d %H:%M:%S} UTC, systemd NextElapse {real:%H:%M:%S}: randomized delay {int((real - base).total_seconds())} s of at most 3600")
PY
else
  echo "  (systemd is not PID 1 here or sudo is unavailable; transient timer skipped)"
fi

if ! command -v kubectl >/dev/null 2>&1; then
  echo "  (kubectl not installed; CronJob checks skipped)"
  echo "SMOKE OK: scripting/job-scheduling (without kind)"
  exit 0
fi
kubectl config get-contexts -o name > "$WORK/contexts.txt" 2>/dev/null || true
if ! grep -qx "$CTX" "$WORK/contexts.txt"; then
  echo "  (context $CTX not found; CronJob checks skipped)"
  echo "SMOKE OK: scripting/job-scheduling (without kind)"
  exit 0
fi
k() { kubectl --context "$CTX" "$@"; }
k delete namespace "$NS" --ignore-not-found --wait=true --timeout=90s >/dev/null 2>&1 || true
NS_CREATED=1
k create namespace "$NS" >/dev/null || fail "create namespace $NS"

cronjob() {
  local name="$1" schedule="$2" zone="$3" policy="$4" seconds="$5"
  local tz_line=""
  [[ -n "$zone" ]] && tz_line="  timeZone: \"$zone\""
  cat <<EOF
apiVersion: batch/v1
kind: CronJob
metadata:
  name: $name
  namespace: $NS
spec:
  schedule: "$schedule"
$tz_line
  concurrencyPolicy: $policy
  successfulJobsHistoryLimit: 5
  jobTemplate:
    spec:
      backoffLimit: 0
      template:
        spec:
          restartPolicy: Never
          terminationGracePeriodSeconds: 0
          containers:
            - name: work
              image: alpine:3.22
              command: ["sleep", "$seconds"]
EOF
}

echo "▸ Use It on $CTX: the API server validates timeZone and refuses CRON_TZ in the schedule"
cronjob zoned "30 2 * * *" "Europe/Berlin" Forbid 5 > "$WORK/zoned.yaml"
k apply --dry-run=server -f "$WORK/zoned.yaml" > "$WORK/zoned.txt" 2>&1 || fail "a CronJob with timeZone Europe/Berlin should be valid: $(cat "$WORK/zoned.txt")"
cronjob badzone "30 2 * * *" "Mars/Olympus" Forbid 5 > "$WORK/badzone.yaml"
if k apply --dry-run=server -f "$WORK/badzone.yaml" > "$WORK/badzone.txt" 2>&1; then fail "timeZone Mars/Olympus should be rejected: $(cat "$WORK/badzone.txt")"; fi
grep -qi "timeZone" "$WORK/badzone.txt" || fail "rejection should name timeZone: $(cat "$WORK/badzone.txt")"
cronjob crontz "CRON_TZ=Europe/Berlin 30 2 * * *" "" Forbid 5 > "$WORK/crontz.yaml"
if k apply --dry-run=server -f "$WORK/crontz.yaml" > "$WORK/crontz.txt" 2>&1; then fail "CRON_TZ in the schedule should be rejected: $(cat "$WORK/crontz.txt")"; fi
grep -qi "schedule" "$WORK/crontz.txt" || fail "rejection should name the schedule: $(cat "$WORK/crontz.txt")"
CODE=0
mc next "CRON_TZ=Europe/Berlin 30 2 * * *" > /dev/null 2> "$WORK/mc-crontz.err" || CODE=$?
[[ "$CODE" == "2" ]] || fail "minicron should refuse CRON_TZ in the expression too: $(cat "$WORK/mc-crontz.err")"
echo "  timeZone Europe/Berlin accepted; Mars/Olympus and CRON_TZ= rejected by the API server and by minicron"

echo "▸ Use It on $CTX: concurrencyPolicy Forbid with a 100 s job on * * * * *, compared with minicron's simulation"
cronjob slow "* * * * *" "" Forbid 100 > "$WORK/slow.yaml"
k apply -f "$WORK/slow.yaml" > /dev/null || fail "apply CronJob slow"
CREATED="$(k get cronjob slow -n "$NS" -o jsonpath='{.metadata.creationTimestamp}')"
job_minutes() {
  k get jobs -n "$NS" -o json > "$WORK/jobs.json" 2>/dev/null || return 1
  python3 - "$WORK/jobs.json" > "$WORK/job-minutes.txt" <<'PY'
import json, sys
names = [j["metadata"]["name"] for j in json.load(open(sys.argv[1]))["items"] if any(o.get("name") == "slow" for o in j["metadata"].get("ownerReferences", []))]
print(" ".join(sorted(n.rsplit("-", 1)[1] for n in names)))
PY
  [[ -s "$WORK/job-minutes.txt" && -n "$(tr -d ' \n' < "$WORK/job-minutes.txt")" ]]
}
wait_until 100 job_minutes || fail "no Job created by CronJob slow within 100 s: $(k get cronjob,jobs -n "$NS" 2>&1)"
T0_MIN="$(tr -d '\n' < "$WORK/job-minutes.txt")"
T0=$(( T0_MIN * 60 ))
echo "  first Job slow-$T0_MIN for $(python3 -c 'import sys, time; print(time.strftime("%H:%M:%S", time.gmtime(int(sys.argv[1]))))' "$T0") UTC (CronJob created $CREATED)"
past_t1() { [[ "$(date +%s)" -ge $(( T0 + 80 )) ]]; }
wait_until 100 past_t1 || fail "clock did not reach T0+80 s"
job_minutes || fail "list jobs"
SEEN="$(tr -d '\n' < "$WORK/job-minutes.txt")"
ACTIVE="$(k get cronjob slow -n "$NS" -o jsonpath='{.status.active[*].name}')"
OBSERVED="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
k get events -n "$NS" --field-selector involvedObject.name=slow -o json > "$WORK/events.json" 2>/dev/null || echo '{"items": []}' > "$WORK/events.json"
k patch cronjob slow -n "$NS" -p '{"spec":{"suspend":true}}' > /dev/null 2>&1 || true
cat > "$WORK/slow-jobs.json" <<'EOF'
[{"name": "slow", "cron": "* * * * *", "dialect": "robfig", "duration": 100, "concurrency": "Forbid"}]
EOF
mc simulate --jobs "$WORK/slow-jobs.json" --from "$CREATED" --to "$OBSERVED" --json > "$WORK/slow-sim.json" || fail "simulate slow: $(cat "$WORK/slow-sim.json")"
python3 - "$WORK/slow-sim.json" "$SEEN" "$ACTIVE" "$WORK/events.json" <<'PY' || fail "Kubernetes and minicron disagree: jobs $SEEN, active $ACTIVE"
import json, sys
from datetime import datetime, timezone
sim, seen, active, events = json.load(open(sys.argv[1])), sys.argv[2].split(), sys.argv[3].split(), json.load(open(sys.argv[4]))
minute = lambda iso: str(int(datetime.strptime(iso, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).timestamp()) // 60)
started = [minute(e["scheduled"]) for e in sim["journal"] if e["event"] == "start"]
skipped = [minute(e["scheduled"]) for e in sim["journal"] if e["event"] == "skip"]
print(f"  minicron: start {started}, skip {skipped} (Forbid)")
print(f"  kubernetes: jobs {seen}, active {active}")
assert started == seen, f"started {started} != Jobs {seen}"
assert len(skipped) >= 1 and all(s not in seen for s in skipped), f"skipped {skipped} vs Jobs {seen}"
assert active == ["slow-" + seen[0]], f"the first Job should still be active: {active}"
reasons = sorted({e.get("reason", "") for e in events.get("items", [])})
note = "JobAlreadyActive event seen" if "JobAlreadyActive" in reasons else f"(no JobAlreadyActive event yet; reasons {reasons})"
print(f"  both skip minute(s) {skipped} while slow-{seen[0]} runs; {note}")
PY
k get jobs -n "$NS" > "$WORK/jobs.txt" 2>&1 || true
sed 's/^/  /' "$WORK/jobs.txt"

echo "SMOKE OK: scripting/job-scheduling"
