import io
import json
import os
import sys
import tempfile
import unittest
import urllib.error
from contextlib import redirect_stderr, redirect_stdout

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import main  # noqa: E402

NS = "web"


def make_world(start=1_000_000.0):
    clock = main.FakeClock(start)
    api = main.FakeApi(clock)
    records = main.MemoryRecords()
    operator = main.Operator(api, records, NS, clock)
    operator.start()
    return clock, api, records, operator


def create_site(api, name="hello", content="<h1>hi</h1>", replicas=2, tier="dev"):
    return api.create(main.SITE, NS, main.site_manifest(name, content, replicas, tier))


def ready(api, name="hello"):
    site = api.get(main.SITE, NS, name)
    return next(c for c in site["status"]["conditions"] if c["type"] == "Ready")


class FakeResponse(io.BytesIO):
    pass


class FakeOpener:
    def __init__(self, bodies):
        self.bodies = list(bodies)
        self.requests = []

    def __call__(self, request, timeout=None):
        self.requests.append((request, timeout))
        body = self.bodies.pop(0)
        if isinstance(body, Exception):
            raise body
        return FakeResponse(body if isinstance(body, bytes) else json.dumps(body).encode())


class CrdAndAdmissionTests(unittest.TestCase):
    def test_crd_declares_status_subresource_cel_rule_and_columns(self):
        version = main.CRD["spec"]["versions"][0]
        self.assertEqual(main.CRD["metadata"]["name"], "staticsites.learn.cloudcaptain.io")
        self.assertEqual(version["subresources"], {"status": {}})
        self.assertTrue(version["served"] and version["storage"])
        spec = version["schema"]["openAPIV3Schema"]["properties"]["spec"]
        self.assertEqual(spec["x-kubernetes-validations"][0]["message"], "prod sites need at least 2 replicas")
        self.assertEqual([c["name"] for c in version["additionalPrinterColumns"]], ["Replicas", "Ready", "Reason", "Age"])
        self.assertEqual(json.loads(json.dumps(main.CRD)), main.CRD)

    def test_cel_subset_evaluates_kubernetes_style_rules(self):
        def run(expression, value):
            return main.cel_eval(main.cel_compile(expression), {"self": value})

        rule = "self.tier != 'prod' || self.replicas >= 2"
        self.assertFalse(run(rule, {"tier": "prod", "replicas": 1}))
        self.assertTrue(run(rule, {"tier": "prod", "replicas": 2}))
        self.assertTrue(run(rule, {"tier": "dev", "replicas": 1}))
        self.assertTrue(run("!has(self.x) && size(self.name) == 5", {"name": "hello"}))
        self.assertTrue(run("self.name.startsWith('he') && self.tier in ['dev', 'prod']", {"name": "hello", "tier": "dev"}))
        self.assertEqual(run("self.replicas > 3 ? 'big' : 'small'", {"replicas": 4}), "big")
        self.assertEqual(run("(self.a + 2) * 3 - 7 / 2", {"a": 1}), 6)
        self.assertTrue(run("self.items.size() == 2 && self.items[1] == 'b'", {"items": ["a", "b"]}))
        with self.assertRaisesRegex(main.CelError, "no such key: tier"):
            run("self.tier == 'prod'", {})
        with self.assertRaisesRegex(main.CelError, r"no matching overload for '==' applied to \(int, string\)"):
            run("self.replicas == 'two'", {"replicas": 2})
        with self.assertRaisesRegex(main.CelError, "expected"):
            main.cel_compile("(self.a")

    def test_admission_defaults_prunes_and_rejects_like_the_api_server(self):
        obj, errors, warnings = main.admit({"apiVersion": "learn.cloudcaptain.io/v1alpha1", "kind": "StaticSite", "metadata": {"name": "a"}, "spec": {"content": "x", "colour": "red"}})
        self.assertEqual(errors, [])
        self.assertEqual(obj["spec"], {"content": "x", "replicas": 1, "tier": "dev"})
        self.assertEqual(warnings, ['unknown field "spec.colour"'])
        self.assertEqual(obj["metadata"], {"name": "a"})
        _, errors, _ = main.admit({"spec": {"content": "x", "replicas": 50, "tier": "staging"}})
        self.assertIn("spec.replicas: Invalid value: 50: spec.replicas in body should be less than or equal to 10", errors)
        self.assertIn('spec.tier: Unsupported value: "staging": supported values: "dev", "prod"', errors)
        _, errors, _ = main.admit({"spec": {"content": "x", "replicas": 1, "tier": "prod"}})
        self.assertEqual(errors, ['spec: Invalid value: "object": prod sites need at least 2 replicas'])
        _, errors, _ = main.admit({"spec": {"replicas": "two"}})
        self.assertIn("spec.content: Required value", errors)
        self.assertTrue(any("must be of type integer" in e for e in errors))
        _, errors, _ = main.admit({"metadata": {"name": "a"}})
        self.assertEqual(errors, ["spec: Required value"])

    def test_fake_api_rejects_invalid_sites_with_a_422(self):
        _, api, _, _ = make_world()
        with self.assertRaises(main.ApiError) as caught:
            create_site(api, "shop", replicas=1, tier="prod")
        self.assertEqual(caught.exception.code, 422)
        self.assertEqual(caught.exception.message, 'StaticSite.learn.cloudcaptain.io "shop" is invalid: spec: Invalid value: "object": prod sites need at least 2 replicas')


