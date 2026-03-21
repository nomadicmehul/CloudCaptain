---
title: "Bash Cheatsheet"
description: "100+ essential Bash commands, snippets, and one-liners for DevOps"
sidebar_label: "Cheatsheet"
sidebar_position: 3
---

# Bash Cheatsheet

A comprehensive quick-reference guide with 100+ essential commands and practical one-liners for daily use.

## File Operations

### Basic File Commands

```bash
ls -la                          # List all files with details
ls -lhS                         # Sort by size (human-readable)
ls -1 | head -20                # Show first 20 files
find . -name "*.txt"            # Find all .txt files
find . -type f -mtime -7        # Find files modified in last 7 days
find . -size +100M              # Find files larger than 100MB
```

### Copy and Move

```bash
cp file.txt file_backup.txt     # Copy file
cp -r dir1 dir2                 # Copy directory recursively
cp -v *.txt /dest/              # Copy with verbose output
mv old_name.txt new_name.txt    # Rename/move file
mv *.log /archive/              # Move all .log files
```

### Delete

```bash
rm file.txt                     # Delete file
rm -r directory/                # Delete directory recursively
rm -f *.tmp                     # Force delete without prompting
find . -name "*.bak" -delete    # Find and delete in one command
shred -vfz -n 3 secret.txt      # Secure delete (overwrite)
```

### File Information

```bash
stat file.txt                   # Detailed file information
file file.bin                   # Determine file type
du -sh directory/               # Directory size
du -sh *                        # Size of each item
wc -l file.txt                  # Count lines
wc -w file.txt                  # Count words
wc -c file.txt                  # Count bytes
```

### Create and View Files

```bash
touch newfile.txt               # Create empty file
mkdir -p a/b/c                  # Create nested directories
cat file.txt                    # Display file contents
less file.txt                   # View file (paginated)
head -20 file.txt               # Show first 20 lines
tail -10 file.txt               # Show last 10 lines
tail -f log.txt                 # Follow file (real-time)
```

## Text Processing

### grep (Search)

```bash
grep "pattern" file.txt         # Search for pattern
grep -i "pattern" file.txt      # Case-insensitive search
grep -c "pattern" file.txt      # Count matching lines
grep -n "pattern" file.txt      # Show line numbers
grep -v "pattern" file.txt      # Invert match (exclude)
grep -E "regex.*pattern" file   # Extended regex
grep -r "pattern" dir/          # Recursive search
grep "pattern1\|pattern2" file  # Multiple patterns
```

### sed (Stream Editor)

```bash
sed 's/old/new/' file.txt       # Replace first on each line
sed 's/old/new/g' file.txt      # Replace all on each line
sed '5d' file.txt               # Delete line 5
sed -n '10,20p' file.txt        # Print lines 10-20
sed -i 's/old/new/g' file.txt   # In-place edit
sed 's|/path|/newpath|g' file   # Use | as delimiter
sed 's/^/PREFIX: /' file.txt    # Add prefix to each line
sed 's/$/ :SUFFIX/' file.txt    # Add suffix to each line
```

### awk (Data Processing)

```bash
awk '{print $1}' file.txt       # Print first column
awk '{print $1, $3}' file.txt   # Print columns 1 and 3
awk '{sum+=$1} END {print sum}' # Sum column
awk -F: '{print $1}' /etc/passwd # Use : as separator
awk 'NR==5' file.txt            # Print line 5
awk 'NR>=5 && NR<=10' file.txt  # Print lines 5-10
awk '{print NF}' file.txt       # Print number of fields
awk '$2 > 100' file.txt         # Filter rows where col 2 > 100
```

### Other Text Tools

```bash
cut -d: -f1,3 file.txt          # Extract columns (delimiter :)
tr 'a-z' 'A-Z' < file.txt       # Convert to uppercase
tr -d '\r' < file.txt           # Remove carriage returns
sort file.txt                   # Sort lines
sort -u file.txt                # Sort and remove duplicates
sort -n file.txt                # Numeric sort
sort -rn file.txt               # Reverse numeric sort
uniq file.txt                   # Remove consecutive duplicates
uniq -c file.txt                # Count duplicates
uniq -d file.txt                # Show only duplicates
```

