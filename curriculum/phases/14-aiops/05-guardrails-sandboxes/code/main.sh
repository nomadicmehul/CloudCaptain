#!/usr/bin/env bash
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
PROXY_IMAGE="${PROXY_IMAGE:-python:3.12-alpine}"
PROXY_PORT="${PROXY_PORT:-3128}"
KUBECONFIG_TARGET=/home/agent/.kube/config

usage() {
  cat >&2 <<'EOF'
usage: main.sh up    --name NAME --allow HOST[:PORT] [--allow ...] [--user UID]
       main.sh run   --name NAME [--image IMG] [--kubeconfig PATH] [--memory 256m] [--pids 64] [--cpus 1] [--user 1000] [--detach] -- CMD [ARGS...]
       main.sh audit --name NAME
       main.sh down  --name NAME

up    creates NAME-net (internal, no route out) and NAME-egress, starts NAME-proxy on both with the allow-list
run   starts NAME-agent on NAME-net with HTTPS_PROXY=http://NAME-proxy:PORT, read-only rootfs, no capabilities,
      no-new-privileges, pids/memory/cpu limits, uid 1000 and the kubeconfig mounted read-only
audit prints the proxy's JSON audit (hosts, allowed/denied, bytes)
down  removes the agent, the proxy and both networks
EOF
  exit 2
}

command -v docker >/dev/null || { echo "main.sh: docker is required" >&2; exit 2; }

ACTION="${1:-}"
[[ -n "$ACTION" ]] || usage
shift
NAME=""; IMAGE="python:3.12-alpine"; KUBECONFIG_SRC=""; MEMORY="256m"; PIDS=64; CPUS=1; USER_ID=1000; DETACH=""
ALLOW=()
while [[ $# -gt 0 ]]; do
  case "$1" in
    --name) NAME="$2"; shift 2 ;;
    --allow) ALLOW+=("$2"); shift 2 ;;
    --image) IMAGE="$2"; shift 2 ;;
    --kubeconfig) KUBECONFIG_SRC="$2"; shift 2 ;;
    --memory) MEMORY="$2"; shift 2 ;;
    --pids) PIDS="$2"; shift 2 ;;
    --cpus) CPUS="$2"; shift 2 ;;
    --user) USER_ID="$2"; shift 2 ;;
    --detach) DETACH=1; shift ;;
    --) shift; break ;;
    *) usage ;;
  esac
done
[[ "$NAME" =~ ^[a-z0-9][a-z0-9-]*$ ]] || usage
NET="$NAME-net"; EGRESS="$NAME-egress"; PROXY="$NAME-proxy"; AGENT="$NAME-agent"
PROXY_URL="http://$PROXY:$PROXY_PORT"

up() {
  local allow_args=()
  for host in ${ALLOW[@]+"${ALLOW[@]}"}; do allow_args+=(--allow "$host"); done
  docker network create --internal "$NET" >/dev/null
  docker network create "$EGRESS" >/dev/null
  docker run -d --rm --name "$PROXY" --network "$EGRESS" \
    --read-only --tmpfs /tmp --cap-drop ALL --security-opt no-new-privileges --pids-limit 64 --memory 128m --user "$USER_ID" \
    -v "$HERE/main.py:/main.py:ro" "$PROXY_IMAGE" \
    python3 /main.py proxy --listen "0.0.0.0:$PROXY_PORT" --audit /tmp/audit.json ${allow_args[@]+"${allow_args[@]}"} >/dev/null
  docker network connect "$NET" "$PROXY"
  echo "sandbox $NAME is up: agents on $NET (internal) reach the world only through $PROXY_URL, which allows: ${ALLOW[*]:-nothing}"
}

run() {
  [[ $# -gt 0 ]] || usage
  local args=(docker run --rm)
  if [[ -n "$DETACH" ]]; then args+=(-d); fi
  args+=(--name "$AGENT" --network "$NET" --read-only --tmpfs /tmp
    --cap-drop ALL --security-opt no-new-privileges --pids-limit "$PIDS" --memory "$MEMORY" --cpus "$CPUS" --user "$USER_ID"
    -e "HTTPS_PROXY=$PROXY_URL" -e "HTTP_PROXY=$PROXY_URL" -e "https_proxy=$PROXY_URL" -e "http_proxy=$PROXY_URL"
    -e "NO_PROXY=localhost,127.0.0.1" -e "no_proxy=localhost,127.0.0.1")
  if [[ -n "$KUBECONFIG_SRC" ]]; then
    args+=(-e "KUBECONFIG=$KUBECONFIG_TARGET" -v "$KUBECONFIG_SRC:$KUBECONFIG_TARGET:ro")
  fi
  args+=("$IMAGE" "$@")
  printf '+' >&2; printf ' %q' "${args[@]}" >&2; echo >&2
  "${args[@]}"
}

audit() {
  docker exec "$PROXY" cat /tmp/audit.json
}

down() {
  docker rm -f "$AGENT" "$PROXY" >/dev/null 2>&1 || true
  docker network rm "$NET" "$EGRESS" >/dev/null 2>&1 || true
  echo "sandbox $NAME removed"
}

case "$ACTION" in
  up) up ;;
  run) run "$@" ;;
  audit) audit ;;
  down) down ;;
  *) usage ;;
esac
