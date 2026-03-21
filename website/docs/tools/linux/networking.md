---
title: "Linux Networking"
sidebar_label: "Networking"
description: "Linux networking — interfaces, routing, DNS, firewalls, troubleshooting, and network services configuration"
sidebar_position: 4
---

# Linux Networking

Master Linux networking from fundamentals to advanced troubleshooting. Configure interfaces, manage routing and DNS, secure with firewalls, and diagnose connectivity issues.

---

## Networking Fundamentals

### OSI Model Quick Reference

| Layer | Name | Function | Protocols |
|-------|------|----------|-----------|
| 7 | Application | User applications | HTTP, HTTPS, FTP, SSH, DNS, SMTP |
| 6 | Presentation | Data encryption, compression | SSL/TLS, JPEG, GIF |
| 5 | Session | Session management | RPC, NetBIOS |
| 4 | Transport | End-to-end communication | TCP, UDP, SCTP |
| 3 | Network | Routing, IP addressing | IP (IPv4, IPv6), ICMP, IGMP |
| 2 | Data Link | MAC addressing, switching | Ethernet, WiFi, PPP, ARP |
| 1 | Physical | Physical transmission | Cables, hubs, repeaters |

### TCP vs UDP

| Aspect | TCP | UDP |
|--------|-----|-----|
| Connection | Connection-oriented | Connectionless |
| Reliability | Guaranteed delivery | Best effort |
| Ordering | In-order delivery | No ordering guarantee |
| Speed | Slower (overhead) | Faster |
| Header Size | 20-60 bytes | 8 bytes |
| Use Cases | Web, email, FTP, SSH | Streaming, DNS, gaming, VoIP |
| Examples | HTTPS (443), SSH (22) | DNS (53), NTP (123), RTP |

### IP Addressing

**IPv4 Basics:**
- Format: `192.168.1.100` (4 octets, 32 bits)
- Range: 0.0.0.0 to 255.255.255.255
- Special addresses:
  - `127.0.0.1` - Loopback/localhost
  - `0.0.0.0` - Default route
  - `255.255.255.255` - Broadcast
  - `169.254.x.x` - Link-local (APIPA)

**IPv6 Basics:**
- Format: `2001:db8::1` (hexadecimal, 128 bits)
- Loopback: `::1`
- Unspecified: `::`

### Subnetting & CIDR Notation

```
CIDR notation: IP/prefix_length
192.168.1.0/24 means:
- Network address: 192.168.1.0
- Netmask: 255.255.255.0
- Host bits: 32 - 24 = 8 bits
- Usable hosts: 2^8 - 2 = 254
- Range: 192.168.1.1 to 192.168.1.254
- Broadcast: 192.168.1.255
```

**Common Netmasks:**
- `/24` = 255.255.255.0 (256 addresses, 254 usable)
- `/25` = 255.255.255.128 (128 addresses)
- `/16` = 255.255.0.0 (65,536 addresses)
- `/8` = 255.0.0.0 (16 million addresses)

### Common Ports

| Port | Service | Protocol |
|------|---------|----------|
| 20, 21 | FTP | TCP |
| 22 | SSH | TCP |
| 23 | Telnet | TCP |
| 25, 587, 465 | SMTP | TCP |
| 53 | DNS | TCP/UDP |
| 80 | HTTP | TCP |
| 110 | POP3 | TCP |
| 123 | NTP | UDP |
| 143 | IMAP | TCP |
| 443 | HTTPS | TCP |
| 3306 | MySQL | TCP |
| 5432 | PostgreSQL | TCP |
| 6379 | Redis | TCP |
| 8080 | Alternative HTTP | TCP |

---

## Network Interfaces

### Viewing Interface Information

```bash
# Modern approach - using ip command
ip addr show                    # Show all interfaces with IPs
ip addr show eth0               # Show specific interface
ip link show                    # Show link status (up/down)
ip route show                   # Show routing table

# Legacy approach (deprecated but still used)
ifconfig                        # Show all interfaces
ifconfig eth0                   # Show specific interface
```

### Interface Naming Conventions

Modern Linux systems use predictable network interface names:

