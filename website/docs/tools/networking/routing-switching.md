---
title: "Routing and Switching"
description: "Routing protocols (OSPF, BGP, EIGRP, RIP, IS-IS), VLANs, spanning tree, and WAN technologies"
sidebar_label: "Routing & Switching"
sidebar_position: 2
---

# Routing and Switching

Explore how routers and switches forward data across networks using protocols and algorithms that enable global Internet connectivity.

## Routing Fundamentals

### Forwarding vs. Routing

**Forwarding** — The action of moving a packet from input port to output port on a single router based on forwarding table lookups.

**Routing** — The process of determining the path packets take from source to destination across multiple routers using routing protocols.

### Forwarding Table

Each router maintains a **forwarding table** that maps destination addresses (or prefixes) to outbound links. When a packet arrives, the router:
1. Examines destination IP address
2. Searches forwarding table for matching prefix
3. Forwards packet to corresponding outbound link

### Router Packet Processing

**Input Processing**
- Receive packet from input link
- Decrement TTL
- Check version and header length
- Perform checksum verification

**Lookup (Longest Prefix Matching)**
- Search routing table for longest matching prefix
- Determine output port
- Handle destination unreachable if no match

**Output Processing**
- Queue packet in output buffer
- Transmit to output link
- Handle congestion and packet loss

## Interior Routing Protocols

Interior routing protocols operate within a single autonomous system (AS).

### RIP (Routing Information Protocol)

**Characteristics**
- Distance-vector algorithm
- Maximum hop count: 15 (limits to small networks)
- Metric: Number of hops
- Updates every 30 seconds
- Slow convergence time
- Oldest routing protocol (deprecated)

**Advantages**
- Simple to understand and implement
- Low overhead on modern networks

**Disadvantages**
- Maximum hop limit of 15
- Slow convergence (up to 3+ minutes)
- Higher bandwidth overhead
- Poor scalability

**Configuration Example (Cisco)**
```bash
# Enable RIP
router rip
version 2
network 10.0.0.0
network 172.16.0.0
passive-interface FastEthernet0/0
```

### OSPF (Open Shortest Path First)

**Characteristics**
- Link-state algorithm
- Metric: Cost (default based on link bandwidth)
- Hello packets every 10 seconds
- No maximum hop count
- Fast convergence (typically under 1 minute)
- Industry standard for large networks

**OSPF Areas**
- **Backbone (Area 0)** — All other areas must connect to it
- **Standard Area** — Connected to backbone
- **Stub Area** — No external routes (default routes only)
- **Totally Stubby Area** — No external or summary routes

**Configuration Example (Cisco)**
```bash
# Enable OSPF
router ospf 1
network 10.0.0.0 0.0.0.255 area 0
network 172.16.0.0 0.0.0.255 area 0
auto-cost reference-bandwidth 10000

# Set router priority (higher = more preferred as DR)
interface FastEthernet0/0
ip ospf priority 255
```

**Designated Router (DR)**
- Elected per broadcast segment
- Receives all OSPF hellos
- Performs LSA flooding
- Reduces overhead on multi-access networks

### EIGRP (Enhanced Interior Gateway Routing Protocol)

**Characteristics**
- Hybrid protocol (distance-vector + link-state)
- Metric: Bandwidth, delay, reliability, load, MTU
- Hello packets every 5-60 seconds (configurable)
- Fast convergence
- Cisco proprietary (though open-sourced)

**EIGRP Concepts**
- **Feasible Successor** — Backup route to same destination
- **Reported Distance (RD)** — Distance reported by neighboring router
- **Feasible Distance (FD)** — Best distance to destination through this router

**Configuration Example (Cisco)**
```bash
# Enable EIGRP
router eigrp 100
network 10.0.0.0 0.0.0.255
network 172.16.0.0 0.0.0.255
eigrp log-neighbor-changes

# Set metric weights (default K1=1, K2=0, K3=1, K4=0, K5=0)
metric weights 0 1 0 1 0 0
```

### IS-IS (Intermediate System to Intermediate System)

**Characteristics**
- Link-state protocol (similar to OSPF)
- Metric: Cost (default 10 per interface)
- No hop limit
- Efficient for very large networks
- Runs directly over data link layer (not IP)

**Advantages**
- Faster convergence than OSPF
- Suitable for very large networks
- Low bandwidth overhead

**Disadvantages**
- More complex to configure
- Less common than OSPF
- Steeper learning curve

### Comparison Table

| Feature | RIP | OSPF | EIGRP | IS-IS |
|:--------|:----|:-----|:------|:------|
| Algorithm | Distance-Vector | Link-State | Hybrid | Link-State |
| Max Hop Count | 15 | Unlimited | Unlimited | Unlimited |
| Metric | Hops | Cost | Composite | Cost |
| Convergence | Slow (180s+) | Fast (60s) | Very Fast (10s) | Very Fast |
| Overhead | High | Medium | Medium | Low |
| Scalability | Poor | Good | Very Good | Excellent |
| Cisco Only | No | No | Yes | No |

