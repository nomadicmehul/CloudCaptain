#!/usr/bin/env bash
set -euo pipefail

usage() { echo "usage: main.sh CMD [ARGS...]   (the tracelens report with strace, grep, awk and sed only)" >&2; exit 2; }
[[ $# -ge 1 ]] || usage
[[ "$(uname -s)" == "Linux" ]] || { echo "needs Linux: strace is built on ptrace(2); on macOS use dtruss or fs_usage, or run this in a Linux container" >&2; exit 1; }
command -v strace >/dev/null || { echo "strace not installed: sudo apt-get install -y strace (Debian/Ubuntu), apk add strace (Alpine)" >&2; exit 1; }

OUT="$(mktemp)"
trap 'rm -f "$OUT" "$OUT.c"' EXIT

set +e
strace -f -tt -T -y -s 200 -e trace=%file,%network,%process,write -o "$OUT" "$@" >/dev/null 2>&1
CODE=$?
set -e
echo "traced: $* (exit $CODE), $(wc -l < "$OUT" | tr -d ' ') lines in $OUT"

echo "exit status per process:"
grep -E '\+\+\+ (exited with|killed by) ' "$OUT" | sed -E 's/^([0-9]+) [0-9:.]+ \+\+\+ (.*) \+\+\+$/  pid \1: \2/' || true

echo "failing calls by errno:"
grep -oE '= -1 E[A-Z0-9_]+' "$OUT" | awk '{print $3}' | sort | uniq -c | sort -rn | awk '{printf "  %-16s %6d\n", $2, $1}' || true

echo "ENOENT paths in the order they were tried (the search path):"
awk -F'"' '/= -1 ENOENT/ {print "  " $2}' "$OUT" | grep -v ld.so.preload | uniq || true

echo "EACCES / EPERM targets:"
awk -F'"' '/= -1 (EACCES|EPERM)/ {print $2}' "$OUT" | sort | uniq -c | sort -rn | awk '{printf "  x%-4d %s\n", $1, $2}' || true

echo "refused, timed-out or unreachable connections (IPv4 address:port errno):"
grep -E '= -1 (ECONNREFUSED|ETIMEDOUT|EHOSTUNREACH|ENETUNREACH)' "$OUT" \
  | sed -nE 's/.*htons\(([0-9]+)\).*inet_addr\("([^"]+)"\).*= -1 (E[A-Z]+).*/\2:\1 \3/p' | sort | uniq -c | sort -rn | awk '{printf "  x%-4d %s  %s\n", $1, $2, $3}' || true

echo "the last 3 things written to stderr:"
grep -E ' write\(2[<,]' "$OUT" | tail -3 | sed -E 's/.*write\(2[^,]*, "(.*)", [0-9]+\) = .*/  \1/' || true

echo "slowest 5 calls (-T; resumed calls not included):"
grep -E '^[0-9]+ [0-9:.]+ [a-z_0-9]+\(.*<[0-9.]+>$' "$OUT" \
  | sed -E 's/^([0-9]+) [0-9:.]+ ([a-z_0-9]+)\(.*<([0-9.]+)>$/\3 \2 pid \1/' | sort -rn | head -5 | awk '{printf "  %ss  %-16s %s %s\n", $1, $2, $3, $4}' || true

echo "strace -c (a second run of the command; counts can differ a little between runs):"
strace -f -c -o "$OUT.c" "$@" >/dev/null 2>&1 || true
head -15 "$OUT.c" | sed 's/^/  /'
