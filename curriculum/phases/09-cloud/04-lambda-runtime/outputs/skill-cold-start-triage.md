---
name: cold-start-triage
description: Find out why a Lambda function is slow or failing some of the time by splitting latency into init, suppressed init and handler time from REPORT lines, separating throttles and resets from cold starts, measuring what module-level code costs, and choosing between code changes, memory, provisioned concurrency, SnapStart and reserved concurrency
version: 1.0.0
phase: cloud
lesson: cloud/lambda-runtime
tags: [aws, lambda, serverless, cold-start, init, concurrency, provisioned-concurrency, snapstart, throttling, latency, debugging]
---

# Cold-start triage

Use this when a Lambda function's p99 is far above its median, when "the first request is slow", when timeouts or `TooManyRequestsException` appear without a code change, or when someone proposes provisioned concurrency, more memory, a longer timeout or client retries as the fix. Do not change any of those before step 4: each one moves the numbers you need to read first.

## 1. Fix the function and its limits

```bash
FN=<function name>; export AWS_REGION=<region>
aws lambda get-function-configuration --function-name $FN \
  --query '{Runtime:Runtime,Package:PackageType,Memory:MemorySize,Timeout:Timeout,Arch:Architectures,SnapStart:SnapStart,Logging:LoggingConfig,Layers:Layers[].Arn,Vpc:VpcConfig.SubnetIds}'
aws lambda get-function-concurrency --function-name $FN                  # ReservedConcurrentExecutions, or nothing
aws lambda list-provisioned-concurrency-configs --function-name $FN     # per alias or version
aws lambda get-account-settings --query 'AccountLimit.{Concurrent:ConcurrentExecutions,Unreserved:UnreservedConcurrentExecutions}'
```

Write down timeout, memory, reserved and provisioned concurrency, and the account's unreserved pool. Provisioned concurrency only helps requests that go to the alias or version it is configured on; calls to `$LATEST` or another alias always use on-demand environments.

## 2. Read the REPORT lines

Every invocation ends with one `REPORT` line. Cold starts are exactly the lines with `Init Duration`; resets add `Status: timeout` or `Status: error` and an `Error Type`. Run this in CloudWatch Logs Insights on `/aws/lambda/$FN` (it works for text and JSON log formats):

```text
filter @type = "REPORT"
| stats count(*) as invocations, count(@initDuration) as coldStarts, pct(@initDuration, 50) as initP50, pct(@initDuration, 99) as initP99,
        pct(@duration, 50) as p50, pct(@duration, 99) as p99, max(@maxMemoryUsed / 1000 / 1000) as maxMemoryMB by bin(15m)
```

Or pull the raw lines and summarise them offline with the lesson's tool:

```bash
START=$(( $(date +%s) - 3600 ))000
aws logs filter-log-events --log-group-name /aws/lambda/$FN --start-time $START \
  --filter-pattern '?"REPORT RequestId" ?INIT_REPORT ?"Task timed out"' --output json | jq -r '.events[].message' > report-lines.txt
python3 minilambda.py report report-lines.txt          # cold-start rate, init p50/p95, statuses, init failures, init share of billed ms
```

| What you see | Meaning | Next |
|---|---|---|
| slow requests all carry `Init Duration` | cold starts | step 4 (measure init), then step 5 |
| slow requests have no `Init Duration` but follow a `Status: timeout` or `Status: error` line | suppressed init after a reset | fix the timeout or crash first; the slowness is a consequence |
| slow requests have neither | the handler itself is slow (downstream calls, CPU) | not a cold-start problem: trace the handler (X-Ray, timers around calls) |
| `INIT_REPORT ... Status: timeout` | init exceeded 10 s and was retried inside an invocation | step 4; init must get under 10 s or move to provisioned concurrency |
| `INIT_REPORT ... Status: error Error Type: Runtime.ImportModuleError` (or `Runtime.Unknown`) | the module fails at import, every time a new environment starts | read the `[ERROR]` line above it; usually a missing dependency, layer or environment variable |
| `Max Memory Used` equal to `Memory Size`, then `Runtime.ExitError` | out of memory | raise memory or fix the leak before anything else |

## 3. Separate throttles and errors from slowness

```bash
for m in Invocations Errors Throttles ConcurrentExecutions ProvisionedConcurrencySpilloverInvocations; do
  aws cloudwatch get-metric-statistics --namespace AWS/Lambda --metric-name $m --dimensions Name=FunctionName,Value=$FN \
    --start-time $(date -u -d '-3 hours' +%FT%TZ) --end-time $(date -u +%FT%TZ) --period 300 --statistics Sum Maximum --output table
done
aws cloudwatch get-metric-statistics --namespace AWS/Lambda --metric-name ConcurrentExecutions \
  --start-time $(date -u -d '-3 hours' +%FT%TZ) --end-time $(date -u +%FT%TZ) --period 60 --statistics Maximum --output table   # the account, all functions
```

