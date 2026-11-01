import base64
import hashlib
import json
import os
import shutil
import sys
import tempfile
import textwrap
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import main  # noqa: E402

FIXTURES = os.path.join(os.path.dirname(__file__), "..", "fixtures")
SAMPLE = os.path.join(FIXTURES, "sample-repo")
SAMPLE_WORKFLOW = os.path.join(SAMPLE, ".github", "workflows", "ci.yml")


def yaml(text):
    return main.parse_yaml(textwrap.dedent(text))


def workflow(text):
    return main.build_workflow(yaml(text))


def scope(**contexts):
    status = {"success": lambda: True, "failure": lambda: False, "cancelled": lambda: False, "always": lambda: True}
    return main.Scope({key.lower(): value for key, value in contexts.items()}, status)


def ev(source, **contexts):
    return main.evaluate(source, scope(**contexts))


def cli(argv):
    out, err = StringIO(), StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = main.main(argv)
    return code, out.getvalue(), err.getvalue()


class Sandbox(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.repo = os.path.join(self.tmp, "repo")
        os.makedirs(self.repo)
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def write(self, relative, text):
        path = os.path.join(self.repo, relative)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as handle:
            handle.write(text)
        return path

    def run_text(self, text, **options):
        wf = workflow(text)
        options.setdefault("runs_dir", os.path.join(self.tmp, "runs"))
        runner = main.Runner(wf, main.RunOptions(repo=self.repo, **options))
        summary = runner.run()
        with open(summary["log"]) as handle:
            log = handle.read()
        return summary, log

    def legs(self, summary, job):
        return [leg for leg in summary["jobs"] if leg["job"] == job]


class YamlTests(unittest.TestCase):
    def test_block_scalars_keep_hash_signs_and_follow_chomping(self):
        data = yaml(
            """
            steps:
              - run: |
                  echo "# not a comment"
                  echo two

              - run: |-
                  no trailing newline
              - run: >
                  folded into
                  one line

                  then a paragraph
              - run: |+
                  keep

            last: 1
            """
        )
        runs = [step["run"] for step in data["steps"]]
        self.assertEqual(runs[0], 'echo "# not a comment"\necho two\n')
        self.assertEqual(runs[1], "no trailing newline")
        self.assertEqual(runs[2], "folded into one line\nthen a paragraph\n")
        self.assertEqual(runs[3], "keep\n\n")
        self.assertEqual(data["last"], 1)

    def test_on_stays_a_string_key_and_unquoted_versions_become_floats(self):
        data = yaml(
            """
            on: push
            matrix:
              python: [3.10, "3.10", 3.9]
              flags: [yes, no, off, true, ~]
              mode: 0o755
            """
        )
        self.assertIn("on", data)
        self.assertEqual(data["on"], "push")
        self.assertEqual(data["matrix"]["python"], [3.1, "3.10", 3.9])
        self.assertEqual(main.to_text(data["matrix"]["python"][0]), "3.1")
        self.assertEqual(data["matrix"]["flags"], ["yes", "no", "off", True, None])
        self.assertEqual(data["matrix"]["mode"], 0o755)

    def test_flow_collections_compact_sequences_and_quotes(self):
        data = yaml(
            """
            branches: [main, "release/**", 'it''s']
            env: {A: 1, B: "x # y"}
            paths:
            - "src/**"
            - '!**.md'
            run: echo it's here # trailing comment
            """
        )
        self.assertEqual(data["branches"], ["main", "release/**", "it's"])
        self.assertEqual(data["env"], {"A": 1, "B": "x # y"})
        self.assertEqual(data["paths"], ["src/**", "!**.md"])
        self.assertEqual(data["run"], "echo it's here")

    def test_errors_name_the_line_and_the_fix(self):
        cases = [
            ("a:\n\tb: 1\n", "tabs"),
            ("if: !cancelled()\n", "YAML tag"),
            ("x: &anchor 1\n", "anchors"),
            ("a: 1\na: 2\n", "duplicate key"),
            ("a: one\n  two\n", "needs | or >"),
            ("a: [1, 2\n", "missing ]"),
        ]
        for text, fragment in cases:
            with self.assertRaises(main.YamlError) as caught:
                main.parse_yaml(text)
            self.assertIn(fragment, str(caught.exception))
            self.assertRegex(str(caught.exception), r"^line \d+:")


class ExpressionTests(unittest.TestCase):
    def test_precedence_and_short_circuit_returns_operands(self):
        self.assertEqual(ev("github.head_ref || 'main'", github={"head_ref": ""}), "main")
        self.assertEqual(ev("inputs.tag && format('v{0}', inputs.tag)", inputs={"tag": "1.2"}), "v1.2")
        self.assertIs(ev("!true || true"), True)
        self.assertIs(ev("1 < 2 == true"), True)
        self.assertIs(ev("!(1 == 1) && x", x=True), False)
        self.assertIs(ev("github.event.pull_request.draft == false", github={"event": {}}), True)

    def test_loose_equality_coerces_to_numbers_and_ignores_case(self):
        self.assertIs(ev("'1' == 1"), True)
        self.assertIs(ev("'' == 0"), True)
        self.assertIs(ev("null == 0"), True)
        self.assertIs(ev("true == 1"), True)
        self.assertIs(ev("'Refs/Heads/MAIN' == 'refs/heads/main'"), True)
        self.assertIs(ev("'abc' == 0"), False)
        self.assertIs(ev("'abc' != 'abc'"), False)
        self.assertIs(ev("fromJSON('[1]') == fromJSON('[1]')"), False)
        self.assertIs(ev("'b' > 'A'"), True)
        self.assertIs(ev("0x10 == 16"), True)

    def test_functions_behave_like_the_runner(self):
        self.assertIs(ev("contains(fromJSON('[\"bug\",\"ui\"]'), 'BUG')"), True)
        self.assertIs(ev("contains('Hello world', 'WORLD')"), True)
        self.assertIs(ev("startsWith(github.ref, 'refs/tags/')", github={"ref": "refs/tags/v1"}), True)
        self.assertIs(ev("endsWith('a.YML', '.yml')"), True)
        self.assertEqual(ev("format('{0}-{{literal}}-{1}', 'a', 2)"), "a-{literal}-2")
        self.assertEqual(ev("join(fromJSON('[1, true, \"x\"]'), ', ')"), "1, true, x")
        self.assertEqual(json.loads(ev("toJSON(matrix)", matrix={"os": "linux"})), {"os": "linux"})
        self.assertEqual(ev("fromJSON('{\"include\":[{\"a\":1}]}').include[0].a"), 1)
        with self.assertRaises(main.ExpressionError):
            ev("format('{1}', 'only one')")

    def test_object_filters_and_case_insensitive_contexts(self):
        needs = {"build": {"result": "success"}, "lint": {"result": "failure"}}
        self.assertEqual(ev("needs.*.result", needs=needs), ["success", "failure"])
        self.assertIs(ev("contains(needs.*.result, 'failure')", needs=needs), True)
        self.assertEqual(ev("GitHub.Event.Labels.*.Name", github={"event": {"labels": [{"name": "a"}, {"name": "b"}]}}), ["a", "b"])
        self.assertEqual(ev("steps.meta.outputs['cache-hit']", steps={"meta": {"outputs": {"cache-hit": "true"}}}), "true")
        self.assertIsNone(ev("github.event.missing.deeper", github={"event": {}}))

    def test_bad_syntax_and_unknown_names_are_errors(self):
        for source, fragment in [
            ("secret.TOKEN", "Unrecognized named-value"),
            ('"double"', "single quotes"),
            ("lower('x')", "Unrecognized function"),
            ("(1 == 1", "expected ')'"),
            ("1 ==", "unexpected the end"),
        ]:
            with self.assertRaises(main.ExpressionError) as caught:
                ev(source)
            self.assertIn(fragment, str(caught.exception))

    def test_conditions_get_an_implicit_success_and_mixed_braces_are_always_true(self):
        failing = main.Scope({"github": {"ref": "refs/heads/dev"}}, {"success": lambda: False, "failure": lambda: True, "always": lambda: True, "cancelled": lambda: False})
        node, warning = main.condition_ast("github.ref == 'refs/heads/dev'")
        self.assertIsNone(warning)
        self.assertIs(main.truthy(main.evaluate_node(node, failing)), False)
        node, _ = main.condition_ast("${{ always() && github.ref == 'refs/heads/dev' }}")
        self.assertIs(main.truthy(main.evaluate_node(node, failing)), True)
        node, warning = main.condition_ast("${{ github.ref }} == 'refs/heads/main'")
        self.assertIn("always true", warning)
        self.assertIs(main.truthy(main.evaluate_node(node, scope(github={"ref": "refs/heads/dev"}))), True)

    def test_interpolation_keeps_types_for_whole_values_and_stringifies_inside_text(self):
        s = scope(matrix={"experimental": True, "python": 3.1}, env={})
        self.assertIs(main.interpolate("${{ matrix.experimental }}", s), True)
        self.assertEqual(main.interpolate("py${{ matrix.python }}-${{ matrix.missing }}!", s), "py3.1-!")
        self.assertEqual(main.interpolate({"k": ["${{ 1 == 1 }}"]}, s), {"k": [True]})
        with self.assertRaises(main.ExpressionError):
            main.interpolate("${{ matrix.python", s)


class TriggerTests(unittest.TestCase):
    def test_filter_patterns_follow_the_cheat_sheet(self):
        self.assertTrue(main.filter_matches("feature/login", ["feature/*"]))
        self.assertFalse(main.filter_matches("feature/a/b", ["feature/*"]))
        self.assertTrue(main.filter_matches("feature/a/b", ["feature/**"]))
        self.assertTrue(main.filter_matches("docs/guide/intro.md", ["**.md"]))
        self.assertTrue(main.filter_matches("v1.10", ["v1.1?0"]))
        self.assertTrue(main.filter_matches("v1.0", ["v1.1?0"]))
        self.assertTrue(main.filter_matches("v1110", ["v1+0"]))
        self.assertFalse(main.filter_matches("v00", ["v1+0"]))
        self.assertTrue(main.filter_matches("release-7", ["release-[0-9]"]))
        self.assertFalse(main.filter_matches("main", ["!main"]))

    def test_branch_tag_and_path_filters(self):
        wf = main.load_workflow(SAMPLE_WORKFLOW)
        self.assertTrue(main.evaluate_trigger(wf, "push", "refs/heads/main", ["src/app.py"]).triggered)
        self.assertTrue(main.evaluate_trigger(wf, "push", "refs/heads/release/2.0", ["requirements.txt"]).triggered)
        self.assertFalse(main.evaluate_trigger(wf, "push", "refs/heads/feature/x", ["src/app.py"]).triggered)
        docs_only = main.evaluate_trigger(wf, "push", "refs/heads/main", ["README.md", "src/notes.md"])
        self.assertFalse(docs_only.triggered)
        self.assertIn("paths matched none", docs_only.reason)
        tag = main.evaluate_trigger(wf, "push", "refs/tags/v1.4.0", ["src/app.py"])
        self.assertFalse(tag.triggered)
        self.assertIn("only branches filters", tag.reason)
        self.assertFalse(main.evaluate_trigger(wf, "pull_request", "refs/pull/7/merge", ["docs/a.md"], "main").triggered)
        self.assertTrue(main.evaluate_trigger(wf, "pull_request", "refs/pull/7/merge", ["docs/a.md", "src/app.py"], "main").triggered)
        self.assertFalse(main.evaluate_trigger(wf, "schedule", "refs/heads/main", None).triggered)
        self.assertIn("not evaluated", main.evaluate_trigger(wf, "push", "refs/heads/main", None).reason)

    def test_later_patterns_override_earlier_ones(self):
        wf = workflow(
            """
            on:
              push:
                paths: ["docs/**", "!docs/drafts/**", "docs/drafts/publish-me.md"]
            jobs:
              a: {runs-on: x, steps: [{run: "true"}]}
            """
        )
        self.assertFalse(main.evaluate_trigger(wf, "push", "refs/heads/main", ["docs/drafts/wip.md"]).triggered)
        self.assertTrue(main.evaluate_trigger(wf, "push", "refs/heads/main", ["docs/drafts/publish-me.md"]).triggered)
        with self.assertRaises(main.WorkflowError):
            workflow("on: {push: {branches: [a], branches-ignore: [b]}}\njobs: {a: {runs-on: x, steps: [{run: 'true'}]}}\n")


class MatrixTests(unittest.TestCase):
    def test_include_rules_from_the_documentation(self):
        combos = main.expand_matrix(
            yaml(
                """
                fruit: [apple, pear]
                animal: [cat, dog]
                include:
                  - color: green
                  - color: pink
                    animal: cat
                  - fruit: apple
                    shape: circle
                  - fruit: banana
                  - fruit: banana
                    animal: cat
                """
            )
        )
        self.assertEqual(
            combos,
            [
                {"fruit": "apple", "animal": "cat", "color": "pink", "shape": "circle"},
                {"fruit": "apple", "animal": "dog", "color": "green", "shape": "circle"},
                {"fruit": "pear", "animal": "cat", "color": "pink"},
                {"fruit": "pear", "animal": "dog", "color": "green"},
                {"fruit": "banana"},
                {"fruit": "banana", "animal": "cat"},
            ],
        )

    def test_exclude_matches_partially_and_include_only_matrices_work(self):
        combos = main.expand_matrix({"os": ["linux", "macos"], "py": ["3.11", "3.12"], "exclude": [{"os": "macos"}]})
        self.assertEqual(combos, [{"os": "linux", "py": "3.11"}, {"os": "linux", "py": "3.12"}])
        self.assertEqual(main.expand_matrix({"include": [{"a": 1}, {"a": 2}]}), [{"a": 1}, {"a": 2}])
        self.assertEqual(main.expand_matrix({"v": [1], "include": [{"v": True, "x": 1}]}), [{"v": 1}, {"v": True, "x": 1}])
        self.assertEqual(main.expand_matrix(None), [{}])

    def test_limits_and_empty_matrices_are_errors(self):
        with self.assertRaises(main.WorkflowError):
            main.expand_matrix({"a": list(range(17)), "b": list(range(16))})
        with self.assertRaises(main.WorkflowError):
            main.expand_matrix({"a": [1], "exclude": [{"a": 1}]})
        with self.assertRaises(main.WorkflowError):
            main.expand_matrix({"a": []})


class GraphTests(unittest.TestCase):
    def test_unknown_needs_cycles_and_typos_are_rejected_with_names(self):
        with self.assertRaises(main.WorkflowError) as caught:
            main.load_workflow(os.path.join(FIXTURES, "broken", "needs-typo.yml"))
        self.assertIn("job 'biuld' does not exist; did you mean 'build'?", caught.exception.problems[0])
        with self.assertRaises(main.WorkflowError) as caught:
            main.load_workflow(os.path.join(FIXTURES, "broken", "needs-cycle.yml"))
        self.assertIn("needs cycle: lint -> release -> test -> lint", caught.exception.problems[0])
        with self.assertRaises(main.WorkflowError) as caught:
            workflow("on: push\njobs:\n  a:\n    runs-on: x\n    step:\n      - run: echo\n")
        self.assertTrue(any("did you mean 'steps'?" in p for p in caught.exception.problems))
        with self.assertRaises(main.WorkflowError) as caught:
            workflow("on: push\njobs:\n  a:\n    runs-on: x\n    if: github.ref ==\n    steps:\n      - run: echo\n")
        self.assertIn("jobs.a.if", caught.exception.problems[0])

    def test_waves_and_critical_path_of_the_sample(self):
        plan = main.plan_data(main.load_workflow(SAMPLE_WORKFLOW))
        self.assertEqual(plan["waves"], [["build", "experimental", "deps"], ["test", "package"], ["deploy"], ["notify", "cleanup"]])
        self.assertEqual(plan["critical_path"][:3], ["build", "test", "deploy"])
        legs = plan["jobs"]["test"]["legs"]
        self.assertEqual(len(legs), 6)
        self.assertIn({"python": "3.13", "os": "linux", "coverage": True}, legs)
        self.assertIn({"python": "3.14", "os": "linux"}, legs)
        self.assertNotIn({"python": "3.11", "os": "macos"}, legs)

    def test_order_violations_catch_a_job_that_started_too_early(self):
        summary = {"jobs": [
            {"job": "build", "needs": [], "started": 1, "finished": 4},
            {"job": "deploy", "needs": ["build"], "started": 3, "finished": 5},
        ]}
        self.assertEqual(main.order_violations(summary), ["deploy started (#3) before build finished (#4)"])


class FileCommandAndMaskingTests(unittest.TestCase):
    def test_output_files_take_pairs_and_heredocs(self):
        text = "version=1.4.0\nnotes<<EOF\nline one\na=b\nEOF\nempty=\n"
        self.assertEqual(main.parse_file_command(text, "GITHUB_OUTPUT"), [("version", "1.4.0"), ("notes", "line one\na=b"), ("empty", "")])
        with self.assertRaises(main.StepFailure) as caught:
            main.parse_file_command("notes<<EOF\nno end\n", "GITHUB_OUTPUT")
        self.assertIn("matching delimiter not found", str(caught.exception))
        with self.assertRaises(main.StepFailure):
            main.parse_file_command("just text\n", "GITHUB_ENV")

    def test_masking_is_exact_string_replacement(self):
        masker = main.Masker(["hunter2", "line-a\nline-b"])
        self.assertEqual(masker.mask("token=hunter2;"), "token=***;")
        self.assertEqual(masker.mask("line-b only"), "*** only")
        encoded = base64.b64encode(b"hunter2").decode()
        self.assertEqual(masker.mask(encoded), encoded)
        self.assertEqual(masker.mask("2retnuh"), "2retnuh")
        self.assertTrue(masker.reveals("x hunter2 y"))


class HashFilesTests(unittest.TestCase):
    def test_hash_files_is_sha256_over_the_per_file_digests(self):
        root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, root, True)
        for relative, body in [("requirements.txt", b"flask\n"), ("sub/requirements.txt", b"rich\n"), ("sub/skip.txt", b"x")]:
            os.makedirs(os.path.dirname(os.path.join(root, relative)), exist_ok=True)
            with open(os.path.join(root, relative), "wb") as handle:
                handle.write(body)
        one = hashlib.sha256(hashlib.sha256(b"flask\n").digest()).hexdigest()
        self.assertEqual(main.hash_files(root, ["requirements.txt"]), one)
        self.assertNotEqual(one, hashlib.sha256(b"flask\n").hexdigest())
        both = hashlib.sha256(hashlib.sha256(b"flask\n").digest() + hashlib.sha256(b"rich\n").digest()).hexdigest()
        self.assertEqual(main.hash_files(root, ["**/requirements.txt"]), both)
        self.assertEqual(main.hash_files(root, ["**/*.txt", "!sub/skip.txt"]), both)
        self.assertEqual(main.hash_files(root, ["nothing/*"]), "")


