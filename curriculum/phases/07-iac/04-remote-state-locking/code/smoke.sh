#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."

SRV="$PWD/code/main.py"
WORK="$(mktemp -d)"
PIDS=()
fail() {
  echo "SMOKE FAIL: $*" >&2
  if [[ -f "$WORK/server.log" ]]; then echo "--- statesrv log (last 20 lines)" >&2; tail -20 "$WORK/server.log" >&2; fi
  exit 1
}
cleanup() {
  for pid in ${PIDS[@]+"${PIDS[@]}"}; do kill "$pid" 2>/dev/null || true; done
  wait 2>/dev/null || true
  rm -rf "$WORK"
}
trap cleanup EXIT

http_code() { curl -s -o "$WORK/body.json" -w '%{http_code}' "$@"; }
status_json() { python3 "$SRV" status "$ADDR" --json > "$WORK/status.json" || fail "statesrv status: $(cat "$WORK/status.json")"; }
field() { python3 -c 'import json,sys; d=json.load(open(sys.argv[1])); v=d.get(sys.argv[2]); print("" if v is None else (v.get("ID", "") if isinstance(v, dict) else v))' "$WORK/status.json" "$1"; }
wait_for_lock() {
  local id=""
  for _ in $(seq 1 300); do
    python3 "$SRV" status "$ADDR" --json > "$WORK/poll.json" 2>/dev/null || true
    id=$(python3 -c 'import json,sys; print((json.load(open(sys.argv[1])).get("lock") or {}).get("ID", ""))' "$WORK/poll.json" 2>/dev/null || true)
    if [[ -n "$id" ]]; then echo "$id"; return 0; fi
    sleep 0.1
  done
  return 1
}

echo "▸ start statesrv on a free port with basic auth"
export STATESRV_PASSWORD="smoke-$RANDOM-$RANDOM"
export TF_HTTP_USERNAME=tofu
export TF_HTTP_PASSWORD="$STATESRV_PASSWORD"
python3 "$SRV" serve --data "$WORK/data" --port 0 --port-file "$WORK/port" --user tofu > "$WORK/server.log" 2>&1 &
PIDS+=("$!")
for _ in $(seq 1 100); do [[ -s "$WORK/port" ]] && break; sleep 0.1; done
[[ -s "$WORK/port" ]] || fail "statesrv did not start: $(cat "$WORK/server.log")"
BASE="http://127.0.0.1:$(cat "$WORK/port")"
ADDR="$BASE/states/demo"
AUTH=(-u "tofu:$STATESRV_PASSWORD")

echo "▸ the protocol by hand with curl: 401, 403, 404, LOCK, 423 with the holder, 409 on a wrong ID"
CODE=$(http_code "$ADDR"); [[ "$CODE" == "401" ]] || fail "no credentials should be 401, got $CODE"
CODE=$(http_code -u tofu:wrong "$ADDR"); [[ "$CODE" == "403" ]] || fail "wrong credentials should be 403, got $CODE"
CODE=$(http_code "${AUTH[@]}" "$ADDR"); [[ "$CODE" == "404" ]] || fail "no state yet should be 404 (tofu reads that as empty), got $CODE"
CODE=$(http_code "${AUTH[@]}" -X LOCK -d '{"ID":"probe-1","Who":"curl-one"}' "$BASE/states/probe"); [[ "$CODE" == "200" ]] || fail "LOCK should be 200, got $CODE: $(cat "$WORK/body.json")"
CODE=$(http_code "${AUTH[@]}" -X LOCK -d '{"ID":"probe-2","Who":"curl-two"}' "$BASE/states/probe"); [[ "$CODE" == "423" ]] || fail "second LOCK should be 423, got $CODE"
python3 -c 'import json,sys; d=json.load(open(sys.argv[1])); assert d["ID"] == "probe-1" and d["Who"] == "curl-one", d' "$WORK/body.json" || fail "423 body should be the holder's lock info: $(cat "$WORK/body.json")"
CODE=$(http_code "${AUTH[@]}" -X UNLOCK -d '{"ID":"probe-2"}' "$BASE/states/probe"); [[ "$CODE" == "409" ]] || fail "UNLOCK with the wrong ID should be 409, got $CODE"
CODE=$(http_code "${AUTH[@]}" -X UNLOCK -d '{"ID":"probe-1"}' "$BASE/states/probe"); [[ "$CODE" == "200" ]] || fail "UNLOCK with the right ID should be 200, got $CODE"
echo "  401 403 404 200 423 409 200"

