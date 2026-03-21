---
title: "AWS Security & IAM"
sidebar_label: "Security & IAM"
description: "AWS security best practices — IAM policies, KMS encryption, WAF, GuardDuty, and the shared responsibility model"
sidebar_position: 3
---

# AWS Security & IAM

Security is a foundational pillar of AWS architecture. This guide covers Identity and Access Management (IAM), encryption, threat detection, and practical security patterns.

---

## 1. IAM (Identity and Access Management)

IAM is the foundation of AWS security. It enables you to control who has access to what resources and under what conditions.

### Core IAM Components

#### Users
- Represent individuals or applications
- Each user has a unique 12-digit AWS account ID
- Credentials: username + password (console), or access key ID + secret access key (API/CLI)
- Never share credentials; each user should have their own
- Example: `john.doe@company.com`, `ci-deployment-service`

#### Groups
- Collections of users with shared permissions
- Simplify permission management at scale
- Users can belong to multiple groups
- Example: `Developers`, `Finance`, `Operations`
- Groups cannot be nested

#### Roles
- Temporary credentials for AWS services or external principals
- Include trusted relationships (trust policy) and permissions (permissions policy)
- No long-lived credentials; session tokens are time-bound
- Example: `EC2-S3-Access`, `LambdaExecutionRole`
- Used for service-to-service communication

#### Policies
- JSON documents that define permissions
- Applied to users, groups, or roles
- Three types:
  - **AWS Managed**: Created and maintained by AWS
  - **Customer Managed**: Created and maintained by you
  - **Inline**: Directly embedded in a single principal (not recommended for reuse)

### Policy Structure

Every IAM policy follows this JSON structure:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "AllowS3ReadAccess",
      "Effect": "Allow",
      "Action": "s3:GetObject",
      "Resource": "arn:aws:s3:::my-bucket/*",
      "Condition": {
        "StringEquals": {
          "aws:username": "alice"
        }
      }
    }
  ]
}
```

**Policy Elements:**
- **Version**: Always use "2012-10-17" (the current version)
- **Sid**: Optional statement ID for readability
- **Effect**: "Allow" or "Deny" (Deny is evaluated first)
- **Action**: What can be done (e.g., "s3:GetObject", "ec2:DescribeInstances")
- **Resource**: Which AWS resources (use ARNs)
- **Condition**: Optional context-based restrictions (IP, MFA, time of day, etc.)
- **Principal**: Who the policy applies to (used in trust policies)

### IAM Best Practices

1. **Enable MFA (Multi-Factor Authentication)**
   - Require MFA for root account and all IAM users
   - Use hardware tokens, virtual MFA apps (Google Authenticator), or U2F keys

2. **Follow the Principle of Least Privilege**
   - Grant only the minimum permissions required
   - Start restrictive, add permissions as needed
   - Regularly audit and remove unused permissions

3. **Rotate Credentials Regularly**
   - Change passwords every 90 days
   - Rotate access keys every 90 days
   - Deactivate old keys before deleting them

4. **Use Roles for Applications, Not Users**
   - Never embed AWS credentials in application code
   - Attach roles to EC2 instances, Lambda functions, ECS tasks
   - Credentials are temporary and auto-refreshed

5. **Enforce Strong Password Policies**
   - Minimum 14 characters, uppercase, lowercase, numbers, symbols
   - Prevent password reuse (last 24 passwords)
   - Set password expiration (90-180 days recommended)

6. **Never Use Root Account for Daily Work**
   - Root account has unrestricted access
   - Use for account setup, billing, and emergency only
   - Lock root account with MFA

7. **Monitor IAM Activity**
   - Enable CloudTrail to log all API calls
   - Use CloudWatch alarms for suspicious activity
   - Review IAM Access Analyzer findings

8. **Implement Identity Federation**
   - Integrate with corporate directories (SAML, OIDC)
   - Avoid creating many IAM users
   - Centralize user management

9. **Use Customer Managed Policies for Reusability**
   - Store common policies as customer managed
   - Easier versioning and auditing
   - Inline policies should only be used for one-off exceptions

10. **Enable Access Logging**
    - S3 bucket access logs
    - CloudFront access logs
    - ALB/NLB access logs
    - Analyze logs for unauthorized access attempts

11. **Implement Service Control Policies (SCPs)**
    - Set organization-wide permission guardrails
    - Prevent users from disabling logging or encryption
    - Example: Deny all actions outside specific AWS regions

12. **Regularly Review and Audit Permissions**
    - Use IAM Access Analyzer to find unused roles and permissions
    - Review group memberships quarterly
    - Remove inactive users within 30 days

### IAM Roles: Deep Dive

Roles are the recommended way to grant temporary access.

#### EC2 Instance Roles
```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": "s3:*",
      "Resource": "arn:aws:s3:::my-app-bucket/*"
    }
  ]
}
```

Attach to EC2 instance via instance profile. Credentials are available at:
```
http://169.254.169.254/latest/meta-data/iam/security-credentials/ROLE-NAME
```

#### Lambda Execution Roles
```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": [
        "logs:CreateLogGroup",
        "logs:CreateLogStream",
        "logs:PutLogEvents"
      ],
      "Resource": "arn:aws:logs:*:ACCOUNT-ID:*"
    }
  ]
}
```

#### Cross-Account Access
Allow one AWS account to assume a role in another:

**Trust Policy** (in account B):
```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Principal": {
        "AWS": "arn:aws:iam::ACCOUNT-A:root"
      },
      "Action": "sts:AssumeRole",
      "Condition": {
        "StringEquals": {
          "sts:ExternalId": "unique-secret-key"
        }
      }
    }
  ]
}
```

**Permissions Policy** (in account B):
```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": "ec2:DescribeInstances",
      "Resource": "*"
    }
  ]
}
```

### Instance Profiles

Instance profiles are the link between EC2 instances and IAM roles.

```bash
# Create role
aws iam create-role \
  --role-name EC2-S3-Access \
  --assume-role-policy-document file://trust-policy.json

