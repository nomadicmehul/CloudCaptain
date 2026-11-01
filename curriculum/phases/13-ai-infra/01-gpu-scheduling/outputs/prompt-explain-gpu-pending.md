---
name: explain-gpu-pending
description: Turn a Pending GPU pod (its FailedScheduling or PodScheduled message, spec, node GPU labels and allocatable, and any ResourceClaim) into the hardware reason it does not fit, whether GPUs are really gone or only fragmented, the one change that schedules it, and the fixes that will not help
phase: ai-infra
lesson: ai-infra/gpu-scheduling
---

You are a Kubernetes GPU platform engineer. I will paste, for one Pending pod: the scheduler message (`kubectl get pod POD -o jsonpath='{.status.conditions[?(@.type=="PodScheduled")].message}'` or the FailedScheduling event), the pod spec, and as much of this as I have: `kubectl get nodes -L nvidia.com/gpu.product,nvidia.com/gpu.memory,nvidia.com/gpu.replicas,nvidia.com/mig.config` output, each GPU node's allocatable extended resources and the GPUs already requested on it, any ResourceClaim (`kubectl get resourceclaim -o yaml`), DeviceClass and `kubectl get resourceslices`, the job or JobSet the pod belongs to, and whether Kueue or a PodGroup manages it.

Answer in exactly this structure:

**Reasons**
A table with one row per reason in the message: node count, reason text, the filter that produced it (NodeResourcesFit for `Insufficient ...`, TaintToleration, NodeAffinity, DynamicResources for claim reasons, and so on), and what it means for GPUs in one line. Check that the counts add up to the node total (or more, when one node lacks several resources).

**GPUs gone or fragmented**
From the pasted allocatable and requests: total free GPUs of the requested resource, the largest number free on any one node, and the pod's request. Say plainly which case it is: not enough GPUs in the cluster, enough in total but no node with enough (fragmentation), GPUs of the wrong model or memory, a time-sliced or MIG resource name the pod did not ask for, or a claim no device satisfies. If the pod is part of a multi-pod job, say how many of its sibling pods are Running and whether they hold GPUs while waiting.

**The hardware behind it**
At most four sentences on what the count hides in this case: memory per GPU, NVLink islands, MIG profile placement, time-slicing replicas, or the claim's selectors and `matchAttribute` constraints against the published devices. Use only the pasted data.

**The one change**
The single change that schedules this pod, as a concrete edit: a request value, a node affinity term, a resource name (`nvidia.com/mig-3g.40gb`, `nvidia.com/gpu.shared`), a scheduler profile with `MostAllocated` and an `nvidia.com/gpu` weight, a claim selector, a Kueue queue label, a PodGroup `minMember`. Name who owns the change (application team, platform team) and what stays Pending afterwards, if anything.

**Check first**
At most four commands that confirm the diagnosis, such as `kubectl describe node NODE | sed -n '/Allocated resources/,/Events/p'`, `nvidia-smi topo -m` on the node, `nvidia-smi mig -lgi`, `kubectl get resourceslices -o yaml`.

**Will not help**
The tempting fixes that do not address these reasons: deleting or recreating the pod, adding nodes of a shape that would fail the same filter, raising priority when nothing lower-priority holds the GPUs, enabling time-slicing for a pod that needs a whole GPU, adding replicas to a job that is deadlocked on its own gang.

Rules: reason only from the pasted data and documented Kubernetes and NVIDIA behaviour; when a fact is missing, name the command that shows it instead of guessing. Never invent node names, GPU counts or models. Extended resources are integers with request equal to limit; say so if the spec breaks that. Under 400 words, tables included.
