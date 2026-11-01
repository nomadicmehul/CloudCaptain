import collections
import http.client
import json
import os
import socket
import sys
import threading
import time
import unittest
import urllib.error
import urllib.request
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import main  # noqa: E402


def backends(*names, weights=None, active=None):
    return [main.Backend(n, weight=(weights or {}).get(n, 1), active=(active or {}).get(n, 0)) for n in names]


class FakeClock:
    def __init__(self, now=100.0):
        self.now = now

    def __call__(self):
        return self.now


def closed_port():
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()
    return port


class AlgorithmTests(unittest.TestCase):
    def test_round_robin_cycles_through_every_backend_in_order(self):
        rr = main.RoundRobin()
        pool = backends("a", "b", "c")
        self.assertEqual([rr.pick(pool).name for _ in range(7)], ["a", "b", "c", "a", "b", "c", "a"])

    def test_smooth_weighted_round_robin_matches_nginx_sequence(self):
        wrr = main.SmoothWeightedRoundRobin()
        pool = backends("a", "b", "c", weights={"a": 5})
        self.assertEqual([wrr.pick(pool).name for _ in range(7)], ["a", "a", "b", "a", "c", "a", "a"])
        counts = collections.Counter(wrr.pick(pool).name for _ in range(70))
        self.assertEqual(counts, {"a": 50, "b": 10, "c": 10})

    def test_least_conn_divides_by_weight_and_rotates_ties(self):
        lc = main.LeastConnections()
        pool = backends("a", "b", "c", weights={"b": 2}, active={"a": 2, "b": 3, "c": 2})
        self.assertEqual(lc.pick(pool).name, "b")
        idle = backends("a", "b", "c")
        self.assertEqual([lc.pick(idle).name for _ in range(3)], ["b", "c", "a"])

    def test_p2c_never_picks_the_busiest_backend(self):
        p2c = main.PowerOfTwoChoices(main.random.Random(3))
        pool = backends("a", "b", "c", active={"a": 0, "b": 5, "c": 9})
        counts = collections.Counter(p2c.pick(pool).name for _ in range(600))
        self.assertNotIn("c", counts)
        self.assertAlmostEqual(counts["a"] / 600, 2 / 3, delta=0.07)

    def test_ring_hash_moves_only_the_keys_of_the_removed_backend_and_gives_them_back(self):
        ring = main.RingHash()
        full = backends("a", "b", "c", "d")
        without_d = backends("a", "b", "c")
        keys = [f"user-{i}" for i in range(3000)]
        before = {k: ring.pick(full, k).name for k in keys}
        after = {k: ring.pick(without_d, k).name for k in keys}
        moved = [k for k in keys if before[k] != after[k]]
        self.assertTrue(moved)
        self.assertTrue(all(before[k] == "d" for k in moved))
        self.assertTrue(all(after[k] != "d" for k in keys))
        self.assertEqual({k: ring.pick(full, k).name for k in keys}, before)

    def test_maglev_table_is_balanced_and_removal_disturbs_few_entries(self):
        table = main.maglev_table(["a", "b", "c", "d", "e"], [1] * 5, 1031)
        counts = collections.Counter(table)
        self.assertLessEqual(max(counts.values()) - min(counts.values()), 1)
        weighted = collections.Counter(main.maglev_table(["a", "b", "c"], [2, 1, 1], 1031))
        self.assertAlmostEqual(weighted["a"] / 1031, 0.5, delta=0.01)
        smaller = main.maglev_table(["a", "b", "c", "e"], [1] * 4, 1031)
        disturbed = sum(1 for old, new in zip(table, smaller) if old != new and old != "d")
        self.assertLess(disturbed / 1031, 0.05)
        with self.assertRaises(main.InputError):
            main.Maglev(1000)

    def test_remap_report_compares_modulo_ring_and_maglev(self):
        report = main.remap_report([("a", 1), ("b", 1), ("c", 1), ("d", 1)], ["d"], [], 4000, 1031)
        rows = {r["algorithm"]: r for r in report["results"]}
        self.assertGreater(rows["modulo_hash"]["moved"], 0.6)
        self.assertEqual(rows["ring_hash"]["moved_from_kept"], 0.0)
        self.assertLess(rows["maglev"]["moved_from_kept"], 0.05)
        self.assertLess(rows["maglev"]["moved"], 0.32)
        with self.assertRaises(main.InputError):
            main.remap_report([("a", 1)], ["x"], [], 10)

    def test_hash_algorithms_fall_back_to_round_robin_without_a_key(self):
        for algorithm in (main.RingHash(), main.Maglev(251), main.ModuloHash()):
            pool = backends("a", "b", "c")
            self.assertEqual([algorithm.pick(pool).name for _ in range(3)], ["a", "b", "c"], algorithm.name)
            self.assertEqual(algorithm.pick(pool, "k").name, algorithm.pick(pool, "k").name)

    def test_hash_keys_come_from_headers_cookies_path_or_source(self):
        headers = {"x-user": "alice", "cookie": "a=1; sid=\"s-42\""}
        self.assertEqual(main.hash_key(main.parse_hash_on("header:X-User"), headers, "/p", "10.0.0.1"), "alice")
        self.assertEqual(main.hash_key(main.parse_hash_on("cookie:sid"), headers, "/p", "10.0.0.1"), "s-42")
        self.assertEqual(main.hash_key(main.parse_hash_on("path"), headers, "/cart?x=1", "10.0.0.1"), "/cart")
        self.assertEqual(main.hash_key(main.parse_hash_on("source"), headers, "/", "10.0.0.1"), "10.0.0.1")
        self.assertIsNone(main.hash_key(main.parse_hash_on("header:x-tenant"), headers, "/", "10.0.0.1"))
        with self.assertRaises(main.InputError):
            main.parse_hash_on("header")


