#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."

fail() { echo "SMOKE FAIL: $*" >&2; exit 1; }
WORK="$(mktemp -d)"
IMAGE="nginx:1.30-alpine"
NGINX="cc-http-server-nginx"
H2="cc-http-server-h2c"
PIDS=()
CONTAINERS=()

cleanup() {
  set +e
  for pid in ${PIDS[@]+"${PIDS[@]}"}; do kill "$pid" 2>/dev/null; wait "$pid" 2>/dev/null; done
  for name in ${CONTAINERS[@]+"${CONTAINERS[@]}"}; do docker rm -f "$name" >/dev/null 2>&1; done
  rm -rf "$WORK"
}
trap cleanup EXIT

free_port() { python3 -c 'import socket; s=socket.socket(); s.bind(("127.0.0.1", 0)); print(s.getsockname()[1]); s.close()'; }
other_port() { local port; port="$(free_port)"; while [[ "$port" == "$1" ]]; do port="$(free_port)"; done; echo "$port"; }
wait_for_file() { for _ in $(seq 1 100); do [[ -s "$1" ]] && return 0; sleep 0.1; done; fail "$1 never appeared: $(cat "$2" 2>/dev/null)"; }
wait_for_http() {
  for _ in $(seq 1 100); do
    if curl -s -o /dev/null --max-time 1 "$@"; then return 0; fi
    sleep 0.2
  done
  return 1
}

command -v curl >/dev/null || fail "curl is required"

echo "▸ document root: hello.txt and docs/index.html with a fixed modification time"
mkdir -p "$WORK/www/docs"
printf 'minihttp and nginx serve these exact bytes from the same file.\n' > "$WORK/www/hello.txt"
printf '<p>docs</p>\n' > "$WORK/www/docs/index.html"
python3 -c 'import os, sys; [os.utime(path, (1767323045, 1767323045)) for path in sys.argv[1:]]' "$WORK/www/hello.txt" "$WORK/www/docs/index.html"
chmod -R a+rX "$WORK/www"
printf 'chunked body sent by curl\n' > "$WORK/upload.txt"
SIZE="$(wc -c < "$WORK/www/hello.txt" | tr -d ' ')"
echo "  hello.txt: $SIZE bytes"

echo "▸ the echo upstream and a port where nothing listens"
python3 code/main.py upstream --port 0 --port-file "$WORK/upstream.port" > "$WORK/upstream.out" 2>&1 &
PIDS+=("$!")
wait_for_file "$WORK/upstream.port" "$WORK/upstream.out"
UP="$(cat "$WORK/upstream.port")"
DEAD="$(free_port)"
FLAGS=(--root "$WORK/www" --header-timeout 2 --body-timeout 2 --keepalive-timeout 3 --keepalive-requests 3 --max-body 65536
  --upstream-connect-timeout 2 --upstream-timeout 2 --proxy "/api/=http://127.0.0.1:$UP" --proxy "/dead/=http://127.0.0.1:$DEAD")
echo "  upstream 127.0.0.1:$UP, dead 127.0.0.1:$DEAD"

echo "▸ minihttp config --json: the settings and the nginx directives they map to"
python3 code/main.py config "${FLAGS[@]}" --json > "$WORK/config.json" 2>&1 || fail "config: $(cat "$WORK/config.json")"
python3 - "$WORK/config.json" <<'PY' || fail "config --json: $(cat "$WORK/config.json")"
import json, sys
report = json.load(open(sys.argv[1]))
nginx = report["nginx_equivalent"]
assert (nginx["client_header_timeout"], nginx["keepalive_timeout"], nginx["keepalive_requests"], nginx["client_max_body_size"]) == ("2s", "3s", "3", "65536"), nginx
assert set(report["nginx_locations"]) == {"/api/", "/dead/"}, report["nginx_locations"]
assert report["nginx_locations"]["/api/"]["proxy_read_timeout"] == "2s", report["nginx_locations"]
print("  client_header_timeout 2s, keepalive_timeout 3s, keepalive_requests 3, client_max_body_size 65536, proxy_read_timeout 2s")
PY

