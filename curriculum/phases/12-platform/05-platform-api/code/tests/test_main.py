import copy
import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import main  # noqa: E402

FIXTURES = os.path.join(os.path.dirname(__file__), "..", "fixtures")


def fixture(name):
    with open(os.path.join(FIXTURES, name), encoding="utf-8") as handle:
        return json.load(handle)


def workload(name):
    result = main.validate(fixture(name), name)
    assert not result.errors, result.errors
    return result.workload


def rendered(*names, **kwargs):
    return main.render([workload(n) for n in names], copy.deepcopy(main.DEFAULT_PLATFORM), **kwargs)


def find(objects, kind, name):
    return next(o for o in objects if o["kind"] == kind and o["metadata"]["name"] == name)


def run(argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = main.main(argv)
    return code, out.getvalue(), err.getvalue()


class ValidationTests(unittest.TestCase):
    def test_sample_workloads_are_valid_in_both_versions(self):
        for name in ("storefront.json", "orders.json", "orders-next.json", "storefront-autoscaled.json"):
            result = main.validate(fixture(name), name)
            self.assertEqual(result.errors, [], name)
        self.assertEqual(main.validate(fixture("storefront.json")).version, "v1alpha1")
        self.assertEqual(main.validate(fixture("orders.json")).version, "v1beta1")

    def test_every_invalid_fixture_fails_with_its_expected_message(self):
        for item in fixture("invalid.json")["items"]:
            expected = item["metadata"]["annotations"]["cc.test/expect"]
            errors = main.validate(item).errors
            self.assertTrue(any(expected in e for e in errors), (item["metadata"]["name"], errors))

    def test_unknown_field_names_the_path_and_suggests_the_closest_field(self):
        obj = fixture("orders.json")
        obj["spec"]["container"]["imge"] = "x:1"
        errors = main.validate(obj).errors
        self.assertIn("spec.container.imge: unknown field (did you mean 'image'?)", errors)

    def test_wrong_type_is_reported_and_rules_on_that_node_are_skipped(self):
        obj = fixture("orders.json")
        obj["spec"]["scaling"] = {"min": "2", "max": 5}
        errors = main.validate(obj).errors
        self.assertEqual(errors, ['spec.scaling.min: Invalid value: "2": must be of type integer'])

    def test_all_errors_are_collected_not_just_the_first(self):
        obj = fixture("orders.json")
        obj["spec"]["size"] = "huge"
        obj["spec"]["container"]["port"] = 70000
        del obj["spec"]["owner"]
        errors = main.validate(obj).errors
        self.assertEqual(len(errors), 3, errors)

    def test_unsupported_version_lists_the_served_versions(self):
        obj = fixture("orders.json")
        obj["apiVersion"] = "platform.cloudcaptain.io/v2"
        errors = main.validate(obj).errors
        self.assertEqual(errors, ["apiVersion: unsupported version 'v2'; served versions: v1alpha1, v1beta1"])

    def test_digest_and_registry_port_images(self):
        rules = main.IMAGE_RULES
        ok = ["python:3.12-alpine", "registry.local:5000/team/app:1.0", "ghcr.io/a/b@sha256:" + "a" * 64]
        bad = ["python", "registry.local:5000/team/app", "python:latest"]
        for image in ok:
            self.assertTrue(all(r.check(image) for r in rules), image)
        for image in bad:
            self.assertFalse(all(r.check(image) for r in rules), image)


class ConversionTests(unittest.TestCase):
    def test_v1alpha1_maps_onto_v1beta1_fields(self):
        beta = main.convert(fixture("storefront.json"), "v1beta1")
        spec = beta["spec"]
        self.assertEqual(beta["apiVersion"], "platform.cloudcaptain.io/v1beta1")
        self.assertEqual(spec["owner"], "web")
        self.assertEqual(spec["scaling"], {"replicas": 2})
        self.assertEqual(spec["route"], {"host": "shop.cc.test", "path": "/"})
        self.assertEqual(spec["container"]["env"], [{"name": "LOG_LEVEL", "value": "info"}])
        self.assertEqual(spec["dependencies"], [{"name": "orders", "type": "workload"}])
        self.assertEqual(main.validate(beta).errors, [])

    def test_round_trip_through_v1alpha1_is_lossless(self):
        original = fixture("orders.json")
        original["spec"]["dependencies"] = [{"name": "ledger", "type": "postgres"}, {"name": "payments", "type": "workload"}]
        original["spec"]["overrides"] = [{"kind": "Deployment", "patch": {"spec": {"template": {"spec": {"priorityClassName": "high"}}}}}]
        alpha = main.convert(original, "v1alpha1")
        self.assertIn(main.PRESERVE_ANNOTATION, alpha["metadata"]["annotations"])
        self.assertEqual(alpha["spec"]["replicas"], 2)
        self.assertEqual(main.validate(alpha).errors, [])
        self.assertEqual(main.convert(alpha, "v1beta1"), original)

    def test_no_annotation_when_v1alpha1_can_hold_everything(self):
        beta = main.convert(fixture("storefront.json"), "v1beta1")
        alpha = main.convert(beta, "v1alpha1")
        self.assertNotIn("annotations", alpha["metadata"])
        self.assertEqual(alpha, fixture("storefront.json"))


class RenderTests(unittest.TestCase):
    def test_orders_expands_into_eleven_objects(self):
        objects = rendered("orders.json").objects
        kinds = sorted(f"{o['kind']}/{o['metadata']['name']}" for o in objects)
        self.assertEqual(
            kinds,
            sorted(
                [
                    "ServiceAccount/orders",
                    "Deployment/orders",
                    "Service/orders",
                    "HorizontalPodAutoscaler/orders",
                    "PodDisruptionBudget/orders",
                    "NetworkPolicy/orders",
                    "HTTPRoute/orders",
                    "Secret/orders-db",
                    "Service/orders-db",
                    "StatefulSet/orders-db",
                    "NetworkPolicy/orders-db",
                ]
            ),
        )

    def test_replicas_belong_to_the_hpa_when_autoscaling(self):
        objects = rendered("orders.json", "storefront.json").objects
        self.assertNotIn("replicas", find(objects, "Deployment", "orders")["spec"])
        self.assertEqual(find(objects, "HorizontalPodAutoscaler", "orders")["spec"]["minReplicas"], 2)
        self.assertEqual(find(objects, "Deployment", "storefront")["spec"]["replicas"], 2)

    def test_pdb_only_when_at_least_two_replicas(self):
        obj = fixture("storefront.json")
        obj["spec"]["replicas"] = 1
        single = main.render([main.validate(obj).workload], main.DEFAULT_PLATFORM).objects
        self.assertFalse(any(o["kind"] == "PodDisruptionBudget" for o in single))
        pdb = find(rendered("storefront.json").objects, "PodDisruptionBudget", "storefront")
        self.assertEqual(pdb["spec"]["maxUnavailable"], 1)

    def test_platform_defaults_are_applied(self):
        deployment = find(rendered("storefront.json").objects, "Deployment", "storefront")
        pod = deployment["spec"]["template"]["spec"]
        container = pod["containers"][0]
        self.assertTrue(pod["securityContext"]["runAsNonRoot"])
        self.assertEqual(pod["securityContext"]["seccompProfile"], {"type": "RuntimeDefault"})
        self.assertFalse(pod["automountServiceAccountToken"])
        self.assertEqual(container["securityContext"]["capabilities"], {"drop": ["ALL"]})
        self.assertTrue(container["securityContext"]["readOnlyRootFilesystem"])
        self.assertEqual(container["readinessProbe"]["httpGet"], {"path": "/", "port": "http"})
        self.assertEqual(container["resources"], {"requests": {"cpu": "100m", "memory": "128Mi"}, "limits": {"memory": "256Mi"}})
        self.assertEqual(container["volumeMounts"], [{"name": "tmp", "mountPath": "/tmp"}])

    def test_dependencies_become_environment_variables(self):
        objects = rendered("orders.json", "storefront.json").objects
        orders_env = {e["name"]: e for e in find(objects, "Deployment", "orders")["spec"]["template"]["spec"]["containers"][0]["env"]}
        self.assertEqual(orders_env["DB_HOST"]["value"], "orders-db")
        self.assertEqual(orders_env["DB_PORT"]["value"], "5432")
        self.assertEqual(orders_env["DB_PASSWORD"]["valueFrom"], {"secretKeyRef": {"name": "orders-db", "key": "password"}})
        storefront_env = {e["name"]: e for e in find(objects, "Deployment", "storefront")["spec"]["template"]["spec"]["containers"][0]["env"]}
        self.assertEqual(storefront_env["ORDERS_URL"]["value"], "http://orders:80")

    def test_network_policies_follow_the_dependency_graph(self):
        objects = rendered("orders.json", "storefront.json").objects
        orders = find(objects, "NetworkPolicy", "orders")["spec"]["ingress"]
        self.assertEqual(orders[0]["from"], [{"namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": "envoy-gateway-system"}}}])
        self.assertEqual(orders[1]["from"], [{"podSelector": {"matchLabels": {"app.kubernetes.io/name": "storefront"}}}])
        self.assertEqual(orders[1]["ports"], [{"port": 8080, "protocol": "TCP"}])
        db = find(objects, "NetworkPolicy", "orders-db")["spec"]["ingress"]
        self.assertEqual(db, [{"from": [{"podSelector": {"matchLabels": {"app.kubernetes.io/name": "orders"}}}], "ports": [{"port": 5432, "protocol": "TCP"}]}])

    def test_missing_dependency_target_is_a_warning(self):
        result = rendered("storefront.json")
        self.assertTrue(any("dependency orders is not among the inputs" in w for w in result.warnings), result.warnings)
        alone = rendered("orders.json").objects
        self.assertEqual(len(find(alone, "NetworkPolicy", "orders")["spec"]["ingress"]), 1)

    def test_gateway_and_namespace_flags(self):
        code, out, _ = run(["render", os.path.join(FIXTURES, "storefront.json"), "-o", "json", "--namespace", "shop", "--gateway", "infra/edge"])
        self.assertEqual(code, 0)
        items = json.loads(out)["items"]
        route = find(items, "HTTPRoute", "storefront")
        self.assertEqual(route["spec"]["parentRefs"], [{"name": "edge", "namespace": "infra"}])
        self.assertEqual(route["spec"]["rules"][0]["backendRefs"], [{"name": "storefront", "port": 80}])
        self.assertTrue(all(o["metadata"]["namespace"] == "shop" for o in items))

    def test_overrides_are_merge_patches_and_must_target_a_rendered_kind(self):
        obj = fixture("orders.json")
        obj["spec"]["overrides"] = [{"kind": "Deployment", "patch": {"spec": {"template": {"spec": {"priorityClassName": "high"}}}}}]
        objects = main.render([main.validate(obj).workload], main.DEFAULT_PLATFORM).objects
        pod = find(objects, "Deployment", "orders")["spec"]["template"]["spec"]
        self.assertEqual(pod["priorityClassName"], "high")
        self.assertTrue(pod["securityContext"]["runAsNonRoot"])
        fixed = fixture("storefront.json")
        fixed["spec"]["replicas"] = 1
        beta = main.convert(fixed, "v1beta1")
        beta["spec"]["overrides"] = [{"kind": "HorizontalPodAutoscaler", "patch": {"spec": {"maxReplicas": 9}}}]
        errors = main.validate(beta).errors
        self.assertTrue(errors and "renders no HorizontalPodAutoscaler" in errors[0], errors)

    def test_merge_patch_semantics(self):
        target = {"a": {"b": 1, "c": 2}, "l": [1, 2, 3]}
        self.assertEqual(main.merge_patch(target, {"a": {"b": None, "d": 4}, "l": [9]}), {"a": {"c": 2, "d": 4}, "l": [9]})
        self.assertEqual(target["a"], {"b": 1, "c": 2})

    def test_database_password_is_deterministic_per_seed(self):
        one = main.password_for("seed-a", "shop", "orders", "db")
        self.assertEqual(one, main.password_for("seed-a", "shop", "orders", "db"))
        self.assertNotEqual(one, main.password_for("seed-b", "shop", "orders", "db"))
        self.assertNotEqual(one, main.password_for("seed-a", "other", "orders", "db"))
        self.assertEqual(len(one), 24)

    def test_render_is_deterministic(self):
        self.assertEqual(rendered("orders.json", "storefront.json").objects, rendered("orders.json", "storefront.json").objects)


