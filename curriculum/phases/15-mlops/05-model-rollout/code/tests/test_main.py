import contextlib
import io
import json
import os
import random
import re
import sys
import tempfile
import threading
import unittest
import urllib.parse

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import main  # noqa: E402

FIXTURES = os.path.join(os.path.dirname(__file__), "..", "fixtures")
V1 = main.TRUE_WEIGHTS
V2 = (1.4, -2.1, 0.2)
V3 = (-1.5, -2.0, 0.25)


def fixture(name):
    with open(os.path.join(FIXTURES, name), encoding="utf-8") as handle:
        return json.load(handle)


def samples(version, count, latency=10.0, errors=0, good=None, role=main.SERVED, agree=None, start=0.0):
    out = []
    for i in range(count):
        out.append(main.Sample(start + i * 0.01, version, role, latency + (i % 7) * 0.5, i >= errors, f"{version}-{role}-{i}", f"u{i}",
                               None, None if agree is None else i >= count - agree, None if good is None else i < good))
    return out


class World:
    def __init__(self, behaviours, rps=50, users=400, seed=3):
        self.t = 1000.0
        self.clock_value = 0.0
        self.behaviours = behaviours
        self.examples = main.make_dataset()
        self.rng = random.Random(seed)
        self.rps, self.users = rps, users
        self.recorder = main.Recorder(clock=lambda: self.t)
        self.router = None
        self.served = []

    def post(self, url, body, headers):
        behaviour = self.behaviours.get(url[: -len("/predict")])
        if behaviour is None:
            raise main.UpstreamError(f"connection refused: {url}")
        status, payload, delay = behaviour.answer(body, headers.get("X-Request-Id", ""))
        self.clock_value += delay
        return status, payload

    def timer(self):
        return self.clock_value

    def make_router(self, primary):
        self.router = main.Router(main.RouteConfig(primary), self.recorder, post=self.post, timer=self.timer, synchronous_mirror=True)
        return self.router

    def request(self):
        example = self.examples[self.rng.randrange(len(self.examples))]
        user = f"u{self.rng.randrange(self.users)}"
        status, payload, headers = self.router.route({"id": example.id, "features": list(example.features)}, {"x-user-id": user})
        if status == 200:
            self.recorder.feedback(headers["X-Request-Id"], payload["label"] == example.label)
        self.served.append((user, headers["X-Model-Version"]))

    def sleep(self, seconds):
        count = int(round(seconds * self.rps))
        if count == 0:
            self.t += seconds
        for _ in range(count):
            self.t += 1.0 / self.rps
            self.request()


def backends():
    return {name: main.Backend(name, f"http://{name}.test") for name in ("v1", "v2", "v3", "v4")}


def behaviours(**overrides):
    base = {"v1": main.ModelBehaviour("v1", V1), "v2": main.ModelBehaviour("v2", V2), "v3": main.ModelBehaviour("v3", V3),
            "v4": main.ModelBehaviour("v4", V2, latency_ms=120.0)}
    base.update(overrides)
    return {f"http://{name}.test": behaviour for name, behaviour in base.items()}


