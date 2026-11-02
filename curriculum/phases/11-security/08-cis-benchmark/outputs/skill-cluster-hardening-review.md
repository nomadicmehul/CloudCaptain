---
name: cluster-hardening-review
description: Review a Kubernetes cluster's nodes against a named CIS Kubernetes Benchmark version with a pinned kube-bench and minibench, verify the kubelet against configz, triage FAIL and WARN with evidence, fix one component and one node at a time, prove each fix with a diff, and report exceptions with owners
version: 1.0.0
phase: security
lesson: security/cis-benchmark
tags: [kubernetes, cis, kube-bench, hardening, kubelet, control-plane, compliance, audit]
---

# Cluster hardening review (CIS Kubernetes Benchmark)

Use this before an audit or a customer security questionnaire, after building a new node image or node pool, after a Kubernetes or kubeadm upgrade, or when someone quotes an old kube-bench report as evidence. Do not change anything until step 4, and never change a control plane you have not backed up.

## 1. Inventory and pick the benchmark

```bash
kubectl version -o json | python3 -c 'import json,sys; print(json.load(sys.stdin)["serverVersion"]["gitVersion"])'
kubectl get nodes -L node-role.kubernetes.io/control-plane,kubernetes.io/os -o wide
kubectl get nodes -o jsonpath='{range .items[*]}{.metadata.name}{"\t"}{.status.nodeInfo.kubeletVersion}{"\t"}{.status.nodeInfo.osImage}{"\n"}{end}'
```

