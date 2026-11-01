import contextlib
import io
import json
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import main  # noqa: E402

FIXTURES = os.path.join(os.path.dirname(__file__), "..", "fixtures")

SITE_ADDRESSES = [
    "local_file.index",
    'module.web["blog"].local_file.page[0]',
    'module.web["blog"].local_file.page[1]',
    'module.web["docs"].local_file.page[0]',
    'module.web["docs"].local_file.page[1]',
]

CHILD = {
    "variable": {"label": {"type": "string"}, "secret": {"type": "string", "default": ""}},
    "resource": {"local_file": {"page": {"filename": "${var.label}.txt", "content": "${var.label}"}}},
    "output": {"file": {"value": "${local_file.page.filename}"}, "shout": {"value": "${upper(var.secret)}"}},
}


def expression(text, scope=None):
    node = main.parse_expression(text)
    if scope is None:
        run = main.Run(main.ModuleDef(".", "."), {})
        scope = main.Scope(run.root)
    return main.evaluate(node, scope)


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = os.path.join(self.tmp.name, "root")
        os.makedirs(self.root)

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, relative, data):
        path = os.path.join(self.root, relative)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(data if isinstance(data, str) else json.dumps(data))

    def site(self):
        target = os.path.join(self.tmp.name, "site")
        shutil.copytree(os.path.join(FIXTURES, "site"), target)
        return target

    def cli(self, *args, directory=None, code=0):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            result = main.main([*args, "--chdir", directory or self.root])
        self.assertEqual(result, code, f"stdout:\n{out.getvalue()}\nstderr:\n{err.getvalue()}")
        return out.getvalue(), err.getvalue()

    def plan_json(self, *args, directory=None):
        return json.loads(self.cli("plan", "--json", *args, directory=directory)[0])

    def run_for(self, inputs=None, environ=None, directory=None):
        directory = directory or self.root
        definition = main.load_module(directory, directory)
        values, _ = main.collect_root_inputs(definition, directory, inputs or [], environ or {})
        return main.Run(definition, values)

    def error_of(self, *args, directory=None):
        return self.cli(*args, directory=directory, code=1)[1]


class ExpressionTests(Base):
    def test_single_interpolation_keeps_the_value_and_mixed_templates_become_strings(self):
        self.assertEqual(expression('"${[1, 2]}"'), [1, 2])
        self.assertEqual(expression('"n=${1 + 1}"'), "n=2")
        self.assertEqual(expression('"${true}!"'), "true!")
        with self.assertRaises(main.MiniModError) as caught:
            expression('"list: ${[1]}"')
        self.assertIn("string required", caught.exception.detail)

    def test_operators_follow_precedence_and_functions_work(self):
        self.assertIs(expression('1 + 2 * 3 == 7 && contains(["a", "b"], "b")'), True)
        self.assertEqual(expression('length("abc") > 2 ? upper("yes") : "no"'), "YES")
        self.assertEqual(expression('join("-", keys({b = 1, a = 2}))'), "a-b")
        self.assertEqual(expression('format("%s-%d", "web", 3)'), "web-3")
        self.assertIs(expression('can(regex("^[a-z]+$", "Abc"))'), False)
        self.assertEqual(expression("7 / 2"), 3.5)
        self.assertEqual(expression("-4 + 6"), 2)
        with self.assertRaises(main.MiniModError) as caught:
            expression('nope("x")')
        self.assertEqual(caught.exception.summary, "Call to unknown function")

    def test_dollar_dollar_escapes_interpolation_and_directives_are_rejected(self):
        node = main.compile_json("cost: $${amount}")
        self.assertEqual(node, ("lit", "cost: ${amount}"))
        with self.assertRaises(main.MiniModError) as caught:
            main.compile_json("%{ if true }yes%{ endif }")
        self.assertEqual(caught.exception.summary, "Unsupported template directive")

    def test_sets_cannot_be_indexed_and_splat_maps_over_lists(self):
        with self.assertRaises(main.MiniModError) as caught:
            expression('toset(["a", "b"])[0]')
        self.assertIn("Elements of a set", caught.exception.detail)
        self.assertEqual(expression('[{n = 1}, {n = 2}][*].n'), [1, 2])