if ! command -v tofu >/dev/null; then
  echo "  (tofu not installed; Use It skipped)"
  echo "SMOKE OK: iac/remote-state-locking (statesrv only)"
  exit 0
fi

echo "▸ tofu init with backend \"http\" (lock_method POST, unlock_method DELETE on /lock)"
STACK="$WORK/stack"
mkdir -p "$STACK" && cd "$STACK"
cat > main.tf <<EOF
terraform {
  required_providers {
    local = { source = "hashicorp/local", version = ">= 2.4" }
    time  = { source = "hashicorp/time", version = ">= 0.9" }
  }
  backend "http" {
    address        = "$ADDR"
    lock_address   = "$ADDR/lock"
    unlock_address = "$ADDR/lock"
    lock_method    = "POST"
    unlock_method  = "DELETE"
  }
}

variable "content" { type = string }
variable "hold" { type = string }
variable "round" { type = string }

resource "local_file" "motd" {
  filename = "\${path.module}/out/motd.txt"
  content  = "\${var.content}\n"
}

resource "time_sleep" "hold" {
  create_duration = var.hold
  triggers        = { round = var.round }
}
EOF
vars() { printf '{"content": "%s", "hold": "%s", "round": "%s"}\n' "$1" "$2" "$3" > terraform.tfvars.json; }
tofu init -input=false -no-color > "$WORK/init.txt" 2>&1 || fail "tofu init: $(tail -20 "$WORK/init.txt")"

echo "▸ first apply: statesrv holds serial 1 with the lineage tofu generated, written under tofu's lock"
vars v1 1s 1
tofu apply -auto-approve -input=false -no-color > "$WORK/apply1.txt" 2>&1 || fail "first apply: $(tail -20 "$WORK/apply1.txt")"
[[ "$(cat out/motd.txt)" == "v1" ]] || fail "apply should write out/motd.txt"
tofu state pull > "$WORK/pulled.json" 2> "$WORK/pull.err" || fail "tofu state pull: $(cat "$WORK/pull.err")"
python3 "$SRV" history "$ADDR" --json > "$WORK/history.json" || fail "statesrv history"
status_json
python3 - "$WORK/status.json" "$WORK/pulled.json" "$WORK/history.json" <<'PY' || fail "state after the first apply: $(cat "$WORK/status.json")"
import json, sys
status, pulled, history = (json.load(open(p)) for p in sys.argv[1:4])
assert status["serial"] == 1 and status["versions"] == 1, status
assert status["lineage"] == pulled["lineage"], (status["lineage"], pulled["lineage"])
assert status["lock"] is None, status["lock"]
assert {r["type"] for r in pulled["resources"]} == {"local_file", "time_sleep"}, pulled["resources"]
assert history[0]["lock_id"], history[0]
print("  serial 1, lineage", status["lineage"], "written by", history[0]["who"], "under lock", history[0]["lock_id"])
PY

echo "▸ a lock someone else holds: tofu apply fails with the lock error naming its ID and holder"
HELD=$(python3 "$SRV" lock "$ADDR" --who smoke-holder --operation OperationTypeApply --info "held by the smoke test") || fail "statesrv lock"
vars v2 1s 1
RC=0
tofu apply -auto-approve -input=false -no-color -lock-timeout=0s > "$WORK/blocked.txt" 2>&1 || RC=$?
[[ "$RC" != "0" ]] || fail "apply should fail while $HELD is held: $(tail -20 "$WORK/blocked.txt")"
grep -q "Error acquiring the state lock" "$WORK/blocked.txt" || fail "expected the lock error: $(tail -30 "$WORK/blocked.txt")"
grep -q "$HELD" "$WORK/blocked.txt" || fail "the lock error should name $HELD: $(tail -30 "$WORK/blocked.txt")"
grep -q "smoke-holder" "$WORK/blocked.txt" || fail "the lock error should show who holds it: $(tail -30 "$WORK/blocked.txt")"
[[ "$(cat out/motd.txt)" == "v1" ]] || fail "a blocked apply must not change anything"
echo "  blocked by $HELD (smoke-holder)"

