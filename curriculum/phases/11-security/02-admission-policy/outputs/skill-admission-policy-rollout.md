---
name: admission-policy-rollout
description: Take one Kubernetes admission rule from a sentence to enforcement without an outage - inventory the write path, pick ValidatingAdmissionPolicy or a webhook, write guarded CEL, test offline and with dry runs, roll out as Warn and Audit, then Deny, and prove the failure mode
version: 1.0.0
phase: security
lesson: security/admission-policy
tags: [kubernetes, admission, validatingadmissionpolicy, cel, webhook, kyverno, gatekeeper, failurepolicy, policy-as-code]
---

# Admission policy rollout

Use this when someone asks for a cluster rule ("no privileged containers", "images only from our registry", "every workload has an owner label"), when a policy engine or webhook is being installed, or after an incident where an admission webhook blocked writes. The goal is a rule that is enforced, tested, scoped, and whose failure mode you have seen with your own eyes.

Never apply a new webhook or policy with `Deny` or `failurePolicy: Fail` to every namespace first. Never put `kube-system` or the webhook's own namespace in its scope.

## 1. Inventory the write path

```bash
kubectl api-resources --api-group=admissionregistration.k8s.io
kubectl get validatingwebhookconfigurations,mutatingwebhookconfigurations -o custom-columns='NAME:.metadata.name,HOOKS:.webhooks[*].name,FAILURE:.webhooks[*].failurePolicy,TIMEOUT:.webhooks[*].timeoutSeconds'
kubectl get validatingadmissionpolicies,validatingadmissionpolicybindings
kubectl get mutatingadmissionpolicies,mutatingadmissionpolicybindings 2>/dev/null || true
kubectl get ns -L pod-security.kubernetes.io/enforce,pod-security.kubernetes.io/warn
kubectl get --raw /metrics | grep -E '^apiserver_admission_webhook_(rejection_count|fail_open_count|admission_duration_seconds_count)' | head -20
```

For every webhook, write down: what it matches (`rules`, `namespaceSelector`, `objectSelector`, `matchConditions`), `failurePolicy`, `timeoutSeconds`, `sideEffects`, and how many replicas serve it (`kubectl -n NS get deploy,pdb`). A webhook with `Fail`, no namespace scope and one replica is an incident waiting for a node drain; fix that before adding anything.

## 2. Choose the mechanism

| The rule needs | Use | Why |
|---|---|---|
| one of the Pod Security Standards controls (privileged, host namespaces, capabilities, runAsNonRoot, seccomp) | Pod Security Admission labels on the namespace | built in, no code, `warn` and `audit` modes |
| a check on fields of the object itself, old object, request user or namespace labels | ValidatingAdmissionPolicy (CEL) | in-process, no certificate, no network hop, typed against the schema |
| a default value or label added on create | MutatingAdmissionPolicy where served (GA in 1.36), else LimitRange for requests, else a mutating webhook | in-process mutation first; a webhook only when CEL cannot express it |
| data from outside the object (another cluster object, an external service, an image signature) | a policy engine (Kyverno, Gatekeeper) or your own webhook | needs lookups CEL cannot do |
| reports on objects that already exist | Kyverno or Gatekeeper audit | admission only sees new writes |

## 3. Write the policy defensively

- Guard every optional field with `has()` before selecting it: `!has(c.securityContext) || !has(c.securityContext.privileged) || !c.securityContext.privileged`. A missing field is an error, and under `failurePolicy: Fail` an error denies.
- Cover `initContainers` and `ephemeralContainers`, not only `containers`, and decide whether workloads (Deployments, Jobs, CronJobs) are judged at their template or only at the Pod.
- Compare image registries as path components after normalising (`nginx` is `docker.io/library/nginx`), never as string prefixes.
- Write a `message` or a `messageExpression` that names the container and the field to change.
- Remember that the API server defaults the object before admission (`resources: {}`, requests copied from limits); write checks and patches against that object.

## 4. Test offline, then on the API server without persisting

```bash
python3 miniadmit.py test --policies policies.json --namespace shop manifests/*.json        # or: kyverno apply policy.yaml --resource m.yaml --audit-warn
python3 miniadmit.py cel "EXPR" --object manifests/good.json                                # one expression against one object
kubectl apply -f policy.yaml                                                                 # with the binding scoped to one test namespace and [Warn, Audit]
kubectl get validatingadmissionpolicy NAME -o jsonpath='{.status.typeChecking}'
kubectl -n shop create --dry-run=server -f manifests/compliant.yaml
kubectl -n shop create --dry-run=server -f manifests/violating.yaml
```

Keep a table of manifests and expected outcomes (allowed, denied by which policy, warned by which) and run it in CI on every policy change. Include at least: a compliant pod, one violation per rule, a pod without the optional fields the expressions touch, a Deployment, and a lookalike (`registry.internal.evil.example`).

## 5. Roll out

1. Bind with `validationActions: [Warn, Audit]` in one namespace, then all application namespaces. Webhooks: return `warnings` and `allowed: true`.
2. Collect for a week: kubectl warnings in CI logs, `validation.policy.admission.k8s.io/validation_failure` annotations in the audit log, background reports if you use an engine. List the owners of every violation.
3. Fix or exempt them (a label in `matchConditions`, a namespace out of the selector), with an expiry date for each exemption.
4. Switch the binding to `[Deny]`, namespace by namespace. Do not combine Deny and Warn; the API server rejects that.

## 6. Prove the failure mode before you leave

| Mechanism | Test | Expected |
|---|---|---|
| VAP | apply a manifest that makes an expression error (missing field) | `resulted in error`: denied under `Fail`, admitted under `Ignore`; choose deliberately |
| webhook, `Ignore` | `kubectl -n NS scale deploy/WEBHOOK --replicas=0`, then a violating dry run | admitted; an alert on `apiserver_admission_webhook_fail_open_count` fires |
| webhook, `Fail` | the same with the webhook down | `failed calling webhook` in the scoped namespaces only; `kube-system` and the webhook's namespace unaffected |
| any | `kubectl delete validatingwebhookconfiguration NAME` as cluster-admin | works even while the webhook is down; write it in the runbook |

For webhooks that run with `Fail`, require at least two replicas, a PodDisruptionBudget, anti-affinity across nodes, `timeoutSeconds` of 5 or less, and a priority class so they are scheduled first after a node loss. Check that `rules` list the subresources that matter (`pods/ephemeralcontainers` for `kubectl debug`).

## 7. Report

State: the rule in one sentence; the mechanism chosen and why (row of the table in step 2); the policy and binding as applied (scope, actions, `failurePolicy`); the test table with expected and observed outcomes, offline and with `--dry-run=server`; the warn period's findings (count, owners, exemptions with expiry); the date `Deny` was enabled per namespace; the failure-mode test results from step 6; and any webhook found in step 1 whose scope or failure policy you changed. Never report "enabled" without the failure-mode row.
