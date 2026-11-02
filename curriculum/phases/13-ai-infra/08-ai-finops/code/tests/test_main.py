import contextlib
import csv
import io
import json
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest
from datetime import date, datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import main  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
FIXTURES = os.path.join(HERE, "..", "fixtures")
TODAY = date(2026, 11, 1)
GPU_OPTIONS = ["l4-llama8b-f16.json", "l4-llama8b-q8_0.json", "l4-llama8b-q4_k.json", "h100-llama8b-f16.json"]


def fixture(name):
    with open(os.path.join(FIXTURES, name), encoding="utf-8") as handle:
        return json.load(handle)


def book():
    return main.load_prices(fixture("prices.json"), TODAY)


def scenario(name="scenario.json", **changes):
    data = fixture(name)
    data.update(changes)
    return main.scenario_from(data)


def option(name):
    return main.option_from(fixture(name), name)


def flat(rps=1.0, **changes):
    data = {"name": "flat", "hourly_requests_per_second": [rps] * 24, "request": {"input_tokens": 600, "output_tokens": 200}, "slo": {"min_stream_tokens_per_second": 15}, "autoscaling": {"target_utilization": 0.7, "min_replicas": 1}}
    data.update(changes)
    return main.scenario_from(data)


def run_cli(*args):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = main.main(list(args))
    return code, out.getvalue(), err.getvalue()


def summarize(report):
    options = {}
    for row in report["self_hosted"]:
        options[row["option"]] = {"chosen": row["chosen"], "points": {str(p["concurrency"]): {
            "meets_slo": p["meets_slo"],
            "shortfall": bool(p["shortfall_hours"]),
            "requests_per_second_per_replica": p["requests_per_second_per_replica"],
            "replica_hours_per_day": p["replica_hours_per_day"],
            "utilization": p["utilization"],
            "monthly": p["monthly"],
            "per_mtok_input": p["per_mtok_input"],
            "per_mtok_output": p["per_mtok_output"],
            "break_even": {b["api"]: b["scale"] for b in p["break_even"]},
        } for p in row["points"]}}
    return {"requests_per_month": report["requests_per_month"], "cheapest": report["cheapest"], "apis": {row["api"]: row["monthly"] for row in report["apis"]}, "options": options}


def assert_close(test, expected, actual, path="report"):
    if isinstance(expected, dict):
        test.assertEqual(sorted(expected), sorted(actual), path)
        for key in expected:
            assert_close(test, expected[key], actual[key], f"{path}.{key}")
    elif isinstance(expected, float) or isinstance(actual, float):
        test.assertAlmostEqual(expected, actual, delta=max(1e-6, abs(expected) * 1e-3), msg=path)
    else:
        test.assertEqual(expected, actual, path)


class PriceTests(unittest.TestCase):
    def test_every_price_needs_a_date_and_a_source(self):
        good = {"name": "gpu", "hourly_price": 1.0, "as_of": "2026-10-01", "source": "quote 42"}
        self.assertEqual(main.load_prices({"instances": [good]}, TODAY).instances["gpu"].source, "quote 42")
        for broken, word in ((dict(good, source=""), "source"), ({k: v for k, v in good.items() if k != "as_of"}, "as_of"), (dict(good, as_of="01/10/2026"), "YYYY-MM-DD"), (dict(good, as_of="2026-12-01"), "after today")):
            with self.assertRaises(main.InputError) as caught:
                main.load_prices({"instances": [broken]}, TODAY)
            self.assertIn(word, str(caught.exception))
        with self.assertRaises(main.InputError):
            main.load_prices({"instances": [good, good]}, TODAY)
        with self.assertRaises(main.InputError):
            main.load_prices({"instances": [dict(good, hourly_price=True)]}, TODAY)
        with self.assertRaises(main.InputError):
            main.load_prices({}, TODAY)

    def test_an_old_price_is_a_warning_with_its_source_not_an_error(self):
        prices = main.load_prices(fixture("prices.json"), date(2027, 6, 1))
        self.assertEqual(len(prices.warnings), 6)
        self.assertIn("216 days old", prices.warnings[0])
        self.assertIn("price list", prices.warnings[0])
        code, out, err = run_cli("prices", os.path.join(FIXTURES, "prices.json"), "--today", "2027-06-01")
        self.assertEqual(code, 0)
        self.assertIn("gpu-l4-1x", out)
        self.assertIn("WARNING", err)
        self.assertEqual(book().warnings, [])


