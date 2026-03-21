---
title: "AWS DevOps & CI/CD"
sidebar_label: "DevOps & CI/CD"
description: "AWS DevOps practices — CI/CD pipelines with CodePipeline, CodeBuild, CodeDeploy, containers, Terraform, and deployment strategies"
sidebar_position: 4
---

# AWS DevOps & CI/CD

## DevOps on AWS Overview

DevOps is a cultural and technical movement that combines software development and IT operations to shorten development cycles and deliver high-quality applications continuously. AWS provides a comprehensive suite of services designed to enable DevOps practices at scale.

### Core DevOps Principles

1. **Automation**: Automated build, test, and deployment pipelines eliminate manual errors and accelerate time-to-market
2. **Integration**: Continuous integration ensures code changes are tested and merged frequently
3. **Monitoring**: Real-time visibility into application and infrastructure health
4. **Collaboration**: Breaking silos between development and operations teams
5. **Infrastructure as Code**: Managing infrastructure through version-controlled code

### AWS DevOps Services Ecosystem

| Service | Purpose |
|---------|---------|
| **CodeCommit** | Git repository hosting and source control |
| **CodeBuild** | Managed build and test service |
| **CodeDeploy** | Automated deployment to EC2, on-premises, or Lambda |
| **CodePipeline** | Orchestrates CI/CD workflows across services |
| **CodeArtifact** | Private artifact repository for packages |
| **CloudFormation** | Infrastructure as Code for AWS resources |
| **AWS CDK** | Write infrastructure code in Python, TypeScript, etc. |
| **CloudWatch** | Monitoring, logging, and alerting |
| **X-Ray** | Distributed tracing and service mapping |

### Benefits of AWS DevOps

- Faster time-to-market and release cycles
- Reduced manual errors through automation
- Improved application reliability and uptime
- Scalable infrastructure that grows with demand
- Better collaboration between teams
- Cost optimization through infrastructure efficiency
- Compliance and audit trails with CloudTrail

---

## AWS CI/CD Services

### CodeCommit: Git Repository Management

AWS CodeCommit is a fully managed Git service that hosts secure, highly scalable private Git repositories.

**Key Features:**
- Fully managed, no infrastructure to maintain
- IAM-based access control
- Encrypted repositories (in transit and at rest)
- Branch protection rules and pull request workflows
- Triggers for automation (SNS, Lambda, CodePipeline)
- Integration with CodeBuild and CodePipeline

**Basic Setup:**

```bash
# Clone a CodeCommit repository
git clone codecommit://my-repository

# Configure credentials (IAM user with CodeCommit access)
# Use AWS CLI credentials or generate HTTPS Git credentials
git config --global user.name "Your Name"
git config --global user.email "your@email.com"

# Create and push a branch
git checkout -b feature/new-feature
git commit -m "Add feature"
git push -u origin feature/new-feature
```

### CodeBuild: Build and Test Service

AWS CodeBuild is a managed service that compiles source code, runs tests, and produces software packages ready for deployment.

**Key Features:**
- Pre-configured environments (Java, Python, Node.js, Go, .NET, etc.)
- Custom Docker images for specialized build needs
- Pay-per-minute pricing, scales automatically
- Build logs stored in S3 and CloudWatch Logs
- Integrates with CodePipeline for automated triggering
- Environment variables and parameter store integration

**buildspec.yml Example:**

```yaml
version: 0.2

phases:
  pre_build:
    commands:
      - echo "Installing dependencies..."
      - npm install
      - echo "Running linting and code quality checks..."
      - npm run lint
  build:
    commands:
      - echo "Running tests..."
      - npm run test
      - echo "Building application..."
      - npm run build
  post_build:
    commands:
      - echo "Build completed successfully"
      - |
        if [ "$CODEBUILD_BUILD_SUCCEEDING" = "1" ]; then
          echo "Pushing to artifact repository..."
        fi

artifacts:
  files:
    - dist/**/*
    - package.json
  name: BuildArtifact

cache:
  paths:
    - node_modules/**/*
```

**Create a Build Project:**

```bash
aws codebuild create-project \
  --name my-build-project \
  --source type=CODECOMMIT,location=https://git-codecommit.us-east-1.amazonaws.com/v1/repos/my-repo \
  --artifacts type=S3,location=my-artifacts-bucket \
  --environment type=LINUX_CONTAINER,image=aws/codebuild/standard:5.0,computeType=BUILD_GENERAL1_SMALL \
  --service-role arn:aws:iam::123456789012:role/CodeBuildRole
```

### CodeDeploy: Automated Deployment

AWS CodeDeploy automates application deployments to EC2 instances, on-premises servers, or Lambda functions.

**Key Features:**
- Automated rollback on deployment failures
- Multiple deployment strategies
- Gradual traffic shifting with validation hooks
- Lifecycle event hooks for custom scripts
- Integration with Auto Scaling groups
- Deployment to fleet of instances simultaneously

**Deployment Types:**

| Strategy | Description | Use Case |
|----------|-------------|----------|
| **In-Place** | Updates application on running instances | Quick updates, simple deployments |
| **Blue/Green** | Replaces entire instance fleet at once | Zero-downtime critical updates |
| **Canary** | Routes small percentage of traffic to new version | Safe validation of changes |
| **Linear** | Gradually shifts traffic (e.g., 10% every 5 minutes) | Controlled rollout with monitoring |

**appspec.yml Example:**

