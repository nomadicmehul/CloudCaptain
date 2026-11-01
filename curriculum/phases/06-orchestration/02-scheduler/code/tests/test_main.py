import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import main  # noqa: E402

FIXTURES = os.path.join(os.path.dirname(__file__), "..", "fixtures")


def node(name, cpu="4", memory="8Gi", pods="110", labels=None, taints=None, unschedulable=False):
    merged = {"kubernetes.io/hostname": name}
    merged.update(labels or {})
    return main.make_node(
        {
            "kind": "Node",
            "metadata": {"name": name, "labels": merged},
            "spec": {"taints": taints or [], "unschedulable": unschedulable},
            "status": {"allocatable": {"cpu": cpu, "memory": memory, "pods": pods}},
        }
    )


def pod(name, cpu=None, memory=None, namespace="default", labels=None, on=None, phase="Running", **spec):
    requests = {}
    if cpu is not None:
        requests["cpu"] = cpu
    if memory is not None:
        requests["memory"] = memory
    body = {"containers": [{"name": "main", "image": "registry.k8s.io/pause:3.10", "resources": {"requests": requests}}]}
    body.update(spec)
    if on:
        body["nodeName"] = on
    return main.make_pod({"kind": "Pod", "metadata": {"name": name, "namespace": namespace, "labels": labels or {}}, "spec": body, "status": {"phase": phase}})


def run(argv):
    out, err = StringIO(), StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = main.main(argv)
    return code, out.getvalue(), err.getvalue()


class QuantityTests(unittest.TestCase):
    def test_cpu_and_memory_quantities_parse_like_the_api_server_and_round_up(self):
        self.assertEqual(main.milli_cpu("500m"), 500)
        self.assertEqual(main.milli_cpu("1"), 1000)
        self.assertEqual(main.milli_cpu("0.5"), 500)
        self.assertEqual(main.milli_cpu("2.25"), 2250)
        self.assertEqual(main.milli_cpu("0.0001"), 1)
        self.assertEqual(main.whole_units("128Mi"), 128 * 2**20)
        self.assertEqual(main.whole_units("1Gi"), 2**30)
        self.assertEqual(main.whole_units("1G"), 10**9)
        self.assertEqual(main.whole_units("16405884Ki"), 16405884 * 1024)
        self.assertEqual(main.whole_units("1e3"), 1000)
        self.assertEqual(main.whole_units("1E"), 10**18)
        self.assertEqual(main.whole_units("1.5Gi"), 3 * 2**29)
        for bad in ("12 Mi", "1GB", "abc", ""):
            with self.assertRaises(main.InputError):
                main.parse_quantity(bad)

    def test_show_prints_cpu_in_millicores_and_bytes_in_binary_units(self):
        self.assertEqual(main.show("cpu", 1500), "1500m")
        self.assertEqual(main.show("memory", 2**31), "2Gi")
        self.assertEqual(main.show("memory", 512 * 2**20), "512Mi")
        self.assertEqual(main.show("memory", 0), "0")
        self.assertEqual(main.show("nvidia.com/gpu", 1), "1")


class RequestTests(unittest.TestCase):
    def test_containers_add_up_init_containers_peak_and_limits_default_requests(self):
        spec = {
            "initContainers": [{"name": "migrate", "resources": {"requests": {"cpu": "2", "memory": "64Mi"}}}],
            "containers": [
                {"name": "app", "resources": {"requests": {"cpu": "500m", "memory": "256Mi"}}},
                {"name": "proxy", "resources": {"limits": {"cpu": "250m", "memory": "128Mi"}}},
            ],
            "overhead": {"cpu": "10m"},
        }
        self.assertEqual(main.pod_requests(spec), {"cpu": 2010, "memory": 384 * 2**20})

    def test_sidecars_run_alongside_the_app_and_count_towards_later_init_containers(self):
        spec = {
            "initContainers": [
                {"name": "mesh", "restartPolicy": "Always", "resources": {"requests": {"cpu": "100m"}}},
                {"name": "warmup", "resources": {"requests": {"cpu": "1"}}},
            ],
            "containers": [{"name": "app", "resources": {"requests": {"cpu": "300m"}}}],
        }
        self.assertEqual(main.pod_requests(spec)["cpu"], 1100)
        spec["initContainers"][1]["resources"]["requests"]["cpu"] = "100m"
        self.assertEqual(main.pod_requests(spec)["cpu"], 400)

    def test_scoring_view_fills_missing_requests_but_keeps_an_explicit_zero(self):
        spec = {"containers": [{"name": "a"}, {"name": "b", "resources": {"requests": {"cpu": "0"}}}]}
        self.assertEqual(main.pod_requests(spec), {"cpu": 0})
        self.assertEqual(main.pod_requests(spec, non_zero=True), {"cpu": 100, "memory": 2 * 200 * 2**20})