class EstimateTests(unittest.TestCase):
    def test_kv_cache_per_token_comes_from_the_architecture(self):
        self.assertEqual(main.kv_bytes_per_token(32, 8, 128), 131072)
        self.assertEqual(main.kv_bytes_per_token(30, 3, 64), 23040)
        self.assertEqual(main.kv_bytes_per_token(32, 8, 128, dtype_bytes=1), 65536)

    def test_quantization_sets_the_weight_bytes_and_how_many_sequences_fit(self):
        self.assertEqual(main.weight_bytes(8, "F16"), 16e9)
        self.assertEqual(main.weight_bytes(8, "Q8_0"), 8.5e9)
        self.assertEqual(main.weight_bytes(8, "q4_k"), 4.5e9)
        l4 = book().instances["gpu-l4-1x"]
        kv = main.kv_bytes_per_token(32, 8, 128)
        self.assertEqual([main.max_batch(l4, 8, q, kv, 4096) for q in ("F16", "Q8_0", "Q4_K")], [10, 24, 31])
        self.assertEqual(main.max_batch(l4, 70, "F16", kv, 4096), 0)
        with self.assertRaises(main.InputError):
            main.weight_bytes(8, "Q3_XYZ")

    def test_decode_is_bandwidth_bound_so_batching_buys_throughput_and_costs_speed(self):
        l4 = book().instances["gpu-l4-1x"]
        kv = main.kv_bytes_per_token(32, 8, 128)
        points = [main.estimate_point(l4, 8, "Q4_K", kv, 600, 200, b) for b in (1, 4, 16, 31)]
        self.assertEqual([p.requests_per_second for p in points], sorted(p.requests_per_second for p in points))
        self.assertEqual([p.stream_tokens_per_second for p in points], sorted((p.stream_tokens_per_second for p in points), reverse=True))
        step = (4.5e9 + 700 * kv) / (300e9 * 0.6)
        self.assertAlmostEqual(points[0].stream_tokens_per_second, 200 / (600 / points[0].prefill_tokens_per_second + 200 * step), places=1)
        self.assertAlmostEqual(points[0].prefill_tokens_per_second, 121e12 * 0.6 / 16e9, places=0)
        f16 = main.estimate_point(l4, 8, "F16", kv, 600, 200, 1)
        self.assertLess(f16.stream_tokens_per_second, points[0].stream_tokens_per_second / 3)

    def test_estimate_cli_regenerates_the_fixture(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp)
        out = os.path.join(tmp, "q4.json")
        code, _, err = run_cli("estimate", "--prices", os.path.join(FIXTURES, "prices.json"), "--today", "2026-11-01", "--instance", "gpu-l4-1x", "--model", "llama-8b", "--params-b", "8", "--quantization", "Q4_K", "--layers", "32", "--kv-heads", "8", "--head-dim", "128", "--input-tokens", "600", "--output-tokens", "200", "--batches", "1,4,8,16,24,31,64", "-o", out)
        self.assertEqual(code, 0, err)
        with open(out, encoding="utf-8") as handle:
            regenerated = json.load(handle)
        self.assertEqual(regenerated["points"], fixture("l4-llama8b-q4_k.json")["points"])
        self.assertEqual(regenerated["max_batch"], 31)
        code, _, err = run_cli("estimate", "--prices", os.path.join(FIXTURES, "prices.json"), "--today", "2026-11-01", "--instance", "gpu-l4-1x", "--model", "llama-70b", "--params-b", "70", "--kv-bytes-per-token", "327680", "--input-tokens", "600", "--output-tokens", "200")
        self.assertEqual(code, 2)
        self.assertIn("leaves no room", err)


