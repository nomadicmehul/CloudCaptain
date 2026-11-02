import base64
import copy
import glob
import json
import os
import shutil
import ssl
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import main  # noqa: E402

FIXTURES = os.path.join(os.path.dirname(__file__), "..", "fixtures")
MANIFESTS = os.path.join(FIXTURES, "manifests")
DIGEST = "sha256:" + "ab" * 32


def fixture(name):
    with open(os.path.join(FIXTURES, name), encoding="utf-8") as handle:
        return json.load(handle)


def manifest(name):
    with open(os.path.join(MANIFESTS, name + ".json"), encoding="utf-8") as handle:
        return json.load(handle)


def run(argv, stdin=None):
    out, err = StringIO(), StringIO()
    old = sys.stdin
    if stdin is not None:
        sys.stdin = StringIO(stdin)
    try:
        with redirect_stdout(out), redirect_stderr(err):
            code = main.main(argv)
    finally:
        sys.stdin = old
    return code, out.getvalue(), err.getvalue()


def cel(expression, obj=None, **request):
    req = main.request_from_object(obj, **request) if obj is not None else main.AdmissionRequest("CREATE", None)
    return main.evaluate(main.compile_cel(expression), main.cel_environment(req))


def pod(containers, labels=None, pod_context=None):
    spec = {"containers": containers}
    if pod_context is not None:
        spec["securityContext"] = pod_context
    return {"apiVersion": "v1", "kind": "Pod", "metadata": {"name": "p", "labels": labels or {}}, "spec": spec}


def policies(*raw):
    return main.load_policies({"policies": list(raw)})


