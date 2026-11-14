# Capstone Projects

*Three graded projects that put the phases together: a platform, an on-call copilot and a landing zone.*

The lessons build one mechanism at a time. A capstone asks for a whole system, the way a team lead would hand it over: a brief, an interface other people depend on, and a deadline measured in milestones. You start from a skeleton in `starter/`, build in your own repository, and a grader scores each milestone against real tools on your machine: a kind cluster, Docker, OpenTofu and a local AWS emulator. Nothing needs a cloud account or an API key. When the score passes, copy the grader's workflow into your repository and every push is graded in CI. A reference build lives in `solution/`; finish before you read it.

## How this phase is built

| Capstone | What you build | Draws on | Graded with |
|---|---|---|---|
| [Build a Tiny PaaS](01-build-a-paas/docs/en.md) | `tinypaas`: an app manifest, image builds per commit, deploys, `git push` to deploy, config, scale, releases with rollback and merged logs | Containers, Orchestration, Git, CI/CD, Platform Engineering | Docker, kind, git |
| [An AIOps Copilot](02-aiops-copilot/docs/en.md) | `copilot`: evidence collection, rule-based triage, a tool-using agent loop on the Claude Messages API, guardrails and redaction, incident reports and evals | AIOps & Agentic Ops, Observability | kind with injected incidents and a scripted model |
| [A Landing Zone You Can Prove](03-landing-zone/docs/en.md) | OpenTofu modules for a hub-and-spoke network and identity baseline, a policy gate on plans, the same interface on GCP and Azure, remote state with locking and drift detection | Infrastructure as Code, Cloud | OpenTofu, moto, `tofu test` with mock providers |

Each capstone takes about twelve hours. Grade a single milestone while you work with `bash grader/grade.sh <your-project> --milestone <id>`.

## Reference layer

The role guides that these projects prepare for are in the [learning paths](https://cloudcaptain.io/docs/learning-paths/career-paths).
