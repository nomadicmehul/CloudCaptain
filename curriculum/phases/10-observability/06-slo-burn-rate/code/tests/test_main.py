import json
import math
import os
import re
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timezone
from io import StringIO

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import main  # noqa: E402

FIXTURES = os.path.join(os.path.dirname(__file__), "..", "fixtures")
SPEC = os.path.join(FIXTURES, "slo.yaml")
OPENSLO = os.path.join(FIXTURES, "openslo.yaml")
SCENARIOS = os.path.join(FIXTURES, "scenarios.yaml")
SLOTH_RULES = os.path.join(FIXTURES, "sloth-rules.yaml")


def read(path):
    with open(path, encoding="utf-8") as handle:
        return handle.read()


def checkout():
    return main.load_slos(read(SPEC))[0]


def scenario(name):
    return [s for s in main.load_scenarios(read(SCENARIOS)) if s.name == name][0]


def run_cli(argv):
    out, err = StringIO(), StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = main.main(argv)
    return code, out.getvalue(), err.getvalue()


def expand_series(text):
    values = []
    for part in text.split():
        match = re.fullmatch(r"([0-9.e+-]+?)([+-])([0-9.e+-]+)x(\d+)", part)
        if not match:
            values.append(float(part))
            continue
        start, sign, step, count = float(match.group(1)), match.group(2), float(match.group(3)), int(match.group(4))
        step = step if sign == "+" else -step
        for _ in range(count + 1):
            values.append(start)
            start += step
    return values


def counter(points):
    return main.Counter([float(t) for t, _ in points], [float(v) for _, v in points])


class YamlTests(unittest.TestCase):
    def test_reads_sloth_output_with_folded_lines_quotes_and_literal_blocks(self):
        groups = main.parse_yaml(read(SLOTH_RULES))[0]["groups"]
        self.assertEqual([g["name"] for g in groups], [
            "sloth-slo-sli-recordings-checkout-requests-availability",
            "sloth-slo-meta-recordings-checkout-requests-availability",
            "sloth-slo-alerts-checkout-requests-availability",
        ])
        first = groups[0]["rules"][0]
        self.assertEqual(first["labels"]["tier"], "1")
        self.assertTrue(first["expr"].endswith("[5m])))\n"))
        page = groups[2]["rules"][0]
        self.assertEqual(page["annotations"]["title"], "(page) {{$labels.sloth_service}} {{$labels.sloth_slo}} SLO error budget burn rate is too fast.")
        remaining = [r for r in groups[1]["rules"] if r["record"] == main.REMAINING_METRIC][0]
        self.assertEqual(remaining["expr"], '1 - slo:period_burn_rate:ratio{sloth_id="checkout-requests-availability", sloth_service="checkout", sloth_slo="requests-availability"}')

    def test_block_scalars_flow_collections_and_scalar_types(self):
        doc = main.parse_yaml(
            "keep: |\n  a\n  b\n\nstrip: |-\n  a\nkeep_all: |+\n  a\n\nfold: >\n  one\n  two\n\n  three\n"
            "flow: {a: 1, b: [x, 'y z', \"\\u00e9\"], c: {}}\nlist: []\n"
            "types: [1, -2, 0.5, 1e-3, true, null, ~, '010', \"it''s\", 'it''s']\n"
            "- not: a key\n".replace("- not: a key\n", "url: http://x:9090/a # comment\n")
        )[0]
        self.assertEqual(doc["keep"], "a\nb\n")
        self.assertEqual(doc["strip"], "a")
        self.assertEqual(doc["keep_all"], "a\n\n")
        self.assertEqual(doc["fold"], "one two\nthree\n")
        self.assertEqual(doc["flow"], {"a": 1, "b": ["x", "y z", "\u00e9"], "c": {}})
        self.assertEqual(doc["list"], [])
        self.assertEqual(doc["types"], [1, -2, 0.5, 0.001, True, None, None, "010", "it''s", "it's"])
        self.assertEqual(doc["url"], "http://x:9090/a")

    def test_sequences_at_the_parent_indentation_and_compact_mappings(self):
        doc = main.parse_yaml("groups:\n- name: a\n  rules:\n  - record: x\n    labels: {w: 5m}\n  - alert: y\n- name: b\n  rules: []\n")[0]
        self.assertEqual(doc, {"groups": [{"name": "a", "rules": [{"record": "x", "labels": {"w": "5m"}}, {"alert": "y"}]}, {"name": "b", "rules": []}]})

    def test_rejects_tabs_duplicate_keys_and_unterminated_quotes_with_a_line_number(self):
        for text, words in (("a:\n\tb: 1\n", "tabs"), ("a: 1\na: 2\n", "duplicate key 'a'"), ('a: "open\n', "unterminated"), ("a: &x 1\n", "anchors")):
            with self.assertRaises(main.YamlError) as caught:
                main.parse_yaml(text)
            self.assertIn(words, str(caught.exception))
            self.assertIn("YAML line", str(caught.exception))

    def test_dump_yaml_round_trips_what_it_writes(self):
        value = {"groups": [{"name": "g", "rules": [{"record": "slo:x", "expr": "a\n/\nb\n", "labels": {"tier": "1", "w": "5m", "empty": "", "bool": "true", "colon": "a: b"}}]}],
                 "nested": {"list": [[1, 2], {"k": None}], "empty": {}, "none": []}, "strip": "x\ny", "float": 0.0144, "quote": 'say "hi"'}
        text = main.dump_yaml(value)
        self.assertIn("expr: |\n", text)
        self.assertIn('tier: "1"', text)
        self.assertEqual(main.parse_yaml(text)[0], value)


