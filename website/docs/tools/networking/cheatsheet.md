---
title: "Networking Cheatsheet"
description: "100+ essential networking commands and protocol quick reference"
sidebar_label: "Cheatsheet"
sidebar_position: 4
---

# Networking Cheatsheet

Quick reference for essential networking commands and protocols.

## Network Status and Information

### netstat — Network Statistics

```bash
# Show all listening ports and connections
netstat -a

# Show listening ports only
netstat -l

# Show TCP connections
netstat -t

# Show UDP connections
netstat -u

# Show processes using connections (requires root)
netstat -p

# Show numeric IP addresses (no DNS lookup)
netstat -n

# Show network interface statistics
netstat -i

# Show routing table
netstat -r

# Show TCP/UDP statistics
netstat -s

# Continuous monitoring, update every 2 seconds
netstat 2

# Show listening ports with process info
netstat -tlnp | grep LISTEN
```

### ss — Socket Statistics (Modern Replacement)

```bash
# List all listening TCP ports
ss -tlnp

# List all listening UDP ports
ss -ulnp

# Show all connections (TCP/UDP)
ss -a

# List established connections
ss -tp

# Show socket summary statistics
ss -s

# Show memory statistics
ss -tempoZ

# Filter by state
ss state established

# Show IPv6 sockets
ss -6
```

### ip — IP Configuration (Modern Linux)

```bash
# Show all interfaces and addresses
ip addr show

# Show specific interface
ip addr show eth0

# Add IP address to interface
sudo ip addr add 192.168.1.100/24 dev eth0

# Remove IP address from interface
sudo ip addr del 192.168.1.100/24 dev eth0

# Show routing table
ip route show

# Add default route
sudo ip route add default via 192.168.1.1

# Add static route
sudo ip route add 10.0.0.0/8 via 192.168.1.254

# Show ARP table
ip neigh show

# Add static ARP entry
sudo ip neigh add 192.168.1.50 lladdr aa:bb:cc:dd:ee:ff dev eth0

# Show interface statistics
ip -s link show
```

### ifconfig — Interface Configuration (Legacy)

```bash
# Show all interfaces
ifconfig -a

# Show specific interface
ifconfig eth0

# Bring interface up
sudo ifconfig eth0 up

# Bring interface down
sudo ifconfig eth0 down

# Set IP address
sudo ifconfig eth0 192.168.1.100 netmask 255.255.255.0

# Set MTU (Maximum Transmission Unit)
sudo ifconfig eth0 mtu 1500

# Bring up interface with IP
sudo ifconfig eth0 192.168.1.100 netmask 255.255.255.0 up
```

## Connectivity Testing

### ping — Test Host Reachability

```bash
# Basic ping
ping example.com

# Ping with specific count
ping -c 4 example.com

# Set timeout
ping -W 5 example.com

# Ping with specific interval (milliseconds)
ping -i 2 example.com

# Send specific packet size
ping -s 256 example.com

# Record route (IP options)
ping -R example.com

# Show timestamp
ping -T example.com
```

### traceroute — Trace Path to Host

```bash
# Basic traceroute
traceroute example.com

# Use ICMP instead of UDP
traceroute -I example.com

# Set max hops
traceroute -m 30 example.com

# Use UDP to specific port
traceroute -p 53 example.com

# Numeric output (no DNS)
traceroute -n example.com

# Print both numeric and domain names
traceroute -n example.com
```

### mtr — Network Diagnostics (Traceroute + Ping)

```bash
# Basic mtr (interactive)
mtr example.com

# Non-interactive, output report
mtr -r -c 100 example.com

# Show packet loss and latency
mtr -l example.com

# Use TCP instead of ICMP
mtr -t example.com
```

### telnet — Test TCP Port Connectivity

```bash
# Connect to host and port
telnet example.com 80

# Connect to specific port
telnet 192.168.1.1 22

# Quit: Ctrl+]
# Then type 'quit'
```

### nc (netcat) — Network Swiss Army Knife

```bash
# Test port connectivity
nc -zv example.com 80

# Scan port range
nc -zv example.com 20-30

# Simple port listener
nc -l 9999

# Connect to listening port
nc example.com 9999

# Transfer file (listener)
nc -l > received_file.txt

# Transfer file (sender)
nc example.com 9999 < file.txt

# Create persistent connection
nc -k -l 9999
```

## DNS Queries

### dig — DNS Lookup

