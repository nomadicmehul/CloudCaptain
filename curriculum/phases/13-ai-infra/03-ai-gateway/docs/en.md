# An AI Gateway Is a Reverse Proxy That Counts Tokens

*Route by model, hold an estimate before the call and settle the real usage after, fall back before the first byte, and price every request.*

## Metadata

| Field | Value |
|---|---|
| Type | Build |
| Languages | Python |
| Prerequisites | [Serving a Model Is a Batch Scheduler With a KV Cache](https://cloudcaptain.io/learn/ai-infra/llm-serving), [Routing Is Listeners, Routes and Precedence: the Gateway API](https://cloudcaptain.io/learn/orchestration/gateway-api) |
| Time | 90 minutes |
| Tools | docker, llama.cpp, litellm, curl, envoy-ai-gateway |

<Quiz stage="pre" />

## The Problem

Monday, 09:00. The support team moved its nightly summariser to the morning, and it sends 4,000 chat requests in ten minutes through the one provider key the whole company shares. At 09:02 the key's tokens-per-minute limit is spent and the provider answers `429 Too Many Requests` to everybody: the summariser, the checkout assistant, the on-call bot. Every client's SDK retries on its own schedule, so the provider now sees three times the traffic while the limit is still spent. Meanwhile the self-hosted llama.cpp server on the GPU node is idle, because nothing in the path knows it is an acceptable substitute for the small hosted model.

At 11:00 finance forwards last month's invoice: one line per model, none per team. Security finds customer email addresses in one service's debug log, which printed whole prompts. Someone proposes nginx in front with `limit_req`. But nginx counts requests, and a request here can be 20 tokens or 20,000. The number the provider limits and bills is in the response's `usage` field, which exists only after the work is done, and for a streamed answer only in the last event.

An AI gateway is the reverse proxy that knows this. It reads `model` from the JSON body to pick a backend, holds an estimate of the request's tokens against the caller's budget before forwarding and settles to the real usage afterwards, retries or falls back while nothing has reached the client yet, counts the tokens of a stream it passes through untouched, answers exact repeats from a cache, and writes one log line per request with the prompt redacted and a price on it. You are going to build `tokengate`, which does all of that with the standard library, put it in front of a real llama.cpp server and a fake provider that returns 429 on demand, and then run LiteLLM with the same routes and fallbacks to watch the tool most teams deploy make the same decisions.

## The Concept

At a fuel pump the card is authorised for a fixed amount before any fuel flows, because nobody knows yet how much you will take. When you hang up the nozzle the hold is replaced by what you actually took. A token budget has the same problem. Before the call the gateway knows the prompt, roughly, and the most the model may write (`max_tokens`), but not what it will write. So it places a hold of prompt estimate plus `max_tokens` on the caller's budget, forwards the request, and when `usage` says the call took 117 tokens it gives the rest back. A request whose hold does not fit right now waits; one whose hold could never fit is refused outright, because waiting will not help.

The budget is a **token bucket** per key and per unit: one bucket for requests per minute (RPM), one for tokens per minute (TPM). A bucket holds at most its limit and refills continuously at limit/60 per second, so a key with 6,000 TPM regains 100 tokens every second instead of its whole budget at the top of each minute. Admission needs room in both buckets. A refusal says how long until there is room, which is the missing amount divided by the refill rate, and that number becomes the `retry-after` header. Settlement can push a bucket below zero when the estimate was low; the refill repays the debt before the next request fits. Anthropic documents its API limits as token buckets, and OpenAI and others publish the same `limit`, `remaining` and `reset` triple in response headers, so a gateway's budgets and a provider's limits are the same kind of object at two levels.

**Routing is by body, not by path.** Every request goes to `/v1/chat/completions`; the `model` field names a route. A route is an ordered list of deployments, each a backend plus the model name that backend knows: the primary first, then fallbacks. The gateway rewrites `model` to the deployment's name and replaces the caller's key with the backend's own credential, so callers never hold provider keys and a provider can be swapped without touching a client.

**Failures split by what a second attempt can change.** A `429` or `5xx` says nothing is wrong with the request itself, so the gateway may retry the same deployment after the provider's `retry-after` (or a short backoff) if that wait fits its own budget; otherwise it falls back to the next deployment and marks the first one as cooling down, so later requests skip it until the provider said it would recover. A `400` (context too long, unknown parameter) would fail the same way everywhere, or worse, succeed on a different model, so it goes back to the caller unchanged. All of this must happen **before the first byte**: once a stream has started, the caller holds part of an answer and nothing can be retried quietly.

**Streams are counted in passing.** A streamed answer is Server-Sent Events: `data: {...}` lines separated by blank lines, ending with `data: [DONE]`. OpenAI-compatible servers add one final event with `usage` only when the request carries `stream_options.include_usage: true`, so the gateway sets it on the way in, reads the usage on the way out, and drops that event again if the caller had not asked for it. When a server reports nothing, counting characters in the deltas is the fallback.

**Cache, logs and price.** An exact-match cache keys on the canonical body (minus transport fields such as `stream` and `user`) and the caller's key, and by default stores only `temperature: 0` answers, because caching a sampled answer turns randomness into a constant. Every request leaves one JSON line: who, which route and deployment, every attempt, tokens and whether they were reported or estimated, latency, a price from a dated price table, and the prompt with emails, keys, card numbers and IP addresses masked.

```mermaid
flowchart LR
  C["client: POST /v1/chat/completions, Bearer key"] --> A["authenticate: sha256 of the key"]
  A --> R["route by body.model"]
  R --> B["buckets per key: requests, tokens"]
  B -->|"no room"| L["429, retry-after, x-ratelimit-*"]
  B -->|"hold: prompt estimate + max_tokens"| K["exact-match cache"]
  K -->|"hit"| H["cached body, hold refunded"]
  K -->|"miss"| P["primary deployment"]
  P -->|"429 or 5xx before the first byte"| F["fallback deployment"]
  P --> S["JSON or SSE passed through"]
  F --> S
  S -->|"usage: prompt + completion tokens"| T["settle the hold, price it"]
  T --> G["one JSON log line, prompt redacted"]
```

## Build It

`code/main.py` is `tokengate`. The policy core (config, buckets, estimates, cache keys, redaction, prices) is plain functions and small classes. `Gateway` ties them together over an injectable transport, clock and sleep, so the tests can script "429, then 200" without waiting a second; `GatewayServer` is `http.server` in front and `urllib` behind. The same file holds `fake`, an OpenAI-compatible backend you can tell to fail, plus `check`, `plan` and `report` for a config, a single request and the logs. `code/fixtures/gateway.json` is the configuration used below.

### Step 1: routes are lists, keys are hashes

```json
"models": {
  "local": [{"backend": "llama", "model": "smollm2"}],
  "fake": [{"backend": "fake", "model": "fake-small"}],
  "chat": [{"backend": "fake", "model": "fake-small"}, {"backend": "llama", "model": "smollm2"}]
},
"keys": [
  {"name": "team-a", "key": "sk-cc-team-a", "rpm": 120, "tpm": 20000},
  {"name": "tight", "key": "sk-cc-tight", "rpm": 60, "tpm": 300, "models": ["fake", "chat"]}
]
```

`chat` is the hosted model with the self-hosted one as its fallback. Keys are stored as SHA-256 digests (`key_sha256` in a real config; `key` is accepted for the lab and hashed on load), and a request is matched by hashing its bearer token, so the config, the logs and `check` never contain a secret:

```python
    def authenticate(self, authorization: str) -> Optional[KeyPolicy]:
        scheme, _, token = (authorization or "").partition(" ")
        if scheme.lower() != "bearer" or not token.strip():
            return None
        return self.config.keys.get(fingerprint(token.strip()))
```

No key is a `401`, a route that does not exist is a `404 model_not_found`, a key whose `models` list omits the route is a `403`, and `GET /v1/models` lists only what the caller may use. Each backend may name `api_key_env`; the gateway sends that credential upstream and never forwards the caller's `Authorization` header.

### Step 2: estimate before you know

```python
def estimate_prompt_tokens(body: Dict[str, Any]) -> int:
    if "messages" in body:
        messages = [m for m in body.get("messages") or [] if isinstance(m, dict)]
        return REPLY_PRIMING_TOKENS + sum(MESSAGE_OVERHEAD_TOKENS + estimate_text_tokens(len(text_of(m.get("content")))) for m in messages)
    return estimate_text_tokens(len(prompt_text(body)))


def requested_output_tokens(body: Dict[str, Any], default: int) -> int:
    for name in ("max_completion_tokens", "max_tokens"):
        value = body.get(name)
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
            return value
    return default


def estimate_tokens(body: Dict[str, Any], default_max_tokens: int) -> int:
    choices = body.get("n") if isinstance(body.get("n"), int) and body.get("n") > 0 else 1
    return estimate_prompt_tokens(body) + choices * requested_output_tokens(body, default_max_tokens)
```

Four characters per token is a rule of thumb for English text; code, other languages and other tokenizers differ, and that is fine because the estimate is only a hold. A high estimate is refunded seconds later; a low one becomes a small debt. What matters is `max_tokens`: a client that omits it gets `default_max_tokens` (256 here) held on every call, which is why gateways and providers alike tell you to set it close to the answer you expect. OpenAI's rate-limit guide says its own limit counts the larger of `max_tokens` and a character-based estimate; `tokengate` holds the sum, the more conservative choice.

### Step 3: a token bucket

```python
    def refill(self, now: float) -> None:
        if now > self.updated:
            self.level = min(self.capacity, self.level + (now - self.updated) * self.per_second)
            self.updated = now

    def wait_for(self, amount: float, now: float) -> float:
        self.refill(now)
        return 0.0 if self.level >= amount else (amount - self.level) / self.per_second
```

`Bucket.per_minute(300, now)` starts full with 300 and refills 5 per second. There is no window and no reset at the top of the minute, so a burst cannot get a full budget at 10:00:59 and another at 10:01:00. `adjust()` adds or removes an amount and caps at capacity but not at zero, which is how a settlement larger than the hold turns into debt.

### Step 4: hold, refuse, settle

```python
        needs = {"requests": 1, "tokens": estimate}
        blocked = {kind: bucket.wait_for(needs[kind], now) for kind, bucket in buckets.items()}
        blocked = {kind: wait for kind, wait in blocked.items() if wait > 0}
        if blocked:
            kind = max(blocked, key=lambda k: blocked[k])
            bucket = buckets[kind]
            unit = "RPM" if kind == "requests" else "TPM"
```

Both buckets are checked before either is charged, so a refusal on tokens does not also spend a request. The refusal names the bucket that waits longest, and the `429` carries `retry-after` (whole seconds, rounded up), `x-tokengate-limited-by: tokens` and the key's `x-ratelimit-*` headers in OpenAI's names. After the call:

```python
    def settle(self, policy: KeyPolicy, estimate: int, actual: int) -> None:
        now = self.clock()
        bucket = self.buckets_for(policy, now).get("tokens")
        if bucket is not None:
            bucket.adjust(estimate - actual, now)
```

The numbers in the walk-through below: the `tight` key has 300 TPM; a 100-word prompt with `max_tokens: 16` is held at 148 tokens and actually uses 117 (101 in, 16 out). After two calls the bucket holds 66, the third needs 148, and `(148 - 66) / 5 = 16.4` seconds becomes `retry-after: 17`. A request held at more than the whole bucket (`max_tokens: 400` against 300 TPM) is a `400 request_too_large`, not a `429`: SDKs retry 429s automatically, and retrying this one can never succeed.

### Step 5: retry, cool down, fall back

`provider_wait()` turns a backend's headers into seconds: `retry-after-ms`, then `retry-after` (seconds or an HTTP date), then the `x-ratelimit-reset-*` duration (`6m0s`, `20ms`) of a bucket whose `remaining` is `0`. The loop over deployments uses it:

```python
            for attempt in range(self.config.retries + 1):
                started = self.clock()
                reply = self.transport(url, payload, headers, timeout)
                wait = provider_wait(reply.headers, self.wall())
                attempts.append(Attempt(deployment.label, reply.status, round((self.clock() - started) * 1000, 1), wait, reply.error))
                if reply.status not in RETRYABLE:
                    return CallResult(deployment, reply, attempts, injected)
                reply.read()
                last = reply
                delay = wait if wait is not None else self.config.backoff_seconds * (2 ** attempt)
                if attempt < self.config.retries and delay <= self.config.max_retry_wait:
                    self.sleep(delay)
                    continue
                if reply.status == 429:
                    self.cool_down(deployment, wait if wait is not None else self.config.cooldown_seconds)
                break
```

| Upstream answer | tokengate |
|---|---|
| 2xx | forward it, settle from `usage` |
| 429 with `retry-after` within `max_wait_seconds` | sleep that long and retry the same deployment, up to `retries` times |
| 429 with a longer wait, or retries used up | cool the deployment down for `retry-after` (or `cooldown_seconds`), try the next one |
| 5xx or connection refused | back off 0.2 s, 0.4 s, ..., then the next deployment |
| 400, 401, 404 from the backend | return it to the client, no fallback |
| every deployment failed | `429` with the shortest `retry-after` seen, or `502`; the hold is refunded |

Retryable is `UNREACHABLE` (connect errors), `429`, `500`, `502`, `503` and `504`. The cooldown is per deployment, not per route: `fake/fake-small` is shared by the `fake` and `chat` routes, and a 429 seen through either cools it for both. `x-tokengate-attempts: fake/fake-small:429,llama/smollm2:200` on the response shows the path a request took.

### Step 6: streams pass through and still count

```python
        if stream:
            options = dict(payload.get("stream_options") or {})
            if not options.get("include_usage"):
                options["include_usage"] = True
                injected = True
            payload["stream_options"] = options
```

`relay_stream()` reads the upstream response line by line, groups lines into events at each blank line, and writes each event to the client as soon as it is complete, flushing every time. `inspect_event()` looks at each one on the way past:

```python
    found = usage_tokens(chunk.get("usage"))
    if found:
        tally.usage = found
    choices = chunk.get("choices") or []
```

It adds up the characters of `delta.content` (and `reasoning_content`, and `text` for `/v1/completions`), records the time of the first content delta as time to first token, and notes `finish_reason`. Comment lines such as llama.cpp's `: ping` keep-alives are forwarded untouched. The event with `usage` and an empty `choices` list is dropped only when the gateway added `include_usage` itself. If the client hangs up, the relay stops reading and closes the upstream connection, which is what makes a server stop generating for nobody. The response headers leave before any token is counted, so a stream's cost appears in the log and in `GET /tokengate/usage`, not in an `x-tokengate-cost-usd` header as it does for a JSON answer.

### Step 7: an exact-match cache

```python
def cache_decision(body: Dict[str, Any], mode: str, cache_control: str = "") -> str:
    if mode == "off":
        return "off"
    if body.get("stream"):
        return "skip:stream"
    if "no-cache" in cache_control or "no-store" in cache_control:
        return "skip:no-cache"
    if body.get("n", 1) != 1:
        return "skip:n"
    if mode == "deterministic" and body.get("temperature") != 0:
        return "skip:temperature"
    return "use"


def cache_key(route: str, body: Dict[str, Any], scope: str) -> str:
    material = {k: v for k, v in body.items() if k not in IGNORED_IN_CACHE_KEY}
    material["model"] = route
    canonical = json.dumps(material, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(f"{scope}\n{canonical}".encode()).hexdigest()
```

Sorting keys and fixing separators makes two requests that differ only in JSON spacing or field order hit the same entry. The scope is the key's name by default: a cache shared between tenants returns one tenant's answer to another, and even its timing tells a second tenant that someone asked the same thing. A hit refunds the hold, costs nothing, and logs `saved_usd` with what the miss cost. Entries expire after `ttl_seconds` and the least recently used one is evicted beyond `max_entries`. Admission runs before the lookup, so a key that is out of tokens is refused even for a repeat the cache could answer; moving the lookup first is a one-line change and a policy decision.

### Step 8: logs you can keep, with a price on them

```python
REDACTIONS = [
    ("EMAIL", re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")),
    ("API_KEY", re.compile(r"\b(?:sk|pk|rk)-[A-Za-z0-9_-]{8,}")),
    ("AWS_KEY", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("CARD", re.compile(r"\b(?:\d[ -]?){12,18}\d\b")),
    ("IP", re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")),
]
```

`log.prompts` is `hash` (only a SHA-256 prefix and the length), `redacted` (the patterns above masked, the default) or `full`. Pattern redaction catches formats, not meaning: a name, an address or a diagnosis written in prose passes through, which is why `hash` is the right default for anything regulated. The price comes from `fixtures/prices.json`, which must carry a `date` and a `source` or the config does not load:

```python
    def cost(self, prompt_tokens: int, completion_tokens: int) -> float:
        return round((prompt_tokens * self.input_per_mtok + completion_tokens * self.output_per_mtok) / 1_000_000, 10)
```

Prices are per million tokens and per deployment (`llama/smollm2`), so the same route costs different amounts depending on which deployment answered; a fallback to a pricier provider shows up as a cost change in the log, not as a surprise on the invoice.

### Step 9: run it

Two fakes stand in for the backends here (in Use It the one on 8081 becomes llama.cpp), and the gateway listens on 4000:

```bash
python3 code/main.py check --config code/fixtures/gateway.json
python3 code/main.py plan --config code/fixtures/gateway.json --key team-a code/fixtures/request-chat.json
python3 code/main.py fake --name llama --model smollm2 --port 8081 &
python3 code/main.py fake --name fake --model fake-small --port 8082 &
python3 code/main.py serve --config code/fixtures/gateway.json --port 4000 --log gateway.jsonl &
K='Authorization: Bearer sk-cc-team-a'
curl -s -H "$K" -d '{"model": "local", "messages": [{"role": "user", "content": "Say hi"}], "max_tokens": 4}' localhost:4000/v1/chat/completions
curl -sN -H "$K" -d '{"model": "fake", "stream": true, "messages": [{"role": "user", "content": "Say hi"}], "max_tokens": 3}' localhost:4000/v1/chat/completions
PROMPT="$(printf 'word %.0s' $(seq 100))"
for i in 1 2 3; do curl -s -o /dev/null -w '%{http_code} ' -H 'Authorization: Bearer sk-cc-tight' -d "{\"model\": \"fake\", \"messages\": [{\"role\": \"user\", \"content\": \"$PROMPT\"}], \"max_tokens\": 16}" localhost:4000/v1/chat/completions; done
for i in 1 2; do curl -s -o /dev/null -D - -H "$K" -d '{"model": "fake", "temperature": 0, "messages": [{"role": "user", "content": "What is a 429?"}], "max_tokens": 8}' localhost:4000/v1/chat/completions | grep -i x-tokengate-cache; done
curl -s -X POST -d '{"status": 429, "times": 1, "retry_after": "30"}' localhost:8082/fake/fail
curl -s -o /dev/null -D - -H "$K" -d '{"model": "chat", "messages": [{"role": "user", "content": "Say hi"}], "max_tokens": 4}' localhost:4000/v1/chat/completions | grep -i x-tokengate-attempts
curl -s -H "$K" localhost:4000/tokengate/usage
python3 code/main.py report gateway.jsonl
kill %1 %2 %3
```

`plan` shows what the gateway would do without sending anything: `deployments in order: fake/fake-small, llama/smollm2`, `estimate: prompt ~55 + output 64 = 119 tokens`, `cache: use`, and the prompt as it would be logged, `...reported by [EMAIL] from [IP]; her token [API_KEY] is in the ticket.` The `tight` loop prints `200 200 429`; the 429's body says `Rate limit reached for key tight on tokens per min (TPM): Limit 300, Remaining 66, Requested 148. Please try again in 17s.` The cache prints `miss` then `hit`, and the fallback prints `x-tokengate-attempts: fake/fake-small:429,llama/smollm2:200`. The gateway's stderr has one access line per request:

```text
tokengate 200 key=tight model=fake -> fake/fake-small in=101 out=16 cost=$0.00002475 cache=skip:temperature attempts=fake/fake-small:200 1ms
tokengate 429 key=tight model=fake -> - in=0 out=0 cost=- cache=- attempts=- 0ms
tokengate 200 key=team-a model=fake -> fake/fake-small in=0 out=0 cost=$0.00000000 cache=hit attempts=- 0ms
tokengate 200 key=team-a model=chat -> llama/smollm2 in=3 out=4 cost=$0.00000055 cache=skip:temperature attempts=fake/fake-small:429,llama/smollm2:200 1ms
```

and `report` turns the JSON lines into a chargeback table per key and model (requests, 429s, errors, cache hits, fallbacks, tokens, cost, p50 and p95 latency). Exit codes: 0 for success, 2 for a config or input error with the field named, 1 for an OS error such as a port in use.

<RunTests />

## Use It

Now the real backend and the real gateways. First a model server: llama.cpp's server runs a GGUF model on CPU and implements `/v1/chat/completions`, `/v1/completions`, `/v1/models`, `/health` and, with `--metrics`, Prometheus `/metrics`. It honours `stream_options.include_usage`. The image and model are the ones the serving lesson pins: build tag `server-b11706` (index digest `sha256:f41d47eee678c02656eefc9ed8cce76cab584aa7032805eeef0e6639fcfda485`) and SmolLM2-135M-Instruct in Q8_0, pinned by Hugging Face revision and SHA-256, exactly as you would pin an image by digest:

```bash
mkdir -p models
REV=09816acd5d99df7be770d85ea30822623dab342c
curl -fL -o models/SmolLM2-135M-Instruct-Q8_0.gguf \
  "https://huggingface.co/bartowski/SmolLM2-135M-Instruct-GGUF/resolve/$REV/SmolLM2-135M-Instruct-Q8_0.gguf"
echo "5a1395716f7913741cc51d98581b9b1228d80987a9f7d3664106742eb06bba83  models/SmolLM2-135M-Instruct-Q8_0.gguf" | sha256sum -c -   # macOS: shasum -a 256 -c -
docker run -d --name llama -p 8081:8080 -v "$PWD/models:/models:ro" ghcr.io/ggml-org/llama.cpp:server-b11706 \
  -m /models/SmolLM2-135M-Instruct-Q8_0.gguf --alias smollm2 --ctx-size 4096 --parallel 2 --metrics
curl -s localhost:8081/health                                  # {"status":"ok"} once the model is loaded
curl -s localhost:8081/v1/models
```

SmolLM2-135M is about 145 MB and answers in milliseconds on a laptop CPU; the answers are poor, which does not matter for a gateway. Stop the fake on 8081, keep the one on 8082, and run the same gateway in front of the real server:

```bash
python3 code/main.py serve --config code/fixtures/gateway.json --port 4000 --log gateway.jsonl &
curl -s -H "$K" -d '{"model": "local", "messages": [{"role": "user", "content": "Name one Linux signal."}], "max_tokens": 24}' localhost:4000/v1/chat/completions
curl -sN -H "$K" -d '{"model": "local", "stream": true, "messages": [{"role": "user", "content": "Count to five."}], "max_tokens": 24}' localhost:4000/v1/chat/completions
tail -1 gateway.jsonl          # "usage_source": "reported": llama.cpp sent the usage event tokengate asked for, and the client never saw it
curl -s localhost:8081/metrics | grep -E '^llamacpp:(prompt_tokens_total|prompt_tokens_cached_total|tokens_predicted_total)'
```

The token counts are now llama.cpp's own tokenizer's. The server's `llamacpp:tokens_predicted_total` counter moves by the completion tokens the gateway billed. Prompt tokens split in two on the server: llama.cpp reuses the KV cache of a prefix it has seen, counts those under `prompt_tokens_cached_total` rather than `prompt_tokens_total`, and reports them in `usage.prompt_tokens_details.cached_tokens`, which is the number a cache-aware price table would discount.

**LiteLLM** is the gateway most teams run. Install a pinned release in a virtual environment (it needs Python 3.10 or later), then start the proxy with `code/fixtures/litellm.yaml`, which declares the same three routes:

```yaml
model_list:
  - model_name: local
    litellm_params:
      model: openai/smollm2
      api_base: os.environ/LLAMA_API_BASE
      api_key: none
  - model_name: chat
    litellm_params:
      model: openai/fake-small
      api_base: os.environ/FAKE_API_BASE
      api_key: os.environ/FAKE_API_KEY
router_settings:
  num_retries: 0
  allowed_fails: 1
  cooldown_time: 30
  fallbacks: [{"chat": ["local"]}]
general_settings:
  master_key: os.environ/LITELLM_MASTER_KEY
```

```bash
python3 -m venv .venv && .venv/bin/pip install 'litellm[proxy]==1.106.6'
export LLAMA_API_BASE=http://127.0.0.1:8081/v1 FAKE_API_BASE=http://127.0.0.1:8082/v1 FAKE_API_KEY=sk-fake-upstream LITELLM_MASTER_KEY=sk-cc-litellm-master
.venv/bin/litellm --config code/fixtures/litellm.yaml --host 127.0.0.1 --port 4001 &
curl -s localhost:4001/health/liveliness
L='Authorization: Bearer sk-cc-litellm-master'
curl -s -H "$L" -H 'Content-Type: application/json' -d '{"model": "local", "messages": [{"role": "user", "content": "Say hi"}], "max_tokens": 8}' localhost:4001/v1/chat/completions
curl -s -X POST -d '{"status": 429, "times": 2, "retry_after": "30"}' localhost:8082/fake/fail
curl -s -D - -o /dev/null -H "$L" -H 'Content-Type: application/json' -d '{"model": "chat", "messages": [{"role": "user", "content": "Say hi"}], "max_tokens": 8}' localhost:4001/v1/chat/completions | grep -i -E 'x-litellm-(model-group|model-api-base|attempted-fallbacks)'
```

The fallback answer carries `x-litellm-model-group: local`, `x-litellm-model-api-base: http://127.0.0.1:8081/v1` and `x-litellm-attempted-fallbacks: 1`, the same story as `x-tokengate-attempts`. With `num_retries: 1` instead of 0, LiteLLM 1.106.6 honoured the fake's `retry-after: 30` before retrying, and in our runs the fallback answered only after about twice that delay: a provider that says "come back in 30 seconds" turns into a one-minute request. `tokengate` makes that a setting (`max_wait_seconds`): it waits for short retry-afters and falls back at once for long ones.

Per-key limits in LiteLLM are virtual keys, which need its database (`DATABASE_URL` pointing at Postgres); without one the proxy knows only its master key. With a database, `POST /key/generate` with `{"models": ["chat"], "tpm_limit": 300, "rpm_limit": 60, "max_budget": 5}` returns a key, LiteLLM stores its SHA-256 hash like `tokengate` does, and the limiter in current releases reserves the input estimate plus `max_tokens` before the call and reconciles from usage afterwards: the same hold and settle, counted in per-minute windows (in memory, or in Redis when several proxy replicas must share one budget) rather than in a continuously refilled bucket.

**Envoy AI Gateway** is the Kubernetes-native version, built on Envoy Gateway and the Gateway API from the prerequisite lesson. Its external processor reads `model` from the body and sets the `x-ai-eg-model` header before Envoy routes, so routing by model is an ordinary header match; `priority` on `backendRefs` orders fallbacks; `llmRequestCosts` names the usage numbers to extract from responses:

```yaml
apiVersion: aigateway.envoyproxy.io/v1beta1
kind: AIGatewayRoute
metadata:
  name: chat
spec:
  parentRefs:
    - name: ai-gateway
      kind: Gateway
      group: gateway.networking.k8s.io
  rules:
    - matches:
        - headers:
            - type: Exact
              name: x-ai-eg-model
              value: chat
      backendRefs:
        - name: hosted-provider
          priority: 0
        - name: llama-cpp
          priority: 1
  llmRequestCosts:
    - metadataKey: llm_total_token
      type: TotalToken
```

The token budget is an Envoy Gateway `BackendTrafficPolicy` global rate limit per tenant header whose request cost is `0` and whose response cost is read from the metadata key `llm_total_token` in the namespace `io.envoy.ai_gateway`. That is a different mechanism from the hold: the request path only checks that budget remains, and the real usage is deducted when the response finishes, so a tenant can overshoot by one request's worth but is never charged an estimate. Each `AIServiceBackend` declares the provider's API schema (`OpenAI`, `AWSBedrock` and others) and the gateway translates between them; a `BackendSecurityPolicy` holds the upstream credential, the job of `api_key_env` here.

**The Gateway API Inference Extension** answers a different question: not which provider or model, but which replica of a self-hosted model server. An `InferencePool` (`inference.networking.k8s.io/v1`) selects model-server pods and is used as an HTTPRoute `backendRef`; an endpoint picker consulted over Envoy's external processing picks the pod by queue depth and KV-cache state. The endpoint picker and the body-based model routing have moved to the llm-d project, while the `InferencePool` API stays in the Kubernetes SIG repository. A typical stack is both: an AI gateway at the edge (keys, budgets, providers, fallbacks, cost) whose self-hosted deployment is an inference gateway in front of the pool.

**Provider headers map onto your buckets.** When a provider is the bottleneck, its headers tell you how much of its budget is left, and a gateway should obey them rather than discover the limit by failing:

| Header | Who sends it | Meaning | In tokengate |
|---|---|---|---|
| `retry-after` | most providers, RFC 9110 | seconds, or an HTTP date, before trying again | wait if within `max_wait_seconds`, else cool down and fall back |
| `retry-after-ms` | some providers; the OpenAI SDKs read it | the same in milliseconds | preferred when present |
| `x-ratelimit-limit-tokens`, `x-ratelimit-remaining-tokens`, `x-ratelimit-reset-tokens` | OpenAI and compatible servers | the bucket's size, what is left, time until full (`6m0s`) | reset used as the wait when remaining is `0`; tokengate sends the same names for its own buckets |
| `x-ratelimit-*-requests` | OpenAI and compatible servers | the same for requests per minute | as above |
| `anthropic-ratelimit-tokens-remaining`, `anthropic-ratelimit-tokens-reset` (and `-input-tokens-`, `-output-tokens-`, `-requests-`) | Anthropic | remaining (rounded to the nearest thousand) and an RFC 3339 time of full replenishment | not parsed yet (the Easy exercise) |

Set the sum of your keys' TPM below the provider's limit for that model, with headroom for retries, or your own buckets will happily admit traffic the provider then refuses.

| What you built | Real tool | Note |
|---|---|---|
| routes as ordered deployment lists | LiteLLM `model_list` groups and `fallbacks`; Envoy AI Gateway rules with `backendRefs` `priority` | routing by body needs the proxy to parse JSON; Envoy AI Gateway copies `model` into `x-ai-eg-model` first |
| SHA-256 key lookup, per-key `models` | LiteLLM virtual keys (`/key/generate`, stored hashed, needs Postgres) | clients never see provider keys in either |
| `Bucket`, `Limiter.admit()` and `settle()` | LiteLLM `tpm_limit` and `rpm_limit` per key; Envoy Gateway `BackendTrafficPolicy` with response costs | hold and settle in LiteLLM; check then charge on response in Envoy AI Gateway |
| `400 request_too_large` | provider "request too large" errors | a request bigger than the budget must not look retryable |
| `call()`: retry, cooldown, fallback | LiteLLM `num_retries`, `allowed_fails`, `cooldown_time`, `fallbacks` | LiteLLM waits out `retry-after` on retries; tokengate caps the wait |
| `include_usage` injection, `relay_stream()` | both stream SSE through and count usage | retries are only possible before the first byte, in every implementation |
| `cache_decision()`, `cache_key()` | LiteLLM `cache: true` with `cache_params` (in-memory or Redis) | scope and temperature rules are yours to decide |
| `REDACTIONS`, `log.prompts` | LiteLLM `turn_off_message_logging`, guardrail integrations for PII | pattern redaction misses prose; hash when in doubt |
| `prices.json` with date and source, `cost_usd` | LiteLLM's model cost map and `x-litellm-response-cost`; Envoy `llmRequestCosts` (tokens, not money) | a price without a date is a guess |
| `report` per key and model | LiteLLM spend tracking per key and team (needs the database) | chargeback is a join of logs and prices |

<Quiz stage="check" />

## Ship It

Two artifacts in `outputs/`:

- **`skill-ai-gateway-policy.md`**: a runbook for putting an AI gateway in front of model providers and self-hosted servers, or reviewing one that is already there: inventory callers and their traffic, derive per-key RPM and TPM from the provider's limits with headroom, define routes and fallback chains, set retry, cooldown and first-byte rules, decide the cache and logging policy, attach a dated price table, verify each rule with `tokengate plan` or the LiteLLM equivalent, and report with a fixed structure.
- **`prompt-design-rate-limits.md`**: paste the provider limits, the callers with their request rates and token sizes, and the models and fallbacks available; get back per-key budgets with the arithmetic shown, `max_tokens` defaults, retry and fallback rules, the headers clients should read, the alerts to set, and the risks the numbers leave open.

<ShipIt />

## Exercises

### Easy

Teach `provider_wait()` Anthropic's headers: when `anthropic-ratelimit-tokens-remaining` (or the `-requests-`, `-input-tokens-`, `-output-tokens-` variants) is `0`, the wait is the matching `-reset` RFC 3339 timestamp minus now. Write the tests first: a 429 with only those headers and a reset 12 seconds ahead must give a wait of 12 seconds, a non-zero remaining must be ignored, and `retry-after` must still win when both are present.

### Medium

Add money budgets: `max_budget_usd` per key per UTC day. Hold the worst-case cost of the estimate (the most expensive deployment in the route, priced from `prices.json`) alongside the token hold, settle it to the real cost, and refuse with `402` and the time the day resets once the budget is spent. Tests first: a key with a $0.0001 budget is refused on the request that would cross it, a cache hit costs nothing against the budget, and a fallback to a cheaper deployment settles the cheaper price.

### Hard

Run two `tokengate` replicas behind one address and show that each enforces the full TPM, so the pair admits double. Then fix it two ways and compare: a shared bucket service (a third small HTTP server with atomic `admit` and `settle` endpoints that both replicas call, with a timeout and a fail-open or fail-closed choice you justify), and static splitting (each of N replicas gets limit/N). Write the test that sends the same burst through both replicas and asserts the combined admitted tokens stay within the limit plus one hold, and measure what each design costs in added latency.

<Quiz stage="post" />

## Key Terms

| Term | Common misconception | Precise definition |
|---|---|---|
| AI gateway | "An API gateway with an LLM plugin." | A reverse proxy whose routing key is the `model` field in the body and whose unit of account is the token: per-key budgets, provider fallbacks, usage accounting, caching, redacted logs and cost per request. |
| Token bucket | "A counter that resets every minute." | A balance with a capacity (the per-minute limit) that refills continuously at limit/60 per second; admission takes from it, and the wait for a refused amount is the shortfall over the refill rate. |
| Hold and settle | "The gateway knows the token count before the call." | Charging prompt estimate plus `max_tokens` before forwarding, then adjusting to the response's `usage`: refunds for over-estimates, debt for under-estimates. |
| TPM and RPM | "Only requests matter for capacity." | Tokens per minute and requests per minute, enforced separately; long prompts exhaust TPM long before RPM, and providers bill tokens. |
| Retry versus fallback | "Retry everything until it works." | Retrying repeats the same deployment after a wait the provider named; falling back moves to the next deployment in the route. Both apply to 429 and 5xx only, and only before the first byte reaches the client. |
| Cooldown | "A deployment that returned 429 is broken." | A period, from `retry-after` or a default, during which the gateway skips a rate-limited deployment for every route that uses it, so it is not hammered while it recovers. |
| `stream_options.include_usage` | "Streamed responses always report usage." | The request option that makes an OpenAI-compatible server send one final event with `usage` and empty `choices`; without it a stream carries no token counts. |
| Exact-match cache | "Caching LLM answers is always safe." | Replaying a stored response for a byte-identical canonical request; safe for deterministic requests within one tenant, and wrong for sampled answers or across tenants. |
| Redaction | "Masked logs contain no personal data." | Replacing known patterns (emails, keys, card numbers, IP addresses) before writing; it does not catch names or facts in prose, so hashing is the safe default. |

## Further Reading

- [Nginx Fundamentals](https://cloudcaptain.io/docs/tools/nginx/fundamentals): reverse proxying, upstreams, caching and `limit_req`, the request-counting gateway this lesson starts from.
- [AI/ML Ops learning path](https://cloudcaptain.io/docs/learning-paths/ai-ml): where gateways sit next to serving, evaluation and token-cost engineering.
- [Observability & SRE](https://cloudcaptain.io/docs/tools/observability): turning the gateway's per-request lines into dashboards and alerts.
- [Kubernetes Networking & Services](https://cloudcaptain.io/docs/tools/kubernetes/networking-services): Services and the Gateway API that Envoy AI Gateway builds on.
- [OpenAI rate limits guide](https://platform.openai.com/docs/guides/rate-limits): the `x-ratelimit-*` headers, the duration format and how `max_tokens` counts against TPM.
- [Anthropic rate limits](https://docs.claude.com/en/api/rate-limits): the token bucket model and the `anthropic-ratelimit-*` headers with RFC 3339 resets, for the Easy exercise.
- [RFC 9110, Retry-After](https://www.rfc-editor.org/rfc/rfc9110#field.retry-after): the two legal forms of the header `provider_wait()` parses.
- [Server-sent events (WHATWG HTML)](https://html.spec.whatwg.org/multipage/server-sent-events.html): the event stream format, including comment lines and multi-line `data`.
- [llama.cpp server README](https://github.com/ggml-org/llama.cpp/blob/master/tools/server/README.md): every flag and endpoint used in Use It, including `--metrics` and the OpenAI-compatible API.
- [LiteLLM proxy: fallbacks and retries](https://docs.litellm.ai/docs/proxy/reliability) and [virtual keys and budgets](https://docs.litellm.ai/docs/proxy/users): the real versions of `call()` and `Limiter`.
- [Envoy AI Gateway: usage-based rate limiting](https://aigateway.envoyproxy.io/docs/capabilities/traffic/usage-based-ratelimiting): `llmRequestCosts` and response-cost rate limits from the project.
- [Gateway API Inference Extension](https://gateway-api-inference-extension.sigs.k8s.io/): `InferencePool` and the endpoint picker, the layer behind the AI gateway.
