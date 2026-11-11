#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."

fail() { echo "SMOKE FAIL: $*" >&2; exit 1; }
[[ "$(uname -s)" == "Linux" ]] || fail "needs Linux"
command -v python3 >/dev/null || fail "needs python3"
command -v jq >/dev/null || fail "needs jq"
LESSON="$PWD"
WORK="$(mktemp -d)"
cleanup() {
  exec 9>&- 2>/dev/null || true
  rm -rf "$WORK"
}
trap cleanup EXIT

mkdir -p "$WORK/bin" "$WORK/xdg"
printf '#!/bin/sh\nexec python3 %s/code/main.py "$@"\n' "$LESSON" > "$WORK/bin/ccops"
chmod +x "$WORK/bin/ccops"
export PATH="$WORK/bin:$PATH" HOME="$WORK" XDG_CONFIG_HOME="$WORK/xdg"
unset NO_COLOR CCOPS_CONFIG CCOPS_INVENTORY CCOPS_COLOR CCOPS_OUTPUT CCOPS_LOCK_TIMEOUT
INV="$WORK/inventory.json"
cp code/fixtures/inventory.json "$INV"
export CCOPS_INVENTORY="$INV"

cat > "$WORK/on_tty.py" <<'PY'
import os, pty, select, subprocess, sys, time
feed = os.environ.get("PTY_FEED", "").encode()
master, slave = pty.openpty()
proc = subprocess.Popen(sys.argv[1:], stdin=slave, stdout=slave, stderr=slave, close_fds=True)
os.close(slave)
if feed:
    os.write(master, feed)
chunks, deadline = [], time.monotonic() + 30
while time.monotonic() < deadline:
    ready, _, _ = select.select([master], [], [], 0.5)
    if not ready:
        if proc.poll() is not None:
            break
        continue
    try:
        data = os.read(master, 65536)
    except OSError:
        break
    if not data:
        break
    chunks.append(data)
code = proc.wait(timeout=30)
sys.stdout.buffer.write(b"".join(chunks))
sys.exit(code)
PY
on_tty() { python3 "$WORK/on_tty.py" "$@"; }

CODE=0
run() {
  set +e
  "$@" > "$WORK/out" 2> "$WORK/err"
  CODE=$?
  set -e
}
expect() {
  local want="$1" stream="$2"; shift 2
  run "$@"
  [[ "$CODE" == "$want" ]] || fail "'$*' exited $CODE, want $want; stdout: $(cat "$WORK/out"); stderr: $(cat "$WORK/err")"
  case "$stream" in
    data) [[ -s "$WORK/out" && ! -s "$WORK/err" ]] || fail "'$*' should write stdout only; stdout: $(cat "$WORK/out"); stderr: $(cat "$WORK/err")" ;;
    json) jq -e . "$WORK/out" >/dev/null || fail "'$*' stdout is not JSON: $(cat "$WORK/out")"
          [[ ! -s "$WORK/err" ]] || fail "'$*' wrote to stderr with --json: $(cat "$WORK/err")" ;;
    error) [[ ! -s "$WORK/out" ]] || fail "'$*' failed but wrote to stdout: $(cat "$WORK/out")"
           grep -q '^ccops: error: ' "$WORK/err" || fail "'$*' should explain itself on stderr: $(cat "$WORK/err")" ;;
    silent) [[ ! -s "$WORK/out" ]] || fail "'$*' should print nothing on stdout: $(cat "$WORK/out")" ;;
  esac
  printf '  %-4s %-6s %s\n' "$CODE" "$stream" "$*"
}

echo "▸ unit tests"
python3 -m unittest discover -s code/tests > "$WORK/unit.txt" 2>&1 || fail "unit tests: $(tail -30 "$WORK/unit.txt")"
tail -1 "$WORK/unit.txt"

