#!/usr/bin/env bash
set -euo pipefail

BOX_HOSTNAME="${1:-box}"
if [[ $# -gt 0 ]]; then shift; fi
if [[ "${1:-}" == "--" ]]; then shift; fi
CMD=("$@")
if [[ ${#CMD[@]} -eq 0 ]]; then CMD=(/bin/sh); fi

exec unshare --uts --pid --mount --fork --mount-proc \
  /bin/sh -c 'hostname "$1"; shift; exec "$@"' sh "$BOX_HOSTNAME" "${CMD[@]}"