class SpecTests(unittest.TestCase):
    def test_sloth_spec_loads_with_an_exact_budget_merged_labels_and_alerts(self):
        slo = checkout()
        self.assertEqual(slo.id, "checkout-requests-availability")
        self.assertEqual(slo.budget, 0.001)
        self.assertEqual(slo.target, 0.999)
        self.assertNotEqual(100 - 99.9, 0.1)
        self.assertEqual(slo.period, 30 * 86400)
        self.assertEqual(slo.labels, {"owner": "payments", "tier": "1"})
        self.assertEqual(slo.page.labels, {"team": "payments", "severity": "critical"})
        self.assertEqual(slo.ticket.labels["severity"], "warning")
        self.assertEqual(slo.page.annotations["runbook_url"], "https://runbooks.example.com/checkout/availability")
        self.assertEqual(main.load_slos(read(SPEC), "28d", {"env": "prod"})[0].labels["env"], "prod")

    def test_openslo_good_total_becomes_an_error_ratio_over_a_rolling_28_days(self):
        availability, latency = main.load_slos(read(OPENSLO))
        self.assertEqual(availability.period, 28 * 86400)
        self.assertEqual(availability.objective, 99.9)
        self.assertEqual(availability.error_ratio_query,
                         '1 - ((sum(rate(http_requests_total{job="checkout",code!~"5.."}[{{.window}}]))) / (sum(rate(http_requests_total{job="checkout"}[{{.window}}]))))')
        self.assertEqual(main.sli_expression(availability, 3600)[:40], '(1 - ((sum(rate(http_requests_total{job=')
        self.assertEqual(latency.calendar, "month")
        self.assertEqual(latency.period, 30 * 86400)
        self.assertTrue(latency.error_query.startswith("sum(rate(http_request_duration_seconds_count"))
        self.assertEqual(latency.total_query, 'sum(rate(http_request_duration_seconds_count{job="checkout"}[{{.window}}]))')
        self.assertEqual(latency.page.name, "CheckoutRequestsLatencyBudgetBurn")

    def test_validation_names_the_problem(self):
        good = read(SPEC)
        cases = (
            (good.replace("objective: 99.9", "objective: 100"), "no error budget"),
            (good.replace("[{{.window}}]))\n        total", "[5m]))\n        total"), "{{.window}}"),
            (good.replace('service: "checkout"', 'service: "check out"'), "service 'check out'"),
            (good.replace('total_query: sum(rate(http_requests_total{job="checkout"}[{{.window}}]))', 'total_query: sum(rate(http_requests_total{job="checkout",code=~"5.."}[{{.window}}]))'), "same query"),
            (good.replace("      name: CheckoutAvailabilityBudgetBurn\n", ""), "alerting.name is required"),
            ("kind: Deployment\n", "not an SLO spec"),
        )
        for text, words in cases:
            with self.assertRaises(main.InputError) as caught:
                main.load_slos(text)
            self.assertIn(words, str(caught.exception))

    def test_prometheus_duration_format(self):
        self.assertEqual([main.prom_duration(s) for s in (300, 5400, 86400, 30 * 86400, 28 * 86400, 7 * 86400, 90 * 86400, 365 * 86400, 0)],
                         ["5m", "1h30m", "1d", "30d", "4w", "1w", "90d", "1y", "0s"])
        self.assertEqual(main.parse_duration("1h30m"), 5400)
        with self.assertRaises(main.InputError):
            main.parse_duration("1.5h")


