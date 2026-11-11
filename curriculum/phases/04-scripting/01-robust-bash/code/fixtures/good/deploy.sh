#!/usr/bin/env bash
set -euo pipefail
release="${1:?usage: deploy.sh RELEASE.tar.gz TARGET_DIR}"
target="${2:?usage: deploy.sh RELEASE.tar.gz TARGET_DIR}"
cd "$target"
tar -xzf "$release"
for f in ./*; do
  chmod 0644 "$f"
done
echo "deployed $release at $(date -u +%FT%TZ)" >&2
