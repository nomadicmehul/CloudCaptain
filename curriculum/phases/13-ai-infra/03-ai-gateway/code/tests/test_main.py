import io
import json
import os
import shutil
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from contextlib import redirect_stderr, redirect_stdout
from email.utils import formatdate
from io import StringIO
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import main  # noqa: E402

FIXTURES = os.path.join(os.path.dirname(__file__), "..", "fixtures")
CONFIG = os.path.join(FIXTURES, "gateway.json")
REQUEST = os.path.join(FIXTURES, "request-chat.json")


def raw_config(**changes):
    with open(CONFIG, encoding="utf-8") as handle:
        raw = json.load(handle)
    for key, value in changes.items():
        raw[key] = value
    return raw


def config(overrides=None, **changes):
    return main.load_config(raw_config(**changes), FIXTURES, overrides)


def chat(model="chat", text="hello there", **extra):
    return dict({"model": model, "messages": [{"role": "user", "content": text}]}, **extra)


class Clock:
    def __init__(self, now=1000.0):
        self.now = now

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


class Writer:
    def __init__(self):
        self.status = None
        self.headers = {}
        self.body = b""
        self.chunks = []

    def respond(self, status, headers, body):
        self.status, self.headers, self.body = status, {k.lower(): v for k, v in headers}, body

    def start_stream(self, status, headers):
        self.status, self.headers = status, {k.lower(): v for k, v in headers}

    def write_chunk(self, chunk):
        self.chunks.append(chunk)
        return True

    def json(self):
        return json.loads(self.body)


def ok_reply(content="[llama] hi", prompt=3, completion=2):
    body = {"choices": [{"index": 0, "message": {"role": "assistant", "content": content}, "finish_reason": "stop"}], "usage": {"prompt_tokens": prompt, "completion_tokens": completion, "total_tokens": prompt + completion}}
    return main.Reply(200, {"content-type": "application/json"}, io.BytesIO(json.dumps(body).encode()))


def error_reply(status, headers=None):
    return main.Reply(status, dict({"content-type": "application/json"}, **(headers or {})), io.BytesIO(main.error_body(str(status), "upstream", f"injected {status}")))


class Script:
    def __init__(self, **replies):
        self.replies = {name: list(items) for name, items in replies.items()}
        self.calls = []

    def __call__(self, url, payload, headers, timeout):
        name = "fake" if ":8082" in url else "llama"
        self.calls.append((name, json.loads(payload)))
        queue = self.replies.get(name) or []
        return queue.pop(0) if queue else ok_reply(f"[{name}] default")


def scripted_gateway(script, clock=None, **changes):
    slept = []
    gateway = main.Gateway(config(**changes), transport=script, clock=clock or Clock(), sleep=slept.append, wall=lambda: 1_800_000_000.0)
    return gateway, slept


def post(gateway, body, key="sk-cc-team-a", path="/v1/chat/completions", headers=None):
    writer = Writer()
    record = gateway.completion(path, dict({"authorization": f"Bearer {key}"}, **(headers or {})), json.dumps(body).encode(), writer)
    return writer, record


class BucketTests(unittest.TestCase):
    def test_bucket_refills_at_the_per_minute_rate_and_never_above_capacity(self):
        bucket = main.Bucket.per_minute(600, 0.0)
        bucket.adjust(-600, 0.0)
        self.assertEqual(bucket.remaining(1.0), 10)
        self.assertEqual(bucket.remaining(30.0), 300)
        self.assertEqual(bucket.remaining(3600.0), 600)
        self.assertEqual(bucket.reset_in(3600.0), 0.0)

    def test_wait_is_the_missing_tokens_over_the_refill_rate(self):
        bucket = main.Bucket.per_minute(60, 0.0)
        bucket.adjust(-50, 0.0)
        self.assertEqual(bucket.wait_for(10, 0.0), 0.0)
        self.assertAlmostEqual(bucket.wait_for(20, 0.0), 10.0)
        bucket.adjust(-30, 0.0)
        self.assertEqual(bucket.level, -20)
        self.assertAlmostEqual(bucket.wait_for(1, 0.0), 21.0)


class LimiterTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock(0.0)
        self.limiter = main.Limiter(self.clock)
        self.policy = main.KeyPolicy("team", "d", rpm=2, tpm=600)

    def test_the_estimate_is_charged_up_front_and_settled_from_usage(self):
        self.assertTrue(self.limiter.admit(self.policy, 400).allowed)
        tokens = self.limiter.buckets["team"]["tokens"]
        self.assertEqual(tokens.level, 200)
        self.limiter.settle(self.policy, 400, 150)
        self.assertEqual(tokens.level, 450)
        self.limiter.settle(self.policy, 0, 500)
        self.assertEqual(tokens.level, -50)
        refused = self.limiter.admit(self.policy, 10)
        self.assertEqual((refused.allowed, refused.limited_by), (False, "tokens"))
        self.assertAlmostEqual(refused.retry_after, 6.0)

    def test_a_refusal_names_the_bucket_and_consumes_nothing(self):
        self.assertTrue(self.limiter.admit(self.policy, 500).allowed)
        refused = self.limiter.admit(self.policy, 200)
        self.assertFalse(refused.allowed)
        self.assertEqual(refused.limited_by, "tokens")
        self.assertAlmostEqual(refused.retry_after, 10.0)
        self.assertIn("on tokens per min (TPM): Limit 600, Remaining 100, Requested 200", refused.message)
        self.assertEqual(self.limiter.buckets["team"]["requests"].level, 1)
        self.assertTrue(self.limiter.admit(self.policy, 50).allowed)
        by_requests = self.limiter.admit(self.policy, 1)
        self.assertEqual(by_requests.limited_by, "requests")
        self.assertAlmostEqual(by_requests.retry_after, 30.0)

    def test_a_request_bigger_than_the_whole_bucket_is_too_large_not_retryable(self):
        refused = self.limiter.admit(self.policy, 601)
        self.assertEqual((refused.allowed, refused.limited_by, refused.retry_after), (False, "too_large", 0.0))
        self.assertEqual(self.limiter.buckets["team"]["requests"].level, 2)

    def test_headers_use_the_openai_names(self):
        self.limiter.admit(self.policy, 300)
        headers = dict(self.limiter.headers(self.policy))
        self.assertEqual(headers["x-ratelimit-limit-tokens"], "600")
        self.assertEqual(headers["x-ratelimit-remaining-tokens"], "300")
        self.assertEqual(headers["x-ratelimit-reset-tokens"], "30s")
        self.assertEqual(headers["x-ratelimit-remaining-requests"], "1")
        self.assertEqual(headers["x-ratelimit-reset-requests"], "30s")


class EstimateTests(unittest.TestCase):
    def test_prompt_estimate_is_characters_over_four_plus_message_overhead(self):
        body = {"messages": [{"role": "system", "content": "x" * 40}, {"role": "user", "content": [{"type": "text", "text": "y" * 9}]}]}
        self.assertEqual(main.estimate_prompt_tokens(body), 3 + (4 + 10) + (4 + 3))
        self.assertEqual(main.estimate_prompt_tokens({"prompt": "z" * 17}), 5)

    def test_output_budget_is_max_completion_tokens_then_max_tokens_then_default_times_n(self):
        self.assertEqual(main.requested_output_tokens({"max_completion_tokens": 50, "max_tokens": 99}, 256), 50)
        self.assertEqual(main.requested_output_tokens({"max_tokens": 99}, 256), 99)
        self.assertEqual(main.requested_output_tokens({}, 256), 256)
        body = {"messages": [], "max_tokens": 10, "n": 3}
        self.assertEqual(main.estimate_tokens(body, 256), 3 + 30)


