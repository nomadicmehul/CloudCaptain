#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."

fail() { echo "SMOKE FAIL: $*" >&2; exit 1; }
[[ "$(uname -s)" == "Linux" ]] || fail "needs Linux (user namespaces)"
WORK="$(mktemp -d)"
PIDS=()
CONTAINER="cc-smoke-userns-$$"
cleanup() {
  kill "${PIDS[@]}" 2>/dev/null || true
  if command -v podman >/dev/null; then podman rm -f "$CONTAINER" >/dev/null 2>&1 || true; fi
  if command -v docker >/dev/null; then docker rm -f "$CONTAINER" >/dev/null 2>&1 || true; fi
  if command -v podman >/dev/null && [[ -d "$WORK/vol" ]]; then podman unshare rm -rf "$WORK/vol" 2>/dev/null || true; fi
  rm -rf "$WORK"
}
trap cleanup EXIT
ME="$(id -un)"
MYUID="$(id -u)"

echo "▸ map: the plan for a fixture user (pure logic, same on macOS)"
cat > "$WORK/subuid" <<'EOF'
alice:100000:65536
bob:165536:65536
EOF
cp "$WORK/subuid" "$WORK/subgid"
python3 code/main.py map --user alice --uid 1000 --gid 1000 --subuid "$WORK/subuid" --subgid "$WORK/subgid" --pid 4242 | tee "$WORK/plan.txt"
grep -q "newuidmap 4242 0 1000 1 1 100000 65536" "$WORK/plan.txt" || fail "newuidmap arguments"
grep -q "container uids 1..65536 are host uids 100000..165535" "$WORK/plan.txt" || fail "range explanation"

echo "▸ map: $ME's own ranges in /etc/subuid (exit 1 with a fix line is a valid answer)"
set +e
python3 code/main.py map --pid 4242 > "$WORK/me.txt"
MAPCODE=$?
set -e
head -n 3 "$WORK/me.txt" | sed 's/^/  /'
grep -q "^verdict:" "$WORK/me.txt" || fail "map on the real /etc/subuid produced no verdict"
if [[ "$MAPCODE" == "0" ]]; then echo "  $ME has subordinate ranges"; else echo "  $ME has none: $(grep '^fix:' "$WORK/me.txt")"; fi

echo "▸ translate across the boundary"
[[ "$(python3 code/main.py translate --map 0 1000 1 --map 1 100000 65536 1001)" == "container uid 1001 -> host uid 101000" ]] || fail "translate to host"
[[ "$(python3 code/main.py translate --map 0 1000 1 --map 1 100000 65536 --from-host 100999)" == "host uid 100999 -> container uid 1000" ]] || fail "translate from host"

echo "▸ unprivileged user namespaces on this kernel"
if ! unshare -Ur true 2>/dev/null; then
  echo "  unshare -Ur failed; trying to enable unprivileged user namespaces with sudo"
  sudo sysctl -w kernel.unprivileged_userns_clone=1 2>/dev/null || true
  sudo sysctl -w kernel.apparmor_restrict_unprivileged_userns=0 2>/dev/null || true
  unshare -Ur true || fail "unprivileged user namespaces are not available (kernel.unprivileged_userns_clone, kernel.apparmor_restrict_unprivileged_userns)"
fi
[[ "$(unshare -Ur id -u)" == "0" ]] || fail "unshare -Ur id -u should print 0"
echo "  unshare -Ur id: $(unshare -Ur id)"

echo "▸ run: unshare(CLONE_NEWUSER) from python; uid 0 inside, files owned by $ME outside"
python3 code/main.py run -- sh -c 'id -u; cat /proc/self/uid_map; touch "$1"' sh "$WORK/made-inside" | tee "$WORK/run.txt"
[[ "$(head -n 1 "$WORK/run.txt")" == "0" ]] || fail "uid inside should be 0"
grep -qE "^ +0 +$MYUID +1$" "$WORK/run.txt" || fail "uid_map inside should read '0 $MYUID 1'"
OWNER="$(stat -c %u "$WORK/made-inside")"
[[ "$OWNER" == "$MYUID" ]] || fail "a file created by uid 0 inside should be owned by $MYUID outside, got $OWNER"
echo "  made-inside is owned by uid $OWNER ($ME) on the host"
set +e
python3 code/main.py run -- sh -c 'exit 7'
CODE=$?
set -e
[[ "$CODE" == "7" ]] || fail "exit code should propagate (got $CODE)"

