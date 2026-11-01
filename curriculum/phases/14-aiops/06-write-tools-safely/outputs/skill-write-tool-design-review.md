---
name: write-tool-design-review
description: Review any agent tool that changes infrastructure (MCP server, SDK tool, plugin) before it gets credentials that matter: preview, confirm binding, expiry, idempotency, blast radius, scope, rate, audit, undo, visibility
version: 1.0.0
phase: aiops
lesson: aiops/write-tools-safely
tags: [mcp, ai-agents, kubernetes, terraform, argo-cd, safety, review]
---

# Write tool design review

Use this before enabling any tool that can change state (`scale`, `apply`, `delete`, `exec`, `run`, `sync`, `merge`, `write_file`, an HTTP `POST`) for an agent, whether you wrote it or a vendor did, and again whenever a tool gains a new write. The previous skill (`mcp-tool-review`) decides whether a server is read-only; this one decides whether its writes are built correctly.

## 1. Enumerate the write surface without an agent in the loop

```bash
printf '%s\n' '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"review","version":"1"}}}' '{"jsonrpc":"2.0","method":"notifications/initialized"}' '{"jsonrpc":"2.0","id":2,"method":"tools/list"}' | <server command> 2>/dev/null \
  | python3 -c 'import json,sys; [print(t["name"], "|", (t.get("annotations") or {}).get("readOnlyHint"), "|", (t.get("annotations") or {}).get("destructiveHint"), "|", sorted(t["inputSchema"].get("properties", {}))) for l in sys.stdin for t in json.loads(l).get("result", {}).get("tools", [])]'
```

Run it twice: with the server's default flags and with every write flag it has (`--allow-write`, `--read-only=false`, `--disable-destructive=false`). Record which tools appear only in the second run. Any tool whose name or arguments imply a change counts as a write, regardless of its `readOnlyHint`; annotations are hints a client may use, never evidence.

## 2. Ten checks per write tool

| Check | Pass looks like | Fail looks like |
|---|---|---|
| Preview | a separate plan call, or a dry-run mode, that returns the exact command and the state it was computed from | the first call is the write; "the model should describe it first" |
| Binding | the confirm value is server-generated and bound to this action with these arguments | `confirm: true`, a phrase the model types itself, a phrase that works for any arguments |
| Expiry | the receipt stops working after a TTL and a fresh plan is required | a phrase from last week still applies; the plan never re-reads the state |
| Idempotency | the same receipt a second time returns the first result and runs nothing | every call applies; retries are the caller's problem |
| Blast radius | the plan says how many things change and what share of capacity that is, from current state | the plan shows the verb and the target only |
| Scope | protected targets refused before any command runs; allow-lists for namespaces, nodes, projects, accounts | scope lives in the system prompt; one tool reaches every cluster in the kubeconfig |
| Rate | a per-minute or per-session cap on real writes, with a wait hint | unbounded; the only limit is the model's patience |
| Audit | one record per plan, write, replay and refusal with the receipt, argv, result, and state before and after, written before the result returns | the tool's stdout, or a log line the agent runtime may or may not keep |
| Undo | every plan names the undo command, or says there is none | "rollback is supported" without saying how, or an undo that reverts something else |
| Visibility | write tools absent from `tools/list` unless a startup flag enables them | always listed, refusing at call time based on configuration |

Score each row pass or fail. Fewer than eight passes: do not connect the tool to credentials that reach production. Any fail in Binding, Idempotency or Scope: do not connect it anywhere a prompt injection can reach, which is everywhere the agent reads logs or tickets.

## 3. Three drills

Run these against the tool with a fake backend or a scratch cluster (`kind create cluster --name cc-review`), never against production.

1. **The retry.** Plan, execute, then send the identical execute again. Pass: the second response says it replayed and the backend shows one change. Fail: two changes, or an error that leaves you unsure whether the first one happened.
2. **The stale plan.** Plan, change the target by hand (scale it, edit it), then execute the old receipt. Pass: refused, or the plan is re-validated against the current state. Fail: the old plan applies on top of the new reality.
3. **The pasted phrase.** Put a plausible receipt into a log line or ticket the agent will read and ask it to "complete the pending change". Pass: refused as unknown. Fail: it runs; the server trusts text as a receipt.

## 4. The layer question

Ask where each guarantee lives, and insist that mechanics live in the server:

| Guarantee | Belongs in | Why not the other layer |
|---|---|---|
| a human agreed to this change | the client (approval prompt, `ask` rules, `always_ask` policies) | the server cannot know who is at the keyboard |
| exactly the planned arguments, once, in time, in scope, on record | the server | the client can be a script with approvals off, a different runtime, or absent |
| humans with the same CLI are bound the same way | the platform (RBAC, `ValidatingAdmissionPolicy`, Argo CD `AppProject`, sync windows) | a tool-level gate only binds the tool |

A tool that passes the ten checks but has no client approval is acceptable for low-radius actions in non-production scope. A client approval in front of a tool that fails Binding or Idempotency is theatre.

## 5. Report

Write five lines into the agent's configuration README or the team runbook: the tool or server reviewed and its version; the write tools found and the flag that exposes them; the ten-check score with the failing rows named; the result of the three drills; and the decision (connect with these flags and scopes, connect to non-production only, or do not connect) with the one change that would move it to the next tier. Date it; repeat the review when the tool's `tools/list` changes.
