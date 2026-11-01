"""kubectl_write_mcp: an MCP server whose write tools are two calls apart. plan_<action> reads the current state, runs kubectl with --dry-run=server, measures the blast radius and returns a one-time confirm phrase; <action>(confirm=...) runs the planned command once, inside the TTL, behind namespace and node policy, a rate limit and an append-only audit."""
from __future__ import annotations

import argparse
import json
import secrets
import subprocess
import sys
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Deque, Dict, List, Optional, Sequence, Tuple

SERVER_NAME = "kubectl-write"
SERVER_VERSION = "1.0.0"
SUPPORTED_PROTOCOLS = ("2025-06-18", "2025-03-26", "2024-11-05")
DEFAULT_PROTECTED = ("kube-system", "kube-public", "kube-node-lease")
DEFAULT_TTL_SECONDS = 120.0
DEFAULT_MAX_PER_MINUTE = 5
DEFAULT_MAX_REPLICAS = 10
DEFAULT_OUTPUT_LIMIT = 8000
DEFAULT_AUDIT_PATH = "kubectl-write-audit.jsonl"
RATE_WINDOW_SECONDS = 60.0

Runner = Callable[[Sequence[str]], Tuple[int, str, str]]
Reader = Callable[[List[str]], Optional[Any]]
Audit = Callable[[Dict[str, Any]], None]


@dataclass
class Policy:
    allow_write: bool = False
    namespaces: Optional[List[str]] = None
    protected: List[str] = field(default_factory=lambda: list(DEFAULT_PROTECTED))
    nodes: List[str] = field(default_factory=list)
    context: Optional[str] = None
    ttl_seconds: float = DEFAULT_TTL_SECONDS
    max_per_minute: int = DEFAULT_MAX_PER_MINUTE
    max_replicas: int = DEFAULT_MAX_REPLICAS
    output_limit: int = DEFAULT_OUTPUT_LIMIT

    def namespace_problem(self, namespace: str) -> Optional[str]:
        if namespace in self.protected:
            return f"namespace {namespace!r} is protected; no write tool touches it"
        if self.namespaces is not None and namespace not in self.namespaces:
            return f"namespace {namespace!r} is not in the allow-list ({', '.join(self.namespaces)})"
        return None

    def node_problem(self, node: str) -> Optional[str]:
        if node in self.nodes:
            return None
        listed = ", ".join(self.nodes) if self.nodes else "empty; start the server with --allow-nodes"
        return f"node {node!r} is not in the node allow-list ({listed})"


@dataclass
class BlastRadius:
    pods_affected: int
    percent: float
    note: str

    def describe(self) -> str:
        return f"{self.pods_affected} pod(s) affected, {self.percent:.0f}% of current capacity: {self.note}"

    def as_dict(self) -> Dict[str, Any]:
        return {"pods_affected": self.pods_affected, "percent": round(self.percent, 1), "note": self.note}


def scale_blast(before: int, after: int) -> BlastRadius:
    delta = after - before
    if delta == 0:
        return BlastRadius(0, 0.0, "no change, the deployment already has that many replicas")
    if after == 0:
        return BlastRadius(before, 100.0, "scales to zero; every pod stops and the service has no endpoints")
    percent = 100.0 if before == 0 else min(100.0, abs(delta) / before * 100.0)
    verb = "adds" if delta > 0 else "removes"
    return BlastRadius(abs(delta), percent, f"{verb} {abs(delta)} replica(s)")


def restart_blast(replicas: int, max_unavailable: str) -> BlastRadius:
    return BlastRadius(replicas, 100.0, f"every pod is replaced by a rolling update, at most {max_unavailable} unavailable at a time")


def delete_pod_blast(siblings: Optional[int], owner: Optional[str]) -> BlastRadius:
    if owner is None:
        return BlastRadius(1, 100.0, "no controller owns this pod; it will not come back")
    share = 100.0 / siblings if siblings else 100.0
    return BlastRadius(1, share, f"{owner} recreates it; one of {siblings or '?'} replica(s) restarts")


