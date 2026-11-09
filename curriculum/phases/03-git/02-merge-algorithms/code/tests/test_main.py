import contextlib
import io
import json
import os
import random
import shutil
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
CODE = os.path.dirname(HERE)
FIXTURES = os.path.join(CODE, "fixtures")
sys.path.insert(0, CODE)

import main  # noqa: E402

GIT = shutil.which("git")
S = main.split_lines


def fixture(*parts):
    return S(main.read_text(os.path.join(FIXTURES, *parts)))


def apply(old, new, changes):
    out, position = [], 0
    for change in changes:
        out.extend(old[position:change.old_start])
        out.extend(new[change.new_start:change.new_start + change.new_count])
        position = change.old_start + change.old_count
    return out + list(old[position:])


def changed_lines(changes):
    return sum(c.old_count + c.new_count for c in changes)


def run(argv, runner=main.subprocess_runner):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = main.main(argv, runner)
    return code, out.getvalue(), err.getvalue()


class Temp(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)

    def write(self, name, text):
        path = os.path.join(self.dir, name)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8", newline="") as handle:
            handle.write(text)
        return path


class LineTests(unittest.TestCase):
    def test_lines_keep_their_newline_and_a_missing_final_newline_is_visible(self):
        self.assertEqual(S("a\nb\n"), ["a\n", "b\n"])
        self.assertEqual(S("a\nb"), ["a\n", "b"])
        self.assertEqual(S(""), [])
        self.assertEqual(S("\n\n"), ["\n", "\n"])
        self.assertNotEqual(S("a\nb")[-1], S("a\nb\n")[-1])


class DiffTests(unittest.TestCase):
    def test_myers_finds_the_shortest_edit_script_of_the_paper_example(self):
        old, new = list("ABCABBA"), list("CBABAC")
        changes = main.diff(old, new, "myers")
        self.assertEqual(changed_lines(changes), 5)
        self.assertEqual(apply(old, new, changes), new)

    def test_identical_and_empty_inputs(self):
        self.assertEqual(main.diff(S("a\nb\n"), S("a\nb\n")), [])
        self.assertEqual(main.diff([], S("a\nb\n")), [main.Change(0, 0, 0, 2)])
        self.assertEqual(main.diff(S("a\n"), []), [main.Change(0, 1, 0, 0)])
        self.assertEqual(main.diff([], []), [])

    def test_every_algorithm_produces_a_script_that_rebuilds_the_new_file(self):
        rng = random.Random(20261109)
        alphabet = ["a\n", "b\n", "c\n", "}\n", "\n", "return 1;\n"]
        for _ in range(300):
            old = [rng.choice(alphabet) for _ in range(rng.randint(0, 30))]
            new = [rng.choice(alphabet) for _ in range(rng.randint(0, 30))]
            for algorithm in main.ALGORITHMS:
                changes = main.diff(old, new, algorithm)
                self.assertEqual(apply(old, new, changes), new, (algorithm, old, new))
                kept_old = [line for i, line in enumerate(old) if not any(c.old_start <= i < c.old_start + c.old_count for c in changes)]
                kept_new = [line for j, line in enumerate(new) if not any(c.new_start <= j < c.new_start + c.new_count for c in changes)]
                self.assertEqual(kept_old, kept_new)

    def test_myers_is_never_longer_than_patience_or_histogram(self):
        rng = random.Random(7)
        for _ in range(200):
            old = [rng.choice("abcde") + "\n" for _ in range(rng.randint(0, 25))]
            new = [rng.choice("abcde") + "\n" for _ in range(rng.randint(0, 25))]
            shortest = changed_lines(main.diff(old, new, "myers"))
            self.assertLessEqual(shortest, changed_lines(main.diff(old, new, "patience")))
            self.assertLessEqual(shortest, changed_lines(main.diff(old, new, "histogram")))

    def test_patience_and_histogram_keep_whole_functions_where_myers_matches_braces(self):
        old, new = fixture("patience", "old.c"), fixture("patience", "new.c")
        result = {name: main.diff_json(old, new, main.diff(old, new, name), name) for name in main.ALGORITHMS}
        removed = {name: data["removed"] for name, data in result.items()}
        self.assertIn("int frobnitz(int foo)\n", removed["myers"])
        self.assertIn("int frobnitz(int foo)\n", result["myers"]["added"])
        for name in ("patience", "histogram"):
            self.assertNotIn("int frobnitz(int foo)\n", removed[name])
            self.assertIn("{\n", removed[name])
            self.assertIn('        printf("Your answer is: ");\n', removed[name])
        self.assertGreater(len(result["myers"]["changes"]), len(result["patience"]["changes"]))

    def test_unique_lcs_is_the_longest_increasing_run_of_unique_pairs(self):
        pairs = [(0, 3), (1, 0), (2, 1), (3, 4), (4, 2), (5, 5)]
        self.assertEqual(main.unique_lcs(pairs), [(1, 0), (2, 1), (4, 2), (5, 5)])
        self.assertEqual(main.unique_lcs([]), [])

    def test_histogram_falls_back_to_myers_when_every_common_line_is_too_frequent(self):
        a, b = main.classify(["x\n"] * 70 + ["a\n"], ["x\n"] * 70 + ["b\n"])
        self.assertEqual(main.rarest_common_region(a, b, 0, len(a), 0, len(b))[0], "fallback")
        self.assertEqual(main.rarest_common_region([1, 2], [3, 4], 0, 2, 0, 2), ("none", None))
        old, new = ["x\n"] * 70 + ["a\n"], ["x\n"] * 70 + ["b\n"]
        self.assertEqual(main.diff(old, new, "histogram"), [main.Change(70, 1, 70, 1)])

    def test_histogram_rediffs_a_group_that_compaction_moved(self):
        old, new = S("b\n}\n"), S("}\nb\na\n}\n")
        self.assertEqual(changed_lines(main.diff(old, new, "histogram")), 2)
        self.assertEqual(changed_lines(main.diff(old, new, "histogram", rediff=False)), 4)
        self.assertEqual(main.diff(old, new, "myers"), main.diff(old, new, "myers", rediff=False))

    def test_compaction_slides_an_ambiguous_insertion_to_the_last_possible_place(self):
        self.assertEqual(main.diff(S("a\nb\nc\n"), S("a\nb\nb\nc\n")), [main.Change(2, 0, 2, 1)])
        self.assertEqual(main.diff(S("x\n}\n\nx\n}\n"), S("x\n}\n\nx\n}\n\nx\n}\n")), [main.Change(5, 0, 5, 3)])

    def test_unknown_algorithm_is_an_error_that_names_the_choices(self):
        with self.assertRaises(main.MergeError) as caught:
            main.diff(["a\n"], ["b\n"], "minimal")
        self.assertIn("myers", str(caught.exception))


