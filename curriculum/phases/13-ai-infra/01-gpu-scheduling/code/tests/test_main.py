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
DGX = {"model": "A100-SXM4-80GB", "count": 8, "memory_gib": 80, "islands": [[0, 1, 2, 3, 4, 5, 6, 7]], "nvlink_gbps": 300, "pcie_gbps": 32}
PAIRS = {"model": "A100-PCIE-80GB", "count": 8, "memory_gib": 80, "islands": [[0, 1], [2, 3], [4, 5], [6, 7]], "nvlink_gbps": 300, "pcie_gbps": 32}


def fixture(name):
    with open(os.path.join(FIXTURES, name), encoding="utf-8") as handle:
        return json.load(handle)


def cluster(*nodes, network=0):
    return main.load_cluster({"network_gbps": network, "nodes": list(nodes)})


def node(name, gpu, busy=None):
    raw = {"name": name, "gpu": dict(gpu)}
    if busy:
        raw["busy"] = busy
    return raw


def pods(*specs):
    return main.load_pods(list(specs))


def run(argv):
    out, err = StringIO(), StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = main.main(argv)
    return code, out.getvalue(), err.getvalue()


def k8s_node(name, gpus=None, taints=None, unschedulable=False):
    allocatable = {"cpu": "4", "memory": "8Gi", "pods": "110"}
    if gpus is not None:
        allocatable["example.com/gpu"] = str(gpus)
    return {"kind": "Node", "metadata": {"name": name}, "spec": {"taints": taints or [], "unschedulable": unschedulable}, "status": {"capacity": dict(allocatable), "allocatable": allocatable}}


def k8s_pod(name, gpus=None, on=None, phase="Running", claims=None, use="limits", tolerations=None):
    resources = {use: {"example.com/gpu": str(gpus)}} if gpus is not None else {}
    spec = {"containers": [{"name": "main", "image": "registry.k8s.io/pause:3.10", "resources": resources}]}
    if on:
        spec["nodeName"] = on
    if claims:
        spec["resourceClaims"] = [{"name": c, "resourceClaimName": c} for c in claims]
        spec["containers"][0]["resources"]["claims"] = [{"name": c} for c in claims]
    if tolerations:
        spec["tolerations"] = tolerations
    return {"kind": "Pod", "metadata": {"name": name, "namespace": "cc"}, "spec": spec, "status": {"phase": phase}}


def device_class(name="cc-gpu", expression='device.driver == "gpu.example.com"'):
    return {"apiVersion": "resource.k8s.io/v1", "kind": "DeviceClass", "metadata": {"name": name}, "spec": {"selectors": [{"cel": {"expression": expression}}]}}


def claim(name, count=2, selectors=(), constraint="gpu.example.com/nvlinkIsland", namespace="cc", mode="ExactCount", allocation=None, cls="cc-gpu"):
    request = {"name": "gpus", "exactly": {"deviceClassName": cls, "allocationMode": mode, "count": count, "selectors": [{"cel": {"expression": s}} for s in selectors]}}
    devices = {"requests": [request]}
    if constraint:
        devices["constraints"] = [{"requests": ["gpus"], "matchAttribute": constraint}]
    obj = {"apiVersion": "resource.k8s.io/v1", "kind": "ResourceClaim", "metadata": {"name": name, "namespace": namespace}, "spec": {"devices": devices}}
    if allocation:
        obj["status"] = {"allocation": allocation}
    return obj


