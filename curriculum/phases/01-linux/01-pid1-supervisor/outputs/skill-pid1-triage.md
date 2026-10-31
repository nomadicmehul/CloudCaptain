---
name: pid1-triage
description: Diagnose slow container stops, ignored Ctrl-C, zombie processes and restart loops by looking at what PID 1 is and how signals and child exits flow through it
version: 1.0.0
phase: linux
lesson: linux/pid1-supervisor
tags: [linux, docker, kubernetes, signals, init, debugging]
---

# PID 1 triage

Use this when a container takes the full grace period to stop, ignores Ctrl-C, shows `Z` (zombie) processes, restarts in a loop, or exits with code 137/143 unexpectedly.

## 1. Find out what PID 1 is

```bash
CID=<container>
docker exec "$CID" sh -c 'cat /proc/1/cmdline | tr "\0" " "; echo; ps -o pid,ppid,stat,comm'
docker inspect -f '{{.Config.Entrypoint}} {{.Config.Cmd}} init={{.HostConfig.Init}}' "$CID"
```

Decide which case you are in:

| PID 1 is | Symptom | Fix |
|---|---|---|
| a shell (`sh -c`, `bash entrypoint.sh`) running the app as a child | stop waits for the grace period; Ctrl-C ignored | end the script with `exec "$@"`, or use the exec form `ENTRYPOINT ["node","server.js"]` |
| the app itself, with no SIGTERM handler | same symptom | add a handler that closes listeners and exits; or run under `--init` / `tini` / `dumb-init` so SIGTERM is delivered with default semantics |
| the app, and it forks workers | `Z` rows in `ps`, process table grows | the app must `wait()` for children, or run under an init that reaps (`--init`, `tini -s`) |
| an init already (`tini`, `dumb-init`, `s6`, `mini_init`) | still slow | the app ignores SIGTERM; check with `kill -TERM <pid>` from inside and watch |

## 2. Confirm with a timed stop

```bash
time docker stop -t 10 "$CID"      # ~10s = signal ignored; <1s = handled
docker inspect -f '{{.State.ExitCode}}' "$CID"   # 143 = SIGTERM honoured, 137 = SIGKILL after grace
```

In Kubernetes: `kubectl get pod -o jsonpath='{.spec.terminationGracePeriodSeconds}'`, then `kubectl describe pod` for `Killing` events and the time between `Stopping container` and the container actually exiting.

## 3. Restart loops

Read the exit codes before touching the restart policy: `docker inspect -f '{{.State.ExitCode}} {{.RestartCount}}'` or `kubectl get pod` (`CrashLoopBackOff` with `Last State: Terminated, Exit Code`). Exit 0 under `restart: on-failure` means the process thinks it is done; exit 1 means it is failing; 137 means something killed it (OOM or SIGKILL after grace). Fix the cause; backoff is a symptom.

## 4. Report

State PID 1, the signal path (who receives SIGTERM, who forwards it, who reaps), the measured stop time before and after the change, and the exact line changed (`exec`, `--init`, handler added). Do not recommend `kill -9` in a script as the fix.