class ModelTests(unittest.TestCase):
    def test_dataset_is_seeded_and_the_versions_differ_in_quality_not_in_shape(self):
        data = main.make_dataset()
        self.assertEqual(data, main.make_dataset())
        self.assertNotEqual(data, main.make_dataset(seed=8))
        self.assertGreaterEqual(main.accuracy(V1, data), 0.95)
        self.assertLess(main.accuracy(V1, data) - main.accuracy(V2, data), 0.03)
        self.assertLess(main.accuracy(V3, data), 0.7)

    def test_model_answers_are_deterministic_and_errors_are_injected_by_request_id(self):
        model = main.ModelBehaviour("v9", V1, latency_ms=4, jitter_ms=2, error_rate=0.25)
        answers = [model.answer({"features": [0.5, -0.5]}, f"r{i}") for i in range(400)]
        self.assertEqual(answers, [model.answer({"features": [0.5, -0.5]}, f"r{i}") for i in range(400)])
        errors = sum(1 for status, _, _ in answers if status == 500)
        self.assertTrue(70 <= errors <= 130, errors)
        self.assertTrue(all(0.004 <= delay <= 0.006 for _, _, delay in answers))
        self.assertEqual(model.answer({"features": [1, "x"]})[0], 400)
        self.assertEqual(model.answer({"features": [1, True]})[0], 400)

    def test_parse_weights_rejects_the_wrong_shape(self):
        self.assertEqual(main.parse_weights("1.5,-2,0.25"), (1.5, -2.0, 0.25))
        with self.assertRaisesRegex(ValueError, "three numbers"):
            main.parse_weights("1,2")
        with self.assertRaisesRegex(ValueError, "three numbers"):
            main.parse_weights("a,b,c")


class AssignmentTests(unittest.TestCase):
    def test_a_user_keeps_its_arm_and_raising_the_weight_only_adds_users(self):
        users = [f"u{i}" for i in range(5000)]
        at10 = {u for u in users if main.assign(u, "v2", 10) == main.CANARY}
        at50 = {u for u in users if main.assign(u, "v2", 50) == main.CANARY}
        self.assertTrue(at10 <= at50)
        self.assertTrue(400 <= len(at10) <= 600, len(at10))
        self.assertTrue(2300 <= len(at50) <= 2700, len(at50))
        self.assertEqual(at10, {u for u in users if main.assign(u, "v2", 10) == main.CANARY})
        self.assertFalse(any(main.assign(u, "v2", 0) == main.CANARY for u in users))
        self.assertTrue(all(main.assign(u, "v2", 100) == main.CANARY for u in users))

    def test_a_new_salt_picks_a_different_cohort(self):
        users = [f"u{i}" for i in range(2000)]
        first = {u for u in users if main.assign(u, "v2", 10) == main.CANARY}
        second = {u for u in users if main.assign(u, "v3", 10) == main.CANARY}
        self.assertLess(len(first & second), len(first) / 3)


class StatisticsTests(unittest.TestCase):
    def test_mann_whitney_and_proportion_tests_match_scipy(self):
        cases = fixture("scipy-reference.json")["cases"]
        for case in cases:
            with self.subTest(case.get("name", case["kind"])):
                if case["kind"] == "mannwhitney":
                    result = main.mann_whitney_greater(case["candidate"], case["primary"])
                    self.assertAlmostEqual(result.statistic, case["u"], places=9)
                    self.assertAlmostEqual(result.p_value, case["p"], delta=1e-9 * max(1.0, case["p"]))
                elif case["kind"] == "proportions":
                    result = main.two_proportion_greater(case["bad_candidate"], case["n_candidate"], case["bad_primary"], case["n_primary"])
                    self.assertAlmostEqual(result.z ** 2, case["chi2"], places=9)
                    self.assertAlmostEqual(2 * main.norm_sf(abs(result.z)), case["p_two_sided"], delta=1e-12 + 1e-9 * case["p_two_sided"])
                else:
                    self.assertAlmostEqual(main.norm_ppf(case["p"]), case["z"], places=9)
        self.assertGreaterEqual(len(cases), 10)

    def test_degenerate_inputs_give_no_evidence_instead_of_dividing_by_zero(self):
        self.assertEqual(main.mann_whitney_greater([5, 5, 5], [5, 5]).p_value, 1.0)
        self.assertEqual(main.two_proportion_greater(0, 100, 0, 300).p_value, 1.0)
        self.assertEqual(main.two_proportion_greater(100, 100, 300, 300).p_value, 1.0)
        with self.assertRaises(ValueError):
            main.mann_whitney_greater([], [1.0])
        with self.assertRaises(ValueError):
            main.one_proportion_greater(1, 10, 0.0)

    def test_one_proportion_test_and_sample_size(self):
        result = main.one_proportion_greater(30, 100, 0.10)
        self.assertAlmostEqual(result.z, 0.2 / (0.09 / 100) ** 0.5, places=9)
        self.assertLess(result.p_value, 1e-9)
        self.assertEqual(main.samples_needed(0.97, 0.10, 0.01, 0.8), 147)
        self.assertGreater(main.samples_needed(0.95, 0.05), main.samples_needed(0.95, 0.10))
        self.assertGreater(main.samples_needed(0.95, 0.10, alpha=0.001), main.samples_needed(0.95, 0.10, alpha=0.05))
        with self.assertRaises(ValueError):
            main.samples_needed(0.05, 0.10)

    def test_percentile_is_nearest_rank(self):
        values = sorted([float(v) for v in range(1, 101)])
        self.assertEqual(main.percentile(values, 50), 50.0)
        self.assertEqual(main.percentile(values, 95), 95.0)
        self.assertEqual(main.percentile([7.0], 95), 7.0)
        self.assertEqual(main.percentile([], 50), 0.0)


