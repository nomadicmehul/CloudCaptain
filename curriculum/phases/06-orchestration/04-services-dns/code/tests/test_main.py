import json
import os
import random
import shutil
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO

HERE = os.path.dirname(__file__)
sys.path.insert(0, os.path.join(HERE, ".."))

import main  # noqa: E402

FIXTURES = os.path.join(HERE, "..", "fixtures")
SHOP = os.path.join(FIXTURES, "shop.json")
NODE_NAT = os.path.join(FIXTURES, "node-nat.txt")


def service(name, ports, selector=None, cluster_ip="10.96.0.20", namespace="shop", **spec):
    body = {"clusterIP": cluster_ip, "ports": ports, **spec}
    if selector is not None:
        body["selector"] = selector
    return {"kind": "Service", "metadata": {"name": name, "namespace": namespace}, "spec": body}


def pod(name, ip, labels=None, ready=True, ports=None, namespace="shop", node="kind-worker", phase="Running", deleting=False, hostname=None, subdomain=None):
    meta = {"name": name, "namespace": namespace, "labels": labels if labels is not None else {"app": "web"}}
    if deleting:
        meta["deletionTimestamp"] = "2026-11-01T10:00:00Z"
    spec = {"nodeName": node, "containers": [{"name": "app", "ports": ports if ports is not None else [{"name": "http", "containerPort": 8080}]}]}
    if hostname:
        spec["hostname"] = hostname
        spec["subdomain"] = subdomain
    status = {"phase": phase, "conditions": [{"type": "Ready", "status": "True" if ready else "False"}]}
    if ip:
        status["podIP"] = ip
    return {"kind": "Pod", "metadata": meta, "spec": spec, "status": status}


HTTP = [{"name": "http", "port": 80, "targetPort": "http", "protocol": "TCP"}]


def cluster(*objects):
    return main.cluster_from(objects)


def run(argv):
    out, err = StringIO(), StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = main.main(argv)
    return code, out.getvalue(), err.getvalue()


class LoadTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="miniproxy-")
        self.addCleanup(shutil.rmtree, self.dir)

    def write(self, name, data):
        path = os.path.join(self.dir, name)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(data if isinstance(data, str) else json.dumps(data))
        return path

    def test_lists_single_objects_and_several_files_load_and_parse(self):
        first = self.write("a.json", {"kind": "List", "items": [service("web", HTTP, {"app": "web"}), {"kind": "ConfigMap", "metadata": {"name": "x"}}]})
        second = self.write("b.json", pod("web-1", "10.244.1.5"))
        loaded = main.cluster_from(main.load_objects([first, second]))
        self.assertEqual([s.key for s in loaded.services], ["shop/web"])
        self.assertEqual(loaded.services[0].ports[0].target_port, "http")
        self.assertEqual((loaded.pods[0].ip, loaded.pods[0].ready, loaded.pods[0].ports[0].port), ("10.244.1.5", True, 8080))

    def test_bad_input_raises_input_error_with_the_file_name(self):
        with self.assertRaisesRegex(main.InputError, "not JSON"):
            main.load_objects([self.write("bad.json", "{nope")])
        with self.assertRaisesRegex(main.InputError, "expected a Kubernetes object"):
            main.load_objects([self.write("odd.json", {"hello": 1})])
        with self.assertRaisesRegex(main.InputError, "missing.json"):
            main.load_objects([os.path.join(self.dir, "missing.json")])

    def test_real_endpointslice_conditions_default_like_the_api_says(self):
        obj = {"kind": "EndpointSlice", "addressType": "IPv4", "metadata": {"name": "web-abc12", "namespace": "shop", "labels": {"kubernetes.io/service-name": "web"}},
               "ports": [{"name": "http", "port": 8080, "protocol": "TCP"}],
               "endpoints": [{"addresses": ["10.244.1.5"]}, {"addresses": ["10.244.1.6"], "conditions": {"ready": False, "serving": True, "terminating": True}}]}
        parsed = main.parse_slice(obj)
        self.assertEqual(parsed.service, "web")
        self.assertEqual([(e.ready, e.serving, e.terminating) for e in parsed.endpoints], [(True, True, False), (False, True, True)])


