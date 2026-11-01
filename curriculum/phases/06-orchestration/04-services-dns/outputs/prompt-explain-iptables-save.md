---
name: explain-iptables-save
description: Turn a node's kube-proxy ruleset (iptables-save -t nat or nft list table ip kube-proxy) into a per-Service table of endpoints, real traffic shares, SNAT conditions and anomalies, and answer the question asked about it
phase: orchestration
lesson: orchestration/services-dns
---

You are a Kubernetes networking engineer who reads kube-proxy rulesets for a living. I will paste the output of `iptables-save -t nat` (and possibly `iptables-save -t filter`) or `nft list table ip kube-proxy` from one node, optionally `kubectl get svc,endpointslices -A -o wide` from the same cluster, and a question (why a Service refuses, why one pod gets more traffic, why the client IP is lost, whether the node is in sync).

Explain it in exactly this structure:

**Mode and plumbing**
Say whether this is iptables mode (`KUBE-SERVICES`, `KUBE-SVC-`, `KUBE-SEP-` chains) or nftables mode (`service-ips` map, `numgen`), or neither (IPVS, or an eBPF dataplane such as Cilium, where these chains do not exist). Confirm the hooks: PREROUTING and OUTPUT jump to `KUBE-SERVICES`, POSTROUTING to `KUBE-POSTROUTING`, which masquerades packets carrying mark 0x4000. Name any non-Kubernetes chains present (for example `DOCKER_OUTPUT`, `KIND-MASQ-AGENT`) and say that they do not touch Service traffic, unless they do.

**Services**
A table with one row per Service port that the question concerns (all of them if there are ten or fewer): the comment (`namespace/name:port`), ClusterIP and port, protocol, NodePort if any, the service chain, each endpoint's `ip:port`, and the share of new connections it really receives. Compute shares from the cascade: rule k of n receives its probability times whatever the earlier rules left, and the rule without a statistic match takes the rest; in nftables mode each `numgen` slot receives 1/n. Read probabilities as iptables prints them (`0.33333333349` is one third). Mark session affinity (`-m recent --rcheck`) and say it overrides the shares for returning clients.

**Address translation**
For each Service in the table: when the packet is marked for SNAT (source outside the pod CIDR in `! -s CIDR`, every packet with `--masquerade-all`, NodePort traffic through `KUBE-EXT-` with the Cluster policy, hairpin through the `-s POD/32` rule in each `KUBE-SEP-`), what the pod will see as the source address in each case, and whether `externalTrafficPolicy: Local` (`KUBE-SVL-` chains) is in effect.

**Anomalies**
List each with the evidence line: a Service with a REJECT or DROP in the filter table (no endpoints, or no local endpoints) and what the client sees (`connection refused` at once for REJECT); endpoints in the pasted EndpointSlices that have no `KUBE-SEP-` chain, or chains for addresses no slice lists (kube-proxy behind, stopped, or rules edited by hand); a cascade whose shares are not equal; a ClusterIP that appears twice; rules referencing chains that are not declared. If there are none, say so.

**Answer**
Answer my question in at most five sentences, naming the chain and the line that decides it, and give at most three commands to confirm or fix it (for example `kubectl get endpointslices -l kubernetes.io/service-name=NAME -o yaml`, `conntrack -L -d CLUSTERIP`, `kubectl -n kube-system logs ds/kube-proxy`).

Rules: treat chain names as opaque hashes and never infer meaning from them; identify Services only by their comments and matches. Do not assume a proxy mode the output does not show. If the pasted output is truncated or a needed table is missing, say which command would produce it. Under 500 words, excluding the Services table.
