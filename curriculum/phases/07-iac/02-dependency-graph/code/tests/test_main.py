import contextlib
import copy
import io
import json
import os
import sys
import tempfile
import threading
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import main  # noqa: E402

LESSON_CONFIG = {
    "terraform": {"required_providers": {"local": {"source": "hashicorp/local"}}},
    "locals": {"out_dir": "${path.module}/out"},
    "resource": {
        "random_pet": {"name": {"length": 2}},
        "time_sleep": {
            "warm_a": {"create_duration": "10ms"},
            "warm_b": {"create_duration": "10ms"},
            "warm_c": {"create_duration": "10ms"},
            "warm_d": {"create_duration": "10ms"},
        },
        "terraform_data": {"release": {"input": "${random_pet.name.id}"}},
        "local_file": {
            "config": {"filename": "${local.out_dir}/config.txt", "content": "release=${terraform_data.release.output}\n"},
            "index": {
                "filename": "${local.out_dir}/index.html",
                "content": "see ${local_file.config.filename}\n",
                "depends_on": ["time_sleep.warm_a", "time_sleep.warm_b"],
            },
            "versioned": {"filename": "${local.out_dir}/v1.txt", "content": "build 1\n", "lifecycle": {"create_before_destroy": True}},
            "pointer": {"filename": "${local.out_dir}/pointer.txt", "content": "current=${local_file.versioned.filename}\n"},
        },
    },
    "output": {"pet": {"value": "${random_pet.name.id}"}},
}

CYCLE_CONFIG = {
    "resource": {
        "terraform_data": {
            "a": {"input": "${terraform_data.c.output}"},
            "b": {"input": "${terraform_data.a.output}"},
            "c": {"input": "x", "depends_on": ["terraform_data.b"]},
            "d": {"input": "${terraform_data.a.id}"},
        }
    }
}


def position(lines, prefix):
    for index, line in enumerate(lines):
        if line.startswith(prefix):
            return index
    raise AssertionError(f"no line starts with {prefix!r} in:\n" + "\n".join(lines))


