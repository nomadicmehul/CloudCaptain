#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."

fail() { echo "SMOKE FAIL: $*" >&2; exit 1; }
WORK="$(mktemp -d)"
chmod 755 "$WORK"
F="code/fixtures"
AM_IMAGE="prom/alertmanager:v0.34.2"
PROM_IMAGE="prom/prometheus:v3.15.1"
AM_NAME="cc-alerting-am-$$"
LABEL="cc-lesson=alerting"
SCALE=3
TOLERANCE=1.0
PIDS=()
mkdir -p "$WORK/bin" "$WORK/rules" "$WORK/neg"

cleanup() {
  set +e
  for pid in ${PIDS[@]+"${PIDS[@]}"}; do { kill "$pid"; wait "$pid"; } 2>/dev/null; done
  if command -v docker >/dev/null; then
    docker ps -aq --filter "label=$LABEL" > "$WORK/leftover.txt" 2>/dev/null
    if [[ -s "$WORK/leftover.txt" ]]; then xargs docker rm -f < "$WORK/leftover.txt" >/dev/null 2>&1; fi
  fi
  rm -rf "$WORK"
}
trap cleanup EXIT

minialert() { python3 code/main.py "$@"; }
free_ports() { python3 -c 'import socket, sys; s = [socket.socket() for _ in range(int(sys.argv[1]))]; [x.bind(("127.0.0.1", 0)) for x in s]; print(*[x.getsockname()[1] for x in s]); [x.close() for x in s]' "$1"; }
json_check() {
  python3 - "$1" "$2" <<'PY'
import json, sys
doc = json.load(open(sys.argv[1]))
ok = eval(sys.argv[2])
if not ok:
    print(json.dumps(doc, indent=1)[:3000])
sys.exit(0 if ok else 1)
PY
}

echo "▸ minialert rules check and rules test: the fixture tests pass, the wrong expectations fail"
minialert rules check "$F/rules.json" > "$WORK/check.txt" 2>&1 || fail "rules check: $(cat "$WORK/check.txt")"
grep -q "SUCCESS: 4 rules found" "$WORK/check.txt" || fail "rules check: $(cat "$WORK/check.txt")"
minialert rules test "$F/rules-test.json" > "$WORK/test.txt" 2>&1 || fail "rules test: $(cat "$WORK/test.txt")"
CODE=0
minialert rules test "$F/rules-test-wrong.json" > "$WORK/test-wrong.txt" 2>&1 || CODE=$?
[[ "$CODE" == "1" ]] || fail "the wrong test file should exit 1, got $CODE: $(cat "$WORK/test-wrong.txt")"
for want in "alertname: HighErrorRate, time: 3m" "alertname: InstanceDown, time: 11m"; do
  grep -q "$want" "$WORK/test-wrong.txt" || fail "minialert should report '$want': $(cat "$WORK/test-wrong.txt")"
done

echo "▸ minialert rules eval: for holds an alert in pending, keep_firing_for bridges the one-sample gap"
minialert rules eval "$F/rules-test.json" --json > "$WORK/eval.json" || fail "rules eval"
python3 - "$WORK/eval.json" <<'PY' || fail "lifecycle of HighErrorRate and QueueBacklog: $(head -c 3000 "$WORK/eval.json")"
import json, sys
doc = json.load(open(sys.argv[1]))
def states(name):
    return [(t["at"], t["state"]) for t in doc["transitions"] if t["alert"] == name]
assert states("HighErrorRate") == [(180, "pending"), (480, "firing"), (660, "firing (kept)"), (720, "firing"), (900, "firing (kept)"), (1500, "resolved")], states("HighErrorRate")
assert states("QueueBacklog")[:2] == [(120, "pending"), (240, "firing")], states("QueueBacklog")
assert (840, "resolved") in states("QueueBacklog"), states("QueueBacklog")
print("  HighErrorRate: " + ", ".join(f"{s} at {int(t) // 60}m" for t, s in states("HighErrorRate")))
PY

