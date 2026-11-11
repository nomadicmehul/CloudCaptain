---
name: hook-rollout
description: Roll out Git hooks to a team so they catch problems seconds after they are made without being skipped: inventory, a pinned pre-commit configuration, a clean baseline, a timing budget, staged rollout, and the same rules enforced in required CI and server-side or hosted push rules
version: 1.0.0
phase: git
lesson: git/git-hooks
tags: [git, hooks, pre-commit, commit-msg, pre-receive, conventional-commits, secret-scanning, ci]
---

# Hook rollout

Use this when someone asks "can we add pre-commit hooks", "why did this secret get pushed when we have a hook", "the hooks are too slow, people use --no-verify" or "how do we enforce commit messages". The rule behind every answer: a client hook is fast feedback, never the enforcement. Every rule a hook checks must also run where nobody can skip it.

## 1. Inventory what exists

```bash
git rev-parse --git-path hooks                               # where Git looks in this clone
git config --show-origin --get-all core.hooksPath            # set by husky, a team script, or nobody
ls -la "$(git rev-parse --git-path hooks)" | grep -v '\.sample$'
cat .pre-commit-config.yaml lefthook.yml .husky/* 2>/dev/null | head -100
grep -rlE 'pre-commit|lefthook|husky|commitlint|gitleaks' .github/workflows/ .gitlab-ci.yml 2>/dev/null
git log --since=90.days --format='%s' | head -500 > subjects.txt   # message style today
```

Write down: which framework (if any) and how it gets installed, which checks run locally, which of them also run in CI and whether that CI job is required, and whether a server-side hook, push ruleset, push rule or secret scanning push protection exists for the default branch.

## 2. Choose the framework

| Situation | Choose | Why |
|---|---|---|
| Polyglot repository, Python on every machine, many ready-made hooks wanted | pre-commit | per-hook environments, staged-only runs with unstaged edits set aside, `run --all-files` for CI |
| JavaScript repository, Node already required | husky with lint-staged | installs through the `prepare` script; lint-staged selects staged files |
| Large monorepo, hooks must be fast, no runtime wanted | lefthook | one binary, parallel commands, globs per command |
| Only a commit message rule | a `commit-msg` hook via the chosen framework plus a CI check of the pull request's commits | the message is the only input |
| `core.hooksPath` is already set by another tool | keep one tool per repository | pre-commit refuses to install while `core.hooksPath` is set |

## 3. Write the configuration

- Pin every remote hook repository to a full commit SHA: `pre-commit autoupdate --freeze`. A tag can be moved to different code.
- Filter precisely with `files`, `exclude` and `types`; check a hook's input with `pre-commit run HOOK --all-files --verbose` and file tags with `identify-cli PATH`.
- Put fixers (formatters, whitespace, end-of-file) before checkers on the same files, so checkers see fixed content.
- Set `default_stages: [pre-commit]` and give `commit-msg` and `pre-push` hooks explicit `stages:`, so a file hook never runs against the message file.
- Keep anything slow (full test suites, type checks of the whole project, network calls) out of `pre-commit`; run it in `pre-push` or only in CI.
- Add a secret check to every repository (`detect-private-key` from pre-commit-hooks, gitleaks or detect-secrets) and keep its allowlist in the repository, reviewed like code.

## 4. Baseline before anyone installs it

```bash
pre-commit run --all-files --show-diff-on-failure > baseline.txt 2>&1; echo "exit $?"
grep -E '\.(Failed)$' baseline.txt
time pre-commit run --all-files > /dev/null 2>&1
git ls-files | wc -l
```

Land the fixes the fixers made as one mechanical commit, and list that commit in `.git-blame-ignore-revs` so blame skips it. Fix or allowlist every remaining failure. Then measure: a typical commit touches 1 to 10 files; time `pre-commit run --files` on ten typical files.

| Measurement | Do |
|---|---|
| a typical commit's hooks take more than 5 seconds | move the slowest hooks to `pre-push` or CI; people skip slow hooks |
| a hook fails on files nobody changed | it is reading the working tree or the whole repository; fix its filters or `pass_filenames` |
| a fixer and a checker disagree (the checker fails on fixed output) | align their configuration (line length, quote style) before rollout |
| the secret check reports test fixtures | allowlist those paths explicitly, never disable the hook |

## 5. Roll out in stages

1. Add the configuration and the CI job (step 6) in one pull request; the CI job is not required yet.
2. Ask a few volunteers to run `pre-commit install --hook-type pre-commit --hook-type commit-msg` for a week and report false positives and timing.
3. Make installation automatic where the ecosystem allows it (`prepare` script for husky, a `make setup` or devcontainer `postCreateCommand` for pre-commit).
4. Make the CI job required on the default branch.
5. Announce `SKIP=hook-id git commit` for a single hook instead of `--no-verify` for all of them.

## 6. Enforce where it cannot be skipped

```yaml
name: hooks
on: [pull_request, merge_group]
jobs:
  hooks:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v5
      - uses: actions/setup-python@v6
        with:
          python-version: "3.12"
      - run: python3 -m venv .venv && .venv/bin/pip install --require-hashes -r requirements-pre-commit.txt
      - run: .venv/bin/pre-commit run --all-files --show-diff-on-failure
```

| Rule | Server-side or hosted enforcement |
|---|---|
| no files above a size | `pre-receive` size check; GitHub push ruleset "restrict file size"; GitLab push rule "maximum file size" |
| no secrets | secret scanning push protection, a `pre-receive` scanner, plus revocation of anything that was pushed |
| commit message format on the default branch | `pre-receive` check on protected refs, or a CI job that checks every commit of the pull request |
| signed commits | ruleset "require signed commits", or `pre-receive` with `gpg.ssh.allowedSignersFile` |
| no force pushes or deletion of the default branch | branch protection or ruleset; `pre-receive` with `merge-base --is-ancestor` |

Check that your plan includes the hosted rule before you rely on it. On a self-hosted server, install `pre-receive` in the bare repository and test it with a rejected and an accepted push before announcing it.

## 7. Report

Post: the repository; the framework and how it is installed; the hooks with stage, file filter and median time on a typical commit; the baseline result and the mechanical commit's SHA; which rules run in required CI and which in server-side hooks or hosted push rules, with any rule that runs only on clients flagged as a gap; the secret check and its allowlist; how often `--no-verify` or `SKIP` is used if you can tell (for example, CI failures on checks the hooks would have caught); and the next change, ordered by risk.
