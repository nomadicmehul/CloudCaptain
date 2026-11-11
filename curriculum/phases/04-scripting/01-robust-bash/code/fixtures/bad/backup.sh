#!/usr/bin/env bash
set -e
src="$1"
dest=$2
tmp=$(mktemp -d)
cat "$src/files.txt" | grep -v '^#' > "$tmp/list"
while read path; do
  cp -a "$src/$path" "$tmp/"
done < "$tmp/list"
tar -czf "$dest" -C $tmp .
rm -rf "$tmp/"*
