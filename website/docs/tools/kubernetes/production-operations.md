---
title: "Kubernetes in Production"
sidebar_label: "Production & Operations"
description: "Run Kubernetes in production — CI/CD pipelines, monitoring, logging, Helm, disaster recovery, and best practices"
sidebar_position: 8
---

# Kubernetes in Production

Running Kubernetes in production requires careful planning around deployment strategies, observability, disaster recovery, and cost optimization. This guide covers operational best practices and tools essential for production workloads.

## Production Readiness Checklist

Before deploying to production, verify all of the following:

- **Infrastructure**
  - [ ] Multi-zone or multi-region cluster for high availability
  - [ ] Node autoscaling configured with appropriate limits
  - [ ] Persistent volume provisioning tested
  - [ ] Network policies defined for pod communication
  - [ ] Cluster RBAC and audit logging enabled

- **Application**
  - [ ] Health checks defined (liveness, readiness, startup probes)
  - [ ] Resource requests and limits set for all containers
  - [ ] Graceful shutdown handling implemented (SIGTERM)
  - [ ] Secrets management in place (not hardcoded in images)
  - [ ] Container images scanned for vulnerabilities

- **Observability**
  - [ ] Metrics collection (Prometheus) running
  - [ ] Log aggregation (ELK/EFK) deployed
  - [ ] Monitoring dashboards (Grafana) configured
  - [ ] Alerting rules for critical events active
  - [ ] Distributed tracing setup (Jaeger/Zipkin) optional but recommended

- **Reliability**
  - [ ] Pod Disruption Budgets defined
  - [ ] Backup and restore procedures tested
  - [ ] Disaster recovery runbook documented
  - [ ] Incident response plan in place
  - [ ] Regular drills scheduled

- **Security**
  - [ ] Network policies enforced
  - [ ] RBAC roles reviewed and applied
  - [ ] Pod Security Policies/Standards enabled
  - [ ] Secrets encrypted at rest
  - [ ] Container registries private and authenticated

- **Operations**
  - [ ] GitOps workflow established
  - [ ] Standard deployment procedures documented
  - [ ] Rollback procedures tested
  - [ ] On-call rotation and runbooks ready
  - [ ] Cost monitoring tools in place

---

## CI/CD with Kubernetes

### Pipeline Architecture

A typical Kubernetes CI/CD pipeline follows this flow:

```
Code Push → Build → Test → Build Image → Push Registry → Deploy to K8s
                                                              ↓
                                                    Health Checks
                                                              ↓
                                                    Monitor & Alert
```

### Pipeline Stages

**1. Build Stage**
```bash
# Compile code, run unit tests
./gradlew build
npm run test
```

**2. Container Image Stage**
```dockerfile
# Dockerfile
FROM node:18-alpine AS builder
WORKDIR /app
COPY package*.json ./
RUN npm ci --only=production

FROM node:18-alpine
WORKDIR /app
COPY --from=builder /app/node_modules ./node_modules
COPY . .
EXPOSE 3000
HEALTHCHECK --interval=30s --timeout=3s CMD curl -f http://localhost:3000/health || exit 1
CMD ["node", "server.js"]
```

**3. Registry Push**
```bash
# Tag and push to container registry
docker build -t myapp:${CI_COMMIT_SHA} .
docker push registry.example.com/myapp:${CI_COMMIT_SHA}
```

**4. Kubernetes Deployment**
```bash
# Deploy using kubectl or Helm
kubectl set image deployment/myapp \
  myapp=registry.example.com/myapp:${CI_COMMIT_SHA}
```

### Deployment Strategies

#### Rolling Deployment (Default)
Gradually replace old pods with new ones. No downtime but slow rollout.

```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: myapp
spec:
  replicas: 3
  strategy:
    type: RollingUpdate
    rollingUpdate:
      maxSurge: 1        # One extra pod during rollout
      maxUnavailable: 0  # Zero pods down at any time
  selector:
    matchLabels:
      app: myapp
  template:
    metadata:
      labels:
        app: myapp
    spec:
      containers:
      - name: myapp
        image: myapp:v2
        readinessProbe:
          httpGet:
            path: /ready
            port: 8080
          initialDelaySeconds: 5
          periodSeconds: 5
```

#### Blue/Green Deployment
Run two identical environments. Switch traffic instantly.

```yaml
---
# Blue deployment (current)
apiVersion: apps/v1
kind: Deployment
metadata:
  name: myapp-blue
spec:
  replicas: 3
  selector:
    matchLabels:
      app: myapp
      version: blue
  template:
    metadata:
      labels:
        app: myapp
        version: blue
    spec:
      containers:
      - name: myapp
        image: myapp:v1

---
# Green deployment (new)
apiVersion: apps/v1
kind: Deployment
metadata:
  name: myapp-green
spec:
  replicas: 3
  selector:
    matchLabels:
      app: myapp
      version: green
  template:
    metadata:
      labels:
        app: myapp
        version: green
    spec:
      containers:
      - name: myapp
        image: myapp:v2

---
# Service routes to blue initially
apiVersion: v1
kind: Service
metadata:
  name: myapp
spec:
  selector:
    app: myapp
    version: blue  # Switch to 'green' for instant cutover
  ports:
  - port: 80
    targetPort: 8080
```

#### Canary Deployment
Route percentage of traffic to new version, gradually increase.

