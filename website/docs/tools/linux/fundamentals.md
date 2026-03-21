---
title: "Linux Fundamentals"
sidebar_label: "Fundamentals"
description: "Linux fundamentals — file system, shell basics, permissions, processes, package management, and getting started"
sidebar_position: 1
---

# Linux Fundamentals

Master the core concepts of Linux, from the kernel and filesystem to permissions, processes, and package management. This guide covers everything you need to work effectively on Linux systems for cloud, DevOps, and operations roles.

---

## What is Linux?

### History and Origins

Linux was created by **Linus Torvalds** in **1991** as a free, open-source Unix-like kernel. What began as a personal project has evolved into one of the world's most influential operating systems. The Linux kernel manages hardware resources and allows multiple programs to run simultaneously while isolating them from each other.

### Kernel vs. Distributions

- **Linux Kernel**: The core that manages CPU, memory, disk, and devices. It's developed by thousands of contributors worldwide.
- **Linux Distribution**: A packaged combination of the Linux kernel, GNU utilities, package managers, and additional software. Examples: Ubuntu, CentOS, Debian, Fedora.

### Why Linux Matters for DevOps and Cloud

- **Ubiquitous in cloud infrastructure**: AWS, Azure, GCP, and all major cloud providers run on Linux
- **Open source**: Transparency, security auditing, and community-driven development
- **Lightweight and efficient**: Runs on everything from servers to containers to IoT devices
- **Free**: No licensing costs; reduces operational expenses
- **Community support**: Millions of developers, extensive documentation, and active forums
- **Scripting and automation**: Bash scripting enables powerful automation and infrastructure-as-code practices

### Market Share

- **Servers**: ~97% of top 1 million web servers run Linux
- **Cloud**: 99%+ of workloads on public clouds use Linux
- **Embedded systems**: Powers billions of devices (Android, routers, smart appliances)
- **Supercomputers**: 100% of the world's top 500 supercomputers run Linux

---

## Linux Distributions

A Linux distribution packages the kernel with tools, package managers, and system utilities. Here are the most common distributions used in cloud and DevOps:

| Distribution | Base | Use Cases | Package Manager | Target |
|---|---|---|---|---|
| **Ubuntu** | Debian | Cloud, servers, beginners | apt | Enterprise, startups |
| **Debian** | Independent | Stability-focused servers | apt | Server stability |
| **CentOS** | RHEL-compatible | Enterprise servers | yum/dnf | Enterprise, cost-conscious |
| **RHEL** | Independent | Enterprise, support-critical | yum/dnf | Enterprise (paid support) |
| **Fedora** | Cutting-edge | Development, latest features | dnf | Developers, innovation |
| **Alpine** | Independent | Containers, minimal footprint | apk | Docker, embedded |
| **Arch** | Independent | Developers, rolling release | pacman | Customization, control |
| **Amazon Linux** | RHEL-based | AWS EC2, AWS optimization | yum/dnf | AWS ecosystem |

**Choosing a distribution**:
- **Enterprise production**: CentOS, RHEL, or Ubuntu LTS
- **Cloud native/containers**: Alpine or Ubuntu
- **Learning and development**: Ubuntu or Debian (largest communities)
- **Rolling updates**: Arch or Fedora

---

## Linux File System Hierarchy

Linux follows a hierarchical directory structure called the **Filesystem Hierarchy Standard (FHS)**. Understanding this structure is essential for navigating and administering Linux systems.

| Directory | Purpose | Contains |
|---|---|---|
| **/** | Root directory | All files and directories start here |
| **/home** | User home directories | User files, documents, downloads (`/home/username`) |
| **/root** | Root user's home | Root user's home directory (not in `/home`) |
| **/etc** | System configuration | Config files (passwd, shadow, hosts, nginx.conf, docker) — text-based |
| **/var** | Variable data | Logs (`/var/log`), caches, temporary data, databases |
| **/tmp** | Temporary files | Temporary storage; cleared on reboot (world-writable) |
| **/usr** | User programs | Applications, libraries, documentation (not user home) |
| **/usr/bin** | User binaries | Executable programs (`ls`, `cat`, `grep`, `vim`) |
| **/usr/sbin** | System binaries | System admin programs (requires root: `useradd`, `systemctl`) |
| **/usr/lib** | User libraries | Shared libraries for programs |
| **/bin** | Essential binaries | Critical programs needed for boot (`sh`, `ls`, `cp`) |
| **/sbin** | Essential system binaries | Critical system admin programs (`init`, `ifconfig`) |
| **/opt** | Optional software | Third-party applications (e.g., `/opt/docker`, `/opt/myapp`) |
| **/proc** | Process information | Virtual filesystem; system and process info (`/proc/cpuinfo`, `/proc/meminfo`) |
| **/dev** | Device files | Hardware device access (`/dev/sda`, `/dev/null`, `/dev/tty`) |
| **/sys** | Kernel interfaces | Kernel subsystems and hardware configuration |
| **/boot** | Boot files | Kernel, bootloader, initrd images |
| **/lib** | System libraries | Essential shared libraries for `/bin` and `/sbin` |
| **/mnt** | Mount points | Temporary mount locations (USB drives, NFS, ISO) |
| **/media** | Removable media | Auto-mount points for USB drives and CDs |
| **/srv** | Service data | Data served by services (web server files, FTP) |
| **/run** | Runtime data | Process runtime data (`/run/pid`, sockets) |