class CelLanguageTests(unittest.TestCase):
    def test_arithmetic_has_no_implicit_conversions_and_integer_division_truncates(self):
        self.assertEqual(cel("1 + 2 * 3 - 4 / 2"), 5)
        self.assertEqual(cel("7 / -2"), -3)
        self.assertEqual(cel("-7 % 2"), -1)
        self.assertEqual(cel("'kube' + '-' + 'system'"), "kube-system")
        self.assertEqual(cel("[1] + [2, 3]"), [1, 2, 3])
        self.assertTrue(cel("1 == 1.0"))
        self.assertFalse(cel("1 == '1'"))
        for broken, message in (("1 + 1.0", "no such overload"), ("1 / 0", "division by zero"), ("1 < 'a'", "no such overload"), ("!1", "no such overload")):
            with self.assertRaises(main.CelError) as caught:
                cel(broken)
            self.assertIn(message, str(caught.exception))

    def test_a_missing_field_is_an_error_and_has_tests_presence_without_one(self):
        obj = pod([{"name": "app", "image": "nginx"}])
        with self.assertRaises(main.CelError) as caught:
            cel("object.spec.containers[0].securityContext.privileged", obj)
        self.assertEqual(str(caught.exception), "no such key: securityContext")
        self.assertFalse(cel("has(object.spec.containers[0].securityContext)", obj))
        self.assertTrue(cel("has(object.metadata.labels)", obj))
        with self.assertRaises(main.CelError):
            cel("has(object.spec.containers[0].securityContext.privileged)", obj)
        with self.assertRaises(main.CelError) as caught:
            cel("object.spec.containers[3]", obj)
        self.assertIn("index out of bounds", str(caught.exception))

    def test_and_or_absorb_errors_in_either_order_like_cel(self):
        obj = pod([{"name": "app", "image": "nginx"}])
        self.assertFalse(cel("false && object.missing", obj))
        self.assertFalse(cel("object.missing && false", obj))
        self.assertTrue(cel("object.missing || true", obj))
        self.assertTrue(cel("true || object.missing", obj))
        for raising in ("object.missing && true", "object.missing || false", "1 && true"):
            with self.assertRaises(main.CelError):
                cel(raising, obj)
        self.assertEqual(cel("true ? 'yes' : object.missing", obj), "yes")

    def test_all_returns_false_on_any_false_element_even_if_another_element_errors(self):
        obj = pod([{"name": "a", "image": "x"}, {"name": "b", "image": "x", "securityContext": {"privileged": True}}])
        self.assertFalse(cel("object.spec.containers.all(c, c.securityContext.privileged == false)", obj))
        self.assertTrue(cel("object.spec.containers.exists(c, c.securityContext.privileged)", obj))
        safe = pod([{"name": "a", "image": "x"}])
        with self.assertRaises(main.CelError):
            cel("object.spec.containers.all(c, c.securityContext.privileged == false)", safe)
        self.assertTrue(cel("object.spec.containers.all(c, !has(c.securityContext) || !has(c.securityContext.privileged) || !c.securityContext.privileged)", safe))
        self.assertTrue(cel("[1, 2, 3].exists_one(n, n > 2)"))
        self.assertEqual(cel("[1, 2, 3].filter(n, n % 2 == 1).map(n, n * 10)"), [10, 30])
        self.assertTrue(cel("{'a': 1, 'b': 2}.all(k, k.size() == 1)"))
        self.assertEqual(cel("object.spec.containers.map(c, c.name).join(', ')", obj), "a, b")

    def test_string_functions_and_matches_is_an_re2_search_not_a_full_match(self):
        self.assertTrue(cel("'registry.k8s.io/pause:3.10'.startsWith('registry.k8s.io/')"))
        self.assertTrue(cel("'nginx@sha256:abc'.contains('@sha256:')"))
        self.assertTrue(cel("'ghcr.io/x'.matches('^ghcr\\\\.io/')"))
        self.assertTrue(cel("'nginx:1.30'.matches('[0-9]+')"))
        self.assertTrue(cel(r"'abc123'.matches(r'\d+$')"))
        self.assertEqual(cel("size('héllo')"), 5)
        self.assertEqual(cel("'Kube-System'.lowerAscii()"), "kube-system")
        self.assertEqual(cel("'a,b'.split(',')"), ["a", "b"])
        self.assertTrue(cel("'x' in {'x': 1} && 2 in [1, 2]"))
        with self.assertRaises(main.CelError) as caught:
            cel("'abc'.matches('a(?=b)')")
        self.assertIn("unsupported Perl syntax", str(caught.exception))

    def test_compile_errors_name_the_position_and_the_problem(self):
        cases = {
            "foo.bar": "undeclared reference to 'foo'",
            "object.spec.containers.all(c, d)": "undeclared reference to 'd'",
            "'\\d+'": "invalid escape sequence",
            "object.": "expected an identifier",
            "1 +": "unexpected end of input",
            "has(1)": "invalid argument to has()",
            "object.metadata.nope()": "undeclared reference to 'nope'",
            "namespace == 'x'": "reserved identifier 'namespace'",
        }
        for source, fragment in cases.items():
            with self.assertRaises(main.CelSyntaxError) as caught:
                main.compile_cel(source)
            self.assertIn(fragment, str(caught.exception), source)
            self.assertTrue(str(caught.exception).startswith("ERROR: <input>:1:"), str(caught.exception))

    def test_request_namespace_object_and_lazy_variables_are_in_scope(self):
        obj = pod([{"name": "a", "image": "x"}])
        request = main.request_from_object(obj, "shop", "CREATE", "jane", ["dev"])
        definitions = [("names", main.compile_cel("object.spec.containers.map(c, c.name)")), ("broken", main.compile_cel("object.nope"))]
        env = main.cel_environment(request, definitions)
        self.assertEqual(main.evaluate(main.compile_cel("request.userInfo.username + '@' + request.namespace"), env), "jane@shop")
        self.assertEqual(main.evaluate(main.compile_cel("namespaceObject.metadata.labels['kubernetes.io/metadata.name']"), env), "shop")
        self.assertEqual(main.evaluate(main.compile_cel("variables.names"), env), ["a"])
        self.assertTrue(main.evaluate(main.compile_cel("oldObject == null && request.operation == 'CREATE'"), env))
        self.assertTrue(main.evaluate(main.compile_cel("has(variables.broken) && size(variables.names) == 1"), env))
        with self.assertRaises(main.CelError):
            main.evaluate(main.compile_cel("variables.broken"), env)


class ImageTests(unittest.TestCase):
    def test_references_normalise_like_the_container_runtime(self):
        cases = {
            "nginx": ("docker.io", "library/nginx", None, None),
            "nginx:1.30-alpine": ("docker.io", "library/nginx", "1.30-alpine", None),
            "bitnami-free/app": ("docker.io", "bitnami-free/app", None, None),
            "index.docker.io/library/redis:7": ("docker.io", "library/redis", "7", None),
            "registry.k8s.io/pause:3.10@" + DIGEST: ("registry.k8s.io", "pause", "3.10", DIGEST),
            "localhost:5000/team/app": ("localhost:5000", "team/app", None, None),
            "localhost/app": ("localhost", "app", None, None),
        }
        for reference, want in cases.items():
            image = main.parse_image(reference)
            self.assertEqual((image.registry, image.repository, image.tag, image.digest), want, reference)
        for bad in ("", "Nginx", "a b", "registry.k8s.io//x"):
            with self.assertRaises(main.InputError):
                main.parse_image(bad)

    def test_allowed_registries_match_whole_path_components(self):
        allowed = ["registry.k8s.io", "docker.io/library"]
        self.assertTrue(main.registry_allowed(main.parse_image("registry.k8s.io/pause:3.10"), allowed))
        self.assertTrue(main.registry_allowed(main.parse_image("nginx"), allowed))
        self.assertFalse(main.registry_allowed(main.parse_image("registry.k8s.io.evil.example/pause"), allowed))
        self.assertFalse(main.registry_allowed(main.parse_image("docker.io/libraryx/nginx"), allowed))
        self.assertFalse(main.registry_allowed(main.parse_image("bitnami-free/app"), allowed))


