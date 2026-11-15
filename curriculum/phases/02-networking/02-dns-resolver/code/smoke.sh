#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."

fail() { echo "SMOKE FAIL: $*" >&2; exit 1; }
MINI="$PWD/code/main.py"
FIX="$PWD/code/fixtures"
WORK="$(mktemp -d)"
COREDNS_IMAGE="coredns/coredns:1.14.7"
GLIBC_IMAGE="debian:13-slim"
POD_IMAGE="alpine:3.22"
PREFIX="cc-dns-resolver"
NS="cc-dns-resolver"
CTX="${CC_SMOKE_CONTEXT:-kind-cc-smoke}"
ROOT=127.0.10.1
TLD=127.0.10.2
AUTH=127.0.10.3
RESOLVER=127.0.10.10
CONTAINERS=()
NS_CREATED=""
SUDO=""
[[ "$(id -u)" == "0" ]] || SUDO="sudo -n"
k() { kubectl --context "$CTX" "$@"; }
cleanup() {
  set +e
  if [[ -s "$WORK/wire.pid" ]]; then $SUDO kill -INT "$(cat "$WORK/wire.pid")" >/dev/null 2>&1; fi
  for c in ${CONTAINERS[@]+"${CONTAINERS[@]}"}; do docker rm -f "$c" >/dev/null 2>&1; done
  if [[ -n "$NS_CREATED" ]]; then k delete namespace "$NS" --ignore-not-found --wait=false >/dev/null 2>&1; fi
  rm -rf "$WORK"
}
trap cleanup EXIT
mini() { python3 "$MINI" "$@"; }

CHECK="$WORK/check.py"
cat > "$CHECK" <<'PY'
import json
import re
import subprocess
import sys

RR_LINE = re.compile(r"^(\S+)\s+(\d+)\s+(\S+)\s+(\S+)\s*(.*)$")
RECEIVED = re.compile(r"^;; Received (\d+) bytes from ([0-9a-fA-F:.]+)#(\d+)\(([^)]*)\)")
LOG = re.compile(r'"(\S+) (\S+) (\S+) (udp|tcp) (\d+) (\S+) (\d+)" (\S+)')


def norm(rec):
    rtype = rec["type"].upper()
    data = " ".join(rec["data"].split())
    return (rec["name"].lower(), int(rec["ttl"]), rtype, data if rtype == "TXT" else data.lower())


def parse_dig(text):
    out = {"status": None, "flags": [], "answer": [], "authority": [], "additional": [], "retried_tcp": "Truncated, retrying in TCP mode" in text}
    section = None
    for line in text.splitlines():
        if "->>HEADER<<-" in line:
            m = re.search(r"status: (\w+)", line)
            out["status"] = m.group(1) if m else None
            continue
        m = re.match(r";; flags:([^;]*);", line)
        if m:
            out["flags"] = m.group(1).split()
            continue
        m = re.match(r";; (ANSWER|AUTHORITY|ADDITIONAL) SECTION:", line)
        if m:
            section = m.group(1).lower()
            continue
        if not line.strip():
            section = None
            continue
        if line.startswith(";") or section is None:
            continue
        m = RR_LINE.match(line)
        if m and m.group(4) != "OPT":
            out[section].append({"name": m.group(1), "ttl": int(m.group(2)), "class": m.group(3), "type": m.group(4), "data": m.group(5)})
    return out


def dig(port, server, name, qtype):
    res = subprocess.run(["dig", "-p", port, "+norecurse", "+time=2", "+tries=2", "@" + server, name, qtype], capture_output=True, text=True, timeout=30)
    return parse_dig(res.stdout)


def compare(dig_file, mini_file):
    d = parse_dig(open(dig_file).read())
    m = json.load(open(mini_file))
    problems = []
    if d["status"] != m["status"]:
        problems.append(f"status: dig {d['status']}, minidig {m['status']}")
    for flag in ("qr", "aa", "tc", "rd", "ra"):
        if (flag in d["flags"]) != (flag in m["flags"]):
            problems.append(f"flag {flag}: dig {d['flags']}, minidig {m['flags']}")
    for section in ("answer", "authority", "additional"):
        a, b = sorted(norm(r) for r in d[section]), sorted(norm(r) for r in m[section])
        if a != b:
            problems.append(f"{section}: dig {a} minidig {b}")
    if problems:
        sys.exit("; ".join(problems))
    print(f"{m['status']} {' '.join(m['flags'])}, {len(m['answer'])}/{len(m['authority'])}/{len(m['additional'])} records identical")


