---
title: "Networking Interview Questions"
description: "40+ networking interview questions with detailed answers (beginner to advanced)"
sidebar_label: "Interview Q&A"
sidebar_position: 5
---

# Networking Interview Questions

Prepare for networking roles with these common interview questions and detailed answers.

## Beginner Level

### 1. What is the OSI model and why is it important?

**Answer:**

The OSI (Open Systems Interconnection) model is a 7-layer conceptual framework that standardizes network communication functions:

| Layer | Name | Function |
|:------|:-----|:---------|
| 7 | Application | User applications (HTTP, SSH, SMTP) |
| 6 | Presentation | Data formatting, encryption, compression |
| 5 | Session | Dialog control, session management |
| 4 | Transport | Reliable delivery (TCP/UDP) |
| 3 | Network | Routing, logical addressing (IP) |
| 2 | Data Link | MAC addressing, frame formatting |
| 1 | Physical | Electrical signals and cables |

**Importance:**
- Standardizes communication between different vendors
- Helps troubleshoot issues layer by layer
- Provides common language for networking professionals
- Models how data flows through network stack

**Remember:** "Please Do Not Throw Sausage Pizza Away"

### 2. What are the differences between TCP and UDP?

**Answer:**

| Characteristic | TCP | UDP |
|:---------------|:----|:----|
| **Connection** | Connection-oriented (3-way handshake) | Connectionless (no handshake) |
| **Reliability** | Reliable delivery guaranteed | Best-effort delivery (may lose packets) |
| **Ordering** | Packets arrive in order | Order not guaranteed |
| **Speed** | Slower (more overhead) | Faster (minimal overhead) |
| **Overhead** | Higher (headers, ACKs) | Lower |
| **Flow Control** | Yes (prevents sender overwhelming receiver) | No |
| **Congestion Control** | Yes (adapts to network conditions) | No |
| **Use Cases** | Email, Web, File transfer, SSH | DNS, Video streaming, Gaming, VoIP |
| **Port Example** | 22 (SSH), 80 (HTTP), 443 (HTTPS) | 53 (DNS), 5004 (RTP) |

**Decision Rule:**
- Use TCP when data **must** arrive intact and in order
- Use UDP when **speed** matters more than reliability

### 3. What is IP subnetting and why is it needed?

**Answer:**

IP subnetting is dividing a large network into smaller logical subnetworks (subnets).

**Why it's needed:**

1. **Efficient IP allocation** — Use IPs only when needed
2. **Security** — Isolate departments/zones
3. **Performance** — Reduce broadcast domains
4. **Management** — Easier to organize and maintain
5. **Scalability** — Support network growth

**Example:**

```
Network: 192.168.1.0/24 (256 hosts)
Need 3 subnets with ~80 hosts each

Solution: Use /25 (128 hosts per subnet)

Subnet 1: 192.168.1.0/25 (hosts: .1-.126)
Subnet 2: 192.168.1.128/25 (hosts: .129-.254)
```

**CIDR Notation:**
- `/24` = 24 bits for network, 8 bits for hosts = 254 usable IPs
- `/25` = 25 bits for network, 7 bits for hosts = 126 usable IPs
- `/26` = 26 bits for network, 6 bits for hosts = 62 usable IPs

### 4. Explain what DNS is and how it works.

**Answer:**

DNS (Domain Name System) translates human-readable domain names to IP addresses.

**How DNS works (Recursive Query):**

