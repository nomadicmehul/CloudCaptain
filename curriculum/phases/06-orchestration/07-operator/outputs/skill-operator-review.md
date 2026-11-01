---
name: operator-review
description: Review a Kubernetes operator (yours or a vendor's) before it reaches a shared cluster: CRD schema and CEL rules, status conventions, RBAC under impersonation, and six behaviour drills on kind with a fixed report
version: 1.0.0
phase: orchestration
lesson: orchestration/operator
tags: [kubernetes, operators, crd, cel, rbac, finalizers, controller-runtime, kubebuilder, kopf, review]
---

# Operator review

Use this before installing an operator into a cluster other teams depend on, when an operator's CRDs or Role change in an upgrade, or when an operator's objects get stuck in `Terminating`. You need the operator's manifests (CRDs, Role or ClusterRole, Deployment) and a scratch cluster: `kind create cluster --name cc-review`. Never run the drills against production.

## 1. Inventory what it installs

```bash
kubectl apply --dry-run=server -f operator.yaml -o name | sort                    # every object the install creates
grep -E '^kind: (CustomResourceDefinition|ClusterRole|Role|ClusterRoleBinding|RoleBinding|MutatingWebhookConfiguration|ValidatingWebhookConfiguration)' -A3 operator.yaml
kubectl get crd -o custom-columns=NAME:.metadata.name,SCOPE:.spec.scope,VERSIONS:.spec.versions[*].name   # after installing on kind
```

Write down: each CRD with its scope and versions, which version is `storage: true`, whether a conversion webhook exists, and every webhook (a webhook in the request path of every pod create is a cluster-wide outage risk if its service is down and `failurePolicy: Fail`).

## 2. Read each CRD

```bash
kubectl get crd NAME -o json | python3 -c 'import json,sys; v=json.load(sys.stdin)["spec"]["versions"]; [print(x["name"], "served" if x["served"] else "-", "storage" if x["storage"] else "-", "status" if "status" in (x.get("subresources") or {}) else "NO STATUS SUBRESOURCE", len(x.get("additionalPrinterColumns") or [])) for x in v]'
kubectl explain KIND.spec --recursive | head -60
kubectl get crd NAME -o json | grep -o '"x-kubernetes-validations"[^]]*]' | head
```

| Check | Pass looks like | Fail looks like |
|---|---|---|
| Spec and status split | `subresources.status` enabled; `status` written only by the controller | status inside spec, or no status subresource, so users and the controller fight over one write path |
| Generation tracking | `status.observedGeneration` and conditions carrying `observedGeneration` | a `phase: Ready` string with no generation; nobody can tell which spec it describes |
| Conditions | `status.conditions` with `type`, `status`, `reason`, `message`, `lastTransitionTime`, `x-kubernetes-list-type: map` | free-form status text, or a different shape per kind |
| Validation | types, `minimum`/`maximum`, `enum`, `maxLength`, `required`, and CEL rules for cross-field constraints with a human `message` | `x-kubernetes-preserve-unknown-fields: true` on spec, so anything is accepted and typos vanish |
| Defaults | `default:` in the schema, visible in `kubectl get -o yaml` | defaults applied silently inside the controller, so the stored spec lies |
| Printer columns | `kubectl get` shows readiness and the reason | only NAME and AGE |
| Versions | one storage version, a written conversion plan for the next | several served versions without a conversion webhook and with different schemas |

## 3. Read the Role, then test it under impersonation

```bash
SA=system:serviceaccount:NAMESPACE:SERVICEACCOUNT
kubectl auth can-i --list --as "$SA" -n NAMESPACE
for v in delete escalate bind impersonate; do echo "$v: $(kubectl auth can-i "$v" '*' --all-namespaces --as "$SA")"; done
kubectl auth can-i get secrets --all-namespaces --as "$SA"
```

The operator needs `get`, `list`, `watch` on its own kinds, `update` on `KIND/status` and usually `KIND/finalizers`, and `get`/`create`/`update` (plus `list`/`watch` for the kinds it watches) on its children. It does not need `delete` on children it owns through `ownerReferences`, because the garbage collector deletes them. Flag every grant beyond that: `*` verbs, cluster-wide `secrets`, `escalate`, `bind`, `impersonate`, or a ClusterRole where the operator only ever touches one namespace. Then run the operator through `kubectl proxy --as "$SA"` (or as its real Deployment) for the drills below; a 403 in its log is a missing rule, and nothing in the drills should need more than the Role grants.

## 4. Six drills on kind

Run each with the operator's log open (`kubectl logs -f deploy/OPERATOR -n NS`) and record pass or fail.

1. **Create.** Apply a minimal valid object. Pass: children appear with `ownerReferences` naming the object's `uid` with `controller: true`; `status.observedGeneration == metadata.generation`; a `Ready`-style condition turns True; `kubectl wait --for=condition=Ready` works.
   `kubectl get deploy,svc,cm -n NS -o jsonpath='{range .items[*]}{.kind}/{.metadata.name} {.metadata.ownerReferences[0].kind} {.metadata.ownerReferences[0].controller}{"\n"}{end}'`
2. **Spec change.** Patch one spec field. Pass: `generation` goes up by one, `observedGeneration` follows, Ready goes False while the change rolls out and True after. Fail: status stays True at the old generation.
3. **Status tampering.** `kubectl patch KIND NAME --subresource=status --type=merge -p '{"status":{"observedGeneration":0}}'`. Pass: the controller rewrites it within seconds. Fail: the wrong value stays, which means it never rereads status or only acts on spec events.
4. **Drift on a child.** `kubectl scale` or `kubectl edit` a child the operator owns. Pass: reverted within seconds (it watches its children) or at the next resync (state the interval). Fail: never reverted, or the operator deletes and recreates the child, which drops traffic.
5. **Deletion with the operator stopped.** Scale the operator to zero, delete an object, inspect it, scale back.
   `kubectl get KIND NAME -o jsonpath='{.metadata.deletionTimestamp} {.metadata.finalizers}{"\n"}'`
   Pass: the object waits with `deletionTimestamp` set and the finalizer present; after the restart the external cleanup runs, the finalizer goes, and the children are garbage collected. Fail: the object vanishes without cleanup (no finalizer), or stays stuck after the restart (it does not handle deletions it finds on startup).
6. **A conflicting writer.** While the operator is reconciling, update the same object from a script in a loop (`kubectl annotate KIND NAME n=$i --overwrite`). Pass: the log shows conflicts retried with backoff and the object converges. Fail: crashes, a reconcile that gives up, or writes without `resourceVersion` that overwrite your annotations.

Also check what a cluster admin will hit later: `kubectl delete crd NAME` with objects present and the operator stopped hangs on the finalizers, so the uninstall procedure must delete the objects while the operator is still running.

## 5. Decide

| Finding | Decision |
|---|---|
| any CRD without a status subresource or with `x-kubernetes-preserve-unknown-fields` on spec | not for a shared cluster until fixed |
| Role grants `escalate`, `bind`, `impersonate`, `*` verbs or cluster-wide `secrets` without a stated reason | not for a shared cluster; ask for a narrower Role |
| drill 5 fails (no finalizer for external resources, or stuck after restart) | not anywhere external resources matter |
| drills 2 or 3 fail (status not tied to generation) | acceptable for dev only; automation and GitOps health checks will lie |
| webhook with `failurePolicy: Fail` and one replica | require two replicas and a PodDisruptionBudget first |
| everything passes | approve, with the Role and the uninstall procedure written down |

## Report

Write six lines into the platform runbook: the operator and version reviewed; the CRDs with scope, storage version and status-subresource yes or no; the Role summary with every grant you flagged; the six drills with pass or fail and one line of evidence each; the decision from the table with the single change that would move it up a tier; and the uninstall order (delete objects, wait for finalizers, delete the operator, delete CRDs). Date it and repeat the review on every upgrade that changes a CRD or the Role.