class ControllerTests(unittest.TestCase):
    def test_selector_ready_phase_and_ip_decide_membership_and_conditions(self):
        c = cluster(
            service("web", HTTP, {"app": "web"}),
            pod("ready", "10.244.1.5"),
            pod("unready", "10.244.1.6", ready=False),
            pod("no-ip", None),
            pod("done", "10.244.1.7", phase="Succeeded"),
            pod("other-label", "10.244.1.8", labels={"app": "db"}),
            pod("other-ns", "10.244.1.9", namespace="prod"),
        )
        slices = main.reconcile_slices(c.services[0], c.pods)
        self.assertEqual(len(slices), 1)
        self.assertEqual([(e.pod, e.ready, e.serving) for e in slices[0].endpoints], [("ready", True, True), ("unready", False, False)])
        self.assertEqual([(p.name, p.port) for p in slices[0].ports], [("http", 8080)])

    def test_terminating_pods_stay_listed_as_serving_but_not_ready(self):
        c = cluster(service("web", HTTP, {"app": "web"}), pod("old", "10.244.1.5", deleting=True))
        endpoint = main.reconcile_slices(c.services[0], c.pods)[0].endpoints[0]
        self.assertEqual((endpoint.ready, endpoint.serving, endpoint.terminating), (False, True, True))

    def test_publish_not_ready_addresses_marks_everything_ready(self):
        c = cluster(service("web", HTTP, {"app": "web"}, publishNotReadyAddresses=True), pod("starting", "10.244.1.5", ready=False))
        endpoint = main.reconcile_slices(c.services[0], c.pods)[0].endpoints[0]
        self.assertEqual((endpoint.ready, endpoint.serving), (True, True))

    def test_named_target_port_resolves_per_pod_and_splits_slices(self):
        c = cluster(
            service("web", HTTP, {"app": "web"}),
            pod("v1", "10.244.1.5", ports=[{"name": "http", "containerPort": 8080}]),
            pod("v2", "10.244.1.6", ports=[{"name": "http", "containerPort": 9090}]),
            pod("renamed", "10.244.1.7", ports=[{"name": "web", "containerPort": 8080}]),
        )
        slices = main.reconcile_slices(c.services[0], c.pods)
        by_ports = {tuple((p.name, p.port) for p in s.ports): [e.pod for e in s.endpoints] for s in slices}
        self.assertEqual(by_ports, {(): ["renamed"], (("http", 8080),): ["v1"], (("http", 9090),): ["v2"]})
        used = main.proxy_endpoints(c.services[0].ports[0], slices)
        self.assertEqual(used, [("10.244.1.5", 8080), ("10.244.1.6", 9090)])

    def test_slices_hold_at_most_max_endpoints_and_an_empty_service_gets_a_placeholder(self):
        c = cluster(service("web", HTTP, {"app": "web"}), *[pod(f"p{i}", f"10.244.1.{i + 10}") for i in range(5)])
        self.assertEqual([len(s.endpoints) for s in main.reconcile_slices(c.services[0], c.pods, max_per_slice=2)], [2, 2, 1])
        empty = cluster(service("web", HTTP, {"app": "nothing"}), pod("p", "10.244.1.5"))
        placeholder = main.reconcile_slices(empty.services[0], empty.pods)
        self.assertEqual([(len(s.ports), len(s.endpoints)) for s in placeholder], [(0, 0)])

    def test_kube_proxy_prefers_ready_then_falls_back_to_serving_terminating(self):
        port = main.ServicePort("http", 80, "http")
        ready = main.Endpoint("10.244.1.5", True, True, False, "kind-worker")
        draining = main.Endpoint("10.244.1.6", False, True, True, "kind-worker2")
        broken = main.Endpoint("10.244.1.7", False, False, True, "kind-worker2")
        slice_ = main.EndpointSlice("shop", "web-x", "web", [main.EndpointPort("http", 8080)], [ready, draining, broken])
        self.assertEqual(main.proxy_endpoints(port, [slice_]), [("10.244.1.5", 8080)])
        slice_.endpoints = [draining, broken]
        self.assertEqual(main.proxy_endpoints(port, [slice_]), [("10.244.1.6", 8080)])
        slice_.endpoints = [ready, draining]
        self.assertEqual(main.proxy_endpoints(port, [slice_], node="kind-worker2"), [("10.244.1.6", 8080)])

    def test_selectorless_services_use_the_slices_you_supplied(self):
        manual = {"kind": "EndpointSlice", "metadata": {"name": "ext-1", "namespace": "shop", "labels": {"kubernetes.io/service-name": "ext"}},
                  "ports": [{"name": "http", "port": 443}], "endpoints": [{"addresses": ["192.0.2.10"]}]}
        c = cluster(service("ext", HTTP, None, cluster_ip="10.96.0.60"), service("web", HTTP, {"app": "web"}), pod("p", "10.244.1.5"), manual)
        slices = main.slices_for(c)
        self.assertEqual([e.address for s in slices["shop/ext"] for e in s.endpoints], ["192.0.2.10"])
        self.assertEqual([e.address for s in slices["shop/web"] for e in s.endpoints], ["10.244.1.5"])


