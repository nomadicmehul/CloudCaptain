import http.client
import io
import json
import os
import shutil
import socket
import sys
import tempfile
import threading
import unittest
from contextlib import redirect_stderr, redirect_stdout
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import main  # noqa: E402

FIXTURES = os.path.join(os.path.dirname(__file__), "..", "fixtures")
HELLO = b"hello from the allowed host\n"


def fixture(name):
    return os.path.join(FIXTURES, name)


def load_json(name):
    with open(fixture(name), encoding="utf-8") as handle:
        return json.load(handle)


class HelloHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        body = HELLO if self.path != "/path" else self.path.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        echo = b"echo:" + body
        self.send_response(201)
        self.send_header("Content-Length", str(len(echo)))
        self.end_headers()
        self.wfile.write(echo)

    def log_message(self, fmt, *args):
        return None


def closed_port():
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()
    return port


def read_until(sock, marker):
    data = b""
    while marker not in data:
        chunk = sock.recv(4096)
        if not chunk:
            break
        data += chunk
    return data


def read_all(sock):
    data = b""
    while True:
        chunk = sock.recv(4096)
        if not chunk:
            return data
        data += chunk


class HostRuleTests(unittest.TestCase):
    def test_rules_match_exact_hosts_wildcards_ports_and_everything(self):
        exact = main.HostRule.parse("API.Anthropic.com.")
        self.assertEqual((exact.pattern, exact.port), ("api.anthropic.com", None))
        self.assertTrue(exact.matches("api.anthropic.com", 443))
        self.assertFalse(exact.matches("evil.api.anthropic.com", 443))
        wildcard = main.HostRule.parse("*.svc.cluster.local:443")
        self.assertTrue(wildcard.matches("kubernetes.default.svc.cluster.local", 443))
        self.assertFalse(wildcard.matches("kubernetes.default.svc.cluster.local", 80))
        self.assertFalse(wildcard.matches("svc.cluster.local", 443))
        self.assertTrue(main.HostRule.parse("*").matches("anything.example", 9))
        with self.assertRaises(ValueError):
            main.HostRule.parse("host:https")
        with self.assertRaises(ValueError):
            main.HostRule.parse("  ")

    def test_policy_denies_by_default_with_a_reason(self):
        policy = main.EgressPolicy(["api.anthropic.com", "127.0.0.1:8000"])
        self.assertEqual(policy.decide("api.anthropic.com", 443), main.Decision(True, "matches rule api.anthropic.com"))
        self.assertEqual(policy.decide("127.0.0.1", 8000).allowed, True)
        denied = policy.decide("127.0.0.1", 8001)
        self.assertFalse(denied.allowed)
        self.assertEqual(denied.reason, "127.0.0.1:8001 is not in the allow-list")
        self.assertFalse(main.EgressPolicy([]).decide("api.anthropic.com", 443).allowed)