class TableAndRuleTests(unittest.TestCase):
    def test_burn_factors_and_thresholds_match_the_sre_workbook(self):
        slo = checkout()
        rows = main.alert_table(slo)
        self.assertEqual([(r.severity, r.factor, r.threshold_text) for r in rows],
                         [("page", 14.4, "0.0144"), ("page", 6.0, "0.006"), ("ticket", 3.0, "0.003"), ("ticket", 1.0, "0.001")])
        slo.period = 28 * 86400
        self.assertEqual([round(r.factor, 4) for r in main.alert_table(slo)], [13.44, 5.6, 2.8, 0.9333])

    def test_detection_time_and_the_budget_spent_when_an_alert_fires(self):
        slo = checkout()
        page = main.alert_table(slo)[0]
        self.assertAlmostEqual(page.detection(1.0), 51.84)
        self.assertAlmostEqual(page.detection(0.02), 2592.0)
        self.assertIsNone(page.detection(0.01))
        self.assertAlmostEqual(page.as_dict(slo.budget, slo.period)["budget_spent_when_it_fires"], 0.02)

    def test_generated_rules_follow_sloth_names_windows_and_labels(self):
        slo = checkout()
        groups = main.generate_groups(slo)
        self.assertEqual([g["name"] for g in groups], [main.SLI_GROUP + slo.id, main.META_GROUP + slo.id, main.ALERT_GROUP + slo.id])
        records = [(r["record"], r["labels"].get("sloth_window")) for r in groups[0]["rules"]]
        self.assertEqual(records, [(f"slo:sli_error:ratio_rate{w}", w) for w in ("5m", "30m", "1h", "2h", "6h", "1d", "3d", "30d")])
        self.assertEqual(groups[0]["rules"][2]["expr"], '(sum(rate(http_requests_total{job="checkout",code=~"5.."}[1h])))\n/\n(sum(rate(http_requests_total{job="checkout"}[1h])))\n')
        self.assertIn('sum_over_time(slo:sli_error:ratio_rate5m{sloth_id="checkout-requests-availability", sloth_service="checkout", sloth_slo="requests-availability"}[30d])', groups[0]["rules"][-1]["expr"])
        self.assertEqual(groups[0]["rules"][0]["labels"]["owner"], "payments")
        meta = {r["record"]: r["expr"] for r in groups[1]["rules"]}
        self.assertEqual(meta[main.BUDGET_METRIC], "vector(0.001)")
        self.assertEqual(meta[main.PERIOD_DAYS_METRIC], "vector(30)")
        self.assertIn("group_left", meta[main.CURRENT_BURN_METRIC])
        unoptimized = main.generate_groups(slo, optimize=False)[0]["rules"][-1]["expr"]
        self.assertIn("[30d]))", unoptimized)
        self.assertNotIn("sum_over_time", unoptimized)

    def test_alerts_are_multiwindow_and_multi_burn_rate(self):
        slo = checkout()
        page, ticket = main.generate_groups(slo)[2]["rules"]
        self.assertEqual(page["labels"], {"sloth_severity": "page", "team": "payments", "severity": "critical"})
        self.assertEqual(re.findall(r"ratio_rate(\w+)\{[^}]*\} > ([0-9.]+)", page["expr"]), [("5m", "0.0144"), ("1h", "0.0144"), ("30m", "0.006"), ("6h", "0.006")])
        self.assertEqual(re.findall(r"ratio_rate(\w+)\{[^}]*\} > ([0-9.]+)", ticket["expr"]), [("2h", "0.003"), ("1d", "0.003"), ("6h", "0.001"), ("3d", "0.001")])
        self.assertEqual(page["expr"].count("and"), 2)
        self.assertEqual(page["expr"].count("\nor\n"), 1)
        self.assertEqual(ticket["annotations"]["title"], "(ticket) {{$labels.sloth_service}} {{$labels.sloth_slo}} SLO error budget burn rate is too fast.")
        slo.ticket = None
        self.assertEqual(len(main.generate_groups(slo)[2]["rules"]), 1)

    def test_inspect_reads_sloth_and_minislo_rules_alike(self):
        sloth = main.inspect_rules(main.parse_yaml(read(SLOTH_RULES))[0])
        mine = main.inspect_rules(main.parse_yaml(main.rules_text([checkout()], "slo.yaml"))[0])
        self.assertEqual(sloth["groups"], mine["groups"])
        self.assertEqual({(r["record"], r["window"]) for r in sloth["records"]} - {(r["record"], r["window"]) for r in mine["records"]}, {("sloth_slo_info", "")})
        self.assertTrue(sloth["optimized"] and mine["optimized"])
        self.assertAlmostEqual(sloth["budget"], 0.001, places=15)
        for theirs, ours in zip(sloth["alerts"], mine["alerts"]):
            self.assertEqual((theirs["alert"], theirs["severity"], theirs["labels"], theirs["annotations"]), (ours["alert"], ours["severity"], ours["labels"], ours["annotations"]))
            for a, b in zip(theirs["conditions"], ours["conditions"]):
                self.assertEqual((a["short"], a["long"]), (b["short"], b["long"]))
                self.assertAlmostEqual(a["threshold"], b["threshold"], places=12)
                self.assertAlmostEqual(a["burn_factor"], b["burn_factor"], places=9)
        self.assertEqual(main.arithmetic("1-0.9990000000000001"), 1 - 0.9990000000000001)
        self.assertEqual(main.arithmetic("(14.4 * 0.0009999999999999432)"), 14.4 * 0.0009999999999999432)