echo "▸ tofu force-unlock releases it by ID; the next apply writes serial 2"
tofu force-unlock -force "$HELD" > "$WORK/force1.txt" 2>&1 || fail "tofu force-unlock: $(cat "$WORK/force1.txt")"
status_json
[[ -z "$(field lock)" ]] || fail "the lock should be gone after force-unlock: $(cat "$WORK/status.json")"
tofu apply -auto-approve -input=false -no-color > "$WORK/apply2.txt" 2>&1 || fail "apply after force-unlock: $(tail -20 "$WORK/apply2.txt")"
status_json
[[ "$(field serial)" == "2" ]] || fail "expected serial 2: $(cat "$WORK/status.json")"

echo "▸ two real applies at once: the second fails on the first one's lock, the first finishes"
vars v3 10s 2
tofu apply -auto-approve -input=false -no-color > "$WORK/first.txt" 2>&1 &
FIRST=$!
PIDS+=("$FIRST")
LOCK2=$(wait_for_lock) || fail "the first apply never took the lock: $(tail -20 "$WORK/first.txt")"
RC=0
tofu apply -auto-approve -input=false -no-color -lock-timeout=0s > "$WORK/second.txt" 2>&1 || RC=$?
[[ "$RC" != "0" ]] || fail "the second apply should fail on the lock: $(tail -20 "$WORK/second.txt")"
grep -q "Error acquiring the state lock" "$WORK/second.txt" || fail "expected the lock error: $(tail -30 "$WORK/second.txt")"
grep -q "$LOCK2" "$WORK/second.txt" || fail "the error should name the first apply's lock $LOCK2: $(tail -30 "$WORK/second.txt")"
RC=0
wait "$FIRST" || RC=$?
[[ "$RC" == "0" ]] || fail "the first apply should succeed: $(tail -20 "$WORK/first.txt")"
[[ "$(cat out/motd.txt)" == "v3" ]] || fail "the first apply should have written v3"
status_json
[[ "$(field serial)" == "3" && -z "$(field lock)" ]] || fail "expected serial 3 and no lock: $(cat "$WORK/status.json")"
echo "  second apply refused by $LOCK2; serial 3"

echo "▸ force-unlock during an apply: statesrv refuses its write and tofu saves errored.tfstate"
vars v4 10s 3
tofu apply -auto-approve -input=false -no-color > "$WORK/orphan.txt" 2>&1 &
ORPHAN=$!
PIDS+=("$ORPHAN")
LOCK3=$(wait_for_lock) || fail "the apply never took the lock: $(tail -20 "$WORK/orphan.txt")"
tofu force-unlock -force "$LOCK3" > "$WORK/force2.txt" 2>&1 || fail "tofu force-unlock: $(cat "$WORK/force2.txt")"
RC=0
wait "$ORPHAN" || RC=$?
[[ "$RC" != "0" ]] || fail "an apply whose lock was force-unlocked must not report success: $(tail -30 "$WORK/orphan.txt")"
grep -q "409" "$WORK/orphan.txt" || fail "tofu should report statesrv's 409: $(tail -30 "$WORK/orphan.txt")"
[[ -f errored.tfstate ]] || fail "tofu should save the state it could not write to errored.tfstate: $(tail -30 "$WORK/orphan.txt")"
[[ "$(cat out/motd.txt)" == "v4" ]] || fail "the changes were applied; only the state write was refused"
status_json
[[ "$(field serial)" == "3" ]] || fail "the refused write must not change the stored serial: $(cat "$WORK/status.json")"
echo "  infrastructure changed, state write refused (409), errored.tfstate saved"

