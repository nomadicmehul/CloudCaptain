# mcp-kubectl-readonly

A read-only Model Context Protocol server for `kubectl`: four tools (`list_pods`, `describe_pod`, `pod_logs`, `list_events`), a namespace allow-list, a pinned kubeconfig context, bounded output, and a structural guarantee that nothing it runs can change the cluster.

The server is the lesson's `code/main.py` (zero dependencies, Python 3.9+). Copy it next to this README as `server.py`, or point the commands below at the lesson folder.

## Run it by hand

```bash
printf '%s\n' \
  '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"me","version":"1"}}}' \
  '{"jsonrpc":"2.0","method":"notifications/initialized"}' \
  '{"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"list_pods","arguments":{"namespace":"kube-system"}}}' \
  | python3 server.py --allow-namespaces kube-system,default
```

Or interactively with the MCP Inspector: `npx @modelcontextprotocol/inspector python3 server.py --allow-namespaces default`.

## Install into an agent

Claude Code:

```bash
claude mcp add kubectl-ro -- python3 /absolute/path/server.py --allow-namespaces default,dev --context kind-cc-lab
```

Cursor (`.cursor/mcp.json`) or any client that takes the standard config shape (`mcp.json` in this folder):

```json
{
  "mcpServers": {
    "kubectl-ro": {
      "command": "python3",
      "args": ["/absolute/path/server.py", "--allow-namespaces", "default,dev", "--context", "kind-cc-lab"]
    }
  }
}
```

Then ask: *"Which pods in dev are not Ready, and what do the events and the last 50 log lines say?"* The agent will call `list_pods`, `list_events(warnings_only=true)`, then `describe_pod` and `pod_logs` for the suspects.

## Flags

| Flag | Default | Purpose |
|---|---|---|
| `--allow-namespaces a,b` | all | refuse any other namespace, including all-namespaces queries |
| `--context NAME` | current | pin a kubeconfig context so the agent cannot drift to another cluster |
| `--max-output N` | 12000 | characters per result before truncation with a narrowing hint |
| `--quiet` | off | no request log on stderr |

## What it will never do

Verbs outside `get`, `describe`, `logs`, `top`, `version`, `api-resources`, `explain` and the flags `--raw`, `--kubeconfig`, `--token`, `--server`, `--as`, `--as-group`, `--exec` are rejected before `kubectl` runs. There is no tool that takes a free-form command string, so a prompt injection in a log line cannot become a shell command. Secrets are not exposed by any tool; `describe_pod` shows env var names from the pod spec but not Secret values.
