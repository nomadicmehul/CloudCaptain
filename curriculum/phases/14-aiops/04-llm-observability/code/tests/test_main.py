import contextlib
import io
import json
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import main  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
FIXTURES = os.path.join(HERE, "..", "fixtures")
START_NS = 1_793_498_580_000_000_000


def fixture(name):
    with open(os.path.join(FIXTURES, name), encoding="utf-8") as handle:
        return handle.read()


def load_run(stem):
    trace = main.parse_trace_lines(fixture(f"{stem}.trace.jsonl"))
    outcome = json.loads(fixture(f"{stem}.outcome.json"))
    return trace, outcome


def build(stem, seed=7, start_ns=START_NS):
    trace, outcome = load_run(stem)
    return main.build_spans(trace, outcome, start_ns, main.IdGenerator(seed))


def by_name(spans, prefix):
    return [span for span in spans if span.name.startswith(prefix)]


class EncodingTests(unittest.TestCase):
    def test_values_follow_the_protobuf_json_mapping(self):
        self.assertEqual(main.encode_value(True), {"boolValue": True})
        self.assertEqual(main.encode_value(1800), {"intValue": "1800"})
        self.assertEqual(main.encode_value(0.0774), {"doubleValue": 0.0774})
        self.assertEqual(main.encode_value("chat"), {"stringValue": "chat"})
        self.assertEqual(main.encode_value(["tool_use", "end_turn"]), {"arrayValue": {"values": [{"stringValue": "tool_use"}, {"stringValue": "end_turn"}]}})
        for value in (True, 1800, 0.0774, "chat", ["a", "b"]):
            self.assertEqual(main.decode_value(main.encode_value(value)), value)

    def test_ids_are_hex_of_the_right_length_and_deterministic_with_a_seed(self):
        ids = main.IdGenerator(42)
        trace_id, span_id = ids.trace_id(), ids.span_id()
        self.assertEqual((len(trace_id), len(span_id)), (32, 16))
        self.assertTrue(all(c in "0123456789abcdef" for c in trace_id + span_id))
        again = main.IdGenerator(42)
        self.assertEqual((again.trace_id(), again.span_id()), (trace_id, span_id))
        self.assertNotEqual(main.IdGenerator().trace_id(), main.IdGenerator().trace_id())

    def test_parse_trace_lines_sorts_by_step_and_reports_bad_lines(self):
        events = main.parse_trace_lines('{"step": 2, "elapsed_ms": 5}\n\n{"step": 1, "elapsed_ms": 3}\n')
        self.assertEqual([event["step"] for event in events], [1, 2])
        with self.assertRaises(ValueError) as caught:
            main.parse_trace_lines('{"step": 1}\nnot json\n')
        self.assertIn("trace line 2", str(caught.exception))
        with self.assertRaises(ValueError):
            main.parse_trace_lines('{"elapsed_ms": 3}\n')

    def test_parse_start_accepts_iso8601_and_defaults_to_now_minus_elapsed(self):
        self.assertEqual(main.parse_start("2026-11-01T02:03:00Z"), START_NS)
        self.assertEqual(main.parse_start("2026-11-01T02:03:00+00:00"), START_NS)
        self.assertEqual(main.parse_start("2026-11-01T02:03:00.250Z"), START_NS + 250_000_000)
        self.assertEqual(main.parse_start(None, elapsed_seconds=21.4, now_ns=lambda: 1_000_000_000_000), 1_000_000_000_000 - 21_400_000_000)
        with self.assertRaises(ValueError):
            main.parse_start("yesterday")


