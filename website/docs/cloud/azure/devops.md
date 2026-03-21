---
title: Azure DevOps
sidebar_label: DevOps
sidebar_position: 3
---

# Azure DevOps

Azure DevOps is a comprehensive suite of tools for planning, developing, delivering, and maintaining software. It enables modern DevOps practices including continuous integration, continuous deployment, and infrastructure automation.

## Azure DevOps Overview

Azure DevOps consists of five core services:

### Azure Boards

Project management and work tracking:
- **Work Items**: Stories, tasks, bugs, epics
- **Boards**: Kanban boards for visual workflow management
- **Backlogs**: Prioritized product backlog
- **Queries**: Custom queries to track work
- **Dashboards**: Real-time visibility into project metrics
- **Sprints**: Agile sprint planning and execution

Use cases: Team collaboration, sprint planning, progress tracking

### Azure Repos

Version control system:
- **Git**: Distributed version control (recommended)
- **Team Foundation Version Control (TFVC)**: Centralized version control
- **Pull Requests**: Code review and merge workflows
- **Branch Policies**: Enforce quality gates before merge
- **Code Search**: Full-text search across repositories

Use cases: Source code management, code review, collaboration

### Azure Pipelines

Continuous Integration (CI) and Continuous Deployment (CD):
- **YAML-based pipelines**: Infrastructure as code for build and release
- **Agent-based execution**: Run on Microsoft-hosted or self-hosted agents
- **Multi-stage pipelines**: Define stages from build through production deployment
- **Templates**: Reusable pipeline components
- **Artifacts**: Build output storage and versioning

Use cases: Automated builds, testing, deployments to any cloud or on-premises

### Azure Test Plans

Test management and execution:
- **Test Cases**: Define and organize test scenarios
- **Test Suites**: Group related tests
- **Test Execution**: Manual and exploratory testing
- **Bug Tracking**: Report and track defects
- **Requirements Traceability**: Link tests to requirements

Use cases: Manual testing, test planning, quality assurance

### Azure Artifacts

Package management:
- **NuGet**: .NET package management
- **npm**: Node.js package management
- **Python**: Python package distribution
- **Maven**: Java/Maven artifacts
- **Universal Packages**: Language-agnostic package storage

Use cases: Dependency management, package versioning, internal package distribution

## Azure Pipelines

### Pipeline Fundamentals

An Azure Pipeline automates the process of building, testing, and deploying code.

**Key Concepts**:
- **Trigger**: What initiates the pipeline (code push, schedule, manual)
- **Agent**: Machine that runs pipeline jobs
- **Job**: Unit of work executing on an agent
- **Step**: Individual command or script in a job
- **Stage**: Collection of jobs that run sequentially or in parallel
- **Artifact**: Output from pipeline (binaries, packages, logs)

### YAML Pipeline Syntax

Pipelines are defined in YAML format (typically `azure-pipelines.yml`):

```yaml
trigger:
  - main
  - develop

pool:
  vmImage: 'ubuntu-latest'

variables:
  buildConfiguration: 'Release'
  nodeVersion: '18.x'

stages:
  - stage: Build
    displayName: 'Build Stage'
    jobs:
      - job: BuildJob
        displayName: 'Build Application'
        steps:
          - task: NodeTool@0
            inputs:
              versionSpec: $(nodeVersion)
            displayName: 'Install Node.js'

          - script: npm install
            displayName: 'Install Dependencies'

          - script: npm run build
            displayName: 'Build Application'

          - task: PublishBuildArtifacts@1
            inputs:
              pathToPublish: $(Build.ArtifactStagingDirectory)
              artifactName: 'drop'
            displayName: 'Publish Artifacts'

  - stage: Test
    displayName: 'Test Stage'
    dependsOn: Build
    jobs:
      - job: TestJob
        displayName: 'Run Tests'
        steps:
          - script: npm test
            displayName: 'Run Unit Tests'

          - task: PublishTestResults@2
            inputs:
              testResultsFormat: 'JUnit'
              testResultsFiles: 'test-results.xml'
            displayName: 'Publish Test Results'

  - stage: Deploy
    displayName: 'Deploy Stage'
    dependsOn: Test
    condition: succeeded()
    jobs:
      - deployment: DeployJob
        displayName: 'Deploy to Production'
        environment: 'Production'
        strategy:
          runOnce:
            deploy:
              steps:
                - download: current
                  artifact: drop

                - task: AzureAppServiceDeploy@1
                  inputs:
                    azureSubscription: 'Azure Subscription'
                    appType: 'webAppLinux'
                    appName: 'myappservice'
                    package: '$(Pipeline.Workspace)/drop'
                  displayName: 'Deploy to App Service'
```

