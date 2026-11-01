#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."

fail() { echo "SMOKE FAIL: $*" >&2; exit 1; }
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
IMAGE="alpine:3.22"

echo "▸ manifest for $IMAGE over plain HTTP (token flow + platform selection)"
python3 code/main.py manifest "$IMAGE" --platform linux/amd64 --json > "$WORK/manifest.json" || fail "manifest fetch"
python3 - "$WORK/manifest.json" <<'PY' || fail "manifest shape"
import json, sys
d = json.load(open(sys.argv[1]))
assert d["digest"].startswith("sha256:"), d["digest"]
assert len(d["manifest"]["layers"]) >= 1, d["manifest"]
print("  digest", d["digest"][:19], "layers", len(d["manifest"]["layers"]))
PY

echo "▸ pull + rootfs with digest verification"
python3 code/main.py rootfs "$IMAGE" --platform linux/amd64 --dest "$WORK/img" --json > "$WORK/pull.json" || fail "pull"
test -f "$WORK/img/rootfs/etc/os-release" || fail "rootfs missing /etc/os-release"
grep -q 'ID=alpine' "$WORK/img/rootfs/etc/os-release" || fail "unexpected os-release"
test -x "$WORK/img/rootfs/bin/busybox" || fail "busybox missing from rootfs"
echo "  rootfs ok: $(grep PRETTY_NAME "$WORK/img/rootfs/etc/os-release")"

if command -v docker >/dev/null; then
  echo "▸ Use It: the digest docker resolves equals the one we fetched"
  docker pull -q --platform linux/amd64 "$IMAGE" >/dev/null
  DOCKER_DIGEST="$(docker manifest inspect "$IMAGE" -v 2>/dev/null | python3 -c '
import json, sys
data = json.load(sys.stdin)
entries = data if isinstance(data, list) else [data]
for e in entries:
    p = e.get("Descriptor", {}).get("platform", {})
    if p.get("os") == "linux" and p.get("architecture") == "amd64":
        print(e["Descriptor"]["digest"]); break
')"
  OUR_DIGEST="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["digest"])' "$WORK/manifest.json")"
  echo "  docker: $DOCKER_DIGEST"
  echo "  ours:   $OUR_DIGEST"
  [[ "$DOCKER_DIGEST" == "$OUR_DIGEST" ]] || fail "digest disagreement with docker manifest inspect"
  echo "▸ the rootfs we unpacked runs as a container (import + run)"
  tar -C "$WORK/img/rootfs" -c . | docker import - cc-smoke-rootfs:latest >/dev/null
  OUT="$(docker run --rm --platform linux/amd64 cc-smoke-rootfs:latest /bin/busybox echo "hello from a hand-pulled rootfs")"
  [[ "$OUT" == "hello from a hand-pulled rootfs" ]] || fail "imported rootfs did not run: $OUT"
  docker rmi cc-smoke-rootfs:latest >/dev/null
else
  echo "  (docker not available; comparison skipped)"
fi

echo "SMOKE OK: containers/registry-pull"
