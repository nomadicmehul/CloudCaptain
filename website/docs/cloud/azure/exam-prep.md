---
title: Azure Certification Prep
sidebar_label: Exam Prep
sidebar_position: 4
---

# Azure Certification Prep

Microsoft Azure certifications validate cloud skills and advance your career. This guide provides exam overviews, study guidance, and practice questions.

## Azure Certification Path

| Certification | Exam Code | Duration | Format | Focus | Prerequisite |
|---|---|---|---|---|---|
| Azure Fundamentals | AZ-900 | 60 min | 40-60 questions | Basic cloud/Azure concepts | None |
| Azure Administrator | AZ-104 | 120 min | 40-55 questions | Manage Azure resources, VMs, networking | AZ-900 (recommended) |
| Azure Developer | AZ-204 | 120 min | 40-55 questions | Develop apps using Azure services | AZ-900 (recommended) |
| Azure DevOps Engineer | AZ-400 | 120 min | 40-55 questions | DevOps, CI/CD, infrastructure automation | AZ-104 or AZ-204 |
| Azure Security Engineer | AZ-500 | 120 min | 40-55 questions | Implement security across Azure | AZ-104 (recommended) |
| Azure Solutions Architect | AZ-305 | 120 min | 40-55 questions | Design cloud solutions | AZ-104 or AZ-204 |
| Azure Data Fundamentals | DP-900 | 60 min | 40-60 questions | Database and data concepts | None |
| Azure Data Engineer | DP-203 | 120 min | 40-55 questions | Design and implement data solutions | DP-900 (recommended) |

## AZ-900: Azure Fundamentals

### Exam Overview

**Duration**: 60 minutes

**Questions**: 40-60 (mix of multiple choice, true/false, multiple select)

**Pass Score**: 700/1000

**Cost**: USD 99 (renewal every 6 months)

**Target Audience**: IT professionals, decision makers, developers new to cloud

### Exam Domains and Weights

| Domain | Weight |
|---|---|
| Describe cloud computing concepts | 20-25% |
| Describe Azure architecture and services | 35-40% |
| Describe Azure management and governance | 20-25% |
| Describe general security and compliance features | 10-15% |

### Domain 1: Cloud Computing Concepts (20-25%)

**Concepts**:
- Cloud computing types: IaaS, PaaS, SaaS
- Cloud service models: public, private, hybrid
- Shared responsibility model
- Capital Expenditure (CapEx) vs Operational Expenditure (OpEx)

**Key Questions**:
- What is the difference between IaaS and PaaS?
- Which cloud model provides maximum control over infrastructure?
- What cost model does Azure use?

**Study Focus**: Understand service models and cloud benefits (scalability, reliability, cost efficiency).

### Domain 2: Azure Architecture and Services (35-40%)

**Core Topics**:
- Azure regions, availability zones, resource hierarchy
- Compute services: VMs, App Service, Functions, AKS, ACI
- Storage services: Blob, Files, Tables, Queues, Data Lake
- Database services: SQL, Cosmos DB, PostgreSQL, MySQL
- Networking: VNet, Load Balancer, Application Gateway, Content Delivery Network (CDN)

**Key Questions**:
- When should you use App Service vs Azure Functions?
- What is the difference between availability zones and regions?
- Which storage service supports structured, relational data?
- How does Azure Cosmos DB differ from SQL Database?

**Study Focus**: Know the characteristics, use cases, and differences between services.

### Domain 3: Azure Management and Governance (20-25%)

**Topics**:
- Azure Resource Manager (ARM)
- Resource groups, subscriptions, management groups
- Azure Policy and Azure Blueprints
- Azure Monitor and Application Insights
- Cost management and budgeting

**Key Questions**:
- What is the purpose of resource groups?
- How do Azure Policies enforce compliance?
- What billing boundary is a subscription?
- How does Azure Monitor collect metrics?

**Study Focus**: Understand organizational hierarchy, governance tools, and cost management.

### Domain 4: Security and Compliance (10-15%)

