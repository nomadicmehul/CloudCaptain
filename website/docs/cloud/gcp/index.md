---
title: "Google Cloud Platform"
description: "GCP learning resources, documentation, and guides"
sidebar_position: 3
---

# Google Cloud Platform (GCP)

Google's suite of cloud computing services running on the same infrastructure that Google uses for its products.

## Learning Path

Start your GCP journey with this structured learning path:

| Step | Topic | Description |
|:-----|:------|:------------|
| 1 | [Fundamentals](./fundamentals.md) | Global infrastructure, core services, resource hierarchy, billing, and IAM |
| 2 | [Architecture & DevOps](./architecture-devops.md) | Landing zones, CI/CD, deployment patterns, microservices, GKE, networking |
| 3 | [Exam Preparation](./exam-prep.md) | Associate Cloud Engineer and Professional Cloud Architect exam guides |
| 4 | [Cheatsheet](./cheatsheet.md) | 80+ gcloud CLI commands organized by service |
| 5 | [Interview Questions](./interview-questions.md) | 40+ GCP interview questions with detailed answers |

## Core Services

| Category | Services |
|:---------|:---------|
| **Compute** | Compute Engine, GKE, Cloud Run, Cloud Functions |
| **Storage** | Cloud Storage, Persistent Disk, Filestore |
| **Databases** | Cloud SQL, Firestore, Bigtable, Spanner |
| **Data** | BigQuery, Dataflow, Pub/Sub |
| **AI/ML** | Vertex AI, AutoML, TensorFlow |

## Certification Path

1. Cloud Digital Leader
2. Associate Cloud Engineer
3. Professional Cloud Architect
4. Professional Cloud DevOps Engineer
5. Professional Data Engineer

## External Resources

| Resource | Link |
|:---------|:-----|
| Google Cloud Skills Boost | [cloudskillsboost.google](https://cloudskillsboost.google/) |
| GCP Architecture Center | [cloud.google.com/architecture](https://cloud.google.com/architecture) |
| Google Cloud Documentation | [cloud.google.com/docs](https://cloud.google.com/docs) |
| Google Cloud Console | [console.cloud.google.com](https://console.cloud.google.com) |
| Cloud SDK Download | [cloud.google.com/sdk](https://cloud.google.com/sdk) |
| GCP Pricing Calculator | [cloud.google.com/products/calculator](https://cloud.google.com/products/calculator) |

## Quick Start

Get started with GCP in minutes:

```bash
# Install Google Cloud SDK
# Visit: https://cloud.google.com/sdk/docs/install

# Initialize gcloud
gcloud init

# Set your default project
gcloud config set project PROJECT_ID

# Create a Compute Engine instance
gcloud compute instances create my-instance \
  --image-family ubuntu-2004-lts \
  --image-project ubuntu-os-cloud \
  --zone us-central1-a

# Connect via SSH
gcloud compute ssh my-instance --zone us-central1-a
```

## Key Concepts

### Global Infrastructure
- **Regions**: Geographic areas containing multiple zones
- **Zones**: Individual data centers within a region
- **Multi-region**: Resources distributed across regions for high availability

### Identity and Access Management (IAM)
- **Roles**: Bundles of permissions (Basic, Predefined, Custom)
- **Service Accounts**: Identity for applications and services
- **Members**: Users, service accounts, groups, and domains
- **Policies**: Bind members to roles with conditions

### Resource Hierarchy
```
Organization
  ├── Folder
  │   ├── Project A
  │   │   ├── Compute Engine VM
  │   │   ├── Cloud Storage Bucket
  │   │   └── Service Accounts
  │   └── Project B
  └── Folder
      └── Project C
```

## Popular Patterns

### Microservices on Cloud Run
- Auto-scaling to zero
- Deploy containers without managing infrastructure
- Pay only for actual usage

### Kubernetes with GKE
- Managed Kubernetes service
- Auto-scaling, networking, security
- Integrated with GCP services

### Data Analytics with BigQuery
- Serverless data warehouse
- Query massive datasets in seconds
- Integrated analytics and visualization

### Event-Driven Architecture
- Pub/Sub for decoupled messaging
- Cloud Functions for event handlers
- Dataflow for streaming processing

## Community and Support

- **Google Cloud Community**: [communities.gcp.google.com](https://communities.gcp.google.com)
- **Stack Overflow**: Tag `google-cloud-platform`
- **GitHub**: [googleapis](https://github.com/googleapis)
- **Issue Tracker**: Report bugs and request features

## Tips for Success

1. **Use the Free Tier**: $300 credit for 90 days + always-free services
2. **Enable APIs**: Most services require API enablement
3. **Use IAM**: Implement least privilege from the start
4. **Monitor Costs**: Set up billing alerts and budgets
5. **Practice**: Use Cloud Skills Boost for hands-on labs
6. **Read Documentation**: GCP docs are comprehensive and regularly updated
7. **Join Community**: Learn from others' experiences and best practices
