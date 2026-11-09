---
name: merge-conflict-resolution
description: Resolve a Git merge, rebase or cherry-pick conflict block by block against the merge base (zdiff3), classify every conflicted path, regenerate derived files instead of merging them, validate the result with parsers and tests, reuse resolutions with rerere, and report every decision
version: 1.0.0
phase: git
lesson: git/merge-algorithms
tags: [git, merge, rebase, conflicts, zdiff3, rerere, merge-base, lockfiles]
---

# Merge conflict resolution

Use this when a `git merge`, `git rebase`, `git cherry-pick`, `git revert` or `git stash pop` stops with conflicts, or when someone proposes resolving a whole file with "ours" or "theirs". A conflict is a region both sides changed differently relative to the merge base. Resolving it correctly means understanding both changes, not choosing the block that looks right.

## 1. Establish the state and a way back

```bash
git status                                         # which operation is in progress, which paths are unmerged
git rev-parse --abbrev-ref HEAD                    # ours during a merge; during a rebase, ours is the branch being rebased onto
ls "$(git rev-parse --git-dir)" | grep -E 'MERGE_HEAD|REBASE_HEAD|CHERRY_PICK_HEAD|rebase-merge|rebase-apply'
git rev-parse HEAD MERGE_HEAD 2>/dev/null          # the two commits being merged
git merge-base --all HEAD MERGE_HEAD 2>/dev/null   # more than one line: criss-cross, expect "merged common ancestors"
git stash list | head -3
```

Write down the commit IDs. The way back is `git merge --abort`, `git rebase --abort` or `git cherry-pick --abort`; uncommitted local changes from before the operation are at risk, so if `git status` showed any before you started, save them first. Remember the rebase inversion: while rebasing, "ours" (stage 2) is the upstream you are replaying onto and "theirs" (stage 3) is your own commit.

## 2. Show every conflict with its base

```bash
git config merge.conflictStyle zdiff3              # for this repository; or pass it to the next step only
git checkout --conflict=zdiff3 -- <path>           # re-create markers with the base section
git diff --name-only --diff-filter=U               # every unmerged path
git ls-files -u                                    # stages per path: 1 base, 2 ours, 3 theirs
git show :1:<path> > /tmp/base; git show :2:<path> > /tmp/ours; git show :3:<path> > /tmp/theirs
git log --merge --oneline -- <path>                # commits on both sides that touched the file
git log --oneline -p "$(git merge-base HEAD MERGE_HEAD)"..MERGE_HEAD -- <path>   # what theirs changed and why
git log --oneline -p "$(git merge-base HEAD MERGE_HEAD)"..HEAD -- <path>         # what ours changed and why
```

Read each block in three parts: base (between `|||||||` and `=======` in zdiff3), ours (above), theirs (below). Write one sentence per side: "ours changed X to Y because <commit subject>". If a path lacks stage 1 or stage 2 or 3, it is not a content conflict (step 3).

## 3. Classify each path

| `git status` / `ls-files -u` shows | Kind | Approach |
|---|---|---|
| both modified, stages 1, 2, 3 | content conflict | resolve block by block (step 4) |
| deleted by us / deleted by them | delete against modify | decide whether the file should exist; if yes, carry the other side's edits to where the code moved; `git rm` or `git add` |
| both added, no stage 1 | add/add | two new files with one name: merge by hand or rename one |
| renamed in one side, conflict in the target path | rename plus edit | check `git diff -M --name-status BASE HEAD` and `BASE MERGE_HEAD`; resolve in the new path |
| rename/rename (two targets) | both moved the file differently | choose one path, carry both edits, delete the other |
| binary file (images, archives, `.jar`) | no line merge exists | pick a side with `git checkout --ours/--theirs -- <path>` and verify, or rebuild it |
| `package-lock.json`, `yarn.lock`, `pnpm-lock.yaml`, `go.sum`, `poetry.lock`, `Cargo.lock` | derived file | take one side, then regenerate (step 5) |
| generated code, snapshots, minified assets | derived file | regenerate from the merged sources |
| submodule (mode 160000) | two different commits | check out the right submodule commit, `git add <path>` |