class UnifiedTests(unittest.TestCase):
    def test_hunk_headers_use_one_based_starts_and_omit_a_count_of_one(self):
        self.assertEqual(main.hunk_range(0, 0), "0,0")
        self.assertEqual(main.hunk_range(4, 0), "4,0")
        self.assertEqual(main.hunk_range(4, 1), "5")
        self.assertEqual(main.hunk_range(4, 3), "5,3")
        text = main.unified([], S("a\n"), main.diff([], S("a\n")))
        self.assertEqual(text, "--- a\n+++ b\n@@ -0,0 +1 @@\n+a\n")

    def test_context_lines_surround_each_change_and_close_changes_share_a_hunk(self):
        old = S("".join(f"{n}\n" for n in range(1, 21)))
        new = list(old)
        new[2], new[8], new[18] = "three\n", "nine\n", "nineteen\n"
        changes = main.diff(old, new)
        self.assertEqual(len(main.hunks(changes, 3)), 2)
        self.assertEqual(len(main.hunks(changes, 0)), 3)
        text = main.unified(old, new, changes, 3, "a/n.txt", "b/n.txt")
        self.assertEqual([line for line in text.splitlines() if line.startswith("@@")], ["@@ -1,12 +1,12 @@", "@@ -16,5 +16,5 @@"])
        self.assertIn("-3\n+three\n", text)
        zero = main.unified(old, new, changes, 0)
        self.assertIn("@@ -3 +3 @@\n-3\n+three\n", zero)

    def test_a_missing_final_newline_gets_the_marker_line(self):
        old, new = S("a\nb"), S("a\nb\n")
        text = main.unified(old, new, main.diff(old, new))
        self.assertEqual(text, "--- a\n+++ b\n@@ -1,2 +1,2 @@\n a\n-b\n\\ No newline at end of file\n+b\n")
        self.assertEqual(main.unified(old, old, []), "")


