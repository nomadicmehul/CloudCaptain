import io
import itertools
import json
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import main  # noqa: E402

NOT_FOUND = 'Error from server (NotFound): {kind} "{name}" not found'


class FakeKubectl:
    def __init__(self):
        self.calls = []
        self.deployments = {("default", "web"): {"replicas": 2, "ready": 2, "generation": 4}, ("payments", "api"): {"replicas": 4, "ready": 4, "generation": 1}}
        self.replicasets = {("default", "web-5d4f"): 3}
        self.pods = {("default", "web-5d4f-abc"): ("ReplicaSet", "web-5d4f", "worker-1"), ("default", "lonely"): (None, None, "worker-2")}
        self.nodes = {"worker-1": False, "worker-2": False, "worker-3": False}
        self.dry_run_error = None
        self.write_error = None

    def __call__(self, argv):
        self.calls.append(list(argv))
        args = list(argv[1:])
        if args[:1] == ["--context"]:
            args = args[2:]
        dry = "--dry-run=server" in args
        args = [a for a in args if a != "--dry-run=server"]
        if args[0] == "get":
            return self.get(args)
        if dry and self.dry_run_error:
            return 1, "", self.dry_run_error
        if not dry and self.write_error:
            return 1, "", self.write_error
        suffix = " (server dry run)" if dry else ""
        if args[0] == "scale":
            key = (args[3], args[1].split("/", 1)[1])
            if not dry:
                self.deployments[key]["replicas"] = int(args[4].split("=", 1)[1])
            return 0, f"deployment.apps/{key[1]} scaled{suffix}", ""
        if args[:2] == ["rollout", "restart"]:
            key = (args[4], args[2].split("/", 1)[1])
            if not dry:
                self.deployments[key]["generation"] += 1
            return 0, f"deployment.apps/{key[1]} restarted{suffix}", ""
        if args[0] in ("cordon", "uncordon"):
            if not dry:
                self.nodes[args[1]] = args[0] == "cordon"
            return 0, f"node/{args[1]} {args[0]}ed{suffix}", ""
        if args[:2] == ["delete", "pod"]:
            key = (args[4], args[2])
            if not dry:
                self.pods.pop(key, None)
            return 0, f'pod "{key[1]}" deleted{suffix}', ""
        return 1, "", f"fake kubectl: unknown command {' '.join(args)}"

    def get(self, args):
        kind = args[1]
        if kind == "deployment":
            key = (args[4], args[2])
            dep = self.deployments.get(key)
            if dep is None:
                return 1, "", NOT_FOUND.format(kind="deployments.apps", name=key[1])
            doc = {"metadata": {"name": key[1], "generation": dep["generation"]}, "spec": {"replicas": dep["replicas"], "strategy": {"type": "RollingUpdate", "rollingUpdate": {"maxUnavailable": "25%"}}}, "status": {"readyReplicas": dep["ready"], "availableReplicas": dep["ready"]}}
            return 0, json.dumps(doc), ""
        if kind == "pod":
            key = (args[4], args[2])
            pod = self.pods.get(key)
            if pod is None:
                return 1, "", NOT_FOUND.format(kind="pods", name=key[1])
            owner_kind, owner_name, node = pod
            refs = [{"kind": owner_kind, "name": owner_name, "controller": True}] if owner_kind else []
            return 0, json.dumps({"metadata": {"name": key[1], "ownerReferences": refs}, "spec": {"nodeName": node}, "status": {"phase": "Running"}}), ""
        if kind == "replicaset":
            return 0, json.dumps({"spec": {"replicas": self.replicasets[(args[4], args[2])]}}), ""
        if kind == "nodes":
            return 0, json.dumps({"items": [{"metadata": {"name": n}, "spec": {"unschedulable": u}} for n, u in self.nodes.items()]}), ""
        if kind == "pods":
            node = args[args.index("--field-selector") + 1].split("=", 1)[1]
            return 0, json.dumps({"items": [{"metadata": {"name": k[1]}} for k, v in self.pods.items() if v[2] == node]}), ""
        return 1, "", "fake kubectl: unknown get"


class Clock:
    def __init__(self):
        self.t = 1_700_000_000.0

    def __call__(self):
        return self.t


def make(allow_write=True, **overrides):
    policy = main.Policy(allow_write=allow_write, **overrides)
    fake, clock, audit = FakeKubectl(), Clock(), []
    counter = itertools.count(1)
    server = main.WriteServer(policy, fake, clock, audit.append, lambda: f"tok{next(counter):05d}")
    return server, fake, clock, audit


