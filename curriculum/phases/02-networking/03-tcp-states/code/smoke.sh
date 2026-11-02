#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."

fail() { echo "SMOKE FAIL: $*" >&2; exit 1; }
[[ "$(uname -s)" == "Linux" ]] || fail "needs Linux (network namespaces, /proc/net/tcp, ss, nstat)"
for tool in ip ss python3; do command -v "$tool" >/dev/null || fail "$tool is not installed"; done
SUDO=""
[[ "$(id -u)" == "0" ]] || SUDO="sudo"
WORK="$(mktemp -d)"
F="code/fixtures"
NS_C="cc-tcp-states-c"
NS_S="cc-tcp-states-s"
VETH_C="cc-tcps-c"
VETH_S="cc-tcps-s"
SERVER="10.203.0.2"
PIDS=()

cleanup() {
  set +e
  for pid in ${PIDS[@]+"${PIDS[@]}"}; do $SUDO kill "$pid" 2>/dev/null; done
  for ns in "$NS_C" "$NS_S"; do
    if [[ -e "/run/netns/$ns" ]]; then
      for pid in $($SUDO ip netns pids "$ns" 2>/dev/null); do $SUDO kill -9 "$pid" 2>/dev/null; done
      $SUDO ip netns del "$ns" 2>/dev/null
    fi
  done
  $SUDO ip link del "$VETH_C" 2>/dev/null
  $SUDO rm -rf "$WORK"
}
trap cleanup EXIT

in_c() { $SUDO ip netns exec "$NS_C" "$@"; }
in_s() { $SUDO ip netns exec "$NS_S" "$@"; }
field() {
  python3 - "$@" <<'PY'
import json, sys
value = json.load(open(sys.argv[1]))
for key in sys.argv[2:]:
    value = value[int(key)] if isinstance(value, list) else value[key]
print(json.dumps(value) if isinstance(value, (dict, list)) else value)
PY
}
wait_json() {
  local file="$1" tries="${2:-100}"
  for _ in $(seq 1 "$tries"); do
    if python3 -c 'import json, sys; json.loads(open(sys.argv[1]).readline())' "$file" 2>/dev/null; then return 0; fi
    sleep 0.1
  done
  fail "no JSON line in $file: $(cat "$file" 2>/dev/null)"
}
diag() {
  local out="$1"
  shift
  local code=0
  python3 code/main.py "$@" --json > "$out" || code=$?
  [[ "$code" == "0" || "$code" == "1" ]] || fail "tcpwatch $* exited $code: $(cat "$out")"
}

echo "▸ tcpwatch on the fixture host: ss and /proc give the same states, every planted failure is found"
diag "$WORK/fx-ss.json" diagnose --ss "$F/ss-tanpio.txt" --nstat "$F/nstat-az.txt" --port-range 40000-40009
diag "$WORK/fx-proc.json" diagnose --proc "$F/proc-net-tcp.txt" --proc "$F/proc-net-tcp6.txt" --netstat "$F/proc-net-netstat.txt" --backlog 8082=1 --port-range 40000-40009
python3 - "$WORK/fx-ss.json" "$WORK/fx-proc.json" <<'PY' || fail "fixture diagnosis: $(head -c 2000 "$WORK/fx-ss.json")"
import json, sys
ss, proc = (json.load(open(path)) for path in sys.argv[1:])
assert ss["states"] == proc["states"], (ss["states"], proc["states"])
wanted = [("accept-queue", "critical"), ("ephemeral-ports", "critical"), ("close-wait", "warning"), ("syn-recv", "warning"), ("syn-sent", "warning"), ("retransmit", "warning"), ("zero-window", "warning")]
for report in (ss, proc):
    found = {(f["kind"], f["severity"]) for f in report["findings"]}
    missing = [w for w in wanted if w not in found]
    assert not missing, (missing, sorted(found))
print("  states:", ss["states"])
PY

echo "▸ the state machine: CLOSE_WAIT has no timer, a blackholed SYN gives up after 127 s on the classic backoff"
python3 code/main.py simulate close-wait-leak --json > "$WORK/sim.json"
[[ "$(field "$WORK/sim.json" rows -1 server)" == "CLOSE_WAIT" ]] || fail "close-wait-leak should end with the server in CLOSE_WAIT: $(cat "$WORK/sim.json")"
python3 code/main.py simulate blackhole --json > "$WORK/blackhole.json"
[[ "$(field "$WORK/blackhole.json" rows -1 t)" == "127.0" ]] || fail "blackhole should time out at 127 s: $(field "$WORK/blackhole.json" rows -1)"

