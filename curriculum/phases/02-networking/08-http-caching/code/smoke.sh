#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."

fail() { echo "SMOKE FAIL: $*" >&2; exit 1; }
WORK="$(mktemp -d)"
NGINX_IMAGE="nginx:1.30-alpine"
NGINX_NAME="cc-http-caching-nginx"
NGINX_STARTED=""
PIDS=()

cleanup() {
  set +e
  for pid in ${PIDS[@]+"${PIDS[@]}"}; do { kill "$pid"; wait "$pid"; } 2>/dev/null; done
  if [[ -n "$NGINX_STARTED" ]]; then docker rm -f "$NGINX_NAME" >/dev/null 2>&1; fi
  rm -rf "$WORK"
}
trap cleanup EXIT

wait_port_file() {
  for _ in $(seq 1 100); do [[ -s "$1" ]] && return 0; sleep 0.1; done
  fail "$2 did not start: $(cat "$3" 2>/dev/null)"
}
free_port() { python3 -c 'import socket; s=socket.socket(); s.bind(("127.0.0.1", 0)); print(s.getsockname()[1]); s.close()'; }
header_of() { tr -d '\r' < "$1" | awk -F': ' -v name="$2" 'tolower($1)==name {print $2}' | tail -1; }
nginx_logs() { if [[ -n "$NGINX_STARTED" ]]; then docker logs "$NGINX_NAME" > "$WORK/nginx.log" 2>&1; tail -20 "$WORK/nginx.log"; fi; }

command -v curl >/dev/null || fail "curl is required on the runner"

echo "▸ minicache check on the header fixtures: four audiences and the classic mistakes"
python3 code/main.py check code/fixtures/account-cookie.txt --url https://shop.example.test/account --json > "$WORK/cookie.json" || fail "check account-cookie.txt"
python3 code/main.py check code/fixtures/redirect-verbose.txt --url https://shop.example.test/catalogue/ --json > "$WORK/catalogue.json" || fail "check redirect-verbose.txt"
python3 code/main.py check code/fixtures/legacy-heuristic.txt --url https://shop.example.test/img/logo.png --json > "$WORK/logo.json" || fail "check legacy-heuristic.txt"
python3 - "$WORK/cookie.json" "$WORK/catalogue.json" "$WORK/logo.json" <<'PY' || fail "checker verdicts: $(cat "$WORK/cookie.json")"
import json, sys
cookie, catalogue, logo = (json.load(open(p)) for p in sys.argv[1:4])
stores = {name: v["store"] for name, v in cookie["audiences"].items()}
assert stores == {"browser": True, "shared": True, "cdn": False, "nginx": False}, stores
assert {n["code"] for n in cookie["notes"]} >= {"set-cookie", "no-validator"}, cookie["notes"]
left = {name: v["remaining"] for name, v in catalogue["audiences"].items()}
assert left["browser"] == 15 and left["cdn"] == 555 and left["nginx"] == 60, left
assert logo["audiences"]["browser"]["source"] == "10% since Last-Modified" and not logo["audiences"]["nginx"]["store"], logo["audiences"]
print("  account: browser and RFC shared caches store it, CDN and nginx refuse the Set-Cookie;",
      f"catalogue: {left['browser']}s left for browsers after Age 45, {left['cdn']}s at the CDN, {left['nginx']}s in nginx (no Age)")
PY

echo "▸ start the test origin and minicache in front of it"
python3 code/main.py origin --port 0 --port-file "$WORK/origin.port" > "$WORK/origin.log" 2>&1 &
ORIGIN_PID=$!
PIDS+=("$ORIGIN_PID")
wait_port_file "$WORK/origin.port" origin "$WORK/origin.log"
ORIGIN_PORT="$(cat "$WORK/origin.port")"
ORIGIN="http://127.0.0.1:$ORIGIN_PORT"
python3 code/main.py serve --origin "$ORIGIN" --port 0 --port-file "$WORK/mini.port" --verbose > "$WORK/mini.log" 2>&1 &
PIDS+=("$!")
wait_port_file "$WORK/mini.port" minicache "$WORK/mini.log"
MINI="http://127.0.0.1:$(cat "$WORK/mini.port")"
CACHES=("mini=$MINI")
echo "  origin $ORIGIN, minicache $MINI"

