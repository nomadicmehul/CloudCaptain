---
name: ai-cost-review
description: Run a budget review of self-hosted LLM inference against API prices: dated and sourced prices, throughput measured at the batch the latency target allows, replicas from the real traffic curve, utilisation, cost per million input and output tokens, break-even volume, sensitivity and GPU showback by team
version: 1.0.0
phase: ai-infra
lesson: ai-infra/ai-finops
tags: [ai-infra, finops, llm, inference, gpu, cost, capacity-planning, opencost, focus]
---

# AI cost review

Use this before a budget review, before moving a workload between a hosted API and your own GPUs, when an inference bill comes in above the estimate, or when finance asks what a token costs. The rule throughout: every price is an input with a date and a source, every throughput number names the batch and request shape it was measured at, and utilisation is computed from the traffic curve, never assumed.

## 1. Collect the inputs, with provenance

| Input | Where it comes from | Reject it when |
|---|---|---|
| instance hourly price | `aws pricing get-products`, the Azure Retail Prices API, the Google Cloud Billing Catalog API, or the bill for committed rates | no date, no source, or older than 90 days |
| accelerator memory, bandwidth, dense TFLOPs | vendor datasheet | the TFLOPs figure is the sparse one (twice the dense) |
| API input, output and cached-input price per million tokens | the provider's pricing page | no date; a different model tier from the one you would self-host |
| traffic | 24 hourly request rates from the gateway or the model server's request counter, plus mean input and output tokens per request | a single average instead of a curve |
| latency target | product requirement, as output tokens per second per request (or a time per output token) | missing; without it any batch looks fine |
| autoscaler settings | target utilisation, min and max replicas, or its real hourly decisions | min replicas unknown |
| people and operations | on-call, upgrades, evaluation runs per month | silently zero |

```bash
python3 tokencost.py prices prices.json            # every entry dated and sourced; stale ones listed with the page to re-read
```

## 2. Get throughput at the batch you will run

Measure if the hardware exists; estimate only to choose what to measure.

```bash
python3 tokencost.py measure --url http://MODEL:8080 --instance INSTANCE --model MODEL --quantization QUANT \
  --concurrency 1,4,8,16 --requests 16 --max-tokens OUTPUT_TOKENS --ignore-eos --no-prompt-cache \
  --metrics-url http://MODEL:8080/metrics -o measured.json
python3 tokencost.py estimate --prices prices.json --instance INSTANCE --model MODEL --params-b B --quantization QUANT \
  --layers L --kv-heads H --head-dim D --input-tokens IN --output-tokens OUT --batches 1,8,16,32 -o estimated.json
```

Check: the server's token counter delta equals the client's count; per-request tokens/s falls as concurrency rises (if it does not, the server is not batching or the client is the bottleneck); the request shape is close to production's. Mark every estimated point as an estimate in the report.

## 3. Build the plan

```bash
python3 tokencost.py plan --prices prices.json --scenario scenario.json --measurements measured.json estimated.json [--replicas autoscaler.json] --json -o plan.json
python3 tokencost.py plan --prices prices.json --scenario scenario.json --measurements measured.json estimated.json
```

For each option read: the chosen batch and its per-request speed against the target, replica-hours per day, utilisation, monthly cost, cost per million input and output tokens, cost per 1,000 requests, and break-even against each API that is a real alternative in quality.

## 4. Decide with the decision table

| What the plan shows | Read it as | Do |
|---|---|---|
| no batch meets the latency target | the weights are too large for the memory bandwidth | quantize (Q8_0, then Q4_K) and re-measure quality, or move to a higher-bandwidth GPU |
| utilisation far below the autoscaler target | the replica floor and whole replicas in quiet hours | send quiet hours to an API, fill them with batch work, or use a smaller GPU per replica |
| shortfall hours (`!`) at the chosen batch | max replicas too low for the peak | raise the ceiling or accept queueing; price both |
| break-even "never" | a fully used replica costs more per request than the API | stay on the API; revisit when prices or throughput change |
| break-even above twice the traffic forecast | self-hosting pays only if growth arrives | stay on the API, or commit only the replica floor |
| break-even below today's traffic | self-hosting is cheaper now | self-host, keep the API as fallback, price people and operations in |
| the cheapest token is on the largest GPU but the monthly bill is highest | one big replica is mostly idle at this volume | choose the smaller GPU now; plan the move at the break-even volume |
| input cost per million tokens far below output | prefill is compute-bound, decode is bandwidth-bound | shorten outputs before prompts; cache prompt prefixes |

## 5. Sensitivity

Re-run the plan with each change alone and record the monthly cost and break-even:

- price: the Spot or committed price instead of on-demand, and API prices 20% lower;
- throughput: the measured point at the next batch down (latency target tightened);
- utilisation: traffic at 0.5x and 2x the forecast (the plan's sweep shows cost per million tokens at 0.25x to 8x);
- caching: `cached_input_fraction` at the share your prompts actually reuse.

The decision is robust if it survives all four.

## 6. Showback

```bash
kubectl get nodes -o json > nodes.json && kubectl get pods -A -o json > pods.json
python3 tokencost.py allocate --prices prices.json --nodes nodes.json --pods pods.json --resource nvidia.com/gpu --by namespace --focus showback.csv
curl -sG localhost:9003/allocation -d window=30d -d aggregate=namespace -d includeIdle=true   # OpenCost, if installed
```

Every team pays for the GPUs it requested; `__idle__` is a line of its own. If idle is above a quarter of the GPU spend, it belongs in the report as a finding, not in the teams' shares. Check that the showback total equals the node line on the invoice (FOCUS `BilledCost` for the same `ResourceId` and charge period).

## 7. Report

State, in this order: the options compared and the quality evidence that makes them comparable; every price with its date and source, and which are stale; for each option the chosen batch, per-request speed, utilisation, monthly cost, cost per million input and output tokens and per 1,000 requests; break-even against each API in requests per day and as a multiple of current traffic; the sensitivity results; the recommendation (API, self-host, or a mix by hour or by workload) with the single number that would change it; what is not included (people, egress, storage, discounts); and the GPU showback by team with idle shown. Do not state a cost per token without the utilisation it assumes.