**Example navigation**:

```bash
cd /
pwd
ls -la
cd /etc
ls -la passwd shadow group
cd /var/log
ls -la
tail -f syslog
cd ~
pwd
```

---

## Shell Basics

### What is a Shell?

A **shell** is a command-line interface that interprets your commands and communicates with the Linux kernel. It reads commands, executes them, and returns output.

### Common Shells

| Shell | Features | Common On |
|---|---|---|
| **bash** (Bourne-Again SHell) | Most popular; powerful scripting | Linux, macOS, default on most systems |
| **zsh** (Z Shell) | Modern improvements; interactive | macOS (default), Linux, developers |
| **sh** (Bourne Shell) | POSIX standard; minimal | System scripts, embedded |
| **fish** | User-friendly; built-in autocompletion | Development workstations |
| **ksh** | Professional; efficient | AIX, Solaris, enterprise |

**Check your current shell**:

```bash
echo $SHELL
ps -p $$
```

### Navigating the Filesystem

```bash
pwd                 # Print working directory
cd /path/to/dir     # Change directory
cd ~                # Home directory
cd ..               # Parent directory
cd -                # Previous directory
ls                  # List files
ls -la              # List all files with details (including hidden)
ls -lh              # Human-readable sizes
ls -lS              # Sort by size
ls -lt              # Sort by modification time
```

### Command Structure

```bash
command [options] [arguments]
```

Example:

```bash
grep -r "error" /var/log
#  ^    ^       ^
#  |    |       arguments (search location)
#  |    options (-r = recursive)
#  command
```

### Man Pages

The manual is your best friend. Access comprehensive documentation:

```bash
man ls              # Full manual for 'ls'
man -k passwd       # Search for keywords
man -s 5 passwd     # Man section 5 (files)
man 5 passwd        # Shorthand for section 5
info ls             # GNU Info (alternative to man)
ls --help           # Brief help
```

**Man page sections**:
- Section 1: User commands
- Section 5: File formats and configuration files
- Section 8: System administration commands

### Tab Completion and History

Tab completion saves time and prevents typos:

```bash
cd /etc/a[TAB]         # Auto-completes paths
grep --in[TAB]         # Auto-completes options
```

Navigate command history:

```bash
history                # Show command history
!42                    # Execute command 42
!grep                  # Execute last grep command
!!                     # Execute previous command
CTRL+R                 # Reverse search through history
CTRL+A                 # Move to beginning of line
CTRL+E                 # Move to end of line
```

### Aliases and Functions

Create shortcuts for frequently used commands:

```bash
alias ll='ls -lah'
alias grep='grep --color=auto'
alias myapp='docker run -it myapp:latest'
unalias ll             # Remove alias
alias                  # List all aliases
```

**Make aliases permanent** by adding to `~/.bashrc` or `~/.bash_profile`:

```bash
echo "alias ll='ls -lah'" >> ~/.bashrc
source ~/.bashrc       # Reload shell configuration
```

### Environment Variables

Environment variables store configuration values available to all programs:

```bash
echo $PATH             # Search path for commands
echo $HOME             # User home directory
echo $USER             # Current user
echo $SHELL            # Current shell
echo $PWD              # Current directory
printenv               # List all environment variables
export MYVAR="value"   # Set and export variable
```

**Set variables permanently** in `~/.bashrc` or `~/.bash_profile`:

```bash
export JAVA_HOME=/usr/lib/jvm/java-11-openjdk
export PATH=$PATH:/opt/custom/bin
```

### Shell Configuration Files

- **`~/.bashrc`**: Sourced for every interactive shell (aliases, functions, PATH)
- **`~/.bash_profile`**: Sourced for login shells (environment variables)
- **`/etc/profile`**: System-wide login shell configuration
- **`/etc/bashrc`**: System-wide interactive shell configuration