echo "▸ sysctl: tcpwatch reads the same values as sysctl(8) and turns them into seconds"
python3 code/main.py sysctl --json > "$WORK/sysctl.json" || fail "tcpwatch sysctl: $(cat "$WORK/sysctl.json")"
python3 - "$WORK/sysctl.json" > "$WORK/sysctl-values.txt" <<'PY'
import json, sys
for name, value in json.load(open(sys.argv[1]))["values"].items():
    print(name, value)
PY
if command -v sysctl >/dev/null; then
  while read -r name value; do
    real="$(sysctl -n "$name" | xargs)"
    [[ "$real" == "$value" ]] || fail "$name: sysctl says '$real', tcpwatch read '$value'"
  done < "$WORK/sysctl-values.txt"
  echo "  $(grep -c . "$WORK/sysctl-values.txt") values match sysctl -n"
else
  echo "  (sysctl not installed; comparison skipped)"
fi
python3 code/main.py sysctl > "$WORK/sysctl.txt"
sed 's/^/  /' "$WORK/sysctl.txt"

echo "▸ two network namespaces joined by a veth pair: clients in $NS_C, servers in $NS_S"
for ns in "$NS_C" "$NS_S"; do
  if [[ -e "/run/netns/$ns" ]]; then $SUDO ip netns del "$ns"; fi
done
$SUDO ip link del "$VETH_C" 2>/dev/null || true
$SUDO ip netns add "$NS_C"
$SUDO ip netns add "$NS_S"
$SUDO ip link add "$VETH_C" type veth peer name "$VETH_S"
$SUDO ip link set "$VETH_C" netns "$NS_C"
$SUDO ip link set "$VETH_S" netns "$NS_S"
in_c ip addr add 10.203.0.1/30 dev "$VETH_C"
in_s ip addr add "$SERVER/30" dev "$VETH_S"
in_c ip link set lo up
in_s ip link set lo up
in_c ip link set "$VETH_C" up
in_s ip link set "$VETH_S" up
if command -v ping >/dev/null; then
  PINGED=""
  for _ in 1 2 3; do
    if in_c ping -c 1 -W 2 "$SERVER" > "$WORK/ping.txt" 2>&1; then PINGED=1; break; fi
  done
  [[ -n "$PINGED" ]] || fail "no path from $NS_C to $SERVER: $(cat "$WORK/ping.txt")"
fi

echo "▸ lab servers in $NS_S: leak (accepts, never closes) on :8081, backlog 1 and never accepts on :8082"
$SUDO ip netns exec "$NS_S" python3 code/main.py lab leak --host "$SERVER" --port 8081 > "$WORK/leak.json" 2> "$WORK/leak.err" &
PIDS+=("$!")
$SUDO ip netns exec "$NS_S" python3 code/main.py lab backlog --host "$SERVER" --port 8082 --backlog 1 > "$WORK/backlog.json" 2> "$WORK/backlog.err" &
PIDS+=("$!")
wait_json "$WORK/leak.json"
wait_json "$WORK/backlog.json"
LEAK_PID="$(field "$WORK/leak.json" pid)"
echo "  leak pid $LEAK_PID, backlog pid $(field "$WORK/backlog.json" pid)"

echo "▸ CLOSE_WAIT: 8 clients connect and close, the server never calls close()"
in_c python3 code/main.py lab clients "$SERVER" 8081 --count 8 --connect-timeout 2 > "$WORK/leak-clients.json"
[[ "$(field "$WORK/leak-clients.json" connected)" == "8" ]] || fail "8 clients should connect to the leak server: $(cat "$WORK/leak-clients.json")"
for _ in $(seq 1 50); do
  in_s ss -Htanp state close-wait '( sport = :8081 )' > "$WORK/close-wait.txt"
  if [[ "$(grep -c "pid=$LEAK_PID," "$WORK/close-wait.txt" || true)" -ge 8 ]]; then break; fi
  sleep 0.1
done
[[ "$(grep -c "pid=$LEAK_PID," "$WORK/close-wait.txt" || true)" == "8" ]] || fail "expected 8 CLOSE-WAIT sockets owned by pid $LEAK_PID: $(cat "$WORK/close-wait.txt")"

