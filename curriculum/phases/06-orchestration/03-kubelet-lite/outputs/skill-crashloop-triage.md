---
name: crashloop-triage
description: Diagnose a pod in CrashLoopBackOff, Init:CrashLoopBackOff or a probe-driven restart loop by reading lastState, the exit code, the previous container's logs and the kubelet's events before changing anything
version: 1.0.0
phase: orchestration
lesson: orchestration/kubelet-lite
tags: [kubernetes, kubelet, crashloopbackoff, probes, debugging, crictl, static-pods]
---

# CrashLoopBackOff triage

Use this when `kubectl get pods` shows `CrashLoopBackOff`, `Init:CrashLoopBackOff`, `Error`, a `RESTARTS` count that keeps climbing, or a pod that is `Running` but never `READY`. The kubelet has already recorded why it restarted the container. The job is to read that record before deleting pods or changing probes, because deleting the pod throws away the crashed container and its logs.

## 1. Freeze the evidence

```bash
NS=<namespace>; POD=<pod>
kubectl get pod $POD -n $NS -o wide
kubectl get pod $POD -n $NS -o json > pod.json                     # keep a copy; the next restart overwrites lastState
kubectl get pod $POD -n $NS -o jsonpath='{range .status.initContainerStatuses[*]}{"init "}{.name}{" "}{.restartCount}{" "}{.state}{" last="}{.lastState.terminated}{"\n"}{end}{range .status.containerStatuses[*]}{.name}{" "}{.restartCount}{" "}{.state}{" last="}{.lastState.terminated}{"\n"}{end}'
kubectl logs $POD -n $NS -c <container> --previous > previous.log   # the container that crashed, not the one waiting
kubectl get events -n $NS --field-selector involvedObject.name=$POD --sort-by=.lastTimestamp
```

Do not delete the pod, scale the Deployment or edit probes until these files exist. Phase is not the answer: a crash-looping pod with `restartPolicy: Always` has phase `Running`.

## 2. Which container, which kind

| What the status shows | Where the problem is | Next |
|---|---|---|
| `Init:CrashLoopBackOff`, `Init:Error`, `Init:N/M` that never moves | the first init container that has not exited 0 | `kubectl logs $POD -c <init-container> --previous`; app containers have not started at all |
| `CrashLoopBackOff` with `lastState.terminated` | an app container exits by itself or is killed | step 3 on the exit code |
| `Running`, `READY 0/1`, `RESTARTS 0`, `Unhealthy` events saying `Readiness probe failed` | readiness probe; nothing restarts | step 5; the process is up but out of Service endpoints |
| `Killing` events saying `failed liveness probe, will be restarted` or `failed startup probe` | a probe kills a process that may be healthy | step 5 before step 4 |
| `CreateContainerConfigError`, `ImagePullBackOff`, `ErrImagePull`, `ContainerCreating` | the container never ran: a missing Secret or ConfigMap key, an image name, a volume | not a crash loop; read the event message |
| `StartError` / `RunContainerError`, exit code 128 | the runtime could not exec the command | wrong `command:` path, missing binary in the image, `exec format error` (wrong architecture) |

## 3. Read the exit code

```bash
kubectl get pod $POD -n $NS -o jsonpath='{.status.containerStatuses[?(@.name=="<container>")].lastState.terminated}{"\n"}'
```

| `exitCode` / `reason` | Means | Typical cause |
|---|---|---|
| `0` / `Completed` | the process finished successfully | a job-like process in a Deployment (`restartPolicy: Always` restarts exit 0 too); use a Job, or keep the process in the foreground |
| `1` / `Error` (or any small number) | the application exited on purpose | a missing environment variable, an unreachable dependency at start, a config parse error; `previous.log` has the message |
| `126` | found but not executable | permissions or a missing interpreter (`#!/bin/bash` in an Alpine image) |
| `127` | command not found | a typo in `command:`, a binary missing from a slim image, `command` used where `args` was meant |
| `128` / `StartError` | the runtime failed to start the process | as above, reported by containerd instead of a shell |
| `137` / `OOMKilled` | the kernel killed it for exceeding the memory limit | raise `resources.limits.memory` or fix the leak; check `kubectl top pod` and the node's `dmesg` |
| `137` / `Error` | SIGKILL from the kubelet | a liveness or startup kill, or the grace period expired on a process that ignores SIGTERM; check for `Killing` events |
| `139` | SIGSEGV | a native crash; a library or architecture mismatch |
| `143` | SIGTERM, and the process exited on it | it was stopped: a liveness kill, an eviction, a rollout, a node drain |

