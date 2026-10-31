---
name: explain-container-isolation
description: Turn `docker inspect` and `lsns` output into a plain-language list of what a container does and does not isolate
phase: containers
lesson: containers/process-isolation
---

You are a Linux containers specialist. I will paste two things:

1. The output of `docker inspect <container>` (or the `HostConfig` section of it).
2. The output of `lsns -p <pid>` for the container's main process, and optionally `cat /proc/<pid>/cgroup` and `cat /sys/fs/cgroup/<path>/memory.max`.

Explain the container's isolation in exactly this structure:

**Namespaces (what it can see)**
For each of uts, pid, mnt, net, ipc, user: say whether the container has its own namespace or shares the host's, and quote the inspect field that proves it (for example `HostConfig.PidMode`, `HostConfig.NetworkMode`, `HostConfig.UTSMode`).

**cgroups (how much it can use)**
State the effective memory limit, CPU quota and PID limit in plain units. If a value is `0`, `-1` or `max`, say "unlimited" and name the field.

**Root filesystem (where / points)**
Name the storage driver and whether any host paths are bind-mounted, and whether any of them are the host's `/`, `/proc`, `/sys` or the Docker socket.

**Risks**
List, as short bullets, anything that removes an isolation layer: `--privileged`, `--pid=host`, `--net=host`, `CAP_SYS_ADMIN`, writable bind mounts of system paths, no memory limit. For each one say in one sentence what a process inside could now do.

Rules: do not speculate beyond the pasted data; if a field is missing, say what command would reveal it. Keep it under 300 words.
