import io
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import main  # noqa: E402


class FakeKubectl:
    def __init__(self, stdout="NAME READY STATUS\nweb-1 1/1 Running", code=0, stderr=""):
        self.calls = []
        self.stdout = stdout
        self.code = code
        self.stderr = stderr

    def __call__(self, argv):
        self.calls.append(list(argv))
        return self.code, self.stdout, self.stderr


def rpc(method, params=None, msg_id=1):
    message = {"jsonrpc": "2.0", "method": method}
    if msg_id is not None:
        message["id"] = msg_id
    if params is not None:
        message["params"] = params
    return message


class LifecycleTests(unittest.TestCase):
    def test_initialize_negotiates_protocol_and_advertises_tools(self):
        res = main.handle(rpc("initialize", {"protocolVersion": "2025-03-26", "capabilities": {}, "clientInfo": {"name": "t", "version": "1"}}), main.Policy(), FakeKubectl())
        self.assertEqual(res["id"], 1)
        self.assertEqual(res["result"]["protocolVersion"], "2025-03-26")
        self.assertIn("tools", res["result"]["capabilities"])
        self.assertEqual(res["result"]["serverInfo"]["name"], "kubectl-readonly")
        newer = main.handle(rpc("initialize", {"protocolVersion": "2099-01-01"}), main.Policy(), FakeKubectl())
        self.assertEqual(newer["result"]["protocolVersion"], main.SUPPORTED_PROTOCOLS[0])

    def test_notifications_get_no_response_and_unknown_methods_error(self):
        self.assertIsNone(main.handle(rpc("notifications/initialized", msg_id=None), main.Policy(), FakeKubectl()))
        res = main.handle(rpc("resources/list"), main.Policy(), FakeKubectl())
        self.assertEqual(res["error"]["code"], -32601)
        self.assertEqual(main.handle(rpc("ping"), main.Policy(), FakeKubectl())["result"], {})

    def test_tools_list_exposes_schemas(self):
        tools = main.handle(rpc("tools/list"), main.Policy(), FakeKubectl())["result"]["tools"]
        names = {t["name"] for t in tools}
        self.assertEqual(names, {"list_pods", "describe_pod", "pod_logs", "list_events"})
        logs = next(t for t in tools if t["name"] == "pod_logs")
        self.assertEqual(logs["inputSchema"]["required"], ["namespace", "name"])
        self.assertEqual(logs["inputSchema"]["properties"]["tail_lines"]["type"], "integer")
        self.assertFalse(logs["inputSchema"]["additionalProperties"])


