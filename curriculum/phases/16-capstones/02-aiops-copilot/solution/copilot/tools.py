from __future__ import annotations

import json
from typing import Any, Dict, List, Tuple

from .config import Config
from .guard import SECRET_KINDS, redact, truncate, wrap
from .kube import Kube, KubeError
from .proposals import ACTIONS, ProposalStore, plan

DESCRIBABLE = ("pod", "deployment", "replicaset", "service", "statefulset", "daemonset", "configmap", "node")

TOOLS: List[Dict[str, Any]] = [
    {
        "name": "list_pods",
        "description": "List the pods of one namespace with status, restarts, age and node (kubectl get pods -o wide).",
        "input_schema": {"type": "object", "properties": {"namespace": {"type": "string"}}, "required": ["namespace"], "additionalProperties": False},
    },
    {
        "name": "describe",
        "description": "kubectl describe for one object: containers, states, conditions, probes, limits and recent events. Secrets cannot be described.",
        "input_schema": {
            "type": "object",
            "properties": {"namespace": {"type": "string"}, "kind": {"type": "string", "enum": list(DESCRIBABLE)}, "name": {"type": "string"}},
            "required": ["namespace", "kind", "name"],
            "additionalProperties": False,
        },
    },
    {
        "name": "pod_logs",
        "description": "The last lines of a pod's logs. Set previous=true to read the container instance that crashed.",
        "input_schema": {
            "type": "object",
            "properties": {
                "namespace": {"type": "string"},
                "name": {"type": "string"},
                "container": {"type": "string"},
                "previous": {"type": "boolean"},
                "tail_lines": {"type": "integer", "minimum": 1, "maximum": 500},
            },
            "required": ["namespace", "name"],
            "additionalProperties": False,
        },
    },
    {
        "name": "list_events",
        "description": "Events of one namespace, oldest first; Warning events explain most failures.",
        "input_schema": {"type": "object", "properties": {"namespace": {"type": "string"}}, "required": ["namespace"], "additionalProperties": False},
    },
    {
        "name": "propose_action",
        "description": "Propose restart, scale or rollback of a Deployment. Nothing runs: the copilot records a proposal with a server-side dry-run diff and an operator approves it with `copilot apply --approve <id>`.",
        "input_schema": {
            "type": "object",
            "properties": {
                "namespace": {"type": "string"},
                "action": {"type": "string", "enum": list(ACTIONS)},
                "target": {"type": "string", "description": "the Deployment name"},
                "replicas": {"type": "integer", "minimum": 0, "description": "only for scale"},
                "reason": {"type": "string", "description": "the evidence that justifies the change"},
            },
            "required": ["namespace", "action", "target", "reason"],
            "additionalProperties": False,
        },
    },
]
TOOL_NAMES = {tool["name"] for tool in TOOLS}


class ToolBox:
    def __init__(self, kube: Kube, config: Config, store: ProposalStore) -> None:
        self.kube = kube
        self.config = config
        self.store = store
        self.proposals: List[Dict[str, Any]] = []

    def definitions(self) -> List[Dict[str, Any]]:
        return TOOLS

    def kubectl(self, source: str, *args: str) -> Tuple[str, bool]:
        code, out, err = self.kube.run(*args)
        text = out if code == 0 else (err or out or f"kubectl exited {code}")
        return wrap(source, truncate(redact(text.rstrip()))), code != 0

    def call(self, name: str, arguments: Any) -> Tuple[str, bool]:
        if name not in TOOL_NAMES:
            return f"unknown tool {name!r}; only {', '.join(sorted(TOOL_NAMES))} exist.", True
        if not isinstance(arguments, dict):
            return "tool input must be an object", True
        namespace = arguments.get("namespace")
        if not self.config.allows(namespace):
            return f"refused: namespace {namespace!r} is outside this copilot's allow-list ({', '.join(self.config.allowed)}).", True
        if name == "list_pods":
            return self.kubectl(name, "get", "pods", "-n", namespace, "-o", "wide")
        if name == "list_events":
            return self.kubectl(name, "get", "events", "-n", namespace, "--sort-by=.lastTimestamp")
        if name == "describe":
            kind = str(arguments.get("kind", "")).lower()
            if kind in SECRET_KINDS:
                return "refused: this copilot never reads Secrets. Say which credential you suspect and the operator will check it.", True
            if kind not in DESCRIBABLE:
                return f"refused: describe supports {', '.join(DESCRIBABLE)}", True
            return self.kubectl(name, "describe", kind, str(arguments.get("name", "")), "-n", namespace)
        if name == "pod_logs":
            tail = int(arguments.get("tail_lines") or 100)
            args = ["logs", str(arguments.get("name", "")), "-n", namespace, f"--tail={max(1, min(tail, 500))}"]
            if arguments.get("container"):
                args += ["-c", str(arguments["container"])]
            if arguments.get("previous"):
                args.append("--previous")
            return self.kubectl(name, *args)
        return self.propose(arguments)

    def propose(self, arguments: Dict[str, Any]) -> Tuple[str, bool]:
        replicas = arguments.get("replicas")
        try:
            proposal = plan(self.kube, self.store, arguments["namespace"], str(arguments.get("action")), str(arguments.get("target", "")), int(replicas) if replicas is not None else None, str(arguments.get("reason", "")))
        except (KubeError, ValueError, KeyError) as exc:
            return f"no proposal recorded: {redact(str(exc))}", True
        self.proposals.append(proposal)
        changes = "; ".join(f"{d['path']}: {json.dumps(d['before'])} -> {json.dumps(d['after'])}" for d in proposal["diff"][:5]) or "no field changes"
        return f"proposal {proposal['id']} recorded with risk {proposal['risk']}; dry run: {changes}. Nothing was changed. Tell the operator to run `copilot apply --approve {proposal['id']}` if they agree.", False
