import contextlib
import io
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import main  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
FIXTURE = os.path.join(HERE, "..", "fixtures", "replay-deploy.json")
T0 = main.parse_timestamp("2026-11-01T10:00:00Z")


def at(seconds, text):
    stamp = main.datetime.fromtimestamp(T0 + seconds, tz=main.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return f"{stamp} {text}"


def mined(lines, **kwargs):
    return main.mine(lines, main.Miner(**kwargs), default_year=2026)


class ParseTests(unittest.TestCase):
    def test_iso_timestamp_and_level_are_stripped_from_the_body(self):
        line = main.parse_line("2026-11-01T10:20:03.120Z ERROR checkout config: missing key DATABASE_URL")
        self.assertEqual(line.ts, T0 + 20 * 60 + 3.12)
        self.assertEqual(line.level, "ERROR")
        self.assertEqual(line.body, "checkout config: missing key DATABASE_URL")
        bracketed = main.parse_line("[2026-11-01 10:00:00,500] [WARNING] disk 91% full")
        self.assertEqual((bracketed.ts, bracketed.level, bracketed.body), (T0 + 0.5, "WARN", "disk 91% full"))
        nanos = main.parse_line("2026-11-01T10:00:01.123456789Z INFO started")
        self.assertAlmostEqual(nanos.ts, T0 + 1.123456, places=5)
        logfmt = main.parse_line("2026-11-01T10:00:02Z level=error msg=boom pod=x")
        self.assertEqual((logfmt.level, logfmt.body), ("ERROR", "msg=boom pod=x"))

    def test_json_lines_yield_message_level_time_and_the_remaining_fields(self):
        line = main.parse_line('{"ts":"2026-11-01T10:00:05Z","level":"warn","msg":"retrying","attempt":3,"pod":"checkout-1","caller":"client.go:42"}')
        self.assertEqual((line.ts, line.level, line.source), (T0 + 5, "WARN", "client.go:42"))
        self.assertEqual(line.body, "retrying attempt=3 pod=checkout-1")
        epoch = main.parse_line('{"time":1761993600,"severity":"ERROR","message":"db down"}')
        self.assertEqual((epoch.ts, epoch.level, epoch.body), (1761993600.0, "ERROR", "db down"))
        self.assertEqual(main.parse_line("{not json").body, "{not json")

    def test_klog_and_kubectl_prefix_give_level_source_and_time(self):
        line = main.parse_line("[pod/coredns-5d7/coredns] I1101 10:05:00.123456       1 reload.go:75] Reloading complete", default_year=2026)
        self.assertEqual(line.level, "INFO")
        self.assertEqual(line.source, "pod/coredns-5d7/coredns")
        self.assertEqual(line.body, "Reloading complete")
        self.assertEqual(line.ts, T0 + 5 * 60 + 0.123456)
        bare = main.parse_line("E1101 10:06:00.000000       1 controller.go:12] sync failed", default_year=2026)
        self.assertEqual((bare.level, bare.source, bare.ts), ("ERROR", "controller.go:12", T0 + 6 * 60))

    def test_syslog_and_access_log_timestamps(self):
        syslog = main.parse_line("Nov  1 10:07:00 node1 sshd[812]: Accepted publickey for root", default_year=2026)
        self.assertEqual((syslog.ts, syslog.source, syslog.body), (T0 + 7 * 60, "node1 sshd", "Accepted publickey for root"))
        access = main.parse_line('10.0.0.9 - - [01/Nov/2026:12:08:00 +0200] "GET /api/v1/orders HTTP/1.1" 200 512')
        self.assertEqual(access.ts, T0 + 8 * 60)
        self.assertEqual(access.body, '10.0.0.9 - - "GET /api/v1/orders HTTP/1.1" 200 512')
        self.assertIsNone(access.level)

    def test_lines_without_a_timestamp_inherit_the_previous_one_or_use_line_numbers(self):
        miner = mined([at(0, "ERROR boom"), '  File "/app/x.py", line 1', at(120, "INFO ok")])
        trace = next(c for c in miner.clusters if c.tokens[0] == "File")
        self.assertEqual((trace.first_seen, miner.synthetic_time), (T0, False))
        bare = mined(["alpha 1", "beta 2", "gamma 3"])
        self.assertTrue(bare.synthetic_time)
        self.assertEqual((bare.start, bare.end), (0.0, 2.0))


class MaskTests(unittest.TestCase):
    def test_variables_become_typed_placeholders(self):
        tokens = main.mask("user 3f2a9c1e-1234-4abc-9def-0123456789ab from 10.1.2.3:443 read /etc/app.yml in 120ms id deadbeefcafe seq 42 url https://x.io/a")
        self.assertEqual(tokens, ["user", "<UUID>", "from", "<IP>", "read", "<PATH>", "in", "<NUM>", "id", "<HEX>", "seq", "<NUM>", "url", "<URL>"])
        self.assertEqual(main.mask('said "hello world" to \'bob smith\' pod checkout-7d9f-x2k9p'), ["said", "<STR>", "to", "<STR>", "pod", "<ID>"])

    def test_logfmt_keys_survive_and_only_values_are_masked(self):
        self.assertEqual(main.mask("duration=120ms status=200 user=alice pod=checkout-7d9f image=sha256:0123456789abcdef"), ["duration=<NUM>", "status=<NUM>", "user=alice", "pod=<ID>", "image=<HEX>"])

    def test_surrounding_punctuation_is_kept_outside_the_placeholder(self):
        self.assertEqual(main.mask("(release checkout-5b8c7), addr=10.0.0.1:8080; took 3.5s."), ["(release", "<ID>),", "addr=<IP>;", "took", "<NUM>."])
        self.assertEqual(main.mask("can't connect to db"), ["can't", "connect", "to", "db"])


class ClusterTests(unittest.TestCase):
    def test_lines_with_the_same_template_form_one_cluster_with_counts_and_examples(self):
        miner = mined([at(i, f"INFO http request path=/api/v1/orders status=200 duration={i * 7}ms") for i in range(5)], max_examples=2)
        self.assertEqual(len(miner.clusters), 1)
        cluster = miner.clusters[0]
        self.assertEqual(cluster.template, "http request path=<PATH> status=<NUM> duration=<NUM>")
        self.assertEqual((cluster.count, cluster.level, len(cluster.examples)), (5, "INFO", 2))
        self.assertEqual((cluster.first_seen, cluster.last_seen), (T0, T0 + 4))
        self.assertEqual(cluster.buckets, {int(T0): 5})

    def test_a_differing_token_becomes_a_wildcard_when_the_lines_are_similar_enough(self):
        miner = mined([at(0, "INFO cache miss key=product:1 region=eu"), at(1, "INFO cache miss key=product:2 region=us")])
        self.assertEqual(len(miner.clusters), 1)
        self.assertEqual(miner.clusters[0].template, "cache miss key=<ID> region=<*>")
        self.assertEqual(main.similarity(["a", "<*>", "c"], ["a", "x", "c"]), 2 / 3)
        self.assertEqual(main.merge(["a", "b", "c"], ["a", "x", "c"]), ["a", "<*>", "c"])

    def test_dissimilar_or_differently_long_lines_stay_apart(self):
        miner = mined([at(0, "INFO cache miss key=product:1"), at(1, "INFO cache hit key=product:1 ttl=30s"), at(2, "ERROR upstream connection reset by peer addr=10.0.0.1")])
        self.assertEqual(len(miner.clusters), 3)
        self.assertEqual(sorted(len(c.tokens) for c in miner.clusters), [3, 4, 6])

    def test_threshold_controls_how_eagerly_lines_merge(self):
        lines = [at(0, "INFO job a finished"), at(1, "INFO job b failed")]
        self.assertEqual(len(mined(lines, threshold=0.3).clusters), 1)
        self.assertEqual(mined(lines, threshold=0.3).clusters[0].template, "job <*> <*>")
        self.assertEqual(len(mined(lines, threshold=0.5).clusters), 2)
        self.assertEqual(main.merge_token("region=eu", "region=us"), "region=<*>")
        self.assertEqual(main.merge_token("finished", "failed"), "<*>")


class AnalysisTests(unittest.TestCase):
    def scenario(self):
        lines = []
        for minute in range(10):
            lines += [at(minute * 60 + s, "INFO health check ok pod=checkout-abc") for s in range(0, 60, 10)]
            lines.append(at(minute * 60 + 5, "ERROR upstream connection reset by peer addr=10.0.0.1"))
            if minute < 6:
                lines.append(at(minute * 60 + 7, "WARN slow query duration=900ms"))
        for minute in range(6, 10):
            lines += [at(minute * 60 + s, "ERROR checkout config: missing key DATABASE_URL (release checkout-5b8c7)") for s in range(0, 60, 4)]
            lines += [at(minute * 60 + s, "WARN upstream timeout service=payments after 3000ms") for s in range(0, 60, 15)]
        for minute in range(0, 6):
            lines.append(at(minute * 60 + 30, "WARN upstream timeout service=payments after 2000ms"))
        return sorted(lines)

    def test_novel_spiking_and_vanished_templates_are_flagged_against_the_marker(self):
        analysis = main.analyse(mined(self.scenario()), marker=T0 + 6 * 60)
        by_template = {r.template: r for r in analysis.ranked}
        novel = by_template["checkout config: missing key DATABASE_URL (release <ID>)"]
        self.assertTrue(novel.novel)
        self.assertEqual((novel.before, novel.after, novel.spike), (0, 60, None))
        spiking = by_template["upstream timeout service=payments after <NUM>"]
        self.assertFalse(spiking.novel)
        self.assertEqual((spiking.before, spiking.after), (6, 16))
        self.assertAlmostEqual(spiking.spike, 4.0)
        self.assertEqual((spiking.before_per_min, spiking.after_per_min), (1.0, 4.0))
        vanished = by_template["slow query duration=<NUM>"]
        self.assertTrue(vanished.vanished)
        self.assertEqual(vanished.after, 0)
        self.assertEqual(analysis.marker_source, "flag")
        self.assertEqual(analysis.marker, T0 + 6 * 60)

    def test_ranking_puts_the_new_error_first_then_the_spike_then_steady_noise(self):
        analysis = main.analyse(mined(self.scenario()), marker=T0 + 6 * 60)
        order = [r.template for r in analysis.ranked]
        self.assertEqual(order[0], "checkout config: missing key DATABASE_URL (release <ID>)")
        self.assertEqual(order[1], "upstream timeout service=payments after <NUM>")
        self.assertEqual(order[2], "upstream connection reset by peer addr=<IP>", "a steady ERROR ranks below anything new or spiking")
        self.assertEqual(order[3], "slow query duration=<NUM>", "a template that vanished still beats steady INFO noise")
        self.assertEqual(order[4], "health check ok pod=checkout-abc")
        self.assertEqual([r.id for r in analysis.ranked], ["T01", "T02", "T03", "T04", "T05"])
        self.assertEqual(main.rank_score(True, False, None, "ERROR", 99), 10.0)
        self.assertEqual(main.rank_score(False, False, 4.0, "WARN", 9), 4.0)
        self.assertEqual(main.rank_score(False, False, 1.0, "INFO", 0), 0.0)
        self.assertEqual(main.rank_score(False, False, 1000.0, "INFO", 0), 4.0)

    def test_without_a_marker_the_midpoint_is_used_and_shapes_follow_the_buckets(self):
        analysis = main.analyse(mined(self.scenario()))
        self.assertEqual(analysis.marker_source, "midpoint")
        self.assertEqual(analysis.marker, T0 + 4 * 60)
        novel = next(r for r in analysis.ranked if "DATABASE_URL" in r.template)
        self.assertEqual(len(novel.shape), 10)
        self.assertTrue(novel.shape.startswith("······"))
        self.assertEqual(novel.shape[-4:], "████")
        self.assertEqual(main.sparkline([0, 1, 8, 4]), "·▁█▄")
        self.assertLessEqual(len(main.sparkline(list(range(100)), width=40)), 40)
        self.assertEqual(main.sparkline([]), "")
        with self.assertRaises(ValueError):
            main.analyse(main.Miner())


class DigestTests(unittest.TestCase):
    def test_digest_fits_the_budget_and_drops_templates_to_make_room(self):
        analysis = main.analyse(mined(AnalysisTests().scenario()), marker=T0 + 6 * 60)
        full = main.build_digest(analysis, budget=4000, top=10)
        self.assertEqual((full.shown, full.total), (5, 5))
        self.assertLessEqual(full.tokens, 4000)
        self.assertEqual(full.tokens, main.estimate_tokens(full.text))
        self.assertIn("T01  score", full.text)
        self.assertIn("NEW", full.text)
        self.assertIn("GONE", full.text)
        self.assertIn("(5 of 5 templates shown; budget 4000 tokens)", full.text)
        small = main.build_digest(analysis, budget=350, top=10)
        self.assertLess(small.shown, full.shown)
        self.assertGreaterEqual(small.shown, 1)
        self.assertLessEqual(small.tokens, 350)
        self.assertIn("DATABASE_URL", small.text)
        self.assertEqual(main.build_digest(analysis, budget=4000, top=2).shown, 2)

    def test_digest_examples_and_templates_are_redacted(self):
        lines = [at(i, f"INFO auth: issued session for user=alice token=eyJhbGciOiJIUzI1NiJ9.abc{i}def password=hunter2 from 10.0.0.{i}") for i in range(3)]
        analysis = main.analyse(mined(lines))
        digest = main.build_digest(analysis, budget=2000)
        self.assertNotIn("eyJ", digest.text)
        self.assertNotIn("hunter2", digest.text)
        self.assertNotIn("10.0.0.", digest.text)
        self.assertIn("token=<REDACTED>", digest.text)
        self.assertIn("password=<REDACTED>", digest.text)

    def test_token_estimate_and_the_raw_versus_digest_report(self):
        self.assertEqual(main.estimate_tokens(""), 0)
        self.assertEqual(main.estimate_tokens("abcd"), 1)
        self.assertEqual(main.estimate_tokens("abcde"), 2)
        analysis = main.analyse(mined(AnalysisTests().scenario()), marker=T0 + 6 * 60)
        digest = main.build_digest(analysis, budget=4000)
        stats = main.render_stats(analysis, digest, "claude-opus-5-5")
        self.assertIn(f"raw logs: {analysis.lines} lines", stats)
        self.assertIn("x smaller", stats)
        self.assertEqual(main.input_price("claude-opus-5-5", 1_000_000), 4.0)
        self.assertEqual(main.input_price("claude-haiku-5-5", 1_000_000), 0.10)
        self.assertEqual(main.input_price("unknown", 5), 0.0)


class RedactTests(unittest.TestCase):
    def test_secrets_bearer_tokens_emails_and_long_blobs_are_masked_but_digests_and_uuids_are_not(self):
        text = 'password=hunter2 api_key: "AKIA123" Authorization: Bearer eyJabc.def-ghi hdr Bearer eyJxyz.123 cookie=sid=1 mail=root@example.com blob=QUJDREVGR0hJSktMTU5PUFFSU1RVVldYWVow1234 id=3f2a9c1e-1234-4abc-9def-0123456789ab image=sha256:0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef from 10.1.2.3'
        out = main.redact(text)
        for secret in ("hunter2", "AKIA123", "eyJabc", "eyJxyz", "sid=1", "root@example.com", "QUJDREVG", "10.1.2.3"):
            self.assertNotIn(secret, out)
        self.assertIn("Authorization: <REDACTED>", out)
        self.assertIn("hdr Bearer <REDACTED>", out)
        self.assertIn("<EMAIL>", out)
        self.assertIn("id=3f2a9c1e-1234-4abc-9def-0123456789ab", out)
        self.assertIn("sha256:0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef", out)
        self.assertIn("from <IP>", out)
        self.assertEqual(main.redact("from 10.1.2.3", ips=False), "from 10.1.2.3")


class ModelTests(unittest.TestCase):
    def report(self, **overrides):
        base = {"headline": "checkout cannot read DATABASE_URL after the release", "likely_cause": "renamed config key", "confidence": "high", "severity": "sev2", "affected_components": ["checkout"], "evidence": ["T01", "T02"], "noise": ["T03"], "next_checks": ["kubectl get configmap checkout -o yaml"]}
        base.update(overrides)
        return base

    def tool_turn(self, report, usage=None):
        return {"stop_reason": "tool_use", "content": [{"type": "text", "text": "One story."}, {"type": "tool_use", "id": "toolu_1", "name": "explain_digest", "input": report}], "usage": usage or {"input_tokens": 2000, "output_tokens": 150}}

    def test_build_request_has_the_messages_api_shape(self):
        request = main.build_request("LOG DIGEST\nT01 ...", "claude-opus-5-5")
        self.assertEqual(request["model"], "claude-opus-5-5")
        self.assertEqual(request["max_tokens"], 16000)
        self.assertEqual(request["output_config"], {"effort": "medium"})
        self.assertEqual(request["fallbacks"], "default")
        self.assertEqual(request["system"][0]["cache_control"], {"type": "ephemeral"})
        self.assertEqual([tool["name"] for tool in request["tools"]], ["explain_digest"])
        self.assertTrue(request["tools"][0]["strict"])
        self.assertFalse(request["tools"][0]["input_schema"]["additionalProperties"])
        self.assertEqual(sorted(request["tools"][0]["input_schema"]["required"]), sorted(request["tools"][0]["input_schema"]["properties"]))
        for forbidden in ("thinking", "temperature", "top_p", "top_k", "tool_choice"):
            self.assertNotIn(forbidden, request)
        self.assertEqual(request["messages"][0]["role"], "user")
        self.assertIn("<digest>\nLOG DIGEST\nT01 ...\n</digest>", request["messages"][0]["content"])
        self.assertNotIn("fallbacks", main.build_request("x", fallbacks=False))

    def test_explain_reads_the_tool_call_and_flags_unknown_template_ids(self):
        model = main.ScriptedModel([self.tool_turn(self.report(evidence=["T01", "T09"]))])
        outcome = main.explain(main.Digest("LOG DIGEST", 3, 2, 2), model, ["T01", "T02", "T03"], clock=iter(range(10)).__next__)
        self.assertEqual(outcome.stop, "report")
        self.assertEqual(outcome.report["severity"], "sev2")
        self.assertEqual(outcome.unknown_refs, ["T09"])
        self.assertEqual(outcome.text, "One story.")
        self.assertEqual((outcome.usage.input_tokens, outcome.usage.output_tokens), (2000, 150))
        self.assertAlmostEqual(outcome.cost_usd, (2000 * 4 + 150 * 20) / 1e6)
        self.assertEqual(len(model.requests), 1)
        self.assertIn("WARNING references templates that are not in the digest: T09", main.render_explanation(outcome))
        self.assertIn("EXPLANATION [sev2] confidence high", main.render_explanation(outcome))

    def test_explain_handles_text_refusal_max_tokens_and_pause_turn(self):
        prose = main.explain(main.Digest("d", 1, 1, 1), main.ScriptedModel([{"stop_reason": "end_turn", "content": [{"type": "text", "text": "Looks like config."}], "usage": {}}]), ["T01"])
        self.assertEqual((prose.stop, prose.report, prose.text), ("text", None, "Looks like config."))
        self.assertIn("answered in prose", main.render_explanation(prose))
        refused = main.explain(main.Digest("d", 1, 1, 1), main.ScriptedModel([{"stop_reason": "refusal", "stop_details": {"type": "refusal", "category": "cyber"}, "content": [], "usage": {}}]), ["T01"])
        self.assertEqual((refused.stop, refused.refusal["category"], refused.report), ("refusal", "cyber", None))
        cut = main.explain(main.Digest("d", 1, 1, 1), main.ScriptedModel([{"stop_reason": "max_tokens", "content": [{"type": "tool_use", "id": "x", "name": "explain_digest", "input": {}}], "usage": {}}]), ["T01"])
        self.assertEqual((cut.stop, cut.report), ("max_tokens", None))
        paused = main.ScriptedModel([{"stop_reason": "pause_turn", "content": [{"type": "text", "text": "working"}], "usage": {"input_tokens": 10}}, self.tool_turn(self.report())])
        outcome = main.explain(main.Digest("d", 1, 1, 1), paused, ["T01", "T02", "T03"])
        self.assertEqual((outcome.stop, len(paused.requests)), ("report", 2))
        self.assertEqual([m["role"] for m in paused.requests[1]["messages"]], ["user", "assistant"])
        self.assertEqual(outcome.usage.input_tokens, 2010)

    def test_transport_receives_headers_and_body(self):
        seen = {}

        def transport(url, headers, body):
            seen.update(url=url, headers=headers, body=json.loads(body))
            return json.dumps({"stop_reason": "end_turn", "content": [], "usage": {}}).encode()

        response = main.anthropic_messages(main.build_request("d"), api_key="sk-test", transport=transport)
        self.assertEqual(response["stop_reason"], "end_turn")
        self.assertEqual(seen["url"], "https://api.anthropic.com/v1/messages")
        self.assertEqual(seen["headers"]["x-api-key"], "sk-test")
        self.assertEqual(seen["headers"]["anthropic-version"], "2026-10-21")
        self.assertEqual(seen["headers"]["anthropic-beta"], "server-side-fallback-2026-07-01")
        self.assertEqual(seen["body"]["fallbacks"], "default")
        plain = {}
        main.anthropic_messages(main.build_request("d", fallbacks=False), api_key="k", transport=lambda u, h, b: plain.update(h) or b'{"stop_reason":"end_turn","content":[],"usage":{}}')
        self.assertNotIn("anthropic-beta", plain)
        saved = os.environ.pop("ANTHROPIC_API_KEY", None)
        try:
            with self.assertRaises(RuntimeError) as caught:
                main.anthropic_messages(main.build_request("d"), transport=transport)
            self.assertIn("--replay FILE", str(caught.exception))
        finally:
            if saved is not None:
                os.environ["ANTHROPIC_API_KEY"] = saved

    def test_cost_estimate_uses_the_price_table(self):
        usage = main.Usage(input_tokens=1_000_000, output_tokens=100_000, cache_read_input_tokens=500_000)
        self.assertAlmostEqual(main.estimate_cost("claude-opus-5-5", usage), 4.0 + 2.0 + 0.10)
        self.assertAlmostEqual(main.estimate_cost("claude-haiku-5-5", usage), 0.10 + 0.05 + 0.005)
        self.assertEqual(main.estimate_cost("unknown-model", usage), 0.0)


class GeneratorAndCliTests(unittest.TestCase):
    def run_cli(self, argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = main.main(argv)
        return code, out.getvalue(), err.getvalue()

    def test_generate_is_deterministic_and_puts_the_new_error_after_the_deploy(self):
        first, second = main.generate(500, seed=3), main.generate(500, seed=3)
        self.assertEqual(first, second)
        self.assertEqual(len(first), 500)
        self.assertNotEqual(first, main.generate(500, seed=4))
        marker = main.parse_timestamp(main.GENERATOR_MARKER)
        self.assertEqual(main.GENERATOR_MARKER, "2026-11-01T10:20:00Z")
        for raw in first:
            line = main.parse_line(raw)
            if "DATABASE_URL" in raw:
                self.assertGreaterEqual(line.ts, marker)
        self.assertTrue(any(raw.startswith("{") for raw in first))
        self.assertTrue(any("deploy: checkout" in raw for raw in first))

    def test_cli_mines_a_file_and_reports_the_new_error_first_with_a_large_token_ratio(self):
        with tempfile.TemporaryDirectory() as work:
            path = os.path.join(work, "app.log")
            with open(path, "w", encoding="utf-8") as handle:
                handle.write("\n".join(main.generate(4000, seed=7)) + "\n")
            code, out, _ = self.run_cli([path, "--marker", "2026-11-01T10:20:00Z", "--no-model", "--json"])
            self.assertEqual(code, 0)
            result = json.loads(out)
            top = result["analysis"]["ranked"][0]
            self.assertTrue(top["novel"])
            self.assertEqual(top["level"], "ERROR")
            self.assertIn("DATABASE_URL", top["template"])
            self.assertLessEqual(result["digest"]["tokens"], 2000)
            self.assertGreater(result["ratio"], 20)
            self.assertIsNone(result["explanation"])
            self.assertNotIn("eyJ", result["digest"]["text"])
            code, text, _ = self.run_cli([path, "--marker", "2026-11-01T10:20:00Z", "--no-model"])
            self.assertEqual(code, 0)
            self.assertIn("T01  score", text)
            self.assertIn("x smaller", text)

    def test_cli_replays_a_scripted_explanation_without_an_api_key(self):
        with tempfile.TemporaryDirectory() as work:
            path = os.path.join(work, "app.log")
            with open(path, "w", encoding="utf-8") as handle:
                handle.write("\n".join(main.generate(2000, seed=7)) + "\n")
            env_key = os.environ.pop("ANTHROPIC_API_KEY", None)
            try:
                code, out, _ = self.run_cli([path, "--marker", "2026-11-01T10:20:00Z", "--replay", FIXTURE, "--json"])
                self.assertEqual(code, 0)
                explanation = json.loads(out)["explanation"]
                self.assertEqual(explanation["stop"], "report")
                self.assertEqual(explanation["report"]["severity"], "sev2")
                self.assertEqual(explanation["unknown_refs"], [])
                self.assertGreater(explanation["cost_usd"], 0)
                code, _, err = self.run_cli([path, "--marker", "2026-11-01T10:20:00Z"])
                self.assertEqual(code, 1)
                self.assertIn("ANTHROPIC_API_KEY", err)
            finally:
                if env_key is not None:
                    os.environ["ANTHROPIC_API_KEY"] = env_key

    def test_cli_errors_are_usage_errors(self):
        code, _, err = self.run_cli(["/nonexistent/app.log", "--no-model"])
        self.assertEqual(code, 2)
        self.assertIn("nonexistent", err)
        with tempfile.NamedTemporaryFile("w", suffix=".log", delete=False) as handle:
            handle.write(at(0, "INFO hi") + "\n")
        try:
            code, _, err = self.run_cli([handle.name, "--marker", "yesterday", "--no-model"])
            self.assertEqual(code, 2)
            self.assertIn("cannot parse marker", err)
            code, _, err = self.run_cli([handle.name, "--budget", "5", "--no-model"])
            self.assertEqual(code, 1)
            self.assertIn("raise --budget", err)
        finally:
            os.unlink(handle.name)
        with self.assertRaises(ValueError):
            main.parse_marker("yesterday")
        self.assertEqual(main.parse_marker("1761993600"), 1761993600.0)
        self.assertEqual(main.parse_marker("250"), 250.0)


if __name__ == "__main__":
    unittest.main()
