---
name: pipeline-review
description: Review an ML pipeline definition (Kubeflow Pipelines v2, minipipe, or an Argo Workflow generated from one) before it merges, and triage a run that missed the cache, hit it when it should not have, retried forever or lost lineage
version: 1.0.0
phase: mlops
lesson: mlops/ml-pipelines
tags: [mlops, ml-pipelines, kubeflow-pipelines, kfp, argo-workflows, caching, lineage, retries, dag, kubernetes]
---

# Pipeline review

Use this in two situations: a pull request adds or changes a pipeline (components, the `@dsl.pipeline` function, base images, caching or retry settings, the compiled IR or the Workflow it becomes); or a pipeline run in production did something surprising: every step reran, a step was a cache hit with stale data, a step retried for an hour, or nobody can say which data produced a model. Do not clear caches or delete runs during triage before you have recorded the cache keys; they are the evidence.

## Part A: before merge

### 1. Compile and read the DAG

```bash
python3 minipipe.py compile pipeline.py -o ir.json          # or: python3 kfp_compile.py pipeline.py ir.json (kfp's IR, same shape)
python3 minipipe.py dag ir.json                              # leaf tasks, deps, where every input comes from, outputs
python3 minipipe.py dag ir.json --json > dag.json
git diff --no-index old-ir.json ir.json                      # what the change does to the compiled spec, not just to the Python
```

For KFP projects without minipipe, compile with `kfp.compiler.Compiler().compile(pipeline, "ir.yaml")` and read `root.dag.tasks`, `components.*.dag.tasks` and `deploymentSpec.executors`. Write down every leaf task, its `dependentTasks`, and for each input whether it is a constant, a pipeline parameter, another task's output, or a loop item.

### 2. Check every task against this table

| Check | How | Problem if it fails | Fix |
|---|---|---|---|
| dependencies come from data | every `dependentTasks` entry is either a producer of an input or an explicit `.after()` with a comment in the PR | hidden ordering through a shared bucket path or a database table: the steps race, and caching hides it | pass the artifact as an input, or add `.after()` and make the side effect an output |
| the component is self-contained | imports inside the function; no module-level globals; no reads of paths that are not inputs | the step works on a laptop and fails in its container, or reads data the cache key does not see | move every file it reads into an `Input[...]` artifact |
| the step is deterministic | seeds passed as parameters; no `time.time()`, no unseeded `random`, no network reads in the function | a cache hit returns a result a rerun would not produce; two runs with the same key disagree | pass seeds and dates as parameters; fetch remote data in its own step that outputs a versioned artifact |
| the image is pinned | `base_image` is a tag you control or a digest | the key does not change when the tag moves, so cached results came from a different runtime | pin `image@sha256:...`, or include the digest in the tag your CI publishes |
| caching is right per step | `cachingOptions.enableCache` per task in the IR | a step with side effects (push to a registry, deploy, send a message) is skipped on a hit; or an expensive pure step runs every time | `set_caching_options(False)` only for side effects and for steps that read the outside world |
| parameters are not secrets | pipeline parameters and constants in the IR | secrets end up in the IR, the run record, the tracking server and the cache key | mount secrets as environment variables or volumes in the backend, never as parameters |
| retries fit the failure | `retryPolicy.maxRetryCount`, `backoffDuration`, `backoffMaxDuration` | retrying a deterministic bug wastes the backoff window; no retry on a step that hits a flaky API fails the whole night | retry only steps that touch the network or preemptible nodes; keep `backoffMaxDuration` below the run's deadline |
| conditions compare the right types | `triggerPolicy.condition` in the IR (`int(...)`, `double(...)`, quoted strings) | comparing a string output with a number never matches; the branch silently never runs | make the producing component return the type you compare |
| loops are bounded | `parameterIterator.items` and `iteratorPolicy.parallelismLimit` | a fan-out over thousands of items starts thousands of pods | set `parallelism`; batch items |
| outputs are small where they travel | parameters versus `Output[...]` artifacts | a dataset returned as a string parameter bloats the IR store and the Workflow object (Kubernetes objects are limited to about 1.5 MiB) | return an artifact; pass only references and digests |

