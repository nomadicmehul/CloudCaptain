import contextlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
CODE = os.path.dirname(HERE)
sys.path.insert(0, CODE)

import main  # noqa: E402

HAS_GIT = shutil.which("git") is not None
SAVED_ENV = {}
STATUS = re.compile(r"^(?P<name>.+?)\.{2,}(?:\(no files to check\))?(?P<status>Passed|Failed|Skipped)$")
FAKE_KEY = "AKIA" + "IOSFODNN7EXAMPLE"


def setUpModule():
    isolated = {"GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull, "GIT_AUTHOR_NAME": "Test", "GIT_AUTHOR_EMAIL": "test@example.invalid",
                "GIT_COMMITTER_NAME": "Test", "GIT_COMMITTER_EMAIL": "test@example.invalid",
                "GIT_AUTHOR_DATE": "1790100000 +0000", "GIT_COMMITTER_DATE": "1790100000 +0000"}
    for key, value in isolated.items():
        SAVED_ENV[key] = os.environ.get(key)
        os.environ[key] = value


def tearDownModule():
    for key, value in SAVED_ENV.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value


def git(cwd, *args, check=True, data=None):
    proc = subprocess.run(["git", "-c", "commit.gpgsign=false", *args], cwd=cwd, input=data, capture_output=True, text=True)
    if check and proc.returncode != 0:
        raise AssertionError(f"git {' '.join(args)}: {proc.stderr}")
    return proc


def write(path, text, mode="w"):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, mode, encoding="utf-8") as handle:
        handle.write(text)


def read(path):
    with open(path, encoding="utf-8") as handle:
        return handle.read()


def cli(argv, stdin=""):
    out, err = io.StringIO(), io.StringIO()
    saved = sys.stdin
    sys.stdin = io.StringIO(stdin)
    try:
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = main.main(argv)
    finally:
        sys.stdin = saved
    return code, out.getvalue(), err.getvalue()


def statuses(text):
    return {found.group("name"): found.group("status") for found in map(STATUS.match, text.splitlines()) if found}


class DemoRepo(unittest.TestCase):
    def setUp(self):
        if not HAS_GIT:
            self.skipTest("git is not installed")
        self.tmp = tempfile.mkdtemp()
        self.made = main.demo(os.path.join(self.tmp, "demo"), install_hooks=False)
        self.work = self.made["work"]
        self.env = dict(os.environ, PATH=self.made["bin"] + os.pathsep + os.environ.get("PATH", ""))
        self.logs = []

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def run_hooks(self, **kwargs):
        kwargs.setdefault("env", self.env)
        return main.run_hooks(self.work, log=self.logs.append, **kwargs)

    def by_id(self, report):
        return {result.id: result for result in report.results}


class YamlSubsetTests(unittest.TestCase):
    def test_parses_the_sample_config_with_comments_quotes_and_flow_lists(self):
        data = main.parse_yaml(read(os.path.join(main.SAMPLE, ".pre-commit-config.yaml")))
        hooks = data["repos"][0]["hooks"]
        self.assertEqual(data["default_stages"], ["pre-commit"])
        self.assertEqual(data["exclude"], "^hooks/")
        self.assertEqual([hook["id"] for hook in hooks], ["trailing-whitespace", "check-json", "no-secrets", "no-env-files", "conventional-commit"])
        self.assertEqual(hooks[0]["types_or"], ["python", "markdown"])
        self.assertEqual(hooks[2]["entry"], "AKIA[0-9A-Z]{16}|-----BEGIN [A-Z ]*PRIVATE KEY-----")
        self.assertEqual(hooks[3]["files"], r"(^|/)\.env$")

    def test_scalars_comments_and_nesting(self):
        text = "a: 'it''s # not a comment'  # a comment\nb: \"tab\\there\"\nc: true\nd: 42\ne:\n- x\n- - y\n  - z\nf:\n  g: ~\n  h: []\n"
        self.assertEqual(main.parse_yaml(text), {"a": "it's # not a comment", "b": "tab\there", "c": True, "d": 42,
                                                 "e": ["x", ["y", "z"]], "f": {"g": None, "h": []}})

    def test_unsupported_or_broken_yaml_names_the_line(self):
        cases = {"a: |\n  text\n": "line 1: block scalars", "a: 1\na: 2\n": "line 2: duplicate key", "a:\n\t- x\n": "line 2: tabs",
                 "a: 1\n    b: 2\n": "line 2: unexpected indentation", "a: [x, [y]]\n": "line 1: nested flow", "a: &anchor x\n": "line 1:"}
        for text, expected in cases.items():
            with self.subTest(text=text):
                with self.assertRaises(main.ConfigError) as caught:
                    main.parse_yaml(text)
                self.assertIn(expected, str(caught.exception))


