---
title: "Linux Security"
sidebar_label: "Security"
description: "Linux security hardening — firewall configuration, SELinux/AppArmor, SSH hardening, audit logging, and security best practices"
sidebar_position: 3
---

# Linux Security

Security is a continuous process, not a destination. This guide covers essential security practices including firewall configuration, mandatory access controls, SSH hardening, audit logging, and a comprehensive hardening checklist.

## Security Fundamentals

### Principle of Least Privilege

Grant users and processes only the minimum permissions needed to function:

```bash
# Bad: World-readable sensitive file
-rw-r--r-- 1 root root 1024 config.conf

# Good: Restricted permissions
-rw------- 1 root root 1024 config.conf
```

Apply least privilege to:
- **File permissions** — Only owner/group can read sensitive files
- **User accounts** — Regular users (non-root), specific roles
- **Services** — Run as dedicated user, not root
- **Network access** — Deny by default, allow specific ports

### Defense in Depth

Layer multiple security controls:

1. **Network level** — Firewall, network segmentation
2. **Host level** — Firewall, SELinux/AppArmor
3. **Application level** — Input validation, secure coding
4. **Data level** — Encryption, access controls

Example: Protect a database server:

1. Block all incoming traffic (firewall)
2. Allow only from app server IP (firewall rule)
3. Enable SELinux context on app process
4. Use strong passwords/keys for database
5. Encrypt data at rest and in transit

### Attack Surface Reduction

Minimize what attackers can target:

```bash
# Disable unnecessary services
sudo systemctl disable cups       # Printing
sudo systemctl disable bluetooth  # Wireless
sudo systemctl disable avahi      # mDNS

# Uninstall unnecessary packages
sudo apt remove --purge xserver-xorg  # Remove X11 on servers

# Close unnecessary ports
sudo ss -tlnp | grep LISTEN  # Audit open ports
```

Remove unused software, disable unneeded services, and close unneeded ports.

## Firewall Configuration

### Iptables: Low-Level Firewall

Iptables operates on packets at the kernel level. Basic structure:

```
Table (filter, nat, mangle)
  ↓
Chain (INPUT, OUTPUT, FORWARD)
  ↓
Rules (match criteria, action)
```

List current rules:

```bash
sudo iptables -L              # Simple list
sudo iptables -L -n           # Numeric (IPs not hostnames)
sudo iptables -L -n -v        # Verbose
sudo iptables -L -n -v -x     # Extended packet/byte counts
```

Add rules to allow SSH:

```bash
sudo iptables -A INPUT -p tcp --dport 22 -j ACCEPT
sudo iptables -A INPUT -i lo -j ACCEPT              # Allow localhost
sudo iptables -A INPUT -m state --state ESTABLISHED,RELATED -j ACCEPT
```

Common rules:

```bash
# Allow SSH (before default deny)
sudo iptables -A INPUT -p tcp --dport 22 -j ACCEPT

# Allow HTTP/HTTPS
sudo iptables -A INPUT -p tcp --dport 80 -j ACCEPT
sudo iptables -A INPUT -p tcp --dport 443 -j ACCEPT

# Allow DNS
sudo iptables -A INPUT -p udp --dport 53 -j ACCEPT
sudo iptables -A INPUT -p tcp --dport 53 -j ACCEPT

# Allow from specific IP
sudo iptables -A INPUT -s 192.168.1.100 -j ACCEPT

# Allow from specific network
sudo iptables -A INPUT -s 10.0.0.0/8 -j ACCEPT

# Default deny incoming
sudo iptables -A INPUT -j DROP
```

Delete rules:

```bash
sudo iptables -D INPUT 5      # Delete rule 5 from INPUT chain
sudo iptables -F INPUT        # Flush (delete all) INPUT chain
sudo iptables -F              # Flush all rules
```

Save iptables rules (persist across reboot):

```bash
# Ubuntu/Debian
sudo apt install iptables-persistent
sudo iptables-save > /etc/iptables/rules.v4

# CentOS/RHEL
sudo service iptables save
```

### UFW (Uncomplicated Firewall)

Higher-level wrapper for iptables, easier to use on Ubuntu/Debian:

```bash
sudo ufw enable               # Enable firewall
sudo ufw status              # Show status and rules
sudo ufw status verbose      # Detailed status
```

Allow services:

