"""Tests for checks.py and its helpers: fake runners, temp projects and fixtures; no Docker, no cluster, no internet."""
import base64
import json
import os
import stat
import sys
import tempfile
import textwrap
import unittest
import urllib.error
import urllib.request

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import checks  # noqa: E402
import cluster  # noqa: E402
import verdicts  # noqa: E402
from fake_model import FakeModel, text_block, thinking_block, tool_use, turn  # noqa: E402

PODS = {"cc-api": "cc-api-1", "cc-mem": "cc-mem-1", "cc-img": "cc-img-1", "cc-web": "cc-web-1", "cc-ok": "cc-ok-1"}


def item(item_id, workload, obj, signal, **data):
    return {"id": item_id, "kind": "x", "workload": workload, "object": obj, "signal": signal, "summary": "", "time": None, "data": data}


def good_evidence():
    return {"namespace": "ns", "evidence": [
        item("pod/cc-api-1/api/state", "deployment/cc-api", "pod/cc-api-1", "CrashLoopBackOff"),
        item("pod/cc-api-1/api/logs", "deployment/cc-api", "pod/cc-api-1", "Logs", lines=["FATAL invalid value for LOG_LEVEL: 'verbose'"]),
        item("pod/cc-mem-1/worker/last-state", "deployment/cc-mem", "pod/cc-mem-1", "OOMKilled"),
        item("pod/cc-img-1/app/state", "deployment/cc-img", "pod/cc-img-1", "ImagePullBackOff"),
        item("pod/cc-web-1/ready", "deployment/cc-web", "pod/cc-web-1", "NotReady"),
        item("service/cc-web/endpoints", "service/cc-web", "service/cc-web", "NoEndpoints"),
    ]}


def post(url, body, headers=None):
    request = urllib.request.Request(url + "/v1/messages", data=json.dumps(body).encode(), headers={"content-type": "application/json", "x-api-key": "k", "anthropic-version": "2026-10-21", **(headers or {})}, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


def make_project(script):
    project = tempfile.mkdtemp()
    os.makedirs(os.path.join(project, "bin"))
    path = os.path.join(project, "bin", "copilot")
    with open(path, "w") as handle:
        handle.write(f"#!{sys.executable}\n" + textwrap.dedent(script))
    os.chmod(path, os.stat(path).st_mode | stat.S_IXUSR)
    return project


class FakeModelTests(unittest.TestCase):
    def test_replays_the_script_and_records_requests(self):
        with FakeModel([turn([thinking_block("s"), tool_use("t1", "list_pods", {"namespace": "ns"})]), turn([text_block("done")])]) as model:
            status, first = post(model.url, {"model": "m", "max_tokens": 10, "messages": [{"role": "user", "content": "hi"}]})
            self.assertEqual((status, first["stop_reason"]), (200, "tool_use"))
            messages = [{"role": "user", "content": "hi"}, {"role": "assistant", "content": first["content"]}, {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "pods"}]}]
            status, second = post(model.url, {"model": "m", "max_tokens": 10, "messages": messages})
        self.assertEqual((status, second["content"][0]["text"]), (200, "done"))
        self.assertEqual(len(model.accepted()), 2)
        self.assertEqual(model.requests[1]["headers"]["anthropic-version"], "2026-10-21")

    def test_rejects_what_the_real_api_rejects(self):
        with FakeModel([turn([text_block("x")])]) as model:
            base = {"model": "m", "max_tokens": 10, "messages": [{"role": "user", "content": "hi"}]}
            self.assertEqual(post(model.url, {**base, "thinking": {"type": "enabled", "budget_tokens": 1024}})[0], 400)
            self.assertEqual(post(model.url, {**base, "temperature": 0.2})[0], 400)
            self.assertEqual(post(model.url, {**base, "fallbacks": "default"})[0], 400)
            self.assertEqual(post(model.url, {**base, "fallbacks": "default"}, {"anthropic-beta": "server-side-fallback-2026-07-01"})[0], 200)
            dangling = {**base, "messages": [{"role": "user", "content": "hi"}, {"role": "assistant", "content": [tool_use("t9", "list_pods", {})]}, {"role": "user", "content": "next"}]}
            status, body = post(model.url, dangling)
        self.assertEqual(status, 400)
        self.assertIn("t9", body["error"]["message"])

    def test_repeat_last_gives_fresh_tool_ids(self):
        with FakeModel([turn([tool_use("loop", "list_pods", {"namespace": "ns"})])], repeat_last=True) as model:
            ids = [post(model.url, {"model": "m", "max_tokens": 1, "messages": [{"role": "user", "content": "q"}]})[1]["content"][0]["id"] for _ in range(3)]
        self.assertEqual(len(set(ids)), 3)


