---
name: service-connectivity-triage
description: Diagnose a Kubernetes Service that refuses, times out, answers from the wrong pods or resolves slowly, by checking the name, the Service, its EndpointSlices, the pod's port, the node's proxy rules and conntrack in the order a packet meets them
version: 1.0.0
phase: orchestration
lesson: orchestration/services-dns
tags: [kubernetes, services, endpointslices, kube-proxy, iptables, nftables, coredns, dns, debugging]
---

# Service connectivity triage

Use this when a client reports `connection refused`, a timeout, `could not resolve host`, "one request in three fails", or slow lookups against a Service, and the pods themselves look healthy.

## 0. Pin down the symptom first

Run the failing call from a pod in the client's namespace, not from your laptop, and keep the exact error:

```bash
NS=<client-namespace>; SVC=<service>; SNS=<service-namespace>
kubectl -n $NS run probe --image=alpine:3.22 --restart=Never -- sh -c "cat /etc/resolv.conf; getent hosts $SVC.$SNS; wget -T 3 -qO- http://$SVC.$SNS/ >/dev/null; echo exit=\$?"
kubectl -n $NS wait --for=jsonpath='{.status.phase}'=Succeeded pod/probe --timeout=60s; kubectl -n $NS logs probe; kubectl -n $NS delete pod probe
```

| Symptom | Layer to start at |
|---|---|
| `bad address`, `could not resolve`, NXDOMAIN | Step 1, the name |
| resolves, then `connection refused` within milliseconds | Step 3: no usable endpoints, so kube-proxy's REJECT answered |
| resolves, then hangs until a timeout | Step 4 or 6: the pod is not listening on the target port, a NetworkPolicy drops it, or the node's rules are stale |
| some requests fail, some work | Step 3 and 4: one endpoint is ready but broken (probe passes, port wrong), or slices differ from the rules |
| works by pod IP, fails by ClusterIP | Step 2 and 5: `targetPort` or the proxy |
| works from pods, fails from a node or from outside | Step 5: masquerade, NodePort, `externalTrafficPolicy` |
| slow everywhere, CoreDNS mostly NXDOMAIN | Step 1: `ndots` and the search path |
| `ping` to the ClusterIP fails | not a symptom: no interface owns a ClusterIP in iptables or nftables mode |

## 1. The name

```bash
kubectl -n $NS exec POD -- cat /etc/resolv.conf                     # search <ns>.svc.cluster.local svc.cluster.local cluster.local; options ndots:5
kubectl -n $NS exec POD -- getent hosts $SVC.$SNS.svc.cluster.local.
kubectl -n kube-system get pods -l k8s-app=kube-dns -o wide; kubectl -n kube-system logs -l k8s-app=kube-dns --tail=50
kubectl -n kube-system get configmap coredns -o jsonpath='{.data.Corefile}'
python3 miniproxy.py dns NAME --resolv-conf resolv.conf --from objects.json   # every name the stub resolver tries, and which answer
```

A short name only resolves in the Service's own namespace; from elsewhere use `svc.ns`. A headless Service returns pod IPs only for **ready** endpoints, so an empty answer with healthy-looking pods means none is Ready. If external names are slow, count the attempts: fewer than `ndots` dots means every search domain is tried first. Fix with a trailing dot, `dnsConfig.options` `ndots: "2"`, or CoreDNS `autopath`, not by restarting CoreDNS. `cache 30` in the Corefile keeps a corrected record stale for up to 30 seconds.

## 2. The Service object

```bash
kubectl -n $SNS get svc $SVC -o yaml | grep -A20 '^spec:'
kubectl -n $SNS get pods -l "$(kubectl -n $SNS get svc $SVC -o jsonpath='{.spec.selector}' | python3 -c 'import json,sys; print(",".join(f"{k}={v}" for k,v in json.load(sys.stdin).items()))')" -o wide
```

| Check | Wrong when |
|---|---|
| `selector` | it matches no pods, or more pods than you think (a canary or a debug pod with the same labels) |
| `ports[].targetPort` | a name that the pods' `containerPort` entries do not carry, or a number the process does not listen on |
| `ports[].name` | missing on a multi-port Service, or renamed while clients use the SRV record |
| `type`, `clusterIP` | `None` (headless: no virtual IP, no rules, DNS returns pods) when clients expect one address |
| `publishNotReadyAddresses` | true on a Service clients use, so traffic reaches pods that are not ready |
| `sessionAffinity` | `ClientIP` behind a proxy or NAT, so all clients look like one and stick to one pod |

