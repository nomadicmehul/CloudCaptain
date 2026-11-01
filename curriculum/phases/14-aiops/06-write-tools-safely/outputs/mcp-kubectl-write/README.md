# mcp-kubectl-write

A Model Context Protocol server for `kubectl` writes that are two calls apart. Five actions (`scale`, `rollout_restart`, `cordon`, `uncordon`, `delete_pod`), each as a `plan_<action>` tool that changes nothing and a `<action>` tool that needs the plan's confirm phrase. Protected namespaces, a namespace allow-list, a node allow-list, a replica cap, a per-minute write limit, a TTL on every phrase, idempotent replays and an append-only audit with the state before and after.

The server is the lesson's `code/main.py` (zero dependencies, Python 3.9+). Copy it next to this README as `server.py`, or point the commands below at the lesson folder. It complements `mcp-kubectl-readonly` from the first lesson of this phase: run both, and the agent reads through one and writes through the other.

## The two-step protocol

1. `plan_scale(namespace, deployment, replicas)`: the server checks scope, reads the deployment, runs `kubectl scale ... --dry-run=server`, computes the blast radius and returns the exact command, the dry-run output, the state before, the radius, the undo command and a confirm phrase such as `scale deployment default/web from 2 to 3 [3f9a1c2e]`. Valid for `--ttl` seconds, single use.
2. `scale(namespace, deployment, replicas, confirm)`: refused unless the phrase was issued by this server for exactly these arguments, is not expired, and the per-minute limit is not reached. Runs the command once, reads the state again, appends an audit line and returns `EXECUTED` with before, after and undo. The same phrase a second time returns the first result marked `REPLAYED`; nothing runs again.

A plan whose dry run fails (NotFound, Forbidden, a denied admission policy) issues no phrase and says why. A write that fails spends its phrase; plan again.

## Run it by hand

```bash
printf '%s\n' \
  '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"me","version":"1"}}}' \
  '{"jsonrpc":"2.0","method":"notifications/initialized"}' \
  '{"jsonrpc":"2.0","id":2,"method":"tools/list"}' \
  '{"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"plan_scale","arguments":{"namespace":"default","deployment":"web","replicas":3}}}' \
  | python3 server.py --allow-write --allow-namespaces default --context kind-cc-lab --audit ./audit.jsonl --quiet
```

The plan's result carries the phrase in its text and in `structuredContent.plan.confirm`. To complete the second step interactively, use the Inspector: `npx @modelcontextprotocol/inspector python3 server.py --allow-write --allow-namespaces default`, call `plan_scale`, copy the phrase into `scale`, then call `scale` again to see the replay. `python3 server.py --list-tools [--allow-write]` prints the tool surface without a session.

## Install into an agent

Claude Code, with the client's approval prompt layered on top of the server's gates:

```bash
claude mcp add kubectl-write -- python3 /absolute/path/server.py --allow-write --allow-namespaces default,dev --context kind-cc-lab --audit "$HOME/kubectl-write-audit.jsonl"
```

Permission rules in `.claude/settings.json` let plans run freely, ask before every execute, and deny the tools you do not want this agent to have:

```json
{
  "permissions": {
    "allow": ["mcp__kubectl-write__plan_scale", "mcp__kubectl-write__plan_rollout_restart"],
    "ask": ["mcp__kubectl-write__scale", "mcp__kubectl-write__rollout_restart"],
    "deny": ["mcp__kubectl-write__delete_pod", "mcp__kubectl-write__cordon", "mcp__kubectl-write__uncordon"]
  }
}
```

Cursor (`.cursor/mcp.json`) or any client that takes the standard config shape: see `mcp.json` in this folder. Without `--allow-write` the server exposes only the five `plan_*` tools, which is the right configuration for an agent that proposes changes for a human to run.

## Flags

| Flag | Default | Purpose |
|---|---|---|
| `--allow-write` | off | expose the five execute tools; without it they are absent from `tools/list` |
| `--allow-namespaces a,b` | any non-protected | refuse writes to any other namespace |
| `--protect a,b` | `kube-system,kube-public,kube-node-lease` | namespaces no write tool may touch, even if allow-listed |
| `--allow-nodes a,b` | none | nodes `cordon` and `uncordon` may target; empty means no node |
| `--context NAME` | current | pin a kubeconfig context on every command |
| `--ttl SECONDS` | 120 | how long a confirm phrase stays valid |
| `--max-per-minute N` | 5 | writes the server executes per sliding minute; replays do not count |
| `--max-replicas N` | 10 | largest replica count `scale` may plan; advertised as `maximum` in the schema |
| `--audit PATH` | `kubectl-write-audit.jsonl` | append-only JSONL, one line per plan, execution, replay and refusal |
| `--list-tools` | | print the tool surface for this configuration and exit |
| `--quiet` | off | no request log on stderr |

## The audit line

```json
{"at":"2026-11-01T14:02:11Z","event":"execute","tool":"scale","arguments":{"namespace":"default","deployment":"web","replicas":3},"confirm":"scale deployment default/web from 2 to 3 [3f9a1c2e]","command":["kubectl","--context","kind-cc-lab","scale","deployment/web","-n","default","--replicas=3"],"exit_code":0,"output":"deployment.apps/web scaled","before":{"replicas":2,"ready":2,"available":2,"generation":4,"max_unavailable":"25%"},"after":{"replicas":3,"ready":2,"available":2,"generation":4,"max_unavailable":"25%"},"undo":["kubectl","--context","kind-cc-lab","scale","deployment/web","-n","default","--replicas=2"]}
```

Events: `plan`, `execute`, `replayed`, `refused` (with `reason`), `expired`, `rate_limited`, `dry_run_failed`. `jq -r 'select(.event=="execute") | [.at, .confirm, .before.replicas, .after.replicas] | @tsv' audit.jsonl` answers "what did the agent change and from what".

## What it will never do

Run a write without a phrase this process issued for exactly those arguments. Run the same phrase twice. Run a phrase after its TTL. Touch a protected namespace, a namespace outside the allow-list, or a node not named in `--allow-nodes`. Plan a scale above `--max-replicas`. Exceed `--max-per-minute` real writes. Accept a free-form command, a kubeconfig, a token or a server address as an argument. Return a result before its audit line is on disk. Expose an execute tool when started without `--allow-write`.

What it does not do, and the client or the cluster must: decide whether the change is a good idea (the approval prompt), bind humans with `kubectl` in their hands (RBAC and `ValidatingAdmissionPolicy`), and keep plans across a restart of the server (plans live in memory; the lesson's Hard exercise signs them with HMAC).
