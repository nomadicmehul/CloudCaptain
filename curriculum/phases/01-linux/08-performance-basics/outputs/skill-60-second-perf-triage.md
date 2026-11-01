---
name: 60-second-perf-triage
description: Find the saturated resource on a Linux host in about a minute with the USE method and Brendan Gregg's 60-second checklist, before anyone scales, restarts or profiles
version: 1.0.0
phase: linux
lesson: linux/performance-basics
tags: [linux, performance, use-method, psi, vmstat, iostat, mpstat, top, docker, kubernetes, debugging]
---

# 60-second performance triage (USE method)

Use this when a host or node is "slow", when latency rose without a deploy, when someone proposes adding CPUs, replicas or memory, or when a dashboard shows a single high number. The method is Brendan Gregg's [USE method](https://www.brendangregg.com/usemethod.html): for every resource, check Utilization, Saturation and Errors before forming a theory. The commands are his [60-second checklist](https://www.brendangregg.com/Articles/Netflix_Linux_Perf_Analysis_60s.pdf) ([Netflix tech blog version](https://netflixtechblog.com/linux-performance-analysis-in-60-000-milliseconds-accc10403c55)), plus PSI, which did not exist when the list was written.

## 0. Frame it (10 seconds)

- What is the symptom in user terms (latency, errors, throughput) and since when? Does it match a deploy, a cron, a traffic peak?
- Where are you: bare metal, VM, container, pod? A VM can have steal; a container sees the host's `/proc` but is limited by its own cgroup.
- Run the checklist on the host or node, not inside the application container, unless the question is about one container.

## 1. The checklist, in order

Run every command even when the first one looks guilty; the point of the method is to finish the table.

| Command | Read | Resource / USE cell | Looks bad when |
|---|---|---|---|
| `uptime` | load 1/5/15 vs `nproc` | CPU saturation (Linux load also counts `D` tasks) | 1-min load above the CPU count, or rising across 1/5/15 |
| `dmesg -T \| tail` | kernel messages | Errors for everything | `Out of memory`, `I/O error`, `MCE`, `link down`, `blocked for more than 120 seconds` |
| `vmstat 1 5` | `r` `b`, `si` `so`, `us sy id wa st` | CPU utilization and saturation, memory saturation | `r` above CPUs, any `si`/`so`, `st` above 5, `wa` high with `b` above 0 |
| `mpstat -P ALL 1 3` | per-CPU `%usr %sys %iowait %steal %idle` | CPU utilization per core | one core near 100% while the average is low (single-threaded or IRQ pinned) |
| `pidstat 1 5` | `%usr %system %CPU` per process | who uses the CPU | one PID near 100% per thread; `pidstat -d 1` for who does the I/O |
| `iostat -xz 1 3` | `r/s w/s rkB/s wkB/s r_await w_await aqu-sz %util` | disk utilization and saturation | `%util` near 100 with `aqu-sz` above 1 and `await` above the device's normal (ms for disks, sub-ms for NVMe) |
| `free -m` | `available` | memory utilization | `available` below 10% of total; ignore `free` |
| `sar -n DEV 1 3` | `rxkB/s txkB/s` per interface | network utilization | near the link speed; `rxdrop/s` in `sar -n EDEV` for saturation |
| `sar -n TCP,ETCP 1 3` | `active/s passive/s retrans/s` | network errors and saturation | `retrans/s` above 0 steadily |
| `top -bn1 \| head -20` | `%Cpu(s)` line, `Tasks: N running`, per-process `%CPU` and `S` | a second look at everything | `st` in the header; many `D` in the `S` column |
| `cat /proc/pressure/cpu /proc/pressure/memory /proc/pressure/io` | `some avg10`, `full avg10` | saturation measured as time | cpu `some` above 10, memory or io `full` above 5 |

If `usetop` from the lesson is on the box: `python3 usetop.py --interval 2 --count 2` prints the whole table and a verdict in one go.

## 2. Fill the table before you decide

```
Resource  Utilization                 Saturation                          Errors
CPU       busy %, per-core max, st    r vs nproc, load, PSI cpu some      dmesg MCE
Memory    available % of total        si/so, major faults, PSI mem full   oom_kill (dmesg, /proc/vmstat)
Disk      %util, IOPS, MB/s           aqu-sz, await, D-state count,       dmesg I/O errors, smartctl
                                      PSI io full
Network   rx/tx vs link speed         drops, retrans/s, socket backlog    errors in sar -n EDEV, ethtool -S
```

