---
title: "Helm"
description: "Kubernetes package manager"
---

# Helm

The package manager for Kubernetes — simplify deployments with reusable charts.

## Key Concepts

- **Chart** — A package of pre-configured K8s resources
- **Release** — An instance of a chart running in a cluster
- **Repository** — A collection of charts
- **Values** — Configuration parameters for customization

## Essential Commands

```bash
# Add a repository (classic index) and search it
helm repo add prometheus-community https://prometheus-community.github.io/helm-charts
helm search repo prometheus-community

# Search Artifact Hub from the CLI
helm search hub podinfo

# Install a chart straight from an OCI registry (Helm 3.8+ / Helm 4)
helm install my-release oci://ghcr.io/stefanprodan/charts/podinfo

# List releases
helm list

# Upgrade a release
helm upgrade my-release oci://ghcr.io/stefanprodan/charts/podinfo --set replicaCount=3

# Rollback
helm rollback my-release 1

# Uninstall
helm uninstall my-release
```

## Creating Your Own Chart

```bash
# Create chart scaffold
helm create my-chart

# Chart structure
my-chart/
  Chart.yaml          # Chart metadata
  values.yaml         # Default values
  templates/          # K8s manifest templates
    deployment.yaml
    service.yaml
    ingress.yaml
    _helpers.tpl      # Template helpers
```

## Best Practices

- Use semantic versioning for charts
- Document all values in `values.yaml`
- Use `_helpers.tpl` for reusable template logic
- Test charts with `helm lint` and `helm template`
- Store charts in a chart repository (ChartMuseum, OCI)

## Learning Resources

### Getting Started

| Resource | Description |
|:---------|:------------|
| [Helm Documentation](https://helm.sh/docs/) | Official Helm documentation |
| [Chart Template Guide](https://helm.sh/docs/chart_template_guide/getting_started/) | Official walkthrough for authoring your first chart |
| [Chart Best Practices](https://helm.sh/docs/chart_best_practices/) | Official conventions for values, templates and dependencies |
| [Kompose Guide](https://kubernetes.io/docs/tasks/configure-pod-container/translate-compose-kubernetes/) | Translate docker-compose to Helm |

### Chart Repositories

| Repository | Description |
|:-----------|:------------|
| [Artifact Hub](https://artifacthub.io/) | CNCF-hosted search across public Helm repositories and OCI registries |
| [prometheus-community](https://github.com/prometheus-community/helm-charts) | Prometheus, Alertmanager, kube-prometheus-stack |
| [Grafana Charts](https://github.com/grafana/helm-charts) | Grafana, Loki, Tempo, Mimir |
| [Cloudsmith](https://cloudsmith.io/l/helm-repository/) | Managed package repository with free tier |
| [Fairwinds Charts](https://artifacthub.io/packages/search?org=fairwinds) | Polaris, Goldilocks, RBAC Manager |

### Helm Plugins

| Plugin | Description |
|:-------|:------------|
| [Helm Diff](https://github.com/databus23/helm-diff) | Show diff of helm upgrade/rollback |
| [Helm Secrets](https://github.com/jkroepke/helm-secrets) | Manage secrets safely |
| [Helm S3](https://github.com/hypnoglow/helm-s3) | Fetch charts from S3 |
| [Helm GCS](https://github.com/hayorov/helm-gcs) | Manage charts on Google Cloud Storage |
| [Helm Schema Gen](https://github.com/karuppiah7890/helm-schema-gen) | Generate values.schema.json |

### Helm Tools

| Tool | Description |
|:-----|:------------|
| [Helmfile](https://github.com/helmfile/helmfile) | Declarative spec for deploying Helm charts |
| [Helmsman](https://github.com/Praqma/helmsman) | Terraform-like desired state for Helm |
| [Reckoner](https://github.com/FairwindsOps/reckoner) | Simplify multiple Helm releases |
| [ChartMuseum](https://chartmuseum.com/) | Self-hosted Helm chart repository |
| [Helmify](https://github.com/arttor/helmify) | Generate Helm charts from K8s YAML |
| [Helm Docs](https://github.com/norwoodj/helm-docs) | Auto-generate chart documentation |
| [werf](https://werf.io/) | CLI tool for CI/CD with extended Helm |

### Useful Applications

| Application | Repository |
|:------------|:-----------|
| GitLab Omnibus | [charts.gitlab.io](https://charts.gitlab.io) |
| JupyterHub | [jupyterhub.github.io/helm-chart](https://jupyterhub.github.io/helm-chart/) |
| Harbor Registry | [github.com/goharbor/harbor-helm](https://github.com/goharbor/harbor-helm) |
| OpenStack | [github.com/openstack/openstack-helm](https://github.com/openstack/openstack-helm) |
| Elasticsearch/Kibana | [github.com/elastic/helm-charts](https://github.com/elastic/helm-charts/) |
| Kafka | [github.com/Landoop/kafka-helm-charts](https://github.com/Landoop/kafka-helm-charts) |

### Community

| Channel | Link |
|:--------|:-----|
| Slack | [#helm-users on K8s Slack](http://slack.k8s.io/) |
| Stack Overflow | [kubernetes-helm tag](https://stackoverflow.com/questions/tagged/kubernetes-helm) |

## Hook Lifecycle

Helm provides hooks to intervene at specific lifecycle points:

| Hook | Timing |
|:-----|:--------|
| pre-install | After templates render, before resources created |
| post-install | After all resources loaded into Kubernetes |
| pre-delete | Before deletion request resources deleted |
| post-delete | After release resources deleted |
| pre-upgrade | After templates render, before update |
| post-upgrade | After resources upgraded |
| pre-rollback | After templates render, before rollback |
| post-rollback | After resources modified |
| test | When Helm test subcommand invoked |