echo "▸ minialert routes test: the receiver table, with --verify.receivers"
python3 -c 'import json, sys; [print(json.dumps(c)) for c in json.load(open(sys.argv[1]))]' "$F/route-cases.json" > "$WORK/cases.jsonl"
CASES="$(wc -l < "$WORK/cases.jsonl" | tr -d ' ')"
while IFS= read -r line; do
  want="$(python3 -c 'import json, sys; print(json.loads(sys.argv[1])["receivers"])' "$line")"
  python3 -c 'import json, sys; [print(f"{k}={v}") for k, v in json.loads(sys.argv[1])["labels"].items()]' "$line" > "$WORK/labels.txt"
  LABELS=()
  while IFS= read -r l; do LABELS+=("$l"); done < "$WORK/labels.txt"
  minialert routes test --config "$F/alertmanager.json" --verify.receivers="$want" "${LABELS[@]}" > "$WORK/route.txt" 2>&1 || fail "minialert routes test ${LABELS[*]}: want $want, got $(cat "$WORK/route.txt")"
done < "$WORK/cases.jsonl"
echo "  $CASES label sets reach the expected receivers"

echo "▸ minialert simulate: groups, inhibition, a silence and deduplication on a virtual clock"
minialert simulate "$F/scenario.json" --config "$F/alertmanager.json" --json > "$WORK/sim-fixture.json" || fail "simulate"
python3 - "$WORK/sim-fixture.json" <<'PY' || fail "simulation: $(head -c 4000 "$WORK/sim-fixture.json")"
import json, sys
doc = json.load(open(sys.argv[1]))
notes = [(n["at"], n["receiver"], n["payload"]["status"], n["payload"]["groupLabels"].get("alertname")) for n in doc["notifications"]]
assert len(notes) == 12, notes
assert (12.0, "pager", "firing", "ClusterDown") in notes and (24.0, "pager", "resolved", "ClusterDown") in notes, notes
assert not [n for n in notes if n[1] in ("db-pager", "db-chat") and 10 < n[0] < 21], "inhibited alerts were sent: %s" % notes
assert [n for n in notes if n[3] == "CertExpiring"] == [(2.0, "default-chat", "firing", "CertExpiring"), (20.0, "default-chat", "firing", "CertExpiring")], notes
snap = {a["labels"]["alertname"] + "/" + a["labels"].get("instance", ""): a["status"] for a in doc["snapshots"][0]["alerts"]}
assert snap["DiskFull/pg-0"]["inhibitedBy"] and snap["CertExpiring/search-1"]["silencedBy"] == ["search-maintenance"], snap
assert snap["ClusterDown/"]["state"] == "active", snap
kinds = [e["kind"] for e in doc["events"]]
print(f"  {len(notes)} notifications, {kinds.count('inhibited')} inhibited and {kinds.count('silenced')} silenced alert-flushes, {kinds.count('dedup')} deduplicated flushes")
PY

command -v docker >/dev/null && docker info >/dev/null 2>&1 || fail "docker is required for promtool, amtool and Alertmanager"
echo "▸ pull $PROM_IMAGE and $AM_IMAGE; copy promtool and amtool out of the images"
for image in "$PROM_IMAGE" "$AM_IMAGE"; do
  docker pull -q "$image" > "$WORK/pull.txt" 2>&1 || docker pull -q "$image" > "$WORK/pull.txt" 2>&1 || fail "docker pull $image: $(cat "$WORK/pull.txt")"
done
for pair in "$PROM_IMAGE:promtool" "$AM_IMAGE:amtool"; do
  image="${pair%:*}" tool="${pair##*:}"
  docker create --name "cc-alerting-$tool-$$" --label "$LABEL" "$image" > /dev/null
  docker cp "cc-alerting-$tool-$$:/bin/$tool" "$WORK/bin/$tool" > /dev/null || fail "copy /bin/$tool out of $image"
  docker rm "cc-alerting-$tool-$$" > /dev/null
done
export PATH="$WORK/bin:$PATH"
echo "  $(promtool --version 2>&1 | head -1); $(amtool --version 2>&1 | head -1)"