class FilterTests(unittest.TestCase):
    def status(self, p, n, others=()):
        cluster = main.Cluster([n], list(others))
        return main.filter_nodes(p, cluster)[n.name]

    def test_toleration_matching_equal_exists_empty_key_and_effect(self):
        taint = {"key": "dedicated", "value": "gpu", "effect": "NoSchedule"}
        self.assertTrue(main.tolerates({"key": "dedicated", "operator": "Equal", "value": "gpu", "effect": "NoSchedule"}, taint))
        self.assertTrue(main.tolerates({"key": "dedicated", "value": "gpu"}, taint))
        self.assertFalse(main.tolerates({"key": "dedicated", "value": "cpu"}, taint))
        self.assertTrue(main.tolerates({"key": "dedicated", "operator": "Exists"}, taint))
        self.assertTrue(main.tolerates({"operator": "Exists"}, taint))
        self.assertFalse(main.tolerates({"key": "dedicated", "operator": "Exists", "effect": "NoExecute"}, taint))
        self.assertFalse(main.tolerates({"key": "other", "operator": "Exists"}, taint))

    def test_untolerated_taint_is_named_in_kube_scheduler_wording_and_prefer_is_ignored(self):
        tainted = node("n1", taints=[
            {"key": "soft", "value": "x", "effect": "PreferNoSchedule"},
            {"key": "node-role.kubernetes.io/control-plane", "effect": "NoSchedule"},
        ])
        status = self.status(pod("p", cpu="100m"), tainted)
        self.assertEqual(status.plugin, "TaintToleration")
        self.assertEqual(status.reasons, ["node(s) had untolerated taint {node-role.kubernetes.io/control-plane: }"])
        tolerated = pod("p", cpu="100m", tolerations=[{"key": "node-role.kubernetes.io/control-plane", "operator": "Exists", "effect": "NoSchedule"}])
        self.assertIsNone(self.status(tolerated, tainted))
        evicting = node("n2", taints=[{"key": "cc-smoke", "value": "1", "effect": "NoExecute"}])
        self.assertEqual(self.status(pod("p"), evicting).reasons, ["node(s) had untolerated taint {cc-smoke: 1}"])

    def test_cordoned_node_is_unschedulable_unless_the_pod_tolerates_the_unschedulable_taint(self):
        cordoned = node("n1", unschedulable=True)
        self.assertEqual(self.status(pod("p"), cordoned).reasons, ["node(s) were unschedulable"])
        daemon = pod("p", tolerations=[{"key": "node.kubernetes.io/unschedulable", "operator": "Exists", "effect": "NoSchedule"}])
        self.assertIsNone(self.status(daemon, cordoned))

    def test_node_name_pins_the_pod_to_one_node(self):
        pinned = pod("p", nodeName="n2")
        self.assertEqual(self.status(pinned, node("n1")).reasons, ["node(s) didn't match the requested node name"])
        self.assertIsNone(self.status(pinned, node("n2")))

    def test_node_selector_and_required_affinity_operators(self):
        n = node("n1", labels={"zone": "a", "gen": "5", "disktype": "ssd"})
        labels = n.labels
        self.assertTrue(main.requirement_matches({"key": "zone", "operator": "In", "values": ["a", "b"]}, labels))
        self.assertFalse(main.requirement_matches({"key": "zone", "operator": "NotIn", "values": ["a"]}, labels))
        self.assertTrue(main.requirement_matches({"key": "gpu", "operator": "NotIn", "values": ["x"]}, labels))
        self.assertTrue(main.requirement_matches({"key": "disktype", "operator": "Exists"}, labels))
        self.assertTrue(main.requirement_matches({"key": "gpu", "operator": "DoesNotExist"}, labels))
        self.assertTrue(main.requirement_matches({"key": "gen", "operator": "Gt", "values": ["4"]}, labels))
        self.assertFalse(main.requirement_matches({"key": "gen", "operator": "Lt", "values": ["4"]}, labels))
        with self.assertRaises(main.InputError):
            main.requirement_matches({"key": "zone", "operator": "Near"}, labels)
        either = {"requiredDuringSchedulingIgnoredDuringExecution": {"nodeSelectorTerms": [
            {"matchExpressions": [{"key": "zone", "operator": "In", "values": ["b"]}]},
            {"matchExpressions": [{"key": "disktype", "operator": "In", "values": ["ssd"]}, {"key": "gpu", "operator": "DoesNotExist"}]},
        ]}}
        self.assertIsNone(self.status(pod("p", affinity={"nodeAffinity": either}), n))
        empty_term = {"requiredDuringSchedulingIgnoredDuringExecution": {"nodeSelectorTerms": [{}]}}
        self.assertEqual(self.status(pod("p", affinity={"nodeAffinity": empty_term}), n).reasons, ["node(s) didn't match Pod's node affinity/selector"])
        selected = self.status(pod("p", nodeSelector={"disktype": "hdd"}), n)
        self.assertEqual(selected.detail, "nodeSelector wants disktype=hdd, node has disktype=ssd")
        by_name = {"requiredDuringSchedulingIgnoredDuringExecution": {"nodeSelectorTerms": [{"matchFields": [{"key": "metadata.name", "operator": "In", "values": ["n1"]}]}]}}
        self.assertIsNone(self.status(pod("p", affinity={"nodeAffinity": by_name}), n))

    def test_resources_fit_counts_bound_pods_skips_finished_ones_and_reports_every_shortage(self):
        n = node("n1", cpu="2", memory="4Gi", pods="3")
        running = pod("a", cpu="1500m", memory="1Gi", on="n1")
        finished = pod("job", cpu="2", memory="2Gi", on="n1", phase="Succeeded")
        bound = main.bound_pods([running, finished])
        self.assertEqual([p.name for p in bound], ["a"])
        status = self.status(pod("p", cpu="600m", memory="3500Mi"), n, bound)
        self.assertEqual(status.plugin, "NodeResourcesFit")
        self.assertEqual(status.code, main.UNSCHEDULABLE)
        self.assertEqual(status.reasons, ["Insufficient cpu", "Insufficient memory"])
        self.assertIn("cpu: requested 600m, free 500m of 2000m", status.detail)
        self.assertIsNone(self.status(pod("p", cpu="500m"), n, bound))
        self.assertIsNone(self.status(pod("p"), node("n2", cpu="0")))
        full = node("n3", pods="1")
        self.assertEqual(self.status(pod("p"), full, [pod("x", on="n3")]).reasons, ["Too many pods"])
        gpu = pod("p", resources=None)
        gpu.requests["nvidia.com/gpu"] = 1
        self.assertEqual(self.status(gpu, node("n4")).reasons, ["Insufficient nvidia.com/gpu"])

    def test_the_first_failing_filter_decides_the_reason_for_a_node(self):
        n = node("n1", cpu="1", taints=[{"key": "dedicated", "value": "db", "effect": "NoSchedule"}], labels={"zone": "a"})
        status = self.status(pod("p", cpu="8", nodeSelector={"zone": "b"}), n)
        self.assertEqual(status.plugin, "TaintToleration")
        self.assertEqual(status.reasons, ["node(s) had untolerated taint {dedicated: db}"])

    def test_topology_spread_skew_counts_tainted_domains_unless_taints_are_honored(self):
        nodes = [
            node("cp", taints=[{"key": "node-role.kubernetes.io/control-plane", "effect": "NoSchedule"}]),
            node("w1"),
            node("w2"),
        ]
        bound = [pod("api-1", labels={"app": "api"}, on="w1"), pod("api-2", labels={"app": "api"}, on="w2"), pod("other", labels={"app": "api"}, namespace="team-b", on="w1")]
        constraint = {"maxSkew": 1, "topologyKey": "kubernetes.io/hostname", "whenUnsatisfiable": "DoNotSchedule", "labelSelector": {"matchLabels": {"app": "api"}}}
        cluster = main.Cluster(nodes, bound)
        statuses = main.filter_nodes(pod("api-3", labels={"app": "api"}, topologySpreadConstraints=[constraint]), cluster)
        self.assertEqual(statuses["w1"].reasons, ["node(s) didn't match pod topology spread constraints"])
        self.assertEqual(statuses["w1"].detail, "kubernetes.io/hostname=w1: skew 2 > maxSkew 1")
        honored = dict(constraint, nodeTaintsPolicy="Honor")
        statuses = main.filter_nodes(pod("api-3", labels={"app": "api"}, topologySpreadConstraints=[honored]), cluster)
        self.assertIsNone(statuses["w1"])
        self.assertIsNone(statuses["w2"])
        zoned = dict(constraint, topologyKey="topology.kubernetes.io/zone")
        statuses = main.filter_nodes(pod("api-3", labels={"app": "api"}, topologySpreadConstraints=[zoned]), cluster)
        self.assertEqual(statuses["w1"].reasons, ["node(s) didn't match pod topology spread constraints (missing required label)"])
        self.assertEqual(statuses["w1"].code, main.UNRESOLVABLE)

    def test_min_domains_treats_the_global_minimum_as_zero_until_enough_domains_exist(self):
        nodes = [node("w1", labels={"zone": "a"}), node("w2", labels={"zone": "b"})]
        bound = [pod("api-1", labels={"app": "api"}, on="w1"), pod("api-2", labels={"app": "api"}, on="w2")]
        base = {"maxSkew": 1, "topologyKey": "zone", "whenUnsatisfiable": "DoNotSchedule", "labelSelector": {"matchLabels": {"app": "api"}}}
        cluster = main.Cluster(nodes, bound)
        self.assertIsNone(main.filter_nodes(pod("api-3", labels={"app": "api"}, topologySpreadConstraints=[base]), cluster)["w1"])
        strict = dict(base, minDomains=3)
        statuses = main.filter_nodes(pod("api-3", labels={"app": "api"}, topologySpreadConstraints=[strict]), cluster)
        self.assertIsNotNone(statuses["w1"])
        self.assertIsNotNone(statuses["w2"])