def rpc(method, params=None, msg_id=1):
    message = {"jsonrpc": "2.0", "method": method}
    if msg_id is not None:
        message["id"] = msg_id
    if params is not None:
        message["params"] = params
    return message


def call(server, tool, **arguments):
    return main.handle(rpc("tools/call", {"name": tool, "arguments": arguments}), server)


def text(response):
    return response["result"]["content"][0]["text"]


def confirm_of(response):
    return response["result"]["structuredContent"]["plan"]["confirm"]


def real_writes(fake):
    out = []
    for argv in fake.calls:
        args = argv[1:]
        if args[:1] == ["--context"]:
            args = args[2:]
        if args[0] != "get" and "--dry-run=server" not in args:
            out.append(args)
    return out


class VisibilityTests(unittest.TestCase):
    def test_tools_list_hides_write_tools_without_allow_write(self):
        server, _, _, _ = make(allow_write=False)
        tools = main.handle(rpc("tools/list"), server)["result"]["tools"]
        self.assertEqual([t["name"] for t in tools], ["plan_scale", "plan_rollout_restart", "plan_cordon", "plan_uncordon", "plan_delete_pod"])
        self.assertTrue(all(t["annotations"]["readOnlyHint"] for t in tools))
        server, _, _, _ = make(allow_write=True)
        tools = {t["name"]: t for t in main.handle(rpc("tools/list"), server)["result"]["tools"]}
        self.assertEqual(len(tools), 10)
        self.assertEqual(tools["scale"]["inputSchema"]["required"], ["namespace", "deployment", "replicas", "confirm"])
        self.assertEqual(tools["scale"]["inputSchema"]["properties"]["replicas"]["maximum"], 10)
        self.assertFalse(tools["scale"]["inputSchema"]["additionalProperties"])
        self.assertTrue(tools["delete_pod"]["annotations"]["destructiveHint"])
        self.assertFalse(tools["scale"]["annotations"]["destructiveHint"])
        self.assertFalse(tools["scale"]["annotations"]["readOnlyHint"])

    def test_calling_a_hidden_write_tool_is_a_jsonrpc_error_and_runs_nothing(self):
        server, fake, _, _ = make(allow_write=False)
        res = call(server, "scale", namespace="default", deployment="web", replicas=3, confirm="anything")
        self.assertEqual(res["error"]["code"], -32602)
        self.assertIn("--allow-write", res["error"]["message"])
        self.assertEqual(fake.calls, [])
        init = main.handle(rpc("initialize", {"protocolVersion": "2025-06-18"}), server)["result"]
        self.assertEqual(init["serverInfo"]["name"], "kubectl-write")
        self.assertIn("disabled", init["instructions"])


