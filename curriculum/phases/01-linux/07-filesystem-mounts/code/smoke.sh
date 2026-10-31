#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."

fail() { echo "SMOKE FAIL: $*" >&2; exit 1; }
[[ "$(uname -s)" == "Linux" ]] || fail "needs Linux (/proc/self/mountinfo, mount namespaces)"
SUDO=""
[[ "$(id -u)" == "0" ]] || SUDO="sudo"
HERE="$PWD"
WORK="$(mktemp -d)"
CONTAINER="cc-smoke-mounts-$$"
cleanup() {
  docker rm -f "$CONTAINER" >/dev/null 2>&1 || true
  if mountpoint -q "$WORK/mnt" 2>/dev/null; then $SUDO umount -l "$WORK/mnt" 2>/dev/null || true; fi
  $SUDO rm -rf "$WORK"
}
trap cleanup EXIT

echo "▸ tree, which and usage on this host's own mount table"
python3 code/main.py tree > "$WORK/tree.txt"
head -12 "$WORK/tree.txt"
python3 code/main.py which /proc/self/status --json > "$WORK/which-proc.json"
python3 - "$WORK/which-proc.json" <<'PY' || fail "which /proc/self/status"
import json, sys
d = json.load(open(sys.argv[1]))
assert (d["mount_point"], d["fstype"], d["path_in_mount"]) == ("/proc", "proc", "self/status"), d
assert d["noexec"] is True, d["options"]
print(f"  /proc/self/status resolves through {d['mount_point']} ({d['fstype']}, {','.join(d['options'])}, propagation {d['propagation']})")
PY
python3 code/main.py usage --json > "$WORK/usage.json"
ROOT_INODES=$(python3 -c 'import json,sys; d=json.load(open(sys.argv[1])); print([m for m in d["mounts"] if m["mount_point"] == "/"][0]["inodes"])' "$WORK/usage.json")
DF_INODES=$(df --output=itotal / | tail -1 | tr -d ' ')
[[ "$ROOT_INODES" == "$DF_INODES" ]] || fail "inode total for /: mountlens $ROOT_INODES vs df $DF_INODES"
echo "  / has $ROOT_INODES inodes according to both statvfs and df -i"

echo "▸ unshare -m: a read-only bind, a stacked mount and an inode-starved tmpfs, seen by which, diff, shadowed and usage"
mkdir -p "$WORK/src" "$WORK/mnt"
echo hello > "$WORK/src/file.txt"
cat /proc/self/mountinfo > "$WORK/before.mountinfo"
cat > "$WORK/inside.sh" <<'EOF'
set -euo pipefail
WORK="$1"
LESSON="$2"
mount --bind "$WORK/src" "$WORK/mnt"
mount -o remount,bind,ro "$WORK/mnt"
cat /proc/self/mountinfo > "$WORK/after.mountinfo"
if echo nope > "$WORK/mnt/file.txt" 2>/dev/null; then echo WRITABLE; else echo READONLY; fi > "$WORK/write.txt"
python3 "$LESSON/code/main.py" which "$WORK/mnt/file.txt" --json > "$WORK/which-mnt.json"
umount "$WORK/mnt"
mount -t tmpfs -o size=1m tmpfs "$WORK/mnt"
mount --bind "$WORK/src" "$WORK/mnt"
python3 "$LESSON/code/main.py" shadowed > "$WORK/shadowed.txt"
umount "$WORK/mnt"
umount "$WORK/mnt"
mount -t tmpfs -o size=1m,nr_inodes=64 tmpfs "$WORK/mnt"
i=0
while [[ $i -lt 500 ]] && touch "$WORK/mnt/f$i" 2>/dev/null; do i=$((i + 1)); done
echo "$i" > "$WORK/files.txt"
python3 "$LESSON/code/main.py" usage --all --json > "$WORK/usage-inside.json"
umount "$WORK/mnt"
EOF
$SUDO unshare -m --propagation private bash "$WORK/inside.sh" "$WORK" "$HERE"
grep -qx READONLY "$WORK/write.txt" || fail "mount -o remount,bind,ro should refuse writes"
python3 code/main.py diff "$WORK/before.mountinfo" "$WORK/after.mountinfo" | tee "$WORK/diff.txt"
grep -q "^+ $WORK/mnt " "$WORK/diff.txt" || fail "diff should list the new bind mount"
python3 - "$WORK/which-mnt.json" "$WORK/mnt" <<'PY' || fail "which inside the namespace"
import json, sys
d = json.load(open(sys.argv[1]))
assert d["mount_point"] == sys.argv[2], d["mount_point"]
assert d["read_only"] is True and "ro" in d["options"], d["options"]
assert d["bind"] is True and d["root"].endswith("/src"), d["root"]
assert d["propagation"] == "private", d["propagation"]
print(f"  {d['path']}: {d['fstype']} bind of {d['root']}, options {','.join(d['options'])}, propagation {d['propagation']}; write refused with EROFS")
PY
grep -q "$WORK/mnt" "$WORK/shadowed.txt" || fail "a bind over a tmpfs should be reported as shadowed: $(cat "$WORK/shadowed.txt")"
echo "  $(sed -n 2p "$WORK/shadowed.txt")"
FILES=$(cat "$WORK/files.txt")
[[ "$FILES" -lt 64 ]] || fail "nr_inodes=64 should stop touch before 64 files (got $FILES)"
python3 - "$WORK/usage-inside.json" "$WORK/mnt" "$FILES" <<'PY' || fail "inode exhaustion not reported"
import json, sys
d = json.load(open(sys.argv[1]))
m = [m for m in d["mounts"] if m["mount_point"] == sys.argv[2]][0]
assert m["inode_percent"] >= 95, m
assert m["use_percent"] < 50, m
assert any(sys.argv[2] in w and "inode exhaustion" in w for w in d["warnings"]), d["warnings"]
print(f"  {sys.argv[3]} files then ENOSPC: {m['inode_percent']}% of inodes used, {m['use_percent']}% of bytes used")
PY

