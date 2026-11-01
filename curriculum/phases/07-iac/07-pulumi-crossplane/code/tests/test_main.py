import contextlib
import copy
import io
import json
import os
import random
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import main  # noqa: E402

CODE = os.path.join(os.path.dirname(__file__), "..")
EXAMPLES = os.path.join(CODE, "examples")


def deploy(program, state=None, mode="up", config=None, seed=7):
    deployment = main.Deployment("demo", "dev", config or {}, state or main.empty_state(), {"random": main.RandomProvider(random.Random(seed))}, mode)
    return main.run_program(program, deployment)


def three_resources(suffix_length=6, seen=None, protect=False, delete_first=False):
    def program():
        pet = main.RandomPet("pet", length=2)
        suffix = main.RandomString("suffix", length=suffix_length, special=False, upper=False,
                                   opts=main.ResourceOptions(protect=protect, delete_before_replace=delete_first))

        def join(parts):
            if seen is not None:
                seen.append(parts)
            return f"{parts[0]}-{parts[1]}"

        name = main.Output.all(pet.id, suffix.result).apply(join)
        port = main.RandomInteger("port", min=8000, max=8999, keepers={"name": name})
        main.export("name", name)
        main.export("port", port.result)
    return program


def ops(deployment):
    return {step.name: step.op for step in deployment.steps}


def run_cli(*args):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = main.main(list(args))
    return code, out.getvalue(), err.getvalue()


class OutputTests(unittest.TestCase):
    def test_apply_all_and_concat_track_resources_and_secrets(self):
        a = main.Output(lambda: "a", {"urn:a"})
        b = main.Output(lambda: 2, {"urn:b"}, secret=True)
        joined = main.Output.all(a, b, "plain").apply(lambda parts: f"{parts[0]}{parts[1]}{parts[2]}")
        self.assertEqual(joined.resolve(), "a2plain")
        self.assertEqual(joined.resources(), frozenset({"urn:a", "urn:b"}))
        self.assertTrue(joined.is_secret())
        self.assertEqual(main.Output.concat(a, "-", "z").resolve(), "a-z")
        self.assertEqual(main.Output.all(x=a, y=b).resolve(), {"x": "a", "y": 2})
        self.assertIn("not supported", str(a))

    def test_apply_on_unknown_skips_the_callback(self):
        calls = []
        unknown = main.Output(lambda: main.UNKNOWN, {"urn:a"})
        derived = unknown.apply(lambda value: calls.append(value) or value)
        self.assertIs(derived.resolve(), main.UNKNOWN)
        self.assertEqual(calls, [])
        self.assertIs(main.Output.all(unknown, main.Output(lambda: 1)).resolve(), main.UNKNOWN)

    def test_apply_returning_an_output_is_rejected(self):
        a = main.Output(lambda: 1)
        with self.assertRaises(TypeError):
            a.apply(lambda value: main.Output(lambda: value)).resolve()