class PlanTests(unittest.TestCase):
    def test_plan_scale_reads_state_runs_a_server_dry_run_and_issues_a_phrase(self):
        server, fake, _, audit = make(context="kind-cc-lab")
        res = call(server, "plan_scale", namespace="default", deployment="web", replicas=3)
        self.assertFalse(res["result"]["isError"], text(res))
        self.assertEqual(fake.calls, [
            ["kubectl", "--context", "kind-cc-lab", "get", "deployment", "web", "-n", "default", "-o", "json"],
            ["kubectl", "--context", "kind-cc-lab", "scale", "deployment/web", "-n", "default", "--replicas=3", "--dry-run=server"],
        ])
        plan = res["result"]["structuredContent"]["plan"]
        self.assertEqual(plan["confirm"], "scale deployment default/web from 2 to 3 [tok00001]")
        self.assertEqual(plan["blast"], {"pods_affected": 1, "percent": 50.0, "note": "adds 1 replica(s)"})
        self.assertEqual(plan["before"]["replicas"], 2)
        self.assertEqual(plan["undo"], ["kubectl", "--context", "kind-cc-lab", "scale", "deployment/web", "-n", "default", "--replicas=2"])
        self.assertEqual(plan["expires_in_seconds"], 120.0)
        body = text(res)
        self.assertIn("dry run (server): deployment.apps/web scaled (server dry run)", body)
        self.assertIn("blast radius: 1 pod(s) affected, 50% of current capacity: adds 1 replica(s)", body)
        self.assertIn("confirm phrase (valid 120s, single use): scale deployment default/web from 2 to 3 [tok00001]", body)
        self.assertEqual([e["event"] for e in audit], ["plan"])
        self.assertEqual(audit[0]["before"], {"replicas": 2, "ready": 2, "available": 2, "generation": 4, "max_unavailable": "25%"})

    def test_protected_and_unlisted_namespaces_are_refused_before_any_kubectl_call(self):
        server, fake, _, audit = make(namespaces=["default", "staging"])
        res = call(server, "plan_scale", namespace="kube-system", deployment="coredns", replicas=0)
        self.assertTrue(res["result"]["isError"])
        self.assertIn("'kube-system' is protected", text(res))
        res = call(server, "plan_rollout_restart", namespace="payments", deployment="api")
        self.assertIn("not in the allow-list (default, staging)", text(res))
        self.assertEqual(fake.calls, [])
        self.assertEqual([(e["event"], e["reason"]) for e in audit], [("refused", "namespace 'kube-system' is protected; no write tool touches it"), ("refused", "namespace 'payments' is not in the allow-list (default, staging)")])

    def test_nodes_need_an_explicit_allow_list(self):
        server, fake, _, _ = make()
        res = call(server, "plan_cordon", node="worker-1")
        self.assertTrue(res["result"]["isError"])
        self.assertIn("start the server with --allow-nodes", text(res))
        self.assertEqual(fake.calls, [])
        server, fake, _, _ = make(nodes=["worker-1"])
        res = call(server, "plan_cordon", node="worker-1")
        self.assertFalse(res["result"]["isError"], text(res))
        plan = res["result"]["structuredContent"]["plan"]
        self.assertEqual(plan["blast"]["pods_affected"], 0)
        self.assertEqual(plan["blast"]["percent"], 33.3)
        self.assertIn("1 pod(s) keep running there", plan["blast"]["note"])
        self.assertEqual(plan["before"], {"unschedulable": False, "schedulable_nodes": 3, "total_nodes": 3, "pods_on_node": 1})
        self.assertEqual(fake.calls[-1], ["kubectl", "cordon", "worker-1", "--dry-run=server"])
        self.assertIn("not in the node allow-list (worker-1)", text(call(server, "plan_cordon", node="worker-2")))

    def test_replicas_above_max_are_rejected_by_the_schema(self):
        server, fake, _, _ = make(max_replicas=5)
        res = call(server, "plan_scale", namespace="default", deployment="web", replicas=50)
        self.assertTrue(res["result"]["isError"])
        self.assertIn("must be <= 5", text(res))
        self.assertEqual(fake.calls, [])
        tools = {t["name"]: t for t in main.handle(rpc("tools/list"), server)["result"]["tools"]}
        self.assertEqual(tools["plan_scale"]["inputSchema"]["properties"]["replicas"]["maximum"], 5)

    def test_failed_dry_run_or_missing_target_issues_no_phrase(self):
        server, fake, _, audit = make()
        res = call(server, "plan_scale", namespace="default", deployment="ghost", replicas=1)
        self.assertTrue(res["result"]["isError"])
        self.assertIn("deployment default/ghost not found", text(res))
        self.assertIn("NotFound", text(res))
        self.assertEqual(len(fake.calls), 1, "no dry run for a missing target")
        fake.dry_run_error = 'Error from server (Forbidden): admission webhook "cap-replicas" denied the request: no more than 10 replicas'
        res = call(server, "plan_scale", namespace="default", deployment="web", replicas=9)
        self.assertTrue(res["result"]["isError"])
        self.assertIn("dry run failed (exit 1)", text(res))
        self.assertIn("cap-replicas", text(res))
        self.assertIn("no confirm phrase was issued", text(res))
        self.assertEqual(server.plans, {})
        self.assertEqual([e["event"] for e in audit], ["refused", "dry_run_failed"])

    def test_argument_validation_mirrors_the_schema(self):
        server, fake, _, _ = make()
        self.assertIn("missing required argument: replicas", text(call(server, "plan_scale", namespace="default", deployment="web")))
        self.assertIn("argument replicas must be a integer", text(call(server, "plan_scale", namespace="default", deployment="web", replicas="3")))
        self.assertIn("unknown argument: force", text(call(server, "plan_delete_pod", namespace="default", name="x", force=True)))
        self.assertIn("missing required argument: confirm", text(call(server, "scale", namespace="default", deployment="web", replicas=3)))
        self.assertIn("arguments must be an object", text(main.handle(rpc("tools/call", {"name": "plan_cordon", "arguments": ["worker-1"]}), server)))
        self.assertEqual(fake.calls, [])


