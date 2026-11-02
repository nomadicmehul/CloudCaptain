import io
import ipaddress
import json
import os
import struct
import sys
import tempfile
import types
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import main  # noqa: E402

T0 = 1793523600 * main.NS
CLIENT, SERVER = "10.0.0.1", "10.0.0.2"
LIBPCAP_COUNTS_ON_DEMO = {
    "": 48,
    "tcp": 39,
    "udp": 9,
    "ip": 42,
    "ip6": 6,
    "port 80": 18,
    "tcp port 80": 18,
    "udp port 53": 9,
    "port 53 or 80": 27,
    "host 10.0.0.3": 3,
    "src host 10.0.0.1": 25,
    "dst host 10.0.0.1": 17,
    "net 10.0.0.0/24": 42,
    "net 2001:db8::/32": 6,
    "not tcp": 9,
    "tcp and not port 80": 21,
    "tcp[tcpflags] & tcp-syn != 0": 8,
    "tcp[tcpflags] & (tcp-syn|tcp-ack) == tcp-syn": 6,
    "tcp[13] & 4 != 0": 2,
    "tcp[tcpflags] == tcp-syn": 6,
    "ip[8] = 64": 42,
    "udp[8:2] = 0x1a2b": 2,
    "portrange 43512-43516": 23,
    "src or dst port 9000": 10,
    "src and dst net 10.0.0.0/8": 42,
    "host 10.0.0.1 and (port 80 or 5432)": 20,
    "not host 10.0.0.53 and udp": 0,
    "ip6[53] & 2 != 0": 2,
    "dst port 53 and src 10.0.0.1": 5,
    "tcp dst port 443 or 9000": 13,
    "host 10.0.0.3 or host 10.0.0.53 and udp": 9,
    "host 10.0.0.3 or (host 10.0.0.53 and udp)": 12,
    "udp and port 53 or tcp": 48,
}


def tcp(src, dst, sport, dport, seq, ack=0, flags=main.ACK, window=502, payload=b"", options=b""):
    return main.ip_packet(src, dst, main.IPPROTO_TCP, main.tcp_segment(src, dst, sport, dport, seq, ack, flags, window, options, payload))


def syn(src=CLIENT, dst=SERVER, sport=40000, dport=80, seq=100):
    options = main.tcp_options(mss=1460, sack_ok=True, timestamps=(1, 0), wscale=7)
    return tcp(src, dst, sport, dport, seq, 0, main.SYN, 65535, options=options)


def frames_of(packets, linktype=main.LINKTYPE_ETHERNET, step_ms=1):
    return [(T0 + i * step_ms * 1_000_000, main.link_frame(p, linktype)) for i, p in enumerate(packets)]


def decoded(packets, linktype=main.LINKTYPE_ETHERNET):
    capture = main.read_capture(main.write_pcap(frames_of(packets, linktype), linktype))
    return [main.decode(p) for p in capture.packets]


def demo():
    return [main.decode(p) for p in main.read_capture(main.demo_capture()).packets]


def count(expression, items):
    selected = main.compile_filter(expression)
    return sum(1 for d in items if selected(d))


def handshake(sport=40000, isn=100, server_isn=900):
    return [
        tcp(CLIENT, SERVER, sport, 80, isn, 0, main.SYN, 64240, options=main.tcp_options(mss=1460, wscale=7)),
        tcp(SERVER, CLIENT, 80, sport, server_isn, isn + 1, main.SYN | main.ACK, 65160, options=main.tcp_options(mss=1460, wscale=7)),
        tcp(CLIENT, SERVER, sport, 80, isn + 1, server_isn + 1),
    ]


def run(argv):
    out, err = StringIO(), StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = main.main(argv)
    return code, out.getvalue(), err.getvalue()


class TempDirTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        for name in os.listdir(self.tmp):
            os.remove(os.path.join(self.tmp, name))
        os.rmdir(self.tmp)

    def write(self, name, data):
        path = os.path.join(self.tmp, name)
        with open(path, "wb") as handle:
            handle.write(data)
        return path


