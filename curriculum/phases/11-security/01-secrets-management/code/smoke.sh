#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."

fail() { echo "SMOKE FAIL: $*" >&2; exit 1; }
WORK="$(mktemp -d)"
SCRIPT="code/fixtures/scenario.json"
BAO_IMAGE="openbao/openbao:2.7.2"
PG_IMAGE="postgres:18.6-alpine"
PREFIX="cc-secrets-management"
NET="$PREFIX-net"
PG="$PREFIX-pg"
BAO="$PREFIX-bao"
BAO_INIT="$PREFIX-bao-init"
NAMESPACE="$PREFIX"
CONTEXT="kind-cc-smoke"
PG_ADMIN_PASSWORD="pg-admin-$RANDOM$RANDOM"
PIDS=()
CONTAINERS=()
KUBE=0

cleanup() {
  set +e
  for pid in ${PIDS[@]+"${PIDS[@]}"}; do
    kill "$pid" 2>/dev/null
    wait "$pid" 2>/dev/null
  done
  for name in ${CONTAINERS[@]+"${CONTAINERS[@]}"}; do docker rm -f -v "$name" >/dev/null 2>&1; done
  docker network rm "$NET" >/dev/null 2>&1
  if [[ "$KUBE" == "1" ]]; then kubectl --context "$CONTEXT" delete namespace "$NAMESPACE" --wait=false >/dev/null 2>&1; fi
  rm -rf "$WORK"
}
trap cleanup EXIT

