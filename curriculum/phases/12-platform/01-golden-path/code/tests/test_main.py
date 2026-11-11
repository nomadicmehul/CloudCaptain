import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import main  # noqa: E402

GIT_ENV = {
    "GIT_AUTHOR_NAME": "Test",
    "GIT_AUTHOR_EMAIL": "test@example.invalid",
    "GIT_COMMITTER_NAME": "Test",
    "GIT_COMMITTER_EMAIL": "test@example.invalid",
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_CONFIG_GLOBAL": os.devnull,
}
HAVE_GIT = shutil.which("git") is not None


def render(source, context=None, **envops):
    return main.Template(source, "test", **envops).render(context or {})


def snapshot(files, label=None):
    return main.Snapshot({path: main.OutFile(text.encode()) for path, text in files.items()}, "/templates/demo", label, None)


class JinjaSubsetTests(unittest.TestCase):
    def test_expressions_filters_and_conditionals(self):
        context = {"name": "checkout", "port": 8080, "envs": ["staging", "production"], "db": False}
        self.assertEqual(render("{{ name | upper }}:{{ port + 1 }}", context), "CHECKOUT:8081")
        self.assertEqual(render("{{ envs | join(', ') }} ({{ envs | length }})", context), "staging, production (2)")
        self.assertEqual(render("{{ 'db ' ~ name if db else 'none' }}", context), "none")
        self.assertEqual(render("{% if port < 1024 %}low{% elif name == 'checkout' %}named{% else %}other{% endif %}", context), "named")
        self.assertEqual(render("{{ missing | default('x') }} {{ name is defined }} {{ 'a' in envs }}", context), "x True False")

    def test_for_loop_exposes_loop_variables(self):
        source = "{% for env in envs %}{{ loop.index }}={{ env }}<-{{ loop.previtem if not loop.first else 'start' }};{% endfor %}"
        self.assertEqual(render(source, {"envs": ["dev", "stg", "prd"]}), "1=dev<-start;2=stg<-dev;3=prd<-stg;")

    def test_trim_and_lstrip_blocks_remove_the_lines_of_block_tags(self):
        source = "a:\n{% if on %}\n  b: 1\n  {% for x in xs %}\n  - {{ x }}\n  {% endfor %}\n{% endif %}\nc: 2\n"
        context = {"on": True, "xs": [1, 2]}
        self.assertEqual(render(source, context, trim_blocks=True, lstrip_blocks=True), "a:\n  b: 1\n  - 1\n  - 2\nc: 2\n")
        self.assertEqual(render(source, context), "a:\n\n  b: 1\n  \n  - 1\n  \n  - 2\n  \n\nc: 2\n")

    def test_dash_modifiers_and_raw_blocks(self):
        self.assertEqual(render("a  {{- ' b ' -}}  c"), "a b c")
        source = "run: docker build -t app:{% raw %}${{ github.sha }}{% endraw %} ."
        self.assertEqual(render(source, trim_blocks=True), "run: docker build -t app:${{ github.sha }} .")
        self.assertEqual(render("{{ '}}' }}{# a comment #}!"), "}}!")

    def test_undefined_names_fail_with_the_file_name(self):
        with self.assertRaises(main.UndefinedError) as raised:
            render("{{ servce_name }}", {"service_name": "x"})
        self.assertIn("'servce_name' is undefined", str(raised.exception))
        self.assertIn("test", str(raised.exception))
        with self.assertRaises(main.TemplateError):
            render("{% if x %}never closed")

    def test_tojson_and_regex_search_behave_like_jinja_and_ansible(self):
        self.assertEqual(render("{{ d | tojson }}", {"d": "a & <b> 'c'"}), '"a \\u0026 \\u003cb\\u003e \\u0027c\\u0027"')
        self.assertEqual(render("{{ 'svc-1' | regex_search('^[a-z]+') }}|{{ 'Svc' | regex_search('^[a-z]+') }}"), "svc|None")


