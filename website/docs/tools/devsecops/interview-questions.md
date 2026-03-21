---
title: "DevSecOps Interview Questions"
description: "30+ DevSecOps interview questions with detailed answers"
sidebar_label: "Interview Questions"
sidebar_position: 4
---

# DevSecOps Interview Questions

Comprehensive interview preparation guide with 30+ DevSecOps questions and answers.

## Fundamentals

### 1. What is DevSecOps and how is it different from DevOps?

**Answer:**

**DevOps:**
- Focuses on automation, collaboration, and fast delivery
- Integrates development and operations
- Emphasizes speed and efficiency

**DevSecOps:**
- Extends DevOps with security integration
- Embeds security throughout the entire pipeline
- Makes security everyone's responsibility, not just security team's

**Key Differences:**

| Aspect | DevOps | DevSecOps |
|--------|--------|----------|
| Focus | Speed + Reliability | Speed + Reliability + Security |
| Security | After deployment | Throughout pipeline |
| Responsibility | Shared (Dev/Ops) | Shared (Dev/Ops/Security) |
| Tools | CI/CD tools | CI/CD + security tools |
| Testing | Functional + performance | + Security testing |

**DevSecOps Pipeline:**
```
Plan → Code → Build → Test → Deploy → Monitor
         ↓      ↓      ↓       ↓        ↓
      Secrets Scan SAST   SCA    DAST   App Sec
      Detection           Scan   Scanning Monitoring
```

### 2. What does "Shift-Left" mean in DevSecOps?

**Answer:**
Shift-Left means moving security checks earlier in the development process (to the left in the timeline), from right (deployment/production) to left (development).

**Timeline Comparison:**

**Traditional (Right):**
```
Code → Build → Test → Deploy → Security Scan
                                    (too late!)
```

**Shift-Left:**
```
Pre-commit → Code → Build → Test → Deploy
   ↓                                  ↓
  SAST,          DAST, Container   Monitoring
  Linting,       Scanning, SCA
  Secrets
(catch early!)
```

**Benefits:**
- Issues caught when developers are still focused
- Cheaper and faster to fix
- Developers invest in security
- Faster feedback loop

**Implementation:**
1. Pre-commit hooks (secrets, linting)
2. IDE plugins (real-time feedback)
3. SAST on every commit
4. Dependency scanning
5. Container scanning
6. DAST in staging
7. Runtime security monitoring

### 3. What is the difference between vulnerability detection and vulnerability management?

**Answer:**

**Vulnerability Detection:**
- Finding vulnerabilities (the "what")
- Tools: SAST, DAST, SCA, scanning
- Output: List of vulnerabilities
- Goal: Identify issues

**Vulnerability Management:**
- Managing the full lifecycle of vulnerabilities (the "how")
- Includes: Detection, assessment, prioritization, remediation, verification
- Process: Find → Prioritize → Fix → Verify → Close
- Goal: Eliminate vulnerabilities

**Example:**

```
Detection:    Trivy finds CVE-2024-1234 in Docker image
Assessment:   CVSS score 8.5, affects production system
Prioritization: Must fix before next deployment
Remediation:  Update library from 1.0 to 1.0.1
Verification: Rescan image, confirm fixed
Closure:      Mark as resolved
```

## SAST & Code Security

### 4. What is SAST and how does it differ from DAST?

**Answer:**

**SAST (Static Application Security Testing):**
- Analyzes source code WITHOUT running it
- Tests the code itself
- Fast (minutes)
- Finds: Code vulnerabilities, logic errors, hardcoded secrets

**Example SAST Issues:**
```python
# SQL Injection vulnerability
query = f"SELECT * FROM users WHERE id = {user_id}"
db.execute(query)
```

**Tools:** SonarQube, Semgrep, Bandit, Checkmarx

---

