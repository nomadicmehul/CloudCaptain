---
name: inference-routing-review
description: Review how an LLM serving fleet routes requests: measure the servers' own prefix cache hit rate and prefix spread, size cache per replica against the shared prefixes, compare routing policies on a replayed trace, set scorer weights against queue depth, decide on prefill/decode disaggregation with KV transfer arithmetic, check the InferencePool and endpoint picker configuration, and report
version: 1.0.0
phase: ai-infra
lesson: ai-infra/inference-routing
tags: [llm, inference, vllm, prefix-caching, kv-cache, routing, gateway-api-inference-extension, inferencepool, llm-d, disaggregation]
---

# Inference routing review

Use this when model servers (vLLM, SGLang, or `llm-d-inference-sim` in a test environment) sit behind a load balancer and time to first token or GPU cost is higher than the prompt mix suggests. The question is whether requests that share a prefix reach a replica that still holds it, and whether load is allowed to overrule that when it should. Work from the servers' own counters, never from the router's opinion of itself.

## 1. Measure what the servers say

```bash
for pod in $(kubectl get pods -l app=vllm -o name); do
  kubectl exec "$pod" -- curl -s localhost:8000/metrics > "metrics-${pod#pod/}.txt"
done
grep -h -E '^vllm:(prefix_cache_(hits|queries)_total|kv_cache_usage_perc|num_requests_(waiting|running)|cache_config_info)' metrics-*.txt
python3 prefixroute.py scrape pod-a=http://POD_A_IP:8000 pod-b=http://POD_B_IP:8000
```

In Prometheus, per replica and fleet-wide:

```text
sum by (pod) (rate(vllm:prefix_cache_hits_total[15m])) / sum by (pod) (rate(vllm:prefix_cache_queries_total[15m]))
sum(rate(vllm:prefix_cache_hits_total[15m])) / sum(rate(vllm:prefix_cache_queries_total[15m]))
max by (pod) (vllm:num_requests_waiting)
```

Record: fleet hit rate, its spread across replicas, TTFT p50/p95, waiting requests per replica, block size and number of GPU blocks from `vllm:cache_config_info`. If the hit counters do not exist, prefix caching is off or the server is too old to export them; fix that first.

## 2. Find the shared prefixes

From request logs or a sample of request bodies (redact content, keep token counts):

| Measure | How |
|---|---|
| distinct shared prefixes (system prompts, tool lists, few-shot blocks, documents) | group requests by a hash of the first N messages |
| tokens per shared prefix | the model's tokenizer, or the server's `/tokenize` endpoint |
| requests per prefix per minute | the same grouping over time |
| how many replicas each prefix reaches | the access log's upstream field grouped by prefix hash |

A prefix that reaches every replica is being prefilled on every replica. That number, more than the hit rate, tells you routing is the problem.

## 3. Check that the cache can hold its share

KV bytes per token = 2 × layers × KV heads × head dimension × bytes per value (128 KiB for Llama 3.1 8B in 16-bit, 320 KiB for 70B). Cache tokens per replica ≈ (GPU memory × `--gpu-memory-utilization` − weights − activations) / bytes per token; `num_gpu_blocks × block_size` from `vllm:cache_config_info` is the exact figure.

| Situation | Meaning |
|---|---|
| sum of hot prefix tokens < cache tokens of one replica | any policy hits after warm-up; low hit rate means prefixes are not shared or salting differs |
| sum of hot prefixes > one replica, < the fleet's total | prefix-aware routing is the fix: it partitions prefixes across replicas |
| one prefix's traffic needs more than one replica | it must be served by several; let load scorers spill it, accept a few cold prefills |
| hot prefixes > the fleet's total cache | add replicas or CPU/disk KV offload; routing alone cannot fix it |

## 4. Compare policies on a replay before touching production

Capture one hour of request bodies (or generate a trace with the same prefix counts and sizes) and replay it against a staging fleet or `llm-d-inference-sim` replicas started with `--enable-kvcache --kv-cache-size N --latency-calculator per-token`, with fresh replicas per policy:

```bash
python3 prefixroute.py simulate trace.jsonl --replicas 8 --cache-blocks BLOCKS --prefill-tps MEASURED
python3 prefixroute.py serve --endpoint a=URL_A --endpoint b=URL_B --policy round-robin &
python3 prefixroute.py replay trace.jsonl --url http://127.0.0.1:8080 --metrics a=URL_A --metrics b=URL_B
```

