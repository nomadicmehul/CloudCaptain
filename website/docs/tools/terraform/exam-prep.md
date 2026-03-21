---
title: Terraform Certification Prep
sidebar_label: Exam Prep
sidebar_position: 4
---

# Terraform Certification Prep

Comprehensive guide for the HashiCorp Certified: Terraform Associate (003) exam.

## Exam Overview

- **Exam ID**: HashiCorp Certified: Terraform Associate (003)
- **Duration**: 60 minutes
- **Questions**: 57 multiple choice and multiple select
- **Passing Score**: 70%
- **Cost**: $70.50 USD
- **Format**: Online proctored
- **Prerequisites**: None (recommended: hands-on experience)

## 9 Exam Domains and Weights

1. **Understand IaC Concepts** (20%)
2. **Understand Terraform's Purpose** (17%)
3. **Understand Terraform Basics** (13%)
4. **Use Terraform Outside of Core Workflow** (8%)
5. **Interact with Terraform Modules** (12%)
6. **Manage Terraform State** (15%)
7. **Understand Terraform Cloud/Enterprise** (8%)
8. **Read, Generate, and Modify Configurations** (15%)
9. **Understand Terraform Security** (12%)

## Domain 1: Understand IaC Concepts (20%)

### Key Concepts

- **Codification**: Infrastructure defined in code rather than manual configuration
- **Version Control**: Track infrastructure changes with Git
- **Automation**: Reduce manual operations and human error
- **Consistency**: Reproduce environments reliably
- **Scalability**: Manage thousands of resources

### IaC Approaches

- **Declarative** (desired state): Terraform, CloudFormation, Ansible
- **Imperative** (step-by-step): Ansible, Chef, Puppet

### Benefits of IaC

- Disaster recovery (rebuild entire infrastructure)
- Environment parity (dev, staging, prod identical)
- Compliance and audit trails
- Self-documentation
- Cost optimization

### Best Practices

- Store code in version control
- Code review via pull requests
- Test configurations before production
- Automate deployment via CI/CD
- Document and maintain code quality

---

## Domain 2: Understand Terraform's Purpose (17%)

### When to Use Terraform

- Multi-cloud infrastructure management
- Complex, dynamic environments
- Team collaboration on infrastructure
- Infrastructure versioning and auditing
- Rapid environment provisioning

### Terraform Advantages vs Alternatives

| Feature | Terraform | CloudFormation | Ansible | Pulumi |
|---------|-----------|---|---|---|
| Multi-cloud | Yes | No | Yes | Yes |
| Language | HCL | YAML/JSON | YAML | Programming |
| State | Explicit | Implicit | Stateless | Explicit |
| Learning curve | Moderate | Steep | Easy | Steep |

### Use Cases

- **AWS/Azure/GCP multi-cloud**: Terraform excels
- **Immutable infrastructure**: Terraform + Packer
- **Complex dependencies**: Terraform's dependency graph
- **Team environments**: Terraform Cloud for collaboration
- **Legacy systems**: Terraform import for existing resources

---

## Domain 3: Understand Terraform Basics (13%)

### Terraform Workflow (Core Workflow)

1. **Write**: Create `.tf` files
2. **Plan**: `terraform plan` shows proposed changes
3. **Apply**: `terraform apply` creates/modifies resources
4. **Destroy**: `terraform destroy` removes resources

### Key Files and Directories

