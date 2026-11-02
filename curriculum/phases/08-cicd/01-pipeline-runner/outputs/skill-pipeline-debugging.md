---
name: pipeline-debugging
description: Find out why a GitHub Actions run failed, skipped a job, never started, ran with empty outputs, missed its cache or leaked a secret, by reading the run as a graph of jobs in clean rooms, then reproduce it locally
version: 1.0.0
phase: cicd
lesson: cicd/pipeline-runner
tags: [github-actions, ci, debugging, act, actionlint, cache, matrix, secrets]
---

# Pipeline debugging

Use this when someone says "CI is broken", "deploy didn't run", "it works locally", "the cache never hits" or "a token showed up in the log". Work from the outside in. Did a run start? Which node of the graph failed first? Why did each skipped job skip? What crossed between jobs, and what was expected to cross without a door?

## 1. Did a run start at all?

```bash
gh run list --workflow ci.yml --branch BRANCH --limit 10
gh run list --commit SHA
gh workflow view ci.yml
git show --stat --format='%H %s' SHA
```

| Symptom | Likely cause | Check | Fix |
|---|---|---|---|
| no run for a push | `branches` or `paths` filter, or the event is not in `on:` | the filter patterns against the branch and the changed files; `*` stops at `/`, `**` does not | widen the pattern; remember the last matching pattern wins and `!` negates |
| no run for a tag push | only `branches` filters are set, so tags never trigger | `on.push` has no `tags` key | add `tags: ["v*"]` |
| no run, commit says `[skip ci]` | the skip markers in the head commit message | `git log -1 --format=%B` | push a commit without the marker |
| no run for a fork pull request | first-time contributors need approval, or the workflow is `pull_request_target` on another branch | the PR's Checks tab | approve the run; read the next lesson before touching `pull_request_target` |
| the workflow file is listed with an error | invalid YAML or an unknown key | `actionlint .github/workflows/ci.yml` | fix what it reports; quote versions and anything that starts with `!`, `*`, `&` or `@` |

## 2. Read the run as a graph

```bash
gh run view RUN_ID
gh run view RUN_ID --json jobs --jq '.jobs[] | [.name, .status, .conclusion] | @tsv'
gh run view RUN_ID --log-failed
actionlint .github/workflows/ci.yml
```

Draw the waves: jobs with no `needs` first, then each job one level after the deepest job it needs. The first job to fail in the earliest wave is the root cause. Everything after it is usually a skip, not a second failure. Matrix legs named like `test (3.12, linux)` belong to one node, and that node fails if any leg without `continue-on-error` fails.

## 3. Why did this job skip?

Every `if:` without `success()`, `failure()`, `always()` or `cancelled()` is evaluated as `success() && (your condition)`, and the job-level `success()` checks every ancestor, not only the direct needs.

| What you see | Cause | Fix |
|---|---|---|
| job skipped after an upstream failure | the implicit `success()` | intended; for cleanup or notification use `always()` or `failure()` |
| job skipped although its direct need succeeded | an ancestor further up was skipped | `if: ${{ !cancelled() && !failure() }}` |
| job runs on every branch | text outside `${{ }}` in `if:` makes it a non-empty string, which is always true | put the whole condition in one `${{ }}` or drop the braces |
| `if: !cancelled()` breaks the file | `!` starts a YAML tag | `if: ${{ !cancelled() }}` |
| condition on `matrix.*` or `env.*` at job level never true | those contexts do not exist when the job-level `if` runs | move the condition to a step, or into the matrix with `exclude` |
| string comparison surprises | `==` ignores case and coerces mixed types to numbers (`'' == 0` is true) | compare like with like; use `startsWith()` for refs |

## 4. Matrix legs

- Count the legs: product of the lists, minus `exclude` (partial matches remove), plus `include`. An include extends every combination it does not contradict. An include that fits none becomes a new leg, and later includes never extend that leg.
- `cancelled` legs next to one `failure` leg mean fail-fast. Find the failing leg; the others did not fail. Set `fail-fast: false` while you investigate, or give a leg `continue-on-error: ${{ matrix.experimental }}`.
- Unquoted `3.10` is the number 3.1. Quote every version in a matrix.

