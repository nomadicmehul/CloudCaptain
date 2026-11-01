import copy
import ipaddress
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
SHOP = os.path.join(FIXTURES, "shop")
QUESTIONS = os.path.join(FIXTURES, "questions.json")
BASE = main.read_documents([SHOP])
IP = ipaddress.IPv4Address


def snapshot(mutate=None):
    doc = copy.deepcopy(BASE)
    if mutate:
        mutate(doc)
    return main.build_snapshot(doc)


def item(doc, key, id_key, value):
    return next(x for x in doc[key] if x[id_key] == value)


def entry(acl, number, egress):
    return next(e for e in acl["Entries"] if e["RuleNumber"] == number and e["Egress"] == egress)


def table(routes):
    parsed = [main.make_route(r, "test") for r in routes]
    return main.RouteTable("rtb-test", "vpc-test", parsed, [], True)


def acl(*entries):
    parsed = [main.make_acl_entry(e, "test") for e in entries]
    return main.NetworkAcl("acl-test", "vpc-test", sorted(parsed, key=lambda e: (e.egress, e.number)), [], False)


def run(argv):
    out, err = StringIO(), StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = main.main(argv)
    return code, out.getvalue(), err.getvalue()


class RouteTests(unittest.TestCase):
    def test_longest_prefix_wins_as_in_the_aws_route_priority_example(self):
        rt = table(
            [
                {"DestinationCidrBlock": "10.0.0.0/16", "GatewayId": "local"},
                {"DestinationCidrBlock": "172.31.0.0/16", "VpcPeeringConnectionId": "pcx-11223344556677889"},
                {"DestinationCidrBlock": "0.0.0.0/0", "GatewayId": "igw-12345678901234567"},
            ]
        )
        snap = main.Snapshot()
        self.assertEqual(main.lookup(snap, rt, IP("172.31.4.5"))[0].target, "pcx-11223344556677889")
        self.assertEqual(main.lookup(snap, rt, IP("10.0.3.3"))[0].target, "local")
        self.assertEqual(main.lookup(snap, rt, IP("8.8.8.8"))[0].target, "igw-12345678901234567")
        route, matched, others = main.lookup(snap, rt, IP("172.31.4.5"))
        self.assertEqual(str(matched), "172.31.0.0/16")
        self.assertEqual(others, ["0.0.0.0/0 -> igw-12345678901234567"])

    def test_on_an_identical_destination_static_beats_prefix_list_beats_propagated(self):
        snap = main.Snapshot(prefix_lists={"pl-1": ("corp", [ipaddress.IPv4Network("172.31.0.0/24")])})
        rt = table(
            [
                {"DestinationCidrBlock": "172.31.0.0/24", "GatewayId": "vgw-1", "Origin": "EnableVgwRoutePropagation"},
                {"DestinationPrefixListId": "pl-1", "TransitGatewayId": "tgw-1"},
                {"DestinationCidrBlock": "172.31.0.0/24", "GatewayId": "igw-1"},
            ]
        )
        self.assertEqual(main.lookup(snap, rt, IP("172.31.0.9"))[0].target, "igw-1")
        rt.routes = [r for r in rt.routes if r.target != "igw-1"]
        self.assertEqual(main.lookup(snap, rt, IP("172.31.0.9"))[0].target, "tgw-1")
        rt.routes.append(main.make_route({"DestinationCidrBlock": "172.31.0.8/29", "GatewayId": "vgw-1", "Origin": "EnableVgwRoutePropagation"}, "t"))
        self.assertEqual(main.lookup(snap, rt, IP("172.31.0.9"))[0].target, "vgw-1", "a more specific route wins whatever its origin")
        self.assertIsNone(main.lookup(snap, table([{"DestinationCidrBlock": "10.0.0.0/16", "GatewayId": "local"}]), IP("8.8.8.8")))

    def test_a_blackhole_route_still_wins_and_drops_the_packet(self):
        def mutate(doc):
            routes = item(doc, "RouteTables", "RouteTableId", "rtb-0private")["Routes"]
            next(r for r in routes if r.get("NatGatewayId"))["State"] = "blackhole"

        answer = main.trace(snapshot(mutate), "batch", "internet", "tcp", 443)
        self.assertEqual((answer.verdict, answer.code), ("blocked", "BAD_STATE_ROUTE"))
        self.assertIn("blackhole", answer.reason)