- `main.tf`: Primary configuration
- `variables.tf`: Input variables
- `outputs.tf`: Output values
- `terraform.tfvars`: Variable values
- `.terraform/`: Provider plugins and modules
- `.tfstate`: Local state file (don't commit!)
- `.gitignore`: Exclude sensitive files

### Initialization

```bash
terraform init
```

- Downloads provider plugins
- Initializes backend
- Validates configuration
- Sets up .terraform directory

### Providers

Plugins enabling Terraform to manage resources on cloud platforms and APIs.

```hcl
terraform {
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
  }
}

provider "aws" {
  region = "us-east-1"
}
```

### Resources vs Data Sources

- **Resources**: Objects you create and manage
- **Data sources**: Read-only queries of existing infrastructure

### State File

- Tracks deployed resources
- Enables resource updates and deletion
- Must be secured (contains secrets)
- Enable remote backends for teams
- Never commit to Git

---

## Domain 4: Use Terraform Outside of Core Workflow (8%)

### terraform console

Interactive REPL for testing expressions:

```bash
terraform console
> aws_instance.web.public_ip
> var.region
> data.aws_availability_zones.available.names
```

### terraform graph

Visualizes dependency graph:

```bash
terraform graph | dot -Tsvg > graph.svg
```

### terraform taint/untaint

Mark resources for recreation:

```bash
terraform taint aws_instance.web
terraform untaint aws_instance.web
terraform apply  # Recreates tainted resource
```

### terraform refresh

Update state to match real infrastructure:

```bash
terraform refresh
terraform refresh -target="aws_instance.web"
```

### terraform fmt

Auto-format HCL files:

```bash
terraform fmt            # Format current directory
terraform fmt -recursive # Recursively format
terraform fmt -check     # Check without modifying
```

### terraform validate

Check syntax and configuration:

```bash
terraform validate
terraform validate -json
```

---

## Domain 5: Interact with Terraform Modules (12%)

### Module Basics

Modules are reusable Terraform packages.

```hcl
module "vpc" {
  source = "./modules/vpc"

  vpc_cidr = "10.0.0.0/16"
  azs      = ["us-east-1a", "us-east-1b"]
}

# Access module outputs
resource "aws_security_group" "app" {
  vpc_id = module.vpc.vpc_id
}
```

### Module Sources

- **Local**: `./modules/vpc`
- **Terraform Registry**: `terraform-aws-modules/vpc/aws`
- **Git**: `git::https://github.com/example/repo.git`
- **HTTP**: `https://example.com/modules.zip`
- **S3**: `s3::https://bucket.s3.amazonaws.com/key`

### Terraform Registry

Public registry at https://registry.terraform.io:

```hcl
module "rds" {
  source  = "terraform-aws-modules/rds/aws"
  version = "~> 5.0"

  # Configuration
}
```

### Module Output Values

Modules expose outputs for composition:

```hcl
# modules/vpc/outputs.tf
output "vpc_id" {
  value = aws_vpc.main.id
}

# root/main.tf
resource "aws_security_group" "web" {
  vpc_id = module.vpc.vpc_id  # Use module output
}
```

### Module Input Variables

Modules accept input variables:

```hcl
# modules/vpc/variables.tf
variable "vpc_cidr" {
  type = string
}

# root/main.tf
module "vpc" {
  source   = "./modules/vpc"
  vpc_cidr = "10.0.0.0/16"
}
```

### Best Practices

- Use Terraform Registry for proven modules
- Validate module inputs with type constraints
- Document module purpose and usage
- Version modules in registry or Git
- Keep modules focused and reusable

---

## Domain 6: Manage Terraform State (15%)

### State File Purpose

- Tracks resource state and metadata
- Enables resource updates
- Maps resource names to cloud IDs
- Contains sensitive data (secrets, passwords)

### Local vs Remote State

- **Local**: Development only (risky for teams)
- **Remote**: Production standard (S3, Terraform Cloud, etc.)

### Remote State Backends

```hcl
terraform {
  backend "s3" {
    bucket         = "my-state-bucket"
    key            = "prod/terraform.tfstate"
    region         = "us-east-1"
    encrypt        = true
    dynamodb_table = "terraform-locks"
  }
}
```

### State Locking

Prevents concurrent modifications via DynamoDB:

```hcl
# Automatic with DynamoDB
dynamodb_table = "terraform-locks"
```

**During terraform apply:**
1. Lock entry created in DynamoDB
2. Changes applied
3. Lock released

### State Operations

```bash
# Show current state
terraform show
terraform state list
terraform state show aws_instance.web

# Modify state (use carefully)
terraform state mv old_name new_name
terraform state rm aws_instance.deprecated

# Pull remote state locally
terraform state pull > backup.tfstate

# Push local state remotely
terraform state push backup.tfstate
```

### State Security

- Encrypt state in transit (HTTPS, TLS)
- Encrypt state at rest (S3 encryption, KMS)
- Restrict access via IAM policies
- Enable versioning on state backend
- Regular backups
- Never commit to Git

### State Sensitivity

Mark sensitive data:

```hcl
variable "db_password" {
  type      = string
  sensitive = true
}

output "connection_string" {
  value     = "mysql://admin:${var.db_password}@db:3306/app"
  sensitive = true  # Not displayed in CLI
}
```

---

## Domain 7: Understand Terraform Cloud/Enterprise (8%)

### Terraform Cloud

SaaS platform for Terraform teams.

**Features:**
- Remote state management
- VCS integration (GitHub, GitLab, Azure DevOps)
- Runs (plan/apply) with approvals
- Cost estimation
- Sentinel policy as code
- Team management and RBAC
- Private module registry
- Run history and audit logs

### Configuration

```hcl
terraform {
  cloud {
    organization = "my-org"

    workspaces {
      name = "production"
    }
  }
}
```

### Workflows

1. **VCS-driven**: Push to Git → Terraform Cloud triggers plan
2. **CLI-driven**: `terraform apply` from CLI → runs in Terraform Cloud
3. **API-driven**: REST API for programmatic runs

### Sentinel Policy as Code

Enforce governance policies:

```hcl
# policy.sentinel
import "tfplan/v2" as tfplan

deny_large_instances = rule {
  all tfplan.resource_changes.aws_instance as _, instances {
    all instances[_].change.after.instance_type as instance_type {
      instance_type not in ["t2.micro", "t2.small", "t3.micro"]
    }
  }
}

main = rule {
  deny_large_instances
}
```

### Terraform Enterprise

Self-hosted version of Terraform Cloud for on-premises deployments.

---

## Domain 8: Read, Generate, and Modify Configurations (15%)

### Dependency Management

Terraform automatically manages explicit and implicit dependencies:

```hcl
# Explicit: depends_on
resource "aws_instance" "web" {
  depends_on = [aws_security_group.web]
}

# Implicit: reference other resources
resource "aws_security_group" "web" {
  vpc_id = aws_vpc.main.id  # Creates implicit dependency
}
```

### Resource Meta-Arguments

```hcl
resource "aws_instance" "web" {
  # depends_on: explicit dependencies
  depends_on = [aws_subnet.public]

  # count: create multiple instances
  count = var.instance_count

  # for_each: create instances from map
  for_each = var.instances

  # provider: specify provider alias
  provider = aws.us-west-2

  # lifecycle: control resource behavior
  lifecycle {
    create_before_destroy = true
  }

  # timeouts: set operation timeouts
  timeouts {
    create = "10m"
    delete = "10m"
  }
}
```

### Expressions and Functions

```hcl
# Conditionals
var.environment == "prod" ? "t3.large" : "t2.micro"

# Functions
concat(var.list1, var.list2)
merge(map1, map2)
lookup(var.tags, "Environment", "default")
contains(var.allowed_regions, var.region)

# For expressions
[for item in var.items : upper(item)]
{for k, v in var.map : k => upper(v)}
```

### Dynamic Blocks

Generate repetitive blocks:

```hcl
dynamic "ingress" {
  for_each = var.ingress_rules
  content {
    from_port   = ingress.value.port
    protocol    = ingress.value.protocol
    cidr_blocks = ingress.value.cidrs
  }
}
```

### Splat Expressions

Get values from multiple resources:

```hcl
aws_instance.web[*].id           # [id1, id2, id3]
aws_instance.web[*].public_ip    # [ip1, ip2, ip3]
```

---

## Domain 9: Understand Terraform Security (12%)

### State Security

- Store remotely (S3, Terraform Cloud)
- Enable encryption at rest and in transit
- Restrict access via IAM
- Enable versioning and MFA delete
- Regular backups

### Sensitive Data

```hcl
variable "api_key" {
  type      = string
  sensitive = true  # Not logged in console
}

output "db_password" {
  value     = random_password.db.result
  sensitive = true  # Not displayed in terraform output
}
```

### Provider Credentials

Never hardcode credentials:

```hcl
# WRONG
provider "aws" {
  access_key = "AKIAIOSFODNN7EXAMPLE"
  secret_key = "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"
}

# RIGHT: Use environment variables or IAM roles
provider "aws" {
  region = var.region
  # Credentials from environment or assume role
}
```

### Terraform Cloud Security

- Team members and permissions
- API tokens scoped to organizations
- Two-factor authentication (2FA)
- Audit logs
- Run approvals
- Sentinel policies

### Backend Security

- Encrypt state bucket
- Block public access
- Enable versioning
- Use IAM policies to restrict access
- Enable bucket logging

### Secrets Management

Integrate with secret managers:

```hcl
# AWS Secrets Manager
data "aws_secretsmanager_secret_version" "db_password" {
  secret_id = "prod/db/password"
}

resource "aws_db_instance" "main" {
  password = jsondecode(
    data.aws_secretsmanager_secret_version.db_password.secret_string
  ).password
}

# HashiCorp Vault
data "vault_generic_secret" "api_key" {
  path = "secret/api/key"
}

resource "aws_instance" "web" {
  user_data = templatefile("${path.module}/init.sh", {
    api_key = data.vault_generic_secret.api_key.data["key"]
  })
}
```

### Code Review and Approval

- Require plan review before apply
- Use Terraform Cloud run approvals
- Code review via pull requests
- Automated compliance checks

---

## Practice Questions (20)

### Question 1
You have a local state file and want to migrate to S3 backend. What is the recommended approach?

A) Copy the state file manually to S3
B) Use `terraform init` with backend configuration
C) Use `terraform state push`
D) Delete local state and re-apply