class YamlTests(unittest.TestCase):
    def test_answers_file_formatting_matches_pyyaml(self):
        value = {"_commit": "v1.0.0", "port": 8080, "x": "8080", "y": "yes", "z": "", "d": "Orders: carts", "w": "a #b",
                 "tilde": "~", "dash": "-x", "envs": ["dev", "prod"], "on": True, "n": None, "q": "it's"}
        expected = ("_commit: v1.0.0\nd: 'Orders: carts'\ndash: -x\nenvs:\n- dev\n- prod\nn: null\n'on': true\nport: 8080\n"
                    "q: it's\ntilde: '~'\nw: 'a #b'\nx: '8080'\ny: 'yes'\nz: ''\n")
        self.assertEqual(main.nice_yaml(value), expected)

    def test_parser_reads_what_the_emitter_writes(self):
        value = {"description": "Orders: carts & \"checkouts\"", "environments": ["dev", "prod"], "port": 9090, "database": True}
        self.assertEqual(main.parse_yaml(main.nice_yaml(value)), value)

    def test_load_config_reads_settings_and_typed_questions(self):
        with open(os.path.join(main.LESSON_TEMPLATE, "copier.yml"), encoding="utf-8") as handle:
            config = main.load_config(handle.read())
        self.assertEqual((config.subdirectory, config.suffix, config.trim_blocks, config.lstrip_blocks), ("skeleton", ".jinja", True, True))
        types = {q.name: q.type for q in config.questions}
        self.assertEqual(types, {"service_name": "str", "description": "str", "owner": "str", "port": "int", "replicas": "int",
                                 "exposure": "str", "hostname": "str", "database": "bool", "environments": "yaml"})
        self.assertEqual(len(config.tasks), 3)
        with self.assertRaises(main.TemplateError) as raised:
            main.load_config("_jinja_extensions: [x]\nname: str\n")
        self.assertIn("unsupported setting _jinja_extensions", str(raised.exception))


class AnswerTests(unittest.TestCase):
    def setUp(self):
        with open(os.path.join(main.LESSON_TEMPLATE, "copier.yml"), encoding="utf-8") as handle:
            self.config = main.load_config(handle.read())

    def test_defaults_render_from_earlier_answers_and_strings_are_cast(self):
        answers = main.resolve_answers(self.config, {"service_name": "orders", "owner": "shop", "port": "9090", "database": "yes",
                                                     "environments": "[dev, prod]", "exposure": "public"})
        self.assertEqual(answers["description"], "orders service")
        self.assertEqual((answers["port"], answers["database"], answers["environments"]), (9090, True, ["dev", "prod"]))
        self.assertEqual(answers["hostname"], "orders.example.com")

    def test_a_question_whose_when_is_false_is_not_asked_or_recorded(self):
        answers = main.resolve_answers(self.config, {"service_name": "orders", "owner": "shop"})
        self.assertNotIn("hostname", answers)
        self.assertEqual(answers["exposure"], "internal")

    def test_every_problem_is_reported_at_once(self):
        with self.assertRaises(main.AnswerError) as raised:
            main.resolve_answers(self.config, {"service_name": "Orders_Svc", "port": "80", "exposure": "world", "replicas": "1", "colour": "red"})
        problems = "\n".join(raised.exception.problems)
        self.assertIn("colour: not a question of this template", problems)
        self.assertIn("service_name: 'Orders_Svc' must be a DNS label", problems)
        self.assertIn("owner: required", problems)
        self.assertIn("port: 80 must be between 1024 and 65535", problems)
        self.assertIn("replicas: 1 must be between 2 and 10", problems)
        self.assertIn("exposure: 'world' is not one of internal, public", problems)

    def test_previous_answers_win_over_defaults_and_new_data_wins_over_both(self):
        answers = main.resolve_answers(self.config, {"replicas": "4"}, {"service_name": "a-b-c", "owner": "o", "replicas": 3, "port": 9000})
        self.assertEqual((answers["port"], answers["replicas"]), (9000, 4))


