#!/bin/bash
# usage: gh_api.sh METHOD /repos/... [payload.json]   -- token from the osxkeychain git credential, never printed
set -euo pipefail
METHOD="$1"; API_PATH="$2"; PAYLOAD="${3:-}"
TOKEN=$(printf 'protocol=https\nhost=github.com\n' | git credential fill | awk -F= '$1=="password"{print $2}')
ARGS=(-sS -X "$METHOD" -H "Accept: application/vnd.github+json" -H "X-GitHub-Api-Version: 2022-11-28" -H "Authorization: Bearer $TOKEN")
if [ -n "$PAYLOAD" ]; then ARGS+=(-H "Content-Type: application/json" --data-binary "@$PAYLOAD"); fi
curl "${ARGS[@]}" "https://api.github.com$API_PATH"