class AclTests(unittest.TestCase):
    EXAMPLE = (
        {"RuleNumber": 100, "Egress": False, "Protocol": "6", "PortRange": {"From": 80, "To": 80}, "CidrBlock": "0.0.0.0/0", "RuleAction": "allow"},
        {"RuleNumber": 110, "Egress": False, "Protocol": "6", "PortRange": {"From": 443, "To": 443}, "CidrBlock": "0.0.0.0/0", "RuleAction": "allow"},
        {"RuleNumber": 120, "Egress": False, "Protocol": "6", "PortRange": {"From": 22, "To": 22}, "CidrBlock": "192.0.2.0/24", "RuleAction": "allow"},
        {"RuleNumber": 140, "Egress": False, "Protocol": "6", "PortRange": {"From": 32768, "To": 65535}, "CidrBlock": "0.0.0.0/0", "RuleAction": "allow"},
        {"RuleNumber": 32767, "Egress": False, "Protocol": "-1", "CidrBlock": "0.0.0.0/0", "RuleAction": "deny"},
    )

    def test_rules_are_evaluated_lowest_number_first_and_the_asterisk_denies_the_rest(self):
        example = acl(*self.EXAMPLE)
        decide = lambda port, src: main.acl_decide(example, False, 6, (port, port), IP(src))[0][2]
        self.assertEqual(decide(443, "198.51.100.7").number, 110)
        self.assertEqual(decide(139, "198.51.100.7").describe(), "rule * deny all from 0.0.0.0/0")
        self.assertEqual(decide(22, "192.0.2.15").number, 120)
        self.assertFalse(decide(22, "198.51.100.7").allow)
        deny_first = acl(
            {"RuleNumber": 90, "Egress": False, "Protocol": "6", "PortRange": {"From": 443, "To": 443}, "CidrBlock": "198.51.100.0/24", "RuleAction": "deny"},
            *self.EXAMPLE,
        )
        self.assertEqual(main.acl_decide(deny_first, False, 6, (443, 443), IP("198.51.100.7"))[0][2].number, 90)

    def test_a_port_range_is_split_between_the_rules_that_decide_each_part(self):
        segments = main.acl_decide(acl(*self.EXAMPLE), False, 6, main.NAT_PORTS, IP("198.51.100.7"))
        self.assertEqual([(lo, hi, e.number, e.allow) for lo, hi, e in segments], [(1024, 32767, 32767, False), (32768, 65535, 140, True)])
        linux = main.acl_decide(acl(*self.EXAMPLE), False, 6, main.LINUX_EPHEMERAL, IP("198.51.100.7"))
        self.assertEqual([(lo, hi, e.number) for lo, hi, e in linux], [(32768, 60999, 140)])

    def test_a_custom_acl_without_listed_asterisk_rules_still_denies_by_default(self):
        bare = acl({"RuleNumber": 100, "Egress": True, "Protocol": "6", "PortRange": {"From": 443, "To": 443}, "CidrBlock": "0.0.0.0/0", "RuleAction": "allow"})
        outcome = main.acl_decide(bare, False, 6, (443, 443), IP("10.0.0.1"))[0][2]
        self.assertEqual((outcome.number, outcome.allow), (main.ASTERISK, False))
        icmp = acl({"RuleNumber": 80, "Egress": False, "Protocol": "1", "IcmpTypeCode": {"Type": -1, "Code": -1}, "CidrBlock": "0.0.0.0/0", "RuleAction": "allow"})
        self.assertTrue(main.acl_decide(icmp, False, 1, (8, 8), IP("10.0.0.1"))[0][2].allow)
        self.assertFalse(main.acl_decide(icmp, False, 6, (8, 8), IP("10.0.0.1"))[0][2].allow)


