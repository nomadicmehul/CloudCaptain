---
name: container-network-triage
description: Find why a container cannot be reached, or cannot reach out, by walking the packet path in order: namespace, veth pair, bridge, forwarding and firewall, NAT, DNS
version: 1.0.0
phase: containers
lesson: containers/container-networking
tags: [docker, linux, networking, netns, veth, bridge, nftables, iptables, nat, dns, kubernetes, debugging]
---

# Container network triage

Use this when a published port does not answer, a container cannot reach another container or the internet, or name resolution fails inside a container. Walk the layers in order and stop at the first one that is broken; every later layer depends on it. Never "fix" a step with `--network host` or `--privileged`; that removes the layer instead of repairing it.

## 0. Find the namespace

```bash
CID=<container name or id>
PID=$(docker inspect -f '{{.State.Pid}}' "$CID")
docker inspect -f '{{json .NetworkSettings.Networks}}' "$CID" | python3 -m json.tool | grep -E '"(IPAddress|Gateway|NetworkID)"'
docker port "$CID"
```

`PID` is `0` when the container is not running: read `docker logs` first. Kubernetes: `kubectl get pod POD -o jsonpath='{.status.podIP}'`, then on the node `crictl inspectp` (the sandbox holds the namespace) or `nsenter -t $(pgrep -f pause | head -1) -n`. Compose: the container's network is `<project>_default` unless the file says otherwise.

## 1. Inside: interface, address, route, loopback

```bash
nsenter -t "$PID" -n ip -br addr
nsenter -t "$PID" -n ip route
nsenter -t "$PID" -n ss -ltnp
```

| Symptom | Meaning |
|---|---|
| no `eth0` | the veth end was never moved in, or a CNI plugin failed; check `docker network connect` / CNI logs (`/var/log/containers`, `journalctl -u kubelet`) |
| `eth0` without an address | IPAM failed or the pool is exhausted (`docker network inspect` shows the subnet; count the containers) |
| `eth0` state DOWN | `ip link set eth0 up` never ran; also look at the peer in step 2 |
| no `default via` route | the gateway was not configured; traffic to anything outside the subnet has nowhere to go |
| `lo` DOWN | the process cannot even talk to itself; `ip link set lo up` |
| `ss -ltnp` does not list the port | the application is not listening, or listens on `127.0.0.1` only; the network is innocent |

A process bound to `127.0.0.1` inside the container is reachable from nowhere else. It must bind `0.0.0.0` (or the container address).

## 2. The cable: veth peer and its master

```bash
IFINDEX=$(nsenter -t "$PID" -n ip -j link show eth0 | python3 -c 'import json,sys; print(json.load(sys.stdin)[0]["link_index"])')
ip -o link | grep "^$IFINDEX:"
bridge link show | grep -E "^$IFINDEX:"
```

The host end must exist, be UP and have `master <bridge>`. A veth without a master is a cable plugged into nothing. If the host end is missing, the peer was deleted (`ip link del`) and the only repair is recreating the pair.

## 3. The bridge

```bash
BR=$(ip -o link | grep "^$IFINDEX:" | sed -n 's/.*master \([^ ]*\).*/\1/p')
ip -br addr show "$BR"
bridge link show master "$BR"
nsenter -t "$PID" -n ping -c1 -W1 <gateway>
```

The bridge must be UP and carry the gateway address the containers route to. If the ping to the gateway fails here while steps 1 and 2 passed, suspect the bridge's filter rules (`nft list ruleset | grep -i "$BR"`, `iptables -S | grep "$BR"`) or `br_netfilter` policies (`sysctl net.bridge.bridge-nf-call-iptables`).

## 4. Forwarding and the firewall

```bash
sysctl net.ipv4.ip_forward
iptables -S FORWARD | head -5        # policy DROP on Docker hosts; look for your bridge or DOCKER-USER
nft list chain ip filter FORWARD 2>/dev/null | head
nft list ruleset | grep -nE 'hook forward|policy drop'
```

`ip_forward=0` stops every container-to-outside packet; a configuration management run or a sysctl file that reset it is the usual cause. With Docker installed the FORWARD policy is DROP and only Docker's bridges are accepted; another bridge (yours, libvirt's, a CNI bridge on a Docker host) needs `iptables -I DOCKER-USER -i BR -j ACCEPT` and `-o BR`. A packet must pass every forward hook: accepting it in your own nft table does not override a drop in another table.

## 5. NAT: published ports and egress

```bash
nft list table ip nat 2>/dev/null; nft list table ip ccnet 2>/dev/null
iptables -t nat -S DOCKER; iptables -t nat -S POSTROUTING
conntrack -L 2>/dev/null | grep <container ip> | head
```

| Check | Expect |
|---|---|
| published port | a DNAT rule with the host port, pointing at the container's current address and port (the address changes on every restart unless it is fixed; a stale rule points at nobody) |
| the DNAT is reachable from host traffic | the chain is jumped to from OUTPUT as well as PREROUTING; with Docker's default userland proxy, `localhost:PORT` is served by `docker-proxy` instead (`ss -ltnp | grep docker-proxy`) |
| egress | a MASQUERADE (or SNAT) rule whose source covers the container subnet and whose out-interface excludes the bridge |
| conntrack | an entry for the flow; `conntrack -L` shows the translated tuple, and a flow that never appears never reached the host |

`main.py explain NAME` from the lesson runs every check of steps 1 to 5 in one command; for a Docker container pass `--ip`, `--gateway`, `--bridge docker0` and `--publish`.

## 6. DNS

```bash
nsenter -t "$PID" -n cat /etc/resolv.conf
docker exec "$CID" nslookup other-container 2>/dev/null || docker exec "$CID" getent hosts other-container
```

On Docker's default `bridge` network containers are not resolvable by name; `127.0.0.11` (the embedded DNS) is configured only on user-defined networks. A `resolv.conf` pointing at `127.0.0.53` (systemd-resolved's stub) is unreachable from inside a namespace; Docker rewrites it, hand-made namespaces need `/etc/netns/NAME/resolv.conf`. In Kubernetes the resolver is the CoreDNS Service IP and `ndots:5` makes short names expand through the search list.

## 7. Decide

| Observation | Layer | Fix |
|---|---|---|
| `curl` from another host times out, from the host works | 4 or 5 | FORWARD policy, DNAT missing from PREROUTING, cloud security group |
| `curl localhost:PORT` on the host fails, container address works | 5 | DNAT not reachable from OUTPUT, `docker-proxy` not running, `route_localnet` |
| container reaches gateway, not the internet | 4 or 5 | `ip_forward`, MASQUERADE, DOCKER-USER accept |
| nothing works, even the gateway | 1 to 3 | address, route, veth master, bridge state |
| works by IP, not by name | 6 | network type (bridge vs user-defined), resolv.conf |
| worked until a firewall reload | 5 | rules flushed; restart dockerd or re-add; keep custom rules in DOCKER-USER so Docker does not flush them |

## 8. Report

State the layer, the command and value that proved it, and the fix: `NAT -> iptables -t nat -S DOCKER has no --dport 8080 rule after the firewall reload -> systemctl restart docker`. Mention what was ruled out above the failing layer, so the next person does not repeat the walk.
