import io
import json
import os
import random
import socket
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from email.utils import formatdate

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import main  # noqa: E402

SCENARIOS = os.path.join(os.path.dirname(__file__), "..", "fixtures", "scenarios.json")


class FakeTime:
    def __init__(self, start=1_700_000_000.0):
        self.now = start
        self.sleeps = []

    def clock(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(round(seconds, 6))
        self.now += seconds


class Fixed:
    def __init__(self, value):
        self.value = value

    def random(self):
        return self.value


def reply(status, headers=None, body=b"{}"):
    return main.Response(status, {k.lower(): v for k, v in (headers or {}).items()}, body, "http://api.test/x")


class Scripted:
    def __init__(self, *steps):
        self.steps = list(steps)
        self.calls = []

    def __call__(self, method, url, headers, body, timeouts):
        self.calls.append({"method": method, "url": url, "headers": dict(headers), "body": body})
        step = self.steps.pop(0) if self.steps else reply(200)
        if isinstance(step, Exception):
            raise step
        return step


def client_with(transport, time_source=None, **options):
    time_source = time_source or FakeTime()
    logs = []
    policy = options.pop("policy", main.RetryPolicy(retries=3, backoff_base=0.5, backoff_cap=30, max_wait=60, deadline=120))
    client = main.Client(policy, transport=transport, clock=time_source.clock, wall=time_source.clock, sleep=time_source.sleep, rng=options.pop("rng", Fixed(0.5)), log=logs.append, **options)
    return client, time_source, logs


def run_cli(*argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = main.main(list(argv))
    return code, out.getvalue(), err.getvalue()


class RetryDecisionTests(unittest.TestCase):
    def setUp(self):
        self.policy = main.RetryPolicy(retries=2, backoff_base=1, backoff_cap=4, max_wait=10)

    def decide(self, method="GET", has_key=False, attempt=1, status=None, headers=None, error=None, rng=Fixed(0.5)):
        outcome = main.Outcome(status, {k.lower(): v for k, v in (headers or {}).items()}, error)
        return main.decide(self.policy, method, has_key, attempt, outcome, 1_700_000_000.0, rng)

    def test_success_and_non_retryable_statuses_stop(self):
        self.assertEqual(self.decide(status=200).reason, "done")
        self.assertFalse(self.decide(status=404).retry)
        self.assertFalse(self.decide(status=501).retry)

    def test_post_without_key_is_not_retried_but_with_key_it_is(self):
        no_key = self.decide("POST", status=503)
        self.assertFalse(no_key.retry)
        self.assertIn("Idempotency-Key", no_key.reason)
        self.assertTrue(self.decide("POST", has_key=True, status=503).retry)
        self.assertFalse(self.decide("PATCH", error="timeout").retry)

    def test_connect_errors_are_safe_for_any_method(self):
        self.assertTrue(self.decide("POST", error="connect").retry)
        self.assertFalse(self.decide("GET", error="tls").retry)

    def test_retries_are_bounded(self):
        self.assertTrue(self.decide(attempt=2, status=503).retry)
        stop = self.decide(attempt=3, status=503)
        self.assertFalse(stop.retry)
        self.assertIn("exhausted", stop.reason)

    def test_full_jitter_stays_under_the_capped_exponential_ceiling(self):
        self.assertEqual([self.policy.ceiling(n) for n in range(5)], [1, 2, 4, 4, 4])
        self.assertEqual(self.decide(attempt=1, status=503, rng=Fixed(0.0)).delay, 0.0)
        self.assertAlmostEqual(self.decide(attempt=2, status=503, rng=Fixed(0.999)).delay, 1.998)
        rng = random.Random(3)
        delays = [main.full_jitter(self.policy, 10, rng) for _ in range(500)]
        self.assertTrue(all(0 <= d < 4 for d in delays))

    def test_retry_after_wins_over_backoff_and_is_capped_by_max_wait(self):
        honoured = self.decide(status=429, headers={"Retry-After": "7"})
        self.assertEqual((honoured.retry, honoured.delay, honoured.reason), (True, 7.0, "retry-after"))
        refused = self.decide(status=429, headers={"Retry-After": "120"})
        self.assertFalse(refused.retry)
        self.assertIn("max_wait", refused.reason)


class ServerWaitTests(unittest.TestCase):
    NOW = 1_700_000_000.0

    def test_retry_after_seconds_and_http_date(self):
        self.assertEqual(main.server_wait({"retry-after": "3"}, self.NOW), (3.0, "retry-after"))
        date = formatdate(self.NOW + 30, usegmt=True)
        wait, source = main.server_wait({"retry-after": date}, self.NOW)
        self.assertAlmostEqual(wait, 30.0, places=3)
        self.assertEqual(main.server_wait({"retry-after": formatdate(self.NOW - 30, usegmt=True)}, self.NOW)[0], 0.0)

    def test_ratelimit_reset_epoch_and_delta_only_when_remaining_is_zero(self):
        epoch = {"x-ratelimit-remaining": "0", "x-ratelimit-reset": str(int(self.NOW) + 42)}
        self.assertEqual(main.server_wait(epoch, self.NOW), (42.0, "x-ratelimit-reset"))
        self.assertEqual(main.server_wait({"x-ratelimit-remaining": "0", "x-ratelimit-reset": "15"}, self.NOW)[0], 15.0)
        self.assertIsNone(main.server_wait({"x-ratelimit-remaining": "12", "x-ratelimit-reset": "15"}, self.NOW))

    def test_garbage_is_ignored(self):
        self.assertIsNone(main.server_wait({"retry-after": "soon"}, self.NOW))
        self.assertIsNone(main.server_wait({}, self.NOW))


class BucketAndBreakerTests(unittest.TestCase):
    def test_token_bucket_allows_a_burst_then_spaces_requests(self):
        clock = FakeTime(0.0)
        bucket = main.TokenBucket(rate=5, burst=2, clock=clock.clock)
        waits = []
        for _ in range(6):
            wait = bucket.reserve()
            waits.append(round(wait, 6))
            clock.sleep(wait)
        self.assertEqual(waits, [0.0, 0.0, 0.2, 0.2, 0.2, 0.2])
        clock.sleep(10)
        self.assertEqual(bucket.reserve(), 0.0)
        self.assertEqual(bucket.tokens, 1.0)

    def test_breaker_opens_rejects_half_opens_and_closes(self):
        clock = FakeTime(0.0)
        breaker = main.CircuitBreaker(failures=3, reset_after=10, clock=clock.clock)
        for _ in range(3):
            breaker.allow()
            breaker.record(True)
        self.assertEqual(breaker.current(), "open")
        with self.assertRaises(main.CircuitOpenError):
            breaker.allow()
        clock.sleep(10)
        self.assertEqual(breaker.current(), "half-open")
        breaker.allow()
        with self.assertRaises(main.CircuitOpenError):
            breaker.allow()
        breaker.record(True)
        self.assertEqual(breaker.current(), "open")
        clock.sleep(10)
        breaker.allow()
        breaker.record(False)
        self.assertEqual((breaker.current(), breaker.failures), ("closed", 0))

    def test_a_success_resets_the_consecutive_count(self):
        breaker = main.CircuitBreaker(failures=2, reset_after=1, clock=FakeTime(0.0).clock)
        breaker.record(True)
        breaker.record(False)
        breaker.record(True)
        self.assertEqual(breaker.current(), "closed")


class ClientTests(unittest.TestCase):
    def test_retries_until_success_with_jittered_sleeps_and_logs(self):
        transport = Scripted(reply(503), reply(502), reply(200, body=b'{"ok": true}'))
        client, clock, logs = client_with(transport)
        response = client.request("GET", "http://api.test/x")
        self.assertEqual((response.status, response.attempts, response.json()), (200, 3, {"ok": True}))
        self.assertEqual(clock.sleeps, [0.25, 0.5])
        self.assertEqual([(r["attempt"], r["status"], r["decision"]) for r in logs], [(1, 503, "retry"), (2, 502, "retry"), (3, 200, "stop")])

    def test_idempotency_key_is_identical_on_every_attempt(self):
        transport = Scripted(reply(503), reply(200))
        client, _, _ = client_with(transport)
        client.request("POST", "http://api.test/orders", body=b"{}", idempotency_key="order-42")
        self.assertEqual([c["headers"]["Idempotency-Key"] for c in transport.calls], ["order-42", "order-42"])
        self.assertEqual(transport.calls[0]["headers"]["Content-Type"], "application/json")

    def test_post_read_failure_is_raised_without_retry(self):
        transport = Scripted(main.TransportError("timeout", "no response"))
        client, _, logs = client_with(transport)
        with self.assertRaises(main.TransportError) as caught:
            client.request("POST", "http://api.test/orders", body=b"{}")
        self.assertEqual((caught.exception.attempts, len(transport.calls)), (1, 1))
        self.assertIn("not retried", logs[-1]["reason"])

    def test_deadline_stops_a_retry_that_would_overrun(self):
        transport = Scripted(reply(429, {"Retry-After": "50"}), reply(200))
        policy = main.RetryPolicy(retries=3, max_wait=60, deadline=30)
        client, _, logs = client_with(transport, policy=policy)
        response = client.request("GET", "http://api.test/x")
        self.assertEqual((response.status, len(transport.calls)), (429, 1))
        self.assertIn("deadline", logs[-1]["reason"])

    def test_open_breaker_ends_a_retry_loop(self):
        transport = Scripted(reply(503), reply(503), reply(503))
        clock = FakeTime()
        breaker = main.CircuitBreaker(failures=2, reset_after=60, clock=clock.clock)
        client, _, logs = client_with(transport, clock, breaker=breaker)
        with self.assertRaises(main.CircuitOpenError):
            client.request("GET", "http://api.test/x")
        self.assertEqual(len(transport.calls), 2)
        self.assertEqual(logs[-1]["event"], "refused")

    def test_remaining_zero_pauses_the_next_request_until_reset(self):
        clock = FakeTime()
        reset = str(int(clock.now) + 20)
        transport = Scripted(reply(200, {"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": reset}), reply(200))
        client, _, logs = client_with(transport, clock)
        client.request("GET", "http://api.test/a")
        self.assertEqual(clock.sleeps, [])
        client.request("GET", "http://api.test/b")
        self.assertEqual(clock.sleeps, [20.0])
        self.assertEqual(logs[1]["event"], "pause")

    def test_client_token_bucket_throttles_every_attempt(self):
        clock = FakeTime(0.0)
        bucket = main.TokenBucket(rate=2, burst=1, clock=clock.clock)
        client, _, logs = client_with(Scripted(), clock, bucket=bucket)
        for _ in range(3):
            client.request("GET", "http://api.test/x")
        self.assertEqual(clock.sleeps, [0.5, 0.5])
        self.assertEqual(sum(1 for r in logs if r["event"] == "throttle"), 2)


class LinkTests(unittest.TestCase):
    def test_parse_github_style_link_header(self):
        value = ('<https://api.github.com/repositories/1/issues?page=2&per_page=5>; rel="next", '
                 '<https://api.github.com/repositories/1/issues?page=9&per_page=5>; rel="last", '
                 '</issues?page=1>; rel="first prev"')
        links = main.parse_link_header(value, "https://api.github.com/repositories/1/issues?page=1")
        self.assertEqual(links["next"], "https://api.github.com/repositories/1/issues?page=2&per_page=5")
        self.assertEqual(links["last"].split("?")[1], "page=9&per_page=5")
        self.assertEqual(links["first"], "https://api.github.com/issues?page=1")
        self.assertEqual(links["prev"], links["first"])
        self.assertEqual(main.parse_link_header(""), {})

    def test_a_link_loop_is_an_error_not_an_endless_crawl(self):
        page = reply(200, {"Link": '<http://api.test/x>; rel="next"'}, b"[1]")
        client, _, _ = client_with(Scripted(page, page))
        with self.assertRaises(main.PaginationError):
            list(main.paginate_links(client, "http://api.test/x"))

    def test_cursor_round_trip(self):
        self.assertEqual(main.decode_cursor(main.encode_cursor(15)), 15)
        with self.assertRaises(ValueError):
            main.decode_cursor("bm9wZQ")


class FaultServerTests(unittest.TestCase):
    def setUp(self):
        self.server = main.FaultServer().start()
        self.url = self.server.url
        self.clock = FakeTime()
        self.logs = []

    def tearDown(self):
        self.server.close()

    def client(self, **options):
        policy = options.pop("policy", main.RetryPolicy(retries=2, backoff_base=0.1))
        timeouts = options.pop("timeouts", main.Timeouts(1, 5))
        return main.Client(policy, timeouts, sleep=self.clock.sleep, rng=random.Random(1), log=self.logs.append, **options)

    def test_scenario_table_attempts_for_apiclient(self):
        with open(SCENARIOS, encoding="utf-8") as handle:
            spec = json.load(handle)
        for scenario in spec["scenarios"]:
            with self.subTest(scenario["name"]):
                self.server.state.plan(scenario["name"], scenario["plan"])
                client = self.client(timeouts=main.Timeouts(1, scenario.get("read_timeout", spec["read_timeout"])))
                body = b"{}" if scenario["method"] == "POST" else None
                response = client.request(scenario["method"], f"{self.url}/s/{scenario['name']}", body=body, idempotency_key=scenario.get("idempotency_key"))
                hits = self.server.state.hits_of(scenario["name"])
                self.assertEqual(len(hits), scenario["attempts"]["apiclient"])
                self.assertEqual(response.status, scenario["final"]["apiclient"])
                if scenario.get("idempotency_key"):
                    self.assertEqual({h["idempotency_key"] for h in hits}, {scenario["idempotency_key"]})

    def test_link_pagination_follows_next_even_when_the_server_caps_per_page(self):
        items = list(main.paginate_links(self.client(), f"{self.url}/items?per_page=100"))
        self.assertEqual([i["id"] for i in items], list(range(1, 24)))
        self.assertEqual(len(self.server.state.hits_of("items")), 3)

    def test_cursor_pagination_collects_every_item_once(self):
        items = list(main.paginate_cursor(self.client(), f"{self.url}/cursor?limit=7"))
        self.assertEqual([i["id"] for i in items], list(range(1, 24)))
        self.assertEqual(len(self.server.state.hits_of("cursor")), 4)

    def test_etag_revalidation_saves_rate_limit_and_sees_changes(self):
        client = self.client()
        first = client.get(f"{self.url}/conditional")
        second = client.get(f"{self.url}/conditional")
        self.assertEqual((first.from_cache, second.from_cache), (False, True))
        self.assertEqual(first.header("x-ratelimit-remaining"), second.header("x-ratelimit-remaining"))
        self.assertEqual(second.json(), first.json())
        self.assertEqual(self.server.state.hits_of("conditional")[1]["if_none_match"], first.header("etag"))
        client.request("PUT", f"{self.url}/conditional", body=b'{"name": "cloudcaptain", "version": 2}')
        third = client.get(f"{self.url}/conditional")
        self.assertEqual((third.from_cache, third.json()["version"]), (False, 2))

    def test_connection_refused_is_retried_even_for_post(self):
        probe = socket.socket()
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
        probe.close()
        with self.assertRaises(main.TransportError) as caught:
            self.client().request("POST", f"http://127.0.0.1:{port}/orders", body=b"{}")
        self.assertEqual((caught.exception.kind, caught.exception.attempts), ("connect", 3))

    def test_bad_plan_is_rejected(self):
        with self.assertRaises(ValueError):
            self.server.state.plan("x", [{"status": 503, "explode": True}])


class CliTests(unittest.TestCase):
    def test_dry_run_explains_the_retry_plan(self):
        code, out, _ = run_cli("request", "--dry-run", "--json", "--retries", "2", "POST", "http://api.test/orders")
        plan = json.loads(out)
        self.assertEqual((code, plan["retry_safe"], plan["max_attempts"]), (0, False, 1))
        code, out, _ = run_cli("request", "--dry-run", "--json", "--auto-idempotency-key", "POST", "http://api.test/orders")
        plan = json.loads(out)
        self.assertEqual((plan["retry_safe"], plan["max_attempts"], len(plan["idempotency_key"])), (True, 4, 36))

    def test_usage_errors_exit_2_on_stderr(self):
        for argv in (["request", "GET", "ftp://x"], ["request", "GET", "http://x", "-H", "broken"], ["request", "--retries", "-1", "GET", "http://x"], ["nope"]):
            with self.subTest(argv):
                code, out, err = run_cli(*argv)
                self.assertEqual((code, out), (2, ""))
                self.assertTrue(err)

    def test_request_and_paginate_against_the_fault_server(self):
        with main.FaultServer() as server, tempfile.TemporaryDirectory() as tmp:
            log = os.path.join(tmp, "attempts.jsonl")
            server.state.plan("cli", [{"status": 500, "times": 9}])
            code, out, err = run_cli("request", "GET", f"{server.url}/s/cli", "--retries", "1", "--backoff-base", "0.01", "--log", log, "--json")
            self.assertEqual((code, json.loads(out)["status"], json.loads(out)["attempts"]), (1, 500, 2))
            self.assertIn("HTTP 500 after 2 attempt(s)", err)
            with open(log, encoding="utf-8") as handle:
                records = [json.loads(line) for line in handle]
            self.assertEqual([r["status"] for r in records], [500, 500])
            code, out, _ = run_cli("paginate", f"{server.url}/items", "--json", "--quiet")
            self.assertEqual((code, len(json.loads(out))), (0, 23))
            code, out, _ = run_cli("request", "GET", f"{server.url}/s/ok", "--quiet")
            self.assertEqual((code, json.loads(out)["name"]), (0, "ok"))


if __name__ == "__main__":
    unittest.main()
