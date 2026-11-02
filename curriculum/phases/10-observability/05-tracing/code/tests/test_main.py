import contextlib
import gzip
import io
import json
import os
import sys
import tempfile
import threading
import unittest
import urllib.request

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import main  # noqa: E402

FIXTURE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "fixtures", "checkout.otlp.jsonl")
SPEC_EXAMPLE = "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01"


def run(argv):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = main.main(argv)
    return code, out.getvalue(), err.getvalue()


def span(name, trace="a" * 32, span_id="1" * 16, parent=None, start=0, end=0, service="svc", status=main.STATUS_UNSET, kind=main.KIND_INTERNAL):
    built = main.Span(name, main.SpanContext(trace, span_id), parent, kind, start * 1_000_000, end * 1_000_000, service)
    built.status_code = status
    return built


class ManualClock:
    def __init__(self):
        self.now = 1_000_000_000

    def __call__(self):
        return self.now


class TraceparentTests(unittest.TestCase):
    def test_the_spec_example_parses_into_its_four_fields(self):
        context = main.parse_traceparent(SPEC_EXAMPLE)
        self.assertEqual(context.trace_id, "4bf92f3577b34da6a3ce929d0e0e4736")
        self.assertEqual(context.span_id, "00f067aa0ba902b7")
        self.assertTrue(context.sampled)
        self.assertTrue(context.remote)
        self.assertFalse(main.parse_traceparent(SPEC_EXAMPLE[:-2] + "00").sampled)

    def test_invalid_headers_are_rejected_with_the_reason(self):
        cases = {
            "ff-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01": "forbidden",
            "00-00000000000000000000000000000000-00f067aa0ba902b7-01": "trace-id is all zeros",
            "00-4bf92f3577b34da6a3ce929d0e0e4736-0000000000000000-01": "parent-id is all zeros",
            "00-4BF92F3577B34DA6A3CE929D0E0E4736-00F067AA0BA902B7-01": "uppercase",
            SPEC_EXAMPLE + "-extra": "exactly four fields",
            "00-4bf92f3577b34da6a3ce929d0e0e473-00f067aa0ba902b7-01": "VERSION-TRACEID",
            "": "VERSION-TRACEID",
        }
        for header, reason in cases.items():
            with self.assertRaises(main.ContextError) as caught:
                main.parse_traceparent(header)
            self.assertIn(reason, str(caught.exception), header)

    def test_a_higher_version_parses_the_first_four_fields_and_ignores_the_rest(self):
        context = main.parse_traceparent("cc-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01-what-the-future-holds")
        self.assertEqual(context.span_id, "00f067aa0ba902b7")
        with self.assertRaises(main.ContextError):
            main.parse_traceparent("cc-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01x")

    def test_outgoing_headers_are_version_00_and_keep_only_the_sampled_flag(self):
        self.assertEqual(main.format_traceparent(main.SpanContext("4bf92f3577b34da6a3ce929d0e0e4736", "00f067aa0ba902b7", 0x03)), SPEC_EXAMPLE)
        self.assertTrue(main.format_traceparent(main.SpanContext("4bf92f3577b34da6a3ce929d0e0e4736", "00f067aa0ba902b7", 0x02)).endswith("-00"))


