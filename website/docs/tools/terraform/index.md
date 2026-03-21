---
title: "Terraform"
sidebar_label: "Terraform Overview"
description: "Infrastructure as Code with Terraform"
---

# Terraform

Build, change, and version infrastructure safely and efficiently with HashiCorp Terraform.

## Core Concepts

| Concept | Description |
|:--------|:------------|
| **Provider** | Plugin to interact with cloud APIs (AWS, Azure, GCP) |
| **Resource** | Infrastructure component to manage |
| **State** | Terraform's record of managed infrastructure |
| **Module** | Reusable, composable Terraform configuration |
| **Plan** | Preview changes before applying |
| **Apply** | Execute planned changes |

## Getting Started

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

## Essential Commands

```bash
terraform init      # Initialize working directory
terraform plan      # Preview changes
terraform apply     # Apply changes
terraform destroy   # Destroy infrastructure
terraform fmt       # Format configuration
terraform validate  # Validate configuration
terraform state list # List resources in state
```

## Best Practices

- Use **remote state** (S3 + DynamoDB for AWS)
- **Lock state** to prevent concurrent modifications
- Use **modules** for reusable infrastructure
- Implement **workspaces** for environment separation
- Version pin providers and modules
- Use **terraform-docs** for documentation
- Scan with **tfsec** or **checkov** for security

## Learning Resources

### Tutorials and Guides