def walk(mini_file, port, root, name, qtype):
    result = json.load(open(mini_file))["results"][0]
    steps = [s for s in result["steps"] if s["depth"] == 0 and s["kind"] != "cache"]
    server, chain = root, []
    for _ in range(8):
        d = dig(port, server, name, qtype)
        ns = [r for r in d["authority"] if r["type"] == "NS"]
        soa = [r for r in d["authority"] if r["type"] == "SOA"]
        if d["status"] == "NOERROR" and d["answer"]:
            chain.append({"server": server, "kind": "answer", "zone": "", "records": d["answer"]})
            break
        if d["status"] == "NOERROR" and ns and not soa:
            targets = {r["data"].lower() for r in ns}
            glue = [r for r in d["additional"] if r["type"] in ("A", "AAAA") and r["name"].lower() in targets]
            chain.append({"server": server, "kind": "referral", "zone": ns[0]["name"].lower(), "records": ns + glue})
            server = next(r["data"] for r in glue if r["type"] == "A")
            continue
        chain.append({"server": server, "kind": "nodata" if d["status"] == "NOERROR" else d["status"].lower(), "zone": "", "records": soa})
        break
    if [(c["server"], c["kind"], c["zone"]) for c in chain] != [(s["server"], s["kind"], s["referral"].lower()) for s in steps]:
        sys.exit(f"chain differs: dig {[(c['server'], c['kind'], c['zone']) for c in chain]} minidig {[(s['server'], s['kind'], s['referral']) for s in steps]}")
    for number, (want, got) in enumerate(zip(chain, steps), 1):
        a, b = sorted(norm(r) for r in want["records"]), sorted(norm(r) for r in got["records"])
        if a != b:
            sys.exit(f"step {number} at {got['server']}: dig {a} minidig {b}")
        print(f"  step {number}: {got['server']} {got['kind']} {got['referral']} ({len(b)} records, same names, TTLs and data as dig)")


def trace(trace_file, *servers):
    received = [m.group(2) for m in (RECEIVED.match(line) for line in open(trace_file).read().splitlines()) if m]
    position = 0
    for address in received:
        if position < len(servers) and address == servers[position]:
            position += 1
    if position != len(servers):
        sys.exit(f"dig +trace received from {received}, expected {list(servers)} in that order")
    print(f"  dig +trace asked {' -> '.join(received)}")


def queries(log_file, start, substring, qtype):
    for line in open(log_file).read().splitlines()[int(start):]:
        m = LOG.search(line)
        if m and m.group(1) == qtype and substring in m.group(3).lower():
            print(f"{m.group(3).lower()} {m.group(4)} {m.group(8)}")


def wire(capture_file):
    # tcpdump -nn lines: "... > 127.0.10.10.53: 4660+ A? www.cc.test.svc.cluster.local. (55)"
    for line in open(capture_file).read().splitlines():
        m = re.search(r"\s\d+\+?(?: \[[^\]]*\])* A\? (\S+) \(", line)
        if m:
            print(m.group(1).lower())


def answers_equal(dig_file, mini_file, count):
    parsed = parse_dig(open(dig_file).read())
    a = sorted(norm(r) for r in parsed["answer"])
    b = sorted(norm(r) for r in json.load(open(mini_file))["answer"])
    if a != b or len(a) != int(count):
        sys.exit(f"dig has {len(a)} records, minidig {len(b)}, expected {count} identical ones")
    print(f"  dig and minidig hold the same {len(a)} records" + (", dig printed 'Truncated, retrying in TCP mode'" if parsed["retried_tcp"] else ""))


def soa_ttl(dig_file):
    m = re.search(r"^\S+\s+(\d+)\s+IN\s+SOA\s", open(dig_file).read(), re.M)
    if not m:
        sys.exit(f"no SOA in {dig_file}")
    print(m.group(1))


def same_names(names_file, plan_file, count):
    sent = [line.split()[0] for line in open(names_file).read().splitlines() if line.strip()]
    plan = [c["name"].lower() for c in json.load(open(plan_file))["candidates"]][: int(count)]
    if sent != plan:
        sys.exit(f"the resolver saw {sent}, minidig predicted {plan}")
    print("  " + "\n  ".join(f"{n + 1}. {name}" for n, name in enumerate(sent)))


commands = {"compare": compare, "walk": walk, "trace": trace, "queries": queries, "wire": wire, "answers-equal": answers_equal, "soa-ttl": soa_ttl, "same-names": same_names}
commands[sys.argv[1]](*sys.argv[2:])
PY

mark() { docker logs "$PREFIX-$1" > "$WORK/$1.log" 2>&1 || true; wc -l < "$WORK/$1.log" | tr -d ' '; }
wait_queries() {
  local role="$1" start="$2" substring="$3" qtype="$4" want="$5" out="$6"
  for _ in $(seq 1 40); do
    docker logs "$PREFIX-$role" > "$WORK/$role.log" 2>&1 || true
    python3 "$CHECK" queries "$WORK/$role.log" "$start" "$substring" "$qtype" > "$out"
    [[ "$(wc -l < "$out" | tr -d ' ')" -ge "$want" ]] && return 0
    sleep 0.25
  done
  return 1
}
field() {
  python3 - "$@" <<'PY'
import json, sys
value = json.load(open(sys.argv[1]))
for key in sys.argv[2:]:
    value = value[int(key)] if isinstance(value, list) else value[key]
print(json.dumps(value) if isinstance(value, (dict, list)) else value)
PY
}