class TracestateAndBaggageTests(unittest.TestCase):
    def test_tracestate_combines_headers_ignores_empty_members_and_accepts_tenant_keys(self):
        entries = main.parse_tracestate(["rojo=00f067aa0ba902b7, ,congo=t61rcWkgMzE", "acme@vendor=x"])
        self.assertEqual(entries, [("rojo", "00f067aa0ba902b7"), ("congo", "t61rcWkgMzE"), ("acme@vendor", "x")])

    def test_invalid_tracestate_is_rejected_as_a_whole(self):
        for bad in (["Rojo=1"], ["rojo=1,rojo=2"], ["rojo=has,comma"], ["rojo=a=b"], ["novalue"], [",".join(f"k{i}=v" for i in range(33))]):
            with self.assertRaises(main.ContextError):
                main.parse_tracestate(bad)

    def test_updating_tracestate_moves_the_key_left_and_truncates_from_the_right(self):
        entries = [("rojo", "1"), ("congo", "2"), ("mine", "old")]
        self.assertEqual(main.update_tracestate(entries, "mine", "new"), [("mine", "new"), ("rojo", "1"), ("congo", "2")])
        full = [(f"k{i}", "v") for i in range(32)]
        updated = main.update_tracestate(full, "mine", "1")
        self.assertEqual(len(updated), 32)
        self.assertEqual(updated[0], ("mine", "1"))
        self.assertNotIn(("k31", "v"), updated)
        with_long = [("long", "x" * 200)] + [(f"k{i}", "v") for i in range(31)]
        self.assertNotIn("long", dict(main.update_tracestate(with_long, "mine", "1")))

    def test_baggage_decodes_values_keeps_properties_and_drops_bad_members(self):
        problems = []
        entries = main.parse_baggage(["user.tier = gold ;ttl=60, note=hello%20world%2C%20caf%C3%A9", "bad key=1,cc.fault=db-slow"], problems)
        self.assertEqual(entries["user.tier"], main.BaggageEntry("user.tier", "gold", ("ttl=60",)))
        self.assertEqual(entries["note"].value, "hello world, café")
        self.assertEqual(entries["cc.fault"].value, "db-slow")
        self.assertNotIn("bad key", entries)
        self.assertEqual(len(problems), 1)

    def test_baggage_is_percent_encoded_and_never_sent_partially_past_the_limits(self):
        text = main.format_baggage([main.BaggageEntry("note", "50% off, café;now")])
        self.assertEqual(text, "note=50%25%20off%2C%20caf%C3%A9%3Bnow")
        self.assertEqual(main.parse_baggage([text])["note"].value, "50% off, café;now")
        many = main.format_baggage(main.BaggageEntry(f"k{i}", "v") for i in range(70))
        self.assertEqual(len(many.split(",")), 64)
        big = main.format_baggage([main.BaggageEntry("a", "x" * 5000), main.BaggageEntry("b", "y" * 5000), main.BaggageEntry("c", "z")])
        self.assertEqual([member.split("=")[0] for member in big.split(",")], ["a", "c"])

    def test_extract_restarts_on_a_bad_traceparent_and_never_reads_its_tracestate(self):
        bad = main.extract({"traceparent": ["00-xyz"], "tracestate": ["rojo=1"], "baggage": ["cc.fault=db-error"]})
        self.assertIsNone(bad.parent)
        self.assertEqual(bad.baggage["cc.fault"].value, "db-error")
        self.assertTrue(any("new trace" in problem for problem in bad.problems))
        good = main.extract({"traceparent": [SPEC_EXAMPLE], "tracestate": ["rojo=00f067aa0ba902b7"]})
        self.assertEqual(good.parent.tracestate, (("rojo", "00f067aa0ba902b7"),))
        dropped = main.extract({"traceparent": [SPEC_EXAMPLE], "tracestate": ["rojo=1,rojo=2"]})
        self.assertEqual(dropped.parent.tracestate, ())
        self.assertTrue(any("tracestate dropped" in problem for problem in dropped.problems))
        headers = main.inject(good.parent, {"cc.fault": main.BaggageEntry("cc.fault", "db-slow")})
        self.assertEqual(headers, {"traceparent": SPEC_EXAMPLE, "tracestate": "rojo=00f067aa0ba902b7", "baggage": "cc.fault=db-slow"})