class ProviderHeaderTests(unittest.TestCase):
    def test_retry_after_in_seconds_milliseconds_and_http_date(self):
        now = 1_800_000_000.0
        self.assertEqual(main.provider_wait({"retry-after": "7"}, now), 7.0)
        self.assertEqual(main.provider_wait({"retry-after-ms": "250", "retry-after": "7"}, now), 0.25)
        self.assertAlmostEqual(main.provider_wait({"retry-after": formatdate(now + 20, usegmt=True)}, now), 20.0, delta=1.0)
        self.assertIsNone(main.provider_wait({"retry-after": "soon"}, now))
        self.assertIsNone(main.provider_wait({}, now))

    def test_reset_headers_count_only_for_an_exhausted_bucket(self):
        headers = {"x-ratelimit-remaining-requests": "12", "x-ratelimit-reset-requests": "1s", "x-ratelimit-remaining-tokens": "0", "x-ratelimit-reset-tokens": "6m0s"}
        self.assertEqual(main.provider_wait(headers, 0.0), 360.0)
        headers["x-ratelimit-remaining-tokens"] = "40"
        self.assertIsNone(main.provider_wait(headers, 0.0))

    def test_durations_parse_and_format_like_the_provider_headers(self):
        self.assertEqual(main.parse_duration("1m30.5s"), 90.5)
        self.assertEqual(main.parse_duration("20ms"), 0.02)
        self.assertEqual(main.parse_duration("2h"), 7200.0)
        self.assertIsNone(main.parse_duration("6 minutes"))
        self.assertEqual([main.format_duration(s) for s in (0, 0.0201, 7.2, 72, 3725)], ["0s", "21ms", "8s", "1m12s", "1h2m5s"])


class CacheTests(unittest.TestCase):
    def test_the_key_ignores_transport_fields_but_not_sampling_or_scope(self):
        base = chat(temperature=0)
        key = main.cache_key("chat", base, "team-a")
        self.assertEqual(key, main.cache_key("chat", dict(base, stream=False, user="u-17", stream_options={"include_usage": True}), "team-a"))
        self.assertNotEqual(key, main.cache_key("chat", dict(base, temperature=0.7), "team-a"))
        self.assertNotEqual(key, main.cache_key("chat", base, "team-b"))
        self.assertNotEqual(key, main.cache_key("local", base, "team-a"))

    def test_only_deterministic_complete_requests_are_cacheable_by_default(self):
        self.assertEqual(main.cache_decision(chat(temperature=0), "deterministic"), "use")
        self.assertEqual(main.cache_decision(chat(), "deterministic"), "skip:temperature")
        self.assertEqual(main.cache_decision(chat(), "all"), "use")
        self.assertEqual(main.cache_decision(chat(temperature=0, stream=True), "all"), "skip:stream")
        self.assertEqual(main.cache_decision(chat(temperature=0, n=2), "all"), "skip:n")
        self.assertEqual(main.cache_decision(chat(temperature=0), "all", "no-cache"), "skip:no-cache")
        self.assertEqual(main.cache_decision(chat(temperature=0), "off"), "off")

    def test_entries_expire_and_the_least_recently_used_is_evicted(self):
        clock = Clock(0.0)
        cache = main.ResponseCache(10, 2, clock)
        cache.put("a", {"v": 1})
        cache.put("b", {"v": 2})
        self.assertEqual(cache.get("a"), {"v": 1})
        cache.put("c", {"v": 3})
        self.assertIsNone(cache.get("b"))
        clock.advance(11)
        self.assertIsNone(cache.get("a"))
        self.assertEqual((cache.hits, cache.misses), (1, 2))