class ApiClientTests(unittest.TestCase):
    def test_watch_lines_parse_bookmarks_and_errors(self):
        lines = [b'{"type":"ADDED","object":{"metadata":{"name":"a","resourceVersion":"5"}}}\n', b"\n", '{"type":"BOOKMARK","object":{"metadata":{"resourceVersion":"9"}}}']
        events = list(main.parse_watch_lines(lines))
        self.assertEqual([e["type"] for e in events], ["ADDED", "BOOKMARK"])
        gone = ['{"type":"ERROR","object":{"kind":"Status","code":410,"reason":"Expired","message":"too old resource version: 1 (7)"}}']
        with self.assertRaises(main.Gone) as caught:
            list(main.parse_watch_lines(gone))
        self.assertIn("too old resource version", caught.exception.message)
        with self.assertRaises(main.ApiError) as caught:
            list(main.parse_watch_lines(['{"type":"ERROR","object":{"code":500,"reason":"InternalError","message":"boom"}}']))
        self.assertEqual(caught.exception.code, 500)

    def test_http_api_builds_the_requests_kubectl_proxy_expects(self):
        opener = FakeOpener([
            {"items": [{"metadata": {"name": "a"}}], "metadata": {"resourceVersion": "41"}},
            b'{"type":"MODIFIED","object":{"metadata":{"name":"a","resourceVersion":"42"}}}\n',
            {"metadata": {"name": "a"}},
            {"metadata": {"name": "a"}},
            {"metadata": {"name": "c"}},
        ])
        api = main.HttpApi("http://127.0.0.1:8001/", opener)
        items, revision = api.list(main.DEPLOYMENT, NS, "app.kubernetes.io/managed-by=minioperator")
        self.assertEqual((len(items), revision), (1, "41"))
        events = list(api.watch(main.SITE, NS, "41", timeout=30))
        self.assertEqual(events[0]["type"], "MODIFIED")
        api.update_status(main.SITE, NS, {"metadata": {"name": "a", "resourceVersion": "42"}})
        api.update(main.SITE, NS, {"metadata": {"name": "a"}})
        api.create(main.CONFIGMAP, NS, {"metadata": {"name": "c"}})
        (listing, _), (watch, watch_timeout), (status, _), (put, _), (post, _) = opener.requests
        self.assertEqual(listing.full_url, "http://127.0.0.1:8001/apis/apps/v1/namespaces/web/deployments?labelSelector=app.kubernetes.io%2Fmanaged-by%3Dminioperator")
        self.assertTrue(watch.full_url.startswith("http://127.0.0.1:8001/apis/learn.cloudcaptain.io/v1alpha1/namespaces/web/staticsites?watch=1&resourceVersion=41&allowWatchBookmarks=true&timeoutSeconds=30"))
        self.assertGreater(watch_timeout, 30)
        self.assertEqual((status.get_method(), status.full_url), ("PUT", "http://127.0.0.1:8001/apis/learn.cloudcaptain.io/v1alpha1/namespaces/web/staticsites/a/status"))
        self.assertEqual((put.get_method(), put.full_url.endswith("/staticsites/a")), ("PUT", True))
        self.assertEqual((post.get_method(), post.full_url), ("POST", "http://127.0.0.1:8001/api/v1/namespaces/web/configmaps"))
        self.assertEqual(json.loads(post.data), {"metadata": {"name": "c"}})
        self.assertEqual(post.get_header("Content-type"), "application/json")

    def test_http_errors_become_api_errors(self):
        status = {"kind": "Status", "code": 409, "reason": "Conflict", "message": "the object has been modified"}
        conflict = urllib.error.HTTPError("http://x", 409, "Conflict", {}, io.BytesIO(json.dumps(status).encode()))
        gone = urllib.error.HTTPError("http://x", 410, "Gone", {}, io.BytesIO(b'{"kind":"Status","code":410,"message":"too old"}'))
        plain = urllib.error.HTTPError("http://x", 503, "Service Unavailable", {}, io.BytesIO(b"upstream down"))
        api = main.HttpApi("http://127.0.0.1:1", FakeOpener([conflict, gone, plain, urllib.error.URLError("Connection refused")]))
        with self.assertRaises(main.ApiError) as caught:
            api.update(main.SITE, NS, {"metadata": {"name": "a"}})
        self.assertEqual((caught.exception.code, caught.exception.reason), (409, "Conflict"))
        with self.assertRaises(main.Gone):
            list(api.watch(main.SITE, NS, "1"))
        with self.assertRaises(main.ApiError) as caught:
            api.get(main.SITE, NS, "a")
        self.assertEqual((caught.exception.code, caught.exception.message), (503, "upstream down"))
        with self.assertRaises(main.ApiError) as caught:
            api.get(main.SITE, NS, "a")
        self.assertEqual(caught.exception.code, 0)
        self.assertIn("kubectl proxy", caught.exception.message)


