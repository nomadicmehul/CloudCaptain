import contextlib
import http.server
import io
import json
import math
import os
import sys
import tempfile
import threading
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import main  # noqa: E402

FIXTURES = os.path.join(os.path.dirname(__file__), "..", "fixtures")


def fixture(name):
    with open(os.path.join(FIXTURES, name), encoding="utf-8") as handle:
        return handle.read()


def replica(name="r", running=0.0, waiting=0.0, kv=None, cpu=None):
    return main.ReplicaMetrics(name, "sim", running, waiting, kv, cpu_percent=cpu)


def queue_policy(target=8.0, **overrides):
    return main.Policy(metric="queue", target=target, **overrides).validate()


def run_cli(*argv):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = main.main(list(argv))
    return code, out.getvalue(), err.getvalue()


class PrometheusTextTests(unittest.TestCase):
    def test_labels_escapes_special_values_and_timestamps(self):
        text = '\n'.join([
            '# HELP demo_total A counter with "quotes" in its help.',
            '# TYPE demo_total counter',
            'demo_total{path="/v1/chat",note="say \\"hi\\"\\n",dir="C:\\\\models",} 3 1730000000000',
            'demo_gauge NaN',
            'demo_bucket{le="+Inf"} +Inf',
        ])
        parsed = main.parse_exposition(text)
        first = parsed.samples[0]
        self.assertEqual(first.label("note"), 'say "hi"\n')
        self.assertEqual(first.label("dir"), "C:\\models")
        self.assertEqual(first.value, 3.0)
        self.assertTrue(math.isnan(parsed.samples[1].value))
        self.assertEqual(parsed.samples[2].value, math.inf)
        self.assertEqual(parsed.types["demo_total"], "counter")
        self.assertEqual(parsed.values("demo_gauge"), [])

    def test_malformed_lines_name_the_line(self):
        for text, token in [('ok 1\nbad{x="1" 2', "metrics:2"), ('m{x=1} 2', "quoted value"), ("m{x=\"1\"}", "expected a value"), ("m 1 2 3", "expected a value"), ("m abc", "not a sample value")]:
            with self.assertRaises(main.InputError) as caught:
                main.parse_exposition(text)
            self.assertIn(token, str(caught.exception))

    def test_vllm_page_becomes_queue_kv_tokens_and_cpu(self):
        metrics = main.replica_metrics(main.parse_exposition(fixture("vllm-busy.prom")), "pod-a")
        self.assertEqual((metrics.server, metrics.running, metrics.waiting, metrics.queue), ("vllm", 7.0, 12.0, 19.0))
        self.assertEqual(metrics.kv_usage, 0.83)
        self.assertEqual((metrics.prompt_tokens, metrics.generation_tokens, metrics.cpu_seconds), (1234567.0, 245000.0, 5321.4))

    def test_older_vllm_kv_name_llamacpp_and_sglang_map_to_the_same_signals(self):
        idle = main.replica_metrics(main.parse_exposition(fixture("vllm-idle.prom")), "idle")
        self.assertEqual((idle.queue, idle.kv_usage), (0.0, 0.0))
        llama = main.replica_metrics(main.parse_exposition(fixture("llamacpp.prom")), "llama")
        self.assertEqual((llama.server, llama.running, llama.waiting, llama.kv_usage, llama.generation_tokens), ("llama.cpp", 4.0, 3.0, None, 6120.0))
        sglang = main.replica_metrics(main.parse_exposition(fixture("sglang.prom")), "sglang")
        self.assertEqual((sglang.server, sglang.queue, sglang.kv_usage), ("sglang", 2988.0, 0.28))
        sim = main.replica_metrics(main.parse_exposition(fixture("sim-loaded.prom")), "sim")
        self.assertEqual((sim.server, sim.running, sim.waiting, sim.cpu_seconds), ("vllm", 2.0, 10.0, 1.27))

    def test_engines_are_summed_and_kv_takes_the_fullest(self):
        text = '\n'.join([
            'vllm:num_requests_running{engine="0"} 3',
            'vllm:num_requests_running{engine="1"} 5',
            'vllm:num_requests_waiting{engine="0"} 1',
            'vllm:kv_cache_usage_perc{engine="0"} 0.4',
            'vllm:kv_cache_usage_perc{engine="1"} 0.9',
        ])
        metrics = main.replica_metrics(main.parse_exposition(text), "dp2")
        self.assertEqual((metrics.running, metrics.waiting, metrics.kv_usage), (8.0, 1.0, 0.9))

    def test_a_page_without_queue_metrics_says_what_was_expected(self):
        with self.assertRaises(main.InputError) as caught:
            main.replica_metrics(main.parse_exposition("process_cpu_seconds_total 4\nhttp_requests_total 10"), "web")
        self.assertIn("vllm:num_requests_waiting", str(caught.exception))
        self.assertIn("--metrics", str(caught.exception))

    def test_counter_rates_handle_resets(self):
        metrics = main.ReplicaMetrics("a", generation_tokens=1500.0, cpu_seconds=12.0)
        main.add_rates(metrics, {"t": 100.0, "generation_tokens": 1000.0, "cpu_seconds": 10.0}, 110.0, 0.5)
        self.assertEqual(metrics.generation_per_s, 50.0)
        self.assertAlmostEqual(metrics.cpu_percent, 40.0)
        restarted = main.ReplicaMetrics("a", generation_tokens=200.0)
        main.add_rates(restarted, {"t": 100.0, "generation_tokens": 1000.0}, 110.0)
        self.assertEqual(restarted.generation_per_s, 20.0)