class SpanBuildingTests(unittest.TestCase):
    def test_one_root_one_chat_per_step_one_tool_per_audit_entry(self):
        spans = build("crashloop")
        self.assertEqual(len(spans), 1 + 5 + 7)
        root = spans[0]
        self.assertIsNone(root.parent_span_id)
        self.assertEqual(root.name, "invoke_agent triage_agent")
        chats = by_name(spans, "chat ")
        self.assertEqual([span.attributes[main.STEP] for span in chats], [1, 2, 3, 4, 5])
        self.assertTrue(all(span.parent_span_id == root.span_id for span in chats))
        tools = by_name(spans, "execute_tool ")
        self.assertEqual([span.attributes[main.TOOL_NAME] for span in tools], ["list_events", "list_pods", "describe_pod", "pod_logs", "run_kubectl", "propose_action", "write_report"])
        chat_by_step = {span.attributes[main.STEP]: span for span in chats}
        for span in tools:
            self.assertEqual(span.parent_span_id, chat_by_step[span.attributes[main.STEP]].span_id)
        self.assertEqual(len({span.trace_id for span in spans}), 1)
        self.assertEqual(len({span.span_id for span in spans}), len(spans))

    def test_chat_spans_carry_the_genai_attribute_names(self):
        chat = by_name(build("crashloop"), "chat ")[0]
        self.assertEqual(chat.name, "chat claude-opus-5-5")
        self.assertEqual(chat.kind, main.SPAN_KIND_CLIENT)
        self.assertEqual(chat.attributes[main.OPERATION_NAME], "chat")
        self.assertEqual(chat.attributes[main.PROVIDER_NAME], "anthropic")
        self.assertEqual(chat.attributes[main.REQUEST_MODEL], "claude-opus-5-5")
        self.assertEqual(chat.attributes[main.RESPONSE_MODEL], "claude-opus-5-5")
        self.assertEqual((chat.attributes[main.INPUT_TOKENS], chat.attributes[main.OUTPUT_TOKENS]), (1800, 90))
        self.assertEqual(chat.attributes[main.FINISH_REASONS], ["tool_use"])
        self.assertAlmostEqual(chat.attributes[main.COST_USD], (1800 * 4 + 90 * 20) / 1e6)
        self.assertEqual(main.SEMCONV_STATUS, "Development")

    def test_timestamps_are_sequential_nanoseconds_and_chat_duration_is_the_model_latency(self):
        spans = build("crashloop")
        root, chats = spans[0], by_name(spans, "chat ")
        self.assertEqual(chats[0].start_ns, START_NS)
        self.assertEqual(chats[0].end_ns - chats[0].start_ns, 3120 * 1_000_000)
        for earlier, later in zip(chats, chats[1:]):
            self.assertLessEqual(earlier.end_ns, later.start_ns)
        self.assertEqual(root.end_ns - root.start_ns, 21_400 * 1_000_000)
        self.assertEqual(root.end_ns, max(span.end_ns for span in spans))
        self.assertTrue(all(span.end_ns >= span.start_ns for span in spans))

    def test_tool_durations_are_shared_out_of_the_unaccounted_time_and_marked_estimated(self):
        spans = build("crashloop")
        tools = by_name(spans, "execute_tool ")
        ran = [span for span in tools if span.attributes[main.OUTCOME] == "ok"]
        self.assertEqual(len(ran), 4)
        for span in ran:
            self.assertAlmostEqual(span.duration_ms, (21_400 - 18_070) / 4, places=3)
            self.assertTrue(span.attributes[main.ESTIMATED])
        for span in tools:
            if span.attributes[main.OUTCOME] in ("refused", "report"):
                self.assertEqual(span.duration_ms, 0)
                self.assertFalse(span.attributes[main.ESTIMATED])

    def test_measured_tool_time_wins_over_the_estimate(self):
        trace, outcome = load_run("oomkilled")
        outcome["audit"][0]["elapsed_ms"] = 640
        spans = main.build_spans(trace, outcome, START_NS, main.IdGenerator(1))
        first, second = by_name(spans, "execute_tool ")[:2]
        self.assertEqual((first.duration_ms, first.attributes[main.ESTIMATED]), (640, False))
        self.assertAlmostEqual(second.duration_ms, 9_800 - 6_200, places=3)
        self.assertTrue(second.attributes[main.ESTIMATED])

    def test_failed_tool_outcomes_set_error_status_and_error_type(self):
        spans = build("crashloop")
        refused = [span for span in spans if span.attributes.get(main.OUTCOME) == "refused"]
        self.assertEqual(len(refused), 2)
        for span in refused:
            self.assertEqual(span.status_code, main.STATUS_ERROR)
            self.assertEqual(span.attributes[main.ERROR_TYPE], "refused")
        self.assertEqual(refused[0].status_message, "unknown tool")
        ok = [span for span in spans if span.attributes.get(main.OUTCOME) == "ok"]
        self.assertTrue(all(span.status_code == main.STATUS_UNSET and main.ERROR_TYPE not in span.attributes for span in ok))
        failed = [span for span in build("maxsteps") if span.attributes.get(main.OUTCOME) == "error"]
        self.assertEqual(failed[0].attributes[main.ERROR_TYPE], "error")
        self.assertIn("timed out", failed[0].status_message)

    def test_root_span_summarises_the_run_and_its_status_follows_the_stop_reason(self):
        root = build("crashloop")[0]
        self.assertEqual(root.status_code, main.STATUS_OK)
        self.assertEqual(root.attributes[main.STOP], "report")
        self.assertEqual((root.attributes[main.INPUT_TOKENS], root.attributes[main.OUTPUT_TOKENS]), (16600, 550))
        self.assertEqual((root.attributes[main.STEPS], root.attributes[main.TOOL_CALLS], root.attributes[main.VIOLATIONS]), (5, 4, 1))
        self.assertEqual(root.attributes[main.COST_USD], 0.0774)
        self.assertEqual(root.attributes[main.AGENT_NAME], "triage_agent")
        stuck = build("maxsteps")[0]
        self.assertEqual(stuck.status_code, main.STATUS_ERROR)
        self.assertIn("max_steps", stuck.status_message)

    def test_trace_without_outcome_has_no_tool_spans_and_sums_model_time(self):
        trace, _ = load_run("oomkilled")
        spans = main.build_spans(trace, None, START_NS, main.IdGenerator(1))
        self.assertEqual([span.name.split(" ")[0] for span in spans], ["invoke_agent", "chat", "chat", "chat"])
        self.assertEqual(spans[0].duration_ms, 1900 + 1700 + 2600)
        self.assertEqual(spans[0].status_code, main.STATUS_UNSET)
        self.assertNotIn(main.STOP, spans[0].attributes)
        self.assertAlmostEqual(spans[0].attributes[main.COST_USD], sum(span.attributes[main.COST_USD] for span in spans[1:]), places=6)


