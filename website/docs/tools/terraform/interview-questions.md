---
title: Terraform Interview Questions
sidebar_label: Interview Questions
sidebar_position: 5
---

# Terraform Interview Questions

Comprehensive guide with 40+ questions across beginner, intermediate, and advanced levels.

---

## Beginner Level

### Q1: What is Terraform and what problem does it solve?

**Answer:**

Terraform is an open-source Infrastructure as Code (IaC) tool by HashiCorp that allows you to define, provision, and manage cloud infrastructure using declarative configuration files written in HCL.

**Problem it solves:**
- Manual infrastructure management is error-prone and inconsistent
- Scaling infrastructure manually is time-consuming
- Disaster recovery requires rebuilding from scratch
- Tracking infrastructure changes is difficult
- Collaborating on infrastructure changes is challenging

Terraform enables you to version-control infrastructure, automate deployments, and maintain consistency across environments.

---

### Q2: What is the difference between declarative and imperative IaC?

**Answer:**

- **Declarative**: You specify the desired end state, and the tool figures out how to achieve it. Example: Terraform, CloudFormation.

```hcl
# Terraform (declarative) - specify what you want
resource "aws_instance" "web" {
  instance_type = "t2.micro"
  ami           = "ami-0c55b159cbfafe1f0"
}
```

- **Imperative**: You specify step-by-step instructions to achieve the desired state. Example: Ansible, Chef, Puppet.

```yaml
# Ansible (imperative) - specify how to do it
- hosts: webservers
  tasks:
    - name: Install package
      apt: name=nginx state=present
    - name: Start service
      service: name=nginx state=started
```

**Advantage of declarative**: Idempotency (safe to run multiple times). Terraform applies only needed changes.

---

### Q3: Explain the Terraform workflow.

**Answer:**

The core Terraform workflow has four steps:

1. **Write**: Create `.tf` files with resource definitions
2. **Plan**: Run `terraform plan` to preview changes
3. **Apply**: Run `terraform apply` to create/modify resources
4. **Destroy**: Run `terraform destroy` to remove resources

```bash
terraform init     # Download providers, initialize backend
terraform plan     # Preview changes
terraform apply    # Create resources
terraform destroy  # Remove resources
```

**Diagram:**
```
Write Config (.tf) → terraform init → terraform plan → terraform apply → Production
                                ↑
                                └─ Review changes, decide to proceed or not
```

---

### Q4: What are providers and why are they important?

**Answer:**

Providers are plugins that enable Terraform to manage resources on cloud platforms and APIs.

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

**Why important:**
- Enable multi-cloud infrastructure (AWS, Azure, GCP, etc.)
- Define authentication credentials
- Set default configurations
- Support 1000+ different services and providers

**Common providers:**
- `hashicorp/aws` (Amazon Web Services)
- `hashicorp/azurerm` (Azure)
- `hashicorp/google` (Google Cloud)
- `hashicorp/kubernetes` (Kubernetes)
- `hashicorp/null` (Testing utilities)

---

### Q5: What is state and why is it important?

**Answer:**

State is a JSON file that Terraform maintains to track real-world resources it has created.

```json
{
  "resources": [
    {
      "type": "aws_instance",
      "name": "web",
      "instances": [{
        "attributes": {
          "id": "i-0123456789abcdef0",
          "public_ip": "203.0.113.42"
        }
      }]
    }
  ]
}
```

**Why it's important:**
- **Tracking**: Maps resource names (`aws_instance.web`) to real IDs (`i-12345`)
- **Updates**: Terraform knows what already exists, so it only changes what's needed
- **Deletion**: Terraform knows what to destroy
- **Outputs**: Provides values for downstream resources

**Criticality**: Without state, Terraform can't manage infrastructure.

**Security considerations:**
- Contains sensitive data (passwords, API keys)
- Should be encrypted at rest and in transit
- Should never be committed to Git
- Requires restricted access (IAM policies)

---

### Q6: What is the difference between resources and data sources?

**Answer:**

| Aspect | Resource | Data Source |
|--------|----------|---|
| **Purpose** | Create, modify, delete infrastructure | Query existing infrastructure (read-only) |
| **State tracking** | Tracked in state file | Not tracked in state |
| **Example** | `aws_instance` | `aws_ami` |

**Resource example:**

```hcl
resource "aws_instance" "web" {
  ami           = "ami-0c55b159cbfafe1f0"
  instance_type = "t2.micro"
}
```

**Data source example:**

```hcl
data "aws_ami" "ubuntu" {
  most_recent = true
  owners      = ["099720109477"]  # Canonical

  filter {
    name   = "name"
    values = ["ubuntu/images/hvm-ssd/ubuntu-jammy-22.04-*"]
  }
}

# Use in resource
resource "aws_instance" "web" {
  ami           = data.aws_ami.ubuntu.id
  instance_type = "t2.micro"
}
```

**Key difference**: Data sources are read-only queries; resources are managed objects.

---

### Q7: How do you define and use variables in Terraform?

**Answer:**

Variables are input parameters for your configuration.

**Definition** (in `variables.tf`):