**Topics**:
- Azure Entra ID (Azure AD)
- Role-Based Access Control (RBAC)
- Encryption at rest and in transit
- Azure Key Vault
- Compliance and data residency

**Key Questions**:
- What is the purpose of RBAC?
- Which service stores encryption keys in Azure?
- How does Azure ensure data residency?

**Study Focus**: Core security concepts and identity management.

### AZ-900 Practice Questions

**Question 1**: Which service provides fully managed Kubernetes container orchestration in Azure?
- A. Azure Virtual Machines
- B. Azure Container Instances
- C. Azure Kubernetes Service
- D. Azure App Service

**Correct Answer**: C (AKS is managed Kubernetes)

**Question 2**: What is a key benefit of cloud computing compared to on-premises infrastructure?
- A. Predictable upfront costs
- B. Unlimited server capacity
- C. No need for security management
- D. Pay-as-you-go cost model

**Correct Answer**: D (OpEx vs CapEx)

**Question 3**: In Azure's shared responsibility model, who is responsible for patching the operating system on a virtual machine?
- A. Microsoft
- B. The customer
- C. A third-party vendor
- D. Automatically handled by the hypervisor

**Correct Answer**: B (Customer manages OS for VMs)

**Question 4**: Which Azure service is best suited for serverless compute triggered by HTTP requests?
- A. Azure Virtual Machines
- B. Azure Functions
- C. Azure App Service
- D. Azure Container Instances

**Correct Answer**: B (Functions for event-driven, serverless)

**Question 5**: What is the primary purpose of availability zones in Azure?
- A. Reduce latency for users
- B. Provide isolation from failures
- C. Store redundant backups
- D. Improve network bandwidth

**Correct Answer**: B (AZs provide fault tolerance)

**Question 6**: Which service is suitable for hosting a relational database in Azure?
- A. Azure Blob Storage
- B. Azure SQL Database
- C. Azure Queue Storage
- D. Azure Table Storage

**Correct Answer**: B (SQL Database for relational data)

**Question 7**: What does RBAC stand for in Azure?
- A. Resource-Based Authentication Control
- B. Resource Backup and Consistency
- C. Role-Based Access Control
- D. Redundancy-Based Architecture Configuration

**Correct Answer**: C (RBAC)

**Question 8**: How long does an Azure free account provide a 200 USD credit?
- A. 1 month
- B. 3 months
- C. 6 months
- D. 12 months

**Correct Answer**: A (30 days for free account credits)

**Question 9**: Which Azure service is used for centralized management of secrets and encryption keys?
- A. Azure DevOps
- B. Azure Key Vault
- C. Azure Monitor
- D. Azure Policy

**Correct Answer**: B (Key Vault for secrets management)

**Question 10**: What is the primary difference between a public cloud and a hybrid cloud?
- A. Public clouds are always more expensive
- B. Hybrid clouds combine on-premises and cloud resources
- C. Public clouds only support IaaS
- D. Hybrid clouds provide better security

**Correct Answer**: B (Hybrid combines on-premises and public cloud)

**Question 11**: Which Azure feature enables you to enforce organizational standards across resources?
- A. Azure Policy
- B. Azure Blueprint
- C. Resource Locks
- D. Management Groups

**Correct Answer**: A (Azure Policy enforces compliance)

**Question 12**: What is the Azure resource container that allows you to organize and manage resources?
- A. Subscription
- B. Resource Group
- C. Management Group
- D. Tenant

**Correct Answer**: B (Resource groups contain resources)

**Question 13**: Which deployment model means you only pay for resources you use?
- A. Reserved Instance
- B. Pay-as-you-go
- C. Spot VM
- D. Yearly commitment

**Correct Answer**: B (Consumption-based pricing)

**Question 14**: What is the purpose of Azure availability zones?
- A. To reduce licensing costs
- B. To improve application performance globally
- C. To protect against data center failures
- D. To simplify management

**Correct Answer**: C (AZs provide fault tolerance)

**Question 15**: Which service provides Platform as a Service (PaaS) compute in Azure?
- A. Virtual Machines
- B. Azure Functions
- C. Azure App Service
- D. Both B and C

