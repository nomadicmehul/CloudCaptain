---
name: api-integration-review
description: Review or write a script, job or service that calls an HTTP API: timeouts, retries classified by idempotency, idempotency keys, backoff with jitter, Retry-After and rate-limit headers, client-side throttling, circuit breaking, pagination and conditional requests, each rule proven against a fault server
version: 1.0.0
phase: scripting
lesson: scripting/api-automation
tags: [scripting, http, api, retries, idempotency, rate-limiting, pagination, circuit-breaker, urllib3, curl]
---

# API integration review

Use this when a script or service calls someone else's HTTP API (a SaaS, a cloud provider, an internal service), before a bulk job (migration, backfill, sync), or after an incident with duplicates, missing records, a stuck job or a rate-limit ban. The output is a list of findings, each with the call it concerns, the evidence and the fix, and a verification command for every rule. Commands use `apiclient` from the lesson (`python3 main.py`); the urllib3, httpx, requests and curl equivalents are named in each step.

## 1. Inventory the calls

List every distinct call: method, path, what it changes, how often it runs, how many items it touches, and the client library and its version. Find them in code:

```bash
grep -rn -E 'requests\.(get|post|put|patch|delete)|urlopen\(|httpx\.|urllib3|curl ' --include='*.py' --include='*.sh' .
```

Mark each call **read** (GET, HEAD), **idempotent write** (PUT, DELETE, or a POST the API documents as idempotent) or **non-idempotent write** (POST, PATCH). The rest of the review is per class.

## 2. Timeouts on every call

| Client | No timeout looks like | Fix |
|---|---|---|
| requests | `requests.get(url)` | `timeout=(3.05, 30)` on every call, or a `Session` wrapper that sets it |
| urllib.request | `urlopen(url)` | `timeout=` (one value for everything); split connect and read with `http.client` |
| urllib3 | `PoolManager()` without `timeout=` | `Timeout(connect=3.05, read=30)` |
| httpx | defaults are 5 s each | set `httpx.Timeout(read=..., connect=3.0)` to the real latency |
| curl | no `--max-time` | `--connect-timeout 3 --max-time N`, plus `--retry-max-time` with retries |

Add a total deadline for jobs with a window (a CronJob's `activeDeadlineSeconds`, a CI step timeout). Finding if any call can wait forever.

## 3. Classify retries

| Outcome | Read or idempotent write | Non-idempotent write without key | With an idempotency key |
|---|---|---|---|
| connection refused, connect timeout | retry | retry (nothing was sent) | retry |
| reset, read timeout, 502, 504 | retry | **do not retry**; reconcile (read back) | retry with the same key |
| 429, 503 with `Retry-After` | retry after that time | do not retry | retry after that time |
| 500 | retry a few times | do not retry | retry with the same key |
| 400, 401, 403 without rate-limit headers, 404, 409, 422 | do not retry | do not retry | do not retry |

Check the client's real behaviour, not its intent:

```bash
python3 main.py request --dry-run --json POST https://api.example.com/v1/orders
python3 main.py request --dry-run --json --auto-idempotency-key POST https://api.example.com/v1/orders
```

Known traps: `curl --retry` repeats POSTs on 5xx; `--retry-all-errors` repeats everything; a urllib3 `Retry` with `allowed_methods=None` retries every method; a generic `@retry` decorator around a function that POSTs retries it on any exception, including read timeouts.

## 4. Idempotency keys for writes that must be retried

If the API supports `Idempotency-Key` (Stripe and many payment, messaging and cloud APIs do), generate one UUID per logical operation, store it with the job's state before the first attempt, and send the same key on every attempt and after a restart. If the API does not support keys, make the write idempotent another way: a client-chosen ID (`PUT /orders/{id}`), a unique external reference the server rejects on repeat (`409`), or a read-back before retrying. Finding if a key is generated inside the retry loop.

## 5. Waits: backoff, Retry-After, rate-limit headers

- Backoff is exponential with **full jitter** (`random() * min(cap, base * 2^n)`), capped, with a bounded number of retries.
- `Retry-After` (seconds or HTTP date) is honoured; urllib3 does it only for 413, 429 and 503.
- `x-ratelimit-remaining: 0` with `x-ratelimit-reset` (epoch on GitHub) pauses the next call until the reset; nobody sends a request that is certain to fail.
- A wait longer than the job can afford (`max_wait`) ends the run with a clear error and the reset time, rather than sleeping for 40 minutes holding a slot.

Read the API's documentation for its headers and secondary limits (GitHub: 403 or 429 with `retry-after`, else wait until reset, else at least a minute).

## 6. Throttle and break

| Situation | Add |
|---|---|
| bulk job against a documented limit (5,000 per hour) | a token bucket at a rate below the limit, shared by all workers of the job |
| many workers or services calling one dependency | a circuit breaker per dependency (failures 5, open 30 s, one half-open trial) |
| several layers each retrying | retries in one layer only, or a retry budget |

## 7. Pagination and conditional requests

- Pages are followed by `Link: rel="next"` or the body's cursor until absent; never computed from a total and `per_page`; a repeated next link or cursor is an error; there is a `max_pages`.
- Long walks checkpoint the next URL or cursor so a crash resumes instead of restarting.
- Pollers send `If-None-Match` with the stored `ETag` (or `If-Modified-Since`) and treat `304` as "unchanged"; on GitHub a `304` to an authorized request does not count against the primary limit.

```bash
gh api --paginate 'repos/OWNER/REPO/issues?per_page=100&state=all' --jq '.[].number' > numbers.txt
wc -l < numbers.txt
python3 main.py paginate 'https://api.example.com/v1/items?per_page=100' --json > items.json
```

## 8. Logs

Each attempt logs method, URL without secrets, attempt number, status or error kind, duration, the decision and its reason, the wait, and the remaining rate limit. Tokens and request bodies with personal data are never logged.

## 9. Prove each rule

Run the lesson's fault server and replay the cases that matter for this integration:

```bash
python3 main.py serve --port 8080 &
U=http://127.0.0.1:8080
curl -s -X POST -d '{"plan": [{"status": 503}]}' $U/__plan/orders
YOUR_CLIENT POST $U/s/orders                                   # then:
curl -s $U/__hits/orders                                          # how many attempts arrived, with which Idempotency-Key
curl -s -X POST -d '{"plan": [{"status": 429, "headers": {"Retry-After": "2"}}]}' $U/__plan/limited
curl -s -X POST -d '{"plan": [{"reset": true}]}' $U/__plan/reset
curl -s -X POST -d '{"plan": [{"delay": 5}]}' $U/__plan/slow
```

Point the client under review at `$U/s/<name>` (a base URL setting or an environment variable) and compare the hits with the table in step 3. Pagination: `$U/items?per_page=100` must yield 23 items although the server caps pages at 10.

## 10. Report

State: the inventory (calls, class, volume); for each call its timeouts, retry rule, idempotency mechanism, waits and pagination; findings ordered by impact (duplicate writes first, then data loss from pagination, then hangs, then rate-limit abuse, then load amplification), each with file and line, evidence (the `__hits` count or log line) and the fix; the verification command and observed result for each fixed rule; and open risks (an API without idempotency support, undocumented limits, several processes sharing one budget without coordination).
