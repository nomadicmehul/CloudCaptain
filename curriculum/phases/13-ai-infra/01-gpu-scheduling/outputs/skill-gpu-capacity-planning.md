---
name: gpu-capacity-planning
description: Review a Kubernetes GPU pool by inventorying what the scheduler cannot see (model, memory, NVLink islands, MIG layouts, time-slicing), measuring fragmentation per job size, checking scoring, sharing and gang admission against the workload mix, and recommending the smallest change that lets the next large job run
version: 1.0.0
phase: ai-infra
lesson: ai-infra/gpu-scheduling
tags: [kubernetes, gpu, scheduling, nvidia, mig, time-slicing, dra, kueue, gang-scheduling, fragmentation, capacity]
---

# GPU capacity planning

Use this when GPU jobs wait while dashboards show idle GPUs, before buying more GPU nodes, when a team asks for a large training slot, after moving to MIG or time-slicing, or as a quarterly review of a GPU pool. Work read-only until step 6. Do not cordon, relabel or repartition anything during the review.

## 1. Inventory what the scheduler cannot see

```bash
kubectl get nodes -L nvidia.com/gpu.product,nvidia.com/gpu.memory,nvidia.com/gpu.count,nvidia.com/gpu.replicas,nvidia.com/mig.config,nvidia.com/mig.strategy
kubectl get nodes -o custom-columns='NAME:.metadata.name,GPU:.status.allocatable.nvidia\.com/gpu,TAINTS:.spec.taints[*].key'
kubectl get nodes -o json | python3 -c 'import json,sys; [print(n["metadata"]["name"], {k: v for k, v in n["status"]["allocatable"].items() if "/" in k}) for n in json.load(sys.stdin)["items"]]'
kubectl get resourceslices -o wide 2>/dev/null || true       # DRA drivers, if any
kubectl get deviceclasses 2>/dev/null || true
```

On one node of each shape, read the hardware itself (from a debug pod with the GPU, or the node):

```bash
nvidia-smi --query-gpu=index,name,memory.total --format=csv
nvidia-smi topo -m                 # NV# between GPUs = NVLink; PIX/PXB/PHB/SYS = PCIe paths
nvidia-smi mig -lgi                # existing MIG instances, if MIG is on
```

Write one row per node shape: model, memory per GPU, GPUs per node, NVLink islands (from `topo -m`), sharing mode (exclusive, time-sliced with N replicas, MIG layout), resource names advertised, node count. This table is the input to everything below; if a column is unknown, say so in the report instead of assuming.

## 2. Measure fragmentation from live allocations

```bash
kubectl get pods -A -o json > pods.json && kubectl get nodes -o json > nodes.json
python3 - <<'PY'
import json
nodes = {n["metadata"]["name"]: int(n["status"]["allocatable"].get("nvidia.com/gpu", 0)) for n in json.load(open("nodes.json"))["items"]}
used = {}
for p in json.load(open("pods.json"))["items"]:
    node = p["spec"].get("nodeName")
    if not node or p["status"].get("phase") in ("Succeeded", "Failed"):
        continue
    for c in p["spec"]["containers"]:
        r = c.get("resources") or {}
        n = int((r.get("requests") or {}).get("nvidia.com/gpu") or (r.get("limits") or {}).get("nvidia.com/gpu") or 0)
        used[node] = used.get(node, 0) + n
free = {n: nodes[n] - used.get(n, 0) for n in nodes if nodes[n]}
total = sum(free.values())
print("free per node:", free)
for size in (1, 2, 4, 8):
    fit = sum(f // size for f in free.values())
    print(f"size {size}: {fit} pods fit, {total - fit * size} of {total} free GPUs stranded")
PY
```

For a finer answer (islands, MIG slices, time-sliced memory), write the node shapes from step 1 and the busy GPU indices into a `gpusched` cluster file and run `python3 gpusched.py frag cluster.json`. Record, per job size the teams actually submit: how many fit by count, how many inside one NVLink island, and the stranded GPUs.

## 3. Check the scheduler's choices

```bash
kubectl get pods -A -o custom-columns='NS:.metadata.namespace,NAME:.metadata.name,SCHED:.spec.schedulerName,NODE:.spec.nodeName,GPU:.spec.containers[*].resources.limits.nvidia\.com/gpu' | awk '$5 != "<none>"'
kubectl -n kube-system get pod -l component=kube-scheduler -o yaml | grep -- '--config' || echo "default profile"
kubectl get events -A --field-selector reason=FailedScheduling | grep -i gpu
```