```yaml
version: 0.0
Resources:
  - TargetService:
      Type: AWS::EC2::Instance
      Properties:
        Tags:
          - Key: Name
            Value: WebServer
Files:
  - source: /
    destination: /var/www/html
Permissions:
  - object: /var/www/html
    pattern: "**"
    owner: www-data
    group: www-data
    mode: 755
Hooks:
  ApplicationStop:
    - location: scripts/stop-server.sh
      timeout: 300
  BeforeInstall:
    - location: scripts/install-dependencies.sh
      timeout: 300
  ApplicationStart:
    - location: scripts/start-server.sh
      timeout: 300
  ValidateService:
    - location: scripts/validate-service.sh
      timeout: 300
```

### CodePipeline: Orchestrating CI/CD Workflows

AWS CodePipeline is a fully managed continuous delivery service that orchestrates the entire CI/CD workflow.

**Pipeline Structure:**
- **Stages**: Logical groupings of actions (Source, Build, Test, Deploy)
- **Actions**: Individual tasks within stages (CodeCommit, CodeBuild, CodeDeploy, etc.)
- **Transitions**: Manual approval gates between stages
- **Artifacts**: Output from one stage passed to the next via S3

**Example Pipeline Architecture:**

```
Source (CodeCommit)
  ↓
Build (CodeBuild)
  ↓
Test (CodeBuild)
  ↓
Manual Approval
  ↓
Deploy to Staging (CodeDeploy)
  ↓
Integration Tests (CodeBuild)
  ↓
Deploy to Production (CodeDeploy)
```

**Creating a Basic Pipeline via AWS CLI:**

```bash
aws codepipeline create-pipeline \
  --cli-input-json file://pipeline.json
```

**pipeline.json Structure:**

```json
{
  "pipeline": {
    "name": "my-app-pipeline",
    "roleArn": "arn:aws:iam::123456789012:role/CodePipelineRole",
    "artifactStore": {
      "type": "S3",
      "location": "my-artifact-bucket"
    },
    "stages": [
      {
        "name": "Source",
        "actions": [
          {
            "name": "SourceAction",
            "actionTypeId": {
              "category": "Source",
              "owner": "AWS",
              "provider": "CodeCommit",
              "version": "1"
            },
            "configuration": {
              "RepositoryName": "my-repo",
              "BranchName": "main",
              "PollForSourceChanges": "false"
            },
            "outputArtifacts": [
              {
                "name": "SourceOutput"
              }
            ]
          }
        ]
      },
      {
        "name": "Build",
        "actions": [
          {
            "name": "BuildAction",
            "actionTypeId": {
              "category": "Build",
              "owner": "AWS",
              "provider": "CodeBuild",
              "version": "1"
            },
            "configuration": {
              "ProjectName": "my-build-project"
            },
            "inputArtifacts": [
              {
                "name": "SourceOutput"
              }
            ],
            "outputArtifacts": [
              {
                "name": "BuildOutput"
              }
            ]
          }
        ]
      },
      {
        "name": "Deploy",
        "actions": [
          {
            "name": "DeployAction",
            "actionTypeId": {
              "category": "Deploy",
              "owner": "AWS",
              "provider": "CodeDeploy",
              "version": "1"
            },
            "configuration": {
              "ApplicationName": "my-application",
              "DeploymentGroupName": "production"
            },
            "inputArtifacts": [
              {
                "name": "BuildOutput"
              }
            ]
          }
        ]
      }
    ]
  }
}
```

### CodeArtifact: Artifact Management

AWS CodeArtifact is a fully managed artifact repository service that stores and retrieves build artifacts, dependencies, and packages.

**Supported Formats:**
- Maven (Java)
- npm and Yarn (JavaScript)
- NuGet (.NET)
- Python (PyPI)
- Generic packages

**Configure npm for CodeArtifact:**

```bash
# Get authorization token
aws codeartifact get-authorization-token \
  --domain my-domain \
  --domain-owner 123456789012 \
  --region us-east-1 \
  --query authorizationToken \
  --output text

# Configure npm
npm config set registry https://my-domain-123456789012.d.codeartifact.us-east-1.amazonaws.com/npm/my-repository/
```

### Full CI/CD Pipeline Walkthrough

A complete CI/CD workflow for a Node.js application:

1. **Developer pushes code to CodeCommit**
   - Triggers CodePipeline automatically
   - Repository hook sends event to CodePipeline

2. **Source Stage**
   - CodePipeline downloads source code from CodeCommit
   - Creates source artifact in S3

3. **Build Stage**
   - CodeBuild starts using buildspec.yml
   - Installs dependencies: npm install
   - Runs tests: npm test
   - Builds application: npm run build
   - Publishes artifact to S3
   - Pushes Docker image to ECR

4. **Test Stage**
   - CodeBuild runs integration tests
   - CodeBuild runs performance tests
   - Results logged to CloudWatch

5. **Manual Approval (Optional)**
   - Team reviews test results
   - Approves deployment to production

6. **Deploy to Staging**
   - CodeDeploy targets staging EC2 instances
   - Uses blue/green deployment strategy
   - Routes 10% traffic to new version (canary)
   - Validates with health checks

7. **Deploy to Production**
   - CodeDeploy targets production instances
   - Gradual traffic shift over 30 minutes
   - Auto-rollback if alarms trigger
   - CloudWatch monitors application metrics

---

## Deployment Strategies on AWS

### Rolling Deployment

**Description:** Updates instances one at a time (or in small batches), keeping the application available throughout.

**Pros:**
- Zero downtime
- Simple to implement
- Easy to rollback individual instances

**Cons:**
- Prolonged deployment window
- Mixed versions running simultaneously
- Can cause issues with backward compatibility

**Implementation with CodeDeploy:**

