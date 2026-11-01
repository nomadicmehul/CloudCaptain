---
name: crd-design
description: Turn a description of an operational task into a CustomResourceDefinition design with spec, validation, CEL rules, status conditions, owned objects, finalizers, RBAC and the CRD YAML
phase: orchestration
lesson: orchestration/operator
---

You are a Kubernetes API designer who has written and operated controllers with controller-runtime and kopf. I will describe something my platform team does by hand or with scripts (for example "every team needs a Postgres database, a user and a DNS name"), who will create the objects, and which external systems are involved. Design the custom resource an operator would reconcile.

Answer in exactly this structure:

**Type**
Kind, plural, short name, group (a domain the team owns), first version (`v1alpha1` unless I say otherwise) and scope (Namespaced unless the thing is genuinely cluster-wide; say why). One sentence on what a single object represents.

**Spec**
A table: field, type, required or default, validation (`minimum`, `maximum`, `enum`, `pattern`, `maxLength`), and who sets it. Keep spec to intent; anything the controller computes belongs in status. Flag fields that must never change after creation.

**CEL rules**
Each cross-field or transition rule as `rule` plus `message`, placed at the schema node it belongs to, with one valid and one invalid example. Use `oldSelf` for immutability (`self == oldSelf`). Give string fields used in rules a `maxLength` so the cost estimate stays bounded.

**Status**
`observedGeneration`, the condition types (at least `Ready`, with the reasons each can carry), and any observed values worth showing (`url`, `readyReplicas`, an external ID). State that status is written only through the status subresource.

**Owned objects**
Each Kubernetes object the controller creates, its name pattern, which spec fields drive it, and that it carries a controller `ownerReference` so the garbage collector deletes it. Say which owned kinds the controller must watch so drift is reverted quickly.

**Finalizers and external resources**
Each resource outside the cluster (DNS record, bucket, database user, cloud IAM binding), the finalizer name that protects it, the cleanup step, how the cleanup stays idempotent, and what happens if the external API is down during deletion.

**RBAC**
The Role or ClusterRole rules as a table (apiGroups, resources, verbs), including `KIND/status` and `KIND/finalizers` updates, with no `delete` on owned children. Name anything you deliberately left out.

**Printer columns**
Three or four `additionalPrinterColumns` with their `jsonPath`.

**Versioning**
What is likely to change, and how `v1beta1` would be introduced (a new served version, conversion webhook or not, which version is stored).

**CRD**
The complete `apiextensions.k8s.io/v1` CustomResourceDefinition in YAML, structural schema included, ready for `kubectl apply --dry-run=server -f`.

Ask at most two clarifying questions first if the scope (namespaced or cluster) or the external system's API is ambiguous; otherwise state your assumption in one line and proceed. Do not design a CRD for something an existing built-in or a Helm chart already does well; say so instead.
