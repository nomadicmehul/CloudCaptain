---
title: "Terraform Commands"
description: "Complete Terraform CLI reference"
---

# Terraform Commands Reference

## Workflow Commands

```bash
# Initialize — download providers and modules
terraform init

# Plan — preview what will change
terraform plan -out=tfplan

# Apply — execute the plan
terraform apply tfplan

# Destroy — remove all managed resources
terraform destroy
```

## State Management

```bash
# List resources in state
terraform state list

# Show a specific resource
terraform state show aws_instance.web

# Move a resource in state
terraform state mv aws_instance.old aws_instance.new

# Remove from state (without destroying)
terraform state rm aws_instance.web

# Import existing resource
terraform import aws_instance.web i-1234567890
```

## Workspace Commands

```bash
terraform workspace list
terraform workspace new staging
terraform workspace select production
terraform workspace delete staging
```
