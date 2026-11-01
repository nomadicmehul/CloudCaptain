---
name: ai-gateway-policy
description: Design or review the policy of an AI gateway (tokengate, LiteLLM, Envoy AI Gateway) in front of model providers and self-hosted servers: routes and fallbacks, per-key RPM and TPM derived from provider limits, retry and cooldown rules, streaming, cache, redacted logging and a dated price table, each verified with a command
version: 1.0.0
phase: ai-infra
lesson: ai-infra/ai-gateway
tags: [ai-infra, ai-gateway, llm, rate-limiting, token-bucket, litellm, envoy-ai-gateway, finops, fallback]
---

# AI gateway policy

Use this before putting an AI gateway in front of shared provider keys or self-hosted model servers, after an incident where one caller exhausted a shared limit, or when finance asks who spent what. The output is a policy (routes, budgets, retry rules, cache, logging, prices) where every rule has a command that proves it. Commands use `tokengate` from the lesson; the LiteLLM and Envoy AI Gateway equivalents are named in each step.

## 1. Inventory callers, models and limits

Collect, per caller (team, service, batch job): requests per minute at peak, median and p95 prompt tokens, median and p95 output tokens, whether it streams, whether it needs a specific model or only a capability. Per provider and model: the RPM and TPM limits for your organisation or project (from the provider console or the `x-ratelimit-limit-*` / `anthropic-ratelimit-*-limit` headers on any response), and the price per million input and output tokens with the date you read it.

```bash
curl -s -D - -o /dev/null https://PROVIDER/v1/chat/completions -H "Authorization: Bearer $KEY" -H 'Content-Type: application/json' \
  -d '{"model": "MODEL", "messages": [{"role": "user", "content": "ping"}], "max_tokens": 1}' | grep -i -E 'ratelimit|retry-after'
python3 tokengate.py report gateway.jsonl --json      # if a gateway already logs: real tokens and requests per key and model
```

If a number is unknown, write "unknown" in the report; do not guess a limit.

## 2. Define routes and fallbacks

One route per capability callers ask for, not per vendor model: `chat`, `small`, `embed`. Each route is an ordered list of deployments (backend + upstream model). A fallback must be acceptable for every caller of that route: same context length or larger, same features (tools, JSON mode), acceptable quality. Never fall back across a `400`.

| Route kind | Primary | Fallback | Check |
|---|---|---|---|
| hosted chat with a self-hosted escape hatch | provider model | self-hosted server (llama.cpp, vLLM) | context length of the fallback covers the p95 prompt |
| self-hosted first, hosted overflow | self-hosted | provider model | cost per million tokens of the overflow is budgeted |
| two regions or two accounts of one provider | provider region A | provider region B | separate limits; not the same key twice |

```bash
python3 tokengate.py check --config gateway.json                       # every route, deployment and price, warnings for unpriced deployments
python3 tokengate.py plan --config gateway.json --key team-a request.json
```

LiteLLM: `model_list` entries sharing a `model_name`, plus `router_settings.fallbacks`. Envoy AI Gateway: an `AIGatewayRoute` rule matching `x-ai-eg-model` with `backendRefs` ordered by `priority`.

## 3. Derive per-key budgets from the provider limit

For each provider model: sum of the TPM of every key routed to it must stay below the provider TPM times a headroom factor (0.8 is a reasonable start; retries and fallbacks into this model consume the rest). Same for RPM. Give batch jobs their own key with a lower TPM than interactive callers, so a backfill cannot starve checkout.

| Caller type | TPM share | `max_tokens` default | Notes |
|---|---|---|---|
| interactive, user-facing | largest share, sized for p95 peak | close to the p95 answer length | latency matters more than throughput |
| batch, backfill | fixed small share | as small as the task allows | run off-peak; let it queue on 429 |
| agents (loops of calls) | per-run budget plus TPM | small | cap tool loops; one run can be hundreds of calls |

A key's TPM must be at least the largest single hold it will send (prompt estimate plus `max_tokens`), or those requests are refused as too large. Set `limits.default_max_tokens` for clients that omit `max_tokens`; every such call holds that much.

