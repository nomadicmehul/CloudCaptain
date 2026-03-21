---
title: "Kubernetes"
sidebar_label: "Kubernetes Overview"
description: "Comprehensive Kubernetes learning resources"
---

# Kubernetes

Master container orchestration with Kubernetes — the industry standard for deploying, scaling, and managing containerized applications.

## Quick Links

| Resource | Description |
|:---------|:------------|
| [Interview Prep](/docs/interview-prep/kubernetes) | K8s interview questions |
| [Play with K8s](https://labs.play-with-k8s.com/) | Free K8s playground |
| [Killercoda](https://killercoda.com/) | Interactive K8s scenarios |

## Core Architecture

```
Control Plane: API Server → etcd → Scheduler → Controller Manager
Worker Nodes:  kubelet → Container Runtime → kube-proxy
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

## Top Resources

| Title | Link |
|:------|:-----|
| Kubernetes Official Docs | [kubernetes.io/docs](https://kubernetes.io/docs/) |
| Kubernetes the Hard Way | [github.com/kelseyhightower](https://github.com/kelseyhightower/kubernetes-the-hard-way) |
| CNCF Landscape | [landscape.cncf.io](https://landscape.cncf.io/) |

## Essential kubectl Commands

```bash
# Get resources
kubectl get pods -A
kubectl get svc,deploy,ing -n my-namespace

# Describe a resource
kubectl describe pod my-pod

# Apply configuration
kubectl apply -f deployment.yaml

# Scale deployment
kubectl scale deployment my-app --replicas=3

# View logs
kubectl logs -f my-pod

# Port forward
kubectl port-forward svc/my-service 8080:80

# Debug
kubectl exec -it my-pod -- sh
kubectl top pods
```

## Certification Path

1. **KCNA** — Kubernetes and Cloud Native Associate
2. **CKA** — Certified Kubernetes Administrator
3. **CKAD** — Certified Kubernetes Application Developer
4. **CKS** — Certified Kubernetes Security Specialist

---

## Comprehensive Learning Resources

### Getting Started

| Resource | Description |
|:---------|:------------|
| [Kubernetes Official Site](https://kubernetes.io) | Official Kubernetes documentation by Google |
| [Kubernetes 101](https://medium.com/google-cloud/kubernetes-101-pods-nodes-containers-and-clusters-c1509e409e16) | Great beginner article on K8s fundamentals |
| [K8s Tutorial for Beginners](https://www.youtube.com/watch?v=X48VuDVv0do) | Full 4-hour video tutorial (TechWorld with Nana, 2020) |
| [Kubernetes Learning Path](https://developer.ibm.com/series/kubernetes-learning-path/) | IBM series from basic to advanced |
| [Katacoda Interactive Labs](https://www.katacoda.com/courses/kubernetes) | Learn K8s with interactive browser scenarios |
| [Kubernetes Scheduler Deep Dive](https://jvns.ca/blog/2017/07/27/how-does-the-kubernetes-scheduler-work/) | Understanding the K8s scheduler |

### Advanced Topics

| Resource | Description |
|:---------|:------------|
| [Kubernetes Networking](https://github.com/nleiva/kubernetes-networking-links) | Comprehensive K8s networking resources |
| [Liveness and Readiness Probes](https://www.openshift.com/blog/liveness-and-readiness-probes) | Health checks and pod lifecycle management |
| [Kubernetes Troubleshooting Guide](https://learnk8s.io/troubleshooting-deployments) | Visual guide to troubleshooting |
| [troubleshoot.sh](https://troubleshoot.sh) | kubectl plugin with diagnostic tools |

### Security Resources

| Resource | Description |
|:---------|:------------|
| [Kubescape](https://github.com/armosec/kubescape) | Security testing per NSA/CISA hardening guidance |
| [Falco](https://falco.org) | De facto Kubernetes threat detection engine |
| [OPA Gatekeeper](https://github.com/open-policy-agent/gatekeeper) | Policy-as-code for Kubernetes |

### Tools & Utilities

| Resource | Description |
|:---------|:------------|
| [Conftest](https://www.conftest.dev) | Test structured configuration data |
| [Datree](https://www.datree.io) | Prevent K8s misconfigurations in development |
| [Telepresence](https://www.telepresence.io) | Fast local development for K8s microservices |
| [KubeInvaders](https://github.com/lucky-sideburn/KubeInvaders) | Chaos engineering tool for K8s |
| [IngressMonitorController](https://github.com/stakater/IngressMonitorController) | Monitor ingresses and create liveness alerts |

### Certification Prep

| Resource | Description |
|:---------|:------------|
| [CKAD Practice Questions](https://github.com/bbachi/CKAD-Practice-Questions) | Consolidated CKAD exam prep questions |
| [CKAD Prep Exam Video](https://www.youtube.com/watch?v=TPXwVmvzlV4) | Full CKAD prep exam walkthrough (2020) |
| [CKA Complete Prep](https://github.com/walidshaari/Kubernetes-Certified-Administrator) | CKA exam preparation (2021) |
| [CKAD Exercises](https://github.com/dgkanatsios/CKAD-exercises) | Hands-on CKAD practice exercises (2021) |
| [CKS Prep](https://github.com/walidshaari/Certified-Kubernetes-Security-Specialist) | CKS security specialist preparation |

### Best Practices

#### Security Best Practices

- Secure inter-service communication (use Istio for mutual TLS)
- Isolate resources into separate namespaces by logical groups
- Use modern container runtimes (CRI-O + podman instead of Docker)
- Test cluster changes (use Datree for prevention)
- Implement access controls (OPA Gatekeeper)
- Apply NetworkPolicy for network security
- Monitor threats (use Falco)

#### Storage Best Practices

- Understand PersistentVolumes (PV), PersistentVolumeClaims (PVC), and StatefulSets
- Plan storage strategy for stateful applications
- Use appropriate storage classes for your workload
- Implement backup strategies for persistent data

#### Deployment Best Practices

- Use Deployments for stateless applications
- Use StatefulSets for applications requiring stable identity and storage
- Implement resource limits (CPU, memory)
- Configure health checks (liveness and readiness probes)
- Use rolling updates for zero-downtime deployments
- Implement proper logging and monitoring

## Books & PDFs

### Core Kubernetes Books

| Book | Link |
|:-----|:-----|
| Kubernetes Up & Running | [View PDF](https://github.com/nomadicmehul/CloudCaptain/blob/main/Kubernetes/Books/Kubernestes%20Up%20%26%20Running.pdf) |
| Introduction to Kubernetes | [View PDF](https://github.com/nomadicmehul/CloudCaptain/blob/main/Kubernetes/Books/Introduction%20to%20Kubernetes.pdf) |
| Kubernetes Essentials | [View PDF](https://github.com/nomadicmehul/CloudCaptain/blob/main/Kubernetes/Books/Kubernates_Essentials.pdf) |
| Everything About Kubernetes | [View PDF](https://github.com/nomadicmehul/CloudCaptain/blob/main/Kubernetes/Books/Everything%20about%20Kubernetes.pdf) |
| Kubernetes Stories | [View PDF](https://github.com/nomadicmehul/CloudCaptain/blob/main/Kubernetes/Books/Kubernetes%20Stories.pdf) |

### Kubernetes Architecture & Concepts

| Book | Link |
|:-----|:-----|
| Kubernetes Architecture Components | [View PDF](https://github.com/nomadicmehul/CloudCaptain/blob/main/Kubernetes/Books/Kubernetes%20Architecture%20Components.pdf) |
| Kubernetes Basic Concepts | [View PDF](https://github.com/nomadicmehul/CloudCaptain/blob/main/Kubernetes/Books/Kubernetes%20Basic%20Concepts.pdf) |
| Introduction to Kubernetes Components | [View PDF](https://github.com/nomadicmehul/CloudCaptain/blob/main/Kubernetes/Books/Introduction%20to%20Kubernetes%20Components%20and%20Architecture.pdf) |
| Kubernetes Namespaces | [View PDF](https://github.com/nomadicmehul/CloudCaptain/blob/main/Kubernetes/Books/Kubernetes%20Namespaces.pdf) |

### Kubernetes Administration & Operations

| Book | Link |
|:-----|:-----|
| Kubernetes Administration from Zero to Hero | [View PDF](https://github.com/nomadicmehul/CloudCaptain/blob/main/Kubernetes/Books/Kubernetes%20Administration%20from%20Zero%20to%20hero.pdf) |
| Cloud Native DevOps with Kubernetes | [View PDF](https://github.com/nomadicmehul/CloudCaptain/blob/main/Kubernetes/Books/Cloud%20Native%20DevOps%20with%20Kubernetes.pdf) |
| Kubernetes Best Practices | [View PDF](https://github.com/nomadicmehul/CloudCaptain/blob/main/Kubernetes/Books/Kubernetes%20Best%20Practices.pdf) |
| Kubernetes Crash Recovery Guide | [View PDF](https://github.com/nomadicmehul/CloudCaptain/blob/main/Kubernetes/Books/Kubernetes%20Crash%20Recovery%20Guide_V1.0.pdf) |

### Advanced Kubernetes Topics

| Book | Link |
|:-----|:-----|
| Containers and Kubernetes Security | [View PDF](https://github.com/nomadicmehul/CloudCaptain/blob/main/Kubernetes/Books/Containers%20and%20Kubernetes%20Security.pdf) |
| Diving Deep into Kubernetes Networking | [View PDF](https://github.com/nomadicmehul/CloudCaptain/blob/main/Kubernetes/Books/Diving%20Deep%20into%20Kubernetes%20Networking.pdf) |
| Kubernetes Hardening Guide | [View PDF](https://github.com/nomadicmehul/CloudCaptain/blob/main/Kubernetes/Books/Kubernetes%20Haedening%20Guide.pdf) |
| Kubernetes Privilege Escalation | [View PDF](https://github.com/nomadicmehul/CloudCaptain/blob/main/Kubernetes/Books/Kubernetes%20Privilege%20Escalation.pdf) |
| Kubernetes Security & Observability | [View PDF](https://github.com/nomadicmehul/CloudCaptain/blob/main/Kubernetes/Books/Kubernetes%20Security%20%26%20Observability.pdf) |

### Kubernetes for Beginners

| Book | Link |
|:-----|:-----|
| A Kubernetes Story | [View PDF](https://github.com/nomadicmehul/CloudCaptain/blob/main/Kubernetes/Books/Kubernetes%20for%20Beginners%20level/A%20Kubernetes%20Story.pdf) |
| Kubernetes an Introduction | [View PDF](https://github.com/nomadicmehul/CloudCaptain/blob/main/Kubernetes/Books/Kubernetes%20for%20Beginners%20level/Kubernetes%20An%20introduction.pdf) |
| Kubernetes Fundamentals | [View PDF](https://github.com/nomadicmehul/CloudCaptain/blob/main/Kubernetes/Books/Kubernetes%20for%20Beginners%20level/Kubernetes%20Fundamentals.pdf) |

### Exam Guides & Certifications

| Resource | Link |
|:---------|:-----|
| CKA Crash Course | [View PDF](https://github.com/nomadicmehul/CloudCaptain/blob/main/Kubernetes/Books/CKA%20Crash%20Course.pdf) |
| CKAD Crash Course | [View PDF](https://github.com/nomadicmehul/CloudCaptain/blob/main/Kubernetes/Books/CKAD%20Crash%20Course.pdf) |
| CKS Exam | [View PDF](https://github.com/nomadicmehul/CloudCaptain/blob/main/Kubernetes/Books/CKS%20Exam.pdf) |

### Cheat Sheets

| Cheat Sheet | Link |
|:------------|:-----|
| Kubernetes Cheat Sheet | [View PDF](https://github.com/nomadicmehul/CloudCaptain/blob/main/Kubernetes/Books/Kubernetes%20Cheat%20Sheet.pdf) |