**DAST (Dynamic Application Security Testing):**
- Tests running application
- Tests application behavior
- Slower (hours)
- Finds: Authentication flaws, business logic issues, API vulnerabilities

**Example DAST Issues:**
- Can bypass authentication
- Can access other user's data
- Business logic allows unauthorized actions

**Tools:** OWASP ZAP, Burp Suite, Acunetix

---

**Comparison:**

| Aspect | SAST | DAST |
|--------|------|------|
| **When** | During development | After deployment |
| **Execution** | No execution | Run application |
| **Speed** | Fast (minutes) | Slow (hours) |
| **False Positives** | More | Fewer |
| **Setup** | Easier | Harder (needs running app) |
| **Finds** | Code issues | Behavioral issues |
| **Cost** | Lower | Higher |

**Best Practice:** Use both (complementary, not competing)

### 5. What are the most common code vulnerabilities found by SAST tools?

**Answer:**

1. **SQL Injection** — Attacker injects SQL code through user input
```python
# Vulnerable
user_id = request.get('id')
query = f"SELECT * FROM users WHERE id = {user_id}"
```

2. **Cross-Site Scripting (XSS)** — Attacker injects JavaScript into page
```html
<!-- Vulnerable -->
<div><%= user_input %></div>
```

3. **Hardcoded Secrets** — API keys, passwords in code
```python
# Vulnerable
API_KEY = "sk_live_abc123def456"
```

4. **Insecure Deserialization** — Untrusted data deserialization
```python
# Vulnerable
data = pickle.loads(user_input)
```

5. **Weak Cryptography** — Using outdated encryption
```python
# Vulnerable
hashlib.md5(password).hexdigest()
```

6. **Path Traversal** — Access files outside intended directory
```python
# Vulnerable
file_path = f"/uploads/{user_provided_filename}"
```

7. **Insecure Randomness** — Predictable random numbers
```python
# Vulnerable
random.randint(1, 1000)  # Not cryptographically secure
```

8. **Unvalidated Redirect** — Redirect to untrusted URL
```python
# Vulnerable
return redirect(request.get('next_url'))
```

9. **Missing Authentication** — Unprotected endpoints
```python
# Vulnerable
@app.route('/admin')
def admin():  # No auth check
    pass
```

10. **Command Injection** — Attacker injects shell commands
```python
# Vulnerable
os.system(f"ping {user_input}")
```

## SCA & Dependencies

### 6. What is Software Composition Analysis (SCA) and why is it important?

**Answer:**
SCA analyzes third-party dependencies and open-source components to identify vulnerabilities and license compliance issues.

**Why Important:**
- Average application has hundreds of dependencies
- Vulnerabilities in dependencies are common
- Attackers target vulnerable libraries
- Licensing issues can be legal/financial risk

**Process:**

```
Dependencies (package.json, requirements.txt, etc.)
    ↓
Extract package info and versions
    ↓
Compare against vulnerability databases
    ↓
Report vulnerabilities and licenses
    ↓
Recommend patches/upgrades
```

**Types of Issues Found:**

1. **Known Vulnerabilities** — Publicly disclosed CVEs
2. **Outdated Versions** — Using old version with patches available
3. **License Compliance** — GPL, MIT, proprietary licenses
4. **Malicious Packages** — Intentionally malicious code
5. **Unmaintained Packages** — No longer maintained

**Popular SCA Tools:**
- Snyk
- Dependabot
- Black Duck
- OWASP Dependency-Check

### 7. How would you handle a vulnerability in a critical dependency?

**Answer:**

**Process:**

1. **Assess Impact**
   - How severe is vulnerability?
   - How many users affected?
   - Can it be exploited in our use case?

2. **Immediate Mitigations**
   ```
   Option A: Update to patched version
   Option B: Use older version if patch available
   Option C: Disable vulnerable feature temporarily
   Option D: Apply WAF rules to block exploitation
   ```

