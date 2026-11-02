import ipaddress
import json
import os
import random
import shutil
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, ".."))

import main  # noqa: E402

FIXTURES = os.path.join(HERE, "..", "fixtures")


def snapshot(name):
    return main.build_snapshot(main.read_dir(os.path.join(FIXTURES, name)))


def read_fixture(*parts):
    with open(os.path.join(FIXTURES, *parts), encoding="utf-8") as handle:
        return handle.read()


def get(name, dst, src=None, iif=None, mark=0):
    return main.route_get(snapshot(name), main.Query(dst, src, iif, mark))


def capture(argv):
    out, err = StringIO(), StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = main.main(argv)
    return code, out.getvalue(), err.getvalue()


def random_prefix(rng, version):
    bits = 32 if version == 4 else 128
    length = rng.randint(bits - 12, bits) if version == 4 else rng.choice([48, 56, 60, 63, 64, 64, 127, 128])
    base = (10 << 24) if version == 4 else (0x20010DB8 << 96)
    value = base | rng.getrandbits(bits - 16 if version == 4 else 32)
    return main.Prefix(version, value & main.mask_for(version, length), length)


class AddressTests(unittest.TestCase):
    def test_ipv4_parse_and_format_round_trip_and_refuse_ambiguous_text(self):
        for text in ("0.0.0.0", "10.0.1.10", "255.255.255.255", "192.0.2.1"):
            version, value = main.parse_ip(text)
            self.assertEqual((version, value), (4, int(ipaddress.IPv4Address(text))))
            self.assertEqual(main.format_ip(4, value), text)
        for bad in ("10.0.1", "10.0.1.256", "010.0.0.1", "10.0.0.1.2", "1e.0.0.1", "", "10.0.0.-1"):
            with self.assertRaises(main.InputError, msg=bad):
                main.parse_ip(bad)

    def test_ipv6_parsing_and_rfc5952_formatting_agree_with_ipaddress(self):
        samples = ["::", "::1", "1::", "2001:db8::1", "2001:0db8:0000:0000:0001:0000:0000:0001", "fe80::a8c1:abff:fe12:3401",
                   "2001:db8:0:0:1:0:0:0", "2001:db8:0:1:0:0:0:1", "1:0:0:2:0:0:0:3", "64:ff9b::192.0.2.33", "FE80::1%eth0", "1:2:3:4:5:6:7:8"]
        for text in samples:
            version, value = main.parse_ip(text)
            oracle = ipaddress.IPv6Address(text.split("%")[0])
            self.assertEqual((version, value), (6, int(oracle)), text)
            self.assertEqual(main.format_ip(6, value), oracle.compressed, text)
        for bad in ("1::2::3", "12345::", "1:2:3:4:5:6:7:8:9", "1:2:3:4:5:6:7", "::g", "1:2:3:4:5:6:7::8"):
            with self.assertRaises(main.InputError, msg=bad):
                main.parse_ip(bad)