class SamplerAndTracerTests(unittest.TestCase):
    def test_trace_id_ratio_compares_the_low_64_bits_with_ratio_times_2_to_the_64(self):
        sampler = main.trace_id_ratio(0.25)
        self.assertTrue(sampler(None, "f" * 16 + "0" * 15 + "1"))
        self.assertTrue(sampler(None, "f" * 16 + "3fffffffffffffff"))
        self.assertFalse(sampler(None, "0" * 16 + "4000000000000000"))
        self.assertFalse(main.trace_id_ratio(0.0)(None, "0" * 31 + "1"))
        self.assertTrue(main.trace_id_ratio(1.0)(None, "f" * 32))
        with self.assertRaises(ValueError):
            main.trace_id_ratio(1.5)

    def test_parent_based_follows_the_incoming_flag_and_names_match_otel_traces_sampler(self):
        sampler = main.make_sampler("parentbased_traceidratio", 0.0)
        sampled_parent = main.SpanContext("a" * 32, "b" * 16, main.FLAG_SAMPLED, (), True)
        self.assertTrue(sampler(sampled_parent, "a" * 32))
        self.assertFalse(sampler(main.SpanContext("a" * 32, "b" * 16, 0), "a" * 32))
        self.assertFalse(sampler(None, "a" * 32))
        self.assertTrue(main.make_sampler("parentbased_always_on")(None, "a" * 32))
        with self.assertRaises(ValueError):
            main.make_sampler("ratio")

    def test_child_spans_share_the_trace_and_unsampled_spans_propagate_without_recording(self):
        ended = []
        tracer = main.Tracer("api", main.make_sampler("parentbased_always_on"), ended.append, main.IdGenerator(1))
        root = tracer.start_span("GET /api/cart", main.KIND_SERVER)
        child = tracer.start_span("GET /query", main.KIND_CLIENT, root.context, {"url.template": "/query"})
        child.end()
        root.end()
        self.assertEqual(child.trace_id, root.trace_id)
        self.assertEqual(child.parent_span_id, root.span_id)
        self.assertEqual([s.name for s in ended], ["GET /query", "GET /api/cart"])
        remote = main.parse_traceparent(SPEC_EXAMPLE[:-2] + "00")
        quiet = tracer.start_span("GET /api/cart", main.KIND_SERVER, remote, {"a": 1})
        quiet.set_attribute("b", 2)
        quiet.end()
        self.assertFalse(quiet.recording)
        self.assertEqual(quiet.attributes, {})
        self.assertEqual(len(ended), 2)
        self.assertTrue(main.format_traceparent(quiet.context).startswith("00-4bf92f3577b34da6a3ce929d0e0e4736-"))
        self.assertTrue(main.format_traceparent(quiet.context).endswith("-00"))

    def test_status_ok_is_final_unset_is_ignored_and_exceptions_become_events(self):
        s = main.Tracer("db").start_span("SELECT cart")
        s.set_status(main.STATUS_ERROR, "pool exhausted")
        s.set_status(main.STATUS_UNSET)
        self.assertEqual((s.status_code, s.status_message), (main.STATUS_ERROR, "pool exhausted"))
        s.set_status(main.STATUS_OK, "ignored")
        s.set_status(main.STATUS_ERROR, "later")
        self.assertEqual((s.status_code, s.status_message), (main.STATUS_OK, ""))
        s.record_exception(TimeoutError("connection pool exhausted"))
        self.assertEqual(s.events[0].attributes, {"exception.type": "TimeoutError", "exception.message": "connection pool exhausted"})

    def test_http_status_rules_differ_for_client_and_server_spans(self):
        self.assertEqual(main.http_span_status(main.KIND_SERVER, 404), main.STATUS_UNSET)
        self.assertEqual(main.http_span_status(main.KIND_CLIENT, 404), main.STATUS_ERROR)
        self.assertEqual(main.http_span_status(main.KIND_SERVER, 503), main.STATUS_ERROR)
        self.assertEqual(main.http_span_status(main.KIND_CLIENT, 0), main.STATUS_ERROR)
        self.assertEqual(main.http_span_status(main.KIND_CLIENT, 200), main.STATUS_UNSET)


