import contextlib
import io
import json
import os
import shutil
import socket
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import main  # noqa: E402

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"


def fixture(name):
    return (FIXTURES / name).read_text()


def proc_table(**kwargs):
    socks = main.parse_proc_net_tcp(fixture("proc-net-tcp.txt"), "little") + main.parse_proc_net_tcp(fixture("proc-net-tcp6.txt"), "little")
    return main.Table(socks, **kwargs)


def ss_table(**kwargs):
    socks = main.parse_ss(fixture("ss-tanpio.txt"))
    return main.Table(socks, owners_known=True, **kwargs)


def kinds(findings, severity=None):
    return [f.kind for f in findings if severity is None or f.severity == severity]


def run(argv):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = main.main(argv)
    return code, out.getvalue(), err.getvalue()


def proc_row(n, state, lport, rport, local="0500000A", remote="0900000A", tx=0, rx=0, timer=0, when=0, retr=0, inode=0):
    return f"{n:4d}: {local}:{lport:04X} {remote}:{rport:04X} {state:02X} {tx:08X}:{rx:08X} {timer:02X}:{when:08X} {retr:08X}  1000        0 {inode} 1 0000000000000000 20 4 30 10 -1"


class ProcParsingTests(unittest.TestCase):
    def test_ipv4_words_are_host_byte_order_and_ports_are_plain_hex(self):
        self.assertEqual(main.decode_address("0100007F", "little"), "127.0.0.1")
        self.assertEqual(main.decode_address("0200CB0A", "little"), "10.203.0.2")
        self.assertEqual(main.decode_address("7F000001", "big"), "127.0.0.1")
        self.assertEqual(main.split_hex_endpoint("0100007F:1F90", "little"), ("127.0.0.1", 8080))

    def test_ipv6_reverses_each_32_bit_word_and_keeps_v4_mapped_dotted(self):
        self.assertEqual(main.decode_address("B80D0120000000000000000005000000", "little"), "2001:db8::5")
        self.assertEqual(main.decode_address("0000000000000000FFFF00000500000A", "little"), "::ffff:10.0.0.5")
        self.assertEqual(main.decode_address("00000000000000000000000001000000", "little"), "::1")
        with self.assertRaises(main.InputError):
            main.decode_address("0100007", "little")

    def test_fixture_rows_give_state_queues_timer_retransmits_and_inode(self):
        socks = {(s.state, s.lport, s.rport): s for s in proc_table().socks}
        listener = socks[("LISTEN", 8082, 0)]
        self.assertEqual((listener.local, listener.recv_q, listener.send_q, listener.inode), ("10.0.0.5", 2, 0, 30002))
        connecting = socks[("SYN_SENT", 46000, 5432)]
        self.assertEqual((connecting.timer, connecting.expires, connecting.retransmits, connecting.send_q), ("on", 6.0, 3, 1))
        waiting = socks[("TIME_WAIT", 40000, 5432)]
        self.assertEqual((waiting.timer, waiting.expires, waiting.inode), ("timewait", 41.0, 0))
        orphan = socks[("FIN_WAIT2", 45001, 8080)]
        self.assertEqual((orphan.timer, orphan.inode), ("timewait", 0))
        self.assertEqual(socks[("ESTABLISHED", 46200, 9000)].timer, "persist")
        self.assertEqual(socks[("ESTABLISHED", 40008, 5432)].timer, "keepalive")
        self.assertEqual(socks[("ESTABLISHED", 9100, 39000)].remote, "::ffff:10.0.0.3")

    def test_rows_that_are_not_proc_net_tcp_name_the_line(self):
        with self.assertRaises(main.InputError) as caught:
            main.parse_proc_net_tcp("  sl  local_address\n   0: 0100007F:1F90 garbage\n")
        self.assertIn("line 2", str(caught.exception))
        with self.assertRaises(main.InputError):
            main.parse_proc_net_tcp(proc_row(0, 1, 80, 80, local="ZZZZZZZZ"))


