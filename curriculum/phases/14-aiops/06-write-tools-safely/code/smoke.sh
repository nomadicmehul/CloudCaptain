#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."

fail() { echo "SMOKE FAIL: $*" >&2; exit 1; }
WORK="$(mktemp -d)"
CTX_ARGS=()
CLUSTER_DEPLOY=""
cleanup() {
  if [[ -n "$CLUSTER_DEPLOY" ]]; then
    kubectl ${CTX_ARGS[@]+"${CTX_ARGS[@]}"} delete deployment "$CLUSTER_DEPLOY" -n default --ignore-not-found --wait=false >/dev/null 2>&1 || true
  fi
  rm -rf "$WORK"
}
trap cleanup EXIT

mkdir -p "$WORK/bin" "$WORK/state"
echo 2 > "$WORK/state/replicas"
export CC_FAKE_STATE="$WORK/state"
cat > "$WORK/bin/kubectl" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
STATE="${CC_FAKE_STATE:?}"
echo "$*" >> "$STATE/calls.log"
args=("$@")
if [[ "${args[0]:-}" == "--context" ]]; then args=("${args[@]:2}"); fi
dry=""
for a in "${args[@]}"; do [[ "$a" == "--dry-run=server" ]] && dry=" (server dry run)"; done
case "${args[0]:-} ${args[1]:-}" in
  "get deployment")
    if [[ "${args[2]}" != "web" ]]; then echo "Error from server (NotFound): deployments.apps \"${args[2]}\" not found" >&2; exit 1; fi
    r="$(cat "$STATE/replicas")"
    printf '{"metadata":{"name":"web","generation":4},"spec":{"replicas":%s,"strategy":{"type":"RollingUpdate","rollingUpdate":{"maxUnavailable":"25%%"}}},"status":{"readyReplicas":%s,"availableReplicas":%s}}\n' "$r" "$r" "$r"
    ;;
  "scale deployment/web")
    if [[ -z "$dry" ]]; then
      for a in "${args[@]}"; do [[ "$a" == --replicas=* ]] && echo "${a#--replicas=}" > "$STATE/replicas"; done
    fi
    echo "deployment.apps/web scaled$dry"
    ;;
  "rollout restart")
    echo "deployment.apps/web restarted$dry"
    ;;
  *)
    echo "fake kubectl: unsupported: $*" >&2
    exit 1
    ;;
esac
EOF
chmod +x "$WORK/bin/kubectl"

cat > "$WORK/drive.py" <<'EOF'
import json
import os
import subprocess
import sys
import time


class Client:
    def __init__(self, server_args):
        self.proc = subprocess.Popen([sys.executable, "code/main.py", *server_args], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, bufsize=1)
        self.n = 0

    def send(self, message):
        self.proc.stdin.write(json.dumps(message) + "\n")
        self.proc.stdin.flush()

    def request(self, method, params=None):
        self.n += 1
        message = {"jsonrpc": "2.0", "id": self.n, "method": method}
        if params is not None:
            message["params"] = params
        self.send(message)
        while True:
            line = self.proc.stdout.readline()
            if not line:
                raise SystemExit("SMOKE FAIL: server exited while answering " + method)
            reply = json.loads(line)
            if reply.get("id") == self.n:
                return reply

    def call(self, name, **arguments):
        return self.request("tools/call", {"name": name, "arguments": arguments})

    def close(self):
        self.proc.stdin.close()
        self.proc.wait(timeout=10)


def text(reply):
    return reply["result"]["content"][0]["text"]


def plan_of(reply):
    assert not reply["result"]["isError"], text(reply)
    return reply["result"]["structuredContent"]["plan"]


scenario, server_args = sys.argv[1], sys.argv[2:]
client = Client(server_args)
client.request("initialize", {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "smoke", "version": "1"}})
client.send({"jsonrpc": "2.0", "method": "notifications/initialized"})
tools = [t["name"] for t in client.request("tools/list")["result"]["tools"]]