class ExecuteTests(unittest.TestCase):
    def test_execute_with_the_phrase_runs_the_planned_command_once_and_records_before_and_after(self):
        server, fake, _, audit = make()
        phrase = confirm_of(call(server, "plan_scale", namespace="default", deployment="web", replicas=3))
        res = call(server, "scale", namespace="default", deployment="web", replicas=3, confirm=phrase)
        self.assertFalse(res["result"]["isError"], text(res))
        self.assertEqual(real_writes(fake), [["scale", "deployment/web", "-n", "default", "--replicas=3"]])
        execution = res["result"]["structuredContent"]["execution"]
        self.assertEqual((execution["exit_code"], execution["before"]["replicas"], execution["after"]["replicas"]), (0, 2, 3))
        self.assertEqual(execution["undo"], ["kubectl", "scale", "deployment/web", "-n", "default", "--replicas=2"])
        body = text(res)
        self.assertTrue(body.startswith("EXECUTED scale deployment default/web from 2 to 3 [tok00001]"), body)
        self.assertIn("$ kubectl scale deployment/web -n default --replicas=3", body)
        self.assertIn("after:  available=2 generation=4 max_unavailable=25% ready=2 replicas=3", body)
        self.assertEqual([e["event"] for e in audit], ["plan", "execute"])
        self.assertEqual((audit[1]["before"]["replicas"], audit[1]["after"]["replicas"], audit[1]["confirm"]), (2, 3, phrase))
        self.assertEqual(audit[1]["at"], "2023-11-14T22:13:20Z")

    def test_unknown_phrase_and_phrase_of_another_plan_are_refused(self):
        server, fake, _, audit = make()
        phrase = confirm_of(call(server, "plan_scale", namespace="default", deployment="web", replicas=3))
        res = call(server, "scale", namespace="default", deployment="web", replicas=3, confirm="scale deployment default/web from 2 to 3 [guessed1]")
        self.assertTrue(res["result"]["isError"])
        self.assertIn("no plan matches this confirm phrase", text(res))
        res = call(server, "scale", namespace="default", deployment="web", replicas=4, confirm=phrase)
        self.assertTrue(res["result"]["isError"])
        self.assertIn('belongs to scale {"deployment": "web", "namespace": "default", "replicas": 3}', text(res))
        res = call(server, "rollout_restart", namespace="default", deployment="web", confirm=phrase)
        self.assertTrue(res["result"]["isError"])
        self.assertEqual(real_writes(fake), [])
        self.assertEqual([e["reason"] for e in audit if e["event"] == "refused"], ["unknown confirm phrase", "confirm phrase belongs to a different plan", "confirm phrase belongs to a different plan"])
        self.assertIn(phrase, server.plans, "a refused attempt does not burn the real plan")

    def test_expired_phrase_is_refused_and_forgotten(self):
        server, fake, clock, audit = make(ttl_seconds=30)
        phrase = confirm_of(call(server, "plan_delete_pod", namespace="default", name="web-5d4f-abc"))
        clock.t += 29
        self.assertIn(phrase, server.plans)
        clock.t += 2
        res = call(server, "delete_pod", namespace="default", name="web-5d4f-abc", confirm=phrase)
        self.assertTrue(res["result"]["isError"])
        self.assertIn("expired 1s ago (TTL 30s)", text(res))
        self.assertNotIn(phrase, server.plans)
        self.assertIn("no plan matches", text(call(server, "delete_pod", namespace="default", name="web-5d4f-abc", confirm=phrase)))
        self.assertEqual(real_writes(fake), [])
        self.assertEqual(audit[1]["event"], "expired")
        self.assertEqual(audit[1]["expired_seconds_ago"], 1.0)

    def test_replaying_a_phrase_returns_the_first_result_without_running_kubectl(self):
        server, fake, clock, audit = make()
        phrase = confirm_of(call(server, "plan_rollout_restart", namespace="default", deployment="web"))
        first = call(server, "rollout_restart", namespace="default", deployment="web", confirm=phrase)
        calls_after_first = len(fake.calls)
        clock.t += 600
        second = call(server, "rollout_restart", namespace="default", deployment="web", confirm=phrase)
        self.assertEqual(len(fake.calls), calls_after_first)
        self.assertFalse(second["result"]["isError"])
        self.assertTrue(text(second).startswith("REPLAYED: this confirm phrase was already executed at 2023-11-14T22:13:20Z; nothing ran again\n"))
        self.assertIn(text(first), text(second))
        self.assertTrue(second["result"]["structuredContent"]["replayed"])
        self.assertEqual(second["result"]["structuredContent"]["execution"], first["result"]["structuredContent"]["execution"])
        self.assertEqual([e["event"] for e in audit], ["plan", "execute", "replayed"])
        self.assertEqual(len(real_writes(fake)), 1)

    def test_rate_limit_counts_real_writes_per_minute(self):
        server, fake, clock, audit = make(max_per_minute=2)
        phrases = [confirm_of(call(server, "plan_scale", namespace="default", deployment="web", replicas=n)) for n in (3, 4, 5)]
        for n, phrase in zip((3, 4), phrases[:2]):
            self.assertFalse(call(server, "scale", namespace="default", deployment="web", replicas=n, confirm=phrase)["result"]["isError"])
        clock.t += 10
        res = call(server, "scale", namespace="default", deployment="web", replicas=5, confirm=phrases[2])
        self.assertTrue(res["result"]["isError"])
        self.assertIn("rate limit of 2 write(s) per minute reached; retry in 50s", text(res))
        self.assertEqual(audit[-1]["event"], "rate_limited")
        self.assertEqual(audit[-1]["retry_in_seconds"], 50.0)
        self.assertEqual(len(real_writes(fake)), 2)
        replay = call(server, "scale", namespace="default", deployment="web", replicas=3, confirm=phrases[0])
        self.assertIn("REPLAYED", text(replay), "replays are not writes and are not rate limited")
        clock.t += 51
        res = call(server, "scale", namespace="default", deployment="web", replicas=5, confirm=phrases[2])
        self.assertFalse(res["result"]["isError"], text(res))
        self.assertEqual(fake.deployments[("default", "web")]["replicas"], 5)

    def test_failed_write_spends_the_phrase_and_reports_the_error(self):
        server, fake, _, audit = make()
        phrase = confirm_of(call(server, "plan_scale", namespace="default", deployment="web", replicas=3))
        fake.write_error = "Error from server (Conflict): the object has been modified"
        res = call(server, "scale", namespace="default", deployment="web", replicas=3, confirm=phrase)
        self.assertTrue(res["result"]["isError"])
        self.assertTrue(text(res).startswith("FAILED (exit 1) scale deployment default/web"), text(res))
        self.assertIn("the confirm phrase is spent", text(res))
        self.assertEqual(audit[-1]["exit_code"], 1)
        again = call(server, "scale", namespace="default", deployment="web", replicas=3, confirm=phrase)
        self.assertIn("REPLAYED", text(again))
        self.assertEqual(len(real_writes(fake)), 1)