## Exterior Routing Protocols

Exterior routing protocols operate between autonomous systems (BGP).

### BGP (Border Gateway Protocol)

**Characteristics**
- Path-vector protocol
- Metric: AS-Path (prefer shorter paths)
- TCP-based (port 179)
- Slow convergence by design (stability over speed)
- Enables Internet-scale routing
- Supports policy-based routing

**BGP Concepts**

| Term | Meaning |
|:-----|:--------|
| **AS (Autonomous System)** | Network under single administration |
| **ASN** | 16-bit or 32-bit AS number |
| **iBGP** | BGP within same AS |
| **eBGP** | BGP between different AS |
| **RIB** | Routing Information Base |
| **BGP Community** | Group routers for policies |

**BGP Attributes (Path Selection)**
1. Preference (local preference)
2. AS-Path length (shorter is better)
3. Origin (IGP > EGP > Incomplete)
4. MED (Multi-Exit Discriminator)
5. Next-hop reachability

**Configuration Example (Cisco)**
```bash
# Enable BGP
router bgp 65000
bgp log-neighbor-changes
neighbor 203.0.113.1 remote-as 65001
neighbor 203.0.113.1 description "ISP Connection"

# Advertise networks
network 10.0.0.0 mask 255.0.0.0
network 172.16.0.0 mask 255.240.0.0

# Set local preference (higher = preferred)
route-map SET_PREF permit 10
set local-preference 200
```

## Static Routing

**When to Use**
- Small networks with few routes
- Default routes to edge networks
- Backup routes for high-priority traffic
- Testing and troubleshooting

**Configuration Example (Cisco)**
```bash
# Default route
ip route 0.0.0.0 0.0.0.0 203.0.113.1

# Specific network route
ip route 192.168.1.0 255.255.255.0 10.0.0.2

# With administrative distance (higher = less preferred)
ip route 10.1.0.0 255.255.0.0 10.0.0.3 120
```

## VLANs (Virtual Local Area Networks)

### Purpose

Segment a physical switch into multiple logical networks to improve security, performance, and management.

### VLAN Benefits

| Benefit | Description |
|:--------|:------------|
| **Security** | Isolate traffic between departments |
| **Performance** | Reduce broadcast domains |
| **Management** | Flexible network organization |
| **Scalability** | Add users without infrastructure changes |
| **Cost** | Minimize cabling and equipment |

### VLAN Configuration Example (Cisco)

```bash
# Create VLAN
vlan 10
name Engineering
description Engineering Department

vlan 20
name Sales
description Sales Department

# Assign port to VLAN (access port)
interface FastEthernet0/1
switchport mode access
switchport access vlan 10

# Configure SVI (Switch Virtual Interface) for routing
interface Vlan10
ip address 10.10.1.1 255.255.255.0

interface Vlan20
ip address 10.20.1.1 255.255.255.0

# Enable routing between VLANs
ip routing
```

### 802.1Q Tagging (Trunking)

**Purpose** — Carry multiple VLAN traffic over single link

**VLAN Tag Format**
- 4-byte tag inserted in Ethernet frame
- Contains VLAN ID (12 bits = 4096 VLANs max)
- Priority bits for QoS
- Type field

**Trunk Configuration (Cisco)**
```bash
interface GigabitEthernet0/1
switchport mode trunk
switchport trunk allowed vlan 10,20,30
switchport trunk native vlan 10
```

## Spanning Tree Protocol (STP)

### Purpose

Prevents loops in switched networks with redundant links, while maintaining fault tolerance.

### How STP Works

1. **Bridge Election** — Select root bridge (lowest bridge ID)
2. **Root Port Selection** — Each bridge selects port with lowest cost to root
3. **Designated Port Selection** — Per segment, select port closest to root
4. **Block Remaining Ports** — All other ports blocked to prevent loops

### Port States

| State | Duration | Description |
|:------|:---------|:------------|
| Disabled | N/A | Port is shutdown |
| Blocking | 20s | Listening for BPDUs, not forwarding |
| Listening | 15s | Preparing to forward |
| Learning | 15s | Learning MAC addresses |
| Forwarding | Indefinite | Forwarding frames |

### STP Configuration (Cisco)

```bash
# Set bridge priority (lower = more preferred as root)
spanning-tree priority 4096

# Set port cost (lower = preferred)
interface FastEthernet0/1
spanning-tree cost 19

# Enable BPDU guard (err-disable port if illegal BPDU received)
spanning-tree bpduguard enable

# Rapid STP (RSTP) for faster convergence
spanning-tree mode rapid-pvst
```

## MPLS (Multiprotocol Label Switching)

