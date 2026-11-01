import contextlib
import io
import json
import os
import re
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
CODE = os.path.dirname(HERE)
sys.path.insert(0, CODE)

import main  # noqa: E402

FIXTURES = os.path.join(CODE, "fixtures")
RULES = os.path.join(CODE, "rules.json")
REGO = os.path.join(CODE, "policy", "plan.rego")

EXPECTED_DENY = {
    ("deny", "require-owner-tag", "terraform_data.cache"),
    ("deny", "require-env-tag", "terraform_data.api"),
    ("deny", "forbid-huge-sizes", "terraform_data.cache"),
    ("deny", "disk-limit", "terraform_data.cache"),
    ("deny", "no-world-writable-files", "local_file.scratch"),
    ("deny", "protect-audit-log", "terraform_data.audit_log"),
    ("deny", "no-replace-stateful", "terraform_data.db"),
    ("deny", "module-source-allow-list", "module.legacy"),
}
EXPECTED_WARN = {
    ("warn", "require-cost-center", "terraform_data.cache"),
    ("warn", "name-convention", "terraform_data.api"),
}


def load(name):
    with open(os.path.join(FIXTURES, name), encoding="utf-8") as handle:
        return json.load(handle)


def shipped_rules():
    with open(RULES, encoding="utf-8") as handle:
        return main.load_rules(json.load(handle))


def change(address, rtype, actions, after=None, after_unknown=None, mode="managed", reason=""):
    item = {"address": address, "mode": mode, "type": rtype, "name": address.split(".")[-1], "change": {"actions": actions, "after": after, "after_unknown": after_unknown if after_unknown is not None else {}}}
    if reason:
        item["action_reason"] = reason
    return item


def plan_of(*changes, module_calls=None):
    data = {"format_version": "1.2", "planned_values": {}, "resource_changes": list(changes)}
    if module_calls is not None:
        data["configuration"] = {"root_module": {"module_calls": module_calls}}
    return main.load_plan(data)


def rule(**fields):
    base = {"id": "r", "message": "m"}
    base.update(fields)
    return main.parse_rule(base, 0)


def keys(violations):
    return {v.key() for v in violations}


class PlanLoadingTests(unittest.TestCase):
    def test_load_plan_normalises_changes_and_walks_module_calls(self):
        plan = main.load_plan(load("plan.json"))
        self.assertEqual(len(plan.changes), 10)
        self.assertEqual(plan.tool_version, "1.13.2")
        db = next(c for c in plan.changes if c.address == "terraform_data.db")
        self.assertEqual(db.actions, ["delete", "create"])
        self.assertEqual(db.action, "replace")
        self.assertEqual(db.action_reason, "replace_because_cannot_update")
        self.assertEqual([(m.address, m.source) for m in plan.module_calls], [("module.legacy", "./vendor/legacy"), ("module.web", "./modules/web")])

    def test_nested_module_calls_get_full_addresses(self):
        configuration = {"root_module": {"module_calls": {"net": {"source": "./modules/net", "module": {"module_calls": {"subnets": {"source": "git::https://example.com/x.git", "version_constraint": ""}}}}}}}
        calls = main.module_calls(configuration)
        self.assertEqual([c.address for c in calls], ["module.net", "module.net.module.subnets"])

    def test_state_json_and_errored_plans_are_refused_with_the_fix(self):
        with self.assertRaisesRegex(main.PlanError, "this is state, not a plan"):
            main.load_plan({"format_version": "1.0", "values": {"root_module": {}}})
        with self.assertRaisesRegex(main.PlanError, "errored"):
            main.load_plan({"format_version": "1.2", "planned_values": {}, "errored": True})
        with self.assertRaisesRegex(main.PlanError, "not a plan JSON"):
            main.load_plan({"hello": "world"})

    def test_action_labels_cover_every_plan_action(self):
        self.assertEqual(main.action_label(["create"]), "create")
        self.assertEqual(main.action_label(["update"]), "update")
        self.assertEqual(main.action_label(["delete"]), "delete")
        self.assertEqual(main.action_label(["delete", "create"]), "replace")
        self.assertEqual(main.action_label(["create", "delete"]), "replace (create before destroy)")
        self.assertEqual(main.action_label(["no-op"]), "no-op")
        self.assertEqual(main.action_label(["read"]), "read")