| Format | Example | Description |
|--------|---------|-------------|
| `eno1` | Ethernet Port 1 | On-board Ethernet |
| `ens33` | Ethernet Slot 3 Sub3 | PCI slot location |
| `enp0s3` | Ethernet PCI 0 Slot 3 | PCI device |
| `eth0`, `eth1` | Deprecated | Old random naming |
| `wlan0` | Wireless LAN 0 | WiFi interface |
| `lo` | Loopback | Localhost (127.0.0.1) |
| `docker0` | Docker bridge | Container networking |
| `veth...` | Virtual Ethernet | Container veth pair |

### Configuring Network Interfaces

**Using `ip` command (temporary - lost on reboot):**

```bash
# Add IP address
sudo ip addr add 192.168.1.100/24 dev eth0

# Remove IP address
sudo ip addr del 192.168.1.100/24 dev eth0

# Bring interface up
sudo ip link set eth0 up

# Bring interface down
sudo ip link set eth0 down

# Set MTU (Maximum Transmission Unit)
sudo ip link set eth0 mtu 1500
```

**Debian/Ubuntu - `/etc/network/interfaces` (persistent):**

```bash
# Static IP configuration
auto eth0
iface eth0 inet static
    address 192.168.1.100
    netmask 255.255.255.0
    gateway 192.168.1.1
    dns-nameservers 8.8.8.8 8.8.4.4

# DHCP configuration
auto eth0
iface eth0 inet dhcp

# Apply changes
sudo systemctl restart networking
```

**RedHat/CentOS - `/etc/sysconfig/network-scripts/ifcfg-eth0` (persistent):**

```bash
TYPE=Ethernet
BOOTPROTO=static
NAME=eth0
DEVICE=eth0
ONBOOT=yes
IPADDR=192.168.1.100
NETMASK=255.255.255.0
GATEWAY=192.168.1.1
DNS1=8.8.8.8

# Apply changes
sudo systemctl restart network
```

### NetworkManager & nmcli

Modern desktop and some server systems use NetworkManager:

```bash
# List connections
nmcli connection show

# Show active connections
nmcli connection show --active

# Create static connection
nmcli con add type ethernet con-name "eth0" ifname eth0 \
    ip4 192.168.1.100/24 gw4 192.168.1.1

# Create DHCP connection
nmcli con add type ethernet con-name "eth0" ifname eth0

# Bring connection up/down
nmcli con up eth0
nmcli con down eth0

# Show device status
nmcli device show
```

---

## Routing

### Viewing Routing Table

```bash
# Modern approach
ip route show                   # Show all routes
ip route show table main        # Show main routing table
ip -6 route show                # Show IPv6 routes

# Legacy approach
route -n                        # Numeric (no DNS)
netstat -r                      # Routing table via netstat
```

### Understanding Route Output

```
Destination   Gateway         Genmask         Flags Metric Ref Use Iface
default       192.168.1.1     0.0.0.0         UG    0      0   0   eth0
192.168.1.0   0.0.0.0         255.255.255.0   U     0      0   0   eth0
```

- `default` - Default route (destination 0.0.0.0/0)
- `192.168.1.0` - Directly connected network
- `U` - Route is up
- `G` - Route uses gateway
- `Metric` - Route cost/preference

### Managing Routes

```bash
# Add static route to network
sudo ip route add 10.0.0.0/8 via 192.168.1.254

# Add route to specific host
sudo ip route add 10.1.1.100/32 via 192.168.1.254

# Add default route
sudo ip route add default via 192.168.1.1

# Delete route
sudo ip route del 10.0.0.0/8

# Replace existing route
sudo ip route replace 10.0.0.0/8 via 192.168.1.253

# View gateway
ip route | grep default

# Set default gateway
sudo ip route replace default via 192.168.1.1 dev eth0
```

### Persistent Routes (Debian/Ubuntu)

Edit `/etc/netplan/01-netcfg.yaml`:

```yaml
network:
  version: 2
  ethernets:
    eth0:
      dhcp4: false
      addresses:
        - 192.168.1.100/24
      gateway4: 192.168.1.1
      nameservers:
        addresses: [8.8.8.8, 8.8.4.4]
      routes:
        - to: 10.0.0.0/8
          via: 192.168.1.254
        - to: 10.1.1.0/24
          via: 192.168.1.253

# Apply configuration
sudo netplan apply
```

---

## DNS (Domain Name System)

### DNS Fundamentals

DNS translates domain names to IP addresses using a hierarchical system:

```
www.example.com
│   │       │
│   │       └─ TLD (Top Level Domain): .com
│   └─────── Domain: example
└─────────── Subdomain: www
```