class DecisionTests(unittest.TestCase):
    def test_queue_target_is_hpa_average_value(self):
        decision = main.decide(queue_policy(4), main.Observation(0.0, 1, [replica(running=2, waiting=10)]), main.ScalerState())
        self.assertEqual((decision.raw, decision.desired, decision.direction, decision.reason), (3, 3, "up", "metric"))
        self.assertIn("queue 12 / target 4 per replica = 3", decision.steps[0])

    def test_scale_up_is_rate_limited_to_double_or_four_pods(self):
        decision = main.decide(queue_policy(4, max_replicas=50), main.Observation(0.0, 2, [replica(running=40, waiting=40)]), main.ScalerState())
        self.assertEqual((decision.raw, decision.desired, decision.reason), (20, 6, "rate-limited"))
        big = main.decide(queue_policy(4, max_replicas=50), main.Observation(0.0, 10, [replica(running=400)]), main.ScalerState())
        self.assertEqual(big.desired, 20)

    def test_tolerance_keeps_the_current_count(self):
        decision = main.decide(queue_policy(8), main.Observation(0.0, 4, [replica(running=33)]), main.ScalerState())
        self.assertEqual((decision.desired, decision.reason), (4, "tolerance"))

    def test_scale_down_waits_for_the_stabilisation_window(self):
        policy = queue_policy(4, down_window_s=300)
        state = main.ScalerState()
        self.assertEqual(main.decide(policy, main.Observation(0.0, 6, [replica(running=24)]), state).desired, 6)
        held = main.decide(policy, main.Observation(60.0, 6, [replica()]), state)
        self.assertEqual((held.desired, held.reason), (6, "stabilised"))
        self.assertIn("highest recommendation in the last 300 s is 6", " ".join(held.steps))
        later = main.decide(policy, main.Observation(301.0, 6, [replica()]), state)
        self.assertEqual((later.desired, later.direction), (1, "down"))

    def test_up_window_takes_the_lowest_recent_recommendation(self):
        policy = queue_policy(4, up_window_s=60)
        state = main.ScalerState()
        main.decide(policy, main.Observation(0.0, 2, [replica(running=8)]), state)
        spike = main.decide(policy, main.Observation(15.0, 2, [replica(running=32)]), state)
        self.assertEqual((spike.raw, spike.desired), (8, 2))
        sustained = main.decide(policy, main.Observation(75.0, 2, [replica(running=32)]), state)
        self.assertEqual(sustained.desired, 6)

    def test_min_and_max_clamp(self):
        self.assertEqual(main.decide(queue_policy(4, max_replicas=3), main.Observation(0.0, 3, [replica(waiting=100)]), main.ScalerState()).reason, "max")
        low = main.decide(queue_policy(4, min_replicas=2, down_window_s=0), main.Observation(0.0, 4, [replica()]), main.ScalerState())
        self.assertEqual((low.desired, low.reason), (2, "min"))

    def test_scale_to_zero_after_the_cooldown_and_back_from_held_requests(self):
        policy = queue_policy(4, min_replicas=0, down_window_s=0, zero_cooldown_s=120)
        state = main.ScalerState()
        main.decide(policy, main.Observation(0.0, 1, [replica(running=1)]), state)
        cooling = main.decide(policy, main.Observation(60.0, 1, [replica()]), state)
        self.assertEqual((cooling.desired, cooling.reason), (1, "min"))
        self.assertIn("idle for 60 s of the 120 s cooldown", " ".join(cooling.steps))
        zero = main.decide(policy, main.Observation(120.0, 1, [replica()]), state)
        self.assertEqual((zero.desired, zero.reason), (0, "idle-to-zero"))
        quiet = main.decide(policy, main.Observation(200.0, 0, [], held=0), state)
        self.assertEqual((quiet.desired, quiet.reason), (0, "idle"))
        woken = main.decide(policy, main.Observation(215.0, 0, [], held=3), state)
        self.assertEqual((woken.desired, woken.reason), (1, "activate-from-zero"))
        burst = main.decide(policy, main.Observation(230.0, 0, [], held=30), state)
        self.assertEqual(burst.desired, 4)

    def test_cpu_utilisation_uses_the_hpa_ratio_and_cannot_start_from_zero(self):
        policy = main.Policy(metric="cpu", target=60).validate()
        decision = main.decide(policy, main.Observation(0.0, 3, [replica(cpu=90), replica(cpu=90), replica(cpu=90)]), main.ScalerState())
        self.assertEqual(decision.desired, 5)
        missing = main.decide(policy, main.Observation(0.0, 2, [replica(running=50), replica(running=50)]), main.ScalerState())
        self.assertEqual((missing.desired, missing.reason), (2, "no-samples"))
        for bad in [dict(metric="cpu", target=60, min_replicas=0), dict(metric="kv", target=80), dict(metric="cpu", target=60, lookahead_s=90), dict(metric="tokens", target=1)]:
            with self.assertRaises(main.InputError):
                main.Policy(**bad).validate()

    def test_lookahead_projects_growth_over_the_cold_start(self):
        policy = queue_policy(8, lookahead_s=90, max_replicas=20)
        state = main.ScalerState()
        main.decide(policy, main.Observation(0.0, 2, [replica(running=10)]), state)
        decision = main.decide(policy, main.Observation(15.0, 2, [replica(running=25)]), state)
        self.assertEqual(decision.raw, 15)
        self.assertIn("grows 1.00/s", decision.steps[0])

    def test_policy_specs_and_state_round_trip(self):
        policy = main.parse_policy("queue:24,min=0,cooldown=120,lookahead=90")
        self.assertEqual((policy.metric, policy.target, policy.min_replicas, policy.zero_cooldown_s, policy.lookahead_s, policy.label), ("queue", 24.0, 0, 120.0, 90.0, "queue:24,min=0,cooldown=120,lookahead=90"))
        for spec, token in [("queue", "METRIC:TARGET"), ("queue:x", "not a number"), ("queue:4,speed=2", "unknown option"), ("queue:4,min=a", "not a number")]:
            with self.assertRaises(main.InputError) as caught:
                main.parse_policy(spec)
            self.assertIn(token, str(caught.exception))
        state = main.ScalerState([(1.0, 3)], 5.0, (5.0, 12.0), {"pod": {"t": 5.0}})
        again = main.ScalerState.from_json(json.loads(json.dumps(state.to_json())))
        self.assertEqual((again.history, again.last_active, again.last_demand, again.counters), (state.history, 5.0, (5.0, 12.0), {"pod": {"t": 5.0}}))


