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
ROOT, TEST, CC = "127.0.10.1", "127.0.10.2", "127.0.10.3"


def rr(name, ttl, rtype, text):
    return main.make_rr(name, ttl, rtype, text)


def zone_file(name):
    with open(os.path.join(LAB, name), encoding="utf-8") as handle:
        return main.parse_records(handle.read())


def run(argv, **kwargs):
    out, err = StringIO(), StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = main.main(argv, **kwargs)
    return code, out.getvalue(), err.getvalue()


class Zone:
    def __init__(self, records):
        self.records = list(records)
        self.soa = next(r for r in self.records if r.rtype == main.SOA)
        self.origin = self.soa.name

    def answer(self, query):
        q = query.question[0]
        reply = main.Message(id=query.id, qr=True, rd=query.rd, question=list(query.question), edns=main.Edns() if query.edns else None)
        for cut in reversed(main.ancestors(q.name)):
            if main.same_name(cut, self.origin) or not main.is_subdomain(cut, self.origin):
                continue
            ns = [r for r in self.records if r.rtype == main.NS and main.same_name(r.name, cut)]
            if ns:
                targets = {r.data.lower() for r in ns}
                reply.authority = ns
                reply.additional = [r for r in self.records if r.rtype in (main.A, main.AAAA) and r.name.lower() in targets]
                return reply
        reply.aa = True
        owned = [r for r in self.records if main.same_name(r.name, q.name)]
        if not owned and not any(main.is_subdomain(r.name, q.name) for r in self.records):
            reply.rcode = main.NXDOMAIN
            reply.authority = [self.soa]
            return reply
        hits = [r for r in owned if r.rtype == q.qtype] or [r for r in owned if r.rtype == main.CNAME]
        if hits:
            reply.answer = hits
        else:
            reply.authority = [self.soa]
        return reply


class FakeTransport:
    def __init__(self, servers):
        self.servers = servers
        self.down = set()
        self.log = []

    def exchange(self, server, port, query):
        q = query.question[0]
        self.log.append((server, q.name, main.type_name(q.qtype), query.rd))
        if server in self.down or server not in self.servers:
            raise main.QueryError(f"{server}#{port}: no response over UDP after 3 tries of 2s")
        wire = main.encode(self.servers[server](query))
        return main.Exchange(main.decode(wire), server, port, "udp", False, 1, len(wire), 0.0)


def lab(extra_cc=()):
    root = Zone(zone_file("root.db") + [rr("example.", 172800, "NS", "ns1.example."), rr("ns1.example.", 172800, "A", "127.0.10.4")])
    test = Zone(zone_file("test.db") + [rr("ext.test.", 3600, "NS", "ns.example.")])
    cc = Zone(zone_file("cc.test.db") + list(extra_cc))
    example = Zone([rr("example.", 3600, "SOA", "ns1.example. h.example. 1 7200 3600 1209600 60"), rr("ns.example.", 3600, "A", "127.0.10.5")])
    ext = Zone([rr("ext.test.", 3600, "SOA", "ns.example. h.example. 1 7200 3600 1209600 60"), rr("www.ext.test.", 120, "A", "127.0.20.9")])
    return FakeTransport({ROOT: root.answer, TEST: test.answer, CC: cc.answer, "127.0.10.4": example.answer, "127.0.10.5": ext.answer})


class Clock:
    def __init__(self, now=1000.0):
        self.now = now

    def __call__(self):
        return self.now


