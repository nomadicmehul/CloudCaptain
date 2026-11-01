#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."

fail() { echo "SMOKE FAIL: $*" >&2; exit 1; }
command -v docker >/dev/null || fail "needs docker"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

echo "▸ audit a real image saved by docker"
docker pull -q alpine:3.22 >/dev/null
docker save alpine:3.22 -o "$WORK/alpine.tar"
OUT="$(python3 code/main.py "$WORK/alpine.tar")"
echo "$OUT"
grep -q "^image: alpine:3.22" <<<"$OUT" || fail "tags from manifest.json"
grep -q "^layers: 1 " <<<"$OUT" || fail "alpine has one layer"
python3 code/main.py "$WORK/alpine.tar" --json | python3 -c 'import json,sys; d=json.load(sys.stdin); assert d["layers"][0]["size"] > 1000000, d' || fail "--json layer size"

echo "▸ the leaked-secret demo from Use It"
mkdir -p "$WORK/leak" && cd "$WORK/leak"
echo "hunter2" > secret.key
cat > Dockerfile <<'EOF'
FROM alpine:3.22
COPY secret.key /secret.key
RUN rm /secret.key
EOF
docker build -q -t cc-smoke-leak:1 . >/dev/null
docker run --rm cc-smoke-leak:1 sh -c 'test ! -e /secret.key' || fail "whiteout should hide the file at runtime"
docker save cc-smoke-leak:1 -o leak.tar
cd - >/dev/null
OUT2="$(python3 code/main.py "$WORK/leak/leak.tar")"
echo "$OUT2"
grep -q "deleted in layer 2: secret.key (8 B)" <<<"$OUT2" || fail "audit should report the deleted secret still shipped"
grep -q "^wasted: 8 B" <<<"$OUT2" || fail "wasted bytes"

echo "▸ docker history agrees on instruction count"
HIST="$(docker history --no-trunc --format '{{.CreatedBy}}' cc-smoke-leak:1 | wc -l)"
[[ "$HIST" -ge 3 ]] || fail "docker history rows"
docker rmi -f cc-smoke-leak:1 >/dev/null

echo "SMOKE OK: containers/image-layers"