class SecurityGroupTests(unittest.TestCase):
    def test_permissions_cover_protocols_ports_and_icmp_types(self):
        everything = main.make_permission({"IpProtocol": "-1", "IpRanges": [{"CidrIp": "0.0.0.0/0"}]}, "t")
        https = main.make_permission({"IpProtocol": "tcp", "FromPort": 443, "ToPort": 443}, "t")
        high = main.make_permission({"IpProtocol": "6", "FromPort": 1024, "ToPort": 65535}, "t")
        echo = main.make_permission({"IpProtocol": "icmp", "FromPort": 8, "ToPort": -1}, "t")
        self.assertTrue(everything.covers(17, 53) and everything.covers(1, 8))
        self.assertTrue(https.covers(6, 443))
        self.assertFalse(https.covers(6, 444) or https.covers(17, 443))
        self.assertTrue(high.covers(6, 50000))
        self.assertTrue(echo.covers(1, 8))
        self.assertFalse(echo.covers(1, 0))
        with self.assertRaises(main.InputError):
            main.make_permission({"IpProtocol": "tcpp"}, "t")

    def test_a_group_reference_matches_private_ips_but_not_traffic_through_a_gateway(self):
        def mutate(doc):
            report = item(doc, "SecurityGroups", "GroupId", "sg-0report")
            report["IpPermissions"] = [{"IpProtocol": "tcp", "FromPort": 443, "ToPort": 443, "UserIdGroupPairs": [{"GroupId": "sg-0web"}], "IpRanges": []}]

        snap = snapshot(mutate)
        private = main.trace(snap, "web", "report", "tcp", 443)
        self.assertEqual(private.verdict, "reachable")
        self.assertIn("from sg-0web", next(h.rule for h in private.hops if h.kind == "sg-ingress"))
        hairpin = main.trace(snap, "web", "203.0.113.40", "tcp", 443)
        self.assertEqual((hairpin.verdict, hairpin.code), ("blocked", "SG_REFERENCES_NOT_PRESERVED"))

    def test_turning_on_transit_gateway_sg_referencing_lets_the_group_rule_match(self):
        before = main.trace(snapshot(), "batch", "ldap", "tcp", 636)
        self.assertEqual(before.code, "SG_REFERENCING_SUPPORT")

        def mutate(doc):
            item(doc, "TransitGateways", "TransitGatewayId", "tgw-0core")["Options"]["SecurityGroupReferencingSupport"] = "enable"

        after = main.trace(snapshot(mutate), "batch", "ldap", "tcp", 636)
        self.assertEqual(after.verdict, "reachable")