class WireTests(unittest.TestCase):
    def test_query_bytes_follow_the_rfc_1035_header_and_question_layout(self):
        wire = main.encode(main.make_query("www.cc.test", main.A, rd=True, bufsize=0, ident=0x1234))
        self.assertEqual(wire, bytes.fromhex("1234 0100 0001 0000 0000 0000") + b"\x03www\x02cc\x04test\x00" + bytes.fromhex("0001 0001"))

    def test_every_header_flag_and_the_opcode_survive_a_round_trip(self):
        message = main.Message(id=7, qr=True, opcode=2, aa=True, tc=True, rd=True, ra=True, ad=True, cd=True, rcode=main.NXDOMAIN)
        self.assertEqual(main.pack_flags(message), 0b1001_0111_1011_0011)
        back = main.decode(main.encode(message))
        self.assertEqual((back.qr, back.opcode, back.aa, back.tc, back.rd, back.ra, back.ad, back.cd, back.rcode), (True, 2, True, True, True, True, True, True, 3))
        self.assertEqual(main.flags_text(back), "qr aa tc rd ra ad cd")

    def test_names_are_compressed_on_write_and_pointers_are_followed_on_read(self):
        reply = main.Message(id=1, qr=True, question=[main.Question("www.cc.test.", main.A)], answer=[rr("www.cc.test.", 300, "A", "127.0.20.1"), rr("www.cc.test.", 300, "A", "127.0.20.2")], authority=[rr("cc.test.", 3600, "NS", "ns1.cc.test.")])
        packed, plain = main.encode(reply), main.encode(reply, compress=False)
        self.assertEqual(packed[29:31], b"\xc0\x0c")
        self.assertIn(b"\x03ns1\xc0\x10", packed)
        self.assertLess(len(packed), len(plain))
        self.assertEqual(main.decode(packed), main.decode(plain))
        self.assertEqual(main.read_name(packed, 12), ("www.cc.test.", 25))
        self.assertEqual(main.read_name(packed, 29), ("www.cc.test.", 31))

    def test_srv_targets_are_written_uncompressed_and_read_either_way(self):
        srv = rr("_http._tcp.cc.test.", 300, "SRV", "10 50 8080 www.cc.test.")
        reply = main.Message(id=1, qr=True, question=[main.Question("www.cc.test.", main.SRV)], answer=[srv])
        wire = main.encode(reply)
        self.assertTrue(wire.endswith(struct.pack("!3H", 10, 50, 8080) + b"\x03www\x02cc\x04test\x00"))
        squeezed = wire[: -len(b"\x03www\x02cc\x04test\x00")] + b"\xc0\x0c"
        squeezed = squeezed[:-10] + struct.pack("!H", 8) + squeezed[-8:]
        self.assertEqual(main.decode(squeezed).answer[0].data, (10, 50, 8080, "www.cc.test."))

    def test_pointer_loops_forward_pointers_and_bad_labels_are_rejected(self):
        header = struct.pack("!6H", 1, 0x8000, 1, 0, 0, 0)
        with self.assertRaisesRegex(main.WireError, "points forward"):
            main.read_name(header + b"\xc0\x0c", 12)
        with self.assertRaisesRegex(main.WireError, "compression loop"):
            main.read_name(header + b"\x01a\xc0\x0c", 12)
        with self.assertRaisesRegex(main.WireError, "label type"):
            main.read_name(header + b"\x41a\x00", 12)
        with self.assertRaisesRegex(main.WireError, "longer than 255"):
            main.read_name(header + (b"\x3f" + b"a" * 63) * 4 + b"\x00", 12)
        with self.assertRaisesRegex(main.WireError, "runs past the end"):
            main.read_name(header + b"\x05ab", 12)

    def test_name_input_is_validated_and_escapes_round_trip(self):
        for bad, why in (("a" * 64 + ".test", "at most 63"), ("a..test", "empty label"), ("café.test", "punycode"), (".".join(["a" * 63] * 4), "longer than 255")):
            with self.assertRaisesRegex(main.InputError, why):
                main.name_to_labels(bad)
        self.assertEqual(main.name_to_labels(r"a\.b.example"), [b"a.b", b"example"])
        self.assertEqual(main.labels_to_name([b"a.b", b"sp ace", b"example"]), r"a\.b.sp\032ace.example.")
        self.assertEqual(main.name_to_labels(main.labels_to_name([b"a.b", b"sp ace"])), [b"a.b", b"sp ace"])
        self.assertEqual(main.ancestors("www.cc.test"), ["www.cc.test.", "cc.test.", "test.", "."])
        self.assertTrue(main.is_subdomain("WWW.cc.test.", "cc.TEST"))
        self.assertFalse(main.is_subdomain("xcc.test.", "cc.test."))

    def test_every_record_type_round_trips_through_wire_and_text(self):
        cases = [
            ("A", "127.0.20.1", "127.0.20.1"),
            ("AAAA", "fd00:cc:0:0::1", "fd00:cc::1"),
            ("NS", "ns1.cc.test", "ns1.cc.test."),
            ("CNAME", "web.test.", "web.test."),
            ("PTR", "www.cc.test.", "www.cc.test."),
            ("MX", "10 mail.cc.test.", "10 mail.cc.test."),
            ("TXT", '"v=spf1 -all" "say \\"hi\\"" "\\255"', '"v=spf1 -all" "say \\"hi\\"" "\\255"'),
            ("SOA", "ns1.cc.test. hostmaster.cc.test. 2026110101 7200 3600 1209600 10", "ns1.cc.test. hostmaster.cc.test. 2026110101 7200 3600 1209600 10"),
            ("SRV", "10 50 8080 www.cc.test.", "10 50 8080 www.cc.test."),
            ("TYPE65", "\\# 4 0A000001", "\\# 4 0A000001"),
        ]
        for rtype, text, shown in cases:
            with self.subTest(rtype=rtype):
                record = rr("x.cc.test.", 60, rtype, text)
                back = main.decode(main.encode(main.Message(id=1, qr=True, answer=[record]))).answer[0]
                self.assertEqual(main.rdata_text(back), shown)
                self.assertEqual(back, record)
                self.assertEqual(main.rr_from_json(main.rr_json(back)), back)
        with self.assertRaisesRegex(main.InputError, "255 octets"):
            rr("x.test.", 60, "TXT", '"' + "a" * 256 + '"')
        with self.assertRaisesRegex(main.InputError, "unknown record type"):
            main.type_code("BOGUS")

    def test_edns_opt_record_carries_payload_size_do_bit_and_extended_rcode(self):
        message = main.Message(id=9, qr=True, rcode=16, edns=main.Edns(udp_size=1232, do=True, options=[(10, b"\x01" * 8)]))
        wire = main.encode(message)
        self.assertEqual(struct.unpack_from("!H", wire, 10)[0], 1)
        self.assertEqual(wire[12:23], b"\x00" + struct.pack("!HHIH", 41, 1232, 0x01008000, 12))
        back = main.decode(wire)
        self.assertEqual((back.rcode, main.rcode_name(back.rcode), back.edns.udp_size, back.edns.do, back.additional), (16, "BADVERS", 1232, True, []))
        self.assertEqual(back.edns.options, [(10, b"\x01" * 8)])
        self.assertEqual(main.Edns(udp_size=100).payload_limit, 512)
        double = wire[:10] + struct.pack("!H", 2) + wire[12:] + wire[12:]
        with self.assertRaisesRegex(main.WireError, "more than one OPT"):
            main.decode(double)
        with self.assertRaisesRegex(main.InputError, "needs EDNS"):
            main.encode(main.Message(rcode=16))

    def test_truncated_and_malformed_messages_raise_wire_errors_and_high_bit_ttls_read_as_zero(self):
        with self.assertRaisesRegex(main.WireError, "header alone is 12"):
            main.decode(b"\x00" * 11)
        good = main.encode(main.Message(id=1, qr=True, answer=[rr("a.test.", 60, "A", "10.0.0.1")]))
        with self.assertRaisesRegex(main.WireError, "RDLENGTH"):
            main.decode(good[:-2])
        bad_length = good[:-6] + struct.pack("!H", 5) + good[-4:] + b"\x00"
        with self.assertRaisesRegex(main.WireError, "4 octets"):
            main.decode(bad_length)
        huge_ttl = good[:-10] + struct.pack("!I", 0x80000001) + good[-6:]
        self.assertEqual(main.decode(huge_ttl).answer[0].ttl, 0)


