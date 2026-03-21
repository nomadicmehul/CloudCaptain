---
title: "Linux"
sidebar_label: "Linux Overview"
description: "Comprehensive Linux learning resources — fundamentals, administration, networking, security, commands, and interview prep"
sidebar_position: 0
---

# Linux

Master **Linux** — the foundation of cloud infrastructure, DevOps, and modern computing. Over 96% of the world's top servers run Linux.

## Documentation

| Guide | Description |
|:------|:------------|
| [Linux Fundamentals](/docs/tools/linux/fundamentals) | File system, shell, permissions, processes, packages, text processing |
| [System Administration](/docs/tools/linux/administration) | Disk management, services, cron, logging, performance, SSH, backups |
| [Networking](/docs/tools/linux/networking) | Interfaces, routing, DNS, firewalls, SSH tunneling, network services |
| [Security](/docs/tools/linux/security) | Firewall, SELinux/AppArmor, SSH hardening, audit, kernel hardening |
| [Command Cheat Sheet](/docs/tools/linux/cheatsheet) | 200+ commands organized by category — files, processes, networking, text |
| [Interview Questions](/docs/tools/linux/interview-questions) | 50+ questions from beginner to advanced with detailed answers |

## Why Linux?

Linux is the operating system of choice for cloud, DevOps, and infrastructure because of its stability, security, flexibility, and open-source nature. If you're working in tech, Linux skills are essential.

## Learning Path

1. [Start with fundamentals](/docs/tools/linux/fundamentals) — file system, shell, permissions, processes
2. [Learn system administration](/docs/tools/linux/administration) — services, disk management, logging, SSH
3. [Master networking](/docs/tools/linux/networking) — interfaces, routing, DNS, firewalls
4. [Harden security](/docs/tools/linux/security) — firewall, SELinux, SSH hardening, auditing
5. [Keep the cheat sheet handy](/docs/tools/linux/cheatsheet) — 200+ commands at your fingertips

## Quick Start

```bash
# System info
uname -a
hostnamectl

# Navigate and explore
ls -la /
cd /var/log && ls -lh

# User and permissions
whoami
id
sudo useradd -m newuser

# Processes
ps aux | head -20
top -bn1 | head -15

# Networking
ip addr show
ss -tulnp

# Package management (Ubuntu/Debian)
sudo apt update && sudo apt upgrade -y
```

## External Resources

| Resource | Description |
|:---------|:------------|
| [Linux Journey](https://linuxjourney.com) | Interactive guides with exercises and quizzes |
| [Linux Survival](https://linuxsurvival.com/linux-tutorial-introduction) | Interactive browser-based tutorial |
| [The Linux Command Line](http://linuxcommand.org/tlcl.php) | Free book by William Shotts |
| [Linux From Scratch](http://www.linuxfromscratch.org) | Build your own Linux from source |
| [NixCraft](https://www.cyberciti.biz/) | Practical system administration guides |
| [learnlinux.tv](https://www.learnlinux.tv) | Practical video tutorials |
| [Linux SysOps Handbook](https://abarrak.gitbook.io/linux-sysops-handbook) | System admin study notes |
| [OSTEP](http://pages.cs.wisc.edu/~remzi/OSTEP) | Operating Systems: Three Easy Pieces |

## Video Tutorials

| Resource | Description |
|:---------|:------------|
| [The Complete Linux Course](https://www.youtube.com/watch?v=wBp0Rb-ZJak) | Beginner to power user (7+ hours) |
| [Linux for Hackers](https://www.youtube.com/watch?v=VbEx7B_PTOE) | NetworkChuck Linux series |
| [Tecmint Linux Guide](https://www.tecmint.com/free-online-linux-learning-guide-for-beginners) | Written articles and lessons |

## Books

| Title | Author | Type |
|:------|:-------|:-----|
| [The Linux Command Line](http://linuxcommand.org/tlcl.php) | William Shotts | Free |
| [Linux Bible](https://www.wiley.com/en-us/Linux+Bible%2C+9th+Edition-p-9781118999875) | Christopher Negus | Paid |
| [How Linux Works](https://nostarch.com/howlinuxworks3) | Brian Ward | Paid |