class PrefixTests(unittest.TestCase):
    def test_masks_broadcast_and_host_ranges_match_ipaddress(self):
        for text in ("10.0.1.0/24", "10.0.1.128/25", "10.0.1.6/31", "10.0.1.7/32", "0.0.0.0/0", "2001:db8::/64", "2001:db8::1/128"):
            prefix = main.parse_prefix(text)
            oracle = ipaddress.ip_network(text)
            self.assertEqual(str(prefix), str(oracle))
            self.assertEqual(main.format_ip(prefix.version, prefix.mask), str(oracle.netmask))
            self.assertEqual(main.format_ip(prefix.version, prefix.hostmask), str(oracle.hostmask))
            self.assertEqual(prefix.size, oracle.num_addresses)
        report = main.prefix_report("10.0.1.77/24")
        self.assertEqual((report["prefix"], report["broadcast"], report["first"], report["last"], report["usable"], report["host"]),
                         ("10.0.1.0/24", "10.0.1.255", "10.0.1.1", "10.0.1.254", 254, "10.0.1.77"))
        point_to_point = main.prefix_report("10.0.1.6/31")
        self.assertEqual((point_to_point["broadcast"], point_to_point["first"], point_to_point["last"], point_to_point["usable"]), (None, "10.0.1.6", "10.0.1.7", 2))
        self.assertIsNone(main.prefix_report("2001:db8::/64")["broadcast"])
        with self.assertRaisesRegex(main.InputError, "host bits set; the network is 10.0.1.0/24"):
            main.parse_prefix("10.0.1.5/24", strict=True)
        for bad in ("10.0.1.0/33", "2001:db8::/129", "10.0.1.0/x"):
            with self.assertRaises(main.InputError, msg=bad):
                main.parse_prefix(bad)
        self.assertEqual(str(main.parse_prefix("default", 6)), "::/0")

    def test_cidr_blocks_either_nest_or_are_disjoint_and_overlap_matches_ipaddress(self):
        rng = random.Random(7)
        for version in (4, 6):
            for _ in range(300):
                a, b = random_prefix(rng, version), random_prefix(rng, version)
                oa, ob = ipaddress.ip_network(str(a)), ipaddress.ip_network(str(b))
                self.assertEqual(a.overlaps(b), oa.overlaps(ob), (a, b))
                self.assertEqual(a.overlaps(b), a.covers(b) or b.covers(a))
                self.assertEqual(a.covers(b), ob.subnet_of(oa), (a, b))
        self.assertEqual(main.relation(main.parse_prefix("10.0.0.0/16"), main.parse_prefix("10.0.3.0/24")), "10.0.0.0/16 contains 10.0.3.0/24")
        self.assertEqual(main.relation(main.parse_prefix("10.0.0.0/24"), main.parse_prefix("10.0.1.0/24")), "10.0.0.0/24 and 10.0.1.0/24 are disjoint")

    def test_collapse_matches_ipaddress_and_supernet_reports_the_extra_addresses(self):
        rng = random.Random(11)
        for version in (4, 6):
            for _ in range(60):
                items = [random_prefix(rng, version) for _ in range(rng.randint(1, 12))]
                oracle = ipaddress.collapse_addresses(ipaddress.ip_network(str(p)) for p in items)
                self.assertEqual([str(p) for p in main.collapse(items)], [str(n) for n in oracle])
        blocks = [main.parse_prefix(t) for t in ("10.0.0.0/24", "10.0.1.0/24", "10.0.3.0/24")]
        self.assertEqual([str(p) for p in main.collapse(blocks)], ["10.0.0.0/23", "10.0.3.0/24"])
        self.assertEqual(str(main.supernet(blocks)), "10.0.0.0/22")
        code, out, _ = capture(["summarise", "10.0.0.0/24", "10.0.1.0/24", "10.0.3.0/24", "--json"])
        self.assertEqual((code, json.loads(out)), (0, {"collapsed": ["10.0.0.0/23", "10.0.3.0/24"], "supernet": "10.0.0.0/22", "extra_addresses": 256}))
        with self.assertRaises(main.InputError):
            main.supernet([main.parse_prefix("10.0.0.0/8"), main.parse_prefix("2001:db8::/32")])
        self.assertEqual(capture(["contains", "10.0.0.0/16", "10.0.3.7"])[0], 0)
        self.assertEqual(capture(["contains", "10.0.0.0/16", "10.1.0.0/24"])[0], 1)