class OtlpShapeTests(unittest.TestCase):
    def test_payload_has_the_otlp_json_shape(self):
        payload = main.to_otlp(build("crashloop"), service="triage-agent")
        self.assertEqual(list(payload), ["resourceSpans"])
        resource = payload["resourceSpans"][0]
        self.assertEqual(resource["resource"]["attributes"], [{"key": "service.name", "value": {"stringValue": "triage-agent"}}])
        scope = resource["scopeSpans"][0]
        self.assertEqual(scope["scope"], {"name": "agenttrace", "version": "1.0.0"})
        spans = scope["spans"]
        self.assertEqual(len(spans), 13)
        for span in spans:
            self.assertEqual(len(span["traceId"]), 32)
            self.assertEqual(len(span["spanId"]), 16)
            self.assertTrue(span["startTimeUnixNano"].isdigit() and span["endTimeUnixNano"].isdigit())
            self.assertIsInstance(span["kind"], int)
            for attribute in span["attributes"]:
                self.assertEqual(set(attribute), {"key", "value"})
        self.assertNotIn("parentSpanId", spans[0])
        self.assertEqual(spans[1]["parentSpanId"], spans[0]["spanId"])
        self.assertEqual(spans[0]["status"], {"code": 1})
        self.assertEqual(spans[1]["kind"], 3)
        attributes = {a["key"]: a["value"] for a in spans[1]["attributes"]}
        self.assertEqual(attributes["gen_ai.usage.input_tokens"], {"intValue": "1800"})
        self.assertEqual(attributes["gen_ai.response.finish_reasons"], {"arrayValue": {"values": [{"stringValue": "tool_use"}]}})
        self.assertEqual(main.validate_otlp(payload), [])
        self.assertEqual(main.service_of(payload), "triage-agent")

    def test_round_trip_through_otlp_json_keeps_every_span(self):
        spans = build("crashloop")
        again = main.from_otlp(json.loads(json.dumps(main.to_otlp(spans))))
        self.assertEqual(again, spans)

    def test_validate_otlp_names_the_problem(self):
        payload = main.to_otlp(build("oomkilled"))
        span = payload["resourceSpans"][0]["scopeSpans"][0]["spans"][1]
        span["traceId"] = "abc"
        span["startTimeUnixNano"] = 123
        span["attributes"][4]["value"] = {"intValue": 2000}
        problems = main.validate_otlp(payload)
        self.assertTrue(any("traceId must be 32" in p for p in problems))
        self.assertTrue(any("startTimeUnixNano must be a string" in p for p in problems))
        self.assertTrue(any("intValue must be a string" in p for p in problems))
        self.assertEqual(main.validate_otlp({}), ["payload needs a non-empty resourceSpans list"])


