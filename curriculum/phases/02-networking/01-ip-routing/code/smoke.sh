#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."

fail() { echo "SMOKE FAIL: $*" >&2; exit 1; }
[[ "$(uname -s)" == "Linux" ]] || fail "needs Linux (network namespaces, veth pairs, policy routing)"
command -v ip >/dev/null || fail "iproute2 (ip) is not installed"
SUDO=""
[[ "$(id -u)" == "0" ]] || SUDO="sudo"
export RT_PREFIX="cc-ip-routing"
C="$RT_PREFIX-c"
R="$RT_PREFIX-r"
U1="$RT_PREFIX-u1"
U2="$RT_PREFIX-u2"
WORK="$(mktemp -d)"
PIDS=()
lab() { $SUDO env RT_PREFIX="$RT_PREFIX" bash code/main.sh "$@"; }
rl() { python3 code/main.py "$@"; }
cleanup() {
  for pid in ${PIDS[@]+"${PIDS[@]}"}; do $SUDO kill "$pid" 2>/dev/null || true; done
  lab down >/dev/null 2>&1 || true
  $SUDO rm -rf "$WORK"
}
trap cleanup EXIT

cat > "$WORK/compare.py" <<'PY'
import json, sys
mine_path, out_path, err_path, code, soft, ns = sys.argv[1:7]
label = f"{ns}: ip " + " ".join(sys.argv[7:])
mine = json.load(open(mine_path))
err = open(err_path).read().strip()
if not mine["ok"]:
    if code == "0":
        print(f"{label}: routelens says {mine['error']} ({mine['errno']}, {mine['reason']}) but the kernel answered {open(out_path).read().strip()}")
        sys.exit(1)
    if mine["error"] not in err:
        if soft:
            print(f"  {label}: both refuse; kernel {err!r}, routelens {mine['error']!r} (note: wording differs)")
            sys.exit(0)
        print(f"{label}: both refuse, but the kernel says {err!r} and routelens {mine['error']!r} ({mine['reason']})")
        sys.exit(1)
    print(f"  {label}: both refuse: {mine['error']} ({mine['errno']})")
    sys.exit(0)
if code != "0":
    print(f"{label}: routelens chose {mine['route']} but the kernel refused: {err}")
    sys.exit(1)
real = json.load(open(out_path))[0]
got = {"type": real.get("type", "unicast"), "dev": real.get("dev"), "gateway": real.get("gateway"),
       "src": real.get("prefsrc") or real.get("from") or real.get("src"), "table": str(real.get("table", "main"))}
hops = mine["nexthops"]
match = [h for h in hops if (h["gateway"], h["dev"]) == (got["gateway"], got["dev"])]
hop = match[0] if match else {"gateway": mine["gateway"], "dev": mine["dev"], "src": mine["src"]}
want = {"type": mine["type"], "dev": hop["dev"], "gateway": hop["gateway"], "src": hop["src"] or mine["src"], "table": mine["table"] or "main"}
if not match or want != got:
    print(f"{label}: kernel {got} but routelens {want} ({mine['route']}; next hops {hops}; trace {mine['trace']})")
    sys.exit(1)
extra = f" (ECMP: the kernel's hash picked 1 of {len(hops)} next hops routelens listed)" if len(hops) > 1 else ""
print(f"  {label}: {got['type']} dev {got['dev']} via {got['gateway'] or '-'} src {got['src']} table {got['table']}{extra}")
PY