3. **Update Dependency**
   ```bash
   # If patch available
   npm update vulnerable-package
   npm audit fix

   # If no patch, upgrade to newer major version
   npm install vulnerable-package@latest
   ```

4. **Test Thoroughly**
   - Run full test suite
   - Test with new version
   - Monitor for regressions
   - Performance testing

5. **Deploy**
   - Deploy to staging first
   - Monitor for issues
   - Deploy to production
   - Verify in production

6. **Documentation**
   - Document vulnerability
   - Document applied mitigations
   - Update security advisories

**Example Timeline:**
```
Day 1: Vulnerability discovered in dependency
Day 1: Assess impact (P1 - critical, 50% users affected)
Day 1-2: Patch released by vendor
Day 2: Update, test, deploy to staging
Day 2: Verify in staging
Day 3: Deploy to production
Day 3: Monitor for issues
Week 1: Document in postmortem
```

## Container Security

### 8. What are the key areas of container security?

**Answer:**

**1. Image Security**
- Scan for vulnerabilities
- Use minimal base images
- Keep images updated
- Sign images for integrity

**2. Registry Security**
- Authenticate to registry
- Encrypt connections
- Scan images on push
- Access control (who can push)

**3. Runtime Security**
- Isolate containers (namespaces)
- Limit resources (CPU, memory)
- Drop unnecessary capabilities
- Read-only filesystems

**4. Supply Chain Security**
- Verify image sources
- Validate signatures
- Track image provenance
- Document dependencies

**Container Scanning Example:**

```bash
# Find vulnerabilities
trivy image myapp:latest

# Output:
# python/cryptography:3.1 (Python)
#   Critical vulnerability: CVE-2023-1234
#   Remote Code Execution possible

# Fix by updating base image
FROM python:3.11-slim
```

### 9. What is the difference between image scanning and runtime security?

**Answer:**

**Image Scanning (Build-time):**
- Scan image before deployment
- Check for known vulnerabilities
- Static analysis
- Fast (minutes)
- Prevents vulnerable images from running

**Tool Example:**
```bash
trivy image myapp:latest
```

---

**Runtime Security (Runtime):**
- Monitor running containers
- Detect suspicious behavior
- Dynamic analysis
- Continuous monitoring
- Detects zero-day exploits

**Tool Example:**
```bash
falco --rules=/etc/falco/rules.yaml
```

---

**Key Differences:**

| Aspect | Image Scanning | Runtime Security |
|--------|---|---|
| **When** | Before deployment | While running |
| **Focuses on** | Known vulnerabilities | Behavior |
| **Finds** | CVEs, misconfigurations | Exploits, anomalies |
| **Tools** | Trivy, Grype, Clair | Falco, Sysdig |
| **Can stop** | Bad image from running | Running exploit |

**Recommendation:** Use both approaches for defense in depth

## Secrets Management

### 10. How do you prevent secrets from being committed to Git?

**Answer:**

**Methods:**

1. **Pre-commit Hooks**
```bash
# .git/hooks/pre-commit
#!/bin/bash
gitleaks detect --staged
if [ $? -ne 0 ]; then
  exit 1
fi
```

2. **CI/CD Checks**
```yaml
jobs:
  scan:
    script:
      - gitleaks detect
      - if [ $? -ne 0 ]; then exit 1; fi
```

3. **Repository Settings**
- Enable secret scanning in GitHub
- Require branch protection rules
- Require pull request reviews

4. **Education**
- Teach developers about secrets
- Show tools available
- Make safe path easier than unsafe path

**Detecting Existing Secrets:**

```bash
# Scan entire history
gitleaks detect --verbose

# If found, rotate immediately
1. Disable compromised key
2. Generate new key
3. Update in secret manager
4. Audit who accessed key
5. Update code to use new key
6. Remove secret from Git history (carefully!)
```

## Compliance & Governance

### 11. What is Compliance-as-Code?

