# Observability & SRE

*A metrics store, a log pipeline and a tracer, then Prometheus, Grafana, OpenTelemetry, SLOs and incident response.*

Every on-call question is a query: is it broken for users, since when, where, and what changed. The tools that answer it (a Prometheus graph, a LogQL search, a trace waterfall, a burn-rate alert, an incident timeline) are only as good as your model of what they store and how they compute. A `rate()` that hides a counter reset, a label that explodes cardinality, a log shipper that drops lines on rotation, a trace that breaks at a queue and an alert that pages for nothing all come from mechanisms you can build in an afternoon. This phase builds each one from the standard library first, then runs the real tool on the same data and compares the answers.

## How this phase is built

| Lesson | Type | What you build | What you then use |
|---|---|---|---|
| [Metrics](01-metrics-tsdb/docs/en.md) | Build | `minitsdb`: the exposition format, a scraper with relabelling and staleness, a TSDB with Gorilla-compressed chunks and a cardinality report | Prometheus, `promtool tsdb analyze`, node_exporter |
| [PromQL](02-promql/docs/en.md) | Build | `miniql`: selectors, `rate` with resets and extrapolation, aggregation, vector matching, `histogram_quantile` | the query API, recording rules, `promtool test rules` |
| [Alerting](03-alerting/docs/en.md) | Build | `minialert`: rule evaluation with `for` and `keep_firing_for`, an Alertmanager-style routing tree with grouping, inhibition and silences | Alertmanager, `amtool config routes test` |
| [Logs](04-log-pipeline/docs/en.md) | Build | `minilog`: rotation-safe tailing, multiline, parsers, label-indexed chunks and a LogQL subset | Loki, Grafana Alloy, the OpenTelemetry filelog receiver |
| [Tracing](05-tracing/docs/en.md) | Build | `minitrace`: W3C Trace Context across three services, OTLP export, head and tail sampling, critical path | OpenTelemetry Collector, Jaeger |
| [SLOs](06-slo-burn-rate/docs/en.md) | Build | `minislo`: error budgets, multiwindow multi-burn-rate alerts, generated recording and alerting rules | Sloth or Pyrra, OpenSLO, `promtool` |
| [Incidents](07-incident-response/docs/en.md) | Build | `miniincident`: incidents from alert webhooks, roles, on-call schedules, escalation, timeline, MTTA/MTTR and a postmortem draft | Alertmanager webhooks, PagerDuty's event shape |
| [Chaos](08-chaos-experiments/docs/en.md) | Build | `minichaos`: experiments with a steady-state hypothesis, faults, abort conditions and rollbacks | Chaos Mesh, `tc netem`, game days |


## Reference layer

The long-form guides for this phase live under [Observability](https://cloudcaptain.io/docs/tools/observability) and the [SRE learning path](https://cloudcaptain.io/docs/learning-paths/sre). The AIOps phase builds on this one: [LLM observability](https://cloudcaptain.io/learn/aiops/llm-observability) traces agents with the same OpenTelemetry model.