class AnalysisTests(unittest.TestCase):
    def test_too_few_samples_hold_whatever_the_numbers_say(self):
        result = main.analyze(samples("v1", 400, good=380), samples("v2", 40, good=10))
        self.assertEqual(result.decision, main.HOLD)
        self.assertIn("quality insufficient: 40 < 150 samples", result.reasons())

    def test_a_quality_drop_rolls_back_although_latency_and_errors_are_fine(self):
        result = main.analyze(samples("v1", 600, good=580), samples("v2", 200, good=130))
        verdicts = {c.metric: c.verdict for c in result.checks}
        self.assertEqual(verdicts, {"latency": main.PASS, "errors": main.PASS, "quality": main.FAIL})
        self.assertEqual(result.decision, main.ROLLBACK)

    def test_a_significant_but_small_difference_promotes(self):
        primary = samples("v1", 20000, good=19400, errors=0)
        candidate = samples("v2", 20000, good=19000, errors=0)
        quality = next(c for c in main.analyze(primary, candidate).checks if c.metric == "quality")
        self.assertLess(quality.p_value, 1e-6)
        self.assertEqual(quality.verdict, main.PASS)
        self.assertEqual(main.analyze(primary, candidate).decision, main.PROMOTE)

    def test_a_large_but_unproven_difference_is_suspect_and_holds(self):
        th = main.Thresholds(min_samples=20)
        result = main.analyze(samples("v1", 25, good=24), samples("v2", 25, good=21), th)
        quality = next(c for c in result.checks if c.metric == "quality")
        self.assertGreater(quality.effect, th.quality_drop)
        self.assertGreater(quality.p_value, th.alpha)
        self.assertEqual((quality.verdict, result.decision), (main.SUSPECT, main.HOLD))

    def test_slow_and_failing_candidates_roll_back(self):
        slow = main.analyze(samples("v1", 300, good=290), samples("v2", 300, latency=90.0, good=290))
        self.assertEqual({c.metric: c.verdict for c in slow.checks}["latency"], main.FAIL)
        failing = main.analyze(samples("v1", 300, errors=3, good=290), samples("v2", 300, errors=30, good=260))
        self.assertEqual({c.metric: c.verdict for c in failing.checks}["errors"], main.FAIL)
        self.assertEqual((slow.decision, failing.decision), (main.ROLLBACK, main.ROLLBACK))

    def test_shadow_mode_judges_disagreement_with_the_primary(self):
        primary = samples("v1", 300)
        agreeing = main.analyze(primary, samples("v2", 300, role=main.SHADOW, agree=294), mode=main.SHADOW)
        disagreeing = main.analyze(primary, samples("v3", 300, role=main.SHADOW, agree=190), mode=main.SHADOW)
        self.assertEqual(agreeing.decision, main.PROMOTE)
        self.assertEqual(disagreeing.decision, main.ROLLBACK)
        self.assertEqual(disagreeing.checks[-1].metric, "disagreement")


