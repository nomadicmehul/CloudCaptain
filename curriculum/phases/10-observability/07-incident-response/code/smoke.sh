#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."

fail() { echo "SMOKE FAIL: $*" >&2; exit 1; }
WORK="$(mktemp -d)"
chmod 755 "$WORK"
AM_IMAGE="${AM_IMAGE:-prom/alertmanager:v0.34.2}"
AM_NAME="cc-incident-am-$$"
F="code/fixtures"
PIDS=()
AM_STARTED=""
GK_A='{}:{alertname="CheckoutHighErrorRate", service="checkout"}'
GK_B='{}:{alertname="SearchErrorRate", service="search"}'

cleanup() {
  set +e
  for pid in ${PIDS[@]+"${PIDS[@]}"}; do { kill "$pid"; wait "$pid"; } 2>/dev/null; done
  if [[ -n "$AM_STARTED" ]]; then docker rm -f "$AM_NAME" >/dev/null 2>&1; fi
  rm -rf "$WORK"
}
trap cleanup EXIT

json_check() {
  local file="$1" expr="$2"
  python3 - "$file" "$expr" <<'PY'
import json, sys
doc = json.load(open(sys.argv[1]))
ok = eval(sys.argv[2])
if not ok:
    print(json.dumps(doc, indent=1)[:3000])
sys.exit(0 if ok else 1)
PY
}

echo "▸ miniincident replay: the fixture scenario on a virtual clock"
python3 code/main.py replay "$F/scenario.json" --config "$F/config.json" > "$WORK/replay.txt" 2>&1 || fail "replay: $(cat "$WORK/replay.txt")"
sed -n '/^incidents/,$p' "$WORK/replay.txt"
python3 code/main.py replay "$F/scenario.json" --config "$F/config.json" --json > "$WORK/replay.json" || fail "replay --json"
json_check "$WORK/replay.json" 'doc["metrics"]["mtta"]["mean"] == 420 and doc["metrics"]["mttr"]["mean"] == 1275 and doc["metrics"]["incidents"] == 2' || fail "replay metrics: MTTA should be 7m and MTTR 21m15s"
json_check "$WORK/replay.json" '[(p["user"], p["level"]) for p in doc["pages"]] == [("alice", 1), ("frank", 2)]' || fail "replay pages: alice (override) then frank after 5m"
json_check "$WORK/replay.json" 'doc["status_page"]["components"] == {"Checkout": "operational", "Search": "operational"}' || fail "status page after both resolves"
python3 code/main.py replay "$F/scenario.json" --config "$F/config.json" --postmortem INC-1 > "$WORK/replay-pm.md" || fail "replay --postmortem"
grep -q 'uses "human error"' "$WORK/replay-pm.md" || fail "the language check should flag the human error note: $(tail -5 "$WORK/replay-pm.md")"

echo "▸ on-call: follow-the-sun layers, an override, a gap"
python3 code/main.py oncall --config "$F/config.json" --at 2026-11-02T10:00:00Z --json > "$WORK/oncall.json" || fail "oncall at 10:00"
json_check "$WORK/oncall.json" '[(r["user"], r["source"]) for r in doc] == [("alice", "override"), ("frank", "layer weekly"), ("ivan", "layer daily")]' || fail "who is on call at 10:00"
python3 code/main.py oncall --config "$F/config.json" --schedule checkout-primary --from 2026-11-02T00:00:00Z --to +7d > "$WORK/week.txt" || fail "the fixture week should have no gap: $(grep NOBODY "$WORK/week.txt")"
python3 - "$F/config.json" "$WORK/gappy.json" <<'PY'
import json, sys
doc = json.load(open(sys.argv[1]))
doc["schedules"]["checkout-primary"]["layers"].pop()
json.dump(doc, open(sys.argv[2], "w"))
PY
CODE=0
python3 code/main.py oncall --config "$WORK/gappy.json" --schedule checkout-primary --from 2026-11-03T00:00:00Z --to +1d > "$WORK/gap.txt" || CODE=$?
[[ "$CODE" == "1" ]] && grep -q "NOBODY" "$WORK/gap.txt" || fail "a schedule without the amer layer should report a gap and exit 1 (exit $CODE): $(cat "$WORK/gap.txt")"
grep "NOBODY" "$WORK/gap.txt" | sed 's/^/ /'