class BlastRadiusTests(unittest.TestCase):
    def test_scale_blast_maths(self):
        self.assertEqual(main.scale_blast(4, 2).as_dict(), {"pods_affected": 2, "percent": 50.0, "note": "removes 2 replica(s)"})
        self.assertEqual(main.scale_blast(2, 3).as_dict(), {"pods_affected": 1, "percent": 50.0, "note": "adds 1 replica(s)"})
        self.assertEqual(main.scale_blast(0, 2).percent, 100.0)
        self.assertEqual(main.scale_blast(3, 30).percent, 100.0)
        self.assertEqual(main.scale_blast(5, 5).as_dict(), {"pods_affected": 0, "percent": 0.0, "note": "no change, the deployment already has that many replicas"})
        zero = main.scale_blast(6, 0)
        self.assertEqual((zero.pods_affected, zero.percent), (6, 100.0))
        self.assertIn("scales to zero", zero.note)
        self.assertEqual(main.restart_blast(4, "25%").describe(), "4 pod(s) affected, 100% of current capacity: every pod is replaced by a rolling update, at most 25% unavailable at a time")

    def test_delete_pod_blast_uses_the_controller_s_replica_count(self):
        server, _, _, _ = make()
        owned = call(server, "plan_delete_pod", namespace="default", name="web-5d4f-abc")["result"]["structuredContent"]["plan"]
        self.assertEqual(owned["blast"], {"pods_affected": 1, "percent": 33.3, "note": "ReplicaSet/web-5d4f recreates it; one of 3 replica(s) restarts"})
        self.assertEqual(owned["before"]["owner"], "ReplicaSet/web-5d4f")
        self.assertIsNone(owned["undo"])
        lonely = call(server, "plan_delete_pod", namespace="default", name="lonely")["result"]["structuredContent"]["plan"]
        self.assertEqual(lonely["blast"]["percent"], 100.0)
        self.assertIn("will not come back", lonely["blast"]["note"])
        self.assertEqual(main.delete_pod_blast(None, "StatefulSet/db").percent, 100.0)

    def test_cordon_blast_and_the_no_change_case(self):
        before = {"present": True, "unschedulable": False, "schedulable_nodes": 4, "total_nodes": 5, "pods_on_node": 7}
        self.assertEqual(main.cordon_blast(before, True).as_dict(), {"pods_affected": 0, "percent": 25.0, "note": "removes one of 4 schedulable node(s); 7 pod(s) keep running there and nothing new lands"})
        self.assertEqual(main.cordon_blast(before, False).note, "already in that state, no change")
        cordoned = dict(before, unschedulable=True, schedulable_nodes=3)
        self.assertEqual(main.cordon_blast(cordoned, False).percent, 25.0)
        self.assertEqual(main.cordon_blast(cordoned, True).percent, 0.0)


