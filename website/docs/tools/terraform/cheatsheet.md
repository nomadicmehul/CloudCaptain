---
title: Terraform Cheat Sheet
sidebar_label: Cheat Sheet
sidebar_position: 3
---

# Terraform Cheat Sheet

Quick reference for Terraform commands and HCL syntax.

## Core Commands

### Initialization

```bash
# Initialize Terraform working directory
terraform init

# Upgrade provider versions
terraform init -upgrade

# Reconfigure backend
terraform init -reconfigure

# Skip backend configuration
terraform init -backend=false
```

### Planning and Applying

```bash
# Show execution plan
terraform plan

# Save plan to file
terraform plan -out=tfplan

# Apply from saved plan
terraform apply tfplan

# Apply without confirmation prompt
terraform apply -auto-approve

# Apply with variable file
terraform apply -var-file="prod.tfvars"

# Set individual variables
terraform apply -var="key=value"

# Apply only specific resources
terraform apply -target="aws_instance.web"
```

### Destroy

```bash
# Destroy all resources
terraform destroy

# Destroy without confirmation
terraform destroy -auto-approve

# Destroy specific resource
terraform destroy -target="aws_instance.web"

# Destroy multiple resources
terraform destroy -target="aws_security_group.web" -target="aws_instance.app"
```

## State Management

### State Commands

```bash
# Show current state
terraform show

# Show state in JSON format
terraform show -json

# List resources in state
terraform state list

# Show specific resource in state
terraform state show aws_instance.web

# Move resource in state (refactoring)
terraform state mv aws_instance.web aws_instance.server

# Remove resource from state (without destroying)
terraform state rm aws_instance.deprecated

# Replace resource in state
terraform state replace-provider hashicorp/aws aws

# Push state to remote
terraform state push terraform.tfstate

# Pull state from remote
terraform state pull > terraform.tfstate

# Lock state (manual)
terraform state lock

# Unlock state (manual)
terraform state unlock
```

## Workspace Commands

```bash
# List all workspaces
terraform workspace list

# Create new workspace
terraform workspace new staging

# Select workspace
terraform workspace select production

# Show current workspace
terraform workspace show

# Delete workspace
terraform workspace delete staging

# List resources in workspace
terraform state list
```

## Import and Refresh

```bash
# Import existing resource
terraform import aws_instance.example i-1234567890abcdef0

# Import with resource path
terraform import module.vpc.aws_vpc.main vpc-12345678

# Refresh state from real infrastructure
terraform refresh

# Refresh specific resource
terraform refresh -target="aws_instance.web"
```

## Output and Graph

```bash
# Display outputs
terraform output

# Show specific output value
terraform output instance_id

# Output in JSON
terraform output -json

# Get output value (for scripts)
terraform output -raw instance_public_ip

# Generate resource dependency graph
terraform graph

# Generate graph for specific module
terraform graph -type=plan

# Show graph in DOT format (visualize with Graphviz)
terraform graph | dot -Tsvg > graph.svg
```

## Validation and Formatting

```bash
# Validate configuration syntax
terraform validate

# Check for errors without state
terraform validate -json

# Format HCL files (in-place)
terraform fmt

# Format specific file
terraform fmt main.tf

# Format recursively
terraform fmt -recursive .

# Check if formatting needed (dry-run)
terraform fmt -check

# List files that need formatting
terraform fmt -write=false
```

## Testing and Debugging

```bash
# Console (interactive REPL)
terraform console

# Debug logging
TF_LOG=DEBUG terraform apply

# Log to file
TF_LOG_PATH=/tmp/terraform.log terraform plan

# Detailed logging
TF_LOG=TRACE terraform apply

# Disable input prompts (CI/CD)
terraform apply -input=false

# Show detailed error trace
TF_LOG=DEBUG terraform apply
```

## Provider and Module

```bash
# Show provider requirements
terraform providers

# Show provider schema
terraform providers schema -json

# Lock provider versions
terraform init

# Download provider plugins
terraform init

# Get and update modules
terraform get

# Update modules to latest versions
terraform get -update

# Show module tree
terraform providers graph
```

## Meta-Commands

```bash
# Show help
terraform help

# Show command help
terraform help apply

# Show version
terraform version

# Show configuration
terraform show

# Show execution plan as JSON
terraform plan -json

# Show all terraform files
terraform files
```

## HCL Syntax Quick Reference

### Variables