class MinipulumiEngineTests(unittest.TestCase):
    def test_preview_plans_four_creates_with_unknown_outputs_and_no_callbacks(self):
        seen = []
        preview = deploy(three_resources(seen=seen), mode="preview")
        self.assertEqual(ops(preview), {"demo-dev": "create", "pet": "create", "suffix": "create", "port": "create"})
        self.assertEqual(preview.counts(), {"create": 4})
        self.assertIs(preview.export_values["name"], main.UNKNOWN)
        self.assertEqual(seen, [])
        self.assertIn("name: [unknown]", main.render_deployment(preview))
        self.assertEqual(main.deployment_json(preview)["outputs"]["name"], main.UNKNOWN_STRING_SENTINEL)

    def test_up_records_dependencies_from_output_use(self):
        up = deploy(three_resources())
        state = up.checkpoint()
        by_name = {main.name_of(r["urn"]): r for r in main.state_resources(state)}
        self.assertEqual([main.name_of(d) for d in by_name["port"]["dependencies"]], ["pet", "suffix"])
        self.assertEqual([main.name_of(d) for d in by_name["port"]["propertyDependencies"]["keepers"]], ["pet", "suffix"])
        self.assertEqual(by_name["pet"]["dependencies"], [])
        self.assertEqual(by_name["port"]["urn"], "urn:pulumi:dev::demo::random:index/randomInteger:RandomInteger::port")
        name = up.export_values["name"]
        self.assertEqual(name, f"{by_name['pet']['id']}-{by_name['suffix']['outputs']['result']}")
        self.assertTrue(8000 <= up.export_values["port"] <= 8999)
        again = deploy(three_resources(), state=state, mode="preview")
        self.assertEqual(set(ops(again).values()), {"same"})
        self.assertEqual(again.export_values["name"], name)

    def test_changing_an_input_replaces_dependents_and_keeps_the_rest(self):
        first = deploy(three_resources(suffix_length=6))
        state = first.checkpoint()
        preview = deploy(three_resources(suffix_length=8), state=state, mode="preview")
        self.assertEqual(ops(preview), {"demo-dev": "same", "pet": "same", "suffix": "replace", "port": "replace"})
        self.assertEqual([s.diffs for s in preview.steps if s.name == "port"], [["keepers"]])
        second = deploy(three_resources(suffix_length=8), state=state, seed=99)
        old_pet = first.export_values["name"].rsplit("-", 1)[0]
        new_pet, new_suffix = second.export_values["name"].rsplit("-", 1)
        self.assertEqual(new_pet, old_pet)
        self.assertEqual(len(new_suffix), 8)
        self.assertEqual(second.events, ["created replacement suffix", "created replacement port", "deleted original port", "deleted original suffix"])

    def test_delete_before_replace_reverses_the_order(self):
        state = deploy(three_resources(suffix_length=6, delete_first=True)).checkpoint()
        second = deploy(three_resources(suffix_length=7, delete_first=True), state=state)
        self.assertEqual(second.events[:2], ["deleted original suffix", "created replacement suffix"])

    def test_removed_resources_are_deleted_and_destroy_goes_dependents_first(self):
        state = deploy(three_resources()).checkpoint()

        def only_pet():
            main.RandomPet("pet")

        smaller = deploy(only_pet, state=state)
        self.assertEqual([(s.name, s.op) for s in smaller.steps if s.op == "delete"], [("port", "delete"), ("suffix", "delete")])
        state = deploy(three_resources()).checkpoint()
        destroy = main.Deployment("demo", "dev", {}, state, {"random": main.RandomProvider(random.Random(1))}, "destroy")
        destroy.finish()
        self.assertEqual(destroy.events, ["deleted port", "deleted suffix", "deleted pet", "deleted demo-dev"])
        self.assertEqual(main.state_resources(destroy.checkpoint()), [])

    def test_protect_blocks_replacement_and_deletion(self):
        state = deploy(three_resources(protect=True)).checkpoint()
        with self.assertRaises(main.EngineError) as caught:
            deploy(three_resources(suffix_length=9, protect=True), state=state, mode="preview")
        self.assertIn("protected", str(caught.exception))
        destroy = main.Deployment("demo", "dev", {}, state, {"random": main.RandomProvider()}, "destroy")
        with self.assertRaises(main.EngineError):
            destroy.finish()

    def test_secrets_propagate_and_are_masked(self):
        def program():
            password = main.RandomPassword("db", length=12)
            main.export("dsn", password.result.apply(lambda value: f"postgres://app:{value}@db"))
            main.export("plain", main.Output.secret("x").apply(len))

        up = deploy(program)
        self.assertEqual(sorted(up.secret_exports), ["dsn", "plain"])
        self.assertEqual(up.masked_outputs()["dsn"], "[secret]")
        state = up.checkpoint()
        password = next(r for r in main.state_resources(state) if r["type"] == main.RandomProvider.PASSWORD)
        self.assertEqual(password["outputs"]["result"][main.SECRET_SIG_KEY], main.SECRET_SIG_VALUE)
        self.assertEqual(password["id"], "none")
        self.assertEqual(main.stack_outputs(state, show_secrets=False)["dsn"], "[secret]")
        self.assertTrue(main.stack_outputs(state, show_secrets=True)["dsn"].startswith("postgres://app:"))

    def test_duplicate_names_and_resources_outside_a_program_fail(self):
        def program():
            main.RandomPet("same")
            main.RandomPet("same")

        with self.assertRaises(main.EngineError) as caught:
            deploy(program)
        self.assertIn("Duplicate resource URN", str(caught.exception))
        with self.assertRaises(main.EngineError):
            main.RandomPet("loose")

    def test_stack_config_parsing(self):
        text = 'encryptionsalt: v1:abc:v1:def\nconfig:\n  demo:suffixLength: "8"\n  demo:region: eu-central-1\n  aws:region: \'us-east-1\'\n'
        self.assertEqual(main.parse_stack_config(text), {"demo:suffixLength": "8", "demo:region": "eu-central-1", "aws:region": "us-east-1"})
        with self.assertRaises(main.UsageError):
            main.parse_stack_config("config:\n  demo:password:\n    secure: v1:xyz\n")

        def program():
            config = main.Config()
            main.export("n", config.get_int("suffixLength"))
            main.export("missing", config.get("nope", "fallback"))

        up = deploy(program, config={"demo:suffixLength": "8"})
        self.assertEqual(up.export_values, {"n": 8, "missing": "fallback"})


class PulumiProgramTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.project = os.path.join(self.tmp.name, "app")
        shutil.copytree(os.path.join(EXAMPLES, "pulumi-app"), self.project)

    def tearDown(self):
        self.tmp.cleanup()

    def test_the_real_pulumi_program_runs_unchanged_through_the_shims(self):
        before = {name: sys.modules.get(name) for name in ("pulumi", "pulumi_random")}
        code, out, err = run_cli("pulumi", "up", "--cwd", self.project, "--seed", "3", "--json")
        self.assertEqual(code, 0, err)
        data = json.loads(out)
        self.assertEqual(data["changeSummary"], {"create": 4})
        self.assertEqual({name: sys.modules.get(name) for name in ("pulumi", "pulumi_random")}, before)
        code, out, _ = run_cli("pulumi", "output", "--cwd", self.project, "--json")
        outputs = json.loads(out)
        self.assertEqual(sorted(outputs), ["name", "port"])
        self.assertEqual(len(outputs["name"].rsplit("-", 1)[1]), 6)
        code, out, _ = run_cli("pulumi", "graph", "--cwd", self.project)
        self.assertIn('"port" -> "suffix";', out)

    def test_config_change_then_destroy_through_the_cli(self):
        run_cli("pulumi", "up", "--cwd", self.project, "--seed", "3")
        with open(os.path.join(self.project, "Pulumi.dev.yaml"), "w", encoding="utf-8") as handle:
            handle.write('config:\n  ccsmoke:suffixLength: "8"\n')
        code, out, _ = run_cli("pulumi", "preview", "--cwd", self.project, "--json")
        steps = {step["name"]: step["op"] for step in json.loads(out)["steps"]}
        self.assertEqual(steps, {"ccsmoke-dev": "same", "suffix": "replace", "port": "replace"})
        code, out, _ = run_cli("pulumi", "destroy", "--cwd", self.project)
        self.assertEqual(code, 0)
        self.assertIn("- 4 deleted", out)

    def test_missing_project_is_a_usage_error(self):
        code, _, err = run_cli("pulumi", "preview", "--cwd", self.tmp.name)
        self.assertEqual(code, 2)
        self.assertIn("no Pulumi.yaml", err)


def example(name):
    with open(os.path.join(EXAMPLES, "xp", name), encoding="utf-8") as handle:
        return json.load(handle)


