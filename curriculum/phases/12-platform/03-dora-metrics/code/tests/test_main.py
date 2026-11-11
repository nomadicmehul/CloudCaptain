import contextlib
import io
import json
import os
import shutil
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
CODE = os.path.dirname(HERE)
sys.path.insert(0, CODE)

import main  # noqa: E402

START = main.parse_time("2026-09-07T00:00:00Z", "start")
H = main.HOUR


def at(hour):
    return START + int(hour * H)


def graph(*spec):
    return {sha: main.Commit(sha, list(parents), at(hour), "dev", f"commit {sha}") for sha, parents, hour in spec}


def deploy(name, sha, hour, **extra):
    return main.Deploy(name, sha, at(hour), **extra)


def incident(name, started, resolved, **extra):
    return main.Incident(name, at(started), None if resolved is None else at(resolved), **extra)


LINEAR = graph(("a", [], 0), ("b", ["a"], 1), ("c", ["b"], 2), ("d", ["c"], 3), ("e", ["d"], 4), ("f", ["e"], 5))

MERGES = graph(
    ("root", [], 0),
    ("x1", ["root"], 1), ("x2", ["x1"], 2),
    ("m1", ["root", "x2"], 5),
    ("y1", ["root"], 3),
    ("m2", ["m1", "y1"], 6),
    ("fix", ["m2"], 7),
)

GIT_LOG = "".join(
    f"{sha}\x1f{' '.join(c.parents)}\x1f{c.authored}\x1f{c.author}\x1f{c.subject}\x1e\n" for sha, c in MERGES.items()
)


def git_available():
    return shutil.which("git") is not None


class ParsingTests(unittest.TestCase):
    def test_git_log_records_round_trip(self):
        commits = main.parse_git_log(GIT_LOG)
        self.assertEqual(set(commits), set(MERGES))
        self.assertEqual(commits["m2"].parents, ["m1", "y1"])
        self.assertEqual(commits["root"].parents, [])
        self.assertEqual(commits["x1"].authored, at(1))

    def test_git_log_with_the_wrong_format_is_refused(self):
        with self.assertRaises(main.InputError) as raised:
            main.parse_git_log("abc def\x1e\n")
        self.assertIn("--format=", str(raised.exception))

    def test_times_need_a_zone(self):
        self.assertEqual(main.parse_time("2026-09-07T02:00:00+02:00", "t"), START)
        self.assertEqual(main.iso(main.parse_time("2026-09-07T00:00:00Z", "t")), "2026-09-07T00:00:00Z")
        with self.assertRaises(main.InputError):
            main.parse_time("2026-09-07T00:00:00", "t")

    def test_percentile_interpolates_like_percentile_cont(self):
        self.assertEqual(main.percentile([4, 1, 3, 2], 0.5), 2.5)
        self.assertAlmostEqual(main.percentile([1, 2, 3, 4, 5, 6, 7, 8, 9, 10], 0.9), 9.1)
        self.assertEqual(main.percentile([7], 0.9), 7)
        self.assertIsNone(main.percentile([], 0.5))

    def test_github_deployments_use_the_first_success_status(self):
        items = [
            {"id": 2, "sha": "c", "environment": "production", "created_at": "2026-09-07T09:55:00Z",
             "statuses": [{"state": "success", "created_at": "2026-09-07T10:00:00Z"},
                          {"state": "in_progress", "created_at": "2026-09-07T09:56:00Z"}]},
            {"id": 1, "sha": "b", "environment": "production", "created_at": "2026-09-07T04:55:00Z",
             "statuses": [{"state": "inactive", "created_at": "2026-09-07T10:00:00Z"},
                          {"state": "success", "created_at": "2026-09-07T05:00:00Z"}]},
            {"id": 3, "sha": "d", "environment": "production", "created_at": "2026-09-07T11:55:00Z",
             "statuses": [{"state": "failure", "created_at": "2026-09-07T12:00:00Z"}]},
            {"id": 4, "sha": "d", "environment": "staging", "created_at": "2026-09-07T11:00:00Z",
             "statuses": [{"state": "success", "created_at": "2026-09-07T11:05:00Z"}]},
        ]
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "deployments.json")
            with open(path, "w") as handle:
                json.dump(items, handle)
            deploys = {d.id: d for d in main.load_deploys(path)}
        self.assertEqual((deploys["1"].status, deploys["1"].time), ("success", at(5)))
        self.assertEqual((deploys["3"].status, deploys["3"].time), ("failure", at(12)))
        report = main.compute(LINEAR, list(deploys.values()), [], since=START, until=START + main.DAY)
        self.assertEqual([r.deploy.id for r in report.deploys], ["1", "2"])
        self.assertEqual([a.id for a in report.attempts], ["3"])

    def test_bad_json_lines_name_the_line(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "deploys.jsonl")
            with open(path, "w") as handle:
                handle.write('{"id": "d1", "sha": "a", "time": "2026-09-07T01:00:00Z"}\n{oops\n')
            with self.assertRaises(main.InputError) as raised:
                main.load_deploys(path)
        self.assertIn("deploys.jsonl:2", str(raised.exception))


