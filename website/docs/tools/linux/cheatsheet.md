---
title: "Linux Command Cheat Sheet"
sidebar_label: "Cheat Sheet"
description: "200+ essential Linux commands organized by category — files, processes, networking, text processing, disk, users, and more"
sidebar_position: 5
---

# Linux Command Cheat Sheet

A comprehensive reference for essential Linux commands organized by function. Master file operations, process management, networking, system administration, and more.

---

## 1. Navigation & File System

| Command | Description |
|---------|-------------|
| `pwd` | Print working directory |
| `ls` | List directory contents |
| `ls -la` | List with hidden files and full details |
| `ls -lh` | List with human-readable file sizes |
| `ls -lt` | List sorted by modification time |
| `cd` | Change directory |
| `cd ~` | Change to home directory |
| `cd -` | Change to previous directory |
| `cd /` | Change to root directory |
| `mkdir` | Create a directory |
| `mkdir -p` | Create directories recursively |
| `rmdir` | Remove empty directory |
| `cp` | Copy file or directory |
| `cp -r` | Copy directory recursively |
| `cp -v` | Copy with verbose output |
| `mv` | Move or rename file/directory |
| `rm` | Remove file |
| `rm -r` | Remove directory recursively |
| `rm -f` | Force remove without confirmation |
| `touch` | Create empty file or update timestamp |
| `find` | Search for files by name, type, size, etc. |
| `find . -name "*.txt"` | Find all .txt files in current directory |
| `find / -size +100M` | Find files larger than 100MB |
| `locate` | Find files by name (fast, uses database) |
| `updatedb` | Update locate database |
| `tree` | Display directory structure as tree |
| `tree -L 2` | Show tree with depth limit 2 |
| `ln` | Create hard link |
| `ln -s` | Create symbolic link |
| `readlink` | Display target of symbolic link |
| `stat` | Display file/directory statistics |
| `file` | Determine file type |
| `basename` | Get filename from path |
| `dirname` | Get directory path from full path |

---

## 2. File Viewing & Editing

| Command | Description |
|---------|-------------|
| `cat` | Display file contents |
| `cat file1 file2` | Concatenate multiple files |
| `head` | Show first 10 lines |
| `head -n 20` | Show first 20 lines |
| `tail` | Show last 10 lines |
| `tail -f` | Follow file (watch updates) |
| `less` | View file with pagination |
| `more` | View file with pagination (less advanced) |
| `nano` | Simple text editor |
| `vi` or `vim` | Advanced text editor |
| `vim -u NONE` | Open vim without configuration |
| `tee` | Read from stdin and write to file and stdout |
| `wc` | Count words, lines, characters |
| `wc -l` | Count lines only |
| `nl` | Display file with line numbers |
| `od` | Dump file in octal/hex format |
| `hexdump` | Display file in hexadecimal |
| `xxd` | Create hex dumps or convert hex to binary |

---

## 3. Text Processing

| Command | Description |
|---------|-------------|
| `grep` | Search for pattern in files |
| `grep -i` | Case-insensitive search |
| `grep -r` | Search recursively in directories |
| `grep -v` | Invert match (show non-matching lines) |
| `grep -n` | Show line numbers |
| `grep -E` | Use extended regex |
| `egrep` | Extended grep (equivalent to grep -E) |
| `fgrep` | Fixed string search (no regex) |
| `sed` | Stream editor for filtering/transforming text |
| `sed 's/old/new/'` | Replace first occurrence on each line |
| `sed 's/old/new/g'` | Replace all occurrences |
| `sed -n '5,10p'` | Print lines 5-10 |
| `sed '10d'` | Delete line 10 |
| `awk` | Text processing and pattern scanning |
| `awk '{print $1}'` | Print first column |
| `awk -F:` | Set field separator |
| `sort` | Sort lines |
| `sort -n` | Numeric sort |
| `sort -r` | Reverse sort |
| `sort -u` | Sort and remove duplicates |
| `uniq` | Remove duplicate consecutive lines |
| `uniq -c` | Count duplicate lines |
| `cut` | Extract columns from text |
| `cut -d: -f1` | Extract field 1 with : separator |
| `tr` | Translate or delete characters |
| `tr 'a-z' 'A-Z'` | Convert lowercase to uppercase |
| `paste` | Merge lines from files |
| `join` | Join lines based on common field |
| `diff` | Show differences between files |
| `diff -u` | Unified diff format |
| `comm` | Compare sorted files line by line |
| `xargs` | Build and execute command from input |
| `fmt` | Format text paragraphs |
| `column` | Format text into columns |
| `column -t` | Align text as table |