compare() {
  local snap="$1" ns="$2" dst="$3" src="$4" iif="$5" mark="$6" soft="${7:-}"
  local -a ipargs=(route get "$dst") rlargs=(get "$dst" --dir "$snap" --json)
  if [[ -n "$src" ]]; then ipargs+=(from "$src"); rlargs+=(--from "$src"); fi
  if [[ -n "$iif" ]]; then ipargs+=(iif "$iif"); rlargs+=(--iif "$iif"); fi
  if [[ -n "$mark" ]]; then ipargs+=(mark "$mark"); rlargs+=(--mark "$mark"); fi
  local code=0 rc=0
  $SUDO ip -n "$ns" -j "${ipargs[@]}" > "$WORK/ip.out" 2> "$WORK/ip.err" || code=$?
  rl "${rlargs[@]}" > "$WORK/rl.json" 2> "$WORK/rl.err" || rc=$?
  [[ "$rc" != "2" ]] || fail "routelens rejected the query ${rlargs[*]}: $(cat "$WORK/rl.err")"
  if ! python3 "$WORK/compare.py" "$WORK/rl.json" "$WORK/ip.out" "$WORK/ip.err" "$code" "$soft" "$ns" "${ipargs[@]}" > "$WORK/cmp.txt"; then
    fail "$(cat "$WORK/cmp.txt")"
  fi
  cat "$WORK/cmp.txt"
}
matrix() {
  local snap="$1" ns="$2" file="$3" dst src iif mark
  while IFS='|' read -r dst src iif mark <&3; do
    if [[ -n "$dst" ]]; then compare "$snap" "$ns" "$dst" "$src" "$iif" "$mark"; fi
  done 3< "$file"
}
json_field() {
  python3 - "$@" <<'PY'
import json, sys
value = json.load(open(sys.argv[1]))
for key in sys.argv[2:]:
    value = value[int(key)] if isinstance(value, list) else value[key]
print(json.dumps(value) if isinstance(value, (dict, list)) else value)
PY
}

echo "▸ routelens CIDR arithmetic (pure, no root)"
rl summarise 10.0.0.0/24 10.0.1.0/24 10.0.3.0/24 --json > "$WORK/sum.json"
[[ "$(json_field "$WORK/sum.json" supernet)" == "10.0.0.0/22" && "$(json_field "$WORK/sum.json" extra_addresses)" == "256" ]] || fail "summarise: $(cat "$WORK/sum.json")"
rl contains 10.0.0.0/16 10.0.3.7 > /dev/null || fail "10.0.3.7 should be in 10.0.0.0/16"
if rl contains 10.0.0.0/16 10.1.0.1 > /dev/null; then fail "10.1.0.1 is not in 10.0.0.0/16"; fi
echo "  summarise and contains ok"

echo "▸ main.sh up: four namespaces, three veth pairs, a dummy, table 100, rules, ECMP, blackhole"
lab down > /dev/null 2>&1 || true
lab up | tee "$WORK/up.txt"
lab status > "$WORK/status.txt" || fail "status: $(cat "$WORK/status.txt")"
grep -q "from 10.0.1.20 lookup 100" "$WORK/status.txt" || fail "rule 100 missing: $(cat "$WORK/status.txt")"
$SUDO ip -n "$C" route show table 100 > "$WORK/t100.txt"
grep -q "10.0.3.0/24 dev c-dum" "$WORK/t100.txt" || fail "table 100 on the client: $(cat "$WORK/t100.txt")"
[[ "$($SUDO ip netns exec "$R" cat /proc/sys/net/ipv4/ip_forward)" == "1" ]] || fail "ip_forward is not 1 in $R"

echo "▸ snapshots: routelens snapshot (Python) and main.sh snapshot (bash) read the same kernel state"
$SUDO python3 code/main.py snapshot "$WORK/c" --netns "$C" | sed 's/^/  /'
lab snapshot "$C" "$WORK/c-sh" > /dev/null
lab snapshot "$R" "$WORK/r" | sed 's/^/  /'
$SUDO python3 code/main.py snapshot "$WORK/u1" --netns "$U1" > /dev/null
python3 - "$WORK/c" "$WORK/c-sh" > "$WORK/same.txt" <<'PY' || fail "the two snapshot paths disagree: $(cat "$WORK/same.txt")"
import sys
sys.path.insert(0, "code")
import main
a, b = (main.build_snapshot(main.read_dir(d)) for d in sys.argv[1:3])
views = lambda s: (sorted(r.describe() for r in s.routes if r.prefix.version == 4), [r.describe() for r in s.rules[4]], sorted(i.name for i in s.interfaces), s.sysctl)
if views(a) != views(b):
    print(views(a), views(b))
    raise SystemExit(1)
