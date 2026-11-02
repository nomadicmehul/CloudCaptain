import json
import os
import socket
import struct
import sys
import tempfile
import threading
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import main  # noqa: E402

FIXTURES = os.path.join(os.path.dirname(__file__), "..", "fixtures")
LAB = os.path.join(FIXTURES, "lab")
NS = "cc-network-policy"
TOOLS = "cc-network-policy-tools"
FRONTEND, API, DB, SCANNER = f"{NS}/frontend", f"{NS}/api", f"{NS}/db", f"{TOOLS}/scanner"


def load(name):
    with open(os.path.join(LAB, name), encoding="utf-8") as handle:
        return json.load(handle)


EXPECTED = load("expected.json")


def pod(name, namespace="shop", labels=None, ip="10.0.0.1", ports=None):
    body = {"containers": [{"name": "main", "ports": [{"name": n, "containerPort": p, "protocol": proto} for n, p, proto in (ports or [])]}]}
    return {"kind": "Pod", "metadata": {"name": name, "namespace": namespace, "labels": labels or {}}, "spec": body, "status": {"phase": "Running", "podIP": ip}}


def namespace(name, **labels):
    return {"kind": "Namespace", "metadata": {"name": name, "labels": labels}}


def policy(name, spec, ns="shop"):
    return {"kind": "NetworkPolicy", "metadata": {"name": name, "namespace": ns}, "spec": spec}


def cluster(*objects):
    return main.load_cluster([list(objects)])


def allowed(c, source, destination, port, protocol="TCP", externals=()):
    src = main.resolve_endpoint(c, source, externals)
    dst = main.resolve_endpoint(c, destination, externals)
    return main.evaluate(c, src, dst, port, protocol).allowed


def lab_cluster(step):
    files = [s["apply"] for s in EXPECTED["steps"][: step + 1] if s["apply"]]
    return main.load_cluster([load("pods.json"), load("namespaces.json")] + [load(f) for f in files])


def lab_matrix(step):
    c = lab_cluster(step)
    ports = [main.parse_port_option(p) for p in EXPECTED["probe_ports"]]
    return main.build_matrix(c, EXPECTED["namespaces"], ports, main.externals_from(EXPECTED["externals"]), dns=True), c


def run(argv, stdin=None):
    out, err = StringIO(), StringIO()
    previous = sys.stdin
    if stdin is not None:
        sys.stdin = StringIO(stdin)
    try:
        with redirect_stdout(out), redirect_stderr(err):
            code = main.main(argv)
    finally:
        sys.stdin = previous
    return code, out.getvalue(), err.getvalue()


SHOP = [
    namespace("shop", team="shop"),
    namespace("tools", team="platform"),
    pod("web", labels={"app": "web"}, ip="10.0.0.1", ports=[("http", 8080, "TCP")]),
    pod("api", labels={"app": "api"}, ip="10.0.0.2", ports=[("http", 8081, "TCP"), ("grpc", 9000, "TCP")]),
    pod("api", namespace="tools", labels={"app": "api"}, ip="10.0.1.2", ports=[("http", 8080, "TCP")]),
    pod("scan", namespace="tools", labels={"app": "scan"}, ip="10.0.1.3"),
]