# Create instance profile
aws iam create-instance-profile \
  --instance-profile-name EC2-S3-Access

# Add role to instance profile
aws iam add-role-to-instance-profile \
  --instance-profile-name EC2-S3-Access \
  --role-name EC2-S3-Access

# Launch EC2 with instance profile
aws ec2 run-instances \
  --image-id ami-12345678 \
  --instance-type t3.medium \
  --iam-instance-profile Name=EC2-S3-Access
```

### Identity Federation

#### SAML 2.0 Federation
Integrate with corporate identity providers (Okta, AD, Azure AD):

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Principal": {
        "Federated": "arn:aws:iam::ACCOUNT-ID:saml-provider/OktaProvider"
      },
      "Action": "sts:AssumeRoleWithSAML",
      "Condition": {
        "StringEquals": {
          "SAML:aud": "https://signin.aws.amazon.com/saml"
        }
      }
    }
  ]
}
```

#### Web Identity (OpenID Connect)
```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Principal": {
        "Federated": "arn:aws:iam::ACCOUNT-ID:oidc-provider/oidc.example.com"
      },
      "Action": "sts:AssumeRoleWithWebIdentity"
    }
  ]
}
```

#### Amazon Cognito
For mobile/web apps:
```bash
aws cognito-identity create-identity-pool \
  --identity-pool-name MobileAppPool \
  --allow-unauthenticated-identities
```

### AWS Organizations & Service Control Policies (SCPs)

Organize multi-account environments:

```bash
# Create organization
aws organizations create-organization --feature-set ALL

# Create organizational unit
aws organizations create-organizational-unit \
  --parent-id r-abcd \
  --name Production

# Create SCP
aws organizations put-policy \
  --content file://policy.json \
  --description "Deny leaving organization" \
  --name DenyLeaveOrganization \
  --type SERVICE_CONTROL_POLICY
```

**Example SCP** - Deny all actions outside allowed regions:
```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Deny",
      "NotAction": [
        "cloudfront:*",
        "iam:*",
        "organizations:*",
        "support:*"
      ],
      "Resource": "*",
      "Condition": {
        "StringNotEquals": {
          "aws:RequestedRegion": [
            "us-east-1",
            "eu-west-1"
          ]
        }
      }
    }
  ]
}
```

### IAM Access Analyzer

Identify unused permissions and external access:

```bash
# Start analysis
aws accessanalyzer start-resource-scan \
  --resource-type AWS::IAM::Role \
  --resource-arn arn:aws:iam::ACCOUNT-ID:role/OldRole

# List findings
aws accessanalyzer list-findings \
  --analyzer-arn arn:aws:access-analyzer:REGION:ACCOUNT-ID:analyzer/MyAnalyzer \
  --filter '{"name": "resourceType", "contains": ["AWS::IAM::Role"]}'
```

---

## 2. KMS (Key Management Service)

KMS provides managed encryption keys for data protection.

### Key Types

#### AWS Managed Keys
- Created and managed by AWS
- Automatically rotated annually
- No additional cost
- Used for S3 default encryption, RDS, etc.
- Key policy cannot be modified