echo "▸ promtool check rules and promtool test rules on the same files"
cp "$F/rules.json" "$F/rules-test.json" "$F/rules-test-wrong.json" "$WORK/rules/"
promtool check rules "$WORK/rules/rules.json" > "$WORK/p-check.txt" 2>&1 || fail "promtool check rules: $(cat "$WORK/p-check.txt")"
grep -q "SUCCESS: 4 rules found" "$WORK/p-check.txt" || fail "promtool check rules: $(cat "$WORK/p-check.txt")"
promtool test rules "$WORK/rules/rules-test.json" > "$WORK/p-test.txt" 2>&1 || fail "promtool test rules: $(cat "$WORK/p-test.txt")"
CODE=0
promtool test rules "$WORK/rules/rules-test-wrong.json" > "$WORK/p-wrong.txt" 2>&1 || CODE=$?
[[ "$CODE" == "1" ]] || fail "promtool should fail the wrong test file with exit 1, got $CODE: $(cat "$WORK/p-wrong.txt")"
for want in "alertname: HighErrorRate, time: 3m" "alertname: InstanceDown, time: 11m"; do
  grep -q "$want" "$WORK/p-wrong.txt" || fail "promtool should report '$want': $(cat "$WORK/p-wrong.txt")"
done
echo "  both pass rules-test.json and fail rules-test-wrong.json at HighErrorRate 3m and InstanceDown 11m"

echo "▸ minialert's prediction at every evaluation, checked by promtool (for, keep_firing_for, labels, annotations)"
minialert rules test "$WORK/rules/rules-test.json" --emit-expected "$WORK/rules/every-minute.json" > "$WORK/emit.txt" 2>&1 || fail "emit: $(cat "$WORK/emit.txt")"
promtool test rules "$WORK/rules/every-minute.json" > "$WORK/p-every.txt" 2>&1 || fail "promtool disagrees with minialert's predictions: $(head -c 4000 "$WORK/p-every.txt")"
echo "  $(grep -m1 "wrote" "$WORK/emit.txt" | sed 's/^minialert: //'); promtool: SUCCESS"
python3 - "$WORK/rules/rules.json" "$WORK/neg/rules.json" <<'PY'
import json, sys
doc = json.load(open(sys.argv[1]))
for rule in doc["groups"][0]["rules"]:
    rule.pop("keep_firing_for", None)
json.dump(doc, open(sys.argv[2], "w"), indent=1)
PY
cp "$WORK/rules/every-minute.json" "$WORK/neg/"
CODE=0
promtool test rules "$WORK/neg/every-minute.json" > "$WORK/p-neg.txt" 2>&1 || CODE=$?
[[ "$CODE" != "0" ]] && grep -q "alertname: HighErrorRate, time: 11m" "$WORK/p-neg.txt" || fail "without keep_firing_for promtool should miss HighErrorRate at 11m (exit $CODE): $(head -c 3000 "$WORK/p-neg.txt")"
echo "  negative control: without keep_firing_for promtool fails at HighErrorRate 11m"

read -r R_AM R_MINI AM_PORT MINI_PORT < <(free_ports 4)
[[ -n "$MINI_PORT" ]] || fail "could not allocate four distinct local ports"
AM="http://127.0.0.1:$AM_PORT"
MINI="http://127.0.0.1:$MINI_PORT"
for pair in "am:$R_AM" "mini:$R_MINI"; do
  python3 - "$F/alertmanager.json" "$WORK/${pair%%:*}.yml" "${pair##*:}" "$SCALE" <<'PY'
import json, sys
sys.path.insert(0, "code")
import main
scale = float(sys.argv[4])
doc = json.loads(open(sys.argv[1]).read().replace("127.0.0.1:9099", "127.0.0.1:" + sys.argv[3]))
def stretch(route):
    for key in ("group_wait", "group_interval", "repeat_interval"):
        if key in route:
            route[key] = main.format_duration(main.parse_duration(route[key]) * scale)
    for child in route.get("routes") or []:
        stretch(child)
stretch(doc["route"])
doc["global"]["resolve_timeout"] = main.format_duration(main.parse_duration(doc["global"]["resolve_timeout"]) * scale)
json.dump(doc, open(sys.argv[2], "w"), indent=1)
PY
done
python3 - "$F/scenario.json" "$WORK/scenario.json" "$SCALE" <<'PY'
import json, sys
sys.path.insert(0, "code")
import main
scale = float(sys.argv[3])
stretch = lambda text: main.format_duration(main.parse_duration(text) * scale)
doc = json.load(open(sys.argv[1]))
for step in doc["steps"]:
    step["at"] = stretch(step["at"])
    if "silence" in step:
        step["silence"]["duration"] = stretch(step["silence"].get("duration", "1h"))