class ParserTests(unittest.TestCase):
    def test_routes_keep_type_table_metric_scope_and_ecmp_next_hops(self):
        routes = snapshot("router").routes
        by_text = {r.describe(): r for r in routes}
        self.assertIn("default via 10.0.3.10 dev r-u2 table 100", by_text)
        self.assertIn("blackhole 10.99.0.0/16", by_text)
        self.assertIn("local 10.0.1.1 dev r-c table local scope host src 10.0.1.1", by_text)
        ecmp = next(r for r in routes if len(r.nexthops) == 2)
        self.assertEqual([(h.gateway, h.dev, h.weight) for h in ecmp.nexthops], [("10.0.2.10", "r-u1", 1), ("10.0.3.10", "r-u2", 1)])
        host = next(r for r in routes if r.type == "local" and r.prefsrc == "10.0.2.1")
        self.assertEqual(host.prefix.length, 32)
        mixed = main.parse_routes(json.dumps([{"dst": "default", "gateway": "fe80::1", "dev": "c0", "metric": 1024, "pref": "medium"}, {"dst": "default", "table": 254, "dev": "wg0", "scope": "link"}]))
        self.assertEqual([(r.prefix.version, r.table, r.metric, r.scope) for r in mixed], [(6, "main", 1024, "global"), (4, "main", 0, "link")])
        with self.assertRaisesRegex(main.InputError, "not JSON"):
            main.parse_routes("default via 10.0.1.1 dev c0")

    def test_rules_parse_selectors_actions_and_defaults(self):
        rules = main.parse_rules(json.dumps([
            {"priority": 32766, "src": "all", "table": "main"},
            {"priority": 100, "src": "10.0.1.20", "table": "100"},
            {"priority": 90, "src": "10.0.0.0", "srclen": 16, "dst": "192.0.2.0", "dstlen": 24, "table": "200"},
            {"priority": 110, "src": "all", "fwmark": "0x10", "fwmask": "0xf0", "table": "100"},
            {"priority": 120, "not": None, "src": "10.0.1.0", "srclen": 24, "action": "prohibit"},
            {"priority": 130, "src": "all", "iif": "eth0", "goto": 32766},
            {"priority": 140, "src": "all", "table": "main", "suppress_prefixlength": 0},
            {"priority": 150, "src": "all", "ipproto": "tcp", "dport": 443, "table": "300"},
        ]))
        self.assertEqual([r.priority for r in rules], [90, 100, 110, 120, 130, 140, 150, 32766])
        self.assertEqual(rules[0].describe(), "90: from 10.0.0.0/16 to 192.0.2.0/24 lookup 200")
        self.assertEqual(rules[1].describe(), "100: from 10.0.1.20 lookup 100")
        self.assertEqual((rules[2].fwmark, rules[2].fwmask), (0x10, 0xF0))
        self.assertEqual((rules[3].action, rules[3].invert), ("prohibit", True))
        self.assertEqual((rules[4].action, rules[4].goto, rules[4].iif), ("goto", 32766, "eth0"))
        self.assertEqual(rules[5].suppress_prefixlength, 0)
        self.assertEqual(rules[6].unmodelled, ["ipproto", "dport"])
        defaults = main.Snapshot([], [], {4: [], 6: []}).rules_for(4)
        self.assertEqual([(r.priority, r.table) for r in defaults], [(0, "local"), (32766, "main"), (32767, "default")])

    def test_addresses_neighbours_and_sysctls(self):
        snap = snapshot("client")
        c0 = snap.interface("c0")
        self.assertEqual([(a.text, a.secondary, a.scope) for a in c0.addresses if a.version == 4], [("10.0.1.10", False, "global"), ("10.0.1.20", True, "global")])
        self.assertIn("NOARP", snap.interface("c-dum").flags)
        self.assertTrue(snap.is_local(4, main.parse_ip("10.0.1.20")[1]))
        self.assertEqual([(n.address, n.state) for n in snap.neighbours][:2], [("10.0.1.1", ["REACHABLE"]), ("10.0.1.250", ["FAILED"])])
        values = main.parse_sysctl("net.ipv4.ip_forward = 1\nnet.ipv4.conf.eth0/100.rp_filter = 2\ngarbage\nnet.ipv4.conf.all.rp_filter=1\n")
        snap.sysctl = values
        self.assertEqual((snap.sysctl_value("net.ipv4.ip_forward"), snap.sysctl_value("net.ipv4.conf.all.rp_filter"), snap.sysctl_value("net.ipv4.conf.eth0.100.rp_filter")), (1, 1, 2))
        with self.assertRaisesRegex(main.InputError, "addr.json"):
            main.build_snapshot({"route.json": "[]"})