class FileFormatTests(TempDirTest):
    def test_pcap_reads_both_byte_orders_and_both_timestamp_precisions(self):
        frames = [(T0 + 123456789, main.link_frame(syn(), main.LINKTYPE_ETHERNET))]
        little = main.read_capture(main.write_pcap(frames))
        big_nano = main.read_capture(main.write_pcap(frames, nanoseconds=True, big_endian=True))
        self.assertEqual((little.format, little.byte_order, little.precision, little.version), ("pcap", "little", "us", "2.4"))
        self.assertEqual((big_nano.byte_order, big_nano.precision), ("big", "ns"))
        self.assertEqual(little.packets[0].ts_ns, T0 + 123456000)
        self.assertEqual(big_nano.packets[0].ts_ns, T0 + 123456789)
        self.assertEqual(little.packets[0].data, big_nano.packets[0].data)
        self.assertEqual(little.linktypes, [main.LINKTYPE_ETHERNET])

    def test_snaplen_cuts_the_bytes_but_lengths_come_from_the_headers(self):
        data = tcp(SERVER, CLIENT, 80, 40000, 901, 101, main.PSH | main.ACK, payload=b"x" * 1000)
        capture = main.read_capture(main.write_pcap(frames_of([syn(), data]), snaplen=60))
        self.assertEqual([len(p.data) for p in capture.packets], [60, 60])
        self.assertTrue(all(p.truncated for p in capture.packets))
        first, second = (main.decode(p) for p in capture.packets)
        self.assertEqual(second.payload_len, 1000)
        self.assertEqual(len(second.payload), 6)
        self.assertEqual(main.flags_text(first.tcp.flags), "S")
        self.assertEqual(first.problem, "[|tcp]")
        self.assertEqual(first.tcp.mss, 1460)
        self.assertEqual(main.capture_info(capture)["truncated_packets"], 2)

    def test_a_cut_off_dump_file_keeps_the_packets_before_the_damage(self):
        whole = main.write_pcap(frames_of([syn(), syn(sport=40001), syn(sport=40002)]))
        cut = main.read_capture(whole[:-10])
        self.assertEqual(len(cut.packets), 2)
        self.assertIn("truncated dump file; tried to read", cut.damage)
        header_cut = main.read_capture(whole[:24 + 16 + len(cut.packets[0].data) + 5])
        self.assertEqual(len(header_cut.packets), 1)
        self.assertIn("header bytes, only got 5", header_cut.damage)
        code, out, err = run(["read", self.write("cut.pcap", whole[:-10])])
        self.assertEqual(code, 1)
        self.assertEqual(len(out.splitlines()), 2)
        self.assertIn("truncated dump file", err)

    def test_files_that_are_not_captures_are_rejected_with_exit_2(self):
        with self.assertRaisesRegex(main.CaptureError, "unknown magic number 48545450"):
            main.read_capture(b"HTTP/1.1 200 OK\r\n\r\n" + b"\0" * 20)
        with self.assertRaisesRegex(main.CaptureError, "gzip"):
            main.read_capture(b"\x1f\x8b\x08\x00" + b"\0" * 30)
        with self.assertRaisesRegex(main.CaptureError, "24 bytes"):
            main.read_capture(struct.pack("<I", main.PCAP_MAGIC_MICRO) + b"\0" * 4)
        code, _, err = run(["info", self.write("notes.txt", b"not a capture at all, just text")])
        self.assertEqual(code, 2)
        self.assertIn("pcaplens: capture: unknown magic number", err)
        code, _, err = run(["read", os.path.join(self.tmp, "missing.pcap")])
        self.assertEqual(code, 2)
        self.assertIn("No such file", err)

    def test_pcapng_section_interface_and_enhanced_packet_blocks_in_both_byte_orders(self):
        frames = frames_of([syn(), syn(sport=40001)])
        frames[1] = (frames[1][0] + 7, frames[1][1])
        for big in (False, True):
            capture = main.read_capture(main.write_pcapng(frames, big_endian=big))
            self.assertEqual((capture.format, capture.version, capture.precision, capture.snaplen), ("pcapng", "1.0", "ns", main.MAX_SNAPLEN))
            self.assertEqual(capture.byte_order, "big" if big else "little")
            self.assertEqual([p.ts_ns for p in capture.packets], [ts for ts, _ in frames])
            self.assertEqual([p.data for p in capture.packets], [f for _, f in frames])

    def test_pcapng_simple_packets_unknown_blocks_tsresol_and_a_missing_interface(self):
        frame = main.link_frame(syn(), main.LINKTYPE_ETHERNET)
        shb = main.pcapng_block(main.PCAPNG_SHB, struct.pack("<IHHq", main.PCAPNG_BYTE_ORDER_MAGIC, 1, 0, -1), "<")
        idb = main.pcapng_block(main.PCAPNG_IDB, struct.pack("<HHI", main.LINKTYPE_ETHERNET, 0, 0), "<")
        custom = main.pcapng_block(0x00000BAD, b"vendor data", "<")
        spb = main.pcapng_block(main.PCAPNG_SPB, struct.pack("<I", len(frame)) + frame, "<")
        epb = main.pcapng_block(main.PCAPNG_EPB, struct.pack("<IIIII", 0, 0, 1_500_000, len(frame), len(frame)) + frame, "<")
        capture = main.read_capture(shb + idb + custom + spb + epb)
        self.assertEqual(len(capture.packets), 2)
        self.assertEqual(capture.precision, "us")
        self.assertEqual(capture.packets[1].ts_ns, 1_500_000_000)
        self.assertEqual(capture.packets[0].data, frame)
        self.assertEqual(main.tsresol_units(b"\x09"), 10 ** 9)
        self.assertEqual(main.tsresol_units(b"\x8a"), 1024)
        self.assertEqual(main.tsresol_units(None), 10 ** 6)
        orphan = main.pcapng_block(main.PCAPNG_EPB, struct.pack("<IIIII", 1, 0, 0, len(frame), len(frame)) + frame, "<")
        with self.assertRaisesRegex(main.CaptureError, "refers to interface 1, but only 1 interface"):
            main.read_capture(shb + idb + orphan)


