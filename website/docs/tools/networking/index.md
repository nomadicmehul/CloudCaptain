---
title: "Networking"
description: "Network fundamentals for DevOps"
---

# Networking

Understand the networking concepts every DevOps engineer needs to know.

## Core Topics

- Networking Commands — Essential tools and commands for network diagnostics
- Networking Concepts — Fundamental networking principles for DevOps engineers
- Networking Cheatsheets — Quick reference guides

## Learn More

### Network Fundamentals

Understand TCP/IP stack, DNS, VPCs, routing, firewalls, load balancing, and network security — critical for cloud infrastructure and DevOps roles.

### Key Protocols

- **TCP/UDP** — Transport layer protocols
- **DNS** — Domain name resolution
- **HTTP/HTTPS** — Application layer protocols
- **SSH** — Secure shell for remote access
- **ICMP** — Ping and traceroute

### DevOps Networking Focus

- VPC design and management
- Network security groups and firewalls
- Load balancer configuration
- DNS routing and CDN
- VPN and hybrid networking
- Container networking

---

## TCP/IP Model Layers

### Application Layer
- **Defines** — TCP/IP application protocols and how host programs interface with transport services
- **Protocols** — HTTP, HTTPS, FTP, SMTP, DNS, SSH, Telnet

### Transport Layer
- **Provides** — Communication session management between nodes
- **Defines** — Service level and connection states
- **Protocols** — TCP, UDP, RTP

### Internet Layer
- **Packages** — Data into IP datagrams with source/destination addresses
- **Performs** — Routing of IP datagrams
- **Protocols** — IP, ICMP, ARP, RARP

### Network Access Layer
- **Specifies** — Physical data transmission through networks
- **Handles** — Electronic signaling by hardware devices
- **Protocols** — Ethernet, Frame Relay, PPP

## IP Addressing

### IPv4 vs IPv6

**IPv4**
- Uses 32-bit binary addresses
- Expressed as four decimal numbers separated by dots (e.g., `192.168.1.1`)
- Each section is called an octet (8 bits)

**IPv6**
- Uses 128-bit binary addresses
- Expressed as eight groups of hexadecimal numbers separated by colons
- Groups of zeros can be omitted to save space

### Special IP Addresses

- `0.0.0.0` — Default network
- `255.255.255.255` — Network broadcast
- `127.0.0.1` — Loopback address (localhost)
- `169.254.0.1 to 169.254.255.254` — Automatic Private IP Addressing (APIPA)

## Essential Networking Commands

### Network Status

```bash
# Show socket ports and states
netstat -a

# Protocol stats and error counts
netstat -s

# Routing table dump
netstat -r

# List interfaces
netstat -i

# Run continuously every N seconds
netstat <seconds>

# Numeric addresses (avoid DNS lookup)
netstat -n

# Modern replacement for netstat
ss -tuln  # Show all listening TCP/UDP ports
```

### Network Interfaces

```bash
# Show all interfaces
ifconfig -a

# Configure a specific interface
ifconfig <interface>

# Set interface parameters (root only)
ifconfig <interface> <params>

# Show filesystem mounts
df .

# Show NFS mounts
df -t nfs
```

### Connectivity Testing

```bash
# Test if host is reachable
ping <host>

# Trace route to destination
traceroute <host>

# More detailed version
traceroute -I <host>

# Test TCP connection to specific port
telnet <host> <port>
```

### DNS & Name Resolution

```bash
# Query DNS server
nslookup <host>

# Extended query
nslookup

# Print ARP table (IP to Ethernet mapping)
arp -a
```

### File Transfer & Remote Access

```bash
# FTP file transfer
ftp <host>

# Remote copy between systems
rcp <switches> system:file system:file

# Remote shell
rsh <host> <command>

# Remote login
rlogin <host> -l <login>
```

### Process & System Monitoring

```bash
# List processes
ps aux

# List processes (alternative format)
ps alx

# Your foreground processes
ps

# Filter processes by string
ps aux | grep <string>
```

### Network Analysis

```bash
# tcpdump — Packet sniffer
# Captures and interprets packets from network interface
tcpdump -i <interface>

# Save packets for analysis
tcpdump -w filename.pcap

# Display detailed packet info
tcpdump -v -i <interface>
```

### Routing

```bash
# Print routing tables
netstat -r

# Add/delete static routes
route add/del <destination>

# BSD dynamic routing daemon
routed

# Alternative routing daemon (OSPF, EGP, RIP)
gated
```

## Important System Files

| File | Purpose |
|:-----|:---------|
| `/etc/hosts` | Map hostnames to IP addresses |
| `/etc/networks` | Map network names to IP addresses |
| `/etc/protocols` | Map protocol names to protocol numbers |
| `/etc/services` | Map TCP/UDP service names to port numbers |
| `/etc/fstab` | Filesystem mount points for boot time |
| `/etc/exports` | NFS export points |
| `/etc/mtab` | Current mount table |

## Contributing

Know great Networking resources? Submit a PR to help the community learn!

## Books & PDFs

### Core Networking Books