### DNS Record Types

| Type | Purpose | Example |
|------|---------|---------|
| `A` | IPv4 address | `example.com` → `93.184.216.34` |
| `AAAA` | IPv6 address | `example.com` → `2606:2800:220:...` |
| `CNAME` | Canonical name (alias) | `www.example.com` → `example.com` |
| `MX` | Mail exchange | `example.com` → `mail.example.com` |
| `NS` | Nameserver | Points to authoritative nameserver |
| `TXT` | Text record | SPF, DKIM, domain verification |
| `PTR` | Reverse DNS | IP → hostname |
| `SOA` | Start of authority | Primary nameserver info |

### Configuring DNS Resolution

**`/etc/resolv.conf` (automatically managed):**

```bash
nameserver 8.8.8.8              # Primary DNS
nameserver 8.8.4.4              # Secondary DNS
search example.com              # Default domain suffix
domain example.com              # Local domain
```

**Manual DNS configuration (Debian/Ubuntu):**

Edit `/etc/netplan/01-netcfg.yaml`:

```yaml
network:
  version: 2
  ethernets:
    eth0:
      addresses:
        - 192.168.1.100/24
      gateway4: 192.168.1.1
      nameservers:
        addresses: [8.8.8.8, 8.8.4.4]
        search: [example.com, internal.corp]

# Apply
sudo netplan apply
```

### Local Hostname Resolution

**`/etc/hosts`:**

```bash
127.0.0.1       localhost
192.168.1.10    server1.local server1
192.168.1.11    server2.local server2
::1             localhost       # IPv6 localhost
```

### DNS Query Tools

```bash
# dig - Advanced DNS query
dig example.com                 # Query A record
dig example.com A               # Explicit A record
dig example.com MX              # Mail records
dig example.com NS              # Nameserver records
dig +short example.com          # Short answer only
dig @8.8.8.8 example.com        # Query specific nameserver
dig +trace example.com          # Trace DNS hierarchy
dig -x 93.184.216.34            # Reverse DNS lookup

# nslookup - Simple DNS lookup (simpler than dig)
nslookup example.com
nslookup example.com 8.8.8.8    # Query specific server
nslookup -type=MX example.com   # Query specific type

# host - Simple command-line tool
host example.com
host -a example.com             # All records
host -t MX example.com          # Mail records
host -v example.com             # Verbose output

# getent - Query system databases
getent hosts example.com        # Check if in /etc/hosts
getent passwd username          # Get user info
```

---

## Network Diagnostics & Troubleshooting

### Basic Connectivity Tests

```bash
# Test if host is reachable
ping -c 4 example.com           # Send 4 pings
ping -i 0.5 example.com         # 0.5 second interval

# Trace route to host
traceroute example.com          # Show hops to destination
tracepath example.com           # Same but doesn't need root

# Combined ping + traceroute
mtr example.com                 # Real-time monitoring
mtr -c 100 example.com          # Send 100 probes
```

### Port & Connection Diagnostics

```bash
# ss - Socket statistics (modern, preferred)
ss -tlnp                        # Listening TCP ports with process
ss -ulnp                        # Listening UDP ports with process
ss -an                          # All connections
ss -an | grep ESTABLISHED       # Show established connections
ss -an | grep LISTEN            # Show listening sockets
ss -tn | grep :8080             # Connections on port 8080

# netstat - Network statistics (legacy)
netstat -tlnp                   # Listening ports
netstat -an                     # All connections
netstat -s                      # Protocol statistics
netstat -r                      # Routing table

# lsof - List open files (includes network)
lsof -i                         # All network connections
lsof -i :8080                   # Connections on port 8080
lsof -i TCP                     # TCP connections only
lsof -p PID                     # Open files by process ID

# nc - Netcat (network utility)
nc -zv example.com 80           # Check if port is open
nc -l -p 5000                   # Listen on port 5000
echo "hello" | nc example.com 5000  # Send data
```

### Packet Capture & Analysis

```bash
# tcpdump - Packet sniffer (requires root)
sudo tcpdump                    # Capture all traffic
sudo tcpdump -i eth0            # Capture on specific interface
sudo tcpdump -n                 # Numeric output (no DNS)
sudo tcpdump -c 100             # Capture 100 packets
sudo tcpdump -w file.pcap       # Write to file
sudo tcpdump -r file.pcap       # Read from file
sudo tcpdump host 192.168.1.100 # Filter by host
sudo tcpdump port 80            # Filter by port
sudo tcpdump tcp port 443       # Filter by protocol/port
sudo tcpdump 'tcp[tcpflags] & (tcp-syn|tcp-rst) != 0' # SYN/RST packets
```