class DiffTests(unittest.TestCase):
    def test_image_and_size_change_restarts_pods(self):
        old, new = rendered("orders.json").objects, rendered("orders-next.json").objects
        changes, warnings = main.diff_objects(old, new)
        by_key = {c.key: c for c in changes}
        self.assertEqual(sorted(by_key), ["Deployment/orders", "HTTPRoute/orders"])
        self.assertTrue(by_key["Deployment/orders"].restarts)
        self.assertFalse(by_key["HTTPRoute/orders"].restarts)
        paths = [p for p, _, _ in by_key["Deployment/orders"].fields]
        self.assertIn("spec.template.spec.containers[0].image", paths)
        self.assertEqual(warnings, [])

    def test_switching_to_autoscaling_warns_about_replicas(self):
        old, new = rendered("storefront.json").objects, rendered("storefront-autoscaled.json").objects
        changes, warnings = main.diff_objects(old, new)
        self.assertIn(("add", "HorizontalPodAutoscaler/storefront"), [(c.action, c.key) for c in changes])
        self.assertEqual(len(warnings), 1)
        self.assertIn("resets the Deployment to 1 replica (from 2)", warnings[0])

    def test_immutable_field_change_is_flagged(self):
        old = [{"kind": "StatefulSet", "metadata": {"name": "db"}, "spec": {"selector": {"matchLabels": {"a": "1"}}}}]
        new = [{"kind": "StatefulSet", "metadata": {"name": "db"}, "spec": {"selector": {"matchLabels": {"a": "2"}}}}]
        changes, warnings = main.diff_objects(old, new)
        self.assertEqual(changes[0].recreate, ["spec.selector"])
        self.assertIn("immutable", warnings[0])

    def test_diff_cli_exit_codes(self):
        same = os.path.join(FIXTURES, "orders.json")
        self.assertEqual(run(["diff", same, same])[0], 0)
        code, out, _ = run(["diff", same, os.path.join(FIXTURES, "orders-next.json")])
        self.assertEqual(code, 1)
        self.assertIn("~ Deployment/orders   [restarts pods]", out)


