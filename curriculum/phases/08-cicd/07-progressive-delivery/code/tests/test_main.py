import copy
import json
import math
import os
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.parse
import urllib.request
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import main  # noqa: E402

FIXTURES = os.path.join(os.path.dirname(__file__), "..", "fixtures")
ROLLOUT = os.path.join(FIXTURES, "rollout.json")
ANALYSIS = os.path.join(FIXTURES, "analysis.json")
BLUE_GREEN = os.path.join(FIXTURES, "blue-green.json")
METRICS = os.path.join(FIXTURES, "metrics.txt")


def load(path):
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


def templates(doc=None):
    template = main.parse_template(doc or load(ANALYSIS))
    return {template.name: template}


def rollout(version="v2", steps=None, **strategy):
    doc = load(ROLLOUT)
    doc["spec"]["template"]["spec"]["containers"][0]["env"][0]["value"] = version
    canary = doc["spec"]["strategy"]["canary"]
    for step in canary["steps"]:
        if "analysis" in step:
            step["analysis"]["args"][0]["value"] = version
    if steps is not None:
        canary["steps"] = steps
    canary.update(strategy)
    return doc


def controller(doc=None, values=None, status=None, target=None, analysis=None):
    values = values if values is not None else {"success-rate": [1.0], "p95-latency": [0.01]}

    def measure(metric):
        value = values[metric.name]
        return value() if callable(value) else value

    st = status if status is not None else main.Status(phase=main.HEALTHY, ready=True, revision=1, stable="old", stable_template={"spec": "old"})
    return main.Controller(main.parse_rollout(doc or rollout()), templates(analysis), target or main.SimTarget(4, 1), measure, st)


def drive(c, seconds, start=0):
    for t in range(start, start + seconds + 1):
        st = c.reconcile(float(t))
        if st.phase == main.DEGRADED or (st.phase == main.HEALTHY and not st.canary and t > start):
            return t
    return None


def reasons(status):
    return [e["reason"] for e in status.events]


def run_cli(argv):
    out, err = StringIO(), StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = main.main(argv)
    return code, out.getvalue(), err.getvalue()


def get(url, headers=None):
    request = urllib.request.Request(url, headers=headers or {})
    try:
        with main.NO_PROXY.open(request, timeout=5) as response:
            return response.status, response.headers, response.read().decode()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.headers, exc.read().decode()


def counter_db(values, start=1.0, name="http_requests_total", labels=None):
    db = main.TSDB()
    for offset, value in enumerate(values):
        db.append(start + offset, [(name, dict(labels or {}), float(value))])
    return db


class FakeKube:
    def __init__(self, lag=False):
        self.objects = {}
        self.applies = []
        self.calls = []
        self.lag = lag
        self.not_ready = set()

    def __call__(self, argv, stdin=None):
        args = list(argv)
        self.calls.append(args)
        if "apply" in args:
            for item in json.loads(stdin)["items"]:
                name = item["metadata"]["name"]
                current = self.objects.get(name)
                generation = (current["metadata"]["generation"] if current else 0) + (0 if current and current["spec"] == item["spec"] else 1)
                obj = copy.deepcopy(item)
                obj["metadata"]["generation"] = generation
                replicas = item["spec"]["replicas"]
                obj["status"] = {"observedGeneration": generation, "replicas": replicas, "updatedReplicas": replicas, "readyReplicas": replicas}
                if self.lag and (not current or current["spec"] != item["spec"]):
                    obj["status"] = {"observedGeneration": generation - 1, "replicas": replicas, "updatedReplicas": 0, "readyReplicas": 0}
                    self.not_ready.add(name)
                self.objects[name] = obj
                self.applies.append((name, replicas, item["metadata"]["annotations"][main.REVISION_ANNOTATION]))
            return 0, "serverside-applied", ""
        if "get" in args:
            names = [a for a in args if a.startswith("demo-")]
            items = []
            for name in names:
                if name not in self.objects:
                    continue
                obj = self.objects[name]
                if name in self.not_ready:
                    self.not_ready.discard(name)
                    snapshot = copy.deepcopy(obj)
                    replicas = obj["spec"]["replicas"]
                    obj["status"] = {"observedGeneration": obj["metadata"]["generation"], "replicas": replicas, "updatedReplicas": replicas, "readyReplicas": replicas}
                    items.append(snapshot)
                else:
                    items.append(copy.deepcopy(obj))
            return 0, json.dumps({"kind": "List", "items": items}), ""
        return 1, "", "unexpected kubectl call"


class Servers:
    def __init__(self):
        self.servers = []

    def start(self, server):
        main.serve_in_thread(server)
        self.servers.append(server)
        return main.base_url(server)

    def close(self):
        for server in self.servers:
            server.shutdown()
            server.server_close()


class DurationAndNumberTests(unittest.TestCase):
    def test_durations_accept_go_style_strings_and_plain_seconds(self):
        self.assertEqual(main.parse_duration("30s"), 30)
        self.assertEqual(main.parse_duration("1m30s"), 90)
        self.assertEqual(main.parse_duration("500ms"), 0.5)
        self.assertEqual(main.parse_duration("2h"), 7200)
        self.assertEqual(main.parse_duration(10), 10)
        self.assertEqual(main.parse_duration("15"), 15)
        for bad in ("", "10 s", "1x", "s", True):
            with self.assertRaises(main.InputError):
                main.parse_duration(bad)

    def test_numbers_are_printed_the_way_prometheus_prints_them(self):
        self.assertEqual(main.format_number(1.0), "1")
        self.assertEqual(main.format_number(0.95), "0.95")
        self.assertEqual(main.format_number(1e-05), "0.00001")
        self.assertEqual(main.format_number(math.inf), "+Inf")
        self.assertEqual(main.format_number(-math.inf), "-Inf")
        self.assertEqual(main.format_number(math.nan), "NaN")