| Book | Link |
|:-----|:-----|
| Computer Networking | [View PDF](https://github.com/nomadicmehul/CloudCaptain/blob/main/Networking/Books/Computer_Networking.pdf) |
| HTTP Caching | [View PDF](https://github.com/nomadicmehul/CloudCaptain/blob/main/Networking/Books/HTTP%20Caching.pdf) |
| Network Attacks and Exploitation | [View PDF](https://github.com/nomadicmehul/CloudCaptain/blob/main/Networking/Books/Network%20Attacks%20and%20Exploitation.pdf) |
| Kurose 7 | [View PDF](https://github.com/nomadicmehul/CloudCaptain/blob/main/Networking/Books/Kurose-7.pdf) |

### Networking Cheatsheets

| Cheatsheet | Link |
|:-----------|:-----|
| BGP | [View PDF](https://github.com/nomadicmehul/CloudCaptain/blob/main/Networking/cheatsheet/BGP.pdf) |
| Cisco IOS Versions | [View PDF](https://github.com/nomadicmehul/CloudCaptain/blob/main/Networking/cheatsheet/Cisco_IOS_Versions.pdf) |
| EIGRP | [View PDF](https://github.com/nomadicmehul/CloudCaptain/blob/main/Networking/cheatsheet/EIGRP.pdf) |
| First Hop Redundancy | [View PDF](https://github.com/nomadicmehul/CloudCaptain/blob/main/Networking/cheatsheet/First_Hop_Redundancy.pdf) |
| Frame Mode MPLS | [View PDF](https://github.com/nomadicmehul/CloudCaptain/blob/main/Networking/cheatsheet/Frame_Mode_MPLS.pdf) |
| IEEE 802.11 WLAN | [View PDF](https://github.com/nomadicmehul/CloudCaptain/blob/main/Networking/cheatsheet/IEEE_802.11_WLAN.pdf) |
| IEEE 802.1X | [View PDF](https://github.com/nomadicmehul/CloudCaptain/blob/main/Networking/cheatsheet/IEEE_802.1X.pdf) |
| IOS IPv4 Access Lists | [View PDF](https://github.com/nomadicmehul/CloudCaptain/blob/main/Networking/cheatsheet/IOS_IPv4_Access_Lists.pdf) |
| IOS Interior Routing Protocols | [View PDF](https://github.com/nomadicmehul/CloudCaptain/blob/main/Networking/cheatsheet/IOS_Interior_Routing_Protocols.pdf) |
| IOS Zone-Based Firewall | [View PDF](https://github.com/nomadicmehul/CloudCaptain/blob/main/Networking/cheatsheet/IOS_Zone-Based_Firewall.pdf) |
| IPsec | [View PDF](https://github.com/nomadicmehul/CloudCaptain/blob/main/Networking/cheatsheet/IPsec.pdf) |
| IPv4 Multicast | [View PDF](https://github.com/nomadicmehul/CloudCaptain/blob/main/Networking/cheatsheet/IPv4_Multicast.pdf) |
| IPv4 Subnetting | [View PDF](https://github.com/nomadicmehul/CloudCaptain/blob/main/Networking/cheatsheet/IPv4_Subnetting.pdf) |
| IPv6 | [View PDF](https://github.com/nomadicmehul/CloudCaptain/blob/main/Networking/cheatsheet/IPv6.pdf) |
| IS-IS | [View PDF](https://github.com/nomadicmehul/CloudCaptain/blob/main/Networking/cheatsheet/IS-IS.pdf) |
| NAT | [View PDF](https://github.com/nomadicmehul/CloudCaptain/blob/main/Networking/cheatsheet/NAT.pdf) |
| OSPF | [View PDF](https://github.com/nomadicmehul/CloudCaptain/blob/main/Networking/cheatsheet/OSPF.pdf) |
| QoS | [View PDF](https://github.com/nomadicmehul/CloudCaptain/blob/main/Networking/cheatsheet/QoS.pdf) |
| RIP | [View PDF](https://github.com/nomadicmehul/CloudCaptain/blob/main/Networking/cheatsheet/RIP.pdf) |
| RIPng | [View PDF](https://github.com/nomadicmehul/CloudCaptain/blob/main/Networking/cheatsheet/RIPng.pdf) |
| Spanning Tree | [View PDF](https://github.com/nomadicmehul/CloudCaptain/blob/main/Networking/cheatsheet/Spanning_Tree.pdf) |
| Static Routing | [View PDF](https://github.com/nomadicmehul/CloudCaptain/blob/main/Networking/cheatsheet/Static_Routing.pdf) |
| Subinterfaces | [View PDF](https://github.com/nomadicmehul/CloudCaptain/blob/main/Networking/cheatsheet/Subinterfaces.pdf) |
| VLAN and Trunking | [View PDF](https://github.com/nomadicmehul/CloudCaptain/blob/main/Networking/cheatsheet/VLAN_and_Trunking.pdf) |
| VPN | [View PDF](https://github.com/nomadicmehul/CloudCaptain/blob/main/Networking/cheatsheet/VPN.pdf) |
| WAN Technologies | [View PDF](https://github.com/nomadicmehul/CloudCaptain/blob/main/Networking/cheatsheet/WAN_Technologies.pdf) |
| Wireless | [View PDF](https://github.com/nomadicmehul/CloudCaptain/blob/main/Networking/cheatsheet/Wireless.pdf) |
