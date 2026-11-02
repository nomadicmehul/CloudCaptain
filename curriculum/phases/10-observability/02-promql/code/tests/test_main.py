import copy
import json
import math
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import main  # noqa: E402

FIXTURES = os.path.join(os.path.dirname(__file__), "..", "fixtures")
QUERIES = os.path.join(FIXTURES, "queries.json")
RULES_TEST = os.path.join(FIXTURES, "rules-test.json")
MINUTE = 60_000


def load(spec, interval="1m"):
    store = main.Store()
    step = main.parse_duration(interval)
    for series, values in spec.items():
        labels, _ = main.parse_metric(series)
        for i, value in enumerate(main.expand_series_values(values)):
            if value is None:
                continue
            if value == main.STALE:
                store.add(main.as_labels(labels), i * step, math.nan, stale=True)
            else:
                store.add(main.as_labels(labels), i * step, value)
    return store


def at(text):
    return main.parse_duration(text)


def query(store, expr, when, **options):
    value, _ = main.instant_query(store, expr, at(when) if isinstance(when, str) else when, **options)
    return value


def values(vector):
    return {main.format_labels(s.labels): s.value for s in vector}


def run(argv):
    out, err = StringIO(), StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = main.main(argv)
    return code, out.getvalue(), err.getvalue()


GENERATED = main.load_exposition(main.generate_openmetrics(), "generated")
BASE = main.DEFAULT_START * 1000


class ParserTests(unittest.TestCase):
    def test_operator_precedence_and_associativity_follow_promql(self):
        cases = {
            "2 ^ 3 ^ 2": 512,
            "1 + 2 * 3": 7,
            "(1 + 2) * 3": 9,
            "10 - 2 - 3": 5,
            "-2 ^ 2": -4,
            "2 * 3 % 4": 2,
            "1 + 1 > bool 1": 1,
            "2 ^ 3 ^ 2 - -2 ^ 2 * 3 % 5": 514,
        }
        for expr, expected in cases.items():
            self.assertEqual(query(main.Store(), expr, 0).value, expected, expr)

    def test_parse_errors_name_the_rule_prometheus_enforces(self):
        cases = {
            "rate(sum(x)[5m])": "ranges only allowed for vector selectors",
            '{job=~".*"}': "at least one non-empty matcher",
            "1 > 2": "comparisons between scalars must use BOOL modifier",
            "rate(x)": 'expected type range vector in call to function "rate", got instant vector',
            "sum(x[5m])": "expected type instant vector in aggregation expression, got range vector",
            "x[5m:1m]": "subqueries",
            "nosuch(x)": 'unknown function with name "nosuch"',
            "deriv(x[5m])": "not part of the miniql subset",
            "x and 1": 'set operator "and" not allowed in binary scalar expression',
            "x + on (a) 1": "vector matching only allowed between instant vectors",
            "x + bool y": "bool modifier can only be used on comparison operators",
            "x * group_left y": "needs on (...) or ignoring (...)",
            "x * on (a) group_left (a) y": "must not occur in ON and GROUP clause",
            "sum(x) offset 5m": "offset modifier must be preceded by",
            "x offset 1m [5m]": "no offset or @ modifiers allowed before range",
            'x{a="\\d"}': "unknown escape sequence",
            "x{a=b}": "expected string",
        }
        for expr, fragment in cases.items():
            with self.assertRaises(main.ParseError, msg=expr) as caught:
                main.parse(expr)
            self.assertIn(fragment, str(caught.exception), expr)

    def test_durations_strings_and_rendering(self):
        self.assertEqual(main.parse_duration("1h30m"), 5_400_000)
        self.assertEqual(main.parse_duration("500ms"), 500)
        self.assertEqual(main.parse_duration("2d"), 172_800_000)
        self.assertEqual(main.format_duration(5_400_000), "1h30m")
        with self.assertRaises(main.InputError):
            main.parse_duration("5")
        node = main.parse('x{a="q\\"u", b=~`\\d+`, "c"="v"}')
        self.assertEqual([(m.name, m.op, m.value) for m in node.matchers[1:]], [("a", "=", 'q"u'), ("b", "=~", "\\d+"), ("c", "=", "v")])
        node = main.parse('{"cc_up", job="api"}')
        self.assertEqual(node.name, "cc_up")
        rendered = main.render(main.parse('sum by (code) (rate(x{a="1"}[5m] offset 1m)) / on (code) group_left (zone) y'))
        self.assertEqual(rendered, 'sum by (code) (rate(x{a="1"}[5m] offset 1m)) / on (code) group_left (zone) y')