**Load changes without restarting**:

```bash
source ~/.bashrc
# or
. ~/.bashrc
```

---

## File and Directory Operations

### Creating and Managing Files

```bash
mkdir mydir                # Create directory
mkdir -p path/to/dir       # Create nested directories
touch myfile.txt           # Create empty file
touch file1 file2 file3    # Create multiple files
```

### Copying, Moving, and Renaming

```bash
cp source.txt dest.txt     # Copy file
cp -r sourcedir/ destdir/  # Copy directory recursively
cp -i source.txt dest.txt  # Interactive (prompt before overwrite)
cp -v source.txt dest.txt  # Verbose output

mv oldname.txt newname.txt # Rename file
mv file.txt /path/to/dir/  # Move file to directory
mv -i source dest          # Interactive

rm myfile.txt              # Delete file
rm -r mydir/               # Delete directory and contents
rm -i myfile.txt           # Interactive confirmation
rm -f myfile.txt           # Force (no confirmation)
rmdir emptydir             # Remove empty directory only
```

### Finding Files

Search by name, type, size, modification time:

```bash
find /home -name "*.log"           # Find by name
find /var -type f -name "*.txt"    # Files only
find /var -type d -name "backup"   # Directories only
find /home -size +100M             # Files larger than 100MB
find /home -mtime -7               # Modified in last 7 days
find /var/log -name "*.log" -delete # Find and delete

locate myfile              # Fast search (uses database)
updatedb                   # Update locate database

which python               # Find command in PATH
whereis python             # Find all locations (binary, source, man)
```

### Listing Files with Tree

```bash
tree /var/www              # Show directory tree
tree -L 2 /var/www         # Limit depth to 2 levels
tree -I 'node_modules'     # Exclude pattern
```

### Wildcards and Globbing

```bash
ls *.txt                   # All files ending in .txt
ls file?.txt               # file1.txt, file2.txt (one character)
ls file[12].txt            # file1.txt or file2.txt
ls file[1-3].txt           # file1.txt, file2.txt, file3.txt
ls file[!3].txt            # All except file3.txt
```

---

## Linux Permissions

### Permission Basics

Every file and directory in Linux has three types of permissions for three user classes:

- **User (u)**: The file owner
- **Group (g)**: The file's group owner
- **Other (o)**: Everyone else
- **All (a)**: User, group, and other

Each class has three permission types:

- **Read (r)**: Permission to read file contents or list directory
- **Write (w)**: Permission to modify file or create/delete files in directory
- **Execute (x)**: Permission to run file as program or enter directory

### Viewing Permissions

```bash
ls -l myfile.txt
# Output: -rw-r--r-- 1 john admin 1024 Mar 15 10:30 myfile.txt
#         ^^^^^^^^^
#         rwxrwxrwx (9 chars: user|group|other)
#         -rw-r--r-- means:
#         - (regular file)
#         rw- (user can read, write; no execute)
#         r-- (group can read only)
#         r-- (others can read only)

stat myfile.txt            # Detailed file information
```

### Numeric Permission Notation

Permissions are represented as octal numbers (0-7):

- **4** = Read (r)
- **2** = Write (w)
- **1** = Execute (x)

Combine for each class (user, group, other):

```
chmod 755 script.sh
#      ^^^
#      user(7=rwx), group(5=r-x), other(5=r-x)

chmod 644 myfile.txt
#      ^^^
#      user(6=rw-), group(4=r--), other(4=r--)

chmod 600 secret.txt
#      ^^^
#      user(6=rw-), group(0=---), other(0=---)
```

### Changing Permissions - Numeric

```bash
chmod 755 script.sh        # rwxr-xr-x (executable by all)
chmod 644 myfile.txt       # rw-r--r-- (readable by all, writable by owner)
chmod 600 secret.txt       # rw------- (readable/writable by owner only)
chmod 700 myscript         # rwx------ (owner can execute, others cannot)
```

### Changing Permissions - Symbolic

```bash
chmod u+x script.sh        # Add execute to user
chmod u-w document.txt     # Remove write from user
chmod g+r myfile.txt       # Add read to group
chmod o-rwx secret.txt     # Remove all permissions from other
chmod a+r document.txt     # Add read to all (user, group, other)
chmod u=rwx,g=rx,o=        # Set specific permissions (user=rwx, group=rx, other=none)
```

### Changing Ownership

```bash
chown newuser myfile.txt          # Change owner
chown newuser:newgroup myfile.txt # Change owner and group
chown -R newuser mydir/           # Recursive (all files in directory)
chgrp newgroup myfile.txt         # Change group only
chgrp -R newgroup mydir/          # Recursive for group
```