class CapacityTests(unittest.TestCase):
    def test_capacity_rescales_a_point_to_another_request_shape(self):
        point = main.Point(8, 2.0, 600, 200, 30.0, 6000.0)
        self.assertAlmostEqual(main.capacity(point, 600, 200)[0], 2.0)
        rps, share = main.capacity(point, 600, 200)
        self.assertAlmostEqual(share, (600 / 6000) / 0.5)
        longer, _ = main.capacity(point, 600, 400)
        self.assertAlmostEqual(1 / longer, 0.1 + 400 * 0.4 / 200)
        no_prefill = main.Point(8, 2.0, 600, 200, 30.0, None)
        self.assertEqual(main.capacity(no_prefill, 6000, 200), (2.0, 0.0))
        with self.assertRaises(main.InputError):
            main.capacity(main.Point(1, 10.0, 600, 200, 30.0, 1000.0), 600, 200)

    def test_replicas_follow_the_curve_between_a_floor_and_a_ceiling(self):
        plan, short = main.replicas_for([0, 0.1, 1, 3], 1.0, 0.5, 1, None)
        self.assertEqual((plan, short), ([1, 1, 2, 6], []))
        plan, short = main.replicas_for([0, 0.1, 1, 3], 1.0, 0.5, 0, 4)
        self.assertEqual((plan, short), ([0, 1, 2, 4], [3]))