class SelectorTests(unittest.TestCase):
    def test_match_labels_and_every_expression_operator(self):
        s = main.parse_selector({"matchLabels": {"app": "api"}, "matchExpressions": [
            {"key": "tier", "operator": "In", "values": ["web", "api"]},
            {"key": "legacy", "operator": "DoesNotExist"},
            {"key": "env", "operator": "NotIn", "values": ["dev"]},
            {"key": "team", "operator": "Exists"},
        ]}, "s")
        self.assertTrue(main.selector_matches(s, {"app": "api", "tier": "web", "team": "x"}))
        self.assertFalse(main.selector_matches(s, {"app": "api", "tier": "db", "team": "x"}))
        self.assertFalse(main.selector_matches(s, {"app": "api", "tier": "web", "team": "x", "legacy": "1"}))
        self.assertFalse(main.selector_matches(s, {"app": "api", "tier": "web", "team": "x", "env": "dev"}))
        self.assertFalse(main.selector_matches(s, {"app": "api", "tier": "web"}))
        self.assertEqual(main.describe_selector(s), "app=api,tier in (web,api),!legacy,env notin (dev),team")

    def test_empty_selector_matches_everything_and_notin_matches_a_missing_key(self):
        self.assertTrue(main.selector_matches(main.parse_selector({}, "s"), {}))
        self.assertTrue(main.selector_matches(main.parse_selector(None, "s"), {"a": "b"}))
        notin = main.parse_selector({"matchExpressions": [{"key": "env", "operator": "NotIn", "values": ["prod"]}]}, "s")
        self.assertTrue(main.selector_matches(notin, {}))

    def test_invalid_selectors_are_refused_with_the_field_path(self):
        with self.assertRaisesRegex(main.InputError, r"s.matchExpressions\[0\].operator: Unsupported value 'Equals'"):
            main.parse_selector({"matchExpressions": [{"key": "a", "operator": "Equals", "values": ["b"]}]}, "s")
        with self.assertRaisesRegex(main.InputError, "values: must be specified"):
            main.parse_selector({"matchExpressions": [{"key": "a", "operator": "In"}]}, "s")


class ParsingTests(unittest.TestCase):
    def test_policy_types_default_like_the_api_server(self):
        def types(spec):
            return main.make_policy(policy("p", spec)).types

        self.assertEqual(types({"podSelector": {}}), ["Ingress"])
        self.assertEqual(types({"podSelector": {}, "egress": []}), ["Ingress"])
        self.assertEqual(types({"podSelector": {}, "egress": [{}]}), ["Ingress", "Egress"])
        self.assertEqual(types({"podSelector": {}, "policyTypes": ["Egress"]}), ["Egress"])
        self.assertEqual(types({"podSelector": {}, "policyTypes": ["Egress", "Ingress"]}), ["Ingress", "Egress"])

    def test_validation_errors_match_the_api_server_wording(self):
        cases = [
            ({"podSelector": {}, "ingress": [{"from": [{}]}]}, "must specify a peer"),
            ({"podSelector": {}, "ingress": [{"from": [{"ipBlock": {"cidr": "10.0.0.0/8"}, "podSelector": {}}]}]}, "may not specify both ipBlock and another peer"),
            ({"podSelector": {}, "ingress": [{"ports": [{"port": "http", "endPort": 9000}]}]}, "may not be specified when `port` is non-numeric"),
            ({"podSelector": {}, "ingress": [{"ports": [{"port": 9000, "endPort": 8000}]}]}, "must be greater than or equal to `port`"),
            ({"podSelector": {}, "ingress": [{"ports": [{"endPort": 8000}]}]}, "may not be specified when `port` is not specified"),
            ({"podSelector": {}, "ingress": [{"ports": [{"protocol": "ICMP"}]}]}, "Unsupported value 'ICMP'"),
            ({"podSelector": {}, "ingress": [{"ports": [{"port": "8080"}]}]}, "is not a port name"),
            ({"podSelector": {}, "egress": [{"to": [{"ipBlock": {"cidr": "10.0.0.0/16", "except": ["10.1.0.0/24"]}}]}]}, "must be a strict subset of `cidr`"),
            ({"podSelector": {}, "egress": [{"to": [{"ipBlock": {"cidr": "10.0.0.0/16", "except": ["10.0.0.0/16"]}}]}]}, "must be a strict subset of `cidr`"),
            ({"podSelector": {}, "policyTypes": ["Both"]}, "Unsupported value 'Both'"),
        ]
        for spec, message in cases:
            with self.subTest(message=message):
                with self.assertRaisesRegex(main.InputError, message):
                    main.make_policy(policy("bad", spec))

    def test_load_skips_host_network_finished_and_unscheduled_pods_and_adds_the_name_label(self):
        c = main.load_cluster([load("pods.json"), load("namespaces.json")])
        keys = [p.key for p in c.pods]
        self.assertIn(FRONTEND, keys)
        self.assertNotIn("kube-system/etcd-cc-smoke-control-plane", keys)
        self.assertNotIn(f"{NS}/migrate-29k7d", keys)
        self.assertTrue(any("hostNetwork" in n for n in c.notes))
        self.assertTrue(any("phase Succeeded" in n for n in c.notes))
        bare = main.load_cluster([{"kind": "PodList", "items": [{"metadata": {"name": "x", "namespace": "y"}, "status": {"podIP": "10.9.9.9"}}, {"metadata": {"name": "pending", "namespace": "y"}}]}])
        self.assertEqual([p.key for p in bare.pods], ["y/x"])
        self.assertEqual(bare.namespace_labels("y"), {main.NAME_LABEL: "y"})
        self.assertTrue(any("no pod IP" in n for n in bare.notes))
        self.assertEqual(main.load_cluster([namespace("plain")]).namespace_labels("plain"), {main.NAME_LABEL: "plain"})

    def test_a_later_file_replaces_a_policy_of_the_same_name(self):
        c = main.load_cluster([load("06-db-allow-and.json"), load("07-db-allow-or.json")])
        self.assertEqual(len(c.policies), 1)
        self.assertEqual(len(c.policies[0].ingress[0].peers), 2)