echo "▸ minihttp parse: a smuggling attempt is one 400 and the end of the stream"
printf 'POST /api/echo HTTP/1.1\r\nHost: shop.test\r\nContent-Length: 4\r\nTransfer-Encoding: chunked\r\n\r\n0\r\n\r\nGET /admin HTTP/1.1\r\nHost: shop.test\r\n\r\n' > "$WORK/smuggle.raw"
python3 code/main.py parse "$WORK/smuggle.raw" --json > "$WORK/parse.json" 2>&1 || fail "parse: $(cat "$WORK/parse.json")"
python3 - "$WORK/parse.json" <<'PY' || fail "parse: $(cat "$WORK/parse.json")"
import json, sys
found = json.load(open(sys.argv[1]))
assert len(found) == 1 and found[0]["status"] == 400 and found[0]["close"] and "Transfer-Encoding" in found[0]["reason"], found
print("  1 entry: 400 and close; GET /admin is never parsed")
PY

echo "▸ minihttp serve on a free port"
python3 code/main.py serve "${FLAGS[@]}" --port 0 --port-file "$WORK/mini.port" --access-log "$WORK/minihttp.access" > "$WORK/minihttp.out" 2>&1 &
PIDS+=("$!")
wait_for_file "$WORK/mini.port" "$WORK/minihttp.out"
MINI="127.0.0.1:$(cat "$WORK/mini.port")"

curl_checks() {
  local label="$1" base="http://$2" etag code took
  curl -s --http1.1 -o /dev/null -o /dev/null -w '%{num_connects} ' "$base/hello.txt" "$base/hello.txt" > "$WORK/$label.reuse" || fail "$label: curl reuse check"
  [[ "$(cat "$WORK/$label.reuse")" == "1 0 " ]] || fail "$label: the second request should reuse the connection (num_connects 1 then 0), got '$(cat "$WORK/$label.reuse")'"
  curl -sv --http1.1 -o /dev/null "$base/hello.txt" 2> "$WORK/$label.verbose" || fail "$label: curl -v: $(cat "$WORK/$label.verbose")"
  grep -q '^> GET /hello.txt HTTP/1.1' "$WORK/$label.verbose" || fail "$label: curl -v shows no request line: $(cat "$WORK/$label.verbose")"
  grep -qiE '^< etag: "' "$WORK/$label.verbose" || fail "$label: curl -v shows no ETag: $(cat "$WORK/$label.verbose")"
  curl -s --http1.1 -r 0-7 "$base/hello.txt" > "$WORK/$label.range" || fail "$label: curl -r"
  [[ "$(cat "$WORK/$label.range")" == "minihttp" ]] || fail "$label: curl -r 0-7 gave '$(cat "$WORK/$label.range")'"
  curl -s --http1.1 -I "$base/hello.txt" > "$WORK/$label.head" || fail "$label: curl -I"
  etag="$(tr -d '\r' < "$WORK/$label.head" | awk 'tolower($1)=="etag:" {print $2}')"
  [[ -n "$etag" ]] || fail "$label: no ETag in curl -I: $(cat "$WORK/$label.head")"
  echo "$etag" > "$WORK/$label.etag"
  code="$(curl -s --http1.1 -o /dev/null -w '%{http_code}' -H "If-None-Match: $etag" "$base/hello.txt")"
  [[ "$code" == "304" ]] || fail "$label: If-None-Match $etag gave $code, want 304"
  curl -s --http1.1 -H 'Transfer-Encoding: chunked' --data-binary @"$WORK/upload.txt" "$base/api/echo" > "$WORK/$label.upload.json" || fail "$label: chunked upload"
  python3 - "$WORK/$label.upload.json" "$WORK/upload.txt" <<'PY' || fail "$label: chunked upload: $(cat "$WORK/$label.upload.json")"
import hashlib, json, sys
seen = json.load(open(sys.argv[1]))
want = hashlib.sha256(open(sys.argv[2], "rb").read()).hexdigest()
fields = {key.lower(): value for key, value in seen["headers"]}
assert seen["body_sha256"] == want, ("the upstream got different bytes", seen)
assert seen["framing"] == "length" and fields.get("content-length") == str(seen["body_length"]) and "transfer-encoding" not in fields, ("the upstream should get a Content-Length body", seen)
PY
  curl -s --http1.1 -o /dev/null -w '%{http_code} %{time_total}\n' --max-time 15 "$base/api/slow?ms=4000" > "$WORK/$label.slow" || true
  code="$(awk '{print $1}' "$WORK/$label.slow")"
  took="$(awk '{print $2}' "$WORK/$label.slow")"
  [[ "$code" == "504" ]] || fail "$label: slow upstream gave '$(cat "$WORK/$label.slow")', want 504"
  python3 -c 'import sys; sys.exit(0 if 1.5 <= float(sys.argv[1]) <= 3.5 else 1)' "$took" || fail "$label: 504 after ${took}s, want about 2s (the upstream read timeout)"
  echo "  $label: reuse 1 0, curl -r 0-7 'minihttp', ETag $etag, If-None-Match 304, chunked upload reaches the upstream with Content-Length, slow upstream 504 after ${took}s"
}

