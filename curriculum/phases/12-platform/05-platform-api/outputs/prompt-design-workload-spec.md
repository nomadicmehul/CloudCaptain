---
name: design-workload-spec
description: Turn a description of a team's services and the platform team's existing decisions into a minimal, versioned developer-facing workload spec with defaults, generated objects, CEL rules, an escape hatch and examples
phase: platform
lesson: platform/platform-api
---

You are a platform engineer who has designed developer-facing abstractions on Kubernetes with Score, KubeVela, Crossplane and home-grown CRDs, and who has had to support them for years afterwards. I will describe the services my teams run (languages, traffic, dependencies such as databases, queues and other services), what the platform team already decides in code review (security context, probes, resources, autoscaling, disruption budgets, network policy, routing), and the tools we use to deploy (kubectl, Helm, Argo CD, Flux, a controller). Design the workload spec developers will write.

Answer in exactly this structure:

**Vocabulary**
One sentence on what a single spec represents and who writes it. Then a table: field, type, required or default, validation (enum, bounds, pattern, `maxLength`), owner (developer or platform), and why it exists. Keep it under fifteen developer-owned fields. For each field you left out on purpose (security context, probe timing, labels, PDB policy, CPU limits), name the platform decision that replaces it.

**Defaults table**
Every value the translator sets without asking: pod and container security context, service account and token, probes, resources per size, replicas or HPA, PodDisruptionBudget rule, NetworkPolicy shape, route attachment (Gateway name, namespace, listener). One line of reasoning per row.

**Objects per field**
A table: spec field, generated objects, and the fields in them it drives. State explicitly that `spec.replicas` is absent from the Deployment whenever autoscaling is on, and when a PodDisruptionBudget is rendered.

**Dependencies**
Each dependency type, the outputs it provides (host, port, credentials by Secret reference), the environment variables the workload gets, the objects or cloud resources it provisions in development and in production, and the NetworkPolicy rule it implies for the caller and the callee. Use the Service port, never the target port, in URLs.

**Validation**
The schema rules, then each cross-field rule as CEL (`rule` plus `message`) at the node it belongs to, with one valid and one invalid example. Give every string a `maxLength` and every list a `maxItems` so the API server can bound the rules' cost. Model key/value lists as lists with `x-kubernetes-list-type: map`, not as maps. List the rules that cannot be CEL (anything inside a free-form patch, anything that needs rendering) and say where they run instead.

**Versioning**
The first version name, the hub version, what is likely to change, how a field that an older version cannot express is preserved across a round trip (an annotation, by convention), and whether a conversion webhook is needed (any served versions with different schemas need one; `strategy: None` only rewrites `apiVersion`).

**Escape hatch**
How a team patches generated objects for a need the schema lacks (merge patch by kind, Score patch templates, a KubeVela trait, a Crossplane function), the guarded paths that stay platform-owned, and how a team leaves the abstraction entirely with the rendered YAML.

**Change behaviour**
For the four most common edits (image, size, fixed to autoscaled, route path), what changes in the generated objects, whether pods restart, and any order of operations required. Include the HPA replicas handover.

**Examples**
Two complete specs in the format you designed: a stateless web service with a public route and fixed replicas, and an autoscaled API with a database dependency that calls another service. Then the full rendered output for the second one as YAML, ready for `kubectl apply --dry-run=server -f`.

If my description leaves the deploy tool, the Gateway implementation or the database offering unclear, ask at most two questions; otherwise state your assumption in one line and proceed. If one of my teams' needs is better served by a plain Helm chart or an existing operator, say so instead of adding a field.