class MessageTests(unittest.TestCase):
    def test_failed_scheduling_message_is_a_sorted_histogram_in_kube_scheduler_format(self):
        statuses = [
            main.Status("NodeResourcesFit", main.UNSCHEDULABLE, ["Insufficient cpu"]),
            main.Status("TaintToleration", main.UNRESOLVABLE, ["node(s) had untolerated taint {k: v}"]),
            main.Status("NodeResourcesFit", main.UNSCHEDULABLE, ["Insufficient cpu"]),
        ]
        self.assertEqual(main.fit_error(3, statuses), "0/3 nodes are available: 1 node(s) had untolerated taint {k: v}, 2 Insufficient cpu.")
        both = [main.Status("NodeResourcesFit", main.UNSCHEDULABLE, ["Insufficient cpu", "Insufficient memory"])]
        self.assertEqual(main.fit_error(1, both), "0/1 nodes are available: 1 Insufficient cpu, 1 Insufficient memory.")
        many = [main.Status("x", main.UNSCHEDULABLE, ["b"])] * 10 + [main.Status("x", main.UNSCHEDULABLE, ["a"])] * 2
        self.assertEqual(main.fit_error(12, many), "0/12 nodes are available: 10 b, 2 a.")

    def test_preemption_sentence_follows_the_filter_codes_and_priorities(self):
        nodes = [node("cp", cpu="2", taints=[{"key": "node-role.kubernetes.io/control-plane", "effect": "NoSchedule"}]), node("w1", cpu="2")]
        bound = [pod("low", cpu="1500m", on="w1")]
        result = main.schedule(pod("big", cpu="1"), main.Cluster(nodes, bound))
        self.assertIsNone(result.node)
        self.assertEqual(
            result.message,
            "0/2 nodes are available: 1 Insufficient cpu, 1 node(s) had untolerated taint {node-role.kubernetes.io/control-plane: }. "
            "preemption: 0/2 nodes are available: 1 No preemption victims found for incoming pod, 1 Preemption is not helpful for scheduling.",
        )
        never = main.schedule(pod("big", cpu="1", priority=100, preemptionPolicy="Never"), main.Cluster(nodes, bound))
        self.assertTrue(never.message.endswith("preemption: not eligible due to preemptionPolicy=Never."))
        hopeless = main.schedule(pod("huge", cpu="3", priority=100), main.Cluster(nodes, bound))
        self.assertTrue(hopeless.message.endswith("preemption: 0/2 nodes are available: 1 Insufficient cpu, 1 Preemption is not helpful for scheduling."))

    def test_preemption_nominates_a_node_and_reprieves_every_victim_it_can(self):
        nodes = [node("w1", cpu="4"), node("w2", cpu="4")]
        bound = [
            pod("a", cpu="2", on="w1", priority=10),
            pod("b", cpu="1500m", on="w1", priority=5),
            pod("c", cpu="3", on="w2", priority=50),
            pod("vip", cpu="200m", on="w2", priority=5000),
        ]
        result = main.schedule(pod("urgent", cpu="2", priority=1000), main.Cluster(nodes, bound))
        self.assertIsNone(result.node)
        self.assertEqual(result.preemption.node, "w1")
        self.assertEqual(result.preemption.victims, ["default/b"])
        self.assertEqual(result.message, result.filter_message)
        self.assertEqual(main.schedule(pod("x"), main.Cluster([], [])).message, "no nodes available to schedule pods")


