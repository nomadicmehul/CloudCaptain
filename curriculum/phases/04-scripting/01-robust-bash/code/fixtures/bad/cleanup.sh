#!/usr/bin/env bash
set -euo pipefail
cache_dir="$1"
days="$2"
if [ $days -gt 30 ]; then
  echo "refusing to keep more than 30 days" >&2
  exit 2
fi
find "$cache_dir" -type f -mtime +"$days" -delete
rm -rf "$cache_dir/"*
logger -t cleanup $*