doc["until"] = stretch(doc["until"])
json.dump(doc, open(sys.argv[2], "w"), indent=1)
PY
chmod 644 "$WORK/am.yml" "$WORK/mini.yml"

echo "▸ amtool check-config and minialert check-config"
amtool check-config "$WORK/am.yml" > "$WORK/am-check.txt" 2>&1 || fail "amtool check-config: $(cat "$WORK/am-check.txt")"
grep -q "SUCCESS" "$WORK/am-check.txt" && grep -q "1 inhibit rules" "$WORK/am-check.txt" && grep -q "5 receivers" "$WORK/am-check.txt" || fail "amtool check-config: $(cat "$WORK/am-check.txt")"
minialert check-config "$WORK/am.yml" > "$WORK/mini-check.txt" 2>&1 || fail "minialert check-config: $(cat "$WORK/mini-check.txt")"
grep -q "1 inhibit rules" "$WORK/mini-check.txt" && grep -q "5 receivers" "$WORK/mini-check.txt" || fail "minialert check-config: $(cat "$WORK/mini-check.txt")"
python3 - "$WORK/am.yml" "$WORK/bad.yml" <<'PY'
import json, sys
doc = json.load(open(sys.argv[1]))
doc["route"]["routes"][2]["receiver"] = "storage-chat"
json.dump(doc, open(sys.argv[2], "w"))
PY
for tool in amtool minialert; do
  CODE=0
  if [[ "$tool" == amtool ]]; then amtool check-config "$WORK/bad.yml" > "$WORK/bad-$tool.txt" 2>&1 || CODE=$?; else minialert check-config "$WORK/bad.yml" > "$WORK/bad-$tool.txt" 2>&1 || CODE=$?; fi
  [[ "$CODE" != "0" ]] && grep -q 'undefined receiver "storage-chat"' "$WORK/bad-$tool.txt" || fail "$tool should reject an undefined receiver (exit $CODE): $(cat "$WORK/bad-$tool.txt")"
done
echo "  both: SUCCESS on the fixture; both reject an undefined receiver"

echo "▸ amtool config routes test and minialert routes test name the same receivers for every label set"
while IFS= read -r line; do
  python3 -c 'import json, sys; [print(f"{k}={v}") for k, v in json.loads(sys.argv[1])["labels"].items()]' "$line" > "$WORK/labels.txt"
  LABELS=()
  while IFS= read -r l; do LABELS+=("$l"); done < "$WORK/labels.txt"
  amtool config routes test --config.file="$WORK/am.yml" "${LABELS[@]}" > "$WORK/amtool-route.txt" 2>&1 || fail "amtool config routes test ${LABELS[*]}: $(cat "$WORK/amtool-route.txt")"
  minialert routes test --config "$WORK/am.yml" "${LABELS[@]}" > "$WORK/mini-route.txt" 2>&1 || fail "minialert routes test ${LABELS[*]}: $(cat "$WORK/mini-route.txt")"
  THEIRS="$(tr -d '[:space:]' < "$WORK/amtool-route.txt")"
  MINE="$(tr -d '[:space:]' < "$WORK/mini-route.txt")"
  [[ "$THEIRS" == "$MINE" ]] || fail "routes differ for ${LABELS[*]}: amtool $THEIRS, minialert $MINE"
  printf '  %-60s %s\n' "${LABELS[*]}" "$MINE"
done < "$WORK/cases.jsonl"

echo "▸ the scenario and the timers stretched x$SCALE for the live run: the same notifications, every step at least 2 x ${TOLERANCE}s from every flush"
minialert simulate "$WORK/scenario.json" --config "$WORK/am.yml" --json > "$WORK/sim.json" || fail "simulate the stretched scenario"
python3 - "$WORK/sim.json" "$WORK/sim-fixture.json" "$SCALE" "$TOLERANCE" <<'PY' || fail "the stretched simulation should be the fixture's, scaled: $(head -c 3000 "$WORK/sim.json")"
import json, sys
live, fixture, scale, tolerance = json.load(open(sys.argv[1])), json.load(open(sys.argv[2])), float(sys.argv[3]), float(sys.argv[4])
shape = lambda doc, k: [(round(n["at"] / k, 6), n["receiver"], n["payload"]["status"], n["payload"]["notification_reason"], len(n["payload"]["alerts"])) for n in doc["notifications"]]
assert shape(live, scale) == shape(fixture, 1.0), (shape(live, scale), shape(fixture, 1.0))
assert live["min_margin"] >= 2 * tolerance, (live["min_margin"], tolerance)
print(f"  {len(live['notifications'])} notifications over {live['until']:.0f}s; smallest step-to-flush gap {live['min_margin']:.1f}s, tolerance {tolerance:.1f}s")
PY

