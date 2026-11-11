import contextlib
import io
import json
import os
import socket
import sys
import tempfile
import threading
import unittest
import xml.etree.ElementTree as ET

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import main  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
CODE_DIR = os.path.dirname(HERE)
SUITE = os.path.join(CODE_DIR, "suites", "ops-assistant.json")
ANSWERS = os.path.join(CODE_DIR, "fixtures", "stub-answers.json")
TRAIN = os.path.join(CODE_DIR, "fixtures", "train-sample.jsonl")

BASELINE_FAILS = {"gib-to-mib", "slo-downtime", "cron-every-15", "pod-status-json", "disk-full-runbook", "p95-definition", "backup-rpo"}


def minimal_suite(**overrides):
    data = {"name": "t", "cases": [{"id": "a", "input": "q?", "assert": [{"type": "contains", "value": "x"}]}]}
    data.update(overrides)
    return data


def results_doc(model, outcomes, critical=(), summary=None, samples=1, digest="same"):
    cases = [{"id": case_id, "passed": ok, "critical": case_id in critical, "samples": [], "pass_at": {"1": 1.0 if ok else 0.0}} for case_id, ok in outcomes.items()]
    return {"model": model, "suite_sha256": digest, "samples": samples, "cases": cases, "summary": summary or {"errors": 0, "latency_p95_ms": 10.0, "mean_cost_usd": 0.0001}}


def paired(n_both, n_regressed, n_improved, n_neither):
    base, cand = {}, {}
    index = 0
    for count, b, c in ((n_both, True, True), (n_regressed, True, False), (n_improved, False, True), (n_neither, False, False)):
        for _ in range(count):
            base[f"c{index:02d}"], cand[f"c{index:02d}"] = b, c
            index += 1
    return base, cand


def free_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class SuiteParsing(unittest.TestCase):
    def test_unknown_assertion_type_names_the_case(self):
        data = minimal_suite(cases=[{"id": "port", "input": "q", "assert": [{"type": "similar", "value": "x"}]}])
        with self.assertRaisesRegex(main.SuiteError, r"case 'port' assert\[0\]: unknown assertion type 'similar'"):
            main.parse_suite(data, "s.json")

    def test_duplicate_ids_bad_regex_and_template_without_input_are_refused(self):
        case = {"id": "a", "input": "q", "assert": [{"type": "contains", "value": "x"}]}
        with self.assertRaisesRegex(main.SuiteError, "duplicate id"):
            main.parse_suite(minimal_suite(cases=[case, dict(case)]))
        with self.assertRaisesRegex(main.SuiteError, "bad regex"):
            main.parse_suite(minimal_suite(cases=[dict(case, **{"assert": [{"type": "regex", "value": "("}]})]))
        with self.assertRaisesRegex(main.SuiteError, "must contain"):
            main.parse_suite(minimal_suite(template="no placeholder"))
        with self.assertRaisesRegex(main.SuiteError, "unknown gate keys"):
            main.parse_suite(minimal_suite(gate={"alpah": 0.05}))

    def test_shipped_suite_uses_every_assertion_type_and_renders_the_template(self):
        suite = main.load_suite(SUITE)
        self.assertEqual(len(suite.cases), 20)
        self.assertEqual({a.type for c in suite.cases for a in c.assertions}, set(main.ASSERTION_TYPES))
        self.assertEqual(sorted(c.id for c in suite.cases if c.critical), ["delete-all-namespaces", "leaked-key", "redact-email"])
        prompt = main.render_prompt(suite.template, suite.cases[0])
        self.assertTrue(prompt.startswith("You are the on-call assistant"))
        self.assertTrue(prompt.endswith("Question: " + suite.cases[0].input))
        self.assertEqual(len(suite.digest), 64)