def cordon_blast(before: Dict[str, Any], cordon: bool) -> BlastRadius:
    if bool(before["unschedulable"]) == cordon:
        return BlastRadius(0, 0.0, "already in that state, no change")
    schedulable = int(before["schedulable_nodes"])
    if cordon:
        percent = 100.0 / schedulable if schedulable else 100.0
        return BlastRadius(0, percent, f"removes one of {schedulable} schedulable node(s); {before['pods_on_node']} pod(s) keep running there and nothing new lands")
    return BlastRadius(0, 100.0 / (schedulable + 1), "adds the node back to the schedulable set; nothing running is touched")


def observe_deployment(args: Dict[str, Any], read: Reader) -> Dict[str, Any]:
    doc = read(["get", "deployment", str(args["deployment"]), "-n", str(args["namespace"]), "-o", "json"])
    if not isinstance(doc, dict):
        return {"present": False}
    spec, status, meta = doc.get("spec") or {}, doc.get("status") or {}, doc.get("metadata") or {}
    strategy = spec.get("strategy") or {}
    rolling = strategy.get("rollingUpdate") or {}
    max_unavailable = "all" if strategy.get("type") == "Recreate" else str(rolling.get("maxUnavailable", "25%"))
    return {
        "present": True,
        "replicas": int(spec.get("replicas") or 0),
        "ready": int(status.get("readyReplicas") or 0),
        "available": int(status.get("availableReplicas") or 0),
        "generation": int(meta.get("generation") or 0),
        "max_unavailable": max_unavailable,
    }


def observe_pod(args: Dict[str, Any], read: Reader) -> Dict[str, Any]:
    namespace = str(args["namespace"])
    doc = read(["get", "pod", str(args["name"]), "-n", namespace, "-o", "json"])
    if not isinstance(doc, dict):
        return {"present": False}
    meta, spec, status = doc.get("metadata") or {}, doc.get("spec") or {}, doc.get("status") or {}
    owner = next((ref for ref in meta.get("ownerReferences") or [] if ref.get("controller")), None)
    state: Dict[str, Any] = {"present": True, "phase": status.get("phase", "Unknown"), "node": spec.get("nodeName", ""), "owner": None, "siblings": None}
    if owner:
        state["owner"] = f"{owner['kind']}/{owner['name']}"
        if owner["kind"] in ("ReplicaSet", "StatefulSet", "DaemonSet"):
            parent = read(["get", owner["kind"].lower(), str(owner["name"]), "-n", namespace, "-o", "json"])
            if isinstance(parent, dict):
                if owner["kind"] == "DaemonSet":
                    state["siblings"] = int((parent.get("status") or {}).get("desiredNumberScheduled") or 0)
                else:
                    state["siblings"] = int((parent.get("spec") or {}).get("replicas") or 0)
    return state


def observe_node(args: Dict[str, Any], read: Reader) -> Dict[str, Any]:
    node = str(args["node"])
    doc = read(["get", "nodes", "-o", "json"])
    items = (doc or {}).get("items") or [] if isinstance(doc, dict) else []
    target = next((item for item in items if (item.get("metadata") or {}).get("name") == node), None)
    if target is None:
        return {"present": False}
    pods = read(["get", "pods", "--all-namespaces", "--field-selector", f"spec.nodeName={node}", "-o", "json"])
    return {
        "present": True,
        "unschedulable": bool((target.get("spec") or {}).get("unschedulable")),
        "schedulable_nodes": sum(1 for item in items if not (item.get("spec") or {}).get("unschedulable")),
        "total_nodes": len(items),
        "pods_on_node": len((pods or {}).get("items") or []) if isinstance(pods, dict) else 0,
    }


@dataclass
class ActionSpec:
    name: str
    description: str
    properties: Dict[str, Dict[str, Any]]
    required: List[str]
    destructive: bool
    target: Callable[[Dict[str, Any]], str]
    argv: Callable[[Dict[str, Any]], List[str]]
    observe: Callable[[Dict[str, Any], Reader], Dict[str, Any]]
    blast: Callable[[Dict[str, Any], Dict[str, Any]], BlastRadius]
    summary: Callable[[Dict[str, Any], Dict[str, Any]], str]
    undo: Callable[[Dict[str, Any], Dict[str, Any]], Optional[List[str]]]


NAMESPACE = {"type": "string", "description": "namespace of the target; protected namespaces are refused"}
DEPLOYMENT = {"type": "string", "description": "deployment name"}
NODE = {"type": "string", "description": "node name; must be in the server's node allow-list"}


def _deployment(args: Dict[str, Any]) -> str:
    return f"deployment {args['namespace']}/{args['deployment']}"