class FieldPathAndTransformTests(unittest.TestCase):
    def test_field_paths_with_indexes_and_bracketed_keys(self):
        self.assertEqual(main.parse_field_path("spec.containers[0].image"), ["spec", "containers", 0, "image"])
        self.assertEqual(main.parse_field_path("metadata.annotations[example.org/owner]"), ["metadata", "annotations", "example.org/owner"])
        obj = {}
        main.set_value(obj, "spec.template.spec.containers[1].name", "web")
        self.assertEqual(obj, {"spec": {"template": {"spec": {"containers": [None, {"name": "web"}]}}}})
        self.assertEqual(main.get_value(obj, "spec.template.spec.containers[1].name"), "web")
        with self.assertRaises(main.FieldNotFound):
            main.get_value(obj, "spec.missing")
        with self.assertRaises(main.CompositionError):
            main.parse_field_path("spec..x")

    def test_transforms_match_patch_and_transform_semantics(self):
        run = main.run_transforms
        self.assertEqual(run([{"type": "map", "map": {"small": 1, "large": 3}}], "large"), 3)
        self.assertEqual(run([{"type": "string", "string": {"type": "Format", "fmt": "%s-web"}}], "shop"), "shop-web")
        self.assertEqual(run([{"type": "string", "string": {"fmt": "%d replicas"}}], 2.0), "2 replicas")
        self.assertEqual(run([{"type": "string", "string": {"type": "Convert", "convert": "ToUpper"}}, {"type": "string", "string": {"type": "TrimPrefix", "trim": "TEAM-"}}], "team-a"), "A")
        self.assertEqual(run([{"type": "string", "string": {"type": "Regexp", "regexp": {"match": "^(\\w+)-", "group": 1}}}], "eu-west-1"), "eu")
        self.assertEqual(run([{"type": "math", "math": {"type": "Multiply", "multiply": 1024}}], 4), 4096)
        self.assertEqual(run([{"type": "convert", "convert": {"toType": "int64"}}], "42"), 42)
        with self.assertRaises(main.CompositionError) as caught:
            run([{"type": "map", "map": {"small": 1}}], "huge")
        self.assertIn("key huge is not found in map", str(caught.exception))
        self.assertEqual(main.go_format("%s owned by %s and %s", ["a", "b"]), "a owned by b and %!s(MISSING)")