---

## 4. Permissions & Ownership

| Command | Description |
|---------|-------------|
| `chmod` | Change file/directory permissions |
| `chmod 755` | rwxr-xr-x |
| `chmod 644` | rw-r--r-- |
| `chmod u+x` | Add execute for owner |
| `chmod g-r` | Remove read from group |
| `chmod a+r` | Add read for all |
| `chown` | Change file owner |
| `chown user:group` | Change owner and group |
| `chown -R` | Change ownership recursively |
| `chgrp` | Change group ownership |
| `umask` | Set default permissions for new files |
| `getfacl` | Display ACL permissions |
| `setfacl` | Set ACL permissions |
| `chattr` | Change file attributes (immutable, etc.) |
| `chattr +i` | Make file immutable |
| `chattr -i` | Remove immutable attribute |
| `lsattr` | List file attributes |

---

## 5. User & Group Management

| Command | Description |
|---------|-------------|
| `useradd` | Create new user |
| `useradd -m` | Create user with home directory |
| `useradd -s /bin/bash` | Specify login shell |
| `userdel` | Delete user |
| `userdel -r` | Delete user and home directory |
| `usermod` | Modify user account |
| `usermod -aG group user` | Add user to group |
| `usermod -d /home/newdir` | Change home directory |
| `usermod -s /bin/zsh` | Change login shell |
| `groupadd` | Create new group |
| `groupdel` | Delete group |
| `groupmod` | Modify group |
| `passwd` | Change user password |
| `passwd -l` | Lock user account |
| `passwd -u` | Unlock user account |
| `whoami` | Display current user |
| `id` | Display user/group IDs |
| `id username` | Show info for specific user |
| `who` | List logged-in users |
| `w` | Show logged-in users and what they're doing |
| `last` | Show login history |
| `lastlog` | Show recent login information |
| `su` | Switch user |
| `su -` | Switch user with login shell |
| `sudo` | Execute as superuser |
| `sudo -i` | Open root shell |
| `sudo -u user` | Execute as specific user |
| `visudo` | Edit sudoers file safely |
| `finger` | Display user information |
| `finger username` | Get info about specific user |

---

## 6. Process Management

| Command | Description |
|---------|-------------|
| `ps` | Display processes |
| `ps aux` | Show all processes with details |
| `ps aux \| grep name` | Find process by name |
| `ps -ef` | Full format process listing |
| `ps -eo pid,user,cpu,mem,comm` | Custom output format |
| `top` | Interactive process monitor |
| `top -u user` | Monitor processes for specific user |
| `htop` | Enhanced process monitor |
| `atop` | Advanced system monitor |
| `kill` | Terminate process by PID |
| `kill -9` | Force kill process |
| `kill -TERM` | Graceful termination |
| `killall` | Terminate processes by name |
| `killall -9` | Force kill all processes by name |
| `pkill` | Kill processes matching pattern |
| `pkill -f command` | Kill by full command match |
| `nice` | Start process with priority |
| `nice -n 10 command` | Lower priority (higher is less important) |
| `renice` | Change process priority |
| `renice -n 5 -p PID` | Adjust priority of running process |
| `bg` | Resume suspended job in background |
| `fg` | Bring background job to foreground |
| `jobs` | List background/suspended jobs |
| `nohup` | Run command immune to hangups |
| `nohup command &` | Run in background and log to nohup.out |
| `screen` | Terminal multiplexer/session manager |
| `tmux` | Terminal multiplexer (modern alternative) |
| `pgrep` | Find processes by name/pattern |
| `pgrep -l` | Show process name and PID |

---

## 7. Networking