class PolicyLoadingTests(unittest.TestCase):
    def test_bad_policy_files_are_input_errors_that_say_what_to_fix(self):
        cases = [
            ({"policies": []}, "at least one policy"),
            ({"policies": [{"name": "Bad_Name", "type": "no-privileged"}]}, "lowercase DNS label"),
            ({"policies": [{"name": "a", "type": "nope"}]}, "unknown type 'nope'"),
            ({"policies": [{"name": "a", "type": "no-privileged"}, {"name": "a", "type": "no-privileged"}]}, "duplicate policy name"),
            ({"policies": [{"name": "a", "type": "no-privileged", "actions": ["Deny", "Warn"]}]}, "Deny and Warn"),
            ({"policies": [{"name": "a", "type": "no-privileged", "actions": ["Block"]}]}, "unknown action"),
            ({"policies": [{"name": "a", "type": "default-requests", "requests": {"cpu": "1"}, "actions": ["Deny"]}]}, "mutating policy has no actions"),
            ({"policies": [{"name": "a", "type": "allowed-registries"}]}, "registries"),
            ({"policies": [{"name": "a", "type": "cel", "validations": [{"expression": "object.spec.containers.all(c, x)"}]}]}, "undeclared reference to 'x'"),
            ({"policies": [{"name": "a", "type": "no-privileged", "match": {"kinds": ["Service"]}}]}, "carries no pod template"),
            ({"policies": [{"name": "a", "type": "no-privileged", "failurePolicy": "Maybe"}]}, "failurePolicy"),
        ]
        for doc, fragment in cases:
            with self.assertRaises(main.InputError) as caught:
                main.load_policies(doc)
            self.assertIn(fragment, str(caught.exception))

    def test_defaults_mirror_validating_admission_policy(self):
        loaded = main.load_policies(fixture("policies.json"))
        by_name = {p.name: p for p in loaded}
        self.assertEqual(by_name["disallow-privileged"].actions, ["Deny"])
        self.assertEqual(by_name["disallow-privileged"].kinds, list(main.POD_SPEC_PATHS))
        self.assertEqual(by_name["memory-limit"].kinds, ["Pod"])
        self.assertEqual(by_name["memory-limit"].failure_policy, "Fail")
        self.assertEqual(by_name["default-requests"].actions, [])
        self.assertTrue(by_name["default-requests"].mutating)
        self.assertEqual(by_name["require-digest"].operations, ["CREATE", "UPDATE"])