class SsParsingTests(unittest.TestCase):
    def test_states_addresses_brackets_wildcards_and_scopes(self):
        text = (
            "State  Recv-Q Send-Q  Local Address:Port  Peer Address:PortProcess\n"
            "ESTAB  0      0       [2001:db8::5]:22    [2001:db8::77]:50200\n"
            "LISTEN 0      4096    *:9100              *:*\n"
            "LISTEN 0      4096    127.0.0.53%lo:53    0.0.0.0:*\n"
            "LISTEN 0      128     [fe80::1%eth0]:8080 [::]:*\n"
            "TIME-WAIT 0   0       10.0.0.5:40000      10.0.0.9:5432\n"
        )
        socks = main.parse_ss(text)
        self.assertEqual([s.state for s in socks], ["ESTABLISHED", "LISTEN", "LISTEN", "LISTEN", "TIME_WAIT"])
        self.assertEqual((socks[0].local, socks[0].lport, socks[0].remote, socks[0].rport), ("2001:db8::5", 22, "2001:db8::77", 50200))
        self.assertEqual((socks[1].local, socks[1].remote, socks[1].rport), ("::", "::", 0))
        self.assertEqual(socks[2].local, "127.0.0.53")
        self.assertEqual(socks[3].local, "fe80::1")
        self.assertEqual(socks[2].send_q, 4096)

    def test_users_list_every_process_and_names_may_contain_spaces(self):
        text = 'LISTEN 0 511 0.0.0.0:443 0.0.0.0:* users:(("nginx",pid=900,fd=6),("nginx",pid=901,fd=6))\nESTAB 0 0 10.0.0.5:5000 10.0.0.6:443 users:(("Web Content",pid=5,fd=77))\n'
        listener, browser = main.parse_ss(text)
        self.assertEqual([(p.name, p.pid, p.fd) for p in listener.processes], [("nginx", 900, 6), ("nginx", 901, 6)])
        self.assertEqual(main.owner_label(listener, True), "nginx (pid 900), nginx (pid 901)")
        self.assertEqual(browser.processes[0].name, "Web Content")

    def test_info_lines_belong_to_the_row_above_and_timers_parse(self):
        socks = {(s.state, s.lport): s for s in main.parse_ss(fixture("ss-tanpio.txt"))}
        syn = socks[("SYN_SENT", 46000)]
        self.assertEqual((syn.timer, syn.expires, syn.retransmits), ("on", 6.0, 3))
        self.assertEqual((syn.info["rto"], syn.info["backoff"], syn.info["retrans"]), ("8000", "3", "3/3"))
        established = socks[("ESTABLISHED", 22)]
        self.assertEqual((established.info["send"], established.info["pacing_rate"], established.timer), ("371Mbps", "742Mbps", "keepalive"))
        self.assertIn("app_limited", established.info)
        self.assertEqual(main.parse_duration("118min"), 7080.0)
        self.assertEqual(main.parse_duration("3min55sec"), 235.0)
        self.assertEqual(main.parse_duration("204ms"), 0.204)
        self.assertEqual(main.parse_duration("1.204ms"), 1.204)

    def test_without_o_retransmits_come_from_retrans_or_backoff(self):
        text = "ESTAB 0 1448 10.0.0.5:46100 10.0.0.9:6379\n\t cubic rto:3264 backoff:4 retrans:4/9 unacked:1\nSYN-SENT 0 1 10.0.0.5:46000 10.0.0.30:5432\n\t cubic rto:8000 backoff:3 cwnd:1\n"
        busy, connecting = main.parse_ss(text)
        self.assertEqual((busy.timer, busy.retransmits), ("off", 4))
        self.assertEqual(connecting.retransmits, 3)

    def test_netid_column_and_other_socket_types_are_skipped(self):
        text = "Netid State Recv-Q Send-Q Local Address:Port Peer Address:Port\nudp UNCONN 0 0 0.0.0.0:68 0.0.0.0:*\n\t skmem:(r0,rb212992)\ntcp ESTAB 0 0 10.0.0.5:22 10.0.0.2:50122\n\t cubic rto:204\n"
        socks = main.parse_ss(text)
        self.assertEqual([(s.state, s.lport) for s in socks], [("ESTABLISHED", 22)])
        self.assertEqual(socks[0].info["rto"], "204")
        with self.assertRaises(main.InputError):
            main.parse_ss("hello world\n")

    def test_proc_and_ss_fixtures_describe_the_same_sockets(self):
        def key(s):
            return (s.state, s.local, s.lport, s.remote, s.rport, s.recv_q, s.send_q if s.state != "LISTEN" else None)
        self.assertEqual(sorted(map(key, proc_table().socks)), sorted(map(key, ss_table().socks)))
        listeners = {s.lport: s.send_q for s in ss_table().socks if s.state == "LISTEN"}
        self.assertEqual(listeners[8082], 1)


class OwnerTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root)

    def make_process(self, pid, name, links):
        fd = self.root / "proc" / str(pid) / "fd"
        fd.mkdir(parents=True)
        (fd.parent / "comm").write_text(name + "\n")
        for number, target in links.items():
            os.symlink(target, fd / str(number))

    def test_socket_owners_follow_fd_links_and_skip_everything_else(self):
        self.make_process(4242, "python3", {3: "socket:[30001]", 4: "socket:[30101]", 5: "/var/log/app.log", 6: "pipe:[777]"})
        self.make_process(812, "sshd", {3: "socket:[18211]"})
        (self.root / "proc" / "self").mkdir()
        (self.root / "proc" / "999").mkdir()
        owners = main.socket_owners(self.root / "proc")
        self.assertEqual(sorted(owners), [18211, 30001, 30101])
        self.assertEqual([(p.name, p.pid, p.fd) for p in owners[30101]], [("python3", 4242, 4)])

    def test_scan_reads_a_fake_root_and_names_the_leaking_process(self):
        net = self.root / "proc" / "net"
        net.mkdir(parents=True)
        for name in ("tcp", "tcp6", "netstat", "snmp"):
            source = "proc-net-netstat.txt" if name == "netstat" else f"proc-net-{name}.txt"
            shutil.copy(FIXTURES / source, net / name)
        ports = self.root / "proc/sys/net/ipv4"
        ports.mkdir(parents=True)
        (ports / "ip_local_port_range").write_text("40000\t40009\n")
        self.make_process(4242, "python3", {n: f"socket:[{30101 + n - 4}]" for n in range(4, 10)})
        table = main.load_live(self.root, "little")
        self.assertEqual(table.port_range, (40000, 40009))
        self.assertEqual(table.counters["TcpExtListenOverflows"], 1873)
        self.assertEqual(table.counters["TcpRetransSegs"], 20391)
        leak = [f for f in main.diagnose(table) if f.kind == "close-wait"]
        self.assertEqual((leak[0].sockets, leak[0].owner, leak[0].severity), (6, "python3 (pid 4242)", "warning"))
        self.assertIn("ephemeral-ports", kinds(main.diagnose(table), "critical"))
        code, out, _ = run(["scan", "--root", str(self.root), "--backlog", "8082=1", "--json"])
        self.assertEqual(code, 1)
        self.assertIn("accept-queue", [f["kind"] for f in json.loads(out)["findings"] if f["severity"] == "critical"])

    def test_scan_without_proc_net_tcp_explains_itself(self):
        code, _, err = run(["scan", "--root", str(self.root)])
        self.assertEqual(code, 2)
        self.assertIn("Linux", err)