class ExpositionTests(unittest.TestCase):
    def test_registry_renders_cumulative_buckets_that_parse_back(self):
        registry = main.Registry(buckets=(0.1, 0.5))
        registry.observe({"version": "v1"}, 200, 0.05, times=3)
        registry.observe({"version": "v1"}, 500, 0.3)
        registry.observe({"version": "v1"}, 200, 2.0)
        text = registry.render()
        self.assertIn("# TYPE http_requests_total counter", text)
        self.assertIn("# TYPE http_request_duration_seconds histogram", text)
        self.assertIn('http_request_duration_seconds_bucket{version="v1",le="+Inf"} 5', text)
        parsed = {(name, main.freeze(labels)): value for name, labels, value in main.parse_exposition(text)}
        self.assertEqual(parsed[("http_requests_total", (("code", "200"), ("version", "v1")))], 4)
        self.assertEqual(parsed[("http_requests_total", (("code", "500"), ("version", "v1")))], 1)
        buckets = [parsed[("http_request_duration_seconds_bucket", (("le", le), ("version", "v1")))] for le in ("0.1", "0.5", "+Inf")]
        self.assertEqual(buckets, [3, 4, 5])
        self.assertAlmostEqual(parsed[("http_request_duration_seconds_sum", (("version", "v1"),))], 0.15 + 0.3 + 2.0)

    def test_parser_reads_escapes_timestamps_and_special_values(self):
        with open(METRICS, encoding="utf-8") as handle:
            samples = main.parse_exposition(handle.read())
        by_name = {}
        for name, labels, value in samples:
            by_name.setdefault(name, []).append((labels, value))
        self.assertEqual(by_name["http_requests_total"][1], ({"code": "500", "version": "v1"}, 3.0))
        self.assertEqual(by_name["http_requests_total"][2][0]["path"], '/say "hi"\\n')
        self.assertEqual(by_name["process_start_time_seconds"][0][1], 1.73e9)
        self.assertTrue(math.isnan(by_name["go_gc_duration_seconds"][0][1]))
        self.assertEqual(len(by_name["http_request_duration_seconds_bucket"]), 4)

    def test_a_bad_line_is_named_with_its_number(self):
        with self.assertRaises(main.InputError) as caught:
            main.parse_exposition('# HELP x y\nok_total 1\nbroken{code="200" 3\n')
        self.assertIn("line 3", str(caught.exception))


class PromQLTests(unittest.TestCase):
    def query(self, db, text, at):
        return main.evaluate_query(main.parse_query(text), db, at)

    def test_rate_extrapolates_to_the_window_like_prometheus_and_survives_a_reset(self):
        db = counter_db([10 * t for t in range(1, 11)])
        [(labels, value)] = self.query(db, "rate(http_requests_total[10s])", 10.0)
        self.assertEqual(labels, {})
        self.assertAlmostEqual(value, 10.0)
        self.assertEqual(self.query(db, "rate(http_requests_total[10s])", 50.0), [])
        self.assertEqual(main.counter_increase([(1, 10), (2, 20), (3, 30), (4, 5), (5, 15)]), 35)
        young = counter_db([5, 15, 25], start=8.0)
        [(_, increase)] = self.query(young, "increase(http_requests_total[60s])", 10.0)
        self.assertAlmostEqual(increase, 25.0)

    def test_sum_by_keeps_only_the_grouping_labels_and_ratios_match_on_labels(self):
        db = main.TSDB()
        for t in (1.0, 2.0, 3.0):
            db.append(t, [
                ("http_requests_total", {"version": "v2", "code": "200", "pod": "a"}, 90 * t),
                ("http_requests_total", {"version": "v2", "code": "500", "pod": "a"}, 10 * t),
                ("http_requests_total", {"version": "v1", "code": "200", "pod": "b"}, 50 * t),
            ])
        by_version = dict((labels["version"], value) for labels, value in self.query(db, "sum by (version) (rate(http_requests_total[3s]))", 3.0))
        self.assertAlmostEqual(by_version["v2"], 100.0)
        self.assertAlmostEqual(by_version["v1"], 50.0)
        [(_, ratio)] = self.query(db, 'sum(rate(http_requests_total{version="v2",code=~"2.."}[3s])) / sum(rate(http_requests_total{version="v2"}[3s]))', 3.0)
        self.assertAlmostEqual(ratio, 0.9)
        [(_, others)] = self.query(db, 'sum(rate(http_requests_total{code!="200",version!~"v1"}[3s]))', 3.0)
        self.assertAlmostEqual(others, 10.0)

    def test_an_error_ratio_without_errors_is_empty_until_or_vector_zero(self):
        db = main.TSDB()
        for t in (1.0, 2.0, 3.0):
            db.append(t, [("http_requests_total", {"version": "v2", "code": "200"}, 10 * t)])
        errors = 'sum(rate(http_requests_total{version="v2",code=~"5.."}[3s]))'
        self.assertEqual(self.query(db, f"{errors} / sum(rate(http_requests_total[3s]))", 3.0), [])
        [(_, value)] = self.query(db, f"({errors} or vector(0)) / sum(rate(http_requests_total[3s]))", 3.0)
        self.assertEqual(value, 0.0)
        self.assertEqual(self.query(db, "1 / 0", 3.0), math.inf)
        self.assertTrue(math.isnan(self.query(db, "0 / 0", 3.0)))

    def test_histogram_quantile_interpolates_inside_the_bucket(self):
        def vector(counts):
            return [({"le": le}, float(count)) for le, count in counts]

        buckets = vector([("0.1", 50), ("0.25", 90), ("0.5", 100), ("+Inf", 100)])
        self.assertAlmostEqual(main.histogram_quantile(0.95, buckets)[0][1], 0.375)
        self.assertAlmostEqual(main.histogram_quantile(0.5, buckets)[0][1], 0.1)
        self.assertEqual(main.histogram_quantile(0.95, vector([("0.1", 10), ("+Inf", 100)]))[0][1], 0.1)
        self.assertTrue(math.isnan(main.histogram_quantile(0.95, vector([("0.1", 0), ("+Inf", 0)]))[0][1]))
        self.assertTrue(math.isnan(main.histogram_quantile(0.95, vector([("0.1", 5), ("0.5", 9)]))[0][1]))

    def test_parse_errors_say_what_is_wrong(self):
        db = main.TSDB()
        for text, words in (
            ("http_requests_total[5m]", "range vector"),
            ("rate(http_requests_total)", "range vector"),
            ('up{code<"2"}', "unexpected character"),
            ("sum(rate(x[1m])", "expected ')'"),
            ("sum without (le) (x)", "without"),
            ('x{code=~"("}', "invalid regular expression"),
        ):
            with self.assertRaises(main.QueryError, msg=text) as caught:
                self.query(db, text, 1.0)
            self.assertIn(words, str(caught.exception))

    def test_query_response_has_the_prometheus_api_shape(self):
        db = counter_db([1, 2, 3])
        code, payload = main.query_response(db, "http_requests_total", 3.0)
        self.assertEqual(code, 200)
        self.assertEqual(payload["data"], {"resultType": "vector", "result": [{"metric": {}, "value": [3.0, "3"]}]})
        code, payload = main.query_response(db, "2 * 21", 3.0)
        self.assertEqual(payload["data"], {"resultType": "scalar", "result": [3.0, "42"]})
        code, payload = main.query_response(db, "rate(", 3.0)
        self.assertEqual((code, payload["status"], payload["errorType"]), (400, "error", "bad_data"))