### HTTP/HTTPS Requests

```bash
# curl - Command-line HTTP client
curl example.com                # GET request
curl -I example.com             # Headers only
curl -X POST example.com        # POST request
curl -d "data=value" example.com  # POST with data
curl -H "Header: value" example.com  # Custom headers
curl -u user:pass example.com   # HTTP authentication
curl -L example.com             # Follow redirects
curl -i example.com             # Include response headers
curl -o file.html example.com   # Save to file
curl -v example.com             # Verbose output

# wget - Download tool
wget example.com                # Download file
wget -O filename example.com    # Save with specific name
wget -r example.com             # Recursive download
wget -P /path example.com       # Save to path
```

### Network Scanning

```bash
# nmap - Port scanner
nmap example.com                # Basic scan
nmap -p 80,443 example.com      # Scan specific ports
nmap -p- example.com            # Scan all ports
nmap -sn 192.168.1.0/24         # Ping scan (find hosts)
nmap -A example.com             # Aggressive (OS, service detection)
nmap -O example.com             # OS detection
nmap -sV example.com            # Service version detection
nmap -T4 example.com            # Faster scanning
```

---

## Firewall Management

### iptables (netfilter)

iptables operates on chains within tables:

**Chains:**
- `INPUT` - Incoming traffic
- `OUTPUT` - Outgoing traffic
- `FORWARD` - Routed traffic

**Basic Commands:**

```bash
# View current rules
sudo iptables -L                # List all rules
sudo iptables -L -n             # Numeric (no DNS)
sudo iptables -L -n -v          # Verbose
sudo iptables -L INPUT          # List INPUT chain only

# Allow traffic
sudo iptables -A INPUT -p tcp --dport 22 -j ACCEPT    # SSH
sudo iptables -A INPUT -p tcp --dport 80 -j ACCEPT    # HTTP
sudo iptables -A INPUT -p tcp --dport 443 -j ACCEPT   # HTTPS

# Block traffic
sudo iptables -A INPUT -s 192.168.1.100 -j DROP       # Drop from IP
sudo iptables -A INPUT -p tcp --dport 23 -j DROP      # Block telnet

# Default policies
sudo iptables -P INPUT DROP                # Default drop incoming
sudo iptables -P FORWARD DROP              # Default drop forwarded
sudo iptables -P OUTPUT ACCEPT             # Default accept outgoing

# Flush rules
sudo iptables -F INPUT                     # Flush INPUT chain
sudo iptables -F                           # Flush all rules

# Save rules (persist across reboot)
sudo iptables-save > /etc/iptables/rules.v4
sudo netfilter-persistent save             # Ubuntu/Debian
```

### UFW (Uncomplicated Firewall) - Ubuntu/Debian

```bash
# Enable firewall
sudo ufw enable                 # Enable on startup
sudo ufw disable                # Disable firewall

# Check status
sudo ufw status                 # Show enabled/disabled
sudo ufw status verbose         # Detailed rules

# Allow traffic
sudo ufw allow 22               # Allow port 22 (SSH)
sudo ufw allow 80/tcp           # Allow TCP port 80
sudo ufw allow from 192.168.1.100  # Allow from IP
sudo ufw allow from 192.168.1.0/24 # Allow from subnet

# Deny traffic
sudo ufw deny 23                # Deny port 23 (Telnet)
sudo ufw deny from 192.168.1.100   # Deny from IP

# Delete rules
sudo ufw delete allow 22        # Delete allow rule for port 22

# Default policies
sudo ufw default deny incoming  # Drop by default
sudo ufw default allow outgoing # Allow outgoing by default
```

### firewalld - RedHat/CentOS/Fedora

```bash
# Enable firewall
sudo systemctl start firewalld
sudo systemctl enable firewalld

# Check status
sudo firewall-cmd --state       # Is it running?
sudo firewall-cmd --list-all    # List all rules

# Allow traffic
sudo firewall-cmd --permanent --add-service=ssh
sudo firewall-cmd --permanent --add-port=8080/tcp
sudo firewall-cmd --permanent --add-rich-rule='rule family="ipv4" source address="192.168.1.0/24" accept'

# Reload to apply permanent rules
sudo firewall-cmd --reload

# Remove rules
sudo firewall-cmd --permanent --remove-service=ssh
```

