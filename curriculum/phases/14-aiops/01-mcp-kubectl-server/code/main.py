"""kubectl_mcp: a read-only Model Context Protocol server for kubectl, speaking JSON-RPC 2.0 over stdio, with an allow-list for namespaces and verbs."""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

SERVER_NAME = "kubectl-readonly"
SERVER_VERSION = "1.0.0"
SUPPORTED_PROTOCOLS = ("2025-06-18", "2025-03-26", "2024-11-05")
READ_ONLY_VERBS = {"get", "describe", "logs", "top", "version", "api-resources", "explain"}
FORBIDDEN_FLAGS = {"--raw", "--kubeconfig", "--token", "--server", "--as", "--as-group", "--exec"}
DEFAULT_OUTPUT_LIMIT = 12_000

Runner = Callable[[Sequence[str]], Tuple[int, str, str]]


@dataclass
class Policy:
    namespaces: Optional[List[str]] = None
    context: Optional[str] = None
    output_limit: int = DEFAULT_OUTPUT_LIMIT

    def allows_namespace(self, namespace: Optional[str]) -> bool:
        if self.namespaces is None:
            return True
        return namespace is not None and namespace in self.namespaces


@dataclass
class ToolSpec:
    name: str
    description: str
    properties: Dict[str, Dict[str, Any]]
    required: List[str]
    build: Callable[[Dict[str, Any]], List[str]]

    def schema(self) -> Dict[str, Any]:
        return {"type": "object", "properties": self.properties, "required": self.required, "additionalProperties": False}


def _ns(args: Dict[str, Any]) -> List[str]:
    return ["-n", str(args["namespace"])] if args.get("namespace") else ["--all-namespaces"]


TOOLS: List[ToolSpec] = [
    ToolSpec(
        "list_pods",
        "List pods with status, restarts and age. Optional namespace (default: all) and label selector.",
        {"namespace": {"type": "string"}, "selector": {"type": "string", "description": "label selector, e.g. app=web"}},
        [],
        lambda a: ["get", "pods", *_ns(a), *(["-l", str(a["selector"])] if a.get("selector") else []), "-o", "wide"],
    ),
    ToolSpec(
        "describe_pod",
        "Full description of one pod: containers, conditions, events, resource requests and limits.",
        {"namespace": {"type": "string"}, "name": {"type": "string"}},
        ["namespace", "name"],
        lambda a: ["describe", "pod", str(a["name"]), "-n", str(a["namespace"])],
    ),
    ToolSpec(
        "pod_logs",
        "Recent logs of a pod (last N lines, default 100). Optional container name and previous=true for the crashed instance.",
        {
            "namespace": {"type": "string"},
            "name": {"type": "string"},
            "container": {"type": "string"},
            "tail_lines": {"type": "integer", "minimum": 1, "maximum": 2000},
            "previous": {"type": "boolean"},
        },
        ["namespace", "name"],
        lambda a: [
            "logs", str(a["name"]), "-n", str(a["namespace"]),
            f"--tail={int(a.get('tail_lines') or 100)}",
            *(["-c", str(a["container"])] if a.get("container") else []),
            *(["--previous"] if a.get("previous") else []),
        ],
    ),
    ToolSpec(
        "list_events",
        "Recent Kubernetes events sorted by time, the first place to look when something is Pending or CrashLooping.",
        {"namespace": {"type": "string"}, "warnings_only": {"type": "boolean"}},
        [],
        lambda a: ["get", "events", *_ns(a), "--sort-by=.lastTimestamp", *(["--field-selector", "type=Warning"] if a.get("warnings_only") else [])],
    ),
]
TOOLS_BY_NAME = {tool.name: tool for tool in TOOLS}


def validate_args(spec: ToolSpec, args: Any) -> Optional[str]:
    if not isinstance(args, dict):
        return "arguments must be an object"
    for key in spec.required:
        if key not in args or args[key] in (None, ""):
            return f"missing required argument: {key}"
    for key, value in args.items():
        prop = spec.properties.get(key)
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
                return f"argument {key} must be <= {prop['maximum']}"
    return None


def ensure_read_only(argv: Sequence[str]) -> None:
    if not argv or argv[0] not in READ_ONLY_VERBS:
        raise PermissionError(f"verb {argv[0] if argv else '(none)'!r} is not read-only")
    for item in argv:
        flag = item.split("=", 1)[0]
        if flag in FORBIDDEN_FLAGS:
            raise PermissionError(f"flag {flag} is not allowed")