class LoaderTests(unittest.TestCase):
    def test_openmetrics_uses_seconds_ignores_exemplars_and_normalizes_le(self):
        text = "\n".join([
            "# HELP lat Latency.",
            "# TYPE lat histogram",
            'lat_bucket{le="1"} 3 1767225600 # {trace_id="abc"} 0.5 1767225600',
            'lat_bucket{le="+Inf"} 4 1767225600',
            "lat_count 4 1767225600.5",
            '# TYPE path_hits counter',
            'path_hits_total{path="a\\"b\\\\c",empty=""} 7 1767225600',
            "# EOF",
            "ignored 1 2",
        ])
        store = main.load_exposition(text)
        labels = {main.format_labels(k): s.times for k, s in store.series.items()}
        self.assertEqual(labels['lat_bucket{le="1.0"}'], [1767225600000])
        self.assertEqual(labels["lat_count"], [1767225600500])
        self.assertIn('path_hits_total{path="a\\"b\\\\c"}', labels)
        self.assertNotIn("ignored", labels)

    def test_text_format_uses_milliseconds_and_rejects_bad_history(self):
        store = main.load_exposition('up{job="a"} 1 1000\nup{job="a"} 0 2000\n')
        self.assertEqual(store.series[(("__name__", "up"), ("job", "a"))].times, [1000, 2000])
        for text, fragment in (("up 1 2000\nup 0 1000\n", "out of order"), ("up 1 1000\nup 2 1000\n", "duplicate sample"), ("up 1\n", "without a timestamp"), ("up one 1000\n", "not a sample value")):
            with self.assertRaises(main.InputError) as caught:
                main.load_exposition(text, "f.prom")
            self.assertIn(fragment, str(caught.exception))
            self.assertIn("f.prom:", str(caught.exception))
        self.assertEqual(main.load_exposition("up 1 1000\nup 1 1000\n").stats()["samples"], 1)


class SelectorTests(unittest.TestCase):
    def test_instant_selector_takes_the_newest_sample_inside_a_left_open_lookback(self):
        store = load({"x": "7"})
        self.assertEqual(values(query(store, "x", 0)), {"x": 7})
        self.assertEqual(values(query(store, "x", "4m59s")), {"x": 7})
        self.assertEqual(values(query(store, "x", "5m")), {})
        self.assertEqual(values(query(store, "x", "10m", lookback_ms=at("15m"))), {"x": 7})
        self.assertEqual(values(query(store, "x", -1)), {})

    def test_matchers_are_anchored_and_a_missing_label_equals_empty(self):
        store = load({'x{job="api",zone="a"}': "1", 'x{job="apiserver"}': "2", 'x{job="db"}': "3"})
        self.assertEqual(values(query(store, 'x{job=~"api"}', 0)), {'x{job="api",zone="a"}': 1})
        self.assertEqual(set(values(query(store, 'x{job=~"api.*"}', 0))), {'x{job="api",zone="a"}', 'x{job="apiserver"}'})
        self.assertEqual(set(values(query(store, 'x{zone=""}', 0))), {'x{job="apiserver"}', 'x{job="db"}'})
        self.assertEqual(set(values(query(store, 'x{job!~"api.*"}', 0))), {'x{job="db"}'})
        self.assertEqual(set(values(query(store, '{__name__=~"x|y",job!="db"}', 0))), {'x{job="api",zone="a"}', 'x{job="apiserver"}'})

    def test_offset_and_at_move_the_evaluation_time(self):
        store = load({"x": "0+10x10"})
        self.assertEqual(values(query(store, "x offset 2m", "5m")), {"x": 30})
        self.assertEqual(values(query(store, "x @ 120", "9m")), {"x": 20})
        self.assertEqual(values(query(store, "x @ 180 offset 1m", "9m")), {"x": 20})
        self.assertEqual(values(query(store, "x offset -2m", "5m")), {"x": 70})
        self.assertAlmostEqual(values(query(store, "rate(x[2m] @ 300)", "9m"))["{}"], 10 / 60)

    def test_a_stale_marker_hides_the_series_until_the_next_sample(self):
        store = load({"x": "1 2 stale 4"})
        self.assertEqual(values(query(store, "x", "1m30s")), {"x": 2})
        self.assertEqual(values(query(store, "x", "2m30s")), {})
        self.assertEqual(values(query(store, "x", "3m")), {"x": 4})
        self.assertEqual(values(query(store, "count_over_time(x[5m])", "3m")), {"{}": 3})