```hcl
variable "instance_type" {
  description = "EC2 instance type"
  type        = string
  default     = "t2.micro"
}

variable "instance_count" {
  description = "Number of instances to create"
  type        = number
  default     = 1
}

variable "tags" {
  description = "Tags to apply to resources"
  type        = map(string)
  default = {
    Environment = "dev"
  }
}
```

**Usage** (in `main.tf`):

```hcl
resource "aws_instance" "web" {
  count         = var.instance_count
  instance_type = var.instance_type
  tags          = var.tags
}
```

**Setting variables:**

```bash
# Via CLI flag
terraform apply -var="instance_type=t3.small"

# Via tfvars file
terraform apply -var-file="prod.tfvars"

# Via environment variables
export TF_VAR_instance_type="t3.small"
terraform apply

# Interactive prompt
terraform apply  # Prompts for variables without defaults
```

---

### Q8: What are outputs and when would you use them?

**Answer:**

Outputs are values exported from your Terraform configuration.

```hcl
output "instance_id" {
  description = "EC2 instance ID"
  value       = aws_instance.web.id
}

output "public_ip" {
  description = "Public IP address"
  value       = aws_instance.web.public_ip
}
```

**When to use outputs:**
- Display important values after `apply`
- Pass values between modules
- Expose values to other systems/scripts
- Create dashboards or reports

**Example output after apply:**

```
Outputs:

instance_id = "i-0123456789abcdef0"
public_ip = "203.0.113.42"
```

**Sensitive outputs:**

```hcl
output "database_password" {
  value     = aws_db_instance.main.password
  sensitive = true  # Not displayed in console
}
```

**Accessing outputs:**

```bash
terraform output instance_id
# i-0123456789abcdef0

terraform output -json
# {"instance_id": {"value": "i-..."}, ...}
```

---

### Q9: What is the purpose of terraform.tfvars?

**Answer:**

`terraform.tfvars` is a file that sets default values for variables without using CLI flags.

**Example** (`terraform.tfvars`):

```hcl
instance_type = "t3.small"
region        = "us-west-2"
environment   = "production"

tags = {
  Environment = "production"
  Team        = "platform"
}
```

**Advantages:**
- Cleaner than CLI `-var` flags
- Persists configuration in version control
- Environment-specific files (dev.tfvars, prod.tfvars)

**Using different tfvars files:**

```bash
terraform apply -var-file="dev.tfvars"
terraform apply -var-file="prod.tfvars"
```

**Auto-loaded files:**
- `terraform.tfvars` (always loaded)
- `*.auto.tfvars` (automatically loaded)

---

### Q10: How do you reference another resource in Terraform?

**Answer:**

Use dot notation: `RESOURCE_TYPE.RESOURCE_NAME.ATTRIBUTE`

```hcl
# Create VPC
resource "aws_vpc" "main" {
  cidr_block = "10.0.0.0/16"
}

# Reference VPC in security group
resource "aws_security_group" "web" {
  vpc_id = aws_vpc.main.id  # Reference the VPC's ID

  ingress {
    from_port   = 80
    to_port     = 80
    protocol    = "tcp"
    cidr_blocks = ["${aws_vpc.main.cidr_block}/32"]  # Use VPC CIDR
  }
}
```

**This creates an implicit dependency**: Terraform knows to create the VPC before the security group.

---

## Intermediate Level

### Q11: Explain the difference between local and remote state. When should you use each?

**Answer:**

| Aspect | Local | Remote |
|--------|-------|--------|
| **Storage** | `.tfstate` file on disk | S3, Terraform Cloud, etc. |
| **Locking** | None | Supported (DynamoDB, etc.) |
| **Sharing** | Manual (email, repos) | Built-in (team access) |
| **Security** | Risk (in Git, on disk) | Encrypted, access controlled |
| **Use case** | Solo development | Teams, production |

**Local state:**

```bash
# Default - state stored locally
terraform init
# Creates ./terraform.tfstate
```

**Remote state (S3):**

```hcl
terraform {
  backend "s3" {
    bucket         = "my-terraform-state"
    key            = "prod/terraform.tfstate"
    region         = "us-east-1"
    encrypt        = true
    dynamodb_table = "terraform-locks"
  }
}
```

**When to use:**
- **Local**: Solo projects, learning, temporary infrastructure
- **Remote**: Teams, production, any shared environment

**Best practice**: Always use remote state in production.

---

### Q12: What is state locking and how does it work?

**Answer:**

State locking prevents multiple users from modifying state simultaneously, causing conflicts.

**How it works:**

1. User A runs `terraform apply`
2. Lock entry created in DynamoDB
3. User B attempts `terraform apply` → waits or errors
4. User A completes apply
5. Lock released
6. User B can proceed

**S3 backend with DynamoDB locking:**

```hcl
terraform {
  backend "s3" {
    bucket         = "my-terraform-state"
    key            = "terraform.tfstate"
    region         = "us-east-1"
    encrypt        = true
    dynamodb_table = "terraform-locks"  # Enables locking
  }
}
```

**DynamoDB table setup:**

