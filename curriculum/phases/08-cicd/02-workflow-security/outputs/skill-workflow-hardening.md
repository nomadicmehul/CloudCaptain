---
name: workflow-hardening
description: Audit and harden the GitHub Actions workflows of one repository: privileged triggers, script injection, token permissions, action pins, persisted credentials, caches, secrets and runners, with zizmor, actionlint and wfaudit as the checks
version: 1.0.0
phase: cicd
lesson: cicd/workflow-security
tags: [github-actions, ci-cd, supply-chain, zizmor, actionlint, permissions, sarif, security]
---

# Workflow hardening

Use this when a repository's workflows have never been reviewed for security, after an incident involving an action or a token, before making a repository public, or when a scanner reports findings nobody owns. The goal is a set of workflows where outsiders cannot run code with your secrets, every action is pinned, every token is minimal, and the checks stay in CI.

## 1. Inventory

```bash
ls .github/workflows/ .github/actions/ 2>/dev/null
grep -rnE '^\s*(on:|  (pull_request_target|workflow_run|issue_comment|issues|discussion)\b)' .github/workflows/
grep -rnE 'uses:' .github/workflows/ | grep -vE '@[0-9a-f]{40}' | grep -vE 'uses: *(\./|\$/)'
grep -rnE 'secrets\.|toJSON\(secrets\)|secrets: inherit' .github/workflows/
grep -rnE 'runs-on:.*self-hosted|cache-mode:' .github/workflows/
gh api "repos/{owner}/{repo}" --jq '{visibility, default_branch}'
gh api "repos/{owner}/{repo}/actions/permissions/workflow"          # default token permissions and PR approval setting
gh api "repos/{owner}/{repo}/environments" --jq '.environments[].name'
```

Write down per workflow: its triggers, the jobs and what each deploys or publishes, the secrets it references, and the actions it uses with their refs. The repository visibility decides how serious a self-hosted runner is.

## 2. Run the scanners

```bash
zizmor --offline .github/workflows/*.yml                       # add a token (GH_TOKEN) to run the online audits too
zizmor --format json .github/workflows/*.yml > zizmor.json
actionlint -format '{{json .}}' > actionlint.json || true       # uses shellcheck on run: scripts when installed
python3 wfaudit.py --public --json . > wfaudit.json            # --private for private repositories
```

Merge the three by (file, line, class). Treat a finding reported by two tools as confirmed; read single-tool findings yourself before acting.

## 3. Fix in this order

| Finding | Why first | Fix |
|---|---|---|
| privileged trigger (`pull_request_target`, `workflow_run`, `issue_comment`) checks out and runs the head | code execution with secrets for anyone who opens a PR | move the build to `pull_request`; the privileged workflow only reads artifacts as data, or runs after a maintainer adds a label |
| `${{ <event text> }}` in `run:` or `github-script` `script:` | code execution for anyone who writes a title, comment or branch name | `env: TITLE: ${{ ... }}` and `"$TITLE"` in the script; never `${{ env.TITLE }}` |
| attacker text written to `$GITHUB_ENV` or `$GITHUB_PATH` | a newline sets `BASH_ENV` or `LD_PRELOAD` for every later step | pass data through `$GITHUB_OUTPUT`; write only constants |
| no `permissions:` or `write-all` | every other bug becomes a write to the repository | `permissions: {}` at the top, per-job minimum (wfaudit prints it) |
| action or reusable workflow not pinned by SHA | the code changes when someone moves a tag | `owner/repo@<40-char sha> # vX.Y.Z`, then Dependabot or Renovate; or adopt `gh actions-lock` for actions (not for reusable workflows) |
| secret given to an unpinned third-party action, `toJSON(secrets)`, `secrets: inherit`, a printed or sliced secret | the secret leaves the runner | pin the action, pass secrets by name to one step, move deploy secrets to an environment with required reviewers |
| checkout without `persist-credentials: false` | the token stays on disk for later steps and artifacts | add it to every checkout; give a pushing step its own token |
| explicit `cache-mode: write` on a low-trust trigger, or a release job that restores caches | a cache entry from untrusted code ends up in a release | remove the override; `cache-mode: none` or no cache step in publishing jobs |
| self-hosted runner on a public repository | fork code runs on your machine | GitHub-hosted or ephemeral just-in-time runners; require approval for outside contributors |

Fix one class per pull request so reviewers can follow it. After each, rerun step 2 on that workflow.

## 4. Resolve and pin actions

```bash
gh api repos/actions/checkout/commits/v7.0.1 --jq .sha
git ls-remote https://github.com/OWNER/ACTION 'refs/tags/vX.Y.Z*'     # the ^{} line is the commit of an annotated tag
```

Confirm the SHA is on a branch or tag of the action's own repository, not only in a fork. Add `.github/dependabot.yml` with `package-ecosystem: github-actions` (weekly, grouped) or Renovate's `helpers:pinGitHubActionDigests` so the pins move with reviewed pull requests.

## 5. Settings that the YAML cannot express

- Default workflow permissions: restricted (read contents and packages only).
- Fork pull request workflows: require approval for outside collaborators (all outside collaborators on public repositories with self-hosted runners).
- Environments for every deploy or publish target, with required reviewers and deployment branch or tag rules; secrets moved from repository level into them.
- Allowed actions policy, and the full-length SHA requirement if the organisation supports it.
- `CODEOWNERS` entry for `.github/workflows/` so workflow changes need a security-aware reviewer.

## 6. Keep it fixed

```yaml
- run: zizmor --format sarif .github/workflows/*.yml > zizmor.sarif
- uses: github/codeql-action/upload-sarif@<sha> # vX.Y.Z
  with:
    sarif_file: zizmor.sarif
- run: actionlint
```

Run the scanners on every pull request that touches `.github/`, upload SARIF for code scanning, and fail the job on new high or critical findings.

## 7. Report

Post: the repository and its visibility; a table of workflows with triggers, jobs, secrets and the before/after findings per severity from each scanner; the pull requests that fixed each class; the settings changed in step 5; findings accepted with a reason, an owner and a review date (never silently ignored); and the remaining risks that need a human decision, such as a `pull_request_target` workflow kept for labelling. Do not call the repository hardened while any critical finding or any unpinned third-party action receiving a secret remains.
