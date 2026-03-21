---
title: "Linux System Administration"
sidebar_label: "System Administration"
description: "Linux system administration — disk management, networking, services, cron, logging, performance tuning, and troubleshooting"
sidebar_position: 2
---

# Linux System Administration

System administration is the practice of maintaining and managing Linux servers. This guide covers essential administration tasks including disk management, networking, services, scheduling, logging, and performance monitoring.

## Disk Management

### Viewing Disk Information

Use `lsblk` to list block devices in a tree format:

```bash
lsblk
lsblk -f          # Show file systems
lsblk -S          # Show SCSI devices
```

The `df` command shows disk space usage by filesystem:

```bash
df -h             # Human-readable format
df -i             # Show inode usage
df /path/to/dir   # Usage for specific mount point
```

The `du` command shows disk usage of files and directories:

```bash
du -sh *          # Summary for each item in current directory
du -sh .          # Total size of current directory
du -h --max-depth=2  # Show to 2 levels deep
```

### Partitioning and Formatting

`fdisk` is the traditional partition editor. Basic workflow:

```bash
sudo fdisk /dev/sda           # Interactive mode
# Inside fdisk:
# p - print partition table
# n - new partition
# d - delete partition
# w - write changes
# q - quit without saving
```

For newer disks with GPT partitions, use `gdisk`:

```bash
sudo gdisk /dev/sda
```

Format a partition with a filesystem:

```bash
sudo mkfs.ext4 /dev/sda1      # Create ext4 filesystem
sudo mkfs.xfs /dev/sda1       # Create XFS filesystem
sudo mkswap /dev/sda2         # Create swap partition
```

### Mounting Filesystems

Mount a filesystem temporarily:

```bash
sudo mount /dev/sda1 /mnt/data
sudo mount -t ext4 /dev/sda1 /mnt/data  # Specify type
sudo mount -o remount,rw /mnt/data      # Remount with different options
```

Unmount a filesystem:

```bash
sudo umount /mnt/data
sudo umount -l /mnt/data      # Lazy unmount (when busy)
```

View all mounts:

```bash
mount
df -h
cat /proc/mounts
```

### Persistent Mounts with /etc/fstab

The `/etc/fstab` file defines filesystems to mount at boot. Format:

```
filesystem  mountpoint  type    options     dump    fsck_order
/dev/sda1   /           ext4    defaults    0       1
/dev/sda2   /home       ext4    defaults    0       2
/dev/sda3   swap        swap    defaults    0       0
```

Common options:
- `defaults` — rw, suid, dev, exec, auto, nouser, async
- `ro` — read-only
- `rw` — read-write
- `noauto` — don't mount at boot
- `user` — allow user to mount
- `exec` / `noexec` — allow/disallow execution
- `nofail` — continue boot even if mount fails

Test fstab changes without rebooting:

```bash
sudo mount -a     # Mount all filesystems in fstab
```

### Logical Volume Management (LVM)

LVM allows dynamic resizing of partitions. Basic workflow:

Create a physical volume:

```bash
sudo pvcreate /dev/sda1
```

Create a volume group from physical volumes:

```bash
sudo vgcreate vg0 /dev/sda1
sudo vgcreate vg0 /dev/sda1 /dev/sda2  # Multiple PVs
```

Create a logical volume from a volume group:

```bash
sudo lvcreate -L 10G -n lv_data vg0    # 10GB logical volume
sudo lvcreate -l 50%VG -n lv_data vg0  # 50% of VG
```

View LVM information:

```bash
pvs
vgs
lvs
pvdisplay
vgdisplay
lvdisplay
```

Extend a logical volume:

```bash
sudo lvextend -L +5G /dev/vg0/lv_data
sudo resize2fs /dev/vg0/lv_data  # For ext4
sudo xfs_growfs /dev/vg0/lv_data  # For XFS
```

### Swap Management

Check swap configuration:

```bash
swapon --show
free -h
```

Create a swap file:

```bash
sudo fallocate -l 4G /swapfile
sudo chmod 600 /swapfile
sudo mkswap /swapfile
sudo swapon /swapfile
```

Enable swap at boot by adding to `/etc/fstab`:

```
/swapfile none swap sw 0 0
```

Disable swap:

```bash
sudo swapoff /swapfile
```

## Networking

### IP Configuration

View network interfaces:

```bash
ip addr                    # Show all interfaces
ip addr show eth0          # Show specific interface
ip link show               # Show layer 2 info
```

