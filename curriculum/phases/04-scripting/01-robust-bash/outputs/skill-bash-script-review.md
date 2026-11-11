---
name: bash-script-review
description: Review a Bash script or CI run block for the failures Bash hides by default (word splitting, ignored errors, empty variables, leaked temp files, destructive commands) with pinned ShellCheck and shfmt, then decide whether it should be Python
version: 1.0.0
phase: scripting
lesson: scripting/robust-bash
tags: [bash, shellcheck, shfmt, strict-mode, quoting, review, scripting]
---

# Bash script review

Use this before merging a new or changed `*.sh` file, a `run:` block in a CI workflow, an entrypoint script in an image, or a cron job; and when a script "succeeded" but did not do its job.

## 1. Let the tools read it first

```bash
bash -n script.sh                                        # parses? (syntax only, runs nothing)
shellcheck --version | grep '^version:'                  # record the version; pin it in CI with a sha256-verified download
shellcheck -f json1 --enable=useless-use-of-cat script.sh > sc.json; echo "exit $?"   # 0 clean, 1 findings, 2+ could not check
jq -r '.comments[] | "\(.line):\(.column) SC\(.code) \(.level): \(.message)"' sc.json
shfmt -i 2 -ci -d script.sh                              # formatting diff; empty output means formatted
```

For a CI `run:` block, copy it into a file with `#!/usr/bin/env bash` and `set -eo pipefail` first: that is what GitHub Actions runs for `shell: bash`. Every finding needs a fix or a `# shellcheck disable=SCxxxx` with a reason on the line above. A file-wide disable needs a stronger reason.

## 2. Header and strict mode

- First lines are `#!/usr/bin/env bash` (or `#!/bin/bash`) and `set -euo pipefail`. A `#!/bin/sh` script that uses `[[`, arrays or `$'...'` is broken on Debian and Ubuntu, where `sh` is `dash`.
- Know what strict mode does not cover, and look for each case:

| Pattern | Why it escapes `set -e` | Fix |
|---|---|---|
| `local out=$(cmd)` / `export X=$(cmd)` | the status is that of `local`/`export` (SC2155) | `local out; out=$(cmd)` |
| `if my_function; then`, `my_function && ...` | errexit is off for everything inside the function (SC2310, optional) | call it on its own line, then test `$?`, or make it return explicit statuses |
| `(( count++ ))` | returns 1 when the old value was 0, which kills the script | `count=$((count + 1))` |
| `grep pattern file \| wc -l` under `pipefail` | `grep` exits 1 on no match | `grep -c pattern file \|\| true`, or decide that no match is an error |
| `x=$(cmd1; cmd2)` | a command substitution does not inherit `-e` | `shopt -s inherit_errexit` (bash 4.4 and later) |
| `cmd \|\| true` everywhere | silences real failures | only where failure is an expected outcome; say why |

## 3. Quoting

Every expansion in a command's arguments, in `[ ]`, and in a redirection target is double-quoted unless splitting is the intent and the comment says so.

| Finding | Red flag | Fix |
|---|---|---|
| SC2086 | `cp $src $dest`, `[ $a = b ]`, `> $log` | `cp -- "$src" "$dest"`, `[[ $a == b ]]` or `[ "$a" = b ]`, `> "$log"` |
| SC2046 | `kill $(pgrep app)`, `rm $(find ...)` | `mapfile -t pids < <(pgrep app); kill "${pids[@]}"`, or `find ... -delete` / `-exec ... {} +` |
| SC2068, SC2048 | `exec $@`, `log $*` | `exec "$@"`; `"$*"` only when you want one joined string |
| SC2045 | `for f in $(ls)` | `for f in ./*; do [[ -e $f ]] \|\| continue; ...` |
| SC2162 | `while read line` | `while IFS= read -r line` |
| SC2006 | backticks | `$(...)` |
| SC2002 | `cat file \| grep x` | `grep x file` or `grep x < file` |

Arguments that start with a variable go after `--` (`rm -- "$f"`, `grep -- "$pattern"`) so a value starting with `-` is not an option.

## 4. Failure, cleanup and destructive commands

- Every `mktemp` is preceded by `trap cleanup EXIT`, and `cleanup` checks that the path is set before removing it. Background processes started with `&` are killed in the same `cleanup`.
- `rm -r`, `rm -rf` and `find ... -delete` on a variable path use `"${var:?}"`: `rm -rf "${dir:?}/"*` (SC2115). Check that the variable cannot be `/`, `.` or `$HOME` by construction, not by hope.
- `cd` is followed by `|| exit` (or the script has `set -e`), and the script does not depend on the caller's working directory: `cd "$(dirname "$0")"` or absolute paths.
- Writes that others read (reports, configs, state files) go to a temporary file in the same directory and `mv` into place, so readers never see half a file.
- External calls have timeouts: `curl --max-time`, `timeout 30 cmd`, `ssh -o ConnectTimeout=10`.
- Idempotence: running the script twice does not fail and does not do the work twice (`mkdir -p`, `ln -sfn`, check before create).

## 5. Interface

- Arguments are parsed in one place (`while (( $# )); do case "$1" in ...`), with `-h`/`--help`, `--` and an error for unknown flags. Required values use `${1:?usage: ...}`.
- Diagnostics and logs go to stderr (`printf '%s\n' "$*" >&2`); stdout carries only data so the script can be piped.
- Exit codes are documented: 0 success, 1 the job failed, 2 usage error. A `--dry-run` exists for anything that changes state.
- Environment variables the script reads are listed in the usage text, with defaults (`"${REGION:-eu-central-1}"`).

## 6. Prove it

```bash
PS4='+ ${BASH_SOURCE##*/}:${LINENO}:${FUNCNAME[0]:-main}: ' bash -x script.sh ARGS 2> trace.txt   # what actually ran
mkdir -p "/tmp/review/dir with space" && script.sh "/tmp/review/dir with space"                   # paths with spaces
script.sh "" ; echo "exit $?"                                                                     # empty argument: must fail with usage, not act on /
script.sh --bogus; echo "exit $?"                                                                 # 2 and a usage line on stderr
ls -la /tmp | tail -5                                                                             # nothing left behind after a failed run
```

Add a `bats` test for each bug found, and run `shellcheck` and `shfmt -d` in CI on every push.

## 7. Should this be Python?

| Signal | Stay in Bash | Move to Python |
|---|---|---|
| Size | under ~100 lines of glue between commands | logic that keeps growing |
| Data | lines and fields, one `jq` call | nested JSON, lists of records, maps |
| Errors | fail fast and let the caller retry | retries with backoff, partial failure, ordered cleanup of several resources |
| Tests | a few `bats` cases of the CLI | unit tests of the logic |
| Platforms | Linux only | macOS too (`/bin/bash` 3.2, BSD tools) or Windows |

If two or more rows say Python, recommend the conversion (the `convert-bash-to-python` prompt) instead of more fixes.

## Report

State: the ShellCheck version and the count of findings by code before and after, the `shfmt` result, the strict-mode header and any escapes from it found in step 2, every destructive command and how it is guarded, temp files and their cleanup, the exit codes and where logs go, what the `bash -x` and empty-argument runs showed, and the Bash-or-Python decision with the rows that decided it. List each change as `line: before -> after: the failure it prevents`. Do not report "added `|| true`" or a file-wide `disable` as a fix without the reason.
