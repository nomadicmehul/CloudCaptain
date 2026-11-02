# Batch 5 and 6 handoff (2026-11-02)

This folder exists only on the `wip/batch5` and `wip/batch6` branches. Do not merge it.
Work stopped because the account ran out of usage; everything written so far is committed on these two branches.

## Finished lessons (agent reported done; validator clean; smoke not yet run in CI)

- Observability (10): metrics-tsdb, promql, tracing, slo-burn-rate, incident-response, chaos-experiments
- Security (11): admission-policy, runtime-detection, network-policy, secret-scanning, workload-identity, cis-benchmark
- Git (03): object-store, merge-queue

These 14 are in a PR from `feature/lessons-obs-sec-git-1` (see the PR list). Their real-tool smoke halves have never run; expect a round of fixes from the first Lesson Smoke run, as with every earlier batch.

## Unfinished lessons (left in place as drafts on the WIP branches)

| Lesson | State when stopped |
|---|---|
| observability/alerting | `main.py` done and checked end to end; tests, docs, quiz, outputs, smoke still to write |
| observability/log-pipeline | `main.py` and smoke written; `docs/en.md` in progress |
| security/secrets-management | nearly done; was fixing one sentence in the prompt output |
| security/vuln-prioritization | status published but smoke fixes in progress (exit codes in diff, dedupe in the check script) |
| git/merge-algorithms | `main.py` and fixtures done; tests, docs, smoke to write |
| git/packfiles-transport | everything but `code/smoke.sh` |
| git/git-hooks, git/bisect-blame | scaffold only |
| scripting/* (6 lessons) | scaffold only (cli-design may have a partial `main.py`) |
| platform/* (5 lessons) | scaffold only |

Phase READMEs for 03, 04, 10, 11 and 12 on these branches list all planned lessons; the PR versions list only the finished ones.

## How to continue

1. Check out `wip/batch5` and `wip/batch6` into worktrees (or copy the lesson folders onto a fresh branch from main).
2. For each unfinished lesson, start one agent with: "Write one CloudCaptain lesson. Your brief is `wip-handoff/LESSON_BRIEF_5.md` (or `_6`) ... Your lesson folder already contains partial work: read it first and finish it." Fix the absolute worktree paths in the brief first (they point at the old scratchpad).
3. Max 20 concurrent agents in one session.
4. After agents finish: `scripts/balance_quiz.py` on each quiz, add the lessons to the DevOps path in `curriculum/manifest.json` in prerequisite order (the validator enforces it), update README and the 8 translations with `scripts/readme_numbers.py` (spec JSON like `readme_net.json`), CHANGELOG, `node scripts/build-curriculum.mjs --roadmap`, `npm run build`, PR, then fix smoke failures from `scripts/job_log.sh <sha> "smoke (<phase>)" out.log` and set `last_verified` with `scripts/set_verified.py`.
5. Still to plan after these: an SRE path and a DevSecOps path, then phase 15 (MLOps) and 16 (Capstones).