class LocalServer:
    def __init__(self, handler):
        self.handler = handler
        self.seen = []
        for _ in range(20):
            self.udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            self.udp.bind(("127.0.0.1", 0))
            self.port = self.udp.getsockname()[1]
            self.tcp = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.tcp.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                self.tcp.bind(("127.0.0.1", self.port))
                break
            except OSError:
                self.udp.close()
                self.tcp.close()
        self.tcp.listen(4)
        self.udp.settimeout(0.05)
        self.tcp.settimeout(0.05)
        self.running = True
        self.threads = [threading.Thread(target=self.serve_udp, daemon=True), threading.Thread(target=self.serve_tcp, daemon=True)]
        for thread in self.threads:
            thread.start()

    def serve_udp(self):
        while self.running:
            try:
                data, peer = self.udp.recvfrom(65535)
            except (socket.timeout, OSError):
                continue
            query = main.decode(data)
            self.seen.append(("udp", query))
            for reply in self.handler(query, "udp", len(self.seen)) or []:
                self.udp.sendto(main.encode(reply), peer)

    def serve_tcp(self):
        while self.running:
            try:
                conn, _ = self.tcp.accept()
            except (socket.timeout, OSError):
                continue
            with conn:
                conn.settimeout(2)
                length = struct.unpack("!H", main.recv_exact(conn, 2))[0]
                query = main.decode(main.recv_exact(conn, length))
                self.seen.append(("tcp", query))
                for reply in self.handler(query, "tcp", len(self.seen)) or []:
                    wire = main.encode(reply)
                    conn.sendall(struct.pack("!H", len(wire)) + wire)

    def close(self):
        self.running = False
        for thread in self.threads:
            thread.join(1)
        self.udp.close()
        self.tcp.close()