**Answer: B** - `terraform init` handles state migration automatically.

### Question 2
Which of the following is a valid provider source?

A) `aws`
B) `registry.terraform.io/hashicorp/aws`
C) `hashicorp/aws`
D) `https://aws-provider.com`

**Answer: C** - Short form with `source = "hashicorp/aws"` is valid.

### Question 3
What does `terraform plan -out=tfplan` do?

A) Creates a plan and immediately applies it
B) Saves the plan to a file for later review/apply
C) Plans and outputs results in JSON format
D) Destroys resources according to the plan

**Answer: B** - The `-out` flag saves the plan for later application.

### Question 4
How would you reference an output from a module named `vpc`?

A) `vpc.vpc_id`
B) `module.vpc.vpc_id`
C) `var.vpc.vpc_id`
D) `output.vpc.vpc_id`

**Answer: B** - Module outputs use `module.NAME.output_name` syntax.

### Question 5
Which backend provides state locking via DynamoDB?

A) Local
B) Terraform Cloud
C) S3 (with DynamoDB table)
D) Git

**Answer: C** - S3 backend supports locking with DynamoDB.

### Question 6
What is the purpose of `terraform.tfstate.backup`?

A) Backup for disaster recovery
B) Previous state version (created before each apply)
C) Encrypted state file
D) For remote backends only

