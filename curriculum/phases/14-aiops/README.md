# AIOps & Agentic Ops

*MCP servers for your cluster, incident-triage agents, LLM observability, guardrails and evaluations for ops agents.*

"AIOps" used to mean anomaly detection on metrics. In 2026 it means agents that read your logs, query your clusters and propose, sometimes execute, changes. That makes two engineering questions urgent and answerable: what exactly can an agent do to production, and how do you know whether it is doing it well? This phase builds the pieces that answer them: the tool servers agents call, the guardrails around write paths, the traces that show what an agent did and why, and the evaluations that turn "it seemed to work" into a number.

## How this phase is built

| Lesson | Type | What you build | What you then use |
|---|---|---|---|
| [MCP for kubectl](01-mcp-kubectl-server/docs/en.md) | Build | a read-only Model Context Protocol server over JSON-RPC/stdio with typed tools, a namespace allow-list and bounded output | Claude Code / Cursor MCP config, the MCP Inspector, the official Kubernetes and Terraform MCP servers |
| [Triage Agent](02-incident-triage-agent/docs/en.md) | Build | an agent loop over the Messages API from the standard library: MCP client, step and tool budgets, scope, one write path behind three gates, audit, trace, cost, a strict report tool | the SDK tool runner with MCP helpers, Claude Code as the loop, Managed Agents permission policies |
| [Log Analysis](03-log-analysis/docs/en.md) | Build | `logmine`: parses the usual log formats, masks variables, clusters lines into templates the way Drain does, ranks what is new, spiking or gone since a deploy marker, and hands the model a redacted digest under a token budget | `kubectl logs`, stern, Loki pattern detection, the Claude API with a strict report tool |
| [LLM Observability](04-llm-observability/docs/en.md) | Build | `agenttrace`: the triage agent's trace and audit as OpenTelemetry spans with GenAI semantic-convention attributes, exported as OTLP/HTTP JSON, viewed as a tree and summarised as p95 latency, cost, tool error and refusal rates | an OpenTelemetry Collector in Docker, Jaeger, Grafana Tempo |
| [Guardrails & Sandboxes](05-guardrails-sandboxes/docs/en.md) | Build | an allow-list egress proxy (HTTP forwarding, CONNECT tunnels, an audit log) and a sandbox policy rendered to exact `docker` arguments: hardening flags, a short-lived read-only token, an `--internal` network whose only exit is the proxy, plus a drift check | Docker internal networks, Kubernetes NetworkPolicy, Cilium, gVisor |
| [Safe Write Tools](06-write-tools-safely/docs/en.md) | Build | an MCP server whose write tools are two calls apart: a plan that changes nothing and returns a confirm phrase, then an execute that can only do what the plan described, once (TTL, idempotency, protected namespaces, audit with before and after) | Claude Code MCP config, kind, OpenTofu plan/apply, Argo CD sync windows |
| [Agent Evals](07-agent-evals/docs/en.md) | Build | an eval harness for the triage agent: scripted incidents behind a generated fake kubectl, several trials each, graders that turn a report into pass or fail, pass rates per scenario and a gate against a saved baseline | promptfoo, GitHub Actions, a live run against the model |

Further lessons for this phase are proposed in [ROADMAP.md](https://github.com/nomadicmehul/CloudCaptain/blob/main/ROADMAP.md).

## Reference layer

The Kubernetes guides this phase leans on live under [Kubernetes](https://cloudcaptain.io/docs/tools/kubernetes/); the AI/ML Ops learning path at [cloudcaptain.io/docs/learning-paths/ai-ml](https://cloudcaptain.io/docs/learning-paths/ai-ml) maps the wider landscape.