## String and Variable Operations

### Variable Expansion

```bash
${var}                          # Expand variable
${var:-default}                 # Use default if empty
${var:=default}                 # Set and use default
${var:?error}                   # Error if empty
${var%suffix}                   # Remove suffix
${var#prefix}                   # Remove prefix
${var/old/new}                  # Replace first
${var//old/new}                 # Replace all
${var:offset:length}            # Substring
${#var}                         # String length
${var^^}                        # Convert to uppercase
${var,,}                        # Convert to lowercase
```

### String Testing

```bash
[[ -z "$var" ]]                 # True if empty
[[ -n "$var" ]]                 # True if not empty
[[ "$var" == "text" ]]          # String equals
[[ "$var" =~ pattern ]]         # Regex match
[[ "$var" != "text" ]]          # Not equals
[[ $var -gt 10 ]]               # Greater than (numeric)
[[ $var -lt 10 ]]               # Less than (numeric)
[[ $var -eq 10 ]]               # Equal (numeric)
```

## File Testing

```bash
[[ -f file.txt ]]               # File exists
[[ -d directory ]]              # Directory exists
[[ -r file.txt ]]               # Readable
[[ -w file.txt ]]               # Writable
[[ -x file.txt ]]               # Executable
[[ -s file.txt ]]               # File exists and not empty
[[ -L link ]]                   # Symbolic link
[[ -e file.txt ]]               # File/dir exists
```

## Process Management

### Running and Monitoring Processes

```bash
ps aux                          # List all processes
ps aux | grep name              # Find process by name
ps -ef --forest                 # Tree view of processes
pgrep nginx                     # Get PID by process name
pidof nginx                     # Get PID of process
top                             # Real-time process monitor
htop                            # Better process monitor
jobs                            # List background jobs
bg                              # Resume job in background
fg                              # Resume job in foreground
```

### Kill Processes

```bash
kill PID                        # Send SIGTERM
kill -9 PID                     # Force kill (SIGKILL)
killall nginx                   # Kill all with name
pkill -f "python script"        # Kill by pattern
pkill -9 -f process             # Force kill by pattern
```

### Background Execution

```bash
command &                       # Run in background
nohup command &                 # Immune to hangup
nohup command > out.log 2>&1 &  # Redirect output and background
disown %1                       # Disown job 1
bg %1                           # Resume job 1 in background
fg %1                           # Bring job 1 to foreground
wait PID                        # Wait for process
```

## Permissions and Ownership

```bash
chmod 644 file.txt              # Set permissions (rw-r--r--)
chmod 755 script.sh             # Make executable (rwxr-xr-x)
chmod +x file                   # Add execute permission
chmod -R 755 directory/         # Recursive change
chmod u+rwx file                # User: add read, write, execute
chmod g-w file                  # Group: remove write
chmod o=r file                  # Others: read only
chown user file                 # Change owner
chown user:group file           # Change owner and group
chown -R user:group dir/        # Recursive change
umask 0022                      # Set default permissions
```

## Directory Navigation and Manipulation

```bash
pwd                             # Print working directory
cd directory                    # Change directory
cd -                            # Go to previous directory
cd ~                            # Go to home
cd ..                           # Go to parent
pushd dir                       # Save current and go to dir
popd                            # Return to saved directory
dirs                            # Show directory stack
mkdir -p a/b/c                  # Create nested directories
rmdir directory                 # Remove empty directory
```

## Networking

### Network Information

```bash
ifconfig                        # Network interface config
ip addr show                    # Show IP addresses
ip route show                   # Show routing table
netstat -tuln                   # Listen ports and connections
ss -tuln                        # Socket statistics
hostname                        # Show hostname
hostname -I                     # Show IP addresses
dig example.com                 # DNS lookup
nslookup example.com            # DNS resolution
ping example.com                # Test connectivity
traceroute example.com          # Show route to host
```

### Testing and Connectivity

