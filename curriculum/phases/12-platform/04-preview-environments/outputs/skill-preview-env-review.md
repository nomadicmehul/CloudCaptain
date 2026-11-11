---
name: preview-env-review
description: Review a per-pull-request preview environment setup (a homegrown controller, an Argo CD ApplicationSet, a vendor product) for guard rails, access, expiry, reaper safety, leftovers and cost, with drills on kind and a fixed report
version: 1.0.0
phase: platform
lesson: platform/preview-environments
tags: [kubernetes, preview-environments, resourcequota, limitrange, networkpolicy, rbac, argo-cd, applicationset, vcluster, platform-engineering, review]
---

# Preview environment review

Use this before preview environments go onto a shared cluster, when someone changes the template or the reaper, or when the cluster fills up with namespaces nobody recognises. You need the setup's manifests or template, read access to a cluster that runs it, and a scratch cluster for the drills: `kind create cluster --name cc-review`. Never run the reaper drills against a cluster other teams use.

## 1. Inventory one environment

Pick one live preview namespace `NS` and list everything that belongs to it, inside and outside:

```bash
kubectl get ns NS -o jsonpath='{.metadata.labels}{"\n"}{.metadata.annotations}{"\n"}'
kubectl api-resources --verbs=list --namespaced -o name | xargs -n1 kubectl get -n NS --ignore-not-found -o name 2>/dev/null | sort
kubectl get pv -o jsonpath='{range .items[?(@.spec.claimRef.namespace=="NS")]}{.metadata.name} {.status.phase} {.spec.persistentVolumeReclaimPolicy}{"\n"}{end}'
kubectl get clusterroles,clusterrolebindings,mutatingwebhookconfigurations,validatingwebhookconfigurations -o name | grep -i "NS" || echo "no cluster-scoped objects named after NS"
```

Write down: who owns it, which pull request, when it expires, its URL, and every object outside the namespace.

## 2. Check the namespace's identity and lifetime

| Check | Pass looks like | Fail looks like |
|---|---|---|
| Ownership label | `app.kubernetes.io/managed-by` set to the controller, plus a repository label | no label: a reaper has to guess by name |
| Pull request and owner | PR number and author as labels | only in the name, so renames and long repo names break matching |
| Expiry | an expiry label or annotation computed from the last push, renewed on each push | none, or "created + N days" so active PRs die and abandoned ones live N days |
| Second line of defence | `janitor/expires` or an equivalent a separate janitor reads | none: if the reaper stops, nothing expires |
| Name length | at most 63 characters with a hash suffix for long repository names | truncation that can collide |

## 3. Test the guard rails with server-side dry run

```bash
kubectl describe resourcequota -n NS
kubectl describe limitrange -n NS
kubectl get networkpolicy -n NS
cat > /tmp/plain.json <<'JSON'
{"apiVersion":"v1","kind":"Pod","metadata":{"name":"plain"},"spec":{"containers":[{"name":"app","image":"registry.k8s.io/pause:3.10"}]}}
JSON
kubectl create -n NS --dry-run=server -f /tmp/plain.json -o jsonpath='{.spec.containers[0].resources}{"\n"}'
sed 's/"image"/"resources":{"limits":{"cpu":"64"}},"image"/' /tmp/plain.json | kubectl create -n NS --dry-run=server -f -
kubectl get svc -A -o jsonpath='{range .items[?(@.spec.type=="LoadBalancer")]}{.metadata.namespace}/{.metadata.name}{"\n"}{end}'
```

Pass: the plain pod gets defaults from a LimitRange; the 64-CPU pod is refused by the LimitRange max or the quota; the quota caps `requests.cpu`, `requests.memory`, `limits.*`, `pods`, and sets `services.loadbalancers` and `services.nodeports` to 0; no preview namespace has a LoadBalancer Service; a default-deny policy exists with explicit exceptions (DNS, same namespace, the ingress or Gateway namespace). Confirm the CNI enforces NetworkPolicy: from a pod in NS, a request to a pod in another preview namespace must time out.

## 4. Test access under impersonation

