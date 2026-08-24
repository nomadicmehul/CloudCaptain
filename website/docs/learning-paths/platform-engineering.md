---
sidebar_position: 8
title: "Platform Engineering Path"
description: "Build Internal Developer Platforms — the senior track that builds on DevOps, IaC, and Kubernetes mastery"
---

# Platform Engineering Learning Path

Design and build Internal Developer Platforms (IDPs) that accelerate engineering teams. This is a **senior track**: plan **~12–16 weeks** if the prerequisites are fresh, longer if you're building them here.

**By the end you'll be able to:** design a self-service platform on Kubernetes, codify golden paths with IaC and GitOps, enforce guardrails with policy-as-code, and make the product case for a platform team — the skills behind the [Platform Engineer career path](/career-paths).

## What is Platform Engineering?

Platform Engineering is the practice of building internal platforms that give development teams self-service capabilities — reducing cognitive load and improving developer experience. If DevOps is a culture, the platform is its product.

## Stage 0: Prerequisites — learn them here

This path assumes solid ground in four areas, all covered on CloudCaptain:

| Prerequisite | Guide |
|:-------------|:------|
| CI/CD | [Fundamentals](/docs/tools/cicd/fundamentals) + [GitOps](/docs/tools/cicd/gitops) |
| Infrastructure as Code | [Terraform Fundamentals](/docs/tools/terraform/fundamentals) → [Advanced](/docs/tools/terraform/advanced) |
| Containers & Kubernetes | [Docker](/docs/tools/docker/fundamentals) → [Kubernetes](/docs/tools/kubernetes/fundamentals) → [Workloads](/docs/tools/kubernetes/workloads) |
| One cloud provider | [AWS](/docs/cloud/aws/fundamentals) · [Azure](/docs/cloud/azure/fundamentals) · [GCP](/docs/cloud/gcp/fundamentals) |

Not there yet? Run the [DevOps path](/docs/learning-paths/devops) and [Containers path](/docs/learning-paths/containers) first.

## Stage 1: Platform Foundations (2–3 weeks)

- Understand developer workflows and pain points — the platform's "user research"
- API design and service architecture — platforms are consumed programmatically
- Infrastructure automation at scale — [Terraform modules](/docs/tools/terraform/advanced) as reusable building blocks
- [Kubernetes production operations](/docs/tools/kubernetes/production-operations) — the substrate most IDPs run on

## Stage 2: Platform Building Blocks (3–4 weeks)

- **Service catalogs & developer portals** — Backstage, Port, Cortex
- **Self-service infrastructure** — Crossplane, Terraform modules behind an API
- **Golden paths** — templated, standardized workflows from repo-creation to production
- **GitOps delivery** — [ArgoCD/FluxCD](/docs/tools/cicd/gitops) as the deployment backbone
- **Internal CLIs** — custom tooling; [Bash](/docs/tools/bash/fundamentals) and [Python scripting](/docs/tools/python/devops-scripting) pay off here

## Stage 3: Advanced Topics (3–4 weeks)

- Multi-tenancy and resource management on shared clusters
- **Policy as Code** — OPA, Kyverno; guardrails instead of gates (see [Kubernetes Security](/docs/tools/kubernetes/security) and [DevSecOps](/docs/tools/devsecops/fundamentals))
- **[FinOps](/docs/cloud/finops/fundamentals) integration** — showback/chargeback per team
- Platform metrics and ROI — DORA metrics, developer satisfaction surveys
- Building and running platform teams

## Key Principles

1. **Treat your platform as a product** — your developers are your customers
2. **Self-service first** — reduce tickets and waiting
3. **Golden paths, not golden cages** — guide, don't restrict
4. **Measure everything** — DORA metrics, developer satisfaction
5. **Iterate continuously** — platforms are never "done"

## Prove It

1. **Build** — stand up a mini-IDP: Backstage + a Terraform module catalog + ArgoCD, letting a "developer" self-serve a new service to production. This is a portfolio-grade project.
2. **Interview** — platform interviews lean on the fundamentals: [Kubernetes](/docs/tools/kubernetes/interview-questions) · [Terraform](/docs/tools/terraform/interview-questions) · [CI/CD](/docs/tools/cicd/interview-questions) · [DevOps](/docs/learning-paths/devops)
3. **Level up** — the [SRE path](/docs/learning-paths/sre) is the natural sibling track.
