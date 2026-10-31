#!/usr/bin/env bash
set -euo pipefail

ROOT="${CGROUP_ROOT:-/sys/fs/cgroup}"
usage() { echo "usage: main.sh create NAME [CPUS] [MEMORY_BYTES] [PIDS] | run NAME -- CMD... | stat NAME | explain PID | delete NAME" >&2; exit 2; }
[[ "$(uname -s)" == "Linux" ]] || { echo "needs Linux (cgroup v2 under $ROOT)" >&2; exit 2; }
[[ -f "$ROOT/cgroup.controllers" ]] || { echo "$ROOT is not a cgroup v2 root; cgroup v1 is legacy, boot with systemd.unified_cgroup_hierarchy=1" >&2; exit 2; }

CMD="${1:-}"; [[ -n "$CMD" ]] || usage; shift

enable_controllers() {
  local parent="$1" c
  for c in cpu memory pids; do
    grep -qw "$c" "$parent/cgroup.subtree_control" && continue
    if [[ "$parent" != "$ROOT" && -s "$parent/cgroup.procs" ]]; then
      echo "cannot enable +$c in ${parent#"$ROOT"}: it holds processes, and a group cannot both hold processes and hand controllers to children (no-internal-processes rule)" >&2
      exit 1
    fi
    echo "+$c" > "$parent/cgroup.subtree_control"
  done
}

show() {
  local dir="$1" f
  for f in "${@:2}"; do
    [[ -f "$dir/$f" ]] && printf '  %-18s %s\n' "$f" "$(tr '\n' ' ' < "$dir/$f")"
  done
  return 0
}

case "$CMD" in
  create)
    NAME="${1:?NAME}"; CPUS="${2:-}"; MEM="${3:-}"; PIDS="${4:-}"
    G="$ROOT"
    IFS=/ read -ra PARTS <<<"${NAME#/}"
    for part in "${PARTS[@]}"; do
      [[ -n "$part" ]] || continue
      enable_controllers "$G"
      G="$G/$part"
      mkdir -p "$G"
    done
    [[ -z "$CPUS" ]] || awk -v c="$CPUS" 'BEGIN { printf "%d 100000\n", c * 100000 }' > "$G/cpu.max"
    if [[ -n "$MEM" ]]; then
      echo "$MEM" > "$G/memory.max"
      [[ ! -f "$G/memory.swap.max" ]] || echo 0 > "$G/memory.swap.max"
    fi
    [[ -z "$PIDS" ]] || echo "$PIDS" > "$G/pids.max"
    echo "created ${G#"$ROOT"}"
    show "$G" cpu.max memory.max memory.swap.max pids.max
    ;;
  run)
    NAME="${1:?NAME}"; shift
    [[ "${1:-}" == "--" ]] && shift
    G="$ROOT/${NAME#/}"
    [[ -d "$G" ]] || { echo "no cgroup $NAME; create it first" >&2; exit 1; }
    [[ ! -s "$G/cgroup.subtree_control" ]] || { echo "$NAME hands controllers to children and cannot hold processes; run in a leaf" >&2; exit 1; }
    set +e
    sh -c 'echo $$ > "$1/cgroup.procs" && shift && exec "$@"' sh "$G" "$@"
    CODE=$?
    set -e
    echo "exit $CODE  memory.events: $(tr '\n' ' ' < "$G/memory.events" 2>/dev/null || echo n/a)" >&2
    exit "$CODE"
    ;;
  stat)
    NAME="${1:?NAME}"; G="$ROOT/${NAME#/}"
    [[ -d "$G" ]] || { echo "no cgroup $NAME" >&2; exit 1; }
    echo "cgroup /${NAME#/}  procs $(wc -l < "$G/cgroup.procs" | tr -d ' ')  controllers $(cat "$G/cgroup.controllers")  subtree_control $(cat "$G/cgroup.subtree_control")"
    show "$G" cpu.max cpu.weight cpu.stat memory.current memory.peak memory.max memory.high memory.swap.max memory.events pids.current pids.max pids.events
    [[ ! -f "$G/cpu.stat" ]] || awk '$1 == "nr_periods" { p = $2 } $1 == "nr_throttled" { t = $2 } $1 == "throttled_usec" { u = $2 } END { if (t > 0) printf "  -> CPU: throttled in %d%% of %d periods, %.2fs spent waiting for quota\n", t * 100 / p, p, u / 1e6 }' "$G/cpu.stat"
    [[ ! -f "$G/memory.events" ]] || awk '$1 == "oom_kill" && $2 > 0 { printf "  -> memory: %d OOM kill(s); an exit code 137 here was the kernel\n", $2 }' "$G/memory.events"
    [[ ! -f "$G/pids.events" ]] || awk '$1 == "max" && $2 > 0 { printf "  -> pids: fork() failed %d time(s) with EAGAIN\n", $2 }' "$G/pids.events"
    ;;
  explain)
    PID="${1:?PID}"
    [[ -r "/proc/$PID/cgroup" ]] || { echo "no readable /proc/$PID/cgroup" >&2; exit 1; }
    P="$(sed -n 's/^0:://p' "/proc/$PID/cgroup")"
    echo "pid $PID ($(cat "/proc/$PID/comm")) is in cgroup ${P:-?}"
    if grep -qv '^0::' "/proc/$PID/cgroup"; then echo "  note: cgroup v1 (legacy) lines present in /proc/$PID/cgroup"; fi
    DIR="$ROOT"
    printf '  %-40s cpu.max %-14s memory.max %-12s pids.max %s\n' "/" "$(cat "$DIR/cpu.max" 2>/dev/null || echo -)" "$(cat "$DIR/memory.max" 2>/dev/null || echo -)" "$(cat "$DIR/pids.max" 2>/dev/null || echo -)"
    IFS=/ read -ra PARTS <<<"${P#/}"
    for part in "${PARTS[@]}"; do
      [[ -n "$part" ]] || continue
      DIR="$DIR/$part"
      [[ -d "$DIR" ]] || { echo "  ${DIR#"$ROOT"} is not visible here (another cgroup namespace?)"; break; }
      printf '  %-40s cpu.max %-14s memory.max %-12s pids.max %s\n' "${DIR#"$ROOT"}" "$(cat "$DIR/cpu.max" 2>/dev/null || echo -)" "$(cat "$DIR/memory.max" 2>/dev/null || echo -)" "$(cat "$DIR/pids.max" 2>/dev/null || echo -)"
    done
    ;;
  delete)
    NAME="${1:?NAME}"; G="$ROOT/${NAME#/}"
    [[ -d "$G" && "$G" != "$ROOT" ]] || { echo "no cgroup $NAME" >&2; exit 1; }
    while IFS= read -r d; do
      if [[ -f "$d/cgroup.kill" ]]; then echo 1 > "$d/cgroup.kill"; else xargs -r kill -9 < "$d/cgroup.procs" 2>/dev/null || true; fi
    done < <(find "$G" -depth -type d)
    for _ in $(seq 1 50); do [[ -s "$G/cgroup.procs" ]] || break; sleep 0.1; done
    find "$G" -depth -type d -exec rmdir {} \;
    echo "removed ${G#"$ROOT"}"
    ;;
  *) usage ;;
esac
