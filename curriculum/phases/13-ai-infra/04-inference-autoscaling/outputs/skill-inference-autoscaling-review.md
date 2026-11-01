---
name: inference-autoscaling-review
description: Review how a model-serving Deployment (vLLM, llama.cpp, SGLang, KServe) scales; measure the replica's knee and cold start, pick the metric and target from those numbers, check windows, rate limits and the scale-to-zero path, replay a trace, and report
version: 1.0.0
phase: ai-infra
lesson: ai-infra/inference-autoscaling
tags: [ai-infra, autoscaling, hpa, keda, kserve, vllm, llama-cpp, inference, slo, cold-start]
---

# Inference autoscaling review

Use this when a model server misses its latency SLO under load, sits at its maximum replicas overnight, takes minutes to recover from a spike, or is about to get scale to zero. The goal is a scaling policy whose metric, target and windows are derived from two measured numbers (the replica's knee and its cold start), not copied from a web service.

## 1. Inventory what scales the Deployment today

```bash
NS=llm; DEPLOY=chat
kubectl -n $NS get hpa,scaledobject,inferenceservice 2>/dev/null
kubectl -n $NS get hpa -o yaml | sed -n '/metrics:/,/status:/p'
kubectl -n $NS describe hpa | sed -n '/Conditions:/,/Events:/p'
kubectl -n $NS get deployment $DEPLOY -o jsonpath='{.spec.replicas}{" "}{.spec.template.spec.containers[0].args}{"\n"}'
```

Write down: the autoscaler (HPA, KEDA ScaledObject, KServe Standard or Knative mode), the metric and target, min and max, `behavior` (or the defaults: scale-down window 300 s, scale-up 100% or 4 pods per 15 s, tolerance 0.1), and the server's batch flags (`--max-num-seqs`, `--max-num-batched-tokens`, `--gpu-memory-utilization` for vLLM; `--parallel` for llama.cpp). Two autoscalers writing the same Deployment is a finding on its own.

## 2. Confirm the server exports the queue

```bash
POD=$(kubectl -n $NS get pods -l app=$DEPLOY -o jsonpath='{.items[0].metadata.name}')
kubectl get --raw "/api/v1/namespaces/$NS/pods/$POD:8000/proxy/metrics" > metrics.txt
grep -E '^(vllm:num_requests_(running|waiting)|vllm:kv_cache_usage_perc|llamacpp:requests_(processing|deferred)|sglang:num_(running|queue)_reqs|sglang:token_usage)' metrics.txt
python3 infscale.py parse metrics.txt
```

| Server | Running | Waiting (overload) | KV pressure | Needs |
|---|---|---|---|---|
| vLLM | `vllm:num_requests_running` | `vllm:num_requests_waiting` | `vllm:kv_cache_usage_perc` (older: `vllm:gpu_cache_usage_perc`) | nothing |
| llama.cpp | `llamacpp:requests_processing` | `llamacpp:requests_deferred` | none | `--metrics` |
| SGLang | `sglang:num_running_reqs` | `sglang:num_queue_reqs` | `sglang:token_usage` | `--enable-metrics` |

If none of these appear, stop: the autoscaler cannot be better than its signal.

## 3. Measure one replica's knee

Pin the Deployment at one replica, then step the load up:

```bash
kubectl -n $NS scale deployment/$DEPLOY --replicas=1
for c in 2 4 8 16 32 64; do
  python3 infscale.py load --url http://$DEPLOY.$NS.svc:8000 --concurrency $c --duration 60 --max-tokens 256 --json > load-$c.json
done
```

Run it from a pod inside the cluster, and use prompts and output lengths shaped like production. For each step record requests/s, latency p95, and running and waiting from `/metrics`. The knee is the concurrency at which waiting stops being zero or TTFT p95 crosses the SLO. Set the per-replica target to 60-80% of that concurrency: the margin is what absorbs a ramp while new replicas start.

## 4. Measure the cold start

```bash
kubectl -n $NS scale deployment/$DEPLOY --replicas=2
kubectl -n $NS get pods -l app=$DEPLOY -w                  # time from Pending to Ready for the new pod
kubectl -n $NS get events --sort-by=.lastTimestamp | grep -E 'Pulling|Pulled|Started'
kubectl -n $NS logs -l app=$DEPLOY --tail=50 | grep -iE 'load|weights|graph|ready'
```

Split it into scheduling (and node provisioning, if the GPU node pool scales), image pull, weight load and warm-up. Measure it twice, once on a node that has the image cached and once on a fresh node.

## 5. Choose the metric

| Situation | Metric | Target | Why |
|---|---|---|---|
| vLLM or SGLang, steady traffic | running + waiting per replica | 60-80% of the knee concurrency | tracks concurrency against batch capacity |
| llama.cpp | `requests_deferred` above 0, or processing + deferred | slots x 0.8 | processing is capped by `--parallel` |
| long contexts, preemptions in the logs | KV usage, plus the queue | 0.8 to 0.9 | memory, not slots, limits the batch |
| SLO-driven teams | TTFT p95 from the histogram as an alert; the queue for scaling | the SLO | the histogram lags; the queue leads |
| anything | CPU or GPU utilisation | do not | CPU plateaus at one busy core; GPU utilisation is near 100% at any batch |

## 6. Check windows, rate limits and zero against the cold start

| Check | Healthy | Fix |
|---|---|---|
| scale-up window | 0 s | remove it; inference ramps need to start early |
| scale-up rate | allows the step the knee math needs within one cold start | raise `Pods` or `Percent` in `behavior.scaleUp` |
| scale-down window | at least 2 x cold start, and at least one traffic period's noise | raise `stabilizationWindowSeconds` |
| scale-down rate | removes at most about 25% per minute | a `Pods` or `Percent` policy in `behavior.scaleDown` |
| minReplicas | enough for the base load plus one cold start of growth | raise it, or accept the misses explicitly |
| scale to zero | a component in front holds and counts requests (KEDA HTTP add-on, Knative activator, gateway) and clients tolerate one cold start | keep min 1, or add the holding component |
| zero with a pod-exported metric | never | the metric disappears with the pods; the service never wakes |

## 7. Replay before you change production

```bash
python3 infscale.py simulate --rates "0=3,180=10,270=20,360=30,720=10,840=3,960=0,1320=0.2" \
  --cold-start 120 --max-num-seqs 64 \
  --policy cpu:60 --policy queue:24 --policy queue:24,lookahead=120 --policy queue:24,min=0,cooldown=300 --timeline
```

Replace the rates with your traffic shape (or `--trace FILE` with `t`, `prompt_tokens` and `output_tokens` rows from gateway logs), use the measured cold start, and read SLO misses and replica-minutes per policy. Look at the timeline for oscillation, meaning a large drop followed by a rise within one cold start.

## 8. Report

State: the autoscaler and its current metric, target and behavior; the server and whether it exports the queue; the knee (concurrency, requests/s, TTFT p95) and the cold start with its breakdown; the recommended metric, target, min, max, windows and rate limits, each with the number it came from; the scale-to-zero verdict and the holding component it relies on; the replay table (misses and replica-minutes for the current and the recommended policy); and the manifest change. Do not recommend a target without the load-test step it came from.