class LinkAndNetworkTests(unittest.TestCase):
    def test_every_link_type_decodes_to_the_same_tcp_segment(self):
        packet = syn()
        expected = ("10.0.0.1", "10.0.0.2", 40000, 80, main.SYN, 100)
        for linktype in (main.LINKTYPE_ETHERNET, main.LINKTYPE_LINUX_SLL, main.LINKTYPE_LINUX_SLL2, main.LINKTYPE_NULL, main.LINKTYPE_RAW):
            d = main.decode(main.Packet(1, T0, linktype, main.link_frame(packet, linktype, outgoing=True, ifindex=3), 0))
            self.assertEqual((str(d.src), str(d.dst), d.sport, d.dport, d.tcp.flags, d.tcp.seq), expected, main.LINKTYPES[linktype])
        sll = main.decode(main.Packet(1, T0, main.LINKTYPE_LINUX_SLL, main.link_frame(packet, main.LINKTYPE_LINUX_SLL, outgoing=True), 0))
        sll2 = main.decode(main.Packet(1, T0, main.LINKTYPE_LINUX_SLL2, main.link_frame(packet, main.LINKTYPE_LINUX_SLL2, ifindex=3), 0))
        self.assertEqual((sll.link, sll.direction, sll.ifindex, sll.net_offset), ("sll", "Out", None, 16))
        self.assertEqual((sll2.link, sll2.direction, sll2.ifindex, sll2.net_offset), ("sll2", "In", 3, 20))
        self.assertTrue(main.summary(sll2).startswith("ifindex 3 In IP 10.0.0.1.40000 > 10.0.0.2.80: Flags [S]"))

    def test_vlan_tags_are_skipped_and_arp_matches_host_but_not_ip_host(self):
        tagged = b"\0" * 12 + b"\x81\x00\x00\x64\x08\x00" + syn()
        d = main.decode(main.Packet(1, T0, main.LINKTYPE_ETHERNET, tagged, len(tagged)))
        self.assertEqual((d.net_offset, d.version, d.dport), (18, 4, 80))
        mac = b"\x02\x00\x00\x00\x00\x01"
        arp_body = struct.pack("!HHBBH", 1, 0x0800, 6, 4, 1) + mac + ipaddress.ip_address(CLIENT).packed + b"\0" * 6 + ipaddress.ip_address(SERVER).packed
        arp = main.decode(main.Packet(2, T0, main.LINKTYPE_ETHERNET, b"\xff" * 6 + mac + b"\x08\x06" + arp_body, 42))
        self.assertEqual(main.summary(arp), "ARP, Request who-has 10.0.0.2 tell 10.0.0.1, length 28")
        self.assertEqual([count(e, [arp]) for e in ("host 10.0.0.2", "ip host 10.0.0.2", "arp", "src host 10.0.0.1", "dst host 10.0.0.1", "ip")], [1, 0, 1, 1, 0, 0])

    def test_ipv4_fragments_after_the_first_carry_no_ports(self):
        segment = main.tcp_segment(CLIENT, SERVER, 40000, 80, 1, 0, main.SYN, payload=b"x" * 40)
        first = main.ip_packet(CLIENT, SERVER, main.IPPROTO_TCP, segment[:24], ident=7, frag=0x2000)
        later = main.ip_packet(CLIENT, SERVER, main.IPPROTO_TCP, segment[24:], ident=7, frag=0x0003)
        items = decoded([first, later])
        self.assertEqual([(d.fragment, d.first_fragment, d.sport) for d in items], [(True, True, 40000), (True, False, None)])
        self.assertEqual([count(e, items) for e in ("tcp", "port 80", "tcp[13] & 2 != 0", "ip")], [2, 1, 1, 2])
        self.assertEqual(main.summary(items[1]), "IP 10.0.0.1 > 10.0.0.2: fragment of tcp, length 36")

    def test_ipv6_extension_headers_are_walked_to_the_transport_header(self):
        src, dst = "2001:db8::1", "2001:db8::2"
        segment = main.tcp_segment(src, dst, 50000, 443, 7, 0, main.SYN)
        hop_by_hop = bytes([main.IPPROTO_TCP, 0, 1, 4, 0, 0, 0, 0])
        body = hop_by_hop + segment
        packet = struct.pack("!IHBB", 6 << 28, len(body), 0, 64) + ipaddress.ip_address(src).packed + ipaddress.ip_address(dst).packed + body
        (d,) = decoded([packet])
        self.assertEqual((d.version, d.proto, d.sport, d.dport, d.l4_len, d.l4_offset), (6, main.IPPROTO_TCP, 50000, 443, len(segment), 14 + 48))
        self.assertEqual(count("ip6 and tcp port 443", [d]), 1)

    def test_tcp_options_flags_and_the_tcpdump_style_line(self):
        (d,) = decoded([syn()])
        self.assertEqual((d.tcp.mss, d.tcp.wscale, d.tcp.sack_ok, d.tcp.timestamps), (1460, 7, True, (1, 0)))
        self.assertEqual(main.summary(d), "IP 10.0.0.1.40000 > 10.0.0.2.80: Flags [S], seq 100, win 65535, options [mss 1460,sackOK,TS val 1 ecr 0,nop,wscale 7], length 0")
        self.assertEqual([main.flags_text(f) for f in (main.SYN | main.ACK, main.FIN | main.PSH | main.ACK, main.RST | main.ACK, 0)], ["S.", "FP.", "R.", "none"])
        (data,) = decoded([tcp(CLIENT, SERVER, 40000, 80, 1, 1, main.PSH | main.ACK, payload=b"hello", options=main.tcp_options(sack=[(10, 20)]))])
        self.assertEqual(main.summary(data), "IP 10.0.0.1.40000 > 10.0.0.2.80: Flags [P.], seq 1:6, ack 1, win 502, options [nop,nop,sack 1 {10:20}], length 5")
        self.assertEqual(data.tcp.sack, [(10, 20)])

    def test_inet_checksum_matches_the_textbook_example_and_built_headers_verify(self):
        header = bytes.fromhex("450000730000400040110000c0a80001c0a800c7")
        self.assertEqual(main.inet_checksum(header), 0xB861)
        packet = syn()
        self.assertEqual(main.inet_checksum(packet[:20]), 0)
        self.assertEqual(main.inet_checksum(main.pseudo_header(CLIENT, SERVER, main.IPPROTO_TCP, len(packet) - 20) + packet[20:]), 0)


class FilterTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.demo = demo()

    def test_filter_counts_match_libpcap_on_the_demo_capture(self):
        for expression, expected in LIBPCAP_COUNTS_ON_DEMO.items():
            self.assertEqual(count(expression, self.demo), expected, expression)

    def test_tcp_byte_loads_only_see_ipv4_like_libpcap(self):
        all_syns = sum(1 for d in self.demo if d.tcp and d.tcp.flags & main.SYN)
        self.assertEqual(all_syns, 10)
        self.assertEqual(count("tcp[tcpflags] & tcp-syn != 0", self.demo), 8)
        self.assertEqual(count("(tcp[tcpflags] & tcp-syn != 0) or (ip6[6] = 6 and ip6[53] & tcp-syn != 0)", self.demo), 10)

    def test_a_load_past_the_captured_bytes_rejects_the_packet_even_under_not(self):
        self.assertEqual(count("tcp[100] = 1", self.demo), 0)
        self.assertEqual(count("not tcp[100] = 1", self.demo), 23)
        self.assertEqual(count("udp or not tcp[100] = 1", self.demo), 23)

    def test_and_and_or_have_equal_precedence_and_group_left_to_right(self):
        self.assertEqual(count("host 10.0.0.3 or host 10.0.0.53 and udp", self.demo), count("(host 10.0.0.3 or host 10.0.0.53) and udp", self.demo))
        self.assertNotEqual(count("host 10.0.0.3 or host 10.0.0.53 and udp", self.demo), count("host 10.0.0.3 or (host 10.0.0.53 and udp)", self.demo))
        tree = main.filter_tree(main.compile_filter("tcp or udp and not port 53").root)
        self.assertEqual([line.split("    --")[0] for line in tree], ["and", "  or", "    tcp", "    udp", "  not", "    port 53"])

    def test_an_identifier_without_a_keyword_inherits_the_previous_one(self):
        self.assertEqual(count("port 53 or 80", self.demo), count("port 53 or port 80", self.demo))
        self.assertEqual(count("host 10.0.0.3 or 10.0.0.53", self.demo), count("host 10.0.0.3 or host 10.0.0.53", self.demo))
        self.assertEqual(count("src 10.0.0.53 or 10.0.0.2", self.demo), count("src host 10.0.0.53 or src host 10.0.0.2", self.demo))
        tree = main.filter_tree(main.compile_filter("not port 53 and 80").root)
        self.assertEqual([line.split("    --")[0] for line in tree], ["and", "  not", "    port 53", "  port 80"])

    def test_services_numbers_and_named_constants(self):
        self.assertEqual(count("port http", self.demo), 18)
        self.assertEqual(count("tcp[13] = 0x12", self.demo), count("tcp[tcpflags] == (tcp-syn|tcp-ack)", self.demo))
        self.assertEqual(count("tcp[13] = 022", self.demo), count("tcp[13] = 18", self.demo))
        self.assertEqual(count("tcp[2:2] = 80", self.demo), count("ip and tcp dst port 80", self.demo))
        self.assertEqual(main.compile_filter("").root, None)
        self.assertTrue(main.compile_filter("  ")(self.demo[0]))

    def test_filter_errors_name_the_problem_and_exit_2(self):
        cases = {
            "net 10.0.0.1/8": "host bits set",
            "tcp host 1.2.3.4": "not tcp",
            "host": "expected an address",
            "ip6 host 10.0.0.1": "IPv6",
            "tcp[13:3] = 1": "1, 2 or 4",
            "ether host 1": "outside pcaplens",
            "host example.com": "does not resolve names",
            "port 70000": "valid port",
            "port https-ssh-x": "not a port number",
            "(tcp": "expected ')'",
            "tcp and": "ends where",
            "tcp[13] & 2": "expected a comparison",
            "tcp $ udp": "unexpected character",
            "tcp udp": "unexpected 'udp'",
        }
        for expression, fragment in cases.items():
            with self.assertRaises(main.FilterError, msg=expression) as caught:
                main.compile_filter(expression)
            self.assertIn(fragment, str(caught.exception), expression)
        code, _, err = run(["filter", "net 10.0.0.1/8"])
        self.assertEqual(code, 2)
        self.assertIn("pcaplens: filter:", err)


class FlowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.flows = {main.endpoint_text(f.server): f for f in main.build_flows(demo()) if f.proto == "tcp"}

    def test_the_lossy_transfer_shows_rtt_retransmission_hole_and_duplicate_acks(self):
        f = self.flows["10.0.0.2:80"]
        out = main.flow_json(f)
        self.assertEqual(out["state"], "closed")
        self.assertEqual(out["handshake"], {"syn_to_synack_ms": 20.0, "rtt_ms": 20.1})
        self.assertEqual(out["bytes"], {"client": 81, "server": 6792})
        self.assertEqual(out["packets"], {"client": 10, "server": 8})
        self.assertEqual(out["dup_acks"], {"client": 3, "server": 0})
        self.assertEqual(out["retransmissions"], {"client": 0, "server": 1})
        self.assertEqual(out["gaps"], {"client": 0, "server": 1})
        self.assertEqual((out["mss"], out["wscale"]), ({"client": 1460, "server": 1460}, {"client": 7, "server": 7}))
        self.assertEqual(out["closed_by"], "client FIN")

    def test_refused_unanswered_and_reset_connections_are_told_apart(self):
        refused, silent, reset = self.flows["10.0.0.2:5432"], self.flows["10.0.0.3:443"], self.flows["10.0.0.2:9000"]
        self.assertEqual((refused.state, refused.closed_by), ("refused", "server RST"))
        self.assertIn("nothing listens on 10.0.0.2:5432", main.flow_notes(refused)[0])
        self.assertEqual((silent.state, silent.up.syns, silent.up.retransmissions), ("no answer", 3, 2))
        self.assertFalse(any("retransmitted" in n for n in main.flow_notes(silent)))
        self.assertEqual((reset.state, reset.closed_by, reset.up.zero_windows, reset.down.keepalives), ("reset", "client RST", 2, 1))
        self.assertTrue(any("zero window 2 time(s)" in n for n in main.flow_notes(reset)))
        self.assertEqual(self.flows["[2001:db8::2]:443"].state, "closed")

    def test_keepalives_are_not_retransmissions(self):
        packets = handshake() + [
            tcp(CLIENT, SERVER, 40000, 80, 100, 901),
            tcp(SERVER, CLIENT, 80, 40000, 901, 101),
            tcp(CLIENT, SERVER, 40000, 80, 100, 901, payload=b"\0"),
        ]
        (f,) = main.build_flows(decoded(packets))
        self.assertEqual((f.up.keepalives, f.up.retransmissions, f.down.dup_acks), (2, 0, 0))
        self.assertEqual(f.state, "open")

    def test_a_capture_that_starts_mid_stream_guesses_the_client_from_the_port(self):
        packets = [
            tcp(SERVER, CLIENT, 443, 51000, 5000, 7000, main.PSH | main.ACK, payload=b"y" * 100),
            tcp(CLIENT, SERVER, 51000, 443, 7000, 5100),
        ]
        (f,) = main.build_flows(decoded(packets))
        self.assertEqual((main.endpoint_text(f.client), f.guessed, f.state, f.rtt_ns), ("10.0.0.1:51000", True, "mid-stream", None))
        self.assertEqual(main.flow_json(f)["handshake"], None)
        self.assertIn("no SYN in the capture", main.flow_notes(f)[-1])

    def test_port_reuse_after_a_close_starts_a_new_flow(self):
        first = handshake(isn=100) + [tcp(CLIENT, SERVER, 40000, 80, 101, 901, main.FIN | main.ACK)]
        second = handshake(isn=5000, server_isn=7000)
        flows = main.build_flows(decoded(first + second))
        self.assertEqual([(f.state, f.client_isn) for f in flows], [("half-closed", 100), ("open", 5000)])