class GraphTests(unittest.TestCase):
    def test_changes_follow_the_first_parent_and_claim_branch_commits(self):
        changes = main.split_changes(MERGES, "fix", {"root"})
        self.assertEqual([head for head, _ in changes], ["m1", "m2", "fix"])
        self.assertEqual(dict(changes), {"m1": ["x1", "x2", "m1"], "m2": ["y1", "m2"], "fix": ["fix"]})

    def test_already_deployed_commits_are_not_changes_again(self):
        self.assertEqual(main.split_changes(MERGES, "m2", {"root", "x1", "x2", "m1"}), [("m2", ["y1", "m2"])])
        self.assertEqual(main.split_changes(MERGES, "m1", set(main.walk(MERGES, "m2", set()))), [])

    def test_each_commit_is_attributed_to_its_first_deploy(self):
        problems = []
        results = main.attribute(MERGES, [deploy("d1", "root", 0.5), deploy("d2", "m1", 8), deploy("d3", "fix", 10)], problems)
        self.assertEqual(problems, [])
        self.assertEqual([[c.head for c in r.changes] for r in results], [["root"], ["m1"], ["m2", "fix"]])
        m2 = results[2].changes[0]
        self.assertEqual((m2.first_authored, m2.lead), (at(3), 7 * H))
        self.assertEqual(sorted(m2.commit_leads), [4 * H, 7 * H])

    def test_rollback_is_a_deploy_of_an_ancestor_and_a_redeploy_is_not(self):
        results = main.attribute(LINEAR, [deploy("d1", "b", 1), deploy("d2", "d", 4), deploy("d3", "b", 5),
                                          deploy("d4", "b", 6), deploy("d5", "f", 7)], [])
        self.assertEqual([r.rollback_of for r in results], ["", "", "d2", "", ""])
        self.assertEqual([len(r.changes) for r in results], [2, 2, 0, 0, 2])

    def test_unknown_and_short_shas(self):
        problems = []
        commits = graph(("aaaaaaa111", [], 0), ("aaaaaaa222", ["aaaaaaa111"], 1), ("bbbbbbb333", ["aaaaaaa222"], 2))
        results = main.attribute(commits, [deploy("d1", "bbbbbbb", 3), deploy("d2", "aaaaaaa", 4), deploy("d3", "zzzz", 5)], problems)
        self.assertEqual([r.deploy.sha for r in results], ["bbbbbbb333"])
        self.assertEqual(len(problems), 2)


