---
sidebar_position: 5
title: "Containers Learning Path"
description: "Master containers from Docker to production Kubernetes — sequenced through CloudCaptain's full guides"
---

# Containers & Orchestration Learning Path

From your first container to production-grade Kubernetes clusters. This path sequences **~75,000 words** of CloudCaptain's Docker and Kubernetes guides: **~10–14 weeks** at 5–7 hours/week, from `docker run hello-world` to CKA-ready.

**By the end you'll be able to:** containerize any application with a production-quality Dockerfile, operate a Kubernetes cluster (workloads, networking, storage, security), and pass the CKA/CKAD with the exam-prep guide.

## Stage 1: Container Fundamentals (2–3 weeks)

1. **[Docker Fundamentals](/docs/tools/docker/fundamentals)** — namespaces, cgroups, OCI, images, containers, volumes
2. **[The Dockerfile Guide](/docs/tools/docker/dockerfile-guide)** — multi-stage builds, layer caching, best practices (80+ examples)
3. **[Compose & Swarm](/docs/tools/docker/compose-swarm)** — multi-container applications
4. **[Networking & Storage](/docs/tools/docker/networking-storage)** — bridge/overlay networks, volumes vs bind mounts
5. **[Security & Production](/docs/tools/docker/security-production)** — image scanning, rootless mode, hardening
6. **[Podman](/docs/tools/podman/)** — the rootless, daemonless alternative

✅ **Checkpoint:** the [Docker cheat sheet](/docs/tools/docker/cheatsheet) should feel like review, and you should hold your own against the [Docker interview questions](/docs/tools/docker/interview-questions).

## Stage 2: Kubernetes Core (3–4 weeks)

1. **[Kubernetes Fundamentals](/docs/tools/kubernetes/fundamentals)** — architecture, pods, kubectl, the control plane
2. **[Workloads](/docs/tools/kubernetes/workloads)** — Deployments, StatefulSets, DaemonSets, Jobs
3. **[Networking & Services](/docs/tools/kubernetes/networking-services)** — Services, Ingress, Network Policies
4. **[Storage](/docs/tools/kubernetes/storage)** — PVs, PVCs, StorageClasses
5. **[Docker → Kubernetes](/docs/tools/docker/docker-kubernetes)** — how the concepts map

✅ **Checkpoint:** drill the [Kubernetes cheat sheet](/docs/tools/kubernetes/cheatsheet) until `kubectl` is muscle memory.

## Stage 3: Advanced Kubernetes (2–3 weeks)

1. **[Security](/docs/tools/kubernetes/security)** — RBAC, Pod Security, secrets management
2. **[Helm](/docs/tools/helm/)** — package management and templating
3. **[GitOps](/docs/tools/cicd/gitops)** — ArgoCD and FluxCD for declarative delivery
4. **Operators & Service Mesh** — custom controllers, Istio/Linkerd concepts

## Stage 4: Production Operations (2–3 weeks)

**[Production Operations](/docs/tools/kubernetes/production-operations)** covers the day-2 reality: monitoring with Prometheus/Grafana, logging, autoscaling (HPA/VPA/Cluster Autoscaler), troubleshooting, and multi-cluster patterns.

## Hands-On Practice

| Lab | What it's for |
|:----|:--------------|
| [Play with Docker](https://labs.play-with-docker.com/) | Free browser Docker sandbox |
| [Play with Kubernetes](https://labs.play-with-k8s.com/) | Free browser K8s cluster |
| [Killercoda](https://killercoda.com/) | Interactive CKA/CKAD-style scenarios |

## Prove It

1. **Certify** — the [Kubernetes exam-prep guide](/docs/tools/kubernetes/exam-prep) covers CKA, CKAD, and CKS: curriculum breakdown, time-saving tricks, and practice strategy.
2. **Interview** — 60+ [Kubernetes interview questions](/docs/tools/kubernetes/interview-questions) and the [Docker set](/docs/tools/docker/interview-questions).
3. **Level up** — continue with the [Platform Engineering path](/docs/learning-paths/platform-engineering) or the [SRE path](/docs/learning-paths/sre).