class DiagnoseTests(unittest.TestCase):
    def test_close_wait_is_grouped_per_process_and_port_with_the_unread_fin(self):
        finding = next(f for f in main.diagnose(ss_table()) if f.kind == "close-wait")
        self.assertEqual((finding.sockets, finding.port, finding.owner, finding.severity), (6, 8081, "python3 (pid 4242)", "warning"))
        self.assertIn("not even read to end of file", finding.summary)
        quiet = main.diagnose(ss_table(), main.Limits(close_wait=10))
        self.assertEqual(next(f for f in quiet if f.kind == "close-wait").severity, "info")

    def test_close_wait_without_a_process_on_a_listening_port_is_the_accept_queue(self):
        text = "LISTEN 3 1 10.0.0.5:8082 0.0.0.0:* users:((\"python3\",pid=4243,fd=3))\nCLOSE-WAIT 1 0 10.0.0.5:8082 10.0.0.21:52000\nCLOSE-WAIT 1 0 10.0.0.5:8082 10.0.0.21:52001\n"
        table = main.Table(main.parse_ss(text), owners_known=True)
        finding = next(f for f in main.diagnose(table) if f.kind == "close-wait")
        self.assertIn("still in the accept queue", finding.summary)
        self.assertEqual(finding.owner, "no process")

    def test_accept_queue_from_ss_send_q_and_from_proc_with_a_backlog_hint(self):
        full = next(f for f in main.diagnose(ss_table()) if f.kind == "accept-queue")
        self.assertEqual((full.severity, full.port, full.sockets), ("critical", 8082, 2))
        self.assertIn("Recv-Q 2 > backlog 1", full.summary)
        unknown = next(f for f in main.diagnose(proc_table()) if f.kind == "accept-queue")
        self.assertEqual(unknown.severity, "info")
        self.assertIn("--backlog", unknown.summary)
        hinted = next(f for f in main.diagnose(proc_table(backlogs={8082: 1})) if f.kind == "accept-queue")
        self.assertEqual(hinted.severity, "critical")
        at_limit = main.Table(main.parse_ss("LISTEN 4 4 0.0.0.0:80 0.0.0.0:*\n"))
        self.assertEqual([f.severity for f in main.diagnose(at_limit)], ["warning"])

    def test_half_open_handshakes_and_silent_peers(self):
        findings = {f.kind: f for f in main.diagnose(ss_table())}
        self.assertEqual((findings["syn-recv"].sockets, findings["syn-recv"].port, findings["syn-recv"].severity), (4, 443, "warning"))
        self.assertIn("up to 3 times", findings["syn-recv"].summary)
        self.assertEqual((findings["syn-sent"].peer, findings["syn-sent"].severity), ("10.0.0.30:5432", "warning"))
        self.assertIn("not even a RST", findings["syn-sent"].summary)
        fresh = main.Table(main.parse_ss("SYN-SENT 0 1 10.0.0.5:46000 10.0.0.30:5432\n"))
        self.assertEqual([(f.kind, f.severity) for f in main.diagnose(fresh)], [("syn-sent", "info")])

    def test_retransmitting_and_zero_window_peers_are_told_apart(self):
        findings = {f.kind: f for f in main.diagnose(proc_table())}
        self.assertEqual((findings["retransmit"].peer, findings["retransmit"].sockets), ("10.0.0.9:6379", 1))
        self.assertIn("up to 4 times, up to 1448 bytes", findings["retransmit"].summary)
        self.assertEqual(findings["zero-window"].peer, "10.0.0.10:9000")
        self.assertEqual((findings["fin-wait2"].sockets, findings["fin-wait2"].severity), (2, "info"))

    def test_ephemeral_ports_are_counted_per_destination_against_the_range(self):
        rows = [proc_row(i, 6, 50000 + i, 5432, timer=3, when=3000) for i in range(95)]
        rows += [proc_row(100 + i, 1, 50100 + i, 6379, inode=900 + i) for i in range(10)]
        rows += [proc_row(200, 10, 50150, 0, remote="00000000"), proc_row(201, 1, 22, 50122)]
        table = main.Table(main.parse_proc_net_tcp("\n".join(rows), "little"), port_range=(50000, 50099))
        ports = [f for f in main.diagnose(table) if f.kind == "ephemeral-ports"]
        self.assertEqual(len(ports), 1)
        self.assertEqual((ports[0].severity, ports[0].sockets, ports[0].peer), ("critical", 95, "10.0.0.9:5432"))
        self.assertIn("95 of 100 ephemeral ports (95%), 95 in TIME_WAIT", ports[0].summary)
        table.port_range = (50000, 50199)
        self.assertEqual([f.severity for f in main.diagnose(table) if f.kind == "ephemeral-ports"], [])
        table.port_range = (50000, 50149)
        self.assertEqual([f.severity for f in main.diagnose(table) if f.kind == "ephemeral-ports"], ["warning"])

    def test_counters_from_proc_net_netstat_snmp_and_nstat_agree(self):
        proc = main.parse_proc_counters(fixture("proc-net-netstat.txt"))
        proc.update(main.parse_proc_counters(fixture("proc-net-snmp.txt")))
        nstat = main.parse_nstat(fixture("nstat-az.txt"))
        for name, value in nstat.items():
            self.assertEqual(proc[name], value, name)
        self.assertEqual(proc["TcpMaxConn"], -1)
        with self.assertRaises(main.InputError):
            main.parse_proc_counters("TcpExt: A B\nIpExt: 1 2\n")

    def test_findings_are_sorted_by_severity_and_drive_the_exit_code(self):
        findings = main.diagnose(ss_table(counters=main.parse_nstat(fixture("nstat-az.txt"))))
        ranks = [main.SEVERITY_RANK[f.severity] for f in findings]
        self.assertEqual(ranks, sorted(ranks))
        self.assertEqual(findings[0].kind, "accept-queue")
        healthy = main.Table(main.parse_ss("ESTAB 0 0 10.0.0.5:22 10.0.0.2:50122\n"))
        self.assertEqual(main.diagnose(healthy), [])