def reply_to(query, answer=(), tc=False, ident=None):
    return main.Message(id=query.id if ident is None else ident, qr=True, aa=True, tc=tc, rd=query.rd, question=list(query.question), answer=list(answer), edns=main.Edns() if query.edns else None)


class TransportTests(unittest.TestCase):
    def serve(self, handler):
        server = LocalServer(handler)
        self.addCleanup(server.close)
        return server

    def test_udp_query_to_a_local_server(self):
        server = self.serve(lambda q, proto, n: [reply_to(q, [rr(q.question[0].name, 300, "A", "127.0.20.1")])])
        exchange = main.Transport(timeout=1).exchange("127.0.0.1", server.port, main.make_query("www.cc.test", main.A))
        self.assertEqual((exchange.transport, exchange.attempts, exchange.truncated_udp), ("udp", 1, False))
        self.assertEqual(main.rdata_text(exchange.response.answer[0]), "127.0.20.1")
        self.assertEqual(server.seen[0][1].edns.udp_size, 1232)

    def test_truncated_udp_reply_is_retried_over_tcp_unless_ignored(self):
        big = [rr("big.cc.test.", 300, "A", f"127.0.30.{i}") for i in range(1, 41)]
        server = self.serve(lambda q, proto, n: [reply_to(q, tc=True)] if proto == "udp" else [reply_to(q, big)])
        query = main.make_query("big.cc.test", main.A, bufsize=0)
        exchange = main.Transport(timeout=1).exchange("127.0.0.1", server.port, query)
        self.assertEqual((exchange.transport, exchange.truncated_udp, len(exchange.response.answer)), ("tcp", True, 40))
        self.assertGreater(exchange.size, main.CLASSIC_UDP_SIZE)
        self.assertEqual([proto for proto, _ in server.seen], ["udp", "tcp"])
        kept = main.Transport(timeout=1, ignore_tc=True).exchange("127.0.0.1", server.port, query)
        self.assertEqual((kept.transport, kept.response.tc, len(kept.response.answer)), ("udp", True, 0))

    def test_lost_datagram_is_retried_and_a_reply_with_the_wrong_id_is_ignored(self):
        def handler(query, proto, count):
            if count == 1:
                return []
            answer = [rr(query.question[0].name, 60, "A", "127.0.20.1")]
            return [reply_to(query, answer, ident=(query.id + 1) % 65536), reply_to(query, answer)]

        server = self.serve(handler)
        exchange = main.Transport(timeout=0.3, tries=3).exchange("127.0.0.1", server.port, main.make_query("www.cc.test", main.A))
        self.assertEqual(exchange.attempts, 2)
        self.assertEqual(len(exchange.response.answer), 1)
        self.assertFalse(main.response_matches(main.make_query("www.cc.test", main.A, ident=5), reply_to(main.make_query("other.test", main.A, ident=5))))

    def test_nothing_listening_is_a_query_error_naming_the_port(self):
        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
        probe.close()
        with self.assertRaisesRegex(main.QueryError, f"127.0.0.1#{port}: (connection refused|no response)"):
            main.Transport(timeout=0.2, tries=2).exchange("127.0.0.1", port, main.make_query("www.cc.test", main.A))
        with self.assertRaisesRegex(main.QueryError, "TCP connection refused"):
            main.Transport(timeout=0.5, tcp=True).exchange("127.0.0.1", port, main.make_query("www.cc.test", main.A))

    def test_cli_query_prints_dig_style_text_and_json(self):
        server = self.serve(lambda q, proto, n: [reply_to(q, [rr(q.question[0].name, 300, "A", "127.0.20.1")])])
        code, out, _ = run(["query", "@127.0.0.1", "www.cc.test", "A", "+norecurse", "--port", str(server.port), "--timeout", "1"])
        self.assertEqual(code, 0)
        self.assertIn(";; flags: qr aa; QUERY: 1, ANSWER: 1, AUTHORITY: 0, ADDITIONAL: 1", out)
        self.assertIn("www.cc.test.\t300\tIN\tA\t127.0.20.1", out)
        self.assertFalse(server.seen[-1][1].rd)
        code, out, _ = run(["query", "@127.0.0.1", "www.cc.test", "+noedns", "+tcp", "--port", str(server.port), "--json"])
        data = json.loads(out)
        self.assertEqual((code, data["status"], data["transport"], data["edns"], data["answer"][0]["data"]), (0, "NOERROR", "tcp", None, "127.0.20.1"))