echo "▸ exit codes, stdout for data, stderr for diagnostics"
expect 0 data ccops --help
expect 0 data ccops hosts remove --help
expect 0 data ccops --version
expect 0 data ccops hosts list
expect 0 json ccops hosts list --json
expect 0 json ccops hosts list -o json --env prod --role web
expect 0 json ccops hosts show web-1 --json
expect 0 json ccops config --explain --json
expect 0 data ccops hosts list --quiet --state active
[[ "$(cat "$WORK/out")" == $'db-1\nweb-1' ]] || fail "--quiet should print names only: $(cat "$WORK/out")"
expect 2 error ccops hosts rmove
grep -q "did you mean 'remove'" "$WORK/err" || fail "no suggestion: $(cat "$WORK/err")"
expect 2 error ccops hosts list --colour never
expect 2 error ccops hosts list --json --output text
expect 2 error ccops hosts list --quiet --verbose
expect 2 error ccops hosts list --lock-timeout soon
expect 2 error ccops
expect 3 error ccops hosts show web-3
expect 1 error ccops hosts list -i "$WORK/missing.json"
expect 0 json ccops hosts list --json --color always
if grep -q $'\033\[' "$WORK/out"; then fail "--json output must never carry colour codes"; fi

echo "▸ every change has --dry-run, and repeating an add changes nothing"
BEFORE="$(sha256sum "$INV")"
expect 0 data ccops hosts add web-3 --env prod --role web --ip 10.0.1.13 --tag canary --dry-run
[[ "$(cat "$WORK/out")" == "would add web-3" ]] || fail "dry-run add: $(cat "$WORK/out")"
expect 0 json ccops hosts remove web-1 web-2 -n --json
jq -e '.dry_run == true and .removed == ["web-1","web-2"]' "$WORK/out" >/dev/null || fail "dry-run remove: $(cat "$WORK/out")"
[[ "$(sha256sum "$INV")" == "$BEFORE" ]] || fail "a dry run changed the inventory"
expect 0 json ccops hosts add web-3 --env prod --role web --ip 10.0.1.13 --tag canary --json
jq -e '.changed == true' "$WORK/out" >/dev/null || fail "first add should change: $(cat "$WORK/out")"
AFTER="$(sha256sum "$INV")"
expect 0 json ccops hosts add web-3 --env prod --role web --ip 10.0.1.13 --tag canary --json
jq -e '.changed == false' "$WORK/out" >/dev/null || fail "second add should be a no-op: $(cat "$WORK/out")"
[[ "$(sha256sum "$INV")" == "$AFTER" ]] || fail "a repeated add rewrote the inventory"
expect 1 error ccops hosts add web-3 --env prod --role web --ip 10.0.1.99
jq -e '.hosts | map(.name) == ["db-1","web-1","web-2","web-3","web-9"]' "$INV" >/dev/null || fail "inventory after add: $(cat "$INV")"

echo "▸ a destructive command refuses without a terminal unless --yes"
BEFORE="$(sha256sum "$INV")"
set +e
echo y | ccops hosts remove web-9 > "$WORK/out" 2> "$WORK/err"
CODE=$?
set -e
[[ "$CODE" == "4" && ! -s "$WORK/out" ]] || fail "piped remove should exit 4 with empty stdout, got $CODE: $(cat "$WORK/out")"
grep -q "stdin is not a terminal" "$WORK/err" || fail "refusal should say why: $(cat "$WORK/err")"
[[ "$(sha256sum "$INV")" == "$BEFORE" ]] || fail "a refused remove changed the inventory"
echo "  4    error  echo y | ccops hosts remove web-9"
PTY_FEED=$'n\n' on_tty ccops hosts remove web-9 > "$WORK/tty.txt" && fail "answering n should refuse: $(cat "$WORK/tty.txt")"
grep -q '\[y/N\]' "$WORK/tty.txt" || fail "a terminal should be asked: $(cat "$WORK/tty.txt")"
PTY_FEED=$'y\n' on_tty ccops hosts remove web-9 > "$WORK/tty.txt" || fail "answering y on a terminal should remove: $(cat "$WORK/tty.txt")"
grep -q "removed web-9" "$WORK/tty.txt" || fail "remove on a terminal: $(cat "$WORK/tty.txt")"
expect 0 data ccops hosts remove web-3 --yes
[[ "$(cat "$WORK/out")" == "removed web-3" ]] || fail "remove --yes: $(cat "$WORK/out")"
expect 3 error ccops hosts remove web-3 --yes
expect 0 silent ccops hosts remove web-3 --yes --missing-ok --quiet
echo "  terminal: asked [y/N], n refused, y removed"

