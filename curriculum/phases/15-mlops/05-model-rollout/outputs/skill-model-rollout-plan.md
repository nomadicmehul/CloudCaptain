---
name: model-rollout-plan
description: Plan and review the rollout of a new model version (shadow, sticky canary, per-version quality, latency and error analysis with minimum samples and effects, registry alias on promotion) and check an existing Rollout, AnalysisTemplate, HTTPRoute or InferenceService against the failure modes that let a wrong model through
version: 1.0.0
phase: mlops
lesson: mlops/model-rollout
tags: [mlops, model-serving, canary, shadow, argo-rollouts, gateway-api, kserve, mlflow, statistics, progressive-delivery]
---

# Model rollout plan

Use this when a new model version (retrained weights, a new architecture, a new prompt or feature pipeline behind the same endpoint) is about to replace the one in production, or when a pull request changes a `Rollout`, an `AnalysisTemplate`, an `HTTPRoute`, an `InferenceService` or a router config that serves models. The output is a plan with numbers, not an opinion. Do not approve a plan whose only gates are HTTP errors and latency: a wrong model answers 200 quickly.

## 1. Collect the inputs

Ask for, or read from the repository and dashboards:

| Input | Where | Why |
|---|---|---|
| requests per second at the quietest hour the rollout may run | Prometheus, load balancer metrics | sample sizes are divided by this |
| the user or entity key requests carry (user ID, account, session) | API schema, router logs | stickiness and feedback attribution need it |
| quality signals and their delay: agreement with the current model, feedback or conversion rate, ground-truth labels | product analytics, label pipeline | decides which steps can judge quality and how long they last |
| baseline quality of the current model on each signal | last 7 days, per signal | input to the sample-size calculation |
| the smallest drop worth acting on, per signal (product decision) | owner of the model | the minimum effect; without it every large rollout rolls back on noise |
| side effects of a prediction call (writes, tool calls, emails, impressions) | code review | decides whether a shadow is safe and what `X-Shadow` must disable |
| traffic layer: plain Service, `HTTPRoute`, mesh, KServe Serverless or RawDeployment, a custom router | manifests | decides whether exact weights, mirroring and stickiness are available |
| registry and alias the serving code loads (`models:/name@champion`) | serving config | promotion must move it; rollback must not |

## 2. Compute the sample sizes and step durations

```bash
python3 minirollout.py plan plan.json --rps RPS --baseline-quality BASELINE --max-quality-drop DROP --window WINDOW_SECONDS
python3 minirollout.py plan rollout.json --rps RPS --baseline-quality BASELINE --json     # an Argo Rollouts Rollout works as a plan
```

By hand, per side, one-sided, 80% power: `n = (z_alpha * sqrt(2 * pbar * (1 - pbar)) + z_beta * sqrt(p1 * (1 - p1) + p2 * (1 - p2)))^2 / d^2` with `p1` the baseline, `p2 = p1 - d`, `pbar` their mean, `z_alpha` 2.326 for alpha 0.01, `z_beta` 0.842. A step at weight `w` needs at least `n / (rps * w)` seconds of traffic, and the analysis window must be at least that long. Write the numbers into the plan.

## 3. Choose the steps

| Situation | Steps |
|---|---|
| prediction calls have no side effects, and the traffic layer can mirror | shadow at 100% with an agreement limit, then canary 5 to 25% with feedback analysis, then 50%, then promote |
| side effects that cannot be disabled by a header | no shadow; offline replay of logged requests against the candidate, then a small sticky canary |
| feedback arrives within minutes | canary steps gated on feedback rate, latency and errors |
| ground truth arrives in days (chargebacks, churn) | promote on the fast signals, keep a holdout cohort on the old model for the label delay, and re-check on labels; or keep the canary at a small weight for that long |
| low traffic (samples per step take hours) | shadow at 100% for agreement, longer windows, fewer steps; say in the report that feedback cannot be judged within the rollout |
| a model behind an LLM or generative endpoint | add the eval gate from the previous lesson before the shadow; use agreement only as a coarse filter, since two good generations rarely match exactly |

## 4. Write the gates

For each analysis step, every metric compares the candidate with the primary over the same window, filtered by model version:

