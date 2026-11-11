#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."

fail() { echo "SMOKE FAIL: $*" >&2; exit 1; }
[[ "$(uname -s)" == "Linux" ]] || fail "needs Linux"
command -v docker >/dev/null || fail "needs docker"
WORK="$(mktemp -d)"
PROJECT="ccsmoke"
COMPOSE_PROJECT="ccsmoke2"
cleanup() {
  python3 code/main.py -f "$WORK/compose.yaml" -p "$PROJECT" down >/dev/null 2>&1 || true
  if docker compose version >/dev/null 2>&1; then
    docker compose -f "$WORK/compose.yaml" -p "$COMPOSE_PROJECT" down --remove-orphans >/dev/null 2>&1 || true
  fi
  docker rm -f "${PROJECT}-db-1" "${PROJECT}-web-1" "${COMPOSE_PROJECT}-db-1" "${COMPOSE_PROJECT}-web-1" >/dev/null 2>&1 || true
  docker network rm "${PROJECT}_default" "${COMPOSE_PROJECT}_default" >/dev/null 2>&1 || true
  rm -rf "$WORK"
}
trap cleanup EXIT

cat > "$WORK/compose.yaml" <<'EOF'
# db is a Python http.server with a healthcheck; web waits for it to be healthy, then fetches it by service name
name: ccsmoke
services:
  db:
    image: python:3.12-alpine
    command: ["python3", "-m", "http.server", "8000"]
    healthcheck:
      test: ["CMD-SHELL", "wget -qO- http://127.0.0.1:8000/ >/dev/null"]
      interval: 2s
      timeout: 2s
      retries: 10
      start_period: 1s
  web:
    image: python:3.12-alpine
    command: ["sh", "-c", "wget -qO- http://db:8000/ >/dev/null && echo reached-db && sleep 300"]
    depends_on:
      db:
        condition: service_healthy
EOF

echo "▸ config, order and render work without touching the daemon"
python3 code/main.py -f "$WORK/compose.yaml" config > "$WORK/config.json"
python3 - "$WORK/config.json" <<'PY' || fail "config shape"
import json, sys
d = json.load(open(sys.argv[1]))
assert d["name"] == "ccsmoke", d["name"]
assert sorted(d["services"]) == ["db", "web"], d["services"]
assert d["services"]["web"]["depends_on"] == {"db": {"condition": "service_healthy"}}, d["services"]["web"]
assert d["services"]["db"]["healthcheck"]["interval"] == "2s", d["services"]["db"]["healthcheck"]
assert d["networks"] == {"default": {"name": "ccsmoke_default"}}, d["networks"]
print("  services", sorted(d["services"]), "network", d["networks"]["default"]["name"])
PY
ORDER="$(python3 code/main.py -f "$WORK/compose.yaml" order)"
[[ "$ORDER" == $'1. db\n2. web' ]] || fail "start order should be db then web, got: $ORDER"
python3 code/main.py -f "$WORK/compose.yaml" render > "$WORK/render.txt"
grep -q "^docker network create .*ccsmoke_default$" "$WORK/render.txt" || fail "render should create the project network"
grep -q -- "--network-alias db .*--health-cmd 'wget -qO- http://127.0.0.1:8000/ >/dev/null' --health-interval 2s" "$WORK/render.txt" || fail "render should carry the healthcheck flags: $(cat "$WORK/render.txt")"

echo "▸ a bad file fails with the line number and exit 2"
printf 'services:\n  web:\n    image: x\n   ports: [1]\n' > "$WORK/bad.yaml"
set +e
ERR="$(python3 code/main.py -f "$WORK/bad.yaml" config 2>&1)"
CODE=$?
set -e
[[ "$CODE" == "2" ]] || fail "expected exit 2 for a parse error, got $CODE"
grep -q "bad.yaml:4: bad indentation" <<<"$ERR" || fail "expected a line-numbered error, got: $ERR"

echo "▸ up: db starts first, web only after db reports healthy"
docker pull -q python:3.12-alpine >/dev/null
python3 code/main.py -f "$WORK/compose.yaml" --verbose up --timeout 90 > "$WORK/up.txt" 2> "$WORK/up.log" || fail "up failed: $(cat "$WORK/up.log")"
cat "$WORK/up.txt"
[[ "$(cat "$WORK/up.txt")" == $'started ccsmoke-db-1\nstarted ccsmoke-web-1' ]] || fail "unexpected up output: $(cat "$WORK/up.txt")"
grep -q "web: db is healthy after" "$WORK/up.log" || fail "up should log the health wait: $(cat "$WORK/up.log")"
HEALTH="$(docker inspect -f '{{.State.Health.Status}}' "${PROJECT}-db-1")"
[[ "$HEALTH" == "healthy" ]] || fail "db should be healthy when web starts, got $HEALTH"
DB_START="$(docker inspect -f '{{.State.StartedAt}}' "${PROJECT}-db-1")"
WEB_START="$(docker inspect -f '{{.State.StartedAt}}' "${PROJECT}-web-1")"
[[ "$WEB_START" > "$DB_START" ]] || fail "web ($WEB_START) should start after db ($DB_START)"
[[ "$(docker inspect -f '{{index .Config.Labels "com.docker.compose.project"}}' "${PROJECT}-web-1")" == "ccsmoke" ]] || fail "project label missing"
for _ in $(seq 1 40); do docker logs "${PROJECT}-web-1" 2>/dev/null | grep -q reached-db && break; sleep 0.5; done
docker logs "${PROJECT}-web-1" > "$WORK/web.log" 2>&1 || true
grep -q reached-db "$WORK/web.log" || fail "web did not reach db by service name: $(cat "$WORK/web.log")"
echo "  db started $DB_START, became healthy, web started $WEB_START and reached http://db:8000/"

