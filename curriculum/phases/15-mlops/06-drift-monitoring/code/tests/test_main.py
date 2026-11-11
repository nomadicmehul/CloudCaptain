import argparse
import contextlib
import io
import json
import math
import os
import re
import shutil
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import main  # noqa: E402

EQUAL_A = [0.1 * i for i in range(40)]
EQUAL_B = [0.1 * i + 0.85 for i in range(40)]
UNEQUAL_A = [(i * 37) % 101 / 10 for i in range(30)]
UNEQUAL_B = [((i * 53) % 97) / 10 + 1.5 for i in range(45)]
PSI_REF = [(i * 0.37) % 10 for i in range(60)]
PSI_CUR = [((i * 0.53) % 10) * 1.2 for i in range(50)]
CAT_REF = ["a"] * 50 + ["b"] * 30 + ["c"] * 20


def run_cli(*argv):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = main.main(list(argv))
    return code, out.getvalue(), err.getvalue()


def write_csv(path, header, rows):
    lines = [",".join(header)] + [",".join(str(v) for v in row) for row in rows]
    Path(path).write_text("\n".join(lines) + "\n")


class StatisticsTests(unittest.TestCase):
    def test_sturges_edges_match_numpy(self):
        edges = main.sturges_edges([1.5, 2.0, 7.25, 3.0, 9.5, 4.4, 0.3])
        self.assertEqual(edges, [0.3, 2.5999999999999996, 4.8999999999999995, 7.199999999999999, 9.5])
        self.assertEqual(main.sturges_edges([2.0, 2.0, 2.0]), [1.5, 2.5])

    def test_histogram_counts_the_last_edge_in_the_last_bin(self):
        self.assertEqual(main.histogram([0.0, 0.5, 1.0, 1.5, 2.0], [0.0, 1.0, 2.0]), [2, 3])
        self.assertEqual(main.histogram([-1.0, 3.0], [0.0, 1.0, 2.0]), [0, 0])

    def test_fill_zeroes_uses_one_in_ten_thousand_or_a_millionth_of_the_smallest_share(self):
        self.assertEqual(main.fill_zeroes([0.5, 0.0, 0.5]), [0.5, 0.0001, 0.5])
        self.assertEqual(main.fill_zeroes([0.99995, 0.00005, 0.0]), [0.99995, 0.00005, 0.00005 / 10 ** 6])

    def test_psi_matches_evidently_for_numerical_and_categorical_columns(self):
        self.assertAlmostEqual(main.psi(PSI_REF, PSI_CUR, True), 0.9275365989695084, places=12)
        current = ["a"] * 30 + ["b"] * 30 + ["c"] * 35 + ["d"] * 5
        self.assertAlmostEqual(main.psi(CAT_REF, current, False), 0.49621643705477886, places=12)
        self.assertEqual(main.psi(PSI_REF, PSI_REF, True), 0.0)

    def test_jensen_shannon_matches_scipy_and_evidently(self):
        self.assertAlmostEqual(main.jensen_shannon([1, 0], [0.5, 0.5]), 0.464501404022459, places=12)
        self.assertAlmostEqual(main.jensen_shannon([1.0, 0.0, 0.0], [0.0, 1.0, 0.0], 2.0), 1.0, places=12)
        self.assertAlmostEqual(main.js_distance(PSI_REF, PSI_CUR, True), 0.22624268188167224, places=12)
        self.assertEqual(main.jensen_shannon([3, 1], [6, 2]), 0.0)

    def test_ks_exact_p_value_for_equal_sizes_matches_scipy(self):
        statistic, p_value, mode = main.ks_2samp(EQUAL_A, EQUAL_B)
        self.assertAlmostEqual(statistic, 0.225, places=12)
        self.assertAlmostEqual(p_value, 0.2656871402817289, places=12)
        self.assertEqual(mode, "exact")

    def test_ks_exact_p_value_for_unequal_sizes_matches_scipy(self):
        statistic, p_value, mode = main.ks_2samp(UNEQUAL_A, UNEQUAL_B)
        self.assertAlmostEqual(statistic, 1 / 6, places=12)
        self.assertAlmostEqual(p_value, 0.659963847805047, places=12)
        self.assertEqual(mode, "exact")

    def test_ks_switches_to_the_asymptotic_distribution_above_ten_thousand_rows(self):
        big_a = [(i * 7919) % 12000 / 12000 for i in range(12000)]
        big_b = [((i * 7919) % 11000) / 11000 + 0.02 for i in range(11000)]
        statistic, p_value, mode = main.ks_2samp(big_a, big_b)
        self.assertEqual(mode, "asymptotic")
        self.assertAlmostEqual(statistic, 0.020083333333333342, places=12)
        self.assertLess(abs(p_value - 0.019254343663002226), 5e-4)

    def test_ks_handles_ties_and_identical_samples(self):
        self.assertEqual(main.ks_2samp([1.0, 1.0, 2.0], [1.0, 2.0, 1.0]), (0.0, 1.0, "exact"))
        statistic, p_value, _ = main.ks_2samp([1.0, 1.0, 1.0, 1.0], [2.0, 2.0, 2.0, 2.0])
        self.assertEqual(statistic, 1.0)
        self.assertAlmostEqual(p_value, 2 / 70, places=12)
        with self.assertRaises(main.InputError):
            main.ks_2samp([], [1.0])

    def test_wasserstein_matches_scipy_and_is_normalised_by_the_reference_std(self):
        self.assertEqual(main.wasserstein([0, 1, 3], [5, 6, 8]), 5.0)
        self.assertAlmostEqual(main.wasserstein([0, 1, 3, 3.5], [0.5, 6, 8]), 3.041666666666667, places=12)
        self.assertAlmostEqual(main.wasserstein_norm([0, 1, 3], [5, 6, 8]), 5.0 / math.sqrt(14 / 9), places=12)
        self.assertAlmostEqual(main.wasserstein_norm([4.0, 4.0], [4.0, 4.001]), 0.5, places=9)

    def test_chi2_survival_function_matches_known_values(self):
        self.assertAlmostEqual(main.chi2_sf(3.841458820694124, 1), 0.05, places=12)
        self.assertAlmostEqual(main.chi2_sf(18.307038053275146, 10), 0.05, places=12)
        self.assertAlmostEqual(main.chi2_sf(5.0, 2), math.exp(-2.5), places=14)
        self.assertLess(abs(main.chi2_sf(250.0, 3) / 6.543750030967994e-54 - 1), 1e-9)
        self.assertEqual(main.chi2_sf(math.inf, 4), 0.0)

    def test_chi_square_matches_scipy_and_a_new_category_makes_it_infinite(self):
        statistic, p_value, df = main.chi_square(CAT_REF, ["a"] * 40 + ["b"] * 25 + ["c"] * 25)
        self.assertAlmostEqual(statistic, 3.425925925925926, places=12)
        self.assertAlmostEqual(p_value, 0.1803306871127069, places=10)
        self.assertEqual(df, 2)
        statistic, p_value, _ = main.chi_square(CAT_REF, ["a"] * 49 + ["d"])
        self.assertEqual((statistic, p_value), (math.inf, 0.0))

    def test_method_depends_on_sample_size_unless_overridden(self):
        self.assertEqual(main.choose_method("numerical", 1000, None), "ks")
        self.assertEqual(main.choose_method("numerical", 1001, None), "wasserstein")
        self.assertEqual(main.choose_method("categorical", 500, None), "chisquare")
        self.assertEqual(main.choose_method("categorical", 5000, None), "jensenshannon")
        self.assertEqual(main.choose_method("numerical", 5000, "psi"), "psi")
        self.assertTrue(main.is_drift("ks", 0.01, 0.05))
        self.assertFalse(main.is_drift("psi", 0.05, 0.1))