class PathTests(unittest.TestCase):
    def test_the_fixture_matrix_matches_every_documented_expectation(self):
        rows = main.run_matrix(snapshot(), main.read_questions(QUESTIONS))
        failed = [(r["name"], r["verdict"], r["explanation_code"], r["reason"]) for r in rows if not r["ok"]]
        self.assertEqual(failed, [])
        self.assertEqual(len(rows), 17)

    def test_the_nat_reply_is_partial_until_the_nat_subnet_acl_opens_1024_65535(self):
        answer = main.trace(snapshot(), "batch", "internet", "tcp", 443)
        self.assertEqual(answer.verdict, "partial")
        partial = [h for h in answer.hops if h.decision == main.PARTIAL]
        self.assertEqual([(h.direction, h.kind, h.component) for h in partial], [("return", "nacl-in", "acl-0public")])
        self.assertIn("1024-32767 denied by rule *", partial[0].rule)
        self.assertIn("about 49%", partial[0].rule)

        def fix(doc):
            entry(item(doc, "NetworkAcls", "NetworkAclId", "acl-0public"), 120, False)["PortRange"] = {"From": 1024, "To": 65535}

        self.assertEqual(main.trace(snapshot(fix), "batch", "internet", "tcp", 443).verdict, "reachable")

    def test_the_client_ephemeral_range_decides_the_return_path(self):
        snap = snapshot()
        self.assertEqual(main.trace(snap, "web", "internet", "tcp", 443).verdict, "reachable")
        self.assertEqual(main.trace(snap, "web", "internet", "tcp", 443, (49152, 65535)).verdict, "reachable")
        old_windows = main.trace(snap, "web", "internet", "tcp", 443, (1025, 5000))
        self.assertEqual((old_windows.verdict, old_windows.code), ("blocked", "SUBNET_ACL_RESTRICTION"))
        self.assertEqual(old_windows.blocked_at.direction, "return")

    def test_the_stateless_return_blocks_peered_replies_until_an_outbound_rule_exists(self):
        answer = main.trace(snapshot(), "etl", "db", "tcp", 5432)
        stop = answer.blocked_at
        self.assertEqual((stop.direction, stop.kind, stop.component), ("return", "nacl-out", "acl-0private"))
        self.assertTrue(all(h.decision != main.DENY for h in answer.hops if h.direction == "forward"))

        def fix(doc):
            item(doc, "NetworkAcls", "NetworkAclId", "acl-0private")["Entries"].append(
                {"RuleNumber": 115, "Egress": True, "Protocol": "6", "PortRange": {"From": 1024, "To": 65535}, "CidrBlock": "10.1.0.0/16", "RuleAction": "allow"}
            )

        self.assertEqual(main.trace(snapshot(fix), "etl", "db", "tcp", 5432).verdict, "reachable")

    def test_traffic_inside_one_subnet_skips_the_network_acl(self):
        def mutate(doc):
            item(doc, "NetworkAcls", "NetworkAclId", "acl-0private")["Entries"] = []
            batch = item(doc, "SecurityGroups", "GroupId", "sg-0batch")
            batch["IpPermissions"] = [{"IpProtocol": "tcp", "FromPort": 8080, "ToPort": 8080, "IpRanges": [{"CidrIp": "10.0.2.0/24"}]}]

        snap = snapshot(mutate)
        inside = main.trace(snap, "report", "batch", "tcp", 8080)
        self.assertEqual(inside.verdict, "reachable")
        self.assertTrue(any(h.decision == main.INFO and "same subnet" in h.rule for h in inside.hops))
        self.assertEqual(main.trace(snap, "web", "batch", "tcp", 8080).code, "SUBNET_ACL_RESTRICTION")

    def test_peering_is_not_transitive_and_requires_an_active_connection(self):
        def through_peer(doc):
            item(doc, "RouteTables", "RouteTableId", "rtb-0analytics")["Routes"].append({"DestinationCidrBlock": "0.0.0.0/0", "VpcPeeringConnectionId": "pcx-0analytics", "State": "active"})

        answer = main.trace(snapshot(through_peer), "etl", "internet", "tcp", 443)
        self.assertEqual(answer.code, "PCX_REQUIRES_ADDRESS_IN_VPC_CIDR")

        def pending(doc):
            item(doc, "VpcPeeringConnections", "VpcPeeringConnectionId", "pcx-0analytics")["Status"] = {"Code": "pending-acceptance"}

        self.assertEqual(main.trace(snapshot(pending), "etl", "db", "tcp", 5432).code, "BAD_STATE")

    def test_nat_gateway_failures_name_the_nat_and_its_subnet_route(self):
        def deleted(doc):
            item(doc, "NatGateways", "NatGatewayId", "nat-0shop")["State"] = "deleted"

        self.assertEqual(main.trace(snapshot(deleted), "batch", "internet", "tcp", 443).code, "BAD_STATE")

        def no_igw_route(doc):
            routes = item(doc, "RouteTables", "RouteTableId", "rtb-0public")["Routes"]
            routes[:] = [r for r in routes if r.get("GatewayId") != "igw-0shop"]

        answer = main.trace(snapshot(no_igw_route), "batch", "internet", "tcp", 443)
        self.assertEqual((answer.code, answer.blocked_at.component), ("NO_ROUTE_TO_DESTINATION", "rtb-0public"))

    def test_icmp_needs_an_echo_rule_and_an_echo_reply_in_every_acl(self):
        def allow_ping(doc):
            item(doc, "SecurityGroups", "GroupId", "sg-0db")["IpPermissions"].append({"IpProtocol": "icmp", "FromPort": 8, "ToPort": -1, "IpRanges": [{"CidrIp": "10.0.1.0/24"}]})
            item(doc, "NetworkAcls", "NetworkAclId", "acl-0private")["Entries"].append(
                {"RuleNumber": 90, "Egress": False, "Protocol": "1", "IcmpTypeCode": {"Type": 8, "Code": -1}, "CidrBlock": "10.0.1.0/24", "RuleAction": "allow"}
            )

        no_reply = main.trace(snapshot(allow_ping), "web", "db", "icmp")
        self.assertEqual((no_reply.blocked_at.direction, no_reply.blocked_at.component), ("return", "acl-0private"))
        self.assertIn("icmp echo reply", no_reply.blocked_at.rule)

        def with_reply(doc):
            allow_ping(doc)
            item(doc, "NetworkAcls", "NetworkAclId", "acl-0private")["Entries"].append(
                {"RuleNumber": 90, "Egress": True, "Protocol": "1", "IcmpTypeCode": {"Type": 0, "Code": -1}, "CidrBlock": "10.0.1.0/24", "RuleAction": "allow"}
            )

        self.assertEqual(main.trace(snapshot(with_reply), "web", "db", "icmp").verdict, "reachable")

    def test_gateway_endpoints_carry_tcp_only_and_unknown_targets_stop_the_walk(self):
        def open_udp(doc):
            item(doc, "NetworkAcls", "NetworkAclId", "acl-0private")["Entries"].append(
                {"RuleNumber": 130, "Egress": True, "Protocol": "17", "PortRange": {"From": 53, "To": 53}, "CidrBlock": "0.0.0.0/0", "RuleAction": "allow"}
            )
            item(doc, "RouteTables", "RouteTableId", "rtb-0private")["Routes"].append({"DestinationCidrBlock": "192.168.0.0/16", "GatewayId": "vgw-0office", "State": "active"})

        snap = snapshot(open_udp)
        self.assertEqual(main.trace(snap, "batch", "pl-0s3", "udp", 53).code, "VPCE_GATEWAY_PROTOCOL_RESTRICTION")
        vpn = main.trace(snap, "batch", "192.168.10.10", "tcp", 443)
        self.assertEqual((vpn.verdict, vpn.code), ("unknown", "UNSUPPORTED_COMPONENT"))

    def test_a_route_into_the_vpc_without_an_interface_at_that_address_goes_nowhere(self):
        answer = main.trace(snapshot(), "web", "10.0.2.99", "tcp", 5432)
        self.assertEqual((answer.verdict, answer.code), ("blocked", "NO_POSSIBLE_DESTINATION"))


