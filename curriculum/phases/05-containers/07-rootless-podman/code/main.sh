#!/usr/bin/env bash
set -euo pipefail

[[ "$(uname -s)" == "Linux" ]] || { echo "needs Linux (user namespaces); on macOS use: python3 code/main.py map --user NAME --uid N --subuid FILE" >&2; exit 1; }

USER_NAME="$(id -un)"
if [[ $# -gt 0 && "$1" != "--" ]]; then USER_NAME="$1"; shift; fi
if [[ "${1:-}" == "--" ]]; then shift; fi
CMD=("$@")
if [[ ${#CMD[@]} -eq 0 ]]; then CMD=(sh -c 'id; cat /proc/self/uid_map'); fi
USER_UID="$(id -u "$USER_NAME")"

echo "subordinate ranges for $USER_NAME (uid $USER_UID):"
grep -H -E "^($USER_NAME|$USER_UID):" /etc/subuid /etc/subgid 2>/dev/null \
  || echo "  none; fix: sudo usermod --add-subuids 100000-165535 --add-subgids 100000-165535 $USER_NAME"

echo "this shell's user namespace (initial = identity map):"
cat /proc/self/uid_map

echo "inside unshare -Ur (single-user map written to /proc/self/uid_map by unshare itself):"
unshare -Ur "${CMD[@]}"

if command -v podman >/dev/null; then
  echo "inside podman unshare (the full /etc/subuid range, written by newuidmap):"
  podman unshare cat /proc/self/uid_map
else
  echo "(podman not installed; podman unshare skipped)"
fi
