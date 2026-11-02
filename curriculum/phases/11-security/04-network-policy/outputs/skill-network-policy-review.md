---
name: network-policy-review
description: Review a NetworkPolicy change or a namespace's isolation by confirming enforcement, linting the policies, computing the connectivity matrix before and after, listing every connection the change opens or closes, and proving the result with probes from inside the pods
version: 1.0.0
phase: security
lesson: security/network-policy
tags: [kubernetes, networkpolicy, security, zero-trust, cni, kindnet, cilium, calico, dns, review]
---

# NetworkPolicy review

Use this for a pull request that adds or changes NetworkPolicy objects, before rolling out a default deny in a namespace, after an incident where a pod reached something it should not have, or when a team reports "the network broke" after a policy change. Work on copies of the objects; apply nothing to the cluster until step 6.

## 1. Is anything enforcing policies?

```bash
kubectl -n kube-system get daemonsets -o custom-columns=NAME:.metadata.name,IMAGE:.spec.template.spec.containers[0].image
kubectl get crd | grep -E 'cilium|projectcalico|policy.networking.k8s.io' || true
```

| What you see | Meaning | Next |
|---|---|---|
| `kindnet` from kind v0.24.0 or later | kube-network-policies enforces standard NetworkPolicy (not ClusterNetworkPolicy) | continue |
| `cilium` or `calico-node` | enforced; their own CRDs may add deny rules and cluster-wide policies | also list `ciliumnetworkpolicies`/`ciliumclusterwidenetworkpolicies` or Calico `globalnetworkpolicies`; they change the answer |
| a managed plugin (AWS VPC CNI, Azure CNI, GKE) | enforcement depends on a setting (network policy agent, Azure NPM or Cilium, GKE Dataplane V2 or network policy enforcement) | confirm that setting is on before trusting any policy |
| `flannel` alone, or an old kindnet | NetworkPolicy objects are stored and ignored | stop: report "not enforced"; the fix is a plugin that enforces, on a new cluster or by a planned migration |

If in doubt, prove it with a canary in a scratch namespace: a default-deny policy and a probe that must fail (step 6).

## 2. Dump the state

```bash
NS=<namespace>
kubectl get pods,networkpolicies -A -o json > live.json
kubectl get namespaces -o json > namespaces.json
kubectl -n $NS get networkpolicy -o yaml > current.yaml          # read back the defaulted policyTypes
kubectl -n $NS get pods --show-labels -o wide
```

