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

## Learning Path

Start with fundamentals and progress through intermediate and advanced topics:

### Beginner Path
1. **Fundamentals** — Master OSI/TCP-IP models, IP addressing, subnetting, DNS, DHCP, ARP
2. **Routing & Switching** — Understand basic routing, VLANs, spanning tree
3. **Cheatsheet** — Reference essential commands

### Intermediate Path
4. **Routing & Switching (Advanced)** — Deep dive into OSPF, BGP, MPLS
5. **Security** — Firewalls, IPsec, VPN, 802.1X

### Advanced Path
6. **Interview Questions** — Scenario-based troubleshooting and design
7. **Security (Advanced)** — Network attacks, defense strategies

---

## Documentation

### Reference by Topic

| Topic | Location |
|:------|:---------|
| OSI Model, IP addressing, Subnetting | [Fundamentals](/docs/tools/networking/fundamentals) |
| Routing protocols (OSPF, BGP, RIP, EIGRP, IS-IS) | [Routing & Switching](/docs/tools/networking/routing-switching) |
| VLANs, Spanning Tree, NAT, MPLS, WAN | [Routing & Switching](/docs/tools/networking/routing-switching) |
| Firewalls, IPsec, VPN, 802.1X, ACLs | [Security](/docs/tools/networking/security) |
| Network attacks and defense strategies | [Security](/docs/tools/networking/security) |
| 100+ commands (netstat, tcpdump, dig, nmap) | [Cheatsheet](/docs/tools/networking/cheatsheet) |
| Protocol quick reference (TCP/IP, DNS, ICMP) | [Cheatsheet](/docs/tools/networking/cheatsheet) |
| 40+ interview questions with answers | [Interview Q&A](/docs/tools/networking/interview-questions) |

---

## Contributing

Know great Networking resources? Submit a PR to help the community learn!