echo "▸ listen overflow: 6 clients against backlog 1; the queue fills, the later SYNs are dropped"
$SUDO ip netns exec "$NS_C" python3 code/main.py lab clients "$SERVER" 8082 --count 6 --connect-timeout 1 --hold 120 > "$WORK/overflow.json" 2> "$WORK/overflow.err" &
PIDS+=("$!")
wait_json "$WORK/overflow.json" 200
QUEUED="$(field "$WORK/overflow.json" connected)"
DROPPED="$(field "$WORK/overflow.json" connect-timeout)"
[[ "$QUEUED" -ge 1 && "$DROPPED" -ge 1 && $((QUEUED + DROPPED)) -eq 6 ]] || fail "expected some clients queued and the rest timed out: $(cat "$WORK/overflow.json")"
echo "  $QUEUED connected into the accept queue, $DROPPED timed out in connect()"

echo "▸ the server namespace through ss -tanpi, /proc/net/tcp and tcpwatch scan: same sockets, same findings, same counters"
in_s ss -tanpi > "$WORK/s-ss.txt"
in_s ss -Hlnt > "$WORK/s-lnt.txt"
in_s cat /proc/net/tcp > "$WORK/s-tcp.txt"
in_s cat /proc/net/tcp6 > "$WORK/s-tcp6.txt" 2>/dev/null || : > "$WORK/s-tcp6.txt"
in_s cat /proc/net/netstat > "$WORK/s-netstat.txt"
if command -v nstat >/dev/null; then
  in_s nstat -asz TcpExtListenOverflows TcpExtListenDrops > "$WORK/s-nstat.txt"
else
  echo "  (nstat not installed; counter comparison uses /proc/net/netstat only)"
fi
diag "$WORK/s-ss.json" diagnose --ss "$WORK/s-ss.txt"
diag "$WORK/s-proc.json" diagnose --proc "$WORK/s-tcp.txt" --proc "$WORK/s-tcp6.txt" --netstat "$WORK/s-netstat.txt" --backlog 8082=1
CODE=0
in_s python3 code/main.py scan --backlog 8082=1 --json > "$WORK/s-scan.json" || CODE=$?
[[ "$CODE" == "1" ]] || fail "scan in $NS_S should report findings and exit 1, got $CODE: $(head -c 2000 "$WORK/s-scan.json")"
python3 - "$WORK" "$LEAK_PID" "$QUEUED" "$DROPPED" <<'PY' || fail "server namespace: tcpwatch disagrees with ss or nstat (ss -tanpi: $(cat "$WORK/s-ss.txt"))"
import json, os, sys
work, leak_pid, queued, dropped = sys.argv[1], sys.argv[2], int(sys.argv[3]), int(sys.argv[4])
reports = {name: json.load(open(f"{work}/s-{name}.json")) for name in ("ss", "proc", "scan")}


def finding(report, kind, port):
    hits = [f for f in report["findings"] if f["kind"] == kind and f["port"] == port]
    assert hits, f"no {kind} finding for :{port} in {report['findings']}"
    return hits[0]


for name, report in reports.items():
    leak = finding(report, "close-wait", 8081)
    assert leak["sockets"] == 8, (name, leak)
    full = finding(report, "accept-queue", 8082)
    assert full["severity"] == "critical" and full["sockets"] == queued, (name, full, queued)
for name in ("ss", "scan"):
    owner = finding(reports[name], "close-wait", 8081)["owner"]
    assert f"pid {leak_pid}" in owner, (name, owner, leak_pid)
assert reports["ss"]["states"] == reports["proc"]["states"] == reports["scan"]["states"], {n: r["states"] for n, r in reports.items()}
lnt = None
for line in open(f"{work}/s-lnt.txt"):
    tokens = line.split()
    if tokens and tokens[0] == "LISTEN":
        tokens = tokens[1:]
    if len(tokens) >= 3 and tokens[2].endswith(":8082"):
        lnt = (int(tokens[0]), int(tokens[1]))
listener = next(s for s in reports["ss"]["sockets"] if s["state"] == "LISTEN" and s["lport"] == 8082)
assert lnt == (listener["recv_q"], listener["send_q"]) == (queued, 1), (lnt, listener, queued)
counters = reports["proc"]["counters"]
assert counters["TcpExtListenOverflows"] >= dropped, (counters, dropped)
if os.path.exists(f"{work}/s-nstat.txt"):
    nstat = {}
    for line in open(f"{work}/s-nstat.txt"):
        parts = line.split()
        if len(parts) >= 2 and not parts[0].startswith("#"):
            nstat[parts[0]] = int(parts[1])
    assert nstat["TcpExtListenOverflows"] == counters["TcpExtListenOverflows"], (nstat, counters)