class RenderTests(unittest.TestCase):
    def setUp(self):
        self.snapshot = main.open_template(main.LESSON_TEMPLATE, None)
        self.config = self.snapshot.config()

    def files(self, data):
        answers = main.resolve_answers(self.config, data)
        return main.render_project(self.snapshot, self.config, answers, "copy", "svc")

    def test_names_are_templated_and_a_name_that_renders_empty_is_skipped(self):
        internal = self.files({"service_name": "orders", "owner": "shop"})
        public = self.files({"service_name": "orders", "owner": "shop", "exposure": "public"})
        self.assertNotIn("k8s/httproute.yaml", internal)
        self.assertIn("k8s/httproute.yaml", public)
        self.assertIn("orders.example.com", public["k8s/httproute.yaml"].data.decode())
        self.assertEqual(sorted(internal), [".copier-answers.yml", ".github/workflows/ci.yaml", ".gitignore", "Dockerfile", "README.md",
                                            "app/__init__.py", "app/main.py", "catalog-info.yaml", "k8s/deployment.yaml", "k8s/service.yaml",
                                            "tests/__init__.py", "tests/test_app.py"])

    def test_rendered_files_carry_the_answers_and_parse(self):
        files = self.files({"service_name": "orders", "owner": "shop", "database": "true", "environments": "[dev, prod]"})
        catalog = main.parse_yaml(files["catalog-info.yaml"].data.decode())
        self.assertEqual(catalog["spec"]["owner"], "group:default/shop")
        self.assertEqual(catalog["metadata"]["annotations"]["cloudcaptain.io/template-version"], "unversioned")
        self.assertEqual(catalog["spec"]["dependsOn"], ["resource:default/orders-database"])
        workflow = main.parse_yaml(files[".github/workflows/ci.yaml"].data.decode())
        self.assertEqual({job: spec.get("needs") for job, spec in workflow["jobs"].items()}, {"build": None, "deploy-dev": "build", "deploy-prod": "deploy-dev"})
        self.assertIn("${{ github.sha }}", files[".github/workflows/ci.yaml"].data.decode())
        answers = main.parse_yaml(files[".copier-answers.yml"].data.decode())
        self.assertEqual(answers["_src_path"], main.LESSON_TEMPLATE)
        self.assertEqual(main.check_rendered(files), [])

    def test_check_rendered_reports_a_template_that_breaks_yaml(self):
        broken = {"deploy.yaml": main.OutFile(b"a: [1, 2\n"), "fine.yaml": main.OutFile(b"a: 1\n")}
        self.assertEqual(len(main.check_rendered(broken)), 1)
        self.assertIn("deploy.yaml", main.check_rendered(broken)[0])

    def test_without_a_subdirectory_copier_files_are_excluded(self):
        source = snapshot({"copier.yml": "name: x\n", "{{ name }}.txt.jinja": "hello {{ name }}\n", "__pycache__/a.pyc": ""})
        files = main.render_project(source, source.config(), {"name": "x"}, "copy", "d")
        self.assertEqual({p: f.data for p, f in files.items()}, {"x.txt": b"hello x\n"})