class CostTests(unittest.TestCase):
    def test_cost_per_token_is_price_over_capacity_times_utilisation(self):
        l4 = book().instances["gpu-l4-1x"]
        point = option("l4-llama8b-q4_k.json").points[3]
        cost = main.self_hosted(l4, point, flat(2.0))
        rps, share = main.capacity(point, 600, 200)
        self.assertEqual(cost["replicas"], [2] * 24)
        self.assertAlmostEqual(cost["utilization"], 2.0 / (2 * rps))
        output_tokens_per_hour = rps * 200 * 3600 * cost["utilization"]
        self.assertAlmostEqual(cost["per_mtok_output"], l4.hourly_price * (1 - share) / output_tokens_per_hour * 1e6)
        self.assertAlmostEqual(cost["monthly"], 2 * 24 * 0.80 * 30)
        busier = main.self_hosted(l4, point, flat(2.0 * 1.8))
        self.assertGreater(busier["utilization"], cost["utilization"])
        self.assertLess(busier["per_mtok_blended"], cost["per_mtok_blended"])

    def test_doubling_traffic_never_raises_the_cost_per_token(self):
        report = main.evaluate(book(), [option(name) for name in GPU_OPTIONS], scenario())
        for row in report["self_hosted"]:
            for point in row["points"]:
                costs = [s["per_mtok_blended"] for s in point["sweep"]]
                utils = [s["utilization"] for s in point["sweep"]]
                self.assertEqual(costs, sorted(costs, reverse=True), (row["option"], point["concurrency"]))
                self.assertEqual(utils, sorted(utils), (row["option"], point["concurrency"]))
                self.assertLess(costs[-1], costs[0])

    def test_api_cost_is_linear_and_uses_the_cached_input_price(self):
        api = book().apis["api-small"]
        plain = main.api_cost(api, flat(1.0))
        self.assertAlmostEqual(plain["per_1k_requests"], (600 * 0.15 + 200 * 0.60) / 1000)
        self.assertAlmostEqual(main.api_cost(api, flat(1.0), scale=3)["monthly"], 3 * plain["monthly"])
        cached = main.api_cost(api, flat(1.0, cached_input_fraction=0.5))
        self.assertAlmostEqual(cached["per_1k_requests"], (300 * 0.15 + 300 * 0.075 + 200 * 0.60) / 1000)

    def test_break_even_is_where_the_two_monthly_bills_meet(self):
        prices = book()
        l4, api = prices.instances["gpu-l4-1x"], prices.apis["api-small"]
        point = option("l4-llama8b-q4_k.json").points[-1]
        plan = scenario()
        result = main.break_even(l4, point, plan, api)
        scale = result["scale"]
        self.assertAlmostEqual(scale, 0.7082, places=3)
        self.assertLessEqual(main.self_hosted(l4, point, plan, scale, bounded=False)["monthly"], main.api_cost(api, plan, scale)["monthly"])
        cheaper = lambda s: main.self_hosted(l4, point, plan, s, bounded=False)["monthly"] <= main.api_cost(api, plan, s)["monthly"]
        self.assertTrue(all(cheaper(scale * (1 + i / 500)) for i in range(1500)))
        self.assertFalse(cheaper(scale * 0.98))
        self.assertEqual(result["requests_per_day"], round(scale * sum(plan.hourly_rps) * 3600))

    def test_break_even_is_where_self_hosting_stays_cheaper_not_where_it_first_wins(self):
        prices = book()
        l4, api = prices.instances["gpu-l4-1x"], prices.apis["api-large"]
        point = [p for p in option("l4-llama8b-q8_0.json").points if p.concurrency == 1][0]
        plan = scenario()
        cheaper = lambda s: main.self_hosted(l4, point, plan, s, bounded=False)["monthly"] <= main.api_cost(api, plan, s)["monthly"]
        self.assertTrue(cheaper(0.0372))
        self.assertFalse(cheaper(0.03806))
        scale = main.break_even(l4, point, plan, api)["scale"]
        self.assertAlmostEqual(scale, 0.0381, places=4)
        self.assertTrue(all(cheaper(scale + i * 1e-6) for i in range(20000)))

    def test_break_even_moves_with_prices_and_throughput_and_can_be_never(self):
        prices = book()
        l4, api = prices.instances["gpu-l4-1x"], prices.apis["api-small"]
        q4, q8 = option("l4-llama8b-q4_k.json").points[-1], option("l4-llama8b-q8_0.json").points[-1]
        plan = scenario()
        base = main.break_even(l4, q4, plan, api)["scale"]
        self.assertGreater(main.break_even(l4, q8, plan, api)["scale"], base)
        cheaper_api = main.ApiPrice("discounted", api.input_per_mtok * 0.8, api.output_per_mtok * 0.8, api.as_of, api.source)
        self.assertGreater(main.break_even(l4, q4, plan, cheaper_api)["scale"], base)
        cheaper_gpu = main.Instance("l4-spot", l4.hourly_price * 0.6, l4.as_of, "spot quote")
        self.assertLess(main.break_even(cheaper_gpu, q4, plan, api)["scale"], base)
        tiny = main.ApiPrice("tiny", 0.01, 0.04, api.as_of, api.source)
        never = main.break_even(l4, q4, plan, tiny)
        self.assertIsNone(never["scale"])
        self.assertIn("never", never["reason"])

    def test_plan_picks_the_cheapest_batch_that_meets_the_slo(self):
        report = main.evaluate(book(), [option(name) for name in GPU_OPTIONS], scenario())
        rows = {row["option"]: row for row in report["self_hosted"]}
        f16 = rows["gpu-l4-1x llama-8b F16"]
        self.assertIsNone(f16["chosen"])
        self.assertIn("15 tokens/s", f16["why_not"])
        self.assertEqual(rows["gpu-l4-1x llama-8b Q8_0"]["chosen"], 24)
        self.assertEqual(rows["gpu-l4-1x llama-8b Q4_K"]["chosen"], 31)
        q4 = [p for p in rows["gpu-l4-1x llama-8b Q4_K"]["points"] if p["concurrency"] == 1][0]
        self.assertTrue(q4["meets_slo"])
        self.assertTrue(q4["shortfall_hours"])
        self.assertEqual(report["cheapest"], {"name": "gpu-l4-1x llama-8b Q4_K", "kind": "self-hosted", "monthly": 1080.0})
        text = main.render_plan(report)
        self.assertIn("Budget review: support-chat", text)
        self.assertIn("gpu-l4-1x llama-8b Q4_K vs api-small: 0.708x current traffic", text)
        self.assertIn("not eligible: no point meets 15 tokens/s", text)

    def test_plan_matches_the_expected_report_fixture(self):
        report = main.evaluate(book(), [option(name) for name in GPU_OPTIONS], scenario())
        assert_close(self, fixture("expected-plan.json"), summarize(report))

    def test_replicas_from_the_autoscaler_replace_the_sizing(self):
        l4 = book().instances["gpu-l4-1x"]
        point = option("l4-llama8b-q4_k.json").points[-1]
        given = [1] * 24
        cost = main.self_hosted(l4, point, scenario(), replicas=given)
        self.assertEqual(cost["replica_hours_per_day"], 24)
        self.assertEqual(cost["shortfall_hours"], list(range(8, 19)))
        self.assertAlmostEqual(cost["utilization"], sum(min(rps, point.requests_per_second) for rps in scenario().hourly_rps) / (24 * point.requests_per_second))
        self.assertEqual(main.replicas_from({"hourly_replicas": given}), given)
        with self.assertRaises(main.InputError):
            main.replicas_from([1] * 23)

    def test_scenario_and_measurement_files_are_validated(self):
        for change in ({"hourly_requests_per_second": [1] * 23}, {"autoscaling": {"target_utilization": 70}}, {"cached_input_fraction": 1}, {"request": {"input_tokens": 10, "output_tokens": 0}}):
            with self.assertRaises(main.InputError):
                scenario(**change)
        with self.assertRaises(main.InputError):
            main.option_from({"instance": "gpu-l4-1x", "points": []}, "empty.json")
        with self.assertRaises(main.InputError):
            main.evaluate(book(), [option("l4-llama8b-q4_k.json")], scenario(compare_with=["api-missing"]))


