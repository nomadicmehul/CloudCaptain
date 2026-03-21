---
title: "AWS CLI & Services Cheat Sheet"
sidebar_label: "Cheat Sheet"
description: "Complete AWS CLI command reference and services quick-reference for EC2, S3, IAM, VPC, Lambda, and more"
sidebar_position: 7
---

# AWS CLI & Services Cheat Sheet

Quick reference for AWS CLI commands and services. This cheat sheet covers the most common operations for developers and DevOps engineers working with AWS.

## AWS CLI Setup

### Installation

```bash
# macOS with Homebrew
brew install awscli

# Linux/WSL with pip
pip install awscliv2

# Windows with MSI installer
# Download from https://aws.amazon.com/cli/
```

### Initial Configuration

```bash
# Interactive configuration wizard
aws configure

# Configure named profile
aws configure --profile production

# Set environment variables instead
export AWS_ACCESS_KEY_ID=your-access-key
export AWS_SECRET_ACCESS_KEY=your-secret-key
export AWS_DEFAULT_REGION=us-east-1
```

### Output Formats

```bash
# JSON (default, most detailed)
aws ec2 describe-instances --output json

# Table format (human-readable)
aws ec2 describe-instances --output table

# Text format (tab-delimited)
aws ec2 describe-instances --output text

# Query with JMESPath to filter results
aws ec2 describe-instances --query 'Reservations[0].Instances[0].InstanceId' --output text
```

### Working with Profiles

```bash
# Use specific profile
aws s3 ls --profile production

# Set default profile
export AWS_PROFILE=production

# List configured profiles
cat ~/.aws/credentials

# Get current identity
aws sts get-caller-identity --profile dev
```

---

## EC2 Commands

### Instance Lifecycle

```bash
# List all running instances
aws ec2 describe-instances --filters "Name=instance-state-name,Values=running"

# List instances with details
aws ec2 describe-instances --query 'Reservations[*].Instances[*].[InstanceId,InstanceType,State.Name,PublicIpAddress]' --output table

# Launch new instance
aws ec2 run-instances --image-id ami-0c55b159cbfafe1f0 --instance-type t2.micro --key-name my-key-pair --security-groups default

# Launch with VPC and subnet
aws ec2 run-instances --image-id ami-12345 --instance-type t2.medium --subnet-id subnet-abc123 --security-group-ids sg-12345

# Start stopped instance
aws ec2 start-instances --instance-ids i-1234567890abcdef0

# Stop instance (keeps EBS volumes)
aws ec2 stop-instances --instance-ids i-1234567890abcdef0

# Terminate instance (deletes EBS volumes)
aws ec2 terminate-instances --instance-ids i-1234567890abcdef0

# Reboot instance
aws ec2 reboot-instances --instance-ids i-1234567890abcdef0
```

### Key Pairs

```bash
# Create key pair
aws ec2 create-key-pair --key-name my-new-key --query 'KeyMaterial' --output text > my-new-key.pem

# List key pairs
aws ec2 describe-key-pairs

# Delete key pair
aws ec2 delete-key-pair --key-name my-old-key

# Import existing public key
aws ec2 import-key-pair --key-name imported-key --public-key-material file://my-key.pub
```

### Security Groups

```bash
# List security groups
aws ec2 describe-security-groups

# Create security group
aws ec2 create-security-group --group-name my-sg --description "My security group" --vpc-id vpc-12345

# Authorize inbound rule (SSH)
aws ec2 authorize-security-group-ingress --group-id sg-12345 --protocol tcp --port 22 --cidr 0.0.0.0/0

# Authorize inbound rule (HTTP)
aws ec2 authorize-security-group-ingress --group-id sg-12345 --protocol tcp --port 80 --cidr 0.0.0.0/0

# Authorize inbound rule (HTTPS)
aws ec2 authorize-security-group-ingress --group-id sg-12345 --protocol tcp --port 443 --cidr 0.0.0.0/0

# Revoke inbound rule
aws ec2 revoke-security-group-ingress --group-id sg-12345 --protocol tcp --port 22 --cidr 0.0.0.0/0

# Authorize security group to allow another security group
aws ec2 authorize-security-group-ingress --group-id sg-12345 --protocol tcp --port 3306 --source-group sg-67890

# Delete security group
aws ec2 delete-security-group --group-id sg-12345
```