echo "▸ colour only on a terminal, never with NO_COLOR or in a pipe"
on_tty ccops hosts list > "$WORK/tty.txt" || fail "list on a terminal: $(cat "$WORK/tty.txt")"
grep -q $'\033\[' "$WORK/tty.txt" || fail "a terminal should get colour: $(cat -v "$WORK/tty.txt")"
NO_COLOR=1 on_tty ccops hosts list > "$WORK/tty.txt" || fail "list with NO_COLOR"
if grep -q $'\033\[' "$WORK/tty.txt"; then fail "NO_COLOR must suppress colour: $(cat -v "$WORK/tty.txt")"; fi
TERM=dumb on_tty ccops hosts list > "$WORK/tty.txt" || fail "list with TERM=dumb"
if grep -q $'\033\[' "$WORK/tty.txt"; then fail "TERM=dumb must suppress colour"; fi
ccops hosts list > "$WORK/pipe.txt"
if grep -q $'\033\[' "$WORK/pipe.txt"; then fail "a pipe must not get colour: $(cat -v "$WORK/pipe.txt")"; fi
NO_COLOR=1 ccops hosts list --color always > "$WORK/pipe.txt"
grep -q $'\033\[' "$WORK/pipe.txt" || fail "--color always is an explicit flag and beats NO_COLOR"
echo "  terminal: colour; NO_COLOR, TERM=dumb, pipe: none; --color always: colour"

echo "▸ config --explain names the source of each value when flag, env and file disagree"
mkdir -p "$WORK/etc"
printf '# ccops config\ninventory = hosts.json\ncolor = never\nlock_timeout = 2\noutput = json\n' > "$WORK/etc/config"
CCOPS_CONFIG="$WORK/etc/config" CCOPS_INVENTORY="" CCOPS_LOCK_TIMEOUT=3 CCOPS_COLOR=always \
  ccops config --explain --color auto -o text > "$WORK/explain.txt" || fail "config --explain"
cat "$WORK/explain.txt"
CCOPS_CONFIG="$WORK/etc/config" CCOPS_INVENTORY="" CCOPS_LOCK_TIMEOUT=3 CCOPS_COLOR=always \
  ccops config --explain --color auto --json > "$WORK/explain.json" 2> "$WORK/err" || fail "config --explain --json: $(cat "$WORK/err")"
src() { jq -r --arg k "$1" '.settings[] | select(.key == $k) | "\(.source) \(.origin) \(.value)"' "$WORK/explain.json"; }
[[ "$(src color)" == "flag --color auto" ]] || fail "color: $(src color)"
[[ "$(src output)" == "flag --json json" ]] || fail "output: $(src output)"
[[ "$(src lock_timeout)" == "env CCOPS_LOCK_TIMEOUT 3" ]] || fail "lock_timeout: $(src lock_timeout)"
[[ "$(src inventory)" == "file $WORK/etc/config:2 $WORK/etc/hosts.json" ]] || fail "inventory: $(src inventory)"
jq -e '.config_file.origin == "CCOPS_CONFIG" and .config_file.exists' "$WORK/explain.json" >/dev/null || fail "config file origin: $(cat "$WORK/explain.json")"
CCOPS_CONFIG="$WORK/etc/config" CCOPS_INVENTORY="" ccops config --explain --json --color never > "$WORK/explain.json"
[[ "$(src lock_timeout)" == "file $WORK/etc/config:4 2" ]] || fail "file should win once env is gone: $(src lock_timeout)"
expect 0 json ccops config --explain --json
cp "$WORK/out" "$WORK/explain.json"
[[ "$(src lock_timeout)" == "default built-in default 5" ]] || fail "default: $(src lock_timeout)"
printf 'inventry = x\n' > "$WORK/etc/bad"
expect 2 error ccops config --config "$WORK/etc/bad"
grep -q "bad:1: unknown key 'inventry'; did you mean 'inventory'" "$WORK/err" || fail "bad config: $(cat "$WORK/err")"