LIVE_PIDS=()
stop_live() {
  local pid
  for pid in ${LIVE_PIDS[@]+"${LIVE_PIDS[@]}"}; do { kill "$pid"; wait "$pid"; } 2>/dev/null || true; done
  LIVE_PIDS=()
  docker rm -f "$AM_NAME" > /dev/null 2>&1 || true
}
background() {
  "$@" &
  LIVE_PIDS+=("$!")
  PIDS+=("$!")
}
live_attempt() {
  local D="$WORK/live-$1" url drive_am code
  mkdir -p "$D"
  echo "▸ attempt $1: start two webhook recorders, Alertmanager ($AM, host network, no clustering) and minialert serve ($MINI)"
  background python3 code/main.py receiver --listen "127.0.0.1:$R_AM" --out "$D/am-hooks.jsonl" 2> "$D/recv-am.log"
  background python3 code/main.py receiver --listen "127.0.0.1:$R_MINI" --out "$D/mini-hooks.jsonl" 2> "$D/recv-mini.log"
  background python3 code/main.py serve --config "$WORK/mini.yml" --listen "127.0.0.1:$MINI_PORT" 2> "$D/serve.log"
  docker run -d --name "$AM_NAME" --label "$LABEL" --network host -v "$WORK/am.yml:/etc/alertmanager/alertmanager.yml:ro" "$AM_IMAGE" \
    --config.file=/etc/alertmanager/alertmanager.yml --storage.path=/alertmanager --web.listen-address="127.0.0.1:$AM_PORT" --cluster.listen-address= > "$D/am-run.txt" 2>&1 \
    || { echo "docker run alertmanager: $(cat "$D/am-run.txt")" > "$D/reason"; return 1; }
  for url in "$AM/-/ready" "$MINI/-/ready" "http://127.0.0.1:$R_AM/" "http://127.0.0.1:$R_MINI/"; do
    for _ in $(seq 1 60); do curl -fsS "$url" > /dev/null 2>&1 && break; sleep 0.5; done
    curl -fsS "$url" > /dev/null 2>&1 || { echo "$url never became ready: $(docker logs "$AM_NAME" 2>&1 | tail -20) $(cat "$D/serve.log")" > "$D/reason"; return 1; }
  done
  curl -fsS "$AM/api/v2/status" > "$D/am-status.json" || { echo "GET /api/v2/status failed" > "$D/reason"; return 1; }
  curl -fsS "$MINI/api/v2/silences" > "$D/mini-identity.json" 2>&1 && python3 -c 'import json, sys; assert isinstance(json.load(open(sys.argv[1])), list)' "$D/mini-identity.json" 2> /dev/null \
    || { echo "$MINI does not answer like minialert serve: $(cat "$D/mini-identity.json") $(cat "$D/serve.log")" > "$D/reason"; return 1; }
  for url in "http://127.0.0.1:$R_AM/" "http://127.0.0.1:$R_MINI/"; do
    curl -fsS "$url" > "$D/recv-identity.json" 2>&1 && grep -q '"received"' "$D/recv-identity.json" || { echo "$url does not answer like a minialert receiver: $(cat "$D/recv-am.log" "$D/recv-mini.log")" > "$D/reason"; return 1; }
  done
  echo "  Alertmanager $(python3 -c 'import json, sys; print(json.load(open(sys.argv[1]))["versionInfo"]["version"])' "$D/am-status.json") ready"

  echo "▸ attempt $1: play the stretched scenario against both in real time (silences through amtool silence add and expire on Alertmanager)"
  python3 code/main.py drive "$WORK/scenario.json" --alertmanager "$AM" --amtool "$WORK/bin/amtool --alertmanager.url=$AM" --out "$D/driven-am.json" 2> "$D/drive-am.log" &
  drive_am=$!
  PIDS+=("$drive_am")
  python3 code/main.py drive "$WORK/scenario.json" --alertmanager "$MINI" --out "$D/driven-mini.json" 2> "$D/drive-mini.log" \
    || { echo "drive minialert: $(cat "$D/drive-mini.log")" > "$D/reason"; wait "$drive_am"; return 1; }
  wait "$drive_am" || { echo "drive Alertmanager: $(cat "$D/drive-am.log")" > "$D/reason"; return 1; }
  sleep 1
  echo "  $(cat "$D/drive-am.log")"
  python3 - "$D/driven-am.json" "$D/driven-mini.json" <<'PY'
import json, sys
for path in sys.argv[1:]:
    steps = json.load(open(path))["steps"]
    print(f"  {path.rsplit('/', 1)[-1]}: latest step {max(s['actual'] - s['at'] for s in steps):.2f}s after its scripted time")
PY

  echo "▸ attempt $1: the webhooks Alertmanager sent: what minialert simulate predicted"
  code=0
  minialert compare --tolerance "$TOLERANCE" --simulated "$WORK/sim.json" --driven "$D/driven-am.json" --webhook "$D/am-hooks.jsonl" > "$D/compare-am.txt" 2>&1 || code=$?
  sed 's/^/  /' "$D/compare-am.txt"
  [[ "$code" == "0" ]] || { echo "Alertmanager and minialert simulate differ (exit $code): $(grep MISMATCH "$D/compare-am.txt" | head -c 3000)" > "$D/reason"; return 1; }
  python3 - "$D/am-hooks.jsonl" > "$D/hooks-check.txt" 2>&1 <<'PY' || { echo "webhooks from Alertmanager: $(cat "$D/hooks-check.txt") $(head -c 3000 "$D/am-hooks.jsonl")" > "$D/reason"; return 1; }
import json, sys
hooks = [json.loads(line)["body"] for line in open(sys.argv[1]) if line.strip()]
by_receiver = {}
for h in hooks:
    by_receiver.setdefault(h["receiver"], []).append((h["status"], h["groupLabels"].get("alertname"), len(h["alerts"])))
assert len(hooks) == 12, len(hooks)
assert by_receiver["pager"].count(("firing", "ClusterDown", 1)) == 1 and ("resolved", "ClusterDown", 1) in by_receiver["pager"], by_receiver["pager"]
assert by_receiver["default-chat"] == [("firing", "CertExpiring", 1), ("firing", "CertExpiring", 2)], by_receiver["default-chat"]
assert "blackhole" not in by_receiver, by_receiver.keys()
assert all(h["version"] == "4" and h["groupKey"].startswith("{}") for h in hooks), [h["groupKey"] for h in hooks]
print("  " + "; ".join(f"{r}: {len(v)}" for r, v in sorted(by_receiver.items())))
PY
  cat "$D/hooks-check.txt"

  echo "▸ attempt $1: the webhooks minialert serve sent: the same prediction"
  code=0
  minialert compare --tolerance "$TOLERANCE" --simulated "$WORK/sim.json" --driven "$D/driven-mini.json" --webhook "$D/mini-hooks.jsonl" > "$D/compare-mini.txt" 2>&1 || code=$?
  [[ "$code" == "0" ]] || { echo "minialert serve and minialert simulate differ: $(cat "$D/compare-mini.txt")" > "$D/reason"; return 1; }
  tail -1 "$D/compare-mini.txt" | sed 's/^/  /'

  docker logs "$AM_NAME" > "$D/am.log" 2>&1 || true
  if grep -q 'level=ERROR' "$D/am.log"; then
    echo "Alertmanager logged errors: $(grep 'level=ERROR' "$D/am.log" | tail -5)" > "$D/reason"
    return 1
  fi
  return 0
}

if ! live_attempt 1; then
  echo "  attempt 1 failed: $(cat "$WORK/live-1/reason")"
  echo "  (a machine that stalls shifts every later event; a real difference fails again, so the live comparison runs once more on a fresh Alertmanager)"
  stop_live
  live_attempt 2 || fail "attempt 2 failed as well: $(cat "$WORK/live-2/reason")"
fi
stop_live

echo "SMOKE OK: observability/alerting"