class RateTests(unittest.TestCase):
    def test_rate_extrapolates_to_the_window_edges_when_samples_reach_them(self):
        store = load({"x_total": "0+60x10"})
        self.assertEqual(values(query(store, "rate(x_total[5m])", "10m")), {"{}": 1.0})
        self.assertAlmostEqual(values(query(store, "increase(x_total[5m])", "10m"))["{}"], 300)
        self.assertAlmostEqual(values(query(store, "increase(x_total[5m])", "9m30s"))["{}"], 240 * 300 / 240)

    def test_a_series_that_starts_or_ends_inside_the_window_is_extrapolated_half_a_spacing(self):
        late = load({"x_total": "_x7 100+60x3"})
        self.assertAlmostEqual(values(query(late, "increase(x_total[5m])", "10m"))["{}"], 210)
        near_zero = load({"x_total": "_x7 10+60x3"})
        self.assertAlmostEqual(values(query(near_zero, "increase(x_total[5m])", "10m"))["{}"], 190)
        ended = load({"x_total": "0+60x6"})
        self.assertAlmostEqual(values(query(ended, "rate(x_total[8m])", "10m"))["{}"], 270 / 480)

    def test_increase_adds_back_the_value_before_a_counter_reset(self):
        store = load({"x_total": "0+60x7 0+60x2"})
        self.assertAlmostEqual(values(query(store, "increase(x_total[5m])", "10m"))["{}"], 225)
        self.assertAlmostEqual(values(query(store, "delta(x_total[5m])", "10m"))["{}"], -300)
        self.assertEqual(values(query(store, "irate(x_total[5m])", "8m")), {"{}": 0.0})
        self.assertEqual(values(query(store, "irate(x_total[5m])", "10m")), {"{}": 1.0})

    def test_a_range_shorter_than_two_scrapes_returns_nothing(self):
        store = load({"x_total": "0+60x10"})
        self.assertEqual(values(query(store, "rate(x_total[1m])", "10m")), {})
        self.assertEqual(values(query(store, "rate(x_total[2m])", "10m")), {"{}": 1.0})

    def test_rate_of_a_gauge_reads_every_drop_as_a_reset_and_says_so(self):
        store = load({"queue": "10 5 10"})
        value, evaluator = main.instant_query(store, "rate(queue[5m])", at("2m"))
        self.assertAlmostEqual(values(value)["{}"], 12.5 / 300)
        self.assertIn("metric might not be a counter", evaluator.infos[0])
        self.assertEqual(values(query(store, "delta(queue[5m])", "2m")), {"{}": 0.0})


class AggregationTests(unittest.TestCase):
    def setUp(self):
        self.store = load({
            'x{job="api",instance="a",code="200"}': "10",
            'x{job="api",instance="b",code="200"}': "30",
            'x{job="api",instance="a",code="500"}': "2",
            'x{job="db",instance="c",code="200"}': "5",
        })

    def test_over_time_functions(self):
        store = load({'g{w="1"}': "1 5 3 9 7"})
        expected = {"avg_over_time": 5, "max_over_time": 9, "min_over_time": 1, "sum_over_time": 25, "count_over_time": 5}
        for name, value in expected.items():
            self.assertEqual(values(query(store, f"{name}(g[5m])", "4m")), {'{w="1"}': value}, name)
        self.assertEqual(values(query(store, "last_over_time(g[5m])", "4m")), {'g{w="1"}': 7})
        self.assertEqual(values(query(store, "quantile_over_time(0.5, g[5m])", "4m")), {'{w="1"}': 5})
        self.assertAlmostEqual(values(query(store, "quantile_over_time(0.9, g[5m])", "4m"))['{w="1"}'], 8.2)

    def test_by_keeps_only_listed_labels_and_without_also_drops_the_name(self):
        self.assertEqual(values(query(self.store, "sum by (code) (x)", 0)), {'{code="200"}': 45, '{code="500"}': 2})
        self.assertEqual(values(query(self.store, "sum without (instance) (x)", 0)), {'{code="200",job="api"}': 40, '{code="500",job="api"}': 2, '{code="200",job="db"}': 5})
        self.assertEqual(values(query(self.store, "sum(x)", 0)), {"{}": 47})
        self.assertEqual(values(query(self.store, "sum by (__name__, job) (x)", 0)), {'x{job="api"}': 42, 'x{job="db"}': 5})

    def test_avg_count_min_max(self):
        self.assertEqual(values(query(self.store, "avg by (job) (x)", 0)), {'{job="api"}': 14, '{job="db"}': 5})
        self.assertEqual(values(query(self.store, "count by (job) (x)", 0)), {'{job="api"}': 3, '{job="db"}': 1})
        self.assertEqual(values(query(self.store, "max(x)", 0)), {"{}": 30})
        self.assertEqual(values(query(self.store, "min by (code) (x)", 0)), {'{code="200"}': 5, '{code="500"}': 2})

    def test_topk_and_bottomk_keep_the_original_labels_per_group(self):
        top = values(query(self.store, "topk(1, x) by (job)", 0))
        self.assertEqual(top, {'x{code="200",instance="b",job="api"}': 30, 'x{code="200",instance="c",job="db"}': 5})
        self.assertEqual(values(query(self.store, "bottomk(2, x)", 0)), {'x{code="500",instance="a",job="api"}': 2, 'x{code="200",instance="c",job="db"}': 5})
        self.assertEqual(values(query(self.store, "topk(0, x)", 0)), {})
        self.assertEqual(len(query(self.store, "topk(10, x)", 0)), 4)


