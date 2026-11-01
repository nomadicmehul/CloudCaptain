#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."

fail() { echo "SMOKE FAIL: $*" >&2; exit 1; }
[[ "$(uname -s)" == "Linux" ]] || fail "needs Linux (tcpdump on lo and -i any, nsenter into a pod)"
command -v tcpdump >/dev/null || fail "tcpdump is not installed (apt-get install tcpdump)"
SUDO=""
[[ "$(id -u)" == "0" ]] || SUDO="sudo"
WORK="$(mktemp -d)"
chmod 755 "$WORK"
NS="cc-packet-capture"
CTX="${CC_SMOKE_CONTEXT:-kind-cc-smoke}"
DNS_IMAGE="coredns/coredns:1.14.7"
DNS_CONTAINER="cc-packet-capture-dns"
POD_IMAGE="python:3.12-alpine"
PIDS=()
CAPTURES=()
NS_CREATED=""
DNS_STARTED=""

pl() { python3 code/main.py "$@"; }
k() { kubectl --context "$CTX" "$@"; }
cleanup() {
  for pidfile in ${CAPTURES[@]+"${CAPTURES[@]}"}; do
    if [[ -s "$pidfile" ]]; then $SUDO kill -INT "$(cat "$pidfile")" 2>/dev/null || true; fi
  done
  for pid in ${PIDS[@]+"${PIDS[@]}"}; do kill "$pid" 2>/dev/null || true; done
  if [[ -n "$DNS_STARTED" ]]; then docker rm -f "$DNS_CONTAINER" >/dev/null 2>&1 || true; fi
  if [[ -n "$NS_CREATED" ]]; then k delete namespace "$NS" --ignore-not-found --wait=false >/dev/null 2>&1 || true; fi
  $SUDO rm -rf "$WORK"
}
trap cleanup EXIT

start_capture() {
  local name="$1"
  shift
  local pidfile="$WORK/$name.pid"
  CAPTURES+=("$pidfile")
  $SUDO sh -c 'echo $$ > "$1"; shift; exec tcpdump "$@"' sh "$pidfile" -nn -U -Z root -B 16384 "$@" 2> "$WORK/$name.err" &
  for _ in $(seq 1 100); do
    if grep -q "listening on" "$WORK/$name.err" 2>/dev/null; then return 0; fi
    sleep 0.1
  done
  fail "tcpdump $name did not start: $(cat "$WORK/$name.err")"
}
wait_live() {
  local file="$1" name="$2"
  for _ in $(seq 1 50); do
    python3 -c 'import socket; socket.socket(socket.AF_INET, socket.SOCK_DGRAM).sendto(b"canary", ("127.0.0.1", 18099))'
    if [[ "$(stat -c %s "$file" 2>/dev/null || echo 0)" -gt 24 ]]; then return 0; fi
    sleep 0.1
  done
  fail "capture $name never recorded the canary datagram: $(cat "$WORK/$name.err")"
}
stop_capture() {
  local name="$1"
  $SUDO kill -INT "$(cat "$WORK/$name.pid")" 2>/dev/null || true
  for _ in $(seq 1 50); do
    if grep -q "packets captured" "$WORK/$name.err"; then break; fi
    sleep 0.1
  done
  grep -q "packets captured" "$WORK/$name.err" || fail "tcpdump $name did not stop cleanly: $(cat "$WORK/$name.err")"
  $SUDO rm -f "$WORK/$name.pid"
  grep -E "packets (captured|dropped by kernel)" "$WORK/$name.err" | sed "s/^/    $name: /"
  grep -q "^0 packets dropped by kernel" "$WORK/$name.err" || fail "tcpdump $name dropped packets in the kernel: $(cat "$WORK/$name.err")"
}
same_counts() {
  local file="$1"
  shift
  local f t p
  for f in "$@"; do
    if [[ -z "$f" ]]; then
      tcpdump -r "$file" -nn > "$WORK/td.txt" 2> "$WORK/td.err" || fail "tcpdump -r $file: $(cat "$WORK/td.err")"
    else
      tcpdump -r "$file" -nn "$f" > "$WORK/td.txt" 2> "$WORK/td.err" || fail "tcpdump -r $file '$f': $(cat "$WORK/td.err")"
    fi
    t="$(wc -l < "$WORK/td.txt" | tr -d ' ')"
    p="$(pl read "$file" "$f" --count)" || fail "pcaplens read $file '$f' failed"
    [[ "$t" == "$p" ]] || fail "$(basename "$file") with filter '$f': tcpdump -r counts $t packets, pcaplens $p; tcpdump said: $(head -c 2000 "$WORK/td.txt")"
  done
  echo "  $(basename "$file"): $# filters, every count equal to tcpdump -r"
}
info_field() {
  pl info "$1" --json > "$WORK/info.json" || fail "pcaplens info $1: $(cat "$WORK/info.json")"
  python3 - "$WORK/info.json" "$2" <<'PY'
import json, sys
value = json.load(open(sys.argv[1]))
for key in sys.argv[2].split("."):
    value = value[int(key)] if isinstance(value, list) else value[key]
print(value)
PY
}