echo "▸ severity matrix"
python3 code/main.py severity --config "$F/config.json" --impact outage --scope many --json > "$WORK/sev.json"
json_check "$WORK/sev.json" 'doc["severity"] == "SEV1" and doc["roles"] == ["commander", "comms", "ops"] and doc["page"]' || fail "outage x many should be SEV1 with three roles"
CODE=0
python3 code/main.py severity --config "$F/config.json" --impact meltdown --scope all > /dev/null 2> "$WORK/sev.err" || CODE=$?
[[ "$CODE" == "2" ]] && grep -q "impact must be one of" "$WORK/sev.err" || fail "an unknown impact should exit 2: code $CODE, $(cat "$WORK/sev.err")"

if ! command -v docker >/dev/null || ! docker info >/dev/null 2>&1; then
  echo "  (docker not available; Alertmanager checks skipped)"
  echo "SMOKE OK: observability/incident-response (offline only)"
  exit 0
fi

T0="$(python3 -c 'import time; print(int(time.time()) // 60 * 60 - 2400)')"
at() { python3 -c 'import sys, datetime; print(datetime.datetime.fromtimestamp(int(sys.argv[1]) + int(sys.argv[2]), datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"))' "$T0" "$1"; }
free_port() { python3 -c 'import socket; s=socket.socket(); s.bind(("127.0.0.1", 0)); print(s.getsockname()[1]); s.close()'; }

echo "▸ miniincident serve with a virtual clock at T0+2m ($(at 120)); scripted times are offsets from T0 = $(at 0)"
python3 code/main.py serve --config "$F/config.json" --listen 127.0.0.1:0 --state "$WORK/state.json" --virtual-clock "$(at 120)" > "$WORK/mini.log" 2>&1 &
PIDS+=("$!")
for _ in $(seq 1 50); do grep -q "listening on" "$WORK/mini.log" 2>/dev/null && break; sleep 0.1; done
grep -q "listening on" "$WORK/mini.log" || fail "miniincident did not start: $(cat "$WORK/mini.log")"
MINI="$(sed -n 's/.*listening on \(http:[^ ]*\).*/\1/p' "$WORK/mini.log")"
echo "  $(cat "$WORK/mini.log")"

mini_get() { curl -fsS "$MINI$1" > "$2" || fail "GET $MINI$1"; }
mini_post() {
  curl -sS --fail-with-body -X POST -H 'Content-Type: application/json' --data @"$2" "$MINI$1" > "$WORK/mini-post.out" 2>&1 || fail "POST $1 $(cat "$2"): $(cat "$WORK/mini-post.out")"
}
clock() {
  printf '{"now": "%s"}' "$(at "$1")" > "$WORK/clock.json"
  mini_post /clock "$WORK/clock.json"
}
cmd() {
  python3 -c 'import json, sys; print(json.dumps({"user": sys.argv[1], "text": sys.argv[2]}))' "$1" "$2" > "$WORK/cmd.json"
  mini_post /commands "$WORK/cmd.json"
  echo "  $1: $2 -> $(python3 -c 'import json, sys; print(json.load(open(sys.argv[1]))["reply"])' "$WORK/mini-post.out")"
}
wait_incidents() {
  local what="$1" expr="$2" deadline=$((SECONDS + ${3:-30}))
  while (( SECONDS < deadline )); do
    if curl -fsS "$MINI/incidents" > "$WORK/incidents.json" 2>/dev/null && python3 - "$WORK/incidents.json" "$expr" "$GK_A" "$GK_B" <<'PY'
import json, sys
incidents = json.load(open(sys.argv[1]))
by_key = {i["group_key"]: i for i in incidents if i["group_key"]}
A, B = by_key.get(sys.argv[3]), by_key.get(sys.argv[4])
try:
    ok = bool(eval(sys.argv[2]))
except (KeyError, TypeError, IndexError):
    ok = False
sys.exit(0 if ok else 1)
PY
    then
      return 0
    fi
    sleep 0.5
  done
  fail "timed out waiting for $what; incidents: $(head -c 4000 "$WORK/incidents.json" 2>/dev/null)"
}
incident_field() {
  python3 - "$WORK/incidents.json" "$1" "$2" <<'PY'
import json, sys
incident = next(i for i in json.load(open(sys.argv[1])) if i["group_key"] == sys.argv[2])
value = incident
for key in sys.argv[3].split("."):
    value = value[int(key)] if isinstance(value, list) else value[key]
print(json.dumps(value) if isinstance(value, (dict, list)) else value)
PY
}
oncall_user() {
  curl -fsS "$MINI/oncall?schedule=$1&at=$(at "$2")" > "$WORK/oncall-now.json" || fail "GET /oncall for $1"
  python3 -c 'import json, sys; print(json.load(open(sys.argv[1]))[0]["user"])' "$WORK/oncall-now.json"
}