ACTIONS: List[ActionSpec] = [
    ActionSpec(
        "scale",
        "Set the replica count of a deployment.",
        {"namespace": NAMESPACE, "deployment": DEPLOYMENT, "replicas": {"type": "integer", "minimum": 0}},
        ["namespace", "deployment", "replicas"],
        False,
        _deployment,
        lambda a: ["scale", f"deployment/{a['deployment']}", "-n", str(a["namespace"]), f"--replicas={int(a['replicas'])}"],
        observe_deployment,
        lambda a, before: scale_blast(int(before["replicas"]), int(a["replicas"])),
        lambda a, before: f"scale deployment {a['namespace']}/{a['deployment']} from {before['replicas']} to {int(a['replicas'])}",
        lambda a, before: ["scale", f"deployment/{a['deployment']}", "-n", str(a["namespace"]), f"--replicas={before['replicas']}"],
    ),
    ActionSpec(
        "rollout_restart",
        "Restart every pod of a deployment with a rolling update.",
        {"namespace": NAMESPACE, "deployment": DEPLOYMENT},
        ["namespace", "deployment"],
        False,
        _deployment,
        lambda a: ["rollout", "restart", f"deployment/{a['deployment']}", "-n", str(a["namespace"])],
        observe_deployment,
        lambda a, before: restart_blast(int(before["replicas"]), str(before["max_unavailable"])),
        lambda a, before: f"rollout restart deployment {a['namespace']}/{a['deployment']} ({before['replicas']} pods)",
        lambda a, before: ["rollout", "undo", f"deployment/{a['deployment']}", "-n", str(a["namespace"])],
    ),
    ActionSpec(
        "cordon",
        "Mark a node unschedulable. Running pods stay where they are; new pods go elsewhere.",
        {"node": NODE},
        ["node"],
        False,
        lambda a: f"node {a['node']}",
        lambda a: ["cordon", str(a["node"])],
        observe_node,
        lambda a, before: cordon_blast(before, True),
        lambda a, before: f"cordon node {a['node']}",
        lambda a, before: ["uncordon", str(a["node"])],
    ),
    ActionSpec(
        "uncordon",
        "Mark a node schedulable again.",
        {"node": NODE},
        ["node"],
        False,
        lambda a: f"node {a['node']}",
        lambda a: ["uncordon", str(a["node"])],
        observe_node,
        lambda a, before: cordon_blast(before, False),
        lambda a, before: f"uncordon node {a['node']}",
        lambda a, before: ["cordon", str(a["node"])],
    ),
    ActionSpec(
        "delete_pod",
        "Delete one pod. Its controller, if it has one, creates a replacement.",
        {"namespace": NAMESPACE, "name": {"type": "string", "description": "pod name"}},
        ["namespace", "name"],
        True,
        lambda a: f"pod {a['namespace']}/{a['name']}",
        lambda a: ["delete", "pod", str(a["name"]), "-n", str(a["namespace"]), "--wait=false"],
        observe_pod,
        lambda a, before: delete_pod_blast(before.get("siblings"), before.get("owner")),
        lambda a, before: f"delete pod {a['namespace']}/{a['name']}",
        lambda a, before: None,
    ),
]
ACTIONS_BY_NAME = {action.name: action for action in ACTIONS}


@dataclass
class ToolDef:
    name: str
    action: ActionSpec
    kind: str
    properties: Dict[str, Dict[str, Any]]
    required: List[str]
    description: str
    annotations: Dict[str, Any]

    def listing(self) -> Dict[str, Any]:
        schema = {"type": "object", "properties": self.properties, "required": self.required, "additionalProperties": False}
        return {"name": self.name, "description": self.description, "inputSchema": schema, "annotations": self.annotations}