```bash
# Basic DNS query
dig example.com

# Query specific record type
dig example.com A
dig example.com AAAA
dig example.com MX
dig example.com NS
dig example.com TXT

# Query specific nameserver
dig @8.8.8.8 example.com

# Short answer only
dig +short example.com

# Reverse DNS lookup
dig -x 93.184.216.34

# Trace DNS resolution path
dig +trace example.com

# Show all details
dig +noall +answer example.com

# Query with TCP instead of UDP
dig +tcp example.com
```

### nslookup — DNS Lookup (Legacy)

```bash
# Basic query
nslookup example.com

# Query specific nameserver
nslookup example.com 8.8.8.8

# Interactive mode
nslookup
> example.com
> set type=MX
> example.com
> exit

# Reverse lookup
nslookup 93.184.216.34
```

### host — Simple DNS Lookup

```bash
# Basic query
host example.com

# Query specific nameserver
host example.com 8.8.8.8

# Show all DNS records
host -a example.com

# Specify DNS type
host -t MX example.com
```

## ARP and MAC Address

### arp — Address Resolution Protocol

```bash
# Display ARP table
arp -a

# Add static ARP entry
sudo arp -s 192.168.1.50 aa:bb:cc:dd:ee:ff

# Delete ARP entry
sudo arp -d 192.168.1.50

# Display in numeric form
arp -n

# Interface-specific ARP
arp -i eth0

# Gratuitous ARP (send without waiting)
sudo arp -i eth0 -s 192.168.1.100 ff:ff:ff:ff:ff:ff publish
```

### ip neighbor — ARP (Modern)

```bash
# Show ARP table
ip neigh show

# Interface specific
ip neigh show dev eth0

# Add static entry
sudo ip neigh add 192.168.1.50 lladdr aa:bb:cc:dd:ee:ff dev eth0

# Delete ARP entry
sudo ip neigh del 192.168.1.50 dev eth0

# Replace entry
sudo ip neigh replace 192.168.1.50 lladdr aa:bb:cc:dd:ee:ff dev eth0
```

### macchanger — Spoof MAC Address

```bash
# Show current MAC
macchanger -s eth0

# Random MAC address
sudo macchanger -r eth0

# Specific MAC address
sudo macchanger --mac=aa:bb:cc:dd:ee:ff eth0

# Reset to permanent MAC
sudo macchanger -p eth0
```

## Packet Capture and Analysis

### tcpdump — Packet Sniffer

```bash
# Capture on default interface
sudo tcpdump

# Capture on specific interface
sudo tcpdump -i eth0

# Capture to file
sudo tcpdump -w capture.pcap

# Read from file
sudo tcpdump -r capture.pcap

# Verbose output
sudo tcpdump -v

# Very verbose
sudo tcpdump -vv

# Specify packet count
sudo tcpdump -c 100

# Filter by protocol
sudo tcpdump ip
sudo tcpdump tcp
sudo tcpdump udp
sudo tcpdump icmp

# Filter by host
sudo tcpdump host 192.168.1.1
sudo tcpdump src 192.168.1.1
sudo tcpdump dst 192.168.1.1

# Filter by port
sudo tcpdump port 80
sudo tcpdump src port 22
sudo tcpdump dst port 443

# Complex filters
sudo tcpdump "tcp and port 80 and host 192.168.1.1"
sudo tcpdump "udp or icmp"
sudo tcpdump "not port 22"

# Show packet hex dump
sudo tcpdump -X

# Show packet ASCII dump
sudo tcpdump -A

# Display without resolving names
sudo tcpdump -n

# Capture with timestamp
sudo tcpdump -tttt
```

### tshark — Command-line Wireshark

```bash
# Capture packets
tshark -i eth0

# Write to file
tshark -i eth0 -w capture.pcap

# Read from file
tshark -r capture.pcap

# Display specific columns
tshark -r capture.pcap -T fields -e ip.src -e ip.dst -e tcp.srcport -e tcp.dstport

# Filter packets
tshark -r capture.pcap -Y "http"
tshark -r capture.pcap -Y "dns"

# Statistics
tshark -r capture.pcap -q -z io,phs
```

## Port Scanning

### nmap — Network Mapper

