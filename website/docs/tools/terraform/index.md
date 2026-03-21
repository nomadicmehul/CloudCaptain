---
title: "Terraform"
sidebar_label: "Terraform Overview"
description: "Comprehensive Terraform learning resources — fundamentals, advanced patterns, modules, state management, exam prep, and interview questions"
sidebar_position: 0
---

# Terraform

Build, change, and version infrastructure safely and efficiently with HashiCorp Terraform.

## Documentation

| Guide | Description |
|:------|:------------|
| [Terraform Fundamentals](/docs/tools/terraform/fundamentals) | HCL syntax, providers, resources, variables, state, data sources, exercises |
| [Advanced Terraform](/docs/tools/terraform/advanced) | Modules, workspaces, remote state, Terragrunt, CI/CD, import, best practices |
| [Command Cheat Sheet](/docs/tools/terraform/cheatsheet) | 100+ CLI commands, HCL patterns, state operations, backend configuration |
| [Exam Prep](/docs/tools/terraform/exam-prep) | HashiCorp Terraform Associate (003) — domains, practice questions, study plan |
| [Interview Questions](/docs/tools/terraform/interview-questions) | 40+ questions from beginner to advanced with detailed answers |

## Core Concepts

| Concept | Description |
|:--------|:------------|
| **Provider** | Plugin to interact with cloud APIs (AWS, Azure, GCP) |
| **Resource** | Infrastructure component to manage |
| **State** | Terraform's record of managed infrastructure |
| **Module** | Reusable, composable Terraform configuration |
| **Plan** | Preview changes before applying |
| **Apply** | Execute planned changes |

## Learning Path

1. [Start with fundamentals](/docs/tools/terraform/fundamentals) — HCL syntax, providers, resources, variables, state
2. [Move to advanced patterns](/docs/tools/terraform/advanced) — modules, workspaces, remote backends, CI/CD
3. [Keep the cheat sheet handy](/docs/tools/terraform/cheatsheet) — 100+ commands at your fingertips
4. [Prepare for certification](/docs/tools/terraform/exam-prep) — HashiCorp Associate exam domains and practice
5. [Practice interview questions](/docs/tools/terraform/interview-questions) — 40+ Q&A with detailed explanations

## Quick Start

```hcl
# main.tf — Example AWS EC2 instance
terraform {
  required_providers {
    aws = { source = "hashicorp/aws", version = "~> 5.0" }
  }
}

provider "aws" { region = "us-east-1" }

resource "aws_instance" "web" {
  ami           = "ami-0c55b159cbfafe1f0"
  instance_type = "t3.micro"
  tags = { Name = "CloudCaptain-Web" }
}
```

```bash
terraform init      # Initialize working directory
terraform plan      # Preview changes
terraform apply     # Apply changes
terraform destroy   # Destroy infrastructure
terraform fmt       # Format configuration
terraform validate  # Validate configuration
```

## Best Practices

- Use **remote state** (S3 + DynamoDB for AWS)
- **Lock state** to prevent concurrent modifications
- Use **modules** for reusable infrastructure
- Implement **workspaces** for environment separation
- Version pin providers and modules
- Use **terraform-docs** for documentation
- Scan with **tfsec** or **checkov** for security

## External Resources

| Resource | Description |
|:---------|:------------|
| [Terraform Documentation](https://developer.hashicorp.com/terraform/docs) | Official HashiCorp docs |
| [Terraform Registry](https://registry.terraform.io/) | Providers, modules, and policies |
| [How To Manage Secrets In Terraform](https://dev.to/kelvinskell/how-to-manage-secrets-in-terraform-like-a-pro-14nn) | Professional secrets management |
| [Reusable EC2 Instances Using Modules](https://betterprogramming.pub/reusable-ec2-instances-using-terraform-modules-59aac51f1fb) | Terraform modules best practices |

## Tools & Utilities

| Tool | Description |
|:-----|:------------|
| [terraformer](https://github.com/GoogleCloudPlatform/terraformer) | Generate tf/json files from existing infrastructure |
| [terrascan](https://github.com/tenable/terrascan) | Detect compliance and security violations in IaC |
| [tfsec](https://github.com/aquasecurity/tfsec) | Static analysis for Terraform security |
| [checkov](https://github.com/bridgecrewio/checkov) | Policy-as-code for cloud infrastructure |
| [terraform-docs](https://github.com/terraform-docs/terraform-docs) | Auto-generate documentation from modules |
