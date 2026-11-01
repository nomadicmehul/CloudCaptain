import json
import math
import os
import random
import shutil
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import main  # noqa: E402


def dataset(n=600, dim=12, clusters=12, queries=25, seed=3, spread=0.8):
    return main.generate(n, dim, clusters, queries, seed, tags=10, spread=spread)


def mean_recall(index, queries, k=10, flt=None, mode="post", **knobs):
    total = 0.0
    for query in queries:
        truth = index.exact(index.space.prepare(query["vector"]), k, flt).keys
        total += main.recall_at_k(index.search(query["vector"], k, flt, mode, **knobs).keys, truth, k)
    return total / len(queries)


def run(argv):
    out, err = StringIO(), StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = main.main(argv)
    return code, out.getvalue(), err.getvalue()


ITEMS, QUERIES = dataset()


class DistanceTests(unittest.TestCase):
    def test_l2_and_cosine_distances_match_hand_computed_values(self):
        self.assertEqual(main.l2_distance([0, 0], [3, 4]), 5.0)
        self.assertAlmostEqual(main.cosine_distance([1, 0], [0, 1]), 1.0)
        self.assertAlmostEqual(main.cosine_distance([1, 1], [2, 2]), 0.0)
        self.assertAlmostEqual(main.cosine_distance([1, 0], [-1, 0]), 2.0)

    def test_cosine_on_unit_vectors_is_half_the_squared_l2_distance(self):
        rng = random.Random(1)
        space = main.Space("cosine", 6)
        for _ in range(20):
            a = [rng.uniform(-1, 1) for _ in range(6)]
            b = [rng.uniform(-1, 1) for _ in range(6)]
            ranked = math.dist(space.prepare(a), space.prepare(b))
            self.assertAlmostEqual(space.report(ranked), main.cosine_distance(a, b), places=12)
        self.assertEqual(main.Space("l2", 2).report(3.0), 3.0)

    def test_vectors_are_validated_like_pgvector_does(self):
        self.assertEqual(main.as_vector("[1, 2,3]"), [1.0, 2.0, 3.0])
        self.assertEqual(main.as_vector([1, 2], dim=2), [1.0, 2.0])
        for bad in ("[1,nan]", "[1,inf]", "[]", "[a,b]"):
            with self.assertRaises(main.InputError):
                main.as_vector(bad)
        with self.assertRaisesRegex(main.InputError, "expected 3 dimensions"):
            main.as_vector([1, 2], dim=3)
        with self.assertRaisesRegex(main.InputError, "zero vector"):
            main.Space("cosine", 2).prepare([0, 0])
        with self.assertRaises(main.InputError):
            main.Space("dot", 2)


class FlatTests(unittest.TestCase):
    def line(self):
        index = main.Index(main.Space("l2", 1))
        for i in range(10):
            index.upsert(f"p{i}", [float(i)], {"tag": i % 2})
        return index

    def test_flat_search_is_exact_ordered_and_scans_everything(self):
        index = self.line()
        result = index.search([3.2], k=3)
        self.assertEqual(result.keys, ["p3", "p4", "p2"])
        self.assertAlmostEqual(result.hits[0].distance, 0.2)
        self.assertEqual(result.evals, 10)
        self.assertEqual(len(index.search([0.0], k=50).hits), 10)
        with self.assertRaises(main.InputError):
            index.search([0.0], k=0)

    def test_flat_filters_are_exact_in_every_mode_but_pre_scans_only_matches(self):
        index = self.line()
        answers = {mode: index.search([3.2], 3, {"tag": 1}, mode) for mode in main.FILTER_MODES}
        for result in answers.values():
            self.assertEqual(result.keys, ["p3", "p5", "p1"])
        self.assertEqual(answers["pre"].evals, 5)
        self.assertEqual(answers["post"].evals, 10)