echo "▸ recover with tofu state push errored.tfstate; the next plan has no changes"
tofu state push errored.tfstate > "$WORK/push.txt" 2>&1 || fail "state push errored.tfstate: $(cat "$WORK/push.txt")"
status_json
SERIAL=$(field serial)
[[ "$SERIAL" -gt 3 ]] || fail "push should store a newer serial: $(cat "$WORK/status.json")"
RC=0
tofu plan -detailed-exitcode -input=false -no-color > "$WORK/plan.txt" 2>&1 || RC=$?
[[ "$RC" == "0" ]] || fail "after the push, state should match reality (plan exit $RC): $(tail -30 "$WORK/plan.txt")"
rm -f errored.tfstate
echo "  serial $SERIAL, plan clean"

echo "▸ the server's own checks: an older serial and a foreign lineage are refused even with state push -force"
python3 "$SRV" pull "$ADDR" --version 1 > "$WORK/v1.tfstate" || fail "statesrv pull --version 1"
RC=0
tofu state push -force "$WORK/v1.tfstate" > "$WORK/push-old.txt" 2>&1 || RC=$?
[[ "$RC" != "0" ]] && grep -q "409" "$WORK/push-old.txt" || fail "an older serial should get 409 from statesrv: rc=$RC $(cat "$WORK/push-old.txt")"
python3 - "$WORK/v1.tfstate" "$WORK/foreign.tfstate" <<'PY'
import json, sys, uuid
state = json.load(open(sys.argv[1]))
state["lineage"] = str(uuid.uuid4())
state["serial"] = 99
json.dump(state, open(sys.argv[2], "w"), indent=2)
PY
RC=0
tofu state push "$WORK/foreign.tfstate" > "$WORK/push-foreign.txt" 2>&1 || RC=$?
[[ "$RC" != "0" ]] && grep -qi "lineage" "$WORK/push-foreign.txt" || fail "tofu itself should refuse a foreign lineage: rc=$RC $(cat "$WORK/push-foreign.txt")"
RC=0
tofu state push -force "$WORK/foreign.tfstate" > "$WORK/push-foreign-force.txt" 2>&1 || RC=$?
[[ "$RC" != "0" ]] && grep -q "409" "$WORK/push-foreign-force.txt" || fail "-force skips tofu's check, statesrv should still refuse: rc=$RC $(cat "$WORK/push-foreign-force.txt")"
status_json
[[ "$(field serial)" == "$SERIAL" ]] || fail "refused pushes must not change the serial: $(cat "$WORK/status.json")"
echo "  older serial: 409; foreign lineage: refused by tofu, and by statesrv under -force"

echo "▸ history and audit log: one lineage, serials only go up, every refusal recorded"
python3 "$SRV" history "$ADDR" --json > "$WORK/history.json" || fail "statesrv history"
python3 "$SRV" events "$ADDR" --json > "$WORK/events.json" || fail "statesrv events"
python3 - "$WORK/history.json" "$WORK/events.json" "$HELD" "$LOCK2" "$LOCK3" <<'PY' || fail "history/events: $(cat "$WORK/history.json")"
import json, sys
history, events = json.load(open(sys.argv[1])), json.load(open(sys.argv[2]))
held, lock2, lock3 = sys.argv[3:6]
serials = [v["serial"] for v in history]
assert serials[:3] == [1, 2, 3] and len(serials) == 4 and serials[3] > 3, serials
assert len({v["lineage"] for v in history}) == 1, history
assert all(v["lock_id"] for v in history), "every tofu write carried its lock ID"
assert history[2]["lock_id"] == lock2, (history[2], lock2)
refused = [e for e in events if e["event"] == "write-refused"]
assert any(e.get("lock_id") == lock3 and "not held" in e["reason"] for e in refused), refused
assert any("older than" in e["reason"] for e in refused), refused
assert any("lineage" in e["reason"] for e in refused), refused
assert any(e["event"] == "lock-refused" and e.get("holder") == held for e in events), "blocked apply not recorded"
assert any(e["event"] == "lock-refused" and e.get("holder") == lock2 for e in events), "racing apply not recorded"
forced = [e for e in events if e["event"] == "unlock" and "force-unlock" in e.get("released_by", "")]
assert {e["lock_id"] for e in forced} >= {held, lock3}, forced
print("  serials", serials, "| refused writes", len(refused), "| force-unlocks", len(forced))
PY
python3 "$SRV" history "$ADDR"

echo "SMOKE OK: iac/remote-state-locking"