```yaml
# appspec.yml
version: 0.0
Resources:
  - TargetService:
      Type: AWS::EC2::Instance
Hooks:
  BeforeBlockingTraffic:
    - location: scripts/pre-traffic-hook.sh
  AfterBlockingTraffic:
    - location: scripts/post-traffic-hook.sh
```

### Blue/Green Deployment

**Description:** Maintains two identical production environments. Traffic switches from blue (current) to green (new) once validated.

**Advantages:**
- Instant switchover, minimal downtime
- Quick rollback by switching back to blue
- Full environment testing before cutover
- No mixed versions

**Disadvantages:**
- Requires double infrastructure resources
- Database migration complexity
- Initial setup complexity

**Implementation with CodeDeploy:**

```bash
aws deploy create-deployment \
  --application-name my-app \
  --deployment-group-name production \
  --deployment-config-name CodeDeployDefault.AllAtOnceBlueGreen \
  --blue-green-deployment-configuration \
    terminateBlueInstancesOnDeploymentSuccess={action=TERMINATE,terminationWaitTimeInMinutes=5}
```

**With Application Load Balancer:**

```
User Traffic
    ↓
 ALB Listener
    ↓
┌─────────────┬─────────────┐
│   Blue      │   Green     │
│  (Current)  │   (New)     │
│  ASG v1.0   │   ASG v2.0  │
└─────────────┴─────────────┘

# After validation, switch target group
ALB → Route to Green Target Group
```

### Canary Deployment

**Description:** Routes small percentage of traffic to new version, monitors metrics, then gradually increases traffic if healthy.

**Ideal For:**
- Testing in production safely
- Detecting issues with real traffic patterns
- Building confidence in changes

**Canary Steps:**
1. Deploy new version alongside current
2. Route 5-10% of traffic to new version
3. Monitor metrics for errors
4. If healthy: increase to 25%, then 50%, then 100%
5. If unhealthy: immediate rollback

**AWS SAM Example (Canary with Lambda):**

```yaml
Transform: AWS::Serverless-2016-10-31

Globals:
  Function:
    Timeout: 20
    MemorySize: 128
    Tracing: Active

Resources:
  MyFunction:
    Type: AWS::Serverless::Function
    Properties:
      Handler: index.handler
      Runtime: nodejs18.x
      CodeUri: src/
      AutoPublishAlias: live
      DeploymentPreference:
        Type: Canary
        Percent: 10
        Interval: 5
        TriggerEvents:
          - CloudWatchAlarm: !Ref CanaryErrorsAlarm

  CanaryErrorsAlarm:
    Type: AWS::CloudWatch::Alarm
    Properties:
      AlarmName: lambda-canary-errors
      MetricName: Errors
      Namespace: AWS/Lambda
      Statistic: Sum
      Period: 60
      EvaluationPeriods: 2
      Threshold: 1
      ComparisonOperator: GreaterThanOrEqualToThreshold
```

### All-at-Once Deployment

**Description:** Deploys to all instances simultaneously. Application experiences brief downtime.

**Use Cases:**
- Non-critical updates
- Maintenance windows
- Simple applications

**Best For:** Development and staging environments

### Deployment Strategies Comparison

| Strategy | Downtime | Rollback Time | Resource Cost | Complexity |
|----------|----------|---------------|---------------|-----------|
| **All-at-Once** | Yes | Slow | Low | Low |
| **Rolling** | No | Medium | Medium | Medium |
| **Blue/Green** | Minimal | Instant | High | Medium |
| **Canary** | No | Instant | Medium | High |

---

## Infrastructure as Code

### CloudFormation: AWS Native IaC

AWS CloudFormation allows you to define and provision AWS infrastructure using JSON or YAML templates.

**Key Concepts:**

- **Template:** YAML/JSON file defining resources
- **Stack:** Instantiation of a template (creates actual resources)
- **Change Set:** Preview of changes before applying
- **Drift Detection:** Identifies manual changes to stack resources

**CloudFormation Template Structure:**

```yaml
AWSTemplateFormatVersion: '2010-09-09'
Description: 'VPC with EC2 instance'

Parameters:
  EnvironmentName:
    Type: String
    Default: dev
    Description: Environment name

Resources:
  # VPC
  MyVPC:
    Type: AWS::EC2::VPC
    Properties:
      CidrBlock: 10.0.0.0/16
      EnableDnsHostnames: true
      Tags:
        - Key: Name
          Value: !Sub '${EnvironmentName}-vpc'

  # Public Subnet
  PublicSubnet:
    Type: AWS::EC2::Subnet
    Properties:
      VpcId: !Ref MyVPC
      CidrBlock: 10.0.1.0/24
      AvailabilityZone: !Select [0, !GetAZs '']
      MapPublicIpOnLaunch: true

  # Internet Gateway
  IGW:
    Type: AWS::EC2::InternetGateway
    Properties:
      Tags:
        - Key: Name
          Value: !Sub '${EnvironmentName}-igw'

  AttachGateway:
    Type: AWS::EC2::VPCGatewayAttachment
    Properties:
      VpcId: !Ref MyVPC
      InternetGatewayId: !Ref IGW

  # Route Table
  PublicRT:
    Type: AWS::EC2::RouteTable
    Properties:
      VpcId: !Ref MyVPC
      Tags:
        - Key: Name
          Value: !Sub '${EnvironmentName}-public-rt'

  PublicRoute:
    Type: AWS::EC2::Route
    DependsOn: AttachGateway
    Properties:
      RouteTableId: !Ref PublicRT
      DestinationCidrBlock: 0.0.0.0/0
      GatewayId: !Ref IGW

  SubnetRouteTableAssoc:
    Type: AWS::EC2::SubnetRouteTableAssociation
    Properties:
      SubnetId: !Ref PublicSubnet
      RouteTableId: !Ref PublicRT

  # Security Group
  WebSecurityGroup:
    Type: AWS::EC2::SecurityGroup
    Properties:
      GroupDescription: Enable HTTP and SSH
      VpcId: !Ref MyVPC
      SecurityGroupIngress:
        - IpProtocol: tcp
          FromPort: 80
          ToPort: 80
          CidrIp: 0.0.0.0/0
        - IpProtocol: tcp
          FromPort: 443
          ToPort: 443
          CidrIp: 0.0.0.0/0
        - IpProtocol: tcp
          FromPort: 22
          ToPort: 22
          CidrIp: 203.0.113.0/32  # Restrict SSH to your IP

  # EC2 Instance
  WebServer:
    Type: AWS::EC2::Instance
    Properties:
      ImageId: ami-0c55b159cbfafe1f0
      InstanceType: t3.micro
      SubnetId: !Ref PublicSubnet
      SecurityGroupIds:
        - !Ref WebSecurityGroup
      UserData:
        Fn::Base64: |
          #!/bin/bash
          yum update -y
          yum install -y httpd
          systemctl start httpd
          echo 'Hello from CloudFormation' > /var/www/html/index.html
      Tags:
        - Key: Name
          Value: !Sub '${EnvironmentName}-webserver'

Outputs:
  VPCId:
    Description: VPC ID
    Value: !Ref MyVPC
  PublicSubnetId:
    Description: Public Subnet ID
    Value: !Ref PublicSubnet
  WebServerPublicIP:
    Description: Public IP of web server
    Value: !GetAtt WebServer.PublicIp
```

