---
title: "Azure"
sidebar_label: "Azure Overview"
description: "Comprehensive Microsoft Azure learning resources — fundamentals, security, DevOps, certifications, and interview prep"
sidebar_position: 0
---

# Microsoft Azure

Microsoft's cloud platform with 200+ products and services for building, running, and managing applications.

## Documentation

| Guide | Description |
|:------|:------------|
| [Azure Fundamentals](/docs/cloud/azure/fundamentals) | Global infrastructure, core services, resource management, Entra ID basics |
| [Security & Identity](/docs/cloud/azure/security-identity) | RBAC, Entra ID, Key Vault, Sentinel, Defender, Zero Trust, exercises |
| [Azure DevOps](/docs/cloud/azure/devops) | Azure Pipelines, Repos, IaC with ARM/Bicep/Terraform, deployment patterns |
| [Exam Prep](/docs/cloud/azure/exam-prep) | AZ-900, AZ-104 — exam domains, practice questions, study plans |
| [CLI & Services Cheat Sheet](/docs/cloud/azure/cheatsheet) | 100+ az CLI commands organized by service category |
| [Interview Questions](/docs/cloud/azure/interview-questions) | 50+ questions from beginner to advanced with detailed answers |

## Core Services

| Category | Services |
|:---------|:---------|
| **Compute** | VMs, AKS, Functions, App Service, Container Instances |
| **Storage** | Blob, Files, Disks, Data Lake |
| **Databases** | SQL Database, Cosmos DB, Cache for Redis |
| **Networking** | VNet, Load Balancer, Front Door, DNS, Application Gateway |
| **DevOps** | Azure DevOps, GitHub Actions, Azure Pipelines |
| **Security** | Entra ID, Key Vault, Sentinel, Defender for Cloud |
| **AI** | OpenAI Service, Cognitive Services, Machine Learning |

## Azure Certification Path

| Level | Certification | Focus |
|:------|:-------------|:------|
| Foundational | AZ-900 Azure Fundamentals | Cloud concepts, services, billing, security |
| Associate | AZ-104 Azure Administrator | Identity, networking, compute, storage, monitoring |
| Associate | AZ-204 Azure Developer | App development, CI/CD, Azure services |
| Expert | AZ-400 Azure DevOps Engineer | CI/CD, automation, monitoring at scale |
| Expert | AZ-305 Azure Solutions Architect | Multi-tier architectures, governance |

## Learning Path

1. [Start with fundamentals](/docs/cloud/azure/fundamentals) — understand Azure infrastructure, core services, pricing
2. [Learn security and identity](/docs/cloud/azure/security-identity) — RBAC, Entra ID, Key Vault, compliance
3. [Master Azure DevOps](/docs/cloud/azure/devops) — pipelines, IaC, deployment strategies
4. [Prepare for certification](/docs/cloud/azure/exam-prep) — AZ-900 and AZ-104 with practice questions
5. [Keep the cheat sheet handy](/docs/cloud/azure/cheatsheet) — 100+ az CLI commands

## Quick Start

```bash
# Login to Azure
az login

# Create a resource group
az group create --name myResourceGroup --location eastus

# Create a VM
az vm create \
  --resource-group myResourceGroup \
  --name myVM \
  --image Ubuntu2204 \
  --admin-username azureuser \
  --generate-ssh-keys

# Create an App Service
az webapp create \
  --resource-group myResourceGroup \
  --plan myAppServicePlan \
  --name myWebApp \
  --runtime "NODE:18-lts"
```

## External Resources

| Resource | Description |
|:---------|:------------|
| [Microsoft Learn](https://learn.microsoft.com/en-us/azure/) | Official Azure learning platform |
| [Azure Architecture Center](https://learn.microsoft.com/en-us/azure/architecture/) | Reference architectures and best practices |
| [Azure Charts](https://azurecharts.com/) | Visual tracking of Azure service updates |
| [Azure Speed](https://www.azurespeed.com/Azure/Latency) | Azure region latency test |