AM_PORT="$(free_port)"
AM="http://127.0.0.1:$AM_PORT"
cat > "$WORK/alertmanager.yml" <<EOF
global:
  resolve_timeout: 5m
route:
  receiver: miniincident
  group_by: [alertname, service]
  group_wait: 1s
  group_interval: 2s
  repeat_interval: 5s
receivers:
  - name: miniincident
    webhook_configs:
      - url: $MINI/alertmanager
        send_resolved: true
    pagerduty_configs:
      - routing_key: cc-smoke-not-a-real-key
        url: $MINI/v2/enqueue
        description: '{{ .CommonAnnotations.summary }}'
        severity: '{{ .CommonLabels.severity }}'
        component: '{{ .CommonLabels.service }}'
        send_resolved: true
EOF
chmod 644 "$WORK/alertmanager.yml"

echo "▸ $AM_IMAGE: amtool check-config and amtool config routes test"
docker pull -q "$AM_IMAGE" > "$WORK/pull.txt" 2>&1 || fail "docker pull $AM_IMAGE: $(cat "$WORK/pull.txt")"
amtool() { docker run --rm --network host -v "$WORK/alertmanager.yml:/etc/alertmanager/alertmanager.yml:ro" --entrypoint /bin/amtool "$AM_IMAGE" "$@"; }
amtool check-config /etc/alertmanager/alertmanager.yml > "$WORK/check.txt" 2>&1 || fail "amtool check-config: $(cat "$WORK/check.txt")"
grep -q "SUCCESS" "$WORK/check.txt" || fail "amtool check-config: $(cat "$WORK/check.txt")"
sed 's/^/  /' "$WORK/check.txt"
amtool config routes test --config.file=/etc/alertmanager/alertmanager.yml alertname=CheckoutHighErrorRate service=checkout > "$WORK/routes.txt" 2>&1 || fail "amtool config routes test: $(cat "$WORK/routes.txt")"
[[ "$(tr -d '[:space:]' < "$WORK/routes.txt")" == "miniincident" ]] || fail "routes test should name the miniincident receiver: $(cat "$WORK/routes.txt")"
echo "  routes test: $(cat "$WORK/routes.txt")"

echo "▸ start Alertmanager on $AM (host network, HA gossip off), webhook and PagerDuty integrations both pointed at miniincident"
docker rm -f "$AM_NAME" >/dev/null 2>&1 || true
AM_STARTED=1
docker run -d --name "$AM_NAME" --network host -v "$WORK/alertmanager.yml:/etc/alertmanager/alertmanager.yml:ro" "$AM_IMAGE" \
  --config.file=/etc/alertmanager/alertmanager.yml --storage.path=/alertmanager --web.listen-address="127.0.0.1:$AM_PORT" --cluster.listen-address= > "$WORK/am-run.txt" 2>&1 \
  || fail "docker run alertmanager: $(cat "$WORK/am-run.txt")"
for _ in $(seq 1 60); do curl -fsS "$AM/-/ready" > /dev/null 2>&1 && break; sleep 0.5; done
curl -fsS "$AM/-/ready" > /dev/null 2>&1 || fail "Alertmanager never became ready: $(docker logs "$AM_NAME" 2>&1 | tail -20)"
curl -fsS "$AM/api/v2/status" > "$WORK/am-status.json" || fail "GET /api/v2/status"
echo "  Alertmanager $(python3 -c 'import json, sys; print(json.load(open(sys.argv[1]))["versionInfo"]["version"])' "$WORK/am-status.json") ready"