class LoadTests(unittest.TestCase):
    def test_instances_alone_are_enough_and_name_tags_come_from_reservations(self):
        doc = copy.deepcopy(BASE)
        doc.pop("NetworkInterfaces")
        for reservation in doc["Reservations"]:
            for instance in reservation["Instances"]:
                instance.pop("NetworkInterfaces")
        snap = main.build_snapshot(doc)
        web = main.resolve(snap, "web", from_internet=True)
        self.assertEqual((web.interface.instance, str(web.address), web.public), ("i-0web", "203.0.113.20", True))
        self.assertEqual(main.trace(snap, "internet", "web", "tcp", 443).verdict, "reachable")

    def test_resolve_accepts_names_ids_addresses_and_prefix_lists(self):
        snap = snapshot()
        self.assertEqual(main.resolve(snap, "db").interface.id, "eni-0db")
        self.assertEqual(main.resolve(snap, "i-0db").interface.id, "eni-0db")
        self.assertEqual(main.resolve(snap, "eni-0db").interface.id, "eni-0db")
        self.assertEqual(main.resolve(snap, "10.0.2.20").interface.id, "eni-0db")
        self.assertTrue(main.resolve(snap, "203.0.113.20").public)
        self.assertEqual(str(main.resolve(snap, "com.amazonaws.eu-central-1.s3").address), "52.216.0.1")
        self.assertIsNone(main.resolve(snap, "8.8.8.8").interface)
        self.assertEqual(main.resolve(snap, "internet").address, main.INTERNET)
        with self.assertRaises(main.InputError):
            main.resolve(snap, "no-such-host")
        snap.interfaces["eni-0db"].name = "web"
        with self.assertRaises(main.InputError):
            main.resolve(snap, "web")

    def test_missing_pieces_are_input_errors_or_warnings(self):
        with self.assertRaises(main.InputError):
            main.build_snapshot({"Vpcs": []})
        doc = copy.deepcopy(BASE)
        doc["RouteTables"] = [t for t in doc["RouteTables"] if t["RouteTableId"] != "rtb-0shop-main"]
        item(doc, "RouteTables", "RouteTableId", "rtb-0public")["Associations"] = []
        snap = main.build_snapshot(doc)
        with self.assertRaises(main.InputError):
            snap.route_table_for("subnet-0public-a")
        doc = copy.deepcopy(BASE)
        doc.pop("PrefixLists")
        self.assertTrue(any("pl-0s3" in w for w in main.build_snapshot(doc).warnings))
        doc["PrefixLists"] = [{"PrefixListId": "pl-0v6", "PrefixListName": "lattice-v6", "Cidrs": ["fd00:ec2:80::/64"]}]
        self.assertEqual(main.build_snapshot(doc).prefix_lists["pl-0v6"], ("lattice-v6", []))

    def test_subnets_shows_public_private_and_main_table_fallback(self):
        rows = {r["subnet"]: r for r in main.subnet_rows(snapshot())}
        self.assertTrue(rows["subnet-0public-a"]["public"])
        self.assertEqual(rows["subnet-0public-a"]["default_route"], "igw-0shop")
        self.assertFalse(rows["subnet-0private-a"]["public"])
        self.assertEqual(rows["subnet-0private-a"]["default_route"], "nat-0shop")
        self.assertEqual((rows["subnet-0analytics-a"]["route_table"], rows["subnet-0analytics-a"]["route_table_association"]), ("rtb-0analytics", "main"))


