import contextlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
CODE = os.path.dirname(HERE)
sys.path.insert(0, CODE)

import main  # noqa: E402

FIXTURES = os.path.join(CODE, "fixtures")
CODEOWNERS = os.path.join(FIXTURES, "CODEOWNERS")
RULESET = os.path.join(FIXTURES, "ruleset.json")
TEAMS = os.path.join(FIXTURES, "teams.json")
QUEUE = ["rename-total", "docs-guide", "invoice", "timeout-60", "timeout-45", "bad-tax", "readme", "workers-8", "stale-approval"]
PASSING = ["rename-total", "docs-guide", "timeout-60", "readme"]
SAVED_ENV = {}


def git_version():
    if not shutil.which("git"):
        return (0, 0)
    found = re.search(r"(\d+)\.(\d+)", subprocess.run(["git", "version"], capture_output=True, text=True).stdout)
    return tuple(int(part) for part in found.groups()) if found else (0, 0)


HAS_GIT = shutil.which("git") is not None
HAS_MERGE_TREE = git_version() >= (2, 40)


def setUpModule():
    isolated = {"GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull, "GIT_AUTHOR_DATE": "1790100000 +0000", "GIT_COMMITTER_DATE": "1790100000 +0000"}
    for key, value in isolated.items():
        SAVED_ENV[key] = os.environ.get(key)
        os.environ[key] = value


def tearDownModule():
    for key, value in SAVED_ENV.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value


def read(path):
    with open(path, encoding="utf-8") as handle:
        return handle.read()


def run(argv):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = main.main(argv)
    return code, out.getvalue(), err.getvalue()


def git(repo, *args):
    return subprocess.run(["git", "-C", repo, "-c", "user.name=Oracle", "-c", "user.email=oracle@example.invalid", *args], check=True, capture_output=True, text=True).stdout


def owners_of(text, path):
    rule = main.owner_rule(main.parse_codeowners(text)[0], path)
    return (rule.line, rule.owners) if rule else None


def check_ignore(gitignore_text, paths):
    root = tempfile.mkdtemp()
    try:
        subprocess.run(["git", "-c", "init.defaultBranch=main", "init", "--quiet", root], check=True)
        with open(os.path.join(root, ".gitignore"), "w", encoding="utf-8") as handle:
            handle.write(gitignore_text)
        for path in paths:
            os.makedirs(os.path.join(root, os.path.dirname(path)), exist_ok=True)
            with open(os.path.join(root, path), "w", encoding="utf-8") as handle:
                handle.write("x\n")
        done = subprocess.run(["git", "-C", root, "-c", "core.ignorecase=false", "check-ignore", "--no-index", "-v", "-n", "--stdin"], input="\n".join(paths) + "\n", capture_output=True, text=True)
        result = {}
        for line in done.stdout.splitlines():
            where, path = line.split("\t", 1)
            result[path] = None if where.startswith("::") else int(where.split(":")[1])
        return result
    finally:
        shutil.rmtree(root)


FIXTURE_PATHS = [
    "app/billing/refunds.py", "app/billing/migrations/0042_refunds.sql", "app/api.py", "app/legacy/old.py", "app/b2.py",
    "docs/billing.md", "src/docs/intro.md", "docs/internal/oncall.md", "README.md", "sub/README.md",
    "tools/lint.sh", "tools/ci/run.sh", "config/prod.ini", "Makefile", "scripts/db/seed.sql",
]
NEGATION_TEXT = "*.log\n!keep.log\n/src/[ab]*.py\n/vendor/\ndocs/*\nbuild/**\n**/tmp\n/src/[!a]?.go\n"
NEGATION_PATHS = ["x.log", "keep.log", "logs/keep.log", "src/a1.py", "src/c1.py", "src/b2.go", "src/a2.go", "vendor/x/README.md", "docs/a.md", "docs/sub/b.md", "build/out/bin", "tmp/t", "a/b/tmp/t", "notes.txt"]
SHARED_PATTERNS = ["*", "*.js", "/docs/", "docs/", "apps/", "/apps/github", "**/logs", "docs/**", "src/**/test_*.py", "/build/logs/", "README.md", "a/**/b", "?.txt", "/config/*.ini", "lib/*.py"]
SHARED_PATHS = [
    "a.js", "apps/x.js", "apps/github/y.rb", "src/apps/z.go", "docs/getting-started.md", "docs/build-app/troubleshooting.md",
    "src/docs/intro.md", "build/logs/a.log", "deep/nested/logs/b.txt", "README.md", "pkg/README.md", "a/b", "a/x/y/b", "a/bx/c.txt",
    "src/test_app.py", "src/core/deep/test_core.py", "src/core/app.py", "x.txt", "xy.txt", "config/prod.ini", "config/env/dev.ini", "lib/util.py",
]


class CodeownersTests(unittest.TestCase):
    def setUp(self):
        self.text = read(CODEOWNERS)

    def test_the_last_matching_line_wins_even_when_it_takes_ownership_away(self):
        self.assertEqual(owners_of(self.text, "app/billing/refunds.py"), (6, ["@acme/payments"]))
        self.assertEqual(owners_of(self.text, "app/billing/migrations/0042_refunds.sql"), (7, ["@acme/dba"]))
        self.assertEqual(owners_of(self.text, "app/api.py"), (5, ["@acme/backend"]))
        self.assertEqual(owners_of(self.text, "Makefile"), (2, ["@acme/maintainers"]))
        self.assertEqual(owners_of(self.text, "config/prod.ini"), (8, ["@acme/sre"]))

    def test_a_line_without_owners_leaves_its_paths_unowned(self):
        self.assertEqual(owners_of(self.text, "docs/internal/oncall.md"), (12, []))
        self.assertEqual(owners_of(self.text, "src/docs/intro.md"), (11, ["@acme/docs"]))
        self.assertEqual(owners_of(self.text, "README.md"), (13, ["@acme/docs", "docs-team@example.com"]))

    def test_anchoring_directories_and_double_stars(self):
        cases = [
            ("apps/", "src/apps/z.go", True), ("/apps/", "src/apps/z.go", False), ("/apps/", "apps/x.js", True),
            ("**/logs", "deep/nested/logs/b.txt", True), ("docs/**", "docs/a/b.md", True), ("docs/**", "docs", False),
            ("a/**/b", "a/b", True), ("a/**/b", "a/x/y/b", True), ("src/**/test_*.py", "src/core/deep/test_core.py", True),
            ("?.txt", "x.txt", True), ("?.txt", "xy.txt", False), ("*.js", "lib/deep/a.js", True), ("/README.md", "pkg/README.md", False),
            ("README.md", "pkg/README.md", True), ("/build/logs/", "build/logs/a.log", True), ("/build/logs/", "x/build/logs/a.log", False),
        ]
        for pattern, path, expected in cases:
            with self.subTest(pattern=pattern, path=path):
                self.assertEqual(main.codeowners_matches(main.compile_rule(1, pattern, ["@a"], False), path), expected)

    def test_a_final_lone_star_covers_only_direct_children(self):
        self.assertEqual(owners_of(self.text, "tools/lint.sh"), (16, ["@acme/tooling"]))
        self.assertEqual(owners_of(self.text, "tools/ci/run.sh"), (2, ["@acme/maintainers"]))
        rule = main.compile_rule(1, "/config/*.ini", ["@a"], False)
        self.assertTrue(main.codeowners_matches(rule, "config/prod.ini"))
        self.assertFalse(main.codeowners_matches(rule, "config/env/dev.ini"))

    def test_unsupported_lines_are_skipped_and_reported(self):
        rules, problems = main.parse_codeowners(self.text + "/ops/ team-without-at\n*.py @acme/backend # inline comment @not-an-owner\n")
        self.assertEqual([(p.line, p.reason.split(" ")[0]) for p in problems], [(19, "negation"), (20, "character"), (21, "'team-without-at'")])
        self.assertEqual(rules[-1].owners, ["@acme/backend"])
        self.assertEqual(owners_of(self.text, "app/b2.py"), (5, ["@acme/backend"]))

    def test_codeowners_locations_are_searched_in_githubs_order(self):
        files = {"docs/CODEOWNERS": "* @docs\n", "CODEOWNERS": "* @root\n"}
        self.assertEqual(main.find_codeowners(files.get), ("CODEOWNERS", "* @root\n"))
        files[".github/CODEOWNERS"] = "* @github\n"
        self.assertEqual(main.find_codeowners(files.get)[0], ".github/CODEOWNERS")
        self.assertEqual(main.find_codeowners({}.get), ("", ""))

    @unittest.skipUnless(HAS_GIT, "git is not installed")
    def test_gitignore_evaluation_matches_git_check_ignore_line_for_line(self):
        for text, paths in ((main.patterns_only(self.text), FIXTURE_PATHS), (NEGATION_TEXT, NEGATION_PATHS)):
            rules = main.parse_gitignore(text)
            mine = {path: (lambda r: r.line if r else None)(main.gitignore_rule(rules, path)) for path in paths}
            self.assertEqual(mine, check_ignore(text, paths))

    @unittest.skipUnless(HAS_GIT, "git is not installed")
    def test_shared_patterns_agree_with_git_check_ignore_one_pattern_at_a_time(self):
        for pattern in SHARED_PATTERNS:
            theirs = check_ignore(pattern + "\n", SHARED_PATHS)
            rule = main.compile_rule(1, pattern, ["@a"], False)
            mine = {path: 1 if main.codeowners_matches(rule, path) else None for path in SHARED_PATHS}
            self.assertEqual(mine, theirs, pattern)

    def test_compare_names_each_kind_of_difference(self):
        text = "*.log @ops\n/tools/* @tooling\n!keep.log\n/src/[ab]*.py @core\n/vendor/ @deps\n*.md @docs\n"
        found = {d.path: d for d in main.compare_semantics(text, ["tools/ci/run.sh", "keep.log", "src/a1.py", "vendor/x/README.md", "tools/lint.sh", "x.log"])}
        self.assertEqual(sorted(found), ["keep.log", "src/a1.py", "tools/ci/run.sh", "vendor/x/README.md"])
        self.assertEqual((found["tools/ci/run.sh"].codeowners, found["tools/ci/run.sh"].gitignore), (None, 2))
        self.assertIn("covers only files directly inside", found["tools/ci/run.sh"].why)
        self.assertIn("CODEOWNERS skips it: negation", found["keep.log"].why)
        self.assertIn("character ranges", found["src/a1.py"].why)
        self.assertEqual((found["vendor/x/README.md"].codeowners, found["vendor/x/README.md"].gitignore), (6, 5))
        self.assertIn("stops at the directory", found["vendor/x/README.md"].why)


class ProtectionTests(unittest.TestCase):
    def setUp(self):
        self.policy = main.load_policy(json.loads(read(RULESET)))
        self.rules = main.parse_codeowners(read(CODEOWNERS))[0]
        self.teams = json.loads(read(TEAMS))

    def pr(self, name, **changes):
        data = json.loads(read(os.path.join(FIXTURES, f"pr-{name}.json")))
        data.update(changes)
        return main.pr_from_json(data)

    def codes(self, pr, method="squash", policy=None):
        return [v.code for v in main.evaluate(policy or self.policy, pr, self.rules, self.teams, method)]

    def test_the_ruleset_is_read_from_githubs_rules_shape(self):
        p = self.policy
        self.assertEqual((p.name, p.enforced, p.approvals, p.code_owner_review, p.dismiss_stale), ("protect-main", True, 1, True, True))
        self.assertEqual((p.required_checks, p.linear_history, p.signatures, p.allowed_methods), (["test"], True, False, ["squash", "rebase"]))
        self.assertEqual((p.queue.method, p.queue.concurrency, p.queue.timeout_minutes), ("squash", 3, 10))
        self.assertEqual(p.ref_rules, ["deletion", "non_fast_forward"])
        self.assertEqual(main.load_policy({"rules": [{"type": "workflows"}]}).not_evaluated, ["workflows"])
        with self.assertRaises(main.QueueError):
            main.load_policy({"name": "classic branch protection", "required_status_checks": {}})

    def test_stale_and_self_approvals_do_not_count(self):
        violations = main.evaluate(self.policy, self.pr("412"), self.rules, self.teams, "squash")
        self.assertEqual([v.code for v in violations], ["codeowners", "codeowners"])
        self.assertIn("app/billing/refunds.py needs an approval from @acme/payments (CODEOWNERS line 6: /app/billing/); the approval of raj was dismissed as stale", violations[0].message)
        self.assertIn("docs/billing.md", violations[1].message)
        self.assertEqual(main.review_state(self.pr("412"), self.policy), ({"dan"}, set(), {"raj"}))

    def test_the_latest_decisive_review_of_each_reviewer_counts(self):
        self.assertEqual(self.codes(self.pr("415")), [])
        reviews = [{"user": "raj", "state": "APPROVED", "commit_id": "c0ffee00c0ffee00c0ffee00c0ffee00c0ffee00"}, {"user": "raj", "state": "CHANGES_REQUESTED"}, {"user": "dan", "state": "APPROVED"}]
        self.assertEqual(self.codes(self.pr("415", reviews=reviews)), ["changes-requested", "codeowners"])
        reviews[1]["state"] = "DISMISSED"
        self.assertEqual(self.codes(self.pr("415", reviews=reviews)), ["codeowners"])

    def test_required_checks_skipped_missing_and_failing(self):
        self.assertEqual(self.codes(self.pr("415", checks={"test": "neutral", "lint": "failure"})), [])
        missing = main.evaluate(self.policy, self.pr("415", checks={}), self.rules, self.teams, "squash")
        self.assertEqual([v.code for v in missing], ["checks"])
        self.assertIn("expected, no status reported", missing[0].message)
        self.assertEqual(self.codes(self.pr("415", checks={"test": "failure"})), ["checks"])

    def test_merge_method_linear_history_signatures_strict_and_last_push(self):
        self.assertEqual(self.codes(self.pr("415"), method="merge"), ["merge-method", "linear-history"])
        strict = main.load_policy({"rules": [
            {"type": "required_signatures"},
            {"type": "required_status_checks", "parameters": {"strict_required_status_checks_policy": True, "required_status_checks": []}},
            {"type": "pull_request", "parameters": {"require_last_push_approval": True, "dismiss_stale_reviews_on_push": True}},
        ]})
        pr = self.pr("412", last_pusher="dan")
        self.assertEqual(self.codes(pr, policy=strict), ["last-push", "up-to-date", "signatures"])
        self.assertIn("9f1c2d3", main.evaluate(strict, pr)[-1].message)

    def test_evaluate_mode_reports_but_does_not_block(self):
        data = json.loads(read(RULESET))
        data["enforcement"] = "evaluate"
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "ruleset.json")
            with open(path, "w", encoding="utf-8") as handle:
                json.dump(data, handle)
            code, out, _ = run(["protect", "--ruleset", path, "--pr", os.path.join(FIXTURES, "pr-412.json"), "--codeowners", CODEOWNERS, "--teams", TEAMS])
        self.assertEqual(code, 0)
        self.assertIn("412: mergeable", out)
        self.assertIn("enforcement is not active", out)
        code, out, _ = run(["protect", "--ruleset", RULESET, "--pr", os.path.join(FIXTURES, "pr-412.json"), "--codeowners", CODEOWNERS, "--teams", TEAMS, "--json"])
        self.assertEqual((code, json.loads(out)["mergeable"]), (1, False))