class ModelTests(unittest.TestCase):
    def test_cluster_loader_builds_islands_busy_gpus_and_mig_layouts(self):
        c = cluster(node("p", PAIRS, busy=[0, 2]), node("l4", {"model": "L4", "count": 2, "memory_gib": 24}), node("m", {"model": "A100-SXM4-80GB", "count": 1, "memory_gib": 80, "mig": [["3g.40gb", "2g.20gb", "1g.10gb@6"]]}))
        pairs = c.node("p")
        self.assertEqual([g.island for g in pairs.gpus], [0, 0, 1, 1, 2, 2, 3, 3])
        self.assertEqual([g.index for g in main.free_exclusive(pairs)], [1, 3, 4, 5, 6, 7])
        self.assertEqual([g.island for g in c.node("l4").gpus], [0, 1])
        self.assertEqual([i.label for i in c.node("m").gpus[0].mig], ["3g.40gb@0", "2g.20gb@4", "1g.10gb@6"])
        bad = [
            {"model": "A100-SXM4-80GB", "count": 1, "memory_gib": 80, "mig": [["3g.40gb@2"]]},
            {"model": "A100-SXM4-80GB", "count": 1, "memory_gib": 80, "mig": [["4g.40gb", "4g.40gb"]]},
            {"model": "A100-SXM4-80GB", "count": 1, "memory_gib": 80, "mig": [["5g.50gb"]]},
            {"model": "L4", "count": 1, "memory_gib": 24, "time_slicing": 4, "mig": [["1g.10gb"]]},
            {"model": "L4", "count": 2, "memory_gib": 24, "islands": [[0, 1], [1]]},
        ]
        for gpu in bad:
            with self.assertRaises(main.InputError):
                cluster(node("x", gpu))
        with self.assertRaises(main.InputError):
            main.load_cluster({"nodes": [{"name": "a"}, {"name": "a"}]})

    def test_mig_placement_rules_follow_the_a100_profile_table(self):
        profiles = main.A100_80GB_MIG

        def at(*labels):
            gpu = main.Gpu(0, 0)
            for label in labels:
                main.add_mig_instance(gpu, label, "test")
            return gpu.mig

        self.assertEqual(main.mig_start(at("4g.40gb@0"), profiles["3g.40gb"]), 4)
        self.assertIsNone(main.mig_start(at("1g.10gb@0", "1g.10gb@4"), profiles["3g.40gb"]))
        self.assertIsNone(main.mig_start(at("1g.10gb@2"), profiles["4g.40gb"]))
        self.assertEqual(main.mig_start(at("1g.10gb@4"), profiles["4g.40gb"]), 0)
        self.assertEqual(main.mig_start(at("1g.10gb@0"), profiles["2g.20gb"]), 2)
        self.assertIsNone(main.mig_start(at(*[f"1g.10gb@{i}" for i in range(7)]), profiles["1g.10gb"]))
        self.assertIsNone(main.mig_start(at("2g.20gb@0", "2g.20gb@2", "2g.20gb@4"), profiles["2g.20gb"]))
        self.assertEqual(main.mig_start(at("2g.20gb@0", "2g.20gb@2", "2g.20gb@4"), profiles["1g.10gb"]), 6)
        self.assertIsNone(main.mig_start(at("3g.40gb@0", "3g.40gb@4"), profiles["1g.10gb"]))
        self.assertEqual(main.mig_start([], profiles["7g.80gb"]), 0)

    def test_advertised_counts_are_what_the_device_plugin_reports(self):
        c = main.load_cluster(fixture("cluster.json"))
        self.assertEqual(main.advertised(c.node("dgx-a")), {"nvidia.com/gpu": 8})
        self.assertEqual(main.advertised(c.node("l4-a")), {"nvidia.com/gpu": 8})
        self.assertEqual(main.advertised(c.node("mig-a")), {"nvidia.com/mig-1g.10gb": 2, "nvidia.com/mig-2g.20gb": 1, "nvidia.com/mig-3g.40gb": 1})

    def test_pods_file_expands_count_and_rejects_impossible_requests(self):
        loaded = pods({"name": "nb", "count": 3, "memory_gib": 8}, {"name": "big", "gpus": 4, "nvlink": True})
        self.assertEqual([p.name for p in loaded], ["nb-0", "nb-1", "nb-2", "big"])
        self.assertEqual(loaded[3].gpus, 4)
        for bad in ({"name": "x", "gpus": 0}, {"name": "x", "mig": "9g.90gb"}, {"name": "x", "mig": "1g.10gb", "gpus": 2}, {"name": "x", "mig": "1g.10gb", "shared": True}, {"gpus": 1}):
            with self.assertRaises(main.InputError):
                pods(bad)


