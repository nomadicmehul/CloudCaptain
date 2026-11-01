---
name: container-hardening-review
description: Review what a running container or a pod manifest lets a compromised process do, fix each gap with the least-privilege flag or field, prove it with negative tests, and roll Pod Security Admission out without breaking workloads
version: 1.0.0
phase: containers
lesson: containers/runtime-security
tags: [containers, docker, kubernetes, seccomp, capabilities, pod-security-standards, hardening, security]
---

# Container hardening review

Use this when a security review asks for "least privilege", when a manifest has no `securityContext`, when a namespace is about to be labelled `pod-security.kubernetes.io/enforce=restricted`, or after an incident where a shell inside a container did more than it should have. The question throughout is: once the process is compromised, what can it still call, hold and write?

## 1. Inventory what it has today

```bash
CID=<container>; POD=<pod>; NS=<namespace>
docker inspect "$CID" | python3 hardencheck.py inspect -                   # 13 checks, a score, the docker run line that would pass
docker exec "$CID" grep -E 'Uid|CapEff|CapBnd|NoNewPrivs|Seccomp' /proc/1/status
docker info --format '{{.SecurityOptions}}'                                 # seccomp profile=builtin? apparmor? rootless? userns?
kubectl get pod "$POD" -n "$NS" -o json | python3 hardencheck.py k8s -      # admitted or REJECTED under restricted, with the admission messages
kubectl exec "$POD" -n "$NS" -- grep -E 'Uid|CapEff|NoNewPrivs|Seccomp' /proc/1/status
```

Write down the score, every FAIL, and the three `/proc/1/status` values: `CapEff` (`0000000000000000` is drop ALL), `NoNewPrivs` (1 is good), `Seccomp` (2 means a filter is installed, 0 means none). `inspect` shows what was asked for; `/proc/1/status` shows what was granted.

## 2. Fix each finding, least privilege first

Work top to bottom. Each row names the one error the fix can cause and what to do instead of reverting.

| Finding | Docker | Kubernetes | If it breaks |
|---|---|---|---|
| runs as root | `--user 1000` | `runAsNonRoot: true`, `runAsUser: 10001` | `Permission denied` on the app's own files: fix ownership in the image (`COPY --chown`), or `fsGroup` for volumes; never `chmod 777` |
| privileged | remove `--privileged` | remove `privileged: true` | find the one thing it needed: a `--device`, a sysctl, a capability; `CAP_SYS_ADMIN` means redesign (CSI driver, DaemonSet, sidecar) |
| default or added capabilities | `--cap-drop ALL`, then one `--cap-add` per proven `EPERM` | `capabilities: {drop: [ALL]}`; restricted allows adding only `NET_BIND_SERVICE` | `bind: Permission denied` on a low port: listen above 1024, or `net.ipv4.ip_unprivileged_port_start=0` |
| no no_new_privs | `--security-opt no-new-privileges` | `allowPrivilegeEscalation: false` | `su`, `sudo`, `gosu` stop working: start as the target user instead |
| writable root | `--read-only --tmpfs /tmp:rw,noexec,nosuid,size=64m` | `readOnlyRootFilesystem: true` plus an `emptyDir` at each write path | `Read-only file system` at a path: mount a tmpfs or emptyDir there; the error names the path for you |
| seccomp unconfined | drop `seccomp=unconfined`; default profile, or `seccomp=profile.json` from a trace | `seccompProfile: {type: RuntimeDefault}` at pod level | `Operation not permitted` with no capability involved, or `Bad system call`: see step 3 |
| sensitive mount | remove it; Docker API through a socket proxy with an allow-list; mount only the sub-path, `:ro` | remove `hostPath`; PVC, `configMap`, `emptyDir` | a sidecar needs the API: give it a proxy and a token with the two endpoints it uses |
| host namespaces | drop `--pid=host`, `--network=host`, `--ipc=host` | drop `hostPID`, `hostNetwork`, `hostIPC`, `hostPort` | ports through `-p` or a Service; node agents are a DaemonSet with an explicit exception |
| no limits | `--pids-limit 100 --memory 256m` | `resources.limits.memory`, `cpu` | `OOMKilled`: the limit was below real use; measure with `docker stats` or `kubectl top` and set it 20 percent above |
| token mounted | n/a | `automountServiceAccountToken: false` | the pod really talks to the API server: give it a dedicated ServiceAccount with a minimal Role instead |

