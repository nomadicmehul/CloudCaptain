---
title: Azure Security & Identity
sidebar_label: Security & Identity
sidebar_position: 2
---

# Azure Security & Identity

Security and identity are foundational pillars of any cloud architecture. Azure provides comprehensive tools and services for securing your resources, managing identities, and controlling access.

## Azure Active Directory / Entra ID

Azure Entra ID (formerly Azure AD) is Microsoft's cloud identity and access management service.

### Users and Groups

**Users**: Individual accounts with credentials for accessing Azure and applications.
- Cloud-only accounts: Created directly in Entra ID
- Synchronized accounts: Synced from on-premises Active Directory via Azure AD Connect
- Guest accounts: External users with limited access

**Groups**: Collections of users for simplified access management.
- Security groups: Used for assigning permissions
- Distribution groups: For email communication
- Microsoft 365 groups: Unified collaboration experience

### Roles and Role-Based Access Control (RBAC)

RBAC grants permissions to perform actions on Azure resources. Three key role types:

**Built-in Roles**:
- **Owner**: Full access, can manage access to resources
- **Contributor**: Can create and manage all resources but cannot grant access
- **Reader**: View-only access to all resources
- **Specific roles**: Scoped to services (Virtual Machine Contributor, App Service Contributor, etc.)

**Custom Roles**: Define permissions tailored to organizational needs.

Role assignments specify: **Who** (user, group, service principal), **What role**, **Where** (scope: management group, subscription, resource group, resource).

Example assignment: Assign "App Service Contributor" role to a development team on a specific resource group.

### Multi-Factor Authentication (MFA)

MFA requires two or more verification methods:
1. Something you know (password)
2. Something you have (phone, authenticator app)
3. Something you are (biometric)

MFA methods in Azure:
- **Microsoft Authenticator app**: Push notifications, OATH codes
- **Phone call**: Voice verification
- **SMS text message**: Code via text
- **Hardware token**: Third-party authenticator devices

MFA is critical for protecting accounts from compromise.

### Conditional Access

Conditional Access policies evaluate risk and enforce authentication requirements based on conditions:

**Conditions evaluated**:
- User or group membership
- Device state (compliant, trusted)
- Location and IP risk
- Application being accessed
- Sign-in risk (unusual activity detected)

**Enforcement actions**:
- Require MFA
- Require password change
- Require compliant device
- Block access

Example policy: "Require MFA for all users accessing sensitive applications outside the corporate network."

## Role-Based Access Control (RBAC)

RBAC is the primary authorization mechanism in Azure. Principles:

**Principle of Least Privilege**: Grant only the minimum permissions needed.

**Separation of Duties**: Different roles for different functions.

**Scope Hierarchy**: Inherited from parent to child scopes.
```
Management Group
  └─ Subscription
      └─ Resource Group
          └─ Resource
```

**Common RBAC Scenarios**:
- Grant subscription owner to team lead
- Grant App Service Contributor to developers on a resource group
- Grant Storage Blob Data Contributor to a data pipeline service principal
- Grant Reader role for audit and compliance teams

**Service Principals**: Applications that need programmatic access use service principals with assigned roles.

## Azure Key Vault

Azure Key Vault is a secure, centralized service for managing secrets, keys, and certificates.

### Features

**Secrets Management**:
- Store connection strings, API keys, passwords
- Automatic rotation policies
- Access auditing and logging

**Cryptographic Keys**:
- Create or import keys
- Hardware Security Module (HSM) backed options
- Key operations: encrypt, decrypt, sign, verify

**Certificates**:
- Automatic renewal
- Integration with certificate authorities
- Support for custom certificates

**Access Control**:
- RBAC for service-level permissions
- Access policies for granular secret/key/certificate permissions
- Managed identities for Azure service access

### Usage Pattern

```bash
az keyvault create --name myKeyVault --resource-group myRG --location eastus
az keyvault secret set --vault-name myKeyVault --name dbPassword --value mySecurePassword
az keyvault secret show --vault-name myKeyVault --name dbPassword
```

Applications retrieve secrets at runtime using managed identity, eliminating hardcoded credentials.

## Network Security

### Network Security Groups (NSGs)

NSGs act as virtual firewalls controlling inbound and outbound traffic.

**NSG Rules**:
- **Priority**: Lower numbers evaluated first (100-4096)
- **Direction**: Inbound or outbound
- **Access**: Allow or Deny
- **Protocol**: TCP, UDP, or wildcard
- **Port Range**: Single port or range
- **Source/Destination**: IP, CIDR, service tag, or application security group