if command -v docker >/dev/null; then
  echo "▸ Use It: -v ...:ro, --tmpfs and --read-only as rows inside a container"
  docker run -d --name "$CONTAINER" -v "$WORK/src:/data:ro" --tmpfs /tmp --read-only alpine:3.22 sleep 300 >/dev/null
  docker exec "$CONTAINER" cat /proc/self/mountinfo > "$WORK/container.mountinfo"
  python3 code/main.py tree --file "$WORK/container.mountinfo"
  LAYERS=$(docker image inspect alpine:3.22 -f '{{len .RootFS.Layers}}')
  python3 code/main.py overlay --file "$WORK/container.mountinfo" --json > "$WORK/overlay.json"
  python3 - "$WORK/overlay.json" "$LAYERS" <<'PY' || fail "overlay report"
import json, sys
found = json.load(open(sys.argv[1]))
want = int(sys.argv[2])
root = [o for o in found if o["mount_point"] == "/"]
assert root, found
n = len(root[0]["lower"])
assert want <= n <= want + 1, (n, want)
assert root[0]["upper"], root[0]
assert root[0]["read_only"] is True, root[0]
print(f"  rootfs: {n} lower layer(s) for a {want}-layer image (Docker adds one init layer), upperdir present, mounted ro")
PY
  python3 - "$WORK/container.mountinfo" <<'PY' || fail "which inside the container"
import json, subprocess, sys
def which(path):
    return json.loads(subprocess.check_output([sys.executable, "code/main.py", "which", path, "--file", sys.argv[1], "--json"]))
data, tmp, root = which("/data/file.txt"), which("/tmp/tool"), which("/usr/bin/env")
assert data["read_only"] and data["bind"] and data["root"].endswith("/src"), data
assert tmp["fstype"] == "tmpfs" and not tmp["read_only"] and tmp["noexec"], tmp
assert root["fstype"] == "overlay" and root["read_only"], root
print(f"  /data: {data['fstype']} bind of {data['root']} {','.join(data['options'])}; /tmp: tmpfs {','.join(tmp['options'])}; /: overlay {','.join(root['options'])}")
PY
  if docker exec "$CONTAINER" sh -c 'echo x > /data/x' 2>/dev/null; then fail "/data should be read-only"; fi
  if docker exec "$CONTAINER" sh -c 'echo x > /etc/newfile' 2>/dev/null; then fail "--read-only root should refuse writes"; fi
  docker exec "$CONTAINER" sh -c 'echo x > /tmp/x' || fail "--tmpfs /tmp should be writable"
  set +e
  docker exec "$CONTAINER" sh -c 'cp /bin/busybox /tmp/b && /tmp/b echo hi' >/dev/null 2>&1
  CODE=$?
  set -e
  [[ "$CODE" != "0" ]] || fail "noexec /tmp should refuse to execute /tmp/b"
  echo "  writes: /data EROFS, / EROFS, /tmp ok; executing a copied binary from the noexec --tmpfs failed as expected (exit $CODE)"
  docker inspect -f '{{json .Mounts}}' "$CONTAINER" > "$WORK/mounts.json"
  python3 - "$WORK/mounts.json" "$WORK/container.mountinfo" <<'PY' || fail "docker inspect .Mounts disagrees with mountinfo"
import json, sys
sys.path.insert(0, "code")
import main
mounts = json.load(open(sys.argv[1]))
table = {m.mount_point: m for m in main.visible(main.load(sys.argv[2]))}
assert mounts, "docker inspect listed no mounts"
for m in mounts:
    row = table[m["Destination"]]
    assert row.read_only == (not m["RW"]), (m, row.options)
    print(f"  docker inspect: {m['Type']} {m['Source']} -> {m['Destination']} RW={m['RW']} Propagation={m['Propagation']}; mountinfo row: {row.label} {','.join(row.options)}")
PY
  HOST_OVERLAYS=$(python3 code/main.py overlay --json | python3 -c 'import json,sys; print(len(json.load(sys.stdin)))')
  echo "  the host's table shows $HOST_OVERLAYS overlay mount(s) while the container runs"
  docker rm -f "$CONTAINER" >/dev/null
else
  echo "  (docker not available; Use It skipped)"
fi

if command -v findmnt >/dev/null; then
  echo "▸ findmnt agrees with which, and main.sh runs"
  FM=$(findmnt -n -o FSTYPE -T /proc/self/status)
  ML=$(python3 code/main.py which /proc/self/status --json | python3 -c 'import json,sys; print(json.load(sys.stdin)["fstype"])')
  [[ "$FM" == "$ML" ]] || fail "findmnt says $FM, mountlens says $ML"
  bash code/main.sh which /proc/self/status
  bash code/main.sh overlay
  bash code/main.sh shadowed
  bash code/main.sh usage > "$WORK/df.txt"
  head -6 "$WORK/df.txt"
else
  echo "  (findmnt not installed; comparison skipped)"
  bash code/main.sh tree > "$WORK/tree-sh.txt"
  head -5 "$WORK/tree-sh.txt"
fi

echo "SMOKE OK: linux/filesystem-mounts"