class MatchingTests(unittest.TestCase):
    def test_globs_match_whole_strings_and_treat_brackets_literally(self):
        self.assertTrue(main.glob_match(["*.db"], "terraform_data.db"))
        self.assertTrue(main.glob_match(["*.db"], "module.x.terraform_data.db"))
        self.assertFalse(main.glob_match(["*.db"], "terraform_data.db_backup"))
        self.assertTrue(main.glob_match(["module.web[\"a\"].*"], "module.web[\"a\"].local_file.page"))
        self.assertFalse(main.glob_match(["module.web[\"a\"].*"], "module.webb.local_file.page"))
        self.assertTrue(main.glob_match(["local_?ile"], "local_file"))

    def test_lookup_walks_maps_and_lists_and_reports_missing(self):
        doc = {"input": {"tags": {"owner": "x"}, "rules": [{"port": 22}]}}
        self.assertEqual(main.lookup(doc, ["input", "tags", "owner"]), "x")
        self.assertEqual(main.lookup(doc, ["input", "rules", "0", "port"]), 22)
        self.assertIs(main.lookup(doc, ["input", "rules", 5]), main.MISSING)
        self.assertIs(main.lookup(doc, ["input", "tags", "owner", "deeper"]), main.MISSING)
        self.assertIs(main.lookup(None, ["a"]), main.MISSING)

    def test_unknown_is_true_when_any_prefix_is_marked_known_after_apply(self):
        after_unknown = {"id": True, "input": {"name": True, "tags": {}}, "ports": [False, True]}
        self.assertTrue(main.is_unknown(after_unknown, ["input", "name"]))
        self.assertTrue(main.is_unknown(after_unknown, ["id", "anything"]))
        self.assertTrue(main.is_unknown(after_unknown, ["ports", 1]))
        self.assertFalse(main.is_unknown(after_unknown, ["ports", 0]))
        self.assertFalse(main.is_unknown(after_unknown, ["input", "tags", "owner"]))
        self.assertFalse(main.is_unknown(False, ["input"]))

    def test_operators_keep_json_types_apart(self):
        self.assertTrue(main.holds("equals", "a", "a"))
        self.assertFalse(main.holds("equals", True, 1))
        self.assertFalse(main.holds("in", 1, [True]))
        self.assertTrue(main.holds("not_in", "small", ["xlarge"]))
        self.assertTrue(main.holds("matches", "web-1", "^[a-z]"))
        self.assertFalse(main.holds("matches", 7, "7"))
        self.assertTrue(main.holds("not_matches", 7, "7"))
        self.assertTrue(main.holds("lte", 500, 500))
        self.assertFalse(main.holds("lt", "100", 500))
        self.assertFalse(main.holds("gt", True, 0))