if scenario == "fake":
    assert len(tools) == 10, tools
    plan = plan_of(client.call("plan_scale", namespace="default", deployment="web", replicas=3))
    assert plan["before"]["replicas"] == 2 and plan["blast"]["percent"] == 50.0, plan
    assert "server dry run" in plan["dry_run"], plan
    phrase = plan["confirm"]
    wrong = client.call("scale", namespace="default", deployment="web", replicas=3, confirm=phrase.replace("[", "[x"))
    assert wrong["result"]["isError"] and "no plan matches" in text(wrong), text(wrong)
    other = client.call("scale", namespace="default", deployment="web", replicas=4, confirm=phrase)
    assert other["result"]["isError"] and "belongs to" in text(other), text(other)
    done = client.call("scale", namespace="default", deployment="web", replicas=3, confirm=phrase)
    assert not done["result"]["isError"], text(done)
    execution = done["result"]["structuredContent"]["execution"]
    assert (execution["before"]["replicas"], execution["after"]["replicas"]) == (2, 3), execution
    again = client.call("scale", namespace="default", deployment="web", replicas=3, confirm=phrase)
    assert again["result"]["structuredContent"].get("replayed") is True and text(again).startswith("REPLAYED"), text(again)
    protected = client.call("plan_scale", namespace="kube-system", deployment="coredns", replicas=0)
    assert protected["result"]["isError"] and "protected" in text(protected), text(protected)
    node = client.call("plan_cordon", node="worker-1")
    assert node["result"]["isError"] and "--allow-nodes" in text(node), text(node)
    missing = client.call("plan_rollout_restart", namespace="default", deployment="ghost")
    assert missing["result"]["isError"] and "NotFound" in text(missing), text(missing)
    calls = open(os.path.join(os.environ["CC_FAKE_STATE"], "calls.log"), encoding="utf-8").read().splitlines()
    real_scales = [c for c in calls if c.startswith("scale") and "--dry-run=server" not in c]
    assert len(real_scales) == 1, real_scales
    print("  plan -> execute -> replay; wrong phrase, other arguments, protected namespace, node allow-list and missing target refused; the fake kubectl saw exactly one real write")
elif scenario == "ttl":
    phrase = plan_of(client.call("plan_scale", namespace="default", deployment="web", replicas=3))["confirm"]
    time.sleep(1.6)
    late = client.call("scale", namespace="default", deployment="web", replicas=3, confirm=phrase)
    assert late["result"]["isError"] and "expired" in text(late), text(late)
    print("  " + text(late).splitlines()[0])
elif scenario == "plans-only":
    assert tools == ["plan_scale", "plan_rollout_restart", "plan_cordon", "plan_uncordon", "plan_delete_pod"], tools
    reply = client.request("tools/call", {"name": "scale", "arguments": {"namespace": "default", "deployment": "web", "replicas": 3, "confirm": "x"}})
    assert reply.get("error", {}).get("code") == -32602 and "--allow-write" in reply["error"]["message"], reply
    print("  without --allow-write only the five plan tools exist; scale is an unknown tool")
elif scenario in ("cluster-up", "cluster-down"):
    name = os.environ["CC_DEPLOY"]
    target = 3 if scenario == "cluster-up" else 2
    plan = plan_of(client.call("plan_scale", namespace="default", deployment=name, replicas=target))
    assert plan["before"]["replicas"] == (2 if target == 3 else 3), plan
    assert "scaled" in plan["dry_run"], plan
    done = client.call("scale", namespace="default", deployment=name, replicas=target, confirm=plan["confirm"])
    assert not done["result"]["isError"], text(done)
    execution = done["result"]["structuredContent"]["execution"]
    assert execution["after"]["replicas"] == target, execution
    print("  real cluster: %s -> %s; dry run said: %s" % (plan["before"]["replicas"], target, plan["dry_run"]))
else:
    raise SystemExit("SMOKE FAIL: unknown scenario " + scenario)
client.close()
EOF

echo "▸ tool surface with and without --allow-write"
python3 code/main.py --list-tools --audit "$WORK/unused.jsonl" | python3 -c 'import json,sys; t=[x["name"] for x in json.load(sys.stdin)]; assert t==["plan_scale","plan_rollout_restart","plan_cordon","plan_uncordon","plan_delete_pod"], t; print("  plans only:", len(t), "tools")' || fail "plans-only listing"
python3 code/main.py --list-tools --allow-write --audit "$WORK/unused.jsonl" | python3 -c 'import json,sys; t=json.load(sys.stdin); assert len(t)==10, len(t); w={x["name"]: x for x in t}; assert w["delete_pod"]["annotations"]["destructiveHint"] is True; assert w["plan_scale"]["annotations"]["readOnlyHint"] is True; print("  with --allow-write:", len(t), "tools, annotations present")' || fail "write listing"

REAL_PATH="$PATH"
export PATH="$WORK/bin:$PATH"

echo "▸ two-step protocol over stdio against a fake kubectl: plan, confirm, execute, replay, refusals"
python3 "$WORK/drive.py" fake --allow-write --audit "$WORK/audit.jsonl" --quiet || fail "fake kubectl session"

echo "▸ an expired confirm phrase is refused (TTL 1s)"
python3 "$WORK/drive.py" ttl --allow-write --ttl 1 --audit "$WORK/audit.jsonl" --quiet || fail "ttl session"