class FakeServer(BaseHTTPRequestHandler):
    delay = 0.05
    bodies = []
    generated = 0
    lock = threading.Lock()

    def log_message(self, *args):
        pass

    def do_GET(self):
        if self.path == "/metrics":
            text = f"# TYPE llamacpp:tokens_predicted_total counter\nllamacpp:tokens_predicted_total {FakeServer.generated}\n"
            self.reply(200, text.encode(), "text/plain")
        else:
            self.reply(404, b"{}")

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["content-length"])))
        FakeServer.bodies.append(body)
        if self.path != "/v1/chat/completions":
            self.reply(404, b"{}")
            return
        time.sleep(FakeServer.delay)
        tokens = body["max_tokens"]
        with FakeServer.lock:
            FakeServer.generated += tokens
        usage = {"prompt_tokens": 50, "completion_tokens": tokens, "prompt_tokens_details": {"cached_tokens": 0}}
        self.reply(200, json.dumps({"choices": [{"message": {"content": "ok"}}], "usage": usage, "timings": {"prompt_n": 50, "prompt_ms": 10.0}}).encode())

    def reply(self, status, data, kind="application/json"):
        self.send_response(status)
        self.send_header("content-type", kind)
        self.send_header("content-length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


class MeasureTests(unittest.TestCase):
    def setUp(self):
        FakeServer.bodies, FakeServer.generated = [], 0
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), FakeServer)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"

    def test_closed_loop_load_over_real_sockets_measures_the_batching_gain(self):
        one, _ = main.run_load(self.url, "tiny", 1, 8, 32, "hello", cache_prompt=False)
        four, results = main.run_load(self.url, "tiny", 4, 8, 32, "hello", cache_prompt=False)
        self.assertGreater(four.requests_per_second, 2 * one.requests_per_second)
        self.assertEqual((four.input_tokens, four.output_tokens), (50, 32))
        self.assertAlmostEqual(four.prefill_tokens_per_second, 5000.0)
        self.assertLess(four.stream_tokens_per_second, 32 / FakeServer.delay)
        self.assertEqual(len(results), 8)
        body = FakeServer.bodies[-1]
        self.assertEqual((body["max_tokens"], body["ignore_eos"], body["cache_prompt"], body["temperature"]), (32, True, False, 0))
        self.assertEqual(len({b["messages"][0]["content"] for b in FakeServer.bodies}), 8)

    def test_measure_cli_writes_points_and_cross_checks_the_server_counter(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp)
        out = os.path.join(tmp, "m.json")
        code, _, err = run_cli("measure", "--url", self.url, "--instance", "cpu-4vcpu", "--concurrency", "1,4", "--requests", "4", "--max-tokens", "16", "--ignore-eos", "--metrics-url", self.url + "/metrics", "-o", out)
        self.assertEqual(code, 0, err)
        self.assertIn("c=4:", err)
        with open(out, encoding="utf-8") as handle:
            data = json.load(handle)
        self.assertEqual([p["concurrency"] for p in data["points"]], [1, 4])
        self.assertEqual(data["server_metrics"]["delta"], data["server_metrics"]["client_completion_tokens"])
        self.assertEqual(data["server_metrics"]["delta"], (1 + 4 + 4) * 16)
        self.assertEqual(len({p["prefill_tokens_per_second"] for p in data["points"]}), 1)
        self.assertEqual(main.option_from(data, "m.json").instance, "cpu-4vcpu")

    def test_errors_name_the_problem(self):
        with self.assertRaises(RuntimeError) as caught:
            main.run_load(self.url, "tiny", 1, 1, 8, "hi", post=lambda url, body, timeout: {"choices": []})
        self.assertIn("usage.completion_tokens", str(caught.exception))
        self.server.shutdown()
        self.server.server_close()
        code, _, err = run_cli("measure", "--url", self.url, "--instance", "cpu-4vcpu", "--requests", "1", "--warmup", "0")
        self.assertEqual(code, 1)
        self.assertIn("cannot reach", err)

    def test_prometheus_values_are_summed_over_label_sets(self):
        text = "# HELP x\nvllm:generation_tokens_total{model_name=\"a\"} 10\nvllm:generation_tokens_total{model_name=\"b\"} 5.5\nllamacpp:tokens_predicted_total 7\n"
        self.assertEqual(main.prometheus_value(text, "vllm:generation_tokens_total"), 15.5)
        self.assertEqual(main.prometheus_value(text, "llamacpp:tokens_predicted_total"), 7)
        self.assertIsNone(main.prometheus_value(text, "llamacpp:prompt_tokens_total"))


