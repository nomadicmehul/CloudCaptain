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
HISTORY = os.path.join(CODE, "fixtures", "history.json")
sys.path.insert(0, CODE)

import main  # noqa: E402

GIT = shutil.which("git")


def graph():
    return main.read_graph(HISTORY)


def has_bug(commits, bug):
    return lambda sha: 1 if bug in main.ancestors(commits, [sha]) else 0


def history(spec, files):
    commits = {name: main.Commit(name, tuple(parents), date, name, author=f"author-{name}") for name, (parents, date) in spec.items()}
    return main.MemoryHistory(commits, {name: {"f.py": text} for name, text in files.items()})


def blame(hist, rev="HEAD", **options):
    return [(line.commit, line.orig_line, line.ignored, line.unblamable) for line in main.Blame(hist, rev, "f.py", **options).run()]


def run(argv, runner=main.subprocess_runner):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = main.main(argv, runner)
    return code, out.getvalue(), err.getvalue()


class BisectTests(unittest.TestCase):
    def test_candidates_are_ancestors_of_bad_minus_ancestors_of_good(self):
        step = main.bisect_step(graph(), "H", ["A"])
        self.assertEqual(step.candidates, 14)
        self.assertNotIn("A", step.distances)
        step = main.bisect_step(graph(), "H", ["A", "D"])
        self.assertEqual(sorted(step.distances), ["E", "F", "G", "H", "M1", "M2", "X1", "X2", "X3", "Y1", "Y2"])

    def test_weights_count_reachable_candidates_not_the_length_of_a_path(self):
        commits = graph()
        order = [sha for sha in main.date_order(commits, ["H", "A"]) if sha != "A"][::-1]
        _, weight = main.find_bisection(commits, order, True)
        self.assertEqual(weight["M1"], 7)
        self.assertEqual(weight["M2"], 12)
        self.assertEqual(weight["H"], 14)
        for sha in order:
            self.assertEqual(weight[sha], len(main.ancestors(commits, [sha]) - {"A"}))

    def test_the_first_commit_to_test_is_the_merge_that_splits_the_graph_in_half(self):
        step = main.bisect_step(graph(), "H", ["A"])
        self.assertEqual((step.state, step.commit, step.reaches, step.remaining, step.steps), ("test", "M1", 7, 6, 3))
        self.assertEqual(step.distances["M1"], 7)
        self.assertEqual(max(step.distances.values()), 7)

    def test_a_run_finds_a_bug_planted_on_a_side_branch(self):
        commits = graph()
        step, log = main.bisect_run(commits, "H", ["A"], has_bug(commits, "X2"))
        self.assertEqual((step.state, step.commit), ("found", "X2"))
        self.assertEqual([(t.commit, t.verdict) for t in log], [("M1", "bad"), ("X2", "bad"), ("X1", "good")])
        self.assertLessEqual(len(log), main.estimate_steps(14) + 1)

    def test_a_skipped_commit_is_replaced_by_the_one_git_would_pick(self):
        commits = graph()
        self.assertEqual(main.bisect_step(commits, "H", ["A"], ["M1"]).commit, "X2")
        self.assertEqual(main.bisect_step(commits, "H", ["A"], ["M1", "E"]).commit, "F")
        test = has_bug(commits, "X2")
        step, log = main.bisect_run(commits, "H", ["A"], lambda sha: 125 if sha in ("M1", "X3") else test(sha))
        self.assertEqual(step.commit, "X2")
        self.assertIn(("M1", "skip"), [(t.commit, t.verdict) for t in log])

    def test_only_skipped_commits_left_lists_every_possible_first_bad_commit(self):
        step = main.bisect_step(graph(), "X3", ["X1"], ["X2"])
        self.assertEqual((step.state, step.suspects), ("only-skipped", ["X2", "X3"]))
        self.assertIn("There are only 'skip'ped commits left to test.", main.describe(step, graph()))

    def test_exit_codes_decide_good_bad_skip_or_abort(self):
        self.assertEqual([main.verdict_of(code) for code in (0, 1, 2, 124, 125, 126, 127)], ["good", "bad", "bad", "bad", "skip", "bad", "bad"])
        for code in (128, 255, -9):
            with self.assertRaisesRegex(main.BisectError, "< 0 or >= 128"):
                main.verdict_of(code)

    def test_a_command_that_cannot_run_on_a_good_commit_either_stops_the_run(self):
        with self.assertRaisesRegex(main.BisectError, "bogus exit code 127"):
            main.bisect_run(graph(), "H", ["A"], lambda sha: 127)
        commits = graph()
        step, log = main.bisect_run(commits, "H", ["A"], lambda sha: 0 if sha == "A" else 126 if "X2" in main.ancestors(commits, [sha]) else 0)
        self.assertEqual(step.commit, "X2")

    def test_prng_integer_sqrt_and_step_estimate_match_git(self):
        self.assertEqual([main.get_prn(n) for n in (1, 5, 13)], [16838, 18655, main.get_prn(13)])
        self.assertEqual([main.sqrti(n) for n in (0, 1, 2, 4, 15, 16, 25, 100, 32768)], [0, 1, 1, 2, 3, 4, 5, 10, 181])
        self.assertEqual([main.estimate_steps(n) for n in range(1, 16)], [0, 0, 1, 1, 1, 2, 2, 2, 2, 2, 3, 3, 3, 3, 3])

    def test_good_that_is_not_an_ancestor_makes_git_test_the_merge_base_first(self):
        step = main.bisect_step(graph(), "X3", ["C"], verify_bases=True)
        self.assertEqual((step.state, step.commit), ("merge-base", "B"))
        self.assertEqual(main.bisect_step(graph(), "X3", ["C"]).state, "test")
        with self.assertRaisesRegex(main.BisectError, "merge base B is bad"):
            main.bisect_step(graph(), "B", ["X1"], verify_bases=True)
        with self.assertRaisesRegex(main.BisectError, "was both good and bad"):
            main.bisect_step(graph(), "B", ["X1"])