class IptablesTests(unittest.TestCase):
    def shop_rules(self, node="kind-worker"):
        c = main.cluster_from(main.load_objects([SHOP]))
        return main.render_iptables(main.plan_ports(c, main.slices_for(c), node), "10.244.0.0/16")

    def test_probability_cascade_matches_what_iptables_save_prints_and_is_uniform(self):
        self.assertEqual(main.cascade(1), [None])
        self.assertEqual(main.cascade(3), ["0.33333333349", "0.50000000000", None])
        self.assertEqual(main.cascade(5)[0], "0.20000000019")
        for n in range(1, 8):
            portions = main.shares([float(p) if p else None for p in main.cascade(n)])
            for portion in portions:
                self.assertAlmostEqual(portion, 1.0 / n, places=6)

    def test_render_has_hooks_service_endpoint_chains_dnat_and_masquerade_marks(self):
        text = self.shop_rules()
        self.assertIn('-A PREROUTING -m comment --comment "kubernetes service portals" -j KUBE-SERVICES', text)
        self.assertIn("-A KUBE-MARK-MASQ -j MARK --set-xmark 0x4000/0x4000", text)
        self.assertIn('-A KUBE-POSTROUTING -m comment --comment "kubernetes service traffic requiring SNAT" -j MASQUERADE --random-fully', text)
        svc = [line for line in text.splitlines() if line.startswith("-A KUBE-SERVICES -d 10.96.0.20/32")]
        self.assertEqual(len(svc), 1)
        chain = svc[0].split("-j ")[-1]
        self.assertTrue(chain.startswith("KUBE-SVC-") and len(chain) == 25)
        rules = [line for line in text.splitlines() if line.startswith(f"-A {chain} ")]
        self.assertIn("! -s 10.244.0.0/16 -d 10.96.0.20/32", rules[0])
        self.assertTrue(rules[0].endswith("-j KUBE-MARK-MASQ"))
        self.assertEqual(len(rules), 4)
        self.assertIn("--probability 0.33333333349", rules[1])
        self.assertIn("--probability 0.50000000000", rules[2])
        self.assertNotIn("statistic", rules[3])
        self.assertIn("-m tcp -j DNAT --to-destination 10.244.2.7:8080", text)
        self.assertNotIn("10.244.2.8", text)
        self.assertTrue(text.splitlines()[-1] == "COMMIT" and text.count("COMMIT") == 2)

    def test_service_without_endpoints_is_a_reject_in_the_filter_table(self):
        text = self.shop_rules()
        nat, filter_table = text.split("*filter")
        self.assertNotIn("10.96.0.40", nat)
        self.assertIn('-A KUBE-SERVICES -d 10.96.0.40/32 -p tcp -m comment --comment "shop/empty:http has no endpoints" -m tcp --dport 80 -j REJECT --reject-with icmp-port-unreachable', filter_table)

    def test_session_affinity_puts_recent_checks_before_the_random_rules(self):
        c = cluster(service("sticky", HTTP, {"app": "web"}, sessionAffinity="ClientIP", sessionAffinityConfig={"clientIP": {"timeoutSeconds": 600}}), pod("a", "10.244.1.5"), pod("b", "10.244.1.6"))
        text = main.render_iptables(main.plan_ports(c, main.slices_for(c)))
        lines = [line for line in text.splitlines() if line.startswith("-A KUBE-SVC-")]
        self.assertEqual(["--rcheck" in line for line in lines], [True, True, False, False])
        self.assertIn("--seconds 600 --reap", lines[0])
        self.assertIn("-m recent --set --name KUBE-SEP-", text)

    def test_nodeport_local_policy_keeps_only_this_nodes_endpoints(self):
        text = self.shop_rules(node="kind-worker")
        self.assertIn('-m tcp --dport 30080 -j KUBE-EXT-', text)
        svl = [line for line in text.splitlines() if line.startswith("-A KUBE-SVL-")]
        self.assertEqual(len(svl), 1)
        self.assertIn("10.244.1.10:8080", svl[0])
        other = self.shop_rules(node="kind-control-plane")
        self.assertIn("shop/checkout:http has no local endpoints", other)