class ScoreTests(unittest.TestCase):
    def test_least_and_most_allocated_use_integer_math_and_pick_opposite_nodes(self):
        self.assertEqual(main.least_allocated([(2500, 3800), (3 * 2**30, 15357308 * 1024)]), 56)
        self.assertEqual(main.most_allocated([(2500, 3800), (3 * 2**30, 15357308 * 1024)]), 42)
        self.assertEqual(main.least_allocated([(5000, 4000), (0, 0)]), 0)
        self.assertEqual(main.most_allocated([(5000, 4000)]), 100)
        nodes = [node("busy", cpu="4", memory="8Gi"), node("idle", cpu="4", memory="8Gi")]
        cluster = main.Cluster(nodes, [pod("x", cpu="3", memory="6Gi", on="busy")])
        self.assertEqual(main.schedule(pod("p", cpu="500m", memory="1Gi"), cluster).node, "idle")
        self.assertEqual(main.schedule(pod("p", cpu="500m", memory="1Gi"), cluster, "MostAllocated").node, "busy")
        with self.assertRaises(main.InputError):
            main.schedule(pod("p"), cluster, "Random")

    def test_balanced_allocation_rewards_equal_cpu_and_memory_fractions(self):
        self.assertEqual(main.balanced_allocation([(2000, 4000), (4 * 2**30, 8 * 2**30)]), 100)
        self.assertEqual(main.balanced_allocation([(4000, 4000), (0, 8 * 2**30)]), 50)
        self.assertEqual(main.balanced_allocation([(9000, 4000), (0, 8 * 2**30)]), 50)
        self.assertEqual(main.balanced_allocation([(1, 0)]), 100)

    def test_normalize_scales_to_the_best_node_and_reverses_for_taint_preferences(self):
        self.assertEqual(main.normalize({"a": 30, "b": 60, "c": 0}), {"a": 50, "b": 100, "c": 0})
        self.assertEqual(main.normalize({"a": 1, "b": 2}, reverse=True), {"a": 50, "b": 0})
        self.assertEqual(main.normalize({"a": 0, "b": 0}, reverse=True), {"a": 100, "b": 100})

    def test_preferred_affinity_and_prefer_no_schedule_change_the_winner(self):
        nodes = [node("hdd", labels={"disktype": "hdd"}), node("ssd", labels={"disktype": "ssd"}, taints=[{"key": "spot", "value": "true", "effect": "PreferNoSchedule"}])]
        cluster = main.Cluster(nodes, [])
        plain = main.schedule(pod("p", cpu="100m"), cluster)
        self.assertEqual(plain.node, "hdd")
        self.assertEqual({s.node: s.plugins["TaintToleration"] for s in plain.scores}, {"hdd": 100, "ssd": 0})
        prefer = {"preferredDuringSchedulingIgnoredDuringExecution": [
            {"weight": 80, "preference": {"matchExpressions": [{"key": "disktype", "operator": "In", "values": ["ssd"]}]}},
            {"weight": 20, "preference": {"matchExpressions": [{"key": "disktype", "operator": "In", "values": ["hdd"]}]}},
        ]}
        tolerant = pod("p", cpu="100m", affinity={"nodeAffinity": prefer}, tolerations=[{"key": "spot", "operator": "Exists"}])
        result = main.schedule(tolerant, cluster)
        self.assertEqual(result.node, "ssd")
        self.assertEqual({s.node: s.plugins["NodeAffinity"] for s in result.scores}, {"ssd": 100, "hdd": 25})
        self.assertEqual(result.scores[0].total, sum(main.WEIGHTS[p] * v for p, v in result.scores[0].plugins.items()))

    def test_soft_spread_prefers_the_emptier_domain(self):
        nodes = [node("w1", labels={"zone": "a"}), node("w2", labels={"zone": "a"}), node("w3", labels={"zone": "b"})]
        bound = [pod("api-1", labels={"app": "api"}, on="w1"), pod("api-2", labels={"app": "api"}, on="w2")]
        soft = [{"maxSkew": 1, "topologyKey": "zone", "whenUnsatisfiable": "ScheduleAnyway", "labelSelector": {"matchLabels": {"app": "api"}}}]
        result = main.schedule(pod("api-3", labels={"app": "api"}, topologySpreadConstraints=soft), main.Cluster(nodes, bound))
        spread = {s.node: s.plugins["PodTopologySpread"] for s in result.scores}
        self.assertEqual(spread, {"w1": 0, "w2": 0, "w3": 100})
        self.assertEqual(result.node, "w3")

    def test_ties_break_by_node_name_and_a_single_feasible_node_skips_scoring(self):
        twins = main.Cluster([node("b"), node("a")], [])
        result = main.schedule(pod("p", cpu="100m"), twins)
        self.assertEqual(result.scores[0].total, result.scores[1].total)
        self.assertEqual(result.node, "a")
        lone = main.schedule(pod("p"), main.Cluster([node("only")], []))
        self.assertEqual((lone.node, lone.scores), ("only", []))