class PolicyRenderTests(unittest.TestCase):
    def test_load_policy_validates_and_fills_defaults(self):
        policy = main.load_policy({"name": "box", "image": "alpine:3.22", "allowed_hosts": ["example.com"]})
        self.assertEqual((policy.tmpfs, policy.pids, policy.memory, policy.cpus, policy.user), (["/tmp"], 64, "256m", "1", "1000"))
        self.assertEqual((policy.network, policy.egress_network, policy.proxy_container, policy.agent_container), ("box-net", "box-egress", "box-proxy", "box-agent"))
        self.assertEqual(policy.proxy_url, "http://box-proxy:3128")
        with self.assertRaises(ValueError):
            main.load_policy({"name": "Bad Name", "image": "x"})
        with self.assertRaises(ValueError):
            main.load_policy({"name": "box", "image": ""})
        with self.assertRaises(ValueError):
            main.load_policy({"name": "box", "image": "x", "allowed_hosts": "api.anthropic.com"})
        with self.assertRaises(ValueError):
            main.load_policy({"name": "box", "image": "x", "limits": {"memory": "lots"}})

    def test_memory_and_cpu_conversions(self):
        self.assertEqual(main.memory_bytes("256m"), 268435456)
        self.assertEqual(main.memory_bytes("1G"), 1073741824)
        self.assertEqual(main.memory_bytes("512k"), 524288)
        self.assertEqual(main.memory_bytes("1048576"), 1048576)
        self.assertEqual(main.nano_cpus("1"), 1000000000)
        self.assertEqual(main.nano_cpus("0.5"), 500000000)
        with self.assertRaises(ValueError):
            main.nano_cpus("two")

    def test_agent_argv_is_exact(self):
        policy = main.read_policy(fixture("policy-triage.json"))
        argv = main.agent_argv(policy, ["python3", "-c", "print(1)"])
        proxy = "http://triage-proxy:3128"
        self.assertEqual(argv, [
            "docker", "run", "--rm", "--name", "triage-agent", "--network", "triage-net", "--read-only", "--tmpfs", "/tmp",
            "--cap-drop", "ALL", "--security-opt", "no-new-privileges", "--pids-limit", "64", "--memory", "256m", "--cpus", "1", "--user", "1000",
            "-e", f"HTTPS_PROXY={proxy}", "-e", f"HTTP_PROXY={proxy}", "-e", f"https_proxy={proxy}", "-e", f"http_proxy={proxy}",
            "-e", "NO_PROXY=localhost,127.0.0.1", "-e", "no_proxy=localhost,127.0.0.1",
            "-e", "ANTHROPIC_BASE_URL=https://api.anthropic.com", "-e", "KUBECONFIG=/home/agent/.kube/config",
            "-v", "/home/me/.kube/agent-config:/home/agent/.kube/config:ro",
            "python:3.12-alpine", "python3", "-c", "print(1)",
        ])
        self.assertEqual(main.agent_argv(policy, ["sleep", "60"], detach=True)[:4], ["docker", "run", "--rm", "-d"])

    def test_network_proxy_and_connect_argv(self):
        policy = main.read_policy(fixture("policy-triage.json"))
        self.assertEqual(main.network_argv(policy), [["docker", "network", "create", "--internal", "triage-net"], ["docker", "network", "create", "triage-egress"]])
        self.assertEqual(main.connect_argv(policy), ["docker", "network", "connect", "triage-net", "triage-proxy"])
        proxy = main.proxy_argv(policy, main_py="/srv/agentbox/main.py")
        self.assertEqual(proxy[:8], ["docker", "run", "-d", "--rm", "--name", "triage-proxy", "--network", "triage-egress"])
        self.assertIn("--read-only", proxy)
        self.assertEqual(proxy[proxy.index("-v") + 1], "/srv/agentbox/main.py:/main.py:ro")
        self.assertEqual(proxy[-8:], ["--listen", "0.0.0.0:3128", "--audit", "/tmp/audit.json", "--allow", "api.anthropic.com", "--allow", "cc-lab-control-plane:6443"])
        text = main.render_text(main.render(policy, ["sh"], main_py="/srv/agentbox/main.py"))
        self.assertEqual(len(text.splitlines()), 5)
        self.assertIn("docker network create --internal triage-net", text)
        self.assertIn("--security-opt no-new-privileges", text)