### Pipeline Variables

Variables store values referenced throughout the pipeline:

```yaml
variables:
  buildConfiguration: 'Release'
  vmImageName: 'ubuntu-latest'
  nodeVersion: '18.x'

steps:
  - script: echo '$(buildConfiguration)'
    displayName: 'Echo Variable'
```

Variable scoping:
- **Pipeline-level**: Available to all stages and jobs
- **Stage-level**: Available within that stage
- **Job-level**: Available within that job

Predefined variables (auto-populated by Azure Pipelines):
- `$(Build.BuildId)`: Unique build identifier
- `$(Build.SourcesDirectory)`: Repository directory
- `$(Build.ArtifactStagingDirectory)`: Artifact output directory
- `$(Agent.OS)`: Operating system (Windows, Linux, Darwin)

### Templates

Templates promote reusability and maintainability:

**Job Template** (`azure-pipelines/job-template.yml`):
```yaml
parameters:
  - name: jobName
    type: string
  - name: nodeVersion
    type: string
    default: '18.x'

jobs:
  - job: ${{ parameters.jobName }}
    displayName: 'Template Job'
    pool:
      vmImage: 'ubuntu-latest'
    steps:
      - task: NodeTool@0
        inputs:
          versionSpec: ${{ parameters.nodeVersion }}
      - script: npm install
      - script: npm run build
```

**Main Pipeline Using Template**:
```yaml
trigger:
  - main

stages:
  - stage: Build
    jobs:
      - template: azure-pipelines/job-template.yml
        parameters:
          jobName: 'BuildJob'
          nodeVersion: '20.x'
```

### Stages and Conditions

Stages define phases of the pipeline, optionally conditional:

```yaml
stages:
  - stage: Build
    displayName: 'Build'
    jobs:
      - job: BuildJob
        steps:
          - script: echo 'Building...'

  - stage: Test
    displayName: 'Test'
    dependsOn: Build
    condition: succeeded()
    jobs:
      - job: TestJob
        steps:
          - script: echo 'Testing...'

  - stage: Deploy
    displayName: 'Deploy to Prod'
    dependsOn: Test
    condition: and(succeeded(), eq(variables['Build.SourceBranch'], 'refs/heads/main'))
    jobs:
      - job: DeployJob
        steps:
          - script: echo 'Deploying...'
```

### Environments and Approvals

Environments protect production deployments:

```yaml
stages:
  - stage: DeployProd
    jobs:
      - deployment: DeploymentJob
        displayName: 'Deploy to Production'
        environment:
          name: 'Production'
          resourceType: 'VirtualMachine'
          resourceId: 'prod-vm-id'
        strategy:
          runOnce:
            deploy:
              steps:
                - script: echo 'Deploying to production...'
```

Environment approvals require manual approval before job execution. Configure in Azure DevOps portal under Environments.

## Container Orchestration

### Azure Container Instances (ACI)

Simple, serverless container deployment:

```yaml
steps:
  - task: AzureContainerInstances@1
    inputs:
      azureSubscription: 'Azure Subscription'
      imageSourceType: 'Registry'
      registryType: 'Azure Container Registry'
      registryName: 'myacr'
      imageName: 'myimage:latest'
      containerInstanceName: 'mycontainer'
      ports: '80,443'
      cpuCores: '1.0'
      memoryInGb: '1.5'
    displayName: 'Deploy to ACI'
```

Use cases: Quick deployments, batch jobs, development/testing

### Azure Kubernetes Service (AKS)

Production-grade container orchestration:

```yaml
steps:
  - task: KubernetesManifest@0
    inputs:
      action: 'deploy'
      kubernetesServiceConnection: 'my-aks-cluster'
      namespace: 'production'
      manifests: |
        $(Pipeline.Workspace)/manifests/deployment.yaml
        $(Pipeline.Workspace)/manifests/service.yaml
    displayName: 'Deploy to AKS'
```

Key AKS features:
- Auto-scaling of nodes and pods
- Rolling updates and rollbacks
- Service discovery and load balancing
- Storage orchestration
- Multi-container deployments via Kubernetes manifests

## Infrastructure as Code

### ARM Templates

Azure Resource Manager templates (JSON) define infrastructure declaratively:

```json
{
  "$schema": "https://schema.management.azure.com/schemas/2019-04-01/deploymentTemplate.json#",
  "contentVersion": "1.0.0.0",
  "parameters": {
    "vmName": {
      "type": "string",
      "defaultValue": "myVM"
    },
    "location": {
      "type": "string",
      "defaultValue": "[resourceGroup().location]"
    }
  },
  "resources": [
    {
      "type": "Microsoft.Compute/virtualMachines",
      "apiVersion": "2021-07-01",
      "name": "[parameters('vmName')]",
      "location": "[parameters('location')]",
      "properties": {
        "hardwareProfile": {
          "vmSize": "Standard_DS1_v2"
        }
      }
    }
  ]
}
```

Deploy ARM template via pipeline:
```yaml
steps:
  - task: AzureResourceManagerTemplateDeployment@3
    inputs:
      deploymentScope: 'Resource Group'
      azureResourceManagerConnection: 'Azure Subscription'
      subscriptionId: '$(subscriptionId)'
      resourceGroupName: '$(resourceGroupName)'
      location: 'East US'
      templateLocation: 'Linked artifact'
      csmFile: '$(Pipeline.Workspace)/templates/azuredeploy.json'
      csmParametersFile: '$(Pipeline.Workspace)/templates/azuredeploy.parameters.json'
      deploymentMode: 'Incremental'
    displayName: 'Deploy ARM Template'
```

### Bicep

Bicep is a cleaner, more concise language for defining Azure infrastructure:

```bicep
param location string = resourceGroup().location
param vmName string = 'myVM'
param vmSize string = 'Standard_DS1_v2'

resource networkInterface 'Microsoft.Network/networkInterfaces@2021-02-01' = {
  name: '${vmName}-nic'
  location: location
  properties: {
    ipConfigurations: [
      {
        name: 'ipconfig1'
        properties: {
          subnet: {
            id: subnet.id
          }
        }
      }
    ]
  }
}

resource vm 'Microsoft.Compute/virtualMachines@2021-07-01' = {
  name: vmName
  location: location
  properties: {
    hardwareProfile: {
      vmSize: vmSize
    }
    networkProfile: {
      networkInterfaces: [
        {
          id: networkInterface.id
        }
      ]
    }
  }
}
```

Deploy Bicep via pipeline:
```yaml
steps:
  - task: AzureResourceManagerTemplateDeployment@3
    inputs:
      deploymentScope: 'Resource Group'
      azureResourceManagerConnection: 'Azure Subscription'
      resourceGroupName: '$(resourceGroupName)'
      location: 'East US'
      templateLocation: 'Linked artifact'
      csmFile: '$(Pipeline.Workspace)/main.bicep'
      deploymentMode: 'Incremental'
```

