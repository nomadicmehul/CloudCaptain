---
name: eval-gate-design
description: Design and run a release gate for a model, prompt or provider change: a suite of contract cases, deterministic assertions before a calibrated judge, a paired McNemar comparison with a minimum effect, critical cases, cost and latency budgets, and a contamination check, wired into CI
version: 1.0.0
phase: mlops
lesson: mlops/llm-eval-gates
tags: [mlops, llmops, evals, release-gate, mcnemar, promptfoo, ci]
---

# Eval gate design

Use this before swapping a model, changing a system prompt or template, changing sampling settings, moving to another provider or server (vLLM, Ollama, a hosted API), or upgrading the serving stack. The output is a suite file, a baseline run, a gate policy and a CI job that blocks the change unless the candidate is not worse, keeps every critical case, and fits the budgets.

## 1. Write the suite from contracts and incidents

One case per thing that must hold. Start with what other systems parse, then what went wrong before, then what must never happen.

| Source | Case | Assertion |
|---|---|---|
| a downstream parser (a ticket bot, a JSON consumer) | the exact format it needs | `equals`, `regex` anchored with `^...$`, `json-schema` with `required` and `additionalProperties: false` |
| a fact with one right answer | the fact | `contains` or `numeric` with a tolerance |
| a safety rule (no secrets, no PII, no destructive one-liners) | the request that tempts it | `not-contains` plus a positive check, `"critical": true` |
| a past incident | the input that caused it, verbatim | whatever would have caught it |
| a quality only a reader can judge | the request | `rubric` with the decisive phrases quoted, last resort |

Rules: one behaviour per case; ids are slugs that say what is tested; every case has an assertion a program can run unless it truly cannot; mark critical only what would page someone or leak data. Keep the suite in the repository next to the prompt it tests.

## 2. Choose assertions deterministic first

- Exact where a machine reads the answer. `6443` and `The API server listens on 6443.` are different outputs to a parser.
- A JSON answer wrapped in a Markdown fence is not JSON. Decide whether the contract allows the fence and say so in the schema case, not in the judge.
- Brittle assertions show up as false regressions. When the gate rejects, read every pass-to-fail case before arguing; fix the assertion when the answer was right (`openssl x509 -dates` versus `-enddate`), then rerun both sides.
- A rubric quotes the phrases or facts that decide it. A rubric a human cannot apply in ten seconds will not be applied consistently by a model either.

## 3. Size the suite for the change you care about

The gate uses McNemar's exact test on the cases that flipped. At alpha 0.05 it needs at least 6 discordant cases even when they all go one way. Probability of catching a regression (power) by suite size:

| Drop to detect | Cases that flip either way | 20 cases | 50 | 100 | 200 | 400 |
|---|---|---|---|---|---|---|
| 20 points | 20% (all one way) | 0.20 | 0.95 | 1.00 | 1.00 | 1.00 |
| 10 points | 10% (all one way) | 0.01 | 0.38 | 0.94 | 1.00 | 1.00 |
| 10 points | 20% (15% down, 5% up) | 0.03 | 0.24 | 0.54 | 0.88 | 0.99 |
| 5 points | 10% (7.5% down, 2.5% up) | 0.00 | 0.06 | 0.24 | 0.54 | 0.87 |

A 20-case suite catches only disasters. Aim for 100 to 200 cases for a gate that guards a 10-point drop, and add samples (`--samples 3` to `5`) when the model runs at a nonzero temperature.

## 4. Set the policy in the suite

```json
"gate": {"alpha": 0.05, "min_effect": 0.05, "max_p95_latency_ms": 2000, "max_mean_cost_usd": 0.0005},
"pricing": {"candidate-model": {"input_per_mtok": 1.0, "output_per_mtok": 3.0}}
```

- `alpha`: 0.05 is the usual; lower it when many gates run per day and a false block is cheap to override.
- `min_effect`: the smallest drop the product owner would roll back for. Without it, a large suite turns half-point noise into a block.
- Budgets: p95 latency measured from the client, mean cost per call from the reported usage and your price table. Set them from the product's limits, not from the baseline's numbers.
- `--require-improvement` only for changes whose whole point is quality; a cost-saving change should pass with "no significant difference".

## 5. Check for contamination before trusting a score

```bash
python3 main.py contamination --suite suites/assistant.json --train data/finetune.jsonl --n 8 --threshold 0.5
```

A flagged case measures memory. Replace it with a new case of the same contract, never just delete it. Run this whenever the training or fine-tuning data changes. No overlap is not proof of no leak: paraphrases and translations pass the check.

## 6. Calibrate the judge before it gates

1. Two people label 100 to 200 outputs pass or fail against the rubrics; measure their agreement first.
2. Run the judge on the same outputs; compute agreement and Cohen's kappa against the human consensus, and the false-pass rate on critical cases.
3. Below a kappa of about 0.6, fix the rubric or move the case to a deterministic assertion.
4. Use a judge model different from the model under test, at temperature 0, with a constrained JSON answer. Recalibrate when the judge model, its version or a rubric changes.

## 7. Run it in CI

```bash
python3 main.py run --suite suites/assistant.json --base-url "$EVAL_BASE_URL" --model "$BASELINE" --judge "$JUDGE" --out results/baseline.json
python3 main.py run --suite suites/assistant.json --base-url "$EVAL_BASE_URL" --model "$CANDIDATE" --judge "$JUDGE" --out results/candidate.json --junit results/candidate.xml
python3 main.py gate --baseline results/baseline.json --candidate results/candidate.json --junit results/gate.xml
```

Rerun the baseline in the same job when it is a hosted model (a name is not a version). Upload the JSON and JUnit files as artifacts. In promptfoo, the same suite runs with `promptfoo eval -c promptfooconfig.yaml -o out.json` (exit 100 on failed assertions); count the pass-to-fail and fail-to-pass cases from its per-row `success` and compute the same p-value with `python3 main.py stats mcnemar B C`.

## 8. Act on the outcome

| Gate output | Means | Next action |
|---|---|---|
| `ACCEPT (quality: improved)` | significant gain at least the minimum effect, no critical regression, budgets held | ship; make the candidate run the new baseline |
| `ACCEPT (quality: no significant difference)` | the suite cannot tell them apart | ship if the change was for cost or latency; if it was for quality, the claim is unproven |
| note: lost N cases net, cannot tell from noise | a drop the suite is too small to confirm | read the pass-to-fail cases; grow the suite before the next change |
| `REJECT (quality: regressed)` | significant drop beyond the minimum effect | read every pass-to-fail case; fix brittle assertions and rerun, otherwise block |
| critical case(s) regressed | a must-never case failed | block, whatever the pass rate says |
| provider error(s) | the run did not measure the model | fix the endpoint and rerun; never gate on an outage |
| missing baseline cases / different suite files | the runs are not comparable | rerun both on the same suite |
| latency or cost over budget | quality may be fine, the candidate is too slow or too expensive | renegotiate the budget or pick another candidate |

## 9. Report

Write: the suite name and hash, the number of cases and samples, baseline and candidate pass rates with Wilson intervals, the pass-to-fail and fail-to-pass case ids, McNemar's p and the effect against alpha and the minimum effect, critical regressions, p95 latency and mean cost per call against the budgets, contamination findings, the judge and its last calibration, and the verdict with every reason. "Looks better in the notebook" is not a result; a paired comparison on a named suite is.