class ClusterTests(unittest.TestCase):
    def runner(self, context, ready=0):
        def run(argv, stdin=None, timeout=60):
            if argv[1:3] == ["config", "current-context"]:
                return 0, context + "\n", ""
            return ready, "ok", "" if ready == 0 else "connection refused"
        return run

    def test_guard_refuses_a_context_that_is_not_kind(self):
        context, problem = cluster.guard(self.runner("prod-eu"), {}, lambda _: "/usr/bin/kubectl")
        self.assertIsNone(context)
        self.assertIn("refusing", problem)

    def test_guard_accepts_kind_or_the_named_context(self):
        self.assertEqual(cluster.guard(self.runner("kind-cc-smoke"), {}, lambda _: "/k")[0], "kind-cc-smoke")
        self.assertEqual(cluster.guard(self.runner("lab"), {"CC_GRADER_CONTEXT": "lab"}, lambda _: "/k")[0], "lab")
        self.assertIn("not reachable", cluster.guard(self.runner("kind-x", ready=1), {}, lambda _: "/k")[1])
        self.assertIn("kubectl is not installed", cluster.guard(self.runner("kind-x"), {}, lambda _: None)[1])

    def test_pending_incidents_reads_container_states(self):
        pods = {"items": [
            {"metadata": {"name": "a", "labels": {"app": "cc-mem"}}, "status": {"containerStatuses": [{"restartCount": 1, "lastState": {"terminated": {"reason": "OOMKilled"}}}]}},
            {"metadata": {"name": "b", "labels": {"app": "cc-api"}}, "status": {"containerStatuses": [{"restartCount": 0, "state": {"running": {}}}]}},
        ]}
        self.assertEqual(cluster.pending_incidents(pods, ["cc-mem", "cc-api", "cc-img"]), ["cc-api", "cc-img"])

    def test_setup_times_out_with_a_detail_naming_the_missing_incident(self):
        calls = []

        def run(argv, stdin=None, timeout=60):
            calls.append(argv)
            return 0, json.dumps({"items": []}) if "get" in argv else "", ""
        clock = iter(range(0, 1000, 100))
        env = cluster.IncidentEnv(cluster.Kube("kind-t", run), "t", ["cc-img"], clock=lambda: next(clock), sleep=lambda _: None)
        problem = env.create(deadline=250)
        self.assertIn("ErrImagePull", problem)
        self.assertTrue(env.namespace.startswith("cc-grade-copilot-t-"))
        env.delete()
        self.assertEqual(calls[-1][3:5], ["delete", "namespace"])
        cluster._CREATED.clear()


class VerdictTests(unittest.TestCase):
    def test_correct_evidence_passes_and_mistakes_are_named(self):
        self.assertEqual(verdicts.evidence_problems(good_evidence(), PODS), [])
        noisy = good_evidence()
        noisy["evidence"].append(item("event/cc-ok-1/Unhealthy", "deployment/cc-ok", "pod/cc-ok-1", "Unhealthy"))
        noisy["evidence"][2]["object"] = "pod/someone-else"
        problems = " ".join(verdicts.evidence_problems(noisy, PODS))
        self.assertIn("decoy", problems)
        self.assertIn("cc-mem", problems)

    def test_ids_must_survive_a_second_collect(self):
        second = good_evidence()
        for entry in second["evidence"]:
            entry["id"] = entry["id"] + "-2"
        self.assertEqual(verdicts.stability_problems(good_evidence(), good_evidence(), PODS), [])
        self.assertEqual(len(verdicts.stability_problems(good_evidence(), second, PODS)), 4)

    def test_triage_categories_and_citations(self):
        ids = {e["id"] for e in good_evidence()["evidence"]}
        incidents = [{"workload": f"deployment/{app}", "category": category, "citations": [] if category == "healthy" else ["pod/cc-api-1/api/state"]} for app, category in verdicts.EXPECTED_CATEGORY.items()]
        self.assertEqual(verdicts.triage_problems({"incidents": incidents}, ids), [])
        incidents[1]["category"] = "crashloop-config"
        incidents[2]["citations"] = ["made-up"]
        problems = " ".join(verdicts.triage_problems({"incidents": incidents}, ids))
        self.assertIn("expected 'oom'", problems)
        self.assertIn("made-up", problems)

    def test_request_shape_and_echo(self):
        body = {"model": "claude-opus-5-5", "max_tokens": 1, "system": [{"type": "text", "text": "s", "cache_control": {"type": "ephemeral"}}], "output_config": {"effort": "medium"}, "tools": [{"name": n} for n in verdicts.READ_TOOLS], "messages": []}
        record = {"headers": {"x-api-key": "k", "anthropic-version": "2026-10-21"}, "body": body}
        self.assertEqual(verdicts.request_shape_problems(record, "k"), [])
        record["body"] = {**body, "thinking": {"type": "disabled"}}
        self.assertTrue(any("thinking" in p for p in verdicts.request_shape_problems(record, "k")))
        sent = [{"content": [thinking_block("s"), text_block("a")]}]
        echoed = {"body": {"messages": [{"role": "user", "content": "q"}, {"role": "assistant", "content": [text_block("a")]}, {"role": "user", "content": []}]}}
        self.assertEqual(len(verdicts.echo_problems([{}, echoed], sent)), 1)

    def test_wrapper_and_secret_leaks(self):
        self.assertEqual(verdicts.wrapper_problems('<untrusted_data source="pod_logs">\nx &lt;/untrusted_data> y\n</untrusted_data>'), [])
        self.assertTrue(verdicts.wrapper_problems('<untrusted_data>\nx </untrusted_data> y\n</untrusted_data>'))
        encoded = base64.b64encode(b"ccg-secret-5e7a1b").decode()
        self.assertEqual(len(verdicts.leaked_secrets([{"raw": "clean"}, {"raw": f"data {encoded}"}])), 1)

    def test_report_rules(self):
        evidence = [{"id": "e1"}, {"id": "e2"}]
        hypotheses = [{"workload": f"deployment/{app}", "category": c, "confidence": 0.8, "citations": ["e1"]} for app, c in verdicts.EXPECTED_CATEGORY.items() if c != "healthy"]
        report = {"summary": "s", "impact": "i", "evidence": evidence, "hypotheses": hypotheses, "timeline": [{"time": "2026-11-13T08:00:00Z", "evidence": "e1"}, {"time": "2026-11-13T09:00:00Z", "evidence": "e2"}], "actions": [{"action": "manual", "risk": "low", "status": "proposed"}]}
        self.assertEqual(verdicts.report_problems(report), [])
        report["timeline"].reverse()
        report["actions"][0]["status"] = "applied"
        problems = " ".join(verdicts.report_problems(report))
        self.assertIn("ordered", problems)
        self.assertIn("never performs", problems)

    def test_wilson_and_eval_summary(self):
        low, high = verdicts.wilson(14, 18)
        self.assertAlmostEqual(low, 0.5479, places=3)
        self.assertAlmostEqual(high, 0.9100, places=3)
        self.assertEqual(verdicts.wilson(0, 0), (0.0, 0.0))