**Answer:**
Compliance-as-Code means encoding security and compliance requirements into code so they're automatically enforced.

**Instead of:**
- Manual compliance checklist
- Security team reviews manually
- Humans forget or make mistakes
- No audit trail

**Use:**
- Code that enforces requirements
- Automatic validation
- Audit trail in version control
- Consistent enforcement

**Example (Terraform):**

```hcl
# Enforce S3 encryption
resource "aws_s3_bucket" "secure" {
  bucket = "my-bucket"

  server_side_encryption_configuration {
    rule {
      apply_server_side_encryption_by_default {
        sse_algorithm = "AES256"  # Required
      }
    }
  }

  # Block public access (required)
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}
```

**Tools:**
- Checkov (IaC scanning)
- Terraform Policy (OPA)
- Kyverno (Kubernetes)

**Benefits:**
- Enforces policy automatically
- Fast feedback
- Audit trail
- Consistent across all resources

### 12. How would you implement security compliance for different environments?

**Answer:**

**Different Security Levels per Environment:**

```
Development: More lenient (speed > security)
Staging: Same as production (test real settings)
Production: Strictest (must comply with regulations)
```

**Implementation:**

1. **Define Policies**
```yaml
policies:
  dev:
    encryption: optional
    logging: optional
    mfa: optional

  staging:
    encryption: required
    logging: required
    mfa: required

  production:
    encryption: required (AES-256)
    logging: required (centralized)
    mfa: required (for all users)
    audit-trail: required
    disaster-recovery: required
```

2. **Enforce with Code**
```python
# Terraform variables per environment
if environment == "production":
  enforce_encryption = true
  enforce_logging = true
  enforce_mfa = true
else:
  enforce_encryption = false  # Dev/staging
```

3. **Validate in Pipeline**
```yaml
# All environments
- Lint code for basic issues
- Run SAST
- Scan dependencies

# Staging only
- Run DAST
- Load testing

# Production only
- Additional approval gates
- Compliance checks
- Security review
```

## Cloud-Specific Security

### 13. What are the most common AWS misconfigurations?

**Answer:**

**Top AWS Security Misconfigurations:**

1. **Overly Permissive IAM Policies**
```json
{
  "Effect": "Allow",
  "Action": "*",           // Too broad!
  "Resource": "*"
}
```

**Fix:**
```json
{
  "Effect": "Allow",
  "Action": [
    "s3:GetObject",
    "s3:ListBucket"
  ],
  "Resource": "arn:aws:s3:::my-bucket/*"
}
```

2. **Public S3 Buckets**
```bash
# Bad: Bucket is public
aws s3api get-bucket-acl --bucket my-bucket
# → Grant EVERYONE ReadACP

# Good: Private bucket
block_public_acls       = true
block_public_policy     = true
ignore_public_acls      = true
restrict_public_buckets = true
```

3. **Unencrypted Data**
```hcl
# Bad: No encryption
resource "aws_rds_instance" "db" {
  engine = "postgres"
  # No encryption!
}

# Good: Encrypted
resource "aws_rds_instance" "db" {
  engine                    = "postgres"
  storage_encrypted         = true
  kms_key_id               = aws_kms_key.example.arn
}
```

4. **VPC Misconfigurations**
- Security groups allowing all ingress
- NACLs allowing all traffic
- VPC Flow Logs disabled

5. **CloudTrail Not Enabled**
- No audit trail of actions
- Can't detect unauthorized access
- Can't meet compliance requirements

**Scanning Tools:**
- Prowler (automated AWS auditing)
- CloudMapper (visualize AWS)
- ScoutSuite (multi-cloud auditing)

### 14. How would you scan AWS infrastructure for compliance?

**Answer:**

**Tools for AWS Compliance Scanning:**

1. **Prowler**
```bash
# Install
git clone https://github.com/prowler-cloud/prowler
cd prowler
poetry shell
poetry install

# Run scan
python prowler.py -v

# Scan specific service
python prowler.py -s iam,s3

# HTML report
python prowler.py --output-html report.html
```