```bash
sudo ufw allow 22/tcp        # Allow SSH
sudo ufw allow 22            # Default to TCP
sudo ufw allow ssh           # By service name
sudo ufw allow http          # HTTP
sudo ufw allow https         # HTTPS
sudo ufw allow 8080/tcp      # Specific port
sudo ufw allow from 192.168.1.100  # From IP
sudo ufw allow from 192.168.1.0/24 to any port 22  # From network
```

Deny traffic:

```bash
sudo ufw deny 8080           # Block port 8080
sudo ufw reject 23           # Reject (send RST)
sudo ufw delete allow 8080   # Remove rule
```

Rate limiting:

```bash
sudo ufw limit 22/tcp        # Limit SSH (rate-limited)
```

Enable/disable:

```bash
sudo ufw enable
sudo ufw disable
sudo ufw reset               # Reset to default (allow all)
```

### Firewalld (RHEL/CentOS)

Firewalld uses zones and services. Default zones:

- `public` — Default, minimal services
- `trusted` — Trust all connections
- `drop` — Drop all, send no response

View and manage:

```bash
sudo firewall-cmd --list-all           # Show default zone
sudo firewall-cmd --list-all-zones     # Show all zones
sudo firewall-cmd --get-active-zones   # Show active zones
```

Add service (permanent):

```bash
sudo firewall-cmd --permanent --add-service=http
sudo firewall-cmd --permanent --add-service=https
sudo firewall-cmd --reload             # Apply changes
```

Add port:

```bash
sudo firewall-cmd --permanent --add-port=8080/tcp
sudo firewall-cmd --reload
```

Add source (only from network):

```bash
sudo firewall-cmd --permanent --add-source=192.168.1.0/24 --zone=public
sudo firewall-cmd --permanent --add-service=ssh --zone=public
sudo firewall-cmd --reload
```

Rich rules (complex rules):

```bash
# Only allow SSH from specific IP
sudo firewall-cmd --permanent --add-rich-rule='rule family="ipv4" source address="203.0.113.5" service name="ssh" accept'
sudo firewall-cmd --reload
```

### Nftables (Modern Replacement)

Nftables is the modern replacement for iptables. View rules:

```bash
sudo nft list ruleset        # Show all rules
sudo nft list table inet filter  # Show filter table
```

Basic firewall with nftables:

```bash
#!/bin/bash
# Create table
sudo nft add table inet filter

# Create chains
sudo nft add chain inet filter input { type filter hook input priority 0 \; policy drop \; }
sudo nft add chain inet filter output { type filter hook output priority 0 \; policy accept \; }
sudo nft add chain inet filter forward { type filter hook forward priority 0 \; policy drop \; }

# Allow localhost
sudo nft add rule inet filter input iif lo accept

# Allow established connections
sudo nft add rule inet filter input ct state established,related accept

# Allow SSH
sudo nft add rule inet filter input tcp dport 22 accept

# Allow HTTP/HTTPS
sudo nft add rule inet filter input tcp dport {80, 443} accept

# Drop everything else
sudo nft add rule inet filter input drop
```

## SELinux (Security-Enhanced Linux)

SELinux enforces mandatory access controls (MAC) in addition to traditional DAC.

### SELinux Modes

```bash
getenforce                   # Current mode
sudo setenforce 0            # Permissive (log violations)
sudo setenforce 1            # Enforcing (block violations)
```

Modes:
- `disabled` — SELinux disabled
- `permissive` — Log violations, don't block
- `enforcing` — Block violations

View SELinux status:

```bash
sestatus
sestatus -v              # Verbose, show contexts
```

### SELinux Contexts

Every file and process has a context: `user:role:type:level`

View file contexts:

```bash
ls -Z
ls -Z /etc/ssh/sshd_config
```

View process contexts:

```bash
ps -eZ
ps -eZ | grep apache
```

Common context types:
- `sshd_exec_t` — SSH daemon executable
- `sshd_key_t` — SSH keys
- `httpd_exec_t` — Web server executable
- `admin_home_t` — Admin home directory

### Change File Contexts

Restore default context:

```bash
# Restore single file
sudo restorecon /etc/ssh/sshd_config

# Restore directory recursively
sudo restorecon -R /var/www/html
```

Set custom context:

```bash
sudo chcon -t httpd_sys_content_t /var/www/html/index.html
sudo chcon -R -t httpd_sys_rw_content_t /var/www/uploads
```

Use semanage for persistent changes:

```bash
sudo semanage fcontext -a -t httpd_sys_rw_content_t "/var/www/uploads(/.*)?"
sudo restorecon -R /var/www/uploads
```

### SELinux Booleans

Booleans enable/disable policy rules:

```bash
getsebool -a                      # List all booleans
getsebool httpd_can_network_connect  # Check specific boolean

# Enable boolean
sudo setsebool -P httpd_can_network_connect on

# Disable boolean
sudo setsebool -P httpd_can_network_connect off
```

Flag `-P` makes changes permanent.

### Troubleshoot SELinux Violations

Check denied actions in audit log:

```bash
sudo ausearch -m avc | tail -20     # Last 20 denials
```

Get explanation:

```bash
sudo audit2why -a              # Show summary of denials
sudo audit2why -a -l           # More detailed
```

Generate policy from denials:

```bash
sudo audit2allow -a -M custom_policy
sudo semodule -i custom_policy.pp
```

## AppArmor

AppArmor is an alternative to SELinux. It uses path-based access control.

### AppArmor Modes

```bash
aa-status                      # Show AppArmor status
```

View enforced profiles:

```bash
sudo aa-status | grep enforce
```

### Create AppArmor Profile

Create profile at `/etc/apparmor.d/usr.local.bin.myapp`:

```
#include <tunables/global>

/usr/local/bin/myapp {
  #include <abstractions/base>
  #include <abstractions/nameservice>

  /usr/local/bin/myapp r,
  /usr/local/lib/myapp/ r,
  /usr/local/lib/myapp/** r,
  /tmp/ rw,
  /tmp/** rw,
  /var/log/myapp.log rw,
}
```