**CloudFormation Stack Operations:**

```bash
# Create stack
aws cloudformation create-stack \
  --stack-name my-stack \
  --template-body file://template.yaml \
  --parameters ParameterKey=EnvironmentName,ParameterValue=prod

# Create change set (preview changes)
aws cloudformation create-change-set \
  --stack-name my-stack \
  --change-set-name update-1 \
  --template-body file://updated-template.yaml \
  --change-set-type UPDATE

# Execute change set
aws cloudformation execute-change-set \
  --stack-name my-stack \
  --change-set-name update-1

# Detect drift
aws cloudformation detect-stack-drift \
  --stack-name my-stack

# Delete stack
aws cloudformation delete-stack \
  --stack-name my-stack
```

### AWS CDK: Infrastructure as Code with Programming Languages

AWS Cloud Development Kit (CDK) allows you to define infrastructure using TypeScript, Python, Java, or other languages.

**Advantages over CloudFormation:**
- Higher-level abstractions (L3 constructs)
- Reusable components
- Type safety with TypeScript
- Easier logic and conditions

**Example AWS CDK (TypeScript):**

```typescript
import * as cdk from 'aws-cdk-lib';
import * as ec2 from 'aws-cdk-lib/aws-ec2';
import * as elbv2 from 'aws-cdk-lib/aws-elasticloadbalancingv2';

export class MyStack extends cdk.Stack {
  constructor(scope: cdk.App, id: string, props?: cdk.StackProps) {
    super(scope, id, props);

    // Create VPC
    const vpc = new ec2.Vpc(this, 'VPC', {
      maxAzs: 3,
      cidrMask: 24,
    });

    // Create ASG with ALB
    const asg = new ec2.AutoScalingGroup(this, 'ASG', {
      vpc,
      instanceType: ec2.InstanceType.of(
        ec2.InstanceClass.T3,
        ec2.InstanceSize.MICRO
      ),
      machineImage: new ec2.AmazonLinuxImage({
        generation: ec2.AmazonLinuxGeneration.AMAZON_LINUX_2,
      }),
      desiredCapacity: 2,
    });

    // Add ALB
    const lb = new elbv2.ApplicationLoadBalancer(this, 'LB', {
      vpc,
      internetFacing: true,
    });

    const listener = lb.addListener('Listener', {
      port: 80,
    });

    listener.addTargets('Target', {
      port: 80,
      targets: [asg],
    });
  }
}

const app = new cdk.App();
new MyStack(app, 'MyStack');
```

**Deploy CDK:**

```bash
cdk synth          # Generate CloudFormation template
cdk deploy         # Deploy stack
cdk destroy        # Delete stack
```

### Terraform on AWS

Terraform is a multi-cloud IaC tool with excellent AWS support.

**Terraform AWS Provider Configuration:**

```hcl
terraform {
  required_version = ">= 1.0"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
  }

  backend "s3" {
    bucket         = "terraform-state-bucket"
    key            = "prod/terraform.tfstate"
    region         = "us-east-1"
    encrypt        = true
    dynamodb_table = "terraform-locks"
  }
}

provider "aws" {
  region = var.aws_region

  default_tags {
    tags = {
      Environment = var.environment
      Project     = var.project_name
    }
  }
}
```

**State Management with S3 Backend:**

```bash
# Initialize S3 backend
aws s3 mb s3://terraform-state-bucket

# Enable versioning
aws s3api put-bucket-versioning \
  --bucket terraform-state-bucket \
  --versioning-configuration Status=Enabled

# Enable encryption
aws s3api put-bucket-encryption \
  --bucket terraform-state-bucket \
  --server-side-encryption-configuration '{
    "Rules": [{
      "ApplyServerSideEncryptionByDefault": {
        "SSEAlgorithm": "AES256"
      }
    }]
  }'

# Create DynamoDB table for locking
aws dynamodb create-table \
  --table-name terraform-locks \
  --attribute-definitions AttributeName=LockID,AttributeType=S \
  --key-schema AttributeName=LockID,KeyType=HASH \
  --provisioned-throughput ReadCapacityUnits=5,WriteCapacityUnits=5
```

