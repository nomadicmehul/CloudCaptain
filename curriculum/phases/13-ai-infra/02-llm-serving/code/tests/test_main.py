import contextlib
import io
import json
import os
import sys
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import main  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
FIXTURES = os.path.join(HERE, "..", "fixtures")
H100 = main.HARDWARE["h100-sxm-80gb"]
L4 = main.HARDWARE["l4-24gb"]
CPU = main.HARDWARE["cpu-4vcpu"]
LLAMA8B = main.MODELS["llama-3.1-8b"]
SMOL = main.MODELS["smollm2-135m-q8_0"]


def engine(hardware=H100, model=LLAMA8B, blocks=None, **config):
    num_blocks = blocks or main.plan_capacity(hardware, model).num_blocks
    return main.Engine(hardware, model, main.EngineConfig(**config), num_blocks)


def run(cli):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = main.main(cli)
    return code, out.getvalue(), err.getvalue()


def closed_loop(concurrency, requests=16, prompt=40, output=64):
    sim = engine(CPU, SMOL, blocks=4096 // 16, max_num_seqs=4)
    work = [main.Request(f"r{i}", 0.0, prompt, output) for i in range(requests)]
    main.simulate(sim, work, concurrency)
    return main.summarize(main.records_from_requests(work)), sim


class MemoryTests(unittest.TestCase):
    def test_kv_bytes_per_token_is_two_tensors_per_layer_per_kv_head(self):
        self.assertEqual(LLAMA8B.kv_bytes_per_token, 2 * 32 * 8 * 128 * 2)
        self.assertEqual(LLAMA8B.kv_bytes_per_token, 131072)
        self.assertEqual(SMOL.kv_bytes_per_token, 23040)
        self.assertAlmostEqual(LLAMA8B.weight_bytes / 1e9, 16.06, places=2)
        self.assertAlmostEqual(SMOL.bytes_per_param, 34 / 32)

    def test_capacity_is_memory_times_utilisation_minus_weights_and_reserve_in_blocks(self):
        plan = main.plan_capacity(H100, LLAMA8B, 0.9, 16, max_model_len=4096)
        kv_bytes = 79.6 * main.GIB * 0.9 - LLAMA8B.weight_bytes - 2.0 * main.GIB
        self.assertEqual(plan.num_blocks, int(kv_bytes // (16 * 131072)))
        self.assertEqual(plan.kv_tokens, plan.num_blocks * 16)
        self.assertAlmostEqual(plan.max_concurrency, plan.kv_tokens / 4096)
        lower = main.plan_capacity(H100, LLAMA8B, 0.5, 16)
        self.assertLess(lower.num_blocks, plan.num_blocks)
        fixed = main.plan_capacity(CPU, SMOL, kv_tokens=4096)
        self.assertEqual((fixed.num_blocks, fixed.fixed), (256, True))

    def test_a_model_that_does_not_fit_is_a_capacity_error_and_bad_flags_are_input_errors(self):
        with self.assertRaises(main.CapacityError) as caught:
            main.plan_capacity(H100, main.MODELS["llama-3.1-70b"])
        self.assertIn("does not fit", str(caught.exception))
        with self.assertRaises(main.InputError):
            main.plan_capacity(H100, LLAMA8B, gpu_memory_utilization=1.5)
        with self.assertRaises(main.InputError):
            main.plan_capacity(CPU, SMOL, kv_tokens=8)


class StepCostTests(unittest.TestCase):
    def test_decode_is_memory_bound_so_a_bigger_batch_costs_little_until_compute_catches_up(self):
        one = main.step_seconds(H100, LLAMA8B, 1, 512)
        sixteen = main.step_seconds(H100, LLAMA8B, 16, 16 * 512)
        self.assertLess(sixteen / one, 1.1)
        self.assertGreater(16 / sixteen, 14 / one)
        knee = main.critical_batch(H100, LLAMA8B)
        self.assertAlmostEqual(knee, (989e12 * 0.5) / (3350e9 * 0.75) * 2 / 2, delta=1)
        far = main.step_seconds(H100, LLAMA8B, 2048, 2048)
        self.assertGreater(far, 2048 * 2 * LLAMA8B.params / (989e12 * 0.5))

    def test_kv_reads_make_long_contexts_slower_per_token(self):
        short = main.step_seconds(L4, LLAMA8B, 32, 32 * 256)
        long = main.step_seconds(L4, LLAMA8B, 32, 32 * 4096)
        self.assertGreater(long, short * 1.5)

    def test_on_the_cpu_preset_batch_four_is_compute_bound_and_slower_per_step(self):
        self.assertLess(main.critical_batch(CPU, SMOL), 4)
        self.assertGreater(main.step_seconds(CPU, SMOL, 4, 400), main.step_seconds(CPU, SMOL, 1, 100))


class BlockPoolTests(unittest.TestCase):
    def test_allocate_release_and_lru_eviction_of_cached_blocks(self):
        pool = main.BlockPool(4)
        first = pool.allocate(2)
        pool.register(first[0], ("sys", 0))
        pool.register(first[1], ("sys", 1))
        self.assertEqual((pool.used(), pool.free_count()), (2, 2))
        pool.release(first)
        self.assertEqual(pool.free_count(), 4)
        self.assertEqual(pool.lookup([("sys", 0), ("sys", 1), ("sys", 2)]), first)
        pool.allocate(2)
        self.assertEqual(pool.evictions, 0)
        pool.allocate(1)
        self.assertEqual(pool.evictions, 1)
        self.assertEqual(pool.lookup([("sys", 0), ("sys", 1)]), [first[0]])
        with self.assertRaises(main.CapacityError):
            pool.allocate(2)

    def test_shared_blocks_are_reference_counted(self):
        pool = main.BlockPool(3)
        blocks = pool.allocate(1)
        pool.register(blocks[0], ("sys", 0))
        pool.touch(blocks)
        pool.release(blocks)
        self.assertEqual(pool.used(), 1)
        pool.release(blocks)
        self.assertEqual(pool.used(), 0)


class SchedulerTests(unittest.TestCase):
    def test_prefill_is_chunked_by_the_token_budget_and_decode_keeps_running(self):
        sim = engine(max_num_batched_tokens=1024)
        talker = main.Request("talker", 0.0, 100, 50)
        sim.add(talker)
        sim.step()
        sim.step()
        reader = main.Request("reader", sim.now, 3000, 4)
        sim.add(reader)
        chunks = []
        while reader.generated == 0:
            before = talker.generated
            sim.step()
            chunks.append(sim.steps[-1].tokens)
            self.assertEqual(talker.generated, before + 1)
        self.assertEqual(chunks, [1024, 1024, 955])
        self.assertEqual(sum(chunks) - len(chunks), 3000)
        self.assertTrue(all(step.tokens <= 1024 for step in sim.steps))

    def test_without_chunked_prefill_a_long_prompt_waits_for_a_whole_budget_or_is_rejected(self):
        sim = engine(max_num_batched_tokens=2048, chunked_prefill=False)
        self.assertFalse(sim.add(main.Request("huge", 0.0, 4000, 10)))
        self.assertIn("chunked prefill off", sim.rejected[0].rejected)
        sim.add(main.Request("a", 0.0, 1500, 4))
        sim.add(main.Request("b", 0.0, 1500, 4))
        sim.step()
        self.assertEqual(sim.steps[0].tokens, 1500)
        self.assertEqual(len(sim.running), 1)

    def test_max_num_seqs_caps_the_batch_and_the_rest_queue(self):
        report, sim = closed_loop(8, requests=8)
        self.assertLessEqual(max(step.seqs for step in sim.steps), 4)
        self.assertGreater(report["queue_ms"]["p95"], 0)
        self.assertEqual(report["completed"], 8)

    def test_when_blocks_run_out_the_newest_request_is_preempted_and_recomputed(self):
        sim = engine(L4, LLAMA8B, blocks=20, block_size=16)
        old = main.Request("old", 0.0, 120, 100)
        new = main.Request("new", 0.0, 120, 100)
        main.simulate(sim, [old, new])
        self.assertEqual(old.preemptions, 0)
        self.assertGreaterEqual(new.preemptions, 1)
        self.assertEqual(sim.preemptions, new.preemptions)
        self.assertEqual((old.generated, new.generated), (100, 100))
        gaps = [b - a for a, b in zip(new.token_times, new.token_times[1:])]
        self.assertGreater(max(gaps), 5 * sorted(gaps)[len(gaps) // 2])
        self.assertEqual(sim.pool.used(), 0)

    def test_prefix_caching_reuses_full_blocks_and_always_computes_one_token(self):
        sim = engine(prefix_caching=True)
        first = main.Request("a", 0.0, 1100, 4, "sys", 1024)
        main.simulate(sim, [first])
        second = main.Request("b", sim.now, 1100, 4, "sys", 1024)
        whole = main.Request("c", sim.now, 1024, 4, "sys", 1024)
        main.simulate(sim, [second, whole])
        self.assertEqual(first.cached_tokens, 0)
        self.assertEqual(second.cached_tokens, 1024)
        self.assertEqual(whole.cached_tokens, 1008)
        self.assertLess(second.token_times[0] - second.arrival, first.token_times[0] - first.arrival)

    def test_requests_that_can_never_fit_are_rejected_with_a_reason(self):
        sim = engine(blocks=10, max_model_len=2048)
        self.assertFalse(sim.add(main.Request("long", 0.0, 2000, 100)))
        self.assertIn("max-model-len 2048", sim.rejected[0].rejected)
        self.assertFalse(sim.add(main.Request("wide", 0.0, 150, 20)))
        self.assertIn("needs 11 KV blocks, the cache has 10", sim.rejected[1].rejected)

    def test_the_batching_trade_off_more_throughput_worse_tpot(self):
        one, _ = closed_loop(1)
        four, _ = closed_loop(4)
        eight, _ = closed_loop(8)
        self.assertGreater(four["throughput"]["output_tokens_per_s"], 1.5 * one["throughput"]["output_tokens_per_s"])
        self.assertGreater(four["tpot_ms"]["p50"], one["tpot_ms"]["p50"])
        self.assertAlmostEqual(eight["throughput"]["output_tokens_per_s"], four["throughput"]["output_tokens_per_s"], delta=1)
        self.assertGreater(eight["ttft_ms"]["p50"], four["ttft_ms"]["p50"])


class MetricsTests(unittest.TestCase):
    def test_ttft_tpot_itl_and_throughput_follow_their_definitions(self):
        record = main.RequestRecord("r1", 10.0, [10.5, 10.6, 10.8, 11.0], 11.05, 20, 4, 10.2, True)
        report = main.summarize([record])
        self.assertAlmostEqual(report["ttft_ms"]["p50"], 500)
        self.assertAlmostEqual(report["tpot_ms"]["p50"], 500 / 3)
        self.assertAlmostEqual(report["itl_ms"]["max"], 200)
        self.assertAlmostEqual(report["queue_ms"]["p50"], 200)
        self.assertAlmostEqual(report["e2e_ms"]["p50"], 1050)
        self.assertAlmostEqual(report["throughput"]["output_tokens_per_s"], 4 / 1.05)

    def test_percentiles_are_nearest_rank_and_errors_are_listed_not_averaged(self):
        values = [float(v) for v in range(1, 21)]
        self.assertEqual((main.percentile(values, 50), main.percentile(values, 95), main.percentile(values, 100)), (10.0, 19.0, 20.0))
        self.assertEqual(main.percentile([], 95), 0.0)
        broken = main.RequestRecord("r2", 0.0, error="HTTP 400: context too long")
        report = main.summarize([main.RequestRecord("r1", 0.0, [0.1, 0.2], 0.2, 5, 2), broken])
        self.assertEqual(report["completed"], 1)
        self.assertEqual(report["errors"], [{"id": "r2", "error": "HTTP 400: context too long"}])

    def test_workloads_are_seeded_and_traces_report_the_bad_line(self):
        a = main.synthetic_workload(5, (100, 200), (10, 20), rate=2.0, seed=3, prefixes=2, prefix_tokens=64)
        b = main.synthetic_workload(5, (100, 200), (10, 20), rate=2.0, seed=3, prefixes=2, prefix_tokens=64)
        self.assertEqual([(r.arrival, r.prompt_tokens, r.output_tokens, r.prefix) for r in a], [(r.arrival, r.prompt_tokens, r.output_tokens, r.prefix) for r in b])
        self.assertEqual({r.prefix for r in a}, {"system-0", "system-1"})
        self.assertEqual(main.parse_range("256-1024", "--x"), (256, 1024))
        with self.assertRaises(main.InputError):
            main.parse_range("1024-256", "--x")
        with self.assertRaises(main.InputError) as caught:
            main.load_trace('{"arrival": 0, "prompt_tokens": 5, "output_tokens": 2}\n{"arrival": 1, "prompt_tokens": "x"}\n')
        self.assertIn("trace line 2", str(caught.exception))
        with open(os.path.join(FIXTURES, "burst.jsonl"), encoding="utf-8") as handle:
            trace = main.load_trace(handle.read())
        self.assertEqual(len(trace), 48)
        self.assertTrue(all(r.prefix_tokens == 1024 for r in trace))

    def test_prefix_caching_on_the_burst_trace_removes_preemptions(self):
        with open(os.path.join(FIXTURES, "burst.jsonl"), encoding="utf-8") as handle:
            text = handle.read()
        results = {}
        for caching in (False, True):
            sim = engine(L4, LLAMA8B, prefix_caching=caching)
            work = main.load_trace(text)
            main.simulate(sim, work)
            results[caching] = (sim.preemptions, main.summarize(main.records_from_requests(work)))
        self.assertGreater(results[False][0], 0)
        self.assertEqual(results[True][0], 0)
        self.assertLess(results[True][1]["ttft_ms"]["p50"], results[False][1]["ttft_ms"]["p50"])


class StreamParsingTests(unittest.TestCase):
    def test_sse_chunks_give_token_times_and_usage(self):
        lines = [
            (1.00, 'data: {"choices":[{"delta":{"role":"assistant","content":"A"},"index":0}]}\n'),
            (1.01, "\n"),
            (1.10, 'data: {"choices":[{"delta":{"content":" KV"},"index":0}]}\n'),
            (1.20, 'data: {"choices":[{"delta":{},"finish_reason":"length","index":0}]}\n'),
            (1.21, 'data: {"choices":[],"usage":{"prompt_tokens":31,"completion_tokens":3},"timings":{"predicted_n":3}}\n'),
            (1.22, "data: [DONE]\n"),
        ]
        record = main.record_from_stream("r1", 0.5, lines)
        self.assertEqual(record.token_times, [1.00, 1.10])
        self.assertEqual((record.prompt_tokens, record.output_tokens, record.usage_reported), (31, 3, True))
        self.assertEqual(record.finished, 1.22)
        self.assertEqual(record.error, "")

    def test_without_usage_tokens_are_counted_and_errors_are_kept(self):
        record = main.record_from_stream("r1", 0.0, [(0.1, 'data: {"choices":[{"delta":{"content":"x"}}]}'), (0.2, 'data: {"choices":[{"delta":{"content":"y"},"finish_reason":"stop"}]}')])
        self.assertEqual((record.output_tokens, record.usage_reported, record.error), (2, False, ""))
        cut = main.record_from_stream("r3", 0.0, [(0.1, 'data: {"choices":[{"delta":{"content":"x"}}]}')])
        self.assertIn("ended before", cut.error)
        failed = main.record_from_stream("r2", 0.0, [(0.1, 'data: {"error":{"code":400,"message":"the request exceeds the available context size"}}')])
        self.assertIn("exceeds the available context size", failed.error)

    def test_prometheus_text_is_parsed_and_the_llama_cpp_batch_is_recovered(self):
        text = "# HELP llamacpp:n_decode_total x\n# TYPE llamacpp:n_decode_total counter\nllamacpp:n_decode_total 100\nllamacpp:n_busy_slots_per_decode 1\nllamacpp:requests_deferred 0\nvllm:num_requests_running{model_name=\"a\",engine=\"0\"} 3\nvllm:num_requests_running{model_name=\"a\",engine=\"1\"} 2\n"
        values = main.parse_prometheus(text)
        self.assertEqual(values["llamacpp:n_decode_total"], 100)
        self.assertEqual(values["vllm:num_requests_running"], 5)
        after = {"llamacpp:n_decode_total": 300.0, "llamacpp:n_busy_slots_per_decode": 3.0, "llamacpp:tokens_predicted_total": 50.0, "llamacpp:requests_deferred": 0.0}
        before = dict(values, **{"llamacpp:tokens_predicted_total": 10.0})
        window = main.server_window(before, after, [{"llamacpp:requests_deferred": 4.0}])
        self.assertEqual(window["implementation"], "llama.cpp")
        self.assertEqual((window["decode_calls"], window["generated_tokens"], window["max_waiting"]), (200.0, 40.0, 4.0))
        self.assertAlmostEqual(window["mean_batch_per_decode"], (900 - 100) / 200)
        self.assertIsNone(main.server_window(None, after))


class BacklogServer(ThreadingHTTPServer):
    request_queue_size = 64


class FakeServer:
    def __init__(self, first_token_delay=0.05, token_delay=0.01):
        self.lock = threading.Lock()
        self.in_flight = 0
        self.max_in_flight = 0
        self.decodes = 0
        self.bodies = []
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                if self.path == "/v1/models":
                    payload = json.dumps({"object": "list", "data": [{"id": "fake-model"}]}).encode()
                    content_type = "application/json"
                elif self.path == "/metrics":
                    with owner.lock:
                        payload = f"llamacpp:n_decode_total {owner.decodes}\nllamacpp:n_busy_slots_per_decode 1\nllamacpp:requests_processing {owner.in_flight}\n".encode()
                    content_type = "text/plain; version=0.0.4"
                else:
                    self.send_error(404)
                    return
                self.send_response(200)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                if body["max_tokens"] > 1000:
                    payload = b'{"error":{"code":400,"message":"the request exceeds the available context size"}}'
                    self.send_response(400)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(payload)))
                    self.end_headers()
                    self.wfile.write(payload)
                    return
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                if body["messages"][0]["content"] == "truncate":
                    self.wfile.write(b'data: {"choices":[{"index":0,"delta":{"content":"half"}}]}\n\n')
                    return
                with owner.lock:
                    owner.bodies.append(body)
                    owner.in_flight += 1
                    owner.max_in_flight = max(owner.max_in_flight, owner.in_flight)
                time.sleep(first_token_delay)
                for index in range(body["max_tokens"]):
                    if index:
                        time.sleep(token_delay)
                    with owner.lock:
                        owner.decodes += 1
                    delta = {"content": f"t{index}"}
                    self.wfile.write(f"data: {json.dumps({'choices': [{'index': 0, 'delta': delta, 'finish_reason': None}]})}\n\n".encode())
                    self.wfile.flush()
                usage = {"choices": [], "usage": {"prompt_tokens": 12, "completion_tokens": body["max_tokens"]}}
                self.wfile.write(f"data: {json.dumps(usage)}\n\ndata: [DONE]\n\n".encode())
                with owner.lock:
                    owner.in_flight -= 1

        self.server = BacklogServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.server.shutdown()
        self.server.server_close()


class LoadgenTests(unittest.TestCase):
    def test_loadgen_streams_concurrently_and_measures_ttft_tpot_and_server_metrics(self):
        with FakeServer(first_token_delay=0.05, token_delay=0.01) as fake:
            report = main.run_load(fake.url, [f"p{i}" for i in range(6)], concurrency=3, max_tokens=5, ignore_eos=True, sample_interval=0.02)
        self.assertEqual((report["completed"], report["errors"], report["model"]), (6, [], "fake-model"))
        self.assertEqual(fake.max_in_flight, 3)
        self.assertTrue(all(body["stream"] and body["stream_options"] == {"include_usage": True} and body["ignore_eos"] for body in fake.bodies))
        self.assertEqual(report["output_tokens"], 30)
        self.assertGreaterEqual(report["ttft_ms"]["p50"], 45)
        self.assertGreaterEqual(report["tpot_ms"]["p50"], 8)
        self.assertLess(report["tpot_ms"]["p50"], report["ttft_ms"]["p50"])
        self.assertEqual(report["server"]["decode_calls"], 30)
        self.assertGreaterEqual(report["server"]["max_running"], 1)

    def test_http_errors_are_recorded_and_an_unreachable_server_exits_one_with_a_hint(self):
        with FakeServer() as fake:
            code, out, _ = run(["loadgen", "--url", fake.url, "--requests", "2", "--max-tokens", "5000", "--json"])
        self.assertEqual(code, 1)
        report = json.loads(out)
        self.assertEqual(report["completed"], 0)
        self.assertIn("HTTP 400", report["errors"][0]["error"])
        with FakeServer() as fake:
            report = main.run_load(fake.url, ["truncate", "fine"], concurrency=2, max_tokens=3)
        self.assertEqual(report["completed"], 1)
        self.assertIn("ended before", report["errors"][0]["error"])
        code, _, err = run(["loadgen", "--url", "http://127.0.0.1:9", "--requests", "1"])
        self.assertEqual(code, 1)
        self.assertIn("docker run", err)
        self.assertIn(main.LLAMA_CPP_IMAGE, err)


class CliTests(unittest.TestCase):
    def test_simulate_and_sweep_json_carry_the_same_keys_as_loadgen(self):
        code, out, _ = run(["simulate", "--hardware", "cpu-4vcpu", "--model", "smollm2-135m-q8_0", "--kv-tokens", "4096", "--max-num-seqs", "4", "--prompt-tokens", "40", "--output-tokens", "64", "--requests", "8", "--concurrency", "4", "--json"])
        self.assertEqual(code, 0)
        report = json.loads(out)
        for key in ("ttft_ms", "tpot_ms", "itl_ms", "queue_ms", "e2e_ms", "throughput", "completed"):
            self.assertIn(key, report)
        self.assertEqual(report["engine"]["kv_blocks"], 256)
        code, out, _ = run(["sweep", "--concurrency", "1,4", "--requests", "8", "--json"])
        rows = json.loads(out)
        self.assertEqual([row["concurrency"] for row in rows], [1, 4])
        code, out, _ = run(["capacity"])
        self.assertIn("447,952 tokens", out)

    def test_bad_input_exits_two_and_capacity_failures_exit_one(self):
        self.assertEqual(run(["simulate", "--prompt-tokens", "lots"])[0], 2)
        self.assertEqual(run(["sweep", "--concurrency", "1,x"])[0], 2)
        self.assertEqual(run(["simulate", "--trace", "/nonexistent.jsonl"])[0], 2)
        self.assertEqual(run(["nonsense"])[0], 2)
        code, _, err = run(["capacity", "--model", "llama-3.1-70b"])
        self.assertEqual(code, 1)
        self.assertIn("tensor parallelism", err)


if __name__ == "__main__":
    unittest.main()
