---
name: pending-pod-triage
description: Find out why a pod is stuck in Pending by ruling out the cases that never reach the scheduler, splitting the FailedScheduling message into one reason per node, confirming each reason with one command, and naming the change that makes a node feasible
version: 1.0.0
phase: orchestration
lesson: orchestration/scheduler
tags: [kubernetes, scheduler, pending, taints, affinity, topology-spread, preemption, autoscaling, debugging]
---

# Pending pod triage

Use this when a pod stays `Pending`, a rollout stops with new pods not Running, a Job never starts, or someone proposes "add nodes" or "delete the pod" as the fix. Do not delete or recreate the pod during triage: the scheduler retries it on its own as soon as something relevant changes, and a new pod resets the evidence.

## 1. Is it the scheduler at all?

```bash
NS=<namespace>; POD=<pod>
kubectl -n $NS get pod $POD -o jsonpath='{.spec.schedulerName}{"\n"}{.spec.nodeName}{"\n"}{.spec.schedulingGates}{"\n"}{.status.conditions}{"\n"}{.status.nominatedNodeName}{"\n"}'
kubectl -n $NS get pvc
```

| What you see | Meaning | Next |
|---|---|---|
| `spec.nodeName` set, pod still Pending | already bound; the kubelet has not started it | not a scheduling problem: `kubectl describe pod` for image pulls, volumes, the node's `Ready` condition |
| STATUS `SchedulingGated`, `schedulingGates` non-empty | held before scheduling on purpose | find the controller that owns the gate (queueing systems such as Kueue add them) and why it has not removed it |
| no events at all, no `PodScheduled` condition | no scheduler picked it up | `schedulerName` names a profile nobody runs; compare with the profiles in the scheduler's `--config` |
| `pod has unbound immediate PersistentVolumeClaims` | a PVC is Pending | `kubectl -n $NS describe pvc`: StorageClass missing, provisioner down, or quota |
| `PodScheduled=False`, reason `Unschedulable` | filters rejected every node | go to step 2 |

## 2. Split the message, one reason per node

```bash
kubectl -n $NS get events --field-selector involvedObject.name=$POD,reason=FailedScheduling -o custom-columns=LAST:.lastTimestamp,MSG:.message
kubectl get nodes -o json > nodes.json && kubectl get pods -A -o json > pods.json
kubectl -n $NS get pod $POD -o json > pod.json
python3 minisched.py --nodes nodes.json --pods pods.json --pod pod.json      # per-node plugin, reason and numbers
```

`0/N nodes are available:` is followed by `COUNT REASON` pairs. Each node is counted under the **first** filter that failed (order: unschedulable, node name, taints, node affinity, ports, resources, volumes, topology spread, inter-pod affinity), so a node that shows a taint may also lack memory. Write the histogram down as a table before changing anything. The `preemption:` sentence after it is a second histogram: `Preemption is not helpful for scheduling` means the reason cannot be fixed by evicting pods; `No preemption victims found for incoming pod` means nothing on that node has a lower priority.

## 3. Confirm each reason, then name the fix