**Terraform VPC and EC2 Example:**

```hcl
# variables.tf
variable "aws_region" {
  default = "us-east-1"
}

variable "environment" {
  default = "dev"
}

variable "instance_type" {
  default = "t3.micro"
}

# main.tf
data "aws_ami" "amazon_linux_2" {
  most_recent = true
  owners      = ["amazon"]

  filter {
    name   = "name"
    values = ["amzn2-ami-hvm-*-x86_64-gp2"]
  }
}

resource "aws_vpc" "main" {
  cidr_block           = "10.0.0.0/16"
  enable_dns_hostnames = true

  tags = {
    Name = "${var.environment}-vpc"
  }
}

resource "aws_subnet" "public" {
  vpc_id                  = aws_vpc.main.id
  cidr_block              = "10.0.1.0/24"
  availability_zone       = data.aws_availability_zones.available.names[0]
  map_public_ip_on_launch = true

  tags = {
    Name = "${var.environment}-public-subnet"
  }
}

resource "aws_internet_gateway" "main" {
  vpc_id = aws_vpc.main.id

  tags = {
    Name = "${var.environment}-igw"
  }
}

resource "aws_route_table" "public" {
  vpc_id = aws_vpc.main.id

  route {
    cidr_block      = "0.0.0.0/0"
    gateway_id      = aws_internet_gateway.main.id
  }

  tags = {
    Name = "${var.environment}-public-rt"
  }
}

resource "aws_route_table_association" "public" {
  subnet_id      = aws_subnet.public.id
  route_table_id = aws_route_table.public.id
}

resource "aws_security_group" "web" {
  name_prefix = "web-"
  vpc_id      = aws_vpc.main.id

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

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = {
    Name = "${var.environment}-web-sg"
  }
}

resource "aws_instance" "web" {
  ami                    = data.aws_ami.amazon_linux_2.id
  instance_type          = var.instance_type
  subnet_id              = aws_subnet.public.id
  vpc_security_group_ids = [aws_security_group.web.id]

  user_data = base64encode(<<-EOF
    #!/bin/bash
    yum update -y
    yum install -y httpd
    systemctl start httpd
    echo 'Hello from Terraform' > /var/www/html/index.html
  EOF
  )

  tags = {
    Name = "${var.environment}-web-server"
  }
}

data "aws_availability_zones" "available" {
  state = "available"
}

# outputs.tf
output "instance_public_ip" {
  value = aws_instance.web.public_ip
}
```

**Terraform Commands:**

```bash
terraform init       # Initialize backend and providers
terraform plan      # Preview changes
terraform apply     # Apply changes
terraform destroy   # Destroy all resources
terraform fmt       # Format code
terraform validate  # Validate configuration
```

---

## Containers on AWS

### ECS with CI/CD

Amazon Elastic Container Service (ECS) is a managed container orchestration service that makes it easy to run Docker containers at scale.

**ECS Architecture:**
- **Cluster:** Logical grouping of EC2 instances or Fargate capacity
- **Task Definition:** Blueprint for Docker containers (similar to docker-compose)
- **Service:** Runs and maintains desired number of task instances
- **Task:** Running instance of a task definition

**Docker to ECS CI/CD Pipeline:**

```
CodeCommit (Source)
    ↓
CodeBuild (Build & Test)
    ↓
CodeBuild (Docker Build & Push to ECR)
    ↓
CodeDeploy/CodePipeline (Update ECS Service)
    ↓
ECS Service (Run Updated Containers)
    ↓
ALB (Route Traffic)
```

**buildspec.yml for Docker Build and ECR Push:**

```yaml
version: 0.2

phases:
  pre_build:
    commands:
      - echo Logging in to Amazon ECR...
      - aws ecr get-login-password --region $AWS_DEFAULT_REGION | docker login --username AWS --password-stdin $AWS_ACCOUNT_ID.dkr.ecr.$AWS_DEFAULT_REGION.amazonaws.com
      - REPOSITORY_URI=$AWS_ACCOUNT_ID.dkr.ecr.$AWS_DEFAULT_REGION.amazonaws.com/my-app
      - COMMIT_HASH=$(echo $CODEBUILD_RESOLVED_SOURCE_VERSION | cut -c 1-7)
      - IMAGE_TAG=${COMMIT_HASH:=latest}
  build:
    commands:
      - echo Build started on `date`
      - docker build -t $REPOSITORY_URI:latest .
      - docker tag $REPOSITORY_URI:latest $REPOSITORY_URI:$IMAGE_TAG
  post_build:
    commands:
      - echo Build completed on `date`
      - docker push $REPOSITORY_URI:latest
      - docker push $REPOSITORY_URI:$IMAGE_TAG
      - echo Writing image definitions file...
      - printf '[{"name":"my-app","imageUri":"%s"}]' $REPOSITORY_URI:$IMAGE_TAG > imagedefinitions.json

artifacts:
  files: imagedefinitions.json
```

**ECS Task Definition:**

```json
{
  "family": "my-app",
  "networkMode": "awsvpc",
  "requiresCompatibilities": ["FARGATE"],
  "cpu": "256",
  "memory": "512",
  "containerDefinitions": [
    {
      "name": "my-app",
      "image": "123456789012.dkr.ecr.us-east-1.amazonaws.com/my-app:latest",
      "portMappings": [
        {
          "containerPort": 3000,
          "protocol": "tcp"
        }
      ],
      "logConfiguration": {
        "logDriver": "awslogs",
        "options": {
          "awslogs-group": "/ecs/my-app",
          "awslogs-region": "us-east-1",
          "awslogs-stream-prefix": "ecs"
        }
      },
      "environment": [
        {
          "name": "NODE_ENV",
          "value": "production"
        }
      ]
    }
  ]
}
```

