---
name: cli-review
description: Review a command-line tool against its contract before release or in a pull request: exit codes, stdout versus stderr, --json, behaviour in pipes and without a terminal, configuration precedence and its explanation, dry runs and confirmations, help, completion and golden tests
version: 1.0.0
phase: scripting
lesson: scripting/cli-design
tags: [cli, bash, python, exit-codes, stdout, stderr, json, no-color, completion, review]
---

# CLI review

Use this when a pull request adds or changes a command, flag, output format or exit code of an internal tool, before a script becomes something other teams call, or when a pipeline that wraps a CLI breaks after an upgrade. Run every command in a scratch directory with a throwaway config so you do not read or change real data.

```bash
WORK="$(mktemp -d)"; export HOME="$WORK" XDG_CONFIG_HOME="$WORK/xdg"
TOOL=ccops                                   # the command under review
```

## 1. Record the exit-code and stream table

Run each invocation with stdout and stderr captured separately, and record the exit status:

```bash
probe() { "$@" >"$WORK/out" 2>"$WORK/err"; echo "exit=$? out=$(wc -c <"$WORK/out") err=$(wc -c <"$WORK/err")  $*"; }
probe $TOOL --help
probe $TOOL --version
probe $TOOL                                  # no command
probe $TOOL definitely-not-a-command
probe $TOOL list --no-such-flag
probe $TOOL show does-not-exist
probe $TOOL list                             # a normal success
probe $TOOL list --json
```

| Invocation | Expect | Flag it when |
|---|---|---|
| `--help`, `-h`, `COMMAND --help` | exit 0, help on stdout, stderr empty | help on stderr (cannot be piped to `less`), exit non-zero |
| unknown command or flag, bad value | exit 2 (or a documented usage code), stdout empty, stderr names the problem and what to run | exit 1 (indistinguishable from a failure), a stack trace, a suggestion missing for an obvious typo |
| named object missing | a documented non-zero code, stdout empty | exit 0 with an empty result, or the same code as a crash |
| success | exit 0, data on stdout, stderr empty or notes only | progress bars, warnings or banners on stdout |
| nothing to do | exit 0 and a statement that nothing changed | non-zero for "already done", which breaks idempotent automation |

Every non-zero code must appear in `--help` or the man page. Changing the meaning of an existing code is a breaking change.

## 2. Machine output

```bash
$TOOL list --json | jq -e . >/dev/null && echo "valid JSON"
$TOOL list --json 2>/dev/null | jq 'type'                  # one document, not JSON Lines unless documented
$TOOL list --json --color always | grep -c $'\033\[' || true   # must be 0
$TOOL list --quiet | head -3                               # one item per line, no header
```

Check that field names are stable and documented, that numbers are numbers and not strings with units, that empty results are `[]` or `{"items": []}` rather than nothing at all, and that errors in `--json` mode still go to stderr with an empty or absent stdout.

## 3. Pipes and terminals

```bash
$TOOL list | cat -v | head                                  # no ^[[ escape codes in a pipe
NO_COLOR=1 script -qec "$TOOL list" /dev/null | cat -v | head   # none with NO_COLOR on a terminal (util-linux script)
script -qec "$TOOL list" /dev/null | cat -v | head          # colour allowed here
$TOOL list --json | head -c 10 >/dev/null; echo "${PIPESTATUS[0]}"   # 0 or 141, never a traceback on stderr
TERM=dumb script -qec "$TOOL list" /dev/null | cat -v | head
```

| Behaviour | Accept | Flag |
|---|---|---|
| colour | only when stdout is a terminal, off with `NO_COLOR` (non-empty) or `TERM=dumb`, an explicit `--color always` may override | colour in pipes, ignoring `NO_COLOR` |
| pager | only on a terminal, disabled by an env var or flag | a pager that waits in scripts (AWS CLI v2 without `--no-cli-pager`) |
| closed pipe | exit 141 or 0, stderr empty | a `BrokenPipeError` traceback |
| width | wraps or truncates only on a terminal | truncated values in a pipe |