class AllocateTests(unittest.TestCase):
    def allocate(self, **kwargs):
        return main.allocate(fixture("gpu-nodes.json"), fixture("gpu-pods.json"), book(), "nvidia.com/gpu", **kwargs)

    def test_gpu_cost_follows_requests_and_idle_is_its_own_line(self):
        report = self.allocate()
        rows = {row["name"]: row for row in report["allocations"]}
        self.assertEqual(list(rows), ["chat", "search", main.IDLE])
        self.assertEqual((rows["chat"]["gpus"], rows["search"]["gpus"], rows[main.IDLE]["gpus"]), (2, 1, 1))
        self.assertAlmostEqual(rows["chat"]["cost"], 2 * 4.60 / 4 * 24)
        self.assertAlmostEqual(report["total"], 4.60 * 24)
        self.assertEqual(rows[main.IDLE]["share"], 0.25)
        self.assertEqual(report["nodes"], [{"node": "gpu-node-a", "instance": "gpu-l4-4x", "hourly_price": 4.6, "gpus": 4, "requested": 3}])
        by_team = {row["name"]: row["gpus"] for row in self.allocate(by="label:team")["allocations"]}
        self.assertEqual(by_team, {"assistant": 2, "search": 1, main.IDLE: 1})

    def test_share_idle_spreads_the_idle_cost_in_proportion(self):
        rows = {row["name"]: row for row in self.allocate(share_idle=True)["allocations"]}
        self.assertNotIn(main.IDLE, rows)
        self.assertAlmostEqual(rows["chat"]["cost"], 4.60 * 24 * 2 / 3, places=3)
        self.assertAlmostEqual(rows["search"]["share"], 1 / 3, places=3)

    def test_focus_rows_use_the_spec_columns_and_leave_idle_unallocated(self):
        start = datetime(2026, 10, 31, tzinfo=timezone.utc)
        rows = main.focus_rows(self.allocate(), start)
        self.assertEqual(list(rows[0]), main.FOCUS_COLUMNS)
        self.assertEqual((rows[0]["ChargePeriodStart"], rows[0]["ChargePeriodEnd"]), ("2026-10-31T00:00:00Z", "2026-11-01T00:00:00Z"))
        self.assertEqual((rows[0]["ConsumedQuantity"], rows[0]["ConsumedUnit"], rows[0]["ChargeCategory"]), (48.0, "GPU-Hours", "Usage"))
        self.assertEqual(json.loads(rows[0]["AllocatedTags"]), {"k8s/namespace": "chat"})
        self.assertEqual((rows[-1]["AllocatedResourceId"], rows[-1]["AllocatedTags"]), (None, None))
        self.assertAlmostEqual(sum(r["BilledCost"] for r in rows), 4.60 * 24)
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp)
        path = os.path.join(tmp, "focus.csv")
        code, out, err = run_cli("allocate", "--prices", os.path.join(FIXTURES, "prices.json"), "--today", "2026-11-01", "--nodes", os.path.join(FIXTURES, "gpu-nodes.json"), "--pods", os.path.join(FIXTURES, "gpu-pods.json"), "--focus", path, "--period-start", "2026-10-31T00:00:00Z")
        self.assertEqual(code, 0, err)
        self.assertIn("__idle__", out)
        with open(path, encoding="utf-8") as handle:
            self.assertEqual(len(list(csv.DictReader(handle))), 3)

    def test_an_unpriced_node_is_an_error_that_names_the_fix(self):
        nodes = fixture("gpu-nodes.json")
        del nodes["items"][0]["metadata"]["labels"]
        with self.assertRaises(main.InputError) as caught:
            main.allocate(nodes, fixture("gpu-pods.json"), book(), "nvidia.com/gpu")
        self.assertIn("--instance gpu-node-a=NAME", str(caught.exception))
        report = main.allocate(nodes, fixture("gpu-pods.json"), book(), "nvidia.com/gpu", instance_for={"gpu-node-a": "gpu-l4-4x"})
        self.assertAlmostEqual(report["total"], 110.4)


