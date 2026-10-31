# Linux & Systems

*Processes, files, cgroups, systemd and the kernel interfaces every ops tool is built on.*

Containers, Kubernetes, Terraform providers and observability agents all bottom out in the same Linux primitives: processes and signals, file descriptors and mounts, namespaces and cgroups, sockets and the network stack. Engineers who know them read error messages instead of searching for them. This phase builds each primitive into a small tool first, then shows where it appears in the stack you run.

## How this phase is built

| Lesson | Type | What you build | What you then use |
|---|---|---|---|
| PID 1 & Signals | Build | a supervisor that reaps children, forwards signals and restarts with backoff | `docker stop` timing, `--init`/tini, `exec` form, `terminationGracePeriodSeconds`, systemd `Restart=` |
| File Descriptors | Build | `fdwatch`: a process's descriptor table from `/proc`, classified, measured against the limit, with leak rate and deleted-but-open files | `lsof`, `prlimit`, `ss`, `ulimit -n`, `LimitNOFILE=`, `docker --ulimit` |
| Syscalls & strace | Build | `tracelens`: a parser for `strace` output that groups failures by errno, reconstructs search paths, finds slow calls and prints a verdict | `strace -f -tt -T -y -e trace=file,network`, `strace -c`, `--cap-add SYS_PTRACE`, `kubectl debug` |
| Permissions & Caps | Build | `capcheck`: a capability-mask decoder with the full 41-bit table, a permission evaluator with setuid/setgid/sticky rules, `/proc/PID/status` explained | `capsh --decode`, `getpcaps`, `setcap`, `docker run --cap-drop ALL`, Kubernetes `securityContext` |
| systemd Units | Build | `miniunit`: a unit-file parser, a dependency graph with `Requires`/`Wants` semantics and a service state machine with restart policies | `systemctl`, `journalctl -u`, `systemd-analyze verify`, `systemd-run`, drop-ins, Quadlet |
| cgroups v2 | Build | `cgtool`: create groups, write `cpu.max` / `memory.max` / `pids.max`, run processes inside them, read `cpu.stat` and `memory.events`, explain any PID's chain | `systemd-cgtop`, `systemd-run -p`, `docker run --cpus --memory --pids-limit`, Kubernetes requests, limits and QoS classes |
| Mounts & Overlay | Build | `mountlens`: a `mountinfo` parser with a mount tree, path lookup, propagation, overlay layers, shadowed mounts and inode usage | `findmnt`, `mount --bind`, `unshare -m`, Docker volumes / `--tmpfs` / `--read-only`, Kubernetes volumes |
| Performance Basics | Build | `usetop`: the USE method over `/proc/stat`, `meminfo`, `loadavg`, `diskstats` and PSI, with verdicts and top processes | `top`, `vmstat`, `mpstat`, `iostat`, `pidstat`, `free`, PSI, `kubectl top` |

Further lessons for this phase are proposed in [ROADMAP.md](https://github.com/nomadicmehul/CloudCaptain/blob/main/ROADMAP.md).

## Reference layer

The long-form guides, cheat sheet and interview questions live under [Linux](https://cloudcaptain.io/docs/tools/linux/) and [Bash](https://cloudcaptain.io/docs/tools/bash/).