echo "▸ without --allow-write the execute tools do not exist"
python3 "$WORK/drive.py" plans-only --audit "$WORK/audit.jsonl" --quiet || fail "plans-only session"

echo "▸ the audit file has one JSON line per event, with before and after on the execution"
python3 - "$WORK/audit.jsonl" <<'EOF' || fail "audit"
import json, sys
entries = [json.loads(line) for line in open(sys.argv[1], encoding="utf-8") if line.strip()]
events = [e["event"] for e in entries]
for wanted in ("plan", "refused", "execute", "replayed", "expired"):
    assert wanted in events, (wanted, events)
execution = next(e for e in entries if e["event"] == "execute")
assert (execution["before"]["replicas"], execution["after"]["replicas"]) == (2, 3), execution
assert execution["command"][-1] == "--replicas=3" and "--dry-run=server" not in execution["command"], execution
print("  %d audit lines: %s" % (len(entries), ", ".join(sorted(set(events)))))
EOF

export PATH="$REAL_PATH"
if command -v kubectl >/dev/null && kubectl cluster-info >/dev/null 2>&1; then
  if kubectl config get-contexts -o name 2>/dev/null | grep -qx kind-cc-smoke; then CTX_ARGS=(--context kind-cc-smoke); fi
  echo "▸ Use It against the real cluster: plan a scale with --dry-run=server, execute with the phrase, scale back"
  CLUSTER_DEPLOY="cc-smoke-web"
  kubectl ${CTX_ARGS[@]+"${CTX_ARGS[@]}"} delete deployment "$CLUSTER_DEPLOY" -n default --ignore-not-found --wait=true >/dev/null 2>&1 || true
  kubectl ${CTX_ARGS[@]+"${CTX_ARGS[@]}"} create deployment "$CLUSTER_DEPLOY" --image=registry.k8s.io/pause:3.10 --replicas=2 -n default >/dev/null || fail "create deployment"
  kubectl ${CTX_ARGS[@]+"${CTX_ARGS[@]}"} rollout status deployment/"$CLUSTER_DEPLOY" -n default --timeout=180s >/dev/null || fail "deployment did not become ready"
  CC_DEPLOY="$CLUSTER_DEPLOY" python3 "$WORK/drive.py" cluster-up --allow-write --allow-namespaces default ${CTX_ARGS[@]+"${CTX_ARGS[@]}"} --audit "$WORK/cluster-audit.jsonl" --quiet || fail "cluster scale up"
  REPLICAS="$(kubectl ${CTX_ARGS[@]+"${CTX_ARGS[@]}"} get deployment "$CLUSTER_DEPLOY" -n default -o jsonpath='{.spec.replicas}')"
  [[ "$REPLICAS" == "3" ]] || fail "expected 3 replicas after the executed plan, got $REPLICAS"
  CC_DEPLOY="$CLUSTER_DEPLOY" python3 "$WORK/drive.py" cluster-down --allow-write --allow-namespaces default ${CTX_ARGS[@]+"${CTX_ARGS[@]}"} --audit "$WORK/cluster-audit.jsonl" --quiet || fail "cluster scale down"
  REPLICAS="$(kubectl ${CTX_ARGS[@]+"${CTX_ARGS[@]}"} get deployment "$CLUSTER_DEPLOY" -n default -o jsonpath='{.spec.replicas}')"
  [[ "$REPLICAS" == "2" ]] || fail "expected 2 replicas after scaling back, got $REPLICAS"
  python3 -c '
import json, sys
entries = [json.loads(l) for l in open(sys.argv[1], encoding="utf-8") if l.strip()]
executes = [e for e in entries if e["event"] == "execute"]
assert [(e["before"]["replicas"], e["after"]["replicas"]) for e in executes] == [(2, 3), (3, 2)], executes
assert all("kind-cc-smoke" in " ".join(e["command"]) or "--context" not in e["command"] for e in executes)
print("  cluster audit: %d lines, executions %s" % (len(entries), [(e["before"]["replicas"], e["after"]["replicas"]) for e in executes]))
' "$WORK/cluster-audit.jsonl" || fail "cluster audit"
  kubectl ${CTX_ARGS[@]+"${CTX_ARGS[@]}"} delete deployment "$CLUSTER_DEPLOY" -n default --wait=false >/dev/null || fail "delete deployment"
  CLUSTER_DEPLOY=""
else
  echo "  (no reachable cluster; real-cluster step skipped)"
fi

echo "SMOKE OK: aiops/write-tools-safely"
