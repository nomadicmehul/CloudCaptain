---
name: slo-design
description: Design or review a service's SLO with its team (an event-ratio SLI measured where users feel it, an objective and window from real data, the error budget and burn-rate table, generated and unit-tested Prometheus rules, page and ticket routing, and a written error budget policy)
version: 1.0.0
phase: observability
lesson: observability/slo-burn-rate
tags: [slo, sli, error-budget, burn-rate, prometheus, sloth, pyrra, openslo, promtool, alertmanager, sre]
---

# SLO design and review

Use this when a team adds its first SLO, when a pull request changes an SLO spec or the rules generated from it, or when an SLO alert pages too often or too late. The output is a spec, a tested rule file, a routing change and a policy, not a dashboard. Do not pick the objective before you have looked at the service's own data, and do not ship rules that have not run through `promtool test rules`.

## 1. Name the journey and the owner

Write one sentence per SLO: who the user is, what they do, and what failing looks like to them ("a shopper submits an order and gets a confirmation", "the CI runner fetches a job within 30 s"). One SLO per journey; a service with five endpoints rarely needs five SLOs. Name the team that is paged and the person who owns the policy.

## 2. Choose the SLI as an event ratio

| Journey | SLI (bad over valid) | Where to measure | Watch out for |
|---|---|---|---|
| request/response API | responses with 5xx (and usually 429) over all responses | load balancer, ingress or gateway metrics, not the pod | 4xx are the caller's errors; health checks and synthetic probes must be excluded from `total` or labelled |
| latency | requests slower than the threshold over all requests | the same histogram at the edge | a bucket must exist exactly at the threshold (`le="0.3"`); otherwise `histogram_quantile` guesses |
| asynchronous work | jobs that fail or exceed their deadline over jobs started | the queue or the worker's completion counter | jobs that never start are invisible to a worker counter; count enqueued jobs |
| freshness | reads that see data older than N minutes over all reads | the reader, or a probe | a stopped pipeline produces no events at all: add a probe or an `absent()` alert |
| low traffic (under ~100 events/hour) | as above, plus synthetic requests at a known rate | a prober | one failure is a huge ratio; use longer windows or group endpoints |

Check that every query returns a number now, not an empty vector:

```bash
PROM=http://prometheus:9090
curl -sG $PROM/api/v1/query --data-urlencode 'query=sum(rate(http_requests_total{job="checkout",code=~"5.."}[5m]))'
curl -sG $PROM/api/v1/query --data-urlencode 'query=sum(rate(http_requests_total{job="checkout"}[5m]))'
```

An empty numerator (no 5xx series yet) makes the recorded ratio empty, not 0. That is fine for alerts and confusing on dashboards; say so in the review.

## 3. Pick the objective and window from the data

```bash
curl -sG $PROM/api/v1/query --data-urlencode 'query=1 - sum(increase(http_requests_total{job="checkout",code=~"5.."}[28d])) / sum(increase(http_requests_total{job="checkout"}[28d]))'
curl -sG $PROM/api/v1/query --data-urlencode 'query=min_over_time((1 - sum(increase(http_requests_total{job="checkout",code=~"5.."}[7d])) / sum(increase(http_requests_total{job="checkout"}[7d])))[28d:1d])'
```

Set the objective at or slightly below what the service achieved in its worst normal week, never above what users get today, and never 100%. Prefer a rolling 28d or 30d window for alerting; a calendar month only for reporting, and know that Prometheus rules are rolling either way. Compute the budget and the table:

```bash
python3 minislo.py validate slo.yaml
python3 minislo.py table slo.yaml
```

## 4. Write the spec

Sloth `prometheus/v1` (also read by minislo), with labels that route and a runbook link:

```yaml
version: "prometheus/v1"
service: "checkout"
labels: {owner: "payments"}
slos:
  - name: "requests-availability"
    objective: 99.9
    sli:
      events:
        error_query: sum(rate(http_requests_total{job="checkout",code=~"5.."}[{{.window}}]))
        total_query: sum(rate(http_requests_total{job="checkout"}[{{.window}}]))
    alerting:
      name: CheckoutAvailabilityBudgetBurn
      labels: {team: payments}
      annotations: {runbook_url: "https://runbooks.example.com/checkout/availability"}
      page_alert: {labels: {severity: critical}}
      ticket_alert: {labels: {severity: warning}}
```