class SimulatorTests(unittest.TestCase):
    def test_gpu_profile_is_derived_from_memory_bandwidth_and_flops(self):
        gpu = main.GpuProfile()
        self.assertEqual(gpu.kv_capacity_tokens, 396728)
        self.assertAlmostEqual(gpu.prefill_tokens_per_s, 24750.0)
        self.assertAlmostEqual(gpu.cold_start_s, 91.0)
        self.assertAlmostEqual(gpu.decode_step_s(1000) * 1000, 6.88, places=2)
        self.assertGreater(gpu.decode_step_s(64000), gpu.decode_step_s(1000) * 1.4)
        with self.assertRaises(main.InputError):
            main.GpuProfile(params_billion=70).kv_capacity_tokens

    def test_pod_cpu_saturates_below_a_typical_hpa_target(self):
        gpu = main.GpuProfile()
        self.assertLess(gpu.cpu_percent(gpu.max_num_seqs), 50)
        self.assertGreater(gpu.cpu_percent(1), 25)
        trace = main.generate_trace([(0, 30)], 240, seed=1)
        result = main.simulate(trace, main.parse_policy("cpu:60"), gpu, 240)
        self.assertEqual((result.peak_replicas, result.scale_events), (1, 0))
        self.assertGreater(result.miss_rate, 0.5)

    def test_queue_scaling_meets_the_slo_cheaper_than_a_low_cpu_target(self):
        gpu = main.GpuProfile()
        rates = main.parse_rates("0=4,120=10,240=18,480=3")
        trace = main.generate_trace(rates, 600, seed=3)
        cpu_high, cpu_low, queue = (main.simulate(trace, main.parse_policy(spec), gpu, 600) for spec in ("cpu:60", "cpu:25", "queue:24"))
        self.assertLess(queue.miss_rate, 0.05)
        self.assertGreater(cpu_high.miss_rate, 0.5)
        self.assertLess(queue.replica_minutes, 0.75 * cpu_low.replica_minutes)
        self.assertEqual((cpu_high.peak_replicas, cpu_low.peak_replicas), (1, 8))
        self.assertLess(queue.peak_replicas, 8)

    def test_activator_holds_requests_while_the_first_replica_loads(self):
        gpu = main.GpuProfile()
        trace = [(30.0, 500, 20), (31.0, 500, 20), (32.0, 500, 20)]
        policy = main.parse_policy("queue:8,min=0,cooldown=60")
        held = main.simulate(trace, policy, gpu, 200, initial=0, cold_start_s=40)
        self.assertEqual((held.rejected, held.completed, held.peak_replicas), (0, 3, 1))
        self.assertGreater(held.ttft_p50_s, 30)
        self.assertLess(held.ttft_max_s, 60)
        self.assertEqual(held.misses, 3)
        dropped = main.simulate(trace, policy, gpu, 200, initial=0, cold_start_s=40, hold=False)
        self.assertEqual((dropped.rejected, dropped.completed, dropped.peak_replicas), (3, 0, 0))

    def test_traces_are_seeded_and_follow_the_rates(self):
        rates = main.parse_rates("0=10,100=0,200=5")
        trace = main.generate_trace(rates, 300, seed=4)
        self.assertEqual(trace, main.generate_trace(rates, 300, seed=4))
        self.assertTrue(all(not 100 <= t < 200 for t, _, _ in trace))
        self.assertAlmostEqual(sum(1 for t, _, _ in trace if t < 100), 1000, delta=120)
        self.assertAlmostEqual(sum(1 for t, _, _ in trace if t >= 200), 500, delta=80)
        for bad in ["5=1", "0=1,0=2", "0=-1", "0:1"]:
            with self.assertRaises(main.InputError):
                main.parse_rates(bad)

    def test_trace_files_load_as_json_or_jsonl(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "trace.jsonl")
            with open(path, "w", encoding="utf-8") as handle:
                handle.write('{"t": 1.5, "prompt_tokens": 100, "output_tokens": 20}\n\n{"t": 3, "prompt_tokens": 50, "output_tokens": 5}\n')
            self.assertEqual(main.load_trace(path), [(1.5, 100, 20), (3.0, 50, 5)])
            with open(path, "w", encoding="utf-8") as handle:
                handle.write('[{"t": 1}]')
            with self.assertRaises(main.InputError):
                main.load_trace(path)