class MergeTests(unittest.TestCase):
    def merge(self, base, ours, theirs, **options):
        options.setdefault("labels", ("ours", "base", "theirs"))
        return main.merge3(S(base), S(ours), S(theirs), **options)

    def test_changes_to_different_lines_merge_cleanly(self):
        result = self.merge("1\n2\n3\n4\n5\n", "1\nTWO\n3\n4\n5\n", "1\n2\n3\n4\nFIVE\n")
        self.assertEqual((result.conflicts, "".join(result.lines)), (0, "1\nTWO\n3\n4\nFIVE\n"))
        self.assertEqual([main.MODE_NAMES[r.mode] for r in result.regions], ["ours", "theirs"])

    def test_an_untouched_side_returns_the_other_side_verbatim(self):
        self.assertEqual("".join(self.merge("a\nb\n", "a\nb\n", "a\nc").lines), "a\nc")
        self.assertEqual("".join(self.merge("a\nb\n", "x\n", "a\nb\n").lines), "x\n")

    def test_the_same_change_on_both_sides_is_not_a_conflict(self):
        result = self.merge("a\nb\nc\n", "a\nB\nc\n", "a\nB\nc\n")
        self.assertEqual((result.conflicts, "".join(result.lines)), (0, "a\nB\nc\n"))

    def test_adjacent_edits_conflict_and_the_rest_of_the_file_still_merges(self):
        base, ours, theirs = (fixture("config", name + ".yaml") for name in ("base", "ours", "theirs"))
        result = main.merge3(base, ours, theirs, labels=("main", "base", "feature"))
        text = "".join(result.lines)
        self.assertEqual(result.conflicts, 1)
        self.assertIn("<<<<<<< main\nreplicas: 3\nimage: registry.example.com/shop/checkout:1.4.3\n=======\nreplicas: 4\n", text)
        self.assertEqual(text.count('CACHE_TTL: "600"'), 1)
        self.assertIn("memory: 512Mi", text)
        self.assertIn("period: 5s", text)

    def test_merge_diff3_and_zdiff3_show_different_amounts_of_the_conflict(self):
        base, ours, theirs = "a\nX\nb\n", "a\n1\n2\nY\n3\nb\n", "a\n1\n2\nZ\n3\nb\n"
        plain = "".join(self.merge(base, ours, theirs, style="merge").lines)
        self.assertEqual(plain, "a\n1\n2\n<<<<<<< ours\nY\n=======\nZ\n>>>>>>> theirs\n3\nb\n")
        diff3 = "".join(self.merge(base, ours, theirs, style="diff3").lines)
        self.assertEqual(diff3, "a\n<<<<<<< ours\n1\n2\nY\n3\n||||||| base\nX\n=======\n1\n2\nZ\n3\n>>>>>>> theirs\nb\n")
        zdiff3 = "".join(self.merge(base, ours, theirs, style="zdiff3").lines)
        self.assertEqual(zdiff3, "a\n1\n2\n<<<<<<< ours\nY\n||||||| base\nX\n=======\nZ\n>>>>>>> theirs\n3\nb\n")

    def test_levels_decide_how_far_conflicts_shrink_and_join(self):
        base, ours, theirs = "a\nX\nb\n", "a\n1\nY\nb\n", "a\n1\nZ\nb\n"
        self.assertIn("<<<<<<< ours\n1\nY\n", "".join(self.merge(base, ours, theirs, level="eager").lines))
        self.assertIn("1\n<<<<<<< ours\nY\n", "".join(self.merge(base, ours, theirs, level="zealous").lines))
        base, ours, theirs = "1\n2\n}\n}\n\n}\n8\n9\n", "1\nA\n}\n}\n\n}\nB\n9\n", "1\nC\n}\n}\n\n}\nD\n9\n"
        self.assertEqual(self.merge(base, ours, theirs, level="zealous").conflicts, 2)
        self.assertEqual(self.merge(base, ours, theirs, level="zealous-alnum").conflicts, 1)
        with self.assertRaises(main.MergeError):
            self.merge(base, ours, theirs, level="greedy")

    def test_delete_against_modify_is_a_conflict_with_an_empty_side(self):
        result = self.merge("a\nb\nc\n", "a\nc\n", "a\nB\nc\n")
        self.assertEqual("".join(result.lines), "a\n<<<<<<< ours\n=======\nB\n>>>>>>> theirs\nc\n")

    def test_favor_resolves_every_conflict_one_way(self):
        base, ours, theirs = "a\nb\nc\n", "a\nO\nc\n", "a\nT\nc\n"
        self.assertEqual("".join(self.merge(base, ours, theirs, favor="ours").lines), "a\nO\nc\n")
        self.assertEqual("".join(self.merge(base, ours, theirs, favor="theirs").lines), "a\nT\nc\n")
        union = self.merge(base, ours, theirs, favor="union")
        self.assertEqual(("".join(union.lines), union.conflicts), ("a\nO\nT\nc\n", 0))

    def test_markers_start_on_their_own_line_when_a_side_has_no_final_newline(self):
        result = self.merge("a\nb\n", "a\nO", "a\nT", marker_size=3)
        self.assertEqual("".join(result.lines), "a\n<<< ours\nO\n===\nT\n>>> theirs\n")

    def test_a_line_based_merge_of_json_is_clean_and_still_wrong(self):
        base, ours, theirs = (fixture("json", name + ".json") for name in ("base", "ours", "theirs"))
        result = main.merge3(base, ours, theirs)
        text = "".join(result.lines)
        self.assertEqual(result.conflicts, 0)
        self.assertEqual(text.count('"retries"'), 2)
        self.assertEqual(json.loads(text)["retries"], 5)
        pairs = json.loads(text, object_pairs_hook=lambda items: [key for key, _ in items])
        self.assertEqual(pairs.count("retries"), 2)

    def test_unknown_style_is_rejected(self):
        with self.assertRaises(main.MergeError):
            self.merge("a\n", "b\n", "c\n", style="fancy")