2. **AWS Config**
```
Continuously monitors AWS resources for compliance
Checks against AWS managed rules or custom rules
```

3. **AWS Security Hub**
```
Aggregates findings from multiple services
Provides single pane of glass for security
```

**Compliance Frameworks:**

| Framework | Focus | Tools |
|-----------|-------|-------|
| CIS Benchmarks | Best practices | Prowler |
| PCI-DSS | Payment card data | AWS Config + Prowler |
| HIPAA | Healthcare | AWS Config |
| SOC 2 | Service organization | CloudTrail + Config |

## OWASP & Common Vulnerabilities

### 15. Explain the OWASP Top 10 web vulnerabilities.

**Answer:**

| # | Vulnerability | Description | Prevention |
|---|---|---|---|
| 1 | Injection | SQL, LDAP, command injection | Input validation, parameterized queries |
| 2 | Broken Authentication | Weak passwords, session fixation | MFA, strong password policy, secure session |
| 3 | Sensitive Data Exposure | Unencrypted PII, weak encryption | HTTPS, data encryption, PII masking |
| 4 | XML External Entities | XXE injection attacks | Disable XXE, validate/sanitize XML |
| 5 | Broken Access Control | Authorization bypass | Proper access controls, role-based access |
| 6 | Security Misconfiguration | Debug mode on, outdated software | Secure defaults, regular patching |
| 7 | XSS (Cross-Site Scripting) | Client-side script injection | Input validation, output encoding, CSP |
| 8 | Insecure Deserialization | Untrusted object deserialization | Avoid deserialization, use safe libraries |
| 9 | Using Known Vulnerable Components | Outdated libraries with CVEs | Dependency scanning, timely updates |
| 10 | Insufficient Logging/Monitoring | Missing security logs | Log security events, monitor alerts |

**Example: SQL Injection Prevention**

```python
# Vulnerable
user_id = request.get('id')
query = f"SELECT * FROM users WHERE id = {user_id}"
# If user_id = "1; DROP TABLE users; --"
# Query becomes: SELECT * FROM users WHERE id = 1; DROP TABLE users; --

# Fixed
user_id = request.get('id')
query = "SELECT * FROM users WHERE id = ?"
db.execute(query, (user_id,))  # Parameterized query
```

## Incident Response

### 16. How would you respond to a security incident?

**Answer:**

**Incident Response Timeline:**

```
1. DETECT (< 5 min)
   - Monitoring alert fires
   - Unusual activity noticed
   - Security team notified

2. RESPOND (< 15 min)
   - On-call security engineer
   - Incident commander assigned
   - Team assembled

3. CONTAIN (15-60 min)
   - Stop spread of compromise
   - Isolate affected systems
   - Preserve evidence

4. ERADICATE (hours-days)
   - Remove attacker access
   - Patch vulnerabilities
   - Apply mitigations

5. RECOVER (hours-days)
   - Restore systems
   - Verify integrity
   - Monitor for re-compromise

6. COMMUNICATE
   - Update management hourly
   - Notify customers if affected
   - Legal/PR coordination

7. POST-INCIDENT
   - Postmortem within 48 hours
   - Root cause analysis
   - Preventative actions
```

**Example Incident:**

```
Alert: Unauthorized access to production database
→ Lock affected user account
→ Revoke API keys
→ Export database access logs
→ Check for data exfiltration
→ Change database password
→ Restore from clean backup if needed
→ Monitor for 7 days for re-compromise
```

## Measurement & Metrics

### 17. What security metrics should you track?

**Answer:**

**Security Metrics:**