class DnsTests(unittest.TestCase):
    def test_names_follow_compression_pointers_and_rdata_types_decode(self):
        header = struct.pack("!6H", 7, 0x8180, 1, 6, 0, 0)
        question = main.dns_name("www.example.com") + struct.pack("!HH", 1, 1)
        records = [
            (5, b"\x03web\xc0\x10"),
            (1, ipaddress.ip_address("192.0.2.7").packed),
            (28, ipaddress.ip_address("2001:db8::7").packed),
            (15, struct.pack("!H", 10) + b"\x02mx\xc0\x10"),
            (16, b"\x05hello\x03you"),
            (33, struct.pack("!HHH", 1, 5, 5060) + b"\x03sip\xc0\x10"),
        ]
        body = b"".join(b"\xc0\x0c" + struct.pack("!HHIH", rtype, 1, 60, len(rdata)) + rdata for rtype, rdata in records)
        msg = main.parse_dns(header + question + body)
        self.assertEqual(msg.questions, [("www.example.com.", 1)])
        self.assertEqual([r.name for r in msg.answers], ["www.example.com."] * 6)
        self.assertEqual([(r.type_name, r.data) for r in msg.answers], [
            ("CNAME", "web.example.com."),
            ("A", "192.0.2.7"),
            ("AAAA", "2001:db8::7"),
            ("MX", "10 mx.example.com."),
            ("TXT", '"hello" "you"'),
            ("SRV", "1 5 5060 sip.example.com."),
        ])
        self.assertEqual((msg.ident, msg.response, msg.rd, msg.ra, msg.aa, msg.rcode_name), (7, True, True, True, False, "NOERROR"))

    def test_pointer_loops_and_short_messages_raise_value_error(self):
        looped = struct.pack("!6H", 1, 0, 1, 0, 0, 0) + b"\xc0\x0c" + struct.pack("!HH", 1, 1)
        with self.assertRaisesRegex(ValueError, "loop"):
            main.parse_dns(looped)
        with self.assertRaisesRegex(ValueError, "12 bytes"):
            main.parse_dns(b"\x00\x01\x02")
        overlong = struct.pack("!6H", 2, 0x8180, 1, 1, 0, 0) + main.dns_name("a.test") + struct.pack("!HH", 1, 1) + b"\xc0\x0c" + struct.pack("!HHIH", 1, 1, 60, 40) + b"\x01\x02"
        with self.assertRaisesRegex(ValueError, "claims 40 bytes"):
            main.parse_dns(overlong)

    def test_transactions_pair_queries_with_answers_and_count_retries(self):
        out = [main.transaction_json(t) for t in main.dns_transactions(demo())]
        self.assertEqual([(t["question"]["name"], t["question"]["type"], t["rcode"]) for t in out], [
            ("app.example.com.", "A", "NOERROR"),
            ("app.example.com.", "AAAA", "NOERROR"),
            ("nosuch.example.com.", "A", "NXDOMAIN"),
            ("db.example.com.", "A", "NOERROR"),
        ])
        self.assertEqual(out[0]["answers"], [{"name": "app.example.com.", "type": "A", "ttl": 300, "data": "203.0.113.10"}])
        self.assertEqual((out[0]["latency_ms"], out[0]["retries"], out[0]["flags"]), (12.0, 0, "rd ra"))
        self.assertEqual((out[1]["answers"], out[1]["authority"][0]["type"], out[1]["authority"][0]["data"].split()[-1]), ([], "SOA", "60"))
        self.assertEqual((out[3]["latency_ms"], out[3]["retries"]), (5010.0, 1))

    def test_unanswered_queries_and_the_tcpdump_style_lines(self):
        query = main.ip_packet(CLIENT, "10.0.0.53", main.IPPROTO_UDP, main.udp_datagram(CLIENT, "10.0.0.53", 53000, 53, main.dns_message(6699, 0x0100, [("app.example.com", 1)])))
        (t,) = main.dns_transactions(decoded([query]))
        self.assertEqual((t.rcode, t.latency_ns, t.response_ns), (None, None, None))
        lines = [main.summary(d).split(": ", 1)[1] for d in demo() if main.is_dns(d)]
        self.assertEqual(lines[:2], ["6699+ A? app.example.com. (33)", "6699 1/0/0 A 203.0.113.10 (49)"])
        self.assertIn("11068 NXDomain 0/1/0 (120)", lines)
        authoritative = main.parse_dns(main.dns_message(9, 0x8403, [("nosuch.cc.test", 1)]))
        self.assertEqual(main.dns_text(authoritative, 32), "9 NXDomain*- 0/0/0 (32)")


