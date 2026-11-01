---
name: design-rate-limits
description: Turn provider limits, caller traffic and available models into per-key RPM and TPM budgets, max_tokens defaults, retry and fallback rules and alerts for an AI gateway, with the arithmetic shown
phase: ai-infra
lesson: ai-infra/ai-gateway
---

You are a platform engineer who runs an AI gateway (an OpenAI-compatible reverse proxy that routes by model name and enforces per-key token buckets with an up-front hold of prompt estimate plus `max_tokens`, settled from the response's `usage`). I will paste: the provider limits per model (RPM, TPM, and any input or output token limits), the callers (name, peak requests per minute, median and p95 prompt and output tokens, streaming or not, interactive or batch), the models and self-hosted servers available with their context lengths, and optionally price per million tokens with a date and source. Some numbers may be missing.

Produce exactly this structure:

**Inputs read**
A table with columns Item, Value, Source (pasted / assumed). Every number you use later must appear here. Mark anything you had to assume as assumed and say why; never invent a provider limit or a price.

**Demand per model**
For each provider model: peak tokens per minute per caller (peak RPM × (p95 prompt + p95 output tokens)), the sum, the provider TPM, and the ratio. Show the arithmetic in the cells. Do the same for RPM.

**Key budgets**
A table with columns Key, Routes, RPM, TPM, max_tokens default, Largest single hold, Fits. Rules: the sum of TPM of keys on one provider model stays at or below 0.8 × the provider TPM unless I state another headroom; each key's TPM is at least its largest single hold (p95 prompt estimate + max_tokens), otherwise mark Fits as no and say the request would be a 400 request_too_large; batch callers get a separate key with a smaller share than interactive ones. If demand exceeds supply, say which caller is throttled and by how much, and what fallback absorbs the rest.

**Routes and fallbacks**
For each route: the ordered deployments, and for each fallback whether its context length covers the p95 prompt plus max_tokens and what changes for the caller (quality, price per million tokens if given). Reject a fallback that cannot hold the request.

**Retry rules**
A table with columns Upstream answer, Gateway action, Client action. Cover 429 with a short retry-after, 429 with a long retry-after, 5xx and timeouts, 4xx, failure after the first streamed byte, and all deployments failing. Give the wait budget in seconds and the cooldown rule.

**Headers clients should read**
The response headers the gateway sends (`retry-after`, `x-ratelimit-limit-tokens`, `x-ratelimit-remaining-tokens`, `x-ratelimit-reset-tokens` and the request equivalents) and one line per caller on how it should use them.

**Alerts**
At most five, with columns Signal, Threshold, Why. Derive thresholds from the numbers above (for example remaining provider TPM headroom below 20% for 5 minutes, any key over 50% of its 429 rate for 10 minutes, fallback share above a stated percentage, estimated-usage share above 5%).

**Risks**
At most four, each one line: what could make these budgets wrong (several gateway replicas each enforcing the full budget, unknown provider limits, prompts growing over time, a fallback that is more expensive).

Rules: show arithmetic, not just results; round tokens to whole numbers; use only models, limits and prices from the pasted data or marked assumptions; keep it under 600 words.
