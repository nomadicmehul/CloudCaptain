---
name: drift-response
description: Respond to a model drift or data-quality alert (FeatureDrift, PredictionDrift, DatasetDrift, FeatureMissingValues, DriftCheckStale) by proving which windows were compared, separating data bugs from real population shifts, checking label coverage, and deciding between fixing the pipeline, recalibrating, retraining or rolling back
version: 1.0.0
phase: mlops
lesson: mlops/drift-monitoring
tags: [mlops, drift, data-quality, psi, ks-test, wasserstein, evidently, prometheus, retraining, on-call]
---

# Drift response

Use this when a drift or data-quality alert fires for a model, when a weekly drift report shows red columns, or when someone says "the model got worse" and nobody deployed anything. A drift score says two windows differ; it never says why. Never retrain as the first step: retraining on broken inputs bakes the bug into the next model. Never silence a drift alert without writing down which cause you ruled in.

## 1. Confirm what was compared

```bash
P=http://prometheus:9090
curl -sG $P/api/v1/query --data-urlencode 'query=time() - minidrift_last_run_timestamp_seconds' | python3 -m json.tool
curl -sG $P/api/v1/query --data-urlencode 'query=minidrift_window_rows' | python3 -m json.tool
curl -sG $P/api/v1/query --data-urlencode 'query=minidrift_feature_drift_detected == 1' | python3 -m json.tool
curl -s http://drift-monitor:9108/report > report.json
```

Write down: the model and version in production, the reference window (which dataset version, and is it the training data of that model version?), the current window (time range, row count), the method and threshold per column, and when the check last ran. If `DriftCheckStale` fired, the job is broken: fix it first, everything else is stale. If the reference window is "last month" rather than a pinned version, the comparison moves under you; pin it before trusting any trend.

## 2. Rule out data bugs before drift

```bash
python3 -c 'import json; r = json.load(open("report.json")); [print(q["column"], q["issues"]) for q in r["quality"] if q["issues"]]'
python3 code/main.py report reference.csv current.csv --schema schema.json --json > fresh.json
```

| Finding | Check | Usually means | Action |
|---|---|---|---|
| missing ratio up in one or more columns | `minidrift_feature_missing_ratio{window="current"}` against `window="reference"` | upstream export, join or schema change | find the change (pipeline commits, upstream release notes); fix or roll back; backfill |
| type errors (`unknown`, empty strings, units in numbers) | `minidrift_feature_type_errors` | a producer changed format | fix the parser or the producer; add a contract test |
| values outside the reference range | `minidrift_feature_out_of_range_ratio` | unit change (cents vs euros, ms vs s) or a sentinel (`-1`, `999`) | compare a few raw rows with the same rows in the source system |
| new categories | `minidrift_feature_new_categories` | new product, region, client version, or a renamed value | map renamed values; add real new ones to the feature pipeline |
| several columns move together at one timestamp | drift gauges over time in Prometheus | a deploy of the feature pipeline, not a population change | correlate with deploys; roll back the pipeline |

If any row here explains the alert, stop: it is an incident in the data pipeline. Record it in the report and route it to the pipeline owner.

## 3. Characterise a real shift

```bash
python3 code/main.py report reference.csv current.csv --schema schema.json --num-method psi --cat-method psi
python3 code/main.py report reference.csv current.csv --schema schema.json --num-method ks --cat-method chisquare --json > pvalues.json
```

1. For each drifted column read the shift in its own units (`mean_reference` and `mean_current`, category shares), not only the score. "Income up 28%" is actionable; "PSI 0.30" is not.
2. Check the window size against the method: with more than a few thousand rows, p-value methods flag tiny shifts. Decide on distances (Wasserstein normalised, Jensen-Shannon, PSI) with thresholds calibrated on past weeks.
3. Segment: rerun the report on the biggest segment columns (plan, region, channel, client version). A shift confined to one segment points at a cause (a campaign, a partner, an app release).
4. Check the business calendar: launches, campaigns, pricing changes, holidays, seasonality. Compare with the same week last year if you have it.
5. Look at prediction drift: if the prediction distribution moved, the shift touches features the model relies on. If only predictions moved, compare the serving features with the training pipeline's features for the same rows (training-serving skew) and confirm the model version.

## 4. Check quality with the labels you have

```bash
python3 code/main.py report reference.csv current.csv --schema schema.json --labels labels.csv --json | python3 -c 'import json,sys; print(json.load(sys.stdin)["performance"])'
```

| Coverage of current window | Quality change | Reading |
|---|---|---|
| below 20% | any | `insufficient_labels`: early labels are a biased slice; report, do not act on the number alone |
| 20% or more | within tolerance | inputs moved but the model still copes; recalibrate thresholds or accept |
| 20% or more | down by more than the tolerance | the model is hurting users; go to step 5 now |
| any | no labels at all | rely on prediction drift and proxy metrics (complaints, overrides, conversion) |

## 5. Decide

| Situation | Decision |
|---|---|
| data bug (step 2) | fix or roll back the pipeline; do not retrain; add a quality rule that would have caught it earlier |
| real shift, quality fine | accept; move the reference window to a pinned newer dataset if the new population is permanent; recalibrate thresholds |
| real shift, quality down, labels for the new population exist | retrain on a window that contains the new population; evaluate against the old test set and a new one; roll out with a canary |
| quality down, no inputs moved | concept drift: retrain on recent labelled data; consider features that capture the change |
| quality down, cannot retrain quickly | roll back to an earlier model version if it does better on recent labels, or route the affected segment to a fallback (rules, a human queue) |
| a column drifts every week and nobody acts | the alert is noise: raise its threshold from history or drop it, and record why |

For text inputs to an LLM application, read `top_new_tokens` and the OOV rate first: new product names, error messages or a new language usually explain token drift; update retrieval content and the eval set before touching the model.

## 6. Prevent the repeat

- Add the failing quality rule to the pipeline (a contract test or an Evidently test suite) so the bug fails the batch, not the model.
- Version the reference window with the model: the training data's version from the run's lineage.
- Keep `promtool test rules` cases for the drift alerts in the repository and run them in CI.

## Report

```text
Model / version:          <name> <version>
Alert(s):                 <alertname> since <time>, severity <ticket|page>
Windows:                  reference <dataset version, rows>, current <range, rows>
Data-quality findings:    <column: finding> or none
Drifted columns:          <column: method score (threshold), shift in units>
Prediction drift:         <score> / none
Label coverage / quality: <coverage>, <metric ref -> cur> or insufficient
Cause (ruled in):         <pipeline bug | population shift | concept drift | threshold noise>
Ruled out:                <causes and the evidence>
Decision:                 <fix pipeline | accept and re-pin reference | retrain | roll back | recalibrate>
Follow-ups:               <owner, ticket>
```