class KernelDefaultTests(unittest.TestCase):
    def test_model_timeout_follows_the_kernel_formula(self):
        self.assertEqual(main.model_timeout(6, 1.0), 127.0)
        self.assertEqual(main.model_timeout(5, 1.0), 63.0)
        self.assertEqual(main.model_timeout(15, 0.2), 924.6)
        self.assertEqual(main.model_timeout(8, 0.2), 102.2)
        self.assertEqual(main.syn_schedule(6), [1.0, 3.0, 7.0, 15.0, 31.0, 63.0])

    def test_sysctl_reads_a_fake_root_and_turns_values_into_seconds(self):
        root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, root)
        values = {"ipv4/tcp_syn_retries": "6", "ipv4/tcp_synack_retries": "5", "ipv4/tcp_retries2": "15", "ipv4/tcp_keepalive_time": "7200", "ipv4/tcp_keepalive_intvl": "75", "ipv4/tcp_keepalive_probes": "9", "ipv4/ip_local_port_range": "32768\t60999", "ipv4/tcp_fin_timeout": "60", "core/somaxconn": "4096"}
        for name, value in values.items():
            path = root / "proc/sys/net" / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(value + "\n")
        read = main.read_sysctls(root)
        self.assertEqual(read["net.ipv4.ip_local_port_range"], "32768 60999")
        self.assertNotIn("net.ipv4.tcp_syn_linear_timeouts", read)
        derived = main.kernel_timeouts(read)
        self.assertEqual((derived["syn_timeout"], derived["synack_timeout"], derived["retries2_timeout"]), (127.0, 63.0, 924.6))
        self.assertEqual((derived["keepalive_detection"], derived["ephemeral_ports"], derived["somaxconn"]), (7875, 28232, 4096))
        code, out, _ = run(["sysctl", "--root", str(root)])
        self.assertEqual(code, 0)
        self.assertIn("fails after 127.0 s", out)
        code, _, err = run(["sysctl", "--root", str(root / "missing")])
        self.assertEqual(code, 2)