```bash
curl https://example.com        # Fetch URL
curl -I https://example.com     # Fetch headers only
wget https://example.com/file   # Download file
nc -zv host port                # Test port connectivity
telnet host port                # Telnet to port
ssh user@host                   # SSH remote login
scp file user@host:~/           # Secure copy to remote
rsync -avz src/ dest/           # Sync files (local/remote)
```

## System Information

```bash
uname -a                        # System information
uname -r                        # Kernel version
cat /etc/os-release             # OS information
lsb_release -a                  # LSB release info
uptime                          # System uptime
date                            # Current date/time
cal                             # Calendar
df -h                           # Disk space
free -h                         # Memory usage
lsblk                           # Block device information
lscpu                           # CPU information
```

## Environment and Variables

```bash
env                             # List all variables
echo $PATH                      # Show PATH variable
export VAR=value                # Set environment variable
export PATH=$PATH:/new/path     # Append to PATH
unset VAR                       # Unset variable
printenv                        # Print environment
set                             # Show shell variables
declare -x VAR=value            # Export variable
source ~/.bashrc                # Reload bash config
```

## User and Group Management

```bash
whoami                          # Current user
id                              # User and group info
id -u                           # Current user ID (UID)
id -g                           # Current group ID (GID)
groups                          # List current groups
getent passwd                   # List all users
getent group                    # List all groups
useradd -m username             # Create user
usermod -G group user           # Add user to group
deluser username                # Delete user
passwd username                 # Change password
su - username                   # Switch user
sudo command                    # Run as superuser
```

## Date and Time

```bash
date                            # Current date/time
date +%Y-%m-%d                  # Format: YYYY-MM-DD
date +%T                        # Format: HH:MM:SS
date -d "2024-03-21"            # Parse date
date -d "tomorrow"              # Tomorrow's date
date -d "2 hours ago"           # Past calculation
timedatectl                     # Timezone and NTP status
tzselect                        # Change timezone
```

## Archive and Compression

```bash
tar -cvf archive.tar dir/       # Create tar archive
tar -xvf archive.tar            # Extract tar archive
tar -czvf archive.tar.gz dir/   # Create gzip archive
tar -xzvf archive.tar.gz        # Extract gzip archive
tar -cjvf archive.tar.bz2 dir/  # Create bzip2 archive
tar -xjvf archive.tar.bz2       # Extract bzip2 archive
zip -r archive.zip dir/         # Create zip archive
unzip archive.zip               # Extract zip archive
gzip file                       # Compress with gzip
gunzip file.gz                  # Decompress gzip
bzip2 file                      # Compress with bzip2
bunzip2 file.bz2                # Decompress bzip2
```

## Package Management

### APT (Debian/Ubuntu)

```bash
apt update                      # Update package list
apt install package             # Install package
apt remove package              # Remove package
apt autoremove                  # Remove unused packages
apt upgrade                     # Upgrade packages
apt full-upgrade                # Full system upgrade
apt search package              # Search packages
apt show package                # Show package info
apt-cache stats                 # Cache statistics
```

### YUM/DNF (RHEL/CentOS/Fedora)

```bash
yum update                      # Update packages
yum install package             # Install package
yum remove package              # Remove package
yum search package              # Search packages
yum info package                # Show package info
dnf install package             # Install (Fedora)
rpm -q package                  # Query installed package
```

## Useful One-Liners

### File Processing

```bash
# Count files
find . -type f | wc -l

# List files by size
du -sh * | sort -h

# Find empty files
find . -type f -size 0

# Find files modified today
find . -type f -mtime 0

# Compress all .log files
find . -name "*.log" -exec gzip {} \;

# Count lines in all .py files
find . -name "*.py" -exec wc -l {} + | tail -1
```

### System Monitoring

```bash
# Show top 5 memory consumers
ps aux --sort=-%mem | head -6

# Show top 5 CPU consumers
ps aux --sort=-%cpu | head -6

# Kill zombie processes
ps aux | grep defunct | awk '{print $2}' | xargs kill -9

# Monitor file changes
watch -n 1 'tail -20 /var/log/syslog'

# Show listening ports
netstat -tuln | grep LISTEN
```