class PrometheusAPITests(unittest.TestCase):
    def setUp(self):
        self.servers = Servers()

    def tearDown(self):
        self.servers.close()

    def test_the_stub_answers_post_and_get_like_prometheus(self):
        registry, db = main.Registry(), main.TSDB()
        now = time.time()
        for offset, value in ((-2, 10.0), (-1, 20.0), (0, 30.0)):
            db.append(now + offset, [("http_requests_total", {"version": "v2", "code": "200"}, value)])
        url = self.servers.start(main.MetricsServer(("127.0.0.1", 0), registry, db))
        self.assertEqual(len(main.prometheus_query(url, 'sum(rate(http_requests_total{version="v2"}[10s]))')), 1)
        status, _, body = get(url + "/api/v1/query?" + urllib.parse.urlencode({"query": "http_requests_total", "time": str(now)}))
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["data"]["result"][0]["value"][1], "30")
        with self.assertRaises(main.MeasurementError) as caught:
            main.prometheus_query(url, "rate(http_requests_total)")
        self.assertIn("HTTP 400", str(caught.exception))
        with self.assertRaises(main.MeasurementError):
            main.prometheus_query("", "up")
        self.assertEqual(get(url + "/-/ready")[0], 200)


class ConditionTests(unittest.TestCase):
    def test_argo_style_conditions_evaluate_on_a_vector(self):
        self.assertTrue(main.Condition("result[0] >= 0.95").evaluate([0.97]))
        self.assertFalse(main.Condition("result[0] >= 0.95").evaluate([0.5]))
        self.assertTrue(main.Condition("len(result) == 0 || result[0] < 0.25").evaluate([]))
        self.assertTrue(main.Condition("isNaN(result[0]) or result[0] > 2 * 0.4").evaluate([math.nan]))
        self.assertTrue(main.Condition("!(result < 1) && result != 3").evaluate(2.0))
        self.assertTrue(main.Condition("asFloat(result[0]) - 1 == -0.5").evaluate([0.5]))

    def test_conditions_that_cannot_be_evaluated_raise_with_the_reason(self):
        with self.assertRaises(main.ConditionError) as caught:
            main.Condition("result[0] >= 0.95").evaluate([])
        self.assertIn("index out of range", str(caught.exception))
        with self.assertRaises(main.ConditionError) as caught:
            main.Condition("result >= 0.95").evaluate([0.9])
        self.assertIn("compare result[0]", str(caught.exception))
        with self.assertRaises(main.ConditionError):
            main.Condition("result[0] >= ")
        with self.assertRaises(main.ConditionError):
            main.Condition("bogus(result)")
        with self.assertRaises(main.ConditionError):
            main.Condition("result[0] + 1").evaluate([1.0])

    def test_success_and_failure_conditions_combine_like_argo(self):
        def spec(success=None, failure=None):
            return main.MetricSpec("m", "up", "", 0.0, 1, 0.0, main.Condition(success) if success else None, main.Condition(failure) if failure else None)

        self.assertEqual(main.evaluate_result([1.0], spec()), main.SUCCESSFUL)
        self.assertEqual(main.evaluate_result([0.5], spec(success="result[0] > 0.9")), main.FAILED)
        self.assertEqual(main.evaluate_result([0.5], spec(failure="result[0] < 0.1")), main.SUCCESSFUL)
        both = spec(success="result[0] > 0.9", failure="result[0] < 0.1")
        self.assertEqual(main.evaluate_result([0.5], both), main.INCONCLUSIVE)
        self.assertEqual(main.evaluate_result([0.05], both), main.FAILED)
        with self.assertRaises(main.ConditionError) as caught:
            main.evaluate_result([], spec(success="result[0] > 0.9"))
        self.assertIn("no data returned from the metric provider", str(caught.exception))


