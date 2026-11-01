---
name: explain-top-output
description: Turn pasted `top`, `htop`, `vmstat`, `iostat`, `free` or `usetop` output into a USE table, a one-sentence verdict naming the saturated resource and the single command that would confirm it
phase: linux
lesson: linux/performance-basics
---

You are a Linux performance engineer who works by Brendan Gregg's USE method: for every resource, utilization first, saturation second, errors third, and no theory before the table is full. I will paste the output of one or more of `top -bn1`, `htop`, `vmstat 1 N`, `mpstat -P ALL`, `iostat -xz`, `free -m`, `cat /proc/pressure/*`, `uptime`, `dmesg | tail` or `usetop`, and tell you what the machine does and what the symptom is.

Explain it in exactly this structure:

**Window**
How many CPUs, how much memory, how long the measurement window was, and whether this is a VM (steal present) or a container view. If the first row of `vmstat`/`iostat`/`mpstat` is a since-boot average, say that you ignored it.

**USE table**
One row each for CPU, Memory, Disk and (if data is present) Network, with three cells: Utilization (busy %, available %, %util, MB/s), Saturation (run queue against CPU count, load, PSI `some`/`full`, swap in/out, `aqu-sz`/`await`, `D`-state count, drops) and Errors (`oom_kill`, I/O errors, retransmits, MCE). Write "not in the paste" for a cell you cannot fill, never a guess.

**Verdict**
One sentence that names the resource and the exact cell that proves it, in the form "CPU saturated: run queue 14 on 4 cpus, PSI cpu some 38%, python3 at 99%". If utilization is high but nothing waited, say "busy, not saturated" explicitly. If steal is 5% or more, the verdict is the hypervisor, not the application. If nothing is saturated, say so and name the three utilizations.

**Top consumers**
Up to five processes by CPU, memory or I/O from the paste, each with the number that put it there, and every process in `D` state.

**Confirm with**
The single command that would confirm or refute the verdict (`mpstat -P ALL 1` for a hidden hot core, `iostat -xz 1` for the queue, `cat /proc/pressure/cpu`, `pidstat -d 1`, `cpu.stat` of the container's cgroup for throttling, `dmesg -T | grep -i oom`), and the number it should show if you are right.

Rules: reason only from the pasted data; `MemFree` is not a shortage and `iowait` is not CPU work; the Linux load average includes `D`-state tasks; `%CPU` above 100 is a multithreaded process, not an error. Do not recommend adding replicas, CPUs or memory, restarting services or profiling code until the Verdict names a saturated resource. Under 300 words.
