---
name: gitops-sync-triage
description: Triage a GitOps app (Argo CD or Flux) that is OutOfSync, Degraded, stuck syncing or flapping, by checking the commit it is at, separating render errors from drift, classifying every differing field by who owns it, and reading a failed sync down to the hook or wave that stopped it
version: 1.0.0
phase: cicd
lesson: cicd/gitops-reconciler
tags: [gitops, argo-cd, flux, kustomize, server-side-apply, managed-fields, drift, prune, sync-waves, hooks, health]
---

# GitOps sync triage

Use this when an Argo CD `Application` or a Flux `Kustomization` is OutOfSync and will not settle, says Synced but the change is not live, is Degraded or Progressing after a sync, has a sync that failed or never finishes, or flips back to OutOfSync after every sync.

Do not hand-edit live objects to make the status green: with self-heal the controller reverts you, without it you have created the drift you are investigating. Do not delete the `Application` while triaging: with the `resources-finalizer.argocd.argoproj.io` finalizer that deletes every workload it manages. Change Git, or change the app's spec, and say which.

The model: the controller resolves a branch to a commit, renders one path at that commit, diffs each rendered object with the live one, applies the difference in hook phases and sync waves with its own field manager, prunes tracked objects that left Git, and assesses health per kind. Every symptom is one of those steps failing or two writers disagreeing about a field.

## 1. Which app, which commit, which policy

```bash
APP=<app>; NS=argocd
kubectl -n $NS get application $APP -o jsonpath='repo={.spec.source.repoURL} path={.spec.source.path} target={.spec.source.targetRevision}{"\n"}sync={.status.sync.status} at={.status.sync.revision} health={.status.health.status}{"\n"}op={.status.operationState.phase} {.status.operationState.message}{"\n"}policy={.spec.syncPolicy}{"\n"}'
kubectl -n $NS get application $APP -o jsonpath='{range .status.conditions[*]}{.type}: {.message}{"\n"}{end}'
git ls-remote <repoURL> refs/heads/<targetRevision>
```

For Flux:

```bash
flux get sources git -A
flux get kustomizations -A
kubectl -n flux-system get kustomization <name> -o jsonpath='ready={.status.conditions[?(@.type=="Ready")].status} {.status.conditions[?(@.type=="Ready")].message}{"\n"}applied={.status.lastAppliedRevision} attempted={.status.lastAttemptedRevision}{"\n"}'
```

| What you see | Meaning | Go to |
|---|---|---|
| a `ComparisonError` condition, or Flux `Ready=False` with a build or fetch message | the controller cannot read Git or cannot render | step 2 |
| `sync.revision` older than `git ls-remote` for minutes | the new commit has not been seen: polling interval, webhook, wrong branch or path | step 2 |
| OutOfSync with a current revision | real differences | step 3 |
| `operationState.phase` Failed or Running for long | the sync itself | step 4 |
| Synced but Degraded or Progressing | the commit was applied and does not work | step 5 |
| Synced, then OutOfSync again after every sync | two writers | step 6 |

## 2. The controller cannot fetch or render

Render the same commit yourself; whatever fails here fails in the repo-server:

```bash
git clone --quiet <repoURL> /tmp/triage && git -C /tmp/triage checkout --quiet <sync.revision or branch SHA>
kubectl kustomize /tmp/triage/<path> > /tmp/rendered.yaml && grep -c '^kind:' /tmp/rendered.yaml
kubectl apply --dry-run=server -f /tmp/rendered.yaml
```

| Message contains | Cause | Fix |
|---|---|---|
| `authentication required`, `repository not found`, `Permission denied (publickey)` | no or wrong repository credentials | a Repository Secret (Argo CD) or `secretRef` on the GitRepository (Flux) for that URL |
| `unable to resolve` the target revision | branch or tag does not exist | fix `targetRevision` or `ref` |
| `accumulating resources`, `no such file or directory` | a path in `resources` or `patches` is wrong at that commit | fix the kustomization; paths are relative to it |
| `no matches for Id`, `failed to find unique target for patch` | a patch names an object that is not in its resources | patch the base name, before the prefix |
| `json: cannot unmarshal number into ... string` | an unquoted value such as `newTag: 3.10` or a label `"true"` written as `true` | quote it in Git |
| `may not add resource with an already registered id` | the same object twice | remove the duplicate |
| the new commit is never seen | polling only (Argo CD refreshes about every two minutes by default; Flux every `interval`) | wait one interval, add a Git webhook, or `kubectl -n argocd annotate application $APP argocd.argoproj.io/refresh=normal` |

## 3. OutOfSync: classify every differing field

```bash
argocd login --core && argocd app diff $APP
kubectl -n $NS get application $APP -o jsonpath='{range .status.resources[?(@.status=="OutOfSync")]}{.kind}/{.namespace}/{.name} requiresPruning={.requiresPruning}{"\n"}{end}'
kubectl -n <ns> get <kind> <name> -o yaml --show-managed-fields | sed -n '/managedFields:/,/^  name:/p' | grep -E 'manager:|operation:|subresource:|time:'
```

For every field in the diff, find who wrote the live value in `managedFields` (the manager whose `fieldsV1` contains it) and decide:

| Live value written by | Typical fields | Meaning | Action |
|---|---|---|---|
| a person: `kubectl-edit`, `kubectl-patch`, a `kubectl` manager with subresource `scale`, a UI | replicas, image, env, resources | real drift | if it must stay, commit it to Git; otherwise sync, or let self-heal revert it; note it in the incident |
| another controller: HorizontalPodAutoscaler, VPA, an operator, a webhook that injects sidecars or a `caBundle` | `spec.replicas`, container resources, injected containers, `caBundle` | two owners for one field | remove the field from Git, or add `ignoreDifferences` (by `jsonPointers`, `jqPathExpressions` or `managedFieldsManagers`) and the sync option `RespectIgnoreDifferences=true` |
| the API server: defaulting or canonical form | `cpu: 0.5` live as `500m`, `1024Mi` as `1Gi`, an empty map dropped, a defaulted `protocol` | an artefact of comparing non-canonical YAML | write the canonical form in Git, or enable `argocd.argoproj.io/compare-options: ServerSideDiff=true` |
| the controller itself, before the field left Git | a removed annotation, env var or port | a removal the next sync will make | sync; with client-side apply a field missing from `last-applied-configuration` can stick, `ServerSideApply=true` removes it through field ownership |
| no live object, `requiresPruning=true` | an object deleted from Git | prune is off, or the object has `argocd.argoproj.io/sync-options: Prune=false` | confirm it is safe to delete (data, PVCs, CRDs with instances), then sync with prune |
| missing live object | created in Git, never applied, or deleted by hand | a sync that has not run or failed | step 4 |

Check the tracking before trusting a prune list: Argo CD's default is the `argocd.argoproj.io/tracking-id` annotation, label tracking uses `app.kubernetes.io/instance`, Flux uses its inventory. An object another tool labelled with the same label value looks like an app resource under label tracking.

## 4. The sync failed or never finishes

```bash
kubectl -n $NS get application $APP -o jsonpath='{.status.operationState.phase}: {.status.operationState.message}{"\n"}{range .status.operationState.syncResult.resources[*]}{.hookPhase}{.syncPhase} {.kind}/{.name} {.status} {.message}{"\n"}{end}'
kubectl -n <ns> get jobs -o wide
kubectl -n <ns> logs job/<hook-job> --tail=50
```

| Message contains | Cause | Fix |
|---|---|---|
| a PreSync or Sync hook `Failed`, `BackoffLimitExceeded` | the hook Job failed; nothing after it ran | read the Job's logs, fix in Git, commit; the same commit is not retried automatically |
| `waiting for healthy state of` a resource | a wave is blocked on health; later waves have not started | step 5 for that resource |
| `field is immutable` | a Deployment selector, a Service `clusterIP` or a Job template changed | plan a recreate: `argocd.argoproj.io/sync-options: Replace=true` on that object, or delete it on purpose |
| `the server could not find the requested resource`, `no matches for kind` | a custom resource is applied before its CRD exists | put the CRD in an earlier wave or another app, or `SkipDryRunOnMissingResource=true` |
| `admission webhook ... denied the request` | a policy engine rejected the object | change the manifest to meet the policy; the message names the rule |
| `conflict with` a field manager | server-side apply without force | Argo CD forces with `ServerSideApply=true`; for another tool, decide which manager owns the field |
| Running for a long time, no message | a wave or hook never becomes healthy | `argocd app terminate-op $APP` after you know why, then fix forward |

## 5. Synced, but Degraded or Progressing

```bash
kubectl -n $NS get application $APP -o jsonpath='{range .status.resources[*]}{.kind}/{.name} {.health.status} {.health.message}{"\n"}{end}' | grep -v Healthy
kubectl -n <ns> rollout status deploy/<name> --timeout=10s
kubectl -n <ns> get events --sort-by=.lastTimestamp | tail -20
```

| Health message | Meaning | Action |
|---|---|---|
| `exceeded its progress deadline` | new pods never became available; old ones still serve | read pod events (image pull, probes, crash); revert the commit or commit the fix |
| `Waiting for rollout to finish: X out of Y new replicas have been updated` and not moving | scheduling or readiness | `kubectl describe pod` for the new ReplicaSet's pods |
| `observed deployment generation less than desired generation` for minutes | the Deployment controller is not processing | the controller-manager, or a stuck admission webhook |
| a LoadBalancer Service Progressing forever | no load balancer implementation (kind, bare metal) | install one, or use ClusterIP or NodePort |
| a PersistentVolumeClaim Pending | no StorageClass or no matching volume | `kubectl describe pvc` |
| a custom resource Healthy while it is broken | no health check for that kind | add a Lua check under `resource.customizations.health.<group>_<kind>` |

Synced and Degraded means the commit is wrong. Another sync applies the same commit again.

## 6. Flapping: OutOfSync again after every sync

```bash
kubectl -n $NS get application $APP -o jsonpath='{range .status.history[*]}{.id} {.revision} {.deployedAt}{"\n"}{end}' | tail -5
kubectl -n <ns> get <kind> <name> -o yaml --show-managed-fields | grep -E 'manager:|time:'
```

Several history entries at the same revision within minutes, with self-heal on, means two writers. Find the second manager in `managedFields` with a recent `time`, then apply the step 3 table: one field, one owner. A mutating webhook that changes the object on every apply shows up as the same diff after every sync; `ServerSideDiff=true` includes webhooks in the comparison.

## 7. Report

State: the app and controller; the commit it is at against the branch head; sync and health status; for each differing field, the path, the Git value, the live value and the manager that wrote it, classified as drift, second owner, artefact, removal or prune; for a failed sync, the phase, wave and resource that stopped it with the message; what you changed (a commit, an `ignoreDifferences` entry, a sync option) and why it ends the difference; and whether the old version is still serving. Never report a hand edit to the cluster as the fix.
