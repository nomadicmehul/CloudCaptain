#!/usr/bin/env bash
set -euo pipefail
src="${1:?usage: backup.sh SRC_DIR DEST.tar.gz}"
dest="${2:?usage: backup.sh SRC_DIR DEST.tar.gz}"
tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT
grep -v '^#' "$src/files.txt" > "$tmp/list" || true
while IFS= read -r path; do
  cp -a "$src/$path" "$tmp/"
done < "$tmp/list"
tar -czf "$dest" -C "$tmp" .
