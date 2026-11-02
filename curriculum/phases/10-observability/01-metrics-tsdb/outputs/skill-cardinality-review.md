---
name: cardinality-review
description: Find out why a Prometheus head is growing (memory, series count, WAL replay time), name the label and the target responsible, pick the cheapest fix and verify it with Prometheus's own counters
version: 1.0.0
phase: observability
lesson: observability/metrics-tsdb
tags: [prometheus, cardinality, tsdb, relabelling, metric-relabel-configs, sample-limit, promtool, oom]
---

# Cardinality review

Use this when Prometheus memory climbs, the process is OOM-killed, restarts take minutes (WAL replay), `prometheus_tsdb_head_series` jumps after a release, or someone proposes "more memory" or "shorter retention" as the fix. Retention deletes blocks on disk and does not shrink the head; more memory makes the next replay longer. Work from evidence: series counts per metric, per label and per target.

Set `PROM=http://127.0.0.1:9090` (port-forward if needed: `kubectl -n monitoring port-forward svc/prometheus-operated 9090`).

## 1. Measure the head and its growth

```bash
curl -s "$PROM/api/v1/query?query=prometheus_tsdb_head_series" | python3 -m json.tool
curl -s --get --data-urlencode 'query=delta(prometheus_tsdb_head_series[1h])' "$PROM/api/v1/query"
curl -s --get --data-urlencode 'query=rate(prometheus_tsdb_head_series_created_total[5m])' "$PROM/api/v1/query"
curl -s --get --data-urlencode 'query=process_resident_memory_bytes{job="prometheus"}' "$PROM/api/v1/query"
```

| What you see | Meaning | Next |
|---|---|---|
| head series flat, memory high | queries, not storage (large range queries, many rule groups) | check `prometheus_engine_query_duration_seconds` and rule evaluation time; not a cardinality problem |
| head series rose in a step at a known time | a release or a new target added labels | step 2, then match the time to a deploy |
| `head_series_created_total` rate high and steady, head series stable | churn: series are created and go stale constantly (pod names, short-lived jobs) | step 2, look for `pod`, `container_id` or per-request labels |
| head series growing without bound | an unbounded label (`user_id`, `path` with IDs, `email`, `ip`, `trace_id`) | step 2 now, step 4 before the next OOM |

## 2. Rank metrics, labels and label pairs

```bash
curl -s "$PROM/api/v1/status/tsdb?limit=20" | python3 -m json.tool > tsdb-status.json
python3 - <<'PY'
import json
d = json.load(open("tsdb-status.json"))["data"]
for key in ("seriesCountByMetricName", "labelValueCountByLabelName", "seriesCountByLabelValuePair"):
    print(key)
    for row in d[key][:10]:
        print(f"  {row['value']:>10}  {row['name']}")
PY
```

For a block-level view (churn over two hours), snapshot the head and analyse it. This needs `--web.enable-admin-api`; otherwise run `promtool tsdb analyze` against the data directory on the server:

```bash
SNAP=$(curl -s -X POST "$PROM/api/v1/admin/tsdb/snapshot" | python3 -c 'import json,sys; print(json.load(sys.stdin)["data"]["name"])')
kubectl -n monitoring exec prometheus-k8s-0 -c prometheus -- promtool tsdb analyze "/prometheus/snapshots/$SNAP" --limit 15
kubectl -n monitoring exec prometheus-k8s-0 -c prometheus -- rm -rf "/prometheus/snapshots/$SNAP"
```

Write down the top three metric names with their series counts, and the top three labels by value count. A label with thousands of values that is not `instance`, `pod` or `le` is the suspect.

## 3. Find the target and the series behind the suspect

```bash
M=<metric>; L=<label>
curl -s --get --data-urlencode "query=topk(10, count by (job) ({__name__=\"$M\"}))" "$PROM/api/v1/query"
curl -s --get --data-urlencode "query=count(count by ($L) ({__name__=\"$M\"}))" "$PROM/api/v1/query"
curl -s --get --data-urlencode 'query=topk(10, scrape_samples_post_metric_relabeling)' "$PROM/api/v1/query"
curl -s --get --data-urlencode 'query=topk(10, scrape_series_added)' "$PROM/api/v1/query"
curl -s http://<target>/metrics > page.prom
python3 minitsdb.py parse page.prom && python3 minitsdb.py lint page.prom
```

Read three example series of the suspect (`curl -s --get --data-urlencode "match[]=$M" "$PROM/api/v1/series" | head -c 2000`) to see what the values look like: IDs, raw URLs, timestamps or error messages in a label are always unbounded.

## 4. Choose the fix

| Situation | Fix | Config |
|---|---|---|
| a label with unbounded values on a metric you need | remove the label in the instrumentation; until the release, drop it at scrape time | `metric_relabel_configs: [{action: labeldrop, regex: user_id}]` (only safe if the remaining labels still make each series unique; otherwise the scrape reports duplicate samples) |
| a metric nobody queries (debug, per-object detail) | drop it before storage | `metric_relabel_configs: [{source_labels: [__name__], regex: "cc_debug_.*", action: drop}]` |
| raw paths with IDs (`/users/123`) | normalise to the route template in code, or `replace` with a regex until then | `{source_labels: [path], regex: "/users/[0-9]+", target_label: path, replacement: "/users/:id"}` |
| one target can explode at any release | cap it, so a bad release fails that scrape (`up` 0) instead of the whole server | `sample_limit: 50000`, `label_limit: 30`, `label_value_length_limit: 200` on that job |
| a needed breakdown that is too expensive to keep raw | a recording rule that aggregates away the label, then drop the raw series | recording rule `sum without (pod) (...)` plus a drop rule for the raw metric |
| per-user or per-request questions | not a metric: logs or traces, with an exemplar from a histogram bucket to one trace | instrumentation change |
| churn from pod or container IDs | keep them only on metrics that need them; `labeldrop` `container_id`, `image_id` elsewhere | `metric_relabel_configs` per job |

Never fix cardinality by lowering retention, raising memory alone, or adding a second identical Prometheus.

## 5. Verify

```bash
curl -s --get --data-urlencode 'query=scrape_samples_scraped{job="<job>"} - scrape_samples_post_metric_relabeling{job="<job>"}' "$PROM/api/v1/query"
curl -s --get --data-urlencode 'query=prometheus_tsdb_head_series' "$PROM/api/v1/query"
curl -s --get --data-urlencode 'query=up{job="<job>"}' "$PROM/api/v1/query"
```

The first query shows how many samples per scrape your rules now drop. The head series count falls only after the dropped series go stale and the next head compaction garbage-collects them (up to about two hours), so judge the trend, not the first minute. `up` must stay 1. If a `sample_limit` trips, `up` is 0 and the target's `lastError` in `/api/v1/targets` names the limit.

## 6. Report

State: head series before and after, with timestamps; memory before and after; the top three metrics and labels from step 2 as a table; the suspect label, its value count, example values and the job and commit that introduced it; the fix applied (the exact relabel rule or limit) and where it lives; the samples per scrape it removes; whether a code change is still needed and who owns it; and the guard added for next time (`sample_limit`, a lint check with `promtool check metrics` in CI, an alert on `delta(prometheus_tsdb_head_series[1h])`).