**Correct Answer**: D (Both Functions and App Service are PaaS)

---

## AZ-104: Azure Administrator

### Exam Overview

**Duration**: 120 minutes

**Questions**: 40-55 (multiple choice, multiple select, labs)

**Pass Score**: 700/1000

**Cost**: USD 165

**Target Audience**: IT professionals managing Azure infrastructure and resources

### Exam Domains and Weights

| Domain | Weight |
|---|---|
| Manage Azure identities and governance | 25-30% |
| Implement and manage storage | 15-20% |
| Deploy and manage Azure compute resources | 20-25% |
| Configure and manage virtual networking | 20-25% |
| Monitor and maintain Azure resources | 10-15% |

### Domain 1: Identities and Governance (25-30%)

**Topics**:
- Azure Entra ID (Azure AD)
- Users, groups, and role assignments
- RBAC and role definitions
- Subscriptions and management groups
- Azure Policy and compliance

**Key Concepts**:
- Create and manage user accounts
- Configure group membership
- Assign roles at subscription and resource group scope
- Implement Azure Policy
- Configure management groups

### Domain 2: Storage (15-20%)

**Topics**:
- Storage accounts (types, performance, replication)
- Blob, Files, Tables, Queues storage
- Storage lifecycle management and tiers
- Access keys and Shared Access Signatures (SAS)
- Network integration (service endpoints, private endpoints)

**Key Concepts**:
- Choose appropriate storage account types
- Configure storage tiers and lifecycle policies
- Generate SAS tokens for temporary access
- Implement network restrictions

### Domain 3: Compute (20-25%)

**Topics**:
- Virtual Machines (creation, sizing, disks)
- Scale sets and load balancers
- App Service and Web Apps
- Container services (ACI, AKS)
- Azure Functions
- Backup and disaster recovery

**Key Concepts**:
- Deploy and configure VMs
- Configure auto-scaling
- Manage disks and snapshots
- Configure backup and recovery

### Domain 4: Virtual Networking (20-25%)

**Topics**:
- Virtual Networks and subnets
- Network Security Groups and firewall rules
- Load Balancers and Application Gateways
- VPN and ExpressRoute
- DNS and content delivery

**Key Concepts**:
- Design VNet topology
- Configure NSG rules
- Implement load balancing
- Establish hybrid connectivity

### Domain 5: Monitoring (10-15%)

**Topics**:
- Azure Monitor and Log Analytics
- Metrics and alerts
- Application Insights
- Diagnostic logging
- Cost analysis

**Key Concepts**:
- Configure monitoring and alerts
- Analyze performance metrics
- Investigate issues using logs
- Optimize costs

### AZ-104 Practice Questions

**Question 1**: You need to assign a role to a user for managing resources in a specific resource group. Which should you use?
- A. Azure Policy
- B. Role-Based Access Control (RBAC)
- C. Network Security Groups
- D. Azure Blueprints

**Correct Answer**: B (RBAC assigns roles)

**Question 2**: Which storage account replication option provides the highest redundancy across regions?
- A. Locally Redundant Storage (LRS)
- B. Zone Redundant Storage (ZRS)
- C. Geo-Redundant Storage (GRS)
- D. Read-Access Geo-Redundant Storage (RA-GRS)

**Correct Answer**: D (RA-GRS provides geographic redundancy with read access)

**Question 3**: You want to move a VM from one subscription to another. Which is the recommended approach?
- A. Create a snapshot and restore to new subscription
- B. Use managed disks export/import
- C. Use Azure Resource Mover
- D. Recreate the VM manually

**Correct Answer**: C (Azure Resource Mover simplifies cross-subscription moves)

**Question 4**: How should you restrict outbound traffic from a subnet?
- A. Network Security Group outbound rule
- B. Azure Firewall
- C. Application Gateway
- D. Load Balancer

**Correct Answer**: A (NSG controls layer 4 traffic)