### nftables (modern replacement for iptables)

```bash
# View current rules
sudo nft list table inet filter

# Allow SSH
sudo nft add rule inet filter input tcp dport 22 accept

# Drop incoming by default
sudo nft add chain inet filter input { policy drop \; }

# Save rules
sudo nft list ruleset > /etc/nftables.conf
```

---

## SSH Configuration & Tunneling

### SSH Basics

```bash
# Connect to remote host
ssh user@hostname               # Connect
ssh -p 2222 user@hostname       # Non-standard port
ssh -i /path/to/key user@hostname  # Specific private key
ssh -l user hostname            # Specify user differently
ssh -v user@hostname            # Verbose (debug)

# Execute command on remote
ssh user@hostname 'command'
ssh user@hostname 'ls -la /tmp'

# Copy files
scp file user@hostname:/path/   # Local to remote
scp user@hostname:/path/file .  # Remote to local
scp -r dir user@hostname:/path/ # Copy directory
```

### Key Management

```bash
# Generate SSH key pair
ssh-keygen -t rsa -b 4096       # RSA key
ssh-keygen -t ed25519           # Modern EdDSA key
ssh-keygen -C "comment"         # Add comment
ssh-keygen -f keyname           # Specific filename
ssh-keygen -p -f keyfile        # Change passphrase

# Default key locations
~/.ssh/id_rsa                   # Private key
~/.ssh/id_rsa.pub               # Public key
~/.ssh/authorized_keys          # Trusted keys (server)

# Add key to agent (avoid entering passphrase repeatedly)
eval "$(ssh-agent -s)"          # Start agent
ssh-add ~/.ssh/id_rsa           # Add key
ssh-add -l                      # List keys in agent
```

### SSH Configuration File

Edit `~/.ssh/config`:

```bash
Host example
    HostName example.com
    User username
    Port 2222
    IdentityFile ~/.ssh/id_ed25519

Host internal-server
    HostName 10.0.0.50
    User admin
    ProxyJump bastion-host
    StrictHostKeyChecking no

Host bastion-host
    HostName bastion.example.com
    User ubuntu
    IdentityFile ~/.ssh/bastion_key

# Usage:
ssh example              # Uses config above
ssh internal-server      # Routes through bastion
```

### SSH Tunneling

```bash
# Local port forwarding
# Access remote service on local port
ssh -L 8000:localhost:3000 user@host
# localhost:8000 → host:3000

# Remote port forwarding
# Expose local service to remote
ssh -R 8000:localhost:3000 user@host
# host:8000 → localhost:3000

# Dynamic proxy (SOCKS)
ssh -D 1080 user@host
# Use as SOCKS5 proxy at localhost:1080

# ProxyJump (go through bastion)
ssh -J bastion-host user@internal-host
# SSH through bastion to internal-host
```

---

## Network Services Configuration

### DHCP (Dynamic Host Configuration Protocol)

**ISC DHCP Server (/etc/dhcp/dhcpd.conf):**

```bash
subnet 192.168.1.0 netmask 255.255.255.0 {
    range 192.168.1.100 192.168.1.200;
    option routers 192.168.1.1;
    option domain-name-servers 8.8.8.8, 8.8.4.4;
    option domain-name "example.com";
    default-lease-time 600;
    max-lease-time 7200;
}

# Restart service
sudo systemctl restart isc-dhcp-server
```

### DNS Server (BIND)

**Basic configuration (/etc/bind/named.conf):**

```bash
zone "example.com" {
    type master;
    file "/etc/bind/db.example.com";
};

zone "1.168.192.in-addr.arpa" {  # Reverse zone
    type master;
    file "/etc/bind/db.192.168.1";
};

# Start service
sudo systemctl start bind9
sudo systemctl enable bind9
```

### NFS (Network File System)

**Server configuration (/etc/exports):**

```bash
/home/shared    192.168.1.0/24(rw,sync,no_subtree_check)
/var/backups    *(ro)

# Export shares
sudo exportfs -a
sudo systemctl restart nfs-server
```

**Client mount:**

