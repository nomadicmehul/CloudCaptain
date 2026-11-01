---
name: strace-triage
description: Trace a failing process safely on a host, in Docker or in Kubernetes, reduce the trace to the failing call, its target and errno, and map the errno to what to check next
version: 1.0.0
phase: linux
lesson: linux/syscalls-strace
tags: [linux, strace, syscalls, errno, ptrace, docker, kubernetes, debugging]
---

# strace triage

Use this when a process exits with a message that does not name the file, address or permission involved, when something "works on my machine" but not in the container, when a pod is in `CrashLoopBackOff` with a one-line log, when startup is slow, or when a process hangs.

## 1. Choose how to trace

| Situation | Command | Cost and caveat |
|---|---|---|
| you can restart it (dev, staging, a job) | `strace -f -tt -T -y -s 200 -e trace=%file,%network,%process,write -o /tmp/t.txt -- CMD ARGS` | the traced program runs slower; fine for a reproduction |
| it is running on a host, same uid | `strace -p PID -f -tt -T -y -e trace=%network,%file -o /tmp/t.txt` for at most 30 s, then Ctrl-C to detach | ptrace stops the process at every traced call; on a hot path use `perf trace -p PID` or `bpftrace` instead and never leave strace attached |
| it is running in a Docker container | `docker run --rm -it --pid=container:NAME --cap-add SYS_PTRACE IMAGE_WITH_STRACE strace -p 1 -f -e trace=%network` | the app image does not need strace; the sidecar shares the PID namespace and carries the capability |
| it is a Kubernetes pod | `kubectl debug -it POD --image=ubuntu --target=APP_CONTAINER --profile=general -- bash`, then `apt-get update && apt-get install -y strace && strace -p 1 -f -e trace=%file` | `--target` shares the PID namespace; `general` adds `SYS_PTRACE`; `sysadmin` runs privileged if that is not enough |
| it only fails under a condition you cannot reproduce | `strace -e inject=openat:error=ENOENT:when=3 CMD` or `-e inject=connect:delay_enter=2000000 CMD` | fault injection without touching the filesystem or network |

Attaching to a process you did not start needs `CAP_SYS_PTRACE` or `kernel.yama.ptrace_scope=0`; tracing a child you start needs neither, and Docker's default seccomp profile allows it on current kernels. `strace: attach: ptrace(PTRACE_SEIZE, 1): Operation not permitted` is the capability missing, not seccomp.

## 2. Reduce the trace to one sentence

```bash
python3 tracelens.py /tmp/t.txt                 # verdict, failures by errno, slowest, histogram, files per process
python3 tracelens.py /tmp/t.txt --verdict       # the one line
```

By hand, in this order:

```bash
grep -E '\+\+\+ (exited|killed)' /tmp/t.txt                       # who exited with what
grep -oE '= -1 E[A-Z]+' /tmp/t.txt | sort | uniq -c | sort -rn      # errnos by count
awk -F'"' '/= -1 ENOENT/ {print $2}' /tmp/t.txt | uniq               # the paths tried, in order
grep -E 'write\(2' /tmp/t.txt | tail -3                              # the error message the program printed
strace -Z ...                                                        # next time: print only failing calls
```

Rules of thumb:

- The cause is the **last failing call before the exit** of the process that exited non-zero. Everything the program recovered from is earlier.
- A run of `ENOENT` on the **same basename in different directories** is a search path (PATH, `sys.path`, `LD_LIBRARY_PATH`, a config search order). Read the order; the fix is usually "put the file where the first lookup happens" or "fix the variable that builds the path".
- Ignore `EAGAIN`, `EINTR`, `EINPROGRESS`, `ENOSYS`, `ENOTTY`, `EEXIST`, `EINVAL` and `/etc/ld.so.preload` unless they are the only errors.
- The path or address in the trace is the one the process used, after `~`, relative paths, DNS and environment variables were resolved. Trust it over the config file.
- If the trace ends without `+++ exited`, the process is alive or was killed by something outside: check `dmesg` for the OOM killer and the container's `State.OOMKilled`.

## 3. errno to what to check

| errno | Meaning | Check |
|---|---|---|
| `ENOENT` | no such name, or a missing component | the path exactly as traced; `readlink /proc/PID/cwd`; in a container, is it in the image or in a volume mounted over it? |
| `ENOTDIR` | a component is a file | `-v file:/dir` bind mounts, a path built from the wrong variable |
| `EACCES` | file mode, a directory without `x`, or AppArmor/SELinux | `ls -ld` every component; `Uid:` in `/proc/PID/status`; `dmesg`, `ausearch -m AVC` |
| `EPERM` | a capability or seccomp rule said no | `CapEff` in `/proc/PID/status`; `--cap-add`, `securityContext.capabilities`; the seccomp profile; `ptrace_scope` |
| `ECONNREFUSED` | RST: nothing listens there from this namespace | `ss -ltnp` on the target; `127.0.0.1` inside a container is the container |
| `ETIMEDOUT`, `EHOSTUNREACH`, `ENETUNREACH` | dropped or unroutable | security groups, NetworkPolicy, `ip route get ADDR`, the CNI |
| `EADDRINUSE` | the port is already bound | `ss -ltnp`; a previous instance; `SO_REUSEADDR` |
| `EMFILE` | this process's descriptor table is full | the file-descriptors lesson: find the leak, not the ulimit |
| `ENOSPC`, `EDQUOT` | no blocks, no inodes, or quota | `df -h`, `df -i`, `quota -s` |
| `EROFS` | read-only filesystem | `--read-only`, `readOnlyRootFilesystem`; write to a volume or `tmpfs` |
| `ENOEXEC` | not an executable for this kernel | `file BIN`; image architecture vs node architecture; missing shebang |
| `ENOSYS` | the kernel or seccomp does not know the call | runtime and kernel versions; the profile's default action |

## 4. Confirm with a second trace

Apply the fix, trace again, and expect the verdict to become `no failing system calls worth reporting` or to move to the next cause. A fix that changes the message but not the failing call is not a fix.

## 5. Report

State: the process and its exit code, the failing system call, the target exactly as traced, the errno and its text, how many locations or attempts preceded it, what the program wrote to stderr, and the change made. Attach the ten lines around the culprit, not the whole trace: traces contain paths, environment variables and sometimes secrets passed as arguments.