Example inbound rule:
- Priority: 100
- Direction: Inbound
- Access: Allow
- Protocol: TCP
- Port Range: 80, 443
- Source: Internet (0.0.0.0/0)

NSGs can be associated with subnets (affects all resources) or individual network interfaces.

### Service Endpoints

Service endpoints extend Azure virtual network identity to Azure services:
- Allow VNet to connect securely to services without internet routing
- Services: Azure Storage, SQL Database, Cosmos DB, Key Vault, etc.
- Restricts service access to authorized VNets only
- No data traverses the internet

Example: Configure Storage Account to accept traffic only from specific VNet service endpoint.

### Private Endpoints

Private endpoints establish private connections to Azure services via Azure backbone network:
- Private IP address in VNet for the service
- No internet exposure
- DNS integration for transparent connection
- Services: Most Azure services support private endpoints

Benefits over service endpoints:
- More granular control
- Works across subscriptions and tenants
- Better security isolation

### Azure Firewall

Azure Firewall is a managed, stateful firewall service:

**Capabilities**:
- Centralized protection for hub-and-spoke networks
- Network and application-level filtering
- Threat intelligence-based filtering
- Built-in high availability and scalability
- SNAT (Source Network Address Translation) for outbound connections

**Rules**:
- Network rules: Layer 4 filtering (IP, port, protocol)
- Application rules: Layer 7 filtering (FQDN, URLs, protocols)

Use case: Filter outbound traffic from multiple VNets through a central firewall.

## Encryption

### Encryption at Rest

Data stored on disk or in storage is encrypted using:

**Azure Storage Service Encryption (SSE)**:
- Automatic encryption of data in Blob, Files, Table, Queue storage
- Uses 256-bit AES encryption
- Keys managed by Microsoft (default) or customer (BYOK in Key Vault)

**SQL Database Transparent Data Encryption (TDE)**:
- Encrypts database files automatically
- Prevents unauthorized disk access
- Key management via Azure Key Vault

**Disk Encryption**:
- Azure Disk Encryption: Encrypts OS and data disks on VMs using BitLocker or DM-Crypt
- Supports key management via Key Vault

**Cosmos DB Encryption**:
- Automatic encryption of data with Microsoft-managed keys
- Customer-managed keys (CMK) option for additional control

### Encryption in Transit

Data in motion is protected using:

**TLS/SSL**:
- HTTPS for web traffic
- TLS for database connections
- Enforced by default for most Azure services
- Minimum TLS 1.2 recommended

**VPN/ExpressRoute**:
- Encrypted tunnels for hybrid connectivity
- Site-to-site and point-to-site VPNs
- ExpressRoute: Private, dedicated connections

**Service-specific encryption**:
- Storage: Shared Access Signature (SAS) tokens with HTTPS
- Messaging: Message encryption in Service Bus and Event Hubs
- Database: Connection strings enforce encrypted connections

## Azure Sentinel

Azure Sentinel is a cloud-native Security Information and Event Management (SIEM) service:

**Capabilities**:
- Collect data from 1000+ sources (logs, events, alerts)
- AI-powered threat detection and response
- Automated incident response playbooks
- Investigation and hunting tools
- Compliance and security monitoring

**Data Sources**:
- Azure services (VMs, App Service, SQL, Storage, etc.)
- On-premises systems via Log Analytics agents
- Third-party solutions (AWS, Google Cloud, Okta, etc.)
- Syslog and CEF formats

**Use Cases**:
- Detect and respond to security threats
- Investigate security incidents
- Monitor compliance requirements
- Threat hunting across organizational data

## Azure Defender / Security Center

Azure Defender (formerly Azure Security Center) provides threat protection across Azure resources:

**Features**:
- **Secure Score**: Benchmark for security posture (0-800 points)
- **Recommendations**: Prioritized security improvements
- **Threat Protection**: Detects and alerts on suspicious activity
- **Compliance**: Monitor compliance with standards (CIS, NIST, PCI-DSS)
- **Just-in-Time (JIT) VM Access**: Time-limited port access for administrative tasks

**Defender Plans** (per resource type):
- Defender for Servers: VMs, on-premises servers
- Defender for App Service: Web and API apps
- Defender for SQL: SQL databases and servers
- Defender for Storage: Blob Storage, Files, Data Lake
- Defender for Cosmos DB
- Defender for Key Vault
- Defender for Resource Manager
- Defender for DNS

Enables EDR (Endpoint Detection and Response) and advanced threat analytics.