class ManifestAndCliTests(unittest.TestCase):
    def test_workload_manifests_contribute_their_pod_template(self):
        deployment = {
            "kind": "Deployment",
            "metadata": {"name": "web", "namespace": "shop"},
            "spec": {"template": {"metadata": {"labels": {"app": "web"}}, "spec": {"containers": [{"name": "web", "resources": {"requests": {"cpu": "250m"}}}]}}},
        }
        p = main.make_pod(deployment)
        self.assertEqual((p.key, p.labels, p.requests), ("shop/web", {"app": "web"}, {"cpu": 250}))
        with self.assertRaises(main.InputError):
            main.make_pod({"kind": "Service", "metadata": {"name": "web"}, "spec": {}})
        with self.assertRaises(main.InputError):
            main.make_pod({"kind": "Pod", "metadata": {"name": "x"}, "spec": {"containers": [{"name": "a", "resources": {"requests": {"cpu": "lots"}}}]}})

    def test_fixtures_reproduce_the_outputs_shown_in_the_lesson(self):
        def fixture(name):
            return ["--nodes", os.path.join(FIXTURES, "nodes.json"), "--pods", os.path.join(FIXTURES, "pods.json"), "--pod", os.path.join(FIXTURES, name), "--json"]

        code, out, _ = run(fixture("pod-web.json"))
        web = json.loads(out)
        self.assertEqual((code, web["node"]), (0, "worker-a"))
        self.assertEqual([(s["node"], s["total"]) for s in web["scores"]], [("worker-a", 433), ("worker-b", 420)])
        code, out, _ = run(fixture("pod-web.json") + ["--strategy", "MostAllocated"])
        self.assertEqual((code, json.loads(out)["node"]), (0, "worker-b"))
        code, out, _ = run(fixture("pod-big.json"))
        big = json.loads(out)
        self.assertEqual(code, 1)
        self.assertEqual(big["filter_message"], "0/3 nodes are available: 1 node(s) had untolerated taint {node-role.kubernetes.io/control-plane: }, 2 Insufficient cpu.")
        self.assertTrue(big["message"].endswith("preemption: 0/3 nodes are available: 1 Preemption is not helpful for scheduling, 2 No preemption victims found for incoming pod."))
        _, out, _ = run(fixture("pod-urgent.json"))
        self.assertEqual(json.loads(out)["preemption"], {"message": "preemption: nominate worker-a and evict default/batch-1 (priority 0)", "node": "worker-a", "victims": ["default/batch-1"]})
        _, out, _ = run(fixture("pod-gpu.json"))
        self.assertIn("2 node(s) didn't match Pod's node affinity/selector.", json.loads(out)["filter_message"])
        _, out, _ = run(fixture("pod-spread.json"))
        self.assertIn("2 node(s) didn't match pod topology spread constraints.", json.loads(out)["filter_message"])

    def test_cli_text_output_exit_codes_and_input_errors(self):
        with tempfile.TemporaryDirectory() as tmp:
            nodes = os.path.join(tmp, "nodes.json")
            spec = os.path.join(tmp, "pod.json")
            with open(nodes, "w", encoding="utf-8") as handle:
                json.dump({"kind": "List", "items": [{"kind": "Node", "metadata": {"name": "kind-control-plane"}, "status": {"allocatable": {"cpu": "4", "memory": "8Gi", "pods": "110"}}}]}, handle)
            with open(spec, "w", encoding="utf-8") as handle:
                json.dump({"kind": "Pod", "metadata": {"name": "hog", "namespace": "cc"}, "spec": {"containers": [{"name": "a", "resources": {"requests": {"cpu": "1000"}}}]}}, handle)
            code, out, _ = run(["--nodes", nodes, "--pod", spec])
            self.assertEqual(code, 1)
            self.assertIn("FailedScheduling: 0/1 nodes are available: 1 Insufficient cpu. preemption: 0/1 nodes are available: 1 No preemption victims found for incoming pod.", out)
            self.assertIn("cpu: requested 1000000m, free 4000m of 4000m", out)
            with open(spec, "w", encoding="utf-8") as handle:
                handle.write("apiVersion: v1\nkind: Pod\n")
            code, _, err = run(["--nodes", nodes, "--pod", spec])
            self.assertEqual(code, 2)
            self.assertIn("is not JSON", err)
            code, _, err = run(["--nodes", os.path.join(tmp, "missing.json"), "--pod", spec])
            self.assertEqual(code, 2)
            self.assertIn("cannot read", err)
        code, out, _ = run(["--nodes", os.path.join(FIXTURES, "nodes.json"), "--pod", os.path.join(FIXTURES, "pod-web.json")])
        self.assertEqual(code, 0)
        self.assertIn("bind default/web to worker-a", out)
        self.assertIn("Fit is LeastAllocated", out)


if __name__ == "__main__":
    unittest.main()
