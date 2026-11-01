import json
import os
import shutil
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import main  # noqa: E402

ADDR_JSON = """[
  {"ifindex": 1, "ifname": "lo", "flags": ["LOOPBACK", "UP", "LOWER_UP"], "mtu": 65536, "qdisc": "noqueue", "operstate": "UNKNOWN", "group": "default", "txqlen": 1000, "link_type": "loopback", "address": "00:00:00:00:00:00", "broadcast": "00:00:00:00:00:00",
   "addr_info": [{"family": "inet", "local": "127.0.0.1", "prefixlen": 8, "scope": "host", "label": "lo", "valid_life_time": 4294967295, "preferred_life_time": 4294967295},
                 {"family": "inet6", "local": "::1", "prefixlen": 128, "scope": "host", "valid_life_time": 4294967295, "preferred_life_time": 4294967295}]},
  {"ifindex": 2, "link_index": 7, "ifname": "eth0", "flags": ["BROADCAST", "MULTICAST", "UP", "LOWER_UP"], "mtu": 1500, "qdisc": "noqueue", "operstate": "UP", "group": "default", "txqlen": 1000, "link_type": "ether", "address": "8a:3f:1c:9d:42:e1", "broadcast": "ff:ff:ff:ff:ff:ff", "link_netnsid": 0,
   "addr_info": [{"family": "inet", "local": "10.90.0.2", "prefixlen": 24, "broadcast": "10.90.0.255", "scope": "global", "label": "eth0", "valid_life_time": 4294967295, "preferred_life_time": 4294967295},
                 {"family": "inet6", "local": "fe80::883f:1cff:fe9d:42e1", "prefixlen": 64, "scope": "link", "valid_life_time": 4294967295, "preferred_life_time": 4294967295}]}
]"""

ROUTE_JSON = """[
  {"dst": "default", "gateway": "10.90.0.1", "dev": "eth0", "flags": []},
  {"dst": "10.90.0.0/24", "dev": "eth0", "protocol": "kernel", "scope": "link", "prefsrc": "10.90.0.2", "flags": []}
]"""

HOST_JSON = """[
  {"ifindex": 1, "ifname": "lo", "flags": ["LOOPBACK", "UP", "LOWER_UP"], "mtu": 65536, "operstate": "UNKNOWN", "link_type": "loopback", "address": "00:00:00:00:00:00",
   "addr_info": [{"family": "inet", "local": "127.0.0.1", "prefixlen": 8, "scope": "host", "label": "lo"}]},
  {"ifindex": 2, "ifname": "ens5", "flags": ["BROADCAST", "MULTICAST", "UP", "LOWER_UP"], "mtu": 9001, "qdisc": "mq", "operstate": "UP", "link_type": "ether", "address": "0a:58:c0:a8:01:0a",
   "addr_info": [{"family": "inet", "local": "192.168.1.10", "prefixlen": 24, "scope": "global", "label": "ens5"}]},
  {"ifindex": 3, "ifname": "docker0", "flags": ["NO-CARRIER", "BROADCAST", "MULTICAST", "UP"], "mtu": 1500, "qdisc": "noqueue", "operstate": "DOWN", "link_type": "ether", "address": "02:42:7a:11:0c:51",
   "addr_info": [{"family": "inet", "local": "172.17.0.1", "prefixlen": 16, "scope": "global", "label": "docker0"}]},
  {"ifindex": 6, "ifname": "cc0", "flags": ["BROADCAST", "MULTICAST", "UP", "LOWER_UP"], "mtu": 1500, "qdisc": "noqueue", "operstate": "UP", "link_type": "ether", "address": "9e:2b:60:77:a1:03",
   "addr_info": [{"family": "inet", "local": "10.90.0.1", "prefixlen": 24, "broadcast": "10.90.0.255", "scope": "global", "label": "cc0"}]},
  {"ifindex": 7, "link_index": 2, "ifname": "veth-web", "flags": ["BROADCAST", "MULTICAST", "UP", "LOWER_UP"], "mtu": 1500, "qdisc": "noqueue", "master": "cc0", "operstate": "UP", "link_type": "ether", "address": "9e:2b:60:77:a1:03", "link_netnsid": 0,
   "addr_info": [{"family": "inet6", "local": "fe80::9c2b:60ff:fe77:a103", "prefixlen": 64, "scope": "link"}]}
]"""