class RenderingTests(unittest.TestCase):
    def test_tree_shows_hierarchy_durations_tokens_cost_and_errors(self):
        spans = build("crashloop")
        text = main.render_tree(spans, "triage-agent")
        lines = text.splitlines()
        self.assertEqual(lines[0], f"trace {spans[0].trace_id[:8]}  triage-agent  2026-11-01 02:03:00Z  21.4 s")
        self.assertIn("└─ invoke_agent triage_agent", lines[1])
        self.assertIn("5 steps  4 tool calls  16600 in / 550 out  $0.0774  stop report  violations 1", lines[1])
        self.assertIn("├─ chat claude-opus-5-5", lines[2])
        self.assertIn("3120 ms", lines[2])
        self.assertIn("step 1  1800 in / 90 out  $0.0090  tool_use", lines[2])
        self.assertIn("│  ├─ execute_tool list_events", lines[3])
        self.assertIn("~832 ms", lines[3])
        self.assertIn("execute_tool run_kubectl", lines[10])
        self.assertIn("refused  unknown tool  ERROR", lines[10])
        self.assertIn("execute_tool propose_action", lines[11])
        self.assertIn("└─ chat claude-opus-5-5", lines[12])
        self.assertIn("execute_tool write_report", lines[13])
        self.assertEqual(len(lines), 14)

    def test_percentile_is_nearest_rank(self):
        values = [2210, 2480, 2600, 2700, 2800, 2900, 3120, 4870, 5390]
        self.assertEqual(main.percentile(values, 50), 2800)
        self.assertEqual(main.percentile(values, 95), 5390)
        self.assertEqual(main.percentile([7], 95), 7)
        self.assertEqual(main.percentile([], 95), 0.0)
        self.assertEqual(main.percentile([1, 2, 3, 4], 50), 2)

    def test_cost_estimate_uses_the_price_table(self):
        self.assertAlmostEqual(main.estimate_cost("claude-opus-5-5", 1_000_000, 100_000, 500_000), 4.0 + 2.0 + 0.10)
        self.assertAlmostEqual(main.estimate_cost("claude-haiku-5-5", 1_000_000, 0), 0.10)
        self.assertEqual(main.estimate_cost("unknown-model", 1000, 1000), 0.0)


class MetricsTests(unittest.TestCase):
    def test_metrics_over_three_runs(self):
        runs = [build("crashloop", seed=1), build("oomkilled", seed=2), build("maxsteps", seed=3)]
        report = main.metrics(runs)
        self.assertEqual(report["runs"], 3)
        self.assertEqual(report["latency_ms"]["claude-opus-5-5"], {"p50": 2800.0, "p95": 5390.0, "spans": 9})
        self.assertEqual(report["latency_ms"]["claude-sonnet-5-5"], {"p50": 1900.0, "p95": 2600.0, "spans": 3})
        self.assertEqual(report["tokens_per_run"]["input_mean"], round((16600 + 8500 + 20200) / 3, 1))
        self.assertAlmostEqual(report["cost_per_run_usd"]["total"], 0.0774 + 0.02018 + 0.0866, places=6)
        self.assertEqual(report["cost_per_run_usd"]["p95"], 0.0866)
        self.assertEqual(report["tool_calls_per_run"], {"mean": round(10 / 3, 2), "p95": 4.0})
        self.assertEqual(report["tool_error_rate"], 0.25)
        self.assertEqual(report["runs_without_report"], 1)
        self.assertEqual(report["refusal_rate"], 0.0)
        self.assertEqual(report["stops"], {"report": 2, "max_steps": 1})
        text = main.render_metrics(report)
        self.assertIn("runs: 3   stops: max_steps=1, report=2", text)
        self.assertIn("chat latency claude-opus-5-5      p50     2800 ms   p95     5390 ms   (9 spans)", text)
        self.assertIn("tool error rate       25.0%", text)
        self.assertIn("runs without report   1/3", text)

    def test_summary_detects_refusals_and_groups_runs_by_trace_id(self):
        trace, outcome = load_run("oomkilled")
        trace[2]["gen_ai.response.finish_reasons"] = ["refusal"]
        outcome.update(stop="refusal", report=None)
        outcome["audit"] = outcome["audit"][:2]
        refused = main.build_spans(trace, outcome, START_NS, main.IdGenerator(5))
        summary = main.summarize_run(refused)
        self.assertTrue(summary.refused)
        self.assertFalse(summary.has_report)
        self.assertEqual((summary.steps, summary.tool_calls, summary.tool_errors, summary.models), (3, 2, 0, ["claude-sonnet-5-5"]))
        mixed = refused + build("crashloop", seed=6)
        groups = main.group_runs(mixed)
        self.assertEqual([len(group) for group in groups], [6, 13])
        self.assertEqual(main.metrics(groups)["refusal_rate"], 0.5)