class FakeApiTests(unittest.TestCase):
    def test_resource_versions_generation_and_optimistic_concurrency(self):
        _, api, _, _ = make_world()
        site = create_site(api)
        self.assertEqual((site["metadata"]["generation"], site["metadata"]["resourceVersion"]), (1, "1"))
        same = api.update(main.SITE, NS, site)
        self.assertEqual(same["metadata"]["resourceVersion"], "1")
        site["metadata"]["labels"] = {"team": "web"}
        relabelled = api.update(main.SITE, NS, site)
        self.assertEqual((relabelled["metadata"]["generation"], relabelled["metadata"]["resourceVersion"]), (1, "2"))
        stale = dict(site)
        with self.assertRaises(main.ApiError) as caught:
            api.update(main.SITE, NS, stale)
        self.assertEqual(caught.exception.code, 409)
        self.assertIn("the object has been modified", caught.exception.message)
        relabelled["spec"]["replicas"] = 3
        self.assertEqual(api.update(main.SITE, NS, relabelled)["metadata"]["generation"], 2)

    def test_status_subresource_splits_spec_from_status(self):
        _, api, _, _ = make_world()
        site = create_site(api)
        site["status"] = {"readyReplicas": 9}
        site["spec"]["replicas"] = 4
        written = api.update_status(main.SITE, NS, site)
        self.assertEqual((written["status"], written["spec"]["replicas"], written["metadata"]["generation"]), ({"readyReplicas": 9}, 2, 1))
        written["status"] = {"readyReplicas": 0}
        written["spec"]["replicas"] = 4
        updated = api.update(main.SITE, NS, written)
        self.assertEqual((updated["status"], updated["spec"]["replicas"], updated["metadata"]["generation"]), ({"readyReplicas": 9}, 4, 2))

    def test_delete_waits_for_finalizers_then_garbage_collects_children(self):
        _, api, _, operator = make_world()
        create_site(api)
        operator.pump()
        api.delete(main.SITE, NS, "hello")
        held = api.objects[(main.SITE.plural, NS, "hello")]
        self.assertIn("deletionTimestamp", held["metadata"])
        self.assertEqual(held["metadata"]["generation"], 2)
        held = api.get(main.SITE, NS, "hello")
        held["metadata"]["finalizers"] = held["metadata"]["finalizers"] + ["other/finalizer"]
        with self.assertRaisesRegex(main.ApiError, "no new finalizers"):
            api.update(main.SITE, NS, held)
        held = api.get(main.SITE, NS, "hello")
        held["metadata"]["finalizers"] = []
        api.update(main.SITE, NS, held)
        self.assertEqual(api.objects, {})
        self.assertEqual(sorted(api.collected), ["ConfigMap web/hello-content", "Deployment web/hello", "Service web/hello"])

    def test_compacted_history_returns_410_and_the_informer_relists(self):
        _, api, _, _ = make_world()
        keys = []
        notes = []
        informer = main.Informer(api, main.SITE, NS, main.site_keys, keys.append, lambda k, a, d: notes.append(a))
        create_site(api, "a")
        create_site(api, "b")
        informer.relist()
        self.assertEqual(sorted(keys), ["web/a", "web/b"])
        api.delete(main.SITE, NS, "b")
        create_site(api, "c")
        api.compact()
        keys.clear()
        with self.assertRaises(main.Gone):
            list(api.watch(main.SITE, NS, informer.resource_version))
        informer.poll()
        self.assertEqual(notes[-2:], ["watch expired", "list"])
        self.assertEqual(sorted(keys), ["web/a", "web/b", "web/c"])
        self.assertEqual(sorted(informer.cache), ["web/a", "web/c"])
        self.assertEqual(informer.resource_version, str(api.revision))