### Default Permissions - umask

The `umask` determines default permissions for new files and directories:

```bash
umask                      # View current umask
umask 0022                 # Set umask to 0022
# Default file permissions: 666 - 022 = 644 (rw-r--r--)
# Default directory permissions: 777 - 022 = 755 (rwxr-xr-x)

umask 0077                 # Restrictive: new files 600, dirs 700
umask 0002                 # Permissive: new files 664, dirs 775
```

### Special Permissions: SUID, SGID, Sticky Bit

#### SUID (Set User ID) - 4000

Execute the file as the owner, not the current user:

```bash
chmod 4755 script.sh       # SUID + rwxr-xr-x
ls -l script.sh
# Output: -rwsr-xr-x (note 's' in user execute position)

# Example: /usr/bin/passwd (owned by root, can run as root)
ls -l /usr/bin/passwd
# Output: -rwsr-xr-x 1 root root 68208 Mar 5 10:15 /usr/bin/passwd
```

#### SGID (Set Group ID) - 2000

Execute as the file's group owner. For directories, new files inherit the directory's group:

```bash
chmod 2755 script.sh       # SGID + rwxr-xr-x
ls -l script.sh
# Output: -rwxr-sr-x (note 's' in group execute position)

mkdir -m2755 shared_dir    # Shared directory where all files inherit group
```

#### Sticky Bit - 1000

On directories, prevents users from deleting files they don't own:

```bash
chmod 1777 /tmp            # rwxrwxrwt (everyone can create, only owner can delete)
ls -ld /tmp
# Output: drwxrwxrwt (note 't' in other execute position)

# Without sticky bit, user A could delete user B's files in /tmp
# With sticky bit, user A can only delete their own files
```

---

## User and Group Management

### Creating and Modifying Users

```bash
useradd newuser            # Create user (no home by default)
useradd -m newuser         # Create user with home directory
useradd -m -s /bin/bash newuser      # With bash shell
useradd -m -G group1,group2 newuser  # Add to groups
useradd -m -u 1050 newuser # Specific UID

usermod -aG docker user1   # Add user to group (append, don't remove from others)
usermod -s /bin/bash user1 # Change shell
usermod -d /home/newpath user1      # Change home directory
usermod -l newname oldname # Rename user
userdel user1              # Delete user (keep files)
userdel -r user1           # Delete user and home directory
```

### Group Management

```bash
groupadd mygroup           # Create group
groupadd -g 1050 mygroup   # Specific GID
groupmod -n newname oldname        # Rename group
groupdel mygroup           # Delete group
groups                     # Show user's groups
groups username            # Show user's groups
```

### Password Management

```bash
passwd                     # Change your password
passwd username            # Change another user's password (root only)
passwd -l username         # Lock user account
passwd -u username         # Unlock user account
passwd -e username         # Force password change on next login
passwd -d username         # Remove password (not recommended)
chage -l username          # Show password expiration info
chage -d 0 username        # Force password change on next login
```

### System User and Group Files

**`/etc/passwd`** — User account information (readable by all):

```
username:x:1000:1000:Full Name:/home/username:/bin/bash
^        ^    ^    ^    ^                      ^
|        |    |    |    |                      shell
|        |    |    |    comment (real name)
|        |    |    primary group ID (GID)
|        |    user ID (UID)
|        password placeholder (x = password in /etc/shadow)
username
```

**`/etc/shadow`** — Encrypted passwords (readable by root only):

```
username:$6$abcd...:19000:0:99999:7:::
^        ^           ^     ^ ^     ^
|        |           |     | |     password-less account
|        |           |     | grace period
|        |           |     days before expiration warning
|        |           min days between changes
|        |           days since last password change
|        encrypted password hash
username
```

**`/etc/group`** — Group information:

```
groupname:x:1000:user1,user2
^         ^    ^  ^
|         |    |  members (comma-separated)
|         |    group ID (GID)
|         placeholder for password
groupname
```

### Sudo and Sudoers

Run commands with elevated privileges:

```bash
sudo command               # Run command as root
sudo -u username command   # Run command as specific user
sudo -i                    # Open root shell
sudo -s                    # Open shell as root (preserves environment)
sudo !!                    # Re-run last command with sudo
sudo -l                    # List user's sudo permissions
sudoedit /etc/config       # Safe edit (uses sudo)
```

**Sudoers file** (`/etc/sudoers`):