class CrdAndScoreTests(unittest.TestCase):
    def test_crd_carries_the_same_rules_as_the_validator(self):
        crd = main.crd()
        version = crd["spec"]["versions"][0]
        self.assertEqual((version["name"], version["served"], version["storage"]), ("v1beta1", True, True))
        spec = version["schema"]["openAPIV3Schema"]["properties"]["spec"]
        scaling = spec["properties"]["scaling"]["x-kubernetes-validations"]
        self.assertEqual([r["message"] for r in scaling], [r.message for r in main.SCALING_RULES])
        deps = spec["properties"]["dependencies"]
        self.assertEqual((deps["x-kubernetes-list-type"], deps["x-kubernetes-list-map-keys"]), ("map", ["name"]))
        self.assertEqual(spec["properties"]["size"]["enum"], ["small", "medium", "large"])

    def test_every_string_and_list_in_the_crd_is_bounded(self):
        def walk(node, path):
            if node.get("type") == "string":
                self.assertIn("maxLength", node, path)
            if node.get("type") == "array":
                self.assertIn("maxItems", node, path)
            for key, child in (node.get("properties") or {}).items():
                walk(child, f"{path}.{key}")
            if "items" in node:
                walk(node["items"], f"{path}[]")

        walk(main.crd()["spec"]["versions"][0]["schema"]["openAPIV3Schema"]["properties"]["spec"], "spec")

    def test_to_score_uses_placeholders_for_dependencies(self):
        score = main.to_score(workload("orders.json"), main.DEFAULT_PLATFORM)
        variables = score["containers"]["main"]["variables"]
        self.assertEqual(variables["DB_HOST"], "${resources.db.host}")
        self.assertEqual(variables["LOG_LEVEL"], "info")
        self.assertEqual(score["service"]["ports"], {"http": {"port": 80, "targetPort": 8080}})
        self.assertEqual(score["resources"]["route"], {"type": "route", "params": {"host": "shop.cc.test", "path": "/api/orders", "port": 80}})
        self.assertEqual(score["resources"]["db"], {"type": "postgres"})

    def test_yaml_scalars_are_quoted_when_yaml_would_reinterpret_them(self):
        self.assertEqual(main.scalar("true"), '"true"')
        self.assertEqual(main.scalar("100m"), '"100m"')
        self.assertEqual(main.scalar("8080"), '"8080"')
        self.assertEqual(main.scalar("python:3.12-alpine"), '"python:3.12-alpine"')
        self.assertEqual(main.scalar("app.kubernetes.io/name"), "app.kubernetes.io/name")
        self.assertEqual(main.scalar(80), "80")
        self.assertEqual(main.to_yaml({"a": [{"b": 1, "c": []}], "d": {}}), "a:\n- b: 1\n  c: []\nd: {}")