class OtlpTests(unittest.TestCase):
    def test_a_span_round_trips_through_otlp_json_with_events_links_and_status(self):
        s = main.Span("GET /query", main.SpanContext("a" * 32, "b" * 16, 1, (("rojo", "1"),)), "c" * 16, main.KIND_CLIENT, 10, 20, "api", {"server.port": 8082, "ok": True, "ratio": 0.5, "tags": ["a", "b"]})
        s.events.append(main.Event("exception", 15, {"exception.type": "TimeoutError"}))
        s.links.append(main.Link("d" * 32, "e" * 16, "congo=2", {"link.reason": "batch"}))
        s.status_code, s.status_message = main.STATUS_ERROR, "HTTP 503"
        payload = main.to_otlp([s])
        raw = payload["resourceSpans"][0]["scopeSpans"][0]["spans"][0]
        self.assertEqual(raw["startTimeUnixNano"], "10")
        self.assertEqual(raw["traceState"], "rojo=1")
        self.assertEqual({a["key"]: a["value"] for a in raw["attributes"]}["server.port"], {"intValue": "8082"})
        back = main.from_otlp(json.loads(json.dumps(payload)))[0]
        self.assertEqual((back.service, back.name, back.kind, back.parent_span_id, back.status_code, back.status_message), ("api", "GET /query", main.KIND_CLIENT, "c" * 16, main.STATUS_ERROR, "HTTP 503"))
        self.assertEqual(back.attributes, s.attributes)
        self.assertEqual(back.events[0].attributes, {"exception.type": "TimeoutError"})
        self.assertEqual((back.links[0].trace_id, back.links[0].tracestate), ("d" * 32, "congo=2"))

    def test_decoding_is_lenient_about_case_numbers_and_enum_names_and_reports_bad_spans(self):
        payload = {"resourceSpans": [{"resource": {"attributes": [{"key": "service.name", "value": {"stringValue": "web"}}]}, "scopeSpans": [{"spans": [
            {"traceId": "A" * 32, "spanId": "B" * 16, "name": "x", "kind": "SPAN_KIND_SERVER", "startTimeUnixNano": 5, "endTimeUnixNano": "9", "status": {"code": "STATUS_CODE_ERROR"}, "attributes": [{"key": "n", "value": {"intValue": 3}}]},
            {"traceId": "0" * 32, "spanId": "b" * 16, "name": "zero"},
            {"traceId": "a" * 32, "spanId": "b" * 15, "name": "short"},
        ]}]}]}
        problems = []
        spans = main.from_otlp(payload, problems)
        self.assertEqual([(s.trace_id, s.kind, s.status_code, s.attributes["n"]) for s in spans], [("a" * 32, main.KIND_SERVER, main.STATUS_ERROR, 3)])
        self.assertEqual(len(problems), 2)
        with self.assertRaises(ValueError):
            main.from_otlp(payload)

    def test_post_retries_retryable_codes_honours_retry_after_and_reports_partial_success(self):
        calls, waits = [], []

        def transport(url, headers, body, timeout):
            calls.append((headers, body))
            if len(calls) == 1:
                return 503, {"Retry-After": "2"}, b"busy"
            return 200, {}, b'{"partialSuccess": {"rejectedSpans": "1", "errorMessage": "bad span"}}'

        payload = main.to_otlp([span("a"), span("b", span_id="2" * 16)])
        result = main.post_otlp("http://x/v1/traces", payload, transport, sleep=waits.append, compress=True)
        self.assertEqual((result["status"], result["attempts"], result["rejected"], result["spans"]), (200, 2, 1, 2))
        self.assertEqual(waits, [2.0])
        self.assertEqual(calls[0][0]["Content-Encoding"], "gzip")
        self.assertEqual(json.loads(gzip.decompress(calls[0][1])), payload)
        never = main.post_otlp("http://x/v1/traces", payload, lambda *a: (400, {}, b"no"), sleep=waits.append)
        self.assertEqual((never["status"], never["attempts"], never["rejected"]), (400, 1, 2))

    def test_the_batch_processor_drops_when_full_and_exports_in_batches(self):
        batches = []
        processor = main.BatchProcessor(lambda spans: batches.append(len(spans)), max_queue=5, max_batch=2)
        for index in range(7):
            processor.on_end(span(str(index)))
        processor.shutdown()
        self.assertEqual((batches, processor.dropped, processor.exported), ([2, 2, 1], 2, 5))


