---
name: promql-review
description: Review a PromQL expression, recording rule or dashboard panel before it ships (counter versus gauge, rate before aggregation, window versus scrape interval, join cardinality, empty numerators, histogram buckets, result cardinality), and explain a panel that shows a spike, a flat line or nothing at all
version: 1.0.0
phase: observability
lesson: observability/promql
tags: [promql, prometheus, recording-rules, promtool, grafana, rate, histogram-quantile, vector-matching, observability]
---

# PromQL review

Use this in two situations: a pull request adds or changes a PromQL expression (a dashboard panel, a recording rule, an alert expression, an SLO query, a canary analysis), or a panel in production shows something the system cannot be doing: a spike after a restart, a rate that never moves, a ratio above 1, a quantile pinned to one value, or an empty graph. Never "fix" a query by editing it until the graph looks plausible; find the rule the engine applied, prove it with a query, then change the query and add a unit test.

## Part A: before merge

### 1. Collect the facts the query depends on

```bash
P=http://prometheus:9090
curl -sG $P/api/v1/metadata --data-urlencode 'metric=NAME'                    # type (counter, gauge, histogram) and help
curl -sG $P/api/v1/series --data-urlencode 'match[]=NAME' --data-urlencode "start=$(date -d -1h +%s)" | python3 -c 'import json,sys; print(len(json.load(sys.stdin)["data"]))'   # series behind the selector
curl -sG $P/api/v1/query --data-urlencode 'query=count by (__name__) ({__name__="NAME"})'
curl -sG $P/api/v1/targets | python3 -c 'import json,sys; print({t["scrapePool"]: t["scrapeInterval"] for t in json.load(sys.stdin)["data"]["activeTargets"]})'   # scrape interval per job
```

Write down, per metric in the expression: its type, its labels and how many values each has, its scrape interval, and whether the job restarts (Deployments, autoscaled pods, batch jobs).

### 2. Check the expression against this table

| Check | How | Problem if it fails | Fix |
|---|---|---|---|
| `rate()`, `increase()`, `irate()` only on counters | the metadata type, or a `_total`, `_count`, `_sum`, `_bucket` suffix; Prometheus adds a "might not be a counter" info to the API response | every decrease of a gauge is read as a reset: a positive "rate" for a shrinking queue | `delta()` or `deriv()` for gauges |
| rate before aggregation | no `rate()` over a recording rule or subquery that sums counters; no `rate(sum(...)[...:])` | one instance's restart becomes a reset of the sum: a spike of the whole previous total | `sum by (...) (rate(x[5m]))`; record that, never `sum(x)` of raw counters |
| window at least four scrape intervals | compare the `[range]` with the scrape interval | Prometheus 3 windows are left-open: `[1m]` at a 1 m scrape holds one sample and returns nothing; `[2m]` returns something only while both scrapes succeed | `[4 x interval]` or Grafana's `$__rate_interval` |
| empty numerator | run the numerator alone while the system is healthy | an error ratio has no 5xx series when nothing fails, so the division returns nothing and an alert or analysis cannot evaluate | `(sum(rate(errors[5m])) or vector(0)) / sum(rate(total[5m]))`, or a success ratio |
| both sides of a ratio have the same labels | `sum by (...)` with the same list on both sides, or an explicit `on (...)` | one-to-one matching on unequal label sets returns nothing | aggregate both sides to the same labels |
| the one side of a join is unique per key | `count by (KEY) (info_metric) > 1` over the last days | `found duplicate series for the match group`: the query fails during a rollout or relabel change | deduplicate the one side (`max by (KEY, LABEL)` only if that is still unique), or match on more labels |
| `group_left` copies only what you need | the label list after `group_left` | every copied label multiplies the result's cardinality downstream | copy the labels the panel or the alert routes on, nothing else |
| histogram quantile has buckets around the threshold | `count by (le) (NAME_bucket)` | `histogram_quantile()` interpolates inside one bucket and returns the highest finite bound when the rank falls in `+Inf` | add bucket edges at and above the SLO threshold |
| aggregation keeps `le` for quantiles | `sum by (le, ...)` before `histogram_quantile()` | without `le` there are no buckets: NaN or no result | always keep `le` in the `by` list |
| result cardinality | `count(EXPR)` | a panel or rule with thousands of output series (per pod, per path) is slow and its recorded series cost storage | aggregate away labels nobody routes on; `topk` for panels |
| `topk` in range queries | the panel type | `topk` picks per step, so a graph shows more than k series | use it for tables or instant queries; for graphs, record a stable selection |
| lookback on sparse series | the series' sample spacing versus 5 m | a series scraped every 10 m disappears between samples; a dead series lingers 5 m in backfilled or federated data | `last_over_time(x[15m])` for sparse series |