| Command | Description |
|---------|-------------|
| `ip addr` | Show IP addresses |
| `ip addr show` | Display all interfaces |
| `ip addr add IP/MASK dev eth0` | Add IP address |
| `ip link` | Show link layer information |
| `ip link set eth0 up` | Bring interface up |
| `ip link set eth0 down` | Bring interface down |
| `ifconfig` | Legacy network interface config (deprecated) |
| `ifconfig eth0` | Show specific interface |
| `ss` | Socket statistics (modern netstat replacement) |
| `ss -tlnp` | Show listening TCP ports with processes |
| `ss -ulnp` | Show listening UDP ports with processes |
| `netstat` | Network statistics (legacy) |
| `netstat -tlnp` | Show listening ports |
| `netstat -an` | Show all connections |
| `ping` | Test connectivity to host |
| `ping -c 4` | Send 4 pings |
| `traceroute` | Trace route to host |
| `tracepath` | Trace path (doesn't need root) |
| `mtr` | Network diagnostic tool (ping + traceroute) |
| `dig` | DNS query tool |
| `dig example.com` | Query A record |
| `dig +short` | Short answer only |
| `dig @8.8.8.8` | Query specific nameserver |
| `nslookup` | Query DNS (simpler than dig) |
| `host` | Simple DNS lookup |
| `curl` | Transfer data with URLs |
| `curl -I` | Fetch headers only |
| `curl -X POST -d data` | Make POST request |
| `wget` | Download files from web |
| `wget -O filename` | Save with specific name |
| `wget -r` | Download recursively |
| `scp` | Secure copy between hosts |
| `scp file user@host:/path/` | Copy file to remote |
| `rsync` | Synchronize files between systems |
| `rsync -avz` | Verbose, archive, compressed |
| `ssh` | Secure shell connection |
| `ssh -i key.pem user@host` | Connect with specific key |
| `ssh -L 8000:localhost:3000` | Local port forwarding |
| `ssh -R 8000:localhost:3000` | Remote port forwarding |
| `ssh -D 1080` | Dynamic proxy (SOCKS) |
| `nc` | Netcat - network utility |
| `nc -l -p 5000` | Listen on port 5000 |
| `nc -zv host port` | Check if port is open |
| `tcpdump` | Packet sniffer |
| `tcpdump -i eth0` | Capture on specific interface |
| `tcpdump -n` | Don't resolve DNS |
| `nmap` | Network scanner |
| `nmap -sn 192.168.1.0/24` | Scan network for hosts |
| `nmap -p 80,443` | Scan specific ports |
| `telnet` | Telnet client (insecure) |
| `hostname` | Display system hostname |
| `hostname -I` | Show all IP addresses |
| `hostnamectl` | Manage system hostname |
| `route` | Show routing table |
| `route -n` | Numeric (no DNS resolution) |
| `arp` | Address resolution protocol table |
| `arp -a` | Show ARP cache |

---

## 8. Disk & Storage

| Command | Description |
|---------|-------------|
| `df` | Disk free space |
| `df -h` | Human-readable format |
| `df -T` | Show filesystem type |
| `du` | Disk usage of directories |
| `du -sh` | Summary in human-readable format |
| `du -sh *` | Size of each item in directory |
| `du -sh * \| sort -h` | Show sizes sorted |
| `lsblk` | List block devices |
| `blkid` | Show block device IDs/UUIDs |
| `fdisk` | Partition table manipulator |
| `fdisk -l` | List partitions |
| `parted` | GNU Parted partition editor |
| `mount` | Mount filesystem |
| `mount /dev/sda1 /mnt/` | Mount device to mountpoint |
| `umount` | Unmount filesystem |
| `umount /mnt/` | Unmount directory |
| `mkfs` | Create filesystem |
| `mkfs.ext4` | Create ext4 filesystem |
| `mkfs.vfat` | Create FAT filesystem |
| `fsck` | Filesystem check and repair |
| `e2fsck` | Check ext filesystem |
| `fstab` | Static filesystem mount table (/etc/fstab) |
| `dd` | Copy and convert data |
| `dd if=/dev/zero of=file bs=1M count=100` | Create 100MB file |
| `dd if=/dev/sda of=backup.img` | Disk backup |

---

## 9. Archive & Compression

| Command | Description |
|---------|-------------|
| `tar` | Archive files |
| `tar -cvf archive.tar files/` | Create archive |
| `tar -xvf archive.tar` | Extract archive |
| `tar -tf archive.tar` | List contents |
| `tar -czf archive.tar.gz files/` | Create gzip compressed |
| `tar -xzf archive.tar.gz` | Extract gzip |
| `tar -cjf archive.tar.bz2 files/` | Create bzip2 compressed |
| `tar -xjf archive.tar.bz2` | Extract bzip2 |
| `gzip` | Compress file |
| `gzip file` | Creates file.gz |
| `gunzip` | Decompress gzip |
| `bzip2` | Compress with bzip2 |
| `bunzip2` | Decompress bzip2 |
| `xz` | Compress with xz |
| `unxz` | Decompress xz |
| `zip` | ZIP archive |
| `zip -r archive.zip folder/` | Zip directory recursively |
| `unzip` | Extract ZIP |
| `unzip -l` | List ZIP contents |
| `7z` | 7-Zip archive tool |
| `zcat` | View compressed file contents |
| `zgrep` | Grep in compressed file |

---

## 10. System Information

| Command | Description |
|---------|-------------|
| `uname` | System information |
| `uname -a` | All system information |
| `uname -r` | Kernel release/version |
| `uname -m` | Machine hardware name (x86_64) |
| `lsb_release -a` | Linux distribution version |
| `cat /etc/os-release` | OS information |
| `hostname` | System hostname |
| `uptime` | System uptime and load |
| `date` | Current date and time |
| `cal` | Calendar |
| `free` | Memory usage |
| `free -h` | Human-readable format |
| `free -m` | Memory in megabytes |
| `lscpu` | CPU information |
| `lspci` | PCI devices (graphics cards, etc.) |
| `lsusb` | USB devices |
| `dmesg` | Kernel messages |
| `dmesg \| tail` | Latest kernel messages |
| `journalctl` | System journal logs |
| `journalctl -xe` | Recent errors |
| `timedatectl` | Time and date settings |
| `timedatectl set-timezone` | Set timezone |
| `hwinfo` | Detailed hardware information |
| `inxi` | System information summary |

---

## 11. Service Management

| Command | Description |
|---------|-------------|
| `systemctl` | Control systemd services |
| `systemctl status service` | Check service status |
| `systemctl start service` | Start service |
| `systemctl stop service` | Stop service |
| `systemctl restart service` | Restart service |
| `systemctl reload service` | Reload configuration |
| `systemctl enable service` | Enable service at boot |
| `systemctl disable service` | Disable service at boot |
| `systemctl list-units` | List all units |
| `systemctl list-units --failed` | Show failed units |
| `journalctl -u service` | View service logs |
| `journalctl -u service -f` | Follow service logs |

---

## 12. Package Management

| Command | Description |
|---------|-------------|
| `apt` | Debian/Ubuntu package manager |
| `apt update` | Update package lists |
| `apt upgrade` | Upgrade packages |
| `apt install package` | Install package |
| `apt remove package` | Remove package |
| `apt search package` | Search for package |
| `apt list --upgradable` | Show upgradable packages |
| `yum` | RedHat/CentOS package manager |
| `yum install package` | Install on RHEL/CentOS |
| `yum update` | Update packages |
| `dnf` | Modern Fedora package manager |
| `dnf install package` | Install on Fedora |
| `pacman` | Arch Linux package manager |
| `pacman -S package` | Install on Arch |
| `apk` | Alpine Linux package manager |
| `apk add package` | Install on Alpine |
| `rpm` | Red Hat Package Manager |
| `rpm -i package.rpm` | Install RPM |
| `rpm -qa` | Query all installed packages |
| `dpkg` | Debian package manager |
| `dpkg -l` | List installed packages |
| `dpkg -i package.deb` | Install DEB |
| `snap` | Universal package manager |
| `snap install app` | Install snap application |

---

## 13. Shell Scripting Essentials

| Command | Description |
|---------|-------------|
| `echo` | Print text |
| `echo -n` | Print without newline |
| `echo -e` | Enable escape sequences |
| `echo $VAR` | Print variable value |
| `read` | Read user input |
| `read -p "prompt" var` | Prompt and read into variable |
| `test` | Evaluate condition (same as [) |
| `[ -f file ]` | Test if file exists |
| `[ -d dir ]` | Test if directory exists |
| `[ -z string ]` | Test if string is empty |
| `[ $a -eq $b ]` | Numeric equality |
| `[ $a -lt $b ]` | Less than |
| `[[ $str =~ pattern ]]` | Regex match |
| `if then fi` | Conditional statement |
| `if then else fi` | If-else statement |
| `if then elif then fi` | If-elif-else |
| `for var in list; do done` | For loop |
| `for ((i=0; i less than 10; i++)); do done` | C-style for loop |
| `while condition; do done` | While loop |
| `case $var in pattern) ;; esac` | Switch statement |
| `function name() { }` | Define function |
| `$0, $1, $2` | Script name and arguments |
| `$#` | Number of arguments |
| `$@` | All arguments as array |
| `$?` | Exit code of last command |
| `exit 0` | Exit with code 0 (success) |
| `trap 'cleanup' EXIT` | Run command on exit/signal |

---

## 14. Useful One-Liners

### File Operations

```bash
# Find largest files in current directory
find . -type f -exec ls -lh {} \; | sort -k5 -rh | head -10

# Find files modified in last 7 days
find . -type f -mtime -7

# Find duplicate files
find . -type f -exec md5sum {} \; | sort | uniq -d

# Count all lines in all files in directory
find . -type f -name "*.txt" -exec wc -l {} + | tail -1

# Recursively search for string in files
grep -r "search_term" /path/to/search

# Replace string in all files in directory
find . -type f -name "*.txt" -exec sed -i 's/old/new/g' {} \;
```

### Monitoring & Processes

```bash
# Monitor log file in real-time
tail -f /var/log/syslog

# Find which process is using a specific port
lsof -i :8080

# Check all open ports and processes
netstat -tulnp | grep LISTEN

# Show CPU and memory usage by process
ps aux --sort=-%cpu | head -10
ps aux --sort=-%mem | head -10

# Monitor directory for changes
watch -n 2 'ls -lah /path/to/dir'
```

### Network

```bash
# Check if port is open
nc -zv hostname port

# Quick DNS lookup
dig +short example.com

# Find all hosts on network
nmap -sn 192.168.1.0/24

# Show current network connections
ss -tan | grep ESTABLISHED
```

### System Administration

```bash
# Show disk usage by directory (top 10)
du -sh */ | sort -rh | head -10

# List files by size in current directory
ls -lhS

# Count files in directory
find . -type f | wc -l

# Show system load average
uptime

# Display top resource consumers
top -b -n 1 | head -20
```

### Text Processing

```bash
# Extract column from CSV
awk -F',' '{print $1, $3}' file.csv

# Sort and count occurrences
sort | uniq -c | sort -rn

# Remove duplicates while preserving order
awk '!seen[$0]++' file.txt

# Print specific lines
sed -n '10,20p' file.txt

# Split large file into parts
split -l 1000 largefile.txt part_

# Merge sorted files
sort -m file1.txt file2.txt file3.txt
```

---

## Quick Reference by Task

**Need to find something?**
- `find` - Comprehensive file search
- `locate` - Quick file search (uses database)
- `grep` - Search within files

**Need to view/edit files?**
- `cat` - Quick view
- `less` - Scroll through large files
- `nano` - Simple editor
- `vim` - Powerful editor

**Need to manage processes?**
- `ps` - List processes
- `top` / `htop` - Interactive monitor
- `kill` - Terminate process
- `bg` / `fg` - Background/foreground

**Need to check system?**
- `df` - Disk space
- `free` - Memory usage
- `top` - CPU/memory
- `systemctl` - Service status

**Need to manage users?**
- `useradd` / `userdel` - Create/delete users
- `passwd` - Change passwords
- `chmod` / `chown` - Permissions/ownership

**Need to work with archives?**
- `tar` - Most common
- `zip` / `unzip` - ZIP format
- `gzip` / `gunzip` - Gzip compression
