#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat >&2 <<'USAGE'
usage: main.sh tree | which PATH | overlay | shadowed | usage
  the mountlens reports with findmnt, awk, df and /proc/self/mountinfo only
USAGE
  exit 2
}

[[ "$(uname -s)" == "Linux" ]] || { echo "needs Linux (/proc/self/mountinfo, findmnt)" >&2; exit 2; }
[[ $# -ge 1 ]] || usage
CMD="$1"
shift

case "$CMD" in
  tree)
    if command -v findmnt >/dev/null; then
      findmnt -o TARGET,SOURCE,FSTYPE,OPTIONS,PROPAGATION
    else
      awk '{ for (i = 7; i <= NF; i++) if ($i == "-") break; printf "%-44s %-28s %-10s %s\n", $5, $(i+2), $(i+1), $6 }' /proc/self/mountinfo
    fi
    ;;
  which)
    TARGET="${1:?usage: main.sh which PATH}"
    if command -v findmnt >/dev/null; then
      findmnt -T "$TARGET" -o TARGET,SOURCE,FSTYPE,OPTIONS,PROPAGATION
    else
      awk -v p="$TARGET" '
        { mp = $5; gsub(/\\040/, " ", mp)
          if (p == mp || mp == "/" || index(p, mp "/") == 1) { if (length(mp) >= length(best)) { best = mp; line = $0 } } }
        END { print line }' /proc/self/mountinfo
    fi
    ;;
  overlay)
    awk '{ for (i = 7; i <= NF; i++) if ($i == "-") break
           if ($(i+1) != "overlay") next
           n = split($(i+3), opts, ","); lowers = 0; upper = "no upperdir"
           for (j = 1; j <= n; j++) {
             if (opts[j] ~ /^lowerdir=/) lowers = split(substr(opts[j], 10), layers, ":")
             if (opts[j] ~ /^upperdir=/) upper = "upperdir present"
           }
           printf "%s  %d lower layer(s), %s, mount options %s\n", $5, lowers, upper, $6 }' /proc/self/mountinfo
    ;;
  shadowed)
    awk '{ print $5 }' /proc/self/mountinfo | sort | uniq -d | sed 's/^/stacked mounts on the same path: /'
    ;;
  usage)
    df -hT -x tmpfs -x devtmpfs -x squashfs
    echo
    df -i -x tmpfs -x devtmpfs -x squashfs
    ;;
  *)
    usage
    ;;
esac