class FakeBackend:
    def __init__(self, touches=None, broken=(), clashes=(), move_once=False):
        self.touches = touches or {}
        self.broken = set(broken)
        self.clashes = [set(pair) for pair in clashes]
        self.base = "main"
        self.move_once = move_once
        self.landed = []

    def tip(self):
        return self.base

    def build(self, parent, name):
        inside = parent.split("+")[1:]
        clash = [other for other in inside if self.touches.get(other, set()) & self.touches.get(name, set())]
        return main.Build(None, f"conflict with {clash[0]}") if clash else main.Build(parent + "+" + name)

    def test(self, commit, names):
        inside = set(commit.split("+")[1:])
        bad = sorted(inside & self.broken) or sorted(n for pair in self.clashes if pair <= inside for n in pair)
        return main.Check(not bad, "broken by " + ",".join(bad) if bad else "ok")

    def land(self, old, new):
        if self.move_once:
            self.move_once = False
            self.base += "+hotfix"
            raise main.BaseMoved("cannot lock ref 'refs/heads/main'")
        assert old == self.base, (old, self.base)
        self.base = new
        self.landed.append(new)


def demo_backend():
    return FakeBackend(touches={"T60": {"settings:2"}, "T45": {"settings:2"}}, broken={"B"}, clashes=[("R", "I")])