class PlacementTests(unittest.TestCase):
    def fixture_run(self, policy, view="gpu"):
        c = main.load_cluster(fixture("cluster.json"))
        decisions = main.place_all(c, main.load_pods(fixture("pods.json")), policy, view)
        return c, {d.pod.name: d for d in decisions}

    def test_binpack_fills_one_node_and_keeps_a_whole_node_for_the_8_gpu_job(self):
        c, decided = self.fixture_run("binpack")
        self.assertEqual({decided[f"notebook-{i}"].placement.node for i in range(6)}, {"dgx-a"})
        train = decided["train-8"].placement
        self.assertEqual((train.node, train.gpus, train.islands), ("dgx-b", list(range(8)), [0]))
        self.assertAlmostEqual(train.allreduce_s, 2 * 7 / 8 * 14 / 300)
        self.assertEqual(decided["finetune-2"].placement.gpus, [6, 7])
        self.assertTrue(all(d.placement and not d.placement.problems for d in decided.values()))

    def test_spread_leaves_every_node_partly_used_and_the_8_gpu_job_pending(self):
        c, decided = self.fixture_run("spread")
        self.assertEqual([decided[f"notebook-{i}"].placement.node for i in range(6)], ["dgx-a", "dgx-b", "pcie-a", "dgx-a", "dgx-b", "pcie-a"])
        train = decided["train-8"]
        self.assertIsNone(train.placement)
        self.assertEqual(train.message, "0/5 nodes are available: 1 GPU model is not A100-SXM4-80GB, 1 node is time-sliced, 3 Insufficient nvidia.com/gpu.")
        self.assertEqual(train.reasons["mig-a"], ["Insufficient nvidia.com/gpu"])
        report = main.fragmentation(c, [8])
        self.assertEqual(report["rows"][0]["by_count"], 0)
        self.assertEqual(report["rows"][0]["stranded_pct"], 100.0)

    def test_topology_puts_single_gpus_on_small_islands_and_keeps_nvlink_nodes_whole(self):
        c, decided = self.fixture_run("topology")
        self.assertEqual([decided[f"notebook-{i}"].placement.gpus for i in range(6)], [[0], [1], [2], [3], [4], [5]])
        self.assertEqual({decided[f"notebook-{i}"].placement.node for i in range(6)}, {"pcie-a"})
        self.assertEqual(decided["train-8"].placement.node, "dgx-a")
        self.assertEqual((decided["finetune-2"].placement.node, decided["finetune-2"].placement.islands), ("pcie-a", [3]))
        self.assertEqual(main.fragmentation(c)["largest_in_island"], 7)

    def test_nvlink_required_rejects_free_gpus_in_different_islands(self):
        c = cluster(node("p", PAIRS, busy=[0, 2, 4, 6]))
        pair = pods({"name": "pair", "gpus": 2, "nvlink": True, "gradient_gb": 14})[0]
        decision = main.schedule(c, pair)
        self.assertIsNone(decision.placement)
        self.assertEqual(decision.message, "0/1 nodes are available: 1 no NVLink island with 2 free GPUs.")
        loose = pods({"name": "loose", "gpus": 2, "gradient_gb": 14})[0]
        placed = main.schedule(c, loose).placement
        self.assertEqual((placed.gpus, placed.islands, placed.link_gbps), ([1, 3], [0, 1], 32.0))
        self.assertAlmostEqual(placed.allreduce_s, 14 / 32)
        extended = main.schedule(cluster(node("p", PAIRS, busy=[0, 2, 4, 6])), pair, view="extended").placement
        self.assertEqual(extended.gpus, [1, 3])
        self.assertIn("spans NVLink islands: all-reduce over PCIe at 32 GB/s instead of NVLink at 300 GB/s", extended.problems)

    def test_gpu_view_filters_model_memory_and_time_slicing_with_one_reason_per_node(self):
        c = main.load_cluster(fixture("cluster.json"))
        big = pods({"name": "big", "gpus": 1, "memory_gib": 90})[0]
        self.assertEqual(main.schedule(c, big).message, "0/5 nodes are available: 1 node is time-sliced, 4 GPU memory below 90 GiB.")
        shared = pods({"name": "s", "shared": True, "memory_gib": 4})[0]
        self.assertEqual(main.schedule(c, shared, apply=False).placement.node, "l4-a")
        sxm = pods({"name": "sxm", "model": "A100-SXM4-80GB"})[0]
        decision = main.schedule(c, sxm, apply=False)
        self.assertEqual(decision.reasons["pcie-a"], ["GPU model is not A100-SXM4-80GB"])
        self.assertEqual(decision.reasons["l4-a"], ["node is time-sliced"])

    def test_extended_view_sees_only_counts_and_reports_what_goes_wrong_at_runtime(self):
        c, decided = self.fixture_run("binpack", view="extended")
        embed = decided["embed"].placement
        self.assertEqual(embed.node, "l4-a")
        self.assertIn("needs 40 GiB per GPU, L4 has 24 GiB: out of memory when the weights load", embed.problems)
        self.assertIn("lands on a time-sliced replica: shares a physical GPU, no memory or fault isolation", embed.problems)
        self.assertEqual(decided["chat-2"].placement.problems, ["GPU 0 memory oversubscribed: 70 of 24 GiB declared"])
        shared = [s for s in main.fragmentation(c)["shared"] if s["gpu"] == 0][0]
        self.assertEqual((shared["replicas_used"], shared["oversubscribed"]), (4, True))
        two = main.schedule(cluster(node("l4", {"model": "L4", "count": 2, "memory_gib": 24, "time_slicing": 4})), pods({"name": "t", "gpus": 2, "memory_gib": 8})[0], view="extended").placement
        self.assertEqual(two.gpus, [0, 0])
        self.assertIn("2 replicas on 1 physical GPU(s): no extra compute", two.problems)
        with self.assertRaises(main.InputError):
            main.schedule(c, pods({"name": "x"})[0], "topology", "extended")

    def test_time_sliced_gpu_view_keeps_declared_memory_within_each_gpu(self):
        c = cluster(node("l4", {"model": "L4", "count": 2, "memory_gib": 24, "time_slicing": 4}))
        decided = main.place_all(c, pods({"name": "chat", "count": 5, "shared": True, "memory_gib": 10}))
        self.assertEqual([d.placement.gpus for d in decided[:4]], [[0], [0], [1], [1]])
        self.assertIsNone(decided[4].placement)
        self.assertEqual(decided[4].message, "0/1 nodes are available: 1 Insufficient nvidia.com/gpu.")
        self.assertEqual([g.declared_gib for g in c.node("l4").gpus], [20, 20])

    def test_mig_pod_takes_a_free_instance_of_its_profile_and_nothing_else(self):
        c = main.load_cluster(fixture("cluster.json"))
        decided = main.place_all(c, pods({"name": "r", "mig": "3g.40gb"}, {"name": "r2", "mig": "3g.40gb"}, {"name": "s", "mig": "1g.10gb", "count": 2}))
        self.assertEqual((decided[0].placement.node, decided[0].placement.mig_start, decided[0].placement.resource), ("mig-a", 4, "nvidia.com/mig-3g.40gb"))
        self.assertEqual(decided[1].message, "0/5 nodes are available: 5 Insufficient nvidia.com/mig-3g.40gb.")
        self.assertEqual([d.placement.mig_start for d in decided[2:]], [0, 1])
        report = main.mig_report(c.node("mig-a"), c.node("mig-a").gpus[0])
        self.assertEqual(report["free"], {"2g.20gb": 1})

    def test_allreduce_estimate_is_the_ring_formula(self):
        self.assertAlmostEqual(main.allreduce_seconds(8, 14, 300), 0.0816666, places=5)
        self.assertAlmostEqual(main.allreduce_seconds(4, 14, 32), 0.65625)
        self.assertEqual(main.allreduce_seconds(1, 14, 300), 0.0)
        self.assertEqual(main.allreduce_seconds(4, 0, 300), 0.0)

    def test_fit_error_is_a_sorted_histogram_like_kube_scheduler(self):
        self.assertEqual(main.fit_error(3, [["b"], ["a"], ["b"]]), "0/3 nodes are available: 1 a, 2 b.")
        self.assertEqual(main.fit_error(12, [["x"]] * 10 + [["y"]] * 2), "0/12 nodes are available: 10 x, 2 y.")
        self.assertEqual(main.fit_error(1, [["Insufficient a", "Insufficient b"]]), "0/1 nodes are available: 1 Insufficient a, 1 Insufficient b.")