class ColumnTests(unittest.TestCase):
    def test_quality_flags_missing_values_type_errors_range_and_new_categories(self):
        options = main.Options()
        ref = main.column_data([{"x": str(v)} for v in range(100)], "x", "numerical")
        cur = main.column_data([{"x": ""}] * 10 + [{"x": "oops"}] + [{"x": "250"}] * 5 + [{"x": str(v)} for v in range(84)], "x", "numerical")
        report = main.quality("x", "numerical", ref, cur, options)
        self.assertEqual((report["missing_reference"], report["missing_current"], report["type_errors"]), (0.0, 0.1, 1))
        self.assertAlmostEqual(report["out_of_range"], 5 / 89)
        self.assertEqual(len(report["issues"]), 3)
        ref = main.column_data([{"c": "eu"}, {"c": "us"}], "c", "categorical")
        cur = main.column_data([{"c": "eu"}, {"c": "apac"}, {"c": "NA"}], "c", "categorical")
        report = main.quality("c", "categorical", ref, cur, options)
        self.assertEqual(report["new_categories"], ["apac"])
        self.assertTrue(any("1 new category: apac" in issue for issue in report["issues"]))

    def test_text_drift_measures_length_token_divergence_and_out_of_vocabulary_rate(self):
        reference = ["where is my order"] * 50
        current = ["where is my order please refund my card twice"] * 50
        stats = main.text_stats(reference, current, 0.05)
        self.assertEqual((stats["mean_tokens_reference"], stats["mean_tokens_current"]), (4.0, 9.0))
        self.assertAlmostEqual(stats["oov_rate"], 4 / 9)
        self.assertEqual(stats["top_new_tokens"], ["card", "please", "refund", "twice"])
        self.assertLess(stats["length_ks_p_value"], 1e-10)
        same = main.text_stats(reference, reference, 0.05)
        self.assertEqual((same["oov_rate"], same["token_jensenshannon"], same["length_ks_p_value"]), (0.0, 0.0, 1.0))
        result = main.compare_column("m", "text", "feature", main.ColumnData(current, 50), main.ColumnData(current, 50), main.Options())
        self.assertFalse(result.drifted)

    def test_performance_uses_late_labels_and_needs_enough_coverage(self):
        options = main.Options()
        ref = [{"id": f"a{i}", "label": str(i % 2), "prediction": "0.9" if i % 2 else "0.1"} for i in range(100)]
        cur = [{"id": f"b{i}", "label": "", "prediction": "0.9"} for i in range(100)]
        labels = {f"b{i}": str(i % 2) for i in range(30)}
        result = main.performance(ref, cur, labels, "id", "label", "prediction", options)
        self.assertEqual((result["metric"], result["reference"], result["current"]), ("accuracy", 1.0, 0.5))
        self.assertEqual((result["coverage"], result["status"]), (0.3, "decayed"))
        few = main.performance(ref, cur, {"b1": "1"}, "id", "label", "prediction", options)
        self.assertEqual((few["status"], few["current"]), ("insufficient_labels", 1.0))
        reg = main.performance([{"id": "r", "label": "2.5", "prediction": "2.0"}], [{"id": "c", "label": "1.0", "prediction": "2.0"}], {}, "id", "label", "prediction", options)
        self.assertEqual((reg["metric"], reg["reference"], reg["current"], reg["status"]), ("mae", 0.5, 1.0, "decayed"))


class EndToEndTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.files = main.generate(self.tmp / "data", 600, 7)

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def args(self, *extra):
        f = self.files
        return [f["reference"], f["current"], "--schema", f["schema"], "--labels", f["labels"], "--model", "churn", *extra]

    def test_generate_is_deterministic_for_a_seed(self):
        again = main.generate(self.tmp / "again", 600, 7)
        for key in ("reference", "current", "labels"):
            self.assertEqual(Path(self.files[key]).read_text(), Path(again[key]).read_text())
        other = main.generate(self.tmp / "other", 600, 8)
        self.assertNotEqual(Path(self.files["current"]).read_text(), Path(other["current"]).read_text())

    def test_report_flags_the_planted_shifts(self):
        code, out, _ = run_cli("report", *self.args("--json"))
        self.assertEqual(code, 0)
        report = json.loads(out)
        drifted = {c["column"] for c in report["columns"] if c["drifted"]}
        self.assertLessEqual({"income", "plan", "region", "message", "prediction"}, drifted)
        methods = {c["column"]: c["method"] for c in report["columns"]}
        self.assertEqual((methods["income"], methods["plan"], methods["message"]), ("ks", "chisquare", "text"))
        issues = {q["column"]: q["issues"] for q in report["quality"]}
        self.assertTrue(issues["age"] and issues["region"])
        self.assertEqual(report["performance"]["status"], "decayed")
        self.assertTrue(report["dataset"]["drift"])
        code, out, _ = run_cli("report", *self.args("--fail-on-drift"))
        self.assertEqual(code, 1)
        self.assertIn("income", out)
        self.assertIn("DRIFT", out)

    def test_bad_input_exits_two_with_a_message(self):
        code, _, err = run_cli("report", str(self.tmp / "nope.csv"), self.files["current"])
        self.assertEqual(code, 2)
        self.assertIn("no such file", err)
        write_csv(self.tmp / "narrow.csv", ["id", "age"], [["x", 1]])
        code, _, err = run_cli("report", self.files["reference"], str(self.tmp / "narrow.csv"), "--schema", self.files["schema"])
        self.assertEqual(code, 2)
        self.assertIn("schema change", err)
        (self.tmp / "schema.json").write_text(json.dumps({"numerical": ["ghost"]}))
        code, _, err = run_cli("report", self.files["reference"], self.files["current"], "--schema", str(self.tmp / "schema.json"))
        self.assertEqual(code, 2)
        self.assertIn("ghost", err)

    def test_metrics_are_valid_prometheus_text(self):
        code, out, _ = run_cli("metrics", *self.args())
        self.assertEqual(code, 0)
        sample = re.compile(r'^[a-z_]+\{(?:[a-z_]+="(?:[^"\\]|\\.)*",)*[a-z_]+="(?:[^"\\]|\\.)*"\} (?:[-+]?[0-9.e+-]+|NaN|[+-]Inf)$')
        declared = set()
        for line in out.splitlines():
            if line.startswith("# TYPE "):
                declared.add(line.split()[2])
                self.assertTrue(line.endswith(" gauge"))
            elif not line.startswith("# HELP "):
                self.assertRegex(line, sample)
                self.assertIn(line.split("{")[0], declared)
        self.assertIn('minidrift_feature_drift_detected{model="churn",method="ks",feature="income",kind="numerical",role="feature"} 1.0', out)
        self.assertIn('minidrift_feature_missing_ratio{model="churn",feature="age",window="reference"} 0.0', out)
        self.assertEqual(main.label_value('a"b\\c\nd'), 'a\\"b\\\\c\\nd')

    def test_rules_cover_drift_quality_and_staleness(self):
        code, out, _ = run_cli("rules", "--for", "15m")
        self.assertEqual(code, 0)
        self.assertEqual(re.findall(r"- alert: (\w+)", out), ["FeatureDrift", "PredictionDrift", "DatasetDrift", "FeatureMissingValues", "DriftCheckStale"])
        self.assertIn('minidrift_feature_drift_detected{role="feature"} == 1', out)
        self.assertEqual(out.count("for: 15m"), 4)
        self.assertIn("{{ $labels.feature }}", out)

    def test_serve_exposes_metrics_and_recomputes_when_an_input_changes(self):
        f = self.files
        args = argparse.Namespace(reference=f["reference"], current=f["current"], labels=None, schema=f["schema"], host="127.0.0.1", port=0)
        server = main.make_server(args, main.Options(model="churn"))
        thread = threading.Thread(target=server.serve_forever)
        thread.start()
        try:
            base = f"http://127.0.0.1:{server.server_address[1]}"
            with urllib.request.urlopen(base + "/metrics", timeout=10) as response:
                self.assertIn("version=0.0.4", response.headers["Content-Type"])
                body = response.read().decode()
            self.assertIn('feature="income",kind="numerical",role="feature"} 1.0', body)
            shutil.copy(f["reference"], f["current"])
            os.utime(f["current"], ns=(10 ** 18, 10 ** 18))
            with urllib.request.urlopen(base + "/report", timeout=10) as response:
                report = json.loads(response.read())
            self.assertEqual(report["dataset"]["drifted"], 0)
            self.assertEqual(server.state.runs, 2)
            with self.assertRaises(urllib.error.HTTPError) as caught:
                urllib.request.urlopen(base + "/nope", timeout=10)
            self.assertEqual(caught.exception.code, 404)
            caught.exception.close()
        finally:
            server.shutdown()
            server.server_close()
            thread.join()


if __name__ == "__main__":
    unittest.main()