```yaml
# Using Istio VirtualService for traffic splitting
apiVersion: networking.istio.io/v1beta1
kind: VirtualService
metadata:
  name: myapp
spec:
  hosts:
  - myapp
  http:
  - match:
    - uri:
        prefix: /
    route:
    - destination:
        host: myapp
        subset: v1
      weight: 95  # 95% to old version
    - destination:
        host: myapp
        subset: v2
      weight: 5   # 5% to new version
    timeout: 30s
    retries:
      attempts: 3
      perTryTimeout: 10s
---
apiVersion: networking.istio.io/v1beta1
kind: DestinationRule
metadata:
  name: myapp
spec:
  host: myapp
  trafficPolicy:
    connectionPool:
      tcp:
        maxConnections: 100
      http:
        http1MaxPendingRequests: 100
        maxRequestsPerConnection: 2
  subsets:
  - name: v1
    labels:
      version: v1
  - name: v2
    labels:
      version: v2
```

### GitOps with ArgoCD

GitOps treats Git as the single source of truth for cluster state. ArgoCD syncs the cluster to match Git.

**Install ArgoCD:**
```bash
kubectl create namespace argocd
kubectl apply -n argocd -f https://raw.githubusercontent.com/argoproj/argo-cd/stable/manifests/install.yaml

# Port forward to access UI
kubectl port-forward -n argocd svc/argocd-server 8080:443
```

**Create ArgoCD Application:**
```yaml
apiVersion: argoproj.io/v1alpha1
kind: Application
metadata:
  name: myapp
  namespace: argocd
spec:
  project: default

  source:
    repoURL: https://github.com/myorg/myapp
    targetRevision: HEAD
    path: k8s/overlays/production
    # Optional: Helm chart
    chart: myapp
    helm:
      values: |
        replicas: 3
        image:
          tag: v1.2.3

  destination:
    server: https://kubernetes.default.svc
    namespace: default

  syncPolicy:
    automated:
      prune: true      # Delete resources not in Git
      selfHeal: true   # Auto-sync if cluster drifts
    syncOptions:
    - CreateNamespace=true
    retry:
      limit: 5
      backoff:
        duration: 5s
        factor: 2
        maxDuration: 3m
```

**GitOps Workflow:**
```bash
# Developer updates deployment in Git
git commit -m "Update myapp image to v1.2.3"
git push origin main

# ArgoCD detects change and syncs to cluster
argocd app get myapp
argocd app sync myapp
```

### GitOps with Flux

Flux is a declarative GitOps operator that keeps cluster state in sync with Git.

**Install Flux:**
```bash
curl -s https://fluxcd.io/install.sh | sudo bash
flux bootstrap github \
  --owner=myorg \
  --repo=fleet \
  --branch=main \
  --path=clusters/production \
  --personal
```

**Define Kustomization (Git source tracking):**
```yaml
apiVersion: source.toolkit.fluxcd.io/v1beta2
kind: GitRepository
metadata:
  name: myapp
  namespace: flux-system
spec:
  interval: 1m
  url: https://github.com/myorg/myapp
  ref:
    branch: main

---
apiVersion: kustomize.toolkit.fluxcd.io/v1
kind: Kustomization
metadata:
  name: myapp
  namespace: flux-system
spec:
  interval: 10m
  sourceRef:
    kind: GitRepository
    name: myapp
  path: ./k8s/overlays/production
  prune: true
  wait: true
  timeout: 5m
```

---

## Helm Package Manager

### What is Helm?

Helm is the package manager for Kubernetes. A Helm chart is a collection of YAML templates that define a Kubernetes application.

**Key Concepts:**
- **Chart**: A package containing templates, values, and metadata
- **Release**: An instance of a chart deployed to a cluster
- **Repository**: A collection of charts

### Basic Helm Commands

```bash
# Add Helm repository
helm repo add bitnami https://charts.bitnami.com/bitnami
helm repo update

# List available charts
helm search repo bitnami

# Install a release
helm install my-postgres bitnami/postgresql \
  --values values.yaml \
  --namespace database \
  --create-namespace

# List releases
helm list -n database

# Upgrade a release
helm upgrade my-postgres bitnami/postgresql \
  --values values.yaml

# Rollback to previous release
helm rollback my-postgres 1

# Get release values
helm get values my-postgres

# Uninstall a release
helm uninstall my-postgres
```

### Creating a Helm Chart

**Directory structure:**
```
myapp/
├── Chart.yaml                 # Chart metadata
├── values.yaml               # Default configuration values
├── values-prod.yaml          # Production overrides
├── templates/
│   ├── deployment.yaml
│   ├── service.yaml
│   ├── configmap.yaml
│   ├── secret.yaml
│   ├── ingress.yaml
│   ├── _helpers.tpl          # Template helpers
│   └── NOTES.txt             # Installation notes
└── charts/                   # Dependency charts
```

**Chart.yaml:**
```yaml
apiVersion: v2
name: myapp
description: A Helm chart for deploying myapp
type: application
version: 1.0.0
appVersion: "1.2.3"
maintainers:
  - name: DevOps Team
    email: devops@example.com
dependencies:
  - name: postgresql
    version: "12.0.0"
    repository: "https://charts.bitnami.com/bitnami"
```

**values.yaml (defaults):**
```yaml
replicaCount: 3

image:
  repository: myapp
  tag: "1.0.0"
  pullPolicy: IfNotPresent

service:
  type: ClusterIP
  port: 80
  targetPort: 8080

ingress:
  enabled: true
  className: nginx
  hosts:
    - host: myapp.example.com
      paths:
        - path: /
          pathType: Prefix

resources:
  limits:
    cpu: 500m
    memory: 512Mi
  requests:
    cpu: 250m
    memory: 256Mi

autoscaling:
  enabled: true
  minReplicas: 2
  maxReplicas: 10
  targetCPUUtilizationPercentage: 80

postgresql:
  auth:
    username: myapp
    password: changeme
  primary:
    persistence:
      size: 10Gi
```