class StateMachineTests(unittest.TestCase):
    def test_handshake_and_active_close_transitions(self):
        self.assertEqual(main.step("CLOSED", "connect"), ("SYN_SENT", "SYN"))
        self.assertEqual(main.step("LISTEN", "SYN"), ("SYN_RECV", "SYN+ACK"))
        self.assertEqual(main.step("SYN_SENT", "SYN+ACK"), ("ESTABLISHED", "ACK"))
        self.assertEqual(main.step("ESTABLISHED", "FIN"), ("CLOSE_WAIT", "ACK"))
        self.assertEqual(main.step("FIN_WAIT1", "FIN+ACK"), ("TIME_WAIT", "ACK"))
        self.assertEqual(main.step("TIME_WAIT", "FIN"), ("TIME_WAIT", "ACK"))
        self.assertEqual(main.step("CLOSE_WAIT", "RST"), ("CLOSED", ""))
        self.assertEqual(main.step("ESTABLISHED", "abort"), ("CLOSED", "RST"))
        self.assertEqual(main.step("SYN_RECV", "RST"), ("LISTEN", ""))

    def test_events_that_make_no_sense_raise(self):
        with self.assertRaises(main.InputError):
            main.step("CLOSE_WAIT", "timeout")
        with self.assertRaises(main.InputError):
            main.step("LISTEN", "ACK")
        with self.assertRaises(main.InputError):
            main.simulate("half-duplex")

    def test_close_leaves_time_wait_on_the_side_that_closed_first(self):
        rows = main.simulate("close")
        self.assertIn(("server", "TIME_WAIT", "CLOSED"), [(r.side, r.client, r.server) for r in rows])
        self.assertEqual((rows[-1].client, rows[-1].server, rows[-1].event), ("CLOSED", "CLOSED", "timeout"))
        self.assertAlmostEqual(rows[-1].t - rows[-2].t, 60.0, places=3)
        self.assertEqual([r.sends for r in rows if r.sends != "-"], ["SYN", "SYN+ACK", "ACK", "FIN", "ACK", "FIN", "ACK"])

    def test_close_wait_leak_never_ends_on_the_server(self):
        rows = main.simulate("close-wait-leak")
        self.assertEqual((rows[-1].client, rows[-1].server), ("CLOSED", "CLOSE_WAIT"))
        self.assertIn("tcp_fin_timeout", rows[-1].note)

    def test_simultaneous_close_passes_through_closing_on_both_sides(self):
        rows = main.simulate("simultaneous-close")
        self.assertIn(("CLOSING", "CLOSING"), [(r.client, r.server) for r in rows])
        self.assertIn(("TIME_WAIT", "TIME_WAIT"), [(r.client, r.server) for r in rows])
        self.assertEqual((rows[-1].client, rows[-1].server), ("CLOSED", "CLOSED"))

    def test_refused_and_reset_name_the_errno_the_application_sees(self):
        refused = main.simulate("refused")
        self.assertEqual([r.sends for r in refused], ["SYN", "RST", "-"])
        self.assertIn("ECONNREFUSED", refused[-1].note)
        reset = main.simulate("reset")
        self.assertIn("ECONNRESET", reset[-1].note)
        self.assertNotIn("TIME_WAIT", [r.client for r in reset] + [r.server for r in reset])

    def test_blackhole_retransmits_syn_on_the_doubling_schedule(self):
        rows = main.simulate("blackhole")
        self.assertEqual([r.t for r in rows if r.event == "rto"], [1.0, 3.0, 7.0, 15.0, 31.0, 63.0])
        self.assertEqual((rows[-1].t, rows[-1].client), (127.0, "CLOSED"))
        self.assertIn("ETIMEDOUT", rows[-1].note)
        self.assertEqual(main.simulate("blackhole", syn_retries=2)[-1].t, 7.0)


