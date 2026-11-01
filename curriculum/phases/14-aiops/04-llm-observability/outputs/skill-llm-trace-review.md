---
name: llm-trace-review
description: Read the trace of an agent run (OTLP JSON, Jaeger, Tempo or agenttrace view) to find where the tokens, time and cost went, decide whether the run was healthy, and turn the numbers into alerts before the invoice does
version: 1.0.0
phase: aiops
lesson: aiops/llm-observability
tags: [aiops, observability, opentelemetry, llm, agents, cost, tracing, alerting]
---

# LLM trace review

Use this when an agent run cost more than expected, took longer than expected, ended without a result, or before you set budgets and alerts for an agent that is about to run unattended. It assumes spans with the OpenTelemetry GenAI attribute names (`gen_ai.operation.name`, `gen_ai.request.model`, `gen_ai.usage.input_tokens`, `gen_ai.usage.output_tokens`, `gen_ai.response.finish_reasons`, `gen_ai.tool.name`), which are in Development status: check the names your instrumentation emits before trusting a saved query.

## 1. Get the run as a tree

```bash
python3 agenttrace.py view run.otlp.json                                  # from the agent's --trace and --json files
curl -s 'localhost:16686/api/traces?service=triage-agent&limit=1'         # Jaeger: the same spans as JSON
# Tempo / Grafana Explore: { resource.service.name = "triage-agent" && span.gen_ai.operation.name = "invoke_agent" }
```

You need the root span (`invoke_agent`), one `chat` span per model call and the `execute_tool` spans under the turn that requested them. If any level is missing, the instrumentation is incomplete; say so in the report rather than guessing.

## 2. Account for the time

Write down: root duration, the sum of `chat` durations, the sum of tool durations, and the remainder. Flag estimated tool durations (`triage.duration_estimated`, shown as `~`): they are a share of the remainder, not a measurement.

| What dominates | Read it as | Check |
|---|---|---|
| chat spans, roughly equal | normal model latency for that model and prompt size | compare p95 of `chat` duration for the same model over the week |
| chat spans growing step by step | the conversation grows; every step re-sends the whole history | input tokens per step should grow the same way; consider summarising tool results |
| one chat span far above the others | long output (`output_tokens` high) or a slow turn at the provider | finish reason `max_tokens`? output tokens? time of day? |
| tool spans | slow `kubectl`, a hung API, a timeout | the tool's own `status`, `error.type`, and the detail text |
| the remainder | time the loop spent outside both: approvals, retries, unmeasured work | instrument it; a human approval gate often lives here |

## 3. Account for the tokens and the money

```bash
python3 agenttrace.py metrics runs/*.otlp.json          # mean and p95 cost per run, tokens per run, p95 latency per model
```

Per step, input tokens should rise by roughly the size of the previous tool results; a jump of thousands of tokens means a tool returned something large (a full log, a whole manifest). Cost per step is `input × price_in + output × price_out`; cache reads are cheaper, so a run with no cache reads on a stable system prompt is wasting money. Compare the run's cost with the p95 of its peers; a single expensive run is an incident, a rising p95 is a regression (new prompt, bigger tool output, a model change).

## 4. Account for the outcome

- Root status `OK` with `triage.stop = report`: the run did its job. Anything else is a finding.
- `stop = max_steps` or `max_tokens`: the loop hit a budget. Look for repeated `execute_tool` spans with the same tool name and growing arguments: a loop.
- `execute_tool` spans with status `ERROR` and `error.type = refused`: the model asked for something the loop forbids (unknown tool, out-of-scope namespace, disabled action). One or two in a run is the policy working; many means the prompt and the tool surface disagree.
- `error.type = error`: the tool itself failed. Read `status.message`.
- `refusal` in a chat span's finish reasons, or `stop = refusal`: a safety classifier declined. Count it; do not treat it as a server error.
- `triage.violations` above zero: the model attempted something outside policy. Quote the span.

## 5. Decision table

| Pattern in the trace | Likely cause | Fix |
|---|---|---|
| same tool, growing `tail_lines` or `limit`, ends in `max_steps` | the model cannot find what it wants and keeps widening the search | cap the tool's arguments server-side; tell the model the budget in the prompt; add a digest tool |
| input tokens double every step | huge tool results re-sent each turn | truncate tool results in the loop; return summaries, not raw logs |
| many `refused` spans | prompt promises tools that do not exist, or scope is wrong | fix the prompt or widen the scope deliberately; never by weakening the server |
| chat p95 up, tokens flat | provider latency or a model change | compare `gen_ai.response.model`; check the provider status page |
| cost p95 up, steps flat | longer outputs or a pricier model | `gen_ai.request.model` per run; `output_tokens` per step |
| runs without report rising | new failure class the agent cannot handle, or budgets too tight | read three such runs end to end; adjust the method in the prompt, not the budget first |

## 6. Alerts to set

| Signal | Where it comes from | Start with |
|---|---|---|
| cost per run, p95 | root `triage.cost_usd` or tokens × price | 1.5 × last week's p95 |
| tool calls per run, p95 | count of `execute_tool` spans per trace | half the loop's `max_tool_calls` |
| runs without a report | root status not `OK` | any, if the agent is unattended |
| refusal rate | `refusal` in finish reasons | above 2% over an hour |
| chat latency per model, p95 | `chat` span duration | 2 × the model's usual p95 |
| tool error rate | `execute_tool` spans with status `ERROR` over all tool requests | above 10% |

Every alert should link to this runbook and to the query that shows the offending traces.

## 7. Report

State: the trace id, the root duration and how it splits between model, tools and remainder (marking estimates), tokens and cost per run against the peer p95, the stop reason and root status, every `ERROR` span with its `error.type`, the pattern from the decision table that matches, the fix, and which alert would have caught it earlier. Do not report "the model was slow" without the model name, the span duration and the comparison number.