class FragmentationTests(unittest.TestCase):
    def test_fragmentation_counts_placeable_jobs_by_node_and_inside_islands(self):
        report = main.fragmentation(main.load_cluster(fixture("fragmented.json")))
        rows = {r["size"]: r for r in report["rows"]}
        self.assertEqual(report["free"], 8)
        self.assertEqual((report["largest_on_node"], report["largest_in_island"]), (4, 2))
        self.assertEqual((rows[2]["by_count"], rows[2]["by_island"]), (4, 2))
        self.assertEqual((rows[4]["by_count"], rows[4]["by_island"], rows[4]["stranded"], rows[4]["stranded_pct"]), (1, 0, 4, 50.0))
        self.assertEqual(rows[8]["stranded_pct"], 100.0)
        self.assertEqual(report["free_per_node"]["pcie-a"], [1, 3, 5, 7])

    def test_mig_fragmentation_shows_what_a_repartition_could_still_add(self):
        report = main.fragmentation(main.load_cluster(fixture("fragmented.json")))["mig"][0]
        self.assertEqual(report["used"], ["1g.10gb@0=chat-0", "1g.10gb@4=chat-1"])
        self.assertEqual(report["free"], {"1g.10gb": 3, "2g.20gb": 1})
        self.assertEqual(report["repartition_could_add"], ["1g.10gb", "2g.20gb"])


