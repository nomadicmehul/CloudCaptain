---
title: "GCP Architecture & DevOps"
description: "Landing zones, CI/CD, deployment patterns, microservices, GKE best practices, networking, and monitoring"
sidebar_label: "Architecture & DevOps"
sidebar_position: 2
---

# GCP Architecture and DevOps

## GCP Landing Zones

A landing zone is a cloud environment pre-configured with governance, security, and operational best practices.

### Landing Zone Components

| Component | Purpose |
|:----------|:--------|
| **Organization Structure** | Folders and projects for separation of concerns |
| **IAM Setup** | Pre-configured roles and service accounts |
| **Networking** | VPC, subnets, firewall rules, and routing |
| **Security** | Secret management, encryption, DLP policies |
| **Logging/Monitoring** | Cloud Logging, Cloud Monitoring, Audit Logs |
| **Cost Management** | Budget alerts, cost allocation tags |

### Landing Zone Architecture Pattern

```
Organization
├── Security Folder
│   └── Audit Project (Logging, Monitoring, Secrets)
├── Shared Services Folder
│   └── Networking Project (VPC, VPN, Interconnect)
└── Workload Folders
    ├── Dev Environment
    ├── Staging Environment
    └── Production Environment
```

## CI/CD Pipelines and Cloud Build

### Cloud Build Overview

Cloud Build is a fully managed CI/CD platform that:
- Builds container images and artifacts
- Runs automated tests
- Deploys to GCP services
- Integrates with GitHub, Bitbucket, and Cloud Source Repositories

### Build Pipeline Stages

```
Source Code Push
    ↓
Trigger (GitHub/CSR/Bitbucket)
    ↓
Build Phase (Compile, Test, Build Docker Image)
    ↓
Scan Phase (Container Analysis, Security Scanning)
    ↓
Push Phase (To Container Registry or Artifact Registry)
    ↓
Deploy Phase (To Cloud Run, GKE, App Engine, or Compute Engine)
```

### Cloud Build Example (cloudbuild.yaml)

```yaml
steps:
  # Build the Docker image
  - name: 'gcr.io/cloud-builders/docker'
    args: ['build', '-t', 'gcr.io/$PROJECT_ID/myapp:$SHORT_SHA', '.']

  # Run tests in the container
  - name: 'gcr.io/$PROJECT_ID/myapp:$SHORT_SHA'
    entrypoint: 'bash'
    args: ['-c', 'npm test']

  # Push to Container Registry
  - name: 'gcr.io/cloud-builders/docker'
    args: ['push', 'gcr.io/$PROJECT_ID/myapp:$SHORT_SHA']

  # Deploy to Cloud Run
  - name: 'gcr.io/cloud-builders/gke-deploy'
    args:
      - run
      - --filename=k8s/
      - --image=gcr.io/$PROJECT_ID/myapp:$SHORT_SHA
      - --location=us-central1
      - --cluster=my-cluster

images:
  - 'gcr.io/$PROJECT_ID/myapp:$SHORT_SHA'
```

### Key Gcloud Commands

```bash
# Enable Cloud Build API
gcloud services enable cloudbuild.googleapis.com

# Submit a build
gcloud builds submit --tag gcr.io/PROJECT-ID/myapp

# View build logs
gcloud builds log BUILD_ID

# Create a build trigger
gcloud builds triggers create github \
  --repo-name=my-repo \
  --repo-owner=my-username \
  --branch-pattern="^main$" \
  --build-config=cloudbuild.yaml
```

## Deployment Patterns

### Blue-Green Deployment
Maintain two identical production environments:
- **Blue**: Current production
- **Green**: Staging/next version
- Switch traffic instantly when green is ready
- Quick rollback by switching back to blue

### Canary Deployment
Gradually shift traffic to new version:
- Route 5% traffic to new version, monitor
- Increase to 25%, 50%, 100% based on metrics
- Automatic rollback if error rates spike
- Supported by Cloud Load Balancing and Spinnaker

### Rolling Deployment
Gradually update instances:
- Update 1-2 instances, verify health
- Continue rolling update across infrastructure
- No downtime with multiple replicas
- Default behavior in GKE and Cloud Run

