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
# Add a repository
helm repo add bitnami https://charts.bitnami.com/bitnami

# Search for charts
helm search repo nginx

# Install a chart
helm install my-release bitnami/nginx

# List releases
helm list

# Upgrade a release
helm upgrade my-release bitnami/nginx --set replicaCount=3

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
| [Create Your First Helm Chart](https://docs.bitnami.com/kubernetes/how-to/create-your-first-helm-chart/) | Bitnami guide on authoring charts |
| [Authoring Awesome Charts](https://github.com/helm/helm-classic/blob/master/docs/awesome.md) | Official Helm guide on chart authoring |
| [Kompose Guide](https://kubernetes.io/docs/tasks/configure-pod-container/translate-compose-kubernetes/) | Translate docker-compose to Helm |

### Chart Repositories

| Repository | Description |
|:-----------|:------------|
| [Helm Hub](https://hub.helm.sh) | Official Helm Hub |
| [Bitnami Charts](https://charts.bitnami.com/bitnami) | Popular application charts |
| [Kubeapps](https://hub.kubeapps.com/) | Helm chart discovery hub |
| [ChartCenter](https://chartcenter.io) | Central Helm chart repository by JFrog |
| [Cloudsmith](https://cloudsmith.io/l/helm-repository/) | Managed package repository with free tier |
| [Fairwinds Charts](https://hub.helm.sh/charts/fairwinds-stable) | Fairwinds chart hub |

### Helm Plugins

| Plugin | Description |
|:-------|:------------|
| [Helm Diff](https://github.com/databus23/helm-diff) | Show diff of helm upgrade/rollback |
| [Helm Secrets](https://github.com/jkroepke/helm-secrets) | Manage secrets safely |
| [Helm S3](https://github.com/hypnoglow/helm-s3) | Fetch charts from S3 |
| [Helm GCS](https://github.com/hayorov/helm-gcs) | Manage charts on Google Cloud Storage |
| [Helm Datree](https://github.com/datreeio/helm-datree) | Enforce best practices and policies |
| [Helm Schema Gen](https://github.com/karuppiah7890/helm-schema-gen) | Generate values.schema.json |
| [Helm Teller](https://github.com/SpectralOps/helm-teller) | Manage deployment configuration securely |

### Helm Tools

| Tool | Description |
|:-----|:------------|
| [Helmfile](https://github.com/roboll/helmfile) | Declarative spec for deploying Helm charts |
| [Helmsman](https://github.com/Praqma/helmsman) | Terraform-like desired state for Helm |
| [Reckoner](https://github.com/FairwindsOps/reckoner) | Simplify multiple Helm releases |
| [Monocular](https://github.com/helm/monocular) | Web UI for searching charts |
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
