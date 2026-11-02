---
name: connection-timeout-triage
description: Diagnose hanging requests, connect timeouts, connection resets and "cannot assign requested address" by capturing the TCP socket table and counters before any restart, classifying the symptom by TCP state, confirming it with one command, and fixing the missing timeout or the leak instead of the symptom
version: 1.0.0
phase: networking
lesson: networking/tcp-states
tags: [networking, tcp, timeouts, keepalive, ss, nstat, close-wait, time-wait, backlog, load-balancer, debugging]
---

# Connection and timeout triage

Use this when requests hang or slow down without CPU load, when clients log `connect timed out`, `Connection reset by peer` (ECONNRESET), `Connection refused` (ECONNREFUSED) or `Cannot assign requested address` (EADDRNOTAVAIL), when a load balancer returns 502 or 504, or when someone proposes a restart as the fix. Do not restart anything until step 1 is done: a restart destroys the socket table, which is the evidence.

## 1. Capture before anyone restarts

Run on the host or inside the pod's network namespace (`nsenter -t PID -n` from the node, or `kubectl debug` with an image that has iproute2). `/proc/net/*` and every counter belong to that namespace.

```bash
D=/tmp/tcp-triage-$(date +%s); mkdir -p $D
ss -tanpio > $D/ss.txt                 # all TCP sockets, owners, timers, tcp_info; never filter one state here
ss -lnt > $D/listen.txt                # listeners: Recv-Q = waiting for accept(), Send-Q = backlog
ss -s > $D/summary.txt
nstat -asz > $D/nstat-1.txt; sleep 10; nstat -asz > $D/nstat-2.txt   # counters twice, ten seconds apart
cat /proc/net/tcp /proc/net/tcp6 > $D/proc-net-tcp.txt 2>/dev/null
sysctl net.ipv4.tcp_syn_retries net.ipv4.tcp_retries2 net.ipv4.tcp_fin_timeout net.ipv4.tcp_keepalive_time \
  net.ipv4.tcp_keepalive_intvl net.ipv4.tcp_keepalive_probes net.ipv4.ip_local_port_range net.core.somaxconn > $D/sysctl.txt
python3 tcpwatch.py diagnose --ss $D/ss.txt --nstat $D/nstat-2.txt --json > $D/tcpwatch.json   # optional, from the lesson
```

Note the exact client error message and which side reports it. A timeout, a reset and a refusal are three different mechanisms.

## 2. Classify by state

Count the states first (`ss -s`, or `awk 'NR>1 {print $1}' $D/ss.txt | sort | uniq -c`), then match the largest abnormal group.