class TypeTests(Base):
    def test_conversion_follows_the_declared_type(self):
        self.assertEqual(main.convert("3", main.parse_type("number")), 3)
        self.assertEqual(main.convert([1, True], main.parse_type("list(string)")), ["1", "true"])
        self.assertEqual(main.convert(["b", "a", "b"], main.parse_type("set(string)")), frozenset({"a", "b"}))
        typ = main.parse_type("map(object({ port = number, host = string }))")
        self.assertEqual(main.convert({"web": {"port": "80", "host": "a", "extra": 1}}, typ), {"web": {"port": 80, "host": "a"}})
        with self.assertRaises(main.ConversionError) as caught:
            main.convert({"web": {"host": "a"}}, typ)
        self.assertEqual(str(caught.exception), 'element "web": attribute "port" is required')
        with self.assertRaises(main.ConversionError) as caught:
            main.convert(["x"], main.parse_type("list(number)"))
        self.assertEqual(str(caught.exception), "element 0: a number is required")


class VariableTests(Base):
    def test_validation_failure_names_the_message_variable_and_value(self):
        site = self.site()
        err = self.error_of("plan", "-var", "env=qa", directory=site)
        self.assertIn("Error: Invalid value for variable", err)
        self.assertIn("The env variable must be dev or prod.", err)
        self.assertIn('var.env is "qa"', err)

    def test_child_validation_runs_per_module_instance(self):
        site = self.site()
        err = self.error_of("plan", "-var", 'sites={"Blog": "x", "blog": "y"}', directory=site)
        self.assertIn("lowercase letters", err)
        self.assertIn('module.web["Blog"].var.name is "dev-site-Blog"', err)

    def test_sensitive_values_are_hidden_in_validation_errors(self):
        self.write("main.tf.json", {"variable": {"password": {"type": "string", "sensitive": True, "validation": [{"condition": "${length(var.password) >= 12}", "error_message": "Use at least 12 characters."}]}}})
        err = self.error_of("plan", "-var", "password=hunter2")
        self.assertIn("var.password is (sensitive value)", err)
        self.assertNotIn("hunter2", err)

    def test_input_precedence_default_env_files_then_command_line_order(self):
        self.write("main.tf.json", {"variable": {"env": {"type": "string", "default": "dev"}}})

        def env_value(inputs=(), environ=None):
            return self.run_for(list(inputs), environ).root.variable("env")

        self.assertEqual(env_value(), "dev")
        self.assertEqual(env_value(environ={"TF_VAR_env": "from-env"}), "from-env")
        self.write("terraform.tfvars", 'env = "from-tfvars"\n')
        self.assertEqual(env_value(environ={"TF_VAR_env": "from-env"}), "from-tfvars")
        self.write("z.auto.tfvars", 'env = "auto-z"\n')
        self.write("a.auto.tfvars.json", {"env": "auto-a"})
        self.assertEqual(env_value(), "auto-z")
        self.write("late.tfvars", '# comment\nenv = "late-file"\n')
        self.assertEqual(env_value([("var", "env=cli"), ("file", "late.tfvars")]), "late-file")
        self.assertEqual(env_value([("file", "late.tfvars"), ("var", "env=cli")]), "cli")

    def test_command_line_values_are_typed_and_checked(self):
        self.write("main.tf.json", {"variable": {"copies": {"type": "number"}, "tags": {"type": "map(string)", "default": {}}}})
        run = self.run_for([("var", "copies=3"), ("var", 'tags={"team": "web"}')])
        self.assertEqual(run.root.variable("copies"), 3)
        self.assertEqual(run.root.variable("tags"), {"team": "web"})
        self.assertIn("a number is required", self.error_of("plan", "-var", "copies=many"))
        self.assertIn("Value for undeclared variable", self.error_of("plan", "-var", "copies=1", "-var", "colour=red"))
        self.assertIn("No value for required variable", self.error_of("plan"))