print(f"  identical: {len(views(a)[0])} IPv4 routes, {len(views(a)[1])} rules, {len(a.sysctl)} sysctls")
PY
cat "$WORK/same.txt"

echo "▸ client: routelens answers every destination exactly as ip route get (device, gateway, source, table)"
cat > "$WORK/client.txt" <<'M'
10.0.1.1|||
10.0.2.10|||
10.0.3.10|||
198.51.100.1|||
198.51.100.1|10.0.1.20||
10.0.3.10|10.0.1.20||
198.51.100.1|||0x1
10.99.0.1|||
10.66.0.1|||
10.0.1.10|||
10.0.1.20|||
172.16.0.77|||
10.0.5.1|||
M
matrix "$WORK/c" "$C" "$WORK/client.txt"

echo "▸ router: forwarded packets (iif r-c), the source rule, the more specific route, ECMP, blackhole, local delivery"
cat > "$WORK/router.txt" <<'M'
198.51.100.1|10.0.1.10|r-c|
198.51.100.1|10.0.1.20|r-c|
203.0.113.1|10.0.1.10|r-c|
10.0.2.10|10.0.1.10|r-c|
10.0.2.10|10.0.1.20|r-c|
10.99.0.1|10.0.1.10|r-c|
10.0.1.1|10.0.1.10|r-c|
192.0.2.7|10.0.1.10|r-c|
198.51.100.1|||
M
matrix "$WORK/r" "$R" "$WORK/router.txt"
rl get 10.0.2.10 --from 10.0.1.20 --iif r-c --dir "$WORK/r" --json > "$WORK/gotcha.json"
[[ "$(json_field "$WORK/gotcha.json" table)" == "100" && "$(json_field "$WORK/gotcha.json" dev)" == "r-u2" ]] || fail "the policy table should shadow the connected route: $(cat "$WORK/gotcha.json")"

echo "▸ upstream 1 has no default route: Network is unreachable for anything it does not know"
cat > "$WORK/u1.txt" <<'M'
203.0.113.9|||
10.0.1.10|||
M
matrix "$WORK/u1" "$U1" "$WORK/u1.txt"

echo "▸ Use It: rules, fibmatch and live routelens on the namespaces"
$SUDO ip -n "$C" route get fibmatch 10.0.2.10 > "$WORK/fibmatch.txt" || fail "ip route get fibmatch: $(cat "$WORK/fibmatch.txt")"
grep -q "10.0.0.0/16 via 10.0.1.1 dev c0" "$WORK/fibmatch.txt" && grep -q "metric 50" "$WORK/fibmatch.txt" || fail "fibmatch should show the metric 50 route: $(cat "$WORK/fibmatch.txt")"
$SUDO python3 code/main.py rules --netns "$C" > "$WORK/rules.txt"
grep -q "100: from 10.0.1.20 lookup 100" "$WORK/rules.txt" && grep -q "110: from all fwmark 0x1 lookup 100" "$WORK/rules.txt" || fail "routelens rules --netns: $(cat "$WORK/rules.txt")"
$SUDO python3 code/main.py get 10.0.2.10 --from 10.0.1.20 --iif r-c --netns "$R" > "$WORK/live.txt" || fail "live get: $(cat "$WORK/live.txt")"
head -1 "$WORK/live.txt" | sed 's/^/  live: /'