```bash
sudo mount -t nfs server.ip:/home/shared /mnt/nfs
# Persistent mount in /etc/fstab:
# server.ip:/home/shared /mnt/nfs nfs defaults 0 0
```

### Samba (SMB/CIFS)

**Share configuration (/etc/samba/smb.conf):**

```bash
[shared]
    path = /home/shared
    browseable = yes
    writable = yes
    read only = no
    valid users = @shared_group

# Restart service
sudo systemctl restart smbd
```

---

## Network Troubleshooting Flowchart

```
Is the interface up?
├─ NO  → sudo ip link set eth0 up
└─ YES → Does it have an IP?
        ├─ NO  → sudo ip addr add 192.168.1.100/24 dev eth0
        └─ YES → Can you ping the gateway?
                ├─ NO  → Check physical connection
                │       Check gateway IP
                │       Check ARP table: arp -a
                │       Send gratuitous ARP: arping -c 5 192.168.1.1
                └─ YES → Can you ping external host?
                        ├─ NO  → Check default route: ip route
                        │       Check upstream connectivity
                        └─ YES → Can you resolve DNS?
                                ├─ NO  → Check /etc/resolv.conf
                                │       Check DNS server: dig @8.8.8.8
                                │       Test with IP address
                                └─ YES → Service is up!
```

### Common Troubleshooting Commands

```bash
# Step 1: Check interface status
ip link show eth0
ip addr show eth0

# Step 2: Check IP configuration
ip addr show
hostname -I

# Step 3: Check gateway
ip route | grep default
ping 192.168.1.1                # Ping gateway

# Step 4: Check DNS
cat /etc/resolv.conf
dig example.com
nslookup example.com

# Step 5: Check firewall
sudo iptables -L -n
sudo ufw status
sudo firewall-cmd --list-all

# Step 6: Check services
sudo systemctl status networking
sudo journalctl -u networking -n 20

# Step 7: Packet capture
sudo tcpdump -i eth0 -n 'host 8.8.8.8'

# Step 8: Check for listening service
ss -tlnp | grep :80
lsof -i :8080
```

---

## Practical Exercises

### Exercise 1: Configure Static IP Address

**Objective:** Configure a network interface with static IP.

```bash
# 1. Check current configuration
ip addr show

# 2. Edit network configuration (Debian/Ubuntu)
sudo nano /etc/netplan/01-netcfg.yaml

# 3. Configuration:
network:
  version: 2
  ethernets:
    eth0:
      dhcp4: false
      addresses:
        - 192.168.1.100/24
      gateway4: 192.168.1.1
      nameservers:
        addresses: [8.8.8.8, 8.8.4.4]

# 4. Apply changes
sudo netplan apply

# 5. Verify
ip addr show
ip route show
ping 192.168.1.1
```

### Exercise 2: Set Up SSH Key-Based Authentication

**Objective:** Replace password authentication with keys.

```bash
# 1. Generate key pair on client
ssh-keygen -t ed25519 -C "user@client"

# 2. Copy public key to server
ssh-copy-id -i ~/.ssh/id_ed25519.pub user@server.ip

# Alternative (manual):
cat ~/.ssh/id_ed25519.pub | ssh user@server.ip 'cat >> ~/.ssh/authorized_keys'

# 3. Verify key login
ssh user@server.ip              # Should not prompt for password

# 4. Disable password authentication (server)
sudo nano /etc/ssh/sshd_config
# Set: PasswordAuthentication no
sudo systemctl restart sshd

# 5. Verify security
ssh user@server.ip 'echo "Login successful"'
```

### Exercise 3: Create Firewall Rules

**Objective:** Set up firewall rules using UFW.

```bash
# 1. Enable UFW
sudo ufw enable

# 2. Set default policies
sudo ufw default deny incoming
sudo ufw default allow outgoing

# 3. Allow specific ports
sudo ufw allow 22/tcp           # SSH
sudo ufw allow 80/tcp           # HTTP
sudo ufw allow 443/tcp          # HTTPS
sudo ufw allow 3306/tcp         # MySQL (local only)

# 4. Restrict MySQL to local network
sudo ufw allow from 192.168.1.0/24 to any port 3306

# 5. Deny specific IP
sudo ufw deny from 192.168.1.100

# 6. Check rules
sudo ufw status verbose

# 7. Delete rule if needed
sudo ufw delete allow 22/tcp
```