| Resource | Description |
|:---------|:------------|
| [How To Manage Secrets In Terraform](https://dev.to/kelvinskell/how-to-manage-secrets-in-terraform-like-a-pro-14nn) | Professional secrets management |
| [Reusable EC2 Instances Using Modules](https://betterprogramming.pub/reusable-ec2-instances-using-terraform-modules-59aac51f1fb) | Terraform modules best practices |
| [5 Tools to Auto-Generate Terraform](https://www.infracloud.io/blogs/auto-generate-terraform-configuration-files/) | Automation for terraform configuration |
| [Create Applications with Terraform in AWS](https://www.linkedin.com/pulse/createlaunch-application-using-terraform-aws-ritik-agarwal) | AWS deployment guide |

### Tools and Projects

| Tool | Description |
|:-----|:------------|
| [terraformer](https://github.com/GoogleCloudPlatform/terraformer) | Generate tf/json files from existing infrastructure |
| [terraforming](https://github.com/dtan4/terraforming) | Export AWS resources to Terraform style |
| [terrascan](https://github.com/tenable/terrascan) | Detect compliance and security violations in IaC |
| [terraform-kvm](https://github.com/dmacvicar/terraform-provider-libvirt) | Terraform provider for KVM |

## State Management Best Practices

### tfstate File

- **Never edit manually** — tfstate is designed to be managed by Terraform only
- **Store securely** — May contain credentials and sensitive data
- **Backup regularly** — Enable easy rollback when issues occur
- **Use remote storage** — Essential when working in teams
- **Enable versioning** — Supported by most remote backends (S3, etc.)
- **Lock state** — Prevent concurrent modifications with DynamoDB or similar

## Comprehensive Cheat Sheet

### Core Operations

```bash
# Initialize working directory
terraform init

# Validate configuration
terraform validate

# Preview changes before applying
terraform plan

# Apply configuration
terraform apply

# Destroy infrastructure
terraform destroy

# Format configuration
terraform fmt

# Show dependency graph
terraform graph

# Open Terraform console for testing expressions
terraform console
```

### State Management

```bash
# Show current state
terraform show

# List all resources in state
terraform state list

# Rename a resource in state
terraform state mv old.name new.name

# Import existing infrastructure into state
terraform import <resource_type>.<name> <id>
```

### Variables and Outputs

```bash
# Pass a variable
terraform apply -var="key=value"

# Pass a variables file
terraform apply -var-file="variables.tfvars"

# List all outputs
terraform output

# Get specific output value
terraform output <OUTPUT_NAME>
```

### Data Sources

```bash
# Syntax for accessing data sources
data.<PROVIDER_AND_TYPE>.<NAME>.<ATTRIBUTE>

# Example: Get AWS VPC by tag
data "aws_vpc" "default" {
  tags = { Name = "default" }
}
```

### Backends and Workspaces

```bash
# Initialize with specific backend configuration
terraform init -backend-config=backend.hcl

# Create a new workspace
terraform workspace new <WORKSPACE_NAME>

# Show current workspace
terraform workspace show

# Select a workspace
terraform workspace select <WORKSPACE_NAME>

# List all workspaces
terraform workspace list
```
| [terraform-kvm](https://github.com/dmacvicar/terraform-provider-libvirt) | Terraform Provider for KVM |

### State Management

**Don'ts:**
- Don't edit tfstate manually — designed for Terraform only
- Don't store sensitive data in tfstate insecurely

**Best Practices:**
- Store in secured location (contains credentials and sensitive data)
- Backup regularly for roll-backs
- Store in remote shared storage for team collaboration
- Enable versioning if storage supports it

### Variables and Data

| Command | Purpose |
|:--------|:--------|
| `terraform -var` | Pass variable at runtime |
| `terraform -var-file` | Pass variables file |
| `terraform output` | List all outputs |
| `terraform output <VAR>` | Get specific output |

### Advanced Operations

| Command | Purpose |
|:--------|:--------|
| `terraform graph` | View dependency graph |
| `terraform show` | Show current state |
| `terraform state list` | List resources in state |
| `terraform state mv` | Rename resource in state |
| `terraform import` | Import existing infrastructure |
| `terraform fmt` | Format configuration |
| `terraform validate` | Validate configuration |
| `terraform plan` | Preview changes |
| `terraform apply` | Apply changes |
| `terraform destroy` | Destroy infrastructure |

## Books & PDFs

### Core Terraform Books

| Book | Link |
|:-----|:-----|
| The Terraform Book | [View PDF](https://github.com/nomadicmehul/CloudCaptain/blob/main/TERRAFORM/Books/The%20Terraform%20Book.pdf) |
| Infrastructure as Code Using Terraform | [View PDF](https://github.com/nomadicmehul/CloudCaptain/blob/main/TERRAFORM/Books/Infrastructure%20as%20Code%20Using%20Terraform.pdf) |
| Infrastructure as Code | [View PDF](https://github.com/nomadicmehul/CloudCaptain/blob/main/TERRAFORM/Books/Infrastructure%20as%20Code.pdf) |
| Terraform Deep Dive | [View PDF](https://github.com/nomadicmehul/CloudCaptain/blob/main/TERRAFORM/Books/Terraform%20Deep%20Dive.pdf) |
| Terraform from Beginner to Master | [View PDF](https://github.com/nomadicmehul/CloudCaptain/blob/main/TERRAFORM/Books/Terraform%20from%20beginner%20to%20master.pdf) |

### Terraform Learning & Practice

| Book | Link |
|:-----|:-----|
| Terraform Basics to Advanced in One Guide | [View PDF](https://github.com/nomadicmehul/CloudCaptain/blob/main/TERRAFORM/Books/Terraform%20Basics%20to%20Advanced%20in%20One%20Guide_v1.0.pdf) |
| Terraform Deep Down with No Fear | [View PDF](https://github.com/nomadicmehul/CloudCaptain/blob/main/TERRAFORM/Books/Terraform%20Deep%20Down%20with%20no%20fear.pdf) |
| Terraform Walkthrough Notes | [View PDF](https://github.com/nomadicmehul/CloudCaptain/blob/main/TERRAFORM/Books/Terraform%20Walkthrough%20notes.pdf) |
| Learning Terraform Notes | [View PDF](https://github.com/nomadicmehul/CloudCaptain/blob/main/TERRAFORM/Books/Learning%20Terraform%20Notes.pdf) |
| Terraform Practice Questions | [View PDF](https://github.com/nomadicmehul/CloudCaptain/blob/main/TERRAFORM/Books/Terraform%20Practice%20Question.pdf) |

### Terraform Advanced Topics

| Book | Link |
|:-----|:-----|
| DevOps Automation with Terraform & VMWare | [View PDF](https://github.com/nomadicmehul/CloudCaptain/blob/main/TERRAFORM/Books/DevOps%20Automation%20With%20Terraform%20%26%20VMWare.pdf) |
| HashiCorp Nomad | [View PDF](https://github.com/nomadicmehul/CloudCaptain/blob/main/TERRAFORM/Books/HashiCorp%20Nomad.pdf) |

### Exam Guides & Certifications

| Resource | Link |
|:---------|:-----|
| Terraform Associate Exam Notes | [View PDF](https://github.com/nomadicmehul/CloudCaptain/blob/main/TERRAFORM/Exam%20Notes%20and%20Guides/Terraform%20Associate%20Exam%20notes.pdf) |
| Terraform Associate Practice Questions | [View PDF](https://github.com/nomadicmehul/CloudCaptain/blob/main/TERRAFORM/Exam%20Notes%20and%20Guides/terraform%20associate%20practice%20questions.pdf) |

### Cheat Sheets

| Cheat Sheet | Link |
|:------------|:-----|
| Terraform CLI Cheat Sheet | [View PDF](https://github.com/nomadicmehul/CloudCaptain/blob/main/TERRAFORM/Books/Terraform%20CLI%20Cheat%20Sheet.pdf) |
| Terraform CLI CheatSheet | [View PDF](https://github.com/nomadicmehul/CloudCaptain/blob/main/TERRAFORM/CheatSheets/Terraform%20CLI%20CheatSheet.pdf) |