### EBS Volumes

```bash
# List EBS volumes
aws ec2 describe-volumes

# Create EBS volume
aws ec2 create-volume --availability-zone us-east-1a --size 100

# Attach volume to instance
aws ec2 attach-volume --volume-id vol-12345 --instance-id i-1234567890abcdef0 --device /dev/sdf

# Detach volume
aws ec2 detach-volume --volume-id vol-12345

# Create snapshot from volume
aws ec2 create-snapshot --volume-id vol-12345 --description "Volume backup"

# Describe snapshots
aws ec2 describe-snapshots --owner-ids self

# Delete snapshot
aws ec2 delete-snapshot --snapshot-id snap-12345

# Create volume from snapshot
aws ec2 create-volume --snapshot-id snap-12345 --availability-zone us-east-1a
```

### Elastic IPs

```bash
# Allocate elastic IP
aws ec2 allocate-address --domain vpc

# Associate elastic IP with instance
aws ec2 associate-address --instance-id i-1234567890abcdef0 --allocation-id eipalloc-12345

# Describe elastic IPs
aws ec2 describe-addresses

# Release elastic IP
aws ec2 release-address --allocation-id eipalloc-12345
```

---

## S3 Commands

### Bucket Operations

```bash
# List all buckets
aws s3 ls

# Create bucket (us-east-1)
aws s3 mb s3://my-bucket

# Create bucket in specific region
aws s3 mb s3://my-bucket --region us-west-2

# Remove empty bucket
aws s3 rb s3://my-bucket

# Force remove bucket (deletes all contents)
aws s3 rb s3://my-bucket --force

# Get bucket location
aws s3api get-bucket-location --bucket my-bucket

# List bucket contents with sizes
aws s3 ls s3://my-bucket --recursive --summarize --human-readable

# Get bucket size in GB
aws s3 ls s3://my-bucket --recursive --summarize --human-readable | grep "Total Size"
```

### File Operations

```bash
# Copy file to S3
aws s3 cp myfile.txt s3://my-bucket/myfile.txt

# Copy file from S3
aws s3 cp s3://my-bucket/myfile.txt myfile.txt

# Copy with metadata
aws s3 cp myfile.txt s3://my-bucket/ --metadata "key1=value1,key2=value2"

# Sync local directory to S3
aws s3 sync ./local-folder s3://my-bucket/remote-folder

# Sync S3 to local directory
aws s3 sync s3://my-bucket/remote-folder ./local-folder

# Sync with deletion (removes files in destination not in source)
aws s3 sync ./local-folder s3://my-bucket --delete

# Sync excluding certain files
aws s3 sync ./local-folder s3://my-bucket --exclude "*.tmp" --exclude "*.log"

# List files in S3 path
aws s3 ls s3://my-bucket/prefix/

# Copy between buckets
aws s3 cp s3://source-bucket/file.txt s3://dest-bucket/file.txt

# Move file (copy then delete)
aws s3 mv s3://source-bucket/file.txt s3://dest-bucket/file.txt

# Delete file from S3
aws s3 rm s3://my-bucket/myfile.txt

# Delete all files with prefix
aws s3 rm s3://my-bucket/prefix --recursive
```

### S3 API Operations

```bash
# Get object (with all metadata)
aws s3api get-object --bucket my-bucket --key myfile.txt myfile.txt

# Put object with ACL
aws s3api put-object --bucket my-bucket --key myfile.txt --body myfile.txt --acl public-read

# Get object ACL
aws s3api get-object-acl --bucket my-bucket --key myfile.txt

# List objects with pagination
aws s3api list-objects-v2 --bucket my-bucket --max-items 10

# Get object tagging
aws s3api get-object-tagging --bucket my-bucket --key myfile.txt

# Put object tagging
aws s3api put-object-tagging --bucket my-bucket --key myfile.txt --tagging 'TagSet=[{Key=Environment,Value=Production}]'

# Get bucket versioning status
aws s3api get-bucket-versioning --bucket my-bucket

# Enable versioning
aws s3api put-bucket-versioning --bucket my-bucket --versioning-configuration Status=Enabled

# Get bucket lifecycle configuration
aws s3api get-bucket-lifecycle-configuration --bucket my-bucket

# Delete object with version ID
aws s3api delete-object --bucket my-bucket --key myfile.txt --version-id abc123
```