### Exercise 4: Troubleshoot Connectivity Issue

**Objective:** Diagnose why host cannot reach external network.

```bash
# 1. Check interface is up
ip link show eth0               # Should see "UP"

# 2. Check IP assignment
ip addr show eth0               # Should have IP address

# 3. Test gateway connectivity
ping -c 4 192.168.1.1

# 4. Test external connectivity
ping -c 4 8.8.8.8

# 5. Check default route
ip route | grep default
# Should see: default via 192.168.1.1 dev eth0

# 6. Check DNS resolution
dig google.com @8.8.8.8
cat /etc/resolv.conf

# 7. Verify firewall isn't blocking
sudo ufw status
sudo iptables -L INPUT -n

# 8. Check service logs
sudo journalctl -u networking -n 50
```

### Exercise 5: Capture and Analyze Network Traffic

**Objective:** Use tcpdump to analyze HTTP traffic.

```bash
# 1. Start packet capture
sudo tcpdump -i eth0 -n -w traffic.pcap 'tcp port 80'

# 2. In another terminal, trigger traffic
curl http://example.com

# 3. Stop capture (Ctrl+C)

# 4. Analyze capture
sudo tcpdump -r traffic.pcap
sudo tcpdump -r traffic.pcap -A     # ASCII output
tcpdump -r traffic.pcap 'tcp.flags.syn==1'  # SYN packets

# 5. Analyze with Wireshark
wireshark traffic.pcap
```

### Exercise 6: Configure Local DNS with /etc/hosts

**Objective:** Add local DNS entries.

```bash
# 1. Edit hosts file
sudo nano /etc/hosts

# 2. Add entries:
192.168.1.10    webserver.local      webserver
192.168.1.11    database.local       database
127.0.0.1       myapp.dev

# 3. Test resolution
ping webserver.local
nslookup myapp.dev
getent hosts webserver.local

# 4. Verify with curl
curl http://webserver.local
```

### Exercise 7: Set Up NFS Share

**Objective:** Share directory over NFS.

**On Server:**

```bash
# 1. Install NFS server
sudo apt install nfs-kernel-server

# 2. Create shared directory
sudo mkdir -p /home/nfs-share
sudo chmod 755 /home/nfs-share

# 3. Configure export
sudo nano /etc/exports
# Add: /home/nfs-share  192.168.1.0/24(rw,sync,no_subtree_check)

# 4. Export shares
sudo exportfs -a

# 5. Restart NFS server
sudo systemctl restart nfs-server
```

**On Client:**

```bash
# 1. Install NFS client
sudo apt install nfs-common

# 2. Create mount point
sudo mkdir -p /mnt/nfs-share

# 3. Mount share
sudo mount -t nfs 192.168.1.10:/home/nfs-share /mnt/nfs-share

# 4. Verify mount
mount | grep nfs
ls /mnt/nfs-share

# 5. Make persistent in /etc/fstab:
# 192.168.1.10:/home/nfs-share /mnt/nfs-share nfs defaults 0 0
sudo mount -a
```

### Exercise 8: Test Connectivity with Netcat

**Objective:** Use netcat to test port connectivity.

```bash
# 1. Start netcat listener on server
nc -l -p 5000

# 2. Connect from client
nc 192.168.1.10 5000

# 3. Send data
# Type text in client window, appears on server

# 4. Check if port is open without listening
nc -zv 192.168.1.10 5000        # Should show "open"
nc -zv 192.168.1.10 5001        # Should show "closed"

# 5. Scan port range
nc -zv 192.168.1.10 5000-5010

# 6. Send HTTP request
echo -e "GET / HTTP/1.1\r\nHost: example.com\r\n\r\n" | nc example.com 80
```

---

## Network Security Best Practices

1. **Always use SSH instead of Telnet** - Telnet sends credentials in plaintext
2. **Keep systems updated** - Security patches are critical
3. **Use strong passwords** - Or better yet, use key-based authentication
4. **Enable firewall** - Default-deny, explicitly allow needed traffic
5. **Monitor logs** - Check `/var/log/auth.log` and `/var/log/syslog` regularly
6. **Use VPN** - For remote access security
7. **Disable unnecessary services** - Reduce attack surface
8. **Implement rate limiting** - Prevent brute-force attacks
9. **Use HTTPS** - Encrypt web traffic
10. **Backup important data** - Network security is no guarantee