**Create ECS Service:**

```bash
aws ecs create-service \
  --cluster my-cluster \
  --service-name my-app \
  --task-definition my-app:1 \
  --desired-count 2 \
  --launch-type FARGATE \
  --network-configuration \
    "awsvpcConfiguration={subnets=[subnet-12345678],securityGroups=[sg-12345678],assignPublicIp=ENABLED}" \
  --load-balancers targetGroupArn=arn:aws:elasticloadbalancing:us-east-1:123456789012:targetgroup/my-app/abc123,containerName=my-app,containerPort=3000
```

### EKS with CI/CD

Amazon Elastic Kubernetes Service (EKS) is a managed Kubernetes service for running containerized applications at scale.

**EKS CI/CD Pipeline:**

```
CodeCommit → CodeBuild → Build Image → Push to ECR
                                            ↓
                    CodeBuild → Update Kubernetes Manifests
                                            ↓
                        CodePipeline → Deploy to EKS Cluster
                                            ↓
                            ALB/NLB → Route to Pods
```

**Kubernetes Deployment Manifest:**

```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: my-app
spec:
  replicas: 3
  selector:
    matchLabels:
      app: my-app
  template:
    metadata:
      labels:
        app: my-app
    spec:
      containers:
      - name: my-app
        image: 123456789012.dkr.ecr.us-east-1.amazonaws.com/my-app:latest
        ports:
        - containerPort: 3000
        env:
        - name: NODE_ENV
          value: "production"
        resources:
          requests:
            memory: "256Mi"
            cpu: "250m"
          limits:
            memory: "512Mi"
            cpu: "500m"
        livenessProbe:
          httpGet:
            path: /health
            port: 3000
          initialDelaySeconds: 30
          periodSeconds: 10
        readinessProbe:
          httpGet:
            path: /ready
            port: 3000
          initialDelaySeconds: 5
          periodSeconds: 5

---
apiVersion: v1
kind: Service
metadata:
  name: my-app-service
spec:
  selector:
    app: my-app
  ports:
  - protocol: TCP
    port: 80
    targetPort: 3000
  type: LoadBalancer
```

**Deploy to EKS:**

```bash
kubectl apply -f deployment.yaml
kubectl set image deployment/my-app my-app=123456789012.dkr.ecr.us-east-1.amazonaws.com/my-app:v2.0
kubectl rollout status deployment/my-app
```

### Fargate: Serverless Containers

AWS Fargate is a serverless compute engine for containers that eliminates the need to manage EC2 instances.

**Fargate Benefits:**
- No infrastructure management
- Pay per vCPU and memory consumed
- Automatic scaling
- Built-in security isolation
- Tight integration with ECS/EKS

**Fargate Task Definition Considerations:**

```json
{
  "requiresCompatibilities": ["FARGATE"],
  "networkMode": "awsvpc",
  "cpu": "256",
  "memory": "512",
  "containerDefinitions": [
    {
      "image": "123456789012.dkr.ecr.us-east-1.amazonaws.com/my-app:latest",
      "cpu": 256,
      "memory": 512
    }
  ]
}
```

**Fargate CPU and Memory Combinations:**
- 256 (.25 vCPU): 512 MB, 1 GB, 2 GB
- 512 (.5 vCPU): 1 GB - 4 GB (1 GB increments)
- 1024 (1 vCPU): 2 GB - 8 GB (1 GB increments)
- 2048 (2 vCPU): 4 GB - 16 GB (1 GB increments)
- 4096 (4 vCPU): 8 GB - 30 GB (1 GB increments)

---

## Jenkins on AWS

### Jenkins on EC2 Setup

**Launch EC2 Instance for Jenkins:**

```bash
# Launch t3.medium instance with Amazon Linux 2
aws ec2 run-instances \
  --image-id ami-0c55b159cbfafe1f0 \
  --instance-type t3.medium \
  --key-name my-key \
  --security-groups jenkins-sg
```

**User Data Script:**

```bash
#!/bin/bash
yum update -y
yum install -y java-11-openjdk java-11-openjdk-devel
wget -O /etc/yum.repos.d/jenkins.repo https://pkg.jenkins.io/redhat-stable/jenkins.repo
rpm --import https://pkg.jenkins.io/redhat-stable/jenkins.io.key
yum install -y jenkins
systemctl start jenkins
systemctl enable jenkins
```

**Configure Jenkins Security:**

```bash
# Get initial admin password
cat /var/lib/jenkins/secrets/initialAdminPassword

# Configure Jenkins to run on port 8080 with reverse proxy (Nginx)
sudo systemctl start nginx
```

### Jenkins Integration with CodePipeline

**Jenkins Plugin for CodePipeline:**

1. Install CodePipeline Plugin in Jenkins
2. Create Jenkins job linked to CodePipeline
3. Use IAM role to authenticate Jenkins to AWS

**Jenkins Pipeline with AWS:**