unread = sorted({s["recv_q"] for s in reports["ss"]["sockets"] if s["state"] == "CLOSE_WAIT"})
print(f"  states {reports['ss']['states']}; ss -lnt :8082 Recv-Q {lnt[0]} Send-Q {lnt[1]}; ListenOverflows {counters['TcpExtListenOverflows']}; CLOSE-WAIT Recv-Q values {unread}")
PY

echo "▸ the client namespace: FIN-WAIT-2 is the other half of the leak, the queued clients are ESTABLISHED"
in_c ss -tanpio > "$WORK/c-ss.txt"
diag "$WORK/c-ss.json" diagnose --ss "$WORK/c-ss.txt"
python3 - "$WORK/c-ss.json" "$SERVER" "$QUEUED" <<'PY' || fail "client namespace: $(cat "$WORK/c-ss.txt")"
import json, sys
report, server, queued = json.load(open(sys.argv[1])), sys.argv[2], int(sys.argv[3])
half = [f for f in report["findings"] if f["kind"] == "fin-wait2" and f["peer"] == f"{server}:8081"]
assert half and half[0]["sockets"] == 8, report["findings"]
established = [s for s in report["sockets"] if s["state"] == "ESTABLISHED" and s["rport"] == 8082]
assert len(established) == queued, (established, queued)
print(f"  {half[0]['summary']}")
PY

echo "▸ a blackholed port: the timed client gives up at its connect timeout, an untimed one is still in SYN-SENT"
HOLE=9
if command -v nft >/dev/null; then
  in_s nft add table inet cc_tcp_states
  in_s nft add chain inet cc_tcp_states input '{ type filter hook input priority 0 ; policy accept ; }'
  in_s nft add rule inet cc_tcp_states input tcp dport 9 drop
  echo "  nft in $NS_S drops tcp dport 9"
elif command -v iptables >/dev/null; then
  in_s iptables -A INPUT -p tcp --dport 9 -j DROP
  echo "  iptables in $NS_S drops tcp dport 9"
else
  HOLE=8082
  echo "  (nft and iptables not installed; the full accept queue on :8082 stands in for the blackhole)"
fi
$SUDO ip netns exec "$NS_C" python3 code/main.py probe "$SERVER" "$HOLE" --no-timeouts --json > "$WORK/untimed.json" 2> "$WORK/untimed.err" &
UNTIMED="$!"
PIDS+=("$UNTIMED")
START="$(date +%s%N)"
CODE=0
in_c python3 code/main.py probe "$SERVER" "$HOLE" --connect-timeout 2 --max-time 5 --json > "$WORK/timed.json" || CODE=$?
ELAPSED_MS=$(( ($(date +%s%N) - START) / 1000000 ))
[[ "$CODE" == "1" && "$(field "$WORK/timed.json" outcome)" == "connect-timeout" ]] || fail "the timed probe should end in connect-timeout (exit $CODE): $(cat "$WORK/timed.json")"
[[ "$ELAPSED_MS" -lt 4500 ]] || fail "the timed probe took ${ELAPSED_MS} ms for a 2 s connect timeout"
$SUDO kill -0 "$UNTIMED" 2>/dev/null || fail "the untimed probe should still be waiting: $(cat "$WORK/untimed.json" "$WORK/untimed.err")"
[[ ! -s "$WORK/untimed.json" ]] || fail "the untimed probe already returned: $(cat "$WORK/untimed.json")"
echo "  timed: connect-timeout after $(field "$WORK/timed.json" elapsed) s (${ELAPSED_MS} ms including sudo and Python start-up); untimed: still waiting"
in_c ss -tanpio > "$WORK/c-syn.txt"
in_c ss -Htan state syn-sent "( dport = :$HOLE )" > "$WORK/c-syn-raw.txt"
in_c cat /proc/net/tcp > "$WORK/c-tcp.txt"
diag "$WORK/c-syn-ss.json" diagnose --ss "$WORK/c-syn.txt"
diag "$WORK/c-syn-proc.json" diagnose --proc "$WORK/c-tcp.txt"
SYN_SENT="$(grep -c . "$WORK/c-syn-raw.txt" || true)"
python3 - "$WORK" "$SERVER:$HOLE" "$SYN_SENT" <<'PY' || fail "SYN-SENT: tcpwatch disagrees with ss ($(cat "$WORK/c-syn.txt"))"
import json, sys
work, peer, expected = sys.argv[1], sys.argv[2], int(sys.argv[3])
assert expected >= 1, "ss shows no SYN-SENT socket"
for name in ("ss", "proc"):
    report = json.load(open(f"{work}/c-syn-{name}.json"))
    hits = [f for f in report["findings"] if f["kind"] == "syn-sent" and f["peer"] == peer]
    assert hits and hits[0]["severity"] == "warning" and hits[0]["sockets"] == expected, (name, report["findings"], expected)