class CliTests(unittest.TestCase):
    def test_plan_cli_text_json_and_exit_codes(self):
        args = ["plan", "--prices", os.path.join(FIXTURES, "prices.json"), "--today", "2026-11-01", "--scenario", os.path.join(FIXTURES, "scenario-cpu.json"), "--measurements", os.path.join(FIXTURES, "cpu-smollm2-q8_0.json")]
        code, out, err = run_cli(*args)
        self.assertEqual(code, 0, err)
        self.assertIn("Budget review: internal-docs-bot", out)
        code, out, _ = run_cli(*args, "--json")
        self.assertEqual(json.loads(out)["self_hosted"][0]["instance"], "cpu-4vcpu")
        code, _, err = run_cli("plan", "--prices", "/nonexistent/prices.json", "--scenario", "x", "--measurements", "y")
        self.assertEqual(code, 2)
        self.assertIn("no such file", err)
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp)
        broken = os.path.join(tmp, "prices.json")
        with open(broken, "w", encoding="utf-8") as handle:
            json.dump({"instances": [{"name": "gpu", "hourly_price": 1, "as_of": "2026-10-01"}]}, handle)
        code, _, err = run_cli("prices", broken, "--today", "2026-11-01")
        self.assertEqual(code, 2)
        self.assertIn("no source", err)


if __name__ == "__main__":
    unittest.main()