class BlameTests(unittest.TestCase):
    def test_unchanged_lines_go_to_the_parent_and_changed_lines_stay(self):
        hist = history({"A": ([], 1), "B": (["A"], 2), "HEAD": (["B"], 3)}, {"A": "a\nb\nc\n", "B": "a\nB\nc\nd\n", "HEAD": "x\na\nB\nc\nd\n"})
        self.assertEqual([(c, n) for c, n, _, _ in blame(hist)], [("HEAD", 1), ("A", 1), ("B", 2), ("A", 3), ("B", 4)])

    def test_a_merge_passes_each_line_to_the_parent_that_has_it(self):
        spec = {"A": ([], 1), "L": (["A"], 2), "R": (["A"], 3), "HEAD": (["L", "R"], 4)}
        files = {"A": "1\n2\n3\n", "L": "1\nleft\n3\n", "R": "1\n2\n3\nright\n", "HEAD": "1\nleft\n3\nright\nmerge\n"}
        self.assertEqual([c for c, _, _, _ in blame(history(spec, files))], ["A", "L", "A", "R", "HEAD"])

    def test_a_parent_with_the_identical_file_takes_every_line(self):
        spec = {"A": ([], 1), "L": (["A"], 2), "R": (["A"], 3), "HEAD": (["L", "R"], 4)}
        files = {"A": "1\n2\n", "L": "1\nleft\n", "R": "1\n2\nright\n", "HEAD": "1\n2\nright\n"}
        self.assertEqual([c for c, _, _, _ in blame(history(spec, files))], ["A", "A", "R"])

    def test_an_ignored_commit_hands_reformatted_lines_to_the_lines_they_replaced(self):
        spec = {"A": ([], 1), "B": (["A"], 2), "FMT": (["B"], 3), "HEAD": (["FMT"], 4)}
        files = {"A": "x=1\ny=2\nz=3\n", "B": "x=1\ny=20\nz=3\n", "FMT": "x = 1\ny = 20\nz = 3\n", "HEAD": "x = 1\ny = 20\nz = 3\nw = 4\n"}
        hist = history(spec, files)
        self.assertEqual([c for c, _, _, _ in blame(hist)], ["FMT", "FMT", "FMT", "HEAD"])
        self.assertEqual(blame(hist, ignore=["FMT"]), [("A", 1, True, False), ("B", 2, True, False), ("A", 3, True, False), ("HEAD", 4, False, False)])

    def test_a_line_an_ignored_commit_added_from_nothing_is_unblamable(self):
        spec = {"A": ([], 1), "FMT": (["A"], 2)}
        files = {"A": "def f():\n    return compute_total(x)\n", "FMT": "\ndef f():\n    return compute_total(x)\n"}
        self.assertEqual(blame(history(spec, files), "FMT", ignore=["FMT"]), [("FMT", 1, False, True), ("A", 1, False, False), ("A", 2, False, False)])

    def test_moved_blocks_are_followed_with_M_when_they_carry_enough_text(self):
        block = ["def connect(host, port):\n", "    return open_socket(host, port)\n"]
        rest = [f"value_{n} = {n}\n" for n in range(6)]
        spec = {"A": ([], 1), "MOVE": (["A"], 2)}
        files = {"A": "".join(block + rest), "MOVE": "".join(rest + block)}
        plain = blame(history(spec, files), "MOVE")
        moved = blame(history(spec, files), "MOVE", moves=True)
        self.assertEqual([c for c, _, _, _ in plain], ["A"] * 6 + ["MOVE"] * 2)
        self.assertEqual([(c, n) for c, n, _, _ in moved], [("A", n) for n in range(3, 9)] + [("A", 1), ("A", 2)])
        short = {"A": "a\nb\n" + "".join(rest), "MOVE": "".join(rest) + "a\nb\n"}
        self.assertEqual([c for c, _, _, _ in blame(history(spec, short), "MOVE", moves=True)][-2:], ["MOVE", "MOVE"])

    def test_fingerprints_are_lower_cased_character_pairs_as_in_git(self):
        self.assertEqual(sum(main.fingerprint("Darth   Radar").values()), 12)
        self.assertEqual(main.similarity(main.fingerprint("cat mat"), main.fingerprint("father rather")), 2)
        self.assertEqual(main.similarity(main.fingerprint("tim"), main.fingerprint("mit")), 0)
        self.assertEqual(main.fingerprint("ABC"), main.fingerprint("abc"))

    def test_ignore_revs_file_takes_full_names_and_comments(self):
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, ".git-blame-ignore-revs")
            with open(path, "w") as handle:
                handle.write("# Format with black\n" + "a" * 40 + "\n\n" + "b" * 40 + "  # isort\n")
            self.assertEqual(main.read_ignore_revs(path), ["a" * 40, "b" * 40])
            with open(path, "a") as handle:
                handle.write("HEAD~3\n")
            with self.assertRaisesRegex(main.BisectError, ":5: 'HEAD~3' is not a full object name"):
                main.read_ignore_revs(path)

    def test_myers_diff_finds_a_shortest_edit_script(self):
        changes = main.diff(list("ABCABBA"), list("CBABAC"))
        self.assertEqual(sum(c.old_count + c.new_count for c in changes), 5)


