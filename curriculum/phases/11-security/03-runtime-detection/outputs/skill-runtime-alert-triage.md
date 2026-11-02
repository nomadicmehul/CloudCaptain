---
name: runtime-alert-triage
description: Triage a runtime detection alert (Falco, Tetragon or minifalco) by reading its fields, reconstructing the process tree and sequence of calls, deciding true positive versus tuned-out noise, containing without destroying evidence, and ending with either an incident or a documented exception
version: 1.0.0
phase: security
lesson: security/runtime-detection
tags: [security, runtime-detection, falco, incident, triage, ebpf, tuning, containers]
---

# Runtime alert triage

Use this when a runtime sensor (Falco, Tetragon, or your own `minifalco`) raises an alert: a shell in a container, a read of a sensitive file, an execve from a world-writable directory, an unexpected outbound connection, a touch of the container runtime socket. The alert is a camera, not a wall: it tells you a call happened, not that it was stopped. Your job is to decide whether it is real, contain it if it is, and tune it if it is not, without destroying the evidence.

## 1. Read the alert as one sentence

Every rule output is a template with `%fields`. Pull the five that matter into a single line before anything else:

```
what:      <rule name> (<priority>)
when:      <timestamp>
where:     container=<container.id/name> image=<container.image.repository> node=<k8s.node / host>
who:       proc=<proc.name> parent=<proc.pname> cmdline=<proc.cmdline> user=<user.name> uid=<user.uid>
on what:   file=<fd.name> / dest=<fd.sip>:<fd.sport>
```

A critical-priority alert with `container.id=host` is the host itself, not a pod, and raises the stakes. `proc.pname` and the command line usually say more than the rule name.

## 2. Is it a real detection or tuned-out noise?

| Signal | Reading | Next |
|---|---|---|
| `proc.name` is a known-good tool for this path (`apt`, your backup agent, a package manager) | likely a false positive the rule should exclude | go to step 5, add an exception; do not page |
| The rule has fired thousands of times across many workloads | untuned rule; the one firing now is drowned | tune first (step 5), then re-examine |
| `cmdline` shows interactive use (`bash -i`, `sh` with a tty) in a service container that never gets a shell | real: nobody should be in that container | go to step 3 |
| The alert is a single rule, but the same pod shows a chain (shell, then shadow read, then `/tmp` execve, then connect) | real and in progress | go to step 3, then step 4 now |
| Priority is NOTICE and the context is a CI or build namespace | expected behaviour in that namespace | scope an exception by namespace |

## 3. Reconstruct the tree and the sequence

A single alert is one call; the attack is the sequence around it. Pull the context live before the pod restarts and erases it:

```bash
NS=<namespace>; POD=<pod>; CID=<container.id>
kubectl -n $NS get pod $POD -o wide                                   # node, status, restarts
kubectl -n $NS describe pod $POD | sed -n '/Events/,$p'               # recent restarts, OOMs, image
# on the node (or: kubectl debug node/<node> -it --image=busybox):
ps -o pid,ppid,user,args --ppid $(pgrep -f $CID) 2>/dev/null         # the process tree under the container
ls -la /proc/<pid>/cwd /proc/<pid>/exe                               # where the binary lives and ran from
cat /proc/<pid>/status | grep -E 'Uid|CapEff|Seccomp|NoNewPrivs'     # what prevention actually applied
ss -tnp | grep <pid>                                                 # its live connections
```

Correlate with the other sensors: `kubectl get events`, the audit log for a matching `exec` subresource, and the deploy timeline. A shell at 02:14 with no deploy and no human on call is not maintenance.

## 4. Contain without destroying evidence

Order matters: capture first, then stop, so the postmortem has something to read.

1. Snapshot: `kubectl -n $NS cp $POD:/tmp /safe/tmp-$POD` for dropped files, `kubectl -n $NS logs $POD --previous`, and save the alert JSON. If the sensor can, capture a short syscall trace of the offending pid.
2. Cut the blast radius: apply a default-deny `NetworkPolicy` to the pod (stops the outbound leg and the Docker-socket pivot) before you kill anything.
3. Rotate what the pod could read: the mounted service account token, any secret in its env or volumes. A shell inside a pod has already read them; hardening did not stop that.
4. Only then remove the workload: `kubectl -n $NS delete pod $POD` (the ReplicaSet makes a clean one) or cordon the node if the host is involved. Do not `docker exec` into the compromised container to "look around"; you are adding events and risk.

## 5. Tune a false positive (the usual outcome)

Most alerts in a new deployment are the rule meeting legitimate behaviour. Do not delete the rule; carve out the known-good case and leave it active for everything else.

```bash
# count what is really firing before you touch anything:
falco -L | grep -A3 "<rule name>"                                    # read the condition and its user_known_ hook
python3 minifalco.py tune events.json                                # or your own counts per rule and process
```

- Falco: override the rule's `user_known_...` macro, or add an `exceptions:` entry keyed on the benign `proc.name`, image or namespace. Scope it as tightly as the evidence allows (one process, one path), not a blanket off switch.
- Re-run in audit and confirm the benign case is quiet and a synthetic bad case still fires. Only then promote to a channel people watch, and only wire an automated action (kill, isolate) once the rule has been quiet in audit for a known period.

## 6. Report

State: the alert as the one-line what/when/where/who/on-what; the verdict (true positive, false positive, or inconclusive) with the evidence that decided it; the sequence of calls you reconstructed; for a true positive, what you captured, what you contained and in which order, and what you rotated; for a false positive, the exact exception you added and why it is safe; and a coverage note (was every node running the sensor, and was the probe healthy). Never close an alert as "pod restarted, gone" without a verdict.