DEMO_FILTERS=("" "tcp" "udp" "ip6" "port 80" "udp port 53" "port 53 or 80" "src host 10.0.0.1" "net 2001:db8::/32"
  "tcp[tcpflags] & tcp-syn != 0" "tcp[tcpflags] & (tcp-syn|tcp-ack) == tcp-syn" "(tcp[tcpflags] & tcp-syn != 0) or (ip6[6] = 6 and ip6[53] & tcp-syn != 0)"
  "host 10.0.0.3 or host 10.0.0.53 and udp" "not tcp[100] = 1" "portrange 43512-43516" "udp[8:2] = 0x1a2b")

echo "▸ pcaplens demo: every link type, pcap and pcapng, read by this host's libpcap with the same counts"
tcpdump --version 2>&1 | head -2 | sed 's/^/  /'
for fmt in pcap pcapng; do
  for link in ether sll sll2 raw; do
    pl demo "$WORK/demo-$link.$fmt" --format "$fmt" --link "$link" > /dev/null || fail "pcaplens demo --format $fmt --link $link"
    same_counts "$WORK/demo-$link.$fmt" "${DEMO_FILTERS[@]}"
  done
done
TZ=UTC tcpdump -r "$WORK/demo-ether.pcap" -nn -S -tttt 2> /dev/null | sed 's/: HTTP.*$//' > "$WORK/demo-tcpdump.txt"
pl read "$WORK/demo-ether.pcap" > "$WORK/demo-pcaplens.txt"
diff "$WORK/demo-tcpdump.txt" "$WORK/demo-pcaplens.txt" > "$WORK/demo.diff" || fail "pcaplens read differs from tcpdump -nn -S -tttt: $(head -40 "$WORK/demo.diff")"
echo "  all $(wc -l < "$WORK/demo-pcaplens.txt") lines identical to tcpdump -nn -S -tttt"

echo "▸ tcpdump -d: tcp[] loads check for IPv4 only, port filters cover both families"
tcpdump -r "$WORK/demo-ether.pcap" -d 'tcp[tcpflags] & tcp-syn != 0' > "$WORK/bpf-flags.txt" 2> /dev/null || fail "tcpdump -d"
tcpdump -r "$WORK/demo-ether.pcap" -d 'tcp port 443' > "$WORK/bpf-port.txt" 2> /dev/null || fail "tcpdump -d"
grep -q "#0x800" "$WORK/bpf-flags.txt" || fail "expected an IPv4 ethertype check: $(cat "$WORK/bpf-flags.txt")"
if grep -q "#0x86dd" "$WORK/bpf-flags.txt"; then fail "tcp[] unexpectedly compiled an IPv6 branch: $(cat "$WORK/bpf-flags.txt")"; fi
grep -q "#0x86dd" "$WORK/bpf-port.txt" && grep -q "#0x800" "$WORK/bpf-port.txt" || fail "tcp port should test both families: $(cat "$WORK/bpf-port.txt")"
echo "  $(wc -l < "$WORK/bpf-flags.txt") instructions for tcp[tcpflags], $(wc -l < "$WORK/bpf-port.txt") for tcp port 443"
pl filter 'tcp[tcpflags] & tcp-syn != 0 or udp port 53 and src host 10.0.0.53' | sed 's/^/  /'

