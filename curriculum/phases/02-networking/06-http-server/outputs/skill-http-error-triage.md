---
name: http-error-triage
description: Find which hop produced an HTTP error (client, load balancer, nginx or another proxy, application), confirm the cause from that hop's logs, check the timeout ordering between hops, reproduce with curl and minihttp probe, and report the fix
version: 1.0.0
phase: networking
lesson: networking/http-server
tags: [http, nginx, reverse-proxy, "502", "504", timeouts, keepalive, request-smuggling, curl]
---

# HTTP error triage

Use this when clients report 4xx or 5xx responses, connection resets or empty replies from a service behind one or more proxies, or when someone proposes "raise all the timeouts" as the fix. Work one hop at a time from the client inward. Do not restart anything before step 2: a restart clears the pooled connections and the evidence with them.

## 1. Which hop answered?

```bash
URL=https://shop.example.com/api/orders
curl -sv --http1.1 -o body.txt "$URL" 2> verbose.txt; grep -E '^[*<>]' verbose.txt
curl -s -o /dev/null -w 'status=%{http_code} connect=%{time_connect} tls=%{time_appconnect} first_byte=%{time_starttransfer} total=%{time_total}\n' "$URL"
head -c 300 body.txt
```

| What you see | Meaning | Next |
|---|---|---|
| `Server: nginx` and an HTML body titled with the status | nginx generated it, the upstream may never have been asked | nginx error log for that second (step 2) |
| the application's JSON error format, its own `Server` | the application answered; the proxies only relayed it | application logs, not proxy tuning |
| `Server: awselb/2.0` or another load balancer's name | the load balancer answered: no healthy target, or its own timeout | target health and LB access logs |
| `curl: (52) Empty reply from server` | the connection closed with no response at all | a header timeout, a crash, or an idle connection reused after the server closed it |
| `curl: (56) ... Connection reset by peer` | a reset instead of a response | server closed with unread request data (no lingering close), or a middlebox |
| `Via` lists several hops | each proxy appended itself in order | start with the innermost hop that appears |

If nginx is a hop, make its access log say who answered: add `$upstream_addr $upstream_status $upstream_response_time $request_time` to the `log_format`. `$upstream_status` of `-` means nginx answered on its own; a value means the upstream did.

## 2. Classify the status and confirm it in the log of the hop that produced it

```bash
grep -E '" (4|5)[0-9]{2} ' /var/log/nginx/access.log | tail -50
python3 minihttp.py logstat /var/log/nginx/access.log               # counts per status from the combined format
grep -E 'upstream|client (sent|intended|timed)|while reading|while connecting' /var/log/nginx/error.log | tail -50
```

| Status | nginx error-log line that confirms it | Cause | Fix |
|---|---|---|---|
| 400 | `client sent "Content-Length" and "Transfer-Encoding" headers at the same time`, `client sent duplicate header line`, `client sent invalid chunked body`, `client sent HTTP/1.1 request without "Host" header` | a broken client or a smuggling probe | fix the client; never relax the parser; check that every hop in front rejects the same request |
| 400 with `Request Header Or Cookie Too Large` | `client sent too long header line` | oversized cookies or tokens | shrink cookies; raise `large_client_header_buffers` only for the server that needs it |
| 408 (logged only, nothing sent) | `client timed out` at `info` level | header not complete within `client_header_timeout`, or body stalled past `client_body_timeout` | slow mobile clients: raise slightly; many from few IPs: a slowloris pattern, add connection limits |
| 413 | `client intended to send too large body: N bytes` | `client_max_body_size` (1m by default) | raise it in the one `location` that accepts uploads |
| 414 | `client sent too long URI` | a query string used as storage | move the data into a POST body |
| 499 | none: nginx-specific code for "client closed the connection first" | the client's timeout is shorter than the response time | compare `$request_time` with the client's timeout; fix the slow path or the client timeout |
| 502 | `connect() failed (111: Connection refused) while connecting to upstream` | upstream not listening: crashed, wrong port, not started | `ss -ltnp` on the upstream host, process status |
| 502 | `upstream prematurely closed connection while reading response header from upstream`, `recv() failed (104: Connection reset by peer)` | the upstream crashed mid-request, or nginx reused a pooled connection the upstream had just closed | if only non-idempotent methods fail and only after idle periods: the keep-alive race in step 3 |
| 502 | `upstream sent too big header`, `upstream sent invalid header` | headers larger than `proxy_buffer_size`, or a broken upstream | raise `proxy_buffer_size`, or fix the upstream's response |
| 503 | `limiting requests, excess` or `limiting connections by zone` | `limit_req` or `limit_conn` (503 by default) | check `$upstream_status`: `-` means nginx's own limit, a value means the app said 503 |
| 504 | `upstream timed out (110: Connection timed out) while reading response header from upstream` | the upstream sent nothing for `proxy_read_timeout` (60s by default, between two reads) | find the slow request in the application; raise the timeout only for the endpoint that needs it |
| 504 | `upstream timed out (110: Connection timed out) while connecting to upstream` | no TCP handshake within `proxy_connect_timeout` | SYNs dropped (security group, firewall, full listen backlog); see the TCP states lesson |

## 3. Check the timeout ordering between hops

Two rules. Write the values of every hop into a table before changing any of them.

| Rule | Why | Check |
|---|---|---|
| Idle keep-alive: the hop that **reuses** a connection must give up on it before the hop that **closes** it | otherwise the reuser writes a request onto a connection being closed: 502 for POSTs, invisible retries for GETs | application server keep-alive timeout greater than nginx's `keepalive_timeout` in the `upstream` block (60s by default; current nginx pools upstream connections by default); nginx's client `keepalive_timeout` greater than the load balancer's idle timeout |
| Request deadlines shrink inward | the innermost hop should fail first with a meaningful error, not the outer one with a bare 504 or 499 | client timeout greater than load balancer timeout greater than `proxy_read_timeout` greater than the application's own work timeout |

Do not paper over the idle race with `proxy_next_upstream non_idempotent`: it retries POSTs that the upstream may already have processed.

## 4. Reproduce it

```bash
curl -v --http1.1 "$URL" "$URL" -o /dev/null -o /dev/null 2>&1 | grep -E '^[*<>]'   # is the connection reused, and does the second request fail?
curl -s -o /dev/null -w '%{http_code} %{time_total}\n' --max-time 120 "$URL"         # 504 at exactly proxy_read_timeout?
python3 minihttp.py probe --target HOST:PORT --header-timeout 60 --keepalive-timeout 75 --upstream-timeout 60   # the 32-case matrix against the proxy
printf '%s' "$RAW_REQUEST" | python3 minihttp.py parse -                               # how a captured byte stream splits into requests
```

For a suspected smuggling issue, send the same raw bytes to each hop separately (`nc HOST PORT < request.raw`) and compare how many responses come back and whether the connection closes.

## 5. Fix and verify

Change one value on one hop. Re-run the reproduction from step 4 and watch the error-log line from step 2 disappear for at least one full idle cycle (the longest keep-alive timeout involved) before calling it fixed. `nginx -t` before every reload.

## 6. Report

State: the failing request (method, URL, status, rate); which hop produced the status and the evidence (headers, `$upstream_status`, the exact error-log line); the timeout table for every hop with the rule that was violated, if any; the single change made, on which hop, with the before and after values; the reproduction command and its result before and after. Never list "restarted nginx" or "raised all timeouts" as the resolution.