class Assertions(unittest.TestCase):
    def check(self, kind, value, output, **kw):
        return main.check_assertion(main.Assertion(kind, value, **kw), output)

    def test_equals_is_exact_and_contains_is_case_sensitive(self):
        self.assertTrue(self.check("equals", "6443", "6443").passed)
        self.assertFalse(self.check("equals", "6443", "6443\n").passed)
        self.assertFalse(self.check("equals", "AAAA", "AAAA record").passed)
        self.assertFalse(self.check("contains", "Service Unavailable", "service unavailable").passed)
        self.assertTrue(self.check("icontains", "Service Unavailable", "503 service unavailable").passed)

    def test_regex_searches_and_not_contains_inverts(self):
        self.assertTrue(self.check("regex", r"kubectl rollout undo (deployment/|deploy/)api", "run kubectl rollout undo deploy/api now").passed)
        self.assertFalse(self.check("regex", r"^\*/15 \* \* \* \*$", "0,15,30,45 * * * *").passed)
        result = self.check("not-contains", "--all", "kubectl delete ns --all")
        self.assertFalse(result.passed)
        self.assertIn("must not contain", result.detail)

    def test_json_schema_subset(self):
        schema = {"type": "object", "required": ["name", "restarts"], "additionalProperties": False,
                  "properties": {"name": {"type": "string", "const": "web-0"}, "restarts": {"type": "integer", "minimum": 0}, "phase": {"enum": ["Running"]}}}
        self.assertTrue(self.check("json-schema", schema, '{"name": "web-0", "restarts": 3}').passed)
        self.assertTrue(self.check("json-schema", schema, '{"name": "web-0", "restarts": 3.0}').passed)
        fenced = self.check("json-schema", schema, '```json\n{"name": "web-0", "restarts": 3}\n```')
        self.assertFalse(fenced.passed)
        self.assertIn("not JSON", fenced.detail)
        errors = main.validate_schema({"name": "web-1", "restarts": True, "phase": "Done", "extra": 1}, schema)
        self.assertIn("$.name: must equal 'web-0'", errors)
        self.assertIn("$.restarts: expected integer, got boolean", errors)
        self.assertIn("$.phase: 'Done' is not one of ['Running']", errors)
        self.assertIn("$: unexpected key 'extra'", errors)
        self.assertEqual(main.validate_schema({}, schema), ["$: missing required key 'name'", "$: missing required key 'restarts'"])

    def test_numeric_reads_the_first_number_within_tolerance(self):
        self.assertTrue(self.check("numeric", 43.2, "43.2 minutes (0.1% of 43200)", tolerance=0.1).passed)
        self.assertTrue(self.check("numeric", 43200, "43,200 minutes").passed)
        self.assertFalse(self.check("numeric", 1536, "1.5 GiB is 1536 MiB").passed)
        self.assertEqual(self.check("numeric", 6, "about six hours").detail, "no number in output, want 6")
        self.assertEqual(main.first_number("-3.5 degrees"), -3.5)

    def test_keyword_judge_and_rubric_without_a_judge(self):
        rubric = "Says to 'revoke the key' and 'rotate'."
        good = main.keyword_judge("Revoke the key first, then rotate it.", rubric)
        self.assertEqual((good.passed, good.score), (True, 1.0))
        half = main.keyword_judge("Rotate it.", rubric)
        self.assertEqual((half.passed, half.score), (False, 0.5))
        self.assertIn("missing 'revoke the key'", half.reason)
        self.assertFalse(main.keyword_judge("anything", "no quotes here").passed)
        self.assertEqual(self.check("rubric", rubric, "Revoke the key").detail, "no judge configured")

    def test_judge_prompt_round_trips_and_chatty_judges_fail(self):
        prompt = main.render_judge_prompt("line one\nline two", "Says 'x'.")
        self.assertEqual(main.split_judge_prompt(prompt), ("line one\nline two", "Says 'x'."))
        self.assertIsNone(main.split_judge_prompt("no tags"))
        verdict = main.parse_judge_text('Sure! Here it is: {"reason": "fine", "pass": true, "score": 0.9} hope that helps')
        self.assertEqual((verdict.passed, verdict.score, verdict.reason), (True, 0.9, "fine"))
        for text in ("I think it passes.", '{"pass": "yes"}', '{"pass": true, "score": "high"}'):
            self.assertEqual(main.parse_judge_text(text).reason, "judge did not return {reason, pass, score}", text)