class WorkQueueTests(unittest.TestCase):
    def test_dedupes_keys_and_backs_off_exponentially(self):
        clock = main.FakeClock(0.0)
        queue = main.WorkQueue(clock, base=0.5, cap=3.0)
        queue.add("web/a")
        queue.add("web/a")
        queue.add("web/b")
        self.assertEqual([queue.pop(), queue.pop(), queue.pop()], ["web/a", "web/b", None])
        self.assertEqual([queue.retry("web/a") for _ in range(4)], [0.5, 1.0, 2.0, 3.0])
        self.assertIsNone(queue.pop())
        clock.advance(0.5)
        self.assertEqual(queue.pop(), "web/a")
        queue.forget("web/a")
        self.assertEqual(queue.retry("web/a"), 0.5)
        queue.add("web/a")
        self.assertEqual(queue.pop(), "web/a")
        self.assertIsNone(queue.wait(0.05))


class ReconcileTests(unittest.TestCase):
    def test_creates_owned_objects_with_controller_references(self):
        _, api, records, operator = make_world()
        site = create_site(api)
        operator.pump()
        site = api.get(main.SITE, NS, "hello")
        self.assertEqual(site["metadata"]["finalizers"], [main.FINALIZER])
        configmap = api.get(main.CONFIGMAP, NS, "hello-content")
        deployment = api.get(main.DEPLOYMENT, NS, "hello")
        service = api.get(main.SERVICE, NS, "hello")
        for child in (configmap, deployment, service):
            ref = child["metadata"]["ownerReferences"][0]
            self.assertEqual((ref["kind"], ref["uid"], ref["controller"], ref["blockOwnerDeletion"]), ("StaticSite", site["metadata"]["uid"], True, True))
            self.assertEqual(child["metadata"]["labels"]["app.kubernetes.io/managed-by"], "minioperator")
        self.assertEqual(configmap["data"], {"index.html": "<h1>hi</h1>"})
        pod = deployment["spec"]["template"]["spec"]
        self.assertEqual((deployment["spec"]["replicas"], pod["containers"][0]["image"]), (2, "nginx:1.29-alpine"))
        self.assertEqual(pod["volumes"][0]["configMap"]["name"], "hello-content")
        self.assertEqual(service["spec"]["selector"], deployment["spec"]["selector"]["matchLabels"])
        self.assertTrue(records.exists(NS, "hello"))
        self.assertEqual(site["status"]["observedGeneration"], 1)
        self.assertEqual(ready(api)["status"], "False")

    def test_ready_follows_the_rollout_and_observed_generation_follows_spec(self):
        clock, api, _, operator = make_world()
        create_site(api)
        operator.pump()
        api.settle()
        operator.pump()
        first = ready(api)
        self.assertEqual((first["status"], first["reason"], first["observedGeneration"]), ("True", "Available", 1))
        clock.advance(60)
        edit = main.edit_spec(api, NS, "hello", replicas=3)
        self.assertEqual(edit["metadata"]["generation"], 2)
        operator.pump()
        rolling = ready(api)
        self.assertEqual((rolling["status"], rolling["reason"], rolling["observedGeneration"]), ("False", "RollingOut", 2))
        self.assertNotEqual(rolling["lastTransitionTime"], first["lastTransitionTime"])
        clock.advance(60)
        api.settle()
        operator.pump()
        site = api.get(main.SITE, NS, "hello")
        self.assertEqual((site["status"]["observedGeneration"], site["status"]["readyReplicas"]), (2, 3))
        self.assertEqual(api.get(main.DEPLOYMENT, NS, "hello")["spec"]["replicas"], 3)
        self.assertEqual(ready(api)["lastTransitionTime"], main.iso(clock()))

    def test_steady_state_writes_nothing(self):
        _, api, _, _ = make_world()
        create_site(api)
        recorder = main.RecordingApi(api)
        clock = main.FakeClock(1_000_000.0)
        operator = main.Operator(recorder, main.MemoryRecords(), NS, clock)
        operator.start()
        operator.pump()
        api.settle()
        operator.pump()
        revision = api.revision
        recorder.calls.clear()
        operator.resync()
        self.assertEqual(operator.pump(), 1)
        self.assertEqual(api.revision, revision)
        self.assertEqual({verb for verb, _, _ in recorder.calls}, {"get", "watch"})

    def test_content_change_updates_configmap_and_rolls_pods(self):
        _, api, _, operator = make_world()
        create_site(api)
        operator.pump()
        before = api.get(main.DEPLOYMENT, NS, "hello")["spec"]["template"]["metadata"]["annotations"][main.CONTENT_ANNOTATION]
        main.edit_spec(api, NS, "hello", content="<h1>v2</h1>")
        operator.pump()
        after = api.get(main.DEPLOYMENT, NS, "hello")["spec"]["template"]["metadata"]["annotations"][main.CONTENT_ANNOTATION]
        self.assertNotEqual(before, after)
        self.assertEqual(api.get(main.CONFIGMAP, NS, "hello-content")["data"]["index.html"], "<h1>v2</h1>")

    def test_drift_on_an_owned_deployment_is_reverted(self):
        _, api, _, operator = make_world()
        create_site(api)
        operator.pump()
        deployment = api.get(main.DEPLOYMENT, NS, "hello")
        deployment["spec"]["replicas"] = 5
        deployment["spec"]["template"]["spec"]["containers"][0]["image"] = "nginx:latest"
        api.update(main.DEPLOYMENT, NS, deployment)
        operator.pump()
        fixed = api.get(main.DEPLOYMENT, NS, "hello")
        self.assertEqual((fixed["spec"]["replicas"], fixed["spec"]["template"]["spec"]["containers"][0]["image"]), (2, "nginx:1.29-alpine"))

    def test_conflicts_are_requeued_with_backoff(self):
        clock, api, _, operator = make_world()
        create_site(api)
        api.inject_conflict("update", main.SITE)
        operator.pump()
        conflict = [e for e in operator.events if e["action"] == "conflict"]
        self.assertEqual(len(conflict), 1)
        self.assertIn("requeue in 0.5s", conflict[0]["detail"])
        self.assertNotIn("finalizers", api.get(main.SITE, NS, "hello")["metadata"])
        self.assertEqual(operator.pump(), 0)
        clock.advance(0.5)
        operator.pump()
        self.assertEqual(api.get(main.SITE, NS, "hello")["metadata"]["finalizers"], [main.FINALIZER])

    def test_finalizer_removes_the_external_record_before_the_object_goes(self):
        _, api, records, operator = make_world()
        create_site(api)
        operator.pump()
        api.delete(main.SITE, NS, "hello")
        operator.pump()
        self.assertFalse(records.exists(NS, "hello"))
        self.assertEqual(api.objects, {})
        actions = [e["action"] for e in operator.events]
        self.assertLess(actions.index("external record deleted"), actions.index("finalizer removed"))

    def test_finalizer_holds_the_object_while_cleanup_fails(self):
        clock, api, records, operator = make_world()
        create_site(api)
        operator.pump()
        records.failures = 1
        api.delete(main.SITE, NS, "hello")
        operator.pump()
        held = api.get(main.SITE, NS, "hello")
        self.assertIn("deletionTimestamp", held["metadata"])
        self.assertEqual(held["metadata"]["finalizers"], [main.FINALIZER])
        self.assertTrue(records.exists(NS, "hello"))
        clock.advance(1)
        operator.pump()
        self.assertFalse(records.exists(NS, "hello"))
        self.assertEqual(api.objects, {})

    def test_objects_it_does_not_control_are_not_adopted(self):
        _, api, records, operator = make_world()
        api.create(main.CONFIGMAP, NS, {"metadata": {"name": "hello-content"}, "data": {"index.html": "someone else's"}})
        create_site(api)
        operator.pump()
        condition = ready(api)
        self.assertEqual((condition["status"], condition["reason"]), ("False", "OwnershipConflict"))
        self.assertIn("ConfigMap hello-content exists and is not controlled", condition["message"])
        self.assertEqual(api.get(main.CONFIGMAP, NS, "hello-content")["data"]["index.html"], "someone else's")
        self.assertFalse(records.exists(NS, "hello"))

    def test_directory_records_publish_idempotently_and_remove(self):
        with tempfile.TemporaryDirectory() as tmp:
            records = main.DirectoryRecords(os.path.join(tmp, "records"))
            record = {"site": "web/a", "url": "http://a.web.svc.cluster.local"}
            self.assertTrue(records.publish(NS, "a", record))
            self.assertFalse(records.publish(NS, "a", record))
            self.assertEqual(json.loads(records.path(NS, "a").read_text()), record)
            self.assertTrue(records.remove(NS, "a"))
            self.assertFalse(records.remove(NS, "a"))