---

## IAM Commands

### Users

```bash
# Create user
aws iam create-user --user-name john.doe

# List all users
aws iam list-users

# Get user details
aws iam get-user --user-name john.doe

# Delete user
aws iam delete-user --user-name john.doe

# Create access key for user
aws iam create-access-key --user-name john.doe

# List access keys for user
aws iam list-access-keys --user-name john.doe

# Delete access key
aws iam delete-access-key --user-name john.doe --access-key-id AKIAIOSFODNN7EXAMPLE

# Create login profile (password for console access)
aws iam create-login-profile --user-name john.doe --password TempPassword123!

# Update user
aws iam update-user --user-name john.doe --new-user-name jane.doe
```

### Groups

```bash
# Create group
aws iam create-group --group-name developers

# List groups
aws iam list-groups

# Add user to group
aws iam add-user-to-group --group-name developers --user-name john.doe

# List users in group
aws iam get-group --group-name developers

# Remove user from group
aws iam remove-user-from-group --group-name developers --user-name john.doe

# Delete group
aws iam delete-group --group-name developers
```

### Roles

```bash
# Create role (with trust policy)
aws iam create-role --role-name ec2-app-role --assume-role-policy-document file://trust-policy.json

# List roles
aws iam list-roles

# Get role details
aws iam get-role --role-name ec2-app-role

# Attach policy to role
aws iam attach-role-policy --role-name ec2-app-role --policy-arn arn:aws:iam::aws:policy/AmazonEC2FullAccess

# List attached policies for role
aws iam list-attached-role-policies --role-name ec2-app-role

# Detach policy from role
aws iam detach-role-policy --role-name ec2-app-role --policy-arn arn:aws:iam::aws:policy/AmazonEC2FullAccess

# Delete role
aws iam delete-role --role-name ec2-app-role
```

### Policies

```bash
# List all managed policies
aws iam list-policies

# Get policy details
aws iam get-policy --policy-arn arn:aws:iam::aws:policy/AmazonEC2FullAccess

# Create custom policy
aws iam create-policy --policy-name my-policy --policy-document file://policy.json

# Get policy version
aws iam get-policy-version --policy-arn arn:aws:iam::123456789012:policy/my-policy --version-id v1

# List policy versions
aws iam list-policy-versions --policy-arn arn:aws:iam::123456789012:policy/my-policy

# Delete policy
aws iam delete-policy --policy-arn arn:aws:iam::123456789012:policy/my-policy
```

---

## VPC Commands

### VPC Management

```bash
# Create VPC
aws ec2 create-vpc --cidr-block 10.0.0.0/16

# List VPCs
aws ec2 describe-vpcs

# Get VPC details
aws ec2 describe-vpcs --vpc-ids vpc-12345 --query 'Vpcs[0]'

# Delete VPC
aws ec2 delete-vpc --vpc-id vpc-12345

# Enable DNS hostnames in VPC
aws ec2 modify-vpc-attribute --vpc-id vpc-12345 --enable-dns-hostnames

# Enable DNS support in VPC
aws ec2 modify-vpc-attribute --vpc-id vpc-12345 --enable-dns-support
```

### Subnets

```bash
# Create subnet
aws ec2 create-subnet --vpc-id vpc-12345 --cidr-block 10.0.1.0/24 --availability-zone us-east-1a

# List subnets
aws ec2 describe-subnets

# Get subnet details
aws ec2 describe-subnets --subnet-ids subnet-12345

# Delete subnet
aws ec2 delete-subnet --subnet-id subnet-12345

# Make subnet public by enabling auto-assign public IP
aws ec2 modify-subnet-attribute --subnet-id subnet-12345 --map-public-ip-on-launch
```

### Internet Gateway

```bash
# Create internet gateway
aws ec2 create-internet-gateway

# Attach IGW to VPC
aws ec2 attach-internet-gateway --internet-gateway-id igw-12345 --vpc-id vpc-12345

# List internet gateways
aws ec2 describe-internet-gateways

# Detach IGW from VPC
aws ec2 detach-internet-gateway --internet-gateway-id igw-12345 --vpc-id vpc-12345

# Delete internet gateway
aws ec2 delete-internet-gateway --internet-gateway-id igw-12345
```