Configure IP address temporarily:

```bash
sudo ip addr add 192.168.1.100/24 dev eth0
sudo ip addr del 192.168.1.100/24 dev eth0
```

Configure routing:

```bash
ip route                   # Show routing table
ip route show table all
sudo ip route add default via 192.168.1.1
sudo ip route add 10.0.0.0/8 via 192.168.1.254
```

Bring interfaces up/down:

```bash
sudo ip link set eth0 up
sudo ip link set eth0 down
```

### Connection Monitoring

View open ports and connections:

```bash
ss -tlnp                   # TCP listening ports with PID
ss -tlpn                   # Numeric ports
ss -tulpn                  # TCP and UDP
netstat -tlnp              # Legacy netstat command
```

Check specific port:

```bash
ss -tlnp | grep 8080
```

### DNS Resolution

Query DNS servers:

```bash
dig example.com            # Full DNS query
dig example.com +short     # Short output
dig @8.8.8.8 example.com   # Query specific resolver
dig example.com MX         # Query MX records
nslookup example.com       # Legacy DNS lookup
```

View DNS configuration:

```bash
cat /etc/resolv.conf
```

Set DNS servers in `/etc/resolv.conf`:

```
nameserver 8.8.8.8
nameserver 8.8.4.4
```

### Network Diagnostics

Test connectivity:

```bash
ping -c 4 8.8.8.8          # Send 4 ICMP packets
ping -i 2 8.8.8.8          # Interval of 2 seconds
```

Trace route to destination:

```bash
traceroute example.com
traceroute -m 20 example.com  # Maximum 20 hops
```

Download files:

```bash
curl -O https://example.com/file.tar.gz
curl -o myfile.tar.gz https://example.com/file.tar.gz
curl -I https://example.com        # Headers only
wget https://example.com/file.tar.gz
wget -c https://example.com/file.tar.gz  # Resume download
```

### Hostname and Hosts File

View hostname:

```bash
hostname
hostname -f                # Fully qualified domain name
```

Set hostname temporarily:

```bash
sudo hostname newhostname
```

Set permanently:

```bash
sudo hostnamectl set-hostname newhostname
```

Edit `/etc/hosts` for local DNS resolution:

```
127.0.0.1       localhost
192.168.1.10    webserver.local webserver
10.0.0.5        db.local database
```

### Firewall Basics

Check which firewall is active:

```bash
sudo systemctl status firewalld
sudo systemctl status ufw
sudo systemctl status iptables
```

**UFW (Uncomplicated Firewall)** for Ubuntu/Debian:

```bash
sudo ufw enable            # Enable firewall
sudo ufw allow 22/tcp      # Allow SSH
sudo ufw allow 80,443/tcp  # Allow HTTP/HTTPS
sudo ufw deny 8080/tcp
sudo ufw status verbose
```

**Firewalld** for RHEL/CentOS:

```bash
sudo firewall-cmd --list-all      # Show all rules
sudo firewall-cmd --add-service=http --permanent
sudo firewall-cmd --add-port=8080/tcp --permanent
sudo firewall-cmd --reload        # Apply changes
```

**Iptables** (lower level):

```bash
sudo iptables -L                  # List rules
sudo iptables -A INPUT -p tcp --dport 22 -j ACCEPT
sudo iptables -A INPUT -j DROP    # Drop by default
```

### Network Interface Configuration

Modern systems use systemd-networkd or NetworkManager. Edit `/etc/netplan/01-netcfg.yaml` on Ubuntu:

```yaml
network:
  version: 2
  ethernets:
    eth0:
      dhcp4: true
    eth1:
      addresses:
        - 192.168.1.100/24
      gateway4: 192.168.1.1
      nameservers:
        addresses: [8.8.8.8, 8.8.4.4]
```

Apply changes:

```bash
sudo netplan apply
```

For RHEL/CentOS, edit `/etc/sysconfig/network-scripts/ifcfg-eth0`:

```
DEVICE=eth0
BOOTPROTO=static
IPADDR=192.168.1.100
NETMASK=255.255.255.0
GATEWAY=192.168.1.1
ONBOOT=yes
```

Restart networking:

```bash
sudo systemctl restart NetworkManager
```

## Service Management

### Systemd Basics

Check service status:

```bash
sudo systemctl status apache2
systemctl is-active apache2
systemctl is-enabled apache2
```

Start, stop, restart services:

