# Git & Collaboration

*A tiny object store and commit graph, then the real Git workflows teams run on.*

Git stops being mysterious the moment you see what is on disk: a content-addressed store of blobs, trees and commits, a few files that name commits, and a protocol that ships the missing objects in one packfile. Every confusing moment (a detached HEAD, a conflict that makes no sense, a clone that takes an hour, a hook that eats your changes, a regression nobody can date) is one of those mechanisms behaving exactly as designed. This phase builds each mechanism small, then checks it against Git itself: whatever your tool writes, `git fsck` has to accept.

## How this phase is built

| Lesson | Type | What you build | What you then use |
|---|---|---|---|
| [Objects](01-object-store/docs/en.md) | Build | `minigit`: blobs, trees, commits and tags with zlib and SHA-1, refs, a reflog, `log`, `checkout` and `fsck` | Git's plumbing: `hash-object`, `cat-file`, `write-tree`, `commit-tree`, `update-ref` |
| [Merging](02-merge-algorithms/docs/en.md) | Build | `minimerge`: Myers and histogram diff, merge-base in a graph, three-way merge with diff3 conflicts, rename detection | `git diff --diff-algorithm`, `merge-base --all`, `merge-file`, `rerere` |
| [Packfiles](03-packfiles-transport/docs/en.md) | Build | `minipack`: packfiles with deltas, the `.idx` index and a smart-HTTP protocol v2 client with shallow and partial clones | `GIT_TRACE_PACKET`, `--depth`, `--filter=blob:none`, `verify-pack` |
| [Merge queues](04-merge-queue/docs/en.md) | Build | `mergequeue`: branch protection as data, CODEOWNERS matching, a speculative merge queue and a branch-lifetime simulator | rulesets, the merge queue, stacked PRs, merge trains |

More lessons for this phase are being written; the plan is in [ROADMAP.md](https://github.com/nomadicmehul/CloudCaptain/blob/main/ROADMAP.md).

## Reference layer

The long-form guides, cheat sheet and interview questions for this phase live under [Git](https://cloudcaptain.io/docs/tools/git). The CI/CD phase builds on it: the [pipeline runner](https://cloudcaptain.io/learn/cicd/pipeline-runner) and [release](https://cloudcaptain.io/learn/cicd/release-versioning) lessons drive Git from code.