if command -v ping >/dev/null; then
  echo "▸ ping from the client: the predicted path works, the blackhole and the unreachable route fail"
  $SUDO ip netns exec "$C" ping -c1 -W2 198.51.100.1 > "$WORK/ping.txt" 2>&1 || fail "ping 198.51.100.1 through the router: $(cat "$WORK/ping.txt")"
  $SUDO ip netns exec "$C" ping -c1 -W2 -I 10.0.1.20 198.51.100.1 > "$WORK/ping2.txt" 2>&1 || fail "ping from 10.0.1.20 through table 100: $(cat "$WORK/ping2.txt")"
  if $SUDO ip netns exec "$C" ping -c1 -W1 10.99.0.1 > "$WORK/hole.txt" 2>&1; then fail "ping into the blackhole succeeded: $(cat "$WORK/hole.txt")"; fi
  if $SUDO ip netns exec "$C" ping -c1 -W1 10.66.0.1 > "$WORK/unreach.txt" 2>&1; then fail "ping to the unreachable route succeeded: $(cat "$WORK/unreach.txt")"; fi
  echo "  blackhole: $(head -1 "$WORK/hole.txt")"
  echo "  unreachable: $(head -1 "$WORK/unreach.txt")"
else
  echo "  (ping not installed; ping checks skipped)"
fi

if command -v curl >/dev/null; then
  echo "▸ traffic: both upstreams own 198.51.100.1, so the answer names the path the router chose"
  for u in u1 u2; do mkdir -p "$WORK/www-$u"; echo "$u" > "$WORK/www-$u/who"; done
  $SUDO ip netns exec "$U1" python3 -m http.server 8080 --bind 0.0.0.0 --directory "$WORK/www-u1" > /dev/null 2>&1 &
  PIDS+=("$!")
  $SUDO ip netns exec "$U2" python3 -m http.server 8080 --bind 0.0.0.0 --directory "$WORK/www-u2" > /dev/null 2>&1 &
  PIDS+=("$!")
  ready=""
  for _ in $(seq 1 50); do
    if $SUDO ip netns exec "$C" curl -s -m 1 -o /dev/null http://198.51.100.1:8080/who && $SUDO ip netns exec "$C" curl -s -m 1 -o /dev/null http://203.0.113.1:8080/who; then ready=1; break; fi
    sleep 0.2
  done
  [[ -n "$ready" ]] || fail "the upstream web servers never answered through the router"
  expect_path() {
    local label="$1" dst="$2" src="$3" curl_src="$4" predicted served
    rl get "$dst" --from "$src" --iif r-c --dir "$WORK/r" --json > "$WORK/path.json"
    case "$(json_field "$WORK/path.json" dev)" in r-u1) predicted=u1 ;; r-u2) predicted=u2 ;; *) fail "unexpected prediction: $(cat "$WORK/path.json")" ;; esac
    if [[ -n "$curl_src" ]]; then
      served="$($SUDO ip netns exec "$C" curl -s -m 3 --interface "$curl_src" "http://$dst:8080/who" || true)"
    else
      served="$($SUDO ip netns exec "$C" curl -s -m 3 "http://$dst:8080/who" || true)"
    fi
    [[ "$served" == "$predicted" ]] || fail "$label: routelens predicted $predicted, the answer came from '$served'"
    echo "  $label: routelens predicted $predicted, served by $served"
  }
  expect_path "198.51.100.1 from 10.0.1.10 (default route)" 198.51.100.1 10.0.1.10 ""
  expect_path "198.51.100.1 from 10.0.1.20 (rule 100, table 100)" 198.51.100.1 10.0.1.20 10.0.1.20
  expect_path "203.0.113.1 from 10.0.1.10 (more specific route)" 203.0.113.1 10.0.1.10 ""
else
  echo "  (curl not installed; path-by-traffic checks skipped)"
fi