```hcl
# String variable
variable "region" {
  type        = string
  description = "AWS region"
  default     = "us-east-1"
  sensitive   = false
}

# Number variable
variable "port" {
  type    = number
  default = 8080
}

# Boolean variable
variable "enabled" {
  type    = bool
  default = true
}

# List variable
variable "azs" {
  type    = list(string)
  default = ["us-east-1a", "us-east-1b"]
}

# Map variable
variable "tags" {
  type = map(string)
  default = {
    Environment = "prod"
    Team        = "platform"
  }
}

# Object variable
variable "instance_config" {
  type = object({
    instance_type = string
    ebs_optimized = bool
    volume_size   = number
  })
}

# Variable validation
variable "environment" {
  type = string
  validation {
    condition     = contains(["dev", "staging", "prod"], var.environment)
    error_message = "Invalid environment."
  }
}
```

### Outputs

```hcl
# Basic output
output "instance_id" {
  value       = aws_instance.web.id
  description = "Instance ID"
}

# Sensitive output (not displayed in CLI)
output "db_password" {
  value     = aws_db_instance.main.password
  sensitive = true
}

# Computed output
output "public_ips" {
  value = [for instance in aws_instance.web : instance.public_ip]
}

# Conditional output
output "load_balancer_dns" {
  value = var.enable_lb ? aws_lb.main[0].dns_name : null
}
```

### Locals

```hcl
locals {
  # Simple value
  region = "us-east-1"

  # Computed value
  environment_short = substr(var.environment, 0, 3)

  # Map merge
  common_tags = {
    Environment = var.environment
    Project     = "CloudCaptain"
  }

  # Conditional
  instance_count = var.environment == "prod" ? 5 : 1

  # Name prefix
  name_prefix = "${var.environment}-${var.region}"
}
```

### Data Sources

```hcl
# Get latest AMI
data "aws_ami" "ubuntu" {
  most_recent = true
  owners      = ["099720109477"]

  filter {
    name   = "name"
    values = ["ubuntu/images/hvm-ssd/ubuntu-jammy-22.04-*"]
  }
}

# Get availability zones
data "aws_availability_zones" "available" {
  state = "available"
}

# Get VPC by tag
data "aws_vpc" "selected" {
  filter {
    name   = "tag:Name"
    values = ["main"]
  }
}

# Reference data source
resource "aws_instance" "web" {
  ami = data.aws_ami.ubuntu.id
}
```

### Resource Syntax

```hcl
# Basic resource
resource "aws_instance" "web" {
  ami           = "ami-0c55b159cbfafe1f0"
  instance_type = "t2.micro"
}

# Resource with explicit provider
resource "aws_s3_bucket" "west" {
  provider = aws.us-west-2
  bucket   = "my-bucket-west"
}

# Conditional resource creation
resource "aws_rds_cluster" "main" {
  count = var.create_rds ? 1 : 0
  # ...
}

# Multiple resources with for_each
resource "aws_instance" "app" {
  for_each      = var.instances
  instance_type = each.value.type
  tags = {
    Name = each.key
  }
}

# Multiple resources with count
resource "aws_instance" "server" {
  count         = var.instance_count
  instance_type = "t2.micro"
  tags = {
    Name = "server-${count.index + 1}"
  }
}
```

### Functions

```hcl
# String functions
length("hello")                    # 5
substr("hello", 1, 3)              # "ell"
upper("hello")                     # "HELLO"
lower("HELLO")                     # "hello"
split(",", "a,b,c")               # ["a", "b", "c"]
join(",", ["a", "b", "c"])        # "a,b,c"
replace("hello", "l", "L")         # "heLLo"
contains("hello", "ell")           # true

# Number functions
min(1, 2, 3)                       # 1
max(1, 2, 3)                       # 3
ceil(4.3)                          # 5
floor(4.9)                         # 4

# Collection functions
concat([1, 2], [3, 4])             # [1, 2, 3, 4]
merge(map1, map2)                  # merged map
flatten([[1], [2, 3]])             # [1, 2, 3]
keys({"a": 1, "b": 2})             # ["a", "b"]
values({"a": 1, "b": 2})           # [1, 2]
lookup(map, key, default)          # value

# Type conversion
tostring(123)                      # "123"
tonumber("123")                    # 123
tolist(var.azs)                    # list
tomap(var.tags)                    # map

# File functions
file("path/to/file")               # file contents
filebase64("path/to/file")         # base64 encoded file
templatefile("path", vars)         # render template

# Date functions
timestamp()                        # current timestamp
formatdate("YYYY-MM-DD", time)    # formatted date

# Conditionals and logic
var.enabled ? "yes" : "no"        # ternary
try(expr, default)                 # try-catch

# For expressions
[for i in range(3) : i * 2]       # [0, 2, 4]
{for k, v in map : k => upper(v)} # transformed map
```

### Interpolation and Splat