```bash
sudo systemctl start apache2
sudo systemctl stop apache2
sudo systemctl restart apache2
sudo systemctl reload apache2    # Reload configuration
```

Enable service at boot:

```bash
sudo systemctl enable apache2
sudo systemctl disable apache2
```

Useful options:

```bash
systemctl list-units --type=service --all
systemctl list-units --type=service --state=running
systemctl list-dependencies apache2      # Show dependencies
```

### View Logs with Journalctl

View logs for a service:

```bash
journalctl -u apache2                 # All logs for service
journalctl -u apache2 -n 50           # Last 50 lines
journalctl -u apache2 -f              # Follow (tail -f)
journalctl -u apache2 --since today   # Since midnight
journalctl -u apache2 -p err          # Only errors
```

Filter by time:

```bash
journalctl --since "2024-01-01 00:00:00"
journalctl --until "2024-12-31 23:59:59"
```

### Creating Custom Systemd Services

Create a service file at `/etc/systemd/system/myapp.service`:

```ini
[Unit]
Description=My Application
After=network.target

[Service]
Type=simple
User=myuser
WorkingDirectory=/opt/myapp
ExecStart=/opt/myapp/run.sh
Restart=on-failure
RestartSec=10

[Install]
WantedBy=multi-user.target
```

Enable and start:

```bash
sudo systemctl daemon-reload
sudo systemctl enable myapp
sudo systemctl start myapp
```

Key directives:
- `Type=simple` — Service runs in foreground
- `Type=forking` — Service daemonizes
- `Type=notify` — Service sends readiness notification
- `Restart=on-failure` — Restart if exit code indicates failure
- `RestartSec=10` — Wait 10 seconds before restart
- `ExecStartPre` / `ExecStartPost` — Commands to run before/after
- `Environment` — Set environment variables
- `EnvironmentFile` — Load from file

### SysV Init and Runlevels

Legacy SysV init uses runlevels:
- 0 — Halt
- 1 — Single user mode
- 2-5 — Multi-user modes
- 6 — Reboot

View current runlevel:

```bash
runlevel
```

Change runlevel:

```bash
sudo init 3        # Switch to multi-user (no X)
sudo init 5        # Switch to multi-user with X
```

Systemd uses targets instead:
- `poweroff.target` — Halt
- `rescue.target` — Single user mode
- `multi-user.target` — Multi-user (no GUI)
- `graphical.target` — Multi-user with GUI
- `reboot.target` — Reboot

View default target:

```bash
systemctl get-default
```

Change default target:

```bash
sudo systemctl set-default multi-user.target
```

## Scheduled Tasks

### Crontab Syntax

Crontab format: `minute hour day month weekday command`

```
*     *     *     *     *     /path/to/command
|     |     |     |     |
|     |     |     |     +---- Day of week (0-6, 0=Sunday)
|     |     |     +---------- Month (1-12)
|     |     +--------------- Day of month (1-31)
|     +---------------------- Hour (0-23)
+------ -------------------- Minute (0-59)
```

Common patterns:
- `0 0 * * *` — Every day at midnight
- `0 9 * * 1-5` — Weekdays at 9 AM
- `*/15 * * * *` — Every 15 minutes
- `0 */4 * * *` — Every 4 hours
- `0 0 1 * *` — First day of month
- `0 0 * * 0` — Every Sunday

Edit crontab:

```bash
crontab -e         # Edit current user's crontab
sudo crontab -e    # Edit root crontab
crontab -l         # List crontab entries
crontab -r         # Remove crontab
```

System-wide crontab at `/etc/crontab`:

```
SHELL=/bin/bash
PATH=/usr/local/sbin:/usr/local/bin:/sbin:/bin:/usr/sbin:/usr/bin

0 2 * * * root /usr/local/bin/backup.sh
30 * * * * nobody /usr/bin/check-updates.sh
```

### Anacron

Anacron runs tasks that may have been missed if the system was off. Edit `/etc/anacrontab`:

```
period  delay   identifier              command
1       5       daily_backup            /usr/local/bin/backup.sh
7       10      weekly_reports          /usr/local/bin/reports.sh
30      15      monthly_maintenance     /usr/local/bin/maintenance.sh
```

- `period` — Days between runs
- `delay` — Minutes to wait before running
- Anacron runs at boot and checks if tasks need to execute

### One-time Tasks with at

Schedule a one-time job:

```bash
at 10:30 AM tomorrow
# at> /usr/local/bin/backup.sh
# at> Ctrl+D to save
```

View scheduled jobs:

```bash
atq
at -l
```

Remove a job:

```bash
atrm 1        # Remove job ID 1
```

### Systemd Timers

Modern alternative to cron. Create a service file `/etc/systemd/system/backup.service`:

```ini
[Unit]
Description=Backup Service

[Service]
Type=oneshot
ExecStart=/usr/local/bin/backup.sh
```

Create timer file `/etc/systemd/system/backup.timer`:

```ini
[Unit]
Description=Backup Timer
Requires=backup.service

[Timer]
OnBootSec=10min
OnUnitActiveSec=1d
AccuracySec=1s

[Install]
WantedBy=timers.target
```

Enable and start:

```bash
sudo systemctl daemon-reload
sudo systemctl enable backup.timer
sudo systemctl start backup.timer
systemctl list-timers         # View all timers
systemctl status backup.timer
journalctl -u backup.service  # View timer logs
```

Timer patterns:
- `OnBootSec=10min` — 10 minutes after boot
- `OnUnitActiveSec=1d` — 1 day after last activation
- `OnUnitActiveSec=1h` — 1 hour after last activation
- `OnCalendar=daily` — Every day at midnight
- `OnCalendar=*-*-* 02:00:00` — Daily at 2 AM

## Log Management

### Log Locations

Common log files in `/var/log`:
- `auth.log` / `secure` — Authentication attempts
- `syslog` / `messages` — General system logs
- `kern.log` — Kernel messages
- `cron` — Cron job execution
- `apache2/error.log` — Web server errors
- `apache2/access.log` — Web server access
- `dmesg` — Boot messages

View logs:

```bash
tail -f /var/log/syslog                # Follow in real-time
tail -100 /var/log/auth.log            # Last 100 lines
head /var/log/kernel.log               # First lines
cat /var/log/dmesg
dmesg | tail                           # Kernel ring buffer
```

### Journalctl (Systemd Logs)

View all logs:

```bash
journalctl                    # All logs (paged)
journalctl -x                 # Include helpful messages
```

Filter by priority:

```bash
journalctl -p err             # Only errors
journalctl -p warning         # Warnings and above
journalctl -p 0               # Emergency
journalctl -p 3               # Error
journalctl -p 4               # Warning
journalctl -p 5               # Notice
journalctl -p 6               # Info
journalctl -p 7               # Debug
```

### Rsyslog Configuration

Rsyslog is the traditional syslog daemon. Configuration in `/etc/rsyslog.conf`:

```
# Log authentication messages
auth,authpriv.*                 /var/log/auth.log
# Log kernel messages
kern.*                          /var/log/kern.log
# Log everything except auth
*.*;auth,authpriv.none          /var/log/syslog
```

Restart rsyslog after changes:

```bash
sudo systemctl restart rsyslog
```

### Logrotate

Rotate logs to prevent disk space issues. Configuration in `/etc/logrotate.d/`:

```
/var/log/myapp/*.log {
    daily
    rotate 7
    compress
    delaycompress
    missingok
    notifempty
    create 0640 appuser appgroup
    sharedscripts
    postrotate
        systemctl reload myapp
    endscript
}
```

Test logrotate:

```bash
sudo logrotate -f /etc/logrotate.d/myapp
sudo logrotate -d /etc/logrotate.conf  # Dry run
```

## Performance Monitoring

### System Overview

View system uptime and load:

```bash
uptime
cat /proc/uptime
```

Load average explanation: `load average: 0.50, 0.60, 0.70`
- First number — Average load last 1 minute
- Second number — Average load last 5 minutes
- Third number — Average load last 15 minutes

Load indicates number of processes waiting for CPU. Compare to CPU count:

```bash
nproc              # Number of processors
```

View memory usage:

```bash
free -h
free -m
```

Columns:
- `total` — Total RAM
- `used` — Memory in use
- `free` — Unused memory
- `shared` — Shared memory
- `buff/cache` — Buffer and cache (can be reclaimed)
- `available` — Usable memory

### Process Monitoring with top and htop

Real-time process monitor:

```bash
top -b -n 5        # Batch mode, 5 iterations
top -u username    # Show specific user
top -p PID         # Monitor specific process
```

In top, press:
- `q` — Quit
- `M` — Sort by memory
- `P` — Sort by CPU
- `1` — Show all CPUs
- `k` — Kill process

