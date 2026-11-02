---
name: rollout-review
description: Review an Argo Rollouts canary or blue/green definition before it merges (pod rounding, a query that selects only the new version, empty results, windows, exposure arithmetic), and triage a rollout that aborted, hangs or promoted a bad version
version: 1.0.0
phase: cicd
lesson: cicd/progressive-delivery
tags: [progressive-delivery, canary, blue-green, argo-rollouts, flagger, prometheus, promql, analysis, kubernetes, gitops]
---

# Rollout review

Use this in two situations: a pull request adds or changes a `Rollout`, an `AnalysisTemplate`, a Flagger `Canary` or the metrics they query; or a rollout in a cluster is `Degraded`, `Paused` for longer than expected, stuck `Progressing`, or finished while users report errors. Do not run `kubectl argo rollouts promote` or `retry` during triage until you know why the analysis decided what it decided; the AnalysisRun is the evidence.

## Part A: before merge

### 1. Read the plan as numbers

```bash
kubectl create --dry-run=client -o json -f rollout.yaml > rollout.json 2>/dev/null || yq -o=json rollout.yaml > rollout.json
yq -o=json analysis.yaml > analysis.json
python3 minirollout.py plan rollout.json --analysis analysis.json                  # pods per step, share by pods, resolved queries
python3 minirollout.py plan rollout.json --analysis analysis.json --replicas 2     # the smallest replica count this service runs at (HPA minReplicas)
```

Write down, per step: requested weight, new and stable pods, share by pods, and whether a traffic router makes the share exact. If `strategy.canary.trafficRouting` is absent, the share is per connection behind one Service: long-lived connections (gRPC, HTTP/2, websockets, connection pools) can put far more or far less than the weight on the new version.

### 2. Check every metric against this table

| Check | How | Problem if it fails | Fix |
|---|---|---|---|
| the query selects only the new version | the selector has a label the stable pods do not have (`version`, `rollouts-pod-template-hash` via `valueFrom.podTemplateHashValue: Latest`, a canary Service name) | stable traffic dilutes the signal by the weight: 8% errors at 10% weight looks like 0.8% | add the label to the instrumentation or the mesh metrics; never gate on a whole-service average |
| a healthy version returns a number | run the query against Prometheus while only the stable version serves | an error-ratio numerator with no 5xx series returns an empty vector: every measurement is an Error and a healthy canary aborts after `consecutiveErrorLimit` | use the success-rate form, or `(... or vector(0))` around the numerator; consider `isNaN(result[0])` for 0/0 |
| the window has data | `initialDelay` is at least the `rate()` window, or the window is short enough to fill | `rate()` needs two samples per series in the window; the first measurement sees no canary or a few seconds of it | set `initialDelay` to the window plus one scrape interval |
| the threshold matches the SLO | compare `successCondition` with the service's SLO and its normal error rate | a threshold tighter than normal noise aborts good releases; looser than the SLO burns error budget | base it on the stable version's last week, or compare canary with stable in one query |
| failure versus inconclusive | both `successCondition` and `failureCondition` set? | the band between them pauses the rollout (`InconclusiveAnalysisRun`) and needs a human | keep the band only when someone is on call to decide |
| limits | `failureLimit`, `consecutiveErrorLimit` (default 4), `count`, `interval` | `failureLimit: 3` with `interval: 5m` is twenty minutes of a broken canary | keep `failureLimit` at 0 or 1 for error rate; tolerate more only for noisy latency |
| provider | `address` reachable from the controller's namespace; auth configured | every measurement Errors; the rollout aborts for an infrastructure reason | test with `curl -G ADDRESS/api/v1/query --data-urlencode 'query=up'` from a pod in `argo-rollouts` |
| latency quantile precision | the histogram's bucket edges around the threshold | `histogram_quantile` interpolates inside a bucket; a 250 ms threshold between 100 ms and 500 ms buckets is a guess | add a bucket at the threshold |

### 3. Compute the exposure

