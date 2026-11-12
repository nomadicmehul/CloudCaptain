---
name: dataset-versioning-review
description: Review how an ML project versions its data with DVC (or a DVC-compatible tool) and prove that any released model's training data can be restored from a fresh clone, that the remote holds every referenced object, and that garbage collection cannot delete a release
version: 1.0.0
phase: mlops
lesson: mlops/data-versioning
tags: [mlops, dvc, data-versioning, reproducibility, s3, lineage, garbage-collection, pipelines]
---

# Dataset versioning review

Use this before a model release, when onboarding a repository into an ML platform, after a "we can't reproduce last month's model" incident, or before anyone runs `dvc gc --cloud`. Work in a fresh clone, never in someone's workspace: the point is to prove what the repository and the remote alone can restore.

## 1. Inventory what is tracked and how

```bash
git clone REPO review && cd review
dvc version                                   # DVC release, cache type, remote backends installed
cat .dvc/config; ls .dvc/config.local 2>/dev/null && echo "local overrides exist (not in Git)"
git ls-files '*.dvc' dvc.yaml dvc.lock params.yaml
dvc data status --granular                    # what DVC thinks is tracked and its state
git ls-files -z | xargs -0 du -k 2>/dev/null | sort -rn | head -10   # the largest files Git itself stores, in KiB
```

| Finding | Meaning | Action |
|---|---|---|
| training data path not covered by any `.dvc` file or `dvc.yaml` out | the model's input is unversioned | track it: `dvc add PATH` or make it a stage output |
| large data files committed to Git | clones are slow and history is bloated | move to DVC; rewriting history is a separate, coordinated decision |
| `.dvc` files without `hash: md5` | DVC 2 format (`md5-dos2unix`, old cache layout) | plan `dvc cache migrate` and re-push |
| credentials in `.dvc/config` | secrets in Git history | move them to `config.local` or the environment and rotate them |
| no default remote | nobody can `dvc pull` | `dvc remote add -d` with a bucket that has versioning and lifecycle rules reviewed |

## 2. Prove the current commit restores from the remote alone

```bash
dvc pull 2>&1 | tail -5                       # must finish without "missing" errors
dvc status --json                             # must be {} right after a pull
dvc status --cloud --json                     # objects referenced here but absent from the remote
```

`dvc status --cloud` reporting `deleted` or `missing` means someone ran `dvc add` or `dvc repro` and committed the pointers without `dvc push`. The fix is `dvc push` from the machine that has the cache; if nobody has it, the version is lost, and the release notes must say so.

## 3. Prove each released model's data can be restored

For every release tag or model registry entry, find the commit that produced it and restore it:

```bash
git tag --list 'model-*'                      # or read the commit from the model registry's lineage tags
for rev in $(git tag --list 'model-*'); do
  git checkout -q "$rev" && dvc pull -q && dvc status -q && echo "$rev OK" || echo "$rev BROKEN"
done
git checkout -q main
```

| Result | Verdict |
|---|---|
| every release `OK` | releases are reproducible from Git plus the remote |
| `BROKEN` with missing objects | objects were never pushed or were garbage-collected; check bucket lifecycle rules and past `dvc gc --cloud` runs |
| `BROKEN` with a modified status | the commit's pointers do not match its lock file, a stage was edited without `dvc repro`; reproduce and re-release |
| model registry entry has no Git commit | lineage is missing; add the commit and the data hash to the registry's tags |

## 4. Check the pipeline lock against the code

```bash
dvc dag
dvc status --json                             # stages with changed deps, outs or command
dvc repro --dry                               # what would run, without running it
git log -1 --format=%H -- dvc.lock; git log -1 --format=%H -- dvc.yaml params.yaml src/
```

`dvc.lock` older than the last change to `dvc.yaml`, `params.yaml` or a stage's code means the committed outputs were not produced by the committed code. Every dependency of the training stage (data, params, code, base model) must be a `deps` entry, or a change to it will not trigger a rerun.

## 5. Make garbage collection safe

```bash
dvc gc --all-commits --cloud --dry            # what an all-commits gc would delete, here and in the bucket
git branch -r; git tag                        # the refs that keep data alive
```

| Situation | Risk | Rule |
|---|---|---|
| someone runs `dvc gc --workspace --cloud` | every version except the current one is deleted from the bucket | only `--all-commits` (or `--all-tags` plus `--all-branches`) with `--cloud`, after a `--dry` run, by one owner |
| a release exists only as a deleted branch or an untagged commit | `--all-commits` treats it as garbage | tag every released commit before any gc |
| bucket lifecycle rule expires objects under the DVC prefix | the bucket deletes what DVC still references | no expiry on `files/md5/`; use noncurrent-version expiry only |
| several projects share one bucket prefix | one project's gc deletes another's objects | one prefix per repository |

## 6. Report

State: the DVC version and cache type; what is tracked and what training input is not; whether the current commit restores from a fresh clone (`dvc status` and `dvc status --cloud` results); the restore result for each release tag; whether `dvc.lock` matches the code; the gc dry-run summary (objects, size) and whether any release would be deleted; and the fixes, each with the command that verifies it. Never report "data is versioned" without the fresh-clone restore result.