class PickaxeTests(unittest.TestCase):
    def setUp(self):
        spec = {"A": ([], 1), "B": (["A"], 2), "C": (["B"], 3), "D": (["A"], 4), "M": (["C", "D"], 5)}
        files = {"A": "timeout = 30\nretries = 3\n", "B": "retries = 3\ntimeout = 30\n", "C": "retries = 3\ntimeout = 10\n",
                 "D": "timeout = 30\nretries = 3\nsecret = 1\n", "M": "retries = 3\ntimeout = 10\nsecret = 1\n"}
        self.hist = history(spec, files)
        self.order = main.date_order(self.hist.commits, ["M"])

    def found(self, **kind):
        return [sha for sha, _ in main.pickaxe(self.hist, self.order, **kind)]

    def test_S_counts_occurrences_so_moving_a_line_is_not_a_match(self):
        self.assertEqual(self.found(needle="timeout = 30"), ["C", "A"])
        self.assertEqual(self.found(needle="secret"), ["D"])

    def test_G_matches_added_or_removed_lines_even_when_the_count_is_unchanged(self):
        self.assertEqual(self.found(pattern=r"timeout = \d+"), ["C", "B", "A"])

    def test_merges_are_not_searched(self):
        self.assertNotIn("M", self.found(pattern="secret|timeout"))