echo "▸ local servers: HTTP on 18080, a listener on 18081 for the reset, nothing on 18082"
mkdir -p "$WORK/www"
echo "hello from pcaplens" > "$WORK/www/hello.txt"
head -c 4000000 /dev/zero > "$WORK/www/big.bin"
python3 -m http.server 18080 --bind 127.0.0.1 --directory "$WORK/www" > "$WORK/http.log" 2>&1 &
PIDS+=("$!")
cat > "$WORK/listener.py" <<'PY'
import socket, sys
server = socket.socket()
server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
server.bind(("127.0.0.1", 18081))
server.listen(8)
open(sys.argv[1], "w").close()
while True:
    conn, _ = server.accept()
    try:
        while conn.recv(4096):
            pass
    except OSError:
        pass
    conn.close()
PY
cat > "$WORK/rst_client.py" <<'PY'
import socket, struct
client = socket.create_connection(("127.0.0.1", 18081), timeout=5)
client.sendall(b"hello")
print(client.getsockname()[1])
client.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
client.close()
PY
python3 "$WORK/listener.py" "$WORK/listener.ready" > "$WORK/listener.log" 2>&1 &
PIDS+=("$!")
for _ in $(seq 1 50); do curl -s --noproxy '*' -m 1 -o /dev/null http://127.0.0.1:18080/ && [[ -f "$WORK/listener.ready" ]] && break; sleep 0.2; done
curl -s --noproxy '*' -m 2 -o /dev/null http://127.0.0.1:18080/ || fail "the HTTP server on 18080 did not start: $(cat "$WORK/http.log")"
[[ -f "$WORK/listener.ready" ]] || fail "the listener on 18081 did not start: $(cat "$WORK/listener.log")"

HAVE_DNS=""
if ! command -v dig >/dev/null; then
  echo "  (dig not installed; the DNS checks are skipped)"
elif ! command -v docker >/dev/null; then
  echo "  (docker not installed; the CoreDNS checks are skipped)"
else
  echo "▸ CoreDNS ($DNS_IMAGE) serving cc.test on 127.0.0.2:53"
  mkdir -m 755 "$WORK/dns"
  cat > "$WORK/dns/Corefile" <<'EOF'
cc.test:53 {
    bind 127.0.0.2
    file /etc/coredns/db.cc.test
}
EOF
  cat > "$WORK/dns/db.cc.test" <<'EOF'
$ORIGIN cc.test.
$TTL 3600
@       IN SOA ns.cc.test. hostmaster.cc.test. 2026110101 7200 3600 1209600 60
@       IN NS  ns.cc.test.
ns      IN A   127.0.0.2
web     300 IN A 10.20.30.40
alias   300 IN CNAME web.cc.test.
EOF
  chmod 644 "$WORK/dns/Corefile" "$WORK/dns/db.cc.test"
  docker rm -f "$DNS_CONTAINER" > /dev/null 2>&1 || true
  DNS_STARTED=1
  docker run -d --name "$DNS_CONTAINER" --network host --user 0:0 -v "$WORK/dns:/etc/coredns:ro" "$DNS_IMAGE" -conf /etc/coredns/Corefile > /dev/null || fail "docker run $DNS_IMAGE"
  for _ in $(seq 1 60); do
    if [[ "$(dig @127.0.0.2 +short +tries=1 +time=1 web.cc.test A 2> /dev/null || true)" == "10.20.30.40" ]]; then HAVE_DNS=1; break; fi
    sleep 0.5
  done
  [[ -n "$HAVE_DNS" ]] || fail "CoreDNS did not answer on 127.0.0.2:53: $(docker logs "$DNS_CONTAINER" 2>&1 | tail -20)"