if command -v docker >/dev/null && docker info >/dev/null 2>&1; then
  echo "▸ nginx proxy_cache ($NGINX_IMAGE, host network) in front of the same origin"
  NGINX_PORT="$(free_port)"
  cat > "$WORK/cache.conf" <<EOF
proxy_cache_path /var/cache/nginx/cc keys_zone=cc:10m levels=1:2 max_size=100m inactive=10m use_temp_path=off;

server {
    listen 127.0.0.1:$NGINX_PORT;

    location / {
        proxy_pass $ORIGIN;
        proxy_http_version 1.1;
        proxy_set_header Connection "";
        proxy_cache cc;
        proxy_cache_revalidate on;
        proxy_cache_lock on;
        proxy_cache_lock_timeout 5s;
        proxy_cache_background_update on;
        add_header X-Cache-Status \$upstream_cache_status always;
    }
}
EOF
  docker pull -q "$NGINX_IMAGE" > "$WORK/pull.txt" 2>&1 || fail "docker pull $NGINX_IMAGE: $(cat "$WORK/pull.txt")"
  docker rm -f "$NGINX_NAME" >/dev/null 2>&1 || true
  NGINX_STARTED=1
  docker run -d --name "$NGINX_NAME" --network host -v "$WORK/cache.conf:/etc/nginx/conf.d/default.conf:ro" "$NGINX_IMAGE" > "$WORK/run.txt" 2>&1 || fail "docker run nginx: $(cat "$WORK/run.txt")"
  CODE=""
  for _ in $(seq 1 120); do
    CODE="$(curl -s -o /dev/null -w '%{http_code}' "http://127.0.0.1:$NGINX_PORT/nginx/ready" || true)"
    [[ "$CODE" == "200" ]] && break
    sleep 0.5
  done
  [[ "$CODE" == "200" ]] || fail "nginx did not answer on $NGINX_PORT (last code $CODE): $(nginx_logs)"
  NGINX="http://127.0.0.1:$NGINX_PORT"
  CACHES+=("nginx=$NGINX")
  echo "  $(docker exec "$NGINX_NAME" nginx -v 2>&1) on $NGINX"
else
  echo "  (docker not available; nginx comparison skipped, minicache checked alone)"
fi

echo "▸ Use It with curl: curl -sI twice, a client If-None-Match, tags, minicache check on a live URL"
for pair in "${CACHES[@]}"; do
  LABEL="${pair%%=*}"
  BASE="${pair#*=}"
  curl -sI "$BASE/use-$LABEL/tagged" > "$WORK/$LABEL-1.txt" || fail "$LABEL: curl -sI"
  curl -sI "$BASE/use-$LABEL/tagged" > "$WORK/$LABEL-2.txt" || fail "$LABEL: curl -sI"
  FIRST="$(header_of "$WORK/$LABEL-1.txt" x-cache-status)"
  SECOND="$(header_of "$WORK/$LABEL-2.txt" x-cache-status)"
  [[ "$FIRST" == "MISS" && "$SECOND" == "HIT" ]] || fail "$LABEL: curl -sI twice should be MISS then HIT, got $FIRST $SECOND: $(cat "$WORK/$LABEL-2.txt")"
  CODE="$(curl -s -o /dev/null -w '%{http_code}' -H 'If-None-Match: "tagged-v1"' "$BASE/use-$LABEL/tagged")"
  [[ "$CODE" == "304" ]] || fail "$LABEL: a matching If-None-Match should get 304 from the cache, got $CODE"
  AGE="$(header_of "$WORK/$LABEL-2.txt" age)"
  KEY="$(header_of "$WORK/$LABEL-2.txt" surrogate-key)"
  if [[ "$LABEL" == "mini" ]]; then
    [[ -n "$AGE" ]] || fail "minicache must send Age on a hit: $(cat "$WORK/$LABEL-2.txt")"
    [[ -z "$KEY" ]] || fail "minicache must strip Surrogate-Key: $(cat "$WORK/$LABEL-2.txt")"
    curl -s -X PURGE "$BASE/.minicache/tag/product-42" > "$WORK/purge.json" || fail "PURGE by tag"
    python3 -c 'import json,sys; d=json.load(open(sys.argv[1])); assert d["purged"] == 1, d' "$WORK/purge.json" || fail "PURGE by tag: $(cat "$WORK/purge.json")"
    curl -sI "$BASE/use-$LABEL/tagged" > "$WORK/$LABEL-3.txt"
    [[ "$(header_of "$WORK/$LABEL-3.txt" x-cache-status)" == "MISS" ]] || fail "after the tag purge the next request must be a MISS: $(cat "$WORK/$LABEL-3.txt")"
  else
    [[ -n "$KEY" ]] || fail "nginx forwards Surrogate-Key untouched; it was missing: $(cat "$WORK/$LABEL-2.txt")"
  fi
  python3 code/main.py check "$BASE/use-$LABEL/fresh" --json > "$WORK/$LABEL-check.json" || fail "$LABEL: minicache check on a live URL"
  python3 - "$WORK/$LABEL-check.json" <<'PY' || fail "$LABEL: check verdict: $(cat "$WORK/$LABEL-check.json")"