class GangTests(unittest.TestCase):
    def simulate(self, mode, timeout=0.0, jobs=None):
        c = main.load_cluster(fixture("gang-cluster.json"))
        return main.simulate_gang(c, main.load_jobs(jobs or fixture("jobs.json")), mode, timeout)

    def test_pod_by_pod_scheduling_deadlocks_two_training_jobs(self):
        result = self.simulate("pod")
        self.assertEqual(result.outcome, "deadlock")
        held = {s.job.name: (len(s.placed), s.held_gpus) for s in result.jobs if s.start is None}
        self.assertEqual(held, {"llm-a": (2, 8), "llm-b": (2, 8)})
        self.assertAlmostEqual(result.idle_gpu_seconds, 8 * 1190)
        self.assertIn("t=10 llm-a holds 4 GPUs with 1/4 pods placed, waiting", result.events)

    def test_gang_all_or_nothing_runs_the_jobs_one_after_another(self):
        result = self.simulate("gang")
        self.assertEqual(result.outcome, "finished")
        times = {s.job.name: (s.start, s.finish) for s in result.jobs}
        self.assertEqual(times, {"etl": (0, 1200), "llm-a": (1200, 4800), "llm-b": (4800, 6600)})
        self.assertEqual((result.makespan, result.idle_gpu_seconds), (6600, 0))

    def test_gang_timeout_releases_held_gpus_and_the_hold_costs_idle_gpu_hours(self):
        result = self.simulate("gang", timeout=300)
        self.assertEqual(result.outcome, "finished")
        self.assertIn("t=310 llm-a timed out with 2/4 pods, releases 8 GPUs", result.events)
        self.assertEqual({s.job.name: s.timeouts for s in result.jobs}, {"etl": 0, "llm-a": 2, "llm-b": 1})
        self.assertAlmostEqual(result.idle_gpu_seconds, 8 * 1190)
        self.assertEqual(result.makespan, 6600)

    def test_a_gang_larger_than_the_cluster_is_stuck_not_deadlocked(self):
        result = self.simulate("gang", jobs=[{"name": "huge", "pods": 3, "gpus_per_pod": 8, "duration": 60}])
        self.assertEqual(result.outcome, "stuck")
        self.assertIn("huge needs 3 x 8 GPUs at once and never fits", result.detail)
        with self.assertRaises(main.InputError):
            main.load_jobs([{"name": "x", "pods": 1}])


class CelTests(unittest.TestCase):
    def device(self, **attributes):
        return main.Device("gpu.example.com", "n1", "n1", "gpu-0", {f"gpu.example.com/{k}": v for k, v in attributes.items()}, {"gpu.example.com/memory": main.parse_quantity("80Gi")})

    def test_cel_subset_reads_driver_attributes_and_capacity(self):
        d = self.device(model="A100-SXM4-80GB", index=3, timeSliced=False)
        cases = {
            'device.driver == "gpu.example.com"': True,
            "device.attributes['gpu.example.com'].model == 'A100-SXM4-80GB'": True,
            'device.attributes["gpu.example.com"].index >= 2 && device.attributes["gpu.example.com"].index < 4': True,
            'device.attributes["gpu.example.com"].model.startsWith("H100") || !device.attributes["gpu.example.com"].timeSliced': True,
            'device.capacity["gpu.example.com"].memory.compareTo(quantity("40Gi")) >= 0': True,
            'device.capacity["gpu.example.com"].memory.isGreaterThan(quantity("80Gi"))': False,
            'device.attributes["gpu.example.com"]["model"].contains("SXM")': True,
            '(device.attributes["gpu.example.com"].index == 1 || device.attributes["gpu.example.com"].index == 3) && true': True,
        }
        for expression, expected in cases.items():
            self.assertIs(main.selector_matches(expression, d), expected, expression)

    def test_cel_runtime_errors_do_not_match_and_short_circuit_like_cel(self):
        d = self.device(model="L4")
        self.assertFalse(main.selector_matches('device.attributes["gpu.example.com"].missing == "x"', d))
        self.assertFalse(main.selector_matches('device.attributes["gpu.example.com"].model == 3', d))
        self.assertTrue(main.selector_matches('device.attributes["gpu.example.com"].missing == "x" || device.driver == "gpu.example.com"', d))
        self.assertFalse(main.selector_matches('device.driver == "other" && device.attributes["gpu.example.com"].missing == "x"', d))
        self.assertFalse(main.selector_matches('device.capacity["gpu.example.com"].memory >= quantity("1Gi")', d))
        for broken in ('device.driver ==', 'device.driver = "x"', 'device.attributes["x"', "device.driver == 'x' ^"):
            with self.assertRaises(main.InputError):
                main.compile_cel(broken)