echo "▸ ps finds the project's containers by label and shows health"
python3 code/main.py -f "$WORK/compose.yaml" ps | tee "$WORK/ps.txt"
grep -qE "^ccsmoke-db-1 +db +running +healthy" "$WORK/ps.txt" || fail "ps should list db as healthy"
grep -qE "^ccsmoke-web-1 +web +running" "$WORK/ps.txt" || fail "ps should list web"
python3 code/main.py -f "$WORK/compose.yaml" --json ps | python3 -c 'import json,sys; rows=json.load(sys.stdin); assert len(rows)==2, rows; assert rows[0]["health"]=="healthy", rows' || fail "ps --json"

echo "▸ down removes containers in reverse order, then the network, and is idempotent"
python3 code/main.py -f "$WORK/compose.yaml" down | tee "$WORK/down.txt"
[[ "$(cat "$WORK/down.txt")" == $'removed ccsmoke-web-1\nremoved ccsmoke-db-1' ]] || fail "down order: $(cat "$WORK/down.txt")"
[[ -z "$(docker ps -aq --filter "label=com.docker.compose.project=${PROJECT}")" ]] || fail "containers left behind"
docker network inspect "${PROJECT}_default" >/dev/null 2>&1 && fail "network left behind"
[[ "$(python3 code/main.py -f "$WORK/compose.yaml" down)" == "nothing to remove" ]] || fail "second down should be a no-op"

echo "▸ a dependency that never becomes healthy fails up fast with the inspect command to run"
cat > "$WORK/broken.yaml" <<'EOF'
name: ccsmoke
services:
  db:
    image: python:3.12-alpine
    command: ["sleep", "300"]
    healthcheck:
      test: ["CMD-SHELL", "exit 1"]
      interval: 1s
      timeout: 1s
      retries: 2
  web:
    image: python:3.12-alpine
    command: ["sleep", "300"]
    depends_on:
      db:
        condition: service_healthy
EOF
set +e
ERR="$(python3 code/main.py -f "$WORK/broken.yaml" up --timeout 60 2>&1 >/dev/null)"
CODE=$?
set -e
[[ "$CODE" == "1" ]] || fail "up should exit 1 when a dependency turns unhealthy, got $CODE: $ERR"
grep -q "db is unhealthy" <<<"$ERR" || fail "expected an unhealthy verdict, got: $ERR"
docker inspect "${PROJECT}-web-1" >/dev/null 2>&1 && fail "web must not start when db is unhealthy"
python3 code/main.py -f "$WORK/broken.yaml" down >/dev/null
echo "  $ERR"

if docker compose version >/dev/null 2>&1; then
  echo "▸ Use It: docker compose agrees on the service list and the start behaviour"
  docker compose -f "$WORK/compose.yaml" config --services | sort > "$WORK/compose-services.txt"
  python3 -c 'import json,sys; print("\n".join(sorted(json.load(open(sys.argv[1]))["services"])))' "$WORK/config.json" > "$WORK/mini-services.txt"
  cmp "$WORK/compose-services.txt" "$WORK/mini-services.txt" || fail "service lists differ: $(cat "$WORK/compose-services.txt") vs $(cat "$WORK/mini-services.txt")"
  docker compose -f "$WORK/compose.yaml" -p "$COMPOSE_PROJECT" up -d --wait --wait-timeout 90
  [[ "$(docker inspect -f '{{.State.Health.Status}}' "${COMPOSE_PROJECT}-db-1")" == "healthy" ]] || fail "docker compose --wait returned before db was healthy"
  [[ "$(docker inspect -f '{{index .Config.Labels "com.docker.compose.service"}}' "${COMPOSE_PROJECT}-web-1")" == "web" ]] || fail "compose sets the same service label"
  docker compose -f "$WORK/compose.yaml" -p "$COMPOSE_PROJECT" ps --services --status running | sort | tr '\n' ' ' | grep -q "db web" || fail "docker compose ps should list db and web running"
  docker compose -f "$WORK/compose.yaml" -p "$COMPOSE_PROJECT" down
  docker network inspect "${COMPOSE_PROJECT}_default" >/dev/null 2>&1 && fail "docker compose down left its network"
  echo "  docker compose: same services, same labels, same order; version $(docker compose version --short)"
else
  echo "  (docker compose not installed; Use It comparison skipped)"
fi

echo "SMOKE OK: containers/compose"