print(f"  {hits[0]['summary']}")
PY

if command -v curl >/dev/null; then
  echo "▸ Use It: curl exits 28 at --connect-timeout on the blackhole and the full queue, and at --max-time on the server that never answers"
  curl_case() {
    local port="$1"
    shift
    local code=0 start end
    start="$(date +%s%N)"
    in_c curl -sS -o /dev/null "$@" "http://$SERVER:$port/" 2> "$WORK/curl.err" || code=$?
    end="$(date +%s%N)"
    echo "$code $(( (end - start) / 1000000 ))"
  }
  read -r CODE MS <<< "$(curl_case "$HOLE" --connect-timeout 2 --max-time 5)"
  [[ "$CODE" == "28" && "$MS" -lt 4500 ]] || fail "curl to the blackhole: exit $CODE after $MS ms, want 28 after about 2 s: $(cat "$WORK/curl.err")"
  echo "  :$HOLE   exit $CODE after $MS ms: $(cat "$WORK/curl.err")"
  read -r CODE MS <<< "$(curl_case 8082 --connect-timeout 2 --max-time 5)"
  [[ "$CODE" == "28" && "$MS" -lt 4500 ]] || fail "curl to the full accept queue: exit $CODE after $MS ms, want 28: $(cat "$WORK/curl.err")"
  echo "  :8082 exit $CODE after $MS ms: $(cat "$WORK/curl.err")"
  read -r CODE MS <<< "$(curl_case 8081 --connect-timeout 2 --max-time 2)"
  [[ "$CODE" == "28" && "$MS" -lt 4500 ]] || fail "curl to the leak server: exit $CODE after $MS ms, want 28 from --max-time: $(cat "$WORK/curl.err")"
  echo "  :8081 exit $CODE after $MS ms: $(cat "$WORK/curl.err")"
else
  echo "  (curl not installed; curl timeout checks skipped)"
fi

echo "▸ keepalive: probe --keepalive 3,1,2 puts a keepalive timer on the socket, visible in ss -o and in tcpwatch"
$SUDO ip netns exec "$NS_C" python3 code/main.py probe "$SERVER" 8081 --connect-timeout 2 --read-timeout 3 --max-time 4 --keepalive 3,1,2 --json > "$WORK/keepalive.json" 2> "$WORK/keepalive.err" &
KEEPALIVE="$!"
PIDS+=("$KEEPALIVE")
FOUND=""
for _ in $(seq 1 50); do
  in_c ss -tanpio > "$WORK/keepalive-ss.txt"
  if grep -q "timer:(keepalive" "$WORK/keepalive-ss.txt"; then FOUND=1; break; fi
  sleep 0.1
done
[[ -n "$FOUND" ]] || fail "no keepalive timer in the client namespace: $(cat "$WORK/keepalive-ss.txt")"
diag "$WORK/keepalive-ss.json" diagnose --ss "$WORK/keepalive-ss.txt"
python3 - "$WORK/keepalive-ss.json" <<'PY' || fail "tcpwatch should read the keepalive timer: $(cat "$WORK/keepalive-ss.txt")"
import json, sys
socks = json.load(open(sys.argv[1]))["sockets"]
assert any(s["state"] == "ESTABLISHED" and s["rport"] == 8081 and s["timer"] == "keepalive" for s in socks), socks
PY
wait "$KEEPALIVE" || true
[[ "$(field "$WORK/keepalive.json" outcome)" == "read-timeout" && "$(field "$WORK/keepalive.json" options SO_KEEPALIVE)" == "1" ]] || fail "keepalive probe: $(cat "$WORK/keepalive.json" "$WORK/keepalive.err")"
echo "  options $(field "$WORK/keepalive.json" options); outcome read-timeout: keepalive keeps a connection alive, it does not bound a wait"

if $SUDO kill -0 "$UNTIMED" 2>/dev/null; then
  echo "  the untimed probe is still in SYN-SENT after every check above; stopping it"
  $SUDO kill "$UNTIMED" 2>/dev/null || true
else
  echo "  the untimed probe has ended: $(cat "$WORK/untimed.json")"
fi

echo "SMOKE OK: networking/tcp-states"
