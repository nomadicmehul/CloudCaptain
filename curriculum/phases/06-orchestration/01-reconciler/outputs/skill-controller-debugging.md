---
name: controller-debugging
description: Find out why Kubernetes did not do what a Deployment, ReplicaSet or Pod spec asked, by locating the controller that owns the symptom, checking it has seen the latest spec, reading what it is blocked on, and ruling out orphaned selectors and two writers fighting over one field
version: 1.0.0
phase: orchestration
lesson: orchestration/reconciler
tags: [kubernetes, controllers, deployments, replicasets, rollout, ownerreferences, garbage-collection, debugging]
---

# Controller debugging

Use this when a rollout never finishes, a deleted pod is not replaced, a replica count keeps changing, an image reverts, a delete hangs, or someone says "Kubernetes ignored my change". Do not delete and recreate objects during triage: the evidence (events, conditions, owner references, the managed-fields record) lives on the objects, and a new object starts with none of it.

The model to keep in your head: every controller is a loop that compares the object's `spec` with what exists and writes the difference, then writes `status`. A symptom is one loop that has not run, ran and was refused, or ran and was undone by another writer.

## 1. Find the layer that owns the symptom

```bash
NS=<namespace>; APP=<deployment-name>
kubectl -n $NS get deploy $APP -o jsonpath='{.spec.selector.matchLabels}{"\n"}'      # the selector, use it below as -l
kubectl -n $NS get deploy,rs,pods -l app=$APP -o wide --show-labels
kubectl -n $NS get deploy $APP -o jsonpath='gen={.metadata.generation} observed={.status.observedGeneration} replicas={.status.replicas} updated={.status.updatedReplicas} ready={.status.readyReplicas} available={.status.availableReplicas}{"\n"}'
```

| What you see | Loop at fault | Go to |
|---|---|---|
| `generation` is higher than `observedGeneration` | the Deployment controller has not processed the latest spec | step 2 |
| spec changed but no new ReplicaSet, or the old one is not shrinking | the Deployment controller ran and is blocked, or the rollout is paused | step 3 |
| a ReplicaSet has `DESIRED` above `CURRENT` | the ReplicaSet controller cannot create pods | step 4 |
| the right number of pods exists but they are not Ready | not a controller problem: scheduling, image pull, probes | `kubectl describe pod`; the scheduler and kubelet lessons |
| pod counts are right now but change again within minutes | two writers | step 6 |
| the pod or ReplicaSet exists with no owner, or two owners claim it | selector and ownership | step 5 |
| delete hangs | finalizers or foreground deletion | step 7 |

## 2. Has the controller seen the latest spec?

If `observedGeneration` lags `generation` for more than a few seconds, nothing is processing the object. On a cluster you run:

```bash
kubectl -n kube-system get pods -l component=kube-controller-manager
kubectl -n kube-system get lease kube-controller-manager -o jsonpath='{.spec.holderIdentity} renewed={.spec.renewTime}{"\n"}'
kubectl -n kube-system logs -l component=kube-controller-manager --tail=40
kubectl get --raw='/readyz?verbose' | grep -v ok || true
```

A lease whose `renewTime` is old means no controller manager holds leadership. On a managed service (EKS, GKE, AKS) the control plane is not visible; check the provider's control plane status and API server latency, then look for admission webhooks that time out and slow every write.

## 3. Read the conditions

```bash
kubectl -n $NS get deploy $APP -o jsonpath='{range .status.conditions[*]}{.type}={.status} {.reason}: {.message}{"\n"}{end}'
kubectl -n $NS rollout status deploy/$APP --timeout=10s
kubectl -n $NS get deploy $APP -o jsonpath='paused={.spec.paused} maxSurge={.spec.strategy.rollingUpdate.maxSurge} maxUnavailable={.spec.strategy.rollingUpdate.maxUnavailable} deadline={.spec.progressDeadlineSeconds}{"\n"}'
```

| Condition and reason | Meaning | Action |
|---|---|---|
| `Progressing=False`, `ProgressDeadlineExceeded` | the counts did not move for `progressDeadlineSeconds`; the controller stops waiting but does not undo anything | find why the new pods are not becoming Ready (step 4 and `describe pod`), then `kubectl rollout undo` or fix forward; old pods are still serving |
| `Progressing=True`, `ReplicaSetUpdated` | a rollout is in progress | read the surge and unavailable limits and the new ReplicaSet's available count; wait if the numbers are moving |
| `Progressing=True`, `NewReplicaSetAvailable` | finished | the symptom is elsewhere |
| `Available=False`, `MinimumReplicasUnavailable` | fewer pods available than replicas minus `maxUnavailable` | compare with the previous revision: `kubectl rollout history deploy/$APP`; a rollout that dipped here had limits that allowed it |
| `ReplicaFailure=True` | the ReplicaSet could not create pods | step 4 |
| `spec.paused: true` | rollouts are held on purpose | `kubectl rollout resume deploy/$APP` if nobody owns the pause |

The rollout arithmetic: it may hold at most `replicas + maxSurge` pods and must keep at least `replicas - maxUnavailable` available. Percentages round up for surge and down for unavailable, and if both come out as zero, unavailable becomes 1. A rollout that makes no progress with these limits and an unready new pod is the limits working.

## 4. The ReplicaSet and its pods: why the difference is not closing

```bash
RS=$(kubectl -n $NS get rs -l app=$APP -o jsonpath='{range .items[?(@.spec.replicas>0)]}{.metadata.name}{"\n"}{end}' | head -1)
kubectl -n $NS describe rs $RS | sed -n '/Events:/,$p'
kubectl -n $NS get events --field-selector reason=FailedCreate -o custom-columns=LAST:.lastTimestamp,MSG:.message
```

