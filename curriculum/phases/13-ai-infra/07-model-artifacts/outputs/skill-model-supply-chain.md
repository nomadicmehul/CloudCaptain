---
name: model-supply-chain
description: Bring a model from Hugging Face into production as a pinned, verified OCI artifact - pin the revision, refuse pickle, record and verify every file's sha256, inspect headers for size and quantization, push to the internal registry with a ModelPack config, deploy by manifest digest
version: 1.0.0
phase: ai-infra
lesson: ai-infra/model-artifacts
tags: [llm, model-serving, supply-chain, oci, oras, modelpack, huggingface, gguf, safetensors, quantization]
---

# Model supply chain

Use this when a team wants to serve a new model or a new version of one, when an init container downloads weights at pod start, when answers changed and nobody can say which weights served them, or before a GPU capacity plan is approved. The goal: every running pod names its weights by a digest you can verify, and nothing reaches a GPU that you have not inspected.

## 1. Pin the source

```bash
REPO=<org/model>; FILES="<file> config.json tokenizer.json"
curl -s "https://huggingface.co/api/models/$REPO" | python3 -c 'import json,sys; d=json.load(sys.stdin); print(d["sha"], d.get("gated"), d.get("cardData", {}).get("license"))'
curl -s "https://huggingface.co/api/models/$REPO/tree/<commit>" | python3 -c 'import json,sys; [print(f["path"], f["size"], (f.get("lfs") or {}).get("oid", "git")) for f in json.load(sys.stdin)]'
```

Record the 40-character commit, not `main`. Record the license and whether the repo is gated (gated repos need a token in CI, which is a secret to manage). For every file you will ship, record its size and its LFS sha256 (`lfs.oid`); small files kept in git are identified by their git blob id.

## 2. Refuse what executes on load

| File | Verdict |
|---|---|
| `*.safetensors`, `*.gguf` | accept: a header plus raw tensors, no code |
| `pytorch_model.bin`, `*.pt`, `*.pth`, `*.ckpt` (zip or pickle) | do not load in the serving path; ask for safetensors, or convert in an isolated sandbox and treat the output as a new artifact |
| `*.py` in the repo, `trust_remote_code=True` | code review required; pin it like any dependency |
| `*.onnx` | accept for ONNX runtimes; inspect with the runtime's tools |

`python3 main.py inspect FILE` exits 2 and names the pickle risk for zip and pickle checkpoints without unpickling them.

## 3. Download by revision and verify

```bash
python3 main.py fetch $REPO <file> --revision <commit> --sha256 <lfs-sha256> --dest model    # refuses before downloading if the Hub disagrees
hf download $REPO <file> config.json --revision <commit> --local-dir model                   # the vendor CLI; then verify:
sha256sum model/<file>                                                                        # must equal the recorded LFS sha256
```

Never accept a download whose hash you did not compare. A truncated file can still load and produce garbage.

## 4. Inspect before you schedule

```bash
python3 main.py inspect model/<file>                        # parameters, bytes per dtype, file type, layers, KV heads, context
python3 main.py inspect https://huggingface.co/$REPO/resolve/<commit>/model.safetensors --config model/config.json   # header only, over HTTP Range
python3 main.py memory model/<file> --ctx <max-model-len> --seqs <max-num-seqs> --kv-type f16 --gpu-gib <GPU GiB> --utilization 0.9
```

| Finding | Meaning | Action |
|---|---|---|
| bits/weight far above the quant name (a "Q4_K_M" at 6+ bits) | rows not divisible by 256 fell back to 32-wide types, or embeddings kept at Q8_0 | size the GPU from the measured tensor bytes, not the name |
| KV cache > weights at the planned context and concurrency | the usual case for long contexts | lower `--max-model-len`, cap concurrency, quantize the cache (`--kv-cache-dtype fp8` in vLLM, `--cache-type-k q8_0` in llama.cpp), or pick a GPU with more memory |
| `max_sequences_at_context` is 0 | one full-length sequence does not fit | the server will refuse to start or truncate; change context or hardware |
| `sliding window` or `latent attention` note | the formula overstates the cache | confirm with the server's startup log |
| parameter count differs from the model card | wrong file, wrong revision, or tied embeddings counted twice by the card | stop and resolve before shipping |

## 5. Push to the internal registry

```bash
REG=<registry>/models/<name>
python3 main.py push $REG:<version> model/<file> model/config.json model/tokenizer.json --revision <commit> --name <name>
# or with ORAS, same media types and config:
python3 main.py push $REG:<version> model/<file> model/config.json --revision <commit> --dry-run --config-out modelpack-config.json > /dev/null
(cd model && oras push $REG:<version> --artifact-type application/vnd.cncf.model.manifest.v1+json \
  --config ../modelpack-config.json:application/vnd.cncf.model.config.v1+json \
  <file>:application/vnd.cncf.model.weight.v1.raw config.json:application/vnd.cncf.model.weight.config.v1.raw)
oras manifest fetch $REG:<version> | python3 -m json.tool       # layer digests must equal the recorded LFS sha256 values
```

Keep weights uncompressed (`.raw` or `.tar` layers): they do not compress, and decompression only slows the pull. Attach the license and an SBOM or signature with `oras attach` so they travel with the digest.

## 6. Deploy by digest, verify on pull

Reference `$REG@sha256:<manifest digest>` in the Deployment: in an image volume (`volumes[].image.reference`), a modelcar `oci://` storage URI, or an init container running `oras pull` / `main.py pull`. Never a tag. On pull, every blob is re-hashed; a mismatch must fail the pod, not warn.

```bash
python3 main.py pull $REG@sha256:<digest> --dest /models      # verifies manifest and every blob, refuses ../ paths
```

After rollout, confirm the server loaded what you shipped: llama.cpp `/v1/models` `meta.n_params` and `meta.size` equal `inspect`'s parameters and tensor bytes; vLLM's startup log shows the KV cache capacity you planned.

## 7. Decision table for failures

| Symptom | Cause | Fix |
|---|---|---|
| `fetch` warns that the revision is a branch | someone pinned `main` | replace with the printed commit |
| `you pinned ... but the Hub serves ...` | the file changed upstream, or you pinned the wrong file | review the upstream change; update the pin deliberately |
| download hash mismatch | truncated transfer, proxy or mirror rewriting | retry from the source; investigate the proxy; never skip the check |
| `401`/`403` from the Hub | gated or private repo | accept the license with a service account; use a token from a secret |
| pull digest mismatch | registry storage corruption or tampering | stop the rollout; compare with the recorded LFS sha256; re-push from a verified copy |
| pods on one node serve different answers | some pods pulled by tag or branch | deploy by manifest digest only |

## 8. Report

State: repository, commit, license and gating; each shipped file with size, LFS sha256 and the registry layer digest (they must match); the format, file type and measured bits per weight; parameters; weights bytes, KV bytes per token and total at the planned context and concurrency, and the GPU it fits; the manifest digest deployed and where it is referenced; any file refused (pickle, remote code) and why; and the server's own report confirming the load.