class ToolCallTests(unittest.TestCase):
    def test_tools_call_builds_the_kubectl_command_and_returns_text(self):
        kubectl = FakeKubectl()
        res = main.handle(rpc("tools/call", {"name": "list_pods", "arguments": {"namespace": "dev", "selector": "app=web"}}), main.Policy(), kubectl)
        self.assertEqual(kubectl.calls, [["kubectl", "get", "pods", "-n", "dev", "-l", "app=web", "-o", "wide"]])
        self.assertFalse(res["result"]["isError"])
        self.assertIn("web-1 1/1 Running", res["result"]["content"][0]["text"])

    def test_pod_logs_arguments_map_to_flags(self):
        kubectl = FakeKubectl("line1\nline2")
        main.handle(rpc("tools/call", {"name": "pod_logs", "arguments": {"namespace": "dev", "name": "web-1", "tail_lines": 50, "container": "app", "previous": True}}), main.Policy(), kubectl)
        self.assertEqual(kubectl.calls[0], ["kubectl", "logs", "web-1", "-n", "dev", "--tail=50", "-c", "app", "--previous"])

    def test_unknown_tool_is_a_jsonrpc_error_but_bad_arguments_are_a_tool_error(self):
        res = main.handle(rpc("tools/call", {"name": "delete_pod", "arguments": {}}), main.Policy(), FakeKubectl())
        self.assertEqual(res["error"]["code"], -32602)
        kubectl = FakeKubectl()
        res = main.handle(rpc("tools/call", {"name": "describe_pod", "arguments": {"namespace": "dev"}}), main.Policy(), kubectl)
        self.assertTrue(res["result"]["isError"])
        self.assertIn("missing required argument: name", res["result"]["content"][0]["text"])
        self.assertEqual(kubectl.calls, [], "kubectl must not run on invalid input")
        res = main.handle(rpc("tools/call", {"name": "pod_logs", "arguments": {"namespace": "dev", "name": "w", "tail_lines": 99999}}), main.Policy(), kubectl)
        self.assertIn("<= 2000", res["result"]["content"][0]["text"])
        res = main.handle(rpc("tools/call", {"name": "list_pods", "arguments": {"namespace": "dev", "format": "json"}}), main.Policy(), kubectl)
        self.assertIn("unknown argument: format", res["result"]["content"][0]["text"])

    def test_namespace_allow_list_is_enforced(self):
        kubectl = FakeKubectl()
        policy = main.Policy(namespaces=["dev", "staging"])
        res = main.handle(rpc("tools/call", {"name": "list_pods", "arguments": {"namespace": "prod"}}), policy, kubectl)
        self.assertTrue(res["result"]["isError"])
        self.assertIn("not in the allow-list", res["result"]["content"][0]["text"])
        res = main.handle(rpc("tools/call", {"name": "list_pods", "arguments": {}}), policy, kubectl)
        self.assertTrue(res["result"]["isError"], "all-namespaces must be refused when an allow-list is set")
        self.assertEqual(kubectl.calls, [])
        main.handle(rpc("tools/call", {"name": "list_pods", "arguments": {"namespace": "dev"}}), policy, kubectl)
        self.assertEqual(len(kubectl.calls), 1)

    def test_read_only_guard_rejects_write_verbs_and_dangerous_flags(self):
        with self.assertRaises(PermissionError):
            main.ensure_read_only(["delete", "pod", "x"])
        with self.assertRaises(PermissionError):
            main.ensure_read_only(["get", "pods", "--raw", "/api"])
        with self.assertRaises(PermissionError):
            main.ensure_read_only(["get", "pods", "--kubeconfig=/tmp/x"])
        main.ensure_read_only(["get", "pods", "-n", "dev"])

    def test_context_is_pinned_and_kubectl_failures_become_tool_errors(self):
        kubectl = FakeKubectl(code=1, stderr='Error from server (NotFound): pods "x" not found')
        res = main.handle(rpc("tools/call", {"name": "describe_pod", "arguments": {"namespace": "dev", "name": "x"}}), main.Policy(context="kind-cc-lab"), kubectl)
        self.assertEqual(kubectl.calls[0][:3], ["kubectl", "--context", "kind-cc-lab"])
        self.assertTrue(res["result"]["isError"])
        self.assertIn("NotFound", res["result"]["content"][0]["text"])

    def test_large_output_is_truncated_with_a_hint(self):
        kubectl = FakeKubectl("x" * 500)
        res = main.handle(rpc("tools/call", {"name": "list_events", "arguments": {}}), main.Policy(output_limit=100), kubectl)
        text = res["result"]["content"][0]["text"]
        self.assertIn("truncated (400 more characters)", text)


class TransportTests(unittest.TestCase):
    def test_serve_reads_newline_delimited_jsonrpc_and_ignores_blank_lines(self):
        session = "\n".join([
            json.dumps(rpc("initialize", {"protocolVersion": "2025-06-18"}, 1)),
            json.dumps(rpc("notifications/initialized", msg_id=None)),
            "",
            "not json",
            json.dumps(rpc("tools/call", {"name": "list_events", "arguments": {"warnings_only": True}}, 2)),
        ]) + "\n"
        out = io.StringIO()
        main.serve(io.StringIO(session), out, main.Policy(), FakeKubectl("LAST SEEN TYPE REASON"), log=lambda line: None)
        responses = [json.loads(line) for line in out.getvalue().splitlines()]
        self.assertEqual([r.get("id") for r in responses], [1, None, 2])
        self.assertEqual(responses[1]["error"]["code"], -32700)
        self.assertIn("LAST SEEN TYPE REASON", responses[2]["result"]["content"][0]["text"])


if __name__ == "__main__":
    unittest.main()