```bash
# Basic host discovery
nmap example.com

# Scan specific port
nmap -p 22 example.com

# Scan port range
nmap -p 20-30 example.com

# Scan all 65535 ports
nmap -p- example.com

# Detect OS and services
nmap -O -sV example.com

# Aggressive scan (OS, version, script, traceroute)
nmap -A example.com

# TCP SYN scan (half-open)
nmap -sS example.com

# TCP connect scan
nmap -sT example.com

# UDP scan
nmap -sU example.com

# Ping sweep
nmap -sn 192.168.1.0/24

# Service version detection
nmap -sV example.com

# NSE scripting engine
nmap --script vuln example.com

# Output formats
nmap -oN output.txt example.com    # Normal
nmap -oX output.xml example.com    # XML
nmap -oG output.txt example.com    # Greppable
nmap -oA output example.com        # All formats

# Timing templates
nmap -T0 example.com   # Paranoid (slowest)
nmap -T5 example.com   # Insane (fastest)
```

## HTTP/Web Tools

### curl — Transfer Data using URLs

```bash
# Basic request
curl example.com

# Follow redirects
curl -L example.com

# Save to file
curl -o filename.html example.com
curl -O example.com/file.zip

# Show headers only
curl -I example.com

# Show response headers and body
curl -i example.com

# Custom HTTP method
curl -X POST example.com
curl -X PUT example.com
curl -X DELETE example.com

# Set custom headers
curl -H "User-Agent: Mozilla/5.0" example.com
curl -H "Authorization: Bearer token123" example.com

# POST data
curl -d "param1=value1&param2=value2" example.com
curl -d '{"key":"value"}' example.com

# Upload file
curl -F "file=@/path/to/file" example.com

# Basic authentication
curl -u username:password example.com

# SSL certificate verification
curl -k https://example.com  # Skip verification
curl --cacert ca.pem https://example.com

# Verbose output
curl -v example.com

# Trace network traffic
curl --trace - example.com

# Proxy
curl -x proxy.example.com:8080 example.com

# Set timeout
curl --max-time 10 example.com
```

### wget — Non-interactive Download

```bash
# Basic download
wget example.com/file.zip

# Save as different name
wget -O newname.zip example.com/file.zip

# Resume download
wget -c example.com/largefile.iso

# Recursive download
wget -r example.com

# Limit depth
wget -r -l 2 example.com

# Mirror website
wget -m example.com

# Background download
wget -b example.com/file.zip

# Show progress bar
wget --progress=bar example.com/file.zip

# User-Agent
wget --user-agent="Mozilla/5.0" example.com

# Timeout
wget --timeout=10 example.com

# Quiet mode
wget -q example.com/file.zip

# Verbose mode
wget -v example.com
```

## Firewall and Filtering

### iptables — Linux Firewall

```bash
# List rules (filter table)
sudo iptables -L

# List with line numbers
sudo iptables -L -n --line-numbers

# List rules for specific chain
sudo iptables -L INPUT

# List with packet/byte counts
sudo iptables -L -v -n

# List NAT rules
sudo iptables -t nat -L

# Add rule (append to chain)
sudo iptables -A INPUT -p tcp --dport 22 -j ACCEPT

# Insert rule at position
sudo iptables -I INPUT 1 -p tcp --dport 22 -j ACCEPT

# Delete rule
sudo iptables -D INPUT -p tcp --dport 22 -j ACCEPT

# Set default policy
sudo iptables -P INPUT DROP
sudo iptables -P FORWARD DROP
sudo iptables -P OUTPUT ACCEPT

# Allow loopback
sudo iptables -A INPUT -i lo -j ACCEPT

# Allow established connections
sudo iptables -A INPUT -m state --state ESTABLISHED,RELATED -j ACCEPT

# Allow SSH from specific IP
sudo iptables -A INPUT -p tcp -s 192.168.1.100 --dport 22 -j ACCEPT

# Block specific IP
sudo iptables -A INPUT -s 192.168.1.50 -j DROP

# Port forwarding
sudo iptables -t nat -A PREROUTING -p tcp --dport 8080 -j REDIRECT --to-port 80

# Save rules
sudo iptables-save > /etc/iptables/rules.v4

# Restore rules
sudo iptables-restore < /etc/iptables/rules.v4
```

### ufw — Uncomplicated Firewall