## Microservices Architecture on Cloud Run

### Cloud Run for Microservices

Cloud Run is ideal for microservices because it:
- Auto-scales to zero (no idle costs)
- Handles traffic spikes automatically
- Supports any language via containers
- Integrates with Cloud Build for CI/CD
- Provides managed HTTPS and traffic splitting

### Microservices Communication Patterns

| Pattern | Technology | Use Case |
|:--------|:-----------|:---------|
| **Synchronous** | Cloud Run HTTP, Cloud Tasks | Request-response, order processing |
| **Asynchronous** | Pub/Sub | Decoupled services, event-driven |
| **Streaming** | Pub/Sub, Dataflow | Real-time data pipelines |

### Cloud Run Deployment Example

```bash
# Build and push image
gcloud builds submit --tag gcr.io/PROJECT-ID/myservice

# Deploy to Cloud Run
gcloud run deploy myservice \
  --image gcr.io/PROJECT-ID/myservice \
  --platform managed \
  --region us-central1 \
  --memory 512Mi \
  --timeout 300 \
  --set-env-vars DATABASE_URL=CONNECTION_STRING

# Add service-to-service authentication
gcloud run services add-iam-policy-binding myservice \
  --member=serviceAccount:other-service@PROJECT-ID.iam.gserviceaccount.com \
  --role=roles/run.invoker

# Split traffic between versions (canary)
gcloud run services update-traffic myservice \
  --to-revisions REVISION1=80,REVISION2=20
```

## GKE Best Practices

### Cluster Design

| Aspect | Best Practice |
|:-------|:--------------|
| **Node Pools** | Multiple pools for different workloads (general, compute, memory-optimized) |
| **Autoscaling** | Enable cluster autoscaling with appropriate min/max nodes |
| **Network** | Use VPC-native clusters with secondary IP ranges |
| **Security** | Enable Binary Authorization, Pod Security Policies, Network Policies |
| **Updates** | Use release channels (regular, stable) for automated updates |

### Resource Management

```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: myapp
spec:
  replicas: 3
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
        image: gcr.io/PROJECT-ID/myapp:latest
        resources:
          requests:
            cpu: "100m"
            memory: "128Mi"
          limits:
            cpu: "500m"
            memory: "512Mi"
```

### GKE Cluster Creation

```bash
# Enable GKE API
gcloud services enable container.googleapis.com

# Create a GKE cluster
gcloud container clusters create my-cluster \
  --zone us-central1-a \
  --num-nodes 3 \
  --enable-ip-alias \
  --enable-cloud-logging \
  --enable-cloud-monitoring \
  --enable-autorepair \
  --enable-autoupgrade \
  --release-channel regular

# Get cluster credentials
gcloud container clusters get-credentials my-cluster --zone us-central1-a

# Deploy to cluster
kubectl apply -f deployment.yaml

# View pods
kubectl get pods

# Delete cluster
gcloud container clusters delete my-cluster --zone us-central1-a
```

## Networking Architecture

### VPC and Subnets

```
VPC: my-network (10.0.0.0/8)
├── Subnet: us-central1-subnet (10.0.1.0/24)
├── Subnet: us-east1-subnet (10.0.2.0/24)
└── Subnet: europe-west1-subnet (10.0.3.0/24)
```

### Firewall Rules

```bash
# Create firewall rule to allow HTTP/HTTPS
gcloud compute firewall-rules create allow-web \
  --network=my-network \
  --allow=tcp:80,tcp:443 \
  --source-ranges=0.0.0.0/0

# Create rule to allow SSH from specific IP
gcloud compute firewall-rules create allow-ssh \
  --network=my-network \
  --allow=tcp:22 \
  --source-ranges=203.0.113.0/32

# List firewall rules
gcloud compute firewall-rules list

# Delete firewall rule
gcloud compute firewall-rules delete allow-web
```

### Cloud VPN and Hybrid Connectivity

**Cloud VPN**: Encrypted site-to-site VPN connectivity
```bash
gcloud compute vpn-gateways create my-vpn-gateway \
  --network=my-network \
  --region=us-central1
```