| Situation | Verdict |
|---|---|
| `Throttles` above zero, nothing in the function's logs for those calls | the frontend refused them: a reserved-concurrency cap on this function, or the account pool used up by others |
| account `ConcurrentExecutions` near the account limit when the throttles happen | another function's burst; reserve concurrency for the functions that must not fail, or raise the quota |
| this function's concurrency equals its reserved concurrency | its own cap; raise it, or slow the producer (batch size, queue maximum concurrency) |
| `ProvisionedConcurrencySpilloverInvocations` above zero | traffic exceeded provisioned concurrency and spilled to cold starts; raise it or schedule it with Application Auto Scaling |
| `Errors` rise together with cold starts | init failures surfaced as invocation errors (step 2 table) |
| clients retry after a short client timeout | each retry is another concurrent request and possibly another cold start; client timeout must exceed the function's p99 including init |

Concurrency is arrival rate times duration: 200 requests per second at 250 ms needs 50 environments. A longer duration, including a slower dependency, raises concurrency and therefore cold starts and throttles.

## 4. Measure init

```bash
python3 -X importtime -c 'import app' 2> imports.txt
sort -t'|' -k2 -n imports.txt | tail -15                  # cumulative microseconds per import, largest last
grep -nE '^(import|from) |^[A-Za-z_]+ *= *.*(client|resource|connect|get_secret|get_parameter|requests\.|open\()' app.py
```

Then time a real init with the function's own image or package, without touching production:

```bash
docker run --rm -d --name cc-init -p 127.0.0.1:9001:8080 -v "$PWD:/var/task:ro" public.ecr.aws/lambda/python:3.12.2026.10.15.13 app.handler
time curl -s -XPOST http://127.0.0.1:9001/2015-03-31/functions/function/invocations -d '{}' > /dev/null    # first call: runtime start plus init plus handler
time curl -s -XPOST http://127.0.0.1:9001/2015-03-31/functions/function/invocations -d '{}' > /dev/null    # warm
docker rm -f cc-init
```

The difference between the two calls is the init cost on this machine. The emulator's own `Init Duration` field is not the init time; use the wall-clock difference, or run `minilambda serve` on the code and read its `Init Duration`. Network calls at module level (secrets, parameters, database connections, service discovery) are the usual cause; their time on Lambda includes VPC and DNS latency that a laptop does not have.

## 5. Choose the fix

| Finding | Fix | Cost or caveat |
|---|---|---|
| imports dominate (large SDKs, ML libraries, unused modules) | import lazily inside the code path that needs them, trim the package, drop unused layers | cheapest; also shortens every scale-out |
| module-level network calls | keep clients at module level but defer the first call, cache secrets with a TTL (the Parameters and Secrets extension or a small cache) | first request on an environment still pays the first call |
| init is CPU-bound | more memory (CPU scales with memory; one full vCPU at 1,769 MB) | higher price per ms; check that total billed ms falls |
| traffic has a predictable floor or a known daily ramp | provisioned concurrency on an alias, scheduled with Application Auto Scaling | billed while allocated; excess spills over to cold starts |
| Java 11+, Python 3.12+ or .NET 8+, init above about 1 s | SnapStart on published versions | `Restore Duration` replaces init; re-create anything unique (random seeds, connections, temporary credentials) in an after-restore hook |
| another function's burst throttles this one | reserved concurrency on this function | it becomes a hard cap; leave at least 100 units unreserved in the account |
| timeouts followed by slow requests | fix the cause of the timeout (downstream timeouts shorter than the function's, connection reuse) | raising the timeout alone only makes each failure cost more |

Never "fix" cold starts with a scheduled ping: it keeps one environment warm and does nothing for the second, third or hundredth concurrent request.

## 6. Verify

Re-run the step 2 query over a window of the same length and traffic shape after the change. Compare cold-start count, init p99, overall p99 and billed duration; for provisioned concurrency also `ProvisionedConcurrencyUtilization` and spillover. A change that lowers p99 but raises total billed milliseconds is a cost decision, not a free fix: write both numbers down.

## 7. Report

State: the function and its configuration from step 1; the step 2 numbers before and after (invocations, cold starts and rate, init p50 and p99, overall p50 and p99, timeouts and init failures by error type); whether throttles came from the function's own cap or the account pool; the init breakdown from step 4 (the top imports and module-level calls with their cost); the fix chosen from the step 5 table and why the others were rejected; and the cost effect in billed milliseconds or provisioned-concurrency hours. Never list "added a warm-up ping" or "raised the timeout" as the resolution of a cold-start problem.
