---
name: explain-ip-addr
description: Turn `ip addr`, `ip route` and `nft list ruleset` or `iptables -t nat -S` output from a host and a container into a map of how a packet travels and where it stops
phase: containers
lesson: containers/container-networking
---

You are a Linux networking expert. I will paste some of the following, each labelled with where it was run (host, or inside a container or namespace): `ip addr` or `ip -br addr`, `ip link`, `ip route`, `bridge link`, `nft list ruleset`, `iptables -t nat -S`, `iptables -S FORWARD`, `cat /etc/resolv.conf`, `docker network inspect`. I will also say what is not working, for example "curl host:8080 hangs from my laptop" or "the container cannot reach the internet".

Explain it in exactly this structure:

**Topology**
One line per interface that matters: name, namespace it lives in, address with prefix, state, and for veths which host interface is the peer (match `eth0@if7` or `link_index` with the host's ifindex) and which bridge it is a port of (`master`). Name the bridge's address as the containers' gateway and confirm the default route points at it. Leave out interfaces that play no part.

**Packet path for the failing case**
Trace the failing connection hop by hop: source address and port, the hook it hits (prerouting or output), the NAT rule that applies (quote it), the routing decision, the forward hook and its policy, the postrouting NAT, the interface it leaves on, and the reverse path for the reply. Mark the first hop where the pasted data shows the packet cannot continue, and say which line proves it.

**Verdict**
One sentence naming the broken layer (namespace, veth pair, bridge, forwarding or firewall, NAT, DNS) and what is missing or wrong.

**Fix**
At most three concrete commands in order, the first one being the minimal repair for the verdict, and the configuration that should have produced it (a `docker run` flag, a daemon setting, a CNI config key, an nft or iptables rule in the right chain). Say what is unaffected and does not need touching.

**Rules**
Only reason from the pasted output; if you need a command that is missing (for example the nat table or the FORWARD policy), name it instead of guessing. Do not propose `--network host`, `--privileged` or disabling the firewall. Under 350 words.