```bash
python3 tokengate.py plan --config gateway.json --key batch big-request.json --json | python3 -c 'import json,sys; r=json.load(sys.stdin); print(r["estimate"], r["tpm"], r["fits"])'
```

LiteLLM: virtual keys from `POST /key/generate` with `tpm_limit`, `rpm_limit`, `max_budget`, `models` (needs `DATABASE_URL`). Envoy AI Gateway: `BackendTrafficPolicy` global rate limit rules per tenant header, with response costs from `llmRequestCosts`.

## 4. Set retry, cooldown and streaming rules

| Upstream answer | Rule |
|---|---|
| 429 with `retry-after` at most your wait budget (1-2 s for interactive) | wait, retry the same deployment once |
| 429 with a longer `retry-after` | fall back now; cool the deployment down for the advertised time |
| 5xx, connect error, timeout before headers | short exponential backoff, then fall back |
| 400, 401, 403, 404, 413 | return to the caller; no retry, no fallback |
| failure after the first streamed byte | end the stream, log partial usage; never retry silently |
| every deployment failed | 429 with the shortest `retry-after`, or 502; refund the hold |

Clients behind the gateway should not also retry aggressively: one layer owns retries. Tell callers to honour the gateway's `retry-after` and to treat `400 request_too_large` as final.

For streams: the gateway sets `stream_options.include_usage` upstream and strips the usage event if the caller did not ask for it; servers that never report usage are billed from a character estimate, flagged `usage_source: estimated` in the log.

## 5. Decide the cache and logging policy

| Question | Default | Change it when |
|---|---|---|
| cache scope | per key (tenant) | never global across tenants with different data |
| what is cacheable | `temperature: 0`, not streamed, `n: 1` | a caller explicitly wants replay of sampled answers |
| TTL | minutes | answers depend on data that changes (RAG): shorter, or off |
| prompt logging | `hash` for regulated data, `redacted` otherwise | debugging a specific caller for a fixed period |
| what each log line carries | key name, route, deployment, attempts, tokens, usage source, latency, cost | add tenant or trace ids; never the key secret |

Pattern redaction catches emails, keys, card numbers and IP addresses, not names or facts in prose.

## 6. Attach a price table

`prices.json` with `currency`, `date`, `source` and per-deployment input and output prices per million tokens. Self-hosted deployments get an internal rate (GPU or CPU cost per hour divided by measured tokens per hour), not zero. Review the table when a provider changes prices and whenever a fallback route changes.

## 7. Verify every rule

```bash
python3 tokengate.py serve --config gateway.json --log gateway.jsonl &
curl -s -D - -o /dev/null -H "Authorization: Bearer $KEY_TIGHT" -d @big.json localhost:4000/v1/chat/completions | grep -i -E '^HTTP|retry-after|x-tokengate-limited-by'
curl -s -X POST -d '{"status": 429, "times": 1, "retry_after": "30"}' localhost:8082/fake/fail       # with the lesson's fake as primary
curl -s -D - -o /dev/null -H "Authorization: Bearer $KEY" -d @chat.json localhost:4000/v1/chat/completions | grep -i x-tokengate-attempts
curl -sN -H "Authorization: Bearer $KEY" -d @stream.json localhost:4000/v1/chat/completions | tail -2
tail -1 gateway.jsonl                                   # usage_source, cost_usd, prompt redacted
python3 tokengate.py report gateway.jsonl
```

LiteLLM: `x-litellm-model-group`, `x-litellm-attempted-fallbacks`, `x-litellm-attempted-retries` and `x-litellm-response-cost` response headers show the same facts.

## 8. Report

State: callers and their budgets (RPM, TPM, `max_tokens` default) with the arithmetic against each provider limit and the headroom left; routes with their fallback chains and why each fallback is acceptable; retry, cooldown and streaming rules; cache scope, modes and TTL; prompt logging mode; the price table's date and source; the verification command and observed result for each rule; open risks (an unknown provider limit, a fallback with a smaller context, several gateway replicas each enforcing the full budget). Do not report a budget without the provider limit it was derived from.