NFT_JSON = json.dumps({"nftables": [
    {"metainfo": {"version": "1.0.9", "release_name": "Old Doc Yak #3", "json_schema_version": 1}},
    {"table": {"family": "ip", "name": "filter", "handle": 3}},
    {"chain": {"family": "ip", "table": "filter", "name": "INPUT", "handle": 1, "type": "filter", "hook": "input", "prio": 0, "policy": "accept"}},
    {"rule": {"family": "ip", "table": "filter", "chain": "INPUT", "handle": 4, "expr": [{"match": {"op": "==", "left": {"meta": {"key": "iifname"}}, "right": "lo"}}, {"accept": None}]}},
    {"table": {"family": "ip", "name": "ccnet", "handle": 21}},
    {"chain": {"family": "ip", "table": "ccnet", "name": "prerouting", "handle": 1, "type": "nat", "hook": "prerouting", "prio": -100, "policy": "accept"}},
    {"chain": {"family": "ip", "table": "ccnet", "name": "output", "handle": 2, "type": "nat", "hook": "output", "prio": -100, "policy": "accept"}},
    {"chain": {"family": "ip", "table": "ccnet", "name": "postrouting", "handle": 3, "type": "nat", "hook": "postrouting", "prio": 100, "policy": "accept"}},
    {"chain": {"family": "ip", "table": "ccnet", "name": "publish", "handle": 4}},
    {"rule": {"family": "ip", "table": "ccnet", "chain": "prerouting", "handle": 5, "expr": [{"match": {"op": "==", "left": {"fib": {"result": "type", "flags": ["daddr"]}}, "right": "local"}}, {"jump": {"target": "publish"}}]}},
    {"rule": {"family": "ip", "table": "ccnet", "chain": "output", "handle": 6, "expr": [{"match": {"op": "==", "left": {"fib": {"result": "type", "flags": ["daddr"]}}, "right": "local"}}, {"jump": {"target": "publish"}}]}},
    {"rule": {"family": "ip", "table": "ccnet", "chain": "postrouting", "handle": 7, "expr": [{"match": {"op": "==", "left": {"payload": {"protocol": "ip", "field": "saddr"}}, "right": {"prefix": {"addr": "10.90.0.0", "len": 24}}}}, {"match": {"op": "!=", "left": {"meta": {"key": "oifname"}}, "right": "cc0"}}, {"masquerade": None}]}},
    {"rule": {"family": "ip", "table": "ccnet", "chain": "postrouting", "handle": 8, "expr": [{"match": {"op": "==", "left": {"meta": {"key": "oifname"}}, "right": "cc0"}}, {"match": {"op": "==", "left": {"fib": {"result": "type", "flags": ["saddr"]}}, "right": "local"}}, {"masquerade": None}]}},
    {"rule": {"family": "ip", "table": "ccnet", "chain": "publish", "handle": 9, "comment": "ccnet:web", "expr": [{"match": {"op": "==", "left": {"payload": {"protocol": "tcp", "field": "dport"}}, "right": 18080}}, {"dnat": {"addr": "10.90.0.2", "port": 80}}]}},
    {"rule": {"family": "ip", "table": "ccnet", "chain": "publish", "handle": 10, "comment": "ccnet:dns", "expr": [{"match": {"op": "==", "left": {"payload": {"protocol": "udp", "field": "dport"}}, "right": 5353}}, {"dnat": {"addr": "10.90.0.3", "port": 53}}]}},
]})

IPTABLES_SAVE = """# Generated by iptables-save v1.8.10 (nf_tables) on Fri Oct 31 10:12:44 2026
*nat
:PREROUTING ACCEPT [0:0]
:INPUT ACCEPT [0:0]
:OUTPUT ACCEPT [0:0]
:POSTROUTING ACCEPT [0:0]
:DOCKER - [0:0]
:CCNET - [0:0]
-A PREROUTING -m addrtype --dst-type LOCAL -j DOCKER
-A PREROUTING -m addrtype --dst-type LOCAL -j CCNET
-A OUTPUT ! -d 127.0.0.0/8 -m addrtype --dst-type LOCAL -j DOCKER
-A OUTPUT -m addrtype --dst-type LOCAL -j CCNET
-A POSTROUTING -s 172.17.0.0/16 ! -o docker0 -j MASQUERADE
-A POSTROUTING -s 172.17.0.2/32 -d 172.17.0.2/32 -p tcp -m tcp --dport 80 -j MASQUERADE
-A POSTROUTING -s 10.90.0.0/24 ! -o cc0 -j MASQUERADE
-A POSTROUTING -o cc0 -m addrtype --src-type LOCAL -j MASQUERADE
-A DOCKER -i docker0 -j RETURN
-A DOCKER ! -i docker0 -p tcp -m tcp --dport 18081 -j DNAT --to-destination 172.17.0.2:80
-A CCNET -p tcp -m tcp --dport 18080 -m comment --comment ccnet:web -j DNAT --to-destination 10.90.0.2:80
COMMIT
# Completed on Fri Oct 31 10:12:44 2026
"""


