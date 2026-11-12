import http.client
import io
import itertools
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import main  # noqa: E402


def metric(key, value, timestamp, step):
    return {"key": key, "value": value, "timestamp": timestamp, "step": step}


class StoreCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.ticks = itertools.count(1000, 10)
        self.store = main.Store(str(self.root / "track.db"), str(self.root / "artifacts"), clock=lambda: next(self.ticks))
        self.exp = self.store.create_experiment("house", {"team": "pricing"})

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def run_with(self, name, params=None, metrics=None, tags=None):
        run = self.store.create_run(self.exp, name, "tester", next(self.ticks), tags or {})
        run_id = run["info"]["run_id"]
        self.store.log_batch(run_id, metrics or [], main.kv_list(params or {}), [])
        return run_id

    def names(self, filter_string="", order_by=None, max_results=None, token=None):
        runs, next_token = self.store.search_runs([self.exp], filter_string, order_by, max_results, token)
        return [r["info"]["run_name"] for r in runs], next_token


class TrainingTest(unittest.TestCase):
    def test_dataset_is_seeded_and_reproducible(self):
        self.assertEqual(main.make_dataset(50, 7), main.make_dataset(50, 7))
        self.assertNotEqual(main.make_dataset(50, 7), main.make_dataset(50, 8))
        self.assertEqual(set(main.make_dataset(1, 7)[0]), set(main.FEATURES + [main.TARGET]))

    def test_training_is_deterministic_and_learning_rate_matters(self):
        rows = main.make_dataset(400, 7)
        first, second = main.train_linear(rows, 0.3, 30), main.train_linear(rows, 0.3, 30)
        self.assertEqual(first.history, second.history)
        self.assertEqual(first.model(), second.model())
        self.assertEqual(len(first.history), 30)
        self.assertLess(first.metrics["rmse"], 0.2)
        self.assertGreater(first.metrics["r2"], 0.95)
        slow = main.train_linear(rows, 0.01, 30)
        self.assertGreater(slow.metrics["rmse"], 1.0)
        self.assertTrue(all(a > b for a, b in zip(slow.history, slow.history[1:])))

    def test_divergence_is_reported_not_hidden(self):
        result = main.train_linear(main.make_dataset(400, 7), 2.5, 30)
        self.assertTrue(result.diverged)
        self.assertEqual(result.metrics, {})
        self.assertLess(len(result.history), 30)

    def test_csv_round_trip_and_prediction(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "houses.csv"
            rows = main.make_dataset(20, 3)
            main.write_csv(rows, path)
            self.assertEqual(main.read_csv(path), rows)
        model = {"features": main.FEATURES, "mean": [0, 0, 0], "std": [1, 1, 1], "weights": [1, 2, 3], "bias": 0.5}
        self.assertEqual(main.predict(model, {"area_m2": 1, "rooms": 1, "age_years": 1}), 6.5)


class FilterTest(unittest.TestCase):
    def test_parses_prefixes_backticks_and_case_insensitive_and(self):
        clauses = main.parse_filter("metrics.rmse < 0.5 and params.lr = '0.1' AND tags.`data.md5` LIKE 'ab%' and run_name != \"x\"")
        self.assertEqual([(c.kind, c.key, c.op, c.value) for c in clauses], [
            ("metric", "rmse", "<", 0.5),
            ("param", "lr", "=", "0.1"),
            ("tag", "data.md5", "LIKE", "ab%"),
            ("attribute", "run_name", "!=", "x"),
        ])
        self.assertEqual(main.parse_filter("tags.mlflow.parentRunId != ''")[0].key, "mlflow.parentRunId")
        self.assertEqual(main.parse_filter("attribute.start_time >= 10")[0].value, 10.0)
        self.assertEqual(main.parse_filter("  "), [])

    def test_rejects_what_mlflow_rejects(self):
        for bad in ["params.lr = 0.1", "metrics.rmse < '0.5'", "params.lr > '0.05'", "metrics.rmse < 0.5 or params.lr = '0.1'", "metrics.rmse <", "colour = 'red'", "model.x = '1'", "metrics.rmse LIKE '1%'"]:
            with self.subTest(bad=bad), self.assertRaises(main.MlflowError) as ctx:
                main.parse_filter(bad)
            self.assertEqual(ctx.exception.code, "INVALID_PARAMETER_VALUE")

    def test_like_is_sql_like(self):
        self.assertTrue(main.compare("lr-0.1", "LIKE", "lr-%"))
        self.assertTrue(main.compare("lr-0.1", "LIKE", "lr-0._"))
        self.assertFalse(main.compare("LR-0.1", "LIKE", "lr-%"))
        self.assertTrue(main.compare("LR-0.1", "ILIKE", "lr-%"))
        self.assertFalse(main.compare("lrX0.1", "LIKE", "lr.%"))
        self.assertFalse(main.compare(None, "!=", "x"))

    def test_order_by_parsing(self):
        keys = main.parse_order_by(["metrics.rmse", "params.lr DESC", "start_time asc"], main.RUN_STRING_ATTRIBUTES + main.RUN_NUMERIC_ATTRIBUTES)
        self.assertEqual([(k.kind, k.key, k.ascending) for k in keys], [("metric", "rmse", True), ("param", "lr", False), ("attribute", "start_time", True)])
        with self.assertRaises(main.MlflowError):
            main.parse_order_by(["metrics.rmse", "metrics.rmse DESC"], ())
        with self.assertRaises(main.MlflowError):
            main.parse_order_by(["colour ASC"], main.RUN_STRING_ATTRIBUTES)


class TrackingStoreTest(StoreCase):
    def test_default_experiment_and_duplicates(self):
        self.assertEqual(self.store.get_experiment("0")["name"], "Default")
        self.assertEqual(self.exp, "1")
        self.assertEqual(self.store.get_experiment_by_name("house")["artifact_location"], "mlflow-artifacts:/1")
        with self.assertRaises(main.MlflowError) as ctx:
            self.store.create_experiment("house", {})
        self.assertEqual(ctx.exception.code, "RESOURCE_ALREADY_EXISTS")
        with self.assertRaises(main.MlflowError) as ctx:
            self.store.get_experiment_by_name("nope")
        self.assertEqual(ctx.exception.status, 404)

    def test_params_are_immutable_tags_are_not(self):
        run_id = self.run_with("a", params={"lr": "0.1"})
        self.store.log_batch(run_id, params=[{"key": "lr", "value": "0.1"}])
        with self.assertRaises(main.MlflowError) as ctx:
            self.store.log_batch(run_id, params=[{"key": "lr", "value": "0.2"}])
        self.assertIn("Changing param values is not allowed", ctx.exception.message)
        self.store.log_batch(run_id, tags=[{"key": "stage", "value": "one"}])
        self.store.log_batch(run_id, tags=[{"key": "stage", "value": "two"}])
        data = self.store.get_run(run_id)["data"]
        self.assertEqual(main.kv_dict(data["params"]), {"lr": "0.1"})
        self.assertEqual(main.kv_dict(data["tags"])["stage"], "two")
        self.assertEqual(main.kv_dict(data["tags"])[main.TAG_RUN_NAME], "a")

    def test_latest_metric_is_highest_step_then_timestamp(self):
        run_id = self.run_with("a", metrics=[metric("rmse", 0.9, 500, 0), metric("rmse", 0.3, 100, 2), metric("rmse", 0.5, 900, 1)])
        latest = {m["key"]: m for m in self.store.get_run(run_id)["data"]["metrics"]}
        self.assertEqual((latest["rmse"]["value"], latest["rmse"]["step"]), (0.3, 2))
        history = self.store.get_metric_history(run_id, "rmse")
        self.assertEqual([(m["timestamp"], m["step"]) for m in history], [(100, 2), (500, 0), (900, 1)])
        self.store.log_batch(run_id, [metric("rmse", 0.3, 100, 2)])
        self.assertEqual(len(self.store.get_metric_history(run_id, "rmse")), 3)

    def test_keys_that_could_escape_are_refused(self):
        run_id = self.run_with("a")
        for key in ["../x", "/abs", "a\nb", ""]:
            with self.subTest(key=key), self.assertRaises(main.MlflowError):
                self.store.log_batch(run_id, params=[{"key": key, "value": "1"}])

    def test_search_filters_order_and_nulls_last(self):
        self.run_with("lr-0.01", {"lr": "0.01"}, [metric("rmse", 2.3, 1, 29)])
        self.run_with("lr-0.1", {"lr": "0.1"}, [metric("rmse", 0.14, 1, 29)])
        self.run_with("lr-0.3", {"lr": "0.3"}, [metric("rmse", 0.12, 1, 29)])
        self.run_with("sweep")
        self.assertEqual(self.names("metrics.rmse < 0.5 and params.lr = '0.1'")[0], ["lr-0.1"])
        self.assertEqual(self.names("", ["metrics.rmse ASC"])[0], ["lr-0.3", "lr-0.1", "lr-0.01", "sweep"])
        self.assertEqual(self.names("", ["metrics.rmse DESC"])[0], ["lr-0.01", "lr-0.1", "lr-0.3", "sweep"])
        self.assertEqual(self.names("params.lr != '0.01'", ["params.lr DESC"])[0], ["lr-0.3", "lr-0.1"])
        self.assertEqual(self.names("")[0], ["sweep", "lr-0.3", "lr-0.1", "lr-0.01"])
        self.assertEqual(self.names("run_name ILIKE 'LR-%'", ["run_name ASC"])[0], ["lr-0.01", "lr-0.1", "lr-0.3"])

    def test_pagination_uses_offset_tokens(self):
        for name in "abcde":
            self.run_with(name)
        pages, token = [], None
        while True:
            page, token = self.names("", ["run_name ASC"], 2, token)
            pages.append(page)
            if not token:
                break
        self.assertEqual(pages, [["a", "b"], ["c", "d"], ["e"]])
        self.assertEqual(json.loads(main.base64.b64decode(main.page_token(4))), {"offset": 4})
        with self.assertRaises(main.MlflowError):
            self.names("", None, 0)
        with self.assertRaises(main.MlflowError):
            self.names("", None, 10, "not-a-token")

    def test_experiment_search(self):
        self.store.create_experiment("churn", {"team": "growth"})
        found, _ = self.store.search_experiments("name LIKE 'c%'", None, None, None)
        self.assertEqual([e["name"] for e in found], ["churn"])
        found, _ = self.store.search_experiments(None, ["name ASC"], None, None)
        self.assertEqual([e["name"] for e in found], ["Default", "churn", "house"])
        found, _ = self.store.search_experiments("tags.team = 'pricing'", None, None, None)
        self.assertEqual([e["name"] for e in found], ["house"])
        found, _ = self.store.search_experiments(None, None, None, None)
        self.assertEqual([e["name"] for e in found], ["churn", "house", "Default"])

    def test_artifacts_stay_inside_the_root(self):
        self.store.put_artifact("1/run/artifacts/model/model.json", b"{}")
        self.assertEqual(self.store.get_artifact("1/run/artifacts/model/model.json"), b"{}")
        self.assertEqual(self.store.list_artifacts("1/run/artifacts"), [{"path": "model", "is_dir": True}])
        self.assertEqual(self.store.list_artifacts("1/run/artifacts/model"), [{"path": "model.json", "is_dir": False, "file_size": 2}])
        self.assertEqual(self.store.list_artifacts("1/missing"), [])
        for bad in ["../escape.txt", "1/%2e%2e/%2e%2e/x", "a/../../x"]:
            with self.subTest(bad=bad), self.assertRaises(main.MlflowError):
                self.store.put_artifact(bad, b"x")
        self.assertFalse((self.root / "escape.txt").exists())


class RegistryTest(StoreCase):
    def setUp(self):
        super().setUp()
        self.run_a = self.run_with("lr-0.1")
        self.run_b = self.run_with("lr-0.3")
        self.store.create_registered_model("house", {"owner": "pricing"}, "prices")

    def test_versions_aliases_and_rollback(self):
        v1 = self.store.create_model_version("house", f"runs:/{self.run_a}/model", self.run_a, {}, None, None)
        v2 = self.store.create_model_version("house", f"runs:/{self.run_b}/model", self.run_b, {"validation": "approved"}, None, None)
        self.assertEqual((v1["version"], v2["version"]), ("1", "2"))
        self.store.set_alias("house", "champion", "2")
        self.store.set_alias("house", "challenger", "1")
        self.assertEqual(self.store.get_model_version_by_alias("house", "champion")["run_id"], self.run_b)
        self.store.set_alias("house", "champion", "1")
        champion = self.store.get_model_version_by_alias("house", "champion")
        self.assertEqual((champion["version"], champion["aliases"]), ("1", ["challenger", "champion"]))
        model = self.store.get_registered_model("house")
        self.assertEqual(model["aliases"], [{"alias": "challenger", "version": "1"}, {"alias": "champion", "version": "1"}])
        self.assertEqual([v["version"] for v in model["latest_versions"]], ["2"])
        self.store.delete_alias("house", "challenger")
        with self.assertRaises(main.MlflowError) as ctx:
            self.store.get_model_version_by_alias("house", "challenger")
        self.assertEqual(ctx.exception.code, "INVALID_PARAMETER_VALUE")
        found, _ = self.store.search_model_versions("tags.validation = 'approved'", None, None, None)
        self.assertEqual([v["version"] for v in found], ["2"])
        found, _ = self.store.search_model_versions("name = 'house'", ["version_number ASC"], None, None)
        self.assertEqual([v["version"] for v in found], ["1", "2"])
        self.assertNotIn("aliases", found[0])

    def test_reserved_aliases_and_missing_versions(self):
        self.store.create_model_version("house", f"runs:/{self.run_a}/model", self.run_a, {}, None, None)
        for alias in ["latest", "LATEST", "v1", "V12"]:
            with self.subTest(alias=alias), self.assertRaises(main.MlflowError):
                self.store.set_alias("house", alias, "1")
        with self.assertRaises(main.MlflowError) as ctx:
            self.store.set_alias("house", "champion", "7")
        self.assertEqual(ctx.exception.code, "RESOURCE_DOES_NOT_EXIST")

    def test_source_must_belong_to_the_run(self):
        cases = [
            (f"runs:/{self.run_a}/model", self.run_b),
            ("/etc/passwd", None),
            (f"runs:/{self.run_a}/../x", self.run_a),
            (f"mlflow-artifacts:/1/{self.run_b}/artifacts/model", self.run_a),
        ]
        for source, run_id in cases:
            with self.subTest(source=source), self.assertRaises(main.MlflowError):
                self.store.create_model_version("house", source, run_id, {}, None, None)
        artifact_uri = self.store.get_run(self.run_a)["info"]["artifact_uri"]
        self.assertEqual(self.store.create_model_version("house", artifact_uri + "/model", self.run_a, {}, None, None)["version"], "1")


class LineageTest(unittest.TestCase):
    def test_git_lineage_records_commit_and_dirty_state(self):
        commit = "a" * 40
        clean = {("rev-parse", "HEAD"): (0, commit), ("status", "--porcelain", "--untracked-files=no"): (0, "")}
        dirty = dict(clean)
        dirty[("status", "--porcelain", "--untracked-files=no")] = (0, " M train.py")

        def runner(table):
            return lambda argv: table[tuple(argv[3:])]

        self.assertEqual(main.git_lineage(Path("."), runner(clean)), {main.TAG_GIT_COMMIT: commit, main.TAG_GIT_DIRTY: "false"})
        self.assertEqual(main.git_lineage(Path("."), runner(dirty))[main.TAG_GIT_DIRTY], "true")
        self.assertEqual(main.git_lineage(Path("."), lambda argv: (128, "fatal: not a git repository")), {})

    def test_data_lineage_prefers_a_matching_dvc_pointer(self):
        with tempfile.TemporaryDirectory() as tmp:
            data = Path(tmp) / "houses.csv"
            data.write_text("area_m2,rooms,age_years,price_100k\n1,1,1,1\n")
            digest = main.file_md5(data)
            self.assertEqual(main.data_lineage(data)[main.TAG_DATA_SOURCE], "file")
            pointer = Path(tmp) / "houses.csv.dvc"
            pointer.write_text(f"outs:\n- md5: {digest}\n  size: 48\n  hash: md5\n  path: houses.csv\n")
            self.assertEqual(main.read_dvc_pointer(pointer), {"md5": digest, "path": "houses.csv"})
            lineage = main.data_lineage(data)
            self.assertEqual((lineage[main.TAG_DATA_MD5], lineage[main.TAG_DATA_SOURCE]), (digest, "dvc"))
            data.write_text("area_m2,rooms,age_years,price_100k\n2,2,2,2\n")
            with self.assertRaisesRegex(ValueError, "does not match its pointer"):
                main.data_lineage(data)

    def test_compare_warns_when_data_versions_differ(self):
        def run(name, md5, rmse, lr):
            tags = [{"key": main.TAG_DATA_MD5, "value": md5}, {"key": main.TAG_GIT_COMMIT, "value": "c" * 40}]
            return {"info": {"run_id": name, "run_name": name, "status": "FINISHED"}, "data": {"metrics": [{"key": "rmse", "value": rmse}], "params": [{"key": "lr", "value": lr}, {"key": "epochs", "value": "30"}], "tags": tags}}

        summary = main.summarize_runs([run("a", "d1", 0.2, "0.1"), run("b", "d1", 0.1, "0.3"), run("c", "d2", 0.05, "0.3")], "rmse")
        self.assertEqual(summary["varying_params"], ["lr"])
        self.assertEqual([(g["data_md5"], g["best"]) for g in summary["groups"]], [("d1", "b"), ("d2", "c")])
        self.assertTrue(any("different data versions" in w for w in summary["warnings"]))
        self.assertIn("best by rmse for data d1", main.render_compare(summary))


class ServerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.store = main.Store(str(self.root / "track.db"), str(self.root / "artifacts"))
        self.server, self.thread = main.start_server(self.store)
        self.client = main.TrackingClient(self.server.url)

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=10)
        self.store.close()
        self.tmp.cleanup()

    def test_sweep_register_and_resolve_over_http(self):
        data = self.root / "houses.csv"
        main.write_csv(main.make_dataset(200, 7), data)
        lineage = main.data_lineage(data)
        lineage[main.TAG_GIT_COMMIT] = "b" * 40
        summary = main.train_sweep(self.client, "house", data, [0.01, 0.3, 2.5], 20, "sweep", lineage, register="house", alias="champion")
        self.assertEqual([r["status"] for r in summary["runs"]], ["FINISHED", "FINISHED", "FAILED"])
        self.assertEqual(summary["best"]["run_name"], "lr-0.3")
        self.assertEqual(summary["registered"]["version"], "1")
        experiment_id = self.client.experiment_id("house", create=False)
        runs = self.client.search_runs([experiment_id], "metrics.rmse < 0.5", ["metrics.rmse ASC"], page_size=1)
        self.assertEqual([r["info"]["run_name"] for r in runs], ["lr-0.3"])
        tags = main.kv_dict(runs[0]["data"]["tags"])
        self.assertEqual(tags[main.TAG_PARENT], summary["parent_run_id"])
        self.assertEqual(tags[main.TAG_DATA_MD5], main.file_md5(data))
        self.assertEqual(len(self.client.metric_history(runs[0]["info"]["run_id"], "rmse")), 20)
        resolved = main.resolve_model(self.client, "models:/house@champion")
        self.assertEqual((resolved["version"], resolved["aliases"]), ("1", ["champion"]))
        model = json.loads(resolved["files"]["model/model.json"])
        self.assertAlmostEqual(main.predict(model, {"area_m2": 95.0, "rooms": 3.0, "age_years": 20.0}), 2.70, delta=0.1)
        self.assertEqual(main.resolve_model(self.client, "models:/house/1")["run_id"], runs[0]["info"]["run_id"])
        failed = [r for r in summary["runs"] if r["status"] == "FAILED"][0]
        self.assertEqual(self.client.list_artifacts(self.client.call("GET", "runs/get", {"run_id": failed["run_id"]})["run"]["info"]["artifact_uri"]), [])

    def test_errors_use_mlflow_codes_and_unknown_endpoints_are_recorded(self):
        with self.assertRaises(main.MlflowError) as ctx:
            self.client.call("GET", "runs/get", {"run_id": "0" * 32})
        self.assertEqual(ctx.exception.code, "RESOURCE_DOES_NOT_EXIST")
        with self.assertRaises(main.MlflowError) as ctx:
            self.client.call("POST", "runs/search", {"experiment_ids": ["0"], "filter": "params.lr = 0.1"})
        self.assertEqual(ctx.exception.code, "INVALID_PARAMETER_VALUE")
        err = io.StringIO()
        with redirect_stderr(err), self.assertRaises(main.MlflowError) as ctx:
            self.client.call("POST", "gateway/routes", {})
        self.assertEqual(ctx.exception.code, "ENDPOINT_NOT_FOUND")
        self.assertIn("no route for POST /api/2.0/mlflow/gateway/routes", err.getvalue())
        self.assertEqual(self.server.unknown, ["POST /api/2.0/mlflow/gateway/routes"])
        self.assertEqual(self.client.call("POST", "logged-models/search", {"experiment_ids": ["0"]}), {})

    def test_get_requests_accept_query_lists_and_chunked_uploads(self):
        self.assertEqual(main.request_args("experiment_ids=1&experiment_ids=2&order_by=x&name=a", b'{"max_results": 5}'), {"experiment_ids": ["1", "2"], "order_by": ["x"], "name": "a", "max_results": 5})
        host, port = self.server.server_address[:2]
        connection = http.client.HTTPConnection(host, port, timeout=10)
        try:
            connection.putrequest("PUT", main.ARTIFACTS_API + "/0/run/artifacts/notes.txt")
            connection.putheader("Transfer-Encoding", "chunked")
            connection.endheaders()
            for chunk in (b"hello ", b"world"):
                connection.send(b"%x\r\n%s\r\n" % (len(chunk), chunk))
            connection.send(b"0\r\n\r\n")
            self.assertEqual(connection.getresponse().status, 200)
        finally:
            connection.close()
        self.assertEqual(self.store.get_artifact("0/run/artifacts/notes.txt"), b"hello world")


class CliTest(unittest.TestCase):
    def run_cli(self, argv):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = main.main(argv)
        return code, out.getvalue(), err.getvalue()

    def test_exit_codes(self):
        with tempfile.TemporaryDirectory() as tmp:
            code, _, err = self.run_cli(["train", "--data", str(Path(tmp) / "missing.csv")])
            self.assertEqual(code, 2)
            self.assertIn("make-data", err)
            code, out, _ = self.run_cli(["make-data", "--rows", "30", "--out", str(Path(tmp) / "h.csv")])
            self.assertEqual(code, 0)
            self.assertIn("30 rows", out)
        code, _, err = self.run_cli(["resolve", "models:/house", "--tracking-uri", "http://127.0.0.1:9"])
        self.assertEqual(code, 2)
        self.assertIn("models:/<name>@<alias>", err)
        code, _, err = self.run_cli(["compare", "--tracking-uri", "http://127.0.0.1:9"])
        self.assertEqual(code, 1)
        self.assertIn("cannot reach", err)


if __name__ == "__main__":
    unittest.main()
