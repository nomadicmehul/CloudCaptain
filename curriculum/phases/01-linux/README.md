# Linux & Systems

*Processes, files, cgroups, systemd and the kernel interfaces every ops tool is built on.*

Containers, Kubernetes, Terraform providers and observability agents all bottom out in the same Linux primitives: processes and signals, file descriptors and mounts, namespaces and cgroups, sockets and the network stack. Engineers who know them read error messages instead of searching for them. This phase builds each primitive into a small tool first, then shows where it appears in the stack you run.

## How this phase is built

| Lesson | Type | What you build | What you then use |
|---|---|---|---|
| PID 1 & Signals | Build | a supervisor that reaps children, forwards signals and restarts with backoff | `docker stop` timing, `--init`/tini, `exec` form, `terminationGracePeriodSeconds`, systemd `Restart=` |

More lessons in this phase are listed in [ROADMAP.md](https://github.com/nomadicmehul/CloudCaptain/blob/main/ROADMAP.md): file descriptors and `strace`, permissions and capabilities, a tiny `systemd` unit runner, cgroups by hand, the filesystem hierarchy and mounts, `/proc` as an API, and performance basics with `top`, `vmstat` and `perf`.

## Reference layer

The long-form guides, cheat sheet and interview questions live under [Linux](https://cloudcaptain.io/docs/tools/linux/) and [Bash](https://cloudcaptain.io/docs/tools/bash/).
