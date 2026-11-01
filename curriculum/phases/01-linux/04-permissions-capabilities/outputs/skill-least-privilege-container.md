---
name: least-privilege-container
description: Derive the minimal capability set and ownership setup for a container, prove it with a negative test, and ship it as docker run flags or a Kubernetes securityContext
version: 1.0.0
phase: linux
lesson: linux/permissions-capabilities
tags: [linux, capabilities, permissions, docker, kubernetes, security, least-privilege, pod-security-standards]
---

# Least-privilege container

Use this when a container runs `privileged: true` or as root "because it did not work otherwise", when a review asks for `drop: [ALL]`, when a `Permission denied` or `Operation not permitted` appears after hardening, or when a setuid binary turns up in an image.

## 1. Read what it has now

```bash
CID=<container>; POD=<pod>
docker inspect -f '{{.Config.User}} caps+{{.HostConfig.CapAdd}} -{{.HostConfig.CapDrop}} {{.HostConfig.SecurityOpt}} privileged={{.HostConfig.Privileged}}' "$CID"
docker exec "$CID" grep -E 'Uid|CapEff|CapBnd|NoNewPrivs|Seccomp' /proc/1/status
kubectl get pod "$POD" -o jsonpath='{.spec.containers[*].securityContext}'; echo
kubectl exec "$POD" -- grep -E 'Uid|CapEff|CapBnd|NoNewPrivs|Seccomp' /proc/1/status
python3 capcheck.py <CapEff hex>            # or: capsh --decode=<hex>
```

Write down: uid, the decoded effective set, `NoNewPrivs`, `Seccomp`, privileged yes/no. `000001ffffffffff` is all 41 bits; `00000000a80425fb` is Docker's default 14; `0000000000000000` is `drop ALL`.

## 2. Start from zero and let the failures name the bit

```bash
docker run --rm --user 1000 --cap-drop ALL --security-opt no-new-privileges --read-only --tmpfs /tmp IMAGE
```

Run the workload's own smoke test against it. Each failure maps to exactly one change:

| Error | Caused by | Change (least privilege first) |
|---|---|---|
| `bind: Permission denied` (EACCES) on a port below 1024 | missing `CAP_NET_BIND_SERVICE` | listen above 1024 and map the port; else sysctl `net.ipv4.ip_unprivileged_port_start=0`; else a file capability on the binary |
| `open`/`write: Permission denied` on a file or volume | permission bits, not a capability | fix ownership in the image (`COPY --chown`), `fsGroup`, or run as the owning uid; never `chmod 777` |
| `chown: Operation not permitted` (EPERM) | missing `CAP_CHOWN` | chown at build time or in an initContainer, not at runtime |
| `ping`/raw sockets fail | missing `CAP_NET_RAW` | sysctl `net.ipv4.ping_group_range`, or drop the need |
| `su`, `gosu`, `s6-setuidgid`, `sudo` fail | missing `CAP_SETUID` + `CAP_SETGID`, or `NoNewPrivs` | start as the target user (`USER` in the Dockerfile, `runAsUser`) and remove the switch |
| `mount`, `sysctl -w`, `nsenter` fail | missing `CAP_SYS_ADMIN` | redesign: CSI volume, sidecar, or a dedicated privileged DaemonSet; `CAP_SYS_ADMIN` is root |
| `mknod` fails | missing `CAP_MKNOD` | device plugins or `--device`, never mknod in the container |
| `Bad system call` (SIGSYS) | seccomp, not capabilities | inspect the profile; `RuntimeDefault` rarely blocks real workloads |
| works as root, fails under `--user` even with `--cap-add` | non-root gets only the bounding set | file capability (`setcap`), or remove the need (see step 3) |

Add one capability per confirmed error, with `--cap-add` or `capabilities.add`, and record the error that required it. Anything that needs `CAP_SYS_ADMIN`, `CAP_SYS_PTRACE`, `CAP_SYS_MODULE`, `CAP_DAC_READ_SEARCH` or `CAP_SETFCAP` is a design problem, not a flag.

## 3. Non-root plus one capability

A non-root process gains effective capabilities only from the binary's file capabilities (masked by the bounding set) or from the ambient set. Docker and Kubernetes set neither. Options, in order:

1. Remove the need: a port above 1024, ownership fixed at build time, a sysctl.
2. File capability in the image: `setcap cap_net_bind_service=+ep /path/to/binary` in the Dockerfile, keep the bit in the bounding set (`--cap-add NET_BIND_SERVICE` with `--cap-drop ALL`), and leave `no-new-privileges` off for that container because it makes `execve` ignore the file capability. Document the trade.
3. Ambient capabilities via `capsh --keep=1 --user=app --inh=cap_net_bind_service --addamb=cap_net_bind_service -- -c CMD` as the entrypoint, or Podman, which sets ambient capabilities for non-root users when asked.

## 4. Freeze it

```bash
docker run --user 1000 --cap-drop ALL --cap-add NET_BIND_SERVICE --security-opt no-new-privileges --read-only --tmpfs /tmp IMAGE
```

```yaml
securityContext:
  runAsNonRoot: true
  runAsUser: 1000
  allowPrivilegeEscalation: false
  readOnlyRootFilesystem: true
  seccompProfile:
    type: RuntimeDefault
  capabilities:
    drop: ["ALL"]
```

Label the namespace `pod-security.kubernetes.io/enforce: restricted` so the next manifest cannot regress. PodSecurityPolicy was removed; Pod Security Admission is what enforces this now.

## 5. Prove it

```bash
docker exec "$CID" cat /proc/1/status > after.status; python3 capcheck.py --explain after.status
kubectl exec "$POD" -- cat /proc/1/status > after.status; python3 capcheck.py --explain after.status
```

Effective must equal the intended set, `NoNewPrivs` must be 1, `Seccomp` 2. Then the negative test: run the same image once more without the capability you kept and confirm the operation fails with `EACCES` or `EPERM`. A capability that makes no difference when removed is one you do not need.

## 6. Report

State: before (uid, decoded caps, privileged, NoNewPrivs), after (uid, decoded caps), each capability kept with the error that required it, what was fixed in the image or manifest instead of adding a capability, the negative test and its error, and the file or line changed. Never present `privileged: true` or `chmod 777` as a fix.
