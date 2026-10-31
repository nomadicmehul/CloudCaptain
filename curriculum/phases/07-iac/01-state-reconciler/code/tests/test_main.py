import contextlib
import io
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import main  # noqa: E402


class MinitfTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = os.path.join(self.tmp.name, "root")
        os.makedirs(self.root)
        self.config = os.path.join(self.tmp.name, "main.json")
        self.state = os.path.join(self.tmp.name, "state.json")
        self.write_config([
            {"type": "local_dir", "name": "site", "path": "site"},
            {"type": "local_file", "name": "index", "path": "site/index.html", "content": "hello"},
        ])

    def tearDown(self):
        self.tmp.cleanup()

    def write_config(self, resources):
        with open(self.config, "w", encoding="utf-8") as handle:
            json.dump({"resources": resources}, handle)

    def run_cli(self, *args):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = main.main([*args, "--config", self.config, "--state", self.state, "--root", self.root])
        self.assertEqual(code, 0)
        return out.getvalue()

    def plan_actions(self):
        provider = main.LocalProvider(self.root)
        state = main.load_state(self.state)
        return main.plan(main.load_config(self.config), state, main.refresh(state, provider))

    def test_plan_creates_everything_on_empty_state(self):
        actions = self.plan_actions()
        self.assertEqual([a.verb for a in actions], ["create", "create"])
        self.assertEqual(main.summary(actions), {"add": 2, "change": 0, "destroy": 0})

    def test_apply_creates_files_then_plan_is_noop(self):
        self.run_cli("apply")
        with open(os.path.join(self.root, "site", "index.html"), encoding="utf-8") as handle:
            self.assertEqual(handle.read(), "hello")
        self.assertEqual(main.load_state(self.state)["serial"], 1)
        self.assertEqual([a.verb for a in self.plan_actions()], ["noop", "noop"])
        self.assertIn("No changes", self.run_cli("plan"))

    def test_content_change_plans_update_and_apply_rewrites_file(self):
        self.run_cli("apply")
        self.write_config([
            {"type": "local_dir", "name": "site", "path": "site"},
            {"type": "local_file", "name": "index", "path": "site/index.html", "content": "hello v2"},
        ])
        actions = self.plan_actions()
        self.assertEqual([a.verb for a in actions], ["noop", "update"])
        self.assertIn("content changed", actions[1].reason)
        self.run_cli("apply")
        with open(os.path.join(self.root, "site", "index.html"), encoding="utf-8") as handle:
            self.assertEqual(handle.read(), "hello v2")
        self.assertEqual(main.load_state(self.state)["serial"], 2)

    def test_manual_deletion_is_detected_as_drift_and_recreated(self):
        self.run_cli("apply")
        os.remove(os.path.join(self.root, "site", "index.html"))
        actions = self.plan_actions()
        self.assertEqual(actions[1].verb, "create")
        self.assertIn("drift", actions[1].reason)
        self.run_cli("apply")
        self.assertTrue(os.path.isfile(os.path.join(self.root, "site", "index.html")))

    def test_removing_from_config_plans_delete(self):
        self.run_cli("apply")
        self.write_config([{"type": "local_dir", "name": "site", "path": "site"}])
        actions = self.plan_actions()
        self.assertEqual([a.verb for a in actions], ["noop", "delete"])
        self.run_cli("apply")
        self.assertFalse(os.path.exists(os.path.join(self.root, "site", "index.html")))
        self.assertNotIn("local_file.index", main.load_state(self.state)["resources"])

    def test_path_change_forces_replace(self):
        self.run_cli("apply")
        self.write_config([
            {"type": "local_dir", "name": "site", "path": "site"},
            {"type": "local_file", "name": "index", "path": "site/home.html", "content": "hello"},
        ])
        actions = self.plan_actions()
        self.assertEqual(actions[1].verb, "replace")
        self.assertEqual(main.summary(actions), {"add": 1, "change": 0, "destroy": 1})
        self.run_cli("apply")
        self.assertFalse(os.path.exists(os.path.join(self.root, "site", "index.html")))
        self.assertTrue(os.path.isfile(os.path.join(self.root, "site", "home.html")))

    def test_render_plan_has_symbols_diff_and_summary(self):
        text = main.render_plan(self.plan_actions())
        self.assertIn("+ local_dir.site  (not in state)", text)
        self.assertIn('content: null -> "hello"', text)
        self.assertTrue(text.endswith("Plan: 2 to add, 0 to change, 0 to destroy."))

    def test_destroy_removes_everything_and_json_plan_is_machine_readable(self):
        self.run_cli("apply")
        data = json.loads(self.run_cli("plan", "--json"))
        self.assertEqual(data["summary"], {"add": 0, "change": 0, "destroy": 0})
        self.run_cli("destroy")
        self.assertEqual(main.load_state(self.state)["resources"], {})
        self.assertFalse(os.path.exists(os.path.join(self.root, "site")))

    def test_duplicate_addresses_are_rejected(self):
        self.write_config([
            {"type": "local_file", "name": "a", "path": "a.txt"},
            {"type": "local_file", "name": "a", "path": "b.txt"},
        ])
        with self.assertRaises(ValueError):
            main.load_config(self.config)


if __name__ == "__main__":
    unittest.main()
