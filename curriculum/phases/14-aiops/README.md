# AIOps & Agentic Ops

*MCP servers for your cluster, incident-triage agents, LLM observability, guardrails and evaluations for ops agents.*

"AIOps" used to mean anomaly detection on metrics. In 2026 it means agents that read your logs, query your clusters and propose, sometimes execute, changes. That makes two engineering questions urgent and answerable: what exactly can an agent do to production, and how do you know whether it is doing it well? This phase builds the pieces that answer them: the tool servers agents call, the guardrails around write paths, the traces that show what an agent did and why, and the evaluations that turn "it seemed to work" into a number.

## How this phase is built

| Lesson | Type | What you build | What you then use |
|---|---|---|---|
| MCP for kubectl | Build | a read-only Model Context Protocol server over JSON-RPC/stdio with typed tools, a namespace allow-list and bounded output | Claude Code / Cursor MCP config, the MCP Inspector, the official Kubernetes and Terraform MCP servers |

More lessons in this phase are listed in [ROADMAP.md](https://github.com/nomadicmehul/CloudCaptain/blob/main/ROADMAP.md): an incident-triage agent with human-in-the-loop approval, AI-assisted log analysis, LLM observability with OpenTelemetry GenAI conventions, guardrails and sandboxes for agents that touch production, write tools done safely (confirm, audit, idempotency), and evaluations that tell you whether an ops agent is safe to run.

## Reference layer

The Kubernetes guides this phase leans on live under [Kubernetes](https://cloudcaptain.io/docs/tools/kubernetes/); the AI/ML Ops learning path at [cloudcaptain.io/docs/learning-paths/ai-ml](https://cloudcaptain.io/docs/learning-paths/ai-ml) maps the wider landscape.
