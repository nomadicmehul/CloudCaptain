---
name: load-balancer-review
description: Review a load balancer configuration (HAProxy, nginx, Envoy, an AWS target group or a Kubernetes Service) for what its health checks really test, detection and recovery time, passive ejection and its percentage cap, retry safety and budgets, draining during deploys, and whether the algorithm and hashing scheme fit the workload, then report findings with fixes
version: 1.0.0
phase: networking
lesson: networking/load-balancing
tags: [load-balancing, haproxy, nginx, envoy, alb, kubernetes, health-checks, outlier-detection, retries, consistent-hashing, draining]
---

# Load balancer review

Use this before a launch, after an incident in which "the load balancer kept sending traffic to a broken instance", when a deploy causes a burst of 502s, when p99 is high while the median is fine, or when someone proposes "just add retries". The review is read-only: collect configuration and evidence, change nothing, and finish with the report in step 8. Every finding needs the line of configuration it comes from and the command output that shows it.

## 1. Collect the configuration and the health endpoint

| Balancer | Configuration | Live state |
|---|---|---|
| HAProxy | `haproxy -c -f /etc/haproxy/haproxy.cfg` then read the file; `defaults` and `default-server` apply to every backend | `echo 'show stat' \| socat stdio /run/haproxy/admin.sock \| cut -d, -f1,2,18` (status column); `echo 'show servers state' \| socat ...` |
| nginx | `nginx -T` prints the whole configuration with every include | `access_log` with `$upstream_addr $upstream_status $upstream_response_time`; NGINX Plus adds an API |
| Envoy | `curl -s localhost:9901/config_dump` (clusters, routes, `outlier_detection`, `health_checks`, `retry_policy`) | `curl -s localhost:9901/clusters` (per host `health_flags`, `cx_active`, `rq_active`); `curl -s localhost:9901/stats \| grep -E 'outlier_detection\|retry'` |
| AWS ALB or NLB | `aws elbv2 describe-target-groups --names TG`; `aws elbv2 describe-target-group-attributes --target-group-arn ARN` | `aws elbv2 describe-target-health --target-group-arn ARN` |
| Kubernetes Service | `kubectl get deploy APP -o jsonpath='{.spec.template.spec.containers[*].readinessProbe}'`, `terminationGracePeriodSeconds`, `lifecycle.preStop` | `kubectl get endpointslices -l kubernetes.io/service-name=SVC -o wide` |

Then open the code behind the health path (`/health`, `/healthz`, `/ready`). Write down what it checks: process alive only, or the dependencies the main request path needs (database pool, cache, downstream calls), and whether it has its own timeout.

## 2. What does the health check prove, and how fast?

| Question | How to answer | Finding if |
|---|---|---|
| Does the check exercise the request path? | the health handler's code from step 1 | it returns 200 without touching anything the real requests need: a broken dependency stays invisible to active checks |
| Can the check take the whole pool down? | does it call a shared dependency every backend uses | yes: one database blip marks every backend down at once; check local readiness and let passive detection catch shared failures, or rely on panic mode or fail-open behaviour knowingly |
| Detection time | `interval × fall`, plus up to one `timeout` | longer than the error budget allows; ALB defaults (30 s interval, 2 unhealthy, 5 healthy) detect in about 60 s and recover in about 150 s |
| Recovery time | `interval × rise` | a flapping backend returns before it is stable: `rise` of 1, or Envoy's `healthy_threshold: 1` |
| Status codes | HAProxy `option httpchk` passes 2xx and 3xx unless `http-check expect`; ALB `Matcher`; Envoy `expected_statuses` | a redirect to a login page counts as healthy |
| Envoy specifics | `health_checks[].http_health_check` | a status outside `expected_statuses` makes the host unhealthy immediately; the threshold only applies to timeouts, connection failures and `retriable_statuses` |
| nginx open source | `nginx -T \| grep -n health_check` | nothing: open-source nginx has no active checks (`health_check` is NGINX Plus); only passive `max_fails` applies |
| Kubernetes | readiness probe `periodSeconds × failureThreshold` | no readiness probe: kube-proxy has no checks of its own and sends connections to the pod as soon as it is Running |

## 3. Passive detection

```bash
nginx -T | grep -nE 'max_fails|fail_timeout|proxy_next_upstream|zone'
grep -nE 'observe|error-limit|on-error' /etc/haproxy/haproxy.cfg
curl -s localhost:9901/config_dump > config_dump.json && grep -n -A8 '"outlier_detection"' config_dump.json
```