class AnalysisTests(unittest.TestCase):
    def metrics(self, args=None, change=None):
        doc = load(ANALYSIS)
        if change:
            change(doc)
        return main.parse_template(doc).resolve(args or {"version": "v2"})

    def test_initial_delay_interval_and_count_decide_when_to_measure(self):
        taken = []
        run = main.AnalysisRun("r", self.metrics()[:1], 100.0)
        for t in range(100, 112):
            run.tick(float(t), lambda metric: taken.append(t) or [1.0])
        self.assertEqual(taken, [105, 108])
        self.assertEqual(run.phase, main.SUCCESSFUL)

    def test_one_failure_over_the_limit_fails_the_run_with_argos_message(self):
        run = main.AnalysisRun("r", self.metrics(), 0.0)
        values = {"success-rate": [0.5], "p95-latency": [0.01]}
        for t in range(0, 10):
            run.tick(float(t), lambda metric: values[metric.name])
        self.assertEqual(run.phase, main.FAILED)
        self.assertEqual(run.message, 'Metric "success-rate" assessed Failed due to failed (1) > failureLimit (0)')
        self.assertEqual([m.phase for m in run.metrics[1].measurements], [main.SUCCESSFUL])
        self.assertEqual(run.metrics[1].phase, main.SUCCESSFUL)

    def test_errors_retry_every_ten_seconds_and_fail_after_the_consecutive_limit(self):
        def broken(metric):
            raise main.MeasurementError("connection refused")

        run = main.AnalysisRun("r", self.metrics()[:1], 0.0)
        attempts = []
        for t in range(0, 60):
            before = len(run.metrics[0].measurements)
            run.tick(float(t), broken)
            if len(run.metrics[0].measurements) > before:
                attempts.append(t)
        self.assertEqual(attempts, [5, 15, 25, 35, 45])
        self.assertEqual(run.phase, main.ERROR)
        self.assertIn("consecutiveErrors (5) > consecutiveErrorLimit (4)", run.message)
        self.assertIn('"Error Message: connection refused"', run.message)

    def test_args_are_substituted_and_a_missing_one_is_named(self):
        [rate, latency] = self.metrics({"version": "v7"})
        self.assertIn('version="v7"', rate.query)
        self.assertIn('version="v7"', latency.query)
        self.assertEqual((rate.interval, rate.count, rate.initial_delay), (3.0, 2, 5.0))
        with self.assertRaises(main.InputError) as caught:
            main.parse_template(load(ANALYSIS)).resolve({})
        self.assertIn("argument 'version' has no value", str(caught.exception))

        def undeclared(doc):
            doc["spec"]["args"] = []

        doc = load(ANALYSIS)
        undeclared(doc)
        with self.assertRaises(main.InputError) as caught:
            main.parse_template(doc).resolve({})
        self.assertIn("{{args.version}}", str(caught.exception))

    def test_only_the_prometheus_provider_and_valid_queries_are_accepted(self):
        def web(doc):
            doc["spec"]["metrics"][0]["provider"] = {"web": {"url": "http://x"}}

        def broken(doc):
            doc["spec"]["metrics"][0]["provider"]["prometheus"]["query"] = "sum(rate(x[1m])"

        def no_interval(doc):
            del doc["spec"]["metrics"][0]["interval"]

        for change, words in ((web, "prometheus provider only (found web)"), (broken, "expected ')'"), (no_interval, "needs an interval")):
            with self.assertRaises(main.InputError) as caught:
                self.metrics(change=change)
            self.assertIn(words, str(caught.exception))


class RolloutSpecTests(unittest.TestCase):
    def test_canary_steps_surge_and_revision_are_parsed(self):
        spec = main.parse_rollout(rollout())
        self.assertEqual([s.label for s in spec.steps], ["setWeight: 25", "analysis: success-rate", "setWeight: 50", "pause: 2s", "analysis: success-rate"])
        self.assertEqual((spec.replicas, spec.max_surge, spec.selector), (4, 1, {"app": "demo"}))
        self.assertEqual(spec.steps[1].args, {"version": "v2"})
        self.assertNotEqual(spec.revision, main.parse_rollout(rollout("v3")).revision)
        self.assertEqual(main.parse_rollout(rollout(maxSurge=0)).max_surge, 0)

    def test_steps_minirollout_does_not_implement_are_rejected_by_number(self):
        for steps, words in (
            ([{"setWeight": 10}, {"setCanaryScale": {"replicas": 1}}], "step 2: setCanaryScale is not implemented"),
            ([{"setWeight": 150}], "between 0 and 100"),
            ([{"setWeight": 10, "pause": {}}], "exactly one key"),
            ([{"analysis": {"templates": []}}], "analysis needs templates"),
        ):
            with self.assertRaises(main.InputError) as caught:
                main.parse_rollout(rollout(steps=steps))
            self.assertIn(words, str(caught.exception))

    def test_blue_green_compiles_to_preview_analysis_pause_switch_analysis(self):
        spec = main.parse_rollout(load(BLUE_GREEN))
        self.assertEqual(spec.strategy, "blueGreen")
        self.assertEqual([s.kind for s in spec.steps], ["preview", "analysis", "pause", "setWeight", "analysis"])
        self.assertEqual(spec.steps[2].reason, "BlueGreenPause")
        self.assertIsNone(spec.steps[2].duration)
        self.assertEqual(spec.steps[3].label, "switch shop to the new version")


