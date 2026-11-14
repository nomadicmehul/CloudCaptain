# An AIOps Copilot That Triages Real Incidents: starter

Copy this folder into a new repository and build the capstone there. The full brief, the
interface and every milestone are at https://cloudcaptain.io/learn/capstones/aiops-copilot.

## What is already here

| Path | What it gives you |
|---|---|
| `bin/copilot` | the entry point the grader runs (keep it executable) |
| `copilot/cli.py` | argument parsing for every command, exit codes, error handling |
| `copilot/config.py` | the environment variables, the namespace allow-list (`Config.require`) |
| `copilot/kube.py` | a `kubectl` runner pinned to `COPILOT_CONTEXT`, with JSON helpers |
| `copilot/agent.py` | `post_messages()`: one raw HTTP POST to `$COPILOT_MODEL_URL/v1/messages` |
| `copilot/{evidence,triage,guard,proposals,report,evals}.py` | one stub per milestone; each raises `NotImplementedError` |
| `tests/` | a few tests for the boilerplate; add yours next to them |

Every command already exists and prints `copilot: not implemented: milestone ...` to stderr with exit 1
until you build it. The grader sees that message and fails the milestone in seconds.

## The interface

| Command | Prints with `--json` |
|---|---|
| `copilot collect --namespace NS --json` | `{"namespace", "evidence": [{"id", "kind", "workload", "object", "signal", "summary", "time", "data"}]}` |
| `copilot triage --namespace NS --json` | `{"namespace", "incidents": [{"workload", "category", "citations", "reason"}]}` |
| `copilot ask --namespace NS [--max-turns N] [--token-budget N] "QUESTION" --json` | `{"answer", "stop_reason", "turns", "tool_calls", "proposals", "usage"}` |
| `copilot apply --approve PROPOSAL_ID --json` | the proposal, `status: "applied"` |
| `copilot report --namespace NS --format json\|md` | the incident report |
| `copilot eval SUITE.json [--trials N] --json` | `{"cases": [...], "summary": {...}}` |

Environment: `COPILOT_ALLOWED_NAMESPACES` (comma-separated names or patterns; empty refuses all),
`COPILOT_CONTEXT`, `COPILOT_HOME` (default `.copilot`), `COPILOT_MODEL_URL` (default
`https://api.anthropic.com`), `COPILOT_MODEL` (default `claude-opus-5-5`), `ANTHROPIC_API_KEY`.
Exit codes: 0 ok, 1 error, 2 usage, 3 refused.

You may rewrite everything in another language: the grader only runs `bin/copilot`.

## Milestones

| Id | Points | What the grader checks |
|---|---|---|
| `evidence` | 3 | each injected incident yields evidence naming the right pod and signal; the healthy decoy yields none; ids are stable |
| `triage` | 3 | exact category per workload; every citation is a real evidence id |
| `agent-loop` | 3 | scripted tool calls run against the cluster, real output reaches the fake model with matching ids, the limits stop a runaway loop |
| `guardrails` | 3 | writes stay proposals until `apply --approve`; disallowed namespaces are refused; no planted secret reaches the model; injected log text stays data |
| `report` | 2 | valid structure, citations resolve, ordered timeline, actions are proposals |
| `evals` | 2 | expected per-case verdicts and correct pass rate and Wilson interval |

Pass mark: 12 of 16.

## Grade it

You need Python 3.9+, `kubectl` and a kind cluster as the current context:

```bash
kind create cluster --name cc-grade
bash <cloudcaptain>/curriculum/phases/16-capstones/02-aiops-copilot/grader/grade.sh .
bash <cloudcaptain>/curriculum/phases/16-capstones/02-aiops-copilot/grader/grade.sh . --milestone evidence
```

Copy `grader/workflow.yml` from CloudCaptain to `.github/workflows/cloudcaptain-grade.yml` to grade every push.