| Configuration | Verdict |
|---|---|
| no passive detection at all | a backend whose health endpoint lies keeps its full share; add Envoy `outlier_detection`, HAProxy `observe layer7` with `error-limit` and `on-error mark-down`, or nginx `max_fails` with `http_500` in `proxy_next_upstream` |
| Envoy `max_ejection_percent` (default 10) with fewer than 10 hosts | finding: ejecting one host would exceed the cap, so nothing is ever ejected; raise it to at least `ceil(100 / hosts)` or set `always_eject_one_host` |
| HAProxy `observe layer7` with default `on-error fail-check` and `error-limit 10` | ten errors in a row only count as one failed check; a broken server keeps traffic until `fall` checks fail |
| nginx `max_fails` without `zone` in the upstream | each worker counts on its own, so `max_fails=3` lets through up to three per worker |
| nginx `proxy_next_upstream` left at `error timeout` | a 500 is not a failure; the backend is never marked unavailable for answering 500 |
| a least-connections or P2C algorithm without passive detection | finding, high: a backend that fails fast attracts more traffic than its share |

## 4. Retries

```bash
nginx -T | grep -nE 'proxy_next_upstream(_tries|_timeout)?|non_idempotent'
grep -nE 'retries|retry-on|redispatch' /etc/haproxy/haproxy.cfg
curl -s localhost:9901/config_dump | grep -nE '"retry_on"|"num_retries"|"retry_budget"|"retriable_request_headers"|"previous_hosts"'
```

| What you find | Verdict |
|---|---|
| retries on connection failures only (HAProxy default `retry-on conn-failure`) | safe for every method: nothing reached the backend |
| retries on 5xx or timeouts for routes that accept `POST` or `PATCH` | finding, high: a request that was executed and then timed out is executed twice. nginx skips non-idempotent requests unless `non_idempotent`; HAProxy and Envoy do not look at the method, so scope the policy (Envoy `retriable_request_headers` on `:method`, separate routes) or require an idempotency key the application honours |
| no limit on retries in flight | finding: a slow backend turns into retry load on every other backend; add an Envoy `retry_budget` (20% of active requests, minimum 3) or lower the attempt count |
| retries to the same backend | add Envoy's `previous_hosts` predicate or HAProxy `option redispatch`; nginx always tries the next server |
| retries at several layers (client, mesh, balancer) | multiply the attempts per layer: three layers of three attempts is 27 requests to a dead dependency; keep retries in one layer |

## 5. Draining and deploys

| Platform | Check | Finding if |
|---|---|---|
| ALB / NLB | `deregistration_delay.timeout_seconds` in the target group attributes | shorter than the longest normal request, or longer than the deploy tool waits per instance |
| HAProxy | how servers leave: `set server BE/SRV state drain` (keeps persistent sessions) or `maint` (stops everything) | servers are removed by editing the file and restarting, which drops in-flight requests |
| nginx | `server ... down` plus `nginx -s reload` | the process is restarted instead of reloaded |
| Kubernetes | `preStop` sleep, `terminationGracePeriodSeconds`, readiness turning false on SIGTERM | the app exits on SIGTERM immediately: kube-proxy rules and external balancers still send connections for a few seconds, which shows up as 502s on every rollout |

## 6. Algorithm and hashing fit

| Workload | Good fit | Finding if configured |
|---|---|---|
| short, uniform requests, identical backends | round robin or weighted round robin | anything with state you do not need |
| request durations vary, or backends differ in speed | least connections, or power of two choices with several balancer instances | round robin: compare p99, not the mean, per backend |
| cache or session locality (key = user, URL, tenant) | consistent hashing: HAProxy `hash-type consistent`, nginx `hash ... consistent`, Envoy `RING_HASH` or `MAGLEV` | HAProxy's default `map-based`, nginx plain `hash` or `ip_hash`: most keys move on every scale event |
| a few very hot keys | bounded-load hashing: HAProxy `hash-balance-factor`, Envoy `hash_balance_factor` | plain consistent hashing: one backend melts |
| long-lived connections (HTTP/2, gRPC, WebSockets) behind an L4 balancer | an L7 balancer that spreads requests, or client-side balancing | a Kubernetes Service or NLB alone: new pods get no traffic from existing connections |
| sticky sessions | only when state really lives on the backend; cookie with a re-pick when the backend leaves | stickiness used to hide missing shared session storage |

## 7. Prove one finding

Pick the most severe finding and reproduce it in a lab before writing it up. With `minilb` from the lesson, three `minilb backend` processes and the balancer's own configuration (or its closest `minilb serve` flags), switch one backend to fail with `POST /_backend/mode?status=500` and count client-visible errors; or run `minilb simulate` with the measured latencies and compare algorithms on p99; or `minilb remap` to show how many keys a scale event moves.

## 8. Report

State: the balancer, version and the configuration files read; the health endpoint and what it proves; detection and recovery time computed from the settings; a findings table with columns severity, finding, evidence (configuration line and command output), fix (the exact directive), and the mechanism it belongs to (active check, passive check, retries, draining, algorithm); the finding you reproduced and its numbers; and what you did not check. Never recommend "add retries" without naming which requests are idempotent and what caps the extra load.