class RateTests(unittest.TestCase):
    def test_extrapolated_rate_follows_prometheus(self):
        steady = counter([(t, t / 6) for t in range(0, 601, 60)])
        self.assertAlmostEqual(main.extrapolated_rate(steady, 600, 300), 10 / 60)
        young = counter([(480, 0), (540, 10), (600, 20)])
        self.assertAlmostEqual(main.extrapolated_rate(young, 600, 300), 20 / 300)
        restarted = counter([(480, 50), (540, 60), (600, 70)])
        self.assertAlmostEqual(main.extrapolated_rate(restarted, 600, 300), 20 * 1.25 / 300)
        reset = counter([(360, 60), (420, 70), (480, 5), (540, 15), (600, 25)])
        self.assertAlmostEqual(main.extrapolated_rate(reset, 600, 300), 35 * 1.25 / 300)
        self.assertIsNone(main.extrapolated_rate(counter([(360, 1), (600, 5)]), 600, 240))
        self.assertIsNone(main.extrapolated_rate(counter([(600, 5)]), 600, 300))

    def test_error_ratio_needs_samples_and_is_nan_without_traffic(self):
        flat = counter([(0, 0), (60, 0), (120, 0)])
        self.assertTrue(math.isnan(main.error_ratio(flat, flat, 120, 300)))
        self.assertIsNone(main.error_ratio(counter([(0, 0)]), counter([(0, 0)]), 0, 300))
        good, bad = counter([(0, 0), (60, 990), (120, 1980)]), counter([(0, 0), (60, 10), (120, 20)])
        self.assertAlmostEqual(main.error_ratio(good, bad, 120, 300), 0.01)

    def test_counters_carry_fractional_bad_events(self):
        sc = main.Scenario("carry", [main.Phase(600, error_ratio=0.0005)], events=1000)
        _, good, bad = main.build_counters(sc, 0.001)
        self.assertEqual(bad.values, [0, 0, 1, 1, 2, 2, 3, 3, 4, 4, 5])
        self.assertEqual(good.values[-1] + bad.values[-1], 10000)