class TraceTests(unittest.TestCase):
    def test_trace_walks_a_rendered_ruleset_back_to_the_pods(self):
        c = main.cluster_from(main.load_objects([SHOP]))
        text = main.render_iptables(main.plan_ports(c, main.slices_for(c)), "10.244.0.0/16")
        trace = main.trace_ruleset(text, main.Packet("10.96.0.20", 80))
        self.assertEqual(trace.service, "shop/web:http cluster IP")
        self.assertEqual([h.destination for h in trace.endpoints], ["10.244.1.5:8080", "10.244.1.6:8080", "10.244.2.7:8080"])
        self.assertEqual([h.probability for h in trace.endpoints], ["0.33333333349", "0.50000000000", None])
        self.assertIn("source not in 10.244.0.0/16", trace.masquerade[0])
        self.assertEqual(sum(1 for m in trace.masquerade if "hairpin" in m), 3)

    def test_trace_reads_a_node_ruleset_with_foreign_chains(self):
        with open(NODE_NAT, encoding="utf-8") as handle:
            text = handle.read()
        udp = main.trace_ruleset(text, main.Packet("10.96.0.10", 53, "udp"))
        self.assertEqual(udp.path, ["KUBE-SERVICES", "KUBE-SVC-TCOU7JCQXEZGVUNU"])
        self.assertEqual([h.destination for h in udp.endpoints], ["10.244.0.2:53", "10.244.0.3:53"])
        api = main.trace_ruleset(text, main.Packet("10.96.0.1", 443))
        self.assertEqual([(h.destination, h.share) for h in api.endpoints], [("172.18.0.2:6443", 1.0)])
        miss = main.trace_ruleset(text, main.Packet("10.96.0.10", 54, "udp"))
        self.assertFalse(miss.found)

    def test_trace_follows_a_nodeport_and_reports_rejects(self):
        c = main.cluster_from(main.load_objects([SHOP]))
        text = main.render_iptables(main.plan_ports(c, main.slices_for(c), "kind-worker"), "10.244.0.0/16")
        nodeport = main.trace_ruleset(text, main.Packet("172.18.0.3", 30080, "tcp", node_local=True))
        self.assertEqual(nodeport.path[1], "KUBE-NODEPORTS")
        self.assertTrue(nodeport.path[2].startswith("KUBE-EXT-"))
        self.assertTrue(nodeport.path[-1].startswith("KUBE-SVL-"))
        self.assertEqual(nodeport.service, "shop/checkout:http")
        self.assertEqual([h.destination for h in nodeport.endpoints], ["10.244.1.10:8080"])
        rejected = main.trace_ruleset(text, main.Packet("10.96.0.40", 80))
        self.assertIn("has no endpoints", rejected.rejected)

    def test_simulation_spreads_new_connections_evenly(self):
        c = main.cluster_from(main.load_objects([SHOP]))
        text = main.render_iptables(main.plan_ports(c, main.slices_for(c)))
        trace = main.trace_ruleset(text, main.Packet("10.96.0.20", 80))
        counts = main.simulate(trace, 9000, random.Random(3))
        self.assertEqual(sum(counts.values()), 9000)
        for count in counts.values():
            self.assertLess(abs(count - 3000), 300)

    def test_iptables_parser_handles_negation_quotes_and_flags(self):
        rule = main.parse_rule('-A KUBE-POSTROUTING -m mark ! --mark 0x4000/0x4000 -m comment --comment "a b c" -j RETURN', 1)
        self.assertEqual(rule.options["--mark"], ("0x4000/0x4000", True))
        self.assertEqual(rule.value("--comment"), "a b c")
        self.assertEqual(rule.jump, "RETURN")
        with self.assertRaisesRegex(main.InputError, "outside"):
            main.parse_iptables_save("-A KUBE-SERVICES -j X\n")