```bash
# Edit safely (validates syntax, prevents lockout)
visudo

# Examples in sudoers:
user1 ALL=(ALL) ALL              # Full sudo access
user1 ALL=(ALL) NOPASSWD: ALL    # No password required
%wheel ALL=(ALL) ALL             # All users in 'wheel' group
user1 ALL=/usr/bin/systemctl     # Only specific commands
```

---

## Process Management

### Viewing Processes

```bash
ps                         # Current shell processes
ps aux                     # All processes (detailed)
ps aux | grep python       # Find python processes
ps -ef                     # All processes with full info
ps -eo pid,user,cmd        # Custom columns
pstree                     # Process tree hierarchy
pstree -p                  # With process IDs

top                        # Real-time process monitor (interactive)
top -u username            # Top processes for user
top -p 1234                # Monitor specific PID
htop                       # Enhanced top (if installed)
```

**ps output columns**:
- PID: Process ID
- USER: Process owner
- %CPU: CPU usage percentage
- %MEM: Memory usage percentage
- COMMAND: Command name

### Signals and Killing Processes

```bash
kill PID                   # Terminate process (default SIGTERM)
kill -9 PID                # Force kill (SIGKILL, cannot be caught)
kill -15 PID               # Terminate gracefully (SIGTERM)
killall firefox            # Kill all processes by name
killall -9 firefox         # Force kill all processes by name
pkill -f "python script"   # Kill by pattern (partial match)
```

**Common signals**:
- SIGTERM (15): Request termination (can be ignored)
- SIGKILL (9): Immediate termination (cannot be ignored)
- SIGSTOP (19): Suspend process
- SIGCONT (18): Resume process

### Process Priority and Nice

Lower nice value = higher priority (range: -20 to 19):

```bash
nice -n 10 mycommand       # Start with nice level 10 (lower priority)
renice -n 5 -p PID         # Change nice level of running process
ps -l                      # Show process priority (NI column)
```

### Background and Foreground Jobs

```bash
command &                  # Start process in background
CTRL+Z                     # Suspend current foreground process
bg                         # Resume suspended process in background
fg                         # Bring background process to foreground
jobs                       # List background jobs
jobs -l                    # List with process IDs
fg %1                      # Bring job 1 to foreground
kill %1                    # Kill job 1
nohup long_process &       # Process continues after logout
```

### Systemd and Service Management

Modern Linux systems use `systemd` for service management:

```bash
systemctl status service   # Check service status
systemctl start service    # Start service
systemctl stop service     # Stop service
systemctl restart service  # Restart service
systemctl reload service   # Reload configuration (without restarting)
systemctl enable service   # Start on boot
systemctl disable service  # Don't start on boot
systemctl list-units --type=service --all  # List all services
```

**Service command** (legacy, but still works):

```bash
service nginx start        # Legacy: start nginx
service nginx status       # Legacy: check status
```

### Viewing Logs

```bash
journalctl                 # Show all systemd logs
journalctl -u nginx        # Logs for specific service
journalctl -u nginx -f     # Follow logs (tail -f style)
journalctl -p err          # Error level only
journalctl --since "1 hour ago"        # Logs from last hour
journalctl -n 50           # Last 50 lines
journalctl --boot          # Logs from current boot
```

---

## Package Management

Package managers automate software installation, updates, and removal. Different distributions use different package managers.

### APT (Debian/Ubuntu)

```bash
apt update                 # Update package list
apt install package        # Install package
apt install package1 package2       # Install multiple
apt remove package         # Remove package (keep config)
apt purge package          # Remove package and config
apt upgrade                # Upgrade installed packages
apt full-upgrade           # Upgrade with dependency handling
apt search package         # Search for package
apt show package           # Show package info
apt list --installed       # List installed packages
apt autoremove             # Remove unused dependencies
apt clean                  # Remove cached packages
apt-cache search keyword   # Search (alternative to apt search)
apt-cache depends package  # Show dependencies
```

**Interactive example**:

```bash
apt update
apt search nginx
apt show nginx
apt install nginx
apt list --upgradable
apt upgrade
apt remove nginx
```

### YUM/DNF (RHEL, CentOS, Fedora)

**dnf** (newer, replaces yum):

```bash
dnf check-update           # Check for updates
dnf install package        # Install package
dnf remove package         # Remove package
dnf upgrade                # Upgrade all packages
dnf search package         # Search for package
dnf info package           # Show package info
dnf list installed         # List installed packages
dnf clean all              # Remove cache
dnf history                # Show transaction history
dnf groupinstall "Development Tools"    # Install group
```

**yum** (legacy, still works):

```bash
yum update                 # Update package list and install updates
yum install package        # Install package
yum remove package         # Remove package
yum search package         # Search for package
yum info package           # Show package info
yum list installed         # List installed packages
```