class RouterTests(unittest.TestCase):
    def setUp(self):
        self.b = backends()

    def test_shadow_answers_never_reach_the_caller(self):
        world = World(behaviours(v3=main.ModelBehaviour("v3", V3, error_rate=0.5)))
        router = world.make_router(self.b["v1"])
        examples = main.make_dataset()[:200]
        direct = [main.ModelBehaviour("v1", V1).answer({"features": list(e.features)})[1] for e in examples]
        router.configure(main.RouteConfig(self.b["v1"], shadow=self.b["v3"], shadow_percent=100))
        routed = [router.route({"features": list(e.features)}, {"x-user-id": f"u{i}"}) for i, e in enumerate(examples)]
        self.assertEqual([payload for _, payload, _ in routed], direct)
        self.assertEqual({headers["X-Model-Version"] for _, _, headers in routed}, {"v1"})
        shadow = world.recorder.select("v3", main.SHADOW)
        self.assertEqual(len(shadow), 200)
        self.assertGreater(sum(1 for s in shadow if not s.ok), 50)
        self.assertGreater(sum(1 for s in shadow if s.agree is False), 30)
        router.configure(main.RouteConfig(self.b["v1"], shadow=main.Backend("v9", "http://gone.test"), shadow_percent=100))
        status, payload, _ = router.route({"features": [1.0, 1.0]}, {})
        self.assertEqual((status, payload), (200, main.ModelBehaviour("v1", V1).answer({"features": [1.0, 1.0]})[1]))

    def test_a_hanging_shadow_does_not_delay_the_answer_and_a_full_queue_drops(self):
        release = threading.Event()
        started = threading.Event()

        def post(url, body, headers):
            if "v3" in url:
                started.set()
                release.wait(10)
            return 200, {"version": "v1", "label": 1}

        router = main.Router(main.RouteConfig(self.b["v1"], shadow=self.b["v3"], shadow_percent=100), post=post, mirror_workers=1, queue_size=2)
        try:
            results = [router.route({"features": [0, 0]}, {"x-request-id": f"r{i}"}) for i in range(5)]
            self.assertTrue(started.wait(5))
            self.assertEqual([status for status, _, _ in results], [200] * 5)
            self.assertFalse(release.is_set())
            self.assertGreaterEqual(router.dropped, 2)
        finally:
            release.set()
            router.close()

    def test_canary_assignment_is_sticky_per_user(self):
        world = World(behaviours())
        router = world.make_router(self.b["v1"])
        router.configure(main.RouteConfig(self.b["v1"], canary=self.b["v2"], weight=30))
        for _ in range(6):
            for user in range(300):
                router.route({"features": [0.1, 0.2]}, {"x-user-id": f"u{user}"})
        seen = {}
        for s in world.recorder.select(role=main.SERVED):
            seen.setdefault(s.user, set()).add(s.version)
        self.assertTrue(all(len(versions) == 1 for versions in seen.values()))
        canary_users = {u for u, versions in seen.items() if versions == {"v2"}}
        self.assertEqual(canary_users, {f"u{i}" for i in range(300) if main.assign(f"u{i}", "v2", 30) == main.CANARY})

    def test_route_config_validation(self):
        with self.assertRaisesRegex(ValueError, "between 0 and 100"):
            main.RouteConfig.from_json({"primary": self.b["v1"].to_json(), "canary": self.b["v2"].to_json(), "weight": 150})
        with self.assertRaisesRegex(ValueError, "needs a canary"):
            main.RouteConfig.from_json({"primary": self.b["v1"].to_json(), "weight": 10})
        with self.assertRaisesRegex(ValueError, "different version"):
            main.RouteConfig.from_json({"primary": self.b["v1"].to_json(), "shadow": self.b["v1"].to_json(), "shadow_percent": 5})
        with self.assertRaisesRegex(ValueError, "primary"):
            main.RouteConfig.from_json({"canary": self.b["v2"].to_json()})
        config = main.RouteConfig(self.b["v1"], canary=self.b["v2"], weight=10, shadow=self.b["v3"], shadow_percent=50)
        self.assertEqual(main.RouteConfig.from_json(config.to_json()), config)
        with self.assertRaisesRegex(ValueError, "NAME=URL"):
            main.Backend.parse("v1")

    def test_recorder_windows_feedback_and_the_compare_log(self):
        now = [100.0]
        recorder = main.Recorder(clock=lambda: now[0])
        for i in range(10):
            recorder.add(main.Sample(91.0 + i, "v1", main.SERVED, 10.0 + i, True, f"r{i}", f"u{i}"))
        self.assertTrue(recorder.feedback("r9", True))
        self.assertFalse(recorder.feedback("nope", True))
        self.assertEqual(len(recorder.select("v1", main.SERVED, 95.0, 100.0)), 5)
        result = recorder.compare("v1", "v2", 5.0, step="1")
        self.assertEqual((result["primary"]["requests"], result["primary"]["feedback"], result["candidate"]["requests"]), (5, 1, 0))
        self.assertEqual(recorder.logged_queries(), [{"since": 95.0, "until": 100.0, "window": 5.0, "primary": "v1", "candidate": "v2", "role": "served", "step": "1"}])