fi

echo "▸ capture on lo while curl fetches, a client resets, a port refuses and dig asks CoreDNS"
start_capture lo -i lo -s 0 -w "$WORK/lo.pcap" 'port 18080 or port 18081 or port 18082 or udp port 18099 or (host 127.0.0.2 and port 53)'
wait_live "$WORK/lo.pcap" lo
curl -s --noproxy '*' -m 5 -o "$WORK/hello.out" http://127.0.0.1:18080/hello.txt || fail "curl hello.txt"
python3 "$WORK/rst_client.py" > "$WORK/rst.port" || fail "the reset client failed"
if curl -s --noproxy '*' -m 2 -o /dev/null http://127.0.0.1:18082/; then fail "something answered on 18082"; fi
if [[ -n "$HAVE_DNS" ]]; then
  for name in web alias nosuch; do
    dig @127.0.0.2 "$name.cc.test" A +tries=1 +time=2 > "$WORK/dig-$name.txt" || fail "dig $name.cc.test: $(cat "$WORK/dig-$name.txt")"
  done
fi
stop_capture lo
[[ "$(info_field "$WORK/lo.pcap" linktypes.0.name)" == "EN10MB" ]] || fail "lo should be EN10MB: $(cat "$WORK/info.json")"
[[ "$(info_field "$WORK/lo.pcap" damage)" == "None" ]] || fail "lo.pcap damaged: $(cat "$WORK/info.json")"
LIVE_FILTERS=("" "tcp" "udp" "port 18080" "tcp port 18081" "port 18082" "src host 127.0.0.1 and dst port 18080" "port 18080 or 18081"
  "tcp[tcpflags] & tcp-syn != 0" "tcp[tcpflags] & (tcp-syn|tcp-ack) == tcp-syn" "tcp[tcpflags] & tcp-rst != 0" "tcp[13] & 1 != 0"
  "not tcp" "udp port 53" "host 127.0.0.2" "udp port 53 or port 18082 and tcp" "ip6")
same_counts "$WORK/lo.pcap" "${LIVE_FILTERS[@]}"

echo "▸ flows: the handshake, bytes each way and who closed first agree with tcpdump's own lines"
pl flows "$WORK/lo.pcap" tcp > "$WORK/flows.txt" || fail "pcaplens flows: $(cat "$WORK/flows.txt")"
sed 's/^/  /' "$WORK/flows.txt"
pl flows "$WORK/lo.pcap" tcp --json > "$WORK/flows.json" || fail "pcaplens flows --json"
tcpdump -r "$WORK/lo.pcap" -nn 'tcp port 18080' > "$WORK/td-18080.txt" 2> /dev/null
tcpdump -r "$WORK/lo.pcap" -nn -S 'tcp[tcpflags] == tcp-syn and dst port 18080' > "$WORK/td-syn.txt" 2> /dev/null
tcpdump -r "$WORK/lo.pcap" -nn 'tcp[tcpflags] & tcp-fin != 0 and port 18080' > "$WORK/td-fin.txt" 2> /dev/null
tcpdump -r "$WORK/lo.pcap" -nn 'tcp[tcpflags] & tcp-rst != 0 and port 18081' > "$WORK/td-rst.txt" 2> /dev/null
tcpdump -r "$WORK/lo.pcap" -nn 'tcp[tcpflags] & tcp-rst != 0 and port 18082' > "$WORK/td-refused.txt" 2> /dev/null
python3 - "$WORK" <<'PY' || fail "flows disagree with tcpdump (see above)"
import json, os, re, sys
work = sys.argv[1]
read = lambda name: open(os.path.join(work, name)).read().splitlines()
flows = json.load(open(os.path.join(work, "flows.json")))
by_port = {}
for f in flows:
    by_port.setdefault(int(f["server"].rsplit(":", 1)[1]), []).append(f)
