#!/usr/bin/env bash
set -euo pipefail

usage() { echo "usage: main.sh [PID|self] [PATH...]   (capcheck by hand: /proc/PID/status, bash arithmetic, capsh, getpcaps, stat, getcap)" >&2; exit 2; }
[[ "${1:-}" == "-h" || "${1:-}" == "--help" ]] && usage
[[ "$(uname -s)" == "Linux" ]] || { echo "needs Linux (/proc/PID/status); on macOS save a status file on a Linux box and run: python3 code/main.py --explain status.txt" >&2; exit 1; }

PID="${1:-self}"
[[ $# -gt 0 ]] && shift
[[ "$PID" == "self" ]] && PID=$$
[[ -r "/proc/$PID/status" ]] || { echo "no readable /proc/$PID/status (wrong PID, or run with sudo)" >&2; exit 1; }

NAMES=(chown dac_override dac_read_search fowner fsetid kill setgid setuid setpcap linux_immutable
  net_bind_service net_broadcast net_admin net_raw ipc_lock ipc_owner sys_module sys_rawio sys_chroot sys_ptrace
  sys_pacct sys_admin sys_boot sys_nice sys_resource sys_time sys_tty_config mknod lease audit_write
  audit_control setfcap mac_override mac_admin syslog wake_alarm block_suspend audit_read perfmon bpf
  checkpoint_restore)

decode() {
  local hex="$1" mask bit names=()
  mask=$((16#$hex))
  for ((bit = 0; bit < ${#NAMES[@]}; bit++)); do
    if (( (mask >> bit) & 1 )); then names+=("cap_${NAMES[bit]}"); fi
  done
  if [[ ${#names[@]} -eq 0 ]]; then
    echo "0 of ${#NAMES[@]}: none"
  else
    echo "${#names[@]} of ${#NAMES[@]}: $(IFS=,; echo "${names[*]}")"
  fi
}

field() { awk -v key="$1:" '$1 == key {print $2}' "/proc/$PID/status"; }

echo "== /proc/$PID/status =="
grep -E '^(Name|Uid|Gid|Groups|CapInh|CapPrm|CapEff|CapBnd|CapAmb|NoNewPrivs|Seccomp|Seccomp_filters):' "/proc/$PID/status" | sed 's/^/  /'

echo "== decoded by hand (bash arithmetic over the ${#NAMES[@]}-name table) =="
for set in CapEff CapPrm CapBnd CapAmb; do
  printf '  %-7s %s\n' "$set" "$(decode "$(field "$set")")"
done
case "$(field NoNewPrivs)" in
  1) echo "  NoNewPrivs 1: execve cannot add privileges (setuid bits and file capabilities are ignored)" ;;
  0) echo "  NoNewPrivs 0: execve may add privileges through setuid bits and file capabilities" ;;
esac

if command -v capsh >/dev/null; then
  echo "== capsh --decode (libcap's table, for comparison) =="
  capsh --decode="$(field CapEff)" | sed 's/^/  /'
else
  echo "  (capsh not installed; apt-get install libcap2-bin)"
fi

if command -v getpcaps >/dev/null; then
  echo "== getpcaps $PID =="
  getpcaps "$PID" 2>&1 | sed 's/^/  /'
fi

for path in "$@"; do
  echo "== $path =="
  stat -c '  %A %a %U %G %n' "$path"
  if command -v getcap >/dev/null; then
    getcap "$path" 2>/dev/null | sed 's/^/  file capabilities: /' || true
  fi
done