### Route Tables

```bash
# Create route table
aws ec2 create-route-table --vpc-id vpc-12345

# List route tables
aws ec2 describe-route-tables

# Associate route table with subnet
aws ec2 associate-route-table --route-table-id rtb-12345 --subnet-id subnet-12345

# Add route to internet gateway
aws ec2 create-route --route-table-id rtb-12345 --destination-cidr-block 0.0.0.0/0 --gateway-id igw-12345

# List routes in route table
aws ec2 describe-route-tables --route-table-ids rtb-12345

# Delete route
aws ec2 delete-route --route-table-id rtb-12345 --destination-cidr-block 0.0.0.0/0
```

### VPC Peering

```bash
# Create VPC peering connection
aws ec2 create-vpc-peering-connection --vpc-id vpc-12345 --peer-vpc-id vpc-67890

# Accept VPC peering connection
aws ec2 accept-vpc-peering-connection --vpc-peering-connection-id pcx-12345

# List VPC peering connections
aws ec2 describe-vpc-peering-connections

# Delete VPC peering connection
aws ec2 delete-vpc-peering-connection --vpc-peering-connection-id pcx-12345
```

### NAT Gateway

```bash
# Create NAT gateway (needs elastic IP)
aws ec2 create-nat-gateway --subnet-id subnet-12345 --allocation-id eipalloc-12345

# List NAT gateways
aws ec2 describe-nat-gateways

# Get NAT gateway details
aws ec2 describe-nat-gateways --nat-gateway-ids natgw-12345

# Delete NAT gateway
aws ec2 delete-nat-gateway --nat-gateway-id natgw-12345
```

---

## Lambda Commands

### Function Management

```bash
# Create function from ZIP file
aws lambda create-function --function-name my-function --runtime python3.9 --role arn:aws:iam::123456789012:role/lambda-role --handler lambda_function.lambda_handler --zip-file fileb://function.zip

# Create function from S3
aws lambda create-function --function-name my-function --runtime python3.9 --role arn:aws:iam::123456789012:role/lambda-role --handler lambda_function.lambda_handler --code S3Bucket=my-bucket,S3Key=function.zip

# List functions
aws lambda list-functions

# Get function details
aws lambda get-function --function-name my-function

# Update function code from ZIP
aws lambda update-function-code --function-name my-function --zip-file fileb://function.zip

# Update function configuration
aws lambda update-function-configuration --function-name my-function --timeout 60 --memory-size 512

# Delete function
aws lambda delete-function --function-name my-function
```

### Function Invocation

```bash
# Synchronous invocation
aws lambda invoke --function-name my-function --payload '{"key":"value"}' response.json

# Asynchronous invocation
aws lambda invoke --function-name my-function --invocation-type Event --payload '{"key":"value"}' response.json

# View invocation response
cat response.json

# Get function logs (CloudWatch)
aws logs tail /aws/lambda/my-function --follow
```

### Function Configuration

```bash
# Set environment variables
aws lambda update-function-configuration --function-name my-function --environment Variables="{KEY1=value1,KEY2=value2}"

# Add function permissions
aws lambda add-permission --function-name my-function --statement-id AllowS3 --action lambda:InvokeFunction --principal s3.amazonaws.com

# Remove permission
aws lambda remove-permission --function-name my-function --statement-id AllowS3

# Get policy
aws lambda get-policy --function-name my-function
```

### Layers

```bash
# Publish layer version
aws lambda publish-layer-version --layer-name my-layer --description "My dependencies" --zip-file fileb://layer.zip --compatible-runtimes python3.9

# List layer versions
aws lambda list-layer-versions --layer-name my-layer

# Delete layer version
aws lambda delete-layer-version --layer-name my-layer --version-number 1
```

---

## CloudFormation Commands

### Stack Lifecycle