class ConfigTests(unittest.TestCase):
    def test_validation_errors_and_warnings(self):
        base = "repos:\n  - repo: local\n    hooks:\n      - id: x\n        name: x\n        entry: echo\n        language: system\n"
        config = main.load_config(base + "        stages: [commit]\n        colour: red\n")
        self.assertEqual(config.hooks[0].stages, ["pre-commit"])
        self.assertEqual(config.warnings, ["hook x: unexpected key `colour` ignored"])
        self.assertEqual(main.load_config(base.replace("system", "unsupported")).hooks[0].language, "system")
        broken = {
            base.replace("repo: local", "repo: https://github.com/pre-commit/pre-commit-hooks"): "only `repo: local`",
            base.replace("language: system", "language: python"): "needs an environment",
            base + "        files: '(unclosed'\n": "invalid regular expression",
            base + "        stages: [pre-comit]\n": "unknown stage",
            base + "        pass_filenames: 'no'\n": "expected true or false",
            base.replace("        entry: echo\n", ""): "missing required key `entry`",
            "hooks: []\n": "top-level `repos:`",
        }
        for text, expected in broken.items():
            with self.subTest(expected=expected):
                with self.assertRaises(main.ConfigError) as caught:
                    main.load_config(text)
                self.assertIn(expected, str(caught.exception))