**templates/deployment.yaml:**
```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: {{ include "myapp.fullname" . }}
  labels:
    {{- include "myapp.labels" . | nindent 4 }}
spec:
  {{- if not .Values.autoscaling.enabled }}
  replicas: {{ .Values.replicaCount }}
  {{- end }}
  selector:
    matchLabels:
      {{- include "myapp.selectorLabels" . | nindent 6 }}
  template:
    metadata:
      labels:
        {{- include "myapp.selectorLabels" . | nindent 8 }}
    spec:
      containers:
      - name: {{ .Chart.Name }}
        image: "{{ .Values.image.repository }}:{{ .Values.image.tag }}"
        imagePullPolicy: {{ .Values.image.pullPolicy }}
        ports:
        - name: http
          containerPort: {{ .Values.service.targetPort }}
          protocol: TCP
        livenessProbe:
          httpGet:
            path: /health
            port: http
          initialDelaySeconds: 30
          periodSeconds: 10
        readinessProbe:
          httpGet:
            path: /ready
            port: http
          initialDelaySeconds: 5
          periodSeconds: 5
        resources:
          {{- toYaml .Values.resources | nindent 12 }}
        env:
        - name: DB_HOST
          value: "{{ include \"myapp.fullname\" . }}-postgresql"
        - name: DB_PORT
          value: "5432"
```

**templates/_helpers.tpl:**
```yaml
{{/*
Expand the name of the chart.
*/}}
{{- define "myapp.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/*
Create a default fully qualified app name.
*/}}
{{- define "myapp.fullname" -}}
{{- if .Values.fullnameOverride }}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" }}
{{- else }}
{{- $name := default .Chart.Name .Values.nameOverride }}
{{- if contains $name .Release.Name }}
{{- .Release.Name | trunc 63 | trimSuffix "-" }}
{{- else }}
{{- printf "%s-%s" .Release.Name $name | trunc 63 | trimSuffix "-" }}
{{- end }}
{{- end }}
{{- end }}

{{/*
Common labels
*/}}
{{- define "myapp.labels" -}}
helm.sh/chart: {{ include "myapp.chart" . }}
{{ include "myapp.selectorLabels" . }}
{{- end }}

{{/*
Selector labels
*/}}
{{- define "myapp.selectorLabels" -}}
app.kubernetes.io/name: {{ include "myapp.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end }}
```

**Helm Best Practices:**
- Use semantic versioning for chart and app versions
- Document required values with comments
- Provide values overrides for different environments (dev, staging, prod)
- Use named templates (`_helpers.tpl`) to reduce duplication
- Always include resource requests and limits
- Test charts with `helm lint` and `helm template`
- Use `--dry-run` to preview changes: `helm upgrade --dry-run --debug`
- Store Helm chart repositories in version control

```bash
# Lint chart for errors
helm lint ./myapp

# Preview rendered templates
helm template myapp ./myapp --values values-prod.yaml

# Dry run deployment
helm upgrade --install myapp ./myapp --dry-run --debug
```

---

## Monitoring & Observability

### Prometheus + Grafana Stack

**Install Prometheus & Grafana via Helm:**
```bash
helm repo add prometheus-community https://prometheus-community.github.io/helm-charts
helm repo update

helm install prometheus prometheus-community/kube-prometheus-stack \
  --namespace monitoring \
  --create-namespace \
  --values prometheus-values.yaml
```

**prometheus-values.yaml:**
```yaml
prometheus:
  prometheusSpec:
    retention: 30d
    resources:
      requests:
        cpu: 500m
        memory: 2Gi
      limits:
        cpu: 2000m
        memory: 4Gi
    storageSpec:
      volumeClaimTemplate:
        spec:
          storageClassName: fast-ssd
          accessModes: ["ReadWriteOnce"]
          resources:
            requests:
              storage: 50Gi

grafana:
  enabled: true
  adminPassword: changeme
  persistence:
    enabled: true
    size: 10Gi

alertmanager:
  enabled: true
  config:
    global:
      resolve_timeout: 5m
    route:
      group_by: ['alertname', 'cluster', 'service']
      group_wait: 10s
      group_interval: 10s
      repeat_interval: 12h
      receiver: 'devops-team'
    receivers:
    - name: 'devops-team'
      slack_configs:
      - api_url: 'https://hooks.slack.com/services/YOUR/WEBHOOK/URL'
        channel: '#alerts'
```

**Service Monitor (Scrape Config):**
```yaml
apiVersion: monitoring.coreos.com/v1
kind: ServiceMonitor
metadata:
  name: myapp
  namespace: default
spec:
  selector:
    matchLabels:
      app: myapp
  endpoints:
  - port: metrics
    interval: 30s
    path: /metrics
```

**PrometheusRule (Alerts):**
```yaml
apiVersion: monitoring.coreos.com/v1
kind: PrometheusRule
metadata:
  name: myapp-alerts
  namespace: monitoring
spec:
  groups:
  - name: myapp
    interval: 30s
    rules:
    - alert: HighErrorRate
      expr: rate(http_requests_total{status=~"5.."}[5m]) > 0.05
      for: 10m
      annotations:
        summary: "High error rate detected"
        description: "Error rate is {{ $value | humanizePercentage }} for {{ $labels.job }}"

    - alert: PodRestartingTooOften
      expr: rate(kube_pod_container_status_restarts_total[1h]) > 5
      for: 5m
      annotations:
        summary: "Pod {{ $labels.pod }} restarting frequently"
        description: "Pod has restarted {{ $value | humanize }} times in the last hour"

    - alert: HighMemoryUsage
      expr: (sum(container_memory_usage_bytes) by (pod) / sum(container_spec_memory_limit_bytes) by (pod)) > 0.9
      for: 5m
      annotations:
        summary: "Pod {{ $labels.pod }} using {{ $value | humanizePercentage }} of memory limit"
```

### Key Metrics to Monitor