minivault() { python3 code/main.py "$@"; }
bao() { docker exec -i -e BAO_ADDR=http://127.0.0.1:8200 -e BAO_TOKEN="${BAO_TOKEN:-root}" "$BAO" bao "$@"; }
free_port() { python3 -c 'import socket; s = socket.socket(); s.bind(("127.0.0.1", 0)); print(s.getsockname()[1]); s.close()'; }
jf() {
  python3 - "$1" "$2" <<'PY'
import json, sys
value = json.load(open(sys.argv[1]))
for part in filter(None, sys.argv[2].split(".")):
    value = value[int(part)] if isinstance(value, list) else value[part]
print(value if isinstance(value, str) else json.dumps(value))
PY
}
api() {
  local method="$1" url="$2" token="$3" body="$4" out="$5"
  local args=(-s -o "$out" -w '%{http_code}' -X "$method")
  [[ -n "$token" ]] && args+=(-H "X-Vault-Token: $token")
  [[ -n "$body" ]] && args+=(-H "Content-Type: application/json" -d "$body")
  curl "${args[@]}" "$url" || true
}
wait_for() {
  local what="$1" seconds="$2"
  shift 2
  local deadline=$((SECONDS + seconds))
  until "$@" >/dev/null 2>&1; do
    (( SECONDS < deadline )) || fail "$what not ready after ${seconds}s${WAIT_LOG:+: $(cat "$WAIT_LOG")}"
    sleep 0.5
  done
}
start_minivault() {
  local log="$1"
  shift
  local port
  port="$(free_port)"
  minivault server --listen "127.0.0.1:$port" "$@" > "$log" 2>&1 &
  PIDS+=($!)
  STARTED_URL="http://127.0.0.1:$port"
  WAIT_LOG="$log" wait_for "minivault on port $port" 20 curl -sf -o /dev/null "$STARTED_URL/v1/sys/seal-status"
}
url_of() { if [[ "$1" == "bao" ]]; then echo "$BAO_URL"; else echo "$MV_URL"; fi; }
role_missing() { ! role_exists "$1"; }
stop_last_minivault() {
  local pid="${PIDS[${#PIDS[@]}-1]}"
  kill "$pid"
  wait "$pid" 2>/dev/null || true
  unset 'PIDS[${#PIDS[@]}-1]'
}
unseal_with() {
  local url="$1" out="$2"
  shift 2
  local key code
  for key in "$@"; do
    code="$(api PUT "$url/v1/sys/unseal" "" "{\"key\": \"$key\"}" "$out")"
  done
  echo "$code"
}
foreign_share() {
  python3 - "$@" <<'PY'
import base64, json, sys
pool_file, used_file, used = sys.argv[1:4]
taken = {base64.b64decode(json.load(open(used_file))["shares_b64"][int(i)])[-1] for i in used.split(",")}
print(next(s for s in json.load(open(pool_file))["unseal_keys_b64"] if base64.b64decode(s)[-1] not in taken))
PY
}
role_exists() {
  docker exec "$PG" psql -U postgres -tAc "SELECT count(*) FROM pg_roles WHERE rolname = '$1'" > "$WORK/role-count.txt" 2>&1 || fail "pg_roles query: $(cat "$WORK/role-count.txt")"
  [[ "$(tr -d '[:space:]' < "$WORK/role-count.txt")" == "1" ]]
}
pg_login() {
  local user="$1" password="$2" out="$3"
  docker exec -e PGPASSWORD="$password" "$PG" psql -h "$PG_IP" -U "$user" -d postgres -tAc 'SELECT current_user, count(*) FROM orders' > "$out" 2>&1
}

command -v docker >/dev/null || fail "docker is required"
command -v curl >/dev/null || fail "curl is required"

echo "▸ minivault shamir: any 3 of 5 shares give the secret back, 2 shares give something else"
minivault shamir split --random-bytes 32 --shares 5 --threshold 3 --json > "$WORK/split.json" || fail "shamir split: $(cat "$WORK/split.json")"
SECRET_HEX="$(jf "$WORK/split.json" secret_hex)"
share() { jf "$WORK/split.json" "shares_hex.$1"; }
for trio in "0 1 2" "2 3 4" "0 2 4" "1 3 4"; do
  read -r a b c <<< "$trio"
  got="$(minivault shamir combine "$(share "$a")" "$(share "$b")" "$(share "$c")")"
  [[ "$got" == "$SECRET_HEX" ]] || fail "shares $trio combine to $got, want $SECRET_HEX"
done
got="$(minivault shamir combine "$(share 0)" "$(share 3)")"
[[ "$got" != "$SECRET_HEX" && "${#got}" == "64" ]] || fail "two shares should give 32 bytes of noise, got $got"
echo "  4 subsets of 3 agree on ${SECRET_HEX:0:16}..., 2 shares give ${got:0:16}..."

echo "▸ minivault sealed mode: operator init 5/3, unseal with three shares, write, stop"
minivault operator init --data "$WORK/sealed" --shares 5 --threshold 3 --json > "$WORK/mv-init.json" || fail "operator init: $(cat "$WORK/mv-init.json")"
MV_ROOT="$(jf "$WORK/mv-init.json" root_token)"
mvkey() { jf "$WORK/mv-init.json" "unseal_keys_b64.$1"; }
start_minivault "$WORK/mv-sealed.out" --data "$WORK/sealed"
SEALED_URL="$STARTED_URL"
code="$(api GET "$SEALED_URL/v1/secret/data/app/db" "$MV_ROOT" "" "$WORK/sealed-read.json")"
[[ "$code" == "503" ]] || fail "a sealed minivault should answer 503, got $code: $(cat "$WORK/sealed-read.json")"
unseal_with "$SEALED_URL" "$WORK/unseal.json" "$(mvkey 0)" "$(mvkey 1)" > /dev/null
[[ "$(jf "$WORK/unseal.json" progress)" == "2" && "$(jf "$WORK/unseal.json" sealed)" == "true" ]] || fail "after two shares: $(cat "$WORK/unseal.json")"
unseal_with "$SEALED_URL" "$WORK/unseal.json" "$(mvkey 2)" > /dev/null
[[ "$(jf "$WORK/unseal.json" sealed)" == "false" ]] || fail "three shares should unseal: $(cat "$WORK/unseal.json")"
SEALED_VALUE="sealed-$RANDOM$RANDOM"
code="$(api POST "$SEALED_URL/v1/secret/data/app/db" "$MV_ROOT" "{\"data\": {\"password\": \"$SEALED_VALUE\"}}" "$WORK/sealed-write.json")"
[[ "$code" == "200" ]] || fail "write after unseal: $code $(cat "$WORK/sealed-write.json")"
stop_last_minivault
STATE="$WORK/sealed/minivault.json"
MODE="$(python3 -c 'import os, stat, sys; print(oct(stat.S_IMODE(os.stat(sys.argv[1]).st_mode)))' "$STATE")"
[[ "$MODE" == "0o600" ]] || fail "state file mode is $MODE, want 0o600"
! grep -q "$SEALED_VALUE" "$STATE" || fail "the secret value is in the state file in plain text"
! grep -q "$MV_ROOT" "$STATE" || fail "the root token is in the state file in plain text"
echo "  state file $MODE, holds neither the value nor the root token"

echo "▸ minivault shamir recovers the unseal key; its re-split shares unseal the stored data, a mixed set does not"
K1="$(minivault shamir combine "$(mvkey 0)" "$(mvkey 2)" "$(mvkey 4)")"
K2="$(minivault shamir combine "$(mvkey 1)" "$(mvkey 3)" "$(mvkey 4)")"
[[ "$K1" == "$K2" ]] || fail "two quorums give different unseal keys: $K1 vs $K2"
minivault shamir split --hex "$K1" --shares 5 --threshold 3 --json > "$WORK/resplit.json"
newkey() { jf "$WORK/resplit.json" "shares_b64.$1"; }
start_minivault "$WORK/mv-sealed2.out" --data "$WORK/sealed"
SEALED_URL="$STARTED_URL"
code="$(unseal_with "$SEALED_URL" "$WORK/mixed.json" "$(newkey 0)" "$(newkey 1)" "$(foreign_share "$WORK/mv-init.json" "$WORK/resplit.json" 0,1)")"
[[ "$code" == "400" ]] || fail "a share from another split should make the unseal fail with 400, got $code: $(cat "$WORK/mixed.json")"
api GET "$SEALED_URL/v1/sys/seal-status" "" "" "$WORK/status.json" > /dev/null
[[ "$(jf "$WORK/status.json" sealed)" == "true" && "$(jf "$WORK/status.json" progress)" == "0" ]] || fail "after a failed unseal: $(cat "$WORK/status.json")"
unseal_with "$SEALED_URL" "$WORK/unseal.json" "$(newkey 2)" "$(newkey 3)" "$(newkey 4)" > /dev/null
[[ "$(jf "$WORK/unseal.json" sealed)" == "false" ]] || fail "re-split shares should unseal: $(cat "$WORK/unseal.json")"
code="$(api GET "$SEALED_URL/v1/secret/data/app/db" "$MV_ROOT" "" "$WORK/sealed-read.json")"
[[ "$code" == "200" && "$(jf "$WORK/sealed-read.json" data.data.password)" == "$SEALED_VALUE" ]] || fail "read after re-split unseal: $code $(cat "$WORK/sealed-read.json")"
stop_last_minivault
echo "  unseal key ${K1:0:16}... recovered from two quorums; the stored value decrypts after a restart"

echo "▸ start PostgreSQL ($PG_IMAGE) and OpenBao ($BAO_IMAGE, dev mode, file audit device)"
for image in "$PG_IMAGE" "$BAO_IMAGE"; do docker pull -q "$image" > "$WORK/pull.txt" 2>&1 || fail "docker pull $image: $(cat "$WORK/pull.txt")"; done
docker network create "$NET" > /dev/null
docker run -d --name "$PG" --network "$NET" -e POSTGRES_PASSWORD="$PG_ADMIN_PASSWORD" "$PG_IMAGE" > /dev/null
CONTAINERS+=("$PG")
docker run -d --name "$BAO" --network "$NET" -p 127.0.0.1::8200 \
  -e BAO_DEV_ROOT_TOKEN_ID=root \
  -e 'BAO_LOCAL_CONFIG={"audit": [{"file": {"file": {"description": "lab audit log", "options": {"file_path": "/openbao/logs/audit.log"}}}}]}' \
  "$BAO_IMAGE" > /dev/null
CONTAINERS+=("$BAO")
docker port "$BAO" 8200/tcp > "$WORK/bao-port.txt"
BAO_URL="http://$(grep -m1 '^127\.0\.0\.1:' "$WORK/bao-port.txt")"
[[ "$BAO_URL" != "http://" ]] || fail "OpenBao port not published: $(cat "$WORK/bao-port.txt")"
wait_for "OpenBao at $BAO_URL" 60 curl -sf "$BAO_URL/v1/sys/health"
wait_for "PostgreSQL on TCP (the image's first, socket-only server is the init run)" 60 docker exec "$PG" psql -h 127.0.0.1 -U postgres -tAc 'SELECT 1'
docker exec "$PG" psql -v ON_ERROR_STOP=1 -q -U postgres -c 'CREATE TABLE orders (id int); INSERT INTO orders VALUES (1), (2)' > "$WORK/pg-setup.txt" 2>&1 || fail "create table: $(cat "$WORK/pg-setup.txt")"
PG_IP="$(docker inspect -f "{{(index .NetworkSettings.Networks \"$NET\").IPAddress}}" "$PG")"
[[ -n "$PG_IP" ]] || fail "no address for $PG on $NET"
bao status > "$WORK/bao-status.txt" || fail "bao status: $(cat "$WORK/bao-status.txt")"
grep -Eq 'Sealed +false' "$WORK/bao-status.txt" || fail "OpenBao dev should be unsealed: $(cat "$WORK/bao-status.txt")"
echo "  OpenBao $BAO_URL, PostgreSQL $PG_IP"

echo "▸ start minivault (dev mode) with the same PostgreSQL behind docker exec psql and a file audit log"
start_minivault "$WORK/mv-dev.out" --dev --audit-log "$WORK/mv-audit.log" --db-command "docker exec -i $PG psql -v ON_ERROR_STOP=1 -q -U postgres"
MV_URL="$STARTED_URL"

DB_CONFIG="{\"plugin_name\": \"postgresql-database-plugin\", \"allowed_roles\": \"readonly,short\", \"connection_url\": \"postgresql://{{username}}:{{password}}@$PG:5432/postgres?sslmode=disable\", \"username\": \"postgres\", \"password\": \"$PG_ADMIN_PASSWORD\"}"
replay() {
  local target="$1" url="$2" phase="$3"
  shift 3
  local code=0
  minivault replay --addr "$url" --script "$SCRIPT" --json "$@" > "$WORK/$target-$phase.json" 2> "$WORK/$target-$phase.err" || code=$?
  [[ "$code" == "0" ]] || fail "$target $phase replay exit $code: $(cat "$WORK/$target-$phase.err")
$(python3 -c 'import json, sys; [print(s["scenario"], s["name"], s["status"], s["problems"], s["errors"]) for s in json.load(open(sys.argv[1]))["steps"] if not s["ok"]]' "$WORK/$target-$phase.json" 2>/dev/null)"
}
compare() {
  python3 - "$WORK/bao-$1.json" "$WORK/mv-$1.json" <<'PY' || fail "OpenBao and minivault disagree in phase $1"
import json, sys
bao, mini = (json.load(open(p))["steps"] for p in sys.argv[1:3])
assert len(bao) == len(mini), (len(bao), len(mini))
diffs = [(b["scenario"], b["name"], b["status"], m["status"], b["observed"], m["observed"]) for b, m in zip(bao, mini)
         if (b["scenario"], b["name"], b["status"], b["observed"]) != (m["scenario"], m["name"], m["status"], m["observed"])]
for d in diffs:
    print("  differs:", d, file=sys.stderr)
assert not diffs
codes = {}
for step in bao:
    codes[step["status"]] = codes.get(step["status"], 0) + 1
print("  %d steps, same status and fields on both: %s" % (len(bao), ", ".join("%dx %s" % (n, c) for c, n in sorted(codes.items()))))
PY
}

echo "▸ the same script against both: KV versions, check-and-set, delete/undelete/destroy, policies, token TTLs, the token tree, database roles and leases"
for target in bao mv; do
  replay "$target" "$(url_of "$target")" main --scenario kv --scenario policy --scenario tokens --scenario db-setup --scenario db-issue \
    --var "db_config=$DB_CONFIG" --vars-out "$WORK/$target-vars.json"
done
compare main

echo "▸ a dynamic credential logs in to PostgreSQL over the network, with VALID UNTIL set, on both servers"
for target in bao mv; do
  user="$(jf "$WORK/$target-vars.json" db_user)"
  password="$(jf "$WORK/$target-vars.json" db_pass)"
  pg_login "$user" "$password" "$WORK/$target-login.txt" || fail "$target credential does not log in: $(cat "$WORK/$target-login.txt")"
  [[ "$(tr -d '[:space:]' < "$WORK/$target-login.txt")" == "$user|2" ]] || fail "$target login answered: $(cat "$WORK/$target-login.txt")"
  docker exec "$PG" psql -U postgres -tAc "SELECT rolvaliduntil IS NOT NULL FROM pg_roles WHERE rolname = '$user'" > "$WORK/$target-valid.txt" 2>&1
  [[ "$(tr -d '[:space:]' < "$WORK/$target-valid.txt")" == "t" ]] || fail "$target user $user has no VALID UNTIL: $(cat "$WORK/$target-valid.txt")"
  code=0; pg_login "$user" "wrong-$password" "$WORK/$target-wrong.txt" || code=$?
  [[ "$code" != "0" ]] && grep -q "password authentication failed" "$WORK/$target-wrong.txt" || fail "$target: a wrong password logged in: $(cat "$WORK/$target-wrong.txt")"
  echo "  $target: $user logs in and reads 2 rows"
done

echo "▸ revoking the lease drops the user; the 4s lease expires and the server drops its user on its own"
for target in bao mv; do
  replay "$target" "$(url_of "$target")" revoke --scenario db-revoke --scenario db-expired --vars-in "$WORK/$target-vars.json"
  user="$(jf "$WORK/$target-vars.json" db_user)"
  code=0; pg_login "$user" "$(jf "$WORK/$target-vars.json" db_pass)" "$WORK/$target-after.txt" || code=$?
  [[ "$code" != "0" ]] && grep -q "password authentication failed" "$WORK/$target-after.txt" || fail "$target: the revoked credential still works: $(cat "$WORK/$target-after.txt")"
  ! role_exists "$user" || fail "$target: role $user still exists after sys/leases/revoke"
  short_user="$(jf "$WORK/$target-vars.json" short_user)"
  wait_for "$target: expiry to drop $short_user" 20 role_missing "$short_user"
  echo "  $target: revoked $user and expired $short_user are both gone from pg_roles"
done
compare revoke

echo "▸ the audit logs hold an HMAC of a probe value, never the value; sys/audit-hash/file finds it"
PROBE="probe-$RANDOM$RANDOM$RANDOM"
docker exec "$BAO" cat /openbao/logs/audit.log > "$WORK/bao-audit-before.log"
for target in bao mv; do
  replay "$target" "$(url_of "$target")" audit --scenario audit --var "probe=$PROBE" --vars-out "$WORK/$target-audit-vars.json"
done
compare audit
docker exec "$BAO" cat /openbao/logs/audit.log > "$WORK/bao-audit.log"
cp "$WORK/mv-audit.log" "$WORK/mv-audit-copy.log"
for target in bao mv; do
  log="$WORK/$target-audit.log"; [[ "$target" == "mv" ]] && log="$WORK/mv-audit-copy.log"
  hash="$(jf "$WORK/$target-audit-vars.json" probe_hash)"
  plain="$(grep -c "$PROBE" "$log" || true)"
  hashed="$(grep -c "$hash" "$log" || true)"
  [[ "$plain" == "0" ]] || fail "$target: the probe value appears $plain times in the audit log"
  [[ "$hashed" -ge 2 ]] || fail "$target: the probe's hash $hash appears $hashed times, want the write and the read at least"
  python3 - "$log" <<'PY' || fail "$target audit log is not one JSON object per line"
import json, sys
entries = [json.loads(line) for line in open(sys.argv[1]) if line.strip()]
assert {e["type"] for e in entries} >= {"request", "response"}, {e["type"] for e in entries}
PY
  echo "  $target: $PROBE appears 0 times, its HMAC $hashed times ($(wc -l < "$log" | tr -d ' ') entries)"
done

echo "▸ bao CLI on the dev server: kv put/get/delete/undelete/destroy/metadata, policy write, token capabilities, renew and revoke"
bao kv put secret/cli/config user=app password=pw-1 > /dev/null
bao kv put -format=json secret/cli/config user=app password=pw-2 > "$WORK/cli-put.json"
[[ "$(jf "$WORK/cli-put.json" data.version)" == "2" ]] || fail "bao kv put: $(cat "$WORK/cli-put.json")"
code=0; bao kv put -cas=1 secret/cli/config password=pw-x > "$WORK/cli-cas.txt" 2>&1 || code=$?
[[ "$code" != "0" ]] && grep -q "check-and-set" "$WORK/cli-cas.txt" || fail "a stale -cas should fail: exit $code, $(cat "$WORK/cli-cas.txt")"
[[ "$(bao kv get -version=1 -field=password secret/cli/config)" == "pw-1" ]] || fail "bao kv get -version=1"
bao kv delete secret/cli/config > /dev/null
bao kv get -format=json secret/cli/config > "$WORK/cli-deleted.json" 2>&1 || true
python3 - "$WORK/cli-deleted.json" <<'PY' || fail "a soft-deleted version should return metadata without data: $(cat "$WORK/cli-deleted.json")"
import json, sys
doc = json.load(open(sys.argv[1]))
assert doc["data"]["data"] is None and doc["data"]["metadata"]["deletion_time"], doc["data"]
PY
bao kv undelete -versions=2 secret/cli/config > /dev/null
[[ "$(bao kv get -field=password secret/cli/config)" == "pw-2" ]] || fail "undelete did not bring version 2 back"
bao kv destroy -versions=1 secret/cli/config > /dev/null
bao kv metadata get -format=json secret/cli/config > "$WORK/cli-meta.json"
[[ "$(jf "$WORK/cli-meta.json" data.versions.1.destroyed)" == "true" && "$(jf "$WORK/cli-meta.json" data.current_version)" == "2" ]] || fail "metadata: $(cat "$WORK/cli-meta.json")"
printf '%s\n' 'path "secret/data/cli/*" { capabilities = ["create", "read", "update"] }' 'path "secret/data/cli/admin" { capabilities = ["deny"] }' 'path "secret/data/+/shared" { capabilities = ["read"] }' > "$WORK/cli.hcl"
bao policy write cli - < "$WORK/cli.hcl" > /dev/null
APP="$(bao token create -policy=cli -ttl=1h -field=token)"
for path in secret/data/cli/config secret/data/cli/admin secret/data/cli/shared secret/data/team/shared secret/data/other; do
  real="$(bao token capabilities "$APP" "$path" | tr -d ' ' | tr ',' '\n' | sort | paste -sd, -)"
  minivault policy check --no-default --policy "cli=$WORK/cli.hcl" --path "$path" --json > "$WORK/check.json" || true
  mine="$(python3 -c 'import json, sys; print(",".join(sorted(json.load(open(sys.argv[1]))[0]["capabilities"])))' "$WORK/check.json")"
  [[ "$real" == "$mine" ]] || fail "capabilities on $path: bao says $real, minivault policy check says $mine"
  echo "  $path: $real on both"
done
code=0; BAO_TOKEN="$APP" bao kv get secret/cli/admin > "$WORK/cli-denied.txt" 2>&1 || code=$?
[[ "$code" != "0" ]] && grep -q "403" "$WORK/cli-denied.txt" || fail "deny should give 403: exit $code, $(cat "$WORK/cli-denied.txt")"
SHORT="$(bao token create -policy=cli -ttl=10m -explicit-max-ttl=1h -field=token)"
bao token renew -format=json -increment=2h "$SHORT" > "$WORK/cli-renew.json"
python3 - "$WORK/cli-renew.json" <<'PY' || fail "renew past explicit_max_ttl: $(cat "$WORK/cli-renew.json")"
import json, sys
doc = json.load(open(sys.argv[1]))
assert 3500 <= doc["auth"]["lease_duration"] <= 3600, doc["auth"]["lease_duration"]
assert any("capped" in w for w in doc["warnings"] or []), doc["warnings"]
print("  renew -increment=2h under a 1h explicit max: %ds (%s)" % (doc["auth"]["lease_duration"], doc["warnings"][0]))
PY
bao token revoke "$APP" > /dev/null
code=0; BAO_TOKEN="$APP" bao token lookup > "$WORK/cli-revoked.txt" 2>&1 || code=$?
[[ "$code" != "0" ]] || fail "a revoked token still looks itself up: $(cat "$WORK/cli-revoked.txt")"
bao read -format=json database/creds/readonly > "$WORK/cli-creds.json"
CLI_LEASE="$(jf "$WORK/cli-creds.json" lease_id)"
bao lease renew -format=json -increment=30m "$CLI_LEASE" > "$WORK/cli-lease.json"
[[ "$(jf "$WORK/cli-lease.json" lease_duration)" == "1800" ]] || fail "bao lease renew: $(cat "$WORK/cli-lease.json")"
bao lease revoke "$CLI_LEASE" > /dev/null
! role_exists "$(jf "$WORK/cli-creds.json" data.username)" || fail "bao lease revoke left the role in PostgreSQL"
echo "  bao read database/creds, lease renew 30m, lease revoke: role dropped"

echo "▸ OpenBao outside dev mode: bao operator init 5/3; minivault's Shamir recovers its unseal key and re-split shares unseal it"
docker run -d --name "$BAO_INIT" \
  -e 'BAO_LOCAL_CONFIG={"storage": {"inmem": {}}, "listener": [{"tcp": {"address": "0.0.0.0:8200", "tls_disable": true}}], "disable_mlock": true}' \
  "$BAO_IMAGE" server > /dev/null
CONTAINERS+=("$BAO_INIT")
binit() { docker exec -i -e BAO_ADDR=http://127.0.0.1:8200 -e BAO_TOKEN="${BAO_TOKEN:-}" "$BAO_INIT" bao "$@"; }
binit_status() { local code=0; binit status -format=json > "$WORK/init-status.json" 2>&1 || code=$?; [[ "$code" == "0" || "$code" == "2" ]]; }
wait_for "OpenBao server mode" 60 binit_status
binit operator init -key-shares=5 -key-threshold=3 -format=json > "$WORK/bao-init.json" || fail "bao operator init: $(cat "$WORK/bao-init.json")"
bkey() { jf "$WORK/bao-init.json" "unseal_keys_b64.$1"; }
INIT_ROOT="$(jf "$WORK/bao-init.json" root_token)"
for i in 0 1 2; do binit operator unseal -format=json "$(bkey "$i")" > "$WORK/bao-unseal.json"; done
[[ "$(jf "$WORK/bao-unseal.json" sealed)" == "false" ]] || fail "three OpenBao shares should unseal: $(cat "$WORK/bao-unseal.json")"
BAO_TOKEN="$INIT_ROOT" binit secrets enable -path=secret kv-v2 > /dev/null
INIT_VALUE="init-$RANDOM$RANDOM"
kv_written() { BAO_TOKEN="$INIT_ROOT" binit kv put secret/app/db "password=$INIT_VALUE"; }
wait_for "the new kv-v2 mount" 30 kv_written
BAO_TOKEN="$INIT_ROOT" binit operator seal > /dev/null
B1="$(minivault shamir combine "$(bkey 0)" "$(bkey 2)" "$(bkey 4)")"
B2="$(minivault shamir combine "$(bkey 1)" "$(bkey 3)" "$(bkey 4)")"
B3="$(minivault shamir combine "$(bkey 1)" "$(bkey 3)")"
[[ "$B1" == "$B2" && "$B1" != "$B3" && "${#B1}" == "64" ]] || fail "minivault on OpenBao's shares: $B1 / $B2 / two shares $B3"
minivault shamir split --hex "$B1" --shares 5 --threshold 3 --json > "$WORK/bao-resplit.json"
rkey() { jf "$WORK/bao-resplit.json" "shares_b64.$1"; }
binit operator unseal -format=json "$(rkey 0)" > /dev/null
binit operator unseal -format=json "$(rkey 1)" > /dev/null
code=0; binit operator unseal -format=json "$(foreign_share "$WORK/bao-init.json" "$WORK/bao-resplit.json" 0,1)" > "$WORK/bao-mixed.txt" 2>&1 || code=$?
[[ "$code" != "0" ]] && grep -q "400" "$WORK/bao-mixed.txt" || fail "OpenBao should refuse a mixed share set: exit $code, $(cat "$WORK/bao-mixed.txt")"
binit_status
[[ "$(jf "$WORK/init-status.json" sealed)" == "true" && "$(jf "$WORK/init-status.json" progress)" == "0" ]] || fail "after a failed unseal: $(cat "$WORK/init-status.json")"
for i in 2 3 4; do binit operator unseal -format=json "$(rkey "$i")" > "$WORK/bao-unseal.json"; done
[[ "$(jf "$WORK/bao-unseal.json" sealed)" == "false" ]] || fail "OpenBao refused minivault's re-split shares: $(cat "$WORK/bao-unseal.json")"
[[ "$(BAO_TOKEN="$INIT_ROOT" binit kv get -field=password secret/app/db)" == "$INIT_VALUE" ]] || fail "the stored value does not read after unsealing with minivault's shares"
echo "  unseal key ${B1:0:16}... from two quorums of OpenBao's shares; OpenBao unseals with minivault's shares and reads the value"

if command -v kubectl >/dev/null && kubectl config get-contexts -o name > "$WORK/contexts.txt" 2>/dev/null && grep -qx "$CONTEXT" "$WORK/contexts.txt"; then
  echo "▸ Kubernetes: a Secret is base64 in the API and plain bytes in etcd on kind"
  KUBE=1
  k() { kubectl --context "$CONTEXT" "$@"; }
  k create namespace "$NAMESPACE" > /dev/null
  K8S_VALUE="pw-in-etcd-$RANDOM$RANDOM"
  k -n "$NAMESPACE" create secret generic app-db --from-literal=password="$K8S_VALUE" > /dev/null
  k -n "$NAMESPACE" get secret app-db -o jsonpath='{.data.password}' > "$WORK/k8s-b64.txt"
  [[ "$(base64 -d < "$WORK/k8s-b64.txt")" == "$K8S_VALUE" ]] || fail "base64 -d of the Secret: $(cat "$WORK/k8s-b64.txt")"
  ETCD_POD="$(k -n kube-system get pods -l component=etcd -o jsonpath='{.items[*].metadata.name}' | awk '{print $1}')"
  [[ -n "$ETCD_POD" ]] || fail "no etcd pod (label component=etcd) in kube-system: $(k -n kube-system get pods 2>&1)"
  k -n kube-system exec "$ETCD_POD" -- etcdctl --endpoints=https://127.0.0.1:2379 \
    --cacert=/etc/kubernetes/pki/etcd/ca.crt --cert=/etc/kubernetes/pki/etcd/server.crt --key=/etc/kubernetes/pki/etcd/server.key \
    get "/registry/secrets/$NAMESPACE/app-db" --print-value-only > "$WORK/etcd-value.bin" 2> "$WORK/etcd.err" || fail "etcdctl get: $(cat "$WORK/etcd.err")"
  if grep -aq "k8s:enc:" "$WORK/etcd-value.bin"; then
    echo "  this cluster encrypts Secrets at rest ($(grep -ao 'k8s:enc:[a-z0-9]*:v[0-9]*:[^:]*' "$WORK/etcd-value.bin" | head -1)); the plain-text check is skipped"
  else
    grep -aq "$K8S_VALUE" "$WORK/etcd-value.bin" || fail "the Secret value is neither encrypted nor visible in etcd: $(head -c 300 "$WORK/etcd-value.bin" | tr -c '[:print:]' '.')"
    echo "  base64 -d gives the value back, and etcd holds it in plain text ($(wc -c < "$WORK/etcd-value.bin" | tr -d ' ') bytes of protobuf)"
  fi
else
  echo "▸ (no kubectl context $CONTEXT; the Kubernetes Secret check is skipped)"
fi

echo "SMOKE OK: security/secrets-management"
