#!/usr/bin/env bash
set -euo pipefail

PROG="${0##*/}"
readonly PROG
readonly BACKTICK=$'\x60'
QUIET=0
VERBOSE=0
OUTPUT=""
WORK=""
STAGED=""
FILES=()

usage() {
  cat <<EOF
usage: $PROG [-q] [-v] [-o REPORT] [--] FILE...

The checks grep can do on a Bash script, reported with bashlint's ids:
  BL002 for-over-ls   BL004 missing-strict-mode   BL006 useless-cat
  BL007 read-without-r   BL009 backticks

  -o, --output FILE   also write the report to FILE (atomically)
  -q, --quiet         print nothing on stdout; use the exit status
  -v, --verbose       log each file to stderr

Exit status: 0 clean, 1 findings, 2 usage or I/O error.
EOF
}

log() { printf '%s: %s\n' "$PROG" "$*" >&2; }
debug() { if (( VERBOSE )); then log "$@"; fi; }
die() { log "error: $*"; exit 2; }

cleanup() {
  if [[ -n "$STAGED" && -e "$STAGED" ]]; then
    rm -f -- "$STAGED"
  fi
  if [[ -n "$WORK" ]]; then
    rm -rf -- "${WORK:?}"
  fi
}

parse_args() {
  while (( $# )); do
    case "$1" in
      -h|--help) usage; exit 0 ;;
      -q|--quiet) QUIET=1 ;;
      -v|--verbose) VERBOSE=1 ;;
      -o|--output)
        [[ $# -ge 2 ]] || die "$1 needs a file name"
        OUTPUT="$2"
        shift ;;
      --) shift; break ;;
      -*) usage >&2; die "unknown option: $1" ;;
      *) break ;;
    esac
    shift
  done
  (( $# )) || { usage >&2; die "no files given"; }
  FILES=("$@")
}

emit() {
  local file="$1" line="$2" id="$3" message="$4"
  printf '%s:%s: %s %s\n' "$file" "$line" "$id" "$message" >> "$WORK/findings"
}

grep_rule() {
  local file="$1" id="$2" message="$3" pattern="$4" exclude="${5:-}"
  local line
  grep -nE -- "$pattern" "$file" > "$WORK/hits" || true
  grep -vE '^[0-9]+:[[:space:]]*#' "$WORK/hits" > "$WORK/code" || true
  if [[ -n "$exclude" ]]; then
    grep -vE -- "$exclude" "$WORK/code" > "$WORK/kept" || true
  else
    cp -- "$WORK/code" "$WORK/kept"
  fi
  cut -d: -f1 "$WORK/kept" > "$WORK/lines"
  while IFS= read -r line; do
    emit "$file" "$line" "$id" "$message"
  done < "$WORK/lines"
}

lint_file() {
  local file="$1"
  [[ -f "$file" && -r "$file" ]] || die "not a readable file: $file"
  debug "checking $file"
  if ! grep -Eq '^[[:space:]]*set -E?euo pipefail([[:space:]]|$)' "$file"; then
    emit "$file" 1 BL004 "missing-strict-mode: no 'set -euo pipefail' line"
  fi
  grep_rule "$file" BL002 "for-over-ls: iterate over a glob, not over ls output" \
    '(^|[;[:space:]])for[[:space:]]+[[:alnum:]_]+[[:space:]]+in[[:space:]]+\$\(ls'
  grep_rule "$file" BL006 "useless-cat: give the file to the next command or use <" \
    '(^|[;&[:space:]])cat[[:space:]]+[^-|<>[:space:]][^|<>[:space:]]*[[:space:]]*\|[^|]'
  grep_rule "$file" BL007 "read-without-r: use read -r" \
    '(^|[;&|[:space:]])read([[:space:]]|$)' \
    'read([[:space:]]+-[[:alpha:]]+)*[[:space:]]+-[[:alpha:]]*r'
  grep_rule "$file" BL009 "backticks: use \$(...)" "$BACKTICK"
}

publish() {
  local count
  count="$(wc -l < "$WORK/report")"
  if (( ! QUIET )); then
    cat -- "$WORK/report"
  fi
  if [[ -n "$OUTPUT" ]]; then
    STAGED="$(mktemp "${OUTPUT}.XXXXXX")"
    cp -- "$WORK/report" "$STAGED"
    mv -f -- "$STAGED" "$OUTPUT"
    STAGED=""
    debug "report written to $OUTPUT"
  fi
  log "$((count)) finding(s) in ${#FILES[@]} file(s)"
  (( count == 0 ))
}

main() {
  [[ "$(uname -s)" == "Linux" ]] || die "written for Linux with bash 4 or later; run it in a Linux container or VM"
  parse_args "$@"
  trap cleanup EXIT
  WORK="$(mktemp -d)"
  : > "$WORK/findings"
  local file
  for file in "${FILES[@]}"; do
    lint_file "$file"
  done
  sort -t: -k1,1 -k2,2n "$WORK/findings" > "$WORK/report"
  publish
}

main "$@"