#### Customer Managed Keys
- You create and manage
- More control over lifecycle
- Can enable/disable rotation
- Can set key rotation period (annually recommended)
- Cost: $1/month per key

### Key Rotation

```bash
# Enable key rotation (annual)
aws kms enable-key-rotation --key-id arn:aws:kms:us-east-1:ACCOUNT-ID:key/12345678

# Check rotation status
aws kms get-key-rotation-status --key-id 12345678
```

**Note:** Automatic rotation doesn't delete old keys; they're retained for decryption of old data.

### Envelope Encryption

Encrypt large data with data keys:

1. Call KMS to generate a data key (encrypted and plaintext)
2. Use plaintext data key to encrypt your data
3. Store encrypted data + encrypted data key
4. To decrypt: use KMS to decrypt the data key, then decrypt data

```bash
# Generate data key
aws kms generate-data-key \
  --key-id arn:aws:kms:us-east-1:ACCOUNT-ID:key/12345678 \
  --key-spec AES_256
```

**Benefits:**
- KMS handles only key operations (fast)
- Data stays encrypted in transit and at rest
- Scales to millions of data keys

### KMS Integration

#### S3 Encryption
```bash
# Enable default encryption
aws s3api put-bucket-encryption \
  --bucket my-bucket \
  --server-side-encryption-configuration '{
    "Rules": [{
      "ApplyServerSideEncryptionByDefault": {
        "SSEAlgorithm": "aws:kms",
        "KMSMasterKeyID": "arn:aws:kms:us-east-1:ACCOUNT-ID:key/12345678"
      }
    }]
  }'
```

#### EBS Encryption
```bash
# Create encrypted EBS volume
aws ec2 create-volume \
  --availability-zone us-east-1a \
  --size 100 \
  --encrypted \
  --kms-key-id arn:aws:kms:us-east-1:ACCOUNT-ID:key/12345678
```

#### RDS Encryption
```bash
# Create encrypted DB instance
aws rds create-db-instance \
  --db-instance-identifier mydb \
  --db-instance-class db.t3.micro \
  --engine postgres \
  --master-username admin \
  --storage-encrypted \
  --kms-key-id arn:aws:kms:us-east-1:ACCOUNT-ID:key/12345678
```

---

## 3. AWS Security Services

### WAF (Web Application Firewall)

Protects web applications from common web exploits.

#### Rule Types

| Rule Type | Purpose | Example |
|-----------|---------|---------|
| IP Set | Allow/block by IP | Block attacker IPs |
| Geo Match | Allow/block by country | Block high-risk regions |
| Rate Limiting | Throttle requests | Max 2000 req/5 min per IP |
| String/Regex | Pattern matching | Block SQL injection patterns |
| Size Constraint | Enforce field sizes | POST body less than 8KB |
| XSS/SQLi | AWS managed rules | Block common attack patterns |

#### WAF Rule Example
```bash
# Create IP set
aws wafv2 create-ip-set \
  --scope REGIONAL \
  --region us-east-1 \
  --name BlockedIPs \
  --ip-address-version IPV4 \
  --addresses '["203.0.113.0/24"]'

# Create rate limiting rule
aws wafv2 create-web-acl \
  --scope REGIONAL \
  --region us-east-1 \
  --name MyWebACL \
  --default-action Allow={} \
  --rules '[{
    "Name": "RateLimitRule",
    "Priority": 1,
    "Statement": {
      "RateBasedStatement": {
        "Limit": 2000,
        "AggregateKeyType": "IP"
      }
    },
    "Action": {"Block": {}},
    "VisibilityConfig": {
      "SampledRequestsEnabled": true,
      "CloudWatchMetricsEnabled": true,
      "MetricName": "RateLimitRule"
    }
  }]' \
  --visibility-config \
    SampledRequestsEnabled=true,CloudWatchMetricsEnabled=true,MetricName=MyWebACL
```

#### Attach WAF to ALB
```bash
aws wafv2 associate-web-acl \
  --web-acl-arn arn:aws:wafv2:us-east-1:ACCOUNT-ID:regional/webacl/MyWebACL/12345678 \
  --resource-arn arn:aws:elasticloadbalancing:us-east-1:ACCOUNT-ID:loadbalancer/app/my-alb/1234567890abcdef
```

### Shield (DDoS Protection)

| Feature | Standard | Advanced |
|---------|----------|----------|
| Cost | Free | $3,000/month |
| Coverage | Layer 3 & 4 | Layer 3, 4, 7 |
| Always On | Yes | Yes |
| DDoS Response Team (DRT) | No | Yes |
| Alerts | No | Yes |
| Cost Protection | No | Yes (up to $20K) |

