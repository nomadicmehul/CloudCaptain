import contextlib
import copy
import io
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import main  # noqa: E402

FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures")


def fixture(name):
    with open(os.path.join(FIXTURES, name), encoding="utf-8") as handle:
        return json.load(handle)


def fixture_text(name):
    with open(os.path.join(FIXTURES, name), encoding="utf-8") as handle:
        return handle.read()


def by_address(report):
    return {finding.address: finding for finding in report.findings}


def labels_only_drift():
    return {
        "format_version": "1.2",
        "planned_values": {},
        "resource_drift": [
            {
                "address": "kubernetes_config_map_v1.routing",
                "mode": "managed",
                "type": "kubernetes_config_map_v1",
                "name": "routing",
                "provider_name": "registry.opentofu.org/hashicorp/kubernetes",
                "change": {
                    "actions": ["update"],
                    "before": {"metadata": [{"labels": {"app": "shop"}}]},
                    "after": {"metadata": [{"labels": {"app": "shop", "owner": "oncall"}}]},
                },
            }
        ],
        "errored": False,
    }


class FakeTofu:
    def __init__(self, refresh_text, plan_text, refresh_exit=2, plan_exit=2, stderr=""):
        self.refresh_text = refresh_text
        self.plan_text = plan_text
        self.refresh_exit = refresh_exit
        self.plan_exit = plan_exit
        self.stderr = stderr
        self.calls = []
        self.saved = {}

    def __call__(self, argv):
        argv = list(argv)
        self.calls.append(argv)
        if argv[2] == "plan":
            out = next(arg for arg in argv if arg.startswith("-out="))[len("-out="):]
            refresh_only = "-refresh-only" in argv
            code = self.refresh_exit if refresh_only else self.plan_exit
            if code == 1:
                return 1, "", self.stderr
            self.saved[out] = self.refresh_text if refresh_only else self.plan_text
            return code, "", ""
        if argv[2:4] == ["show", "-json"]:
            return 0, self.saved[argv[4]], ""
        return 1, "", f"unexpected call {argv}"


def run_cli(args, runner=main.subprocess_runner, environ=None):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = main.main(args, runner=runner, environ=environ or {})
    return code, out.getvalue(), err.getvalue()


