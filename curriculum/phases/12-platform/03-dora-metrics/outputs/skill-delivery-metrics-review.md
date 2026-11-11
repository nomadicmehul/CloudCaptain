---
name: delivery-metrics-review
description: Review a team's DORA metrics for a period by checking the deploy, commit and incident sources, computing the four keys with minidora for this period and the previous one, reconciling a sample by hand, reading the gaming and data-quality warnings, and finding the delivery constraint from the slowest changes and the failed deploys
version: 1.0.0
phase: platform
lesson: platform/dora-metrics
tags: [dora, delivery-metrics, lead-time, change-failure-rate, deployment-frequency, recovery-time, four-keys, github-deployments, platform-engineering]
---

# Delivery metrics review

Use this monthly or quarterly for one service, before DORA numbers go on a slide, when a metric moves suddenly, or when someone proposes a metric as a team target. Work from events: the Git repository of the deployed branch, the production deploy log (or GitHub deployments with their statuses) and the incident log with root-cause commits where postmortems recorded them. Never edit the events to improve a number; fix the source and say so in the report.

## 1. Are the sources complete and the right ones?

```bash
git -C REPO fetch --all --prune && git -C REPO log --all --format='%H%x1f%P%x1f%at%x1f%an%x1f%s%x1e' > git.log
gh api repos/OWNER/REPO/environments --jq '.environments[].name'
gh api --paginate 'repos/OWNER/REPO/deployments?environment=production&per_page=100' --jq '.[]' | jq -s 'length'
python3 minidora.py report --git-log git.log --deploys DEPLOYS --incidents incidents.jsonl --since START --until END --json > now.json; echo "exit $?"
jq '.problems, [.attempts[] | .status], [.incidents[] | .how] ' now.json
```

| What you see | Meaning | Next |
|---|---|---|
| exit 1, `commit ... is not in the Git history` | deploys of commits that were force-pushed away, or from another repository | fetch the right repository and all refs; if the commit is gone, report the deploy as unmeasurable, do not drop it silently |
| exit 1, `root cause ... was never deployed` | the postmortem names a commit from a branch that never shipped | correct the root cause in the incident record |
| deploys from `staging` or preview environments in the count | wrong environment filter | `--environment production`; check the environment names above |
| many `attempts` | failed or pending deploys | not deployments; look at them as pipeline reliability, separately |
| most incidents `unlinked` | no root causes recorded and nothing deployed shortly before | add `root_cause` to the postmortem template; until then, the change failure rate is a lower bound |
| exit 2 | unreadable or wrongly shaped input | fix the export; GitHub deployments need their `statuses` attached |

## 2. Compute this period and the previous one

```bash
python3 minidora.py report --git-log git.log --deploys DEPLOYS --incidents incidents.jsonl --since PREV_START --until START --json > before.json
python3 minidora.py report --git-log git.log --deploys DEPLOYS --incidents incidents.jsonl --since START --until END --view weekly --changes
```

Use the same window (`--window-hours`), environment and sources for both periods. Read deployment frequency together with the number of changes, lead time as median and p90, the change failure rate with its count of failed deploys, and failed deployment recovery time next to the old all-incident time to restore.

## 3. Reconcile a sample by hand

Pick two deploys, one failed. For each, confirm with Git what it shipped and how the failure was linked:

```bash
git -C REPO rev-list --first-parent DEPLOYED ^PREVIOUS_DEPLOYED     # the change heads minidora lists for it
git -C REPO rev-list --count DEPLOYED ^PREVIOUS_DEPLOYED
git -C REPO merge-base --is-ancestor DEPLOYED RUNNING_BEFORE && echo "this deploy was a rollback"
jq '.deploys[] | select(.id == "DEPLOY_ID")' now.json
```

If the change heads, the commit count or the failure cause differ from what `minidora` reports, stop: the numbers are not ready to publish.

## 4. Read the warnings

| Warning | Verdict |
|---|---|
| `empty-deploys` | redeploys inflate frequency; report frequency with and without them and find the job that redeploys unchanged commits |
| `bursty-deploys` | deploys arrive in batches; look for a release train or manual approval gate |
| `single-commit-changes` | squash or rebase merges hide review time; measure lead time from the pull request's first commit, or accept that it is merge-to-deploy time |
| `no-failures-recorded`, `unlinked-incidents` | the change failure rate is not trustworthy; publish it as "at least" |
| `open-incidents` | recovery time is missing for these; wait for resolution or list them |
| `small-sample` | compare periods, not single numbers; widen the period |

## 5. Find the constraint

```bash
jq -r '.changes | sort_by(-.lead_seconds) | .[:5][] | "\(.lead_seconds / 3600 | floor) h  \(.first_commit)  \(.deploy)  \(.subject)"' now.json
jq -r '.deploys[] | select(.failed) | "\(.id) \(.causes | join(", ")) recovered after \((.recovery_seconds // 0) / 3600) h, \(.changes | length) change(s)"' now.json
```

| Pattern | Likely constraint | Action to try |
|---|---|---|
| p90 lead time far above the median, slow changes have old first commits | long-lived branches, slow review | smaller pull requests, a review rotation, a review-time budget |
| changes merge quickly but wait for a deploy | release batching, manual gates | deploy on merge, automate the gate |
| failures cluster on deploys with many changes | big batches | deploy more often with fewer changes per deploy |
| recovery is long and done by hotfix | no fast rollback | make rollback one command, add a canary |
| frequency up, lead time flat, `empty-deploys` up | the metric became a target | remove the target; track the outcome it was meant for |

## 6. Report

State: the service, period and previous period; the sources (repository and refs, deploy source and environment, incident source) and the window; a table of the four keys for both periods with the counts they are computed over, plus the old time to restore; every problem and warning with what it means for the numbers; the hand-reconciled sample; the constraint you found with the slowest changes and the failed deploys as evidence; and one or two actions, each with the metric it should move and the one it must not worsen. If you quote a performance level, name the State of DevOps report year it comes from. Never rank this team against other teams.