| Metric | Query | Threshold | Action |
|--------|-------|-----------|--------|
| Pod Restarts | `rate(kube_pod_container_status_restarts_total[1h])` | > 5/hour | Investigate container crashes |
| CPU Usage | `sum(rate(container_cpu_usage_seconds_total[5m])) by (pod)` | > 80% limit | Scale or optimize |
| Memory Usage | `sum(container_memory_usage_bytes) by (pod)` | > 90% limit | Increase limit or leak check |
| Request Latency | `histogram_quantile(0.95, rate(http_request_duration_seconds_bucket[5m]))` | > 1s p95 | Optimize app or infra |
| Error Rate | `rate(http_requests_total{status=~"5.."}[5m])` | > 1% | Check logs and alerts |
| Disk Usage | `kubelet_volume_stats_used_bytes / kubelet_volume_stats_capacity_bytes` | > 80% | Expand PV or cleanup |

### Kubernetes Dashboard

Deploy the Kubernetes Dashboard for visual cluster management:

```bash
kubectl apply -f https://raw.githubusercontent.com/kubernetes/dashboard/v2.7.0/aio/deploy/recommended.yaml

# Create admin user
kubectl apply -f - <<EOF
apiVersion: v1
kind: ServiceAccount
metadata:
  name: admin-user
  namespace: kubernetes-dashboard
---
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRoleBinding
metadata:
  name: admin-user
roleRef:
  apiGroup: rbac.authorization.k8s.io
  kind: ClusterRole
  name: cluster-admin
subjects:
- kind: ServiceAccount
  name: admin-user
  namespace: kubernetes-dashboard
EOF

# Get access token
kubectl -n kubernetes-dashboard create token admin-user

# Port forward
kubectl proxy &
# Access at http://localhost:8001/api/v1/namespaces/kubernetes-dashboard/services/https:kubernetes-dashboard:/proxy/
```

---

## Logging

### EFK Stack (Elasticsearch, Fluentd, Kibana)

**Architecture:**
```
Containers → Fluentd (DaemonSet) → Elasticsearch → Kibana (UI)
```

**Install via Helm:**
```bash
helm repo add elastic https://helm.elastic.co
helm repo update

# Install Elasticsearch
helm install elasticsearch elastic/elasticsearch \
  --namespace logging \
  --create-namespace \
  --set replicas=3 \
  --set volumeClaimTemplate.storageClassName=fast-ssd

# Install Kibana
helm install kibana elastic/kibana \
  --namespace logging \
  --set elasticsearchHosts=http://elasticsearch-master:9200

# Install Fluent Bit
helm install fluent-bit fluent/fluent-bit \
  --namespace logging \
  --values fluent-bit-values.yaml
```

**fluent-bit-values.yaml:**
```yaml
image:
  repository: fluent/fluent-bit
  tag: 2.1.0

serviceAccount:
  create: true

rbac:
  create: true

podSecurityPolicy:
  create: true

config:
  service: |
    [SERVICE]
        Flush         5
        Log_Level     info
        Daemon        off

  inputs: |
    [INPUT]
        Name              tail
        Path              /var/log/containers/*/*.log
        Parser            docker
        Tag               kubernetes.*
        Mem_Buf_Limit     5MB
        Skip_Long_Lines   On

    [INPUT]
        Name            systemd
        Tag             host.*
        Systemd_Filter  _SYSTEMD_UNIT=kubelet.service

  filters: |
    [FILTER]
        Name                kubernetes
        Match               kubernetes.*
        Kube_URL            https://kubernetes.default.svc:443
        Kube_CA_File        /var/run/secrets/kubernetes.io/serviceaccount/ca.crt
        Kube_Token_File     /var/run/secrets/kubernetes.io/serviceaccount/token

  outputs: |
    [OUTPUT]
        Name            es
        Match           *
        Host            ${ELASTICSEARCH_HOST}
        Port            ${ELASTICSEARCH_PORT}
        Logstash_Format On
        Logstash_Prefix kubernetes-cluster
        Retry_Limit     5
        Type            _doc
```

### Centralized Logging with Loki

**Install Loki (lighter weight than EFK):**
```bash
helm repo add grafana https://grafana.github.io/helm-charts
helm repo update

helm install loki grafana/loki-stack \
  --namespace logging \
  --create-namespace \
  --values loki-values.yaml
```

**loki-values.yaml:**
```yaml
loki:
  auth_enabled: false
  ingester:
    chunk_idle_period: 3m
    chunk_retain_period: 1m
    max_chunk_age: 1h
    chunk_encoding: snappy
  limits_config:
    enforce_metric_name: false
    reject_old_samples: true
    reject_old_samples_max_age: 168h
  schema_config:
    configs:
    - from: 2020-10-24
      store: boltdb-shipper
      object_store: filesystem
      schema:
        version: v11
        index:
          prefix: index_
          period: 24h

promtail:
  enabled: true
  config:
    clients:
    - url: http://loki:3100/loki/api/v1/push

grafana:
  enabled: true
  persistence:
    enabled: true
    size: 10Gi

prometheus:
  enabled: true
  server:
    persistentVolume:
      enabled: true
      size: 10Gi
```

### Debugging with kubectl logs

```bash
# View logs from a pod
kubectl logs pod-name

# Follow logs (tail -f)
kubectl logs -f pod-name

# View logs from specific container in multi-container pod
kubectl logs pod-name -c container-name

# View logs from previous instance (if pod restarted)
kubectl logs pod-name --previous

# View logs from all pods in a deployment
kubectl logs -l app=myapp --tail=100

# Filter logs with grep
kubectl logs pod-name | grep ERROR

# Export logs to file
kubectl logs pod-name > pod-logs.txt
```

---

## Disaster Recovery

### etcd Backup and Restore

etcd stores all Kubernetes cluster state. Regular backups are critical.

