---
name: packet-capture-triage
description: Decide whether a packet capture can answer the question, take it at the right place without filling the disk or losing packets, and read it into a verdict per connection and per DNS lookup
version: 1.0.0
phase: networking
lesson: networking/packet-capture
tags: [networking, tcpdump, pcap, bpf, tcp, dns, kubernetes, wireshark, debugging]
---

# Packet capture triage

Use this when someone says "it's the network", when latency has a suspiciously round number in it (1 s, 3 s, 5 s), when connections are refused, reset or hang, or when two teams disagree about which side is slow. A capture settles it, but only if it is taken at the right place, with a filter that keeps the evidence, and read with the mechanism in mind. Do not paste raw captures into tickets: they contain addresses, cookies and sometimes credentials.

## 1. State the question before capturing

Write down one sentence: which client, which server (address and port), which symptom, and roughly when it happens. Then pick the capture point:

| Question | Capture where | Why |
|---|---|---|
| does the request leave the client at all | client host or client pod | proves the SYN or query was sent |
| does it arrive at the server | server host or server pod | a SYN on the server but not answered points at the server side |
| is it lost in between | both ends at once, same filter | a packet present on one side and missing on the other was lost between them |
| is a Kubernetes node or CNI the problem | the node, `-i any`, then one interface at a time | shows the packet on the veth, the bridge and the uplink, before and after NAT |
| is DNS slow | the client pod, `udp port 53 or tcp port 53` | the resolver's retries and timeouts are visible only from the client |

The tap copies incoming frames before netfilter and outgoing frames after NAT; packets an XDP program drops are never seen. A captured SYN proves arrival at the interface, nothing more.

## 2. Take the capture safely

```bash
IFACE=eth0; FILTER='host 10.1.2.3 and tcp port 5432'
sudo tcpdump -i "$IFACE" -nn -s 128 -U -Z root -c 200000 -w /tmp/case.pcap "$FILTER"
sudo tcpdump -i "$IFACE" -nn -s 128 -Z root -C 100 -W 10 -w /tmp/ring.pcap "$FILTER"      # long-running: ten 100 MB files in a ring
sudo tcpdump -i "$IFACE" -d "$FILTER" | head -5                                             # the compiled BPF, if the filter is in doubt
```

- `-nn` always: name resolution slows tcpdump down and adds its own DNS queries to the capture.
- `-s 128` keeps every header for connection analysis; use `-s 0` only when you need payload (HTTP bodies, DNS over TCP).
- The filter must keep the evidence. Add `or udp port 53` when names are involved. Remember that `and` and `or` have equal precedence (write the parentheses), and that `tcp[...]` byte filters never match IPv6.
- `-Z root` and a file name ending in `.pcap` avoid permission and AppArmor surprises on Debian and Ubuntu.
- When you stop it, read the last three lines. `packets dropped by kernel` above zero means the capture has holes that look like network loss: narrow the filter, lower the snaplen or raise `-B`.

Inside a pod, from the node:

```bash
PID=$(crictl inspect --output go-template --template '{{.info.pid}}' "$(crictl ps --name CONTAINER -q | head -n1)")
sudo nsenter -t "$PID" -n tcpdump -i eth0 -nn -s 128 -U -Z root -c 50000 -w /tmp/pod.pcap "$FILTER"
```

Without node access: `kubectl debug -i POD --image=nicolaka/netshoot:v0.16 --profile=netadmin -- tcpdump -i eth0 -nn -U -c 50000 -w - "$FILTER" > pod.pcap` (refused in namespaces that enforce the baseline or restricted Pod Security Standard).

## 3. Read it

```bash
python3 pcaplens.py info case.pcap                       # format, link type, snaplen, packets, truncation, time span
python3 pcaplens.py flows case.pcap --json > flows.json  # one verdict per connection
python3 pcaplens.py dns case.pcap                         # every lookup: latency, retries, rcode
tcpdump -r case.pcap -nn -tttt 'tcp[tcpflags] & (tcp-syn|tcp-rst) != 0'   # handshakes and resets only (IPv4)
tshark -r case.pcap -q -z conv,tcp                        # the same table in Wireshark's terms, if installed
```

If the link type is `LINUX_SLL` or `LINUX_SLL2` (`-i any`), a packet appears once per interface it crossed: compare one interface at a time before counting retransmissions.

## 4. Map what you see to a cause

| In the capture | Most likely cause | Next command |
|---|---|---|
| SYN, SYN, SYN at 1 s, 3 s, 7 s, no reply (`no answer`) | a firewall, security group or NetworkPolicy drops it, a wrong route, or the host is down | capture on the server side: SYN absent means dropped in between; present means a host firewall (`nft list ruleset`) |
| SYN answered with RST (`refused`) | nothing listens on that address and port, or a firewall rejects with RST | `ss -lnt` on the server; check the bind address (127.0.0.1 versus 0.0.0.0) |
| SYN-ACK retransmitted, client ACK missing | return path broken or asymmetric routing; the client never saw the SYN-ACK | capture on the client; check `rp_filter` and routes |
| handshake fine, data segments retransmitted, duplicate ACKs with SACK, holes | packet loss on the path from the sender | capture at both ends to locate it; check interface errors (`ip -s link`) and MTU |
| retransmissions of large segments only, small ones fine | MTU black hole: ICMP "fragmentation needed" blocked | `ping -M do -s 1472 HOST`; allow ICMP type 3 code 4, or clamp MSS |
| receiver's window falls to 0, sender's probes repeat | the receiving application stopped reading | thread dump or `ss -tnpi` on the receiver: Recv-Q grows |
| RST after a long idle period | an idle timeout in a NAT, load balancer or firewall dropped the conntrack entry | compare the idle time with the device's timeout; enable TCP keepalive below it |
| RST right after a request with unread data | the peer closed its socket with unread input, which Linux turns into RST | application logs on the side that sent the RST |
| DNS query, no answer, same query 5 s later | a lost UDP query (often a conntrack race on A and AAAA from one socket) and the resolver's timeout | `options single-request-reopen` or `use-vc` in resolv.conf, NodeLocal DNSCache, or an upgrade of the node's kernel and conntrack handling |
| one lookup becomes five queries ending in NXDOMAIN | `ndots` and `search` expansion in the pod's resolv.conf | use a fully qualified name with a trailing dot, or lower `ndots` |
| far more packets than expected, each appearing twice | `-i any` on a host with veths and bridges | capture on one interface |

## 5. Verify the conclusion

A verdict from one side is a hypothesis. Confirm it with a second capture at the other end, or by changing the suspected cause and capturing again with the same filter: the symptom must disappear, not move.

## 6. Report

State: the question; where, when and with which filter and snaplen you captured, and the `dropped by kernel` count; the link type; for each relevant connection the `flows` verdict (state, handshake RTT, retransmissions, duplicate ACKs, zero windows, who closed or reset first) and the three to five tcpdump lines that show it; for DNS the query, latency, retries and rcode; the cause from the table above; the confirming capture or change; and what the capture could not show (payload with a small snaplen, encrypted content, packets dropped before the tap). Never attach the raw capture to a public ticket.