class ModuleTests(Base):
    def test_fixture_expands_into_namespaced_addresses(self):
        site = self.site()
        out, _ = self.cli("addresses", "-var-file=prod.tfvars", directory=site)
        self.assertEqual(out.split(), SITE_ADDRESSES)
        out, _ = self.cli("addresses", directory=site)
        self.assertEqual(out.split(), ["local_file.index", 'module.web["blog"].local_file.page[0]', 'module.web["docs"].local_file.page[0]'])
        rows = json.loads(self.cli("addresses", "--json", "-var-file=prod.tfvars", directory=site)[0])
        page = next(row for row in rows if row["address"] == 'module.web["docs"].local_file.page[1]')
        self.assertEqual(page["module"], 'module.web["docs"]')
        self.assertEqual(page["index_key"], 1)
        self.assertEqual(page["attributes"], {"filename": "./out/prod-site-docs-1.txt", "content": "# prod-site-docs\nHello from the docs\n"})

    def test_module_arguments_are_checked_against_the_child_variables(self):
        self.write("modules/child/main.tf.json", CHILD)
        self.write("main.tf.json", {"module": {"one": {"source": "./modules/child", "label": "a", "colour": "red"}}})
        self.assertIn('passes "colour"', self.error_of("plan"))
        self.write("main.tf.json", {"module": {"one": {"source": "./modules/child"}}})
        self.assertIn('must set "label"', self.error_of("plan"))

    def test_path_module_is_relative_and_remote_sources_are_rejected(self):
        self.write("modules/child/main.tf.json", {"output": {"where": {"value": "${path.module}"}}})
        self.write("main.tf.json", {"module": {"one": {"source": "./modules/child"}}, "output": {"where": {"value": "${module.one.where}"}, "root": {"value": "${path.module}"}}})
        outputs = json.loads(self.cli("outputs", "--json")[0])
        self.assertEqual(outputs["where"]["value"], "modules/child")
        self.assertEqual(outputs["root"]["value"], ".")
        self.write("main.tf.json", {"module": {"vpc": {"source": "terraform-aws-modules/vpc/aws", "version": "~> 5.1"}}})
        self.assertIn("Module not installed", self.error_of("plan"))
        self.write("main.tf", 'resource "local_file" "x" {}\n')
        self.assertIn("HCL native syntax", self.error_of("plan"))

    def test_duplicate_declarations_across_files_are_errors(self):
        self.write("a.tf.json", {"variable": {"env": {}}})
        self.write("b.tf.json", {"variable": {"env": {}}})
        err = self.error_of("plan")
        self.assertIn("Duplicate variable declaration", err)
        self.assertIn("a.tf.json and again in b.tf.json", err)