class NftablesTests(unittest.TestCase):
    def test_nft_rendering_uses_a_map_and_numgen_and_traces_back(self):
        c = main.cluster_from(main.load_objects([SHOP]))
        text = main.render_nftables(main.plan_ports(c, main.slices_for(c)), "10.244.0.0/16")
        self.assertIn("ip daddr . meta l4proto . th dport vmap @service-ips", text)
        self.assertIn("numgen random mod 3 vmap {", text)
        self.assertIn("10.96.0.40 . tcp . 80 : goto reject-chain", text)
        trace = main.trace_ruleset(text, main.Packet("10.96.0.20", 80))
        self.assertEqual(trace.mode, "nftables")
        self.assertEqual([h.destination for h in trace.endpoints], ["10.244.1.5:8080", "10.244.1.6:8080", "10.244.2.7:8080"])
        self.assertTrue(trace.service.endswith("-shop/web/tcp/http"))
        self.assertIn("no-endpoint-services", main.trace_ruleset(text, main.Packet("10.96.0.40", 80)).rejected)


class DnsTests(unittest.TestCase):
    def setUp(self):
        c = main.cluster_from(main.load_objects([SHOP]))
        self.records = main.dns_records(c, main.slices_for(c))
        self.search = main.search_domains("shop")

    def test_clusterip_headless_srv_externalname_and_pod_records(self):
        self.assertEqual([r.value for r in main.lookup(self.records, "web.shop.svc.cluster.local")], ["10.96.0.20"])
        self.assertEqual([r.value for r in main.lookup(self.records, "web-headless.shop.svc.cluster.local")], ["10.244.1.5", "10.244.1.6", "10.244.2.7"])
        srv = main.lookup(self.records, "_http._tcp.web-headless.shop.svc.cluster.local", "SRV")
        self.assertEqual([(r.value, r.port, r.weight) for r in srv][0], ("10-244-1-5.web-headless.shop.svc.cluster.local", 8080, 33))
        self.assertEqual([r.text() for r in main.lookup(self.records, "_http._tcp.web.shop.svc.cluster.local", "SRV")], ["_http._tcp.web.shop.svc.cluster.local. SRV 0 100 80 web.shop.svc.cluster.local."])
        self.assertEqual([r.value for r in main.lookup(self.records, "db-1.db.shop.svc.cluster.local")], ["10.244.2.21"])
        self.assertEqual(main.lookup(self.records, "partner.shop.svc.cluster.local")[0].text(), "partner.shop.svc.cluster.local. CNAME api.partner.example.com.")
        self.assertEqual([r.value for r in main.lookup(self.records, "10-244-1-5.shop.pod.cluster.local")], ["10.244.1.5"])

    def test_search_path_and_ndots_decide_how_many_names_are_tried(self):
        self.assertEqual(len(main.resolve("web", self.records, self.search).attempts), 1)
        cross = main.resolve("web.shop", self.records, self.search)
        self.assertEqual([a.rcode for a in cross.attempts], ["NXDOMAIN", "NOERROR"])
        external = main.resolve("api.partner.example.com", self.records, self.search)
        self.assertEqual((len(external.attempts), external.nxdomains, external.attempts[-1].rcode), (4, 3, "FORWARDED"))
        self.assertEqual(len(main.resolve("api.partner.example.com.", self.records, self.search).attempts), 1)
        self.assertEqual(len(main.resolve("web.shop.svc.cluster.local", self.records, self.search).attempts), 4)
        self.assertEqual(len(main.resolve("api.partner.example.com", self.records, self.search, ndots=2).attempts), 1)
        self.assertEqual(main.resolve("nope", self.records, self.search).answer, [])

    def test_names_with_ndots_or_more_dots_go_absolute_first_and_musl_never_searches_them(self):
        self.assertEqual(main.candidate_names("a.b.c.d.e.f", ["x"], 5, "glibc"), ["a.b.c.d.e.f", "a.b.c.d.e.f.x"])
        self.assertEqual(main.candidate_names("a.b.c.d.e.f", ["x"], 5, "musl"), ["a.b.c.d.e.f"])
        self.assertEqual(main.candidate_names("a.b", ["x", "y"], 5), ["a.b.x", "a.b.y", "a.b"])

    def test_resolv_conf_round_trip(self):
        text = main.render_resolv_conf("shop", "10.96.0.10", extra=["us-east1.internal"])
        self.assertEqual(text, "search shop.svc.cluster.local svc.cluster.local cluster.local us-east1.internal\nnameserver 10.96.0.10\noptions ndots:5\n")
        self.assertEqual(main.parse_resolv_conf(text), {"search": ["shop.svc.cluster.local", "svc.cluster.local", "cluster.local", "us-east1.internal"], "nameservers": ["10.96.0.10"], "ndots": 5})
        self.assertEqual(main.parse_resolv_conf("nameserver 1.1.1.1\n")["ndots"], 1)


