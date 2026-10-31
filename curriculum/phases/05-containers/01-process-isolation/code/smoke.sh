#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."

fail() { echo "SMOKE FAIL: $*" >&2; exit 1; }
[[ "$(uname -s)" == "Linux" ]] || fail "needs Linux (namespaces, cgroups)"
command -v docker >/dev/null || fail "needs docker"

echo "▸ explain mode runs anywhere"
python3 code/main.py --explain --memory 100M -- sleep 1 | grep -q "104857600 bytes" || fail "--explain output"

echo "▸ unshare + fork + sethostname + /proc via python (root)"
OUT="$(sudo python3 code/main.py --hostname smokebox -- /bin/sh -c 'hostname; ps -eo pid,comm | wc -l')"
echo "$OUT"
grep -q "^smokebox$" <<<"$OUT" || fail "hostname inside the uts namespace"
[[ "$(tail -n1 <<<"$OUT")" -le 4 ]] || fail "pid namespace should hide host processes (got $(tail -n1 <<<"$OUT") lines)"

echo "▸ util-linux variant (code/main.sh)"
OUT2="$(sudo bash code/main.sh smokesh -- /bin/sh -c 'hostname')"
[[ "$OUT2" == "smokesh" ]] || fail "main.sh hostname (got '$OUT2')"

echo "▸ cgroup v2 memory.max via python (expect OOM kill, exit 137)"
if [[ -f /sys/fs/cgroup/cgroup.controllers ]]; then
  set +e
  sudo python3 code/main.py --name smoke-mem --memory 50M -- /bin/sh -c 'head -c 200m /dev/zero | tail' >/dev/null 2>&1
  CODE=$?
  set -e
  [[ "$CODE" == "137" ]] || fail "expected exit 137 from the OOM killer, got $CODE"
  sudo rmdir /sys/fs/cgroup/smoke-mem 2>/dev/null || true
else
  echo "  (cgroup v2 not mounted; skipped)"
fi

echo "▸ Use It: docker maps onto the same primitives"
DOCKER_OUT="$(docker run --rm --hostname box --memory 100m --pids-limit 50 alpine:3.22 sh -c 'hostname; cat /sys/fs/cgroup/memory.max; cat /sys/fs/cgroup/pids.max')"
echo "$DOCKER_OUT"
grep -q "^box$" <<<"$DOCKER_OUT" || fail "docker --hostname"
grep -q "^104857600$" <<<"$DOCKER_OUT" || fail "docker --memory 100m → memory.max"
grep -q "^50$" <<<"$DOCKER_OUT" || fail "docker --pids-limit 50 → pids.max"
HOST_PS="$(docker run --rm --pid=host alpine:3.22 ps -eo pid | wc -l)"
[[ "$HOST_PS" -gt 10 ]] || fail "--pid=host should expose host processes"

echo "SMOKE OK: containers/process-isolation"