DEMO_ORDER = ["R", "D", "I", "T60", "T45", "B", "Rd"]


class QueueEngineTests(unittest.TestCase):
    def test_a_serial_queue_tests_each_change_on_top_of_the_ones_before(self):
        backend = demo_backend()
        report = main.run_queue(DEMO_ORDER, backend, concurrency=1)
        self.assertEqual(report.merged, ["R", "D", "T60", "Rd"])
        self.assertEqual(sorted(report.ejected), ["B", "I", "T45"])
        self.assertTrue(report.ejected["I"].startswith("check failed"))
        self.assertEqual((report.rounds, report.runs), (6, 6))
        self.assertEqual(backend.base, "main+R+D+T60+Rd")

    def test_a_speculative_window_needs_fewer_rounds_and_rebuilds_behind_a_failure(self):
        backend = demo_backend()
        report = main.run_queue(DEMO_ORDER, backend, concurrency=3)
        self.assertEqual(report.merged, ["R", "D", "T60", "Rd"])
        self.assertEqual((report.rounds, report.runs), (3, 7))
        builds = [e["candidate"] for e in report.events if e["event"] == "build"]
        self.assertIn("main+R+D+T60+B+Rd", builds)
        self.assertIn("main+R+D+T60+Rd", builds)
        self.assertEqual(backend.base, "main+R+D+T60+Rd")

    def test_a_textual_conflict_is_ejected_at_build_without_a_ci_run(self):
        report = main.run_queue(["T60", "T45"], demo_backend(), concurrency=2)
        self.assertEqual((report.merged, report.ejected, report.runs), (["T60"], {"T45": "conflict with T60"}, 1))

    def test_a_failing_batch_is_split_until_the_culprit_is_alone(self):
        report = main.run_queue(DEMO_ORDER, demo_backend(), concurrency=1, batch=4)
        self.assertEqual(report.merged, ["R", "D", "T60", "Rd"])
        self.assertEqual((report.rounds, report.runs), (8, 8))
        splits = [e["into"] for e in report.events if e["event"] == "split"]
        self.assertEqual(splits[0], [["R", "D"], ["I", "T60"]])
        clean = main.run_queue(["R", "D", "T60", "Rd"], demo_backend(), concurrency=1, batch=4)
        self.assertEqual((clean.rounds, clean.runs, clean.merged), (1, 1, ["R", "D", "T60", "Rd"]))

    def test_a_moved_base_is_rebuilt_not_overwritten(self):
        backend = FakeBackend(move_once=True)
        report = main.run_queue(["A", "B"], backend, concurrency=2)
        self.assertEqual(report.merged, ["A", "B"])
        self.assertEqual(backend.base, "main+hotfix+A+B")
        self.assertIn("rebuild", [e["event"] for e in report.events])

    def test_concurrency_and_batch_must_be_positive(self):
        with self.assertRaises(main.QueueError):
            main.run_queue(["A"], FakeBackend(), concurrency=0)