Compare hit rate from the servers' counters, TTFT p50/p95, queue p95, requests per replica and groups served by one replica. Expect prefix-aware routing to raise the hit rate and lower TTFT; if it does not, step 2 or 3 is wrong.

## 5. Set scorer weights against load

With the llm-d router (or any EPP) start from the chart default: `prefix-cache-scorer` 3, `queue-scorer` 2, `kv-cache-utilization-scorer` 2, plus `no-hit-lru-scorer` for cold requests if many new prefixes arrive.

| Symptom | Change |
|---|---|
| one replica's `num_requests_waiting` far above the others, hit rate high | raise queue or KV weight, or add `no-hit-lru-scorer` |
| hit rate barely above round robin, queues even | raise prefix weight; check that the router hashes with the same block size and `cache_salt` as the servers |
| long prompts lose to short ones with the same ratio | set `matchLengthWeight` and `matchLengthScaleTokens` near p95 prompt length |
| router CPU high on long prompts | cap `maxPrefixTokensToMatch` |
| hits drop after bursts of new tenants | the approximate index is stale after evictions; use `precise-prefix-cache-producer` fed by KV events |

Change one weight at a time and rerun step 4.

## 6. Decide on prefill/decode disaggregation

```bash
python3 prefixroute.py disagg --model llama-3.1-8b --prompt-tokens P95_UNCACHED --prefill-tps MEASURED --link-gbps 25 --link-gbps 100
```

| Finding | Decision |
|---|---|
| decode ITL p95 spikes when long prompts arrive, prefill throughput measured | try chunked prefill (`--max-num-batched-tokens`) first |
| link Gb/s below `kv_bytes_per_token × prefill_tps × 8 / 1e9` | do not disaggregate over this link |
| link keeps up, uncached prompts long, ITL SLO tight | disaggregate with a `prefix-based-pd-decider` threshold (`nonCachedTokens`) near the prompt length where remote TTFT stops losing |
| most prompts mostly cached on the decode pod | keep them local: the decider's threshold already does this |

## 7. Check the Kubernetes wiring

```bash
kubectl get crd inferencepools.inference.networking.k8s.io
kubectl get inferencepool -A -o yaml
kubectl get inferencepool POOL -o jsonpath='{.spec.selector.matchLabels}{"\n"}'
kubectl get pods -l app=MODEL_SERVER_LABEL -o wide
kubectl get httproute -A -o jsonpath='{range .items[*]}{.metadata.name}{"\t"}{range .spec.rules[*].backendRefs[*]}{.kind}/{.name} {end}{"\n"}{end}'
kubectl get inferencepools.inference.networking.k8s.io,pods -o json > live.json && python3 prefixroute.py pool live.json
```

| Check | Expect |
|---|---|
| pool `selector.matchLabels` | selects exactly the model-server Pods, all Ready |
| `targetPorts` | the server's HTTP port, unique |
| `endpointPickerRef` | the EPP Service and its port; `failureMode` chosen on purpose (FailClose refuses traffic when the EPP is down, FailOpen lets the gateway forward to a pool endpoint without a pick) |
| HTTPRoute backendRef | `group: inference.networking.k8s.io`, `kind: InferencePool` |
| pool `status.parents` | `Accepted` and `ResolvedRefs` True for the gateway |
| EPP logs and metrics | scraping every endpoint; no endpoints stuck without metrics |

## 8. Roll out behind a comparison

Run the new router on a copy of the traffic or a fraction of tenants first. Watch fleet hit rate, TTFT p95, per-replica waiting requests and error rate for a full traffic cycle. Roll back by pointing the route back at the Service.

## 9. Report

State: fleet and per-replica hit rate before and after (from the servers' counters), how many replicas each hot prefix reached before and after, cache tokens per replica against hot prefix tokens, the policies compared with hit rate, TTFT p50/p95 and queue p95 each, the scorer weights chosen and why, the disaggregation decision with the transfer arithmetic, the InferencePool and EPP findings, and the rollout and rollback plan. Do not claim a hit-rate improvement measured by the router's own index.