```hcl
# String interpolation
"Instance: ${aws_instance.web.id}"

# Resource reference
aws_instance.web.public_ip

# Attribute reference
aws_instance.web.vpc_security_group_ids

# Splat (all instances)
aws_instance.web[*].id            # [id1, id2, id3]
aws_instance.web[*].public_ip      # [ip1, ip2, ip3]

# Map values
{for k, v in aws_instance.app : k => v.id}

# Conditional expression
var.enable_monitoring ? aws_cloudwatch_alarm.high_cpu[0].id : null
```

### Provisioners

```hcl
# Local execution
provisioner "local-exec" {
  command = "echo 'Instance created' > instance.txt"
}

# Remote execution (SSH)
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
  source      = "local/path"
  destination = "/tmp/path"

  connection {
    type        = "ssh"
    user        = "ubuntu"
    private_key = file("~/.ssh/id_rsa")
    host        = self.public_ip
  }
}

# On failure behavior
provisioner "remote-exec" {
  on_failure = continue
  inline     = ["echo 'Continuing despite error'"]
}
```

### Dynamic Blocks

```hcl
# Dynamic ingress rules
dynamic "ingress" {
  for_each = var.ingress_rules

  content {
    from_port   = ingress.value.from_port
    to_port     = ingress.value.to_port
    protocol    = ingress.value.protocol
    cidr_blocks = ingress.value.cidr_blocks
  }
}

# Dynamic labels
dynamic "tag" {
  for_each = var.tags

  content {
    key                 = tag.key
    value               = tag.value
    propagate_at_launch = true
  }
}
```

### Lifecycle

```hcl
lifecycle {
  # Prevent destruction
  prevent_destroy = true

  # Create before destroying
  create_before_destroy = true

  # Ignore certain attribute changes
  ignore_changes = [tags, root_block_device[0].volume_size]

  # Ignore all computed attributes
  ignore_changes = all

  # Replace when trigger changes
  replace_triggered_by = [aws_security_group.app.id]
}
```

### Backend Configuration

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

terraform {
  backend "remote" {
    organization = "my-org"

    workspaces {
      name = "production"
    }
  }
}
```

## Common Patterns

### Count-based Scaling

```hcl
resource "aws_instance" "app" {
  count         = var.instance_count
  instance_type = "t2.micro"

  tags = {
    Name = "app-${count.index + 1}"
  }
}

output "instance_ids" {
  value = aws_instance.app[*].id
}
```

### Conditional Resource Creation

```hcl
resource "aws_db_instance" "main" {
  count             = var.create_database ? 1 : 0
  allocated_storage = 100
  engine            = "postgres"
}

# Reference conditionally created resource
resource "aws_security_group" "db" {
  count  = var.create_database ? 1 : 0
  vpc_id = aws_vpc.main.id
}
```

### Module Composition

```hcl
module "vpc" {
  source = "./modules/vpc"
  cidr   = var.vpc_cidr
}

module "app" {
  source     = "./modules/app"
  vpc_id     = module.vpc.vpc_id
  subnet_ids = module.vpc.subnet_ids
}
```

### Environment-specific Configuration

```hcl
locals {
  env_config = {
    dev = {
      instance_type = "t2.micro"
      instance_count = 1
    }
    prod = {
      instance_type = "t3.large"
      instance_count = 5
    }
  }
}

resource "aws_instance" "app" {
  count         = local.env_config[var.environment].instance_count
  instance_type = local.env_config[var.environment].instance_type
}
```

### Multi-AZ Deployment

```hcl
data "aws_availability_zones" "available" {
  state = "available"
}

resource "aws_instance" "app" {
  for_each              = toset(data.aws_availability_zones.available.names)
  availability_zone     = each.value
  instance_type         = "t2.micro"

  tags = {
    Name = "app-${each.value}"
  }
}
```

## Useful One-Liners

```bash
# Check plan changes only (no resources)
terraform plan -json | jq '.resource_changes[] | select(.change.actions != ["no-op"])'

# Count resources in state
terraform state list | wc -l

# List resource types in state
terraform state list | cut -d. -f1 | sort | uniq

# Export state to JSON
terraform show -json > state.json

# Get all outputs as JSON
terraform output -json > outputs.json

# Find resource by tag
terraform state list | grep -i "tag_name"

# Destroy specific resource type
terraform destroy -target='aws_instance.*' -auto-approve

# Import multiple resources from file
cat resources.txt | xargs -I {} terraform import {}

# Check for unused variables
terraform plan 2>&1 | grep "unused"

# Validate all tf files
find . -name "*.tf" -exec terraform validate {} \;

# Format all files in directory
terraform fmt -recursive .
```

---

**Pro Tips:**
- Use `terraform plan -out=tfplan` to review and persist plans
- Always validate with `terraform validate` before committing
- Use `-target` carefully; only for specific scenarios
- Keep state backed up and versioned in S3
- Use `workspace` for environment separation, not `branches`
- Enable cost estimation in Terraform Cloud for production
