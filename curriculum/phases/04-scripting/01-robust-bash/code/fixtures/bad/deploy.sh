#!/usr/bin/env bash
release=$1
target=$2
cd $target
tar -xzf $release
for f in $(ls); do
  chmod 0644 $f
done
echo "deployed $release at `date -u +%FT%TZ`" >&2