import json, sys
report = json.load(open(sys.argv[1]))
browser, nginx = report["audiences"]["browser"], report["audiences"]["nginx"]
assert report["status"] == 200 and browser["store"] and browser["lifetime"] == 2 and browser["after_expiry"] == "revalidate with ETag", browser
assert nginx["store"] and nginx["lifetime"] == 2, nginx
PY
  echo "  $LABEL: curl -sI $FIRST then $SECOND, If-None-Match 304, Age '${AGE:-none}', Surrogate-Key to the client '${KEY:-stripped}'"
done

echo "▸ the same scenarios through every cache: hits, revalidation, Vary, no-store, collapsing, stale-while-revalidate, stale-if-error"
cat > "$WORK/probe.py" <<'PY'
import concurrent.futures
import http.client
import json
import os
import signal
import sys
import time
import urllib.parse

origin, origin_pid = sys.argv[1], int(sys.argv[2])
caches = [tuple(arg.split("=", 1)) for arg in sys.argv[3:]]


def request(base, path, headers=None, method="GET"):
    parts = urllib.parse.urlsplit(base)
    connection = http.client.HTTPConnection(parts.hostname, parts.port, timeout=15)
    try:
        connection.request(method, path, headers=headers or {})
        response = connection.getresponse()
        body = response.read().decode()
        return response.status, {k.lower(): v for k, v in response.getheaders()}, body
    finally:
        connection.close()


def rows(path):
    paths = json.loads(request(origin, "/__origin/stats")[2])["paths"]
    return paths.get(path, {"requests": []})["requests"]


def check(condition, *what):
    if not condition:
        raise SystemExit("SMOKE FAIL: " + " ".join(str(w) for w in what))


def word(headers):
    return headers.get("x-cache-status", "")


primed = {}
for label, base in caches:
    p = f"/{label}"
    first, second = request(base, p + "/fresh"), request(base, p + "/fresh")
    primed[label] = time.time()
    check(first[0] == second[0] == 200, label, "fresh: status", first[0], second[0])
    check((word(first[1]), word(second[1])) == ("MISS", "HIT"), label, "fresh: MISS then HIT", word(first[1]), word(second[1]))
    check(len(rows(p + "/fresh")) == 1, label, "fresh: a hit inside max-age must not reach the origin", rows(p + "/fresh"))
    langs = ("en", "de", "en", "de")
    seen = [request(base, p + "/vary", {"Accept-Language": lang}) for lang in langs]
    check([s[2].strip().endswith("lang=" + lang) for s, lang in zip(seen, langs)] == [True] * 4, label, "vary: each language must get its own body", [s[2] for s in seen])
    check([word(s[1]) for s in seen] == ["MISS", "MISS", "HIT", "HIT"], label, "vary: one stored variant per Accept-Language", [word(s[1]) for s in seen])
    check(len(rows(p + "/vary")) == 2, label, "vary: two origin requests for four requests", rows(p + "/vary"))
    for _ in range(2):
        request(base, p + "/nostore")
    check(len(rows(p + "/nostore")) == 2, label, "no-store: both requests must reach the origin", rows(p + "/nostore"))
    for name in ("sie", "swr", "plain"):
        status, headers, _ = request(base, f"{p}/{name}")
        check(status == 200 and word(headers) == "MISS", label, name, "prime", status, word(headers))
    print(f"  {label}: fresh MISS then HIT; Vary en/de MISS MISS HIT HIT with 2 origin requests; no-store 2 of 2 at the origin")