1. User types `example.com` in browser
2. Browser queries **local resolver** (ISP's DNS server)
3. Resolver queries **root nameserver** ("Where is .com?")
4. Root directs to **TLD nameserver** for `.com`
5. TLD directs to **authoritative nameserver** for example.com
6. Authoritative returns IP `93.184.216.34`
7. Browser receives IP and connects to web server

**DNS Hierarchy:**

```
Root Nameserver (.)
    ↓
TLD Nameserver (.com)
    ↓
Authoritative Nameserver (example.com)
    ↓
A Record: example.com = 93.184.216.34
```

**Key Points:**
- Uses UDP port 53 (fast, connectionless)
- Results cached at each level for performance
- MX records point to mail servers
- CNAME creates aliases
- DNSSEC adds security through signing

### 5. What is ARP and what problem does it solve?

**Answer:**

ARP (Address Resolution Protocol) maps IP addresses (Layer 3) to MAC addresses (Layer 2) on the same network segment.

**The Problem:**

Devices on a LAN know each other's IP addresses but need MAC addresses to send frames. IP routing only works for remote networks; locally, data is sent via MAC address.

**How ARP Works:**

1. Host A wants to send to IP `192.168.1.50` (same subnet)
2. Host A broadcasts: "Who has `192.168.1.50`?"
3. Host with `192.168.1.50` responds: "I have `192.168.1.50`, my MAC is `aa:bb:cc:dd:ee:ff`"
4. Host A caches this mapping and sends frames to that MAC address

**ARP Table:**

```bash
192.168.1.1     aa:bb:cc:dd:ee:ff
192.168.1.50    11:22:33:44:55:66
192.168.1.100   ff:ff:ff:ff:ff:ff
```

**Security Concern:**

ARP doesn't verify responses, so attackers can spoof ARP replies to redirect traffic (ARP spoofing).

**Defense:**
- Static ARP entries for critical servers
- ARP inspection on switches
- Network monitoring

### 6. What is DHCP and how does it work?

**Answer:**

DHCP (Dynamic Host Configuration Protocol) automatically assigns IP addresses and network configuration to devices.

**DORA Process (4 Steps):**

1. **Discover** — Client broadcasts: "I need an IP"
2. **Offer** — DHCP server responds: "I can offer you `192.168.1.100`"
3. **Request** — Client accepts: "I want `192.168.1.100`"
4. **Acknowledge** — Server confirms: "OK, it's yours for 24 hours"

**DHCP Lease Timeline:**

```
T=0%:   Lease issued (24 hours)
T=50%:  Client tries to renew (12 hours)
T=87.5%: Client accepts any offer (21 hours)
T=100%: Lease expires, IP reclaimed
```

**Benefits:**
- Reduces manual configuration
- Prevents IP conflicts
- Efficient address management
- Flexibility for mobile/temporary devices

**Limitations:**
- Dependency on DHCP server availability
- IP changes can break static DNS entries
- Less control over IP allocation

### 7. What are the different types of routing?

**Answer:**

**Static Routing:**
- Administrator manually configures routes
- Use case: Small networks, default routes
- Advantages: Simple, no overhead
- Disadvantages: Not scalable, no failover

```bash
ip route 192.168.1.0 255.255.255.0 10.0.0.2
```

**Dynamic Routing:**
- Routers learn routes automatically via protocols
- Adapts to network changes

**Interior Routing (IGP - within AS):**
- **RIP** (Hop count) — Slow, max 15 hops
- **OSPF** (Link-state) — Fast, scalable, industry standard
- **EIGRP** (Hybrid) — Fast, Cisco-native

**Exterior Routing (EGP - between AS):**
- **BGP** (Path-vector) — Internet backbone, policy-based

**When to use:**
- **Static:** Small networks, edge routes
- **OSPF:** Medium-large networks, fast convergence needed
- **BGP:** Multi-ISP, Internet connectivity

---

## Intermediate Level

### 8. Explain the difference between routing and switching.

**Answer:**

| Aspect | Routing | Switching |
|:-------|:--------|:----------|
| **Layer** | Network (Layer 3) | Data Link (Layer 2) |
| **Address Type** | IP address (logical) | MAC address (physical) |
| **Device** | Router | Switch |
| **Scope** | Between different networks | Within same network (segment) |
| **Forwarding** | Based on IP destination | Based on MAC destination |
| **Speed** | Slower (IP lookup) | Faster (MAC table lookup) |
| **Broadcast** | Blocked at boundaries | Flooded within segment |

**Example:**

```
Host A (192.168.1.10)          Host B (192.168.2.10)
       ↓                                ↓
   SWITCH                          SWITCH
       ↓                                ↓
   ROUTER (routing between networks)
```

- Switch: A to A's switch (Layer 2, MAC)
- Router: A's switch to B's switch (Layer 3, IP)
- Switch: Router to B (Layer 2, MAC)

**Key Point:** Switches are "dumb" (just forward frames); routers are "smart" (route packets intelligently).

### 9. What is VLAN and what problems does it solve?

**Answer:**

VLAN (Virtual Local Area Network) logically divides a physical switch into multiple isolated networks.

**Problems it solves:**

1. **Security** — Isolate sensitive traffic
2. **Broadcast Control** — Reduce broadcast domains (each VLAN is separate domain)
3. **Performance** — Limit broadcast traffic impact
4. **Flexibility** — Users can move without recabling
5. **Management** — Organize users by function not location

**Example:**

```
Physical Switch (3 VLANs)

Port 1-8:   VLAN 10 (Engineering)
Port 9-16:  VLAN 20 (Sales)
Port 17-24: VLAN 30 (Finance)

Broadcast in VLAN 10 only reaches ports 1-8
Sales users never see Engineering broadcasts
```

**Inter-VLAN Communication:**

Requires Layer 3 routing (either router or Layer 3 switch).

```bash
# Configure inter-VLAN routing
interface Vlan10
ip address 10.10.1.1 255.255.255.0

interface Vlan20
ip address 10.20.1.1 255.255.255.0

# Enable routing
ip routing
```

### 10. What is a VLAN trunk and how does it differ from an access port?

**Answer:**

| Feature | Access Port | Trunk Port |
|:--------|:-----------|:-----------|
| **VLANs** | Single VLAN | Multiple VLANs |
| **Tagging** | No tags | 802.1Q tags |
| **Use** | End devices (PCs, printers) | Switch-to-switch, switch-to-router |
| **Frame Format** | Untagged | Tagged with VLAN ID |
| **Example** | Port connected to PC | Port connecting two switches |

**Access Port:**

```bash
interface FastEthernet0/1
switchport mode access
switchport access vlan 10
# Only VLAN 10 traffic on this port
```

**Trunk Port:**

```bash
interface GigabitEthernet0/1
switchport mode trunk
switchport trunk allowed vlan 10,20,30
switchport trunk native vlan 1
# Multiple VLAN traffic on this port
# Native VLAN = untagged traffic (for backward compatibility)
```

**802.1Q Tag:**

```
Ethernet Frame + 4-byte tag (VLAN ID + priority)
+--------+--------+-----+------+--------+-----+
| Dest   | Source | Tag | Type | Data   | FCS |
| MAC    | MAC    |VLAN |      |        |     |
+--------+--------+-----+------+--------+-----+
```

### 11. Explain Spanning Tree Protocol (STP) and why it's needed.

**Answer:**

STP prevents loops in redundant switched networks, ensuring only one active path between switches.

**Why it's needed:**

In a switch with redundant links, frames would loop forever:

```
    ┌─────────────────┐
    │     Switch A    │
    │                 │
    └────┬─────────┬──┘
         │ Link1   │ Link2
         │         │
    ┌────┴──────┬──┴────┐
    │   Switch B │  C    │
    └────┬──────┴───────┘
    Frames loop A→B→C→A→B→C...
```

**STP Solution:**

Blocks one link, maintaining redundancy but preventing loops.

**How STP Works:**

1. **Root Bridge Election** — Lowest bridge ID becomes root
2. **Root Port Selection** — Each bridge picks shortest path to root
3. **Designated Port Selection** — Per segment, one designated port
4. **Block Remaining Ports** — Prevent loops

**Port States:**

```
Disabled → Blocking (20s) → Listening (15s) → Learning (15s) → Forwarding
         Total Convergence Time: ~50 seconds
```

**Modern Improvement - RSTP (Rapid STP):**

- Faster convergence (~1-2 seconds vs 50 seconds)
- Uses `rapid-pvst` instead of `pvst`
- Backward compatible with STP

### 12. What is NAT and when is it used?

**Answer:**

NAT (Network Address Translation) translates private IP addresses to public IP addresses.

**Why it's used:**

1. **IP Conservation** — Private networks need fewer public IPs
2. **Security** — Hides internal network structure
3. **Multi-vendor Networks** — Enables connectivity between incompatible networks

**Three Types:**

**Static NAT** (1-to-1):
```
Private:  10.0.0.5 ↔ Public: 203.0.113.100
Always same mapping for same private IP
```

**Dynamic NAT** (Many-to-Many):
```
10.0.0.0/24 → 203.0.113.100-110
Temporary mappings from pool
```

**PAT/Overloading** (Many-to-One):
```
Multiple private IPs → Single public IP (with port numbers)
10.0.0.5:40000 → 203.0.113.100:50000
10.0.0.6:40001 → 203.0.113.100:50001
```

**Configuration Example:**

```bash
# Mark interfaces
interface GigabitEthernet0/0
ip nat inside

interface Serial0/0
ip nat outside

# Define private range
access-list 1 permit 10.0.0.0 0.0.0.255

# Configure PAT
ip nat inside source list 1 interface Serial0/0 overload
```

### 13. What is the difference between IPv4 and IPv6?

**Answer:**

| Feature | IPv4 | IPv6 |
|:--------|:-----|:-----|
| **Address Size** | 32-bit | 128-bit |
| **Address Space** | ~4.3 billion | ~340 undecillion |
| **Format** | Dotted decimal (192.168.1.1) | Hex colon (2001:db8::1) |
| **Classes** | Classes A, B, C | No classes (CIDR only) |
| **Header Size** | 20-60 bytes | 40 bytes (fixed) |
| **Broadcast** | Yes | No (uses multicast) |
| **IPsec** | Optional | Mandatory |
| **Fragmentation** | Host & router | Host only |
| **Loopback** | 127.0.0.1 | ::1 |
| **Adoption** | Near universal (98%+) | Growing (~35% 2024) |

**IPv6 Special Addresses:**

| Address | Purpose |
|:--------|:--------|
| ::1/128 | Loopback |
| ::/128 | Default route |
| fe80::/10 | Link-local (autoconfigured) |
| fc00::/7 | Unique local (private) |
| 2000::/3 | Global unicast |
| ff00::/8 | Multicast |

**Migration Strategies:**

- **Dual Stack:** Run IPv4 and IPv6 simultaneously
- **6to4 Tunneling:** Tunnel IPv6 in IPv4
- **NAT64:** Translate IPv6 to IPv4

### 14. Explain the TCP three-way handshake.

**Answer:**

TCP establishes a connection through a three-way handshake (SYN, SYN-ACK, ACK):

**Step 1: SYN (Client)**

```
Client → Server
SYN, SEQ=100
"I want to connect, my sequence number is 100"
```

**Step 2: SYN-ACK (Server)**

```
Server → Client
SYN, ACK, SEQ=300, ACK=101
"I acknowledge (101 = 100+1), my sequence is 300"
```

**Step 3: ACK (Client)**

```
Client → Server
ACK, SEQ=101, ACK=301
"I acknowledge (301 = 300+1), connection established"
```

**Result:** Connection established, both sides can send/receive data.

**Connection Termination (4-Way Handshake):**

```
Client → Server: FIN (I'm done sending)
Server → Client: ACK (I got it)
Server → Client: FIN (I'm done sending)
Client → Server: ACK (I got it)
Result: Connection closed
```

**Tcpdump example:**

```bash
# Capture handshake
sudo tcpdump -i eth0 "tcp[tcpflags] & (tcp-syn) != 0"
# Shows SYN packets from handshake
```

### 15. What is a reverse proxy and how does it differ from a forward proxy?

**Answer:**

| Aspect | Forward Proxy | Reverse Proxy |
|:-------|:--------------|:--------------|
| **Location** | Client-side (sits between clients and Internet) | Server-side (sits between Internet and servers) |
| **Purpose** | Hide client IP, filter content | Hide server identity, load balance |
| **Client knows** | Proxy exists | No proxy exists (transparent) |
| **Server sees** | Proxy IP, not client IP | Proxy IP, not client IP |
| **Use case** | Corporate firewall, anonymity | Load balancing, caching, security |
| **Example** | Corporate proxy for all employees | Nginx, HAProxy, cloud CDN |

**Forward Proxy:**

```
Client → Corporate Proxy → Internet → Server
Client IP: 192.168.1.100
Server sees: Corporate Proxy IP (203.0.113.50)
```

**Reverse Proxy:**

```
Internet Client → Reverse Proxy → Internal Web Servers
Client sees: Single IP (example.com)
Proxy distributes to backend servers (10.0.0.1-3)
```

---

## Advanced Level

### 16. What is BGP and how does it differ from IGP protocols like OSPF?

**Answer:**

BGP (Border Gateway Protocol) and IGPs (Interior Gateway Protocols) operate at different scales:

| Aspect | BGP | OSPF/EIGRP |
|:-------|:----|:-----------|
| **Scope** | Between AS (Exterior) | Within AS (Interior) |
| **Metric** | Path-vector (AS-Path) | Cost/bandwidth |
| **Scalability** | Internet-scale (millions) | Enterprise-scale |
| **Convergence** | Slow (controlled, stable) | Fast |
| **Configuration** | Complex, policy-based | Simpler |
| **Purpose** | Route between ISPs | Route within network |

**BGP Decision Process:**

```
1. Highest Local Preference (policy)
2. Shortest AS-Path length
3. Origin type (IGP > EGP > Incomplete)
4. Lowest MED (Multi-Exit Discriminator)
5. eBGP vs iBGP (prefer eBGP)
6. Lowest IGP metric to next-hop
```

**Example:**

```
Your ISP: AS 65000
ISP1 neighbor: AS 65001
ISP2 neighbor: AS 65002

BGP announces your networks to ISPs
ISPs announce Internet routes to you
Router selects best path to 8.8.8.8 via either ISP
```

### 17. Explain OSPF areas and why they're used.

**Answer:**

OSPF areas reduce resource consumption in large networks by creating hierarchy.

**Single Area (Flat) Problem:**

```
200 routers all computing shortest path to all destinations
Router A must know full network topology
High memory usage, long convergence time
```

**Multi-Area Solution:**

```
         Area 0 (Backbone)
              ↓
    ┌─────────┼─────────┐
   Area 1   Area 2   Area 3
(50 routers)(60 routers)(90 routers)

Backbone connects all areas via ABR (Area Border Router)
Each area independent, ABRs summarize routes
```

**Area Types:**

| Type | Description | Use |
|:-----|:-----------|:----|
| **Backbone (Area 0)** | Must be center, all others connect to it | Core network |
| **Standard Area** | Full topology, all route types | Normal areas |
| **Stub Area** | No external routes (block EGP) | Leaf areas |
| **Totally Stubby** | No external or summary routes | Minimal routing |
| **Not-So-Stubby (NSSA)** | Can originate external routes | Branch offices |

**Configuration:**

```bash
router ospf 1

# Backbone router
network 10.0.0.0 0.0.0.255 area 0

# Area border router (interfaces in different areas)
network 192.168.1.0 0.0.0.255 area 0
network 172.16.0.0 0.0.0.255 area 1

# Route summarization at ABR
area 1 range 172.16.0.0 255.255.0.0
```

### 18. What is MPLS and what problems does it solve?

**Answer:**

MPLS (Multiprotocol Label Switching) uses short labels instead of full IP lookups for faster forwarding.

**Problems it solves:**

1. **Performance** — Label lookup faster than IP lookup
2. **Traffic Engineering** — Explicit path selection
3. **VPN Support** — MPLS L3VPN enables carrier-grade VPN
4. **QoS** — Label can carry QoS priority
5. **Simplification** — Single mechanism for all protocols (IP, IPv6, etc.)

**How MPLS Works:**

```
IP Lookup (Slow):         MPLS Lookup (Fast):
Search routing table      Look up label
Match prefix              Direct table index
Calculate next-hop        Get next-hop & new label

Performance: ~10x faster for large routing tables
```

**MPLS Label:**

```
+--------+---+---+
| Label  |EXP|S/U|  (20 bits) Label
| (20b)  |(3)|(1)|  (3 bits) EXP (QoS)
+--------+---+---+  (1 bit) Bottom of Stack
```

**MPLS VPN (L3VPN):**

```
                Carrier Network (MPLS backbone)
                        ↑
Customer A (VRF A)      Customer B (VRF B)
     ↑                        ↑
   CE-A                      CE-B
    ↑                        ↑
Provider Edge Routers (PE):
PE routers maintain separate routing tables (VRF) per customer
Traffic segregated using MPLS labels
```

### 19. What is IPsec and what does it protect against?

**Answer:**

IPsec is a framework providing encryption, authentication, and integrity for IP traffic.

**What it protects against:**

| Threat | IPsec Component | Protection |
|:-------|:----------------|:-----------|
| Eavesdropping | ESP (Encryption) | Encrypts payload |
| Tampering | AH/ESP (Authentication) | HMAC detects changes |
| Replay | Sequence Numbers | Drops old duplicates |
| Man-in-the-Middle | IKE (Key Exchange) | Authenticates peers |

**IPsec Components:**

**IKE (Internet Key Exchange):**
- Phase 1: Authenticate peers, establish secure channel
- Phase 2: Negotiate encryption parameters

**AH (Authentication Header):**
- Authenticates source and ensures integrity
- No confidentiality (doesn't encrypt)

**ESP (Encapsulating Security Payload):**
- Provides encryption, authentication, and integrity
- More commonly used than AH

**Modes:**

**Transport Mode:**
```
Only payload encrypted, preserves original headers
Source IP → Dest IP → [AH/ESP protected payload]
Use: Host-to-host
```

**Tunnel Mode:**
```
Entire packet encrypted, new outer header added
New Source → New Dest → [Original entire packet encrypted]
Use: Gateway-to-gateway, VPN
```

### 20. What are the differences between VPN and MPLS VPN?

**Answer:**

| Aspect | VPN (IPsec) | MPLS VPN |
|:-------|:-----------|:---------|
| **Type** | Customer-managed | Provider-managed |
| **Encryption** | Yes (built-in) | No (optional) |
| **Control** | Customer controls | Provider controls routing |
| **Setup** | Complex, manual | Simple, provider managed |
| **Scalability** | Mesh is complex | Provider handles |
| **Cost** | Lower (self-managed) | Higher (provider service) |
| **Security** | IPsec encryption | Relies on provider isolation |
| **Use Case** | Branch-to-branch, secure remote | Carrier service offering |

**IPsec VPN:**

```
Customer A ──────[IPsec Tunnel]──────→ Customer B
Encrypted end-to-end, customer manages keys
```

**MPLS VPN:**

```
Customer A → PE-A ─[MPLS Labels]─ PE-B → Customer B
Provider manages VRFs and labels
Not encrypted (though provider can add encryption)
```

---

## Advanced Troubleshooting

### 21. How would you troubleshoot a connectivity issue between two hosts?

**Answer:**

**Systematic approach:**

**1. Layer 1 (Physical):**

```bash
# Check if interfaces are up
ethtool eth0 | grep "Link detected"
ip link show eth0
# Should show "state UP"
```

**2. Layer 2 (Data Link):**

```bash
# Ping local gateway (ARP)
ping -c 4 gateway.ip

# Check ARP table
arp -a | grep target.ip

# Verify MAC connectivity
sudo tcpdump -i eth0 -n arp host target.ip
```

**3. Layer 3 (Network):**

```bash
# Ping target IP
ping -c 4 target.ip

# Check routing
ip route show
route -n

# Verify next hop is reachable
ping next.hop.ip

# Trace route
traceroute target.ip
```

**4. Layer 4 (Transport):**

```bash
# Check if port is open
nc -zv target.ip port

# Check local listening ports
netstat -tlnp | grep :port

# Monitor SYN/ACK
sudo tcpdump -i eth0 "tcp[tcpflags] & (tcp-syn | tcp-ack) != 0" and host target.ip
```

**5. Layer 5-7 (Application):**

```bash
# Test HTTP
curl -v http://target.ip:port

# Test SSH
ssh -vvv user@target.ip

# Verbose application logging
```

### 22. A web server is slow. What would you check?

**Answer:**

**Network Issues:**

```bash
# Bandwidth utilization
iftop -i eth0

# Packet loss
ping -c 100 webserver.ip | grep loss

# Latency
mtr webserver.ip

# TCP window size (congestion)
netstat -tan | grep ESTABLISHED | awk '{sum+=$3} END {print sum}'
```

**Connection Issues:**

```bash
# Connection count
netstat -tan | grep ESTABLISHED | wc -l

# Half-open connections (possible SYN flood)
netstat -tan | grep SYN_RECV | wc -l

# Time wait states (closing connections)
netstat -tan | grep TIME_WAIT | wc -l
```

**Routing Issues:**

```bash
# MTU size (fragmentation?)
ping -D -s 1500 webserver.ip

# Route to server
traceroute webserver.ip

# DNS resolution time
time dig webserver.com
```

**Possible causes:**
- High bandwidth consumption (congestion)
- Packet loss (check ISP)
- MTU mismatch (fragmentation)
- Connection table full
- DDoS attack
- Application bottleneck (not network)

---

## Scenario-Based Questions

### 23. Design a network for a company with 3 offices (100 users each).

**Answer:**

```
HQ (Office 1)              Branch 1              Branch 2
100 users                  100 users             100 users
192.168.1.0/24            192.168.2.0/24        192.168.3.0/24

            │─────────┐
            │         │
         Router A  Branch Routers
            │         │
            └────┬────┘
                 │
           (Site-to-Site VPN
            or MPLS VPN)
```

**Design Decisions:**

1. **Routing:** OSPF (fast convergence, good for multiple sites)
2. **Inter-office connection:** IPsec VPN or dedicated MPLS circuit
3. **Redundancy:** Dual connections between sites
4. **VLANs:** Separate departments at each site
5. **DNS:** Redundant DNS servers at HQ
6. **DHCP:** Distributed, local DHCP per site

**Configuration (HQ Router):**

```bash
# Enable routing
router ospf 1
network 192.168.1.0 0.0.0.255 area 0
network 10.0.0.0 0.0.0.255 area 0

# VPN to Branch 1
crypto isakmp policy 1
authentication pre-share
encryption aes 256
hash sha256

# Inter-office VPN tunnel
```

### 24. What would you do if a user's internet is down?

**Answer:**

**Quick diagnosis (5 minutes):**

```bash
# 1. Check physical connection
ethtool eth0 | grep "Link detected"

# 2. Check local IP assignment
ip addr show eth0

# 3. Ping gateway
ping 192.168.1.1

# 4. Ping ISP DNS
ping 8.8.8.8

# 5. Check routing table
ip route show

# 6. Test DNS
dig google.com @8.8.8.8
```

**If local network is up but internet down:**

```bash
# Check ISP connectivity
mtr 8.8.8.8

# Check ISP-facing interface
ip link show | grep wan

# Check gateway logs (if accessible)
ssh gateway.ip 'tail -f /var/log/messages'

# Contact ISP if line is down
```

**If local network is down:**

```bash
# Check DHCP lease
dhclient -v eth0

# Check gateway ARP
arp -a | grep gateway

# Check switch port status
# (if enterprise)
```

### 25. How would you design a network security architecture?

**Answer:**

**Defense in Depth Layers:**

```
┌─────────────────────────────────────┐
│ External Attacker                   │
└────────────────┬────────────────────┘
                 │
    ┌────────────▼───────────┐
    │  ISP/DDoS Mitigation   │
    └────────────┬───────────┘
                 │
    ┌────────────▼──────────────┐
    │  Border Firewall (NGFW)   │
    │  - Stateful inspection    │
    │  - IPS/IDS                │
    └────────────┬──────────────┘
                 │
    ┌────────────▼──────────────┐
    │  Network Segmentation     │
    │  - DMZ (Web servers)      │
    │  - Internal (Apps)        │
    │  - Sensitive (Data)       │
    └────────────┬──────────────┘
                 │
    ┌────────────▼──────────────┐
    │  Internal Firewalls       │
    │  - Zone-based             │
    │  - VLAN isolation         │
    └────────────┬──────────────┘
                 │
    ┌────────────▼──────────────┐
    │  Endpoint Security        │
    │  - Personal firewall      │
    │  - Antivirus              │
    │  - EDR                    │
    └────────────┬──────────────┘
                 │
    ┌────────────▼──────────────┐
    │  Data Protection          │
    │  - Encryption             │
    │  - Access control         │
    │  - DLP                    │
    └──────────────────────────┘
```

**Specific Components:**

1. **Perimeter Security:**
   - Border firewall (Palo Alto, Fortinet, Check Point)
   - DDoS protection
   - WAF for web servers

2. **Network Segmentation:**
   - DMZ with restricted access
   - VLANs for departments
   - Air-gap for sensitive systems
   - ACLs between zones

3. **Identity & Access:**
   - 802.1X for network access
   - RADIUS authentication
   - Role-based access control

4. **Monitoring:**
   - SIEM (ELK, Splunk, ArcSight)
   - Network Flow analysis (NetFlow)
   - IDS/IPS (Suricata, Snort)

5. **Encryption:**
   - IPsec for VPN
   - TLS for HTTPS
   - SSH for management

---

## Summary

**For interviews, focus on:**

1. **Fundamentals** — OSI, TCP/IP, IP addressing
2. **Protocols** — TCP vs UDP, DNS, DHCP, ARP
3. **Routing** — Static vs dynamic, IGP vs EGP
4. **Security** — Firewalls, VPN, IPsec, ACLs
5. **Troubleshooting** — Systematic layer-by-layer approach
6. **Design** — Multi-site, redundancy, security

**Practice with real scenarios** and always ask clarifying questions during interviews.