class ClassifyTests(unittest.TestCase):
    def setUp(self):
        self.refresh = fixture("refresh_only.json")
        self.plan = fixture("plan.json")
        self.rules = main.load_ignore_rules([os.path.join(FIXTURES, "main.tf.json")])
        self.report = main.classify(self.refresh, self.plan, self.rules, "shop-prod")

    def test_full_plan_puts_every_drifted_resource_in_one_category(self):
        categories = {address: finding.category for address, finding in by_address(self.report).items()}
        self.assertEqual(categories, {
            "kubernetes_config_map_v1.flags": main.CHANGED,
            "local_file.motd": main.DELETED,
            "kubernetes_config_map_v1.routing": main.IGNORED,
            "google_compute_instance.worker": main.REPLACE,
            "kubernetes_secret_v1.db": main.CHANGED,
        })
        self.assertEqual(main.summary(self.report), {main.REPLACE: 1, main.DELETED: 1, main.CHANGED: 2, main.IGNORED: 1, "pending": 1})

    def test_changed_outside_carries_the_attribute_diff(self):
        flags = by_address(self.report)["kubernetes_config_map_v1.flags"]
        diffs = {diff.path: (diff.before, diff.after) for diff in flags.diffs}
        self.assertEqual(diffs, {"data.color": ("blue", "red"), "metadata[0].resource_version": ("1201", "1388")})
        self.assertEqual(flags.next_apply, "update")
        self.assertIn("reverting the change", flags.reason)

    def test_deleted_outside_has_no_diff_and_is_recreated(self):
        motd = by_address(self.report)["local_file.motd"]
        self.assertEqual((motd.drift, motd.diffs, motd.next_apply), ("delete", [], "create"))
        self.assertIn("creates it again", motd.reason)

    def test_replace_on_apply_names_the_attribute_that_forces_it(self):
        worker = by_address(self.report)["google_compute_instance.worker"]
        self.assertEqual(worker.replace_paths, ["metadata_startup_script"])
        self.assertEqual([diff.path for diff in worker.diffs], ["metadata_startup_script"])
        self.assertIn("forces replacement", worker.reason)

    def test_ignored_drift_names_the_ignore_changes_rule(self):
        routing = by_address(self.report)["kubernetes_config_map_v1.routing"]
        self.assertEqual(routing.ignored_by, ["metadata[0].labels"])
        self.assertEqual(routing.next_apply, "no-op")
        self.assertIn("covered by ignore_changes", routing.reason)

    def test_unapplied_change_is_pending_not_drift(self):
        self.assertEqual([(item.address, item.action) for item in self.report.pending], [("local_file.banner", "create")])
        self.assertNotIn("local_file.banner", by_address(self.report))

    def test_data_sources_are_never_drift(self):
        self.assertNotIn("data.http.ip", by_address(self.report))
        self.assertNotIn("data.http.ip", [item.address for item in self.report.pending])

    def test_refresh_only_alone_cannot_hide_uncovered_attributes(self):
        report = main.classify(self.refresh, None, self.rules)
        routing = by_address(report)["kubernetes_config_map_v1.routing"]
        self.assertEqual(routing.category, main.CHANGED)
        self.assertEqual(routing.ignored_by, ["metadata[0].labels"])
        self.assertIsNone(routing.next_apply)
        self.assertIn("drop --drift-only", routing.reason)
        self.assertEqual(report.pending, [])

    def test_ignore_rules_alone_classify_when_every_path_is_covered(self):
        report = main.classify(labels_only_drift(), None, self.rules)
        self.assertEqual(report.findings[0].category, main.IGNORED)
        self.assertEqual(main.exit_code(report), 0)
        self.assertEqual(main.exit_code(report, strict=True), 2)

    def test_sensitive_values_never_leave_the_plan(self):
        secret = by_address(self.report)["kubernetes_secret_v1.db"]
        password = next(diff for diff in secret.diffs if diff.path == "data.password")
        self.assertEqual((password.before, password.after, password.sensitive), (None, None, True))
        rendered = main.render_markdown(self.report, 2) + main.render_text(self.report, 2) + json.dumps(main.report_dict(self.report, 2))
        self.assertNotIn("hunter2", rendered)
        self.assertNotIn("rotated-by-hand", rendered)
        self.assertIn("(sensitive)", rendered)


class ExitCodeTests(unittest.TestCase):
    def test_exit_codes_follow_detailed_exitcode(self):
        clean = main.classify({"format_version": "1.2", "resource_drift": []})
        self.assertEqual(main.exit_code(clean), 0)
        drift = main.classify(fixture("refresh_only.json"))
        self.assertEqual(main.exit_code(drift), 2)
        pending_only = main.classify({"resource_drift": []}, {"resource_changes": [{"address": "local_file.banner", "mode": "managed", "change": {"actions": ["create"]}}]})
        self.assertEqual(main.exit_code(pending_only), 0)
        self.assertEqual(main.exit_code(pending_only, fail_on_pending=True), 2)

    def test_clean_report_says_no_drift_and_has_a_stable_fingerprint(self):
        report = main.classify({"format_version": "1.2", "resource_drift": []}, None, None, "shop-prod")
        markdown = main.render_markdown(report, 0)
        self.assertIn("No drift", markdown)
        self.assertEqual(main.fingerprint(report), "clean")
        self.assertIn("no drift", main.render_text(report, 0))


