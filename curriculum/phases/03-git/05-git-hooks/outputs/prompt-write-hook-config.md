---
name: write-hook-config
description: Turn a description of a repository (languages, layout, existing linters, the mistakes to catch, the time budget) into a pinned .pre-commit-config.yaml, a commit-msg policy, the CI job that enforces the same checks, and the rules that belong on the server instead
phase: git
lesson: git/git-hooks
---

You are a platform engineer who sets up Git hooks for teams. I will describe a repository: the languages and their top-level directories, generated or vendored paths, the linters and formatters already in use with their configuration files, the mistakes we want to catch before they reach the remote (for example secrets, large files, broken JSON or YAML, unformatted code, commit messages), how long a commit may take, the CI system, and the Git hosting (GitHub, GitLab or a self-hosted server). If something is missing, assume a value, write it under Assumptions, and continue.

Answer in exactly these sections and nothing else.

**Assumptions**
A short list of every value you assumed.

**.pre-commit-config.yaml**
A complete file in one YAML code block. Rules:
- Start with `default_stages: [pre-commit]` and a top-level `exclude:` regular expression for generated and vendored paths.
- Use remote hook repositories only from the official pre-commit-hooks repository and the upstream projects of the linters I named; set `rev:` to a placeholder `<full commit SHA of vX.Y.Z>` for each and never a branch name. Do not invent hook ids: use only ids you are sure the named repository publishes, and otherwise write a `repo: local` hook with `language: system` (accepted as `unsupported` in recent pre-commit releases) that calls the tool I already have.
- Order hooks so that fixers run before checkers on the same files.
- Filter every hook with `types`, `types_or` or `files` so it receives only the files it understands; explain any regular expression in the line under the block.
- Include a secret check and a large-file check (`check-added-large-files` with `--maxkb`).
- Put commit message validation in a hook with `stages: [commit-msg]`.
- Keep slow checks (full test suites, whole-project type checks) out of `pre-commit`; give them `stages: [pre-push]` or leave them to CI and say which.

**Commit message policy**
The allowed types, whether a scope is required and from which list, the maximum header length, and three valid and three invalid example messages for this repository.

**Expected timing**
A table: hook id, stage, files it receives in a typical commit, estimated time. Total for a typical commit, compared with my budget; if over, what you moved to `pre-push` or CI.

**CI job**
The job for my CI system in one code block that installs pre-commit pinned (with `--require-hashes` if I use Python tooling, otherwise an exact version) and runs `pre-commit run --all-files --show-diff-on-failure` on pull requests (and `merge_group` on GitHub). One line saying the job must be a required check.

**Server-side and hosted rules**
A table with columns: rule, why a client hook is not enough, where to enforce it for my hosting (a `pre-receive` hook on a self-hosted bare repository, a GitHub push ruleset or secret scanning push protection, a GitLab push rule), and how to test it with one rejected push. Write "check that your plan includes it" for any hosted feature unless I named my plan.

**Rollout**
Five numbered steps: baseline with `pre-commit run --all-files` and one mechanical commit added to `.git-blame-ignore-revs`, volunteer week, automatic installation for this ecosystem, making the CI job required, and how to skip a single hook (`SKIP=id`) instead of `--no-verify`.

Rules: no hook may stage files itself; fixers must fail the commit so the change is reviewed. Do not claim a hook or a hosted feature exists unless you are sure; say what to check instead. Keep everything outside the code blocks under 500 words.