class ValidatorTests(unittest.TestCase):
    def decide(self, raw, obj, **request):
        return main.admit(policies(*raw), main.request_from_object(obj, **request))

    def test_privileged_is_found_in_every_container_list_with_its_path(self):
        spec = {"initContainers": [{"name": "init", "image": "x", "securityContext": {"privileged": True}}], "containers": [{"name": "app", "image": "x"}], "ephemeralContainers": [{"name": "debug", "image": "x", "securityContext": {"privileged": True}}]}
        obj = {"kind": "Pod", "metadata": {"name": "p"}, "spec": spec}
        decision = self.decide([{"name": "np", "type": "no-privileged"}], obj)
        self.assertFalse(decision.allowed)
        self.assertEqual([v.message for v in decision.violations], ['container "init" is privileged (spec.initContainers[0].securityContext.privileged)', 'container "debug" is privileged (spec.ephemeralContainers[0].securityContext.privileged)'])

    def test_workloads_are_judged_by_their_pod_template(self):
        deployment = manifest("deployment-privileged")
        decision = self.decide([{"name": "np", "type": "no-privileged"}, {"name": "team", "type": "required-labels", "labels": ["team"]}], deployment)
        self.assertEqual(decision.policies("Deny"), ["np"])
        self.assertIn("spec.template.spec.containers[0]", decision.violations[0].message)
        cron = {"kind": "CronJob", "metadata": {"name": "c"}, "spec": {"jobTemplate": {"spec": {"template": {"metadata": {"labels": {}}, "spec": {"containers": [{"name": "job", "image": "x"}]}}}}}}
        missing = self.decide([{"name": "team", "type": "required-labels", "labels": ["team"]}], cron)
        self.assertEqual(missing.violations[0].message, 'missing required label "team" (spec.jobTemplate.spec.template.metadata.labels)')

    def test_run_as_non_root_is_inherited_from_the_pod_unless_a_container_overrides_it(self):
        rule = [{"name": "nonroot", "type": "run-as-non-root"}]
        inherited = pod([{"name": "a", "image": "x"}], pod_context={"runAsNonRoot": True})
        self.assertTrue(self.decide(rule, inherited).allowed)
        overridden = pod([{"name": "a", "image": "x", "securityContext": {"runAsNonRoot": False}}], pod_context={"runAsNonRoot": True})
        self.assertFalse(self.decide(rule, overridden).allowed)
        per_container = pod([{"name": "a", "image": "x", "securityContext": {"runAsNonRoot": True}}], pod_context={"runAsNonRoot": False})
        self.assertTrue(self.decide(rule, per_container).allowed)
        self.assertFalse(self.decide(rule, pod([{"name": "a", "image": "x"}])).allowed)

    def test_actions_turn_the_same_violation_into_a_denial_a_warning_or_an_audit_note(self):
        obj = pod([{"name": "a", "image": "nginx:1.30"}])
        for actions, allowed, deny, warn, audit in ((["Deny"], False, ["d"], [], []), (["Warn"], True, [], ["d"], []), (["Audit"], True, [], [], ["d"]), (["Warn", "Audit"], True, [], ["d"], ["d"])):
            decision = self.decide([{"name": "d", "type": "image-digest", "actions": actions}], obj)
            self.assertEqual((decision.allowed, decision.policies("Deny"), decision.policies("Warn"), decision.policies("Audit")), (allowed, deny, warn, audit), actions)
        pinned = pod([{"name": "a", "image": "nginx@" + DIGEST}])
        self.assertEqual(self.decide([{"name": "d", "type": "image-digest"}], pinned).violations, [])

    def test_match_limits_kinds_operations_and_namespaces(self):
        obj = pod([{"name": "a", "image": "x", "securityContext": {"privileged": True}}])
        only_shop = [{"name": "np", "type": "no-privileged", "match": {"namespaces": ["shop"]}}]
        self.assertFalse(self.decide(only_shop, obj, namespace="shop").allowed)
        self.assertTrue(self.decide(only_shop, obj, namespace="other").allowed)
        create_only = [{"name": "np", "type": "no-privileged", "match": {"operations": ["CREATE"]}}]
        self.assertTrue(self.decide(create_only, obj, operation="UPDATE").allowed)
        deployments_only = [{"name": "np", "type": "no-privileged", "match": {"kinds": ["Deployment"]}}]
        self.assertTrue(self.decide(deployments_only, obj).allowed)
        delete = main.AdmissionRequest("DELETE", None, old_object=obj, namespace="shop", name="p", kind="Pod")
        self.assertTrue(main.admit(policies({"name": "np", "type": "no-privileged", "match": {"operations": ["DELETE"]}}), delete).allowed)


class CelPolicyTests(unittest.TestCase):
    def test_evaluation_errors_follow_the_failure_policy(self):
        naive = fixture("cel-naive.json")
        good = main.request_from_object(manifest("good"))
        failed = main.admit(main.load_policies(naive), good)
        self.assertFalse(failed.allowed)
        self.assertEqual(failed.violations[0].message, "expression 'object.spec.containers.all(c, c.securityContext.privileged == false)' resulted in error: no such key: securityContext")
        self.assertTrue(failed.violations[0].error)
        ignored = copy.deepcopy(naive)
        ignored["policies"][0]["failurePolicy"] = "Ignore"
        self.assertTrue(main.admit(main.load_policies(ignored), good).allowed)
        warned = copy.deepcopy(naive)
        warned["policies"][0]["actions"] = ["Warn"]
        decision = main.admit(main.load_policies(warned), good)
        self.assertTrue(decision.allowed)
        self.assertEqual(decision.policies("Warn"), ["cc-admission-naive-privileged"])

    def test_message_expression_match_conditions_and_the_fallback_message(self):
        loaded = main.load_policies(fixture("cel-policies.json"))
        privileged = main.admit(loaded, main.request_from_object(manifest("privileged")))
        self.assertEqual([v.message for v in privileged.violations], ["privileged containers are not allowed: web"])
        exempt = main.admit(loaded, main.request_from_object(manifest("digest-exempt")))
        self.assertTrue(exempt.allowed)
        self.assertEqual(exempt.violations, [])
        bare = policies({"name": "b", "type": "cel", "validations": [{"expression": "object.metadata.name == 'x'", "messageExpression": "object.nope"}]})
        decision = main.admit(bare, main.request_from_object(pod([{"name": "a", "image": "x"}])))
        self.assertEqual(decision.violations[0].message, "failed expression: object.metadata.name == 'x'")

    def test_the_cel_set_matches_its_expected_table(self):
        loaded = main.load_policies(fixture("cel-policies.json"))
        for name, want in fixture("expected.json")["vap"].items():
            decision = main.admit(loaded, main.request_from_object(manifest(name), "cc-admission-policy"))
            self.assertEqual((decision.allowed, decision.policies("Deny"), decision.policies("Warn")), (want["allowed"], want["deny"], want["warn"]), name)