echo "▸ minidig offline: the search plan for a pod-style resolv.conf (ndots:5, four search domains)"
mini search www.cc.test --resolv-conf "$FIX/resolv-pod.conf" --hostname "" --json > "$WORK/plan-ndots5.json" || fail "minidig search: $(cat "$WORK/plan-ndots5.json")"
[[ "$(field "$WORK/plan-ndots5.json" queries)" == "5" && "$(field "$WORK/plan-ndots5.json" candidates 4 name)" == "www.cc.test." ]] || fail "expected five candidates ending with the name as written: $(cat "$WORK/plan-ndots5.json")"
CODE=0
mini query @127.0.0.1 "$(printf 'a%.0s' $(seq 1 64)).test" > /dev/null 2> "$WORK/bad.err" || CODE=$?
[[ "$CODE" == "2" ]] && grep -q "at most 63" "$WORK/bad.err" || fail "a 64-octet label should exit 2: code $CODE, $(cat "$WORK/bad.err")"

if [[ "$(uname -s)" != "Linux" ]] || ! command -v docker >/dev/null || ! docker info >/dev/null 2>&1; then
  echo "  (Linux with Docker not available; the CoreDNS lab is skipped)"
  echo "SMOKE OK: networking/dns-resolver (minidig offline only)"
  exit 0
fi
HAVE_DIG=""
if command -v dig >/dev/null; then HAVE_DIG=1; else echo "  (dig not installed; comparisons with dig skipped)"; fi

echo "▸ choose the port: 53 unless something already listens on it for every address"
PORT=53
if command -v ss >/dev/null; then
  { ss -H -lnu; ss -H -lnt; } > "$WORK/listeners.txt" 2>/dev/null || true
  if grep -Eq '[[:space:]](\*|0\.0\.0\.0|\[::\]|127\.0\.10\.[0-9]+):53[[:space:]]' "$WORK/listeners.txt"; then PORT=1053; fi