### Purpose

- Simplify routing by using labels instead of IP lookups
- Enable traffic engineering and QoS
- Support VPN (MPLS L3VPN)
- Improve performance in core networks

### MPLS Concepts

**Label** — Short fixed-length identifier (20 bits) replacing IP lookup

**LSP (Label Switched Path)** — Path through MPLS network

**LER (Label Edge Router)** — Entry/exit point of MPLS network

**LSR (Label Switch Router)** — Core router in MPLS network

**FEC (Forwarding Equivalence Class)** — Group of packets treated identically

### MPLS Configuration Example (Cisco)

```bash
# Enable MPLS globally
mpls label range 100 199

# Enable on interface
interface GigabitEthernet0/0
mpls ip

# Configure LDP (Label Distribution Protocol)
mpls ldp neighbor 10.0.0.2 password cisco123
```

## NAT (Network Address Translation)

### Purpose

- Conserve public IP addresses
- Hide internal network structure
- Enable private networks

### NAT Types

**Static NAT** — One-to-one mapping
```bash
ip nat inside source static 10.0.0.5 203.0.113.100
```

**Dynamic NAT** — Pool of public IPs for private hosts
```bash
access-list 1 permit 10.0.0.0 0.0.0.255
ip nat pool PUBIPS 203.0.113.100 203.0.113.110 netmask 255.255.255.0
ip nat inside source list 1 pool PUBIPS
```

**PAT (Port Address Translation)** — Many-to-one with port multiplexing
```bash
ip nat inside source list 1 interface GigabitEthernet0/0 overload
```

### NAT Configuration (Cisco)

```bash
# Mark inside interface
interface FastEthernet0/0
ip nat inside

# Mark outside interface
interface Serial0/0
ip nat outside

# Configure translation
access-list 1 permit 10.0.0.0 0.0.0.255
ip nat inside source list 1 interface Serial0/0 overload
```

## WAN Technologies

### Circuit-Switched Networks

**Example: ISDN**
- Dial-on-demand connectivity
- Suited for low-bandwidth, intermittent use
- Cost: Per-minute charges

**Example: PSTN**
- Traditional telephone networks
- Limited to voice and low-speed data

### Packet-Switched Networks

**Frame Relay**
- PVC (Permanent Virtual Circuits) or SVC (Switched Virtual Circuits)
- CIR (Committed Information Rate)
- Bandwidth efficient, lower cost than leased lines
- Largely replaced by modern technologies

**ATM (Asynchronous Transfer Mode)**
- Fixed 53-byte cells
- QoS guarantees
- Complex, rarely deployed for new networks

**MPLS VPN (L3VPN)**
- VPN services over MPLS backbone
- Provider-managed routing
- Carrier-grade security and QoS

### Modern WAN Options

**Metro Ethernet**
- Ethernet service over metro area
- Simple, standardized, growing adoption

**Direct Internet Access (DIA)**
- Dedicated Internet connection
- High speed, low latency
- Cost depends on bandwidth

**SD-WAN (Software-Defined WAN)**
- Cloud-based WAN orchestration
- Multiple link types (broadband, LTE, MPLS)
- Centralized management
- Improved application performance

## Exercises

### Exercise 1: Shortest Path Calculation

**Q:** Given a network with routers A-B(cost 1), B-C(cost 3), A-C(cost 5), find shortest path A to C.

**A:**
- Direct: A → C (cost 5)
- Via B: A → B → C (cost 1+3=4)
- **Shortest path: A → B → C (cost 4)**

### Exercise 2: OSPF Area Design

**Q:** Design OSPF for a company with 3 buildings (Building A: 50 routers, Building B: 30 routers, Building C: 20 routers).

**A:**
- Area 0 (Backbone): Building A
- Area 1: Building B connected to Backbone
- Area 2: Building C connected to Backbone
- Use ABR (Area Border Router) to interconnect areas

### Exercise 3: VLAN Configuration

**Q:** Create VLANs for departments: Engineering (VLAN 10), Sales (VLAN 20), Finance (VLAN 30).

**A:**
```bash
# Create VLANs
vlan 10
name Engineering

vlan 20
name Sales

vlan 30
name Finance

# Assign access ports
interface range Fa0/1-10
switchport access vlan 10

interface range Fa0/11-20
switchport access vlan 20

interface range Fa0/21-30
switchport access vlan 30

# Create inter-VLAN routing
interface Vlan10
ip address 10.10.1.1 255.255.255.0
```

## Summary

Routing and switching are fundamental to network architecture. Key takeaways:
- Interior protocols (OSPF, EIGRP, RIP) route within an AS
- BGP routes between autonomous systems
- VLANs logically segment physical networks
- Spanning Tree prevents loops
- MPLS enables traffic engineering
- NAT conserves IP addresses
- WAN technologies connect remote sites