```bash
aws dynamodb create-table \
  --table-name terraform-locks \
  --attribute-definitions AttributeName=LockID,AttributeType=S \
  --key-schema AttributeName=LockID,KeyType=HASH \
  --billing-mode PAY_PER_REQUEST
```

**Lock entry example:**

```json
{
  "LockID": "my-terraform-state/prod/terraform.tfstate",
  "Info": "...",
  "Who": "user@example.com",
  "Version": "1.5.0",
  "Created": "2024-03-21T10:30:00Z"
}
```

**Manual operations:**

```bash
# Unlock manually (use with caution)
terraform force-unlock LOCK_ID
```

---

### Q13: What are modules and why would you use them?

**Answer:**

Modules are reusable, self-contained Terraform packages that group related resources.

**Benefits:**
- **Reusability**: Use same module in multiple projects
- **Organization**: Logical grouping of resources
- **Maintainability**: Updates in one place
- **Composition**: Build complex infrastructure from simple pieces
- **Encapsulation**: Hide implementation details

**Module structure:**

```
modules/vpc/
  main.tf       # Resource definitions
  variables.tf  # Input variables
  outputs.tf    # Outputs
  README.md     # Documentation
```

**Using a module:**

```hcl
module "vpc" {
  source = "./modules/vpc"

  vpc_cidr           = "10.0.0.0/16"
  availability_zones = ["us-east-1a", "us-east-1b"]
  public_subnets     = ["10.0.1.0/24", "10.0.2.0/24"]
}

resource "aws_security_group" "app" {
  vpc_id = module.vpc.vpc_id
}
```

**Module sources:**

```hcl
# Local module
source = "./modules/vpc"

# Terraform Registry
source = "terraform-aws-modules/vpc/aws"
version = "~> 5.0"

# Git
source = "git::https://github.com/example/repo.git"

# HTTP
source = "https://example.com/modules.zip"

# S3
source = "s3::https://bucket.s3.amazonaws.com/modules.zip"
```

---

### Q14: How do you manage multiple environments (dev, staging, prod) with Terraform?

**Answer:**

**Approach 1: Workspaces**

```bash
# Create workspaces
terraform workspace new dev
terraform workspace new staging
terraform workspace new prod

# Select workspace
terraform workspace select prod

# Use workspace-specific variables
resource "aws_instance" "web" {
  instance_type = var.instance_types[terraform.workspace]
}
```

**variables.tf:**

```hcl
variable "instance_types" {
  type = map(string)
  default = {
    dev     = "t2.micro"
    staging = "t2.small"
    prod    = "t3.large"
  }
}
```

**Approach 2: Separate Directories**

```
infrastructure/
  dev/
    main.tf
    variables.tf
    terraform.tfvars
  staging/
    main.tf
    variables.tf
    terraform.tfvars
  prod/
    main.tf
    variables.tf
    terraform.tfvars
```

**Approach 3: Variable Files**

```bash
terraform plan -var-file="dev.tfvars"
terraform plan -var-file="prod.tfvars"
```

**Approach 4: Modules + Workspaces (Best)**

Combine approaches:

```hcl
# Root module using workspaces
module "vpc" {
  source = "./modules/vpc"

  vpc_cidr    = var.vpc_cidrs[terraform.workspace]
  environment = terraform.workspace
}

# Use workspace-specific tfvars
# dev.tfvars, staging.tfvars, prod.tfvars
```

**Recommendations:**
- Use workspaces for similar environments
- Use separate directories for drastically different setups
- Terraform Cloud for team-based environment management

---

### Q15: What is `terraform import` and when would you use it?

**Answer:**

`terraform import` adds existing cloud resources to your Terraform state without recreating them.

**Syntax:**

```bash
terraform import RESOURCE_TYPE.RESOURCE_NAME CLOUD_RESOURCE_ID
```

**Example:**

```bash
# Import existing EC2 instance
terraform import aws_instance.web i-1234567890abcdef0

# Import existing security group
terraform import aws_security_group.web sg-12345678

# Import into module
terraform import module.vpc.aws_vpc.main vpc-12345678
```

**After import**, create resource block in configuration:

```hcl
resource "aws_instance" "web" {
  # Terraform fills in attributes from state
  # You may need to add/fix some manually
}
```

**When to use:**
- Adopt existing infrastructure into Terraform
- Recover from accidental deletion
- Migrate from manual management to IaC
- Integrate legacy systems

**Limitations:**
- Requires manual resource block creation
- Not all attributes may be imported
- Doesn't import associated resources (e.g., attached volumes)

---

### Q16: Explain the difference between count and for_each.

**Answer:**

Both create multiple instances of a resource, but with different tradeoffs.

**count (Index-based):**

```hcl
variable "instance_count" {
  type    = number
  default = 3
}

resource "aws_instance" "server" {
  count         = var.instance_count
  instance_type = "t2.micro"

  tags = {
    Name = "server-${count.index + 1}"
  }
}

output "instance_ids" {
  value = aws_instance.server[*].id  # [id0, id1, id2]
}
```