| Event message contains | Cause | Fix |
|---|---|---|
| `exceeded quota` | a `ResourceQuota` in the namespace is full | `kubectl -n $NS describe quota`; raise it or free requests |
| `violates PodSecurity` | Pod Security admission for the namespace labels | change the pod's `securityContext` to meet the level, not the label |
| `admission webhook ... denied the request` or `failed calling webhook` | a policy engine or a broken webhook | read the webhook message; `kubectl get validatingwebhookconfigurations` for one with `failurePolicy: Fail` and no endpoints |
| `serviceaccount "x" not found` | the pod template names a ServiceAccount that does not exist | create it or fix the name |
| `pods "x" is forbidden` | RBAC or `LimitRange` limits | `kubectl auth can-i create pods -n $NS --as=system:serviceaccount:kube-system:replicaset-controller` for RBAC; `kubectl describe limitrange` for limits |
| no events and `DESIRED` = `CURRENT` | pods exist and are not ready | not a controller problem; `kubectl describe pod` |

## 5. Selectors and ownership

```bash
kubectl -n $NS get pods --show-labels
kubectl -n $NS get pod $POD -o jsonpath='{.metadata.ownerReferences}{"\n"}'
kubectl -n $NS get rs,deploy,sts,ds -o custom-columns=KIND:.kind,NAME:.metadata.name,SELECTOR:.spec.selector.matchLabels
```

| Finding | Meaning | Action |
|---|---|---|
| a pod with no `ownerReferences` whose labels match a ReplicaSet's selector | an orphan; the ReplicaSet adopts it and counts it | expected after `--cascade=orphan`; otherwise find who removed the owner |
| a running pod that no ReplicaSet counts, labels edited | someone quarantined it (`kubectl label pod X app-`), the ReplicaSet created a replacement | inspect it, then delete it by hand; it will not be cleaned up |
| two controllers with overlapping selectors | each counts pods the other made; controllers adopt only pods that have no controller owner, so they fight over orphans | give every workload a unique label, and never reuse a Deployment's selector for another workload; `spec.selector` of a Deployment is immutable |
| a Service with endpoints missing for a healthy pod | the Service selects by labels, so a relabelled pod leaves it | restore the label or accept the quarantine |
| ReplicaSets with `0` replicas piling up | `revisionHistoryLimit` keeps them as rollback targets | lower the limit only if you accept fewer revisions to undo to |

## 6. Two writers on one field

Symptoms: replicas reset after a manual scale, an image reverts, `metadata.generation` climbs with nobody editing, `409 Conflict` lines in a controller's log at a steady rate (an occasional one is normal and is retried).

```bash
kubectl -n $NS get deploy $APP -o yaml --show-managed-fields | sed -n '/managedFields:/,$p' | grep -E 'manager:|operation:|time:|f:replicas|f:image'
kubectl -n $NS get deploy $APP -w -o custom-columns=GEN:.metadata.generation,REPLICAS:.spec.replicas,IMAGE:.spec.template.spec.containers[0].image
```

| Pair of writers | What happens | Fix |
|---|---|---|
| HorizontalPodAutoscaler and `replicas:` in the manifest | every apply resets the autoscaled count | remove `replicas` from the manifest; in Argo CD or Flux ignore the field |
| GitOps with self-heal and `kubectl edit` or `kubectl scale` | the sync reverts the manual change within minutes | change Git, or suspend self-heal for the incident |
| Helm upgrade and `kubectl set image` | the next upgrade resets the image | one owner for the field; values for Helm, nothing else |
| an operator and a human editing a child it owns | the operator rewrites the child on its next reconcile | change the parent resource; the child is not yours |

The rule: a field has one owner. `managedFields` names the managers; a field that lists two is the bug.

## 7. Stuck deletion

```bash
kubectl -n $NS get $KIND $NAME -o jsonpath='deletionTimestamp={.metadata.deletionTimestamp} finalizers={.metadata.finalizers}{"\n"}'
kubectl -n $NS get all -o json | python3 -c 'import json,sys; [print(i["kind"], i["metadata"]["name"], i["metadata"].get("finalizers")) for i in json.load(sys.stdin)["items"] if i["metadata"].get("deletionTimestamp")]'
```

An object with a `deletionTimestamp` and a finalizer is waiting for its controller to finish cleanup. If that controller is down or broken, repair it first. A foreground delete (`--cascade=foreground`) waits for children that block it with `blockOwnerDeletion`. Remove a finalizer by hand only after you know what it protects.

## 8. If the change stream itself is suspect

```bash
RV=$(kubectl -n $NS get pods -o jsonpath='{.metadata.resourceVersion}')
kubectl get --raw "/api/v1/namespaces/$NS/pods?watch=1&resourceVersion=$RV&timeoutSeconds=10"
```

Change something in the namespace from another terminal. If a line appears, the API server is publishing changes and the controller is the problem. If nothing does, suspect the API server, etcd or a webhook. A `410` or `too old resource version` in a controller log is a relist and is harmless unless it repeats every few seconds.

## 9. Report

State: the object chain (Deployment, ReplicaSet, pods) with desired, current, ready and available counts; `generation` against `observedGeneration`; the condition that explains the stall with its reason; the event or log line that names the blocker; whether selectors, owners or a second writer are involved; what you changed or recommend and why it will close the gap; and whether it is safe to wait (old pods serving, numbers moving) or needs action now. Do not recommend deleting pods as a fix unless you have shown the owning controller will recreate them and the cause is in the pod, not the controller.