class RedactionTests(unittest.TestCase):
    def test_redact_masks_emails_keys_cards_and_addresses(self):
        text, counts = main.redact("mail ops@example.com, key sk-live-4f9a8b7c6d5e, AKIAABCDEFGHIJKLMNOP, card 4111 1111 1111 1111 from 10.0.0.12")
        self.assertEqual(text, "mail [EMAIL], key [API_KEY], [AWS_KEY], card [CARD] from [IP]")
        self.assertEqual(counts, {"EMAIL": 1, "API_KEY": 1, "AWS_KEY": 1, "CARD": 1, "IP": 1})

    def test_prompt_logging_modes(self):
        body = chat(text="reach me at ops@example.com")
        hashed = main.prompt_record(body, "hash")
        self.assertEqual(set(hashed), {"sha256", "chars"})
        self.assertEqual(main.prompt_record(body, "redacted")["text"], "reach me at [EMAIL]")
        self.assertEqual(main.prompt_record(body, "full")["text"], "reach me at ops@example.com")
        self.assertEqual(hashed["sha256"], main.prompt_record(body, "full")["sha256"])


class ConfigTests(unittest.TestCase):
    def test_the_fixture_loads_with_hashed_keys_routes_and_dated_prices(self):
        loaded = config()
        self.assertEqual([d.label for d in loaded.routes["chat"]], ["fake/fake-small", "llama/smollm2"])
        self.assertNotIn("sk-cc-team-a", json.dumps(main.check_report(loaded)))
        self.assertIn(main.fingerprint("sk-cc-team-a"), loaded.keys)
        self.assertEqual(loaded.prices.date, "2026-11-01")
        self.assertEqual(loaded.prices.lookup(loaded.routes["local"][0]).cost(1_000_000, 1_000_000), 0.15)
        self.assertEqual(loaded.warnings, [])

    def test_errors_name_the_field(self):
        cases = [
            (dict(models={"x": [{"backend": "nope", "model": "m"}]}), "models.x[0]: backend 'nope' is not defined"),
            (dict(prices={"per_million_tokens": {}}), "needs a 'date'"),
            (dict(keys=[{"name": "a", "key": "k"}, {"name": "a", "key": "j"}]), "duplicate key"),
            (dict(cache={"mode": "sometimes"}), "cache.mode: 'sometimes' is not one of"),
            (dict(keys=[{"name": "a", "key": "k", "tpm": -5}]), "keys[0].tpm: must be positive"),
        ]
        for changes, message in cases:
            with self.assertRaises(main.InputError) as caught:
                config(**changes)
            self.assertIn(message, str(caught.exception))
        with self.assertRaises(main.InputError) as caught:
            config({"ollama": "http://x"})
        self.assertIn("no backend named 'ollama'", str(caught.exception))


class StreamTests(unittest.TestCase):
    def events(self, *chunks):
        lines = [b": ping\n", b"\n"]
        for chunk in chunks:
            lines += [b"data: " + json.dumps(chunk).encode() + b"\n", b"\n"]
        return lines + [b"data: [DONE]\n", b"\n"]

    def test_relay_forwards_events_counts_usage_and_drops_only_the_injected_usage_chunk(self):
        lines = iter(self.events({"choices": [{"delta": {"content": "Hel"}}]}, {"choices": [{"delta": {"content": "lo"}, "finish_reason": "stop"}]}, {"choices": [], "usage": {"prompt_tokens": 9, "completion_tokens": 2}}))
        written = []
        tally = main.relay_stream(lambda: next(lines, b""), lambda b: written.append(b) or True, main.StreamTally(), Clock(5.0), True)
        self.assertEqual((tally.usage, tally.content_chars, tally.done, tally.finish_reason, tally.first_token_at), ((9, 2), 5, True, "stop", 5.0))
        self.assertEqual(written[0], b": ping\n\n")
        self.assertEqual(len(written), 4)
        self.assertFalse(any(b"usage" in w for w in written))
        lines = iter(self.events({"choices": [], "usage": {"prompt_tokens": 9, "completion_tokens": 2}}))
        written = []
        main.relay_stream(lambda: next(lines, b""), lambda b: written.append(b) or True, main.StreamTally(), Clock(), False)
        self.assertTrue(any(b"usage" in w for w in written))

    def test_relay_stops_reading_when_the_client_goes_away(self):
        lines = iter(self.events(*({"choices": [{"delta": {"content": "x"}}]} for _ in range(10))))
        tally = main.relay_stream(lambda: next(lines, b""), lambda b: b"data" not in b, main.StreamTally(), Clock(), True)
        self.assertTrue(tally.client_gone)
        self.assertEqual(tally.content_chars, 1)