Write every cell, including "fine". An empty cell is where the next wrong fix comes from.

## 3. Decide

| Pattern | Verdict | Next tool |
|---|---|---|
| CPU busy 80%+, `r` at or below CPUs, PSI cpu some below 10 | CPU is well used, not saturated; latency is elsewhere | finish the table, then look at the app (locks, downstream calls) |
| `r` above CPUs or PSI cpu some 10%+, one process on top | CPU saturated by that process | `perf top -p PID`, `pidstat -t` for threads; fewer or lighter requests, more cores or replicas on other nodes |
| `r` above CPUs, no single process on top | many small consumers or interrupts | `mpstat -P ALL` for IRQ/softirq skew, `top -H`, `/proc/interrupts` |
| `st` 5%+ in a VM | hypervisor steal | resize or move the instance, check burst credits, talk to the provider; do not profile the app yet |
| `available` below 10%, `si`/`so` above 0, PSI memory full above 5 | memory saturated, kernel swapping or thrashing | `smem`/`ps --sort=-rss`, `pidstat -r`; stop the growth, then size |
| `oom_kill` rose, `available` high | a cgroup limit ran out, not the host | `memory.events` of the container or pod (cgroups lesson), raise or fix the limit |
| `%util` near 100, `aqu-sz` above 1, `await` high, `D` tasks | disk saturated | `pidstat -d`, `iotop`, `biotop`; fewer or larger I/Os, cache, faster device |
| `wa` high but `iostat` calm | waiting on a remote filesystem or a slow device not in `iostat` | `nfsiostat`, `mount`, `dmesg` |
| load high, CPU idle, no disk queue | tasks blocked on something else (locks, NFS, a hung device) | `ps -eo pid,stat,wchan:32,comm \| awk '$2 ~ /^D/'` |
| container slow, node calm | throttled against its own `cpu.max` or at `memory.max` | `cpu.stat` `nr_throttled`, `memory.events`, `docker stats`, `kubectl top pod --containers` |
| everything calm, latency still high | not this host | downstream dependencies, DNS, network path, the client |

## 4. Containers and Kubernetes

```bash
docker stats --no-stream                                  # cgroup cpu.stat / memory.current over host capacity
docker inspect -f '{{.HostConfig.NanoCpus}} {{.HostConfig.Memory}}' CID    # the caps that throttle or kill
cat /sys/fs/cgroup/<container group>/cpu.stat             # nr_throttled, throttled_usec: saturation against the cap
cat /sys/fs/cgroup/<container group>/cpu.pressure         # PSI for one container
kubectl top node; kubectl top pod -A --sort-by=cpu --containers
kubectl describe node NODE | sed -n '/Allocated resources/,/Events/p'      # requests vs allocatable: scheduling, not saturation
kubectl debug node/NODE -it --image=busybox -- sh -c 'cat /host/proc/loadavg /host/proc/pressure/cpu; head -3 /host/proc/meminfo'
```

Steal is only visible from inside the VM (`st`), never from `docker stats` or `kubectl top`. A pod can be throttled with the node at 20%; `kubectl top` showing 40% of the limit does not rule it out, `cpu.stat` does.

## 5. Traps

- `MemFree` small is normal; `MemAvailable` small is the problem.
- Averages hide a hot core and a hot second; use `mpstat -P ALL` and 1-second intervals.
- The first row of `vmstat`, `iostat` and `mpstat` is the since-boot average; skip it.
- Linux load includes `D`-state tasks; load 30 on 4 CPUs with 5% CPU busy is a disk or NFS problem.
- `iowait` is idle time with I/O pending, not CPU work; the disk's `await` and queue are the measurement.
- `%util` pins at 100 on NVMe and RAID that serve many requests in parallel; `aqu-sz` and `await` tell you whether it matters.
- A container sees the host's `/proc/stat` and `/proc/meminfo`; its own limits live in its cgroup files.
- `top` `%CPU` can exceed 100 for a multithreaded process; `pidstat -t` shows the threads.

## 6. Report

State: the symptom and window; the full USE table (every resource, every cell); the verdict as one sentence naming the resource and the cell that proves it ("CPU saturated: run queue 14 on 4 cpus, PSI cpu some 38%, process X at 99%"); what was ruled out and by which number; the next command or change, and what number it should move. Do not recommend scaling, restarting or profiling before the table is full.
