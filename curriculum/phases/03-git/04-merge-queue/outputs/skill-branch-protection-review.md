---
name: branch-protection-review
description: Audit how changes reach a protected branch: rulesets and classic protection as data, CODEOWNERS correctness, required checks that also run in the merge queue, queue load and branch lifetime, with a fix for every finding and a fixed report
version: 1.0.0
phase: git
lesson: git/merge-queue
tags: [git, github, rulesets, branch-protection, codeowners, merge-queue, trunk-based-development]
---

# Branch protection review

Use this when someone asks "is main actually protected", "why did this merge without the right review", "why is the merge queue slow" or "should we turn on the merge queue". The question behind all of them: what was tested before a commit landed on the protected branch, and who had to approve it. Answer it from the configuration and the history, never from what people remember setting.

## 1. Collect the facts

```bash
R=OWNER/REPO; B=main
gh api repos/$R --jq '{default_branch, allow_merge_commit, allow_squash_merge, allow_rebase_merge, delete_branch_on_merge}'
gh api repos/$R/rulesets --jq '.[] | {id, name, target, enforcement}'
for id in $(gh api repos/$R/rulesets --jq '.[].id'); do gh api repos/$R/rulesets/$id > ruleset-$id.json; done
gh api repos/$R/rules/branches/$B > active-rules.json            # what applies to the branch, from every ruleset
gh api repos/$R/branches/$B/protection > classic.json 2>/dev/null || echo "no classic protection"
gh api repos/$R/codeowners/errors > codeowners-errors.json
git fetch origin $B && git show origin/$B:.github/CODEOWNERS > CODEOWNERS 2>/dev/null \
  || git show origin/$B:CODEOWNERS > CODEOWNERS 2>/dev/null || git show origin/$B:docs/CODEOWNERS > CODEOWNERS
grep -l "merge_group" .github/workflows/*.y*ml                   # workflows that run in the merge queue
```

Write down: every ruleset that targets the branch and its `enforcement`, the `bypass_actors` of each, whether classic protection exists as well (both apply), the allowed merge methods, the required check contexts, and which workflow job produces each context.

## 2. Evaluate the rules as data

Pick five recently merged pull requests and five open ones, export their facts (`gh api repos/$R/pulls/N/reviews`, `.../pulls/N/files`, `.../commits/SHA/check-runs`) into the JSON `mergequeue protect` reads, and run:

```bash
python3 mergequeue.py protect --ruleset ruleset-ID.json --pr pr-N.json --codeowners CODEOWNERS --teams teams.json --json
```

A merged pull request that `protect` would block means the live configuration differs from the export, or a bypass was used: find out which. Then check each finding:

| Finding | Risk | Fix |
|---|---|---|
| `enforcement` is `evaluate` or `disabled` | nothing is blocked | set `active` once the evaluate-mode report is clean |
| broad `bypass_actors` (a whole team, "repository admin" for everyone) | the rules are optional | keep bypass to a break-glass role with an audit trail |
| no `pull_request` rule, or 0 required approvals | direct or unreviewed changes | require at least one approval and code owner review |
| `dismiss_stale_reviews_on_push` off | an approval of old code merges new code | turn it on; add `require_last_push_approval` for two-person rules |
| `require_code_owner_review` on, but hot paths unowned (step 3) | the rule passes vacuously | add owners for every deployable path |
| `strict_required_status_checks_policy` on, no merge queue | the update-branch treadmill, CI cost grows with the number of open pull requests | enable the merge queue, then turn strict off |
| a required check with no `merge_group` trigger | every queue entry waits until the timeout and is removed | add `merge_group:` to the workflow `on:` |
| a required check from a workflow with `paths:` filters | "Expected" forever on pull requests that skip it | filter inside the job with `if:`, or require one always-running summary job |
| `required_linear_history` with merge method `MERGE` | the queue cannot merge anything | use `SQUASH` or `REBASE` |
| `required_signatures` with a rebase merge done by a tool that cannot sign | re-created commits lose their signatures | squash or merge on the platform, or sign in the tool |
| no `non_fast_forward` or `deletion` rule | history can be rewritten or the branch deleted | add both |
| rule types in `not_evaluated` | the verdict is incomplete | review those rules by hand |

## 3. Lint CODEOWNERS

```bash
git ls-tree -r --name-only origin/$B | python3 mergequeue.py owners --codeowners CODEOWNERS --stdin --strict > owners.txt; echo "exit $?"
grep "(no owners)" owners.txt | cut -d' ' -f1 | sed 's|/[^/]*$||' | sort | uniq -c | sort -rn | head -20
python3 mergequeue.py owners --codeowners CODEOWNERS --stdin --compare < <(git ls-tree -r --name-only origin/$B) | head -40
cat codeowners-errors.json
```

| Finding | Do |
|---|---|
| lines reported as skipped (`!`, `[ ]`, bad owner syntax) or in `codeowners/errors` | rewrite them; GitHub does not use them |
| an unknown owner or a team without write access | fix the name or the team's permission; until then the line protects nothing |
| a broad line (`*.sql`, `*.yaml`, `docs/`) after a specific one | it takes ownership away for those files; move it above, or repeat the specific owners on a later, more specific line |
| `dir/*` meant to cover subdirectories | use `dir/` or `/dir/**` |
| deployable paths (services, infrastructure, workflows, CODEOWNERS itself) unowned | add owners; put `/.github/` under the platform team |
| the only owner of a path is one person | add a team; an author can never approve their own change |

## 4. Measure the queue and the branches

```bash
gh pr list --repo $R --state merged --limit 200 --json number,createdAt,mergedAt > merged.json
python3 -c "
import json, statistics, datetime as d
p = lambda s: d.datetime.fromisoformat(s.replace('Z', '+00:00'))
h = sorted((p(x['mergedAt']) - p(x['createdAt'])).total_seconds() / 3600 for x in json.load(open('merged.json')))
print('pull requests', len(h), 'open-to-merge hours p50', round(statistics.median(h), 1), 'p90', round(h[int(0.9 * len(h)) - 1], 1))"
gh run list --repo $R --event merge_group --limit 100 --json conclusion,createdAt,updatedAt > queue-runs.json
```

From these compute: pull requests per working hour (arrivals), the CI duration p50 and p90 and the failure rate of `merge_group` runs, and the queue load = arrivals × CI hours ÷ `max_entries_to_build`.

| Measurement | Do |
|---|---|
| load at or above 0.8 | raise `max_entries_to_build`, or batch with `max_entries_to_merge` when the failure rate is low |
| `merge_group` failure rate above 10% | fix flaky tests first; every failure restarts the candidates behind it |
| failures on `merge_group` that passed on the pull request | semantic conflicts are being caught: the queue is doing its job, keep it |
| open-to-merge p50 above two working days | branches are too long; split work into stacked pull requests and merge behind flags |
| CI p90 above 30 minutes | shard or cache the required checks; queue throughput is bounded by them |

## 5. Report

Post: the repository and branch; every ruleset and classic rule that applies, with enforcement and bypass actors; the required checks and whether each runs on `merge_group`; the merge methods and history rules; CODEOWNERS problems (skipped lines, unknown owners, unowned deployable paths, ownership taken by later lines) with line numbers; the queue load, CI p50 and p90, `merge_group` failure rate and branch lifetime p50 and p90; each finding from the tables with its fix, ordered by risk; and the pull requests checked with `protect`, including any that merged although the rules would block them.
