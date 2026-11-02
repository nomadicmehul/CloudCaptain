---
name: routing-triage
description: Find out why a packet takes the wrong path or no path on a Linux host or router by asking the kernel for its decision with the real source, mark and input interface, then walking the policy rules, the chosen table, the source address, forwarding, rp_filter and the neighbour, hop by hop
version: 1.0.0
phase: networking
lesson: networking/ip-routing
tags: [linux, networking, routing, ip-rule, policy-routing, rp-filter, ecmp, arp, vpc, debugging]
---

# Routing triage

Use this when a host or a router cannot reach a destination, reaches it through the wrong link, works for one source address and not another, or worked until a route, rule or sysctl change. Routing is decided per packet from the destination, the source, the firewall mark and the input interface, so never test with a command that has different values from the failing traffic: a `ping` from the router is not the backup job's packet. Do not reboot, flush tables or add routes during triage; every change destroys the evidence.

## 1. Ask the kernel for the decision of the failing packet

```bash
DST=<destination>; SRC=<source address of the failing traffic>
ip route get $DST                                  # the host's own traffic with an unbound socket
ip route get $DST from $SRC                        # the host's own traffic from that source (SRC must be local)
ip route get $DST from $SRC iif <in-dev>           # a packet this router forwards, as it arrives
ip route get $DST from $SRC mark <0xN>             # traffic the firewall or application marks
ip route get fibmatch $DST                         # the table entry that matched, with its metric
```

On a remote or containerised host, run the same inside the right namespace: `ip -n NS route get ...` or `nsenter -t PID -n ip route get ...`. Save `ip -j addr`, `ip -4 -j route show table all`, `ip -4 -j rule`, `ip -j neigh` and the forwarding sysctls (or `routelens snapshot DIR --netns NS`), then `routelens get $DST --from $SRC --iif DEV --dir DIR` prints every rule the kernel looked at and why it moved on.

| Answer | Meaning | Next |
|---|---|---|
| `RTNETLINK answers: Network is unreachable` | no rule's table has a route (ENETUNREACH) | step 3: which tables were consulted, is there a default route |
| `Invalid argument` | a blackhole route or rule (EINVAL); with `iif`, also a martian source | step 3 or step 5 |
| `No route to host` | an `unreachable` route, or forwarding is off on the input interface (EHOSTUNREACH) | step 3, then step 5 |
| `Permission denied` | a `prohibit` route or rule (EACCES) | step 2 or step 3 |
| `Invalid cross-device link` | rp_filter rejected the source (EXDEV) | step 5 |
| `Invalid argument` with `iif` | newer kernels report an rp_filter or other input-route drop as EINVAL; check rp_filter and forwarding as in step 5 | step 5 |
| `table N` you did not expect | a policy rule won before main | step 2 |
| right table, wrong `dev` or `via` | a longer prefix or lower metric than you thought | step 3 |
| right route, wrong `src` | prefsrc or address order | step 4 |
| the answer looks right | the host is fine; the next hop or a later router is not | steps 6 and 7 |

## 2. Rules: which table did the packet see?

```bash
ip rule                                             # IPv4, in priority order
ip -6 rule
ip route show table <N>                             # every table a matching rule names
```

Read the rules top to bottom with the packet's values. A `from` selector never matches the host's own traffic with an unbound socket (the source is 0.0.0.0 during the lookup); `iif lo` matches only local traffic; a `fwmark` rule needs the mark the firewall actually sets (`nft list ruleset | grep -i mark`). The first rule whose table yields a route ends the lookup, so a table that contains only a default route answers for everything, including networks that are directly connected in `main`. Typical repair: `ip rule add to <local-prefix> lookup main priority <lower than the source rule>`, or copy the connected routes into the policy table.

## 3. Table: longest prefix, then metric

```bash
ip route show table all | grep -v '^local\|^broadcast'
ip route show table main match $DST                 # every prefix that contains DST
```

The longest prefix wins regardless of the order or metric of others; the metric only breaks ties between routes for the same prefix. Look for a more specific route someone added (a /32 host route, a VPN's split routes), a summary route that is too wide, routes with `linkdown` or `dead`, and `blackhole`, `unreachable`, `prohibit` or `throw` entries. Multipath routes (`nexthop ... nexthop ...`) hash each flow onto one next hop: one failing flow and other working flows to the same destination point at one bad path, which `ip route get $DST from $SRC ipproto tcp sport A dport B` can reveal when `fib_multipath_hash_policy` includes ports.

## 4. Source address

```bash
ip -br addr show dev <out-dev>
ip addr show dev <out-dev> | grep -E 'secondary|tentative|deprecated'
```

The source is the route's `src` if it has one; otherwise the first primary address on the outgoing device in the gateway's subnet, then any primary address on it, then any global address on the host. Secondary addresses are never chosen automatically. A wrong source breaks replies (the far end routes them elsewhere), firewall rules and policy routing on the next router. Fix it with `src` on the route (`ip route change ... src ADDR`) or by binding the application, not by reordering addresses.

## 5. Forwarding and reverse-path filtering (routers only)

```bash
sysctl net.ipv4.ip_forward net.ipv4.conf.<in-dev>.forwarding
sysctl net.ipv4.conf.all.rp_filter net.ipv4.conf.<in-dev>.rp_filter
sysctl -w net.ipv4.conf.all.log_martians=1 && dmesg | grep -i martian | tail
ip route get $SRC                                   # the route back to the source
```

Forwarding off gives EHOSTUNREACH for every forwarded packet. The effective `rp_filter` is the larger of `conf.all` and the interface value. Strict (1) drops a packet when the route back to its source leaves through a different interface than the one it arrived on, which breaks asymmetric routing, dual uplinks and some overlay networks; loose (2) only requires that a route back exists.

| Situation | Setting |
|---|---|
| host with one uplink, no forwarding | `rp_filter=1` is right |
| router with asymmetric paths, policy routing, ECMP | `rp_filter=2` on the affected interfaces (and `conf.all` not higher) |
| Kubernetes node, container host, overlay networks | follow the CNI's documented requirement, usually 0 or 2 |

## 6. Neighbour: does the next hop answer?

```bash
ip neigh show dev <out-dev>
ip neigh show nud failed
arping -c 2 -I <out-dev> <next-hop>                 # if installed
```

`FAILED` or `INCOMPLETE` for the gateway (or for the destination on an on-link route) means the route is right and nothing answers ARP or NDP: wrong gateway address, host down, VLAN or switch problem, or a cloud security control. `REACHABLE`, `STALE` and `DELAY` are healthy. Devices with `NOARP` (dummy, WireGuard, loopback) have no neighbours to check.

## 7. Follow the path

```bash
tracepath -n $DST                                   # or: traceroute -n -s $SRC $DST
```

Repeat steps 1 to 6 on the first hop where the trace stops, with the packet's values as they arrive there (`iif`, source after any NAT). The return path is a separate routing decision on every hop, made from the reply's addresses; a one-way path is the common result of asymmetric routing plus strict rp_filter or a stateful firewall. In a cloud VPC the "router" is the subnet's route table: check which route table the source subnet is associated with, its longest matching route and whether that route's state is `blackhole`, and the source/destination check on any instance that forwards.

## 8. Report

State the failing packet (destination, source, mark, input interface), the kernel's answer from step 1 quoted exactly, the rule and table and route that produced it (or the errno and which route or rule caused it), the source address and where it came from, the forwarding, rp_filter and neighbour findings, the first hop where the path breaks, and the change that fixes it with the `ip route get` output after the change. Never report "added a route" without showing that the lookup now uses it.