class ExpansionTests(Base):
    def test_count_shifts_addresses_but_for_each_keys_are_stable(self):
        self.write("main.tf.json", {
            "variable": {"names": {"type": "list(string)", "default": ["a", "b", "c"]}},
            "resource": {"local_file": {
                "by_index": {"count": "${length(var.names)}", "filename": "${var.names[count.index]}.txt", "content": "${var.names[count.index]}"},
                "by_key": {"for_each": "${toset(var.names)}", "filename": "${each.key}.txt", "content": "${each.value}"},
            }},
        })
        self.cli("apply")
        plan = self.plan_json("-var", 'names=["a", "c"]')
        actions = {change["address"]: change["action"] for change in plan["changes"]}
        self.assertEqual(actions["local_file.by_index[0]"], "noop")
        self.assertEqual(actions["local_file.by_index[1]"], "replace")
        self.assertEqual(actions["local_file.by_index[2]"], "delete")
        self.assertEqual(actions['local_file.by_key["a"]'], "noop")
        self.assertEqual(actions['local_file.by_key["b"]'], "delete")
        self.assertEqual(actions['local_file.by_key["c"]'], "noop")
        self.assertEqual(plan["summary"], {"add": 1, "change": 0, "destroy": 3, "move": 0})

    def test_for_each_needs_a_map_or_set_of_known_non_sensitive_keys(self):
        self.write("main.tf.json", {"resource": {"local_file": {"x": {"for_each": '${["a", "b"]}', "filename": "${each.key}"}}}})
        self.assertIn("must be a map, or set of strings", self.error_of("plan"))
        self.write("main.tf.json", {"resource": {"local_file": {
            "seed": {"filename": "seed.txt"},
            "x": {"for_each": "${toset([local_file.seed.id])}", "filename": "${each.key}"},
        }}})
        self.assertIn("cannot be determined until apply", self.error_of("plan"))
        self.write("main.tf.json", {"variable": {"token": {"default": "t", "sensitive": True}}, "resource": {"local_file": {"x": {"for_each": "${toset([var.token])}", "filename": "${each.key}"}}}})
        self.assertIn("Sensitive values", self.error_of("plan"))

    def test_count_and_each_outside_their_blocks_are_errors(self):
        self.write("main.tf.json", {"resource": {"local_file": {"x": {"filename": "${count.index}.txt"}}}})
        self.assertIn('Reference to "count" in non-counted context', self.error_of("plan"))
        self.write("main.tf.json", {"locals": {"k": "${each.key}"}})
        self.assertIn('Reference to "each" in context without for_each', self.error_of("plan"))
        self.write("main.tf.json", {"resource": {"local_file": {"x": {"count": 1, "for_each": "${toset([])}", "filename": "x"}}}})
        self.assertIn('Invalid combination of "count" and "for_each"', self.error_of("plan"))

    def test_a_reference_cycle_is_reported_with_its_path(self):
        self.write("main.tf.json", {"locals": {"a": "${local.b}-x", "b": "${local.a}-y"}})
        err = self.error_of("plan")
        self.assertIn("Error: Cycle", err)
        self.assertIn("local.a -> local.b -> local.a", err)


class OutputTests(Base):
    def test_outputs_hide_sensitive_values_but_json_shows_them(self):
        site = self.site()
        out, _ = self.cli("outputs", "-var-file=prod.tfvars", directory=site)
        self.assertIn('index_file = "./out/index.txt"', out)
        self.assertIn("token = <sensitive>", out)
        self.assertNotIn("s3cr3t", out)
        outputs = json.loads(self.cli("outputs", "--json", directory=site)[0])
        self.assertEqual(outputs["token"], {"sensitive": True, "value": "s3cr3t-token"})
        self.assertEqual(outputs["blog_files"]["value"], ["./out/dev-site-blog-0.txt"])

    def test_root_output_derived_from_a_sensitive_value_must_be_marked(self):
        self.write("modules/child/main.tf.json", CHILD)
        config = {
            "variable": {"token": {"type": "string", "default": "abc", "sensitive": True}},
            "module": {"one": {"source": "./modules/child", "label": "one", "secret": "${var.token}"}},
            "output": {"file": {"value": "${module.one.file}"}, "shout": {"value": "${module.one.shout}"}},
        }
        self.write("main.tf.json", config)
        err = self.error_of("outputs")
        self.assertIn("Output refers to sensitive values", err)
        self.assertIn('output "shout"', err)
        config["output"]["shout"]["sensitive"] = True
        self.write("main.tf.json", config)
        out, _ = self.cli("outputs")
        self.assertIn('file = "one.txt"', out)
        self.assertIn("shout = <sensitive>", out)

    def test_computed_attributes_are_known_after_apply(self):
        self.write("main.tf.json", {"resource": {"local_file": {"a": {"filename": "a.txt"}}}, "output": {"name": {"value": "${local_file.a.filename}"}, "id": {"value": "${local_file.a.id}"}, "label": {"value": "id=${local_file.a.id}"}}})
        out, _ = self.cli("outputs")
        self.assertIn('name = "a.txt"', out)
        self.assertIn("id = (known after apply)", out)
        self.assertIn("label = (known after apply)", out)


