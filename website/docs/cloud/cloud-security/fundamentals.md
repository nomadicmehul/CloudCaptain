---
title: "Cloud Security Fundamentals"
sidebar_label: "Fundamentals"
description: "Shared responsibility model, IAM, encryption, network security, compliance (SOC2, ISO 27001, HIPAA), and cloud security posture management"
sidebar_position: 1
---

# Cloud Security Fundamentals

## Cloud Security Overview

Cloud security is a shared responsibility between cloud providers and customers. Understanding this model is critical for building secure cloud deployments.

### The Shared Responsibility Model

The shared responsibility model defines who is responsible for security in different layers of cloud computing.

```
┌────────────────────────────────────────────────────────┐
│ ON-PREMISES (Customer Responsible for Everything)     │
├────────────────────────────────────────────────────────┤
│ ┌──────────────────────────────────────────────────┐  │
│ │ IaaS (Customer Responsible for Marked Items)    │  │
│ │ ┌────────────────────────────────────────────┐  │  │
│ │ │ PaaS (Less Customer Responsibility)        │  │  │
│ │ │ ┌──────────────────────────────────────┐  │  │  │
│ │ │ │ SaaS (Least Customer Responsibility)│  │  │  │
│ │ │ │ ┌────────────────────────────────┐  │  │  │  │
│ │ │ │ │ ☁  Provider Responsibility     │  │  │  │  │
│ │ │ │ │ Physical security             │  │  │  │  │
│ │ │ │ │ Hardware & firmware           │  │  │  │  │
│ │ │ │ │ Infrastructure encryption     │  │  │  │  │
│ │ │ │ └────────────────────────────────┘  │  │  │  │
│ │ │ │ CUSTOMER: Applications, Data  │  │  │  │
│ │ │ │ APIs, Runtime, OS             │  │  │  │
│ │ │ │ Network security, IAM         │  │  │  │
│ │ │ └──────────────────────────────────────┘  │  │  │
│ │ │ CUSTOMER: Applications, Data  │  │  │
│ │ │ APIs, Databases, Runtime      │  │  │
│ │ │ IAM, Network security         │  │  │
│ │ └────────────────────────────────────────────┘  │  │
│ │ CUSTOMER: Applications, Data  │  │
│ │ Identity, Access Management   │  │
│ └──────────────────────────────────────────────────┘  │
│ CUSTOMER: Everything (Applications, Data, Identity)  │
└────────────────────────────────────────────────────────┘
```

### What Each Provider Is Responsible For

| Component | IaaS (AWS EC2) | PaaS (App Service) | SaaS (Office 365) |
|-----------|---|---|---|
| **Physical Security** | Provider | Provider | Provider |
| **Network** | Both | Provider | Provider |
| **Host OS** | Provider | Provider | Provider |
| **Application OS** | Customer | Provider | Provider |
| **Applications** | Customer | Customer | Provider |
| **Data** | Customer | Customer | Customer |
| **Identity/Access** | Customer | Customer | Both |

### Critical Customer Responsibilities

**ALWAYS your responsibility:**
1. Data encryption (encryption keys, encryption at rest/transit)
2. Identity and access management (IAM policies, user provisioning)
3. Application security (code, dependencies, configuration)
4. Network security (security groups, firewall rules)
5. Compliance and auditing (logs, audit trails)
6. Incident response and disaster recovery

---

## Identity and Access Management (IAM)

IAM is foundational to cloud security. It controls who (identity) can do what (action) on which resources (resources).

### Core IAM Concepts

**1. Users**
   - Individual people who need access to cloud resources
   - Each user has a unique identity and credentials

**2. Roles**
   - Collections of permissions (policies)
   - Not assigned to users directly; users assume roles
   - Example: "EC2AdminRole", "DataAnalystRole"

**3. Policies**
   - Explicit rules defining what actions are allowed/denied
   - Written in JSON format
   - Attached to users, roles, or groups

**4. Groups**
   - Collections of users with similar permissions
   - Example: "Engineers", "DataAnalysts", "Interns"

### IAM Best Practices