### Pacman (Arch Linux)

```bash
pacman -Sy                 # Update package list
pacman -S package          # Install package
pacman -R package          # Remove package
pacman -Rs package         # Remove with unused dependencies
pacman -Syu                # Update all packages
pacman -Qs package         # Search installed packages
pacman -Ss package         # Search repositories
pacman -Qi package         # Package info (installed)
pacman -Si package         # Package info (repository)
pacman -Sc                 # Clean cache
pacman -G base-devel       # Install package group
```

### APK (Alpine Linux)

```bash
apk update                 # Update package list
apk add package            # Install package
apk del package            # Remove package
apk upgrade                # Upgrade packages
apk search package         # Search for package
apk info -a                # List all installed packages
apk info package           # Show installed package info
apk cache clean            # Remove cache
```

### Comparing Package Managers

| Task | apt | dnf | pacman | apk |
|---|---|---|---|---|
| Install | `apt install pkg` | `dnf install pkg` | `pacman -S pkg` | `apk add pkg` |
| Update list | `apt update` | `dnf check-update` | `pacman -Sy` | `apk update` |
| Upgrade all | `apt upgrade` | `dnf upgrade` | `pacman -Syu` | `apk upgrade` |
| Remove | `apt remove pkg` | `dnf remove pkg` | `pacman -R pkg` | `apk del pkg` |
| Search | `apt search pkg` | `dnf search pkg` | `pacman -Ss pkg` | `apk search pkg` |
| Info | `apt show pkg` | `dnf info pkg` | `pacman -Si pkg` | `apk info pkg` |

---

## I/O Redirection and Pipes

### Standard Streams

Every process has three standard streams:
- **stdin (0)**: Standard input (keyboard by default)
- **stdout (1)**: Standard output (screen by default)
- **stderr (2)**: Standard error (screen by default)

### Output Redirection

```bash
command > file             # Redirect stdout to file (overwrite)
command >> file            # Redirect stdout to file (append)
command 2> file            # Redirect stderr to file
command 2>> file           # Append stderr to file
command > file 2>&1        # Redirect stdout and stderr to file
command &> file            # Shorthand: redirect both
command 1> out.txt 2> err.txt          # Separate stdout and stderr
command > /dev/null 2>&1   # Suppress all output
```

**Examples**:

```bash
ls /etc > files.txt        # Save directory listing
echo "data" >> log.txt     # Append to log
grep "error" *.log 2> errors.txt       # Capture errors
python script.py > output.txt 2>&1     # Save everything
```

### Input Redirection

```bash
command < file             # Use file as stdin
mail -s "Subject" user@example.com < message.txt    # Send email with file content
```

### Pipes

Connect output of one command to input of another:

```bash
command1 | command2        # Pipe stdout of command1 to command2
ps aux | grep python       # Find python processes
ls -la | head -10          # Show first 10 files
cat log.txt | grep "error" # Search for errors
du -sh * | sort -h         # Sort by size
```

**Pipe chains**:

```bash
cat access.log | grep "404" | cut -d' ' -f1 | sort | uniq -c | sort -rn
# Shows IPs with most 404 errors
```

### Tee (Read and Write)

Write to file while also displaying output:

```bash
ls | tee output.txt        # Save output and print it
command | tee -a log.txt   # Append to file
ps aux | tee processes.txt | grep python    # Tee in the middle
```

### Xargs (Build Commands)

Convert input to command arguments:

```bash
find . -name "*.txt" | xargs rm        # Delete all .txt files
echo file1 file2 file3 | xargs mkdir   # Create multiple directories
cat filelist.txt | xargs -I {} cp {} backup/    # Copy with placeholder
ps aux | grep "firefox" | awk '{print $2}' | xargs kill            # Kill all firefox processes
```

---

## Text Processing

Text processing is core to Unix philosophy. Process and transform text efficiently:

### Viewing Files

```bash
cat myfile.txt             # Print entire file
cat file1.txt file2.txt    # Concatenate multiple files
cat > newfile.txt          # Create file (CTRL+D to end)
head -20 myfile.txt        # First 20 lines (default: 10)
tail -20 myfile.txt        # Last 20 lines
tail -f logfile.txt        # Follow log (real-time)
less myfile.txt            # Pager (q to quit, space to page)
more myfile.txt            # Older pager
```

### Searching with Grep

```bash
grep "pattern" file.txt    # Find pattern in file
grep -i "pattern" file.txt # Case-insensitive
grep -v "pattern" file.txt # Lines NOT matching
grep -n "pattern" file.txt # Show line numbers
grep -c "pattern" file.txt # Count matching lines
grep -r "pattern" /dir/    # Recursive search
grep -E "regex" file.txt   # Extended regex
grep "^start" file.txt     # Lines starting with "start"
grep "end$" file.txt       # Lines ending with "end"
```