class ClassifyTests(unittest.TestCase):
    def test_referral_keeps_in_bailiwick_glue_and_drops_the_rest(self):
        reply = main.Message(
            qr=True,
            authority=[rr("ext.test.", 3600, "NS", "ns1.ext.test."), rr("ext.test.", 3600, "NS", "ns.example.")],
            additional=[rr("ns1.ext.test.", 3600, "A", "127.0.10.7"), rr("ns.example.", 3600, "A", "6.6.6.6"), rr("unrelated.test.", 3600, "A", "6.6.6.7")],
        )
        outcome = main.classify(reply, "www.ext.test.", main.A, "test.")
        self.assertEqual((outcome.kind, outcome.zone), ("referral", "ext.test."))
        self.assertEqual([(g.name, g.data) for g in outcome.glue], [("ns1.ext.test.", "127.0.10.7")])

    def test_negative_answers_lame_delegations_and_the_negative_ttl(self):
        soa = rr("cc.test.", 3600, "SOA", "ns1.cc.test. h.cc.test. 1 7200 3600 1209600 10")
        nx = main.classify(main.Message(qr=True, aa=True, rcode=main.NXDOMAIN, authority=[soa]), "nope.cc.test.", main.A, "cc.test.")
        self.assertEqual((nx.kind, nx.soa), ("nxdomain", soa))
        nodata = main.classify(main.Message(qr=True, aa=True, authority=[soa]), "www.cc.test.", main.MX, "cc.test.")
        self.assertEqual(nodata.kind, "nodata")
        upward = main.classify(main.Message(qr=True, authority=[rr("test.", 3600, "NS", "ns1.test.")]), "www.cc.test.", main.A, "cc.test.")
        self.assertEqual(upward.kind, "lame")
        self.assertEqual(main.classify(main.Message(qr=True, rcode=main.REFUSED), "x.test.", main.A, ".").kind, "error")
        self.assertEqual(main.negative_ttl(soa), 10)
        self.assertEqual(main.negative_ttl(rr("cc.test.", 5, "SOA", "a. b. 1 2 3 4 10")), 5)

    def test_cname_chain_inside_one_response_is_followed_to_the_data_or_the_last_target(self):
        chain = [rr("a.test.", 60, "CNAME", "b.test."), rr("b.test.", 60, "CNAME", "c.test."), rr("c.test.", 60, "A", "10.0.0.3")]
        full = main.classify(main.Message(qr=True, answer=chain), "a.test.", main.A, "test.")
        self.assertEqual((full.kind, len(full.records), full.target), ("answer", 3, "c.test."))
        partial = main.classify(main.Message(qr=True, answer=chain[:2]), "a.test.", main.A, "test.")
        self.assertEqual((partial.kind, partial.target), ("cname", "c.test."))
        self.assertEqual(main.classify(main.Message(qr=True, answer=chain[:1]), "a.test.", main.CNAME, "test.").kind, "answer")


