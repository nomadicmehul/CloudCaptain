#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."

fail() { echo "SMOKE FAIL: $*" >&2; exit 1; }
WORK="$(mktemp -d)"
HAPROXY_IMAGE="haproxy:3.4.7-alpine"
NGINX_IMAGE="nginx:1.30.6-alpine"
PYTHON_IMAGE="python:3.12-alpine"
HAP="cc-load-balancing-haproxy"
NGX="cc-load-balancing-nginx"
NS="cc-load-balancing"
CTX="kind-cc-smoke"
PIDS=()
CONTAINERS=()
NS_CREATED=""

cleanup() {
  set +e
  for pid in ${PIDS[@]+"${PIDS[@]}"}; do kill "$pid" 2>/dev/null; wait "$pid" 2>/dev/null; done
  for c in ${CONTAINERS[@]+"${CONTAINERS[@]}"}; do docker rm -f "$c" >/dev/null 2>&1; done
  if [[ -n "$NS_CREATED" ]]; then kubectl --context "$CTX" delete namespace "$NS" --ignore-not-found --wait=false >/dev/null 2>&1; fi
  rm -rf "$WORK"
}
trap cleanup EXIT

cat > "$WORK/probe.py" <<'PY'
import collections
import csv
import json
import socket
import sys
import threading
import time
import urllib.error
import urllib.request

OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def get(url, headers=None, method="GET", data=None):
    request = urllib.request.Request(url, headers=headers or {}, method=method, data=data)
    try:
        with OPENER.open(request, timeout=10) as response:
            status, raw, hdrs = response.status, response.read(), dict(response.getheaders())
    except urllib.error.HTTPError as err:
        status, raw, hdrs = err.code, err.read(), dict(err.headers.items())
    try:
        backend = json.loads(raw).get("backend") or "-"
    except ValueError:
        backend = "-"
    return status, backend, hdrs


def tally(results):
    return {
        "backends": dict(collections.Counter(b for _, b in results)),
        "statuses": dict(collections.Counter(str(s) for s, _ in results)),
        "errors": dict(collections.Counter(b for s, b in results if s >= 500)),
        "total": len(results),
    }


def show_stat(host, port):
    text = runtime(host, port, "show stat")
    lines = [l for l in text.splitlines() if l.strip()]
    header = lines[0].lstrip("# ").split(",")
    return list(csv.DictReader(lines[1:], fieldnames=header))


def runtime(host, port, command):
    with socket.create_connection((host, int(port)), timeout=5) as conn:
        conn.sendall(command.encode() + b"\n")
        chunks = []
        while True:
            data = conn.recv(65536)
            if not data:
                break
            chunks.append(data)
    return b"".join(chunks).decode()


cmd, args = sys.argv[1], sys.argv[2:]
if cmd == "wait-url":
    deadline = time.time() + float(args[1])
    while True:
        try:
            get(args[0])
            break
        except OSError as exc:
            if time.time() > deadline:
                raise SystemExit(f"nothing answered at {args[0]}: {exc}")
            time.sleep(0.1)
elif cmd == "seq":
    print(json.dumps(tally([get(args[0])[:2] for _ in range(int(args[1]))])))
