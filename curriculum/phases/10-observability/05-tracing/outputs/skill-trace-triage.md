---
name: trace-triage
description: Triage a slow or failing request from its distributed trace: find the trace or prove which sampler dropped it, check that it is one tree and locate the hop that broke propagation, read the critical path and self time per service, and report the cause with evidence
version: 1.0.0
phase: observability
lesson: observability/tracing
tags: [observability, tracing, opentelemetry, w3c-trace-context, sampling, critical-path, jaeger, debugging]
---

# Trace triage

Use this when a request is slow or failing and the per-service dashboards disagree about whose fault it is, when a trace in Jaeger or Tempo looks incomplete, or when someone says "there is no trace for that request". It works with any OpenTelemetry backend and with `minitrace analyze` on OTLP JSON files. Do not restart services or change sampling during triage: you will lose the traces you are looking for.

## 1. Find the trace, or find out why there is none

```bash
curl -s "http://JAEGER:16686/api/traces?service=frontend&operation=GET%20/checkout&minDuration=1s&limit=20"   # slow ones
curl -s "http://JAEGER:16686/api/traces?service=frontend&tags=%7B%22error%22%3A%22true%22%7D&limit=20"         # failing ones
python3 minitrace.py analyze traces.jsonl --json                                                                # from a collector file exporter
```

Take the trace ID from a response header, a log line (log the trace ID on every request) or the backend's search. If the request has no trace:

| Check | Command or place | Meaning |
|---|---|---|
| was it sampled at the root? | the edge's sampler config (`OTEL_TRACES_SAMPLER`, `OTEL_TRACES_SAMPLER_ARG`); the `traceparent` flags in a downstream log line | flags `00`: head sampling dropped it before it ran; nothing downstream recorded it either |
| did the tail sampler drop it? | Collector `tail_sampling` policies; `otelcol_processor_tail_sampling_*` metrics on the Collector's `:8888/metrics` | healthy and fast traces are kept only by the probabilistic policy; check `decision_wait` against the request duration |
| did it arrive at all? | exporter errors in the service logs; `otelcol_receiver_refused_spans*`; `rejectedSpans` in OTLP responses | network, auth, payload rejected, queue full (`BatchSpanProcessor` drops when full) |
| is the context propagated? | `traceparent` in the callee's access log for the same request | missing header: the trace exists but is split (step 2) |

## 2. Is it one tree?

```bash
python3 minitrace.py analyze trace.json --trace TRACE_ID          # prints "missing parent span(s)" and the roots
python3 minitrace.py analyze traces.jsonl --strict                 # exit 1 if any trace is not a single tree
```

One root with no parent and no missing parent IDs is healthy. Otherwise:

| Shape | Cause | Confirm | Fix |
|---|---|---|---|
| a `CLIENT` span with no `SERVER` child | the callee did not continue the trace: it is not instrumented, rejected the header (invalid `traceparent`, uppercase hex, all-zero ID) or exports elsewhere | search the callee's spans in the same time window; check its access log for `traceparent` | instrument the callee, fix the propagator (`OTEL_PROPAGATORS=tracecontext,baggage`), send its spans to the same backend |
| a span whose parent ID is missing | something between caller and callee forwarded the header but its own spans never arrived (a proxy, a mesh sidecar, an SDK exporting to another collector) | the missing ID is the proxy's span ID | export the proxy's spans to the same pipeline, or accept the gap and say so |
| many short traces whose root is an internal service | context lost across a thread pool, a queue or an async callback inside the caller | the root's `SERVER` or `CONSUMER` span has no parent; the caller's trace ends early | pass the context explicitly to the worker; for queues, put `traceparent` in the message and use a link when one consumer span handles many messages |
| a new trace at the edge with a link to the caller | the edge does not trust incoming context (by design) | the link's trace ID is the caller's | follow the link; this is correct for public endpoints |
| spans of one trace split between kept and dropped | tail sampling across several collector instances without trace-ID routing | each instance's kept set | put the `loadbalancing` exporter in front of the tail samplers |

## 3. Where did the time go?

```bash
python3 minitrace.py analyze trace.json --trace TRACE_ID --json | python3 -c 'import json,sys; t=json.load(sys.stdin)["traces"][0]; print(t["slowest_hop"]); print(t["critical_ms_by_service"]); print(t["self_ms_by_service"])'
```

Read the critical path, not the longest bar. The path is walked back from the root's end through the child that finished last, so a parallel branch that finished earlier is not on it and making it faster changes nothing. Then read self time per service: duration minus the time covered by children.

| Pattern | Read it as | Next step |
|---|---|---|
| one `INTERNAL` or `SERVER` span holds most of the critical path, with high self time | that service's own code or its local resource (a lock, a CPU-bound loop, a slow query in an un-instrumented driver) | profile that service; add internal spans around the suspect section |
| a `CLIENT` span much longer than its `SERVER` child | time on the wire or in a queue before the server started: connection pool exhaustion, DNS, TLS handshakes, a load balancer, retries | client pool metrics, `http.request.resend_count`, server-side queue metrics |
| a gap with no span between two children of the same parent | the parent did un-instrumented work or waited (a sleep, a lock, serial code between calls) | add spans to the parent; the gap is the parent's self time |
| sequential calls that could run in parallel | N round trips on the critical path | batch or parallelise; the path should then go through only the slowest call |
| children that start after the parent ended | fire-and-forget or async work clipped onto the path | decide whether it should be a link in its own trace instead |
| timestamps out of order between services | clock skew between hosts | trust durations within one service; compare client and server durations instead of absolute times |

## 4. What failed, and where did it start?

List every span with status `ERROR` and walk down to the deepest one: that is where the failure started; the rest are callers reporting it. Read its `exception` events (`exception.type`, `exception.message`) and `error.type`. Remember the HTTP rule: a 4xx is an error on the `CLIENT` span and unset on the `SERVER` span, so a failing request with no server-side error is usually a bad request, not a broken server. Failure and latency are separate questions: the failing branch is often not on the critical path.

## 5. Is this trace representative?

Note which tail-sampling policy kept it (errors, latency, probabilistic) and the head-sampling ratio. A trace kept only for being an error says nothing about typical latency; one kept by the probabilistic policy is a fair sample of normal traffic. Before generalising, find two more traces with the same shape, or check the span metrics (request rate and duration histograms per span name) for the same window.

## 6. Report

State: the trace ID and how it was found (or which sampler dropped it); whether it is a single tree, and if not, the hop where propagation broke with the evidence; the end-to-end duration and the critical path as service and span with milliseconds; self time per service; the deepest error span with its exception; which policy kept the trace and whether other traces confirm the pattern; the fix and who owns it. Never report "the api is slow" without the span name, its self time and its share of the critical path.