## 4. A process that crashes by itself

Read `previous.log` from the bottom. Then compare what the container gets with what it needs:

```bash
kubectl get pod $POD -n $NS -o jsonpath='{.spec.containers[?(@.name=="<container>")].env}{"\n"}{.spec.containers[?(@.name=="<container>")].command}{" "}{.spec.containers[?(@.name=="<container>")].args}{"\n"}'
kubectl debug $POD -n $NS -it --copy-to=$POD-debug --container=<container> -- sh   # same spec, a shell instead of the entrypoint; run the command by hand
kubectl delete pod $POD-debug -n $NS
```

If the crash depends on something outside the pod (a database, DNS, a Secret that rotated), the fix is a retry in the application or a readiness gate. Making the kubelet restart it faster does not help.

## 5. A probe that kills or excludes a healthy process

```bash
kubectl describe pod $POD -n $NS | grep -E 'Startup|Liveness|Readiness'      # http-get http://:8080/healthz delay=0s timeout=1s period=10s #success=1 #failure=3
kubectl get events -n $NS --field-selector involvedObject.name=$POD,reason=Unhealthy
kubectl exec $POD -n $NS -c <container> -- sh -c 'time wget -qO- http://127.0.0.1:8080/healthz'   # how long the endpoint really takes
```

| Finding | Fix |
|---|---|
| the liveness endpoint checks a database, cache or downstream API | liveness checks only the process; move the dependency into readiness |
| restarts happen during start-up, before the app listens | add a `startupProbe` with `failureThreshold × periodSeconds` above the slowest start you have seen; do not inflate `initialDelaySeconds` on liveness |
| `timeoutSeconds: 1` against an endpoint that takes longer under load | raise the timeout or make the endpoint cheap |
| liveness and readiness are the same probe | readiness should fail first (lower threshold), liveness only for a real hang |
| exec probe in an image without the tool (`curl` in distroless) | an `httpGet` or `tcpSocket` probe, which the kubelet runs from the node |
| `Readiness probe failed: HTTP probe failed with statuscode: 404` | wrong `path` or `port`, or a named port that does not exist |

Worst-case time to detect a hang is `periodSeconds × failureThreshold` after the last success. Write it down for the probe you propose.

## 6. Grace, shutdown and exit 137 on every deletion

If every rollout or deletion takes exactly `terminationGracePeriodSeconds` and ends in 137, the process is PID 1 without a SIGTERM handler or runs behind a shell that does not forward signals. Fix the entrypoint (`exec` the process from the script, handle SIGTERM, or add `tini`) rather than lowering the grace period. A `preStop` hook's time counts against the same grace period.

## 7. Static pods on a control-plane node

When the crashing pod is a mirror pod (`kubernetes.io/config.mirror` annotation, name ends in the node name), `kubectl` cannot fix it. Go to the node:

```bash
ls -la /etc/kubernetes/manifests/                         # hidden files are ignored; a broken file removes its pod
crictl ps -a --name kube-apiserver
crictl logs <container-id>
journalctl -u kubelet --no-pager -n 100 | grep -iE 'manifest|static|kube-apiserver'
```

Edit the manifest by copying it out, editing the copy, validating it (`python3 minikubelet.py validate file.yaml`, or `kubectl apply --dry-run=client -f file.yaml`) and moving it back in one `mv`. Keep a copy outside the directory before every change.

## 8. Back-off arithmetic

The kubelet restarts at once after the first crash, then waits 10s, 20s, 40s and so on up to 300s, and resets after 10 minutes of clean running. Recent releases can tune this curve with kubelet feature gates, so read the node's kubelet configuration before quoting the numbers. A pod at `RESTARTS 8` waits about five minutes between attempts, which is why "it fixed itself after I waited" often means the dependency came back during the 300s wait. Deleting the pod resets the curve and the evidence; it does not fix anything.

## 9. Report

Write five lines in the incident channel or ticket: the pod and container; `lastState.terminated` (exit code, reason, finishedAt) and the restart count; the cause in one sentence with the log line or event that proves it; the change made (code, config, probe, limit) and why it addresses the cause rather than the restart; and how you confirmed it (restartCount stable for ten minutes, `READY` at the expected count, no new `Unhealthy` or `BackOff` events). If you changed a probe, include the old and new `kubectl describe` lines and the worst-case detection time for both.