class MatchingTests(unittest.TestCase):
    def setUp(self):
        self.store = load({
            'errors{instance="a",code="500"}': "3",
            'errors{instance="b",code="500"}': "1",
            'requests{instance="a",code="200"}': "30",
            'requests{instance="b",code="200"}': "10",
            'info{instance="a",version="1.4"}': "1",
            'info{instance="b",version="1.5"}': "1",
            'lim{instance="a"}': "20",
            'lim{instance="b"}': "20",
        })

    def test_one_to_one_needs_identical_label_sets_and_drops_the_name(self):
        self.assertEqual(values(query(self.store, "errors / requests", 0)), {})
        self.assertEqual(values(query(self.store, "errors / ignoring (code) requests", 0)), {'{instance="a"}': 0.1, '{instance="b"}': 0.1})
        self.assertEqual(values(query(self.store, "errors / on (instance) requests", 0)), {'{instance="a"}': 0.1, '{instance="b"}': 0.1})
        self.assertEqual(values(query(self.store, "requests > on (instance) lim", 0)), {'{instance="a"}': 30})

    def test_group_left_copies_labels_from_the_one_side(self):
        joined = values(query(self.store, "requests * on (instance) group_left (version) info", 0))
        self.assertEqual(joined, {'{code="200",instance="a",version="1.4"}': 30, '{code="200",instance="b",version="1.5"}': 10})
        right = values(query(self.store, "info * on (instance) group_right (version) errors", 0))
        self.assertEqual(right, {'{code="500",instance="a",version="1.4"}': 3, '{code="500",instance="b",version="1.5"}': 1})

    def test_ambiguous_matches_are_execution_errors_with_prometheus_wording(self):
        store = load({'r{instance="a",code="200"}': "1", 'r{instance="a",code="500"}': "2", 'u{instance="a"}': "1", 'v{instance="a",zone="x"}': "1", 'v{instance="a",zone="y"}': "1"})
        cases = {
            "u * on (instance) r": 'found duplicate series for the match group {instance="a"} on the right hand-side',
            "r * on (instance) u": "many-to-one matching must be explicit (group_left/group_right)",
            "r * on (instance) group_left v": "found duplicate series",
            "sum by (instance) (r) * on (instance) group_left (zone) v": "found duplicate series",
        }
        for expr, fragment in cases.items():
            with self.assertRaises(main.EvalError, msg=expr) as caught:
                query(store, expr, 0)
            self.assertIn(fragment, str(caught.exception), expr)
        store = load({'r{instance="a"}': "1", 's{instance="a"}': "2", 'u{instance="a"}': "1", 'x_total{instance="a"}': "0 1", 'y_total{instance="a"}': "0 1"})
        with self.assertRaises(main.EvalError) as caught:
            query(store, '{__name__=~"r|s"} * on (instance) group_left u', 0)
        self.assertIn("multiple matches for labels: grouping labels must ensure unique matches", str(caught.exception))
        with self.assertRaises(main.EvalError) as caught:
            query(store, 'rate({__name__=~"x_total|y_total"}[5m])', "1m")
        self.assertIn("vector cannot contain metrics with the same labelset", str(caught.exception))

    def test_comparisons_filter_unless_bool_and_keep_the_vector_value(self):
        self.assertEqual(values(query(self.store, "requests > 15", 0)), {'requests{code="200",instance="a"}': 30})
        self.assertEqual(values(query(self.store, "requests > bool 15", 0)), {'{code="200",instance="a"}': 1, '{code="200",instance="b"}': 0})
        self.assertEqual(values(query(self.store, "15 < requests", 0)), {'requests{code="200",instance="a"}': 30})
        self.assertEqual(values(query(self.store, "requests + 1", 0)), {'{code="200",instance="a"}': 31, '{code="200",instance="b"}': 11})

    def test_set_operators_match_on_labels_without_the_name(self):
        self.assertEqual(set(values(query(self.store, "requests and on (instance) errors", 0))), {'requests{code="200",instance="a"}', 'requests{code="200",instance="b"}'})
        self.assertEqual(set(values(query(self.store, 'requests unless on (instance) info{version="1.4"}', 0))), {'requests{code="200",instance="b"}'})
        self.assertEqual(len(query(self.store, "errors or requests", 0)), 4)
        self.assertEqual(values(query(self.store, "sum(nothing) or vector(0)", 0)), {"{}": 0})

    def test_arithmetic_follows_go_float_rules(self):
        cases = {"1 / 0": math.inf, "-1 / 0": -math.inf, "-7 % 3": -1.0, "2 ^ 0.5": math.sqrt(2), "0 ^ -1": math.inf, "10 % 2.5": 0.0}
        for expr, expected in cases.items():
            self.assertEqual(query(main.Store(), expr, 0).value, expected, expr)
        for expr in ("0 / 0", "5 % 0", "(-8) ^ (1 / 3)", "Inf % 2"):
            self.assertTrue(math.isnan(query(main.Store(), expr, 0).value), expr)
        self.assertEqual([main.fmt_value(v) for v in (math.inf, -math.inf, math.nan, 2.0, 0.1, 1e-7, 1e21, -0.0)], ["+Inf", "-Inf", "NaN", "2", "0.1", "0.0000001", "1000000000000000000000", "-0"])
        self.assertEqual([main.openmetrics_float(v) for v in (1, 0, 0.25, 1234567, 0.00001, 100000)], ["1.0", "0.0", "0.25", "1.234567e+06", "1e-05", "100000.0"])