assert len(by_port.get(18080, [])) == 1, by_port.get(18080)
web = by_port[18080][0]
assert web["state"] == "closed", web
assert web["handshake"] and web["handshake"]["rtt_ms"] is not None and web["handshake"]["rtt_ms"] >= 0, web
lines = read("td-18080.txt")
length = lambda line: int(re.search(r"length (\d+)", line).group(1))
up = sum(length(l) for l in lines if "> 127.0.0.1.18080:" in l)
down = sum(length(l) for l in lines if "127.0.0.1.18080 >" in l)
assert web["bytes"] == {"client": up, "server": down}, (web["bytes"], up, down)
assert web["packets"]["client"] + web["packets"]["server"] == len(lines), (web["packets"], len(lines))
syn = read("td-syn.txt")
assert len(syn) == 1, syn
assert web["mss"]["client"] == int(re.search(r"mss (\d+)", syn[0]).group(1)), (web["mss"], syn[0])
assert web["wscale"]["client"] == int(re.search(r"wscale (\d+)", syn[0]).group(1)), (web["wscale"], syn[0])
assert web["client"].endswith(":" + re.search(r"127\.0\.0\.1\.(\d+) >", syn[0]).group(1)), (web["client"], syn[0])
first_fin = read("td-fin.txt")[0]
closer = "server" if "127.0.0.1.18080 >" in first_fin else "client"
assert web["closed_by"] == closer + " FIN", (web["closed_by"], first_fin)
print(f"  18080: rtt {web['handshake']['rtt_ms']} ms, mss {web['mss']['client']}, wscale {web['wscale']['client']}, {up} bytes up, {down} down, first FIN from the {closer}")
rst = by_port[18081][0]
rst_port = open(os.path.join(work, "rst.port")).read().strip()
rst_lines = read("td-rst.txt")
assert rst["state"] == "reset" and rst["closed_by"] == "client RST", rst
assert rst["client"] == "127.0.0.1:" + rst_port, (rst["client"], rst_port)
assert len(rst_lines) == 1 and f"127.0.0.1.{rst_port} > 127.0.0.1.18081: Flags [R" in rst_lines[0], rst_lines
print(f"  18081: reset by the client from port {rst_port}: {rst_lines[0].split(': ', 1)[1]}")
refused = by_port[18082][0]
refused_lines = read("td-refused.txt")
assert refused["state"] == "refused" and refused["closed_by"] == "server RST", refused
assert len(refused_lines) == 1 and "127.0.0.1.18082 >" in refused_lines[0], refused_lines
print(f"  18082: refused: {refused['notes'][0]}")
PY

if [[ -n "$HAVE_DNS" ]]; then
  echo "▸ DNS: pcaplens decodes the same questions as tcpdump and the same answers as dig"
  tcpdump -r "$WORK/lo.pcap" -nn 'udp port 53' > "$WORK/td-dns.txt" 2> /dev/null
  sed 's/^/  /' "$WORK/td-dns.txt"
  pl dns "$WORK/lo.pcap" --json > "$WORK/dns.json" || fail "pcaplens dns: $(cat "$WORK/dns.json")"
  python3 - "$WORK" <<'PY' || fail "DNS decoding disagrees with tcpdump or dig (see above)"
import json, os, re, sys
work = sys.argv[1]
transactions = json.load(open(os.path.join(work, "dns.json")))
tcpdump_questions = set()
for line in open(os.path.join(work, "td-dns.txt")):
    match = re.search(r"\s([A-Z0-9]+)\? (\S+) ", line)
    if match:
        tcpdump_questions.add((match.group(1), match.group(2)))
ours = {(t["question"]["type"], t["question"]["name"]) for t in transactions}
assert ours == tcpdump_questions, (ours, tcpdump_questions)

def dig_sections(name):
    out = {"status": None, "ANSWER": [], "AUTHORITY": []}
    current = None
    for line in open(os.path.join(work, f"dig-{name}.txt")):
        status = re.search(r"status: (\w+)", line)
        if status:
            out["status"] = status.group(1)
        if line.startswith(";; ") and line.rstrip().endswith("SECTION:"):
            current = line[3:].split()[0]
            continue
        if not line.strip():
            current = None
            continue
        if current in ("ANSWER", "AUTHORITY") and not line.startswith(";"):
            owner, ttl, _cls, rtype, *data = line.split()
            out[current].append((owner, int(ttl), rtype, " ".join(data)))
    return out