```groovy
pipeline {
  agent any

  stages {
    stage('Checkout') {
      steps {
        checkout scm
      }
    }

    stage('Build') {
      steps {
        sh 'npm install'
        sh 'npm run build'
      }
    }

    stage('Test') {
      steps {
        sh 'npm test'
      }
    }

    stage('Push to CodeArtifact') {
      steps {
        sh '''
          aws codeartifact get-authorization-token \
            --domain my-domain \
            --domain-owner 123456789012 \
            --region us-east-1 \
            --query authorizationToken \
            --output text | npm install
        '''
      }
    }

    stage('Notify CodePipeline') {
      steps {
        sh '''
          aws codepipeline put-job-success-result \
            --job-id $CODEPIPELINE_JOB_ID
        '''
      }
    }
  }

  post {
    failure {
      sh '''
        aws codepipeline put-job-failure-result \
          --job-id $CODEPIPELINE_JOB_ID \
          --failure-details message="Jenkins build failed"
      '''
    }
  }
}
```

### Jenkins with ECS/Docker Agents

**Use ECS as Jenkins Agent Pool:**

```groovy
pipeline {
  agent {
    ecs {
      cluster = 'jenkins-cluster'
      taskDefinition = 'jenkins-agent'
      launchType = 'FARGATE'
      networkMode = 'awsvpc'
      subnets = ['subnet-12345678']
      securityGroups = ['sg-12345678']
    }
  }

  stages {
    stage('Build') {
      steps {
        sh 'docker build -t my-app:latest .'
        sh 'docker push 123456789012.dkr.ecr.us-east-1.amazonaws.com/my-app:latest'
      }
    }
  }
}
```

---

## Monitoring & Observability for DevOps

### CloudWatch: Core Monitoring Service

**CloudWatch Components:**

1. **Metrics:** Time-series data about AWS resources
2. **Logs:** Application and system logs aggregated centrally
3. **Alarms:** Triggers actions based on metric thresholds
4. **Dashboards:** Custom visualizations of metrics

**Create Custom Metric:**

```bash
aws cloudwatch put-metric-data \
  --namespace MyApplication \
  --metric-name ProcessedRecords \
  --value 123 \
  --unit Count
```

**Create Alarm:**

```bash
aws cloudwatch put-metric-alarm \
  --alarm-name high-error-rate \
  --alarm-description "Alert when error rate exceeds 5%" \
  --metric-name Errors \
  --namespace AWS/Lambda \
  --statistic Sum \
  --period 300 \
  --threshold 50 \
  --comparison-operator GreaterThanThreshold \
  --evaluation-periods 2 \
  --alarm-actions arn:aws:sns:us-east-1:123456789012:alert-topic
```

**CloudWatch Log Insights Query:**

```
fields @timestamp, @message, @duration
| filter @message like /ERROR/
| stats count() as error_count by bin(5m)
```

### X-Ray: Distributed Tracing

AWS X-Ray provides insights into application behavior across microservices.

**Instrument Application with X-Ray SDK:**

```javascript
// Node.js example
const AWSXRay = require('aws-xray-sdk-core');
const http = AWSXRay.captureHTTPsGlobal(require('http'));
const mysql = AWSXRay.captureClient(require('mysql'));

const connection = mysql.createConnection({
  host: 'mydb.example.com',
  user: 'root',
  password: 'password',
  database: 'mydb'
});

const subsegment = AWSXRay.getSegment().addNewSubsegment('query');
connection.query('SELECT * FROM users', (err, results) => {
  subsegment.close();
});
```

**X-Ray Service Map Example:**

```
User Request
    ↓
API Gateway
    ↓
   ├─→ Lambda (100ms)
   │    ├─→ DynamoDB (50ms)
   │    └─→ S3 (30ms)
   │
   ├─→ RDS (150ms)
   │
   └─→ SQS (10ms)
        ↓
   Async Processing
```

### CloudTrail: Audit Logging

CloudTrail records API calls and activities in your AWS account.

**Enable CloudTrail:**

```bash
aws cloudtrail create-trail \
  --name my-trail \
  --s3-bucket-name my-cloudtrail-bucket

aws cloudtrail start-logging \
  --trail-name my-trail
```

**CloudTrail Event Structure:**

```json
{
  "eventVersion": "1.09",
  "eventTime": "2024-01-15T10:30:45Z",
  "eventSource": "ec2.amazonaws.com",
  "eventName": "RunInstances",
  "awsRegion": "us-east-1",
  "sourceIPAddress": "203.0.113.1",
  "userAgent": "aws-cli/2.0",
  "requestParameters": {
    "instanceType": "t3.micro",
    "imageId": "ami-0c55b159cbfafe1f0"
  },
  "responseElements": {
    "instancesSet": {
      "items": [
        {
          "instanceId": "i-0123456789abcdef0"
        }
      ]
    }
  }
}
```

### AWS Config: Compliance and Configuration Management

AWS Config tracks configuration changes and evaluates compliance.

**Create Config Rule:**

```bash
aws configservice put-config-rule \
  --config-rule file://ec2-encrypted-volumes.json
```

**ec2-encrypted-volumes.json:**

```json
{
  "ConfigRuleName": "encrypted-volumes",
  "Description": "Checks that EC2 volumes are encrypted",
  "Source": {
    "Owner": "AWS",
    "SourceIdentifier": "ENCRYPTED_VOLUMES"
  }
}
```

---

## Microservices on AWS

### API Gateway + Lambda

**API Gateway Architecture:**

```
User Request
    ↓
API Gateway
    ├─→ REST API Endpoint
    ├─→ Request Validation
    ├─→ Throttling & Quotas
    ├─→ CORS Configuration
    │
    ↓
Lambda Functions
    ├─→ GET /users → ListUsers
    ├─→ POST /users → CreateUser
    ├─→ GET /users/{id} → GetUser
    └─→ DELETE /users/{id} → DeleteUser
```

**Create API Gateway with Lambda:**