class HistogramTests(unittest.TestCase):
    def store(self, counts, bounds=("0.1", "0.5", "1", "+Inf")):
        return load({f'lat_bucket{{job="api",le="{b}"}}': str(c) for b, c in zip(bounds, counts)})

    def test_histogram_quantile_interpolates_inside_the_bucket(self):
        store = self.store([10, 30, 40, 40])
        cases = {0.5: 0.3, 0.25: 0.1, 0.99: 0.98, 1: 1.0}
        for q, expected in cases.items():
            self.assertAlmostEqual(values(query(store, f"histogram_quantile({q}, lat_bucket)", 0))['{job="api"}'], expected, msg=q)
        self.assertEqual(values(query(self.store([10, 30, 40, 50]), "histogram_quantile(0.9, lat_bucket)", 0)), {'{job="api"}': 1.0})

    def test_histogram_quantile_edge_cases(self):
        self.assertTrue(math.isnan(values(query(self.store([10, 30, 40], ("0.1", "0.5", "1")), "histogram_quantile(0.5, lat_bucket)", 0))['{job="api"}']))
        self.assertTrue(math.isnan(values(query(self.store([0, 0, 0, 0]), "histogram_quantile(0.5, lat_bucket)", 0))['{job="api"}']))
        self.assertEqual(values(query(self.store([10, 30, 40, 40]), "histogram_quantile(-1, lat_bucket)", 0))['{job="api"}'], -math.inf)
        self.assertEqual(values(query(self.store([10, 30, 40, 40]), "histogram_quantile(2, lat_bucket)", 0))['{job="api"}'], math.inf)
        self.assertAlmostEqual(values(query(self.store([10, 8, 20, 20]), "histogram_quantile(0.5, lat_bucket)", 0))['{job="api"}'], 0.1)
        self.assertAlmostEqual(main.bucket_quantile(0.5, [(1.0, 10), (1.0, 10), (math.inf, 40)]), 1.0)
        self.assertEqual(main.bucket_quantile(0.5, [(-1.0, 30), (math.inf, 40)]), -1.0)
        value, evaluator = main.instant_query(load({'lat_bucket{le="x"}': "1"}), "histogram_quantile(0.5, lat_bucket)", 0)
        self.assertEqual(len(value), 0)
        self.assertIn('bucket label "le" is missing or has a malformed value', evaluator.infos[0])