class DriftTests(unittest.TestCase):
    def setUp(self):
        self.policy = main.read_policy(fixture("policy-triage.json"))

    def test_hardened_container_has_no_drift(self):
        findings = main.check_container(self.policy, load_json("inspect-hardened.json")[0])
        self.assertEqual(findings, [])
        self.assertIn("0 drift finding(s)", main.render_findings(self.policy, findings))

    def test_default_container_lists_every_gap_with_a_fix(self):
        findings = main.check_container(self.policy, load_json("inspect-default.json")[0])
        by_check = {}
        for finding in findings:
            by_check.setdefault(finding.check, []).append(finding)
        self.assertEqual(sorted(by_check), sorted([
            "read-only rootfs", "capabilities dropped", "no capabilities added", "no-new-privileges", "pids limit", "memory limit", "cpu limit",
            "network", "user", "proxy environment", "tmpfs", "sensitive mount", "missing mount",
        ]))
        self.assertEqual(by_check["read-only rootfs"][0].fix, "--read-only")
        self.assertEqual(by_check["capabilities dropped"][0].fix, "--cap-drop ALL")
        self.assertEqual(by_check["network"][0].actual, "bridge / attached ['bridge']")
        self.assertEqual(by_check["user"][0].actual, "root (empty)")
        sensitive = sorted(finding.actual for finding in by_check["sensitive mount"])
        self.assertEqual(sensitive, [
            "/home/me/.aws -> /root/.aws (credential directory .aws)",
            "/home/me/.kube -> /home/agent/.kube (credential directory .kube)",
            "/var/run/docker.sock -> /var/run/docker.sock (host path under /var/run/docker.sock)",
        ])
        self.assertEqual(by_check["missing mount"][0].fix, "-v /home/me/.kube/agent-config:/home/agent/.kube/config:ro")
        text = main.render_findings(self.policy, findings)
        self.assertIn("DRIFT read-only rootfs", text)
        self.assertIn("fix: remove -v /var/run/docker.sock:/var/run/docker.sock", text)

    def test_extra_network_rw_mount_and_unconfined_seccomp_are_drift(self):
        inspect = load_json("inspect-hardened.json")[0]
        inspect["NetworkSettings"]["Networks"]["bridge"] = {"IPAddress": "172.17.0.9"}
        inspect["Mounts"][0]["RW"] = True
        inspect["HostConfig"]["SecurityOpt"] = ["no-new-privileges", "seccomp=unconfined"]
        checks = [finding.check for finding in main.check_container(self.policy, inspect)]
        self.assertEqual(checks, ["seccomp confined", "network", "mount is writable"])
        inspect["Mounts"][0]["RW"] = False
        inspect["Mounts"][0]["Source"] = "/home/me/.kube/agent-config"
        inspect["Mounts"][0]["Destination"] = "/home/agent/.kube/config"
        inspect["HostConfig"]["SecurityOpt"] = ["no-new-privileges"]
        inspect["NetworkSettings"]["Networks"].pop("bridge")
        self.assertEqual(main.check_container(self.policy, inspect), [])

    def test_kubeconfig_from_another_source_is_drift(self):
        inspect = load_json("inspect-hardened.json")[0]
        inspect["Mounts"][0]["Source"] = "/home/me/.kube/config"
        findings = main.check_container(self.policy, inspect)
        self.assertEqual([finding.check for finding in findings], ["mount source"])
        self.assertEqual(findings[0].actual, "/home/me/.kube/config -> /home/agent/.kube/config")
        self.assertEqual(findings[0].fix, "-v /home/me/.kube/agent-config:/home/agent/.kube/config:ro")

    def test_sensitive_sources(self):
        self.assertIsNone(main.is_sensitive_source(""))
        self.assertEqual(main.is_sensitive_source("/"), "the host root filesystem")
        self.assertEqual(main.is_sensitive_source("/var/run/docker.sock"), "host path under /var/run/docker.sock")
        self.assertEqual(main.is_sensitive_source("/proc/sys"), "host path under /proc")
        self.assertEqual(main.is_sensitive_source("/home/me/.config/gcloud"), "credential directory .config/gcloud")
        self.assertEqual(main.is_sensitive_source("/home/me/.ssh/id_ed25519"), "credential directory .ssh")
        self.assertEqual(main.is_sensitive_source("/home/me/.kube/agent-config"), "credential directory .kube")
        self.assertIsNone(main.is_sensitive_source("/srv/agent/code"))
        self.assertIsNone(main.is_sensitive_source("/home/me/incident-42/policy.json"))