class ServerThread:
    def __init__(self, target, backlog=8, **kwargs):
        self.listener = main.listen("127.0.0.1", 0, backlog)
        self.port = self.listener.getsockname()[1]
        self.stop = threading.Event()
        self.held = []
        self.thread = threading.Thread(target=target, args=(self.listener, self.stop, self.held), kwargs=kwargs, daemon=True)
        self.thread.start()

    def close(self):
        self.stop.set()
        self.listener.close()
        for conn in self.held:
            conn.close()
        self.thread.join(2)


class ClientTests(unittest.TestCase):
    def serve(self, target, **kwargs):
        server = ServerThread(target, **kwargs)
        self.addCleanup(server.close)
        return server

    def test_refused_is_immediate_and_is_not_a_timeout(self):
        probe_socket = socket.socket()
        probe_socket.bind(("127.0.0.1", 0))
        port = probe_socket.getsockname()[1]
        probe_socket.close()
        result = main.probe("127.0.0.1", port, main.Timeouts(2, 2, 5))
        self.assertEqual((result.outcome, result.connected), ("refused", False))
        self.assertLess(result.elapsed, 1.0)

    def test_connect_succeeds_on_a_listener_that_never_accepts_and_only_the_read_timeout_ends_it(self):
        server = self.serve(main.serve_never_accept)
        result = main.probe("127.0.0.1", server.port, main.Timeouts(connect=1, read=0.3), payload=b"GET / HTTP/1.0\r\n\r\n")
        self.assertEqual((result.outcome, result.connected), ("read-timeout", True))
        self.assertGreaterEqual(result.elapsed, 0.25)
        self.assertLess(result.elapsed, 2.0)

    def test_a_trickle_defeats_the_read_timeout_but_not_the_total_timeout(self):
        server = self.serve(main.serve_trickle, interval=0.05, duration=0.6)
        finished = main.probe("127.0.0.1", server.port, main.Timeouts(connect=1, read=0.3))
        self.assertEqual(finished.outcome, "ok")
        self.assertGreater(finished.received, 4)
        self.assertGreaterEqual(finished.elapsed, 0.5)
        slow = self.serve(main.serve_trickle, interval=0.05, duration=5)
        bounded = main.probe("127.0.0.1", slow.port, main.Timeouts(connect=1, read=0.3, total=0.5))
        self.assertEqual(bounded.outcome, "total-timeout")
        self.assertGreater(bounded.received, 0)
        self.assertLess(bounded.elapsed, 1.5)

    def test_connect_timeout_and_the_total_budget_limit_the_connect_wait(self):
        waits = []

        def silent(address, timeout):
            waits.append(timeout)
            raise socket.timeout("timed out")

        result = main.probe("192.0.2.1", 9, main.Timeouts(connect=2, read=5), connector=silent)
        self.assertEqual((result.outcome, waits), ("connect-timeout", [2]))
        result = main.probe("192.0.2.1", 9, main.Timeouts(connect=2, read=5, total=0.5), connector=silent)
        self.assertEqual(result.outcome, "total-timeout")
        self.assertLessEqual(waits[-1], 0.5)
        main.probe("192.0.2.1", 9, main.Timeouts(), connector=silent)
        self.assertIsNone(waits[-1])

        def unreachable(address, timeout):
            raise OSError(main.errno.EHOSTUNREACH, "No route to host")

        self.assertEqual(main.probe("192.0.2.1", 9, main.Timeouts(1, 1), connector=unreachable).outcome, "unreachable")

    def test_keepalive_options_are_set_on_the_socket(self):
        with socket.socket() as sock:
            applied = main.set_keepalive(sock, main.Keepalive(30, 10, 3))
            self.assertEqual(sock.getsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE) != 0, True)
            for name, option, value in main.keepalive_options(main.Keepalive(30, 10, 3)):
                self.assertEqual(sock.getsockopt(socket.IPPROTO_TCP, option), value, name)
                self.assertEqual(applied[name], value)
        self.assertEqual(applied["SO_KEEPALIVE"], 1)
        with self.assertRaises(main.InputError):
            main.parse_keepalive("30,10")

    def test_without_timeouts_the_client_waits_until_the_server_closes(self):
        server = self.serve(main.serve_leak)
        outcome = {}
        thread = threading.Thread(target=lambda: outcome.update(result=main.probe("127.0.0.1", server.port, main.Timeouts())), daemon=True)
        thread.start()
        for _ in range(100):
            if server.held:
                break
            time.sleep(0.02)
        thread.join(0.5)
        self.assertTrue(thread.is_alive(), "an untimed client should still be blocked in recv()")
        server.held[0].close()
        thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertEqual(outcome["result"].outcome, "ok")
        self.assertGreaterEqual(outcome["result"].elapsed, 0.5)

    def test_lab_clients_count_what_connected(self):
        server = self.serve(main.serve_leak)
        held, tally = main.open_clients("127.0.0.1", server.port, 3, 1.0)
        for conn in held:
            conn.close()
        self.assertEqual(tally, {"connected": 3, "connect-timeout": 0, "refused": 0, "error": 0})


