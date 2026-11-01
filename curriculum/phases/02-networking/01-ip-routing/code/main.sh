#!/usr/bin/env bash
set -euo pipefail

P="${RT_PREFIX:-cc-rt}"
C="$P-c"
R="$P-r"
U1="$P-u1"
U2="$P-u2"
ALL=("$C" "$R" "$U1" "$U2")

usage() {
  cat >&2 <<USAGE
usage: main.sh up                  build the lab by hand: four network namespaces, three veth pairs,
                                   a dummy device, two tables, policy rules, ECMP and blackhole routes
       main.sh down                delete the namespaces and everything in them
       main.sh status              addresses, rules and every routing table, per namespace
       main.sh snapshot NS DIR     the by-hand routelens snapshot: ip -j dumps and sysctls of NS
Namespaces: $C (client) -- $R (router) -- $U1 and $U2 (upstreams). RT_PREFIX renames them.
USAGE
  exit 2
}

die() { echo "lab: $*" >&2; exit 1; }

need_linux_root() {
  [[ "$(uname -s)" == "Linux" ]] || die "needs Linux (network namespaces, veth); routelens and its tests run anywhere"
  [[ "$(id -u)" == "0" ]] || die "run as root: sudo bash $0 $*"
}

in_ns() { local ns="$1"; shift; ip netns exec "$ns" "$@"; }

cable() {
  local a_ns="$1" a_if="$2" b_ns="$3" b_if="$4"
  ip link add "$a_if" netns "$a_ns" type veth peer name "$b_if" netns "$b_ns"
  ip -n "$a_ns" link set "$a_if" up
  ip -n "$b_ns" link set "$b_if" up
}

cmd_up() {
  need_linux_root up
  local ns
  for ns in "${ALL[@]}"; do
    [[ ! -e "/run/netns/$ns" ]] || die "$ns already exists; run: main.sh down"
  done
  for ns in "${ALL[@]}"; do
    ip netns add "$ns"
    ip -n "$ns" link set lo up
    in_ns "$ns" sysctl -qw net.ipv4.conf.all.rp_filter=0 net.ipv4.conf.default.rp_filter=0
  done

  cable "$C" c0 "$R" r-c
  cable "$R" r-u1 "$U1" u1-0
  cable "$R" r-u2 "$U2" u2-0
  ip -n "$C" link add c-dum type dummy || die "cannot create a dummy device (modprobe dummy)"
  ip -n "$C" link set c-dum up

  ip -n "$C" addr add 10.0.1.10/24 dev c0
  ip -n "$C" addr add 10.0.1.20/24 dev c0
  ip -n "$C" addr add 172.16.0.1/24 dev c-dum
  ip -n "$R" addr add 10.0.1.1/24 dev r-c
  ip -n "$R" addr add 10.0.2.1/24 dev r-u1
  ip -n "$R" addr add 10.0.3.1/24 dev r-u2
  ip -n "$U1" addr add 10.0.2.10/24 dev u1-0
  ip -n "$U1" addr add 198.51.100.1/32 dev lo
  ip -n "$U2" addr add 10.0.3.10/24 dev u2-0
  ip -n "$U2" addr add 198.51.100.1/32 dev lo
  ip -n "$U2" addr add 203.0.113.1/32 dev lo

  ip -n "$C" route add default via 10.0.1.1 dev c0
  ip -n "$C" route add 10.0.0.0/16 via 10.0.1.1 dev c0 metric 50
  ip -n "$C" route add 10.0.0.0/16 dev c-dum metric 100
  ip -n "$C" route add 10.0.3.0/24 via 10.0.1.1 dev c0 src 10.0.1.20
  ip -n "$C" route add 10.0.5.0/24 via 10.0.1.250 dev c0
  ip -n "$C" route add blackhole 10.99.0.0/16
  ip -n "$C" route add unreachable 10.66.0.0/16
  ip -n "$C" route add default via 10.0.1.1 dev c0 table 100
  ip -n "$C" route add 10.0.3.0/24 dev c-dum table 100
  ip -n "$C" rule add from 10.0.1.20 lookup 100 priority 100
  ip -n "$C" rule add fwmark 0x1 lookup 100 priority 110

  in_ns "$R" sysctl -qw net.ipv4.ip_forward=1
  ip -n "$R" route add default via 10.0.2.10 dev r-u1
  ip -n "$R" route add 203.0.113.0/24 via 10.0.3.10 dev r-u2
  ip -n "$R" route add 192.0.2.0/24 nexthop via 10.0.2.10 dev r-u1 weight 1 nexthop via 10.0.3.10 dev r-u2 weight 1
  ip -n "$R" route add blackhole 10.99.0.0/16
  ip -n "$R" route add default via 10.0.3.10 dev r-u2 table 100
  ip -n "$R" rule add from 10.0.1.20 lookup 100 priority 100

  ip -n "$U1" route add 10.0.1.0/24 via 10.0.2.1 dev u1-0
  ip -n "$U2" route add default via 10.0.3.1 dev u2-0
  echo "lab up: $C 10.0.1.10+.20 -> $R 10.0.1.1 | 10.0.2.1 -> $U1 10.0.2.10 | 10.0.3.1 -> $U2 10.0.3.10"
}

cmd_down() {
  need_linux_root down
  local ns
  for ns in "${ALL[@]}"; do
    ip netns pids "$ns" 2>/dev/null | xargs -r kill 2>/dev/null || true
    ip netns del "$ns" 2>/dev/null || true
  done
  echo "lab down: ${ALL[*]} deleted"
}

cmd_status() {
  need_linux_root status
  local ns
  for ns in "${ALL[@]}"; do
    [[ -e "/run/netns/$ns" ]] || { echo "== $ns: not created"; continue; }
    echo "== $ns"
    ip -n "$ns" -br -4 addr
    ip -n "$ns" -4 rule
    ip -n "$ns" -4 route show table all | grep -vE '^(local|broadcast) ' || true
  done
}

cmd_snapshot() {
  [[ $# -eq 2 ]] || usage
  need_linux_root snapshot
  local ns="$1" dir="$2"
  [[ -e "/run/netns/$ns" ]] || die "no namespace named $ns"
  mkdir -p "$dir"
  ip -n "$ns" -j addr > "$dir/addr.json"
  ip -n "$ns" -4 -j route show table all > "$dir/route.json"
  ip -n "$ns" -6 -j route show table all > "$dir/route6.json" 2>/dev/null || echo "[]" > "$dir/route6.json"
  ip -n "$ns" -4 -j rule > "$dir/rule.json"
  ip -n "$ns" -6 -j rule > "$dir/rule6.json" 2>/dev/null || echo "[]" > "$dir/rule6.json"
  ip -n "$ns" -j neigh > "$dir/neigh.json"
  in_ns "$ns" sh -c '
    echo "net.ipv4.ip_forward = $(cat /proc/sys/net/ipv4/ip_forward)"
    for f in /proc/sys/net/ipv4/conf/*/rp_filter /proc/sys/net/ipv4/conf/*/forwarding; do
      d="${f%/*}"
      echo "net.ipv4.conf.${d##*/}.${f##*/} = $(cat "$f")"
    done' > "$dir/sysctl.txt"
  echo "$ns -> $dir: addr route route6 rule rule6 neigh sysctl"
}

[[ $# -ge 1 ]] || usage
COMMAND="$1"
shift
case "$COMMAND" in
  up) cmd_up ;;
  down) cmd_down ;;
  status) cmd_status ;;
  snapshot) cmd_snapshot "$@" ;;
  *) usage ;;
esac