| Finding | Meaning |
|---|---|
| GPU pods use `default-scheduler` with no custom profile | scoring is `LeastAllocated` over CPU and memory only: small GPU pods spread and fragment nodes |
| many 1-GPU pods on the nodes with the largest NVLink islands | large training jobs will not find a whole island |
| `Insufficient nvidia.com/gpu` while step 2 shows enough free GPUs in total | fragmentation, not capacity |
| `didn't match Pod's node affinity/selector` on GPU pods | the product or memory label the pod wants is missing or misspelled |
| GPU nodes without a taint | CPU-only pods consume CPU and memory on GPU nodes and can block GPU pods |

## 4. Check sharing against the workloads

| Sharing mode in use | Check | Risk |
|---|---|---|
| time-slicing, resource still named `nvidia.com/gpu` | which pods on those nodes need a whole GPU or more memory than `memory / replicas` | training or large-model pods land on a replica: OOM or slow; set `renameByDefault: true` so the shared resource is `nvidia.com/gpu.shared` |
| time-slicing with pods requesting more than 1 replica | `failRequestsGreaterThanOne` | they get no more compute, only more contention |
| MIG `mixed` | free instances per profile versus Pending requests per profile (`Insufficient nvidia.com/mig-...`) | instances of the wrong profile sit idle; a repartition needs the GPU drained |
| MIG `single` | every GPU on the node has one profile | fine for uniform inference, wasteful for mixed sizes |
| exclusive GPUs for notebooks and small inference | average GPU utilisation from the DCGM exporter (`DCGM_FI_DEV_GPU_UTIL`) | whole GPUs idling; MIG or time-slicing on a dedicated pool is cheaper |

## 5. Check gang admission

```bash
kubectl get clusterqueues,localqueues -A 2>/dev/null || echo "no Kueue"
kubectl get podgroups -A 2>/dev/null || echo "no coscheduling PodGroups"
kubectl get jobsets -A 2>/dev/null || true
kubectl get pods -A -o json | python3 -c 'import json,sys,collections; c=collections.Counter(); [c.update([(p["metadata"]["namespace"], (p["metadata"].get("ownerReferences") or [{}])[0].get("name","-"), p["status"]["phase"])]) for p in json.load(sys.stdin)["items"]]; [print(k, v) for k, v in sorted(c.items())]'
```

A multi-pod training job with some pods Running and others Pending for longer than a few minutes is a partial gang holding GPUs. If distributed jobs are submitted without Kueue, a PodGroup or Volcano, record it as a deadlock risk regardless of whether one has happened yet.

## 6. Decide

| Situation | Change | Not this |
|---|---|---|
| large jobs Pending, step 2 shows stranded GPUs, small pods spread | a scheduler profile with `MostAllocated` and `nvidia.com/gpu` weight, or a separate node pool (taint plus toleration) for small GPU pods | buying more nodes of the same shape |
| multi-GPU jobs slow, GPUs span islands | pin them to whole-island nodes (node affinity on the product label), size requests to island size, or move to a DRA driver and `matchAttribute` on the island | more GPUs per pod |
| small inference and notebooks on whole GPUs, low utilisation | a dedicated pool with MIG (isolation) or time-slicing (density, renamed resource) | time-slicing the training pool |
| pods OOM on shared GPUs | rename the shared resource, or MIG with a profile of enough memory | raising replicas |
| partial gangs seen or possible | Kueue ClusterQueue with GPU quota and `waitForPodsReady`, or a coscheduling PodGroup with `minMember` and a timeout | longer job timeouts |
| heterogeneous GPUs selected by labels and affinity, with frequent mistakes | ResourceFlavors per GPU type in Kueue, or DRA DeviceClasses per GPU type | more labels |

Before applying a change, replay it: write the change into the `gpusched` cluster or pods file and rerun `place` and `frag` with the current workload mix. Apply only if the target job size fits and nothing that ran before is now Pending.

## 7. Report

State: the node-shape table from step 1; the fragmentation table per job size (fit by count, fit inside one island, stranded GPUs) before and after the proposed change; the scheduler profile in use; the sharing modes and any workload that does not belong on them; whether gang admission exists; the one change you recommend with its replay result; and what you would buy only if that change is not enough, as a node shape (GPUs per node, island size, memory), not a GPU count.