### 3. Prove it with numbers

```bash
python3 miniql.py query --series data.om --at +15m --explain 'EXPR'        # samples, resets, extrapolation, matches and dropped series
curl -sG $P/api/v1/query --data-urlencode 'query=EXPR' --data-urlencode 'time=2026-11-02T10:15:00Z'
curl -sG $P/api/v1/query_range --data-urlencode 'query=EXPR' --data-urlencode start=... --data-urlencode end=... --data-urlencode step=60
promtool check rules rules.yml
promtool test rules rules-test.yml
```

For a recording rule or an alert expression, add a `promtool test rules` case that contains the situation the review is about: an instance that restarts in the middle (`0+120x9 0+120x10`), a missing scrape (`_`), a series that disappears (`stale`), a second value of an info label. The test must fail on the old expression and pass on the new one.

### 4. Decide

| Finding | Decision |
|---|---|
| rate over a sum of counters, rate of a gauge, window under two scrapes | block: the numbers are wrong, not imprecise |
| join without a uniqueness guarantee on the one side | block for alerts and SLOs; allow for ad hoc panels with a note |
| empty numerator in an alert or analysis | block: the alert cannot fire or the canary aborts on Error |
| quantile threshold between two distant bucket edges | change the buckets first, or state the precision in the panel title |
| result cardinality over a few hundred series | ask for an aggregation or a recording rule |

## Part B: a panel shows something impossible

```bash
python3 miniql.py query --series exported.om --at TIME --explain 'EXPR'       # if you exported the raw series with the federation or the API
curl -sG $P/api/v1/query --data-urlencode 'query=EXPR' --data-urlencode "time=TIME"
curl -sG $P/api/v1/query --data-urlencode 'query=resets(COUNTER[30m])' --data-urlencode "time=TIME"
curl -sG $P/api/v1/query --data-urlencode 'query=COUNTER[10m]' --data-urlencode "time=TIME"      # the raw samples with their timestamps
```

| What you see | Meaning | Next |
|---|---|---|
| a spike right after a pod restart | a reset inside a sum: `rate()` of a summed series, or a counter that restarted at a non-zero value | find the recording rule or subquery that sums counters; `resets()` on the raw series shows the restart |
| a "rate" for a queue, temperature or pool size | `rate()` of a gauge | `delta()` or `deriv()`; check `/api/v1/metadata` |
| an empty panel for a new or slow job | window shorter than two scrapes, or a selector that matches nothing at that time | `count_over_time(x[RANGE])` must be at least 2; compare the window with the scrape interval |
| an empty panel when nothing is failing | empty numerator in a ratio | `or vector(0)`, or the success-ratio form |
| a series that stays flat for 5 m after a target died | lookback without a staleness marker (federation, remote read, backfill) | `up` and the target's last scrape time; expect it to vanish after 5 m |
| `found duplicate series for the match group` | the one side of a join is not unique at that time | `count by (KEY) (info) > 1` at that time names the duplicate |
| the quantile stuck at one value during an incident | the rank is in the `+Inf` bucket, or all observations are in one bucket | `sum by (le) (rate(x_bucket[5m]))` at that time; add bucket edges |
| a ratio above 1 | numerator and denominator selected different series, or windows differ | the same selector and window on both sides; `on (...)` |
| values change when you zoom | the step changed, and a short window inside `rate()` skips samples between steps | a window at least as long as the step; `$__rate_interval` |

## Report

State: the expression (or rule name) and where it is used; each metric's type, labels, scrape interval and restart behaviour; every check from the table that failed, with the query or test that proves it (value before and after, at a named evaluation time); the corrected expression; the `promtool test rules` case that now guards it; and the expected change on the panel or alert (for example "the 10:12 spike disappears; the rate stays at 2.6k req/s"). Never report "fixed" for a query change without a value at a fixed time that was wrong before and is right now.
