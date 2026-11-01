#!/usr/bin/env bash
set -euo pipefail

IMAGE="${IMAGE:-alpine:3.22}"
HERE="$(cd "$(dirname "$0")" && pwd)"

stop_time() {
  local name="$1"; shift
  docker rm -f "$name" >/dev/null 2>&1 || true
  docker run --rm -d --name "$name" "$@" >/dev/null
  sleep 1
  local start end
  start=$(date +%s.%N)
  docker stop -t 10 "$name" >/dev/null
  end=$(date +%s.%N)
  printf '%-10s %5.1fs\n' "$name" "$(echo "$end - $start" | bc)"
}

echo "docker stop duration for the same workload with three different PID 1s:"
stop_time plain            "$IMAGE" sh -c 'sleep 600'
stop_time withinit --init  "$IMAGE" sh -c 'sleep 600'
stop_time mini -v "$HERE/main.py:/mini_init.py:ro" python:3.12-alpine python3 /mini_init.py --restart never --run 'sleep 600'
echo
echo "plain:    sleep is PID 1 and ignores SIGTERM; Docker waits the grace period, then SIGKILLs (exit 137)."
echo "withinit: tini is PID 1 and forwards SIGTERM to sleep; exit is immediate (143)."
echo "mini:     your supervisor is PID 1 and does the same."