| What you see | Meaning | Confirm with | Stop-gap | Fix |
|---|---|---|---|---|
| Many `CLOSE-WAIT`, one process, growing | the peer closed; this process never calls `close()` | `ss -tanp state close-wait` twice, a minute apart; owner and count | restart the process (it will come back) | the code path that drops a socket without `close()`: error branches, early returns, a client object created per request, a pool that never releases |
| `CLOSE-WAIT` with no process on a listening port | finished connections waiting in the accept queue; clients gave up | `ss -lnt` for that port | none | see the next row |
| Listener with Recv-Q greater than Send-Q | accept queue full; new SYNs dropped silently | `nstat -az TcpExtListenOverflows` rising between captures | add replicas, shed load | find why `accept()` stalls (blocked event loop, too few workers, a deadlock on a downstream call with no timeout); only then size the backlog (`listen()` argument, capped by `net.core.somaxconn`) |
| Many `SYN-RECV` on one port, SYN-ACK retransmits | handshakes not completed by clients | `ss -tano state syn-recv` timers; `nstat TcpExtTCPSynRetrans` | keep `tcp_syncookies=1` | full accept queue (row above), broken return path (asymmetric routing, a firewall dropping the client's ACK) or a SYN flood |
| `SYN-SENT` with `timer:(on,...,N)`, N above 0 | `connect()` nobody answers, not even with RST | `ss -tano state syn-sent`; `curl --connect-timeout 2 -v` to the same address | set a connect timeout so callers fail fast | the security group, firewall or route that drops SYNs; a host that is down; a full accept queue on the server |
| `ESTAB` with Send-Q above 0 and `timer:(on,...,N)` | the peer or path is not acknowledging data | `ss -tio dst PEER`: `retrans`, `backoff`, `rto`, `lastack` | close the stuck connections (`ss -K dst PEER dport = :PORT` where the kernel supports it) | find the drop (`tcpdump` on both ends, the middlebox); bound it per socket with `TCP_USER_TIMEOUT` or a read timeout |
| `ESTAB` with `timer:(persist,...)` | the peer advertises a zero window: its application stopped reading | on the peer, its Recv-Q on the same connection is large | none | the reader on the peer; a write timeout on this side bounds the wait |
| Many `FIN-WAIT-2` to one peer | this side closed; the peer has not | on the peer: `ss -tanp state close-wait` | none needed here | the peer's missing `close()` |
| Many `TIME-WAIT` to one destination, `EADDRNOTAVAIL` | local ports for that destination exhausted | `ss -Htan state time-wait \| awk '{print $4}' \| sort \| uniq -c \| sort -rn \| head` against `ip_local_port_range` | `net.ipv4.tcp_tw_reuse=1` for outgoing connections; widen the range | reuse connections (pool, HTTP keep-alive); more source addresses or destination ports |
| Healthy-looking `ESTAB`, idle for minutes, then ECONNRESET or a long hang on first use | a load balancer, NAT gateway or firewall forgot the idle flow | the idle time before failure against the middlebox's idle timeout (ALB 60 s, NLB and NAT gateway 350 s, Azure LB 4 min) | retry once on a fresh connection for idempotent requests | keepalive with `TCP_KEEPIDLE` below that idle timeout, or a pool max-idle time below it; servers behind a load balancer keep connections open longer than its idle timeout |
| Nothing abnormal in the table, client still hangs | the wait is above TCP: DNS, TLS, a lock, a slow query | `curl -w '%{time_namelookup} %{time_connect} %{time_appconnect} %{time_starttransfer}\n'` | none | the slow phase that measurement names |

## 3. Check every client on the path for its timeouts

For each hop (browser or caller, load balancer, service, its outgoing clients to databases, caches and APIs), write down four values: connect timeout, read timeout, total timeout, keepalive. Look in code and config, not in documentation:

| Client | Default that bites |
|---|---|
| Python `requests`, `urllib`, raw `socket` | no timeout at all unless passed |
| Go `http.DefaultClient` | no timeout; set `http.Client.Timeout` and the transport's dial and idle timeouts |
| `curl` | connect timeout 300 s, no total limit; use `--connect-timeout` and `--max-time` |
| Kernel TCP | SYN gives up after about 127 s (`tcp_syn_retries=6`), unacknowledged data after at least 924.6 s (`tcp_retries2=15`), idle connections never, keepalive off per socket and 7200 s idle by default |

A value of "none" on any hop is a finding even if it is not today's cause. Every inner timeout should be shorter than the outer one, so the caller gets an error instead of the load balancer's 504.

## 4. Fix, then verify

Apply the fix that addresses the state, not the restart. Verify with the same commands as step 1:

```bash
ss -s; ss -lnt; ss -tanp state close-wait | wc -l
nstat TcpExtListenOverflows TcpExtTCPAbortOnTimeout TcpExtTCPSynRetrans     # deltas since the last nstat run
ss -tano dst PEER | grep -o 'timer:([a-z]*' | sort | uniq -c                 # keepalive timers present after a keepalive change
```

The group you acted on must stop growing over at least two captures, and the client error rate must fall. If a second group appears, it was hidden behind the first; go back to step 2.

## 5. Report

State: the client error and where it was observed; the capture time and namespace; the state histogram; the group you identified with its count, owner process and peer, and the command output that confirmed it; the counter deltas (`TcpExtListenOverflows`, `TcpExtTCPSynRetrans`, `TcpExtTCPAbortOnTimeout`); the timeout table from step 3 with every "none" marked; the stop-gap applied, if any; the fix and the change that carries it; and the evidence from step 4 that it worked. Never list "restarted the pods" or "raised ulimit/somaxconn" as the resolution on its own.