class RangeAndExplainTests(unittest.TestCase):
    def test_a_range_query_evaluates_every_step_and_at_start_pins_one_time(self):
        store = load({"x_total": "0+60x10"})
        series, _ = main.range_query(store, "rate(x_total[5m])", at("5m"), at("10m"), at("1m"))
        self.assertEqual(series, {(): [(at(f"{m}m"), 1.0) for m in range(5, 11)]})
        pinned, _ = main.range_query(store, "x_total @ start()", at("2m"), at("4m"), at("1m"))
        self.assertEqual([v for _, v in pinned[(("__name__", "x_total"),)]], [120, 120, 120])
        for args, fragment in ((("x_total", 10, 0, 1), "must not be before"), (("x_total", 0, 10, 0), "zero or negative"), (("x_total[5m]", 0, 10, 1), "invalid expression type")):
            with self.assertRaises(ValueError) as caught:
                main.range_query(store, *args)
            self.assertIn(fragment, str(caught.exception))

    def test_explain_shows_the_samples_the_reset_and_the_extrapolation(self):
        store = load({'x_total{i="a"}': "0+60x7 0+60x2"})
        value, _ = main.instant_query(store, "sum(increase(x_total[5m]))", at("10m"), explain=True)
        why = "\n".join(value[0].why)
        self.assertIn("sum over 1 series = 225", why)
        self.assertIn('increase(x_total{i="a"}[5m]) = 225', why)
        self.assertIn("5 sample(s) in (-300s, 0s]: -240s 360, -180s 420, -120s 0, -60s 60, 0s 120", why)
        self.assertIn("plus 420 added back for 1 counter reset(s) (at -120s 420 -> 0) = 180", why)
        self.assertIn("180 x 300s / 240s sampled = 225", why)

    def test_explain_lists_what_was_dropped_and_why(self):
        store = load({'x_total{i="a"}': "0+60x10", 'y{i="b"}': "1"})
        value, evaluator = main.instant_query(store, 'rate(x_total[1m]) or y > 5', at("10m"), explain=True)
        self.assertEqual(len(value), 0)
        dropped = "\n".join(evaluator.dropped)
        self.assertIn('rate(x_total{i="a"}[1m]): 1 sample in the window: two are needed', dropped)
        self.assertIn('y{i="b"}: no sample in the 5m lookback', dropped)
        code, out, _ = run(["query", "--series", self.write(main.generate_openmetrics()), "--at", "+22m", "--explain", 'histogram_quantile(0.5, rate(cc_http_request_duration_seconds_bucket{instance="b"}[5m]))'])
        self.assertEqual(code, 0)
        self.assertIn("rank = 0.5 x ", out)
        self.assertIn("linear inside it", out)

    def write(self, text):
        handle = tempfile.NamedTemporaryFile("w", suffix=".om", delete=False)
        handle.write(text)
        handle.close()
        self.addCleanup(os.unlink, handle.name)
        return handle.name


class ApiTests(unittest.TestCase):
    def test_the_query_api_speaks_prometheus_json(self):
        status, body = main.api_query(GENERATED, "/api/v1/query", {"query": 'rate(cc_http_requests_total{instance="a",code="200"}[5m])', "time": str(main.DEFAULT_START + 900)})
        self.assertEqual(status, 200)
        self.assertEqual(body["data"], {"resultType": "vector", "result": [{"metric": {"code": "200", "instance": "a", "job": "api"}, "value": [main.DEFAULT_START + 900, "1"]}]})
        status, body = main.api_query(GENERATED, "/api/v1/query", {"query": "1 + 1", "time": "2026-01-01T00:10:00Z"})
        self.assertEqual(body["data"], {"resultType": "scalar", "result": [main.DEFAULT_START + 600, "2"]})
        status, body = main.api_query(GENERATED, "/api/v1/query", {"query": "rate(x)"})
        self.assertEqual((status, body["errorType"]), (400, "bad_data"))
        status, body = main.api_query(GENERATED, "/api/v1/query", {"query": "cc_build_info * on (instance) cc_queue_depth", "time": str(main.DEFAULT_START)})
        self.assertEqual((status, body["data"]["result"]), (200, []))
        status, body = main.api_query(GENERATED, "/api/v1/query", {"query": "cc_http_requests_total * on (job) cc_build_info", "time": str(main.DEFAULT_START)})
        self.assertEqual((status, body["errorType"]), (422, "execution"))
        status, body = main.api_query(GENERATED, "/api/v1/query_range", {"query": "sum(cc_queue_depth)", "start": str(main.DEFAULT_START), "end": str(main.DEFAULT_START + 60), "step": "30s"})
        self.assertEqual(body["data"]["result"], [{"metric": {}, "values": [[main.DEFAULT_START, "3"], [main.DEFAULT_START + 30, "12"], [main.DEFAULT_START + 60, "22"]]}])
        status, body = main.api_query(GENERATED, "/api/v1/query", {"query": "rate(cc_queue_depth[5m])", "time": str(main.DEFAULT_START + 600)})
        self.assertIn("metric might not be a counter", body["infos"][0])

    def test_serve_and_compare_agree_on_every_fixture_query(self):
        server = main.start_server(GENERATED, "127.0.0.1", 0)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        url = f"http://127.0.0.1:{server.server_address[1]}"
        spec = json.loads(Path(QUERIES).read_text())
        results = main.compare_queries(GENERATED, spec, main.http_fetcher(url))
        self.assertGreaterEqual(len(results), 25)
        self.assertEqual([r["name"] for r in results if not r["agree"]], [])
        by_name = {r["name"]: r for r in results}
        self.assertIn("both rejected it", by_name["sum before rate is a parse error"]["detail"])
        self.assertIn("both rejected it", by_name["many-to-many is an error"]["detail"])
        code, out, _ = run(["compare", "--series", self.write_generated(), "--queries", QUERIES, "--url", url])
        self.assertEqual(code, 0, out)
        self.assertIn(f"{len(results)} of {len(results)} queries agree", out)

    def test_compare_reports_values_series_and_errors_that_differ(self):
        spec = {"base": main.DEFAULT_START, "queries": [
            {"name": "value", "query": "sum(cc_queue_depth)", "at": 600},
            {"name": "series", "query": "cc_queue_depth", "at": 600},
            {"name": "error", "query": "rate(x)", "at": 600},
            {"name": "range", "query": "sum(cc_queue_depth)", "start": 0, "end": 60, "step": 30},
        ]}

        def tampered(path, params):
            _, body = main.api_query(GENERATED, path, params)
            body = copy.deepcopy(body)
            if params["query"] == "sum(cc_queue_depth)" and path.endswith("/query"):
                body["data"]["result"][0]["value"][1] = "7.000001"
            elif params["query"] == "cc_queue_depth":
                body["data"]["result"].pop()
            elif params["query"] == "rate(x)":
                body = {"status": "success", "data": {"resultType": "vector", "result": []}}
            else:
                body["data"]["result"][0]["values"][1][1] = "13"
            return body

        results = {r["name"]: r for r in main.compare_queries(GENERATED, spec, tampered)}
        self.assertFalse(any(r["agree"] for r in results.values()))
        self.assertIn("miniql 7, prometheus 7.000001", results["value"]["detail"])
        self.assertIn("series only in miniql", results["series"]["detail"])
        self.assertIn("only miniql rejected it", results["error"]["detail"])
        self.assertIn("miniql 12, prometheus 13", results["range"]["detail"])
        self.assertTrue(main.same_response({"status": "success", "data": {"resultType": "vector", "result": [{"metric": {"le": "1"}, "value": [0, "NaN"]}]}}, {"status": "success", "data": {"resultType": "vector", "result": [{"metric": {"le": "1.0"}, "value": [0, "NaN"]}]}}, 1e-9)[0])

    def write_generated(self):
        handle = tempfile.NamedTemporaryFile("w", suffix=".om", delete=False)
        handle.write(main.generate_openmetrics())
        handle.close()
        self.addCleanup(os.unlink, handle.name)
        return handle.name