def tool_definitions(policy: Policy) -> List[ToolDef]:
    tools: List[ToolDef] = []
    for action in ACTIONS:
        props = {key: dict(value) for key, value in action.properties.items()}
        if "replicas" in props:
            props["replicas"]["maximum"] = policy.max_replicas
        tools.append(ToolDef(
            f"plan_{action.name}", action, "plan", props, list(action.required),
            f"Plan a {action.name}: {action.description} Reads the current state, runs kubectl with --dry-run=server, measures the blast radius and returns the confirm phrase that {action.name} needs. Changes nothing.",
            {"title": f"Plan {action.name}", "readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False},
        ))
        if policy.allow_write:
            write_props = dict(props)
            write_props["confirm"] = {"type": "string", "description": f"the confirm phrase returned by plan_{action.name}, passed unchanged"}
            tools.append(ToolDef(
                action.name, action, "write", write_props, [*action.required, "confirm"],
                f"{action.description} Needs the confirm phrase from plan_{action.name}; runs once, inside the TTL, with the planned arguments. The same phrase a second time returns the first result and runs nothing.",
                {"title": action.name, "readOnlyHint": False, "destructiveHint": action.destructive, "idempotentHint": True, "openWorldHint": False},
            ))
    return tools


def validate_args(properties: Dict[str, Dict[str, Any]], required: Sequence[str], args: Any) -> Optional[str]:
    if not isinstance(args, dict):
        return "arguments must be an object"
    for key in required:
        if key not in args or args[key] in (None, ""):
            return f"missing required argument: {key}"
    for key, value in args.items():
        prop = properties.get(key)
        if prop is None:
            return f"unknown argument: {key}"
        expected = prop["type"]
        ok = (
            (expected == "string" and isinstance(value, str))
            or (expected == "integer" and isinstance(value, int) and not isinstance(value, bool))
            or (expected == "boolean" and isinstance(value, bool))
        )
        if not ok:
            return f"argument {key} must be a {expected}"
        if expected == "integer":
            if "minimum" in prop and value < prop["minimum"]:
                return f"argument {key} must be >= {prop['minimum']}"
            if "maximum" in prop and value > prop["maximum"]:
                return f"argument {key} must be <= {prop['maximum']} (the server's --max-replicas)"
    return None


@dataclass
class Plan:
    action: str
    arguments: Dict[str, Any]
    summary: str
    phrase: str
    command: List[str]
    undo_command: Optional[List[str]]
    before: Dict[str, Any]
    blast: BlastRadius
    created_at: float
    expires_at: float
    executed_at: Optional[float] = None
    result: Optional[Dict[str, Any]] = None


def iso(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def public(state: Dict[str, Any]) -> Dict[str, Any]:
    return {key: value for key, value in state.items() if key != "present"}


def describe_state(state: Dict[str, Any]) -> str:
    if not state.get("present"):
        return "absent"
    return " ".join(f"{key}={'-' if value is None else value}" for key, value in sorted(public(state).items()))


def truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n… truncated ({len(text) - limit} more characters)"


def text_result(text: str, is_error: bool = False, structured: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    result: Dict[str, Any] = {"content": [{"type": "text", "text": text}], "isError": is_error}
    if structured is not None:
        result["structuredContent"] = structured
    return result


def render_plan(plan: Plan, dry_run: str, ttl_seconds: float) -> Dict[str, Any]:
    undo = " ".join(plan.undo_command) if plan.undo_command else "none; this cannot be undone"
    lines = [
        f"PLAN {plan.summary}",
        f"command: {' '.join(plan.command)}",
        f"dry run (server): {dry_run or '(no output)'}",
        f"before: {describe_state(plan.before)}",
        f"blast radius: {plan.blast.describe()}",
        f"undo after execution: {undo}",
        f"confirm phrase (valid {ttl_seconds:.0f}s, single use): {plan.phrase}",
    ]
    structured = {"plan": {
        "action": plan.action,
        "arguments": plan.arguments,
        "command": plan.command,
        "dry_run": dry_run,
        "before": public(plan.before),
        "blast": plan.blast.as_dict(),
        "undo": plan.undo_command,
        "confirm": plan.phrase,
        "expires_in_seconds": ttl_seconds,
    }}
    return text_result("\n".join(lines), structured=structured)


def render_execution(plan: Plan, code: int, output: str, after: Dict[str, Any], limit: int) -> Dict[str, Any]:
    undo = " ".join(plan.undo_command) if plan.undo_command else "none"
    verdict = "EXECUTED" if code == 0 else f"FAILED (exit {code})"
    lines = [
        f"{verdict} {plan.summary} [{plan.phrase.rsplit('[', 1)[-1]}",
        f"$ {' '.join(plan.command)}",
        truncate(output or "(no output)", limit),
        f"before: {describe_state(plan.before)}",
        f"after:  {describe_state(after)}",
        f"undo: {undo}",
    ]
    if code != 0:
        lines.append("the confirm phrase is spent; plan again and read the new dry run")
    structured = {"execution": {
        "action": plan.action,
        "arguments": plan.arguments,
        "command": plan.command,
        "exit_code": code,
        "output": output,
        "before": public(plan.before),
        "after": public(after) if after.get("present") else {"present": False},
        "undo": plan.undo_command,
        "confirm": plan.phrase,
        "executed_at": iso(plan.executed_at or 0),
    }}
    return text_result("\n".join(lines), code != 0, structured)


def replay_result(first: Dict[str, Any], executed_at: str) -> Dict[str, Any]:
    text = f"REPLAYED: this confirm phrase was already executed at {executed_at}; nothing ran again\n" + first["content"][0]["text"]
    result = dict(first)
    result["content"] = [{"type": "text", "text": text}]
    structured = dict(first.get("structuredContent") or {})
    structured["replayed"] = True
    result["structuredContent"] = structured
    return result


def subprocess_runner(argv: Sequence[str]) -> Tuple[int, str, str]:
    try:
        proc = subprocess.run(list(argv), capture_output=True, text=True, timeout=60)
    except FileNotFoundError:
        return 127, "", "kubectl not found on PATH"
    except subprocess.TimeoutExpired:
        return 124, "", "kubectl timed out after 60s"
    return proc.returncode, proc.stdout, proc.stderr


def file_audit(path: str) -> Audit:
    def append(entry: Dict[str, Any]) -> None:
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, sort_keys=True) + "\n")
    return append


class WriteServer:
    def __init__(
        self,
        policy: Policy,
        runner: Runner = subprocess_runner,
        now: Callable[[], float] = time.time,
        audit: Audit = lambda entry: None,
        token: Callable[[], str] = lambda: secrets.token_hex(4),
    ) -> None:
        self.policy = policy
        self.runner = runner
        self.now = now
        self.audit = audit
        self.token = token
        self.tools: Dict[str, ToolDef] = {tool.name: tool for tool in tool_definitions(policy)}
        self.plans: Dict[str, Plan] = {}
        self.executions: Deque[float] = deque()
        self.last_read_error = ""

    def command(self, argv: Sequence[str]) -> List[str]:
        context = ["--context", self.policy.context] if self.policy.context else []
        return ["kubectl", *context, *argv]

    def read_json(self, argv: List[str]) -> Optional[Any]:
        code, out, err = self.runner(self.command(argv))
        if code != 0:
            self.last_read_error = (err.strip() or out.strip())[:300]
            return None
        try:
            return json.loads(out)
        except ValueError:
            self.last_read_error = "kubectl returned something that is not JSON"
            return None

    def record(self, event: str, tool: str, arguments: Dict[str, Any], **extra: Any) -> None:
        entry: Dict[str, Any] = {"at": iso(self.now()), "event": event, "tool": tool, "arguments": arguments}
        entry.update(extra)
        self.audit(entry)

    def scope_problem(self, args: Dict[str, Any]) -> Optional[str]:
        if "namespace" in args:
            problem = self.policy.namespace_problem(str(args["namespace"]))
            if problem:
                return problem
        if "node" in args:
            return self.policy.node_problem(str(args["node"]))
        return None

    def new_phrase(self, summary: str) -> str:
        while True:
            phrase = f"{summary} [{self.token()}]"
            if phrase not in self.plans:
                return phrase

    def call(self, tool: ToolDef, args: Any) -> Dict[str, Any]:
        problem = validate_args(tool.properties, tool.required, args)
        if problem:
            return text_result(f"invalid arguments for {tool.name}: {problem}", True)
        return self.plan(tool, args) if tool.kind == "plan" else self.execute(tool, args)

    def plan(self, tool: ToolDef, args: Dict[str, Any]) -> Dict[str, Any]:
        action = tool.action
        problem = self.scope_problem(args)
        if problem:
            self.record("refused", tool.name, args, reason=problem)
            return text_result(f"refused: {problem}", True)
        before = action.observe(args, self.read_json)
        if not before.get("present"):
            self.record("refused", tool.name, args, reason="target not found", detail=self.last_read_error)
            return text_result(f"refused: {action.target(args)} not found; nothing to plan ({self.last_read_error or 'no such object'})", True)
        command = self.command(action.argv(args))
        code, out, err = self.runner([*command, "--dry-run=server"])
        if code != 0:
            detail = err.strip() or out.strip()
            self.record("dry_run_failed", tool.name, args, command=command, exit_code=code, output=detail[:500])
            return text_result(f"dry run failed (exit {code}): {detail}\nno confirm phrase was issued; fix the cause and plan again", True)
        blast = action.blast(args, before)
        summary = action.summary(args, before)
        created = self.now()
        undo = action.undo(args, before)
        plan = Plan(action.name, dict(args), summary, self.new_phrase(summary), command, self.command(undo) if undo else None, before, blast, created, created + self.policy.ttl_seconds)
        self.plans[plan.phrase] = plan
        self.record("plan", tool.name, args, confirm=plan.phrase, command=command, before=public(before), blast=blast.as_dict(), expires_at=iso(plan.expires_at), dry_run=out.strip()[:500])
        return render_plan(plan, out.strip(), self.policy.ttl_seconds)

    def execute(self, tool: ToolDef, args: Dict[str, Any]) -> Dict[str, Any]:
        action = tool.action
        phrase = str(args["confirm"])
        wanted = {key: value for key, value in args.items() if key != "confirm"}
        plan = self.plans.get(phrase)
        if plan is None:
            self.record("refused", tool.name, wanted, confirm=phrase, reason="unknown confirm phrase")
            return text_result(f"refused: no plan matches this confirm phrase; call plan_{action.name} first and pass its phrase unchanged", True)
        if plan.action != action.name or plan.arguments != wanted:
            self.record("refused", tool.name, wanted, confirm=phrase, reason="confirm phrase belongs to a different plan")
            return text_result(f"refused: this confirm phrase belongs to {plan.action} {json.dumps(plan.arguments, sort_keys=True)}, not to {action.name} {json.dumps(wanted, sort_keys=True)}", True)
        if plan.result is not None:
            self.record("replayed", tool.name, wanted, confirm=phrase, executed_at=iso(plan.executed_at or 0))
            return replay_result(plan.result, iso(plan.executed_at or 0))
        now = self.now()
        if now > plan.expires_at:
            del self.plans[phrase]
            self.record("expired", tool.name, wanted, confirm=phrase, expired_seconds_ago=round(now - plan.expires_at, 1))
            return text_result(f"refused: the confirm phrase expired {now - plan.expires_at:.0f}s ago (TTL {self.policy.ttl_seconds:.0f}s); plan again and read the new dry run", True)
        problem = self.scope_problem(wanted)
        if problem:
            self.record("refused", tool.name, wanted, confirm=phrase, reason=problem)
            return text_result(f"refused: {problem}", True)
        while self.executions and now - self.executions[0] >= RATE_WINDOW_SECONDS:
            self.executions.popleft()
        if len(self.executions) >= self.policy.max_per_minute:
            wait = RATE_WINDOW_SECONDS - (now - self.executions[0])
            self.record("rate_limited", tool.name, wanted, confirm=phrase, retry_in_seconds=round(wait, 1))
            return text_result(f"refused: rate limit of {self.policy.max_per_minute} write(s) per minute reached; retry in {wait:.0f}s with the same confirm phrase if it is still valid", True)
        self.executions.append(now)
        plan.executed_at = now
        code, out, err = self.runner(plan.command)
        output = (out.strip() or err.strip())
        after = action.observe(wanted, self.read_json)
        plan.result = render_execution(plan, code, output, after, self.policy.output_limit)
        self.record("execute", tool.name, wanted, confirm=phrase, command=plan.command, exit_code=code, output=output[:500], before=public(plan.before), after=public(after) if after.get("present") else {"present": False}, undo=plan.undo_command)
        return plan.result


def handle(message: Dict[str, Any], server: WriteServer) -> Optional[Dict[str, Any]]:
    method = message.get("method")
    msg_id = message.get("id")
    params = message.get("params") or {}

    def ok(result: Any) -> Dict[str, Any]:
        return {"jsonrpc": "2.0", "id": msg_id, "result": result}

    def err(code: int, text: str) -> Dict[str, Any]:
        return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": code, "message": text}}

    if method == "initialize":
        requested = params.get("protocolVersion")
        version = requested if requested in SUPPORTED_PROTOCOLS else SUPPORTED_PROTOCOLS[0]
        mode = "Write tools are enabled." if server.policy.allow_write else "Write tools are disabled on this server: plans only; hand the plan to an operator."
        return ok({
            "protocolVersion": version,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
            "instructions": "Every change to the cluster takes two calls. Call plan_<action> first: it changes nothing and returns the exact command, a server-side dry run, the current state, the blast radius and a confirm phrase. Show the plan to the operator, then call <action> with that phrase unchanged, before it expires. Never invent or edit a confirm phrase. A phrase works once; a second call returns the first result. " + mode,
        })
    if method == "ping":
        return ok({})
    if method == "tools/list":
        return ok({"tools": [tool.listing() for tool in server.tools.values()]})
    if method == "tools/call":
        name = params.get("name")
        tool = server.tools.get(str(name))
        if tool is None:
            hint = " (write tools need --allow-write; plan_" + str(name) + " is available)" if name in ACTIONS_BY_NAME else ""
            return err(-32602, f"Unknown tool: {name}{hint}")
        return ok(server.call(tool, params.get("arguments")))
    if msg_id is None:
        return None
    return err(-32601, f"Method not found: {method}")


def serve(stdin, stdout, server: WriteServer, log: Callable[[str], None] = lambda line: print(line, file=sys.stderr, flush=True)) -> int:
    for raw in stdin:
        line = raw.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except json.JSONDecodeError as exc:
            stdout.write(json.dumps({"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": f"Parse error: {exc.msg}"}}) + "\n")
            stdout.flush()
            continue
        log(f"← {message.get('method')} id={message.get('id')}")
        response = handle(message, server)
        if response is not None:
            stdout.write(json.dumps(response) + "\n")
            stdout.flush()
    return 0


