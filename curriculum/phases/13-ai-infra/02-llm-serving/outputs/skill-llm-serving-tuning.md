---
name: llm-serving-tuning
description: Diagnose and tune an LLM inference server (vLLM, llama.cpp, SGLang) that is slow or expensive, by finding which budget binds (sequences, tokens per step or KV cache blocks) and changing one knob at a time against a TTFT and TPOT SLO
version: 1.0.0
phase: ai-infra
lesson: ai-infra/llm-serving
tags: [ai-infra, llm, inference, vllm, llama-cpp, sglang, kv-cache, batching, latency, capacity]
---

# LLM serving tuning

Use this when users report a slow first token or slow streaming, when a model server's cost per token is higher than planned, before you raise replica counts, or before a launch that changes prompt sizes or traffic. A model server is a scheduler with three budgets: sequences per batch, tokens per step and KV cache blocks. Every symptom is one of them binding, and every fix is a trade between TTFT, TPOT and throughput.

## 1. Write down the target and the traffic

Before touching a flag, record:

- The SLO as percentiles: TTFT p95 (what a chat user feels) and TPOT or ITL p95 (streaming smoothness), or end-to-end p95 for batch jobs.
- The traffic shape: requests per second at peak, prompt length p50/p95, output length p50/p95, and how much of each prompt is shared (system prompts, few-shot examples, retrieved documents reused across turns).
- The server, version, model, dtype, device and every flag it was started with (`ps`, the Deployment spec, or the startup log).

If you cannot fill this in, measure it first (step 4) and say so in the report.

## 2. Do the memory arithmetic

```bash
python3 servesim.py capacity --hardware l4-24gb --model llama-3.1-8b --gpu-memory-utilization 0.90 --max-model-len 8192
```

By hand: weights = parameters x bytes per parameter (2 for BF16, about 1.06 for Q8_0, about 0.56 for 4-bit); KV per token = 2 x layers x KV heads x head dim x bytes (from the model's `config.json`); KV tokens = (device memory x memory share - weights - activation reserve) / KV per token. Compare with the server's own number: vLLM logs `KV cache size: N tokens, Maximum concurrency for M tokens per request: X`; llama.cpp prints `n_ctx_slot` per slot at startup and in `/slots`. Maximum concurrency below the number of requests you expect in flight means memory is the binding budget before you have measured anything.

## 3. Read the server's own metrics

```bash
curl -s localhost:8000/metrics | grep -E '^vllm:(num_requests_running|num_requests_waiting|kv_cache_usage_perc|num_preemptions_total|prefix_cache_(hits|queries)_total)'
curl -s localhost:8080/metrics | grep -E '^llamacpp:(requests_processing|requests_deferred|n_busy_slots_per_decode|n_decode_total|prompt_tokens_cached_total)'
curl -s localhost:8080/slots          # llama.cpp: per-slot n_ctx and is_processing
```

Sample them during peak, not after. The histograms `vllm:time_to_first_token_seconds`, `vllm:inter_token_latency_seconds`, `vllm:request_queue_time_seconds` and `vllm:e2e_request_latency_seconds` split latency into queue, prefill and decode inside the server.

## 4. Measure a concurrency sweep

```bash
for c in 1 4 16 64; do python3 servesim.py loadgen --url http://127.0.0.1:8000 --concurrency $c --requests $((c * 8)) --max-tokens 256 --ignore-eos --json > sweep-$c.json; done
python3 servesim.py sweep --hardware h100-sxm-80gb --model llama-3.1-8b --prompt-tokens 256-2048 --output-tokens 64-512 --concurrency 1,4,16,64
```

Use prompts and output lengths that match production. Plot or tabulate output tokens per second, TTFT p50/p95 and TPOT p50/p95 per concurrency. The healthy shape: throughput rises steeply and TPOT barely moves at low concurrency, then TPOT climbs, then throughput flattens and TTFT explodes. The knee is your capacity per replica at this SLO.

## 5. Find the binding budget and pick one knob

| Symptom (metrics) | Binding budget | Change | Cost |
|---|---|---|---|
| waiting > 0, running = `--max-num-seqs` (or `--parallel`), KV usage well under 1 | sequence cap | raise `--max-num-seqs` / `--parallel` / SGLang `--max-running-requests` | TPOT rises with the batch |
| KV usage near 1, preemptions rising, running below the cap | KV cache | prefix caching on; KV cache in FP8 (`--kv-cache-dtype fp8`); smaller `--max-model-len`; higher memory share; quantize weights; more GPUs per replica (tensor parallel) | accuracy check for FP8 or quantization; long contexts refused |
| TTFT high, queue time low, prompts long | prefill compute | raise `--max-num-batched-tokens` (bigger prefill chunks), prefix caching for shared prefixes | ITL spikes when long prompts arrive |
| ITL p95 spikes while long prompts arrive, TPOT p50 fine | token budget per step | lower `--max-num-batched-tokens` (smaller chunks); separate prefill and decode workers at scale | longer TTFT for long prompts |
| TTFT high and queue time high at a fraction of the knee | too few replicas or a router piling onto one replica | scale on queue depth, route by load and prefix | more replicas |
| prefix hit rate low although prompts share a system prompt | cache not shared | put the shared text first and byte-identical; route a conversation to the same replica | none |
| llama.cpp: `exceeds the available context size` with idle slots | per-slot static context | raise `--ctx-size`, lower `--parallel`, or use `--kv-unified` | memory |
| throughput flat from concurrency 1 to 4 on a GPU | something serialises: a proxy, `--max-num-seqs 1`, a client without concurrency | check the client and every hop | none |

Change one knob, rerun step 4 with the same workload, and keep the change only if the SLO percentiles improved at the target load. Write the before and after numbers down.

## 6. What not to do

- Do not read `nvidia-smi` utilisation as load: it is the fraction of time a kernel ran, 100% at a batch of 1.
- Do not raise the sequence cap when the KV cache is full; it only adds preemptions.
- Do not scale replicas on CPU utilisation; scale on running plus waiting requests or KV usage.
- Do not compare TPOT across runs with different output lengths or `ignore_eos` settings.
- Do not trust a server default across upgrades: pass `--gpu-memory-utilization`, `--max-num-seqs`, `--max-num-batched-tokens` and `--max-model-len` explicitly.

## 7. Report

State: the SLO and traffic shape; the capacity arithmetic (weights, KV per token, KV tokens, maximum concurrency) next to the server's own figure; the metrics at peak (running, waiting, KV usage, preemptions, prefix hit rate); the sweep table with the knee; the binding budget from the decision table and the evidence for it; the single change made, with TTFT p95, TPOT p95 and throughput before and after at the same load; the replicas needed at the measured capacity per replica; and what you would try next. Never report "the GPU is maxed" without the batch size and the KV usage that go with it.