echo "▸ curl against minihttp: keep-alive reuse, range, conditional GET, chunked upload, upstream timeout"
curl_checks minihttp "$MINI"

echo "▸ minihttp probe against minihttp: 32 raw-socket cases"
python3 code/main.py probe --target "$MINI" "${FLAGS[@]}" --json > "$WORK/minihttp.json" 2> "$WORK/probe.err" || fail "probe against minihttp: $(cat "$WORK/probe.err")"
python3 - "$WORK/minihttp.json" <<'PY' || fail "probe against minihttp: $(cat "$WORK/minihttp.json" | head -c 4000)"
import json, sys
results = json.load(open(sys.argv[1]))
bad = [(r["case"], r["status"], r["expected"], r.get("error")) for r in results if r["verdict"] != "ok"]
assert len(results) == 32 and not bad, bad
print(f"  {len(results)} cases, all as designed")
PY

if ! command -v docker >/dev/null || ! docker info >/dev/null 2>&1; then
  echo "  (docker not available; nginx comparison skipped)"
  echo "SMOKE OK: networking/http-server"
  exit 0
fi

echo "▸ nginx.conf generated from the same flags, checked with nginx -t ($IMAGE)"
NPORT="$(other_port "$DEAD")"
python3 code/main.py config "${FLAGS[@]}" --nginx --nginx-root /srv/www --nginx-listen "127.0.0.1:$NPORT" > "$WORK/nginx.conf"
chmod 644 "$WORK/nginx.conf"
docker pull -q "$IMAGE" > "$WORK/pull.txt" 2>&1 || fail "docker pull $IMAGE: $(cat "$WORK/pull.txt")"
docker run --rm -e NGINX_ENTRYPOINT_QUIET_LOGS=1 -v "$WORK/nginx.conf:/etc/nginx/nginx.conf:ro" "$IMAGE" nginx -t > "$WORK/nginx-t.txt" 2>&1 || fail "nginx -t: $(cat "$WORK/nginx-t.txt")"
grep -q "syntax is ok" "$WORK/nginx-t.txt" || fail "nginx -t: $(cat "$WORK/nginx-t.txt")"
grep "nginx version\|syntax is ok" "$WORK/nginx-t.txt" | sed 's/^/  /' || true

echo "▸ nginx serving the same directory and proxying to the same upstream (--network host)"
docker rm -f "$NGINX" >/dev/null 2>&1 || true
CONTAINERS+=("$NGINX")
docker run -d --name "$NGINX" --network host -e NGINX_ENTRYPOINT_QUIET_LOGS=1 \
  -v "$WORK/www:/srv/www:ro" -v "$WORK/nginx.conf:/etc/nginx/nginx.conf:ro" "$IMAGE" > "$WORK/run.txt" 2>&1 || fail "docker run nginx: $(cat "$WORK/run.txt")"