class Statistics(unittest.TestCase):
    def test_pass_at_k_is_the_unbiased_estimator(self):
        self.assertAlmostEqual(main.pass_at_k(5, 3, 1), 0.6)
        self.assertEqual(main.pass_at_k(5, 3, 5), 1.0)
        self.assertAlmostEqual(main.pass_at_k(5, 1, 2), 0.4)
        self.assertEqual(main.pass_at_k(5, 0, 5), 0.0)
        with self.assertRaises(ValueError):
            main.pass_at_k(3, 1, 4)

    def test_wilson_interval_reference_values(self):
        low, high = main.wilson_interval(13, 20)
        self.assertAlmostEqual(low, 0.4328542766852363, places=12)
        self.assertAlmostEqual(high, 0.8188081758989179, places=12)
        self.assertEqual(main.wilson_interval(0, 20)[0], 0.0)
        self.assertAlmostEqual(main.wilson_interval(0, 20)[1], 0.16112515805281935, places=12)
        self.assertEqual(main.wilson_interval(20, 20)[1], 1.0)
        self.assertEqual(main.wilson_interval(0, 0), (0.0, 1.0))
        narrow, wide = main.wilson_interval(65, 100), main.wilson_interval(13, 20)
        self.assertLess(narrow[1] - narrow[0], wide[1] - wide[0])

    def test_mcnemar_exact_reference_values(self):
        self.assertEqual(main.mcnemar_exact(8, 1), 0.0390625)
        self.assertEqual(main.mcnemar_exact(1, 8), 0.0390625)
        self.assertEqual(main.mcnemar_exact(0, 6), 0.03125)
        self.assertEqual(main.mcnemar_exact(5, 0), 0.0625)
        self.assertEqual(main.mcnemar_exact(3, 3), 1.0)
        self.assertEqual(main.mcnemar_exact(0, 0), 1.0)
        self.assertEqual(main.mcnemar_exact(10, 4), 0.1795654296875)
        self.assertEqual(main.min_discordant_for(0.05), 6)
        self.assertEqual(main.min_discordant_for(0.01), 8)

    def test_percentile_is_nearest_rank(self):
        values = list(range(1, 21))
        self.assertEqual(main.percentile(values, 95), 19.0)
        self.assertEqual(main.percentile(values, 50), 10.0)
        self.assertEqual(main.percentile([], 95), 0.0)


class Gate(unittest.TestCase):
    policy = main.GatePolicy(alpha=0.05, min_effect=0.1)

    def test_significant_regression_is_rejected_and_critical_cases_named(self):
        base, cand = paired(5, 8, 1, 6)
        result = main.gate(results_doc("v1", base), results_doc("v2", cand, critical={"c05"}), self.policy)
        self.assertFalse(result.passed)
        self.assertEqual(result.verdict, "regressed")
        self.assertEqual(result.stats["p_value"], 0.0390625)
        self.assertAlmostEqual(result.stats["effect"], -0.35)
        self.assertIn("McNemar exact p = 0.03906 < 0.05", result.reasons[0])
        self.assertEqual(result.reasons[1], "critical case(s) regressed: c05")

    def test_improvement_passes_and_require_improvement_blocks_a_tie(self):
        base, cand = paired(13, 0, 6, 1)
        result = main.gate(results_doc("v1", base), results_doc("v2", cand), self.policy)
        self.assertEqual((result.passed, result.verdict), (True, "improved"))
        base, cand = paired(10, 2, 2, 6)
        strict = main.GatePolicy(require_improvement=True)
        result = main.gate(results_doc("v1", base), results_doc("v2", cand), strict)
        self.assertEqual(result.verdict, "no significant difference")
        self.assertFalse(result.passed)
        self.assertIn("an improvement is required", result.reasons[0])

    def test_a_small_drop_passes_with_a_note_that_the_suite_is_too_small(self):
        base, cand = paired(14, 3, 0, 3)
        result = main.gate(results_doc("v1", base), results_doc("v2", cand), self.policy)
        self.assertTrue(result.passed)
        self.assertEqual(result.stats["p_value"], 0.25)
        self.assertIn("cannot tell that from noise", result.notes[0])
        self.assertIn("fewer than 6", result.notes[1])

    def test_a_tiny_but_significant_effect_is_not_a_regression(self):
        base, cand = paired(900, 30, 10, 60)
        result = main.gate(results_doc("v1", base), results_doc("v2", cand), self.policy)
        self.assertLess(result.stats["p_value"], 0.05)
        self.assertEqual((result.verdict, result.passed), ("no significant difference", True))

    def test_budgets_missing_cases_and_errors_are_reasons(self):
        base, cand = paired(10, 0, 0, 0)
        del cand["c09"]
        summary = {"errors": 2, "latency_p95_ms": 2500.0, "mean_cost_usd": 0.002}
        policy = main.GatePolicy(max_p95_latency_ms=2000, max_mean_cost_usd=0.0005)
        result = main.gate(results_doc("v1", base), results_doc("v2", cand, summary=summary, digest="other"), policy)
        self.assertFalse(result.passed)
        joined = " | ".join(result.reasons)
        self.assertIn("1 baseline case(s) missing from the candidate run: c09", joined)
        self.assertIn("2 provider error(s)", joined)
        self.assertIn("p95 latency 2500 ms over the budget of 2000 ms", joined)
        self.assertIn("mean cost $0.002000 per call over the budget of $0.000500", joined)
        self.assertIn("different suite files", " ".join(result.notes))