### GuardDuty (Threat Detection)

Monitors for suspicious activity using machine learning.

#### Enable GuardDuty
```bash
aws guardduty create-detector --enable

# List detectors
aws guardduty list-detectors

# Get findings
aws guardduty list-findings --detector-id 12345678
```

#### Finding Types
- **Reconnaissance**: Unusual API calls, port scanning
- **CryptoCurrency**: EC2 performing crypto mining
- **Persistence**: Compromised credentials, persistence mechanisms
- **Privilege Escalation**: Attempts to escalate permissions
- **Defense Evasion**: Disabling logging, deleting evidence

#### Finding Example
```json
{
  "Id": "12345678",
  "Type": "EC2_UnauthorizedAccess:EC2/SSHBruteForce",
  "Severity": 7.5,
  "FindingTime": "2024-03-21T10:30:00Z",
  "Resource": {
    "InstanceDetails": {
      "InstanceId": "i-1234567890abcdef0"
    }
  },
  "ServiceDetails": {
    "Action": {
      "NetworkConnectionAction": {
        "RemoteIpDetails": {
          "IpAddressV4": "192.0.2.1",
          "Country": "KP"
        }
      }
    }
  }
}
```

### Inspector (Vulnerability Assessment)

Automated security assessments for EC2 and container images.

```bash
# Enable Inspector
aws inspector2 enable

# List findings
aws inspector2 list-findings

# Get findings details
aws inspector2 batch-get-findings \
  --finding-arns 'arn:aws:inspector2:us-east-1:ACCOUNT-ID:finding/12345678'
```

**Assessment Types:**
- Network reachability (open ports, exposed services)
- Software vulnerabilities (CVEs in installed packages)
- Container image vulnerabilities

### Macie (Data Discovery)

Discovers and protects sensitive data in S3.

```bash
# Enable Macie
aws macie2 enable-macie

# Create classification job
aws macie2 create-classification-job \
  --job-type DISCOVER_S3 \
  --job-config-file-path s3://my-bucket/job-config.json
```

**Sensitive Data Types:**
- Credit card numbers
- Social Security numbers
- Passport numbers
- Bank account numbers
- API keys, passwords

### Security Hub (Centralized Security)

Aggregates findings from GuardDuty, Inspector, Macie, IAM Access Analyzer.

```bash
# Enable Security Hub
aws securityhub enable-security-hub

# Get aggregated findings
aws securityhub get-findings \
  --filters '{
    "SeverityLabel": [{"Value": "CRITICAL", "Comparison": "EQUALS"}],
    "RecordState": [{"Value": "ACTIVE", "Comparison": "EQUALS"}]
  }'
```

### CloudTrail (API Logging)

Logs all API calls for audit and compliance.

```bash
# Create trail
aws cloudtrail create-trail \
  --name MyTrail \
  --s3-bucket-name my-cloudtrail-logs

# Enable logging
aws cloudtrail start-logging --trail-name MyTrail

# Get events
aws cloudtrail lookup-events \
  --lookup-attributes AttributeKey=ResourceName,AttributeValue=my-instance \
  --max-results 50
```

**Event Example:**
```json
{
  "EventTime": "2024-03-21T14:30:00Z",
  "EventName": "DeleteBucket",
  "Username": "alice@company.com",
  "Resources": [
    {
      "ARN": "arn:aws:s3:::my-bucket",
      "ResourceType": "AWS::S3::Bucket"
    }
  ],
  "CloudTrailEvent": {
    "eventVersion": "1.08",
    "userIdentity": {
      "type": "IAMUser",
      "principalId": "AIDAI1234567890ABCD",
      "arn": "arn:aws:iam::ACCOUNT-ID:user/alice",
      "sourceIPAddress": "203.0.113.100",
      "mfaAuthenticated": true
    },
    "eventTime": "2024-03-21T14:30:00Z",
    "eventSource": "s3.amazonaws.com",
    "eventName": "DeleteBucket",
    "awsRegion": "us-east-1",
    "sourceIPAddress": "203.0.113.100",
    "requestParameters": {
      "bucketName": "my-bucket"
    }
  }
}
```

### AWS Config (Resource Compliance)

Tracks resource configuration changes and compliance.

```bash
# Enable AWS Config
aws configservice put-config-recorder \
  --config-recorder-name default \
  --role-arn arn:aws:iam::ACCOUNT-ID:role/aws-service-role/config.amazonaws.com/AWSServiceRoleForConfig

aws configservice start-config-recorder --config-recorder-names default

# Create rule
aws configservice put-config-rule \
  --config-rule '{
    "ConfigRuleName": "encrypted-volumes",
    "Source": {
      "Owner": "AWS",
      "SourceIdentifier": "ENCRYPTED_VOLUMES"
    }
  }'

# Get compliance
aws configservice describe-compliance-by-config-rule
```