| Metric | What to Measure | Target |
|--------|---|---|
| Vulnerability Response Time | Time to fix critical vulnerability | `< 24 hours` |
| Mean Time to Detect | Time to discover breach | `< 1 hour` |
| Mean Time to Remediate | Time to fix security issue | `< 48 hours` |
| Vulnerability Density | CVEs per 1000 lines of code | `< 1` |
| Test Coverage | % of code covered by tests | `> 80%` |
| Secrets Detected | Number of secrets in code | `0` (zero tolerance) |
| Security Debt | Number of open security issues | Tracked, prioritized |
| Patch Currency | % of patches applied within 30 days | `> 95%` |

**Dashboard Example:**

```
Critical Vulnerabilities: 2 (🔴 above target of 0)
High Vulnerabilities: 8 (🟡 within target of 10)
Secrets Detected: 0 (🟢 at target)
Mean Time to Remediate: 36 hours (🟡 above target of 24h)
Test Coverage: 85% (🟢 above target of 80%)
```

## Incident Management

### 18. How would you set up a security incident response team?

**Answer:**

**Team Structure:**

```
Security Team Lead
├── Incident Commander (prioritization)
├── Operations Lead (system access, recovery)
├── Security Engineer (forensics, analysis)
├── Communication Lead (updates, PR)
└── Documentation Lead (logging, postmortem)
```

**Responsibilities:**

- **Incident Commander** — Makes decisions, coordinates team
- **Operations** — Takes actions, isolates systems, restores
- **Security** — Analyzes logs, identifies root cause
- **Communication** — Updates management, customers
- **Documentation** — Records timeline, evidence

**Runbook Example:**

```
INCIDENT: Database compromise

SEVERITY: P1 (critical)

IMMEDIATE ACTIONS:
1. Lock all database user accounts
2. Revoke API keys and tokens
3. Take database offline if needed
4. Enable audit logging
5. Begin evidence collection

ESCALATION:
- Notify CISO
- Notify CEO/Board
- Notify legal team
- Notify affected customers (within 2 hours if data involved)

RECOVERY:
- Restore from known-good backup
- Change all passwords
- Monitor for 30 days for re-compromise
```

## Advanced Topics

### 19. What is the principle of least privilege and how do you implement it?

**Answer:**
Principle of Least Privilege (PoLP) means every user/system has only the minimum permissions needed to do their job.

**Not PoLP:**
```json
{
  "Effect": "Allow",
  "Action": "*",              // All actions
  "Resource": "*"             // All resources
}
```

**PoLP:**
```json
{
  "Effect": "Allow",
  "Action": [
    "s3:GetObject",           // Only what's needed
    "s3:ListBucket"
  ],
  "Resource": "arn:aws:s3:::my-bucket/*",  // Only specific bucket
  "Condition": {
    "IpAddress": {
      "aws:SourceIp": "10.0.0.0/8"  // Only from internal network
    }
  }
}
```

**Implementation:**

1. **For Users**
   - Developers: Only can deploy to dev
   - QA: Can deploy to staging
   - DevOps: Can deploy to all
   - Contractors: Only access needed systems

2. **For Services**
   - Web server: Only read from database
   - Worker: Only write to queue
   - Logger: Only write to logs

3. **For Infrastructure**
   - Database: Only access from app servers
   - API: Only accessible from frontend
   - Admin: Only from VPN

**Benefits:**
- Limits impact of compromised account
- Easier to audit who did what
- Reduces accidental damage
- Better compliance

### 20. How would you implement zero-trust security?

**Answer:**
Zero-Trust means never automatically trust, always verify — every access request is authenticated and authorized.

**Old Model (Perimeter Security):**
```
Trust inside firewall → Verify outside firewall
Problems: Insider threats, compromised internal users
```

**Zero-Trust Model:**
```
Never trust → Always verify → Grant minimal access
Everyone, everywhere, every time
```

**Implementation:**