class MovedTests(Base):
    def applied_site(self):
        site = self.site()
        self.cli("apply", "-var-file=prod.tfvars", directory=site)
        return site

    def test_a_rename_without_moved_destroys_and_creates(self):
        site = self.applied_site()
        with open(os.path.join(FIXTURES, "renamed", "main.tf.json"), encoding="utf-8") as handle:
            config = json.load(handle)
        del config["moved"]
        with open(os.path.join(site, "main.tf.json"), "w", encoding="utf-8") as handle:
            json.dump(config, handle)
        plan = self.plan_json("-var-file=prod.tfvars", directory=site)
        self.assertEqual(plan["summary"], {"add": 5, "change": 0, "destroy": 5, "move": 0})

    def test_moved_blocks_turn_the_rename_into_a_no_op(self):
        site = self.applied_site()
        shutil.copy(os.path.join(FIXTURES, "renamed", "main.tf.json"), os.path.join(site, "main.tf.json"))
        out, _ = self.cli("plan", "-var-file=prod.tfvars", directory=site)
        self.assertIn('# module.web["blog"].local_file.page[1] has moved to module.site["blog"].local_file.page[1]', out)
        self.assertIn("# local_file.index has moved to local_file.home", out)
        self.assertIn("Plan: 0 to add, 0 to change, 0 to destroy.", out)
        plan = self.plan_json("-var-file=prod.tfvars", directory=site)
        self.assertEqual(plan["summary"]["move"], 5)
        self.cli("apply", "-var-file=prod.tfvars", directory=site)
        self.assertIn("No changes", self.cli("plan", "-var-file=prod.tfvars", directory=site)[0])

    def test_chained_moves_resolve_in_any_order_and_cycles_fail(self):
        self.write("main.tf.json", {"resource": {"local_file": {"a": {"filename": "f.txt"}}}})
        self.cli("apply")
        self.write("main.tf.json", {
            "resource": {"local_file": {"c": {"filename": "f.txt"}}},
            "moved": [{"from": "local_file.b", "to": "local_file.c"}, {"from": "local_file.a", "to": "local_file.b"}],
        })
        plan = self.plan_json()
        self.assertEqual(plan["moves"], [{"from": "local_file.a", "to": "local_file.c"}])
        self.assertEqual(plan["summary"]["add"] + plan["summary"]["destroy"], 0)
        self.write("main.tf.json", {
            "resource": {"local_file": {"z": {"filename": "f.txt"}}},
            "moved": [{"from": "local_file.a", "to": "local_file.b"}, {"from": "local_file.b", "to": "local_file.a"}],
        })
        self.assertIn("Cyclic dependency in move statements", self.error_of("plan"))

    def test_moved_rejects_a_source_that_still_exists_and_a_type_change(self):
        self.write("main.tf.json", {"resource": {"local_file": {"a": {"filename": "a"}, "b": {"filename": "b"}}}, "moved": [{"from": "local_file.a", "to": "local_file.b"}]})
        self.assertIn("Moved object still exists", self.error_of("plan"))
        self.write("main.tf.json", {"resource": {"local_sensitive_file": {"b": {"filename": "b"}}}, "moved": [{"from": "local_file.a", "to": "local_sensitive_file.b"}]})
        self.assertIn("Resource type mismatch", self.error_of("plan"))

    def test_moving_a_single_module_into_a_for_each_instance(self):
        self.write("modules/child/main.tf.json", CHILD)
        self.write("main.tf.json", {"module": {"web": {"source": "./modules/child", "label": "blog"}}})
        self.cli("apply")
        self.write("main.tf.json", {
            "module": {"web": {"source": "./modules/child", "for_each": "${toset([\"blog\", \"docs\"])}", "label": "${each.key}"}},
            "moved": [{"from": "module.web", "to": "module.web[\"blog\"]"}],
        })
        plan = self.plan_json()
        self.assertEqual(plan["moves"], [{"from": "module.web.local_file.page", "to": 'module.web["blog"].local_file.page'}])
        actions = {change["address"]: change["action"] for change in plan["changes"]}
        self.assertEqual(actions, {'module.web["blog"].local_file.page': "noop", 'module.web["docs"].local_file.page': "create"})