class HealthTests(unittest.TestCase):
    def test_fall_and_rise_count_consecutive_checks_only(self):
        policy = main.HealthPolicy(rise=2, fall=3)
        backend = main.Backend("a")
        results = [main.record_check(backend, ok, policy) for ok in (False, False, True, False, False)]
        self.assertEqual(results, [None] * 5)
        self.assertTrue(backend.healthy)
        self.assertEqual(main.record_check(backend, False, policy), "down")
        self.assertEqual([main.record_check(backend, ok, policy) for ok in (True, False, True)], [None, None, None])
        self.assertFalse(backend.healthy)
        self.assertEqual(main.record_check(backend, True, policy), "up")

    def test_active_checker_takes_a_real_backend_out_and_back(self):
        echo = main.EchoBackend(("127.0.0.1", 0), "b1", status=500)
        threading.Thread(target=echo.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
        self.addCleanup(echo.server_close)
        self.addCleanup(echo.shutdown)
        dead = main.Backend("gone", address=f"127.0.0.1:{closed_port()}")
        live = main.Backend("b1", address=f"127.0.0.1:{echo.server_address[1]}")
        pool = main.Pool([live, dead], main.RoundRobin(), health=main.HealthPolicy(timeout=1.0, rise=2, fall=3))
        checker = main.HealthChecker(pool)
        for _ in range(3):
            checker.run_once()
        self.assertEqual((live.healthy, dead.healthy), (False, False))
        self.assertEqual(echo.counts["health_checks"], 3)
        down = [e for e in pool.events if e["event"] == "down"]
        self.assertEqual(down[0]["reason"], "3 consecutive failed checks (last: HTTP 500)")
        echo.status = 200
        checker.run_once()
        self.assertFalse(live.healthy)
        checker.run_once()
        self.assertTrue(live.healthy)
        self.assertEqual(pool.events[-1]["reason"], "2 consecutive passed checks")
        self.assertEqual([b.name for b in pool.candidates()], ["b1"])

    def test_outlier_ejection_time_grows_with_each_ejection_and_expires(self):
        clock = FakeClock()
        policy = main.OutlierPolicy(consecutive_5xx=3, base_ejection=10, max_ejection=25, max_ejection_percent=50)
        pool = main.Pool(backends("a", "b"), main.RoundRobin(), outlier=policy, clock=clock)
        a = pool.backends[0]
        durations = []
        for _ in range(3):
            for status in (500, 500, 500):
                pool.acquire(sticky="a")
                pool.release(a, status)
            self.assertFalse(a.available(clock.now))
            durations.append(a.ejected_until - clock.now)
            clock.now = a.ejected_until
            self.assertTrue(a.available(clock.now))
        self.assertEqual(durations, [10, 20, 25])
        self.assertEqual([e["event"] for e in pool.events], ["ejected"] * 3)
        for status in (500, 500, 200, 500, 500):
            pool.release(a, status)
        self.assertTrue(a.available(clock.now))

    def test_max_ejection_percent_counts_the_ejection_being_made(self):
        def run(**policy):
            clock = FakeClock()
            pool = main.Pool(backends("a", "b", "c"), main.RoundRobin(), outlier=main.OutlierPolicy(consecutive_5xx=2, **policy), clock=clock)
            for backend in (pool.backends[0],) * 2 + (pool.backends[1],) * 2:
                pool.release(backend, 503)
            return [e["event"] for e in pool.events], [b.available(clock.now) for b in pool.backends]
        self.assertEqual(run(), (["ejection-refused", "ejection-refused"], [True, True, True]))
        self.assertEqual(run(always_eject_one_host=True), (["ejected", "ejection-refused"], [False, True, True]))
        self.assertEqual(run(max_ejection_percent=34), (["ejected", "ejection-refused"], [False, True, True]))
        self.assertEqual(run(max_ejection_percent=67), (["ejected", "ejected"], [False, False, True]))


def fake_forwarder(statuses, calls=None):
    def forwarder(address, method, path, headers, body, timeout):
        if calls is not None:
            calls.append((address, method, dict(headers)))
        outcome, status = statuses.get(address, ("response", 200))
        return outcome, status, [("Content-Type", "text/plain"), ("Connection", "keep-alive")], f"{address} {status}".encode()
    return forwarder


def retry_pool(names=("b1", "b2", "b3"), **retry):
    policy = main.RetryPolicy(**dict({"retries": 1, "retry_on": ("connect-failure", "gateway-error")}, **retry))
    return main.Pool([main.Backend(n, address=n) for n in names], main.RoundRobin(), retry=policy)


class RetryTests(unittest.TestCase):
    def test_retry_reasons_respect_idempotency_and_the_retry_on_list(self):
        policy = main.RetryPolicy(retry_on=("connect-failure", "gateway-error"))
        self.assertEqual(main.retry_reason("GET", "response", 502, policy), "gateway-error")
        self.assertEqual(main.retry_reason("PUT", "timeout", 504, policy), "gateway-error")
        self.assertIsNone(main.retry_reason("GET", "response", 500, policy))
        self.assertEqual(main.retry_reason("GET", "response", 500, main.RetryPolicy(retry_on=("5xx",))), "5xx")
        self.assertIsNone(main.retry_reason("POST", "response", 503, policy))
        self.assertIsNone(main.retry_reason("PATCH", "timeout", 504, policy))
        self.assertEqual(main.retry_reason("POST", "connect-failure", 503, policy), "connect-failure")
        self.assertIsNone(main.retry_reason("GET", "connect-failure", 503, main.RetryPolicy(retry_on=("5xx",))))

    def test_retry_budget_is_a_share_of_active_requests_with_a_floor(self):
        budget = main.RetryBudget(percent=20, minimum=3)
        budget.active_requests = 10
        self.assertEqual(budget.limit(), 3)
        budget.active_requests = 100
        self.assertEqual(budget.limit(), 20)
        budget.active_retries = 19
        self.assertTrue(budget.allows())
        budget.active_retries = 20
        self.assertFalse(budget.allows())

    def test_get_is_retried_on_another_backend_and_post_is_not(self):
        calls = []
        forwarder = fake_forwarder({"b1": ("response", 503)}, calls)
        pool = retry_pool()
        got = main.proxy_request(pool, "GET", "/x", {"Host": "shop"}, None, "10.0.0.9", forwarder=forwarder)
        self.assertEqual((got.status, got.backend, got.attempts), (200, "b3", ["b1:503", "b3:200"]))
        pool = retry_pool()
        posted = main.proxy_request(pool, "POST", "/x", {"Host": "shop"}, b"order", "10.0.0.9", forwarder=forwarder)
        self.assertEqual((posted.status, posted.attempts), (503, ["b1:503"]))
        self.assertIn(("x-minilb-note", "not retried: POST is not idempotent"), posted.headers)
        refused = fake_forwarder({"b1": ("connect-failure", 503)})
        pool = retry_pool()
        self.assertEqual(main.proxy_request(pool, "POST", "/x", {}, b"order", "10.0.0.9", forwarder=refused).attempts, ["b1:503", "b3:200"])
        self.assertEqual((pool.budget.active_requests, pool.budget.active_retries), (0, 0))

    def test_retries_stop_at_the_limit_the_budget_or_the_last_backend(self):
        failing = fake_forwarder({n: ("response", 503) for n in ("b1", "b2", "b3")})
        pool = retry_pool(retries=5)
        result = main.proxy_request(pool, "GET", "/", {}, None, "c", forwarder=failing)
        self.assertEqual(sorted(result.attempts), ["b1:503", "b2:503", "b3:503"])
        self.assertIn(("x-minilb-note", "no other backend to retry on (no healthy upstream)"), result.headers)
        pool = retry_pool(retries=1)
        self.assertEqual(len(main.proxy_request(pool, "GET", "/", {}, None, "c", forwarder=failing).attempts), 2)
        pool = retry_pool(retries=3, min_retry_concurrency=1)
        pool.budget.active_retries = 1
        result = main.proxy_request(pool, "GET", "/", {}, None, "c", forwarder=failing)
        self.assertEqual(result.attempts, ["b1:503"])
        self.assertIn(("x-minilb-note", "not retried: retry budget exhausted"), result.headers)
        self.assertEqual(pool.budget.refused, 1)

    def test_upstream_headers_drop_hop_by_hop_and_append_forwarded_for(self):
        calls = []
        pool = retry_pool(names=("b1",))
        headers = {"Host": "shop", "Connection": "close, X-Secret", "X-Secret": "s", "Keep-Alive": "5", "X-Forwarded-For": "203.0.113.7", "X-Trace": "t1"}
        result = main.proxy_request(pool, "GET", "/", headers, None, "10.0.0.9", forwarder=fake_forwarder({}, calls))
        sent = calls[0][2]
        self.assertEqual(sent["X-Forwarded-For"], "203.0.113.7, 10.0.0.9")
        self.assertEqual((sent["Host"], sent["X-Trace"]), ("shop", "t1"))
        self.assertFalse({"Connection", "X-Secret", "Keep-Alive"} & set(sent))
        self.assertNotIn("Connection", dict(result.headers))
        self.assertEqual(dict(result.headers)["x-minilb-backend"], "b1")


class StickyAndDrainTests(unittest.TestCase):
    def test_cookie_value_finds_one_cookie_among_many(self):
        self.assertEqual(main.cookie_value("theme=dark; MINILB=b2; other=1", "MINILB"), "b2")
        self.assertIsNone(main.cookie_value("MINILBX=b2", "MINILB"))
        self.assertIsNone(main.cookie_value(None, "MINILB"))

    def test_sticky_cookie_pins_a_client_and_moves_it_when_its_backend_is_gone(self):
        pool = main.Pool([main.Backend(n, address=n) for n in ("b1", "b2", "b3")], main.RoundRobin(), sticky_cookie="MINILB")
        first = main.proxy_request(pool, "GET", "/", {}, None, "c", forwarder=fake_forwarder({}))
        self.assertIn(("Set-Cookie", "MINILB=b1; Path=/; HttpOnly"), first.headers)
        pinned = [main.proxy_request(pool, "GET", "/", {"Cookie": "MINILB=b3"}, None, "c", forwarder=fake_forwarder({})) for _ in range(4)]
        self.assertEqual({r.backend for r in pinned}, {"b3"})
        self.assertFalse(any(n == "Set-Cookie" for r in pinned for n, _ in r.headers))
        pool.find("b3").healthy = False
        moved = main.proxy_request(pool, "GET", "/", {"Cookie": "MINILB=b3"}, None, "c", forwarder=fake_forwarder({}))
        self.assertNotEqual(moved.backend, "b3")
        self.assertIn(("Set-Cookie", f"MINILB={moved.backend}; Path=/; HttpOnly"), moved.headers)

    def test_draining_backend_gets_no_new_requests_and_leaves_when_idle_or_late(self):
        clock = FakeClock()
        pool = main.Pool(backends("a", "b", "c"), main.RoundRobin(), clock=clock)
        a, _ = pool.acquire(sticky="a")
        pool.drain("a", 30)
        self.assertEqual(pool.find("a").state(clock.now), "draining")
        picked = [pool.acquire()[0] for _ in range(6)]
        self.assertNotIn("a", [p.name for p in picked])
        for backend in picked:
            pool.release(backend, 200)
        pool.release(a, 200)
        self.assertIsNone(pool.find("a"))
        self.assertEqual(pool.events[-1]["reason"], "drained: no requests in flight")
        b, _ = pool.acquire(sticky="b")
        pool.drain("b", 5)
        clock.now += 5
        pool.reap()
        self.assertIsNone(pool.find("b"))
        self.assertEqual(pool.events[-1]["reason"], "drain deadline passed with 1 in flight")
        with self.assertRaises(main.UnknownBackend):
            pool.drain("zz", 1)


class SimulatorTests(unittest.TestCase):
    def specs(self):
        return [main.parse_sim_spec(s) for s in main.DEFAULT_SIM_BACKENDS]

    def test_least_conn_and_p2c_cut_the_tail_that_a_slow_backend_causes(self):
        rr = main.simulate(self.specs(), "round_robin", 150, 4000, seed=4)
        lc = main.simulate(self.specs(), "least_conn", 150, 4000, seed=4)
        p2c = main.simulate(self.specs(), "p2c", 150, 4000, seed=4)
        self.assertAlmostEqual(rr["share"]["slow"], 1 / 3, delta=0.01)
        self.assertLess(lc["share"]["slow"], 0.25)
        self.assertLess(p2c["share"]["slow"], 0.25)
        self.assertGreater(rr["latency_ms"]["p99"], 5 * lc["latency_ms"]["p99"])
        self.assertGreater(rr["latency_ms"]["p99"], 5 * p2c["latency_ms"]["p99"])
        self.assertEqual(rr["served"], 4000)

    def test_weighted_round_robin_shares_follow_the_weights(self):
        specs = [main.parse_sim_spec("big=10,capacity=50,weight=3"), main.parse_sim_spec("small=10,capacity=50")]
        result = main.simulate(specs, "weighted_round_robin", 100, 2000)
        self.assertEqual(result["share"], {"big": 0.75, "small": 0.25})

    def test_simulation_is_reproducible_and_ejection_limits_errors(self):
        specs = [main.parse_sim_spec("a=10"), main.parse_sim_spec("b=10"), main.parse_sim_spec("broken=10,errors=1")]
        plain = main.simulate(specs, "round_robin", 100, 3000, seed=9)
        self.assertEqual(plain, main.simulate(specs, "round_robin", 100, 3000, seed=9))
        self.assertAlmostEqual(plain["errors"] / 3000, 1 / 3, delta=0.01)
        capped = main.simulate(specs, "round_robin", 100, 3000, seed=9, outlier=main.OutlierPolicy(consecutive_5xx=3, base_ejection=1))
        self.assertEqual((capped["ejections"], capped["errors"]), (0, plain["errors"]))
        self.assertGreater(capped["ejections_refused"], 0)
        ejecting = main.simulate(specs, "round_robin", 100, 3000, seed=9, outlier=main.OutlierPolicy(consecutive_5xx=3, base_ejection=1, max_ejection_percent=34))
        self.assertGreater(ejecting["ejections"], 0)
        self.assertLess(ejecting["errors"] / 3000, 0.05)
        keyed = main.simulate(specs[:2], "ring_hash", 100, 1000, keys=50)
        self.assertEqual(sum(keyed["share"].values()), 1.0)

    def test_percentile_is_nearest_rank(self):
        values = list(range(1, 101))
        self.assertEqual([main.percentile(values, p) for p in (50, 95, 99, 100)], [50, 95, 99, 100])
        self.assertEqual(main.percentile([], 99), 0.0)


class LiveProxyTests(unittest.TestCase):
    def setUp(self):
        self.echo = {}
        servers = []
        for name in ("b1", "b2", "b3"):
            server = main.EchoBackend(("127.0.0.1", 0), name)
            self.echo[name] = server
            servers.append(server)
        self.clock = FakeClock()

    def start(self, servers):
        for server in servers:
            threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
            self.addCleanup(server.server_close)
            self.addCleanup(server.shutdown)

    def balancer(self, **pool_options):
        pool_backends = [main.Backend(name, address=f"127.0.0.1:{s.server_address[1]}") for name, s in self.echo.items()]
        pool = main.Pool(pool_backends, pool_options.pop("algorithm", main.RoundRobin()), clock=self.clock, **pool_options)
        lb = main.LoadBalancer(("127.0.0.1", 0), pool)
        self.start(list(self.echo.values()) + [lb])
        self.base = f"http://127.0.0.1:{lb.server_address[1]}"
        return pool

    def request(self, path="/", method="GET", headers=None, body=None):
        request = urllib.request.Request(self.base + path, data=body, method=method, headers=headers or {})
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                return response.status, dict(response.getheaders()), response.read()
        except urllib.error.HTTPError as err:
            return err.code, dict(err.headers.items()), err.read()

    def served_by(self, **kwargs):
        status, headers, body = self.request(**kwargs)
        return status, headers.get("x-minilb-backend"), headers

    def test_round_robin_over_real_sockets_with_forwarded_for(self):
        self.balancer()
        seen = []
        for _ in range(9):
            status, headers, body = self.request("/orders", headers={"X-Trace": "t1"})
            payload = json.loads(body)
            self.assertEqual((status, payload["backend"]), (200, headers["x-minilb-backend"]))
            self.assertEqual((payload["headers"]["x-forwarded-for"], payload["headers"]["x-trace"]), ("127.0.0.1", "t1"))
            seen.append(payload["backend"])
        self.assertEqual(collections.Counter(seen), {"b1": 3, "b2": 3, "b3": 3})
        self.assertEqual({n: s.counts["requests"] for n, s in self.echo.items()}, {"b1": 3, "b2": 3, "b3": 3})

    def test_a_failing_backend_is_ejected_after_consecutive_5xx_and_returns_after_the_ejection(self):
        pool = self.balancer(outlier=main.OutlierPolicy(consecutive_5xx=2, base_ejection=30, max_ejection_percent=50))
        self.echo["b3"].status = 500
        statuses = [self.served_by()[:2] for _ in range(12)]
        self.assertEqual([s for s in statuses if s[0] == 500], [(500, "b3"), (500, "b3")])
        self.assertEqual(self.echo["b3"].counts["requests"], 2)
        self.assertEqual(pool.status()["backends"][2]["state"], "ejected")
        self.echo["b3"].status = 200
        self.clock.now += 30
        self.assertIn("b3", [self.served_by()[1] for _ in range(3)])

    def test_get_is_retried_over_real_sockets_and_post_is_not(self):
        self.balancer(retry=main.RetryPolicy(retries=1, retry_on=("5xx",)))
        self.echo["b2"].status = 500
        results = [self.served_by() for _ in range(6)]
        self.assertEqual({s for s, _, _ in results}, {200})
        self.assertIn("b2:500,b3:200", [h["x-minilb-attempts"] for _, _, h in results])
        posts = [self.served_by(method="POST", body=b"{}") for _ in range(3)]
        failed = [h for s, b, h in posts if b == "b2"]
        self.assertEqual(len(failed), 1)
        self.assertEqual(failed[0]["x-minilb-note"], "not retried: POST is not idempotent")

    def test_sticky_cookie_and_draining_with_a_request_in_flight(self):
        pool = self.balancer(sticky_cookie="MINILB")
        status, headers, _ = self.request()
        cookie = headers["Set-Cookie"].split(";")[0]
        owner = cookie.split("=")[1]
        self.assertEqual({self.served_by(headers={"Cookie": cookie})[1] for _ in range(5)}, {owner})
        slow = {}
        worker = threading.Thread(target=lambda: slow.update(zip(("status", "headers", "body"), self.request("/report?delay=0.8", headers={"Cookie": cookie}))))
        worker.start()
        deadline = time.time() + 3
        while pool.find(owner).active == 0 and time.time() < deadline:
            time.sleep(0.01)
        status, _, body = self.request(f"/_minilb/drain?backend={owner}&timeout=30", method="POST", body=b"")
        self.assertEqual((status, json.loads(body)), (200, {"backend": owner, "state": "draining", "in_flight": 1}))
        others = [self.served_by(headers={"Cookie": cookie}) for _ in range(4)]
        self.assertNotIn(owner, {b for _, b, _ in others})
        self.assertTrue(all(h["Set-Cookie"] != headers["Set-Cookie"] for _, _, h in others))
        worker.join(timeout=5)
        self.assertEqual((slow["status"], json.loads(slow["body"])["backend"]), (200, owner))
        status, _, body = self.request("/_minilb/status")
        report = json.loads(body)
        self.assertNotIn(owner, [b["name"] for b in report["backends"]])
        self.assertEqual(report["events"][-1], {"t": 0.0, "backend": owner, "event": "removed", "reason": "drained: no requests in flight"})

    def test_no_healthy_upstream_admin_errors_and_adding_a_backend(self):
        pool = self.balancer()
        for backend in pool.backends:
            backend.healthy = False
        status, headers, body = self.request()
        self.assertEqual((status, body, headers["x-minilb-backend"]), (503, b"no healthy upstream\n", "-"))
        self.assertEqual(self.request("/_minilb/drain?backend=nope", method="POST", body=b"")[0], 404)
        self.assertEqual(self.request("/_minilb/reload", method="POST", body=b"")[0], 404)
        self.assertEqual(self.request("/_minilb/add?backend=b9", method="POST", body=b"")[0], 400)
        extra = main.EchoBackend(("127.0.0.1", 0), "b4")
        self.start([extra])
        status, _, _ = self.request(f"/_minilb/add?backend=b4&address=127.0.0.1:{extra.server_address[1]}", method="POST", body=b"")
        self.assertEqual(status, 201)
        self.assertEqual(self.served_by()[:2], (200, "b4"))

    def test_head_keeps_the_upstream_length_and_chunked_bodies_are_refused(self):
        self.balancer()
        connection = http.client.HTTPConnection("127.0.0.1", int(self.base.rsplit(":", 1)[1]), timeout=5)
        self.addCleanup(connection.close)
        connection.request("HEAD", "/")
        response = connection.getresponse()
        self.assertEqual((response.status, response.read()), (200, b""))
        self.assertGreater(int(response.getheader("Content-Length")), 0)
        connection.close()
        connection.putrequest("POST", "/upload")
        connection.putheader("Transfer-Encoding", "chunked")
        connection.endheaders()
        connection.send(b"0\r\n\r\n")
        self.assertEqual(connection.getresponse().status, 411)


class CliTests(unittest.TestCase):
    def run_main(self, argv):
        out, err = StringIO(), StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = main.main(argv)
        return code, out.getvalue(), err.getvalue()

    def test_pick_remap_simulate_and_print_config(self):
        code, out, _ = self.run_main(["pick", "--algorithm", "weighted_round_robin", "--backends", "a:5,b:1,c:1", "--count", "7"])
        self.assertEqual((code, out.splitlines()[0]), (0, "a a b a c a a"))
        code, out, _ = self.run_main(["pick", "--algorithm", "least_conn", "--backends", "a,b,c", "--active", "a=4,b=1,c=2", "--count", "1", "--json"])
        self.assertEqual(json.loads(out)["sequence"], ["b"])
        code, out, _ = self.run_main(["pick", "--algorithm", "maglev", "--backends", "a,b,c", "--table-size", "251", "--key", "u1", "--key", "u1", "--json"])
        self.assertEqual(list(json.loads(out)["keys"]), ["u1"])
        code, out, _ = self.run_main(["remap", "--backends", "a,b,c", "--add", "d", "--keys", "2000", "--table-size", "251", "--json"])
        rows = {r["algorithm"]: r for r in json.loads(out)["results"]}
        self.assertEqual(rows["ring_hash"]["moved_from_kept"], rows["ring_hash"]["moved"])
        self.assertAlmostEqual(rows["ring_hash"]["moved"], rows["ring_hash"]["share"]["d"], places=4)
        code, out, _ = self.run_main(["simulate", "--requests", "300", "--algorithms", "round_robin,maglev", "--keys", "20", "--table-size", "251", "--json"])
        self.assertEqual([r["algorithm"] for r in json.loads(out)], ["round_robin", "maglev"])
        code, out, _ = self.run_main(["simulate", "--requests", "300"])
        self.assertIn("least_conn", out)
        code, out, _ = self.run_main(["serve", "--backend", "b1=127.0.0.1:9001", "--backend", "b2=127.0.0.1:9002,weight=3", "--algorithm", "ring_hash", "--print-config"])
        config = json.loads(out)
        self.assertEqual((code, config["backends"][1]["weight"], config["health"]["fall"]), (0, 3, 3))
        self.assertIn("falls back to round robin", config["warnings"][0])
        self.assertIn("forbids every ejection; raise it to 50", config["warnings"][1])

    def test_errors_exit_two_with_the_reason(self):
        cases = [
            (["serve", "--backend", "b1"], "expected NAME=HOST:PORT"),
            (["serve", "--backend", "b1=localhost"], "expected HOST:PORT"),
            (["serve", "--backend", "b1=127.0.0.1:1", "--retry-on", "always"], "choose from connect-failure"),
            (["serve", "--backend", "b1=127.0.0.1:1", "--hash-on", "query"], "expected header:NAME"),
            (["serve"], "at least one --backend"),
            (["pick", "--algorithm", "maglev", "--backends", "a,b", "--table-size", "100", "--key", "k"], "must be a prime"),
            (["pick", "--algorithm", "round_robin", "--backends", "a:0"], "WEIGHT >= 1"),
            (["remap", "--backends", "a,b", "--remove", "c"], "not one of a, b"),
            (["simulate", "--backend", "slow=fast"], "numbers expected"),
        ]
        for argv, message in cases:
            code, _, err = self.run_main(argv)
            self.assertEqual(code, 2, argv)
            self.assertIn(message, err, argv)


if __name__ == "__main__":
    unittest.main()