**Question 5**: What is the maximum storage capacity for a standard storage account?
- A. 1 TB
- B. 10 TB
- C. 500 TB (unlimited scalability)
- D. 100 TB

**Correct Answer**: C (Azure Storage provides unlimited scalability)

**Question 6**: You need to encrypt data at rest in a storage account. Which encryption method is applied by default?
- A. Azure Disk Encryption
- B. Storage Service Encryption (SSE)
- C. Transport Layer Security (TLS)
- D. BitLocker

**Correct Answer**: B (SSE automatically encrypts storage data)

**Question 7**: Which tool should you use to evaluate compliance with Azure best practices and recommendations?
- A. Azure Policy
- B. Azure Advisor
- C. Azure Security Center
- D. Azure Monitor

**Correct Answer**: B (Azure Advisor provides recommendations)

**Question 8**: You want to ensure a web app is highly available across regions. Which service should you use?
- A. Single App Service in one region
- B. Traffic Manager with App Services in multiple regions
- C. Load Balancer
- D. Azure Front Door

**Correct Answer**: D (Front Door enables global load balancing)

**Question 9**: How should you manage secrets like database passwords in Azure?
- A. Store in application configuration
- B. Use Azure Key Vault
- C. Store in connection strings
- D. Use environment variables

**Correct Answer**: B (Key Vault securely stores secrets)

**Question 10**: What is the purpose of a Network Security Group (NSG)?
- A. Manage network performance
- B. Filter traffic to/from resources
- C. Load balance traffic
- D. Replicate data across regions

**Correct Answer**: B (NSGs act as firewalls)

---

## Study Tips and Resources

### Before the Exam

1. **Understand Core Concepts**: Don't just memorize; understand why each service exists
2. **Hands-on Practice**: Deploy actual resources in Azure using free tier account
3. **Review Official Documentation**: Microsoft Learn provides free, comprehensive training
4. **Take Practice Exams**: Use MeasureUp, Whizlabs, or exam simulator
5. **Join Study Groups**: Community forums, Discord servers, Reddit provide peer support
6. **Create Flashcards**: Review key terms, service comparisons, pricing
7. **Practice Labs**: Microsoft Learn includes interactive labs
8. **Watch Video Courses**: Pluralsight, A Cloud Guru, Coursera offer structured learning

### Recommended Resources

**Official Microsoft**:
- Microsoft Learn: Free training modules for each exam
- Azure documentation: Authoritative reference
- Azure free account: Hands-on practice environment

**Third-Party**:
- Pluralsight Azure learning paths
- A Cloud Guru Azure courses
- Whizlabs practice exams
- MeasureUp official exam simulator

### Test-Taking Strategies

1. **Read Questions Carefully**: Note keywords like "best", "most", "all", "none"
2. **Eliminate Obviously Wrong Answers**: Narrows choices
3. **Answer Easy Questions First**: Build momentum and manage time
4. **Flag Difficult Questions**: Return to them after easier ones
5. **Review Before Submitting**: Check multiple-select answers
6. **Don't Second-Guess**: Your first instinct is often correct
7. **Manage Time**: 1.5-2 minutes per question on average
8. **Stay Calm**: Pacing and breathing helps with test anxiety

### Common Exam Pitfalls

- **Confusing similar services**: Study service comparison tables
- **Not reading lab instructions carefully**: Follow steps precisely
- **Misunderstanding pricing models**: Review CapEx vs OpEx, billing boundaries
- **Forgetting hands-on experience**: Labs are critical; don't skip them
- **Studying outdated content**: Verify resource publication dates
- **Ignoring weak areas**: Focus extra time on challenging topics

## Key Takeaways

- Azure certifications validate cloud skills and advance career opportunities
- AZ-900 covers cloud fundamentals and basic Azure concepts
- AZ-104 focuses on infrastructure management and administration
- Higher-level certifications (AZ-204, AZ-400, AZ-305) require deeper expertise
- Hands-on practice with Azure services is essential
- Official Microsoft Learn provides free, authoritative study material
- Practice exams help identify weak areas before the real exam
- Success requires understanding concepts, not just memorization