```bash
# Create REST API
API_ID=$(aws apigateway create-rest-api \
  --name my-api \
  --description "My Microservice API" \
  --query 'id' --output text)

# Create Lambda function
aws lambda create-function \
  --function-name GetUsers \
  --runtime nodejs18.x \
  --role arn:aws:iam::123456789012:role/LambdaRole \
  --handler index.handler \
  --zip-file fileb://function.zip

# Create API resource
RESOURCE_ID=$(aws apigateway get-resources \
  --rest-api-id $API_ID \
  --query 'items[0].id' --output text)

# Create /users resource
USERS_RESOURCE=$(aws apigateway create-resource \
  --rest-api-id $API_ID \
  --parent-id $RESOURCE_ID \
  --path-part users \
  --query 'id' --output text)

# Create GET method
aws apigateway put-method \
  --rest-api-id $API_ID \
  --resource-id $USERS_RESOURCE \
  --http-method GET \
  --authorization-type NONE

# Integrate with Lambda
aws apigateway put-integration \
  --rest-api-id $API_ID \
  --resource-id $USERS_RESOURCE \
  --http-method GET \
  --type AWS_PROXY \
  --integration-http-method POST \
  --uri arn:aws:apigateway:us-east-1:lambda:path/2015-03-31/functions/arn:aws:lambda:us-east-1:123456789012:function:GetUsers/invocations
```

### ECS/EKS Microservices

**ECS Service Discovery:**

```bash
aws ecs register-service \
  --cluster my-cluster \
  --service-name user-service \
  --task-definition user-service:1 \
  --desired-count 2 \
  --service-registries registryArn=arn:aws:servicediscovery:us-east-1:123456789012:service/user-service
```

**Inter-service Communication:**

```
User Service
    ↓ (calls via service discovery)
Order Service
    ↓
Inventory Service
```

### Event-Driven Architecture with EventBridge

**EventBridge Rule:**

```bash
aws events put-rule \
  --name order-processing \
  --event-pattern '{
    "source": ["order.service"],
    "detail-type": ["Order Placed"],
    "detail": {
      "status": ["confirmed"]
    }
  }' \
  --state ENABLED
```

**Event Processing Pipeline:**

```
Order Placed Event
    ↓
EventBridge Rule
    ├─→ SNS Topic (notify admin)
    ├─→ SQS Queue (async processing)
    ├─→ Lambda (update inventory)
    └─→ Kinesis Stream (analytics)
```

---

## Hands-On Exercises

### Exercise 1: Build a CI/CD Pipeline with CodePipeline

**Objective:** Create a CodePipeline that automatically builds and deploys a Node.js application.

**Steps:**

1. Create a CodeCommit repository and push a Node.js application
2. Create a CodeBuild project with buildspec.yml
3. Create CodeDeploy application targeting EC2 instances
4. Create CodePipeline with Source, Build, and Deploy stages
5. Test by pushing code changes and verifying automatic deployment

**Success Criteria:**
- Code pushed to CodeCommit triggers pipeline
- Pipeline builds application and runs tests
- Application deployed to EC2 instances with zero downtime
- Deployment visible within 5 minutes

### Exercise 2: Implement Blue/Green Deployment with CodeDeploy

**Objective:** Deploy application updates with instant rollback capability.

**Steps:**

1. Create two EC2 ASGs (blue and current)
2. Configure Application Load Balancer with target groups
3. Create CodeDeploy deployment group for blue/green
4. Configure appspec.yml with health check validation
5. Deploy new version and verify traffic routing
6. Rollback and verify quick recovery

**Success Criteria:**
- Zero downtime during deployment
- Traffic switches instantly after validation
- Rollback completes in less than 30 seconds
- All instances report healthy status

### Exercise 3: Create CloudFormation Stack for 3-Tier Application

**Objective:** Define infrastructure as code for a multi-tier application.

**Architecture:**
- Web tier: ALB in public subnets
- Application tier: Auto Scaling group in private subnets
- Database tier: RDS in private subnets

**Steps:**

1. Create CloudFormation template with VPC, subnets, routing
2. Add security groups for each tier
3. Define RDS database with encrypted storage
4. Create Auto Scaling group for application servers
5. Add ALB with listener rules
6. Create outputs for stack information

**Success Criteria:**
- Stack creates without errors
- All resources properly configured
- Web tier accessible via ALB
- Database accessible from app tier only
- Stack outputs provide useful information

### Exercise 4: Deploy Docker Container to ECS with Fargate

**Objective:** Run containerized application on serverless ECS Fargate.

**Steps:**

1. Create ECR repository
2. Build and push Docker image
3. Create ECS cluster (Fargate launch type)
4. Create ECS task definition
5. Create ECS service with ALB
6. Verify container is running and accessible
7. Update image tag and verify service automatically updates

**Success Criteria:**
- Container builds and pushes to ECR successfully
- ECS service launches desired number of tasks
- Application accessible through ALB
- Service auto-healing replaces failed tasks
- Container can be updated without manual intervention

---

## Summary

AWS DevOps services provide a comprehensive platform for implementing modern CI/CD practices:

- **CodeCommit** enables centralized source control with security
- **CodeBuild** automates testing and artifact creation
- **CodeDeploy** provides flexible deployment strategies
- **CodePipeline** orchestrates the entire workflow
- **Infrastructure as Code** (CloudFormation, CDK, Terraform) codifies infrastructure
- **Containers** (ECS, EKS, Fargate) simplify application deployment
- **Monitoring** (CloudWatch, X-Ray, CloudTrail) provides observability

By combining these services, organizations can achieve rapid, reliable, and automated application delivery while maintaining security and compliance.