### Terraform on Azure

Terraform provisions Azure resources using HCL syntax:

```hcl
terraform {
  required_providers {
    azurerm = {
      source  = "hashicorp/azurerm"
      version = "~> 3.0"
    }
  }
}

provider "azurerm" {
  features {}
}

resource "azurerm_resource_group" "rg" {
  name     = "my-resource-group"
  location = "East US"
}

resource "azurerm_virtual_machine" "vm" {
  name                  = "my-vm"
  location              = azurerm_resource_group.rg.location
  resource_group_name   = azurerm_resource_group.rg.name
  vm_size               = "Standard_DS1_v2"
}
```

Deploy Terraform via pipeline:
```yaml
steps:
  - task: ms-devlabs.custom-terraform-tasks.custom-terraform-release-task.TerraformTaskV3@3
    inputs:
      provider: 'azurerm'
      command: 'init'
      backendServiceArm: 'Azure Subscription'
      backendAzureRmResourceGroupName: 'terraform-rg'
      backendAzureRmStorageAccountName: 'terraformstate'
      backendAzureRmContainerName: 'tfstate'
      backendAzureRmKey: 'terraform.tfstate'
    displayName: 'Terraform Init'

  - task: ms-devlabs.custom-terraform-tasks.custom-terraform-release-task.TerraformTaskV3@3
    inputs:
      provider: 'azurerm'
      command: 'plan'
      backendServiceArm: 'Azure Subscription'
      environmentServiceArm: 'Azure Subscription'
    displayName: 'Terraform Plan'

  - task: ms-devlabs.custom-terraform-tasks.custom-terraform-release-task.TerraformTaskV3@3
    inputs:
      provider: 'azurerm'
      command: 'apply'
      backendServiceArm: 'Azure Subscription'
      environmentServiceArm: 'Azure Subscription'
    displayName: 'Terraform Apply'
```

## Deployment Patterns

### Blue-Green Deployment

Two identical production environments: blue (current) and green (new). Deploy to green, test, then switch traffic.

**Benefits**:
- Zero-downtime deployments
- Quick rollback capability
- Full testing in production-like environment

**Pipeline Implementation**:
```yaml
stages:
  - stage: Deploy_Green
    jobs:
      - job: DeployGreen
        steps:
          - task: AzureAppServiceDeploy@1
            inputs:
              appName: 'myapp-green'
              package: '$(Build.ArtifactStagingDirectory)'

  - stage: Validate_Green
    jobs:
      - job: RunTests
        steps:
          - script: |
              curl -f https://myapp-green.azurewebsites.net/health
              # Run integration tests
            displayName: 'Test Green Environment'

  - stage: Switch_Traffic
    jobs:
      - job: UpdateTrafficRouting
        steps:
          - task: AzureCLI@2
            inputs:
              azureSubscription: 'Azure Subscription'
              scriptType: 'bash'
              scriptLocation: 'inlineScript'
              inlineScript: |
                az webapp traffic-routing set --name myapp --resource-group myRG --distribution green=100
            displayName: 'Route 100% traffic to green'
```

### Canary Deployment

Gradually roll out new version to a small subset of users, monitoring for issues before full rollout.

**Benefits**:
- Detect issues with real users before full rollout
- Gradual traffic shift (5% → 25% → 50% → 100%)
- Easy rollback if issues detected