class RecallTests(unittest.TestCase):
    def test_recall_at_k_counts_overlap_order_free_and_missing_results_as_misses(self):
        self.assertAlmostEqual(main.recall_at_k(["a", "b"], ["a", "b", "c"], 3), 2 / 3)
        self.assertEqual(main.recall_at_k(["c", "b", "a"], ["a", "b", "c"], 3), 1.0)
        self.assertEqual(main.recall_at_k(["a", "x", "y", "b"], ["a", "b"], 2), 0.5)
        self.assertEqual(main.recall_at_k([], [], 10), 1.0)
        self.assertEqual(main.percentile([5, 1, 3, 2, 4], 50), 3.0)
        self.assertEqual(main.percentile([5, 1, 3, 2, 4], 95), 5.0)


class IVFTests(unittest.TestCase):
    def test_kmeans_separates_well_separated_clusters_deterministically(self):
        rng = random.Random(0)
        points = [[rng.gauss(0, 0.1), rng.gauss(0, 0.1)] for _ in range(50)] + [[rng.gauss(10, 0.1), rng.gauss(10, 0.1)] for _ in range(50)]
        centroids, assignment = main.kmeans(points, 2, random.Random(4))
        self.assertEqual(sorted(round(c[0]) for c in centroids), [0, 10])
        self.assertEqual(len(set(assignment[:50])), 1)
        self.assertNotEqual(assignment[0], assignment[-1])
        self.assertEqual(main.kmeans(points, 2, random.Random(4)), (centroids, assignment))

    def test_probing_every_list_is_exact_and_work_is_centroids_plus_scanned_lists(self):
        index = main.build("ivf", ITEMS, nlist=8, seed=1)
        self.assertEqual(sum(len(members) for members in index.lists), len(ITEMS))
        for query in QUERIES[:10]:
            exact = index.exact(index.space.prepare(query["vector"]), 10).keys
            self.assertEqual(index.search(query["vector"], 10, nprobe=8).keys, exact)
        one = index.search(QUERIES[0]["vector"], 10, nprobe=1)
        nearest = main.nearest_index(index.space.prepare(QUERIES[0]["vector"]), index.centroids)
        self.assertEqual(one.evals, 8 + len(index.lists[nearest]))
        self.assertEqual(one.note, "probed 1 of 8 lists")

    def test_recall_rises_with_probes_and_late_inserts_join_the_nearest_list(self):
        index = main.build("ivf", ITEMS, nlist=16, seed=1)
        low, mid, high = (mean_recall(index, QUERIES, nprobe=p) for p in (1, 4, 16))
        self.assertLessEqual(low, mid)
        self.assertLessEqual(mid, high)
        self.assertEqual(high, 1.0)
        target = index.centroids[5]
        node = index.upsert("late", [x + 1e-6 for x in target])
        self.assertIn(node, index.lists[5])
        self.assertEqual(index.stats()["growth_since_training"], round((len(ITEMS) + 1) / len(ITEMS), 2))

    def test_training_on_fewer_vectors_than_lists_warns_and_untrained_search_fails(self):
        index = main.build("ivf", ITEMS[:5], nlist=8)
        self.assertEqual(len(index.centroids), 5)
        self.assertIn("only 5 vectors for nlist=8", index.stats()["warnings"][0])
        empty = main.IVF(main.Space("l2", 12), nlist=4)
        with self.assertRaisesRegex(main.InputError, "load data first"):
            empty.train()
        with self.assertRaisesRegex(main.InputError, "not trained"):
            empty.search(ITEMS[0]["vector"], 5)

    def test_iterative_probing_fills_k_when_post_filtering_cannot(self):
        index = main.build("ivf", ITEMS, nlist=16, seed=1)
        flt = {"tag": 3}
        post = [index.search(q["vector"], 10, flt, "post", nprobe=1) for q in QUERIES]
        iterative = [index.search(q["vector"], 10, flt, "iterative", nprobe=1) for q in QUERIES]
        self.assertLess(sum(len(r.hits) for r in post) / len(post), 10)
        self.assertTrue(all(len(r.hits) == 10 for r in iterative))
        self.assertTrue(all(h.meta["tag"] == 3 for r in iterative for h in r.hits))
        capped = index.search(QUERIES[0]["vector"], 10, flt, "iterative", nprobe=1, max_probes=1)
        self.assertEqual(capped.note, "probed 1 of 16 lists")


class HNSWTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.index = main.build("hnsw", ITEMS, m=8, ef_construction=48, seed=2)

    def test_graph_respects_degree_caps_and_levels_thin_out_geometrically(self):
        index = self.index
        for node, node_links in enumerate(index.links):
            for layer, neighbours in enumerate(node_links):
                self.assertLessEqual(len(neighbours), index.max_links(layer))
                self.assertNotIn(node, neighbours)
                for other in neighbours:
                    self.assertGreater(len(index.links[other]), layer)
        levels = index.stats()["levels"]
        self.assertGreater(levels["0"], levels.get("1", 0))
        self.assertGreater(levels["0"], len(ITEMS) // 2)
        self.assertEqual(index.top, max(len(node_links) for node_links in index.links) - 1)

    def test_recall_and_work_rise_with_ef_search(self):
        index = self.index
        low, high = mean_recall(index, QUERIES, ef=10), mean_recall(index, QUERIES, ef=200)
        self.assertLessEqual(low, high)
        self.assertGreaterEqual(high, 0.98)
        cheap = index.search(QUERIES[0]["vector"], 10, ef=10).evals
        dear = index.search(QUERIES[0]["vector"], 10, ef=200).evals
        self.assertLess(cheap, dear)
        self.assertLess(dear, len(ITEMS) * 1.01)

    def test_returns_at_most_ef_search_results_like_pgvector(self):
        self.assertEqual(len(self.index.search(QUERIES[0]["vector"], 10, ef=5).hits), 5)
        self.assertEqual(len(self.index.search(QUERIES[0]["vector"], 10, ef=40).hits), 10)

    def test_post_filter_comes_up_short_while_iterative_and_pre_return_k(self):
        flt = {"tag": 3}
        post = [self.index.search(q["vector"], 10, flt, "post", ef=40) for q in QUERIES]
        self.assertLess(sum(len(r.hits) for r in post) / len(post), 7)
        for query in QUERIES[:10]:
            iterative = self.index.search(query["vector"], 10, flt, "iterative", ef=40)
            pre = self.index.search(query["vector"], 10, flt, "pre")
            self.assertEqual(len(iterative.hits), 10)
            self.assertTrue(all(h.meta["tag"] == 3 for h in iterative.hits))
            self.assertEqual(pre.keys, self.index.exact(self.index.space.prepare(query["vector"]), 10, flt).keys)
            self.assertEqual(pre.evals, sum(1 for item in ITEMS if item["meta"]["tag"] == 3))

    def test_neighbour_heuristic_skips_a_candidate_closer_to_a_chosen_neighbour(self):
        index = main.HNSW(main.Space("l2", 2), m=4)
        for key, vector in (("q", [0, 0]), ("a", [1, 0]), ("b", [1.5, 0]), ("c", [0, -1.6])):
            index.upsert(key, vector)
        self.assertEqual(index.select_neighbors([(1.0, 1), (1.5, 2), (1.6, 3)], 3), [(1.0, 1), (1.6, 3)])

    def test_cosine_index_ranks_like_cosine_distance(self):
        index = main.build("hnsw", ITEMS[:200], metric="cosine", m=8, ef_construction=48)
        query = QUERIES[0]["vector"]
        result = index.search(query, 5, ef=200)
        expected = sorted(ITEMS[:200], key=lambda item: main.cosine_distance(query, item["vector"]))[:5]
        self.assertEqual(result.keys, [item["id"] for item in expected])
        self.assertAlmostEqual(result.hits[0].distance, main.cosine_distance(query, expected[0]["vector"]), places=9)


class MutationTests(unittest.TestCase):
    def test_delete_tombstones_and_hides_a_vector_in_every_index_kind(self):
        for kind in main.KINDS:
            index = main.build(kind, ITEMS[:300], m=8, nlist=8, seed=1)
            query = ITEMS[42]["vector"]
            self.assertEqual(index.search(query, 1, ef=40, nprobe=8).keys, ["42"], kind)
            self.assertTrue(index.delete("42"))
            self.assertFalse(index.delete("42"))
            self.assertNotIn("42", index.search(query, 10, ef=40, nprobe=8).keys, kind)
            self.assertEqual((index.live, index.tombstones), (299, 1), kind)

    def test_upsert_replaces_the_old_version_and_leaves_a_tombstone(self):
        for kind in main.KINDS:
            index = main.build(kind, ITEMS[:300], m=8, nlist=8, seed=1)
            query = QUERIES[0]["vector"]
            index.upsert("7", query, {"tag": 99})
            top = index.search(query, 1, ef=40, nprobe=8).hits[0]
            self.assertEqual((top.key, top.meta), ("7", {"tag": 99}), kind)
            self.assertAlmostEqual(top.distance, 0.0, places=9)
            self.assertEqual((index.live, index.tombstones), (300, 1), kind)


class SnapshotTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def test_round_trip_gives_identical_results_for_every_kind(self):
        for kind in main.KINDS:
            index = main.build(kind, ITEMS[:300], m=8, nlist=8, seed=1)
            index.delete("5")
            path = os.path.join(self.tmp, f"{kind}.json")
            main.save(index, path)
            again = main.load(path)
            self.assertFalse(os.path.exists(path + ".tmp"))
            self.assertEqual(again.stats(), index.stats(), kind)
            for query in QUERIES[:5]:
                self.assertEqual(again.search(query["vector"], 10, ef=20, nprobe=2).keys, index.search(query["vector"], 10, ef=20, nprobe=2).keys, kind)

    def test_inserting_after_a_reload_builds_the_same_graph_as_inserting_without_one(self):
        index = main.build("hnsw", ITEMS[:200], m=8, seed=5)
        path = os.path.join(self.tmp, "h.json")
        main.save(index, path)
        reloaded = main.load(path)
        for item in ITEMS[200:260]:
            index.upsert(item["id"], item["vector"], item["meta"])
            reloaded.upsert(item["id"], item["vector"], item["meta"])
        self.assertEqual(reloaded.links, index.links)
        self.assertEqual(reloaded.entry, index.entry)

    def test_load_rejects_files_that_are_not_snapshots_or_are_corrupt(self):
        path = os.path.join(self.tmp, "x.json")
        with open(path, "w") as handle:
            json.dump({"format": "something-else"}, handle)
        with self.assertRaisesRegex(main.InputError, "not a vecindex/1 snapshot"):
            main.load(path)
        doc = main.snapshot(main.build("hnsw", ITEMS[:50], m=4))
        doc["links"] = doc["links"][:10]
        with self.assertRaisesRegex(main.InputError, "10 link lists for 50 nodes"):
            main.restore(doc)
        del doc["links"]
        with self.assertRaisesRegex(main.InputError, "incomplete or corrupt"):
            main.restore(doc)


class EstimateTests(unittest.TestCase):
    def test_memory_work_and_latency_follow_the_formulas(self):
        n, dim = 1_000_000, 768
        self.assertEqual(main.memory_estimate("flat", n, dim), {"vectors": 3_072_000_000, "structure": 0, "total": 3_072_000_000})
        self.assertEqual(main.memory_estimate("ivf", n, dim, nlist=1000)["structure"], 1000 * 768 * 4 + n * 4)
        self.assertEqual(main.memory_estimate("hnsw", n, dim, m=16)["structure"], round(n * 4 * (32 + 16 / 15)))
        self.assertEqual(main.work_estimate("flat", n), 1_000_000)
        self.assertEqual(main.work_estimate("ivf", n, nlist=1000, nprobe=10), 11_000)
        self.assertIsNone(main.work_estimate("hnsw", n))
        self.assertAlmostEqual(main.latency_ms(1_000_000, 768, 10.0), 307.2)
        rows = {r["index"]: r for r in main.estimate(n, dim, nlist=1000, nprobe=10, hnsw_evals=2000)}
        self.assertEqual(rows["hnsw"]["evals"], 2000)
        self.assertLess(rows["hnsw"]["latency_ms"], rows["ivf"]["latency_ms"])
        self.assertLess(rows["ivf"]["latency_ms"], rows["flat"]["latency_ms"])


class PgvectorBridgeTests(unittest.TestCase):
    def test_generate_is_deterministic_and_each_tag_matches_one_row_in_ten(self):
        items, queries = dataset(n=100, dim=4, queries=3)
        again, _ = dataset(n=100, dim=4, queries=7)
        self.assertEqual(items, again)
        self.assertEqual([i["meta"]["tag"] for i in items[:12]], [0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 0, 1])
        self.assertEqual({len(i["vector"]) for i in items}, {4})
        self.assertEqual([q["qid"] for q in queries], [0, 1, 2])

    def test_pgcopy_rows_escape_the_copy_text_format(self):
        line = main.pgcopy_lines([{"id": "a\tb", "vector": [1, 2.5], "meta": {"note": "x\\y"}}])[0]
        self.assertEqual(line.split("\t"), ["a\\tb", '{"note": "x\\\\\\\\y"}', "[1.0,2.5]"])

    def test_benchmark_sql_runs_exact_first_and_sets_each_knob(self):
        sql = main.benchmark_sql(QUERIES[:2], 10, "l2", [10, 40], [1, 4], m=16, ef_construction=64, lists=40, flt={"tag": 3})
        self.assertLess(sql.index("SELECT 'exact'"), sql.index("CREATE INDEX"))
        self.assertIn("USING hnsw (embedding vector_l2_ops) WITH (m = 16, ef_construction = 64);", sql)
        self.assertIn("USING ivfflat (embedding vector_l2_ops) WITH (lists = 40);", sql)
        for line in ("SET hnsw.ef_search = 10;", "SET ivfflat.probes = 4;", "SET hnsw.iterative_scan = relaxed_order;", "SET ivfflat.iterative_scan = off;", "SET enable_seqscan = off;"):
            self.assertIn(line, sql)
        self.assertEqual(sql.count("SELECT 'hnsw:ef_search=40:post'"), 2)
        self.assertIn("WHERE meta @> '{\"tag\": 3}'::jsonb ORDER BY d LIMIT 10", sql)
        cosine = main.benchmark_sql(QUERIES[:1], 5, "cosine", [40], [], flt={"name": "o'brien"})
        self.assertIn("vector_cosine_ops", cosine)
        self.assertIn(" <=> '[", cosine)
        self.assertIn("o''brien", cosine)
        self.assertNotIn("ivfflat", cosine)

    def test_parse_and_grade_results_from_another_system(self):
        reference = main.build("flat", ITEMS)
        lines = []
        for query in QUERIES[:4]:
            truth = reference.exact(reference.space.prepare(query["vector"]), 10).keys
            lines.append(f"exact\t{{}}\t{query['qid']}\t{','.join(truth)}")
            lines.append(f"half\t{{}}\t{query['qid']}\t{','.join(truth[:5])}")
            filtered = reference.exact(reference.space.prepare(query["vector"]), 10, {"tag": 3}).keys
            lines.append(f"tagged\t{{\"tag\": 3}}\t{query['qid']}\t{','.join(filtered)}")
        rows = {r["label"]: r for r in main.grade(main.parse_results("\n".join(lines) + "\n"), reference, QUERIES, 10)}
        self.assertEqual((rows["exact"]["recall"], rows["half"]["recall"], rows["tagged"]["recall"]), (1.0, 0.5, 1.0))
        self.assertEqual(rows["half"]["returned"], 5.0)
        with self.assertRaisesRegex(main.InputError, "results line 2"):
            main.parse_results("exact\t{}\t0\ta,b\nbroken line\n")


class CliTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.data = os.path.join(self.tmp, "data.jsonl")
        self.queries = os.path.join(self.tmp, "queries.jsonl")
        code, out, _ = run(["generate", "--n", "300", "--dim", "8", "--clusters", "6", "--queries", "5", "--out", self.data, "--queries-out", self.queries, "--pgcopy", os.path.join(self.tmp, "items.tsv")])
        self.assertEqual(code, 0, out)

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def test_build_search_upsert_delete_round_trip(self):
        snap = os.path.join(self.tmp, "h.json")
        code, out, _ = run(["build", "--data", self.data, "--index", "hnsw", "--m", "8", "-o", snap, "--json"])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["live"], 300)
        vector = json.dumps([0.5] * 8)
        self.assertEqual(run(["upsert", "--index", snap, "--id", "probe", "--vector", vector, "--meta", '{"tag": 42}'])[0], 0)
        code, out, _ = run(["search", "--index", snap, "--vector", vector, "--k", "3", "--json"])
        hits = json.loads(out)["hits"]
        self.assertEqual((code, hits[0]["id"], hits[0]["meta"]), (0, "probe", {"tag": 42}))
        code, out, _ = run(["search", "--index", snap, "--vector", vector, "--filter", "tag=42", "--filter-mode", "iterative"])
        self.assertIn("probe", out)
        self.assertEqual(run(["delete", "--index", snap, "--id", "probe"])[0], 0)
        code, out, _ = run(["search", "--index", snap, "--vector", vector, "--k", "3", "--json"])
        self.assertNotIn("probe", [h["id"] for h in json.loads(out)["hits"]])
        code, _, err = run(["delete", "--index", snap, "--id", "probe"])
        self.assertEqual(code, 2)
        self.assertIn("no live vector", err)

    def test_sweep_sql_and_recall_with_a_recall_gate(self):
        code, out, _ = run(["sweep", "--data", self.data, "--index", "ivf", "--nlist", "6", "--queries", self.queries, "--values", "1,6", "--json"])
        rows = json.loads(out)["rows"]
        self.assertEqual((code, [r["label"] for r in rows]), (0, ["ivf:probes=1", "ivf:probes=6"]))
        self.assertEqual(rows[1]["recall"], 1.0)
        code, _, err = run(["sweep", "--data", self.data, "--index", "flat", "--queries", self.queries, "--min-recall", "1.01"])
        self.assertEqual(code, 1)
        self.assertIn("recall below 1.01", err)
        code, out, _ = run(["sql", "--queries", self.queries, "--hnsw-ef", "40", "--ivf-probes", ""])
        self.assertEqual(code, 0)
        self.assertNotIn("ivfflat", out)
        results = os.path.join(self.tmp, "results.tsv")
        reference = main.build("flat", main.read_items(self.data))
        with open(results, "w") as handle:
            for query in main.read_queries(self.queries):
                handle.write(f"seqscan\t{{}}\t{query['qid']}\t{','.join(reference.exact(query['vector'], 10).keys)}\n")
        code, out, _ = run(["recall", "--data", self.data, "--queries", self.queries, "--results", results, "--min-recall", "0.99", "--json"])
        self.assertEqual((code, json.loads(out)["rows"][0]["recall"]), (0, 1.0))

    def test_bad_input_exits_2_with_the_reason(self):
        snap = os.path.join(self.tmp, "f.json")
        self.assertEqual(run(["build", "--data", self.data, "--index", "flat", "-o", snap])[0], 0)
        cases = [
            (["search", "--index", snap, "--vector", "[1,2]"], "expected 8 dimensions"),
            (["search", "--index", snap, "--vector", "[1,2,3,4,5,6,7,nan]"], "finite"),
            (["search", "--index", snap, "--vector", "[1,1,1,1,1,1,1,1]", "--filter", "tag"], "key=value"),
            (["search", "--index", os.path.join(self.tmp, "missing.json"), "--vector", "[1]"], "cannot read"),
            (["build", "--data", self.queries + ".nope", "-o", snap], "cannot read"),
            (["sweep", "--data", self.data, "--queries", self.queries, "--values", "0"], "positive integers"),
        ]
        for argv, message in cases:
            code, _, err = run(argv)
            self.assertEqual(code, 2, argv)
            self.assertIn(message, err, argv)


if __name__ == "__main__":
    unittest.main()