class PlanTests(unittest.TestCase):
    def test_the_argo_rollout_is_a_plan(self):
        steps = main.parse_plan(fixture("rollout.json"))
        self.assertEqual([s.label for s in steps], ["setWeight 25", "analysis (model-quality)", "setWeight 50", "analysis (model-quality)"])
        shadow = main.parse_plan(fixture("plan-shadow.json"))
        self.assertEqual([(s.kind, s.value) for s in shadow[:4]], [("setMirrorRoute", 100.0), ("analysis", shadow[1].value), ("setMirrorRoute", None), ("setWeight", 25.0)])

    def test_bad_steps_name_their_number(self):
        with self.assertRaisesRegex(main.PlanError, "step 2: minirollout does not implement setCanaryScale"):
            main.parse_plan([{"setWeight": 5}, {"setCanaryScale": {"replicas": 1}}])
        with self.assertRaisesRegex(main.PlanError, "step 1: minirollout needs pause.duration"):
            main.parse_plan([{"pause": {}}])
        with self.assertRaisesRegex(main.PlanError, "step 1: setWeight"):
            main.parse_plan([{"setWeight": 120}])
        with self.assertRaisesRegex(main.PlanError, "no steps"):
            main.parse_plan({"steps": []})
        self.assertEqual(main.parse_duration("2m"), 120.0)
        self.assertEqual(main.parse_duration("250ms"), 0.25)

    def test_describe_plan_counts_time_to_min_samples_and_warns(self):
        report = main.describe_plan(main.parse_plan(fixture("plan-shadow.json")), 60.0, main.Thresholds(), 0.95, 20.0)
        analysis = [r for r in report["rows"] if "mode" in r]
        self.assertEqual([(r["mode"], r["candidate_rps"], r["seconds_to_min_samples"]) for r in analysis],
                         [("shadow", 60.0, 2.5), ("served", 15.0, 10.0), ("served", 30.0, 5.0)])
        self.assertEqual(report["samples_needed"], 180)
        self.assertTrue(any("below the 180 per side" in w for w in report["warnings"]))
        slow = main.describe_plan(main.parse_plan([{"setWeight": 5}, {"analysis": {}}]), 40.0, main.Thresholds(min_samples=200), 0.97, 20.0)
        self.assertTrue(any("can never decide" in w for w in slow["warnings"]))

    def test_the_analysis_template_asks_compare_for_fields_it_returns(self):
        template = fixture("analysis-template.json")
        fields = set(main.summarize([]))
        recorder = main.Recorder(clock=lambda: 50.0)
        for metric in template["spec"]["metrics"]:
            url = urllib.parse.urlsplit(metric["provider"]["web"]["url"])
            query = {k: v[-1] for k, v in urllib.parse.parse_qs(url.query).items()}
            self.assertEqual(url.path, "/compare")
            self.assertEqual(set(query), {"primary", "candidate", "window", "step"})
            used = set(re.findall(r"result\.(?:candidate|primary)\.(\w+)", metric["successCondition"] + metric["failureCondition"]))
            self.assertTrue(used and used <= fields, used - fields)
        response = recorder.compare("v1", "v2", 20.0)
        self.assertTrue(fields <= set(response["candidate"]) and fields <= set(response["primary"]))


