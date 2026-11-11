#!/usr/bin/env bash
set -euo pipefail
out="$1"
shift
mode="${MODE:-text}"
probe() {
  ping -c 1 -W 1 "$1" > /dev/null
}
for h in "$@"; do
  if probe "$h"; then
    echo "$h up" >> "$out"
  fi
done
case $mode in
  text) cat "$out" ;;
  json) printf '{"up": %s}\n' "$(wc -l < "$out")" ;;
esac
if [[ $mode == text && -s $out ]]; then
  ssh-keyscan "$@" >> known_hosts
fi
cat <<EOF
report for $mode written to $out
EOF
wc -l ./*.txt