## 5. Empty outputs

```bash
gh run view RUN_ID --log --job JOB_ID | grep -n -i -E 'GITHUB_OUTPUT|set-output|Skip output'
```

| Symptom | Cause | Fix |
|---|---|---|
| `needs.build.outputs.x` is empty | the job does not list `x` under `outputs:`, or the step has no `id` | add `outputs: x: ${{ steps.ID.outputs.x }}` and the `id` |
| empty in a job that does not `need` the producer | `needs` context only contains direct needs | add the producer to `needs` |
| log says `Skip output 'x' since it may contain secret.` | the value contains a masked string | pass a reference (a name or a digest), never the secret |
| a multi-line value is cut to one line | written as `x=value` | use `x<<EOF`, the lines, `EOF` |
| matrix job outputs flip between runs | every leg writes the same name and the last leg to finish wins | one name per leg, or aggregate through artifacts |

## 6. Cache misses and stale hits

```bash
gh cache list --limit 50
gh cache list --key deps-Linux-
gh run view RUN_ID --log --job JOB_ID | grep -n -E 'Cache (restored|not found|saved)|Unable to reserve'
```

| Symptom | Cause | Fix |
|---|---|---|
| exact hit on a key with no hash in it (`deps-Linux-`) | `hashFiles()` ran before checkout and matched nothing, so it returned an empty string | check out first; fail the job if the hash is empty |
| always a miss on a feature branch | caches are scoped to the branch; a branch can read its own, its PR base and the default branch, never a sibling | let the default branch populate the cache |
| miss although the key is identical | the `path` input changed, which changes the cache version | keep `path` stable, or accept one miss |
| new dependencies never saved | keys are immutable and the key did not change | include the lock file hash in the key |
| restore from `restore-keys`, then a slow install | expected partial hit; `cache-hit` is `false` | keep the install step on `cache-hit != 'true'` |

## 7. Files that crossed without a door

A job that reads a file another job wrote only works if the file travelled as an artifact (same run) or a cache (later runs), or if the jobs share a machine. On GitHub-hosted runners every job is a new VM. On self-hosted runners and in `act -P ...=-self-hosted` the jobs share the host, so a pipeline can pass there and fail on hosted runners.

```bash
gh run download RUN_ID --name bundle --dir /tmp/bundle
gh api repos/OWNER/REPO/actions/runs/RUN_ID/artifacts --jq '.artifacts[] | [.name, .size_in_bytes] | @tsv'
```

Fix it by adding `needs`, uploading in the producer and downloading in the consumer. Register self-hosted runners as `--ephemeral`, and never attach them to a public repository.

## 8. Secrets

- Empty secret in a pull request from a fork: secrets (other than `GITHUB_TOKEN`) are not passed to those runs. This is by design; do not work around it with `pull_request_target`.
- A secret you can read in the log: masking replaces exact strings only. Base64, URL-encoded, reversed or split values are printed in full. Treat the secret as leaked. Revoke and rotate it first, then delete the run logs (`gh run delete RUN_ID`), then fix the step that transformed and printed it.
- Generated credentials should be masked with `echo "::add-mask::$VALUE"` before anything prints them.

## 9. Reproduce locally

```bash
actionlint .github/workflows/*.yml
act -l
act push -j JOB -P ubuntu-latest=-self-hosted -s NAME --no-cache-server
act push -j JOB -e event.json
python3 minici.py run .github/workflows/ci.yml --secret NAME --changed path/that/changed --json
```

Known differences to rule out before blaming the workflow: act has no job-level `continue-on-error`. act's `hashFiles()` hashes the concatenated contents and so differs from the runner's. In self-hosted mode act shares your machine between jobs, and remote actions are cloned from GitHub. minici runs only `run:` steps and four built-in actions.

## 10. Report

Post one message with: the run URL and the commit; whether a run started and which filter decided it; the waves with each job's conclusion; the first failing node, with its failing step, the exact error line and the cause; every skipped job with the condition that skipped it, and whether that was intended; any output, artifact or cache that was empty or stale, and why; whether a secret was exposed, and if so the rotation done; the fix as a diff; and how it was verified (actionlint clean, a local reproduction, a green re-run).