**Backup etcd:**
```bash
# Snapshot database
ETCDCTL_API=3 etcdctl \
  --endpoints=https://127.0.0.1:2379 \
  --cacert=/etc/kubernetes/pki/etcd/ca.crt \
  --cert=/etc/kubernetes/pki/etcd/server.crt \
  --key=/etc/kubernetes/pki/etcd/server.key \
  snapshot save /backup/etcd-backup-$(date +%Y-%m-%d-%H%M%S).db

# Verify backup
ETCDCTL_API=3 etcdctl \
  --cacert=/etc/kubernetes/pki/etcd/ca.crt \
  --cert=/etc/kubernetes/pki/etcd/server.crt \
  --key=/etc/kubernetes/pki/etcd/server.key \
  snapshot status /backup/etcd-backup.db
```

**Restore etcd:**
```bash
# Stop API server and etcd
sudo systemctl stop kube-apiserver
sudo systemctl stop etcd

# Restore snapshot
ETCDCTL_API=3 etcdctl \
  --data-dir=/var/lib/etcd-backup \
  snapshot restore /backup/etcd-backup.db

# Update etcd data directory
sudo mv /var/lib/etcd /var/lib/etcd-backup-old
sudo mv /var/lib/etcd-backup /var/lib/etcd

# Restart services
sudo systemctl start etcd
sudo systemctl start kube-apiserver
```

**Automated etcd Backup with CronJob:**
```yaml
apiVersion: batch/v1
kind: CronJob
metadata:
  name: etcd-backup
  namespace: kube-system
spec:
  schedule: "0 2 * * *"  # Daily at 2 AM
  jobTemplate:
    spec:
      template:
        spec:
          serviceAccountName: etcd-backup
          containers:
          - name: etcd-backup
            image: bitnami/etcd:latest
            command:
            - /bin/sh
            - -c
            - |
              ETCDCTL_API=3 etcdctl \
                --endpoints=https://etcd.kube-system:2379 \
                --cacert=/etc/kubernetes/pki/etcd/ca.crt \
                --cert=/etc/kubernetes/pki/etcd/server.crt \
                --key=/etc/kubernetes/pki/etcd/server.key \
                snapshot save /backups/etcd-backup-$(date +\%Y-\%m-\%d-\%H\%M\%S).db
            volumeMounts:
            - name: etcd-certs
              mountPath: /etc/kubernetes/pki/etcd
            - name: backups
              mountPath: /backups
          volumes:
          - name: etcd-certs
            hostPath:
              path: /etc/kubernetes/pki/etcd
          - name: backups
            persistentVolumeClaim:
              claimName: etcd-backups
          restartPolicy: OnFailure
```

### Velero for Cluster Backup

Velero backs up entire Kubernetes clusters including persistent volumes.

**Install Velero:**
```bash
# Download and install
wget https://github.com/vmware-tanzu/velero/releases/download/v1.12.0/velero-v1.12.0-linux-amd64.tar.gz
tar -xzf velero-v1.12.0-linux-amd64.tar.gz
sudo mv velero-v1.12.0-linux-amd64/velero /usr/local/bin/

# Create S3 credentials file (AWS)
cat > credentials-velero <<EOF
[default]
aws_access_key_id = AKIAIOSFODNN7EXAMPLE
aws_secret_access_key = wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY
EOF

# Install Velero server
velero install \
  --provider aws \
  --plugins velero/velero-plugin-for-aws:v1.8.0 \
  --bucket velero-backups \
  --secret-file ./credentials-velero \
  --use-volume-snapshots=false
```

**Create Backup Schedule:**
```bash
# One-time backup
velero backup create my-cluster-backup

# Scheduled daily backups
velero schedule create daily-backup \
  --schedule="0 2 * * *" \
  --include-namespaces='*'

# List backups
velero backup get

# Restore from backup
velero restore create --from-backup my-cluster-backup
```

**Velero Backup with PersistentVolumes:**
```yaml
apiVersion: velero.io/v1
kind: Backup
metadata:
  name: full-cluster-backup
  namespace: velero
spec:
  includedNamespaces:
  - '*'
  storageLocation: default
  volumeSnapshotLocation: aws-snapshot-location
  snapshotVolumes: true
  ttl: 720h  # 30 days
```

### Multi-Cluster Disaster Recovery

**Active-Passive Setup:**
- Primary cluster handles all traffic
- Secondary cluster in standby, receives backup updates
- DNS failover or manual switch on primary failure

**Implementation:**
```bash
# Backup from primary cluster
velero backup create production-backup --wait

# Transfer backup to secondary cluster
aws s3 sync s3://primary-backups s3://secondary-backups/

# Restore on secondary cluster
velero restore create --from-backup production-backup --wait

# Update DNS/load balancer to point to secondary
# Manual or automated with Route53 health checks
```

---

## Kubernetes on Cloud Providers

### Amazon EKS (Elastic Kubernetes Service)

**Quick Start:**
```bash
# Install eksctl
curl --silent --location "https://github.com/weaveworks/eksctl/releases/latest/download/eksctl_$(uname -s)_amd64.tar.gz" | tar xz -C /tmp
sudo mv /tmp/eksctl /usr/local/bin

# Create cluster
eksctl create cluster \
  --name production \
  --version 1.27 \
  --region us-east-1 \
  --nodegroup-name standard-workers \
  --node-type t3.medium \
  --nodes 3 \
  --nodes-min 1 \
  --nodes-max 10

# Update kubeconfig
aws eks update-kubeconfig --region us-east-1 --name production

# Verify
kubectl get nodes
```

**EKS Features:**
- Managed control plane (AWS manages masters)
- Auto-scaling node groups
- IAM integration for RBAC
- CloudWatch integration for logs/metrics
- EBS for persistent storage
- VPC networking with security groups