**Pipeline Implementation** (using Application Gateway weighted routing):
```yaml
stages:
  - stage: Deploy_Canary
    jobs:
      - job: DeployNewVersion
        steps:
          - task: AzureAppServiceDeploy@1
            inputs:
              appName: 'myapp-v2'
              package: '$(Build.ArtifactStagingDirectory)'

          - task: AzureCLI@2
            inputs:
              azureSubscription: 'Azure Subscription'
              scriptType: 'bash'
              scriptLocation: 'inlineScript'
              inlineScript: |
                # Configure Application Gateway to route 5% to new version
                az network application-gateway http-settings update \
                  --gateway-name myAppGW \
                  --name myHttpSettings \
                  --resource-group myRG \
                  --backend-pool myapp-v2

  - stage: Monitor_Canary
    jobs:
      - job: CanaryValidation
        steps:
          - script: |
              # Monitor metrics: error rate, latency, etc.
              # If healthy, proceed. If issues, trigger rollback.
            displayName: 'Monitor canary metrics'

  - stage: Rollout_Full
    jobs:
      - job: FullDeployment
        steps:
          - task: AzureCLI@2
            inputs:
              azureSubscription: 'Azure Subscription'
              scriptType: 'bash'
              scriptLocation: 'inlineScript'
              inlineScript: |
                # Shift 100% traffic to new version
                az network application-gateway http-settings update \
                  --gateway-name myAppGW \
                  --name myHttpSettings \
                  --resource-group myRG \
                  --backend-pool myapp-v2-100pct
```

### Rolling Deployment

Update instances one or few at a time, keeping service available throughout.

**Benefits**:
- No downtime
- Automatic rollback on failure
- Resource-efficient

**Kubernetes Implementation**:
```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: myapp
spec:
  replicas: 3
  strategy:
    type: RollingUpdate
    rollingUpdate:
      maxSurge: 1
      maxUnavailable: 0
  template:
    spec:
      containers:
        - name: app
          image: myapp:v2
```

## Azure DevOps Integration with Pipelines

### Four Essential Exercises

### Exercise 1: Create a Simple CI Pipeline
1. Create Azure DevOps organization and project
2. Create a Git repository
3. Push sample Node.js application to repository
4. Create `azure-pipelines.yml` with build and test stages
5. Configure build trigger on code push to main branch
6. Run pipeline and verify build success
7. Check generated artifacts

**Expected Outcome**: Understand pipeline triggers, build jobs, and artifact generation.

### Exercise 2: Deploy to App Service with CD
1. Create Azure App Service with staging and production slots
2. Create pipeline with build, test, and deployment stages
3. Configure deployment stage to deploy to staging slot
4. Add approval gate requiring manual approval
5. Add second deployment stage to production
6. Trigger pipeline and approve deployment
7. Verify application is running in production

**Expected Outcome**: Understand deployment stages, approvals, and slot deployments.

### Exercise 3: Use Templates for Reusability
1. Create pipeline template for common tasks (build, test)
2. Create main pipeline that references templates with different parameters
3. Create job template for deploying to different environments
4. Run pipeline and verify template substitution
5. Modify template and verify changes propagate to all pipelines using it

**Expected Outcome**: Understand template reusability and parameter passing.

### Exercise 4: Deploy Infrastructure with Bicep
1. Create Bicep file defining VNet, subnet, Network Security Group, and VM
2. Create pipeline with Infrastructure as Code stage
3. Use AzureResourceManagerTemplateDeployment task to deploy Bicep
4. Configure pipeline parameters for environment (dev/prod)
5. Trigger pipeline and verify infrastructure deployment
6. Modify Bicep and redeploy to verify updates
7. Delete resources via Azure CLI

**Expected Outcome**: Understand Infrastructure as Code and Bicep deployment automation.

## Key Takeaways

- Azure DevOps provides comprehensive tools for planning, developing, and deploying software
- Azure Pipelines enables CI/CD with YAML-based, version-controlled pipeline definitions
- Templates promote reusability and consistency across pipelines
- Stages, jobs, and steps organize pipeline logic hierarchically
- Environments and approvals protect production deployments
- Container orchestration with ACI and AKS automates containerized workloads
- ARM templates and Bicep enable Infrastructure as Code for Azure resources
- Terraform provides multi-cloud infrastructure provisioning
- Blue-green, canary, and rolling deployment patterns support zero-downtime releases
