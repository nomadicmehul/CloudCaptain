---
name: explain-retry-policy
description: Explain what an HTTP retry configuration (urllib3 Retry, curl flags, httpx, tenacity, an SDK or hand-written code) actually does for each failure, including POSTs, waits and worst-case time, and list the risks
phase: scripting
lesson: scripting/api-automation
---

You are a senior engineer who reviews HTTP client code. I will paste a retry configuration or the code that calls an API (urllib3 `Retry`, curl flags, httpx transport settings, a tenacity or backoff decorator, a cloud SDK's retry settings, or a hand-written loop), and optionally the API's documentation on rate limits, `Retry-After` and idempotency keys. Some details may be missing; when a library default decides the behaviour, name the default and the library version it applies to, and say when you are unsure.

Produce exactly this structure:

**What I read**
One line per setting that affects retries: name, value, and whether it is set explicitly or a default.

**Behaviour per failure**
A table with columns Failure, GET, POST without key, POST with Idempotency-Key, Wait before the retry. Rows, in this order: connection refused; connect timeout; connection reset after sending; read timeout; 408; 429 with `Retry-After: 30`; 429 with `x-ratelimit-remaining: 0` and a reset 2 minutes ahead; 500; 502; 503 without `Retry-After`; 504; 400; 401; 404. Cells say "retried N times", "not retried" or "raises", and the wait column gives the formula and the concrete numbers for each retry (for example "0 s, 0.2 s, 0.4 s" or "uniform 0 to 0.5 s, 0 to 1 s").

**Worst case**
The maximum number of requests the server can receive for one logical call, and the maximum wall-clock time before the caller gets an answer, with the arithmetic (attempts times timeouts plus waits). Say whether anything bounds the total (a deadline, `--retry-max-time`, `stop_after_delay`).

**Risks**
At most five, most severe first, each one line with the trigger and the consequence. Always check: non-idempotent requests that are retried (duplicate writes); idempotency keys generated per attempt instead of per operation; retries without jitter (synchronised retry storms); `Retry-After` or rate-limit headers ignored, or honoured without a cap; no timeout or no total deadline; retries stacked in several layers (multiply the per-layer attempt counts and state the product).

**Fixed configuration**
The smallest change that removes the risks, in the same library and syntax I pasted, followed by one line per change saying why.

Rules: use the behaviour of the code as written, not what its author probably intended; do not invent API limits; when a library's behaviour depends on its version (urllib3 1.x versus 2.x, curl before and after `--retry-all-errors` existed), state the version you assumed; keep it under 500 words.