class EvaluationTests(unittest.TestCase):
    def test_a_pod_no_policy_selects_is_open_in_both_directions(self):
        c = cluster(*SHOP)
        self.assertTrue(allowed(c, "shop/web", "shop/api", 8081))
        self.assertTrue(allowed(c, "tools/scan", "shop/web", 22))

    def test_isolation_is_per_direction(self):
        c = cluster(*SHOP, policy("ingress-only", {"podSelector": {"matchLabels": {"app": "api"}}, "policyTypes": ["Ingress"]}))
        self.assertFalse(allowed(c, "shop/web", "shop/api", 8081))
        self.assertTrue(allowed(c, "shop/api", "shop/web", 8080))

    def test_allowed_traffic_is_the_union_of_every_policy_that_selects_the_pod(self):
        c = cluster(
            *SHOP,
            policy("from-web", {"podSelector": {"matchLabels": {"app": "api"}}, "ingress": [{"from": [{"podSelector": {"matchLabels": {"app": "web"}}}]}]}),
            policy("from-tools", {"podSelector": {"matchLabels": {"app": "api"}}, "ingress": [{"from": [{"namespaceSelector": {"matchLabels": {"team": "platform"}}}]}]}),
        )
        self.assertTrue(allowed(c, "shop/web", "shop/api", 8081))
        self.assertTrue(allowed(c, "tools/scan", "shop/api", 8081))
        side = main.evaluate(c, main.resolve_endpoint(c, "tools/scan", []), main.resolve_endpoint(c, "shop/api", []), 8081, "TCP").ingress
        self.assertEqual(side.isolating, ["from-tools", "from-web"])
        self.assertEqual(side.deciding.where, "from-tools ingress[0]")

    def test_pod_selector_alone_means_the_policy_namespace_and_one_element_means_and(self):
        same_ns = policy("p", {"podSelector": {"matchLabels": {"app": "web"}}, "ingress": [{"from": [{"podSelector": {"matchLabels": {"app": "api"}}}]}]})
        c = cluster(*SHOP, same_ns)
        self.assertTrue(allowed(c, "shop/api", "shop/web", 8080))
        self.assertFalse(allowed(c, "tools/api", "shop/web", 8080))
        both = policy("p", {"podSelector": {"matchLabels": {"app": "web"}}, "ingress": [{"from": [{"namespaceSelector": {"matchLabels": {"team": "platform"}}, "podSelector": {"matchLabels": {"app": "api"}}}]}]})
        c = cluster(*SHOP, both)
        self.assertTrue(allowed(c, "tools/api", "shop/web", 8080))
        self.assertFalse(allowed(c, "tools/scan", "shop/web", 8080))
        self.assertFalse(allowed(c, "shop/api", "shop/web", 8080))
        two = policy("p", {"podSelector": {"matchLabels": {"app": "web"}}, "ingress": [{"from": [{"namespaceSelector": {"matchLabels": {"team": "platform"}}}, {"podSelector": {"matchLabels": {"app": "api"}}}]}]})
        c = cluster(*SHOP, two)
        self.assertTrue(allowed(c, "tools/scan", "shop/web", 8080))
        self.assertTrue(allowed(c, "shop/api", "shop/web", 8080))

    def test_empty_selectors_in_a_peer_and_an_empty_rule(self):
        own = policy("p", {"podSelector": {"matchLabels": {"app": "web"}}, "ingress": [{"from": [{"podSelector": {}}]}]})
        c = cluster(*SHOP, own)
        self.assertTrue(allowed(c, "shop/api", "shop/web", 8080))
        self.assertFalse(allowed(c, "tools/scan", "shop/web", 8080))
        everywhere = policy("p", {"podSelector": {"matchLabels": {"app": "web"}}, "ingress": [{"from": [{"namespaceSelector": {}}]}]})
        c = cluster(*SHOP, everywhere)
        self.assertTrue(allowed(c, "tools/scan", "shop/web", 8080))
        self.assertFalse(allowed(c, "192.0.2.10", "shop/web", 8080))
        open_rule = policy("p", {"podSelector": {"matchLabels": {"app": "web"}}, "ingress": [{}]})
        self.assertTrue(allowed(cluster(*SHOP, open_rule), "192.0.2.10", "shop/web", 8080))

    def test_ip_block_with_except_and_named_ports_never_match_outside_addresses(self):
        rule = {"to": [{"ipBlock": {"cidr": "172.18.0.0/16", "except": ["172.18.0.12/32"]}}], "ports": [{"port": 8000, "endPort": 8099}]}
        c = cluster(*SHOP, policy("p", {"podSelector": {"matchLabels": {"app": "web"}}, "policyTypes": ["Egress"], "egress": [rule]}))
        externals = main.externals_from(["a=172.18.0.11:8080", "b=172.18.0.12:8080"])
        self.assertTrue(allowed(c, "shop/web", "a", 8080, externals=externals))
        self.assertTrue(allowed(c, "shop/web", "a", 8099, externals=externals))
        self.assertFalse(allowed(c, "shop/web", "a", 8100, externals=externals))
        self.assertFalse(allowed(c, "shop/web", "b", 8080, externals=externals))
        self.assertFalse(allowed(c, "shop/web", "shop/api", 8081))
        named = cluster(*SHOP, policy("p", {"podSelector": {"matchLabels": {"app": "web"}}, "policyTypes": ["Egress"], "egress": [{"ports": [{"port": "http"}]}]}))
        self.assertFalse(allowed(named, "shop/web", "a", 8080, externals=externals))

    def test_ip_block_matches_pod_ips_too(self):
        c = cluster(*SHOP, policy("p", {"podSelector": {"matchLabels": {"app": "api"}}, "ingress": [{"from": [{"ipBlock": {"cidr": "10.0.1.0/24"}}]}]}))
        self.assertTrue(allowed(c, "tools/scan", "shop/api", 8081))
        self.assertFalse(allowed(c, "shop/web", "shop/api", 8081))

    def test_named_ports_resolve_on_the_destination_pod_and_protocols_must_match(self):
        c = cluster(*SHOP, policy("p", {"podSelector": {}, "ingress": [{"ports": [{"port": "http"}, {"protocol": "UDP"}]}]}))
        self.assertTrue(allowed(c, "shop/web", "shop/api", 8081))
        self.assertFalse(allowed(c, "shop/web", "shop/api", 8080))
        self.assertTrue(allowed(c, "shop/api", "shop/web", 8080))
        self.assertFalse(allowed(c, "shop/web", "shop/api", 9000))
        self.assertTrue(allowed(c, "shop/web", "shop/api", 9000, "UDP"))
        self.assertFalse(allowed(c, "shop/web", "shop/api", 9000, "SCTP"))

    def test_both_ends_must_allow_and_explain_names_the_blocking_side(self):
        c = lab_cluster(2)
        verdict = main.evaluate(c, main.resolve_endpoint(c, "frontend", []), main.resolve_endpoint(c, "api", []), 8080, "TCP")
        self.assertFalse(verdict.allowed)
        self.assertTrue(verdict.ingress.allowed)
        self.assertEqual(verdict.ingress.deciding.where, "api-allow-frontend ingress[0]")
        self.assertFalse(verdict.egress.allowed)
        self.assertEqual(verdict.egress.isolating, ["default-deny"])
        text = main.render_explain(verdict)
        self.assertIn("default-deny: no egress rules, it only isolates", text)
        self.assertIn("change the egress side of cc-network-policy/frontend", text)
        self.assertIn("ports[0] http/TCP (named) is 8080 on cc-network-policy/api", text)