class MutationTests(unittest.TestCase):
    def test_json_pointer_escapes_tilde_and_slash_and_patches_round_trip(self):
        old = {"metadata": {"labels": {"a": "1"}}, "spec": {"list": [1, 2, 3], "gone": True}}
        new = {"metadata": {"labels": {"a": "1", "cloudcaptain.io/x~y": "2"}}, "spec": {"list": [1, 5, 3]}}
        ops = main.json_patch(old, new)
        self.assertIn({"op": "add", "path": "/metadata/labels/cloudcaptain.io~1x~0y", "value": "2"}, ops)
        self.assertIn({"op": "replace", "path": "/spec/list/1", "value": 5}, ops)
        self.assertIn({"op": "remove", "path": "/spec/gone"}, ops)
        self.assertEqual(main.apply_patch(old, ops), new)
        self.assertEqual(main.apply_patch([1, 2], [{"op": "add", "path": "/-", "value": 3}, {"op": "add", "path": "/0", "value": 0}]), [0, 1, 2, 3])
        for bad in ([{"op": "add", "path": "/nope/deeper", "value": 1}], [{"op": "replace", "path": "/missing", "value": 1}], [{"op": "move", "path": "/a"}], [{"op": "add", "path": "a", "value": 1}], [{"op": "test", "path": "/a", "value": 2}]):
            with self.assertRaises(main.PatchError):
                main.apply_patch({"a": 1}, bad)

    def test_default_requests_respect_existing_requests_limits_and_server_defaulting(self):
        rule = policies({"name": "defaults", "type": "default-requests", "requests": {"cpu": "100m", "memory": "128Mi"}})
        bare = pod([{"name": "a", "image": "x"}])
        decision = main.admit(rule, main.request_from_object(bare))
        self.assertEqual(decision.patch, [{"op": "add", "path": "/spec/containers/0/resources", "value": {"requests": {"cpu": "100m", "memory": "128Mi"}}}])
        served = pod([{"name": "a", "image": "x", "resources": {}}])
        self.assertEqual(main.admit(rule, main.request_from_object(served)).patch[0]["path"], "/spec/containers/0/resources/requests")
        partial = pod([{"name": "a", "image": "x", "resources": {"requests": {"cpu": "2"}, "limits": {"memory": "1Gi"}}}])
        patched = main.admit(rule, main.request_from_object(partial)).patched
        self.assertEqual(patched["spec"]["containers"][0]["resources"]["requests"], {"cpu": "2", "memory": "1Gi"})
        again = main.admit(rule, main.request_from_object(patched))
        self.assertEqual((again.patch, again.mutated_by), ([], []))
        debug = {"kind": "Pod", "metadata": {"name": "p"}, "spec": {"containers": [{"name": "a", "image": "x", "resources": {"requests": {"cpu": "1", "memory": "1Gi"}}}], "ephemeralContainers": [{"name": "dbg", "image": "x"}]}}
        self.assertEqual(main.admit(rule, main.request_from_object(debug)).patch, [])

    def test_validation_sees_the_object_after_mutation(self):
        loaded = policies(
            {"name": "needs-requests", "type": "cel", "validations": [{"expression": "object.spec.containers.all(c, has(c.resources) && has(c.resources.requests))"}]},
            {"name": "defaults", "type": "default-requests", "requests": {"cpu": "100m"}},
        )
        decision = main.admit(loaded, main.request_from_object(pod([{"name": "a", "image": "x"}])))
        self.assertTrue(decision.allowed)
        self.assertEqual(decision.mutated_by, ["defaults"])

    def test_default_labels_patch_uses_an_escaped_pointer(self):
        decision = main.admit(main.load_policies(fixture("labels-policy.json")), main.request_from_object(manifest("good")))
        self.assertEqual(decision.patch, [{"op": "add", "path": "/metadata/labels/cloudcaptain.io~1mutated-by", "value": "mutatingadmissionpolicy"}])