for name in ("web", "alias", "nosuch"):
    (t,) = [t for t in transactions if t["question"]["name"] == f"{name}.cc.test."]
    dig = dig_sections(name)
    mine = lambda records: [(r["name"], r["ttl"], r["type"], r["data"]) for r in records]
    assert t["rcode"] == dig["status"], (name, t["rcode"], dig["status"])
    assert mine(t["answers"]) == dig["ANSWER"], (name, mine(t["answers"]), dig["ANSWER"])
    assert mine(t["authority"]) == dig["AUTHORITY"], (name, mine(t["authority"]), dig["AUTHORITY"])
    assert t["latency_ms"] is not None and t["retries"] == 0, t
    print(f"  {name}.cc.test: {t['rcode']} in {t['latency_ms']} ms, answers {mine(t['answers']) or '-'}, authority {[r[2] for r in mine(t['authority'])] or '-'}")
PY
  same_counts "$WORK/lo.pcap" "src host 127.0.0.2 and udp port 53" "udp[10:2] & 0x000f = 3"
fi

echo "▸ -i any writes a cooked header; pcaplens decodes it and still agrees with tcpdump"
start_capture any -i any -s 0 -w "$WORK/any.pcap" 'port 18080 or udp port 18099'
wait_live "$WORK/any.pcap" any
curl -s --noproxy '*' -m 5 -o /dev/null http://127.0.0.1:18080/hello.txt || fail "curl during the -i any capture"
stop_capture any
ANY_LINK="$(info_field "$WORK/any.pcap" linktypes.0.name)"
[[ "$ANY_LINK" == "LINUX_SLL2" || "$ANY_LINK" == "LINUX_SLL" ]] || fail "-i any should write a cooked link type, got $ANY_LINK"
grep -o "link-type [A-Z_0-9]*" "$WORK/any.err" | sed 's/^/  tcpdump: /'
COOKED_FILTERS=("" "tcp" "port 18080" "tcp[tcpflags] & tcp-syn != 0" "udp" "src host 127.0.0.1 and dst port 18080")
same_counts "$WORK/any.pcap" "${COOKED_FILTERS[@]}"
pl read "$WORK/any.pcap" --json > "$WORK/any.json"
python3 - "$WORK/any.json" <<'PY' || fail "cooked packets lack direction or interface data"
import collections, json, sys
packets = json.load(open(sys.argv[1]))
assert packets and all(p["direction"] for p in packets), packets[:2]
seen = collections.Counter((p["ifindex"], p["direction"]) for p in packets)
print("  (ifindex, direction) -> packets:", dict(seen))
PY
pl flows "$WORK/any.pcap" 'port 18080' --json > "$WORK/any-flows.json"
python3 -c 'import json,sys; f=[x for x in json.load(open(sys.argv[1])) if x["handshake"] and x["handshake"]["rtt_ms"] is not None]; assert f, "no complete handshake"; print("  handshake seen through the cooked header:", f[0]["client"], "->", f[0]["server"], f[0]["state"])' "$WORK/any-flows.json" || fail "no handshake in the -i any capture: $(cat "$WORK/any-flows.json")"
$SUDO tcpdump -L -i any > "$WORK/linktypes.txt" 2>&1 || true
OTHER=""
if [[ "$ANY_LINK" == "LINUX_SLL2" ]] && grep -qE "LINUX_SLL \(" "$WORK/linktypes.txt"; then OTHER="LINUX_SLL"; fi
if [[ "$ANY_LINK" == "LINUX_SLL" ]] && grep -qE "LINUX_SLL2 \(" "$WORK/linktypes.txt"; then OTHER="LINUX_SLL2"; fi
if [[ -n "$OTHER" ]]; then
  start_capture any2 -i any -y "$OTHER" -s 0 -w "$WORK/any2.pcap" 'port 18080 or udp port 18099'
  wait_live "$WORK/any2.pcap" any2
  curl -s --noproxy '*' -m 5 -o /dev/null http://127.0.0.1:18080/hello.txt || fail "curl during the -y $OTHER capture"
  stop_capture any2
  [[ "$(info_field "$WORK/any2.pcap" linktypes.0.name)" == "$OTHER" ]] || fail "-y $OTHER was not honoured: $(cat "$WORK/info.json")"
  same_counts "$WORK/any2.pcap" "${COOKED_FILTERS[@]}"
