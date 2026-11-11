import io
import json
import socket
import sys
import tempfile
import textwrap
import threading
import unittest
from contextlib import redirect_stderr, redirect_stdout
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

CODE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(CODE))

import main  # noqa: E402

PIPELINE = CODE / "pipeline.py"
FIXTURES = CODE / "fixtures"

SMALL = textwrap.dedent('''
    from minipipe import dsl


    @dsl.component
    def produce(n: int, data: dsl.Output[dsl.Dataset]) -> int:
        with open(data.path, "w") as handle:
            handle.write(str(n * 2))
        return n * 2


    @dsl.component
    def consume(data: dsl.Input[dsl.Dataset], bonus: int) -> int:
        with open(data.path) as handle:
            return int(handle.read()) + bonus


    @dsl.component
    def flaky(tag: str) -> str:
        import os
        marker = os.path.join(os.environ["MINIPIPE_STORE"], "flaky-" + tag)
        if not os.path.exists(marker):
            open(marker, "w").close()
            raise RuntimeError("the first attempt fails")
        return tag


    @dsl.component
    def broken(tag: str) -> str:
        raise RuntimeError("always fails: " + tag)


    @dsl.component
    def folder(model: dsl.Output[dsl.Model]):
        import os
        os.makedirs(model.path)
        for name in ("b.txt", "a.txt"):
            with open(os.path.join(model.path, name), "w") as handle:
                handle.write(name)


    @dsl.pipeline(name="small")
    def small(n: int = 3, bonus: int = 1):
        first = produce(n=n)
        second = consume(data=first.outputs["data"], bonus=bonus)
        second.set_caching_options(False)


    @dsl.pipeline(name="retrying")
    def retrying(tag: str = "a"):
        task = flaky(tag=tag)
        task.set_retry(num_retries=2)


    @dsl.pipeline(name="impatient")
    def impatient(tag: str = "a"):
        task = broken(tag=tag)
        task.set_retry(num_retries=3, backoff_duration="5s", backoff_max_duration="1s")


    @dsl.pipeline(name="failing")
    def failing(tag: str = "a"):
        bad = broken(tag=tag)
        bad.set_retry(num_retries=1)
        later = flaky(tag=tag)
        later.after(bad)
        folder()
''')


def shape(ir):
    executors = {k: {"image": v["container"]["image"], "args": v["container"]["args"]} for k, v in ir["deploymentSpec"]["executors"].items()}
    return {"components": ir["components"], "root": ir["root"], "pipelineInfo": ir["pipelineInfo"], "executors": executors}


def quiet():
    return lambda line: None