class SimulationTests(unittest.TestCase):
    def test_a_fast_burn_pages_after_43_minutes_and_resolves_when_the_short_window_clears(self):
        sim = main.simulate(checkout(), scenario("fast-burn"))
        self.assertEqual([(main.prom_duration(c["t"]), c["severity"], c["state"], c["rows"]) for c in sim.transitions],
                         [("3d43m", "page", "firing", ["quick"]), ("3d1h2m", "page", "resolved", ["quick"])])
        summary = main.simulation_summary(sim)
        self.assertEqual(summary["alerts"]["ticket"]["times"], 0)
        self.assertAlmostEqual(summary["remaining_by_events"], 1 - (0.5 * 75 + 19.5 * 1) / 720, places=6)
        self.assertEqual(summary["action"], "ship")
        self.assertEqual(summary["action_by_rules"], "slow down")

    def test_a_slow_burn_opens_a_ticket_and_never_pages(self):
        sim = main.simulate(checkout(), scenario("slow-burn"))
        self.assertEqual([(main.prom_duration(c["t"]), c["severity"], c["state"], c["rows"]) for c in sim.transitions], [("3d8h", "ticket", "firing", ["slow"])])
        last = sim.evaluations[-1]
        self.assertEqual(last.firing, {"page": [], "ticket": ["quick", "slow"]})
        self.assertLess(last.remaining, 0)
        self.assertGreater(last.event_remaining, 0.8)

    def test_no_burn_fires_nothing(self):
        sim = main.simulate(checkout(), scenario("no-burn"))
        self.assertEqual(sim.transitions, [])
        self.assertAlmostEqual(sim.evaluations[-1].event_remaining, 1 - 0.5 / 30, places=6)
        self.assertAlmostEqual(sim.evaluations[-1].remaining, 0.5, places=6)

    def test_the_optimized_period_ratio_weights_quiet_hours_like_busy_ones(self):
        slo = checkout()
        sc = main.Scenario("day-night", [main.Phase(43200, burn_rate=0.5, events=10000), main.Phase(43200, burn_rate=4, events=1000)])
        averaged = main.simulate(slo, sc).evaluations[-1]
        exact = main.simulate(slo, sc, optimize=False).evaluations[-1]
        self.assertAlmostEqual(exact.period_ratio, (0.0005 * 10000 + 0.004 * 1000) / 11000, places=6)
        self.assertGreater(averaged.period_ratio, exact.period_ratio * 1.5)

    def test_a_calendar_budget_resets_on_the_first_of_the_month(self):
        latency = main.load_slos(read(OPENSLO))[1]
        sc = main.Scenario("month-end", [main.Phase(2 * 86400, burn_rate=2)], start=datetime(2026, 1, 31, tzinfo=timezone.utc))
        evaluations = main.simulate(latency, sc).evaluations
        before = [e for e in evaluations if e.t == 86400 - 60][0]
        boundary = [e for e in evaluations if e.t == 86400][0]
        after = [e for e in evaluations if e.t == 2 * 86400][0]
        self.assertEqual(before.length, 31 * 86400)
        self.assertAlmostEqual(before.event_remaining, 1 - 2 * (86340 / 86400) / 31, places=6)
        self.assertEqual((boundary.covered, boundary.event_remaining), (0, 1.0))
        self.assertEqual(after.length, 28 * 86400)
        self.assertAlmostEqual(after.event_remaining, 1 - 2 * 1 / 28, places=6)