class ExportTests(unittest.TestCase):
    def test_export_posts_json_and_reads_partial_success(self):
        seen = {}

        def transport(url, headers, body):
            seen.update(url=url, headers=headers, body=json.loads(body))
            return 200, b'{"partialSuccess":{}}'

        payload = main.to_otlp(build("crashloop"))
        result = main.export(payload, "http://collector:4318/v1/traces", transport, headers={"Authorization": "Basic abc"})
        self.assertEqual(seen["url"], "http://collector:4318/v1/traces")
        self.assertEqual(seen["headers"], {"content-type": "application/json", "Authorization": "Basic abc"})
        self.assertEqual(seen["body"], payload)
        self.assertEqual((result["spans"], result["rejected_spans"], result["message"]), (13, 0, ""))
        self.assertEqual(result["bytes"], len(json.dumps(payload).encode()))

    def test_export_surfaces_rejections_and_http_errors(self):
        payload = main.to_otlp(build("oomkilled"))
        result = main.export(payload, transport=lambda u, h, b: (200, b'{"partialSuccess":{"rejectedSpans":"2","errorMessage":"bad span"}}'))
        self.assertEqual((result["rejected_spans"], result["message"]), (2, "bad span"))
        with self.assertRaises(RuntimeError) as caught:
            main.export(payload, transport=lambda u, h, b: (400, b'{"code":3,"message":"invalid"}'))
        self.assertIn("HTTP 400", str(caught.exception))
        broken = json.loads(json.dumps(payload))
        broken["resourceSpans"][0]["scopeSpans"][0]["spans"][0]["spanId"] = "short"
        with self.assertRaises(ValueError):
            main.export(broken, transport=lambda u, h, b: self.fail("must not send an invalid payload"))

    def test_parse_headers(self):
        self.assertEqual(main.parse_headers(["Authorization=Basic abc", "X-Scope-OrgID=team-a"]), {"Authorization": "Basic abc", "X-Scope-OrgID": "team-a"})
        with self.assertRaises(ValueError):
            main.parse_headers(["nonsense"])


class CliTests(unittest.TestCase):
    def setUp(self):
        self.work = tempfile.mkdtemp(prefix="agenttrace-")
        self.addCleanup(shutil.rmtree, self.work)

    def run_cli(self, argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = main.main(argv)
        return code, out.getvalue(), err.getvalue()

    def convert(self, stem, seed):
        target = os.path.join(self.work, f"{stem}.otlp.json")
        code, _, err = self.run_cli(["convert", "--trace", os.path.join(FIXTURES, f"{stem}.trace.jsonl"), "--outcome", os.path.join(FIXTURES, f"{stem}.outcome.json"), "--start", "2026-11-01T02:03:00Z", "--seed", str(seed), "-o", target])
        self.assertEqual(code, 0, err)
        return target

    def test_convert_view_and_metrics_from_the_command_line(self):
        crash = self.convert("crashloop", 1)
        oom = self.convert("oomkilled", 2)
        with open(crash, encoding="utf-8") as handle:
            payload = json.load(handle)
        self.assertEqual(main.validate_otlp(payload), [])
        code, out, _ = self.run_cli(["view", crash])
        self.assertEqual(code, 0)
        self.assertIn("invoke_agent triage_agent", out)
        self.assertIn("execute_tool pod_logs", out)
        code, out, _ = self.run_cli(["metrics", crash, oom, "--json"])
        self.assertEqual(code, 0)
        report = json.loads(out)
        self.assertEqual(report["runs"], 2)
        self.assertEqual(report["runs_without_report"], 0)
        code, out, _ = self.run_cli(["convert", "--trace", os.path.join(FIXTURES, "oomkilled.trace.jsonl"), "--seed", "3"])
        self.assertEqual(code, 0)
        self.assertEqual(len(main.from_otlp(json.loads(out))), 4)

    def test_cli_fails_cleanly_on_missing_or_invalid_input(self):
        code, _, err = self.run_cli(["view", os.path.join(self.work, "missing.json")])
        self.assertEqual(code, 1)
        self.assertIn("no such file", err)
        bad = os.path.join(self.work, "bad.json")
        with open(bad, "w", encoding="utf-8") as handle:
            json.dump({"resourceSpans": [{"scopeSpans": [{"spans": [{"traceId": "x", "spanId": "y", "startTimeUnixNano": "1", "endTimeUnixNano": "2"}]}]}]}, handle)
        code, _, err = self.run_cli(["metrics", bad])
        self.assertEqual(code, 1)
        self.assertIn("traceId must be 32", err)
        empty = os.path.join(self.work, "empty.jsonl")
        open(empty, "w").close()
        code, _, err = self.run_cli(["convert", "--trace", empty])
        self.assertEqual(code, 1)
        self.assertIn("no trace lines", err)
        code, _, err = self.run_cli(["convert", "--trace", os.path.join(FIXTURES, "crashloop.trace.jsonl"), "--start", "yesterday"])
        self.assertEqual(code, 1)
        self.assertIn("ISO 8601", err)


if __name__ == "__main__":
    unittest.main()