class PathAndRuleTests(unittest.TestCase):
    def test_render_path_quotes_keys_that_are_not_identifiers(self):
        self.assertEqual(main.render_path(("metadata", 0, "labels", "app.kubernetes.io/name")), 'metadata[0].labels["app.kubernetes.io/name"]')
        self.assertEqual(main.render_path(("tags", "Cost Center")), 'tags["Cost Center"]')
        self.assertEqual(main.render_path(("data", "color")), "data.color")

    def test_parse_rule_reads_native_traversals(self):
        self.assertEqual(main.parse_rule("metadata[0].labels"), ("metadata", 0, "labels"))
        self.assertEqual(main.parse_rule("metadata.0.labels"), ("metadata", 0, "labels"))
        self.assertEqual(main.parse_rule('tags["Cost Center"]'), ("tags", "Cost Center"))
        self.assertEqual(main.parse_rule("all"), ())
        self.assertTrue(main.covers((), ("anything", 3)))
        with self.assertRaises(main.DriftError):
            main.parse_rule("tags[oops")

    def test_load_ignore_rules_handles_json_syntax_variants_and_instances(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "main.tf.json")
            with open(path, "w", encoding="utf-8") as handle:
                json.dump({"resource": [{"aws_instance": {"web": [{"count": 2, "lifecycle": [{"ignore_changes": "all"}]}]}}]}, handle)
            rules = main.load_ignore_rules([path])
        self.assertEqual(rules, {"aws_instance.web": [("all", ())]})
        drift = {"resource_drift": [{"address": "aws_instance.web[1]", "mode": "managed", "type": "aws_instance", "change": {"actions": ["update"], "before": {"tags": {"a": "1"}}, "after": {"tags": {"a": "2"}}}}]}
        finding = main.classify(drift, None, rules).findings[0]
        self.assertEqual((finding.category, finding.ignored_by), (main.IGNORED, ["all"]))


class ReportTests(unittest.TestCase):
    def setUp(self):
        rules = main.load_ignore_rules([os.path.join(FIXTURES, "main.tf.json")])
        self.report = main.classify(fixture("refresh_only.json"), fixture("plan.json"), rules, "shop-prod")

    def test_markdown_has_summary_table_details_pending_and_marker(self):
        markdown = main.render_markdown(self.report, 2)
        self.assertTrue(markdown.startswith("## Drift report: shop-prod\n"))
        self.assertIn("| `kubernetes_config_map_v1.flags` | changed outside | update | `data.color`, `metadata[0].resource_version` |", markdown)
        self.assertIn("| `google_compute_instance.worker` | replaced by the next apply | replace |", markdown)
        self.assertIn('| `data.color` | `"blue"` | `"red"` |', markdown)
        self.assertIn("### Unapplied changes (not drift)", markdown)
        self.assertIn(f"<!-- {main.dedupe_key('shop-prod')} fingerprint={main.fingerprint(self.report)} -->", markdown)

    def test_markdown_escapes_pipes_inside_table_cells(self):
        self.assertEqual(main.md_code('"a|b"'), '`"a\\|b"`')
        self.assertEqual(main.md_code("x`y"), "`` x`y ``")

    def test_fingerprint_ignores_values_but_not_paths(self):
        refresh = fixture("refresh_only.json")
        changed_values = copy.deepcopy(refresh)
        changed_values["resource_drift"][0]["change"]["after"]["data"]["color"] = "green"
        self.assertEqual(main.fingerprint(main.classify(refresh)), main.fingerprint(main.classify(changed_values)))
        new_path = copy.deepcopy(refresh)
        new_path["resource_drift"][0]["change"]["after"]["data"]["size"] = "xl"
        self.assertNotEqual(main.fingerprint(main.classify(refresh)), main.fingerprint(main.classify(new_path)))

    def test_dedupe_key_is_one_per_stack(self):
        self.assertEqual(main.dedupe_key("shop-prod"), main.dedupe_key("shop-prod"))
        self.assertNotEqual(main.dedupe_key("shop-prod"), main.dedupe_key("shop-staging"))
        self.assertRegex(main.dedupe_key("shop-prod"), r"^driftwatch-[0-9a-f]{12}$")


class ParsePlanTests(unittest.TestCase):
    def test_rejects_garbage_state_json_and_errored_plans(self):
        with self.assertRaisesRegex(main.DriftError, "not JSON"):
            main.parse_plan("Error: no plan", "x")
        with self.assertRaisesRegex(main.DriftError, "state JSON"):
            main.parse_plan(json.dumps({"format_version": "1.0", "values": {}}), "x")
        with self.assertRaisesRegex(main.DriftError, "errored"):
            main.parse_plan(json.dumps({"format_version": "1.2", "planned_values": {}, "errored": True}), "x")
        with self.assertRaisesRegex(main.DriftError, "format_version 2.0"):
            main.parse_plan(json.dumps({"format_version": "2.0"}), "x")


class CliTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.stack = os.path.join(self.tmp.name, "shop-prod")
        os.makedirs(self.stack)
        with open(os.path.join(self.stack, "main.tf.json"), "w", encoding="utf-8") as handle:
            handle.write(fixture_text("main.tf.json"))

    def tearDown(self):
        self.tmp.cleanup()

    def test_run_drives_a_refresh_only_plan_then_a_normal_plan(self):
        tofu = FakeTofu(fixture_text("refresh_only.json"), fixture_text("plan.json"))
        code, out, _ = run_cli(["run", "--chdir", self.stack, "--tofu", "tofu", "--json"], tofu)
        self.assertEqual(code, 2)
        plan_calls = [call for call in tofu.calls if call[2] == "plan"]
        self.assertEqual(len(tofu.calls), 4)
        self.assertEqual(plan_calls[0][:4], ["tofu", f"-chdir={self.stack}", "plan", "-refresh-only"])
        self.assertIn("-detailed-exitcode", plan_calls[0])
        self.assertNotIn("-refresh-only", plan_calls[1])
        self.assertEqual(tofu.calls[1][2:4], ["show", "-json"])
        data = json.loads(out)
        self.assertEqual((data["stack"], data["tofu_exit"], data["full_plan"]), ("shop-prod", 2, True))
        self.assertEqual(data["findings"][0]["category"], main.REPLACE)

    def test_run_reads_ignore_changes_from_tf_json_in_the_root_module(self):
        tofu = FakeTofu(json.dumps(labels_only_drift()), "")
        code, out, _ = run_cli(["run", "--chdir", self.stack, "--drift-only"], tofu)
        self.assertEqual(len(tofu.calls), 2)
        self.assertEqual(code, 0)
        self.assertIn("covered by ignore_changes (metadata[0].labels)", out)
        self.assertIn("tofu -detailed-exitcode said 2", out)

    def test_tofu_failure_is_exit_1_with_its_stderr(self):
        tofu = FakeTofu("", "", refresh_exit=1, stderr="Error: Error acquiring the state lock\nLock Info:\n  ID: 4f1c")
        code, _, err = run_cli(["run", "--chdir", self.stack], tofu)
        self.assertEqual(code, 1)
        self.assertIn("refresh-only plan failed (exit 1)", err)
        self.assertIn("Error acquiring the state lock", err)

    def test_usage_errors_exit_1_so_ci_never_reads_them_as_drift(self):
        self.assertEqual(run_cli(["report"])[0], 1)
        self.assertEqual(run_cli(["run", "--chdir", os.path.join(self.tmp.name, "missing")])[0], 1)

    def test_report_writes_markdown_and_appends_to_the_step_summary(self):
        markdown = os.path.join(self.tmp.name, "drift.md")
        summary = os.path.join(self.tmp.name, "summary.md")
        with open(summary, "w", encoding="utf-8") as handle:
            handle.write("previous step\n")
        args = ["report", "--refresh-plan", os.path.join(FIXTURES, "refresh_only.json"), "--plan", os.path.join(FIXTURES, "plan.json"),
                "--config", os.path.join(FIXTURES, "main.tf.json"), "--stack", "shop-prod", "--markdown", markdown, "--step-summary"]
        code, out, _ = run_cli(args, environ={"GITHUB_STEP_SUMMARY": summary})
        self.assertEqual(code, 2)
        self.assertIn("-/+ google_compute_instance.worker", out)
        with open(markdown, encoding="utf-8") as handle:
            written = handle.read()
        with open(summary, encoding="utf-8") as handle:
            self.assertEqual(handle.read(), "previous step\n" + written)
        self.assertEqual(run_cli(args, environ={})[0], 1)


if __name__ == "__main__":
    unittest.main()
