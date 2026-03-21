---
title: "Kubernetes"
sidebar_label: "Kubernetes Overview"
description: "Comprehensive Kubernetes learning resources — architecture, workloads, networking, security, storage, certifications, and more"
sidebar_position: 0
---

# Kubernetes

Master container orchestration with **Kubernetes** — the industry standard for deploying, scaling, and managing containerized applications.

## Documentation

| Guide | Description |
|:------|:------------|
| [Fundamentals](/docs/tools/kubernetes/fundamentals) | Architecture, pods, services, deployments, and core concepts |
| [Workloads & Scheduling](/docs/tools/kubernetes/workloads) | Deployments, StatefulSets, DaemonSets, Jobs, auto-scaling, and health checks |
| [Networking & Services](/docs/tools/kubernetes/networking-services) | Service types, Ingress, DNS, Network Policies, and service mesh |
| [Storage & Persistence](/docs/tools/kubernetes/storage) | Volumes, PV/PVC, StorageClasses, CSI, and backup strategies |
| [Security & Hardening](/docs/tools/kubernetes/security) | RBAC, pod security, image scanning, secrets, and NSA/CISA hardening guide |
| [Exam Prep (CKA/CKAD/CKS)](/docs/tools/kubernetes/exam-prep) | Certification domains, practice questions, study tips, and labs |
| [kubectl Cheat Sheet](/docs/tools/kubernetes/cheatsheet) | 200+ commands for pods, deployments, services, debugging, and cluster management |
| [Production & Operations](/docs/tools/kubernetes/production-operations) | CI/CD, Helm, monitoring, logging, disaster recovery, and cloud providers |
| [Interview Questions](/docs/tools/kubernetes/interview-questions) | 60+ questions from beginner to advanced with detailed answers |

## Core Architecture

```
┌─────────────────────────────────────────────────────┐
│                   Control Plane                      │
│  ┌───────────┐  ┌──────┐  ┌───────────┐  ┌───────┐ │
│  │API Server │  │ etcd │  │ Scheduler │  │Ctrl Mgr│ │
│  └───────────┘  └──────┘  └───────────┘  └───────┘ │
└──────────────────────┬──────────────────────────────┘
                       │
       ┌───────────────┼───────────────┐
       ▼               ▼               ▼
┌─────────────┐ ┌─────────────┐ ┌─────────────┐
│  Worker 1   │ │  Worker 2   │ │  Worker 3   │
│  kubelet    │ │  kubelet    │ │  kubelet    │
│  kube-proxy │ │  kube-proxy │ │  kube-proxy │
│  [pods]     │ │  [pods]     │ │  [pods]     │
└─────────────┘ └─────────────┘ └─────────────┘
```

## Essential Concepts

| Concept | Description |
|:--------|:------------|
| **Pod** | Smallest deployable unit, one or more containers |
| **Service** | Stable network endpoint for pods |
| **Deployment** | Manages ReplicaSets and rolling updates |
| **StatefulSet** | For stateful applications with stable identity |
| **DaemonSet** | Runs a pod on every node |
| **Ingress** | HTTP/HTTPS routing to services |
| **ConfigMap** | Configuration data as key-value pairs |
| **Secret** | Sensitive data (passwords, tokens) |
| **PV/PVC** | Persistent storage for pods |
| **Namespace** | Virtual cluster for resource isolation |

## Learning Path

1. [Start with fundamentals](/docs/tools/kubernetes/fundamentals) — understand architecture, pods, services
2. [Master workloads](/docs/tools/kubernetes/workloads) — deployments, scaling, scheduling, health checks
3. [Learn networking](/docs/tools/kubernetes/networking-services) — services, ingress, DNS, network policies
4. [Understand storage](/docs/tools/kubernetes/storage) — volumes, PV/PVC, StatefulSets with data
5. [Harden security](/docs/tools/kubernetes/security) — RBAC, pod security, image scanning
6. [Prepare for certifications](/docs/tools/kubernetes/exam-prep) — CKA, CKAD, CKS exam prep
7. [Go to production](/docs/tools/kubernetes/production-operations) — CI/CD, monitoring, Helm, disaster recovery

## Certification Path

| Certification | Level | Focus |
|:--------------|:------|:------|
| **KCNA** | Beginner | Kubernetes and Cloud Native concepts |
| **CKA** | Intermediate | Cluster administration and operations |
| **CKAD** | Intermediate | Application development on Kubernetes |
| **CKS** | Advanced | Security specialist (requires CKA) |

## Quick Start

```bash
# Install minikube and start a local cluster
minikube start

# Deploy an app
kubectl create deployment hello --image=nginx
kubectl expose deployment hello --port=80 --type=NodePort

# View resources
kubectl get pods,svc,deploy

# Scale up
kubectl scale deployment hello --replicas=3

# Clean up
kubectl delete deployment hello
kubectl delete svc hello
```

## External Resources

| Resource | Description |
|:---------|:------------|
| [Kubernetes Official Docs](https://kubernetes.io/docs/) | Official documentation |
| [Kubernetes the Hard Way](https://github.com/kelseyhightower/kubernetes-the-hard-way) | Learn K8s from scratch by Kelsey Hightower |
| [Play with K8s](https://labs.play-with-k8s.com/) | Free K8s playground |
| [Killercoda](https://killercoda.com/) | Interactive K8s scenarios |
| [CNCF Landscape](https://landscape.cncf.io/) | Cloud native ecosystem overview |
| [Kubernetes Troubleshooting Guide](https://learnk8s.io/troubleshooting-deployments) | Visual troubleshooting flowchart |

## Security Tools

| Tool | Description |
|:-----|:------------|
| [Kubescape](https://github.com/armosec/kubescape) | Security testing per NSA/CISA hardening guidance |
| [Falco](https://falco.org) | Kubernetes threat detection engine |
| [OPA Gatekeeper](https://github.com/open-policy-agent/gatekeeper) | Policy-as-code for Kubernetes |
| [Trivy](https://github.com/aquasecurity/trivy) | Container image vulnerability scanner |

## Useful Tools

| Tool | Description |
|:-----|:------------|
| [Helm](https://helm.sh/) | Kubernetes package manager |
| [Telepresence](https://www.telepresence.io) | Fast local development for K8s microservices |
| [Lens](https://k8slens.dev/) | Kubernetes IDE |
| [k9s](https://k9scli.io/) | Terminal UI for Kubernetes |
| [Datree](https://www.datree.io) | Prevent K8s misconfigurations in development |