**1. Principle of Least Privilege**
   ```
   DON'T: grant admin access to everyone
   grant arn:aws:iam::123456789:root

   DO: grant specific permissions needed
   {
     "Effect": "Allow",
     "Action": [
       "s3:GetObject",
       "s3:ListBucket"
     ],
     "Resource": "arn:aws:s3:::my-bucket/*"
   }
   ```

**2. Use Roles, Not Long-Term Keys**
   ```
   DON'T: Create access key for human user
   ├─ Keys never rotate automatically
   ├─ If leaked, stays active until manually deleted
   └─ Hard to track who did what

   DO: Use role assumption + temporary credentials
   ├─ Credentials expire automatically (15 min - 12 hrs)
   ├─ Each request is audited
   ├─ Easy to revoke (just delete role)
   └─ Clear audit trail
   ```

**3. Enable Multi-Factor Authentication (MFA)**
   ```
   DON'T: Password-only authentication
   └─ If password is compromised, account is compromised

   DO: MFA (something you know + something you have)
   ├─ Password + authenticator app (Google Authenticator)
   ├─ Password + hardware key (YubiKey)
   ├─ Makes account much harder to compromise
   └─ Should be mandatory for admin users
   ```

**4. Regular Access Reviews**
   ```
   Process:
   1. Quarterly: list all users and their permissions
   2. Review: do they still need these permissions?
   3. Remove: unused access
   4. Document: justification for remaining access

   Tools: AWS Access Analyzer, Azure Access Reviews
   ```

**5. Separate AWS/Azure/GCP Accounts**
   ```
   DON'T: All environments in one account
   └─ If compromised, everything is compromised

   DO: Separate accounts per environment
   ├─ Production account (restricted)
   ├─ Staging account (moderate access)
   ├─ Development account (developer access)
   ├─ Management account (billing, organization)
   └─ Security account (logging, monitoring)
   ```

### IAM Anti-Patterns to Avoid

| Anti-Pattern | Problem | Solution |
|--------------|---------|----------|
| **Root account access key** | Never expires, super-privileged | Disable immediately, use IAM roles |
| **Shared credentials** | No audit trail, hard to revoke | Individual user accounts with roles |
| **Standing privilege** | Permissions never removed | Schedule regular access reviews |
| **Overly permissive policies** | Violates least privilege | Use specific actions and resources |
| **No MFA on admin** | Easy to compromise | Mandatory MFA for all admin users |

---

## Encryption

Encryption protects data from being read by unauthorized parties. Two types: at-rest and in-transit.

### Encryption at Rest

Data stored on disks, databases, or storage systems should be encrypted.

**Why It Matters:**
- Physical theft of hard drives
- Decommissioned hardware not properly wiped
- Insider threats
- Regulatory requirements (GDPR, HIPAA, PCI-DSS)

**Implementation:**

| Storage Type | AWS | Azure | GCP | Default |
|--------------|-----|-------|-----|---------|
| **Compute (volumes)** | EBS Encryption | Disk Encryption | Persistent Disk | ❌ Optional |
| **Object Storage** | S3 SSE | Blob Storage Encryption | Cloud Storage | ✓ Enabled by default |
| **Database** | RDS Encryption | Transparent Data Encryption | Cloud SQL | ✓ Enabled by default |
| **Backups** | S3 SSE | Storage Encryption | Storage Encryption | ✓ Enabled by default |

**Encryption Options:**

1. **Server-Side Encryption (SSE)** - Provider manages keys
   ```
   Pros: Simple, free, automatic
   Cons: Provider has access to encryption keys

   AWS Example:
   aws s3 cp file.txt s3://bucket/ \
     --sse AES256  # or --sse aws:kms
   ```

2. **Customer-Managed Key Encryption (CMK)** - You manage keys
   ```
   Pros: You control keys, better compliance
   Cons: Key management burden

   AWS Example:
   aws s3 cp file.txt s3://bucket/ \
     --sse aws:kms \
     --sse-kms-key-id arn:aws:kms:...
   ```