class TagAndSelectionTests(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()
        write(os.path.join(self.root, "app.py"), "print(1)\n")
        write(os.path.join(self.root, "deploy"), "#!/usr/bin/env bash\necho hi\n")
        os.chmod(os.path.join(self.root, "deploy"), 0o755)
        write(os.path.join(self.root, "docs/guide.md"), "# Guide\n")
        write(os.path.join(self.root, "vendor/lib.py"), "x = 1\n")
        write(os.path.join(self.root, ".env"), "X=1\n")
        with open(os.path.join(self.root, "logo.bin"), "wb") as handle:
            handle.write(b"\x89PNG\x00\x01")

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def test_tags_follow_extension_shebang_and_content(self):
        tag = lambda name: main.tags_for(os.path.join(self.root, name))
        self.assertEqual(tag("app.py"), {"file", "non-executable", "text", "python"})
        self.assertEqual(tag("deploy"), {"file", "executable", "text", "shell", "bash"})
        self.assertEqual(tag("logo.bin"), {"file", "non-executable", "binary"})
        self.assertEqual(tag(".env"), {"file", "non-executable", "text"})
        self.assertEqual(tag("missing"), set())

    def test_files_exclude_types_and_types_or_combine(self):
        names = ["app.py", "deploy", "docs/guide.md", "vendor/lib.py", ".env", "logo.bin", "deleted.py"]
        config = main.Config(hooks=[], exclude="^vendor/")
        pick = lambda **kw: main.select_files(main.Hook("h", "h", "true", "system", **kw), names, config, self.root)
        self.assertEqual(pick(), ["app.py", "deploy", "docs/guide.md", ".env", "logo.bin"])
        self.assertEqual(pick(types=["python"]), ["app.py"])
        self.assertEqual(pick(types=["text"], types_or=["python", "shell"]), ["app.py", "deploy"])
        self.assertEqual(pick(types=["text"], exclude_types=["markdown"], exclude=r"\.env$"), ["app.py", "deploy"])
        self.assertEqual(pick(files=r"(^|/)\.env$"), [".env"])


class SchedulingTests(unittest.TestCase):
    def job(self, index, files, **kw):
        return main.Job(index, main.Hook(f"h{index}", f"h{index}", "true", "system", **kw), files)

    def test_dependencies_only_between_overlapping_or_exclusive_hooks(self):
        jobs = [self.job(0, ["a.py"]), self.job(1, ["b.json"]), self.job(2, ["a.py", "b.json"]), self.job(3, ["c.md"]),
                self.job(4, ["c.md"], pass_filenames=False), self.job(5, ["d.txt"])]
        self.assertEqual(main.dependencies(jobs), {0: set(), 1: set(), 2: {0, 1}, 3: set(), 4: {0, 1, 2, 3}, 5: {4}})

    def test_disjoint_hooks_really_run_at_the_same_time_and_overlapping_ones_wait(self):
        jobs = [self.job(0, ["a.py"]), self.job(1, ["b.json"]), self.job(2, ["a.py", "b.json"])]
        barrier = threading.Barrier(2, timeout=10)
        order, lock = [], threading.Lock()

        def work(job):
            if job.index in (0, 1):
                barrier.wait()
            with lock:
                order.append(job.index)
            return main.HookResult(job.hook.id, job.hook.name, "Passed")

        results = main.schedule(jobs, main.dependencies(jobs), work, workers=4)
        self.assertEqual(sorted(results), [0, 1, 2])
        self.assertEqual(order[-1], 2)
        serial = []
        main.schedule(jobs, main.dependencies(jobs), lambda job: serial.append(job.index) or main.HookResult("x", "x", "Passed"), workers=1)
        self.assertEqual(serial, [0, 1, 2])


class CommitMessageTests(unittest.TestCase):
    def test_valid_messages(self):
        self.assertEqual(main.check_message("feat(api): add pagination\n").errors, [])
        bang = main.check_message("fix!: drop the v1 endpoint")
        self.assertEqual((bang.type, bang.scope, bang.breaking, bang.errors), ("fix", "", True, []))
        footer = main.check_message("refactor(core): split the parser\n\nBody text.\n\nBREAKING CHANGE: parse() returns a list\nRefs: #12\n")
        self.assertTrue(footer.breaking)
        self.assertEqual(footer.errors, [])
        edited = "docs: explain hooks\n# Please enter the commit message\n# ------------------------ >8 ------------------------\nnot: part of it\n"
        self.assertEqual(main.check_message(edited).errors, [])
        self.assertEqual(main.check_message("FEAT: shout").errors, [])
        for exempt in ("Merge branch 'topic'", 'Revert "feat: x"', "fixup! feat: x"):
            self.assertTrue(main.check_message(exempt).exempt, exempt)

    def test_invalid_messages_say_why(self):
        cases = {
            "": "empty", "# only a comment\n": "empty", "feat:no space": "is not `type(scope)!: description`",
            "update stuff": "is not `type(scope)!: description`", "feature: add x": "type 'feature' is not one of",
            "feat(): x": "scope in parentheses is empty", "feat: x\nbody right away": "blank line",
            "feat: x\n\nbreaking change: y": "in capitals", "feat: " + "x" * 100: "keep it to at most 100",
        }
        for text, expected in cases.items():
            with self.subTest(text=text):
                errors = main.check_message(text).errors
                self.assertTrue(any(expected in error for error in errors), errors)
        self.assertEqual(main.check_message("wip: x", types=["wip"]).errors, [])


class StagedRunTests(DemoRepo):
    def test_hooks_see_only_staged_files_and_unstaged_edits_survive(self):
        write(os.path.join(self.work, "app.py"), "def total(items):\n    return sum(items)\n# staged\n")
        git(self.work, "add", "app.py")
        write(os.path.join(self.work, "app.py"), "def total(items):\n    return sum(items)\n# staged\n# unstaged\n")
        write(os.path.join(self.work, "README.md"), "# Sample project   \n")
        write(os.path.join(self.work, "notes.md"), "untracked   \n")
        report = self.run_hooks()
        hooks = self.by_id(report)
        self.assertFalse(report.failed, main.render(report))
        self.assertEqual(hooks["trailing-whitespace"].files, ["app.py"])
        self.assertEqual(hooks["check-json"].reason, "(no files to check)")
        self.assertNotIn("conventional-commit", hooks)
        self.assertTrue(report.stash.patch and not report.stash.conflicted)
        self.assertTrue(read(os.path.join(self.work, "app.py")).endswith("# unstaged\n"))
        self.assertEqual(read(os.path.join(self.work, "README.md")), "# Sample project   \n")
        self.assertEqual(read(os.path.join(self.work, "notes.md")), "untracked   \n")
        self.assertIn("[WARNING] Unstaged files detected.", self.logs)

    def test_a_fixer_that_exits_zero_still_fails_because_it_changed_files(self):
        write(os.path.join(self.work, "app.py"), "def total(items):   \n    return sum(items)\n")
        git(self.work, "add", "app.py")
        report = self.run_hooks()
        fixer = self.by_id(report)["trailing-whitespace"]
        self.assertEqual((fixer.status, fixer.exit_code, fixer.modified), ("Failed", 0, ["app.py"]))
        self.assertIn("- files were modified by this hook", main.render(report))
        self.assertEqual(read(os.path.join(self.work, "app.py")), "def total(items):\n    return sum(items)\n")
        self.assertIsNone(report.stash.patch or None)
        git(self.work, "add", "app.py")
        self.assertFalse(self.run_hooks().failed)

    def test_a_fix_that_collides_with_unstaged_edits_is_rolled_back(self):
        staged = "def total(items):   \n    return sum(items)\n"
        unstaged = "def total(items, start=0):   \n    return sum(items, start)\n"
        write(os.path.join(self.work, "app.py"), staged)
        git(self.work, "add", "app.py")
        write(os.path.join(self.work, "app.py"), unstaged)
        report = self.run_hooks()
        self.assertTrue(report.stash.conflicted)
        self.assertEqual(self.by_id(report)["trailing-whitespace"].status, "Failed")
        self.assertEqual(read(os.path.join(self.work, "app.py")), unstaged)
        self.assertEqual(git(self.work, "show", ":app.py").stdout, staged)
        self.assertIn("[WARNING] Stashed changes conflicted with hook auto-fixes... Rolling back fixes...", self.logs)
        self.assertTrue(os.path.exists(report.stash.patch))

    def test_in_process_languages_skip_and_parallel_output_stays_in_config_order(self):
        write(os.path.join(self.work, "keys.txt"), f'key = "{FAKE_KEY}"\n')
        write(os.path.join(self.work, ".env"), "TOKEN=1\n")
        write(os.path.join(self.work, "settings.json"), "{bad\n")
        write(os.path.join(self.work, "tidy.py"), "x = 1\n")
        git(self.work, "add", "-A")
        report = self.run_hooks(env=dict(self.env, SKIP="check-json"), workers=8)
        self.assertEqual([r.id for r in report.results], ["trailing-whitespace", "check-json", "no-secrets", "no-env-files"])
        hooks = self.by_id(report)
        self.assertEqual((hooks["check-json"].status, hooks["check-json"].reason), ("Skipped", "SKIP"))
        self.assertEqual(hooks["no-secrets"].output, f'keys.txt:1:key = "{FAKE_KEY}"\n')
        self.assertEqual(hooks["no-env-files"].output, ".env files hold secrets; keep them out of Git\n\n.env\n")
        self.assertEqual(hooks["no-secrets"].after, ["trailing-whitespace"])
        self.assertEqual(statuses(main.render(report)), {"trim trailing whitespace": "Passed", "check json syntax": "Skipped",
                                                         "block secrets": "Failed", "forbid .env files": "Failed"})

    def test_refuses_an_unstaged_config_and_all_files_needs_no_stash(self):
        with open(os.path.join(self.work, ".pre-commit-config.yaml"), "a", encoding="utf-8") as handle:
            handle.write("# edited\n")
        with self.assertRaises(main.HookError) as caught:
            self.run_hooks()
        self.assertIn("git add .pre-commit-config.yaml", str(caught.exception))
        report = self.run_hooks(all_files=True)
        self.assertIsNone(report.stash)
        self.assertEqual(self.by_id(report)["trailing-whitespace"].files, ["README.md", "app.py"])


class InstalledHookTests(DemoRepo):
    def commit(self, message):
        proc = subprocess.run(["git", "-c", "commit.gpgsign=false", "commit", "-m", message], cwd=self.work, env=self.env, capture_output=True, text=True)
        return proc.returncode, proc.stdout + proc.stderr

    def test_real_git_commit_runs_pre_commit_and_commit_msg_shims(self):
        main.install(self.work, ["pre-commit", "commit-msg"])
        head = git(self.work, "rev-parse", "HEAD").stdout
        write(os.path.join(self.work, "app.py"), "def total(items):\n    return sum(items, 0)  \n")
        git(self.work, "add", "app.py")
        code, out = self.commit("feat: add totals")
        self.assertNotEqual(code, 0)
        self.assertEqual(statuses(out)["trim trailing whitespace"], "Failed", out)
        git(self.work, "add", "app.py")
        code, out = self.commit("added totals")
        self.assertNotEqual(code, 0, out)
        self.assertEqual(statuses(out).get("conventional commit message"), "Failed", out)
        self.assertIn("is not `type(scope)!: description`", out)
        self.assertEqual(git(self.work, "rev-parse", "HEAD").stdout, head)
        code, out = self.commit("feat: add totals")
        self.assertEqual(code, 0, out)
        self.assertEqual(git(self.work, "log", "-1", "--format=%s").stdout.strip(), "feat: add totals")

    def test_install_respects_core_hooks_path_and_refuses_foreign_hooks(self):
        git(self.work, "config", "core.hooksPath", ".githooks")
        written = main.install(self.work, ["pre-commit"])
        self.assertEqual(written, [os.path.join(self.work, ".githooks", "pre-commit")])
        self.assertTrue(os.access(written[0], os.X_OK))
        main.install(self.work, ["pre-commit"])
        write(os.path.join(self.work, ".githooks", "commit-msg"), "#!/bin/sh\nexit 0\n")
        with self.assertRaises(main.HookError):
            main.install(self.work, ["commit-msg"])
        main.install(self.work, ["commit-msg"], overwrite=True)
        self.assertIn(main.MARKER, read(os.path.join(self.work, ".githooks", "commit-msg")))


class PreReceiveTests(DemoRepo):
    def orphan_commit(self, files, message, start="main"):
        git(self.work, "checkout", "-q", "-b", "scratch", start)
        for name, content in files.items():
            mode = "wb" if isinstance(content, bytes) else "w"
            with open(os.path.join(self.work, name), mode) as handle:
                handle.write(content)
        git(self.work, "add", "-A")
        git(self.work, "commit", "-q", "--no-verify", "-m", message)
        sha = git(self.work, "rev-parse", "HEAD").stdout.strip()
        git(self.work, "checkout", "-q", "main")
        git(self.work, "branch", "-q", "-D", "scratch")
        return sha

    def test_reads_updates_and_rejects_garbage(self):
        self.assertEqual(main.read_updates("0 1 refs/heads/a\n\n2 3 refs/heads/b\n"), [("0", "1", "refs/heads/a"), ("2", "3", "refs/heads/b")])
        with self.assertRaises(main.HookError):
            main.read_updates("only-two fields\n")

    def test_new_commits_are_checked_for_size_secrets_and_protected_ref_rules(self):
        main_sha = git(self.work, "rev-parse", "main").stdout.strip()
        big = self.orphan_commit({"big.bin": b"\0" * 2048, "creds.py": f'KEY = "{FAKE_KEY}"\n'}, "wip")
        policy = main.Policy(max_bytes=1024, protected=["refs/heads/main"])
        found = main.check_push(main.Git(self.work), [(main_sha, big, "refs/heads/main")], policy)
        rules = sorted((v.rule, v.path) for v in found)
        self.assertEqual(rules, [("conventional-commit", ""), ("large-file", "big.bin"), ("secret", "creds.py")])
        self.assertIn("creds.py:1 looks like a aws-access-key-id", [v.detail for v in found if v.rule == "secret"][0])
        topic = main.check_push(main.Git(self.work), [("0" * 40, big, "refs/heads/topic")], policy)
        self.assertEqual(sorted(v.rule for v in topic), ["large-file", "secret"])
        ok = self.orphan_commit({"app.py": "def total(items):\n    return sum(items)\n\n\nTOTAL = 0\n"}, "feat: keep a running total")
        self.assertEqual(main.check_push(main.Git(self.work), [(main_sha, ok, "refs/heads/main")], policy), [])

    def test_protected_refs_refuse_deletion_and_rewrites(self):
        main_sha = git(self.work, "rev-parse", "main").stdout.strip()
        rewrite = self.orphan_commit({"app.py": "x = 1\n"}, "feat: start over", start="main")
        git(self.work, "update-ref", "refs/heads/old", main_sha)
        sibling = git(self.work, "commit-tree", "-m", "feat: sibling", git(self.work, "rev-parse", "main^{tree}").stdout.strip(), data="").stdout.strip()
        policy = main.Policy(protected=["refs/heads/main", "refs/heads/release/*"])
        repo = main.Git(self.work)
        self.assertEqual([v.rule for v in main.check_push(repo, [(main_sha, "0" * 40, "refs/heads/release/1.0")], policy)], ["delete-protected"])
        self.assertEqual(main.check_push(repo, [(main_sha, "0" * 40, "refs/heads/topic")], policy), [])
        self.assertEqual([v.rule for v in main.check_push(repo, [(rewrite, sibling, "refs/heads/main")], policy)], ["non-fast-forward"])

    def test_a_real_push_is_rejected_and_quarantined_objects_never_land(self):
        origin = self.made["origin"]
        before = git(origin, "rev-parse", "main").stdout.strip()
        write(os.path.join(self.work, "creds.py"), f'KEY = "{FAKE_KEY}"\n')
        git(self.work, "add", "-A")
        git(self.work, "commit", "-q", "--no-verify", "-m", "feat: add credentials")
        bad = git(self.work, "rev-parse", "HEAD").stdout.strip()
        push = git(self.work, "push", "origin", "main", check=False)
        self.assertNotEqual(push.returncode, 0)
        self.assertIn("pre-receive hook declined", push.stderr)
        self.assertIn("secret: creds.py:1", push.stderr)
        self.assertEqual(git(origin, "rev-parse", "main").stdout.strip(), before)
        self.assertNotEqual(git(origin, "cat-file", "-e", bad, check=False).returncode, 0)
        git(self.work, "reset", "-q", "--hard", "HEAD~1")
        write(os.path.join(self.work, "app.py"), "def total(items):\n    return sum(items, 0)\n")
        git(self.work, "commit", "-q", "--no-verify", "-am", "fix: start sums at zero")
        self.assertEqual(git(self.work, "push", "origin", "main", check=False).returncode, 0)


class CliTests(DemoRepo):
    def test_cli_exit_codes_json_and_dry_run(self):
        message = os.path.join(self.tmp, "msg")
        write(message, "chore: tidy\n")
        self.assertEqual(cli(["check-msg", message])[0], 0)
        write(message, "tidy\n")
        code, _, err = cli(["check-msg", message])
        self.assertEqual(code, 1)
        self.assertIn("commit message:", err)
        cwd = os.getcwd()
        os.chdir(self.work)
        try:
            write("app.py", "x = 1   \n")
            git(self.work, "add", "app.py")
            code, out, _ = cli(["run", "--dry-run"])
            self.assertEqual(code, 0)
            self.assertIn("trailing-whitespace: start at once; 1 file(s): app.py", out)
            code, out, _ = cli(["run", "--json"])
            self.assertEqual(code, 1)
            data = json.loads(out)
            self.assertEqual([h["status"] for h in data["hooks"]], ["Failed", "Skipped", "Passed", "Skipped"])
            write(".pre-commit-config.yaml", "repos: [\n")
            git(self.work, "add", ".pre-commit-config.yaml")
            code, _, err = cli(["run"])
            self.assertEqual(code, 2)
            self.assertIn("minihooks: line 1", err)
        finally:
            os.chdir(cwd)
        code, out, _ = cli(["tags", os.path.join(self.work, "app.py")])
        self.assertEqual((code, out.split(": ")[1].split()), (0, ["file", "non-executable", "python", "text"]))


if __name__ == "__main__":
    unittest.main()
