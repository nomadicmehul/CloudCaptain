---
name: log-triage-with-llm
description: Use a model on production logs during an incident without pasting logs into it - fix the window and the deploy marker, cluster into templates, rank what is new or spiking, redact, budget the digest, ask with a structure and verify every claim against a template id and kubectl
version: 1.0.0
phase: aiops
lesson: aiops/log-analysis
tags: [aiops, logs, kubernetes, loki, redaction, llm, debugging, incident]
---

# Log triage with a model

Use this when a service is failing and the logs are too long to read, when someone is about to paste `kubectl logs` into a chat window, or when an AI assistant's log summary has to be checked before anyone acts on it. The model explains; a program counts. Never swap the two.

## 1. Fix the window and the marker

```bash
NS=payments; APP=checkout
kubectl rollout history -n $NS deploy/$APP                                   # which revision, when
kubectl get -n $NS deploy/$APP -o jsonpath='{.status.conditions[?(@.type=="Progressing")].lastUpdateTime}'; echo
kubectl get events -n $NS --sort-by=.lastTimestamp | tail -20                 # ScalingReplicaSet, Killing, BackOff: the change timeline
kubectl logs -n $NS -l app=$APP --all-containers --prefix --timestamps --since=30m --max-log-requests 20 > /tmp/$APP.log
kubectl logs -n $NS deploy/$APP --previous --timestamps > /tmp/$APP-prev.log 2>/dev/null || true   # the container that crashed
```

Choose a window that contains at least as much time before the change as after it. The marker is the deploy, config change or scale event, as a timestamp. Write both down; every later statement is relative to them.

## 2. Cluster before you read

```bash
python3 logmine.py /tmp/$APP.log --marker 2026-11-01T10:20:00Z --no-model --top 10 --budget 2000
```

Or in Loki: open Grafana Logs Drilldown, select the service, and read the Patterns tab (needs the pattern ingester). Or one template at a time:

```logql
sum by (level) (count_over_time({namespace="payments", app="checkout"} | logfmt [1m]))
{namespace="payments", app="checkout"} | pattern "<_> level=<level> msg=<msg>" | level="error"
```

Do not read raw lines yet. Read the template table: id, level, before/min, after/min, change, count, first and last seen.

## 3. Decide what matters

| Template is | Meaning | Do |
|---|---|---|
| NEW and ERROR or FATAL | appeared with the change | this is the story; read its two examples and the first-seen time |
| NEW and INFO or no level | something new started logging (a deploy line, a new code path, a stack-trace continuation) | attach it to the error it belongs to; it is evidence, not a second incident |
| SPIKE x5 or more on WARN or ERROR | an existing failure mode got worse | a downstream effect or the same cause; compare first-seen with the NEW template |
| GONE after the marker | a component went quiet | check whether a consumer, health check or cron stopped; silence is a symptom |
| steady ERROR, flat rate | background | note it in the report as pre-existing; do not let it become the hypothesis |
| flat INFO at high volume | request and health-check noise | ignore; if the model mentions it, that is a sign it was given raw lines |

If nothing is NEW or spiking, widen the window or move the marker; the change may be earlier than the deploy you were told about.

## 4. Redact before anything leaves

What never leaves the machine: raw log files, values of `password`, `token`, `secret`, `api_key`, `authorization`, `cookie` and similar keys, bearer tokens, email addresses, long opaque strings, and by default client IP addresses. `logmine` redacts templates and examples on the way into the digest; confirm with `grep -c -E 'eyJ|Bearer |@' digest.txt` before pasting anywhere. In the pipeline, redact at collection time: Vector `redact()`, Fluent Bit `modify` or `lua`, the OpenTelemetry Collector `redaction` processor. Lines that are already clean in storage cannot leak later.

## 5. Budget and cost

Tokens are about characters divided by four. Set the budget to what a human would read, 1500 to 3000 tokens, and let the ranking decide what fits. Record the two numbers `logmine` prints: raw tokens and digest tokens, with the input price of the model you use. If the ratio is below 20x, the window is too small or the service's output is not repetitive; both are findings.

## 6. Ask with a structure

Send the digest, not the file. Ask for exactly: headline, what changed, most likely cause, evidence as template ids, noise as template ids, next checks, confidence. `outputs/prompt-explain-log-digest.md` is that prompt; `logmine` without `--no-model` does the same through the `explain_digest` tool. Tell the model the marker is a deploy if it is, and name the component if you know it. Do not ask it to fix anything.

## 7. Verify

- Every template id in the answer must exist in the digest. `logmine` prints `WARNING references templates that are not in the digest` for the rest; treat those claims as unverified.
- Every count or time the answer states must match a row in the digest. An answer that says "hundreds of errors" when the row says 1883 after the marker is paraphrasing; an answer that says "since 10:14" when first-seen is 10:20 is inventing.
- Confirm the cause with the cluster, not with the model: `kubectl get configmap`, `kubectl describe deploy`, `kubectl diff` against the previous revision, `kubectl logs --previous`.

## 8. Report

State: the window and the marker; the top NEW, SPIKE and GONE templates with their before/after rates and first-seen times; the pre-existing background you excluded; the model's headline and cause with its confidence and the ids it cited; what you verified and how; the raw-versus-digest token numbers. Do not include raw log lines in the report; include redacted examples by template id.