class FailureTests(unittest.TestCase):
    def run_scenario(self, incidents, deploys=None, window=24):
        deploys = deploys or [deploy("d1", "b", 1), deploy("d2", "c", 10), deploy("d3", "d", 20), deploy("d4", "e", 30)]
        return main.compute(LINEAR, deploys, incidents, window_hours=window, since=START, until=START + 2 * main.DAY)

    def test_window_links_to_the_latest_deploy_before_the_incident(self):
        report = self.run_scenario([incident("i1", 21, 23), incident("i2", 9, 12, external=True)])
        failed = {r.deploy.id: r for r in report.deploys if r.failed}
        self.assertEqual(list(failed), ["d3"])
        self.assertEqual(failed["d3"].recovery, 3 * H)
        self.assertEqual({l.incident.id: l.how for l in report.links}, {"i1": "window", "i2": "external"})
        self.assertEqual(report.metrics["incident_time_to_restore"]["incidents"], 2)

    def test_root_cause_wins_over_the_window_and_outside_the_window_is_unlinked(self):
        report = self.run_scenario([incident("i1", 31, 35, root_cause="c"), incident("i2", 40, 41)], window=6)
        self.assertEqual([r.deploy.id for r in report.deploys if r.failed], ["d2"])
        self.assertEqual(report.deploys[1].recovery, 25 * H)
        self.assertEqual({l.incident.id: (l.deploy, l.how) for l in report.links}, {"i1": ("d2", "root cause"), "i2": ("", "unlinked")})
        self.assertIn("unlinked-incidents", [w["id"] for w in report.warnings])

    def test_rollback_fails_the_previous_deploy_and_restores_at_the_rollback(self):
        deploys = [deploy("d1", "b", 1), deploy("d2", "d", 4), deploy("d3", "b", 5), deploy("d4", "f", 30)]
        report = self.run_scenario([incident("i1", 4.5, 9)], deploys)
        d2 = report.deploys[1]
        self.assertEqual(d2.causes, ["rolled back by d3", "incident i1"])
        self.assertEqual(d2.recovery, 1 * H)
        self.assertEqual(report.metrics["change_failure_rate"]["failed"], 1)
        self.assertEqual(report.metrics["change_failure_rate"]["percent"], 25.0)

    def test_a_rollback_outside_the_window_is_not_a_change_failure(self):
        deploys = [deploy("d1", "b", 1), deploy("d2", "d", 4), deploy("d3", "b", 40)]
        report = self.run_scenario([], deploys, window=24)
        self.assertEqual(report.deploys[2].rollback_of, "d2")
        self.assertFalse(report.deploys[1].failed)

    def test_an_open_incident_leaves_recovery_missing(self):
        report = self.run_scenario([incident("i1", 11, None)])
        self.assertTrue(report.deploys[1].failed)
        self.assertIsNone(report.deploys[1].recovery)
        self.assertEqual(report.metrics["failed_deployment_recovery_time"]["restored"], 0)
        self.assertIn("open-incidents", [w["id"] for w in report.warnings])


class LevelAndWarningTests(unittest.TestCase):
    def test_levels_per_metric_and_year(self):
        cfr_2024 = main.CLUSTERS["2024"]["change_failure_rate"]
        self.assertEqual(main.level_nearest(3, cfr_2024), "elite")
        self.assertEqual(main.level_nearest(8, cfr_2024), "medium")
        self.assertEqual(main.level_nearest(19, cfr_2024), "high")
        self.assertEqual(main.level_nearest(30, cfr_2024), "low")
        self.assertEqual(main.level_nearest(12, main.CLUSTERS["2023"]["change_failure_rate"]), "high")
        self.assertEqual(main.level_under(9.5 * H, main.CLUSTERS["2023"]["recovery_under"]), "high")
        self.assertEqual(main.level_under(3 * main.DAY, main.CLUSTERS["2023"]["recovery_under"]), "medium")
        self.assertEqual(main.level_under(3 * main.DAY, main.CLUSTERS["2024"]["recovery_under"]), "low")
        self.assertEqual(main.level_at_least(0.75, main.CLUSTERS["2024"]["deploys_per_day_at_least"]), "high")
        self.assertEqual(main.level_at_least(1 / 60, main.CLUSTERS["2024"]["deploys_per_day_at_least"]), "low")

    def test_gaming_warnings(self):
        burst = [deploy(f"d{n}", sha, 1 + n * 0.1) for n, sha in enumerate("abcdef")]
        burst += [deploy(f"r{n}", "f", 2 + n * 0.1) for n in range(6)]
        report = main.compute(LINEAR, burst, [], since=START, until=START + 7 * main.DAY)
        ids = [w["id"] for w in report.warnings]
        self.assertEqual(report.metrics["deployment_frequency"]["deploys"], 12)
        self.assertEqual(ids, ["small-sample", "empty-deploys", "bursty-deploys", "no-failures-recorded", "single-commit-changes"])
        self.assertEqual(report.metrics["deployment_frequency"]["median_deploy_days_per_week"], 1.0)
        fortnight = main.compute(LINEAR, burst, [], since=START, until=START + 14 * main.DAY)
        self.assertNotIn("bursty-deploys", [w["id"] for w in fortnight.warnings])
        self.assertEqual(fortnight.metrics["deployment_frequency"]["median_deploy_days_per_week"], 0.5)

    def test_weekly_view_lists_empty_weeks(self):
        report = main.compute(LINEAR, [deploy("d1", "b", 1), deploy("d2", "f", 24 * 15)], [], since=START, until=START + 21 * main.DAY)
        rows = main.view_rows(report, "weekly")
        self.assertEqual([(r["bucket"], r["deploys"]) for r in rows], [("2026-W37", 1), ("2026-W38", 0), ("2026-W39", 1)])
        self.assertIsNone(rows[1]["change_failure_percent"])


class CliTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp)

    def write(self, name, text):
        path = os.path.join(self.tmp, name)
        with open(path, "w") as handle:
            handle.write(text)
        return path

    def run_cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = main.main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def test_json_report_from_a_git_log_file_and_problems_exit_1(self):
        log = self.write("git.log", GIT_LOG)
        good = self.write("good.jsonl", "".join(json.dumps({"id": i, "sha": s, "time": main.iso(at(h))}) + "\n"
                                               for i, s, h in [("d1", "root", 0.5), ("d2", "m1", 8), ("d3", "fix", 10)]))
        code, out, err = self.run_cli("report", "--git-log", log, "--deploys", good, "--json")
        self.assertEqual((code, err), (0, ""))
        doc = json.loads(out)
        self.assertEqual(doc["metrics"]["deployment_frequency"]["deploys"], 3)
        self.assertEqual([c["head"] for c in doc["changes"]], ["root", "m1", "m2", "fix"])
        bad = self.write("bad.jsonl", json.dumps({"id": "d9", "sha": "deadbeef99", "time": main.iso(at(9))}) + "\n")
        with open(good) as first, open(bad) as second:
            both = self.write("both.jsonl", first.read() + second.read())
        code, out, err = self.run_cli("report", "--git-log", log, "--deploys", both)
        self.assertEqual(code, 1)
        self.assertIn("deadbeef99 is not in the Git history", err)

    def test_input_errors_exit_2(self):
        code, _, err = self.run_cli("report", "--git-log", os.path.join(self.tmp, "missing"), "--deploys", "x")
        self.assertEqual(code, 2)
        self.assertIn("cannot read", err)
        code, _, _ = self.run_cli("report")
        self.assertEqual(code, 2)


@unittest.skipUnless(git_available(), "git is not installed")
class DemoRepositoryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()
        with open(main.DEFAULT_SCENARIO) as handle:
            cls.scenario = json.load(handle)
        cls.paths = main.build_demo(cls.scenario, os.path.join(cls.tmp, "base"))

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp)

    def report(self, paths, deploys="deploys.jsonl"):
        commits = main.load_commits(paths["repo"])
        return main.compute(commits, main.load_deploys(os.path.join(os.path.dirname(paths["repo"]), deploys)),
                            main.load_incidents(paths["incidents.jsonl"]),
                            since=main.parse_day("2026-09-07", "s"), until=main.parse_day("2026-10-05", "u"))

    def test_the_scenario_gives_the_hand_checked_numbers(self):
        m = self.report(self.paths).metrics
        self.assertEqual(m["deployment_frequency"]["deploys"], 21)
        self.assertEqual(m["lead_time"]["changes"], 22)
        self.assertEqual(m["lead_time"]["median_seconds"], 8 * H)
        self.assertEqual(m["change_failure_rate"]["failed"], 4)
        self.assertAlmostEqual(m["change_failure_rate"]["percent"], 400 / 21)
        self.assertEqual(m["failed_deployment_recovery_time"]["median_seconds"], 9.5 * H)
        self.assertEqual(m["incident_time_to_restore"]["incidents"], 5)

    def test_github_deployments_give_the_same_metrics(self):
        self.assertEqual(self.report(self.paths).metrics, self.report(self.paths, "github-deployments.json").metrics)

    def test_a_slow_review_moves_lead_time_only(self):
        scenario = json.loads(json.dumps(self.scenario))
        scenario["changes"].append({"id": "pr-slow", "title": "Long review", "author": "eve", "commits": [20], "merged": 500})
        slow = self.report(main.build_demo(scenario, os.path.join(self.tmp, "slow")))
        base = self.report(self.paths)
        self.assertEqual(slow.metrics["lead_time"]["changes"], 23)
        self.assertGreater(slow.metrics["lead_time"]["p90_seconds"], base.metrics["lead_time"]["p90_seconds"])
        for key in ("deployment_frequency", "change_failure_rate", "failed_deployment_recovery_time"):
            self.assertEqual(slow.metrics[key], base.metrics[key])
        heads = [c.subject for c in slow.changes if c.deploy == "d-18"]
        self.assertEqual(heads, ["Merge pr-slow: Long review", "Merge pr-117: Move invoices to the new store"])


if __name__ == "__main__":
    unittest.main()