**Cloud Interconnect**: Dedicated network connection
- 10 Gbps or 100 Gbps private connectivity
- Lower latency, more predictable bandwidth
- Hybrid cloud and multi-cloud use cases

### Cloud Load Balancing

| Load Balancer | Layer | Use Case |
|:--------------|:------|:---------|
| **HTTP/HTTPS** | Layer 7 | Web apps, content-based routing |
| **Network** | Layer 4 | High throughput, low latency |
| **Internal** | Layer 4 | Internal traffic between services |
| **SSL Proxy** | Layer 4 | Non-HTTP protocols with SSL |

```bash
# Create HTTP load balancer
gcloud compute load-balancers create my-lb \
  --global \
  --http-backend-service my-backend-service

# Create health check
gcloud compute health-checks create http my-health-check \
  --port=80 \
  --request-path=/health
```

## Monitoring and Observability

### Cloud Logging

Collect, store, and analyze logs from all GCP resources:

```bash
# Write a log entry
gcloud logging write my-log "Application started" --severity=INFO

# Read recent logs
gcloud logging read "resource.type=gce_instance" --limit 10

# Create log sink to BigQuery
gcloud logging sinks create my-sink bigquery.googleapis.com/projects/PROJECT_ID/datasets/my_dataset \
  --log-filter='resource.type="gce_instance"'
```

### Cloud Monitoring

Monitor metrics, create alerts, and build dashboards:

```yaml
apiVersion: monitoring.coreos.com/v1
kind: PrometheusRule
metadata:
  name: app-alerts
spec:
  groups:
  - name: app.rules
    rules:
    - alert: HighErrorRate
      expr: rate(errors_total[5m]) > 0.05
      for: 5m
      annotations:
        summary: "High error rate detected"
```

### Key Metrics to Monitor

- **Latency**: Response time for requests
- **Error Rate**: Percentage of failed requests
- **Throughput**: Requests per second
- **Resource Usage**: CPU, memory, disk I/O
- **Availability**: Uptime percentage

## Hands-on Exercises

### Exercise 1: Create a Cloud Build CI/CD Pipeline
```bash
# Create a cloudbuild.yaml in your repo
# Enable Cloud Build
gcloud services enable cloudbuild.googleapis.com

# Create a trigger
gcloud builds triggers create github \
  --repo-name=my-repo \
  --repo-owner=my-username \
  --branch-pattern="^main$" \
  --build-config=cloudbuild.yaml

# Test build manually
gcloud builds submit --config cloudbuild.yaml
```

### Exercise 2: Deploy Microservices to Cloud Run with Traffic Splitting
```bash
# Deploy version 1
gcloud run deploy myapp \
  --image gcr.io/PROJECT-ID/myapp:v1 \
  --platform managed \
  --region us-central1

# Deploy version 2 (creates new revision)
gcloud run deploy myapp \
  --image gcr.io/PROJECT-ID/myapp:v2 \
  --platform managed \
  --region us-central1

# Split traffic 80/20
gcloud run services update-traffic myapp \
  --to-revisions myapp-v1=80,myapp-v2=20
```

### Exercise 3: Create a GKE Cluster and Deploy an Application
```bash
# Create cluster
gcloud container clusters create my-app-cluster \
  --zone us-central1-a \
  --num-nodes 3

# Get credentials
gcloud container clusters get-credentials my-app-cluster --zone us-central1-a

# Create and apply deployment
kubectl apply -f - <<EOF
apiVersion: apps/v1
kind: Deployment
metadata:
  name: nginx
spec:
  replicas: 3
  selector:
    matchLabels:
      app: nginx
  template:
    metadata:
      labels:
        app: nginx
    spec:
      containers:
      - name: nginx
        image: nginx:latest
        ports:
        - containerPort: 80
EOF

# Expose with service
kubectl expose deployment nginx --type=LoadBalancer --port=80

# Check service
kubectl get svc nginx
```

## Review Questions

1. What is a landing zone and why is it important?
2. Explain the difference between blue-green and canary deployments.
3. Why is Cloud Run suitable for microservices?
4. What are the key components of a VPC?
5. Name three metrics you should monitor in production.