class DraTests(unittest.TestCase):
    def test_slices_round_trip_from_the_cluster_model_in_v1_and_v1beta1_shapes(self):
        c = main.load_cluster(fixture("cluster.json"))
        slices = main.cluster_slices(c)
        self.assertEqual([s["spec"]["nodeName"] for s in slices], ["dgx-a", "dgx-b", "pcie-a", "l4-a", "mig-a"])
        pcie = slices[2]["spec"]["devices"][3]
        self.assertEqual((pcie["name"], pcie["attributes"]["nvlinkIsland"], pcie["capacity"]["memory"]), ("gpu-3", {"int": 1}, {"value": "80Gi"}))
        devices = main.devices_from_slice(slices[4])
        self.assertEqual([d.name for d in devices], ["gpu-0-mig-1g-10gb-0", "gpu-0-mig-1g-10gb-1", "gpu-0-mig-2g-20gb-2", "gpu-0-mig-3g-40gb-4"])
        self.assertEqual(devices[3].capacity["gpu.example.com/memory"], 40 * 2**30)
        beta = main.cluster_slices(c, api_version="resource.k8s.io/v1beta1")[0]
        self.assertIn("basic", beta["spec"]["devices"][0])
        self.assertEqual(main.devices_from_slice(beta)[0].attributes["gpu.example.com/model"], "A100-SXM4-80GB")

    def test_allocation_honours_class_and_request_selectors_constraints_and_devices_in_use(self):
        c = main.load_cluster(fixture("cluster.json"))
        world = main.load_dra([fixture("dra.json"), {"items": main.cluster_slices(c)}])
        for default in main.default_classes():
            world.classes[default.name] = default
        reports = {r["claim"]: r for r in main.allocate_claims(world)}
        self.assertEqual(reports["ml/big-sxm"]["allocation"]["devices"]["results"][0]["device"], "gpu-0")
        self.assertEqual(reports["ml/big-sxm"]["node"], "dgx-a")
        pair = reports["ml/nvlink-pair"]["allocation"]["devices"]["results"]
        self.assertEqual([r["device"] for r in pair], ["gpu-0", "gpu-1"])
        self.assertEqual(reports["ml/rerank-mig"]["allocation"]["devices"]["results"][0]["device"], "gpu-0-mig-3g-40gb-4")
        self.assertEqual(reports["ml/h200"]["why"], "cannot allocate all claims: no device on any node matches the DeviceClass and the selectors")
        interleaved = main.load_cluster({"nodes": [node("n", {"model": "A100-PCIE-80GB", "count": 4, "memory_gib": 80, "islands": [[0, 2], [1, 3]], "nvlink_gbps": 300})]})
        world = main.load_dra([device_class(), claim("pair"), claim("second"), claim("third"), {"items": main.cluster_slices(interleaved)}])
        results = [[r["device"] for r in report["allocation"]["devices"]["results"]] if report["allocated"] else None for report in main.allocate_claims(world)]
        self.assertEqual(results, [["gpu-0", "gpu-2"], ["gpu-1", "gpu-3"], None])

    def test_allocation_mode_all_already_allocated_claims_and_a_missing_class(self):
        two = main.load_cluster({"nodes": [node("n", {"model": "L4", "count": 2, "memory_gib": 24})]})
        slices = {"items": main.cluster_slices(two)}
        taken = {"devices": {"results": [{"request": "gpus", "driver": "gpu.example.com", "pool": "n", "device": "gpu-1"}]}}
        world = main.load_dra([device_class(), claim("old", count=1, constraint="", allocation=taken), claim("all", mode="All", constraint=""), slices])
        reports = main.allocate_claims(world)
        self.assertTrue(reports[0]["already"])
        self.assertFalse(reports[1]["allocated"])
        world = main.load_dra([device_class(), claim("all", mode="All", constraint=""), slices])
        self.assertEqual([r["device"] for r in main.allocate_claims(world)[0]["allocation"]["devices"]["results"]], ["gpu-0", "gpu-1"])
        world = main.load_dra([claim("x", cls="nope"), slices])
        with self.assertRaises(main.InputError):
            main.allocate_claims(world)

    def test_claims_parse_from_v1_and_v1beta1_shapes_and_templates(self):
        v1 = main.make_claim(claim("a", count=3, selectors=['device.driver == "x"']))
        self.assertEqual((v1.requests[0].device_class, v1.requests[0].count, v1.requests[0].selectors, v1.constraints), ("cc-gpu", 3, ['device.driver == "x"'], [(["gpus"], "gpu.example.com/nvlinkIsland")]))
        beta = main.make_claim({"kind": "ResourceClaim", "metadata": {"name": "b", "namespace": "ml"}, "spec": {"devices": {"requests": [{"name": "g", "deviceClassName": "gpu.example.com", "allocationMode": "ExactCount", "count": 2}]}}})
        self.assertEqual((beta.key, beta.requests[0].device_class, beta.requests[0].count), ("ml/b", "gpu.example.com", 2))
        template = main.make_claim({"kind": "ResourceClaimTemplate", "metadata": {"name": "t", "namespace": "ml"}, "spec": {"spec": {"devices": {"requests": [{"name": "g", "exactly": {"deviceClassName": "c"}}]}}}})
        self.assertEqual((template.requests[0].device_class, template.requests[0].count), ("c", 1))
        allocated = main.make_claim(claim("c", allocation={"devices": {"results": [{"device": "gpu-0"}]}, "nodeSelector": {"nodeSelectorTerms": [{"matchFields": [{"key": "metadata.name", "operator": "In", "values": ["kind-1"]}]}]}}))
        self.assertEqual((allocated.allocated, allocated.node), (True, "kind-1"))
        with self.assertRaises(main.InputError):
            main.make_claim({"kind": "ResourceClaim", "metadata": {"name": "f"}, "spec": {"devices": {"requests": [{"name": "g", "firstAvailable": []}]}}})