class PolicyTests(unittest.TestCase):
    def test_budget_arithmetic_for_a_full_window_and_a_partial_one(self):
        slo = checkout()
        full = main.budget_report(slo, 1_000_000, 600)
        self.assertAlmostEqual(full["consumed"], 0.6)
        self.assertAlmostEqual(full["burn_rate"], 0.6)
        self.assertAlmostEqual(full["full_outage_allowed_seconds"], 2592)
        self.assertIsNotNone(full["exhausted_in_seconds"])
        partial = main.budget_report(slo, 1_000_000, 600, elapsed=10 * 86400)
        self.assertAlmostEqual(partial["consumed"], 0.2)
        self.assertAlmostEqual(partial["remaining"], 0.8)
        self.assertAlmostEqual(partial["allowed_bad_events_in_period"], 3000)
        hot = main.budget_report(slo, 1_000_000, 2000, elapsed=10 * 86400)
        self.assertAlmostEqual(hot["exhausted_in_seconds"], (1 - 2000 / 3000) * 30 * 86400 / 2)
        self.assertTrue(hot["within_period"])
        with self.assertRaises(main.InputError):
            main.budget_report(slo, 10, 20)

    def test_policy_levels_and_the_page_override(self):
        self.assertEqual(main.decide(-0.01, False)[0], "freeze")
        self.assertEqual(main.decide(0.0, False)[0], "freeze")
        self.assertEqual(main.decide(0.2, False)[0], "slow down")
        self.assertEqual(main.decide(0.9, False)[0], "ship")
        self.assertEqual(main.decide(0.9, True)[0], "incident")
        custom = main.load_policy("levels:\n  - {max_remaining: 0.5, action: review, rule: two approvers}\n  - {max_remaining: 0.1, action: freeze}\n")
        self.assertEqual([level.action for level in custom], ["freeze", "review"])
        self.assertEqual(main.decide(0.3, False, custom), ("review", "two approvers"))
        with self.assertRaises(main.InputError):
            main.load_policy("levels: []\n")