Load profile in complain mode (log, don't block):

```bash
sudo aa-complain /usr/local/bin/myapp
```

Check logs for violations:

```bash
sudo tail -f /var/log/audit/audit.log | grep myapp
```

Enforce profile:

```bash
sudo aa-enforce /usr/local/bin/myapp
```

## SSH Hardening

### Disable Root Login

Edit `/etc/ssh/sshd_config`:

```
PermitRootLogin no
```

### Password-Only to Key-Only Authentication

Generate key on client:

```bash
ssh-keygen -t ed25519 -f ~/.ssh/id_ed25519
```

Copy to server:

```bash
ssh-copy-id -i ~/.ssh/id_ed25519.pub user@host
```

Edit `/etc/ssh/sshd_config` to disable passwords:

```
PasswordAuthentication no
PubkeyAuthentication yes
```

### SSH Hardening Checklist

Apply to `/etc/ssh/sshd_config`:

```bash
# Change port (optional, but helps)
Port 2222

# Disable root login
PermitRootLogin no

# Disable password authentication
PasswordAuthentication no
PubkeyAuthentication yes

# Disable empty passwords
PermitEmptyPasswords no

# Limit authentication attempts
MaxAuthTries 3
MaxSessions 10

# Timeout idle sessions
ClientAliveInterval 300
ClientAliveCountMax 3

# Disable host-based authentication
HostbasedAuthentication no

# Disable X11 forwarding (if not needed)
X11Forwarding no

# Disable TCP forwarding (if not needed)
AllowTcpForwarding no

# Restrict user/group access (optional)
AllowUsers user1 user2
AllowGroups ssh-users

# Key algorithms (modern)
HostKeyAlgorithms ssh-ed25519,rsa-sha2-512,rsa-sha2-256
PubkeyAcceptedAlgorithms ssh-ed25519,rsa-sha2-512,rsa-sha2-256

# Encryption ciphers
Ciphers chacha20-poly1305@openssh.com,aes256-gcm@openssh.com,aes128-gcm@openssh.com

# Key exchange algorithms
KexAlgorithms curve25519-sha256,diffie-hellman-group16-sha512

# Message authentication codes
MACs hmac-sha2-512-etm@openssh.com,hmac-sha2-256-etm@openssh.com
```

Restart SSH:

```bash
sudo sshd -t              # Test configuration
sudo systemctl restart ssh
```

### Fail2ban for Brute-Force Protection

Install fail2ban:

```bash
sudo apt install fail2ban
```

Create local configuration `/etc/fail2ban/jail.local`:

```
[DEFAULT]
bantime = 3600
findtime = 600
maxretry = 5

[sshd]
enabled = true
port = ssh
filter = sshd
maxretry = 3
bantime = 3600
```

Start and enable:

```bash
sudo systemctl enable fail2ban
sudo systemctl start fail2ban
```

Monitor bans:

```bash
sudo fail2ban-client status
sudo fail2ban-client status sshd
```

Unban IP:

```bash
sudo fail2ban-client set sshd unbanip 203.0.113.5
```

## User and Account Security

### Password Policies

Install PAM password quality tools:

```bash
sudo apt install libpam-pwquality
```

Edit `/etc/security/pwquality.conf`:

```
# Minimum password length
minlen = 14

# Number of digits required
dcredit = -1

# Number of uppercase required
ucredit = -1

# Number of lowercase required
lcredit = -1

# Number of special characters required
ocredit = -1

# Reject similar to old password
difok = 5

# Check dictionary words
reject_username
```

### Account Lockout with PAM

Edit `/etc/pam.d/common-auth` to lock account after failed attempts:

```
auth required pam_tally2.so onerr=fail audit silent deny=5 unlock_time=900
```

Check failed attempts:

```bash
sudo pam_tally2 --user=username
```

Unlock account:

```bash
sudo pam_tally2 --user=username --reset
```

### Sudo Configuration

Edit `/etc/sudoers` with `visudo` (safe editor):

```bash
sudo visudo
```

Grant user sudo access:

```
username ALL=(ALL) ALL        # Full sudo access
username ALL=(ALL) NOPASSWD: /usr/bin/systemctl  # No password for systemctl
```

Grant group sudo access:

```
%wheel ALL=(ALL) NOPASSWD: ALL
%sudo ALL=(ALL) NOPASSWD: ALL
```

Audit sudo usage:

```bash
sudo grep sudo /var/log/auth.log
journalctl SYSLOG_IDENTIFIER=sudo
```

## File System Security

### File Permissions

Understanding permissions:

```
-rw-r--r-- 1 user group 1024 file.txt
drwxr-xr-x 2 user group 4096 directory

First char: - (file) or d (directory)
Next 3: owner permissions (rwx)
Next 3: group permissions (r-x)
Last 3: other/world permissions (r--)
```

Set permissions:

```bash
chmod 755 script.sh          # rwxr-xr-x
chmod 644 file.txt           # rw-r--r--
chmod 700 ~/.ssh             # rwx------
chmod 600 ~/.ssh/id_rsa      # rw-------
```

Change ownership:

```bash
sudo chown user:group file.txt
sudo chown -R user:group directory/
```

### Access Control Lists (ACLs)

Grant specific user access without changing group:

```bash
# Give user read access
setfacl -m u:username:r file.txt

# Give group write access
setfacl -m g:groupname:rw file.txt

# Set default ACL for new files in directory
setfacl -d -m u:username:rx directory/
```

View ACLs:

```bash
getfacl file.txt
getfacl -R directory/
```

Remove ACL:

```bash
setfacl -x u:username file.txt   # Remove specific ACL
setfacl -b file.txt              # Remove all ACLs
```

### Immutable Files

Mark files to prevent modification/deletion:

```bash
sudo chattr +i /etc/critical-config.conf
lsattr /etc/critical-config.conf   # View attributes
```

Remove immutable flag:

```bash
sudo chattr -i /etc/critical-config.conf
```

Other useful flags:
- `+a` — Append-only (log files)
- `+u` — Allow undelete
- `+e` — Extent format (ext4)

### SUID and SGID Audit

SUID (Set User ID) allows execution as file owner. Dangerous if compromised.

Find all SUID binaries:

```bash
find / -perm -4000 2>/dev/null
```

Find SGID binaries:

```bash
find / -perm -2000 2>/dev/null
```

Audit and remove unnecessary:

```bash
# Remove SUID from file
sudo chmod u-s /usr/bin/somebinary

# Remove SGID
sudo chmod g-s /usr/bin/somebinary
```

### World-Writable Files

Find dangerous world-writable files:

```bash
find / -type f -perm -002 2>/dev/null
```

Remove world-write from:

```bash
sudo find /tmp -type f -perm -002 -exec chmod o-w {} \;
```

World-writable directories are sometimes needed for `/tmp`, `/var/tmp`, but should use sticky bit:

```bash
ls -ld /tmp
# drwxrwxrwt  - sticky bit set

# Set sticky bit (user can only delete own files)
chmod o+t /directory
```

## Audit and Monitoring

### Auditd (Linux Audit Framework)

Install auditd:

```bash
sudo apt install auditd audispd-plugins
```

Enable and start:

```bash
sudo systemctl enable auditd
sudo systemctl start auditd
```

View audit rules:

```bash
sudo auditctl -l
sudo auditctl -l -m
```

Add audit rules:

```bash
# Watch configuration file for changes
sudo auditctl -w /etc/passwd -p wa -k passwd_changes

# Watch system calls
sudo auditctl -a always,exit -F arch=b64 -S execve -F uid=1000 -k user_execve

# Watch directory
sudo auditctl -w /etc/shadow -p wa -k shadow_changes
```

Search audit logs:

```bash
sudo ausearch -m exec -ts today    # Executions from today
sudo ausearch -k passwd_changes     # By rule key
sudo ausearch -f /etc/passwd        # For specific file
sudo ausearch -ui 1000 -ts recent   # By user ID
```

Generate audit report:

```bash
sudo aureport
sudo aureport --auth                # Authentication report
sudo aureport --executable         # Executable report
sudo aureport --failed              # Failed operations
```

### File Integrity Monitoring (FIM)

Use AIDE or Tripwire to detect file changes.

Install AIDE:

```bash
sudo apt install aide aide-common
```

Initialize database:

```bash
sudo aideinit             # Creates /var/lib/aide/aide.db
```

Check for changes:

```bash
sudo aide --check         # Compare against database
```

Update database after approved changes:

```bash
sudo aide --update        # Creates aide.db.new
sudo mv /var/lib/aide/aide.db.new /var/lib/aide/aide.db
```

Run daily with cron:

```bash
sudo crontab -e
# 0 2 * * * /usr/sbin/aide --check | mail -s "AIDE Report" root@example.com
```

### System and Network Monitoring

Enable network connection logging:

```bash
# Log new connections
sudo auditctl -a always,exit -F arch=b64 -S socket -S connect -S sendto -S recvfrom -k network
```

Monitor failed login attempts:

```bash
sudo ausearch -m USER_LOGIN -ts recent
sudo ausearch -m USER_AUTH_FAIL -ts recent
```

Monitor file access:

```bash
sudo auditctl -w /sensitive/file -p r -k sensitive_read
```

## Kernel Hardening

### Kernel Parameters with Sysctl

View kernel parameters:

```bash
sysctl -a
sysctl kernel.unprivileged_userns_clone
```

Set parameter temporarily:

```bash
sudo sysctl -w kernel.unprivileged_userns_clone=0
```

Set permanently in `/etc/sysctl.conf` or `/etc/sysctl.d/99-hardening.conf`:

```
# Disable SysRq key
kernel.sysrq = 0

# Restrict kernel module loading
kernel.modules_disabled = 1

# Restrict access to /proc
kernel.kptr_restrict = 2
kernel.perf_event_paranoid = 3

# Disable ICMP redirect
net.ipv4.conf.all.accept_redirects = 0
net.ipv4.conf.default.accept_redirects = 0
net.ipv6.conf.all.accept_redirects = 0

# Disable source packet routing
net.ipv4.conf.all.send_redirects = 0
net.ipv4.conf.default.send_redirects = 0

# Enable reverse path filtering (anti-spoofing)
net.ipv4.conf.all.rp_filter = 1
net.ipv4.conf.default.rp_filter = 1

# Log suspicious packets
net.ipv4.conf.all.log_martians = 1

# Disable ICMP echo
net.ipv4.icmp_echo_ignore_all = 0

# Increase TCP backlog
net.ipv4.tcp_max_syn_backlog = 2048

# Enable TCP syncookies
net.ipv4.tcp_syncookies = 1

# Restrict ptrace scope
kernel.yama.ptrace_scope = 2

# Restrict access to /proc/sys
kernel.dmesg_restrict = 1
```

Apply changes:

```bash
sudo sysctl -p /etc/sysctl.d/99-hardening.conf
```

### Disable Unnecessary Modules

View loaded modules:

```bash
lsmod
```

Disable module (temporary):

```bash
sudo modprobe -r usb_storage
```

Disable module (permanent) by blacklisting in `/etc/modprobe.d/blacklist.conf`:

```
blacklist usb_storage
blacklist dccp
blacklist sctp
blacklist rds
```

Reboot to apply.

## Security Hardening Checklist

20-point checklist for production Linux servers:

1. **System Updates** — Run `apt update && apt upgrade` regularly
2. **SSH Hardening** — Disable root login, key-only auth, change port
3. **Firewall** — Default deny, allow only necessary ports
4. **Fail2Ban** — Protect against brute force
5. **Strong Passwords** — Enforce with PAM
6. **User Accounts** — Remove default accounts, use sudo
7. **File Permissions** — Apply least privilege (644/755 defaults)
8. **SUID/SGID Audit** — Remove unnecessary binaries
9. **Audit Logging** — Enable auditd, monitor auth logs
10. **SELinux/AppArmor** — Enable enforcing mode
11. **Network Hardening** — Sysctl parameters (disable redirects, etc.)
12. **USB Restrictions** — Disable if not needed
13. **Kernel Modules** — Blacklist unnecessary modules
14. **Core Dumps** — Disable if not needed: `sysctl -w fs.suid_dumpable=0`
15. **Process Accounting** — Enable to track processes: `apt install acct`
16. **Log Retention** — Configure logrotate for audit retention
17. **NTP** — Sync time for accurate logs: `apt install chrony`
18. **Limits** — Set resource limits in `/etc/security/limits.conf`
19. **Mount Options** — Use noexec, nosuid on /tmp: `mount -o remount,noexec,nosuid /tmp`
20. **Regular Backups** — Test restore procedures

## Exercises

### Exercise 1: Configure Firewall with UFW

Set up firewall on Ubuntu:

1. Enable UFW: `sudo ufw enable`
2. Set default policy: `sudo ufw default deny incoming`
3. Allow SSH: `sudo ufw allow 22/tcp`
4. Allow HTTP: `sudo ufw allow 80/tcp`
5. Check status: `sudo ufw status verbose`
6. Block specific IP: `sudo ufw deny from 203.0.113.5`
7. Test rules: `curl localhost`

### Exercise 2: Set Up Fail2Ban

Protect against SSH brute force:

1. Install fail2ban: `sudo apt install fail2ban`
2. Create local config: `sudo cp /etc/fail2ban/jail.conf /etc/fail2ban/jail.local`
3. Set maxretry=3, bantime=3600 for sshd
4. Restart: `sudo systemctl restart fail2ban`
5. Simulate failed login: `ssh user@localhost` (wrong password 5 times)
6. Check bans: `sudo fail2ban-client status sshd`
7. Monitor: `sudo tail -f /var/log/fail2ban.log`

### Exercise 3: Harden SSH Configuration

Secure SSH access:

1. Generate ED25519 key: `ssh-keygen -t ed25519`
2. Copy to server: `ssh-copy-id -i ~/.ssh/id_ed25519.pub user@host`
3. Edit `/etc/ssh/sshd_config` — set PermitRootLogin=no, PasswordAuthentication=no
4. Test config: `sudo sshd -t`
5. Restart SSH: `sudo systemctl restart ssh`
6. Test password auth fails: `ssh -o PubkeyAuthentication=no user@host`
7. Test key auth works: `ssh user@host`

### Exercise 4: Create SELinux Policy

Create custom SELinux policy:

1. Check SELinux mode: `getenforce`
2. Set permissive: `sudo setenforce 0`
3. Create test application: `/usr/local/bin/myapp`
4. View denials: `sudo ausearch -m avc | head`
5. Generate policy: `sudo audit2allow -a -M custom`
6. Install policy: `sudo semodule -i custom.pp`
7. Enforce: `sudo setenforce 1`
8. Verify application works

### Exercise 5: Audit File Changes

Monitor file integrity:

1. Install auditd: `sudo apt install auditd`
2. Add watch rule: `sudo auditctl -w /etc/passwd -p wa -k passwd_changes`
3. Make change: `sudo touch /etc/passwd`
4. Search logs: `sudo ausearch -k passwd_changes`
5. Generate report: `sudo aureport -f`
6. Save rules: `sudo service auditd save`

### Exercise 6: Run Security Scan

Scan system for vulnerabilities:

1. Install lynis: `sudo apt install lynis`
2. Run scan: `sudo lynis audit system`
3. Review warnings and suggestions
4. Fix high-priority items
5. Create hardening action plan
6. Rerun scan to verify improvements
7. Schedule regular scans with cron

---

**Next Steps:** Review [System Administration](/docs/tools/linux/administration) for operational tasks. Combine security hardening with proper administration practices.