class ProxyTests(unittest.TestCase):
    def setUp(self):
        self.target = ThreadingHTTPServer(("127.0.0.1", 0), HelloHandler)
        self.target.daemon_threads = True
        threading.Thread(target=self.target.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
        self.addCleanup(self.target.server_close)
        self.addCleanup(self.target.shutdown)
        self.target_port = self.target.server_address[1]
        self.denied_port = closed_port()
        self.work = tempfile.mkdtemp(prefix="agentbox-")
        self.addCleanup(shutil.rmtree, self.work)
        self.lines = []

    def proxy_for(self, rules):
        self.audit = main.Audit(rules, os.path.join(self.work, "audit.json"), self.lines.append)
        server = main.start_proxy(main.EgressPolicy(rules), self.audit)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return server.server_address[1]

    def via_proxy(self, proxy_port, method, url, body=None):
        conn = http.client.HTTPConnection("127.0.0.1", proxy_port, timeout=5)
        host = url.split("/")[2]
        conn.request(method, url, body=body, headers={"Host": host})
        response = conn.getresponse()
        data = response.read()
        conn.close()
        return response.status, dict(response.getheaders()), data

    def test_plain_http_to_the_allowed_host_is_forwarded(self):
        proxy_port = self.proxy_for([f"127.0.0.1:{self.target_port}"])
        status, headers, data = self.via_proxy(proxy_port, "GET", f"http://127.0.0.1:{self.target_port}/")
        self.assertEqual((status, data), (200, HELLO))
        self.assertEqual(headers.get("Content-Type"), "text/plain")
        status, _, data = self.via_proxy(proxy_port, "GET", f"http://127.0.0.1:{self.target_port}/path")
        self.assertEqual((status, data), (200, b"/path"))
        status, _, data = self.via_proxy(proxy_port, "POST", f"http://127.0.0.1:{self.target_port}/submit", body=b"payload")
        self.assertEqual((status, data), (201, b"echo:payload"))

    def test_plain_http_to_an_unlisted_port_is_denied_with_403(self):
        proxy_port = self.proxy_for([f"127.0.0.1:{self.target_port}"])
        status, headers, data = self.via_proxy(proxy_port, "GET", f"http://127.0.0.1:{self.denied_port}/secret")
        self.assertEqual(status, 403)
        self.assertEqual(headers.get("X-Egress-Decision"), "denied")
        body = json.loads(data)
        self.assertEqual((body["host"], body["port"]), ("127.0.0.1", self.denied_port))
        self.assertEqual(body["reason"], f"127.0.0.1:{self.denied_port} is not in the allow-list")
        self.assertEqual(len(self.audit.requests), 1)
        self.assertFalse(self.audit.requests[0]["allowed"])

    def test_connect_tunnel_relays_bytes_to_the_allowed_host(self):
        proxy_port = self.proxy_for([f"127.0.0.1:{self.target_port}"])
        with socket.create_connection(("127.0.0.1", proxy_port), timeout=5) as sock:
            sock.sendall(f"CONNECT 127.0.0.1:{self.target_port} HTTP/1.1\r\nHost: 127.0.0.1:{self.target_port}\r\n\r\n".encode())
            head = read_until(sock, b"\r\n\r\n")
            self.assertTrue(head.startswith(b"HTTP/1.1 200 Connection established"), head)
            sock.sendall(b"GET /tunnel HTTP/1.1\r\nHost: 127.0.0.1\r\nConnection: close\r\n\r\n")
            data = read_all(sock)
        self.assertTrue(data.startswith(b"HTTP/1.0 200"), data)
        self.assertTrue(data.endswith(HELLO), data)
        entry = self.audit.requests[0]
        self.assertEqual((entry["method"], entry["allowed"], entry["status"]), ("CONNECT", True, 200))
        self.assertEqual(entry["bytes_out"], len(b"GET /tunnel HTTP/1.1\r\nHost: 127.0.0.1\r\nConnection: close\r\n\r\n"))
        self.assertEqual(entry["bytes_in"], len(data))

    def test_connect_to_a_denied_host_is_refused_before_any_connection(self):
        proxy_port = self.proxy_for([f"127.0.0.1:{self.target_port}"])
        with socket.create_connection(("127.0.0.1", proxy_port), timeout=5) as sock:
            sock.sendall(f"CONNECT 127.0.0.1:{self.denied_port} HTTP/1.1\r\nHost: 127.0.0.1:{self.denied_port}\r\n\r\n".encode())
            data = read_all(sock)
        self.assertTrue(data.startswith(b"HTTP/1.1 403 Egress denied"), data)
        self.assertIn(b"not in the allow-list", data)
        with socket.create_connection(("127.0.0.1", proxy_port), timeout=5) as sock:
            sock.sendall(b"CONNECT evil.example.com:443 HTTP/1.1\r\nHost: evil.example.com:443\r\n\r\n")
            self.assertTrue(read_all(sock).startswith(b"HTTP/1.1 403"))
        self.assertEqual([e["port"] for e in self.audit.requests], [self.denied_port, 443])
        self.assertEqual(self.audit.requests[1]["host"], "evil.example.com")

    def test_connect_to_an_allowed_but_closed_port_is_a_502(self):
        proxy_port = self.proxy_for([f"127.0.0.1:{self.denied_port}"])
        with socket.create_connection(("127.0.0.1", proxy_port), timeout=5) as sock:
            sock.sendall(f"CONNECT 127.0.0.1:{self.denied_port} HTTP/1.1\r\nHost: x\r\n\r\n".encode())
            data = read_all(sock)
        self.assertTrue(data.startswith(b"HTTP/1.1 502 Bad gateway"), data)
        self.assertIn(b"upstream unreachable", data)
        self.assertEqual(self.audit.requests[0]["status"], 502)

    def test_relative_urls_are_rejected_as_not_proxy_requests(self):
        proxy_port = self.proxy_for(["*"])
        conn = http.client.HTTPConnection("127.0.0.1", proxy_port, timeout=5)
        conn.request("GET", "/not-absolute")
        response = conn.getresponse()
        self.assertEqual(response.status, 400)
        self.assertIn(b"absolute http:// URL or CONNECT", response.read())
        conn.close()

    def test_audit_counts_per_host_and_writes_the_json_file(self):
        proxy_port = self.proxy_for([f"127.0.0.1:{self.target_port}"])
        self.via_proxy(proxy_port, "GET", f"http://127.0.0.1:{self.target_port}/")
        self.via_proxy(proxy_port, "GET", f"http://127.0.0.1:{self.target_port}/")
        self.via_proxy(proxy_port, "GET", f"http://127.0.0.1:{self.denied_port}/")
        snapshot = self.audit.snapshot()
        self.assertEqual(snapshot["allowed_hosts"], [f"127.0.0.1:{self.target_port}"])
        self.assertEqual(snapshot["summary"]["requests"], 3)
        self.assertEqual((snapshot["summary"]["allowed"], snapshot["summary"]["denied"]), (2, 1))
        allowed = snapshot["hosts"][f"127.0.0.1:{self.target_port}"]
        denied = snapshot["hosts"][f"127.0.0.1:{self.denied_port}"]
        self.assertEqual((allowed["allowed"], allowed["denied"]), (2, 0))
        self.assertEqual((denied["allowed"], denied["denied"]), (0, 1))
        self.assertGreater(allowed["bytes_out"], 0)
        self.assertEqual(allowed["bytes_in"], 2 * (snapshot["requests"][0]["bytes_in"]))
        with open(os.path.join(self.work, "audit.json"), encoding="utf-8") as handle:
            on_disk = json.load(handle)
        self.assertEqual(on_disk["summary"], snapshot["summary"])
        self.assertEqual(sorted(snapshot["requests"][0]), ["allowed", "at", "bytes_in", "bytes_out", "duration_ms", "host", "method", "port", "reason", "status"])
        self.assertEqual(len(self.lines), 3)
        self.assertTrue(self.lines[0].startswith("GET     127.0.0.1:"))
        self.assertIn("ALLOW 200", self.lines[0])
        self.assertIn("DENY  403", self.lines[2])


class CliTests(unittest.TestCase):
    def run_main(self, argv):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = main.main(argv)
        return code, out.getvalue(), err.getvalue()

    def test_render_prints_commands_and_json(self):
        code, out, _ = self.run_main(["render", "--policy", fixture("policy-triage.json"), "--", "python3", "-c", "print(1)"])
        self.assertEqual(code, 0)
        self.assertEqual(len(out.strip().splitlines()), 5)
        self.assertIn("docker network connect triage-net triage-proxy", out)
        self.assertTrue(out.strip().endswith("python:3.12-alpine python3 -c 'print(1)'"))
        code, out, _ = self.run_main(["render", "--policy", fixture("policy-triage.json"), "--json", "--detach", "sleep", "60"])
        plan = json.loads(out)
        self.assertEqual(sorted(plan), ["agent", "connect", "network", "proxy"])
        self.assertEqual(plan["agent"][-3:], ["python:3.12-alpine", "sleep", "60"])
        self.assertIn("-d", plan["agent"])

    def test_check_exit_codes_for_fixture_container_and_errors(self):
        code, out, _ = self.run_main(["check", "--policy", fixture("policy-triage.json"), "--inspect", fixture("inspect-hardened.json")])
        self.assertEqual(code, 0)
        self.assertIn("no drift", out)
        code, out, _ = self.run_main(["check", "--policy", fixture("policy-triage.json"), "--inspect", fixture("inspect-default.json"), "--json"])
        self.assertEqual(code, 1)
        report = json.loads(out)
        self.assertEqual(report["container"], "triage-agent")
        self.assertGreaterEqual(report["drift"], 10)
        code, _, err = self.run_main(["check", "--policy", fixture("policy-triage.json"), "--inspect", fixture("missing.json")])
        self.assertEqual(code, 2)
        self.assertIn("agentbox:", err)

    def test_check_against_a_live_container_uses_the_runner(self):
        calls = []

        def runner(argv):
            calls.append(list(argv))
            with open(fixture("inspect-hardened.json"), encoding="utf-8") as handle:
                return 0, handle.read(), ""

        policy = fixture("policy-triage.json")
        args = main.argparse.Namespace(policy=policy, inspect=None, container="triage-agent", json=False)
        with redirect_stdout(io.StringIO()):
            self.assertEqual(main.run_check(args, runner), 0)
        self.assertEqual(calls, [["docker", "inspect", "triage-agent"]])
        with self.assertRaises(RuntimeError):
            main.inspect_container("gone", lambda argv: (1, "", "Error: No such object: gone"))

    def test_parse_listen(self):
        self.assertEqual(main.parse_listen("127.0.0.1:3128"), ("127.0.0.1", 3128))
        self.assertEqual(main.parse_listen(":8080"), ("0.0.0.0", 8080))
        with self.assertRaises(ValueError):
            main.parse_listen("3128")


if __name__ == "__main__":
    unittest.main()