```
1. IDENTITY
   - Strong authentication (MFA)
   - Continuous identity verification
   - Device health checks

2. AUTHENTICATION
   - Verify identity for every action
   - Not just once at login
   - Re-verify periodically

3. AUTHORIZATION
   - Minimum necessary permissions
   - Time-based (expires after 8 hours)
   - Device-based (must be compliant device)

4. MONITORING
   - Monitor all access
   - Alert on anomalies
   - Continuous verification
```

**Example: Employee accessing AWS**

**Old way:**
```
Employee → VPN login → Automatically trusted → Can access anything
```

**Zero-Trust way:**
```
Employee → MFA → Verify device health → Verify identity →
Grant specific AWS role (1 hour) → Monitor access
```

## Assessment & Testing

### 21. What is the difference between vulnerability scanning and penetration testing?

**Answer:**

**Vulnerability Scanning:**
- Automated tool runs
- Checks against known vulnerabilities
- No human interpretation
- Fast (hours)
- Finds: Known CVEs, misconfigurations

**Example:**
```bash
trivy image myapp:latest
# Finds: CVE-2023-1234 in library X
```

---

**Penetration Testing:**
- Security professional manually tests
- Tries to exploit vulnerabilities
- Uses creativity and skill
- Slow (days/weeks)
- Finds: Logical flaws, chain of exploits, unknown vulnerabilities

**Example:**
```
Tester finds:
1. Weak password policy
2. Unencrypted password reset email
3. Chains them: Brute force password → Intercept reset email
   → Take over account
```

---

**Comparison:**

| Aspect | Scanning | Penetration Test |
|---|---|---|
| **Type** | Automated | Manual |
| **Scope** | Known vulnerabilities | All possible exploits |
| **Cost** | Low | High |
| **Time** | Fast | Slow |
| **Zero-days** | No | Possibly |
| **Business logic** | No | Yes |

**Recommendation:** Use both
- Continuous scanning in pipeline
- Periodic penetration testing (quarterly, annually)

## Exercises & Scenarios

### 22. Design a secure CI/CD pipeline

**Answer:**

```yaml
name: Secure CI/CD Pipeline

stages:
  - Scan Secrets
  - Build
  - Security Testing
  - Deploy Staging
  - Security Validation
  - Deploy Production

scan_secrets:
  stage: Scan Secrets
  script:
    - gitleaks detect --staged
    - detect-secrets scan --all-files
  fail_fast: true

build:
  stage: Build
  script:
    - npm install
    - npm run build
    - docker build -t app:$CI_COMMIT_SHA .

sast_scan:
  stage: Security Testing
  script:
    - semgrep --config=p/security-audit --json .

sca_scan:
  stage: Security Testing
  script:
    - snyk test --json

container_scan:
  stage: Security Testing
  script:
    - trivy image app:$CI_COMMIT_SHA

deploy_staging:
  stage: Deploy Staging
  script:
    - kubectl set image deployment/app \
        app=registry/app:$CI_COMMIT_SHA \
        -n staging

dast_scan:
  stage: Security Validation
  script:
    - docker run owasp/zap2docker \
        zap-baseline.py -t https://staging.example.com

security_approval:
  stage: Deploy Production
  script:
    - echo "Awaiting security team approval"
  when: manual

deploy_production:
  stage: Deploy Production
  script:
    - kubectl set image deployment/app \
        app=registry/app:$CI_COMMIT_SHA \
        -n production
```

## Key Takeaways

- **DevSecOps integrates security throughout pipeline**, not as an afterthought
- **Shift-left moves security early**, enabling faster feedback
- **Automate security checks** in CI/CD pipeline
- **Use complementary tools** — SAST + DAST, scanning + monitoring
- **Shift-left doesn't eliminate the right** — still need runtime monitoring
- **Secrets must never be in code** — use secure management tools
- **Regular scanning** finds vulnerabilities early
- **Incident response plan** minimizes damage when breaches occur
- **Least privilege** reduces impact of compromised accounts
- **Compliance-as-code** ensures consistent enforcement