class LabMatrixTests(unittest.TestCase):
    def test_every_step_predicts_exactly_the_expected_connections(self):
        for index, step in enumerate(EXPECTED["steps"]):
            with self.subTest(step=step["name"]):
                matrix, _ = lab_matrix(index)
                self.assertEqual(len(matrix.cells), 48)
                got = sorted(c.key for c in matrix.cells if c.allowed)
                want = sorted(c.key for c in matrix.cells) if step["allowed"] == "*" else sorted(step["allowed"])
                self.assertEqual(got, want)

    def test_matrix_text_and_json(self):
        matrix, c = lab_matrix(7)
        text = main.render_matrix(matrix, c)
        self.assertIn("            api   db    frontend  scanner  ext-a  ext-b  dns", text)
        self.assertIn("  frontend  8080  -     .         -        -      -      53/UDP", text)
        self.assertIn("  scanner   -     5432  -         .        8080   8080   53/UDP", text)
        self.assertIn("9 of 48 connections allowed", text)
        doc = main.matrix_json(matrix, c)
        self.assertEqual((doc["allowed"], doc["total"]), (9, 48))
        types = {p["key"]: p["policyTypes"] for p in doc["policies"]}
        self.assertEqual(types[f"{NS}/api-allow-frontend"], ["Ingress"])
        self.assertEqual(types[f"{NS}/default-deny"], ["Ingress", "Egress"])
        frontend = next(p for p in doc["pods"] if p["key"] == FRONTEND)
        self.assertEqual(frontend["egress"], ["allow-dns", "default-deny", "frontend-egress-api"])
        cell = next(x for x in doc["cells"] if x["from"] == API and x["to"] == DB and x["port"] == 5432)
        self.assertEqual(cell["reasons"], ["egress: isolated by allow-dns, api-egress-external, default-deny, nothing allows it", "ingress: allowed by db-allow ingress[0]"])

    def test_dns_is_partial_when_only_one_kube_dns_pod_is_allowed(self):
        partial = policy("one-dns", {"podSelector": {}, "policyTypes": ["Egress"], "egress": [{"to": [{"ipBlock": {"cidr": "10.244.0.2/32"}}]}]}, ns=NS)
        c = main.load_cluster([load("pods.json"), load("namespaces.json"), load("01-default-deny.json"), partial])
        matrix = main.build_matrix(c, [NS], dns=True)
        cell = next(x for x in matrix.cells if x.source == FRONTEND and x.target == "dns")
        self.assertEqual(cell.state, main.PARTIAL)
        self.assertIn("1 of 2 kube-dns pods allowed", cell.reasons[0])
        self.assertIn("~53/UDP", main.render_matrix(matrix, c))

    def test_workload_manifests_match_the_pods_fixture(self):
        manifests = {(m["metadata"]["namespace"], m["metadata"]["name"]): m for m in load("workloads.json")["items"]}
        fixtures = {(p["metadata"]["namespace"], p["metadata"]["name"]): p for p in load("pods.json")["items"]}
        for key, manifest in manifests.items():
            self.assertEqual(manifest["metadata"]["labels"], fixtures[key]["metadata"]["labels"])
            self.assertEqual(manifest["spec"]["containers"][0]["ports"], fixtures[key]["spec"]["containers"][0]["ports"])
            self.assertEqual(manifest["spec"]["containers"][0]["command"][-6:], ["--port", "8080", "--port", "9090", "--port", "5432"])