class StateTests(Base):
    TOFU_STATE = {
        "version": 4,
        "terraform_version": "1.13.2",
        "serial": 3,
        "lineage": "8d5c0a3e",
        "resources": [
            {"mode": "managed", "type": "local_file", "name": "index", "instances": [{"schema_version": 0, "attributes": {"filename": "./out/index.txt", "content": "x", "id": "11f6ad8e"}}]},
            {"module": 'module.web["blog"]', "mode": "managed", "type": "local_file", "name": "page", "instances": [{"index_key": 0, "attributes": {"filename": "a"}}, {"index_key": 1, "attributes": {"filename": "b"}}]},
            {"mode": "data", "type": "local_file", "name": "read", "instances": [{"attributes": {}}]},
        ],
    }

    def test_reads_version_4_state_into_instance_addresses(self):
        self.write("terraform.tfstate", self.TOFU_STATE)
        state = main.load_state(os.path.join(self.root, "terraform.tfstate"))
        self.assertEqual(sorted(state["instances"]), ["local_file.index", 'module.web["blog"].local_file.page[0]', 'module.web["blog"].local_file.page[1]'])
        self.assertTrue(state["written_by_tofu"])
        address = main.parse_address('module.web["blog"].local_file.page[1]')
        self.assertEqual(address, main.Address((("web", "blog"),), "local_file", "page", 1))
        self.assertEqual(str(address), 'module.web["blog"].local_file.page[1]')
        with self.assertRaises(main.MiniModError):
            main.parse_address("module..web")

    def test_apply_writes_its_own_state_and_refuses_to_overwrite_tofus(self):
        self.write("main.tf.json", {"resource": {"local_file": {"a": {"count": 2, "filename": "a${count.index}"}}}})
        self.cli("apply")
        with open(os.path.join(self.root, "minimod.tfstate"), encoding="utf-8") as handle:
            state = json.load(handle)
        self.assertEqual(state["version"], 4)
        self.assertEqual(state["serial"], 1)
        self.assertEqual(state["resources"][0]["instances"][1], {"index_key": 1, "attributes": {"filename": "a1"}})
        self.cli("apply")
        self.assertEqual(main.load_state(os.path.join(self.root, "minimod.tfstate"))["serial"], 1)
        self.write("terraform.tfstate", self.TOFU_STATE)
        self.assertIn("Refusing to overwrite state", self.error_of("apply", "--state", "terraform.tfstate"))

    def test_eval_works_like_tofu_console(self):
        site = self.site()
        out, _ = self.cli("eval", "local.prefix", "-var", "env=prod", directory=site)
        self.assertEqual(out.strip(), '"prod-site"')
        out, _ = self.cli("eval", "var.token", directory=site)
        self.assertEqual(out.strip(), "(sensitive value)")
        result = json.loads(self.cli("eval", "--json", 'module.web["docs"].files', directory=site)[0])
        self.assertEqual(result, {"value": ["./out/dev-site-docs-0.txt"], "sensitive": False, "known": True})
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            self.assertEqual(main.main(["eval", "--chdir", site]), 2)


if __name__ == "__main__":
    unittest.main()