### Secrets Manager vs Parameter Store

| Feature | Secrets Manager | Parameter Store |
|---------|-----------------|-----------------|
| **Type** | Rotation, versioning | Simple key-value |
| **Rotation** | Automatic | Manual |
| **Encryption** | KMS (required) | KMS (optional) |
| **Versioning** | Native | Via labels |
| **Audit** | CloudTrail | CloudTrail |
| **Cost** | $0.40 per secret/month | Free (Standard tier) |
| **Best For** | Database passwords, API keys | Configuration, flags |

#### Secrets Manager Example
```bash
# Create secret
aws secretsmanager create-secret \
  --name prod/db/password \
  --secret-string '{"username":"admin","password":"SuperSecret123!"}' \
  --kms-key-id arn:aws:kms:us-east-1:ACCOUNT-ID:key/12345678

# Retrieve secret
aws secretsmanager get-secret-value \
  --secret-id prod/db/password \
  --query SecretString \
  --output text | jq .password
```

#### Parameter Store Example
```bash
# Create parameter
aws ssm put-parameter \
  --name /app/config/api-timeout \
  --value 30 \
  --type String

# Get parameter
aws ssm get-parameter --name /app/config/api-timeout --query Parameter.Value
```

---

## 4. Encryption

### At-Rest Encryption

Data encrypted while stored in AWS services.

| Service | Method | Key Management |
|---------|--------|-----------------|
| S3 | SSE-S3, SSE-KMS | AWS or Customer |
| EBS | Automatic | AWS or Customer |
| RDS | Native encryption | AWS or Customer |
| DynamoDB | Native encryption | AWS managed |
| ElastiCache | Redis encryption | AWS or Customer |

**S3 at-rest encryption:**
```bash
# Server-side encryption (KMS)
aws s3api put-object \
  --bucket my-bucket \
  --key important-file.txt \
  --body file.txt \
  --server-side-encryption aws:kms \
  --sse-kms-key-id arn:aws:kms:us-east-1:ACCOUNT-ID:key/12345678
```

### In-Transit Encryption

Data encrypted during transmission.

#### TLS/SSL
- Enforced between client and AWS
- Use HTTPS for all API calls
- TLS 1.2 minimum (1.3 recommended)

```bash
# Enforce HTTPS in S3
aws s3api put-bucket-policy \
  --bucket my-bucket \
  --policy '{
    "Version": "2012-10-17",
    "Statement": [{
      "Effect": "Deny",
      "Principal": "*",
      "Action": "s3:*",
      "Resource": ["arn:aws:s3:::my-bucket", "arn:aws:s3:::my-bucket/*"],
      "Condition": {
        "Bool": {
          "aws:SecureTransport": "false"
        }
      }
    }]
  }'
```

#### VPC Encryption
- RDS: Enable encryption in transit
- ElastiCache: Enable in-transit encryption for Redis
- ECS: Encrypt task-to-task communication

### Certificate Management (ACM)

AWS Certificate Manager provides free public certificates.

```bash
# Request certificate
aws acm request-certificate \
  --domain-name example.com \
  --subject-alternative-names www.example.com \
  --validation-method DNS

# List certificates
aws acm list-certificates

# Describe certificate
aws acm describe-certificate --certificate-arn arn:aws:acm:us-east-1:ACCOUNT-ID:certificate/12345678
```

**Certificate Types:**
- Public SSL/TLS (free, auto-renewed)
- Private CA (for internal PKI, paid)

---

## 5. VPC Security

### Security Groups

Stateful firewall rules at instance level.

```bash
# Create security group
aws ec2 create-security-group \
  --group-name web-sg \
  --description "Web tier security group" \
  --vpc-id vpc-12345678

# Allow inbound HTTP/HTTPS
aws ec2 authorize-security-group-ingress \
  --group-id sg-12345678 \
  --protocol tcp \
  --port 80 \
  --cidr 0.0.0.0/0

aws ec2 authorize-security-group-ingress \
  --group-id sg-12345678 \
  --protocol tcp \
  --port 443 \
  --cidr 0.0.0.0/0

# Allow inbound from app tier
aws ec2 authorize-security-group-ingress \
  --group-id sg-web \
  --protocol tcp \
  --port 3000 \
  --source-group sg-app
```

**Best Practices:**
- Never allow 0.0.0.0/0 for SSH (port 22)
- Use specific CIDRs and security groups
- Remove unused rules quarterly