class ControllerTests(unittest.TestCase):
    def run_rollout(self, plan, candidate, world=None, registry=None, timeout=60.0):
        b = backends()
        world = world or World(behaviours())
        world.make_router(b["v1"])
        world.sleep(5)
        log = []
        controller = main.Controller(main.parse_plan(plan), main.LocalClient(world.router), b[candidate], window=20.0, tick=1.0, analysis_timeout=timeout,
                                     registry=registry, model="churn", registry_version=candidate[1:], clock=lambda: world.t, sleep=world.sleep, log=log.append)
        return controller.run(), world, log

    def test_a_good_version_is_promoted_and_the_alias_moves(self):
        with tempfile.TemporaryDirectory() as tmp:
            registry = main.FileRegistry(os.path.join(tmp, "registry.json"))
            registry.set_alias("churn", "champion", "1")
            state, world, log = self.run_rollout(fixture("plan-shadow.json"), "v2", registry=registry)
            self.assertEqual(state["phase"], main.PROMOTED, log)
            self.assertEqual(world.router.snapshot(), main.RouteConfig(backends()["v2"]))
            self.assertEqual(registry.get_alias("churn", "champion"), "2")
            self.assertEqual(registry.load()["models"]["churn"]["versions"]["2"]["tags"], {"rollout": "promoted"})
            self.assertEqual([a["mode"] for a in state["analyses"]], ["shadow", "served", "served"])

    def test_shadow_catches_a_bad_model_before_any_user_sees_it(self):
        state, world, log = self.run_rollout(fixture("plan-shadow.json"), "v3")
        self.assertEqual((state["phase"], state["failed_step"]), (main.ROLLED_BACK, 1), log)
        self.assertEqual(state["exposure"]["served_requests"], 0)
        self.assertGreater(state["exposure"]["shadow_requests"], 0)
        self.assertNotIn("v3", {version for _, version in world.served})
        self.assertEqual(world.router.snapshot(), main.RouteConfig(backends()["v1"]))
        self.assertIn("disagreement fail", " ".join(state["analyses"][-1]["reasons"]))

    def test_without_shadow_the_canary_catches_it_through_feedback_at_the_same_step_as_argo(self):
        state, world, log = self.run_rollout(fixture("rollout.json"), "v3")
        self.assertEqual((state["phase"], state["failed_step"]), (main.ROLLED_BACK, 1), log)
        self.assertGreater(state["exposure"]["served_requests"], 0)
        reasons = " ".join(state["analyses"][-1]["reasons"])
        self.assertIn("quality fail", reasons)
        self.assertNotIn("latency fail", reasons)
        self.assertNotIn("errors fail", reasons)

    def test_a_slow_version_is_rolled_back_on_latency(self):
        state, _, log = self.run_rollout(fixture("rollout.json"), "v4")
        self.assertEqual(state["phase"], main.ROLLED_BACK, log)
        self.assertIn("latency fail", " ".join(state["analyses"][-1]["reasons"]))

    def test_no_traffic_means_hold_and_traffic_is_left_alone(self):
        world = World(behaviours(), rps=50)
        b = backends()
        world.make_router(b["v1"])
        world.rps = 0
        controller = main.Controller(main.parse_plan(fixture("rollout.json")), main.LocalClient(world.router), b["v2"], analysis_timeout=10.0,
                                     clock=lambda: world.t, sleep=world.sleep, log=lambda line: None)
        state = controller.run()
        self.assertEqual(state["phase"], main.HELD)
        self.assertEqual(world.router.snapshot().weight, 25.0)

    def test_rolling_out_the_primary_is_refused(self):
        world = World(behaviours())
        world.make_router(backends()["v1"])
        with self.assertRaisesRegex(main.PlanError, "already the primary"):
            main.Controller(main.parse_plan(fixture("rollout.json")), main.LocalClient(world.router), backends()["v1"]).run()