**Problem with count**: Changing the count value shifts indices, causing unnecessary resource replacement.

```hcl
# Instance count changes from 3 to 2
# count.0, count.1, count.2 → count.0, count.1
# count.2 is destroyed even though server-3 still exists
```

**for_each (Key-based):**

```hcl
variable "instances" {
  type = map(object({
    instance_type = string
  }))
  default = {
    web = {
      instance_type = "t2.micro"
    }
    app = {
      instance_type = "t2.small"
    }
  }
}

resource "aws_instance" "server" {
  for_each      = var.instances
  instance_type = each.value.instance_type

  tags = {
    Name = each.key
  }
}

output "instance_ids" {
  value = {
    for k, v in aws_instance.server : k => v.id
  }
  # {web = "i-...", app = "i-..."}
}
```

**Advantages of for_each:**
- Keys remain stable when variables change
- Safe refactoring without resource replacement
- Clearer output references

**When to use:**
- **count**: Simple numbering (1, 2, 3...)
- **for_each**: Map-based or complex scenarios (safer)

---

### Q17: What is a provisioner and when should you use one?

**Answer:**

Provisioners run scripts or commands on resources during creation or destruction.

**Types:**

```hcl
resource "aws_instance" "web" {
  ami           = "ami-0c55b159cbfafe1f0"
  instance_type = "t2.micro"

  # Local execution
  provisioner "local-exec" {
    command = "echo 'Instance created' > log.txt"
  }

  # Remote SSH execution
  provisioner "remote-exec" {
    inline = [
      "sudo apt-get update",
      "sudo apt-get install -y nginx"
    ]

    connection {
      type        = "ssh"
      user        = "ec2-user"
      private_key = file("~/.ssh/id_rsa")
      host        = self.public_ip
    }
  }

  # File provisioner
  provisioner "file" {
    source      = "local/app.conf"
    destination = "/tmp/app.conf"

    connection {
      type        = "ssh"
      user        = "ec2-user"
      host        = self.public_ip
    }
  }
}
```

**When to use:**
- Last resort (indicates design issues)
- Simple post-creation setup
- Most of the time, use `user_data` or cloud-init instead

**Better alternatives:**

```hcl
# Instead of provisioner, use user_data
resource "aws_instance" "web" {
  user_data = <<-EOF
              #!/bin/bash
              apt-get update
              apt-get install -y nginx
              systemctl start nginx
              EOF
}

# Use configuration management (Ansible, Chef, Puppet)
resource "aws_instance" "web" {
  # Let Ansible configure the instance after creation
}
```

