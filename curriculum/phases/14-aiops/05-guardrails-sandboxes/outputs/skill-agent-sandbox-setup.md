---
name: agent-sandbox-setup
description: Put an ops agent into a sandbox before it runs unattended - a short-lived read-only credential, a hardened container on an internal network, an egress proxy with a host allow-list and an audit - then prove each property from inside and gate on drift
version: 1.0.0
phase: aiops
lesson: aiops/guardrails-sandboxes
tags: [aiops, agents, sandbox, egress, proxy, docker, kubernetes, security, least-privilege]
---

# Agent sandbox setup

Use this before an agent (yours, a vendor's, or one a teammate wrote) runs on a schedule, in CI, or anywhere its input includes logs, events, tickets or web pages. It assumes prompt injection will eventually succeed and makes the outcome boring: the agent can only read what the job needs, can only reach the hosts the job needs, and every attempt to go elsewhere is refused and recorded. The commands use `agentbox` (`main.py`) and `main.sh` from the lesson; the same steps apply with Squid or Envoy as the proxy and NetworkPolicy instead of Docker networks.

## 1. Inventory what the agent needs

Write three lists before touching any config. If you cannot fill one, run the agent once in a throwaway environment behind the proxy with `--allow '*'` and read the audit's `hosts`: that is the set it actually used, not the set you guessed.

| List | Questions | Example for the triage agent |
|---|---|---|
| Reads | Which files, which cluster resources, which cloud APIs? | pods, events, logs in two namespaces; no Secrets |
| Executes | Which binaries and subprocesses? | `python3`, `kubectl` through the MCP server |
| Talks to | Which hosts and ports, at run time (not build time)? | `api.anthropic.com:443`, the API server `:6443` |

Anything needed only to build (package registries, Git hosts) goes into the image at build time and stays out of the run-time allow-list.

## 2. Mint the credential

```bash
kubectl create serviceaccount triage-ro -n triage
kubectl create rolebinding triage-ro-view -n default --clusterrole=view --serviceaccount=triage:triage-ro
kubectl auth can-i --list --as=system:serviceaccount:triage:triage-ro -n default | head -20
kubectl auth can-i get secrets --as=system:serviceaccount:triage:triage-ro -n default      # must be "no"
TOKEN=$(kubectl create token triage-ro -n triage --duration=1h)
```

Build a separate kubeconfig with only this cluster, this user and this token (`kubectl --kubeconfig FILE config set-cluster/set-credentials/set-context`). Never mount `~/.kube`, `~/.aws`, `~/.config/gcloud`, `~/.ssh` or `/var/run/docker.sock`. For cloud APIs use workload identity or a role assumed for minutes, not static keys. Model API keys go in a secret store or a hosted vault, not in `-e`.

## 3. Write the policy

```json
{
  "name": "triage",
  "image": "registry.example.com/triage-agent:1.4.2",
  "allowed_hosts": ["api.anthropic.com", "cc-lab-control-plane:6443"],
  "kubeconfig": "/srv/agents/triage/kubeconfig",
  "tmpfs": ["/tmp"],
  "limits": {"pids": 64, "memory": "256m", "cpus": "1"},
  "user": "1000"
}
```

Rules for `allowed_hosts`: exact names first; `*.domain` only when the provider really uses many subdomains; pin the port when it is not 443; never `*`. Every host that accepts writes (Git hosting, chat webhooks, object storage, paste sites) is an exfiltration channel if the agent holds a token for it: allow it only with a token that cannot write, or not at all.

## 4. Bring it up

```bash
python3 main.py render --policy policy.json -- python3 /agent/main.py "checkout is crashlooping"   # review the exact argv first
bash main.sh up --name triage --allow api.anthropic.com --allow cc-lab-control-plane:6443
docker network connect kind triage-proxy                                                          # only if the API server lives on another Docker network
bash main.sh run --name triage --image registry.example.com/triage-agent:1.4.2 --kubeconfig /srv/agents/triage/kubeconfig --detach -- python3 /agent/main.py "..."
```

In Kubernetes: the agent pod with `runAsNonRoot`, `allowPrivilegeEscalation: false`, `capabilities.drop: [ALL]`, `readOnlyRootFilesystem: true`, `seccompProfile.type: RuntimeDefault`, resource limits, `HTTPS_PROXY` pointing at an egress-proxy Service, and an egress NetworkPolicy that allows only the proxy pod and `kube-dns`. Add `runtimeClassName` for gVisor or Kata when the agent executes code it did not ship with.

## 5. Prove it from inside

Run each check and keep the output for the report. A property you did not test is a property you do not have.

| Property | Command | Pass |
|---|---|---|
| identity | `docker exec triage-agent id -u` | not 0 |
| capabilities | `docker exec triage-agent grep -E 'CapEff\|NoNewPrivs' /proc/self/status` | `CapEff: 0000000000000000`, `NoNewPrivs: 1` |
| filesystem | `docker exec triage-agent touch /etc/x` and `touch /tmp/x` | first fails read-only, second works |
| allowed egress | `docker exec triage-agent curl -s -o /dev/null -w '%{http_code}' https://api.anthropic.com/v1/models` | any HTTP status (401 without a key) |
| denied egress | `docker exec triage-agent curl -s -o /dev/null -w '%{http_connect}' https://example.com/` | `403` |
| no bypass | `docker exec triage-agent curl --noproxy '*' --max-time 5 https://example.com/` | fails: no route |
| credential scope | `kubectl auth can-i delete pods --as=system:serviceaccount:triage:triage-ro` | `no` |

## 6. Gate on drift

```bash
python3 main.py check --policy policy.json --container triage-agent --json   # exit 0 no drift, 1 drift, 2 error
bash main.sh audit --name triage | python3 -m json.tool                       # hosts, allowed/denied, bytes
```

Run `check` after every deploy and on a schedule; alert on exit 1. Ship the proxy audit to the same store as the agent's trace, and alert on any denied request (an injection attempt or a missing dependency, both worth a human) and on bytes out to an allowed host far above its usual volume.

## 7. Decision table

| Symptom | Likely cause | Fix |
|---|---|---|
| curl works, the agent's HTTP client times out | the client ignores proxy variables (raw sockets, a library with its own config) | configure the proxy in the client; the internal network keeps it contained meanwhile |
| kubectl `Unable to connect to the server`, audit shows 403 for the API server | API server host:port not in the allow-list | add exactly `NAME:6443`; check the port in the kubeconfig |
| kubectl `Unable to connect`, audit shows 502 | the proxy cannot resolve or reach the API server | connect the proxy to the API server's network (`docker network connect kind ...`) |
| `x509: certificate is valid for ..., not ...` | the kubeconfig server name is not in the API server certificate | use the internal name (`kind get kubeconfig --internal`) or a SAN the cert has |
| `permission denied` on the kubeconfig | file is mode 600 for a different uid | run with `--user "$(id -u)"` or give the file to the container's uid |
| `Read-only file system` at startup | the app writes to `$HOME` or a cache dir | add a tmpfs for that path to the policy and set `HOME`; do not drop `--read-only` |
| `pip install` or `npm install` denied at run time | dependencies fetched at start | install them in the image; never allow registries at run time |
| NetworkPolicy applied, egress still open | the CNI does not enforce NetworkPolicy | use a CNI that does (Calico, Cilium); re-test by trying to get past it |
| repeated 403s to one unfamiliar host | injection attempt, or a dependency you missed | read the agent trace around the attempt before allowing anything |

## 8. Report

State: the agent and image digest; the three inventory lists; the credential (ServiceAccount, role, expiry) and every mount with its mode; the policy file; the result of each check in section 5 with the command output; `check` exit code and findings; the audit summary (hosts, allowed, denied, bytes out per host) for one representative run; and what the sandbox does not cover for this agent (data it may read and send to the model provider, write-capable allowed hosts, readable ConfigMaps or logs that hold secrets). Do not write "sandboxed" without the section 5 table.