class HttpTests(unittest.TestCase):
    def setUp(self):
        self.servers = []
        self.routers = []

    def tearDown(self):
        for router in self.routers:
            router.close()
        for server in self.servers:
            server.shutdown()
            server.server_close()

    def serve(self, app):
        server = main.start_server(app)
        self.servers.append(server)
        return main.server_url(server)

    def test_router_over_real_sockets_with_a_shadow_and_a_compare_endpoint(self):
        v1 = self.serve(main.model_app(main.ModelBehaviour("v1", V1, latency_ms=0, jitter_ms=1)))
        v3 = self.serve(main.model_app(main.ModelBehaviour("v3", V3, latency_ms=0, jitter_ms=1)))
        router = main.Router(main.RouteConfig(main.Backend("v1", v1)))
        self.routers.append(router)
        url = self.serve(main.router_app(router))
        status, body, _ = main.http_json("POST", f"{url}/admin/config", {"primary": {"name": "v1", "url": v1}, "shadow": {"name": "v3", "url": v3}, "shadow_percent": 100})
        self.assertEqual((status, body["shadow"]["name"]), (200, "v3"))
        examples = main.make_dataset()[:30]
        for i, example in enumerate(examples):
            status, body, headers = main.http_json("POST", f"{url}/predict", {"features": list(example.features)}, {"X-User-Id": f"u{i}", "X-Request-Id": f"t{i}"})
            self.assertEqual((status, headers["x-model-version"], body["label"]), (200, "v1", main.predict(V1, example.features)[0]))
            self.assertEqual(main.http_json("POST", f"{url}/feedback", {"request_id": f"t{i}", "good": body["label"] == example.label})[0], 200)
        router.drain()
        status, compare, _ = main.http_json("GET", f"{url}/compare?primary=v1&candidate=v3&role=shadow&window=60&step=0")
        self.assertEqual((status, compare["primary"]["requests"], compare["candidate"]["requests"], compare["primary"]["feedback"]), (200, 30, 30, 30))
        self.assertLess(compare["candidate"]["agreement_rate"], 0.9)
        fetched = main.fetch_samples(url, "v3", main.SHADOW)
        self.assertEqual(len(fetched), 30)
        self.assertEqual(main.http_json("POST", f"{url}/feedback", {"request_id": "nope", "good": True})[0], 404)
        self.assertEqual(main.http_json("POST", f"{url}/admin/config", {"primary": {"name": "v1", "url": v1}, "weight": 5})[0], 400)
        self.assertEqual(main.http_json("GET", f"{url}/compare?primary=v1")[0], 400)
        with self.assertRaises(main.UpstreamError):
            main.http_json("GET", "http://127.0.0.1:9/healthz", timeout=2)

    def test_mlflow_registry_speaks_the_rest_api(self):
        calls = []
        aliases = {}

        def app(method, path, query, body, headers):
            calls.append((method, path, body or query))
            if path == "/api/2.0/mlflow/registered-models/alias" and method == "POST":
                if body["name"] != "churn":
                    return 404, {"error_code": "RESOURCE_DOES_NOT_EXIST", "message": f"Registered Model with name={body['name']} not found"}, {}
                aliases[body["alias"]] = body["version"]
                return 200, {}, {}
            if path == "/api/2.0/mlflow/registered-models/alias" and method == "GET":
                if query["alias"] not in aliases:
                    return 404, {"error_code": "RESOURCE_DOES_NOT_EXIST"}, {}
                return 200, {"model_version": {"name": query["name"], "version": aliases[query["alias"]], "aliases": [query["alias"]]}}, {}
            if path == "/api/2.0/mlflow/model-versions/set-tag":
                return 200, {}, {}
            return 404, {}, {}

        registry = main.registry_from(self.serve(app))
        self.assertIsInstance(registry, main.MlflowRegistry)
        self.assertIsNone(registry.get_alias("churn", "champion"))
        registry.set_alias("churn", "champion", "2")
        registry.set_tag("churn", "2", "rollout", "promoted")
        self.assertEqual(registry.get_alias("churn", "champion"), "2")
        self.assertIn(("POST", "/api/2.0/mlflow/model-versions/set-tag", {"name": "churn", "version": "2", "key": "rollout", "value": "promoted"}), calls)
        with self.assertRaisesRegex(main.RegistryError, "HTTP 404 RESOURCE_DOES_NOT_EXIST"):
            registry.set_alias("other", "champion", "1")