class LintTests(unittest.TestCase):
    def codes(self, *docs):
        return {(f.subject, f.code): f for f in main.lint(main.load_cluster(list(docs)))}

    def test_every_trap_in_the_traps_fixture_is_reported(self):
        with open(os.path.join(FIXTURES, "traps.json"), encoding="utf-8") as handle:
            traps = json.load(handle)
        found = self.codes(load("pods.json"), load("namespaces.json"), traps)
        for name, code in [
            ("deny-egress-typo", "egress-not-isolated"),
            ("no-selector", "podselector-missing"),
            ("tools-or", "or-peers"),
            ("ns-by-name", "peer-selects-nothing"),
            ("typo-port", "named-port-missing"),
            ("pod-cidr", "ipblock-pods"),
            ("ignored-ingress", "ignored-rules"),
            ("open-door", "allow-all"),
            ("legacy-worker", "selects-nothing"),
        ]:
            with self.subTest(code=code):
                self.assertIn((f"{NS}/{name}", code), found)
        self.assertIn("put both selectors in one element", found[(f"{NS}/tools-or", "or-peers")].message)
        self.assertIn("kubernetes.io/metadata.name", found[(f"{NS}/ns-by-name", "peer-selects-nothing")].message)
        self.assertEqual(found[(f"{NS}/open-door", "allow-all")].severity, main.INFO)

    def test_dns_egress_is_flagged_until_a_rule_allows_kube_dns(self):
        base = [load("pods.json"), load("namespaces.json"), load("01-default-deny.json"), load("03-frontend-egress-api.json")]
        found = self.codes(*base)
        self.assertIn((NS, "dns-egress"), found)
        self.assertIn(FRONTEND, found[(NS, "dns-egress")].message)
        self.assertNotIn((NS, "dns-egress"), self.codes(*base, load("04-allow-dns.json")))
        udp_only = policy("udp-dns", {"podSelector": {}, "policyTypes": ["Egress"], "egress": [{"ports": [{"protocol": "UDP", "port": 53}]}]}, ns=NS)
        self.assertEqual(self.codes(*base, udp_only)[(NS, "dns-tcp")].severity, main.INFO)
        policy_only = self.codes(load("01-default-deny.json"))
        self.assertIn((NS, "dns-egress"), policy_only)

    def test_the_lab_policies_lint_clean_apart_from_information(self):
        docs = [load("pods.json"), load("namespaces.json")] + [load(s["apply"]) for s in EXPECTED["steps"] if s["apply"]]
        findings = main.lint(main.load_cluster(docs))
        self.assertEqual([f for f in findings if f.severity == main.WARNING and f.code != "or-peers"], [])
        self.assertTrue(any(f.code == "or-peers" and f.subject == f"{NS}/db-allow" for f in findings))