### Azure AKS (Azure Kubernetes Service)

**Quick Start:**
```bash
# Create resource group
az group create --name myResourceGroup --location eastus

# Create AKS cluster
az aks create \
  --resource-group myResourceGroup \
  --name productionAKS \
  --node-count 3 \
  --vm-set-type VirtualMachineScaleSets \
  --load-balancer-sku standard \
  --enable-managed-identity \
  --network-plugin azure \
  --network-policy azure

# Get credentials
az aks get-credentials --resource-group myResourceGroup --name productionAKS

# Verify
kubectl get nodes
```

**AKS Features:**
- Managed Kubernetes control plane
- Virtual Machine Scale Sets for nodes
- Azure CNI networking
- Azure Storage integration (AKS Persistent Volumes)
- Azure Container Registry integration
- Azure Monitor for observability
- Azure Policy for governance

### Google GKE (Google Kubernetes Engine)

**Quick Start:**
```bash
# Create GKE cluster
gcloud container clusters create production \
  --zone us-central1-a \
  --num-nodes 3 \
  --machine-type n1-standard-2 \
  --enable-autoscaling \
  --min-nodes 1 \
  --max-nodes 10 \
  --enable-autorepair \
  --enable-autoupgrade

# Get credentials
gcloud container clusters get-credentials production --zone us-central1-a

# Verify
kubectl get nodes
```

**GKE Features:**
- Managed Kubernetes on Google Cloud
- Automatic node repair and upgrades
- Workload Identity for pod-to-GCP service authentication
- Cloud Monitoring integration
- Cloud Logging integration
- Binary Authorization for image security
- Config Connector for managing GCP resources via Kubernetes

### Cloud Provider Comparison

| Feature | EKS | AKS | GKE |
|---------|-----|-----|-----|
| **Managed Control Plane** | Yes | Yes | Yes |
| **Auto-scaling Nodes** | Yes | Yes (VMSS) | Yes |
| **Storage Integration** | EBS, EFS | Azure Disk, Files | Persistent Disk, Filestore |
| **Container Registry** | ECR | ACR | GCR, Artifact Registry |
| **Network Plugin** | VPC CNI | Azure CNI | GKE Network (Calico) |
| **RBAC Integration** | IAM | Azure AD | Cloud Identity |
| **Monitoring** | CloudWatch | Azure Monitor | Cloud Monitoring |
| **Pricing Model** | Hourly node cost | Resource-based | Hourly node cost |
| **Multi-region** | EC2 across regions | Available Zones | Multi-region clusters |

---

## Microservices on Kubernetes

### Service-to-Service Communication

**Service Discovery via DNS:**
```yaml
apiVersion: v1
kind: Service
metadata:
  name: user-service
  namespace: default
spec:
  selector:
    app: user-service
  ports:
  - port: 8080
    targetPort: 8080
  type: ClusterIP

---
# Within cluster, pods access via: http://user-service:8080
# Or with namespace: http://user-service.default.svc.cluster.local:8080
```

**Microservices Example:**
```yaml
---
# API Gateway
apiVersion: apps/v1
kind: Deployment
metadata:
  name: api-gateway
spec:
  replicas: 2
  selector:
    matchLabels:
      app: api-gateway
  template:
    metadata:
      labels:
        app: api-gateway
    spec:
      containers:
      - name: gateway
        image: api-gateway:v1
        env:
        - name: USER_SERVICE_URL
          value: "http://user-service:8080"
        - name: ORDER_SERVICE_URL
          value: "http://order-service:8080"

---
# User Service
apiVersion: apps/v1
kind: Deployment
metadata:
  name: user-service
spec:
  replicas: 3
  selector:
    matchLabels:
      app: user-service
  template:
    metadata:
      labels:
        app: user-service
    spec:
      containers:
      - name: app
        image: user-service:v1
        env:
        - name: DB_HOST
          valueFrom:
            secretKeyRef:
              name: db-secrets
              key: host

---
apiVersion: v1
kind: Service
metadata:
  name: user-service
spec:
  selector:
    app: user-service
  ports:
  - port: 8080
    targetPort: 8080

---
# Order Service
apiVersion: apps/v1
kind: Deployment
metadata:
  name: order-service
spec:
  replicas: 3
  selector:
    matchLabels:
      app: order-service
  template:
    metadata:
      labels:
        app: order-service
    spec:
      containers:
      - name: app
        image: order-service:v1
        env:
        - name: USER_SERVICE_URL
          value: "http://user-service:8080"

---
apiVersion: v1
kind: Service
metadata:
  name: order-service
spec:
  selector:
    app: order-service
  ports:
  - port: 8080
    targetPort: 8080
```

### API Gateway

Use Ingress or a dedicated API Gateway for external traffic.

```yaml
apiVersion: networking.k8s.io/v1
kind: Ingress
metadata:
  name: api-gateway
  annotations:
    cert-manager.io/cluster-issuer: letsencrypt-prod
    nginx.ingress.kubernetes.io/rewrite-target: /
spec:
  ingressClassName: nginx
  tls:
  - hosts:
    - api.example.com
    secretName: api-tls-cert
  rules:
  - host: api.example.com
    http:
      paths:
      - path: /users
        pathType: Prefix
        backend:
          service:
            name: user-service
            port:
              number: 8080
      - path: /orders
        pathType: Prefix
        backend:
          service:
            name: order-service
            port:
              number: 8080
```

### Circuit Breakers and Retries (Istio)

**Install Istio:**
```bash
curl -L https://istio.io/downloadIstio | sh -
cd istio-1.17.0
sudo cp bin/istioctl /usr/local/bin/
istioctl install --set profile=demo -y
```