echo "▸ a closed pipe is exit 141 and no traceback"
python3 - "$WORK/big.json" <<'PY'
import json, sys
hosts = [{"name": f"h-{i:05d}", "env": "prod", "role": "web", "ip": f"10.{i // 65536}.{i // 256 % 256}.{i % 256}"} for i in range(30000)]
json.dump({"version": 1, "hosts": hosts}, open(sys.argv[1], "w"))
PY
set +e
ccops hosts list --json -i "$WORK/big.json" 2> "$WORK/err" | head -c 20 > /dev/null
CODES=("${PIPESTATUS[@]}")
set -e
[[ "${CODES[0]}" == "141" ]] || fail "writer into a closed pipe should exit 141, got ${CODES[0]}: $(cat "$WORK/err")"
if grep -q Traceback "$WORK/err"; then fail "broken pipe printed a traceback: $(cat "$WORK/err")"; fi
echo "  ccops exit ${CODES[0]}, stderr $(wc -c < "$WORK/err") bytes"

if command -v flock >/dev/null; then
  echo "▸ flock(1) and ccops share one lock: a held lock times out with exit 1"
  exec 9> "$INV.lock"
  flock -n 9 || fail "flock(1) could not take $INV.lock"
  expect 1 error ccops hosts add web-4 --env prod --role web --ip 10.0.1.14 --lock-timeout 0.5
  grep -q "locked by another writer" "$WORK/err" || fail "lock timeout message: $(cat "$WORK/err")"
  exec 9>&-
else
  echo "  (flock not installed; lock interop skipped)"
fi
expect 0 data ccops hosts add web-4 --env prod --role web --ip 10.0.1.14 --lock-timeout 5