class Workspace(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.small_file = self.dir / "small_pipelines.py"
        self.small_file.write_text(SMALL, encoding="utf-8")

    def tearDown(self):
        self.tmp.cleanup()

    def run_pipeline(self, path, name=None, store="store", params=None, **options):
        ir = main.load_ir(path, name)
        runner = main.LocalRunner(ir, main.Store(self.dir / store), log=quiet(), **options)
        return runner.run(params or {})

    def statuses(self, report):
        return {t["path"]: t["status"] for t in report["tasks"]}


class CompilerTests(Workspace):
    def test_house_prices_ir_equals_what_kfp_2_18_compiles(self):
        ours = main.compile_pipeline(main.load_pipeline(PIPELINE))
        theirs = json.loads((FIXTURES / "kfp-2.18.0" / "house-prices.json").read_text())
        self.assertEqual(shape(ours), shape(theirs))

    def test_conditions_loops_after_and_caching_options_match_kfp(self):
        ours = main.compile_pipeline(main.load_pipeline(FIXTURES / "features_pipeline.py"))
        theirs = json.loads((FIXTURES / "kfp-2.18.0" / "features.json").read_text())
        self.assertEqual(shape(ours), shape(theirs))
        tasks = ours["root"]["dag"]["tasks"]
        self.assertEqual(tasks["count-2"]["dependentTasks"], ["count"])
        self.assertEqual(tasks["count-2"]["cachingOptions"], {})
        self.assertIn("double(int(", tasks["condition-7"]["triggerPolicy"]["condition"])

    def test_reused_components_get_numbered_task_names(self):
        ir = main.compile_pipeline(main.load_pipeline(FIXTURES / "features_pipeline.py"))
        names = sorted(row["task"].split("/")[-1] for row in main.flatten(ir))
        self.assertEqual(names, ["check", "count", "count-2", "count-3", "count-4", "count-5", "scale", "scale-2"])

    def test_executor_carries_the_function_source_and_the_kfp_argument_shape(self):
        ir = main.compile_pipeline(main.load_pipeline(PIPELINE))
        container = ir["deploymentSpec"]["executors"]["exec-train-model"]["container"]
        self.assertEqual(container["args"], ["--executor_input", "{{$}}", "--function_to_execute", "train_model"])
        self.assertTrue(container["command"][3].startswith("def train_model("))
        self.assertNotIn("@dsl.component", container["command"][3])

    def test_wiring_mistakes_are_compile_errors(self):
        pipe = main.load_pipeline(self.small_file, "small")
        produce = pipe.func.__globals__["produce"]
        consume = pipe.func.__globals__["consume"]
        cases = {
            "missing argument 'n'": lambda: produce(),
            "unknown argument 'm'": lambda: produce(n=1, m=2),
            "expects NUMBER_INTEGER": lambda: produce(n="three"),
            "is an input artifact": lambda: consume(data=3, bonus=1),
            "has 2 outputs": lambda: produce(n=1).output,
            "branch with dsl.If": lambda: bool(produce(n=1).outputs["Output"]),
        }
        for message, build in cases.items():
            with self.subTest(message=message):
                builder = main.Builder()
                main.BUILDERS.append(builder)
                try:
                    with self.assertRaises(main.PipelineError) as caught:
                        build()
                finally:
                    main.BUILDERS.pop()
                self.assertIn(message, str(caught.exception))

    def test_loop_outputs_cannot_leave_the_loop_and_groups_do_not_nest(self):
        source = SMALL + textwrap.dedent('''
            @dsl.pipeline(name="leaky")
            def leaky():
                with dsl.ParallelFor(items=[1, 2]) as n:
                    inner = produce(n=n)
                consume(data=inner.outputs["data"], bonus=1)


            @dsl.pipeline(name="nested")
            def nested(k: int = 1):
                with dsl.If(k == 1):
                    with dsl.ParallelFor(items=[1, 2]) as n:
                        produce(n=n)
        ''')
        path = self.dir / "bad.py"
        path.write_text(source, encoding="utf-8")
        with self.assertRaisesRegex(main.PipelineError, "produced inside for-loop-2"):
            main.load_ir(path, "leaky")
        with self.assertRaisesRegex(main.PipelineError, "one level"):
            main.load_ir(path, "nested")

    def test_conditions_evaluate_like_cel_for_the_compiled_forms(self):
        values = {"pipelinechannel--a": 0.3, "pipelinechannel--b": 0.5, "pipelinechannel--n": 2.0, "pipelinechannel--mode": "fast"}
        cases = {
            "inputs.parameter_values['pipelinechannel--a'] < inputs.parameter_values['pipelinechannel--b']": True,
            "int(inputs.parameter_values['pipelinechannel--n']) == 2": True,
            "inputs.parameter_values['pipelinechannel--b'] >= double(int(inputs.parameter_values['pipelinechannel--n']))": False,
            "inputs.parameter_values['pipelinechannel--mode'] == 'fast'": True,
            "inputs.parameter_values['pipelinechannel--mode'] != 'fast' || int(inputs.parameter_values['pipelinechannel--n']) > 1": True,
            "inputs.parameter_values['pipelinechannel--a'] > 0.1 && inputs.parameter_values['pipelinechannel--a'] > 0.4": False,
        }
        for text, expected in cases.items():
            with self.subTest(text=text):
                self.assertIs(main.evaluate_condition(text, values), expected)
        with self.assertRaisesRegex(main.PipelineError, "not an input"):
            main.evaluate_condition("inputs.parameter_values['missing'] == 1", values)

    def test_flatten_reads_kfp_ir_and_minipipe_ir_the_same_way(self):
        ours = main.flatten(main.compile_pipeline(main.load_pipeline(PIPELINE)))
        theirs = main.flatten(json.loads((FIXTURES / "kfp-2.18.0" / "house-prices.json").read_text()))
        self.assertEqual(ours, theirs)
        loop = next(r for r in ours if r["task"] == "for-loop-3/score-slice")
        self.assertEqual(loop["deps"], ["make-dataset", "train-model"])
        self.assertEqual(loop["inputs"]["segment"], 'loop item of ["low", "mid", "high"]')


class RunnerTests(Workspace):
    def test_second_run_is_all_cache_hits_with_identical_outputs(self):
        first = self.run_pipeline(PIPELINE)
        second = self.run_pipeline(PIPELINE)
        self.assertEqual(set(self.statuses(first).values()), {"executed"})
        self.assertEqual(set(self.statuses(second).values()), {"cached"})
        self.assertEqual(len(first["tasks"]), 7)
        outputs = lambda report: {t["path"]: (t["cache_key"], t["parameters"], t["artifacts"]) for t in report["tasks"]}
        self.assertEqual(outputs(first), outputs(second))
        rmse = next(t for t in first["tasks"] if t["task"] == "evaluate-model")["parameters"]["Output"]
        self.assertLess(rmse, 0.5)

    def test_tasks_start_after_their_dependencies_finish(self):
        report = self.run_pipeline(PIPELINE, parallelism=3)
        deps = {r["task"].split("/")[-1]: [d.split("/")[-1] for d in r["deps"]] for r in main.flatten(main.load_ir(PIPELINE))}
        for task in report["tasks"]:
            for dep in deps[task["task"]]:
                for upstream in (t for t in report["tasks"] if t["task"] == dep):
                    self.assertLessEqual(upstream["finished"], task["started"], (task["path"], dep))

    def test_a_parameter_change_reruns_only_downstream_tasks_and_explain_names_it(self):
        self.run_pipeline(PIPELINE)
        report = self.run_pipeline(PIPELINE, params={"lr": "0.4"})
        status = self.statuses(report)
        self.assertEqual(status["make-dataset"], "cached")
        self.assertEqual(status["train-model"], "executed")
        self.assertEqual(status["for-loop-3[1]/score-slice"], "executed")
        lines = main.explain(main.Store(self.dir / "store"), "train-model")
        self.assertIn("  parameters.lr: 0.5 -> 0.4", lines)
        lines = main.explain(main.Store(self.dir / "store"), "make-dataset")
        self.assertTrue(any("same key" in line for line in lines), lines)

    def test_a_false_condition_skips_the_group(self):
        report = self.run_pipeline(PIPELINE, params={"max_rmse": "0.1"})
        status = self.statuses(report)
        self.assertEqual(status["condition-1/register-model"], "skipped")
        self.assertEqual(report["status"], "succeeded")
        skipped = next(t for t in report["tasks"] if t["status"] == "skipped")
        self.assertIn("condition false", skipped["reason"])

    def test_a_changed_artifact_on_disk_turns_a_hit_into_a_miss(self):
        first = self.run_pipeline(PIPELINE)
        model = next(t for t in first["tasks"] if t["task"] == "train-model")["artifacts"]["model"]
        path = main.Store(self.dir / "store").local(model["uri"])
        path.write_text('{"tampered": true}')
        second = self.run_pipeline(PIPELINE)
        train = next(t for t in second["tasks"] if t["task"] == "train-model")
        self.assertEqual(train["status"], "executed")
        self.assertIn("artifact model changed on disk", train["reason"])
        self.assertEqual(train["artifacts"]["model"]["sha256"], model["sha256"])

    def test_a_task_with_caching_disabled_always_runs(self):
        self.run_pipeline(self.small_file, "small")
        report = self.run_pipeline(self.small_file, "small")
        self.assertEqual(self.statuses(report), {"produce": "cached", "consume": "executed"})
        consume = next(t for t in report["tasks"] if t["task"] == "consume")
        self.assertEqual(consume["parameters"]["Output"], 7)
        self.assertEqual(consume["reason"], "caching disabled for this task")

    def test_editing_a_component_body_changes_its_cache_key(self):
        self.run_pipeline(self.small_file, "small")
        self.small_file.write_text(SMALL.replace("str(n * 2)", "str(n * 2 + 0)"), encoding="utf-8")
        report = self.run_pipeline(self.small_file, "small")
        self.assertEqual(self.statuses(report)["produce"], "executed")
        lines = main.explain(main.Store(self.dir / "store"), "produce")
        self.assertTrue(any(line.startswith("  container.command[3]: text changed") for line in lines), lines)

    def test_retries_until_the_step_succeeds(self):
        report = self.run_pipeline(self.small_file, "retrying")
        task = report["tasks"][0]
        self.assertEqual((task["status"], task["attempts"], task["parameters"]["Output"]), ("executed", 2, "a"))

    def test_exhausted_retries_fail_the_run_and_block_downstream(self):
        report = self.run_pipeline(self.small_file, "failing")
        status = self.statuses(report)
        self.assertEqual(status, {"broken": "failed", "flaky": "upstream-failed", "folder": "executed"})
        broken = next(t for t in report["tasks"] if t["task"] == "broken")
        self.assertEqual(broken["attempts"], 2)
        self.assertIn("always fails: a", broken["log"])
        self.assertEqual(report["status"], "failed")

    def test_the_backoff_window_caps_the_total_retry_time(self):
        report = self.run_pipeline(self.small_file, "impatient")
        task = report["tasks"][0]
        self.assertEqual((task["status"], task["attempts"]), ("failed", 1))

    def test_directory_artifacts_hash_the_same_inside_and_outside_the_step(self):
        report = self.run_pipeline(self.small_file, "failing")
        folder = next(t for t in report["tasks"] if t["task"] == "folder")
        path = main.Store(self.dir / "store").local(folder["artifacts"]["model"]["uri"])
        self.assertTrue(path.is_dir())
        self.assertEqual(main.digest(path), folder["artifacts"]["model"]["sha256"])
        self.assertTrue(folder["artifacts"]["model"]["sha256"].startswith("dir-"))

    def test_executor_input_has_the_kfp_shape(self):
        ir = main.load_ir(PIPELINE)
        runner = main.LocalRunner(ir, main.Store(self.dir / "store"), log=quiet())
        comp = ir["components"]["comp-train-model"]
        refs = {"train_data": {"uri": "minipipe://artifacts/x/train_data", "sha256": "ab"}}
        payload = runner.executor_input(comp, "train-model", "f" * 64, {"lr": 0.5, "epochs": 3}, refs, self.dir / "work")
        self.assertEqual(payload["inputs"]["parameterValues"], {"lr": 0.5, "epochs": 3})
        self.assertEqual(payload["inputs"]["artifacts"]["train_data"]["artifacts"][0]["uri"], "minipipe://artifacts/x/train_data")
        self.assertEqual(payload["outputs"]["artifacts"]["model"]["artifacts"][0]["uri"], "minipipe://artifacts/train-model/ffffffffffffffff/model")
        self.assertIn("Output", payload["outputs"]["parameters"])


class FakeMlflow(BaseHTTPRequestHandler):
    calls = []
    lock = threading.Lock()

    def log_message(self, *args):
        pass

    def reply(self, code, body):
        data = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        with self.lock:
            self.calls.append(("GET", self.path, None))
        self.reply(404, {"error_code": "RESOURCE_DOES_NOT_EXIST"})

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        with self.lock:
            self.calls.append(("POST", self.path, body))
            count = len(self.calls)
        if self.path.endswith("experiments/create"):
            self.reply(200, {"experiment_id": "7"})
        elif self.path.endswith("runs/create"):
            self.reply(200, {"run": {"info": {"run_id": f"run{count}"}}})
        else:
            self.reply(200, {})


class LineageTests(Workspace):
    def test_every_task_becomes_a_child_run_with_input_and_output_digests(self):
        FakeMlflow.calls = []
        server = ThreadingHTTPServer(("127.0.0.1", 0), FakeMlflow)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            url = f"http://127.0.0.1:{server.server_address[1]}"
            tracker = main.Tracker(url, "house-prices", quiet())
            report = self.run_pipeline(PIPELINE, tracker=tracker)
        finally:
            server.shutdown()
            server.server_close()
            thread.join()
        self.assertEqual(report["status"], "succeeded")
        creates = [body for method, path, body in FakeMlflow.calls if path.endswith("runs/create")]
        self.assertEqual(len(creates), 8)
        parent = creates[0]
        self.assertEqual(parent["experiment_id"], "7")
        children = {c["run_name"]: {t["key"]: t["value"] for t in c["tags"]} for c in creates[1:]}
        evaluate = children["evaluate-model"]
        train = next(t for t in report["tasks"] if t["task"] == "train-model")
        self.assertEqual(evaluate["minipipe.input.model"], "sha256:" + train["artifacts"]["model"]["sha256"])
        self.assertTrue(evaluate["mlflow.parentRunId"].startswith("run"))
        updates = [body for method, path, body in FakeMlflow.calls if path.endswith("runs/update")]
        self.assertEqual({u["status"] for u in updates}, {"FINISHED"})
        batches = [body for method, path, body in FakeMlflow.calls if path.endswith("runs/log-batch") and body.get("metrics")]
        self.assertTrue(any(m["key"] == "Output" for b in batches for m in b["metrics"]))

    def test_an_unreachable_tracking_server_only_costs_a_message(self):
        probe = socket.socket()
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
        probe.close()
        messages = []
        tracker = main.Tracker(f"http://127.0.0.1:{port}", "house-prices", messages.append, timeout=2)
        report = self.run_pipeline(PIPELINE, tracker=tracker)
        self.assertEqual(report["status"], "succeeded")
        self.assertEqual(len(messages), 1)
        self.assertIn("continuing without lineage", messages[0])


class ArgoTests(Workspace):
    def setUp(self):
        super().setUp()
        self.workflow = main.to_argo(main.load_ir(PIPELINE), service_account="minipipe")
        self.templates = {t["name"]: t for t in self.workflow["spec"]["templates"]}

    def test_the_root_dag_mirrors_the_ir_dependencies_and_condition(self):
        root = {t["name"]: t for t in self.templates["root"]["dag"]["tasks"]}
        self.assertEqual(root["evaluate-model"]["dependencies"], ["make-dataset", "train-model"])
        self.assertEqual(root["condition-1"]["when"], "{{tasks.evaluate-model.outputs.parameters.Output}} < {{workflow.parameters.max_rmse}}")
        self.assertEqual(root["for-loop-3"]["template"], "for-loop-3-fanout")
        self.assertEqual(self.templates["for-loop-3-fanout"]["parallelism"], 2)
        self.assertEqual(self.templates["for-loop-3-fanout"]["dag"]["tasks"][0]["withItems"], ["low", "mid", "high"])
        self.assertEqual(self.workflow["spec"]["serviceAccountName"], "minipipe")
        self.assertEqual({p["name"]: p["value"] for p in self.workflow["spec"]["arguments"]["parameters"]}["rows"], "200")

    def test_retries_and_artifact_references_become_argo_fields(self):
        train = self.templates["exec-train-model-retry-2"]
        self.assertEqual(train["retryStrategy"]["limit"], 2)
        outputs = {p["name"] for p in train["outputs"]["parameters"]}
        self.assertEqual(outputs, {"Output", "model-uri", "model-sha256"})
        executor_input = json.loads(train["container"]["args"][1])
        self.assertEqual(executor_input["inputs"]["artifacts"]["train_data"]["artifacts"][0]["uri"], "{{inputs.parameters.train_data}}")
        self.assertIn("{{pod.name}}", executor_input["outputs"]["artifacts"]["model"]["artifacts"][0]["uri"])
        self.assertEqual(train["container"]["volumeMounts"], [{"name": "minipipe-store", "mountPath": main.STORE_MOUNT}])

    def test_every_template_variable_is_declared_by_its_template(self):
        for template in self.workflow["spec"]["templates"]:
            declared = {p["name"] for p in template.get("inputs", {}).get("parameters", [])}
            used = set(main.re.findall(r"\{\{inputs\.parameters\.([^}]+)\}\}", json.dumps(template.get("container", {}))))
            with self.subTest(template=template["name"]):
                self.assertLessEqual(used, declared)

    def test_kfp_ir_is_refused_because_its_executor_needs_the_kfp_backend(self):
        kfp_ir = json.loads((FIXTURES / "kfp-2.18.0" / "house-prices.json").read_text())
        with self.assertRaisesRegex(main.PipelineError, "KFP backend"):
            main.to_argo(kfp_ir)


class CliTests(Workspace):
    def cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = main.main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def test_compile_dag_argo_and_run_from_the_command_line(self):
        ir_path = self.dir / "ir.json"
        code, out, _ = self.cli("compile", str(PIPELINE), "-o", str(ir_path))
        self.assertEqual((code, out.strip()), (0, f"compiled house-prices: 5 tasks -> {ir_path}"))
        code, out, _ = self.cli("dag", str(ir_path))
        self.assertEqual(code, 0)
        self.assertIn("condition-1/register-model  after [evaluate-model, train-model]", out)
        code, out, _ = self.cli("argo", str(ir_path))
        self.assertEqual((code, json.loads(out)["kind"]), (0, "Workflow"))
        code, out, _ = self.cli("run", str(ir_path), "--store", str(self.dir / "store"), "--param", "rows=100")
        self.assertEqual(code, 0)
        self.assertIn("summary: 7 executed", out)

    def test_bad_input_exits_2_and_a_failed_run_exits_1(self):
        code, _, err = self.cli("run", str(PIPELINE), "--store", str(self.dir / "s"), "--param", "speed=3")
        self.assertEqual(code, 2)
        self.assertIn("unknown pipeline parameter(s) ['speed']", err)
        code, _, err = self.cli("compile", str(self.small_file))
        self.assertEqual(code, 2)
        self.assertIn("choose one with --pipeline", err)
        code, out, _ = self.cli("run", str(self.small_file), "--pipeline", "failing", "--store", str(self.dir / "f"))
        self.assertEqual(code, 1)
        self.assertIn("upstream-failed", out)


if __name__ == "__main__":
    unittest.main()