class ClientLookupTests(unittest.TestCase):
    def assertRoute(self, decision, gateway, dev, src, table="main", kind="unicast"):
        self.assertTrue(decision.ok, decision.reason)
        self.assertEqual((decision.type, decision.hops[0].gateway, decision.hops[0].dev, decision.src, decision.table), (kind, gateway, dev, src, table))

    def test_connected_route_uses_its_prefsrc(self):
        self.assertRoute(get("client", "10.0.1.1"), None, "c0", "10.0.1.10")
        self.assertRoute(get("client", "172.16.0.77"), None, "c-dum", "172.16.0.1")

    def test_longest_prefix_wins_then_the_lower_metric(self):
        decision = get("client", "10.0.2.10")
        self.assertRoute(decision, "10.0.1.1", "c0", "10.0.1.10")
        self.assertEqual(decision.route.metric, 50)
        self.assertIn("/16 is the longest of 3 matching prefixes", decision.trace[-1])
        self.assertIn("metric 50 beats 100 for the same prefix", decision.trace[-1])
        self.assertRoute(get("client", "198.51.100.1"), "10.0.1.1", "c0", "10.0.1.10")

    def test_a_more_specific_route_with_src_changes_the_source_address(self):
        self.assertRoute(get("client", "10.0.3.10"), "10.0.1.1", "c0", "10.0.1.20")

    def test_source_rule_needs_a_source_and_then_selects_table_100(self):
        plain = get("client", "10.0.3.10")
        self.assertTrue(any("rule 100: from 10.0.1.20 lookup 100: skipped, source unset (0.0.0.0) is not in 10.0.1.20" in t for t in plain.trace))
        self.assertRoute(get("client", "10.0.3.10", src="10.0.1.20"), None, "c-dum", "10.0.1.20", table="100")
        self.assertRoute(get("client", "198.51.100.1", src="10.0.1.20"), "10.0.1.1", "c0", "10.0.1.20", table="100")
        self.assertEqual(main.route_line(get("client", "198.51.100.1", src="10.0.1.20")), "198.51.100.1 from 10.0.1.20 via 10.0.1.1 dev c0 table 100")

    def test_fwmark_rule_selects_table_100(self):
        decision = get("client", "198.51.100.1", mark=1)
        self.assertRoute(decision, "10.0.1.1", "c0", "10.0.1.10", table="100")
        self.assertTrue(any(t.startswith("rule 110: from all fwmark 0x1 lookup 100: matched") for t in decision.trace))
        self.assertTrue(get("client", "198.51.100.1", mark=2).table == "main")

    def test_blackhole_and_unreachable_routes_answer_with_their_errno(self):
        hole = get("client", "10.99.0.1")
        self.assertEqual((hole.ok, hole.type, hole.errno, hole.message, hole.table), (False, "blackhole", "EINVAL", "Invalid argument", "main"))
        unreachable = get("client", "10.66.255.1")
        self.assertEqual((unreachable.errno, unreachable.message), ("EHOSTUNREACH", "No route to host"))
        code, out, _ = capture(["get", "10.99.0.1", "--dir", os.path.join(FIXTURES, "client"), "--json"])
        self.assertEqual((code, json.loads(out)["errno"], json.loads(out)["route"]), (1, "EINVAL", "blackhole 10.99.0.0/16"))

    def test_own_addresses_are_local_on_lo_and_a_secondary_borrows_the_primary_source(self):
        self.assertRoute(get("client", "10.0.1.10"), None, "lo", "10.0.1.10", table="local", kind="local")
        self.assertRoute(get("client", "10.0.1.20"), None, "lo", "10.0.1.10", table="local", kind="local")
        self.assertEqual(main.route_line(get("client", "10.0.1.10")), "local 10.0.1.10 dev lo table local src 10.0.1.10")

    def test_neighbour_check_flags_a_failed_next_hop_and_skips_noarp_devices(self):
        failed = get("client", "10.0.5.1")
        self.assertRoute(failed, "10.0.1.250", "c0", "10.0.1.10")
        self.assertFalse(failed.neighbour_ok)
        self.assertIn("neighbour 10.0.1.250 dev c0: FAILED", failed.neighbours[0])
        self.assertTrue(get("client", "198.51.100.1").neighbour_ok)
        self.assertIn("NOARP", get("client", "172.16.0.9").neighbours[0])

    def test_source_selection_without_prefsrc_skips_secondaries_and_prefers_the_gateway_subnet(self):
        snap = main.build_snapshot({
            "addr.json": json.dumps([
                {"ifindex": 1, "ifname": "lo", "flags": ["LOOPBACK", "UP"], "addr_info": [{"family": "inet", "local": "127.0.0.1", "prefixlen": 8, "scope": "host"}, {"family": "inet", "local": "192.0.2.53", "prefixlen": 32, "scope": "global"}]},
                {"ifindex": 2, "ifname": "eth0", "flags": ["UP"], "addr_info": [
                    {"family": "inet", "local": "10.1.0.5", "prefixlen": 24, "scope": "global"},
                    {"family": "inet", "local": "10.1.0.6", "prefixlen": 24, "scope": "global", "secondary": True},
                    {"family": "inet", "local": "10.2.0.5", "prefixlen": 24, "scope": "global"}]},
                {"ifindex": 3, "ifname": "eth1", "flags": ["UP"], "addr_info": []}]),
            "route.json": json.dumps([
                {"dst": "default", "gateway": "10.2.0.1", "dev": "eth0"},
                {"dst": "172.20.0.0/16", "dev": "eth0", "scope": "link"},
                {"dst": "172.30.0.0/16", "dev": "eth1", "scope": "link"}]),
        })
        self.assertEqual(main.route_get(snap, main.Query("8.8.8.8")).src, "10.2.0.5")
        self.assertEqual(main.route_get(snap, main.Query("172.20.1.1")).src, "10.1.0.5")
        self.assertEqual(main.route_get(snap, main.Query("172.30.1.1")).src, "192.0.2.53")