class KubernetesTests(unittest.TestCase):
    def test_extended_resources_count_bound_pods_and_print_kube_scheduler_wording(self):
        nodes = [k8s_node("kind-1", gpus=4)]
        bound = [k8s_pod("a", 2, on="kind-1"), k8s_pod("b", 1, on="kind-1", use="requests"), k8s_pod("done", 4, on="kind-1", phase="Succeeded")]
        result = main.k8s_predict(nodes, bound, k8s_pod("c", 2), main.load_dra([]))
        self.assertFalse(result["fits"])
        self.assertEqual(result["message"], "0/1 nodes are available: 1 Insufficient example.com/gpu.")
        self.assertEqual(result["nodes"][0]["detail"], "example.com/gpu: requested 2, free 1 of 4")
        self.assertTrue(main.k8s_predict(nodes, bound, k8s_pod("d", 1), main.load_dra([]))["fits"])
        self.assertEqual(main.k8s_predict([k8s_node("cpu-only")], [], k8s_pod("c", 1), main.load_dra([]))["message"], "0/1 nodes are available: 1 Insufficient example.com/gpu.")
        self.assertEqual(main.pod_extended({"containers": [{"resources": {"limits": {"nvidia.com/gpu": "1", "cpu": "2", "hugepages-2Mi": "1Gi"}}}], "initContainers": [{"resources": {"limits": {"nvidia.com/gpu": "2"}}}]}), {"nvidia.com/gpu": 2})

    def test_cordons_and_taints_answer_before_the_count(self):
        nodes = [k8s_node("cp", gpus=8, taints=[{"key": "node-role.kubernetes.io/control-plane", "effect": "NoSchedule"}]), k8s_node("w1", gpus=8, unschedulable=True), k8s_node("w2", gpus=1)]
        result = main.k8s_predict(nodes, [], k8s_pod("c", 2), main.load_dra([]))
        self.assertEqual(result["message"], "0/3 nodes are available: 1 Insufficient example.com/gpu, 1 node(s) had untolerated taint(s), 1 node(s) were unschedulable.")
        tolerant = k8s_pod("c", 2, tolerations=[{"key": "node-role.kubernetes.io/control-plane", "operator": "Exists", "effect": "NoSchedule"}])
        self.assertEqual(main.k8s_predict(nodes, [], tolerant, main.load_dra([]))["node"], "cp")

    def test_a_claim_without_a_driver_stays_unallocatable_until_slices_appear(self):
        nodes = [k8s_node("kind-1")]
        pod = k8s_pod("dra", claims=["pair"])
        empty = main.load_dra([device_class(), claim("pair")])
        result = main.k8s_predict(nodes, [], pod, empty)
        self.assertEqual(result["message"], "0/1 nodes are available: 1 cannot allocate all claims.")
        self.assertIn("0 device(s) match class cc-gpu", result["nodes"][0]["detail"])
        model = main.load_cluster({"nodes": [node("kind-1", {"model": "A100-PCIE-80GB", "count": 4, "memory_gib": 80, "islands": [[0, 2], [1, 3]], "nvlink_gbps": 300})]})
        published = main.load_dra([device_class(), claim("pair"), {"items": main.cluster_slices(model)}])
        result = main.k8s_predict(nodes, [], pod, published)
        self.assertTrue(result["fits"])
        allocation = result["nodes"][0]["allocations"][0]["allocation"]
        self.assertEqual([r["device"] for r in allocation["devices"]["results"]], ["gpu-0", "gpu-2"])
        self.assertEqual(allocation["nodeSelector"]["nodeSelectorTerms"][0]["matchFields"][0]["values"], ["kind-1"])
        with self.assertRaises(main.InputError):
            main.k8s_predict(nodes, [], k8s_pod("dra", claims=["missing"]), published)

    def test_an_allocated_claim_pins_the_pod_to_its_node(self):
        nodes = [k8s_node("kind-1"), k8s_node("kind-2")]
        pinned = {"devices": {"results": [{"request": "gpus", "driver": "gpu.example.com", "pool": "kind-2", "device": "gpu-0"}]}, "nodeSelector": {"nodeSelectorTerms": [{"matchFields": [{"key": "metadata.name", "operator": "In", "values": ["kind-2"]}]}]}}
        world = main.load_dra([device_class(), claim("pair", allocation=pinned)])
        result = main.k8s_predict(nodes, [], k8s_pod("dra", claims=["pair"]), world)
        self.assertEqual(result["node"], "kind-2")
        self.assertEqual(result["nodes"][0]["reasons"], ["resourceclaim not available on the node"])