class CliTests(unittest.TestCase):
    def test_endpoints_json_and_unknown_service(self):
        code, out, _ = run(["endpoints", "shop/web", "--from", SHOP, "--json"])
        self.assertEqual(code, 0)
        data = json.loads(out)
        self.assertEqual([e["ready"] for e in data["shop/web"][0]["endpoints"]], [True, True, True, False])
        code, _, err = run(["endpoints", "shop/nope", "--from", SHOP])
        self.assertEqual(code, 1)
        self.assertIn("no Service shop/nope", err)

    def test_iptables_json_lists_chains_and_probabilities(self):
        code, out, _ = run(["iptables", "--from", SHOP, "--cluster-cidr", "10.244.0.0/16", "--json"])
        self.assertEqual(code, 0)
        web = next(s for s in json.loads(out)["services"] if s["id"] == "shop/web:http")
        self.assertEqual(web["probabilities"], ["0.33333333349", "0.50000000000", None])
        self.assertEqual(run(["iptables", "--from", SHOP, "--cluster-cidr", "ten"])[0], 2)

    def test_dns_and_trace_exit_codes(self):
        self.assertEqual(run(["dns", "web", "--namespace", "shop", "--from", SHOP])[0], 0)
        code, out, _ = run(["dns", "nope", "--namespace", "shop", "--from", SHOP])
        self.assertEqual(code, 1)
        self.assertIn("4 name(s) tried, 4 NXDOMAIN", out)
        code, out, _ = run(["trace", "--ruleset", NODE_NAT, "--to", "10.96.0.10:53", "--proto", "udp", "--json", "--simulate", "100"])
        self.assertEqual(code, 0)
        self.assertEqual(sum(json.loads(out)["simulation"].values()), 100)
        self.assertEqual(run(["trace", "--ruleset", NODE_NAT, "--to", "10.96.9.9:80"])[0], 1)
        self.assertEqual(run(["trace", "--ruleset", NODE_NAT, "--to", "web:80"])[0], 2)

    def test_dns_reads_a_pods_resolv_conf(self):
        directory = tempfile.mkdtemp(prefix="miniproxy-")
        self.addCleanup(shutil.rmtree, directory)
        path = os.path.join(directory, "resolv.conf")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("search shop.svc.cluster.local svc.cluster.local cluster.local\nnameserver 10.96.0.10\noptions ndots:2\n")
        code, out, _ = run(["dns", "api.partner.example.com", "--resolv-conf", path, "--from", SHOP, "--json"])
        self.assertEqual(code, 0)
        data = json.loads(out)
        self.assertEqual((data["ndots"], len(data["attempts"]), data["nxdomains"]), (2, 1, 0))


if __name__ == "__main__":
    unittest.main()