class TailSamplingTests(unittest.TestCase):
    def test_fnv1a_64_matches_the_published_test_vectors(self):
        self.assertEqual(main.fnv1a_64(b""), 0xCBF29CE484222325)
        self.assertEqual(main.fnv1a_64(b"a"), 0xAF63DC4C8601EC8C)
        self.assertEqual(main.fnv1a_64(b"foobar"), 0x85944171F73967E8)

    def test_the_threshold_follows_the_collectors_big_float_arithmetic(self):
        self.assertEqual(main.probabilistic_threshold(100), main.MAX_UINT64)
        self.assertEqual(main.probabilistic_threshold(50), 2**63 - 1)
        self.assertEqual(main.probabilistic_threshold(25), 2**62 - 1)
        self.assertEqual(main.probabilistic_threshold(0), 0)
        tenth = main.probabilistic_threshold(10)
        self.assertLessEqual(abs(tenth - main.MAX_UINT64 // 10), 2**11)

    def test_errors_and_latency_keep_a_trace_and_the_hash_samples_the_rest(self):
        policy = main.TailPolicy(True, 300, 0.0)
        ok = [span("root", start=0, end=299), span("child", span_id="2" * 16, parent="1" * 16, start=1, end=299.9999)]
        self.assertEqual(main.tail_reasons("a" * 32, ok, policy), [])
        slow = ok + [span("late", span_id="3" * 16, parent="1" * 16, start=10, end=300)]
        self.assertEqual(main.tail_reasons("a" * 32, slow, policy), ["slow"])
        failed = ok + [span("db", span_id="4" * 16, parent="1" * 16, start=2, end=3, status=main.STATUS_ERROR)]
        self.assertEqual(main.tail_reasons("a" * 32, failed, policy), ["errors"])
        sampled = main.TailPolicy(False, None, 25.0, "cc-smoke")
        ids = [main.IdGenerator(3).trace_id() for _ in range(1)] + [main.IdGenerator(seed).trace_id() for seed in range(400)]
        kept = [tid for tid in ids if main.tail_reasons(tid, ok, sampled) == ["sample"]]
        self.assertTrue(60 < len(kept) < 140, len(kept))
        self.assertEqual(kept, [tid for tid in ids if main.trace_hash(tid, "cc-smoke") <= 2**62 - 1])
        everything = main.TailPolicy(False, None, 100.0)
        self.assertTrue(all(main.tail_reasons(tid, ok, everything) == ["sample"] for tid in ids[:20]))

    def test_decisions_wait_for_the_window_and_late_spans_follow_the_decision(self):
        now = [0.0]
        sampler = main.TailSampler(main.TailPolicy(True, None, 0.0), 3.0, lambda: now[0])
        good, bad = "a" * 32, "b" * 32
        sampler.add([span("root", trace=good, start=0, end=5), span("root", trace=bad, start=0, end=5, status=main.STATUS_ERROR)])
        now[0] = 2.9
        self.assertEqual(sampler.tick(), [])
        now[0] = 3.0
        decided = {d.trace_id: d for d, spans in sampler.tick()}
        self.assertEqual((decided[good].keep, decided[bad].keep, decided[bad].reasons), (False, True, ["errors"]))
        late = sampler.add([span("late", trace=bad, span_id="9" * 16, parent="1" * 16), span("late", trace=good, span_id="9" * 16, parent="1" * 16)])
        self.assertEqual([s.trace_id for s in late], [bad])
        self.assertEqual((sampler.decisions[good].late_spans, sampler.decisions[bad].late_spans), (1, 1))


class AnalysisTests(unittest.TestCase):
    def test_the_critical_path_follows_the_last_finishing_child_and_sums_to_the_root(self):
        spans = [
            span("GET /checkout", span_id="1" * 16, start=0, end=100, service="frontend"),
            span("GET /api/cart", span_id="2" * 16, parent="1" * 16, start=10, end=90, service="frontend"),
            span("GET /api/recommendations", span_id="3" * 16, parent="1" * 16, start=20, end=95, service="frontend"),
            span("score", span_id="4" * 16, parent="3" * 16, start=30, end=80, service="api"),
            span("fire-and-forget", span_id="5" * 16, parent="1" * 16, start=96, end=150, service="frontend"),
        ]
        path = main.critical_path(spans)
        self.assertEqual(sum(segment.ns for segment in path), 100 * 1_000_000)
        self.assertEqual([(item["name"], item["ms"]) for item in main.merge_path(path)], [
            ("GET /checkout", 10.0), ("GET /api/cart", 10.0), ("GET /api/recommendations", 10.0), ("score", 50.0), ("GET /api/recommendations", 15.0), ("GET /checkout", 1.0), ("fire-and-forget", 4.0),
        ])
        summary = main.summarize(spans)
        self.assertEqual(summary["slowest_hop"]["name"], "score")
        self.assertEqual(summary["critical_ms_by_service"], {"api": 50.0, "frontend": 50.0})
        self.assertEqual(main.tail_reasons("a" * 32, spans, main.TailPolicy(False, 150)), ["slow"])

    def test_self_time_subtracts_the_union_of_children_clipped_to_the_parent(self):
        parent = span("p", span_id="1" * 16, start=0, end=100)
        children = [span("a", span_id="2" * 16, parent="1" * 16, start=10, end=50), span("b", span_id="3" * 16, parent="1" * 16, start=40, end=60), span("c", span_id="4" * 16, parent="1" * 16, start=90, end=130)]
        self.assertEqual(main.self_time_ns(parent, children), 40 * 1_000_000)

    def test_a_missing_parent_breaks_the_tree_and_is_named(self):
        spans = [span("GET /checkout", span_id="1" * 16, parent="f" * 16, start=0, end=10), span("GET /api/cart", span_id="2" * 16, parent="1" * 16, start=1, end=9)]
        roots, missing = main.trace_roots(spans)
        self.assertEqual(([r.name for r in roots], missing), (["GET /checkout"], ["f" * 16]))
        self.assertFalse(main.summarize(spans)["single_tree"])
        self.assertIn("missing parent span(s) ffffffffffffffff", main.waterfall(spans))

    def test_analyze_reads_the_fixture_and_strict_mode_fails_on_the_broken_trace(self):
        code, out, _ = run(["analyze", FIXTURE, "--json"])
        self.assertEqual(code, 0)
        report = json.loads(out)
        hops = [(t["slowest_hop"]["service"], t["slowest_hop"]["name"], t["single_tree"], t["spans"]) for t in report["traces"]]
        self.assertEqual(hops, [("db", "SELECT cart", True, 10), ("api", "score", True, 10), ("api", "score", False, 9)])
        self.assertEqual(report["traces"][1]["errors"][-1], "db SELECT cart")
        self.assertEqual(report["broken"], [report["traces"][2]["trace_id"]])
        code, out, _ = run(["analyze", FIXTURE, "--strict"])
        self.assertEqual(code, 1)
        self.assertIn("BROKEN", out)
        code, out, _ = run(["analyze", FIXTURE, "--trace", report["traces"][0]["trace_id"][:8]])
        self.assertIn("db SELECT cart 400.4 ms", out)
        self.assertEqual(run(["analyze", "/no/such/file.json"])[0], 2)


class ServiceTests(unittest.TestCase):
    def setUp(self):
        self.spans = []
        self.logs = []
        self.servers = []
        delays = {"db": 1.0, "score": 2.0, "db-slow": 120.0, "api-slow": 90.0}
        db = self.start(main.ServiceConfig("db", delays_ms=delays))
        api = self.start(main.ServiceConfig("api", db_url=db, delays_ms=delays))
        self.frontend = self.start(main.ServiceConfig("frontend", api_url=api, delays_ms=delays))

    def start(self, config):
        tracer = main.Tracer(config.name, main.make_sampler("parentbased_always_on"), self.spans.append)
        service = main.TracedService(config, tracer, log=self.logs.append)
        server = main.make_server(service.app)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.servers.append(server)
        return f"http://127.0.0.1:{server.server_address[1]}"

    def tearDown(self):
        for server in self.servers:
            server.shutdown()
            server.server_close()

    def load(self, pattern, seed=11):
        tracer = main.Tracer("loadgen", main.always_on, self.spans.append, main.IdGenerator(seed))
        return main.run_load(self.frontend + "/checkout", len(main.parse_pattern(pattern)), main.parse_pattern(pattern), tracer)

    def test_one_request_is_one_tree_across_four_services_with_baggage_everywhere(self):
        result = self.load("db-slow")[0]
        self.assertEqual(result["status"], 200)
        trace = main.group_traces(self.spans)[result["trace_id"]]
        summary = main.summarize(trace)
        self.assertTrue(summary["single_tree"])
        self.assertEqual((summary["spans"], summary["services"]), (10, ["api", "db", "frontend", "loadgen"]))
        self.assertEqual((summary["slowest_hop"]["service"], summary["slowest_hop"]["name"]), ("db", "SELECT cart"))
        self.assertGreaterEqual(summary["slowest_hop"]["ms"], 115)
        servers = [s for s in trace if s.kind == main.KIND_SERVER]
        self.assertTrue(all(s.attributes["cc.fault"] == "db-slow" for s in servers))
        by_id = {s.span_id: s for s in trace}
        self.assertTrue(all(by_id[s.parent_span_id].kind == main.KIND_CLIENT for s in servers))

    def test_a_db_error_marks_the_failing_path_and_a_slow_api_moves_the_critical_path(self):
        failed, slow = self.load("db-error,api-slow")
        self.assertEqual((failed["status"], slow["status"]), (502, 200))
        traces = main.group_traces(self.spans)
        errors = sorted(f"{s.service} {s.name}" for s in traces[failed["trace_id"]] if s.status_code == main.STATUS_ERROR)
        self.assertEqual(errors, ["api GET /api/cart", "api GET /query", "db GET /query", "db SELECT cart", "frontend GET /api/cart", "frontend GET /checkout", "loadgen GET /checkout"])
        events = [e.name for s in traces[failed["trace_id"]] for e in s.events]
        self.assertIn("exception", events)
        hop = main.summarize(traces[slow["trace_id"]])["slowest_hop"]
        self.assertEqual((hop["service"], hop["name"]), ("api", "score"))

    def test_an_unsampled_request_records_nothing_but_still_carries_its_context(self):
        result = self.load("unsampled+db-error")[0]
        self.assertEqual(result["status"], 502)
        self.assertEqual(self.spans, [])
        db_lines = [line for line in self.logs if line.startswith("db ")]
        self.assertEqual(len(db_lines), 1)
        self.assertIn(f"traceparent=00-{result['trace_id']}-", db_lines[0])
        self.assertIn("-00 ", db_lines[0])
        self.assertIn("sampled=false status=503", db_lines[0])

    def test_an_untrusted_frontend_starts_a_new_trace_and_links_to_the_caller(self):
        tracer = main.Tracer("frontend", main.always_on, self.spans.append)
        edge = main.TracedService(main.ServiceConfig("frontend", api_url="http://127.0.0.1:9", trust_incoming=False), tracer, http_get=lambda url, headers: (200, {}), log=self.logs.append)
        status, _ = edge.app("GET", "/checkout", {"traceparent": [SPEC_EXAMPLE]}, b"")
        self.assertEqual(status, 200)
        server = [s for s in self.spans if s.kind == main.KIND_SERVER][0]
        self.assertNotEqual(server.trace_id, "4bf92f3577b34da6a3ce929d0e0e4736")
        self.assertIsNone(server.parent_span_id)
        self.assertEqual((server.links[0].trace_id, server.links[0].span_id), ("4bf92f3577b34da6a3ce929d0e0e4736", "00f067aa0ba902b7"))
        self.assertEqual(main.summarize([server])["links"][0]["span_id"], "00f067aa0ba902b7")

    def test_unknown_routes_are_404_with_an_unset_server_status(self):
        tracer = main.Tracer("api", main.always_on, self.spans.append)
        status, _ = main.TracedService(main.ServiceConfig("api"), tracer, log=self.logs.append).app("GET", "/nope", {}, b"")
        self.assertEqual(status, 404)
        self.assertEqual((self.spans[0].name, self.spans[0].status_code), ("GET", main.STATUS_UNSET))
        with self.assertRaises(ValueError):
            main.TracedService(main.ServiceConfig("cache"), tracer)
        with self.assertRaises(ValueError):
            main.parse_pattern("ok,db-slwo")


class CollectorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.out = os.path.join(self.tmp.name, "kept.jsonl")
        self.collector = main.Collector(main.TailSampler(main.TailPolicy(True, 300, 0.0), 0.0), self.out)
        self.server = main.make_server(self.collector.app)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.tmp.cleanup()

    def post(self, body, content_type="application/json", encoding=None):
        headers = {"Content-Type": content_type}
        if encoding:
            headers["Content-Encoding"] = encoding
        return main.urllib_transport(self.url + "/v1/traces", headers, body, 5.0)

    def test_the_collector_accepts_json_and_gzip_rejects_protobuf_and_garbage(self):
        payload = main.to_otlp([span("root", trace="b" * 32, start=0, end=400), span("db", trace="c" * 32, start=0, end=1, status=main.STATUS_ERROR), span("fast", trace="d" * 32, start=0, end=1)])
        result = main.post_otlp(self.url + "/v1/traces", payload, compress=True)
        self.assertEqual((result["status"], result["rejected"]), (200, 0))
        self.assertEqual(self.post(b"\x0a\x00", "application/x-protobuf")[0], 415)
        self.assertEqual(self.post(b"{not json")[0], 400)
        self.assertEqual(self.post(b"plain", encoding="gzip")[0], 400)
        status, _, body = self.post(json.dumps({"resourceSpans": [{"scopeSpans": [{"spans": [{"traceId": "0" * 32, "spanId": "1" * 16}]}]}]}).encode())
        self.assertEqual((status, json.loads(body)["partialSuccess"]["rejectedSpans"]), (200, "1"))
        self.collector.tick()
        with urllib.request.urlopen(self.url + "/api/decisions") as response:
            decisions = json.loads(response.read())
        self.assertEqual((decisions["kept"], decisions["pending"], decisions["rejected_spans"]), (["b" * 32, "c" * 32], 0, 1))
        self.assertEqual(decisions["decisions"]["b" * 32]["reasons"], ["slow"])
        kept = main.group_traces(main.load_otlp_files([self.out]))
        self.assertEqual(sorted(kept), ["b" * 32, "c" * 32])
        with urllib.request.urlopen(self.url + "/api/traces/" + "b" * 32) as response:
            self.assertEqual(json.loads(response.read())["decision"], {"keep": True, "reasons": ["slow"]})


class CliTests(unittest.TestCase):
    def test_context_prints_the_fields_and_the_next_hop(self):
        code, out, _ = run(["context", "--traceparent", SPEC_EXAMPLE, "--tracestate", "congo=t61rcWkgMzE", "--set-tracestate", "cc=1", "--baggage", "cc.fault=db-slow", "--seed", "1", "--json"])
        self.assertEqual(code, 0)
        result = json.loads(out)
        self.assertEqual(result["incoming"]["parent_id"], "00f067aa0ba902b7")
        self.assertTrue(result["outgoing"]["traceparent"].startswith("00-4bf92f3577b34da6a3ce929d0e0e4736-"))
        self.assertNotIn("00f067aa0ba902b7", result["outgoing"]["traceparent"])
        self.assertEqual(result["outgoing"]["tracestate"], "cc=1,congo=t61rcWkgMzE")
        self.assertEqual(result["outgoing"]["baggage"], "cc.fault=db-slow")

    def test_context_exits_1_on_an_invalid_traceparent_and_restarts_the_trace(self):
        code, out, _ = run(["context", "--traceparent", "00-4bf92f3577b34da6a3ce929d0e0e4736-0000000000000000-01"])
        self.assertEqual(code, 1)
        self.assertIn("parent-id is all zeros", out)
        self.assertIn("new trace", out)
        self.assertEqual(run(["context", "--set-tracestate", "Bad=1"])[0], 2)


if __name__ == "__main__":
    unittest.main()