CRISS_CROSS = {
    "A": ["1", "2", "3", "4", "5", "6", "7", "8", "9"],
    "X1": ["1", "2x", "3", "4", "5", "6", "7", "8", "9"],
    "Y1": ["1", "2y", "3", "4", "5", "6", "7", "8", "9"],
    "X2": ["1", "2xy", "3", "4", "5", "6", "7", "8", "9"],
    "Y2": ["1", "2yx", "3", "4", "5", "6", "7", "8", "9"],
    "X3": ["1", "2xy", "3", "4", "5", "6", "7", "8x", "9"],
    "Y3": ["1", "2yx", "3", "4", "5y", "6", "7", "8", "9"],
}


class MergeBaseTests(unittest.TestCase):
    def setUp(self):
        self.graph = main.read_graph(os.path.join(FIXTURES, "criss-cross.json"))

    def test_a_criss_cross_history_has_two_best_common_ancestors(self):
        self.assertEqual(main.merge_bases(self.graph, "X3", "Y3"), ["X1", "Y1"])
        self.assertEqual(main.merge_bases(self.graph, "X2", "Y2"), ["X1", "Y1"])

    def test_an_ancestor_is_its_own_merge_base_and_older_common_ancestors_are_not_best(self):
        self.assertEqual(main.merge_bases(self.graph, "B", "X3"), ["B"])
        self.assertEqual(main.merge_bases(self.graph, "X3", "X3"), ["X3"])
        self.assertEqual(main.merge_bases(self.graph, "X1", "Y1"), ["B"])
        self.assertTrue(main.is_ancestor(self.graph, "A", "Y3"))
        self.assertFalse(main.is_ancestor(self.graph, "X3", "Y3"))
        self.assertEqual(main.generations(self.graph)["X3"], 5)

    def test_unrelated_histories_have_no_merge_base(self):
        graph = {"a1": (), "a2": ("a1",), "b1": (), "b2": ("b1",)}
        self.assertEqual(main.merge_bases(graph, "a2", "b2"), [])
        with self.assertRaises(main.MergeError):
            main.merge_bases(graph, "a2", "zz")

    def test_merge_bases_agree_with_a_brute_force_definition(self):
        rng = random.Random(3)
        for _ in range(20):
            graph = {}
            for n in range(25):
                earlier = list(graph)
                graph[f"c{n}"] = tuple(rng.sample(earlier, min(len(earlier), rng.choice([1, 1, 2, 3])))) if earlier else ()

            def ancestors(commit):
                seen, stack = set(), [commit]
                while stack:
                    current = stack.pop()
                    if current not in seen:
                        seen.add(current)
                        stack.extend(graph[current])
                return seen

            for _ in range(20):
                one, two = rng.choice(list(graph)), rng.choice(list(graph))
                common = ancestors(one) & ancestors(two)
                best = {c for c in common if not any(o != c and c in ancestors(o) for o in common)}
                self.assertEqual(set(main.merge_bases(graph, one, two)), best)

    def test_a_criss_cross_merge_builds_a_virtual_base_first(self):
        graph = {"A": (), "X1": ("A",), "Y1": ("A",), "X2": ("X1", "Y1"), "Y2": ("Y1", "X1"), "X3": ("X2",), "Y3": ("Y2",)}
        store = main.Store(lambda commit: [line + "\n" for line in CRISS_CROSS[commit]])
        steps = []
        result, bases = main.merge_commits(graph, "X3", "Y3", store, steps=steps, names=("x", "y"))
        self.assertEqual(bases, ["X1", "Y1"])
        self.assertEqual(result.conflicts, 1)
        text = "".join(result.lines)
        self.assertIn("<<<<<<< x\n2xy\n=======\n2yx\n>>>>>>> y\n", text)
        self.assertIn("5y\n", text)
        self.assertIn("8x\n", text)
        self.assertIn("1 conflict(s) left in it", " ".join(steps))
        virtual = [name for name in store.cache if name.startswith("virtual")]
        self.assertEqual(len(virtual), 1)
        self.assertIn("<<<<<<<<< Temporary merge branch 1\n", "".join(store(virtual[0])))

    def test_read_graph_rejects_malformed_files(self):
        with tempfile.TemporaryDirectory() as folder:
            bad = os.path.join(folder, "bad.json")
            for content in ("{not json", '{"commits": {"A": "B"}}', "[1, 2]"):
                with open(bad, "w") as handle:
                    handle.write(content)
                with self.assertRaises(main.MergeError):
                    main.read_graph(bad)