NGX="127.0.0.1:$NPORT"
wait_for_http "http://$NGX/hello.txt" || fail "nginx never answered on $NGX: $(docker logs "$NGINX" 2>&1 | tail -20)"
docker exec "$NGINX" nginx -v > "$WORK/nginx-v.txt" 2>&1 || true
echo "  $(cat "$WORK/nginx-v.txt") on $NGX"

echo "▸ curl against nginx: the same checks"
curl_checks nginx "$NGX"
[[ "$(cat "$WORK/minihttp.etag")" == "$(cat "$WORK/nginx.etag")" ]] || fail "ETag differs for the same file: minihttp $(cat "$WORK/minihttp.etag"), nginx $(cat "$WORK/nginx.etag")"
code="$(curl -s --http1.1 -o /dev/null -w '%{http_code}' -H "If-None-Match: $(cat "$WORK/minihttp.etag")" "http://$NGX/hello.txt")"
[[ "$code" == "304" ]] || fail "nginx should accept minihttp's ETag as current, got $code"
echo "  one ETag for both servers: $(cat "$WORK/nginx.etag"); minihttp's validator revalidates against nginx"

echo "▸ the same 32 requests to nginx, compared case by case"
python3 code/main.py probe --target "$NGX" "${FLAGS[@]}" --json > "$WORK/nginx.json" 2> "$WORK/probe.err" || fail "probe against nginx: $(cat "$WORK/probe.err")"
python3 - "$WORK/minihttp.json" "$WORK/nginx.json" "$SIZE" <<'PY' || fail "minihttp and nginx disagree outside the documented differences"
import json, sys
mini = {r["case"]: r for r in json.load(open(sys.argv[1]))}
ngx = {r["case"]: r for r in json.load(open(sys.argv[2]))}
size = int(sys.argv[3])
header_timeout, keepalive_timeout, upstream_timeout = 2.0, 3.0, 2.0
NGINX_DIFFERS = {"ims-later": [200], "range-multi": [206], "slow-header": []}
problems = []


def check(ok, *what):
    if not ok:
        problems.append(what)


for name, result in mini.items():
    check(result["status"] == result["expected"], "minihttp", name, result["status"], result.get("error"))
    want = NGINX_DIFFERS.get(name, result["expected"])
    check(ngx[name]["status"] == want, "nginx", name, ngx[name]["status"], "want", want, ngx[name].get("error"))

for label, r in (("minihttp", mini), ("nginx", ngx)):
    k = r["keepalive"]
    check(k.get("connection", [""])[-1] == "close" and "close" not in k.get("connection", [])[:-1] and k.get("closed_after_last"), label, "keepalive", k)
    idle = r["idle-close"].get("idle_closed_after")
    check(idle is not None and keepalive_timeout - 0.5 <= idle <= keepalive_timeout + 2, label, "idle-close", idle)
    h = r["http10"]
    check(h.get("open_after_keep_alive") and h.get("closed_after_plain"), label, "http10", h)
    hd = r["head"]
    check((hd.get("head_content_length"), hd.get("head_body_length"), hd.get("get_body_length")) == (str(size), 0, size), label, "head", hd)
    check((r["range"].get("content_range"), r["range"].get("body")) == (f"bytes 0-4/{size}", "minih"), label, "range", r["range"])
    check(r["suffix-range"].get("content_range") == f"bytes {size - 5}-{size - 1}/{size}", label, "suffix-range", r["suffix-range"])
    check(r["range-416"].get("content_range") == f"bytes */{size}", label, "range-416", r["range-416"])
    check(r["if-range-stale"].get("body_length") == size, label, "if-range-stale", r["if-range-stale"])
    e = r["etag-304"]
    check(e.get("body_length") == 0 and e.get("content_length") == "" and e.get("etag"), label, "etag-304", e)
    check(r["dir-redirect"].get("location_path") == "/docs/", label, "dir-redirect", r["dir-redirect"])
    check(r["dir-index"].get("content_type") == "text/html", label, "dir-index", r["dir-index"])
    for name in ("both-lengths", "te-unknown", "http10-te", "bad-chunk", "too-large"):
        check(r[name].get("closed") and r[name].get("responses") == 1, label, name, r[name])
    up = r["chunked-upload"]
    check(up.get("upstream_sha256") == up.get("sent_sha256") and up.get("upstream_framing") == "length" and up.get("upstream_transfer_encoding") == "", label, "chunked-upload", up)
    ex = r["expect-continue"]
    check((ex.get("interim"), ex.get("upstream_body_length"), ex.get("upstream_expect")) == ([100], 11, ""), label, "expect-continue", ex)
    f = r["forwarding"]
    check((f.get("host"), f.get("x_forwarded_for"), f.get("keep_alive"), f.get("te"), f.get("x_end_to_end")) == ("probe.test", "203.0.113.7, 127.0.0.1", "", "", "kept"), label, "forwarding", f)
    check(r["upstream-chunked"].get("body") == "alpha beta gamma\n", label, "upstream-chunked", r["upstream-chunked"])
    slow = r["slow-upstream"].get("answered_after") or 0
    check(upstream_timeout - 0.3 <= slow <= upstream_timeout + 1.5, label, "slow-upstream", r["slow-upstream"])
    sh = r["slow-header"]
    check(sh.get("closed_after") is not None and header_timeout - 0.3 <= sh["closed_after"] <= header_timeout + 1.5 and sh.get("drips", 0) >= 3, label, "slow-header", sh)