Enhanced version:

```bash
htop              # Colorized, more user-friendly
htop -u username
htop -p PID
```

### CPU and Memory Stats

View CPU information:

```bash
nproc
lscpu
cat /proc/cpuinfo
```

Virtual memory stats:

```bash
vmstat 1 5         # Every 1 second, 5 times
vmstat 5           # Continuous update every 5 seconds
```

Columns in vmstat output:
- `us` — User CPU time
- `sy` — System CPU time
- `id` — Idle
- `wa` — I/O wait
- `st` — Steal time (virtual machines)

### I/O Monitoring

Monitor I/O operations:

```bash
iostat -x 1 5      # Extended I/O stats
iostat -d          # Disk I/O only
iostat -c          # CPU usage only
```

Columns:
- `r/s` — Reads per second
- `w/s` — Writes per second
- `await` — Average wait time
- `util` — Percent of CPU time spent on I/O

Monitor I/O by process:

```bash
sudo iotop         # Top-like tool for I/O
```

### System Activity Reporter (SAR)

View historical performance data:

```bash
sar -u             # CPU usage
sar -r             # Memory usage
sar -d             # Disk I/O
sar 5 10           # Every 5 seconds, 10 times
```

View weekly summary:

```bash
sar -f /var/log/sa/sa$(date +%d)
```

### Dynamic System Monitoring

Comprehensive system stats:

```bash
dstat -tcms        # Time, CPU, memory, system
dstat -g           # Disk I/O
dstat -n           # Network
dstat --top-mem    # Top memory processes
dstat --top-io     # Top I/O processes
```

### Process Call Tracing

Trace system calls of a process:

```bash
strace ls -la      # Trace ls command
strace -p PID      # Trace running process
strace -e open,read /bin/cat /etc/hostname  # Trace specific calls
strace -c command  # Summary statistics
```

## SSH (Secure Shell)

### Basic SSH Usage

Connect to remote server:

```bash
ssh username@hostname
ssh -i /path/to/key username@hostname
ssh username@192.168.1.100
```

Execute remote command:

```bash
ssh user@host "uptime"
ssh user@host "df -h"
```

### SSH Key Generation

Generate SSH key pair:

```bash
ssh-keygen -t rsa -b 4096
# or
ssh-keygen -t ed25519
```

Key types:
- `rsa` — Older, larger keys
- `ed25519` — Modern, smaller, faster
- `ecdsa` — Elliptic curve

Save location: `~/.ssh/id_rsa` (private) and `~/.ssh/id_rsa.pub` (public)

### Copy SSH Key to Server

Automated method:

```bash
ssh-copy-id -i ~/.ssh/id_rsa.pub user@host
```

Manual method:

```bash
cat ~/.ssh/id_rsa.pub | ssh user@host "cat >> ~/.ssh/authorized_keys"
```

Or manually copy to `~/.ssh/authorized_keys` on server and set permissions:

```bash
chmod 700 ~/.ssh
chmod 600 ~/.ssh/authorized_keys
```

### SSH Client Configuration

Edit `~/.ssh/config` for convenient hosts:

```
Host prod-web
    HostName 10.0.0.5
    User deploy
    IdentityFile ~/.ssh/id_rsa
    Port 2222

Host *.internal
    User admin
    IdentityFile ~/.ssh/id_ed25519
```

Connect with:

```bash
ssh prod-web       # Uses settings from config
```

### Copy Files with SCP

Copy file to remote:

```bash
scp /local/file.txt user@host:/remote/path/
```

Copy from remote:

```bash
scp user@host:/remote/file.txt /local/path/
```

Copy directory recursively:

```bash
scp -r /local/dir user@host:/remote/path/
```

### Sync with Rsync

Rsync is more efficient for ongoing syncs:

```bash
rsync -avz /local/dir user@host:/remote/path/
rsync -avz user@host:/remote/dir /local/path/
```

Useful options:
- `-a` — Archive mode (preserve permissions, timestamps)
- `-v` — Verbose
- `-z` — Compress
- `--delete` — Delete remote files not in source
- `--exclude='*.tmp'` — Exclude patterns
- `-e ssh` — Specify SSH (default with remote paths)

### SSH Tunneling

Local port forward (access remote service locally):

```bash
ssh -L 3306:192.168.1.100:3306 jump@bastion
# Access MySQL via localhost:3306
```

Remote port forward (expose local service to remote):