| Reason in the message | Confirm with | Fix |
|---|---|---|
| `Insufficient cpu` / `Insufficient memory` | `kubectl describe node NODE \| sed -n '/Allocated resources/,/Events/p'` and the pod's requests | lower the requests to measured usage plus headroom, remove pods that request but do not use, or add capacity of a shape that fits; never quote `kubectl top` as evidence of free room |
| `Too many pods` | `kubectl get node NODE -o jsonpath='{.status.allocatable.pods}'` | the kubelet's `maxPods` or the CNI's IP limit per node; larger nodes do not help if the limit is per node |
| `Insufficient nvidia.com/gpu` (any extended resource) | `kubectl get nodes -o custom-columns=NAME:.metadata.name,GPU:.status.allocatable.nvidia\.com/gpu` | the device plugin DaemonSet is missing or not ready on GPU nodes, or every GPU is requested |
| `node(s) had untolerated taint(s)` | `kubectl get nodes -o custom-columns=NAME:.metadata.name,TAINTS:.spec.taints` | add a toleration only if the pod belongs on those nodes; otherwise this is working as intended and the other reasons are the real ones |
| `node(s) were unschedulable` | `kubectl get nodes` shows `SchedulingDisabled` | someone cordoned for maintenance; `kubectl uncordon NODE` when it is finished |
| `node(s) didn't match Pod's node affinity/selector` | `kubectl get nodes -L KEY` for every key in `nodeSelector` and required affinity | fix the selector or label the nodes; `NotIn` and `DoesNotExist` match unlabelled nodes, `In` does not |
| `node(s) didn't match pod topology spread constraints` | count matching pods per domain: `kubectl get pods -l SELECTOR -o wide` against `kubectl get nodes -L topology.kubernetes.io/zone` | tainted or cordoned nodes count as empty domains unless `nodeTaintsPolicy: Honor`; consider `ScheduleAnyway` for the zone key, `matchLabelKeys: [pod-template-hash]` during rollouts, or capacity in the short zone |
| `(missing required label)` after the spread reason | nodes without the `topologyKey` label | label them or use a key every node carries |
| `node(s) didn't match pod anti-affinity rules` / `didn't satisfy existing pods anti-affinity rules` | replicas versus nodes per `topologyKey` | required anti-affinity on hostname caps replicas at the node count; use preferred anti-affinity or a spread constraint |
| `node(s) didn't have free ports for the requested pod ports` | `hostPort` in the pod spec and on pods already on the nodes | drop `hostPort` and use a Service, or one pod per node via a DaemonSet |
| `node(s) had volume node affinity conflict` / `didn't find available persistent volumes to bind` | `kubectl get pv -o custom-columns=NAME:.metadata.name,ZONE:.spec.nodeAffinity` | the volume lives in a zone with no feasible node; use `volumeBindingMode: WaitForFirstConsumer` or add capacity in that zone |

Fix the reason that covers the most nodes first, and check that those nodes do not have a second reason hidden behind the first: rerun `minisched` with the proposed change (edit `pod.json` or `nodes.json`) before touching the cluster.

## 4. Can preemption or an autoscaler help?

```bash
kubectl -n $NS get pod $POD -o jsonpath='{.spec.priority} {.spec.priorityClassName} {.spec.preemptionPolicy}{"\n"}'
kubectl get priorityclass
kubectl -n $NS get events --field-selector involvedObject.name=$POD | grep -E 'TriggeredScaleUp|NotTriggerScaleUp' || true
kubectl get nodeclaims 2>/dev/null || true                   # Karpenter
```

| Situation | Verdict |
|---|---|
| reasons are resources only, pod priority above other workloads | preemption will evict lower-priority pods; watch `status.nominatedNodeName` and `Preempted` events on the victims; check PodDisruptionBudgets |
| reasons are resources only, autoscaler present | a scale-up should follow; `NotTriggerScaleUp` names the filter a new node would still fail |
| any taint, affinity, label or spread reason | neither preemption nor a new node of the same shape helps; change the pod or the node shape |
| `preemptionPolicy: Never` | the pod waits for room; nothing will be evicted for it |

## 5. Verify

After the change, do not recreate the pod. Wait for the scheduler to requeue it:

```bash
kubectl -n $NS wait --for=condition=PodScheduled pod/$POD --timeout=120s
kubectl -n $NS get pod $POD -o wide
```

If it stays Pending, read the newest FailedScheduling event again: a different histogram means the first reason is fixed and a second one is now first.

## 6. Report

State: the full FailedScheduling message; the histogram as a table (count, reason, plugin, whether preemption could help); for the reason you acted on, the confirming command output and the change; any reason hidden behind it; whether preemption or an autoscaler was involved and what it did; and the time from the change to `PodScheduled=True`. Never list "deleted the pod" or "restarted the scheduler" as a resolution.