### 3. Prove the cache behaves

```bash
python3 minipipe.py run ir.json --store /tmp/review-store --json > run1.json
python3 minipipe.py run ir.json --store /tmp/review-store --json > run2.json      # expect every task "cached"
python3 minipipe.py run ir.json --store /tmp/review-store --param lr=0.4          # expect only lr's consumers and their downstream to execute
python3 minipipe.py explain train-model --store /tmp/review-store                  # names the ingredient that changed
```

A second run that is not all cache hits means a step is nondeterministic (its output digest changes, so everything downstream misses). Find the first task whose `artifacts.*.sha256` differs between `run1.json` and `run2.json` with caching off (`--no-cache`), and fix that step.

### 4. Check the backend translation

```bash
python3 minipipe.py argo ir.json --service-account pipeline-runner -o workflow.json
kubectl create --dry-run=server -f workflow.json -n NAMESPACE      # needs the Argo CRDs; catches schema errors
```

Check that every task's `dependencies` match the IR, that `when` expressions reference the right outputs, that `withItems` loops have a `parallelism` limit, that `retryStrategy.limit` matches `maxRetryCount`, and that the service account can create `workflowtaskresults` in the namespace (Argo's executor needs it to report outputs).

## Part B: triage a run

### 1. Collect the evidence

```bash
python3 minipipe.py explain TASK --store STORE                     # status, reason, key, and the ingredient diff with the previous execution
ls STORE/runs/ && jq '.tasks[] | {path, status, reason, cache_key}' STORE/runs/RUN.json
kubectl get workflow WF -n NS -o json | jq '.status.nodes[] | {displayName, type, phase, message, startedAt, finishedAt}'
argo get WF -n NS && argo logs WF -n NS                             # if the argo CLI is installed
```

On Kubeflow Pipelines, open the run in the UI or list executions through the KFP client; a cached task shows as cached and links to the execution whose outputs it reused.

### 2. Decide what happened

| Symptom | Likely cause | Confirm with | Action |
|---|---|---|---|
| every task executed although nothing changed | the first task is nondeterministic, or the image tag moved, or the function source changed by formatting | `explain` on the first task: `container.image`, `container.command[3]`, or an `artifacts.*` digest | pin the image; make the step deterministic; accept a one-time miss after a refactor |
| a task executed with reason "artifact ... changed on disk" or "is gone" | someone edited or a lifecycle rule deleted a stored artifact | the reason line; object-store access logs | restore lifecycle rules for referenced objects; the rerun already repaired the entry |
| a task was a cache hit but its output is wrong | the step reads something that is not an input (a `latest` table, a URL, the clock) | look for reads in the function that are not parameters or `Input[...]` | make that read its own uncached step that outputs a versioned artifact; disable caching meanwhile |
| a branch never runs | the condition compares mismatched types, or the producing task was skipped | `triggerPolicy.condition` in the IR; the producer's output value | fix the type; print the value in the step |
| a step retried until the window closed | a deterministic error retried as if it were transient | the step's log: the same exception on every attempt | remove the retry or fix the bug; keep retries for network and preemption errors |
| downstream tasks show `upstream-failed` or Argo `Omitted` | a dependency failed after its retries | the first failed node | fix the first failure; do not rerun the tail by hand |
| a model has no lineage | the run had no tracking URI, or the tracking server was down and the run continued | the `lineage:` line in the run's stderr | rerun with `--tracking-uri` (cache hits are recorded too, so lineage costs no compute) |

## Report

Write the result in this form:

```
Pipeline: NAME (IR sha256 ..., N leaf tasks)
Verdict: approve | changes requested | incident explained
DAG: one line per task: deps, cache on/off, retries, condition or loop
Findings: numbered, each with the check from the table, the evidence (IR path, explain line, node message) and the fix
Cache: first run X executed; second run Y cached of Y (must be all); keys that differ and why
Backend: Workflow validated yes/no; service account; parallelism limits
Lineage: tracking URI used; parent run id; models traceable to dataset digests yes/no
```