3. **Client-Side Encryption** - You encrypt before sending
   ```
   Pros: Maximum control, provider can't read data
   Cons: Complex, key management responsibility

   Python Example:
   from cryptography.fernet import Fernet
   key = Fernet.generate_key()
   cipher = Fernet(key)
   encrypted_data = cipher.encrypt(data)
   ```

### Encryption in Transit

Data moving between systems should be encrypted (HTTPS/TLS).

**Implementation:**

```
DON'T: Unencrypted HTTP
GET http://api.example.com/user/123
├─ Credentials visible in transit
├─ Data can be intercepted
└─ Man-in-the-middle attacks possible

DO: Encrypted HTTPS/TLS
GET https://api.example.com/user/123
├─ Credentials encrypted
├─ Data encrypted
├─ Mutual authentication (server proves identity)
└─ Protects from MITM attacks
```

**Best Practices:**

1. **Enforce HTTPS Everywhere**
   ```hcl
   # Terraform: Force HTTPS on S3 bucket
   resource "aws_s3_bucket_policy" "enforce_https" {
     bucket = aws_s3_bucket.main.id
     policy = jsonencode({
       Statement = [{
         Effect = "Deny"
         Principal = "*"
         Action = "s3:*"
         Resource = [
           aws_s3_bucket.main.arn,
           "${aws_s3_bucket.main.arn}/*"
         ]
         Condition = {
           Bool = {
             "aws:SecureTransport" = "false"
           }
         }
       }]
     })
   }
   ```

2. **Use TLS 1.2 or Higher**
   ```
   DON'T: SSL 3.0, TLS 1.0, TLS 1.1 (vulnerable to attacks)
   DO:    TLS 1.2 (minimum), TLS 1.3 (recommended)

   Verification:
   openssl s_client -connect example.com:443
   # Look for "TLSv1.2" or "TLSv1.3"
   ```