class RuleTests(unittest.TestCase):
    def test_exists_flags_missing_values_only_on_writes(self):
        r = rule(type="terraform_data", path="input.tags.owner", op="exists")
        plan = plan_of(
            change("terraform_data.a", "terraform_data", ["create"], {"input": {"tags": {}}}),
            change("terraform_data.b", "terraform_data", ["update"], {"input": {"tags": {"owner": None}}}),
            change("terraform_data.c", "terraform_data", ["delete"], None, False),
            change("terraform_data.d", "terraform_data", ["no-op"], {"input": {}}),
            change("data.terraform_data.e", "terraform_data", ["read"], {"input": {}}, mode="data"),
            change("terraform_data.f", "terraform_data", ["create"], {"input": {"tags": {"owner": "x"}}}),
        )
        found = main.evaluate([r], plan)
        self.assertEqual([v.address for v in found], ["terraform_data.a", "terraform_data.b"])
        self.assertIn("input.tags.owner is missing", found[0].detail)

    def test_value_operators_say_nothing_about_a_missing_attribute(self):
        r = rule(path="input.size", op="not_in", value=["xlarge"])
        plan = plan_of(
            change("x.missing", "x", ["create"], {"input": {}}),
            change("x.big", "x", ["create"], {"input": {"size": "xlarge"}}),
            change("x.ok", "x", ["create"], {"input": {"size": "small"}}),
        )
        found = main.evaluate([r], plan)
        self.assertEqual([v.address for v in found], ["x.big"])
        self.assertIn('input.size is "xlarge", want not_in ["xlarge"]', found[0].detail)

    def test_known_after_apply_fails_closed_unless_the_rule_allows_it(self):
        unknown_name = change("x.a", "x", ["create"], {"input": {}}, {"input": {"name": True}})
        strict = rule(path="input.name", op="matches", value="^[a-z]")
        lenient = rule(id="r2", path="input.name", op="matches", value="^[a-z]", unknown="allow")
        found = main.evaluate([strict, lenient], plan_of(unknown_name))
        self.assertEqual([v.rule for v in found], ["r"])
        self.assertIn("known after apply", found[0].detail)

    def test_actions_filter_lets_a_rule_grandfather_existing_resources(self):
        r = rule(path="tags.cost_center", op="exists", actions=["create"])
        plan = plan_of(
            change("x.old", "x", ["update"], {"tags": {}}),
            change("x.new", "x", ["create"], {"tags": {}}),
            change("x.replaced", "x", ["delete", "create"], {"tags": {}}),
        )
        self.assertEqual(sorted(v.address for v in main.evaluate([r], plan)), ["x.new", "x.replaced"])

    def test_no_delete_catches_deletes_and_replacements_of_protected_addresses(self):
        r = rule(check="no_delete", address=["terraform_data.audit_log"])
        plan = plan_of(
            change("terraform_data.audit_log", "terraform_data", ["delete"], None, False, reason="delete_because_no_resource_config"),
            change("terraform_data.other", "terraform_data", ["delete"], None, False),
        )
        found = main.evaluate([r], plan)
        self.assertEqual([(v.address, v.action) for v in found], [("terraform_data.audit_log", "delete")])
        self.assertIn("delete_because_no_resource_config", found[0].detail)
        replaced = plan_of(change("terraform_data.audit_log", "terraform_data", ["create", "delete"], {}))
        self.assertEqual([v.action for v in main.evaluate([r], replaced)], ["replace (create before destroy)"])

    def test_no_replace_ignores_creates_updates_and_plain_deletes(self):
        r = rule(check="no_replace", address=["*.db"])
        plan = plan_of(
            change("aws_db_instance.db", "aws_db_instance", ["update"], {}),
            change("terraform_data.db", "terraform_data", ["delete"], None, False),
            change("module.data.terraform_data.db", "terraform_data", ["delete", "create"], {}),
            change("terraform_data.cache", "terraform_data", ["delete", "create"], {}),
        )
        self.assertEqual([v.address for v in main.evaluate([r], plan)], ["module.data.terraform_data.db"])

    def test_module_source_allow_list_checks_every_call(self):
        r = rule(check="module_source", allow=["^\\./modules/", "^registry\\.opentofu\\.org/acme/"])
        calls = {
            "web": {"source": "./modules/web"},
            "vpc": {"source": "registry.opentofu.org/acme/vpc/aws", "version_constraint": "~> 5.0"},
            "fork": {"source": "git::https://github.com/someone/fork.git", "module": {"module_calls": {"inner": {"source": "./modules/x"}}}},
        }
        found = main.evaluate([r], plan_of(module_calls=calls))
        self.assertEqual([(v.address, v.action) for v in found], [("module.fork", "config")])

    def test_rule_validation_names_the_rule_and_the_problem(self):
        cases = [
            ({"id": "a", "message": "m", "path": "x", "op": "less_than", "value": 1}, 'rule "a": op "less_than"'),
            ({"id": "b", "message": "m", "path": "x", "op": "lte"}, 'rule "b": op "lte" needs a "value"'),
            ({"id": "c", "message": "m", "path": "x", "op": "lte", "value": "500"}, "needs a number"),
            ({"id": "d", "message": "m", "path": "x", "op": "matches", "value": "("}, "invalid regular expression"),
            ({"id": "e", "message": "m", "path": "x", "op": "exists", "opp": 1}, "unknown field"),
            ({"id": "f", "message": "m", "path": "x", "op": "in", "value": "dev"}, "needs a list"),
            ({"id": "g", "path": "x", "op": "exists"}, "needs a message"),
            ({"id": "h", "message": "m", "level": "error", "path": "x", "op": "exists"}, 'level "error"'),
            ({"id": "i", "message": "m", "path": "x", "op": "exists", "actions": ["delete"]}, "not checkable"),
            ({"id": "j", "message": "m", "check": "module_source"}, "allow must be"),
            ({"id": "k", "message": "m", "path": "a..b", "op": "exists"}, "empty or invalid segment"),
        ]
        for item, expected in cases:
            with self.subTest(item=item["id"]):
                with self.assertRaisesRegex(main.PolicyError, re.escape(expected)):
                    main.parse_rule(item, 0)
        with self.assertRaisesRegex(main.PolicyError, "duplicate rule id"):
            main.load_rules({"rules": [{"id": "z", "message": "m", "check": "no_delete"}, {"id": "z", "message": "m", "check": "no_delete"}]})