**Answer: B** - `.backup` is the previous state version before the last apply.

### Question 7
How do you make a variable sensitive so it's not displayed in logs?

A) Use `private = true`
B) Use `sensitive = true`
C) Prefix with underscore: `_variable`
D) Store in Vault instead

**Answer: B** - The `sensitive` meta-argument hides the value.

### Question 8
What does `terraform import aws_instance.web i-1234567890abcdef0` do?

A) Creates a new instance
B) Adds existing resource to state file
C) Imports Terraform configuration from AWS
D) Backs up the resource

**Answer: B** - `terraform import` adds an existing resource to state.

### Question 9
Which is the correct way to use `for_each` in a resource?

A) `for_each = var.instances` (where instances is a list)
B) `for_each = var.instances` (where instances is a map)
C) `for_each = range(5)`
D) Both B and C

**Answer: D** - `for_each` accepts maps or sets; `range()` creates a list converted to set.

### Question 10
What happens if you use `count` with a value that changes?

A) Resources are reused with new indices
B) Resources are destroyed and recreated
C) Error message appears
D) Resources shift indices (causing unnecessary replacements)

**Answer: D** - Changing count value causes index shifts and resource replacement.

### Question 11
How do you prevent accidental resource destruction?

A) Use `prevent_destroy = true` in lifecycle
B) Delete the resource block
C) Set state to read-only
D) Use `-protect` flag