For Pyrra or an OpenSLO-based tool, translate it field by field and keep the same objective, window and queries.

## 5. Read the alert table as questions

| Question | Where to read it | Acceptable answer |
|---|---|---|
| time to page at a full outage | `table`, row page 1h/5m, column "fires after (100% errors)" | about a minute for 99.9%; minutes for 99% |
| smallest error rate that ever pages | `table`, page row error ratio (6h row) | the team agrees users would call this an incident |
| does a slow leak open a ticket within a working day | `table`, ticket rows; `simulate` with a 2x to 5x phase | yes |
| how long a fixed incident keeps paging | the short windows (5m, 30m) | minutes |
| does one failure page at night traffic | events per hour at the quietest hour x budget | at least ~100 events in the 5m window, or synthetic traffic |

## 6. Generate and test the rules

```bash
sloth generate -i slo.yaml -o rules/checkout-slo.yaml               # or: python3 minislo.py generate slo.yaml -o ...
python3 minislo.py inspect rules/checkout-slo.yaml                  # windows, thresholds, burn factors
P="docker run --rm --user $(id -u):$(id -g) -v $PWD:/work -w /work --entrypoint /bin/promtool prom/prometheus:v3.15.1"
$P check rules rules/checkout-slo.yaml
python3 minislo.py test-file slo.yaml --scenarios scenarios.yaml --rules rules/checkout-slo.yaml -o rules/checkout-slo-test.yaml
$P test rules rules/checkout-slo-test.yaml
```

The scenarios file must contain at least a fast burn (20x for an hour after days of normal traffic), a slow burn (3x to 5x for a day), no burn, and a replay of the service's last real incident. Commit the spec, the generated rules and the test; CI regenerates the rules and fails on a diff.

## 7. Route

Page on `sloth_severity="page"`, ticket on `sloth_severity="ticket"`, group by `alertname`, `sloth_id` and `sloth_severity`, and put the runbook URL in the annotation. Delete the old threshold alerts the SLO replaces in the same change, or the team is paged twice.

## 8. Write the error budget policy

One page, agreed by the team and the product owner: what happens above 25% left (ship, spend it on experiments and migrations), between 0 and 25% (canaries only, no risky launches), at 0 (freeze features; reliability fixes and security patches only, until the budget is positive), and who can grant an exception. Gate deploys on it:

```bash
python3 minislo.py budget slo.yaml --total "$TOTAL_EVENTS" --bad "$BAD_EVENTS" --elapsed 12d || echo "release blocked by the error budget policy"
```

## 9. Review findings

| Finding | Problem | Fix |
|---|---|---|
| SLI measured on the pod | requests that never reach a pod (crash loop, bad ingress) are not counted | measure at the load balancer or gateway |
| 4xx counted as errors | a client bug burns your budget | count 5xx and 429 only |
| objective 99.99% for a service at 99.9% today | permanently exhausted budget, policy ignored | set it from the data; tighten later |
| single-window alert on the error ratio | pages on blips, misses leaks | burn-rate alerts with long and short windows |
| `for: 10m` on a burn-rate alert | delays the page; the long window already is the duration | remove `for` |
| a policy reading `slo:period_error_budget_remaining:ratio` on a new service | it projects the burn rate over the data it has, not the period | count events, or wait for a full window of data |
| no unit test | thresholds and windows unverified | `minislo test-file` + `promtool test rules` in CI |
| alert without `runbook_url` | the pager has no first step | add it in `alerting.annotations` |

## Report

State: the journey and owner; the SLI queries and where they are measured; the objective, window and budget (events and full-outage minutes); the burn-rate table with time to page at 100% errors and the smallest error rate that pages; the scenarios tested and their results (`promtool test rules` output); the routing; the policy levels and who signed them; and every finding from section 9 with the change made. Never report "SLO added" without the test run and the policy.