class MergeTests(unittest.TestCase):
    base = "".join(f"line {n}\n" for n in range(1, 11))

    def test_changes_far_apart_combine(self):
        ours = self.base.replace("line 2\n", "line 2 ours\n")
        theirs = self.base.replace("line 9\n", "line 9 theirs\n")
        merged, conflicts = main.merge3(self.base, ours, theirs)
        self.assertEqual(conflicts, 0)
        self.assertEqual(merged, self.base.replace("line 2\n", "line 2 ours\n").replace("line 9\n", "line 9 theirs\n"))

    def test_the_same_change_on_both_sides_is_not_a_conflict(self):
        both = self.base.replace("line 5\n", "line five\n")
        self.assertEqual(main.merge3(self.base, both, both), (both, 0))

    def test_overlapping_and_touching_changes_conflict_like_git_merge_file(self):
        ours = self.base.replace("line 5\n", "line 5 ours\n")
        theirs = self.base.replace("line 5\n", "line 5 theirs\n")
        merged, conflicts = main.merge3(self.base, ours, theirs)
        self.assertEqual(conflicts, 1)
        self.assertIn("line 4\n<<<<<<< before updating\nline 5 ours\n=======\nline 5 theirs\n>>>>>>> after updating\nline 6\n", merged)
        touching = main.merge3(self.base, self.base.replace("line 5\n", "five\n"), self.base.replace("line 6\n", "six\n"))
        self.assertEqual(touching[1], 1)

    def test_a_conflict_without_a_final_newline_still_closes_its_markers(self):
        merged, conflicts = main.merge3("a\nb", "a\nours", "a\ntheirs")
        self.assertEqual((merged, conflicts), ("a\n<<<<<<< before updating\nours\n=======\ntheirs\n>>>>>>> after updating\n", 1))

    def test_plan_file_decides_from_three_versions(self):
        out = main.OutFile
        cases = [
            (None, None, out(b"new"), "added"),
            (out(b"old"), b"old", None, "removed"),
            (out(b"old"), b"mine", None, "orphaned"),
            (out(b"old"), None, out(b"new"), "skipped"),
            (out(b"old"), b"mine", out(b"old"), "kept"),
            (out(b"old"), b"old", out(b"new"), "updated"),
            (out(b"a\nb\nc\nd\ne\n"), b"A\nb\nc\nd\ne\n", out(b"a\nb\nc\nd\nE\n"), "merged"),
        ]
        for base, ours, theirs, action in cases:
            with self.subTest(action=action):
                self.assertEqual(main.plan_file("f", base, ours, theirs)[0].action, action)
        self.assertEqual(main.plan_file("f", out(b"a\nb\nc\nd\ne\n"), b"A\nb\nc\nd\ne\n", out(b"a\nb\nc\nd\nE\n"))[1], b"A\nb\nc\nd\nE\n")