post_checkout() {
  python3 - "$T0" "$WORK/post.json" "$@" <<'PY'
import datetime, json, sys
t0, path, specs = int(sys.argv[1]), sys.argv[2], sys.argv[3:]
fmt = lambda s: datetime.datetime.fromtimestamp(t0 + int(s), datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
alerts = []
for spec in specs:
    parts = spec.split(":")
    instance, starts, ends = parts[0], parts[1], parts[2] if len(parts) > 2 else ""
    alert = {"labels": {"alertname": "CheckoutHighErrorRate", "service": "checkout", "severity": "critical", "instance": instance},
             "annotations": {"summary": "CheckoutHighErrorRate on checkout"}, "startsAt": fmt(starts), "generatorURL": "http://prometheus.example/graph"}
    if ends:
        alert["endsAt"] = fmt(ends)
    alerts.append(alert)
json.dump(alerts, open(path, "w"))
PY
  curl -sS --fail-with-body -X POST -H 'Content-Type: application/json' --data @"$WORK/post.json" "$AM/api/v2/alerts" > "$WORK/post.out" 2>&1 || fail "POST /api/v2/alerts: $(cat "$WORK/post.out") $(cat "$WORK/post.json")"
}
add_search() {
  amtool --alertmanager.url="$AM" alert add alertname=SearchErrorRate service=search severity=critical instance=search-1 \
    --annotation='summary="SearchErrorRate on search"' --start="$(at 30)" "$@" > "$WORK/amtool-alert.txt" 2>&1 || fail "amtool alert add: $(cat "$WORK/amtool-alert.txt")"
}

echo "▸ fire two groups: checkout through the API (started at T0), search through amtool alert add (started at T0+30s)"
post_checkout "checkout-a:0"
add_search
wait_incidents "one incident per group" 'A is not None and B is not None and len(incidents) == 2'
for key in "$GK_A" "$GK_B"; do
  [[ "$(incident_field "$key" opened_at)" == "$((T0 + 120)).0" ]] || fail "$key should open at the virtual T0+2m: $(incident_field "$key" opened_at)"
  [[ "$(incident_field "$key" severity)" == "SEV2" ]] || fail "$key should be SEV2 (alert severity critical): $(incident_field "$key" severity)"
done
ID_A="$(incident_field "$GK_A" id)"
ID_B="$(incident_field "$GK_B" id)"
L1_A="$(oncall_user checkout-primary 120)"
L1_B="$(oncall_user search-primary 120)"
[[ "$(incident_field "$GK_A" pages.0.user)" == "$L1_A" ]] || fail "$ID_A level 1 should page $L1_A, the checkout-primary on-call at T0+2m: $(incident_field "$GK_A" pages)"
[[ "$(incident_field "$GK_B" pages.0.user)" == "$L1_B" ]] || fail "$ID_B level 1 should page $L1_B: $(incident_field "$GK_B" pages)"
echo "  $ID_A $GK_A paged $L1_A; $ID_B $GK_B paged $L1_B"

echo "▸ repeat and grow: the same checkout alert again (Alertmanager deduplicates), a second instance (the group updates), then a repeat_interval resend"
post_checkout "checkout-a:0" "checkout-b:60"
wait_incidents "the second checkout alert in the same incident" 'len(A["alerts"]) == 2 and len(incidents) == 2'
wait_incidents "a repeat notification" 'A["repeats"] >= 1' 20
python3 - "$WORK/incidents.json" "$GK_A" <<'PY' || fail "PagerDuty dedup_key should be sha256(groupKey) and land on the same incidents: $(head -c 3000 "$WORK/incidents.json")"
import hashlib, json, sys
incidents = json.load(open(sys.argv[1]))
assert len(incidents) == 2, [i["id"] for i in incidents]
for i in incidents:
    assert i["key"] == hashlib.sha256(i["group_key"].encode()).hexdigest(), (i["key"], i["group_key"])
    assert i["notifications"].get("alertmanager", 0) >= 1 and i["notifications"].get("pagerduty", 0) >= 1, i["notifications"]
a = next(i for i in incidents if i["group_key"] == sys.argv[2])
print(f"  {a['id']}: {len(a['alerts'])} alerts, notifications {a['notifications']}, {a['repeats']} repeat(s); every key == sha256(groupKey)")
PY

echo "▸ a silenced group never reaches the incident service"
amtool --alertmanager.url="$AM" silence add service=batch --author=smoke --comment="planned batch maintenance" --duration=10m > "$WORK/silence.txt" 2>&1 || fail "amtool silence add: $(cat "$WORK/silence.txt")"
SILENCE="$(tr -d '[:space:]' < "$WORK/silence.txt")"
amtool --alertmanager.url="$AM" alert add alertname=BatchLag service=batch severity=critical > "$WORK/batch.txt" 2>&1 || fail "amtool alert add BatchLag: $(cat "$WORK/batch.txt")"
SUPPRESSED=""
for _ in $(seq 1 20); do
  curl -fsS "$AM/api/v2/alerts?filter=service%3D%22batch%22" > "$WORK/batch-alerts.json" 2>/dev/null || true
  if json_check "$WORK/batch-alerts.json" 'len(doc) == 1 and doc[0]["status"]["state"] == "suppressed" and doc[0]["status"]["silencedBy"]' > /dev/null 2>&1; then SUPPRESSED=1; break; fi
  sleep 0.5
done
[[ -n "$SUPPRESSED" ]] || fail "BatchLag should be suppressed by silence $SILENCE: $(cat "$WORK/batch-alerts.json")"
sleep 5
mini_get /incidents "$WORK/incidents.json"
json_check "$WORK/incidents.json" 'len(doc) == 2 and all(i["service"] != "batch" for i in doc)' || fail "the silenced BatchLag alert must not open an incident"
echo "  silence $SILENCE suppressed BatchLag; still 2 incidents"

echo "▸ T0+4m: $L1_B acknowledges $ID_B"
clock 240
cmd "$L1_B" "/inc ack $ID_B"

echo "▸ T0+8m: nobody acknowledged $ID_A within 5m, so level 2 was paged at T0+7m"
clock 480
L2_A="$(oncall_user checkout-secondary 420)"
mini_get "/incidents/$ID_A" "$WORK/a.json"
python3 - "$WORK/a.json" "$((T0 + 420))" "$L2_A" <<'PY' || fail "escalation of $ID_A: $(head -c 3000 "$WORK/a.json")"
import json, sys
incident, due, user = json.load(open(sys.argv[1])), float(sys.argv[2]), sys.argv[3]
levels = [(p["level"], p["user"], p["at"]) for p in incident["pages"]]
assert len(levels) == 2 and levels[1] == (2, user, due), levels
assert any(e["kind"] == "escalation" and e["at"] == due for e in incident["timeline"]), [e["kind"] for e in incident["timeline"]]
print(f"  pages: {levels[0][1]} (level 1), {levels[1][1]} (level 2 at T0+7m)")
PY

echo "▸ T0+9m to T0+10m: $L2_A acknowledges, raises severity by the matrix, assigns comms, updates the status page; a deploy and a rollback are recorded"
clock 540
cmd "$L2_A" "/inc ack $ID_A"
cmd "$L2_A" "/inc sev $ID_A impact=outage scope=many"
cmd "$L2_A" "/inc role $ID_A comms @olivia"
cmd "olivia" "/inc status $ID_A investigating Some customers cannot complete checkout. We are investigating."
printf '{"service": "checkout", "version": "v2.14.0", "actor": "ci", "at": "%s"}' "$(at -300)" > "$WORK/deploy.json"
mini_post /deploys "$WORK/deploy.json"
clock 600
printf '{"service": "checkout", "version": "v2.13.4", "actor": "%s", "kind": "rollback"}' "$L2_A" > "$WORK/rollback.json"
mini_post /deploys "$WORK/rollback.json"

echo "▸ T0+14m: the search alert resolves through amtool (endsAt T0+14m)"
clock 840
add_search --end="$(at 840)"
wait_incidents "$ID_B resolved" 'B["status"] == "resolved"'
[[ "$(incident_field "$GK_B" resolved_at)" == "$((T0 + 840)).0" ]] || fail "$ID_B should resolve at T0+14m: $(incident_field "$GK_B" resolved_at)"

echo "▸ T0+27m: both checkout alerts resolve through the API"
clock 1620
post_checkout "checkout-a:0:1620" "checkout-b:60:1620"
wait_incidents "$ID_A resolved" 'A["status"] == "resolved"'
[[ "$(incident_field "$GK_A" resolved_at)" == "$((T0 + 1620)).0" ]] || fail "$ID_A should resolve at T0+27m: $(incident_field "$GK_A" resolved_at)"

echo "▸ MTTA and MTTR match the script: TTA 7m and 2m, TTR 25m and 12m"
mini_get /stats "$WORK/stats.json"
json_check "$WORK/stats.json" 'abs(doc["mtta"]["mean"] - 270) < 1e-6 and abs(doc["mttr"]["mean"] - 1110) < 1e-6 and doc["resolved"] == 2 and doc["escalations"] == 1' \
  || fail "MTTA should be 270s and MTTR 1110s"
python3 -c 'import json, sys; d = json.load(open(sys.argv[1])); print("  MTTA %(m)ss over %(c)s, MTTR %(r)ss over %(n)s" % {"m": d["mtta"]["mean"], "c": d["mtta"]["count"], "r": d["mttr"]["mean"], "n": d["mttr"]["count"]})' "$WORK/stats.json"
mini_get "/incidents/$ID_A" "$WORK/a.json"
python3 - "$WORK/a.json" <<'PY' || fail "timeline of $ID_A: $(head -c 4000 "$WORK/a.json")"
import json, sys
incident = json.load(open(sys.argv[1]))
kinds = [e["kind"] for e in incident["timeline"]]
for kind in ("deploy", "alert", "opened", "page", "alert.added", "escalation", "ack", "role", "severity", "status", "resolved"):
    assert kind in kinds, (kind, kinds)
assert kinds.index("deploy") < kinds.index("alert") < kinds.index("opened") < kinds.index("escalation") < kinds.index("ack") < kinds.index("resolved"), kinds
assert incident["severity"] == "SEV1" and incident["roles"].get("comms") == "olivia" and "commander" in incident["roles"], incident["roles"]
print("  timeline: " + " ".join(kinds))
PY

echo "▸ status page and postmortem draft"
mini_get /status "$WORK/status.json"
json_check "$WORK/status.json" 'doc["components"]["Checkout"] == "operational" and [u["state"] for u in doc["incidents"][0]["updates"]] == ["investigating", "resolved"]' || fail "status page after the resolve"
curl -fsS "$MINI/incidents/$ID_A/postmortem" > "$WORK/pm-http.md" || fail "GET postmortem"
python3 - "$WORK/pm-http.md" <<'PY' || fail "postmortem sections: $(cat "$WORK/pm-http.md")"
import sys
text = open(sys.argv[1]).read()
sections = ["Summary", "Impact", "Timeline", "Response", "Contributing factors", "What went well", "What was hard", "Action items", "Language check"]
positions = [text.index(f"## {s}\n") for s in sections]
assert positions == sorted(positions), positions
assert "| Time to acknowledge | 7m 00s |" in text and "| Time to resolve | 25m 00s |" in text
assert "| FILL IN | | | |" in text and "escalated 1 time(s)" in text and "v2.14.0 was deployed 5m 00s before the first alert" in text
print("  " + " / ".join(sections))
PY
python3 code/main.py postmortem --state "$WORK/state.json" --incident "$ID_A" > "$WORK/pm-cli.md" || fail "postmortem from the saved state"
cmp -s "$WORK/pm-http.md" "$WORK/pm-cli.md" || fail "the CLI draft from --state differs from the served one: $(diff "$WORK/pm-http.md" "$WORK/pm-cli.md" | head -20)"
python3 code/main.py metrics --state "$WORK/state.json" | sed 's/^/  /'
docker logs "$AM_NAME" > "$WORK/am.log" 2>&1 || true
if grep -q 'level=ERROR.*Notify' "$WORK/am.log"; then
  grep 'level=ERROR' "$WORK/am.log" | tail -5
  fail "Alertmanager logged notification errors"
fi

echo "SMOKE OK: observability/incident-response"