class RenameTests(unittest.TestCase):
    def test_exact_renames_score_100_and_prefer_the_same_basename(self):
        old = {"a/x.txt": b"same\n", "b/y.txt": b"same\n"}
        new = {"c/y.txt": b"same\n"}
        self.assertEqual(main.find_renames(old, new), [main.Rename("b/y.txt", "c/y.txt", main.MAX_SCORE)])
        self.assertEqual(main.find_renames(old, new)[0].similarity, 100)

    def test_an_edited_file_is_a_rename_above_the_threshold_only(self):
        body = "".join(f"line {n}\n" for n in range(20)).encode()
        edited = body.replace(b"line 3\n", b"line three\n").replace(b"line 9\n", b"line nine\n")
        found = main.find_renames({"old.txt": body}, {"new.txt": edited})
        self.assertEqual(len(found), 1)
        self.assertTrue(80 <= found[0].similarity < 100, found[0].similarity)
        self.assertEqual(main.find_renames({"old.txt": body}, {"new.txt": edited}, threshold=95), [])
        self.assertEqual(main.find_renames({"old.txt": body}, {"new.txt": b"unrelated\n" * 20}), [])
        with self.assertRaises(main.MergeError):
            main.find_renames({}, {}, threshold=101)

    def test_similarity_is_bounded_and_size_difference_short_circuits(self):
        self.assertEqual(main.similarity(b"abc\n", b"abc\n", 0), main.MAX_SCORE)
        self.assertEqual(main.similarity(b"a\n", b"a\n" * 100, 30000), 0)
        self.assertEqual(main.similarity(b"a\n", b"", 0), 0)
        self.assertEqual(main.spans(b"x\r\ny\r\n"), main.spans(b"x\ny\n"))

    def test_each_source_is_used_once_and_the_best_match_wins(self):
        base = "".join(f"row {n}\n" for n in range(30))
        old = {"src/a.py": base.encode(), "src/b.py": (base + "extra\n" * 30).encode()}
        new = {"lib/a.py": (base + "tail\n").encode(), "lib/b2.py": base.encode()}
        found = {r.target: r.source for r in main.find_renames(old, new)}
        self.assertEqual(found["lib/b2.py"], "src/a.py")
        self.assertEqual(found.get("lib/a.py"), "src/b.py")