**Why avoid provisioners:**
- Hidden dependencies (Terraform can't see what they do)
- Not idempotent (running twice may cause issues)
- Hard to debug (commands may fail silently)
- Configuration management tools are better

---

### Q18: What is a dynamic block and when would you use it?

**Answer:**

Dynamic blocks generate repetitive nested blocks from variables.

**Without dynamic blocks** (repetitive):

```hcl
resource "aws_security_group" "web" {
  name = "web-sg"

  ingress {
    from_port   = 80
    to_port     = 80
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }

  ingress {
    from_port   = 443
    to_port     = 443
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }

  ingress {
    from_port   = 22
    to_port     = 22
    protocol    = "tcp"
    cidr_blocks = ["10.0.0.0/8"]
  }
}
```

**With dynamic blocks** (cleaner):

```hcl
variable "ingress_rules" {
  type = list(object({
    port     = number
    protocol = string
    cidr     = string
  }))
  default = [
    { port = 80, protocol = "tcp", cidr = "0.0.0.0/0" },
    { port = 443, protocol = "tcp", cidr = "0.0.0.0/0" },
    { port = 22, protocol = "tcp", cidr = "10.0.0.0/8" }
  ]
}

resource "aws_security_group" "web" {
  name = "web-sg"

  dynamic "ingress" {
    for_each = var.ingress_rules

    content {
      from_port   = ingress.value.port
      to_port     = ingress.value.port
      protocol    = ingress.value.protocol
      cidr_blocks = [ingress.value.cidr]
    }
  }
}
```

**When to use:**
- Flexible configurations from variables
- Avoid repetitive code
- Support variable-length lists

---

### Q19: What are locals and how do they differ from variables?

**Answer:**

| Aspect | Variables | Locals |
|--------|-----------|--------|
| **Purpose** | External inputs | Internal computed values |
| **Definition** | `variable` block | `local` block |
| **Setting** | CLI, files, environment | Only in configuration |
| **Reusability** | Across modules | Within module only |

**Variables (external inputs):**

```hcl
variable "environment" {
  type    = string
  default = "dev"
}
```

**Locals (computed values):**

```hcl
locals {
  # Simple value
  environment_short = substr(var.environment, 0, 3)

  # Conditional
  instance_count = var.environment == "prod" ? 5 : 1

  # Common tags
  common_tags = {
    Environment = var.environment
    Project     = "CloudCaptain"
    ManagedBy   = "Terraform"
  }

  # Name prefix
  name_prefix = "${var.environment}-${var.region}"
}

resource "aws_instance" "web" {
  count = local.instance_count

  tags = merge(local.common_tags, {
    Name = "${local.name_prefix}-web-${count.index + 1}"
  })
}
```

**When to use:**
- **Variables**: Input flexibility, external configuration
- **Locals**: Reduce repetition, computed values, simplify expressions

---

### Q20: Explain conditional expressions in Terraform.

**Answer:**

Conditional expressions use ternary operator syntax: `CONDITION ? TRUE_VAL : FALSE_VAL`

```hcl
resource "aws_instance" "web" {
  # Simple conditional
  instance_type = var.environment == "production" ? "t3.large" : "t2.micro"

  # Conditional with locals
  root_block_device {
    volume_size = var.enable_large_disk ? 100 : 20
  }
}

locals {
  # Create RDS only in production
  create_rds = var.deploy_database ? 1 : 0
}

resource "aws_db_instance" "main" {
  count = local.create_rds
  # ...
}

# Nested conditionals
variable "environment" {
  type = string
}

locals {
  instance_type = var.environment == "prod" ? "t3.xlarge" : \
                  var.environment == "staging" ? "t3.medium" : "t2.micro"
}
```

**Common use cases:**
- Environment-based sizing
- Feature flags
- Conditional resource creation
- Conditional output values

---

## Advanced Level

### Q21: How would you structure a large Terraform project with multiple teams and environments?

**Answer:**

**Recommended structure:**

```
infrastructure/
  modules/
    vpc/
    security_groups/
    databases/
    load_balancers/
    monitoring/
  environments/
    dev/
      main.tf
      variables.tf
      dev.tfvars
      terraform.tf (backend config)
    staging/
      main.tf
      variables.tf
      staging.tfvars
      terraform.tf
    prod/
      main.tf
      variables.tf
      prod.tfvars
      terraform.tf
  shared/
    variables.tf (shared variable definitions)
    outputs.tf
```

**Modules structure** (`modules/vpc/`):

```hcl
# modules/vpc/variables.tf
variable "vpc_cidr" {
  type = string
}

variable "public_subnets" {
  type = list(string)
}

# modules/vpc/outputs.tf
output "vpc_id" {
  value = aws_vpc.main.id
}

# modules/vpc/main.tf
resource "aws_vpc" "main" {
  cidr_block = var.vpc_cidr
}
```

**Environment usage** (`environments/prod/main.tf`):

```hcl
module "vpc" {
  source = "../../modules/vpc"

  vpc_cidr       = var.vpc_cidr
  public_subnets = var.public_subnets
}

module "security_groups" {
  source = "../../modules/security_groups"

  vpc_id = module.vpc.vpc_id
}

resource "aws_instance" "app" {
  vpc_security_group_ids = [module.security_groups.app_sg_id]
}
```

**Best practices:**
- Keep modules focused and reusable
- Use Terraform Registry for standard modules
- Separate state per environment
- Use workspaces or separate backends
- Document module inputs and outputs
- Version modules in Git or registry

---

### Q22: How do you handle sensitive data and secrets in Terraform?

**Answer:**

**1. Mark outputs as sensitive:**

```hcl
output "database_password" {
  value     = random_password.db.result
  sensitive = true  # Not displayed in console
}

variable "api_key" {
  type      = string
  sensitive = true
}
```

**2. Never hardcode secrets:**

```hcl
# WRONG
variable "aws_secret" {
  default = "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"
}

# RIGHT
variable "aws_secret" {
  type      = string
  sensitive = true
  # Provide via environment or secure backend
}
```

**3. Use AWS Secrets Manager:**

```hcl
data "aws_secretsmanager_secret_version" "db_password" {
  secret_id = "prod/database/password"
}

resource "aws_db_instance" "main" {
  password = jsondecode(
    data.aws_secretsmanager_secret_version.db_password.secret_string
  )["password"]
}
```

**4. Use HashiCorp Vault:**

```hcl
provider "vault" {
  address = "https://vault.example.com:8200"
}

data "vault_generic_secret" "api_key" {
  path = "secret/data/myapp/api_key"
}

resource "aws_instance" "web" {
  user_data = templatefile("${path.module}/init.sh", {
    api_key = data.vault_generic_secret.api_key.data["key"]
  })
}
```

**5. Encrypt state:**

```hcl
terraform {
  backend "s3" {
    bucket         = "my-state"
    key            = "terraform.tfstate"
    region         = "us-east-1"
    encrypt        = true  # Server-side encryption
    dynamodb_table = "terraform-locks"
  }
}
```

**6. Environment variables:**

```bash
export TF_VAR_database_password="secret123"
terraform apply
```

**Best practices:**
- Never commit secrets to Git
- Use .gitignore for tfvars with secrets
- Enable encryption on state backends
- Use IAM roles instead of keys
- Rotate secrets regularly
- Audit access to state files
- Use Terraform Cloud for secret management

---

### Q23: How would you implement CI/CD for Terraform?

**Answer:**

**GitHub Actions pipeline:**

```yaml
name: Terraform

on:
  pull_request:
    paths:
      - 'infrastructure/**'
  push:
    branches: [main]
    paths:
      - 'infrastructure/**'

jobs:
  terraform:
    runs-on: ubuntu-latest

    steps:
      - uses: actions/checkout@v3

      - name: Setup Terraform
        uses: hashicorp/setup-terraform@v2
        with:
          terraform_version: 1.5.0

      - name: Initialize Terraform
        run: |
          cd infrastructure/prod
          terraform init

      - name: Format Check
        run: |
          cd infrastructure/prod
          terraform fmt -check -recursive

      - name: Validate
        run: |
          cd infrastructure/prod
          terraform validate

      - name: Plan
        run: |
          cd infrastructure/prod
          terraform plan -out=tfplan
        env:
          AWS_ACCESS_KEY_ID: ${{ secrets.AWS_ACCESS_KEY_ID }}
          AWS_SECRET_ACCESS_KEY: ${{ secrets.AWS_SECRET_ACCESS_KEY }}

      - name: Upload Plan
        uses: actions/upload-artifact@v3
        with:
          name: tfplan
          path: infrastructure/prod/tfplan

      - name: Comment Plan on PR
        if: github.event_name == 'pull_request'
        uses: actions/github-script@v6
        with:
          script: |
            const fs = require('fs');
            const plan = fs.readFileSync('infrastructure/prod/tfplan', 'utf8');
            github.rest.issues.createComment({
              issue_number: context.issue.number,
              owner: context.repo.owner,
              repo: context.repo.repo,
              body: '```\n' + plan + '\n```'
            });

  apply:
    if: github.ref == 'refs/heads/main' && github.event_name == 'push'
    needs: terraform
    runs-on: ubuntu-latest

    steps:
      - uses: actions/checkout@v3

      - name: Setup Terraform
        uses: hashicorp/setup-terraform@v2

      - name: Download Plan
        uses: actions/download-artifact@v3
        with:
          name: tfplan

      - name: Apply
        run: |
          cd infrastructure/prod
          terraform init
          terraform apply -auto-approve tfplan
        env:
          AWS_ACCESS_KEY_ID: ${{ secrets.AWS_ACCESS_KEY_ID }}
          AWS_SECRET_ACCESS_KEY: ${{ secrets.AWS_SECRET_ACCESS_KEY }}
```

**Best practices:**
- Require plan review before apply
- Separate plan and apply jobs
- Use protected branches for production
- Store secrets in GitHub Secrets
- Comment plans on pull requests
- Run linting and validation on every PR
- Require approval for production changes

---

### Q24: How do you test Terraform code?

**Answer:**

**Unit tests with `terraform test`:**

```hcl
# main.tf
variable "create_instance" {
  type    = bool
  default = true
}

resource "aws_instance" "web" {
  count         = var.create_instance ? 1 : 0
  instance_type = "t2.micro"
}

# tests.tf
run "test_instance_creation" {
  command = plan

  variables {
    create_instance = true
  }

  assert {
    condition     = length(aws_instance.web) == 1
    error_message = "Should create one instance"
  }
}

run "test_no_instance_when_disabled" {
  command = plan

  variables {
    create_instance = false
  }

  assert {
    condition     = length(aws_instance.web) == 0
    error_message = "Should not create instance"
  }
}
```

**Integration testing with Terratest (Go):**

```go
package test

import (
  "testing"
  "github.com/gruntwork-io/terratest/modules/terraform"
)

func TestTerraformExample(t *testing.T) {
  terraformOptions := &terraform.Options{
    TerraformDir: "../",
    Vars: map[string]interface{}{
      "environment": "test",
    },
  }

  defer terraform.Destroy(t, terraformOptions)

  terraform.InitAndApply(t, terraformOptions)

  // Verify outputs
  output := terraform.Output(t, terraformOptions, "instance_id")
  if output == "" {
    t.Fatal("Instance ID should not be empty")
  }
}
```

**Compliance testing:**

```hcl
# policy.sentinel (Terraform Cloud policy)
import "tfplan/v2" as tfplan

# Enforce encryption
enforce_encryption = rule {
  all tfplan.resource_changes.aws_s3_bucket as _, buckets {
    all buckets[_].change.after.server_side_encryption_configuration as enc {
      enc.rule[0].apply_server_side_encryption_by_default[0].sse_algorithm == "AES256"
    }
  }
}

main = rule {
  enforce_encryption
}
```

---

### Q25: How do you handle Terraform state file conflicts and recovery?

**Answer:**

**Prevention:**

1. **Remote state with locking:**

```hcl
terraform {
  backend "s3" {
    bucket         = "my-state"
    dynamodb_table = "terraform-locks"
  }
}
```

2. **State versioning:**

```bash
# Enable S3 versioning
aws s3api put-bucket-versioning \
  --bucket my-terraform-state \
  --versioning-configuration Status=Enabled
```

**Detection:**

```bash
# Check lock status
aws dynamodb get-item \
  --table-name terraform-locks \
  --key '{"LockID":{"S":"my-state/terraform.tfstate"}}'
```

**Recovery:**

```bash
# Unlock stuck state
terraform force-unlock LOCK_ID

# Restore from backup
aws s3api get-object \
  --bucket my-terraform-state \
  --key terraform.tfstate.v123 \
  restored-state.tfstate

# Compare states
terraform state pull > current.json
# Edit to resolve conflicts

# Push corrected state
terraform state push corrected-state.json
```

**Best practices:**
- Always use remote state with locking
- Enable versioning on backend storage
- Regular backups
- Document state recovery procedures
- Test recovery procedures periodically
- Restrict access to state files

---

### Q26: What are lifecycle rules and when should you use them?

**Answer:**

Lifecycle meta-arguments control resource creation, updating, and deletion.

```hcl
resource "aws_instance" "web" {
  ami           = "ami-0c55b159cbfafe1f0"
  instance_type = "t2.micro"

  lifecycle {
    # Prevent destruction
    prevent_destroy = true

    # Create replacement before destroying old
    create_before_destroy = true

    # Ignore certain attribute changes
    ignore_changes = [tags["LastModified"]]

    # Ignore all computed attributes
    ignore_changes = all

    # Custom replacement trigger
    replace_triggered_by = [aws_ami.ubuntu.id]
  }
}
```

**When to use:**

```hcl
# Prevent database deletion
resource "aws_db_instance" "main" {
  lifecycle {
    prevent_destroy = true
  }
}

# Zero-downtime updates (create new before destroying old)
resource "aws_launch_template" "app" {
  # ...

  lifecycle {
    create_before_destroy = true
  }
}

# Ignore tagging changes by external systems
resource "aws_ec2_instance_tag" "team" {
  resource_id = aws_instance.web.id
  key         = "Team"
  value       = "platform"

  lifecycle {
    ignore_changes = [value]  # Team can update tag without Terraform
  }
}

# Recreate on dependency change
resource "aws_instance" "app" {
  lifecycle {
    replace_triggered_by = [aws_security_group.app.id]
  }
}
```

---

### Q27: How would you implement a disaster recovery plan with Terraform?

**Answer:**

**1. Backup strategy:**

```bash
# Automated state backups
aws s3 cp s3://production-state/terraform.tfstate \
  s3://production-state-backups/terraform.tfstate.$(date +%Y%m%d-%H%M%S)
```

**2. Documentation as code:**

```hcl
# outputs.tf
output "recovery_runbook" {
  value = <<-EOT
    DISASTER RECOVERY RUNBOOK:
    1. Restore latest state: aws s3 cp s3://...
    2. Run: terraform plan
    3. Review changes carefully
    4. Apply: terraform apply
    5. Verify services: https://status.example.com
  EOT
}
```

**3. Infrastructure as code for recovery:**

```hcl
# recovery.tf
module "restored_vpc" {
  source = "./modules/vpc"

  vpc_cidr  = var.recovered_vpc_cidr
  from_backup = true
}
```

**4. Automated failover:**

```bash
#!/bin/bash
# dr_failover.sh

# Pull latest backup
aws s3 cp s3://backup-bucket/latest-state.tfstate .

# Apply recovered infrastructure
terraform init -reconfigure
terraform apply -var-file="dr-recovery.tfvars" -auto-approve

# Notify stakeholders
aws sns publish --topic-arn arn:aws:sns:... \
  --message "DR failover completed"
```

**5. Regular DR drills:**

```bash
# Schedule monthly DR tests
0 2 * * 1 /path/to/dr_test.sh
```

---

### Q28: How do you optimize Terraform performance for large infrastructure?

**Answer:**

**1. Parallelize operations:**

```bash
# Default parallelism is 10
terraform apply -parallelism=20

# Configure in backend
terraform {
  backend "s3" {
    # ...
  }
}
```

**2. Target specific resources:**

```bash
# Apply only needed changes
terraform apply -target="module.vpc"
terraform apply -target="aws_instance.web[0]"

# Chain targets
terraform apply -target="aws_vpc.main" \
                -target="aws_subnet.public"
```

**3. Refresh sparingly:**

```bash
# Skip refresh (faster)
terraform apply -refresh=false

# Refresh specific resource
terraform refresh -target="aws_instance.web"
```

**4. Use state filtering:**

```bash
# List by type (faster than state show)
terraform state list 'aws_instance.*'

# Remove unused resources
terraform state rm aws_instance.old
```

**5. Optimize provider configuration:**

```hcl
terraform {
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"  # Pin version
    }
  }
}

provider "aws" {
  skip_credentials_validation = true  # Faster validation
  skip_requesting_account_id  = true
}
```

**6. Cache provider plugins:**

```bash
# Initialize with plugin cache
export TF_PLUGIN_CACHE_DIR="~/.terraform.d/plugin-cache"
terraform init
```

**7. Use remote state:**

```hcl
# Faster than local for large states
terraform {
  backend "s3" {
    bucket = "terraform-state"
  }
}
```

---

### Q29: How do you handle Terraform dependency management and ordering?

**Answer:**

**Implicit dependencies** (automatic):

```hcl
resource "aws_vpc" "main" {
  cidr_block = "10.0.0.0/16"
}

resource "aws_security_group" "web" {
  vpc_id = aws_vpc.main.id  # Implicit dependency
}

# Terraform automatically creates VPC first
```

**Explicit dependencies:**

```hcl
resource "aws_vpc" "main" {
  cidr_block = "10.0.0.0/16"
}

resource "aws_route_table" "public" {
  vpc_id = aws_vpc.main.id
}

resource "aws_iam_role" "lambda" {
  # No relationship to VPC, but must be created after
  depends_on = [aws_route_table.public]
}
```

**Dependency graph visualization:**

```bash
terraform graph | dot -Tsvg > dependencies.svg
```

**Module dependencies:**

```hcl
module "vpc" {
  source = "./modules/vpc"
}

module "security_groups" {
  source = "./modules/security_groups"

  vpc_id = module.vpc.vpc_id  # Implicit dependency
}

module "instances" {
  source = "./modules/instances"

  security_groups = module.security_groups.ids
  subnets         = module.vpc.subnet_ids

  depends_on = [module.security_groups]  # Explicit if needed
}
```

**Output dependencies:**

```hcl
# Control order with outputs
output "step1_complete" {
  value = aws_vpc.main.id
}

output "step2_complete" {
  value = aws_subnet.public[*].id
  depends_on = [aws_vpc.main]
}
```

---

### Q30: What are Terraform workspaces and how do you use them effectively?

**Answer:**

Workspaces allow managing multiple environments with the same configuration.

**Basic usage:**

```bash
terraform workspace new dev
terraform workspace new staging
terraform workspace new prod

terraform workspace list
#   default
# * dev
#   prod
#   staging

terraform workspace select prod

terraform workspace show
# prod
```

**State isolation:**

```bash
# Each workspace has separate state
terraform.tfstate.d/
  dev/
    terraform.tfstate
  prod/
    terraform.tfstate
  staging/
    terraform.tfstate
```

**Using workspaces in configuration:**

```hcl
variable "instance_types" {
  type = map(string)
  default = {
    dev     = "t2.micro"
    staging = "t2.small"
    prod    = "t3.large"
  }
}

locals {
  current_workspace = terraform.workspace
  instance_type     = var.instance_types[local.current_workspace]
}

resource "aws_instance" "web" {
  instance_type = local.instance_type

  tags = {
    Environment = terraform.workspace
  }
}

output "workspace" {
  value = terraform.workspace
}
```

**Workspace-specific variables:**

```bash
# dev.tfvars, prod.tfvars
terraform apply -var-file="${terraform.workspace}.tfvars"
```

**When to use workspaces:**
- Similar environments (dev, staging, prod)
- Quick environment switching
- Shared configuration

**Limitations:**
- Single working directory
- All environments in one state (if local)
- Team collaboration complex
- Limited RBAC

**Better alternative: Terraform Cloud workspaces** (different concept):

```hcl
terraform {
  cloud {
    organization = "my-org"

    workspaces {
      name = "prod-us-east-1"
    }
  }
}
```

**Cloud workspaces advantages:**
- True isolation with separate state
- Team-based access control
- Run approvals
- Cost estimation
- Better for production

---

## Quick Reference: Advanced Scenarios

### Managing hundreds of resources efficiently

```hcl
# Use count for simple repetition
resource "aws_instance" "app" {
  count         = 100
  instance_type = "t2.micro"
}

# Use for_each for complex scenarios
locals {
  instances = {for i in range(100) : "app-${i}" => {
    type = "t2.micro"
    az   = element(data.aws_availability_zones.available.names, i % 3)
  }}
}

resource "aws_instance" "app" {
  for_each          = local.instances
  instance_type     = each.value.type
  availability_zone = each.value.az
}
```

### Cost optimization with Terraform

```hcl
locals {
  cost_optimization = {
    dev     = true
    staging = true
    prod    = false
  }
}

resource "aws_instance" "web" {
  # Turn off non-prod instances daily
  instance_type = local.cost_optimization[terraform.workspace] ? "t2.nano" : "t3.large"
}
```

### Blue-green deployment

```hcl
# Blue environment
resource "aws_instance" "blue" {
  count         = var.active_environment == "blue" ? 1 : 0
  instance_type = "t2.micro"

  tags = {
    Environment = "blue"
  }
}

# Green environment
resource "aws_instance" "green" {
  count         = var.active_environment == "green" ? 1 : 0
  instance_type = "t2.micro"

  tags = {
    Environment = "green"
  }
}

# Switch traffic
resource "aws_route53_record" "app" {
  zone_id = aws_route53_zone.main.zone_id
  name    = "app.example.com"
  type    = "A"
  ttl     = 60

  alias {
    name                   = var.active_environment == "blue" ? aws_lb.blue[0].dns_name : aws_lb.green[0].dns_name
    zone_id                = aws_lb.blue[0].zone_id
    evaluate_target_health = true
  }
}
```

---

**Good luck with your Terraform interviews!** Focus on understanding the concepts deeply rather than memorizing syntax. Real-world scenarios and thoughtful architecture discussions are more valuable than perfect answers.
