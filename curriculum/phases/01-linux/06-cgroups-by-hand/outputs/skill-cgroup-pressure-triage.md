---
name: cgroup-pressure-triage
description: Decide whether a slow or dying container, pod or service is CPU-throttled, OOM-killed or out of PIDs by reading its cgroup v2 files, then map the fix to the Docker flag, systemd property or Kubernetes field that writes that file
version: 1.0.0
phase: linux
lesson: linux/cgroups-by-hand
tags: [linux, cgroups, docker, kubernetes, systemd, oom, throttling, debugging]
---

# cgroup pressure triage

Use this when a workload is slower than its CPU usage suggests, exits with code 137, logs `cannot fork` or `Resource temporarily unavailable`, or when a pod shows `OOMKilled` or `CrashLoopBackOff` and someone is about to raise a limit without evidence.

## 1. Find the group

| Where it runs | Command |
|---|---|
| a PID on a host | `cat /proc/PID/cgroup` (the `0::/path` line); `python3 cgtool.py explain PID` for the whole chain with the effective limits |
| a Docker container | `PID=$(docker inspect -f '{{.State.Pid}}' CID); cat /proc/$PID/cgroup` from the host; inside the container `/sys/fs/cgroup` already is the group |
| a Kubernetes container | `kubectl exec POD -c CTR -- cat /sys/fs/cgroup/cpu.max /sys/fs/cgroup/cpu.stat /sys/fs/cgroup/memory.max /sys/fs/cgroup/memory.events /sys/fs/cgroup/pids.events`; on the node, `crictl inspect -o go-template --template '{{.info.pid}}' CID` then `explain` |
| a systemd service | `systemctl show -p ControlGroup -p MainPID --value UNIT` |

Inside a container `/proc/self/cgroup` says `0::/` because of the cgroup namespace; read the files under `/sys/fs/cgroup` there, or read the real path from the host.

## 2. Read the counters next to the limits

```bash
G=/sys/fs/cgroup/<path>
cat $G/cpu.max $G/cpu.stat                                        # QUOTA PERIOD; nr_periods nr_throttled throttled_usec
cat $G/memory.max $G/memory.high $G/memory.swap.max $G/memory.current $G/memory.events
cat $G/pids.max $G/pids.current $G/pids.events
python3 cgtool.py stat <path>                                     # the same files with one verdict line per resource
```

If a file is missing, the controller is not enabled: `cat $(dirname $G)/cgroup.subtree_control` shows what the parent handed down.

## 3. Decide

| Evidence | Meaning | What it rules out |
|---|---|---|
| `nr_throttled / nr_periods` above 10%, `throttled_usec` growing | the group hits `cpu.max` in bursts; the cost is latency, not load | average CPU% as a signal (30% average can still be throttled every period) |
| `oom_kill` in `memory.events` moved, exit 137 | the kernel killed a process for exceeding `memory.max` | "the app crashed"; a 137 with `oom_kill` unchanged is a SIGKILL from outside (grace period, eviction, `kill -9`) |
| `max` or `high` in `memory.events` growing, `oom_kill` flat | the group lives at its ceiling and pays in reclaim; `memory.high` is slowing it | disk IO as the first suspect; check `memory.pressure` |
| `memory.swap.max` not 0 and `memory.swap.current` growing | the ceiling is soft: the group pages out instead of dying | "the limit is not working" |
| `max` in `pids.events` growing, `cannot fork`, `EAGAIN` | `pids.max` reached by processes or threads in the subtree | file descriptors and memory |
| none of the above | not a limit on this group; run `explain PID` for a tighter ancestor, then look at the application | the limit you were about to raise |

## 4. Measure before changing anything

```bash
python3 cgtool.py stat <path> --watch 5 --count 6                 # cpu% and throttled periods per interval, memory delta, new kills
```

Write down throttled% per interval and the memory trend. Memory that grows linearly until the kill is a leak: raising `memory.max` moves the kill later, it does not remove it.

## 5. Fix at the layer that writes the file

| File | Docker | systemd | Kubernetes |
|---|---|---|---|
| `cpu.max` | `--cpus N` | `CPUQuota=N%` | `resources.limits.cpu` (500m is `50000 100000`); dropping the CPU limit stops throttling while `requests` keep scheduling and `cpu.weight` |
| `cpu.weight` | `--cpu-shares` | `CPUWeight=` | `resources.requests.cpu` |
| `memory.max` | `--memory` | `MemoryMax=` | `resources.limits.memory` |
| `memory.swap.max` | `--memory-swap` (equal to `--memory` for no swap) | `MemorySwapMax=0` | swap is off on the node unless enabled |
| `pids.max` | `--pids-limit` | `TasksMax=` | kubelet `podPidsLimit`; no per-container field |

Run `explain PID` first: when the tightest limit sits on an ancestor (the pod group, a slice, `kubepods`), changing the container's own limit changes nothing.

## 6. Report

State the cgroup path, the limit values, the counters at the start and end of your sampling window, the verdict (throttled, OOM-killed, out of PIDs, or not a cgroup problem) with the file that proves it, the knob you changed and the measured result afterwards. Do not report a raised memory limit as a fix until `oom_kill` has stayed flat through a full load cycle.