@unittest.skipUnless(HAS_MERGE_TREE, "needs git 2.40 or later")
class GitQueueTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp)
        self.paths = main.build_demo(self.tmp)
        self.origin = self.paths["origin"]
        self.start = git(self.origin, "rev-parse", "main").strip()

    def queue(self, *extra, branches=QUEUE):
        return run(["queue", "--repo", self.origin, "--prs", self.paths["prs"], "--teams", self.paths["teams"], "--check", f"{sys.executable} check.py", "--json", *extra, *branches])

    def oracle_tree(self, names):
        clone = os.path.join(self.tmp, "oracle")
        subprocess.run(["git", "clone", "--quiet", self.origin, clone], check=True, capture_output=True)
        git(clone, "checkout", "--quiet", "--detach", self.start)
        for name in names:
            git(clone, "merge", "--quiet", "--no-edit", f"origin/{name}")
        return git(clone, "rev-parse", "HEAD^{tree}").strip()

    def test_the_queue_lands_the_passing_branches_and_ejects_the_rest(self):
        code, out, err = self.queue("--ruleset", self.paths["ruleset"])
        self.assertEqual(code, 1, err)
        report = json.loads(out)
        self.assertEqual((report["method"], report["concurrency"]), ("squash", 3))
        self.assertEqual(report["merged"], PASSING)
        self.assertEqual(sorted(report["ejected"]), ["bad-tax", "invoice", "timeout-45"])
        self.assertIn("has no attribute 'total'", report["ejected"]["invoice"])
        self.assertEqual(report["ejected"]["timeout-45"], "conflict in config/settings.ini")
        self.assertEqual(sorted(report["blocked"]), ["stale-approval", "workers-8"])
        self.assertTrue(report["blocked"]["workers-8"][0].startswith("codeowners: config/settings.ini needs an approval from @acme/sre"))
        self.assertEqual((report["rounds"], report["runs"]), (3, 7))
        self.assertEqual(git(self.origin, "rev-parse", "main^{tree}").strip(), self.oracle_tree(PASSING))
        self.assertEqual(git(self.origin, "rev-list", "--merges", "--count", "main").strip(), "0")
        subjects = git(self.origin, "log", "--reverse", "--format=%s", f"{self.start}..main").splitlines()
        self.assertEqual([re.search(r"\(([^)]+)\)$", s).group(1) for s in subjects], PASSING)
        self.assertEqual(git(self.origin, "for-each-ref", "refs/mergequeue"), "")
        self.assertEqual(len(git(self.origin, "worktree", "list").splitlines()), 1)

    def test_merge_and_rebase_methods_give_the_same_tree_with_different_histories(self):
        code, out, err = self.queue("--method", "merge", "--concurrency", "2", branches=PASSING + ["invoice"])
        self.assertEqual(code, 1, err)
        merged_tree = git(self.origin, "rev-parse", "main^{tree}").strip()
        self.assertEqual(git(self.origin, "rev-list", "--merges", "--count", f"{self.start}..main").strip(), "4")
        self.assertEqual(git(self.origin, "rev-list", "--first-parent", "--count", f"{self.start}..main").strip(), "4")
        git(self.origin, "update-ref", "refs/heads/main", self.start)
        code, out, err = self.queue("--method", "rebase", "--batch", "2", branches=PASSING + ["stale-approval"])
        self.assertEqual((code, json.loads(out)["merged"]), (0, PASSING + ["stale-approval"]), err)
        self.assertEqual(git(self.origin, "rev-list", "--merges", "--count", "main").strip(), "0")
        self.assertEqual(git(self.origin, "rev-list", "--count", f"{self.start}..main").strip(), "6")
        authors = git(self.origin, "log", "--format=%an %cn", f"{self.start}..main").splitlines()
        self.assertEqual(authors[0], "cy mergequeue")
        self.assertEqual(self.oracle_tree(PASSING), merged_tree)

    def test_dry_run_leaves_the_base_alone_and_a_checked_out_base_is_refused(self):
        code, out, _ = self.queue("--dry-run", branches=PASSING)
        self.assertEqual((code, json.loads(out)["merged"]), (0, PASSING))
        self.assertEqual(git(self.origin, "rev-parse", "main").strip(), self.start)
        work = self.paths["work"]
        git(work, "checkout", "--quiet", "main")
        code, _, err = run(["queue", "--repo", work, "--check", "true", "readme"])
        self.assertEqual(code, 2)
        self.assertIn("is checked out", err)
        code, _, err = run(["queue", "--repo", self.origin, "no-such-branch"])
        self.assertEqual(code, 2)
        self.assertIn("no branch 'no-such-branch'", err)

    def test_a_rebase_merge_drops_commits_that_are_already_on_the_base(self):
        stack = os.path.join(self.tmp, "stack")
        subprocess.run(["git", "-c", "init.defaultBranch=main", "init", "--quiet", stack], check=True)
        for branch, start, name in (("main", None, "base.txt"), ("part1", "main", "one.txt"), ("part2", "part1", "two.txt")):
            if start:
                git(stack, "checkout", "--quiet", "-b", branch, start)
            with open(os.path.join(stack, name), "w", encoding="utf-8") as handle:
                handle.write(name + "\n")
            git(stack, "add", name)
            git(stack, "commit", "--quiet", "-m", f"add {name}")
        git(stack, "checkout", "--quiet", "--detach", "main")
        code, out, err = run(["queue", "--repo", stack, "--method", "rebase", "--json", "part1", "part2"])
        self.assertEqual((code, json.loads(out)["merged"]), (0, ["part1", "part2"]), err)
        self.assertEqual(git(stack, "log", "--format=%s", "main").splitlines(), ["add two.txt", "add one.txt", "add base.txt"])

    def test_pull_request_facts_come_from_git(self):
        repo = main.GitRepo(self.origin)
        pr = main.pr_from_git(repo, self.start, "stale-approval", {"author": "cy"})
        self.assertEqual((pr.author, pr.files, len(pr.commits), pr.up_to_date), ("cy", ["app/discount.py"], 2, True))
        self.assertEqual(pr.head, git(self.origin, "rev-parse", "stale-approval").strip())
        self.assertFalse(any(c.signed for c in pr.commits))