class WeightTests(unittest.TestCase):
    def test_canary_counts_follow_argo_rollouts_rounding(self):
        self.assertEqual(main.canary_counts(4, 25, 1), (1, 3))
        self.assertEqual(main.canary_counts(3, 10, 1), (1, 3))
        self.assertEqual(main.canary_counts(3, 10, 0), (1, 2))
        self.assertEqual(main.canary_counts(3, 50, 1), (2, 2))
        self.assertEqual(main.canary_counts(1, 50, 1), (1, 1))
        self.assertEqual(main.canary_counts(1, 50, 0), (1, 0))
        self.assertEqual(main.canary_counts(10, 0, 3), (0, 10))
        self.assertEqual(main.canary_counts(10, 100, 3), (10, 0))
        self.assertEqual(main.canary_counts(10, 5, 0), (1, 9))
        self.assertEqual(main.canary_counts(0, 50, 1), (0, 0))

    def test_exact_share_is_exact_and_evenly_spread(self):
        share = main.ExactShare(0.25)
        picks = [share.next() for _ in range(100)]
        self.assertEqual(sum(picks), 25)
        self.assertEqual([i for i, p in enumerate(picks[:12]) if p], [3, 7, 11])
        share.set(0.5)
        self.assertEqual([share.next() for _ in range(4)], [False, True, False, True])
        self.assertFalse(any(main.ExactShare(0.0).next() for _ in range(50)))


class ControllerTests(unittest.TestCase):
    def test_a_good_version_walks_every_step_and_becomes_stable(self):
        c = controller()
        self.assertEqual(drive(c, 60), 18)
        st = c.status
        self.assertEqual((st.phase, st.revision, st.stable, st.canary), (main.HEALTHY, 2, c.spec.revision, ""))
        self.assertEqual(reasons(st).count("RolloutStepCompleted"), 5)
        self.assertEqual(reasons(st).count("AnalysisRunSuccessful"), 2)
        self.assertEqual(reasons(st)[-1], "RolloutCompleted")
        self.assertIn("Rollout step 4/5 completed (pause: 2s)", [e["message"] for e in st.events])
        self.assertEqual(st.stable_template, c.spec.template)

    def test_a_bad_version_aborts_on_the_first_failed_measurement(self):
        target = main.SimTarget(4, 1)
        c = controller(values={"success-rate": [0.5], "p95-latency": [0.01]}, target=target)
        self.assertEqual(drive(c, 60), 5)
        st = c.status
        self.assertEqual((st.phase, st.abort, st.weight, st.stable), (main.DEGRADED, True, 0, "old"))
        self.assertEqual(st.message, 'RolloutAborted: Rollout aborted update to revision 2: Metric "success-rate" assessed Failed due to failed (1) > failureLimit (0)')
        self.assertEqual(target.live, 0.0)
        self.assertEqual(st.runs[-1]["metrics"][0]["measurements"][0]["value"], [0.5])

    def test_an_indefinite_pause_waits_for_promote(self):
        c = controller(rollout(steps=[{"setWeight": 20}, {"pause": {}}, {"setWeight": 60}]))
        self.assertIsNone(drive(c, 30))
        self.assertEqual((c.status.phase, c.status.message, c.status.weight), (main.PAUSED, "CanaryPauseStep", 20))
        self.assertTrue(c.waiting_for_human())
        c.status.skip_pause = True
        self.assertEqual(c.reconcile(31.0).phase, main.HEALTHY)
        self.assertIn("RolloutResumed", reasons(c.status))

    def test_promote_full_skips_every_remaining_step(self):
        c = controller(rollout(steps=[{"setWeight": 10}, {"pause": {}}, {"setWeight": 50}, {"pause": {}}]))
        drive(c, 3)
        c.status.promote_full = True
        st = c.reconcile(4.0)
        self.assertEqual((st.phase, st.stable), (main.HEALTHY, c.spec.revision))
        self.assertEqual(reasons(st).count("RolloutStepCompleted"), 1)

    def test_setting_the_template_back_after_an_abort_is_healthy_again(self):
        status = main.Status()
        first = controller(rollout("v1"), status=status)
        self.assertEqual(first.reconcile(0.0).phase, main.HEALTHY)
        stable = status.stable
        bad = controller(rollout("v2"), values={"success-rate": [0.2], "p95-latency": [0.01]}, status=status)
        self.assertEqual(drive(bad, 30, start=1), 6)
        back = controller(rollout("v1"), status=status)
        st = back.reconcile(40.0)
        self.assertEqual((st.phase, st.abort, st.canary, st.stable), (main.HEALTHY, False, "", stable))

    def test_a_new_template_during_a_rollout_restarts_the_steps(self):
        c = controller()
        drive(c, 3)
        c.spec = main.parse_rollout(rollout("v3"))
        st = c.reconcile(4.0)
        self.assertEqual((st.revision, st.canary), (3, c.spec.revision))
        self.assertIn("replaces the canary in progress", " ".join(e["message"] for e in st.events))
        self.assertEqual(st.runs[-1]["phase"], main.SUCCESSFUL)

    def test_background_analysis_aborts_between_steps(self):
        doc = load(ANALYSIS)
        for metric in doc["spec"]["metrics"]:
            del metric["count"]
        clock = {"t": 0}
        c = controller(
            rollout(steps=[{"setWeight": 20}, {"pause": {"duration": "30s"}}, {"setWeight": 50}], analysis={"templates": [{"templateName": "success-rate"}], "args": [{"name": "version", "value": "v2"}], "startingStep": 1}),
            values={"success-rate": lambda: [1.0] if clock["t"] < 12 else [0.4], "p95-latency": [0.01]},
            analysis=doc,
        )
        for t in range(0, 30):
            clock["t"] = t
            if c.reconcile(float(t)).phase == main.DEGRADED:
                break
        self.assertEqual((t, c.status.step), (12, 1))
        self.assertIn("AnalysisRun demo-2-background Failed", " ".join(e["message"] for e in c.status.events))

    def test_an_inconclusive_analysis_pauses_until_promoted(self):
        doc = load(ANALYSIS)
        doc["spec"]["metrics"][0]["failureCondition"] = "result[0] < 0.5"
        c = controller(rollout(steps=[{"setWeight": 20}, {"analysis": {"templates": [{"templateName": "success-rate"}], "args": [{"name": "version", "value": "v2"}]}}]), values={"success-rate": [0.7], "p95-latency": [0.01]}, analysis=doc)
        self.assertIsNone(drive(c, 20))
        self.assertEqual((c.status.phase, c.status.message), (main.PAUSED, "InconclusiveAnalysisRun"))
        self.assertTrue(c.waiting_for_human())
        c.status.skip_pause = True
        self.assertEqual(c.reconcile(21.0).phase, main.HEALTHY)

    def test_blue_green_switches_all_at_once_and_back_when_post_analysis_fails(self):
        target = main.SimTarget(3, 0, by_replicas=True)
        values = {"success-rate": [1.0], "p95-latency": [0.01]}
        c = controller(load(BLUE_GREEN), values=values, target=target)
        self.assertIsNone(drive(c, 20))
        self.assertEqual((c.status.phase, c.status.message, target.pods, target.live), (main.PAUSED, "BlueGreenPause", 3, 0.0))
        c.status.skip_pause = True
        c.reconcile(21.0)
        self.assertEqual(target.live, 100.0)
        values["success-rate"] = [0.1]
        self.assertEqual(drive(c, 20, start=22), 26)
        self.assertEqual((c.status.phase, target.live, target.pods), (main.DEGRADED, 0.0, 0))

    def test_status_survives_a_round_trip_through_the_state_file(self):
        c = controller(values={"success-rate": [0.5], "p95-latency": [0.01]})
        drive(c, 10)
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "state.json")
            main.save_state(path, c.status)
            self.assertEqual(main.load_state(path), c.status)
            self.assertEqual(main.load_state(os.path.join(tmp, "missing.json")), main.Status())