**Answer: A** - The `prevent_destroy` lifecycle argument blocks deletion.

### Question 12
What is Terraform Cloud?

A) AWS's infrastructure service
B) HashiCorp's SaaS platform for Terraform collaboration
C) A free hosting service for state files
D) A Terraform plugin marketplace

**Answer: B** - Terraform Cloud is HashiCorp's managed platform.

### Question 13
Which statement about modules is true?

A) Modules cannot have outputs
B) Modules can be nested
C) Modules require Terraform Cloud
D) Modules are only from the registry

**Answer: B** - Modules can be nested and combined hierarchically.

### Question 14
How do you set variable values via CLI?

A) `terraform apply region=us-west-2`
B) `terraform apply -var="region=us-west-2"`
C) `terraform apply --set region=us-west-2`
D) Set environment variables only

**Answer: B** - Use `-var="key=value"` to set variables.

### Question 15
What is the main advantage of remote state?

A) Faster plan/apply
B) Allows team collaboration and locking
C) Automatic backups only
D) Reduced file size

**Answer: B** - Remote state enables team workflows and prevents concurrent modifications.

### Question 16
How do you reference a data source in Terraform?

A) `var.datasource_name`
B) `data.datasource_type.datasource_name`
C) `module.datasource_name`
D) `datasource.name`

**Answer: B** - Data sources are referenced as `data.TYPE.NAME`.

### Question 17
What is the purpose of `.gitignore` in a Terraform project?

A) Ignore Terraform syntax errors
B) Exclude files from version control (like `.tfstate`)
C) Ignore module errors
D) Skip validation checks

**Answer: B** - `.gitignore` prevents committing sensitive files to Git.

### Question 18
Which command formats all Terraform files recursively?

A) `terraform fmt .`
B) `terraform fmt -recursive .`
C) `terraform format -all`
D) `terraform init --format`

**Answer: B** - The `-recursive` flag formats all files in subdirectories.

### Question 19
What does Sentinel policy as code do in Terraform Cloud?

A) Encrypts state files
B) Enforces governance and compliance policies
C) Manages user authentication
D) Compresses Terraform configurations

**Answer: B** - Sentinel enforces policy rules before runs are applied.

### Question 20
How do you create an EC2 instance using an implicit dependency?

A) `depends_on = [aws_security_group.web]`
B) Reference security group: `vpc_security_group_ids = [aws_security_group.web.id]`
C) Both create implicit dependency
D) Only explicit `depends_on` works

**Answer: B** - Referencing a resource creates an implicit dependency automatically.

---

## Study Tips

1. **Hands-on Practice**: Build actual infrastructure with Terraform
2. **Read Documentation**: Review official Terraform docs thoroughly
3. **Understand State**: Deep knowledge of state management is critical
4. **Module Experience**: Create and use modules in real projects
5. **Command Proficiency**: Know all major commands and flags
6. **Security Focus**: Understand secrets, sensitive data, and access control
7. **Compare Tools**: Know Terraform vs CloudFormation vs Pulumi
8. **Take Practice Tests**: Use HashiCorp's official practice exams
9. **Review Exam Objectives**: Spend extra time on high-weight domains
10. **Time Management**: 60 minutes for 57 questions = ~1 minute per question

---

## Resources

- [HashiCorp Terraform Docs](https://www.terraform.io/docs)
- [Terraform Associate Exam Guide](https://www.hashicorp.com/certification/terraform-associate)
- [Terraform Registry](https://registry.terraform.io)
- [Terraform Cloud Documentation](https://developer.hashicorp.com/terraform/cloud-docs)

Good luck on your exam!
