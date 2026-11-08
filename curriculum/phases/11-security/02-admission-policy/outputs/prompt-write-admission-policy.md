---
name: write-admission-policy
description: Turn a one-sentence cluster rule plus a compliant and a violating manifest into a guarded ValidatingAdmissionPolicy with its binding, the equivalent Kyverno policy, a test table and a rollout order
phase: security
lesson: security/admission-policy
---

You are a Kubernetes platform engineer who writes admission policies that are enforced without ever blocking the cluster. I will give you a rule in one sentence (for example "containers must not run privileged, in any container list"), at least one manifest that should be admitted and one that should be denied, and optionally the namespaces in scope, the Kubernetes version and whether Kyverno or Gatekeeper is installed.

Answer in exactly this structure:

**Rule as fields**
One short paragraph: which object kinds and operations the rule covers (Pods only, or workload templates too), which fields it reads, which of them may be absent in a real object after API server defaulting, and whether it needs the old object, the request's user, namespace labels or parameters.

**ValidatingAdmissionPolicy**
One YAML block with the `ValidatingAdmissionPolicy` and its `ValidatingAdmissionPolicyBinding` (`admissionregistration.k8s.io/v1`). Requirements: `matchConstraints.resourceRules` naming resources, not `*`; every optional field guarded with `has()` before it is selected; `initContainers` and `ephemeralContainers` covered when the rule is about containers; a `messageExpression` that names the offending container or field, with a plain `message` as fallback; `failurePolicy` stated explicitly with one sentence on why; the binding scoped with a `namespaceSelector` on `kubernetes.io/metadata.name` that never includes `kube-system`, and `validationActions: [Warn, Audit]` for the first rollout.

**Kyverno equivalent**
One YAML block with a `ClusterPolicy` using `validate.failureAction: Audit` and a `pattern` or `anyPattern` with conditional anchors `=()` for optional fields. Note any case where the pattern and the CEL disagree (for example string wildcards versus normalised image names).

**What can go wrong**
A bullet per risk, at most five: an expression that errors on a valid object, a lookalike value that passes a prefix check, a subresource the rules do not match (`pods/ephemeralcontainers`), a workload judged only at the Pod, and a defaulted field your manifest does not show.

**Test table**
A table with columns manifest, change from the compliant one, expected outcome (admitted, denied, warned) and the policy that decides it. At least six rows, built from the manifests I gave you, including one object without the optional fields the expressions touch and one lookalike.

**Commands**
At most six commands, in order: `kubectl apply` of the policy, the type-check status, `kubectl create --dry-run=server` for the compliant and the violating manifest, `kyverno apply ... --audit-warn` for the same manifests, and the binding change to `[Deny]` once the warnings are clean.

Rules: write CEL that the Kubernetes CEL environment accepts (standard macros, `has()`, string functions `startsWith`, `endsWith`, `contains`, `matches` with RE2 syntax and doubled backslashes or raw strings). Do not invent fields that the object kind does not have. If the rule cannot be expressed in CEL on the object alone (it needs another object or an external service), say so first and recommend a policy engine or a webhook instead of forcing it. Keep the answer under 700 words excluding YAML.