class SimulatorTests(unittest.TestCase):
    def sim(self, **kwargs):
        return main.simulate(main.SimConfig(**kwargs))

    def test_conflicts_grow_with_branch_lifetime_and_team_size(self):
        short, long = self.sim(developers=8, branch_hours=4), self.sim(developers=8, branch_hours=40)
        self.assertLess(short.conflict_rate, 0.1)
        self.assertGreater(long.conflict_rate, short.conflict_rate + 0.25)
        self.assertGreater(long.rework_share, short.rework_share)
        small, large = self.sim(developers=4, branch_hours=16), self.sim(developers=30, branch_hours=16, concurrency=4)
        self.assertGreater(large.conflict_rate, small.conflict_rate + 0.25)

    def test_a_serial_queue_saturates_and_a_speculative_window_fixes_it(self):
        serial, parallel = self.sim(developers=30, branch_hours=4), self.sim(developers=30, branch_hours=4, concurrency=4)
        self.assertGreater(serial.load, 1)
        self.assertLess(parallel.load, 1)
        self.assertGreater(serial.wait_p50_minutes, 20 * parallel.wait_p50_minutes)
        self.assertGreater(serial.conflict_rate, parallel.conflict_rate)
        self.assertGreater(parallel.ci_runs_per_pr, 1.0)
        self.assertEqual(serial.ci_runs_per_pr, 1.0)

    def test_queue_times_restart_the_window_behind_a_failure(self):
        outcome, runs = main.queue_times([(0.0, 0, True), (0.0, 1, False), (0.0, 2, False)], 1.0, 3)
        self.assertEqual(outcome, {0: (1.0, False), 1: (2.0, True), 2: (2.0, True)})
        self.assertEqual(runs, 5)

    def test_the_simulation_is_deterministic_and_the_cli_prints_json(self):
        self.assertEqual(self.sim(seed=3), self.sim(seed=3))
        code, out, _ = run(["simulate", "--developers", "4", "--branch-hours", "4,40", "--json"])
        rows = json.loads(out)
        self.assertEqual((code, [r["branch_hours"] for r in rows]), (0, [4.0, 40.0]))
        self.assertEqual(run(["simulate", "--developers", "four"])[0], 2)