else
  echo "  (tcpdump -L -i any offers no second cooked type; -y check skipped)"
fi

echo "▸ -s 96 keeps only headers and -C 1 -W 3 rotates, while curl downloads 4 MB"
start_capture snap -i lo -s 96 -w "$WORK/snap.pcap" 'port 18080 or udp port 18099'
start_capture ring -i lo -s 0 -C 1 -W 3 -w "$WORK/ring.pcap" 'port 18080 or udp port 18099'
wait_live "$WORK/snap.pcap" snap
wait_live "$WORK/ring.pcap0" ring
curl -s --noproxy '*' -m 20 --limit-rate 8M -o /dev/null http://127.0.0.1:18080/big.bin || fail "curl big.bin"
stop_capture snap
stop_capture ring
[[ "$(info_field "$WORK/snap.pcap" snaplen)" == "96" ]] || fail "snaplen should be 96: $(cat "$WORK/info.json")"
TRUNCATED="$(info_field "$WORK/snap.pcap" truncated_packets)"
[[ "$TRUNCATED" -gt 0 ]] || fail "a 96-byte snaplen should cut the data segments: $(cat "$WORK/info.json")"
same_counts "$WORK/snap.pcap" "" "tcp" "tcp[tcpflags] & tcp-push != 0" "src port 18080"
tcpdump -r "$WORK/snap.pcap" -nn 'tcp src port 18080' > "$WORK/td-snap.txt" 2> /dev/null
pl flows "$WORK/snap.pcap" 'port 18080' --json > "$WORK/snap-flows.json"
python3 - "$WORK/snap-flows.json" "$WORK/td-snap.txt" "$TRUNCATED" <<'PY' || fail "byte counts from a headers-only capture are wrong"
import json, re, sys
(flow,) = json.load(open(sys.argv[1]))
from_tcpdump = sum(int(re.search(r"length (\d+)", l).group(1)) for l in open(sys.argv[2]))
assert flow["bytes"]["server"] == from_tcpdump >= 4000000, (flow["bytes"], from_tcpdump)
print(f"  {sys.argv[3]} records cut at 96 bytes, yet {flow['bytes']['server']} bytes from the server, the same total tcpdump prints")
PY
for i in 0 1 2; do
  f="$WORK/ring.pcap$i"
  [[ -f "$f" ]] || fail "rotation file ring.pcap$i is missing: $(ls -l "$WORK")"
  size="$(stat -c %s "$f")"
  [[ "$size" -lt 1100000 ]] || fail "ring.pcap$i is $size bytes, more than -C 1 allows"
  [[ "$(info_field "$f" damage)" == "None" && "$(info_field "$f" linktypes.0.name)" == "EN10MB" ]] || fail "ring.pcap$i is not a complete pcap: $(cat "$WORK/info.json")"
  same_counts "$f" "" "tcp" "src port 18080"
done
[[ ! -e "$WORK/ring.pcap3" ]] || fail "-W 3 should keep three files"

if ! command -v kubectl > /dev/null; then
  echo "  (kubectl not installed; pod capture skipped)"
  echo "SMOKE OK: networking/packet-capture (no pod capture)"
  exit 0
fi
kubectl config get-contexts -o name > "$WORK/contexts.txt" 2> /dev/null || true
if ! grep -qx "$CTX" "$WORK/contexts.txt"; then
  echo "  (context $CTX not found; pod capture skipped)"
  echo "SMOKE OK: networking/packet-capture (no pod capture)"
  exit 0