class GeneratedDataTests(unittest.TestCase):
    def test_the_generator_is_deterministic_and_writes_one_block_per_family(self):
        text = main.generate_openmetrics()
        self.assertEqual(text, main.generate_openmetrics())
        self.assertTrue(text.endswith("# EOF\n"))
        families = [line.split()[2] for line in text.splitlines() if line.startswith("# TYPE")]
        self.assertEqual(families, ["cc_build_info", "cc_http_requests", "cc_http_request_duration_seconds", "cc_queue_depth"])
        seen = []
        for line in text.splitlines():
            if not line.startswith("#"):
                family = next(f for f in families if line.startswith(f))
                if not seen or seen[-1] != family:
                    seen.append(family)
        self.assertEqual(seen, families)
        stats = GENERATED.stats()
        self.assertEqual((stats["series"], stats["samples"]), (26, 2036))
        c = GENERATED.series[main.as_labels({"__name__": "cc_http_requests_total", "job": "api", "instance": "c", "code": "200"})]
        self.assertEqual((c.times[0], c.times[-1]), (BASE + 1_500_000, BASE + 1_800_000))

    def test_known_answers_the_smoke_checks_against_prometheus(self):
        def one(expr, seconds):
            return values(query(GENERATED, expr, BASE + seconds * 1000))
        self.assertEqual(one('rate(cc_http_requests_total{instance="a",code="200"}[5m])', 900), {'{code="200",instance="a",job="api"}': 1.0})
        self.assertAlmostEqual(one('increase(cc_http_requests_total{instance="b",code="200"}[5m])', 1320)['{code="200",instance="b",job="api"}'], 500 * 300 / 270)
        self.assertAlmostEqual(one('rate(cc_http_requests_total{instance="c"}[5m])', 1650)['{code="200",instance="c",job="api"}'], 225 * 165 / 150 / 300)
        joined = one("sum by (instance) (rate(cc_http_requests_total[5m])) * on (instance) group_left (version, zone) cc_build_info", 1700)
        self.assertAlmostEqual(joined['{instance="a",version="1.4.2",zone="eu-west-1a"}'], 1 + 70 / 300)
        self.assertEqual(set(joined), {'{instance="a",version="1.4.2",zone="eu-west-1a"}', '{instance="b",version="1.5.0",zone="eu-west-1b"}', '{instance="c",version="1.5.0",zone="eu-west-1a"}'})
        p50 = one('histogram_quantile(0.5, rate(cc_http_request_duration_seconds_bucket{instance="b"}[5m]))', 1320)['{instance="b",job="api"}']
        self.assertTrue(0.1 < p50 < 0.25, p50)