class CliTests(unittest.TestCase):
    def test_owners_json_and_strict_exit_codes(self):
        code, out, _ = run(["owners", "--codeowners", CODEOWNERS, "--json", "app/billing/refunds.py", "docs/internal/oncall.md"])
        data = json.loads(out)
        self.assertEqual(code, 0)
        self.assertEqual(data["paths"]["app/billing/refunds.py"], {"line": 6, "pattern": "/app/billing/", "owners": ["@acme/payments"]})
        self.assertEqual(data["paths"]["docs/internal/oncall.md"]["owners"], [])
        self.assertEqual([p["line"] for p in data["skipped"]], [19, 20])
        self.assertEqual(run(["owners", "--codeowners", CODEOWNERS, "--strict", "app/api.py"])[0], 1)

    def test_owners_gitignore_mode_prints_check_ignore_lines(self):
        code, out, _ = run(["owners", "--codeowners", CODEOWNERS, "--gitignore", "tools/ci/run.sh", "x/y"])
        self.assertEqual(code, 0)
        self.assertEqual(out.splitlines(), [f"{CODEOWNERS}:2:*\ttools/ci/run.sh", f"{CODEOWNERS}:2:*\tx/y"])

    def test_bad_input_exits_2_with_a_message(self):
        with tempfile.TemporaryDirectory() as tmp:
            bad = os.path.join(tmp, "bad.json")
            with open(bad, "w", encoding="utf-8") as handle:
                handle.write("{not json")
            code, _, err = run(["protect", "--ruleset", bad, "--pr", bad])
            self.assertEqual(code, 2)
            self.assertIn("is not valid JSON", err)
            code, _, err = run(["owners", "--codeowners", os.path.join(tmp, "missing"), "a"])
            self.assertEqual(code, 2)
            self.assertIn("cannot read", err)


if __name__ == "__main__":
    unittest.main()
