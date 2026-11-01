# AI Infrastructure

*GPU scheduling, model serving with vLLM, KServe and llm-d, AI gateways and the cost of running models.*

Running models in production is an infrastructure problem with unusual numbers. A GPU is scarce, expensive and indivisible until you split it; a model server is a batch scheduler whose memory is a KV cache that grows with every token; a gateway has to count tokens, not requests; an autoscaler that watches CPU scales the wrong thing; and the bill is decided by utilisation more than by any price list. This phase builds each of those mechanisms as a small tool first, from first principles and on a laptop without a GPU, then runs the same job with real software on CPU (llama.cpp, pgvector, LiteLLM, Kubernetes) so every vLLM flag, routing decision and cost line reads as something you have already computed.

## How this phase is built

| Lesson | Type | What you build | What you then use |
|---|---|---|---|
| GPU Scheduling | Build | `gpusched`: GPUs, GPU memory, NVLink islands, MIG and time-slicing; bin-pack, spread and topology-aware placement; a DRA allocator with a CEL subset; gang scheduling and fragmentation | extended resources on kind, DeviceClass, ResourceClaim and ResourceSlice, the NVIDIA device plugin, Kueue and JobSet |
| LLM Serving | Build | `servesim`: a token-level server with continuous batching, chunked prefill, a paged KV cache with prefix caching and preemption, TTFT, TPOT and throughput; a load generator for real servers | llama.cpp server on CPU with a small model, vLLM's batching and memory knobs, `/metrics` |
| AI Gateway | Build | `tokengate`: an OpenAI-compatible proxy with model routing, request and token budgets per key, retries and fallback, counted streams, a cache, redacted logs and a cost line | LiteLLM proxy, Envoy AI Gateway, provider rate-limit headers |
| Inference Autoscaling | Build | `infscale`: the HPA algorithm on server metrics (running, waiting, KV usage) with cold-start lookahead and scale to zero, and a simulator that shows why CPU is the wrong signal | HPA on external metrics, KEDA, KServe, `llm-d-inference-sim` on kind |
| Inference Routing | Build | `prefixroute`: prefix-cache-aware routing with block hashes and llm-d's scorers, a replica simulator, disaggregated prefill and decode costs, an OpenAI-compatible router | `llm-d-inference-sim`, the Gateway API Inference Extension's InferencePool, llm-d's router |
| Vector Search | Build | `vecindex`: brute force, IVF and HNSW in pure Python, filters, tombstones, snapshots, recall@k sweeps and memory estimates | pgvector's HNSW and IVFFlat indexes, `ef_search`, `probes`, iterative index scans |
| Model Artifacts | Build | `modelinspect`: safetensors and GGUF parsers, parameter and memory counts per quantization, pinned downloads by revision and digest, models pushed and pulled as OCI artifacts | `hf download --revision`, `oras` against a local registry, ModelPack, llama.cpp loading the pulled file |
| AI Cost | Build | `tokencost`: dated and sourced prices, throughput estimates and measurements, replicas over a day's traffic, cost per million tokens, break-even against APIs and GPU allocation by namespace | OpenCost, cloud price APIs as inputs, FOCUS columns |

Further lessons for this phase are proposed in [ROADMAP.md](https://github.com/nomadicmehul/CloudCaptain/blob/main/ROADMAP.md).

## Reference layer

The wider landscape of ML and AI operations is mapped in the [AI/ML Ops learning path](https://cloudcaptain.io/docs/learning-paths/ai-ml); the Kubernetes guides this phase leans on live under [Kubernetes](https://cloudcaptain.io/docs/tools/kubernetes).