echo "▸ run --ns mnt,pid: the new root owns mount and pid namespaces of its own"
if unshare -Urpf --mount-proc true 2>/dev/null; then
  INNER="$(python3 code/main.py run --ns mnt,pid -- sh -c 'echo $$; ls /proc | grep -c "^[0-9]"')"
  sed 's/^/  /' <<<"$INNER"
  [[ "$(head -n 1 <<<"$INNER")" == "1" ]] || fail "the command should be PID 1 in its own pid namespace"
  [[ "$(tail -n 1 <<<"$INNER")" -le 3 ]] || fail "a fresh /proc should list only the inner processes"
else
  echo "  (this kernel refuses a fresh /proc mount in a user namespace; skipped)"
fi

echo "▸ explain: a live process in a user namespace, then this shell"
unshare -Ur sleep 60 &
NSPID=$!
PIDS+=("$NSPID")
sleep 0.3
python3 code/main.py explain "$NSPID" | tee "$WORK/explain.txt"
grep -q "container uid 0 is host uid $MYUID" "$WORK/explain.txt" || fail "explain of the unshare -Ur process"
grep -q "verdict: rootless single-user map" "$WORK/explain.txt" || fail "verdict for a single-user map"
python3 code/main.py explain $$ --json > "$WORK/self.json"
python3 -c 'import json, sys; d = json.load(open(sys.argv[1])); print("  this shell:", d["verdict"])' "$WORK/self.json"

echo "▸ main.sh (grep, cat, unshare -Ur)"
bash code/main.sh | tee "$WORK/sh.txt"
grep -q "uid=0(root)" "$WORK/sh.txt" || fail "main.sh should show id inside unshare -Ur as root"

if command -v docker >/dev/null && docker info >/dev/null 2>&1; then
  echo "▸ Use It: docker, is uid 0 inside the host's uid 0?"
  docker run --rm --name "$CONTAINER" alpine:3.22 cat /proc/self/uid_map > "$WORK/docker-uid_map"
  python3 code/main.py explain --uid-map "$WORK/docker-uid_map" | tee "$WORK/docker-explain.txt"
  if docker info -f '{{.SecurityOptions}}' | grep -qE 'rootless|userns'; then
    grep -q "verdict: rootless" "$WORK/docker-explain.txt" || fail "rootless or userns-remap docker should show a rootless map"
  else
    grep -q "verdict: not rootless" "$WORK/docker-explain.txt" || fail "rootful docker should show the identity map"
  fi
else
  echo "  (docker not available; rootful comparison skipped)"
fi

if command -v podman >/dev/null && podman info >/dev/null 2>&1; then
  echo "▸ Use It: rootless podman"
  podman info --format '{{json .Host.IDMappings}}' | tee "$WORK/idmappings.json"
  echo
  podman unshare cat /proc/self/uid_map > "$WORK/podman-uid_map"
  podman unshare cat /proc/self/gid_map > "$WORK/podman-gid_map"
  python3 code/main.py explain --uid-map "$WORK/podman-uid_map" --gid-map "$WORK/podman-gid_map" | tee "$WORK/podman-explain.txt"
  grep -q "container uid 0 is host uid $MYUID" "$WORK/podman-explain.txt" || fail "podman unshare map should put $ME at uid 0"
  ID_OUT="$(podman run --rm --name "$CONTAINER" alpine:3.22 id -u)"
  [[ "$ID_OUT" == "0" ]] || fail "podman run id -u should print 0 inside (got $ID_OUT)"
  KEEP="$(podman run --rm --name "$CONTAINER" --userns=keep-id alpine:3.22 id -u)"
  [[ "$KEEP" == "$MYUID" ]] || fail "--userns=keep-id should show $MYUID inside (got $KEEP)"
  mkdir -p "$WORK/vol"
  podman run --rm --name "$CONTAINER" -v "$WORK/vol:/data" alpine:3.22 sh -c 'touch /data/by-999 && chown 999:999 /data/by-999'
  HOST_OWNER="$(stat -c %u "$WORK/vol/by-999")"
  EXPECTED="$(python3 code/main.py translate --uid-map "$WORK/podman-uid_map" 999 | awk '{print $NF}')"
  [[ "$HOST_OWNER" == "$EXPECTED" ]] || fail "a file owned by container uid 999 should be host uid $EXPECTED, got $HOST_OWNER"
  echo "  container uid 999 owns /data/by-999; on the host that is uid $HOST_OWNER"
else
  echo "  (podman not installed or not usable rootless here; podman checks skipped)"
fi

echo "SMOKE OK: containers/rootless-podman"