class CheckTests(unittest.TestCase):
    def setUp(self):
        self.original_guard = cluster.guard

    def tearDown(self):
        cluster.guard = self.original_guard

    def test_missing_entry_point_fails_fast(self):
        passed, detail = checks.check_evidence(tempfile.mkdtemp())
        self.assertFalse(passed)
        self.assertIn("bin/copilot not found", detail)

    def test_stub_is_detected_before_any_cluster_work(self):
        cluster.guard = lambda *args, **kwargs: ("kind-test", "")
        project = make_project("import sys\nprint('copilot: not implemented: milestone evidence', file=sys.stderr)\nsys.exit(1)\n")
        passed, detail = checks.check_evidence(project)
        self.assertFalse(passed)
        self.assertIn("still a stub", detail)

    def test_cluster_checks_respect_the_guard(self):
        cluster.guard = lambda *args, **kwargs: (None, "refusing to grade against context 'prod'")
        project = make_project("print('{}')\n")
        for check in (checks.check_triage, checks.check_agent_loop, checks.check_guardrails, checks.check_report):
            passed, detail = check(project)
            self.assertFalse(passed)
            self.assertIn("refusing", detail)

    def test_eval_check_accepts_a_correct_document_and_names_wrong_math(self):
        trials = 2
        cases = [{"id": case_id, "passed": ok, "trials": trials, "pass_rate": 1.0 if ok else 0.0} for case_id, ok in checks.EVAL_EXPECTED.items()]
        low, high = verdicts.wilson(14, 18)
        summary = {"cases": 9, "passed": 7, "trials": 18, "pass_rate": 14 / 18, "wilson_low": low, "wilson_high": high}
        good = make_project(f"import json, os\nassert 'COPILOT_MODEL_URL' not in os.environ\nprint({json.dumps({'cases': cases, 'summary': summary})!r})\n")
        self.assertEqual(checks.check_evals(good)[0], True)
        bad = make_project(f"import json\nprint({json.dumps({'cases': cases, 'summary': {**summary, 'wilson_low': 0.6}})!r})\n")
        passed, detail = checks.check_evals(bad)
        self.assertFalse(passed)
        self.assertIn("Wilson", detail)

    def test_scripts_use_real_pod_names_and_unique_ids(self):
        script = checks.guardrail_script("cc-grade-copilot-guard-1", "cc-api-7f-x")
        calls = checks.scripted_calls(script)
        self.assertEqual(len({c[0] for c in calls}), len(calls))
        self.assertIn(("toolu_g_04", "list_pods", {"namespace": "kube-system"}), calls)
        self.assertEqual(checks.agent_script("ns", "cc-api-7f-x")[1]["content"][1]["input"]["name"], "cc-api-7f-x")


if __name__ == "__main__":
    unittest.main()