class CliTests(Temp):
    def test_diff_exit_codes_labels_and_json(self):
        old, new = self.write("old.txt", "a\nb\n"), self.write("new.txt", "a\nc\n")
        code, out, _ = run(["diff", old, new, "--label", "a/f", "--label", "b/f"])
        self.assertEqual(code, 1)
        self.assertTrue(out.startswith("--- a/f\n+++ b/f\n@@ -1,2 +1,2 @@\n"))
        self.assertEqual(run(["diff", old, old])[:2], (0, ""))
        data = json.loads(run(["diff", "--json", "--algorithm", "patience", old, new])[1])
        self.assertEqual((data["removed"], data["added"], data["changes"][0]["old_start"]), (["b\n"], ["c\n"], 2))
        code, _, err = run(["diff", "-U", "-1", old, new])
        self.assertEqual(code, 2)
        self.assertIn("-U", err)

    def test_merge_file_writes_output_reports_conflicts_and_json(self):
        base, ours, theirs = self.write("b", "a\nb\nc\n"), self.write("o", "a\nO\nc\n"), self.write("t", "a\nT\nc\n")
        target = os.path.join(self.dir, "out")
        code, _, err = run(["merge-file", "-L", "mine", "-L", "orig", "-L", "yours", "--diff3", "-o", target, ours, base, theirs])
        self.assertEqual(code, 1)
        self.assertIn("1 conflict(s)", err)
        self.assertEqual(main.read_text(target), "a\n<<<<<<< mine\nO\n||||||| orig\nb\n=======\nT\n>>>>>>> yours\nc\n")
        data = json.loads(run(["merge-file", "--json", ours, base, theirs])[1])
        self.assertEqual((data["clean"], data["regions"][0]["mode"], data["regions"][0]["base"]), (False, "conflict", [2, 1]))
        self.assertEqual(run(["merge-file", "--favor", "theirs", ours, base, theirs])[:2], (0, "a\nT\nc\n"))

    def test_errors_exit_2_with_a_message(self):
        code, _, err = run(["merge-file", "missing-ours", "missing-base", "missing-theirs"])
        self.assertEqual(code, 2)
        self.assertIn("cannot read missing-", err)
        code, _, err = run(["merge-file", "-L", "1", "-L", "2", "-L", "3", "-L", "4", "a", "b", "c"])
        self.assertEqual(code, 2)
        self.assertIn("at most three", err)
        code, _, err = run(["renames", self.dir, os.path.join(self.dir, "nope")])
        self.assertEqual(code, 2)
        self.assertIn("is not a directory", err)

    def test_merge_base_from_a_graph_file(self):
        graph = os.path.join(FIXTURES, "criss-cross.json")
        self.assertEqual(run(["merge-base", "X3", "Y3", "--graph", graph])[:2], (0, "X1\nY1\n"))
        data = json.loads(run(["merge-base", "--json", "X3", "Y3", "--graph", graph])[1])
        self.assertTrue(data["criss_cross"])
        unrelated = self.write("unrelated.json", '{"commits": {"a": [], "b": []}}')
        code, out, err = run(["merge-base", "a", "b", "--graph", unrelated])
        self.assertEqual((code, out), (1, ""))
        self.assertIn("no common ancestor", err)

    def test_merge_base_reads_a_repository_through_the_runner(self):
        calls = []

        def fake(argv):
            calls.append(list(argv))
            if "rev-parse" in argv:
                return 0, {"x^{commit}": "c3\n", "y^{commit}": "c4\n"}[argv[-1]], ""
            return 0, "c4 c2\nc3 c2 c1\nc2 c1\nc1\n", ""

        code, out, _ = run(["merge-base", "--repo", "/r", "x", "y"], fake)
        self.assertEqual((code, out), (0, "c2\n"))
        self.assertEqual(calls[-1][:6], ["git", "-C", "/r", "rev-list", "--parents", "--all"])
        code, _, err = run(["merge-base", "--repo", "/r", "x", "y"], lambda argv: (128, "", "fatal: not a git repository"))
        self.assertEqual(code, 2)
        self.assertIn("does not name a commit", err)

    def test_renames_prints_name_status_lines(self):
        self.write("old/docs/guide.md", "".join(f"step {n}\n" for n in range(10)))
        self.write("old/gone.txt", "bye\n")
        self.write("new/handbook/guide.md", "".join(f"step {n}\n" for n in range(10)))
        self.write("new/fresh.txt", "hi\n")
        code, out, _ = run(["renames", os.path.join(self.dir, "old"), os.path.join(self.dir, "new")])
        self.assertEqual(code, 0)
        self.assertEqual(out, "R100\tdocs/guide.md\thandbook/guide.md\nD\tgone.txt\nA\tfresh.txt\n")


