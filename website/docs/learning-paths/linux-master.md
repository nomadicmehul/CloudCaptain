---
sidebar_position: 9
title: "Linux Systems Master Path"
description: "Deep-dive roadmap to master Linux systems — from fundamentals to kernel internals"
---

# Linux Systems Master Path

A dedicated, deep-dive path for mastering Linux — the operating system that powers the cloud, AI, and the modern internet. This isn't a surface-level overview. This is the path to becoming the person everyone calls when systems break.

:::tip Why Master Linux?
Every Docker container is a Linux process. Every Kubernetes node runs Linux. Every AWS EC2 instance runs Linux. Every GPU training cluster runs Linux. **Mastering Linux is the single highest-ROI investment you can make as a systems engineer.**
:::

---

## Stage 1: Linux Fundamentals (Weeks 1-4)

**Goal:** Navigate the system confidently, manage files, users, and processes.

### Topics
- **File system hierarchy** — `/etc`, `/var`, `/proc`, `/sys`, `/dev`, `/tmp`
- **Essential commands** — `ls`, `cd`, `cp`, `mv`, `rm`, `find`, `grep`, `awk`, `sed`, `xargs`
- **File permissions** — `chmod`, `chown`, `umask`, SUID/SGID, sticky bit
- **Process management** — `ps`, `top`, `htop`, `kill`, `nice`, `nohup`, `&`, `jobs`
- **User & group management** — `useradd`, `usermod`, `passwd`, `/etc/passwd`, `/etc/shadow`
- **Package management** — `apt`, `yum`/`dnf`, `pacman`, `snap`, `flatpak`
- **Text processing** — `cat`, `head`, `tail`, `sort`, `uniq`, `cut`, `tr`, `wc`
- **I/O redirection** — `>`, `>>`, `<`, `|`, `2>`, `&>`, `tee`

### Hands-On Projects
1. Set up an Ubuntu/Fedora VM from scratch (no GUI)
2. Create multiple users with different permission levels
3. Write a script that automates user creation from a CSV file
4. Build a file organizer that sorts files by extension

### Resources
- [Linux documentation](/docs/tools/linux/)
- [Bash scripting](/docs/tools/bash/)

---

## Stage 2: Shell Scripting & Automation (Weeks 5-8)

**Goal:** Automate anything with Bash. Write production-quality scripts.

### Topics
- **Bash fundamentals** — Variables, loops, conditionals, functions
- **Advanced Bash** — Arrays, associative arrays, parameter expansion, here-docs
- **Error handling** — `set -euo pipefail`, trap, exit codes
- **Text processing power tools** — `awk` (full language), `sed` (advanced), `jq` for JSON
- **Cron & systemd timers** — Scheduling tasks
- **Script organization** — Logging, config files, argument parsing (`getopts`)
- **Expect & automation** — Automating interactive programs

### Hands-On Projects
1. Build a system health monitoring script (CPU, RAM, disk, network)
2. Create an automated backup system with rotation
3. Write a log analyzer that detects anomalies
4. Build a deployment script that pulls, builds, and restarts a service

---

## Stage 3: Networking Deep Dive (Weeks 9-12)

**Goal:** Understand networking at every layer. Debug any connectivity issue.

### Topics
- **TCP/IP stack** — How packets flow from application to wire
- **DNS** — Resolution process, `/etc/resolv.conf`, `dig`, `nslookup`, DNS caching
- **IP routing** — `ip route`, routing tables, static routes, policy routing
- **Firewalls** — `iptables`, `nftables`, `firewalld`, chains, NAT, MASQUERADE
- **Network debugging** — `tcpdump`, `wireshark`, `ss`, `netstat`, `traceroute`, `mtr`
- **SSH mastery** — Tunneling, port forwarding, jump hosts, config, keys
- **Load balancing** — HAProxy, Nginx, keepalived
- **VPNs & tunnels** — WireGuard, OpenVPN, IPsec

### Hands-On Projects
1. Set up a multi-VM network with routing between subnets
2. Configure iptables to build a stateful firewall
3. Set up SSH tunneling through a bastion host
4. Build a load-balanced web cluster with HAProxy

### Resources
- [Networking documentation](/docs/tools/networking/)

---

## Stage 4: System Administration (Weeks 13-16)

**Goal:** Manage production Linux servers. Handle storage, services, and monitoring.

### Topics
- **Systemd** — Units, services, targets, journalctl, timers, dependencies
- **Storage** — LVM, RAID, ZFS/Btrfs, disk quotas, `fstab`, iSCSI, NFS
- **Boot process** — BIOS/UEFI → GRUB → kernel → init → targets
- **Logging** — rsyslog, journald, log rotation, centralized logging
- **Monitoring** — `sar`, `vmstat`, `iostat`, `dstat`, Prometheus node_exporter
- **NFS & Samba** — Network file sharing
- **LDAP/FreeIPA** — Centralized authentication
- **Configuration management** — [Ansible](/docs/tools/ansible/) for Linux fleet management

### Hands-On Projects
1. Set up LVM with snapshots and demonstrate recovery
2. Configure systemd services with auto-restart and dependencies
3. Build a centralized logging pipeline (rsyslog → Elasticsearch)
4. Automate server provisioning with Ansible

---

## Stage 5: Security & Hardening (Weeks 17-20)

**Goal:** Secure Linux systems to production standards. Pass security audits.

