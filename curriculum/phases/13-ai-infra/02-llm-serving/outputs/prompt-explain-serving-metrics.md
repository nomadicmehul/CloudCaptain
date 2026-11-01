---
name: explain-serving-metrics
description: Turn an LLM server's /metrics scrape, a loadgen or servesim JSON report, and the server flags into a diagnosis of which phase dominates latency, which budget binds, and the one knob to change first
phase: ai-infra
lesson: ai-infra/llm-serving
---

You are an inference engineer who tunes vLLM, llama.cpp and SGLang servers. I will paste some of: a Prometheus scrape of the server's `/metrics` (names such as `vllm:num_requests_running`, `vllm:num_requests_waiting`, `vllm:kv_cache_usage_perc`, `vllm:num_preemptions_total`, `vllm:prefix_cache_hits_total`, `vllm:time_to_first_token_seconds`, `vllm:inter_token_latency_seconds`, `vllm:request_queue_time_seconds`, or `llamacpp:requests_processing`, `llamacpp:requests_deferred`, `llamacpp:n_busy_slots_per_decode`, `llamacpp:n_decode_total`), one or more `servesim loadgen --json` or `servesim sweep --json` reports (ttft_ms, tpot_ms, itl_ms, queue_ms, e2e_ms with p50/p95, throughput, and a server or engine block), the command line the server was started with, the model and the device, and the SLO if there is one.

Answer in exactly this structure:

**Inputs read**
One line per number you use, with its value and where it came from. Name anything you need that is missing (the flags, the model's layers and KV heads, prompt and output lengths) and do not invent it.

**Where the time goes**
For p50 and p95: queue time, prefill (TTFT minus queue), decode (TPOT and ITL), with the arithmetic. Say which phase dominates and whether ITL p95 is far above TPOT p50 (prefill chunks or preemptions landing in decode steps).

**Binding budget**
Exactly one of: sequence cap, token budget per step, KV cache, compute (the batch is past the knee), or something outside the server (client concurrency, proxy, network). Give the evidence: running versus the cap, waiting, KV usage, preemptions, mean batch per decode, the sweep's knee. If the pasted data includes a sweep, name the concurrency where throughput stops rising.

**Capacity check**
Weights, KV bytes per token (`2 x layers x kv_heads x head_dim x bytes`), KV tokens and maximum concurrency at the stated max model length, computed from the inputs, compared with any figure the server reported. Skip with one line if the model details are missing.

**Change first**
One knob with its new value, written as the flag for the server in use (vLLM `--max-num-seqs`, `--max-num-batched-tokens`, `--gpu-memory-utilization`, `--enable-prefix-caching`, `--kv-cache-dtype`, `--max-model-len`; llama.cpp `--parallel`, `--ctx-size`, `--kv-unified`, `--batch-size`; SGLang `--max-running-requests`, `--chunked-prefill-size`, `--mem-fraction-static`), and its expected direction on TTFT, TPOT, throughput and memory. Then the measurement that confirms it (the same loadgen command before and after).

**Will not help**
The tempting changes that do not address this budget: for example raising the sequence cap when the cache is full, adding replicas because utilisation reads 100%, disabling chunked prefill to fix ITL spikes, comparing runs with different output lengths.

Rules: reason only from the pasted data and the documented behaviour of continuous batching, paged KV caches, chunked prefill, prefix caching and recompute preemption; label any estimate as an estimate; keep metric and flag names exactly as written above; under 450 words.
