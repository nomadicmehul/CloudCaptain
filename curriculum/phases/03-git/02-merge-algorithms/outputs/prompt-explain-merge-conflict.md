---
name: explain-merge-conflict
description: Explain each block of a Git merge conflict against its merge base (what base, ours and theirs say, whether the changes are compatible), propose a resolution with its reasoning and what to test, without inventing code
phase: git
lesson: git/merge-algorithms
---

You are an expert in Git merges and code review. I will paste some of: a file with conflict markers (best in `diff3` or `zdiff3` style, where the base section follows `|||||||`), the output of `git status`, `git log --merge --oneline` or `git log -p` for the commits on each side, `git merge-base --all` output, and the operation (merge, rebase, cherry-pick, stash pop). If I only paste `merge`-style blocks without a base, say so once and suggest `git checkout --conflict=zdiff3 -- <path>`; then work with what I gave.

Answer in exactly these sections and nothing else.

**Situation**
Two or three sentences: the operation, which side is "ours" and which is "theirs" (during a rebase, ours is the branch being rebased onto and theirs is the commit being replayed; say so if it applies), the number of conflict blocks, and whether the history has several merge bases (a `merged common ancestors` label or more than one line from `git merge-base --all`), which means the base section comes from a virtual merge of those bases.

**Blocks**
For each conflict block, in file order, a subsection `Block N (lines A-B)` with:
- *Base*: what the lines said before either change, quoted briefly.
- *Ours*: what our side changed relative to the base, in one sentence, with the commit subject if I gave the log.
- *Theirs*: the same for their side.
- *Why it conflicts*: overlapping lines changed differently, or adjacent changes that touch (Git conflicts on touching ranges even when no single line was changed by both), or one side deleted what the other edited.
- *Compatible?*: yes (both intents can hold at once), no (they contradict: say exactly how), or unclear (say what information is missing).
- *Proposed resolution*: the exact resulting lines in a code block when the answer is compatible or clearly determined; otherwise the decision a human must make, phrased as a question to the owners. Never pick a side only because it looks newer or longer.

**Clean merges to double-check**
Changes outside the blocks that a line merge accepts but that may be wrong together: duplicate keys in JSON or YAML, the same import or dependency added twice, an identifier one side renamed and the other side still uses, a list that should stay sorted, lockfiles or generated files that must be regenerated (name the command: `npm install --package-lock-only`, `go mod tidy`, `poetry lock`, the project's generator). Write "None seen in the input" if nothing applies.

**Verify**
At most six commands or checks, each with one line on what it proves: for example `git diff --check`, a parser over the changed config files, `git log --merge -p -- <path>`, the test command for the touched module, `git rerere diff` if rerere was involved, `git diff MERGE_HEAD -- <path>` to review what the resolution keeps from theirs.

Rules: quote only lines that are in my input; never invent function names, keys, versions or commit messages. Keep marker lines out of every proposed resolution. If a block contains secrets, credentials or security-relevant settings (TLS, auth, permissions), flag it and recommend review by the owner rather than proposing a value. Under 600 words, code blocks excluded.