class ShippedPolicyTests(unittest.TestCase):
    def test_fixture_plan_produces_exactly_the_expected_violations(self):
        found = main.evaluate(shipped_rules(), main.load_plan(load("plan.json")))
        self.assertEqual(keys(found), EXPECTED_DENY | EXPECTED_WARN)
        self.assertEqual(main.exit_code(found), 1)
        self.assertEqual([v.level for v in found][:8], ["deny"] * 8)

    def test_clean_fixture_passes_the_shipped_rules(self):
        found = main.evaluate(shipped_rules(), main.load_plan(load("plan-clean.json")))
        self.assertEqual(found, [])

    def test_rego_policy_has_one_block_per_json_rule_at_the_same_level(self):
        with open(REGO, encoding="utf-8") as handle:
            rego = handle.read()
        blocks = re.findall(r"^(deny|warn) contains result if \{(.*?)^\}", rego, re.MULTILINE | re.DOTALL)
        in_rego = set()
        for level, body in blocks:
            named = re.search(r'finding\("([a-z0-9-]+)"', body) or re.search(r'"rule": "([a-z0-9-]+)"', body)
            self.assertIsNotNone(named, body)
            in_rego.add((level, named.group(1)))
        self.assertEqual(in_rego, {(r.level, r.id) for r in shipped_rules()})
        for r in shipped_rules():
            self.assertIn(r.message, rego, r.id)


class CliTests(unittest.TestCase):
    def run_cli(self, *args, runner=None):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = main.main(list(args), runner) if runner else main.main(list(args))
        return code, out.getvalue(), err.getvalue()

    def test_text_json_and_markdown_reports_and_exit_codes(self):
        plan = os.path.join(FIXTURES, "plan.json")
        code, out, _ = self.run_cli(plan, "--rules", RULES)
        self.assertEqual(code, 1)
        self.assertIn("-/+ terraform_data.db (replace)", out)
        self.assertIn("8 denied, 2 warning(s): apply is blocked.", out)
        code, out, _ = self.run_cli(plan, "--rules", RULES, "--json")
        report = json.loads(out)
        self.assertEqual(code, 1)
        self.assertFalse(report["passed"])
        self.assertEqual(report["summary"]["deny"], 8)
        self.assertEqual({(v["level"], v["rule"], v["address"]) for v in report["violations"]}, EXPECTED_DENY | EXPECTED_WARN)
        code, out, _ = self.run_cli(plan, "--rules", RULES, "--markdown")
        self.assertIn("| deny | `no-replace-stateful` | `terraform_data.db` | replace |", out)
        code, out, _ = self.run_cli(os.path.join(FIXTURES, "plan-clean.json"), "--rules", RULES)
        self.assertEqual(code, 0)
        self.assertIn("policy passed", out)

    def test_warnings_pass_unless_fail_on_warn(self):
        with tempfile.TemporaryDirectory() as tmp:
            rules = os.path.join(tmp, "rules.json")
            with open(rules, "w", encoding="utf-8") as handle:
                json.dump({"rules": [{"id": "w", "level": "warn", "path": "tags.owner", "op": "exists", "message": "tag it"}]}, handle)
            plan = os.path.join(FIXTURES, "plan.json")
            self.assertEqual(self.run_cli(plan, "--rules", rules)[0], 0)
            self.assertEqual(self.run_cli(plan, "--rules", rules, "--fail-on-warn")[0], 1)

    def test_bad_inputs_exit_2_with_a_message(self):
        with tempfile.TemporaryDirectory() as tmp:
            broken = os.path.join(tmp, "rules.json")
            with open(broken, "w", encoding="utf-8") as handle:
                handle.write("{not json")
            code, _, err = self.run_cli(os.path.join(FIXTURES, "plan.json"), "--rules", broken)
            self.assertEqual(code, 2)
            self.assertIn("not valid JSON", err)
            code, _, err = self.run_cli(os.path.join(tmp, "missing.json"), "--rules", RULES)
            self.assertEqual(code, 2)
            self.assertIn("cannot read plan", err)
            code, _, err = self.run_cli("--rules", RULES)
            self.assertEqual(code, 2)
            self.assertIn("exactly one of", err)

    def test_show_runs_tofu_show_json_through_the_runner(self):
        calls = []
        with open(os.path.join(FIXTURES, "plan-clean.json"), encoding="utf-8") as handle:
            text = handle.read()

        def runner(argv):
            calls.append(list(argv))
            return 0, text, ""

        code, out, _ = self.run_cli("--show", "plan.out", "--tofu", "terraform", "--rules", RULES, runner=runner)
        self.assertEqual(code, 0)
        self.assertEqual(calls, [["terraform", "show", "-json", "plan.out"]])
        self.assertIn("No violations.", out)
        code, _, err = self.run_cli("--show", "plan.out", "--rules", RULES, runner=lambda argv: (1, "", "Error: Failed to read the given file as a state or plan file"))
        self.assertEqual(code, 2)
        self.assertIn("Failed to read", err)


if __name__ == "__main__":
    unittest.main()