def write(path, text):
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(text)


def capture(argv):
    out, err = StringIO(), StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = main.main(argv)
    return code, out.getvalue(), err.getvalue()


class PublishAndNameTests(unittest.TestCase):
    def test_parse_publish_accepts_ports_and_protocols_and_rejects_garbage(self):
        self.assertEqual(main.parse_publish("18080:80"), main.Publish(18080, 80, "tcp"))
        self.assertEqual(main.parse_publish("5353:53/udp"), main.Publish(5353, 53, "udp"))
        self.assertEqual(str(main.parse_publish("8080:80")), "8080:80/tcp")
        for bad in ("80", "a:80", "80:b", "0:80", "80:70000", "80:80/sctp"):
            with self.assertRaises(ValueError, msg=bad):
                main.parse_publish(bad)

    def test_validate_name_keeps_veth_names_under_fifteen_characters(self):
        self.assertEqual(main.validate_name("web"), "web")
        self.assertEqual(main.validate_name("api-worker"), "api-worker")
        for bad in ("", "Web", "api_worker", "-web", "averyverylongname"):
            with self.assertRaises(ValueError, msg=bad):
                main.validate_name(bad)


class IpamTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="ccnet-")
        self.addCleanup(shutil.rmtree, self.dir)
        self.state = os.path.join(self.dir, "state.json")

    def test_allocate_skips_the_gateway_persists_and_release_reuses_the_lowest_address(self):
        net = main.load_state(self.state)
        self.assertEqual((net.cidr, net.gateway, net.prefixlen), ("10.90.0.0/24", "10.90.0.1", 24))
        web = main.allocate(net, "web", [main.parse_publish("18080:80")])
        db = main.allocate(net, "db")
        self.assertEqual((web.ip, db.ip, web.veth), ("10.90.0.2", "10.90.0.3", "veth-web"))
        main.save_state(net, self.state)
        reloaded = main.load_state(self.state)
        self.assertEqual(reloaded.containers["web"].publish, [main.Publish(18080, 80)])
        self.assertEqual(main.release(reloaded, "web").ip, "10.90.0.2")
        self.assertEqual(main.allocate(reloaded, "cache").ip, "10.90.0.2")
        self.assertEqual(main.allocate(reloaded, "queue").ip, "10.90.0.4")
        with self.assertRaises(KeyError):
            main.release(reloaded, "ghost")

    def test_allocate_refuses_duplicates_port_clashes_and_a_full_subnet(self):
        net = main.Network(cidr="10.90.0.0/29")
        main.allocate(net, "web", [main.parse_publish("8080:80")])
        with self.assertRaisesRegex(ValueError, "already has 10.90.0.2"):
            main.allocate(net, "web")
        with self.assertRaisesRegex(ValueError, "8080/tcp is already published by web"):
            main.allocate(net, "web2", [main.parse_publish("8080:80")])
        main.allocate(net, "udp", [main.parse_publish("8080:80/udp")])
        for name in ("c4", "c5", "c6"):
            self.assertEqual(main.allocate(net, name).ip, f"10.90.0.{name[1]}")
        with self.assertRaisesRegex(ValueError, "no free address in 10.90.0.0/29"):
            main.allocate(net, "c7")

    def test_render_ls_sorts_by_address(self):
        net = main.Network()
        main.allocate(net, "web", [main.parse_publish("18080:80")])
        main.allocate(net, "db")
        text = main.render_ls(net)
        self.assertRegex(text, r"NAME\s+ADDRESS\s+VETH\s+PUBLISHED")
        self.assertLess(text.index("web"), text.index("db"))
        self.assertIn("18080:80/tcp", text)
        self.assertEqual(main.render_ls(main.Network()), "no containers on cc0 (10.90.0.0/24, gateway 10.90.0.1)")