class FallbackTests(unittest.TestCase):
    def test_a_short_retry_after_is_honoured_then_the_deployment_cools_down_and_the_next_one_answers(self):
        script = Script(fake=[error_reply(429, {"retry-after": "1"}), error_reply(429, {"retry-after": "1"})])
        clock = Clock()
        gateway, slept = scripted_gateway(script, clock)
        writer, record = post(gateway, chat(max_tokens=8))
        self.assertEqual(writer.status, 200)
        self.assertEqual(writer.headers["x-tokengate-attempts"], "fake/fake-small:429,fake/fake-small:429,llama/smollm2:200")
        self.assertEqual(slept, [1.0])
        self.assertEqual([name for name, _ in script.calls], ["fake", "fake", "llama"])
        self.assertEqual(script.calls[2][1]["model"], "smollm2")
        self.assertEqual(record["deployment"], "llama/smollm2")
        self.assertIn("fake/fake-small", gateway.cooldowns)

    def test_a_long_retry_after_falls_back_at_once_and_later_requests_skip_the_cooling_deployment(self):
        script = Script(fake=[error_reply(429, {"retry-after": "30"})])
        clock = Clock()
        gateway, slept = scripted_gateway(script, clock)
        post(gateway, chat(max_tokens=8))
        self.assertEqual(slept, [])
        writer, _ = post(gateway, chat(max_tokens=8))
        self.assertEqual(writer.headers["x-tokengate-attempts"], "llama/smollm2:200")
        writer, _ = post(gateway, chat(model="fake", max_tokens=8))
        self.assertEqual((writer.status, writer.headers["retry-after"], writer.json()["error"]["code"]), (429, "30", "upstream_cooldown"))
        clock.advance(31)
        writer, _ = post(gateway, chat(model="fake", max_tokens=8))
        self.assertEqual(writer.status, 200)
        self.assertEqual([name for name, _ in script.calls], ["fake", "llama", "llama", "fake"])

    def test_5xx_is_retried_with_backoff_and_a_4xx_is_returned_without_fallback(self):
        script = Script(fake=[error_reply(503), ok_reply("[fake] ok")])
        gateway, slept = scripted_gateway(script)
        writer, _ = post(gateway, chat(max_tokens=8))
        self.assertEqual((writer.status, slept), (200, [0.2]))
        script = Script(fake=[error_reply(400)])
        gateway, slept = scripted_gateway(script)
        writer, record = post(gateway, chat(max_tokens=8))
        self.assertEqual((writer.status, record["error"], len(script.calls)), (400, "upstream_400", 1))
        tokens = gateway.limiter.buckets["team-a"]["tokens"]
        self.assertEqual(tokens.level, tokens.capacity)

    def test_when_every_deployment_fails_the_client_gets_429_or_502_and_its_tokens_back(self):
        gateway, _ = scripted_gateway(Script(fake=[error_reply(429, {"retry-after": "9"})], llama=[error_reply(429, {"retry-after": "4"})]))
        writer, _ = post(gateway, chat(max_tokens=8))
        self.assertEqual((writer.status, writer.headers["retry-after"], writer.headers["x-tokengate-limited-by"]), (429, "4", "upstream"))
        dead = main.Reply(main.UNREACHABLE, {}, None, "upstream connect error: refused")
        gateway, _ = scripted_gateway(Script(fake=[error_reply(502), error_reply(502)], llama=[dead, dead]))
        writer, record = post(gateway, chat(max_tokens=8))
        self.assertEqual((writer.status, writer.json()["error"]["code"]), (502, "upstream_error"))
        self.assertIn("llama/smollm2 unreachable", writer.json()["error"]["message"])
        self.assertEqual(gateway.limiter.buckets["team-a"]["tokens"].level, 20000)