@unittest.skipUnless(GIT, "git is not installed")
class GitOracleTests(Temp):
    def git(self, *args, check=True, **extra):
        env = dict(os.environ, GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1", GIT_AUTHOR_NAME="Ada", GIT_AUTHOR_EMAIL="ada@example.invalid",
                   GIT_COMMITTER_NAME="Ada", GIT_COMMITTER_EMAIL="ada@example.invalid", GIT_AUTHOR_DATE="@1767600000 +0000", GIT_COMMITTER_DATE="@1767600000 +0000", **extra)
        done = subprocess.run(["git", "-c", "init.defaultBranch=main", "-c", "diff.indentHeuristic=false", *args], cwd=self.dir, env=env,
                              capture_output=True, text=True, stdin=subprocess.DEVNULL)
        if check and done.returncode not in (0, 1):
            self.fail(f"git {args}: {done.stderr}")
        return done

    def git_version(self):
        numbers = self.git("version").stdout.split()[2].split(".")
        return tuple(int(n) for n in numbers[:2] if n.isdigit())

    def test_diff_changes_equal_git_diff_no_index_for_each_algorithm(self):
        old, new = os.path.join(FIXTURES, "patience", "old.c"), os.path.join(FIXTURES, "patience", "new.c")
        a, b = fixture("patience", "old.c"), fixture("patience", "new.c")

        def body(text):
            lines = text.splitlines()
            start = next(i for i, line in enumerate(lines) if line.startswith("@@"))
            return [line.split(" @@")[0] if line.startswith("@@") else line for line in lines[start:]]

        for algorithm in main.ALGORITHMS:
            theirs = self.git("diff", "--no-index", "--no-color", "--diff-algorithm=" + algorithm, old, new).stdout
            mine = main.unified(a, b, main.diff(a, b, algorithm))
            self.assertEqual(body(mine), body(theirs), algorithm)

    def test_merge_file_equals_git_merge_file_in_every_style(self):
        paths = [os.path.join(FIXTURES, "config", name + ".yaml") for name in ("ours", "base", "theirs")]
        texts = [fixture("config", name + ".yaml") for name in ("base", "ours", "theirs")]
        for style in main.STYLES:
            flags = [] if style == "merge" else ["--" + style]
            theirs = self.git("merge-file", "-p", "-L", "o", "-L", "b", "-L", "t", *flags, *paths)
            mine = main.merge3(*texts, style=style, labels=("o", "b", "t"))
            self.assertEqual("".join(mine.lines), theirs.stdout, style)
            self.assertEqual(mine.conflicts, theirs.returncode, style)

    def test_merge_bases_equal_git_merge_base_all_on_a_real_criss_cross(self):
        self.git("init", "-q", "repo")
        repo = os.path.join(self.dir, "repo")
        tree = self.git("-C", repo, "hash-object", "-t", "tree", "-w", "/dev/null").stdout.strip()
        ids = {}
        for name, parents in (("A", ()), ("X1", ("A",)), ("Y1", ("A",)), ("X2", ("X1", "Y1")), ("Y2", ("Y1", "X1")), ("X3", ("X2",)), ("Y3", ("Y2",))):
            args = [part for parent in parents for part in ("-p", ids[parent])]
            ids[name] = self.git("-C", repo, "commit-tree", tree, "-m", name, *args).stdout.strip()
        self.git("-C", repo, "update-ref", "refs/heads/x", ids["X3"])
        self.git("-C", repo, "update-ref", "refs/heads/y", ids["Y3"])
        expected = set(self.git("-C", repo, "merge-base", "--all", "x", "y").stdout.split())
        self.assertEqual(expected, {ids["X1"], ids["Y1"]})
        code, out, err = run(["merge-base", "--repo", repo, "x", "y"])
        self.assertEqual((code, set(out.split())), (0, expected), err)


if __name__ == "__main__":
    unittest.main()