for label, base in caches:
    p = f"/{label}"
    with concurrent.futures.ThreadPoolExecutor(8) as pool:
        results = list(pool.map(lambda _: request(base, p + "/slow"), range(8)))
    words = sorted(word(r[1]) for r in results)
    check({r[0] for r in results} == {200} and len({r[2] for r in results}) == 1, label, "collapse: all eight must get the same 200", [(r[0], r[2]) for r in results])
    check(len(rows(p + "/slow")) == 1, label, "collapse: eight concurrent misses must reach the origin once", len(rows(p + "/slow")))
    check(words.count("MISS") == 1, label, "collapse: one MISS, the rest served from the first response", words)
    print(f"  {label}: 8 concurrent misses -> 1 origin request ({' '.join(words)})")

time.sleep(max(0.0, max(primed.values()) + 3.6 - time.time()))

for label, base in caches:
    p = f"/{label}"
    status, headers, body = request(base, p + "/fresh")
    last = rows(p + "/fresh")[-1]
    check(word(headers) == "REVALIDATED" and body.strip() == "fresh v1 lang=-", label, "fresh: after max-age the cache must revalidate", word(headers), body)
    check(last["if_none_match"] == '"fresh-v1"' and last["status"] == 304, label, "fresh: the origin must see If-None-Match and answer 304", last)
    status, headers, _ = request(base, p + "/fresh", {"Cache-Control": "no-cache"})
    count = len(rows(p + "/fresh"))
    if label == "mini":
        check(word(headers) == "REVALIDATED" and count == 3, label, "request no-cache must revalidate", word(headers), count)
    else:
        check(word(headers) == "HIT" and count == 2, label, "nginx ignores the client's Cache-Control: no-cache", word(headers), count)
    status, headers, body = request(base, p + "/swr")
    check(status == 200 and word(headers) == "STALE" and body.strip() == "swr v1 lang=-", label, "swr: the stale copy must be served at once", status, word(headers), body)
    deadline = time.time() + 5
    while len(rows(p + "/swr")) < 2 and time.time() < deadline:
        time.sleep(0.1)
    refresh = rows(p + "/swr")
    check(len(refresh) == 2 and refresh[-1]["if_none_match"] == '"swr-v1"' and refresh[-1]["status"] == 304, label, "swr: one conditional background refresh", refresh)
    print(f"  {label}: REVALIDATED with If-None-Match \"fresh-v1\" -> 304; request no-cache {'revalidated' if label == 'mini' else 'ignored (HIT)'}; swr STALE at once, background 304")

request(origin, "/__origin/down", method="POST")
for label, base in caches:
    p = f"/{label}"
    status, headers, body = request(base, p + "/sie")
    if label == "mini":
        check(status == 200 and word(headers) == "STALE" and body.strip() == "sie v1 lang=-", label, "stale-if-error must cover an origin 503", status, word(headers), body)
    else:
        check(status == 503, label, "nginx passes an origin 503 through: the header's stale-if-error covers connection failures only", status, word(headers))
    status, headers, _ = request(base, p + "/plain")
    check(status == 503, label, "without stale-if-error the origin's 503 reaches the client", status, word(headers))
    print(f"  {label}: origin answers 503 -> sie {'STALE' if label == 'mini' else '503 passed through'}, plain 503")

os.kill(origin_pid, signal.SIGTERM)
deadline = time.time() + 5
while time.time() < deadline:
    try:
        request(origin, "/__origin/stats")
        time.sleep(0.1)
    except OSError:
        break
else:
    raise SystemExit("SMOKE FAIL: the origin did not stop")
for label, base in caches:
    p = f"/{label}"
    status, headers, body = request(base, p + "/sie")
    check(status == 200 and word(headers) == "STALE" and body.strip() == "sie v1 lang=-", label, "stale-if-error must serve the copy while the origin is unreachable", status, word(headers), body)
    status, headers, _ = request(base, p + "/plain")
    check(status == 502, label, "an unreachable origin without stale-if-error is a 502", status, word(headers))
    print(f"  {label}: origin unreachable -> sie STALE, plain 502")
PY
python3 "$WORK/probe.py" "$ORIGIN" "$ORIGIN_PID" "${CACHES[@]}" || fail "scenario matrix; minicache log: $(tail -20 "$WORK/mini.log"); nginx log: $(nginx_logs)"

echo "SMOKE OK: networking/http-caching"
