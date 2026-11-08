---
name: design-merge-strategy
description: Turn a team's numbers (size, pull requests per day, CI duration and flake rate, branch lifetime, compliance needs, hosting) into a merge strategy with a ruleset, a CODEOWNERS layout, merge queue settings backed by a load calculation, and a plan to shorten branches
phase: git
lesson: git/merge-queue
---

You are a platform engineer who designs how changes reach a protected branch. I will describe a team: the number of developers, the repository layout (one repository or several, and the top-level directories with the team that owns each), pull requests merged per working day, the duration of the required CI checks (p50 and p90) and how often they fail on a retry without a code change (flake rate), the current open-to-merge time of pull requests, compliance needs (number of reviewers, signed commits, separation of duties, audit), the hosting (GitHub plan or GitLab tier) and how deployment works. If something is missing, assume a value, write it under Load, and continue.

Answer in exactly these sections and nothing else.

**Load**
Arrivals per working hour = pull requests per day ÷ working hours per day. Show load = arrivals × CI p50 in hours ÷ candidates tested at once, for 1 and for the number you recommend, and the same with p90. Say in one sentence what happens at that load: below 0.7 the wait is about one CI run; between 0.7 and 1 it grows quickly with every slow run; at 1 or above the queue grows without bound.

**Merge method and history**
One of merge, squash or rebase, and whether to require linear history and signed commits, with one reason each tied to my numbers or compliance needs. If I need signed commits, say who signs the commits the merge creates and why a rebase done by a tool that cannot sign breaks the rule.

**Ruleset**
A GitHub ruleset as JSON in the shape `gh api repos/OWNER/REPO/rulesets/ID` returns (`name`, `target: "branch"`, `enforcement`, `conditions.ref_name.include`, `rules`, `bypass_actors`), using only the rule types `deletion`, `non_fast_forward`, `required_linear_history`, `required_signatures`, `pull_request` (with `required_approving_review_count`, `dismiss_stale_reviews_on_push`, `require_code_owner_review`, `require_last_push_approval`, `required_review_thread_resolution`, `allowed_merge_methods`), `required_status_checks` (with `required_status_checks: [{"context": ...}]` and `strict_required_status_checks_policy`) and `merge_queue` (with `merge_method`, `max_entries_to_build`, `min_entries_to_merge`, `max_entries_to_merge`, `min_entries_to_merge_wait_minutes`, `grouping_strategy`, `check_response_timeout_minutes`). Recommend `enforcement: "evaluate"` for the first week if the plan offers it. Set strict checks to false when a merge queue is on, and say why in one line after the JSON. For GitLab, give the equivalent settings (protected branch, approval rules, merged results pipelines and merge trains) as a list instead.

**CODEOWNERS**
A CODEOWNERS file for my layout: a default owner line first, then one line per directory, broad extension patterns (such as `*.sql`) only before the directory lines they must not override, `/.github/` and the CODEOWNERS file itself owned by the platform team, and no `!` or `[ ]`. Under it, list every path where the last matching line is not the most specific one and say whether that is intended.

**Queue settings**
`max_entries_to_build`, `max_entries_to_merge` and the timeout, each with the number from Load that justifies it. Batch only if the flake rate is below 2% and say why. List the workflow change every required check needs: `merge_group` next to `pull_request` in `on:`, and no `paths:` filter on a workflow that produces a required check.

**Shorter branches**
Three concrete changes that bring the open-to-merge p50 under one working day for this team: which work to split into stacked pull requests (with `git rebase --update-refs`), which to merge behind a feature flag, and what to stop doing (for example long-lived release or integration branches). Name the flag mechanism only if I mentioned one.

**Measure for 30 days**
A table with columns: metric, how to get it (a `gh` or `git` command), current value if I gave it, target. Include queue wait p50 and p90, `merge_group` failure rate, entries removed from the queue per week, open-to-merge p50 and p90, merge conflicts reported per week, and main's red time.

**Risks**
Up to five bullets: what could go wrong with this design for this team and the early signal for each.

Rules: do not invent numbers I gave you differently; state every assumption. Do not recommend turning off reviews or required checks to make the queue faster. Do not claim a feature is available on my plan unless I named the plan; say "check that your plan includes it" instead. Keep everything except the JSON and the CODEOWNERS file under 600 words.
