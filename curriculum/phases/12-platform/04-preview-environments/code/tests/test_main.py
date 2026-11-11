import contextlib
import copy
import io
import json
import os
import sys
import tempfile
import unittest
from datetime import timedelta
from fractions import Fraction

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import main  # noqa: E402

NOW = main.parse_time("2026-11-11T09:00:00Z")
PULLS_FILE = os.path.join(main.FIXTURES, "pulls.json")


def pulls():
    return main.load_pulls(PULLS_FILE)


def pull(number, **changes):
    found = copy.deepcopy(next(p for p in pulls() if p.number == number))
    for key, value in changes.items():
        setattr(found, key, value)
    return found


def by_kind(objects):
    return {main.kind_name(o): o for o in objects}


def run_cli(argv, cluster=None, runner=main.run_command):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = main.main(argv, runner=runner, cluster=cluster)
    return code, out.getvalue(), err.getvalue()


def pod(name, *containers, init=()):
    spec = {"containers": [{"name": n, "image": "registry.k8s.io/pause:3.10", "resources": r} for n, r in containers]}
    if init:
        spec["initContainers"] = [{"name": n, "image": "registry.k8s.io/pause:3.10", "resources": r} for n, r in init]
    return {"apiVersion": "v1", "kind": "Pod", "metadata": {"name": name}, "spec": spec}


def namespace_objects(used=None, hard=None):
    quota_hard = hard or dict(main.DEFAULT_QUOTA)
    return [
        {"kind": "LimitRange", "metadata": {"name": "preview-limits"}, "spec": {"limits": [dict({"type": "Container"}, **copy.deepcopy(main.DEFAULT_LIMITS))]}},
        {"kind": "ResourceQuota", "metadata": {"name": "preview-quota"}, "spec": {"hard": quota_hard},
         "status": {"hard": dict(quota_hard), "used": dict(used or {})}},
    ]


