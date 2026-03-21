---
title: "Linux"
description: "Linux system administration"
---

# Linux

Master Linux — the foundation of cloud infrastructure and DevOps.

## Learning Resources

### Tutorials and Guides

| Resource | Description |
|:---------|:------------|
| [Linux Journey](https://linuxjourney.com) | Written guides + exercises + quiz |
| [Techmint Linux](https://www.tecmint.com/free-online-linux-learning-guide-for-beginners) | Written articles and lessons |
| [Linux Survival](https://linuxsurvival.com/linux-tutorial-introduction) | Interactive guide |
| [NixCraft](https://www.cyberciti.biz/) | Succinct guides for system administration |
| [Linux Filesystem Explained](https://www.linux.com/training-tutorials/linux-filesystem-explained) | NSIA Linux filesystem guide |
| [Linux SysOps Handbook](https://abarrak.gitbook.io/linux-sysops-handbook) | Study notes for Linux system admin |
| [LinuxToday](https://www.linuxtoday.com) | Contributor-driven news resources |

### Linux Internals

| Resource | Description |
|:---------|:------------|
| [How are Unix pipes implemented?](https://toroid.org/unix-pipe-implementation) | Deep dive into pipe implementation |
| [Understanding fork() system call](https://www.youtube.com/watch?v=PwxTbksJ2fo) | Video on process creation |

### Video Tutorials

| Resource | Description |
|:---------|:------------|
| [learnlinux.tv](https://www.learnlinux.tv) | Very practical videos on various topics |
| [The Complete Linux Course](https://www.youtube.com/watch?v=wBp0Rb-ZJak&t=6578s) | Beginner to Power User (07:23:52) |

### Books

| Author | Title | Type |
|:-------|:------|:-----|
| William Shotts | [The Linux Command Line](http://linuxcommand.org/tlcl.php) | Free |
| Christopher Negus | [Linux Bible](https://www.wiley.com/en-us/Linux+Bible%2C+9th+Edition-p-9781118999875) | Paid |

### Community Sites

| Site | Description |
|:-----|:------------|
| [Linux From Scratch](http://www.linuxfromscratch.org) | Build Linux from source |
| [Operating Systems: Three Easy Pieces](http://pages.cs.wisc.edu/~remzi/OSTEP) | OS fundamentals |

### Popular Topics

| Topic | Link |
|:------|:-----|
| ps aux Command | [linode.com/docs/guides/use-the-ps-aux-command](https://www.linode.com/docs/guides/use-the-ps-aux-command-in-linux/) |
| Awk Tutorial | [thegeekstuff.com/awk-introduction-tutorial](https://www.thegeekstuff.com/2010/01/awk-introduction-tutorial-7-awk-print-examples/) |

### System Administration

| Presentation | Level | Topic |
|:-------------|:------|:------|
| [Linux Networking](https://www.slideshare.net/ArieBregman/linux-networking-113100224) | Beginner | Arie Bregman |
| [tcpdump Hunter](https://www.slideshare.net/j0b1n/tcpdump-hunter) | Beginner | Andrew McNico |

## Cheat Sheet

### File & Directory Operations

```bash
# Copy files to a remote host
rsync -azv dir USER@REMOTE_HOST_ADDR

# View file/directory size
du -sh DIR/FILE

# Sort files by size
ls -l | sort -nk5

# Find broken links
find /some/path -type l -exec test ! -e {} \; -print
```

### Process & System Monitoring

```bash
# List processes with CPU and memory usage
top

# Show system uptime
uptime

# List open files
lsof

# List open network connections
lsof -i

# Show connected users
w
```

### User Management

```bash
# View current user
whoami

# List all logged in users
w

# Change user
su username
```

### Terminal Multiplexing (Tmux)

```bash
# Join a session
tmux a

# Attach to existing session
tmux attach -t <SESSION_NAME>

# Create new session
tmux new -s session_name

# New window in session (Ctrl+B then C)
Ctrl+B C

# Switch between windows
Ctrl+B N  # next window
Ctrl+B P  # previous window
```

### System Information

```bash
# Get current year
date +"%Y"

# Get current month
date +"%m"

# Get day of week
date +"%a"

# Get system information
uname -a
```

### Virtualization (Virsh)

```bash
# Destroy VM
virsh destroy <vm_name>

# Shutdown VM gracefully
virsh shutdown <vm_name>
```

### SELinux

```bash
# Get SELinux denials
semodule -DB
sudo ausearch -m avc -m user_avc -m selinux_err -m user_selinux_err -i -ts today
```

### Communication

```bash
# Broadcast message to all users
wall "message here"
```

### LDAP

```bash
# LDAP search for a username
ldapsearch -x -W -D username
```

### Fedora-Specific

```bash
# Disable alert sound
dconf write /org/gnome/desktop/sound/event-sounds "false"

# Reset interface text font
gsettings reset org.gnome.desktop.interface font-name

# Reset monospace font
gsettings reset org.gnome.desktop.interface monospace-font-name

# Reset document text font
gsettings reset org.gnome.desktop.interface document-font-name

# Reset window title font
gsettings reset org.gnome.desktop.wm.preferences titlebar-font
```

## Contributing

Know great Linux resources? Submit a PR to help the community learn!

## Books & PDFs

| Book | Link |
|:-----|:-----|
| 50 Linux Commands | [View PDF](https://github.com/nomadicmehul/CloudCaptain/blob/main/Linux/Books/50_Linux_Commands.pdf) |
| All Kali Linux Commands | [View PDF](https://github.com/nomadicmehul/CloudCaptain/blob/main/Linux/Books/All%20Kali%20Linux%20Commands_080520070935.pdf) |
| Basic Linux Commands | [View PDF](https://github.com/nomadicmehul/CloudCaptain/blob/main/Linux/Books/Basic%20Linux%20Commands.pdf) |
| Birth of Linux | [View PDF](https://github.com/nomadicmehul/CloudCaptain/blob/main/Linux/Books/Birth%20Of%20Linux.pdf) |
| Introduction to Command Line Linux | [View PDF](https://github.com/nomadicmehul/CloudCaptain/blob/main/Linux/Books/Introduction%20to%20Command%20Line%20Linux.pdf) |
| Just Enough Linux | [View PDF](https://github.com/nomadicmehul/CloudCaptain/blob/main/Linux/Books/Just%20Enough%20Linux.pdf) |
| Linux 101 Hacks | [View PDF](https://github.com/nomadicmehul/CloudCaptain/blob/main/Linux/Books/Linux%20101%20Hacks.pdf) |
| Linux Admin | [View PDF](https://github.com/nomadicmehul/CloudCaptain/blob/main/Linux/Books/Linux%20Admin.pdf) |
| Linux Administration Quick Reference | [View PDF](https://github.com/nomadicmehul/CloudCaptain/blob/main/Linux/Books/Linux%20Administration%20Quick%20Reference.pdf) |
| Linux Basic | [View PDF](https://github.com/nomadicmehul/CloudCaptain/blob/main/Linux/Books/Linux%20Basic.pdf) |
| Linux Bootcamp | [View PDF](https://github.com/nomadicmehul/CloudCaptain/blob/main/Linux/Books/Linux%20Bootcamp.pdf) |
| Linux Fundamentals Bootcamp | [View PDF](https://github.com/nomadicmehul/CloudCaptain/blob/main/Linux/Books/Linux%20Fundamentals%20Bootcamp.pdf) |
| Linux Fundamentals | [View PDF](https://github.com/nomadicmehul/CloudCaptain/blob/main/Linux/Books/Linux%20Fundamentals.pdf) |
| Linux Networking 101 | [View PDF](https://github.com/nomadicmehul/CloudCaptain/blob/main/Linux/Books/Linux%20Networking%20101.pdf) |
| Linux Notes for Professionals | [View PDF](https://github.com/nomadicmehul/CloudCaptain/blob/main/Linux/Books/Linux%20Notes%20For%20Professionals.pdf) |
| Linux The Complete Reference (6th Edition) | [View PDF](https://github.com/nomadicmehul/CloudCaptain/blob/main/Linux/Books/Linux%20The%20Complete%20Reference.6th.Edition%28Nov.2007%29%5B2842313%5D.PDF) |
| Linux for Beginners | [View PDF](https://github.com/nomadicmehul/CloudCaptain/blob/main/Linux/Books/Linux%20for%20Beginners.pdf) |
| The Linux Commands Handbook | [View PDF](https://github.com/nomadicmehul/CloudCaptain/blob/main/Linux/Books/The%20Linux%20Commands%20Handbook.pdf) |
