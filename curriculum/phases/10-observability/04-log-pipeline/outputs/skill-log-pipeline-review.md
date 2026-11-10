---
name: log-pipeline-review
description: Review a log pipeline (Alloy, Fluent Bit, OpenTelemetry Collector filelog or minilog into Loki) for lost and duplicated lines, broken multiline entries, wrong timestamps and label cardinality, and report each finding with evidence and a fix
version: 1.0.0
phase: observability
lesson: observability/log-pipeline
tags: [observability, logging, loki, alloy, fluent-bit, opentelemetry, logrotate, cardinality, logql]
---

# Log pipeline review

Use this before a new service's logs go to production, after an incident where "the logs were missing" or "every line is there twice", or when Loki queries get slow and ingesters use more memory every week. It works on the shipper configuration, the host's rotation settings and the store's API. Do not change rotation or restart shippers during the review; you need the current behaviour as evidence.

## 1. Inventory the sources

For every file the shipper reads, record: the glob, the rotation tool and mode, the line format, and where the shipper keeps its positions.

```bash
grep -rE 'copytruncate|create|compress|delaycompress|rotate|size|daily' /etc/logrotate.conf /etc/logrotate.d/
ls -li /var/log/app/                                # inode numbers: rename keeps them, copytruncate does not create new ones for the live file
grep -nE '__path__|path_targets|include|exclude|Path|DB|storage|positions' config.alloy fluent-bit.conf otelcol.yaml 2>/dev/null
```

| Check | Expectation | Finding if not |
|---|---|---|
| rotated name in the glob | `app.log*` (or `include: [app.log, app.log.1]`) | with copytruncate, unread bytes at rotation are lost; with rename, lines written after the last read are lost |
| compressed rotations excluded | `.gz`, `.zst`, `.bz2` not matched | binary garbage in the store, or one huge entry |
| positions on persistent storage | Alloy `--storage.path`, Fluent Bit `DB`, Collector `storage` extension on a volume | every restart re-reads (or, with `start_at: end`, skips) whole files |
| rotation by rename where the app can reopen | `create` plus a reopen signal, or the app rotates itself | copytruncate has an unavoidable window in which new lines are in neither file |

## 2. Prove nothing is lost or duplicated

Pick a line field that increases (a sequence number, a request counter) or count lines at the source, then compare with the store over a window that includes a rotation.

```bash
wc -l /var/log/app/app.log /var/log/app/app.log.1
logcli query --since 2h --limit 100000 --output raw '{app="checkout"}' | wc -l
python3 minilog.py query --url http://LOKI:3100 'sum(count_over_time({app="checkout"}[2h]))' --instant
logcli query --since 2h --limit 100000 --output raw '{app="checkout"}' | sort | uniq -d | head
```

| Symptom | Cause | Fix |
|---|---|---|
| fewer entries than lines, gap right at a rotation | copytruncate with the rotated file outside the glob, or a shipper that was down longer than the retention of rotated files | widen the glob, rotate by rename, keep more rotated files |
| every line of a rotated file twice | positions keyed by path, or a copy read from the start, and timestamps taken at read time | extract the timestamp from the line; key positions by inode or fingerprint |
| duplicates after each shipper restart | positions not persisted, or committed before delivery | persist positions; check the shipper's restart behaviour with a test file |
| `entry too far behind` or `timestamp too old` in shipper logs | shipper catching up after downtime, or a wrong timestamp format | drop old entries in the shipper (`stage.drop` with `older_than`), fix the format |

## 3. Check entries, not just lines

```bash
logcli query --since 1h --limit 5 '{app="payments"} |= "Traceback"'
logcli query --since 1h --limit 5 '{app="payments"} |~ "^\\s+File \""'          # continuation lines stored as their own entries
logcli query --since 1h --limit 20 --output jsonl '{app="checkout"}' | head
```

- Multiline: a stack trace must be one entry. If continuation lines appear on their own, add `stage.multiline` (`firstline`), `multiline.parser` (Fluent Bit) or `multiline.line_start_pattern` (Collector), and check `max_wait_time` is shorter than the batch delay.
- Timestamps: compare the entry timestamp with the time in the line. A constant offset is a timezone in the format; equal timestamps for many lines mean the stage failed and the read time was used.
- Parsing: run the query with the parser and `| __error__!=""`; any result is a line the parser cannot read.

## 4. Measure label cardinality

```bash
logcli series --since 1h --analyze-labels '{app=~".+"}'
python3 minilog.py cardinality --url http://LOKI:3100 --match '{app=~".+"}' --since 1h
curl -s http://LOKI:3100/metrics | grep -E 'loki_ingester_memory_streams|loki_discarded_samples_total'
```

| Label | Keep as label? | Instead |
|---|---|---|
| `namespace`, `app`, `container`, `cluster`, `env`, `level` | yes: few values, stable, used in every query | |
| `pod`, `filename` | acceptable when bounded by deployment size; watch churn | drop `filename` when one app writes several files |
| `user_id`, `trace_id`, `request_id`, `session`, IP, URL path, status code per endpoint | no | structured metadata (`stage.structured_metadata`) or keep in the line and filter |
| anything with more than about 100 values per app, or values that never repeat | no | as above |

Loki refuses pushes beyond `max_label_names_per_series` and the per-tenant stream limits (`loki_discarded_samples_total` by `reason`). Fix the shipper, not the limit.

## 5. Check delivery

- Retries: 429 and 5xx retried with backoff, other 4xx logged and not retried; check the shipper's metrics or logs for dropped batches.
- Buffering: how long can Loki be down before the shipper drops data? (Alloy `loki.write` queue and optional WAL, Fluent Bit `storage.type filesystem`, Collector `sending_queue` with storage.)
- Limits: a `limit` or `throttle` stage on noisy debug sources protects the store, but say explicitly which lines it drops.

## 6. Check the queries people will run

Take the five most common queries from dashboards and alert rules and check each: a selector on low-cardinality labels, a time range no longer than needed, line filters before parsers, `| __error__=""` in metric queries, and `sum by` on labels that exist. Run each with `--stats` in `logcli` or read `stats.minilog` from `minilog` and note chunks and bytes processed.

## 7. Report

For each source: glob, rotation mode, positions location, and whether the loss and duplicate check passed with the numbers (lines at source, entries in store, duplicates found). Then each finding as: what, evidence (command and output), impact (lines lost per rotation, streams created per day, query time), fix and owner. List label changes as before and after stream counts. Never report "logs look fine" without the counts from step 2.