class CliTests(unittest.TestCase):
    def test_validate_exit_codes_and_json(self):
        self.assertEqual(run(["validate", os.path.join(FIXTURES, "orders.json")])[0], 0)
        code, out, _ = run(["validate", "--json", os.path.join(FIXTURES, "invalid.json")])
        self.assertEqual(code, 1)
        results = json.loads(out)["results"]
        self.assertEqual(len(results), len(fixture("invalid.json")["items"]))
        self.assertFalse(any(r["valid"] for r in results))

    def test_missing_file_and_bad_json_exit_2_on_stderr(self):
        code, out, err = run(["render", "/nonexistent/w.json"])
        self.assertEqual((code, out), (2, ""))
        self.assertIn("/nonexistent/w.json", err)
        with tempfile.TemporaryDirectory() as tmp:
            bad = os.path.join(tmp, "bad.json")
            with open(bad, "w", encoding="utf-8") as handle:
                handle.write("{not json")
            code, _, err = run(["validate", bad])
        self.assertEqual(code, 2)
        self.assertIn("not valid JSON", err)

    def test_render_refuses_invalid_input_and_names_the_field(self):
        code, out, err = run(["render", os.path.join(FIXTURES, "invalid.json")])
        self.assertEqual((code, out), (1, ""))
        self.assertIn("min-over-max: spec.scaling: Invalid value: min must not be greater than max", err)

    def test_to_score_writes_one_file_per_workload(self):
        with tempfile.TemporaryDirectory() as tmp:
            code, out, _ = run(["to-score", os.path.join(FIXTURES, "orders.json"), os.path.join(FIXTURES, "storefront.json"), "--out", tmp])
            self.assertEqual(code, 0)
            self.assertEqual(sorted(os.listdir(tmp)), ["orders.score.yaml", "storefront.score.yaml"])
            with open(os.path.join(tmp, "storefront.score.yaml"), encoding="utf-8") as handle:
                score = json.load(handle)
        self.assertEqual(score["resources"]["orders"], {"type": "service-port", "params": {"workload": "orders", "port": "http"}})


if __name__ == "__main__":
    unittest.main()