class NamesAndInputs(unittest.TestCase):
    def test_namespace_names_are_dns_labels_even_for_long_repositories(self):
        config = main.Config(repo="Payments_Service.Backend-With-A-Very-Long-Name-Indeed-And-More", prefix="preview")
        name = main.namespace_name(config, 12345)
        self.assertLessEqual(len(name), 63)
        self.assertRegex(name, r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?$")
        self.assertTrue(name.endswith("-pr-12345"))
        other = main.namespace_name(main.Config(repo="Payments_Service.Backend-With-A-Very-Long-Name-Indeed-And-Less"), 12345)
        self.assertNotEqual(name, other)
        self.assertEqual(main.namespace_name(main.Config(repo="shop", prefix="cc-preview-environments"), 7), "cc-preview-environments-shop-pr-7")

    def test_parse_pulls_reads_the_github_shape_and_rejects_bad_entries(self):
        found = {p.number: p for p in pulls()}
        self.assertEqual(found[101].login, "alice")
        self.assertEqual(found[103].labels, ["preview", "preview-ttl-1h"])
        self.assertEqual(found[104].state, "closed")
        with self.assertRaises(main.InputError) as missing:
            main.parse_pulls([{"number": 1, "head": {"sha": "abcdef1"}, "updated_at": "2026-11-11T00:00:00Z"}])
        self.assertIn("[0]", str(missing.exception))
        with self.assertRaises(main.InputError):
            main.parse_pulls([{"number": 1, "user": {"login": "a"}, "head": {"sha": "not-a-sha"}, "updated_at": "2026-11-11T00:00:00Z"}])
        with self.assertRaises(main.InputError):
            main.parse_pulls({"items": []})

    def test_ttl_label_overrides_the_default_and_expiry_counts_from_the_last_push(self):
        config = main.Config(ttl_hours=72)
        self.assertEqual(main.ttl_hours(pull(103), config), 1)
        self.assertEqual(main.expiry(pull(103), config), main.parse_time("2026-11-11T09:30:00Z"))
        self.assertEqual(main.expiry(pull(101), config), main.parse_time("2026-11-13T15:00:00Z"))

    def test_wanted_skips_closed_unlabelled_opted_out_and_expired_pull_requests(self):
        config = main.Config(require_label="preview")
        self.assertEqual(main.wanted(pull(101), config, NOW), (True, "open"))
        self.assertIn("closed", main.wanted(pull(104), config, NOW)[1])
        self.assertIn("no-preview", main.wanted(pull(105), config, NOW)[1])
        self.assertIn("missing label preview", main.wanted(pull(101, labels=[]), config, NOW)[1])
        self.assertIn("expired", main.wanted(pull(103), config, NOW + timedelta(hours=1))[1])


class DesiredObjects(unittest.TestCase):
    def test_one_environment_is_a_namespace_with_guard_rails_access_and_the_app(self):
        objects = by_kind(main.desired_objects(pull(101), main.Config()))
        self.assertEqual(sorted(objects), [
            "Deployment/app", "LimitRange/preview-limits", "Namespace/preview-shop-pr-101", "NetworkPolicy/allow-dns",
            "NetworkPolicy/allow-same-namespace", "NetworkPolicy/default-deny", "ResourceQuota/preview-quota",
            "RoleBinding/preview-author", "Service/app"])
        ns = objects["Namespace/preview-shop-pr-101"]["metadata"]
        self.assertEqual(ns["labels"][main.LABEL_OWNER], "alice")
        self.assertEqual(ns["labels"][main.LABEL_PR], "101")
        self.assertEqual(ns["labels"][main.LABEL_EXPIRES], str(int(main.parse_time("2026-11-13T15:00:00Z").timestamp())))
        self.assertEqual(ns["annotations"][main.JANITOR_EXPIRES], "2026-11-13T15:00:00Z")
        self.assertEqual(ns["annotations"][main.ANNOTATION_URL], "https://pr-101.shop.preview.example.test")
        for name, obj in objects.items():
            if obj["kind"] != "Namespace":
                self.assertEqual(obj["metadata"]["namespace"], "preview-shop-pr-101", name)
                self.assertEqual(obj["metadata"]["labels"][main.LABEL_MANAGED], "minienv", name)

    def test_deployment_runs_the_pull_request_image_with_requests_inside_the_limit_range(self):
        config = main.Config()
        container = by_kind(main.desired_objects(pull(101), config))["Deployment/app"]["spec"]["template"]["spec"]["containers"][0]
        self.assertEqual(container["image"], "ghcr.io/acme/shop:pr-101-a1b2c3d")
        admitted, problem = main.admit_pod({"metadata": {"name": "app-x"}, "spec": {"containers": [container]}}, namespace_objects())
        self.assertIsNone(problem)
        self.assertEqual(admitted["spec"]["containers"][0]["resources"], container["resources"])
        with self.assertRaises(main.InputError):
            main.image_for(pull(101), main.Config(image_template="x:{tag}"))

    def test_network_policies_deny_by_default_and_allow_dns_and_the_namespace(self):
        objects = by_kind(main.desired_objects(pull(101), main.Config()))
        deny = objects["NetworkPolicy/default-deny"]["spec"]
        self.assertEqual((deny["podSelector"], deny["policyTypes"]), ({}, ["Ingress", "Egress"]))
        self.assertNotIn("ingress", deny)
        dns = objects["NetworkPolicy/allow-dns"]["spec"]["egress"][0]
        self.assertEqual(sorted((p["protocol"], p["port"]) for p in dns["ports"]), [("TCP", 53), ("UDP", 53)])
        self.assertEqual(dns["to"][0]["podSelector"]["matchLabels"], {"k8s-app": "kube-dns"})
        self.assertNotIn("NetworkPolicy/allow-gateway", objects)
        self.assertNotIn("HTTPRoute/app", objects)

    def test_a_gateway_adds_an_httproute_and_one_ingress_exception(self):
        objects = by_kind(main.desired_objects(pull(101), main.Config(gateway="gateway-system/previews")))
        route = objects["HTTPRoute/app"]["spec"]
        self.assertEqual(route["hostnames"], ["pr-101.shop.preview.example.test"])
        self.assertEqual(route["parentRefs"], [{"name": "previews", "namespace": "gateway-system"}])
        peer = objects["NetworkPolicy/allow-gateway"]["spec"]["ingress"][0]["from"][0]
        self.assertEqual(peer["namespaceSelector"]["matchLabels"], {"kubernetes.io/metadata.name": "gateway-system"})
        with self.assertRaises(main.InputError):
            main.split_gateway("no-slash")

    def test_rolebinding_binds_only_the_author(self):
        binding = by_kind(main.desired_objects(pull(102), main.Config(user_prefix="oidc:")))["RoleBinding/preview-author"]
        self.assertEqual(binding["subjects"], [{"apiGroup": "rbac.authorization.k8s.io", "kind": "User", "name": "oidc:bob"}])
        self.assertEqual(binding["roleRef"]["name"], "edit")


class Quantities(unittest.TestCase):
    def test_parse_and_format_like_the_api_server(self):
        self.assertEqual(main.parse_quantity("250m")[0], Fraction(1, 4))
        self.assertEqual(main.parse_quantity("1Gi"), (Fraction(1024 ** 3), True))
        self.assertEqual(main.parse_quantity("1.5")[0], Fraction(3, 2))
        self.assertEqual(main.parse_quantity("1e3")[0], Fraction(1000))
        cases = [(Fraction(6, 5), False, "1200m"), (Fraction(1), False, "1"), (Fraction(2000), False, "2k"),
                 (Fraction(0), False, "0"), (Fraction(1024 ** 3), True, "1Gi"), (Fraction(384 * 1024 ** 2), True, "384Mi"),
                 (Fraction(1536 * 1024 ** 2), True, "1536Mi"), (Fraction(1500), False, "1500")]
        for value, binary, text in cases:
            self.assertEqual(main.format_quantity(value, binary), text)
        for bad in ("1x", "ten", "", "1.2.3"):
            with self.assertRaises(main.InputError):
                main.parse_quantity(bad)

    def test_covers_compares_quantities_by_value_and_ignores_server_defaults(self):
        live = {"spec": {"hard": {"requests.memory": "1Gi", "requests.cpu": "1"}, "extra": True}, "status": {}}
        self.assertTrue(main.covers(live, {"spec": {"hard": {"requests.memory": "1024Mi", "requests.cpu": "1000m"}}}))
        self.assertFalse(main.covers(live, {"spec": {"hard": {"requests.cpu": "2"}}}))
        self.assertFalse(main.covers({"ports": [{"port": 80}, {"port": 81}]}, {"ports": [{"port": 80}]}))


class Admission(unittest.TestCase):
    def test_limit_range_fills_defaults_and_requests_default_to_limits(self):
        admitted, problem = main.admit_pod(pod("plain", ("a", {}), ("b", {"limits": {"cpu": "400m"}})), namespace_objects())
        self.assertIsNone(problem)
        first, second = admitted["spec"]["containers"]
        self.assertEqual(first["resources"], {"limits": {"cpu": "250m", "memory": "256Mi"}, "requests": {"cpu": "100m", "memory": "128Mi"}})
        self.assertEqual(second["resources"]["requests"]["cpu"], "400m")
        self.assertEqual(second["resources"]["requests"]["memory"], "128Mi")

    def test_limit_range_max_rejects_a_container_with_the_api_servers_words(self):
        _, problem = main.admit_pod(pod("huge", ("huge", {"requests": {"cpu": "100m"}, "limits": {"cpu": "2"}})), namespace_objects())
        self.assertEqual(problem, 'pods "huge" is forbidden: maximum cpu usage per Container is 1, but limit is 2')

    def test_quota_rejection_names_only_the_exceeded_resources(self):
        each = {"limits": {"cpu": "600m", "memory": "64Mi"}}
        _, problem = main.admit_pod(pod("greedy", ("a", each), ("b", each)), namespace_objects(used={"requests.cpu": "100m", "pods": "1"}))
        self.assertEqual(problem, 'pods "greedy" is forbidden: exceeded quota: preview-quota, requested: requests.cpu=1200m, used: requests.cpu=100m, limited: requests.cpu=1')
        parsed = main.parse_rejection(problem)
        self.assertEqual(parsed["requested"], {"requests.cpu": Fraction(6, 5)})
        self.assertEqual(parsed["limited"], {"requests.cpu": Fraction(1)})

    def test_quota_counts_init_containers_as_a_maximum_and_pods_as_one(self):
        usage = main.pod_usage(pod("p", ("a", {"requests": {"cpu": "100m"}}), ("b", {"requests": {"cpu": "100m"}}), init=[("i", {"requests": {"cpu": "500m"}})]))
        self.assertEqual(usage["requests.cpu"][0], Fraction(1, 2))
        self.assertEqual(usage["pods"][0], 1)
        _, problem = main.admit_pod(pod("tenth", ("a", {})), namespace_objects(used={"pods": "10"}))
        self.assertIn("requested: pods=1, used: pods=10, limited: pods=10", problem)

    def test_quota_without_limit_range_requires_explicit_resources_and_a_synced_status(self):
        quota_only = namespace_objects()[1:]
        _, problem = main.admit_pod(pod("bare", ("app", {})), quota_only)
        self.assertIn("failed quota: preview-quota: must specify limits.cpu for: app", problem)
        unsynced = copy.deepcopy(quota_only)
        unsynced[0]["status"] = {}
        _, problem = main.admit_pod(pod("early", ("app", {"limits": {"cpu": "100m", "memory": "64Mi"}})), unsynced)
        self.assertIn("status unknown for quota: preview-quota", problem)


class Reconcile(unittest.TestCase):
    def setUp(self):
        self.cluster = main.FakeCluster()
        self.config = main.Config()

    def reconcile(self, prs=None, now=NOW, dry_run=False):
        return main.Reconciler(self.cluster, self.config, now, dry_run=dry_run).reconcile(pulls() if prs is None else prs)

    def versions(self):
        return {key: obj["metadata"]["resourceVersion"] for key, obj in self.cluster.objects.items()}

    def test_first_reconcile_creates_three_environments_and_skips_the_rest(self):
        report = self.reconcile()
        self.assertEqual([e["namespace"] for e in report["environments"]], ["preview-shop-pr-101", "preview-shop-pr-102", "preview-shop-pr-103"])
        self.assertEqual([s["pr"] for s in report["skipped"]], [104, 105])
        self.assertEqual(report["writes"], 27)
        self.assertTrue(all(action == "created" for env in report["environments"] for action in env["actions"].values()))

    def test_second_reconcile_is_a_no_op_even_after_server_defaults(self):
        self.reconcile()
        before = self.versions()
        quota = self.cluster.objects[("ResourceQuota", "preview-shop-pr-101", "preview-quota")]
        self.assertEqual(quota["spec"]["hard"]["requests.memory"], "1Gi")
        self.assertIn("clusterIP", self.cluster.objects[("Service", "preview-shop-pr-101", "app")]["spec"])
        writes = self.cluster.writes
        report = self.reconcile()
        self.assertEqual(report["writes"], 0)
        self.assertEqual(self.cluster.writes, writes)
        self.assertEqual(self.versions(), before)

    def test_a_new_push_updates_the_image_and_the_expiry_and_nothing_else(self):
        self.reconcile()
        pushed = [p for p in pulls() if p.number != 101] + [pull(101, head_sha="f" * 40, updated_at=NOW)]
        report = self.reconcile(pushed)
        env = next(e for e in report["environments"] if e["pr"] == 101)
        self.assertEqual({k: v for k, v in env["actions"].items() if v != "unchanged"}, {"Namespace/preview-shop-pr-101": "updated", "Deployment/app": "updated"})
        image = self.cluster.objects[("Deployment", "preview-shop-pr-101", "app")]["spec"]["template"]["spec"]["containers"][0]["image"]
        self.assertEqual(image, "ghcr.io/acme/shop:pr-101-fffffff")
        self.assertEqual(report["writes"], 2)

    def test_dry_run_writes_nothing(self):
        report = self.reconcile(dry_run=True)
        self.assertEqual(self.cluster.writes, 0)
        self.assertEqual(report["environments"][0]["actions"]["Namespace/preview-shop-pr-101"], "would be created")

    def test_refuses_to_adopt_a_namespace_it_does_not_manage(self):
        self.cluster.store({"apiVersion": "v1", "kind": "Namespace", "metadata": {"name": "preview-shop-pr-101", "labels": {}}, "status": {"phase": "Active"}}, "create")
        with self.assertRaises(main.ClusterError) as refused:
            self.reconcile()
        self.assertIn("not managed by minienv", str(refused.exception))

    def test_quota_is_enforced_in_the_fake_cluster_with_live_usage(self):
        self.reconcile()
        ns = "preview-shop-pr-101"
        self.cluster.create_pod(pod("one", ("a", {"requests": {"cpu": "500m"}, "limits": {"cpu": "500m"}})), ns)
        with self.assertRaises(main.ClusterError) as rejected:
            self.cluster.create_pod(pod("two", ("a", {"requests": {"cpu": "600m"}, "limits": {"cpu": "600m"}})), ns)
        self.assertIn("used: requests.cpu=500m", str(rejected.exception))
        self.assertEqual(self.cluster.objects[("ResourceQuota", ns, "preview-quota")]["status"]["used"]["pods"], "1")


class Reap(unittest.TestCase):
    def setUp(self):
        self.cluster = main.FakeCluster()
        self.config = main.Config()
        main.Reconciler(self.cluster, self.config, NOW).reconcile(pulls())

    def reap(self, prs, now=NOW + timedelta(hours=2), **kwargs):
        dry_run = kwargs.pop("dry_run", False)
        return main.Reconciler(self.cluster, self.config, now, dry_run=dry_run).reap(prs, **kwargs)

    def names(self):
        return sorted(k[2] for k in self.cluster.objects if k[0] == "Namespace")

    def leftover(self, kind, name, env, claim=False):
        obj = {"apiVersion": "v1", "kind": kind, "metadata": {"name": name, "labels": {} if claim else {main.LABEL_ENV: env}}, "spec": {}}
        if claim:
            obj["spec"]["claimRef"] = {"namespace": env, "name": "data"}
        self.cluster.store(obj, "create")

    def test_reaper_deletes_the_expired_environment_and_only_it(self):
        self.cluster.store({"apiVersion": "v1", "kind": "Namespace", "metadata": {"name": "preview-shop-pr-999", "labels": {}}, "status": {"phase": "Active"}}, "create")
        report = self.reap(pulls())
        self.assertEqual([(d["namespace"], d["reason"]) for d in report["deleted"]], [("preview-shop-pr-103", "expired at 2026-11-11T09:30:00Z")])
        self.assertEqual(self.names(), ["preview-shop-pr-101", "preview-shop-pr-102", "preview-shop-pr-999"])
        self.assertFalse([k for k in self.cluster.objects if k[1] == "preview-shop-pr-103"])

    def test_closed_pull_requests_are_reaped_but_an_empty_list_is_not_trusted(self):
        report = self.reap([p for p in pulls() if p.number != 102], now=NOW)
        self.assertEqual([d["namespace"] for d in report["deleted"]], ["preview-shop-pr-102"])
        guarded = self.reap([], now=NOW)
        self.assertEqual(guarded["deleted"], [])
        self.assertIn("empty", guarded["warnings"][0])
        forced = self.reap([], now=NOW, allow_empty=True)
        self.assertEqual([d["namespace"] for d in forced["deleted"]], ["preview-shop-pr-101", "preview-shop-pr-103"])

    def test_cluster_scoped_leftovers_go_with_their_environment(self):
        self.leftover("ClusterRoleBinding", "pr-103-reader", "preview-shop-pr-103")
        self.leftover("PersistentVolume", "pr-103-data", "preview-shop-pr-103", claim=True)
        self.leftover("ClusterRoleBinding", "pr-101-reader", "preview-shop-pr-101")
        dry = self.reap(pulls(), dry_run=True)
        self.assertEqual(dry["deleted"][0]["leftovers"], ["ClusterRoleBinding/pr-103-reader", "PersistentVolume/pr-103-data"])
        self.assertIn(("ClusterRoleBinding", "", "pr-103-reader"), self.cluster.objects)
        self.reap(pulls())
        cluster_scoped = sorted(k[2] for k in self.cluster.objects if k[0] in main.CLUSTER_LEFTOVER_KINDS)
        self.assertEqual(cluster_scoped, ["pr-101-reader"])

    def test_orphans_whose_namespace_is_already_gone_are_swept(self):
        self.leftover("PersistentVolume", "pr-77-data", "preview-shop-pr-77", claim=True)
        self.leftover("ClusterRole", "pr-78-role", "preview-shop-pr-78")
        self.leftover("ClusterRole", "someone-elses", "other-team-pr-1")
        report = self.reap(pulls(), now=NOW)
        self.assertEqual([o["namespace"] for o in report["orphans"]], ["preview-shop-pr-77", "preview-shop-pr-78"])
        self.assertEqual(sorted(k[2] for k in self.cluster.objects if k[0] in main.CLUSTER_LEFTOVER_KINDS), ["someone-elses"])


class Cost(unittest.TestCase):
    def test_cost_comes_from_requests_the_quota_ceiling_and_time_left(self):
        cluster = main.FakeCluster()
        config = main.Config()
        reconciler = main.Reconciler(cluster, config, NOW)
        reconciler.reconcile(pulls())
        report = main.cost_report(cluster, reconciler, {"currency": "EUR", "cpu_core_hour": 0.04, "memory_gib_hour": 0.008})
        rows = {r["pr"]: r for r in report["environments"]}
        self.assertEqual((rows["101"]["requests_cpu"], rows["101"]["requests_memory"]), ("100m", "128Mi"))
        self.assertAlmostEqual(rows["101"]["hourly"], 0.1 * 0.04 + 0.125 * 0.008)
        self.assertAlmostEqual(rows["101"]["hourly_ceiling"], 1 * 0.04 + 1 * 0.008)
        self.assertAlmostEqual(rows["103"]["hours_left"], 0.5)
        self.assertAlmostEqual(rows["101"]["projected"], round(0.005 * 54, 4))
        self.assertEqual(report["currency"], "EUR")


class FakeRunner:
    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = []

    def __call__(self, argv, stdin=None):
        self.calls.append((argv, stdin))
        return self.replies.pop(0) if self.replies else (0, "", "")


class KubectlAndCli(unittest.TestCase):
    def test_kubectl_uses_the_context_server_side_apply_and_maps_not_found_to_none(self):
        runner = FakeRunner([(0, "", ""), (0, '{"kind": "Namespace"}', ""), (1, "", 'Error from server (Forbidden): namespaces is forbidden')])
        kubectl = main.Kubectl("kind-cc-smoke", runner)
        self.assertIsNone(kubectl.get("ResourceQuota", "preview-quota", "ns"))
        self.assertEqual(runner.calls[0][0], ["kubectl", "--context", "kind-cc-smoke", "get", "resourcequotas", "preview-quota", "-n", "ns", "-o", "json", "--ignore-not-found"])
        kubectl.apply({"kind": "Namespace", "metadata": {"name": "x"}})
        self.assertIn("--server-side", runner.calls[1][0])
        self.assertIn("--field-manager=minienv", runner.calls[1][0])
        self.assertEqual(json.loads(runner.calls[1][1])["metadata"]["name"], "x")
        with self.assertRaises(main.ClusterError) as failed:
            kubectl.list("Namespace", "a=b")
        self.assertIn("Forbidden", str(failed.exception))

    def test_cli_reconcile_and_reap_against_an_injected_cluster(self):
        cluster = main.FakeCluster()
        code, out, err = run_cli(["reconcile", "--prs", PULLS_FILE, "--now", "2026-11-11T09:00:00Z", "--json"], cluster=cluster)
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(out)["writes"], 27)
        code, out, _ = run_cli(["reap", "--prs", PULLS_FILE, "--now", "2026-11-11T11:00:00Z", "--json"], cluster=cluster)
        self.assertEqual(code, 0)
        self.assertEqual([d["namespace"] for d in json.loads(out)["deleted"]], ["preview-shop-pr-103"])
        code, out, _ = run_cli(["list", "--now", "2026-11-11T11:00:00Z", "--json"], cluster=cluster)
        self.assertEqual([r["namespace"] for r in json.loads(out)], ["preview-shop-pr-101", "preview-shop-pr-102"])

    def test_cli_render_admit_and_errors(self):
        code, out, _ = run_cli(["render", "--prs", PULLS_FILE, "--number", "102", "--prefix", "cc-preview-environments"])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["items"][0]["metadata"]["name"], "cc-preview-environments-shop-pr-102")
        self.assertEqual(run_cli(["render", "--prs", PULLS_FILE, "--number", "999"])[0], 2)
        self.assertEqual(run_cli(["render", "--prs", "/nonexistent.json", "--number", "1"])[0], 2)
        self.assertEqual(run_cli(["reconcile", "--prs", PULLS_FILE, "--prefix", "Bad_Prefix"])[0], 2)
        with tempfile.TemporaryDirectory() as tmp:
            pod_file, objects_file = os.path.join(tmp, "pod.json"), os.path.join(tmp, "objects.json")
            with open(pod_file, "w") as handle:
                json.dump(pod("huge", ("huge", {"limits": {"cpu": "2"}})), handle)
            with open(objects_file, "w") as handle:
                json.dump({"apiVersion": "v1", "kind": "List", "items": namespace_objects()}, handle)
            code, out, err = run_cli(["admit", pod_file, "--objects", objects_file])
            self.assertEqual(code, 1)
            self.assertEqual(out, "")
            self.assertIn("maximum cpu usage per Container is 1, but limit is 2", err)

    def test_demo_runs_the_whole_lifecycle(self):
        code, out, _ = run_cli(["demo", "--json"])
        self.assertEqual(code, 0)
        result = json.loads(out)
        self.assertEqual(result["writes"], [27, 0, 2])
        self.assertEqual(result["remaining"], ["preview-shop-pr-101"])
        self.assertEqual(len(result["rejections"]), 2)


if __name__ == "__main__":
    unittest.main()