```bash
# Create stack from template file
aws cloudformation create-stack --stack-name my-stack --template-body file://template.yaml

# Create stack from S3
aws cloudformation create-stack --stack-name my-stack --template-url https://s3.amazonaws.com/my-bucket/template.yaml

# List stacks
aws cloudformation list-stacks --stack-status-filter CREATE_COMPLETE UPDATE_COMPLETE

# Describe stack
aws cloudformation describe-stacks --stack-name my-stack

# Update stack
aws cloudformation update-stack --stack-name my-stack --template-body file://template.yaml

# Delete stack
aws cloudformation delete-stack --stack-name my-stack

# Wait for stack creation
aws cloudformation wait stack-create-complete --stack-name my-stack

# Wait for stack deletion
aws cloudformation wait stack-delete-complete --stack-name my-stack
```

### Stack Events and Resources

```bash
# Describe stack events
aws cloudformation describe-stack-events --stack-name my-stack

# List stack resources
aws cloudformation list-stack-resources --stack-name my-stack

# Describe stack resource
aws cloudformation describe-stack-resource --stack-name my-stack --logical-resource-id MyResource

# Get template for stack
aws cloudformation get-template --stack-name my-stack

# Validate template
aws cloudformation validate-template --template-body file://template.yaml

# Detect stack drift
aws cloudformation detect-stack-drift --stack-name my-stack

# Get drift information
aws cloudformation describe-stack-drift-detection-status --stack-drift-detection-id drift-id
```

---

## ECS/EKS Commands

### ECS Clusters

```bash
# Create cluster
aws ecs create-cluster --cluster-name my-cluster

# List clusters
aws ecs list-clusters

# Describe cluster
aws ecs describe-clusters --clusters my-cluster

# Delete cluster
aws ecs delete-cluster --cluster my-cluster
```

### ECS Task Definitions

```bash
# Register task definition
aws ecs register-task-definition --cli-input-json file://task-definition.json

# List task definitions
aws ecs list-task-definitions

# Describe task definition
aws ecs describe-task-definition --task-definition my-task:1

# Deregister task definition
aws ecs deregister-task-definition --task-definition my-task:1
```

### ECS Services

```bash
# Create service
aws ecs create-service --cluster my-cluster --service-name my-service --task-definition my-task --desired-count 1

# List services
aws ecs list-services --cluster my-cluster

# Describe service
aws ecs describe-services --cluster my-cluster --services my-service

# Update service
aws ecs update-service --cluster my-cluster --service my-service --desired-count 3

# Delete service
aws ecs delete-service --cluster my-cluster --service my-service --force
```

### EKS Clusters

```bash
# Create cluster
aws eks create-cluster --name my-cluster --version 1.24 --role-arn arn:aws:iam::123456789012:role/eks-role --resources-vpc-config subnetIds=subnet-12345,subnet-67890

# List clusters
aws eks list-clusters

# Describe cluster
aws eks describe-cluster --name my-cluster

# Create node group
aws eks create-nodegroup --cluster-name my-cluster --nodegroup-name my-nodes --subnets subnet-12345 --role-arn arn:aws:iam::123456789012:role/NodeInstanceRole

# List node groups
aws eks list-nodegroups --cluster-name my-cluster

# Delete cluster
aws eks delete-cluster --name my-cluster
```

---

## Useful One-Liners

### EC2 Operations

```bash
# Find all running instances and their IPs
aws ec2 describe-instances --filters "Name=instance-state-name,Values=running" --query 'Reservations[*].Instances[*].[InstanceId,PrivateIpAddress,PublicIpAddress]' --output table

# Terminate all stopped instances (DANGEROUS!)
aws ec2 describe-instances --filters "Name=instance-state-name,Values=stopped" --query 'Reservations[*].Instances[*].InstanceId' --output text | xargs -n1 aws ec2 terminate-instances --instance-ids

# Find instances with specific tag
aws ec2 describe-instances --filters "Name=tag:Environment,Values=production" --query 'Reservations[*].Instances[*].[InstanceId,Tags[?Key==`Name`].Value|[0]]' --output table

# Stop all instances with specific tag
aws ec2 describe-instances --filters "Name=tag:Environment,Values=dev" --query 'Reservations[*].Instances[*].InstanceId' --output text | xargs -n1 aws ec2 stop-instances --instance-ids

# Get instance type and CPU count
aws ec2 describe-instances --query 'Reservations[*].Instances[*].[InstanceId,InstanceType]' --output table

# Find unattached EBS volumes
aws ec2 describe-volumes --filters "Name=status,Values=available" --query 'Volumes[*].[VolumeId,Size,State]' --output table
```