class ProbeTests(unittest.TestCase):
    def test_server_accepts_until_stopped_and_tcp_probe_reports_refused_after(self):
        server = main.Server([0, 0], "127.0.0.1", "test")
        ports = server.start()
        try:
            for port in ports:
                self.assertEqual(main.tcp_probe("127.0.0.1", port, 2.0), (True, "connected"))
                with socket.create_connection(("127.0.0.1", port), timeout=2) as sock:
                    self.assertEqual(sock.recv(64).decode(), f"test {port}\n")
        finally:
            server.stop()
        self.assertEqual(main.tcp_probe("127.0.0.1", ports[0], 2.0), (False, "refused"))

    def test_dns_query_bytes_and_a_probe_against_a_fake_server(self):
        self.assertEqual(main.dns_query("a.bc.", 0x1234), bytes.fromhex("123401000001000000000000") + b"\x01a\x02bc\x00" + bytes.fromhex("00010001"))
        responder = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        responder.bind(("127.0.0.1", 0))
        responder.settimeout(3)

        def answer():
            data, peer = responder.recvfrom(512)
            responder.sendto(b"\x00\x00" + data[2:], peer)
            responder.sendto(data[:2] + struct.pack("!BB", 0x81, 0x83) + data[4:], peer)

        thread = threading.Thread(target=answer, daemon=True)
        thread.start()
        try:
            self.assertEqual(main.dns_probe("127.0.0.1", "kubernetes.default.svc.cluster.local", 2.0, responder.getsockname()[1]), (True, "answer NXDOMAIN"))
        finally:
            thread.join(3)
            responder.close()
        silent = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        silent.bind(("127.0.0.1", 0))
        try:
            self.assertEqual(main.dns_probe("127.0.0.1", "x.example", 0.2, silent.getsockname()[1]), (False, "timeout"))
        finally:
            silent.close()

    def test_nameserver_comes_from_resolv_conf(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "resolv.conf")
            with open(path, "w", encoding="utf-8") as handle:
                handle.write("search cc-network-policy.svc.cluster.local svc.cluster.local\nnameserver 10.96.0.10\noptions ndots:5\n")
            self.assertEqual(main.nameserver(path), "10.96.0.10")
            with open(path, "w", encoding="utf-8") as handle:
                handle.write("options ndots:5\n")
            with self.assertRaisesRegex(main.InputError, "no nameserver"):
                main.nameserver(path)

    def test_probe_command_tries_only_its_own_row_and_compare_reports_differences(self):
        server = main.Server([0], "127.0.0.1")
        open_port = server.start()[0]
        closer = socket.socket()
        closer.bind(("127.0.0.1", 0))
        closed_port = closer.getsockname()[1]
        closer.close()
        try:
            predicted = {"cells": [
                {"from": "a/x", "to": "a/y", "kind": "pod", "address": "127.0.0.1", "port": open_port, "protocol": "TCP", "state": "allowed", "allowed": True},
                {"from": "a/x", "to": "a/z", "kind": "pod", "address": "127.0.0.1", "port": closed_port, "protocol": "TCP", "state": "allowed", "allowed": True},
                {"from": "a/x", "to": "a/z", "kind": "pod", "address": "127.0.0.1", "port": 53, "protocol": "UDP", "state": "blocked", "allowed": False},
                {"from": "a/y", "to": "a/x", "kind": "pod", "address": "127.0.0.1", "port": open_port, "protocol": "TCP", "state": "blocked", "allowed": False},
            ]}
            code, out, err = run(["probe", "--from", "a/x", "--timeout", "1", "-"], stdin=json.dumps(predicted))
            self.assertEqual(code, 0, err)
            measured = json.loads(out)
            self.assertEqual([c["result"].split(":")[0] for c in measured["cells"]], ["connected", "refused", "not probed"])
        finally:
            server.stop()
        report = main.compare(predicted, [measured])
        self.assertFalse(report["equal"])
        self.assertEqual(report["compared"], 2)
        self.assertEqual(len(report["differ"]), 1)
        self.assertIn("not a policy decision", report["differ"][0])
        self.assertEqual(len(report["skipped"]), 1)
        measured["cells"][1]["allowed"], measured["cells"][1]["result"] = True, "connected"
        self.assertTrue(main.compare(predicted, [measured])["equal"])