class LostTests(unittest.TestCase):
    def test_lost_tips_and_dangling_commits_follow_the_fsck_rules(self):
        commits = {sha: main.Commit(sha, parents, n) for n, (sha, parents) in enumerate([("a", ()), ("b", ("a",)), ("x1", ("b",)), ("x2", ("x1",)), ("w", ("b", "i")), ("i", ("b",)), ("o", ())])}
        reflog = [main.ReflogEntry("HEAD", 0, "x2", "b", "checkout: moving from x to main")]
        found = {item["commit"]: item for item in main.lost_commits(commits, ["b"], reflog)}
        self.assertEqual(sorted(found), ["o", "w", "x2"])
        self.assertEqual({sha for sha, item in found.items() if item["dangling"]}, {"o", "w"})
        self.assertEqual(found["x2"]["reflog"], ["HEAD@{0}: checkout: moving from x to main"])

    def test_reflog_entries_are_numbered_from_the_newest(self):
        zero, one, two = "0" * 40, "1" * 40, "2" * 40
        text = f"{zero} {one} Ada <ada@example.invalid> 1767600000 +0100\tcommit (initial): base\n{one} {two} Ada <ada@example.invalid> 1767600060 +0100\tcommit: next\n"
        entries = main.parse_reflog("HEAD", text)
        self.assertEqual([(e.name, e.new, e.message) for e in entries], [("HEAD@{1}", one, "commit (initial): base"), ("HEAD@{0}", two, "commit: next")])


class CliTests(unittest.TestCase):
    def test_next_prints_the_commit_to_test_like_git(self):
        code, out, _ = run(["next", "--graph", HISTORY, "--bad", "H", "--good", "A"])
        self.assertEqual(code, 0)
        self.assertEqual(out, "Bisecting: 6 revisions left to test after this (roughly 3 steps)\n[M1] Merge branch 'retry'\n")
        code, out, _ = run(["next", "--graph", HISTORY, "--bad", "X2", "--good", "X1"])
        self.assertEqual(out.splitlines()[0], "X2 is the first bad commit")

    def test_next_json_and_all(self):
        code, out, _ = run(["next", "--graph", HISTORY, "--bad", "H", "--good", "A", "--skip", "M1", "--json"])
        data = json.loads(out)
        self.assertEqual((code, data["state"], data["commit"], data["candidates"]), (0, "test", "X2", 14))
        code, out, _ = run(["next", "--graph", HISTORY, "--bad", "H", "--good", "A", "--all"])
        self.assertEqual(out.splitlines()[:2], ["M1 (dist=7)", "E (dist=6)"])

    def test_errors_exit_2_with_a_message(self):
        code, _, err = run(["next", "--graph", HISTORY, "--bad", "NOPE", "--good", "A"])
        self.assertEqual((code, err.strip()), (2, "minibisect: NOPE is not a commit in this history"))
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as handle:
            handle.write("{broken")
        self.addCleanup(os.unlink, handle.name)
        code, _, err = run(["next", "--graph", handle.name, "--bad", "H", "--good", "A"])
        self.assertEqual(code, 2)
        self.assertIn("is not valid JSON", err)
        code, _, err = run(["run", "--graph", HISTORY, "--bad", "H", "--good", "A", "--", "true"])
        self.assertEqual(code, 2)