class ResolverTests(unittest.TestCase):
    def resolver(self, transport=None, clock=None, hints=(("a.root.", ROOT),)):
        transport = transport or lab()
        return main.Resolver(transport, list(hints), cache=main.Cache(clock or Clock())), transport

    def test_walks_from_the_root_through_two_referrals_to_the_answer(self):
        resolver, transport = self.resolver()
        result = resolver.resolve("www.cc.test", main.A)
        self.assertEqual(result.status, "NOERROR")
        self.assertEqual([main.rr_text(r) for r in result.answer], ["www.cc.test.\t300\tIN\tA\t127.0.20.1"])
        self.assertEqual([(s.server, s.zone, s.kind, s.referral) for s in result.steps], [(ROOT, ".", "referral", "test."), (TEST, "test.", "referral", "cc.test."), (CC, "cc.test.", "answer", "")])
        self.assertEqual([main.rdata_text(r) for r in result.steps[0].records], ["ns1.test.", "127.0.10.2"])
        self.assertTrue(result.steps[2].aa)
        self.assertTrue(all(rd is False for _, _, _, rd in transport.log))

    def test_cname_into_another_zone_restarts_at_the_cached_delegation(self):
        resolver, transport = self.resolver()
        result = resolver.resolve("alias.cc.test", main.A)
        self.assertEqual([main.rr_text(r) for r in result.answer], ["alias.cc.test.\t300\tIN\tCNAME\tweb.test.", "web.test.\t300\tIN\tA\t127.0.20.2"])
        self.assertEqual([(s.server, s.kind) for s in result.steps], [(ROOT, "referral"), (TEST, "referral"), (CC, "cname"), (TEST, "answer")])
        self.assertEqual(result.steps[-1].zone, "test.")

    def test_cname_loops_and_long_chains_stop_with_servfail(self):
        loop = [rr("loop1.cc.test.", 60, "CNAME", "loop2.cc.test."), rr("loop2.cc.test.", 60, "CNAME", "loop1.cc.test.")]
        resolver, _ = self.resolver(lab(loop))
        result = resolver.resolve("loop1.cc.test", main.A)
        self.assertEqual(result.status, "SERVFAIL")
        self.assertIn("CNAME loop: loop1.cc.test. -> loop2.cc.test. -> loop1.cc.test.", result.error)
        chain = [rr(f"c{i}.cc.test.", 60, "CNAME", f"c{i + 1}.cc.test.") for i in range(12)]
        resolver, _ = self.resolver(lab(chain))
        self.assertIn("more than 8 CNAMEs", resolver.resolve("c0.cc.test", main.A).error)

    def test_glueless_delegation_resolves_the_name_server_address_first(self):
        resolver, _ = self.resolver()
        result = resolver.resolve("www.ext.test", main.A)
        self.assertEqual((result.status, main.rdata_text(result.answer[0])), ("NOERROR", "127.0.20.9"))
        lookups = [(s.qname, s.depth) for s in result.steps if s.depth == 1]
        self.assertIn(("ns.example.", 1), lookups)
        self.assertEqual(result.steps[-1].server, "127.0.10.5")
        self.assertIn(";; [name server lookup] Received", main.render_trace(result))

    def test_a_server_that_does_not_answer_is_skipped(self):
        transport = lab()
        transport.down.add("192.0.2.1")
        resolver, _ = self.resolver(transport, hints=(("dead.root.", "192.0.2.1"), ("a.root.", ROOT)))
        result = resolver.resolve("www.cc.test", main.A)
        self.assertEqual(result.status, "NOERROR")
        self.assertEqual((result.steps[0].kind, result.steps[1].server), ("no-response", ROOT))
        transport.down.add(ROOT)
        fresh, _ = self.resolver(transport, hints=(("dead.root.", "192.0.2.1"), ("a.root.", ROOT)))
        failed = fresh.resolve("www.cc.test", main.A)
        self.assertEqual(failed.status, "SERVFAIL")
        self.assertIn("every server for . failed", failed.error)

    def test_answers_come_from_cache_with_a_falling_ttl_until_they_expire(self):
        clock = Clock()
        resolver, transport = self.resolver(clock=clock)
        resolver.resolve("www.cc.test", main.A)
        sent = len(transport.log)
        clock.now += 100
        cached = resolver.resolve("www.cc.test", main.A)
        self.assertTrue(cached.from_cache)
        self.assertEqual(cached.answer[0].ttl, 200)
        self.assertEqual(len(transport.log), sent)
        clock.now += 201
        again = resolver.resolve("www.cc.test", main.A)
        self.assertEqual([(s.server, s.zone) for s in again.steps], [(CC, "cc.test.")])

    def test_nxdomain_is_cached_for_the_soa_minimum_for_every_type_and_nodata_per_type(self):
        clock = Clock()
        resolver, transport = self.resolver(clock=clock)
        first = resolver.resolve("nope.cc.test", main.A)
        self.assertEqual((first.status, first.negative_ttl, first.soa.data.minimum, first.soa.ttl), ("NXDOMAIN", 10, 10, 3600))
        clock.now += 5
        other_type = resolver.resolve("nope.cc.test", main.AAAA)
        self.assertEqual((other_type.status, other_type.from_cache, other_type.negative_ttl), ("NXDOMAIN", True, 5))
        clock.now += 6
        expired = resolver.resolve("nope.cc.test", main.A)
        self.assertEqual((expired.from_cache, expired.queries), (False, 1))
        nodata = resolver.resolve("www.cc.test", main.MX)
        self.assertEqual((nodata.status, nodata.negative_ttl), ("NODATA", 10))
        self.assertEqual(resolver.resolve("www.cc.test", main.A).status, "NOERROR")
        self.assertIn("negative-cached for 10 s", main.summary_line(nodata))

    def test_cache_dump_and_load_keep_live_entries_only(self):
        clock = Clock()
        resolver, _ = self.resolver(clock=clock)
        resolver.resolve("www.cc.test", main.A)
        resolver.resolve("nope.cc.test", main.A)
        saved = json.loads(json.dumps(resolver.cache.dump()))
        clock.now += 20
        restored = main.Cache(clock)
        restored.load(saved)
        self.assertEqual(restored.get("www.cc.test", main.A).records[0].ttl, 280)
        self.assertIsNone(restored.get("nope.cc.test", main.A))
        self.assertEqual(restored.delegation("www.cc.test")[0], "cc.test.")

    def test_cli_resolve_trace_json_and_cache_file(self):
        transport = lab()
        hints = os.path.join(LAB, "root.hints")
        code, out, _ = run(["resolve", "www.cc.test", "--hints", hints, "--trace"], transport=transport)
        self.assertEqual(code, 0)
        self.assertIn(";; Received", out)
        self.assertIn("from 127.0.10.1#53(a.root.) in 0 ms over UDP: referral to test.", out)
        self.assertIn(";; NOERROR www.cc.test. A: 1 record(s) after 3 queries", out)
        with tempfile.TemporaryDirectory() as tmp:
            cache = os.path.join(tmp, "cache.json")
            code, out, _ = run(["resolve", "nope.cc.test", "nope.cc.test", "--root", ROOT, "--cache-file", cache, "--json"], transport=transport, clock=Clock())
            results = json.loads(out)["results"]
            self.assertEqual(code, 1)
            self.assertEqual([(r["status"], r["from_cache"], r["negative_ttl"]) for r in results], [("NXDOMAIN", False, 10), ("NXDOMAIN", True, 10)])
            code, out, _ = run(["resolve", "nope.cc.test", "--root", ROOT, "--cache-file", cache, "--json"], transport=transport, clock=Clock(1004.0))
            self.assertEqual(json.loads(out)["results"][0]["negative_ttl"], 6)
            self.assertTrue(json.loads(out)["results"][0]["from_cache"])