### Network Access Control Lists (NACLs)

Stateless rules at subnet level (evaluated before security groups).

```bash
# Create NACL
aws ec2 create-network-acl \
  --vpc-id vpc-12345678

# Add inbound rule
aws ec2 create-network-acl-entry \
  --network-acl-id acl-12345678 \
  --rule-number 100 \
  --protocol tcp \
  --port-range FromPort=80,ToPort=80 \
  --cidr-block 0.0.0.0/0 \
  --ingress

# Add outbound rule
aws ec2 create-network-acl-entry \
  --network-acl-id acl-12345678 \
  --rule-number 100 \
  --protocol tcp \
  --port-range FromPort=1024,ToPort=65535 \
  --cidr-block 0.0.0.0/0 \
  --egress
```

### VPC Endpoints

Access AWS services without traversing the internet.

#### Gateway Endpoints (Free)
- S3, DynamoDB
- Accessible only within VPC
- No internet gateway required

```bash
# Create S3 endpoint
aws ec2 create-vpc-endpoint \
  --vpc-id vpc-12345678 \
  --service-name com.amazonaws.us-east-1.s3 \
  --route-table-ids rtb-12345678
```

#### Interface Endpoints ($7/month)
- EC2, SNS, SQS, Secrets Manager, etc.
- Uses ENI with private IP
- Requires security group and DNS

```bash
# Create Secrets Manager endpoint
aws ec2 create-vpc-endpoint \
  --vpc-id vpc-12345678 \
  --vpc-endpoint-type Interface \
  --service-name com.amazonaws.us-east-1.secretsmanager \
  --subnet-ids subnet-12345678 \
  --security-group-ids sg-12345678
```

### PrivateLink

Securely expose services across accounts/VPCs.

```bash
# Create NLB endpoint service
aws elbv2 create-network-load-balancer \
  --name my-nlb \
  --subnets subnet-12345678

aws ec2 create-vpc-endpoint-service-configuration \
  --network-load-balancer-arns arn:aws:elasticloadbalancing:us-east-1:ACCOUNT-ID:loadbalancer/net/my-nlb/1234567890abcdef
```

---

## 6. Security Best Practices Checklist

### Account & Identity
- [ ] Enable MFA on root account
- [ ] Create individual IAM users (never share credentials)
- [ ] Enable CloudTrail on all accounts
- [ ] Enable GuardDuty globally
- [ ] Enable Security Hub for centralized findings
- [ ] Implement SCPs in AWS Organizations
- [ ] Review IAM Access Analyzer findings monthly
- [ ] Enforce password complexity (14+ chars, mixed case, symbols)
- [ ] Rotate credentials every 90 days
- [ ] Remove inactive users and roles within 30 days

### Data Protection
- [ ] Enable default S3 bucket encryption (KMS)
- [ ] Block public S3 access (account and bucket level)
- [ ] Enable EBS encryption by default
- [ ] Enable RDS encryption at rest and in transit
- [ ] Enable S3 versioning and MFA delete
- [ ] Use HTTPS everywhere (enforce via policy)
- [ ] Enable S3 access logging and bucket logging
- [ ] Implement KMS key rotation (annual)
- [ ] Enable DynamoDB encryption
- [ ] Use Secrets Manager for sensitive data (not Parameter Store)

### Monitoring & Detection
- [ ] Enable CloudTrail with S3 storage
- [ ] Create CloudWatch alarms for suspicious activity
- [ ] Enable GuardDuty threat detection
- [ ] Enable Inspector for vulnerability scanning
- [ ] Enable Macie for sensitive data discovery
- [ ] Enable AWS Config for resource compliance
- [ ] Send logs to centralized SIEM (e.g., Splunk)
- [ ] Review CloudTrail logs daily
- [ ] Set up SNS alerts for security findings
- [ ] Monitor failed login attempts

### Network Security
- [ ] Use VPC (never launch in EC2-Classic)
- [ ] Enable Flow Logs on all VPCs
- [ ] Use security groups instead of NACLs (when possible)
- [ ] Never allow SSH (22) from 0.0.0.0/0
- [ ] Use VPC endpoints for AWS services
- [ ] Enable VPC Private Link for service exposure
- [ ] Use WAF on ALBs and CloudFront
- [ ] Enable Shield Advanced for DDoS protection
- [ ] Block unused ports and protocols
- [ ] Implement network segmentation (DMZ, app, data tiers)