fi
echo "  port $PORT"
cp -R "$FIX/lab" "$WORK/lab"
if [[ "$PORT" != "53" ]]; then sed -i -E "s/:53([^0-9]|$)/:$PORT\1/g" "$WORK"/lab/*.Corefile; fi
chmod 755 "$WORK"
chmod -R a+rX "$WORK/lab"

echo "▸ start four CoreDNS servers ($COREDNS_IMAGE): root, test., cc.test. and a caching resolver"
docker pull -q "$COREDNS_IMAGE" > /dev/null || fail "docker pull $COREDNS_IMAGE"
for role in root test cc resolver; do
  docker rm -f "$PREFIX-$role" > /dev/null 2>&1 || true
  docker run -d --name "$PREFIX-$role" --network host -v "$WORK/lab:/etc/coredns:ro" "$COREDNS_IMAGE" -conf "/etc/coredns/$role.Corefile" > /dev/null || fail "docker run $role"
  CONTAINERS+=("$PREFIX-$role")
done
ready() {
  for _ in $(seq 1 80); do
    mini query "@$2" "$3" SOA +norecurse --port "$PORT" --timeout 0.5 --tries 1 --json > "$WORK/ready.json" 2> /dev/null && [[ "$(field "$WORK/ready.json" status)" == "NOERROR" ]] && return 0
    sleep 0.25
  done
  fail "$1 on $2:$PORT never answered SOA for $3: $(docker logs "$PREFIX-$1" 2>&1 | tail -20)"
}
ready root "$ROOT" .
ready test "$TLD" test.
ready cc "$AUTH" cc.test.
ready resolver "$RESOLVER" cluster.local.

if [[ -n "$HAVE_DIG" ]]; then
  echo "▸ minidig query and dig +norecurse against the cc.test. server: every record type, the same sections"
  for spec in "www.cc.test A" "www.cc.test AAAA" "cc.test MX" "cc.test TXT" "_http._tcp.cc.test SRV" "cc.test SOA" "cc.test NS" "www.cc.test MX" "nope.cc.test A"; do
    read -r qname qtype <<< "$spec"
    mini query "@$AUTH" "$qname" "$qtype" +norecurse --port "$PORT" --json > "$WORK/q.json" || fail "minidig query $spec: $(cat "$WORK/q.json")"
    dig -p "$PORT" +norecurse "@$AUTH" "$qname" "$qtype" > "$WORK/q.txt" || fail "dig $spec: $(cat "$WORK/q.txt")"
    RESULT="$(python3 "$CHECK" compare "$WORK/q.txt" "$WORK/q.json" 2>&1)" || fail "$spec: $RESULT"
    echo "  $spec: $RESULT"
  done
fi

echo "▸ minidig resolves iteratively from the lab's root hints: two referrals with glue, then the answer"
mini resolve www.cc.test --hints "$WORK/lab/root.hints" --port "$PORT" --json > "$WORK/www.json" || fail "minidig resolve www.cc.test: $(cat "$WORK/www.json")"
[[ "$(field "$WORK/www.json" results 0 answer 0 data)" == "127.0.20.1" && "$(field "$WORK/www.json" results 0 queries)" == "3" ]] || fail "www.cc.test: $(cat "$WORK/www.json")"
mini resolve www.cc.test --hints "$WORK/lab/root.hints" --port "$PORT" --trace > "$WORK/www.trace" || fail "minidig --trace"
grep -q "Received .* from 127.0.10.1#$PORT(a.root.) .*referral to test." "$WORK/www.trace" || fail "trace should show the root's referral: $(cat "$WORK/www.trace")"
mini resolve nope.cc.test --hints "$WORK/lab/root.hints" --port "$PORT" --json > "$WORK/nope.json" && fail "nope.cc.test should exit 1"
[[ "$(field "$WORK/nope.json" results 0 status)" == "NXDOMAIN" ]] || fail "nope.cc.test: $(cat "$WORK/nope.json")"
if [[ -n "$HAVE_DIG" ]]; then
  echo "▸ the same walk with dig +norecurse, one server at a time, following dig's own glue"
  python3 "$CHECK" walk "$WORK/www.json" "$PORT" "$ROOT" www.cc.test A || fail "www.cc.test walk differs"
  python3 "$CHECK" walk "$WORK/nope.json" "$PORT" "$ROOT" nope.cc.test A || fail "nope.cc.test walk differs"
  dig -p "$PORT" +norecurse "@$AUTH" nope.cc.test A > "$WORK/nope-dig.txt"
  read -r SOA_TTL SOA_MIN < <(python3 -c 'import re,sys; t=open(sys.argv[1]).read(); m=re.search(r"^cc\.test\.\s+(\d+)\s+IN\s+SOA\s+\S+\s+\S+\s+\d+\s+\d+\s+\d+\s+\d+\s+(\d+)", t, re.M); print(m.group(1), m.group(2))' "$WORK/nope-dig.txt") || fail "no SOA in dig's NXDOMAIN: $(cat "$WORK/nope-dig.txt")"
  WANT=$(( SOA_TTL < SOA_MIN ? SOA_TTL : SOA_MIN ))
  [[ "$(field "$WORK/nope.json" results 0 negative_ttl)" == "$WANT" ]] || fail "negative TTL should be min($SOA_TTL, $SOA_MIN) = $WANT: $(field "$WORK/nope.json" results 0)"
  echo "  NXDOMAIN negative TTL $WANT = min(SOA TTL $SOA_TTL, MINIMUM $SOA_MIN), as dig's authority section says"
fi

echo "▸ a CNAME into another zone: the second walk starts at the cached test. delegation"
mini resolve alias.cc.test --root "a.root.=$ROOT" --port "$PORT" --json > "$WORK/alias.json" || fail "alias.cc.test: $(cat "$WORK/alias.json")"
python3 - "$WORK/alias.json" "$TLD" <<'PY' || fail "alias.cc.test: $(cat "$WORK/alias.json")"
import json, sys
r = json.load(open(sys.argv[1]))["results"][0]
answer = [(a["name"], a["type"], a["data"]) for a in r["answer"]]
assert answer == [("alias.cc.test.", "CNAME", "web.test."), ("web.test.", "A", "127.0.20.2")], answer
assert [s["kind"] for s in r["steps"]] == ["referral", "referral", "cname", "answer"], [s["kind"] for s in r["steps"]]
assert (r["steps"][-1]["zone"], r["steps"][-1]["server"]) == ("test.", sys.argv[2]), r["steps"][-1]
print("  alias.cc.test. CNAME web.test. A 127.0.20.2 in 4 queries; the last went straight to", sys.argv[2])
PY
if [[ -n "$HAVE_DIG" ]]; then
  dig -p "$PORT" +norecurse "@$AUTH" alias.cc.test A > "$WORK/alias-dig.txt"
  grep -Eq '^alias\.cc\.test\.[[:space:]]+300[[:space:]]+IN[[:space:]]+CNAME[[:space:]]+web\.test\.' "$WORK/alias-dig.txt" || fail "the cc.test. server should return only the CNAME: $(cat "$WORK/alias-dig.txt")"

  echo "▸ dig -4 +trace from the fake root"
  dig -4 -p "$PORT" +trace "@$ROOT" www.cc.test A > "$WORK/dig-trace.txt" 2>&1 || true
  if grep -q "^;; Received" "$WORK/dig-trace.txt" && grep -q "from $AUTH#" "$WORK/dig-trace.txt"; then
    python3 "$CHECK" trace "$WORK/dig-trace.txt" "$ROOT" "$TLD" "$AUTH" || fail "dig +trace order: $(cat "$WORK/dig-trace.txt")"
    grep -Eq '^www\.cc\.test\.[[:space:]]+300[[:space:]]+IN[[:space:]]+A[[:space:]]+127\.0\.20\.1' "$WORK/dig-trace.txt" || fail "dig +trace should end with the answer: $(cat "$WORK/dig-trace.txt")"
  else
    echo "  (dig +trace could not finish against the fake root: $(grep -m1 -E "couldn't|error|timed out" "$WORK/dig-trace.txt" || echo 'no Received lines'); the step-by-step walk above already matched dig)"
  fi
fi

echo "▸ truncation: 40 A records do not fit 512 bytes without EDNS, so the client retries over TCP"
START="$(mark cc)"
mini query "@$AUTH" big.cc.test A +noedns +norecurse --port "$PORT" --json > "$WORK/big-mini.json" || fail "minidig big: $(cat "$WORK/big-mini.json")"
[[ "$(field "$WORK/big-mini.json" transport)" == "tcp" && "$(field "$WORK/big-mini.json" truncated_udp)" == "True" ]] || fail "minidig should have fallen back to TCP: $(head -c 600 "$WORK/big-mini.json")"
python3 -c 'import json,sys; d=json.load(open(sys.argv[1])); assert len([a for a in d["answer"] if a["type"] == "A"]) == 40, len(d["answer"])' "$WORK/big-mini.json" || fail "minidig should have all 40 records over TCP"
wait_queries cc "$START" big.cc.test A 2 "$WORK/big-log.txt" || fail "CoreDNS should log big.cc.test over udp and then tcp: $(cat "$WORK/big-log.txt")"
[[ "$(awk '{print $2}' "$WORK/big-log.txt" | paste -sd, -)" == "udp,tcp" ]] || fail "expected udp then tcp in the cc.test. log: $(cat "$WORK/big-log.txt")"
echo "  CoreDNS log: $(paste -sd'|' "$WORK/big-log.txt")"
mini query "@$AUTH" big.cc.test A +norecurse --port "$PORT" --json > "$WORK/big-edns.json" || fail "minidig big with EDNS"
[[ "$(field "$WORK/big-edns.json" transport)" == "udp" && "$(field "$WORK/big-edns.json" size)" -gt 512 ]] || fail "with EDNS 1232 the answer should fit UDP: $(head -c 400 "$WORK/big-edns.json")"
echo "  with EDNS udp 1232: $(field "$WORK/big-edns.json" size) bytes over UDP"
if [[ -n "$HAVE_DIG" ]]; then
  dig -p "$PORT" +noedns +ignore +norecurse "@$AUTH" big.cc.test A > "$WORK/big-ignore.txt"
  grep -Eq '^;; flags:[^;]*\btc\b' "$WORK/big-ignore.txt" || fail "dig +noedns +ignore should show the tc flag: $(cat "$WORK/big-ignore.txt")"
  echo "  dig +noedns +ignore: $(grep -m1 '^;; flags:' "$WORK/big-ignore.txt")"
  dig -p "$PORT" +noedns +norecurse "@$AUTH" big.cc.test A > "$WORK/big-dig.txt"
  python3 "$CHECK" answers-equal "$WORK/big-dig.txt" "$WORK/big-mini.json" 40 || fail "dig and minidig over TCP should hold the same 40 records: $(cat "$WORK/big-dig.txt")"
fi

echo "▸ negative caching: minidig keeps NXDOMAIN for min(SOA TTL, MINIMUM) = 10 s; CoreDNS's cache uses the SOA's TTL, capped at 30 s"
CACHE="$WORK/cache.json"
START="$(mark cc)"
mini resolve nope.cc.test nope.cc.test --hints "$WORK/lab/root.hints" --port "$PORT" --cache-file "$CACHE" --json > "$WORK/nx1.json" && fail "NXDOMAIN should exit 1"
python3 - "$WORK/nx1.json" <<'PY' || fail "first run: $(cat "$WORK/nx1.json")"
import json, sys
first, second = json.load(open(sys.argv[1]))["results"]
assert (first["status"], first["from_cache"], first["negative_ttl"]) == ("NXDOMAIN", False, 10), first
assert (second["status"], second["from_cache"]) == ("NXDOMAIN", True), second
PY
mini resolve nope.cc.test --hints "$WORK/lab/root.hints" --port "$PORT" --cache-file "$CACHE" --json > "$WORK/nx2.json" && fail "NXDOMAIN should exit 1"
[[ "$(field "$WORK/nx2.json" results 0 from_cache)" == "True" ]] || fail "a second process should answer from the cache file: $(cat "$WORK/nx2.json")"
if [[ -n "$HAVE_DIG" ]]; then
  dig -p "$PORT" "@$RESOLVER" gone.cc.test A > "$WORK/fwd1.txt"
  dig -p "$PORT" "@$RESOLVER" gone.cc.test A > "$WORK/fwd2.txt"
  grep -q "status: NXDOMAIN" "$WORK/fwd2.txt" || fail "the resolver should return NXDOMAIN for gone.cc.test: $(cat "$WORK/fwd2.txt")"
fi
wait_queries cc "$START" nope.cc.test A 1 "$WORK/nx-log.txt" || fail "the cc.test. server never saw nope.cc.test"
sleep 1
mark cc > /dev/null
python3 "$CHECK" queries "$WORK/cc.log" "$START" nope.cc.test A > "$WORK/nx-log.txt"
[[ "$(wc -l < "$WORK/nx-log.txt" | tr -d ' ')" == "1" ]] || fail "three minidig lookups should have sent one query, the server saw: $(cat "$WORK/nx-log.txt")"
if [[ -n "$HAVE_DIG" ]]; then
  python3 "$CHECK" queries "$WORK/cc.log" "$START" gone.cc.test A > "$WORK/fwd-log.txt"
  [[ "$(wc -l < "$WORK/fwd-log.txt" | tr -d ' ')" == "1" ]] || fail "two digs through the caching resolver should reach the cc.test. server once: $(cat "$WORK/fwd-log.txt")"
fi
echo "  within 10 s: three minidig lookups and two digs through CoreDNS, one upstream query each; waiting out the SOA minimum"
sleep 11
mini resolve nope.cc.test --hints "$WORK/lab/root.hints" --port "$PORT" --cache-file "$CACHE" --json > "$WORK/nx3.json" && fail "NXDOMAIN should exit 1"
[[ "$(field "$WORK/nx3.json" results 0 from_cache)" == "False" && "$(field "$WORK/nx3.json" results 0 queries)" == "1" ]] || fail "after 10 s minidig should ask cc.test. again, once, from the cached delegation: $(cat "$WORK/nx3.json")"
wait_queries cc "$START" nope.cc.test A 2 "$WORK/nx-log.txt" || fail "the cc.test. server should see a second nope.cc.test query: $(cat "$WORK/nx-log.txt")"
echo "  after 11 s minidig asked the cc.test. server again"
if [[ -n "$HAVE_DIG" ]]; then
  dig -p "$PORT" "@$RESOLVER" gone.cc.test A > "$WORK/fwd3.txt"
  grep -q "status: NXDOMAIN" "$WORK/fwd3.txt" || fail "the resolver should still say NXDOMAIN: $(cat "$WORK/fwd3.txt")"
  LEFT="$(python3 "$CHECK" soa-ttl "$WORK/fwd3.txt")" || fail "no SOA in the resolver's NXDOMAIN: $(cat "$WORK/fwd3.txt")"
  sleep 1
  mark cc > /dev/null
  python3 "$CHECK" queries "$WORK/cc.log" "$START" gone.cc.test A > "$WORK/fwd-log.txt"
  [[ "$(wc -l < "$WORK/fwd-log.txt" | tr -d ' ')" == "1" && "$LEFT" -ge 1 && "$LEFT" -le 19 ]] || fail "CoreDNS should still serve gone.cc.test from cache (30 s cap, SOA TTL 3600) with at most 19 s left: SOA TTL $LEFT, upstream queries $(cat "$WORK/fwd-log.txt")"
  echo "  CoreDNS still answers from its cache, $LEFT s left: it read the SOA record's TTL (3600, capped by cache 30), not MINIMUM"
fi

if [[ "$PORT" == "53" ]]; then
  echo "▸ glibc's stub resolver ($GLIBC_IMAGE, getent ahostsv4) sends exactly the names minidig predicts"
  docker pull -q "$GLIBC_IMAGE" > /dev/null || fail "docker pull $GLIBC_IMAGE"
  printf 'hosts: files dns\n' > "$WORK/nsswitch.conf"
  sed 's/ndots:5/ndots:1/' "$FIX/resolv-pod.conf" > "$WORK/resolv-ndots1.conf"
  # CoreDNS logs a query after answering it, from its own goroutine, so two quick queries
  # can appear swapped in its log. The order glibc sent them in comes from the wire.
  WIRE=""
  if command -v tcpdump >/dev/null && $SUDO true 2>/dev/null; then WIRE=1; else echo "  (tcpdump with root not available: order taken from the CoreDNS log, which can swap two lines)"; fi
  start_wire() {
    rm -f "$WORK/wire.pid"
    $SUDO sh -c 'echo $$ > "$1"; shift; exec tcpdump "$@"' sh "$WORK/wire.pid" -i lo -nn -l --immediate-mode "udp and dst host $RESOLVER and dst port 53" > "$WORK/wire.txt" 2> "$WORK/wire.err" &
    for _ in $(seq 1 100); do
      grep -q "listening on" "$WORK/wire.err" 2>/dev/null && return 0
      sleep 0.1
    done
    fail "tcpdump did not start: $(cat "$WORK/wire.err")"
  }
  stop_wire() {
    $SUDO kill -INT "$(cat "$WORK/wire.pid")" 2>/dev/null || true
    for _ in $(seq 1 50); do
      grep -Eq "packets? captured" "$WORK/wire.err" && break
      sleep 0.1
    done
    grep -Eq "packets? captured" "$WORK/wire.err" || fail "tcpdump did not stop: $(cat "$WORK/wire.err")"
    rm -f "$WORK/wire.pid"
  }
  getent_in() {
    docker run --rm --network host -v "$1:/etc/resolv.conf:ro" -v "$WORK/nsswitch.conf:/etc/nsswitch.conf:ro" "$GLIBC_IMAGE" getent ahostsv4 "$2"
  }
  glibc_case() {
    local conf="$1" name="$2" want="$3" expect_found="$4" start code=0
    mini search "$name" --resolv-conf "$conf" --hostname "" --json > "$WORK/plan.json" || fail "minidig search $name"
    start="$(mark resolver)"
    if [[ -n "$WIRE" ]]; then start_wire; fi
    getent_in "$conf" "$name" > "$WORK/getent.txt" 2>&1 || code=$?
    if [[ "$expect_found" == "yes" ]]; then
      [[ "$code" == "0" && "$(awk 'NR==1 {print $1}' "$WORK/getent.txt")" == "127.0.20."* ]] || fail "getent $name should resolve (exit $code): $(cat "$WORK/getent.txt")"
    else
      [[ "$code" == "2" ]] || fail "getent $name should exit 2 for not found, got $code: $(cat "$WORK/getent.txt")"
    fi
    wait_queries resolver "$start" "$(echo "${name%.}" | cut -d. -f1)." A "$want" "$WORK/glibc.txt" || fail "the resolver should log $want A queries for $name: $(cat "$WORK/glibc.txt")"
    sleep 0.5
    if [[ -n "$WIRE" ]]; then
      stop_wire
      python3 "$CHECK" wire "$WORK/wire.txt" > "$WORK/glibc.txt"
    else
      docker logs "$PREFIX-resolver" > "$WORK/resolver.log" 2>&1
      python3 "$CHECK" queries "$WORK/resolver.log" "$start" "$(echo "${name%.}" | cut -d. -f1)." A > "$WORK/glibc.txt"
    fi
    python3 "$CHECK" same-names "$WORK/glibc.txt" "$WORK/plan.json" "$want" || fail "$name with $(grep -o 'ndots:[0-9]*' "$conf"): glibc and minidig disagree"
  }
  echo "  www.cc.test, ndots:5: four search names, then the name as written"
  glibc_case "$FIX/resolv-pod.conf" www.cc.test 5 yes
  echo "  www.cc.test, ndots:1: two dots >= 1, as written first, found"
  glibc_case "$WORK/resolv-ndots1.conf" www.cc.test 1 yes
  echo "  www.cc.test. with a trailing dot: no search at all"
  glibc_case "$FIX/resolv-pod.conf" www.cc.test. 1 yes
  echo "  absent.cc.test, ndots:1: as written first, NXDOMAIN, then every search name"
  glibc_case "$WORK/resolv-ndots1.conf" absent.cc.test 5 no
  echo "▸ minidig search --send against the same resolver stops where glibc stopped"
  START="$(mark resolver)"
  mini search www.cc.test --resolv-conf "$FIX/resolv-pod.conf" --hostname "" --send --json > "$WORK/send.json" || fail "minidig search --send: $(cat "$WORK/send.json")"
  [[ "$(field "$WORK/send.json" found)" == "True" ]] || fail "minidig search --send should find www.cc.test: $(cat "$WORK/send.json")"
  python3 -c 'import json,sys; d=json.load(open(sys.argv[1])); s=[x["status"] for x in d["sent"]]; assert s == ["NXDOMAIN"]*4 + ["NOERROR"], s; print("  " + ", ".join(s))' "$WORK/send.json" || fail "minidig --send statuses: $(cat "$WORK/send.json")"
  wait_queries resolver "$START" www.cc.test A 5 "$WORK/send-log.txt" || fail "the resolver should log minidig's five queries: $(cat "$WORK/send-log.txt")"
else
  echo "  (port 53 is taken on this host; glibc can only use port 53, so the getent comparison is skipped)"
fi

echo "▸ the host's resolver configuration, read by minidig, getent and resolvectl"
if [[ -r /etc/resolv.conf ]]; then
  mini search www --resolv-conf /etc/resolv.conf --json > "$WORK/host.json" || fail "minidig cannot read /etc/resolv.conf: $(cat "$WORK/host.json")"
  echo "  nameservers $(field "$WORK/host.json" nameservers), search $(field "$WORK/host.json" search), ndots $(field "$WORK/host.json" ndots): 'www' is tried as $(field "$WORK/host.json" queries) name(s)"
fi
getent hosts localhost > "$WORK/localhost.txt" || fail "getent hosts localhost failed"
echo "  getent hosts localhost: $(head -1 "$WORK/localhost.txt")"
grep '^hosts:' /etc/nsswitch.conf > "$WORK/nsswitch-host.txt" 2>/dev/null && echo "  nsswitch: $(cat "$WORK/nsswitch-host.txt")" || echo "  (no hosts: line in /etc/nsswitch.conf)"
if command -v resolvectl >/dev/null && resolvectl status > "$WORK/resolvectl.txt" 2>&1; then
  grep -m3 -E 'DNS Servers|Current DNS Server|resolv.conf mode' "$WORK/resolvectl.txt" | sed 's/^ */  resolvectl: /' || true