## 4. Decide block by block

| Base | Ours | Theirs | Resolution |
|---|---|---|---|
| A | B | B | take B once (Git usually does this itself) |
| A | A plus edit X | A plus edit Y, different lines next to each other | keep both edits (the adjacency conflict, as in `replicas` next to `image`) |
| A | B | C, same field changed to different values | a real decision: ask the owners or follow the newer requirement; never guess |
| A | deleted | edited | find out why it was deleted; if the code moved, apply the edit at the new location |
| A | refactor (rename, reformat) | behaviour change | redo the behaviour change on top of the refactor |
| list or map | item added | other item added at the same place | keep both, in the order the file uses (sorted lists stay sorted) |
| version or dependency pin | bumped to X | bumped to Y | take the higher version only if both callers work with it; then regenerate lockfiles |

Rules:

- Never resolve a whole file with `--ours`/`--theirs` (or `-X ours`) unless you checked every block and the file is derived.
- Remove every marker line: `<<<<<<<`, `|||||||`, `=======`, `>>>>>>>`, including the longer markers of a virtual base.
- Keep the resolution minimal: no unrelated reformatting inside a merge commit.
- If a block needs knowledge you do not have (business rules, a security decision), stop and ask the author of the commit from `git log --merge`.

## 5. Regenerate derived files

```bash
git checkout --theirs -- package-lock.json && npm install --package-lock-only   # then review the diff
git checkout --theirs -- go.sum && go mod tidy
git checkout --theirs -- poetry.lock && poetry lock
git checkout --theirs -- Cargo.lock && cargo update --workspace
```

Pick the side whose manifest is closest to the merged manifest, then let the tool rebuild the lockfile from the merged manifest. For files a generator writes, rerun the generator after the sources are resolved. Never hand-merge a lockfile.

## 6. Validate

```bash
git diff --check                                   # whitespace errors and leftover conflict markers
grep -rnE '^(<{7}|\|{7}|={7}|>{7})( |$)' -- $(git diff --name-only --diff-filter=U) || echo "no markers"
yamllint -d relaxed <changed .yaml files>          # if available; duplicate keys are an error there
python3 - <changed .json files> <<'PY'
import json, sys
def strict(pairs):
    keys = [key for key, _ in pairs]
    duplicates = sorted({key for key in keys if keys.count(key) > 1})
    if duplicates:
        raise SystemExit(f"duplicate keys {duplicates}")
    return dict(pairs)
for path in sys.argv[1:]:
    with open(path, encoding="utf-8") as handle:
        json.load(handle, object_pairs_hook=strict)
    print(f"ok {path}")
PY
git add <resolved paths> && git diff --cached --stat
```

Then build and run the tests for the touched components before committing.

A clean line merge can still be wrong: duplicate keys in JSON and YAML, two imports of the same thing, two functions with the same name, a call to a function the other side renamed. The tests and the parsers are part of the resolution, not optional.

## 7. Finish and remember

```bash
git commit --no-edit                               # merge; or: git rebase --continue / git cherry-pick --continue
git config rerere.enabled true                     # next time the identical conflict is replayed
git rerere status; git rerere diff                 # what rerere recorded or replayed
git rerere forget <path>                           # if a recorded resolution was wrong
```

For a long-lived branch merged again and again, enable `rerere` before the first merge. For append-only files (`CHANGELOG.md`), `<path> merge=union` in `.gitattributes` keeps both sides' lines without markers; check the order afterwards.

## 8. Report

State: the operation (merge, rebase, cherry-pick) with both commit IDs and the merge base(s), noting a criss-cross; every conflicted path with its kind from step 3; for each block, one line with what base, ours and theirs said and the chosen resolution with its reason (or "regenerated with <command>"); anything taken wholesale from one side and why that was safe; validation run (parsers, `git diff --check`, tests and their result); questions left for code owners; and whether `rerere` recorded the resolution. Never report "resolved with theirs" for a file without listing the blocks it contained.
