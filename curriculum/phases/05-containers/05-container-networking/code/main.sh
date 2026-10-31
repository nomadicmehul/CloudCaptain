#!/usr/bin/env bash
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
STATE_DIR="${CCNET_STATE:-/var/lib/ccnet}"
STATE="$STATE_DIR/state.json"
BRIDGE="cc0"
CIDR="10.90.0.0/24"
GATEWAY="10.90.0.1"
PREFIX="24"
DNS="${CCNET_DNS:-1.1.1.1}"
TABLE="ccnet"
CHAIN="CCNET"
BACKEND=""

usage() {
  cat >&2 <<EOF
usage: main.sh up NAME [--publish HOSTPORT:CONTAINERPORT[/udp]]... [--dry-run]
       main.sh exec NAME [--] COMMAND...
       main.sh down NAME
       main.sh ls
       main.sh purge
Builds a container network by hand: bridge $BRIDGE ($CIDR), one netns + veth pair per
container, MASQUERADE for egress, DNAT for published ports (nft, or iptables if nft is missing).
EOF
  exit 2
}

die() { echo "ccnet: $*" >&2; exit 1; }
ipam() { python3 "$HERE/main.py" --state "$STATE" "$@"; }

need_linux_root() {
  [[ "$(uname -s)" == "Linux" ]] || die "needs Linux (network namespaces, veth, netfilter); the tests and 'plan' run anywhere"
  [[ "$(id -u)" == "0" ]] || die "run as root: sudo bash $0 $*"
}

detect_backend() {
  if command -v nft >/dev/null 2>&1; then echo nft
  elif command -v iptables >/dev/null 2>&1; then echo iptables
  else die "neither nft nor iptables found; install the nftables package"
  fi
}

ipt() { iptables -w -t nat "$@"; }

ensure_bridge() {
  if ! ip link show "$BRIDGE" >/dev/null 2>&1; then
    ip link add "$BRIDGE" type bridge
    ip addr add "$GATEWAY/$PREFIX" dev "$BRIDGE"
    ip link set "$BRIDGE" up
  fi
  sysctl -qw net.ipv4.ip_forward=1
  sysctl -qw "net.ipv4.conf.$BRIDGE.route_localnet=1"
}

ensure_nat_nft() {
  nft list table ip "$TABLE" >/dev/null 2>&1 && return 0
  nft add table ip "$TABLE"
  nft "add chain ip $TABLE prerouting { type nat hook prerouting priority -100; policy accept; }"
  nft "add chain ip $TABLE output { type nat hook output priority -100; policy accept; }"
  nft "add chain ip $TABLE postrouting { type nat hook postrouting priority 100; policy accept; }"
  nft add chain ip "$TABLE" publish
  nft add rule ip "$TABLE" prerouting fib daddr type local jump publish
  nft add rule ip "$TABLE" output fib daddr type local jump publish
  nft add rule ip "$TABLE" postrouting ip saddr "$CIDR" oifname != "$BRIDGE" masquerade
  nft add rule ip "$TABLE" postrouting oifname "$BRIDGE" fib saddr type local masquerade
}

ensure_nat_iptables() {
  ipt -nL "$CHAIN" >/dev/null 2>&1 || ipt -N "$CHAIN"
  ipt -C PREROUTING -m addrtype --dst-type LOCAL -j "$CHAIN" 2>/dev/null || ipt -A PREROUTING -m addrtype --dst-type LOCAL -j "$CHAIN"
  ipt -C OUTPUT -m addrtype --dst-type LOCAL -j "$CHAIN" 2>/dev/null || ipt -A OUTPUT -m addrtype --dst-type LOCAL -j "$CHAIN"
  ipt -C POSTROUTING -s "$CIDR" ! -o "$BRIDGE" -j MASQUERADE 2>/dev/null || ipt -A POSTROUTING -s "$CIDR" ! -o "$BRIDGE" -j MASQUERADE
  ipt -C POSTROUTING -o "$BRIDGE" -m addrtype --src-type LOCAL -j MASQUERADE 2>/dev/null || ipt -A POSTROUTING -o "$BRIDGE" -m addrtype --src-type LOCAL -j MASQUERADE
}

open_forward_on_docker_hosts() {
  command -v iptables >/dev/null 2>&1 || return 0
  iptables -w -nL DOCKER-USER >/dev/null 2>&1 || return 0
  iptables -w -C DOCKER-USER -i "$BRIDGE" -j ACCEPT 2>/dev/null || iptables -w -I DOCKER-USER -i "$BRIDGE" -j ACCEPT
  iptables -w -C DOCKER-USER -o "$BRIDGE" -j ACCEPT 2>/dev/null || iptables -w -I DOCKER-USER -o "$BRIDGE" -j ACCEPT
}

close_forward_on_docker_hosts() {
  command -v iptables >/dev/null 2>&1 || return 0
  iptables -w -nL DOCKER-USER >/dev/null 2>&1 || return 0
  iptables -w -D DOCKER-USER -i "$BRIDGE" -j ACCEPT 2>/dev/null || true
  iptables -w -D DOCKER-USER -o "$BRIDGE" -j ACCEPT 2>/dev/null || true
}

ensure_network() {
  ensure_bridge
  case "$BACKEND" in
    nft) ensure_nat_nft ;;
    iptables) ensure_nat_iptables ;;
  esac
  open_forward_on_docker_hosts
}