else
  echo "  (resolvectl not available or systemd-resolved not running; skipped)"
fi

if ! command -v kubectl >/dev/null; then
  echo "  (kubectl not installed; the pod resolv.conf check is skipped)"
  echo "SMOKE OK: networking/dns-resolver"
  exit 0
fi
kubectl config get-contexts -o name > "$WORK/contexts.txt" 2>/dev/null || true
if ! grep -qx "$CTX" "$WORK/contexts.txt"; then
  echo "  (context $CTX not found; the pod resolv.conf check is skipped)"
  echo "SMOKE OK: networking/dns-resolver"
  exit 0
fi

echo "▸ a pod's resolv.conf on $CTX: ndots:5 and the namespace's search list, expanded by minidig"
k -n kube-system get configmap coredns -o jsonpath='{.data.Corefile}' > "$WORK/cluster-Corefile" || fail "read the coredns ConfigMap"
grep -q "kubernetes cluster.local" "$WORK/cluster-Corefile" && grep -q "forward \." "$WORK/cluster-Corefile" || fail "unexpected cluster Corefile: $(cat "$WORK/cluster-Corefile")"
DNS_IP="$(k -n kube-system get svc kube-dns -o jsonpath='{.spec.clusterIP}')"
k delete namespace "$NS" --ignore-not-found --wait=true --timeout=90s > /dev/null 2>&1 || true
NS_CREATED=1
k create namespace "$NS" > /dev/null || fail "create namespace $NS"
for _ in $(seq 1 30); do k -n "$NS" get serviceaccount default > /dev/null 2>&1 && break; sleep 1; done
k -n "$NS" run resolvconf --image="$POD_IMAGE" --restart=Never -- cat /etc/resolv.conf > /dev/null || fail "kubectl run resolvconf"
k -n "$NS" wait --for=jsonpath='{.status.phase}'=Succeeded pod/resolvconf --timeout=120s > /dev/null || fail "resolvconf pod did not finish: $(k -n "$NS" get pod resolvconf -o wide 2>&1)"
k -n "$NS" logs resolvconf > "$WORK/pod-resolv.conf" || fail "kubectl logs resolvconf"
mini search api.example.com --resolv-conf "$WORK/pod-resolv.conf" --hostname "" --json > "$WORK/pod-plan.json" || fail "minidig search on the pod resolv.conf: $(cat "$WORK/pod-resolv.conf")"
python3 - "$WORK/pod-plan.json" "$NS" "$DNS_IP" <<'PY' || fail "pod resolv.conf: $(cat "$WORK/pod-resolv.conf")"
import json, sys
d = json.load(open(sys.argv[1]))
assert d["ndots"] == 5, d["ndots"]
assert d["search"][:3] == [f"{sys.argv[2]}.svc.cluster.local", "svc.cluster.local", "cluster.local"], d["search"]
assert d["nameservers"][0] == sys.argv[3], (d["nameservers"], sys.argv[3])
assert d["queries"] == len(d["search"]) + 1 and d["candidates"][-1]["name"] == "api.example.com.", d["candidates"]
print(f"  nameserver {sys.argv[3]} (kube-dns), search {' '.join(d['search'])}: api.example.com costs {d['queries']} names, {2 * d['queries']} packets with AAAA")
PY
k -n "$NS" delete pod resolvconf --wait=false > /dev/null 2>&1 || true

echo "SMOKE OK: networking/dns-resolver"