class ReviewTests(unittest.TestCase):
    def setUp(self):
        self.policies = main.load_policies(fixture("policies.json"))
        self.body = json.dumps(fixture("review-pod.json")).encode()

    def test_validate_answers_with_the_same_uid_a_403_and_warnings(self):
        status, doc, summary = main.handle_review(self.policies, self.body, "validate")
        self.assertEqual(status, 200)
        self.assertEqual((doc["apiVersion"], doc["kind"]), ("admission.k8s.io/v1", "AdmissionReview"))
        response = doc["response"]
        self.assertEqual(response["uid"], "705ab4f5-6393-11e8-b7cc-42010a800002")
        self.assertFalse(response["allowed"])
        self.assertEqual((response["status"]["code"], response["status"]["reason"]), (403, "Forbidden"))
        self.assertIn("[require-team-label]", response["status"]["message"])
        self.assertIn("[disallow-privileged]", response["status"]["message"])
        self.assertEqual(response["warnings"], ['[require-digest] image "registry.k8s.io/pause:3.10" in container "web" is not pinned by digest (@sha256:...)'])
        self.assertEqual(json.loads(response["auditAnnotations"]["require-digest"]), ['image "registry.k8s.io/pause:3.10" in container "web" is not pinned by digest (@sha256:...)'])
        self.assertNotIn("patch", response)
        self.assertEqual((summary["dryRun"], summary["allowed"], summary["namespace"]), (True, False, "shop"))

    def test_mutate_answers_with_a_base64_json_patch_against_the_defaulted_object(self):
        status, doc, _ = main.handle_review(self.policies, self.body, "mutate")
        response = doc["response"]
        self.assertTrue(response["allowed"])
        self.assertEqual(response["patchType"], "JSONPatch")
        ops = json.loads(base64.b64decode(response["patch"]))
        self.assertEqual(ops, [{"op": "add", "path": "/spec/containers/0/resources/requests/cpu", "value": "100m"}])
        patched = main.apply_patch(fixture("review-pod.json")["request"]["object"], ops)
        self.assertEqual(patched["spec"]["containers"][0]["resources"]["requests"], {"memory": "64Mi", "cpu": "100m"})
        self.assertEqual(response["auditAnnotations"], {"mutated-by": "default-requests"})
        _, again, _ = main.handle_review(self.policies, json.dumps({"apiVersion": "admission.k8s.io/v1", "kind": "AdmissionReview", "request": dict(fixture("review-pod.json")["request"], object=patched)}).encode(), "mutate")
        self.assertEqual(again["response"], {"uid": "705ab4f5-6393-11e8-b7cc-42010a800002", "allowed": True})

    def test_malformed_reviews_are_rejected_with_400(self):
        v1beta1 = dict(fixture("review-pod.json"), apiVersion="admission.k8s.io/v1beta1")
        no_uid = fixture("review-pod.json")
        del no_uid["request"]["uid"]
        for body, fragment in ((b"{", "not JSON"), (json.dumps(v1beta1).encode(), "admissionReviewVersions"), (json.dumps(no_uid).encode(), "uid"), (b'{"kind": "Pod"}', "AdmissionReview")):
            status, doc, _ = main.handle_review(self.policies, body, "validate")
            self.assertEqual(status, 400)
            self.assertIn(fragment, doc["error"])