| Cluster | Benchmark to name | Targets you own |
|---|---|---|
| kubeadm, kind, self-managed | newest CIS Kubernetes Benchmark kube-bench ships (`cis-1.12` = v1.12.0, written for 1.32 to 1.34) | `master,controlplane,etcd,node` on control-plane nodes, `node` on workers |
| EKS | `eks-*` | `node,policies,managedservices,controlplane` (the control plane is AWS's) |
| GKE | `gke-*` | `node,policies,managedservices` |
| AKS | `aks-*` | `node` |
| OpenShift, k3s, RKE2 | their own `rh-*`, `k3s-cis-*`, `rke2-cis-*` configs | as the config lists |

Write down the benchmark name and version, the Kubernetes version, and one node per pool to sample (pools differ by image, so one node per image is the minimum). If the cluster is newer than the benchmark, say so in the report.

## 2. Run both tools on each sampled node

```bash
V=0.16.0
curl -fsSLO https://github.com/aquasecurity/kube-bench/releases/download/v$V/kube-bench_${V}_linux_amd64.tar.gz
curl -fsSLO https://github.com/aquasecurity/kube-bench/releases/download/v$V/kube-bench_${V}_checksums.txt
grep " kube-bench_${V}_linux_amd64.tar.gz\$" kube-bench_${V}_checksums.txt | sha256sum -c -       # stop if this fails
# on the node (or as a Job with hostPID and read-only hostPath mounts, image pinned, never :latest):
./kube-bench run --targets <targets> --benchmark <benchmark> --json > kube-bench-<node>.json
python3 minibench.py collect --node local -o snapshot-<node>.json                                 # or docker:<kind-node>, root:/mnt/<copy>
python3 minibench.py run snapshot-<node>.json --json > minibench-<node>.json
python3 minibench.py diff minibench-<node>.json kube-bench-<node>.json --common --fail-on any
```

Every disagreement printed by the diff is a finding about a tool before it is a finding about the node. Resolve it by reading the audit in `cfg/<benchmark>/*.yaml` and the evidence `minibench` printed:

| Disagreement | Usual cause | Trust |
|---|---|---|
| kubelet 4.2.x PASS in kube-bench, FAIL in minibench | the kubelet runs without `--config`; kube-bench read a file the kubelet never loaded | configz (step 3) |
| 4.2.1 FAIL in kube-bench, PASS in minibench | config file lacks the key; the config-file default is safe | configz |
| 1.3.7 or 1.4.2 PASS in kube-bench only | `--bind-address` missing; the default listens on all interfaces | minibench |
| 4.2.13 PASS in kube-bench only | `podPidsLimit: -1`, which is no limit | minibench |
| admission plugin check PASS in kube-bench only | substring match on a plugin name | minibench |
| manifest and process disagree (`drift` in minibench) | an edit the kubelet has not applied, or a broken manifest | the process, then fix the manifest |

## 3. Check the kubelet against what is running

```bash
kubectl get --raw "/api/v1/nodes/<node>/proxy/configz" > configz-<node>.json
python3 -c 'import json,sys; c=json.load(open(sys.argv[1]))["kubeletconfig"]; [print(k, c.get(k)) for k in ("readOnlyPort","streamingConnectionIdleTimeout","podPidsLimit","seccompDefault","rotateCertificates")]; print("anonymous", c["authentication"]["anonymous"]["enabled"], "authz", c["authorization"]["mode"])' configz-<node>.json
```

A missing key in configz is the Go zero value (`readOnlyPort` absent means 0). If configz says anonymous `true` or `AlwaysAllow` on any node, stop the review and treat it as an incident: anyone who reaches port 10250 can read pod specs and exec into containers.

## 4. Triage

| Result | Examples | Decision |
|---|---|---|
| FAIL, reachable credential or remote access | 4.2.1, 4.2.2, 4.2.4 serving, 1.2.2 token file, 2.2/2.5 etcd without client certificates | fix now; check access logs for use |
| FAIL, missing audit trail | 1.2.16 to 1.2.19, 3.2.1 | fix this week; without it an incident cannot be investigated |
| FAIL, file permissions on credentials | 1.1.13 to 1.1.18, 1.1.21, 4.1.5 | fix this week; who else can log in to the node decides urgency |
| FAIL, hardening defaults | 1.2.15, 1.3.2, 1.4.1 profiling, 1.2.30, 4.1.1, 4.1.9 | next change window |
| FAIL that needs design | 1.2.5 (kubelet serving certificates signed by the cluster CA), 1.1.12 (etcd user) | plan with an owner and a date |
| WARN | Manual or unscored: 1.2.1, 1.2.9, 1.2.27, 4.2.13, 3.x | decide and record: adopt, accept with a reason and expiry, or not applicable |

An accepted exception names the check and benchmark version, the reason, the compensating control, the owner and the expiry. "kind does it this way" is not a reason for a production cluster.

## 5. Fix one component, one node at a time

```bash
cp /etc/kubernetes/manifests/kube-apiserver.yaml /root/kube-apiserver.yaml.$(date +%F)           # back up outside the manifests directory
# edit the flag in the manifest; the kubelet restarts the static pod
until crictl ps --name kube-apiserver --state running -q | grep -q .; do sleep 2; done
kubectl get --raw /readyz?verbose | tail -3
```

Never leave backups in `/etc/kubernetes/manifests`: the kubelet runs every manifest in that directory. Change one control-plane node, wait for it to be healthy, then the next. For kubelet settings, change the config file (or the node image), `systemctl restart kubelet`, and confirm with configz before moving on. Roll back by restoring the backup file.

## 6. Prove each fix

```bash
python3 minibench.py collect --node local -o snapshot-<node>-after.json
python3 minibench.py run snapshot-<node>-after.json --json > minibench-<node>-after.json
python3 minibench.py diff minibench-<node>.json minibench-<node>-after.json                        # exit 1 means something got worse
./kube-bench run --targets <targets> --benchmark <benchmark> --check <ids you fixed> --json
```

A fix is done when both tools show the new state and nothing else changed.

## 7. Report

State: cluster, Kubernetes version, benchmark name and version (and that it predates the cluster, if it does), tool versions with the kube-bench checksum, nodes sampled per pool; totals of PASS, FAIL and WARN per node from both tools; every tool disagreement with its resolution; each FAIL with the evidence line, the fix applied or planned, the owner and date; each WARN with its decision; exceptions with expiry; and the JUnit or JSON reports attached. Never report "CIS compliant" without the benchmark version and the list of accepted exceptions.