class FakeOpenAI(http.server.BaseHTTPRequestHandler):
    metrics_text = ""
    seen = []
    lock = threading.Lock()

    def log_message(self, *args):
        pass

    def reply(self, status, body, kind="application/json"):
        data = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path == "/v1/models":
            self.reply(200, json.dumps({"object": "list", "data": [{"id": "cc-tiny", "object": "model"}]}))
        elif self.path == "/metrics":
            self.reply(200, self.metrics_text, "text/plain; version=0.0.4")
        else:
            self.reply(404, "{}")

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        with self.lock:
            self.seen.append(body)
            number = len(self.seen)
        if number % 5 == 0:
            self.reply(429, json.dumps({"error": {"message": "queue full"}}))
            return
        self.reply(200, json.dumps({"id": f"c{number}", "choices": [{"text": "ok"}], "usage": {"prompt_tokens": 7, "completion_tokens": body["max_tokens"]}}))


class HttpTests(unittest.TestCase):
    def setUp(self):
        FakeOpenAI.seen = []
        FakeOpenAI.metrics_text = fixture("sim-loaded.prom")
        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), FakeOpenAI)
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
        self.thread.start()
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()

    def test_load_generator_discovers_the_model_and_counts_statuses(self):
        report = main.run_load(self.url + "/v1/", concurrency=4, total=20, max_tokens=8)
        self.assertEqual((report.model, report.sent, report.ok, report.statuses), ("cc-tiny", 20, 16, {"200": 16, "429": 4}))
        self.assertEqual(report.output_tokens, 16 * 8)
        self.assertEqual({body["model"] for body in FakeOpenAI.seen}, {"cc-tiny"})
        self.assertGreater(report.requests_per_s, 0)

    def test_decide_reads_metrics_over_http_and_keeps_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = os.path.join(tmp, "state.json")
            code, out, err = run_cli("decide", self.url + "/metrics", "--target", "4", "--max", "3", "--state", state, "--now", "100", "--json")
            self.assertEqual(code, 0, err)
            report = json.loads(out)
            self.assertEqual((report["current"], report["desired"], report["demand"]), (1, 3, 12.0))
            FakeOpenAI.metrics_text = fixture("sim-loaded.prom").replace("1384", "1484").replace("process_cpu_seconds_total 1.27", "process_cpu_seconds_total 1.47")
            code, out, _ = run_cli("decide", self.url + "/metrics", "--current", "3", "--target", "4", "--state", state, "--now", "110", "--cpu-request", "1", "--json")
            again = json.loads(out)
            self.assertEqual(again["replicas"][0]["generation_per_s"], 10.0)
            self.assertAlmostEqual(again["replicas"][0]["cpu_percent"], 2.0)
            self.assertEqual(again["desired"], 3)

    def test_unreachable_server_exits_1(self):
        code, _, err = run_cli("decide", self.url + "/nothing-here")
        self.assertEqual(code, 1)
        self.assertIn("HTTP 404", err)
        closed = http.server.ThreadingHTTPServer(("127.0.0.1", 0), FakeOpenAI)
        port = closed.server_address[1]
        closed.server_close()
        code, _, err = run_cli("decide", f"http://127.0.0.1:{port}/metrics")
        self.assertEqual(code, 1)
        self.assertIn("GET", err)