class CliTests(unittest.TestCase):
    def run_main(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = main.main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def test_analyze_exit_codes_follow_the_decision(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "samples.json")
            for candidate, expected in ((samples("v2", 300, good=290), 0), (samples("v2", 300, good=180), 1), (samples("v2", 50, good=50), 3)):
                with open(path, "w", encoding="utf-8") as handle:
                    json.dump({"samples": [s.to_json() for s in samples("v1", 300, good=291) + candidate]}, handle)
                code, out, _ = self.run_main("analyze", "--samples", path, "--primary", "v1", "--candidate", "v2", "--window", "1000", "--json")
                self.assertEqual(code, expected, out)
                self.assertEqual(json.loads(out)["decision"], {0: "promote", 1: "rollback", 3: "hold"}[expected])

    def test_plan_assign_and_bad_input(self):
        code, out, _ = self.run_main("plan", os.path.join(FIXTURES, "rollout.json"), "--rps", "40", "--json")
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["rows"][1]["seconds_to_min_samples"], 15.0)
        code, out, _ = self.run_main("assign", "--weight", "30", "--salt", "v2", "--users", "1000", "--json")
        self.assertEqual(json.loads(out)["canary"], sum(main.assign(f"u{i}", "v2", 30) == main.CANARY for i in range(1000)))
        code, _, err = self.run_main("plan", os.path.join(FIXTURES, "missing.json"))
        self.assertEqual(code, 2)
        self.assertIn("cannot read", err)
        code, _, err = self.run_main("run", os.path.join(FIXTURES, "rollout.json"), "--router", "http://127.0.0.1:9", "--candidate", "v2")
        self.assertEqual(code, 2)
        self.assertIn("NAME=URL", err)

    def test_replay_reanalyses_logged_windows(self):
        primary = samples("v1", 300, good=291, start=10.0)
        good = samples("v2", 200, good=192, start=10.0)
        bad = samples("v3", 200, good=120, start=20.0)
        queries = [{"since": 9.0, "until": 13.5, "primary": "v1", "candidate": "v2", "role": "served", "step": "1"},
                   {"since": 19.0, "until": 23.5, "primary": "v1", "candidate": "v3", "role": "served", "step": "1"},
                   {"since": 9.0, "until": 9.5, "primary": "v1", "candidate": "v2", "role": "served", "step": "3"}]
        rows = main.replay(queries, primary + good + bad, main.Thresholds())
        self.assertEqual([r["decision"] for r in rows], [main.PROMOTE, main.HOLD, main.HOLD])
        bad_primary = samples("v1", 300, good=291, start=20.0)
        self.assertEqual(main.replay(queries[1:2], bad_primary + bad, main.Thresholds())[0]["decision"], main.ROLLBACK)


if __name__ == "__main__":
    unittest.main()
