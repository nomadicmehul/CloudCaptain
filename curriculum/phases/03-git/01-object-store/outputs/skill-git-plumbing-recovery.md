---
name: git-plumbing-recovery
description: Recover a Git repository that looks broken (lost commits, a dropped stash, empty or corrupt objects after a crash, stale lock files, damaged refs or HEAD) with the reflog, fsck and plumbing commands, proving every restored object by its hash and never deleting what you have not backed up
version: 1.0.0
phase: git
lesson: git/object-store
tags: [git, recovery, reflog, fsck, objects, refs, corruption, plumbing]
---

# Git plumbing recovery

Use this when someone says "my commits are gone", "Git says the object is corrupt", "fatal: Unable to create ... .lock", "HEAD is broken" or proposes deleting `.git` and re-cloning. Most of these are recoverable because objects are immutable files named by their hash and every ref move is logged. Work on a copy first and change one thing at a time.

## 1. Stop and back up

```bash
cd <repository>
git rev-parse --git-dir                       # where the store is (.git, or a worktree's gitdir)
pgrep -fl '[g]it ' || true                    # nothing else may be writing
tar czf ../git-backup-$(date +%Y%m%d%H%M%S).tgz "$(git rev-parse --git-dir)"
git config gc.auto 0                          # no automatic gc may prune what you are about to recover
```

If `git rev-parse` itself fails, back up the `.git` directory with `tar` directly. Do not run `git gc`, `git prune`, `git reflog expire` or `git clean` until the report is written.

## 2. Classify the symptom

| Symptom | Likely cause | Go to |
|---|---|---|
| commits missing after `reset --hard`, `rebase`, `commit --amend`, `checkout` of another branch | a ref moved; objects untouched | 3 |
| a deleted branch (`git branch -D`), a dropped or cleared stash | the ref is gone; its reflog may be gone too | 4 |
| `object file ... is empty`, `loose object ... is corrupt`, `inflate: data stream error`, `hash mismatch` | an object file damaged by a crash, a full disk or bad storage | 5 |
| `Unable to create '.../index.lock'` or `'.../refs/heads/X.lock': File exists` | a crashed or still-running git process | 6 |
| `fatal: bad object HEAD`, `HEAD` or a ref file empty or holding garbage, `refs/heads/X` is a 0-byte file | a ref write interrupted | 7 |
| `fatal: not a git repository` although `.git` exists | `HEAD` missing or not a valid ref, or `objects/` or `refs/` missing | 7 |

## 3. A ref moved: use the reflog

```bash
git reflog                                    # HEAD's history: HEAD@{N}: action: subject
git reflog show <branch>                      # that branch's own log
git log --oneline -g --date=iso <branch>      # the same with dates
git show --stat 'HEAD@{2}'                    # inspect a candidate before touching anything
git branch rescue/<name> 'HEAD@{2}'           # give it a name: safe, changes nothing else
```

Prefer creating a branch over `reset --hard`: a new ref cannot destroy work, and you can compare it with the current branch (`git log --oneline <branch>..rescue/<name>`). For a rebase, `ORIG_HEAD` holds the pre-rebase tip until the next rebase, merge or reset: `git branch rescue/pre-rebase ORIG_HEAD`.

## 4. No ref left: find dangling commits

```bash
git fsck --no-reflogs --unreachable | grep commit            # unreachable commits, newest work among them
git fsck --lost-found                                        # copies dangling commits and blobs to .git/lost-found/
for c in $(git fsck --no-reflogs --unreachable 2>/dev/null | awk '/commit/ {print $3}'); do git log -1 --format='%h %ci %an %s' "$c"; done | sort -k2
git log --oneline --graph $(git fsck --no-reflogs 2>/dev/null | awk '/dangling commit/ {print $3}')
git branch rescue/found <commit-id>
```

A dropped stash is a merge commit whose subject starts with `WIP on` or `On <branch>:`; restore it with `git stash apply <commit-id>`. A lost blob (a file staged but never committed) appears as `dangling blob`: `git cat-file -p <blob-id> > recovered.txt`. If nothing appears, the objects were already pruned by an earlier gc; check other clones, CI caches and the hosting provider's own restore features.

## 5. A damaged object: restore it by its hash