class MinixpRenderTests(unittest.TestCase):
    def setUp(self):
        self.xr = example("xr.json")
        self.composition = example("composition.json")

    def observed_from(self, rendered, ready=True):
        deployment, service = [copy.deepcopy(obj) for obj in rendered.composed]
        for obj, suffix in ((deployment, "7d9f"), (service, "x2k1")):
            obj["metadata"]["name"] = obj["metadata"].pop("generateName") + suffix
            obj["metadata"].pop("ownerReferences")
        deployment["status"] = {"availableReplicas": 2, "conditions": [{"type": "Available", "status": "True" if ready else "False"}]}
        service["spec"]["clusterIP"] = "10.96.14.7"
        return [deployment, service]

    def test_render_patches_and_stamps_metadata(self):
        rendered = main.render(self.xr, self.composition)
        deployment, service = rendered.composed
        self.assertEqual(deployment["spec"]["replicas"], 2)
        self.assertEqual(deployment["spec"]["selector"]["matchLabels"], {"app": "checkout-web"})
        self.assertEqual(deployment["spec"]["template"]["spec"]["containers"][0]["image"], "registry.k8s.io/pause:3.10")
        self.assertEqual(deployment["metadata"]["labels"], {"team": "payments", "crossplane.io/composite": "checkout"})
        self.assertEqual(deployment["metadata"]["annotations"]["platform.example.org/description"], "checkout, owned by Payments")
        self.assertEqual(deployment["metadata"]["generateName"], "checkout-")
        self.assertEqual(deployment["metadata"]["namespace"], "cc-pulumi-crossplane")
        self.assertTrue(deployment["metadata"]["ownerReferences"][0]["controller"])
        self.assertEqual(service["spec"]["selector"], {"app": "checkout-web"})
        condition = rendered.composite["status"]["conditions"][0]
        self.assertEqual((condition["status"], condition["reason"]), ("False", "Creating"))
        self.assertEqual(condition["message"], "Unready resources: deployment, service")
        self.assertEqual(main.render(self.xr, self.composition).composed, rendered.composed)

    def test_status_and_readiness_flow_back_from_observed_resources(self):
        first = main.render(self.xr, self.composition)
        second = main.render(self.xr, self.composition, self.observed_from(first))
        self.assertEqual(second.composite["status"]["address"], "10.96.14.7")
        self.assertEqual(second.composite["status"]["availableReplicas"], 2)
        self.assertEqual(second.composite["status"]["conditions"][0]["status"], "True")
        self.assertEqual(second.composed[0]["metadata"]["name"], "checkout-7d9f")
        self.assertNotIn("generateName", second.composed[0]["metadata"])
        stalled = main.render(self.xr, self.composition, self.observed_from(first, ready=False))
        self.assertEqual(stalled.ready, {"deployment": False, "service": True})
        self.assertEqual(stalled.composite["status"]["conditions"][0]["message"], "Unready resources: deployment")

    def test_default_readiness_is_the_ready_condition(self):
        self.assertFalse(main.is_ready({"status": {"conditions": [{"type": "Available", "status": "True"}]}}, []))
        self.assertTrue(main.is_ready({"status": {"conditions": [{"type": "Ready", "status": "True"}]}}, []))
        self.assertTrue(main.run_readiness_check({"type": "MatchString", "fieldPath": "status.phase", "matchString": "Bound"}, {"status": {"phase": "Bound"}}))
        self.assertFalse(main.run_readiness_check({"type": "MatchInteger", "fieldPath": "status.n", "matchInteger": 3}, {"status": {}}))

    def test_required_policy_skips_a_new_resource(self):
        resources = self.composition["spec"]["pipeline"][0]["input"]["resources"]
        resources[1]["patches"].append({"fromFieldPath": "spec.port", "toFieldPath": "spec.ports[0].port", "policy": {"fromFieldPath": "Required"}})
        rendered = main.render(self.xr, self.composition)
        self.assertEqual([obj["kind"] for obj in rendered.composed], ["Deployment"])
        self.assertEqual(rendered.skipped, ["service"])
        self.assertIn("Required", rendered.warnings[0])
        self.assertIn("service", rendered.composite["status"]["conditions"][0]["message"])

    def test_composition_errors_are_explicit(self):
        wrong_kind = copy.deepcopy(self.composition)
        wrong_kind["spec"]["compositeTypeRef"]["kind"] = "Database"
        with self.assertRaises(main.CompositionError) as caught:
            main.render(self.xr, wrong_kind)
        self.assertIn("does not match XR's kind", str(caught.exception))
        legacy = copy.deepcopy(self.composition)
        legacy["spec"]["mode"] = "Resources"
        with self.assertRaises(main.CompositionError) as caught:
            main.render(self.xr, legacy)
        self.assertIn("removed in Crossplane v2", str(caught.exception))
        huge = copy.deepcopy(self.xr)
        huge["spec"]["size"] = "huge"
        with self.assertRaises(main.CompositionError) as caught:
            main.render(huge, self.composition)
        self.assertIn("'deployment' FromCompositeFieldPath patch at index 4", str(caught.exception))
        self.assertIn("key huge is not found in map", str(caught.exception))

    def test_cli_render_json_stream_and_errors(self):
        xr, comp = os.path.join(EXAMPLES, "xp", "xr.json"), os.path.join(EXAMPLES, "xp", "composition.json")
        code, out, _ = run_cli("xp", "render", xr, comp, "--json")
        self.assertEqual(code, 0)
        objects = json.loads(out)
        self.assertEqual([obj["kind"] for obj in objects], ["App", "Deployment", "Service"])
        code, out, _ = run_cli("xp", "render", xr, comp, "--stream")
        self.assertEqual(out.count("---\n"), 3)
        with tempfile.TemporaryDirectory() as tmp:
            observed = os.path.join(tmp, "observed.yaml")
            first = main.render(example("xr.json"), example("composition.json"))
            with open(observed, "w", encoding="utf-8") as handle:
                handle.write("\n".join("---\n" + json.dumps(obj) for obj in self.observed_from(first)))
            code, out, _ = run_cli("xp", "render", xr, comp, "--observed", observed)
            self.assertEqual(code, 0)
            self.assertIn("Ready=True Available", out)
            self.assertIn('"address": "10.96.14.7"', out)
            bad = os.path.join(tmp, "bad.yaml")
            with open(bad, "w", encoding="utf-8") as handle:
                handle.write("kind: App\n")
            code, _, err = run_cli("xp", "render", bad, comp)
            self.assertEqual(code, 2)
            self.assertIn("reads JSON documents", err)


if __name__ == "__main__":
    unittest.main()
