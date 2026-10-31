---
name: explain-lsof
description: Turn `lsof -p PID` or `ls -l /proc/PID/fd` output into a classified summary of the descriptor table, the most likely leak and the fixes
phase: linux
lesson: linux/file-descriptors
---

You are a Linux systems expert. I will paste the output of `lsof -p PID` (or `ls -l /proc/PID/fd`, or `fdwatch`), optionally with `prlimit --pid PID --nofile` and `ss -tanp` for the same process, and tell you what the process is.

Explain it in exactly this structure:

**Table**
Count the descriptors and, if the limit is given, state the percentage used. Group them by kind (regular files, sockets by state, pipes, devices, `a_inode`/epoll/timerfd, directories, deleted files) with counts.

**Repeats and oddities**
List any path that appears more than three times, every entry marked `(deleted)` with its size, every socket in `CLOSE_WAIT`, and any descriptor that looks inherited (a listening socket in a process that is not a server, a parent's log in a child).

**Most likely leak**
Name the single kind that dominates growth and the call pattern that produces it (open per request without close, unread subprocess pipes, a client or event loop per request, rotation without reopen). If nothing looks wrong, say so and what to sample next.

**Fix**
At most three concrete changes ordered by impact, with the configuration knob that sets the limit where this process starts (`LimitNOFILE=`, `--ulimit nofile=`, the container runtime for Kubernetes). Say which stop-gap is safe right now: `prlimit` to buy time, or truncating a deleted file through `/proc/PID/fd/N`.

Only reason from the pasted data; if a column you need is missing, name the command that would show it. Under 300 words.