**Configure Circuit Breaker:**
```yaml
apiVersion: networking.istio.io/v1beta1
kind: DestinationRule
metadata:
  name: user-service-circuit-breaker
spec:
  host: user-service
  trafficPolicy:
    connectionPool:
      tcp:
        maxConnections: 100
      http:
        http1MaxPendingRequests: 50
        http2MaxRequests: 100
        http1MaxRequests: 100
    outlierDetection:
      consecutive5xxErrors: 5
      interval: 30s
      baseEjectionTime: 30s
      minRequestVolume: 10
      splitExternalLocalOriginErrors: true
```

**Retry Policy:**
```yaml
apiVersion: networking.istio.io/v1beta1
kind: VirtualService
metadata:
  name: user-service
spec:
  hosts:
  - user-service
  http:
  - match:
    - uri:
        prefix: /
    route:
    - destination:
        host: user-service
        port:
          number: 8080
    retries:
      attempts: 3
      perTryTimeout: 2s
    timeout: 10s
```

---

## Cost Optimization

### Right-Sizing Resources

```yaml
# Good: Requests and limits set appropriately
apiVersion: apps/v1
kind: Deployment
metadata:
  name: optimized-app
spec:
  replicas: 3
  template:
    spec:
      containers:
      - name: app
        image: myapp:v1
        resources:
          requests:
            cpu: 250m
            memory: 256Mi
          limits:
            cpu: 500m
            memory: 512Mi
```

**How to determine right sizes:**
1. Monitor actual usage: `kubectl top pods`
2. Check Prometheus metrics over time
3. Set requests slightly above 50th percentile
4. Set limits at 95th percentile + buffer
5. Adjust quarterly based on trends

```bash
# Monitor resource usage
kubectl top nodes
kubectl top pods --all-namespaces

# Get historical metrics from Prometheus
# Query: histogram_quantile(0.95, rate(container_memory_usage_bytes[7d]))
```

### Spot/Preemptible Instances

Cost savings (60-90% discount) for non-critical workloads.

**AWS Spot Instances:**
```bash
# Create spot instance node group
eksctl create nodegroup \
  --cluster=production \
  --name=spot-workers \
  --spot \
  --instance-types=t3.medium,t3.large,m5.large \
  --nodes-min=1 \
  --nodes-max=10
```

**Mark pods to tolerate spot interruption:**
```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: batch-job
spec:
  template:
    spec:
      tolerations:
      - key: spot
        operator: Equal
        value: "true"
        effect: NoSchedule
      nodeSelector:
        node-type: spot
      containers:
      - name: job
        image: batch-processor:v1
```

### Namespace Resource Quotas

Prevent resource overuse by setting quotas per namespace.

```yaml
apiVersion: v1
kind: Namespace
metadata:
  name: team-a

---
apiVersion: v1
kind: ResourceQuota
metadata:
  name: team-a-quota
  namespace: team-a
spec:
  hard:
    requests.cpu: "10"
    requests.memory: "20Gi"
    limits.cpu: "20"
    limits.memory: "40Gi"
    pods: "100"
    persistentvolumeclaims: "10"
  scopes:
  - NotTerminating

---
apiVersion: v1
kind: LimitRange
metadata:
  name: team-a-limits
  namespace: team-a
spec:
  limits:
  - type: Container
    default:
      cpu: "1000m"
      memory: "1Gi"
    defaultRequest:
      cpu: "100m"
      memory: "128Mi"
    min:
      cpu: "50m"
      memory: "64Mi"
    max:
      cpu: "2000m"
      memory: "2Gi"
```

**Check quota usage:**
```bash
kubectl describe resourcequota team-a-quota -n team-a
```

### Cost Monitoring

**Tools:**
- **Kubecost**: Open-source, provides cost visibility per namespace/pod
- **AWS Compute Optimizer**: Recommends right-sized instances
- **Azure Cost Management**: Azure cost tracking
- **Google Cloud Cost Insights**: GCP spending analysis

**Install Kubecost:**
```bash
helm repo add kubecost https://kubecost.github.io/cost-analyzer/
helm install kubecost kubecost/cost-analyzer \
  --namespace kubecost \
  --create-namespace \
  --set prometheus.server.global.external_labels.cluster_id=production
```

---

## Performance Best Practices

### Resource Requests and Limits Tuning

**Correct configuration:**
```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: high-performance-app
spec:
  replicas: 3
  template:
    spec:
      terminationGracePeriodSeconds: 30  # Allow graceful shutdown
      containers:
      - name: app
        image: myapp:v1
        resources:
          requests:
            cpu: 500m         # Reserve CPU
            memory: 512Mi     # Reserve memory
          limits:
            cpu: 1000m        # Throttle at this limit
            memory: 1Gi       # Kill if exceeds
        lifecycle:
          preStop:
            exec:
              command: ["/bin/sh", "-c", "sleep 15"]  # Drain connections
```

### Pod Disruption Budgets

Ensure minimum availability during voluntary disruptions (upgrades, drains).

```yaml
apiVersion: policy/v1
kind: PodDisruptionBudget
metadata:
  name: myapp-pdb
spec:
  minAvailable: 2
  selector:
    matchLabels:
      app: myapp

---
# Alternative: maxUnavailable
apiVersion: policy/v1
kind: PodDisruptionBudget
metadata:
  name: critical-app-pdb
spec:
  maxUnavailable: 1
  selector:
    matchLabels:
      app: critical-service
```

### Priority Classes

Ensure critical workloads survive eviction during resource pressure.

```yaml
apiVersion: scheduling.k8s.io/v1
kind: PriorityClass
metadata:
  name: critical-priority
value: 1000
globalDefault: false
description: "For critical production services"

---
apiVersion: scheduling.k8s.io/v1
kind: PriorityClass
metadata:
  name: batch-priority
value: 100
globalDefault: false
description: "For batch/background jobs"

---
# Use in Pod spec
apiVersion: v1
kind: Pod
metadata:
  name: critical-app
spec:
  priorityClassName: critical-priority
  containers:
  - name: app
    image: critical-app:v1
```