| Metric | Test | Minimum effect (start here, then adjust) | Guard |
|---|---|---|---|
| latency | Mann-Whitney U, candidate greater; report median difference | 50 ms or 20% of the median, whichever is larger; plus an absolute p95 SLO | at least `n` successful requests per side |
| errors | two-proportion z-test, candidate greater | 1 to 2 points | at least `n` requests per side |
| quality during shadow | one-proportion test of disagreement against a limit | the disagreement a good retrain shows (measure it on the last two retrains) | at least `n` mirrored answers |
| quality during canary | two-proportion z-test on negative feedback | the product's minimum drop | at least `n` feedback answers per side |

With Argo Rollouts and the web provider, write both conditions with the sample guard, so too little data is Inconclusive (the Rollout pauses) rather than a pass or a fail:

```yaml
successCondition: result.candidate.feedback >= 150 && result.candidate.feedback_rate >= result.primary.feedback_rate - 0.10
failureCondition: result.candidate.feedback >= 150 && result.candidate.feedback_rate < result.primary.feedback_rate - 0.10
```

## 5. Check the plan against this table

| Check | How to verify | Problem if it fails | Fix |
|---|---|---|---|
| a quality signal gates at least one step | read the AnalysisTemplate metrics | a wrong model promotes with flat errors and latency | add agreement (shadow) or feedback (canary) |
| metrics are split by model version | the query or endpoint filters on a version label the stable pods do not have | the canary is averaged with the stable version and diluted by the weight | label responses and metrics with the model version, not the Service name |
| assignment is sticky | router config, or a cohort header set at the edge with header `matches` | one user sees two models; feedback cannot be attributed; inconsistent decisions | hash the user ID with a per-rollout salt |
| each step can reach its sample size | `minirollout plan` warnings | analyses hold forever or decide on noise | longer window, larger weight, or a shadow step |
| a minimum effect is set | thresholds in the template or `--max-*` flags | large rollouts roll back on insignificant differences | agree the smallest drop worth acting on with the model owner |
| repeated looks | the analysis interval and count | every look is another chance for a false rollback | small alpha, a minimum effect, or alpha split over the planned looks |
| web provider URL returns JSON | `curl -s URL \| python3 -m json.tool` from a pod in the controller's namespace | a non-JSON 200 body is recorded as Successful without evaluating conditions | fix the URL; assert a field that only a real answer has |
| shadow side effects | code review of the prediction path | the shadow sends emails, writes rows or calls tools twice | honour `X-Shadow`, or do not shadow |
| shadow cost | candidate replicas sized for 100% of mirrored traffic | the shadow queue drops copies (`mirror_dropped`) or the mirror backend throttles the cluster | mirror a percentage, scale the candidate |
| KServe canary mode | `serving.kserve.io/deploymentMode` | canary rollout only works in Serverless mode | use a router or Argo Rollouts in RawDeployment mode |
| registry action | the promotion step | the registry says v6 is champion while v7 serves | move `@champion` only on promotion; tag rolled-back versions |
| rollback path | who sets the weight to 0, how fast | the canary keeps serving while someone finds the runbook | automated abort (`failureLimit: 0` for quality) and a tested manual command |

## 6. Decide

| Finding | Decision |
|---|---|
| no quality gate, or metrics not split by version | block the rollout |
| a step cannot reach its sample size within its duration | block, or change window, weight or steps |
| shadow unsafe because of side effects, and no replay | ship with a smaller first canary step and say so in the report |
| all checks pass | approve, with the step table and durations |

## Report

```markdown
## Model rollout plan: <model> <current version> -> <candidate version>

**Verdict:** approve | approve with changes | block

### Inputs
| rps (quietest hour) | user key | quality signals and delay | baseline | minimum effects | traffic layer |
|---|---|---|---|---|---|

### Steps
| # | step | candidate share (live / mirrored) | samples needed per side | seconds to reach them | gate (metric, test, limit) |
|---|---|---|---|---|---|

### Exposure if the candidate is bad
Users and requests served by the candidate before the first quality gate can decide: <number>, from <rps> x <weight> x <seconds>.

### Findings
| check | result | evidence | fix |
|---|---|---|---|

### Registry and rollback
- promotion moves `<model>@<alias>` to version <n>; rollback tags version <n> `rollout=rolled-back`
- abort path: <automatic condition> / <manual command>

### After promotion
<delayed-label check, holdout cohort and its duration, or "none needed because ...">
```