class CliTests(unittest.TestCase):
    def test_cli_exit_codes_text_and_json(self):
        cluster_file, pods_file = os.path.join(FIXTURES, "cluster.json"), os.path.join(FIXTURES, "pods.json")
        code, out, _ = run(["place", cluster_file, pods_file])
        self.assertEqual(code, 0)
        self.assertIn("train-8        -> dgx-b", out)
        code, out, _ = run(["place", cluster_file, pods_file, "--policy", "spread", "--json"])
        self.assertEqual(code, 1)
        doc = json.loads(out)
        self.assertFalse([d for d in doc["decisions"] if d["pod"] == "train-8"][0]["scheduled"])
        code, out, _ = run(["gang", os.path.join(FIXTURES, "gang-cluster.json"), os.path.join(FIXTURES, "jobs.json"), "--mode", "pod", "--json"])
        self.assertEqual((code, json.loads(out)["outcome"]), (1, "deadlock"))
        code, out, _ = run(["dra", "--cluster", cluster_file, "--objects", os.path.join(FIXTURES, "dra.json")])
        self.assertEqual(code, 1)
        self.assertIn("claim ml/nvlink-pair: allocated on pcie-a: pcie-a/gpu-0, pcie-a/gpu-1", out)
        code, out, _ = run(["slices", cluster_file])
        self.assertEqual((code, len(json.loads(out)["items"])), (0, 5))
        code, out, _ = run(["frag", os.path.join(FIXTURES, "fragmented.json"), "--json", "--sizes", "2"])
        self.assertEqual(json.loads(out)["rows"][0]["by_island"], 2)
        with tempfile.TemporaryDirectory() as tmp:
            broken = os.path.join(tmp, "broken.json")
            with open(broken, "w", encoding="utf-8") as handle:
                handle.write("{not json")
            code, _, err = run(["place", broken, pods_file])
            self.assertEqual(code, 2)
            self.assertIn("is not JSON", err)
            code, _, err = run(["frag", cluster_file, "--sizes", "two"])
            self.assertEqual(code, 2)


if __name__ == "__main__":
    unittest.main()