class LessonDir(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = self.tmp.name
        self.config = copy.deepcopy(LESSON_CONFIG)
        self.write()

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, config=None):
        with open(os.path.join(self.root, "main.tf.json"), "w", encoding="utf-8") as handle:
            json.dump(self.config if config is None else config, handle)

    def run_cli(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = main.main(["-chdir", self.root, "--seed", "1", *args])
        return code, out.getvalue(), err.getvalue()

    def plan_changes(self):
        config, graph = main.build(self.root)
        state = main.load_state(os.path.join(self.root, main.STATE_FILE))
        changes = main.plan(config, graph, state, main.refresh(state, main.Provider(self.root)))
        return {change.address: change for change in changes}

    def resource_edges(self):
        _, graph = main.build(self.root)
        return [(node, dep) for node, found in main.resource_graph(graph).items() for dep in found]

    def read(self, name):
        with open(os.path.join(self.root, "out", name), encoding="utf-8") as handle:
            return handle.read()


class GraphTests(LessonDir):
    def test_references_locals_and_depends_on_become_edges(self):
        _, graph = main.build(self.root)
        self.assertEqual(graph.deps["terraform_data.release"], {"random_pet.name"})
        self.assertEqual(graph.deps["local_file.config"], {"local.out_dir", "terraform_data.release"})
        self.assertEqual(graph.deps["local_file.index"], {"local.out_dir", "local_file.config", "time_sleep.warm_a", "time_sleep.warm_b"})
        self.assertEqual(graph.deps["output.pet"], {"random_pet.name"})
        self.assertEqual(graph.deps["local.out_dir"], set())
        self.assertIn(("local_file.index", "time_sleep.warm_a"), graph.explicit)
        resources = main.resource_graph(graph)
        self.assertEqual(resources["local_file.config"], {"terraform_data.release"})
        self.assertEqual(resources["local_file.versioned"], set())
        self.assertNotIn("output.pet", resources)

    def test_templates_escape_dollars_and_a_pure_reference_keeps_its_type(self):
        self.assertEqual(main.template_parts("a $${b} ${local.x}", "t"), [("text", "a ${b} "), ("ref", "local.x")])
        evaluator = main.Evaluator(main.Config(locals={"obj": {"k": [1, 2]}, "n": 3, "flag": True}), {})
        self.assertEqual(evaluator.evaluate("${local.obj}", "t"), {"k": [1, 2]})
        self.assertEqual(evaluator.evaluate("n=${local.n} ${local.flag}", "t"), "n=3 true")
        self.assertIs(main.Evaluator(main.Config(), {"random_pet.p": {"id": main.UNKNOWN}}).evaluate("x-${random_pet.p.id}", "t"), main.UNKNOWN)
        with self.assertRaisesRegex(main.ConfigError, "list or object"):
            evaluator.evaluate("${local.obj}-suffix", "t")

    def test_bad_references_fail_with_the_address_and_the_reason(self):
        cases = [
            ({"local_file": {"a": {"filename": "x", "content": "${local_file.b.id}"}}}, "undeclared resource local_file.b"),
            ({"local_file": {"a": {"filename": "x"}, "b": {"filename": "y", "content": "${local_file.a.size}"}}}, 'unsupported attribute "size"'),
            ({"local_file": {"a": {"filename": "${var.name}"}}}, "var.* references are not supported"),
            ({"local_file": {"a": {"filename": "${upper(local.x)}"}}}, "plain references"),
            ({"local_file": {"a": {"filename": "x", "content": "${local_file.a.id}"}}}, "self-referential"),
            ({"local_file": {"a": {"filename": "x", "depends_on": ["local_file.zzz"]}}}, "not a declared resource"),
            ({"local_file": {"a": {"filename": "x", "count": 2}}}, "count is not supported"),
            ({"local_file": {"a": {"filename": "x", "mode": "0644"}}}, 'argument named "mode"'),
            ({"aws_instance": {"a": {}}}, "not implemented"),
        ]
        for resources, message in cases:
            with self.subTest(message=message):
                self.write({"resource": resources})
                with self.assertRaisesRegex(main.ConfigError, message):
                    main.build(self.root)

    def test_a_cycle_is_reported_as_a_path_and_validate_exits_1(self):
        self.write(CYCLE_CONFIG)
        _, graph = main.build(self.root)
        path = main.find_cycle(graph.deps)
        self.assertEqual(path, ["terraform_data.a", "terraform_data.c", "terraform_data.b", "terraform_data.a"])
        with self.assertRaises(main.CycleError):
            main.waves(graph.deps)
        code, _, err = self.run_cli("validate")
        self.assertEqual(code, 1)
        self.assertIn("Cycle: terraform_data.a -> terraform_data.c -> terraform_data.b -> terraform_data.a", err)
        self.assertNotIn("terraform_data.d", err)
        code, out, _ = self.run_cli("graph", "-draw-cycles")
        self.assertEqual(code, 1)
        self.assertIn('"terraform_data.b" -> "terraform_data.a" [color = "red"]', out)
        self.assertIn('"terraform_data.d" -> "terraform_data.a"\n', out)

    def test_the_shipped_fixtures_load_and_the_cycle_fixture_is_rejected(self):
        fixtures = os.path.join(os.path.dirname(__file__), "..", "fixtures")
        _, graph = main.build(os.path.join(fixtures, "demo"))
        _, test_graph = main.build(self.root)
        self.assertEqual(main.resource_graph(graph), main.resource_graph(test_graph))
        _, cycle = main.build(os.path.join(fixtures, "cycle"))
        self.assertEqual(main.find_cycle(cycle.deps), ["terraform_data.a", "terraform_data.c", "terraform_data.b", "terraform_data.a"])

    def test_waves_and_critical_path(self):
        _, graph = main.build(self.root)
        deps = main.resource_graph(graph)
        self.assertEqual(main.waves(deps), [
            ["local_file.versioned", "random_pet.name", "time_sleep.warm_a", "time_sleep.warm_b", "time_sleep.warm_c", "time_sleep.warm_d"],
            ["local_file.pointer", "terraform_data.release"],
            ["local_file.config"],
            ["local_file.index"],
        ])
        self.assertEqual(main.critical_path(deps), ["random_pet.name", "terraform_data.release", "local_file.config", "local_file.index"])
        code, out, _ = self.run_cli("order")
        self.assertEqual(code, 0)
        self.assertIn("critical path (4 steps): random_pet.name -> terraform_data.release", out)

    def test_transitive_reduction_drops_edges_that_other_paths_imply(self):
        deps = {"c": {"a", "b"}, "b": {"a"}, "a": set()}
        reduced = main.transitive_reduction(deps)
        self.assertEqual(reduced, {"c": {"b"}, "b": {"a"}, "a": set()})
        dot = main.render_dot(reduced)
        self.assertIn('"c" -> "b"', dot)
        self.assertNotIn('"c" -> "a"', dot)
        self.assertEqual(main.reachable(reduced, "c"), {"a", "b"})

    def test_graph_command_prints_dot_and_json(self):
        code, out, _ = self.run_cli("graph")
        self.assertEqual(code, 0)
        self.assertTrue(out.startswith("digraph {"))
        self.assertIn('"local_file.index" -> "time_sleep.warm_a" [style = "dashed"]', out)
        self.assertIn('"local.out_dir" [label = "local.out_dir", shape = "note"]', out)
        code, out, _ = self.run_cli("--json", "graph")
        edges = json.loads(out)["edges"]
        self.assertIn(["terraform_data.release", "random_pet.name"], edges)
        self.assertIn(["local_file.config", "local.out_dir"], edges)


class WalkTests(unittest.TestCase):
    DEPS = {"a": set(), "b": set(), "c": set(), "d": {"a"}, "e": {"a", "b"}, "f": {"d", "e"}, "g": {"c"}, "h": {"f", "g"}}

    def test_walk_never_exceeds_parallelism_and_starts_nodes_only_after_their_dependencies(self):
        for parallelism, low in ((1, 1), (3, 2)):
            with self.subTest(parallelism=parallelism):
                result = main.walk(self.DEPS, lambda node: time.sleep(0.02), parallelism)
                self.assertEqual(sorted(result.completed), sorted(self.DEPS))
                self.assertLessEqual(result.max_in_flight, parallelism)
                self.assertGreaterEqual(result.max_in_flight, low)
                order = [(node, event) for _, node, event in result.events]
                for node, found in self.DEPS.items():
                    for dep in found:
                        self.assertLess(order.index((dep, "finish")), order.index((node, "start")), f"{node} started before {dep} finished")

    def test_walk_starts_a_node_as_soon_as_its_own_dependencies_finish(self):
        deps = {"slow": set(), "fast": set(), "after_fast": {"fast"}}
        released = threading.Event()

        def visit(node):
            if node == "slow" and not released.wait(2):
                raise RuntimeError("after_fast did not start while slow was still running")
            if node == "after_fast":
                released.set()

        result = main.walk(deps, visit, parallelism=2)
        self.assertEqual(result.failed, {})
        self.assertLess(result.completed.index("after_fast"), result.completed.index("slow"))

    def test_a_failure_skips_dependents_and_finishes_other_branches(self):
        deps = {"a": set(), "b": {"a"}, "c": {"b"}, "x": set(), "y": {"x"}}

        def visit(node):
            if node == "a":
                raise RuntimeError("boom")

        result = main.walk(deps, visit, parallelism=2)
        self.assertEqual(result.failed, {"a": "boom"})
        self.assertEqual(result.skipped, ["b", "c"])
        self.assertEqual(set(result.completed), {"x", "y"})

    def test_walk_rejects_cycles_and_zero_parallelism(self):
        with self.assertRaises(main.CycleError):
            main.walk({"a": {"b"}, "b": {"a"}}, lambda node: None)
        with self.assertRaises(ValueError):
            main.walk({"a": set()}, lambda node: None, parallelism=0)


class PlanApplyTests(LessonDir):
    def test_plan_marks_values_that_depend_on_new_resources_known_after_apply(self):
        changes = self.plan_changes()
        self.assertIs(changes["random_pet.name"].after["id"], main.UNKNOWN)
        self.assertIs(changes["terraform_data.release"].after["input"], main.UNKNOWN)
        self.assertIs(changes["local_file.config"].after["content"], main.UNKNOWN)
        self.assertEqual(changes["local_file.config"].after["filename"], "./out/config.txt")
        self.assertEqual(changes["local_file.index"].after["content"], "see ./out/config.txt\n")
        data = main.plan_json(list(changes.values()))
        config = next(item for item in data["resource_changes"] if item["address"] == "local_file.config")
        self.assertEqual(config["change"]["after_unknown"], {"content": True, "id": True})
        self.assertEqual(data["summary"], {"add": 10, "change": 0, "destroy": 0})
        code, out, _ = self.run_cli("plan")
        self.assertIn("+ content  = (known after apply)", out)
        self.assertTrue(out.rstrip().endswith("Plan: 10 to add, 0 to change, 0 to destroy."))

    def test_apply_follows_every_edge_and_the_next_plan_is_empty(self):
        code, out, _ = self.run_cli("apply", "-parallelism=4")
        self.assertEqual(code, 0, out)
        lines = out.splitlines()
        for node, dep in self.resource_edges():
            self.assertLess(position(lines, f"{dep}: Creation complete"), position(lines, f"{node}: Creating..."), f"{node} before {dep}")
        self.assertIn("Apply complete! Resources: 10 added, 0 changed, 0 destroyed.", out)
        state = main.load_state(os.path.join(self.root, main.STATE_FILE))
        pet = state["resources"]["random_pet.name"]["attrs"]["id"]
        self.assertEqual(self.read("config.txt"), f"release={pet}\n")
        self.assertEqual(state["outputs"], {"pet": pet})
        self.assertEqual(state["resources"]["local_file.index"]["dependencies"], ["local_file.config", "time_sleep.warm_a", "time_sleep.warm_b"])
        self.assertEqual({change.verb for change in self.plan_changes().values()}, {"noop"})
        self.assertIn("No changes.", self.run_cli("plan")[1])

    def test_destroy_runs_the_edges_backwards(self):
        self.run_cli("apply")
        code, out, _ = self.run_cli("destroy", "-parallelism=4")
        self.assertEqual(code, 0, out)
        lines = out.splitlines()
        for node, dep in self.resource_edges():
            self.assertLess(position(lines, f"{node}: Destruction complete"), position(lines, f"{dep}: Destroying..."), f"{dep} destroyed before {node}")
        self.assertIn("Destroy complete! Resources: 10 destroyed.", out)
        self.assertEqual(os.listdir(os.path.join(self.root, "out")), [])
        self.assertEqual(main.load_state(os.path.join(self.root, main.STATE_FILE))["resources"], {})

    def test_replacement_destroys_first_by_default(self):
        self.run_cli("apply")
        self.config["resource"]["local_file"]["pointer"]["filename"] = "${local.out_dir}/pointer2.txt"
        self.write()
        change = self.plan_changes()["local_file.pointer"]
        self.assertEqual((change.verb, change.actions(), change.forced_by), ("replace", ["delete", "create"], ["filename"]))
        code, out, _ = self.run_cli("apply")
        lines = out.splitlines()
        self.assertIn("-/+ local_file.pointer", out)
        self.assertLess(position(lines, "local_file.pointer: Destruction complete"), position(lines, "local_file.pointer: Creating..."))
        self.assertEqual(sorted(os.listdir(os.path.join(self.root, "out"))), ["config.txt", "index.html", "pointer2.txt", "v1.txt"])

    def test_create_before_destroy_creates_first_and_retires_the_old_object_last(self):
        self.run_cli("apply")
        self.config["resource"]["local_file"]["versioned"]["filename"] = "${local.out_dir}/v2.txt"
        self.write()
        changes = self.plan_changes()
        self.assertEqual(changes["local_file.versioned"].actions(), ["create", "delete"])
        self.assertEqual(changes["local_file.pointer"].verb, "replace")
        code, out, _ = self.run_cli("apply")
        self.assertEqual(code, 0, out)
        self.assertIn("+/- local_file.versioned", out)
        lines = out.splitlines()
        retire = position(lines, "local_file.versioned (deposed object ")
        self.assertLess(position(lines, "local_file.versioned: Creating..."), retire)
        self.assertLess(position(lines, "local_file.pointer: Creation complete"), retire)
        self.assertEqual(self.read("pointer.txt"), "current=./out/v2.txt\n")
        self.assertFalse(os.path.exists(os.path.join(self.root, "out", "v1.txt")))
        self.assertTrue(os.path.exists(os.path.join(self.root, "out", "v2.txt")))

    def test_create_before_destroy_propagates_to_dependencies_or_the_graph_has_a_cycle(self):
        config = {"resource": {"terraform_data": {
            "a": {"triggers_replace": "1"},
            "b": {"input": "${terraform_data.a.id}", "triggers_replace": "1", "lifecycle": {"create_before_destroy": True}},
        }}}
        self.write(config)
        self.run_cli("apply")
        config["resource"]["terraform_data"]["a"]["triggers_replace"] = "2"
        config["resource"]["terraform_data"]["b"]["triggers_replace"] = "2"
        self.write(config)
        changes = self.plan_changes()
        self.assertTrue(changes["terraform_data.a"].create_before_destroy)
        loaded, graph = main.build(self.root)
        state = main.load_state(os.path.join(self.root, main.STATE_FILE))
        nodes, _ = main.apply_graph(list(changes.values()), main.resource_graph(graph), state)
        self.assertIsNone(main.find_cycle(nodes))
        changes["terraform_data.a"].create_before_destroy = False
        broken, _ = main.apply_graph(list(changes.values()), main.resource_graph(graph), state)
        self.assertIsNotNone(main.find_cycle(broken))
        code, out, _ = self.run_cli("apply")
        self.assertEqual(code, 0, out)
        lines = out.splitlines()
        steps = [position(lines, "terraform_data.a: Creating..."), position(lines, "terraform_data.b: Creating..."),
                 position(lines, "terraform_data.b (deposed object "), position(lines, "terraform_data.a (deposed object ")]
        self.assertEqual(steps, sorted(steps))

    def test_an_unknown_value_on_a_force_new_argument_cascades_replacement(self):
        self.run_cli("apply")
        self.config["resource"]["random_pet"]["name"]["length"] = 3
        self.write()
        changes = self.plan_changes()
        verbs = {address: change.verb for address, change in changes.items() if change.verb != "noop"}
        self.assertEqual(verbs, {"random_pet.name": "replace", "terraform_data.release": "update", "local_file.config": "replace"})
        self.assertIs(changes["terraform_data.release"].after["output"], main.UNKNOWN)
        self.assertEqual(changes["local_file.config"].forced_by, ["content"])
        code, out, _ = self.run_cli("apply")
        self.assertIn("terraform_data.release: Modifying...", out)
        pet = main.load_state(os.path.join(self.root, main.STATE_FILE))["resources"]["random_pet.name"]["attrs"]["id"]
        self.assertEqual(len(pet.split("-")), 3)
        self.assertEqual(self.read("config.txt"), f"release={pet}\n")

    def test_removed_resources_are_destroyed_using_dependencies_recorded_in_state(self):
        self.run_cli("apply")
        del self.config["resource"]["local_file"]["config"]
        del self.config["resource"]["local_file"]["index"]
        self.write()
        changes = self.plan_changes()
        self.assertEqual(changes["local_file.index"].reason, "not in configuration")
        code, out, _ = self.run_cli("apply")
        lines = out.splitlines()
        self.assertLess(position(lines, "local_file.index: Destruction complete"), position(lines, "local_file.config: Destroying..."))
        self.assertIn("0 added, 0 changed, 2 destroyed", out)

    def test_a_failed_resource_skips_its_dependents_and_keeps_partial_state(self):
        with open(os.path.join(self.root, "blocker"), "w", encoding="utf-8") as handle:
            handle.write("a file where a directory should be")
        self.write({"resource": {"local_file": {
            "bad": {"filename": "./blocker/x.txt", "content": "x"},
            "after": {"filename": "./after.txt", "content": "${local_file.bad.id}"},
            "ok": {"filename": "./ok.txt", "content": "fine"},
        }}})
        code, _, err = self.run_cli("apply")
        self.assertEqual(code, 1)
        self.assertIn("Error: local_file.bad:", err)
        self.assertIn("Skipped because a dependency failed: local_file.after", err)
        self.assertEqual(sorted(main.load_state(os.path.join(self.root, main.STATE_FILE))["resources"]), ["local_file.ok"])
        self.assertTrue(os.path.exists(os.path.join(self.root, "ok.txt")))

    def test_parallelism_flag_spellings_and_json_report(self):
        code, out, _ = self.run_cli("--json", "apply", "-parallelism=1")
        self.assertEqual(code, 0)
        data = json.loads(out)
        self.assertEqual(data["max_in_flight"], 1)
        self.assertEqual(data["counts"], {"added": 10, "changed": 0, "destroyed": 0})
        self.assertEqual(sum(1 for event in data["events"] if event["event"] == "start"), 10)
        code, out, _ = self.run_cli("--json", "destroy", "--parallelism", "4")
        self.assertEqual(json.loads(out)["counts"]["destroyed"], 10)
        with self.assertRaises(SystemExit) as raised:
            with contextlib.redirect_stderr(io.StringIO()):
                main.main(["-chdir", self.root, "apply", "-parallelism=0"])
        self.assertEqual(raised.exception.code, 2)


if __name__ == "__main__":
    unittest.main()