```bash
# Enable firewall
sudo ufw enable

# Disable firewall
sudo ufw disable

# Show rules
sudo ufw show added

# Show detailed status
sudo ufw status verbose

# Default policy
sudo ufw default deny incoming
sudo ufw default allow outgoing

# Allow port
sudo ufw allow 22/tcp
sudo ufw allow 80
sudo ufw allow 443

# Deny port
sudo ufw deny 23

# Allow from specific IP
sudo ufw allow from 192.168.1.100
sudo ufw allow from 192.168.1.100 to any port 22

# Delete rule
sudo ufw delete allow 22

# Rate limiting
sudo ufw limit 22/tcp
```

## Network Configuration

### hostname and Network Settings

```bash
# Show hostname
hostname

# Set hostname (temporary)
sudo hostname newhostname

# Set hostname (permanent, Linux)
sudo hostnamectl set-hostname newhostname

# Show DNS servers
cat /etc/resolv.conf
systemd-resolve --status

# Add DNS server (temporary)
sudo resolvectl dns eth0 8.8.8.8 8.8.4.4

# Set default route
sudo ip route add default via 192.168.1.1 dev eth0

# Show all routes
ip route show
```

## Protocol Quick Reference

### Common Ports and Protocols

| Port | Protocol | Service | Purpose |
|:-----|:---------|:--------|:--------|
| 21 | TCP | FTP | File transfer |
| 22 | TCP | SSH | Secure shell |
| 25 | TCP | SMTP | Email send |
| 53 | TCP/UDP | DNS | Domain resolution |
| 80 | TCP | HTTP | Web (unencrypted) |
| 110 | TCP | POP3 | Email retrieve |
| 143 | TCP | IMAP | Email retrieve (modern) |
| 389 | TCP/UDP | LDAP | Directory service |
| 443 | TCP | HTTPS | Web (encrypted) |
| 445 | TCP | SMB | Windows file sharing |
| 3306 | TCP | MySQL | Database |
| 5432 | TCP | PostgreSQL | Database |
| 5672 | TCP | AMQP | Message queue |
| 6379 | TCP | Redis | Cache |
| 8080 | TCP | HTTP Alt | Alternate HTTP |
| 8443 | TCP | HTTPS Alt | Alternate HTTPS |

### TCP States

| State | Meaning |
|:------|:--------|
| LISTEN | Waiting for connection |
| ESTABLISHED | Connection active |
| TIME_WAIT | Waiting for timeout after close |
| CLOSE_WAIT | Waiting for application to close |
| FIN_WAIT_1 | Sent FIN, waiting for ACK |
| FIN_WAIT_2 | Sent FIN, received ACK, waiting for FIN |
| CLOSING | Both sides sent FIN |
| SYN_SENT | Sent SYN, waiting for SYN-ACK |
| SYN_RECV | Received SYN, sent SYN-ACK |

### Common ICMP Types

| Type | Name | Purpose |
|:-----|:-----|:--------|
| 0 | Echo Reply | Response to ping |
| 3 | Destination Unreachable | Network/host/port unreachable |
| 8 | Echo Request | Ping request |
| 11 | Time Exceeded | TTL expired (traceroute) |
| 13 | Timestamp | Time measurement |

## Exercises

### Exercise 1: Diagnose Network Connectivity

**Q:** Server A cannot reach Server B. What commands would you run?

**A:**
```bash
# 1. Check local connectivity
ping serverB

# 2. Check if port is open
nc -zv serverB 22

# 3. Trace path to server
traceroute serverB

# 4. Check routing table
ip route show

# 5. Monitor packets
sudo tcpdump -i eth0 host serverB

# 6. Verify firewall rules
sudo iptables -L -n
```

### Exercise 2: Port Scan Analysis

**Q:** Use nmap to find all open ports and identify services on a host.

**A:**
```bash
# Scan for open ports and service detection
nmap -p- -sV example.com

# Output: Shows open ports and running services
# Port 22/tcp open ssh
# Port 80/tcp open http
# Port 443/tcp open https
```

### Exercise 3: Traffic Capture and Analysis

**Q:** Capture HTTP traffic and display the request.

**A:**
```bash
# Capture HTTP traffic
sudo tcpdump -i eth0 port 80 -A

# Or use tshark
tshark -i eth0 -Y http

# Output shows HTTP GET/POST requests and responses
```

## Summary

Master these commands for network troubleshooting:
- **netstat/ss** — Connection and port information
- **ping/traceroute** — Connectivity testing
- **dig** — DNS resolution
- **tcpdump** — Packet capture
- **nmap** — Port scanning
- **curl** — HTTP requests
- **iptables** — Firewall rules
