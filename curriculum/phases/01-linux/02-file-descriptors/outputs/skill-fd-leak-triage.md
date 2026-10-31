---
name: fd-leak-triage
description: Diagnose "Too many open files", CLOSE_WAIT piles and disk space that df sees but du cannot, by reading a process's descriptor table against its limit and classifying what is leaking
version: 1.0.0
phase: linux
lesson: linux/file-descriptors
tags: [linux, file-descriptors, ulimit, lsof, sockets, docker, kubernetes, debugging]
---

# File descriptor leak triage

Use this when a service logs `Too many open files` (`EMFILE`, errno 24), when `ss -s` shows a growing `closed`/`CLOSE_WAIT` count, when `df` and `du` disagree about a disk, or when a port stays "in use" after a restart.

## 1. Which limit, which process

```bash
grep -i "too many open files" /var/log/syslog | tail -3          # EMFILE (24): one process's table. ENFILE (23) "in system": the host
cat /proc/sys/fs/file-nr /proc/sys/fs/file-max                  # allocated free max; if allocated is near max it is the host, go to step 6
PID=<pid>
ls /proc/$PID/fd | wc -l; awk '/Max open files/ {print $4, $5}' /proc/$PID/limits
```

Report the count and the soft limit together. Above 80% the next `accept()` or `open()` is the outage.

## 2. Classify what the table holds

```bash
python3 fdwatch.py $PID            # or:
lsof -p $PID | awk 'NR>1 {print $5}' | sort | uniq -c | sort -rn
lsof -p $PID | awk 'NR>1 {print $NF}' | sort | uniq -c | sort -rn | head
```

| Mostly | Pattern | Typical cause |
|---|---|---|
| `IPv4`/`IPv6` sockets in `CLOSE_WAIT` (`ss -tanp state close-wait`) | grows with request rate | responses or upstream connections never closed; missing `with`/`finally`, HTTP client without pooling |
| `REG` files, same path hundreds of times | grows with request rate | a config, template or log opened per request and never closed |
| `REG` files ending in `(deleted)` | one or a few, huge | log rotation without `copytruncate`/reopen; the disk fills while `du` sees nothing |
| `FIFO` pipes | grows with subprocess calls | `subprocess.Popen` stdout/stderr never read or closed |
| `a_inode` (`eventpoll`, `timerfd`) | grows steadily | an event loop, client or timer created per request |
| `unix` sockets to the same path | grows | a new connection to a local daemon (Docker, systemd, DNS stub) per call |

## 3. Measure the rate

```bash
python3 fdwatch.py $PID --watch 10 --count 6      # rate per minute and time to the soft limit
# or: for i in 1 2 3; do ls /proc/$PID/fd | wc -l; sleep 10; done
```

Quote the rate and the forecast ("1024 in about 14 minutes"). That number decides whether you fix now or after a stop-gap.

## 4. Stop-gaps that need no restart

```bash
prlimit --pid $PID --nofile=65536:65536           # buys time only; say so in the ticket
: > /proc/$PID/fd/<fd>                            # truncate a deleted-but-open file and free its blocks
```

Never present either as the fix. Write down the new time-to-limit after the change.

## 5. The fix is in the code

Name the call site from the repeated path or the socket peer, then: close in `finally`/`with`; pool connections instead of opening per request; read and close subprocess pipes; create one event loop or client per process; pass `close_fds=True` / open with `O_CLOEXEC` so children stop inheriting the listening socket; make rotation reopen the file (`copytruncate`, `SIGHUP` handler, `WatchedFileHandler`). Then set the limit deliberately where the process starts: `LimitNOFILE=` in the unit, `--ulimit nofile=` for Docker, the runtime's defaults for Kubernetes (`kubectl exec POD -- sh -c 'ulimit -n'` shows what you got; there is no pod field).

## 6. Containers and Kubernetes

```bash
docker exec $CID sh -c 'ls /proc/1/fd | wc -l; ulimit -n'
docker inspect -f '{{.HostConfig.Ulimits}}' $CID
kubectl debug -it POD --image=busybox --target=APP_CONTAINER -- sh -c 'ls /proc/1/fd | wc -l; cat /proc/1/limits | grep "open files"'
```

`--target` shares the process namespace so `/proc/1` is the application. If `file-nr` on the node is near `file-max`, the host is the problem: find the top processes with `for p in /proc/[0-9]*; do echo "$(ls $p/fd 2>/dev/null | wc -l) $p"; done | sort -rn | head`.

## 7. Report

State: the error (`EMFILE` or `ENFILE`), count against limit, the dominant kind and its target or peer, the measured rate and forecast, the stop-gap applied and its new forecast, and the code change. Do not recommend raising the limit as the resolution.