```bash
ssh -R 8080:localhost:80 user@host
# Remote host can access localhost port 8080
```

Create persistent tunnel:

```bash
ssh -N -L 3306:db.internal:3306 -i ~/.ssh/key jump@bastion &
```

### SSH Key-Only Authentication

Disable password authentication on server (`/etc/ssh/sshd_config`):

```
PasswordAuthentication no
PubkeyAuthentication yes
```

Restrict key permissions on server:

```bash
chmod 700 ~/.ssh
chmod 600 ~/.ssh/authorized_keys
```

Restart SSH:

```bash
sudo systemctl restart ssh
```

### SSH Hardening Checklist

Quick hardening steps:

```bash
# Change SSH port
sudo nano /etc/ssh/sshd_config
# Uncomment and change: Port 2222

# Disable root login
# Change: PermitRootLogin no

# Key-only authentication
# Change: PasswordAuthentication no

# Disable empty passwords
# Change: PermitEmptyPasswords no

# Limit connection attempts
# Add: MaxAuthTries 3

# Restart SSH
sudo systemctl restart ssh
```

## Storage and Backup

### Archive Compression

Create archives:

```bash
tar -cvf archive.tar /path/to/files      # Uncompressed
tar -cvzf archive.tar.gz /path/to/files  # Gzip compression
tar -cvjf archive.tar.bz2 /path/to/files # Bzip2 compression
tar -cvJf archive.tar.xz /path/to/files  # XZ compression
```

Extract archives:

```bash
tar -xvf archive.tar
tar -xvzf archive.tar.gz
tar -xvjf archive.tar.bz2
tar -xvJf archive.tar.xz
tar -xvf archive.tar -C /extract/here    # Extract to directory
```

Options:
- `-c` — Create archive
- `-x` — Extract archive
- `-v` — Verbose
- `-f` — Filename
- `-z` — Gzip
- `-j` — Bzip2
- `-J` — XZ
- `-t` — List contents

Compress files individually:

```bash
gzip filename              # Creates filename.gz
gunzip filename.gz
bzip2 filename            # Creates filename.bz2
bunzip2 filename.bz2
xz filename               # Creates filename.xz
unxz filename.xz
zip archive.zip file1 file2    # Create ZIP
unzip archive.zip
```

### Low-level Disk Copy

Copy disk or partition byte-for-byte:

```bash
sudo dd if=/dev/sda of=disk_backup.img bs=4M
sudo dd if=disk_backup.img of=/dev/sda bs=4M
```

Options:
- `if` — Input file
- `of` — Output file
- `bs` — Block size (4M is safe)
- `status=progress` — Show progress
- `conv=fsync` — Ensure sync before completion

Monitor progress:

```bash
sudo dd if=/dev/sda of=/backup.img bs=4M status=progress
```

### Backup with Rsync

Simple daily backup:

```bash
#!/bin/bash
SOURCE="/home/user/important"
DEST="/mnt/backup/important"
rsync -avz --delete "$SOURCE" "$DEST"
```

Backup script with timestamp:

```bash
#!/bin/bash
DATE=$(date +%Y-%m-%d_%H-%M-%S)
rsync -avz /home/user /backup/user_$DATE/
```

Full backup and incremental approach:

```bash
# Full backup
rsync -avz /data /backup/full_$(date +%Y-%m-%d)/

# Daily incremental (only changed files)
rsync -avz --delete /data /backup/latest/
```

### Snapshot Strategies

LVM snapshots for point-in-time backups:

```bash
# Create snapshot of logical volume
sudo lvcreate -L 10G -s -n backup /dev/vg0/lv_data

# Mount snapshot
sudo mount /dev/vg0/backup /mnt/snapshot

# Backup from snapshot
rsync -avz /mnt/snapshot /backup/

# Remove snapshot
sudo umount /mnt/snapshot
sudo lvremove /dev/vg0/backup
```

## Troubleshooting Guide

### Systematic Troubleshooting Approach

1. **Check Logs** — Always start here
2. **Check Services** — Are relevant services running?
3. **Check Network** — Can you reach the service?
4. **Check Disk** — Is disk full?
5. **Check Memory/CPU** — Resource constraints?

### Common Issues and Solutions

**Service won't start:**

```bash
# 1. Check service status and logs
sudo systemctl status myservice
journalctl -u myservice -n 50

# 2. Check if port is in use
sudo ss -tlnp | grep :8080

# 3. Check permissions
ls -la /opt/myapp
sudo chown myuser:mygroup /opt/myapp

# 4. Test service manually
/opt/myapp/run.sh
```