## Azure DDoS Protection

Azure DDoS Protection shields applications from Distributed Denial of Service attacks:

### Protection Tiers

**Basic (Standard)**: Automatic protection for all Azure customers.
- No additional cost
- Protects against common network-layer attacks
- Always-on monitoring

**Standard**: Enhanced DDoS protection for applications.
- Additional cost per month
- Protects against volumetric and protocol attacks
- DDoS rapid response (DRR) support
- Cost protection guarantees
- Real-time attack metrics and analytics

Configuration:
- Apply to public IPs of resources (load balancers, App Gateways, VMs)
- Requires Network Watcher monitoring

## Zero Trust Security Model

Azure supports Zero Trust principles:

**Core Assumptions**:
- Never trust, always verify
- Assume breach occurred
- Minimize blast radius

**Implementation**:
- **Identity**: Verify every user (Entra ID, MFA, Conditional Access)
- **Device**: Ensure device compliance and health
- **Network**: Microsegmentation with NSGs and firewalls
- **Encryption**: Encrypt all data in transit and at rest
- **Visibility**: Monitor and log all access and activity
- **Applications**: Least privilege access to applications

Azure services supporting Zero Trust:
- Azure Entra ID with Conditional Access
- Azure Defender for comprehensive visibility
- Azure Firewall for network segmentation
- Key Vault for secrets management
- Azure Sentinel for incident response

## Azure Security Best Practices Checklist

Use this checklist to evaluate your Azure security posture:

**Identity and Access**
- [ ] Enable MFA for all user accounts
- [ ] Implement Conditional Access policies for critical applications
- [ ] Use managed identities for Azure service authentication
- [ ] Regularly audit and remove unused permissions
- [ ] Enable Entra ID password protection and Smart Lockout
- [ ] Use service principals with least privilege roles
- [ ] Enable Entra ID sign-in and audit logging

**Network Security**
- [ ] Configure NSGs with explicit allow rules (whitelist approach)
- [ ] Enable Azure Firewall for centralized protection
- [ ] Use service endpoints or private endpoints for Azure service access
- [ ] Implement network segmentation with subnets and user-defined routes
- [ ] Enable Azure DDoS Protection Standard for critical applications
- [ ] Use Web Application Firewall (WAF) for web applications
- [ ] Restrict RDP/SSH access to specific IPs (no 0.0.0.0/0)

**Data Protection**
- [ ] Enable encryption at rest for all storage accounts and databases
- [ ] Enforce TLS 1.2+ for data in transit
- [ ] Use Azure Key Vault for secrets and encryption keys
- [ ] Enable SQL Database Transparent Data Encryption (TDE)
- [ ] Use Managed Disks with encryption enabled
- [ ] Configure backup and disaster recovery
- [ ] Implement data retention and archival policies

**Monitoring and Compliance**
- [ ] Enable Azure Defender on all subscriptions
- [ ] Configure Azure Sentinel for threat detection
- [ ] Enable audit logging for all resources
- [ ] Set up alerts for suspicious activity
- [ ] Regularly review and address security recommendations
- [ ] Enable compliance monitoring (CIS, NIST, PCI-DSS)
- [ ] Conduct regular security assessments and penetration testing

**Governance**
- [ ] Use Azure Policies to enforce organizational standards
- [ ] Implement resource tagging for cost and compliance tracking
- [ ] Use Management Groups for multi-subscription governance
- [ ] Regularly review and update RBAC assignments
- [ ] Enable subscription-level diagnostic logging
- [ ] Implement budget and cost alerts

**Application Security**
- [ ] Use Azure App Configuration for secure configuration management
- [ ] Implement input validation and output encoding
- [ ] Use SQL parameterized queries (prevent SQL injection)
- [ ] Enable Web Application Firewall (WAF) on Application Gateway
- [ ] Keep software dependencies updated
- [ ] Scan container images for vulnerabilities
- [ ] Enable API authentication and authorization

## Key Takeaways

- Entra ID is the foundation for identity and access management in Azure
- RBAC enables fine-grained access control with role assignments at various scopes
- MFA and Conditional Access provide defense against identity compromise
- Azure Key Vault centralizes secrets, keys, and certificate management
- Network Security Groups and Azure Firewall protect networks from unauthorized access
- Encryption at rest and in transit protects data confidentiality
- Azure Defender and Sentinel provide comprehensive threat detection and response
- Zero Trust principles guide secure architecture in Azure
- Regular security assessments and compliance monitoring are essential