class RunTests(Sandbox):
    def test_job_outputs_flow_through_needs_and_every_job_gets_a_fresh_workspace(self):
        self.write("VERSION", "2.0.1\n")
        summary, log = self.run_text(
            """
            on: push
            jobs:
              build:
                runs-on: x
                outputs:
                  version: ${{ steps.meta.outputs.version }}
                steps:
                  - uses: actions/checkout@v7
                  - id: meta
                    run: echo "version=$(cat VERSION)" >> "$GITHUB_OUTPUT"
                  - run: echo leftover > leak.txt
              deploy:
                needs: build
                runs-on: x
                steps:
                  - env:
                      V: ${{ needs.build.outputs.version }}
                    run: echo "deploying $V"
                  - run: if [ -e leak.txt ] || [ -e VERSION ]; then echo dirty; else echo clean; fi
            """
        )
        self.assertEqual(summary["conclusion"], "success")
        self.assertEqual(summary["outputs"]["build"], {"version": "2.0.1"})
        self.assertIn("deploying 2.0.1", log)
        self.assertIn("| clean", log)
        self.assertEqual(main.order_violations(summary), [])

    def test_a_failed_job_skips_dependents_unless_always_or_failure(self):
        summary, _ = self.run_text(
            """
            on: push
            jobs:
              test:
                runs-on: x
                steps:
                  - run: exit 3
                  - run: echo never
                  - if: failure()
                    run: echo collecting logs
              deploy: {needs: test, runs-on: x, steps: [{run: echo deploy}]}
              notify: {needs: deploy, if: failure(), runs-on: x, steps: [{run: echo notify}]}
              report: {needs: [test, deploy], if: always(), runs-on: x, steps: [{run: echo report}]}
            """
        )
        self.assertEqual(summary["results"], {"test": "failure", "deploy": "skipped", "notify": "success", "report": "success"})
        self.assertEqual(summary["conclusion"], "failure")
        steps = self.legs(summary, "test")[0]["steps"]
        self.assertEqual([s["conclusion"] for s in steps], ["failure", "skipped", "success"])
        self.assertEqual(steps[0]["error"], "Process completed with exit code 3.")
        self.assertIn("needs test was failure", self.legs(summary, "deploy")[0]["reason"])

    def test_success_looks_at_every_ancestor_so_a_skipped_grandparent_skips(self):
        summary, _ = self.run_text(
            """
            on: push
            jobs:
              docs: {if: github.event_name == 'release', runs-on: x, steps: [{run: echo docs}]}
              gate: {needs: docs, if: always(), runs-on: x, steps: [{run: echo gate}]}
              plain: {needs: gate, runs-on: x, steps: [{run: echo plain}]}
              guarded: {needs: gate, if: "${{ !cancelled() && !failure() }}", runs-on: x, steps: [{run: echo guarded}]}
            """
        )
        self.assertEqual(summary["results"], {"docs": "skipped", "gate": "success", "plain": "skipped", "guarded": "success"})
        self.assertIn("docs was skipped", self.legs(summary, "plain")[0]["reason"])

    def test_continue_on_error_splits_outcome_from_conclusion(self):
        summary, _ = self.run_text(
            """
            on: push
            jobs:
              flaky:
                runs-on: x
                continue-on-error: true
                steps:
                  - run: exit 1
              lint:
                runs-on: x
                steps:
                  - id: style
                    continue-on-error: true
                    run: exit 1
                  - env:
                      O: ${{ steps.style.outcome }}
                      C: ${{ steps.style.conclusion }}
                    run: echo "outcome=$O conclusion=$C"
              after: {needs: flaky, runs-on: x, steps: [{run: echo ran}]}
            """
        )
        self.assertEqual(summary["conclusion"], "success")
        flaky = self.legs(summary, "flaky")[0]
        self.assertEqual((flaky["outcome"], flaky["result"]), ("failure", "success"))
        self.assertEqual(summary["results"]["after"], "success")
        with open(summary["log"]) as handle:
            self.assertIn("outcome=failure conclusion=success", handle.read())

    def test_the_default_shell_has_no_pipefail_but_shell_bash_does(self):
        summary, _ = self.run_text(
            """
            on: push
            jobs:
              default: {runs-on: x, steps: [{run: "false | true"}]}
              explicit: {runs-on: x, steps: [{run: "false | true", shell: bash}]}
              python: {runs-on: x, steps: [{run: "import sys; print(sys.version_info[0])", shell: python}]}
            """
        )
        self.assertEqual(summary["results"], {"default": "success", "explicit": "failure", "python": "success"})

    def test_step_timeout_kills_the_process_and_always_steps_still_run(self):
        started = time.monotonic()
        summary, log = self.run_text(
            """
            on: push
            jobs:
              slow:
                runs-on: x
                steps:
                  - timeout-minutes: 0.005
                    run: sleep 20
                  - if: always()
                    run: echo cleanup ran
            """
        )
        self.assertLess(time.monotonic() - started, 10)
        steps = self.legs(summary, "slow")[0]["steps"]
        self.assertEqual(steps[0]["conclusion"], "failure")
        self.assertIn("timeout-minutes", steps[0]["error"])
        self.assertIn("cleanup ran", log)

    def test_a_job_timeout_cancels_the_job(self):
        summary, log = self.run_text(
            """
            on: push
            jobs:
              slow:
                runs-on: x
                timeout-minutes: 0.005
                steps:
                  - run: sleep 20
                  - run: echo should not run
                  - if: always()
                    run: echo cleanup ran
            """
        )
        leg = self.legs(summary, "slow")[0]
        self.assertEqual(leg["result"], "cancelled")
        self.assertIn("timeout-minutes", leg["reason"])
        self.assertEqual([s["conclusion"] for s in leg["steps"]], ["cancelled", "skipped", "success"])
        self.assertEqual(summary["conclusion"], "cancelled")

    def test_fail_fast_cancels_the_rest_of_the_matrix(self):
        summary, _ = self.run_text(
            """
            on: push
            jobs:
              test:
                runs-on: x
                strategy:
                  max-parallel: 2
                  matrix:
                    n: [1, 2, 3, 4]
                steps:
                  - env:
                      N: ${{ matrix.n }}
                    run: if [ "$N" = 1 ]; then exit 1; fi; sleep 5
              loose:
                runs-on: x
                strategy:
                  fail-fast: false
                  matrix:
                    n: [1, 2]
                steps:
                  - env:
                      N: ${{ matrix.n }}
                    run: test "$N" = 2
            """
        )
        results = sorted(leg["result"] for leg in self.legs(summary, "test"))
        self.assertEqual(results, ["cancelled", "cancelled", "cancelled", "failure"])
        self.assertEqual(sorted(leg["result"] for leg in self.legs(summary, "loose")), ["failure", "success"])
        self.assertEqual(summary["results"]["test"], "failure")

    def test_env_precedence_is_workflow_job_github_env_step_and_defaults_win(self):
        summary, log = self.run_text(
            """
            on: push
            env:
              LEVEL: workflow
              ONLY_WORKFLOW: w
              GITHUB_SHA: forged
            jobs:
              show:
                runs-on: x
                env:
                  LEVEL: job
                steps:
                  - run: echo "1 $LEVEL $ONLY_WORKFLOW"
                  - run: echo "LEVEL=from-file" >> "$GITHUB_ENV"
                  - run: echo "2 $LEVEL"
                  - env:
                      LEVEL: step
                    run: echo "3 $LEVEL ${{ env.LEVEL }}"
                  - run: echo "4 $GITHUB_SHA"
                  - run: echo "5 [$HOME] [$API_TOKEN]"
            """,
            sha="a" * 40,
            secrets={"API_TOKEN": "tok"},
        )
        self.assertIn("| 1 job w", log)
        self.assertIn("| 2 from-file", log)
        self.assertIn("| 3 step step", log)
        self.assertIn("| 4 " + "a" * 40, log)
        self.assertRegex(log, r"\| 5 \[[^\]]*/home\] \[\]")

    def test_secrets_are_masked_add_mask_works_and_secret_outputs_are_dropped(self):
        summary, log = self.run_text(
            """
            on: push
            jobs:
              leak:
                runs-on: x
                outputs:
                  token: ${{ steps.t.outputs.token }}
                  safe: ${{ steps.t.outputs.safe }}
                steps:
                  - id: t
                    env:
                      TOKEN: ${{ secrets.API_TOKEN }}
                    run: |
                      echo "plain $TOKEN"
                      echo "::add-mask::generated-42"
                      echo "later generated-42"
                      echo "encoded $(printf %s "$TOKEN" | base64)"
                      echo "token=$TOKEN" >> "$GITHUB_OUTPUT"
                      echo "safe=ok" >> "$GITHUB_OUTPUT"
            """,
            secrets={"API_TOKEN": "s3cr3t-xyz"},
        )
        self.assertNotIn("s3cr3t-xyz", log)
        self.assertNotIn("generated-42", log)
        self.assertIn("plain ***", log)
        self.assertIn("later ***", log)
        self.assertIn("encoded " + base64.b64encode(b"s3cr3t-xyz").decode(), log)
        self.assertEqual(summary["outputs"]["leak"], {"safe": "ok"})
        self.assertTrue(any("Skip output 'token'" in w for w in summary["warnings"]))
        self.assertNotIn("s3cr3t-xyz", json.dumps(summary))

    def cache_workflow(self, path=".deps"):
        return f"""
            on: push
            jobs:
              deps:
                runs-on: x
                steps:
                  - uses: actions/checkout@v7
                  - id: cache
                    uses: actions/cache@v6
                    with:
                      path: {path}
                      key: deps-${{{{ hashFiles('requirements.txt') }}}}
                      restore-keys: deps-
                  - if: steps.cache.outputs.cache-hit != 'true'
                    run: mkdir -p {path} && cp requirements.txt {path}/installed && echo "installed"
                  - env:
                      HIT: ${{{{ steps.cache.outputs.cache-hit }}}}
                    run: echo "hit=[$HIT] files=$(ls {path})"
            """

    def test_cache_miss_then_exact_hit_then_partial_hit_and_path_changes_miss(self):
        self.write("requirements.txt", "flask\n")
        first, log1 = self.run_text(self.cache_workflow())
        second, log2 = self.run_text(self.cache_workflow())
        self.write("requirements.txt", "flask\nrich\n")
        third, log3 = self.run_text(self.cache_workflow())
        fourth, _ = self.run_text(self.cache_workflow(path=".deps2"))
        self.assertEqual([e["event"] for e in first["cache"]], ["restore-miss", "save"])
        self.assertIn("hit=[]", log1)
        self.assertEqual([e["event"] for e in second["cache"]], ["restore-hit"])
        self.assertIn("hit=[true] files=installed", log2)
        self.assertNotIn("| installed", log2)
        self.assertEqual([e["event"] for e in third["cache"]], ["restore-partial", "save"])
        self.assertIn("hit=[false]", log3)
        self.assertEqual(fourth["cache"][0]["event"], "restore-miss")
        key = first["cache"][0]["key"]
        self.assertEqual(key, "deps-" + hashlib.sha256(hashlib.sha256(b"flask\n").digest()).hexdigest())

    def test_caches_are_scoped_to_the_branch_with_a_default_branch_fallback(self):
        self.write("requirements.txt", "flask\n")
        self.run_text(self.cache_workflow(), ref="refs/heads/feature-a")
        sibling, _ = self.run_text(self.cache_workflow(), ref="refs/heads/feature-b")
        self.run_text(self.cache_workflow(), ref="refs/heads/main")
        later, _ = self.run_text(self.cache_workflow(), ref="refs/heads/feature-c")
        self.assertEqual(sibling["cache"][0]["event"], "restore-miss")
        self.assertEqual(later["cache"][0]["event"], "restore-hit")

    def test_artifacts_pass_between_jobs_and_names_are_unique_per_run(self):
        summary, log = self.run_text(
            """
            on: push
            jobs:
              make:
                runs-on: x
                steps:
                  - run: mkdir -p dist/sub && echo a > dist/a.txt && echo b > dist/sub/b.txt
                  - uses: actions/upload-artifact@v7
                    with: {name: bundle, path: dist/}
              again:
                needs: make
                runs-on: x
                steps:
                  - run: mkdir -p dist && echo c > dist/c.txt
                  - uses: actions/upload-artifact@v7
                    with: {name: bundle, path: dist/}
              use:
                needs: make
                runs-on: x
                steps:
                  - uses: actions/download-artifact@v8
                    with: {name: bundle, path: got}
                  - run: cat got/a.txt got/sub/b.txt
            """
        )
        self.assertEqual(summary["results"]["use"], "success")
        self.assertEqual(summary["results"]["again"], "failure")
        self.assertIn("already exists in this run", self.legs(summary, "again")[0]["steps"][1]["error"])
        self.assertEqual(summary["artifacts"], ["bundle"])
        self.assertIn("[use]   | b", log)

    def test_independent_jobs_run_in_parallel(self):
        started = time.monotonic()
        summary, _ = self.run_text(
            """
            on: push
            jobs:
              a: {runs-on: x, steps: [{run: sleep 0.6}]}
              b: {runs-on: x, steps: [{run: sleep 0.6}]}
              c: {needs: [a, b], runs-on: x, steps: [{run: "true"}]}
            """
        )
        elapsed = time.monotonic() - started
        a, b = self.legs(summary, "a")[0], self.legs(summary, "b")[0]
        self.assertLess(max(a["started"], b["started"]), min(a["finished"], b["finished"]))
        self.assertLess(elapsed, 1.15)
        self.assertEqual(main.order_violations(summary), [])

    def test_unsupported_actions_fail_at_set_up_with_the_reason(self):
        summary, _ = self.run_text(
            """
            on: push
            jobs:
              lint:
                runs-on: x
                steps:
                  - run: echo would run first
                  - uses: golangci/golangci-lint-action@v8
            """
        )
        leg = self.legs(summary, "lint")[0]
        self.assertEqual(leg["result"], "failure")
        self.assertEqual(leg["steps"], [])
        self.assertIn("https://github.com/golangci/golangci-lint-action at v8", leg["reason"])


class CliTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def test_run_the_sample_twice_and_read_the_json_summary(self):
        argv = ["run", SAMPLE_WORKFLOW, "--runs-dir", self.tmp, "--changed", "src/app.py", "--secret", "API_TOKEN=tok-123", "--json"]
        code, out, err = cli(argv)
        self.assertEqual(code, 0, err)
        first = json.loads(out)
        self.assertEqual(first["conclusion"], "success")
        self.assertEqual(first["results"]["notify"], "skipped")
        self.assertEqual(first["results"]["experimental"], "success")
        self.assertEqual(first["outputs"]["build"]["version"], "1.4.0")
        self.assertEqual(len([j for j in first["jobs"] if j["job"] == "test"]), 6)
        self.assertNotIn("tok-123", err)
        self.assertIn("CC-SECRET token=***", err)
        self.assertEqual(main.order_violations(first), [])
        code, out, err = cli(argv)
        self.assertEqual(code, 0, err)
        self.assertIn("CC-CACHE hit", err)
        self.assertEqual(json.loads(out)["cache"][0]["event"], "restore-hit")

    def test_act_event_skips_the_jobs_guarded_by_github_event_act(self):
        argv = ["run", SAMPLE_WORKFLOW, "--runs-dir", self.tmp, "--changed", "src/app.py", "--event-path", os.path.join(FIXTURES, "act-event.json"), "--json"]
        code, out, _ = cli(argv)
        self.assertEqual(code, 0)
        results = json.loads(out)["results"]
        self.assertEqual((results["deps"], results["package"]), ("skipped", "skipped"))

    def test_exit_codes_for_not_triggered_warnings_and_errors(self):
        code, out, _ = cli(["run", SAMPLE_WORKFLOW, "--runs-dir", self.tmp, "--changed", "README.md"])
        self.assertEqual((code, "not triggered" in out), (0, True))
        code, out, _ = cli(["check", os.path.join(FIXTURES, "broken", "if-always-true.yml")])
        self.assertEqual((code, "always true" in out), (1, True))
        code, _, err = cli(["check", os.path.join(FIXTURES, "broken", "needs-cycle.yml")])
        self.assertEqual((code, "needs cycle" in err), (2, True))
        code, out, _ = cli(["check", SAMPLE_WORKFLOW])
        self.assertEqual(code, 0, out)
        code, _, err = cli(["run", SAMPLE_WORKFLOW, "--runs-dir", self.tmp, "--secret", "CC_MISSING_SECRET_NAME"])
        self.assertEqual((code, "needs a value" in err), (2, True))

    def test_plan_and_expr_commands(self):
        code, out, _ = cli(["plan", SAMPLE_WORKFLOW, "--json"])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["jobs"]["deploy"]["wave"], 3)
        context = os.path.join(self.tmp, "ctx.json")
        with open(context, "w") as handle:
            json.dump({"needs": {"a": {"result": "success"}, "b": {"result": "skipped"}}}, handle)
        code, out, _ = cli(["expr", "${{ contains(needs.*.result, 'skipped') }}", "--context", context])
        self.assertEqual((code, json.loads(out)), (0, True))


if __name__ == "__main__":
    unittest.main()