class KubeTargetTests(unittest.TestCase):
    def setUp(self):
        self.spec = main.parse_rollout(rollout())
        self.old = main.parse_rollout(rollout("v1"))

    def desired(self, weight, promoting=False):
        if promoting:
            return main.Desired(self.spec.revision, self.spec.template, promoting=True)
        return main.Desired(self.old.revision, self.old.template, self.spec.revision, self.spec.template, weight)

    def test_weights_become_pod_counts_on_two_deployments_applied_server_side(self):
        kube = FakeKube()
        target = main.KubeTarget(self.spec, "cc", "kind-x", kube)
        self.assertTrue(target.sync(self.desired(25), 0.0))
        self.assertEqual(sorted(kube.applies), [("demo-canary", 1, self.spec.revision), ("demo-stable", 3, self.old.revision)])
        apply_call = [c for c in kube.calls if "apply" in c][0]
        self.assertEqual(apply_call[:5], ["kubectl", "--context", "kind-x", "-n", "cc"])
        self.assertIn("--server-side", apply_call)
        self.assertIn("--field-manager=minirollout", apply_call)
        canary = kube.objects["demo-canary"]
        self.assertEqual(canary["spec"]["selector"]["matchLabels"], {"app": "demo", "track": "canary"})
        self.assertEqual(canary["spec"]["template"]["metadata"]["labels"], {"app": "demo", "track": "canary"})
        self.assertEqual(canary["spec"]["template"]["spec"]["containers"][0]["env"][0]["value"], "v2")
        count = len(kube.applies)
        target.sync(self.desired(25), 1.0)
        self.assertEqual(len(kube.applies), count)
        target.sync(self.desired(50), 2.0)
        self.assertEqual(kube.objects["demo-canary"]["spec"]["replicas"], 2)
        self.assertEqual(kube.objects["demo-stable"]["spec"]["replicas"], 2)

    def test_promotion_rolls_the_stable_deployment_before_scaling_the_canary_to_zero(self):
        kube = FakeKube(lag=True)
        target = main.KubeTarget(self.spec, "cc", None, kube)
        target.sync(self.desired(50), 0.0)
        target.sync(self.desired(50), 1.0)
        kube.applies.clear()
        results = [target.sync(self.desired(0, promoting=True), t) for t in (2.0, 3.0, 4.0, 5.0)]
        self.assertEqual(results, [False, False, False, True])
        self.assertEqual(kube.applies, [("demo-stable", 4, self.spec.revision), ("demo-canary", 0, self.spec.revision)])
        self.assertEqual(kube.objects["demo-stable"]["spec"]["template"]["spec"]["containers"][0]["env"][0]["value"], "v2")

    def test_blue_green_preview_is_refused_with_a_pointer(self):
        target = main.KubeTarget(self.spec, "cc", None, FakeKube())
        preview = main.Desired(self.old.revision, self.old.template, self.spec.revision, self.spec.template, 0, preview=True)
        with self.assertRaises(main.TargetError) as caught:
            target.sync(preview, 0.0)
        self.assertIn("--target proxy", str(caught.exception))

    def test_a_kubectl_failure_carries_its_stderr(self):
        target = main.KubeTarget(self.spec, "cc", None, lambda argv, stdin=None: (1, "", 'Error from server (Forbidden): deployments.apps is forbidden'))
        with self.assertRaises(main.TargetError) as caught:
            target.sync(self.desired(25), 0.0)
        self.assertIn("Forbidden", str(caught.exception))