**Examples**:

```bash
grep "error" /var/log/syslog           # Find errors in system log
grep -n "TODO" *.py                    # Find TODO comments in Python files
grep -r "password" /etc/ --include="*.conf"    # Find config with password
tail -f /var/log/apache2/error.log | grep "500"          # Monitor 500 errors
```

### Stream Editor (sed)

Edit streams of text:

```bash
sed 's/old/new/' file.txt  # Replace first match per line
sed 's/old/new/g' file.txt # Replace all matches (g = global)
sed 's/old/new/i' file.txt # Case-insensitive replacement
sed '5d' file.txt          # Delete line 5
sed '1,10d' file.txt       # Delete lines 1-10
sed -n '5p' file.txt       # Print only line 5
sed 's/^/  /' file.txt     # Add indentation to every line
sed -i 's/old/new/g' file.txt          # Edit file in-place
```

### AWK (Data Processing)

Process structured text:

```bash
awk '{print $1}' file.txt  # Print first field
awk '{print $1, $3}' file.txt          # Print fields 1 and 3
awk -F':' '{print $1}' /etc/passwd     # Use : as field separator
awk '{sum += $1} END {print sum}' nums.txt     # Sum numbers
awk 'NR > 2' file.txt      # Skip first 2 lines (NR = line number)
awk '$2 > 50' file.txt     # Filter rows where field 2 > 50
```

**Examples**:

```bash
ps aux | awk '{print $1, $2}' # Print user and PID
df -h | awk '{print $1, $5}'  # Print filesystem and usage percentage
cut -d: -f1 /etc/passwd | awk '{print length, $0}' | sort -n | tail -1
# Find longest username
```

### Sorting and Counting

```bash
sort file.txt              # Sort lines
sort -n file.txt           # Sort numerically
sort -rn file.txt          # Sort reverse numerical
sort -u file.txt           # Sort and remove duplicates
uniq file.txt              # Remove consecutive duplicate lines
uniq -c file.txt           # Count occurrences
sort | uniq -c             # Count all occurrences (sort first)
wc -l file.txt             # Count lines
wc -w file.txt             # Count words
wc -c file.txt             # Count bytes
```

### Cutting and Transforming

```bash
cut -d':' -f1 /etc/passwd  # Extract first field (: delimiter)
cut -d',' -f2-4 data.csv   # Extract fields 2-4 from CSV
cut -c1-10 file.txt        # Extract characters 1-10
tr 'a-z' 'A-Z' < file.txt  # Convert lowercase to uppercase
tr -d '[:space:]' < file.txt           # Remove whitespace
tr -s '[:space:]' < file.txt           # Collapse multiple spaces
```

### Comparing Files

```bash
diff file1.txt file2.txt   # Show differences
diff -u file1.txt file2.txt            # Unified diff (clearer)
diff -r dir1/ dir2/        # Recursive directory diff
cmp file1.txt file2.txt    # Compare bytes
comm -12 file1.txt file2.txt           # Common lines in both
```

**Example pipeline**:

```bash
cat /var/log/access.log | grep "404" | awk '{print $1}' | sort | uniq -c | sort -rn | head -10
# Top 10 IPs with 404 errors
```

---

## Exercises

### Exercise 1: Explore the File System

Navigate the Linux file system and understand key directories:

```bash
cd /
pwd
ls -la
cd /etc
ls -la | head -20
cat passwd | head -5
cat group | head -5
cd /var/log
ls -la
tail -f syslog
# CTRL+C to stop
cd ~
pwd
ls -la
```

**Questions to answer**:
- What's in your home directory?
- How many files are in `/etc`?
- What's the size of your home directory?

### Exercise 2: User and Group Management

Create users and groups, then set permissions:

```bash
sudo useradd -m testuser
sudo passwd testuser
# Enter a password
sudo groupadd developers
sudo usermod -aG developers testuser
groups testuser
id testuser
sudo userdel -r testuser
```

### Exercise 3: Find Files with Patterns

Use `find` and `grep` to locate specific files:

```bash
find /etc -name "*.conf" -type f
find /var/log -name "*.log" -mtime -7
grep -r "root" /etc --include="*.conf" | head -5
find /home -type f -size +100M 2>/dev/null
```

### Exercise 4: Monitor and Kill Processes

Start a long-running process and manage it:

