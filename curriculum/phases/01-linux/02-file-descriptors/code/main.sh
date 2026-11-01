#!/usr/bin/env bash
set -euo pipefail

PID="${1:?usage: main.sh PID   (the fdwatch report with ls, awk and readlink only)}"
[[ -d "/proc/$PID/fd" ]] || { echo "no readable /proc/$PID/fd (wrong PID, or run with sudo)" >&2; exit 1; }

COMM="$(cat "/proc/$PID/comm")"
OPEN="$(ls "/proc/$PID/fd" | wc -l | tr -d ' ')"
read -r SOFT HARD < <(awk '/^Max open files/ {print $4, $5}' "/proc/$PID/limits")
echo "pid $PID ($COMM): $OPEN open file descriptors, soft limit $SOFT, hard limit $HARD"

echo "by kind:"
for fd in /proc/"$PID"/fd/*; do readlink "$fd"; done \
  | sed -E 's/^socket:.*/socket/; s/^pipe:.*/pipe/; s/^anon_inode:\[?([a-z_-]+)\]?$/\1/; s#^/dev/.*#device#; s#^/.* \(deleted\)$#file (deleted)#; s#^/.*#file#' \
  | sort | uniq -c | sort -rn | awk '{printf "  %-20s %6d\n", $2, $1}'

echo "same path open more than once:"
for fd in /proc/"$PID"/fd/*; do readlink "$fd"; done | grep '^/' | sort | uniq -c | sort -rn | awk '$1 > 1 {printf "  %6d  %s\n", $1, $2}'

echo "deleted but still open (all processes you can read):"
find /proc/[0-9]*/fd -lname '*(deleted)' -printf '  pid %h fd %f -> %l\n' 2>/dev/null | sed 's#/proc/##; s#/fd##' | head -20 || true