class ServerTests(unittest.TestCase):
    def setUp(self):
        self.servers = Servers()

    def tearDown(self):
        self.servers.close()

    def test_the_app_fails_the_configured_share_and_counts_it_in_metrics(self):
        url = self.servers.start(main.AppServer(("127.0.0.1", 0), "v9", 0.25))
        results = [get(url + "/")[:2] for _ in range(8)]
        self.assertEqual([code for code, _ in results].count(500), 2)
        self.assertTrue(all(headers.get("X-Version") == "v9" for _, headers in results))
        self.assertEqual(get(url + "/healthz")[0], 200)
        samples = main.parse_exposition(get(url + "/metrics")[2])
        counts = {labels["code"]: value for name, labels, value in samples if name == "http_requests_total"}
        self.assertEqual(counts, {"200": 6.0, "500": 2.0})
        request = urllib.request.Request(url + "/admin/errors?rate=0", data=b"", method="POST")
        with main.NO_PROXY.open(request, timeout=5) as response:
            self.assertEqual(response.status, 200)
        self.assertTrue(all(get(url + "/")[0] == 200 for _ in range(4)))

    def test_the_proxy_splits_exactly_records_versions_and_honours_the_canary_header(self):
        stable = self.servers.start(main.AppServer(("127.0.0.1", 0), "v1"))
        canary = self.servers.start(main.AppServer(("127.0.0.1", 0), "v2"))
        proxy = main.ProxyServer(("127.0.0.1", 0), stable, canary, 25)
        url = self.servers.start(proxy)
        seen = [get(url + "/")[1] for _ in range(20)]
        self.assertEqual(sum(1 for h in seen if h.get("X-Version") == "v2"), 5)
        self.assertEqual({h.get("X-Upstream") for h in seen if h.get("X-Version") == "v2"}, {"canary"})
        self.assertEqual(get(url + "/", {"X-Canary": "always"})[1].get("X-Version"), "v2")
        now = time.time()
        proxy.sample(now - 1)
        proxy.sample(now)
        result =main.evaluate_query(main.parse_query("sum by (version) (http_requests_total)"), proxy.db, now)
        by_version = {labels["version"]: value for labels, value in result}
        self.assertEqual(by_version, {"v1": 15.0, "v2": 6.0})
        bad = urllib.request.Request(url + "/admin/config", data=json.dumps({"stable": stable, "weight": 30}).encode(), method="POST")
        with self.assertRaises(urllib.error.HTTPError) as caught:
            main.NO_PROXY.open(bad, timeout=5)
        self.assertEqual(caught.exception.code, 400)
        target = main.ProxyTarget(url, canary)
        target.sync(main.Desired("a", {}, promoting=True), 0.0)
        self.assertEqual(proxy.config(), {"stable": canary, "canary": None, "weight": 0})

    def test_minirollout_run_promotes_a_good_version_and_aborts_a_bad_one_through_the_proxy(self):
        v1 = self.servers.start(main.AppServer(("127.0.0.1", 0), "v1"))
        v2 = self.servers.start(main.AppServer(("127.0.0.1", 0), "v2"))
        v3 = self.servers.start(main.AppServer(("127.0.0.1", 0), "v3", 0.5))
        proxy = main.ProxyServer(("127.0.0.1", 0), v1)
        url = self.servers.start(proxy)
        stop_sampler = main.start_sampler(proxy.sample, 0.2)
        load = main.LoadGenerator(url + "/", 80, 4, main.Registry())
        load.start()
        try:
            with tempfile.TemporaryDirectory() as tmp:
                analysis = load_fast_analysis(tmp)
                state = os.path.join(tmp, "state.json")
                common = ["--analysis", analysis, "--target", "proxy", "--proxy", url, "--state", state, "--prometheus-url", url, "--tick", "0.2", "--timeout", "20", "--json"]
                steps = [{"setWeight": 50}, {"analysis": {"templates": [{"templateName": "success-rate"}], "args": [{"name": "version", "value": "VERSION"}]}}]
                paths = {}
                for version in ("v1", "v2", "v3"):
                    doc = rollout(version, steps=json.loads(json.dumps(steps).replace("VERSION", version)))
                    paths[version] = os.path.join(tmp, f"rollout-{version}.json")
                    with open(paths[version], "w", encoding="utf-8") as handle:
                        json.dump(doc, handle)
                self.assertEqual(run_cli(["run", paths["v1"], "--canary-url", v1] + common)[0], 0)
                code, out, _ = run_cli(["run", paths["v2"], "--canary-url", v2] + common)
                self.assertEqual(code, 0, out)
                self.assertEqual(proxy.config(), {"stable": v2, "canary": None, "weight": 0})
                code, out, _ = run_cli(["run", paths["v3"], "--canary-url", v3] + common)
                self.assertEqual(code, 1, out)
                final = json.loads(out)
                self.assertTrue(final["message"].startswith("RolloutAborted: Rollout aborted update to revision 3"))
                measured = final["runs"][-1]["metrics"][0]["measurements"][0]["value"][0]
                self.assertTrue(0.3 <= measured <= 0.7, measured)
                self.assertEqual(proxy.config(), {"stable": v2, "canary": v3, "weight": 0})
        finally:
            load.stop.set()
            stop_sampler.set()