class PlanTests(unittest.TestCase):
    def setUp(self):
        self.net = main.Network()
        self.web = main.allocate(self.net, "web", [main.parse_publish("18080:80")])

    def test_detect_backend_prefers_nft_and_falls_back_to_iptables(self):
        self.assertEqual(main.detect_backend(lambda tool: "/usr/sbin/nft" if tool == "nft" else None), "nft")
        self.assertEqual(main.detect_backend(lambda tool: "/usr/sbin/iptables" if tool == "iptables" else None), "iptables")
        with self.assertRaises(RuntimeError):
            main.detect_backend(lambda tool: None)

    def test_network_plan_with_nft_has_bridge_forwarding_and_three_nat_rules(self):
        lines = main.plan_network(self.net, "nft")
        self.assertEqual(lines[:5], [
            "ip link add cc0 type bridge",
            "ip addr add 10.90.0.1/24 dev cc0",
            "ip link set cc0 up",
            "sysctl -qw net.ipv4.ip_forward=1",
            "sysctl -qw net.ipv4.conf.cc0.route_localnet=1",
        ])
        self.assertIn("nft 'add chain ip ccnet prerouting { type nat hook prerouting priority -100; policy accept; }'", lines)
        self.assertIn("nft 'add chain ip ccnet postrouting { type nat hook postrouting priority 100; policy accept; }'", lines)
        self.assertIn("nft add rule ip ccnet prerouting fib daddr type local jump publish", lines)
        self.assertIn("nft add rule ip ccnet output fib daddr type local jump publish", lines)
        self.assertEqual(lines[-2:], [
            "nft add rule ip ccnet postrouting ip saddr 10.90.0.0/24 oifname != cc0 masquerade",
            "nft add rule ip ccnet postrouting oifname cc0 fib saddr type local masquerade",
        ])

    def test_network_plan_with_iptables_uses_a_chain_reached_from_prerouting_and_output(self):
        lines = main.plan_network(self.net, "iptables")
        self.assertEqual(lines[5:], [
            "iptables -t nat -N CCNET",
            "iptables -t nat -A PREROUTING -m addrtype --dst-type LOCAL -j CCNET",
            "iptables -t nat -A OUTPUT -m addrtype --dst-type LOCAL -j CCNET",
            "iptables -t nat -A POSTROUTING -s 10.90.0.0/24 ! -o cc0 -j MASQUERADE",
            "iptables -t nat -A POSTROUTING -o cc0 -m addrtype --src-type LOCAL -j MASQUERADE",
        ])

    def test_container_plan_renders_the_exact_ip_commands_and_the_dnat_rule(self):
        lines = main.plan_container(self.net, self.web, "nft")
        self.assertEqual(lines, [
            "ip netns add web",
            "ip link add veth-web type veth peer name ceth-web",
            "ip link set ceth-web netns web",
            "ip -n web link set ceth-web name eth0",
            "ip link set veth-web master cc0 up",
            "ip -n web addr add 10.90.0.2/24 dev eth0",
            "ip -n web link set lo up",
            "ip -n web link set eth0 up",
            "ip -n web route add default via 10.90.0.1",
            "mkdir -p /etc/netns/web",
            "echo 'nameserver 1.1.1.1' > /etc/netns/web/resolv.conf",
            'nft add rule ip ccnet publish tcp dport 18080 dnat to 10.90.0.2:80 comment \\"ccnet:web\\"',
        ])
        iptables_lines = main.plan_container(self.net, self.web, "iptables", dns="10.0.0.53")
        self.assertEqual(iptables_lines[-2], "echo 'nameserver 10.0.0.53' > /etc/netns/web/resolv.conf")
        self.assertEqual(iptables_lines[-1], "iptables -t nat -A CCNET -p tcp --dport 18080 -m comment --comment ccnet:web -j DNAT --to-destination 10.90.0.2:80")

    def test_teardown_and_purge_plans_undo_everything_in_reverse(self):
        nft_down = main.plan_teardown(self.web, "nft")
        self.assertEqual(nft_down[0], "ip netns pids web | xargs -r kill")
        self.assertIn("nft -a list chain ip ccnet publish | awk '/\"ccnet:web\"/ {print $NF}' | xargs -rn1 nft delete rule ip ccnet publish handle", nft_down)
        self.assertEqual(nft_down[-2:], ["ip netns del web", "rm -rf /etc/netns/web"])
        ipt_down = main.plan_teardown(self.web, "iptables")
        self.assertIn("iptables -t nat -D CCNET -p tcp --dport 18080 -m comment --comment ccnet:web -j DNAT --to-destination 10.90.0.2:80", ipt_down)
        self.assertEqual(main.plan_teardown(main.Container("db", "10.90.0.3"), "nft"), ["ip netns pids db | xargs -r kill", "ip netns del db", "rm -rf /etc/netns/db"])
        self.assertEqual(main.plan_purge(self.net, "nft"), ["nft delete table ip ccnet", "ip link del cc0"])
        ipt_purge = main.plan_purge(self.net, "iptables")
        self.assertEqual(ipt_purge[-3:], ["iptables -t nat -F CCNET", "iptables -t nat -X CCNET", "ip link del cc0"])
        self.assertIn("iptables -t nat -D POSTROUTING -s 10.90.0.0/24 ! -o cc0 -j MASQUERADE", ipt_purge)