### Compliance & Audit
- [ ] Document security architecture
- [ ] Maintain change log for all permissions
- [ ] Conduct quarterly access reviews
- [ ] Implement least privilege principle
- [ ] Use service control policies for guardrails
- [ ] Document incident response procedures
- [ ] Enable CloudTrail for regulatory compliance
- [ ] Enable S3 object lock for compliance
- [ ] Implement tagging for cost/security tracking

---

## 7. Common IAM Policy Examples

### 1. Admin Access
Full unrestricted access (use sparingly).

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": "*",
      "Resource": "*"
    }
  ]
}
```

**Use Case:** Break-glass account, initial setup only.

### 2. S3 Read-Only Access to Specific Bucket

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "ListBucket",
      "Effect": "Allow",
      "Action": "s3:ListBucket",
      "Resource": "arn:aws:s3:::my-app-bucket"
    },
    {
      "Sid": "GetObject",
      "Effect": "Allow",
      "Action": "s3:GetObject",
      "Resource": "arn:aws:s3:::my-app-bucket/*"
    }
  ]
}
```

**Use Case:** Data analyst reading reporting data. Prevents writes and cross-bucket access.

### 3. EC2 Start/Stop in Specific Region

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "EC2DescribeInstances",
      "Effect": "Allow",
      "Action": [
        "ec2:DescribeInstances",
        "ec2:DescribeInstanceStatus"
      ],
      "Resource": "*"
    },
    {
      "Sid": "EC2StartStop",
      "Effect": "Allow",
      "Action": [
        "ec2:StartInstances",
        "ec2:StopInstances"
      ],
      "Resource": "arn:aws:ec2:us-east-1:ACCOUNT-ID:instance/*"
    }
  ]
}
```

**Use Case:** Cost optimization script that auto-stops instances in dev environment.

### 4. Lambda Invocation

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": [
        "lambda:InvokeFunction",
        "lambda:ListFunctions"
      ],
      "Resource": "arn:aws:lambda:us-east-1:ACCOUNT-ID:function:ProcessData"
    }
  ]
}
```

**Use Case:** Application invoking Lambda functions for async tasks. Prevents deletion or modification.

### 5. Cross-Account Role Assumption

Assume role in another AWS account (with external ID for security).

**Trust Policy (Account B):**
```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Principal": {
        "AWS": "arn:aws:iam::111111111111:role/DeploymentRole"
      },
      "Action": "sts:AssumeRole",
      "Condition": {
        "StringEquals": {
          "sts:ExternalId": "abc123def456"
        }
      }
    }
  ]
}
```

**Permissions Policy (Account B):**
```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": [
        "ec2:*",
        "s3:*",
        "lambda:*"
      ],
      "Resource": "*"
    }
  ]
}
```

**Assume role in Python:**
```python
import boto3

sts = boto3.client('sts')
response = sts.assume_role(
    RoleArn='arn:aws:iam::222222222222:role/CrossAccountRole',
    RoleSessionName='DeploymentSession',
    ExternalId='abc123def456'
)

credentials = response['Credentials']
s3 = boto3.client(
    's3',
    aws_access_key_id=credentials['AccessKeyId'],
    aws_secret_access_key=credentials['SecretAccessKey'],
    aws_session_token=credentials['SessionToken']
)
```

---

## Hands-On Exercises

### Exercise 1: Create IAM Users, Groups, and Policies

**Objective:** Set up a development team with restricted S3 access.

```bash
# Create group
aws iam create-group --group-name Developers

# Create users
aws iam create-user --user-name alice
aws iam create-user --user-name bob

# Add users to group
aws iam add-user-to-group --group-name Developers --user-name alice
aws iam add-user-to-group --group-name Developers --user-name bob

# Create customer-managed policy
aws iam create-policy \
  --policy-name DeveloperS3Access \
  --policy-document '{
    "Version": "2012-10-17",
    "Statement": [
      {
        "Effect": "Allow",
        "Action": ["s3:GetObject", "s3:PutObject"],
        "Resource": "arn:aws:s3:::dev-bucket/*"
      },
      {
        "Effect": "Allow",
        "Action": "s3:ListBucket",
        "Resource": "arn:aws:s3:::dev-bucket"
      }
    ]
  }'

# Attach policy to group
aws iam attach-group-policy \
  --group-name Developers \
  --policy-arn arn:aws:iam::ACCOUNT-ID:policy/DeveloperS3Access

# Verify
aws iam list-group-policies --group-name Developers
```

### Exercise 2: Enable CloudTrail and Review API Activity

**Objective:** Set up centralized logging and identify unexpected API calls.