def pod(name, ready=True, cpu="500m", deleting=False):
    item = {"metadata": {"name": name}, "spec": {"containers": [{"name": "sim", "resources": {"requests": {"cpu": cpu}}}]}, "status": {"conditions": [{"type": "Ready", "status": "True" if ready else "False"}]}}
    if deleting:
        item["metadata"]["deletionTimestamp"] = "2026-11-01T10:00:00Z"
    return item


class FakeKubectl:
    def __init__(self, replicas=1, pods=None, metrics=None):
        self.calls = []
        self.replicas = replicas
        self.pods = pods if pods is not None else [pod("cc-sim-a")]
        self.metrics = metrics or {}

    def __call__(self, args):
        self.calls.append(args)
        if args[:2] == ["get", "--raw"]:
            name = args[2].split("/pods/")[1].split(":")[0]
            return self.metrics[name]
        if args[2:5] == ["get", "deployment", "cc-sim"]:
            return json.dumps({"spec": {"replicas": self.replicas, "selector": {"matchLabels": {"app": "cc-sim", "tier": "llm"}}}})
        if args[2:4] == ["get", "pods"]:
            return json.dumps({"items": self.pods})
        if args[2] == "scale":
            return "deployment.apps/cc-sim scaled\n"
        raise AssertionError(args)