class RouterLookupTests(unittest.TestCase):
    def test_forwarded_packets_follow_the_source_rule_even_past_a_connected_route(self):
        direct = get("router", "10.0.2.10", src="10.0.1.10", iif="r-c")
        self.assertEqual((direct.hops[0].gateway, direct.hops[0].dev, direct.table), (None, "r-u1", "main"))
        policy = get("router", "10.0.2.10", src="10.0.1.20", iif="r-c")
        self.assertEqual((policy.hops[0].gateway, policy.hops[0].dev, policy.table), ("10.0.3.10", "r-u2", "100"))
        self.assertEqual(main.route_line(policy), "10.0.2.10 from 10.0.1.20 via 10.0.3.10 dev r-u2 table 100 iif r-c")
        specific = get("router", "203.0.113.1", src="10.0.1.10", iif="r-c")
        self.assertEqual((specific.hops[0].gateway, specific.hops[0].dev), ("10.0.3.10", "r-u2"))
        local = get("router", "10.0.1.1", src="10.0.1.10", iif="r-c")
        self.assertEqual((local.type, local.hops[0].dev, local.table), ("local", "lo", "local"))

    def test_ecmp_lists_every_next_hop_with_its_weight(self):
        decision = get("router", "192.0.2.7", src="10.0.1.10", iif="r-c")
        self.assertEqual([(h.gateway, h.dev, h.weight) for h in decision.hops], [("10.0.2.10", "r-u1", 1), ("10.0.3.10", "r-u2", 1)])
        data = main.decision_json(decision)
        self.assertIsNone(data["dev"])
        self.assertEqual(len(data["nexthops"]), 2)
        self.assertIn("ECMP", main.route_line(decision))
        own = get("router", "192.0.2.7")
        self.assertEqual([h.src for h in own.hops], ["10.0.2.1", "10.0.3.1"])

    def test_forwarding_off_and_rp_filter_strict_and_loose(self):
        snap = snapshot("router")
        query = main.Query("198.51.100.1", "10.0.2.10", "r-c")
        self.assertTrue(main.route_get(snap, query).ok)
        snap.sysctl["net.ipv4.conf.r-c.rp_filter"] = 1
        strict = main.route_get(snap, query)
        self.assertEqual((strict.ok, strict.errno, strict.message), (False, "EXDEV", "Invalid cross-device link"))
        self.assertIn("leaves through r-u1, not r-c", strict.reason)
        self.assertTrue(main.route_get(snap, main.Query("198.51.100.1", "10.0.1.10", "r-c")).ok)
        self.assertEqual(main.route_get(snap, main.Query("198.51.100.1", "10.0.1.1", "r-c")).errno, "EINVAL")
        snap.sysctl["net.ipv4.conf.r-c.rp_filter"] = 2
        self.assertTrue(main.route_get(snap, query).ok)
        snap.sysctl["net.ipv4.conf.r-c.forwarding"] = 0
        off = main.route_get(snap, main.Query("198.51.100.1", "10.0.1.10", "r-c"))
        self.assertEqual((off.ok, off.errno), (False, "EHOSTUNREACH"))
        self.assertIn("forwarding is off on r-c", off.reason)
        self.assertTrue(main.route_get(snap, main.Query("10.0.1.1", "10.0.1.10", "r-c")).ok)

    def test_no_route_in_any_table_is_enetunreach_with_a_trace_per_rule(self):
        snap = main.build_snapshot({"addr.json": read_fixture("router", "addr.json"),
                                    "route.json": json.dumps([{"dst": "10.0.2.0/24", "dev": "r-u1", "scope": "link", "prefsrc": "10.0.2.1"},
                                                              {"type": "throw", "dst": "10.9.0.0/16", "table": "50"},
                                                              {"dst": "default", "gateway": "10.0.2.10", "dev": "r-u1", "table": "60"}]),
                                    "rule.json": json.dumps([{"priority": 0, "src": "all", "table": "local"}, {"priority": 10, "src": "all", "table": "50"},
                                                             {"priority": 20, "src": "all", "table": "60", "suppress_prefixlength": 0},
                                                             {"priority": 32766, "src": "all", "table": "main"}])})
        decision = main.route_get(snap, main.Query("10.9.1.1"))
        self.assertEqual((decision.ok, decision.errno, decision.message), (False, "ENETUNREACH", "Network is unreachable"))
        self.assertIn("hands the lookup back to the rules", decision.trace[2])
        self.assertIn("suppressed (/0 <= suppress_prefixlength 0)", decision.trace[3])
        self.assertIn("table main has no route for 10.9.1.1", decision.trace[4])

    def test_ipv6_longest_match_and_source_by_scope(self):
        default = get("client", "2001:db8:9::1")
        self.assertEqual((default.hops[0].gateway, default.hops[0].dev, default.src), ("2001:db8:1::1", "c0", "2001:db8:1::10"))
        link = get("client", "fe80::1")
        self.assertEqual((link.hops[0].gateway, link.src), (None, "fe80::a8c1:abff:fe12:3401"))
        self.assertEqual(get("client", "2001:db8:1::10").type, "local")