if command -v tracepath >/dev/null; then
  echo "▸ tracepath from the client lists the router as hop 1"
  $SUDO ip netns exec "$C" tracepath -n -m 4 198.51.100.1 > "$WORK/tracepath.txt" 2>&1 || true
  grep -q "10.0.1.1" "$WORK/tracepath.txt" || fail "tracepath should show 10.0.1.1: $(cat "$WORK/tracepath.txt")"
  sed 's/^/  /' "$WORK/tracepath.txt"
else
  echo "  (tracepath not installed; trace skipped)"
fi

echo "▸ neighbour: a route via 10.0.1.250, which does not exist, is chosen and then fails ARP"
$SUDO ip netns exec "$C" ping -c1 -W1 10.0.5.1 > /dev/null 2>&1 || true
state=""
for _ in $(seq 1 40); do
  state="$($SUDO ip -n "$C" neigh show 10.0.1.250 dev c0)"
  [[ "$state" == *FAILED* ]] && break
  sleep 0.25
done
echo "  ip neigh: ${state:-no entry}"
[[ "$state" == *FAILED* || "$state" == *INCOMPLETE* ]] || fail "10.0.1.250 should be FAILED or INCOMPLETE, got: ${state:-no entry}"
$SUDO python3 code/main.py snapshot "$WORK/c2" --netns "$C" > /dev/null
rl get 10.0.5.1 --dir "$WORK/c2" --json > "$WORK/neigh.json"
[[ "$(json_field "$WORK/neigh.json" neighbour_ok)" == "False" ]] || fail "routelens should flag the next hop: $(cat "$WORK/neigh.json")"
compare "$WORK/c2" "$C" 10.0.5.1 "" "" ""

echo "▸ rp_filter on the router: strict rejects a source whose way back is another interface, loose accepts it"
$SUDO ip netns exec "$R" sysctl -qw net.ipv4.conf.r-c.rp_filter=1
lab snapshot "$R" "$WORK/r-strict" > /dev/null
compare "$WORK/r-strict" "$R" 198.51.100.1 10.0.2.10 r-c ""
compare "$WORK/r-strict" "$R" 198.51.100.1 10.0.1.10 r-c ""
$SUDO ip netns exec "$R" sysctl -qw net.ipv4.conf.r-c.rp_filter=2
lab snapshot "$R" "$WORK/r-loose" > /dev/null
compare "$WORK/r-loose" "$R" 198.51.100.1 10.0.2.10 r-c ""
$SUDO ip netns exec "$R" sysctl -qw net.ipv4.conf.r-c.rp_filter=0

echo "▸ forwarding off on the router: the lookup refuses forwarded packets and the client's ping dies"
$SUDO ip netns exec "$R" sysctl -qw net.ipv4.ip_forward=0
lab snapshot "$R" "$WORK/r-off" > /dev/null
compare "$WORK/r-off" "$R" 198.51.100.1 10.0.1.10 r-c "" soft
compare "$WORK/r-off" "$R" 10.0.1.1 10.0.1.10 r-c ""
if command -v ping >/dev/null; then
  if $SUDO ip netns exec "$C" ping -c1 -W1 198.51.100.1 > "$WORK/noforward.txt" 2>&1; then fail "ping should fail with forwarding off: $(cat "$WORK/noforward.txt")"; fi
fi
$SUDO ip netns exec "$R" sysctl -qw net.ipv4.ip_forward=1
if command -v ping >/dev/null; then
  $SUDO ip netns exec "$C" ping -c1 -W2 198.51.100.1 > "$WORK/forward.txt" 2>&1 || fail "ping should work again with forwarding on: $(cat "$WORK/forward.txt")"
fi

echo "▸ main.sh down removes every namespace"
lab down | sed 's/^/  /'
$SUDO ip netns list > "$WORK/netns.txt"
if grep -q "$RT_PREFIX" "$WORK/netns.txt"; then fail "namespaces survived down: $(cat "$WORK/netns.txt")"; fi

echo "SMOKE OK: networking/ip-routing"