def load_fast_analysis(folder):
    doc = load(ANALYSIS)
    for metric in doc["spec"]["metrics"]:
        metric["interval"], metric["initialDelay"] = "500ms", "1s"
        metric["provider"]["prometheus"]["query"] = metric["provider"]["prometheus"]["query"].replace("[20s]", "[3s]")
    path = os.path.join(folder, "analysis-fast.json")
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(doc, handle)
    return path


class SimulatorTests(unittest.TestCase):
    def test_smaller_first_steps_expose_fewer_requests_to_a_bad_version(self):
        spec = main.parse_rollout(rollout())
        tpl = templates()
        args = main.default_args(spec)
        careful = main.simulate(main.dataclasses.replace(spec, steps=main.parse_plan("5%,analysis,50%,analysis", tpl, args)), tpl, rps=50, duration=300)
        reckless = main.simulate(main.dataclasses.replace(spec, steps=main.parse_plan("100", tpl, args)), tpl, rps=50, duration=300)
        self.assertEqual((careful.outcome, careful.at), ("aborted", 5.0))
        self.assertEqual(careful.new_requests, 12)
        self.assertEqual(reckless.outcome, "promoted")
        self.assertEqual(reckless.new_requests, 50 * 301)
        self.assertAlmostEqual(reckless.new_errors / reckless.new_requests, 0.2, places=2)

    def test_whole_pods_turn_ten_percent_into_a_quarter_of_the_traffic(self):
        spec = main.dataclasses.replace(main.parse_rollout(rollout()), replicas=3)
        tpl = templates()
        spec = main.dataclasses.replace(spec, steps=main.parse_plan("10%,analysis", tpl, main.default_args(spec)))
        exact = main.simulate(spec, tpl, rps=40, duration=60)
        pods = main.simulate(spec, tpl, rps=40, duration=60, by_replicas=True, ready_delay=4)
        self.assertEqual((exact.outcome, exact.at, exact.new_requests), ("aborted", 5.0, 20))
        self.assertEqual((pods.outcome, pods.at), ("aborted", 9.0))
        self.assertEqual(pods.new_requests, 50)


class CLITests(unittest.TestCase):
    def test_plan_prints_pod_counts_traffic_and_the_resolved_queries(self):
        code, out, _ = run_cli(["plan", ROLLOUT, "--analysis", ANALYSIS])
        self.assertEqual(code, 0)
        self.assertRegex(out, r"1\s+setWeight: 25\s+1\s+3\s+25%\s+25%")
        self.assertIn('query: sum(rate(http_requests_total{version="v2",code=~"2.."}[20s]))', out)
        code, out, _ = run_cli(["plan", ROLLOUT, "--analysis", ANALYSIS, "--json", "--replicas", "3"])
        self.assertEqual(json.loads(out)["steps"][0]["new_pods"], 1)

    def test_bad_input_exits_2_with_the_reason(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "r.json")
            with open(path, "w", encoding="utf-8") as handle:
                json.dump(rollout(steps=[{"setCanaryScale": {"weight": 10}}]), handle)
            code, _, err = run_cli(["plan", path, "--analysis", ANALYSIS])
            self.assertEqual(code, 2)
            self.assertIn("step 1: setCanaryScale is not implemented", err)
            code, _, err = run_cli(["plan", ROLLOUT])
            self.assertEqual(code, 2)
            self.assertIn("analysis template 'success-rate', which is not loaded", err)
            with open(path, "w", encoding="utf-8") as handle:
                handle.write("kind: Rollout\n")
            code, _, err = run_cli(["plan", path])
            self.assertEqual(code, 2)
            self.assertIn("is not JSON", err)

    def test_promote_and_abort_leave_requests_the_run_loop_consumes(self):
        c = controller(rollout(steps=[{"setWeight": 20}, {"pause": {}}]))
        drive(c, 3)
        with tempfile.TemporaryDirectory() as tmp:
            state = os.path.join(tmp, "state.json")
            self.assertEqual(run_cli(["abort", "--state", state])[0], 0)
            self.assertTrue(os.path.exists(state + ".abort"))
            main.apply_requests(c, state, 4.0)
            self.assertFalse(os.path.exists(state + ".abort"))
            self.assertEqual((c.status.phase, c.status.message), (main.DEGRADED, "RolloutAborted: Rollout aborted update to revision 2"))
            main.save_state(state, c.status)
            code, out, _ = run_cli(["status", "--state", state])
            self.assertEqual(code, 0)
            self.assertIn("phase Degraded", out)
            run_cli(["promote", "--state", state, "--full"])
            self.assertTrue(os.path.exists(state + ".promote-full"))

    def test_simulate_prints_one_row_per_plan(self):
        code, out, _ = run_cli(["simulate", ROLLOUT, "--analysis", ANALYSIS, "--plan", "100", "--duration", "60", "--json"])
        self.assertEqual(code, 0)
        rows = json.loads(out)
        self.assertEqual([r["outcome"] for r in rows], ["aborted", "promoted"])
        self.assertLess(rows[0]["new_requests"], rows[1]["new_requests"])


if __name__ == "__main__":
    unittest.main()