### S3 Operations

```bash
# List all S3 buckets and their sizes
aws s3 ls --recursive | awk '{sum+=$3} END {print "Total size:", sum/1024/1024/1024, "GB"}'

# Find largest objects in bucket
aws s3api list-objects-v2 --bucket my-bucket --query 'Contents[].[Key,Size]' --output text | sort -k2 -rn | head -20

# Calculate bucket size in MB
aws s3 ls s3://my-bucket --recursive --summarize | grep "Total Size"

# Find all public objects in bucket
aws s3api list-objects-v2 --bucket my-bucket --query 'Contents[*].Key' --output text | while read key; do aws s3api head-object --bucket my-bucket --key "$key" 2>/dev/null | grep -q "PublicRead" && echo "$key"; done

# Delete all objects older than 30 days
aws s3api list-objects-v2 --bucket my-bucket --query 'Contents[].[Key,LastModified]' --output text | awk -v date="$(date -d '30 days ago' '+%Y-%m-%d')" '$2 < date {print $1}' | xargs -I {} aws s3 rm s3://my-bucket/{}

# Sync with checksum validation
aws s3 sync s3://source-bucket . --sse AES256 --metadata-directive COPY
```

### IAM Operations

```bash
# List all users with access keys
aws iam list-users --query 'Users[*].UserName' --output text | while read user; do echo "=== $user ==="; aws iam list-access-keys --user-name "$user"; done

# Find unused access keys (no activity in 90 days)
aws iam get-credential-report && aws iam get-credential-report --query 'Content' | base64 -d | awk -F',' '$4=="" || $5!="N/A"' | tail -n +2 | while read line; do echo "$line" | awk -F',' '{print $1, $2}'; done

# List all inline policies for a user
aws iam list-user-policies --user-name john.doe --query 'PolicyNames' --output text

# Get policy document for user
aws iam get-user-policy --user-name john.doe --policy-name policy-name --query 'UserPolicyDocument'
```

### CloudWatch Logs

```bash
# Stream logs from Lambda function
aws logs tail /aws/lambda/my-function --follow

# Get logs from specific time range
aws logs filter-log-events --log-group-name /aws/lambda/my-function --start-time 1609459200000 --end-time 1609545600000

# Count errors in logs
aws logs filter-log-events --log-group-name /aws/lambda/my-function --filter-pattern "ERROR" --query 'events | length(@)'

# Export logs to S3
aws logs create-export-task --log-group-name /aws/lambda/my-function --from 1609459200000 --to 1609545600000 --destination my-bucket --destination-prefix logs/
```

---

## AWS Services Quick Reference

Comprehensive table of 50+ AWS services with categories and descriptions.