class KubeTests(unittest.TestCase):
    def test_ready_pods_are_scraped_through_the_api_server_proxy_and_scaled(self):
        run = FakeKubectl(pods=[pod("cc-sim-a"), pod("cc-sim-b", ready=False), pod("cc-sim-c", deleting=True)], metrics={"cc-sim-a": fixture("sim-loaded.prom")})
        decision, observation, pods, applied = main.kube_step(run, "cc-ns", "cc-sim", queue_policy(4, max_replicas=3), main.ScalerState(), 100.0, apply=True)
        self.assertEqual((pods, [r.name for r in observation.replicas], decision.desired, applied), (3, ["cc-sim-a"], 3, True))
        self.assertIn(["-n", "cc-ns", "get", "pods", "-l", "app=cc-sim,tier=llm", "-o", "json"], run.calls)
        self.assertIn(["get", "--raw", "/api/v1/namespaces/cc-ns/pods/cc-sim-a:8000/proxy/metrics"], run.calls)
        self.assertEqual(run.calls[-1], ["-n", "cc-ns", "scale", "deployment/cc-sim", "--replicas=3"])

    def test_counters_from_the_previous_scrape_give_rates_and_cpu_percent(self):
        state = main.ScalerState(counters={"cc-sim-a": {"t": 90.0, "generation_tokens": 1284.0, "cpu_seconds": 1.22}, "gone": {"t": 1.0}})
        run = FakeKubectl(replicas=3, metrics={"cc-sim-a": fixture("sim-loaded.prom")})
        decision, observation, _, applied = main.kube_step(run, "cc-ns", "cc-sim", queue_policy(4), state, 100.0)
        self.assertEqual(observation.replicas[0].generation_per_s, 10.0)
        self.assertAlmostEqual(observation.replicas[0].cpu_percent, 1.0)
        self.assertEqual(set(state.counters), {"cc-sim-a"})
        self.assertEqual((decision.desired, applied), (3, False))
        self.assertFalse(any(call[2:3] == ["scale"] for call in run.calls))


class CliTests(unittest.TestCase):
    def test_parse_prints_one_row_per_source(self):
        code, out, _ = run_cli("parse", os.path.join(FIXTURES, "vllm-busy.prom"), os.path.join(FIXTURES, "llamacpp.prom"), "--json")
        self.assertEqual(code, 0)
        self.assertEqual([(r["server"], r["running"], r["waiting"]) for r in json.loads(out)], [("vllm", 7.0, 12.0), ("llama.cpp", 4.0, 3.0)])

    def test_exit_codes(self):
        self.assertEqual(run_cli("decide", os.path.join(FIXTURES, "nope.prom"))[0], 2)
        code, _, err = run_cli("decide", "--metric", "cpu", "--min", "0", "--target", "60")
        self.assertEqual(code, 2)
        self.assertIn("cannot scale from zero", err)
        self.assertEqual(run_cli("simulate", "--rates", "1=2")[0], 2)
        self.assertEqual(run_cli("load", "--url", "http://127.0.0.1:9")[0], 2)

    def test_simulate_and_gpu_json(self):
        code, out, _ = run_cli("simulate", "--rates", "0=5,60=0", "--duration", "120", "--policy", "queue:24", "--policy", "cpu:60", "--json")
        self.assertEqual(code, 0)
        report = json.loads(out)
        self.assertEqual([r["policy"] for r in report["results"]], ["queue:24", "cpu:60"])
        self.assertTrue(all("timeline" not in r for r in report["results"]))
        code, out, _ = run_cli("gpu", "--max-num-seqs", "128", "--json")
        self.assertEqual((code, json.loads(out)["max_num_seqs"], json.loads(out)["kv_capacity_tokens"]), (0, 128, 396728))


if __name__ == "__main__":
    unittest.main()