echo "▸ generated Bash completion in a non-interactive bash"
ccops completion bash > "$WORK/ccops.bash"
bash -n "$WORK/ccops.bash" || fail "completion script does not parse"
cat > "$WORK/probe.bash" <<'EOF'
source "$1"
complete -p ccops
t() { COMP_WORDS=("$@"); COMP_CWORD=$(( ${#COMP_WORDS[@]} - 1 )); COMPREPLY=(); _ccops_complete; echo "${COMPREPLY[*]}"; }
t ccops ""
t ccops ho
t ccops hosts ""
t ccops hosts remove --d
t ccops hosts list --
t ccops --color ""
t ccops hosts list --state m
t ccops completion ""
t ccops hosts show web
t ccops -i "$2" hosts remove h-0000
compgen -W "$(ccops hosts list --quiet)" -- db
EOF
bash --norc --noprofile "$WORK/probe.bash" "$WORK/ccops.bash" "$WORK/big.json" > "$WORK/completion.txt" 2> "$WORK/err" || fail "completion probe: $(cat "$WORK/err")"
cat "$WORK/completion.txt"
mapfile -t LINES < "$WORK/completion.txt"
[[ "${LINES[0]}" == "complete -F _ccops_complete ccops" ]] || fail "complete -p: ${LINES[0]}"
[[ "${LINES[1]}" == "hosts config completion man" ]] || fail "top-level commands: ${LINES[1]}"
[[ "${LINES[2]}" == "hosts" ]] || fail "prefix ho: ${LINES[2]}"
[[ "${LINES[3]}" == "list show add remove" ]] || fail "hosts subcommands: ${LINES[3]}"
[[ "${LINES[4]}" == "--dry-run" ]] || fail "remove --d: ${LINES[4]}"
[[ " ${LINES[5]} " == *" --env "* && " ${LINES[5]} " == *" --json "* && " ${LINES[5]} " != *" --yes "* ]] || fail "list flags: ${LINES[5]}"
[[ "${LINES[6]}" == "auto always never" ]] || fail "--color values: ${LINES[6]}"
[[ "${LINES[7]}" == "maintenance" ]] || fail "--state m: ${LINES[7]}"
[[ "${LINES[8]}" == "bash" ]] || fail "completion shells: ${LINES[8]}"
[[ "${LINES[9]}" == "web-1 web-2 web-4" ]] || fail "host names from the inventory: ${LINES[9]}"
[[ "${LINES[10]}" == "h-00000 h-00001 h-00002 h-00003 h-00004 h-00005 h-00006 h-00007 h-00008 h-00009" ]] || fail "-i is passed to the dynamic completion: ${LINES[10]}"
[[ "${LINES[11]}" == "db-1" ]] || fail "compgen -W: ${LINES[11]}"

echo "▸ generated man page renders"
ccops man > "$WORK/ccops.1"
if command -v groff >/dev/null; then
  groff -man -Tascii -P-cbou "$WORK/ccops.1" > "$WORK/man.txt" 2> "$WORK/err" || fail "groff: $(cat "$WORK/err")"
  for want in "EXIT STATUS" "--dry-run" "CCOPS_INVENTORY" "NO_COLOR" "ccops hosts remove"; do
    grep -qF -- "$want" "$WORK/man.txt" || fail "rendered man page lacks $want: $(head -40 "$WORK/man.txt")"
  done
  echo "  groff rendered $(wc -l < "$WORK/man.txt") lines"
else
  grep -q '^\.SH EXIT STATUS$' "$WORK/ccops.1" || fail "man page source: $(head -20 "$WORK/ccops.1")"
  echo "  (groff not installed; checked the roff source only)"
fi

if command -v kubectl >/dev/null; then
  echo "▸ Use It: how kubectl answers the same contract questions"
  run kubectl definitely-not-a-command
  [[ "$CODE" != "0" && ! -s "$WORK/out" ]] || fail "kubectl unknown command: $CODE $(cat "$WORK/out")"
  echo "  kubectl unknown command: exit $CODE (ccops: 2), message on stderr: $(head -1 "$WORK/err")"
  run kubectl version --client -o json
  [[ "$CODE" == "0" ]] && jq -e '.clientVersion.gitVersion' "$WORK/out" >/dev/null || fail "kubectl -o json: $(cat "$WORK/out")"
  echo "  kubectl -o json: stdout is JSON, client $(jq -r .clientVersion.gitVersion "$WORK/out")"
  run kubectl delete --help
  grep -q -- "--dry-run" "$WORK/out" || fail "kubectl delete should document --dry-run"
  if grep -q -- "--interactive" "$WORK/out"; then
    echo "  kubectl delete: --dry-run and an opt-in --interactive confirmation"
  else
    echo "  kubectl delete: --dry-run, no confirmation flag"
  fi
  kubectl completion bash > "$WORK/kubectl.bash"
  bash -n "$WORK/kubectl.bash" || fail "kubectl completion does not parse"
  if grep -q "__complete" "$WORK/kubectl.bash"; then
    echo "  kubectl completion asks the binary at runtime (kubectl __complete), as ccops does for host names"
  fi
else
  echo "  (kubectl not installed; Use It comparison skipped)"
fi

echo "SMOKE OK: scripting/cli-design"