class CliTests(unittest.TestCase):
    def test_diagnose_prints_text_and_json_and_exits_one_on_warnings(self):
        code, out, _ = run(["diagnose", "--ss", str(FIXTURES / "ss-tanpio.txt"), "--port-range", "40000-40009"])
        self.assertEqual(code, 1)
        self.assertTrue(out.startswith("tcpwatch: 35 sockets: LISTEN 6, SYN_SENT 1"))
        self.assertIn("critical  accept-queue", out)
        code, out, _ = run(["diagnose", "--proc", str(FIXTURES / "proc-net-tcp.txt"), "--proc", str(FIXTURES / "proc-net-tcp6.txt"), "--netstat", str(FIXTURES / "proc-net-netstat.txt"), "--json"])
        data = json.loads(out)
        self.assertEqual(data["states"]["CLOSE_WAIT"], 6)
        self.assertEqual(data["counters"]["TcpExtListenOverflows"], 1873)
        self.assertEqual(len(data["sockets"]), 35)
        self.assertEqual(data["port_range"], [32768, 60999])
        code, _, _ = run(["diagnose", "--ss", str(FIXTURES / "ss-tanpio.txt"), "--json", "--close-wait-threshold", "100"])
        self.assertEqual(code, 1)

    def test_simulate_lists_scenarios_and_prints_json(self):
        code, out, _ = run(["simulate", "--list"])
        self.assertEqual(code, 0)
        self.assertIn("close-wait-leak", out)
        code, out, _ = run(["simulate", "blackhole", "--json"])
        self.assertEqual(json.loads(out)["rows"][-1]["t"], 127.0)

    def test_errors_exit_two_with_the_reason(self):
        cases = [
            (["diagnose"], "either --proc"),
            (["diagnose", "--ss", "/nonexistent/ss.txt"], "cannot read"),
            (["diagnose", "--ss", str(FIXTURES / "ss-tanpio.txt"), "--port-range", "60999-32768"], "low <= high"),
            (["diagnose", "--ss", str(FIXTURES / "ss-tanpio.txt"), "--backlog", "8082"], "PORT=N"),
            (["diagnose", "--proc", str(FIXTURES / "ss-tanpio.txt")], "not a /proc/net/tcp row"),
            (["simulate", "nope"], "unknown scenario"),
            (["probe", "127.0.0.1", "9", "--keepalive", "x"], "IDLE,INTERVAL,COUNT"),
        ]
        for argv, needle in cases:
            code, _, err = run(argv)
            self.assertEqual(code, 2, argv)
            self.assertIn(needle, err, argv)


if __name__ == "__main__":
    unittest.main()
