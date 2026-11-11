---
name: regression-hunt
description: Find the commit that introduced a regression with git bisect run (reliable good and bad ends, a test script with 0/125/1 exit codes and a timeout, skips handled), confirm it by reverting, explain it with blame (ignoring formatting commits) and pickaxe, recover lost branches from the reflog, and report every step
version: 1.0.0
phase: git
lesson: git/bisect-blame
tags: [git, bisect, blame, pickaxe, reflog, regression, debugging, incident]
---

# Regression hunt

Use this when behaviour changed between two versions (a test that used to pass, a latency or error-rate jump, a config that is suddenly ignored) and the question is "which commit did it". Bisect needs one assumption: every commit that contains the bad change shows the bad behaviour, and every commit that does not, does not. If the behaviour is flaky, make the test deterministic first (step 2), or the answer is noise.

## 1. Pin down a good and a bad commit

```bash
git fetch --tags origin
git log --oneline -1 v1.4.0 && git log --oneline -1 origin/main     # names you will pass to bisect
git rev-list --count v1.4.0..origin/main                            # candidates, roughly
git merge-base --is-ancestor v1.4.0 origin/main && echo "good is an ancestor of bad"
```

Reproduce the bad behaviour on the bad commit and the good behaviour on the good commit with the exact test you will automate, in a clean work tree (`git status` empty, or `git stash -u`). If the good commit is not an ancestor of the bad one (a release branch), expect Git to test the merge base first.

## 2. Write the test script

Keep it outside the repository so checkouts do not change it, and make it decide only the question you are asking:

```bash
cat > /tmp/regression.sh <<'EOF'
#!/bin/sh
timeout 300 make build >/dev/null 2>&1 || exit 125       # cannot build: skip, never "bad"
timeout 120 ./bin/payments selftest --retries > /tmp/out.txt 2>&1
status=$?
[ "$status" -eq 124 ] && exit 125                         # hung: cannot tell
grep -q 'max_retries=5' /tmp/out.txt                      # 0 good, 1 bad
EOF
chmod +x /tmp/regression.sh
git checkout -q v1.4.0 && /tmp/regression.sh; echo "good end: $?"          # must print 0
git checkout -q origin/main && /tmp/regression.sh; echo "bad end: $?"     # must print 1
git checkout -q -
```

| Exit code | Meaning for `git bisect run` | Use it when |
|---|---|---|
| 0 | good | the behaviour is the old one |
| 1 to 127, except 125 | bad | the behaviour is the new one |
| 125 | skip | the commit cannot be tested (build broken, dependency missing, timeout) |
| 128 or more, or a signal | abort the whole run | the test harness itself failed |

A missing interpreter or a script without the execute bit returns 126 or 127; Git double-checks that on a good commit, but do not rely on it.

## 3. Run the bisection

```bash
git bisect start origin/main v1.4.0            # add more good commits if you know them: fewer candidates
git bisect run /tmp/regression.sh > /tmp/bisect-run.txt 2>&1
tail -20 /tmp/bisect-run.txt
git bisect log > /tmp/bisect.log
git bisect reset
```

For a history with long-lived merged branches where only the main line matters (for example to find which merge brought the change), use `git bisect start --first-parent`. To find when something got fixed rather than broken, use `--term-old=broken --term-new=fixed`.

## 4. Read the result

| Output | Meaning | Next |
|---|---|---|
| `<sha> is the first bad commit` (Git 2.55+: `first 'bad' commit`) | found | step 5 |
| `There are only 'skip'ped commits left to test` with a list | the change is in one of the listed commits, Git cannot tell which | test the listed commits by hand with a patch that fixes the build (`git cherry-pick -n <fix>`), or read them with step 6 |
| `bisect run failed: exit code ... is < 0 or >= 128` | the script crashed | fix the script, then `git bisect replay /tmp/bisect.log` and `git bisect run` again |
| `The merge base ... is bad` | the "good" commit is on a branch that fixed the problem | swap terms or pick a good commit on the bad commit's own line |
| `Bisecting: a merge base must be tested` | a good commit is not an ancestor of the bad one, so Git checked out their merge base | test and mark it like any other commit; if the run stopped with `bisect run success`, start `git bisect run` again |

A wrong verdict earlier (a flaky pass) gives a wrong answer with full confidence. Edit `/tmp/bisect.log`, remove the bad line and everything after it, and `git bisect replay /tmp/bisect.log`.

## 5. Confirm it

```bash
FIRST=<sha from step 4>
git show --stat "$FIRST"
git checkout -q "$FIRST^" && /tmp/regression.sh; echo "parent: $?"       # 0
git checkout -q "$FIRST" && /tmp/regression.sh; echo "first bad: $?"     # 1
git checkout -q origin/main && git revert --no-commit "$FIRST" && /tmp/regression.sh; echo "reverted on main: $?"; git revert --abort
```

If the first bad commit is a merge, the change is the interaction of the two sides: `git log --oneline "$FIRST^1..$FIRST^2"` lists what the merge brought in, and bisecting that range with `--first-parent` off narrows it further.

## 6. Explain it

```bash
git blame -w -M --ignore-revs-file .git-blame-ignore-revs -L '/def authorize/,+30' src/retry.py
git log -S'MAX_RETRIES = 5' --oneline                       # commits that changed how often the string occurs
git log -G'retr(y|ies)' --oneline -- src/                   # commits whose added/removed lines match
git log -L '/def authorize/,+30:src/retry.py' --oneline     # the function's history, commit by commit
git show "$FIRST" -- src/retry.py
```

If the file was reformatted, add the formatter commit's full object name to `.git-blame-ignore-revs` (and `git config blame.ignoreRevsFile .git-blame-ignore-revs`) instead of reading past it by hand. Lines marked `*` (`blame.markUnblamableLines`) have no earlier source; lines marked `?` were handed past an ignored commit.

## 7. Recover lost work if needed

```bash
git reflog --date=iso | grep -i retry                       # checkouts and commits on deleted branches
git fsck --no-reflogs | awk '/dangling commit/ {print $3}' | xargs -r git log --no-walk --oneline
git branch rescued <sha>                                     # then cherry-pick or merge from it
```

Reflogs and dangling objects exist only in the repository where the work was done, and only until `git gc` expires them (unreachable reflog entries after 30 days, pruning after two more weeks by default).

## 8. Report

State: the good and bad commits and the number of candidates; the test script (inline) and how its exit codes were chosen; the number of tests run and every skipped commit with the reason; the result (first bad commit with subject and author, or the list of possible commits); the confirmation from step 5 (parent good, commit bad, revert on the bad end good); the explanation from step 6 in two or three sentences, naming the lines and why they changed, quoting commit messages rather than guessing intent; anything recovered in step 7; and the proposed fix (revert, patch or config change) with who should review it.
