import json
import math
import os
import sys
import tempfile
import threading
import unittest
from contextlib import redirect_stderr, redirect_stdout
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import StringIO

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import main  # noqa: E402

FIXTURES = os.path.join(os.path.dirname(__file__), "..", "fixtures")


def fixture(name):
    with open(os.path.join(FIXTURES, name), encoding="utf-8") as handle:
        return json.load(handle)


def run_doc(doc, world=None):
    world = world or main.SimulatedWorld()
    return main.run_experiment(main.load_experiment(doc, FIXTURES), world), world


def cli(argv):
    out, err = StringIO(), StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = main.main(argv)
    return code, out.getvalue(), err.getvalue()


def commands(world):
    return [" ".join(c) for c in world.commands]


class Server:
    def __init__(self, handler):
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.httpd.daemon_threads = True
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"

    def __enter__(self):
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        return self

    def __exit__(self, *exc):
        self.httpd.shutdown()
        self.httpd.server_close()


class Hello(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Length", "3")
        self.end_headers()
        self.wfile.write(b"ok\n")

    def log_message(self, *args):
        return


class ExpositionTests(unittest.TestCase):
    def test_labels_escapes_special_values_comments_and_exemplars_parse(self):
        text = (
            "# HELP x_total requests\n# TYPE x_total counter\n"
            'x_total{path="/a,b}",msg="say \\"hi\\"\\n"} 3 1700000000000\n'
            "up NaN\n"
            'gauge{a="1"} +Inf\n'
            'h_bucket{le="0.1"} 2 # {trace_id="abc"} 0.05\n'
            "# EOF\n"
        )
        samples = main.parse_exposition(text)
        self.assertEqual([s.name for s in samples], ["x_total", "up", "gauge", "h_bucket"])
        self.assertEqual(dict(samples[0].labels), {"path": "/a,b}", "msg": 'say "hi"\n'})
        self.assertEqual(samples[0].value, 3)
        self.assertTrue(math.isnan(samples[1].value))
        self.assertEqual(samples[2].value, math.inf)
        self.assertEqual(samples[3].value, 2)

    def test_broken_lines_name_the_source_and_line(self):
        for text, fragment in (
            ("ok 1\nx{a=1} 2\n", "src:2"),
            ("9bad 1\n", "does not start with a metric name"),
            ("x 1 2 3\n", "expected a value"),
            ('x{a="1 2\n', "no closing quote"),
            ('x{a="1" 2\n', 'expected label="value"'),
            ("x one\n", "is not a number"),
        ):
            with self.assertRaises(main.InputError) as caught:
                main.parse_exposition(text, "src")
            self.assertIn(fragment, str(caught.exception))


class SelectorTests(unittest.TestCase):
    def sample(self, **labels):
        return main.Sample("http_requests_total", tuple(sorted(labels.items())), 1.0)

    def test_matchers_behave_like_promql(self):
        selector = main.parse_selector('http_requests_total{code=~"5..", path!="/healthz", region=""}')
        self.assertTrue(selector.matches(self.sample(code="503", path="/api")))
        self.assertFalse(selector.matches(self.sample(code="1500", path="/api")))
        self.assertFalse(selector.matches(self.sample(code="503", path="/healthz")))
        self.assertFalse(selector.matches(self.sample(code="503", path="/api", region="eu")))
        self.assertTrue(main.parse_selector('http_requests_total{code!~"2.."}').matches(self.sample(code="404")))
        self.assertFalse(main.parse_selector("other_total").matches(self.sample(code="200")))
        self.assertEqual(main.parse_selector('x{a="b",}').text(), 'x{a="b"}')

    def test_bad_selectors_are_input_errors(self):
        for text in ("", "x{a=1}", 'x{a="1" b="2"}', 'x{a=~"("}', "{a=\"1\"}"):
            with self.assertRaises(main.InputError):
                main.parse_selector(text)


class SliMathTests(unittest.TestCase):
    def test_counter_increase_counts_resets_and_new_series(self):
        before = {(("a", "1"),): 10.0, (("a", "2"),): 5.0}
        after = {(("a", "1"),): 12.0, (("a", "2"),): 2.0, (("a", "3"),): 4.0}
        self.assertEqual(main.counter_increase(before, after), 2 + 2 + 4)

    def test_bucket_quantile_interpolates_inside_a_bucket_and_caps_at_the_highest_finite_bound(self):
        buckets = [(0.1, 50.0), (0.5, 90.0), (1.0, 100.0), (math.inf, 100.0)]
        self.assertAlmostEqual(main.bucket_quantile(0.5, buckets), 0.1)
        self.assertAlmostEqual(main.bucket_quantile(0.95, buckets), 0.75)
        self.assertEqual(main.bucket_quantile(0.99, [(0.1, 50.0), (0.5, 90.0), (1.0, 100.0), (math.inf, 120.0)]), 1.0)
        self.assertAlmostEqual(main.bucket_quantile(0.5, [(0.1, 10.0), (0.5, 8.0), (math.inf, 20.0)]), 0.1)
        self.assertIsNone(main.bucket_quantile(0.95, []))
        self.assertIsNone(main.bucket_quantile(0.95, [(0.1, 0.0), (math.inf, 0.0)]))

    def test_slis_come_from_the_difference_between_two_scrapes(self):
        stats = main.LoadStats()
        stats.observe("ok", 0.02, 900)
        stats.observe("error", 0.001, 100)
        first = main.parse_exposition(stats.exposition())
        stats.observe("ok", 0.02, 85)
        stats.observe("ok", 0.4, 10)
        stats.observe("error", 0.001, 5)
        second = main.parse_exposition(stats.exposition())
        errors = main.make_sli({"name": "e", "type": "error_ratio", "errors": 'minichaos_load_requests_total{outcome="error"}', "total": "minichaos_load_requests_total", "max": 0.1})
        p95 = main.make_sli({"name": "p", "type": "latency_quantile", "histogram": 'minichaos_load_request_duration_seconds{outcome="ok"}', "quantile": 0.95, "max": 0.3})
        self.assertEqual(errors.evaluate(first, second), (0.05, 100.0))
        value, requests = p95.evaluate(first, second)
        self.assertEqual(requests, 95.0)
        self.assertAlmostEqual(value, 0.25 + 0.25 * (90.25 - 85) / 10)
        self.assertEqual(errors.evaluate(second, second), (None, 0.0))


class BlastRadiusTests(unittest.TestCase):
    def test_selection_rounds_up_like_chaos_mesh_and_the_limit_rounds_down(self):
        self.assertEqual(main.selection_size("fixed-percent", 34, 3), 2)
        self.assertEqual(main.selection_size("fixed-percent", 30, 10), 3)
        self.assertEqual(main.selection_size("random-max-percent", 50, 3, worst_case=True), 2)
        self.assertEqual(main.selection_size("fixed", 5, 3), 3)
        self.assertEqual(main.selection_size("one", 1, 3), 1)
        self.assertEqual(main.selection_size("all", 1, 3), 3)
        blast = main.BlastRadius(["ns"], [], max_percent=34)
        self.assertEqual(main.allowed_count(3, blast), 1)
        self.assertEqual(main.allowed_count(1, main.BlastRadius(["ns"], [], max_percent=50)), 0)
        self.assertEqual(main.allowed_count(10, main.BlastRadius(["ns"], [], max_percent=50, max_pods=2)), 2)
        with self.assertRaises(main.InputError):
            main.selection_size("fixed-percent", 0, 3)

    def test_static_refusals_name_every_reason_before_anything_runs(self):
        doc = fixture("pod-delete.json")
        doc["method"]["duration_s"] = 600
        doc["abort"]["rules"] = []
        doc["method"]["faults"] = [
            {"type": "pod-delete", "namespace": "kube-system", "selector": "k8s-app=kube-dns"},
            {"type": "pod-delete", "namespace": "prod", "selector": "app=web", "mode": "all"},
            {"type": "netem", "device": "eth0", "delay_ms": 100},
            {"type": "netem", "netns": "other", "device": "veth0", "loss_percent": 1},
            {"type": "process-kill", "match": "py"},
            {"type": "cpu-burn", "workers": 8},
        ]
        reasons = " | ".join(main.static_refusals(main.load_experiment(doc, FIXTURES)))
        for fragment in ("max_duration_s", "abort.rules is empty", "kube-system runs cluster add-ons", "namespace prod is not in", "mode all affects 100%", "shapes the host's own eth0", "netns other is not in", "shorter than 4 characters", "max_workers is 1"):
            self.assertIn(fragment, reasons)

    def test_rounding_up_past_the_blast_radius_is_refused_without_touching_anything(self):
        journal, world = run_doc(fixture("wide-pod-delete.json"))
        self.assertEqual((journal.verdict, journal.exit_code), ("refused", 2))
        self.assertIn("would affect 2 of 3 pods", journal.reason)
        self.assertFalse(any("delete pod" in c for c in commands(world)))

    def test_a_single_replica_has_nothing_to_spare(self):
        journal, world = run_doc(fixture("pod-delete.json"), main.SimulatedWorld(replicas=1))
        self.assertEqual(journal.verdict, "refused")
        self.assertIn("allows 0", journal.reason)


class FaultTests(unittest.TestCase):
    def test_netem_runs_inside_the_namespace_and_refuses_a_configured_root_qdisc(self):
        fault = main.Netem({"netns": "lab", "device": "veth-s", "delay_ms": 200, "jitter_ms": 20, "loss_percent": 5})
        self.assertEqual(fault.add_command(), ["ip", "netns", "exec", "lab", "tc", "qdisc", "add", "dev", "veth-s", "root", "netem", "delay", "200ms", "20ms", "loss", "5%"])
        self.assertEqual(fault.delete_command(), ["ip", "netns", "exec", "lab", "tc", "qdisc", "del", "dev", "veth-s", "root"])

        class Shaped(main.SimulatedWorld):
            def run(self, argv):
                return 0, "qdisc htb 1: root refcnt 2 r2q 10 default 0x10\nqdisc fq_codel 0: parent 1:10\n", ""

        with self.assertRaises(main.Refused) as caught:
            fault.plan(Shaped(), main.BlastRadius([], ["lab"]))
        self.assertIn("htb 1:", str(caught.exception))
        for bad in ({"device": "x"}, {"device": "x", "loss_percent": 120}, {"device": "x", "jitter_ms": 5, "loss_percent": 1}):
            with self.assertRaises(main.InputError):
                main.Netem(bad)

    def test_process_kill_reads_proc_and_never_targets_its_own_process_tree(self):
        with tempfile.TemporaryDirectory() as root:
            for pid, cmdline in (("1", b"/sbin/init\0"), ("200", b"cc-chaos-decoy\x00300\x00"), ("300", b""), ("999", b"python3\x00main.py\x00run\x00cc-chaos-decoy.json\x00")):
                os.makedirs(os.path.join(root, pid))
                with open(os.path.join(root, pid, "cmdline"), "wb") as handle:
                    handle.write(cmdline)
            os.makedirs(os.path.join(root, "self"))
            world = main.RealWorld(proc_root=root)
            self.assertEqual(world.processes(), [(1, "/sbin/init"), (200, "cc-chaos-decoy 300"), (999, "python3 main.py run cc-chaos-decoy.json")])
            world.protected = {1, 999}
            killed = []
            world.kill = lambda pid, sig: killed.append((pid, sig))
            fault = main.ProcessKill({"match": "cc-chaos-decoy", "signal": "SIGKILL"})
            fault.start(world, main.BlastRadius([], []), 10)
            self.assertEqual(killed, [(200, main.signal.SIGKILL)])
            world.protected = {1}
            with self.assertRaises(main.Refused) as caught:
                fault.plan(world, main.BlastRadius([], [], max_processes=1))
            self.assertIn("2 processes match", str(caught.exception))
        with self.assertRaises(main.FaultError):
            main.read_processes(os.path.join(root, "gone"))

    def test_cpu_burn_workers_expire_on_their_own_and_stop_on_rollback(self):
        world = main.SimulatedWorld()
        fault = main.CpuBurn({"workers": 2})
        fault.start(world, main.BlastRadius([], [], max_workers=2), 20)
        self.assertEqual(len(world.burners), 2)
        self.assertEqual(world.burners[0].argv[-3:], ["burn", "--seconds", "50"])
        self.assertIn("stopped 2", fault.revert(world))
        self.assertTrue(all(p.poll() is not None for p in world.burners))

    def test_manifest_blast_radius_reads_the_chaos_mesh_selector(self):
        fault = main.Manifest({"path": "podchaos.json"}, FIXTURES)
        self.assertEqual(fault.namespaces, ["cc-chaos-experiments"])
        self.assertIn("not in blast_radius.namespaces", " ".join(fault.refusals(main.BlastRadius(["elsewhere"], []))))
        self.assertEqual(fault.refusals(main.BlastRadius(["cc-chaos-experiments"], [], max_percent=34)), [])
        self.assertIn("at most 1 of 3 pods", fault.plan(main.SimulatedWorld(), main.BlastRadius(["cc-chaos-experiments"], [], max_percent=34))[0])
        fault.doc["spec"]["mode"] = "all"
        self.assertIn("mode all affects 100%", " ".join(fault.refusals(main.BlastRadius(["cc-chaos-experiments"], [], max_percent=34))))


class ExperimentTests(unittest.TestCase):
    def test_pod_delete_hypothesis_holds_and_the_deployment_recovers(self):
        journal, world = run_doc(fixture("pod-delete.json"))
        self.assertEqual((journal.verdict, journal.exit_code), ("held", 0))
        deletes = [c for c in commands(world) if c.startswith("kubectl delete pod")]
        self.assertEqual(len(deletes), 1)
        self.assertTrue(deletes[0].endswith("--wait=false"))
        self.assertEqual(len(world.pods), 3)
        after = [m for m in journal.measurements if m.phase == "after"][-1]
        self.assertEqual([c.detail for c in after.checks], ["deployment cc-chaos-experiments/web 3/3 ready"])
        self.assertEqual(len([m for m in journal.measurements if m.phase == "during"]), 5)

    def test_a_force_delete_skips_the_grace_period_and_breaks_the_error_sli(self):
        doc = fixture("pod-delete.json")
        doc["method"]["faults"][0]["grace_period_s"] = 0
        journal, world = run_doc(doc)
        self.assertEqual((journal.verdict, journal.exit_code), ("deviated", 1))
        self.assertIn("--grace-period=0 --force", " ".join(commands(world)))
        first = [m for m in journal.measurements if m.phase == "during"][0]
        self.assertAlmostEqual(first.reading("error_rate").value, 50 * 1.0 / 3 / 150)

    def test_a_latency_fault_aborts_early_and_the_rollback_removes_the_qdisc(self):
        journal, world = run_doc(fixture("netem-latency.json"))
        self.assertEqual(journal.verdict, "aborted")
        self.assertEqual(journal.aborted_at_s - journal.injected_at_s, 4.0)
        self.assertEqual(world.qdisc, {})
        self.assertIn("ip netns exec cc-chaos-srv tc qdisc del dev cc-chaos-s root", commands(world))
        self.assertEqual(journal.recovered_after_s, 2.0)
        self.assertIn("p95_latency_s 2.000 > 0.15", journal.reason)

    def test_no_fault_is_injected_when_the_steady_state_is_already_broken(self):
        journal, world = run_doc(fixture("netem-latency.json"), main.SimulatedWorld(latency_s=0.3))
        self.assertEqual(journal.verdict, "not-run")
        self.assertFalse(any("qdisc add" in c for c in commands(world)))

    def test_rollback_runs_for_every_started_fault_when_a_later_one_fails(self):
        doc = fixture("netem-latency.json")
        doc["method"]["faults"].append({"type": "pod-delete", "namespace": "cc-chaos-experiments", "selector": "app=web"})
        doc["blast_radius"]["namespaces"] = ["cc-chaos-experiments"]
        world = main.SimulatedWorld()
        world.failing["delete pod"] = 1
        journal, _ = run_doc(doc, world)
        self.assertEqual(journal.verdict, "failed")
        self.assertIn("simulated failure of delete pod", journal.reason)
        self.assertEqual(world.qdisc, {})
        self.assertEqual([e["message"].split(":")[0] for e in journal.events if e["phase"] == "rollback"], ["pod-delete", "netem"])

    def test_a_rollback_that_keeps_failing_is_loud_and_names_the_manual_command(self):
        world = main.SimulatedWorld()
        world.failing["tc qdisc del"] = 99
        journal, _ = run_doc(fixture("netem-latency.json"), world)
        self.assertFalse(journal.rollback_ok)
        self.assertEqual(journal.exit_code, 1)
        self.assertIn("ROLLBACK FAILED", journal.reason)
        self.assertIn("remove it by hand: ip netns exec cc-chaos-srv tc qdisc del dev cc-chaos-s root", " ".join(e["message"] for e in journal.events))
        self.assertIsNone(journal.recovered_after_s)

    def test_the_abort_button_stops_the_method_and_still_rolls_back(self):
        doc = fixture("netem-latency.json")
        doc["method"]["faults"][0].update({"delay_ms": 10, "loss_percent": 0})
        world = main.SimulatedWorld()
        world.interrupt_at = (5.0, "signal SIGINT")
        journal, _ = run_doc(doc, world)
        self.assertEqual(journal.verdict, "aborted")
        self.assertIn("abort button: signal SIGINT", journal.reason)
        self.assertEqual(world.qdisc, {})
        with tempfile.TemporaryDirectory() as tmp:
            button = os.path.join(tmp, "abort")
            real = main.RealWorld(abort_file=button)
            self.assertEqual(real.stopping(), "")
            open(button, "w").close()
            self.assertIn(button, real.stopping())

    def test_probe_errors_in_a_row_abort_and_a_single_gap_is_inconclusive(self):
        world = main.SimulatedWorld()
        world.unreachable = [(3.5, 12.5)]
        journal, _ = run_doc(fixture("pod-delete.json"), world)
        self.assertEqual(journal.verdict, "aborted")
        self.assertIn("3 probe errors in a row", journal.reason)
        world = main.SimulatedWorld()
        world.unreachable = [(5.5, 6.5)]
        journal, _ = run_doc(fixture("pod-delete.json"), world)
        self.assertEqual(journal.verdict, "inconclusive")
        self.assertIn("had no data", journal.reason)

    def test_a_chaos_mesh_podchaos_reaches_the_same_verdict_as_pod_delete(self):
        mine, _ = run_doc(fixture("pod-delete.json"))
        theirs, world = run_doc(fixture("podchaos-experiment.json"))
        self.assertEqual((mine.verdict, theirs.verdict), ("held", "held"))
        self.assertEqual(len(world.pods), 3)
        self.assertEqual(len(set(world.pods) - {"web-1", "web-2", "web-3"}), 1)
        self.assertTrue(any(c.startswith("kubectl delete -f") for c in commands(world)))
        self.assertEqual(world.applied, {})


class LoadAndCliTests(unittest.TestCase):
    def test_load_stats_export_both_outcomes_from_zero_and_round_trip(self):
        stats = main.LoadStats()
        samples = main.parse_exposition(stats.exposition())
        counters = {dict(s.labels)["outcome"]: s.value for s in samples if s.name == main.LOAD_REQUESTS}
        self.assertEqual(counters, {"ok": 0.0, "error": 0.0})
        stats.observe("ok", 0.003)
        stats.observe("ok", 7.0)
        samples = main.parse_exposition(stats.exposition())
        ok = {dict(s.labels).get("le"): s.value for s in samples if s.name == main.LOAD_LATENCY + "_bucket" and dict(s.labels)["outcome"] == "ok"}
        self.assertEqual((ok["0.005"], ok["5"], ok["+Inf"]), (1, 1, 2))
        count = [s.value for s in samples if s.name == main.LOAD_LATENCY + "_count" and dict(s.labels)["outcome"] == "ok"]
        self.assertEqual(count, [2])
        self.assertEqual(main.show(1234567.0), "1234567")

    def test_the_load_generator_counts_real_requests_and_serves_metrics(self):
        with Server(Hello) as server:
            code, out, _ = cli(["load", "--url", server.url + "/", "--concurrency", "2", "--rate", "40", "--duration", "1", "--metrics-port", "0", "--json"])
        report = json.loads(out)
        self.assertEqual(code, 0)
        self.assertGreater(report["ok"], 10)
        self.assertEqual(report["errors"], 0)
        stats = main.LoadStats()
        stats.observe("ok", 0.02, 5)
        with Server(main.metrics_handler(stats)) as metrics:
            text = main.RealWorld().fetch({"url": metrics.url + "/metrics"})
        self.assertIn('minichaos_load_requests_total{outcome="ok"} 5', text)
        with self.assertRaises(main.ProbeError):
            main.RealWorld().fetch({"url": metrics.url + "/metrics"})

    def test_cli_validate_simulate_and_input_errors(self):
        code, out, _ = cli(["validate", os.path.join(FIXTURES, "netem-latency.json"), "--commands"])
        self.assertEqual(code, 0)
        self.assertIn("$ ip netns exec cc-chaos-srv tc qdisc add dev cc-chaos-s root netem delay 200ms loss 5%", out)
        code, out, _ = cli(["simulate", os.path.join(FIXTURES, "netem-latency.json"), "--json"])
        report = json.loads(out)
        self.assertEqual((code, report["verdict"]), (1, "aborted"))
        self.assertEqual([m["phase"] for m in report["measurements"]], ["before", "during", "during", "after"])
        self.assertIn("tc qdisc del", report["faults"][0]["revert"])
        code, _, _ = cli(["simulate", os.path.join(FIXTURES, "process-cpu.json")])
        self.assertEqual(code, 0)
        with tempfile.TemporaryDirectory() as tmp:
            broken = os.path.join(tmp, "broken.json")
            with open(broken, "w") as handle:
                handle.write("{not json")
            code, _, err = cli(["simulate", broken])
            self.assertEqual(code, 2)
            self.assertIn("is not JSON", err)
            doc = fixture("pod-delete.json")
            del doc["blast_radius"]
            with open(broken, "w") as handle:
                json.dump(doc, handle)
            code, _, err = cli(["validate", broken])
            self.assertEqual(code, 2)
            self.assertIn("list the namespaces and netns it may touch", err)


if __name__ == "__main__":
    unittest.main()
