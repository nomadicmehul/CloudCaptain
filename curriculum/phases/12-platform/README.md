# Platform Engineering

*A golden-path generator, then a service catalog, DORA metrics, preview environments and a platform API.*

A platform team's product is the shortest safe path from an idea to production: a template that already made the boring decisions, a catalog that answers who owns what, a preview environment per pull request, an API that hides ten Kubernetes objects behind one, and numbers that show whether delivery is getting faster or just busier. Each of those is a small, buildable mechanism on top of what earlier phases taught. This phase builds them from the standard library and compares each with the tool platform teams adopt.

## How this phase is built

| Lesson | Type | What you build | What you then use |
|---|---|---|---|
| [Golden paths](01-golden-path/docs/en.md) | Build | `scaffolder`: a validated template that generates a whole service repository and updates old services with a three-way merge | Backstage Software Templates, copier, cruft |
| [Service catalog](02-service-catalog/docs/en.md) | Build | `minicatalog`: catalog entities, validation, two-way relations, blast-radius queries and scorecards | the Backstage catalog, Port, Cortex |
| [DORA metrics](03-dora-metrics/docs/en.md) | Build | `minidora`: the four keys from Git history, deployments and incidents, with percentiles and gaming warnings | Four Keys, GitHub deployments |
| [Preview environments](04-preview-environments/docs/en.md) | Build | `minienv`: per-PR namespaces with quotas, network policy, author access, idempotent reconcile and a TTL reaper | ApplicationSet PR generator, vcluster |
| [Platform API](05-platform-api/docs/en.md) | Build | `miniplatform`: one workload spec expanded into Deployment, Service, HPA, PDB, NetworkPolicy and HTTPRoute, with versioned schemas | Score and `score-k8s`, KubeVela, Crossplane |

Further lessons for this phase are proposed in [ROADMAP.md](https://github.com/nomadicmehul/CloudCaptain/blob/main/ROADMAP.md).

## Reference layer

The long-form guide for this phase is the [Platform Engineering learning path](https://cloudcaptain.io/docs/learning-paths/platform-engineering); cost allocation is covered in the Cloud phase's [FOCUS lesson](https://cloudcaptain.io/learn/cloud/finops-focus) and the [FinOps](https://cloudcaptain.io/docs/cloud/finops) reference.