```bash
# In one terminal, start a process
sleep 3600 &
sleep 3600 &
sleep 3600 &
jobs
ps aux | grep sleep
# Get the PID
kill PID
# Verify it's gone
ps aux | grep sleep
```

### Exercise 5: Chain Commands with Pipes

Combine multiple commands to solve a problem:

```bash
# Show all running processes, extract python processes, count them
ps aux | grep python | grep -v grep | wc -l

# Show disk usage of directories in /var, sorted
du -sh /var/* | sort -h

# Find most used commands in history
history | awk '{print $2}' | sort | uniq -c | sort -rn | head -10
```

### Exercise 6: Install and Remove Packages

Practice package management (adjust for your distribution):

**On Ubuntu/Debian**:

```bash
apt search htop
apt install htop
htop
# CTRL+C to exit
apt remove htop
```

**On CentOS/RHEL**:

```bash
dnf search htop
dnf install htop
htop
dnf remove htop
```

### Exercise 7: Create a Bash Alias

Make a useful alias for common commands:

```bash
alias ll='ls -lah'
alias myip='hostname -I'
alias servers='ps aux | grep -E "nginx|apache|mysql"'
alias diskspace='df -h | grep -E "^/dev"'

# Test your aliases
ll
myip
servers

# Make them permanent
echo "alias ll='ls -lah'" >> ~/.bashrc
echo "alias myip='hostname -I'" >> ~/.bashrc
source ~/.bashrc
```

### Exercise 8: I/O Redirection and Logging

Capture output and errors:

```bash
# Create a test script
cat > test_script.sh << 'EOF'
#!/bin/bash
echo "This is stdout"
echo "This is stderr" >&2
EOF

chmod +x test_script.sh

# Redirect output
./test_script.sh > output.txt 2> errors.txt
cat output.txt
cat errors.txt

# Combined redirection
./test_script.sh > all_output.txt 2>&1
cat all_output.txt
```

### Exercise 9: Set Up a Cron Job

Schedule a task to run periodically:

```bash
# Edit your crontab
crontab -e

# Add a line to run a script every day at 2 AM
0 2 * * * /home/user/backup.sh >> /var/log/backup.log 2>&1

# List your cron jobs
crontab -l

# Cron format: minute hour day month dayofweek
# 0 2 * * * = 2:00 AM every day
# 0 */4 * * * = Every 4 hours
# 0 0 1 * * = 1st of every month
# 0 9 * * 1-5 = 9 AM weekdays only
```

### Exercise 10: Analyze Disk Usage

Find what's consuming disk space:

```bash
# Summary of disk space
df -h
df -h /

# Disk usage by directory
du -sh /home
du -sh /var
du -sh /var/*

# Find largest files
find / -type f -size +100M 2>/dev/null | head -10

# Summary of home directory
du -sh ~
du -sh ~/* | sort -h | tail -10

# Show directory tree with sizes
tree -h -L 2 /var

# Disk usage per user
du -sh /home/*
```

---

## Next Steps

Now that you understand Linux fundamentals, explore these advanced topics:

- **Networking**: IP addressing, DNS, network configuration, firewalls
- **System Administration**: Package management, service management, backups, monitoring
- **Shell Scripting**: Automate tasks with bash scripts
- **System Security**: User permissions, SSH, firewall rules, audit logs
- **Container Technology**: Docker and Kubernetes (built on Linux concepts)
- **Cloud Deployment**: Deploy applications on AWS, Azure, or GCP

---

## Quick Reference Card

| Task | Command |
|---|---|
| **Navigation** | `cd /path`, `pwd`, `ls -la` |
| **File Ops** | `mkdir`, `touch`, `cp -r`, `mv`, `rm -rf` |
| **Permissions** | `chmod 755`, `chown user:group`, `ls -l` |
| **Users/Groups** | `useradd -m user`, `usermod -aG group user`, `passwd` |
| **Processes** | `ps aux`, `top`, `kill PID`, `systemctl start service` |
| **Packages** | `apt install`, `dnf install`, `pacman -S` |
| **Searching** | `grep -r`, `find /path -name`, `locate` |
| **Text** | `cat`, `grep`, `sed`, `awk`, `sort`, `uniq` |
| **I/O** | `>, >>`, `2>`, `|`, `tee`, `xargs` |
| **Logs** | `journalctl -u service -f`, `tail -f logfile` |

---

## Resources

- **GNU Coreutils Manual**: `info coreutils`
- **Bash Manual**: `man bash`
- **Linux Foundation**: https://www.linuxfoundation.org/
- **The Linux Documentation Project**: https://tldp.org/
- **ArchWiki**: Comprehensive documentation for any topic
- **Man Pages Online**: https://man7.org/