## 3. The EndpointSlices

```bash
kubectl -n $SNS get endpointslices -l kubernetes.io/service-name=$SVC -o wide
kubectl -n $SNS get endpointslices -l kubernetes.io/service-name=$SVC -o yaml | grep -E 'addresses|ready|serving|terminating|name:|port:'
python3 miniproxy.py endpoints $SNS/$SVC --from objects.json --slices computed   # what the controller should have written, from the pods
```

| What you see | Meaning |
|---|---|
| PORTS `<unset>`, ENDPOINTS `<unset>` | the selector matches no pod with an IP |
| endpoints present, all `ready: false` | readiness fails everywhere; kube-proxy rejects and headless DNS is empty |
| `ready: false`, `serving: true`, `terminating: true` | a rollout or drain in progress; used only when nothing is ready |
| some pods in a slice without the Service's port | their container ports do not carry the named `targetPort` (a rename) |
| the computed slices differ from the real ones | the controller is behind or failing: check kube-controller-manager logs |

## 4. The pod's own port

```bash
kubectl -n $SNS get pod POD -o jsonpath='{range .spec.containers[*].ports[*]}{.name}={.containerPort}/{.protocol}{"\n"}{end}'
kubectl -n $SNS port-forward pod/POD 18080:TARGET_PORT & sleep 1; curl -sv localhost:18080/ -o /dev/null; kill %1
kubectl -n $SNS debug -it POD --image=busybox:1.37 --target=CONTAINER -- netstat -lnt
```

A process bound to `127.0.0.1` passes an `exec` probe and fails every Service request; it must listen on `0.0.0.0` or the pod IP. A readiness probe on a different port than the Service's `targetPort` proves nothing about the Service.

## 5. The node's rules

```bash
kubectl -n kube-system get configmap kube-proxy -o jsonpath='{.data.config\.conf}' | grep -E '^(mode|clusterCIDR):'
kubectl -n kube-system get pods -l k8s-app=kube-proxy -o wide; kubectl -n kube-system logs ds/kube-proxy --tail=50
CIP=$(kubectl -n $SNS get svc $SVC -o jsonpath='{.spec.clusterIP}')
# on the client's node (kind: docker exec NODE ...; cloud: a node debug pod, kubectl debug node/NODE -it --image=busybox:1.37, then chroot /host)
iptables-save -t nat > nat.txt; iptables-save -t filter | grep "$SNS/$SVC"
nft list table ip kube-proxy > rules.nft                           # when the mode is nftables
python3 miniproxy.py trace --ruleset nat.txt --to $CIP:PORT        # endpoints, real shares, SNAT conditions, or the REJECT that answered
```

The endpoint set in the rules must equal the ready endpoints in the slices. Fewer means kube-proxy on that node is behind or crashed; extra `KUBE-SEP-` targets mean stale rules. An uneven cascade (shares not equal) is never written by kube-proxy; something else edited the table. With Cilium's kube-proxy replacement there are no `KUBE-` chains: use `kubectl -n kube-system exec ds/cilium -- cilium-dbg service list`.

For traffic from a node or from outside: the `KUBE-SVC-` chain marks sources outside `clusterCIDR` for SNAT; `KUBE-EXT-` marks NodePort traffic with `externalTrafficPolicy: Cluster`; with `Local` only `KUBE-SVL-` endpoints on that node are used, and a node without one drops the traffic, which looks like a load balancer sending to the wrong nodes.

## 6. Conntrack and policy

```bash
conntrack -L -d $CIP 2>/dev/null | head        # on the node: existing flows keep their old pod after a rules change
kubectl get networkpolicy -A -o wide           # a default-deny in either namespace turns a refused connection into a timeout
```

UDP flows to a pod that went away keep their conntrack entry until it expires; kube-proxy clears entries for removed UDP endpoints, but a stale DNS flow is still the classic "DNS fails for 30 seconds after a CoreDNS restart".

## 7. Report

State: the exact error and where you ran it from; the name and the attempts it took (with `ndots`); the Service's selector, ports and `targetPort`; the EndpointSlice rows (address, ready, serving, terminating) and whether the controller's computed rows agree; the proxy mode and the trace from the client's node (endpoint set, shares, SNAT conditions, or the REJECT/DROP that answered); conntrack or policy findings; the single layer at fault with the command and value that proved it; and the change, with how you verified it (the probe from Step 0 succeeding). Do not recommend restarting CoreDNS or kube-proxy unless a step above showed that component wrong.