| Category | Service | Description |
|----------|---------|-------------|
| **Compute** | EC2 | Virtual machines in the cloud with flexible sizing and configuration options |
| **Compute** | Lambda | Serverless compute for event-driven workloads without managing infrastructure |
| **Compute** | ECS | Managed container orchestration service supporting Docker and Fargate |
| **Compute** | EKS | Managed Kubernetes service for container orchestration at scale |
| **Compute** | Elastic Beanstalk | PaaS for deploying web apps without infrastructure management |
| **Compute** | Lightsail | Simple, low-cost virtual private servers for small applications |
| **Storage** | S3 | Scalable object storage service for files, backups, and data lakes |
| **Storage** | EBS | Block storage volumes for EC2 instances with snapshot capabilities |
| **Storage** | EFS | Managed NFS file system for shared storage across instances |
| **Storage** | Glacier | Low-cost archive storage for long-term data retention and compliance |
| **Storage** | FSx | Managed file storage for Windows and Lustre workloads |
| **Storage** | Backup | Centralized, policy-based backup service for AWS resources |
| **Database** | RDS | Managed relational database with MySQL, PostgreSQL, MariaDB, Oracle, SQL Server |
| **Database** | DynamoDB | Fully managed NoSQL database for low-latency applications |
| **Database** | ElastiCache | In-memory data store for caching and sessions using Redis or Memcached |
| **Database** | Redshift | Data warehouse for analytics on large datasets with SQL queries |
| **Database** | DocumentDB | MongoDB-compatible managed NoSQL database for document workloads |
| **Database** | Neptune | Managed graph database for relationship and recommendation queries |
| **Database** | Timestream | Time-series database optimized for metrics and monitoring data |
| **Networking** | VPC | Virtual private cloud with subnets, routing, and network isolation |
| **Networking** | CloudFront | Content delivery network caching content at edge locations globally |
| **Networking** | Route 53 | DNS service for routing traffic and health checking endpoints |
| **Networking** | ELB | Classic load balancer for distributing traffic to EC2 instances |
| **Networking** | ALB | Application load balancer for layer 7 routing by hostname or path |
| **Networking** | NLB | Network load balancer for extreme performance and low latency |
| **Networking** | VPN | Virtual private network connecting on-premises networks to AWS |
| **Networking** | Direct Connect | Dedicated network connection from your datacenter to AWS |
| **Networking** | AppMesh | Service mesh for microservices communication and traffic management |
| **Security** | IAM | Identity and access management for users, groups, roles, and policies |
| **Security** | Cognito | Authentication and authorization service for web and mobile apps |
| **Security** | Secrets Manager | Service for storing and rotating secrets like passwords and API keys |
| **Security** | KMS | Key management service for encryption keys and data protection |
| **Security** | ACM | Certificate manager for SSL/TLS certificates used by AWS services |
| **Security** | GuardDuty | Threat detection service monitoring accounts for suspicious activity |
| **Security** | Security Hub | Centralized security findings dashboard for compliance and threats |
| **Security** | WAF | Web application firewall protecting applications from web attacks |
| **Security** | Shield | DDoS protection service with Standard (free) and Advanced (paid) tiers |
| **Management** | CloudFormation | Infrastructure-as-code for defining and deploying AWS resources |
| **Management** | CloudWatch | Monitoring service for metrics, logs, and alarms across resources |
| **Management** | CloudTrail | Audit logging service tracking API calls and resource changes |
| **Management** | Config | Configuration management service auditing resource compliance |
| **Management** | Systems Manager | Operations service for patching, automation, and compliance |
| **Management** | OpsWorks | Automation with Chef and Puppet for infrastructure management |
| **Developer Tools** | CodeCommit | Managed Git repository service for version control |
| **Developer Tools** | CodeBuild | Fully managed build service for compiling and testing code |
| **Developer Tools** | CodeDeploy | Service for automating application deployments to instances |
| **Developer Tools** | CodePipeline | CI/CD service orchestrating build, test, and deployment stages |
| **Developer Tools** | CodeStar | Unified interface for developing, building, and deploying apps |
| **Analytics** | Athena | Query service for analyzing data in S3 using standard SQL |
| **Analytics** | EMR | Managed Hadoop cluster service for processing big data |
| **Analytics** | Kinesis | Service for real-time streaming data ingestion and processing |
| **Analytics** | QuickSight | Business intelligence and visualization dashboard service |
| **ML/AI** | SageMaker | Managed machine learning service for training and deploying models |
| **ML/AI** | Rekognition | Image and video analysis service using machine learning |
| **ML/AI** | Textract | Automatic extraction of text and data from documents |
| **Integration** | SNS | Simple notification service for sending messages and alerts |
| **Integration** | SQS | Message queue service for decoupling application components |
| **Integration** | EventBridge | Event routing service connecting applications and AWS services |
| **Integration** | API Gateway | Managed service for creating and publishing REST and WebSocket APIs |
| **Migration** | DMS | Database migration service for moving databases to AWS |
| **Migration** | DataSync | Automated data transfer service for on-premises to AWS |
| **Migration** | Application Discovery Service | Tools for discovering on-premises applications and servers |

---

## Tips for Using This Cheat Sheet

- Replace placeholder values (my-bucket, subnet-12345, etc.) with your actual resource names
- Use `--query` flag with JMESPath for advanced filtering and formatting
- Add `--dry-run` flag to EC2 commands to test without making changes
- Use profiles with `--profile` for multi-account management
- Pipe output to `jq` for advanced JSON processing and formatting
- Always test destructive operations (delete, terminate) in non-production environments first
- Check AWS CLI documentation with `aws <service> help` for latest commands