Convert the proposed manifests to JSON (`kubectl apply --dry-run=server -f proposed.yaml -o json > proposed.json` also validates them and shows the server's defaults) and keep them separate from `live.json`.

## 3. Lint

```bash
python3 mininetpol.py lint -f live.json -f namespaces.json -f proposed.json
```

| Finding | Why it matters | Fix |
|---|---|---|
| `egress-not-isolated` | `egress: []` without `policyTypes` defaults to Ingress only; egress stays open | `policyTypes: [Ingress, Egress]` |
| `or-peers` | a `namespaceSelector` and a `podSelector` in two elements allow either | one element if AND was meant |
| `peer-selects-nothing` | a typo, or a `name` label no namespace carries | select namespaces by `kubernetes.io/metadata.name` |
| `named-port-missing` | the rule matches nothing; traffic you meant to allow stays blocked | use the container port name the destination declares, or the number |
| `ipblock-pods` | pod IPs inside an `ipBlock` match on some plugins and not on others | select pods with selectors; keep `ipBlock` for outside addresses |
| `dns-egress` | egress-isolated pods cannot resolve names; failures look like timeouts | allow 53/UDP and 53/TCP to `k8s-app=kube-dns` in `kube-system` |
| `selects-nothing` | the policy does nothing today and something unexpected tomorrow | fix the selector or delete the policy |
| `ignored-rules`, `podselector-missing`, `allow-all` | the policy says something other than it reads | rewrite it explicitly |

Any warning blocks the review until it is fixed or explained in the pull request.

## 4. Matrix before and after

```bash
SCOPE="--namespace $NS"                                   # add every namespace whose pods talk to $NS
PORTS="--port 8080 --port 9090"                           # the ports the workloads actually listen on
python3 mininetpol.py matrix -f live.json -f namespaces.json $SCOPE $PORTS --dns --json > before.json
python3 mininetpol.py matrix -f live.json -f namespaces.json -f proposed.json $SCOPE $PORTS --dns --json > after.json
python3 - before.json after.json <<'PY'
import json, sys
before, after = (json.load(open(p)) for p in sys.argv[1:3])
key = lambda c: f"{c['from']} -> {c['to']} {c['port']}/{c['protocol']}"
was = {key(c): c["state"] for c in before["cells"]}
for cell in after["cells"]:
    old = was.get(key(cell))
    if old != cell["state"]:
        print(f"{old} -> {cell['state']}: {key(cell)}  ({'; '.join(cell['reasons'])})")
PY
```

Add `--external NAME=IP:PORT` for the outside addresses that matter (databases outside the cluster, the cloud metadata address `169.254.169.254:80`, payment providers).

## 5. Check the connections people forget

| Connection | Who needs it | Typical rule |
|---|---|---|
| DNS | every egress-isolated pod | egress to `k8s-app=kube-dns` in `kube-system`, 53/UDP and 53/TCP |
| metrics scrapes | Prometheus in the monitoring namespace | ingress from that namespace on the metrics port only |
| ingress controller or gateway | pods behind an Ingress or HTTPRoute | ingress from the controller's namespace and pods on the service's target port |
| admission webhooks, aggregated APIs | the API server, often from node or control-plane addresses | ingress from those addresses; test after the change, a blocked webhook fails requests cluster-wide |
| egress to cloud APIs and the metadata address | pods using cloud identity | allow only what is needed; deny metadata where workload identity replaces it |
| health checks from the node | kubelet probes | always allowed from the pod's own node; nothing to add |

Any connection in the "newly blocked" list from step 4 that appears here must be added or justified.

## 6. Prove it on the cluster

Apply allow policies before the default deny, so nothing is cut off between the two. Then measure from inside the real pods:

```bash
kubectl apply -f proposed.yaml
kubectl get pods,networkpolicies -A -o json > live.json
python3 mininetpol.py matrix -f live.json -f namespaces.json $SCOPE $PORTS --dns --json > predicted.json
for src in $(python3 -c 'import json; print(" ".join(p["key"] for p in json.load(open("predicted.json"))["pods"]))'); do
  kubectl -n ${src%/*} exec -i ${src#*/} -- python3 /tmp/mininetpol.py probe --from $src - < predicted.json > measured-${src#*/}.json
done
python3 mininetpol.py compare predicted.json measured-*.json
```

Copy `mininetpol.py` into the pods first (`kubectl cp`) or use `kubectl debug POD -it --image=busybox:1.37 -- nc -zv -w 2 IP PORT` for single checks. Enforcement is asynchronous: repeat the measurement for up to a minute before calling a difference real. A `timeout` is a drop; `refused` means nothing listens, which is a workload problem, not a policy one.

| `compare` says | Meaning | Action |
|---|---|---|
| identical | the cluster enforces what the objects say | done |
| predicted allowed, measured timeout | another layer drops it: a Cilium or Calico policy, a cluster-wide policy, a cloud security group, or enforcement still converging | wait, then list the plugin's own policy CRDs |
| predicted blocked, measured connected | not enforced, or the plugin matches differently (`ipBlock` on pod IPs, traffic from the node) | stop the rollout; return to step 1 |
| `partial` for DNS | the rule matches only some CoreDNS pods | select by `k8s-app=kube-dns`, never by pod IP |

## 7. Report

State: the network plugin and how enforcement was confirmed; every lint warning and how it was resolved; the connections the change opens and closes (from step 4), each with the policy and rule responsible; the forgotten-connection checks from step 5; the `compare` result after the rollout with the time it took to converge; and the rollback (the exact `kubectl delete networkpolicy` commands). Never report "applied successfully" as evidence that a policy works.