### Topics
- **SELinux / AppArmor** — Mandatory access control, policies, troubleshooting
- **CIS Benchmarks** — Implementing the CIS hardening checklist
- **PKI & certificates** — OpenSSL, `certbot`, certificate chains, mutual TLS
- **PAM** — Pluggable Authentication Modules
- **Audit framework** — `auditd`, rules, log analysis
- **Kernel security** — `sysctl` hardening, seccomp, capabilities
- **Container security** — Namespaces, cgroups, rootless containers
- **Intrusion detection** — AIDE, OSSEC, Falco

### Hands-On Projects
1. Harden a server following CIS Level 2 benchmarks
2. Implement SELinux policies for a custom application
3. Set up certificate-based SSH authentication with a CA
4. Build an audit pipeline that alerts on suspicious activity

---

## Stage 6: Performance Tuning & Troubleshooting (Weeks 21-24)

**Goal:** Diagnose and fix any performance issue. Tune systems for maximum throughput.

### Topics
- **CPU** — Scheduling, NUMA, CPU pinning, `perf`, flamegraphs
- **Memory** — Virtual memory, hugepages, OOM killer, `free`, `/proc/meminfo`, NUMA
- **Disk I/O** — I/O schedulers, `iostat`, `fio` benchmarking, NVMe tuning
- **Network** — `ethtool`, TCP tuning (`sysctl`), interrupt affinity, ring buffers
- **Kernel parameters** — `/proc/sys/`, `sysctl.conf`, tuning for specific workloads
- **eBPF & tracing** — `bpftrace`, `bcc`, dynamic kernel tracing
- **Profiling** — `strace`, `ltrace`, `perf record`, CPU/memory profiling

### Hands-On Projects
1. Profile a slow application and optimize it 10x
2. Tune kernel parameters for a high-traffic web server
3. Use eBPF to trace system calls and identify bottlenecks
4. Benchmark and optimize disk I/O for a database workload

---

## Stage 7: Virtualization & Containers Deep Dive (Weeks 25-28)

**Goal:** Understand the Linux primitives that make containers and VMs work.

### Topics
- **Namespaces** — PID, network, mount, UTS, IPC, user — the foundation of containers
- **Cgroups v2** — Resource limiting, CPU shares, memory limits, I/O throttling
- **KVM/QEMU** — Linux virtualization, `virsh`, `virt-manager`, libvirt
- **Container runtimes** — containerd, runc, cri-o — what happens when you `docker run`
- **Rootless containers** — [Podman](/docs/tools/podman/), user namespaces
- **OverlayFS** — How container images work
- **seccomp & capabilities** — Fine-grained security for containers

### Hands-On Projects
1. Build a container from scratch using namespaces and cgroups (no Docker!)
2. Set up a KVM hypervisor with live migration
3. Implement cgroup resource limits and test OOM behavior
4. Create a custom seccomp profile for an application

---

## Stage 8: Linux for AI/ML Infrastructure (Weeks 29-32)

**Goal:** Prepare systems for AI workloads — GPUs, distributed training, high-performance networking.

### Topics
- **GPU management** — NVIDIA drivers, CUDA, `nvidia-smi`, GPU monitoring
- **NVIDIA Container Toolkit** — GPU passthrough to containers
- **High-performance networking** — RDMA, InfiniBand, RoCE, NCCL
- **Distributed storage** — Lustre, GPFS, Ceph for ML datasets
- **Kernel tuning for ML** — Huge pages, NUMA-aware scheduling, memory overcommit
- **Slurm / Ray** — Job scheduling for training clusters
- **Bare-metal provisioning** — MAAS, iPXE, Tinkerbell

### Hands-On Projects
1. Set up an NVIDIA GPU server with CUDA and container toolkit
2. Configure NUMA-aware scheduling for a multi-GPU workload
3. Build a bare-metal provisioning pipeline
4. Deploy a multi-node training cluster with Slurm

---

## Certifications

| Certification | Level | Focus |
|:-------------|:------|:------|
| CompTIA Linux+ | Entry | Fundamentals |
| LFCS (Linux Foundation) | Intermediate | System administration |
| LFCE (Linux Foundation) | Advanced | Engineering |
| RHCSA (Red Hat) | Intermediate | Red Hat administration |
| RHCE (Red Hat) | Advanced | Red Hat engineering |
| LPIC-1, LPIC-2, LPIC-3 | Progressive | Comprehensive Linux |

---

## Essential Books

| Book | Author | Focus |
|:-----|:-------|:------|
| How Linux Works | Brian Ward | Internals |
| The Linux Command Line | William Shotts | CLI mastery |
| UNIX and Linux System Administration Handbook | Nemeth et al. | System admin |
| Linux Kernel Development | Robert Love | Kernel |
| Systems Performance | Brendan Gregg | Performance |
| BPF Performance Tools | Brendan Gregg | eBPF/tracing |

Check our [Linux Books & PDFs](/docs/tools/linux/) for the full collection.

---

:::info The CloudCaptain Philosophy: Learn by Doing
Reading documentation is step 1. The real learning happens when you:
1. **Build** — Set up real systems, not just tutorials
2. **Break** — Intentionally crash things and learn to fix them
3. **Automate** — If you did it twice, script it
4. **Teach** — Explain what you learned to someone else
5. **Contribute** — Share your knowledge back with the community

That's how you go from "I know Linux" to "I *master* Linux."
:::
