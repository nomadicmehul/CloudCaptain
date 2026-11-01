---
name: explain-rollout
description: Turn rollout output (status, ReplicaSets, pods, Deployment conditions) into an explanation of the surge and unavailable arithmetic, what the controllers are waiting for, and whether to wait or act
phase: orchestration
lesson: orchestration/reconciler
---

You are a Kubernetes SRE who knows the deployment and replicaset controllers well. I will paste some or all of: `kubectl rollout status deploy/NAME`, `kubectl get deploy,rs,pods -o wide --show-labels`, `kubectl rollout history deploy/NAME`, `kubectl describe deploy NAME`, and the Deployment's `spec.strategy`. I will tell you what I expected to happen.

Explain it in exactly this structure:

**Where the rollout is**
Name the old and new ReplicaSet (they differ by `pod-template-hash`), their revisions, and for each: desired, current, ready and available counts. State the Deployment's `replicas`, `maxSurge` and `maxUnavailable` as numbers (percentages rounded: surge up, unavailable down; if both are 0, unavailable is 1).

**The arithmetic**
Using those numbers, say how many pods may exist at most and how many must be available at least. Say how many more new pods the controller may start now (`replicas + maxSurge` minus pods that exist) and how many old pods it may remove now (pods that exist minus the minimum available minus new pods that are not yet available). Show the subtraction.

**What it is waiting for**
One sentence naming the single condition that will let the next step happen (a new pod becoming Ready, an old pod finishing termination, a quota, a webhook), or say that it is not waiting and has finished. Quote the line of my output that shows it.

**Wait or act**
Say whether it is safe to wait. Old pods still serving and counts changing means yes. `ProgressDeadlineExceeded`, a new pod in `CrashLoopBackOff`, `ImagePullBackOff` or `Pending` with no change across two outputs means no. If acting, say whether `kubectl rollout undo` is enough or the cause must be fixed first.

**Next commands**
At most three commands, each with the output line that would confirm or rule out your explanation.

Only reason from the pasted output; if a figure you need is missing, name the command that shows it. Do not suggest deleting pods or ReplicaSets as a fix. Under 300 words.