class RuleTestTests(unittest.TestCase):
    def test_expanding_notation_matches_the_promtool_documentation(self):
        self.assertEqual(main.expand_series_values("-2+4x3"), [-2, 2, 6, 10])
        self.assertEqual(main.expand_series_values("1-2x4"), [1, -1, -3, -5, -7])
        self.assertEqual(main.expand_series_values("1x4"), [1, 1, 1, 1, 1])
        self.assertEqual(main.expand_series_values("1 _x3 stale"), [1, None, None, None, "stale"])
        self.assertEqual(main.expand_series_values("0+0.1x3")[-1], 0.1 + 0.1 + 0.1)
        with self.assertRaises(main.InputError):
            main.expand_series_values("1+x3")

    def test_the_fixture_rule_tests_pass_and_show_sum_before_rate(self):
        report = main.run_rule_tests(Path(RULES_TEST))
        self.assertTrue(report["ok"], report)
        self.assertEqual(len(report["cases"]), 7)
        sums = {c["expr"]: c["got"] for c in report["cases"]}
        self.assertEqual(sums["sum(rate(cc_requests_total[5m]))"], [["{}", "2.6"]])
        self.assertEqual(sums["rate(job:cc_requests:sum[5m])"], [['{job="api",team="checkout"}', "5.075"]])
        code, out, _ = run(["test", RULES_TEST])
        self.assertEqual(code, 0)
        self.assertTrue(out.rstrip().endswith("SUCCESS"))

    def test_a_wrong_expectation_fails_with_expected_and_got(self):
        with tempfile.TemporaryDirectory() as tmp:
            spec = json.loads(Path(RULES_TEST).read_text())
            spec["rule_files"] = [os.path.abspath(os.path.join(FIXTURES, "rules.json"))]
            spec["tests"][0]["promql_expr_test"][2]["exp_samples"][0]["value"] = 2
            broken = Path(tmp) / "broken.json"
            broken.write_text(json.dumps(spec))
            code, out, _ = run(["test", str(broken)])
        self.assertEqual(code, 1)
        self.assertIn('FAILED rate(cc_requests_total{instance="b"}[5m]) at 12m', out)
        self.assertIn('exp: {code="200",instance="b",job="api"} 2', out)
        self.assertIn('got: {code="200",instance="b",job="api"} 1.5', out)
        self.assertTrue(out.rstrip().endswith("FAILED:"))

    def test_a_recorded_series_that_disappears_gets_a_stale_marker(self):
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, "rules.json").write_text(json.dumps({"groups": [{"name": "g", "rules": [{"record": "busy", "expr": "load > 5", "labels": {"src": "rule"}}, {"alert": "Busy", "expr": "busy"}]}]}))
            Path(tmp, "test.json").write_text(json.dumps({"rule_files": ["rules.json"], "tests": [{"input_series": [{"series": "load", "values": "9 9 1 1"}], "promql_expr_test": [
                {"expr": "busy", "eval_time": "1m", "exp_samples": [{"labels": 'busy{src="rule"}', "value": 9}]},
                {"expr": "busy", "eval_time": "2m30s", "exp_samples": []},
            ]}]}))
            report = main.run_rule_tests(Path(tmp, "test.json"))
        self.assertTrue(report["ok"], report)
        self.assertEqual(report["skipped_alerts"], ["Busy"])


class CliTests(unittest.TestCase):
    def test_query_json_exit_codes_and_messages(self):
        with tempfile.TemporaryDirectory() as tmp:
            series = os.path.join(tmp, "series.om")
            self.assertEqual(run(["generate", "-o", series])[0], 0)
            code, out, _ = run(["query", "--series", series, "--at", "2026-01-01T00:15:00Z", "--json", 'sum by (code) (rate(cc_http_requests_total[5m]))'])
            self.assertEqual(code, 0)
            body = json.loads(out)
            self.assertEqual(body["data"]["resultType"], "vector")
            self.assertEqual({r["metric"]["code"] for r in body["data"]["result"]}, {"200", "500"})
            code, _, err = run(["query", "--series", series, "rate(sum(cc_http_requests_total)[5m])"])
            self.assertEqual((code, "ranges only allowed for vector selectors" in err), (2, True))
            code, _, err = run(["query", "--series", series, "cc_http_requests_total * on (job) cc_build_info"])
            self.assertEqual((code, "execution error" in err), (1, True))
            code, _, err = run(["query", "--series", os.path.join(tmp, "missing.om"), "up"])
            self.assertEqual((code, "cannot read" in err), (2, True))
            code, out, _ = run(["range", "--series", series, "--start", "+10m", "--end", "+11m", "--step", "30s", 'cc_http_requests_total{instance="a",code="200"}'])
            self.assertEqual(code, 0)
            self.assertIn("2026-01-01T00:10:30Z 1630", out)
            code, out, _ = run(["stats", "--series", series, "--json"])
            self.assertEqual(json.loads(out)["series_per_metric"]["cc_http_requests_total"], 5)


if __name__ == "__main__":
    unittest.main()