```bash
git fsck --full --no-dangling 2>&1 | tee ../fsck.txt          # every broken or missing ID
OID=<broken-id>
ls -l "$(git rev-parse --git-dir)/objects/${OID:0:2}/${OID:2}"  # 0 bytes means an interrupted write
git log --all --format=%H --raw --no-abbrev 2>/dev/null | grep -n "$OID" | head    # which commit and path used it (if logs still work)
```

Get the object from any other copy: a teammate's clone, the remote, a CI workspace, a backup. The ID proves the bytes, so the source does not need to be trusted:

```bash
git -C <other-clone> cat-file -t "$OID"                       # must exist there
git -C <other-clone> cat-file "$(git -C <other-clone> cat-file -t "$OID")" "$OID" > /tmp/obj
mv "$(git rev-parse --git-dir)/objects/${OID:0:2}/${OID:2}" ../damaged-"$OID"     # the empty or corrupt file must go first
git hash-object -w -t "$(git -C <other-clone> cat-file -t "$OID")" /tmp/obj      # must print exactly $OID
git fsck --full --no-dangling                                  # repeat until clean
```

When many objects are damaged and the remote has them, fetch into the same store instead: `git fetch origin` restores everything reachable from the remote's refs once the damaged files are moved aside. If a blob exists nowhere else but you have the file in a working copy, `git hash-object -w <file>` restores it if and only if the printed ID matches; if it does not match, that version of the file is lost and the commit that used it can be rebuilt only by rewriting history.

| fsck says | Meaning | Action |
|---|---|---|
| `missing blob/tree/commit ID` | referenced but absent | restore from another copy (above) |
| `hash mismatch` / `sha1 mismatch` | the file's content is not the object named | move it aside, restore from another copy |
| `object file ... is empty` / `is corrupt` | interrupted write or storage damage | move it aside, restore; check the disk (`dmesg`, SMART) |
| `dangling ...` | unreachable, not damage | ignore, or rescue (step 4) |
| `treeNotSorted`, `zeroPaddedFilemode`, `badTimezone`, `hasDotgit` | an object written by a buggy tool, or hostile | not repairable in place (the ID would change); report it; `fsck.<id>` and `receive.fsck.<id>` can relax checks for old history only after review |

## 6. A stale lock

```bash
pgrep -fl '[g]it ' || echo "no git running"
ls -l "$(git rev-parse --git-dir)"/index.lock "$(git rev-parse --git-dir)"/refs/heads/*.lock 2>/dev/null
```

Remove a lock only when no git process (and no IDE or file-sync tool running git) is alive. After removing `index.lock`, run `git status`; if the index itself is damaged (`index file corrupt`), move `.git/index` aside and run `git reset` to rebuild it from `HEAD` (the working tree is untouched).

## 7. Damaged refs or HEAD

```bash
cat "$(git rev-parse --git-dir)/HEAD"                          # should be "ref: refs/heads/<branch>" or a full ID
git for-each-ref --format='%(refname) %(objectname)' 2>&1 | head
grep -n '<branch>' "$(git rev-parse --git-dir)/packed-refs"    # an old value may still be packed
tail -3 "$(git rev-parse --git-dir)/logs/refs/heads/<branch>"  # the last good value is the second ID on the last line
git update-ref refs/heads/<branch> <last-good-id>              # rewrite the ref file atomically
git symbolic-ref HEAD refs/heads/<branch>                      # repair HEAD
```

An empty loose ref file hides the packed value; moving the empty file aside makes the packed line visible again.

## 8. Verify

```bash
git fsck --full --strict
git status
git log --oneline --graph --all | head -30
git config --unset gc.auto
```

## 9. Report

State: the symptom with the exact error text; the backup path; the classification from step 2; every command that changed state, in order; each recovered commit or object ID with how it was proven (reflog entry, dangling commit with its subject and date, or `hash-object` printing the expected ID); anything that could not be recovered and which commits or paths it affects; the likely root cause (crash, full disk, storage error, a tool that writes bad objects, a mistaken command) and the prevention (push work in progress to a branch, `core.fsync` settings on unreliable storage, keep `gc.reflogExpire` at its default or longer). Never list "deleted .git and re-cloned" as a recovery unless every local ref was confirmed pushed first.