class ParserTests(unittest.TestCase):
    def test_parse_interfaces_reads_addresses_flags_peer_index_and_master(self):
        ns = main.parse_interfaces(ADDR_JSON)
        self.assertEqual(sorted(ns), ["eth0", "lo"])
        self.assertEqual(ns["eth0"].addresses, ["10.90.0.2/24"])
        self.assertTrue(ns["eth0"].up and ns["lo"].up)
        self.assertEqual((ns["eth0"].ifindex, ns["eth0"].link_index), (2, 7))
        self.assertTrue(ns["eth0"].has_address("10.90.0.2"))
        host = main.parse_interfaces(HOST_JSON)
        self.assertEqual(host["veth-web"].master, "cc0")
        self.assertEqual(host["cc0"].addresses, ["10.90.0.1/24"])
        self.assertEqual(host["veth-web"].addresses, [])
        self.assertEqual(main.parse_interfaces(""), {})

    def test_parse_routes_finds_the_default_gateway(self):
        routes = main.parse_routes(ROUTE_JSON)
        self.assertEqual(routes[0], main.Route("default", "eth0", "10.90.0.1"))
        self.assertEqual(routes[1], main.Route("10.90.0.0/24", "eth0", None))

    def test_parse_nft_ruleset_extracts_dnat_and_masquerade_and_ignores_filter_rules(self):
        rules = main.parse_nft_ruleset(NFT_JSON)
        self.assertEqual([r.kind for r in rules], ["masquerade", "masquerade", "dnat", "dnat"])
        egress, local, web, dns = rules
        self.assertEqual((egress.source, egress.out_iface, egress.out_negated, egress.chain), ("10.90.0.0/24", "cc0", True, "ccnet/postrouting"))
        self.assertEqual((local.source, local.out_iface, local.out_negated), (None, "cc0", False))
        self.assertEqual((web.proto, web.dport, web.to_ip, web.to_port, web.comment, web.chain), ("tcp", 18080, "10.90.0.2", 80, "ccnet:web", "ccnet/publish"))
        self.assertEqual((dns.proto, dns.dport, dns.to_ip, dns.to_port), ("udp", 5353, "10.90.0.3", 53))
        self.assertTrue(egress.covers_source("10.90.0.9"))
        self.assertFalse(egress.covers_source("172.17.0.2"))
        self.assertEqual(main.parse_nft_ruleset(""), [])

    def test_parse_iptables_save_reads_docker_and_ccnet_rules(self):
        rules = main.parse_iptables_save(IPTABLES_SAVE)
        self.assertEqual([r.kind for r in rules], ["masquerade", "masquerade", "masquerade", "masquerade", "dnat", "dnat"])
        docker_masq = rules[0]
        self.assertEqual((docker_masq.source, docker_masq.out_iface, docker_masq.out_negated, docker_masq.chain), ("172.17.0.0/16", "docker0", True, "nat/POSTROUTING"))
        docker_dnat = rules[4]
        self.assertEqual((docker_dnat.proto, docker_dnat.dport, docker_dnat.to_ip, docker_dnat.to_port, docker_dnat.chain), ("tcp", 18081, "172.17.0.2", 80, "nat/DOCKER"))
        ccnet_dnat = rules[5]
        self.assertEqual((ccnet_dnat.dport, ccnet_dnat.to_ip, ccnet_dnat.to_port, ccnet_dnat.comment), (18080, "10.90.0.2", 80, "ccnet:web"))
        self.assertEqual(rules[3].out_iface, "cc0")
        self.assertFalse(rules[3].out_negated)


class ExplainTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="ccnet-explain-")
        self.addCleanup(shutil.rmtree, self.dir)
        self.exp = main.Expectation("web", "10.90.0.2", "10.90.0.1", "cc0", [main.parse_publish("18080:80")])

    def fixture_dir(self, addr=ADDR_JSON, route=ROUTE_JSON, host=HOST_JSON, nat_json=NFT_JSON, nat_txt=None):
        for name in main.FIXTURE_FILES:
            path = os.path.join(self.dir, name)
            if os.path.exists(path):
                os.remove(path)
        write(os.path.join(self.dir, "addr.json"), addr)
        write(os.path.join(self.dir, "route.json"), route)
        write(os.path.join(self.dir, "host.json"), host)
        if nat_json is not None:
            write(os.path.join(self.dir, "nat.json"), nat_json)
        if nat_txt is not None:
            write(os.path.join(self.dir, "nat.txt"), nat_txt)
        return self.dir

    def test_healthy_container_passes_every_check_with_nft_or_iptables(self):
        for data in (main.gather_dir(self.fixture_dir()), main.gather_dir(self.fixture_dir(nat_json=None, nat_txt=IPTABLES_SAVE))):
            checks = main.explain_from_data(self.exp, data)
            self.assertTrue(all(c.ok for c in checks), [c.text for c in checks if not c.ok])
            self.assertEqual(len(checks), 8)
        text = main.render_explain(self.exp, checks)
        self.assertIn("web (10.90.0.2 on cc0, gateway 10.90.0.1)", text)
        self.assertIn("veth peer: veth-web (ifindex 7) master cc0 UP", text)
        self.assertIn("published 18080/tcp: DNAT to 10.90.0.2:80 (nat/CCNET)", text)
        self.assertIn("web: all 8 checks passed; reachable at 10.90.0.2, 18080->80/tcp", text)

    def test_broken_plumbing_is_named_check_by_check(self):
        addr = json.loads(ADDR_JSON)
        addr[1]["flags"] = ["BROADCAST", "MULTICAST"]
        addr[1]["operstate"] = "DOWN"
        host = json.loads(HOST_JSON)
        host[4]["master"] = None
        nat = json.loads(NFT_JSON)
        nat["nftables"] = [e for e in nat["nftables"] if not (e.get("rule") and e["rule"].get("comment") == "ccnet:web")]
        data = main.gather_dir(self.fixture_dir(addr=json.dumps(addr), route="[]", host=json.dumps(host), nat_json=json.dumps(nat)))
        checks = main.explain_from_data(self.exp, data)
        failed = [c.text for c in checks if not c.ok]
        self.assertEqual(len(failed), 4)
        self.assertIn("eth0 state: DOWN", failed)
        self.assertIn("default route: missing (expected via 10.90.0.1)", failed)
        self.assertIn("veth peer: veth-web (ifindex 7) master none UP (expected bridge cc0)", failed)
        self.assertIn("published 18080/tcp: no DNAT rule to 10.90.0.2:80; the host port is not forwarded", failed)
        self.assertIn("web: 4 of 8 checks failed", main.render_explain(self.exp, checks))

    def test_dnat_to_the_wrong_container_and_missing_masquerade_are_reported(self):
        exp = main.Expectation("web", "10.90.0.9", "10.90.0.1", "cc0", [main.parse_publish("18080:80")])
        nat = json.loads(NFT_JSON)
        nat["nftables"] = [e for e in nat["nftables"] if not (e.get("rule") and e["rule"]["chain"] == "postrouting")]
        data = main.gather_dir(self.fixture_dir(nat_json=json.dumps(nat)))
        failed = [c.text for c in main.explain_from_data(exp, data) if not c.ok]
        self.assertIn("published 18080/tcp: DNAT goes to 10.90.0.2:80 in ccnet/publish, not to 10.90.0.9:80", failed)
        self.assertIn("masquerade: no rule covers 10.90.0.9 leaving the host; replies to egress traffic would never come back", failed)

    def test_gather_dir_names_what_is_missing(self):
        write(os.path.join(self.dir, "addr.json"), ADDR_JSON)
        with self.assertRaisesRegex(FileNotFoundError, "missing: route.json, host.json"):
            main.gather_dir(self.dir)


class CliTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="ccnet-cli-")
        self.addCleanup(shutil.rmtree, self.dir)
        self.state = os.path.join(self.dir, "nested", "state.json")

    def test_allocate_plan_ls_release_round_trip(self):
        code, out, _ = capture(["--state", self.state, "allocate", "web", "--publish", "18080:80"])
        self.assertEqual((code, out.strip()), (0, "10.90.0.2"))
        code, out, _ = capture(["--state", self.state, "allocate", "db", "--json"])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["ip"], "10.90.0.3")
        code, out, _ = capture(["--state", self.state, "plan", "up", "web", "--backend", "iptables"])
        self.assertEqual(code, 0)
        self.assertIn("iptables -t nat -A CCNET -p tcp --dport 18080 -m comment --comment ccnet:web -j DNAT --to-destination 10.90.0.2:80", out)
        code, out, _ = capture(["--state", self.state, "plan", "up", "cache", "--backend", "nft"])
        self.assertIn("ip -n cache addr add 10.90.0.4/24 dev eth0", out)
        code, out, _ = capture(["--state", self.state, "plan", "network", "--backend", "nft"])
        self.assertIn("nft add table ip ccnet", out)
        code, out, _ = capture(["--state", self.state, "ls", "--json"])
        self.assertEqual(sorted(json.loads(out)["containers"]), ["db", "web"])
        code, out, _ = capture(["--state", self.state, "ls", "--names"])
        self.assertEqual(out.split(), ["db", "web"])
        code, out, _ = capture(["--state", self.state, "release", "web"])
        self.assertEqual((code, out.strip()), (0, "released 10.90.0.2 from web"))
        code, _, err = capture(["--state", self.state, "release", "web"])
        self.assertEqual(code, 1)
        self.assertIn("no container named 'web'", err)

    def test_allocate_errors_exit_one_with_a_reason(self):
        capture(["--state", self.state, "allocate", "web", "--publish", "18080:80"])
        code, _, err = capture(["--state", self.state, "allocate", "api", "--publish", "18080:80"])
        self.assertEqual(code, 1)
        self.assertIn("already published by web", err)
        code, _, err = capture(["--state", self.state, "allocate", "Bad_Name"])
        self.assertEqual(code, 1)
        self.assertIn("invalid name", err)
        code, _, err = capture(["--state", self.state, "plan", "up", "--backend", "nft"])
        self.assertEqual(code, 1)
        self.assertIn("needs a container name", err)

    def test_explain_from_dir_uses_state_or_flags_and_exits_one_on_failure(self):
        fixtures = os.path.join(self.dir, "fx")
        os.makedirs(fixtures)
        write(os.path.join(fixtures, "addr.json"), ADDR_JSON)
        write(os.path.join(fixtures, "route.json"), ROUTE_JSON)
        write(os.path.join(fixtures, "host.json"), HOST_JSON)
        write(os.path.join(fixtures, "nat.txt"), IPTABLES_SAVE)
        capture(["--state", self.state, "allocate", "web", "--publish", "18080:80"])
        code, out, _ = capture(["--state", self.state, "explain", "web", "--from-dir", fixtures, "--json"])
        self.assertEqual(code, 0)
        report = json.loads(out)
        self.assertTrue(report["ok"])
        self.assertEqual(len(report["checks"]), 8)
        code, out, _ = capture(["--state", self.state, "explain", "other", "--from-dir", fixtures, "--ip", "10.90.0.2", "--publish", "18080:80", "--publish", "19000:9000"])
        self.assertEqual(code, 1)
        self.assertIn("FAIL  published 19000/tcp: no DNAT rule to 10.90.0.2:9000", out)
        code, _, err = capture(["--state", self.state, "explain", "ghost", "--from-dir", fixtures])
        self.assertEqual(code, 1)
        self.assertIn("pass --ip", err)

    @unittest.skipIf(sys.platform == "linux", "the live path is exercised by code/smoke.sh")
    def test_explain_without_fixtures_needs_linux(self):
        capture(["--state", self.state, "allocate", "web"])
        code, _, err = capture(["--state", self.state, "explain", "web"])
        self.assertEqual(code, 2)
        self.assertIn("need Linux", err)


if __name__ == "__main__":
    unittest.main()