check(mini["forwarding"].get("via") == "1.0 edge, 1.1 minihttp" and mini["forwarding"].get("x_hop") == "", "minihttp removes fields named in Connection", mini["forwarding"])
check(ngx["forwarding"].get("via") == "1.0 edge, 1.1 nginx" and ngx["forwarding"].get("x_hop") == "secret", "nginx forwards fields named in Connection", ngx["forwarding"])
check(ngx["range-multi"].get("content_type") == "multipart/byteranges", "nginx range-multi", ngx["range-multi"])
check(mini["slow-header"]["status"] == [408], "minihttp slow-header", mini["slow-header"])
for case in ("etag-304", "range", "ims-exact"):
    check(mini[case].get("etag") == ngx[case].get("etag") and mini[case].get("last_modified") == ngx[case].get("last_modified"), "validators", case, mini[case].get("etag"), ngx[case].get("etag"))
check(mini["range"].get("body_sha256") == ngx["range"].get("body_sha256"), "range bytes", mini["range"], ngx["range"])

if problems:
    for problem in problems:
        print("  mismatch:", *problem)
    sys.exit(1)
same = sum(1 for name in mini if mini[name]["status"] == ngx[name]["status"])
print(f"  {same} of {len(mini)} cases answer with the same status; documented differences:")
for name in NGINX_DIFFERS:
    print(f"    {name:<12} minihttp {mini[name]['status']}  nginx {ngx[name]['status']}")
print(f"    forwarding   X-Hop named in Connection: minihttp removes it, nginx forwards {ngx['forwarding']['x_hop']!r}")
print(f"  slow-header closed after {mini['slow-header']['closed_after']}s (minihttp, 408) and {ngx['slow-header']['closed_after']}s (nginx, no response); slow upstream 504 after {mini['slow-upstream']['answered_after']}s and {ngx['slow-upstream']['answered_after']}s")
PY

echo "▸ access logs: one combined format, one parser for both servers; nginx's error log names each cause"
docker logs "$NGINX" > "$WORK/nginx.access" 2> "$WORK/nginx.error" || true
python3 code/main.py logstat "$WORK/minihttp.access" --json > "$WORK/minihttp.stats"
python3 code/main.py logstat "$WORK/nginx.access" --json > "$WORK/nginx.stats"
python3 - "$WORK/minihttp.stats" "$WORK/nginx.stats" <<'PY' || fail "access logs: $(tail -5 "$WORK/nginx.access")"
import json, sys
want = {"200", "206", "301", "304", "400", "404", "405", "408", "413", "416", "501", "502", "504"}
for label, path in (("minihttp", sys.argv[1]), ("nginx", sys.argv[2])):
    stats = json.load(open(path))
    missing = want - set(stats["status"])
    assert not missing, (label, "statuses missing from the access log", sorted(missing), stats["status"])
    print(f"  {label:<8} {stats['parsed']} combined-format lines; " + " ".join(f"{k}x{v}" for k, v in stats["status"].items()))