3. **Use Strong Certificates**
   - Use certificates from trusted CAs (Let's Encrypt, AWS Certificate Manager)
   - Implement certificate pinning for critical services
   - Automate certificate rotation (expires after 90 days)

4. **Encrypt Service-to-Service Communication**
   ```
   ┌────────────────┐  HTTPS with mTLS  ┌────────────────┐
   │  Web Service   │ ◄────────────────► │  API Service   │
   │ (Client Cert)  │                    │ (Server Cert)  │
   └────────────────┘                    └────────────────┘

   Benefits:
   - Both parties authenticate each other
   - Prevents MITM attacks
   - Ensures only authorized services communicate
   ```

---

## Network Security

Network security controls what traffic is allowed in and out of your cloud resources.

### Network Architecture

```
┌─────────────────────────────────────────────────────┐
│  Internet                                           │
└─────────────────────────────────────────────────────┘
                    ↓ (public traffic)
┌─────────────────────────────────────────────────────┐
│  Load Balancer (public)                             │
│  - Distributes traffic                              │
│  - Rate limiting, DDoS protection                   │
└─────────────────────────────────────────────────────┘
                    ↓ (private traffic)
┌─────────────────────────────────────────────────────┐
│  Application Subnet (internal)                      │
│  ├─ Web App 1, Web App 2, Web App 3                │
│  │ (no direct internet access)                     │
│  └─ Only accessible from load balancer             │
└─────────────────────────────────────────────────────┘
                    ↓ (private, restricted)
┌─────────────────────────────────────────────────────┐
│  Database Subnet (restricted)                       │
│  ├─ Database 1, Database 2                         │
│  │ (no internet access, restricted access)         │
│  └─ Only accessible from App subnet                │
└─────────────────────────────────────────────────────┘
```

### Security Groups / Network Security Groups

Control inbound and outbound traffic to resources.

**AWS Security Group Example:**

```hcl
resource "aws_security_group" "web" {
  name = "web-sg"

  # Allow HTTPS from anywhere
  ingress {
    from_port   = 443
    to_port     = 443
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }

  # Allow SSH only from office IP
  ingress {
    from_port   = 22
    to_port     = 22
    protocol    = "tcp"
    cidr_blocks = ["203.0.113.0/32"]  # Office IP
  }

  # Allow all outbound traffic
  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
}
```

### Network Segmentation

Divide network into subnets, each with different security requirements.

```
┌──────────────────────────────────────┐
│  VPC: 10.0.0.0/16                    │
├──────────────────────────────────────┤
│ Public Subnet: 10.0.1.0/24           │
│ ├─ Load Balancer                     │
│ ├─ NAT Gateway                       │
│ └─ Route to Internet Gateway         │
├──────────────────────────────────────┤
│ Private Subnet: 10.0.2.0/24          │
│ ├─ Web Servers (no internet access)  │
│ ├─ Route through NAT Gateway         │
│ └─ Security Group restricts inbound  │
├──────────────────────────────────────┤
│ Isolated Subnet: 10.0.3.0/24         │
│ ├─ Databases (no internet access)    │
│ ├─ No outbound routes                │
│ └─ Only accessible from App subnet   │
└──────────────────────────────────────┘
```

### DDoS Protection

Distribute Denial of Service attacks flood your system with traffic.

**Protection Layers:**

1. **Rate Limiting** - Limit requests per IP
2. **WAF** (Web Application Firewall) - Filter malicious requests
3. **DDoS Mitigation** - Absorb and distribute attack traffic
4. **Auto-Scaling** - Scale to handle traffic spikes

**AWS Approach:**
```
AWS Shield (free)
├─ Protects against Layer 3/4 DDoS attacks
└─ Basic protection included

AWS Shield Advanced ($3,000/month)
├─ Additional Layer 7 (application) DDoS protection
├─ AWS WAF included
├─ 24x7 DDoS Response Team (DRT)
└─ Financial protection (credits for scaling costs)
```

---

## Compliance Frameworks

Organizations must comply with regulations that mandate security controls.

### SOC 2 (Service Organization Control 2)

Attestation that a service provider has appropriate controls.

**SOC 2 Principles:**

| Principle | What It Means |
|-----------|---------------|
| **Security** | System protected from unauthorized access |
| **Availability** | System available for operation (99.9%+) |
| **Processing Integrity** | Data processed accurately and completely |
| **Confidentiality** | Confidential information protected |
| **Privacy** | Personal information handled appropriately |

**Audit Process:**
```
1. Define controls
   ├─ Access controls
   ├─ Encryption
   ├─ Monitoring
   └─ Incident response

2. Implement controls
   ├─ Enforce in code/infrastructure
   ├─ Train staff
   └─ Document procedures

3. Audit (Type II is yearly)
   ├─ External auditor reviews controls
   ├─ Tests that controls work (e.g., attempts unauthorized access)
   ├─ Generates SOC 2 report
   └─ Valid for 1 year

4. Maintain compliance
   ├─ Regular audits
   ├─ Continuous monitoring
   └─ Update controls as needed
```

### ISO 27001

International standard for information security management.

**13 Control Domains:**
1. Organization of information security
2. Governance and risk management
3. Human resource security
4. Asset management
5. Access control
6. Cryptography
7. Physical and environmental security
8. Operations security
9. Communications security
10. System acquisition, development, and maintenance
11. Supplier relationships
12. Information security incident management
13. Business continuity management

**Certification Process:**
```
Stage 1: Gap Assessment
├─ Audit current state
├─ Identify gaps
└─ Develop remediation plan

Stage 2: Implementation
├─ Deploy controls
├─ Train staff
├─ Document everything
└─ Evidence collection (6-12 months)

Stage 3: Certification Audit
├─ External auditor reviews controls
├─ Tests effectiveness
└─ Issues ISO 27001 certificate

Ongoing: Surveillance Audits
├─ Annual audits
├─ Maintain controls
├─ Demonstrate continuous improvement
└─ Recertification every 3 years
```

### HIPAA (Health Insurance Portability and Accountability Act)

Protects health information privacy in the United States.

**Who Must Comply:** Healthcare organizations, health plans, healthcare clearinghouses

**Key Requirements:**

1. **Administrative Safeguards**
   - Designate security officer
   - Conduct risk analysis
   - Implement security training
   - Report incidents

2. **Physical Safeguards**
   - Facility access controls (badges, locks)
   - Device and media controls (secure disposal)
   - Audit controls (logs)

3. **Technical Safeguards**
   - Access controls (authentication, authorization)
   - Audit controls (activity logs)
   - Integrity controls (prevent unauthorized modification)
   - Transmission security (encryption in transit)

**Cloud Considerations:**
```
AWS HIPAA Eligible Services:
├─ EC2, ECS, Fargate (compute)
├─ RDS, DynamoDB (databases)
├─ S3, EBS (storage)
├─ VPC, IAM (networking/access)
└─ CloudTrail, CloudWatch (monitoring)

NOT eligible: SNS email, SES, public S3 websites

Business Associate Agreement (BAA):
├─ Require all vendors (including cloud providers) to sign
├─ Vendors must comply with HIPAA
├─ Vendor breach = your breach
└─ Always use HIPAA-eligible services
```

### PCI-DSS (Payment Card Industry Data Security Standard)

Protects payment card data (credit cards, debit cards).

**Applies To:** Any organization processing, storing, or transmitting credit card data

**12 Core Requirements:**
1. Firewall configuration
2. Default credentials removal
3. Data protection (encryption)
4. Data access auditing (logs)
5. Malware protection
6. Security patching
7. Network access controls
8. Access identifiers (unique users)
9. Physical access restriction
10. Network monitoring (intrusion detection)
11. Security testing (penetration tests)
12. Documented security policy

**Key Practices:**
```
DON'T:
├─ Store full credit card numbers
├─ Store CVV (security code)
├─ Transmit unencrypted card data
└─ Use default vendor credentials

DO:
├─ Use tokenization (replace card # with token)
├─ Encrypt card data in transit and at rest
├─ Store minimal card information
├─ Use validated payment gateways
├─ Regular penetration testing
└─ Maintain audit logs of all access
```

---

## Cloud Security Posture Management (CSPM)

Continuous monitoring of cloud security configurations.

### Key CSPM Capabilities

**1. Misconfiguration Detection**
   ```
   Automatically detects:
   ├─ Public S3 buckets (should be private)
   ├─ Unencrypted databases
   ├─ Overly permissive security groups
   ├─ Missing backups
   ├─ Unpatched instances
   └─ Default credentials still enabled
   ```

**2. Compliance Monitoring**
   ```
   Maps cloud resources to compliance frameworks:
   ├─ CIS Benchmarks
   ├─ NIST Cybersecurity Framework
   ├─ PCI-DSS
   ├─ HIPAA
   ├─ SOC 2
   └─ ISO 27001

   Provides compliance score and remediation guidance
   ```

**3. Risk Scoring**
   ```
   Prioritizes issues by severity:
   ├─ Critical (exploitable immediately) - Fix NOW
   ├─ High (easily exploitable) - Fix within days
   ├─ Medium (requires some access) - Fix within weeks
   └─ Low (low impact) - Track and monitor
   ```

**4. Remediation Guidance**
   ```
   Example:
   ISSUE: S3 bucket is publicly accessible
   SEVERITY: Critical
   REMEDIATION:
     aws s3api put-bucket-acl --bucket my-bucket --acl private
     aws s3api put-bucket-policy --bucket my-bucket \
       --policy file://private-policy.json
   ```

### Popular CSPM Tools

| Tool | Cloud(s) | Strengths |
|------|----------|-----------|
| **Prisma Cloud** | AWS, Azure, GCP | Comprehensive, threat detection |
| **CloudSploit** | AWS, Azure, GCP | Open source, free |
| **Lacework** | AWS, Azure, GCP | API-driven, real-time |
| **CloudGuard** | AWS, Azure, GCP | IaC scanning, compliance |
| **AWS Security Hub** | AWS | Native, free tier available |

---

## Zero Trust Security

Traditional security assumes "trust the inside network." Zero Trust assumes "never trust, always verify."

### Zero Trust Principles

```
Traditional:
┌──────────────────────────────┐
│  Trusted Internal Network    │
│  ├─ Employees can access    │
│  │  anything inside         │
│  └─ External traffic filtered│
└──────────────────────────────┘

Zero Trust:
Every access request is verified:
├─ Who are you? (Identity)
├─ What are you trying to do? (Action)
├─ Are you authorized? (Authorization)
├─ Is your device secure? (Device posture)
├─ Where are you connecting from? (Location)
└─ Is this access suspicious? (Risk analysis)
```

### Zero Trust Implementation

**1. Verify Identity**
   ```
   Require:
   ├─ Username/password (something you know)
   ├─ MFA (something you have)
   ├─ Biometric or hardware key (stronger)
   └─ No shared credentials
   ```

**2. Verify Device**
   ```
   Check device before allowing access:
   ├─ Is OS patched?
   ├─ Is antivirus running?
   ├─ Is disk encrypted?
   ├─ Has device been compromised?
   └─ Is device managed by company?
   ```

**3. Verify Network**
   ```
   Require:
   ├─ VPN or corporate network (known network)
   ├─ Not from anonymous proxy or Tor
   └─ Not from high-risk country
   ```

**4. Verify Access Reason**
   ```
   Principle of Least Privilege:
   ├─ Request specific permissions (not "admin")
   ├─ Temporary access (not permanent)
   ├─ Justification required
   └─ Approval required (manager sign-off)
   ```

**5. Continuous Monitoring**
   ```
   After access granted:
   ├─ Monitor what they're accessing
   ├─ Alert on suspicious behavior
   ├─ Revoke access if risk detected
   └─ Audit all actions
   ```

---

## Practical Exercises

### Exercise 1: Implement IAM Best Practices

**Objective**: Create a secure IAM structure with least privilege

```hcl
# variables.tf
variable "environment" {
  default = "production"
}

# iam.tf
# 1. Create groups
resource "aws_iam_group" "developers" {
  name = "developers"
}

resource "aws_iam_group" "admins" {
  name = "admins"
}

# 2. Create policy (developers can only access dev resources)
resource "aws_iam_policy" "developer_policy" {
  name = "developer-policy"
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Action = [
          "ec2:DescribeInstances",
          "ec2:StartInstances",
          "ec2:StopInstances"
        ]
        Resource = "arn:aws:ec2:*:*:instance/*"
        Condition = {
          StringEquals = {
            "aws:ResourceTag/Environment" = "development"
          }
        }
      },
      {
        Effect = "Allow"
        Action = [
          "s3:GetObject",
          "s3:ListBucket"
        ]
        Resource = [
          "arn:aws:s3:::dev-bucket",
          "arn:aws:s3:::dev-bucket/*"
        ]
      }
    ]
  })
}

# 3. Attach policy to group
resource "aws_iam_group_policy_attachment" "developer_attach" {
  group      = aws_iam_group.developers.name
  policy_arn = aws_iam_policy.developer_policy.arn
}

# 4. Create users and add to groups
resource "aws_iam_user" "dev1" {
  name = "alice"
}

resource "aws_iam_group_membership" "developers" {
  name       = "developer-membership"
  users      = [aws_iam_user.dev1.name]
  group      = aws_iam_group.developers.name
}

# 5. Enable MFA requirement
resource "aws_iam_policy" "require_mfa" {
  name = "require-mfa"
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Deny"
        Action = "*"
        Resource = "*"
        Condition = {
          BoolIfExists = {
            "aws:MultiFactorAuthPresent" = "false"
          }
        }
      }
    ]
  })
}
```

**Tasks**:
1. Create 3 groups: developers, data-analysts, admins
2. Create 3 users and assign to groups
3. Create policies limiting each group to their tools
4. Require MFA for admin access
5. Document the IAM structure

### Exercise 2: Enable Encryption

**Objective**: Encrypt an S3 bucket and RDS database

```bash
# 1. Create KMS key for encryption
aws kms create-key \
  --description "Key for S3 bucket encryption"

# 2. Create S3 bucket with encryption
aws s3api create-bucket \
  --bucket my-encrypted-bucket \
  --region us-east-1

# 3. Enable default encryption
aws s3api put-bucket-encryption \
  --bucket my-encrypted-bucket \
  --server-side-encryption-configuration '{
    "Rules": [{
      "ApplyServerSideEncryptionByDefault": {
        "SSEAlgorithm": "aws:kms",
        "KMSMasterKeyID": "arn:aws:kms:us-east-1:123456789:key/..."
      }
    }]
  }'

# 4. Create RDS with encryption
aws rds create-db-instance \
  --db-instance-identifier mydb \
  --db-instance-class db.t3.micro \
  --engine postgres \
  --master-username admin \
  --master-user-password <password> \
  --storage-encrypted \
  --kms-key-id arn:aws:kms:us-east-1:123456789:key/...

# 5. Verify encryption
aws s3api get-bucket-encryption --bucket my-encrypted-bucket
aws rds describe-db-instances --db-instance-identifier mydb \
  --query 'DBInstances[0].StorageEncrypted'
```

**Tasks**:
1. Enable encryption for S3 bucket
2. Enable encryption for RDS database
3. Use customer-managed KMS keys
4. Document encryption key IDs
5. Verify encryption is working

### Exercise 3: Configure Network Security

**Objective**: Create secure network with security groups

```bash
# 1. Create VPC
aws ec2 create-vpc --cidr-block 10.0.0.0/16

# 2. Create subnets
aws ec2 create-subnet \
  --vpc-id vpc-xxxxx \
  --cidr-block 10.0.1.0/24  # Public

aws ec2 create-subnet \
  --vpc-id vpc-xxxxx \
  --cidr-block 10.0.2.0/24  # Private

# 3. Create security groups
aws ec2 create-security-group \
  --group-name web-sg \
  --description "Web server security group" \
  --vpc-id vpc-xxxxx

aws ec2 create-security-group \
  --group-name app-sg \
  --description "App server security group" \
  --vpc-id vpc-xxxxx

# 4. Allow traffic from LB to web
aws ec2 authorize-security-group-ingress \
  --group-id sg-web \
  --protocol tcp --port 443 \
  --cidr 0.0.0.0/0

# 5. Allow traffic from web to app
aws ec2 authorize-security-group-ingress \
  --group-id sg-app \
  --protocol tcp --port 8080 \
  --source-group sg-web
```

**Tasks**:
1. Create VPC with public and private subnets
2. Create security groups for each tier
3. Allow traffic only between specific security groups
4. Restrict SSH access to office IP
5. Document all rules

### Exercise 4: Implement SOC 2 Controls

**Objective**: Document and implement SOC 2 controls

**Requirements**:
1. **Access Controls**: Implement and document
   - User provisioning/deprovisioning process
   - MFA requirement for all admin access
   - Quarterly access reviews

2. **Encryption**: Implement and document
   - Data encryption at rest (AES-256)
   - Data encryption in transit (TLS 1.2+)
   - Key management procedures

3. **Monitoring & Logging**: Implement and document
   - CloudTrail for API audit logging
   - Application logging to centralized location
   - Real-time alerting for security events

4. **Incident Response**: Document procedures for
   - Detecting security incidents
   - Investigating incidents
   - Notifying affected parties
   - Post-incident review

**Deliverables**:
- SOC 2 Control Mapping document
- Implementation checklist with evidence
- Monitoring dashboard
- Incident response runbook

---

## Key Takeaways

1. **Shared Responsibility**: Cloud providers secure the infrastructure; you secure your data and applications
2. **Least Privilege**: Give users/applications only the minimum permissions needed
3. **Defense in Depth**: Layer multiple security controls (IAM, encryption, network security, monitoring)
4. **Encryption Everywhere**: Encrypt data at rest and in transit
5. **Monitor Continuously**: Use CSPM and monitoring tools to detect misconfigurations
6. **Compliance is Continuous**: Compliance requires ongoing audits and monitoring, not one-time assessments

---

## Further Resources

- [AWS Security Best Practices](https://aws.amazon.com/security/best-practices/)
- [Azure Security Benchmarks](https://learn.microsoft.com/en-us/security/benchmark/azure/)
- [NIST Cybersecurity Framework](https://www.nist.gov/cyberframework/)
- [Cloud Security Alliance](https://cloudsecurityalliance.org/)