@unittest.skipUnless(shutil.which("openssl"), "openssl is not installed")
class TlsServerTests(unittest.TestCase):
    def test_https_round_trip_with_a_certificate_from_openssl(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = main.make_certs(tmp, ["localhost"], ["127.0.0.1"])
            lines = []
            server = main.build_server(main.load_policies(fixture("policies.json")), "127.0.0.1", 0, paths["cert"], paths["key"], log=lines.append)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                port = server.server_address[1]
                context = ssl.create_default_context(cafile=paths["ca_cert"])
                with urllib.request.urlopen(f"https://127.0.0.1:{port}/healthz", context=context, timeout=10) as reply:
                    self.assertEqual(json.load(reply)["status"], "ok")
                request = urllib.request.Request(f"https://127.0.0.1:{port}/validate?timeout=5s", data=json.dumps(fixture("review-pod.json")).encode(), headers={"Content-Type": "application/json"})
                with urllib.request.urlopen(request, context=context, timeout=10) as reply:
                    review = json.load(reply)
                self.assertFalse(review["response"]["allowed"])
                with self.assertRaises(urllib.error.URLError):
                    urllib.request.urlopen(f"https://127.0.0.1:{port}/healthz", context=ssl.create_default_context(), timeout=10)
                self.assertEqual(json.loads(lines[-1])["phase"], "validate")
            finally:
                server.shutdown()
                server.server_close()


class GeneratorTests(unittest.TestCase):
    def test_certs_runs_five_openssl_steps_with_the_san_in_the_config(self):
        calls = []

        def fake(cmd):
            calls.append(cmd)
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        with tempfile.TemporaryDirectory() as tmp:
            paths = main.make_certs(tmp, ["miniadmit.hook.svc"], ["10.0.0.1"], runner=fake)
            with open(paths["config"], encoding="utf-8") as handle:
                config = handle.read()
        self.assertEqual([c[1] for c in calls], ["genpkey", "req", "genpkey", "req", "x509"])
        self.assertIn("-extensions", calls[4])
        self.assertIn("DNS.1 = miniadmit.hook.svc", config)
        self.assertIn("IP.1 = 10.0.0.1", config)
        self.assertIn("extendedKeyUsage = serverAuth", config)
        with self.assertRaises(main.InputError):
            main.make_certs("unused", [], [], runner=fake)
        with self.assertRaises(main.InputError) as caught:
            with tempfile.TemporaryDirectory() as tmp:
                main.make_certs(tmp, ["x.svc"], runner=lambda cmd: SimpleNamespace(returncode=1, stdout="", stderr="bad key"))
        self.assertIn("bad key", str(caught.exception))

    def test_webhook_configs_fail_open_scoped_and_never_intercept_themselves(self):
        ca = "-----BEGIN CERTIFICATE-----\nMII\n-----END CERTIFICATE-----\n"
        doc = main.webhook_configs("cc", ca, ["shop"], service="miniadmit", service_namespace="hook", labels={"a": "b"})
        mutating, validating = doc["items"]
        self.assertEqual((mutating["kind"], validating["kind"]), ("MutatingWebhookConfiguration", "ValidatingWebhookConfiguration"))
        hook = validating["webhooks"][0]
        self.assertEqual(hook["name"], "validate.miniadmit.cloudcaptain.io")
        self.assertEqual((hook["failurePolicy"], hook["sideEffects"], hook["timeoutSeconds"], hook["admissionReviewVersions"]), ("Ignore", "None", 5, ["v1"]))
        self.assertEqual(hook["namespaceSelector"], {"matchExpressions": [{"key": "kubernetes.io/metadata.name", "operator": "In", "values": ["shop"]}]})
        self.assertEqual(hook["clientConfig"]["service"], {"name": "miniadmit", "namespace": "hook", "path": "/validate", "port": 443})
        self.assertEqual(base64.b64decode(hook["clientConfig"]["caBundle"]).decode(), ca)
        self.assertEqual(mutating["webhooks"][0]["reinvocationPolicy"], "Never")
        self.assertIn({"apiGroups": ["apps"], "apiVersions": ["v1"], "operations": ["CREATE", "UPDATE"], "resources": ["daemonsets", "deployments", "replicasets", "statefulsets"], "scope": "Namespaced"}, hook["rules"])
        external = main.webhook_configs("cc", ca, ["shop"], url="https://10.0.0.1:8443")
        self.assertEqual(external["items"][1]["webhooks"][0]["clientConfig"]["url"], "https://10.0.0.1:8443/validate")
        for kwargs, fragment in (
            ({"namespaces": [], "service": "s", "service_namespace": "hook"}, "at least one --namespace"),
            ({"namespaces": ["kube-system"], "service": "s", "service_namespace": "hook"}, "kube-system"),
            ({"namespaces": ["hook"], "service": "s", "service_namespace": "hook"}, "own namespace"),
            ({"namespaces": ["shop"], "url": "http://x"}, "https"),
            ({"namespaces": ["shop"], "service": "s", "service_namespace": "hook", "timeout": 31}, "between 1 and 30"),
        ):
            with self.assertRaises(main.InputError) as caught:
                main.webhook_configs("cc", ca, **kwargs)
            self.assertIn(fragment, str(caught.exception))

    def test_vap_objects_translate_cel_policies_into_policy_and_binding_pairs(self):
        doc, skipped = main.vap_objects(main.load_policies(fixture("cel-policies.json")), ["cc-admission-policy"], {"x": "y"})
        kinds = [(item["kind"], item["metadata"]["name"]) for item in doc["items"]]
        self.assertEqual(kinds[:2], [("ValidatingAdmissionPolicy", "cc-admission-team-label"), ("ValidatingAdmissionPolicyBinding", "cc-admission-team-label")])
        self.assertEqual(skipped, [])
        by_name = {(i["kind"], i["metadata"]["name"]): i for i in doc["items"]}
        digest = by_name[("ValidatingAdmissionPolicy", "cc-admission-digest")]["spec"]
        self.assertEqual(digest["matchConditions"][0]["name"], "not-exempt")
        self.assertEqual(digest["matchConstraints"]["resourceRules"][0]["resources"], ["pods"])
        privileged = by_name[("ValidatingAdmissionPolicy", "cc-admission-no-privileged")]["spec"]
        self.assertEqual(privileged["variables"][0]["name"], "privileged")
        self.assertIn("messageExpression", privileged["validations"][0])
        binding = by_name[("ValidatingAdmissionPolicyBinding", "cc-admission-memory-limit")]["spec"]
        self.assertEqual(binding["validationActions"], ["Warn"])
        self.assertEqual(binding["matchResources"]["namespaceSelector"]["matchExpressions"][0]["values"], ["cc-admission-policy"])
        _, skipped = main.vap_objects(main.load_policies(fixture("policies.json")), ["shop"])
        self.assertIn("disallow-privileged", skipped)
        with self.assertRaises(main.InputError):
            main.vap_objects(main.load_policies(fixture("labels-policy.json")), ["shop"])


class CliTests(unittest.TestCase):
    def test_test_command_reproduces_the_expected_webhook_table_and_exits_1(self):
        files = sorted(glob.glob(os.path.join(MANIFESTS, "*.json")))
        code, out, err = run(["test", "--policies", os.path.join(FIXTURES, "policies.json"), "--namespace", "cc-admission-policy", "--json", *files])
        self.assertEqual((code, err), (1, ""))
        results = {r["name"]: r for r in json.loads(out)["results"]}
        for name, want in fixture("expected.json")["webhook"].items():
            got = results[name]
            self.assertEqual((got["allowed"], got["deny"], got["warn"]), (want["allowed"], want["deny"], want["warn"]), name)
            if "requests" in want:
                path = main.pod_spec_path(got["patched"])
                spec = main.get_path(got["patched"], path)
                self.assertEqual(spec["containers"][0]["resources"]["requests"], want["requests"], name)
            self.assertEqual(got["namespace"], "cc-admission-policy")

    def test_test_command_text_output_and_exit_codes(self):
        code, out, _ = run(["test", "--policies", os.path.join(FIXTURES, "policies.json"), os.path.join(MANIFESTS, "good.json")])
        self.assertEqual(code, 0)
        self.assertIn("ALLOWED  Pod default/good", out)
        self.assertIn("patch add /spec/containers/0/resources/requests", out)
        code, _, err = run(["test", "--policies", os.path.join(FIXTURES, "policies.json"), "-"], stdin="not json")
        self.assertEqual(code, 2)
        self.assertIn("kubectl create --dry-run=client -o json", err)
        code, _, err = run(["test", "--policies", os.path.join(FIXTURES, "missing.json"), os.path.join(MANIFESTS, "good.json")])
        self.assertEqual(code, 2)
        self.assertIn("cannot read", err)

    def test_cel_command_prints_values_errors_and_compile_errors(self):
        good = os.path.join(MANIFESTS, "good.json")
        self.assertEqual(run(["cel", "object.spec.containers.map(c, c.name)", "--object", good]), (0, '["web"]\n', ""))
        code, out, _ = run(["cel", "object.spec.containers[0].securityContext.privileged", "--object", good, "--json"])
        self.assertEqual((code, json.loads(out)), (1, {"error": "no such key: securityContext"}))
        code, _, err = run(["cel", "object.spec.containers.all(c, x)"])
        self.assertEqual(code, 2)
        self.assertIn("undeclared reference to 'x'", err)

    def test_webhook_config_and_vap_commands_print_lists(self):
        with tempfile.TemporaryDirectory() as tmp:
            ca = os.path.join(tmp, "ca.crt")
            with open(ca, "w", encoding="utf-8") as handle:
                handle.write("-----BEGIN CERTIFICATE-----\nMII\n-----END CERTIFICATE-----\n")
            code, out, _ = run(["webhook-config", "--name", "cc", "--ca-bundle", ca, "--namespace", "shop", "--service", "miniadmit", "--service-namespace", "hook", "--label", "cloudcaptain.io/lesson=x"])
            self.assertEqual(code, 0)
            doc = json.loads(out)
            self.assertEqual(doc["items"][0]["metadata"]["labels"], {"cloudcaptain.io/lesson": "x"})
            code, _, err = run(["webhook-config", "--name", "cc", "--ca-bundle", ca, "--namespace", "kube-system", "--service", "s", "--service-namespace", "hook"])
            self.assertEqual(code, 2)
            self.assertIn("kube-system", err)
        code, out, err = run(["vap", "--policies", os.path.join(FIXTURES, "policies.json"), "--namespace", "shop"])
        self.assertEqual(code, 0)
        self.assertEqual([i["metadata"]["name"] for i in json.loads(out)["items"]], ["memory-limit", "memory-limit"])
        self.assertIn("skipped default-requests", err)


if __name__ == "__main__":
    unittest.main()