```bash
python3 minirollout.py simulate rollout.json --analysis analysis.json --rps RPS --error-rate 0.1 \
  --plan 100 --plan "5%,analysis,20%,analysis,50%,analysis" [--replicas N --ready-delay POD_START_SECONDS]
```

By hand: requests that reach a broken version at weight w is roughly `rps x w x (pod start + initialDelay + failureLimit x interval)`. Compare the plan with a plain rolling update (`--plan 100`) and with a smaller first step. If the first step is above 10% of traffic for a user-facing service, ask why.

### 4. Strategy-level checks

| Item | Problem | Fix |
|---|---|---|
| no analysis, only `pause` steps | the rollout is just slower; nothing aborts | add an analysis step after each weight, or background analysis with `startingStep` |
| background analysis with `count` | it stops measuring after `count` and the later steps run unwatched | leave `count` out for background analysis, set `interval` |
| `pause: {}` without an owner | rollouts wait forever in `Paused` | a duration, or a named on-call step in the runbook |
| blue/green without `prePromotionAnalysis` | the preview is never tested before the switch | add it, and send test traffic to the `previewService` |
| `scaleDownDelaySeconds` or `abortScaleDownDelaySeconds` set to 0 | switching back needs new pods | keep the default 30 seconds or more |
| the Rollout is managed by Argo CD | Argo CD shows the Rollout as Synced while it is Degraded | Argo CD's Rollout health check reports Degraded; alert on health, and revert in Git |

## Part B: during an incident

```bash
NS=<namespace>; R=<rollout>
kubectl argo rollouts get rollout $R -n $NS
kubectl -n $NS get rollout $R -o jsonpath='{.status.phase}{"\n"}{.status.message}{"\n"}{.status.abort}{"\n"}{.status.currentStepIndex}{"\n"}'
kubectl -n $NS get analysisrun --sort-by=.metadata.creationTimestamp
kubectl -n $NS get analysisrun NAME -o jsonpath='{range .status.metricResults[*]}{.name}{" "}{.phase}{" "}{.message}{"\n"}{range .measurements[*]}{"  "}{.phase}{" "}{.value}{" "}{.message}{"\n"}{end}{end}'
kubectl -n $NS get events --field-selector involvedObject.name=$R --sort-by=.lastTimestamp
```

| What you see | Meaning | Next |
|---|---|---|
| `Degraded`, `RolloutAborted: ... Metric "X" assessed Failed due to failed (N) > failureLimit (M)` | the metric's condition said no N times | read the measured values; if they are real, revert in Git; if the threshold is wrong, fix the template and push a new revision |
| `assessed Error due to consecutiveErrors (5) > consecutiveErrorLimit (4)` | the provider was unreachable or the result was empty | `Error Message:` in the run names it; `no data returned from the metric provider` means the query selects nothing for this version |
| `Paused`, message `CanaryPauseStep` | an indefinite `pause` step | `kubectl argo rollouts promote $R -n $NS` after the agreed check |
| `Paused`, message `InconclusiveAnalysisRun` | a measurement fell between success and failure | decide: `promote` accepts, `abort` stops |
| `Progressing`, stuck at a `setWeight` step | the new pods do not become available | `kubectl -n $NS get pods -l rollouts-pod-template-hash=HASH`, then the pod events: image, probes, quota, scheduling |
| `Healthy`, but users see errors | the analysis did not see the failure | the query averaged both versions, the threshold was looser than the failure, or the failing path had no traffic during analysis |
| aborted, then `retry` aborts again | the template is still the bad revision | `undo` or a Git revert; `retry` only reruns the same revision |

After the fix, watch the next rollout of the same service step by step with `kubectl argo rollouts get rollout $R -n $NS --watch` and check that the new measurements are numbers, not Errors.

## Report

State: the Rollout and revision; the strategy and steps with requested weight, pods and real share per step; each metric's query, condition, limits and the measured values (or the reason there were none); the decision (promoted, aborted, paused) and whether it was right; the exposure in requests or minutes at each weight; the change you made (template, query, threshold, steps, a Git revert); and the follow-up that prevents the same blind spot. Never report "promoted manually" without the measurement that justified it.