## 4. Configuration precedence

Make the sources disagree and ask the tool which one won:

```bash
mkdir -p "$XDG_CONFIG_HOME/$TOOL"
printf 'lock_timeout = 2\ncolor = never\n' > "$XDG_CONFIG_HOME/$TOOL/config"
CCOPS_LOCK_TIMEOUT=3 $TOOL config --explain --color auto --json | jq '.settings'
CCOPS_LOCK_TIMEOUT= $TOOL config --explain --json | jq '.settings'         # empty env counts as unset
printf 'lock_timout = 2\n' > "$WORK/bad"; $TOOL config --config "$WORK/bad"; echo "exit $?"
```

The order must be flag, then environment, then config file, then default, and the tool must be able to print the winning source per setting (`config --explain`, `aws configure list`, `git config --show-origin`). An unknown key in a config file must be an error with the line number, not silently ignored. Relative paths in a config file should resolve relative to the file. Flag any setting that has a flag but no environment variable or config key without a stated reason, and any secret that can only be passed as a flag (it ends up in shell history and `ps`).

## 5. Changes: dry run, idempotence, confirmation

```bash
cp inventory.json "$WORK/before.json"
$TOOL add x --dry-run; cmp inventory.json "$WORK/before.json" && echo "dry run changed nothing"
$TOOL add x; $TOOL add x --json | jq '.changed'               # second run: false
echo y | $TOOL remove x; echo "exit $?"                        # must refuse without a terminal
$TOOL remove x --yes; $TOOL remove x --yes; echo "exit $?"     # documented code for "already absent"
```

| Check | Accept | Flag |
|---|---|---|
| every command that changes state | has `--dry-run` that prints exactly what would change and writes nothing, not even lock files | no preview, or a preview that runs a different code path |
| repeating a change | exit 0 and "unchanged"; a conflicting repeat fails with the differences | duplicates, or an error for "already exists with the same values" |
| destructive commands | ask on a terminal with default No; refuse with a distinct code and a hint without one; `--yes` to confirm in scripts | prompts that read from a pipe (`yes \|` works), or no confirmation at all |
| writes | atomic (temporary file, `fsync`, rename) under a lock with a timeout | in-place writes, locks that wait forever |
| Ctrl-C | exit 130 and nothing half-written | partial files |

## 6. Help, completion, man page, tests

```bash
$TOOL --help | head -40
source <($TOOL completion bash) && complete -p $TOOL
COMP_WORDS=($TOOL ""); COMP_CWORD=1; "$(complete -p $TOOL | awk '{print $3}')"; echo "${COMPREPLY[*]}"
$TOOL man > "$WORK/t.1" && groff -man -Tascii -P-cbou "$WORK/t.1" | head -30
```

Help must show usage, commands, flags with their env var and config key, examples and the exit codes. Completion and the man page should be generated from the same model as the parser; check that a flag added in this change appears in all three. Ask for golden-file tests of help and of text and JSON output, and read the golden diff in the pull request as the user-visible change log.

## Decision table

| Finding | Severity |
|---|---|
| data and diagnostics mixed on stdout, or colour in pipes | blocker: breaks every caller that parses output |
| destructive command with no `--dry-run` or that confirms from a pipe | blocker |
| exit code meaning changed, flag removed or renamed, JSON field removed or retyped | breaking: needs a major version or a deprecation period with a warning on stderr |
| usage errors share the failure code; no `config --explain` | should fix |
| new flag without env var or config key; help without examples | nice to have |

## Report

State: the tool and version reviewed; the invocation table from step 1 with exit codes and which stream each wrote to; whether `--json` is valid and stable; the pipe and terminal results from step 3; the precedence test with the source shown for each value; the dry-run, idempotence and confirmation results; whether help, completion and man page agree; and the findings ordered by the decision table, each with the exact command that shows it. Do not approve a change that adds a destructive command without `--dry-run` and a refusal without a terminal.
