"""Tests for the shared grader harness (grade.py). Add checks.py tests next to it."""
import io
import json
import os
import sys
import tempfile
import types
import unittest
from contextlib import redirect_stdout

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import grade  # noqa: E402

LESSON = {
    "id": "capstones/example",
    "pass_score": 3,
    "milestones": [
        {"id": "first-step", "title": "First step", "points": 2},
        {"id": "second-step", "title": "Second step", "points": 2},
        {"id": "third-step", "title": "Third step", "points": 1},
    ],
}


def fake_checks(**results):
    module = types.ModuleType("fake_checks")
    for name, value in results.items():
        if isinstance(value, Exception):
            def fn(project, exc=value):
                raise exc
        else:
            def fn(project, value=value):
                return value
        setattr(module, name, fn)
    return module


class GradeHarnessTests(unittest.TestCase):
    def setUp(self):
        self.project = tempfile.mkdtemp()

    def test_score_at_pass_mark_passes(self):
        checks = fake_checks(check_first_step=(True, "ok"), check_second_step=(False, "no"), check_third_step=(True, "ok"))
        report = grade.grade(self.project, LESSON, checks)
        self.assertEqual((report["score"], report["total"], report["passed"]), (3, 5, True))

    def test_score_below_pass_mark_fails(self):
        checks = fake_checks(check_first_step=(True, "ok"), check_second_step=(False, "no"), check_third_step=(False, "no"))
        report = grade.grade(self.project, LESSON, checks)
        self.assertEqual((report["score"], report["passed"]), (2, False))

    def test_missing_check_fails_its_milestone_with_a_hint(self):
        report = grade.grade(self.project, LESSON, fake_checks(check_first_step=(True, "ok")))
        self.assertIn("check_second_step", report["milestones"][1]["detail"])
        self.assertFalse(report["milestones"][1]["passed"])

    def test_crashing_check_fails_only_its_milestone(self):
        checks = fake_checks(check_first_step=RuntimeError("boom"), check_second_step=(True, "ok"), check_third_step=(True, "ok"))
        report = grade.grade(self.project, LESSON, checks)
        self.assertIn("RuntimeError: boom", report["milestones"][0]["detail"])
        self.assertEqual((report["score"], report["passed"]), (3, True))

    def test_selected_milestones_pass_only_when_all_selected_pass(self):
        checks = fake_checks(check_first_step=(True, "ok"), check_second_step=(False, "no"), check_third_step=(True, "ok"))
        self.assertTrue(grade.grade(self.project, LESSON, checks, ["first-step"])["passed"])
        self.assertFalse(grade.grade(self.project, LESSON, checks, ["first-step", "second-step"])["passed"])

    def test_unknown_milestone_is_a_usage_error(self):
        with self.assertRaises(ValueError):
            grade.grade(self.project, LESSON, fake_checks(), ["nope"])

    def test_check_names_replace_hyphens(self):
        self.assertEqual(grade.check_name("deploy-on-push"), "check_deploy_on_push")

    def test_markdown_summary_has_one_row_per_milestone(self):
        checks = fake_checks(check_first_step=(True, "ok"), check_second_step=(True, "ok"), check_third_step=(False, "no"))
        md = grade.render_markdown(grade.grade(self.project, LESSON, checks))
        self.assertEqual(md.count("\n| ✅") + md.count("\n| ❌"), 3)

    def test_every_milestone_in_lesson_json_has_a_check(self):
        lesson = grade.load_lesson()
        checks = grade.load_checks()
        missing = [m["id"] for m in lesson["milestones"] if not hasattr(checks, grade.check_name(m["id"]))]
        self.assertEqual(missing, [])

    def test_main_exit_codes_and_json(self):
        out = io.StringIO()
        with redirect_stdout(out):
            code = grade.main([self.project, "--json"])
        self.assertIn(code, (0, 1))
        self.assertEqual(json.loads(out.getvalue())["capstone"], grade.load_lesson()["id"])
        self.assertEqual(grade.main([os.path.join(self.project, "missing")]), 2)


if __name__ == "__main__":
    unittest.main()