class PromtoolTestFileTests(unittest.TestCase):
    def build(self, rules_text=None):
        slo = checkout()
        rules_doc = main.parse_yaml(rules_text or read(SLOTH_RULES))[0]
        return main.promtool_test(slo, main.load_scenarios(read(SCENARIOS)), "sloth-rules.yaml", rules_doc)

    def test_series_values_encode_the_simulated_counters_exactly(self):
        sim = main.simulate(checkout(), scenario("fast-burn"))
        for series in (sim.good, sim.bad):
            text = main.series_values(series.values)
            self.assertEqual(expand_series(text), series.values)
        self.assertEqual(main.series_values(sim.bad.values), "0+5x4320 21800+200x59 33605+5x119")
        self.assertEqual(main.series_values([7.0]), "7")

    def test_the_test_file_orders_groups_and_expects_the_page_at_the_predicted_minute(self):
        doc, notes = self.build()
        self.assertEqual(doc["rule_files"], ["sloth-rules.yaml"])
        self.assertEqual(doc["group_eval_order"][0], "sloth-slo-sli-recordings-checkout-requests-availability")
        fast = doc["tests"][0]
        self.assertEqual(fast["name"], "fast-burn")
        by_time = {t["eval_time"]: t["exp_alerts"] for t in fast["alert_rule_test"]}
        self.assertEqual(by_time["3d42m"], [])
        self.assertEqual(by_time["3d43m"], [{
            "exp_labels": {"owner": "payments", "severity": "critical", "sloth_id": "checkout-requests-availability", "sloth_service": "checkout",
                           "sloth_severity": "page", "sloth_slo": "requests-availability", "team": "payments", "tier": "1"},
            "exp_annotations": {"runbook_url": "https://runbooks.example.com/checkout/availability", "summary": "Checkout is spending its availability error budget too fast",
                                "title": "(page) checkout requests-availability SLO error budget burn rate is too fast."},
        }])
        self.assertEqual(by_time["3d1h2m"], [])
        check = fast["promql_expr_test"][0]
        self.assertRegex(check["expr"], r'^count\(abs\(slo:sli_error:ratio_rate5m\{sloth_id="checkout-requests-availability", sloth_window="5m"\} - [0-9.e-]+\) <= [0-9.e-]+\)$')
        self.assertEqual(check["exp_samples"], [{"labels": "{}", "value": 1}])
        negative = [c["expr"] for c in doc["tests"][1]["promql_expr_test"] if main.REMAINING_METRIC in c["expr"]][-1]
        self.assertRegex(negative, r"\} \+ [0-9.e-]+\) <= ")
        self.assertEqual(notes, [])
        self.assertEqual(main.parse_yaml(main.dump_yaml(doc))[0], doc)

    def test_checkpoints_on_an_exact_threshold_tie_are_skipped(self):
        tie = main.Scenario("tie", [main.Phase(3 * 86400, burn_rate=0.5), main.Phase(11 * 3600, burn_rate=4)])
        sim = main.simulate(checkout(), tie)
        self.assertEqual(main.prom_duration(sim.transitions[0]["t"]), "3d10h17m")
        self.assertLess(sim.evaluations[int(sim.transitions[0]["t"] // 60)].margin, main.AMBIGUOUS)
        clear, skipped = main.checkpoints(sim, 1800)
        self.assertEqual(skipped, 1)
        self.assertNotIn(sim.transitions[0]["t"], [e.t for e in clear])
        self.assertIn(sim.transitions[0]["t"] - 60, [e.t for e in clear])

    def test_annotations_with_value_templates_cannot_be_predicted(self):
        slo = checkout()
        slo.page.annotations["description"] = "burning at {{ $value }}"
        with self.assertRaises(main.InputError) as caught:
            main.expected_alert(slo, "page")
        self.assertIn("$value", str(caught.exception))
        slo.page.annotations["description"] = "{{ $labels.owner }} owns {{$labels.missing}}"
        self.assertEqual(main.expected_alert(slo, "page")["exp_annotations"]["description"], "payments owns <no value>")

    def test_rules_without_the_spec_alert_are_rejected(self):
        with self.assertRaises(main.InputError) as caught:
            self.build(read(SLOTH_RULES).replace("CheckoutAvailabilityBudgetBurn", "SomethingElse"))
        self.assertIn("no alert named CheckoutAvailabilityBudgetBurn", str(caught.exception))


class CliTests(unittest.TestCase):
    def test_commands_exit_codes_and_outputs(self):
        code, out, _ = run_cli(["validate", SPEC])
        self.assertEqual((code, out.split()[0]), (0, "ok"))
        code, _, err = run_cli(["validate", os.path.join(FIXTURES, "missing.yaml")])
        self.assertEqual(code, 2)
        self.assertIn("cannot read", err)
        code, out, _ = run_cli(["table", SPEC, "--json"])
        self.assertEqual([row["burn_factor"] for row in json.loads(out)["checkout-requests-availability"]], [14.4, 6, 3, 1])
        self.assertEqual(run_cli(["budget", SPEC, "--total", "1000000", "--bad", "400"])[0], 0)
        code, out, _ = run_cli(["budget", SPEC, "--total", "1000000", "--bad", "1200"])
        self.assertEqual(code, 1)
        self.assertIn("freeze", out)
        code, out, _ = run_cli(["simulate", SPEC, "--scenarios", SCENARIOS, "--scenario", "fast-burn", "--json"])
        self.assertEqual(json.loads(out)[0]["transitions"][0]["time"], "3d43m")
        self.assertEqual(run_cli(["simulate", SPEC, "--scenarios", SCENARIOS, "--scenario", "nope"])[0], 2)
        with tempfile.TemporaryDirectory() as tmp:
            rules = os.path.join(tmp, "rules.yaml")
            self.assertEqual(run_cli(["generate", SPEC, "-o", rules])[0], 0)
            code, out, _ = run_cli(["inspect", rules])
            self.assertIn("1h+5m > 0.0144 (14.4x)", out)
            test = os.path.join(tmp, "test.yaml")
            self.assertEqual(run_cli(["test-file", SPEC, "--scenarios", SCENARIOS, "--rules", rules, "-o", test, "--scenario", "no-burn"])[0], 0)
            self.assertEqual(main.parse_yaml(read(test))[0]["rule_files"], ["rules.yaml"])


if __name__ == "__main__":
    unittest.main()