class CliTests(TempDirTest):
    def setUp(self):
        super().setUp()
        self.path = self.write("demo.pcap", main.demo_capture())

    def test_read_prints_counts_lines_and_json(self):
        self.assertEqual(run(["read", self.path, "tcp port 80", "--count"])[1].strip(), "18")
        self.assertEqual(run(["read", self.path, "tcp", "port", "80", "--count"])[1].strip(), "18")
        code, out, _ = run(["read", self.path, "-c", "2", "--time", "rel"])
        self.assertEqual(code, 0)
        self.assertEqual([line.split()[0] for line in out.splitlines()], ["0.000000", "0.020000"])
        packets = json.loads(run(["read", self.path, "udp port 53 and src host 10.0.0.53", "--json"])[1])
        self.assertEqual(len(packets), 4)
        self.assertEqual(packets[0]["dns"]["answers"][0]["data"], "203.0.113.10")
        syn_packet = json.loads(run(["read", self.path, "-c", "1", "--json"])[1])[0]
        self.assertEqual((syn_packet["tcp"]["flags"], syn_packet["tcp"]["mss"], syn_packet["sport"], syn_packet["len"]), ("S", 1460, 43512, 74))

    def test_flows_dns_info_and_filter_commands(self):
        flows = json.loads(run(["flows", self.path, "tcp", "--json"])[1])
        self.assertEqual([f["state"] for f in flows], ["closed", "refused", "no answer", "reset", "closed"])
        table = run(["flows", self.path, "port 80"])[1]
        self.assertIn("client sent 3 duplicate ACK(s)", table)
        dns = json.loads(run(["dns", self.path, "--json"])[1])
        self.assertEqual(len(dns), 4)
        self.assertEqual(run(["dns", self.path, "--port", "5353", "--json"])[1].strip(), "[]")
        info = json.loads(run(["info", self.path, "--json"])[1])
        self.assertEqual((info["packets"], info["linktypes"], info["duration_s"], info["damage"]), (48, [{"id": 1, "name": "EN10MB"}], 5.61, None))
        tree = json.loads(run(["filter", "tcp", "and", "not", "port", "80", "--json"])[1])
        self.assertEqual((tree["op"], tree["children"][0]["primitive"], tree["children"][1]["op"]), ("and", "tcp", "not"))
        self.assertIn("IPv4-only", run(["filter", "tcp[13] & 2 != 0"])[1])

    def test_demo_writes_pcap_and_pcapng_that_read_back_the_same(self):
        lines = {}
        for fmt in ("pcap", "pcapng"):
            out_path = os.path.join(self.tmp, f"cooked.{fmt}")
            code, out, _ = run(["demo", out_path, "--format", fmt, "--link", "sll2"])
            self.assertEqual(code, 0)
            self.assertIn("LINUX_SLL2, 48 packets", out)
            lines[fmt] = run(["read", out_path])[1]
        self.assertEqual(lines["pcap"], lines["pcapng"])
        self.assertTrue(lines["pcap"].splitlines()[0].endswith("ifindex 1 Out IP 10.0.0.1.43512 > 10.0.0.2.80: Flags [S], seq 1000, win 64240, options [mss 1460,sackOK,TS val 1000 ecr 0,nop,wscale 7], length 0"))
        self.assertEqual(run(["read", os.path.join(self.tmp, "cooked.pcapng"), "ip6", "--count"])[1].strip(), "6")

    def test_a_dash_reads_the_capture_from_stdin(self):
        stdin = types.SimpleNamespace(buffer=io.BytesIO(main.demo_capture()))
        with mock.patch.object(sys, "stdin", stdin):
            code, out, _ = run(["read", "-", "host 10.0.0.3", "--count"])
        self.assertEqual((code, out.strip()), (0, "3"))


if __name__ == "__main__":
    unittest.main()