@unittest.skipUnless(HAVE_GIT, "git is not installed")
class GitTests(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.dict(os.environ, GIT_ENV)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.work = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.work, True)
        self.repo = main.build_template_repo(self.work)
        self.service = os.path.join(self.work, "checkout")

    def git(self, *args):
        return subprocess.run(["git", "-C", self.service, *args], capture_output=True, text=True, check=True).stdout

    def generate(self, **extra):
        data = {"service_name": "checkout", "owner": "payments"}
        data.update(extra)
        return main.generate(self.repo, self.service, data, ref="v1.0.0", trust=True)

    def test_tasks_need_trust_and_run_once_trusted(self):
        with self.assertRaises(main.ScaffoldError) as raised:
            main.generate(self.repo, self.service, {"service_name": "checkout", "owner": "payments"})
        self.assertIn("--trust", str(raised.exception))
        self.assertFalse(os.path.exists(self.service))
        result = self.generate()
        self.assertEqual(result.version, "v1.0.0")
        self.assertEqual([t["exit"] for t in result.tasks], [0, 0, 0])
        self.assertEqual(self.git("log", "--format=%s").strip(), "Scaffold checkout from the service template")
        with open(os.path.join(self.service, "catalog-info.yaml"), encoding="utf-8") as handle:
            self.assertIn('cloudcaptain.io/template-version: "v1.0.0"\n', handle.read())
        self.assertEqual(self.git("status", "--porcelain"), "")

    def test_highest_version_tag_is_the_default(self):
        result = main.generate(self.repo, self.service, {"service_name": "checkout", "owner": "payments"}, skip_tasks=True)
        self.assertEqual(result.version, "v1.1.0")
        self.assertIn("cpu_request", result.answers)
        self.assertFalse(os.path.exists(os.path.join(self.service, ".git")))

    def test_update_keeps_own_changes_lands_template_changes_and_marks_conflicts(self):
        self.generate()
        main.own_changes(self.service, base_image=True)
        self.git("commit", "--quiet", "--all", "--message", "own changes")
        result = main.update(self.service, ref="v1.1.0", trust=True)
        actions = {c.path: c.action for c in result.changes if c.action != "unchanged"}
        self.assertEqual(actions, {".github/workflows/ci.yaml": "updated", "Dockerfile": "conflict", "README.md": "merged",
                                   "app/main.py": "kept", "catalog-info.yaml": "updated", "k8s/deployment.yaml": "merged"})
        with open(os.path.join(self.service, "catalog-info.yaml"), encoding="utf-8") as handle:
            text = handle.read()
        self.assertIn('    cloudcaptain.io/template-version: "v1.1.0"\n', text)
        self.assertEqual(main.parse_yaml(text)["metadata"]["annotations"]["cloudcaptain.io/template-version"], "v1.1.0")
        with open(os.path.join(self.service, "k8s", "deployment.yaml"), encoding="utf-8") as handle:
            deployment = main.parse_yaml(handle.read())
        container = deployment["spec"]["template"]["spec"]["containers"][0]
        self.assertEqual([e["name"] for e in container["env"]], ["PORT", "FEATURE_FLAGS"])
        self.assertEqual(container["resources"]["requests"]["cpu"], "100m")
        with open(os.path.join(self.service, "Dockerfile"), encoding="utf-8") as handle:
            self.assertTrue(handle.read().startswith("<<<<<<< before updating\nFROM python:3.13-slim-trixie\n=======\nFROM python:3.14-slim\n>>>>>>> after updating\n"))
        answers = main.read_answers(self.service)
        self.assertEqual((answers["_commit"], answers["cpu_request"]), ("v1.1.0", "100m"))

    def test_update_refuses_uncommitted_changes_and_dry_run_writes_nothing(self):
        self.generate()
        with open(os.path.join(self.service, "README.md"), "a", encoding="utf-8") as handle:
            handle.write("draft\n")
        with self.assertRaises(main.ScaffoldError) as raised:
            main.update(self.service, trust=True)
        self.assertIn("uncommitted changes", str(raised.exception))
        self.git("checkout", "--", "README.md")
        result = main.update(self.service, trust=True, dry_run=True)
        self.assertEqual(result.to_version, "v1.1.0")
        self.assertEqual(self.git("status", "--porcelain"), "")

    def test_changing_an_answer_on_update_is_a_template_change(self):
        self.generate()
        main.update(self.service, data={"replicas": "3"}, ref="v1.0.0", trust=True)
        self.assertIn("replicas: 3\n", self.git("diff"))
        self.assertEqual(sorted(self.git("diff", "--name-only").split()), [".copier-answers.yml", "k8s/deployment.yaml"])

    def test_cli_exit_codes(self):
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            code = main.main(["new", self.service, "--template", self.repo, "-d", "service_name=X", "--trust"])
        self.assertEqual(code, 2)
        self.assertIn("owner: required", stderr.getvalue())
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            self.assertEqual(main.main(["new", self.service, "--template", self.repo, "--ref", "v1.0.0", "-d", "service_name=checkout",
                                        "-d", "owner=payments", "--trust", "--json"]), 0)
        self.assertIn("catalog-info.yaml", json.loads(stdout.getvalue())["files"])
        main.own_changes(self.service, base_image=True)
        self.git("commit", "--quiet", "--all", "--message", "own changes")
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(main.main(["update", self.service, "--trust"]), 1)

    def test_demo_runs_the_whole_story(self):
        with contextlib.redirect_stdout(io.StringIO()):
            outcome = main.demo()
        self.assertEqual(outcome["update"]["conflicts"], ["Dockerfile"])
        self.assertTrue(outcome["deployment_keeps_flag"] and outcome["deployment_has_probes"])


if __name__ == "__main__":
    unittest.main()