def kubectl_argv(spec: ToolSpec, args: Dict[str, Any], policy: Policy) -> List[str]:
    if "namespace" in spec.properties and not policy.allows_namespace(args.get("namespace")):
        allowed = ", ".join(policy.namespaces or [])
        asked = repr(args.get("namespace")) if args.get("namespace") else "all namespaces"
        raise PermissionError(f"namespace {asked} is not in the allow-list ({allowed}); pass one of them explicitly")
    argv = spec.build(args)
    if policy.context:
        argv = ["--context", policy.context, *argv]
    ensure_read_only([a for a in argv if a not in ("--context", policy.context)] if policy.context else argv)
    return ["kubectl", *argv]


def subprocess_runner(argv: Sequence[str]) -> Tuple[int, str, str]:
    try:
        proc = subprocess.run(list(argv), capture_output=True, text=True, timeout=30)
    except FileNotFoundError:
        return 127, "", "kubectl not found on PATH"
    except subprocess.TimeoutExpired:
        return 124, "", "kubectl timed out after 30s"
    return proc.returncode, proc.stdout, proc.stderr


def truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n… truncated ({len(text) - limit} more characters); narrow the query with a namespace, selector or tail_lines"


def text_result(text: str, is_error: bool = False) -> Dict[str, Any]:
    return {"content": [{"type": "text", "text": text}], "isError": is_error}


def call_tool(name: str, args: Any, policy: Policy, runner: Runner) -> Dict[str, Any]:
    spec = TOOLS_BY_NAME[name]
    problem = validate_args(spec, args or {})
    if problem:
        return text_result(f"invalid arguments for {name}: {problem}", True)
    try:
        argv = kubectl_argv(spec, args or {}, policy)
    except PermissionError as exc:
        return text_result(f"refused: {exc}", True)
    code, out, err = runner(argv)
    if code != 0:
        return text_result(f"kubectl exited {code}: {err.strip() or out.strip()}", True)
    body = out.strip() or "(no results)"
    return text_result(f"$ {' '.join(argv)}\n\n{truncate(body, policy.output_limit)}")


def handle(message: Dict[str, Any], policy: Policy, runner: Runner) -> Optional[Dict[str, Any]]:
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
        return ok({
            "protocolVersion": version,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
            "instructions": "Read-only view of a Kubernetes cluster via kubectl. Start with list_events(warnings_only=true) and list_pods, then describe_pod and pod_logs for anything not Running/Ready. Nothing here can change the cluster.",
        })
    if method == "ping":
        return ok({})
    if method == "tools/list":
        return ok({"tools": [{"name": t.name, "description": t.description, "inputSchema": t.schema()} for t in TOOLS]})
    if method == "tools/call":
        name = params.get("name")
        if name not in TOOLS_BY_NAME:
            return err(-32602, f"Unknown tool: {name}")
        return ok(call_tool(name, params.get("arguments"), policy, runner))
    if msg_id is None:
        return None
    return err(-32601, f"Method not found: {method}")


def serve(stdin, stdout, policy: Policy, runner: Runner = subprocess_runner, log=lambda line: print(line, file=sys.stderr, flush=True)) -> int:
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
        response = handle(message, policy, runner)
        if response is not None:
            stdout.write(json.dumps(response) + "\n")
            stdout.flush()
    return 0


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="kubectl_mcp", description=__doc__)
    parser.add_argument("--allow-namespaces", help="comma-separated namespaces the agent may query (default: all)")
    parser.add_argument("--context", help="kubeconfig context to pin")
    parser.add_argument("--max-output", type=int, default=DEFAULT_OUTPUT_LIMIT, help="characters per tool result before truncation")
    parser.add_argument("--quiet", action="store_true", help="no request log on stderr")
    args = parser.parse_args(list(sys.argv[1:] if argv is None else argv))
    policy = Policy(
        namespaces=[ns.strip() for ns in args.allow_namespaces.split(",") if ns.strip()] if args.allow_namespaces else None,
        context=args.context,
        output_limit=args.max_output,
    )
    log = (lambda line: None) if args.quiet else (lambda line: print(f"[kubectl_mcp] {line}", file=sys.stderr, flush=True))
    return serve(sys.stdin, sys.stdout, policy, subprocess_runner, log)


if __name__ == "__main__":
    raise SystemExit(main())
