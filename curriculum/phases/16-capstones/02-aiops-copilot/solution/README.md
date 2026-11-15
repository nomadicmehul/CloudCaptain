# An AIOps Copilot That Triages Real Incidents: reference solution

Finish the capstone before reading this. CI grades this folder on every change
to prove the grader can be passed with full marks.

Python 3.9+ standard library only; every cluster read or write is a `kubectl`
subprocess, and the model is called over raw HTTP.

| File | Milestone | What it does |
|---|---|---|
| `bin/copilot` | all | entry point; puts the package on `sys.path` and calls `copilot.cli.main` |
| `copilot/cli.py` | all | subcommands, `--json` output, exit codes 0/1/2/3 |
| `copilot/config.py` | all | environment variables and the namespace allow-list (fails closed) |
| `copilot/kube.py` | all | `kubectl` runner pinned to `COPILOT_CONTEXT` |
| `copilot/evidence.py` | evidence | pods, ReplicaSet owners, EndpointSlices, Warning events and previous logs into items with ids derived from the object |
| `copilot/triage.py` | triage | the first-match rule table, citations, every Deployment listed |
| `copilot/agent.py` | agent-loop | request shape, headers, the loop with max turns and token budget |
| `copilot/tools.py` | agent-loop, guardrails | the five tools, namespace checks, Secret refusal, redact and wrap on every result |
| `copilot/guard.py` | guardrails | redaction patterns and the `<untrusted_data>` wrapper with escaping |
| `copilot/proposals.py` | guardrails | merge patches, server-side dry-run diffs, the proposal store, `apply --approve` |
| `copilot/report.py` | report | hypotheses, timeline, actions (manual or rollback proposals), markdown |
| `copilot/evals.py` | evals | suite runner, optional model verdicts, Wilson interval |
| `tests/test_copilot.py` | all | unit tests with fake `kubectl` and a scripted model |

```bash
python3 -m unittest discover -s solution/tests -v
bash grader/grade.sh solution
```