class CliTests(unittest.TestCase):
    def lab_args(self, step):
        args = ["-f", os.path.join(LAB, "pods.json"), "-f", os.path.join(LAB, "namespaces.json")]
        for s in EXPECTED["steps"][: step + 1]:
            if s["apply"]:
                args += ["-f", os.path.join(LAB, s["apply"])]
        return args

    def test_matrix_json_from_the_command_line(self):
        argv = ["matrix", *self.lab_args(5), "--namespace", NS, "--namespace", TOOLS, "--dns", "--json"]
        for port in EXPECTED["probe_ports"]:
            argv += ["--port", port]
        for external in EXPECTED["externals"]:
            argv += ["--external", external]
        code, out, err = run(argv)
        self.assertEqual(code, 0, err)
        doc = json.loads(out)
        allowed_keys = sorted(f"{c['from']} -> {c['to']} {c['port']}/{c['protocol']}" for c in doc["cells"] if c["allowed"])
        self.assertEqual(allowed_keys, sorted(EXPECTED["steps"][5]["allowed"]))
        self.assertEqual(next(c for c in doc["cells"] if c["to"] == "ext-b")["address"], "172.18.0.12")

    def test_explain_exit_codes_and_named_port_resolution(self):
        code, out, _ = run(["explain", *self.lab_args(7), "--from", "scanner", "--to", f"{NS}/db", "--port", "postgres", "--json"])
        self.assertEqual(code, 0)
        doc = json.loads(out)
        self.assertEqual((doc["port"], doc["egress"]["pod"], doc["ingress"]["allowed_by"]), (5432, SCANNER, "db-allow ingress[0]"))
        code, out, _ = run(["explain", *self.lab_args(7), "--from", "api", "--to", "db", "--port", "5432"])
        self.assertEqual(code, 1)
        self.assertIn("DENIED", out)
        self.assertIn("allowed by db-allow ingress[0]: from[1] pods app=api in namespace cc-network-policy", out)

    def test_lint_exit_codes(self):
        code, out, _ = run(["lint", "-f", os.path.join(LAB, "01-default-deny.json")])
        self.assertEqual(code, 1)
        self.assertIn("dns-egress", out)
        code, out, _ = run(["lint", "-f", os.path.join(LAB, "04-allow-dns.json"), "--json"])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["policies"][0]["policyTypes"], ["Egress"])

    def test_bad_input_exits_2_with_the_reason(self):
        with tempfile.TemporaryDirectory() as tmp:
            bad = os.path.join(tmp, "bad.json")
            with open(bad, "w", encoding="utf-8") as handle:
                handle.write("apiVersion: v1\n")
            code, _, err = run(["lint", "-f", bad])
            self.assertEqual(code, 2)
            self.assertIn("not JSON", err)
        cases = [
            (["explain", *self.lab_args(0), "--from", "nobody", "--to", "api", "--port", "80"], "no pod, IP or --external named nobody"),
            (["explain", *self.lab_args(0), "--from", "frontend", "--to", "db", "--port", "http"], "declares no container port named http"),
            (["matrix", *self.lab_args(0), "--external", "ext=not-an-ip:80"], "is not an IP address"),
            (["matrix", *self.lab_args(0), "--port", "eighty"], "expected PORT or PORT/PROTOCOL"),
            (["matrix", "-f", os.path.join(LAB, "namespaces.json"), "--namespace", NS], "no pods with an IP in namespace"),
        ]
        for argv, message in cases:
            with self.subTest(message=message):
                code, _, err = run(argv)
                self.assertEqual(code, 2)
                self.assertIn(message, err)


if __name__ == "__main__":
    unittest.main()