PY
for token in 'client timed out' '"Content-Length" and "Transfer-Encoding" headers at the same time' 'upstream timed out' 'connect() failed' 'client intended to send too large body'; do
  grep -qF "$token" "$WORK/nginx.error" || fail "nginx error log has no '$token': $(tail -20 "$WORK/nginx.error")"
done
echo "  nginx error log: client timed out, Content-Length and Transfer-Encoding at the same time, upstream timed out, connect() failed, too large body"

curl -V > "$WORK/curl-version.txt" 2>&1 || true
if grep -q 'HTTP2' "$WORK/curl-version.txt"; then
  echo "▸ HTTP/2 over cleartext with prior knowledge: new framing, the same semantics"
  H2PORT="$(other_port "$DEAD")"
  cat > "$WORK/h2.conf" <<EOF
worker_processes 1;
pid /tmp/nginx.pid;
error_log /var/log/nginx/error.log notice;
events {
    worker_connections 64;
}
http {
    include /etc/nginx/mime.types;
    access_log /var/log/nginx/access.log combined;
    server {
        listen 127.0.0.1:$H2PORT;
        http2 on;
        root /srv/www;
    }
}
EOF
  chmod 644 "$WORK/h2.conf"
  docker rm -f "$H2" >/dev/null 2>&1 || true
  CONTAINERS+=("$H2")
  docker run -d --name "$H2" --network host -e NGINX_ENTRYPOINT_QUIET_LOGS=1 \
    -v "$WORK/www:/srv/www:ro" -v "$WORK/h2.conf:/etc/nginx/nginx.conf:ro" "$IMAGE" > "$WORK/run-h2.txt" 2>&1 || fail "docker run h2c nginx: $(cat "$WORK/run-h2.txt")"
  wait_for_http --http2-prior-knowledge "http://127.0.0.1:$H2PORT/hello.txt" || fail "h2c nginx never answered: $(docker logs "$H2" 2>&1 | tail -20)"
  version="$(curl -s --http2-prior-knowledge -o /dev/null -w '%{http_version} %{http_code}' "http://127.0.0.1:$H2PORT/hello.txt")"
  [[ "$version" == "2 200" ]] || fail "HTTP/2 prior knowledge gave '$version', want '2 200'"
  range="$(curl -s --http2-prior-knowledge -r 0-7 "http://127.0.0.1:$H2PORT/hello.txt")"
  [[ "$range" == "minihttp" ]] || fail "HTTP/2 range gave '$range'"
  curl -s --http2-prior-knowledge -I "http://127.0.0.1:$H2PORT/hello.txt" > "$WORK/h2.head" || fail "HTTP/2 HEAD"
  h2etag="$(tr -d '\r' < "$WORK/h2.head" | awk 'tolower($1)=="etag:" {print $2}')"
  [[ "$h2etag" == "$(cat "$WORK/minihttp.etag")" ]] || fail "HTTP/2 ETag '$h2etag' differs from HTTP/1.1 '$(cat "$WORK/minihttp.etag")'"
  code="$(curl -s --http2-prior-knowledge -o /dev/null -w '%{http_code}' -H "If-None-Match: $h2etag" "http://127.0.0.1:$H2PORT/hello.txt")"
  [[ "$code" == "304" ]] || fail "HTTP/2 conditional GET gave $code"
  echo "  HTTP/2: version 2, 206 'minihttp', ETag $h2etag, If-None-Match 304: the same semantics in binary frames"
else
  echo "  (curl without HTTP/2 support; HTTP/2 check skipped)"
fi

echo "SMOKE OK: networking/http-server"