class Contamination(unittest.TestCase):
    def test_verbatim_cases_are_flagged_and_a_paraphrase_is_not(self):
        suite = main.load_suite(SUITE)
        report = {row["id"]: row for row in main.contamination(suite, main.training_texts(TRAIN), n=8, threshold=0.5)}
        self.assertEqual(sorted(i for i, r in report.items() if r["flagged"]), ["redact-email", "slo-downtime"])
        self.assertEqual(report["slo-downtime"]["overlap"], 1.0)
        self.assertEqual(report["k8s-apiserver-port"]["matched"], 0)
        self.assertTrue(0 < report["incident-json"]["overlap"] < 0.5)

    def test_short_texts_use_the_whole_text_as_one_gram(self):
        self.assertEqual(main.ngrams(["what", "is", "dns"], 8), {("what", "is", "dns")})
        self.assertEqual(len(main.ngrams(main.normalize_words("a b c d e f g h i"), 8)), 2)
        self.assertEqual(main.ngrams([], 8), set())
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "bad.jsonl")
            with open(path, "w") as handle:
                handle.write('{"text": "ok"}\nnot json\n')
            with self.assertRaisesRegex(main.SuiteError, "bad.jsonl:2: not JSON"):
                main.training_texts(path)


class StubAndRunner(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.suite = main.load_suite(SUITE)
        with open(ANSWERS) as handle:
            cls.answers = json.load(handle)
        cls.server = main.make_stub_server(cls.suite, cls.answers)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base_url = "http://127.0.0.1:%d/v1" % cls.server.server_address[1]

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(5)

    def provider(self, model, **kw):
        return main.OpenAIChatProvider(self.base_url, model, **kw)

    def test_stub_models_inherit_and_samples_follow_the_seed(self):
        models, judges = main.load_stub_models(self.answers)
        self.assertEqual(judges, ["judge-v1"])
        self.assertEqual(models["ops-v2-large"].answers, models["ops-v2-improved"].answers)
        self.assertEqual(models["ops-v2-regressed"].answers["cpu-millicores"], models["ops-v1"].answers["cpu-millicores"])
        case = next(c for c in self.suite.cases if c.id == "crashloop-first-step")
        prompt = main.render_prompt(self.suite.template, case)
        answers = [main.stub_answer(self.suite, models["ops-v1"], prompt, seed) for seed in range(6)]
        self.assertEqual(answers[0], answers[5])
        self.assertEqual(answers[0], "kubectl logs web-0 --previous")
        self.assertEqual(main.stub_answer(self.suite, models["ops-v1"], "unrelated", 0), main.FALLBACK_ANSWER)
        with self.assertRaisesRegex(main.SuiteError, "inherit loop"):
            main.load_stub_models({"models": {"a": {"inherit": "b"}, "b": {"inherit": "a"}}})

    def test_baseline_run_over_http_with_the_model_judge(self):
        judge = main.model_judge(self.provider("judge-v1"))
        results = main.run_suite(self.suite, self.provider("ops-v1"), judge=judge, judge_name="judge-v1")
        failed = {c["id"] for c in results["cases"] if not c["passed"]}
        self.assertEqual(failed, BASELINE_FAILS)
        summary = results["summary"]
        self.assertEqual((summary["passed"], summary["cases"], summary["errors"]), (13, 20, 0))
        self.assertGreater(summary["mean_cost_usd"], 0)
        disk = next(c for c in results["cases"] if c["id"] == "disk-full-runbook")
        self.assertIn("missing 'not delete database files'", disk["samples"][0]["assertions"][0]["detail"])

    def test_gates_on_the_planted_candidates(self):
        judge = main.model_judge(self.provider("judge-v1"))
        runs = {m: main.run_suite(self.suite, self.provider(m), judge=judge) for m in ("ops-v1", "ops-v2-regressed", "ops-v2-improved", "ops-v2-large")}
        verdicts = {m: main.gate(runs["ops-v1"], runs[m], self.suite.gate) for m in runs if m != "ops-v1"}
        self.assertEqual((verdicts["ops-v2-regressed"].passed, verdicts["ops-v2-regressed"].verdict), (False, "regressed"))
        self.assertEqual(verdicts["ops-v2-regressed"].stats["improved"], ["gib-to-mib"])
        self.assertEqual((verdicts["ops-v2-improved"].passed, verdicts["ops-v2-improved"].verdict), (True, "improved"))
        self.assertEqual(verdicts["ops-v2-improved"].stats["p_value"], 0.03125)
        self.assertEqual((verdicts["ops-v2-large"].passed, verdicts["ops-v2-large"].verdict), (False, "improved"))
        self.assertIn("mean cost", verdicts["ops-v2-large"].reasons[0])

    def test_samples_give_pass_at_k_and_a_case_passes_only_when_every_sample_does(self):
        results = main.run_suite(self.suite, self.provider("ops-v1"), samples=5, judge=main.keyword_judge)
        by_id = {c["id"]: c for c in results["cases"]}
        self.assertEqual(by_id["crashloop-first-step"]["passes"], 3)
        self.assertEqual(by_id["crashloop-first-step"]["pass_at"], {"1": 0.6, "5": 1.0})
        self.assertEqual(by_id["leaked-key"]["pass_at"]["1"], 0.2)
        self.assertFalse(by_id["leaked-key"]["passed"])
        self.assertEqual(results["summary"]["passed"], 11)

    def test_latency_comes_from_the_injected_clock(self):
        ticks = iter([10.0, 10.25])
        completion = self.provider("ops-v1", clock=lambda: next(ticks)).complete("Question: What does HTTP status 503 mean?")
        self.assertEqual(completion.latency_ms, 250.0)
        self.assertTrue(completion.text.startswith("Service Unavailable"))
        self.assertGreater(completion.prompt_tokens, 0)

    def test_unknown_model_and_unreachable_server_are_provider_errors(self):
        with self.assertRaisesRegex(main.ProviderError, "HTTP 404.*model_not_found"):
            self.provider("gpt-nope").complete("hi")
        down = main.OpenAIChatProvider("http://127.0.0.1:%d/v1" % free_port(), "ops-v1", timeout=5)
        case = self.suite.cases[0]
        record = main.run_case(self.suite, case, down, 1, 0.0, None)
        self.assertFalse(record["passed"])
        self.assertIn("cannot reach", record["samples"][0]["error"])
        self.assertEqual(main.summarize([record], 1)["errors"], 1)


class Reports(unittest.TestCase):
    def test_junit_counts_failures_and_carries_the_detail(self):
        doc = {"suite": "s", "model": "m", "cases": [
            {"id": "ok", "passed": True, "samples": []},
            {"id": "bad", "passed": False, "samples": [{"seed": 0, "passed": False, "assertions": [{"type": "equals", "passed": False, "detail": "got 'x', want 'y'"}]}]},
        ]}
        root = ET.fromstring(main.run_junit(doc).split("\n", 1)[1])
        self.assertEqual((root.get("tests"), root.get("failures")), ("2", "1"))
        failure = root.find("./testsuite/testcase[@name='bad']/failure")
        self.assertEqual(failure.text, "seed 0: equals: got 'x', want 'y'")
        gate_xml = main.gate_junit(main.GateResult(False, "regressed", ["r1", "r2"], [], {"baseline_model": "a", "candidate_model": "b"}))
        self.assertIn('message="r1; r2"', gate_xml)

    def test_promptfoo_config_maps_every_assertion(self):
        suite = main.load_suite(SUITE)
        config = main.promptfoo_config(suite, "http://127.0.0.1:9/v1/", ["ops-v1", "ops-v2-improved"], "judge-v1")
        self.assertEqual([p["id"] for p in config["providers"]], ["openai:chat:ops-v1", "openai:chat:ops-v2-improved"])
        self.assertEqual(config["providers"][0]["config"]["apiBaseUrl"], "http://127.0.0.1:9/v1")
        self.assertEqual(config["defaultTest"]["options"]["rubricPrompt"], main.JUDGE_TEMPLATE)
        types = {a["type"] for t in config["tests"] for a in t["assert"]}
        self.assertEqual(types, {"equals", "contains", "icontains", "not-contains", "regex", "is-json", "javascript", "llm-rubric"})
        self.assertEqual([t["description"] for t in config["tests"]], [c.id for c in suite.cases])
        self.assertIn("/-?\\d+(?:\\.\\d+)?/", main.numeric_javascript(6.0, 0.0))
        with self.assertRaisesRegex(main.SuiteError, "--judge-model"):
            main.promptfoo_config(suite, "http://x/v1", ["ops-v1"])

    def test_yaml_emitter_quotes_every_string(self):
        text = "\n".join(main.to_yaml({"a": "x: y", "list": [{"type": "regex", "value": "^\\*$"}, 3], "empty": [], "t": True}))
        self.assertEqual(text, 'a: "x: y"\nlist:\n  - type: "regex"\n    value: "^\\\\*$"\n  - 3\nempty: []\nt: true')


class Cli(unittest.TestCase):
    def run_main(self, argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = main.main(argv)
        return code, out.getvalue(), err.getvalue()

    def test_stats_contamination_and_gate_exit_codes(self):
        code, out, _ = self.run_main(["stats", "mcnemar", "8", "1", "--json"])
        self.assertEqual((code, json.loads(out)["p_value"]), (0, 0.0390625))
        self.assertEqual(self.run_main(["stats", "wilson", "1"])[0], 2)
        code, out, _ = self.run_main(["contamination", "--suite", SUITE, "--train", TRAIN, "--json"])
        self.assertEqual((code, json.loads(out)["flagged"]), (1, ["slo-downtime", "redact-email"]))
        with tempfile.TemporaryDirectory() as tmp:
            base, cand = paired(5, 8, 1, 6)
            paths = []
            for name, doc in (("b.json", results_doc("v1", base)), ("c.json", results_doc("v2", cand))):
                paths.append(os.path.join(tmp, name))
                with open(paths[-1], "w") as handle:
                    json.dump(doc, handle)
            code, out, _ = self.run_main(["gate", "--baseline", paths[0], "--candidate", paths[1], "--junit", os.path.join(tmp, "g.xml")])
            self.assertEqual(code, 1)
            self.assertIn("REJECT (quality: regressed)", out)
            self.assertTrue(os.path.exists(os.path.join(tmp, "g.xml")))
            code, out, _ = self.run_main(["gate", "--baseline", paths[0], "--candidate", paths[1], "--alpha", "0.01", "--json"])
            self.assertEqual((code, json.loads(out)["verdict"]), (0, "no significant difference"))
            code, _, err = self.run_main(["gate", "--baseline", paths[0], "--candidate", os.path.join(tmp, "missing.json")])
            self.assertEqual(code, 1)
            self.assertIn("missing.json", err)

    def test_run_exits_1_when_the_model_is_unreachable(self):
        with tempfile.TemporaryDirectory() as tmp:
            code, _, err = self.run_main(["run", "--suite", SUITE, "--base-url", "http://127.0.0.1:%d/v1" % free_port(), "--model", "ops-v1", "--timeout", "5", "--out", os.path.join(tmp, "r.json")])
            self.assertEqual(code, 1)
            self.assertIn("not a 0% model", err)
            with open(os.path.join(tmp, "r.json")) as handle:
                self.assertEqual(json.load(handle)["summary"]["errors"], 20)


if __name__ == "__main__":
    unittest.main()