```bash
AUTHOR=$(kubectl get ns NS -o jsonpath='{.metadata.labels.preview\.cloudcaptain\.io/owner}')
kubectl get rolebindings -n NS -o wide
kubectl auth can-i create deployments -n NS --as "$AUTHOR"
kubectl auth can-i create deployments -n NS --as someone-else
kubectl auth can-i create rolebindings -n NS --as "$AUTHOR"
kubectl auth can-i get secrets -n OTHER-PREVIEW-NS --as "$AUTHOR"
```

Pass: yes, no, no, no. A group subject (all developers) on every preview, `admin` instead of `edit` (it allows RoleBindings, so the author can invite anyone), or any ClusterRoleBinding created per preview is a finding.

## 5. Reaper drills on kind

Install the setup on the scratch cluster with three pull requests, then run each drill and record pass or fail:

1. **Expired.** Advance time (a `--now` flag, or edit the expiry label to the past). Pass: exactly that environment is deleted, with a reason in the log.
2. **Closed.** Mark one pull request closed in the source. Pass: its environment goes at the next run; the others stay.
3. **Empty list.** Make the pull request source fail (revoke the token, point it at a dead URL). Pass: nothing is deleted for being "closed", and the run reports an error or a warning. Fail: every environment disappears.
4. **Not ours.** Create a namespace that matches the naming pattern without the ownership label. Pass: never touched.
5. **Crash halfway.** Create a ClusterRoleBinding labelled for an environment and a PersistentVolume with `claimRef.namespace` pointing at it, delete the namespace by hand, then run the reaper. Pass: both leftovers are removed as orphans.
6. **Idempotence.** Run reconcile twice and compare `resourceVersion`s of every managed object. Pass: unchanged.

```bash
kubectl get ns,resourcequota,limitrange,networkpolicy,rolebinding,deploy,svc -A -l app.kubernetes.io/managed-by=CONTROLLER -o jsonpath='{range .items[*]}{.kind}/{.metadata.namespace}/{.metadata.name} {.metadata.resourceVersion}{"\n"}{end}' > before.txt
# run reconcile again
kubectl get ... > after.txt && diff before.txt after.txt
```

A Deployment's or a quota's resourceVersion can change because a controller updated its status; check `kubectl get -o json --show-managed-fields` to see whether the setup's own field manager wrote.

## 6. Cost

Sum requests per preview namespace and multiply by your price table; compare with the quota ceiling and with what the platform budget allows:

```bash
kubectl get pods -A -l app.kubernetes.io/managed-by=CONTROLLER -o jsonpath='{range .items[*]}{.metadata.namespace} {range .spec.containers[*]}{.resources.requests.cpu} {.resources.requests.memory} {end}{"\n"}{end}'
```

Flag environments idle for days (no push, no traffic) that still hold requests; scale-to-zero or a shorter TTL is the fix.

## 7. Decide

| Finding | Decision |
|---|---|
| reaper deletes by name pattern without an ownership label, or acts on an empty pull request list | block: one bad run deletes other teams' work |
| no quota, or a quota without a LimitRange | block on a shared cluster: one preview can starve others, or nothing deploys |
| no expiry, or expiry from creation time | fix before scaling past a handful of environments |
| access granted to a group, or `admin` instead of `edit` | fix: least privilege per author |
| cluster-scoped leftovers found (Released volumes, ClusterRoleBindings) | fix the reaper; clean up by hand once |
| apps need CRDs, webhooks or cluster-admin | move those apps to virtual clusters (vcluster) instead of namespaces |
| everything passes | approve, with the TTL, quota and price table written down |

## Report

Write seven lines into the platform runbook: the setup and version reviewed; one environment's inventory (inside and outside the namespace); the guard-rail results (defaults, max, quota, LoadBalancers, NetworkPolicy enforcement); the access results under impersonation; the six reaper drills with pass or fail and one line of evidence each; the hourly and projected cost of all environments today; and the decision from the table with the single change that would move it up a tier. Date it and repeat the review when the template, the reaper or the cluster's CNI changes.
