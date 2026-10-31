#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."

fail() { echo "SMOKE FAIL: $*" >&2; exit 1; }
command -v docker >/dev/null || fail "needs docker"
WORK="$(mktemp -d)"
cleanup() { rm -rf "$WORK"; docker rmi -f cc-smoke-hand:1 cc-smoke-oci:1 >/dev/null 2>&1 || true; }
trap cleanup EXIT

echo "▸ a root filesystem to build on: alpine exported, no Dockerfile involved"
mkdir -p "$WORK/base" "$WORK/app/app"
CID="$(docker create alpine:3.22)"
docker export "$CID" | tar -x -C "$WORK/base"
docker rm "$CID" >/dev/null
echo "hello from a hand-built image" > "$WORK/app/app/hello.txt"
printf '#!/bin/sh\ncat /app/hello.txt\n' > "$WORK/app/app/run.sh"
chmod +x "$WORK/app/app/run.sh"
test -f "$WORK/base/etc/motd" || fail "alpine rootfs should contain /etc/motd for the whiteout check"

build() {
  python3 code/main.py build --layer "$WORK/base" --layer "$WORK/app" --tag cc-smoke-hand:1 \
    --entrypoint /app/run.sh --env GREETING=hi --label org.opencontainers.image.title=cc-smoke \
    --delete etc/motd "$@"
}

echo "▸ build: two layers, config, manifest, index, packaged for docker load and as an OCI layout"
build --out "$WORK/img.tar" --oci-dir "$WORK/oci" --json > "$WORK/build.json"
python3 code/main.py verify "$WORK/img.tar"
IMAGE_ID="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["image_id"])' "$WORK/build.json")"
DIFF_IDS="$(python3 -c 'import json,sys; print(json.dumps([l["diff_id"] for l in json.load(open(sys.argv[1]))["layers"]]))' "$WORK/build.json")"

echo "▸ docker load accepts it and the container runs"
docker load -i "$WORK/img.tar" | tee "$WORK/load.txt"
grep -q "cc-smoke-hand:1" "$WORK/load.txt" || fail "docker load did not report the tag"
OUT="$(docker run --rm cc-smoke-hand:1)"
[[ "$OUT" == "hello from a hand-built image" ]] || fail "unexpected container output: $OUT"
docker run --rm --entrypoint sh cc-smoke-hand:1 -c 'test ! -e /etc/motd' || fail "the --delete whiteout should hide /etc/motd"
[[ "$(docker run --rm --entrypoint sh cc-smoke-hand:1 -c 'echo $GREETING')" == "hi" ]] || fail "Env from the config"

echo "▸ the daemon agrees on every digest"
GOT_ID="$(docker image inspect cc-smoke-hand:1 --format '{{.Id}}')"
[[ "$GOT_ID" == "$IMAGE_ID" ]] || fail "image ID should be the config digest: ours $IMAGE_ID, docker $GOT_ID"
GOT_LAYERS="$(docker image inspect cc-smoke-hand:1 --format '{{json .RootFS.Layers}}')"
[[ "$GOT_LAYERS" == "$DIFF_IDS" ]] || fail "RootFS.Layers should equal our diff_ids: ours $DIFF_IDS, docker $GOT_LAYERS"
docker history --format '{{.CreatedBy}}' cc-smoke-hand:1 | grep -q "mkimage --layer" || fail "history should carry our created_by"
[[ "$(docker image inspect cc-smoke-hand:1 --format '{{index .Config.Labels "org.opencontainers.image.title"}}')" == "cc-smoke" ]] || fail "label from the config"
echo "  image id $IMAGE_ID"

echo "▸ reproducible: a second build is byte-identical, an epoch change moves every digest"
build --out "$WORK/img2.tar" >/dev/null
cmp "$WORK/img.tar" "$WORK/img2.tar" || fail "two builds from the same input must be identical"
build --out "$WORK/img3.tar" --epoch 1700000000 --json > "$WORK/build3.json"
ID3="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["image_id"])' "$WORK/build3.json")"
[[ "$ID3" != "$IMAGE_ID" ]] || fail "a different epoch must change the image ID"

if command -v skopeo >/dev/null; then
  echo "▸ the OCI layout is what registries speak: skopeo copies it into the daemon"
  skopeo copy "oci:$WORK/oci:1" docker-daemon:cc-smoke-oci:1 >/dev/null
  OUT2="$(docker run --rm cc-smoke-oci:1)"
  [[ "$OUT2" == "hello from a hand-built image" ]] || fail "skopeo-copied image did not run: $OUT2"
  MANIFEST_DIGEST="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["manifest_digest"])' "$WORK/build.json")"
  SKOPEO_DIGEST="$(skopeo manifest-digest "$WORK/oci/blobs/sha256/${MANIFEST_DIGEST#sha256:}")"
  [[ "$SKOPEO_DIGEST" == "$MANIFEST_DIGEST" ]] || fail "skopeo disagrees on the manifest digest"
else
  echo "  (skopeo not installed; the OCI layout was checked by verify)"
fi

echo "SMOKE OK: containers/image-build"
