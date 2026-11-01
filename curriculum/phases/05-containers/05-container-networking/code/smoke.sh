#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."

fail() { echo "SMOKE FAIL: $*" >&2; exit 1; }
[[ "$(uname -s)" == "Linux" ]] || fail "needs Linux (network namespaces, veth, netfilter)"
SUDO=""
[[ "$(id -u)" == "0" ]] || SUDO="sudo"
WORK="$(mktemp -d)"
STATE="$WORK/state"
PIDS=()
ccnet() { $SUDO env CCNET_STATE="$STATE" bash code/main.sh "$@"; }
ccpy() { $SUDO env CCNET_STATE="$STATE" python3 code/main.py "$@"; }
cleanup() {
  if [[ ${#PIDS[@]} -gt 0 ]]; then $SUDO kill "${PIDS[@]}" 2>/dev/null || true; fi
  docker rm -f cc-smoke-web >/dev/null 2>&1 || true
  ccnet purge >/dev/null 2>&1 || true
  $SUDO rm -rf "$WORK"
}
trap cleanup EXIT
wait_http() { for _ in $(seq 1 50); do curl -s -m 1 "$1" >/dev/null 2>&1 && return 0; sleep 0.2; done; fail "nothing answered at $1"; }

if command -v nft >/dev/null; then BACKEND=nft; else BACKEND=iptables; fi
echo "▸ plan renders for both backends without touching the kernel"
python3 code/main.py --state "$WORK/plan.json" plan up web --ip 10.90.0.2 --publish 18080:80 --backend nft | grep -q 'dnat to 10.90.0.2:80' || fail "nft plan"
python3 code/main.py --state "$WORK/plan.json" plan up web --ip 10.90.0.2 --publish 18080:80 --backend iptables | grep -q 'DNAT --to-destination 10.90.0.2:80' || fail "iptables plan"
ccnet up web --publish 18080:80 --dry-run > "$WORK/dry.txt"
grep -q "ip link add veth-web type veth peer name ceth-web" "$WORK/dry.txt" || fail "--dry-run"
echo "  backend on this host: $BACKEND"

echo "▸ up web --publish 18080:80: bridge, netns, veth pair, addresses, NAT"
ccnet up web --publish 18080:80 | tee "$WORK/up.txt"
grep -q "10.90.0.2/24 via 10.90.0.1 on cc0" "$WORK/up.txt" || fail "up summary"
$SUDO ip netns list | grep -qw web || fail "netns web missing"
ip -o link show veth-web | grep -q "master cc0" || fail "veth-web is not attached to cc0"
$SUDO ip -n web -br addr show eth0 | grep -q "10.90.0.2/24" || fail "eth0 address inside the namespace"
$SUDO ip -n web route | grep -q "default via 10.90.0.1" || fail "default route inside the namespace"
if [[ "$BACKEND" == nft ]]; then
  $SUDO nft list chain ip ccnet publish | grep -q "tcp dport 18080 dnat to 10.90.0.2:80" || fail "nft DNAT rule"
  $SUDO nft list chain ip ccnet postrouting | grep -q "masquerade" || fail "nft masquerade rule"
else
  $SUDO iptables -t nat -S CCNET | grep -q -- "--dport 18080 .* --to-destination 10.90.0.2:80" || fail "iptables DNAT rule"
  $SUDO iptables -t nat -S POSTROUTING | grep -q "10.90.0.0/24 ! -o cc0 -j MASQUERADE" || fail "iptables masquerade rule"
fi

echo "▸ a server inside the namespace answers on the published port from the host"
ccnet exec web -- python3 -m http.server 80 --directory "$WORK" >/dev/null 2>&1 &
PIDS+=("$!")
wait_http http://localhost:18080/
curl -s localhost:18080/ | grep -q "Directory listing" || fail "curl localhost:18080 did not reach the server in the namespace"
HOST_IP="$(ip -4 route get 1.1.1.1 2>/dev/null | awk '{for (i = 1; i <= NF; i++) if ($i == "src") print $(i + 1)}' | head -n1 || true)"
if [[ -n "$HOST_IP" ]]; then
  curl -s "http://$HOST_IP:18080/" | grep -q "Directory listing" || fail "published port not reachable on the host address $HOST_IP"
  echo "  reachable on localhost and on $HOST_IP"
fi

echo "▸ explain web agrees with the plumbing (ip -j, $BACKEND)"
ccpy explain web | tee "$WORK/explain.txt"
grep -q "all 8 checks passed" "$WORK/explain.txt" || fail "explain reported a failing check"
ccpy explain web --json > "$WORK/explain.json"
python3 - "$WORK/explain.json" <<'PY' || fail "explain --json"
import json, sys
d = json.load(open(sys.argv[1]))
assert d["ok"], d
assert any("DNAT to 10.90.0.2:80" in c["text"] for c in d["checks"]), d
print("  json ok:", len(d["checks"]), "checks")
PY

echo "▸ the namespace reaches the gateway, has its own resolv.conf, and leaves through MASQUERADE"
python3 -m http.server 18090 --bind 10.90.0.1 --directory "$WORK" >/dev/null 2>&1 &
PIDS+=("$!")
wait_http http://10.90.0.1:18090/
ccnet exec web -- curl -s -m 3 http://10.90.0.1:18090/ | grep -q "Directory listing" || fail "container cannot reach the gateway address"
ccnet exec web -- cat /etc/resolv.conf | grep -q "^nameserver" || fail "/etc/netns/web/resolv.conf not mounted by ip netns exec"
if curl -s -m 5 -o /dev/null http://1.1.1.1/; then
  CODE="$(ccnet exec web -- curl -s -m 8 -o /dev/null -w '%{http_code}' http://1.1.1.1/ || true)"
  [[ "$CODE" =~ ^[0-9]{3}$ ]] || fail "egress from the namespace failed (http code '$CODE'); check ip_forward, the masquerade rule and the FORWARD policy"
  echo "  egress ok: http://1.1.1.1 answered $CODE from inside the namespace"
else
  echo "  (host has no route to 1.1.1.1; egress check skipped)"
fi

echo "▸ a second container on the same bridge talks to the first by address"
ccnet up db | tee "$WORK/up2.txt"
grep -q "10.90.0.3/24" "$WORK/up2.txt" || fail "second address should be 10.90.0.3"
ccnet exec db -- curl -s -m 3 http://10.90.0.2:80/ | grep -q "Directory listing" || fail "db cannot reach web over cc0"
ccnet ls | tee "$WORK/ls.txt"
grep -q "veth-db" "$WORK/ls.txt" || fail "ls should list db"
grep -q "18080:80/tcp" "$WORK/ls.txt" || fail "ls should list the published port"

echo "▸ down removes the namespace, the veth, the DNAT rule and the address; purge removes the bridge and the table"
$SUDO kill "${PIDS[0]}" 2>/dev/null || true
ccnet down web | tee "$WORK/down.txt"
grep -q "released 10.90.0.2 from web" "$WORK/down.txt" || fail "down web"
$SUDO ip netns list | grep -qw web && fail "netns web survived down"
ip link show veth-web >/dev/null 2>&1 && fail "veth-web survived down"
if [[ "$BACKEND" == nft ]]; then
  $SUDO nft list chain ip ccnet publish | grep -q 18080 && fail "DNAT rule survived down"
else
  $SUDO iptables -t nat -S CCNET | grep -q 18080 && fail "DNAT rule survived down"
fi
ccnet down db >/dev/null
ccnet purge | tee "$WORK/purge.txt"
ip link show cc0 >/dev/null 2>&1 && fail "bridge cc0 survived purge"
if [[ "$BACKEND" == nft ]]; then
  $SUDO nft list table ip ccnet >/dev/null 2>&1 && fail "nft table ccnet survived purge"
else
  $SUDO iptables -t nat -nL CCNET >/dev/null 2>&1 && fail "iptables chain CCNET survived purge"
fi
echo "  clean"

if command -v docker >/dev/null; then
  echo "▸ Use It: docker -p does the same job; verify it with explain through nsenter"
  docker run -d --rm --name cc-smoke-web -p 18081:80 python:3.12-alpine python3 -m http.server 80 >/dev/null
  wait_http http://localhost:18081/
  curl -s localhost:18081/ | grep -q "Directory listing" || fail "docker -p 18081:80"
  PID="$(docker inspect -f '{{.State.Pid}}' cc-smoke-web)"
  DIP="$(docker inspect -f '{{.NetworkSettings.IPAddress}}' cc-smoke-web)"
  GW="$(docker network inspect bridge -f '{{(index .IPAM.Config 0).Gateway}}')"
  DBR="$(docker network inspect bridge -f '{{index .Options "com.docker.network.bridge.name"}}')"
  D="$WORK/docker"
  mkdir -p "$D"
  $SUDO nsenter -t "$PID" -n ip -j addr > "$D/addr.json"
  $SUDO nsenter -t "$PID" -n ip -j route > "$D/route.json"
  ip -j addr > "$D/host.json"
  if command -v iptables-save >/dev/null; then $SUDO iptables-save -t nat > "$D/nat.txt" 2>/dev/null || true; fi
  if command -v nft >/dev/null; then $SUDO nft -j list ruleset > "$D/nat.json" 2>/dev/null || true; fi
  python3 code/main.py --state "$WORK/none.json" explain cc-smoke-web --from-dir "$D" --ip "$DIP" --gateway "$GW" --bridge "${DBR:-docker0}" --publish 18081:80 | tee "$WORK/docker-explain.txt"
  grep -q "all 8 checks passed" "$WORK/docker-explain.txt" || fail "explain on the docker container"
  grep -q "$DIP" "$D/addr.json" || fail "nsenter ip addr does not show the address docker inspect reports ($DIP)"
  if command -v bridge >/dev/null; then echo "  bridge link:"; bridge link show | sed 's/^/    /' | head -5; fi
  docker rm -f cc-smoke-web >/dev/null
else
  echo "  (docker not available; Use It skipped)"
fi

echo "SMOKE OK: containers/container-networking"