class RbacAndCliTests(unittest.TestCase):
    def test_role_covers_every_call_and_grants_no_delete(self):
        result = main.run_demo()
        self.assertTrue(result["calls"])
        self.assertTrue(all(call["allowed"] for call in result["calls"]), result["calls"])
        self.assertIn({"verb": "update", "group": "learn.cloudcaptain.io", "resource": "staticsites/finalizers", "allowed": True}, result["calls"])
        self.assertFalse(any("delete" in rule["verbs"] for rule in main.RBAC_RULES))
        self.assertFalse(main.rbac_allows(main.RBAC_RULES, "list", "", "secrets"))
        kinds = [item["kind"] for item in main.rbac_manifest("cc-operator")["items"]]
        self.assertEqual(kinds, ["ServiceAccount", "Role", "RoleBinding"])

    def test_demo_event_log_covers_the_whole_lifecycle(self):
        result = main.run_demo()
        actions = {e["action"] for e in result["events"]}
        for wanted in ("finalizer added", "created", "status", "conflict", "watch expired", "rejected", "error", "external record deleted", "finalizer removed", "garbage collected"):
            self.assertIn(wanted, actions)
        generations = [e["detail"] for e in result["events"] if e["action"] == "status"]
        self.assertTrue(any("observedGeneration 2" in g for g in generations))
        self.assertEqual((result["remaining"], result["records"]), ([], []))

    def test_cli_commands_and_exit_codes(self):
        def run(argv):
            out, err = io.StringIO(), io.StringIO()
            with redirect_stdout(out), redirect_stderr(err):
                code = main.main(argv)
            return code, out.getvalue(), err.getvalue()

        code, out, _ = run(["crd"])
        self.assertEqual((code, json.loads(out)["kind"]), (0, "CustomResourceDefinition"))
        code, out, _ = run(["cel", "self.tier != 'prod' || self.replicas >= 2", "--self", '{"tier":"prod","replicas":1}'])
        self.assertEqual((code, out.strip()), (0, "false"))
        self.assertEqual(run(["cel", "self.nope", "--self", "{}"])[0], 1)
        with tempfile.TemporaryDirectory() as tmp:
            bad = os.path.join(tmp, "bad.json")
            with open(bad, "w") as handle:
                json.dump(main.site_manifest("bad", "<p>x</p>", 1, "prod"), handle)
            code, out, _ = run(["validate", bad])
            self.assertEqual(code, 1)
            self.assertIn("prod sites need at least 2 replicas", out)
            good = os.path.join(tmp, "good.json")
            with open(good, "w") as handle:
                json.dump({"metadata": {"name": "ok"}, "spec": {"content": "x", "extra": 1}}, handle)
            code, out, err = run(["validate", good, "--json"])
            self.assertEqual((code, json.loads(out)["object"]["spec"]["replicas"]), (0, 1))
            self.assertEqual(json.loads(out)["warnings"], ['unknown field "spec.extra"'])
            self.assertEqual(run(["validate", os.path.join(tmp, "missing.json")])[0], 2)
            code, _, err = run(["run", "--server", "http://127.0.0.1:9", "--namespace", NS, "--records-dir", tmp])
            self.assertEqual(code, 1)
            self.assertIn("kubectl proxy", err)
            self.assertEqual(run(["run", "--server", "http://x", "--namespace", NS, "--records-dir", tmp, "--resync", "0"])[0], 2)
        code, out, _ = run(["demo", "--json"])
        self.assertEqual(code, 0)
        self.assertIn("events", json.loads(out))


if __name__ == "__main__":
    unittest.main()