class ResolvConfTests(unittest.TestCase):
    def test_parses_nameservers_search_and_options_like_glibc(self):
        text = "\n".join([
            "# comment",
            "; also a comment",
            "nameserver 10.0.0.1",
            "nameserver 10.0.0.2",
            "nameserver fe80::1%eth0",
            "nameserver 10.0.0.4",
            "nameserver not-an-ip",
            "domain old.example",
            "search corp.example. lab.example",
            "options ndots:20 timeout:1 attempts:9 rotate edns0 ndots:x",
            "frobnicate yes",
        ])
        conf = main.parse_resolv_conf(text)
        self.assertEqual(conf.nameservers, ["10.0.0.1", "10.0.0.2", "fe80::1"])
        self.assertEqual(conf.search, ["corp.example", "lab.example"])
        self.assertEqual((conf.ndots, conf.timeout, conf.attempts, conf.options), (15, 1, 5, ["rotate", "edns0"]))
        self.assertEqual(len(conf.warnings), 4)
        self.assertEqual(main.parse_resolv_conf("search a.example\ndomain b.example").search, ["b.example"])

    def test_environment_overrides_and_the_hostname_default(self):
        conf = main.parse_resolv_conf("nameserver 10.0.0.1\nsearch a.example\noptions ndots:5", env={"LOCALDOMAIN": "x.example y.example", "RES_OPTIONS": "ndots:2 edns0"})
        self.assertEqual((conf.search, conf.ndots, conf.options), (["x.example", "y.example"], 2, ["edns0"]))
        bare = main.parse_resolv_conf("", hostname="node1.corp.example")
        self.assertEqual((bare.search, bare.nameservers), (["corp.example"], ["127.0.0.1"]))
        self.assertIn("127.0.0.1", bare.warnings[0])

    def test_ndots_5_in_a_pod_puts_four_search_names_before_the_real_one(self):
        with open(os.path.join(FIXTURES, "resolv-pod.conf"), encoding="utf-8") as handle:
            conf = main.parse_resolv_conf(handle.read())
        plan = main.plan_search("api.example.com", conf)
        self.assertEqual([c.name for c in plan], ["api.example.com.default.svc.cluster.local.", "api.example.com.svc.cluster.local.", "api.example.com.cluster.local.", "api.example.com.cc.internal.", "api.example.com."])
        self.assertEqual(plan[-1].phase, "as-is-last")
        self.assertEqual([c.name for c in main.plan_search("api.example.com.", conf)], ["api.example.com."])
        self.assertEqual(len(main.plan_search("my-svc.my-ns.svc.cluster.local", conf)), 1 + 4)

    def test_ndots_threshold_trailing_dot_no_tld_query_and_duplicates(self):
        conf = main.parse_resolv_conf("search corp.example .\noptions ndots:1")
        self.assertEqual([(c.name, c.phase) for c in main.plan_search("www.cc.test", conf)], [("www.cc.test.", "as-is-first"), ("www.cc.test.corp.example.", "search")])
        self.assertEqual([c.name for c in main.plan_search("db", conf)], ["db.corp.example.", "db."])
        quiet = main.parse_resolv_conf("search corp.example\noptions ndots:1 no-tld-query")
        self.assertEqual([c.name for c in main.plan_search("db", quiet)], ["db.corp.example."])
        with self.assertRaises(main.InputError):
            main.plan_search("", conf)

    def test_search_stops_at_the_first_answer_and_follows_glibc_on_errors(self):
        conf = main.parse_resolv_conf("search a.example b.example\noptions ndots:5")
        plan = main.plan_search("www.cc.test", conf)
        sent, found = main.run_search(plan, lambda name: ("NOERROR", 1) if name == "www.cc.test." else ("NXDOMAIN", 0))
        self.assertEqual(([s["name"] for s in sent], found), (["www.cc.test.a.example.", "www.cc.test.b.example.", "www.cc.test."], True))
        sent, found = main.run_search(plan, lambda name: ("REFUSED", 0))
        self.assertEqual(([s["name"] for s in sent], found), (["www.cc.test.a.example.", "www.cc.test."], False))
        sent, _ = main.run_search(plan, lambda name: ("NOERROR", 0) if "a.example" in name else ("SERVFAIL", 0))
        self.assertEqual(len(sent), 3)
        one = main.parse_resolv_conf("search a.example\noptions ndots:1")
        sent, found = main.run_search(main.plan_search("www.cc.test", one), lambda name: ("NOERROR", 2))
        self.assertEqual(([s["name"] for s in sent], found), (["www.cc.test."], True))

    def test_cli_search_plans_and_sends_like_a_stub_resolver(self):
        pod = os.path.join(FIXTURES, "resolv-pod.conf")
        code, out, _ = run(["search", "www.cc.test", "--resolv-conf", pod, "--hostname", ""])
        self.assertEqual(code, 0)
        self.assertIn("  5  www.cc.test.", out)
        self.assertIn("up to 5 queries for one lookup, 10 packets", out)
        forwarder = Zone(zone_file("cluster.local.db"))
        cc = Zone(zone_file("cc.test.db"))
        internal = Zone(zone_file("cc.internal.db"))

        def resolver_answer(query):
            name = query.question[0].name
            for zone in (forwarder, internal, cc):
                if main.is_subdomain(name, zone.origin):
                    return zone.answer(query)
            return main.Message(id=query.id, qr=True, rcode=main.REFUSED, question=list(query.question))

        transport = FakeTransport({"127.0.10.10": resolver_answer})
        code, out, _ = run(["search", "www.cc.test", "--resolv-conf", pod, "--hostname", "", "--send", "--json"], transport=transport)
        data = json.loads(out)
        self.assertEqual((code, data["found"], data["ndots"], data["queries"]), (0, True, 5, 5))
        self.assertEqual([s["status"] for s in data["sent"]], ["NXDOMAIN"] * 4 + ["NOERROR"])
        self.assertTrue(all(rd for _, _, _, rd in transport.log))


class CliErrorTests(unittest.TestCase):
    def test_bad_input_exits_2_with_a_reason(self):
        cases = [
            (["query", "@127.0.0.1", "x.test", "BOGUS"], "unknown record type"),
            (["query", "@127.0.0.1", "a" * 64 + ".test"], "at most 63"),
            (["query", "@not-an-ip", "x.test"], "must be an IP address"),
            (["query", "@127.0.0.1", "x.test", "+bufsize=lots"], "expected 0 to 65535"),
            (["query", "@127.0.0.1", "x.test", "+frobnicate"], "unknown option"),
            (["resolve", "x.test", "--root", "nowhere"], "expected IP or NAME=IP"),
            (["search", "x.test", "--resolv-conf", "/nonexistent/resolv.conf"], "cannot read"),
        ]
        for argv, message in cases:
            with self.subTest(argv=argv):
                code, _, err = run(argv)
                self.assertEqual(code, 2)
                self.assertIn(message, err)


if __name__ == "__main__":
    unittest.main()