publish_port() {
  local name="$1" ip="$2" spec="$3" proto="tcp"
  if [[ "$spec" == */* ]]; then proto="${spec##*/}"; spec="${spec%/*}"; fi
  local hport="${spec%%:*}" cport="${spec##*:}"
  case "$BACKEND" in
    nft) nft add rule ip "$TABLE" publish "$proto" dport "$hport" dnat to "$ip:$cport" comment "\"ccnet:$name\"" ;;
    iptables) ipt -A "$CHAIN" -p "$proto" --dport "$hport" -m comment --comment "ccnet:$name" -j DNAT --to-destination "$ip:$cport" ;;
  esac
}

unpublish_all() {
  local name="$1" handle rule
  case "$BACKEND" in
    nft)
      nft -a list chain ip "$TABLE" publish 2>/dev/null | awk -v tag="\"ccnet:$name\"" 'index($0, tag) {print $NF}' | while read -r handle; do
        nft delete rule ip "$TABLE" publish handle "$handle"
      done ;;
    iptables)
      ipt -S "$CHAIN" 2>/dev/null | grep -F -- "--comment ccnet:$name " | sed 's/^-A /-D /' | while read -r rule; do
        read -ra words <<<"$rule"
        ipt "${words[@]}"
      done ;;
  esac
}

cmd_up() {
  [[ $# -ge 1 ]] || usage
  local name="$1" dry=0; shift
  local -a publish=() pubargs=()
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --publish|-p) [[ $# -ge 2 ]] || usage; publish+=("$2"); pubargs+=(--publish "$2"); shift 2 ;;
      --dry-run) dry=1; shift ;;
      *) usage ;;
    esac
  done
  BACKEND="$(detect_backend)"
  if [[ "$dry" == 1 ]]; then
    ipam plan network --backend "$BACKEND"
    ipam plan up "$name" --backend "$BACKEND" --dns "$DNS" ${pubargs[@]+"${pubargs[@]}"}
    return 0
  fi
  need_linux_root up "$name"
  mkdir -p "$STATE_DIR"
  local ip
  ip="$(ipam allocate "$name" ${pubargs[@]+"${pubargs[@]}"})"
  ensure_network
  ip netns add "$name"
  ip link add "veth-$name" type veth peer name "ceth-$name"
  ip link set "ceth-$name" netns "$name"
  ip -n "$name" link set "ceth-$name" name eth0
  ip link set "veth-$name" master "$BRIDGE" up
  ip -n "$name" addr add "$ip/$PREFIX" dev eth0
  ip -n "$name" link set lo up
  ip -n "$name" link set eth0 up
  ip -n "$name" route add default via "$GATEWAY"
  mkdir -p "/etc/netns/$name"
  echo "nameserver $DNS" > "/etc/netns/$name/resolv.conf"
  local spec
  for spec in ${publish[@]+"${publish[@]}"}; do publish_port "$name" "$ip" "$spec"; done
  echo "$name: $ip/$PREFIX via $GATEWAY on $BRIDGE, veth-$name, dns $DNS, nat via $BACKEND${publish[*]:+, published ${publish[*]}}"
}

cmd_exec() {
  [[ $# -ge 2 ]] || usage
  local name="$1"; shift
  [[ "${1:-}" == "--" ]] && shift
  need_linux_root exec "$name"
  [[ -e "/run/netns/$name" ]] || die "no namespace named $name; run: main.sh up $name"
  exec ip netns exec "$name" "$@"
}

cmd_down() {
  [[ $# -eq 1 ]] || usage
  local name="$1"
  need_linux_root down "$name"
  BACKEND="$(detect_backend)"
  ip netns pids "$name" 2>/dev/null | xargs -r kill 2>/dev/null || true
  unpublish_all "$name"
  ip netns del "$name" 2>/dev/null || true
  ip link del "veth-$name" 2>/dev/null || true
  rm -rf "/etc/netns/$name"
  ipam release "$name"
}

cmd_ls() {
  ipam ls
  if [[ "$(uname -s)" == "Linux" ]] && ip link show "$BRIDGE" >/dev/null 2>&1; then
    echo "ports on $BRIDGE:"
    ip -br link show master "$BRIDGE" | sed 's/^/  /'
  fi
}

cmd_purge() {
  need_linux_root purge
  BACKEND="$(detect_backend)"
  local name
  for name in $(ipam ls --names 2>/dev/null || true); do cmd_down "$name"; done
  case "$BACKEND" in
    nft) nft delete table ip "$TABLE" 2>/dev/null || true ;;
    iptables)
      ipt -D PREROUTING -m addrtype --dst-type LOCAL -j "$CHAIN" 2>/dev/null || true
      ipt -D OUTPUT -m addrtype --dst-type LOCAL -j "$CHAIN" 2>/dev/null || true
      ipt -D POSTROUTING -s "$CIDR" ! -o "$BRIDGE" -j MASQUERADE 2>/dev/null || true
      ipt -D POSTROUTING -o "$BRIDGE" -m addrtype --src-type LOCAL -j MASQUERADE 2>/dev/null || true
      ipt -F "$CHAIN" 2>/dev/null || true
      ipt -X "$CHAIN" 2>/dev/null || true ;;
  esac
  close_forward_on_docker_hosts
  ip link del "$BRIDGE" 2>/dev/null || true
  rm -f "$STATE"
  echo "purged: $BRIDGE, the $BACKEND nat rules and $STATE"
}

[[ $# -ge 1 ]] || usage
COMMAND="$1"; shift
case "$COMMAND" in
  up) cmd_up "$@" ;;
  exec) cmd_exec "$@" ;;
  down) cmd_down "$@" ;;
  ls) cmd_ls ;;
  purge) cmd_purge ;;
  *) usage ;;
esac