@unittest.skipUnless(GIT, "git is not installed")
class GitOracleTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.repo = os.path.join(self.dir, "repo")
        self.clock = 1767600000
        self.git("init", "-q", self.repo, cwd=self.dir)

    def git(self, *args, who="Ada", cwd=None):
        env = dict(os.environ, GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1", GIT_AUTHOR_NAME=who, GIT_AUTHOR_EMAIL=f"{who.lower()}@example.invalid",
                   GIT_COMMITTER_NAME=who, GIT_COMMITTER_EMAIL=f"{who.lower()}@example.invalid", GIT_AUTHOR_DATE=f"@{self.clock} +0100", GIT_COMMITTER_DATE=f"@{self.clock} +0100")
        done = subprocess.run(["git", "-c", "init.defaultBranch=main", "-c", "advice.detachedHead=false", *args], cwd=cwd or self.repo, env=env,
                              capture_output=True, text=True, stdin=subprocess.DEVNULL)
        if done.returncode:
            self.fail(f"git {args}: {done.stderr}")
        return done.stdout.strip()

    def commit(self, message, text, who="Ada", path="app.py"):
        self.clock += 60
        with open(os.path.join(self.repo, path), "w") as handle:
            handle.write(text)
        self.git("add", path, who=who)
        self.git("commit", "-q", "-m", message, who=who)
        return self.git("rev-parse", "HEAD")

    def history(self, bug_at=4):
        shas = [self.commit(f"main {n}", f"THRESHOLD = {7 if n >= bug_at else 10}\nn = {n}\n") for n in range(8)]
        self.git("checkout", "-q", "-b", "side", shas[2])
        side = self.commit("side 1", "side = 1\n", path="side.py")
        self.git("checkout", "-q", "main")
        self.clock += 60
        self.git("merge", "-q", "--no-ff", "-m", "Merge side", "side")
        return shas, side

    def test_next_equals_git_rev_list_bisect_vars(self):
        shas, side = self.history()
        for bad, goods in (("main", [shas[0]]), ("main", [shas[1], side]), (shas[6], [shas[0]]), ("main", [shas[5]])):
            vars_out = self.git("rev-list", "--bisect-vars", bad, *[f"^{g}" for g in goods])
            expected = {k: v.strip("'") for k, v in (line.split("=", 1) for line in vars_out.splitlines())}
            code, out, err = run(["next", "--repo", self.repo, "--bad", bad, *[a for g in goods for a in ("--good", g)], "--json"])
            data = json.loads(out)
            self.assertEqual(code, 0, err)
            self.assertEqual(data["commit"], expected["bisect_rev"])
            if data["state"] == "test":
                self.assertEqual((str(data["candidates"]), str(data["steps"]), str(data["remaining"])), (expected["bisect_all"], expected["bisect_steps"], expected["bisect_good"]))

    def test_run_checks_out_commits_restores_the_branch_and_names_the_bug(self):
        shas, _ = self.history(bug_at=5)
        script = os.path.join(self.dir, "test.sh")
        with open(script, "w") as handle:
            handle.write("grep -q 'THRESHOLD = 10' app.py\n")
        code, out, err = run(["run", "--repo", self.repo, "--bad", "main", "--good", shas[0], "--json", "--", "sh", script])
        data = json.loads(out)
        self.assertEqual((code, data["state"], data["commit"]), (0, "found", shas[5]), err)
        self.assertEqual(self.git("rev-parse", "--abbrev-ref", "HEAD"), "main")

    def test_blame_equals_git_blame_porcelain_with_and_without_ignored_revisions(self):
        self.commit("start", "TIMEOUT=30\nRETRIES=3\ndef run():\n    return go(TIMEOUT)\n", who="Ada")
        self.commit("retries", "TIMEOUT=30\nRETRIES=5\ndef run():\n    return go(TIMEOUT)\n", who="Grace")
        fmt = self.commit("format", "TIMEOUT = 30\nRETRIES = 5\n\ndef run():\n    return go(TIMEOUT)\n", who="Margaret")
        self.commit("timeout", "TIMEOUT = 10\nRETRIES = 5\n\ndef run():\n    return go(TIMEOUT)\n", who="Linus")
        for extra in ([], ["--ignore-rev", fmt]):
            porcelain = self.git("-c", "blame.markIgnoredLines=true", "-c", "blame.markUnblamableLines=true", "blame", "--line-porcelain", *extra, "app.py")
            theirs, flags = [], []
            for line in porcelain.splitlines():
                match = re.match(r"^([0-9a-f]{40}) (\d+) (\d+)", line)
                if match:
                    theirs.append((match[1], int(match[2])))
                    flags.append(set())
                elif line in ("ignored", "unblamable"):
                    flags[-1].add(line)
            code, out, err = run(["blame", "--repo", self.repo, "--json", *extra, "app.py"])
            mine = json.loads(out)["lines"]
            self.assertEqual(theirs, [(b["commit"], b["orig_line"]) for b in mine], extra)
            self.assertEqual(flags, [{k for k in ("ignored", "unblamable") if b[k]} for b in mine], extra)

    def test_lost_finds_the_tip_of_a_deleted_branch_through_the_reflog(self):
        self.commit("base", "a\n")
        self.git("checkout", "-q", "-b", "experiment")
        tip = self.commit("experiment", "b\n")
        self.git("checkout", "-q", "main")
        self.git("branch", "-D", "experiment")
        code, out, err = run(["lost", "--repo", self.repo, "--json"])
        found = json.loads(out)
        self.assertEqual(code, 0, err)
        self.assertEqual([item["commit"] for item in found], [tip])
        self.assertFalse(found[0]["dangling"])
        self.assertTrue(any("checkout: moving from experiment to main" in m for m in found[0]["reflog"]))
        dangling = self.git("fsck", "--no-reflogs")
        self.assertIn(f"dangling commit {tip}", dangling)


if __name__ == "__main__":
    unittest.main()