class CliTests(unittest.TestCase):
    def test_get_json_and_input_errors_exit_2(self):
        client = os.path.join(FIXTURES, "client")
        code, out, _ = capture(["get", "198.51.100.1", "--from", "10.0.1.20", "--dir", client, "--json"])
        data = json.loads(out)
        self.assertEqual((code, data["dev"], data["gateway"], data["src"], data["table"]), (0, "c0", "10.0.1.1", "10.0.1.20", "100"))
        for argv, fragment in ((["get", "10.0.1.300", "--dir", client], "not an IPv4 address"),
                               (["get", "198.51.100.1", "--iif", "c0", "--dir", client], "give its source with --from"),
                               (["get", "198.51.100.1", "--from", "10.9.9.9", "--dir", client], "not an address of this host"),
                               (["get", "198.51.100.1", "--from", "2001:db8:1::10", "--dir", client], "is IPv6"),
                               (["get", "198.51.100.1", "--dir", os.path.join(FIXTURES, "missing")], "not a directory")):
            code, _, err = capture(argv)
            self.assertEqual(code, 2, argv)
            self.assertIn(fragment, err)
        code, _, _ = capture(["get", "198.51.100.1", "--from", "10.0.1.10", "--iif", "nope", "--dir", os.path.join(FIXTURES, "router")])
        self.assertEqual(code, 1)

    def test_gather_runs_ip_in_the_namespace_and_maps_sysctl_paths(self):
        calls = []
        addr = read_fixture("router", "addr.json")

        def fake(argv):
            calls.append(list(argv))
            if argv[-2:] == ["-j", "addr"]:
                return addr
            if "rule" in argv and "-6" in argv:
                raise subprocess.CalledProcessError(2, argv, stderr="IPv6 disabled")
            if argv[:4] == ["ip", "netns", "exec", "cc-rt-r"]:
                return "/proc/sys/net/ipv4/ip_forward = 1\n/proc/sys/net/ipv4/conf/r-c/rp_filter = 2\n/proc/sys/net/ipv4/conf/gone/rp_filter = \n"
            return "[]"

        files = main.gather("cc-rt-r", fake)
        self.assertEqual(calls[0], ["ip", "-n", "cc-rt-r", "-j", "addr"])
        self.assertIn(["ip", "-n", "cc-rt-r", "-4", "-j", "route", "show", "table", "all"], calls)
        self.assertEqual(files["rule6.json"], "")
        self.assertEqual(main.parse_sysctl(files["sysctl.txt"]), {"net.ipv4.ip_forward": 1, "net.ipv4.conf.r-c.rp_filter": 2})
        self.assertIn("/proc/sys/net/ipv4/conf/r-u2/forwarding", calls[-1])
        out_dir = tempfile.mkdtemp(prefix="routelens-")
        self.addCleanup(shutil.rmtree, out_dir)
        for name, text in files.items():
            with open(os.path.join(out_dir, name), "w", encoding="utf-8") as handle:
                handle.write(text)
        self.assertEqual(len(main.build_snapshot(main.read_dir(out_dir)).interfaces), 4)


if __name__ == "__main__":
    unittest.main()