class CliTests(unittest.TestCase):
    def test_check_exit_codes_and_json(self):
        code, out, _ = run(["check", "web", "db", "--port", "5432", "--snapshot", SHOP])
        self.assertEqual(code, 0)
        self.assertIn("REACHABLE", out)
        code, out, _ = run(["check", "etl", "db", "--port", "5432", "--snapshot", SHOP, "--json"])
        self.assertEqual(code, 1)
        doc = json.loads(out)
        self.assertEqual((doc["verdict"], doc["explanation_code"], doc["blocked_at"]["component"]), ("blocked", "SUBNET_ACL_RESTRICTION", "acl-0private"))
        self.assertEqual([h["kind"] for h in doc["forward"]][:3], ["sg-egress", "route", "nacl-out"])
        code, out, _ = run(["matrix", "--questions", QUESTIONS, "--snapshot", SHOP])
        self.assertEqual(code, 0)
        self.assertIn("17/17 as expected", out)

    def test_bad_input_exits_2_with_a_reason(self):
        with tempfile.TemporaryDirectory() as tmp:
            broken = os.path.join(tmp, "broken.json")
            with open(broken, "w") as handle:
                handle.write("{not json")
            code, _, err = run(["check", "web", "db", "--port", "5432", "--snapshot", broken])
            self.assertEqual(code, 2)
            self.assertIn("not JSON", err)
            code, _, err = run(["check", "web", "db", "--port", "5432", "--snapshot", tmp + "/empty"])
            self.assertEqual(code, 2)
            questions = os.path.join(tmp, "q.json")
            with open(questions, "w") as handle:
                json.dump([{"from": "web", "to": "db", "port": 5432, "expect": "maybe"}], handle)
            code, _, err = run(["matrix", "--questions", questions, "--snapshot", SHOP])
            self.assertEqual(code, 2)
            self.assertIn("maybe", err)
        code, _, err = run(["check", "web", "db", "--snapshot", SHOP])
        self.assertEqual(code, 2)
        self.assertIn("port", err)
        code, _, err = run(["check", "web", "db", "--port", "5432", "--ephemeral", "60000-1024", "--snapshot", SHOP])
        self.assertEqual(code, 2)

    def test_a_failed_expectation_makes_the_matrix_exit_1(self):
        with tempfile.TemporaryDirectory() as tmp:
            questions = os.path.join(tmp, "q.json")
            with open(questions, "w") as handle:
                json.dump({"questions": [{"name": "wrong", "from": "etl", "to": "db", "port": 5432, "expect": "reachable"}]}, handle)
            code, out, _ = run(["matrix", "--questions", questions, "--snapshot", SHOP])
        self.assertEqual(code, 1)
        self.assertIn("FAIL wrong", out)
        self.assertIn("0/1 as expected", out)


if __name__ == "__main__":
    unittest.main()
