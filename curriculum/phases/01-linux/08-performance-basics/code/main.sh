#!/usr/bin/env bash
set -euo pipefail

INTERVAL="${1:-1}"
[[ "$(uname -s)" == "Linux" ]] || { echo "main.sh needs Linux /proc (on macOS run: python3 code/main.py --replay code/fixtures/saturated)" >&2; exit 2; }

NCPU="$(grep -c '^cpu[0-9]' /proc/stat)"
echo "▸ load and run queue (/proc/loadavg: 1 5 15 min, runnable/threads, last pid) on $NCPU cpus"
cat /proc/loadavg
grep -E '^procs_(running|blocked)' /proc/stat | awk '{printf "  %-14s %s\n", $1, $2}'

echo "▸ CPU over ${INTERVAL}s from two reads of /proc/stat (awk does the deltas)"
A="$(grep '^cpu ' /proc/stat)"
sleep "$INTERVAL"
B="$(grep '^cpu ' /proc/stat)"
awk -v a="$A" -v b="$B" '
  BEGIN {
    split(a, x, " "); split(b, y, " ")
    total = 0
    for (i = 2; i <= 9; i++) { d[i] = y[i] - x[i]; total += d[i] }
    if (total <= 0) { print "  no ticks elapsed"; exit }
    printf "  busy %.1f%%  us %.1f  sy %.1f  wa %.1f  st %.1f  id %.1f\n",
      (total - d[5] - d[6]) / total * 100, (d[2] + d[3]) / total * 100, d[4] / total * 100,
      d[6] / total * 100, d[9] / total * 100, d[5] / total * 100
  }'

echo "▸ memory: available is the number that matters, not free (/proc/meminfo)"
awk '/^(MemTotal|MemFree|MemAvailable|Dirty|Writeback|SwapTotal|SwapFree):/ {printf "  %-14s %8d MB\n", $1, $2 / 1024}' /proc/meminfo
awk '/^MemTotal/ {t = $2} /^MemAvailable/ {a = $2} END {if (t) printf "  available %.0f%%\n", a / t * 100}' /proc/meminfo
grep -E '^(pswpin|pswpout|oom_kill) ' /proc/vmstat | awk '{printf "  %-14s %s (cumulative; diff two reads)\n", $1, $2}'

echo "▸ pressure stall information (/proc/pressure/*: share of wall time tasks waited for the resource)"
if [[ -d /proc/pressure ]]; then
  for f in /proc/pressure/cpu /proc/pressure/memory /proc/pressure/io; do
    [[ -r "$f" ]] && printf '  %-7s %s\n' "$(basename "$f")" "$(head -n1 "$f")"
  done
else
  echo "  (no /proc/pressure: kernel without CONFIG_PSI, or booted with psi=0)"
fi

echo "▸ busiest block devices over ${INTERVAL}s (/proc/diskstats field 13 = ms busy, field 14 = weighted ms)"
D1="$(mktemp)"; D2="$(mktemp)"
cat /proc/diskstats > "$D1"; sleep "$INTERVAL"; cat /proc/diskstats > "$D2"
awk -v secs="$INTERVAL" 'NR == FNR { busy[$3] = $13; wq[$3] = $14; rd[$3] = $4; wr[$3] = $8; next }
  { parent = $3; sub(/p?[0-9]+$/, "", parent) }
  $3 !~ /^(loop|ram|sr|fd)/ && !(parent != $3 && (parent in busy)) && ($3 in busy) {
    printf "  %-10s %5.1f%% busy  queue %5.2f  %6.0f r/s %6.0f w/s\n", $3, ($13 - busy[$3]) / (secs * 10), ($14 - wq[$3]) / (secs * 1000), ($4 - rd[$3]) / secs, ($8 - wr[$3]) / secs }' "$D1" "$D2"
rm -f "$D1" "$D2"

echo "▸ processes in D state (uninterruptible sleep, almost always waiting on I/O)"
ps -eo pid,stat,comm --no-headers | awk '$2 ~ /^D/ {printf "  %s %s\n", $1, $3}'

echo "▸ top processes by CPU (ps shows the average since the process started; top and usetop use a window)"
ps -eo pid,pcpu,stat,comm --sort=-pcpu --no-headers | sed -n '1,5p' | awk '{printf "  %7s %5s%% %-4s %s\n", $1, $2, $3, $4}'

if command -v vmstat >/dev/null; then
  echo "▸ vmstat 1 3 (r = run queue, b = blocked, si/so = swap in/out, wa = iowait, st = steal)"
  vmstat 1 3
else
  echo "  (vmstat not installed: apt-get install procps / dnf install procps-ng)"
fi
if command -v top >/dev/null; then
  echo "▸ top -bn1 header: the %Cpu(s) line is the same /proc/stat delta, load is /proc/loadavg"
  top -bn1 | sed -n '1,5p'
fi
