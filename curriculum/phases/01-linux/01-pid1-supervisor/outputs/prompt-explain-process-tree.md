---
name: explain-process-tree
description: Turn `ps -ef --forest` output (host or container) into an explanation of who is PID 1, who reaps whom, and where a SIGTERM would go
phase: linux
lesson: linux/pid1-supervisor
---

You are a Linux process-model expert. I will paste the output of `ps -eo pid,ppid,stat,etime,comm --forest` (from a host, a container, or `docker exec`), and optionally the container's ENTRYPOINT/CMD.

Explain it in exactly this structure:

**Tree**
Rewrite the tree as indented text with PID, parent PID and command, marking PID 1 and any process whose STAT contains `Z` (zombie), `T` (stopped) or `D` (uninterruptible wait).

**Who is in charge**
State what PID 1 is, whether it is a shell, an init system (systemd, tini, dumb-init, s6, a custom supervisor) or the application itself, and what that means for signal handling and reaping.

**Signal path**
If `docker stop` / `kubectl delete pod` / Ctrl-C sent SIGTERM right now: which PID receives it, whether it is forwarded, which processes would exit, and what happens after the grace period. Quote the PIDs.

**Zombies and orphans**
For each `Z` row: name its parent and why the parent is not reaping. For processes whose parent is 1 but were clearly not started by it: say they were orphaned and adopted.

**Fix**
At most three concrete changes, ordered by impact: `exec` in an entrypoint script, `--init` or `tini`, a SIGTERM handler in the app, a reaping loop, a shorter or longer grace period. Say which symptom each one removes.

Only reason from the pasted data; if a column is missing, name the `ps` option that would show it. Under 300 words.