### Text Processing

```bash
# Remove empty lines
grep -v '^$' file.txt

# Count occurrences
grep -o pattern file.txt | wc -l

# Extract unique values
cut -d: -f1 /etc/passwd | sort | uniq

# Show line count by word frequency
cat file.txt | tr ' ' '\n' | sort | uniq -c | sort -rn

# Replace in multiple files
find . -name "*.txt" -exec sed -i 's/old/new/g' {} \;
```

### Networking

```bash
# Check if port is open
timeout 1 bash -c '</dev/tcp/host/port' && echo "Open" || echo "Closed"

# Monitor network traffic
tcpdump -i eth0 'tcp port 80'

# List all established connections
netstat -an | grep ESTABLISHED

# Get external IP
curl -s https://ipinfo.io/ip

# Measure download speed
curl -o /dev/null -s -w '%{speed_download}\n' https://example.com/large-file
```

### Development

```bash
# Search in git history
git log -S "searchterm" --oneline

# Show git diff by word
git diff --word-diff

# Find large files in git
git rev-list --all --objects | sed -n '$(git rev-list --all --objects | cut -f1 | git cat-file --batch-check | grep -v missing | sort -k3 -n | tail -10 | cut -d" " -f1 | while read sha1; do git rev-list --all --objects | grep $sha1; done)p'

# Remove git history for a file
git filter-branch --tree-filter 'rm -f file.txt' HEAD

# Show commit log with changes
git log --stat

# Find who changed a line
git blame file.txt
```

## Advanced Snippets

### Process Monitoring Function

```bash
monitor_process() {
  local name=$1
  while true; do
    pgrep "$name" > /dev/null && \
      echo "$(date): $name is running" || \
      echo "$(date): $name STOPPED!"
    sleep 5
  done
}
monitor_process nginx
```

### Backup with Timestamp

```bash
backup_dir() {
  local dir=$1
  local timestamp=$(date +%Y%m%d_%H%M%S)
  tar -czf "${dir}_${timestamp}.tar.gz" "$dir"
}
backup_dir /home/user/data
```

### Simple HTTP Server

```bash
# Python 3
python3 -m http.server 8000

# Python 2
python -m SimpleHTTPServer 8000

# Node.js
npx http-server -p 8000
```

### Wait for Service

```bash
wait_for_service() {
  local host=$1 port=$2 timeout=${3:-30}
  timeout "$timeout" bash -c "until nc -z $host $port; do sleep 1; done"
}
wait_for_service localhost 5432 60
```

## Tips & Tricks

- **Redirect output**: Use `>` for stdout, `2>` for stderr, `&>` for both
- **Pipe output**: Chain commands with `|` to pass output to next command
- **Background jobs**: Add `&` at end to run in background, `nohup` to survive logout
- **Command history**: Use `Ctrl+R` to search history, `!!` for last command, `!$` for last argument
- **History expansion**: `^old^new` replaces in last command
- **Aliases**: Define shortcuts in `~/.bashrc`: `alias ll='ls -la'`
- **Functions**: Create reusable commands in shell scripts or `~/.bashrc`

## Quick Reference Table

| Task | Command |
|:-----|:--------|
| List files | `ls -la` |
| Search files | `grep -r "pattern"` |
| Find files | `find . -name "*.txt"` |
| Copy | `cp -r src dest` |
| Move | `mv old new` |
| Delete | `rm -r dir` |
| Permissions | `chmod 755 file` |
| Process list | `ps aux` |
| Kill process | `kill -9 PID` |
| Disk usage | `du -sh` |
| Network info | `ip addr show` |
| Archive | `tar -czf file.tar.gz dir` |
| Extract | `tar -xzf file.tar.gz` |

## See Also

- [Fundamentals](/docs/tools/bash/fundamentals) for core concepts
- [Advanced](/docs/tools/bash/advanced) for process substitution and signal handling
- [Interview Questions](/docs/tools/bash/interview-questions) for assessment preparation