class ProxyTests(unittest.TestCase):
    def setUp(self):
        self.fake = main.FakeServer(("127.0.0.1", 0), "fake", ["fake-small"])
        self.llama = main.FakeServer(("127.0.0.1", 0), "llama", ["smollm2"], report_usage=False)
        self.log_dir = tempfile.mkdtemp(prefix="tokengate-")
        self.addCleanup(shutil.rmtree, self.log_dir)
        self.log = os.path.join(self.log_dir, "gateway.jsonl")
        overrides = {"fake": f"http://127.0.0.1:{self.fake.server_address[1]}", "llama": f"http://127.0.0.1:{self.llama.server_address[1]}"}
        self.lines = []
        self.gateway = main.Gateway(config(overrides), sleep=lambda s: None, log_path=self.log, access=self.lines.append)
        self.server = main.GatewayServer(("127.0.0.1", 0), self.gateway)
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"
        for server in (self.fake, self.llama, self.server):
            thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
            thread.start()
            self.addCleanup(server.server_close)
            self.addCleanup(server.shutdown)

    def call(self, body=None, key="sk-cc-team-a", path="/v1/chat/completions", method="POST", headers=None):
        data = json.dumps(body).encode() if body is not None else None
        request = urllib.request.Request(self.base + path, data=data, method=method, headers=dict({"Authorization": f"Bearer {key}", "Content-Type": "application/json"}, **(headers or {})))
        try:
            with main.OPENER.open(request, timeout=10) as response:
                return response.status, {k.lower(): v for k, v in response.getheaders()}, response.read()
        except urllib.error.HTTPError as err:
            return err.code, {k.lower(): v for k, v in err.headers.items()}, err.read()

    def stats(self, server):
        return json.loads(json.dumps(server.stats))

    def test_routes_by_model_name_and_rewrites_the_model_for_the_backend(self):
        status, headers, body = self.call(chat(model="local", max_tokens=4))
        self.assertEqual((status, headers["x-tokengate-deployment"]), (200, "llama/smollm2"))
        self.assertEqual(json.loads(body)["choices"][0]["message"]["content"], "[llama] w1 w2 w3")
        status, headers, body = self.call(chat(model="fake", max_tokens=4))
        self.assertEqual(json.loads(body)["choices"][0]["message"]["content"], "[fake] w1 w2 w3")
        self.assertEqual(self.stats(self.fake)["models"], {"fake-small": 1})
        self.assertEqual(self.stats(self.llama)["models"], {"smollm2": 1})
        self.assertAlmostEqual(float(headers["x-tokengate-cost-usd"]), (3 * 0.15 + 4 * 0.60) / 1e6)
        status, _, body = self.call(None, path="/v1/models", method="GET")
        self.assertEqual([m["id"] for m in json.loads(body)["data"]], ["chat", "fake", "local"])

    def test_the_client_key_stays_at_the_gateway_and_the_backend_gets_its_own(self):
        with mock.patch.dict(os.environ, {"FAKE_API_KEY": "sk-upstream-secret"}):
            self.call(chat(model="fake", max_tokens=2))
        self.call(chat(model="local", max_tokens=2))
        self.assertEqual(self.stats(self.fake)["last_authorization"], "Bearer sk-upstream-secret")
        self.assertEqual(self.stats(self.llama)["last_authorization"], "")

    def test_authentication_unknown_models_and_per_key_model_lists(self):
        self.assertEqual(self.call(chat(), key="sk-wrong")[0], 401)
        status, _, body = self.call(chat(model="gpt-unknown"))
        self.assertEqual((status, json.loads(body)["error"]["code"]), (404, "model_not_found"))
        status, _, body = self.call(chat(model="local"), key="sk-cc-tight")
        self.assertEqual((status, json.loads(body)["error"]["code"]), (403, "model_not_allowed"))
        self.assertEqual(self.call({"messages": []})[0], 400)
        self.assertEqual(self.stats(self.fake)["requests"] + self.stats(self.llama)["requests"], 0)

    def test_a_spent_token_budget_is_a_429_that_never_reaches_the_backend(self):
        long_prompt = " ".join(["word"] * 100)
        statuses = []
        for _ in range(4):
            status, headers, body = self.call(chat(model="fake", text=long_prompt, max_tokens=16), key="sk-cc-tight")
            statuses.append(status)
        self.assertEqual(statuses, [200, 200, 429, 429])
        self.assertEqual(headers["x-tokengate-limited-by"], "tokens")
        self.assertEqual(headers["retry-after"], "17")
        self.assertEqual(headers["x-ratelimit-remaining-tokens"], "66")
        self.assertEqual(json.loads(body)["error"]["type"], "tokens")
        self.assertEqual(self.stats(self.fake)["requests"], statuses.count(200))
        status, headers, body = self.call(chat(model="fake", max_tokens=400), key="sk-cc-tight")
        self.assertEqual((status, headers["x-tokengate-limited-by"]), (400, "too_large"))

    def test_an_injected_429_falls_back_to_the_second_backend(self):
        self.fake.failures.append({"status": 429, "times": 1, "retry_after": "30"})
        status, headers, body = self.call(chat(max_tokens=4))
        self.assertEqual((status, headers["x-tokengate-attempts"]), (200, "fake/fake-small:429,llama/smollm2:200"))
        self.assertTrue(json.loads(body)["choices"][0]["message"]["content"].startswith("[llama]"))
        status, headers, _ = self.call(chat(max_tokens=4))
        self.assertEqual(headers["x-tokengate-attempts"], "llama/smollm2:200")
        self.assertEqual(self.stats(self.fake)["requests"], 1)

    def read_stream(self, body, key="sk-cc-team-a"):
        request = urllib.request.Request(self.base + "/v1/chat/completions", data=json.dumps(body).encode(), headers={"Authorization": f"Bearer {key}"})
        with main.OPENER.open(request, timeout=10) as response:
            return response.headers.get("Content-Type"), response.read().decode()

    def usage(self):
        return json.loads(self.call(None, path="/tokengate/usage", method="GET")[2])

    def test_streams_pass_through_and_are_counted_from_usage_the_gateway_asked_for(self):
        kind, text = self.read_stream(chat(model="fake", stream=True, max_tokens=5))
        self.assertEqual(kind, "text/event-stream")
        self.assertEqual("".join(json.loads(line[6:])["choices"][0]["delta"].get("content", "") for line in text.splitlines() if line.startswith("data: {")), "[fake] w1 w2 w3 w4")
        self.assertTrue(text.rstrip().endswith("data: [DONE]"))
        self.assertNotIn('"usage"', text)
        self.assertEqual(self.stats(self.fake)["last_request"]["stream_options"], {"include_usage": True})
        self.assertEqual(self.usage()["usage"]["by_route"]["fake"], {"requests": 1, "prompt_tokens": 3, "completion_tokens": 5})
        _, text = self.read_stream(chat(model="fake", stream=True, max_tokens=5, stream_options={"include_usage": True}))
        self.assertIn('"usage"', text)

    def test_a_stream_without_usage_is_estimated_from_its_content(self):
        _, text = self.read_stream(chat(model="local", stream=True, max_tokens=6))
        self.assertIn("[llama] ", text)
        with open(self.log, encoding="utf-8") as handle:
            record = json.loads(handle.readlines()[-1])
        self.assertEqual((record["usage_source"], record["completion_tokens"]), ("estimated", main.estimate_text_tokens(len("[llama] w1 w2 w3 w4 w5"))))
        self.assertTrue(record["stream_done"])
        self.assertIn("ttft_ms", record)

    def test_an_exact_repeat_is_served_from_the_cache_without_tokens(self):
        body = chat(model="fake", max_tokens=4, temperature=0)
        first = self.call(body)
        second = self.call(body)
        self.assertEqual((first[1]["x-tokengate-cache"], second[1]["x-tokengate-cache"]), ("miss", "hit"))
        self.assertEqual(first[2], second[2])
        self.assertEqual(self.stats(self.fake)["requests"], 1)
        self.assertEqual(self.call(body, headers={"Cache-Control": "no-cache"})[1]["x-tokengate-cache"], "skip:no-cache")
        self.assertEqual(self.call(body, key="sk-cc-tight")[1]["x-tokengate-cache"], "miss")
        usage = self.usage()["usage"]
        self.assertEqual((usage["cache_hits"], usage["completion_tokens"]), (1, 8))

    def test_logs_are_redacted_priced_and_summarised(self):
        self.call(chat(model="fake", text="customer ops@example.com wrote from 10.1.2.3", max_tokens=4))
        self.call(chat(model="fake", max_tokens=4), key="sk-wrong")
        with open(self.log, encoding="utf-8") as handle:
            records = [json.loads(line) for line in handle]
        self.assertEqual(records[0]["prompt"]["text"], "customer [EMAIL] wrote from [IP]")
        with open(self.log, encoding="utf-8") as handle:
            self.assertNotIn("sk-cc-team-a", handle.read())
        self.assertEqual((records[0]["key"], records[0]["cost_usd"], records[1]["status"]), ("team-a", round((6 * 0.15 + 4 * 0.6) / 1e6, 10), 401))
        self.assertTrue(self.lines[0].startswith("tokengate 200 key=team-a model=fake -> fake/fake-small in=6 out=4 cost=$0.00000330"))
        rows = main.summarise_logs(records)
        self.assertEqual([(r["key"], r["requests"], r["errors"]) for r in rows], [("-", 1, 1), ("team-a", 1, 0)])