def split_list(value: Optional[str]) -> List[str]:
    return [item.strip() for item in value.split(",") if item.strip()] if value else []


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="kubectl_write_mcp", description=__doc__)
    parser.add_argument("--allow-write", action="store_true", help="expose the write tools; without it only plan_* exist")
    parser.add_argument("--allow-namespaces", help="comma-separated namespaces writes may target (default: any that is not protected)")
    parser.add_argument("--protect", default=",".join(DEFAULT_PROTECTED), help="comma-separated namespaces no write tool may touch")
    parser.add_argument("--allow-nodes", help="comma-separated nodes cordon/uncordon may target (default: none)")
    parser.add_argument("--context", help="kubeconfig context to pin on every command")
    parser.add_argument("--ttl", type=float, default=DEFAULT_TTL_SECONDS, help="seconds a confirm phrase stays valid")
    parser.add_argument("--max-per-minute", type=int, default=DEFAULT_MAX_PER_MINUTE, help="writes the server executes per minute")
    parser.add_argument("--max-replicas", type=int, default=DEFAULT_MAX_REPLICAS, help="largest replica count scale may plan")
    parser.add_argument("--audit", default=DEFAULT_AUDIT_PATH, help="append-only JSONL file with one line per plan, execution and refusal")
    parser.add_argument("--list-tools", action="store_true", help="print the tools this configuration exposes as JSON and exit")
    parser.add_argument("--quiet", action="store_true", help="no request log on stderr")
    args = parser.parse_args(list(sys.argv[1:] if argv is None else argv))
    if args.ttl <= 0 or args.max_per_minute <= 0 or args.max_replicas < 0:
        print("kubectl_write_mcp: --ttl and --max-per-minute must be positive, --max-replicas must not be negative", file=sys.stderr)
        return 2
    policy = Policy(
        allow_write=args.allow_write,
        namespaces=split_list(args.allow_namespaces) or None,
        protected=split_list(args.protect),
        nodes=split_list(args.allow_nodes),
        context=args.context,
        ttl_seconds=args.ttl,
        max_per_minute=args.max_per_minute,
        max_replicas=args.max_replicas,
    )
    server = WriteServer(policy, subprocess_runner, time.time, file_audit(args.audit))
    if args.list_tools:
        print(json.dumps([tool.listing() for tool in server.tools.values()], indent=2))
        return 0
    log = (lambda line: None) if args.quiet else (lambda line: print(f"[kubectl_write_mcp] {line}", file=sys.stderr, flush=True))
    return serve(sys.stdin, sys.stdout, server, log)


if __name__ == "__main__":
    raise SystemExit(main())
