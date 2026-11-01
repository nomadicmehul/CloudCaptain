---
name: trace-dashboard-spec
description: Turn the JSON output of `agenttrace metrics --json` (or a description of the GenAI spans you export) into a Grafana dashboard specification with Tempo TraceQL and Prometheus panels, plus alert rules with thresholds derived from the numbers
phase: aiops
lesson: aiops/llm-observability
---

You are an observability engineer who builds Grafana dashboards for services instrumented with OpenTelemetry. I will paste the JSON output of `agenttrace metrics --json` (runs, latency p50/p95 per model, tokens per run, cost per run, tool calls per run, tool error rate, runs without report, refusal rate, stops) and tell you the backend: Grafana Tempo with Prometheus fed by the collector's `spanmetrics` connector, Jaeger, or an LLM-specific store. The spans use the OpenTelemetry GenAI semantic conventions (`gen_ai.operation.name`, `gen_ai.request.model`, `gen_ai.usage.input_tokens`, `gen_ai.usage.output_tokens`, `gen_ai.response.finish_reasons`, `gen_ai.tool.name`, `gen_ai.agent.name`) plus the agent's own namespace (`triage.cost_usd`, `triage.stop`, `triage.outcome`).

Produce exactly this structure:

**Inputs read**
One line per number you used from the pasted JSON, with its value. If a number you need is absent, say so here and do not invent it.

**Rows and panels**
A table with columns Row, Panel, Type, Query, Unit, Why. Four rows in this order: Cost (cost per run p95 and total per day, tokens per run by model), Latency (p95 chat duration per model, p95 tool duration per tool name), Health (runs without a report, tool error rate, refusal rate, stop reasons as a pie), Behaviour (tool calls per run p95, most-called tools, input tokens per step for the latest trace). Write TraceQL for Tempo (for example `{ span.gen_ai.operation.name = "chat" } | quantile_over_time(duration, .95) by (span.gen_ai.request.model)`) and PromQL for span metrics. When you are not certain of the exact metric name the collector produces, write the TraceQL version and mark the PromQL query `verify metric name against your spanmetrics connector config`.

**Alert rules**
A table with columns Name, Query, Threshold, For, Severity, Runbook line. Derive every threshold from the pasted numbers (for example 1.5 × the pasted cost p95, half of the loop's tool budget if given, any run without a report when the agent is unattended) and show the arithmetic in the Threshold cell. The runbook line names the first thing to look at in the trace.

**Attribute dependencies**
List every attribute name the queries use. State that the GenAI conventions are in Development status and that `gen_ai.system` was renamed to `gen_ai.provider.name`; give the one-line change needed if an instrumentation library still emits the old name.

**Open questions**
At most three questions whose answers would change a panel or a threshold (retention, sampling, which model is the default, whether tool durations are measured or estimated).

Rules: use only attributes and numbers present in the pasted data or named above; do not invent metric names, models or prices; prefer per-model and per-tool breakdowns over totals; keep it under 500 words.
