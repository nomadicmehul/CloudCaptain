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
from io import StringIO

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import main  # noqa: E402

VLLM_METRICS = """# HELP vllm:num_requests_waiting Number of requests waiting
# TYPE vllm:num_requests_waiting gauge
vllm:num_requests_waiting{model_name="cc-sim"} 3
vllm:num_requests_running{model_name="cc-sim"} 2
vllm:num_requests_running{model_name="cc-sim-lora"} 1
vllm:kv_cache_usage_perc{model_name="cc-sim"} 0.25
vllm:prefix_cache_hits_total{model_name="cc-sim"} 288
vllm:prefix_cache_queries_total{model_name="cc-sim"} 400
vllm:cache_config_info{block_size="16",cache_dtype="auto",num_gpu_blocks="64"} 1
process_start_time_seconds 1.7e+09
"""


def trace_requests(**kwargs):
    return [main.parse_trace_line(line, i) for i, line in enumerate(main.make_trace(**kwargs))]


def chat(system, user, model="cc-sim"):
    return {"model": model, "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}], "max_tokens": 1}


def pool(ports=None, labels=None, ref=None, app=None, name="sim-pool", namespace="cc"):
    spec = {"selector": {"matchLabels": {"app": "cc-sim"} if labels is None else labels}, "targetPorts": [{"number": 8000}] if ports is None else ports}
    if ref is not None:
        spec["endpointPickerRef"] = ref
    if app is not None:
        spec["appProtocol"] = app
    return {"apiVersion": "inference.networking.k8s.io/v1", "kind": "InferencePool", "metadata": {"name": name, "namespace": namespace}, "spec": spec}


def pod(name, ip="10.0.0.1", labels=None, ready=True, phase="Running", namespace="cc"):
    return {"kind": "Pod", "metadata": {"name": name, "namespace": namespace, "labels": {"app": "cc-sim"} if labels is None else labels}, "status": {"phase": phase, "podIP": ip, "conditions": [{"type": "Ready", "status": "True" if ready else "False"}]}}


def run_cli(args):
    out, err = StringIO(), StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = main.main(args)
    return code, out.getvalue(), err.getvalue()


class TokenizerTests(unittest.TestCase):
    def test_fnv1a_known_values(self):
        self.assertEqual(main.fnv1a_32(""), 0x811C9DC5)
        self.assertEqual(main.fnv1a_32("a"), 0xE40C292C)

    def test_tokenize_splits_words_and_punctuation_keeping_trailing_space(self):
        self.assertEqual(main.tokenize("Hello, world!"), [main.fnv1a_32("Hello"), main.fnv1a_32(", "), main.fnv1a_32("world"), main.fnv1a_32("!")])

    def test_prompt_text_puts_the_system_prompt_first_and_handles_completions(self):
        text = main.prompt_text(chat("You are the bot.", "Hi"))
        self.assertTrue(text.startswith("<|system|>You are the bot.<|end|>"))
        self.assertTrue(text.endswith("<|user|>Hi<|end|>"))
        self.assertEqual(main.prompt_text({"prompt": ["a", "b"]}), "ab")
        with self.assertRaises(main.InputError):
            main.prompt_text({"model": "x"})
        with self.assertRaises(main.InputError):
            main.prompt_text({"messages": []})


class BlockHashTests(unittest.TestCase):
    def test_shared_prefix_shares_leading_hashes(self):
        a = main.block_hashes(list(range(40)), 16)
        b = main.block_hashes(list(range(32)) + [999] * 8, 16)
        self.assertEqual(len(a), 3)
        self.assertEqual(a[:2], b[:2])
        self.assertNotEqual(a[2], b[2])

    def test_a_change_in_the_first_block_changes_every_later_hash(self):
        a = main.block_hashes(list(range(48)), 16)
        b = main.block_hashes([7] + list(range(1, 48)), 16)
        self.assertTrue(all(x != y for x, y in zip(a, b)))

    def test_model_and_salt_change_the_chain_seed(self):
        tokens = list(range(32))
        base = main.block_hashes(tokens, 16, "model-a")
        self.assertNotEqual(base, main.block_hashes(tokens, 16, "model-b"))
        self.assertNotEqual(base, main.block_hashes(tokens, 16, "model-a", "tenant-1"))
        self.assertNotEqual(main.block_hashes(tokens, 16, "ab", "c"), main.block_hashes(tokens, 16, "a", "bc"))

    def test_partial_block_only_when_asked(self):
        self.assertEqual(len(main.block_hashes(list(range(40)), 16, include_partial=True)), 3)
        self.assertEqual(len(main.block_hashes(list(range(40)), 16, include_partial=False)), 2)

    def test_block_size_must_be_positive(self):
        with self.assertRaises(main.InputError):
            main.block_hashes([1, 2, 3], 0)


class IndexAndCacheTests(unittest.TestCase):
    def test_longest_match_stops_at_the_first_missing_block(self):
        index = main.PrefixIndex()
        index.record([1, 2, 3], "a")
        self.assertEqual(index.longest_match([1, 2, 3, 4], "a"), 3)
        self.assertEqual(index.longest_match([1, 9, 3], "a"), 1)
        self.assertEqual(index.longest_match([1, 2], "b"), 0)

    def test_index_lru_evicts_the_oldest_blocks(self):
        index = main.PrefixIndex(capacity=3)
        index.record([1, 2], "a")
        index.record([3], "a")
        index.record([1], "a")
        self.assertEqual(index.record([4], "a"), 1)
        self.assertEqual(index.longest_match([2], "a"), 0)
        self.assertEqual(index.longest_match([1], "a"), 1)

    def test_server_cache_hits_full_blocks_of_a_shared_prefix(self):
        cache = main.BlockCache(64, 16)
        prefix = list(range(40))
        self.assertEqual(cache.admit(prefix + [100, 101]), 0)
        self.assertEqual(cache.admit(prefix + [200, 201]), 32)
        self.assertEqual((cache.hit_tokens, cache.query_tokens), (32, 84))

    def test_a_full_prompt_hit_still_recomputes_the_last_block(self):
        cache = main.BlockCache(64, 16)
        cache.admit(list(range(32)))
        self.assertEqual(cache.admit(list(range(32))), 16)

    def test_server_cache_evicts_least_recently_used_blocks(self):
        cache = main.BlockCache(2, 16)
        a, b = list(range(33)), list(range(100, 133))
        cache.admit(a)
        cache.admit(b)
        self.assertEqual(cache.admit(a), 0)


class ScorerTests(unittest.TestCase):
    def test_queue_scores_are_min_max_and_neutral_when_equal(self):
        endpoints = [main.Endpoint("a", waiting=0, has_metrics=True), main.Endpoint("b", waiting=5, has_metrics=True), main.Endpoint("c", waiting=10, has_metrics=True)]
        self.assertEqual(main.score_queue(endpoints), {"a": 1.0, "b": 0.5, "c": 0.0})
        same = [main.Endpoint("a", waiting=2, has_metrics=True), main.Endpoint("b", waiting=2, has_metrics=True)]
        self.assertEqual(main.score_queue(same), {"a": 1.0, "b": 1.0})

    def test_kv_scores_and_unmeasured_endpoints_stay_unscored(self):
        endpoints = [main.Endpoint("a", kv_usage=0.25, has_metrics=True), main.Endpoint("b")]
        self.assertEqual(main.score_kv(endpoints), {"a": 0.75})
        self.assertEqual(main.score_queue([main.Endpoint("b")]), {})

    def test_no_hit_lru_prefers_never_used_then_oldest_and_is_neutral_when_warm(self):
        self.assertEqual(main.score_no_hit_lru(["a", "b", "c"], True, []), {"a": 1.0, "b": 1.0, "c": 1.0})
        scores = main.score_no_hit_lru(["a", "b", "c"], True, ["a", "b"])
        self.assertEqual(scores["c"], 1.0)
        self.assertGreater(scores["a"], scores["b"])
        self.assertEqual(main.score_no_hit_lru(["a", "b"], False, ["a"]), {"a": 0.5, "b": 0.5})


class RouterTests(unittest.TestCase):
    def setUp(self):
        self.endpoints = [main.Endpoint(n, has_metrics=True) for n in ("a", "b", "c")]
        self.system = "You are the billing assistant. " + "Always check the invoice before you answer the customer. " * 12

    def route(self, router, user, system=None):
        decision = router.pick(main.tokenize(main.prompt_text(chat(system or self.system, user))), "cc-sim")
        router.commit(decision)
        return decision

    def test_a_repeated_prefix_goes_back_to_the_same_endpoint(self):
        router = main.Router(self.endpoints, "prefix", seed=1)
        first = self.route(router, "Where is my refund?")
        second = self.route(router, "Why was I charged twice?")
        self.assertTrue(first.cold)
        self.assertFalse(second.cold)
        self.assertEqual(second.endpoint, first.endpoint)
        self.assertGreater(second.matches[first.endpoint], 0)
        self.assertIn("match", second.describe())

    def test_cold_prefixes_are_spread_by_the_no_hit_lru_scorer(self):
        router = main.Router(self.endpoints, "prefix", seed=3)
        chosen = {self.route(router, "hello", f"You are assistant number {i}. " + "Be brief and kind. " * 10).endpoint for i in range(3)}
        self.assertEqual(chosen, {"a", "b", "c"})

    def test_load_outweighs_a_partial_match_but_not_for_prefix_only(self):
        for policy, expected in (("prefix", "b"), ("prefix-only", "a")):
            endpoints = [main.Endpoint("a", has_metrics=True), main.Endpoint("b", has_metrics=True)]
            router = main.Router(endpoints, policy, seed=0)
            tokens = main.tokenize(main.prompt_text(chat(self.system, "first")))
            router.index.record(main.block_hashes(tokens, 16, "cc-sim"), "a")
            endpoints[0].waiting, endpoints[0].kv_usage = 10, 0.95
            decision = router.pick(main.tokenize(main.prompt_text(chat(self.system, "second question"))), "cc-sim")
            self.assertEqual(decision.endpoint, expected, (policy, decision.totals))

    def test_round_robin_cycles_and_least_queue_picks_the_lowest_load(self):
        router = main.Router(self.endpoints, "round-robin")
        self.assertEqual([self.route(router, str(i)).endpoint for i in range(4)], ["a", "b", "c", "a"])
        self.endpoints[0].waiting, self.endpoints[1].inflight, self.endpoints[2].running = 3, 1, 2
        self.assertEqual(main.Router(self.endpoints, "least-queue").pick([1, 2, 3]).endpoint, "b")

    def test_router_rejects_bad_configuration(self):
        with self.assertRaises(main.InputError):
            main.Router(self.endpoints, "sticky")
        with self.assertRaises(main.InputError):
            main.Router([main.Endpoint("a"), main.Endpoint("a")])
        with self.assertRaises(main.InputError):
            main.Router(self.endpoints, weights={"latency": 1.0})
        with self.assertRaises(main.InputError):
            main.Router([])

    def test_unhealthy_endpoints_are_skipped(self):
        self.endpoints[0].healthy = False
        router = main.Router(self.endpoints, "round-robin")
        self.assertNotIn("a", {self.route(router, str(i)).endpoint for i in range(4)})


class MetricsTests(unittest.TestCase):
    def test_server_metrics_sums_series_and_reads_block_size(self):
        metrics = main.server_metrics(VLLM_METRICS)
        self.assertEqual((metrics["waiting"], metrics["running"]), (3, 3))
        self.assertEqual(metrics["kv_usage"], 0.25)
        self.assertEqual((metrics["prefix_hits"], metrics["prefix_queries"]), (288, 400))
        self.assertEqual(metrics["block_size"], 16)
        self.assertIn("vllm:prefix_cache_hits_total", metrics["found"])

    def test_missing_metrics_read_as_zero(self):
        metrics = main.server_metrics("# nothing here\nsome_other_metric 5\n")
        self.assertEqual((metrics["waiting"], metrics["prefix_queries"], metrics["block_size"]), (0, 0, None))

    def test_extract_usage_from_json_and_from_sse(self):
        body = json.dumps({"usage": {"prompt_tokens": 10, "prompt_tokens_details": {"cached_tokens": 8}}}).encode()
        self.assertEqual(main.extract_usage(body), {"prompt_tokens": 10, "cached_tokens": 8})
        sse = b'data: {"choices": []}\n\ndata: {"usage": {"prompt_tokens": 5, "prompt_tokens_details": null}}\n\ndata: [DONE]\n\n'
        self.assertEqual(main.extract_usage(sse), {"prompt_tokens": 5, "cached_tokens": 0})
        self.assertIsNone(main.extract_usage(b"not json"))


class SimulationTests(unittest.TestCase):
    def test_prefix_routing_beats_round_robin_on_shared_system_prompts(self):
        requests = trace_requests()
        config = main.SimConfig()
        prefix = main.simulate(requests, "prefix", config)
        rr = main.simulate(requests, "round-robin", config)
        self.assertGreater(prefix["hit_rate"], rr["hit_rate"] + 0.15)
        self.assertEqual(prefix["groups_on_one_endpoint"], prefix["groups_total"])
        self.assertEqual(rr["groups_on_one_endpoint"], 0)
        self.assertLess(prefix["ttft_ms"]["mean"], rr["ttft_ms"]["mean"])
        self.assertEqual(sum(prefix["per_endpoint"].values()), len(requests))

    def test_prefix_only_hot_spots_a_hot_tenant_and_the_load_scorers_spill_it(self):
        requests = trace_requests(hot=0.7, rate=40.0, requests=200)
        config = main.SimConfig(slots=2)
        only = main.simulate(requests, "prefix-only", config)
        weighted = main.simulate(requests, "prefix", config)
        self.assertGreater(max(only["per_endpoint"].values()), max(weighted["per_endpoint"].values()))
        self.assertGreater(only["ttft_ms"]["p95"], weighted["ttft_ms"]["p95"])

    def test_trace_is_deterministic_and_groups_share_their_system_prompt(self):
        first, second = main.make_trace(seed=3), main.make_trace(seed=3)
        self.assertEqual(first, second)
        by_group = {}
        for line in first:
            by_group.setdefault(line["group"], set()).add(line["body"]["messages"][0]["content"])
        self.assertTrue(all(len(systems) == 1 for systems in by_group.values()))
        self.assertGreater(len(by_group), 1)


class DisaggTests(unittest.TestCase):
    def test_kv_bytes_per_token(self):
        self.assertEqual(main.MODEL_SHAPES["llama-3.1-8b"].kv_bytes_per_token(), 131072)
        self.assertEqual(main.MODEL_SHAPES["llama-3.1-70b"].kv_bytes_per_token(), 327680)

    def test_transfer_time_ttft_and_the_decider(self):
        plan = main.disagg_plan(main.MODEL_SHAPES["llama-3.1-8b"], 4000, 0, 12000.0, 100.0, 0.5, 256)
        self.assertAlmostEqual(plan["transfer_ms"], 0.5 + 4000 * 131072 * 8 / 1e11 * 1000, places=0)
        self.assertEqual(plan["prefill_ms"], 333.3)
        self.assertAlmostEqual(plan["ttft_disaggregated_ms"], plan["prefill_ms"] + plan["transfer_ms"], delta=0.2)
        self.assertEqual(plan["decider"], "remote prefill")
        cached = main.disagg_plan(main.MODEL_SHAPES["llama-3.1-8b"], 4000, 3900, threshold_tokens=256)
        self.assertEqual((cached["uncached_tokens"], cached["decider"]), (100, "local prefill"))

    def test_kv_production_rate_chunking_and_capacity(self):
        shape = main.MODEL_SHAPES["llama-3.1-8b"]
        plan = main.disagg_plan(shape, 4000, link_gbps=10.0, chunk_tokens=512)
        self.assertEqual(plan["kv_production_gbps"], 12.6)
        self.assertFalse(plan["link_keeps_up"])
        self.assertEqual(plan["decode_stall_avoided_ms"], round(512 / 12000 * 1000, 1))
        self.assertEqual(main.kv_capacity_tokens(shape, 80.0, 0.9), int((72e9 - 8.03e9 * 2) // 131072))
        self.assertEqual(main.kv_capacity_tokens(main.MODEL_SHAPES["llama-3.1-70b"], 80.0, 0.9), 0)

    def test_disagg_rejects_impossible_inputs(self):
        with self.assertRaises(main.InputError):
            main.disagg_plan(main.MODEL_SHAPES["llama-3.1-8b"], 100, 200)
        with self.assertRaises(main.InputError):
            main.disagg_plan(main.MODEL_SHAPES["llama-3.1-8b"], 100, link_gbps=0)


class PoolTests(unittest.TestCase):
    def test_a_valid_pool_resolves_ready_matching_pods_in_its_namespace(self):
        pods = [pod("sim-1", "10.0.0.1"), pod("sim-2", "10.0.0.2", ready=False), pod("other", "10.0.0.3", labels={"app": "web"}), pod("sim-3", "10.0.0.4", namespace="elsewhere")]
        self.assertEqual(main.validate_pool(pool()), [])
        endpoints = main.pool_endpoints(pool(), pods)
        self.assertEqual([(e["name"], e["url"], e["ready"]) for e in endpoints], [("sim-1", "http://10.0.0.1:8000", True), ("sim-2", "http://10.0.0.2:8000", False)])
        two_ports = main.pool_endpoints(pool(ports=[{"number": 8000}, {"number": 8001}]), pods[:1])
        self.assertEqual([e["name"] for e in two_ports], ["sim-1-8000", "sim-1-8001"])

    def test_crd_validation_messages(self):
        duplicate = main.validate_pool(pool(ports=[{"number": 8000}, {"number": 8000}]))
        self.assertTrue(any(main.UNIQUE_PORT_MESSAGE in p for p in duplicate), duplicate)
        no_port = main.validate_pool(pool(ref={"name": "epp"}))
        self.assertTrue(any(main.SERVICE_PORT_MESSAGE in p for p in no_port), no_port)
        self.assertEqual(main.validate_pool(pool(ref={"name": "epp", "port": {"number": 9002}, "failureMode": "FailOpen"})), [])
        self.assertEqual(main.validate_pool(pool(ref={"name": "epp", "kind": "Deployment", "group": "apps"})), [])

    def test_empty_selector_too_many_ports_and_bad_protocol(self):
        self.assertTrue(any("matchLabels" in p for p in main.validate_pool(pool(labels={}))))
        self.assertTrue(any("between 1 and 8" in p for p in main.validate_pool(pool(ports=[{"number": 8000 + i} for i in range(9)]))))
        self.assertTrue(any("appProtocol" in p for p in main.validate_pool(pool(app="grpc"))))
        self.assertTrue(any("65535" in p for p in main.validate_pool(pool(ports=[{"number": 70000}]))))


class HttpTests(unittest.TestCase):
    def start(self, server):
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return f"http://127.0.0.1:{server.server_address[1]}"

    def fleet(self, policy):
        urls = [(name, self.start(main.FakeModelServer(("127.0.0.1", 0), name, prefill_overhead_ms=0, prefill_ms_per_token=0))) for name in ("sim-a", "sim-b", "sim-c")]
        router = main.Router([main.Endpoint(n, u) for n, u in urls], policy, seed=2)
        main.Scraper(router, 1.0).scrape_once()
        server = main.RouterServer(("127.0.0.1", 0), router)
        return self.start(server), urls, server

    def test_prefix_router_keeps_each_tenant_on_one_server_and_the_servers_confirm_hits(self):
        url, urls, server = self.fleet("prefix")
        requests = trace_requests(groups=4, requests=16, prefix_words=60)
        summary = main.replay(requests, url, concurrency=2, metrics=urls, wait=2.0)
        self.assertEqual(summary["errors"], 0, summary["first_error"])
        self.assertEqual(summary["groups_on_one_endpoint"], summary["groups_total"])
        self.assertTrue(summary["server"]["synced"])
        self.assertGreater(summary["server"]["hit_rate"], 0.5)
        self.assertEqual(summary["client_hit_rate"], summary["server"]["hit_rate"])
        state = json.loads(urllib.request.urlopen(url + "/prefixroute/state", timeout=5).read())
        self.assertEqual(sum(e["requests"] for e in state["endpoints"]), 16)
        self.assertTrue(all(e["indexed_blocks"] > 0 for e in state["endpoints"] if e["requests"]))

    def test_round_robin_spreads_tenants_and_hits_less(self):
        url, urls, _ = self.fleet("round-robin")
        summary = main.replay(trace_requests(groups=4, requests=16, prefix_words=60), url, metrics=urls, wait=2.0)
        self.assertEqual(summary["groups_on_one_endpoint"], 0)
        self.assertEqual(sorted(summary["per_endpoint"].values()), [5, 5, 6])

    def test_router_headers_bad_json_and_dead_endpoints(self):
        url, _, _ = self.fleet("prefix")
        request = urllib.request.Request(url + "/v1/chat/completions", data=json.dumps(chat("sys", "hi")).encode(), headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(request, timeout=5) as response:
            self.assertIn(response.headers["x-prefixroute-endpoint"], ("sim-a", "sim-b", "sim-c"))
            self.assertEqual(response.headers["x-prefixroute-policy"], "prefix")
            self.assertEqual(json.loads(response.read())["usage"]["prompt_tokens_details"]["cached_tokens"], 0)
        with self.assertRaises(urllib.error.HTTPError) as bad:
            urllib.request.urlopen(urllib.request.Request(url + "/v1/chat/completions", data=b"{not json"), timeout=5)
        self.assertEqual(bad.exception.code, 400)
        probe = main.FakeModelServer(("127.0.0.1", 0), "gone")
        dead_url = f"http://127.0.0.1:{probe.server_address[1]}"
        probe.server_close()
        dead = main.RouterServer(("127.0.0.1", 0), main.Router([main.Endpoint("gone", dead_url)]))
        dead_router = self.start(dead)
        status, _, _, _ = main.send_request(dead_router, "/v1/chat/completions", chat("sys", "hi"), 5.0)
        self.assertEqual(status, 502)

    def test_scraper_reads_the_fake_servers_metrics(self):
        _, urls, server = self.fleet("prefix")
        for endpoint in server.router.endpoints.values():
            self.assertTrue(endpoint.has_metrics)
            self.assertEqual(endpoint.block_size, 16)
        self.assertEqual(main.scrape_server(urls[0][1])["prefix_queries"], 0)


class CliTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp)

    def write(self, name, data):
        path = os.path.join(self.tmp, name)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(data if isinstance(data, str) else json.dumps(data))
        return path

    def test_trace_then_simulate_as_json(self):
        path = os.path.join(self.tmp, "trace.jsonl")
        code, out, _ = run_cli(["trace", "-o", path, "--groups", "3", "--requests", "12"])
        self.assertEqual(code, 0)
        self.assertIn("12 requests", out)
        code, out, _ = run_cli(["simulate", path, "--json"])
        reports = {r["policy"]: r for r in json.loads(out)}
        self.assertEqual(set(reports), set(main.POLICIES))
        code, out, _ = run_cli(["simulate", path, "--policy", "prefix", "--show-decisions", "2"])
        self.assertEqual(code, 0)
        self.assertIn("#1 tenant-", out)

    def test_pool_exit_codes_and_args(self):
        good = self.write("good.json", {"kind": "List", "items": [pool(), pod("sim-1", "10.0.0.9")]})
        code, out, _ = run_cli(["pool", good, "--args"])
        self.assertEqual((code, out.strip()), (0, "--endpoint sim-1=http://10.0.0.9:8000"))
        bad = self.write("bad.json", pool(ports=[{"number": 1}, {"number": 1}]))
        code, _, err = run_cli(["pool", bad])
        self.assertEqual(code, 2)
        self.assertIn(main.UNIQUE_PORT_MESSAGE, err)

    def test_bad_trace_and_disagg_text(self):
        path = self.write("bad.jsonl", '{"t": 0, "body": {"model": "x"}}\n')
        code, _, err = run_cli(["simulate", path])
        self.assertEqual(code, 2)
        self.assertIn("line 1", err)
        code, out, _ = run_cli(["disagg", "--link-gbps", "25"])
        self.assertEqual(code, 0)
        self.assertIn("131072 bytes of KV per token", out)


if __name__ == "__main__":
    unittest.main()