Add one change, run the workload's own smoke test, record the error if any, apply the row's alternative. Never answer an error by reverting to the previous row.

## 3. Build and prove a seccomp profile

```bash
docker run --rm --cap-add SYS_PTRACE IMAGE sh -c 'apk add -q strace && strace -f -qq -o /tmp/t.log CMD >/dev/null && cat /tmp/t.log' > trace.log   # apt-get install strace on Debian images
python3 hardencheck.py generate trace.log --out profile.json
python3 hardencheck.py seccomp profile.json --check mkdir,mount,ptrace,unshare           # what the workload may and may not call
docker run --rm --security-opt seccomp=profile.json IMAGE CMD                            # must work
docker run --rm --security-opt seccomp=profile.json IMAGE mkdir /tmp/x                   # must fail with Operation not permitted
```

Trace inside the image, with the real command and the real test suite; a laptop trace is a trace of a different libc. For a service you cannot afford to break, edit `defaultAction` to `SCMP_ACT_LOG`, run staging under it, read `dmesg | grep SECCOMP` (or `journalctl -k`) for syscall numbers, add them, then set `SCMP_ACT_ERRNO`. In Kubernetes, ship it as a `Localhost` profile under the kubelet's seccomp directory or keep `RuntimeDefault`.

## 4. Negative tests before the change is called done

```bash
docker exec "$CID" sh -c 'touch /usr/x; echo "rootfs write exit $?"'                     # non-zero
docker exec "$CID" sh -c 'cat /proc/1/status | grep -E "CapEff|NoNewPrivs|Seccomp"'      # 0000000000000000, 1, 2
docker run --rm --user 1000 --cap-drop ALL IMAGE CMD-THAT-BINDS-80                       # fails: proves the capability you removed mattered
kubectl apply -f violating.yaml -n "$NS"                                                 # Error ... violates PodSecurity "restricted:latest"
```

A control that makes no difference when removed is one you have not tested. Each negative test goes in the ticket with its exact error.

## 5. Roll Pod Security Admission out

```bash
kubectl label --dry-run=server --overwrite ns "$NS" pod-security.kubernetes.io/enforce=restricted    # lists every existing pod that would be rejected; changes nothing
kubectl label ns "$NS" pod-security.kubernetes.io/warn=restricted pod-security.kubernetes.io/audit=restricted
kubectl get deploy,sts,ds,job,cronjob -n "$NS" -o json | python3 hardencheck.py k8s -                 # fix the REJECTED ones; --fix prints the patch
kubectl patch deploy NAME -n "$NS" --type strategic -p "$(kubectl get deploy NAME -n "$NS" -o json | python3 hardencheck.py k8s - --fix)"
kubectl label ns "$NS" pod-security.kubernetes.io/enforce=restricted                                  # only new pods are checked; running ones keep running
```

Namespaces that legitimately need `baseline` or `privileged` (CNI, storage drivers, node agents) get that label and a comment naming why. PodSecurityPolicy was removed; if the cluster still carries PSP manifests, they do nothing and should be deleted. Rules beyond the standards (allowed registries, a required seccomp profile name) belong to Kyverno or Gatekeeper, beside Pod Security Admission, not instead of it.

## 6. Report

State: the score before and after, the `/proc/1/status` triple before and after, every finding with the fix applied and the error it caused (or "none"), every negative test with its exact error, the profile's syscall counts (traced, added, denied canary), the namespace's three labels, and which items are the restricted standard versus the extras (`readOnlyRootFilesystem`, limits, token). Never report `privileged: true`, `seccomp=unconfined`, `chmod 777` or a container socket mount as a fix.