class CliTests(unittest.TestCase):
    def run_main(self, argv):
        out, err = StringIO(), StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = main.main(argv)
        return code, out.getvalue(), err.getvalue()

    def test_check_and_plan_print_text_and_json(self):
        code, out, _ = self.run_main(["check", "--config", CONFIG])
        self.assertEqual(code, 0)
        self.assertIn("chat       fake/fake-small then llama/smollm2", out)
        code, out, _ = self.run_main(["plan", "--config", CONFIG, "--key", "team-a", REQUEST, "--json"])
        report = json.loads(out)
        self.assertEqual(report["deployments"], ["fake/fake-small", "llama/smollm2"])
        self.assertEqual(report["estimate"], {"prompt": 55, "output": 64, "total": 119})
        self.assertEqual(report["cache"], "use")
        self.assertIn("[EMAIL]", report["logged_prompt"]["text"])
        self.assertNotIn("maria@example.com", out)
        code, out, _ = self.run_main(["plan", "--config", CONFIG, "--key", "sk-cc-tight", REQUEST])
        self.assertIn("key tight, model chat", out)

    def test_bad_inputs_exit_two_with_the_reason(self):
        code, _, err = self.run_main(["check", "--config", os.path.join(tempfile.gettempdir(), "tokengate-missing.json")])
        self.assertEqual((code, "No such file" in err), (2, True))
        code, _, err = self.run_main(["check", "--config", CONFIG, "--backend", "llama"])
        self.assertEqual((code, "expected NAME=URL" in err), (2, True))
        code, _, err = self.run_main(["plan", "--config", CONFIG, "--key", "nobody", REQUEST])
        self.assertEqual((code, "no key with that name" in err), (2, True))


if __name__ == "__main__":
    unittest.main()