```bash
# Create S3 bucket for logs
aws s3 mb s3://cloudtrail-logs-$(date +%s)

# Create bucket policy
aws s3api put-bucket-policy \
  --bucket cloudtrail-logs-12345 \
  --policy '{
    "Version": "2012-10-17",
    "Statement": [
      {
        "Sid": "AWSCloudTrailAclCheck",
        "Effect": "Allow",
        "Principal": {
          "Service": "cloudtrail.amazonaws.com"
        },
        "Action": "s3:GetBucketAcl",
        "Resource": "arn:aws:s3:::cloudtrail-logs-12345"
      },
      {
        "Sid": "AWSCloudTrailWrite",
        "Effect": "Allow",
        "Principal": {
          "Service": "cloudtrail.amazonaws.com"
        },
        "Action": "s3:PutObject",
        "Resource": "arn:aws:s3:::cloudtrail-logs-12345/*",
        "Condition": {
          "StringEquals": {
            "s3:x-amz-acl": "bucket-owner-full-control"
          }
        }
      }
    ]
  }'

# Create trail
aws cloudtrail create-trail \
  --name ManagementTrail \
  --s3-bucket-name cloudtrail-logs-12345

# Enable logging
aws cloudtrail start-logging --trail-name ManagementTrail

# Query events
aws cloudtrail lookup-events \
  --max-results 10 \
  --query 'Events[*].[EventTime,EventName,Username,Resources[0].ResourceName]' \
  --output table
```

### Exercise 3: Set Up GuardDuty and Review Findings

**Objective:** Enable threat detection and investigate suspicious activity.

```bash
# Enable GuardDuty
aws guardduty create-detector --enable

# Get detector ID
DETECTOR_ID=$(aws guardduty list-detectors --query 'DetectorIds[0]' --output text)

# List findings (may be empty initially)
aws guardduty list-findings \
  --detector-id $DETECTOR_ID \
  --query 'FindingIds' \
  --output table

# Create sample finding (manual testing)
# Simulate by stopping/starting EC2 instances, failed SSH attempts, etc.

# Get findings details
aws guardduty get-findings \
  --detector-id $DETECTOR_ID \
  --finding-ids finding-id-here \
  --query 'Findings[*].[Type,Severity,FindingTime]' \
  --output table
```

### Exercise 4: Implement KMS Encryption for S3 Bucket

**Objective:** Create a customer-managed KMS key and apply it to S3.

```bash
# Create customer-managed KMS key
KEY_ID=$(aws kms create-key \
  --description "S3 bucket encryption key" \
  --key-usage ENCRYPT_DECRYPT \
  --query 'KeyMetadata.KeyId' \
  --output text)

# Create alias
aws kms create-alias \
  --alias-name alias/s3-bucket-key \
  --target-key-id $KEY_ID

# Enable key rotation
aws kms enable-key-rotation --key-id $KEY_ID

# Apply to S3 bucket default encryption
aws s3api put-bucket-encryption \
  --bucket my-bucket \
  --server-side-encryption-configuration '{
    "Rules": [{
      "ApplyServerSideEncryptionByDefault": {
        "SSEAlgorithm": "aws:kms",
        "KMSMasterKeyID": "arn:aws:kms:us-east-1:ACCOUNT-ID:key/'$KEY_ID'"
      }
    }]
  }'

# Verify
aws s3api get-bucket-encryption --bucket my-bucket

# Test encryption
aws s3api put-object \
  --bucket my-bucket \
  --key test-file.txt \
  --body test-file.txt

aws s3api head-object \
  --bucket my-bucket \
  --key test-file.txt \
  --query 'ServerSideEncryption'
```

---

## Key Takeaways

1. **IAM is foundational** — Use roles for applications, enforce MFA, follow least privilege
2. **Encryption matters** — Use KMS for data at rest, enforce TLS for data in transit
3. **Monitor everything** — CloudTrail, GuardDuty, Security Hub provide visibility
4. **Automate security** — Use SCPs, Config rules, and Inspector for continuous compliance
5. **Defense in depth** — Combine security groups, NACLs, WAF, Shield, and GuardDuty
6. **Rotate credentials** — Every 90 days for users and access keys
7. **Test your controls** — Regularly audit findings and review access

---

## References

- [AWS IAM Best Practices](https://docs.aws.amazon.com/IAM/latest/UserGuide/best-practices.html)
- [AWS KMS Developer Guide](https://docs.aws.amazon.com/kms/)
- [AWS WAF User Guide](https://docs.aws.amazon.com/waf/)
- [AWS CloudTrail User Guide](https://docs.aws.amazon.com/awscloudtrail/)
- [AWS Security Best Practices](https://aws.amazon.com/security/best-practices/)