elif cmd == "conc":
    url, total, workers = args[0], int(args[1]), int(args[2])
    results, lock = [], threading.Lock()

    def work():
        for _ in range(total // workers):
            try:
                result = get(url)[:2]
            except OSError as exc:
                result = (0, f"error: {exc}")
            with lock:
                results.append(result)

    threads = [threading.Thread(target=work) for _ in range(workers)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    print(json.dumps(tally(results)))
elif cmd == "keys":
    mapping = {}
    for i in range(int(args[1])):
        key = f"user-{i}"
        first, second = get(args[0], {"X-User": key}), get(args[0], {"X-User": key})
        if first[0] != 200 or second[0] != 200 or first[1] != second[1]:
            raise SystemExit(f"{key}: {first[:2]} then {second[:2]}; a hash key must stay on one healthy backend")
        mapping[key] = first[1]
    print(json.dumps(mapping, sort_keys=True))
elif cmd == "key-wait":
    url, key, op, backend, timeout = args[0], args[1], args[2], args[3], float(args[4])
    deadline = time.time() + timeout
    while True:
        status, seen, _ = get(url, {"X-User": key})
        if status == 200 and ((op == "is") == (seen == backend)):
            break
        if time.time() > deadline:
            raise SystemExit(f"{key} on {seen} (HTTP {status}); wanted it {op} {backend}")
        time.sleep(0.1)
elif cmd == "until-gone":
    url, backend, timeout = args[0], args[1], float(args[2])
    deadline, streak, errors, total = time.time() + timeout, 0, 0, 0
    while streak < 30:
        if time.time() > deadline:
            raise SystemExit(f"{backend} still served after {timeout}s ({total} requests, {errors} errors from it)")
        status, seen, _ = get(url)
        total += 1
        if seen == backend:
            streak = 0
            errors += 1 if status >= 500 else 0
        else:
            streak += 1
    print(json.dumps({"errors": errors, "requests": total}))
elif cmd == "until-back":
    url, backend, timeout = args[0], args[1], float(args[2])
    deadline = time.time() + timeout
    while get(url)[:2] != (200, backend):
        if time.time() > deadline:
            raise SystemExit(f"{backend} never served again within {timeout}s")
        time.sleep(0.05)
elif cmd == "minilb-wait":
    url, backend, states, timeout = args[0], args[1], args[2].split(","), float(args[3])
    deadline = time.time() + timeout
    while True:
        report = json.loads(OPENER.open(url + "/_minilb/status", timeout=5).read())
        row = next((b for b in report["backends"] if b["name"] == backend), None)
        state = row["state"] if row else "absent"
        if state in states:
            print(json.dumps({"backend": backend, "state": state, "events": [e for e in report["events"] if e["backend"] == backend]}))
            break
        if time.time() > deadline:
            raise SystemExit(f"minilb {url}: {backend} is {state}, wanted {states}: {report['events'][-5:]}")
        time.sleep(0.05)
elif cmd == "hap":
    print(runtime(args[0], args[1], args[2]), end="")
elif cmd == "hap-wait":
    host, port, proxy, server, prefix, timeout = args[0], args[1], args[2], args[3], args[4], float(args[5])
    deadline = time.time() + timeout
    while True:
        row = next((r for r in show_stat(host, port) if r["pxname"] == proxy and r["svname"] == server), None)
        if row and row["status"].startswith(prefix):
            print(json.dumps({k: row.get(k) for k in ("pxname", "svname", "status", "check_status", "check_code", "chkfail", "chkdown", "lbtot")}))
            break
        if time.time() > deadline:
            raise SystemExit(f"haproxy {proxy}/{server} is {row and row['status']}, wanted {prefix}: {row}")
        time.sleep(0.1)
elif cmd == "stats":
    print(OPENER.open(args[0] + "/_backend/stats", timeout=5).read().decode())
elif cmd == "mode":
    print(OPENER.open(urllib.request.Request(args[0] + "/_backend/mode?status=" + args[1], method="POST", data=b""), timeout=5).read().decode())
else:
    raise SystemExit(f"unknown probe command {cmd}")
PY
probe() { python3 "$WORK/probe.py" "$@"; }
backend_of() { python3 -c 'import json,sys; print(json.load(open(sys.argv[1])).get("backend", "-"))' "$1"; }
check() {
  python3 - "$1" "$2" <<'PY' || fail "$3: $(cat "$1")"
import json, sys
d = json.load(open(sys.argv[1]))
if not eval(sys.argv[2], {"d": d}):
    raise SystemExit(1)
PY
}

echo "▸ minilb without traffic: nginx's smooth weighted sequence, three hashing schemes, the latency simulator"
python3 code/main.py pick --algorithm weighted_round_robin --backends a:5,b:1,c:1 --count 7 > "$WORK/wrr.txt"
[[ "$(head -n 1 "$WORK/wrr.txt")" == "a a b a c a a" ]] || fail "smooth weighted round robin: $(cat "$WORK/wrr.txt")"
python3 code/main.py remap --backends a,b,c,d --remove d --keys 5000 --json > "$WORK/remap.json"
check "$WORK/remap.json" '{r["algorithm"]: r for r in d["results"]}["ring_hash"]["moved_from_kept"] == 0' "ring hash must move only the removed backend's keys"
check "$WORK/remap.json" '{r["algorithm"]: r for r in d["results"]}["modulo_hash"]["moved"] > 0.6' "modulo hashing should reshuffle most keys"
check "$WORK/remap.json" '{r["algorithm"]: r for r in d["results"]}["maglev"]["moved_from_kept"] < 0.02' "Maglev should disturb almost no kept keys"
python3 code/main.py remap --backends a,b,c,d --remove d --keys 5000
python3 code/main.py simulate --requests 3000 --json > "$WORK/sim.json"
check "$WORK/sim.json" 'd[0]["latency_ms"]["p99"] > 5 * d[1]["latency_ms"]["p99"] and d[1]["share"]["slow"] < 0.25' "least_conn should cut round robin's p99 with a slow backend"
python3 code/main.py simulate --requests 3000

read -r P1 P2 P3 M_RR M_LC M_HASH H_RR H_LC H_HASH H_ADMIN N_RR N_LC N_PASSIVE N_HASH < <(python3 -c '
import socket
socks = [socket.socket() for _ in range(14)]
for s in socks:
    s.bind(("127.0.0.1", 0))
print(" ".join(str(s.getsockname()[1]) for s in socks))
for s in socks:
    s.close()
')
B1="http://127.0.0.1:$P1"; B2="http://127.0.0.1:$P2"; B3="http://127.0.0.1:$P3"

echo "▸ three backends: b1 fast, b2 slow (50 ms), b3 fast and about to fail"
python3 code/main.py backend --name b1 --port "$P1" > "$WORK/b1.out" 2>&1 &
PIDS+=("$!")
python3 code/main.py backend --name b2 --port "$P2" --delay 0.05 > "$WORK/b2.out" 2>&1 &
PIDS+=("$!")
python3 code/main.py backend --name b3 --port "$P3" > "$WORK/b3.out" 2>&1 &
PIDS+=("$!")
for url in "$B1" "$B2" "$B3"; do probe wait-url "$url/health" 10 || fail "backend $url did not start: $(cat "$WORK"/b*.out)"; done

echo "▸ minilb: round robin with a sticky cookie, least_conn, ring hash on X-User; checks every 200 ms, rise 2, fall 3, eject after 3 consecutive 5xx"
COMMON=(--backend "b1=127.0.0.1:$P1" --backend "b2=127.0.0.1:$P2" --backend "b3=127.0.0.1:$P3" --check-interval 0.2 --check-timeout 0.5 --rise 2 --fall 3 --consecutive-5xx 3 --base-ejection 1 --max-ejection-percent 34 --retries 0)
python3 code/main.py serve --listen "127.0.0.1:$M_RR" "${COMMON[@]}" --algorithm round_robin --sticky-cookie MINILB > "$WORK/minilb-rr.out" 2> "$WORK/minilb-rr.err" &
PIDS+=("$!")
python3 code/main.py serve --listen "127.0.0.1:$M_LC" "${COMMON[@]}" --algorithm least_conn > "$WORK/minilb-lc.out" 2> "$WORK/minilb-lc.err" &
PIDS+=("$!")
python3 code/main.py serve --listen "127.0.0.1:$M_HASH" "${COMMON[@]}" --algorithm ring_hash --hash-on header:X-User > "$WORK/minilb-hash.out" 2> "$WORK/minilb-hash.err" &
PIDS+=("$!")
for port in "$M_RR" "$M_LC" "$M_HASH"; do probe wait-url "http://127.0.0.1:$port/_minilb/status" 10 || fail "minilb on $port did not start: $(cat "$WORK"/minilb-*.err)"; done
python3 code/main.py serve --listen "127.0.0.1:$M_RR" "${COMMON[@]}" --print-config > "$WORK/config.json"
check "$WORK/config.json" 'd["health"]["fall"] == 3 and d["outlier"]["consecutive_5xx"] == 3 and len(d["backends"]) == 3' "--print-config"

LBS=("minilb=http://127.0.0.1:$M_RR")
LC_LBS=("minilb=http://127.0.0.1:$M_LC")
HASH_LBS=("minilb=http://127.0.0.1:$M_HASH")
HAVE_HAP=""
HAVE_NGX=""
if [[ "$(uname -s)" == "Linux" ]] && command -v docker >/dev/null && docker info >/dev/null 2>&1; then
  echo "▸ HAProxy $HAPROXY_IMAGE with the equivalent configuration (host network, runtime API on $H_ADMIN)"
  mkdir -p "$WORK/haproxy"
  chmod 755 "$WORK/haproxy"
  servers="    server b1 127.0.0.1:$P1 check
    server b2 127.0.0.1:$P2 check
    server b3 127.0.0.1:$P3 check"
  cat > "$WORK/haproxy/haproxy.cfg" <<EOF
global
    log stdout format raw local0 info
    stats socket ipv4@127.0.0.1:$H_ADMIN level admin

defaults
    mode http
    log global
    option httplog
    timeout connect 1s
    timeout client 10s
    timeout server 10s
    timeout check 500ms
    default-server inter 200ms rise 2 fall 3 observe layer7 error-limit 3 on-error mark-down

frontend fe_rr
    bind 127.0.0.1:$H_RR
    default_backend be_rr

frontend fe_lc
    bind 127.0.0.1:$H_LC
    default_backend be_lc

frontend fe_hash
    bind 127.0.0.1:$H_HASH
    default_backend be_hash

backend be_rr
    balance roundrobin
    option httpchk
    http-check send meth GET uri /health
$servers

backend be_lc
    balance leastconn
    option httpchk
    http-check send meth GET uri /health
$servers

backend be_hash
    balance hdr(X-User)
    hash-type consistent
    option httpchk
    http-check send meth GET uri /health
$servers
EOF
  chmod 644 "$WORK/haproxy/haproxy.cfg"
  docker pull -q "$HAPROXY_IMAGE" > /dev/null || fail "docker pull $HAPROXY_IMAGE"
  docker run --rm -v "$WORK/haproxy:/usr/local/etc/haproxy:ro" "$HAPROXY_IMAGE" haproxy -c -f /usr/local/etc/haproxy/haproxy.cfg > "$WORK/haproxy-check.txt" 2>&1 || fail "haproxy -c: $(cat "$WORK/haproxy-check.txt")"
  docker rm -f "$HAP" > /dev/null 2>&1 || true
  CONTAINERS+=("$HAP")
  docker run -d --name "$HAP" --network host -v "$WORK/haproxy:/usr/local/etc/haproxy:ro" "$HAPROXY_IMAGE" > /dev/null || fail "docker run haproxy"
  for port in "$H_RR" "$H_LC" "$H_HASH"; do probe wait-url "http://127.0.0.1:$port/" 20 || fail "haproxy did not listen on $port: $(docker logs --tail 30 "$HAP" 2>&1)"; done
  probe hap-wait 127.0.0.1 "$H_ADMIN" be_rr b3 UP 10 > /dev/null || fail "haproxy runtime API: $(docker logs --tail 30 "$HAP" 2>&1)"
  LBS+=("haproxy=http://127.0.0.1:$H_RR")
  LC_LBS+=("haproxy=http://127.0.0.1:$H_LC")
  HASH_LBS+=("haproxy=http://127.0.0.1:$H_HASH")
  HAVE_HAP=1

  echo "▸ nginx $NGINX_IMAGE: upstream blocks with round robin, least_conn, max_fails, and hash \$http_x_user consistent"
  mkdir -p "$WORK/nginx"
  chmod 755 "$WORK/nginx"
  write_nginx() {
    cat > "$WORK/nginx/nginx.conf" <<EOF
worker_processes 1;
pid /tmp/nginx.pid;
error_log /dev/stderr warn;
events { worker_connections 1024; }
http {
    access_log off;
    upstream rr { zone rr 64k; server 127.0.0.1:$P1; server 127.0.0.1:$P2; server 127.0.0.1:$P3; }
    upstream lc { zone lc 64k; least_conn; server 127.0.0.1:$P1; server 127.0.0.1:$P2; server 127.0.0.1:$P3; }
    upstream passive { zone passive 64k; server 127.0.0.1:$P1 max_fails=3 fail_timeout=30s; server 127.0.0.1:$P2 max_fails=3 fail_timeout=30s; server 127.0.0.1:$P3 max_fails=3 fail_timeout=30s; }
    upstream byuser { zone byuser 64k; hash \$http_x_user consistent; server 127.0.0.1:$P1; server 127.0.0.1:$P2; server 127.0.0.1:$P3$1; }
    server { listen 127.0.0.1:$N_RR; location / { proxy_pass http://rr; } }
    server { listen 127.0.0.1:$N_LC; location / { proxy_pass http://lc; } }
    server { listen 127.0.0.1:$N_PASSIVE; location / { proxy_pass http://passive; proxy_next_upstream error timeout http_500; } }
    server { listen 127.0.0.1:$N_HASH; location / { proxy_pass http://byuser; } }
}
EOF
    chmod 644 "$WORK/nginx/nginx.conf"
  }
  write_nginx ""
  docker pull -q "$NGINX_IMAGE" > /dev/null || fail "docker pull $NGINX_IMAGE"
  docker rm -f "$NGX" > /dev/null 2>&1 || true
  CONTAINERS+=("$NGX")
  docker run -d --name "$NGX" --network host -v "$WORK/nginx:/etc/nginx/cc:ro" "$NGINX_IMAGE" nginx -c /etc/nginx/cc/nginx.conf -g 'daemon off;' > /dev/null || fail "docker run nginx"
  for port in "$N_RR" "$N_LC" "$N_PASSIVE" "$N_HASH"; do probe wait-url "http://127.0.0.1:$port/" 20 || fail "nginx did not listen on $port: $(docker logs --tail 30 "$NGX" 2>&1)"; done
  docker exec "$NGX" nginx -c /etc/nginx/cc/nginx.conf -t > "$WORK/nginx-t.txt" 2>&1 || fail "nginx -t: $(cat "$WORK/nginx-t.txt")"
  LBS+=("nginx=http://127.0.0.1:$N_RR")
  LC_LBS+=("nginx=http://127.0.0.1:$N_LC")
  HASH_LBS+=("nginx=http://127.0.0.1:$N_HASH")
  HAVE_NGX=1
else
  echo "  (Docker on Linux not available; HAProxy and nginx comparisons skipped)"
fi
nginx_reload() {
  write_nginx "$1"
  docker exec "$NGX" nginx -c /etc/nginx/cc/nginx.conf -s reload > "$WORK/nginx-reload.txt" 2>&1 || fail "nginx -s reload: $(cat "$WORK/nginx-reload.txt")"
}

echo "▸ round robin: 90 sequential requests split 30/30/30"
for entry in "${LBS[@]}"; do
  name="${entry%%=*}"; url="${entry#*=}"
  probe seq "$url/" 90 > "$WORK/rr-$name.json" || fail "$name: $(cat "$WORK/rr-$name.json")"
  if [[ "$name" == "minilb" ]]; then
    check "$WORK/rr-$name.json" 'd["backends"] == {"b1": 30, "b2": 30, "b3": 30}' "$name round robin should be exactly even"
  else
    check "$WORK/rr-$name.json" 'set(d["backends"]) == {"b1", "b2", "b3"} and max(d["backends"].values()) - min(d["backends"].values()) <= 2' "$name round robin should be even"
  fi
  echo "  $name: $(cat "$WORK/rr-$name.json")"
done

echo "▸ 8 concurrent clients, 200 requests: round robin gives the slow b2 a third, least connections much less"
for entry in "${LBS[@]}"; do
  name="${entry%%=*}"; url="${entry#*=}"
  probe conc "$url/" 200 8 > "$WORK/conc-rr-$name.json" || fail "$name: $(cat "$WORK/conc-rr-$name.json")"
  check "$WORK/conc-rr-$name.json" 'd["statuses"] == {"200": 200} and 0.29 <= d["backends"].get("b2", 0) / 200 <= 0.38' "$name round robin share of b2"
  echo "  $name round robin: $(cat "$WORK/conc-rr-$name.json")"
done
for entry in "${LC_LBS[@]}"; do
  name="${entry%%=*}"; url="${entry#*=}"
  probe conc "$url/" 200 8 > "$WORK/conc-lc-$name.json" || fail "$name: $(cat "$WORK/conc-lc-$name.json")"
  check "$WORK/conc-lc-$name.json" 'd["statuses"] == {"200": 200} and d["backends"].get("b2", 0) / 200 < 0.25' "$name least connections should starve the slow backend"
  echo "  $name least_conn: $(cat "$WORK/conc-lc-$name.json")"
done

echo "▸ consistent hashing: every X-User key lands on one backend, twice in a row"
for entry in "${HASH_LBS[@]}"; do
  name="${entry%%=*}"; url="${entry#*=}"
  probe keys "$url/" 40 > "$WORK/keys-$name.json" || fail "$name: $(cat "$WORK/keys-$name.json")"
  check "$WORK/keys-$name.json" 'len(set(d.values())) >= 2 and "b3" in d.values()' "$name should spread 40 keys and give b3 some"
done

echo "▸ b3 starts answering 500 (requests and /health)"
probe mode "$B3" 500 > /dev/null

echo "▸ minilb round robin: passive ejection after 3 consecutive 5xx, then active checks keep b3 out"
probe until-gone "http://127.0.0.1:$M_RR/" b3 15 > "$WORK/gone-minilb.json" || fail "minilb kept sending to b3: $(cat "$WORK/gone-minilb.json") $(tail -5 "$WORK/minilb-rr.err")"
check "$WORK/gone-minilb.json" '1 <= d["errors"] <= 3' "minilb should let at most 3 errors through"
probe minilb-wait "http://127.0.0.1:$M_RR" b3 unhealthy 10 > "$WORK/minilb-b3.json" || fail "minilb b3 state: $(cat "$WORK/minilb-b3.json")"
python3 - "$WORK/minilb-b3.json" "$WORK/gone-minilb.json" <<'PY' || fail "minilb events for b3: $(cat "$WORK/minilb-b3.json")"
import json, sys
events, errors = json.load(open(sys.argv[1]))["events"], json.load(open(sys.argv[2]))["errors"]
ejected = any(e["event"] == "ejected" and e["reason"].startswith("3 consecutive 5xx") for e in events)
down = any(e["event"] == "down" and "3 consecutive failed checks (last: HTTP 500)" in e["reason"] for e in events)
raise SystemExit(0 if down and (ejected or errors < 3) else 1)
PY
grep b3 "$WORK/minilb-rr.err" > "$WORK/minilb-b3-events.txt" || true
echo "  minilb: $(cat "$WORK/gone-minilb.json"); $(head -n 2 "$WORK/minilb-b3-events.txt" | tr '\n' ' ')"

if [[ -n "$HAVE_HAP" ]]; then
  echo "▸ HAProxy be_rr: observe layer7 error-limit 3 on-error mark-down, then rise 2 checks bring it back"
  probe until-gone "http://127.0.0.1:$H_RR/" b3 15 > "$WORK/gone-haproxy.json" || fail "haproxy kept sending to b3: $(cat "$WORK/gone-haproxy.json") $(docker logs --tail 20 "$HAP" 2>&1)"
  check "$WORK/gone-haproxy.json" 'd["errors"] <= 3' "haproxy should let at most error-limit errors through"
  probe hap-wait 127.0.0.1 "$H_ADMIN" be_rr b3 DOWN 10 > "$WORK/hap-down.json" || fail "haproxy be_rr/b3 never DOWN: $(docker logs --tail 20 "$HAP" 2>&1)"
  echo "  haproxy: $(cat "$WORK/gone-haproxy.json"); stats $(cat "$WORK/hap-down.json")"
fi

if [[ -n "$HAVE_NGX" ]]; then
  echo "▸ nginx with defaults has no active checks and passes 500s through; max_fails plus proxy_next_upstream http_500 hides them"
  probe seq "http://127.0.0.1:$N_RR/" 30 > "$WORK/ngx-rr-fail.json" || fail "nginx rr during the failure"
  check "$WORK/ngx-rr-fail.json" 'd["errors"].get("b3", 0) >= 8' "nginx rr should keep sending to b3"
  before="$(probe stats "$B3")" || fail "b3 stats"
  probe seq "http://127.0.0.1:$N_PASSIVE/" 30 > "$WORK/ngx-passive.json" || fail "nginx passive during the failure"
  after="$(probe stats "$B3")" || fail "b3 stats"
  check "$WORK/ngx-passive.json" 'd["statuses"] == {"200": 30}' "nginx passive: GETs retried on the next server"
  python3 - "$before" "$after" <<'PY' || fail "nginx max_fails=3 should stop sending to b3 after 3 failures: before $before after $after"
import json, sys
delta = json.loads(sys.argv[2])["requests"] - json.loads(sys.argv[1])["requests"]
print(f"  nginx passive: b3 received {delta} of 30 requests, the client saw none of its 500s")
raise SystemExit(0 if 1 <= delta <= 3 else 1)
PY
  echo "  nginx defaults: $(cat "$WORK/ngx-rr-fail.json")"
fi

echo "▸ b3 out of every hash ring: keys on b1 and b2 stay put, b3's keys move"
probe minilb-wait "http://127.0.0.1:$M_HASH" b3 unhealthy 10 > /dev/null || fail "minilb hash instance never took b3 out"
probe keys "http://127.0.0.1:$M_HASH/" 40 > "$WORK/keys-minilb-down.json" || fail "minilb keys"
if [[ -n "$HAVE_HAP" ]]; then
  probe hap-wait 127.0.0.1 "$H_ADMIN" be_hash b3 DOWN 10 > /dev/null || fail "haproxy be_hash/b3 never DOWN"
  probe keys "http://127.0.0.1:$H_HASH/" 40 > "$WORK/keys-haproxy-down.json" || fail "haproxy keys"
fi
if [[ -n "$HAVE_NGX" ]]; then
  B3_KEY="$(python3 -c 'import json,sys; print(next(k for k, v in json.load(open(sys.argv[1])).items() if v == "b3"))' "$WORK/keys-nginx.json")"
  nginx_reload " down"
  probe key-wait "http://127.0.0.1:$N_HASH/" "$B3_KEY" not b3 15 || fail "nginx reload with b3 down"
  probe keys "http://127.0.0.1:$N_HASH/" 40 > "$WORK/keys-nginx-down.json" || fail "nginx keys"
fi
for entry in "${HASH_LBS[@]}"; do
  name="${entry%%=*}"
  python3 - "$WORK/keys-$name.json" "$WORK/keys-$name-down.json" "$name" <<'PY' || fail "$name: consistent hashing moved keys whose backend stayed"
import json, sys
before, after, name = json.load(open(sys.argv[1])), json.load(open(sys.argv[2])), sys.argv[3]
kept = [k for k, v in before.items() if v != "b3"]
moved = [k for k, v in before.items() if v == "b3"]
assert all(after[k] == before[k] for k in kept), {k: (before[k], after[k]) for k in kept if after[k] != before[k]}
assert all(after[k] in ("b1", "b2") for k in moved), {k: after[k] for k in moved}
print(f"  {name}: {len(kept)} keys stayed, {len(moved)} keys of b3 moved to b1/b2")
PY
done

echo "▸ b3 recovers: rise 2 checks bring it back everywhere, and its keys come home"
probe mode "$B3" 200 > /dev/null
probe minilb-wait "http://127.0.0.1:$M_RR" b3 healthy 15 > /dev/null || fail "minilb rr: b3 never healthy again: $(tail -5 "$WORK/minilb-rr.err")"
probe minilb-wait "http://127.0.0.1:$M_HASH" b3 healthy 15 > /dev/null || fail "minilb hash: b3 never healthy again"
grep -q "b3 up: 2 consecutive passed checks" "$WORK/minilb-rr.err" || fail "minilb should log the up transition: $(cat "$WORK/minilb-rr.err")"
if [[ -n "$HAVE_HAP" ]]; then
  probe hap-wait 127.0.0.1 "$H_ADMIN" be_rr b3 UP 15 > /dev/null || fail "haproxy be_rr/b3 never UP again"
  probe hap-wait 127.0.0.1 "$H_ADMIN" be_hash b3 UP 15 > /dev/null || fail "haproxy be_hash/b3 never UP again"
fi
if [[ -n "$HAVE_NGX" ]]; then
  nginx_reload ""
  probe key-wait "http://127.0.0.1:$N_HASH/" "$B3_KEY" is b3 15 || fail "nginx reload with b3 back"
fi
for entry in "${HASH_LBS[@]}"; do
  name="${entry%%=*}"; url="${entry#*=}"
  probe keys "$url/" 40 > "$WORK/keys-$name-back.json" || fail "$name keys after recovery"
  python3 -c 'import json,sys; raise SystemExit(0 if json.load(open(sys.argv[1])) == json.load(open(sys.argv[2])) else 1)' "$WORK/keys-$name.json" "$WORK/keys-$name-back.json" || fail "$name: keys should map exactly as before the failure"
done
for entry in "${LBS[@]}"; do
  name="${entry%%=*}"; url="${entry#*=}"
  [[ "$name" == "nginx" ]] && continue
  probe seq "$url/" 90 > "$WORK/rr-back-$name.json" || fail "$name after recovery"
  check "$WORK/rr-back-$name.json" 'd["backends"].get("b3", 0) >= 28 and d["statuses"] == {"200": 90}' "$name should send b3 its share again"
  echo "  $name after recovery: $(cat "$WORK/rr-back-$name.json")"
done

if [[ -n "$HAVE_HAP" ]]; then
  echo "▸ HAProxy runtime API: set server be_rr/b2 state drain, then ready"
  probe hap 127.0.0.1 "$H_ADMIN" "set server be_rr/b2 state drain" > "$WORK/hap-drain.txt"
  probe seq "http://127.0.0.1:$H_RR/" 30 > "$WORK/hap-drained.json" || fail "haproxy during drain"
  check "$WORK/hap-drained.json" '"b2" not in d["backends"] and d["statuses"] == {"200": 30}' "a draining HAProxy server gets no new requests"
  probe hap 127.0.0.1 "$H_ADMIN" "set server be_rr/b2 state ready" > /dev/null
  probe until-back "http://127.0.0.1:$H_RR/" b2 10 || fail "b2 should return after state ready"
  echo "  drained: $(cat "$WORK/hap-drained.json")"
fi

echo "▸ minilb sticky cookie, then drain the sticky backend with a request in flight"
if command -v curl >/dev/null; then
  : > "$WORK/jar"
  curl -s -c "$WORK/jar" -b "$WORK/jar" -o "$WORK/first.json" "http://127.0.0.1:$M_RR/" || fail "curl through minilb"
  OWNER="$(backend_of "$WORK/first.json")"
  grep -q "MINILB" "$WORK/jar" || fail "no MINILB cookie in the jar: $(cat "$WORK/jar")"
  for _ in 1 2 3 4 5; do
    curl -s -c "$WORK/jar" -b "$WORK/jar" -o "$WORK/again.json" "http://127.0.0.1:$M_RR/" || fail "curl with the cookie jar"
    [[ "$(backend_of "$WORK/again.json")" == "$OWNER" ]] || fail "the cookie should pin the client to $OWNER: $(cat "$WORK/again.json")"
  done
  curl -s -b "$WORK/jar" -o "$WORK/slow.json" -w '%{http_code}' "http://127.0.0.1:$M_RR/report?delay=1.5" > "$WORK/slow.code" &
  SLOW=$!
  for _ in $(seq 1 50); do
    curl -s "http://127.0.0.1:$M_RR/_minilb/status" > "$WORK/status.json" || true
    python3 -c 'import json,sys; d=json.load(open(sys.argv[1])); raise SystemExit(0 if any(b["name"] == sys.argv[2] and b["active"] == 1 for b in d["backends"]) else 1)' "$WORK/status.json" "$OWNER" 2>/dev/null && break
    sleep 0.05
  done
  curl -s -X POST "http://127.0.0.1:$M_RR/_minilb/drain?backend=$OWNER&timeout=30" > "$WORK/drain.json" || fail "POST /_minilb/drain"
  check "$WORK/drain.json" 'd["state"] == "draining" and d["in_flight"] == 1' "drain with one request in flight"
  for _ in 1 2 3 4 5 6; do
    curl -s -b "$WORK/jar" -o "$WORK/moved.json" "http://127.0.0.1:$M_RR/" || fail "curl during the drain"
    if [[ "$(backend_of "$WORK/moved.json")" == "$OWNER" ]]; then fail "a draining backend must not get new requests, even with its cookie"; fi
  done
  wait "$SLOW" || fail "the in-flight request failed"
  [[ "$(cat "$WORK/slow.code")" == "200" && "$(backend_of "$WORK/slow.json")" == "$OWNER" ]] || fail "the in-flight request should finish on $OWNER: $(cat "$WORK/slow.code") $(cat "$WORK/slow.json")"
  probe minilb-wait "http://127.0.0.1:$M_RR" "$OWNER" absent 5 > "$WORK/removed.json" || fail "the drained backend should be removed once idle"
  check "$WORK/removed.json" 'd["events"][-1]["reason"] == "drained: no requests in flight"' "drain event"
  echo "  pinned to $OWNER by cookie, drained with 1 request in flight, removed when it finished"
else
  echo "  (curl not installed; sticky cookie and drain checks skipped)"
fi

kubectl config get-contexts -o name > "$WORK/contexts.txt" 2>/dev/null || true
if ! command -v kubectl >/dev/null || ! grep -qx "$CTX" "$WORK/contexts.txt"; then
  echo "  (kubectl or context $CTX not available; Kubernetes Service checks skipped)"
  echo "SMOKE OK: networking/load-balancing"
  exit 0
fi
k() { kubectl --context "$CTX" "$@"; }
echo "▸ Kubernetes: a Service sends traffic only to Ready endpoints; the readiness probe is the health check, not kube-proxy"
k delete namespace "$NS" --ignore-not-found --wait=true --timeout=90s > /dev/null 2>&1 || true
NS_CREATED=1
k create namespace "$NS" > /dev/null || fail "create namespace $NS"
k -n "$NS" create configmap minilb --from-file=main.py=code/main.py > /dev/null || fail "configmap"
cat > "$WORK/k8s.yaml" <<EOF
apiVersion: apps/v1
kind: Deployment
metadata:
  name: lb-backend
  namespace: $NS
spec:
  replicas: 2
  selector:
    matchLabels: {app: lb-backend}
  template:
    metadata:
      labels: {app: lb-backend}
    spec:
      terminationGracePeriodSeconds: 1
      containers:
        - name: app
          image: $PYTHON_IMAGE
          command: ["python3", "/app/main.py", "backend", "--name", "\$(POD_NAME)", "--host", "0.0.0.0", "--port", "8080"]
          env:
            - name: POD_NAME
              valueFrom: {fieldRef: {fieldPath: metadata.name}}
          ports: [{containerPort: 8080}]
          readinessProbe:
            httpGet: {path: /health, port: 8080}
            periodSeconds: 1
            failureThreshold: 2
          volumeMounts: [{name: code, mountPath: /app}]
      volumes:
        - name: code
          configMap: {name: minilb}
---
apiVersion: v1
kind: Service
metadata:
  name: lb-backend
  namespace: $NS
spec:
  selector: {app: lb-backend}
  ports: [{port: 80, targetPort: 8080}]
EOF
k apply -f "$WORK/k8s.yaml" > /dev/null || fail "apply backends"
k -n "$NS" rollout status deployment/lb-backend --timeout=180s > /dev/null || fail "rollout: $(k -n "$NS" describe pods 2>&1 | tail -20)"
k -n "$NS" get pods -l app=lb-backend -o jsonpath='{range .items[*]}{.metadata.name}{"\n"}{end}' > "$WORK/pods.txt"
POD_A="$(sed -n 1p "$WORK/pods.txt")"; POD_B="$(sed -n 2p "$WORK/pods.txt")"
[[ -n "$POD_A" && -n "$POD_B" ]] || fail "expected two pods: $(cat "$WORK/pods.txt")"
ready_endpoints() {
  for _ in $(seq 1 60); do
    k -n "$NS" get endpointslices -l kubernetes.io/service-name=lb-backend -o json > "$WORK/eps.json"
    python3 - "$WORK/eps.json" "$1" <<'PY' && return 0
import json, sys
want = set(filter(None, sys.argv[2].split(",")))
ready = {e.get("targetRef", {}).get("name") for s in json.load(open(sys.argv[1]))["items"] for e in s.get("endpoints") or [] if (e.get("conditions") or {}).get("ready")}
raise SystemExit(0 if ready == want else 1)
PY
    sleep 1
  done
  fail "ready endpoints never became {$1}: $(cat "$WORK/eps.json")"
}
client() {
  local name="$1" count="$2" mode="$3" pod="$4"
  k -n "$NS" run "$name" --image="$PYTHON_IMAGE" --restart=Never --env="COUNT=$count" --env="MODE=$mode" --env="POD=$pod" --command -- python3 -c '
import collections, json, os, time, urllib.error, urllib.request

def ask():
    try:
        with urllib.request.urlopen("http://lb-backend/", timeout=5) as r:
            return json.load(r)["backend"]
    except urllib.error.HTTPError as err:
        return "500:" + json.load(err)["backend"]
    except OSError as err:
        return "error: " + str(err)

pod, streak, deadline = os.environ["POD"], 0, time.time() + 60
while time.time() < deadline:
    seen = ask()
    if os.environ["MODE"] == "until-seen" and seen == pod:
        break
    streak = streak + 1 if seen == pod else 0
    if os.environ["MODE"] == "until-only" and streak >= 10:
        break
    time.sleep(0.2)
counts = collections.Counter(ask() for _ in range(int(os.environ["COUNT"])))
print(json.dumps(counts))
' > /dev/null || fail "kubectl run $name"
  k -n "$NS" wait --for=jsonpath='{.status.phase}'=Succeeded "pod/$name" --timeout=120s > /dev/null || fail "$name did not succeed: $(k -n "$NS" logs "$name" 2>&1 | tail -5) $(k -n "$NS" describe pod "$name" 2>&1 | tail -10)"
  k -n "$NS" logs "$name" > "$WORK/$name.json"
  k -n "$NS" delete pod "$name" --wait=false > /dev/null 2>&1 || true
}
ready_endpoints "$POD_A,$POD_B"
k -n "$NS" exec "$POD_A" -- python3 -c 'import urllib.request; urllib.request.urlopen(urllib.request.Request("http://127.0.0.1:8080/_backend/mode?status=500", method="POST", data=b""), timeout=5)' || fail "flip $POD_A to 500"
ready_endpoints "$POD_B"
client lb-client-1 20 until-only "$POD_B"
check "$WORK/lb-client-1.json" "d == {\"$POD_B\": 20}" "with $POD_A not Ready, every request should reach $POD_B"
echo "  $POD_A failing its readiness probe: $(cat "$WORK/lb-client-1.json")"
k -n "$NS" exec "$POD_A" -- python3 -c 'import urllib.request; urllib.request.urlopen(urllib.request.Request("http://127.0.0.1:8080/_backend/mode?status=200", method="POST", data=b""), timeout=5)' || fail "flip $POD_A back"
ready_endpoints "$POD_A,$POD_B"
client lb-client-2 40 until-seen "$POD_A"
check "$WORK/lb-client-2.json" "set(d) == {\"$POD_A\", \"$POD_B\"}" "both Ready pods should get new connections"
echo "  both Ready again: $(cat "$WORK/lb-client-2.json")"

echo "SMOKE OK: networking/load-balancing"