### Cluster Autoscaler Tuning

```yaml
# Adjust autoscaler deployment in kube-system
kubectl patch deployment -n kube-system cluster-autoscaler \
  -p '{"spec":{"template":{"spec":{"containers":[{"name":"cluster-autoscaler","args":["--cloud-provider=aws","--nodes-min=1","--nodes-max=100","--scale-down-enabled=true","--scale-down-delay-after-add=10m","--scale-down-unneeded-time=10m"]}]}}}}'
```

---

## Exercises

### Exercise 1: Deploy Prometheus + Grafana Stack

**Objective:** Set up monitoring for your Kubernetes cluster.

**Steps:**

1. Add Prometheus Helm repository and install:
```bash
helm repo add prometheus-community https://prometheus-community.github.io/helm-charts
helm repo update
helm install prometheus prometheus-community/kube-prometheus-stack \
  --namespace monitoring \
  --create-namespace
```

2. Verify installation:
```bash
kubectl get pods -n monitoring
kubectl get svc -n monitoring
```

3. Port forward to Grafana:
```bash
kubectl port-forward -n monitoring svc/prometheus-grafana 3000:80 &
```

4. Access Grafana at `http://localhost:3000` (default: admin/prom-operator)

5. Import a dashboard:
   - Go to Dashboards → Import
   - Use dashboard ID: 6417 (Kubernetes Cluster Monitoring)
   - Select Prometheus as data source

6. Create an alert rule:
   - Go to Alerts → Alert Rules → New Alert Rule
   - Create rule: Pod restart rate > 5 in 1 hour
   - Set notification channel (webhook or email)

### Exercise 2: Create a Helm Chart for a Simple Web App

**Objective:** Package your application as a reusable Helm chart.

**Steps:**

1. Create chart structure:
```bash
helm create mywebapp
cd mywebapp
```

2. Edit `Chart.yaml`:
```yaml
apiVersion: v2
name: mywebapp
description: A simple web application
version: 1.0.0
appVersion: "1.0"
```

3. Customize `values.yaml` for your app:
```yaml
replicaCount: 2
image:
  repository: nginx
  tag: "latest"
service:
  type: LoadBalancer
  port: 80
```

4. Update `templates/deployment.yaml` with your image configuration

5. Lint and test:
```bash
helm lint mywebapp
helm template mywebapp mywebapp
helm install test-release mywebapp --dry-run --debug
```

6. Install to cluster:
```bash
helm install myapp mywebapp
helm status myapp
```

7. Upgrade the chart:
```bash
# Update image tag in values.yaml
helm upgrade myapp mywebapp --values mywebapp/values.yaml
```

### Exercise 3: Set Up etcd Backup with CronJob

**Objective:** Automate etcd cluster backups.

**Steps:**

1. Create backup storage:
```bash
kubectl create namespace backups
kubectl apply -f - <<'EOF'
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: etcd-backups
  namespace: backups
spec:
  accessModes:
    - ReadWriteOnce
  resources:
    requests:
      storage: 50Gi
  storageClassName: standard
EOF
```

2. Create RBAC for backup job:
```bash
kubectl apply -f - <<'EOF'
apiVersion: v1
kind: ServiceAccount
metadata:
  name: etcd-backup
  namespace: backups
---
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRole
metadata:
  name: etcd-backup
rules:
- apiGroups: [""]
  resources: ["pods", "pods/log"]
  verbs: ["get", "list"]
EOF
```

3. Deploy backup CronJob:
```bash
kubectl apply -f - <<'EOF'
apiVersion: batch/v1
kind: CronJob
metadata:
  name: etcd-backup
  namespace: backups
spec:
  schedule: "0 2 * * *"  # 2 AM daily
  jobTemplate:
    spec:
      template:
        spec:
          serviceAccountName: etcd-backup
          containers:
          - name: backup
            image: bitnami/etcd:latest
            command:
            - /bin/sh
            - -c
            - |
              ETCDCTL_API=3 etcdctl \
                --endpoints=https://etcd.kube-system:2379 \
                --cacert=/etc/kubernetes/pki/etcd/ca.crt \
                --cert=/etc/kubernetes/pki/etcd/server.crt \
                --key=/etc/kubernetes/pki/etcd/server.key \
                snapshot save /backups/etcd-backup-$(date +%Y-%m-%d-%H%M%S).db
            volumeMounts:
            - name: etcd-certs
              mountPath: /etc/kubernetes/pki/etcd
            - name: backups
              mountPath: /backups
          volumes:
          - name: etcd-certs
            hostPath:
              path: /etc/kubernetes/pki/etcd
          - name: backups
            persistentVolumeClaim:
              claimName: etcd-backups
          restartPolicy: OnFailure
EOF
```

4. Verify backups are being created:
```bash
kubectl logs -n backups -l job-name=etcd-backup-XXXXX
kubectl exec -it -n backups <pod-name> -- ls -lh /backups/
```

5. Test restore procedure (documented in etcd section above)

---

## Summary

Running Kubernetes in production requires:

1. **Planning**: Use the readiness checklist before going live
2. **Automation**: Implement CI/CD with GitOps (ArgoCD/Flux)
3. **Observability**: Deploy monitoring (Prometheus/Grafana) and logging (EFK/Loki)
4. **Resilience**: Backup with Velero, use PDBs and priority classes
5. **Cost Control**: Right-size resources, use spot instances, set quotas
6. **Performance**: Tune autoscaler, set appropriate requests/limits, use caching
7. **Expertise**: Invest in team training and runbook documentation

Start with the exercises above and gradually adopt these practices in your environment.
