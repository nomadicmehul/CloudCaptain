---
name: container-isolation-triage
description: Diagnose "it works on the host but not in the container" by checking each isolation layer in order: namespaces, cgroups, root filesystem
version: 1.0.0
phase: containers
lesson: containers/process-isolation
tags: [docker, linux, namespaces, cgroups, debugging]
---

# Container isolation triage

Use this when a process behaves differently inside a container than on the host: cannot see a process, a file or a network interface, gets killed, or reports the wrong hostname. Work top to bottom and stop at the first layer that explains the symptom.

## 0. Find the process

```bash
CID=<container name or id>
PID=$(docker inspect -f '{{.State.Pid}}' "$CID")
echo "$PID"
```

If `PID` is `0` the container is not running; read `docker logs "$CID"` and `docker inspect -f '{{.State.ExitCode}} {{.State.OOMKilled}}' "$CID"` first. `OOMKilled: true` sends you straight to step 2.

## 1. Namespaces: what can it see?

```bash
sudo lsns -p "$PID"
sudo lsns -p 1 | grep -E 'uts|pid|mnt|net'
```

Compare the namespace ids with PID 1's. A matching id means the container shares that namespace with the host (`--pid=host`, `--net=host`, `--uts=host`). Then look from the inside:

```bash
sudo nsenter -t "$PID" -u hostname
sudo nsenter -t "$PID" -p -m ps -eo pid,comm
sudo nsenter -t "$PID" -n ip -brief addr
```

Symptom to layer: cannot see a host process → pid; wrong hostname in logs → uts; a port that is open on the host is unreachable → net; a mount that exists on the host is missing → mnt.

## 2. cgroups: how much can it use?

```bash
CG=$(cat /proc/"$PID"/cgroup | cut -d: -f3)
cat /sys/fs/cgroup"$CG"/memory.max /sys/fs/cgroup"$CG"/memory.current
cat /sys/fs/cgroup"$CG"/memory.events
cat /sys/fs/cgroup"$CG"/cpu.max /sys/fs/cgroup"$CG"/pids.max
```

`oom_kill` greater than 0 in `memory.events` means the limit, not the application, ended the process. `cpu.max` of `50000 100000` is half a core; throttling shows up in `cpu.stat` as `nr_throttled`. `pids.max` explains "fork: retry: Resource temporarily unavailable".

## 3. Root filesystem: where does / point?

```bash
docker inspect -f '{{.GraphDriver.Name}} {{json .Mounts}}' "$CID"
sudo nsenter -t "$PID" -m cat /proc/mounts | head
sudo nsenter -t "$PID" -m ls -la /
```

A file "missing" inside is usually on the host path, not the image. A file that should not be visible usually means a bind mount of `/`, `/proc`, `/sys` or `/var/run/docker.sock`. Confirm the image layer with `docker history <image>`.

## 4. Report

Write the finding as: **layer** → **evidence (command + value)** → **fix**. Example: `cgroups → memory.events oom_kill 3, memory.max 104857600 → raise --memory or fix the leak`. Never recommend `--privileged`, `--pid=host` or `--net=host` as a fix; they remove the layer instead of configuring it.