fi
command -v nsenter > /dev/null || fail "nsenter is not installed (util-linux)"

echo "▸ inside a pod: nsenter -n runs the node's tcpdump in the pod's network namespace"
k delete namespace "$NS" --ignore-not-found --wait=true --timeout=90s > /dev/null 2>&1 || true
NS_CREATED=1
k create namespace "$NS" > /dev/null || fail "create namespace $NS"
for _ in $(seq 1 30); do k get serviceaccount default -n "$NS" > /dev/null 2>&1 && break; sleep 1; done
cat > "$WORK/pod.json" <<EOF
{"apiVersion": "v1", "kind": "Pod", "metadata": {"name": "web", "namespace": "$NS", "labels": {"app": "web"}},
 "spec": {"terminationGracePeriodSeconds": 0, "containers": [{"name": "web", "image": "$POD_IMAGE",
   "command": ["python3", "-m", "http.server", "18765"], "ports": [{"containerPort": 18765}],
   "readinessProbe": {"httpGet": {"path": "/", "port": 18765}, "periodSeconds": 1}}]}}
EOF
k apply -f "$WORK/pod.json" > /dev/null || fail "apply the web pod"
k wait --for=condition=Ready pod/web -n "$NS" --timeout=180s > /dev/null || fail "pod web not Ready: $(k describe pod web -n "$NS" 2>&1 | tail -20)"
POD_IP="$(k get pod web -n "$NS" -o jsonpath='{.status.podIP}')"
HOST_IP="$(k get pod web -n "$NS" -o jsonpath='{.status.hostIP}')"
PID="$(pgrep -f -n 'http.server 18765' || true)"
[[ -n "$PID" ]] || fail "no host process for the pod's http.server (kind nodes share this kernel): $(ps -eo pid,args | grep -c http.server)"
$SUDO nsenter -t "$PID" -n ip -br addr > "$WORK/pod-addr.txt" || fail "nsenter -t $PID -n ip addr"
grep -q "$POD_IP" "$WORK/pod-addr.txt" || fail "pid $PID's network namespace does not hold the pod IP $POD_IP: $(cat "$WORK/pod-addr.txt")"
echo "  pod $POD_IP on node $HOST_IP, host pid $PID: $(grep "$POD_IP" "$WORK/pod-addr.txt" | awk '{print $1}')"
$SUDO timeout 60 nsenter -t "$PID" -n tcpdump -i eth0 -nn -s 0 -U -Z root -c 30 -w "$WORK/pod.pcap" 'tcp port 18765' 2> "$WORK/pod.err" || fail "tcpdump in the pod's namespace: $(cat "$WORK/pod.err")"
grep -o "listening on [^,]*, link-type [A-Z_0-9]*" "$WORK/pod.err" | sed 's/^/  tcpdump: /'
same_counts "$WORK/pod.pcap" "" "tcp[tcpflags] & tcp-syn != 0" "dst port 18765" "dst host $POD_IP"
pl flows "$WORK/pod.pcap" --json > "$WORK/pod-flows.json"
python3 - "$WORK/pod-flows.json" "$POD_IP" <<'PY' || fail "no complete probe connection to the pod in the capture: $(cat "$WORK/pod-flows.json")"
import json, sys
flows, pod_ip = json.load(open(sys.argv[1])), sys.argv[2]
complete = [f for f in flows if f["server"] == f"{pod_ip}:18765" and f["handshake"] and f["handshake"]["rtt_ms"] is not None]
assert complete, flows
f = complete[0]
print(f"  {len(flows)} probe connection(s); {f['client']} -> {f['server']}: {f['state']}, rtt {f['handshake']['rtt_ms']} ms, {f['bytes']['server']} bytes of HTTP response")
PY
k delete pod web -n "$NS" --wait=false > /dev/null 2>&1 || true

echo "SMOKE OK: networking/packet-capture"