**High CPU or memory:**

```bash
# Find top processes
top -b -n 1 | head -20
ps aux --sort=-%cpu | head -10
ps aux --sort=-%mem | head -10

# Check what a process is doing
strace -p PID
lsof -p PID
```

**Disk full:**

```bash
# Find large files
du -sh /* | sort -hr
find / -size +1G -type f 2>/dev/null

# Find disk usage by directory
du -h --max-depth=2 / | sort -hr

# Check inode usage
df -i
```

**Network not working:**

```bash
# Check connectivity
ping -c 4 8.8.8.8
ping -c 4 gateway.local

# Check DNS
dig example.com
cat /etc/resolv.conf

# Check routes
ip route
traceroute example.com

# Check interface
ip addr
sudo ip link set eth0 up
```

**Can't connect to SSH:**

```bash
# Check SSH is running
sudo systemctl status ssh

# Check port
sudo ss -tlnp | grep ssh

# Test connectivity
ssh -v user@host  # Verbose mode

# Check SSH config
sudo sshd -t      # Test config syntax
```

## Exercises

### Exercise 1: Disk Partitioning and Formatting

Partition a disk, create filesystems, and mount them:

1. List all block devices: `lsblk`
2. Create a new partition on `/dev/sdb` using `fdisk`
3. Format the partition as ext4
4. Create a mount point: `mkdir -p /mnt/data`
5. Mount the partition
6. Verify the mount: `df -h`
7. Add to `/etc/fstab` for persistent mounting

### Exercise 2: Network Configuration and Testing

Configure networking and test connectivity:

1. View current IP configuration: `ip addr`
2. Add a secondary IP address temporarily
3. Test DNS resolution: `dig google.com`
4. Trace route to a remote host: `traceroute`
5. Check listening ports: `ss -tlnp`
6. Test a connection: `curl -I https://example.com`

### Exercise 3: Systemd Service Creation

Create a custom systemd service:

1. Create a simple script in `/opt/myapp/run.sh`
2. Create service file at `/etc/systemd/system/myapp.service`
3. Set appropriate permissions and reload daemon
4. Enable the service: `systemctl enable myapp`
5. Start and check status: `systemctl start myapp && systemctl status myapp`
6. View logs: `journalctl -u myapp -f`

### Exercise 4: Cron Job Scheduling

Schedule and monitor cron jobs:

1. Create a backup script: `/usr/local/bin/backup.sh`
2. Edit crontab: `crontab -e`
3. Schedule daily backup at 2 AM: `0 2 * * * /usr/local/bin/backup.sh`
4. List crontab entries: `crontab -l`
5. Monitor execution: `grep CRON /var/log/syslog`
6. Create systemd timer as alternative

### Exercise 5: SSH Key Setup and Hardening

Configure passwordless SSH:

1. Generate SSH key pair: `ssh-keygen -t ed25519`
2. Copy key to remote: `ssh-copy-id -i ~/.ssh/id_ed25519.pub user@host`
3. Test key-based login: `ssh user@host`
4. Disable password auth on server: edit `/etc/ssh/sshd_config`
5. Set `PasswordAuthentication no`
6. Restart SSH and test

### Exercise 6: Log Analysis and Rotation

Work with system logs:

1. View recent auth attempts: `tail /var/log/auth.log`
2. Search for errors: `grep ERROR /var/log/syslog`
3. Check boot messages: `dmesg | head -20`
4. Create logrotate config in `/etc/logrotate.d/`
5. Test rotation: `sudo logrotate -f /etc/logrotate.d/myconfig`
6. Verify logs are rotated: `ls -la /var/log/myapp*`

### Exercise 7: Performance Monitoring

Monitor system performance:

1. Check uptime and load: `uptime`
2. Monitor processes in real-time: `top`
3. View memory stats: `free -h`
4. Check I/O performance: `iostat -x 1 5`
5. Monitor network: `ss -s`
6. Create performance baseline script

### Exercise 8: Backup and Recovery

Create and test backup strategy:

1. Create backup directory: `mkdir -p /mnt/backup`
2. Create rsync backup script
3. Schedule with crontab
4. Verify backup contents
5. Test restore procedure
6. Document recovery steps

---

**Next Steps:** Review the [Security](/docs/tools/linux/security) guide for hardening your Linux systems.