class TransportAndAuditTests(unittest.TestCase):
    def test_serve_handles_a_full_two_step_session_over_stdio(self):
        server, fake, _, _ = make()
        session = "\n".join([
            json.dumps(rpc("initialize", {"protocolVersion": "2025-03-26", "capabilities": {}, "clientInfo": {"name": "t", "version": "1"}}, 1)),
            json.dumps(rpc("notifications/initialized", msg_id=None)),
            "not json",
            json.dumps(rpc("tools/list", msg_id=2)),
            json.dumps(rpc("tools/call", {"name": "plan_uncordon", "arguments": {"node": "worker-9"}}, 3)),
            json.dumps(rpc("resources/list", msg_id=4)),
        ]) + "\n"
        out = io.StringIO()
        main.serve(io.StringIO(session), out, server, log=lambda line: None)
        responses = [json.loads(line) for line in out.getvalue().splitlines()]
        self.assertEqual([r.get("id") for r in responses], [1, None, 2, 3, 4])
        self.assertEqual(responses[0]["result"]["protocolVersion"], "2025-03-26")
        self.assertEqual(responses[1]["error"]["code"], -32700)
        self.assertEqual(len(responses[2]["result"]["tools"]), 10)
        self.assertIn("not in the node allow-list", responses[3]["result"]["content"][0]["text"])
        self.assertEqual(responses[4]["error"]["code"], -32601)
        self.assertEqual(fake.calls, [])

    def test_file_audit_appends_one_json_line_per_event(self):
        folder = tempfile.mkdtemp(prefix="kubectl-write-")
        self.addCleanup(shutil.rmtree, folder)
        path = os.path.join(folder, "audit.jsonl")
        fake, clock = FakeKubectl(), Clock()
        server = main.WriteServer(main.Policy(allow_write=True), fake, clock, main.file_audit(path), lambda: "cafe0001")
        phrase = confirm_of(call(server, "plan_scale", namespace="default", deployment="web", replicas=1))
        call(server, "scale", namespace="default", deployment="web", replicas=1, confirm=phrase)
        call(server, "scale", namespace="default", deployment="web", replicas=1, confirm=phrase)
        with open(path, encoding="utf-8") as handle:
            entries = [json.loads(line) for line in handle]
        self.assertEqual([e["event"] for e in entries], ["plan", "execute", "replayed"])
        self.assertEqual(entries[0]["confirm"], "scale deployment default/web from 2 to 1 [cafe0001]")
        self.assertEqual((entries[1]["before"]["replicas"], entries[1]["after"]["replicas"]), (2, 1))
        self.assertEqual(entries[1]["command"], ["kubectl", "scale", "deployment/web", "-n", "default", "--replicas=1"])

    def test_cli_lists_tools_and_rejects_bad_numbers(self):
        from contextlib import redirect_stdout, redirect_stderr
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out):
            self.assertEqual(main.main(["--list-tools", "--audit", os.devnull]), 0)
        self.assertEqual([t["name"] for t in json.loads(out.getvalue())], ["plan_scale", "plan_rollout_restart", "plan_cordon", "plan_uncordon", "plan_delete_pod"])
        with redirect_stderr(err):
            self.assertEqual(main.main(["--ttl", "0", "--audit", os.devnull]), 2)
        self.assertIn("--ttl", err.getvalue())
        self.assertEqual(main.split_list(" a, b ,,c"), ["a", "b", "c"])
        self.assertEqual(main.split_list(None), [])


if __name__ == "__main__":
    unittest.main()
